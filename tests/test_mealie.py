import socket
from datetime import date

import pytest

from app.mealie import (
    MEALIE_TAGS,
    MealieAuthError,
    MealieClient,
    MealieError,
    MealieReader,
    MealieUnavailable,
    RecipeSummary,
)

TOKEN = "secret-token"  # the fixture's token, see tests/conftest.py
LIST_NAME = "Courses du 23/11/2026 au 29/11/2026"


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_tags_convention():
    assert MEALIE_TAGS == {"protein": "protéines", "veggie": "végétarien"}


async def test_search_filters_by_tag_and_query(mealie):
    fake, client = mealie
    fake.add_recipe("Poulet rôti, courge", tags=["protéines"])
    fake.add_recipe("Poulet au curry", tags=["protéines"])
    fake.add_recipe("Curry de pois chiches", tags=["végétarien"])
    fake.add_recipe("Soupe de poireaux")  # untagged, like the user's existing recipes

    assert [r.name for r in await client.search_recipes()] == [
        "Curry de pois chiches",
        "Poulet au curry",
        "Poulet rôti, courge",
        "Soupe de poireaux",
    ]
    assert [r.name for r in await client.search_recipes("poulet")] == [
        "Poulet au curry",
        "Poulet rôti, courge",
    ]
    found = await client.search_recipes(tag="végétarien")
    assert found == [
        RecipeSummary(
            slug="curry-de-pois-chiches",
            name="Curry de pois chiches",
            tags=["végétarien"],
        )
    ]
    assert [r.name for r in await client.search_recipes("curry", tag="protéines")] == [
        "Poulet au curry"
    ]
    assert await client.search_recipes("introuvable") == []
    assert len(await client.search_recipes(limit=2)) == 2


async def test_get_recipe_returns_name_ingredients_and_steps_as_strings(mealie):
    fake, client = mealie
    fake.add_recipe(
        "Gratin",
        ingredients=["400 g de butternut", "chèvre"],
        steps=["Cuire.", "Gratiner."],
    )
    assert await client.get_recipe("gratin") == {
        "slug": "gratin",
        "name": "Gratin",
        "servings": 2,
        "tags": [],
        "ingredients": ["400 g de butternut", "chèvre"],
        "steps": ["Cuire.", "Gratiner."],
    }


async def test_get_unknown_recipe_raises_plain_mealie_error(mealie):
    _, client = mealie
    with pytest.raises(MealieError) as exc:
        await client.get_recipe("nope")
    assert not isinstance(exc.value, (MealieAuthError, MealieUnavailable))


async def test_create_recipe_writes_ingredients_steps_tags_and_two_servings(mealie):
    fake, client = mealie
    slug = await client.create_recipe(
        "Curry de pois chiches",
        ["400 g de pois chiches", "1 oignon"],
        ["Faire revenir l'oignon.", "Mijoter 20 min."],
        ["végétarien"],
    )
    recipe = fake.recipes[slug]
    assert recipe["recipeServings"] == 2
    assert [i["note"] for i in recipe["recipeIngredient"]] == [
        "400 g de pois chiches",
        "1 oignon",
    ]
    assert [s["text"] for s in recipe["recipeInstructions"]] == [
        "Faire revenir l'oignon.",
        "Mijoter 20 min.",
    ]
    assert [t["name"] for t in recipe["tags"]] == ["végétarien"]


async def test_create_recipe_is_idempotent_by_name(mealie):
    fake, client = mealie
    args = ("Curry de pois chiches", ["pois chiches"], ["Cuire."], ["végétarien"])
    first = await client.create_recipe(*args)
    second = await client.create_recipe(*args)
    assert first == second
    assert len(fake.recipes) == 1
    assert fake.recipes[first]["recipeServings"] == 2
    assert len(fake.tags) == 1


async def test_tags_are_created_once_and_reused_across_recipes(mealie):
    fake, client = mealie
    await client.create_recipe("A", ["a"], ["a"], ["protéines"])
    await client.create_recipe("B", ["b"], ["b"], ["Protéines"])  # same tag, other case
    assert len(fake.tags) == 1
    assert all(len(r["tags"]) == 1 for r in fake.recipes.values())


async def test_create_recipe_completes_an_empty_shell_left_by_an_interrupted_attempt(
    mealie,
):
    fake, client = mealie
    fake.add_recipe(
        "Soupe", ingredients=[], steps=[], servings=0.0
    )  # created, then the update failed
    slug = await client.create_recipe("Soupe", ["poireaux"], ["Cuire."], ["végétarien"])
    assert len(fake.recipes) == 1
    assert [i["note"] for i in fake.recipes[slug]["recipeIngredient"]] == ["poireaux"]
    assert fake.recipes[slug]["recipeServings"] == 2


async def test_create_recipe_never_overwrites_an_existing_filled_recipe(mealie):
    fake, client = mealie
    existing = fake.add_recipe(
        "Poulet rôti", ingredients=["un poulet"], steps=["Rôtir."]
    )
    slug = await client.create_recipe(
        "Poulet rôti", ["autre chose"], ["Autre."], ["protéines"]
    )
    assert slug == existing["slug"]
    assert [i["note"] for i in fake.recipes[slug]["recipeIngredient"]] == ["un poulet"]
    assert fake.recipes[slug]["tags"] == []


async def test_meal_plan_entry_not_duplicated(mealie):
    fake, client = mealie
    slug = fake.add_recipe("Gratin")["slug"]
    other = fake.add_recipe("Soupe")["slug"]
    await client.set_meal_plan_entry(date(2026, 11, 23), "dinner", slug)
    await client.set_meal_plan_entry(date(2026, 11, 23), "dinner", slug)
    await client.set_meal_plan_entry(date(2026, 11, 24), "lunch", slug)
    await client.set_meal_plan_entry(
        date(2026, 11, 23), "dinner", other
    )  # another recipe: its own entry
    assert [(e["date"], e["entryType"], e["recipeId"]) for e in fake.entries] == [
        ("2026-11-23", "dinner", fake.recipes[slug]["id"]),
        ("2026-11-24", "lunch", fake.recipes[slug]["id"]),
        ("2026-11-23", "dinner", fake.recipes[other]["id"]),
    ]


async def test_meal_plan_entry_rejects_bad_slot_and_unknown_recipe(mealie):
    fake, client = mealie
    slug = fake.add_recipe("Gratin")["slug"]
    with pytest.raises(ValueError):
        await client.set_meal_plan_entry(date(2026, 11, 23), "brunch", slug)
    with pytest.raises(MealieError):
        await client.set_meal_plan_entry(date(2026, 11, 23), "dinner", "unknown")
    assert fake.entries == []


async def test_shopping_items_not_duplicated(mealie):
    fake, client = mealie
    await client.add_shopping_items(["Lentilles — 300 g", "citron"], LIST_NAME)
    await client.add_shopping_items(
        ["lentilles — 300 g ", "Citron", "poireaux"], LIST_NAME
    )
    assert len(fake.lists) == 1
    [shopping_list] = fake.lists.values()
    assert shopping_list["name"] == LIST_NAME
    assert [i["note"] for i in shopping_list["listItems"]] == [
        "Lentilles — 300 g",
        "citron",
        "poireaux",
    ]


async def test_checked_items_are_added_again(mealie):
    fake, client = mealie
    await client.add_shopping_items(["lait"], LIST_NAME)
    next(iter(fake.lists.values()))["listItems"][0]["checked"] = True
    await client.add_shopping_items(["lait"], LIST_NAME)
    assert [i["checked"] for i in next(iter(fake.lists.values()))["listItems"]] == [
        True,
        False,
    ]


async def test_shopping_goes_to_the_list_with_that_name_and_other_lists_are_untouched(
    mealie,
):
    fake, client = mealie
    await client.add_shopping_items(["pain"], "Courses du 13/11/2026 au 17/11/2026")
    await client.add_shopping_items(["lait"], LIST_NAME)
    names = {
        lst["name"]: [i["note"] for i in lst["listItems"]]
        for lst in fake.lists.values()
    }
    assert names == {
        "Courses du 13/11/2026 au 17/11/2026": ["pain"],
        LIST_NAME: ["lait"],
    }


async def test_empty_shopping_items_create_nothing(mealie):
    fake, client = mealie
    await client.add_shopping_items([], LIST_NAME)
    await client.add_shopping_items(["  "], LIST_NAME)
    assert fake.lists == {}


async def test_401_raises_auth_error(mealie):
    fake, _ = mealie
    async with MealieClient(fake.base_url, "wrong-token") as bad:
        with pytest.raises(MealieAuthError) as exc:
            await bad.search_recipes()
    assert "wrong-token" not in str(exc.value)


async def test_403_raises_auth_error(mealie):
    fake, client = mealie
    fake.fail_next("GET", "/api/recipes", 403)
    with pytest.raises(MealieAuthError):
        await client.search_recipes()


async def test_connection_refused_raises_unavailable():
    async with MealieClient(f"http://127.0.0.1:{closed_port()}", TOKEN) as client:
        with pytest.raises(MealieUnavailable) as exc:
            await client.search_recipes()
    assert TOKEN not in str(exc.value)


async def test_500_raises_unavailable(mealie):
    fake, client = mealie
    fake.fail_next("GET", "/api/recipes", 500)
    with pytest.raises(MealieUnavailable):
        await client.search_recipes()
    assert await client.search_recipes() == []  # the next call works again


async def test_other_4xx_raises_mealie_error_without_the_token(mealie):
    fake, client = mealie
    fake.fail_next("POST", "/api/recipes", 422)
    with pytest.raises(MealieError) as exc:
        await client.create_recipe("X", ["a"], ["b"], [])
    assert not isinstance(exc.value, (MealieAuthError, MealieUnavailable))
    assert TOKEN not in str(exc.value)


async def test_requests_carry_auth_header(mealie):
    fake, client = mealie
    slug = fake.add_recipe("Gratin")["slug"]
    await client.search_recipes()
    await client.get_recipe(slug)
    await client.set_meal_plan_entry(date(2026, 11, 23), "dinner", slug)
    await client.add_shopping_items(["lait"], LIST_NAME)
    assert len(fake.requests) >= 5
    assert {r["auth"] for r in fake.requests} == {f"Bearer {TOKEN}"}


async def test_search_by_a_tag_that_does_not_exist_returns_nothing(mealie):
    fake, client = mealie
    fake.add_recipe("Soupe de poireaux")  # untagged, no tag exists in Mealie at all
    assert await client.search_recipes(tag="protéines") == []


async def test_reader_has_no_write_methods_and_refuses_writes(mealie):
    fake, _ = mealie
    async with MealieReader(fake.base_url, fake.token) as reader:
        for name in ("create_recipe", "set_meal_plan_entry", "add_shopping_items"):
            assert not hasattr(reader, name)
        with pytest.raises(ValueError):
            await reader._request("POST", "/api/recipes", json={"name": "x"})
        assert fake.recipes == {}
        assert await reader.search_recipes() == []
