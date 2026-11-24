"""Every read the models can trigger goes through the read-only connection."""

import json
from pathlib import Path

import pytest

from app import coach, db, exercises, nutritionist


@pytest.fixture
def seeded(tmp_db):
    profile = json.loads(Path("data/profile.example.json").read_text(encoding="utf-8"))
    (tmp_db / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    exercises.seed()
    return tmp_db


@pytest.fixture
def no_rw(seeded, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("read path opened a read-write connection")

    monkeypatch.setattr(db, "connect", boom)


async def call(tool, **kwargs):
    return json.loads(await tool.call(kwargs))


async def test_coach_read_tools_use_the_read_only_connection(no_rw):
    assert "planned" in await call(coach.get_plan, start="2026-11-23", end="2026-11-29")
    assert await call(coach.get_recent_sessions) == []
    assert await call(coach.get_benchmarks) == []
    assert await call(coach.list_exercises)
    assert "error" not in await call(coach.get_exercise, name="squats")


async def test_nutritionist_read_tools_use_the_read_only_connection(no_rw):
    out = await call(
        nutritionist.get_meal_targets, start="2026-11-23", end="2026-11-24"
    )
    assert len(out["days"]) == 2
    assert await call(nutritionist.get_fridge) == {"items": []}
    assert (await call(nutritionist.get_draft))["dinners"] == []
    out = await call(nutritionist.get_away_days, start="2026-11-23", end="2026-11-29")
    assert out == {"away_days": []}


def test_linkify_uses_the_read_only_connection(no_rw):
    assert exercises.linkify("squats").startswith("[squats](")
