import json

import pytest

from app import config, db, exercises


@pytest.fixture
def catalog(tmp_db):
    exercises.seed()
    return {e["name"]: e for e in exercises.list_all()}


def url_of(name):
    return exercises.get(name)["url"]


def test_seed_inserts_all_exercises_once_and_keeps_user_edits(tmp_db):
    seed = json.loads(
        (config.ROOT / "app" / "exercises_seed.json").read_text(encoding="utf-8")
    )
    exercises.seed()
    exercises.seed()
    assert len(exercises.list_all()) == len(seed) == 12
    assert exercises.set_url("skaters", "https://example.com/my-skaters-video")
    exercises.seed()
    assert exercises.get("skaters")["url"] == "https://example.com/my-skaters-video"
    with db.connect() as conn:
        assert conn.execute("select count(*) from exercises").fetchone()[0] == 12


def test_seed_urls_are_youtube_searches(catalog):
    for e in catalog.values():
        assert e["url"].startswith("https://www.youtube.com/results?search_query=")
        assert e["url"].endswith("+exercice")
        assert e["explanation"].strip()


def test_get_matches_alias_and_ignores_case_and_accents(catalog):
    expected = exercises.get("fentes arrière")
    assert expected is not None and expected["name"] == "Fentes arrière"
    assert exercises.get("FENTES ARRIERE") == expected
    assert exercises.get("reverse lunges") == expected
    assert exercises.get("  Fentes Arrière ") == expected
    assert exercises.get("yoga du rire") is None


def test_set_url_rejects_non_http_and_unknown(catalog):
    before = url_of("skaters")
    assert exercises.set_url("skaters", "javascript:alert(1)") is False
    assert exercises.set_url("skaters", "ftp://example.com/v") is False
    assert exercises.set_url("skaters", "https://") is False
    assert exercises.set_url("skaters", "https://exa mple.com/v") is False
    assert exercises.set_url("unknown exercise", "https://example.com") is False
    assert url_of("skaters") == before
    assert exercises.set_url("Skaters", "http://example.com/v") is True
    assert url_of("skaters") == "http://example.com/v"


def test_linkify_links_known_names(catalog):
    out = exercises.linkify("Mountain climbers puis squats")
    assert out == (
        f"[Mountain climbers]({url_of('mountain climbers')}) puis [squats]({url_of('squats')})"
    )


def test_linkify_longest_match_wins(catalog):
    out = exercises.linkify("Fais des squat jumps.")
    assert out == f"Fais des [squat jumps]({url_of('squat jumps')})."
    assert url_of("squat jumps") != url_of("squats")


def test_linkify_skips_code_blocks_inline_code_and_existing_links(catalog):
    text = (
        "```\nsquats\n```\n"
        "Tape `squats` ici. "
        "Vois [mes squats](https://example.com/x) et gainage."
    )
    out = exercises.linkify(text)
    assert out.startswith(
        "```\nsquats\n```\nTape `squats` ici. Vois [mes squats](https://example.com/x) et "
    )
    assert out.endswith(f"[gainage]({url_of('gainage')}).")


def test_linkify_is_idempotent(catalog):
    text = "Lundi: rameur sprint, squats, fentes arrière, pompes inclinées et bird-dog."
    once = exercises.linkify(text)
    assert once != text
    assert exercises.linkify(once) == once


def test_linkify_leaves_unknown_words_and_partial_words(catalog):
    text = "Un squatter fait du yoga, et des superskaters."
    assert exercises.linkify(text) == text


def test_linkify_leaves_exercise_without_url_unlinked(catalog):
    with db.connect() as conn:
        conn.execute("update exercises set url = NULL where key = 'skaters'")
    assert exercises.linkify("skaters") == "skaters"


def test_linkify_leaves_bare_urls_intact(catalog):
    url = url_of("skaters")
    text = f"Regarde ici : {url} et https://example.com/squats?x=gainage."
    assert exercises.linkify(text) == text
