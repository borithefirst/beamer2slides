"""Glyph bullets sized and shaped by their ink (render.glyph_ink), and every bullet no larger
than its item's text (emit.text_box_requests, `body_size`)."""

from pathlib import Path

import pytest

from beamer2slides import emit
from beamer2slides.emit import BULLET_SHAPES, FontMapper, bullet_level, bullet_shape, bullet_size, text_box_requests

DECKS = Path(__file__).resolve().parent / "decks" / "out"
SCALE = 1.98


def first_glyph(pdf: Path, glyph: str):
    """The first character `glyph` of a built deck: (page, box, size)."""
    from beamer2slides.pdf import Document, backend, renders
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    if not renders(backend()):
        pytest.skip("this backend does not draw")
    doc = Document(pdf)
    for page in doc:
        for ch in page.chars():
            if ch.c == glyph:
                return doc, page, ch.box, ch.size
    pytest.fail(f"no {glyph} in {pdf.name}")


@pytest.mark.parametrize("deck,glyph,height,fill", [
    ("09_metropolis_fira", "•", (0.12, 0.18), (0.7, 0.86)),  # Fira Sans Light: a small dot
    ("01_basic", "▶", (0.55, 0.61), (0.5, 0.6)),             # MSAM10's triangle
    ("21_bullet_shapes", "■", (0.65, 0.72), (0.95, 1.0)),
])
def test_glyph_ink_is_what_the_pdf_draws_not_the_font_box(deck, glyph, height, fill):
    from beamer2slides.render import glyph_ink
    doc, page, box, size = first_glyph(DECKS / f"{deck}.pdf", glyph)
    try:
        ink, f = glyph_ink(page, list(box))
    finally:
        doc.close()
    assert box[0] - 0.5 <= ink[0] < ink[2] <= box[2] + 0.5 and box[1] - 0.5 <= ink[1] < ink[3] <= box[3] + 0.5
    assert height[0] <= (ink[3] - ink[1]) / size <= height[1] and fill[0] <= f <= fill[1]


def glyph(text: str, ink_em: tuple[float, float], fill: float, size: float = 10.91) -> dict:
    """A glyph bullet of a `size` pt label with its box 1 em tall and its ink (w, h) em."""
    w, h = ink_em
    return {"kind": "glyph", "text": text, "color": "#000000", "bbox": [22.0, 90.0, 22.0 + 0.5 * size, 90.0 + size],
            "label": {"size": size}, "ink": [22.5, 95.0, 22.5 + w * size, 95.0 + h * size], "fill": fill}


def test_a_small_dot_is_sized_by_its_ink():
    """r2_fonts_firamath s2 (metropolis, Fira Sans Light): a 0.15 em dot became Slides' disc
    at the text's full size, 0.41 em."""
    dot = glyph("•", (0.155, 0.149), 0.78)
    text = 10.91 * SCALE
    assert bullet_shape(dot) == "disc"
    z = bullet_size(dot, text, SCALE)
    assert z == round(0.149 * 10.91 * SCALE / BULLET_SHAPES["disc"][2], 1) and z < 0.5 * text
    # beside it the bullet's right edge is its ink's, the gap the preset's
    x0, x1, gap = emit.bullet_extent(dot, text, SCALE)
    assert (x0, x1) == (dot["ink"][0], dot["ink"][2]) and gap == BULLET_SHAPES["disc"][3] * z


def test_a_bullet_drawn_square_is_a_square():
    """r3_layout_v2 s2 (metropolis + lmodern): LM Sans's \\textbullet is a filled 0.29 em square;
    it became a larger round dot."""
    square = glyph("•", (0.286, 0.286), 1.0)
    assert bullet_shape(square) == "square" and bullet_level(square, 0) == 2 and bullet_level(square, 1) == 5
    assert bullet_size(square, 10.91 * SCALE, SCALE) == round(0.286 * 10.91 * SCALE / BULLET_SHAPES["square"][2], 1)


def test_beamers_own_glyphs_keep_the_labels_size():
    text = 10.91 * SCALE
    for b in (glyph("•", (0.39, 0.39), 0.78),    # CMSY's bullet: about the disc preset's ink
              glyph("▶", (0.613, 0.579), 0.54),  # MSAM's triangle
              {"kind": "glyph", "text": "•", "color": "#000000", "bbox": [22, 90, 27, 101], "label": {"size": 10.91}}):
        assert bullet_size(b, text, SCALE) == round(text, 1)
        assert emit.bullet_extent(b, text, SCALE) == (b["bbox"][0], b["bbox"][2], emit.BULLET_GAP)


def run_of(text: str, size: float = 10.91, font: str = "LMSans10-Regular") -> dict:
    return {"text": text, "font": font, "family": "sans", "size": size, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None}


def test_a_bullet_is_no_larger_than_its_items_text():
    """r3_textfx_v1 s8: one {\\Large tenfold} in an item made its bullet the large word's size,
    larger than its neighbours'."""
    ball = {"kind": "image", "image": "p7i1", "text": "", "bbox": [22.0, 107.0, 28.0, 113.0]}
    item = {"align": "left", "level": 0, "size": 10.91, "text_x0": 35.0, "tab_x0": None, "wrap_limit": None,
            "bullet": ball, "lines": [{"baseline": 112.0, "x0": 35.0, "x1": 300.0}],
            "runs": [run_of("Our sensor weighs 12 g, a "), run_of("tenfold ", 14.35, "LMSans12-Regular"),
                     run_of("reduction in mass over the last generation.")]}
    plain = {**item, "runs": [run_of("Our sensor weighs 12 g, a tenfold reduction in mass over the last generation.")],
             "lines": [{"baseline": 128.0, "x0": 35.0, "x1": 300.0}], "bullet": {**ball, "bbox": [22.0, 123.0, 28.0, 129.0]}}
    el = {"id": "t", "kind": "text", "role": "body", "paragraphs": [item, plain]}
    reqs = text_box_requests(el, "s", "b", SCALE, FontMapper())
    # the bullets' own style: the first request over each whole paragraph, with its colour
    whole = [r["updateTextStyle"]["style"]["fontSize"]["magnitude"] for r in reqs
             if "updateTextStyle" in r and "foregroundColor" in r["updateTextStyle"]["style"]
             and r["updateTextStyle"]["fields"].startswith("fontFamily,fontSize,foregroundColor")]
    assert len(whole) == 2 and whole[0] == whole[1]


def edge_page():
    """A colorbar (a vertical purple gradient) ending in a black rule on white, 10 px per pt, and
    what it looks like with a blue ball bullet drawn over the rule (r2_figures_v1 s7)."""
    import numpy as np
    img = np.full((200, 200, 3), 255, np.uint8)
    for y in range(200):
        img[y, :100] = (70 - y // 8, 50, 140 - y // 4)
    img[:, 100:103] = 0
    ball = img.copy()
    yy, xx = np.mgrid[:200, :200]
    ball[(yy - 100) ** 2 + (xx - 101) ** 2 <= 30 ** 2] = (57, 57, 139)
    return img, ball


def test_ball_patch_on_a_pictures_edge_keeps_the_edge():
    """The patch was the ring's median: a lilac square over the colorbar's edge."""
    import numpy as np
    from beamer2slides.render import patch_rects
    page, ball = edge_page()
    patch_rects(ball, [[7.0, 7.0, 13.2, 13.2]], 10.0)
    assert np.abs(ball.astype(int) - page.astype(int)).max() <= 12


def test_ball_colour_on_a_pictures_edge_is_the_balls():
    from beamer2slides.render import ink_colour
    _, ball = edge_page()
    assert ink_colour(ball, [7.0, 7.0, 13.2, 13.2], 10.0) == "#39398b"


def test_a_ball_shows_its_letter_not_the_white_parentheses_at_its_edges():
    """r3_dense_v2 s7: enumerate items [ball] with [(a)]: the white parentheses sit on the ball's
    rim and the page, unseen; set in Slides' font they cut white crescents into the ball."""
    from beamer2slides.classify import ball_number
    white, blue = {"color": "#ffffff", "x0": 12.88}, {"color": "#3333b3", "x0": 12.88}
    ball = {"kind": "image", "image": "p6i0", "text": "(a)", "bbox": [13, 92, 22, 101]}
    assert ball_number(ball, white) == "a"
    assert ball_number({**ball, "text": "iv"}, white) == "iv"
    assert ball_number(ball, blue) == "(a)", "parentheses one can see stay"
    assert ball_number({**ball, "kind": "number"}, white) == "(a)"


def test_parentheses_on_the_balls_face_stay():
    """r3_dense_v3 s7: [(i)] on 10 pt balls. '(i)' and '(ii)' are narrower than the ball and
    their parentheses show on its dark face; wave 2 dropped them. '(iii)' reaches the rim."""
    from beamer2slides.classify import ball_number
    ball = {"kind": "image", "image": "p6i0", "text": "(i)", "bbox": [15.0, 168.0, 25.0, 178.0]}
    assert ball_number(ball, {"color": "#ffffff", "x0": 16.3}) == "(i)"
    assert ball_number({**ball, "text": "(ii)"}, {"color": "#ffffff", "x0": 15.54}) == "(ii)"
    assert ball_number({**ball, "text": "(iii)"}, {"color": "#ffffff", "x0": 14.78}) == "iii"
