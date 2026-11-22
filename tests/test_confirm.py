import asyncio

import pytest

from app.mealie import MealieAuthError, MealieError, MealieUnavailable
from app.meals.commit import CommitResult, EmptyDraft, StaleDraft
from app.meals.confirm import ConfirmController


class FakeMessage:
    def __init__(self, log: list, name: str = "msg"):
        self.log, self.name = log, name
        self.button_visible = True
        self.texts: list[str] = []

    async def show_button(self, visible: bool) -> None:
        self.button_visible = visible
        self.log.append((self.name, "button", visible))

    async def set_text(self, text: str) -> None:
        self.texts.append(text)
        self.log.append((self.name, "text", text))


class Harness:
    """A controller wired to a scriptable commit and a movable 'latest version'."""

    def __init__(self, outcome=None, delay: float = 0.0):
        outcome = outcome or CommitResult(2, 4, 7)
        self.log: list = []
        self.latest = 1
        self.commits: list[int] = []
        self.outcomes = [outcome] if not isinstance(outcome, list) else list(outcome)
        self.delay = delay
        self.controller = ConfirmController(self._commit, lambda: self.latest)

    async def _commit(self, version: int) -> CommitResult:
        self.commits.append(version)
        self.log.append(("commit", version))
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def table(self, name: str = "msg") -> FakeMessage:
        message = FakeMessage(self.log, name)
        await self.controller.register(self.latest, message)
        return message


async def test_press_hides_button_before_commit():
    h = Harness()
    message = await h.table()
    await h.controller.on_press(1, message)
    assert h.log.index(("msg", "button", False)) < h.log.index(("commit", 1))
    assert message.button_visible is False


async def test_double_press_commits_once():
    h = Harness(delay=0.05)
    message = await h.table()
    first, second = await asyncio.gather(
        h.controller.on_press(1, message), h.controller.on_press(1, message)
    )
    assert h.commits == [1]
    assert sorted([first.startswith("✅"), second.startswith("✅")]) == [False, True]
    assert "plus à jour" in (second if first.startswith("✅") else first)


async def test_stale_version_does_not_commit():
    h = Harness()
    message = await h.table()
    h.latest = 2  # the draft changed after this table was shown
    text = await h.controller.on_press(1, message)
    assert text == "Ce tableau n'est plus à jour, utilise le dernier."
    assert h.commits == [] and message.button_visible is False


async def test_a_message_that_is_not_the_latest_table_cannot_commit():
    h = Harness()
    old = await h.table("old")
    new = await h.table("new")
    text = await h.controller.on_press(
        1, old
    )  # same version number, but a superseded message
    assert "plus à jour" in text and h.commits == []
    assert old.button_visible is False and new.button_visible is True


async def test_new_table_removes_old_button():
    h = Harness()
    old = await h.table("old")
    assert old.button_visible is True
    await h.table("new")
    assert ("old", "button", False) in h.log
    assert old.button_visible is False


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            MealieUnavailable("down"),
            "Mealie est injoignable pour l'instant, réessaie dans un moment.",
        ),
        (MealieAuthError("401"), "Mealie a refusé le jeton d'API."),
        (
            MealieError("422"),
            "Mealie est injoignable pour l'instant, réessaie dans un moment.",
        ),
        (EmptyDraft("nothing"), "Il n'y a rien à confirmer pour l'instant."),
        (
            RuntimeError("bug"),
            "Une erreur inattendue est survenue, réessaie dans un moment.",
        ),
    ],
)
async def test_failure_restores_button_and_keeps_draft_confirmable(error, expected):
    h = Harness(outcome=[error, CommitResult(1, 2, 3)])
    message = await h.table()
    text = await h.controller.on_press(1, message)
    assert text == expected
    assert message.button_visible is True
    assert message.texts == []  # the table stays as it is
    # the same table can be confirmed again, and this time it works
    again = await h.controller.on_press(1, message)
    assert again.startswith("✅") and h.commits == [1, 1]
    assert message.button_visible is False


async def test_stale_draft_error_from_the_commit_keeps_the_button_hidden():
    h = Harness(outcome=StaleDraft("changed"))
    message = await h.table()
    text = await h.controller.on_press(1, message)
    assert "plus à jour" in text
    assert message.button_visible is False and message.texts == []


async def test_success_text_contains_counts_and_replaces_the_status():
    h = Harness(
        outcome=CommitResult(recipes_created=2, plan_entries=11, shopping_items=14)
    )
    message = await h.table()
    text = await h.controller.on_press(1, message)
    assert (
        text
        == "✅ Confirmé: 2 recettes créées, 11 repas planifiés, 14 articles ajoutés dans Mealie."
    )
    assert message.texts == [text]


async def test_success_text_agrees_in_number():
    h = Harness(
        outcome=CommitResult(recipes_created=1, plan_entries=1, shopping_items=1)
    )
    message = await h.table()
    assert await h.controller.on_press(1, message) == (
        "✅ Confirmé: 1 recette créée, 1 repas planifié, 1 article ajouté dans Mealie."
    )


async def test_after_success_the_table_cannot_be_committed_again():
    h = Harness()
    message = await h.table()
    await h.controller.on_press(1, message)
    text = await h.controller.on_press(1, message)
    assert "plus à jour" in text and h.commits == [1]


async def test_a_failing_discord_call_never_raises_to_the_caller():
    h = Harness()
    message = await h.table()

    async def broken(visible):
        raise RuntimeError("discord down")

    message.show_button = broken
    text = await h.controller.on_press(
        1, message
    )  # the commit still goes through, no exception
    assert text.startswith("✅")
