import json
import socket
from pathlib import Path

import pytest

from app import db
from app.meals import store


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def nutri(tmp_db, monkeypatch):
    from app import nutritionist

    profile = json.loads(Path("data/profile.example.json").read_text(encoding="utf-8"))
    (tmp_db / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return nutritionist


async def call(tool, **kwargs):
    return json.loads(await tool.call(kwargs))


async def test_get_meal_targets_reads_coach_plan(nutri):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO planned_sessions (date, kind, description, created_at) VALUES (?, ?, ?, ?)",
            ("2026-11-23", "walk", "Brisk walk", db.now()),
        )
    out = await call(nutri.get_meal_targets, start="2026-11-23", end="2026-11-24")
    monday = out["days"][0]
    assert monday["day"] == "2026-11-23"
    assert monday["exercise"] is False
    assert monday["dinner_type"] == "veggie"


async def test_get_meal_targets_falls_back_to_profile_week(nutri):
    out = await call(nutri.get_meal_targets, start="2026-11-25", end="2026-11-25")
    assert out["days"][0]["training"] == "hiit"  # Wednesday in the example profile
    assert out["days"][0]["dinner_type"] == "protein"


async def test_get_meal_targets_marks_away_and_lunch(nutri):
    await call(nutri.set_away_days, days="2026-11-26")
    out = await call(nutri.get_meal_targets, start="2026-11-25", end="2026-11-27")
    wed, thu, fri = out["days"]
    assert thu["away"] is True and thu["dinner_type"] is None
    assert wed["spare_portion"] is True
    assert fri["lunch_from"] is None


async def test_set_draft_dinner_uses_computed_dinner_type(nutri):
    out = await call(
        nutri.set_draft_dinner,
        day="2026-11-25",
        source="existing",
        name="Saumon, lentilles",
        recipe_slug="saumon-lentilles",
    )
    assert out["ok"] is True
    assert store.draft_get().dinners[__import__("datetime").date(2026, 11, 25)].dinner_type == "protein"


async def test_set_draft_dinner_refuses_away_day(nutri):
    await call(nutri.set_away_days, days="2026-11-26")
    out = await call(
        nutri.set_draft_dinner,
        day="2026-11-26",
        source="existing",
        name="X",
        recipe_slug="x",
    )
    assert "error" in out
    assert store.draft_get().dinners == {}


async def test_set_draft_dinner_rejects_bad_source_and_date(nutri):
    bad_source = await call(
        nutri.set_draft_dinner, day="2026-11-25", source="invented", name="X"
    )
    bad_date = await call(
        nutri.set_draft_dinner, day="demain", source="existing", name="X", recipe_slug="x"
    )
    missing_slug = await call(
        nutri.set_draft_dinner, day="2026-11-25", source="existing", name="X"
    )
    assert "error" in bad_source and "error" in bad_date and "error" in missing_slug


async def test_new_recipe_lines_are_parsed(nutri):
    await call(
        nutri.set_draft_dinner,
        day="2026-11-24",
        source="new",
        name="Curry de pois chiches",
        ingredients="400 g pois chiches\n1 oignon\n",
        steps="1. Faire revenir\n2. Mijoter",
    )
    dinner = next(iter(store.draft_get().dinners.values()))
    assert dinner.ingredients == ["400 g pois chiches", "1 oignon"]
    assert len(dinner.steps) == 2


async def test_fridge_tools_parse_lines(nutri):
    await call(nutri.add_fridge_items, items="œufs: 6\nlentilles")
    items = {i["name"]: i["quantity"] for i in (await call(nutri.get_fridge))["items"]}
    assert items == {"œufs": "6", "lentilles": ""}
    await call(nutri.remove_fridge_items, names="Lentilles")
    assert [i["name"] for i in (await call(nutri.get_fridge))["items"]] == ["œufs"]
    await call(nutri.replace_fridge, items="courge: 1")
    assert [i["name"] for i in (await call(nutri.get_fridge))["items"]] == ["courge"]


async def test_away_days_roundtrip_and_bad_date(nutri):
    await call(nutri.set_away_days, days="2026-11-26\n2026-11-27")
    out = await call(nutri.get_away_days, start="2026-11-23", end="2026-11-29")
    assert out["away_days"] == ["2026-11-26", "2026-11-27"]
    assert "error" in await call(nutri.set_away_days, days="jeudi")
    assert (await call(nutri.get_away_days, start="2026-11-23", end="2026-11-29"))[
        "away_days"
    ] == ["2026-11-26", "2026-11-27"]


async def test_draft_tools_bump_version(nutri):
    v0 = (await call(nutri.get_draft))["version"]
    await call(nutri.set_new_recipe_count, count=2)
    await call(nutri.set_draft_shopping_list, items="citron\nlentilles")
    draft = await call(nutri.get_draft)
    assert draft["version"] == v0 + 2
    assert draft["new_recipe_count"] == 2
    assert draft["shopping"] == ["citron", "lentilles"]
    await call(nutri.clear_draft)
    assert (await call(nutri.get_draft))["version"] == v0 + 3
    assert "error" in await call(nutri.set_new_recipe_count, count=-1)


async def test_mealie_down_returns_error_json(nutri, monkeypatch):
    monkeypatch.setenv("MEALIE_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.setenv("MEALIE_API_TOKEN", "secret-token")
    for out in (
        await call(nutri.search_recipes, query="poulet"),
        await call(nutri.get_recipe, slug="poulet"),
    ):
        assert "error" in out
        assert "secret-token" not in json.dumps(out)


async def test_mealie_not_configured_returns_error_json(nutri, monkeypatch):
    monkeypatch.delenv("MEALIE_URL", raising=False)
    monkeypatch.delenv("MEALIE_API_TOKEN", raising=False)
    assert "error" in await call(nutri.search_recipes)


async def test_search_recipes_maps_dinner_type_to_tag(nutri, mealie, monkeypatch):
    fake, _ = mealie
    monkeypatch.setenv("MEALIE_URL", fake.base_url)
    monkeypatch.setenv("MEALIE_API_TOKEN", fake.token)
    from app.mealie import MealieClient

    async with MealieClient(fake.base_url, fake.token) as client:
        await client.create_recipe("Curry", ["pois chiches"], ["cuire"], ["végétarien"])
        await client.create_recipe("Poulet", ["poulet"], ["rôtir"], ["protéines"])
    out = await call(nutri.search_recipes, tag="veggie")
    assert [r["name"] for r in out["recipes"]] == ["Curry"]
    recipe = await call(nutri.get_recipe, slug=out["recipes"][0]["slug"])
    assert recipe["name"] == "Curry"


def test_no_tool_can_write_to_mealie(nutri):
    names = {tool.name for tool in nutri.NUTRITIONIST.tools}
    assert not names & {
        "create_recipe",
        "set_meal_plan_entry",
        "add_shopping_items",
        "commit_draft",
    }
    source = Path(nutri.__file__).read_text(encoding="utf-8")
    assert "commit_draft" not in source
    assert nutri.NUTRITIONIST.name == "nutritionist"
    assert (Path("prompts") / nutri.NUTRITIONIST.prompt_file).exists()
