# Structured interface and security hardening: design

Status: draft for approval. Not committed (user's rule: never commit).

## Intent and why

The app will run as a Docker stack on the user's NAS (TrueNAS). The user wants a stack with good security standards, wants to learn about those standards, and wants the NAS protected from prompt injection and attacks. Three requests came out of the security assessment:

- The nutritionist must not be able to change the database.
- Discord must be locked down: a person (or a hijacked account) should not be able to push arbitrary instructions at the agents. The user proposed replacing chat with buttons, selects and tightly typed inputs.
- The coach is too insistent: it keeps asking for past session reports. The user wants to decide when to send logs, and the coach never to chase them.

## Goals

1. **No free chat.** Every input is a slash command, button, select or modal with validated values. The one free-text input is the fridge (bounded, parsed by code).
2. **The model proposes, code writes.** Models get no write tools. Every write to SQLite or Mealie happens in handler code after a user action. Model reads go through a read-only SQLite connection and a read-only Mealie token.
3. **Pull-based coach.** No automatic check-ins, no requests for reports or distances. Missing logs mean "unknown", never "failed".
4. **Container and network hardening** suited to a NAS: minimal privileges, a read-only filesystem, an egress allowlist, pinned dependencies.
5. **The user learns the standards.** A `docs/security.md` explains each threat and control, where it lives, and how to check it.
6. **Stay on Discord now, keep a thin seam** between "a user action happened" and "the planner runs", so a web front end can replace Discord later.

## Non-goals

- A web UI, Matrix or Signal now (see Approaches).
- End-to-end encryption of the chat. Discord and Anthropic still see what is sent.
- Multi-user support. One allowed user stays the model.
- Changing the meal rules, the Mealie commit logic or the progress charts' content.

## Anti-goals

- The coach sends an unprompted message that asks about the past. A passive weekly post with a button is allowed; a question is not.
- Any path where model output becomes a write without a user press.
- Regressing the idempotent, version-bound Mealie commit.
- Security that depends on the prompt ("treat recipe text as data" stays, but is not a control).

## Threat model (short)

| Actor / event | Impact today | Main controls here |
|---|---|---|
| Prompt injection via Mealie recipe text (imported from the web) | Can edit fridge, away days, draft; poisoned recipe confirmed unseen | No model write tools; closed recipe list; shown-in-full confirmation; output validators |
| Hijacked Discord account | Can drive agents and press Confirmer | Only enumerated choices exist; locked channels; per-day proposal cap |
| Leaked bot token | Impersonate the bot, read channels | Minimal channel permissions, no Message Content intent, token in a secret file, rotation steps |
| Compromised dependency or container | Reaches the NAS LAN, reads all secrets | Pinned and hashed dependencies, read-only container, egress allowlist, no capabilities |
| Runaway model loop | Unbounded API spend | Single-shot calls, iteration cap, daily proposal cap, Anthropic workspace spend limit |

## Approaches considered

- **A. Discord, structured inputs only.** Slash commands, buttons, selects and modals; channels locked for humans. Smallest step from today; no new infrastructure.
- **B. Self-hosted web app behind Tailscale.** Same forms, no third-party chat provider, smallest attack surface; means writing a UI and adding a notification path.
- **C. A now, behind a thin interface, B later if wanted.**

Chosen: **C (A now)**. This is my call: it was the recommendation in the conversation and the user answered by refining the flow, not by objecting. Telegram gives no real privacy gain over Discord; Matrix and Signal cost more than they return for one user. Revisit if the user wants B.

## Design

### 1. Interaction layer

- Delete `on_message`, `!progress`, `!checkin`. Turn the Message Content intent off in code and in the Developer Portal.
- Commands: `/coach plan`, `/coach log`, `/coach progress`, `/coach profile`, `/nutrition plan`, `/nutrition fridge`. Each is available only in its own channel.
- A `CommandTree.interaction_check` and every button, select and modal callback reject any user who is not `ALLOWED_USER_ID`, with an ephemeral message. This replaces the check in `on_message`.
- Humans cannot send messages in the two channels (channel permission override); they can use application commands. The bot has View, Send, Read History, Attach Files, and nothing else.
- `discord.Client(allowed_mentions=AllowedMentions.none())` globally, so no output can ping.
- Views are persistent: fixed `custom_id`s and `add_view` in `setup_hook`, so buttons survive a restart (today's `ConfirmView` does not).
- Handlers `defer()` immediately (Discord's 3 s limit), then run the planner and follow up. A message holds at most 5 component rows and a select 25 options; layouts must respect that.

### 2. Coach flow (pull-based)

- **Plan the week** (`/coach plan`): multi-select of training days; select of week intensity (light, normal, heavy); optional select "last week felt: easier, as planned, harder, skip". The "last week" answer is the only feedback collected, and only at the moment the user plans.
- The planner makes one model call with forced structured output: for each chosen day `{day, kind, description}` plus one short rationale. Code validates it: days only from the selection, `kind` in `KINDS`, at least 2 rest days a week, bounded text, no URLs or mentions.
- The proposal is a table with a Confirm button; confirming writes `planned_sessions`. After the proposal, the user can nudge one day's intensity up or down with a button (layout left to the builder).
- **Log** (`/coach log`): day select (last 7 days), done / partly / skipped, RPE 1-10, then a small modal for numbers (distance, duration, HIIT efforts / effort / rest) as validated integers, and a benchmark toggle. A **Pain** button saves the log and the code itself posts the stop-and-see-a-professional text; no model call.
- No free-text notes in v1. My call: nothing the user types freely reaches the model.
- **No nagging.** Remove `checkin_job`, `CHECKIN_HOUR`, `CHECKIN_EVERY_DAYS`, the `[check-in]` trigger and the prompt's check-in section. The prompt gains: never ask for reports, distances or missing logs; an unlogged session is unknown; with missing data hold the load rather than raise it.
- **Progress chart:** on demand only (`/coach progress`). My call: the automatic weekly chart goes off (setting `PROGRESS_AUTO_DAYS`, default 0).
- **Optional reminder:** one passive weekly post with a "Plan the week" button, off by default for the coach (`COACH_PLAN_REMINDER_HOUR` unset). It never asks a question.
- `/coach profile`: a modal with bounded fields (weight, injury text excluded) replaces the model's `update_profile` tool. `set_exercise_url` stops being a model tool; URLs are changed by code or a script.

### 3. Nutritionist flow

- `/nutrition plan`: multi-select of days to cook, select of new-recipe count (0 to 5), a Fridge button (modal), then Propose.
- Fridge modal: one text input, 600 characters at most. Code parses `name: quantity` lines: at most 40 items, each matching a letters, digits, spaces and basic punctuation allowlist, no URLs or markdown. Rejected lines are reported to the user. The model receives the fridge as a JSON field, never inside instruction text.
- The model sees recipe names, slugs and tags from Mealie, never recipe bodies. It returns existing dinners as slugs from that closed list, plus new recipes (name, ingredients, steps) in a forced structured format. Code checks that every slug is in the list, the new-recipe count is exact, dinner types follow `get_meal_targets`, and text is bounded with no URLs, mentions or markdown links.
- The confirmation shows the full content of new recipes and the shopping list (a "Details" button for the long part), not only dish names.
- Per-day **Swap** button with an optional constraint select (fish, quick, veggie, no preference) re-runs that single day, bumps the draft version and re-posts the table. The existing version-bound Confirmer and idempotent commit stay as they are.
- The weekly Sunday run stays but becomes a passive post with a "Plan dinners" button (no questions).

### 4. Model runtime

- Replace the chat loop with single-shot jobs: `plan_week(inputs) -> Proposal`. No chat history is replayed; the `messages` table is no longer written.
- Models keep only read tools, wrapped in the read-only connection: `get_profile`, `get_plan`, `get_recent_sessions`, `get_benchmarks`, `get_meal_targets`, `get_fridge`, `get_draft`, and the two Mealie reads. All write tools are removed from the model and their logic is called by handlers.
- Caps: `max_tokens` lowered, a small iteration limit on the tool runner (check the SDK parameter name when building), and a daily proposal limit stored in SQLite. The user sets a monthly spend limit on the Anthropic workspace (manual, in the checklist).

### 5. Database and Mealie

- Two connection factories in `app/db.py`: read-write for handlers, and `connect_ro()` (URI `mode=ro` plus `PRAGMA query_only=ON`) for every model read. The nutritionist's reads of `planned_sessions` use it. A test proves a write through it fails.
- One SQLite file stays (my call). Splitting into two files or containers is a documented later step, not part of this work.
- Mealie: split `MealieClient` into a reader (search, get recipe) and a writer (create recipe, plan entry, shopping). Two tokens: `MEALIE_READ_TOKEN` for the model's reads, `MEALIE_WRITE_TOKEN` used only by the commit (see the amendment: Mealie cannot give them different rights). If only the old `MEALIE_API_TOKEN` is set, use it for both and log a warning.

### 6. Container and network

- `compose.yaml`: `read_only: true` with tmpfs for `/tmp` and matplotlib's cache, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `pids_limit`, memory and CPU limits, a healthcheck.
- Secrets as files (`*_FILE` support in `config.py`) instead of one `.env` holding everything. `data/` is mode 700.
- Network: the agents container sits on an internal network and reaches the internet only through an egress proxy sidecar with a domain allowlist (Discord gateway and API, `api.anthropic.com`, the Mealie host). No route to the rest of the LAN. The exact domain list must be confirmed by running it.
- Supply chain: base image pinned by digest, dependencies locked with hashes (`--require-hashes`), `pip-audit` and `hadolint` added to pre-commit or CI.
- A nightly SQLite backup (`.backup`) to another dataset is documented.
- I could not run Docker in the session environment, so the compose hardening must be verified on the NAS.

### 7. Documentation

`docs/security.md`: threat model, a table of threat, control, where it lives and how to verify it, the manual Discord checklist (private server, no Administrator, Public Bot off, 2FA, channel overrides, token rotation), and pointers for learning more (OWASP Top 10 for LLM applications, notably prompt injection and excessive agency; the CIS Docker Benchmark).

## Order of work

Each phase ships on its own.

1. Pull-based coach: remove the check-in job and prompt text, progress on demand only. Fixes the user's main annoyance quickly.
2. Read-only database connection, Mealie reader/writer split and token split, output validators.
3. Structured interface and single-shot planners (the largest phase).
4. Container and network hardening (optionally with the Mealie gate, `docs/mealie-gate.md`).
5. `docs/security.md` and the Discord checklist.

## Tests

- A write through `connect_ro()` raises; model tools use it.
- Validators: slug outside the closed list, URLs, mentions, markdown links, wrong counts and bad enums are rejected.
- An interaction from another user is rejected for every command, button, select and modal.
- No `on_message` handler and no scheduled check-in exist; views are registered at start.
- The fridge parser accepts normal lines and rejects the rest.
- The existing Mealie commit and confirm tests keep passing.

## Left to the builder

- Exact component layout within Discord's row and option limits, and how the per-day intensity nudge looks.
- Whether log numbers go in a modal or selects, depending on what discord.py's installed version supports in modals.
- Egress proxy software and the final domain allowlist.
- The hash-locking tool, and the schema migration for the pain flag on `session_logs`.
- Whether to drop the unused `messages` table.

## Amendments

**2026-11-25: Mealie cannot restrict a token.** Mealie tokens carry all their user's rights and Mealie has no read-only role, so the read/write token split enforces nothing on the Mealie side (the original text assumed two users with different rights). Decision (user): keep the split as enforced by the bot, fix the docs, and document a gate that would enforce it outside the bot in `docs/mealie-gate.md` without building it now.
