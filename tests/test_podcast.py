# tests/test_podcast.py
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_load_dotenv_sets_var(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_VAR_PODCAST", raising=False)
    env = tmp_path / ".env"
    env.write_text("TEST_VAR_PODCAST=hello\n")
    import podcast as p
    p._load_dotenv(env)
    assert os.environ.get("TEST_VAR_PODCAST") == "hello"


def test_load_dotenv_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "original")
    env = tmp_path / ".env"
    env.write_text("ALREADY_SET=override\n")
    import podcast as p
    p._load_dotenv(env)
    assert os.environ["ALREADY_SET"] == "original"


def test_load_feed_events_groups_by_feed(tmp_path):
    import podcast as p
    state = {
        "today_events": {
            "date": "2026-08-14",
            "events": [
                {"event_id": "a", "topic": "ai",     "importance": 8, "title": "AI story"},
                {"event_id": "b", "topic": "world",  "importance": 7, "title": "World story"},
                {"event_id": "c", "topic": "space",  "importance": 6, "title": "Space story"},
                {"event_id": "d", "topic": "unknown","importance": 5, "title": "Unknown"},
            ],
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    result = p._load_feed_events(state_file, date="2026-08-14")
    assert result["technology"][0]["title"] == "AI story"
    assert result["world"][0]["title"] == "World story"
    assert result["science"][0]["title"] == "Space story"
    assert "unknown" not in result


def test_load_feed_events_wrong_date(tmp_path):
    import podcast as p
    state = {"today_events": {"date": "2026-08-13", "events": [
        {"event_id": "x", "topic": "ai", "importance": 5, "title": "Old"}
    ]}}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    result = p._load_feed_events(state_file, date="2026-08-14")
    assert all(len(v) == 0 for v in result.values())
