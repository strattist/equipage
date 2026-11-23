"""Discord bot: routes each channel to its agent, sends replies in chunks with
exercise links and posts progress charts. The coach never writes first: the only
automatic jobs are the optional progress chart and the nutritionist's weekly run."""

import io
import json
import logging
import os
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta

import anthropic
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config, db, exercises, progress
from app.coach import COACH
from app.meals import store
from app.meals.commit import CommitResult, commit_draft
from app.meals.confirm import ConfirmController
from app.nutritionist import NUTRITIONIST, meal_targets, mealie_writer
from app.runtime import Agent, run_agent

log = logging.getLogger("agents.bot")

ALLOWED_USER_ID = int(config.require("ALLOWED_USER_ID"))
COACH_CHANNEL_ID = int(config.require("COACH_CHANNEL_ID"))

NUTRITION_ENV = ("NUTRITION_CHANNEL_ID", "MEALIE_URL")


def build_agents(env: Mapping[str, str]) -> dict[int, Agent]:
    """Channel id to agent. The nutritionist needs its channel and the Mealie settings."""
    agents: dict[int, Agent] = {int(env["COACH_CHANNEL_ID"]): COACH}
    missing = [name for name in NUTRITION_ENV if not env.get(name)]
    read, write = config.mealie_token("read", env), config.mealie_token("write", env)
    if not read:
        missing.append("MEALIE_READ_TOKEN (or MEALIE_API_TOKEN)")
    if not write:
        missing.append("MEALIE_WRITE_TOKEN (or MEALIE_API_TOKEN)")
    if missing:
        log.warning("Nutritionist disabled, missing: %s", ", ".join(missing))
        return agents
    if read == write:
        log.warning(
            "One Mealie token does both reading and writing; create a read-only "
            "MEALIE_READ_TOKEN and a MEALIE_WRITE_TOKEN"
        )
    agents[int(env["NUTRITION_CHANNEL_ID"])] = NUTRITIONIST
    return agents


AGENTS: dict[int, Agent] = build_agents(os.environ)

CHUNK_SIZE = 1900
PROGRESS_EVERY_DAYS = 7
WEEKLY_EVERY_DAYS = 7

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
scheduler: AsyncIOScheduler | None = None


def _today() -> date:
    return datetime.now(config.TZ).date()


def chunks(text: str, size: int = CHUNK_SIZE) -> Iterator[str]:
    """Split on the last newline in the second half of the window, else hard-split.
    The newline used as a split point is dropped; empty parts are skipped."""
    while len(text) > size:
        cut = text.rfind("\n", size // 2, size)
        if cut == -1:
            part, text = text[:size], text[size:]
        else:
            part, text = text[:cut], text[cut + 1 :]
        if part.strip():
            yield part
    if text.strip():
        yield text


def _days_since(last: str | None, today: date) -> int | None:
    if last is None:
        return None
    try:
        return (today - date.fromisoformat(last)).days
    except ValueError:
        return None


def should_send_progress(
    last: str | None,
    today: date,
    has_new_data: bool,
    every_days: int = PROGRESS_EVERY_DAYS,
) -> bool:
    if not has_new_data:
        return False
    days = _days_since(last, today)
    return days is None or days >= every_days


async def _reply(agent: Agent, channel, text: str) -> str:
    try:
        return exercises.linkify(await run_agent(agent, str(channel.id), text))
    except anthropic.APIError as exc:
        log.warning("Anthropic API error for %s: %r", agent.name, exc)
        return (
            f"Erreur côté API Claude ({type(exc).__name__}). Réessaie dans un moment."
        )
    except Exception as exc:
        log.exception("Unexpected error while running %s", agent.name)
        return f"Erreur interne ({type(exc).__name__}). Réessaie dans un moment."


async def ask(channel, agent: Agent, text: str) -> None:
    async with channel.typing():
        reply = await _reply(agent, channel, text)
    for part in chunks(reply):
        await channel.send(part, suppress_embeds=True)
    if agent is NUTRITIONIST:
        try:
            await post_draft_table(channel)
        except Exception:
            log.exception("Could not post the dinner table")


def _fingerprint(data: progress.ProgressData) -> str | None:
    """Identifies the chart's data, to know whether anything new was logged."""
    series = (data.benchmarks, data.hiit, data.rows)
    if not any(series):
        return None
    return json.dumps([[len(s), s[-1][0].isoformat() if s else None] for s in series])


def _progress_trigger(summary: dict) -> str:
    return (
        "[progress] The system is posting the user's progress chart. Here are the computed numbers "
        f"(JSON): {json.dumps(summary, ensure_ascii=False)}\n"
        "Write 2-3 short encouraging sentences in French for the user, using only these numbers. "
        "Do not invent any other figure. Do not call tools."
    )


async def send_progress(channel, force: bool) -> None:
    data = progress.load_progress()
    png = progress.render_png(data)
    if png is None:
        if force:
            missing = ", ".join(progress.missing_panels(data))
            await channel.send(
                "Pas encore assez de données pour un graphique: il me faut au moins 2 mesures "
                f"({missing})."
            )
        return
    db.kv_set("last_progress", _today().isoformat())
    db.kv_set("progress_data", _fingerprint(data) or "")
    async with channel.typing():
        reply = await _reply(
            COACH, channel, _progress_trigger(progress.summarize(data))
        )
    parts = list(chunks(reply)) or [""]
    for part in parts[:-1]:
        await channel.send(part, suppress_embeds=True)
    await channel.send(
        parts[-1] or None,
        file=discord.File(io.BytesIO(png), filename="progres.png"),
        suppress_embeds=True,
    )


async def progress_job() -> None:
    """Optional automatic chart (PROGRESS_AUTO_DAYS > 0): only when there is new data."""
    channel = client.get_channel(COACH_CHANNEL_ID)
    if channel is None:
        log.error(
            "Coach channel %s not found; check COACH_CHANNEL_ID", COACH_CHANNEL_ID
        )
        return
    try:
        today = _today()
        fingerprint = _fingerprint(progress.load_progress())
        has_new = fingerprint is not None and fingerprint != db.kv_get("progress_data")
        if should_send_progress(
            db.kv_get("last_progress"),
            today,
            has_new,
            every_days=config.PROGRESS_AUTO_DAYS,
        ):
            await send_progress(channel, force=False)
    except Exception:
        log.exception("Automatic progress chart failed")


# --- nutritionist: weekly run and the Confirmer button ---------------------------------


def weekly_trigger_text() -> str:
    return (
        "[weekly] Automatic Sunday run. Ask the user, in one short message: which days next week "
        "they are away, what changed in the fridge, and how many new recipes they want. "
        "Do not plan yet."
    )


def should_run_weekly(last: str | None, today: date) -> bool:
    days = _days_since(last, today)
    return days is None or days >= WEEKLY_EVERY_DAYS


def nutrition_channel_id() -> int | None:
    return next((cid for cid, a in AGENTS.items() if a is NUTRITIONIST), None)


async def nutrition_weekly_job() -> None:
    channel_id = nutrition_channel_id()
    channel = client.get_channel(channel_id) if channel_id else None
    if channel is None:
        log.error("Nutrition channel %s not found; check NUTRITION_CHANNEL_ID", channel_id)
        return
    today = _today()
    if not should_run_weekly(db.kv_get("last_weekly"), today):
        return
    db.kv_set("last_weekly", today.isoformat())  # before asking: a crash must not repeat it
    try:
        await ask(channel, NUTRITIONIST, weekly_trigger_text())
    except Exception:
        log.exception("Weekly nutrition run failed")


async def commit_current_draft(version: int) -> CommitResult:
    async with mealie_writer() as mealie:
        return await commit_draft(version, mealie)


controller = ConfirmController(
    commit=lambda version: commit_current_draft(version),
    latest_version=lambda: store.draft_get().version,
)


class ConfirmView(discord.ui.View):
    """One Confirmer button, usable only by the allowed user."""

    def __init__(self, table: "DiscordTableMessage | None" = None):
        super().__init__(timeout=None)
        self.table = table
        button = discord.ui.Button(label="Confirmer", style=discord.ButtonStyle.success)
        button.callback = self._pressed
        self.add_item(button)

    async def _pressed(self, interaction) -> None:
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message(
                "Ce bouton n'est pas pour toi.", ephemeral=True
            )
            return
        if self.table is None:
            return
        await interaction.response.defer()
        table = self.table
        table.outcome_shown = False
        text = await controller.on_press(table.version, table)
        if not table.outcome_shown:  # failure or stale: tell the user where they are
            await interaction.followup.send(text, suppress_embeds=True)


class DiscordTableMessage:
    """A posted dinner table. The controller compares tables by identity, so one object
    is shared by the view, the controller and every later edit."""

    def __init__(self, message, text: str, version: int):
        self.message, self.text, self.version = message, text, version
        self.outcome_shown = False

    async def show_button(self, visible: bool) -> None:
        view = ConfirmView(self) if visible else None
        await self.message.edit(content=self.text, view=view)

    async def set_text(self, text: str) -> None:
        combined = f"{self.text}\n{text}"
        await self.message.edit(
            content=combined if len(combined) <= 2000 else text, view=None
        )
        self.outcome_shown = True


def _draft_week(draft: store.Draft) -> tuple[date, date]:
    first = min(draft.dinners)
    monday = first - timedelta(days=first.weekday())
    return monday, monday + timedelta(days=6)


async def post_draft_table(channel) -> None:
    """Post the dinner table with its button if the draft changed since the last table."""
    draft = store.draft_get()
    if not draft.dinners or str(draft.version) == db.kv_get("last_table_version"):
        return
    first, last = _draft_week(draft)
    text = store.render_table(draft, meal_targets(first, last))
    view = ConfirmView()
    sent = await channel.send(text, view=view, suppress_embeds=True)
    table = DiscordTableMessage(sent, text, draft.version)
    view.table = table
    db.kv_set("last_table_version", str(draft.version))
    await controller.register(draft.version, table)


@client.event
async def on_message(message) -> None:
    if message.author.bot or message.author.id != ALLOWED_USER_ID:
        return
    agent = AGENTS.get(message.channel.id)
    if agent is None:
        return
    content = message.content.strip()
    if content == "!progress" and agent is COACH:
        await send_progress(message.channel, force=True)
        return
    if content:
        await ask(message.channel, agent, content)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    if config.PROGRESS_AUTO_DAYS > 0:
        scheduler.add_job(
            progress_job,
            CronTrigger(hour=config.PROGRESS_HOUR, minute=0, timezone=config.TZ),
            id="progress",
            coalesce=True,
            misfire_grace_time=3600,
        )
    if nutrition_channel_id() is not None:
        scheduler.add_job(
            nutrition_weekly_job,
            CronTrigger(
                day_of_week="sun",
                hour=config.NUTRITION_WEEKLY_HOUR,
                minute=0,
                timezone=config.TZ,
            ),
            id="nutrition_weekly",
            coalesce=True,
            misfire_grace_time=3600,
        )


@client.event
async def on_ready() -> None:
    global scheduler
    log.info("Logged in as %s", client.user)
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone=config.TZ)
        register_jobs(scheduler)
        scheduler.start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    db.init()
    exercises.seed()
    client.run(config.require("DISCORD_TOKEN"), log_handler=None)


if __name__ == "__main__":
    main()
