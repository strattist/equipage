from datetime import date

import pytest

from app.mealie import MealieUnavailable
from app.meals import store
from app.meals.commit import CommitResult, EmptyDraft, StaleDraft, commit_draft

MON, TUE, WED = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)
LIST_NAME = "Courses du 23/11/2026 au 24/11/2026"


def new_dinner(day, name, dinner_type="veggie"):
    return store.draft_set_dinner(
        day, "new", name, dinner_type, ingredients=["a", "b"], steps=["1. Cuire."]
    )


def entries(fake):
    return sorted((e["date"], e["entryType"]) for e in fake.entries)


async def test_commit_writes_recipes_plan_and_shopping(tmp_db, mealie):
    fake, client = mealie
    fake.add_recipe("Soupe")
    new_dinner(MON, "Curry de pois chiches")
    store.draft_set_dinner(TUE, "existing", "Soupe", "veggie", recipe_slug="soupe")
    store.draft_set_shopping(["lentilles", "citron"])
    version = store.draft_get().version

    result = await commit_draft(version, client)

    assert result == CommitResult(recipes_created=1, plan_entries=4, shopping_items=2)
    curry = fake.recipes["curry-de-pois-chiches"]
    assert curry["recipeServings"] == 2 and [t["name"] for t in curry["tags"]] == [
        "végétarien"
    ]
    assert entries(fake) == [
        ("2026-11-23", "dinner"),
        ("2026-11-24", "dinner"),
        ("2026-11-24", "lunch"),
        ("2026-11-25", "lunch"),
    ]
    [shopping_list] = fake.lists.values()
    assert shopping_list["name"] == LIST_NAME
    assert [i["note"] for i in shopping_list["listItems"]] == ["lentilles", "citron"]
    assert (
        store.draft_get().version == version
    )  # marking progress never invalidates the table


async def test_new_recipe_tag_follows_the_dinner_type(tmp_db, mealie):
    fake, client = mealie
    new_dinner(MON, "Poulet rôti", "protein")
    new_dinner(TUE, "Curry", "veggie")
    await commit_draft(store.draft_get().version, client)
    assert [t["name"] for t in fake.recipes["poulet-roti"]["tags"]] == ["protéines"]
    assert [t["name"] for t in fake.recipes["curry"]["tags"]] == ["végétarien"]


async def test_stale_version_raises_and_writes_nothing(tmp_db, mealie):
    fake, client = mealie
    new_dinner(MON, "Curry")
    old_version = store.draft_get().version
    new_dinner(TUE, "Soupe")  # the draft moved on
    with pytest.raises(StaleDraft):
        await commit_draft(old_version, client)
    assert (fake.recipes, fake.entries, fake.lists, fake.requests) == ({}, [], {}, [])


async def test_empty_draft_raises(tmp_db, mealie):
    fake, client = mealie
    with pytest.raises(EmptyDraft):
        await commit_draft(store.draft_get().version, client)
    store.draft_set_shopping(["lait"])  # a shopping list alone is not a plan
    with pytest.raises(EmptyDraft):
        await commit_draft(store.draft_get().version, client)
    assert fake.requests == []


async def test_no_lunch_entry_before_away_day(tmp_db, mealie):
    fake, client = mealie
    fake.add_recipe("Soupe")
    store.draft_set_dinner(MON, "existing", "Soupe", "veggie", recipe_slug="soupe")
    store.away_set([TUE])
    result = await commit_draft(store.draft_get().version, client)
    assert result.plan_entries == 1
    assert entries(fake) == [("2026-11-23", "dinner")]


async def test_draft_without_shopping_list_writes_no_list(tmp_db, mealie):
    fake, client = mealie
    new_dinner(MON, "Curry")
    result = await commit_draft(store.draft_get().version, client)
    assert result.shopping_items == 0
    assert fake.lists == {}


async def test_retry_after_failure_creates_no_duplicates(tmp_db, mealie):
    fake, client = mealie
    new_dinner(MON, "Curry de pois chiches")
    new_dinner(TUE, "Soupe de poireaux")
    store.draft_set_shopping(["lentilles", "citron"])
    version = store.draft_get().version
    # meal plan POSTs: dinner Mon, lunch Tue, dinner Tue (the third one fails), lunch Wed
    fake.fail_next("POST", "/api/households/mealplans", 500, skip=2)

    with pytest.raises(MealieUnavailable):
        await commit_draft(version, client)

    draft = store.draft_get()
    assert draft.version == version
    assert (draft.dinners[MON].recipe_created, draft.dinners[MON].plan_written) == (
        True,
        True,
    )
    assert (draft.dinners[TUE].recipe_created, draft.dinners[TUE].plan_written) == (
        True,
        False,
    )
    assert draft.shopping_written is False
    assert fake.lists == {}

    result = await commit_draft(version, client)

    assert result == CommitResult(recipes_created=2, plan_entries=4, shopping_items=2)
    assert len(fake.recipes) == 2
    assert entries(fake) == [
        ("2026-11-23", "dinner"),
        ("2026-11-24", "dinner"),
        ("2026-11-24", "lunch"),
        ("2026-11-25", "lunch"),
    ]
    assert [i["note"] for lst in fake.lists.values() for i in lst["listItems"]] == [
        "lentilles",
        "citron",
    ]
    assert store.draft_get().shopping_written is True


async def test_retry_after_a_crash_between_mealie_and_the_flag_creates_no_duplicate(
    tmp_db, mealie
):
    fake, client = mealie
    new_dinner(MON, "Curry")
    fake.add_recipe("Curry")  # Mealie got the recipe but the flag was never written
    await commit_draft(store.draft_get().version, client)
    assert len(fake.recipes) == 1


async def test_commit_again_after_success_writes_nothing_new(tmp_db, mealie):
    fake, client = mealie
    new_dinner(MON, "Curry")
    store.draft_set_shopping(["lait"])
    version = store.draft_get().version
    await commit_draft(version, client)
    written = len(fake.requests)
    again = await commit_draft(version, client)
    assert again == CommitResult(recipes_created=1, plan_entries=2, shopping_items=1)
    assert [
        r["method"] for r in fake.requests[written:]
    ] == []  # nothing left to do, nothing sent


class DraftEditingClient:
    """A Mealie stand-in that lets the user change the draft while the commit runs."""

    def __init__(self):
        self.plan_calls = []

    async def create_recipe(self, name, ingredients, steps, tags, servings=2):
        new_dinner(TUE, "Autre recette")  # the draft changes mid-commit
        return "curry"

    async def set_meal_plan_entry(self, day, slot, recipe_slug):
        self.plan_calls.append((day, slot))

    async def add_shopping_items(self, items, list_name):
        self.plan_calls.append(("shopping",))


async def test_draft_edited_during_commit_stops_it_without_marking_the_changed_draft(
    tmp_db,
):
    new_dinner(MON, "Curry")
    new_dinner(TUE, "Soupe")
    client = DraftEditingClient()
    with pytest.raises(StaleDraft):
        await commit_draft(store.draft_get().version, client)
    draft = store.draft_get()
    assert (draft.dinners[MON].recipe_created, draft.dinners[TUE].recipe_created) == (
        False,
        False,
    )
    assert client.plan_calls == []
