"""The coach agent: tools over the shared database and the profile file.

Every tool returns a JSON string. Invalid input returns {"error": ...}; tools never
raise, so the model can correct itself and the database is never half-written."""

import json
from datetime import date, datetime, timedelta

from anthropic import beta_async_tool

from app import config, db, exercises
from app.runtime import Agent

KINDS = ("rowing", "hiit", "walk", "strength", "mobility", "rest")

LOG_FIELDS = "id, date, completed, done, rpe, notes, distance_m, duration_min, efforts, effort_s, rest_s"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error(message: str) -> str:
    return _json({"error": message})


class _Invalid(ValueError):
    pass


def _day(value: str, name: str = "day") -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise _Invalid(
            f"{name} must be an ISO date YYYY-MM-DD, got {value!r}"
        ) from None


def _optional_int(
    value: int, name: str, low: int = 0, high: int | None = None
) -> int | None:
    """0 means unknown and is stored as NULL."""
    if value == 0:
        return None
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise _Invalid(f"{name} must be {bound} (0 if unknown), got {value}")
    return value


def _log_row(row) -> dict:
    out = dict(row)
    out["completed"] = bool(out["completed"])
    return out


def _load_profile() -> dict | None:
    try:
        return json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


@beta_async_tool
async def get_profile() -> str:
    """Read the user's profile (age, height, weight, goal, equipment, limits, weekly structure, history)."""
    try:
        profile = _load_profile()
    except json.JSONDecodeError as exc:
        return _error(f"profile.json is not valid JSON: {exc}")
    if profile is None:
        return _error(
            f"profile file not found at {config.PROFILE_PATH}; ask the user to create it"
        )
    return _json(profile)


@beta_async_tool
async def update_profile(key: str, value: str) -> str:
    """Set one field of the user's profile.

    Args:
        key: Profile key in English, e.g. "weight_kg" or "injuries_or_limits".
        value: New value. Parsed as JSON when possible (numbers, lists, objects), otherwise stored as text.
    """
    if not key.strip():
        return _error("key must not be empty")
    try:
        profile = _load_profile() or {}
    except json.JSONDecodeError as exc:
        return _error(f"profile.json is not valid JSON, not overwriting it: {exc}")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    profile[key] = parsed
    config.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(config.PROFILE_PATH)
    return _json({"ok": True, "key": key, "value": parsed})


@beta_async_tool
async def save_planned_session(day: str, kind: str, description: str) -> str:
    """Save the planned session for one day, replacing any session already planned that day.

    Args:
        day: ISO date YYYY-MM-DD.
        kind: One of rowing, hiit, walk, strength, mobility, rest.
        description: The session in English, e.g. "Steady row 30 min, RPE 5".
    """
    try:
        day = _day(day)
    except _Invalid as exc:
        return _error(str(exc))
    if kind not in KINDS:
        return _error(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO planned_sessions (date, kind, description, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET kind = excluded.kind, description = excluded.description, "
            "created_at = excluded.created_at",
            (day, kind, description, db.now()),
        )
    return _json({"ok": True, "date": day, "kind": kind, "description": description})


@beta_async_tool
async def get_plan(start: str, end: str) -> str:
    """Planned sessions and logged sessions between two dates, both included.

    Args:
        start: First ISO date YYYY-MM-DD.
        end: Last ISO date YYYY-MM-DD.
    """
    try:
        start, end = _day(start, "start"), _day(end, "end")
    except _Invalid as exc:
        return _error(str(exc))
    with db.connect_ro() as conn:
        planned = conn.execute(
            "SELECT date, kind, description FROM planned_sessions "
            "WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        ).fetchall()
        logged = conn.execute(
            f"SELECT {LOG_FIELDS} FROM session_logs WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start, end),
        ).fetchall()
    return _json(
        {"planned": [dict(r) for r in planned], "logged": [_log_row(r) for r in logged]}
    )


@beta_async_tool
async def log_session(
    day: str,
    completed: bool,
    done: str,
    rpe: int = 0,
    notes: str = "",
    distance_m: int = 0,
    duration_min: int = 0,
    efforts: int = 0,
    effort_s: int = 0,
    rest_s: int = 0,
) -> str:
    """Record what the user actually did on a day (call it before replying to their report).

    Args:
        day: ISO date YYYY-MM-DD of the session.
        completed: True if the session was done (even partly), False if skipped.
        done: What was actually done, in English, e.g. "HIIT 12 x 45 s / 45 s".
        rpe: Perceived effort 1-10, 0 if unknown.
        notes: Feelings, pain, sleep, anything useful, in English.
        distance_m: Rower distance in meters, 0 if unknown or not a rower session.
        duration_min: Duration in minutes, 0 if unknown.
        efforts: HIIT number of efforts, 0 if not HIIT.
        effort_s: HIIT effort time in seconds, 0 if not HIIT.
        rest_s: HIIT rest time in seconds, 0 if not HIIT.
    """
    try:
        day = _day(day)
        values = (
            _optional_int(rpe, "rpe", 1, 10),
            notes or None,
            _optional_int(distance_m, "distance_m", 1),
            _optional_int(duration_min, "duration_min", 1),
            _optional_int(efforts, "efforts", 1),
            _optional_int(effort_s, "effort_s", 1),
            _optional_int(rest_s, "rest_s", 1),
        )
    except _Invalid as exc:
        return _error(str(exc))
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO session_logs (date, completed, done, rpe, notes, distance_m, duration_min, "
            "efforts, effort_s, rest_s, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, int(bool(completed)), done, *values, db.now()),
        )
    return _json({"ok": True, "id": cur.lastrowid, "date": day})


@beta_async_tool
async def update_session_log(
    id: int,
    done: str = "",
    rpe: int = 0,
    notes: str = "",
    distance_m: int = 0,
    duration_min: int = 0,
    efforts: int = 0,
    effort_s: int = 0,
    rest_s: int = 0,
) -> str:
    """Add details to a session that is already logged, instead of logging it a second time.

    Only the fields you give are changed; a field left at its default (empty text or 0) keeps
    its stored value. `done` and `notes` replace the stored text, so repeat what must be kept.

    Args:
        id: The id of the log row, as returned by get_plan, get_recent_sessions or log_session.
        done: New text of what was done, in English. Empty keeps the stored one.
        rpe: Perceived effort 1-10. 0 keeps the stored one.
        notes: Feelings, pain, sleep, in English. Empty keeps the stored one.
        distance_m: Rower distance in meters. 0 keeps the stored one.
        duration_min: Duration in minutes. 0 keeps the stored one.
        efforts: HIIT number of efforts. 0 keeps the stored one.
        effort_s: HIIT effort time in seconds. 0 keeps the stored one.
        rest_s: HIIT rest time in seconds. 0 keeps the stored one.
    """
    try:
        changes = {
            "done": done or None,
            "rpe": _optional_int(rpe, "rpe", 1, 10),
            "notes": notes or None,
            "distance_m": _optional_int(distance_m, "distance_m", 1),
            "duration_min": _optional_int(duration_min, "duration_min", 1),
            "efforts": _optional_int(efforts, "efforts", 1),
            "effort_s": _optional_int(effort_s, "effort_s", 1),
            "rest_s": _optional_int(rest_s, "rest_s", 1),
        }
    except _Invalid as exc:
        return _error(str(exc))
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        return _error("nothing to update: give at least one field to change")
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE session_logs SET {assignments} WHERE id = ?",
            (*changes.values(), id),
        )
        if cur.rowcount == 0:
            return _error(f"no session log with id {id}")
        row = conn.execute(
            f"SELECT {LOG_FIELDS} FROM session_logs WHERE id = ?", (id,)
        ).fetchone()
    return _json({"ok": True, "log": _log_row(row)})


@beta_async_tool
async def get_recent_sessions(days: int = 14) -> str:
    """Sessions logged in the last `days` days (today included), oldest first.

    Args:
        days: How many days to look back.
    """
    if days < 0:
        return _error(f"days must be positive, got {days}")
    since = (datetime.now(config.TZ).date() - timedelta(days=days)).isoformat()
    with db.connect_ro() as conn:
        rows = conn.execute(
            f"SELECT {LOG_FIELDS} FROM session_logs WHERE date >= ? ORDER BY date, id",
            (since,),
        ).fetchall()
    return _json([_log_row(r) for r in rows])


@beta_async_tool
async def record_benchmark(
    day: str, distance_m: int, avg_split_s: float = 0.0, notes: str = ""
) -> str:
    """Record a rowing benchmark (20 min steady row at RPE 5).

    Args:
        day: ISO date YYYY-MM-DD.
        distance_m: Total distance in meters.
        avg_split_s: Average split per 500 m in seconds, 0 if unknown.
        notes: Anything useful, in English.
    """
    try:
        day = _day(day)
    except _Invalid as exc:
        return _error(str(exc))
    if distance_m <= 0:
        return _error(f"distance_m must be positive, got {distance_m}")
    if avg_split_s < 0:
        return _error(f"avg_split_s must be positive (0 if unknown), got {avg_split_s}")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO benchmarks (date, distance_m, avg_split_s, notes, created_at) VALUES (?, ?, ?, ?, ?)",
            (day, distance_m, avg_split_s or None, notes or None, db.now()),
        )
    return _json({"ok": True, "date": day, "distance_m": distance_m})


@beta_async_tool
async def get_benchmarks() -> str:
    """All rowing benchmarks, oldest first."""
    with db.connect_ro() as conn:
        rows = conn.execute(
            "SELECT date, distance_m, avg_split_s, notes FROM benchmarks ORDER BY date, id"
        ).fetchall()
    return _json([dict(r) for r in rows])


@beta_async_tool
async def list_exercises() -> str:
    """The exercise catalog: French names and a one-line explanation each. Use these names in replies."""
    return _json(
        [
            {"name": e["name"], "explanation": e["explanation"].splitlines()[0]}
            for e in exercises.list_all()
        ]
    )


@beta_async_tool
async def get_exercise(name: str) -> str:
    """Full explanation and link of one exercise, to answer "what is ...?" questions.

    Args:
        name: Exercise name or alias, any case or accents.
    """
    found = exercises.get(name)
    if found is None:
        return _error("unknown exercise")
    return _json(
        {
            "name": found["name"],
            "explanation": found["explanation"],
            "url": found["url"],
        }
    )


@beta_async_tool
async def set_exercise_url(name: str, url: str) -> str:
    """Replace an exercise's link, e.g. with a video the user prefers.

    Args:
        name: Exercise name or alias.
        url: An http:// or https:// URL.
    """
    if exercises.get(name) is None:
        return _error("unknown exercise")
    if not exercises.set_url(name, url):
        return _error("url must be a valid http:// or https:// URL without spaces")
    return _json({"ok": True, "name": exercises.get(name)["name"], "url": url})


TOOLS: list = [
    get_profile,
    update_profile,
    save_planned_session,
    get_plan,
    log_session,
    update_session_log,
    get_recent_sessions,
    record_benchmark,
    get_benchmarks,
    list_exercises,
    get_exercise,
    set_exercise_url,
]

COACH = Agent(name="coach", prompt_file="coach.md", tools=TOOLS)
