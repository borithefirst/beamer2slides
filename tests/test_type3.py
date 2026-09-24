"""Bitmap (Type 3) TeX text fonts (docs/project-notes.md "Visual hunt", type3 family): pdflatex
without cm-super embeds EC/LH/TC fonts as Type 3 fonts PDFium reads as raw codes in one font named
"Type3" - T1's fi ligature U+001C and en dash U+0015 dropped or shifting every later style, ś as
±, T2A Cyrillic as Latin-1 mojibake, the TS1 euro as ¿, and bold, italic, sans and typewriter all
one serif. `type3` names each font from its glyphs' advances (the TFM widths) and reads its codes
through its encoding. Synthetic characters set at the fonts' own widths, no PDF."""

import pytest

from beamer2slides import extract, type3
from beamer2slides.fonts import font_info
from beamer2slides.pdf import NO_OBJECT, Char, char_box

X0, Y = 30.0, 100.0
WORD_SPACE = 3.6


def setline(words: list[str], font: str, size: float, font_id: int, y: float = Y, x: float = X0,
            lean: float = 0.0, direction=(1.0, 0.0)) -> list[Char]:
    """Words of character codes (a str of code points 0-255) set in the TeX font `font` as a Type 3
    font: the pen moves by each glyph's TFM width, PDFium's box is that wide (`lean`: this much
    wider, as a slanted glyph's ink reaches out)."""
    widths = type3.fonts()[font].widths
    ux, uy = direction
    out = []
    for word in words:
        for c in word:
            w = widths[ord(c)] * size
            box_w = w * (1 + lean) if c.isalpha() else w
            if ux < 0.999:  # a rotated glyph's box is its ink's upright bounds, not its advance
                box_w = w * abs(ux) + size * 0.7 * abs(uy)
            out.append(Char(c, "Type3", size, 0, 255, (x, y), char_box(x, y, ux, uy, box_w, size, 0.8, -0.2),
                            direction, 1, font_id, box_w))
            x, y = x + w * ux, y + w * uy
        x, y = x + WORD_SPACE * ux, y + WORD_SPACE * uy
    return out


def read(chars: list[Char]) -> tuple[dict, list[Char]]:
    found = type3.page_fonts(chars)
    return found, type3.decode(chars, found)


def test_the_encoding_tables_are_latex_own():
    assert type3.encoding("T1")[0x1C] == "ﬁ" and type3.encoding("T1")[0x15] == "–"
    assert type3.encoding("T1")[0xB1] == "ś" and type3.encoding("T1")[0xAE] == "ő"
    assert type3.encoding("TS1")[0xBF] == "€" and type3.encoding("TS1")[0x88] == "•"
    assert type3.encoding("T2A")[0xC6] == "Ж" and type3.encoding("T2A")[0x16] == "—"
    assert type3.encoding("T2A")[ord("I")] == "I"  # T2A's І is the Latin I glyph


def test_a_bold_t1_title_keeps_its_ligature_dash_and_weight():
    chars = setline(["Cook\x15Levin:", "de\x1Cnitions", "and", "e\x1Eciency"], "ecbx1095", 10.95, 0)
    found, decoded = read(chars)
    assert found[0].name == "ecbx1095"
    assert "".join(ch.c for ch in decoded) == "Cook–Levin:deﬁnitionsandeﬃciency"
    assert {ch.font for ch in decoded} == {"ECBX1095"}
    info = font_info("ECBX1095")
    assert (info.family, info.bold, info.italic, info.design_size) == ("serif", True, False, 10.95)


def test_body_and_bold_are_two_fonts_and_split_the_spans():
    """The hunt's 'bold lost' and 'styles shift': one "Type3" font for all, no split at the bold
    word; a dropped U+0015 moved every later style one character."""
    body = setline(["Wi\xB1niewska", "proved", "the", "\x1Crst"], "ecrm1095", 10.95, 0)
    bold = setline(["claim", "\x15", "twice"], "ecbx1095", 10.95, 1, x=body[-1].origin[0] + 10)

    class Page:
        rect = (0, 0, 400, 300)

        def chars(self):
            return body + bold

    class Shown:
        def hidden(self, ch):
            return False

    chars, decoded = extract.page_chars(Page())
    assert decoded == {0, 1}
    spans = extract.spans(Page(), Shown(), chars=chars)
    assert [(s["text"], s["font"]) for s in spans] == [
        ("Wiśniewska", "ECRM1095"), ("proved", "ECRM1095"), ("the", "ECRM1095"), ("ﬁrst", "ECRM1095"),
        ("claim", "ECBX1095"), ("–", "ECBX1095"), ("twice", "ECBX1095")]


def test_slanted_glyphs_are_the_slanted_twin():
    """ecsi has ecss's widths; its letters' ink leans past their advances."""
    upright, _ = read(setline(["Mean", "earnings"], "ecss1095", 10.95, 0))
    slanted, decoded = read(setline(["Mean", "earnings"], "ecss1095", 10.95, 0, lean=0.12))
    assert upright[0].name == "ecss1095" and slanted[0].name == "ecsi1095"
    assert font_info(decoded[0].font).italic and font_info(decoded[0].font).family == "sans"


def test_small_caps_and_typewriter_are_their_own_faces():
    found, decoded = read(setline(["Sat", "Circuit-Sat"], "eccc1095", 10.95, 0)
                          + setline(["git", "switch", "-c", "feature"], "ectt1095", 10.95, 1, y=120))
    assert (found[0].name, found[1].name) == ("eccc1095", "ectt1095")
    assert font_info("ECCC1095").smallcaps and font_info("ECTT1095").family == "mono"


def test_digits_every_face_fits_take_the_page_face():
    """Digits are as wide in every face: a line number takes the family of the page's words."""
    words = setline(["Querying", "the", "orders", "table"], "ecsx1200", 11.96, 0)
    numbers = setline(["1", "2", "3", "4", "5", "6", "7", "8"], "ecss0800", 7.97, 1, y=140)
    found, _ = read(words + numbers)
    assert found[0].name == "ecsx1200"
    assert found[1].name[2:4] == "ss" and found[1].name.endswith("0800")


def test_a_lone_euro_is_the_text_companion_symbol():
    euro = setline(["\xBF", "\xBF"], "tcss1095", 10.95, 5)
    found, decoded = read(euro)
    assert found[5].encoding == "TS1" and [ch.c for ch in decoded] == ["€", "€"]


def test_a_lone_micro_sign_is_no_t1_letter():
    """sci v3 (r8t3): textgreek's \\textmu, alone in its Type 3 font at 0xB5, is exactly as wide as
    ectt's 0xB5 (ţ) and tctt's (µ); T1 came first and 'µs-scale' read 'ţs-scale'. A font of codes
    above 0x7F only is no T1 text font (T1's accents are in their words' font)."""
    body = setline(["Lifetime"], "ecss1095", 10.91, 0)
    found, decoded = read(body + setline(["\xB5"], "ectt1095", 10.91, 9, x=73.07))
    assert found[9].encoding == "TS1" and decoded[-1].c == "µ"
    # a T1 accented letter among its word's letters is still T1
    found, decoded = read(setline(["Wi\xB1niewska"], "ecrm1095", 10.95, 0))
    assert found[0].encoding == "T1" and "".join(ch.c for ch in decoded) == "Wiśniewska"


def test_cyrillic_t2a_is_read_as_cyrillic():
    found, decoded = read(setline(["\xCF\xF0\xE8\xEC\xE5\xF0", "\xE6\xBC\xF1\xF2\xEA\xEE\xE9",
                                   "\xE7\xE0\xE4\xE0\xF7\xE8", "\x16"], "lass1095", 10.95, 0))
    assert found[0].encoding == "T2A"
    assert "".join(ch.c for ch in decoded) == "Примержёсткойзадачи—"
    assert font_info(decoded[0].font).family == "sans"


def test_cyrillic_of_a_size_without_widths_is_read_through_its_latin_letters():
    """LH fonts are generated on demand, so the table lacks most sizes: the Latin letters are EC's."""
    fonts = type3.fonts()
    ec = next(n for n in sorted(fonts) if n.startswith("ecss") and "la" + n[2:] not in fonts)
    size = fonts[ec].size
    latin = setline(["Newton"], ec, size, 0)
    cyrillic = setline(["\xEC\xE5\xF2\xEE\xE4"], "lass1095", size, 0, x=latin[-1].box[2] + WORD_SPACE)
    found, decoded = read(latin + cyrillic + setline(["Runge-Kutta"], ec, size, 0, x=cyrillic[-1].box[2] + WORD_SPACE))
    assert found[0] == type3.PageFont("la" + ec[2:], "T2A")
    assert "".join(ch.c for ch in decoded) == "NewtonметодRunge-Kutta"


def test_rotated_chart_labels_are_read_from_their_pen_moves():
    found, _ = read(setline(["Restaurants", "Retail", "Manufacturing"], "ecss0800", 7.97, 0,
                            direction=(0.9397, -0.342)))
    assert found[0].name == "ecss0800"


def test_code_zero_is_the_grave_accent_and_the_compound_mark_goes():
    chars = setline(["cr\x00eme", "shelf\x17ful"], "ecrm1095", 10.95, 0)
    chars[2].c = "�"  # PDFium gives code 0 as U+FFFD
    found, decoded = read(chars)
    assert "".join(ch.c for ch in decoded) == "cr`emeshelfful"


def test_an_unknown_font_stays_type3():
    chars = setline(["abcdef"], "ecrm1095", 10.95, 0)
    for i, ch in enumerate(chars):  # widths no TeX font has
        ch.advance = ch.size * (0.31 + 0.07 * i)
        ch.box = char_box(*ch.origin, 1, 0, ch.advance, ch.size, 0.8, -0.2)
        ch.origin = (X0 + sum(c.advance for c in chars[:i]), Y)
    found, decoded = read(chars)
    assert found == {} and {ch.font for ch in decoded} == {"Type3"}


@pytest.mark.parametrize("name, family, bold, italic, smallcaps", [
    ("ECRM1095", "serif", False, False, False), ("ECTI1095", "serif", False, True, False),
    ("ECBL1095", "serif", True, True, False), ("ECSC1095", "serif", False, True, True),
    ("ECSO1095", "sans", True, True, False), ("ECST1095", "mono", False, True, False),
    ("LASX1095", "sans", True, False, False), ("TCSS1095", "sans", False, False, False),
])
def test_tex_bitmap_font_names_say_their_face(name, family, bold, italic, smallcaps):
    info = font_info(name)
    assert (info.family, info.bold, info.italic, info.smallcaps) == (family, bold, italic, smallcaps)
