# Sam's News — self-hosted daily tech-news briefings

A GitHub Actions cron that turns RSS + Google News into a skimmable, searchable
tech-news site — clustered, summarized, and cited by Claude — for **~$0.20/day**.
No server, no database: everything (feeds, prompts, generated docs, run state,
and per-run cost metrics) is a git file.

**▶ Live demo:** https://srschreiber.github.io/news/

> _If this is useful, a ⭐ on the repo is appreciated._

<!-- Replace with a real screenshot of the site once deployed: -->
<!-- ![Screenshot of the briefing site](docs/assets/screenshot.png) -->

## Why

Reading a dozen feeds every morning is noise. This distills them into per-topic
briefings with a TL;DR, an importance meter, key-fact bullets, and real source
citations — plus weekly/monthly/yearly rollups and keyword search — so you skim
one page instead of scrolling twenty.

## Features

- **Global cross-topic clustering** — a story spanning several topics is
  de-duplicated into one event and researched **once**, then reused everywhere.
- **Importance scoring** — each event gets a 1–5 🔥 meter; only important
  stories get web research (configurable per topic).
- **Cited sources** — every story links its real outlets with RSS/Research
  badges; summaries stay short, with skimmable key-fact bullets. Depth is in the
  links.
- **Rollups** — weekly → monthly → yearly digests cascade automatically.
- **Search + archive + tags** — client-side keyword search with date/topic
  filters, a full month-by-month archive, and auto-tagging.
- **Cost-transparent** — a per-run cost + token breakdown is committed to
  `metrics.json` every run.

## How it works

Everything runs through one endpoint (the Anthropic Messages API — direct SDK,
no framework). A daily run is three stages:

1. **Cluster & score** (`claude-haiku-4-5`) — one global pass over each run's
   *new* RSS items only (hourly incremental runs stay cheap) returns
   structured events: de-duplicated across feeds, each with an importance
   score, theme, and keywords.
2. **Read** (`claude-haiku-4-5`) — Python calls [Serper.dev](https://serper.dev)
   (Google News, $1/1k queries) and fetches pages directly (urllib, no LLM
   tool loop); one Haiku call per event turns the pre-fetched content into a
   dense factual extract. A topic with zero RSS events gets one fallback
   search per day before it's written up as quiet. Known RSS source URLs are
   fetched first.
3. **Polish** (`claude-sonnet-5`) — one batched call turns all the small
   extracts into final summaries + key-fact bullets, prioritizing fidelity to
   the source's precise wording (a "response rate" never becomes a "success
   rate"). Never sees raw pages.

**Story updates merge instead of duplicating.** When a new item's headline is
close enough to something already published today (embedding similarity via
[Voyage AI](https://voyageai.com), validated by a cheap Haiku classification —
falls back to keyword overlap if `VOYAGE_API_KEY` isn't set), it updates the
existing story in place rather than appearing as a second one: importance and
keywords refresh immediately, and it's queued for re-research so takeaways
catch up to the new facts. A caused-by-but-distinct follow-on (an aftershock,
a lawsuit filed over an incident) gets its own story instead, cross-linked
from the original.

Rollups are a single Haiku call synthesizing the level below, per topic. All
markdown is rendered deterministically in code — the models return structured
data, never free-form docs.

## Cost

A real-news-day run lands around **$0.05–0.20** depending on hourly volume,
and the full breakdown is committed each run to `metrics.json`. Cluster and
read stay on Haiku ($1/$5 per 1M); polish runs on Sonnet ($3/$15 per 1M) for
better fact-fidelity, but only once per batched run. Serper.dev Google News
search costs $1/1k queries (vs $10/1k for Anthropic's built-in search). Page
reads are capped at 1000 tokens each — enough for a news article lede.

## Layout

```
sources.yaml            # feeds + Google News queries + research/editorial config
prompts/                # cluster.md, read.md, write.md, rollup.md (edit freely)
generate.py             # the pipeline (daily | weekly | monthly | yearly)
state.json              # last_run + recently-seen links (dedup across runs)
metrics.json            # per-run cost + token breakdown (newest first)
docs/
  index.md  archive.md  search.md  tags.md
  news/<topic>/YYYY-MM-DD.md
  weekly|monthly|yearly/<topic>/...
  search-index.json     # per-event index for keyword search
overrides/              # MkDocs Material theme tweaks
mkdocs.yml
.github/workflows/
  pipeline.yml          # the cron: generate + publish (needs the API key)
  publish.yml           # build + deploy the site from committed docs (no LLM)
```

## Configure sources

Edit `sources.yaml`. Each source has a `name`, one of `url:` (direct RSS/Atom)
or `query:` (Google News search — the URL is built for you), and a `topic:`
(default `general`). Each topic gets its own daily file and rollups.

```yaml
# Turn web research off for niche/low-value topics to save cost:
research:
  default: true
  topics:
    golang: false
    markets: false

# Optional house-style instructions applied by the models (not find/replace):
custom_instructions: |
  Prefer plain language over hype.

sources:
  - name: The Verge
    url: https://www.theverge.com/rss/index.xml
    topic: general
  - name: "Google News — gaming"
    query: "gaming news"          # keep queries short — long AND-queries skew stale
    topic: gaming
```

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export SERPER_DEV_API_KEY=...       # serper.dev — Google News search
export VOYAGE_API_KEY=...           # voyageai.com — embeddings for story-update
                                     # matching (optional: falls back to a
                                     # keyword-overlap heuristic if unset)

python generate.py --dry-run     # cluster + score, print top events (no research, no writes)
python generate.py               # full daily run
python generate.py weekly        # weekly rollup (also: monthly, yearly)
python generate.py --no-research # daily run with web research forced off
mkdocs serve                     # preview the site at localhost:8000
```

## Deploy (GitHub)

1. Push to a GitHub repo.
2. Add repo secrets **`ANTHROPIC_API_KEY`**, **`SERPER_DEV_API_KEY`**, and
   optionally **`VOYAGE_API_KEY`**.
3. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
4. `pipeline.yml` runs on cron (daily/weekly/monthly/yearly) and via **Run
   workflow**; it generates docs and deploys. `publish.yml` re-deploys the site
   on any docs/theme change **without an LLM run** — so UI tweaks cost nothing.

## Cost controls

All in `generate.py` constants, and every cap degrades gracefully (a hit never
fails the run):

- `MIN_RESEARCH_IMPORTANCE` — only research events at least this important.
- `RESEARCH_BUDGET_PER_TOPIC` / `GLOBAL_SEARCH_SAFETY` — every research-enabled
  subfeed gets its own guaranteed budget, plus a run-wide safety cap.
- `WEB_FETCHES_PER_EVENT` — hard per-event page-fetch cap.
- `WEB_FETCH_MAX_CONTENT_TOKENS` — per-page ingest cap (lands on cheap Haiku).
- `MAX_ITEMS_PER_FEED`, `STAGE2_MAX_TOKENS`, `STAGE2_EFFORT` — input/output trims.
- `MERGE_COS_THRESHOLD` — embedding similarity floor before a story-update
  merge is even considered (then validated by a cheap Haiku call).

## Tests

```bash
pip install -r requirements.txt
pytest
```

Covers the pure (no-network, no-API) logic: parsing, filtering, dedup/cap,
clustering-payload trimming, rendering (meters, badges, takeaways), index/archive
building, and state cutoff/pruning.
