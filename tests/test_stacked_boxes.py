"""Framed boxes stacked one under another with a gap between them (real_decision-tree-lect s34:
two \\fbox'd tabulars per column, 8 pt apart) are tables of their own: grouped by their rules'
common extent they were one table whose middle row was the empty gap, its rules drawn where no
rule is and the gap's height given to a row (`classify_graphics.stacked_boxes`). Synthetic page."""

from beamer2slides.classify_graphics import stacked_boxes
from beamer2slides.classify_model import Rect
from beamer2slides.classify_state import Rule

from .test_tables_hunt import BLACK, Page, cell_texts, tables

X0, X1 = 60.0, 200.0


def framed(page: Page, top: float, words: tuple[str, str]) -> None:
    """A framed one-column tabular of two rows, 12 pt each, its frame's sides from top to bottom."""
    for y in (top, top + 12, top + 24):
        page.hline(X0, X1, y, 0.4)
    page.vline(X0, top, top + 24, 0.4)
    page.vline(X1, top, top + 24, 0.4)
    page.words(words[0], X0 + 4, top + 9)
    page.words(words[1], X0 + 4, top + 21)


def test_stacked_framed_boxes_are_tables_of_their_own() -> None:
    page = Page()
    framed(page, 100, ("Pick", "Lottery A"))
    framed(page, 132, ("Pick", "Lottery B"))
    got = tables(page.elements())
    assert sorted(cell_texts(t) for t in got) == [[["Pick"], ["Lottery A"]], [["Pick"], ["Lottery B"]]]


def rule(y: float) -> Rule:
    return Rule(rect=Rect(X0, y - 0.2, X1, y + 0.2), color=BLACK, weight=0.4)


def upright(x: float, y0: float, y1: float) -> Rect:
    return Rect(x - 0.2, y0, x + 0.2, y1)


def frames(*spans: tuple[float, float]) -> list[Rect]:
    """Both sides of a box for each (top, bottom)."""
    return [upright(x, y0, y1) for y0, y1 in spans for x in (X0, X1)]


def test_a_gap_with_words_or_a_side_running_through_it_is_a_row() -> None:
    rules = [rule(y) for y in (100, 112, 124, 132, 144, 156)]
    sides = frames((100, 124), (132, 156))
    assert [len(part) for part in stacked_boxes(rules, sides, [], 10.0)] == [3, 3]
    # words in the gap: a row
    assert [len(part) for part in stacked_boxes(rules, sides, [Rect(70, 126, 90, 130)], 10.0)] == [6]
    # a side drawn through the gap: one frame
    assert [len(part) for part in stacked_boxes(rules, frames((100, 156)), [], 10.0)] == [6]
    # a \hline\hline's two rules, 2 pt apart: one table
    double = [rule(y) for y in (100, 112, 114, 126)]
    assert [len(part) for part in stacked_boxes(double, frames((100, 112), (114, 126)), [], 10.0)] == [4]


def test_rows_each_between_rules_of_their_own_are_one_table() -> None:
    # real_africa-remote-sens-30's PDF page 10: each row of a small-type table between two
    # rules 2.84 pt from the next row's, a column rule between them and none at the table's
    # ends; a gap over 0.4 em of its body size, but no box closes there
    rules = [rule(y) for y in (70.1, 72.94, 80.76, 83.6, 91.42, 94.26, 102.08)]
    column = [upright(75.0, 73.36, 80.34), upright(75.0, 84.02, 91.0), upright(75.0, 94.68, 101.66)]
    assert [len(part) for part in stacked_boxes(rules, column, [], 6.4)] == [7]
