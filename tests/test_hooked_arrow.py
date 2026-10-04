"""A hooked arrow is a hole at its PDF length (real_beamer-derived-cat s9, "Si i : X \\hookrightarrow
Y es la inclusion"): TeX sets \\hookrightarrow as a math italic hook overlapped by a whole CMSY
arrow, 1.4 em; composed into Slides' U+21AA it came out half that, the formula closed up around
it. `classify_text.long_arrow_groups` takes it as it takes \\longrightarrow."""

from beamer2slides.classify import Rect, Span, new_span
from beamer2slides.classify_text import long_arrow_groups
from beamer2slides.fonts import font_info


def span(sid: str, text: str, font: str, box: tuple[float, float, float, float]) -> Span:
    return new_span(id=sid, text=text, font=font, size=11.96, color="#000000", rect=Rect(*box), baseline=88.89,
                    horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def test_a_hook_and_its_arrow_are_one_long_arrow() -> None:
    x = span("x", "X", "LinLibertineTI", (75.47, 79.94, 82.98, 91.9))
    hook = span("hook", "↩", "NewTXMI", (87.55, 77.18, 91.36, 89.13))
    arrow = span("arrow", "→", "txsys", (89.7, 79.05, 101.94, 91.01))
    y = span("y", " Y", "LinLibertineTI", (101.94, 79.94, 111.86, 91.9))
    assert [[s.id for s in g] for g in long_arrow_groups([x, hook, arrow, y])] == [["hook", "arrow"]]


def test_a_plain_arrow_is_none() -> None:
    arrow = span("arrow", "→", "txsys", (89.7, 79.05, 101.94, 91.01))
    assert long_arrow_groups([arrow]) == []
