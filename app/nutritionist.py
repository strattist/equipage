"""The nutritionist agent: tools over meal rules, the fridge, the draft and read-only Mealie.

Every tool returns a JSON string. Invalid input or a Mealie failure returns {"error": ...};
tools never raise. No tool writes to Mealie: the bot commits the stored draft when the user
presses the Confirmer button."""

import json
import os
from datetime import date, timedelta

from anthropic import beta_async_tool

from app import config, db
from app.coach import _day, _error, _Invalid, _json, _load_profile, get_profile
from app.mealie import MEALIE_TAGS, MealieClient, MealieError
from app.meals import rules, store
from app.runtime import WEEKDAYS, Agent

MAX_RANGE_DAYS = 31


def _date_range(start: str, end: str) -> tuple[date, date]:
    first, last = date.fromisoformat(_day(start, "start")), date.fromisoformat(
        _day(end, "end")
    )
    if last < first:
        raise _Invalid("end must not be before start")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise _Invalid(f"the range must not exceed {MAX_RANGE_DAYS} days")
    return first, last


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _coach_kinds(first: date, last: date) -> dict[date, str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date, kind FROM planned_sessions WHERE date BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
    return {date.fromisoformat(r["date"]): r["kind"] for r in rows}


def _default_week() -> dict[int, str]:
    try:
        profile = _load_profile()
    except json.JSONDecodeError:
        return {}
    return rules.default_kinds_from_profile(profile) if profile else {}


def meal_targets(first: date, last: date) -> list[rules.DayTarget]:
    return rules.compute_targets(
        first,
        last,
        _coach_kinds(first, last),
        store.away_get(first - timedelta(days=1), last),
        _default_week(),
    )


def _target_json(t: rules.DayTarget) -> dict:
    return {
        "day": t.day.isoformat(),
        "weekday": WEEKDAYS[t.day.weekday()],
        "training": t.training,
        "exercise": t.exercise,
        "away": t.away,
        "dinner_type": t.dinner_type,
        "lunch_from": t.lunch_from.isoformat() if t.lunch_from else None,
        "spare_portion": t.spare_portion,
        "portions": 2,
    }


@beta_async_tool
async def get_meal_targets(start: str, end: str) -> str:
    """Per-day meal rules from the coach's plan and the away days: dinner type, leftover lunch, spare portion.

    Args:
        start: First ISO date YYYY-MM-DD.
        end: Last ISO date YYYY-MM-DD (at most 31 days after start).
    """
    try:
        first, last = _date_range(start, end)
    except _Invalid as exc:
        return _error(str(exc))
    return _json({"days": [_target_json(t) for t in meal_targets(first, last)]})


# --- fridge and away days ------------------------------------------------------------


def _parse_items(text: str) -> list[tuple[str, str]]:
    items = []
    for line in _lines(text):
        name, _, quantity = line.partition(":")
        if name.strip():
            items.append((name.strip(), quantity.strip()))
    return items


@beta_async_tool
async def get_fridge() -> str:
    """List what is currently in the fridge."""
    return _json({"items": store.fridge_list()})


@beta_async_tool
async def add_fridge_items(items: str) -> str:
    """Add or update fridge items.

    Args:
        items: One item per line, as `name: quantity` (the quantity is optional free text).
    """
    parsed = _parse_items(items)
    for name, quantity in parsed:
        store.fridge_add(name, quantity)
    return _json({"ok": True, "added": len(parsed), "items": store.fridge_list()})


@beta_async_tool
async def remove_fridge_items(names: str) -> str:
    """Remove items from the fridge (names matched ignoring case).

    Args:
        names: One item name per line.
    """
    for name in _lines(names):
        store.fridge_remove(name)
    return _json({"ok": True, "items": store.fridge_list()})


@beta_async_tool
async def replace_fridge(items: str) -> str:
    """Replace the whole fridge content.

    Args:
        items: One item per line, as `name: quantity`. An empty text empties the fridge.
    """
    store.fridge_replace(_parse_items(items))
    return _json({"ok": True, "items": store.fridge_list()})


@beta_async_tool
async def set_away_days(days: str) -> str:
    """Set the days the user is away (no lunch, no dinner). Replaces the previous set.

    Args:
        days: One ISO date YYYY-MM-DD per line. An empty text means no away day.
    """
    try:
        parsed = sorted({date.fromisoformat(_day(d, "day")) for d in _lines(days)})
    except _Invalid as exc:
        return _error(str(exc))
    store.away_set(parsed)
    return _json({"ok": True, "away_days": [d.isoformat() for d in parsed]})


@beta_async_tool
async def get_away_days(start: str, end: str) -> str:
    """Away days between two dates, both included.

    Args:
        start: First ISO date YYYY-MM-DD.
        end: Last ISO date YYYY-MM-DD.
    """
    try:
        first, last = _date_range(start, end)
    except _Invalid as exc:
        return _error(str(exc))
    return _json(
        {"away_days": [d.isoformat() for d in sorted(store.away_get(first, last))]}
    )


# --- draft ---------------------------------------------------------------------------


def _draft_json(draft: store.Draft) -> dict:
    return {
        "version": draft.version,
        "new_recipe_count": draft.new_recipe_count,
        "dinners": [
            {
                "day": d.day.isoformat(),
                "source": d.source,
                "name": d.name,
                "dinner_type": d.dinner_type,
                "recipe_slug": d.recipe_slug,
                "ingredients": d.ingredients,
                "steps": d.steps,
            }
            for d in draft.dinners.values()
        ],
        "shopping": draft.shopping,
    }


@beta_async_tool
async def set_new_recipe_count(count: int) -> str:
    """Record how many brand-new recipes the user wants this week.

    Args:
        count: Number of new recipes, 0 or more.
    """
    try:
        version = store.draft_set_new_recipe_count(count)
    except ValueError as exc:
        return _error(str(exc))
    return _json({"ok": True, "new_recipe_count": count, "version": version})


@beta_async_tool
async def set_draft_dinner(
    day: str,
    source: str,
    name: str,
    recipe_slug: str = "",
    ingredients: str = "",
    steps: str = "",
) -> str:
    """Set the dinner of one day in the draft (nothing is written to Mealie until the user confirms).

    The dinner type (protein or veggie) comes from the meal rules, not from you. Recipes are
    always for 2 portions. Recipe name, ingredients and steps are written in French.

    Args:
        day: ISO date YYYY-MM-DD of the dinner. Refused for an away day.
        source: `existing` (a recipe already in Mealie, give recipe_slug) or `new` (give ingredients and steps).
        name: Recipe name, in French.
        recipe_slug: Mealie slug, required for an existing recipe.
        ingredients: For a new recipe, one ingredient per line, with quantities for 2 portions.
        steps: For a new recipe, one step per line.
    """
    try:
        target_day = date.fromisoformat(_day(day))
    except _Invalid as exc:
        return _error(str(exc))
    target = meal_targets(target_day, target_day)[0]
    if target.away:
        return _error(f"{day} is an away day: no dinner is cooked")
    try:
        version = store.draft_set_dinner(
            target_day,
            source,
            name,
            dinner_type=target.dinner_type,
            recipe_slug=recipe_slug or None,
            ingredients=_lines(ingredients),
            steps=_lines(steps),
        )
    except ValueError as exc:
        return _error(str(exc))
    return _json(
        {"ok": True, "day": day, "dinner_type": target.dinner_type, "version": version}
    )


@beta_async_tool
async def set_draft_shopping_list(items: str) -> str:
    """Set the draft shopping list (replaces it): ingredients to buy, minus what the fridge covers.

    Args:
        items: One shopping item per line, in French.
    """
    version = store.draft_set_shopping(_lines(items))
    return _json({"ok": True, "version": version})


@beta_async_tool
async def get_draft() -> str:
    """Read the current draft: version, requested new-recipe count, dinners and shopping list."""
    return _json(_draft_json(store.draft_get()))


@beta_async_tool
async def clear_draft() -> str:
    """Empty the draft dinners and shopping list (the requested new-recipe count is kept)."""
    return _json({"ok": True, "version": store.draft_clear()})


# --- Mealie (read-only) --------------------------------------------------------------


def mealie_client() -> MealieClient:
    url, token = os.environ.get("MEALIE_URL"), os.environ.get("MEALIE_API_TOKEN")
    if not url or not token:
        raise MealieError("Mealie is not configured (MEALIE_URL / MEALIE_API_TOKEN)")
    return MealieClient(url, token)


@beta_async_tool
async def search_recipes(query: str = "", tag: str = "") -> str:
    """Search the user's Mealie recipes by name and/or tag. Read-only.

    Args:
        query: Text to look for in recipe names (optional).
        tag: `protein`, `veggie`, or a Mealie tag text (optional).
    """
    try:
        async with mealie_client() as client:
            found = await client.search_recipes(
                query, MEALIE_TAGS.get(tag, tag) or None
            )
    except MealieError as exc:
        return _error(str(exc))
    return _json(
        {"recipes": [{"slug": r.slug, "name": r.name, "tags": r.tags} for r in found]}
    )


@beta_async_tool
async def get_recipe(slug: str) -> str:
    """Read one Mealie recipe (name, tags, ingredients, steps). Read-only.

    Args:
        slug: The recipe slug from search_recipes.
    """
    try:
        async with mealie_client() as client:
            return _json(await client.get_recipe(slug))
    except MealieError as exc:
        return _error(str(exc))


TOOLS = [
    get_meal_targets,
    get_profile,
    get_fridge,
    add_fridge_items,
    remove_fridge_items,
    replace_fridge,
    set_away_days,
    get_away_days,
    set_new_recipe_count,
    search_recipes,
    get_recipe,
    set_draft_dinner,
    set_draft_shopping_list,
    get_draft,
    clear_draft,
]

NUTRITIONIST = Agent(name="nutritionist", prompt_file="nutritionist.md", tools=TOOLS)
