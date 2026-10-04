"""Word spaces extract finds between glyphs (out/hunt/WAVE3.md, fixer X): a narrow space between
touching glyphs, letterspaced words, an italic accent reaching past its advance, and text a font's
ToUnicode misnames. Synthetic characters, no PDF."""

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import pytest

from beamer2slides import extract
from beamer2slides.arrays import Pixels
from beamer2slides.extract import readable
from beamer2slides.pdf import Char, Document, char_box
from beamer2slides.pdf.api import Box, Drawing, EmbeddedImage, ImageInfo, Link, PageObject
from beamer2slides.raw_types import RawSpan

BOLD = "LMSans10-Bold"
SIZE = 10.0
X0 = 20.0        # where the first piece starts
WIDTH = 0.5      # em, every glyph's advance
BASELINE = 100.0
DECKS = Path(__file__).parent / "decks" / "out"


def glyphs(pieces: list[tuple[str, float]], *, font: str, size: float) -> list[Char]:
    """Characters of (text, gap before it in em) pieces: each glyph `WIDTH` em wide, the pieces'
    glyphs touching, the first glyph of a piece `gap` em after the one before."""
    out: list[Char] = []
    x = X0
    for text, gap in pieces:
        x += gap * size
        for c in text:
            adv = WIDTH * size
            out.append(Char(c=c, font=font, size=size, color=0, alpha=255, origin=(x, BASELINE),
                            box=char_box(x, BASELINE, 1.0, 0.0, adv, size, 0.8, -0.2), dir=(1.0, 0.0), obj=len(out),
                            font_id=0, advance=adv, synthetic=False, ascent=0.8, descent=-0.2, exact_advance=True))
            x += adv
    return out


def unasked(what: str) -> NoReturn:
    raise AssertionError(f"extract.spans was not expected to ask the page for {what}")


class Page:
    """A page of characters only (`PdfPage`), and the fonts' widths for them."""

    index = 0
    width = 400.0
    height = 300.0

    def __init__(self, chars: list[Char], widths: Mapping[str, float]) -> None:
        self._chars, self.widths = chars, widths

    @property
    def rect(self) -> Box:
        return (0.0, 0.0, self.width, self.height)

    def objects(self) -> list[PageObject]:
        return []  # (a font width stands in unstretched: no text object scales its glyphs)

    def object_bounds(self) -> list[Box]:
        unasked("object bounds")

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        unasked("set_active")

    def chars(self) -> list[Char]:
        return self._chars

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        return [self.widths.get(c) for _, c, _ in requests]

    def drawings(self) -> list[Drawing]:
        unasked("drawings")

    def images(self) -> list[ImageInfo]:
        unasked("images")

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        unasked("an embedded image")

    def links(self) -> list[Link]:
        unasked("links")

    def render(self, zoom: float, clip: Box | None, transparent: bool) -> Pixels:
        # (the defaults are `PdfPage.render`'s: the Protocol dictates them)
        unasked("a render")


class Shown:
    def hidden(self, ch: Char) -> bool:
        return False


def texts(chars: list[Char], widths: Mapping[str, float]) -> list[str]:
    return [s.text for s in extract.spans(Page(chars, widths), Shown(), False, chars, {})]


def test_a_narrow_space_between_touching_glyphs_is_a_space() -> None:
    # design v1 s2: '18 mo' at 26 pt in LMSans10-Bold, the space 0.14 em (below JOIN_GAP)
    assert texts(glyphs([("18", 0), ("mo", 0.14)], font=BOLD, size=SIZE), {}) == ["18 mo"]
    # polyglossia's French thin space after « (0.124 em, Palatino italic)
    assert texts(glyphs([("«", 0), ("Le", 0.124)], font="PalatinoLinotype-Italic", size=SIZE), {}) == ["« Le"]
    # a kern or italic correction stays inside the word, and math is left alone
    assert texts(glyphs([("Ta", 0), ("ble", 0.09)], font=BOLD, size=SIZE), {}) == ["Table"]
    assert texts(glyphs([("M", 0), ("x", 0.109)], font="CMMI10", size=SIZE), {}) == ["Mx"]


def test_tracked_small_caps_take_no_space_at_a_kern() -> None:
    # textfx v2 s2: \textsc 'Results' tracked by 0.11 em, 't' and 's' kerned closer (0.044):
    # the 's' after it is no word
    chars = glyphs([("R", 0), ("e", 0.11), ("s", 0.11), ("u", 0.11), ("l", 0.11), ("t", 0.044), ("s", 0.111)],
                   font="LMRomanCaps10-Regular", size=SIZE)
    assert texts(chars, {}) == ["Results"]


def test_letterspaced_words_keep_their_word_gaps() -> None:
    # textfx v3 s5: soul's \so{less is more}, letters 0.25 em apart (a kern 0.222), words 0.65:
    # Slides has no letter spacing, so a no-break space (Lato 0.192 em) stands between letters
    # and before each word gap (wave 3 had joined them tight: 'less is more')
    so = glyphs([(":", 0), ("l", 0.55), ("e", 0.25), ("s", 0.25), ("s", 0.25), ("i", 0.65), ("s", 0.25),
                 ("m", 0.65), ("o", 0.25), ("r", 0.222), ("e", 0.25)], font="LMSans10-Regular", size=SIZE)
    assert " ".join(texts(so, {})) == ":\u00a0 l\u00a0e\u00a0s\u00a0s\u00a0 i\u00a0s\u00a0 m\u00a0o\u00a0r\u00a0e"
    # textfx v1 s8: \textls[200]{SPACED} among ordinary words, P-A kerned to 0.116; the PDF line
    # is 361.8 Slides pt, with the spaces 362.2, tight 339.3
    line = glyphs([("The", 0), ("word", 0.33), ("S", 0.5), ("P", 0.2), ("A", 0.116), ("C", 0.173), ("E", 0.2),
                   ("D", 0.2), ("is", 0.5), ("letterspaced", 0.33)], font="LMSans10-Regular", size=SIZE)
    assert " ".join(texts(line, {})) == "The word\u00a0 S\u00a0P\u00a0A\u00a0C\u00a0E\u00a0D\u00a0 is letterspaced"
    # textfx v2 s1: a title tracked by 0.147 em (READABLE SLIDES: PDF 236.8 pt, spaced 235.2, tight 181.5)
    title = glyphs([("R", 0), ("E", 0.147), ("A", 0.147), ("D", 0.147), ("S", 0.52), ("L", 0.147), ("I", 0.147)],
                   font=BOLD, size=SIZE)
    assert " ".join(texts(title, {})) == "R\u00a0E\u00a0A\u00a0D\u00a0 S\u00a0L\u00a0I"
    # before wave 3: 'S PA C E D' and 'l e s s i s m o r e'


def test_ordinary_single_letter_words_are_no_letterspacing() -> None:
    # CM's word space (0.333 em) is past any tracking: 'a b c d e' stays five words
    assert " ".join(texts(glyphs([("a", 0), ("b", 0.333), ("c", 0.333), ("d", 0.333), ("e", 0.333)],
                                 font="LMSans10-Regular", size=SIZE), {})) == "a b c d e"


def test_an_italic_accent_past_its_advance_leaves_the_word_space() -> None:
    # lang v2 s5: Calibri Italic 'ì' reported 3.51 pt wide (its grave accent's ink), the font
    # says 2.5: the word space after it read 0.133 em and 'yì yuè' became 'yìyuè'
    chars = glyphs([("y", 0), ("ì", 0), ("yu", 0.226)], font="Calibri-Italic", size=10.91)
    wide = chars[1].advance
    chars[1].advance = 3.51
    chars[1].box = char_box(*chars[1].origin, 1.0, 0.0, 3.51, 10.91, 0.8, -0.2)
    for ch in chars[2:]:  # the PDF's glyphs stand where the font's own width puts them
        ch.origin = (ch.origin[0] - (wide - 2.5), ch.origin[1])
        ch.box = char_box(ch.origin[0], ch.origin[1], 1.0, 0.0, ch.advance, ch.size, 0.8, -0.2)
    assert texts(chars, {"ì": 2.5}) == ["yì yu"]
    assert extract._accent_overhang(Page(chars, {"ì": 2.5}), chars)[1].advance == 2.5
    # an upright letter, or one whose width the font agrees with, keeps its advance
    assert extract._accent_overhang(Page(chars, {"ì": 3.5}), chars)[1].advance == 3.51
    upright = [dataclasses.replace(ch, font="Calibri") for ch in chars]
    assert extract._accent_overhang(Page(upright, {"ì": 2.5}), upright)[1].advance == 3.51


def line_of(pieces: list[tuple[str, str, float, float]], *, size: float) -> list[Char]:
    """Characters of (character, font, advance em, pen gap em before it) on one baseline."""
    out: list[Char] = []
    x = X0
    for c, font, advance, gap in pieces:
        x += gap * size
        adv = advance * size
        out.append(Char(c=c, font=font, size=size, color=0, alpha=255, origin=(x, BASELINE),
                        box=char_box(x, BASELINE, 1.0, 0.0, adv, size, 0.8, -0.2), dir=(1.0, 0.0), obj=len(out),
                        font_id=0, advance=adv, synthetic=False, ascent=0.8, descent=-0.2, exact_advance=False))
        x += adv
    return out


def test_a_math_italic_correction_is_no_space() -> None:
    # 29_tikz_diagrams p17, $h_t = f(h_{t-1}, x_t)$ in beamer's sans math: CMSSI10's f (advance
    # 0.305 em at 10.95 pt) and the '(' 0.217 em after it, f's TFM italic correction (0.21705):
    # it read 'f (h' in Slides
    f_paren = line_of([("f", "CMSSI10", 0.305, 0), ("(", "CMSS10", 0.389, 0.217), ("h", "CMSSI10", 0.5, 0)], size=10.95)
    assert texts(f_paren, {}) == ["f", "(", "h"]  # (before: "f", " (", "h")
    f = next(s for s in extract.spans(Page(f_paren, {}), Shown(), False, f_paren, {}) if s.text == "f")
    assert abs(f.bbox[2] - f_paren[1].origin[0]) < 1e-9  # TeX's box of f reaches the '('
    # CMMI's V (0.222 em, a medium space's width), lmodern's name for it, pdflatex's bitmap ECSI
    assert texts(line_of([("V", "CMMI10", 0.583, 0), ("(", "CMR10", 0.389, 0.222)], size=10), {}) == ["V", "("]
    assert extract.italic_correction("ABCDEF+LMMathItalic10-Regular", "V") == 0.222
    assert extract.italic_correction("ECSI1095", "f") == 0.224
    assert extract.italic_correction("CMSSI10", "a") is None


def test_a_space_after_an_italic_letter_stays() -> None:
    # 02_math p2, 28_display_math p6: TeX's thick and medium spaces after CMSSI10's a and b come on
    # top of their corrections (0.010 + 0.278, 0.031 + 0.222)
    assert texts(line_of([("a", "CMSSI10", 0.48, 0), ("=", "CMSS10", 0.778, 0.287)], size=10.95), {}) == ["a", " ="]
    assert texts(line_of([("b", "CMSSI10", 0.516, 0), ("+", "CMSS10", 0.778, 0.253)], size=10.95), {}) == ["b", " +"]
    # 28_line_breaks p2: a shrunk word space after an oblique f (0.267 em; its correction 0.218)
    word = line_of([("o", "LMSans9-Oblique", 0.5, 0), ("f", "LMSans9-Oblique", 0.314, 0),
                    ("p", "LMSans9-Oblique", 0.53, 0.267)], size=9)
    assert texts(word, {}) == ["of p"]
    # a thin space after the correction ($f\,(x)$: 0.384 em) is a word gap, the advance its own
    thin = line_of([("f", "CMSSI10", 0.305, 0), ("(", "CMSS10", 0.389, 0.384)], size=10.95)
    assert extract._italic_corrections(thin)[0].advance == thin[0].advance


@pytest.mark.needs_decks("out/29_tikz_diagrams.pdf")
def test_the_tikz_deck_reads_f_of_h() -> None:
    doc = Document(DECKS / "29_tikz_diagrams.pdf")
    try:
        text = "".join(s.text for s in extract.shown_spans(doc[16]))
    finally:
        doc.close()
    assert "f(h" in text and "f (" not in text


@pytest.mark.needs_decks("out/29_tikz_diagrams.pdf")
def test_a_turned_word_is_no_small_caps() -> None:
    """rotate=20 'Tilted': a turned glyph's advance is read off the upright box around it, so
    every letter looked like an alternate glyph and Slides wrote TILTED."""
    doc = Document(DECKS / "29_tikz_diagrams.pdf")
    try:
        page = extract.extract_page(doc[12], "13")
    finally:
        doc.close()
    [tilted] = [s for s in page["spans"] if s["text"].strip() == "Tilted"]
    assert not tilted["smallcaps"]


def test_misnamed_characters_read_as_their_words() -> None:
    # design v3: Calibri's U+2010 HYPHEN, which the Google substitutes lack
    assert readable("state‐of‐the‐art", "Calibri") == "state-of-the-art"
    # lang v1 s2-4: old-style small-cap figures whose ToUnicode says 'inferior'
    assert readable("DU SUBLIME ₍₁₆₇₄₎", "PalatinoLinotype-Italic") == "DU SUBLIME (1674)"
    assert readable("§₂₅", "PalatinoLinotype-Roman") == "§25"
    assert readable("ROBERTS,₁₈₉₉", "PalatinoLinotype-Roman") == "ROBERTS,1899"
    # a letter's subscript stays one, and so does math's
    assert readable("CO₂ and x₁", "Calibri") == "CO₂ and x₁"
    assert readable("₁₂", "CMMI10") == "₁₂"
    assert readable("ﬁrst", "Calibri") == "first"


def raw_span(i: int, text: str, font: str, x0: float, x1: float, *, size: float) -> RawSpan:
    """A span of raw.json on the baseline at 100 pt."""
    baseline = 100.0
    return {"id": f"s{i}", "text": text, "font": font, "size": size, "color": "#000000", "alpha": 255,
            "origin": [x0, baseline], "bbox": [x0, baseline - 0.75 * size, x1, baseline + 0.25 * size],
            "dir": [1.0, 0.0], "smallcaps": False}


def test_a_quad_before_a_graphic_in_the_words_stays_wide() -> None:
    # textfx v3 s9: 'Questions? \quad \ding{46}\,e-mail' - the quad before the dingbat's hole
    # became one space and the centred line came out narrower
    from beamer2slides.classify import PageClassifier, new_line, new_paragraph

    def texts_around(gap: float) -> list[str]:
        spans = [raw_span(0, "Questions?", "LMSans10-Bold", 10, 62, size=14),
                 raw_span(1, "\u270e", "PZDR", 62 + gap, 74 + gap, size=14),
                 raw_span(2, "elise.martin", "LMSans10-Bold", 76 + gap, 150 + gap, size=14)]
        page = PageClassifier({"index": 0, "label": "", "size": [364, 273], "spans": spans, "images": [],
                               "drawings": [], "links": []}, 10)
        line = new_line(page.spans())
        line.holes = [[s for s in line.spans if s.font == "PZDR"]]
        return [r["text"] for r in PageClassifier.runs(new_paragraph([line], align="left", reason=None), "", False, None)]

    assert texts_around(18.6)[0] == "Questions? \u2003"   # a word space and a quad (1.33 em)
    assert texts_around(4.6)[0] == "Questions? "          # a word space



def test_a_word_stacked_under_the_last_starts_a_line() -> None:
    # third-year-talk p19: 'THE' \\[-x] 'END', the pen 0.42 em back and 0.75 em down (under
    # NEW_BASELINE, short of BACK_GAP): one span read 'THEEND' and 'END' stayed in the background
    the, end = glyphs([("THE", 0)], font="Helvetica-Bold", size=SIZE), glyphs([("END", 0)], font="Helvetica-Bold", size=SIZE)
    shift = (the[-1].origin[0] + the[-1].advance - SIZE * 0.42 - end[0].origin[0], 0.75 * SIZE)
    end = [dataclasses.replace(ch, origin=(ch.origin[0] + shift[0], ch.origin[1] + shift[1]), obj=3 + i,
                               box=char_box(ch.origin[0] + shift[0], ch.origin[1] + shift[1], 1.0, 0.0,
                                            ch.advance, SIZE, 0.8, -0.2))
           for i, ch in enumerate(end)]
    assert texts(the + end, {}) == ["THE", "END"]  # (before: ["THEEND"])


def test_text_scaled_across_is_no_small_caps() -> None:
    # third-year-talk p5/p10/p14: words of an included figure stretched 0.95x/1.162x/1.285x: every
    # letter off the font's advance by one factor, and Slides wrote LIKELIHOOD
    word = glyphs([("likelihood", 0)], font="Helvetica", size=SIZE)
    widths = {c: 0.5 * SIZE / 0.95 for c in "likelihood"}
    assert not extract._small_caps(Page(word, widths), word)
    # true small caps: each letter's own advance, not one factor
    sc = {c: 0.5 * SIZE * f for c, f in zip("likehod", (1.4, 1.5, 1.3, 1.2, 1.25, 1.12, 1.33))}
    assert extract._small_caps(Page(word, sc), word)


def stretched_pdf(path: Path, across: float) -> None:
    """A one-page PDF of Helvetica words drawn `across` times wider than tall (an included
    figure scaled across), as pdfTeX writes them: the pair kerns of 'Attach' and 'There' in TJ."""
    content = (f"BT /F1 10 Tf {across} 0 0 1 20 200 Tm [(A) 30 (ttach parent)] TJ "
               f"{across} 0 0 1 20 180 Tm [(T) 18 (here is always)] TJ ET").encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 250] /Contents 4 0 R"
               b" /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(out)


@pytest.mark.parametrize("across", [2.0, 1.285, 0.95, 1.0])
def test_a_kerned_capital_in_words_scaled_across_takes_no_space(tmp_path: Path, across: float) -> None:
    """third-year-talk p11: a figure's captions stretched 1.285x read 'A ttach parent', 'T here is
    always'. The capital's ink reaches its advance, so the backend takes the font's width for it,
    which was the width at the size drawn (the square root of the matrix's determinant) and not\n    stretched across: 0.18 em short of the next letter there, 0.23 em here at 2x."""
    pdf = tmp_path / "stretched.pdf"
    stretched_pdf(pdf, across)
    doc = Document(pdf)
    try:
        chars = doc[0].chars()
        text = "".join(s.text for s in extract.shown_spans(doc[0]))
    finally:
        doc.close()
    for a, b in zip(chars, chars[1:]):
        if a.c in "AT" and b.c in "th":
            assert abs(a.origin[0] + a.advance - b.origin[0]) < 0.1 * a.size, (a, b)
    assert "Attach" in text and "There" in text, text
