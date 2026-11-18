# Equipage

Two Discord agents that run on a home NAS (TrueNAS) and help with sport and food:

- **Coach** (`#coach`): plans the training week, checks in every 2 days, adapts the load to what you report, and posts progress charts.
- **Nutritionist** (`#nutrition`): plans the week's dinners (and next-day leftover lunches) in [Mealie](https://mealie.io), builds the shopping list, and works from what is in the fridge.

Both agents talk to the user in French. Everything else (code, database, prompts, this README) is English. See the language policy in `docs/superpowers/specs/2026-11-17-coach-design.md`.

> **Status:** design and plans are written (see [Documentation](#documentation)); implementation follows the plans task by task. Sections below describe the target behavior and setup.

## How it works

One bot process routes each Discord channel to an `Agent` (name + prompt file + tools). A shared run loop replays the last 24 chat messages, runs the Anthropic tool runner, and saves the visible text. Durable facts (plan, logs, benchmarks, fridge, meal draft) live in SQLite and are read and written through tools, never from the model's memory.

```
Discord #coach ───────► Coach ────────┐
                                      ├─► run_agent ─► Anthropic API
Discord #nutrition ───► Nutritionist ─┘        │
                              │                ▼
                              │          SQLite (/data)
                              ▼
                       Mealie REST API
```

The nutritionist reads the coach's planned sessions from the shared database to know which days are exercise days.

### Coach

- Weekly structure (Mon steady row, Wed HIIT, Thu brisk walk, Fri rower intervals, Sat light strength, 2 full rest days), adapted from your feedback in both directions.
- Automatic check-in at `CHECKIN_HOUR`, only if the last one is at least 2 days old. `!checkin` forces one.
- Exercise names in replies become links to a short explanation or video.
- `!progress` (and automatically about once a week) posts a chart: rowing benchmark distance, HIIT effort vs rest, distance per rower session, with a short encouraging message based on the real numbers.
- Joint pain stops the session; no medical advice.

### Nutritionist

- Sundays at `NUTRITION_WEEKLY_HOUR`: asks which days you are away, fridge changes, and how many new recipes you want this week.
- Proposes a dinner table; you can ask for changes ("remplace jeudi par du poisson").
- **Nothing is written to Mealie until you press the Confirmer button** under the table. The button disappears once used, and only the latest table can be confirmed.
- Every recipe is for 2 portions (dinner + next day's lunch). Protein dinners fall on exercise days, veggie dinners on the others.

## Requirements

- Python 3.12 (3.12 for discord.py compatibility), [uv](https://docs.astral.sh/uv/) for the virtualenv, or Docker.
- An Anthropic API key.
- A Discord bot in your server, with two channels (`#coach`, `#nutrition`).
- A Mealie instance reachable from the bot's container (nutritionist only).

## Setup

### 1. Discord

1. In the [Developer Portal](https://discord.com/developers/applications), create an application and add a bot. Copy the bot token.
2. Under *Bot*, enable the **Message Content** intent.
3. Invite the bot with scope `bot` and permissions View Channels, Send Messages, Read Message History, Attach Files.
4. Enable Developer Mode in Discord (*Settings → Advanced*), then right-click to copy your user ID and each channel's ID.

### 2. Mealie (nutritionist)

Create an API token at `<MEALIE_URL>/user/profile/api-tokens`. The endpoints used are documented in `docs/mealie-endpoints.md` (taken from your instance's `/docs`).

### 3. Configuration

```bash
cp .env.example .env
cp data/profile.example.json data/profile.json
```

Fill in `.env`:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | | Anthropic API access |
| `DISCORD_TOKEN` | yes | | Bot token |
| `ALLOWED_USER_ID` | yes | | The only user the bot answers to (and who can press Confirmer) |
| `COACH_CHANNEL_ID` | yes | | `#coach` channel |
| `NUTRITION_CHANNEL_ID` | no | | `#nutrition` channel; the nutritionist is skipped with a warning if unset |
| `MEALIE_URL` | for nutritionist | | Base URL of your Mealie |
| `MEALIE_API_TOKEN` | for nutritionist | | Mealie API token |
| `CLAUDE_MODEL` | no | `claude-opus-5-5` | `claude-sonnet-5-5` is the cheaper option |
| `TZ` | no | `Europe/Paris` | Scheduler timezone |
| `CHECKIN_HOUR` | no | `18` | Hour of the daily check-in job |
| `NUTRITION_WEEKLY_HOUR` | no | `10` | Hour of the Sunday nutrition run |
| `DATA_DIR` | no | `./data` (`/data` in Docker) | SQLite database and profile |

Edit `data/profile.json` with your age, height and weight. `.env` and `data/` (except `profile.example.json`) are git-ignored.

## Run

### Locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m app.bot
```

### Docker / TrueNAS

```bash
docker compose up -d --build
```

The bot only makes outbound connections (Discord, Anthropic, Mealie). In `compose.yaml`, point the `./data:/data` volume at a TrueNAS dataset so the database survives updates. The container must be able to reach `MEALIE_URL`.

## Tests

```bash
.venv/bin/python -m pytest
```

Pure logic (meal rules, tools, Mealie client against a mocked server, commit, button logic) is covered. Live Discord and API behavior is verified by hand, following the last task of each plan.

## Documentation

| File | What it covers |
|---|---|
| `docs/superpowers/specs/2026-11-17-coach-design.md` | Coach and bot foundation: goals, decisions, language policy |
| `docs/superpowers/specs/2026-11-17-nutritionist-design.md` | Nutritionist: rules, confirmation flow, Mealie integration |
| `docs/superpowers/plans/2026-11-17-coach-agent.md` | Task-by-task plan for the foundation and the coach |
| `docs/superpowers/plans/2026-11-17-nutritionist-agent.md` | Task-by-task plan for the nutritionist (builds on the coach plan) |
| `history/month1.md`, `history/month2.md` | The coaching history that seeds the profile and the weekly structure |

To understand the code, start with `app/runtime.py` (`Agent`, `run_agent`), then `app/coach.py` and `app/nutritionist.py`, then `app/bot.py`.
