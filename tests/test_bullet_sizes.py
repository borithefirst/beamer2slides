"""Bullet sizes against the PDF's ink: an icon font's glyph drawn as Slides' nearest preset is sized
by its ink (`emit_metrics.ink_sized`, `SetBullet.label_icon`), beamer's own glyphs keep the
label's size, and no bullet is larger than its item's text."""

from beamer2slides.emit_metrics import BULLET_SHAPES, bullet_extent_of, bullet_size_of, ink_sized
from beamer2slides.emit_model import GlyphInk, SetBullet, bullet_of
from beamer2slides.ir_types import Color
from beamer2slides.json_types import JsonObject

SCALE = 1.9844
STAR_INK = BULLET_SHAPES["star"][2]


def star(*, icon: bool, ink_em: float, size: float) -> SetBullet:
    """A ★ glyph bullet of a `size` pt label at x 161, its ink `ink_em` of the label tall."""
    top = 58.3 - ink_em * size
    return SetBullet(kind="glyph", text="★", bbox=(161.05, 58.3 - size, 165.77, 58.3), color=Color("#000000"),
                     label_size=size, label_icon=icon, ink=GlyphInk(box=(161.25, top, 165.56, 58.38), fill=0.21),
                     shape=None)


def test_a_dingbat_star_is_as_tall_as_the_pdfs() -> None:
    """real_africa-remote-sens-30 41: pifont's eight-pointed ✴ (Zapf Dingbats, 0.72 em of ink at
    5.98 pt) is written as Slides' ★ (0.81 em). At the label's size it came out 12% taller than
    the PDF's; sized by its ink it is as tall."""
    text = 5.98 * SCALE
    b = star(icon=True, ink_em=0.72, size=5.98)
    z = bullet_size_of(b, text, SCALE, False)
    pdf_ink = (b.ink.box[3] - b.ink.box[1]) * SCALE if b.ink is not None else 0.0
    assert abs(z * STAR_INK - pdf_ink) < 0.1
    assert z < text
    # placed by its ink, as any ink-sized bullet
    x0, x1, gap = bullet_extent_of(b, text, SCALE, False)
    assert (x0, x1) == (161.25, 165.56) and gap == BULLET_SHAPES["star"][3] * z


def test_a_star_of_the_texts_own_font_keeps_the_labels_size() -> None:
    """Only an icon font's glyph is told apart: a ★ set in the text's font within 25% of the
    preset's ink keeps the label's size (INK_SIZED), as beamer's own glyphs do."""
    text = 5.98 * SCALE
    b = star(icon=False, ink_em=0.72, size=5.98)
    assert ink_sized(b, text, SCALE, False) is None
    assert bullet_size_of(b, text, SCALE, False) == round(text, 1)


def test_a_dingbat_bullet_is_no_larger_than_its_text() -> None:
    text = 5.98 * SCALE
    big = star(icon=True, ink_em=0.95, size=5.98)
    assert bullet_size_of(big, text, SCALE, False) == round(text, 1)


def test_deck_json_says_icon_by_the_labels_family() -> None:
    """`bullet_of` (the dict reader) reads `label_icon` from the label's family, for glyphs only."""
    glyph: JsonObject = {"kind": "glyph", "text": "★", "bbox": [161.05, 52.31, 165.77, 58.29], "color": "#000000",
                         "label": {"font": "Dingbats", "family": "icon", "size": 5.98},
                         "ink": [161.25, 54.06, 165.56, 58.38], "fill": 0.21}
    assert bullet_of(glyph).label_icon
    assert not bullet_of({**glyph, "label": {"font": "CMSY10", "family": "symbol", "size": 10.91}}).label_icon
    number: JsonObject = {"kind": "number", "text": "1.", "bbox": [0.0, 0.0, 5.0, 10.0],
                          "label": {"font": "Dingbats", "family": "icon", "size": 10.0}}
    assert not bullet_of(number).label_icon
