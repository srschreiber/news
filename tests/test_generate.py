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


def test_payload_items_drops_link_and_private_fields():
    items = [{"id": "s-0", "source": "The Reg", "topic": "ai", "title": "T",
              "summary": "S", "link": "https://news.google.com/rss/…verylong",
              "published": "2026-07-25", "_published_dt": object()}]
    out = g.payload_items(items)
    assert out == [{"id": "s-0", "source": "The Reg", "topic": "ai",
                    "title": "T", "summary": "S"}]
    assert "link" not in out[0] and "published" not in out[0]


def test_group_by_topic():
    items = [_item("a", None, topic="gaming"), _item("b", None, topic="ai"), _item("c", None, topic="gaming")]
    groups = g.group_by_topic(items)
    assert set(groups) == {"gaming", "ai"} and len(groups["gaming"]) == 2


# --- rendering helpers ------------------------------------------------------ #
def test_meter():
    m = g.meter(4)
    assert m.count('<i class="on">') == 4      # 4 filled bars
    assert m.count("<i") == 5                   # 5 bars total
    assert 'aria-label="Importance 4 of 5"' in m
    assert g.meter(0).count('<i class="on">') == 1   # clamps up to 1
    assert g.meter(9).count('<i class="on">') == 5   # clamps down to 5


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


def test_already_ran_today():
    assert g._already_ran_today({"last_run": "2026-07-28T13:31:00+00:00"}, "2026-07-28")
    assert not g._already_ran_today({"last_run": "2026-07-27T13:31:00+00:00"}, "2026-07-28")
    assert not g._already_ran_today({}, "2026-07-28")                 # no prior run
    assert not g._already_ran_today({"last_run": "garbage"}, "2026-07-28")  # unparseable


def test_compute_cutoff_clamps_to_start_of_day_on_same_day_rerun():
    # A re-run later the same day must re-cover the WHOLE day, not just since the
    # earlier run — otherwise the re-run sees an empty slice and clobbers content.
    now = dt.datetime(2026, 7, 24, 18, 0, tzinfo=UTC)
    same_day = g.compute_cutoff({"last_run": "2026-07-24T09:00:00+00:00"}, now)
    assert same_day == dt.datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def test_filter_keeps_items_seen_today_but_drops_prior_days():
    # Same-day regenerate: an item first seen today is re-processed; one seen
    # yesterday stays deduped.
    now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    cutoff = now.replace(hour=0, minute=0)
    items = [
        _item("seen-today", now - dt.timedelta(hours=1)),
        _item("seen-yesterday", now - dt.timedelta(hours=2)),
    ]
    seen = {"seen-today": "2026-07-24", "seen-yesterday": "2026-07-23"}
    kept = {it["link"] for it in g.filter_and_cap(items, cutoff, seen, today="2026-07-24")}
    assert kept == {"seen-today"}
    # Without `today`, both are treated as seen and dropped (back-compat).
    kept_legacy = {it["link"] for it in g.filter_and_cap(items, cutoff, seen)}
    assert kept_legacy == set()


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
    assert "[Go 1.18 ships generics](#go-118-ships-generics)" in body
    assert '<span class="imp imp-' in body   # custom signal-bar meter
    assert "### " in body and "Go 1.18 ships generics" in body  # bars now part of heading
    assert "Sources: [go.dev](https://go.dev)" in body
    # highest importance first in TL;DR
    assert body.index("Go 1.18") < body.index("Minor patch")


def test_render_briefing_empty_is_quiet_day():
    assert "Quiet day" in g.render_briefing([], "gaming")


def test_parse_wotd():
    summary = (
        '<p><strong><font>Merriam-Webster\'s Word of the Day for July 27, 2026 is:'
        '</font></strong></p>'
        '<p><strong>parochial</strong> &#x2022; \\puh-ROH-kee-ul\\&nbsp; &#x2022; '
        '<em>adjective</em><br />'
        '<p><em>Parochial</em> is a formal word used to describe something limited '
        'in range or scope.</p>'
        '<p>// It lifts them out of a <em>parochial</em> mindset.</p>'
        '<p><a href="https://m-w.com/dictionary/parochial">See the entry &gt;</a></p>'
        '</p>'
        '<p><strong>Examples:</strong><br /><p>Some long editorial quote.</p></p>'
    )
    w = g._parse_wotd("parochial", summary, "limited in range or scope")
    assert w["word"] == "parochial"
    assert w["part_of_speech"] == "adjective"
    assert "limited in range or scope" in w["definition"]
    assert w["example"].startswith("It lifts them out of a")
    assert "See the entry" not in w["definition"]  # link stripped


def test_parse_wotd_falls_back_to_shortdef():
    w = g._parse_wotd("cromulent", "<p>no structure here</p>", "acceptable or fine")
    assert w["definition"] == "acceptable or fine"


def test_factsite_snippet():
    raw = ('<html><body><nav>menu</nav>'
           '<h2>Fact of the Day</h2><script>var x=1;</script>'
           '<div class="fotd">Honey never spoils.</div>'
           '<footer>...</footer></body></html>')
    s = g._factsite_snippet(raw)
    assert "Honey never spoils." in s
    assert "<" not in s and "var x" not in s   # tags + scripts stripped
    assert g._factsite_snippet("<html>no widget here</html>") == ""


def test_parse_fact():
    e = {"year": 1976, "text": "Apple Computer was founded.",
         "pages": [{"content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Apple"}}}]}
    f = g._parse_fact(e)
    assert f == {"year": 1976, "text": "Apple Computer was founded.",
                 "link": "https://en.wikipedia.org/wiki/Apple"}
    assert g._parse_fact({"year": None, "text": "x"}) is None
    # missing pages -> empty link, still valid
    assert g._parse_fact({"year": 1969, "text": "Moon landing."})["link"] == ""


def test_topic_display():
    assert g.topic_display("ai") == "AI"
    assert g.topic_display("gpt") == "GPT"
    assert g.topic_display("email-security") == "Email Security"
    assert g.topic_display("markets") == "Markets"


def test_daily_link_pluralizes_and_counts():
    counts = {("ai", "2026-07-25"): 1, ("ai", "2026-07-24"): 3}
    assert g._daily_link("ai", "2026-07-25", counts) == \
        "[2026-07-25 (1 event)](news/ai/2026-07-25.md)"
    assert g._daily_link("ai", "2026-07-24", counts) == \
        "[2026-07-24 (3 events)](news/ai/2026-07-24.md)"
    # unknown -> 0 events
    assert "(0 events)" in g._daily_link("ai", "2026-07-01", counts)


def test_event_counts_from_index():
    index = [
        {"date": "2026-07-25", "topics": ["ai", "general"]},
        {"date": "2026-07-25", "topics": ["ai"]},
        {"date": "2026-07-24", "topics": ["general"]},
    ]
    counts = g._event_counts(index)
    assert counts[("ai", "2026-07-25")] == 2
    assert counts[("general", "2026-07-25")] == 1
    assert counts[("general", "2026-07-24")] == 1


def test_render_briefing_renders_takeaways_as_card():
    events = [
        {"title": "Opus 5 launches", "one_liner": "Near-frontier at half the price.",
         "importance": 5, "theme": "AI", "keywords": [],
         "takeaways": ["$5/M input, $25/M output", "Tops Frontier-Bench v0.1"],
         "sources": [{"label": "Anthropic", "url": "https://a", "origin": "research"}]},
    ]
    body = g.render_briefing(events, "ai")
    assert '<ul class="takeaways">' in body
    assert "<li>$5/M input, $25/M output</li>" in body
    assert "<li>Tops Frontier-Bench v0.1</li>" in body
    # the card sits between the summary line and the Sources line
    assert body.index("Near-frontier") < body.index("takeaways") < body.index("Sources:")


def test_render_briefing_takeaways_html_escaped():
    events = [
        {"title": "T", "one_liner": "x", "importance": 3, "theme": "T",
         "keywords": [], "takeaways": ["a < b & c"], "sources": []},
    ]
    body = g.render_briefing(events, "ai")
    assert "<li>a &lt; b &amp; c</li>" in body


def test_render_briefing_no_takeaways_still_ok():
    events = [
        {"title": "Small patch", "one_liner": "Bugfix shipped.", "importance": 2,
         "theme": "Languages", "keywords": [], "takeaways": [], "sources": []},
    ]
    body = g.render_briefing(events, "golang")
    assert "Bugfix shipped." in body
    assert "takeaways" not in body.split("## Languages")[1]  # no empty card


# --- home-page top stories -------------------------------------------------- #
_CFG = {"default": True, "topics": {}}


def test_period_lists_windows_and_dedup():
    records = [
        {"date": "2026-07-25", "topics": ["ai"], "title": "Big", "importance": 5,
         "keywords": ["Big", "Story", "Today"], "url": "news/ai/2026-07-25/#big"},
        {"date": "2026-07-25", "topics": ["world"], "title": "Med", "importance": 3,
         "keywords": [], "url": "news/world/2026-07-25/#med"},
        {"date": "2026-07-20", "topics": ["ai"], "title": "Old", "importance": 5,
         "keywords": [], "url": "news/ai/2026-07-20/#old"},
        {"date": "2026-06-01", "topics": ["ai"], "title": "Ancient", "importance": 5,
         "keywords": [], "url": "news/ai/2026-06-01/#ancient"},
    ]
    data = g._period_lists(records)
    assert [r["title"] for r in data["daily"]] == ["Big", "Med"]  # only the latest day
    assert {r["title"] for r in data["weekly"]} == {"Big", "Med", "Old"}
    assert "Ancient" not in {r["title"] for r in data["monthly"]}  # outside the 30-day window
    assert g._period_lists([]) == {"daily": [], "weekly": [], "monthly": []}


def test_period_lists_dedupes_same_story():
    records = [
        {"date": "2026-07-25", "topics": ["ai"], "title": "Anthropic launches Claude Opus 5",
         "importance": 5, "keywords": ["Anthropic", "Claude Opus 5"], "url": "u1"},
        {"date": "2026-07-25", "topics": ["tech"], "title": "Anthropic ships Claude Opus 5 model",
         "importance": 4, "keywords": ["Anthropic", "Claude Opus 5"], "url": "u2"},
    ]
    data = g._period_lists(records)
    assert len(data["daily"]) == 1
    assert data["daily"][0]["importance"] == 5   # highest-importance survivor kept


def test_period_view_block_embeds_json_and_script():
    records = [
        {"date": "2026-07-25", "topics": ["ai"], "title": "Big", "summary": "desc",
         "importance": 5, "keywords": [], "url": "news/ai/2026-07-25/#big",
         "sources": [], "takeaways": []},
    ]
    text = "\n".join(g._period_view_block(records, prefix="../../"))
    assert '<div id="period-view" class="period-view" data-prefix="../../">' in text
    assert '<script id="period-view-data" type="application/json">' in text
    assert '"title": "Big"' in text
    # raw directory-URL form (no .md) — this is a runtime <a href>, not a
    # markdown-syntax link, so MkDocs's build-time .md rewriter never sees it
    assert '"url": "news/ai/2026-07-25/#big"' in text
    assert '<script src="../../assets/period-view.js" defer></script>' in text
    assert g._period_view_block([]) == ["_No stories yet._", ""]


def test_period_view_block_includes_feed_titles():
    records = [
        {"date": "2026-07-25", "topics": ["ai"], "title": "Big", "importance": 5,
         "keywords": [], "url": "u1", "feeds": ["technology"]},
    ]
    feeds = {"technology": {"title": "Technology"}}
    text = "\n".join(g._period_view_block(records, feeds=feeds))
    assert '"feeds": ["Technology"]' in text


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
    items = [{"id": "ai-0", "topic": "ai"}, {"id": "gen-0", "topic": "tech"},
             {"id": "ai-1", "topic": "ai"}]
    events = [{"title": "E", "source_item_ids": ["ai-0", "gen-0", "ai-1"]}]
    g.assign_topics(events, items, {"ai": "technology", "tech": "technology"})
    assert events[0]["topics"] == ["ai", "tech"]        # sorted union of topics
    assert events[0]["primary_topic"] == "ai"           # ai contributed 2 vs tech 1
    assert events[0]["feeds"] == ["technology"]         # feed derived from topics
    assert events[0]["primary_feed"] == "technology"


def test_assign_topics_cross_lists_via_also_includes():
    items = [{"id": "cr-0", "topic": "climate-resilience"}]
    events = [{"title": "E", "source_item_ids": ["cr-0"]}]
    topic_feed = {"climate-resilience": "climate-resilience"}
    feeds = {
        "climate-resilience": {"title": "Climate", "also_includes": []},
        "science": {"title": "Science", "also_includes": ["climate-resilience"]},
    }
    g.assign_topics(events, items, topic_feed, feeds)
    assert events[0]["feeds"] == ["climate-resilience", "science"]  # cross-listed on Science
    assert events[0]["primary_feed"] == "climate-resilience"


def test_load_feeds_maps_topics_and_synthesizes_other(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "feeds:\n"
        "  technology: {title: Technology, research_budget: 6, topics: [ai, tech]}\n"
        "  world: {title: World, research_budget: 2, topics: [world]}\n"
    )
    feeds, tf = g.load_feeds(["ai", "tech", "world", "orphan"], path=cfg)
    assert tf == {"ai": "technology", "tech": "technology", "world": "world",
                  "orphan": g.DEFAULT_FEED}                 # orphan -> synthesized feed
    assert feeds["technology"]["research_budget"] == 6
    assert "orphan" in feeds[g.DEFAULT_FEED]["topics"]


def test_select_research_dedupes_cross_topic():
    events = [
        {"title": "Opus 5", "importance": 5, "topics": ["ai", "anthropic", "tech"]},
        {"title": "Game", "importance": 4, "topics": ["gaming"]},
        {"title": "Minor", "importance": 1, "topics": ["ai"]},
    ]
    feeds = {"technology": {"title": "T", "research_budget": 6,
                            "topics": ["ai", "anthropic", "tech", "gaming"]}}
    sel = g.select_research(events, feeds, _CFG)
    titles = [e["title"] for e in sel]
    assert titles.count("Opus 5") == 1                  # researched once despite 3 topics
    assert "Game" in titles
    assert "Minor" not in titles                        # importance 1 < threshold -> skipped


def test_select_research_skips_unimportant_topics():
    events = [
        {"title": "meh1", "importance": 1, "topics": ["python"]},
        {"title": "meh2", "importance": 2, "topics": ["python"]},
    ]
    feeds = {"technology": {"title": "T", "research_budget": 6, "topics": ["python"]}}
    assert g.select_research(events, feeds, _CFG) == []   # no top event clears the bar


def test_select_research_respects_per_feed_budget():
    events = [
        {"title": "T1", "importance": 5, "topics": ["ai"]},
        {"title": "T2", "importance": 5, "topics": ["tech"]},
        {"title": "W1", "importance": 5, "topics": ["world"]},
    ]
    feeds = {
        "technology": {"title": "T", "research_budget": 1, "topics": ["ai", "tech"]},
        "world": {"title": "W", "research_budget": 5, "topics": ["world"]},
    }
    sel = g.select_research(events, feeds, _CFG)
    titles = {e["title"] for e in sel}
    assert len(titles & {"T1", "T2"}) == 1              # tech feed budget=1 -> only one
    assert "W1" in titles                                # world feed has room
    # no_research short-circuits to nothing
    assert g.select_research(events, feeds, _CFG, no_research=True) == []


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
