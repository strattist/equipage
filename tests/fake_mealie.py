"""An in-memory Mealie, mirroring the paths and payloads recorded in docs/mealie-endpoints.md.

Behaviors copied from the real instance that matter for idempotency tests:
- POST /api/recipes with a name already used creates a second recipe with a suffixed slug;
- recipes only accept tags that already exist (by id);
- a created recipe starts empty, with 0 servings.
"""

import itertools
import re
import unicodedata
import uuid

from aiohttp import web


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


class FakeMealie:
    def __init__(self, token: str = "secret-token"):
        self.token = token
        self.recipes: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.entries: list[dict] = []
        self.lists: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.failures: list[dict] = []
        self._entry_ids = itertools.count(1)

    # --- test helpers -------------------------------------------------------

    def fail_next(
        self, method: str, path_prefix: str, status: int = 500, skip: int = 0
    ) -> None:
        """A matching request answers `status`, once, after `skip` matching requests went through."""
        self.failures.append(
            {
                "method": method.upper(),
                "prefix": path_prefix,
                "status": status,
                "skip": skip,
            }
        )

    def add_tag(self, name: str) -> dict:
        tag = {
            "id": str(uuid.uuid4()),
            "groupId": "g",
            "name": name,
            "slug": slugify(name),
            "recipeCount": 0,
        }
        self.tags[tag["id"]] = tag
        return tag

    def add_recipe(
        self, name: str, tags=(), ingredients=("x",), steps=("y",), servings=2.0
    ) -> dict:
        recipe = self._new_recipe(name)
        recipe["recipeServings"] = servings
        recipe["tags"] = [self.add_tag(t) if isinstance(t, str) else t for t in tags]
        recipe["recipeIngredient"] = [self._ingredient(i) for i in ingredients]
        recipe["recipeInstructions"] = [self._step(s) for s in steps]
        return recipe

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.add_routes(
            [
                web.get("/api/recipes", self.list_recipes),
                web.post("/api/recipes", self.create_recipe),
                web.get("/api/recipes/{slug}", self.get_recipe),
                web.patch("/api/recipes/{slug}", self.patch_recipe),
                web.get("/api/organizers/tags", self.list_tags),
                web.post("/api/organizers/tags", self.create_tag),
                web.get("/api/households/mealplans", self.list_entries),
                web.post("/api/households/mealplans", self.create_entry),
                web.get("/api/households/shopping/lists", self.list_lists),
                web.post("/api/households/shopping/lists", self.create_list),
                web.get("/api/households/shopping/lists/{id}", self.get_list),
                web.post(
                    "/api/households/shopping/items/create-bulk", self.create_items
                ),
            ]
        )
        return app

    # --- plumbing -----------------------------------------------------------

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        auth = request.headers.get("Authorization")
        self.requests.append(
            {"method": request.method, "path": request.path, "auth": auth}
        )
        if auth != f"Bearer {self.token}":
            return web.json_response({"detail": "Unauthorized"}, status=401)
        for failure in self.failures:
            if failure["method"] == request.method and request.path.startswith(
                failure["prefix"]
            ):
                if failure["skip"] > 0:
                    failure["skip"] -= 1
                    continue
                self.failures.remove(failure)
                return web.json_response({"detail": "boom"}, status=failure["status"])
        return await handler(request)

    @staticmethod
    def _page(request: web.Request, items: list) -> web.Response:
        page = int(request.query.get("page", 1))
        per_page = int(request.query.get("perPage", 50))
        chunk = items[(page - 1) * per_page : page * per_page]
        total_pages = max(1, -(-len(items) // per_page))
        return web.json_response(
            {
                "page": page,
                "per_page": per_page,
                "total": len(items),
                "total_pages": total_pages,
                "items": chunk,
                "next": None,
                "previous": None,
            }
        )

    # --- recipes ------------------------------------------------------------

    def _new_recipe(self, name: str) -> dict:
        base = slugify(name)
        slug, n = base, 0
        while slug in self.recipes:
            n += 1
            slug = f"{base}-{n}"
        recipe = {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "name": name,
            "recipeServings": 0.0,
            "tags": [],
            "recipeIngredient": [],
            "recipeInstructions": [],
        }
        self.recipes[slug] = recipe
        return recipe

    @staticmethod
    def _ingredient(text: str) -> dict:
        return {
            "quantity": 0.0,
            "unit": None,
            "food": None,
            "note": text,
            "display": text,
            "referenceId": str(uuid.uuid4()),
        }

    @staticmethod
    def _step(text: str) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "title": "",
            "summary": "",
            "text": text,
            "ingredientReferences": [],
        }

    @staticmethod
    def _summary(recipe: dict) -> dict:
        keys = ("id", "slug", "name", "recipeServings", "tags")
        return {k: recipe[k] for k in keys}

    async def list_recipes(self, request: web.Request) -> web.Response:
        search = request.query.get("search", "").casefold()
        wanted_tags = request.query.getall("tags", [])
        found = []
        for recipe in sorted(self.recipes.values(), key=lambda r: r["name"].casefold()):
            if search and search not in recipe["name"].casefold():
                continue
            slugs = {t["slug"] for t in recipe["tags"]} | {
                t["id"] for t in recipe["tags"]
            }
            if wanted_tags and not slugs & set(wanted_tags):
                continue
            found.append(self._summary(recipe))
        return self._page(request, found)

    async def create_recipe(self, request: web.Request) -> web.Response:
        body = await request.json()
        recipe = self._new_recipe(body["name"])
        return web.json_response(recipe["slug"], status=201)

    async def get_recipe(self, request: web.Request) -> web.Response:
        recipe = self.recipes.get(request.match_info["slug"])
        if recipe is None:
            return web.json_response({"detail": "Not Found"}, status=404)
        return web.json_response(recipe)

    async def patch_recipe(self, request: web.Request) -> web.Response:
        recipe = self.recipes.get(request.match_info["slug"])
        if recipe is None:
            return web.json_response({"detail": "Not Found"}, status=404)
        body = await request.json()
        if "tags" in body:
            unknown = [t for t in body["tags"] if t.get("id") not in self.tags]
            if unknown:
                return web.json_response({"detail": "unknown tag"}, status=422)
            recipe["tags"] = [self.tags[t["id"]] for t in body["tags"]]
        if "recipeServings" in body:
            recipe["recipeServings"] = float(body["recipeServings"])
        if "recipeIngredient" in body:
            recipe["recipeIngredient"] = [
                self._ingredient(i["note"]) for i in body["recipeIngredient"]
            ]
        if "recipeInstructions" in body:
            recipe["recipeInstructions"] = [
                self._step(s["text"]) for s in body["recipeInstructions"]
            ]
        return web.json_response(recipe)

    # --- tags ---------------------------------------------------------------

    async def list_tags(self, request: web.Request) -> web.Response:
        search = request.query.get("search", "").casefold()
        found = [t for t in self.tags.values() if search in t["name"].casefold()]
        return self._page(request, found)

    async def create_tag(self, request: web.Request) -> web.Response:
        name = (await request.json())["name"]
        if any(t["name"].casefold() == name.casefold() for t in self.tags.values()):
            return web.json_response({"detail": "tag exists"}, status=400)
        return web.json_response(self.add_tag(name), status=201)

    # --- meal plan ----------------------------------------------------------

    async def list_entries(self, request: web.Request) -> web.Response:
        start = request.query.get("start_date", "0000-00-00")
        end = request.query.get("end_date", "9999-99-99")
        found = [e for e in self.entries if start <= e["date"] <= end]
        return self._page(request, found)

    async def create_entry(self, request: web.Request) -> web.Response:
        body = await request.json()
        entry = {
            "id": next(self._entry_ids),
            "date": body["date"],
            "entryType": body["entryType"],
            "title": body.get("title", ""),
            "text": body.get("text", ""),
            "recipeId": body.get("recipeId"),
            "recipeScale": 1.0,
        }
        self.entries.append(entry)
        return web.json_response(entry, status=201)

    # --- shopping -----------------------------------------------------------

    async def list_lists(self, request: web.Request) -> web.Response:
        summaries = [
            {k: v for k, v in lst.items() if k != "listItems"}
            for lst in self.lists.values()
        ]
        return self._page(request, summaries)

    async def create_list(self, request: web.Request) -> web.Response:
        name = (await request.json())["name"]
        shopping_list = {"id": str(uuid.uuid4()), "name": name, "listItems": []}
        self.lists[shopping_list["id"]] = shopping_list
        return web.json_response(shopping_list, status=201)

    async def get_list(self, request: web.Request) -> web.Response:
        shopping_list = self.lists.get(request.match_info["id"])
        if shopping_list is None:
            return web.json_response({"detail": "Not Found"}, status=404)
        return web.json_response(shopping_list)

    async def create_items(self, request: web.Request) -> web.Response:
        created = []
        for item in await request.json():
            shopping_list = self.lists.get(item["shoppingListId"])
            if shopping_list is None:
                return web.json_response({"detail": "Not Found"}, status=404)
            stored = {
                "id": str(uuid.uuid4()),
                "shoppingListId": item["shoppingListId"],
                "note": item["note"],
                "display": f"1 {item['note']}",
                "quantity": 1.0,
                "checked": False,
            }
            shopping_list["listItems"].append(stored)
            created.append(stored)
        return web.json_response(
            {"createdItems": created, "updatedItems": [], "deletedItems": []},
            status=201,
        )
