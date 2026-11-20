"""Shared run loop: an Agent is data (name, prompt file, tools); run_agent replays
recent chat text, runs the Anthropic tool runner, and saves the visible text."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import anthropic

from app import config, db

EMPTY_REPLY = "Je n'ai pas pu formuler de réponse, peux-tu reformuler ?"
REFUSAL_REPLY = "Je ne peux pas répondre à ça."
MAX_TOKENS = 8000

# Models that accept server-side refusal fallbacks with the "default" routing.
FALLBACK_MODELS = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5",
    "claude-fable-5-1",
    "claude-fable-5-2",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


@dataclass(frozen=True)
class Agent:
    name: str
    prompt_file: str
    tools: list


client = anthropic.AsyncAnthropic()

_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _system(agent: Agent) -> list[dict]:
    prompt = (config.PROMPTS_DIR / agent.prompt_file).read_text(encoding="utf-8")
    today = datetime.now(config.TZ).date()
    return [
        {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}},
        {
            "type": "text",
            "text": f"Today: {WEEKDAYS[today.weekday()]} {today.isoformat()}.",
        },
    ]


def _reply_text(message) -> str:
    if message is None:
        return EMPTY_REPLY
    text = "".join(
        b.text for b in message.content if getattr(b, "type", None) == "text"
    ).strip()
    if text:
        return text
    return REFUSAL_REPLY if message.stop_reason == "refusal" else EMPTY_REPLY


async def run_agent(agent: Agent, channel: str, user_text: str) -> str:
    async with _locks[agent.name]:
        messages = db.load_messages(channel, config.HISTORY_LIMIT)
        messages.append({"role": "user", "content": user_text})
        kwargs: dict = {
            "model": config.MODEL,
            "max_tokens": MAX_TOKENS,
            "system": _system(agent),
            "tools": agent.tools,
            "messages": messages,
        }
        if config.MODEL in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        last = None
        async for message in client.beta.messages.tool_runner(**kwargs):
            last = message
        reply = _reply_text(last)

        db.save_message(channel, "user", user_text)
        db.save_message(channel, "assistant", reply)
        return reply
