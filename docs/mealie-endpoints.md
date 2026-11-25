# Mealie endpoints used by the nutritionist

Taken from the user's instance (`/openapi.json`, Mealie **v3.31.0**, downloaded 2026-11-20) and checked with read-only calls (`GET`). The `POST`/`PATCH` calls below follow the documented schemas but have **not** been executed against the real instance yet: they are exercised for the first time in the live verification (nutritionist plan, Task 9).

Base URL: `MEALIE_URL` (no trailing slash). Every path starts with `/api`.

## Authentication

`Authorization: Bearer <MEALIE_API_TOKEN>` on every request (API token created at `/user/profile/api-tokens`). Checked: `GET /api/app/about` and the read calls below answer `200` with it. A wrong token gives `401`.

### Tokens

The nutritionist uses two tokens (`MEALIE_READ_TOKEN`, `MEALIE_WRITE_TOKEN`). **Mealie tokens carry all the rights of the user that created them and cannot be restricted**, and Mealie has no read-only role (reported on the user's instance, 2026-11-25). The two tokens are therefore equally powerful: they can be revoked separately, and that is all Mealie enforces.

The separation is enforced by the bot: `MealieReader` only sends `GET` (any other method raises before a request is made), and only the Confirmer commit uses `MealieClient`. A compromised bot container would still hold a token that can do anything the Mealie user can. `docs/mealie-gate.md` documents a proxy that would enforce the separation outside the bot; it is not built.

All list endpoints are paginated: `{"page", "per_page", "total", "total_pages", "items", "next", "previous"}`; query parameters `page` and `perPage`.

## 1. Search recipes: `GET /api/recipes`

| Query parameter | Use |
|---|---|
| `search` | free text on the recipe name |
| `tags` | repeatable; tag **slug** or id (`requireAllTags=false` = any of them) |
| `page`, `perPage` | pagination |
| `orderBy=name`, `orderDirection=asc` | stable order |

Response items (`RecipeSummary`) carry `id`, `slug`, `name`, `recipeServings`, `tags: [{id, name, slug, recipeCount}]`, `recipeCategory`, and more fields that are not used.

## 2. Get a recipe: `GET /api/recipes/{slug}`

`RecipeOut`: `id`, `slug`, `name`, `recipeServings`, `tags`, `recipeIngredient: [{note, display, quantity, unit, food, referenceId, ...}]`, `recipeInstructions: [{id, title, summary, text, ...}]`. `404` if the slug does not exist.

## 3. Create a recipe

Two calls, because the create endpoint only takes a name:

1. `POST /api/recipes` with `{"name": "..."}`. Answers `201` with the **slug as a JSON string**. If the name is already used Mealie creates a second recipe with a suffixed slug (`name-1`): the client must look for an existing recipe by name first.
2. `PATCH /api/recipes/{slug}` (partial update) with:

```json
{
  "recipeServings": 2,
  "recipeIngredient": [{"note": "400 g de lentilles"}],
  "recipeInstructions": [{"text": "Faire cuire les lentilles."}],
  "tags": [{"id": "<tag id>", "name": "protéines", "slug": "proteines"}]
}
```

Tags must exist before they are attached: `GET /api/organizers/tags?search=<name>` and, if there is no exact (case-insensitive) match, `POST /api/organizers/tags` with `{"name": "..."}` (answers the tag with `id`, `name`, `slug`).

## 4. Meal plan entries

- `GET /api/households/mealplans?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&perPage=100` → entries `{id, date, entryType, title, text, recipeId, recipeScale, recipe}`.
- `POST /api/households/mealplans` with `{"date": "YYYY-MM-DD", "entryType": "dinner", "title": "", "text": "", "recipeId": "<recipe id>"}` → the created entry. `entryType` is one of `breakfast, lunch, dinner, side, snack, drink, dessert`; the client uses `lunch` and `dinner`.

The entry takes the recipe **id** (uuid), not the slug: the client reads it with `GET /api/recipes/{slug}`.

## 5. Shopping lists and items

- `GET /api/households/shopping/lists?perPage=50` → lists `{id, name, ...}` (no items in this summary).
- `POST /api/households/shopping/lists` with `{"name": "..."}` → the created list.
- `GET /api/households/shopping/lists/{id}` → the list with `listItems: [{id, note, display, quantity, checked, ...}]`.
- `POST /api/households/shopping/items/create-bulk` with `[{"shoppingListId": "<list id>", "note": "Lentilles — 300 g"}]` → `{createdItems, updatedItems, deletedItems}`.

## What the user's instance contains (2026-11-20)

These findings shaped the client:

- **32 recipes, all in French, none tagged** (there are no tags at all). The convention `protéines` / `végétarien` (plan Task 4) therefore applies to the recipes the nutritionist creates; for existing recipes the dinner type is judged from the name and ingredients (`search_recipes` without a tag, then `get_recipe`).
- Recipes are for 2 servings (`recipeServings: 2.0`). Ingredients are **free text**: `note` holds the text, `food` and `unit` are null, `quantity` is `0`, and `display` equals the note. Steps are plain `text`.
- Meal plan: each cooked dinner has a `dinner` entry and the next day a `lunch` entry, both with `recipeId` set and an empty `title`, which is what the client writes.
- Shopping lists: **there is no default list** in Mealie v3 (the summary has no such flag). The user keeps one list per shopping trip, named `Courses du dd/mm/YYYY au dd/mm/YYYY`. Items are free text in `note`, with the default `quantity` 1.0, so the client sends only `shoppingListId` and `note`. The client finds or creates a list by name (`add_shopping_items(items, list_name)`), and the commit names it after the draft's first and last dinner day.
