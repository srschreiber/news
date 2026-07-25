# rss-news-generator

Automated, topic-partitioned tech-news briefings. A daily GitHub Actions cron
pulls RSS/Google-News sources, clusters them into distinct **events**, scores
each by importance, researches the top stories with Claude's web tools, and
writes short, fact-dense markdown briefings — published as a searchable MkDocs
site on GitHub Pages. Weekly / monthly / yearly rollups cascade on top.

Everything is a git file: sources, prompts, generated docs, and state.

## How it works

Two-stage pipeline per topic (see the design doc in
`docs/superpowers/specs/` for full detail):

1. **Cluster & score** — `claude-haiku-4-5` reads the day's items for a topic
   and returns structured JSON: distinct events (de-duplicated across feeds),
   each with an importance score (1–5), a theme, and keywords. No web tools,
   cheap.
2. **Research & write** — the top-K events go to `claude-sonnet-5`, which
   researches only where the RSS summary is thin (prefers `web_fetch` on the
   article link, falls back to `web_search`), then writes the briefing. Summaries
   are short and fact-dense; depth lives in the cited links.

Rollups (`weekly`/`monthly`/`yearly`) are a single Sonnet call that synthesizes
the level below (dailies → weekly → monthly → yearly), per topic.

## Layout

```
sources.yaml            # feeds + Google News queries, grouped by topic
prompts/                # cluster.md, write.md, rollup.md (edit freely)
generate.py             # the pipeline (daily | weekly | monthly | yearly)
state.json              # last_run + recently-seen links (dedup across runs)
docs/
  news/<topic>/YYYY-MM-DD.md
  weekly|monthly|yearly/<topic>/...
  search.md + assets/   # client-side keyword search over search-index.json
  search-index.json     # per-event index (appended each run)
mkdocs.yml              # Material theme, search + tags plugins
.github/workflows/pipeline.yml
```

## Configure sources

Edit `sources.yaml`. Each entry has a `name`, one of `url:` (direct RSS/Atom) or
`query:` (Google News search — the URL is built for you), and an optional
`topic:` (default `general`). Each topic gets its own daily file and rollups.

```yaml
sources:
  - name: The Verge
    url: https://www.theverge.com/rss/index.xml
    topic: general
  - name: "Google News — gaming industry"
    query: "video game industry release studio"
    topic: gaming
```

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python generate.py --dry-run        # fetch + cluster + score, print top events (cheap)
python generate.py                  # full daily run, writes docs
python generate.py weekly           # weekly rollup
mkdocs serve                        # preview the site at localhost:8000
```

`--dry-run` runs through Stage 1 and prints the selected events per topic
without Stage 2 research or writing any files — a cheap way to sanity-check
clustering granularity and importance scores.

## Deploy (GitHub)

1. Push to a GitHub repo.
2. Add repo secret **`ANTHROPIC_API_KEY`**.
3. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
4. The workflow runs on cron (daily/weekly/monthly/yearly) and via
   **Run workflow** (manual `workflow_dispatch`, pick a mode). It commits the
   generated docs + `state.json` and deploys the site.

## Cost controls

All in `generate.py` constants: `TOP_K_TO_RESEARCH` (only the top-K events per
topic get researched), `web_search`/`web_fetch` `max_uses` caps,
`MAX_TOOL_LOOP_ITERS`, per-feed item cap, short `STAGE2_MAX_TOKENS` + `medium`
effort, and prompt caching on the stage prompts. Haiku handles the bulk input;
Sonnet only sees the top-K. Hitting a cap degrades gracefully — it never fails
the run.

## Tests

```bash
pip install pytest feedparser pyyaml
pytest
```

Covers the pure (no-network, no-API) logic: parsing, filtering, dedup/cap,
grouping, rendering helpers, state cutoff/pruning.
