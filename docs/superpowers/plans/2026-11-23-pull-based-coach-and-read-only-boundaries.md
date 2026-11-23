# Pull-based coach and read-only boundaries: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the coach stop asking for reports (spec phase 1), and put hard read-only boundaries around what the models can touch: SQLite, Mealie and the text they may write (spec phase 2).

**Architecture:** Phase 1 removes the scheduled check-in and rewrites the coach prompt. Phase 2 adds a read-only SQLite connection used by every model read path, splits the Mealie client into a read-only reader and a writer with separate tokens, and adds a validation module applied to the nutritionist's existing tools. The chat architecture stays; the structured interface (spec phase 3) replaces it in a later plan.

**Tech Stack:** Python 3.12, sqlite3, aiohttp, discord.py, APScheduler 3, pytest (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-11-23-structured-interface-and-hardening-design.md` (sections 2 "No nagging", 5, and the validators of 3).

**Out of scope, planned separately:** phase 3 (slash commands, single-shot planners, closed recipe list, full-content confirmation, the coach's optional "Plan the week" reminder button), phase 4 (compose, network, supply chain), phase 5 (`docs/security.md`, Discord checklist).

## Global Constraints

- **Never commit.** The user's rule: leave all changes in the working tree for review. Tasks end with "leave in working tree", not a commit.
- Python 3.12; the existing test style (`tmp_db` fixture, `tool.call(kwargs)` returning JSON strings).
- Setup before the first test run (see README): `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt`. Test command: `.venv/bin/python -m pytest`. Style: `ruff check` and `ruff format` as in `.pre-commit-config.yaml`.
- Tools never raise: invalid input returns `{"error": ...}`.
- Mealie error text never contains a token.
- Spec values: fridge text at most 600 characters, at most 40 items; an automatic progress chart only when `PROGRESS_AUTO_DAYS` > 0 (default 0); two Mealie tokens `MEALIE_READ_TOKEN` and `MEALIE_WRITE_TOKEN`, falling back to `MEALIE_API_TOKEN` with a warning.

## Review Focus

1. `connect_ro` while a writer holds an open write transaction: reads must still succeed (test in Task 3).
2. `connect_ro` on a missing database: a clear `sqlite3.OperationalError` and no file created (Task 3).
3. Upgrade path for the existing `.env`: only `MEALIE_API_TOKEN` keeps working; a lone `MEALIE_READ_TOKEN` disables the nutritionist with a named warning instead of failing at commit time (Task 4).
4. Fridge input: accents, lines with no quantity, over-limit input, and a replace whose lines are all rejected must not wipe the fridge (Tasks 5 and 6).
5. `PROGRESS_AUTO_DAYS` set to an empty string means off, and old `last_checkin` rows in the database are harmless (Task 1).

---

### Task 1: Remove the scheduled check-in; progress only on demand

**Files:**
- Modify: `app/config.py` (remove `CHECKIN_HOUR`, `CHECKIN_EVERY_DAYS`; add `PROGRESS_AUTO_DAYS: int` default 0, `PROGRESS_HOUR: int` default 18)
- Modify: `app/bot.py` (remove `checkin_job`, `should_checkin`, `AUTO_CHECKIN`, `MANUAL_CHECKIN`, the `!checkin` branch; add `progress_job`, `register_jobs`)
- Modify: `tests/test_bot.py`
- Modify: `.env.example`, `README.md` (remove `CHECKIN_HOUR`; document `PROGRESS_AUTO_DAYS`, `PROGRESS_HOUR`; the Coach section no longer mentions check-ins)

**Interfaces:**
- Produces: `should_send_progress(last: str | None, today: date, has_new_data: bool, every_days: int = PROGRESS_EVERY_DAYS) -> bool`; `async def progress_job() -> None`; `def register_jobs(scheduler: AsyncIOScheduler) -> None` adding job id `"progress"` only when `config.PROGRESS_AUTO_DAYS > 0` and job id `"nutrition_weekly"` only when a nutrition channel exists. `on_ready` calls `register_jobs` then starts the scheduler.

- [ ] **Step 1: Set up the venv and run the baseline suite**

Run the setup from Global Constraints, then `.venv/bin/python -m pytest -q`. Record any pre-existing failure; do not fix unrelated ones.

- [ ] **Step 2: Write the failing tests in `tests/test_bot.py`**

Delete `test_should_checkin_rules`, `test_checkin_job_records_state_before_asking`, `test_checkin_job_skips_when_recent`, `test_manual_checkin_sends_trigger_and_records_state`. Rewrite `test_automatic_progress_only_with_new_data` to call `bot.progress_job()` with `monkeypatch.setattr(config, "PROGRESS_AUTO_DAYS", 7)` and without the `last_checkin` line. Add:

```python
def test_checkin_machinery_is_gone(bot):
    for name in ("checkin_job", "should_checkin", "AUTO_CHECKIN", "MANUAL_CHECKIN"):
        assert not hasattr(bot, name)
    assert not hasattr(config, "CHECKIN_HOUR")
    assert not hasattr(config, "CHECKIN_EVERY_DAYS")

async def test_bang_checkin_is_plain_text_and_records_nothing(bot, monkeypatch):
    run = recorder()
    monkeypatch.setattr(bot, "run_agent", run)
    await bot.on_message(fake_message("!checkin"))
    assert run.calls[0][2] == "!checkin"
    assert db.kv_get("last_checkin") is None

def test_should_send_progress_honours_every_days(bot):
    assert bot.should_send_progress("2026-11-17", date(2026, 11, 20), True, every_days=3) is True
    assert bot.should_send_progress("2026-11-17", date(2026, 11, 20), True, every_days=4) is False

def test_no_progress_job_by_default(bot, monkeypatch):
    monkeypatch.setattr(config, "PROGRESS_AUTO_DAYS", 0)
    scheduler = AsyncIOScheduler()
    bot.register_jobs(scheduler)
    assert "progress" not in {j.id for j in scheduler.get_jobs()}

def test_progress_job_registered_when_enabled(bot, monkeypatch):
    monkeypatch.setattr(config, "PROGRESS_AUTO_DAYS", 7)
    scheduler = AsyncIOScheduler()
    bot.register_jobs(scheduler)
    assert "progress" in {j.id for j in scheduler.get_jobs()}
```

In `tests/test_config.py` add a test that `PROGRESS_AUTO_DAYS=""` with `importlib.reload(config)` gives `config.PROGRESS_AUTO_DAYS == 0`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot.py tests/test_config.py -q`
Expected: FAIL (missing `progress_job`, `register_jobs`, old names still present).

- [ ] **Step 4: Implement the interfaces above**

`progress_job` is the progress half of the old `checkin_job` (same channel lookup and error logging) and passes `every_days=config.PROGRESS_AUTO_DAYS`. Parse the new settings with the file's existing `int(os.environ.get(...) or default)` pattern. `register_jobs` uses `CronTrigger(hour=config.PROGRESS_HOUR, minute=0, timezone=config.TZ)` for progress and keeps the nutritionist's Sunday trigger unchanged.

- [ ] **Step 5: Update `.env.example` and the README**

Step 6 below checks them.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q` and `grep -rn "CHECKIN\|check-in\|checkin" app README.md .env.example`
Expected: all tests pass; the grep prints nothing. Leave changes in the working tree.

---

### Task 2: Pull-based coach prompt

**Files:**
- Modify: `prompts/coach.md`
- Create: `tests/test_prompts.py`

- [ ] **Step 1: Write the failing tests in `tests/test_prompts.py`**

```python
from pathlib import Path

COACH = Path("prompts/coach.md").read_text(encoding="utf-8")

def test_coach_prompt_has_no_checkin_machinery():
    for text in ("[check-in]", "# Check-ins", "check in about every", "you may ask for the distance"):
        assert text not in COACH

def test_coach_prompt_is_pull_based():
    for phrase in (
        "Never ask the user for a report, a log, a distance or any missing information about a past session.",
        "An unlogged session is unknown, never a failure.",
        "When data is missing, hold the load",
    ):
        assert phrase in COACH

def test_progress_messages_section_is_kept():
    assert "[progress]" in COACH
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: FAIL on the first two tests.

- [ ] **Step 3: Edit `prompts/coach.md`**

Remove the `# Check-ins` section. In `# Mission`, replace "check in about every 2 days, and adapt" with wording that says the coach adapts the load to what the user chooses to report. In `# Logging numbers`, replace the two sentences about asking for a distance with "Never ask for a distance; record it only when the user gives it." Add a new section, with these exact sentences:

```
# Pull-based

You never start a conversation about the past. The user decides when to report a session.

- Never ask the user for a report, a log, a distance or any missing information about a past session. Answer what they send and nothing more.
- An unlogged session is unknown, never a failure. Do not mention it and do not count it against the user.
- When data is missing, hold the load: do not raise it, and do not ask why.
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: PASS. Leave changes in the working tree.

---

### Task 3: Read-only SQLite connection for every model read path

**Files:**
- Modify: `app/db.py` (add `connect_ro`)
- Modify: `app/coach.py` (`get_plan`, `get_recent_sessions`, `get_benchmarks`), `app/exercises.py` (`_rows`), `app/meals/store.py` (`fridge_list`, `away_get`, `draft_get`), `app/nutritionist.py` (`_coach_kinds`)
- Modify: `tests/test_db.py`; Create: `tests/test_read_paths.py`

**Interfaces:**
- Produces: `db.connect_ro() -> Iterator[sqlite3.Connection]` (context manager, `row_factory = sqlite3.Row`, opened with the URI `<DB_PATH as file URI>?mode=ro`, then `PRAGMA query_only = ON`; closes on exit; never creates the file). Functions that also write (`fridge_add`, `fridge_remove`, `fridge_replace`, `away_set`, all `draft_set_*`, `mark_*`) keep `db.connect()`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_db.py`:

```python
def test_connect_ro_reads_but_cannot_write(tmp_db):
    db.kv_set("k", "v")
    with db.connect_ro() as conn:
        assert conn.execute("SELECT value FROM kv WHERE key = 'k'").fetchone()["value"] == "v"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")

def test_connect_ro_reads_while_a_writer_is_mid_transaction(tmp_db):
    writer = sqlite3.connect(config.DB_PATH)
    writer.execute("INSERT INTO kv (key, value) VALUES ('w', '1')")
    try:
        with db.connect_ro() as conn:
            assert conn.execute("SELECT count(*) FROM kv WHERE key = 'w'").fetchone()[0] == 0
    finally:
        writer.rollback()
        writer.close()

def test_connect_ro_on_missing_database_raises_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "nope.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        with db.connect_ro():
            pass
    assert not (tmp_path / "nope.sqlite3").exists()
```

`tests/test_read_paths.py`: with `tmp_db`, `exercises.seed()` and the profile fixture from `test_nutritionist_tools.py` done first, then `monkeypatch.setattr(db, "connect", boom)` where `boom` raises `AssertionError("read path opened a read-write connection")`, call each of these and assert it returns without error: coach `get_plan`, `get_recent_sessions`, `get_benchmarks`, `list_exercises`, `get_exercise(name="squats")`; nutritionist `get_meal_targets`, `get_fridge`, `get_draft`, `get_away_days`; `exercises.linkify("squats")`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db.py tests/test_read_paths.py -q`
Expected: FAIL (`connect_ro` missing; read paths still use `db.connect`).

- [ ] **Step 3: Implement `db.connect_ro` and switch the listed read functions to it**

Build the URI with `config.DB_PATH.resolve().as_uri() + "?mode=ro"` and `sqlite3.connect(uri, uri=True)`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. Leave changes in the working tree.

---

### Task 4: Mealie reader/writer split and token split

**Files:**
- Modify: `app/mealie.py`, `app/config.py`, `app/nutritionist.py`, `app/bot.py`
- Modify: `tests/test_mealie.py`, `tests/test_config.py`, `tests/test_nutritionist_tools.py`, `tests/test_bot_helpers.py`
- Modify: `.env.example`, `README.md` (env table, a "read-only Mealie user" note), `docs/mealie-endpoints.md` (a Tokens section)

**Interfaces:**
- Produces in `app/mealie.py`: `class MealieReader` (plumbing, `_all_pages`, `_find_tag`, `_recipe`, `search_recipes`, `get_recipe`; class attribute `ALLOWED_METHODS = frozenset({"GET"})`; `_request` raises `ValueError` for any other method before any network call). `class MealieClient(MealieReader)` adds `create_recipe`, `set_meal_plan_entry`, `add_shopping_items`, and `ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH"})`.
- Produces in `app/config.py`: `mealie_token(role: str, env: Mapping[str, str] = os.environ) -> str | None` where `role` is `"read"` or `"write"`: `env["MEALIE_READ_TOKEN"]` / `env["MEALIE_WRITE_TOKEN"]`, else `env["MEALIE_API_TOKEN"]`, else `None`.
- Produces in `app/nutritionist.py`: `mealie_reader() -> MealieReader` and `mealie_writer() -> MealieClient` (replace `mealie_client`; both raise `MealieError("Mealie is not configured ...")` when the URL or the role's token is missing). `search_recipes` and `get_recipe` use the reader. `bot.commit_current_draft` uses the writer.
- `bot.build_agents(env)` requires `NUTRITION_CHANNEL_ID`, `MEALIE_URL`, and both role tokens; the warning names what is missing (`MEALIE_READ_TOKEN`, `MEALIE_WRITE_TOKEN`). When the read and write tokens are the same value it logs a warning containing `MEALIE_READ_TOKEN` advising two separate tokens.

- [ ] **Step 1: Write the failing tests**

`tests/test_mealie.py`:

```python
async def test_reader_has_no_write_methods_and_refuses_writes(mealie):
    fake, _ = mealie
    async with MealieReader(fake.base_url, fake.token) as reader:
        for name in ("create_recipe", "set_meal_plan_entry", "add_shopping_items"):
            assert not hasattr(reader, name)
        with pytest.raises(ValueError):
            await reader._request("POST", "/api/recipes", json={"name": "x"})
        assert fake.recipes == {}
        assert await reader.search_recipes() == []
```

`tests/test_config.py`: `mealie_token` returns `None` with nothing set; `"legacy"` for both roles with only `MEALIE_API_TOKEN`; with `MEALIE_READ_TOKEN="r"` added, read is `"r"` and write stays `"legacy"`.

`tests/test_nutritionist_tools.py`:

```python
async def test_recipe_tools_use_the_read_token(nutri, mealie, monkeypatch):
    fake, _ = mealie
    monkeypatch.setenv("MEALIE_URL", fake.base_url)
    monkeypatch.delenv("MEALIE_API_TOKEN", raising=False)
    monkeypatch.setenv("MEALIE_READ_TOKEN", fake.token)
    monkeypatch.setenv("MEALIE_WRITE_TOKEN", "not-the-token")
    assert "error" not in await call(nutri.search_recipes)

async def test_writer_uses_the_write_token_and_reader_cannot_write(nutri, mealie, monkeypatch):
    fake, _ = mealie
    monkeypatch.setenv("MEALIE_URL", fake.base_url)
    monkeypatch.delenv("MEALIE_API_TOKEN", raising=False)
    monkeypatch.setenv("MEALIE_READ_TOKEN", "not-the-token")
    monkeypatch.setenv("MEALIE_WRITE_TOKEN", fake.token)
    async with nutri.mealie_writer() as writer:
        assert await writer.create_recipe("Curry", ["pois chiches"], ["cuire"], [])
    from app.mealie import MealieClient
    assert not isinstance(nutri.mealie_reader(), MealieClient)
```

`tests/test_bot_helpers.py` (use `FULL_ENV` as the base): split tokens without `MEALIE_API_TOKEN` register both agents; only `MEALIE_READ_TOKEN` registers only the coach and the warning text contains `MEALIE_WRITE_TOKEN`; `FULL_ENV` as is (legacy token) registers both agents and the warning contains `MEALIE_READ_TOKEN`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mealie.py tests/test_config.py tests/test_nutritionist_tools.py tests/test_bot_helpers.py -q`
Expected: FAIL (`MealieReader`, `mealie_token`, `mealie_reader` and `mealie_writer` missing).

- [ ] **Step 3: Implement the interfaces above**

Move the plumbing and read methods into `MealieReader`; `MealieClient` inherits and keeps its write methods. Update `NUTRITION_ENV` in `app/bot.py` to `("NUTRITION_CHANNEL_ID", "MEALIE_URL")` plus the token check through `config.mealie_token`.

- [ ] **Step 4: Document the tokens**

In `docs/mealie-endpoints.md` add a "Tokens" section: the read token belongs to a second Mealie user with the fewest permissions that still allow reading recipes; the write token belongs to the user that may create recipes, plan entries and shopping items. Mark it as **to verify by hand on the real instance**: with the read token, `curl -X POST $MEALIE_URL/api/recipes -H "Authorization: Bearer $READ" -d '{"name":"x"}'` must be refused. Update the README env table and `.env.example` (`MEALIE_READ_TOKEN`, `MEALIE_WRITE_TOKEN`; `MEALIE_API_TOKEN` marked as the legacy single token).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. Leave changes in the working tree.

---

### Task 5: The validation module

**Files:**
- Create: `app/validate.py`, `tests/test_validate.py`

**Interfaces:**
- Produces:
  - `check_text(value: str, field: str, max_len: int) -> str`: returns the stripped value; raises `ValueError(f"{field} ...")` when it is empty, longer than `max_len`, or contains any of: `http://`, `https://`, `www.`, a markdown link `[x](y)`, `<@`, `<#`, `<&`, `<!`, `@everyone`, `@here` (case-insensitive).
  - `check_lines(lines: list[str], field: str, max_lines: int, max_len: int) -> list[str]`: `check_text` on each line; raises `ValueError` beyond `max_lines`.
  - `parse_fridge(text: str) -> tuple[list[tuple[str, str]], list[str]]`: returns `(accepted (name, quantity) pairs, rejected lines each cut to 40 characters)`. Raises `ValueError` when `text` exceeds `FRIDGE_MAX_CHARS = 600` or has more than `FRIDGE_MAX_ITEMS = 40` non-empty lines. A line is `name` or `name: quantity` (split at the first `:`); name and quantity each match `^[\w '’.,%/()+-]{1,40}$` (quantity may be empty). A line containing any pattern that `check_text` forbids is rejected as a whole line, before the split (so `voir http://x.io` is rejected even though `voir http` and `//x.io` would each match the field regex). Blank lines are skipped.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text", [
    "voir https://x.io", "http://a", "www.exemple.fr", "[ici](https://x)",
    "<@123>", "@everyone salut", "@HERE",
])
def test_check_text_rejects_links_and_mentions(text):
    with pytest.raises(ValueError):
        check_text(text, "steps", 200)

def test_check_text_strips_and_keeps_accents():
    assert check_text("  Faire revenir l'oignon à feu doux. ", "step", 200) == "Faire revenir l'oignon à feu doux."

def test_check_text_rejects_empty_and_too_long():
    for bad in ("   ", "a" * 201):
        with pytest.raises(ValueError):
            check_text(bad, "step", 200)

def test_check_lines_limits_count_and_checks_each_line():
    with pytest.raises(ValueError):
        check_lines(["a"] * 3, "ingredients", 2, 50)
    with pytest.raises(ValueError):
        check_lines(["ok", "http://x.io"], "ingredients", 5, 50)

def test_parse_fridge_accepts_normal_lines():
    assert parse_fridge("œufs: 6\nlentilles\n\nlait: 1 L") == (
        [("œufs", "6"), ("lentilles", ""), ("lait", "1 L")], [])

def test_parse_fridge_reports_rejected_lines():
    ok, rejected = parse_fridge("beurre: 250 g\nvoir http://x.io\n<@1>\n" + "a" * 41)
    assert ok == [("beurre", "250 g")]
    assert rejected == ["voir http://x.io", "<@1>", "a" * 40]

def test_parse_fridge_limits():
    with pytest.raises(ValueError):
        parse_fridge("\n".join(f"a{i}" for i in range(41)))
    with pytest.raises(ValueError):
        parse_fridge("a: 1\n" * 130)

def test_parse_fridge_empty_text():
    assert parse_fridge("") == ([], [])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_validate.py -q`
Expected: FAIL (`app.validate` missing).

- [ ] **Step 3: Implement `app/validate.py`**

One compiled regex for the forbidden patterns and one for fridge fields; no dependencies.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_validate.py -q`
Expected: PASS. Leave changes in the working tree.

---

### Task 6: Apply the validators to the nutritionist's tools

**Files:**
- Modify: `app/nutritionist.py` (replace `_parse_items`; validate `set_draft_dinner`, `set_draft_shopping_list`, the fridge tools)
- Modify: `tests/test_nutritionist_tools.py`, `README.md` (a short "Input limits" note)

**Interfaces:**
- Consumes: `app.validate.check_text`, `check_lines`, `parse_fridge`.
- Produces (limits as module constants in `app/nutritionist.py`): `set_draft_dinner` accepts `name` up to 100 characters, `recipe_slug` matching `^[a-z0-9]+(?:-[a-z0-9]+)*$` and at most 100 characters, at most 40 ingredient lines of 150 characters and 40 step lines of 400 characters; `set_draft_shopping_list` accepts at most 60 items of 80 characters. Every failure returns `_error(str(exc))` and changes nothing. `add_fridge_items` and `replace_fridge` return `{"ok": True, "added": n, "items": [...], "rejected": [...]}`; `replace_fridge` with non-empty text whose lines are all rejected returns an error and leaves the fridge untouched; an empty text still empties it.

- [ ] **Step 1: Write the failing tests**

```python
async def test_set_draft_dinner_rejects_links_and_mentions(nutri):
    base = dict(day="2026-11-24", source="new", name="Curry", ingredients="1 oignon", steps="1. Cuire")
    for field, value in (("steps", "1. Va sur https://evil.example"),
                         ("ingredients", "[x](http://e)"), ("name", "@everyone")):
        assert "error" in await call(nutri.set_draft_dinner, **{**base, field: value})
    assert store.draft_get().dinners == {}

async def test_set_draft_dinner_validates_the_slug(nutri):
    base = dict(day="2026-11-24", source="existing", name="Curry")
    for bad in ("../admin", "a b", "A-B", "x" * 101):
        assert "error" in await call(nutri.set_draft_dinner, **base, recipe_slug=bad)
    assert store.draft_get().dinners == {}
    assert (await call(nutri.set_draft_dinner, **base, recipe_slug="curry-de-pois-chiches"))["ok"] is True

async def test_shopping_list_is_validated_and_left_unchanged_on_error(nutri):
    await call(nutri.set_draft_shopping_list, items="citron")
    before = store.draft_get()
    assert "error" in await call(nutri.set_draft_shopping_list, items="lentilles\nvoir http://x.io")
    assert "error" in await call(nutri.set_draft_shopping_list, items="\n".join(f"a{i}" for i in range(61)))
    after = store.draft_get()
    assert (after.version, after.shopping) == (before.version, ["citron"])

async def test_fridge_tools_report_rejected_lines(nutri):
    out = await call(nutri.add_fridge_items, items="œufs: 6\nvoir http://x.io")
    assert out["added"] == 1 and out["rejected"] == ["voir http://x.io"]
    assert [i["name"] for i in out["items"]] == ["œufs"]

async def test_replace_fridge_with_only_rejected_lines_keeps_the_fridge(nutri):
    await call(nutri.add_fridge_items, items="œufs: 6")
    assert "error" in await call(nutri.replace_fridge, items="http://x.io")
    assert [i["name"] for i in (await call(nutri.get_fridge))["items"]] == ["œufs"]
    await call(nutri.replace_fridge, items="")
    assert (await call(nutri.get_fridge))["items"] == []

async def test_fridge_over_limits_returns_error(nutri):
    assert "error" in await call(nutri.add_fridge_items, items="\n".join(f"a{i}" for i in range(41)))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_nutritionist_tools.py -q`
Expected: FAIL on the six new tests; the existing ones still pass.

- [ ] **Step 3: Implement the validation in `app/nutritionist.py`**

Catch `ValueError` from the validators and return `_error`. Validate before touching the store, so a rejected call never changes the draft version.

- [ ] **Step 4: Run the full suite and the linter**

Run: `.venv/bin/python -m pytest -q`, then `ruff check app tests` and `ruff format --check app tests`
Expected: all tests pass; ruff reports nothing to fix. Leave changes in the working tree.
