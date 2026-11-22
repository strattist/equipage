"""Meal rules, as pure functions: which days are exercise days, what kind of dinner
each day gets, where the next lunch comes from. The model picks recipes; it does not
decide these rules."""

import re
from dataclasses import dataclass
from datetime import date, timedelta

EXERCISE_KINDS = {"rowing", "hiit", "strength"}

_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Checked in this order: "HIIT ... exercises", "Light bodyweight strength" and
# "Rest or mobility" each contain several keywords and must resolve to the intended kind.
_KEYWORDS = [
    (re.compile(r"hiit"), "hiit"),
    (re.compile(r"strength"), "strength"),
    (re.compile(r"\brow"), "rowing"),
    (re.compile(r"walk"), "walk"),
    (re.compile(r"mobil"), "mobility"),
]


def kind_from_text(text: str) -> str:
    """Map an English profile description to a coach session kind."""
    lowered = text.lower()
    for pattern, kind in _KEYWORDS:
        if pattern.search(lowered):
            return kind
    return "rest"


def default_kinds_from_profile(profile: dict) -> dict[int, str]:
    """Weekday (0 = Monday) to session kind, from the profile's weekly structure."""
    week = profile.get("weekly_structure") or {}
    return {
        i: kind_from_text(str(week[key]))
        for i, key in enumerate(_WEEKDAY_KEYS)
        if key in week
    }


@dataclass(frozen=True)
class DayTarget:
    day: date
    training: str
    exercise: bool
    away: bool
    dinner_type: str | None
    lunch_from: date | None
    spare_portion: bool


def compute_targets(
    start: date,
    end: date,
    kinds: dict[date, str],
    away: set[date],
    default_week: dict[int, str],
) -> list[DayTarget]:
    """One target per day from `start` to `end` inclusive.

    `kinds` is the coach's plan; days it does not cover fall back to `default_week`.
    An away day skips lunch and dinner. The lunch of a day is the previous evening's
    dinner, which is looked up even before `start`. A dinner cooked the evening before an
    away day still makes 2 portions, so its second one is flagged `spare_portion`."""
    one_day = timedelta(days=1)
    targets = []
    day = start
    while day <= end:
        training = kinds.get(day) or default_week.get(day.weekday(), "rest")
        exercise = training in EXERCISE_KINDS
        is_away = day in away
        previous = day - one_day
        targets.append(
            DayTarget(
                day=day,
                training=training,
                exercise=exercise,
                away=is_away,
                dinner_type=None if is_away else ("protein" if exercise else "veggie"),
                lunch_from=None if is_away or previous in away else previous,
                spare_portion=not is_away and (day + one_day) in away,
            )
        )
        day += one_day
    return targets
