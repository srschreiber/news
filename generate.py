#!/usr/bin/env python3
"""Generate tech-news briefings and rollups, partitioned by topic.

Modes:
  daily    fetch RSS sources -> cluster+score (Haiku) -> research+write (Sonnet)
  weekly   roll up the last 7 daily docs (per topic)
  monthly  roll up the month's weekly docs (per topic)
  yearly   roll up the year's monthly docs (per topic)

Sources are grouped by `topic` (sources.yaml). Each topic produces its own
files: docs/<kind>/<topic>/<stem>.md. Untagged sources fall into "general".

Design: docs/superpowers/specs/2026-07-24-rss-news-generator-design.md

The Anthropic SDK is imported lazily so `--dry-run` and unit tests that only
exercise the pure (fetch/parse/render) functions don't require a network or key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import feedparser
import yaml

# --------------------------------------------------------------------------- #
# Tunable constants (all guardrails live here)
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.yaml"
PROMPTS_DIR = ROOT / "prompts"
DOCS = ROOT / "docs"
STATE_FILE = ROOT / "state.json"
SEARCH_INDEX_FILE = DOCS / "search-index.json"

# base dir per doc kind; actual docs live under <base>/<topic>/
KIND_DIR = {
    "daily": DOCS / "news",
    "weekly": DOCS / "weekly",
    "monthly": DOCS / "monthly",
    "yearly": DOCS / "yearly",
}
DEFAULT_TOPIC = "general"

CLUSTER_MODEL = "claude-haiku-4-5"      # Stage 1 — cheap, handles bulk input
WRITE_MODEL = "claude-sonnet-5"         # Stage 2 — research + writing quality
ROLLUP_MODEL = "claude-sonnet-5"

LOOKBACK_HOURS = 24                     # first-run fallback window
MAX_ITEMS_PER_FEED = 40                 # cap noisy feeds before Stage 1
TOP_K_TO_RESEARCH = 10                  # only these events reach Stage 2 (per topic)
TOP_STORIES_N = 12                      # biggest events across all topics on the home page
MAX_TOPIC_CONCURRENCY = 4               # topics researched in parallel (cap for rate limits)
WEB_SEARCHES_PER_EVENT = 2              # research budget scales with # events...
WEB_FETCHES_PER_EVENT = 2
WEB_SEARCH_MAX_USES = 20                # ...bounded by this per-topic backstop
WEB_FETCH_MAX_USES = 20
WEB_FETCH_MAX_CONTENT_TOKENS = 8000     # per-page cap so one page can't flood
MAX_TOOL_LOOP_ITERS = 8                 # incl. pause_turn resumes
STAGE1_MAX_TOKENS = 8000
STAGE2_MAX_TOKENS = 5000                # short, dense summaries — small ceiling
STAGE2_EFFORT = "medium"                # writing task; medium trims thinking tokens
ROLLUP_MAX_TOKENS = 5000
ROLLUP_EFFORT = "medium"
SEEN_LINKS_RETENTION_DAYS = 7
SUMMARY_MAX_CHARS = 600

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)

METRICS_FILE = ROOT / "metrics.json"
# Estimated USD per 1M tokens (standard rates; cache write = 1.25x input,
# cache read = 0.1x input). These are approximations for cost tracking.
PRICES = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
}
WEB_SEARCH_COST_PER_1K = 10.0  # ~$10 per 1,000 web searches

METER_FILLED = "🔥"
METER_EMPTY = "◯"

EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "one_liner": {"type": "string"},
                    "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "theme": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "source_item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "title",
                    "one_liner",
                    "importance",
                    "theme",
                    "keywords",
                    "source_item_ids",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", file=sys.stderr, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no network, no API)
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """Approximate MkDocs' default heading slug (for deep-link anchors)."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text


def source_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "src"


def clean_summary(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


def resolve_source_url(source: dict) -> str:
    """A source is either a direct feed `url` or a Google News `query`."""
    if source.get("url"):
        return source["url"]
    if source.get("query"):
        q = urllib.parse.quote_plus(source["query"])
        return GOOGLE_NEWS_RSS.format(q=q)
    raise ValueError(f"source {source.get('name')!r} needs a 'url' or 'query'")


def load_sources(path: Path = SOURCES_FILE) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    sources = data.get("sources", [])
    for s in sources:
        s["_url"] = resolve_source_url(s)
        s["topic"] = (s.get("topic") or DEFAULT_TOPIC).strip()
    return sources


def load_research_config(path: Path = SOURCES_FILE) -> dict:
    """Per-topic web-research toggle from sources.yaml:

        research:
          default: false
          topics:
            ai: true
    """
    data = yaml.safe_load(path.read_text()) or {}
    r = data.get("research", {}) or {}
    return {"default": bool(r.get("default", False)), "topics": r.get("topics", {}) or {}}


def research_enabled(topic: str, cfg: dict) -> bool:
    return bool(cfg["topics"].get(topic, cfg["default"]))


def entry_datetime(entry: dict) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return dt.datetime(*st[:6], tzinfo=dt.timezone.utc)
    return None


def normalize_entry(
    source_name: str, topic: str, idx: int, source_key: str, entry: dict
) -> dict:
    published = entry_datetime(entry)
    return {
        "id": f"{source_key}-{idx}",
        "source": source_name,
        "topic": topic,
        "title": (entry.get("title") or "").strip(),
        "summary": clean_summary(entry.get("summary") or entry.get("description")),
        "link": (entry.get("link") or "").strip(),
        "published": published.isoformat() if published else None,
        "_published_dt": published,
    }


def filter_and_cap(
    items: list[dict],
    cutoff: dt.datetime,
    seen_links: dict[str, str],
    max_per_feed: int = MAX_ITEMS_PER_FEED,
) -> list[dict]:
    """Keep items published since `cutoff` and not already seen; cap per source.

    Items with no publish date are kept (many feeds omit it) unless already seen.
    """
    kept: list[dict] = []
    per_source: dict[str, int] = {}
    ordered = sorted(
        items,
        key=lambda it: it.get("_published_dt")
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    for it in ordered:
        if it["link"] and it["link"] in seen_links:
            continue
        pub = it.get("_published_dt")
        if pub is not None and pub < cutoff:
            continue
        n = per_source.get(it["source"], 0)
        if n >= max_per_feed:
            continue
        per_source[it["source"]] = n + 1
        kept.append(it)
    return kept


def group_by_topic(items: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it.get("topic", DEFAULT_TOPIC), []).append(it)
    return groups


def meter(score: int) -> str:
    score = max(1, min(5, int(score)))
    return METER_FILLED * score + METER_EMPTY * (5 - score)


def payload_items(items: list[dict]) -> list[dict]:
    """Strip private fields before sending to the model."""
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]


def attach_sources(events: list[dict], items: list[dict]) -> list[dict]:
    """Resolve each event's source_item_ids to {label, url} from the raw items."""
    by_id = {it["id"]: it for it in items}
    out = []
    for ev in events:
        sources, seen = [], set()
        for sid in ev.get("source_item_ids", []):
            it = by_id.get(sid)
            if not it or not it.get("link") or it["link"] in seen:
                continue
            seen.add(it["link"])
            sources.append({"label": it["source"], "url": it["link"]})
        out.append({**ev, "sources": sources})
    return out


def select_top_k(events: list[dict], k: int = TOP_K_TO_RESEARCH) -> list[dict]:
    return sorted(events, key=lambda e: e.get("importance", 0), reverse=True)[:k]


def front_matter(tags: Iterable[str]) -> str:
    uniq = sorted({t for t in tags if t})
    if not uniq:
        return ""
    return "\n".join(["---", "tags:"] + [f"  - {t}" for t in uniq] + ["---", ""])


def daily_tags(events: list[dict], date: str, topic: str) -> list[str]:
    tags: list[str] = [date[:4], date[:7], topic]  # YYYY, YYYY-MM, topic
    for ev in events:
        tags.extend(ev.get("keywords", []))
        if ev.get("theme"):
            tags.append(ev["theme"])
    return tags


# --------------------------------------------------------------------------- #
# Anthropic client (lazy)
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (no dependency). Sets vars not already in the env.

    In CI the key comes from GitHub Secrets as a real env var, so .env is unused
    there; this is just for convenient local runs.
    """
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


def _cost_of(by_model: dict, web_searches: int) -> float:
    total = web_searches * WEB_SEARCH_COST_PER_1K / 1000.0
    for model, t in by_model.items():
        p = PRICES.get(model)
        if not p:
            continue
        total += (
            t["input"] * p["input"]
            + t["output"] * p["output"]
            + t["cache_write"] * p["input"] * 1.25
            + t["cache_read"] * p["input"] * 0.1
        ) / 1_000_000.0
    return total


class Metrics:
    """Thread-safe accumulator of token usage + web searches → estimated USD,
    tracked per topic (topics run in parallel) and aggregated globally."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.topics: dict[str, dict] = {}

    def add(self, topic: str, model: str, usage) -> None:
        if usage is None:
            return
        with self._lock:
            top = self.topics.setdefault(topic, {"calls": 0, "web_searches": 0, "by_model": {}})
            top["calls"] += 1
            m = top["by_model"].setdefault(
                model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
            )
            m["input"] += getattr(usage, "input_tokens", 0) or 0
            m["output"] += getattr(usage, "output_tokens", 0) or 0
            m["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
            m["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
            stu = getattr(usage, "server_tool_use", None)
            if stu is not None:
                top["web_searches"] += getattr(stu, "web_search_requests", 0) or 0

    def estimate_usd(self) -> float:
        return sum(_cost_of(v["by_model"], v["web_searches"]) for v in self.topics.values())

    def record(self) -> dict:
        by_topic, agg_model, calls, ws = {}, {}, 0, 0
        for topic, v in sorted(self.topics.items()):
            by_topic[topic] = {
                "calls": v["calls"],
                "web_searches": v["web_searches"],
                "tokens_by_model": v["by_model"],
                "estimated_cost_usd": round(_cost_of(v["by_model"], v["web_searches"]), 4),
            }
            calls += v["calls"]
            ws += v["web_searches"]
            for model, t in v["by_model"].items():
                a = agg_model.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
                for k in t:
                    a[k] += t[k]
        return {
            "calls": calls,
            "web_searches": ws,
            "tokens_by_model": agg_model,
            "estimated_cost_usd": round(self.estimate_usd(), 4),
            "by_topic": by_topic,
        }


METRICS = Metrics()


_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            import anthropic  # here so pure paths don't need the dep/key

            _load_dotenv()
            _CLIENT = anthropic.Anthropic()  # httpx-based; safe to share across threads
    return _CLIENT


def _sys(prompt_name: str) -> list[dict]:
    text = (PROMPTS_DIR / prompt_name).read_text()
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _extract_briefing(text: str) -> str:
    """Drop any leading narration the model emits between web searches.

    Stage 2 interleaves reasoning text ("let me check X...") with tool calls;
    concatenating all text blocks glues that narration before the real briefing.
    The briefing always starts at a '## ' heading (per the prompt), so cut to it.
    """
    text = text.strip()
    if text.startswith("## "):
        return text
    idx = text.find("\n## ")
    return text[idx + 1:].strip() if idx != -1 else text


def stage1_cluster(items: list[dict], topic: str) -> list[dict]:
    """Cluster+score+extract keywords via Haiku with structured output.

    All items belong to a single `topic` (e.g. gaming, world, general); the
    model clusters and scores within that topic's world.
    """
    client = _client()
    user = json.dumps(
        {"topic": topic, "items": payload_items(items)}, ensure_ascii=False
    )
    for attempt in (1, 2):
        resp = client.messages.create(
            model=CLUSTER_MODEL,
            max_tokens=STAGE1_MAX_TOKENS,
            system=_sys("cluster.md"),
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": EVENTS_SCHEMA}},
        )
        METRICS.add(topic, CLUSTER_MODEL, resp.usage)
        try:
            events = json.loads(_text_of(resp)).get("events", [])
            if not events:
                log("stage 1 returned no events")
            return events
        except json.JSONDecodeError as e:
            log(f"stage 1 JSON parse failed (attempt {attempt}): {e}")
    return []


def stage2_write(events: list[dict], date: str, topic: str) -> str:
    """Research (discretionary) + write the daily briefing body (below the H1)."""
    client = _client()
    n = max(1, len(events))
    search_uses = min(WEB_SEARCH_MAX_USES, n * WEB_SEARCHES_PER_EVENT)
    fetch_uses = min(WEB_FETCH_MAX_USES, n * WEB_FETCHES_PER_EVENT)
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": search_uses,
        },
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": fetch_uses,
            "max_content_tokens": WEB_FETCH_MAX_CONTENT_TOKENS,
        },
    ]
    user = json.dumps({"date": date, "topic": topic, "events": events}, ensure_ascii=False)
    messages: list[dict] = [{"role": "user", "content": user}]
    final = None
    for i in range(MAX_TOOL_LOOP_ITERS):
        with client.messages.stream(
            model=WRITE_MODEL,
            max_tokens=STAGE2_MAX_TOKENS,
            output_config={"effort": STAGE2_EFFORT},
            system=_sys("write.md"),
            tools=tools,
            messages=messages,
        ) as stream:
            final = stream.get_final_message()
        METRICS.add(topic, WRITE_MODEL, final.usage)
        if final.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": final.content})
            log(f"stage 2 pause_turn — resuming ({i + 1}/{MAX_TOOL_LOOP_ITERS})")
            continue
        break
    else:
        log("stage 2 hit MAX_TOOL_LOOP_ITERS — using best output so far")
    return _extract_briefing(_text_of(final)) if final else ""


def rollup_write(period_label: str, topic: str, docs: list[dict]) -> str:
    """One Sonnet call, no tools: synthesize child docs into a rollup body."""
    client = _client()
    user = json.dumps(
        {"period": period_label, "topic": topic, "documents": docs}, ensure_ascii=False
    )
    resp = client.messages.create(
        model=ROLLUP_MODEL,
        max_tokens=ROLLUP_MAX_TOKENS,
        output_config={"effort": ROLLUP_EFFORT},
        system=_sys("rollup.md"),
        messages=[{"role": "user", "content": user}],
    )
    METRICS.add(topic, ROLLUP_MODEL, resp.usage)
    return _text_of(resp).strip()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json unreadable — treating as first run")
    return {}


def compute_cutoff(state: dict, now: dt.datetime) -> dt.datetime:
    last = state.get("last_run")
    if last:
        try:
            return dt.datetime.fromisoformat(last)
        except ValueError:
            pass
    return now - dt.timedelta(hours=LOOKBACK_HOURS)


def prune_seen(seen: dict[str, str], now: dt.datetime) -> dict[str, str]:
    horizon = (now - dt.timedelta(days=SEEN_LINKS_RETENTION_DAYS)).date().isoformat()
    return {k: v for k, v in seen.items() if v >= horizon}


def save_state(state: dict, items: list[dict], run_start: dt.datetime) -> None:
    seen = prune_seen(dict(state.get("seen_links", {})), run_start)
    today = run_start.date().isoformat()
    for it in items:
        if it.get("link"):
            seen[it["link"]] = today
    STATE_FILE.write_text(
        json.dumps({"last_run": run_start.isoformat(), "seen_links": seen}, indent=2)
        + "\n"
    )


def record_metrics(mode: str, run_start: dt.datetime) -> None:
    """Log a cost summary and append a per-run record to metrics.json."""
    rec = {"timestamp": run_start.isoformat(), "mode": mode, **METRICS.record()}
    history: list = []
    if METRICS_FILE.exists():
        try:
            history = json.loads(METRICS_FILE.read_text())
        except json.JSONDecodeError:
            history = []
    history.append(rec)
    METRICS_FILE.write_text(json.dumps(history, indent=2) + "\n")
    log(f"cost: ~${rec['estimated_cost_usd']:.3f} total  "
        f"(calls={rec['calls']}, web_searches={rec['web_searches']})")
    for topic, tv in rec["by_topic"].items():
        log(f"  {topic}: ~${tv['estimated_cost_usd']:.3f} "
            f"(searches={tv['web_searches']}, calls={tv['calls']})")


# --------------------------------------------------------------------------- #
# Search index
# --------------------------------------------------------------------------- #
def load_search_index() -> list[dict]:
    if SEARCH_INDEX_FILE.exists():
        try:
            return json.loads(SEARCH_INDEX_FILE.read_text())
        except json.JSONDecodeError:
            log("search-index.json unreadable — starting fresh")
    return []


def update_search_index(events: list[dict], date: str, topic: str) -> None:
    index = load_search_index()
    # idempotent re-runs: drop prior records for this (date, topic)
    index = [r for r in index if not (r.get("date") == date and r.get("topic") == topic)]
    for ev in events:
        index.append(
            {
                "date": date,
                "topic": topic,
                "title": ev["title"],
                "summary": (ev.get("one_liner") or "").strip(),
                "theme": ev.get("theme", ""),
                "importance": ev.get("importance", 0),
                "keywords": ev.get("keywords", []),
                "url": f"news/{topic}/{date}/#{slugify(ev['title'])}",
                "sources": ev.get("sources", []),
            }
        )
    index.sort(key=lambda r: (r["date"], -r.get("importance", 0)), reverse=True)
    SEARCH_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Doc writing
# --------------------------------------------------------------------------- #
def write_doc(path: Path, fm: str, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    if fm:
        parts.append(fm)
    parts.append(f"# {title}\n")
    parts.append(body.rstrip() + "\n")
    path.write_text("\n".join(parts))
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path
    log(f"wrote {shown}")


def quiet_day_body(topic: str) -> str:
    return f"## TL;DR\n\n- Quiet day — no notable {topic} news in this window.\n"


def render_briefing(events: list[dict], topic: str, tldr_n: int = 6) -> str:
    """Deterministically render a briefing from Stage-1 events — no Stage 2, no
    web research. Uses the one-sentence summary Haiku already produced. Cheapest
    path; also guarantees format (meters, anchors, citations)."""
    if not events:
        return quiet_day_body(topic)
    ev = sorted(events, key=lambda e: e.get("importance", 0), reverse=True)

    lines = ["## TL;DR", ""]
    for e in ev[:tldr_n]:
        lines.append(f"- {meter(e.get('importance', 0))} [{e['title']}](#{slugify(e['title'])})")
    lines.append("")

    by_theme: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in ev:
        t = (e.get("theme") or "Other").strip() or "Other"
        if t not in by_theme:
            by_theme[t] = []
            order.append(t)
        by_theme[t].append(e)

    for t in order:
        lines += [f"## {t}", ""]
        for e in by_theme[t]:
            lines.append(f"### {e['title']}")
            summary = (e.get("one_liner") or "").strip()
            lines.append(f"{meter(e.get('importance', 0))} {summary}".rstrip())
            srcs = ", ".join(f"[{s['label']}]({s['url']})" for s in e.get("sources", []))
            if srcs:
                lines.append(f"Sources: {srcs}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def minimal_body_from_items(items: list[dict]) -> str:
    """Fallback when Stage 1 fails: list raw items grouped by source, cited."""
    lines = ["## TL;DR\n", "- Automated summary unavailable; raw items below.\n"]
    by_source: dict[str, list[dict]] = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    for source, its in by_source.items():
        lines.append(f"\n## {source}\n")
        for it in its[:10]:
            lines.append(f"- [{it['title']}]({it['link']})" if it["link"] else f"- {it['title']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Index page
# --------------------------------------------------------------------------- #
def _topics_in(base: Path) -> list[str]:
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def _dated_stems(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted((p.stem for p in directory.glob("*.md")), reverse=True)


def _top_stories_section(index: list[dict]) -> list[str]:
    """The biggest events across ALL topics for the most recent day."""
    if not index:
        return []
    latest = max(r["date"] for r in index)
    todays = sorted(
        (r for r in index if r["date"] == latest),
        key=lambda r: (r.get("importance", 0)),
        reverse=True,
    )[:TOP_STORIES_N]
    if not todays:
        return []
    lines = [f"## Top stories — {latest}", ""]
    for r in todays:
        desc = (r.get("summary") or "").strip()
        desc = f" — {desc}" if desc else ""
        lines.append(
            f"- {meter(r.get('importance', 0))} [{r['title']}]({r['url']}){desc} "
            f"· _{r.get('topic', '')}_"
        )
    lines.append("")
    return lines


def rebuild_index() -> None:
    lines = [
        "# Tech News",
        "",
        "Daily tech-news briefings by topic, plus weekly / monthly / yearly "
        "rollups. Use the [keyword search](search.md) to filter by term, date, "
        "topic, and importance.",
        "",
    ]
    lines += _top_stories_section(load_search_index())

    kind_labels = [
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
        ("yearly", "Yearly"),
    ]
    topics = sorted(
        {t for kind, _ in kind_labels for t in _topics_in(KIND_DIR[kind])}
    )
    if topics:
        lines += ["## Browse by topic", ""]
    for topic in topics:
        lines.append(f"### {topic}\n")
        for kind, label in kind_labels:
            directory = KIND_DIR[kind] / topic
            stems = _dated_stems(directory)[:8]
            if not stems:
                continue
            rel = f"{KIND_DIR[kind].name}/{topic}"
            links = " · ".join(f"[{s}]({rel}/{s}.md)" for s in stems)
            lines.append(f"- **{label}:** {links}")
        lines.append("")
    (DOCS / "index.md").write_text("\n".join(lines) + "\n")
    log("rebuilt docs/index.md")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_all(sources: list[dict]) -> list[dict]:
    items: list[dict] = []
    for src in sources:
        key = source_slug(src["name"])
        try:
            parsed = feedparser.parse(src["_url"])
        except Exception as e:  # never let one feed kill the run
            log(f"feed {src['name']!r} failed: {e}")
            continue
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            log(f"feed {src['name']!r} returned no entries (bozo)")
            continue
        for i, entry in enumerate(parsed.entries):
            items.append(normalize_entry(src["name"], src["topic"], i, key, dict(entry)))
    return items


# --------------------------------------------------------------------------- #
# Rollup child selection
# --------------------------------------------------------------------------- #
def _read_doc_stripped(path: Path) -> str:
    text = path.read_text()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


def _parse_date_stem(stem: str) -> dt.date | None:
    # Strictly YYYY-MM-DD; reject ISO-week ("2026-W30") and other forms that
    # date.fromisoformat() would otherwise accept on Python 3.11+.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
        return None
    try:
        return dt.date.fromisoformat(stem)
    except ValueError:
        return None


ROLLUP_TITLES = {
    "weekly": "Tech News — {topic} — Week {stem}",
    "monthly": "Tech News — {topic} — {stem}",
    "yearly": "Tech News — {topic} — {stem} in Review",
}
ROLLUP_CHILD_KIND = {"weekly": "daily", "monthly": "weekly", "yearly": "monthly"}


def gather_children(mode: str, topic: str, now: dt.datetime) -> tuple[list[Path], str]:
    """Return (child doc paths, output filename stem) for a topic's rollup."""
    child_dir = KIND_DIR[ROLLUP_CHILD_KIND[mode]] / topic
    today = now.date()
    if mode == "weekly":
        start = today - dt.timedelta(days=6)
        kids = [
            p for p in sorted(child_dir.glob("*.md"))
            if (d := _parse_date_stem(p.stem)) and start <= d <= today
        ]
        iso_year, iso_week, _ = today.isocalendar()
        return kids, f"{iso_year}-W{iso_week:02d}"
    if mode == "monthly":
        target = (today.replace(day=1) - dt.timedelta(days=1)) if today.day == 1 else today
        kids = []
        for p in sorted(child_dir.glob("*.md")):
            m = re.match(r"(\d{4})-W(\d{2})", p.stem)
            if not m:
                continue
            monday = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            if monday.year == target.year and monday.month == target.month:
                kids.append(p)
        return kids, f"{target.year}-{target.month:02d}"
    if mode == "yearly":
        target_year = today.year - 1 if (today.month == 1 and today.day == 1) else today.year
        kids = [p for p in sorted(child_dir.glob("*.md")) if p.stem.startswith(str(target_year))]
        return kids, str(target_year)
    raise ValueError(mode)


# --------------------------------------------------------------------------- #
# Mode runners
# --------------------------------------------------------------------------- #
def _process_topic(
    topic: str, topic_items: list[dict], date: str, dry_run: bool, research: bool
) -> dict:
    """Do a topic's Stage 1 (+ optional Stage 2 research) and RETURN what to write.

    Runs in a worker thread — it performs API/compute only and never touches the
    shared files (search-index.json, state.json, index.md); the main thread does
    all writes so there are no races.

    research=True  -> Stage 2 (Sonnet + web tools) on the top-K events.
    research=False -> render the briefing straight from the Stage-1 summaries
                      (Haiku only; cheap and fast).
    """
    base_fm = front_matter([date[:4], date[:7], topic])
    doc_path = KIND_DIR["daily"] / topic / f"{date}.md"
    title = f"Tech News — {topic} — {date}"

    if not topic_items:
        log(f"[{topic}] no fresh items — quiet-day doc")
        return {"topic": topic, "kind": "quiet", "doc_path": doc_path,
                "fm": base_fm, "title": title, "body": quiet_day_body(topic)}

    t0 = time.monotonic()
    events = attach_sources(stage1_cluster(topic_items, topic), topic_items)
    mode = "research" if research else "summarize"
    log(f"[{topic}] deduped {len(topic_items)} RSS posts -> {len(events)} events ({mode})")

    if dry_run:
        return {"topic": topic, "kind": "dry", "top": select_top_k(events)}

    if not events:
        body, fm, index_events = minimal_body_from_items(topic_items), base_fm, None
    elif research:
        top = select_top_k(events)
        body = stage2_write(top, date, topic) or minimal_body_from_items(topic_items)
        fm, index_events = front_matter(daily_tags(top, date, topic)), top
    else:
        body = render_briefing(events, topic)
        fm, index_events = front_matter(daily_tags(events, date, topic)), events

    log(f"[{topic}] done in {time.monotonic() - t0:.0f}s")
    return {"topic": topic, "kind": "written", "doc_path": doc_path,
            "fm": fm, "title": title, "body": body, "top": index_events}


def run_daily(dry_run: bool, no_research: bool = False) -> None:
    run_start = now_utc()
    date = run_start.date().isoformat()
    state = load_state()
    cutoff = compute_cutoff(state, run_start)
    seen = state.get("seen_links", {})
    log(f"daily {date}: cutoff={cutoff.isoformat()} seen_links={len(seen)}")

    sources = load_sources()
    cfg = load_research_config()
    raw = fetch_all(sources)
    items = filter_and_cap(raw, cutoff, seen)
    groups = group_by_topic(items)
    all_topics = sorted({s["topic"] for s in sources})

    def wants_research(t: str) -> bool:
        return (not no_research) and research_enabled(t, cfg)

    researched = [t for t in all_topics if wants_research(t)]
    log(f"fetched {len(raw)} raw RSS posts, {len(items)} kept across "
        f"{len(all_topics)} topics; web research on for: {researched or 'none'}")

    # Stage 1 (+2) per topic, in parallel threads. Writes happen after, serially.
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_TOPIC_CONCURRENCY) as ex:
        futs = {
            ex.submit(_process_topic, t, groups.get(t, []), date, dry_run, wants_research(t)): t
            for t in all_topics
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # one topic failing must not sink the rest
                log(f"[{futs[fut]}] failed: {e}")

    # Serialized writes in the main thread (deterministic order).
    for r in sorted(results, key=lambda r: r["topic"]):
        if r["kind"] == "dry":
            print(json.dumps({"topic": r["topic"], "date": date, "top_events": r["top"]},
                             ensure_ascii=False, indent=2))
            continue
        write_doc(r["doc_path"], r["fm"], r["title"], r["body"])
        if r.get("top"):
            update_search_index(r["top"], date, r["topic"])

    if not dry_run:
        save_state(state, items, run_start)
        rebuild_index()
        record_metrics("daily", run_start)
    log("daily run complete")


def run_rollup(mode: str, dry_run: bool) -> None:
    run_start = now = now_utc()
    # topics = those present in the child kind's directory, plus configured ones
    child_base = KIND_DIR[ROLLUP_CHILD_KIND[mode]]
    topics = sorted(set(_topics_in(child_base)) | {s["topic"] for s in load_sources()})

    for topic in topics:
        children, stem = gather_children(mode, topic, now)
        out_path = KIND_DIR[mode] / topic / f"{stem}.md"
        title = ROLLUP_TITLES[mode].format(topic=topic, stem=stem)
        log(f"[{topic}] {mode} {stem}: {len(children)} child docs")

        if not children:
            if not dry_run:
                write_doc(out_path, front_matter([stem, topic]), title, "## TL;DR\n\n- Nothing to roll up yet.\n")
            continue

        docs = [
            {
                "title": p.stem,
                "path": Path(os.path.relpath(p, out_path.parent)).as_posix(),
                "markdown": _read_doc_stripped(p),
            }
            for p in children
        ]

        if dry_run:
            print(json.dumps({"topic": topic, "period": stem, "children": [d["title"] for d in docs]}, indent=2))
            continue

        body = rollup_write(f"{mode} ({stem})", topic, docs)
        if not body:
            links = "\n".join(f"- [{d['title']}]({d['path']})" for d in docs)
            body = f"## TL;DR\n\n- Rollup unavailable; child docs:\n\n{links}\n"
        write_doc(out_path, front_matter([stem, topic]), title, body)

    if not dry_run:
        rebuild_index()
        record_metrics(mode, run_start)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate tech-news briefings.")
    parser.add_argument(
        "mode", nargs="?", default="daily",
        choices=["daily", "weekly", "monthly", "yearly"],
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run Stage 1 / child selection and print, without Stage 2 or writing.",
    )
    parser.add_argument(
        "--no-research", action="store_true",
        help="Force web research OFF for all topics (overrides sources.yaml research config).",
    )
    args = parser.parse_args(argv)

    if args.mode == "daily":
        run_daily(args.dry_run, no_research=args.no_research)
    else:
        run_rollup(args.mode, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
