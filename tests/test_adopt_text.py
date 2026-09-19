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


def test_a_family_in_two_file_formats_names_every_file():
    """Windows ships Cambria as cambria.ttc beside cambriab.ttf: one Extension for the family made
    fontspec look for cambriab.ttc, and ap-bio-stats stopped compiling."""
    from pathlib import Path

    from beamer2slides.adopt import font_files_latex
    opts = font_files_latex({"UprightFont": Path("c/cambria.ttc"), "BoldFont": Path("c/cambriab.ttf")}, None)
    assert "Extension" not in opts
    assert "UprightFont=cambria.ttc" in opts and "BoldFont=cambriab.ttf" in opts
