"""Fonts the r9 verifiers found wrong after wave 4 (out/hunt/WAVE5.md, fixer B): a 6 pt footline
drawn Bold, a small reference block whose bold volume numbers no longer stood out, and a hyphen
Slides drew from a fallback font. Synthetic runs, no PDF."""

from beamer2slides import emit
from beamer2slides.extract import readable

from .test_emit_hunt import styled, text_element
from .test_emit_requests import FONTS, SCALE, run_of


def drawn_bold(style: dict) -> bool:
    """Whether Slides draws a written run style with the family's Bold face: `bold`, or a weight
    of 700 or more (probe_font_weights: Lato 500 and 600 draw Regular, 700 and 800 Bold)."""
    weight = (style.get("weightedFontFamily") or {}).get("weight") or 0
    return bool(style.get("bold")) or weight >= emit.DRAWN_BOLD_WEIGHT


def test_a_tiny_reference_block_keeps_its_bold_numbers_heavier_than_its_words():
    # r1_sci_v1 s8 (r9): '[1] Y. Hori, Modern Aspects of Electrochemistry 42, 89 (2008).' in
    # \tiny (SFSS0600, SFSI0600, SFSX0600 for the volume): wave 4 set the regular cut at Lato 800,
    # which Slides draws Bold, and the whole block came out bold - the volume no longer stood out.
    el = text_element([run_of("[1] Y. Hori, ", 5.98, font="SFSS0600"),
                       run_of("Modern Aspects of Electrochemistry ", 5.98, font="SFSI0600", italic=True),
                       run_of("42", 5.98, font="SFSX0600", bold=True),
                       run_of(", 89 (2008).", 5.98, font="SFSS0600")])
    styles = dict(styled(emit.text_box_requests(el, "b2s_s001", "b2s_s001_t0", SCALE, FONTS)))
    assert drawn_bold(styles["42"])
    for words in ("[1] Y. Hori, ", "Modern Aspects of Electrochemistry ", ", 89 (2008)."):
        assert not drawn_bold(styles[words]), words


def test_a_six_point_footline_is_drawn_in_the_regular_face():
    # r4_control_a2 (every slide), r1_sci_v1 s1 'M. Keller' (r9): Lato 800 read heavier than the
    # PDF. Stroke widths on the r8/r9 renders: PDF 1.85 px, Lato Regular 1.75, Lato Bold 2.35.
    for font in ("SFSS0600", "ECSS0600", "SFSS0500"):
        style = FONTS.text_style(run_of("Dr. Peter Schmidt", 5.98, font=font), SCALE)[0]
        assert not drawn_bold(style), font
        assert FONTS.face(run_of("x", 5.98, font=font)) == "regular"
    # (and still written at a weight deck_ir knows for a small cut: its width inverts as a 6 pt one)
    assert emit.OPTICAL_WEIGHT in emit.OPTICAL_WEIGHTS_READ


def test_a_non_breaking_hyphen_is_written_as_a_hyphen():
    # r1_lang_v3 s5: fontspec's Cambria (and lang_v1's Palatino Linotype) map the '-' of
    # 'Spear-Danes' to U+2011 in the PDF's ToUnicode; Caladea lacks it and Slides drew a short
    # dash from a fallback font, raised off the hyphen's height.
    assert readable("Spear‑Danes’ kings", "Cambria") == "Spear-Danes’ kings"
    assert readable("well‑known", "PalatinoLinotype-Roman") == "well-known"
