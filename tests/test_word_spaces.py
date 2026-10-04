"""Word spaces lost between words (the realdecks campaign, track A): the space after an italic
ligature whose loose box reaches its ink, and the word gaps of a line set with negative tracking.
Synthetic characters, no PDF."""

import dataclasses

from beamer2slides import extract
from beamer2slides.pdf import Char

from .test_span_joining import Page, Shown, line_of

ITALIC, UPRIGHT = "LinBiolinumTI", "LinBiolinumT"


def texts(chars: list[Char], widths: dict[str, float]) -> list[str]:
    return [s.text for s in extract.spans(Page(chars, widths), Shown(), False, chars, {})]


def ligature(chars: list[Char], at: int) -> list[Char]:
    """`chars` with the one at `at` a ligature as PDFium gives it: its advance its loose box."""
    return [dataclasses.replace(ch, exact_advance=True) if k == at else ch for k, ch in enumerate(chars)]


def test_the_space_after_an_italic_ligature_stays() -> None:
    # real_beamer-monodromy s15: '... a \emph{left} action!' in LinBiolinum, the italic ft
    # ligature's loose box 0.769 em (its t's ink) against f + t = 0.62 em: the word space after
    # it measured 0.141 em, under JOIN_GAP, and the deck read 'leftaction'
    left = ligature(line_of([("a", UPRIGHT, 0.481, 0), ("l", ITALIC, 0.261, 0.25), ("e", ITALIC, 0.427, 0),
                             ("ft", ITALIC, 0.769, 0), ("a", UPRIGHT, 0.481, 0.141), ("c", UPRIGHT, 0.429, 0)],
                            size=10), 3)
    widths = {"f": 2.8, "t": 3.4}
    assert texts(left, widths) == ["a", " left", " ac"]  # (before: "a", " left", "ac")
    assert abs(extract._ligature_overhang(Page(left, widths), left)[3].advance - 6.2) < 1e-9
    # an upright ligature, or one whose letters are as wide as its box, keeps its advance
    upright = [dataclasses.replace(ch, font=UPRIGHT) for ch in left]
    assert extract._ligature_overhang(Page(upright, widths), upright)[3].advance == left[3].advance
    assert extract._ligature_overhang(Page(left, {"f": 3.8, "t": 3.8}), left)[3].advance == left[3].advance
    # a letter its font has no width for: nothing to measure by
    assert extract._ligature_overhang(Page(left, {"f": 2.8}), left)[3].advance == left[3].advance


def test_an_italic_ligature_inside_its_word_stays_joined() -> None:
    # 28_line_breaks p2: LMSans10-Oblique's fi, loose box 0.605 em, f + i 0.543, the 'n' after it
    # 0.069 em into the box: 0.007 em into the letters' widths, no space
    fin = ligature(line_of([("ﬁ", "LMSans10-Oblique", 0.605, 0), ("n", "LMSans10-Oblique", 0.556, -0.069)],
                           size=10.91), 0)
    assert texts(fin, {"f": 0.306 * 10.91, "i": 0.237 * 10.91}) == ["ﬁn"]


def tracked(words: list[tuple[str, float]], *, font: str, track: float) -> list[tuple[str, str, float, float]]:
    """(character, font, advance em, pen gap em before it) pieces of words given as (text, gap em
    before its first glyph), their glyphs 0.6 em wide and `track` em apart."""
    return [(c, font, 0.6, gap if i == 0 else track) for word, gap in words for i, c in enumerate(word)]


def test_a_tightly_tracked_line_keeps_its_word_spaces() -> None:
    # real_pnuc-intro-pnuc s17: a journal figure's caption, DejaVu Sans at 4.22 pt, every glyph
    # 0.056 em into the one before, the word gaps 0.096 - 0.17 em, the bold label's glyphs 0.105
    # em into each other; the deck read 'Figure3. Hazardratios (HRs)forrespiratory-disease–related
    # hospitalizationandmortality'
    label = tracked([("Figure", 0), ("3.", 0.064)], font="DejaVuSans-Bold", track=-0.105)
    # (an en dash before a letter stands 0.11 em past the tracking: no space)
    words = tracked([("Hazard", 0.856), ("ratios", 0.108), ("for", 0.128), ("respiratory-disease–", 0.108),
                     ("related", 0.055), ("hospitalization", 0.114), ("and", 0.113), ("mortality", 0.108),
                     ("associated", 0.096)], font="DejaVuSans", track=-0.056)
    line = line_of(label + words, size=4.22)
    assert " ".join(texts(line, {})) == \
        "Figure 3. Hazard ratios for respiratory-disease–related hospitalization and mortality associated"
    # (before: 'Figure3.', 'Hazardratiosforrespiratory-disease–relatedhospitalizationandmortalityassociated')


def test_ordinary_lines_gain_no_space() -> None:
    # kerns are pairs, not a line's tracking: a short heavily kerned word, and a line whose glyphs
    # touch, keep a 0.1 em gap inside their words
    wavy = line_of([("W", "DejaVuSans", 1.0, 0), ("A", "DejaVuSans", 0.7, -0.08), ("V", "DejaVuSans", 0.7, -0.08),
                    ("Y", "DejaVuSans", 0.6, -0.08), ("S", "DejaVuSans", 0.6, 0.1)], size=10)
    assert texts(wavy, {}) == ["WAVYS"]
    plain = line_of(tracked([("Table", 0), ("of", 0.33), ("contents", 0.33), ("s", 0.1)], font="DejaVuSans", track=0.0),
                    size=10)
    assert texts(plain, {}) == ["Table", "of", "contentss"]
    # math and monospaced glyphs are never read as tracked
    for font in ("CMMI10", "LMMono10-Regular"):
        glyphs = line_of(tracked([("abcde", 0), ("fghij", 0.1)], font=font, track=-0.06), size=10)
        assert extract.tight_tracking(glyphs) == {}
