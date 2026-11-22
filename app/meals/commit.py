"""Commit the confirmed draft to Mealie: new recipes, meal plan entries (dinner + next-day
leftover lunch) and the shopping list. Runs from the Confirmer button, never from the model.

Every step is idempotent and its progress is stored in the draft, so a commit that failed
halfway can simply be pressed again: nothing is created twice."""

from dataclasses import dataclass
from datetime import date, timedelta

from app.mealie import MEALIE_TAGS, MealieClient
from app.meals import store
from app.meals.store import StaleDraft

__all__ = [
    "CommitResult",
    "EmptyDraft",
    "StaleDraft",
    "commit_draft",
    "shopping_list_name",
]

PORTIONS = 2


class EmptyDraft(Exception):
    """There is no dinner in the draft: nothing to confirm."""


@dataclass(frozen=True)
class CommitResult:
    recipes_created: int
    plan_entries: int
    shopping_items: int


def shopping_list_name(first: date, last: date) -> str:
    """The user keeps one shopping list per trip, named like this in Mealie."""
    return f"Courses du {first:%d/%m/%Y} au {last:%d/%m/%Y}"


async def commit_draft(expected_version: int, client: MealieClient) -> CommitResult:
    """Write the draft to Mealie. Counts describe the whole draft, so a retry reports the same totals.

    Raises StaleDraft if the draft is not at `expected_version` (at the start, or if it changes
    while committing) and EmptyDraft if it has no dinner. Mealie errors propagate unchanged."""
    draft = store.draft_get()
    if draft.version != expected_version:
        raise StaleDraft(f"draft is at version {draft.version}, not {expected_version}")
    if not draft.dinners:
        raise EmptyDraft("the draft has no dinner")

    one_day = timedelta(days=1)
    days = sorted(draft.dinners)
    recipes_created = plan_entries = 0
    for day in days:
        dinner = draft.dinners[day]
        slug = dinner.recipe_slug
        if dinner.source == "new":
            if not dinner.recipe_created:
                tag = MEALIE_TAGS.get(dinner.dinner_type or "")
                slug = await client.create_recipe(
                    dinner.name,
                    dinner.ingredients,
                    dinner.steps,
                    [tag] if tag else [],
                    servings=PORTIONS,
                )
                store.mark_recipe_created(day, slug, expected_version)
            recipes_created += 1
        lunch_next_day = (day + one_day) not in store.away_get(
            day + one_day, day + one_day
        )
        plan_entries += 2 if lunch_next_day else 1
        if not dinner.plan_written:
            await client.set_meal_plan_entry(day, "dinner", slug)
            if lunch_next_day:
                await client.set_meal_plan_entry(day + one_day, "lunch", slug)
            store.mark_plan_written(day, expected_version)

    if draft.shopping and not draft.shopping_written:
        await client.add_shopping_items(
            draft.shopping, shopping_list_name(days[0], days[-1])
        )
        store.mark_shopping_written(expected_version)
    return CommitResult(recipes_created, plan_entries, len(draft.shopping))
