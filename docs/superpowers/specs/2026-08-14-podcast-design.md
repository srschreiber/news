# Podcast Feature Design

**Date:** 2026-08-14  
**Status:** Approved

## Overview

A daily podcast generator that produces one MP3 per feed (Technology, World, Science, Environment). Each episode is a 5-7 minute conversational radio-style briefing covering the feed's top stories grouped by sub-topic. Published via a dedicated `/podcast` page with embedded audio players and +15s/-15s skip controls.

## Architecture

A standalone script `podcast.py` in the repo root, independent of `generate.py`. Invoked separately as a daily cron at ~9 AM PST. Reads today's already-researched event data (from the JSON embedded in the daily topic docs), generates scripts via Claude Sonnet, renders audio via OpenAI TTS, and rebuilds `docs/podcast.md`.

If `OPENAI_API_KEY` is unset, the script logs a message and exits cleanly — no error.

## Script Generation

For each feed, `podcast.py`:

1. Reads today's events for all topics within the feed from `docs/news/{topic}/{date}.md` (parses the embedded JSON blob).
2. Selects the top events by importance across all sub-topics, grouped by sub-topic for context.
3. Calls Claude Sonnet with a Serper `search(query)` tool (budget: 5 calls per feed). The prompt instructs it to write a 400-600 word conversational radio script covering the feed's stories, and to use search only when it genuinely needs additional context to make the commentary richer.
4. Script style: natural intro naming the feed and date, stories woven together in prose (sub-topics not announced as headers), smooth transitions, brief outro. No bullet points, full sentences throughout.

Feeds with zero events today are skipped silently.

## TTS

- **API:** OpenAI `/v1/audio/speech` via raw `urllib.request` (no new Python package)
- **Model:** `tts-1-hd`
- **Voice:** `onyx`
- **Format:** `mp3`
- One API call per feed, response bytes written directly to disk.

## Storage

```
docs/audio/{feed-key}/{YYYY-MM-DD}.mp3
```

- 5-day rolling window — on each run, files older than 5 days are deleted.
- Steady-state size: 4 feeds × 5 days × ~4 MB = ~80 MB. No git LFS needed.
- No date suffix on the podcast page URL — links reference the file by date, so old links remain valid for 5 days.

## Podcast Page

`docs/podcast.md` — rebuilt on every run. Contains:

- Brief intro sentence.
- One card per feed (Technology, World, Science, Environment) with:
  - Feed name and today's date.
  - Custom HTML `<audio>` player with −15s / +15s skip buttons (inline JS, no dependencies).
  - Links to the previous 4 available episodes as a small archive row.
- Feeds with no audio file for today are omitted from the page.

The page is linked from the site nav.

## Cost Estimate

| Component | Rate | Daily Cost |
|---|---|---|
| Claude Sonnet scripts | ~$3/1M tokens, ~4k tokens/feed | ~$0.05 |
| Serper searches | $0.001/call, 5 max/feed | ~$0.02 max |
| OpenAI TTS (tts-1-hd) | $0.015/1k chars, ~500 chars/feed | ~$0.03 |
| **Total** | | **~$0.10/day (~$3/month)** |

## Invocation

```bash
python podcast.py
```

Reads `.env` via the same `_load_dotenv()` pattern as `generate.py`. Requires `ANTHROPIC_API_KEY`, `SERPER_DEV_API_KEY`, and `OPENAI_API_KEY`. Missing `OPENAI_API_KEY` → graceful skip. Missing Anthropic or Serper keys → script errors (same as main pipeline).

## Out of Scope

- Synchronized transcript / captions
- Per-topic (sub-feed level) podcasts
- External audio hosting
- Podcast RSS feed / Apple Podcasts submission
