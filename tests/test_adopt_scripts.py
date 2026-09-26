"""Text in scripts other than Latin in adopted sources (scripts.py, and what deck_ir reads for it).

Offline, no TeX and no Google: hand-built `presentations.get` answers with Hebrew, Arabic and
Japanese text, the IR that comes back (direction, visual alignment), the LaTeX written for right-to-left
paragraphs, and the preamble that gives TeX fonts for every script - chosen from tiny fonts made
here, so the answer does not depend on what this machine has installed.
"""

from pathlib import Path

import pytest

from beamer2slides import adopt, scripts
from beamer2slides.deck_ir import deck_ir
from beamer2slides.inverse import Context, paragraphs_latex, runs_latex

from .test_adopt import at, pt

EMU = 12700


@pytest.fixture(autouse=True)
def font_folder(monkeypatch, tmp_path):
    """Fonts come from a folder of this test's own (`$B2S_FONTS`), empty unless a test fills it."""
    folder = tmp_path / "fonts"
    folder.mkdir()
    monkeypatch.setenv("B2S_FONTS", str(folder))
    monkeypatch.delenv("B2S_NO_SCRIPTS", raising=False)
    return folder


def make_font(folder: Path, family: str, chars: str, bold: bool = False) -> Path:
    """A font named `family` whose cmap has exactly `chars` (and a space)."""
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


def paragraph(words: str, *, direction: str | None = None, align: str | None = None, font: str = "Arial",
              bullet: bool = False) -> list[dict]:
    style: dict = {}
    if direction:
        style["direction"] = direction
    if align:
        style["alignment"] = align
    marker: dict = {"style": style}
    if bullet:
        marker["bullet"] = {"glyph": "●", "nestingLevel": 0}
    return [{"paragraphMarker": marker},
            {"textRun": {"content": words + "\n", "style": {"fontFamily": font, "fontSize": pt(18)}}}]


def deck(*boxes: list[dict]) -> dict:
    elements = [{"objectId": f"t{i}", "size": {"width": pt(400), "height": pt(100)}, "transform": at(40, 40 + 110 * i),
                 "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": b}}} for i, b in enumerate(boxes)]
    return {"presentationId": "p", "pageSize": {"width": pt(720), "height": pt(405)},
            "slides": [{"objectId": "s1", "pageElements": elements}], "layouts": [], "masters": []}


def texts(target: dict) -> list[dict]:
    return [e for s in target["slides"] for e in s["elements"] if e["kind"] == "text"]


# ------------------------------------------------------------------------------------------- the IR

def test_a_right_to_left_paragraph_says_so_and_sits_where_the_deck_has_it():
    """START in a right-to-left paragraph is flush right: `align` says where the lines sit."""
    target = deck(paragraph("שלום עולם", direction="RIGHT_TO_LEFT"),
                  paragraph("مرحبا", direction="RIGHT_TO_LEFT", align="END"),
                  paragraph("Hello", direction="LEFT_TO_RIGHT"))
    t = deck_ir(target, foreign=True)
    first, second, third = texts(t)
    assert first["paragraphs"][0]["direction"] == "rtl" and first["paragraphs"][0]["align"] == "right"
    assert second["paragraphs"][0]["align"] == "left"
    assert "direction" not in third["paragraphs"][0] and third["paragraphs"][0]["align"] == "left"
    # the anchor follows the visual alignment: the right edge, less the padding
    x0, _, x1, _ = first["bbox"]
    assert first["anchor"][0] > (x0 + x1) / 2


# ----------------------------------------------------------------------------------------- the text

def rtl(words: str, align: str = "right", bullet: bool = False) -> dict:
    return {"runs": [{"text": words, "size": 18.0}], "align": align, "direction": "rtl",
            "bullet": {"kind": "glyph"} if bullet else None, "level": 0}


def ltr(words: str, align: str = "left") -> dict:
    return {"runs": [{"text": words, "size": 18.0}], "align": align, "bullet": None, "level": 0}


def test_a_right_to_left_text_is_set_in_its_language_with_logical_alignment():
    """LuaTeX's skips are logical: in an RTL paragraph `\\raggedright` is flush right."""
    out = paragraphs_latex([rtl("مرحبا"), rtl("يسار", "left"), rtl("وسط", "center")],
                           lambda p: {"size": 18.0}, Context(), "  ")
    assert out.startswith("  \\begin{otherlanguage}{arabic}\n") and out.endswith("\\par\n  \\end{otherlanguage}")
    assert "\\raggedright مرحبا" in out and "\\raggedleft يسار" in out and "\\centering وسط" in out


def test_bullets_of_a_right_to_left_text_are_inside_its_language():
    out = paragraphs_latex([rtl("אחד", bullet=True), rtl("שתיים", bullet=True)], lambda p: {"size": 18.0},
                           Context(), "")
    assert out.index("\\begin{otherlanguage}{hebrew}") < out.index("\\begin{itemize}")
    assert out.index("\\end{itemize}") < out.index("\\end{otherlanguage}")


def test_one_right_to_left_paragraph_among_others_gets_a_group_of_its_own():
    out = paragraphs_latex([ltr("Hello"), rtl("שלום")], lambda p: {"size": 18.0}, Context(), "")
    assert out.splitlines()[0] == "Hello"
    assert "\\begin{otherlanguage}{hebrew}\\raggedright שלום\\par\\end{otherlanguage}" in out


def test_a_list_opening_one_level_deep_has_an_item_to_hang_on():
    """`\\begin{itemize}\\begin{itemize}` is "Something's wrong--perhaps a missing \\item"
    (arabic-training slides 12 and 17)."""
    deep = {**rtl("עמוק", bullet=True), "level": 1}
    out = paragraphs_latex([deep, rtl("אחד", bullet=True), deep], lambda p: {"size": 18.0}, Context(), "")
    lines = [l.strip() for l in out.splitlines()]
    assert lines[1:4] == ["\\begin{itemize}", "\\item[]", "\\begin{itemize}"]
    assert lines.count("\\item[]") == 1                   # the second deep item follows a real one


def test_left_to_right_paragraphs_are_written_as_before():
    out = paragraphs_latex([ltr("a"), ltr("b", "right"), ltr("c", "center")], lambda p: {"size": 18.0}, Context(), "")
    assert out == "a\n\n\\raggedleft b\n\n\\centering c"


def test_a_soft_break_is_never_inside_a_style():
    """`\\underline{a\\\\ b}` stops the build ("Not allowed in LR mode", jruby-ja slide 10)."""
    out = runs_latex([{"text": "10000\x0bmatcher", "underline": True}], {}, Context())
    assert out == "\\underline{10000}\\\\ \\underline{matcher}"
    assert "\\underline{\\\\" not in runs_latex([{"text": "\x0b", "underline": True}], {}, Context())


def test_two_soft_breaks_in_a_row_leave_an_empty_line_that_compiles():
    """A second `\\\\` on an empty line is "There's no line here to end" in ragged text."""
    out = runs_latex([{"text": "a\x0b\x0bb"}], {}, Context())
    assert out == "a\\\\ \\mbox{}\\\\ b"


# -------------------------------------------------------------------------------------- the fonts

def test_scripts_are_told_apart_by_their_letters():
    assert [scripts.script_of(c) for c in "aé→あ漢한שم"] == [None, None, "other", "kana", "han", "hangul",
                                                         "hebrew", "arabic"]
    assert scripts.cjk_language({"あ": 1, "漢": 3}) == "japanese"
    assert scripts.cjk_language({"這": 2, "個": 1}) == "chinese-traditional"
    assert scripts.cjk_language({"这": 2}) == "chinese-simplified"


def target_with(words: str, font: str = "Arial", direction: str | None = None) -> dict:
    return deck_ir(deck(paragraph(words, font=font, direction=direction)), foreign=True)


def test_a_latin_deck_gets_nothing():
    assert scripts.script_preamble(target_with("Hello world"), None) == []


def test_a_japanese_deck_breaks_lines_between_any_two_characters_and_gets_a_fallback(font_folder, tmp_path):
    make_font(font_folder, "Yu Gothic", "日本語です")
    make_font(font_folder, "Yu Gothic", "日本語です", bold=True)
    lines = scripts.script_preamble(target_with("日本語です"), tmp_path / "tree")
    assert "\\babelprovide[import,onchar=ids]{japanese}" in lines
    chain = next(l for l in lines if "add_fallback(\"b2sscripts\"" in l)
    assert "[fonts/YuGothic-Regular.ttf]:mode=node;+palt;" in chain
    assert "YuGothic-Bold.ttf" in next(l for l in lines if "b2sscriptsbold" in l)
    assert (tmp_path / "tree" / "fonts" / "YuGothic-Regular.ttf").exists()
    assert lines[-1].startswith("\\defaultfontfeatures{RawFeature={fallback=b2sscripts}")


def test_the_decks_own_font_wins_when_it_covers_the_letters(font_folder):
    make_font(font_folder, "Microsoft JhengHei", "這是中文")
    make_font(font_folder, "Microsoft YaHei", "這是中文")
    make_font(font_folder, "Half Font", "這是")                  # the deck's, but it lacks two of them
    p = scripts.plan(target_with("這是中文", font="Microsoft JhengHei"))
    assert p.cjk == "chinese-traditional"
    assert [f.families for f, _ in p.chain] == [("microsoftjhenghei",)]
    p = scripts.plan(target_with("這是中文", font="Half Font"))
    assert [f.families for f, _ in p.chain] == [("microsoftjhenghei",)]   # the traditional fallback


def test_every_face_of_a_collection_is_found(font_folder):
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
    assert scripts.find_face("MS PGothic").index == 1
    files = adopt.font_family("MS PGothic", "sans")
    assert files["FontIndex"] == 1
    opts = adopt.font_files_latex({k: v for k, v in files.items() if k not in ("stem", "match")}, None)
    # FontIndex is family-wide, so it comes first and holds for the faces the family has no file of
    # its own for (measured: the faked bold keeps face 1's widths, not face 0's)
    assert opts.startswith("FontIndex=1,")
    assert "Extension=.ttc,UprightFont=*," in opts and "BoldFont=*,BoldFeatures={FakeBold=" in opts
    assert files["stem"] == "msgothic"


def test_cjk_letters_do_not_count_against_the_font_they_are_typed_in(font_folder):
    """jruby-ja's Arial runs are mostly Japanese, which Slides draws from its CJK fallback: Arial
    still sets their Latin (it went to Tahoma, 4% wider)."""
    make_font(font_folder, "Arial", "Rubyis")
    make_font(font_folder, "Noto Sans JP", "日本語です")
    adopt._FAMILIES.clear()
    lines = adopt.font_preamble(target_with("Ruby is 日本語です日本語です日本語です"), None)
    assert any(l.startswith("\\setsansfont{Arial}") for l in lines)


@pytest.fixture
def google_says_no(monkeypatch):
    """Fetching on, and google/fonts has none of the fonts asked for."""
    from beamer2slides import fontfetch
    monkeypatch.setattr(adopt, "fetching", lambda: True)
    monkeypatch.setattr(adopt, "_LACKS", {})
    monkeypatch.setattr(fontfetch, "fetch_family", lambda name, log=print: None)
    monkeypatch.setattr(fontfetch, "_missing", lambda: {"microsoftjhenghei", "mspgothic", "notosanstc"})


def test_a_cjk_font_slides_lacks_is_drawn_in_times_and_the_renderers_noto(font_folder, google_says_no):
    """apps-edu-zh's Microsoft JhengHei: Slides draws its Latin in Times New Roman and its ideographs
    in Noto Sans TC, proportionally (palt)."""
    make_font(font_folder, "Microsoft JhengHei", "這是中文Gogle")
    make_font(font_folder, "Times New Roman", "Gogle")
    make_font(font_folder, "Noto Sans TC", "這是中文")
    adopt._FAMILIES.clear()
    assert adopt.slides_lacks_cjk("Microsoft JhengHei")
    files = adopt.font_family("Microsoft JhengHei", "sans")
    assert files["UprightFont"].name == "TimesNewRoman-Regular.ttf" and files["match"] == "Microsoft JhengHei"
    t = target_with("Google 這是中文", font="Microsoft JhengHei")
    p = scripts.plan(t)
    assert [f.families for f, _ in p.chain] == [("notosanstc",)]
    chain = next(l for l in scripts.script_preamble(t, None) if "add_fallback(\"b2sscripts\"" in l)
    assert "NotoSansTC-Regular.ttf]:mode=node;+palt;" in chain


def test_a_cjk_font_slides_has_or_a_latin_one_is_drawn_as_itself(font_folder, google_says_no):
    make_font(font_folder, "MS PGothic", "這是中文Gogle")
    make_font(font_folder, "CMTT9", "Gogle")              # Slides draws it in a monospace, not Times
    make_font(font_folder, "Times New Roman", "Gogle")
    adopt._FAMILIES.clear()
    assert not adopt.slides_lacks_cjk("MS PGothic")
    assert not adopt.slides_lacks_cjk("CMTT9")
    assert adopt.font_family("MS PGothic", "sans")["UprightFont"].name == "MSPGothic-Regular.ttf"


def test_arabic_is_set_whole_in_a_font_of_its_own_with_harfbuzz(font_folder):
    """A glyph-by-glyph fallback shapes each letter alone: Arabic goes through babel's fonts."""
    make_font(font_folder, "Arial", "مرحبا ")
    lines = scripts.script_preamble(target_with("مرحبا", direction="RIGHT_TO_LEFT"), None)
    assert lines[0] == "\\usepackage[bidi=basic,layout=lists]{babel}"
    assert "\\babelprovide[import,onchar=ids fonts]{arabic}" in lines
    sf = next(l for l in lines if l.startswith("\\babelfont[arabic]{sf}"))
    assert "Renderer=HarfBuzz" in sf and sf.endswith("{Arial-Regular.ttf}")
    assert not any("add_fallback" in l for l in lines)


def test_the_deck_s_own_hebrew_font_is_fetched_before_one_is_picked(font_folder, monkeypatch):
    """The showcase's water-cycle deck types its Hebrew and Arabic in Noto Sans Hebrew and Arabic. On
    the first run neither was in the font folders yet: CJK fetched its font first, these letters did
    not, and they were set in Arial (smaller, other shapes)."""
    from beamer2slides import fontfetch
    make_font(font_folder, "Arial", "שלום ")
    monkeypatch.setattr(adopt, "fetching", lambda: True)
    monkeypatch.setattr(fontfetch, "fetch_family",
                        lambda name, log=print: {"Regular": make_font(font_folder, name, "שלום ")})
    lines = scripts.script_preamble(target_with("שלום", font="Noto Sans Hebrew", direction="RIGHT_TO_LEFT"),
                                    None)
    sf = next(l for l in lines if l.startswith("\\babelfont[hebrew]{sf}"))
    assert sf.endswith("{NotoSansHebrew-Regular.ttf}")


def test_the_adopted_preamble_puts_script_lines_before_the_fonts(font_folder, tmp_path):
    make_font(font_folder, "Yu Gothic", "日本")
    t = target_with("日本")
    text = adopt.bootstrap(t, tmp_path / "main.tex")
    assert text.index("\\defaultfontfeatures") < text.index("\\setsansfont")
    assert text.index("{babel}") < text.index("\\usepackage{fontspec}")


def test_a_glyph_bullet_is_text_its_face_must_draw():
    """supercharge-slides' ➔ bullets, which Alegreya lacks, came out as its .notdef cross: a bullet
    glyph counts among the deck's text, so a fallback is found for it (● ○ ■ are drawn, not set)."""
    para = {"runs": [{"text": "Click", "font": "Alegreya", "family": "sans"}],
            "bullet": {"kind": "glyph", "text": "\u2794"}}
    dot = {**para, "bullet": {"kind": "glyph", "text": "\u25cf"}}
    got = list(scripts.deck_text({"slides": [{"elements": [{"paragraphs": [para, dot]}]}]}))
    assert ("\u2794", "Alegreya", "sans") in got and not any(t == "\u25cf" for t, _, _ in got)


def test_a_machine_without_fonttools_loses_the_fallback_chain_and_not_the_source(
        font_folder, tmp_path, monkeypatch, capsys):
    """It is a plain dependency now (pyproject.toml), and where it is missing anyway what goes is
    the chain, not the tree: the playground's own image had none, so `deck_adopt` read the deck,
    fetched its fonts, wrote figures, shapes and fonts, and then died on `import fontTools` with
    main.tex unwritten - minutes of Google thumbnails for nothing (2026-09-21)."""
    import builtins
    make_font(font_folder, "Segoe UI Symbol", "\u2192")
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name.split(".")[0] == "fontTools":
            raise ModuleNotFoundError("No module named 'fontTools'", name=name)
        return real(name, *a, **k)

    scripts._FACES.clear()
    scripts._SAID = False
    monkeypatch.setattr(builtins, "__import__", refuse)
    assert scripts.script_preamble(target_with("go \u2192 on"), tmp_path / "tree") == []
    assert "fontTools is not installed" in capsys.readouterr().out
