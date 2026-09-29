"""Bugs in the pure reader's font code that its types revealed, pinned: a TrueType collection's
member read as member 0 when judging trickiness, and a system font FreeType would not open
crashing the font mapper instead of counting as a failed face."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont

from beamer2slides.pdf.pure import fontmapper, truetype
from beamer2slides.pdf.pure.fonts import load_truetype
from beamer2slides.pdf.pure.ftoutline import Unported


def _font(family: str) -> TTFont:
    """A one-glyph TrueType font named `family`."""
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef"])
    fb.setupCharacterMap({})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 500))
    pen.lineTo((400, 500))
    pen.closePath()
    fb.setupGlyf({".notdef": pen.glyph()})
    fb.setupHorizontalMetrics({".notdef": (500, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    return fb.font


def _bytes(font: TTFont | TTCollection) -> bytes:
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def test_a_tricky_member_of_a_collection_is_judged_by_its_own_names():
    """FreeType's tt_check_trickyness reads the family names of the face it opened: member 1 of a
    collection named MingLiU is tricky though member 0 is not. TrueTypeFace read member 0's."""
    collection = TTCollection()
    collection.fonts = [_font("Plain"), _font("MingLiU")]
    data = _bytes(collection)
    plain, tricky = load_truetype(data, 0), load_truetype(data, 1)
    assert plain is not None and tricky is not None
    truetype.TrueTypeFace(plain)
    with pytest.raises(Unported, match="tricky"):
        truetype.TrueTypeFace(tricky)


def test_a_system_font_freetype_will_not_open_is_a_failed_face(tmp_path: Path):
    """An installed font with a name table (so the folder scan lists it) but no head or maxp
    (so FT_Open_Face refuses it): load_truetype answers None, which the mapper records in
    face_failed and substitutes past. It read `platform_data` off the None and crashed."""
    name = _font("Brokenface")["name"].compile(_font("Brokenface"))
    header = (0x00010000).to_bytes(4, "big") + (1).to_bytes(2, "big") + bytes(6)
    entry = b"name" + bytes(4) + (12 + 16).to_bytes(4, "big") + len(name).to_bytes(4, "big")
    (tmp_path / "broken.ttf").write_bytes(header + entry + name)
    info = fontmapper.LinuxFontInfo([str(tmp_path)])
    mapper = fontmapper.FontMapper[fontmapper.FolderFace](info)
    info.enum_font_list(mapper)
    face = info.get_font("Brokenface")
    assert face is not None
    got = mapper.use_external_subst(info, face, "Brokenface", 400, False, 0, fontmapper.CHARSET_ANSI,
                                    fontmapper.SubstFont())
    assert got is None
    assert ("Brokenface", 400, False) in mapper.face_failed
