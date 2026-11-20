import importlib
import json
from datetime import date, datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app import config, db, exercises

USER_ID = 111
CHANNEL_ID = 222


@pytest.fixture
def bot(tmp_db, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_ID", str(USER_ID))
    monkeypatch.setenv("COACH_CHANNEL_ID", str(CHANNEL_ID))
    module = importlib.import_module("app.bot")
    exercises.seed()
    return module


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    def __init__(self, id=CHANNEL_ID):
        self.id = id
        self.sent = []

    def typing(self):
        return Typing()

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def fake_message(content, author_id=USER_ID, channel=None, is_bot=False):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=is_bot),
        channel=channel or FakeChannel(),
    )


def recorder(reply="ok"):
    calls = []

    async def run(agent, channel, text):
        calls.append((agent, channel, text))
        return reply

    run.calls = calls
    return run


def today():
    return datetime.now(config.TZ).date()


def test_chunks_short_text_single_chunk(bot):
    assert list(bot.chunks("Salut !")) == ["Salut !"]


@pytest.mark.parametrize(
    "text",
    [
        "\n".join(f"ligne {i} " + "x" * (i % 70) for i in range(150))[:4500],
        "y" * 4500,
    ],
)
def test_chunks_long_text_each_under_limit_and_no_content_lost(bot, text):
    assert len(text) >= 4500
    parts = list(bot.chunks(text))
    assert len(parts) >= 3
    assert all(0 < len(p) <= 1900 for p in parts)
    assert "".join(text.split()) == "".join("".join(parts).split())


def test_should_checkin_rules(bot):
    t = date(2026, 11, 20)
    assert bot.should_checkin(None, t) is True
    assert bot.should_checkin("2026-11-19", t) is False
    assert bot.should_checkin("2026-11-20", t) is False
    assert bot.should_checkin("2026-11-18", t) is True
    assert bot.should_checkin("2026-10-31", date(2026, 11, 2)) is True
    assert bot.should_checkin("garbage", t) is True


async def test_ask_sends_api_error_message(bot, monkeypatch):
    async def boom(*args):
        raise anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    monkeypatch.setattr(bot, "run_agent", boom)
    channel = FakeChannel()
    await bot.ask(channel, bot.COACH, "salut")
    assert len(channel.sent) == 1
    assert channel.sent[0]["content"].startswith(
        "Erreur côté API Claude (APIConnectionError)"
    )


async def test_ask_reports_unexpected_errors_in_french(bot, monkeypatch):
    async def boom(*args):
        raise KeyError("bug")

    monkeypatch.setattr(bot, "run_agent", boom)
    channel = FakeChannel()
    await bot.ask(channel, bot.COACH, "salut")
    assert len(channel.sent) == 1
    assert channel.sent[0]["content"].startswith("Erreur interne")


async def test_ask_links_exercise_names_and_suppresses_embeds(bot, monkeypatch):
    run = recorder("Mercredi : squats et skaters.")
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    await bot.ask(channel, bot.COACH, "plan ?")
    assert run.calls == [(bot.COACH, str(CHANNEL_ID), "plan ?")]
    [sent] = channel.sent
    assert f"[squats]({exercises.get('squats')['url']})" in sent["content"]
    assert sent["suppress_embeds"] is True


async def test_checkin_job_records_state_before_asking(bot, monkeypatch):
    async def boom(*args):
        raise RuntimeError("discord down")

    monkeypatch.setattr(bot, "ask", boom)
    monkeypatch.setattr(bot.client, "get_channel", lambda cid: FakeChannel(cid))
    await bot.checkin_job()
    assert db.kv_get("last_checkin") == today().isoformat()


async def test_checkin_job_skips_when_recent(bot, monkeypatch):
    run = recorder()
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    monkeypatch.setattr(bot.client, "get_channel", lambda cid: channel)
    await bot.checkin_job()
    await bot.checkin_job()  # same day, e.g. after a restart
    texts = [c[2] for c in run.calls]
    assert texts == ["[check-in] Automatic end-of-day check-in."]


def test_should_send_progress_rules(bot):
    t = date(2026, 11, 20)
    assert bot.should_send_progress(None, t, has_new_data=False) is False
    assert bot.should_send_progress(None, t, has_new_data=True) is True
    assert bot.should_send_progress("2026-11-17", t, has_new_data=True) is False
    assert bot.should_send_progress("2026-11-13", t, has_new_data=True) is True
    assert bot.should_send_progress("2026-11-13", t, has_new_data=False) is False


async def test_progress_command_sends_png_and_encouragement(bot, monkeypatch):
    run = recorder("Bravo, +300 m !")
    monkeypatch.setattr(bot, "run_agent", run)
    monkeypatch.setattr(bot.progress, "render_png", lambda data: b"\x89PNG fake")
    channel = FakeChannel()
    await bot.on_message(fake_message("!progress", channel=channel))
    [(agent, _, trigger)] = run.calls
    assert agent is bot.COACH
    assert trigger.startswith("[progress]")
    assert json.dumps({"sessions_last_28_days": 0}) in trigger
    [sent] = channel.sent
    assert sent["content"] == "Bravo, +300 m !"
    assert sent["file"].filename == "progres.png"
    assert db.kv_get("last_progress") == today().isoformat()


async def test_progress_command_without_data_explains_what_is_missing(bot, monkeypatch):
    run = recorder()
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    await bot.on_message(fake_message("!progress", channel=channel))
    assert run.calls == []
    [sent] = channel.sent
    assert sent["content"].startswith("Pas encore assez de données pour un graphique")
    assert (
        "benchmark rameur" in sent["content"] and "HIIT effort/repos" in sent["content"]
    )


async def test_automatic_progress_only_with_new_data(bot, monkeypatch):
    run = recorder("Continue !")
    monkeypatch.setattr(bot, "run_agent", run)
    monkeypatch.setattr(bot.progress, "render_png", lambda data: b"\x89PNG fake")
    channel = FakeChannel()
    monkeypatch.setattr(bot.client, "get_channel", lambda cid: channel)
    db.kv_set("last_checkin", today().isoformat())  # no check-in today

    await bot.checkin_job()  # nothing logged yet: nothing new, no chart
    assert channel.sent == []

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO benchmarks (date, distance_m, created_at) VALUES ('2026-10-12', 4000, 'x')"
        )
    await bot.checkin_job()
    assert [s.get("file").filename for s in channel.sent] == ["progres.png"]

    db.kv_set("last_progress", "2000-01-01")  # a week has passed, but no new data
    await bot.checkin_job()
    assert len(channel.sent) == 1


async def test_manual_checkin_sends_trigger_and_records_state(bot, monkeypatch):
    run = recorder()
    monkeypatch.setattr(bot, "run_agent", run)
    await bot.on_message(fake_message("!checkin"))
    assert run.calls[0][2] == "[check-in] Manual check-in requested."
    assert db.kv_get("last_checkin") == today().isoformat()


async def test_other_users_are_ignored(bot, monkeypatch):
    run = recorder()
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    await bot.on_message(fake_message("salut", author_id=999, channel=channel))
    await bot.on_message(fake_message("salut", is_bot=True, channel=channel))
    await bot.on_message(fake_message("salut", channel=FakeChannel(id=333)))
    assert run.calls == [] and channel.sent == []
    await bot.on_message(fake_message("salut", channel=channel))
    assert run.calls == [(bot.COACH, str(CHANNEL_ID), "salut")]
