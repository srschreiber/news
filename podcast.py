#!/usr/bin/env python3
"""Daily podcast generator — one MP3 per feed via Claude Sonnet + OpenAI TTS.

Run independently of generate.py. Reads today's events from state.json,
generates a conversational script per feed, renders to MP3, rebuilds
docs/podcast.md. Exits cleanly if OPENAI_API_KEY is unset.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DOCS = ROOT / "docs"
AUDIO_DIR = DOCS / "audio"
PODCAST_PAGE = DOCS / "podcast.md"
SOURCES_FILE = ROOT / "sources.yaml"

AUDIO_KEEP_DAYS = 5
SCRIPT_MODEL = "claude-sonnet-5"
SCRIPT_MAX_TOKENS = 2000
SERPER_BUDGET = 5          # max search calls per feed
TTS_MODEL = "tts-1-hd"
TTS_VOICE = "onyx"

def _load_feeds_config() -> list[dict]:
    """Read feed→topic mapping from sources.yaml. Falls back to hardcoded defaults
    if the file is missing or unreadable (e.g., in test environments)."""
    _FALLBACK = [
        {"key": "technology", "title": "Technology",
         "topics": ["tech", "ai", "anthropic", "gpt", "security", "email-security",
                    "golang", "python", "postgres", "tech-research"]},
        {"key": "world",       "title": "World",       "topics": ["world", "markets"]},
        {"key": "science",     "title": "Science",
         "topics": ["science", "space", "health", "diet-exercise"]},
        {"key": "environment", "title": "Environment",
         "topics": ["climate-resilience", "climate-change", "conservation"]},
    ]
    try:
        import yaml
        raw = yaml.safe_load(SOURCES_FILE.read_text())
        feeds_raw = raw.get("feeds") or {}
        result = []
        for key, meta in feeds_raw.items():
            result.append({
                "key": key,
                "title": meta.get("title", key.title()),
                "topics": meta.get("topics") or [],
            })
        return result if result else _FALLBACK
    except Exception:
        return _FALLBACK


FEEDS: list[dict] = _load_feeds_config()


# Duplicated from generate.py intentionally — podcast.py must run standalone without
# importing the 3300-line generate.py (whose module-level code has side effects).
def _load_dotenv(path: Path = ROOT / ".env") -> None:
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


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def run(dry_run: bool = False) -> None:
    pass  # implemented in a later task


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Generate daily podcasts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate scripts but skip TTS and file writes")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY") and not args.dry_run:
        _log("OPENAI_API_KEY not set — skipping podcast generation")
        return

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
