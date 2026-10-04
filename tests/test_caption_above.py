"""A tree standing on a three-line caption whose first two lines its box holds as labels
(real_third-year-talk-2017-lmcarvalh p10, panel 6): only the last line was words, one stroke
ended on it, and the tree stayed an overlay stretched with the caption."""

from beamer2slides.classify_figures import caption_above

from .test_charts_diagrams import span


def test_caption_lines_held_as_labels_are_stacked_up_from_the_met_line() -> None:
    first = span("Hastings ratio: ratio of reverse probability", 230.26, 5.0, 158.0)
    second = span("(1 / number of reverse locations) to", 230.26, 5.0, 162.0)
    last = span("forwards probability (i.e., 1/5).", 230.26, 5.0, 166.0)
    numeral = span("6)", 232.1, 5.0, 108.0)  # the panel's number: same column, far above
    elsewhere = span("operator", 290.0, 5.0, 162.0)  # a label beside, not above
    assert caption_above([last], [numeral, first, second, elsewhere]) == [second, first]
    assert caption_above([last], [numeral, elsewhere]) == []
