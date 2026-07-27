# rss-news-generator — Design

**Date:** 2026-07-24
**Status:** Approved for planning

## Overview

A daily automated tech-news briefing. A GitHub Actions cron fetches configured
RSS feeds from big-name tech sources and runs a **two-stage** Claude pipeline:

1. **Cluster & score** — Claude reads all the day's RSS items and returns
   structured JSON: a list of distinct, **event-level** news items (dedup'd
   across feeds), each with an importance score and its source provenance.
2. **Research & write** — our code selects the top-K events by importance;
   Claude then researches only those (via `web_fetch` / `web_search`, and only
   when the RSS summary is insufficient) and writes a themed markdown briefing
   with a TL;DR at the top and a per-story importance meter.

The generated document is committed to the repo and published as a static site
via MkDocs on GitHub Pages.

The goal is a low-maintenance way to stay up to date on tech news. **Everything
is a git file** — feed list, prompts, generated docs, and config all live in
the repo.

## Goals

- One new briefing document per day, each with a TL;DR at the top.
- Broad tech-industry coverage from big-name sources.
- **Event-level** clustering — each item is a specific happening (a funding
  round, a release, a ruling), not a broad topic.
- Real depth via optional web research, not just headline restatement.
- Every claim traceable to a source (RSS and/or researched web page).
- Every story carries an importance score so readers can triage at a glance.
- **Code-enforced cost control:** only the top-K scored events get researched.
- Fully automated (daily cron) and hosted on GitHub Pages.
- Cheap, simple, and predictable — two sequential SDK calls, no agent framework.

## Non-goals

- No full-article scraping. RSS title + summary is the raw input; when depth is
  needed, Claude uses the `web_fetch` server tool on the article's own link
  (server-side, graceful on paywalls) — we write no scraper.
- No LangChain / LangGraph. Two sequential `anthropic` SDK calls in plain
  Python; there is no branching agent graph to orchestrate.
- No database or persistent state beyond git files.
- No per-user personalization or email/notification delivery.

## Architecture & Data Flow

```
GitHub Actions cron (daily)
  → generate.py

    STAGE 0 — fetch (local, no API)
      0. Read state.json → last_run cutoff + seen_links (first run: 24h fallback)
      1. Parse sources.yaml (url: feeds + query: Google News searches)
      2. Fetch each feed; keep items published since the cutoff AND whose link
         is not already in seen_links
         (dead/timed-out feeds skipped with a warning, not fatal)
      3. Cap to top N most-recent items per feed
      4. Build payload: {id, source, title, summary, link, published} per item

    STAGE 1 — cluster & score (Claude Haiku, NO tools, structured output)
      5. client.messages.create(
             model=CLUSTER_MODEL,   # claude-haiku-4-5 — cheap, fast, handles the bulk input
             output_config={format: EVENTS_SCHEMA},   # structured JSON
             ...cluster_prompt + item payload...)      # cached prefix
         → returns events[]: each an atomic event with score + source item ids

    SELECT (local)
      6. Sort events by importance; take top-K (TOP_K_TO_RESEARCH)

    STAGE 2 — research & write (Claude Sonnet, web_fetch + web_search, bounded loop)
      7. client.messages.create(
             model=WRITE_MODEL,   # claude-sonnet-5 — quality where it matters
             tools=[web_fetch, web_search],   # tool_choice: auto, max_uses caps
             ...write_prompt + top-K events (with their source links)...)
         run a bounded tool loop; handle stop_reason == "pause_turn"
      8. Write docs/news/YYYY-MM-DD.md

  → commit the new doc
  → mkdocs build
  → deploy to GitHub Pages
```

Stage 1 is cheap (no web tools, small structured output), so scoring the whole
day costs a fraction of a cent. The expensive part — research — runs only on
the top-K events *chosen in our code*, which is the primary cost lever.

## Components

### `sources.yaml`
List of sources, easy to edit. Two entry kinds, both resolved to RSS under the
hood (so both flow through the cheap Stage-1 clustering unchanged):

- **`url:`** — a direct RSS/Atom feed.
- **`query:`** — a Google News topic search. `generate.py` builds the Google
  News RSS search URL (`https://news.google.com/rss/search?q=<query>`) so you
  write the topic, not the URL. This gives per-topic / feed-less-site coverage
  without any non-RSS plumbing.

Default set (broad tech): TechCrunch, The Verge, Ars Technica, Wired, Engadget,
Hacker News (front-page RSS), The Register — plus room for Google News topic
queries.

Shape:
```yaml
sources:
  - name: TechCrunch
    url: https://techcrunch.com/feed/
  - name: The Verge
    url: https://www.theverge.com/rss/index.xml
  - name: "Google News — AI chips"
    query: "AI chips"          # → news.google.com/rss/search?q=AI+chips
  # ...
```

**Why RSS stays the backbone (not raw URLs to the LLM):** RSS is free and
structured — clean title/summary/link/date per item is exactly what cheap Haiku
needs to cluster and score the whole day. Handing raw URLs to the LLM to
discover what happened would move bulk ingestion into the expensive
tool-using stage, against the cost guardrails. RSS does the cheap firehose;
the LLM's web tools do targeted depth on the top-K. True web-discovery beyond
feeds is a deferred add-on.

### `prompts/cluster.md` (Stage 1 prompt)
Git-tracked, human-editable. Instructs Claude to:
- Read all RSS items and group those covering the **same specific event** into
  one cluster (dedup across feeds — the same funding round reported by three
  outlets is one event).
- Keep clusters **event-level and granular**, not broad topics. Good: "Company
  ABC raised $40M Series B", "HTTP QUERY method advances to Proposed Standard",
  "EU fines Vendor X €200M". Bad: "Security", "Funding", "Web standards",
  "AI" — those are themes, not events.
- Assign each event an **importance score** (1–5) — breadth of impact,
  consequence, novelty. Rubric spans "nobody cares" (1) → "this is
  important" (5).
- Record, for each event, which source items it was built from (their ids →
  giving us the feed names + article links).
- Return **structured JSON only** (no prose), matching `EVENTS_SCHEMA`.

Stage-1 JSON schema (`EVENTS_SCHEMA`):
```json
{
  "events": [
    {
      "title": "Company ABC raised $40M Series B",
      "one_liner": "Led by XYZ Capital; funds international expansion.",
      "importance": 4,
      "theme": "Funding",                 // for later grouping in the doc
      "keywords": ["Company ABC", "Series B", "XYZ Capital", "funding"],
      "source_item_ids": ["tc-12", "verge-3"]   // ids from the Stage-0 payload
    }
  ]
}
```
`source_item_ids` map back to the Stage-0 items, so our code attaches the real
feed names + article URLs — the model never invents links.

### `prompts/write.md` (Stage 2 prompt)
Git-tracked, human-editable. Given the top-K events (each with its source
links), instructs Claude to:
- Decide **per event** whether web research adds value. If the RSS summary is
  enough, **skip all tools**. When more depth is warranted, prefer `web_fetch`
  on the event's own article link first; fall back to `web_search` only if the
  fetch is blocked/paywalled or broader context is needed. Do not browse
  exhaustively.
- Group events into theme sections (the `theme` field, `##`), with each event
  as an `###` heading under its theme.
- Put a **TL;DR** (3–6 one-line highlights) at the very top, highest-importance
  events first, each bullet an anchor link to that event's `###` section.
- Render each event with its importance meter and a mandatory **Sources** trail.
- **Cite sources always:** every story links to at least one originating RSS
  article; any fact added from web research cites the web page it came from. No
  uncited enrichment.

### `generate.py`
- Stage 0: parses `sources.yaml`, fetches feeds (`feedparser`), filters to last
  24h, caps per-feed items, builds the id'd payload.
- Stage 1: calls Claude with `cluster.md` + structured output; parses `events[]`.
- Select: sorts by importance, takes `TOP_K_TO_RESEARCH`.
- Stage 2: attaches source links to the selected events, calls Claude with
  `write.md` + `web_fetch`/`web_search`; runs a bounded loop handling
  `pause_turn` and enforcing the guardrails below.
- Writes `docs/news/YYYY-MM-DD.md`.
- Structured so Stage 0 (feed parsing) and doc-assembly are unit-testable with
  fixtures (no network). A `--dry-run` flag runs Stage 0 + Stage 1 and prints
  the selected events without spending Stage-2 research tokens.

### Search & keywords
- Stage 1 extracts per-event `keywords` (normalized entities/topics).
- Each generated doc carries front-matter `tags` = event keywords + period tags
  (`YYYY`, `YYYY-MM`), feeding Material's **tags** plugin for faceted filtering.
- `generate.py` maintains **`docs/search-index.json`** — one record per event
  (`{date, title, theme, importance, keywords, url, sources}`), appended each
  run; it is the searchable corpus.
- **`docs/search.md`** hosts a static, client-side search page (`docs/assets/
  search.js`) that loads `search-index.json` and provides keyword-weighted
  relevance (keywords weighted above title/theme — a BM25-lite), a date-range
  filter, and importance sort. Works on GitHub Pages, no backend.
- Material's built-in Lunr search stays enabled as a zero-config fallback.
- Deferred: swap Lunr/custom for Pagefind if stronger full-text search is later
  wanted.

### `docs/` + `mkdocs.yml`
- MkDocs (Material theme, `search` + `tags` plugins). `docs/index.md` lists
  recent briefings; `docs/news/YYYY-MM-DD.md` is one generated briefing per day;
  rollups under `docs/weekly|monthly|yearly/`.

### `.github/workflows/daily.yml`
- Triggers: `schedule` cron (default **13:00 UTC**) + `workflow_dispatch`.
- `ANTHROPIC_API_KEY` from GitHub Actions secrets.
- Steps: checkout → set up Python → install deps → run `generate.py` → commit
  the new doc → `mkdocs build` → deploy to GitHub Pages.

## Output Document Shape

**One markdown file per day** (`docs/news/YYYY-MM-DD.md`). The TL;DR at the top
links to each event's full section further down the same page (intra-page
anchors), so you can skim the TL;DR and jump to details+sources in one click.
MkDocs Material also auto-renders a right-sidebar table of contents from the
headings, giving free navigation without splitting into multiple files.

```markdown
# Tech News — 2026-07-24

## TL;DR
- <span class="imp imp-5" title="Importance 5/5" aria-label="Importance 5 of 5"><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i></span> [Go 1.18 ships generics](#go-118-ships-generics)
- <span class="imp imp-4" title="Importance 4/5" aria-label="Importance 4 of 5"><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i><i></i></span> [Company ABC raised $40M Series B led by XYZ](#company-abc-raised-40m-series-b)
- ...

## Programming Languages

### Go 1.18 ships generics
<span class="imp imp-5" title="Importance 5/5" aria-label="Importance 5 of 5"><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i></span> Type parameters land in the stable release after years of proposals.
<fuller detail from research if any>.
Sources: [Ars Technica](url), [The Register](url), [go.dev release notes](url)

## Funding

### Company ABC raised $40M Series B
<span class="imp imp-4" title="Importance 4/5" aria-label="Importance 4 of 5"><i class="on"></i><i class="on"></i><i class="on"></i><i class="on"></i><i></i></span> Led by XYZ Capital; funds international expansion.
Sources: [TechCrunch](url), [The Verge](url)
```

Each event is an `###` under its theme `##` (so both the TL;DR anchors and the
Material ToC resolve to it). Each carries a 5-slot importance meter (`<span class="imp imp-1" title="Importance 1/5" aria-label="Importance 1 of 5"><i class="on"></i><i></i><i></i><i></i><i></i></span>` filled,
`◯` empty) mapping to the 1–5 score — clean labels: 1 = meh, 2 = notable,
3 = big deal, 4 = major, 5 = huge — and ends with a "Sources:" trail combining
RSS article links and any researched web links.

**Deferred alternative (not building now):** a per-day *folder* with a primary
TL;DR page fanning out to one page per event. Better if individual events grow
long enough to deserve their own URLs; it's a contained change later. For
≤10 events/day the single-page form is more skimmable and simpler to generate.

## Rollups (weekly / monthly / yearly)

Cascading summaries, where **each level summarizes the level below — never the
raw feeds again**:

| Mode | Reads | Writes |
|---|---|---|
| `daily` | RSS sources (the two-stage pipeline above) | `docs/news/YYYY-MM-DD.md` |
| `weekly` | the 7 daily docs in the ISO week | `docs/weekly/YYYY-Www.md` |
| `monthly` | the weekly docs in the month | `docs/monthly/YYYY-MM.md` |
| `yearly` | the 12 monthly docs in the year | `docs/yearly/YYYY.md` |

A rollup is a **single Sonnet call, web tools off** (sources are already cited
one level down): it reads the child markdown docs, re-clusters events across the
period, surfaces the highest-importance ones, notes trends, and links back to
the child docs. Importance scores propagate upward for prioritization; links
cascade down (yearly → monthly → weekly → daily → original article), so every
claim stays traceable to its origin without re-researching. Shared prompt
`prompts/rollup.md`, parameterized by period label.

`generate.py <mode>` selects behavior (`daily` default). Rollups degrade
gracefully: if a period has no child docs yet, write a short "nothing to roll
up" doc rather than failing.

## Cost & Token Guardrails

Layered, independent caps so a confused or looping browse cannot run away with
tokens or cost. All values live as named constants at the top of `generate.py`.

- **`TOP_K_TO_RESEARCH`** — the primary lever. Only the top-K scored events
  (default **10**) reach Stage 2, so research spend is bounded *in our code*,
  not left to the model.
- **`web_search` `max_uses`** — hard cap on searches per run (default **15**),
  enforced by the tool definition.
- **`web_fetch` `max_uses`** — hard cap on article fetches per run
  (default **20**) + `max_content_tokens` per page so one huge page can't flood
  context.
- **Loop-iteration cap** — Stage-2 tool loop stops after **N** continuations
  (default **8**), incl. `pause_turn` resumes; take best-so-far and warn rather
  than looping forever.
- **`max_tokens`** — bounded output ceiling (streaming).
- **Input bounding** — cap items per feed (default **top 40** most recent)
  before Stage 1, so a noisy day can't balloon the payload.
- **Prompt caching** — each stage's prompt + payload prefix is cached, so
  repeated context (Stage-2 tool-loop turns especially) bills at cache-read
  rates (~0.1×).
- **Domain hygiene (optional)** — `web_fetch`/`web_search` can be scoped with
  `allowed_domains`/`blocked_domains`; open by default, available as a lever.

Hitting any cap logs clearly (visible in the Actions run) and still writes
whatever briefing was produced — a capped run degrades, it doesn't fail.

## Error Handling & Edge Cases

- **Feed fetch failure:** skip that feed with a logged warning; continue with
  the feeds that succeeded. One bad feed never fails the run.
- **No items in 24h:** write a short "quiet day" document instead of erroring.
- **Stage 1 returns no events / malformed JSON:** retry once; if still bad, log
  and write a minimal doc from the raw RSS items (no research).
- **Long research turn:** handle `stop_reason == "pause_turn"` by re-sending to
  resume; the loop-iteration cap bounds it.
- **API/auth failure:** the Actions job fails loudly (visible in the run log);
  no partial/empty doc is committed.

## Decisions

| Decision | Choice |
|---|---|
| LLM client | Direct `anthropic` Python SDK (no LangChain/LangGraph) |
| Models | Stage 1 `claude-haiku-4-5` (cluster/score, bulk input); Stage 2 `claude-sonnet-5` (research/write). Per-stage constants. |
| Pipeline | Two-stage: cluster+score (no tools) → select top-K → research+write |
| Clustering unit | Atomic **event** (funding round, release, ruling), not broad topic |
| Feed scope | Broad tech (7 big-name sources) |
| Input content | RSS title + summary (no full-article scraping) |
| Research | web_fetch (article link) + web_search server tools, discretionary per event |
| Dedup | In Stage 1 — events clustered across feeds; provenance recorded |
| Doc layout | TL;DR + theme sections |
| Importance score | 1–5 per event, rendered as a <span class="imp imp-1" title="Importance 1/5" aria-label="Importance 1 of 5"><i class="on"></i><i></i><i></i><i></i><i></i></span>/◯ meter (meh → huge) |
| Citations | Mandatory for every story; researched facts cite their page |
| Cost guardrails | TOP_K_TO_RESEARCH + max_uses caps + loop cap + max_tokens + per-feed cap + caching |
| Site | MkDocs (Material) on GitHub Pages |
| Schedule | Daily cron at 13:00 UTC + manual dispatch |
| Secrets | `ANTHROPIC_API_KEY` in GitHub Actions secrets |

## Testing

- Unit tests for Stage 0 feed parsing and for doc assembly using fixture RSS
  data (no network, no API).
- `--dry-run` runs Stage 0 + Stage 1 and prints the clustered/scored events and
  the top-K selection, without spending Stage-2 research tokens — cheap way to
  eyeball clustering granularity and scores.
