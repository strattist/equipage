# Coach Agent and Bot Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Discord bot with a French-speaking sport coach that plans weekly sessions, logs what was done, tracks rowing benchmarks and posts a check-in every 2 days; the foundation (database, shared run loop, agent registry) is reused by the nutritionist plan.

**Architecture:** One bot process routes Discord channels to `Agent` objects (prompt file + tools). `run_agent` replays recent chat text, runs the Anthropic tool runner, and saves the visible text. All durable facts live in SQLite and are read and written through tools.

**Tech Stack:** Python 3.12, `anthropic` (async tool runner, `beta_async_tool`), discord.py, APScheduler 3.x, SQLite, pytest + pytest-asyncio, uv for the virtualenv.

**Spec:** `docs/superpowers/specs/2026-11-17-coach-design.md`. A previous scaffold of this plan was archived at `/tmp/claude-1000/-home-baptiste-work-agents/a08e335c-e6d7-422b-b080-c843660f6648/scratchpad/coach-v0-archive` (may be gone); it may be consulted but nothing is to be copied without writing the test first.

**Repo rule (user's global instruction):** never run `git commit`; leave changes in the working tree. There are no commit steps.

## Global Constraints

- Language policy (see spec): the agent speaks to the user in French (tutoiement, short replies); everything else is English: database values, code and comments, system prompts (English text that tells the model to reply in French), README, commands, logs, env vars, profile keys and values, tool names and descriptions, trigger markers. Exceptions: text the code sends to Discord (errors, button labels, chart labels), the `messages` table, and the exercise catalog's French names, aliases and explanations.
- Python 3.12 (`uv venv --python 3.12 .venv`; install with `uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt`). Run tests with `.venv/bin/python -m pytest`.
- `requirements.txt`: `anthropic>=1.1.0`, `discord.py>=2.4`, `apscheduler>=3.10,<4`, `matplotlib>=3.8`. `requirements-dev.txt`: `pytest`, `pytest-asyncio`. `pytest.ini`: `asyncio_mode = auto`, `testpaths = tests`.
- Env: `ANTHROPIC_API_KEY`, `DISCORD_TOKEN`, `ALLOWED_USER_ID`, `COACH_CHANNEL_ID` required; `CLAUDE_MODEL` default `claude-opus-5-5`; `TZ` default `Europe/Paris`; `CHECKIN_HOUR` default 18; `DATA_DIR` default `./data` (`/data` in Docker).
- Constants: history replay limit 24 messages; check-in every 2 days; Discord chunk size 1900 characters; `max_tokens=8000`.
- Only `ALLOWED_USER_ID` is answered. `.env` and `data/` (except `data/profile.example.json`) are git-ignored.
- No `temperature`, `thinking` or `budget_tokens` parameters.

## Review Focus

1. A reply longer than 2000 characters, or with a very long line, must be split without losing text.
2. Check-in fires at most once per 2 days even if the bot restarts at 18:00, runs twice the same day, or the previous check-in was on the first of a month.
3. The model sends a bad date, bad types or a missing profile file: tools return an error string, nothing raises, the database is not corrupted.
4. An Anthropic API error mid-run: the user gets a French message, the lock is released, the next message works, and no half-saved turn lands in the history.
5. History replay when the oldest stored message is from the assistant, when the table is empty, and when two messages arrive back to back.
6. Charts with 0, 1 or a single panel of data, with dates out of order or a benchmark logged twice the same day, with very long histories, and with the weekly auto-chart when nothing new was logged.

---

## File Structure

- `app/config.py`: settings and `require()`.
- `app/db.py`: schema, connection helper, message history, kv.
- `app/runtime.py`: `Agent`, `run_agent`.
- `app/coach.py`: the eight tools and `COACH`.
- `app/exercises.py`, `app/exercises_seed.json`: exercise catalog, seeding and `linkify`.
- `app/progress.py`: progress data, statistics and chart rendering.
- `app/bot.py`: Discord client, registry, chunking, check-in job.
- `prompts/coach.md`, `data/profile.example.json`.
- `tests/conftest.py`, `tests/test_db.py`, `tests/test_runtime.py`, `tests/test_coach_tools.py`, `tests/test_exercises.py`, `tests/test_progress.py`, `tests/test_bot.py`.
- `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.env.example`, `Dockerfile`, `compose.yaml`, `README.md`.

---

### Task 1: Project skeleton, config and test harness

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.env.example`, `app/__init__.py`, `app/config.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces in `config.py`: `DATA_DIR: Path`, `DB_PATH: Path` (`DATA_DIR/"agents.sqlite3"`), `PROFILE_PATH: Path` (`DATA_DIR/"profile.json"`), `PROMPTS_DIR: Path`, `MODEL: str`, `TZ: ZoneInfo`, `CHECKIN_HOUR: int`, `CHECKIN_EVERY_DAYS = 2`, `HISTORY_LIMIT = 24`, `require(name: str) -> str` (raises `SystemExit` naming the variable).
- `tests/conftest.py`: sets `os.environ.setdefault("ANTHROPIC_API_KEY", "test")` at import; fixture `tmp_db` monkeypatches `config.DATA_DIR/DB_PATH/PROFILE_PATH` into `tmp_path` and calls `db.init()` (the fixture may import `db` lazily so Task 1's tests do not need it).

- [ ] **Step 1: Write the failing tests** (`tests/test_config.py`): `test_require_returns_value`, `test_require_missing_exits_with_name` (`pytest.raises(SystemExit)` and the variable name in the message), `test_model_default_is_opus_5_5`.
- [ ] **Step 2:** Create the virtualenv and install dependencies (commands above). Run `.venv/bin/python -m pytest tests/test_config.py -v`. Expected: FAIL (`app.config` missing).
- [ ] **Step 3: Implement** `config.py` and the listed files; `.gitignore` lists `.env`, `.venv/`, `__pycache__/`, `data/` with `!data/profile.example.json`.
- [ ] **Step 4: Run** the tests. Expected: PASS.

### Task 2: Database layer

**Files:**
- Create: `app/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `SCHEMA` (tables `planned_sessions(date PK, kind, description, created_at)`, `session_logs(id, date, completed INT, done, rpe INT NULL, notes NULL, distance_m INT NULL, duration_min INT NULL, efforts INT NULL, effort_s INT NULL, rest_s INT NULL, created_at)`, `benchmarks(id, date, distance_m INT, avg_split_s REAL NULL, notes NULL, created_at)`, `messages(id, channel, role, content, created_at)`, `kv(key PK, value)`, `exercises(key TEXT PK, name TEXT, aliases TEXT, explanation TEXT, url TEXT)` with `aliases` comma-separated; `key` is English, `name`, `aliases` and `explanation` are French because the user sees them); `now() -> str` (ISO, `config.TZ`); `connect()` context manager (creates `DATA_DIR`, `row_factory=sqlite3.Row`, commits on success); `init() -> None`; `save_message(channel: str, role: str, content: str) -> None`; `load_messages(channel: str, limit: int) -> list[dict]` (oldest first, `{"role","content"}`, dropping leading non-user messages); `kv_get(key: str) -> str | None`; `kv_set(key: str, value: str) -> None` (upsert).

- [ ] **Step 1: Write the failing tests** (`tmp_db`): `test_load_messages_orders_oldest_first_and_respects_limit`, `test_load_messages_drops_leading_assistant` (`assistant,user,assistant` stored with limit 10 → starts with the user), `test_load_messages_empty_channel_returns_empty_list`, `test_channels_are_isolated`, `test_kv_upsert`, `test_init_is_idempotent`.
- [ ] **Step 2: Run** `pytest tests/test_db.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `db.py`.
- [ ] **Step 4: Run.** Expected: PASS.

### Task 3: Shared run loop

**Files:**
- Create: `app/runtime.py`, `tests/test_runtime.py`

**Interfaces:**
- Consumes: `db.load_messages`, `db.save_message`, `config.*`.
- Produces: `@dataclass(frozen=True) class Agent: name: str; prompt_file: str; tools: list`; module attribute `client` (`anthropic.AsyncAnthropic()`); `async def run_agent(agent: Agent, channel: str, user_text: str) -> str`.
- Behavior: system = `[ {type:"text", text:<prompt file>, cache_control:{type:"ephemeral"}}, {type:"text", text:"Today: <English weekday name> <ISO date>."} ]`; messages = history + the new user message; `client.beta.messages.tool_runner(model=config.MODEL, max_tokens=8000, system=..., tools=agent.tools, messages=...)` iterated with `async for`, keeping the last message; reply = concatenated text blocks, stripped; empty reply → `"Je n'ai pas pu formuler de réponse, peux-tu reformuler ?"`, and `"Je ne peux pas répondre à ça."` when `stop_reason == "refusal"`; **both turns are saved only after a successful run** (an exception leaves the history unchanged); one `asyncio.Lock` per agent name serializes runs.

- [ ] **Step 1: Write the failing tests** with a fake `client` whose `beta.messages.tool_runner(**kw)` records `kw` and returns an async iterator: `test_returns_text_and_saves_both_turns`, `test_system_has_cache_block_then_date_block` (assert `cache_control == {"type":"ephemeral"}` on block 0 and `"Today:"` prefix on block 1), `test_history_is_replayed_before_new_message`, `test_empty_reply_uses_fallback_and_refusal_text`, `test_exception_leaves_history_unchanged_and_releases_lock` (first call raises, second call succeeds), `test_runs_for_same_agent_are_serialized` (two concurrent calls never overlap inside the fake).
- [ ] **Step 2: Run** `pytest tests/test_runtime.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `runtime.py`.
- [ ] **Step 4: Run.** Expected: PASS.

### Task 4: Coach tools, prompt and profile

**Files:**
- Create: `app/coach.py`, `prompts/coach.md`, `data/profile.example.json`, `tests/test_coach_tools.py`

**Interfaces:**
- Produces in `coach.py` (all `@beta_async_tool`, docstrings with `Args:` sections, return JSON strings via `json.dumps(..., ensure_ascii=False)`): `get_profile()`, `update_profile(key: str, value: str)` (value parsed as JSON else stored as text), `save_planned_session(day: str, kind: str, description: str)` (upsert), `get_plan(start: str, end: str)` → `{"planned": [...], "logged": [...]}`, `log_session(day: str, completed: bool, done: str, rpe: int = 0, notes: str = "", distance_m: int = 0, duration_min: int = 0, efforts: int = 0, effort_s: int = 0, rest_s: int = 0)` (every numeric 0 → NULL; `get_plan` and `get_recent_sessions` return all structured fields), `get_recent_sessions(days: int = 14)`, `record_benchmark(day: str, distance_m: int, avg_split_s: float = 0.0, notes: str = "")`, `get_benchmarks()`; `TOOLS: list`; `COACH = Agent(name="coach", prompt_file="coach.md", tools=TOOLS)`.
- Invalid ISO dates return `{"error": "..."}`; `kind` must be one of `rowing|hiit|walk|strength|mobility|rest` else error; no tool takes a list parameter.
- Prompt content (written in English; it tells the model to reply to the user in French with tutoiement, and to write session descriptions, `done` texts and notes in English): role and tone; mission (weekly plan, check-in every 2 days, goal weight loss without injury, 30–45 min/day, rower/bodyweight/walking); work rules (tools are the only memory: read profile and plan before planning, never invent past sessions, log before answering, never say a plan is saved without calling `save_planned_session`, absolute dates in tools); default weekly structure; adaptation rules (compare planned vs logged: user did more and felt good → progress next sessions, mostly via RPE 7→8 and effort/rest ratio, not by piling on reps; user did less or felt bad → hold or lower; always log what was actually done before replying; ask for the rowing distance only after rower sessions, optional, never after HIIT; when the user reports numbers, also pass them as structured fields (HIIT "12 x 45 s / 45 s" → `efforts=12, effort_s=45, rest_s=45`; rower → `distance_m`, `duration_min`) while keeping the free text in `done`; RPE scale 3–4/6–7/8–9; heavy legs after 2 days, bad sleep or dread → drop 3 reps; joint pain → stop and refer to a professional; travel → bodyweight circuit or outdoor walk/jog; HIIT exercise menu and Saturday strength circuit; benchmark about every 4 weeks); check-in behavior for messages starting with `[check-in]` (one short follow-up, not an interrogation); limit (nutrition goes to the nutritionist).
- `profile.example.json` fields: `age`, `height_cm`, `weight_kg` (null), `goal`, `available_minutes_per_day`, `equipment`, `injuries_or_limits`, `weekly_structure` (keys `mon..sun`; all text values in English), `history` (two strings summarizing months 1 and 2 from `history/*.md`).

- [ ] **Step 1: Write the failing tests** (`tmp_db`, call tools with `.call({...})`): `test_save_planned_session_upserts_same_day`, `test_invalid_date_and_kind_return_error_json` (for `save_planned_session`, `log_session`, `get_plan`, `record_benchmark`), `test_log_session_stores_null_for_zero_rpe_and_distance` and `test_log_session_stores_distance_and_hiit_fields` (`efforts=12, effort_s=45, rest_s=45` round-trip through `get_recent_sessions`), `test_get_plan_joins_planned_and_logged_in_range`, `test_get_recent_sessions_excludes_older_than_n_days`, `test_benchmarks_ordered_oldest_first`, `test_profile_missing_returns_error_json`, `test_update_profile_parses_json_or_text` (`"82"` → 82, `"genou sensible"` → string), `test_tool_schemas_have_expected_required_fields` (via `.to_dict()["input_schema"]["required"]`, e.g. `log_session` requires `day`, `completed`, `done`), `test_example_profile_is_valid_json_with_weekly_structure`, `test_prompt_mentions_every_tool_name` (prompt ignores the exercise tools; they are for the bot and for questions), `test_prompt_has_no_hardcoded_one_point_five_rule` (the text "1,5" and "1.5x" do not appear) (read `prompts/coach.md`, assert each tool name appears).
- [ ] **Step 2: Run** `pytest tests/test_coach_tools.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the tools, write the prompt and the example profile.
- [ ] **Step 4: Run** `pytest -v`. Expected: PASS.

### Task 5: Exercise catalog and automatic links

**Files:**
- Create: `app/exercises.py`, `app/exercises_seed.json`, `tests/test_exercises.py`
- Modify: `app/coach.py` (add three tools; add them to `TOOLS`), `tests/test_coach_tools.py`

**Interfaces:**
- Consumes: `db.connect`, the `exercises` table (Task 2).
- Produces in `exercises.py`: `seed() -> None` (inserts rows from `app/exercises_seed.json` whose `key` is missing; never overwrites existing rows; called from `db.init()` is **not** allowed, call it from `bot.main()` after `db.init()`); `list_all() -> list[dict]` (`name, aliases, explanation, url`); `get(name: str) -> dict | None` (case- and accent-insensitive match on name or alias); `set_url(name: str, url: str) -> bool` (only `http://` or `https://` URLs; returns False if the exercise is unknown or the URL is invalid); `linkify(text: str) -> str`.
- `linkify` rules: replace each occurrence of a name or alias (case-insensitive, whole words, accents respected as written) by `[<text as written>](<url>)`; longest name first so `Squat jumps` wins over `Squats`; text inside triple-backtick blocks, inside inline backticks and inside an existing `[...](...)` is left alone; an exercise without URL is left unlinked.
- Seed content: the 12 exercises named in the spec (decision 9b), an English `key` (e.g. `reverse_lunges`), French display names, French and English aliases for common variants (e.g. `squat`, `squats`; `burpees`; `planche`, `gainage`), 2–3 line explanation each (starting position, movement, one safety cue), `url` = `https://www.youtube.com/results?search_query=` + URL-encoded French exercise name + `+exercice`.
- New tools in `coach.py` (`@beta_async_tool`): `list_exercises()` (names and one-line explanation only), `get_exercise(name: str)` (full explanation and link, or `{"error": "unknown exercise"}`), `set_exercise_url(name: str, url: str)`.

- [ ] **Step 1: Write the failing tests** (`tmp_db`):
  - `test_seed_inserts_all_exercises_once_and_keeps_user_edits` (seed twice; `set_url` then seed again keeps the new URL).
  - `test_get_matches_alias_and_ignores_case_and_accents` (`"FENTES ARRIERE"`, `"fentes arrière"`, alias).
  - `test_set_url_rejects_non_http_and_unknown`.
  - `test_linkify_links_known_names`: `"Mountain climbers puis squats"` → both become masked links with the stored URLs.
  - `test_linkify_longest_match_wins`: `"squat jumps"` produces one link whose target is the squat-jumps URL, not a link on `squat`.
  - `test_linkify_skips_code_blocks_inline_code_and_existing_links`.
  - `test_linkify_is_idempotent` (applying it twice equals applying it once).
  - `test_linkify_leaves_unknown_words_and_partial_words` (`"squatter"` untouched).
  - In `test_coach_tools.py`: `test_exercise_tools_roundtrip` (`list_exercises`, `get_exercise`, `set_exercise_url` through `.call`).
- [ ] **Step 2: Run** `pytest tests/test_exercises.py tests/test_coach_tools.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `exercises.py`, the seed file and the three tools; mention in `prompts/coach.md` that exercise names are linked automatically by the system and that the coach should use the catalog names and never write URLs itself.
- [ ] **Step 4: Run** `pytest -v`. Expected: PASS.

### Task 6: Progress data and charts

**Files:**
- Create: `app/progress.py`, `tests/test_progress.py`

**Interfaces:**
- Consumes: tables `benchmarks` and `session_logs` (Task 2 columns), `db.connect`.
- Produces in `progress.py`:
  - `@dataclass class ProgressData: benchmarks: list[tuple[date, int, float | None]]` (day, distance_m, avg_split_s); `hiit: list[tuple[date, int, int, int]]` (day, efforts, effort_s, rest_s; only completed logs with both `effort_s` and `rest_s`); `rows: list[tuple[date, int, int | None]]` (day, distance_m, rpe; only completed logs with `distance_m`, excluding days that also have a benchmark)
  - `load_progress() -> ProgressData` (all dates ascending)
  - `missing_panels(data: ProgressData) -> list[str]` (French names of the panels that have fewer than 2 points: `"benchmark rameur"`, `"HIIT effort/repos"`, `"distance rameur"`)
  - `summarize(data: ProgressData) -> dict` (JSON-serializable; keys present only when computable: `benchmark: {first_m, latest_m, delta_m, delta_pct, first_split_s, latest_split_s, delta_split_s}`, `hiit: {first_ratio, latest_ratio, first: {efforts, effort_s, rest_s}, latest: {...}}` with ratio = effort_s / rest_s rounded to 2 decimals, `sessions_last_28_days: int` counting completed logs in the 28 days before the latest log)
  - `render_png(data: ProgressData) -> bytes | None` (None when every panel has fewer than 2 points)
- Chart rules: matplotlib `Agg` backend, one figure with one subplot row per available panel (width 8 in, 3.2 in per panel, `dpi=150`, so at most 1200 px wide), French titles and axis labels, dates as `jj/mm`; panel 1 "Benchmark rameur 20 min (RPE 5)": line with markers of distance in meters, last point annotated with the gain since the first; panel 2 "HIIT: temps d'effort vs repos (secondes)": grouped bars per session (effort and rest in two distinguishable colors, colorblind-safe, with direct labels or a legend) and the effort:rest ratio written above each pair; panel 3 "Distance par séance de rameur (m)": markers connected by a line. Follow the `dataviz` skill (loaded before writing chart code): restrained palette, no chart junk, legible on a phone, readable in dark mode when sent on Discord (opaque white figure background).

- [ ] **Step 1: Write the failing tests** (`tmp_db`, rows inserted with SQL):
  - `test_load_progress_filters_completed_and_structured_rows` (a skipped session and a HIIT without `rest_s` are excluded).
  - `test_summarize_benchmark_and_hiit`: benchmarks (2026-10-12, 4000 m, 150.0 s) and (2026-11-08, 4300 m, 145.0 s) → `delta_m == 300`, `delta_pct == 7.5`, `delta_split_s == -5.0`; HIIT sessions 12 × (40 s / 50 s) then 12 × (50 s / 40 s) → `first_ratio == 0.8`, `latest_ratio == 1.25`.
  - `test_summarize_omits_keys_without_enough_data` (one benchmark → no `benchmark` delta keys; empty database → `{"sessions_last_28_days": 0}`).
  - `test_missing_panels_lists_french_names`.
  - `test_render_png_returns_png_bytes`: with all three panels populated the result starts with `b"\x89PNG\r\n\x1a\n"` and the decoded width is ≤ 1200 px (read the IHDR chunk, no extra dependency).
  - `test_render_png_none_when_not_enough_data`.
  - `test_render_png_with_only_one_panel_is_shorter_than_three` (height of the one-panel image < height of the three-panel image).
  - `test_render_handles_long_history` (60 HIIT sessions render without error and within the width limit).
- [ ] **Step 2: Run** `pytest tests/test_progress.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `progress.py` (import matplotlib lazily inside `render_png` and select the `Agg` backend there).
- [ ] **Step 4: Run** `pytest -v`. Expected: PASS. Then save one sample chart from a fixture dataset to the scratchpad directory, open it and check labels, accents and contrast by eye.

### Task 7: Discord bot

**Files:**
- Create: `app/bot.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `COACH`, `run_agent`, `db`, `config`.
- Produces: `chunks(text: str, size: int = 1900) -> Iterator[str]` (splits on the last newline in the second half of the window, else hard-splits; joining the parts with `""` after re-adding stripped newlines never loses non-whitespace content); `should_checkin(last: str | None, today: date, every_days: int = 2) -> bool`; `AGENTS: dict[int, Agent]` (`{COACH_CHANNEL_ID: COACH}`); `async def ask(channel, agent: Agent, text: str) -> None` (typing indicator, `run_agent(agent, str(channel_id), text)`, `anthropic.APIError` → `"Erreur côté API Claude (<ClassName>). Réessaie dans un moment."`, then `exercises.linkify(reply)` **before** splitting into chunks, sent with `suppress_embeds=True`); `on_message` (ignores bots and anyone but `ALLOWED_USER_ID`, ignores unregistered channels, `!checkin` becomes `"[check-in] Manual check-in requested."`, `!progress` calls `send_progress(channel, force=True)`); `async def send_progress(channel, force: bool) -> None` (loads `progress.load_progress()`; if `render_png` returns None and `force` → message `"Pas encore assez de données pour un graphique: il me faut au moins 2 mesures (<liste des panneaux manquants>)."`; otherwise runs the coach with a `[progress]` trigger containing the `summarize` numbers as JSON and the instruction to write 2–3 encouraging sentences using only those numbers, then sends the reply with the PNG as `discord.File(io.BytesIO(png), filename="progres.png")`; records `kv["last_progress"]` = today); `should_send_progress(last: str | None, today: date, has_new_data: bool) -> bool` (True when `has_new_data` and (`last` is None or at least 7 days old)); `checkin_job` additionally calls `send_progress(channel, force=False)` when `should_send_progress` allows it; `checkin_job()` (calls `should_checkin`, writes `kv["last_checkin"]` **before** asking, text `"[check-in] Automatic end-of-day check-in."`); `main()` (`logging.basicConfig`, `db.init()`, `exercises.seed()`, `client.run(config.require("DISCORD_TOKEN"))`); scheduler: APScheduler `AsyncIOScheduler` with a cron job daily at `CHECKIN_HOUR:00` in `config.TZ`, started once in `on_ready`.
- Importing `app.bot` requires `ALLOWED_USER_ID` and `COACH_CHANNEL_ID`; tests set them with `monkeypatch.setenv` and import inside the test.

- [ ] **Step 1: Write the failing tests**: `test_chunks_short_text_single_chunk`; `test_chunks_long_text_each_under_limit_and_no_content_lost` (4500 chars with newlines, and a 4500-char single line); `test_should_checkin_rules` (`None` → True; 1 day ago → False; 2 days ago → True; last on `2026-10-31`, today `2026-11-02` → True); `test_ask_sends_api_error_message` with a fake channel (`run_agent` patched to raise `anthropic.APIConnectionError`) asserting one message starting with `"Erreur côté API Claude"`; `test_ask_links_exercise_names_and_suppresses_embeds` (fake channel records `send` kwargs: text contains a masked link, `suppress_embeds is True`), `test_checkin_job_records_state_before_asking` (patch `ask` to raise, assert `kv["last_checkin"]` is already today's date); `test_should_send_progress_rules` (no new data → False; None + new data → True; 3 days ago → False; 7 days ago + new data → True), `test_progress_command_sends_png_and_encouragement` (patch `run_agent` and `progress.render_png`; `send` receives a file named `progres.png`), `test_progress_command_without_data_explains_what_is_missing`, `test_other_users_are_ignored` with a fake message from a different `author.id`.
- [ ] **Step 2: Run** `pytest tests/test_bot.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `bot.py`.
- [ ] **Step 4: Run** `pytest -v`. Expected: ALL PASS.

### Task 8: Packaging, docs and live verification

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `README.md`

**Interfaces:** none new.
- `Dockerfile`: `python:3.12-slim`, `DATA_DIR=/data`, installs `requirements.txt`, copies `app/` and `prompts/`, non-root user owning `/data`, `CMD ["python","-m","app.bot"]`. `compose.yaml`: service `agents`, `restart: unless-stopped`, `env_file: .env`, `TZ: Europe/Paris`, volume `./data:/data` with a comment to point it at a TrueNAS dataset. `README.md` (English): Discord Developer Portal steps (bot, Message Content intent, invite scope `bot` with View/Send Messages/Read History), copying IDs with Developer Mode, `.env` and `profile.json` setup, local run, TrueNAS deployment, and where to look to understand the code.

- [ ] **Step 1:** Write the three files.
- [ ] **Step 2:** If Docker is available: `docker build -t agents .` Expected: success (otherwise note it as not run).
- [ ] **Step 3 (manual, needs the user's keys):** start the bot; message `#coach` "Planifie ma semaine". Expected: the coach reads profile/plan via tools, proposes a week, and after "ok" calls `save_planned_session` for each day (check with `sqlite3 data/agents.sqlite3 "select * from planned_sessions"`).
- [ ] **Step 4 (manual):** tell the coach what you did on a day; expected `session_logs` row with the RPE. Type `!checkin`; expected a short check-in message referencing the logged session.
- [ ] **Step 5 (manual):** restart the bot right after the check-in; expected no second automatic check-in the same day.
- [ ] **Step 6 (manual):** log two HIIT sessions with different effort/rest times and two rower sessions with distances, then type `!progress`. Expected: a PNG with the matching panels and 2–3 encouraging sentences that quote only the real numbers. With no data, expected: the message explaining what is missing.

### Task 9: One-off HIIT backfill

**Files:**
- Create: `scripts/__init__.py`, `scripts/backfill_hiit.py`, `data/backfill_hiit.json` (filled with the user's confirmed dates; git-ignored like the rest of `data/`), `tests/test_backfill.py`

**Interfaces:**
- Consumes: `db.connect`, `session_logs` columns (Task 2).
- Produces: `backfill(entries: list[dict]) -> int` (returns the number of rows inserted); CLI `python -m scripts.backfill_hiit data/backfill_hiit.json`, which calls `db.init()` first and prints the count.
- Entry shape: `{"day": "YYYY-MM-DD", "efforts": int, "effort_s": int, "rest_s": int}`. Inserted row: `completed=1`, `done="HIIT {efforts} x ({effort_s}s / {rest_s}s)"`, the three structured fields, `rpe=NULL`, `notes="reconstructed from plan"`. An entry is skipped if a log with the same `day` and the same `notes` marker already exists (idempotent). Invalid entries (bad date, non-positive numbers, missing key) raise `ValueError` before anything is written (all-or-nothing).
- The JSON file is written **only after the user confirms the values**. Proposed entries (day, efforts, effort_s, rest_s): month 1 on Wednesdays `2026-09-16 (9, 20, 90)`, `2026-09-23 (12, 25, 75)`, `2026-09-30 (15, 30, 60)`, `2026-10-07 (12, 40, 50)`; month 2 `2026-10-14 (12, 40, 50)`, `2026-10-21 (12, 45, 45)`, `2026-11-04 (12, 45, 45)`, `2026-11-11 (12, 45, 45)` (the user confirmed these last two). Nothing for 26 Oct–1 Nov (ill). The user's next HIIT is Wed 2026-11-18 (third session at 12 x 45 s / 45 s); it is logged live, not backfilled. Total: 8 backfilled sessions.

- [ ] **Step 1: Write the failing tests** (`tmp_db`): `test_backfill_inserts_structured_rows`; `test_backfill_twice_inserts_once`; `test_invalid_entry_writes_nothing` (valid + invalid entry → `ValueError` and zero rows); `test_backfilled_rows_feed_progress` (after backfilling 2 sessions, `progress.load_progress().hiit` has 2 entries and `summarize` gives `first_ratio` and `latest_ratio`).
- [ ] **Step 2: Run** `pytest tests/test_backfill.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `scripts/backfill_hiit.py`.
- [ ] **Step 4: Run** `pytest -v`. Expected: PASS.
- [ ] **Step 5 (manual, after the user confirms dates):** write `data/backfill_hiit.json`, run the CLI against the real database, check with `sqlite3 data/agents.sqlite3 "select date, done from session_logs"`, then type `!progress` in `#coach`.
