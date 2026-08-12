# Sam's News

A GitHub Actions cron that turns RSS feeds and Google searches into a daily skimmable news site — clustered, scored, and summarized by Claude. Runs for ~$0.10–0.20/day with no server or database; everything is a git file.

**Live site:** https://srschreiber.github.io/news/

## What it does

Each run (every 2 hours):

1. **Fetches** RSS + runs per-topic Serper searches to catch stories RSS missed
2. **Clusters** items by embedding similarity, then scores and titles each story with Haiku
3. **Researches** high-importance stories: fetches source pages, extracts key facts
4. **Writes** daily topic pages + updates weekly/monthly rollups

Stories that update across runs get merged (existing takeaways preserved, new facts added). Stories that continue across days are cross-linked.

Feeds: **Technology** (tech, AI, security, research) · **World** · **Science** (health, space, diet) · **Environment** (climate, conservation)

## Stack

| | |
|---|---|
| Claude Haiku | Clustering, scoring, research reads |
| Claude Sonnet | Final summaries (one batched call/run) |
| [Voyage AI](https://voyageai.com) | Embeddings for story dedup + clustering pregrouping |
| [Serper.dev](https://serper.dev) | Google search ($1/1k queries) |
| MkDocs Material | Static site |

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export SERPER_DEV_API_KEY=...
export VOYAGE_API_KEY=...

python generate.py --dry-run   # cluster + score only, no writes
python generate.py             # full run
mkdocs serve                   # preview at localhost:8000
```

## Deploy

1. Push to GitHub
2. Add repo secrets: `ANTHROPIC_API_KEY`, `SERPER_DEV_API_KEY`, `VOYAGE_API_KEY`
3. Settings → Pages → Source: **GitHub Actions**

The pipeline runs on cron and commits results back to the repo. A separate `publish.yml` redeploys on docs/theme changes without an LLM run.

## Configure

Edit `sources.yaml` to add/remove feeds and adjust per-topic research:

```yaml
research:
  default: true
  topics:
    golang: false   # RSS-only, no web research

sources:
  - name: The Verge
    url: https://www.theverge.com/rss/index.xml
    topic: tech
  - name: "Google News — AI"
    query: "AI model release LLM"
    topic: ai
```

Edit `prompts/` to change how stories are clustered, read, or written. Per-run cost breakdown is in `metrics.json`.

## Tests

```bash
pytest
```
