import struct
from datetime import date, timedelta

from app import db, progress


def add_log(
    day,
    done="x",
    completed=1,
    rpe=None,
    distance_m=None,
    efforts=None,
    effort_s=None,
    rest_s=None,
):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO session_logs (date, completed, done, rpe, distance_m, efforts, effort_s, rest_s, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00')",
            (day, completed, done, rpe, distance_m, efforts, effort_s, rest_s),
        )


def add_benchmark(day, distance_m, split=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO benchmarks (date, distance_m, avg_split_s, created_at) VALUES (?, ?, ?, '2026-01-01T00:00:00')",
            (day, distance_m, split),
        )


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def full_dataset():
    add_benchmark("2026-10-12", 4000, 150.0)
    add_benchmark("2026-11-08", 4300, 145.0)
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    add_log("2026-10-21", efforts=12, effort_s=45, rest_s=45)
    add_log("2026-10-19", distance_m=6000, rpe=5)
    add_log("2026-11-02", distance_m=6400, rpe=5)


def test_load_progress_filters_completed_and_structured_rows(tmp_db):
    add_log("2026-11-04", efforts=12, effort_s=45, rest_s=45)
    add_log("2026-10-28", completed=0, efforts=15, effort_s=40, rest_s=40)  # skipped
    add_log("2026-10-21", efforts=12, effort_s=45)  # no rest_s
    add_log("2026-11-06", distance_m=3000, rpe=7)
    add_log("2026-11-07", completed=0, distance_m=2000)  # skipped
    add_log("2026-11-08", distance_m=4300)  # benchmark day
    add_log("2026-11-02", distance_m=6400, rpe=5)
    add_benchmark("2026-11-08", 4300, 145.0)
    data = progress.load_progress()
    assert data.hiit == [(date(2026, 11, 4), 12, 45, 45)]
    assert data.rows == [(date(2026, 11, 2), 6400, 5), (date(2026, 11, 6), 3000, 7)]
    assert data.benchmarks == [(date(2026, 11, 8), 4300, 145.0)]


def test_load_progress_keeps_latest_benchmark_of_a_day(tmp_db):
    add_benchmark("2026-11-08", 4200)
    add_benchmark("2026-10-12", 4000, 150.0)
    add_benchmark("2026-11-08", 4300, 145.0)  # re-recorded the same day: correction
    assert progress.load_progress().benchmarks == [
        (date(2026, 10, 12), 4000, 150.0),
        (date(2026, 11, 8), 4300, 145.0),
    ]


def test_summarize_benchmark_and_hiit(tmp_db):
    add_benchmark("2026-11-08", 4300, 145.0)
    add_benchmark("2026-10-12", 4000, 150.0)
    add_log("2026-10-21", efforts=12, effort_s=50, rest_s=40)
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    summary = progress.summarize(progress.load_progress())
    assert summary["benchmark"] == {
        "first_m": 4000,
        "latest_m": 4300,
        "delta_m": 300,
        "delta_pct": 7.5,
        "first_split_s": 150.0,
        "latest_split_s": 145.0,
        "delta_split_s": -5.0,
    }
    assert summary["hiit"] == {
        "first_ratio": 0.8,
        "latest_ratio": 1.25,
        "first": {"efforts": 12, "effort_s": 40, "rest_s": 50},
        "latest": {"efforts": 12, "effort_s": 50, "rest_s": 40},
    }


def test_summarize_counts_sessions_in_the_28_days_before_latest_log(tmp_db):
    latest = date(2026, 11, 20)
    for delta in (0, 5, 27, 28, 40):
        add_log((latest - timedelta(days=delta)).isoformat())
    add_log((latest - timedelta(days=1)).isoformat(), completed=0)
    assert progress.summarize(progress.load_progress())["sessions_last_28_days"] == 3


def test_summarize_omits_keys_without_enough_data(tmp_db):
    assert progress.summarize(progress.load_progress()) == {"sessions_last_28_days": 0}
    add_benchmark("2026-10-12", 4000)
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    summary = progress.summarize(progress.load_progress())
    assert "benchmark" not in summary
    assert "hiit" not in summary
    assert summary["sessions_last_28_days"] == 1


def test_summarize_omits_split_when_unknown(tmp_db):
    add_benchmark("2026-10-12", 4000)
    add_benchmark("2026-11-08", 4100, 145.0)
    assert progress.summarize(progress.load_progress())["benchmark"] == {
        "first_m": 4000,
        "latest_m": 4100,
        "delta_m": 100,
        "delta_pct": 2.5,
    }


def test_missing_panels_lists_french_names(tmp_db):
    assert progress.missing_panels(progress.load_progress()) == [
        "benchmark rameur",
        "HIIT effort/repos",
        "distance rameur",
    ]
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    add_log("2026-10-21", efforts=12, effort_s=45, rest_s=45)
    assert progress.missing_panels(progress.load_progress()) == [
        "benchmark rameur",
        "distance rameur",
    ]


def test_render_png_returns_png_bytes(tmp_db):
    full_dataset()
    png = progress.render_png(progress.load_progress())
    width, height = png_size(png)
    assert width <= 1200
    assert height > width / 2


def test_render_png_none_when_not_enough_data(tmp_db):
    assert progress.render_png(progress.load_progress()) is None
    add_benchmark("2026-10-12", 4000)
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    add_log("2026-10-19", distance_m=6000)
    assert progress.render_png(progress.load_progress()) is None


def test_render_png_with_only_one_panel_is_shorter_than_three(tmp_db):
    add_log("2026-10-14", efforts=12, effort_s=40, rest_s=50)
    add_log("2026-10-21", efforts=12, effort_s=45, rest_s=45)
    _, one = png_size(progress.render_png(progress.load_progress()))
    full_dataset()
    _, three = png_size(progress.render_png(progress.load_progress()))
    assert one < three


def test_render_handles_long_history(tmp_db):
    start = date(2025, 1, 1)
    for i in range(60):
        add_log(
            (start + timedelta(days=7 * i)).isoformat(),
            efforts=12,
            effort_s=20 + i % 30,
            rest_s=60 - i % 30,
        )
    width, _ = png_size(progress.render_png(progress.load_progress()))
    assert width <= 1200
