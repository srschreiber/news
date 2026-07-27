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

1. **Cluster & score** (`claude-haiku-4-5`) — one global pass over all feeds
   returns structured events: de-duplicated across feeds, each with an
   importance score, theme, and keywords. Cheap; handles the bulk input.
2. **Read** (`claude-haiku-4-5`) — for each important event, Haiku runs
   `web_search` + `web_fetch` and returns a small, dense factual extract. The
   expensive page-reading lands on the cheap model.
3. **Polish** (`claude-sonnet-5`) — one batched call turns all the small
   extracts into final summaries + key-fact bullets. Sonnet never sees raw
   pages, so its share of the cost is tiny.

Rollups are a single Sonnet call synthesizing the level below, per topic. All
markdown is rendered deterministically in code — the models return structured
data, never free-form docs.

## Cost

A real-news-day run lands around **$0.20–0.35**, and the breakdown is committed
each run. The two-stage split is the trick: Haiku ($1/$5 per 1M) does the
clustering and page-reading (where the tokens are); Sonnet ($3/$15) only stitches
a few thousand tokens of extracts. Server-side web search is a flat ~$0.01/query.

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

python generate.py --dry-run     # cluster + score, print top events (no research, no writes)
python generate.py               # full daily run
python generate.py weekly        # weekly rollup (also: monthly, yearly)
python generate.py --no-research # daily run with web research forced off
mkdocs serve                     # preview the site at localhost:8000
```

## Deploy (GitHub)

1. Push to a GitHub repo.
2. Add repo secret **`ANTHROPIC_API_KEY`**.
3. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
4. `pipeline.yml` runs on cron (daily/weekly/monthly/yearly) and via **Run
   workflow**; it generates docs and deploys. `publish.yml` re-deploys the site
   on any docs/theme change **without an LLM run** — so UI tweaks cost nothing.

## Cost controls

All in `generate.py` constants, and every cap degrades gracefully (a hit never
fails the run):

- `MIN_RESEARCH_IMPORTANCE` — only research events at least this important.
- `RESEARCH_PER_TOPIC` / `MAX_RESEARCHED_EVENTS` — how many stories get web
  research (breadth-first, with a run-wide safety cap).
- `WEB_SEARCHES_PER_EVENT` / `WEB_FETCHES_PER_EVENT` — hard per-event tool caps.
- `WEB_FETCH_MAX_CONTENT_TOKENS` — per-page ingest cap (lands on cheap Haiku).
- `MAX_ITEMS_PER_FEED`, `STAGE2_MAX_TOKENS`, `STAGE2_EFFORT` — input/output trims.

## Tests

```bash
pip install -r requirements.txt
pytest
```

Covers the pure (no-network, no-API) logic: parsing, filtering, dedup/cap,
clustering-payload trimming, rendering (meters, badges, takeaways), index/archive
building, and state cutoff/pruning.
