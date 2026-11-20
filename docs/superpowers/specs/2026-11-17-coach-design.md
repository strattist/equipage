# Coach agent and Discord bot foundation: design

Status: implemented on branch `coach-agent` (see the plan's implementation record and the amendments at the end). Written after the fact from decisions made in conversation; the first scaffold was rolled back so the work can follow spec, plan, tests.

## Intent and why

The user (back to sport after a sedentary period, goal: progressive, durable weight loss without injury) was coached in Claude chat sessions: monthly programs on a rower, HIIT, walking and light strength, adapted from their feedback (see `history/month1.md`, `history/month2.md`). They want that coaching to run by itself on their TrueNAS NAS and be controlled from Discord, and as a way to learn how to build agents. This spec covers the foundation (bot, shared database, shared run loop) and the first agent, the coach. The nutritionist is specified separately (`2026-11-17-nutritionist-design.md`) and builds on this foundation.

## Language policy (decided by the user)

- The agents **talk to the user in French** (tutoiement) and **save everything to Mealie in French**: recipe names, ingredients, steps, tags, shopping list items, meal plan notes.
- **Everything else is English**: the database (table and column names, and stored values such as session `kind`, planned-session descriptions, `done` texts and notes), code and comments, system prompts (written in English, with the instruction to reply in French), README, commands (`!checkin`, `!progress`), logs, environment variables, `profile.json` keys and values, tool names, arguments and descriptions, trigger markers (`[check-in]`, `[weekly]`, `[progress]`).
- **Exceptions, because the text is shown to the user or goes to Mealie as is:** messages the code sends to Discord (errors, button labels, chart titles and labels, table headers), the `messages` table (the real conversation, in French), the exercise catalog's display names, aliases and explanations, and the recipe content of the nutritionist's draft tables (it is committed to Mealie unchanged).
- The model translates when it reads English stored text to the user (e.g. a planned-session description).

## Goals

- A Discord bot in the user's server; the channel `#coach` is the coach's.
- The coach plans sessions **weekly, with a check-in every 2 days** (user's choice): a fixed weekly structure adapted to feedback, and an automatic check-in message that asks how the planned sessions went and announces what follows.
- **The coach adapts from what the user reports.** If the user says they did more than planned and felt good, the next sessions progress; if they say they did less or felt bad, the coach holds or lowers the load. This comparison of planned vs actually done is the main feedback loop.
- **Exercise names are clickable.** HIIT and strength exercises the user may not know appear as links to a short explanation or video; asking "c'est quoi les skaters ?" gets a short written explanation.
- On rower sessions the user can report the distance; it is stored and used to follow progress between benchmarks.
- **Progress charts to encourage the user.** A chart image is posted in `#coach` on request (`!progress`) and automatically about once a week, with a short encouraging message written by the coach from the real numbers. Panels (each shown only when it has at least 2 data points): rowing benchmark distance over time; HIIT effort time vs rest time per session (with the effort:rest ratio); distance per rower session.
- The coach has no memory of its own: plan, what the user actually did, and rowing benchmarks live in SQLite and are read and written through tools.
- Conversation in French, tutoiement, short Discord-friendly messages.
- Foundation reusable by the nutritionist: one bot process, one SQLite database under `/data`, one run loop shared by agents (an `Agent` = name + prompt file + tools).
- Runs in Docker on TrueNAS; the bot only makes outbound connections.

## Non-goals (v1)

- No weight or calorie tracking, no nutrition advice (the nutritionist's job; the coach says so and redirects).
- No wearable or heart-rate integration; effort is RPE (1–10) reported by the user.
- No automatic monthly program generation: planning is conversational, week by week.
- No slash commands or buttons for the coach (plain messages, plus `!checkin` to trigger a check-in by hand).

## Anti-goals

- The coach claims a plan is saved, or a past session happened, without having called the tool.
- Someone other than the user can talk to the bot.
- A restart or a retry posts a check-in twice.
- Medical advice. Joint pain stops the session and is referred to a professional.

## Constraints

- Charts are drawn with matplotlib (headless `Agg` backend) and sent as a PNG attachment; French labels; readable on a phone.
- Python 3.12 in the container (discord.py compatibility), `anthropic` SDK with the async tool runner, APScheduler 3.x, SQLite.
- Secrets only in `.env`: `ANTHROPIC_API_KEY`, `DISCORD_TOKEN`; `ALLOWED_USER_ID` and `COACH_CHANNEL_ID` identify the user and channel.
- The Discord "Message Content" intent must be enabled in the Developer Portal.
- Model from `CLAUDE_MODEL` (default `claude-opus-5-5`; `claude-sonnet-5-5` is the cheaper option). No `temperature` or `thinking` settings.

## Approaches considered

- **Claude API + tool runner, own bot (chosen).** Best for learning, full control, data stays on the NAS.
- Managed Agents (hosted by Anthropic): less code but state and sandbox live outside the NAS, less to learn.
- Claude Agent SDK: batteries included but aimed at coding agents.
- n8n/low-code: quick, but teaches little about agents.

## Decisions made by the user

- French; weekly structure with a check-in (not a day-by-day plan).
- Weekly structure inherited from the history: Mon steady row, Tue rest, Wed HIIT, Thu brisk walk, Fri rower intervals, Sat light strength, Sun rest/mobility; always 2 full rest days.
- The coach adapts the plan to the user's feedback in both directions (more than planned and felt good → progress; less than planned or felt bad → hold or lower). Progression is mostly by intensity (RPE) and effort/rest ratio rather than piling on repetitions.
- The user reports rower distance when they have it (optional, rower sessions only; not asked after HIIT).
- Exercise names link to a short explanation or video.
- Benchmark: 20 min steady row at RPE 5, distance and average split per 500 m, repeated about every 4 weeks.
- Profile (age, height, weight) is filled in by the user in a local file on the NAS.

## Decisions made by the design (my calls, for the user to check)

1. **Agents are data**: `Agent(name, prompt_file, tools)` plus `run_agent(agent, channel, user_text)`. Channel id → agent registry in the bot.
2. **Tools** (names indicative): `get_profile`, `update_profile`, `save_planned_session` (one per day, upsert), `get_plan(start,end)` (planned + logged), `log_session(day, completed, done, rpe, notes, distance_m, duration_min, efforts, effort_s, rest_s)`, `update_session_log(id, ...)` (adds details to an already logged session; see the amendments), `get_recent_sessions(days)`, `record_benchmark`, `get_benchmarks`, `list_exercises`, `get_exercise(name)`, `set_exercise_url(name, url)` (to replace a link with a video the user prefers). Dates are ISO strings; invalid input returns an error string, never an exception. No list-typed parameters (one call per day).
3. **Shared tables**: `planned_sessions(date PK, kind, description)`, `session_logs` (with optional structured fields `distance_m`, `duration_min`, `efforts`, `effort_s`, `rest_s`, filled by the coach when the user reports numbers, e.g. "HIIT 12 x 45 s / 45 s" → efforts 12, effort_s 45, rest_s 45; the free-text `done` is kept too), `benchmarks`, `exercises(name, aliases, explanation, url)`, `messages` (chat history per channel), `kv`. `kind` ∈ `rowing|hiit|walk|strength|mobility|rest`; the nutritionist reads `kind` to find exercise days.
4. **Chat history**: only visible user/assistant text is stored and replayed (last 24 messages); facts that matter live in tables, not in the chat. Replay must start with a user message.
5. **Prompt caching**: the stable prompt is the first system block with `cache_control`; the date goes in a second block so it cannot invalidate the cache.
6. **Check-in**: a daily cron at `CHECKIN_HOUR` (default 18) runs but only acts if the last check-in is at least 2 calendar days old, state in `kv`. The trigger text starts with `[check-in]`. `!checkin` forces one.
7. **Concurrency**: one run at a time per agent (lock), so a check-in and a user message cannot interleave.
8. **Errors**: Anthropic API errors become a short French message in the channel; the bot keeps running. Replies over 1900 characters are split on line breaks.
9b. **Exercise links are added by code, not by the model.** Catalog rows have an English `key` (e.g. `reverse_lunges`); the display name, aliases and explanation are French because they are shown to the user, plus English aliases so linking still works if an English name slips in. A catalog of ~12 exercises (rameur sprint, squats, squat jumps, fentes arrière, montées de genoux, skaters, mountain climbers, burpees avec pas en arrière, pompes inclinées, pont fessier, bird-dog, gainage) ships with a 2–3 line French explanation each and, as the default link, a YouTube search URL (always valid, never an invented video). The bot rewrites known exercise names in the coach's replies as Discord masked links (longest name first, not inside code blocks, not already linked) and sends with link previews suppressed. The user can replace a link with a specific video via `set_exercise_url`. The user's catalog edits survive restarts (seeding only inserts missing rows).
9c. **Rower distance** is asked only after rower sessions (steady row, intervals), is optional, and is stored in the log (`distance_m`) so trends are queryable; the 4-weekly benchmark remains the reference measurement.
9d. **Progress charts** come from structured log fields and benchmarks, never from the model's memory. The code computes the statistics (benchmark distance and split change since the first one, HIIT effort:rest ratio first → latest, sessions done in the last 4 weeks); the coach receives those numbers and writes 2–3 encouraging sentences from them without inventing any. The automatic weekly chart is attached only if there is new data since the last one. With too little data, `!progress` says what is missing instead of drawing an empty chart.
9e. **One-off backfill of past HIIT sessions (decided by the user).** There are no benchmark results to import. The HIIT sessions of months 1 and 2 are reconstructed so the HIIT chart has a history from day one: effort and rest times as prescribed in `history/*.md` (month 1: 20/90, 25/75, 30/60, 40/50 seconds; month 2 week 1: 40/50; week 2: 45/45), and the number of efforts the user actually did (month 1: 9, 12, 15, 12, from the user's own feedback; month 2 weeks 1 and 2: 12, to be confirmed; 4 Nov and 11 Nov: 12 efforts at 45 s / 45 s, confirmed by the user, who repeated the week-2 session on both days after the illness). The next HIIT (Wed 2026-11-18) is the third session at 12 x 45 s / 45 s, so the coach should consider progressing it (higher RPE or a longer effort/rest ratio) when it plans the following week. RPE unknown (stored empty). Dates follow the history files (month 1: Mon 14 Sep → Sun 11 Oct 2026; month 2 from Mon 12 Oct). The user was ill from 26 Oct to 1 Nov (no sessions), did only part of the program in the week of 2–8 Nov, and was back on track from 9 Nov. So the HIIT sessions to backfill are the Wednesdays that actually happened; sessions that did not happen are not inserted, and partial or uncertain ones need the user's confirmation. Entries are marked in `notes` as reconstructed from the plan, the script is idempotent, and it only runs after the user confirms the dates. The first real benchmark is scheduled by the coach in the coming weeks (the 12 Oct test has no result).
9. **Profile**: `data/profile.json` read/written by tools; `profile.example.json` ships pre-filled with the weekly structure and a summary of months 1–2.

## Left to the builder

- The exact wording of the English system prompt (which tells the model to reply in French), following the content listed in the plan.
- Logging format; Dockerfile details; README wording.
- Tests for tool functions call them directly; the live Discord and API behavior is verified by hand.

## Open items

None blocking.

## Amendments made during implementation

- **Logging a session once.** Only visible chat text is replayed, so the coach cannot remember earlier tool results. `get_plan` and `get_recent_sessions` therefore return each log's `id`, and `update_session_log` changes only the fields it is given. The prompt tells the coach to read `get_plan` before `log_session` and to update an existing row instead of logging again. `log_session` still always inserts, because one day can hold several real sessions.
- **Check-ins.** `!checkin` also records `last_checkin`, so a manual check-in is not followed by the automatic one the same day.
- **Weekly chart.** "New data" means the chart data (count and last date per panel) differs from the fingerprint stored when the last chart was posted.
- **Errors.** Anthropic API errors give the French "Erreur côté API Claude" message; any other exception gives "Erreur interne" and is logged with its traceback.
- **Refusal fallbacks.** `run_agent` opts into server-side refusal fallbacks (`fallbacks="default"`) on the models that support them.
