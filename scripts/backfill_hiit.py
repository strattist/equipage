"""One-off backfill of past HIIT sessions reconstructed from the plan (spec 9e).

Usage: python -m scripts.backfill_hiit data/backfill_hiit.json
The JSON file is a list of {"day": "YYYY-MM-DD", "efforts": int, "effort_s": int, "rest_s": int}.
All entries are validated before anything is written; already backfilled days are skipped."""

import json
import sys
from datetime import date

from app import db

NOTES = "reconstructed from plan"
FIELDS = ("efforts", "effort_s", "rest_s")


def _validate(entry: dict, index: int) -> tuple[str, int, int, int]:
    if not isinstance(entry, dict):
        # ValueError, not TypeError: callers rely on one exception for every invalid entry.
        raise ValueError(f"entry {index}: expected an object, got {entry!r}")  # noqa: TRY004
    missing = [k for k in ("day", *FIELDS) if k not in entry]
    if missing:
        raise ValueError(f"entry {index}: missing {', '.join(missing)}")
    try:
        day = date.fromisoformat(entry["day"]).isoformat()
    except (TypeError, ValueError):
        raise ValueError(
            f"entry {index}: day must be YYYY-MM-DD, got {entry['day']!r}"
        ) from None
    numbers = []
    for key in FIELDS:
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"entry {index}: {key} must be a positive integer, got {value!r}"
            )
        numbers.append(value)
    return day, *numbers


def backfill(entries: list[dict]) -> int:
    """Insert the entries not yet backfilled; return how many rows were inserted."""
    rows = [_validate(e, i) for i, e in enumerate(entries)]
    inserted = 0
    with db.connect() as conn:
        for day, efforts, effort_s, rest_s in rows:
            exists = conn.execute(
                "SELECT 1 FROM session_logs WHERE date = ? AND notes = ?", (day, NOTES)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO session_logs (date, completed, done, rpe, notes, efforts, effort_s, rest_s, created_at)"
                " VALUES (?, 1, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    day,
                    f"HIIT {efforts} x ({effort_s}s / {rest_s}s)",
                    NOTES,
                    efforts,
                    effort_s,
                    rest_s,
                    db.now(),
                ),
            )
            inserted += 1
    return inserted


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit(
            "usage: python -m scripts.backfill_hiit data/backfill_hiit.json"
        )
    with open(argv[0], encoding="utf-8") as f:
        entries = json.load(f)
    db.init()
    print(f"Inserted rows: {backfill(entries)}")


if __name__ == "__main__":
    main(sys.argv[1:])
