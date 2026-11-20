"""Discord bot: routes each channel to its agent, sends replies in chunks with
exercise links, runs the check-in job and posts progress charts."""

import io
import json
import logging
from collections.abc import Iterator
from datetime import date, datetime

import anthropic
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config, db, exercises, progress
from app.coach import COACH
from app.runtime import Agent, run_agent

log = logging.getLogger("agents.bot")

ALLOWED_USER_ID = int(config.require("ALLOWED_USER_ID"))
COACH_CHANNEL_ID = int(config.require("COACH_CHANNEL_ID"))

AGENTS: dict[int, Agent] = {COACH_CHANNEL_ID: COACH}

CHUNK_SIZE = 1900
PROGRESS_EVERY_DAYS = 7
MANUAL_CHECKIN = "[check-in] Manual check-in requested."
AUTO_CHECKIN = "[check-in] Automatic end-of-day check-in."

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


def should_checkin(
    last: str | None, today: date, every_days: int = config.CHECKIN_EVERY_DAYS
) -> bool:
    days = _days_since(last, today)
    return days is None or days >= every_days


def should_send_progress(last: str | None, today: date, has_new_data: bool) -> bool:
    if not has_new_data:
        return False
    days = _days_since(last, today)
    return days is None or days >= PROGRESS_EVERY_DAYS


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


async def checkin_job() -> None:
    channel = client.get_channel(COACH_CHANNEL_ID)
    if channel is None:
        log.error(
            "Coach channel %s not found; check COACH_CHANNEL_ID", COACH_CHANNEL_ID
        )
        return
    today = _today()
    if should_checkin(db.kv_get("last_checkin"), today):
        db.kv_set(
            "last_checkin", today.isoformat()
        )  # before asking: a crash must not repeat it
        try:
            await ask(channel, COACH, AUTO_CHECKIN)
        except Exception:
            log.exception("Automatic check-in failed")
    try:
        fingerprint = _fingerprint(progress.load_progress())
        has_new = fingerprint is not None and fingerprint != db.kv_get("progress_data")
        if should_send_progress(db.kv_get("last_progress"), today, has_new):
            await send_progress(channel, force=False)
    except Exception:
        log.exception("Automatic progress chart failed")


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
    if content == "!checkin" and agent is COACH:
        db.kv_set("last_checkin", _today().isoformat())
        content = MANUAL_CHECKIN
    if content:
        await ask(message.channel, agent, content)


@client.event
async def on_ready() -> None:
    global scheduler
    log.info("Logged in as %s", client.user)
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone=config.TZ)
        scheduler.add_job(
            checkin_job,
            CronTrigger(hour=config.CHECKIN_HOUR, minute=0, timezone=config.TZ),
            coalesce=True,
            misfire_grace_time=3600,
        )
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
