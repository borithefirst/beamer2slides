"""Word spaces extract finds between glyphs (out/hunt/WAVE3.md, fixer X): a narrow space between
touching glyphs, letterspaced words, an italic accent reaching past its advance, and text a font's
ToUnicode misnames. Synthetic characters, no PDF."""

from beamer2slides import extract
from beamer2slides.extract import readable
from beamer2slides.pdf import Char, char_box


def glyphs(pieces: list[tuple[str, float]], font: str = "LMSans10-Bold", size: float = 10.0, x: float = 20.0,
           width: float = 0.5, baseline: float = 100.0) -> list[Char]:
    """Characters of (text, gap before it in em) pieces: each glyph `width` em wide, the pieces'
    glyphs touching, the first glyph of a piece `gap` em after the one before."""
    out = []
    for text, gap in pieces:
        x += gap * size
        for c in text:
            adv = width * size
            out.append(Char(c, font, size, 0, 255, (x, baseline), char_box(x, baseline, 1.0, 0.0, adv, size, 0.8, -0.2),
                            (1.0, 0.0), len(out), 0, adv))
            x += adv
    return out


class Page:
    rect = (0, 0, 400, 300)

    def __init__(self, chars, widths=None):
        self._chars, self.widths = chars, widths or {}

    def chars(self):
        return self._chars

    def glyph_widths(self, requests):
        return [self.widths.get(c) for _, c, _ in requests]


class Shown:
    def hidden(self, ch):
        return False


def texts(chars, widths=None) -> list[str]:
    return [s["text"] for s in extract.spans(Page(chars, widths), Shown(), chars=chars)]


def test_a_narrow_space_between_touching_glyphs_is_a_space():
    # design v1 s2: '18 mo' at 26 pt in LMSans10-Bold, the space 0.14 em (below JOIN_GAP)
    assert texts(glyphs([("18", 0), ("mo", 0.14)])) == ["18 mo"]
    # polyglossia's French thin space after « (0.124 em, Palatino italic)
    assert texts(glyphs([("«", 0), ("Le", 0.124)], font="PalatinoLinotype-Italic")) == ["« Le"]
    # a kern or italic correction stays inside the word, and math is left alone
    assert texts(glyphs([("Ta", 0), ("ble", 0.09)])) == ["Table"]
    assert texts(glyphs([("M", 0), ("x", 0.109)], font="CMMI10")) == ["Mx"]


def test_tracked_small_caps_take_no_space_at_a_kern():
    # textfx v2 s2: \textsc 'Results' tracked by 0.11 em, 't' and 's' kerned closer (0.044):
    # the 's' after it is no word
    chars = glyphs([("R", 0), ("e", 0.11), ("s", 0.11), ("u", 0.11), ("l", 0.11), ("t", 0.044), ("s", 0.111)],
                   font="LMRomanCaps10-Regular")
    assert texts(chars) == ["Results"]


def test_letterspaced_words_keep_their_word_gaps():
    # textfx v3 s5: soul's \so{less is more}, letters 0.25 em apart (a kern 0.222), words 0.65
    so = glyphs([(":", 0), ("l", 0.55), ("e", 0.25), ("s", 0.25), ("s", 0.25), ("i", 0.65), ("s", 0.25),
                 ("m", 0.65), ("o", 0.25), ("r", 0.222), ("e", 0.25)], font="LMSans10-Regular")
    assert " ".join(texts(so)) == ": less is more"
    # textfx v1 s8: \textls[200]{SPACED} among ordinary words, P-A kerned to 0.116
    line = glyphs([("The", 0), ("word", 0.33), ("S", 0.5), ("P", 0.2), ("A", 0.116), ("C", 0.173), ("E", 0.2),
                   ("D", 0.2), ("is", 0.5), ("letterspaced", 0.33)], font="LMSans10-Regular")
    assert " ".join(texts(line)) == "The word SPACED is letterspaced"
    # before: 'S PA C E D' and 'l e s s i s m o r e'


def test_ordinary_single_letter_words_are_no_letterspacing():
    # CM's word space (0.333 em) is past any tracking: 'a b c d e' stays five words
    assert " ".join(texts(glyphs([("a", 0), ("b", 0.333), ("c", 0.333), ("d", 0.333), ("e", 0.333)],
                                 font="LMSans10-Regular"))) == "a b c d e"


def test_an_italic_accent_past_its_advance_leaves_the_word_space():
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
    upright = [Char(**{**ch.__dict__, "font": "Calibri"}) for ch in chars]
    assert extract._accent_overhang(Page(upright, {"ì": 2.5}), upright)[1].advance == 3.51


def test_misnamed_characters_read_as_their_words():
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


def test_a_quad_before_a_graphic_in_the_words_stays_wide():
    # textfx v3 s9: 'Questions? \quad \ding{46}\,e-mail' - the quad before the dingbat's hole
    # became one space and the centred line came out narrower
    from beamer2slides.classify import Line, Paragraph, PageClassifier

    from .test_fonts_encodings import raw_span

    def texts_around(gap):
        spans = [raw_span(0, "Questions?", "LMSans10-Bold", 10, 62, size=14),
                 raw_span(1, "\u270e", "PZDR", 62 + gap, 74 + gap, size=14),
                 raw_span(2, "elise.martin", "LMSans10-Bold", 76 + gap, 150 + gap, size=14)]
        line = Line(PageClassifier({"size": [364, 273], "spans": spans, "links": []}, 10).spans())
        line.holes = [[s for s in line.spans if s.font == "PZDR"]]
        return [r["text"] for r in PageClassifier.runs(Paragraph([line]))]

    assert texts_around(18.6)[0] == "Questions? \u2003"   # a word space and a quad (1.33 em)
    assert texts_around(4.6)[0] == "Questions? "          # a word space
