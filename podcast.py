#!/usr/bin/env python3
"""Daily podcast generator — one MP3 per feed via Claude Sonnet + OpenAI TTS.

Run independently of generate.py. Reads today's events from state.json,
generates a conversational script per feed, renders to MP3, rebuilds
docs/podcast.md. Exits cleanly if OPENAI_API_KEY is unset.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DOCS = ROOT / "docs"
AUDIO_DIR = DOCS / "audio"
PODCAST_PAGE = DOCS / "podcast.md"
SOURCES_FILE = ROOT / "sources.yaml"

AUDIO_KEEP_DAYS = 5
SCRIPT_MODEL = "claude-sonnet-5"
SCRIPT_MAX_TOKENS = 2000
SERPER_BUDGET = 5          # max search calls per feed
TTS_MODEL = "tts-1-hd"
TTS_VOICE = "onyx"

def _load_feeds_config() -> list[dict]:
    """Read feed→topic mapping from sources.yaml. Falls back to hardcoded defaults
    if the file is missing or unreadable (e.g., in test environments)."""
    _FALLBACK = [
        {"key": "technology", "title": "Technology",
         "topics": ["tech", "ai", "anthropic", "gpt", "security", "email-security",
                    "golang", "python", "postgres", "tech-research"]},
        {"key": "world",       "title": "World",       "topics": ["world", "markets"]},
        {"key": "science",     "title": "Science",
         "topics": ["science", "space", "health", "diet-exercise"]},
        {"key": "environment", "title": "Environment",
         "topics": ["climate-resilience", "climate-change", "conservation"]},
    ]
    try:
        import yaml
        raw = yaml.safe_load(SOURCES_FILE.read_text())
        feeds_raw = raw.get("feeds") or {}
        result = []
        for key, meta in feeds_raw.items():
            result.append({
                "key": key,
                "title": meta.get("title", key.title()),
                "topics": meta.get("topics") or [],
            })
        return result if result else _FALLBACK
    except Exception:
        return _FALLBACK


FEEDS: list[dict] = _load_feeds_config()


# Duplicated from generate.py intentionally — podcast.py must run standalone without
# importing the 3300-line generate.py (whose module-level code has side effects).
def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _topic_to_feed() -> dict[str, str]:
    """Map each topic string to its feed key."""
    return {topic: f["key"] for f in FEEDS for topic in f["topics"]}


def _load_feed_events(state_file: Path = STATE_FILE, date: str | None = None) -> dict[str, list[dict]]:
    """Return {feed_key: [events sorted by importance desc]} for today.

    Events whose topic isn't in any feed are silently dropped."""
    date = date or _today()
    result: dict[str, list[dict]] = {f["key"]: [] for f in FEEDS}
    if not state_file.exists():
        return result
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        return result
    stored = state.get("today_events") or {}
    if stored.get("date") != date:
        return result
    t2f = _topic_to_feed()
    for ev in stored.get("events", []):
        feed_key = t2f.get(ev.get("topic", ""))
        if feed_key:
            result[feed_key].append(ev)
    for key in result:
        result[key].sort(key=lambda e: e.get("importance", 0), reverse=True)
    return result


def _format_events_for_prompt(events: list[dict], max_events: int = 12) -> str:
    """Render top events as structured text for the Claude prompt."""
    lines = []
    for ev in events[:max_events]:
        lines.append(f"[{ev.get('topic', '?').upper()}] {ev['title']} (importance {ev.get('importance', 0)}/10)")
        if ev.get("one_liner"):
            lines.append(f"  Summary: {ev['one_liner']}")
        for t in (ev.get("takeaways") or [])[:4]:
            lines.append(f"  • {t}")
        lines.append("")
    return "\n".join(lines).strip()


def _serper_search(query: str, n: int = 3) -> list[dict]:
    """Search Google News via Serper.dev. Returns [{title, url, snippet}]."""
    key = os.environ.get("SERPER_DEV_API_KEY", "")
    if not key:
        return []
    body = json.dumps({"q": query, "num": n}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/news",
        data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return [{"title": r["title"], "url": r["link"], "snippet": r.get("snippet", "")}
                for r in result.get("news", [])]
    except Exception:
        return []


_SCRIPT_PROMPT = """\
You are a professional radio news anchor. Write a {word_target}-word conversational podcast script for the **{feed_title}** briefing, dated {date}.

You have a web search tool with a budget of {search_budget} calls. Use it when you genuinely need fresher context or a key fact to make commentary richer — not for every story.

Today's stories:
{event_text}

Script requirements:
- Natural spoken intro: greet listeners, name the feed, mention the date
- Weave stories into flowing prose — do NOT announce sub-topic headers
- Smooth transitions between stories ("Meanwhile...", "On another front...", etc.)
- Brief outro thanking listeners
- Full sentences only — no bullet points, no markdown
- Write ONLY the script text; no stage directions, no metadata
"""

_SEARCH_TOOL = {
    "name": "search",
    "description": "Search Google News for additional context. Budget is limited — use only when needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
}


def _generate_script(feed: dict, events: list[dict], date: str) -> str:
    """Run Claude Sonnet in an agentic tool-use loop to produce a podcast script.

    Returns the final script text, or empty string on failure."""
    import anthropic  # local import so missing dep is a clear error
    client = anthropic.Anthropic()

    event_text = _format_events_for_prompt(events, max_events=12)
    if not event_text:
        return ""

    prompt = _SCRIPT_PROMPT.format(
        feed_title=feed["title"],
        date=date,
        word_target="400–600",
        search_budget=SERPER_BUDGET,
        event_text=event_text,
    )
    messages: list[dict] = [{"role": "user", "content": prompt}]
    searches_used = 0

    while True:
        response = client.messages.create(
            model=SCRIPT_MODEL,
            max_tokens=SCRIPT_MAX_TOKENS,
            tools=[_SEARCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return ""

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "search":
                    if searches_used < SERPER_BUDGET:
                        hits = _serper_search(block.input.get("query", ""), n=3)
                        result_text = "\n".join(
                            f"- {h['title']}: {h['snippet']}" for h in hits
                        ) or "No results found."
                        searches_used += 1
                        _log(f"  search [{searches_used}/{SERPER_BUDGET}]: {block.input.get('query','')[:60]}")
                    else:
                        result_text = "Search budget exhausted — please write the script with information you have."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason
            break

    return ""


def _render_tts(script: str, output_path: Path) -> None:
    """Call OpenAI TTS and write MP3 bytes to output_path."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    body = json.dumps({"model": TTS_MODEL, "input": script, "voice": TTS_VOICE}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as resp:
        output_path.write_bytes(resp.read())


def _prune_old_audio(feed_dir: Path, keep_days: int = AUDIO_KEEP_DAYS,
                     today: dt.date | None = None) -> None:
    """Delete MP3 files in feed_dir older than keep_days."""
    if not feed_dir.exists():
        return
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=keep_days - 1)
    for mp3 in feed_dir.glob("*.mp3"):
        try:
            file_date = dt.date.fromisoformat(mp3.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            mp3.unlink()


def _available_episodes(feed_dir: Path) -> list[str]:
    """Return date strings (YYYY-MM-DD) for existing MP3s, newest first."""
    if not feed_dir.exists():
        return []
    dates = []
    for mp3 in feed_dir.glob("*.mp3"):
        try:
            dates.append(dt.date.fromisoformat(mp3.stem).isoformat())
        except ValueError:
            continue
    return sorted(dates, reverse=True)


def _build_podcast_page(audio_dir: Path = AUDIO_DIR, date: str | None = None) -> str:
    """Render docs/podcast.md content — one player card per feed that has audio today."""
    date = date or _today()
    lines = [
        "---",
        "title: Podcast",
        "---",
        "",
        "# Sam's News Podcast",
        "",
        "Daily audio briefings — one per feed, generated each morning around 9 AM PT.",
        "",
    ]
    any_feed = False
    for feed in FEEDS:
        feed_dir = audio_dir / feed["key"]
        today_mp3 = feed_dir / f"{date}.mp3"
        if not today_mp3.exists():
            continue
        any_feed = True
        rel = f"audio/{feed['key']}/{date}.mp3"
        player_id = f"player-{feed['key']}"
        lines += [
            f"## {feed['title']}",
            "",
            '<div class="podcast-player">',
            f'<audio id="{player_id}" controls src="{rel}"></audio>',
            '<div class="podcast-controls">',
            f'<button class="skip-btn" onclick="document.getElementById(\'{player_id}\').currentTime -= 15">&#x23EA; −15s</button>',
            f'<button class="skip-btn" onclick="document.getElementById(\'{player_id}\').currentTime += 15">+15s &#x23E9;</button>',
            "</div>",
            "</div>",
            "",
        ]
        episodes = [e for e in _available_episodes(feed_dir) if e != date]
        if episodes:
            archive_links = " · ".join(
                f'<a href="audio/{feed["key"]}/{e}.mp3">{e}</a>'
                for e in episodes[:AUDIO_KEEP_DAYS - 1]
            )
            lines += [f'<p class="podcast-archive">Previous: {archive_links}</p>', ""]
        lines.append("")
    if not any_feed:
        lines.append("_No episodes available yet. Check back after the morning run._")
        lines.append("")
    return "\n".join(lines)


def run(dry_run: bool = False) -> None:
    pass  # implemented in a later task


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Generate daily podcasts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate scripts but skip TTS and file writes")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY") and not args.dry_run:
        _log("OPENAI_API_KEY not set — skipping podcast generation")
        return

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
