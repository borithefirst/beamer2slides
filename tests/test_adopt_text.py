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


def macros(tmp_path) -> str:
    """The macro layer `bootstrap` writes beside main.tex."""
    return (tmp_path / "tree" / "slides.sty").read_text(encoding="utf-8")


def test_a_box_is_its_own_size_and_aligned_by_tex(tmp_path):
    d = deck(box("s_m", para("Middle"), x=100, y=100, w=200, h=80, contentAlignment="MIDDLE"))
    frame = frame_of(source(tmp_path, d))
    height = 80 / 720 * 453.54
    assert re.search(rf"\\slidetext\[middle\]\{{[\d.]+,[\d.]+,[\d.]+,{height:.1f}\}}", frame), frame
    sty = macros(tmp_path)
    assert "\\vbox to\\slides@h bp" in sty
    assert sty.count("\\else\\vss\\fi") == 2, "the slack goes above and below a middle-aligned stack"
    assert "itemize" not in frame and "\\item" not in frame


def test_list_items_are_drawn_with_slides_bullets_and_no_itemize(tmp_path):
    """Beamer's blue triangles are ink the deck does not have, and itemize cannot nest past three
    levels or hang a bullet where Slides does."""
    text = source(tmp_path, body_deck())
    frame = frame_of(text)
    assert "itemize" not in frame and "\\item" not in frame
    assert frame.count("\\slidebullet{") == 2
    assert "\\definecolor{Red}{HTML}{CC0000}" in text
    assert "circle[radius=" in text and "draw=Red" in text and "fill=Red" in text
    assert "\\llap{\\csname slides@m@" in macros(tmp_path)


def test_a_bullet_is_followed_by_no_word_space(tmp_path):
    """The line end after a bullet's \\llap{...} was a space: every bulleted line of the corpus began
    one word space right of Slides' (cs161-tls 0.869 -> 0.989)."""
    frame = frame_of(source(tmp_path, body_deck()))
    assert frame.count("\\slidebullet{") == 2
    assert not re.search(r"\\slidebullet\{[^}]*\}\{[^}]*\}\s", frame), "the words follow the bullet"


def test_the_thumbnail_tells_a_fixed_box_with_no_insets():
    """A box that does not resize to fit its text says nothing of its insets to the API; its
    thumbnail does, by where the words' ink begins (gdg24's stat grids)."""
    import numpy as np

    from beamer2slides.deck_thumbs import thumbnail_insets
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

    # gdg24's stat grids: a caption box overlaps the heading box's lower rows. Those rows are not read,
    # the rest still are - either way round.
    def caption(ink_x):
        im, px = thumb(50.6, 54.2)
        im[int(88 * px):int(94 * px), int(ink_x * px):int((ink_x + 30) * px)] = 20
        return im, px

    cap = {"kind": "text", "bbox": [45.0, 85.0, 200.0, 110.0]}
    el = element()
    thumbnail_insets([el, cap], *caption(55.0))
    assert el["box"].get("insets") == 0, "a box crossing the lower rows leaves the first line readable"
    el = element()
    im, px = caption(50.3)
    im[int(54.2 * px):int(60.2 * px)] = 240
    im[int(58.2 * px):int(64.2 * px), int(55 * px):int(95 * px)] = 20
    thumbnail_insets([el, cap], im, px)
    assert "insets" not in el["box"], "the caption's words at the edge are not this box's"


def test_ink_on_the_boxs_first_column_is_its_own_unless_it_goes_on_outside():
    """A glyph 0.13 pt inside the box edge rounds onto its first pixel column (gdg24's "Connect");
    ink that runs on past the edge is something else's (a highlight bar under a code listing)."""
    import numpy as np

    from beamer2slides.deck_thumbs import thumbnail_insets
    px = 4.0

    def read(ink_from):
        el = {"kind": "text", "bbox": [50.0, 50.0, 200.0, 100.0], "anchor": [54.22, 64.0], "wrap_width": 141.5,
              "paragraphs": [{"align": "left", "bullet": None, "runs": [{"text": "Words", "size": 8.0}],
                              "slides": {"indent_first": 0, "indent_start": 0}}],
              "box": {"valign": "top", "scale": 720 / 453.54}}
        im = np.full((int(300 * px), int(453.54 * px), 3), 240, dtype=np.int16)
        im[int(54.2 * px):int(60.2 * px), int(ink_from * px):int(90 * px)] = 20
        thumbnail_insets([el], im, px)
        return el["box"].get("insets")

    assert read(50.0) == 0
    assert read(48.0) is None


def monospace_face(monkeypatch, lsb: float = 0.05, top: float = 0.7):
    """The deck's own font at hand (`deck_thumbs.face_glyphs`): every glyph 0.6 em wide, ink from
    `lsb` to 0.55 em across and from the baseline to `top` em up."""
    from beamer2slides import deck_thumbs
    monkeypatch.setattr(deck_thumbs, "face_glyphs",
                        lambda font, bold=False, italic=False: lambda c: (0.6, lsb, 0.0, 0.55, top))


def test_a_big_first_glyphs_own_bearing_does_not_hide_a_box_with_no_insets(monkeypatch):
    """devfest2020's 65 pt "Use over" starts 4.5 pt into a box with no insets: more than half the
    inset, which alone read as Slides' default. The deck's font says how far its first glyph stands in."""
    import numpy as np

    from beamer2slides.deck_thumbs import starting_bearing, thumbnail_insets
    px, size, lsb = 4.0, 40.0, 0.12

    def read():
        el = {"kind": "text", "bbox": [50.0, 50.0, 300.0, 120.0], "anchor": [54.22, 90.0], "wrap_width": 241.5,
              "paragraphs": [{"align": "left", "bullet": None,
                              "runs": [{"text": "Use", "size": size, "font": "Space Mono"}],
                              "slides": {"indent_first": 0, "indent_start": 0}}],
              "box": {"valign": "middle", "scale": 720 / 453.54}}
        im = np.full((int(300 * px), int(453.54 * px), 3), 240, dtype=np.int16)
        im[int(62 * px):int(90 * px), int((50 + lsb * size) * px):int(150 * px)] = 20
        thumbnail_insets([el], im, px)
        return el
    assert "insets" not in read()["box"], "no metrics: 4.8 pt in is Slides' inset"
    monospace_face(monkeypatch, lsb=lsb)
    assert starting_bearing(read()["paragraphs"]) == pytest.approx(lsb * size)
    el = read()
    assert el["box"]["insets"] == 0 and el["anchor"] == [round(54.22 - 6.7 / (720 / 453.54), 2), 90.0]


def centred_title(valign: str = "top"):
    return {"kind": "text", "bbox": [50.0, 50.0, 400.0, 100.0], "anchor": [0.0, 70.0], "wrap_width": 341.5,
            "paragraphs": [{"align": "center", "bullet": None,
                            "runs": [{"text": "Colors", "size": 20.0, "font": "Space Mono"}],
                            "slides": {"indent_first": 0, "indent_start": 0, "line_spacing": 1.0}}],
            "box": {"valign": valign, "scale": 1920 / 453.54}}


def test_a_centred_box_with_no_insets_is_told_by_its_rows(monkeypatch):
    """devfest2020's Space Mono headings are centred: no side edge to read, and "Colors" does not
    reach a generic face's cap height. Their first line's glyph tops, from the deck's own font, stand
    one top inset higher than Slides' default puts them."""
    import numpy as np

    from beamer2slides.adopt import snapped_line_box
    from beamer2slides.deck_thumbs import inset_rows, thumbnail_insets
    from beamer2slides.emit import BASELINE_A
    monospace_face(monkeypatch)
    px = 4.0
    e = centred_title()
    scale = e["box"]["scale"]
    inset = BASELINE_A / scale
    base = 50.0 + inset + snapped_line_box(20.0, 1.0, scale, False)[0]
    default_top = base - 0.7 * 20.0

    def thumb(top):
        im = np.full((int(255 * px), int(453.54 * px), 3), 240, dtype=np.int16)
        for k in range(6):          # six letters' stems
            x = 191 + 12 * k
            im[int(round(top * px)):int(round((top + 14) * px)), int(x * px):int((x + 2.5) * px)] = 20
        return im

    for top, told in ((default_top - inset, 0), (default_top, 1), (default_top - inset / 2, None)):
        el = centred_title()
        assert inset_rows(el, [el], el["paragraphs"], thumb(top), px) == told, top
    el = centred_title()
    thumbnail_insets([el], thumb(default_top - inset), px)
    assert el["box"]["insets"] == 0
    assert el["anchor"] == [0.0, round(70.0 - inset, 2)], "the baseline goes up by the top inset, not sideways"
    el = centred_title()
    thumbnail_insets([el], thumb(default_top), px)
    assert "insets" not in el["box"]
    el = centred_title("middle")
    thumbnail_insets([el], thumb(default_top - inset), px)
    assert "insets" not in el["box"], "a middle-aligned stack does not move with its insets"
    el, picture = centred_title(), {"kind": "image", "bbox": [200.0, 40.0, 240.0, 70.0]}
    thumbnail_insets([el, picture], thumb(default_top - inset), px)
    assert "insets" not in el["box"], "a picture over the line's rows is ink too"


def test_a_panel_under_a_box_that_runs_off_the_slide_still_counts_as_ground():
    """devfest2020's "50%" box runs 800 pt past the slide's right edge; the panel it stands on ends
    at the slide edge, and only has to hold the strip that is read."""
    from beamer2slides.deck_thumbs import crossed
    e = {"kind": "text", "bbox": [100.0, 50.0, 1000.0, 120.0]}
    panel = {"kind": "shape", "bbox": [0.0, 0.0, 453.54, 255.12]}
    assert not crossed(e, [panel, e], (99.0, 50.0, 110.0, 120.0))
    assert crossed(e, [panel, e], (99.0, 50.0, 460.0, 120.0)), "past the panel is not its ground"
    narrow = {"kind": "shape", "bbox": [90.0, 0.0, 105.0, 255.12]}
    assert crossed(e, [narrow, e], (99.0, 50.0, 110.0, 120.0))


def test_a_box_starting_left_of_the_slide_is_read_from_the_slides_edge():
    """ap-bio-stats' full-width boxes start 1.9 pt left of the slide, where the thumbnail has no
    pixels: the words' first ink column counts from the slide's edge, not from the box's."""
    import numpy as np

    from beamer2slides.deck_thumbs import thumbnail_insets
    px = 4.0
    el = {"kind": "text", "bbox": [-4.0, 50.0, 460.0, 100.0], "anchor": [2.7, 75.0], "wrap_width": 450.6,
          "paragraphs": [{"align": "left", "bullet": None, "runs": [{"text": "Words", "size": 12.0}],
                          "slides": {"indent_first": 0, "indent_start": 0}}],
          "box": {"valign": "middle", "scale": 1.0}}
    panel = {"kind": "shape", "bbox": [-10.0, -10.0, 500.0, 300.0]}
    im = np.full((int(300 * px), int(453.54 * px), 3), 240, dtype=np.int16)
    im[int(66 * px):int(75 * px), int(2.9 * px):int(60 * px)] = 20       # Slides' 6.7 pt inset, 0.2 pt bearing
    thumbnail_insets([panel, el], im, px)
    assert "insets" not in el["box"]


def test_shaped_scripts_are_not_read_by_their_glyph_tops(monkeypatch):
    """An Arabic letter's joined form is not the glyph its code point maps to: arabic-training's
    lists read as inset-free from the cmap's heights, and lost 0.04 a slide."""
    from beamer2slides.deck_thumbs import inset_rows
    monospace_face(monkeypatch)
    el = centred_title()
    el["paragraphs"][0]["runs"][0]["text"] = "التطبيقات"
    assert inset_rows(el, [el], el["paragraphs"], None, 4.0) is None


def test_an_overflowing_box_is_read_where_its_words_stand():
    """gdg24's code listings hold more lines than their middle-aligned box: the lines that start at
    the box's edge stand above and below it, and only the indented ones inside it."""
    import numpy as np

    from beamer2slides.deck_thumbs import text_rows, thumbnail_insets
    px = 4.0

    def element(lines):
        paras = [{"align": "left", "bullet": None, "runs": [{"text": "x = 1", "size": 8.0}],
                  "slides": {"indent_first": 0, "indent_start": 0, "line_spacing": 1.0}} for _ in range(lines)]
        return {"kind": "text", "bbox": [50.0, 50.0, 200.0, 70.0], "wrap_width": 141.5, "paragraphs": paras,
                "box": {"valign": "middle", "scale": 720 / 453.54}}

    im = np.full((int(300 * px), int(453.54 * px), 3), 240, dtype=np.int16)
    im[int(40 * px):int(46 * px), int(50.4 * px):int(90 * px)] = 20        # `def f():` over the box
    im[int(55 * px):int(61 * px), int(70 * px):int(110 * px)] = 20         # its indented body in it
    assert text_rows(element(1), element(1)["paragraphs"], 300) == (50.0, 70.0)
    top, bottom = text_rows(element(4), element(4)["paragraphs"], 300)
    assert round(top, 2) == 40.96 and round(bottom, 2) == 79.04, "4 x 9.52 pt, centred"
    assert text_rows(element(10), element(10)["paragraphs"], 300)[0] < 40
    el = element(10)
    thumbnail_insets([el], im, px)
    assert el["box"].get("insets") == 0
    el = element(1)
    thumbnail_insets([el], im, px)
    assert "insets" not in el["box"], "a box its words fit reads its own rows only"


def test_a_middle_aligned_box_stacks_its_last_paragraphs_space_below():
    """intro-lecture's titles (10 pt below) stood 5 pt low without it; top-aligned boxes and gdg24's
    turned stickers (ELLIPSE) show none of it."""
    last = {"slides": {"space_below": 10.0}}
    assert adopt.trailing_space(last, "middle") == 10.0 and adopt.trailing_space(last, "bottom") == 10.0
    assert adopt.trailing_space(last, "top") == 0.0 and adopt.trailing_space(last, "middle", "ELLIPSE") == 0.0
    el = {"kind": "text", "bbox": [0, 0, 100, 80], "box": {"scale": 2.0, "valign": "middle"},
          "paragraphs": [{"runs": [{"text": "add(6, 7)", "size": 15.0}], "slides": {}},
                         {"runs": [{"text": "???", "size": 24.0}], "slides": {"space_below": 10.0}}]}
    text = adopt.text_box_latex(el, adopt.Context(), "")
    assert "\\begin{slidebox}[middle,tail=5]" in text, text
    el["box"]["valign"] = "top"
    assert "tail=" not in adopt.text_box_latex(el, adopt.Context(), "")


def test_a_line_as_wide_as_its_box_stays_on_one_line():
    """The measure is scaled as the words are: sizes are written to 0.01 page pt, so gdg24's 15 pt
    code (9.44875 page pt, set at 9.45) needs a measure 0.013% wider to keep its 621.0 pt line, and
    sc-dark-minimal's 88 pt heading (27.714, set at 27.71) one that much narrower to wrap."""
    scale = 720 / 453.54
    code = [{"runs": [{"text": "x", "size": 9.45}], "slides": {"size": 15.0}}]
    assert 69 * 0.6 * 9.45 <= adopt.measure(391.18, code, scale) < 391.26
    heading = [{"runs": [{"text": "About Us.", "size": 27.71}], "slides": {"size": 87.99}}]
    assert adopt.measure(129.13, heading, 3.1750231512104774) < 129.13
    odd = [{"runs": [{"text": "x", "size": 12.0}], "slides": {"size": 30.0}}]
    assert adopt.measure(100.0, odd, 2.0) == 100.0 + adopt.FIT_SLACK, "not a rounding: left alone"


def title_deck(style: dict) -> dict:
    """A title whose layout placeholder is bold Arial and whose one run says `style`."""
    layout_title = box("L_title", para("", runs=[("", {"bold": True, "fontFamily": "Arial", "fontSize": pt(28)})]),
                       placeholder={"type": "TITLE"})
    return deck(box("s_title", para("JRuby", runs=[("JRuby InvokeDynamic", style)]), h=60,
                    placeholder={"type": "TITLE", "parentObjectId": "L_title"}),
                layout_elements=[layout_title])


def stroked(el: dict, stroke_em: float):
    """A 1600 px wide thumbnail with letter-like bars `stroke_em` wide inside the element's box."""
    import numpy as np
    px = 1600 / 453.54
    im = np.full((900, 1600, 3), 255, dtype=np.uint8)
    z = max(r["size"] for p in el["paragraphs"] for r in p["runs"])
    w = max(1, int(round(stroke_em * z * px)))
    x0, y0, x1, y1 = (int(v * px) for v in el["bbox"])
    top = y0 + (y1 - y0) // 4
    for x in range(x0 + 10, x1 - 10, 3 * w):
        im[top:top + int(0.7 * z * px), x:x + w] = 0
    return im


def test_a_run_that_only_names_its_font_takes_the_weight_its_thumbnail_shows():
    """jruby-ja's Tahoma titles read `bold: false` at weight 400 under a bold layout title and are
    drawn bold; drawings-basics has such titles drawn bold and others, identical in the API, drawn
    regular. The thumbnail's stroke width settles it (deck_ir.thumbnail_weights)."""
    named = {"bold": False, "fontFamily": "Tahoma", "weightedFontFamily": {"fontFamily": "Tahoma", "weight": 400}}
    plain = text_of(deck_ir(title_deck(named), foreign=True), "s_title")
    run = plain["paragraphs"][0]["runs"][0]
    assert run["bold"] is False and "weight_unsure" not in run, "no thumbnail: the API's word"

    def bold_with(style, stroke):
        thumb = stroked(plain, stroke)
        el = text_of(deck_ir(title_deck(style), foreign=True, thumbnails=lambda n: thumb), "s_title")
        run = el["paragraphs"][0]["runs"][0]
        assert "weight_unsure" not in run
        return run["bold"]

    assert bold_with(named, 0.15) is True
    assert bold_with(named, 0.07) is False
    # a `bold: false` with no weight beside it is the person's own: jruby-ja's numbered titles
    assert bold_with({"bold": False}, 0.15) is False


def test_the_stroke_width_of_a_thumbnail_in_em():
    from beamer2slides.deck_thumbs import BOLD_STROKE_EM, stroke_em
    el =text_of(deck_ir(title_deck({}), foreign=True), "s_title")
    thin, thick = (stroke_em(el, [el], stroked(el, w).astype("int16"), 1600 / 453.54) for w in (0.07, 0.15))
    assert thin == pytest.approx(0.07, abs=0.02) and thick == pytest.approx(0.15, abs=0.03)
    assert thin < BOLD_STROKE_EM < thick
    assert stroke_em(el, [el, {"kind": "image", "bbox": [0, 0, 400, 400]}],
                     stroked(el, 0.15).astype("int16"), 1600 / 453.54) is None, "a picture over it is ink too"


def test_a_hebrew_first_line_is_placed_by_its_baseline():
    """Hebrew has no capitals for `top_drift` to read: hebrew-lesson's boxes sit 3.6 pt high (a
    PowerPoint import's insets), which the row where the letters' ink thins out shows. Underlines are
    cleared first; Latin and CJK first lines are left to the cap rule."""
    import numpy as np

    from beamer2slides.deck_thumbs import baseline_drift
    px, scale, z = 4.0, 2.0, 12.0

    def element(text):
        return {"kind": "text", "bbox": [50.0, 50.0, 250.0, 120.0], "anchor": [53.0, 65.0],
                "box": {"valign": "top", "scale": scale},
                "paragraphs": [{"runs": [{"text": text, "size": z}], "slides": {}}]}

    def thumb(baseline, underline=False, bold_tops=False):
        im = np.full((600, 1200, 3), 250, dtype=np.int16)
        B = int(round(baseline * px))
        for x in range(int(55 * px), int(200 * px), 12):
            im[B - int(0.6 * z * px):B, x:x + 3] = 10           # letter stems down to the baseline
            if bold_tops:
                im[B - int(0.6 * z * px):B - int(0.5 * z * px), x:x + 10] = 10
        if underline:
            im[B + 4:B + 6, int(55 * px):int(200 * px)] = 10
        return im

    el = element("שלום עולם")
    paras = el["paragraphs"]
    for shown in (61.4, 65.0):
        for kw in ({}, {"underline": True}, {"bold_tops": True}):
            got = baseline_drift(el, [el], paras, thumb(shown, **kw), px)
            assert got == pytest.approx((shown - 65.0) * scale, abs=0.6), (shown, kw)
    latin = element("Hello world")
    assert baseline_drift(latin, [latin], latin["paragraphs"], thumb(61.4), px) is None
    cjk = element("日本語")
    assert baseline_drift(cjk, [cjk], cjk["paragraphs"], thumb(61.4), px) is None


def test_list_items_collapse_their_spacing_and_other_paragraphs_do_not(tmp_path):
    """Between two list items COLLAPSE_LISTS drops spaceBelow; between two plain paragraphs it is
    the gap Slides leaves (12 pt / scale more than the plain pitch)."""
    items = frame_of(source(tmp_path, body_deck()))
    plain = deck(box("s_p", para("One", style={"spaceBelow": pt(12), "spacingMode": "COLLAPSE_LISTS"})
                     + para("Two", style={"spaceBelow": pt(12), "spacingMode": "COLLAPSE_LISTS"})))
    # `space=` on a paragraph after the first is the space between the two line boxes, \prevdepth less it
    shifts = lambda text: [-float(k) for k in re.findall(r"\n\s*\\slidepar\[[^\]]*space=([-\d.]+)", text)]
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
    assert "\\slidepar[space=13.86]{body}{One}" in kept and "\\slidepar{body}{One}" in grows
    for frame in (kept, grows):
        assert "\\slidepar[space=13.86]{body}{Two}" in frame, "the second one gets its 22 pt"


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
    tight_text, roomy_text = source(tmp_path, fitted(59)), source(tmp_path / "r", fitted(72))
    tight, roomy = frame_of(tight_text), frame_of(roomy_text)
    x = 100 / scale
    geometry = re.search(r"\\begin\{slidebox\}\{([\d.]+),[\d.]+,([\d.]+),", tight)
    assert abs(float(geometry[2]) - 300 / scale) < 0.06 and geometry[1] == f"{x:.0f}"
    assert "\\setslideinset" not in tight_text and "inset=" not in tight, "the default inset is 0"
    assert f"{{{x + 6.7 / scale:.1f}," in roomy and f"\\setslideinset{{{6.48 / scale:.2f}}}" in roomy_text


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


def test_the_side_a_boxs_words_start_on_says_whose_side_insets_an_import_kept():
    """hebrew-lesson is a .pptx import like comps-analysis (its boxes stand PowerPoint's 3.6 pt high),
    but its right-aligned Hebrew starts 3.2 pt further from the box's right edge than PowerPoint's
    3.6 pt side inset put it: its boxes kept Slides' own sides. `side_gap` reads a box's start side
    (the right one for right-to-left text) and `pptx_insets` takes the deck's median."""
    import numpy as np

    from beamer2slides.deck_thumbs import PPTX_INSET_Y, pptx_insets, side_gap, side_inset
    px, scale = 4.0, 2.0

    def element(rtl, oid="e"):
        p = {"align": "right" if rtl else "left", "bullet": None, "runs": [{"text": "שלום" if rtl else "Hi", "size": 10.0}],
             "slides": {"indent_first": 0, "indent_start": 0}}
        if rtl:
            p["direction"] = "rtl"
        return {"kind": "text", "id": oid, "bbox": [50.0, 50.0, 250.0, 100.0], "paragraphs": [p],
                "box": {"valign": "top", "scale": scale}}

    def thumb(x0, x1):
        im = np.full((600, 1200, 3), 250, dtype=np.int16)
        im[int(55 * px):int(62 * px), int(x0 * px):int(x1 * px)] = 10
        return im

    # words 3.4 IR pt (6.8 Slides pt) in from the start side, whichever side that is
    assert side_gap(element(False), [], thumb(53.4, 120), px) == pytest.approx(6.8, abs=0.3)
    assert side_gap(element(True), [], thumb(180, 246.6), px) == pytest.approx(6.8, abs=0.3)
    centred = element(True)
    centred["paragraphs"][0]["align"] = "center"
    assert side_gap(centred, [], thumb(180, 246.6), px) is None, "no line starts at a side"
    over = {"kind": "image", "bbox": [40.0, 40.0, 260.0, 110.0]}
    assert side_gap(element(True), [element(True), over], thumb(180, 246.6), px) is None
    assert side_inset(element(True), 6.8) == pytest.approx(6.8 - 0.04 * 10 * scale)

    def imported(sides):
        slides = [{"elements": [element(True, str(k)) for k in range(4)]}]
        drifts = [(e, PPTX_INSET_Y - 7.2) for e in slides[0]["elements"]]
        pptx_insets(slides, drifts, sides)
        return slides[0]["elements"][0]["box"]

    assert imported([6.1, 6.5, 7.3])["inset_y"] == PPTX_INSET_Y
    assert "inset_x" not in imported([6.1, 6.5, 7.3]), "Slides' own sides"
    assert imported([3.3, 3.5, 6.9])["inset_x"] == PPTX_INSET_Y, "comps-analysis: PowerPoint's"
    # words never start outside their box: arabic-training's negative readings are other ink
    assert "inset_x" not in imported([-3.14, -1.4, -0.95, 5.47, 5.47, 6.62]), "arabic-training"
    assert imported([])["inset_x"] == PPTX_INSET_Y, "nothing to go by: the import's"


def test_single_spaced_lines_are_a_whole_number_of_pixels_apart():
    """24 pt lines at 100% stand 28.5 pt apart on the thumbnails, not 1.2 em (hebrew-lesson,
    arabic-training, ap-bio-stats, comps-analysis): 38 CSS pixels. Small type, other spacings and
    pages wider than 960 pt are not snapped."""
    scale = 2.0
    pitch = lambda z, r=1.0, on=True: sum(adopt.snapped_line_box(z / scale, r, scale, on)) * scale
    assert pitch(24) == pytest.approx(28.5)
    assert pitch(18) == pytest.approx(21.75)
    assert pitch(16) == pytest.approx(19.5)
    assert pitch(14) == pytest.approx(16.8), "gdg24's 14 pt body copy"
    assert pitch(24, on=False) == pytest.approx(28.8)
    assert pitch(14, 1.15) == pytest.approx(14 * 1.2 * 1.15)
    above, _ = adopt.snapped_line_box(12.0, 1.0, scale)
    assert above == adopt.line_box(12.0, 1.0)[0], "the baseline stays; the depth takes the difference"

    def snap_of(width):
        d = deck(box("s_s", para("Words")))
        d["pageSize"] = {"width": pt(width), "height": pt(width * 9 / 16)}
        return text_of(deck_ir(d, foreign=True), "s_s")["box"].get("snap")

    assert snap_of(720) is True
    assert snap_of(1440) is None, "the 1440 pt SlidesCarnival decks' lines are 1.2 em apart"


def test_text_after_a_tab_starts_at_the_next_default_stop(tmp_path):
    """creandum-board's tables of figures are words and tabs: Slides jumps to the next multiple of
    36 pt from the text's edge, TeX's space did not move them at all."""
    d = deck(box("s_t", para("x", runs=[("Revenue\t12\tup", {})])))
    text = source(tmp_path, d)
    frame = frame_of(text)
    assert "\\newcommand\\slidestab" in macros(tmp_path) and "\\usepackage{slides}" in text
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
    assert re.search(r"\\slidepar(\[[^\]]*\])?\{[\w-]+\}\{\}\s*\n\s*\\end\{slidebox\}", frame_of(source(tmp_path, body("MIDDLE"))))


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
        # the stack ends on the depth of its last paragraph's style
        return float(re.findall(r"depth=([\d.]+)\}", source(tmp_path / str(spacing), d))[-1])
    assert ending(115) > ending(100)
    assert ending(150) == ending(170) == ending(100)


def test_every_font_a_text_box_selects_spaces_its_words_with_its_own_space(tmp_path):
    """\\spaceskip was set once, by \\slidesize, from the font selected then: a paragraph whose
    typeface switch came after it (sc-dark-modern's Courier Prime) was spaced with the sans font's
    narrow space."""
    source(tmp_path, body_deck())
    sty = macros(tmp_path)
    assert "\\AddToHook{selectfont}{\\ifslidesspace\\spaceskip=\\fontdimen2\\font" in sty
    assert "\\slidesbox}{\\slidesspacetrue" in sty


def test_a_box_whose_base_is_bold_writes_its_regular_words_regular(tmp_path):
    """`runs_latex` only ever wrote \\textbf, so under a bold base the regular words stayed bold."""
    d = deck(box("s_b", para("x", runs=[("Mostly bold words here", {"bold": True}), (" plain", {})])))
    text = source(tmp_path, d)
    frame = frame_of(text)
    style = re.search(r"\\slidetext\{[^}]*\}\{([\w-]+)\}", frame)[1]
    assert re.search(rf"\\slidestyle\{{{style}\}}\{{[^}}]*weight=bold", text), "the box's base is bold"
    assert "\\textmd{\\ plain}" in frame, "its space is a regular one too"


def test_a_soft_break_anywhere_is_a_line_break_that_compiles(tmp_path):
    """`\\\\` at a paragraph's start, after \\centering or at an \\item's start stopped whole decks
    (cs161-net 13, arabic-training 6-8): a break is written between styled pieces, in horizontal mode."""
    d = deck(box("s_s", para("x", runs=[("\x0bopening", {}), ("bold\x0bword", {"bold": True})],
                             style={"alignment": "CENTER"})))
    frame = frame_of(source(tmp_path, d))
    assert "\\\\" not in frame
    assert frame.count("\\slidebreak") == 2
    assert "\\newcommand\\slidebreak{\\unskip\\break}" in macros(tmp_path)
    # most of the words are bold, so bold is the box's base and the regular word says so
    assert "\\slidebreak \\textmd{opening}bold\\slidebreak word" in frame


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
    from beamer2slides.deck_thumbs import pptx_insets

    def box(valign="top"):
        return {"kind": "text", "box": {"valign": valign, "scale": 2.0}, "anchor": [10.0, 20.0]}

    hit, miss, other, low = box(), box(), box(), box("bottom")
    slides = [{"elements": [hit, miss, other, low]}]
    pptx_insets(slides, [(hit, -3.5), (miss, 0.2)])
    assert hit["box"]["inset_y"] == 3.6 and hit["anchor"] == [10.0, 18.2]
    assert "inset_y" not in miss["box"] and "inset_y" not in other["box"]
    assert "inset_x" not in hit["box"], "a lone box keeps Slides' sides (ap-bio-stats)"
    a, b, c, d = box(), box(), box(), box("bottom")
    pptx_insets([{"elements": [a, b, c, d]}], [(a, -3.6), (b, -3.9), (c, -3.2)])
    assert d["box"]["inset_y"] == 3.6 and d["anchor"] == [10.0, 21.8]
    assert all(e["box"]["inset_x"] == 3.6 for e in (a, b, c, d)), "a deck imported whole (comps-analysis)"


def test_a_deck_is_imported_whole_when_most_sane_readings_are_nearer_powerpoints_insets():
    """arabic-training: Arial and Times read -3.7 and -4.0, Calibri's Arabic -2.3 (outside the hit
    window, nearer -3.6 than 0 all the same), two lines misread by 8 pt. The deck came whole, so its
    unmeasured boxes get the insets; a measured box outside the window keeps Slides'."""
    from beamer2slides.deck_thumbs import pptx_insets

    def box():
        return {"kind": "text", "box": {"valign": "top", "scale": 2.0}, "anchor": [10.0, 20.0]}

    read = [box() for _ in range(6)]
    unmeasured = box()
    pptx_insets([{"elements": read + [unmeasured]}], list(zip(read, [-2.28, -4.01, -3.71, -2.36, 7.99, -8.08])))
    assert unmeasured["box"]["inset_y"] == 3.6
    assert [("inset_y" in e["box"]) for e in read] == [False, True, True, False, False, False]
    read = [box() for _ in range(4)]
    unmeasured = box()
    pptx_insets([{"elements": read + [unmeasured]}], list(zip(read, [-3.6, 0.1, 0.2, 9.0])))
    assert "inset_y" not in unmeasured["box"], "one in three sane readings is a mixed deck"


def test_a_bulleted_right_to_left_first_line_is_measured_by_its_baseline():
    """arabic-training's lists: the bullet stands beside the words, the baseline still says where
    the line is."""
    import numpy as np

    from beamer2slides.deck_thumbs import top_drift
    px, scale, z = 4.0, 2.0, 12.0
    el = {"kind": "text", "bbox": [50.0, 50.0, 250.0, 120.0], "anchor": [53.0, 65.0],
          "box": {"valign": "top", "scale": scale},
          "paragraphs": [{"bullet": {"glyph": "●"}, "runs": [{"text": "مرحبا بالعالم", "size": z}], "slides": {}}]}
    im = np.full((600, 1200, 3), 250, dtype=np.int16)
    B = int(round(63.2 * px))
    for x in range(int(55 * px), int(200 * px), 12):
        im[B - int(0.6 * z * px):B, x:x + 3] = 10
    assert top_drift(el, [el], im, px) == pytest.approx((63.2 - 65.0) * scale, abs=0.6)


def test_a_box_with_powerpoint_insets_starts_its_text_3_6_pt_higher():
    el = {"kind": "text", "bbox": [0, 0, 100, 50], "box": {"scale": 1.0, "valign": "top"},
          "paragraphs": [{"runs": [{"text": "Hi", "size": 10.0}], "slides": {}}]}
    plain = adopt.text_box_latex(el, adopt.Context(), "")
    el["box"]["inset_y"] = 3.6
    assert "inset=6.48" in plain and "inset=2.88" in adopt.text_box_latex(el, adopt.Context(), "")
    assert "{6.7,0,86.61,50}" in plain
    el["box"]["inset_x"] = 3.6
    assert "{3.6,0,92.81,50}" in adopt.text_box_latex(el, adopt.Context(), "")


def test_the_thumbnails_measure_a_first_line_and_skip_what_crosses_it():
    import numpy as np
    from beamer2slides.deck_thumbs import ink_widths
    px = 4.0
    im = np.full((int(100 * px), int(200 * px), 3), 240, dtype=np.int16)
    im[int(24 * px):int(30 * px), int(20 * px):int(80 * px)] = 20       # the first line's words
    im[int(36 * px):int(42 * px), int(20 * px):int(150 * px)] = 20      # a longer second line

    def element():
        return {"kind": "text", "bbox": [10.0, 10.0, 190.0, 60.0], "anchor": [16.7, 30.0], "box": {"scale": 1.0},
                "paragraphs": [{"align": "left", "bullet": None, "runs": [{"text": "Some words", "size": 8.0}]},
                               {"align": "left", "bullet": None, "runs": [{"text": "More", "size": 8.0}]}]}
    el = element()
    ink_widths([el], im, px)
    assert el["ink_width"] == pytest.approx(60.0, abs=0.3)
    el = element()
    ink_widths([el, {"kind": "image", "bbox": [100.0, 20.0, 120.0, 40.0]}], im, px)
    assert "ink_width" not in el


def test_a_stand_in_is_condensed_to_the_widths_the_thumbnails_show(tmp_path):
    """comps-analysis's Bodoni is set in Libre Bodoni, 6% wider than Slides draws it: its titles
    wrapped a word. The deck's own font is never touched (Pacifico's kerning read as 3% narrow)."""
    from .test_adopt_media import tiny_font
    path = tmp_path / "TinySans-Regular.ttf"
    path.write_bytes(tiny_font("Tiny Sans"))
    files = {"UprightFont": path}
    # five "A"s at 10 pt: 3000 units of advance less the last one's 100 of right bearing = 29 pt of ink

    def sample(width, text="AAAAA"):
        return {"ink_width": width, "paragraphs": [{"runs": [{"text": text, "size": 10.0, "font": "Deck Serif"}]}]}
    target = {"slides": [{"elements": [sample(26.1), sample(26.2), sample(15.0)]}]}
    assert adopt.font_widths("Deck Serif", files, target) == pytest.approx(0.9, abs=0.005)
    assert adopt.stretch("Deck Serif", "TinySans", files, target) == ",FakeStretch=0.902"
    assert adopt.stretch("Tiny Sans", "TinySans", files, target) == "", "the deck's own font"
    near = {"slides": [{"elements": [sample(28.8), sample(29.1)]}]}
    assert adopt.font_widths("Deck Serif", files, near) is None, "within 2%"
    lone = {"slides": [{"elements": [sample(26.1)]}]}
    assert adopt.font_widths("Deck Serif", files, lone) is None, "one measure is not enough"


def test_a_paragraph_of_two_sizes_is_spaced_line_by_line():
    """comps-analysis: a 26.7 pt lead-in, 21.3 pt words; the line they wrap onto is 21.3 pt apart."""
    el = {"kind": "text", "bbox": [0, 0, 100, 80], "box": {"scale": 1.0, "valign": "top"},
          "paragraphs": [{"runs": [{"text": "First step: ", "size": 20.0},
                                   {"text": "two words", "size": 10.0}], "slides": {}},
                         {"runs": [{"text": "Next", "size": 10.0}], "slides": {}}]}
    ctx = adopt.Context()
    tex = adopt.text_box_latex(el, ctx, "")
    styles = "\n".join(adopt.style_definitions(ctx))
    small, big = adopt.line_box(10.0, 1.0), adopt.line_box(20.0, 1.0)
    first = re.search(r"\\slidepar\[mixed\]\{([\w-]+)\}", tex)
    assert first, "a paragraph of several sizes is `mixed`: each word carries its line box"
    assert re.search(rf"\\slidestyle\{{{first[1]}\}}\{{[^}}]*pitch={adopt.num(sum(small))},", styles)
    assert f"\\slidestrut{{{adopt.num(small[0])}}}{{{adopt.num(small[1])}}}words" in tex
    assert f"\\slidestrut{{{adopt.num(big[0])}}}{{{adopt.num(big[1])}}}step:" in tex
    assert "\\lineskiplimit=0bp" in adopt.SLIDES_TEXT
    # the next paragraph is spaced from the depth TeX recorded, not from a guessed size
    assert f"prevdepth={adopt.num(sum(small) - small[0])}" in tex


def prose(*paras: dict, scale: float = 1.0) -> dict:
    return {"kind": "text", "bbox": [0, 0, 300, 200], "box": {"scale": scale, "valign": "top"},
            "paragraphs": list(paras)}


def words(text: str, size: float = 10.0, **run) -> dict:
    return {"text": text, "size": size, "font": "Calibri", "family": "sans", "bold": False, "italic": False,
            "color": None, **run}


def test_the_gap_between_two_paragraphs_is_the_bigger_of_their_spaces():
    """ap-bio-stats slide 52: 11 pt below one paragraph and 11 pt above the next stand them 11 pt apart
    on the thumbnail, not 22."""
    def gap(below, above):
        return adopt.text_box_latex(prose({"runs": [words("One")], "slides": {"space_below": below}},
                                          {"runs": [words("Two")], "slides": {"space_above": above}}),
                                    adopt.Context(), "")
    assert gap(11, 11) == gap(11, 0) == gap(0, 11) != gap(0, 0)


def test_each_list_item_says_whether_its_side_of_the_gap_collapses():
    """creandum-board: the first item is NEVER_COLLAPSE with 3 pt below, the next COLLAPSE_LISTS."""
    def items(mode):
        bullet = {"text": "●", "size": 10.0}
        return adopt.text_box_latex(prose(
            {"runs": [words("One")], "bullet": bullet, "slides": {"space_below": 3, "spacing_mode": mode}},
            {"runs": [words("Two")], "bullet": bullet, "slides": {"spacing_mode": "COLLAPSE_LISTS"}}),
            adopt.Context(), "")
    assert items("NEVER_COLLAPSE") != items("COLLAPSE_LISTS")
    assert "space=" not in items("COLLAPSE_LISTS"), "no space between the two line boxes"
    assert "\\slidepar[space=3]" in items("NEVER_COLLAPSE")


def test_a_bulleted_line_with_tabs_stands_them_on_the_default_stops():
    """creandum-board's "DD/MM/YY XX am<TAB><TAB>Other important date" items."""
    tex = adopt.text_box_latex(prose({"runs": [words("9 am\t\tBoard")], "bullet": {"text": "●", "size": 10.0},
                                      "slides": {"indent_start": 18, "indent_first": 0}}), adopt.Context(), "")
    assert tex.count("\\slidestab{36.00pt}") == 2 and "\\global\\slidesx=18.00pt" in tex


def test_spaces_are_kept_however_many_and_as_wide_as_their_own_font():
    """ap-bio-stats' literal "•  " bullets: an Arial run in a Calibri paragraph whose two spaces stand
    the text off by two Arial spaces - folded into one Calibri space, every item's text came 2 pt short."""
    ctx = adopt.Context()
    ctx.font_switches = {"Arial": "\\adoptfontA"}
    base = {"size": 10.0, "font": "Calibri", "family": "sans", "bold": False, "italic": False, "color": None}
    tex = adopt.runs_tex([words("•  ", font="Arial"), words("Mathematically")], base, ctx, "\\break ")
    assert tex == "{\\adoptfontA •\\ \\ }Mathematically"
    # the same style on both sides of a run boundary: the two spaces are two, and none ends the paragraph
    tex = adopt.runs_tex([words("a "), words(" b  ", color="#ff0000")], base, ctx, "\\break ")
    assert tex.startswith("a \\") and tex.endswith("{b}")


def test_a_superscript_does_not_raise_its_line_box():
    """ap-bio-stats slide 4: the strut inside "E = mc²"'s superscript made its line 4 pt taller."""
    el = prose({"runs": [words("Law ", size=20.0), words("E = mc"), words("2", script="super")], "slides": {}})
    tex = adopt.text_box_latex(el, adopt.Context(), "")
    sup = tex[tex.index("\\textsuperscript"):]
    assert "\\slidestrut" not in sup.split("}")[0]
    assert re.search(r"\\slidestrut\{[\d.]+\}\{[\d.]+\}\\textsuperscript\{2\}", tex)


def test_deck_ir_keeps_a_run_weight_other_than_regular_and_bold():
    d = deck(box("s_w", para("x", runs=[("Semi", {"fontFamily": "Open Sans", "weightedFontFamily":
                                                  {"fontFamily": "Open Sans", "weight": 600}}),
                                         (" plain", {"fontFamily": "Open Sans"})])))
    runs = text_of(deck_ir(d, foreign=True), "s_w")["paragraphs"][0]["runs"]
    assert [r.get("weight") for r in runs] == [600, None]

