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


def test_format_events_for_prompt_includes_title_and_takeaways():
    import podcast as p
    events = [
        {"title": "AI breaks record", "one_liner": "OpenAI's model beats benchmark.",
         "takeaways": ["First time in 10 years", "Major leap"], "importance": 9,
         "topic": "ai"},
        {"title": "Market up", "one_liner": "S&P hits high.", "takeaways": [],
         "importance": 7, "topic": "markets"},
    ]
    text = p._format_events_for_prompt(events, max_events=10)
    assert "AI breaks record" in text
    assert "First time in 10 years" in text
    assert "Market up" in text


def test_format_events_for_prompt_caps_at_max():
    import podcast as p
    events = [
        {"title": f"Story {i}", "one_liner": "x", "takeaways": [], "importance": i, "topic": "ai"}
        for i in range(20)
    ]
    text = p._format_events_for_prompt(events, max_events=5)
    assert text.count("Story") == 5


def test_render_tts_missing_key_raises(monkeypatch):
    import podcast as p
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        p._render_tts("Hello world", Path("/tmp/test.mp3"))
        assert False, "should have raised"
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)


def test_prune_old_audio_removes_old_files(tmp_path):
    import podcast as p
    feed_dir = tmp_path / "technology"
    feed_dir.mkdir()
    today = dt.date(2026, 8, 14)
    # Create files: 3 recent (delta 0,1,2), 2 old (delta 6,10)
    for delta in [0, 1, 2, 6, 10]:
        d = (today - dt.timedelta(days=delta)).isoformat()
        (feed_dir / f"{d}.mp3").write_bytes(b"x")
    p._prune_old_audio(feed_dir, keep_days=5, today=today)
    remaining = sorted(f.name for f in feed_dir.iterdir())
    assert remaining == ["2026-08-12.mp3", "2026-08-13.mp3", "2026-08-14.mp3"]


def test_available_episodes_returns_sorted_dates(tmp_path):
    import podcast as p
    feed_dir = tmp_path / "world"
    feed_dir.mkdir()
    for d in ["2026-08-12", "2026-08-14", "2026-08-13"]:
        (feed_dir / f"{d}.mp3").write_bytes(b"x")
    eps = p._available_episodes(feed_dir)
    assert eps == ["2026-08-14", "2026-08-13", "2026-08-12"]


def test_build_podcast_page_contains_feed_name(tmp_path):
    import podcast as p
    tech_dir = tmp_path / "audio" / "technology"
    tech_dir.mkdir(parents=True)
    (tech_dir / "2026-08-14.mp3").write_bytes(b"x")

    page = p._build_podcast_page(audio_dir=tmp_path / "audio", date="2026-08-14")
    assert "Technology" in page
    assert "2026-08-14.mp3" in page
    assert "<audio" in page


def test_build_podcast_page_omits_feed_with_no_audio(tmp_path):
    import podcast as p
    world_dir = tmp_path / "audio" / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "2026-08-14.mp3").write_bytes(b"x")

    page = p._build_podcast_page(audio_dir=tmp_path / "audio", date="2026-08-14")
    assert "World" in page
    assert "Technology" not in page
