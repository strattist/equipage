import importlib

import pytest


def test_require_returns_value(monkeypatch):
    from app import config

    monkeypatch.setenv("SOME_SETTING", "value-1")
    assert config.require("SOME_SETTING") == "value-1"


def test_require_missing_exits_with_name(monkeypatch):
    from app import config

    monkeypatch.delenv("MISSING_SETTING", raising=False)
    with pytest.raises(SystemExit) as exc:
        config.require("MISSING_SETTING")
    assert "MISSING_SETTING" in str(exc.value)


def test_model_default_is_opus_5_5(monkeypatch):
    from app import config

    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    importlib.reload(config)
    assert config.MODEL == "claude-opus-5-5"


def test_progress_auto_days_empty_means_off(monkeypatch):
    from app import config

    monkeypatch.setenv("PROGRESS_AUTO_DAYS", "")
    importlib.reload(config)
    assert config.PROGRESS_AUTO_DAYS == 0


def test_mealie_token_prefers_the_role_token_then_the_legacy_one():
    from app import config

    assert config.mealie_token("read", {}) is None
    assert config.mealie_token("write", {}) is None
    legacy = {"MEALIE_API_TOKEN": "legacy"}
    assert config.mealie_token("read", legacy) == "legacy"
    assert config.mealie_token("write", legacy) == "legacy"
    mixed = {**legacy, "MEALIE_READ_TOKEN": "r"}
    assert config.mealie_token("read", mixed) == "r"
    assert config.mealie_token("write", mixed) == "legacy"
    assert config.mealie_token("write", {"MEALIE_WRITE_TOKEN": "w"}) == "w"
