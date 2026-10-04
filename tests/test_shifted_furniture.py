"""Theme text on every slide that moves a little across (`classify.shifted_theme_texts`): a centred
footline author shifts 2 pt once the frame number has two digits (real_africa-remote-sens-30
'Леменкова П. А.' at x 223 and 225) and is still one text on the layout."""

from beamer2slides import ir
from beamer2slides.classify import loose_key, shifted_place, shifted_theme_texts

Key = tuple[str, int, int, str]


def theme(words: str, x: int) -> ir.ThemeText:
    return {"kind": "text", "role": "layout", "bbox": [float(x), 265.0, float(x) + 30, 270.0], "panel": None,
            "code": False, "key": (words, x, 269, "#000000"), "chars": len(words),
            "paragraphs": [{"align": "left", "level": 0, "bullet": None, "size": 5.0, "text_x0": float(x),
                            "lines": [], "runs": []}],
            "spans": []}


def by_key(*texts: ir.ThemeText) -> dict[Key, ir.ThemeText]:
    return {t["key"]: t for t in texts}


def test_an_author_shifted_two_points_is_one_layout_text() -> None:
    keys = [by_key(theme("Author", 223)), by_key(theme("Author", 223)), by_key(theme("Author", 225))]
    shifted = shifted_theme_texts(keys, set())
    assert shifted == {("Author", 269, "#000000")}
    assert shifted_place(keys, ("Author", 269, "#000000"))["key"][1] == 223   # where most slides have it


def test_words_further_than_half_an_em_apart_are_not_one_text() -> None:
    keys = [by_key(theme("Author", 223)), by_key(theme("Author", 229))]
    assert shifted_theme_texts(keys, set()) == set()


def test_a_text_twice_on_a_slide_or_already_common_is_left_alone() -> None:
    twice = [by_key(theme("A", 10), theme("A", 11)), by_key(theme("A", 10))]
    assert shifted_theme_texts(twice, set()) == set()
    same = [by_key(theme("A", 10)), by_key(theme("A", 10))]
    common: set[Key] = {("A", 10, 269, "#000000")}
    assert shifted_theme_texts(same, common) == set()
    assert loose_key(("A", 10, 269, "#000000")) == ("A", 269, "#000000")
