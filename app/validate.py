"""Validation of the text a model (or the fridge box) may put into the database and,
later, into Mealie: bounded, one line per item, no links and no Discord mentions."""

import re

FRIDGE_MAX_CHARS = 600
FRIDGE_MAX_ITEMS = 40

_FORBIDDEN = re.compile(
    r"https?://|www\.|\[[^\]]*\]\([^)]*\)|<[@#&!]|@everyone|@here", re.IGNORECASE
)
_FRIDGE_FIELD = re.compile(r"^[\w '’.,%/()+-]{1,40}$")


def check_text(value: str, field: str, max_len: int) -> str:
    """The stripped value, or ValueError if it is empty, too long, a link or a mention."""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > max_len:
        raise ValueError(f"{field} must not exceed {max_len} characters")
    if _FORBIDDEN.search(text):
        raise ValueError(f"{field} must not contain links or mentions")
    return text


def check_lines(
    lines: list[str], field: str, max_lines: int, max_len: int
) -> list[str]:
    if len(lines) > max_lines:
        raise ValueError(f"{field} must not exceed {max_lines} lines")
    return [check_text(line, field, max_len) for line in lines]


def parse_fridge(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Fridge lines `name` or `name: quantity` as (accepted pairs, rejected lines).

    Rejected lines are cut to 40 characters so they can be echoed back safely. Raises
    ValueError when the text or the number of lines is over the limits."""
    text = text or ""
    if len(text) > FRIDGE_MAX_CHARS:
        raise ValueError(
            f"the fridge text must not exceed {FRIDGE_MAX_CHARS} characters"
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > FRIDGE_MAX_ITEMS:
        raise ValueError(f"the fridge must not exceed {FRIDGE_MAX_ITEMS} items")
    accepted: list[tuple[str, str]] = []
    rejected: list[str] = []
    for line in lines:
        name, _, quantity = line.partition(":")
        name, quantity = name.strip(), quantity.strip()
        if (
            _FORBIDDEN.search(line)
            or not _FRIDGE_FIELD.match(name)
            or (quantity and not _FRIDGE_FIELD.match(quantity))
        ):
            rejected.append(line[:40])
        else:
            accepted.append((name, quantity))
    return accepted, rejected
