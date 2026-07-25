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
READ_MODEL = "claude-haiku-4-5"         # Stage 2a — reads/fetches pages cheaply
WRITE_MODEL = "claude-sonnet-5"         # Stage 2b — polishes final summaries
ROLLUP_MODEL = "claude-sonnet-5"

LOOKBACK_HOURS = 24                     # first-run fallback window
MAX_ITEMS_PER_FEED = 25                 # cap noisy feeds before Stage 1
EVENTS_PER_TOPIC = 10                   # events shown in each topic's doc
RESEARCH_PER_TOPIC = 3                  # of a topic's top events, consider these for research
MIN_RESEARCH_IMPORTANCE = 4             # only research events at least this important (1-5)
TOP_STORIES_N = 12                      # biggest events across all topics on the home page
MAX_TOPIC_CONCURRENCY = 4               # topics researched in parallel (cap for rate limits)
WEB_SEARCHES_PER_EVENT = 2              # HARD per-event search cap (Haiku read call)
WEB_FETCHES_PER_EVENT = 2               # HARD per-event fetch cap
GLOBAL_SEARCH_SAFETY = 50               # run-wide safety net (rarely hit)
MAX_RESEARCHED_EVENTS = GLOBAL_SEARCH_SAFETY // WEB_SEARCHES_PER_EVENT  # ~25 events/run
WEB_FETCH_MAX_CONTENT_TOKENS = 8000     # HARD per-page cap — fine now, it lands on cheap Haiku
MAX_SOURCES_PER_EVENT = 6               # distinct source links shown per event
MAX_TOOL_LOOP_ITERS = 8                 # incl. pause_turn resumes
STAGE1_MAX_TOKENS = 16000               # one global clustering pass over all feeds
READ_MAX_TOKENS = 2000                  # Haiku read: just a factual extract
STAGE2_MAX_TOKENS = 5000                # Sonnet polish: short dense summaries (batched)
STAGE2_EFFORT = "low"                   # scoped writing task; low trims thinking cost
ROLLUP_MAX_TOKENS = 5000
ROLLUP_EFFORT = "medium"
SEEN_LINKS_RETENTION_DAYS = 7
SUMMARY_MAX_CHARS = 300

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

# Stage 2a (Haiku read): a factual extract of ONE event + the sources it used.
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "extract": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
                "required": ["label", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["extract", "sources"],
    "additionalProperties": False,
}

# Stage 2b (Sonnet polish): a final summary per event, keyed by its ref — batched
# so Sonnet writes all summaries in one cheap call from Haiku's small extracts.
POLISH_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["ref", "summary"],
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


def load_custom_instructions(path: Path = SOURCES_FILE) -> str:
    """Free-text editorial instructions from sources.yaml, appended to both LLM
    prompts (e.g. house style, or 'refer to Trump as the president')."""
    data = yaml.safe_load(path.read_text()) or {}
    return (data.get("custom_instructions") or "").strip()


def entry_datetime(entry: dict) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return dt.datetime(*st[:6], tzinfo=dt.timezone.utc)
    return None


def entry_outlet(entry: dict, fallback: str) -> str:
    """Real publishing outlet. Google News RSS items carry the true outlet in a
    <source> element (feedparser: entry['source']['title']) — use that so links
    read 'The Register' instead of the feed name 'Google News — …'."""
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"].strip()
    return fallback


def normalize_entry(
    source_name: str, topic: str, idx: int, source_key: str, entry: dict
) -> dict:
    published = entry_datetime(entry)
    return {
        "id": f"{source_key}-{idx}",
        "source": entry_outlet(entry, source_name),
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
    today: str | None = None,
) -> list[dict]:
    """Keep items published since `cutoff` and not already seen; cap per source.

    Items with no publish date are kept (many feeds omit it) unless already seen.
    `seen` means seen on a *prior* day: when `today` is given, items first seen
    today are still kept, so a same-day re-run can rebuild the full day. Cross-day
    dedup (items seen yesterday or earlier) always applies.
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
        link = it["link"]
        if link and link in seen_links and (today is None or seen_links[link] < today):
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


def _source_badge(origin: str) -> str:
    """GitHub-style pill tagging a source as from the RSS feed or from research."""
    label = "Research" if origin == "research" else "RSS"
    return f'<span class="src-badge src-{origin}">{label}</span>'


def payload_items(items: list[dict]) -> list[dict]:
    """Strip private fields before sending to the model."""
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]


def attach_sources(events: list[dict], items: list[dict]) -> list[dict]:
    """Resolve each event's source_item_ids to {label, url} from the raw items."""
    by_id = {it["id"]: it for it in items}
    out = []
    for ev in events:
        # Dedupe by outlet (feed label), not by link — Google News gives each
        # article a unique redirect URL, so 40 articles from one feed would
        # otherwise become 40 "sources". One link per outlet.
        sources, seen_labels = [], set()
        for sid in ev.get("source_item_ids", []):
            it = by_id.get(sid)
            if not it or not it.get("link"):
                continue
            label = it["source"]
            if label in seen_labels:
                continue
            seen_labels.add(label)
            sources.append({"label": label, "url": it["link"], "origin": "rss"})
        out.append({**ev, "sources": sources})
    return out


def select_top_k(events: list[dict], k: int = EVENTS_PER_TOPIC) -> list[dict]:
    return sorted(events, key=lambda e: e.get("importance", 0), reverse=True)[:k]


def merge_enrichment(events: list[dict], enriched: list[dict]) -> list[dict]:
    """Overlay researched summaries/sources onto matching events (by title).

    Researched events get the enriched summary + RSS sources merged with any
    researched web sources (deduped). Non-researched events are unchanged, so
    their Sources line stays RSS-only.
    """
    by_ref = {e.get("ref"): e for e in enriched if e.get("ref")}
    out = []
    for ev in events:
        e = dict(ev)
        hit = by_ref.get(ev.get("ref"))
        if hit:
            e["one_liner"] = (hit.get("summary") or e.get("one_liner", "")).strip()
            # Web pages the model actually read are the most useful links, so
            # list them first; then RSS outlets. Dedupe by outlet/label.
            # Cap research sources to what it could actually have fetched — the
            # reader sometimes lists outlets it saw in search but didn't open.
            web = [{"label": s.get("label") or "source", "url": s["url"], "origin": "research"}
                   for s in hit.get("sources", []) if s.get("url")][:WEB_FETCHES_PER_EVENT]
            merged, seen = [], set()
            for s in web + list(e.get("sources", [])):
                if s["label"] in seen:
                    continue
                seen.add(s["label"])
                merged.append(s)
            e["sources"] = merged
            e["researched"] = True
        out.append(e)
    return out


def assign_topics(events: list[dict], items: list[dict]) -> list[dict]:
    """Derive each event's topics from the feeds its source items came from.

    Sets `topics` (sorted unique) and `primary_topic` (the topic contributing the
    most source items; ties broken alphabetically)."""
    by_id = {it["id"]: it for it in items}
    for e in events:
        tops = [by_id[sid]["topic"] for sid in e.get("source_item_ids", []) if sid in by_id]
        e["topics"] = sorted(set(tops)) or [DEFAULT_TOPIC]
        counts: dict[str, int] = {}
        for t in tops:
            counts[t] = counts.get(t, 0) + 1
        e["primary_topic"] = (
            min(counts, key=lambda t: (-counts[t], t)) if counts else DEFAULT_TOPIC
        )
    return events


def select_research(
    events: list[dict], research_topics: list[str], max_events: int = MAX_RESEARCHED_EVENTS
) -> list[dict]:
    """Pick which events to research, BREADTH-first and deduped.

    Each research-enabled topic contributes its top RESEARCH_PER_TOPIC events, but
    we interleave round-robin (round 0 = every topic's #1, round 1 = every topic's
    #2, ...) so distinct topics are covered before going deeper. A story spanning
    several topics is chosen once (researched once, reused everywhere). Capped at
    `max_events` as the run-wide safety net."""
    # Each topic's top RESEARCH_PER_TOPIC events, but only those important enough
    # to be worth researching. If none of a topic's top events clear the bar, that
    # topic contributes nothing (no research spent on a quiet/low-value topic).
    by_topic = {
        t: [e for e in
            sorted((e for e in events if t in e.get("topics", [])),
                   key=lambda e: e.get("importance", 0), reverse=True)[:RESEARCH_PER_TOPIC]
            if e.get("importance", 0) >= MIN_RESEARCH_IMPORTANCE]
        for t in research_topics
    }
    chosen, chosen_ids = [], set()
    for rnd in range(RESEARCH_PER_TOPIC):
        for t in research_topics:
            lst = by_topic[t]
            if rnd < len(lst) and id(lst[rnd]) not in chosen_ids:
                chosen_ids.add(id(lst[rnd]))
                chosen.append(lst[rnd])
                if len(chosen) >= max_events:
                    return chosen
    return chosen


def research_events(selected: list[dict], date: str) -> list[dict]:
    """Two-stage research: HAIKU reads each selected event in parallel (its own
    call -> hard per-event search cap), then SONNET polishes all extracts in one
    batched call. Returns enrichment ({ref, summary, sources}) for merge."""
    if not selected:
        return []
    for i, e in enumerate(selected):
        e["ref"] = f"e{i}"

    # Stage 2a — Haiku reads pages (parallel).
    reads: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_TOPIC_CONCURRENCY) as ex:
        futs = {ex.submit(stage2a_read, e, date): e for e in selected}
        for fut in as_completed(futs):
            ev = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:  # a failed read just keeps its RSS summary
                log(f"[{ev['ref']}] read failed: {exc}")
                r = None
            if r:
                reads.append({"ref": ev["ref"], "title": ev["title"],
                              "one_liner": ev.get("one_liner", ""),
                              "extract": r["extract"], "sources": r.get("sources", [])})

    # Stage 2b — Sonnet polishes all extracts in one call.
    polished = stage2b_polish(reads, date)
    return [
        {"ref": rd["ref"],
         "summary": polished.get(rd["ref"]) or rd["extract"],
         "sources": rd["sources"]}
        for rd in reads
    ]


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
    ci = load_custom_instructions()
    if ci:
        text += "\n\n## Editorial instructions (must follow)\n" + ci + "\n"
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


def stage1_cluster(items: list[dict]) -> list[dict]:
    """Cluster+score+extract keywords via Haiku with structured output.

    ONE global pass over items from all feeds/topics — the same story surfaced
    under multiple topics is merged into a single event (topics + merged sources
    are derived from source_item_ids in code afterwards).
    """
    client = _client()
    user = json.dumps({"items": payload_items(items)}, ensure_ascii=False)
    for attempt in (1, 2):
        resp = client.messages.create(
            model=CLUSTER_MODEL,
            max_tokens=STAGE1_MAX_TOKENS,
            system=_sys("cluster.md"),
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": EVENTS_SCHEMA}},
        )
        METRICS.add("(clustering)", CLUSTER_MODEL, resp.usage)
        try:
            events = json.loads(_text_of(resp)).get("events", [])
            if not events:
                log("stage 1 returned no events")
            return events
        except json.JSONDecodeError as e:
            log(f"stage 1 JSON parse failed (attempt {attempt}): {e}")
    return []


def stage2a_read(event: dict, date: str) -> dict | None:
    """Stage 2a — HAIKU reads one event: googles it (web_search) and reads the
    best page(s) (web_fetch, up to 8000 tokens — cheap on Haiku), and returns a
    factual extract + the outlets it actually used. Uses the basic web-tool
    variants (Haiku tier). Returns None on any failure (event keeps RSS summary)."""
    client = _client()
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCHES_PER_EVENT},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": WEB_FETCHES_PER_EVENT,
         "max_content_tokens": WEB_FETCH_MAX_CONTENT_TOKENS},
    ]
    payload = {
        "title": event["title"], "one_liner": event.get("one_liner", ""),
        "keywords": event.get("keywords", []), "topics": event.get("topics", []),
        "sources": [s["url"] for s in event.get("sources", [])],
    }
    user = json.dumps(
        {"date": date, "max_searches": WEB_SEARCHES_PER_EVENT, "event": payload},
        ensure_ascii=False,
    )
    messages: list[dict] = [{"role": "user", "content": user}]
    final = None
    for _ in range(MAX_TOOL_LOOP_ITERS):
        try:
            with client.messages.stream(
                model=READ_MODEL,
                max_tokens=READ_MAX_TOKENS,
                output_config={"format": {"type": "json_schema", "schema": READ_SCHEMA}},
                system=_sys("read.md"),
                tools=tools,
                messages=messages,
            ) as stream:
                final = stream.get_final_message()
        except Exception as e:  # e.g. Haiku can't use these web tools -> degrade
            log(f"read failed ({event.get('title', '?')[:40]}): {e}")
            return None
        METRICS.add("(read)", READ_MODEL, final.usage)
        if final.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": final.content})
            continue
        break
    if not final:
        return None
    try:
        obj = json.loads(_text_of(final))
        return obj if (obj.get("extract") or "").strip() else None
    except json.JSONDecodeError:
        return None


def stage2b_polish(reads: list[dict], date: str) -> dict:
    """Stage 2b — SONNET writes the final summaries from Haiku's extracts, in ONE
    batched call (Sonnet sees only the short extracts, never raw pages). Returns
    {ref: summary}. On failure, callers fall back to the raw extract."""
    if not reads:
        return {}
    client = _client()
    payload = [
        {"ref": r["ref"], "title": r["title"], "one_liner": r.get("one_liner", ""),
         "extract": r["extract"]}
        for r in reads
    ]
    user = json.dumps({"date": date, "events": payload}, ensure_ascii=False)
    try:
        resp = client.messages.create(
            model=WRITE_MODEL,
            max_tokens=STAGE2_MAX_TOKENS,
            output_config={"effort": STAGE2_EFFORT,
                           "format": {"type": "json_schema", "schema": POLISH_SCHEMA}},
            system=_sys("write.md"),
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        log(f"polish failed: {e}")
        return {}
    METRICS.add("(write)", WRITE_MODEL, resp.usage)
    try:
        events = json.loads(_text_of(resp)).get("events", [])
        return {e["ref"]: e["summary"] for e in events if e.get("ref")}
    except json.JSONDecodeError:
        return {}


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
    """Window start for a daily run. Normally `last_run`, but never later than the
    start of the current day: a same-day re-run then re-covers the *whole* day and
    regenerates today's docs completely, instead of seeing an empty slice. Normal
    once-a-day cron is unaffected (last_run is yesterday, already < midnight)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last = state.get("last_run")
    if last:
        try:
            return min(dt.datetime.fromisoformat(last), midnight)
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
    """Log a cost summary and prepend a per-run record to metrics.json so the
    newest run is always first (top of the file)."""
    rec = {"timestamp": run_start.isoformat(), "mode": mode, **METRICS.record()}
    history: list = []
    if METRICS_FILE.exists():
        try:
            history = json.loads(METRICS_FILE.read_text())
        except json.JSONDecodeError:
            history = []
    history.insert(0, rec)
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


def build_search_index(shown: list[tuple], date: str) -> None:
    """Rebuild the index for `date`: ONE record per shown event, carrying the
    full `topics` list and a canonical `url` into the topic doc where it's shown.
    `shown` is a list of (event, display_topic) pairs."""
    index = [r for r in load_search_index() if r.get("date") != date]
    for ev, display_topic in shown:
        index.append(
            {
                "date": date,
                "event_id": slugify(ev["title"]),
                "title": ev["title"],
                "summary": (ev.get("one_liner") or "").strip(),
                "theme": ev.get("theme", ""),
                "importance": ev.get("importance", 0),
                "topics": ev.get("topics", []),
                "keywords": ev.get("keywords", []),
                "url": f"news/{display_topic}/{date}/#{slugify(ev['title'])}",
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
            srcs = ", ".join(
                f"[{s['label']}]({s['url']}) {_source_badge(s.get('origin', 'rss'))}"
                for s in e.get("sources", [])[:MAX_SOURCES_PER_EVENT]
            )
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


_STORY_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with", "at",
    "by", "from", "as", "is", "are", "be", "its", "it", "new", "will", "has",
    "after", "over", "amid", "into", "launches", "launch", "released", "release",
    "releases", "announces", "announced", "announcement", "unveils", "reveals",
    "reports", "report", "says", "plans", "update", "updates", "news",
}


def _story_tokens(rec: dict) -> set[str]:
    text = (rec.get("title", "") + " " + " ".join(rec.get("keywords", []))).lower()
    return {t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1 and t not in _STORY_STOP}


def _dedupe_cross_topic(ranked: list[dict], min_shared: int = 3) -> list[dict]:
    """Collapse the same real-world story surfaced under multiple topics.

    Two records are the same story if their significant title+keyword tokens
    overlap by >= min_shared. Input must be importance-sorted; the first
    (highest-importance) survivor is kept.
    """
    kept, kept_tokens = [], []
    for r in ranked:
        tks = _story_tokens(r)
        if any(len(tks & kt) >= min_shared for kt in kept_tokens):
            continue
        kept.append(r)
        kept_tokens.append(tks)
    return kept


def _top_stories_section(index: list[dict]) -> list[str]:
    """The biggest events across ALL topics for the most recent day (deduped)."""
    if not index:
        return []
    latest = max(r["date"] for r in index)
    ranked = sorted(
        (r for r in index if r["date"] == latest),
        key=lambda r: (r.get("importance", 0)),
        reverse=True,
    )
    todays = _dedupe_cross_topic(ranked)[:TOP_STORIES_N]
    if not todays:
        return []
    lines = [f"## Top stories — {latest}", ""]
    for r in todays:
        desc = (r.get("summary") or "").strip()
        desc = f" — {desc}" if desc else ""
        topics = ", ".join(r.get("topics", [])) or r.get("topic", "")
        lines.append(
            f"- {meter(r.get('importance', 0))} [{r['title']}]({r['url']}){desc} "
            f"· _{topics}_"
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
        "⭐ Like this? [Star it on GitHub]"
        "(https://github.com/srschreiber/rss-news-generator).",
        "",
    ]
    index = load_search_index()
    lines += _top_stories_section(index)

    # event count per (topic, date) from the index, to annotate daily links
    counts: dict[tuple, int] = {}
    for r in index:
        for t in r.get("topics", []):
            counts[(t, r["date"])] = counts.get((t, r["date"]), 0) + 1

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
            if kind == "daily":
                def _daily_link(s: str) -> str:
                    n = counts.get((topic, s), 0)
                    unit = "event" if n == 1 else "events"
                    return f"[{s} ({n} {unit})]({rel}/{s}.md)"

                links = " · ".join(_daily_link(s) for s in stems)
            else:
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
def _write_topic_doc(topic: str, shown: list[dict], date: str) -> None:
    """Write one topic's daily doc from the events shown under it (quiet if none).
    Docs and the search index are both rebuilt from this run's events, so they
    stay consistent by construction."""
    doc_path = KIND_DIR["daily"] / topic / f"{date}.md"
    title = f"Tech News — {topic} — {date}"
    if shown:
        write_doc(doc_path, front_matter(daily_tags(shown, date, topic)),
                  title, render_briefing(shown, topic))
    else:
        write_doc(doc_path, front_matter([date[:4], date[:7], topic]),
                  title, quiet_day_body(topic))


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
    items = filter_and_cap(raw, cutoff, seen, today=date)
    all_topics = sorted({s["topic"] for s in sources})

    # Stage 1: ONE global clustering pass across all feeds/topics, then derive
    # each event's topics + merged sources from its source items.
    events = assign_topics(attach_sources(stage1_cluster(items), items), items) if items else []
    spanning = sum(1 for e in events if len(e.get("topics", [])) > 1)
    log(f"clustered {len(items)} items -> {len(events)} global events ({spanning} cross-topic)")

    if dry_run:
        preview = [
            {"title": e["title"], "importance": e.get("importance"),
             "topics": e.get("topics"), "primary": e.get("primary_topic")}
            for e in sorted(events, key=lambda e: e.get("importance", 0), reverse=True)[:30]
        ]
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    # No-op re-run guard: if nothing new survived dedup AND today's docs already
    # exist, this is a same-day re-run — preserve the already-published content
    # rather than clobbering it with quiet-day placeholders and wiping the index.
    # (A genuinely quiet *first* run of the day has no docs yet, so it falls
    # through and writes quiet-day docs as before.)
    if not events and any(
        (KIND_DIR["daily"] / t / f"{date}.md").exists() for t in all_topics
    ):
        log("no new items; preserving today's existing docs (no-op re-run)")
        save_state(state, items, run_start)
        record_metrics("daily", run_start)
        log("daily run complete")
        return

    # Research each qualifying story ONCE (union of research-enabled topics'
    # top-K), then reuse the enrichment everywhere the story appears.
    research_topics = [t for t in all_topics if (not no_research) and research_enabled(t, cfg)]
    selected = select_research(events, research_topics)
    log(f"research on for {research_topics or 'none'}; researching {len(selected)} unique stories")
    events = merge_enrichment(events, research_events(selected, date))

    # Per-topic docs: each topic shows the top events whose topics include it.
    per_topic: dict[str, list[dict]] = {}
    for topic in all_topics:
        per_topic[topic] = sorted(
            (e for e in events if topic in e.get("topics", [])),
            key=lambda e: e.get("importance", 0), reverse=True,
        )[:EVENTS_PER_TOPIC]
        _write_topic_doc(topic, per_topic[topic], date)

    # One search-index record per shown event; url -> its primary topic when the
    # event is shown there, else the first topic where it appears.
    display_topic: dict[str, str] = {}
    for topic in all_topics:
        for e in per_topic[topic]:
            display_topic.setdefault(e["title"], topic)
    for topic in all_topics:
        for e in per_topic[topic]:
            if e.get("primary_topic") == topic:
                display_topic[e["title"]] = topic
    shown, seen_titles = [], set()
    for topic in all_topics:
        for e in per_topic[topic]:
            if e["title"] not in seen_titles:
                seen_titles.add(e["title"])
                shown.append((e, display_topic[e["title"]]))
    build_search_index(shown, date)

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
