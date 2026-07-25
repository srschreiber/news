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


def test_attach_sources_dedupes_by_outlet():
    # two items from the SAME outlet (e.g. Google News feed) collapse to one
    items = [_item("http://a", None, source="Google News"),
             _item("http://b", None, source="Google News"),
             _item("http://c", None, source="The Register")]
    for i, it in enumerate(items):
        it["id"] = f"x{i}"
    events = [{"title": "E", "source_item_ids": ["x0", "x1", "x2", "missing"]}]
    out = g.attach_sources(events, items)
    labels = [s["label"] for s in out[0]["sources"]]
    assert labels == ["Google News", "The Register"]      # deduped by outlet
    assert all(s["origin"] == "rss" for s in out[0]["sources"])


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


# --- home-page top stories -------------------------------------------------- #
def test_top_stories_section():
    index = [
        {"date": "2026-07-25", "topic": "ai", "title": "Big", "summary": "desc",
         "importance": 5, "url": "news/ai/2026-07-25/#big"},
        {"date": "2026-07-25", "topic": "world", "title": "Med", "summary": "",
         "importance": 3, "url": "news/world/2026-07-25/#med"},
        {"date": "2026-07-24", "topic": "ai", "title": "Old", "summary": "x",
         "importance": 5, "url": "u3"},
    ]
    text = "\n".join(g._top_stories_section(index))
    assert "Top stories — 2026-07-25" in text     # only the latest day
    assert "Old" not in text
    assert "[Big](news/ai/2026-07-25/#big) — desc" in text  # description shown
    assert text.index("Big") < text.index("Med")   # sorted by importance
    assert g._top_stories_section([]) == []


def test_dedupe_cross_topic_collapses_same_story():
    recs = [
        {"title": "Anthropic launches Claude Opus 5 with efficiency improvements",
         "keywords": ["Anthropic", "Claude Opus 5"], "importance": 4},
        {"title": "Anthropic launches Claude Opus 5 AI model",
         "keywords": ["Anthropic", "Claude Opus 5"], "importance": 4},
        {"title": "Anthropic releases Claude Opus 5 at 50% lower cost than Fable",
         "keywords": ["Anthropic", "Claude Opus 5", "Fable"], "importance": 4},
        {"title": "EU fines Google 890 million euros for DMA violations",
         "keywords": ["Google", "EU", "DMA"], "importance": 4},
    ]
    kept = g._dedupe_cross_topic(recs)
    titles = [k["title"] for k in kept]
    assert len(kept) == 2                                  # 3 Opus 5 -> 1, plus Google
    assert sum("Opus 5" in t for t in titles) == 1
    assert any("Google" in t for t in titles)


# --- hybrid research: enrichment overlay + fallback --------------------------- #
def test_merge_enrichment_overlays_by_ref():
    events = [
        {"title": "A", "ref": "e0", "one_liner": "orig a", "importance": 5,
         "sources": [{"label": "RSS", "url": "http://a", "origin": "rss"}]},
        {"title": "B", "one_liner": "orig b", "importance": 3,
         "sources": [{"label": "RSS", "url": "http://b", "origin": "rss"}]},
    ]
    enriched = [{"ref": "e0", "summary": "researched a",
                 "sources": [{"label": "Web", "url": "http://a2"}]}]
    a, b = g.merge_enrichment(events, enriched)
    assert a["one_liner"] == "researched a" and a["researched"] is True
    assert {s["url"] for s in a["sources"]} == {"http://a", "http://a2"}  # RSS + web
    origins = {s["url"]: s["origin"] for s in a["sources"]}
    assert origins["http://a2"] == "research" and origins["http://a"] == "rss"
    assert b["one_liner"] == "orig b" and "researched" not in b          # no ref -> untouched
    assert b["sources"] == [{"label": "RSS", "url": "http://b", "origin": "rss"}]


def test_merge_enrichment_empty_keeps_rss():
    events = [{"title": "A", "one_liner": "orig", "sources": [{"label": "RSS", "url": "u"}]}]
    merged = g.merge_enrichment(events, [])              # research produced nothing
    assert merged[0]["one_liner"] == "orig" and "researched" not in merged[0]


# --- global clustering: topic assignment + research selection ---------------- #
def test_assign_topics_and_primary():
    items = [{"id": "ai-0", "topic": "ai"}, {"id": "gen-0", "topic": "general"},
             {"id": "ai-1", "topic": "ai"}]
    events = [{"title": "E", "source_item_ids": ["ai-0", "gen-0", "ai-1"]}]
    g.assign_topics(events, items)
    assert events[0]["topics"] == ["ai", "general"]     # sorted union of feeds
    assert events[0]["primary_topic"] == "ai"           # ai contributed 2 vs general 1


def test_select_research_dedupes_cross_topic():
    events = [
        {"title": "Opus 5", "importance": 5, "topics": ["ai", "anthropic", "general"]},
        {"title": "Game", "importance": 4, "topics": ["gaming"]},
        {"title": "Minor", "importance": 1, "topics": ["ai"]},
    ]
    sel = g.select_research(events, ["ai", "anthropic", "general", "gaming"])
    titles = [e["title"] for e in sel]
    assert titles.count("Opus 5") == 1                  # researched once despite 3 topics
    assert "Game" in titles
    assert "Minor" not in titles                        # importance 1 < threshold -> skipped


def test_select_research_skips_unimportant_topics():
    events = [
        {"title": "meh1", "importance": 2, "topics": ["python"]},
        {"title": "meh2", "importance": 3, "topics": ["python"]},
    ]
    assert g.select_research(events, ["python"]) == []   # no top event clears the bar


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
