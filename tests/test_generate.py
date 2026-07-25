"""Unit tests for the pure (no-network, no-API) parts of generate.py."""
import datetime as dt
from pathlib import Path

import pytest

import generate as g

UTC = dt.timezone.utc


# --- slug / summary -------------------------------------------------------- #
def test_slugify_matches_heading_style():
    assert g.slugify("Go 1.18 ships generics!") == "go-118-ships-generics"
    assert g.slugify("Company ABC raised $40M (Series B)") == "company-abc-raised-40m-series-b"


def test_clean_summary_strips_html_and_truncates():
    assert g.clean_summary("<p>Hello &amp; welcome</p>") == "Hello & welcome"
    long = "x" * (g.SUMMARY_MAX_CHARS + 50)
    out = g.clean_summary(long)
    assert len(out) <= g.SUMMARY_MAX_CHARS and out.endswith("…")
    assert g.clean_summary(None) == ""


# --- source resolution ----------------------------------------------------- #
def test_resolve_source_url_direct_and_query():
    assert g.resolve_source_url({"url": "https://x/feed"}) == "https://x/feed"
    url = g.resolve_source_url({"query": "AI chips"})
    assert url.startswith("https://news.google.com/rss/search?q=AI+chips")


def test_resolve_source_url_requires_url_or_query():
    with pytest.raises(ValueError):
        g.resolve_source_url({"name": "broken"})


# --- entry parsing --------------------------------------------------------- #
def test_entry_datetime_and_normalize():
    st = (2026, 7, 24, 12, 0, 0, 0, 0, 0)
    entry = {"title": " T ", "summary": "<b>s</b>", "link": " http://a ", "published_parsed": st}
    it = g.normalize_entry("Src", "gaming", 3, "src", entry)
    assert it["id"] == "src-3"
    assert it["title"] == "T" and it["summary"] == "s" and it["link"] == "http://a"
    assert it["topic"] == "gaming"
    assert it["_published_dt"] == dt.datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def test_entry_datetime_missing():
    assert g.entry_datetime({}) is None


# --- filtering / capping --------------------------------------------------- #
def _item(link, when, source="S", topic="general"):
    return {
        "id": link, "source": source, "topic": topic, "title": link,
        "summary": "", "link": link, "published": None, "_published_dt": when,
    }


def test_filter_drops_old_and_seen_keeps_undated():
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    cutoff = now - dt.timedelta(hours=24)
    items = [
        _item("fresh", now - dt.timedelta(hours=1)),
        _item("old", now - dt.timedelta(hours=48)),
        _item("seen", now - dt.timedelta(hours=1)),
        _item("undated", None),
    ]
    kept = g.filter_and_cap(items, cutoff, {"seen": "2026-07-24"})
    links = {it["link"] for it in kept}
    assert links == {"fresh", "undated"}


def test_per_feed_cap():
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    cutoff = now - dt.timedelta(hours=24)
    items = [_item(f"l{i}", now - dt.timedelta(minutes=i)) for i in range(50)]
    kept = g.filter_and_cap(items, cutoff, {}, max_per_feed=40)
    assert len(kept) == 40


def test_group_by_topic():
    items = [_item("a", None, topic="gaming"), _item("b", None, topic="ai"), _item("c", None, topic="gaming")]
    groups = g.group_by_topic(items)
    assert set(groups) == {"gaming", "ai"} and len(groups["gaming"]) == 2


# --- rendering helpers ------------------------------------------------------ #
def test_meter():
    assert g.meter(4) == "🔥🔥🔥🔥◯"
    assert g.meter(0) == "🔥◯◯◯◯"   # clamps up to 1
    assert g.meter(9) == "🔥🔥🔥🔥🔥"   # clamps down to 5


def test_attach_sources_dedupes_and_resolves():
    items = [_item("http://a", None, source="TC"), _item("http://a", None, source="Verge")]
    items[0]["id"] = "x1"; items[1]["id"] = "x2"; items[1]["link"] = "http://b"
    events = [{"title": "E", "source_item_ids": ["x1", "x2", "missing"]}]
    out = g.attach_sources(events, items)
    urls = [s["url"] for s in out[0]["sources"]]
    assert urls == ["http://a", "http://b"]


def test_select_top_k_by_importance():
    events = [{"importance": i} for i in (1, 5, 3, 2, 4)]
    top = g.select_top_k(events, k=3)
    assert [e["importance"] for e in top] == [5, 4, 3]


def test_front_matter_and_daily_tags():
    fm = g.front_matter(["b", "a", "a", ""])
    assert fm.startswith("---\ntags:\n") and "  - a" in fm and "  - b" in fm
    tags = g.daily_tags([{"keywords": ["Postgres"], "theme": "Databases"}], "2026-07-24", "postgres")
    assert set(tags) >= {"2026", "2026-07", "postgres", "Postgres", "Databases"}


# --- state ------------------------------------------------------------------ #
def test_compute_cutoff_uses_last_run_then_fallback():
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    assert g.compute_cutoff({"last_run": "2026-07-23T06:00:00+00:00"}, now) == dt.datetime(2026, 7, 23, 6, 0, tzinfo=UTC)
    assert g.compute_cutoff({}, now) == now - dt.timedelta(hours=g.LOOKBACK_HOURS)
    assert g.compute_cutoff({"last_run": None}, now) == now - dt.timedelta(hours=g.LOOKBACK_HOURS)


def test_prune_seen():
    now = dt.datetime(2026, 7, 24, tzinfo=UTC)
    seen = {"keep": "2026-07-20", "drop": "2026-07-01"}
    pruned = g.prune_seen(seen, now)
    assert "keep" in pruned and "drop" not in pruned


# --- rollup date parsing ---------------------------------------------------- #
def test_parse_date_stem():
    assert g._parse_date_stem("2026-07-24") == dt.date(2026, 7, 24)
    assert g._parse_date_stem("2026-W30") is None


# --- research config -------------------------------------------------------- #
def test_research_enabled():
    cfg = {"default": False, "topics": {"ai": True, "world": True}}
    assert g.research_enabled("ai", cfg) is True
    assert g.research_enabled("world", cfg) is True
    assert g.research_enabled("gaming", cfg) is False
    cfg2 = {"default": True, "topics": {"gaming": False}}
    assert g.research_enabled("anything", cfg2) is True
    assert g.research_enabled("gaming", cfg2) is False


# --- deterministic (no-research) renderer ----------------------------------- #
def test_render_briefing():
    events = [
        {"title": "Go 1.18 ships generics", "one_liner": "Type params land.",
         "importance": 5, "theme": "Languages", "keywords": ["Go"],
         "sources": [{"label": "go.dev", "url": "https://go.dev"}]},
        {"title": "Minor patch", "one_liner": "Bugfix.", "importance": 2,
         "theme": "Languages", "keywords": [], "sources": []},
    ]
    body = g.render_briefing(events, "golang")
    assert body.startswith("## TL;DR")
    assert "🔥🔥🔥🔥🔥 [Go 1.18 ships generics](#go-118-ships-generics)" in body
    assert "### Go 1.18 ships generics" in body
    assert "Sources: [go.dev](https://go.dev)" in body
    # highest importance first in TL;DR
    assert body.index("Go 1.18") < body.index("Minor patch")


def test_render_briefing_empty_is_quiet_day():
    assert "Quiet day" in g.render_briefing([], "gaming")


# --- metrics (per-topic + global cost) -------------------------------------- #
class _Usage:
    def __init__(self, i, o, searches=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.server_tool_use = type("S", (), {"web_search_requests": searches})() if searches else None


def test_metrics_per_topic_and_global():
    m = g.Metrics()
    m.add("ai", "claude-haiku-4-5", _Usage(1000, 500))          # $0.0035
    m.add("ai", "claude-sonnet-5", _Usage(2000, 1000, searches=3))  # tokens + 3 searches
    m.add("gaming", "claude-haiku-4-5", _Usage(1000, 500))      # $0.0035
    rec = m.record()
    assert set(rec["by_topic"]) == {"ai", "gaming"}
    assert rec["web_searches"] == 3
    # gaming: haiku only, no searches
    assert abs(rec["by_topic"]["gaming"]["estimated_cost_usd"] - 0.0035) < 1e-6
    # ai should cost more than gaming (extra sonnet call + searches)
    assert rec["by_topic"]["ai"]["estimated_cost_usd"] > rec["by_topic"]["gaming"]["estimated_cost_usd"]
    # global = sum of topics
    assert abs(rec["estimated_cost_usd"]
               - (rec["by_topic"]["ai"]["estimated_cost_usd"]
                  + rec["by_topic"]["gaming"]["estimated_cost_usd"])) < 1e-6
