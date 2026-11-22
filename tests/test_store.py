from datetime import date

import pytest

from app import db
from app.meals import rules, store

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(23, 30))
DEFAULT_WEEK = {
    0: "rowing",
    1: "rest",
    2: "hiit",
    3: "walk",
    4: "rowing",
    5: "strength",
    6: "mobility",
}


def week_targets(away=None):
    return rules.compute_targets(MON, SUN, {}, away or set(), DEFAULT_WEEK)


def test_fridge_add_replace_remove(tmp_db):
    store.fridge_add("Œufs", "6")
    store.fridge_add("lentilles")
    store.fridge_add("œufs", "4")  # same item, other case: updated, not duplicated
    assert store.fridge_list() == [
        {"name": "lentilles", "quantity": ""},
        {"name": "œufs", "quantity": "4"},
    ]
    store.fridge_remove("LENTILLES")
    assert [i["name"] for i in store.fridge_list()] == ["œufs"]
    store.fridge_replace([("poireaux", "3"), ("citron", "")])
    assert [i["name"] for i in store.fridge_list()] == ["citron", "poireaux"]
    store.fridge_remove("not there")  # no error


def test_away_set_replaces_and_filters_by_range(tmp_db):
    store.away_set([WED, THU, date(2026, 12, 5)])
    assert store.away_get(MON, SUN) == {WED, THU}
    store.away_set([FRI])
    assert store.away_get(MON, SUN) == {FRI}
    assert store.away_get(THU, THU) == set()
    store.away_set([])
    assert store.away_get(MON, SUN) == set()


def test_draft_version_increments_on_each_change(tmp_db):
    v0 = store.draft_get().version
    v1 = store.draft_set_dinner(
        MON, "existing", "Poulet rôti", "protein", recipe_slug="poulet-roti"
    )
    v2 = store.draft_set_dinner(
        TUE,
        "new",
        "Curry de pois chiches",
        "veggie",
        ingredients=["pois chiches"],
        steps=["Cuire"],
    )
    v3 = store.draft_set_shopping(["citron", "lait de coco"])
    v4 = store.draft_set_new_recipe_count(2)
    assert [v1, v2, v3, v4] == [v0 + 1, v0 + 2, v0 + 3, v0 + 4]
    draft = store.draft_get()
    assert draft.version == v4 and draft.new_recipe_count == 2
    assert list(draft.dinners) == [MON, TUE]
    assert draft.dinners[TUE].ingredients == ["pois chiches"] and draft.dinners[
        TUE
    ].steps == ["Cuire"]
    assert draft.shopping == ["citron", "lait de coco"]
    v5 = store.draft_clear()
    cleared = store.draft_get()
    assert v5 == v4 + 1 and cleared.version == v5
    assert cleared.dinners == {} and cleared.shopping == []


def test_changing_dinner_resets_commit_flags(tmp_db):
    store.draft_set_dinner(
        MON, "new", "Curry", "veggie", ingredients=["a"], steps=["b"]
    )
    store.draft_set_dinner(
        TUE, "new", "Soupe", "veggie", ingredients=["a"], steps=["b"]
    )
    with db.connect() as conn:
        conn.execute("UPDATE draft_dinners SET recipe_created = 1, plan_written = 1")
        conn.execute("UPDATE draft_meta SET shopping_written = 1")
    store.draft_set_dinner(
        MON, "new", "Curry coco", "veggie", ingredients=["a"], steps=["b"]
    )
    draft = store.draft_get()
    assert (draft.dinners[MON].recipe_created, draft.dinners[MON].plan_written) == (
        False,
        False,
    )
    assert (draft.dinners[TUE].recipe_created, draft.dinners[TUE].plan_written) == (
        True,
        True,
    )
    assert draft.shopping_written is False


def test_changing_the_shopping_list_resets_its_flag(tmp_db):
    store.draft_set_shopping(["lait"])
    with db.connect() as conn:
        conn.execute("UPDATE draft_meta SET shopping_written = 1")
    store.draft_set_shopping(["lait", "citron"])
    assert store.draft_get().shopping_written is False


def test_invalid_source_raises(tmp_db):
    version = store.draft_get().version
    with pytest.raises(ValueError):
        store.draft_set_dinner(MON, "invented", "x", "veggie")
    assert store.draft_get().version == version
    assert store.draft_get().dinners == {}


def test_negative_new_recipe_count_raises(tmp_db):
    with pytest.raises(ValueError):
        store.draft_set_new_recipe_count(-1)


def test_multiline_ingredients_do_not_corrupt_storage(tmp_db):
    store.draft_set_dinner(
        MON,
        "new",
        "Curry",
        "veggie",
        ingredients=["pois\nchiches", "  "],
        steps=["1. Cuire\n2. Servir"],
    )
    dinner = store.draft_get().dinners[MON]
    assert dinner.ingredients == ["pois chiches"]
    assert dinner.steps == ["1. Cuire 2. Servir"]


def test_render_table_marks_new_existing_away_and_spare(tmp_db):
    store.draft_set_dinner(
        MON, "existing", "Poulet rôti, courge", "protein", recipe_slug="poulet"
    )
    store.draft_set_dinner(
        TUE, "new", "Curry de pois chiches", "veggie", ingredients=["a"], steps=["b"]
    )
    text = store.render_table(store.draft_get(), week_targets(away={THU}))
    assert text.startswith("```\n") and text.endswith("\n```")
    lines = text.splitlines()[1:-1]
    assert lines[0].split()[:3] == ["Date", "Entraînement", "Dîner"]
    assert "Déj. lendemain" in lines[0]
    by_day = {line.split()[0] + " " + line.split()[1]: line for line in lines[1:]}
    assert "rameur" in by_day["Lun 23/11"] and "protéines" in by_day["Lun 23/11"]
    assert "Poulet rôti, courge (existante)" in by_day["Lun 23/11"] and by_day[
        "Lun 23/11"
    ].endswith("Mar: reste")
    assert (
        "Curry de pois chiches (NOUVELLE)" in by_day["Mar 24/11"]
        and "végé" in by_day["Mar 24/11"]
    )
    assert "— absent(e) —" in by_day["Jeu 26/11"] and by_day["Jeu 26/11"].endswith(
        "Ven: vide"
    )
    assert "portion en trop" in by_day["Mer 25/11"]
    assert "Jeu: vide ⚠ portion en trop" in by_day["Mer 25/11"]
    assert "portion en trop" not in by_day["Lun 23/11"]
    assert by_day["Mer 25/11"].split()[2] == "HIIT"
    assert (
        by_day["Sam 28/11"].count("—") == 1
    )  # not drafted yet: only the recipe cell shows a dash


def test_render_table_fits_discord_limit(tmp_db):
    days = [MON, TUE, WED, THU, FRI, SAT, SUN]
    for i, day in enumerate(days):
        name = f"Gratin de légumes d'hiver à la crème fraîche épaisse et au thym frais {i}".ljust(
            80, "é"
        )
        store.draft_set_dinner(
            day,
            "new" if i % 2 else "existing",
            name,
            "protein",
            recipe_slug="x",
            ingredients=["a"],
            steps=["b"],
        )
    text = store.render_table(store.draft_get(), week_targets(away={SUN}))
    lines = text.splitlines()[1:-1]
    assert len(lines) == 8
    assert len(text) <= 1900
    assert max(len(line) for line in lines) <= 100
    assert all("…" in line for line in lines[1:7])
    recette_col = lines[0].index("Recette")
    for line in lines[1:]:
        assert line[recette_col] != " " and line[recette_col - 1] == " "
    assert (
        "(NOUVELLE)" in text and "(existante)" in text
    )  # the suffix survives truncation


def test_render_table_with_empty_draft_and_no_targets(tmp_db):
    text = store.render_table(store.draft_get(), [])
    assert text.startswith("```") and "Date" in text


def test_existing_dinner_requires_a_recipe_slug(tmp_db):
    version = store.draft_get().version
    with pytest.raises(ValueError):
        store.draft_set_dinner(MON, "existing", "Poulet rôti", "protein")
    assert store.draft_get().version == version


def test_new_dinner_requires_ingredients_and_steps(tmp_db):
    with pytest.raises(ValueError):
        store.draft_set_dinner(
            MON, "new", "Curry", "veggie", ingredients=["pois chiches"], steps=[]
        )
    with pytest.raises(ValueError):
        store.draft_set_dinner(
            MON, "new", "Curry", "veggie", ingredients=[" "], steps=["Cuire."]
        )
    assert store.draft_get().dinners == {}
