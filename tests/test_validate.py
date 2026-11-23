import pytest

from app.validate import check_lines, check_text, parse_fridge


@pytest.mark.parametrize(
    "text",
    [
        "voir https://x.io",
        "http://a",
        "www.exemple.fr",
        "[ici](https://x)",
        "<@123>",
        "@everyone salut",
        "@HERE",
    ],
)
def test_check_text_rejects_links_and_mentions(text):
    with pytest.raises(ValueError):
        check_text(text, "steps", 200)


def test_check_text_strips_and_keeps_accents():
    assert (
        check_text("  Faire revenir l'oignon à feu doux. ", "step", 200)
        == "Faire revenir l'oignon à feu doux."
    )


def test_check_text_rejects_empty_and_too_long():
    for bad in ("   ", "a" * 201):
        with pytest.raises(ValueError):
            check_text(bad, "step", 200)


def test_check_lines_limits_count_and_checks_each_line():
    with pytest.raises(ValueError):
        check_lines(["a"] * 3, "ingredients", 2, 50)
    with pytest.raises(ValueError):
        check_lines(["ok", "http://x.io"], "ingredients", 5, 50)
    assert check_lines([" a ", "b"], "ingredients", 5, 50) == ["a", "b"]


def test_parse_fridge_accepts_normal_lines():
    assert parse_fridge("œufs: 6\nlentilles\n\nlait: 1 L") == (
        [("œufs", "6"), ("lentilles", ""), ("lait", "1 L")],
        [],
    )


def test_parse_fridge_reports_rejected_lines():
    ok, rejected = parse_fridge("beurre: 250 g\nvoir http://x.io\n<@1>\n" + "a" * 41)
    assert ok == [("beurre", "250 g")]
    assert rejected == ["voir http://x.io", "<@1>", "a" * 40]


def test_parse_fridge_limits():
    with pytest.raises(ValueError):
        parse_fridge("\n".join(f"a{i}" for i in range(41)))
    with pytest.raises(ValueError):
        parse_fridge("a: 1\n" * 130)


def test_parse_fridge_empty_text():
    assert parse_fridge("") == ([], [])
