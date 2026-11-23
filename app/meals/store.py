"""Fridge, away days and the meal draft (SQLite), plus the dinner table shown in Discord.

The draft is what the user confirms. Every change bumps its `version`, so a button
pressed on an older table can never commit a draft that has changed since."""

from dataclasses import dataclass, field
from datetime import date, timedelta

from app import db
from app.meals.rules import DayTarget

SOURCES = ("existing", "new")


class StaleDraft(Exception):
    """The draft changed since the table the user is acting on was shown."""


LABELS_FR = {
    "rowing": "rameur",
    "hiit": "HIIT",
    "walk": "marche",
    "strength": "renfo",
    "mobility": "mobilité",
    "rest": "repos",
    "protein": "protéines",
    "veggie": "végé",
}
DAYS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

MAX_LINE = 100
HEADERS = ("Date", "Entraînement", "Dîner", "Recette", "Déj. lendemain")


# --- fridge -----------------------------------------------------------------


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def fridge_list() -> list[dict]:
    with db.connect_ro() as conn:
        rows = conn.execute("SELECT name, quantity FROM fridge_items").fetchall()
    items = [{"name": r["name"], "quantity": r["quantity"] or ""} for r in rows]
    return sorted(items, key=lambda i: i["name"].casefold())


def fridge_add(name: str, quantity: str = "") -> None:
    name = " ".join(name.split())
    if not name:
        raise ValueError("fridge item name must not be empty")
    fridge_remove(name)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO fridge_items (name, quantity, updated_at) VALUES (?, ?, ?)",
            (name, " ".join(quantity.split()), db.now()),
        )


def fridge_remove(name: str) -> None:
    with db.connect() as conn:
        for row in conn.execute("SELECT name FROM fridge_items").fetchall():
            if _same(row["name"], name):
                conn.execute("DELETE FROM fridge_items WHERE name = ?", (row["name"],))


def fridge_replace(items: list[tuple[str, str]]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM fridge_items")
    for name, quantity in items:
        fridge_add(name, quantity)


# --- away days --------------------------------------------------------------


def away_set(days: list[date]) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM away_days")
        conn.executemany(
            "INSERT OR IGNORE INTO away_days (day) VALUES (?)",
            [(d.isoformat(),) for d in days],
        )


def away_get(start: date, end: date) -> set[date]:
    with db.connect_ro() as conn:
        rows = conn.execute(
            "SELECT day FROM away_days WHERE day BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return {date.fromisoformat(r["day"]) for r in rows}


# --- draft ------------------------------------------------------------------


@dataclass
class DraftDinner:
    day: date
    source: str
    name: str
    dinner_type: str | None
    recipe_slug: str | None
    ingredients: list[str]
    steps: list[str]
    recipe_created: bool
    plan_written: bool


@dataclass
class Draft:
    version: int
    new_recipe_count: int
    dinners: dict[date, DraftDinner] = field(default_factory=dict)
    shopping: list[str] = field(default_factory=list)
    shopping_written: bool = False


def _lines(items: list[str] | None) -> list[str]:
    """One line per item: inner newlines would corrupt the newline-joined storage."""
    return [clean for item in items or [] if (clean := " ".join(item.split()))]


def _bump(conn) -> int:
    conn.execute("UPDATE draft_meta SET version = version + 1 WHERE id = 1")
    return conn.execute("SELECT version FROM draft_meta WHERE id = 1").fetchone()[
        "version"
    ]


def draft_get() -> Draft:
    with db.connect_ro() as conn:
        meta = conn.execute("SELECT * FROM draft_meta WHERE id = 1").fetchone()
        dinners = conn.execute("SELECT * FROM draft_dinners ORDER BY day").fetchall()
        shopping = conn.execute(
            "SELECT item FROM draft_shopping ORDER BY rowid"
        ).fetchall()
    return Draft(
        version=meta["version"],
        new_recipe_count=meta["new_recipe_count"],
        dinners={
            date.fromisoformat(r["day"]): DraftDinner(
                day=date.fromisoformat(r["day"]),
                source=r["source"],
                name=r["name"],
                dinner_type=r["dinner_type"],
                recipe_slug=r["recipe_slug"],
                ingredients=[x for x in (r["ingredients"] or "").split("\n") if x],
                steps=[x for x in (r["steps"] or "").split("\n") if x],
                recipe_created=bool(r["recipe_created"]),
                plan_written=bool(r["plan_written"]),
            )
            for r in dinners
        },
        shopping=[r["item"] for r in shopping],
        shopping_written=bool(meta["shopping_written"]),
    )


def draft_set_dinner(
    day: date,
    source: str,
    name: str,
    dinner_type: str | None = None,
    recipe_slug: str | None = None,
    ingredients: list[str] | None = None,
    steps: list[str] | None = None,
) -> int:
    """Set or replace one day's dinner; returns the new draft version."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}, got {source!r}")
    name = " ".join(name.split())
    if not name:
        raise ValueError("dinner name must not be empty")
    if source == "existing" and not (recipe_slug or "").strip():
        raise ValueError("an existing recipe needs its Mealie slug")
    if source == "new" and not (_lines(ingredients) and _lines(steps)):
        raise ValueError("a new recipe needs ingredients and steps")
    with db.connect() as conn:
        # INSERT OR REPLACE resets recipe_created / plan_written for this day.
        conn.execute(
            "INSERT OR REPLACE INTO draft_dinners "
            "(day, source, name, dinner_type, recipe_slug, ingredients, steps) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                day.isoformat(),
                source,
                name,
                dinner_type,
                recipe_slug or None,
                "\n".join(_lines(ingredients)),
                "\n".join(_lines(steps)),
            ),
        )
        conn.execute("UPDATE draft_meta SET shopping_written = 0 WHERE id = 1")
        return _bump(conn)


def draft_set_shopping(items: list[str]) -> int:
    with db.connect() as conn:
        conn.execute("DELETE FROM draft_shopping")
        conn.executemany(
            "INSERT OR IGNORE INTO draft_shopping (item) VALUES (?)",
            [(i,) for i in _lines(items)],
        )
        conn.execute("UPDATE draft_meta SET shopping_written = 0 WHERE id = 1")
        return _bump(conn)


def draft_set_new_recipe_count(n: int) -> int:
    if n < 0:
        raise ValueError(f"new recipe count must not be negative, got {n}")
    with db.connect() as conn:
        conn.execute("UPDATE draft_meta SET new_recipe_count = ? WHERE id = 1", (n,))
        return _bump(conn)


def draft_clear() -> int:
    """Empty the dinners and the shopping list. The requested new-recipe count is kept."""
    with db.connect() as conn:
        conn.execute("DELETE FROM draft_dinners")
        conn.execute("DELETE FROM draft_shopping")
        conn.execute("UPDATE draft_meta SET shopping_written = 0 WHERE id = 1")
        return _bump(conn)


# Progress marks written by the commit. They never change `version` (the table the user is
# looking at must stay confirmable) and they only apply while the draft is still the one
# being committed: a draft edited mid-commit raises StaleDraft instead of being marked.


def _mark(statement: str, params: tuple, expected_version: int) -> None:
    with db.connect() as conn:
        cursor = conn.execute(
            f"{statement} AND (SELECT version FROM draft_meta WHERE id = 1) = ?",
            (*params, expected_version),
        )
        if cursor.rowcount == 0:
            raise StaleDraft("the draft changed while it was being committed")


def mark_recipe_created(day: date, recipe_slug: str, expected_version: int) -> None:
    _mark(
        "UPDATE draft_dinners SET recipe_created = 1, recipe_slug = ? WHERE day = ?",
        (recipe_slug, day.isoformat()),
        expected_version,
    )


def mark_plan_written(day: date, expected_version: int) -> None:
    _mark(
        "UPDATE draft_dinners SET plan_written = 1 WHERE day = ?",
        (day.isoformat(),),
        expected_version,
    )


def mark_shopping_written(expected_version: int) -> None:
    _mark(
        "UPDATE draft_meta SET shopping_written = 1 WHERE id = 1", (), expected_version
    )


# --- table ------------------------------------------------------------------


def _recipe_cell(dinner: DraftDinner | None, target: DayTarget, width: int) -> str:
    if target.away:
        return "— absent(e) —"
    if dinner is None:
        return "—"
    suffix = "(NOUVELLE)" if dinner.source == "new" else "(existante)"
    cell = f"{dinner.name} {suffix}"
    if len(cell) <= width:
        return cell
    keep = max(1, width - len(suffix) - 2)
    return f"{dinner.name[:keep].rstrip()}… {suffix}"


def _next_lunch_cell(target: DayTarget) -> str:
    following = DAYS_FR[(target.day + timedelta(days=1)).weekday()]
    leftover = not target.away and not target.spare_portion
    text = f"{following}: {'reste' if leftover else 'vide'}"
    return f"{text} ⚠ portion en trop" if target.spare_portion else text


def render_table(draft: Draft, targets: list[DayTarget]) -> str:
    """The dinner table as a monospace code block (Discord does not render markdown tables).

    Columns are padded to the same width on every row; only the recipe column is
    truncated (with …, keeping its (NOUVELLE)/(existante) marker) so no line exceeds 100
    characters. The last column is free-length so it never shifts the others."""
    dates, trainings, dinners, lasts = [], [], [], []
    for t in targets:
        dates.append(f"{DAYS_FR[t.day.weekday()]} {t.day:%d/%m}")
        trainings.append(LABELS_FR.get(t.training, t.training))
        dinners.append(
            "—"
            if t.dinner_type is None
            else LABELS_FR.get(t.dinner_type, t.dinner_type)
        )
        lasts.append(_next_lunch_cell(t))

    w_date = max([len(HEADERS[0]), *map(len, dates)])
    w_train = max([len(HEADERS[1]), *map(len, trainings)])
    w_dinner = max([len(HEADERS[2]), *map(len, dinners)])
    w_last = max([len(HEADERS[4]), *map(len, lasts)])
    w_recipe = max(
        len(HEADERS[3]), MAX_LINE - (w_date + w_train + w_dinner + w_last + 4 * 2)
    )

    recipes = [_recipe_cell(draft.dinners.get(t.day), t, w_recipe) for t in targets]
    w_recipe = max([len(HEADERS[3]), *map(len, recipes)])

    def row(*cells: str) -> str:
        widths = (w_date, w_train, w_dinner, w_recipe)
        padded = [c.ljust(w) for c, w in zip(cells[:4], widths)]
        return "  ".join([*padded, cells[4]]).rstrip()

    lines = [row(*HEADERS)]
    lines += [row(*cells) for cells in zip(dates, trainings, dinners, recipes, lasts)]
    return "```\n" + "\n".join(lines) + "\n```"
