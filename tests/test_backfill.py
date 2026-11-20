import json
import subprocess
import sys

import pytest

from app import config, db, progress
from scripts.backfill_hiit import backfill

ENTRIES = [
    {"day": "2026-09-16", "efforts": 9, "effort_s": 20, "rest_s": 90},
    {"day": "2026-10-21", "efforts": 12, "effort_s": 45, "rest_s": 45},
]


def rows():
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT date, completed, done, rpe, notes, efforts, effort_s, rest_s FROM session_logs ORDER BY date"
            )
        ]


def test_backfill_inserts_structured_rows(tmp_db):
    assert backfill(ENTRIES) == 2
    assert rows() == [
        {
            "date": "2026-09-16",
            "completed": 1,
            "done": "HIIT 9 x (20s / 90s)",
            "rpe": None,
            "notes": "reconstructed from plan",
            "efforts": 9,
            "effort_s": 20,
            "rest_s": 90,
        },
        {
            "date": "2026-10-21",
            "completed": 1,
            "done": "HIIT 12 x (45s / 45s)",
            "rpe": None,
            "notes": "reconstructed from plan",
            "efforts": 12,
            "effort_s": 45,
            "rest_s": 45,
        },
    ]


def test_backfill_twice_inserts_once(tmp_db):
    assert backfill(ENTRIES) == 2
    assert backfill(ENTRIES) == 0
    assert len(rows()) == 2


def test_backfill_does_not_skip_a_real_log_on_the_same_day(tmp_db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO session_logs (date, completed, done, created_at) "
            "VALUES ('2026-09-16', 1, 'Walk 30 min', 'x')"
        )
    assert backfill(ENTRIES) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"day": "16/09/2026", "efforts": 9, "effort_s": 20, "rest_s": 90},
        {"day": "2026-09-16", "efforts": 0, "effort_s": 20, "rest_s": 90},
        {"day": "2026-09-16", "efforts": 9, "effort_s": -20, "rest_s": 90},
        {"day": "2026-09-16", "efforts": 9, "effort_s": 20},
        {"day": "2026-09-16", "efforts": "9", "effort_s": 20, "rest_s": 90},
        {"day": "2026-09-16", "efforts": True, "effort_s": 20, "rest_s": 90},
    ],
)
def test_invalid_entry_writes_nothing(tmp_db, bad):
    with pytest.raises(ValueError):
        backfill([ENTRIES[0], bad])
    assert rows() == []


def test_backfilled_rows_feed_progress(tmp_db):
    backfill(ENTRIES)
    data = progress.load_progress()
    assert len(data.hiit) == 2
    summary = progress.summarize(data)["hiit"]
    assert (summary["first_ratio"], summary["latest_ratio"]) == (0.22, 1.0)


def test_cli_initializes_db_and_prints_count(tmp_path):
    path = tmp_path / "backfill.json"
    path.write_text(json.dumps(ENTRIES))
    env = {
        "DATA_DIR": str(tmp_path / "data"),
        "ANTHROPIC_API_KEY": "test",
        "PATH": "/usr/bin:/bin",
    }
    out = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_hiit", str(path)],
        cwd=config.ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip().endswith("2")
    assert (tmp_path / "data" / "agents.sqlite3").exists()
