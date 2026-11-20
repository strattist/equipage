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
