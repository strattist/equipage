"""The Confirmer button's logic, free of Discord so it can be tested.

Rules: the button is removed *before* the commit starts, so a double click cannot commit
twice; only the latest table (and only while its draft version is the latest) can commit;
a failed commit restores the button so the same table can be pressed again. The user-facing
texts are French because they are shown in Discord. Nothing here raises to the caller."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from app.mealie import MealieAuthError, MealieError
from app.meals.commit import CommitResult, EmptyDraft, StaleDraft

log = logging.getLogger("agents.confirm")

STALE = "Ce tableau n'est plus à jour, utilise le dernier."
EMPTY = "Il n'y a rien à confirmer pour l'instant."
AUTH = "Mealie a refusé le jeton d'API."
UNAVAILABLE = "Mealie est injoignable pour l'instant, réessaie dans un moment."
UNEXPECTED = "Une erreur inattendue est survenue, réessaie dans un moment."


class TableMessage(Protocol):
    async def show_button(self, visible: bool) -> None: ...

    async def set_text(self, text: str) -> None: ...


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {plural if n > 1 else singular}"


def success_text(result: CommitResult) -> str:
    return (
        "✅ Confirmé: "
        f"{_count(result.recipes_created, 'recette créée', 'recettes créées')}, "
        f"{_count(result.plan_entries, 'repas planifié', 'repas planifiés')}, "
        f"{_count(result.shopping_items, 'article ajouté', 'articles ajoutés')} dans Mealie."
    )


class ConfirmController:
    def __init__(
        self,
        commit: Callable[[int], Awaitable[CommitResult]],
        latest_version: Callable[[], int],
    ):
        self._commit = commit
        self._latest_version = latest_version
        self._current: TableMessage | None = None
        self._lock = asyncio.Lock()

    async def _show_button(self, message: TableMessage, visible: bool) -> None:
        try:
            await message.show_button(visible)
        except Exception:
            log.exception(
                "Could not %s the Confirmer button", "show" if visible else "hide"
            )

    async def register(self, version: int, message: TableMessage) -> None:
        """A new table was posted: its predecessor can no longer be confirmed."""
        previous, self._current = self._current, message
        if previous is not None and previous is not message:
            await self._show_button(previous, False)

    async def on_press(self, version: int, message: TableMessage) -> str:
        """Handle a press on `message`'s button; returns the French text for the user."""
        if self._lock.locked():  # a commit is already running (double click)
            await self._show_button(message, False)
            return STALE
        async with self._lock:
            if message is not self._current or version != self._latest_version():
                await self._show_button(message, False)
                return STALE
            await self._show_button(
                message, False
            )  # before the commit: no second press can start one
            try:
                result = await self._commit(version)
            except StaleDraft:
                self._current = None
                return STALE
            except EmptyDraft:
                return await self._failed(message, EMPTY)
            except MealieAuthError:
                return await self._failed(message, AUTH)
            except MealieError:
                return await self._failed(message, UNAVAILABLE)
            except Exception:
                log.exception("Unexpected error while committing the draft")
                return await self._failed(message, UNEXPECTED)
            self._current = None
            text = success_text(result)
            try:
                await message.set_text(text)
            except Exception:
                log.exception(
                    "Could not update the table message after a successful commit"
                )
            return text

    async def _failed(self, message: TableMessage, text: str) -> str:
        await self._show_button(
            message, True
        )  # the draft is intact: the same table stays confirmable
        return text
