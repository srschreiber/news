#!/usr/bin/env python3
"""Generate daily tech-news briefings, partitioned by topic.

Pipeline: fetch RSS sources -> cluster+score (Haiku) -> research+write (Sonnet).

Sources are grouped by `topic` (sources.yaml). Each topic produces its own
daily file: docs/news/<topic>/<date>.md. Untagged sources fall into "general".

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
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
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

KIND_DIR = {"daily": DOCS / "news"}  # actual docs live under <base>/<topic>/
DEFAULT_TOPIC = "general"
REPO_URL = "https://github.com/srschreiber/news"
# Merriam-Webster's Word of the Day — reputable, curated for usable (not archaic)
# words, and each entry ships a definition + example.
WOTD_FEED_URL = "https://www.merriam-webster.com/wotd/feed/rss2"
WORDS_FILE = DOCS / "words.json"   # accumulated WOTD history, feeds the quiz page
WORDS_HISTORY_MAX = 90             # cap the stored word history
# Wikipedia "On this day" (free, no key, verifiable) — Wikipedia's curated
# `selected` events for the calendar date.
ONTHISDAY_URL = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{mm}/{dd}"
ONTHISDAY_UA = "sam-news/1.0 (https://github.com/srschreiber/news)"
# thefactsite.com has no API — fetch the homepage and let Haiku extract the
# 'Fact of the Day' widget verbatim from a tiny pre-sliced snippet (~$0.0005).
FACTSITE_URL = "https://www.thefactsite.com/"
FACTSITE_FOTD = "https://www.thefactsite.com/fact-of-the-day/"
FUNFACTS_FILE = DOCS / "funfacts.json"   # accumulated fun-fact history for the Facts page
FUNFACTS_HISTORY_MAX = 120

CLUSTER_MODEL = "claude-haiku-4-5"      # Stage 1 — cheap, handles bulk input
READ_MODEL = "claude-haiku-4-5"         # Stage 2a — reads/fetches pages cheaply
WRITE_MODEL = "claude-sonnet-5"         # Stage 2b — polishes final summaries; more careful
                                        # with nuance/fact-fidelity than Haiku, worth the
                                        # cost since it only runs once per batched run
VOYAGE_MODEL = "voyage-4-large"          # story-update matching embeddings. Best-quality
                                        # tier: at our volume (title+summary per event, a
                                        # few dozen/day) even this tier costs a fraction of
                                        # a cent/day — cost isn't a reason to trade down
                                        # accuracy here, where a false merge is the real risk.
CROSS_DAY_LOOKBACK_DAYS = 5              # how far back a story can still be "updated" —
                                        # beyond this, a new development reads as new news
MERGE_COS_THRESHOLD = 0.86               # embedding similarity floor before even asking
                                        # Haiku to validate a same-story merge candidate
CLUSTER_COS_THRESHOLD = 0.90            # stricter than MERGE_COS_THRESHOLD: no Haiku
                                        # backstop here, so precision matters more than
                                        # recall — under-clustering just costs a few more
                                        # tokens, over-clustering silently mixes two stories

LOOKBACK_HOURS = 24                     # first-run fallback window
MAX_ITEMS_PER_FEED = 60                 # cap noisy feeds before Stage 1 — high headroom is cheap
                                        # now that embedding pregrouping keeps Haiku's clustering
                                        # input small regardless of raw item count
EVENTS_PER_TOPIC = 10                   # events shown in each topic's doc
RESEARCH_BUDGET_PER_TOPIC = 4           # events researched per SUBFEED (topic), not per feed —
                                        # every subfeed gets guaranteed research depth instead of
                                        # a few feed-wide "winners" starving the rest.
MIN_RESEARCH_IMPORTANCE = 6             # only research events at least this important (1-10)
TOP_STORIES_N = 12                      # biggest events across all topics on the home page
MAX_TOPIC_CONCURRENCY = 4               # topics researched in parallel (cap for rate limits)
WEB_FETCHES_PER_EVENT = 3               # pages fetched per event (1 RSS source + Serper fills)
GLOBAL_SEARCH_SAFETY = 80              # max Serper searches per run (1 per event) — 19 topics x
                                        # RESEARCH_BUDGET_PER_TOPIC already theoretical-maxes near
                                        # this; the old 40 was the actual binding constraint
MAX_RESEARCHED_EVENTS = GLOBAL_SEARCH_SAFETY
WEB_FETCH_MAX_CONTENT_TOKENS = 1000     # per-page cap (~4000 chars). News inverted pyramid
                                        # means key facts are always first.
MAX_SOURCES_PER_EVENT = 6               # distinct source links shown per event
STAGE1_MAX_TOKENS = 16000               # one global clustering pass over all feeds
READ_MAX_TOKENS = 2000                  # Haiku read: just a factual extract
STAGE2_MAX_TOKENS = 5000                # Sonnet polish: short dense summaries (batched)
STAGE2_EFFORT = "low"                   # scoped writing task; low trims thinking cost
SEEN_LINKS_RETENTION_DAYS = 7
SUMMARY_MAX_CHARS = 300

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
)
USFS_TREESEARCH_SEARCH = "https://www.fs.usda.gov/treesearch/pubs/search"
USFS_TREESEARCH_BASE = "https://www.fs.usda.gov"

METRICS_FILE = ROOT / "metrics.json"
EMBEDDING_CACHE_FILE = ROOT / "embedding_cache.json"  # internal only — never
                                        # served as part of the public site
# Estimated USD per 1M tokens (standard rates; cache write = 1.25x input,
# cache read = 0.1x input). These are approximations for cost tracking.
PRICES = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
}
WEB_SEARCH_COST_PER_1K = 1.0   # Serper.dev: $1 per 1,000 searches

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
                    "importance": {"type": "number", "minimum": 1, "maximum": 10},
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

# Stage 1, embedding-pregrouped variant: Haiku only titles/scores/keywords
# each already-formed group — source_item_ids is reconstructed in Python
# from the group membership computed by _embedding_cluster_items, not asked
# of the model.
GROUPED_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "title": {"type": "string"},
                    "one_liner": {"type": "string"},
                    "importance": {"type": "number", "minimum": 1, "maximum": 10},
                    "theme": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "discard_from_group": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "also_reported headlines that don't actually "
                                       "belong to this event.",
                    },
                },
                "required": [
                    "group_id", "title", "one_liner", "importance", "theme", "keywords",
                    "discard_from_group",
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
                "properties": {
                    "ref": {"type": "string"},
                    "summary": {"type": "string", "maxLength": 300},
                    "takeaways": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ref", "summary", "takeaways"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


# Fallback search: when a topic clusters zero RSS events for the day, one
# Serper search + Haiku call checks whether there's genuine news anyway.
FALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string", "maxLength": 300},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "integer"},
        "keywords": {"type": "array", "items": {"type": "string"}},
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
    "required": ["found"],
    "additionalProperties": False,
}


# One-off historical backfill: a date-scoped Serper search may turn up several
# distinct stories for that day, unlike the single-story FALLBACK_SCHEMA.
BACKFILL_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "takeaways": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "number", "minimum": 1, "maximum": 10},
                    "keywords": {"type": "array", "items": {"type": "string"}},
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
                "required": ["title", "summary", "importance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events"],
    "additionalProperties": False,
}


FUNFACT_SCHEMA = {
    "type": "object",
    "properties": {"fact": {"type": "string"}},
    "required": ["fact"],
    "additionalProperties": False,
}


# Cheap same-story validation before merging two events with a high embedding
# similarity — guards against embeddings false-positiving on generic phrasing
# (e.g. two unrelated earthquakes that both mention "death toll rising").
SAME_STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["duplicate", "update", "follow_on", "related", "new"],
        },
    },
    "required": ["relation"],
    "additionalProperties": False,
}
# Only these relations should merge into the existing story; follow_on (a
# caused-by-but-distinct event, e.g. an aftershock) gets its own event, and
# related/new obviously aren't the same story at all.
_MERGE_RELATIONS = {"duplicate", "update"}


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", file=sys.stderr, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no network, no API)
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """Approximate MkDocs' default heading slug (for deep-link anchors).

    MkDocs applies NFKD normalization before stripping non-word chars, which
    converts accented letters to their ASCII base (ñ→n, é→e, etc.)."""
    text = re.sub(r"<[^>]+>", "", text)
    text = unicodedata.normalize("NFKD", text)
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
    """A source is a direct feed `url`, a Google News `query`, or a custom `scraper`."""
    if source.get("url"):
        return source["url"]
    if source.get("scraper"):
        return ""  # fetched by custom scraper, not feedparser
    if source.get("query"):
        q = urllib.parse.quote_plus(source["query"])
        return GOOGLE_NEWS_RSS.format(q=q)
    raise ValueError(f"source {source.get('name')!r} needs a 'url', 'query', or 'scraper'")


def load_sources(path: Path = SOURCES_FILE) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    sources = data.get("sources", [])
    for s in sources:
        s["_url"] = resolve_source_url(s)
        s["topic"] = (s.get("topic") or DEFAULT_TOPIC).strip()
    return sources


def _fetch_usfs_treesearch(source: dict) -> list[dict]:
    """Scrape USFS Treesearch search results. Returns feedparser-style entry dicts.

    USFS Treesearch has no native RSS; this hits their search endpoint and parses
    the HTML response. Pub links are /treesearch/pubs/<id>; titles, dates, and
    abstracts are extracted via regex. Best-effort — structure may change."""
    query = source.get("query", "wildfire fire ecology fuel treatment")
    params = urllib.parse.urlencode({
        "searchtype": "all",
        "terms": query,
        "sort": "dateDescend",
        "perpage": "25",
    })
    url = f"{USFS_TREESEARCH_SEARCH}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": ONTHISDAY_UA})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")

    entries: list[dict] = []
    seen_links: set[str] = set()
    for m in re.finditer(
        r'href="(/treesearch/pubs/\d+)"[^>]*>\s*(.*?)\s*</a',
        raw, re.DOTALL,
    ):
        path_str, raw_title = m.group(1), m.group(2)
        title = html.unescape(re.sub(r"<[^>]+>", " ", raw_title))
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 15 or path_str in seen_links:
            continue
        seen_links.add(path_str)
        link = USFS_TREESEARCH_BASE + path_str

        # Date: look within ±600 chars of the match for YYYY-MM-DD or YYYY/MM/DD
        window = raw[max(0, m.start() - 200): m.end() + 600]
        pub_parsed: tuple | None = None
        dm = re.search(r"\b(20\d\d)[/-](\d{2})[/-](\d{2})\b", window)
        if dm:
            try:
                pub_parsed = (int(dm.group(1)), int(dm.group(2)),
                              int(dm.group(3)), 0, 0, 0)
            except ValueError:
                pass

        # Abstract snippet: text after a class containing "abstract"
        summary = ""
        am = re.search(
            r'class="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|p)',
            window, re.DOTALL | re.I,
        )
        if am:
            summary = html.unescape(re.sub(r"<[^>]+>", " ", am.group(1)))
            summary = re.sub(r"\s+", " ", summary).strip()[:SUMMARY_MAX_CHARS]

        entries.append({
            "title": title,
            "link": link,
            "published_parsed": pub_parsed,
            "summary": summary,
        })
    return entries


SCRAPERS: dict[str, object] = {
    "usfs_treesearch": _fetch_usfs_treesearch,
}


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


DEFAULT_FEED = "other"
DEFAULT_RESEARCH_BUDGET = 3


def load_feeds(all_topics: list[str], path: Path = SOURCES_FILE) -> tuple[dict, dict]:
    """Feeds group topics into broad sections. Returns:
      feeds       — ordered {feed_key: {title, topics, research_budget}}
      topic_feed  — {topic: feed_key} for EVERY topic in `all_topics`

    Topics not listed under any feed are collected into a synthesized "other"
    feed so nothing is dropped. Config shape in sources.yaml:

        feeds:
          technology: {title: Technology, research_budget: 6, topics: [...]}
    """
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("feeds", {}) or {}
    feeds: dict = {}
    topic_feed: dict = {}
    for key, spec in raw.items():
        spec = spec or {}
        topics = [str(t).strip() for t in (spec.get("topics") or []) if str(t).strip()]
        also = [str(t).strip() for t in (spec.get("also_includes") or []) if str(t).strip()]
        feeds[key] = {
            "title": (spec.get("title") or key.replace("-", " ").title()),
            "kind": (spec.get("kind") or "news").strip(),
            "topics": topics,
            "also_includes": also,
            "research_budget": int(spec.get("research_budget", DEFAULT_RESEARCH_BUDGET)),
            "scoring_context": (spec.get("scoring_context") or "").strip(),
        }
        for t in topics:
            topic_feed.setdefault(t, key)
    # Any topic with sources but no feed -> synthesized "other" feed.
    orphans = [t for t in all_topics if t not in topic_feed]
    if orphans:
        feeds.setdefault(DEFAULT_FEED, {"title": "Other", "topics": [],
                                        "research_budget": DEFAULT_RESEARCH_BUDGET})
        for t in orphans:
            feeds[DEFAULT_FEED]["topics"].append(t)
            topic_feed[t] = DEFAULT_FEED
    return feeds, topic_feed


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
    source_last_ts: dict[str, str] | None = None,
) -> list[dict]:
    """Keep items published since `cutoff` and not already seen; cap per source.

    Items with no publish date are kept (many feeds omit it) unless already seen.
    `seen` means seen on a *prior* day: when `today` is given, items first seen
    today are still kept, so a same-day re-run can rebuild the full day. Cross-day
    dedup (items seen yesterday or earlier) always applies.

    `source_last_ts` maps source name -> ISO timestamp of the newest item already
    processed for that source. Items at-or-before that timestamp are skipped so
    hourly incremental runs only cluster genuinely new RSS items.
    """
    kept: list[dict] = []
    per_source: dict[str, int] = {}
    ordered = sorted(
        items,
        key=lambda it: it.get("_published_dt")
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    parsed_src_ts: dict[str, dt.datetime] = {}
    if source_last_ts:
        for src, ts_str in source_last_ts.items():
            try:
                parsed_src_ts[src] = dt.datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass
    for it in ordered:
        link = it["link"]
        if link and link in seen_links and (today is None or seen_links[link] < today):
            continue
        pub = it.get("_published_dt")
        if pub is not None and pub < cutoff:
            continue
        src_floor = parsed_src_ts.get(it.get("source", ""))
        if pub is not None and src_floor is not None and pub <= src_floor:
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


def meter(score: float) -> str:
    """Importance as a numeric badge (e.g. '7.5'). Styled by `.imp` in extra.css;
    mirrored in period-view.js and search.js so all views match."""
    raw = float(score or 0)
    n = max(1.0, min(10.0, raw))
    label = int(n) if n == int(n) else round(n, 1)
    tier = "high" if n >= 7 else ("mid" if n >= 4 else "low")
    return (f'<span class="imp imp-{tier}" title="Importance {label}/10" '
            f'aria-label="Importance {label} of 10">{label}</span>')


def _source_badge(origin: str) -> str:
    """GitHub-style pill tagging a source as from the RSS feed or from research."""
    label = "AI Web Searched" if origin == "research" else "RSS"
    return f'<span class="src-badge src-{origin}">{label}</span>'


def payload_items(items: list[dict]) -> list[dict]:
    """Trim items to just what clustering needs (id, outlet, topic, title,
    summary). Drops `link` and `published`: the Google News redirect URLs are
    long (~250 chars each — ~25% of the clustering payload) and clustering never
    uses them (sources are resolved separately by `id` in attach_sources)."""
    keep = ("id", "source", "topic", "title", "summary")
    return [{k: it[k] for k in keep if k in it} for it in items]


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
            e["takeaways"] = [t.strip() for t in hit.get("takeaways", []) if t.strip()]
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


def _also_includes_map(feeds: dict | None) -> dict[str, set[str]]:
    """topic -> set of extra feed keys that cross-list it via `also_includes`."""
    also_map: dict[str, set[str]] = {}
    for fkey, spec in (feeds or {}).items():
        for t in spec.get("also_includes", []):
            also_map.setdefault(t, set()).add(fkey)
    return also_map


def _feeds_for_topics(topics: list[str], topic_feed: dict, feeds: dict | None) -> list[str]:
    """Every feed a set of topics should appear under: each topic's primary
    feed, plus any feed that cross-lists one of them via `also_includes`."""
    also_map = _also_includes_map(feeds)
    primary_feeds = {topic_feed.get(t, DEFAULT_FEED) for t in topics}
    cross_listed = {fk for t in topics for fk in also_map.get(t, ())}
    return sorted(primary_feeds | cross_listed)


def assign_topics(events: list[dict], items: list[dict],
                  topic_feed: dict | None = None, feeds: dict | None = None) -> list[dict]:
    """Derive each event's topics from the source items it clustered.

    Sets `topics` (sorted unique) and `primary_topic` (the topic contributing the
    most source items; ties broken alphabetically). When `topic_feed` is given,
    also sets `feeds` (sorted unique feeds of its topics, plus any feed that
    cross-lists one of its topics via `also_includes`) and `primary_feed`
    (feed of the primary topic)."""
    topic_feed = topic_feed or {}
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
        e["feeds"] = _feeds_for_topics(e["topics"], topic_feed, feeds)
        e["primary_feed"] = topic_feed.get(e["primary_topic"], DEFAULT_FEED)
    return events


def select_research(
    events: list[dict], feeds: dict, cfg: dict, no_research: bool = False,
    max_events: int = MAX_RESEARCHED_EVENTS,
    remaining_budget: dict[str, int] | None = None,
    budget_per_topic: int = RESEARCH_BUDGET_PER_TOPIC,
) -> list[dict]:
    """Pick which events to research, per-SUBFEED (topic) budget.

    Every research-enabled topic gets its own guaranteed slice of the budget,
    rather than competing feed-wide against every other topic in the same
    feed for a shared pool (which starved low-traffic topics in a busy feed).
    A story chosen once (it may span topics) is never chosen again —
    researched once, reused everywhere. `max_events` is the run-wide safety net.

    `remaining_budget`, keyed by topic, overrides each topic's budget with
    today's remaining capacity (full budget minus events already researched
    in earlier runs today)."""
    if no_research:
        return []
    chosen, chosen_ids = [], set()
    all_topics = [t for spec in feeds.values() for t in spec.get("topics", [])]
    # Shuffle so equal-importance ties are broken randomly across topics,
    # not by sources.yaml order.
    random.shuffle(all_topics)
    for t in all_topics:
        if not research_enabled(t, cfg):
            continue
        budget = remaining_budget.get(t, 0) if remaining_budget is not None else budget_per_topic
        candidates = sorted(
            (e for e in events
             if t in e.get("topics", []) and e.get("importance", 0) >= MIN_RESEARCH_IMPORTANCE),
            key=lambda e: e.get("importance", 0), reverse=True,
        )
        picked = 0
        for e in candidates:
            if picked >= budget or len(chosen) >= max_events:
                break
            if id(e) not in chosen_ids:
                chosen_ids.add(id(e))
                chosen.append(e)
                picked += 1
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
    out = []
    for rd in reads:
        p = polished.get(rd["ref"]) or {}
        out.append({
            "ref": rd["ref"],
            "summary": p.get("summary") or rd["extract"],
            "takeaways": p.get("takeaways", []),
            "sources": rd["sources"],
        })
    return out


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

    def add_searches(self, topic: str, count: int) -> None:
        with self._lock:
            top = self.topics.setdefault(topic, {"calls": 0, "web_searches": 0, "by_model": {}})
            top["web_searches"] += count

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


def _sys(prompt_name: str, extra: str = "") -> list[dict]:
    text = (PROMPTS_DIR / prompt_name).read_text()
    ci = load_custom_instructions()
    if ci:
        text += "\n\n## Editorial instructions (must follow)\n" + ci + "\n"
    if extra:
        text += "\n\n" + extra
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _feed_scoring_context(feeds: dict) -> str:
    """Build a feed-scoring block to inject into the cluster prompt."""
    lines = [
        "## Feed scoring contexts",
        "",
        "Score importance 1–10 (decimals OK, e.g. 7.5) relative to the **primary feed** of each event's topics,",
        "not against a single global baseline. Use the reader context below:",
        "",
    ]
    for spec in feeds.values():
        ctx = spec.get("scoring_context", "").strip()
        if not ctx:
            continue
        topics_str = ", ".join(spec["topics"])
        lines.append(f"- **{spec['title']}** (topics: {topics_str}): {ctx}")
    return "\n".join(lines)


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _embedding_cluster_items(items: list[dict]) -> list[list[dict]] | None:
    """Greedy embedding-based grouping of raw items into same-event clusters,
    before Haiku ever sees them — collapses hundreds of raw items (many
    outlets covering the same story) down to the handful of distinct events
    Haiku actually needs to score, cutting clustering's input tokens sharply.
    Returns None (caller falls back to sending Haiku every raw item) if
    VOYAGE_API_KEY is unset or the embed call fails — an optimization, never
    a requirement for the pipeline to run.

    Single pass, each item joins the most-similar existing cluster above
    CLUSTER_COS_THRESHOLD or starts a new one; a joined cluster's centroid is
    the running average of its members, so it stays representative as it
    grows rather than anchored to whichever item arrived first."""
    if not items:
        return []
    texts = [f"{it.get('title', '')} {it.get('summary', '')}".strip() for it in items]
    vecs = _voyage_embed(texts)
    if vecs is None or len(vecs) != len(items):
        return None

    clusters: list[dict] = []
    for it, emb in zip(items, vecs):
        best, best_score = None, 0.0
        for c in clusters:
            score = _cosine(emb, c["centroid"])
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= CLUSTER_COS_THRESHOLD:
            best["items"].append(it)
            n = len(best["items"])
            best["centroid"] = [(cv * (n - 1) + ev) / n for cv, ev in zip(best["centroid"], emb)]
        else:
            clusters.append({"items": [it], "centroid": list(emb)})
    return [(c["items"], [round(x, 4) for x in c["centroid"]]) for c in clusters]


def _cluster_groups_payload(groups: list[list[dict]]) -> list[dict]:
    """Compact per-group payload for cluster_grouped.md: one representative
    (the richest summary) plus a couple of other outlets' headlines for
    context, rather than every member's full text."""
    payload = []
    for i, members in enumerate(groups):
        rep = max(members, key=lambda it: len(it.get("summary", "")))
        others = [m["title"] for m in members if m is not rep][:2]
        payload.append({
            "group_id": f"g{i}",
            "title": rep.get("title", ""),
            "summary": rep.get("summary", ""),
            "also_reported": others,
            "outlet_count": len({m.get("source", "") for m in members}),
            "topics": sorted({m.get("topic", "") for m in members if m.get("topic")}),
        })
    return payload


def _ungroup_events(events: list[dict], groups: list[list[dict]]) -> list[dict]:
    """Reconstruct source_item_ids from group membership (computed
    deterministically in Python, not asked of Haiku) and drop the internal
    group_id key used only to key the response. Honors discard_from_group —
    Haiku's chance to flag an also_reported headline that doesn't actually
    belong, since the embedding grouping has no other validation backstop.
    If discarding would empty the group, keeps the original membership
    instead — an empty, sourceless event is worse than an occasional
    mis-grouped citation."""
    members_by_gid = {f"g{i}": members for i, members in enumerate(groups)}
    out = []
    for e in events:
        e = dict(e)
        gid = e.pop("group_id", None)
        members = members_by_gid.get(gid, [])
        discard = set(e.pop("discard_from_group", []) or [])
        kept = [it for it in members if it.get("title") not in discard] or members
        e["source_item_ids"] = [it["id"] for it in kept]
        out.append(e)
    return out


def _call_stage1(user_payload: dict, prompt_name: str, schema: dict,
                 feeds: dict, label: str) -> list[dict]:
    """Shared Haiku call + retry-on-parse-failure for stage 1, whichever
    payload shape (raw items or embedding-pregrouped) is being sent."""
    client = _client()
    user = json.dumps(user_payload, ensure_ascii=False)
    for attempt in (1, 2):
        resp = client.messages.create(
            model=CLUSTER_MODEL,
            max_tokens=STAGE1_MAX_TOKENS,
            system=_sys(prompt_name, extra=_feed_scoring_context(feeds)),
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        METRICS.add(label, CLUSTER_MODEL, resp.usage)
        try:
            events = json.loads(_text_of(resp)).get("events", [])
            if not events:
                log(f"stage 1 ({prompt_name}) returned no events")
            return events
        except json.JSONDecodeError as e:
            log(f"stage 1 ({prompt_name}) JSON parse failed (attempt {attempt}): {e}")
    return []


def stage1_cluster(
    items: list[dict], feeds: dict,
    prompt_name: str = "cluster.md",
    schema: dict | None = None,
    label: str = "(clustering)",
) -> list[dict]:
    """Cluster+score+extract keywords via Haiku with structured output.

    ONE global pass over items from all feeds/topics — the same story surfaced
    under multiple topics is merged into a single event (topics + merged sources
    are derived from source_item_ids in code afterwards).

    For the default prompt/schema, this REQUIRES embedding pre-grouping (see
    _embedding_cluster_items) and raises if Voyage is unavailable, rather than
    falling back to the far more expensive send-every-raw-item approach. This
    is a deliberate hard failure, not an oversight: run_daily() only persists
    seen_links/source_last_ts (marking today's RSS items processed) after a
    clean run, so a crash here means those same items are simply retried next
    run once Voyage is fixed — no silent cost blowup, no silently dropped
    news. A caller that passes a custom prompt_name/schema (whose contract
    this optimization doesn't know how to satisfy) is unaffected — it never
    goes through the embedding path."""
    if not items:
        return []
    if schema is None and prompt_name == "cluster.md":
        groups_with_centroids = _embedding_cluster_items(items)
        if groups_with_centroids is None:
            raise RuntimeError(
                "Voyage embeddings unavailable (VOYAGE_API_KEY unset or the "
                "embed call failed) — refusing to fall back to raw-item "
                "clustering. Fix Voyage and rerun; today's items are untouched."
            )
        groups = [g for g, _ in groups_with_centroids]
        centroid_by_gid = {f"g{i}": c for i, (_, c) in enumerate(groups_with_centroids)}
        payload = {"groups": _cluster_groups_payload(groups)}
        raw_events = _call_stage1(payload, "cluster_grouped.md", GROUPED_EVENTS_SCHEMA, feeds, label)
        for ev in raw_events:
            gid = ev.get("group_id")
            if gid in centroid_by_gid:
                ev["_embedding"] = centroid_by_gid[gid]
        return _ungroup_events(raw_events, groups)
    return _call_stage1({"items": payload_items(items)}, prompt_name, schema or EVENTS_SCHEMA, feeds, label)


_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "mc_cid", "mc_eid",
})
_MAX_URL_LEN = 1024  # web_fetch rejects very long URLs


def _clean_url(url: str) -> str | None:
    """Strip tracking params and enforce a length cap. Returns None if the URL
    is unusable (too long even after cleaning, or not http/https)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    qs = {k: v for k, v in urllib.parse.parse_qsl(parsed.query)
          if k.lower() not in _TRACKING_PARAMS}
    clean = parsed._replace(query=urllib.parse.urlencode(qs)).geturl()
    return clean if len(clean) <= _MAX_URL_LEN else None


def _fetch_page_text(url: str) -> str:
    """Fetch a URL and return stripped plain text, capped at WEB_FETCH_MAX_CONTENT_TOKENS tokens."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(400_000).decode("utf-8", "ignore")
    except Exception:
        return ""
    raw = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", raw,
                 flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:WEB_FETCH_MAX_CONTENT_TOKENS * 4]  # ~4 chars/token


def _serper_search(query: str, n: int = 5, tbs: str | None = None) -> list[dict]:
    """Search Google News via Serper.dev /news endpoint (journalistic sources only).
    `tbs` is Google's raw time-based-search operator, e.g. a custom date range:
    "cdr:1,cd_min:8/1/2026,cd_max:8/1/2026" — used for date-scoped backfills.
    Returns list of {title, url, snippet}."""
    key = os.environ.get("SERPER_DEV_API_KEY", "")
    if not key:
        raise RuntimeError("SERPER_DEV_API_KEY not set")
    body: dict = {"q": query, "num": n}
    if tbs:
        body["tbs"] = tbs
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/news",
        data=data,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return [
        {"title": r["title"], "url": r["link"], "snippet": r.get("snippet", "")}
        for r in result.get("news", [])
    ]


def stage2a_read(event: dict, date: str) -> dict | None:
    """Stage 2a — deterministic fetch: Serper search + urllib page fetches, then one
    Haiku call. No tool loop — each token is paid once. Returns {extract, sources}."""
    # 1. Serper search (Python, no LLM cost)
    try:
        hits = _serper_search(event["title"], n=5)
        METRICS.add_searches("(read)", 1)
    except Exception as e:
        log(f"serper failed ({event['title'][:40]}): {e}")
        hits = []

    # 2. Collect URLs: prefer RSS source first, fill slots with Serper results
    rss_urls = [u for s in event.get("sources", [])
                if (u := _clean_url(s.get("url", ""))) is not None]
    serper_urls = [u for h in hits if (u := _clean_url(h["url"])) is not None]
    seen: set[str] = set()
    urls_to_fetch: list[str] = []
    for u in (rss_urls[:1] + serper_urls):
        if u not in seen and len(urls_to_fetch) < WEB_FETCHES_PER_EVENT:
            urls_to_fetch.append(u)
            seen.add(u)

    # 3. Fetch pages (Python, no LLM cost)
    pages = []
    for url in urls_to_fetch:
        text = _fetch_page_text(url)
        if text:
            pages.append({"url": url, "content": text})

    # 4. One-shot extraction — single Haiku call, no tools
    payload = {
        "date": date,
        "event": {
            "title": event["title"],
            "one_liner": event.get("one_liner", ""),
            "keywords": event.get("keywords", []),
        },
        "search_results": [
            {"title": h["title"], "url": h["url"], "snippet": h.get("snippet", "")}
            for h in hits
        ],
        "pages": pages,
    }
    client = _client()
    try:
        resp = client.messages.create(
            model=READ_MODEL,
            max_tokens=READ_MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": READ_SCHEMA}},
            system=_sys("read.md"),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
    except Exception as e:
        log(f"read failed ({event['title'][:40]}): {e}")
        return None
    METRICS.add("(read)", READ_MODEL, resp.usage)
    try:
        obj = json.loads(_text_of(resp))
        return obj if (obj.get("extract") or "").strip() else None
    except json.JSONDecodeError:
        return None


def _topic_fallback_query(topic: str, sources: list[dict]) -> str:
    """A search query for a quiet topic: reuse its configured Google News
    query if it has one (closest match to what the topic actually covers),
    else fall back to the topic's display name."""
    for s in sources:
        if s.get("topic") == topic and s.get("query"):
            return s["query"]
    return f"{topic_display(topic)} news"


def fallback_search_event(topic: str, feeds: dict, topic_feed: dict,
                          sources: list[dict]) -> dict | None:
    """When a topic clusters zero RSS events for the day, try ONE Serper
    search to see if there's real news anyway (e.g. slow-publishing academic
    RSS feeds miss something a live news search would catch). Returns a
    synthesized, already-researched event, or None if nothing genuinely
    newsworthy turns up — callers should still fall back to "Quiet day"."""
    query = _topic_fallback_query(topic, sources)
    try:
        hits = _serper_search(query, n=5)
        METRICS.add_searches("(fallback)", 1)
    except Exception as e:
        log(f"fallback search failed ({topic}): {e}")
        return None
    if not hits:
        return None

    urls = [u for h in hits if (u := _clean_url(h["url"])) is not None][:WEB_FETCHES_PER_EVENT]
    pages = [{"url": u, "content": t} for u in urls if (t := _fetch_page_text(u))]

    fkey = topic_feed.get(topic, DEFAULT_FEED)
    scoring_context = feeds.get(fkey, {}).get("scoring_context", "")
    payload = {
        "topic": topic,
        "scoring_context": scoring_context,
        "search_results": [{"title": h["title"], "url": h["url"], "snippet": h.get("snippet", "")}
                           for h in hits],
        "pages": pages,
    }
    client = _client()
    try:
        resp = client.messages.create(
            model=READ_MODEL,
            max_tokens=READ_MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": FALLBACK_SCHEMA}},
            system=_sys("fallback.md"),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
    except Exception as e:
        log(f"fallback extraction failed ({topic}): {e}")
        return None
    METRICS.add("(fallback)", READ_MODEL, resp.usage)
    try:
        obj = json.loads(_text_of(resp))
    except json.JSONDecodeError:
        return None
    if not obj.get("found") or not (obj.get("title") or "").strip():
        return None

    sources_out = [{"label": s["label"], "url": s["url"], "origin": "research"}
                   for s in obj.get("sources", []) if s.get("url")][:MAX_SOURCES_PER_EVENT]
    return {
        "title": obj["title"].strip(),
        "one_liner": (obj.get("summary") or "").strip(),
        "takeaways": [t.strip() for t in obj.get("takeaways", []) if t.strip()],
        "importance": round(max(1.0, min(10.0, float(obj.get("importance") or 5))), 1),
        "keywords": [k.strip() for k in obj.get("keywords", []) if k.strip()],
        "theme": topic_display(topic),
        "topics": [topic],
        "primary_topic": topic,
        "feeds": _feeds_for_topics([topic], topic_feed, feeds),
        "primary_feed": fkey,
        "sources": sources_out,
        "researched": True,
        "received_at": now_utc().isoformat(),
    }


def backfill_topic_day(topic: str, date_str: str, feeds: dict, topic_feed: dict,
                       sources: list[dict], n_results: int = 8, max_pages: int = 4,
                       max_events: int = 3) -> list[dict]:
    """One-off historical backfill: date-scoped Serper search + Haiku extraction
    for a topic that was quiet on `date_str` (YYYY-MM-DD). Returns 0+ synthesized,
    already-researched events shaped like fallback_search_event()'s output — a
    docs/news/<topic>/<date_str>.md write and a search-index update still need
    to happen at the call site (this is a research-only helper, no side effects)."""
    query = _topic_fallback_query(topic, sources)
    d = dt.date.fromisoformat(date_str)
    mmddyyyy = f"{d.month}/{d.day}/{d.year}"
    tbs = f"cdr:1,cd_min:{mmddyyyy},cd_max:{mmddyyyy}"
    try:
        hits = _serper_search(query, n=n_results, tbs=tbs)
        METRICS.add_searches("(backfill)", 1)
    except Exception as e:
        log(f"backfill search failed ({topic} {date_str}): {e}")
        return []
    if not hits:
        return []

    urls = [u for h in hits if (u := _clean_url(h["url"])) is not None][:max_pages]
    pages = [{"url": u, "content": t} for u in urls if (t := _fetch_page_text(u))]

    fkey = topic_feed.get(topic, DEFAULT_FEED)
    scoring_context = feeds.get(fkey, {}).get("scoring_context", "")
    payload = {
        "topic": topic,
        "date": date_str,
        "scoring_context": scoring_context,
        "search_results": [{"title": h["title"], "url": h["url"], "snippet": h.get("snippet", "")}
                           for h in hits],
        "pages": pages,
    }
    client = _client()
    try:
        resp = client.messages.create(
            model=READ_MODEL,
            max_tokens=READ_MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": BACKFILL_SCHEMA}},
            system=_sys("backfill.md"),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
    except Exception as e:
        log(f"backfill extraction failed ({topic} {date_str}): {e}")
        return []
    METRICS.add("(backfill)", READ_MODEL, resp.usage)
    try:
        obj = json.loads(_text_of(resp))
    except json.JSONDecodeError:
        return []

    out = []
    for ev in obj.get("events", [])[:max_events]:
        if not (ev.get("title") or "").strip():
            continue
        sources_out = [{"label": s["label"], "url": s["url"], "origin": "research"}
                       for s in ev.get("sources", []) if s.get("url")][:MAX_SOURCES_PER_EVENT]
        out.append({
            "title": ev["title"].strip(),
            "one_liner": (ev.get("summary") or "").strip(),
            "takeaways": [t.strip() for t in ev.get("takeaways", []) if t.strip()],
            "importance": round(max(1.0, min(10.0, float(ev.get("importance") or 5))), 1),
            "keywords": [k.strip() for k in ev.get("keywords", []) if k.strip()],
            "theme": topic_display(topic),
            "topics": [topic],
            "primary_topic": topic,
            "feeds": _feeds_for_topics([topic], topic_feed, feeds),
            "primary_feed": fkey,
            "sources": sources_out,
            "researched": True,
        })
    return out


def backfill_write_index(date_str: str, topic: str, events: list[dict]) -> None:
    """Replace `topic`'s search-index records for `date_str` with `events`,
    leaving every other topic/date untouched. build_search_index() can't be
    reused here — it replaces ALL records for a date, which would wipe out
    unrelated topics' real same-day events."""
    index = [r for r in load_search_index()
            if not (r["date"] == date_str and topic in r.get("topics", []))]
    base_dir = KIND_DIR["daily"].name
    for ev in events:
        index.append({
            "date": date_str,
            "event_id": slugify(ev["title"]),
            "title": ev["title"],
            "summary": ev.get("one_liner", ""),
            "theme": ev.get("theme", ""),
            "importance": ev.get("importance", 0),
            "topics": ev.get("topics", []),
            "feeds": ev.get("feeds", []),
            "primary_feed": ev.get("primary_feed", DEFAULT_FEED),
            "keywords": ev.get("keywords", []),
            "url": f"{base_dir}/{topic}/{date_str}/#{slugify(ev['title'])}",
            "sources": ev.get("sources", []),
            "takeaways": ev.get("takeaways", []),
            "researched": bool(ev.get("researched", False)),
        })
    index.sort(key=lambda r: (r["date"], -r.get("importance", 0)), reverse=True)
    SEARCH_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")


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
        return {
            e["ref"]: {"summary": e.get("summary", ""), "takeaways": e.get("takeaways", [])}
            for e in events if e.get("ref")
        }
    except json.JSONDecodeError:
        return {}


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


def save_state(
    state: dict,
    items: list[dict],
    run_start: dt.datetime,
    today_events_data: dict | None = None,
    feed_last_refresh: dict[str, str] | None = None,
    fallback_tried_data: dict | None = None,
) -> None:
    seen = prune_seen(dict(state.get("seen_links", {})), run_start)
    today = run_start.date().isoformat()
    for it in items:
        if it.get("link"):
            seen[it["link"]] = today

    # Advance per-source timestamp pointers to the newest item each source served
    # this run, so subsequent hourly runs skip already-processed items.
    source_last_ts: dict[str, str] = dict(state.get("source_last_ts", {}))
    for it in items:
        src = it.get("source") or ""
        pub: dt.datetime | None = it.get("_published_dt")
        if src and pub:
            pub_str = pub.isoformat()
            if pub_str > source_last_ts.get(src, ""):
                source_last_ts[src] = pub_str

    # Merge incoming feed refresh timestamps with what's already stored.
    merged_feed_refresh: dict[str, str] = dict(state.get("feed_last_refresh") or {})
    if feed_last_refresh:
        merged_feed_refresh.update(feed_last_refresh)

    payload: dict = {
        "last_run": run_start.isoformat(),
        "seen_links": seen,
        "source_last_ts": source_last_ts,
        "feed_last_refresh": merged_feed_refresh,
    }
    if today_events_data is not None:
        payload["today_events"] = today_events_data
    elif "today_events" in state:
        payload["today_events"] = state["today_events"]
    if fallback_tried_data is not None:
        payload["fallback_tried"] = fallback_tried_data
    elif "fallback_tried" in state:
        payload["fallback_tried"] = state["fallback_tried"]
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n")


def _load_today_events(state: dict, date: str) -> list[dict]:
    """Return stored events from a previous run today, or [] if no match."""
    stored = state.get("today_events")
    if not isinstance(stored, dict) or stored.get("date") != date:
        return []
    events = stored.get("events", [])
    if not isinstance(events, list):
        return []
    # Strip ref — it's a per-run temporary key. Stale refs from a prior run
    # collide with new ref assignments in research_events() and cause
    # merge_enrichment() to overwrite the wrong events' summaries.
    return [{k: v for k, v in e.items() if k != "ref"} for e in events]


def _load_fallback_tried(state: dict, date: str) -> set[str]:
    """Topics already attempted for the once-per-day Serper fallback search
    today (regardless of whether they found anything) — avoids re-searching
    the same quiet topic on every hourly run."""
    stored = state.get("fallback_tried")
    if not isinstance(stored, dict) or stored.get("date") != date:
        return set()
    topics = stored.get("topics", [])
    return set(topics) if isinstance(topics, list) else set()


_VOYAGE_BATCH = 128  # voyage-4-large hard limit per request


def _voyage_embed(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts via Voyage AI (cheap, no SDK dependency — raw
    REST). Automatically chunks into batches of _VOYAGE_BATCH to stay within
    the API's per-request limit. Returns None if VOYAGE_API_KEY isn't set or
    any chunk fails — callers treat None as "no embeddings available"."""
    if not texts:
        return []
    key = os.environ.get("VOYAGE_API_KEY", "")
    if not key:
        return None
    out: list[list[float]] = []
    for i in range(0, len(texts), _VOYAGE_BATCH):
        chunk = texts[i: i + _VOYAGE_BATCH]
        for attempt in range(6):
            try:
                data = json.dumps({"input": chunk, "model": VOYAGE_MODEL}).encode()
                req = urllib.request.Request(
                    "https://api.voyageai.com/v1/embeddings",
                    data=data,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                # Rounded — cosine similarity doesn't need full float precision, and
                # this halves what a 512-dim vector costs in state.json.
                vecs = [[round(x, 4) for x in d["embedding"]] for d in result.get("data", [])]
                if len(vecs) != len(chunk):
                    log(f"voyage embed: expected {len(chunk)} vectors, got {len(vecs)}")
                    return None
                out.extend(vecs)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    import time, random
                    # Voyage docs recommend exponential backoff with jitter; no Retry-After header.
                    wait = 2 ** (attempt + 2) + random.uniform(0, 1)
                    log(f"voyage embed 429 (chunk {i//_VOYAGE_BATCH}), retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                log(f"voyage embed failed (chunk {i//_VOYAGE_BATCH}): {e}")
                return None
            except Exception as e:
                log(f"voyage embed failed (chunk {i//_VOYAGE_BATCH}): {e}")
                return None
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    # A dimension mismatch (e.g. a cached embedding from a since-changed
    # VOYAGE_MODEL) would otherwise silently truncate via zip() into a
    # meaningless partial score — treat it as "no evidence of similarity"
    # instead of a wrong one.
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _embed_text(ev: dict) -> str:
    """Text representation of an event for embedding/matching. Title alone is
    often too generic to disambiguate similar-sounding stories (e.g. two
    different Iran-related security stories) — one_liner carries the specific
    facts (who, what, how) that actually distinguish one event from another."""
    summary = (ev.get("one_liner") or "").strip()
    return f"{ev['title']} {summary}".strip()


def _classify_relation(text_a: str, text_b: str) -> str:
    """Cheap Haiku classification before committing to an embedding-suggested
    merge: duplicate/update (same event — merge), follow_on (caused by but
    distinct, e.g. an aftershock — link, don't merge), related (same subject,
    different event), or new (unrelated). Fails closed ("new") on error — a
    missed update/link costs far less than a bad merge."""
    client = _client()
    try:
        resp = client.messages.create(
            model=READ_MODEL,
            max_tokens=20,
            output_config={"format": {"type": "json_schema", "schema": SAME_STORY_SCHEMA}},
            system=_sys("same_story.md"),
            messages=[{"role": "user",
                      "content": json.dumps({"a": text_a, "b": text_b}, ensure_ascii=False)}],
        )
        METRICS.add("(merge-check)", READ_MODEL, resp.usage)
        return json.loads(_text_of(resp)).get("relation", "new")
    except Exception as e:
        log(f"relation check failed: {e}")
        return "new"


def _merge_sources(existing: dict, ev: dict) -> None:
    """Append ev's source URLs onto existing that aren't already there."""
    existing_urls = {s.get("url") for s in existing.get("sources", [])}
    for src in ev.get("sources", []):
        if src.get("url") not in existing_urls:
            existing.setdefault("sources", []).append(src)
            existing_urls.add(src.get("url"))


def _index_url_to_md(url: str) -> str:
    """search-index's directory-URL form ('news/tech/2026-08-10/#slug') into a
    daily-doc-relative markdown link ('../tech/2026-08-10.md#slug') — for
    linking from one docs/news/<topic>/<date>.md file to another. Real
    markdown-syntax link, so MkDocs's build-time resolver handles it (unlike
    the period-view widget's runtime-injected <a> tags)."""
    path, _, anchor = url.partition("#")
    topic, date_part = path.strip("/").split("/")[1:3]
    return f"../{topic}/{date_part}.md#{anchor}"


def load_embedding_cache(today: str, lookback_days: int = CROSS_DAY_LOOKBACK_DAYS) -> dict:
    """{event_id: {embedding, title, one_liner, topics, date, md_url}} for the
    last `lookback_days` days — pruned on load so the file never grows
    unbounded. Internal only: powers cross-day story-update matching, never
    served as part of the public site."""
    if not EMBEDDING_CACHE_FILE.exists():
        return {}
    try:
        cache = json.loads(EMBEDDING_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=lookback_days)).isoformat()
    return {eid: v for eid, v in cache.items() if v.get("date", "") >= cutoff}


def save_embedding_cache(cache: dict) -> None:
    EMBEDDING_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n")


def update_embedding_cache(all_events: list[dict], date: str) -> None:
    """Refresh the cross-day embedding cache from today's final published
    search-index records (which have the correct, already-resolved doc URLs),
    joined with today's event embeddings (embedding any stragglers that
    never went through candidate-matching this run). Keeps the last
    CROSS_DAY_LOOKBACK_DAYS days; older entries are pruned automatically."""
    need_embed = [e for e in all_events if "_embedding" not in e][:_VOYAGE_BATCH]
    if need_embed:
        vecs = _voyage_embed([_embed_text(e) for e in need_embed])
        if vecs is not None and len(vecs) == len(need_embed):
            for e, v in zip(need_embed, vecs):
                e["_embedding"] = v

    emb_by_id = {e["event_id"]: e["_embedding"] for e in all_events if e.get("_embedding")}
    cache = load_embedding_cache(date)
    for r in load_search_index():
        if r["date"] != date:
            continue
        emb = emb_by_id.get(r["event_id"])
        if not emb:
            continue
        cache[r["event_id"]] = {
            "embedding": emb,
            "title": r["title"],
            "one_liner": r.get("summary", ""),
            "topics": r.get("topics", []),
            "date": date,
            "md_url": _index_url_to_md(r["url"]),  # for the markdown-rendered daily doc
            "url": r["url"],  # raw directory-URL form, for the JS-rendered period-view widget
            "lineage_id": r.get("lineage_id", ""),
        }
    save_embedding_cache(cache)


def _merge_new_events(
    stored: list[dict], new_events: list[dict], now: dt.datetime,
    cross_day: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Merge new_events into stored events. Returns (all_events, truly_new, updated).

    Events are matched two ways, in order:
    1. **Exact title match** (same slugified title): just merges in new source
       URLs — the common case when the same RSS item resurfaces.
    2. **Embedding similarity + Haiku validation** (same topic, cosine sim >=
       MERGE_COS_THRESHOLD, then a cheap same-story check): catches a story
       that evolved with a very different headline (e.g. "Colombia quake
       kills 111" -> "South American disaster toll climbs") — semantically
       the same event with near-zero shared words. Requires VOYAGE_API_KEY;
       if it's unset or the embed call fails, no match is attempted for that
       event (fails closed — no LLM-unvalidated heuristic merge).

    A same-day match from (2) is treated as an UPDATE, not a new
    story: the ORIGINAL title/event_id/URL anchor is kept so shared links
    never break; summary/importance/keywords refresh from the new clustering
    pass, sources merge, and it's returned in `updated` — always
    re-researched this run (not budget-gated like `truly_new`), since a
    story a reader already trusts deserves fresh takeaways, not stale ones
    under a bumped headline.

    When no same-day match is found, `cross_day` (the embedding cache from
    `load_embedding_cache`, spanning the last CROSS_DAY_LOOKBACK_DAYS days)
    is checked too. A match there does NOT rewrite the earlier day's already-
    published page — instead a new event is created for TODAY, backlinked to
    the earlier version ("Update to: ..."), so a reader can jump back day by
    day without ever risking a stale/broken link to a page that moved. Among
    several qualifying candidates across days, the MOST RECENT one wins, so
    a multi-day chain always points to its immediately preceding link, not
    an arbitrary earlier one in the window.

    `truly_new` is genuinely new events (subject to select_research's
    per-topic budget); `updated` is existing or cross-day-linked stories
    whose facts changed.
    """
    cross_day = cross_day or {}
    now_iso = now.isoformat()
    by_id: dict[str, dict] = {}
    for ev in stored:
        eid = ev.get("event_id") or slugify(ev.get("title", ""))
        ev["event_id"] = eid
        ev.setdefault("lineage_id", eid)
        ev.setdefault("received_at", now_iso)
        by_id[eid] = ev

    # Only candidates that survive the exact-title check need matching at all.
    candidates: list[dict] = []
    for ev in new_events:
        eid = slugify(ev.get("title", ""))
        ev["event_id"] = eid
        if eid in by_id:
            _merge_sources(by_id[eid], ev)
        else:
            candidates.append(ev)

    # Embed any events still missing a vector: candidates that didn't get a
    # centroid from stage1 (rare), plus stored events from state that predate
    # the centroid carry-through. Skip anything already embedded — stage1
    # centroids count, so well-formed runs only embed the stored backlog once.
    # Cap stored backfill to one batch per run so we never exceed the Voyage
    # RPM limit; embeddings persist in state.json so the backlog drains over
    # several runs without ever hammering the API.
    need_candidate_embed = [c for c in candidates if "_embedding" not in c]
    stored_backfill_cap = max(0, _VOYAGE_BATCH - len(need_candidate_embed))
    need_stored_embed = [ev for ev in by_id.values() if "_embedding" not in ev][:stored_backfill_cap]
    need_embed = need_candidate_embed + need_stored_embed
    embeddings_ok = all("_embedding" in c for c in candidates)  # true if all candidates have centroid
    if need_embed:
        vecs = _voyage_embed([_embed_text(e) for e in need_embed])
        if vecs is not None and len(vecs) == len(need_embed):
            for ev, v in zip(need_embed, vecs):
                ev["_embedding"] = v
            embeddings_ok = all("_embedding" in c for c in candidates)
        else:
            embeddings_ok = False

    truly_new: list[dict] = []
    updated: list[dict] = []
    updated_ids: set[str] = set()
    for ev in candidates:
        new_topics = set(ev.get("topics", []))
        match_id = None

        if embeddings_ok and "_embedding" in ev:
            best_id, best_score = None, 0.0
            for sid, stored_ev in by_id.items():
                if sid in updated_ids or not (new_topics & set(stored_ev.get("topics", ()))):
                    continue
                stored_vec = stored_ev.get("_embedding")
                if not stored_vec:
                    continue
                score = _cosine(ev["_embedding"], stored_vec)
                if score > best_score:
                    best_id, best_score = sid, score
            if best_id and best_score >= MERGE_COS_THRESHOLD:
                relation = _classify_relation(_embed_text(ev), _embed_text(by_id[best_id]))
                if relation in _MERGE_RELATIONS:
                    match_id = best_id
                elif relation in ("follow_on", "related"):
                    # Distinct event, but worth cross-linking from its parent
                    # rather than showing up as if unconnected.
                    ev["related_to"] = best_id
        # (no else branch — without embeddings, no match is attempted; the
        # event falls through to truly_new rather than merging on a weaker,
        # LLM-unvalidated heuristic.)

        if match_id:
            existing = by_id[match_id]
            _merge_sources(existing, ev)
            existing["one_liner"] = ev.get("one_liner") or existing.get("one_liner", "")
            existing["importance"] = max(existing.get("importance", 0), ev.get("importance", 0))
            existing["keywords"] = sorted(
                set(existing.get("keywords", [])) | set(ev.get("keywords", []))
            )
            existing.pop("researched", None)  # facts changed — takeaways are now stale
            existing["received_at"] = now_iso
            updated_ids.add(match_id)
            updated.append(existing)
            continue

        # No same-day match — check the last CROSS_DAY_LOOKBACK_DAYS days.
        # Copy, don't move: today's event stands on its own (its own
        # event_id/importance/takeaways), backlinked to the earlier version
        # rather than reopening and rewriting that day's already-published,
        # possibly-bookmarked page.
        if embeddings_ok and "_embedding" in ev and cross_day:
            qualifying = [
                (cid, c) for cid, c in cross_day.items()
                if (new_topics & set(c.get("topics", ())))
                and c.get("embedding")
                and _cosine(ev["_embedding"], c["embedding"]) >= MERGE_COS_THRESHOLD
            ]
            if qualifying:
                # Most recent qualifying candidate wins — a multi-day chain
                # always points to its immediately preceding link, never a
                # scattered set of every earlier day it resembles.
                best_cid, best = max(qualifying, key=lambda kv: kv[1]["date"])
                cand_text = f"{best['title']} {best.get('one_liner', '')}".strip()
                relation = _classify_relation(_embed_text(ev), cand_text)
                if relation in _MERGE_RELATIONS:
                    ev["updates_title"] = best["title"]
                    ev["updates_url"] = best["md_url"]  # markdown-relative, for render_briefing
                    ev["updates_index_url"] = best.get("url", "")  # directory-URL, for search-index/period-view
                    ev["received_at"] = now_iso
                    # Inherit the chain's stable id (not this copy's own
                    # event_id) so every link in a multi-day chain shares one
                    # lineage_id, however titles drift along the way.
                    ev["lineage_id"] = best.get("lineage_id") or best_cid
                    eid = ev["event_id"]
                    by_id[eid] = ev
                    updated.append(ev)  # always re-researched, like a same-day update
                    continue

        eid = ev["event_id"]
        ev["received_at"] = now_iso
        ev.setdefault("lineage_id", eid)
        by_id[eid] = ev
        truly_new.append(ev)

    return list(by_id.values()), truly_new, updated


def record_metrics(mode: str, run_start: dt.datetime, refresh: bool = False) -> None:
    """Log a cost summary and prepend a per-run record to metrics.json so the
    newest run is always first (top of the file)."""
    rec = {"timestamp": run_start.isoformat(), "mode": mode,
           **({"refresh": True} if refresh else {}), **METRICS.record()}
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
    base_dir = KIND_DIR["daily"].name
    # related_to points at another event_id shown today (a distinct-but-connected
    # story) — resolve it to that event's own title/url here, once, so the
    # client doesn't need a second lookup. If that target didn't make the cut
    # for display today, it's simply omitted (not every clustered event ships).
    shown_by_id = {
        (ev.get("event_id") or slugify(ev["title"])): {
            "title": ev["title"],
            "url": f"{base_dir}/{dt_}/{date}/#{slugify(ev['title'])}",
        }
        for ev, dt_ in shown
    }
    for ev, display_topic in shown:
        related = shown_by_id.get(ev.get("related_to"))
        index.append(
            {
                "date": date,
                "event_id": slugify(ev["title"]),
                "title": ev["title"],
                "summary": (ev.get("one_liner") or "").strip(),
                "theme": ev.get("theme", ""),
                "importance": ev.get("importance", 0),
                "topics": ev.get("topics", []),
                "feeds": ev.get("feeds", []),
                "primary_feed": ev.get("primary_feed", DEFAULT_FEED),
                "keywords": ev.get("keywords", []),
                "url": f"{base_dir}/{display_topic}/{date}/#{slugify(ev['title'])}",
                "sources": ev.get("sources", []),
                "takeaways": ev.get("takeaways", []),
                "researched": bool(ev.get("researched", False)),
                "received_at": ev.get("received_at", ""),
                "lineage_id": ev.get("lineage_id", ""),
                "related_title": related["title"] if related else "",
                "related_url": related["url"] if related else "",
                "updates_title": ev.get("updates_title", ""),
                "updates_url": ev.get("updates_index_url", ""),
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


def render_briefing(events: list[dict], topic: str) -> str:
    """Deterministically render a briefing from Stage-1 events — no Stage 2, no
    web research. Uses the one-sentence summary Haiku already produced. Cheapest
    path; also guarantees format (meters, anchors, citations)."""
    if not events:
        return quiet_day_body(topic)
    ev = sorted(events, key=lambda e: e.get("importance", 0), reverse=True)
    title_by_id = {e["event_id"]: e["title"] for e in ev if e.get("event_id")}

    lines: list[str] = []

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
            lines.append(f"### {meter(e.get('importance', 0))} {e['title']}")
            summary = (e.get("one_liner") or "").strip()
            if summary:
                lines.append(summary)
            takeaways = [t.strip() for t in e.get("takeaways", []) if t.strip()]
            if takeaways:
                lines.append("")
                lines.append('<ul class="takeaways">')
                lines += [f"<li>{html.escape(t)}</li>" for t in takeaways]
                lines.append("</ul>")
            lines.append("")
            srcs = ", ".join(
                f"[{s['label']}]({s['url']}) {_source_badge(s.get('origin', 'rss'))}"
                for s in e.get("sources", [])[:MAX_SOURCES_PER_EVENT]
            )
            if srcs:
                lines.append(f"Sources: {srcs}")
            received = e.get("received_at")
            if received:
                lines.append(f'<time class="received-time" datetime="{received}"></time>')
            related_title = title_by_id.get(e.get("related_to"))
            if related_title:
                lines.append(f"See also: [{related_title}](#{slugify(related_title)})")
            if e.get("updates_title") and e.get("updates_url"):
                lines.append(f"Update to: [{e['updates_title']}]({e['updates_url']})")
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


def _period_lists(records: list[dict], weekly_days: int = 7, monthly_days: int = 30) -> dict:
    """Split scope-filtered `records` into daily/weekly/monthly windows, anchored
    on the most recent date present. Each window is deduped (same story surfaced
    under multiple topics) and importance-sorted. No caps — pagination handles it."""
    if not records:
        return {"daily": [], "weekly": [], "monthly": []}
    latest = max(r["date"] for r in records)
    latest_d = dt.date.fromisoformat(latest)
    weekly_cutoff = (latest_d - dt.timedelta(days=weekly_days - 1)).isoformat()
    monthly_cutoff = (latest_d - dt.timedelta(days=monthly_days - 1)).isoformat()

    def _window(cutoff: str) -> list[dict]:
        ranked = sorted((r for r in records if r["date"] >= cutoff),
                        key=lambda r: r.get("importance", 0), reverse=True)
        return _dedupe_cross_topic(ranked)

    return {
        "daily": _window(latest),
        "weekly": _window(weekly_cutoff),
        "monthly": _window(monthly_cutoff),
    }


def _period_item(r: dict, feeds: dict | None = None) -> dict:
    """One record trimmed to what the client-side period-view widget needs.

    `url` is left in its raw directory-URL form (e.g. "news/tech/2026-08-11/#slug")
    — unlike markdown-rendered links, this is injected as a raw <a href> at
    runtime by JS, so it must match MkDocs's actual served path (no .md suffix;
    MkDocs's build-time .md link rewriting never sees this).

    `feeds`/`subfeeds` carry both the machine key (for filtering) and the
    display title (for the clickable badges) — feeds are the broad section
    (Technology, World, ...), subfeeds are the finer topic within it (Tech,
    AI, Anthropic, ...)."""
    feeds = feeds or {}
    return {
        "title": r["title"],
        "url": r["url"],
        "importance": r.get("importance", 0),
        "date": r["date"],
        "summary": (r.get("summary") or "").strip()[:300],
        "takeaways": [t.strip() for t in r.get("takeaways", []) if t.strip()],
        "sources": [
            {"label": s["label"], "url": s["url"], "origin": s.get("origin", "rss")}
            for s in r.get("sources", []) if s.get("url")
        ][:MAX_SOURCES_PER_EVENT],
        "subfeeds": [{"key": t, "title": topic_display(t)} for t in r.get("topics", [])],
        "feeds": [{"key": fk, "title": feeds[fk]["title"]} for fk in r.get("feeds", []) if fk in feeds],
        "researched": bool(r.get("researched", False)),
        "receivedAt": r.get("received_at") or "",
        "relatedTitle": r.get("related_title") or "",
        "relatedUrl": r.get("related_url") or "",
        "updatesTitle": r.get("updates_title") or "",
        "updatesUrl": r.get("updates_url") or "",
    }


def _period_view_block(records: list[dict], prefix: str = "", feeds: dict | None = None,
                       scope_feed: str | None = None) -> list[str]:
    """Markdown lines embedding a client-rendered Daily/Weekly/Monthly story
    widget (docs/assets/period-view.js). No LLM involved — pure aggregation of
    the search index, done here in Python; the JS just switches between the
    three precomputed, already-sorted-and-deduped windows.

    When `feeds` is given, also embeds a static feed -> subfeed(topic) map
    (`feedMeta`) so the widget's Feed filter can narrow its Subfeed options to
    exactly what that feed configures, independent of which topics happen to
    have stories in the current window.

    `scope_feed`, on an already feed-scoped page, hides the redundant Feed
    filter dropdown (the page's records may still cross-list a second feed,
    but re-picking "this feed" here isn't useful) — badges/click-filtering
    still work, only the dropdown is suppressed."""
    data = _period_lists(records)
    if not any(data.values()):
        return ["_No stories yet._", ""]
    payload = {period: [_period_item(r, feeds) for r in rows] for period, rows in data.items()}
    if feeds:
        payload["feedMeta"] = {
            fk: {"title": spec["title"],
                 "subfeeds": [{"key": t, "title": topic_display(t)}
                             for t in spec.get("topics", []) + spec.get("also_includes", [])]}
            for fk, spec in feeds.items()
        }
    blob = json.dumps(payload, ensure_ascii=False).replace("</script>", "<\\/script>")
    scope_attr = f' data-scope-feed="{scope_feed}"' if scope_feed else ""
    return [
        f'<div id="period-view" class="period-view" data-prefix="{prefix}"{scope_attr}></div>',
        "",
        '<script id="period-view-data" type="application/json">',
        blob,
        "</script>",
        "",
        f'<script src="{prefix}assets/period-view.js" defer></script>',
        "",
    ]


def _event_counts(index: list[dict]) -> dict[tuple, int]:
    """Map (topic, date) -> number of events, from the search index."""
    counts: dict[tuple, int] = {}
    for r in index:
        for t in r.get("topics", []):
            counts[(t, r["date"])] = counts.get((t, r["date"]), 0) + 1
    return counts


def _daily_link(topic: str, stem: str, counts: dict[tuple, int],
                base_dir: str | None = None) -> str:
    """A daily-doc link annotated with its event count, e.g. '2026-07-25 (3 events)'."""
    n = counts.get((topic, stem), 0)
    unit = "event" if n == 1 else "events"
    d = base_dir or KIND_DIR["daily"].name
    return f"[{stem} ({n} {unit})]({d}/{topic}/{stem}.md)"


TOPIC_DISPLAY = {"ai": "AI", "gpt": "OpenAI", "api": "API",
                 "tech-research": "Tech Research",
                 "climate-resilience": "Climate & Ecological Resilience",
                 "climate-change": "Climate Change",
                 "diet-exercise": "Diet & Exercise"}


def topic_display(topic: str) -> str:
    """Human-friendly topic label for headings/nav (ai -> AI, email-security ->
    Email Security)."""
    return TOPIC_DISPLAY.get(topic, topic.replace("-", " ").title())


def _parse_wotd(word: str, summary_html: str, shortdef: str | None) -> dict | None:
    """Pull {part_of_speech, definition, example} out of a Merriam-Webster WOTD
    entry's summary HTML. Pure/testable. Falls back to the short definition."""
    text = re.sub(r"(?is)<a\s.*?</a>", "", summary_html or "")  # drop 'See the entry >'
    text = re.sub(r"(?is)</?(p|br|div)\s*/?>", "\n", text)      # paragraph/break -> newline
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]

    pos = definition = example = ""
    seen_pron = False
    for ln in lines:
        if "•" in ln and not seen_pron:            # "word • \pron\ • adjective"
            seen_pron = True
            pos = ln.split("•")[-1].strip()
            continue
        if not seen_pron:
            continue
        if ln.startswith("//"):
            example = example or ln.lstrip("/").strip()
        elif not definition and not ln.lower().startswith(("examples", "did you know")):
            definition = ln
    if not definition and isinstance(shortdef, str):
        definition = shortdef.strip()
    if not (word and definition):
        return None
    return {"word": word.strip(), "part_of_speech": pos,
            "definition": definition, "example": example}


def fetch_word_of_the_day() -> dict | None:
    """Merriam-Webster's Word of the Day (usable words w/ definition + example).
    Returns {word, part_of_speech, definition, example, link} or None on any
    failure — the home page simply omits the card then."""
    try:
        d = feedparser.parse(WOTD_FEED_URL)
    except Exception as e:
        log(f"word-of-the-day fetch failed: {e}")
        return None
    if not d.entries:
        return None
    e = d.entries[0]
    parsed = _parse_wotd(e.get("title") or "", e.get("summary") or "",
                         e.get("merriam_shortdef"))
    if parsed:
        parsed["link"] = e.get("link") or "https://www.merriam-webster.com/word-of-the-day"
    return parsed


def _wotd_card(w: dict) -> list[str]:
    """Render the Word of the Day as a raw-HTML card (no blank lines inside so
    Markdown treats it as one HTML block)."""
    esc = html.escape
    pos = (f' <span class="wotd-pos">{esc(w["part_of_speech"])}</span>'
           if w.get("part_of_speech") else "")
    inner = [
        '<div class="wotd-label">\U0001F4D6 Word of the day</div>',
        f'<div class="wotd-word">{esc(w["word"])}{pos}</div>',
        f'<div class="wotd-def">{esc(w["definition"])}</div>',
    ]
    if w.get("example"):
        inner.append(f'<div class="wotd-ex">{esc(w["example"])}</div>')
    inner.append(f'<a class="wotd-src" href="{esc(w.get("link", ""))}" '
                 'target="_blank" rel="noopener">Merriam-Webster</a>')
    return ['<div class="wotd" id="wotd">'] + inner + ['</div>', '']


def _parse_fact(e: dict) -> dict | None:
    year, text = e.get("year"), (e.get("text") or "").strip()
    if not (year and text):
        return None
    link = ""
    for p in e.get("pages") or []:
        try:
            link = p["content_urls"]["desktop"]["page"]
            break
        except (KeyError, TypeError):
            continue
    return {"year": int(year), "text": text, "link": link}


def fetch_fact_of_the_day(now: dt.datetime | None = None) -> dict | None:
    """A verifiable 'on this day' fact from Wikipedia's curated `selected` events.
    Returns {year, text, link} or None on failure — the home page just omits the
    card then. Deterministic, no LLM. Shows the first (most recent) event."""
    now = now or now_utc()
    url = ONTHISDAY_URL.format(mm=f"{now.month:02d}", dd=f"{now.day:02d}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ONTHISDAY_UA})
        data = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception as e:
        log(f"fact-of-the-day fetch failed: {e}")
        return None
    facts = [f for f in (_parse_fact(e) for e in data.get("selected", [])) if f]
    return facts[0] if facts else None


def _fact_card(f: dict) -> list[str]:
    esc = html.escape
    src = (f' <a class="fact-src" href="{esc(f["link"])}" target="_blank" '
           'rel="noopener">Wikipedia&nbsp;&rarr;</a>') if f.get("link") else ""
    return [
        '<div class="fact" id="on-this-day">',
        f'<div class="fact-label">\U0001F4C5 On this day &middot; {esc(str(f["year"]))}</div>',
        f'<div class="fact-text">{esc(f["text"])}{src}</div>',
        "</div>",
        "",
    ]


def _factsite_snippet(raw: str) -> str:
    """Text slice around thefactsite.com's 'Fact of the Day' widget, tags
    stripped — keeps the Haiku input tiny. Pure/testable."""
    i = raw.lower().find("fact of the day")
    if i < 0:
        return ""
    seg = raw[i:i + 1600]
    seg = re.sub(r"(?is)<script.*?</script>", " ", seg)
    seg = re.sub(r"(?is)<[^>]+>", " ", seg)
    return html.unescape(re.sub(r"\s+", " ", seg)).strip()


def fetch_fun_fact() -> dict | None:
    """thefactsite.com 'Fact of the Day': fetch the homepage, slice the widget,
    and have Haiku extract the fact verbatim from a tiny snippet (~$0.0005).
    Returns {text} or None — the card is simply omitted on any failure. Not an
    API, so this degrades gracefully if the page changes."""
    try:
        req = urllib.request.Request(FACTSITE_URL, headers={"User-Agent": ONTHISDAY_UA})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
    except Exception as e:
        log(f"fun-fact fetch failed: {e}")
        return None
    snippet = _factsite_snippet(raw)
    if not snippet:
        return None
    try:
        resp = _client().messages.create(
            model=CLUSTER_MODEL,
            max_tokens=150,
            output_config={"format": {"type": "json_schema", "schema": FUNFACT_SCHEMA}},
            system=("You are given text scraped from around a website's 'Fact of "
                    "the Day' widget. Return JSON {\"fact\": \"...\"} containing "
                    "ONLY that day's fact, copied verbatim from the text. Do not "
                    "invent, summarize, or add anything. If there is no clear "
                    "single fact, return an empty string."),
            messages=[{"role": "user", "content": snippet}],
        )
    except Exception as e:
        log(f"fun-fact parse failed: {e}")
        return None
    METRICS.add("(funfact)", CLUSTER_MODEL, resp.usage)
    try:
        fact = (json.loads(_text_of(resp)).get("fact") or "").strip()
    except json.JSONDecodeError:
        return None
    return {"text": fact} if fact else None


def _funfact_card(f: dict) -> list[str]:
    esc = html.escape
    return [
        '<div class="fact funfact" id="fun-fact">',
        '<div class="fact-label">\U0001F4A1 Fact of the day</div>',
        f'<div class="fact-text">{esc(f["text"])} '
        '<a class="fact-src" href="https://www.thefactsite.com/fact-of-the-day/" '
        'target="_blank" rel="noopener">The Fact Site&nbsp;&rarr;</a></div>',
        "</div>",
        "",
    ]


def record_fun_fact(f: dict) -> None:
    """Append today's fun fact + date to docs/funfacts.json (deduped against the
    last entry). The Facts page reads this history."""
    hist: list = []
    if FUNFACTS_FILE.exists():
        try:
            hist = json.loads(FUNFACTS_FILE.read_text())
        except json.JSONDecodeError:
            hist = []
    if hist and hist[-1].get("text") == f["text"]:
        return
    hist.append({"date": now_utc().date().isoformat(), "text": f["text"]})
    FUNFACTS_FILE.write_text(
        json.dumps(hist[-FUNFACTS_HISTORY_MAX:], ensure_ascii=False, indent=2) + "\n"
    )


def record_word_of_the_day(w: dict) -> None:
    """Append today's word to docs/words.json (deduped against the last entry so
    same-day re-runs don't duplicate). The quiz page reads the recent tail."""
    hist: list = []
    if WORDS_FILE.exists():
        try:
            hist = json.loads(WORDS_FILE.read_text())
        except json.JSONDecodeError:
            hist = []
    if hist and hist[-1].get("word") == w["word"]:
        return
    hist.append({"date": now_utc().date().isoformat(),
                 "word": w["word"], "part_of_speech": w.get("part_of_speech", ""),
                 "definition": w["definition"], "example": w.get("example", ""),
                 "link": w.get("link", "")})
    hist = hist[-WORDS_HISTORY_MAX:]
    WORDS_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n")


def rebuild_index() -> None:
    all_topics = sorted({s["topic"] for s in load_sources()})
    feeds, _topic_feed = load_feeds(all_topics)
    lines = [
        "---",
        "hide:",
        "  - toc",
        "---",
        "",
    ]
    today_str = now_utc().date().isoformat()

    # Use cached WOTD if already fetched today (avoids re-hitting the API on
    # every 15-minute run; the Merriam-Webster word-of-the-day doesn't change
    # intra-day anyway).
    wotd = None
    if WORDS_FILE.exists():
        try:
            _wh = json.loads(WORDS_FILE.read_text())
            if _wh and isinstance(_wh[-1], dict) and _wh[-1].get("date") == today_str:
                wotd = _wh[-1]
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    if not wotd:
        wotd = fetch_word_of_the_day()
        if wotd:
            record_word_of_the_day(wotd)

    # "On this day" fact: Wikipedia, free HTTP call, always fresh.
    fact = fetch_fact_of_the_day()

    # Use cached fun fact if already fetched today (avoids a Haiku call on
    # every run — thefactsite's fact-of-the-day doesn't change intra-day).
    fun = None
    if FUNFACTS_FILE.exists():
        try:
            _fh = json.loads(FUNFACTS_FILE.read_text())
            if _fh and isinstance(_fh[-1], dict) and _fh[-1].get("date") == today_str:
                fun = _fh[-1]
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    if not fun:
        fun = fetch_fun_fact()
        if fun:
            record_fun_fact(fun)

    # Compact teaser bar linking down to the cards below the stories section.
    teaser_parts = []
    esc = html.escape
    if wotd:
        teaser_parts.append(
            f'<a href="#wotd">\U0001F4D6 Word of the day: <strong>{esc(wotd["word"])}</strong></a>'
        )
    if fun:
        teaser_parts.append('<a href="#fun-fact">\U0001F4A1 Fact of the day</a>')
    if fact:
        teaser_parts.append('<a href="#on-this-day">\U0001F4C5 On this day</a>')
    if teaser_parts:
        lines += [
            '<div class="daily-teasers">',
            "\n".join(teaser_parts),
            "</div>",
            "",
        ]

    lines += ["## Top stories", ""]
    lines += _period_view_block(load_search_index(), feeds=feeds)
    if wotd:
        lines += _wotd_card(wotd)
    if fact:
        lines += _fact_card(fact)
    if fun:
        lines += _funfact_card(fun)
    lines += [
        "",
        "---",
        "",
        "Don't see a topic you want? "
        f"[Request a new topic]({REPO_URL}/issues/new?template=topic-request.yml).",
        "",
    ]
    (DOCS / "index.md").write_text("\n".join(lines) + "\n")
    log("rebuilt docs/index.md")


def _feed_page_body(
    fkey: str, spec: dict, index: list[dict],
    latest: str | None, refresh_map: dict[str, str],
    feeds: dict,
    topic_link_dir: str = "topics",
    story_prefix: str = "../../",
) -> list[str]:
    """Build the markdown lines for one feed overview page. `index` records
    carry a `feeds` list (primary + any also_includes cross-listing) computed
    at clustering time — membership here is just a lookup, no re-derivation.

    story_prefix defaults to "../../": with MkDocs's directory URLs,
    docs/feeds/<fkey>.md is SERVED at /feeds/<fkey>/ (two path segments), so
    the period-view widget's runtime-injected <a href> links — which MkDocs's
    build-time link rewriter never sees — need two levels back to site root."""
    ftopics = spec["topics"]
    also_topics = set(spec.get("also_includes", []))
    records = [r for r in index if fkey in r.get("feeds", [])]
    today_count = sum(1 for r in records if r["date"] == latest) if latest else 0

    lines = [f"# {spec['title']} ({today_count})" if today_count else f"# {spec['title']}", ""]
    ts_str = refresh_map.get(fkey)
    if ts_str:
        try:
            rt = dt.datetime.fromisoformat(ts_str)
            utc_label = rt.strftime("%H:%M UTC")
            lines += [f'_Feed <time class="feed-refresh" datetime="{rt.isoformat()}">'
                     f'refreshed {utc_label}</time>._', ""]
        except (ValueError, TypeError):
            pass
    lines += ["## Top stories", ""]
    lines += _period_view_block(records, prefix=story_prefix, feeds=feeds, scope_feed=fkey)
    counts = _event_counts(index)
    lines += ["## Topics", ""]
    all_shown_topics = ftopics + [t for t in sorted(also_topics) if t not in ftopics]
    for t in all_shown_topics:
        n = counts.get((t, latest), 0) if latest else 0
        unit = "story" if n == 1 else "stories"
        lines.append(f"- [{topic_display(t)}](../{topic_link_dir}/{t}.md) — {n} {unit}")
    lines.append("")
    return lines


def rebuild_feed_pages(feed_last_refresh: dict[str, str] | None = None) -> None:
    """One page per feed (docs/feeds/<feed>.md). Built from the search index —
    no LLM, regenerated each run."""
    index = load_search_index()
    all_topics = sorted({s["topic"] for s in load_sources()})
    feeds, _topic_feed = load_feeds(all_topics)
    latest = max((r["date"] for r in index), default=None)
    refresh_map: dict[str, str] = feed_last_refresh or {}

    for fkey, spec in feeds.items():
        lines = _feed_page_body(fkey, spec, index, latest, refresh_map, feeds)
        path = DOCS / "feeds" / f"{fkey}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    log(f"rebuilt {len(feeds)} feed pages")


def _topic_page_body(
    topic: str, by_topic: dict, feeds: dict, topic_feed: dict, research_cfg: dict,
    daily_dir: str, feed_link_dir: str = "feeds",
) -> list[str]:
    """Build the markdown lines for one topic overview page."""
    dates = by_topic.get(topic, {})
    latest = max(dates) if dates else None
    latest_count = len(dates.get(latest, [])) if latest else 0
    disp = topic_display(topic)
    lines = ["---", "hide:", "  - toc", "---", "", f"# {disp} ({latest_count})" if latest else f"# {disp}", ""]
    fkey = topic_feed.get(topic, DEFAULT_FEED)
    badge = "AI-researched" if research_enabled(topic, research_cfg) else "RSS only"
    lines += [f"_Part of the [{feeds[fkey]['title']}](../{feed_link_dir}/{fkey}.md) feed · {badge}._", ""]
    if latest:
        records = [r for rows in dates.values() for r in rows]
        lines += ["## Latest", ""]
        lines += _period_view_block(records, prefix="../../", feeds=feeds)
        earlier = sorted((d for d in dates if d != latest), reverse=True)
        if earlier:
            lines += ["## Earlier", ""]
            earlier_counts = {(topic, d): len(dates[d]) for d in earlier}
            parts = [_daily_link(topic, d, earlier_counts, base_dir=f"../{daily_dir}")
                     for d in earlier]
            lines += [" · ".join(parts), ""]
    else:
        lines += ["_No briefings yet._", ""]
    return lines


def rebuild_topic_pages() -> None:
    """One page per topic (docs/topics/<topic>.md)."""
    index = load_search_index()
    by_topic: dict[str, dict[str, list[dict]]] = {}
    for r in index:
        for t in r.get("topics", []):
            by_topic.setdefault(t, {}).setdefault(r["date"], []).append(r)

    all_topics = sorted({s["topic"] for s in load_sources()})
    feeds, topic_feed = load_feeds(all_topics)
    research_cfg = load_research_config()
    for topic in all_topics:
        lines = _topic_page_body(
            topic, by_topic, feeds, topic_feed, research_cfg,
            daily_dir=KIND_DIR["daily"].name,
        )
        path = DOCS / "topics" / f"{topic}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    log(f"rebuilt {len(all_topics)} topic pages")


def rebuild_archive() -> None:
    """Full back-catalog: every daily doc grouped by month -> topic (with event
    counts). Regenerated each run alongside the index."""
    index = load_search_index()
    counts = _event_counts(index)

    lines = [
        "# Archive",
        "",
        "Full history of daily briefings, grouped by month. Use the "
        "[keyword search](search.md) to filter by term, date, or topic.",
        "",
    ]

    # daily docs: month (YYYY-MM) -> topic -> [stems]
    daily_base = KIND_DIR["daily"]
    by_month: dict[str, dict[str, list[str]]] = {}
    for topic in _topics_in(daily_base):
        for stem in _dated_stems(daily_base / topic):
            if _parse_date_stem(stem) is None:
                continue
            by_month.setdefault(stem[:7], {}).setdefault(topic, []).append(stem)

    if not by_month:
        lines += ["_No briefings yet._", ""]
    for month in sorted(by_month, reverse=True):
        lines += [f"## {month}", ""]
        for topic in sorted(by_month[month]):
            stems = sorted(by_month[month][topic], reverse=True)
            links = " · ".join(_daily_link(topic, s, counts) for s in stems)
            lines.append(f"- **{topic}:** {links}")
        lines.append("")

    (DOCS / "archive.md").write_text("\n".join(lines) + "\n")
    log("rebuilt docs/archive.md")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_all(sources: list[dict]) -> list[dict]:
    items: list[dict] = []
    for src in sources:
        key = source_slug(src["name"])
        scraper_name = src.get("scraper")
        if scraper_name:
            scraper_fn = SCRAPERS.get(scraper_name)
            if scraper_fn is None:
                log(f"unknown scraper {scraper_name!r} for {src['name']!r}, skipping")
                continue
            try:
                entries = scraper_fn(src)  # type: ignore[operator]
            except Exception as e:
                log(f"scraper {scraper_name!r} ({src['name']!r}) failed: {e}")
                continue
            for i, entry in enumerate(entries):
                items.append(normalize_entry(src["name"], src["topic"], i, key, entry))
            continue
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


def _parse_date_stem(stem: str) -> dt.date | None:
    # Strictly YYYY-MM-DD; reject ISO-week ("2026-W30") and other forms that
    # date.fromisoformat() would otherwise accept on Python 3.11+.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
        return None
    try:
        return dt.date.fromisoformat(stem)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Daily run
# --------------------------------------------------------------------------- #
def _write_topic_doc(topic: str, shown: list[dict], date: str) -> None:
    """Write one topic's daily doc from the events shown under it (quiet if none).
    Docs and the search index are both rebuilt from this run's events, so they
    stay consistent by construction."""
    doc_path = KIND_DIR["daily"] / topic / f"{date}.md"
    title = f"Sam's News — {topic} — {date}"
    if shown:
        write_doc(doc_path, front_matter(daily_tags(shown, date, topic)),
                  title, render_briefing(shown, topic))
    else:
        write_doc(doc_path, front_matter([date[:4], date[:7], topic]),
                  title, quiet_day_body(topic))


def _already_ran_today(state: dict, date: str) -> bool:
    """True if state's last_run is on `date` (UTC). Used to skip redundant
    scheduled cron slots without re-running."""
    last = state.get("last_run")
    if not last:
        return False
    try:
        return dt.datetime.fromisoformat(last).date().isoformat() == date
    except ValueError:
        return False


def run_daily(dry_run: bool, no_research: bool = False, skip_if_done: bool = False) -> None:
    run_start = now_utc()
    date = run_start.date().isoformat()
    state = load_state()
    if skip_if_done and _already_ran_today(state, date):
        log(f"daily already ran today ({date}); skipping redundant scheduled slot")
        return
    cutoff = compute_cutoff(state, run_start)
    seen = state.get("seen_links", {})
    source_last_ts: dict[str, str] = state.get("source_last_ts") or {}
    log(f"daily {date}: cutoff={cutoff.isoformat()} "
        f"seen_links={len(seen)} src_ts_pointers={len(source_last_ts)}")

    sources = load_sources()
    cfg = load_research_config()
    raw = fetch_all(sources)
    # Only cluster items newer than each source's last-processed timestamp so
    # hourly incremental runs don't reprocess events already clustered today.
    items = filter_and_cap(raw, cutoff, seen, today=date, source_last_ts=source_last_ts)
    all_topics = sorted({s["topic"] for s in sources})
    feeds, topic_feed = load_feeds(all_topics)

    # Load stored events from earlier runs today.
    all_stored = _load_today_events(state, date)
    fallback_tried = _load_fallback_tried(state, date)

    # Stage 1: one unified clustering pass over all feeds.
    if items:
        new_events = assign_topics(
            attach_sources(stage1_cluster(items, feeds), items),
            items, topic_feed, feeds,
        )
    else:
        new_events = []
    log(f"clustered {len(items)} items -> {len(new_events)} events; "
        f"stored: {len(all_stored)}")

    # Merge new into stored, collect truly new events and updates to existing ones.
    # cross_day lets a new item link back to (not rewrite) a story from an
    # earlier day it updates — see _merge_new_events for why.
    cross_day = load_embedding_cache(date)
    all_events, truly_new, updated = _merge_new_events(all_stored, new_events, run_start, cross_day)
    if updated:
        log(f"{len(updated)} existing event(s) updated with new info: "
            + ", ".join(e["title"][:50] for e in updated))

    if dry_run:
        preview = [
            {"title": e["title"], "importance": e.get("importance"),
             "topics": e.get("topics")}
            for e in sorted(all_events,
                            key=lambda e: e.get("importance", 0), reverse=True)[:30]
        ]
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    # No-op guard: nothing new or updated since the last run.
    if not truly_new and not updated:
        if all_stored:
            log("no new events since last run; updating source timestamps only")
            save_state(state, items, run_start,
                       today_events_data={"date": date, "events": all_stored})
            record_metrics("daily", run_start, refresh=True)
            return
        existing = any((KIND_DIR["daily"] / t / f"{date}.md").exists() for t in all_topics)
        if existing:
            log("no new items; preserving today's existing docs (no-op re-run)")
            save_state(state, items, run_start)
            record_metrics("daily", run_start, refresh=True)
            return

    # truly_new competes for the feed's per-topic research budget; updated
    # events always get re-researched regardless of budget — a story readers
    # already trust deserves fresh takeaways, not stale ones under a new headline.
    selected = select_research(truly_new, feeds, cfg, no_research=no_research)
    if not no_research:
        selected += updated
    log(f"researching {len(selected)} events ({len(updated) if not no_research else 0} of them updates)")
    enriched = research_events(selected, date)
    all_events = merge_enrichment(all_events, enriched)

    # Per-topic docs: write all topics to docs/news/. A topic with zero RSS
    # events gets ONE Serper-backed fallback search per day (not no_research,
    # not already tried today) before it's written up as "Quiet day" — some
    # topics (e.g. slow-publishing academic feeds) miss real news that a live
    # search would catch.
    per_topic: dict[str, list[dict]] = {}
    for topic in all_topics:
        ranked = sorted(
            (e for e in all_events if topic in e.get("topics", [])),
            key=lambda e: e.get("importance", 0), reverse=True,
        )
        # Same real-world story sometimes survives clustering as two distinct
        # events (different exact headlines/sources) — collapse near-duplicates
        # by title/keyword overlap before writing the daily doc.
        per_topic[topic] = _dedupe_cross_topic(ranked)[:EVENTS_PER_TOPIC]
        if (not per_topic[topic] and not no_research
                and topic not in fallback_tried
                and research_enabled(topic, cfg)):
            fallback_tried.add(topic)
            fb = fallback_search_event(topic, feeds, topic_feed, sources)
            if fb:
                log(f"fallback search found news for quiet topic '{topic}': {fb['title'][:60]}")
                all_events.append(fb)
                per_topic[topic] = [fb]
        _write_topic_doc(topic, per_topic[topic], date)

    # One search-index record per shown event.
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
    update_embedding_cache(all_events, date)

    # Mark feeds that got new content this run.
    affected = {fkey for ev in truly_new + updated for fkey in ev.get("feeds", [])}
    new_refresh = {fkey: run_start.isoformat() for fkey in affected}
    feed_last_refresh: dict[str, str] = {
        **dict(state.get("feed_last_refresh") or {}),
        **new_refresh,
    }

    save_state(state, items, run_start,
               today_events_data={"date": date, "events": all_events},
               feed_last_refresh=new_refresh,
               fallback_tried_data={"date": date, "topics": sorted(fallback_tried)})
    rebuild_index()
    rebuild_archive()
    rebuild_topic_pages()
    rebuild_feed_pages(feed_last_refresh=feed_last_refresh)
    record_metrics("daily", run_start)
    log("daily run complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate daily tech-news briefings.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run Stage 1 / child selection and print, without Stage 2 or writing.",
    )
    parser.add_argument(
        "--no-research", action="store_true",
        help="Force web research OFF for all topics (overrides sources.yaml research config).",
    )
    parser.add_argument(
        "--skip-if-done", action="store_true",
        help="Exit early if today's daily already ran (for redundant scheduled cron slots).",
    )
    args = parser.parse_args(argv)
    run_daily(args.dry_run, no_research=args.no_research, skip_if_done=args.skip_if_done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
