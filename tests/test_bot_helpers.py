import importlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import config, db
from app.meals import store

USER_ID = 111
COACH_ID = 222
NUTRITION_ID = 333


@pytest.fixture
def bot(tmp_db, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_ID", str(USER_ID))
    monkeypatch.setenv("COACH_CHANNEL_ID", str(COACH_ID))
    return importlib.import_module("app.bot")


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSent:
    def __init__(self, content, view):
        self.content, self.view, self.edits = content, view, []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, id=NUTRITION_ID):
        self.id = id
        self.sent = []

    def typing(self):
        return Typing()

    async def send(self, content=None, view=None, **kwargs):
        message = FakeSent(content, view)
        self.sent.append(message)
        return message


def recorder(reply="ok"):
    calls = []

    async def run(agent, channel, text):
        calls.append((agent, channel, text))
        return reply

    run.calls = calls
    return run


FULL_ENV = {
    "COACH_CHANNEL_ID": str(COACH_ID),
    "NUTRITION_CHANNEL_ID": str(NUTRITION_ID),
    "MEALIE_URL": "http://mealie.local",
    "MEALIE_API_TOKEN": "token",
}


def test_weekly_trigger_text_starts_with_marker(bot):
    assert bot.weekly_trigger_text().startswith("[weekly]")


def test_should_run_weekly(bot):
    today = date(2026, 11, 29)
    assert bot.should_run_weekly(None, today) is True
    assert bot.should_run_weekly((today - timedelta(days=3)).isoformat(), today) is False
    assert bot.should_run_weekly((today - timedelta(days=7)).isoformat(), today) is True
    assert bot.should_run_weekly("garbage", today) is True


def test_agents_registry_skips_nutritionist_without_channel_id(bot, caplog):
    env = {k: v for k, v in FULL_ENV.items() if k != "NUTRITION_CHANNEL_ID"}
    assert bot.build_agents(env) == {COACH_ID: bot.COACH}
    assert "NUTRITION_CHANNEL_ID" in caplog.text


def test_agents_registry_skips_nutritionist_without_mealie_settings(bot):
    env = {k: v for k, v in FULL_ENV.items() if k != "MEALIE_URL"}
    assert bot.build_agents(env) == {COACH_ID: bot.COACH}


def test_agents_registry_has_both_agents(bot):
    agents = bot.build_agents(FULL_ENV)
    assert agents == {COACH_ID: bot.COACH, NUTRITION_ID: bot.NUTRITIONIST}


def test_weekly_hour_default(bot):
    assert config.NUTRITION_WEEKLY_HOUR == 10


async def test_post_draft_table_skips_empty_draft(bot):
    channel = FakeChannel()
    await bot.post_draft_table(channel)
    assert channel.sent == []


async def test_post_draft_table_posts_table_with_button_once_per_version(bot):
    store.draft_set_dinner(
        date(2026, 11, 24), "existing", "Curry", "veggie", recipe_slug="curry"
    )
    channel = FakeChannel()
    await bot.post_draft_table(channel)
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert "Curry" in sent.content and sent.content.startswith("```")
    assert sent.view is not None
    await bot.post_draft_table(channel)  # same version: nothing new to confirm
    assert len(channel.sent) == 1
    store.draft_set_dinner(
        date(2026, 11, 25), "existing", "Saumon", "protein", recipe_slug="saumon"
    )
    await bot.post_draft_table(channel)
    assert len(channel.sent) == 2
    assert channel.sent[0].edits[-1]["view"] is None  # old table lost its button


async def test_ask_posts_table_after_a_nutritionist_reply(bot, monkeypatch):
    store.draft_set_dinner(
        date(2026, 11, 24), "existing", "Curry", "veggie", recipe_slug="curry"
    )
    run = recorder("Voilà le plan.")
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    await bot.ask(channel, bot.NUTRITIONIST, "prépare la semaine")
    assert [m.content for m in channel.sent][0] == "Voilà le plan."
    assert channel.sent[1].content.startswith("```")


async def test_weekly_job_records_state_before_asking(bot, monkeypatch):
    run = recorder("Quels jours es-tu absent ?")
    monkeypatch.setattr(bot, "run_agent", run)
    channel = FakeChannel()
    monkeypatch.setattr(bot, "AGENTS", {NUTRITION_ID: bot.NUTRITIONIST})
    monkeypatch.setattr(bot.client, "get_channel", lambda cid: channel)
    await bot.nutrition_weekly_job()
    assert run.calls[0][0] is bot.NUTRITIONIST
    assert run.calls[0][2].startswith("[weekly]")
    assert db.kv_get("last_weekly") == bot._today().isoformat()
    await bot.nutrition_weekly_job()  # already ran today
    assert len(run.calls) == 1


class FakeInteraction:
    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.responses = []
        self.response = SimpleNamespace(
            send_message=self._send, defer=self._defer
        )
        self.followup = SimpleNamespace(send=self._followup)
        self.deferred = False

    async def _send(self, content, ephemeral=False):
        self.responses.append((content, ephemeral))

    async def _defer(self):
        self.deferred = True

    async def _followup(self, content, **kwargs):
        self.responses.append((content, kwargs.get("ephemeral", False)))


async def test_confirm_button_refuses_other_users(bot):
    store.draft_set_dinner(
        date(2026, 11, 24), "existing", "Curry", "veggie", recipe_slug="curry"
    )
    channel = FakeChannel()
    await bot.post_draft_table(channel)
    button = channel.sent[0].view.children[0]
    assert button.label == "Confirmer"
    interaction = FakeInteraction(user_id=999)
    await button.callback(interaction)
    assert interaction.responses and interaction.responses[0][1] is True
    assert not interaction.deferred


async def test_confirm_button_commits_for_allowed_user(bot, monkeypatch):
    store.draft_set_dinner(
        date(2026, 11, 24), "existing", "Curry", "veggie", recipe_slug="curry"
    )
    seen = []

    async def fake_commit(version):
        seen.append(version)
        from app.meals.commit import CommitResult

        return CommitResult(0, 2, 0)

    monkeypatch.setattr(bot, "commit_current_draft", fake_commit)
    channel = FakeChannel()
    await bot.post_draft_table(channel)
    sent = channel.sent[0]
    await sent.view.children[0].callback(FakeInteraction(user_id=USER_ID))
    assert seen == [store.draft_get().version]
    assert "Confirmé" in sent.edits[-1]["content"]
    assert sent.edits[-1]["view"] is None


SPLIT_ENV = {
    k: v for k, v in FULL_ENV.items() if k != "MEALIE_API_TOKEN"
} | {"MEALIE_READ_TOKEN": "read", "MEALIE_WRITE_TOKEN": "write"}


def test_agents_registry_accepts_split_tokens(bot, caplog):
    assert bot.build_agents(SPLIT_ENV) == {
        COACH_ID: bot.COACH,
        NUTRITION_ID: bot.NUTRITIONIST,
    }
    assert "MEALIE_READ_TOKEN" not in caplog.text


def test_agents_registry_skips_nutritionist_with_only_a_read_token(bot, caplog):
    env = {k: v for k, v in SPLIT_ENV.items() if k != "MEALIE_WRITE_TOKEN"}
    assert bot.build_agents(env) == {COACH_ID: bot.COACH}
    assert "MEALIE_WRITE_TOKEN" in caplog.text


def test_agents_registry_warns_when_one_token_does_both(bot, caplog):
    agents = bot.build_agents(FULL_ENV)
    assert agents == {COACH_ID: bot.COACH, NUTRITION_ID: bot.NUTRITIONIST}
    assert "MEALIE_READ_TOKEN" in caplog.text
