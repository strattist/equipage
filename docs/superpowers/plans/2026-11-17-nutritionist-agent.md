# Nutritionist Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a French-speaking nutritionist agent in its own Discord channel that drafts a weekly dinner plan around the coach's exercise days, shows it as a table, and writes to Mealie only when the user presses a "Confirmer" button.

**Architecture:** Both agents share one run loop (`app/runtime.py`) and one SQLite database. Meal rules are pure code (`app/meals/rules.py`); the model picks recipes and stores a *draft* in SQLite through tools. The bot, not the model, commits the draft to Mealie when the button is pressed, so the model has no Mealie write tool.

**Tech Stack:** Python 3.12, `anthropic` tool runner, discord.py (`discord.ui.View`), APScheduler 3.x, SQLite, `aiohttp` (already installed with discord.py) for the Mealie client, pytest + pytest-asyncio.

**Depends on:** `docs/superpowers/plans/2026-11-17-coach-agent.md` (execute it first).

**Spec:** `docs/superpowers/specs/2026-11-17-nutritionist-design.md`

## Implementation record

**Status (2026-11-24):** Tasks 1-8 are implemented and merged to `main` (PR #2). The suite passes (220 tests on `main`, which also include the later security work of PR #3). Their steps are ticked from the merged code and the passing suite; whether each test was written before its code was not re-checked after the fact.

**Task 9 (live verification): done.** On 2026-11-25 the user reported that the live test against their real Discord server and Mealie worked. All six steps are ticked on that report; it gave no per-step detail, so the record holds nothing more specific than that. The nutritionist is implemented and verified live.

**Changes made after this plan** (PR #3, see `docs/superpowers/plans/2026-11-23-pull-based-coach-and-read-only-boundaries.md`): the single `MEALIE_API_TOKEN` is replaced by `MEALIE_READ_TOKEN` (recipe search, GET only) and `MEALIE_WRITE_TOKEN` (the Confirmer commit); the old variable still works with a warning. Task 9 step 1 should now set the two new tokens. The model's reads go through a read-only SQLite connection, and the nutritionist's fridge, draft and shopping inputs are length-limited and reject links and mentions.

**Repo rule (user's global instruction):** never run `git commit`; the project is not a git repo anyway. Leave all changes in the working tree. There are no commit steps in this plan.

## Global Constraints

- Language policy (see the coach spec): the nutritionist speaks French to the user (tutoiement) and writes all Mealie content in French (recipe names, ingredients, steps, tags, shopping items). Everything else is English: database values, code, prompts (English text telling the model to reply in French), README, commands, env vars, tool names. Discord text produced by the code (table headers, button label, error messages) is French, and so are the recipe contents stored in the draft tables because they are committed to Mealie unchanged.
- Every recipe is for 2 portions, always (`servings=2`); no scaling logic.
- Nothing is written to Mealie without the button press. No Mealie write function is exposed as a model tool. Read-only Mealie calls (search, get recipe) need no confirmation.
- Exercise day = coach `planned_sessions.kind` in `{"rowing","hiit","strength"}`; `walk`, `mobility`, `rest` or no plan = non-exercise (fallback: weekly structure in `data/profile.json`).
- Dinner type: `protein` on exercise days, `veggie` otherwise. Next-day lunch = leftover of the previous evening's dinner.
- Away day = whole day skipped (no lunch, no dinner). No dinner cooked that day → next day's lunch has no leftover. The dinner the evening before an away day is still 2 portions; flag it `spare_portion`.
- Weekly run: Sundays 10:00 (`NUTRITION_WEEKLY_HOUR`, default 10), tz `Europe/Paris`, state in SQLite `kv` so restarts cannot double-post.
- Secrets in `.env`: `MEALIE_URL`, `MEALIE_API_TOKEN`, `NUTRITION_CHANNEL_ID`. Only `ALLOWED_USER_ID` may talk to the bot or press the button.
- The button disappears once pressed; a re-posted table removes the button from the previous table.
- Table is a monospace code block (Discord does not render markdown tables), total message ≤ 2000 characters.
- The model's `CLAUDE_MODEL` setting applies to both agents.

## Review Focus

1. Away days at the edges: first or last day of the range, and several consecutive away days (spare-portion and empty-lunch flags must stay correct).
2. Button pressed twice quickly, or pressed on a table that is no longer the latest draft: must commit at most once and never commit a changed draft.
3. Commit fails halfway (Mealie error after some recipes were created): draft stays intact, button restored, retry creates no duplicate recipe, meal plan entry or shopping item.
4. Mealie unreachable or token rejected (401): plain French error, no traceback in Discord, draft intact.
5. Long output: 7 dinners with accented, long recipe names must still fit one Discord message in aligned columns; pressing confirm with an empty draft must say so.

---

## File Structure

- `app/runtime.py`, `app/coach.py`, `app/config.py` (exist, from the coach plan; not modified here).
- `app/meals/__init__.py`, `app/meals/rules.py` (new): pure meal rules.
- `app/meals/store.py` (new): fridge, away days, draft storage and versioning, table rendering.
- `app/mealie.py` (new): async Mealie REST client.
- `app/meals/commit.py` (new): draft → Mealie commit, idempotent.
- `app/meals/confirm.py` (new): Discord-free button logic (`ConfirmController`).
- `app/nutritionist.py` (new): tools + `NUTRITIONIST = Agent(...)`.
- `prompts/nutritionist.md` (new).
- `app/bot.py` (modify): channel registry, confirm button view, weekly job.
- `app/db.py` (modify): new tables.
- `docs/mealie-endpoints.md` (new): endpoints actually used, copied from the user's instance.
- `tests/` (new files): `test_rules.py`, `test_store.py`, `test_mealie.py`, `test_commit.py`, `test_confirm.py`, `test_nutritionist_tools.py`.
- `README.md`, `.env.example`, `app/config.py` (modify: new variables).

---

### Task 1: Prerequisite check (delivered by the coach plan)

This plan builds on `docs/superpowers/plans/2026-11-17-coach-agent.md`, which must be fully executed first. It provides the test harness (`pytest.ini`, `tests/conftest.py` with the `tmp_db` fixture), `app/config.py`, `app/db.py`, `app/runtime.py` (`Agent`, `run_agent`, module attribute `runtime.client`), `app/coach.py` (`COACH`, the `planned_sessions` table) and `app/bot.py`.

- [x] **Step 1:** Run `.venv/bin/python -m pytest -v`. Expected: all coach-plan tests pass. If anything is missing or red, stop and finish the coach plan first.
- [x] **Step 2:** Confirm the interfaces this plan relies on exist: `from app.runtime import Agent, run_agent`, `from app.coach import COACH`, `from app import db` with `db.connect`, `db.init`, `db.kv_get`, `db.kv_set`, and the `tmp_db` fixture. No code is written in this task.

### Task 2: Meal rules (pure)

**Files:**
- Create: `app/meals/__init__.py`, `app/meals/rules.py`, `tests/test_rules.py`

**Interfaces:**
- Produces in `rules.py`:
  - `EXERCISE_KINDS = {"rowing", "hiit", "strength"}`
  - `kind_from_text(text: str) -> str` (case-insensitive keyword match on the English profile strings: `row`→`rowing`, `hiit`→`hiit`, `strength`→`strength`, `walk`→`walk`, `mobil`→`mobility`, else `rest`)
  - `default_kinds_from_profile(profile: dict) -> dict[int, str]` (weekday 0=Monday…6; reads `profile["weekly_structure"]` keys `mon..sun`)
  - `@dataclass(frozen=True) class DayTarget: day: date; training: str; exercise: bool; away: bool; dinner_type: str | None; lunch_from: date | None; spare_portion: bool`
  - `compute_targets(start: date, end: date, kinds: dict[date, str], away: set[date], default_week: dict[int, str]) -> list[DayTarget]`
- Semantics: `training = kinds.get(day) or default_week[day.weekday()]`; `dinner_type=None` when away else `"protein"` if exercise else `"veggie"`; `lunch_from = day - 1 day` if the day is not away and `day-1` not in `away`, else `None` (the previous day is looked up even when it is before `start`); `spare_portion = (not away) and (day+1 in away)`.

- [x] **Step 1: Write the failing tests** (`tests/test_rules.py`), with `start=Mon 2026-11-23`, `end=Sun 2026-11-29`, `default_week` = the weekly structure of `data/profile.example.json`:
  - `test_exercise_days_get_protein_dinner`: Mon/Wed/Fri/Sat → `"protein"`; Tue/Thu/Sun → `"veggie"`.
  - `test_coach_plan_overrides_default`: `kinds={Mon: "walk"}` → Mon `exercise` False, `"veggie"`.
  - `test_away_day_has_no_dinner_and_no_lunch`: away Thu → Thu `dinner_type is None`, `lunch_from is None`; Fri `lunch_from is None` (no leftover); Wed `spare_portion is True`.
  - `test_first_day_lunch_uses_previous_day_unless_away`: Mon `lunch_from == 2026-11-22`; with `away={2026-11-22}` → `None`.
  - `test_consecutive_away_days`: away Thu+Fri → Wed spare True, Thu spare False, Sat `lunch_from is None`, Sun `lunch_from == Sat`.
  - `test_kind_from_text`: the five profile strings map as listed above.
- [x] **Step 2: Run** `pytest tests/test_rules.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `rules.py` as specified (pure, no I/O).
- [x] **Step 4: Run** the tests. Expected: PASS.

### Task 3: Storage (fridge, away days, draft) and table rendering

**Files:**
- Modify: `app/db.py` (append to `SCHEMA`)
- Create: `app/meals/store.py`, `tests/test_store.py`

**Interfaces:**
- New tables: `fridge_items(name TEXT PRIMARY KEY, quantity TEXT, updated_at TEXT)`, `away_days(day TEXT PRIMARY KEY)`, `draft_meta(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, new_recipe_count INTEGER NOT NULL, shopping_written INTEGER NOT NULL DEFAULT 0)`, `draft_dinners(day TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, dinner_type TEXT, recipe_slug TEXT, ingredients TEXT, steps TEXT, recipe_created INTEGER NOT NULL DEFAULT 0, plan_written INTEGER NOT NULL DEFAULT 0)`, `draft_shopping(item TEXT PRIMARY KEY)`.
- Produces in `store.py`:
  - Fridge: `fridge_list() -> list[dict]`, `fridge_add(name: str, quantity: str = "") -> None`, `fridge_remove(name: str) -> None`, `fridge_replace(items: list[tuple[str, str]]) -> None`
  - Away: `away_set(days: list[date]) -> None` (replaces the set), `away_get(start: date, end: date) -> set[date]`
  - Draft: `@dataclass class Draft: version: int; new_recipe_count: int; dinners: dict[date, DraftDinner]; shopping: list[str]; shopping_written: bool`; `@dataclass class DraftDinner: day: date; source: str; name: str; dinner_type: str | None; recipe_slug: str | None; ingredients: list[str]; steps: list[str]; recipe_created: bool; plan_written: bool`
  - `draft_get() -> Draft`, `draft_set_dinner(day: date, source: str, name: str, dinner_type: str | None = None, recipe_slug: str | None = None, ingredients: list[str] | None = None, steps: list[str] | None = None) -> int` (`source` in `"existing"|"new"`; raises `ValueError` otherwise; returns new version), `draft_set_shopping(items: list[str]) -> int`, `draft_set_new_recipe_count(n: int) -> int`, `draft_clear() -> int`. Every mutation increments `version` by 1; changing a dinner resets that row's `recipe_created`/`plan_written` to 0 and `shopping_written` to 0.
  - `render_table(draft: Draft, targets: list[DayTarget]) -> str`: a fenced code block, one header row (`Date`, `Entraînement`, `Dîner`, `Recette`, `Déj. lendemain`) and one row per target; recipe cell shows `name (existante)` or `name (NOUVELLE)`, `— absent(e) —` for away days, `—` for a not yet drafted dinner; the last column shows `<jour>: reste` or `<jour>: vide`; a spare portion appends ` ⚠ portion en trop`. The training and dinner-type cells use French labels from a `LABELS_FR` dict in `store.py` (rowing → rameur, hiit → HIIT, walk → marche, strength → renfo, mobility → mobilité, rest → repos; protein → protéines, veggie → végé). Columns padded by display width; names truncated with `…` so no line exceeds 100 characters and the whole message stays ≤ 1900 characters.

- [x] **Step 1: Write the failing tests** (`tests/test_store.py`, use `tmp_db`):
  - `test_fridge_add_replace_remove` (names matched case-insensitively; `fridge_replace` drops old items).
  - `test_away_set_replaces_and_filters_by_range`.
  - `test_draft_version_increments_on_each_change`: after `draft_set_dinner` twice and `draft_set_shopping`, versions are consecutive; `draft_clear()` empties dinners and shopping and still bumps the version.
  - `test_changing_dinner_resets_commit_flags`: mark flags directly in SQL, call `draft_set_dinner` on the same day → flags 0.
  - `test_invalid_source_raises`.
  - `test_render_table_marks_new_existing_away_and_spare`: build targets with `compute_targets` for the Task 2 week, away Thu, dinners for Mon (existing) and Tue (new); assert the output starts with ```` ``` ````, contains `(NOUVELLE)`, `(existante)`, `— absent(e) —`, `portion en trop`, and `Ven: vide`.
  - `test_render_table_fits_discord_limit`: 7 dinners with 80-character accented names → `len(text) <= 1900` and every line ≤ 100 characters, columns aligned (all rows have `|`-free equal-width columns: check that the `Recette` column starts at the same index on every row).
- [x] **Step 2: Run** `pytest tests/test_store.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** the schema additions and `store.py`. Ingredients/steps are stored newline-joined. Use `unicodedata.east_asian_width` only if needed; plain `len` is acceptable for the accented Latin text here.
- [x] **Step 4: Run** the tests. Expected: PASS.

### Task 4: Mealie client

**Files:**
- Create: `docs/mealie-endpoints.md`, `app/mealie.py`, `tests/test_mealie.py`

**Interfaces:**
- Produces in `mealie.py`:
  - `class MealieError(Exception)`; `class MealieUnavailable(MealieError)` (connection failure, timeout, 5xx); `class MealieAuthError(MealieError)` (401/403).
  - `@dataclass class RecipeSummary: slug: str; name: str; tags: list[str]`
  - `class MealieClient(base_url: str, token: str)`, async context manager (owns an `aiohttp.ClientSession`, timeout 15 s), with:
    - `async def search_recipes(self, query: str = "", tag: str | None = None, limit: int = 10) -> list[RecipeSummary]`
    - `async def get_recipe(self, slug: str) -> dict` (name, ingredients as strings, steps as strings)
    - `async def create_recipe(self, name: str, ingredients: list[str], steps: list[str], tags: list[str], servings: int = 2) -> str` (returns the slug; if a recipe with that exact name already exists it returns the existing slug instead of creating a duplicate)
    - `async def set_meal_plan_entry(self, day: date, slot: str, recipe_slug: str) -> None` (`slot` in `"lunch"|"dinner"`; skips if the same recipe already sits in that day/slot)
    - `async def add_shopping_items(self, items: list[str]) -> None` (into the household's default shopping list; skips an item whose text already exists unchecked)
  - Tag convention (French, because tags are saved in Mealie): new and searched recipes use the tags `protéines` and `végétarien`. `app/mealie.py` exposes `MEALIE_TAGS = {"protein": "protéines", "veggie": "végétarien"}`; `search_recipes(tag=...)` takes the French Mealie tag text, and callers map dinner types through `MEALIE_TAGS`.
  - Auth header, URL paths and payload shapes: **taken from the user's instance**, see Step 1.

- [x] **Step 1: Capture the real API.** Ask the user for `MEALIE_URL` and a token; run `curl -H "Authorization: Bearer $MEALIE_API_TOKEN" "$MEALIE_URL/openapi.json" -o /tmp/mealie-openapi.json` (use the scratchpad dir) and write into `docs/mealie-endpoints.md` the exact method, path, auth header, request and response fields for each of the five client methods (list/search recipes, get recipe, create recipe, meal plan entries, shopping lists and items). If the instance is unreachable from this machine, stop and ask the user to paste the relevant `/docs` sections. Do not guess paths.
- [x] **Step 2: Write the failing tests** (`tests/test_mealie.py`) against a fake Mealie built with `aiohttp.web` + `aiohttp.test_utils.TestServer` that mirrors the paths and payloads recorded in Step 1 (in-memory recipes, mealplan entries, shopping items):
  - `test_search_filters_by_tag_and_query`
  - `test_create_recipe_is_idempotent_by_name` (two calls → one recipe, same slug; stored servings == 2)
  - `test_meal_plan_entry_not_duplicated`
  - `test_shopping_items_not_duplicated`
  - `test_401_raises_auth_error` and `test_connection_refused_raises_unavailable` (point at a closed port) and `test_500_raises_unavailable`
  - `test_requests_carry_auth_header`
- [x] **Step 2b: Run** `pytest tests/test_mealie.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `mealie.py` with the signatures above; map exceptions as specified; never include the token in exception text.
- [x] **Step 4: Run** the tests. Expected: PASS.

### Task 5: Commit a draft to Mealie

**Files:**
- Create: `app/meals/commit.py`, `tests/test_commit.py`

**Interfaces:**
- Consumes: `store.draft_get`, the flags in `draft_dinners` / `draft_meta`, `MealieClient` (Task 4), `compute_targets` is **not** needed here.
- Produces:
  - `class StaleDraft(Exception)`, `class EmptyDraft(Exception)`
  - `@dataclass class CommitResult: recipes_created: int; plan_entries: int; shopping_items: int`
  - `async def commit_draft(expected_version: int, client: MealieClient) -> CommitResult`
- Behavior: raise `StaleDraft` if `draft.version != expected_version`; `EmptyDraft` if no dinners. For each dinner in date order: if `source == "new"` and not `recipe_created`, `create_recipe(..., tags=[...], servings=2)` (tag `MEALIE_TAGS[dinner_type]`, from the dinner's stored `dinner_type`) and mark `recipe_created`; then set the dinner entry for the day and the lunch entry for the next day (same recipe) unless the next day is away (the draft has no away info: the lunch entry is written only if `store.away_get` does not contain the next day), mark `plan_written`. Then the shopping list once, mark `shopping_written`. Each flag is written right after its Mealie call succeeds, so a retry resumes. Mealie errors propagate unchanged.

- [x] **Step 1: Write the failing tests** using the fake Mealie from Task 4 (move it to `tests/fake_mealie.py` and import it in both test files) and `tmp_db`:
  - `test_commit_writes_recipes_plan_and_shopping`: draft with one new and one existing dinner on consecutive days, shopping `["lentilles","citron"]` → result `CommitResult(recipes_created=1, plan_entries=4, shopping_items=2)`; fake shows recipe servings 2, dinner entries on both days and lunch entries on the following days.
  - `test_stale_version_raises_and_writes_nothing`.
  - `test_empty_draft_raises`.
  - `test_no_lunch_entry_before_away_day`: away on day+1 → only the dinner entry is written.
  - `test_retry_after_failure_creates_no_duplicates`: make the fake fail the second dinner's meal plan call once (HTTP 500), call `commit_draft` expecting `MealieUnavailable`, call again → fake holds exactly one recipe for the new dinner, no duplicate plan entries, no duplicate shopping items.
- [x] **Step 2: Run** `pytest tests/test_commit.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `commit.py`.
- [x] **Step 4: Run** `pytest -v`. Expected: PASS.

### Task 6: Confirm-button logic (Discord-free)

**Files:**
- Create: `app/meals/confirm.py`, `tests/test_confirm.py`

**Interfaces:**
- Produces:
  - `class TableMessage(Protocol)`: `async def show_button(self, visible: bool) -> None`; `async def set_text(self, text: str) -> None`
  - `class ConfirmController`: `__init__(self, commit: Callable[[int], Awaitable[CommitResult]], latest_version: Callable[[], int])`; `async def register(self, version: int, message: TableMessage) -> None` (removes the button from the previously registered message, remembers the new one); `async def on_press(self, version: int, message: TableMessage) -> str` (returns the French text shown to the user).
- Behavior of `on_press`: under an `asyncio.Lock`; if a commit is running or `version` differs from `latest_version()` or the message is not the registered one → `show_button(False)` on that message and return `"Ce tableau n'est plus à jour, utilise le dernier."`. Otherwise `show_button(False)` **first**, then `commit(version)`; success → `set_text` with `"✅ Confirmé: N recettes créées, M repas planifiés, K articles ajoutés dans Mealie."`; `EmptyDraft` → `"Il n'y a rien à confirmer pour l'instant."` and button restored; `StaleDraft` → stale text, button stays hidden; `MealieAuthError` → `"Mealie a refusé le jeton d'API."` with button restored; `MealieUnavailable` or other `MealieError` → `"Mealie est injoignable pour l'instant, réessaie dans un moment."` with button restored. Never raise to the caller.

- [x] **Step 1: Write the failing tests** with a `FakeMessage` recording calls:
  - `test_press_hides_button_before_commit` (record the order of `show_button(False)` and `commit`).
  - `test_double_press_commits_once` (two concurrent `on_press` with a slow fake commit → commit called once).
  - `test_stale_version_does_not_commit`.
  - `test_new_table_removes_old_button` (`register` twice → first message got `show_button(False)`).
  - `test_failure_restores_button_and_keeps_draft_confirmable` for `MealieUnavailable`, `MealieAuthError` (different texts) and `EmptyDraft`.
  - `test_success_text_contains_counts`.
- [x] **Step 2: Run** `pytest tests/test_confirm.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `confirm.py` as specified.
- [x] **Step 4: Run** the tests. Expected: PASS.

### Task 7: The nutritionist agent

**Files:**
- Create: `prompts/nutritionist.md`, `app/nutritionist.py`, `tests/test_nutritionist_tools.py`

**Interfaces:**
- Consumes: `rules`, `store`, `MealieClient`, `runtime.Agent`, `coach`'s `planned_sessions` table (read-only via SQL for `kinds`).
- Produces: `NUTRITIONIST = Agent(name="nutritionist", prompt_file="nutritionist.md", tools=[...])` and these `@beta_async_tool` functions (all return JSON strings, English keys): `get_meal_targets(start: str, end: str)`, `get_profile()`, `get_fridge()`, `add_fridge_items(items: str)` (one `name: quantity` per line), `remove_fridge_items(names: str)` (one per line), `replace_fridge(items: str)`, `set_away_days(days: str)` (one ISO date per line, replaces the set), `get_away_days(start: str, end: str)`, `set_new_recipe_count(count: int)`, `search_recipes(query: str = "", tag: str = "")`, `get_recipe(slug: str)`, `set_draft_dinner(day: str, source: str, name: str, recipe_slug: str = "", ingredients: str = "", steps: str = "")` (it looks up the day's `dinner_type` through `compute_targets`, passes it to `store.draft_set_dinner`, and refuses an away day with an error string), `set_draft_shopping_list(items: str)`, `get_draft()`, `clear_draft()`. Invalid dates/sources return `{"error": ...}` strings, never raise. The Mealie client is created from `MEALIE_URL` / `MEALIE_API_TOKEN` per call; Mealie errors become `{"error": "<English message>"}`.
- The prompt (`nutritionist.md`, written in English) must state: role; reply to the user in French with tutoiement; write all Mealie content (recipe names, ingredients, steps, tags, shopping items) in French; short Discord replies; tools are the only memory; principles from the history (protein in every meal, vegetables, no forbidden foods, seasonal French produce, modest deficit 300–500 kcal, weigh at most weekly); always ask for the new-recipe count if not given and honor it even when an existing recipe fits; recipes always for 2 portions; new recipes need ingredients and numbered steps; shopping list = week's ingredients minus what the fridge covers (agent's judgment); never claim anything is saved in Mealie, only that the draft is updated and the user must press "Confirmer"; messages starting with `[weekly]` are the automatic Sunday trigger: ask away days, fridge changes and the new-recipe count in one short message, then plan after the answer.

- [x] **Step 1: Write the failing tests** (`tests/test_nutritionist_tools.py`, `tmp_db`, call tools via `.call({...})`):
  - `test_get_meal_targets_reads_coach_plan`: insert a coach `planned_sessions` row `("2026-11-23","walk",...)` → Monday `dinner_type == "veggie"` in the JSON.
  - `test_set_draft_dinner_refuses_away_day`.
  - `test_set_draft_dinner_rejects_bad_source_and_date` (error strings, no exception).
  - `test_fridge_tools_parse_lines` (`"œufs: 6\nlentilles"` → two items, second with empty quantity).
  - `test_draft_tools_bump_version` (`get_draft` shows `version` increasing).
  - `test_mealie_down_returns_error_json` (point `MEALIE_URL` at a closed port; `search_recipes` → JSON with `"error"`).
  - `test_no_tool_can_write_to_mealie`: assert no tool in `NUTRITIONIST.tools` has a name in `{"create_recipe","set_meal_plan_entry","add_shopping_items","commit_draft"}` and that `app/nutritionist.py` imports nothing named `commit_draft`.
- [x] **Step 2: Run** `pytest tests/test_nutritionist_tools.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** the module and write the prompt.
- [x] **Step 4: Run** `pytest -v`. Expected: PASS.

### Task 8: Bot wiring, weekly run, config and docs

**Files:**
- Modify: `app/bot.py`, `app/config.py`, `.env.example`, `README.md`, `compose.yaml`
- Create: `tests/test_bot_helpers.py`

**Interfaces:**
- Consumes: `COACH`, `NUTRITIONIST`, `run_agent`, `ConfirmController`, `commit_draft`, `MealieClient`, `store.draft_get`, `store.render_table`, `rules.compute_targets`.
- Produces:
  - `config.NUTRITION_WEEKLY_HOUR = int(os.environ.get("NUTRITION_WEEKLY_HOUR", "10"))`
  - In `bot.py`: `AGENTS: dict[int, Agent]` built from `COACH_CHANNEL_ID` and `NUTRITION_CHANNEL_ID` (the nutritionist is skipped with a log warning if its env vars are missing); `class ConfirmView(discord.ui.View)` with one button labelled `Confirmer` that only `ALLOWED_USER_ID` can press (others get an ephemeral refusal) and calls `controller.on_press`; `DiscordTableMessage` implementing `TableMessage` (hide/show button = edit the message with `view=None`/`view=ConfirmView`); `async def post_draft_table(channel) -> None`; `def weekly_trigger_text() -> str` returning a string starting with `"[weekly]"`; `def should_run_weekly(last: str | None, today: date) -> bool` (True when `last` is None or ≥ 7 days old).
- Behavior: after every nutritionist reply, if the draft is non-empty and its version differs from the last posted one, send the table (computed from `compute_targets` for the draft's week, with the coach plan and away days) with the button and `controller.register` it. A weekly APScheduler cron job (`day_of_week="sun"`, hour `NUTRITION_WEEKLY_HOUR`) runs the nutritionist with `weekly_trigger_text()` guarded by `kv` key `last_weekly`. The confirm commit uses a fresh `MealieClient` from env.

- [x] **Step 1: Write the failing tests** (`tests/test_bot_helpers.py`; importing `app.bot` needs `ALLOWED_USER_ID`, `COACH_CHANNEL_ID` env set in the test via monkeypatch before import): `test_weekly_trigger_text_starts_with_marker`, `test_should_run_weekly` (None → True; 3 days ago → False; 7 days ago → True), `test_agents_registry_skips_nutritionist_without_channel_id`.
- [x] **Step 2: Run** `pytest tests/test_bot_helpers.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** the changes above. `.env.example`/`README.md`/`compose.yaml` (no change needed beyond env) get the three new variables, the `#nutrition` channel step, the Mealie token step (`/user/profile/api-tokens`), and a note that the container must reach `MEALIE_URL`.
- [x] **Step 4: Run** `pytest -v`. Expected: ALL PASS.

### Task 9: Live verification (manual, needs the user's keys)

**Files:** none.

- [x] **Step 1:** User fills `.env` (`MEALIE_URL`, `MEALIE_API_TOKEN`, `NUTRITION_CHANNEL_ID`, others from Task 8) and starts the bot (`README.md` steps).
- [x] **Step 2:** In `#nutrition`, write `[weekly]`-equivalent: "prépare la semaine". Expected: the agent asks away days, fridge and the number of new recipes; after answering, a dinner table appears with a **Confirmer** button; Mealie still contains nothing new.
- [x] **Step 3:** Ask "remplace jeudi par du poisson". Expected: a new table is posted, the previous table's button has disappeared, Mealie still untouched.
- [x] **Step 4:** Press **Confirmer**. Expected: the button disappears immediately, then the message reports counts; Mealie shows the new recipes (2 portions), dinner and next-day lunch entries, and the shopping list without fridge items; pressing again is impossible.
- [x] **Step 5:** Stop Mealie, redo steps 2–4. Expected: a plain French error, the button returns, the draft is intact; start Mealie and press again → no duplicate recipes or entries.
- [x] **Step 6:** Ask the coach to change a planned session, then "recalcule le tableau". Expected: dinner types follow the new plan.
