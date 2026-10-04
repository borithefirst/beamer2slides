"""A tree standing on a three-line caption beside a plot that reaches down past the caption's
first lines (real_third-year-talk-2017-lmcarvalh p10, panel 6): those lines were the figure's
labels, in its picture in the PDF's face, and only the last line was native text in Slides' face
and measure. The caption is given back whole, the picture holding the drawing and its number."""

from .test_charts_diagrams import Page, body_text, elements, images, lines, run_texts, text_elements

CAPTION = ("Hastings ratio: ratio of reverse probability", "(1 / number of reverse locations) to",
           "forwards probability (i.e., 1/5).")


def panel() -> Page:
    p = Page()
    p.text("6)", 232.1, 112.0, 6.0)
    root = (266.0, 112.0)
    for leaf_x in (240.0, 252.0, 258.0, 265.0, 278.0, 286.0):
        p.draw(lines(root, (leaf_x, 152.6)), type="s", stroke="#231f20", width=0.4)
    p.draw(lines((230.2, 134.5), (308.6, 134.5)), type="s", stroke="#231f20", width=0.4)  # a height across
    # the plot beside it: down past the caption's first two lines, its foot on the third
    p.draw(lines((293.1, 167.0), (293.1, 107.2), (308.6, 137.0), closed=True), type="fs",
           fill="#89b8e2", stroke="#231f20", width=0.4)
    for i, words in enumerate(CAPTION):
        p.text(words, 230.3, 158.0 + 6.5 * i, 6.0)
    body_text(p)
    return p


def test_caption_lines_a_standing_figure_held_go_back_to_the_caption() -> None:
    els = elements(panel())
    caption = [e for e in text_elements(els) if any("forwards" in run_texts(par["runs"]) for par in e["paragraphs"])]
    assert len(caption) == 1, [run_texts(par["runs"]) for e in text_elements(els) for par in e["paragraphs"]]
    words = " ".join(run_texts(par["runs"]) for par in caption[0]["paragraphs"])
    assert all(w in words for w in ("Hastings", "number", "forwards")), words
    figures = images(els)
    assert len(figures) == 1 and not figures[0].get("overlay"), figures
    assert len(figures[0]["spans"]) == 1, figures[0]["spans"]  # its number only
