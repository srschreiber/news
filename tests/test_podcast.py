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
