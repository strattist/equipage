"""Settings read from the environment. Modules read these as `config.X` at call
time so tests can monkeypatch them."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR: Path = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH: Path = DATA_DIR / "agents.sqlite3"
PROFILE_PATH: Path = DATA_DIR / "profile.json"
PROMPTS_DIR: Path = ROOT / "prompts"

MODEL: str = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
TZ: ZoneInfo = ZoneInfo(os.environ.get("TZ") or "Europe/Paris")
CHECKIN_HOUR: int = int(os.environ.get("CHECKIN_HOUR") or 18)
NUTRITION_WEEKLY_HOUR: int = int(os.environ.get("NUTRITION_WEEKLY_HOUR") or 10)

CHECKIN_EVERY_DAYS = 2
HISTORY_LIMIT = 24


def require(name: str) -> str:
    """Return the environment variable `name`, or exit naming it."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value
