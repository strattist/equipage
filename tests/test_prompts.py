from pathlib import Path

COACH = Path("prompts/coach.md").read_text(encoding="utf-8")


def test_coach_prompt_has_no_checkin_machinery():
    for text in (
        "[check-in]",
        "# Check-ins",
        "check in about every",
        "you may ask for the distance",
    ):
        assert text not in COACH


def test_coach_prompt_is_pull_based():
    for phrase in (
        "Never ask the user for a report, a log, a distance or any missing information about a past session.",
        "An unlogged session is unknown, never a failure.",
        "When data is missing, hold the load",
    ):
        assert phrase in COACH


def test_progress_messages_section_is_kept():
    assert "[progress]" in COACH
