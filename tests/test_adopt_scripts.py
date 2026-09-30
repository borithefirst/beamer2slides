"""Text in scripts other than Latin in adopted sources (scripts.py, and what deck_ir reads for it).

Offline, no TeX and no Google: hand-built `presentations.get` answers with Hebrew, Arabic and
Japanese text, the IR that comes back (direction, visual alignment), the LaTeX written for right-to-left
paragraphs, and the preamble that gives TeX fonts for every script - chosen from tiny fonts made
here, so the answer does not depend on what this machine has installed.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from beamer2slides import adopt, scripts
from beamer2slides.adopt_context import MissingFont
from beamer2slides.json_types import Json, JsonObject
from .irs import deck_ir
from beamer2slides.deck_ir_types import parse_target
from beamer2slides.inverse import (BEAMER_PT, LoopParagraph, LoopRun, TextStyle, fresh_context, loop_paragraph,
                                   loop_run, paragraphs_latex, runs_latex)

from .deck_records import target_filled
from .json_reads import jnum, jnums, jobj, jobjs
from .test_adopt import at, pt

# what a paragraph's base style says: 18 pt and nothing else, or nothing at all
BASE18 = TextStyle(size=18.0, color=None, family=None, bold=False, italic=False, font=None, weight=None)
NO_BASE = TextStyle(size=None, color=None, family=None, bold=False, italic=False, font=None, weight=None)


def base18(_p: object) -> TextStyle:
    return BASE18


def paras(*ps: JsonObject) -> list[LoopParagraph]:
    return [loop_paragraph(p, "test") for p in ps]


def loop_runs(*rs: JsonObject) -> list[LoopRun]:
    return [loop_run(r, "test") for r in rs]

EMU = 12700


def no_family(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
    """google/fonts has no such family."""
    return None


def never_fetched(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
    pytest.fail(f"fetched {name}")


def fetching() -> bool:
    return True


@pytest.fixture(autouse=True)
def font_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fonts come from a folder of this test's own (`$B2S_FONTS`), empty unless a test fills it."""
    folder = tmp_path / "fonts"
    folder.mkdir()
    monkeypatch.setenv("B2S_FONTS", str(folder))
    monkeypatch.delenv("B2S_NO_SCRIPTS", raising=False)
    return folder


def make_font(folder: Path, family: str, chars: str) -> Path:
    """A font named `family` whose cmap has exactly `chars` (and a space), its Regular."""
    return make_face(folder, family, chars, False)


def make_face(folder: Path, family: str, chars: str, bold: bool) -> Path:
    """`make_font`, its Bold when `bold`."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    names = [".notdef", "space"] + [f"g{ord(c):X}" for c in chars]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({32: "space", **{ord(c): f"g{ord(c):X}" for c in chars}})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)); pen.lineTo((500, 0)); pen.lineTo((500, 500)); pen.closePath()
    box = pen.glyph()
    fb.setupGlyf({n: box for n in names})
    fb.setupHorizontalMetrics({n: (600, 0) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    style = "Bold" if bold else "Regular"
    fb.setupNameTable({"familyName": family, "styleName": style})
    fb.setupOS2(usWeightClass=700 if bold else 400)
    fb.setupPost()
    path = folder / f"{family.replace(' ', '')}-{style}.ttf"
    fb.save(path)
    scripts._FACES.clear()
    return path


def paragraph(words: str, *, direction: str | None, align: str | None, font: str) -> list[Json]:
    style: JsonObject = {}
    if direction:
        style["direction"] = direction
    if align:
        style["alignment"] = align
    return [{"paragraphMarker": {"style": style}},
            {"textRun": {"content": words + "\n", "style": {"fontFamily": font, "fontSize": pt(18)}}}]


def plain(words: str, font: str) -> list[Json]:
    """A paragraph of `words` in `font`, saying nothing of its direction or alignment."""
    return paragraph(words, direction=None, align=None, font=font)


def deck(*boxes: list[Json]) -> JsonObject:
    elements: list[Json] = [{"objectId": f"t{i}", "size": {"width": pt(400), "height": pt(100)},
                             "transform": at(40, 40 + 110 * i),
                             "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": b}}}
                            for i, b in enumerate(boxes)]
    return {"presentationId": "p", "pageSize": {"width": pt(720), "height": pt(405)},
            "slides": [{"objectId": "s1", "pageElements": elements}], "layouts": [], "masters": []}


def texts(target: JsonObject) -> list[JsonObject]:
    return [e for s in jobjs(target, "slides") for e in jobjs(s, "elements") if e["kind"] == "text"]


# ------------------------------------------------------------------------------------------- the IR

def test_a_right_to_left_paragraph_says_so_and_sits_where_the_deck_has_it():
    """START in a right-to-left paragraph is flush right: `align` says where the lines sit."""
    target = deck(paragraph("שלום עולם", direction="RIGHT_TO_LEFT", align=None, font="Arial"),
                  paragraph("مرحبا", direction="RIGHT_TO_LEFT", align="END", font="Arial"),
                  paragraph("Hello", direction="LEFT_TO_RIGHT", align=None, font="Arial"))
    t = jobj(deck_ir(target, foreign=True))
    first, second, third = (jobj(box, "paragraphs", 0) for box in texts(t))
    assert first["direction"] == "rtl" and first["align"] == "right"
    assert second["align"] == "left"
    assert "direction" not in third and third["align"] == "left"
    # the anchor follows the visual alignment: the right edge, less the padding
    box = texts(t)[0]
    x0, _, x1, _ = jnums(box, "bbox")
    assert jnum(box, "anchor", 0) > (x0 + x1) / 2


# ----------------------------------------------------------------------------------------- the text

def rtl(words: str, align: str, bullet: bool) -> JsonObject:
    return {"runs": [{"text": words, "size": 18.0}], "align": align, "direction": "rtl",
            "bullet": {"kind": "glyph"} if bullet else None, "level": 0}


def ltr(words: str, align: str) -> JsonObject:
    return {"runs": [{"text": words, "size": 18.0}], "align": align, "bullet": None, "level": 0}


def test_a_right_to_left_text_is_set_in_its_language_with_logical_alignment():
    """LuaTeX's skips are logical: in an RTL paragraph `\\raggedright` is flush right."""
    out = paragraphs_latex(paras(rtl("مرحبا", "right", False), rtl("يسار", "left", False),
                                 rtl("وسط", "center", False)),
                           base18, fresh_context(BEAMER_PT), "  ")
    assert out.startswith("  \\begin{otherlanguage}{arabic}\n") and out.endswith("\\par\n  \\end{otherlanguage}")
    assert "\\raggedright مرحبا" in out and "\\raggedleft يسار" in out and "\\centering وسط" in out


def test_bullets_of_a_right_to_left_text_are_inside_its_language():
    out = paragraphs_latex(paras(rtl("אחד", "right", True), rtl("שתיים", "right", True)), base18,
                           fresh_context(BEAMER_PT), "")
    assert out.index("\\begin{otherlanguage}{hebrew}") < out.index("\\begin{itemize}")
    assert out.index("\\end{itemize}") < out.index("\\end{otherlanguage}")


def test_one_right_to_left_paragraph_among_others_gets_a_group_of_its_own():
    out = paragraphs_latex(paras(ltr("Hello", "left"), rtl("שלום", "right", False)), base18, fresh_context(BEAMER_PT), "")
    assert out.splitlines()[0] == "Hello"
    assert "\\begin{otherlanguage}{hebrew}\\raggedright שלום\\par\\end{otherlanguage}" in out


def test_a_list_opening_one_level_deep_has_an_item_to_hang_on():
    """`\\begin{itemize}\\begin{itemize}` is "Something's wrong--perhaps a missing \\item"
    (arabic-training slides 12 and 17)."""
    deep: JsonObject = {**rtl("עמוק", "right", True), "level": 1}
    out = paragraphs_latex(paras(deep, rtl("אחד", "right", True), deep), base18, fresh_context(BEAMER_PT), "")
    lines = [l.strip() for l in out.splitlines()]
    assert lines[1:4] == ["\\begin{itemize}", "\\item[]", "\\begin{itemize}"]
    assert lines.count("\\item[]") == 1                   # the second deep item follows a real one


def test_left_to_right_paragraphs_are_written_as_before():
    out = paragraphs_latex(paras(ltr("a", "left"), ltr("b", "right"), ltr("c", "center")), base18, fresh_context(BEAMER_PT), "")
    assert out == "a\n\n\\raggedleft b\n\n\\centering c"


def test_a_blank_lines_size_ends_with_it():
    """cs161-tls slide 41: a 9 pt spacer line between 14 pt paragraphs left its `\\fontsize` on, and
    both paragraphs after it came out at 9 pt (runs are written against the base style)."""
    spacer: JsonObject = {"runs": [{"text": " ", "size": 7.0}], "align": "left", "bullet": None, "level": 0}
    out = paragraphs_latex(paras(ltr("a", "left"), spacer, ltr("b", "left")), base18, fresh_context(BEAMER_PT), "")
    blank = out.split("\n\n")[1]
    assert blank.startswith("{\\fontsize") and blank.endswith("\\strut\\par}")


def test_a_soft_break_is_never_inside_a_style():
    """`\\underline{a\\\\ b}` stops the build ("Not allowed in LR mode", jruby-ja slide 10)."""
    out = runs_latex(loop_runs({"text": "10000\x0bmatcher", "underline": True}), NO_BASE, fresh_context(BEAMER_PT))
    assert out == "\\underline{10000}\\\\ \\underline{matcher}"
    assert "\\underline{\\\\" not in runs_latex(loop_runs({"text": "\x0b", "underline": True}), NO_BASE, fresh_context(BEAMER_PT))


def test_two_soft_breaks_in_a_row_leave_an_empty_line_that_compiles():
    """A second `\\\\` on an empty line is "There's no line here to end" in ragged text."""
    out = runs_latex(loop_runs({"text": "a\x0b\x0bb"}), NO_BASE, fresh_context(BEAMER_PT))
    assert out == "a\\\\ \\mbox{}\\\\ b"


# -------------------------------------------------------------------------------------- the fonts

def test_scripts_are_told_apart_by_their_letters():
    assert [scripts.script_of(c) for c in "aé→あ漢한שم"] == [None, None, "other", "kana", "han", "hangul",
                                                         "hebrew", "arabic"]
    assert scripts.cjk_language({"あ": 1, "漢": 3}) == "japanese"
    assert scripts.cjk_language({"這": 2, "個": 1}) == "chinese-traditional"
    assert scripts.cjk_language({"这": 2}) == "chinese-simplified"


def target_with(words: str) -> JsonObject:
    """The IR of a deck of one left-to-right box of `words` in Arial."""
    return target_set(words, font="Arial", direction=None)


def target_set(words: str, *, font: str, direction: str | None) -> JsonObject:
    """The IR of a deck of one box of `words` in `font`, `direction` as its paragraph says."""
    return jobj(deck_ir(deck(paragraph(words, font=font, direction=direction, align=None)), foreign=True))


def test_a_latin_deck_gets_nothing():
    assert scripts.script_preamble(target_with("Hello world"), None, None) == []


def test_a_japanese_deck_breaks_lines_between_any_two_characters_and_gets_a_fallback(font_folder: Path, tmp_path: Path):
    make_font(font_folder, "Yu Gothic", "日本語です")
    make_face(font_folder, "Yu Gothic", "日本語です", True)
    lines = scripts.script_preamble(target_with("日本語です"), tmp_path / "tree", None)
    assert "\\babelprovide[import,onchar=ids]{japanese}" in lines
    chain = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "[fonts/YuGothic-Regular.ttf]:mode=node;+palt;" in chain
    assert "YuGothic-Bold.ttf" in next(l for l in lines if "b2sscriptsbold" in l)
    assert (tmp_path / "tree" / "fonts" / "YuGothic-Regular.ttf").exists()
    assert lines[-1].startswith("\\defaultfontfeatures{RawFeature={fallback=b2sscripts}")


def test_a_korean_deck_is_set_proportionally_too(font_folder: Path, tmp_path: Path):
    """HarfBuzz without `+palt` sets Korean wider than Slides does (wow-korea, korea-pptx): Korean
    gets the same proportional metrics as Japanese and Chinese (`adopt_bench --tag fontfix`)."""
    make_font(font_folder, "Malgun Gothic", "한국어입니다")
    lines = scripts.script_preamble(target_with("한국어입니다"), tmp_path / "tree", None)
    assert "\\babelprovide[import,onchar=ids]{korean}" in lines
    chain = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "[fonts/MalgunGothic-Regular.ttf]:mode=node;+palt;" in chain


def test_the_decks_own_font_wins_when_it_covers_the_letters(font_folder: Path):
    make_font(font_folder, "Microsoft JhengHei", "這是中文")
    make_font(font_folder, "Microsoft YaHei", "這是中文")
    make_font(font_folder, "Half Font", "這是")                  # the deck's, but it lacks two of them
    p = scripts.plan(target_set("這是中文", font="Microsoft JhengHei", direction=None))
    assert p.cjk == "chinese-traditional"
    assert [f.families for f, _ in p.chain] == [("microsoftjhenghei",)]
    p = scripts.plan(target_set("這是中文", font="Half Font", direction=None))
    assert [f.families for f, _ in p.chain] == [("microsoftjhenghei",)]   # the traditional fallback


def test_every_face_of_a_collection_is_found(font_folder: Path):
    """MS PGothic is face 2 of msgothic.ttc: closing the file after face 0 hid the others."""
    from fontTools.ttLib import TTCollection, TTFont
    paths = [make_font(font_folder, name, "abc") for name in ("MS Gothic", "MS PGothic")]
    ttc = TTCollection()
    ttc.fonts = [TTFont(p) for p in paths]
    ttc.save(font_folder / "msgothic.ttc")
    for p in paths:
        p.unlink()
    scripts._FACES.clear()
    adopt._FAMILIES.clear()
    face = scripts.find_face("MS PGothic", False)
    assert face is not None and face.index == 1
    got = adopt.font_family("MS PGothic", "sans", "")
    assert got is not None and got.files.index == 1
    opts = adopt.font_files_latex(got.files, None)
    # FontIndex is family-wide, so it comes first and holds for the faces the family has no file of
    # its own for (measured: the faked bold keeps face 1's widths, not face 0's)
    assert opts.startswith("FontIndex=1,")
    assert "Extension=.ttc,UprightFont=*," in opts and "BoldFont=*,BoldFeatures={FakeBold=" in opts
    assert got.stem == "msgothic"


def test_cjk_letters_do_not_count_against_the_font_they_are_typed_in(font_folder: Path):
    """jruby-ja's Arial runs are mostly Japanese, which Slides draws from its CJK fallback: Arial
    still sets their Latin (it went to Tahoma, 4% wider)."""
    make_font(font_folder, "Arial", "Rubyis")
    make_font(font_folder, "Noto Sans JP", "日本語です")
    adopt._FAMILIES.clear()
    t = target_with("Ruby is 日本語です日本語です日本語です")
    lines = adopt.font_preamble(parse_target(t), t, None, None)
    assert any(l.startswith("\\setsansfont{Arial}") for l in lines)


@pytest.fixture
def google_says_no(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetching on, and google/fonts has none of the fonts asked for."""
    from beamer2slides import fontfetch

    def missing() -> set[str]:
        return {"microsoftjhenghei", "mspgothic", "notosanstc"}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(adopt, "_LACKS", {})
    monkeypatch.setattr(fontfetch, "fetch_family", no_family)
    monkeypatch.setattr(fontfetch, "_missing", missing)


def test_a_cjk_font_slides_lacks_is_drawn_in_times_and_the_renderers_noto(font_folder: Path, google_says_no: None):
    """apps-edu-zh's Microsoft JhengHei: Slides draws its Latin in Times New Roman and its ideographs
    in Noto Serif TC, proportionally (palt) - Slides' own substitute for a face it cannot draw at all
    is serif throughout, not just its Latin (hunt 2026-09-27)."""
    make_font(font_folder, "Microsoft JhengHei", "這是中文Gogle")
    make_font(font_folder, "Times New Roman", "Gogle")
    make_font(font_folder, "Noto Serif TC", "這是中文")
    adopt._FAMILIES.clear()
    assert adopt.slides_lacks_cjk("Microsoft JhengHei")
    got = adopt.font_family("Microsoft JhengHei", "sans", "")
    assert got is not None
    assert got.files.upright.name == "TimesNewRoman-Regular.ttf" and got.match == "Microsoft JhengHei"
    t = target_set("Google 這是中文", font="Microsoft JhengHei", direction=None)
    p = scripts.plan(t)
    assert [f.families for f, _ in p.chain] == [("notoseriftc",)]
    chain = next(l for l in scripts.script_preamble(t, None, None) if "add_fallback(\"b2sscripts\"" in l)
    assert "NotoSerifTC-Regular.ttf]:mode=node;+palt;" in chain


def test_a_cjk_font_slides_has_or_a_latin_one_is_drawn_as_itself(font_folder: Path, google_says_no: None):
    make_font(font_folder, "MS PGothic", "這是中文Gogle")
    make_font(font_folder, "CMTT9", "Gogle")              # Slides draws it in a monospace, not Times
    make_font(font_folder, "Times New Roman", "Gogle")
    adopt._FAMILIES.clear()
    assert not adopt.slides_lacks_cjk("MS PGothic")
    assert not adopt.slides_lacks_cjk("CMTT9")
    got = adopt.font_family("MS PGothic", "sans", "")
    assert got is not None and got.files.upright.name == "MSPGothic-Regular.ttf"


def test_a_mincho_face_is_a_serif_that_its_noto_face_stands_in_for(font_folder: Path, google_says_no: None):
    """ja-schedule's address is MS Mincho, on no machine here: it read as a sans and fell to the
    fallback chain's Gothic. It is a serif, set in Noto Serif JP."""
    from beamer2slides.deck_ir import family_of
    make_font(font_folder, "Noto Serif JP", "住所です")
    adopt._FAMILIES.clear()
    assert family_of("MS Mincho") == "serif" and family_of("MS PGothic") == "sans"
    got = adopt.font_family("MS Mincho", family_of("MS Mincho"), "")
    assert got is not None
    assert got.files.upright.name == "NotoSerifJP-Regular.ttf" and got.match == "MS Mincho"


def test_arabic_is_set_whole_in_a_font_of_its_own_with_harfbuzz(font_folder: Path):
    """A glyph-by-glyph fallback shapes each letter alone: Arabic goes through babel's fonts."""
    make_font(font_folder, "Arial", "مرحبا ")
    lines = scripts.script_preamble(target_set("مرحبا", font="Arial", direction="RIGHT_TO_LEFT"), None, None)
    assert lines[0] == "\\usepackage[bidi=basic,layout=lists]{babel}"
    assert "\\babelprovide[import,onchar=ids fonts]{arabic}" in lines
    sf = next(l for l in lines if l.startswith("\\babelfont[arabic]{sf}"))
    assert "Renderer=HarfBuzz" in sf and sf.endswith("{Arial-Regular.ttf}")
    assert not any("add_fallback" in l for l in lines)


def test_tamil_is_set_whole_in_a_font_of_its_own(font_folder: Path):
    """tamil-wiki's Tamil came from the glyph-by-glyph fallback chain: vowel signs on dotted circles,
    conjuncts without theirs. A Brahmic script is a babel language with a font of its own - its
    fonts only: `onchar=ids` brought Tamil's hyphenation, and LuaTeX's line breaker stopped on
    babel's marks inside the discretionaries ("invalid node with type whatsit")."""
    make_font(font_folder, "Nirmala UI", "".join(sorted(set("இணையத்தொழில்நுட்பங்கள்"))))
    lines = scripts.script_preamble(target_with("இணையத் தொழில்நுட்பங்கள்"), None, None)
    assert "\\babelprovide[import,onchar=fonts]{tamil}" in lines
    sf = next(l for l in lines if l.startswith("\\babelfont[tamil]{sf}"))
    assert "Renderer=HarfBuzz" in sf and sf.endswith("{NirmalaUI-Regular.ttf}")
    assert scripts.fontspec_script("hindi") == "Devanagari" and scripts.fontspec_script("tamil") == "Tamil"


def test_arabic_in_the_decks_second_typeface_is_sent_to_a_font_that_has_it(font_folder: Path):
    """saudi-cats' Montserrat title "Shukran | شكراً": babel's `onchar=ids fonts` only swaps the
    families it was told about, so in a `\\newfontfamily` switch the Arabic drew as empty boxes."""
    latin = "Welcome to the kingdom of cats and coral stone alleys "
    make_font(font_folder, "Arial", "".join(sorted(set(latin + "Shukran|"))) + "شكراً")
    make_font(font_folder, "Montserrat", "".join(sorted(set("Shukran | " * 8))))
    adopt._FAMILIES.clear()
    t = jobj(deck_ir(deck(plain(latin * 3, "Arial"), plain("Shukran | " * 8 + "شكراً", "Montserrat")),
                     foreign=True))
    ctx = adopt.adopt_context()
    lines = adopt.font_preamble(parse_target(t), t, None, ctx)
    command = ctx.font_switches["Montserrat"]
    assert f"\\babelfont{{{command[1:]}}}[" in "\n".join(lines)
    assert any(l.startswith(f"\\babelfont[arabic]{{{command[1:]}}}") and l.endswith("{Arial-Regular.ttf}")
               for l in lines)
    assert f"\\newcommand{command}{{\\{command[1:]}family}}" in lines
    assert not any(l.startswith("\\newfontfamily") for l in lines)


@pytest.mark.parametrize("arabic", ["", "شكراً"])
def test_a_second_typeface_that_draws_all_its_letters_is_a_plain_newfontfamily(font_folder: Path, arabic: str):
    """arabic-training's Tahoma has Arabic of its own: sent to the sans family's Arabic face its slides
    lost up to 0.05 ink."""
    latin = "Welcome to the kingdom of cats and coral stone alleys "
    make_font(font_folder, "Arial", "".join(sorted(set(latin))) + "شكراً")
    make_font(font_folder, "Montserrat", "".join(sorted(set("Shukran |"))) + arabic)
    adopt._FAMILIES.clear()
    t = jobj(deck_ir(deck(plain(latin * 3, "Arial"), plain("Shukran | " * 8 + arabic, "Montserrat")),
                     foreign=True))
    ctx = adopt.adopt_context()
    lines = adopt.font_preamble(parse_target(t), t, None, ctx)
    line = next(l for l in lines if l.startswith(f"\\newfontfamily{ctx.font_switches['Montserrat']}{{Montserrat}}"))
    assert not any(l.startswith("\\babelfont") for l in lines)
    # and it shapes its own Arabic: LuaTeX's node renderer drew arabic-training's Tahoma unjoined
    # (and HarfBuzz left to guess the script joined only some letters)
    assert line.endswith(",Renderer=HarfBuzz,Script=Arabic]") == bool(arabic)


def test_a_font_missing_after_a_second_typeface_is_still_named(font_folder: Path):
    """drawing-workshop crashed in `font_preamble` ('set' object is not callable): the switch branch's
    set of languages had taken the name of the recorder of missing fonts, called for the next font."""
    latin = "Welcome to the kingdom of cats and coral stone alleys "
    make_font(font_folder, "Arial", "".join(sorted(set(latin))))
    make_font(font_folder, "Montserrat", "".join(sorted(set("Shukran |"))))
    adopt._FAMILIES.clear()
    t = jobj(deck_ir(deck(plain(latin * 3, "Arial"), plain("Shukran | " * 8, "Montserrat"),
                          plain("Nowhere to be found " * 2, "Nowhere")), foreign=True))
    ctx = adopt.adopt_context()
    adopt.font_preamble(parse_target(t), t, None, ctx)
    assert "Montserrat" in ctx.font_switches
    assert "Nowhere" in [m["font"] for m in ctx.missing_fonts]


def test_a_font_only_a_table_is_set_in_gets_its_switch(font_folder: Path):
    """saudi-cats slide 5: the table's Roboto went uncounted (cells hold their paragraphs under
    `table_cells`), and every cell was set in the document's Montserrat."""
    make_font(font_folder, "Montserrat", "Felinspcy ")
    make_font(font_folder, "Roboto", "Densfurpadwk ")
    adopt._FAMILIES.clear()
    def run(text: str, font: str) -> JsonObject:
        return {"paragraphs": [{"runs": [{"text": text, "font": font, "family": "sans", "size": 10}]}]}
    title: JsonObject = {"kind": "text", "bbox": [0, 0, 400, 40], **run("Feline species " * 3, "Montserrat")}
    table: JsonObject = {"kind": "table", "bbox": [0, 50, 400, 150],
             "table_cells": [{"row": 0, "col": k, **run("Dense fur padded paws ", "Roboto")} for k in range(4)]}
    ctx = adopt.adopt_context()
    t = target_filled({"slides": [{"elements": [title, table]}]})
    lines = adopt.font_preamble(parse_target(t), t, None, ctx)
    assert "Roboto" in ctx.font_switches or any(l.startswith("\\setsansfont{Roboto}") for l in lines)


def test_the_deck_s_own_hebrew_font_is_fetched_before_one_is_picked(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """The showcase's water-cycle deck types its Hebrew and Arabic in Noto Sans Hebrew and Arabic. On
    the first run neither was in the font folders yet: CJK fetched its font first, these letters did
    not, and they were set in Arial (smaller, other shapes)."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "שלום ")
    monkeypatch.setattr(adopt, "fetching", fetching)

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        return {"Regular": make_font(font_folder, name, "שלום ")}
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_set("שלום", font="Noto Sans Hebrew", direction="RIGHT_TO_LEFT"),
                                    None, None)
    sf = next(l for l in lines if l.startswith("\\babelfont[hebrew]{sf}"))
    assert sf.endswith("{NotoSansHebrew-Regular.ttf}")


def test_hebrew_fetches_heebo_before_noto_sans_hebrew(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """Heebo is measured closest to what Slides itself draws for a deck whose font lacks Hebrew (IoU
    0.337 against Noto Sans Hebrew's 0.173, out\\grind\\arabic-face\\): it is tried first."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Montserrat", "Thanks ")
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        return {"Regular": make_font(font_folder, name, "שלום ")}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_set("שלום", font="Montserrat", direction="RIGHT_TO_LEFT"), None, None)
    assert fetched == ["Heebo"]
    assert any(l.startswith("\\babelfont[hebrew]{sf}") and "Heebo" in l for l in lines)


def test_a_machine_with_no_font_for_a_script_fetches_one(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """plain-fonts offline (2026-09-27): a sandbox has no Arial, and Montserrat's Arabic came out
    as boxes - the fallback chain named only fonts a machine has. Now google/fonts gives one."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Montserrat", "Thanks ")
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        return {"Regular": make_font(font_folder, name, "شكرا ")}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_set("شكرا", font="Montserrat", direction="RIGHT_TO_LEFT"), None, None)
    assert fetched == [scripts.FETCHABLE["arabic"][0]]
    assert any(l.startswith("\\babelfont[arabic]{sf}") and "NotoNaskhArabic" in l for l in lines)


def test_a_machine_with_a_font_for_the_script_fetches_nothing(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "شكرا ")
    make_font(font_folder, "Montserrat", "Thanks ")
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", never_fetched)
    lines = scripts.script_preamble(target_set("شكرا", font="Montserrat", direction="RIGHT_TO_LEFT"), None, None)
    assert any(l.startswith("\\babelfont[arabic]{sf}") and "{Arial" in l for l in lines)


def test_thai_letters_fetch_a_thai_face_first(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    from beamer2slides import fontfetch
    make_font(font_folder, "Roboto", "Hi ")
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        return {"Regular": make_font(font_folder, name, "".join(sorted(set("สวัสดี"))) if "Thai" in name else "→")}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    scripts.script_preamble(target_set("สวัสดี", font="Roboto", direction=None), None, None)
    assert fetched == ["Noto Sans Thai"]


def test_thai_letters_are_their_own_script_not_a_symbol():
    """thai-history: Thai fell in `script_of`'s "other" catch-all, glyph by glyph like a math symbol,
    and the glyph-by-glyph fallback drew none of it at all (no font in the chain covered every
    character it needed at once). It is its own script now, like the Brahmic ones."""
    assert scripts.script_of("ก") == "thai" and scripts.script_of("๙") == "thai"
    assert scripts.fontspec_script("thai") == "Thai"


def test_thai_is_set_whole_in_a_font_of_its_own_with_word_breaks(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """thai-history's Arial and Calibri have no Thai on this machine: fetched a face of its own,
    not the "other" group's glyph-by-glyph chain (which drew nothing - no single fallback font
    covered the run). `onchar=ids fonts`, unlike the Brahmic scripts, gives it babel's `hyph-th`
    word breaks - Thai has no spaces between words - with no line-breaker crash seen."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "".join(sorted(set("Hello "))))
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        chars = "".join(sorted(set("สวัสดีครับ")))
        return {"Regular": make_font(font_folder, name, chars),
                "Bold": make_face(font_folder, name, chars, True)}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_with("สวัสดีครับ"), None, None)
    assert "\\babelprovide[import,onchar=ids fonts]{thai}" in lines
    sf = next(l for l in lines if l.startswith("\\babelfont[thai]{sf}"))
    assert "Renderer=HarfBuzz" in sf and sf.endswith("{NotoSansThai-Regular.ttf}")
    assert fetched == ["Noto Sans Thai"]
    assert not any("add_fallback" in l for l in lines)      # set whole, not glyph by glyph


def test_the_decks_own_thai_font_wins_when_it_covers_the_letters(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """A deck already set in a face that has Thai (unlike thai-history's Arial and Calibri) keeps
    it, and fetches nothing."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Angsana New", "".join(sorted(set("สวัสดีครับ"))))
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", never_fetched)
    lines = scripts.script_preamble(target_set("สวัสดีครับ", font="Angsana New", direction=None), None, None)
    sf = next(l for l in lines if l.startswith("\\babelfont[thai]{sf}"))
    assert sf.endswith("{AngsanaNew-Regular.ttf}")


def test_the_adopted_preamble_puts_script_lines_before_the_fonts(font_folder: Path, tmp_path: Path):
    make_font(font_folder, "Yu Gothic", "日本")
    t = target_with("日本")
    text = adopt.bootstrap(t, tmp_path / "main.tex", False, None)
    assert text.index("\\defaultfontfeatures") < text.index("\\setsansfont")
    assert text.index("{babel}") < text.index("\\usepackage{fontspec}")


def test_a_glyph_bullet_is_text_its_face_must_draw():
    """supercharge-slides' ➔ bullets, which Alegreya lacks, came out as its .notdef cross: a bullet
    glyph counts among the deck's text, so a fallback is found for it (● ○ ■ are drawn, not set)."""
    para: JsonObject = {"runs": [{"text": "Click", "font": "Alegreya", "family": "sans"}],
            "bullet": {"kind": "glyph", "text": "\u2794"}}
    dot: JsonObject = {**para, "bullet": {"kind": "glyph", "text": "\u25cf"}}
    got = list(scripts.deck_text({"slides": [{"elements": [{"paragraphs": [para, dot]}]}]}))
    assert ("\u2794", "Alegreya", "sans") in got and not any(t == "\u25cf" for t, _, _ in got)


def test_a_machine_without_fonttools_loses_the_fallback_chain_and_not_the_source(
        font_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]):
    """It is a plain dependency now (pyproject.toml), and where it is missing anyway what goes is
    the chain, not the tree: the playground's own image had none, so `deck_adopt` read the deck,
    fetched its fonts, wrote figures, shapes and fonts, and then died on `import fontTools` with
    main.tex unwritten - minutes of Google thumbnails for nothing (2026-09-21)."""
    import builtins
    make_font(font_folder, "Segoe UI Symbol", "\u2192")
    real = builtins.__import__

    # as the import statement calls it: every argument, `fromlist` None for a plain `import`
    def refuse(name: str, globals: Mapping[str, object] | None, locals: Mapping[str, object] | None,
               fromlist: Sequence[str] | None, level: int) -> ModuleType:
        if name.split(".")[0] == "fontTools":
            raise ModuleNotFoundError("No module named 'fontTools'", name=name)
        return real(name, globals, locals, fromlist or (), level)

    scripts._FACES.clear()
    scripts._SAID = False
    monkeypatch.setattr(builtins, "__import__", refuse)
    assert scripts.script_preamble(target_with("go \u2192 on"), tmp_path / "tree", None) == []
    assert "fontTools is not installed" in capsys.readouterr().out


def test_georgian_letters_get_their_own_group_and_a_fetched_face(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """ka-project: Georgian was `script_of`'s "other" catch-all before, and typed "in" Times New
    Roman and Arial like every deck's Georgian is, its letters counted against those fonts'
    coverage and rejected them outright (`adopt.chain_letter`, `MIN_MAIN_COVERAGE`). It is its own
    group now, fetched like Thai/Hebrew when nothing on the machine has it."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Times New Roman", "".join(sorted(set("Hello "))))
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        return {"Regular": make_font(font_folder, name, "\u10d2\u10d4\u10dd")}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_set("\u10d2\u10d4\u10dd", font="Times New Roman", direction=None), None, None)
    assert fetched == ["Noto Sans Georgian"]
    chain = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "NotoSansGeorgian-Regular.ttf" in chain


def test_armenian_letters_get_their_own_group_too(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "".join(sorted(set("Hello "))))
    fetched: list[str] = []

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        fetched.append(name)
        return {"Regular": make_font(font_folder, name, "\u0561\u0562\u0563")}
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    lines = scripts.script_preamble(target_set("\u0561\u0562\u0563", font="Arial", direction=None), None, None)
    assert fetched == ["Noto Sans Armenian"]
    chain = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "NotoSansArmenian-Regular.ttf" in chain


def test_georgian_and_armenian_do_not_count_against_the_deck_s_main_font(font_folder: Path):
    """The same letters, counted by `adopt.chain_letter`: a deck typed "in" Times New Roman with a
    third of it Georgian must not reject Times New Roman as the document's serif face for it, any
    more than a mostly-Japanese Arial title does (`test_cjk_letters_do_not_count_against_the_font_
    they_are_typed_in`)."""
    assert adopt.chain_letter("\u10d2")            # georgian
    assert adopt.chain_letter("\u0561")            # armenian
    assert not adopt.chain_letter("A")


def test_a_script_with_no_font_anywhere_is_reported_not_silent(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """Georgian on ka-project's sandbox before this fix: nothing on the machine and nothing fetched
    covered it, so lualatex warned "Missing character" for every letter and adopt's own log said
    nothing (the coordinator's "silent tofu" - hunt 2026-09-27). A script `plan()` never finds a
    single font for goes into `missing` like a substituted font does, so the report says so."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Times New Roman", "".join(sorted(set("Hello "))))
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", no_family)   # google/fonts gives nothing
    t = target_set("\u10d2\u10d4\u10dd", font="Times New Roman", direction=None)
    p = scripts.plan(t)
    assert p.uncovered == {"georgian": 3}
    missing: list[MissingFont] = []
    scripts.script_preamble(t, None, missing)
    assert missing == [{"font": "", "kind": "georgian", "letters": 3, "set_in": "", "script": True}]
    lines = adopt.missing_fonts_lines(missing)
    assert any("no font anywhere" in l for l in lines)
    assert any(l.strip().startswith("georgian (3 letters)") for l in lines)


def test_georgian_chain_letters_are_stretched_narrower_to_match_slides(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """ka-project: Noto Sans Georgian, the only offline fallback for Georgian typed "in" Arial or
    Times New Roman (`CHAIN_GROUPS`), sets its letters 1.20-1.22x as wide as Slides actually draws
    them (two clean lines measured against the deck's own thumbnails, hunt 2026-09-27) - a mismatch
    that wraps a line later than Slides did and drifts the rest of the box down. `CHAIN_STRETCH`'s
    ratio must reach the raw luaotfload request as its own `extend=` (confirmed offline: narrower
    glyphs, unchanged line height), for both the regular and the bold fallback face, whichever font
    the deck declared."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "".join(sorted(set("Hello "))))
    make_font(font_folder, "Noto Sans Georgian", "გეო")
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", no_family)  # already on the machine
    lines = scripts.script_preamble(target_set("გეო", font="Arial", direction=None), None, None)
    regular = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    bold = next(l for l in lines if "add_fallback(\"b2sscriptsbold\"" in l)
    ratio = scripts.CHAIN_STRETCH["georgian"]
    assert f"extend={ratio};" in regular
    assert f"extend={ratio};" in bold


def test_a_script_with_no_stretch_entry_keeps_a_plain_chain_spec(font_folder: Path, monkeypatch: pytest.MonkeyPatch):
    """Only a group named in `CHAIN_STRETCH` (georgian) gets an `extend=`: the symbol
    fallback group "other" (`\\rightarrow` here) is never stretched, so the chain spec luaotfload
    sees for it is unchanged from before this fix."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "".join(sorted(set("Hello "))))
    make_font(font_folder, "Noto Sans Symbols 2", "→")
    monkeypatch.setattr(adopt, "fetching", fetching)
    monkeypatch.setattr(fontfetch, "fetch_family", no_family)
    lines = scripts.script_preamble(target_set("go → on", font="Arial", direction=None), None, None)
    regular = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "extend=" not in regular
