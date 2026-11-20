import json
from datetime import datetime, timedelta

from app import coach, config

ROOT = config.ROOT


async def call(tool, **args):
    return json.loads(await tool.call(args))


async def test_save_planned_session_upserts_same_day(tmp_db):
    await call(
        coach.save_planned_session,
        day="2026-11-23",
        kind="rowing",
        description="Steady row 30 min",
    )
    await call(
        coach.save_planned_session,
        day="2026-11-23",
        kind="walk",
        description="Brisk walk 40 min",
    )
    plan = await call(coach.get_plan, start="2026-11-23", end="2026-11-23")
    assert [(p["date"], p["kind"], p["description"]) for p in plan["planned"]] == [
        ("2026-11-23", "walk", "Brisk walk 40 min")
    ]


async def test_invalid_date_and_kind_return_error_json(tmp_db):
    bad = [
        (
            coach.save_planned_session,
            {"day": "23/11/2026", "kind": "rowing", "description": "x"},
        ),
        (
            coach.save_planned_session,
            {"day": "2026-11-23", "kind": "yoga", "description": "x"},
        ),
        (coach.log_session, {"day": "2026-13-01", "completed": True, "done": "x"}),
        (coach.get_plan, {"start": "tomorrow", "end": "2026-11-29"}),
        (coach.get_plan, {"start": "2026-11-23", "end": "2026-02-30"}),
        (coach.record_benchmark, {"day": "nope", "distance_m": 4000}),
    ]
    for tool, args in bad:
        result = json.loads(await tool.call(args))
        assert set(result) == {"error"}, (tool.name, args, result)
    plan = await call(coach.get_plan, start="2000-01-01", end="2100-01-01")
    assert plan == {"planned": [], "logged": []}
    assert await call(coach.get_benchmarks) == []


async def test_log_session_stores_null_for_zero_rpe_and_distance(tmp_db):
    await call(
        coach.log_session, day="2026-11-20", completed=True, done="Steady row 30 min"
    )
    [row] = (await call(coach.get_plan, start="2026-11-20", end="2026-11-20"))["logged"]
    for field in ("rpe", "distance_m", "duration_min", "efforts", "effort_s", "rest_s"):
        assert row[field] is None, field
    assert row["completed"] is True
    assert row["done"] == "Steady row 30 min"


async def test_log_session_stores_distance_and_hiit_fields(tmp_db):
    today = datetime.now(config.TZ).date().isoformat()
    await call(
        coach.log_session,
        day=today,
        completed=True,
        done="HIIT 12 x 45 s / 45 s",
        rpe=7,
        efforts=12,
        effort_s=45,
        rest_s=45,
    )
    await call(
        coach.log_session,
        day=today,
        completed=True,
        done="Steady row",
        distance_m=6100,
        duration_min=30,
    )
    rows = await call(coach.get_recent_sessions, days=7)
    hiit = next(r for r in rows if r["efforts"])
    row = next(r for r in rows if r["distance_m"])
    assert (hiit["rpe"], hiit["efforts"], hiit["effort_s"], hiit["rest_s"]) == (
        7,
        12,
        45,
        45,
    )
    assert (row["distance_m"], row["duration_min"]) == (6100, 30)


async def test_get_plan_joins_planned_and_logged_in_range(tmp_db):
    await call(
        coach.save_planned_session, day="2026-11-22", kind="rest", description="Rest"
    )
    await call(
        coach.save_planned_session,
        day="2026-11-23",
        kind="rowing",
        description="Steady row",
    )
    await call(
        coach.save_planned_session, day="2026-11-25", kind="hiit", description="HIIT"
    )
    await call(
        coach.save_planned_session,
        day="2026-11-30",
        kind="rowing",
        description="Next week",
    )
    await call(
        coach.log_session, day="2026-11-23", completed=True, done="Row 30 min", rpe=5
    )
    await call(coach.log_session, day="2026-11-21", completed=True, done="Before range")
    plan = await call(coach.get_plan, start="2026-11-23", end="2026-11-29")
    assert [p["date"] for p in plan["planned"]] == ["2026-11-23", "2026-11-25"]
    assert [(l["date"], l["rpe"]) for l in plan["logged"]] == [("2026-11-23", 5)]


async def test_get_recent_sessions_excludes_older_than_n_days(tmp_db):
    today = datetime.now(config.TZ).date()
    for delta in (0, 3, 10):
        day = (today - timedelta(days=delta)).isoformat()
        await call(coach.log_session, day=day, completed=True, done=f"{delta} days ago")
    rows = await call(coach.get_recent_sessions, days=5)
    assert sorted(r["done"] for r in rows) == ["0 days ago", "3 days ago"]


async def test_benchmarks_ordered_oldest_first(tmp_db):
    await call(
        coach.record_benchmark, day="2026-12-07", distance_m=4300, avg_split_s=139.5
    )
    await call(coach.record_benchmark, day="2026-11-09", distance_m=4100)
    rows = await call(coach.get_benchmarks)
    assert [(r["date"], r["distance_m"], r["avg_split_s"]) for r in rows] == [
        ("2026-11-09", 4100, None),
        ("2026-12-07", 4300, 139.5),
    ]


async def test_profile_missing_returns_error_json(tmp_db):
    result = await call(coach.get_profile)
    assert set(result) == {"error"}


async def test_update_profile_parses_json_or_text(tmp_db):
    config.PROFILE_PATH.write_text(json.dumps({"age": 40}))
    await call(coach.update_profile, key="weight_kg", value="82")
    await call(coach.update_profile, key="injuries_or_limits", value="genou sensible")
    profile = await call(coach.get_profile)
    assert profile == {
        "age": 40,
        "weight_kg": 82,
        "injuries_or_limits": "genou sensible",
    }


def test_tool_schemas_have_expected_required_fields():
    def required(tool):
        return set(tool.to_dict()["input_schema"].get("required", []))

    assert required(coach.log_session) == {"day", "completed", "done"}
    assert required(coach.save_planned_session) == {"day", "kind", "description"}
    assert required(coach.get_plan) == {"start", "end"}
    assert required(coach.record_benchmark) == {"day", "distance_m"}
    assert required(coach.update_profile) == {"key", "value"}
    assert required(coach.get_recent_sessions) == set()
    for tool in coach.TOOLS:
        for prop in tool.to_dict()["input_schema"]["properties"].values():
            assert prop.get("type") != "array", tool.name


def test_example_profile_is_valid_json_with_weekly_structure():
    profile = json.loads(
        (ROOT / "data" / "profile.example.json").read_text(encoding="utf-8")
    )
    for key in (
        "age",
        "height_cm",
        "weight_kg",
        "goal",
        "available_minutes_per_day",
        "equipment",
        "injuries_or_limits",
        "weekly_structure",
        "history",
    ):
        assert key in profile, key
    assert set(profile["weekly_structure"]) == {
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
    }
    assert len(profile["history"]) == 2


def test_prompt_mentions_every_tool_name():
    prompt = (ROOT / "prompts" / "coach.md").read_text(encoding="utf-8")
    for name in (
        "get_profile",
        "update_profile",
        "save_planned_session",
        "get_plan",
        "log_session",
        "get_recent_sessions",
        "record_benchmark",
        "get_benchmarks",
    ):
        assert name in prompt, name


def test_prompt_has_no_hardcoded_one_point_five_rule():
    prompt = (ROOT / "prompts" / "coach.md").read_text(encoding="utf-8")
    assert "1,5" not in prompt
    assert "1.5x" not in prompt


def test_coach_agent_uses_tools_and_prompt():
    assert coach.COACH.name == "coach"
    assert (config.PROMPTS_DIR / coach.COACH.prompt_file).is_file()
    assert {t.name for t in coach.TOOLS} >= {
        "get_profile",
        "log_session",
        "get_benchmarks",
    }


async def test_exercise_tools_roundtrip(tmp_db):
    from app import exercises

    exercises.seed()
    listing = await call(coach.list_exercises)
    assert len(listing) == 12
    assert set(listing[0]) == {"name", "explanation"}
    assert "\n" not in listing[0]["explanation"]

    skaters = await call(coach.get_exercise, name="skaters")
    assert skaters["name"] == "Skaters" and skaters["url"].startswith("https://")
    assert await call(coach.get_exercise, name="yoga") == {"error": "unknown exercise"}

    assert (
        await call(coach.set_exercise_url, name="skaters", url="https://example.com/v")
    )["ok"] is True
    assert (await call(coach.get_exercise, name="skaters"))[
        "url"
    ] == "https://example.com/v"
    assert set(
        await call(coach.set_exercise_url, name="skaters", url="file:///etc/passwd")
    ) == {"error"}


async def test_logged_rows_expose_their_id(tmp_db):
    logged = await call(
        coach.log_session, day="2026-11-19", completed=True, done="HIIT"
    )
    [row] = (await call(coach.get_plan, start="2026-11-19", end="2026-11-19"))["logged"]
    assert row["id"] == logged["id"]
    today = datetime.now(config.TZ).date().isoformat()
    await call(coach.log_session, day=today, completed=True, done="Walk")
    [recent] = await call(coach.get_recent_sessions, days=0)
    assert isinstance(recent["id"], int)


async def test_update_session_log_changes_only_given_fields(tmp_db):
    logged = await call(
        coach.log_session,
        day="2026-11-19",
        completed=True,
        done="HIIT 12 x 45 s / 45 s",
        efforts=12,
        effort_s=45,
        rest_s=45,
    )
    result = await call(
        coach.update_session_log, id=logged["id"], rpe=7, notes="Felt ok. Slept well."
    )
    assert result["ok"] is True
    [row] = (await call(coach.get_plan, start="2026-11-19", end="2026-11-19"))["logged"]
    assert (row["rpe"], row["notes"]) == (7, "Felt ok. Slept well.")
    assert (row["done"], row["efforts"], row["effort_s"], row["rest_s"]) == (
        "HIIT 12 x 45 s / 45 s",
        12,
        45,
        45,
    )


async def test_update_session_log_rejects_bad_input_and_changes_nothing(tmp_db):
    logged = await call(
        coach.log_session, day="2026-11-19", completed=True, done="Row", rpe=5
    )
    for args in (
        {"id": 999, "notes": "x"},  # unknown id
        {"id": logged["id"]},  # nothing to change
        {"id": logged["id"], "rpe": 11},
        {"id": logged["id"], "distance_m": -5},
    ):
        assert set(await call(coach.update_session_log, **args)) == {"error"}, args
    [row] = (await call(coach.get_plan, start="2026-11-19", end="2026-11-19"))["logged"]
    assert (row["rpe"], row["notes"], row["distance_m"]) == (5, None, None)


def test_update_session_log_requires_only_the_id():
    required = set(coach.update_session_log.to_dict()["input_schema"]["required"])
    assert required == {"id"}


def test_prompt_tells_the_coach_to_update_instead_of_logging_twice():
    prompt = (ROOT / "prompts" / "coach.md").read_text(encoding="utf-8")
    assert "update_session_log" in prompt
