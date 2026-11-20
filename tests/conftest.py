import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "agents.sqlite3")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.json")

    from app import db

    db.init()
    return tmp_path
