"""Async client for the user's Mealie (REST, API token). Endpoints and payload shapes are
recorded in docs/mealie-endpoints.md, taken from the user's own instance.

Write methods are idempotent so a commit that failed halfway can be pressed again
without duplicating recipes, meal plan entries or shopping items. Error text never
contains the API token."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Self
from urllib.parse import quote

import aiohttp

TIMEOUT_SECONDS = 15
MAX_PAGES = 20
SLOTS = ("lunch", "dinner")

# Mealie tags are saved in French: they are what the user sees in Mealie.
MEALIE_TAGS = {"protein": "protéines", "veggie": "végétarien"}


class MealieError(Exception):
    """Mealie answered with an error that retrying will not fix (404, 422, ...)."""


class MealieUnavailable(MealieError):
    """Connection failure, timeout or 5xx: Mealie may work again later."""


class MealieAuthError(MealieError):
    """The API token was rejected (401/403)."""


@dataclass
class RecipeSummary:
    slug: str
    name: str
    tags: list[str]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _clean(items: Iterable[str]) -> list[str]:
    """Whitespace-collapsed, non-empty, unique (case-insensitive), in order."""
    seen, cleaned = set(), []
    for item in items:
        text = " ".join(item.split())
        if text and _norm(text) not in seen:
            seen.add(_norm(text))
            cleaned.append(text)
    return cleaned


class MealieClient:
    def __init__(self, base_url: str, token: str):
        self._base = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- plumbing -----------------------------------------------------------

    async def _request(self, method: str, path: str, *, params=None, json=None):
        if self._session is None:
            raise RuntimeError("MealieClient must be used as an async context manager")
        try:
            async with self._session.request(
                method, self._base + path, params=params, json=json
            ) as response:
                status = response.status
                if status in (401, 403):
                    raise MealieAuthError("Mealie rejected the API token")
                if status >= 500:
                    raise MealieUnavailable(
                        f"Mealie server error ({status}) on {method} {path}"
                    )
                if status >= 400:
                    raise MealieError(f"Mealie answered {status} on {method} {path}")
                if status == 204:
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise MealieUnavailable(
                f"Cannot reach Mealie ({type(exc).__name__})"
            ) from exc

    async def _all_pages(self, path: str, params: list[tuple]) -> list[dict]:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._request(
                "GET", path, params=[*params, ("page", page), ("perPage", 100)]
            )
            items += data["items"]
            if page >= data["total_pages"]:
                break
        return items

    # --- recipes ------------------------------------------------------------

    async def _find_tag(self, name: str) -> dict | None:
        for tag in await self._all_pages("/api/organizers/tags", [("search", name)]):
            if _norm(tag["name"]) == _norm(name):
                return tag
        return None

    async def _ensure_tag(self, name: str) -> dict:
        tag = await self._find_tag(name)
        if tag is None:
            tag = await self._request(
                "POST", "/api/organizers/tags", json={"name": name}
            )
        return {"id": tag["id"], "name": tag["name"], "slug": tag["slug"]}

    async def search_recipes(
        self, query: str = "", tag: str | None = None, limit: int = 10
    ) -> list[RecipeSummary]:
        """Recipes by name and/or tag (the tag's Mealie text, e.g. "végétarien"), by name order."""
        params: list[tuple] = [
            ("page", 1),
            ("perPage", limit),
            ("orderBy", "name"),
            ("orderDirection", "asc"),
        ]
        if query:
            params.append(("search", query))
        if tag:
            found = await self._find_tag(tag)
            if found is None:
                return []  # nothing can carry a tag that does not exist
            params.append(("tags", found["slug"]))
        data = await self._request("GET", "/api/recipes", params=params)
        return [
            RecipeSummary(
                slug=r["slug"],
                name=r["name"],
                tags=[t["name"] for t in r.get("tags", [])],
            )
            for r in data["items"]
        ]

    async def _recipe(self, slug: str) -> dict:
        return await self._request("GET", f"/api/recipes/{quote(slug, safe='')}")

    async def get_recipe(self, slug: str) -> dict:
        """Name, servings, tags, ingredients and steps, as plain strings."""
        recipe = await self._recipe(slug)
        servings = recipe.get("recipeServings") or 0
        return {
            "slug": recipe["slug"],
            "name": recipe["name"],
            "servings": int(servings) if float(servings).is_integer() else servings,
            "tags": [t["name"] for t in recipe.get("tags", [])],
            "ingredients": [
                i.get("display") or i.get("note") or ""
                for i in recipe.get("recipeIngredient", [])
            ],
            "steps": [s["text"] for s in recipe.get("recipeInstructions", [])],
        }

    async def _find_recipe_by_name(self, name: str) -> dict | None:
        data = await self._request(
            "GET",
            "/api/recipes",
            params=[
                ("search", name),
                ("page", 1),
                ("perPage", 50),
                ("orderBy", "name"),
            ],
        )
        for item in data["items"]:
            if _norm(item["name"]) == _norm(name):
                return await self._recipe(item["slug"])
        return None

    async def create_recipe(
        self,
        name: str,
        ingredients: list[str],
        steps: list[str],
        tags: list[str],
        servings: int = 2,
    ) -> str:
        """Create a recipe and return its slug.

        A recipe with that exact name is never duplicated: its slug is returned and a filled
        recipe is left untouched. An empty one (left by an attempt that created it and then
        failed) is completed."""
        existing = await self._find_recipe_by_name(name)
        if existing is not None and (
            existing.get("recipeIngredient") or existing.get("recipeInstructions")
        ):
            return existing["slug"]
        slug = (
            existing["slug"]
            if existing is not None
            else await self._request("POST", "/api/recipes", json={"name": name})
        )
        tag_refs = [await self._ensure_tag(t) for t in _clean(tags)]
        await self._request(
            "PATCH",
            f"/api/recipes/{quote(slug, safe='')}",
            json={
                "recipeServings": servings,
                "recipeIngredient": [{"note": i} for i in _clean(ingredients)],
                "recipeInstructions": [{"text": s} for s in steps if s.strip()],
                "tags": tag_refs,
            },
        )
        return slug

    # --- meal plan ----------------------------------------------------------

    async def set_meal_plan_entry(self, day: date, slot: str, recipe_slug: str) -> None:
        """Put the recipe in the day's lunch or dinner slot, unless it is already there."""
        if slot not in SLOTS:
            raise ValueError(f"slot must be one of {', '.join(SLOTS)}, got {slot!r}")
        recipe_id = (await self._recipe(recipe_slug))["id"]
        existing = await self._all_pages(
            "/api/households/mealplans",
            [("start_date", day.isoformat()), ("end_date", day.isoformat())],
        )
        if any(
            e["entryType"] == slot
            and e["recipeId"] == recipe_id
            and e["date"] == day.isoformat()
            for e in existing
        ):
            return
        await self._request(
            "POST",
            "/api/households/mealplans",
            json={
                "date": day.isoformat(),
                "entryType": slot,
                "title": "",
                "text": "",
                "recipeId": recipe_id,
            },
        )

    # --- shopping -----------------------------------------------------------

    async def add_shopping_items(self, items: list[str], list_name: str) -> None:
        """Add items to the shopping list called `list_name` (created if missing), skipping
        any whose text is already on the list unchecked. Mealie v3 has no default list."""
        wanted = _clean(items)
        if not wanted:
            return
        lists = await self._all_pages("/api/households/shopping/lists", [])
        found = next(
            (lst for lst in lists if _norm(lst["name"] or "") == _norm(list_name)), None
        )
        if found is None:
            found = await self._request(
                "POST", "/api/households/shopping/lists", json={"name": list_name}
            )
        current = await self._request(
            "GET", f"/api/households/shopping/lists/{found['id']}"
        )
        present = {
            _norm(i.get("note") or i.get("display") or "")
            for i in current.get("listItems", [])
            if not i.get("checked")
        }
        missing = [i for i in wanted if _norm(i) not in present]
        if missing:
            await self._request(
                "POST",
                "/api/households/shopping/items/create-bulk",
                json=[{"shoppingListId": found["id"], "note": i} for i in missing],
            )
