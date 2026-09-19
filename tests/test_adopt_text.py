"""Text boxes as Slides lays them out (adopt.text_box_latex, and what deck_ir reads for it).

Offline, no TeX and no Google: hand-built `presentations.get` answers shaped like the corpus decks
that showed each problem (cs161's per-level master styles and collapsed list spacing, a title shrunk
by autofit, a label bottom-aligned by its layout, soft breaks inside bold words), then the IR and
the LaTeX `adopt.bootstrap` writes from it.
"""

import re

import pytest

from beamer2slides import adopt
from beamer2slides.deck_ir import deck_ir

from .test_adopt import at, pt, solid

EMU = 12700


@pytest.fixture(autouse=True)
def lengths_as_written(monkeypatch):
    """These tests read the writers' lengths; their rewriting into bp is test_adopt's
    `test_lengths_are_written_in_pdf_points_and_the_deck_words_are_left_alone`."""
    monkeypatch.setattr(adopt, "to_bp", lambda text: text)

@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def colour(hexc: str) -> dict:
    return {"opaqueColor": solid(hexc)["solidFill"]["color"]}


def para(text: str, *, level: int | None = None, glyph: str = "●", style: dict | None = None,
         runs: list[tuple[str, dict]] | None = None, bullet_style: dict | None = None) -> list[dict]:
    marker: dict = {"style": style or {}}
    if level is not None:
        marker["bullet"] = {"listId": "L", "nestingLevel": level, "glyph": glyph,
                            "bulletStyle": bullet_style or {}}
    runs = runs or [(text, {})]
    out = [{"paragraphMarker": marker}]
    for k, (words, st) in enumerate(runs):
        out.append({"textRun": {"content": words + ("\n" if k == len(runs) - 1 else ""), "style": st}})
    return out


def box(oid: str, elements: list[dict], x=40, y=80, w=600, h=250, placeholder=None, **props) -> dict:
    shape = {"shapeType": "TEXT_BOX", "shapeProperties": props, "text": {"textElements": elements}}
    if placeholder:
        shape["placeholder"] = placeholder
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y), "shape": shape}


def master_body() -> dict:
    """The cs161 decks' master BODY placeholder: one empty paragraph per list level, 18 pt at level
    0 and 14 pt below it, spaceBelow 12 with COLLAPSE_LISTS, bullets DARK1."""
    elements = []
    for level, size in ((0, 18), (1, 14), (2, 14)):
        elements += [{"paragraphMarker": {"style": {"lineSpacing": 115, "spaceBelow": pt(12),
                                                    "spacingMode": "COLLAPSE_LISTS"},
                                          "bullet": {"listId": "M", "nestingLevel": level, "glyph": "●"}}},
                     {"textRun": {"content": "\n", "style": {"fontFamily": "Arial", "fontSize": pt(size),
                                                             "foregroundColor": colour("000000")}}}]
    shape = box("m_body", elements, placeholder={"type": "BODY"},
                contentAlignment="TOP", autofit={"fontScale": 1})
    shape["shape"]["text"]["lists"] = {"M": {"nestingLevel": {
        str(n): {"bulletStyle": {"foregroundColor": colour("CC0000")}} for n in range(3)}}}
    return shape


def deck(*slide_elements: dict, layout_elements: list[dict] | None = None) -> dict:
    master = {"objectId": "m1", "pageElements": [master_body()]}
    layout = {"objectId": "L1", "layoutProperties": {"masterObjectId": "m1"},
              "pageElements": layout_elements or []}
    slide = {"objectId": "s1", "slideProperties": {"layoutObjectId": "L1"},
             "pageElements": list(slide_elements),
             "pageProperties": {"pageBackgroundFill": solid("FFFFFF")}}
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [master], "layouts": [layout], "slides": [slide]}


def body_deck() -> dict:
    return deck(box("s_body", para("Goals", level=0, style={"indentStart": pt(36), "indentFirstLine": pt(18)})
                    + para("Confidentiality: read", level=1, glyph="○",
                           style={"indentStart": pt(72), "indentFirstLine": pt(54)},
                           runs=[("Confidentiality", {"bold": True}), (": read", {})]),
                    placeholder={"type": "BODY", "parentObjectId": "m_body"}))


def text_of(ir: dict, oid: str) -> dict:
    return next(e for e in ir["slides"][0]["elements"] if e.get("id") == oid)


# ---------------------------------------------------------------- what deck_ir reads

def test_each_list_level_takes_its_own_size_from_the_master():
    """The master holds one paragraph per list level; reading only the first set every sub-item of
    cs161 in the level-0 size and broke all their lines elsewhere."""
    el = text_of(deck_ir(body_deck(), foreign=True), "s_body")
    scale = el["box"]["scale"]
    sizes = [round(p["runs"][0]["size"] * scale, 1) for p in el["paragraphs"]]
    assert sizes == [18.0, 14.0]
    assert [p["slides"]["space_below"] for p in el["paragraphs"]] == [12.0, 12.0]
    assert el["paragraphs"][0]["slides"]["spacing_mode"] == "COLLAPSE_LISTS"
    assert el["paragraphs"][1]["slides"]["line_spacing"] == 1.15


def test_a_bullet_has_the_colour_its_list_level_gives_it():
    el = text_of(deck_ir(body_deck(), foreign=True), "s_body")
    b = el["paragraphs"][1]["bullet"]
    assert b["text"] == "○" and b["color"] == "#cc0000"
    assert abs(b["size"] - el["paragraphs"][1]["runs"][0]["size"]) < 0.01


def test_autofit_shrinks_what_slides_draws():
    """cs161's titles say 28 pt with fontScale 0.9 and the thumbnail's caps are 25.2 pt letters."""
    d = deck(box("s_t", para("Title", runs=[("Title", {"fontFamily": "Arial", "fontSize": pt(28)})]),
                 autofit={"fontScale": 0.9, "lineSpacingReduction": 0.1}))
    el = text_of(deck_ir(d, foreign=True), "s_t")
    assert round(el["paragraphs"][0]["runs"][0]["size"] * el["box"]["scale"], 1) == 25.2
    assert el["paragraphs"][0]["slides"]["line_spacing"] == pytest.approx(0.9)


def test_the_vertical_alignment_a_box_inherits_is_read():
    layout = [box("L_lbl", para(""), placeholder={"type": "BODY"}, contentAlignment="BOTTOM")]
    d = deck(box("s_l", para("Label"), placeholder={"type": "BODY", "parentObjectId": "L_lbl"}),
             layout_elements=layout)
    assert text_of(deck_ir(d, foreign=True), "s_l")["box"]["valign"] == "bottom"
    assert text_of(deck_ir(d), "s_l")["box"]["valign"] == "top", "pull keeps reading the slide only"


# ---------------------------------------------------------------- the LaTeX it writes

def source(tmp_path, d: dict) -> str:
    return adopt.bootstrap(deck_ir(d, foreign=True), tmp_path / "tree" / "main.tex")


def frame_of(text: str) -> str:
    return text[text.index("\\begin{frame}"):text.index("\\end{frame}")]


def test_a_box_is_its_own_size_and_aligned_by_tex(tmp_path):
    d = deck(box("s_m", para("Middle"), x=100, y=100, w=200, h=80, contentAlignment="MIDDLE"))
    frame = frame_of(source(tmp_path, d))
    height = 80 / 720 * 453.54
    assert f"\\vbox to {height:.1f}pt{{\\slidesbox" in frame
    assert frame.count("\\vss") == 2, "the slack goes above and below a middle-aligned stack"
    assert "itemize" not in frame and "\\item" not in frame


def test_list_items_are_drawn_with_slides_bullets_and_no_itemize(tmp_path):
    """Beamer's blue triangles are ink the deck does not have, and itemize cannot nest past three
    levels or hang a bullet where Slides does."""
    frame = frame_of(source(tmp_path, body_deck()))
    assert "itemize" not in frame and "\\item" not in frame
    assert frame.count("\\llap{\\tikz") == 2
    assert "circle[radius=" in frame and "draw=b2sCC0000" in frame and "fill=b2sCC0000" in frame


def test_a_bullet_is_followed_by_no_word_space(tmp_path):
    """The line end after a bullet's \\llap{...} was a space: every bulleted line of the corpus began
    one word space right of Slides' (cs161-tls 0.869 -> 0.989)."""
    frame = frame_of(source(tmp_path, body_deck()))
    assert frame.count("\\llap{") == 2
    assert not re.search(r"\\llap\{.*\}\s*\n", frame), "each bullet line ends in %"


def test_the_thumbnail_tells_a_fixed_box_with_no_insets():
    """A box that does not resize to fit its text says nothing of its insets to the API; its
    thumbnail does, by where the words' ink begins (gdg24's stat grids)."""
    import numpy as np

    from beamer2slides.deck_ir import thumbnail_insets
    scale = 720 / 453.54

    def element():
        return {"kind": "text", "bbox": [50.0, 50.0, 200.0, 100.0], "anchor": [54.22, 64.0], "wrap_width": 141.5,
                "paragraphs": [{"align": "left", "bullet": None, "runs": [{"text": "Words", "size": 8.0}],
                                "slides": {"indent_first": 0, "indent_start": 0}}],
                "box": {"valign": "top", "scale": scale}}

    def thumb(ink_at, top):
        """Words' ink from `ink_at` across and `top` down; default insets put the caps at 58.24."""
        px = 4.0
        im = np.full((int(300 * px), int(453.54 * px), 3), 240, dtype=np.int16)
        x = int(ink_at * px)
        im[int(top * px):int((top + 6) * px), x:x + int(40 * px)] = 20
        return im, px

    for ink_at, top, bare in ((50.6, 54.2, True), (55.0, 58.2, False), (50.6, 58.2, False)):
        el = element()
        thumbnail_insets([el], *thumb(ink_at, top))
        assert (el["box"].get("insets") == 0) == bare, (ink_at, top)
    el = element()
    thumbnail_insets([el, {"kind": "image", "bbox": [45.0, 40.0, 60.0, 110.0]}], *thumb(50.6, 54.2))
    assert "insets" not in el["box"], "a picture across the box's left strip is ink too"
    el = element()
    ground = {"kind": "image", "bbox": [0.0, 0.0, 453.54, 300.0]}
    thumbnail_insets([ground, el], *thumb(50.6, 54.2))
    assert el["box"].get("insets") == 0, "a template's full-slide picture under the box is its ground"
    el = element()
    thumbnail_insets([el, ground], *thumb(50.6, 54.2))
    assert "insets" not in el["box"], "a picture over the box hides what it would show"


def test_list_items_collapse_their_spacing_and_other_paragraphs_do_not(tmp_path):
    """Between two list items COLLAPSE_LISTS drops spaceBelow; between two plain paragraphs it is
    the gap Slides leaves (12 pt / scale more than the plain pitch)."""
    items = frame_of(source(tmp_path, body_deck()))
    plain = deck(box("s_p", para("One", style={"spaceBelow": pt(12), "spacingMode": "COLLAPSE_LISTS"})
                     + para("Two", style={"spaceBelow": pt(12), "spacingMode": "COLLAPSE_LISTS"})))
    shifts = lambda text: [float(k) for k in re.findall(r"\\prevdepth=\\dimexpr\\prevdepth([-+][\d.]+)pt", text)]
    (between_items,), (between_paras,) = shifts(items), shifts(frame_of(source(tmp_path / "b", plain)))
    # the plain paragraphs step 12 pt (7.56 PDF pt) further; the items only differ by their sizes
    assert between_paras == pytest.approx(-12 / 1.5875, abs=0.01)
    assert between_items > -1.5


def test_a_box_that_grows_to_fit_drops_its_first_space_above(tmp_path):
    """ds-lecture's bodies keep the 6 pt spaceAbove their master gives the first paragraph; gdg24's
    body copy, in boxes that resize to fit their text, says 22 pt and starts 22 pt higher."""
    paras = para("One", style={"spaceAbove": pt(22)}) + para("Two", style={"spaceAbove": pt(22)})
    kept = frame_of(source(tmp_path, deck(box("s_a", paras))))
    grows = frame_of(source(tmp_path / "g", deck(box("s_a", paras, autofit={"autofitType": "SHAPE_AUTOFIT"}))))
    assert "\\vskip13.86pt" in kept and "\\vskip13.86pt" not in grows
    for frame in (kept, grows):
        assert "\\prevdepth=\\dimexpr\\prevdepth-13.86pt" in frame, "the second one gets its 22 pt"


def fitted(h: float, **props) -> dict:
    """A box that resizes to fit two 24 pt lines (1.19 x 24 = 28.56 pt each), `h` pt tall."""
    words = [("Two lines", {"fontFamily": "Arial", "fontSize": pt(24)})]
    return deck(box("s_z", para("x", runs=words) + para("x", runs=words), x=100, y=100, w=300, h=h,
                    autofit={"autofitType": "SHAPE_AUTOFIT"}, **props))


def test_a_box_that_fits_its_text_with_no_room_to_spare_has_no_insets():
    """sc-dark-minimal's boxes (a PowerPoint template) are exactly as tall as their lines: their
    insets are 0, which the API never says, and Slides draws their text 6.5 pt higher and 6.7 pt
    further left than default insets would."""
    tight, roomy = deck_ir(fitted(59), foreign=True), deck_ir(fitted(72), foreign=True)
    assert text_of(tight, "s_z")["box"]["insets"] == 0
    assert "insets" not in text_of(roomy, "s_z")["box"], "57.1 pt of lines + 14.7 pt of default insets"
    assert "insets" not in text_of(deck_ir(fitted(59)), "s_z")["box"], "pull reads the converter's boxes"
    a, b = text_of(tight, "s_z"), text_of(roomy, "s_z")
    assert a["anchor"][1] < b["anchor"][1] and a["anchor"][0] < b["anchor"][0]


def test_a_box_with_no_insets_sets_its_text_against_its_edges(tmp_path):
    scale = 720 / 453.54
    tight = frame_of(source(tmp_path, fitted(59)))
    roomy = frame_of(source(tmp_path / "r", fitted(72)))
    x = 100 / scale
    assert f"\\begin{{textblock*}}{{{300 / scale:.1f}pt}}({x:.1f}pt," in tight
    assert "\\vskip0.00pt" in tight
    assert f"({x + 6.7 / scale:.1f}pt," in roomy and f"\\vskip{6.48 / scale:.2f}pt" in roomy


def imported_box(oid: str, h: float, y: float) -> dict:
    """What a .pptx import leaves: NEVER_COLLAPSE on every paragraph."""
    words = [("Two lines", {"fontFamily": "Arial", "fontSize": pt(24)})]
    style = {"spacingMode": "NEVER_COLLAPSE"}
    return box(oid, para("x", runs=words, style=style) + para("x", runs=words, style=style),
               x=100, y=y, w=300, h=h, autofit={"autofitType": "SHAPE_AUTOFIT"})


def test_an_imported_box_has_no_insets_when_the_decks_imports_prove_it():
    """A box whose lines wrap has room left over whatever its insets, so its height cannot tell.
    The SlidesCarnival templates came through a .pptx import that set every inset to 0, and their
    boxes that fit one line per paragraph prove it; gdg24 came through an import too and kept
    Slides' insets, with no imported box to say otherwise."""
    alone = deck_ir(deck(imported_box("s_w", 90, 100)), foreign=True)
    assert "insets" not in text_of(alone, "s_w")["box"]
    proven = deck_ir(deck(imported_box("s_w", 90, 100), imported_box("s_a", 58, 200),
                          imported_box("s_b", 59, 280)), foreign=True)
    assert all(text_of(proven, oid)["box"]["insets"] == 0 for oid in ("s_w", "s_a", "s_b"))


def test_text_after_a_tab_starts_at_the_next_default_stop(tmp_path):
    """creandum-board's tables of figures are words and tabs: Slides jumps to the next multiple of
    36 pt from the text's edge, TeX's space did not move them at all."""
    d = deck(box("s_t", para("x", runs=[("Revenue\t12\tup", {})])))
    text = source(tmp_path, d)
    frame = frame_of(text)
    assert "\\newcommand\\slidestab" in text
    assert frame.count("\\slidestab{22.68pt}") == 2, "36 pt of Slides is 22.68 pt of the page"
    assert "\\global\\slidesx=0.00pt" in frame
    broken = frame_of(source(tmp_path / "b", deck(box("s_t", para("x", runs=[("Revenue\t12\x0bup", {})])))))
    assert "\\slidestab" not in broken, "a soft break restarts the line: no stop to count from"


def test_empty_lines_at_the_end_of_a_middle_aligned_box_are_height(tmp_path):
    """ap-bio-stats' bodies are centred and end on an empty 24 pt line at 80% after 7 pt: the text
    stands 15 pt higher than without it. Under a top-aligned stack such a line changes nothing."""
    def body(align):
        return deck(box("s_e", para("Words", runs=[("Words", {"fontSize": pt(18)})])
                        + para("", runs=[("", {"fontSize": pt(24)})],
                               style={"lineSpacing": 80, "spaceAbove": pt(7)}),
                        contentAlignment=align))
    middle = text_of(deck_ir(body("MIDDLE"), foreign=True), "s_e")["paragraphs"]
    top = text_of(deck_ir(body("TOP"), foreign=True), "s_e")["paragraphs"]
    assert len(top) == 1 and len(middle) == 2
    scale = 720 / 453.54
    assert middle[1]["runs"][0]["text"] == " " and middle[1]["runs"][0]["size"] * scale == pytest.approx(24, rel=0.05)
    assert middle[1]["slides"]["line_spacing"] == pytest.approx(0.8)
    assert "% blank line" in frame_of(source(tmp_path, body("MIDDLE")))


def test_an_empty_line_is_as_tall_as_its_own_newline():
    """Slides sizes an empty paragraph by the style of its newline; taking the next paragraph's
    size made creandum-board's 8 pt spacer lines above 40 pt figures 40 pt tall."""
    d = deck(box("s_g", para("A", runs=[("A", {"fontSize": pt(18)})])
                 + para("", runs=[("", {"fontSize": pt(8)})])
                 + para("B", runs=[("B", {"fontSize": pt(40)})])))
    paras = text_of(deck_ir(d, foreign=True), "s_g")["paragraphs"]
    scale = 720 / 453.54
    assert [round(p["runs"][0]["size"] * scale) for p in paras] == [18, 8, 40]


def test_a_wide_line_spacing_adds_nothing_under_the_last_line(tmp_path):
    """Under 115% the last line keeps its extra space (firebase-jam, ap-bio-stats' centred bodies);
    from 150% up it is not in the stack (sc-dark-modern's 170% labels stood 7 pt high)."""
    def ending(spacing):
        d = deck(box("s_w", para("x", runs=[("Words", {"fontSize": pt(20)})], style={"lineSpacing": spacing}),
                     contentAlignment="MIDDLE"))
        return float(re.findall(r"\\vskip\\dimexpr([\d.]+)pt-\\prevdepth", frame_of(source(tmp_path / str(spacing), d)))[-1])
    assert ending(115) > ending(100)
    assert ending(150) == ending(170) == ending(100)


def test_every_font_a_text_box_selects_spaces_its_words_with_its_own_space(tmp_path):
    """\\spaceskip was set once, by \\slidesize, from the font selected then: a paragraph whose
    typeface switch came after it (sc-dark-modern's Courier Prime) was spaced with the sans font's
    narrow space."""
    text = source(tmp_path, body_deck())
    assert "\\AddToHook{selectfont}{\\ifslidesspace\\spaceskip=\\fontdimen2\\font" in text
    assert "\\slidesbox}{\\slidesspacetrue" in text


def test_a_box_whose_base_is_bold_writes_its_regular_words_regular(tmp_path):
    """`runs_latex` only ever wrote \\textbf, so under a bold base the regular words stayed bold."""
    d = deck(box("s_b", para("x", runs=[("Mostly bold words here", {"bold": True}), (" plain", {})])))
    frame = frame_of(source(tmp_path, d))
    assert "\\bfseries" in frame and "\\textmd{plain}" in frame


def test_a_soft_break_anywhere_is_a_line_break_that_compiles(tmp_path):
    """`\\\\` at a paragraph's start, after \\centering or at an \\item's start stopped whole decks
    (cs161-net 13, arabic-training 6-8): a break is written between styled pieces, in horizontal mode."""
    d = deck(box("s_s", para("x", runs=[("\x0bopening", {}), ("bold\x0bword", {"bold": True})],
                             style={"alignment": "CENTER"})))
    frame = frame_of(source(tmp_path, d))
    assert "\\\\" not in frame
    assert frame.count("\\unskip\\break") == 2
    # most of the words are bold, so bold is the box's base and the regular word says so
    assert "\\unskip\\break \\textmd{opening}bold\\unskip\\break word" in frame


def test_straight_quotes_and_double_hyphens_stay_as_typed():
    """fontspec's TeX ligatures curl a straight quote and join -- into an en dash; Slides shows
    what was typed (ds-lecture's "objects")."""
    assert adopt.text_escape('say "hi" it\'s `x` a--b---c') == (
        "say \\symbol{34}hi\\symbol{34} it\\symbol{39}s \\symbol{96}x\\symbol{96} a-{}-b-{}-{}-c")


def test_windows_font_files_are_one_family(tmp_path, monkeypatch):
    """arial.ttf, arialbd.ttf, ariali.ttf, arialbi.ttf: read as four families, Arial had no bold and
    every bold word of the cs161 decks came out regular."""
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    for n in ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"):
        (shelf / n).write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    text = source(tmp_path, body_deck())
    line = next(l for l in text.splitlines() if l.startswith("\\setsansfont"))
    assert "UprightFont=*" in line and "BoldFont=arialbd" in line and "BoldItalicFont=arialbi" in line


def test_a_foreign_deck_keeps_the_converters_own_fonts_by_name():
    """Lato, PT Serif and Roboto Mono are the converter's stand-ins for Computer Modern only in a
    deck it wrote: intro-lecture's code in Roboto Mono came back as CMTT9 and was set in Courier."""
    d = deck(box("s_c", para("x", runs=[("code()", {"fontFamily": "Roboto Mono", "fontSize": pt(14)})])))
    run = text_of(deck_ir(d, foreign=True), "s_c")["paragraphs"][0]["runs"][0]
    assert run["font"] == "RobotoMono" and run["family"] == "mono"
    assert run["size"] * 720 / 453.54 == pytest.approx(14, abs=0.01), "not FontMapper's Lato factor"
    assert text_of(deck_ir(d), "s_c")["paragraphs"][0]["runs"][0]["font"].startswith("CMTT")


def test_a_windows_font_is_found_by_the_family_its_file_names(tmp_path, monkeypatch):
    """cour.ttf is Courier New and ariblk.ttf Arial Black, which only their name tables say:
    comps-analysis's Courier New was set in Courier Prime, ap-bio-stats' Arial Black in Arial."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def ttf(path, family, weight=400):
        fb = FontBuilder(1000, isTTF=True)
        fb.setupGlyphOrder([".notdef", "a"])
        fb.setupCharacterMap({ord("a"): "a"})
        fb.setupGlyf({g: TTGlyphPen(None).glyph() for g in (".notdef", "a")})
        fb.setupHorizontalMetrics({".notdef": (500, 0), "a": (600, 0)})
        fb.setupHorizontalHeader(ascent=800, descent=-200)
        fb.setupNameTable({"familyName": family, "styleName": "Regular"})
        fb.setupOS2(usWeightClass=weight)
        fb.setupPost()
        fb.save(str(path))

    shelf = tmp_path / "shelf"
    shelf.mkdir()
    ttf(shelf / "cour.ttf", "Courier New")
    ttf(shelf / "courbd.ttf", "Courier New", 700)
    ttf(shelf / "arial.ttf", "Arial")
    ttf(shelf / "ariblk.ttf", "Arial Black", 900)
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    adopt._FAMILIES.clear()
    from beamer2slides import scripts
    scripts._FACES.clear()
    try:
        mono = adopt.font_family("Courier New", "mono")
        black = adopt.font_family("Arial Black", "sans")
    finally:
        adopt._FAMILIES.clear()
        scripts._FACES.clear()
    assert mono["stem"] == "cour" and mono["BoldFont"].name == "courbd.ttf"
    assert black["stem"] == "ariblk", "not Arial, whose name it begins with"
    assert adopt.flatten(black["match"]) == "arialblack"


def test_a_family_in_two_file_formats_names_every_file():
    """Windows ships Cambria as cambria.ttc beside cambriab.ttf: one Extension for the family made
    fontspec look for cambriab.ttc, and ap-bio-stats stopped compiling."""
    from pathlib import Path

    from beamer2slides.adopt import font_files_latex
    opts = font_files_latex({"UprightFont": Path("c/cambria.ttc"), "BoldFont": Path("c/cambriab.ttf")}, None)
    assert "Extension" not in opts
    assert "UprightFont=cambria.ttc" in opts and "BoldFont=cambriab.ttf" in opts


def test_powerpoint_insets_where_the_thumbnails_show_them():
    """A measured box 3.6 pt high has PowerPoint's insets; a deck most of whose measured boxes do
    lends them to its unmeasured ones, a mixed deck does not (deck_ir.pptx_insets)."""
    from beamer2slides.deck_ir import pptx_insets

    def box(valign="top"):
        return {"kind": "text", "box": {"valign": valign, "scale": 2.0}, "anchor": [10.0, 20.0]}

    hit, miss, other, low = box(), box(), box(), box("bottom")
    slides = [{"elements": [hit, miss, other, low]}]
    pptx_insets(slides, [(hit, -3.5), (miss, 0.2)])
    assert hit["box"]["inset_y"] == 3.6 and hit["anchor"] == [10.0, 18.2]
    assert "inset_y" not in miss["box"] and "inset_y" not in other["box"]
    a, b, c, d = box(), box(), box(), box("bottom")
    pptx_insets([{"elements": [a, b, c, d]}], [(a, -3.6), (b, -3.9), (c, -3.2)])
    assert d["box"]["inset_y"] == 3.6 and d["anchor"] == [10.0, 21.8]


def test_a_box_with_powerpoint_insets_starts_its_text_3_6_pt_higher():
    el = {"kind": "text", "bbox": [0, 0, 100, 50], "box": {"scale": 1.0, "valign": "top"},
          "paragraphs": [{"runs": [{"text": "Hi", "size": 10.0}], "slides": {}}]}
    plain = adopt.text_box_latex(el, adopt.Context(), "")
    el["box"]["inset_y"] = 3.6
    assert "\\vskip6.48pt" in plain and "\\vskip2.88pt" in adopt.text_box_latex(el, adopt.Context(), "")


def test_a_paragraph_of_two_sizes_is_spaced_line_by_line():
    """comps-analysis: a 26.7 pt lead-in, 21.3 pt words; the line they wrap onto is 21.3 pt apart."""
    el = {"kind": "text", "bbox": [0, 0, 100, 80], "box": {"scale": 1.0, "valign": "top"},
          "paragraphs": [{"runs": [{"text": "First step: ", "size": 20.0},
                                   {"text": "two words", "size": 10.0}], "slides": {}},
                         {"runs": [{"text": "Next", "size": 10.0}], "slides": {}}]}
    tex = adopt.text_box_latex(el, adopt.Context(), "")
    small, big = adopt.line_box(10.0, 1.0), adopt.line_box(20.0, 1.0)
    assert f"\\baselineskip={sum(small):.2f}pt" in tex and "\\lineskiplimit=0pt" in tex
    assert f"height{small[0]:.2f}pt depth{small[1]:.2f}pt\\relax words" in tex
    assert f"height{big[0]:.2f}pt depth{big[1]:.2f}pt\\relax step:" in tex
    # the next paragraph is spaced from the depth TeX recorded, not from a guessed size
    assert f"\\prevdepth={sum(small) - small[0]:.2f}pt" in tex
