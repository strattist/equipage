import json
from datetime import date

from app import config
from app.meals import rules

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(23, 30))
START, END = MON, SUN

PROFILE = json.loads(
    (config.ROOT / "data" / "profile.example.json").read_text(encoding="utf-8")
)
DEFAULT_WEEK = rules.default_kinds_from_profile(PROFILE)


def targets(kinds=None, away=None, start=START, end=END):
    result = rules.compute_targets(start, end, kinds or {}, away or set(), DEFAULT_WEEK)
    return {t.day: t for t in result}


def test_exercise_days_get_protein_dinner():
    t = targets()
    assert [t[d].dinner_type for d in (MON, WED, FRI, SAT)] == ["protein"] * 4
    assert [t[d].dinner_type for d in (TUE, THU, SUN)] == ["veggie"] * 3
    assert [d for d in t if t[d].exercise] == [MON, WED, FRI, SAT]


def test_coach_plan_overrides_default():
    t = targets(kinds={MON: "walk"})
    assert t[MON].exercise is False
    assert t[MON].dinner_type == "veggie"
    assert t[MON].training == "walk"


def test_coach_plan_can_turn_a_rest_day_into_an_exercise_day():
    t = targets(kinds={TUE: "hiit"})
    assert (t[TUE].exercise, t[TUE].dinner_type) == (True, "protein")


def test_away_day_has_no_dinner_and_no_lunch():
    t = targets(away={THU})
    assert t[THU].away is True
    assert t[THU].dinner_type is None
    assert t[THU].lunch_from is None
    assert t[FRI].lunch_from is None  # nothing was cooked on Thursday
    assert t[WED].spare_portion is True
    assert t[THU].spare_portion is False
    assert t[SAT].lunch_from == FRI


def test_first_day_lunch_uses_previous_day_unless_away():
    assert targets()[MON].lunch_from == date(2026, 11, 22)
    assert targets(away={date(2026, 11, 22)})[MON].lunch_from is None


def test_last_day_has_spare_portion_when_the_day_after_the_range_is_away():
    assert targets(away={date(2026, 11, 30)})[SUN].spare_portion is True
    assert targets()[SUN].spare_portion is False


def test_consecutive_away_days():
    t = targets(away={THU, FRI})
    assert t[WED].spare_portion is True
    assert t[THU].spare_portion is False  # away itself: nothing cooked
    assert t[FRI].spare_portion is False
    assert t[SAT].lunch_from is None  # Friday was away: no leftover
    assert t[SUN].lunch_from == SAT


def test_every_day_away_gives_no_meals_at_all():
    t = targets(away=set(targets()))
    assert all(
        x.dinner_type is None and x.lunch_from is None and not x.spare_portion
        for x in t.values()
    )


def test_range_is_inclusive_and_empty_when_reversed():
    assert list(targets(start=WED, end=THU)) == [WED, THU]
    assert list(targets(start=SUN, end=MON)) == []


def test_missing_weekday_in_default_week_counts_as_rest():
    result = rules.compute_targets(MON, MON, {}, set(), {})
    assert (result[0].training, result[0].exercise, result[0].dinner_type) == (
        "rest",
        False,
        "veggie",
    )


def test_kind_from_text():
    ws = PROFILE["weekly_structure"]
    assert {k: rules.kind_from_text(v) for k, v in ws.items()} == {
        "mon": "rowing",
        "tue": "rest",
        "wed": "hiit",
        "thu": "walk",
        "fri": "rowing",
        "sat": "strength",
        "sun": "mobility",
    }
    assert rules.kind_from_text("ROWER intervals") == "rowing"
    assert rules.kind_from_text("Day off") == "rest"
    assert rules.kind_from_text("") == "rest"


def test_default_kinds_from_profile_maps_weekdays():
    assert DEFAULT_WEEK == {
        0: "rowing",
        1: "rest",
        2: "hiit",
        3: "walk",
        4: "rowing",
        5: "strength",
        6: "mobility",
    }
    assert rules.default_kinds_from_profile({}) == {}
