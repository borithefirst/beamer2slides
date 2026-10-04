"""A superscript beside \\tiny words is nearly their size (TeX's script sizes stop at 5 pt): the
5.48 pt `$^*$` opening '* plus complicated models' (real_third-year-talk-2017-lmcarvalh p2) was
written at the baseline as a plain 5.48 pt asterisk."""

from beamer2slides.classify_text import script_of

from .test_charts_diagrams import Page, body_text, elements, span, text_elements


def test_a_raised_mark_nearly_its_words_size_is_a_superscript() -> None:
    words = span("plus", 150.46, 5.98, 101.14)
    assert script_of(span("∗", 146.33, 5.48, 98.79), words) == "super"  # raised 0.39 em
    assert script_of(span("∗", 146.33, 5.48, 100.5), words) is None    # raised 0.11 em: no script
    assert script_of(span("∗", 146.33, 5.98, 98.79), words) is None    # its words' size


def test_the_footnote_asterisk_is_written_as_a_raised_mark() -> None:
    p = Page()
    p.text("∗", 146.33, 98.79, 5.48, font="pxsys", w=2.13)
    p.text("plus complicated models", 150.46, 101.14, 5.98, font="TeXGyrePagellaX-Regular", w=66.04)
    body_text(p)
    runs = [r for e in text_elements(elements(p)) for par in e["paragraphs"] for r in par["runs"]]
    star = next(r for r in runs if r["text"].strip() in ("*", "∗"))
    # Lato's '*' stands raised: written at the line's size, as every raised asterisk is (RAISED_MARKS)
    assert star["text"].strip() == "*" and star["size"] == 5.98, runs
