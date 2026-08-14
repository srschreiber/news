# tests/test_podcast.py
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def test_load_dotenv_sets_var(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TEST_VAR_PODCAST=hello\n")
    import podcast as p
    p._load_dotenv(env)
    assert os.environ.get("TEST_VAR_PODCAST") == "hello"

def test_load_dotenv_skips_existing(tmp_path):
    os.environ["ALREADY_SET"] = "original"
    env = tmp_path / ".env"
    env.write_text("ALREADY_SET=override\n")
    import podcast as p
    p._load_dotenv(env)
    assert os.environ["ALREADY_SET"] == "original"
