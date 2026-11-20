import asyncio
from types import SimpleNamespace

import pytest

from app import config, db, runtime
from app.runtime import Agent


def msg(text=None, stop_reason="end_turn"):
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeRunner:
    def __init__(self, owner, kw):
        self.owner, self.kw = owner, kw

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        self.owner.active += 1
        self.owner.max_active = max(self.owner.max_active, self.owner.active)
        try:
            await asyncio.sleep(0.01)
            step = self.owner.script.pop(0)
            if isinstance(step, Exception):
                raise step
            # An intermediate tool-use turn first, then the final message.
            yield msg("thinking about tools", stop_reason="tool_use")
            yield step
        finally:
            self.owner.active -= 1


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(tool_runner=self._tool_runner)
        )

    def _tool_runner(self, **kw):
        self.calls.append(kw)
        return FakeRunner(self, kw)


@pytest.fixture
def agent(tmp_db, tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "test.md").write_text("You are a test agent.")
    monkeypatch.setattr(config, "PROMPTS_DIR", prompts)
    return Agent(name="test", prompt_file="test.md", tools=[])


def use(monkeypatch, fake):
    monkeypatch.setattr(runtime, "client", fake)
    return fake


async def test_returns_text_and_saves_both_turns(agent, monkeypatch):
    use(monkeypatch, FakeClient(msg("  Salut !  ")))
    assert await runtime.run_agent(agent, "c1", "Bonjour") == "Salut !"
    assert db.load_messages("c1", 10) == [
        {"role": "user", "content": "Bonjour"},
        {"role": "assistant", "content": "Salut !"},
    ]


async def test_system_has_cache_block_then_date_block(agent, monkeypatch):
    fake = use(monkeypatch, FakeClient(msg("ok")))
    await runtime.run_agent(agent, "c1", "hi")
    kw = fake.calls[0]
    system = kw["system"]
    assert system[0]["text"] == "You are a test agent."
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]
    assert system[1]["text"].startswith("Today:")
    assert kw["model"] == config.MODEL
    assert kw["max_tokens"] == 8000
    assert kw["tools"] == []
    for banned in ("temperature", "thinking", "budget_tokens"):
        assert banned not in kw


async def test_history_is_replayed_before_new_message(agent, monkeypatch):
    db.save_message("c1", "user", "earlier question")
    db.save_message("c1", "assistant", "earlier answer")
    fake = use(monkeypatch, FakeClient(msg("ok")))
    await runtime.run_agent(agent, "c1", "new question")
    assert fake.calls[0]["messages"] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "new question"},
    ]


async def test_empty_reply_uses_fallback_and_refusal_text(agent, monkeypatch):
    use(
        monkeypatch, FakeClient(msg(None), msg("   "), msg(None, stop_reason="refusal"))
    )
    assert (
        await runtime.run_agent(agent, "c1", "a")
        == "Je n'ai pas pu formuler de réponse, peux-tu reformuler ?"
    )
    assert (
        await runtime.run_agent(agent, "c1", "b")
        == "Je n'ai pas pu formuler de réponse, peux-tu reformuler ?"
    )
    assert await runtime.run_agent(agent, "c1", "c") == "Je ne peux pas répondre à ça."


async def test_exception_leaves_history_unchanged_and_releases_lock(agent, monkeypatch):
    use(monkeypatch, FakeClient(RuntimeError("api down"), msg("back")))
    with pytest.raises(RuntimeError):
        await runtime.run_agent(agent, "c1", "first")
    assert db.load_messages("c1", 10) == []
    reply = await asyncio.wait_for(runtime.run_agent(agent, "c1", "second"), timeout=1)
    assert reply == "back"
    assert db.load_messages("c1", 10) == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "back"},
    ]


async def test_runs_for_same_agent_are_serialized(agent, monkeypatch):
    fake = use(monkeypatch, FakeClient(msg("one"), msg("two")))
    replies = await asyncio.gather(
        runtime.run_agent(agent, "c1", "a"),
        runtime.run_agent(agent, "c1", "b"),
    )
    assert sorted(replies) == ["one", "two"]
    assert fake.max_active == 1


async def test_refusal_fallbacks_only_for_models_that_support_them(agent, monkeypatch):
    fake = use(monkeypatch, FakeClient(msg("a"), msg("b")))
    monkeypatch.setattr(config, "MODEL", "claude-opus-5-5")
    await runtime.run_agent(agent, "c1", "x")
    assert fake.calls[0]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in fake.calls[0]["betas"]

    monkeypatch.setattr(config, "MODEL", "claude-haiku-4-5")
    await runtime.run_agent(agent, "c1", "y")
    assert "fallbacks" not in fake.calls[1]
    assert "betas" not in fake.calls[1]
