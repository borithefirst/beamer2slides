"""Adopting a deck nobody converted (adopt.py, and what deck_ir(foreign=True) reads for it).

Offline, no TeX and no Google: a hand-built `presentations.get` answer shaped like the template
decks that made this necessary - decoration on the master, more on the layout, a placeholder whose
alignment only the master states, and a connector - then the IR that comes back and the source
`adopt.bootstrap` writes from it.
"""

import json
import re
from pathlib import Path

import pytest

from beamer2slides import adopt
from beamer2slides.deck_ir import deck_ir, family_of

EMU = 12700


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    """Whether this machine has Google Sans installed must not decide what the tests read: every
    test names the fonts it has through `$B2S_FONTS` (`adopt.font_dirs`), and by default none."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def pt(v: float) -> dict:
    return {"magnitude": v * EMU, "unit": "EMU"}


def at(x: float, y: float) -> dict:
    return {"scaleX": 1.0, "scaleY": 1.0, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def solid(hexc: str) -> dict:
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"solidFill": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}}


def shape(oid: str, kind: str, x: float, y: float, w: float, h: float, fill: str | None = None,
          outline: str | None = None, **extra) -> dict:
    props: dict = {"shapeBackgroundFill": solid(fill) if fill else {"propertyState": "NOT_RENDERED"}}
    if outline:
        props["outline"] = {"outlineFill": solid(outline), "weight": pt(2), "propertyState": "RENDERED"}
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y),
            "shape": {"shapeType": kind, "shapeProperties": props, **extra}}


def text_shape(oid: str, words: str, x: float, y: float, w: float, h: float, *, kind: str = "TEXT_BOX",
               placeholder: dict | None = None, align: str | None = None, font: str = "Google Sans",
               size: float = 20.0, colour: str = "0000FF", **extra) -> dict:
    marker: dict = {"style": {}}
    if align:
        marker["style"]["alignment"] = align
    sh = shape(oid, kind, x, y, w, h, **extra)
    sh["shape"]["text"] = {"textElements": [
        {"paragraphMarker": marker},
        {"textRun": {"content": words + "\n", "style": {"fontFamily": font, "fontSize": pt(size),
                                                        "foregroundColor": {"opaqueColor": {"rgbColor": {
                                                            "red": int(colour[0:2], 16) / 255,
                                                            "green": int(colour[2:4], 16) / 255,
                                                            "blue": int(colour[4:6], 16) / 255}}}}}}]}
    if placeholder:
        sh["shape"]["placeholder"] = placeholder
    return sh


def presentation() -> dict:
    """A master with a backdrop, a layout with a card and a connector, two slides on it."""
    master = {"objectId": "m1", "pageElements": [
        {"objectId": "m1_bg", "size": {"width": pt(720), "height": pt(405)}, "transform": at(0, 0),
         "image": {"contentUrl": "https://example.invalid/backdrop.png"}},
        text_shape("m1_ph", "", 100, 100, 400, 60, placeholder={"type": "SUBTITLE"}, align="CENTER")]}
    # the master's placeholder prompt: the text a slide fills in, never drawn
    master["pageElements"][1]["shape"]["text"] = {"textElements": [
        {"paragraphMarker": {"style": {"alignment": "CENTER"}}},
        {"textRun": {"content": "\n", "style": {}}}]}
    layout = {"objectId": "L1", "layoutProperties": {"masterObjectId": "m1", "displayName": "Section"},
              "pageElements": [
                  shape("L1_card", "ROUND_RECTANGLE", 80, 60, 560, 280, fill="FFFFFF", outline="FBBC04"),
                  {"objectId": "L1_line", "size": {"width": pt(100), "height": pt(0)},
                   "transform": at(200, 350),
                   # a line Slides drew says what kind it is; one that says nothing is a freeform
                   "line": {"lineType": "STRAIGHT_CONNECTOR_1", "lineCategory": "STRAIGHT",
                            "lineProperties": {"lineFill": solid("EA4335"), "weight": pt(3),
                                               "endArrow": "FILL_ARROW"}}}]}
    # two slides on the deck's own colour and one that sits on another, so "the deck's colour" is
    # the majority and not a coin toss between two.
    slides = [{"objectId": f"s{n}", "slideProperties": {"layoutObjectId": "L1"},
               "pageElements": [text_shape(f"s{n}_t", f"Slide {n}", 100, 100, 400, 60,
                                           placeholder={"type": "SUBTITLE", "parentObjectId": "m1_ph"})],
               "pageProperties": {"pageBackgroundFill": solid("0005DF" if n == 1 else "F4CCCC")}}
              for n in (0, 1, 2)]
    return {"presentationId": "p", "title": "Template", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [master], "layouts": [layout], "slides": slides}


# ---------------------------------------------------------------- what the IR reads

def test_a_font_is_known_by_its_name():
    """Only the three fonts the converter writes were named, so a deck typed in Space Mono read back
    as prose - and its size came through the wrong width factors with it."""
    assert family_of("Space Mono") == "mono"
    assert family_of("Roboto Mono") == "mono"
    assert family_of("Source Code Pro") == "mono"
    assert family_of("Playfair Display") == "serif"
    assert family_of("EB Garamond") == "serif"
    assert family_of("Google Sans") == "sans"


def test_a_slide_is_given_what_its_layout_and_master_draw():
    ir = deck_ir(presentation(), foreign=True)
    kinds = [(e["kind"], e.get("role"), e.get("inherited")) for e in ir["slides"][0]["elements"]]
    assert kinds == [("image", "figure", "m1"), ("shape", "panel", "L1"), ("shape", "line", "L1"),
                     ("text", "body", None)]
    # the inherited ones come first, so the slide's own words are drawn on top of the decoration
    assert [e["id"] for e in ir["slides"][0]["elements"]][:1] == ["m1~m1_bg"]


def test_the_layouts_own_placeholder_is_not_drawn():
    """It is the slide's to fill, and holds the layout's prompt - printing it would put the
    template's words under the person's."""
    ir = deck_ir(presentation(), foreign=True)
    assert not any(e.get("inherited") and e["kind"] == "text" for e in ir["slides"][0]["elements"])


def test_pull_still_sees_only_the_slide():
    """The same deck read the way `pull` reads it: a source being refined already draws its theme,
    so giving it the layout's decoration would have it drawn twice."""
    ir = deck_ir(presentation())
    assert [e["kind"] for e in ir["slides"][0]["elements"]] == ["text"]
    assert not any(e.get("inherited") for s in ir["slides"] for e in s["elements"])


def test_a_connector_keeps_the_two_points_it_runs_between():
    """A line's box cannot say which way it points; `adopt` needs the ends, and the arrow."""
    line = next(e for e in deck_ir(presentation(), foreign=True)["slides"][0]["elements"]
                if e.get("role") == "line")
    assert line["from"] == [200 / 720 * 453.54, 350 / 720 * 453.54] or line["from"][0] > 0
    assert line["to"][0] > line["from"][0] and line["arrow"] is True
    assert line["outline"] == "#ea4335"


def blank_line_deck() -> dict:
    """A box whose person pressed Return twice: a blank line above the words and one between."""
    pres = presentation()
    box = text_shape("s0_b", "first", 60, 150, 300, 90)
    tes = box["shape"]["text"]["textElements"]
    style = dict(tes[1]["textRun"]["style"])
    box["shape"]["text"]["textElements"] = [
        {"paragraphMarker": {"style": {}}},                      # a blank line above the words
        {"textRun": {"content": "\n", "style": style}},
        {"paragraphMarker": {"style": {}}},
        {"textRun": {"content": "first\n", "style": style}},
        {"paragraphMarker": {"style": {}}},                      # and one between them
        {"textRun": {"content": "\n", "style": style}},
        {"paragraphMarker": {"style": {}}},
        {"textRun": {"content": "second\n", "style": style}},
        {"paragraphMarker": {"style": {}}},                      # trailing: pushes nothing down
        {"textRun": {"content": "\n", "style": style}}]
    pres["slides"][0]["pageElements"].append(box)
    return pres


def test_a_blank_line_someone_typed_is_a_line(tmp_path):
    """Of the 717 paragraphs of the DevFest template, 282 are blank, and dropping one pulls
    everything under it up by a line. A PDF has only the gap a blank line leaves, so classify never
    makes one and `pull`'s IR must not either."""
    ir = deck_ir(blank_line_deck(), foreign=True)
    box = next(e for e in ir["slides"][0]["elements"] if e["kind"] == "text" and e["bbox"][1] > 80)
    assert ["".join(r["text"] for r in p["runs"]) for p in box["paragraphs"]] == \
        [" ", "first", " ", "second"], "the blank lines are kept, the trailing one is not"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    blank = re.findall(r"\\slide(?:par(?:\[[^\]]*\])?|text(?:\[[^\]]*\])?(?:\{[^}]*\})+)\{\}$", text, re.M)
    assert len(blank) == 2, "each blank line takes a line of its own"


def test_pull_still_sees_no_blank_paragraph():
    box = next(e for e in deck_ir(blank_line_deck())["slides"][0]["elements"]
               if e["kind"] == "text" and e["bbox"][1] > 80)
    assert ["".join(r["text"] for r in p["runs"]) for p in box["paragraphs"]] == ["first", "second"]


def test_alignment_comes_from_the_placeholder_it_inherits():
    """The template centres its subtitle on the master and the slide says nothing at all. Reading
    only the slide made centred text left-aligned, which no later round can put right: the loop has
    no translator for alignment."""
    ir = deck_ir(presentation(), foreign=True)
    body = next(e for e in ir["slides"][0]["elements"] if e["kind"] == "text")
    assert [p["align"] for p in body["paragraphs"]] == ["center"]


# ---------------------------------------------------------------- the source it writes

def a_png(path: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (8, 4), (255, 255, 255)).save(path)
    return path


def target_with_pictures(tmp_path: Path) -> dict:
    """The IR as it comes back when the deck did give us the picture files."""
    target = deck_ir(presentation(), foreign=True)
    png = a_png(tmp_path / "backdrop.png")
    for s in target["slides"]:
        for e in s["elements"]:
            if e["kind"] == "image":
                e["file"] = str(png)
    return target


def places(text: str) -> int:
    """How many elements a piece of source places: pictures, text boxes, shapes, lines, tables."""
    return len(re.findall(r"\\begin\{textblock\*\}|\\begin\{slidebox\}|\\slidetext\b|\\slideshape\b|"
                          r"\\sliderect\b|\\slideellipse\b|\\slideline\b|\\slidefreeform\b|\\slidepicture\b|"
                          r"\\begin\{slidetable\}", text))


def placed(text: str) -> int:
    """How many elements the frames of a main.tex place."""
    return places(text.split("\\begin{document}")[1])


def source_for(tmp_path: Path, target: dict | None = None) -> str:
    target = target if target is not None else target_with_pictures(tmp_path)
    return adopt.bootstrap(target, tmp_path / "tree" / "main.tex")


def theme_of(tmp_path: Path) -> str:
    """The beamer theme the bootstrap wrote beside main.tex (adopt_theme.py): what the layout draws."""
    (sty,) = (tmp_path / "tree").glob("beamertheme*.sty")
    return sty.read_text(encoding="utf-8")


def test_the_source_has_a_frame_per_slide_and_compiles_as_beamer(tmp_path):
    text = source_for(tmp_path)
    assert text.count("\\begin{frame}") == 3 and text.count("\\end{frame}") == 3
    assert "\\documentclass[aspectratio=169]{beamer}" in text
    assert text.index("\\begin{document}") < text.index("\\begin{frame}") < text.index("\\end{document}")


def test_every_element_keeps_its_own_place(tmp_path):
    """A foreign deck's geometry is boxes a person dragged, not flow text a theme laid out, so the
    bootstrap places each one; the loop would otherwise spend a round per element escalating flow
    text back into a textblock (`inverse.Planner.geometry`)."""
    text = source_for(tmp_path)
    assert "\\usepackage[absolute,overlay]{textpos}" in text
    # backdrop, card, connector and the subtitle placeholder: the layout's, so said once in its
    # template, and each frame names the layout and hands it its words
    assert places(theme_of(tmp_path)) == 4
    assert placed(text) == 0
    assert text.count("\\begin{frame}[plain,layout=section") == 3
    assert "\\framesubtitle{Slide 0}" in text


def test_a_picture_the_deck_would_not_give_us_is_left_out(tmp_path):
    """Not drawn as an empty box: the loop then reports `element_missing`, which says what happened.
    (The backdrop of the fixture has no file unless the test puts one there.)"""
    text = source_for(tmp_path, deck_ir(presentation(), foreign=True)) + theme_of(tmp_path)
    assert "figures/" not in text
    assert places(text) == 3


def test_a_slide_that_sits_on_another_colour_says_so(tmp_path):
    """Decoration is often a picture with transparency, so the colour under it is not a detail: one
    deck-wide background would make every such slide the wrong colour end to end."""
    text = source_for(tmp_path)
    assert "\\definecolor{deckbg}{HTML}{F4CCCC}" in text            # what most of the deck sits on
    assert "\\setbeamercolor{background canvas}{bg=deckbg}" in text
    # and the one slide that does not says so in its options, which last until the next frame
    name = re.search(r"\\definecolor\{(\w+)\}\{HTML\}\{0005DF\}", text).group(1)
    assert text.count(f"background={name}]") == 1
    assert "bg=deckbg}}" in theme_of(tmp_path), "the next frame starts from the deck's colour again"


def test_a_node_is_drawn_with_its_outline_and_its_rounded_corners(tmp_path):
    text = source_for(tmp_path) + theme_of(tmp_path)
    # the corners are quarter circles of the preset's radius: TikZ's rounded corners, named once
    card = next(l for l in text.splitlines() if "\\sliderect[" in l and "rounded=" in l)
    assert "fill=" in card and "draw=" in card and "line width=" in card


def test_the_base_style_of_a_box_is_set_where_the_box_is(tmp_path):
    """`inverse.runs_latex` writes a run's style only where it differs from a base, which is true of
    a source being refined and false of one written from nothing: without this the crimson 9 pt
    instruction slides and the blue 26 pt section titles both come out black."""
    text = source_for(tmp_path)
    styles = re.findall(r"\\slidestyle\{[^}]*\}\{[^}]*\}", text)
    assert any("color=" in s for s in styles)
    assert "\\color{\\slides@k@color}" in (Path(tmp_path) / "tree" / "slides.sty").read_text(encoding="utf-8")


def test_a_picture_is_copied_into_the_tree(tmp_path):
    """The download sits in the work folder, which is scratch: a source tree that referred to it
    would stop compiling the moment that folder went."""
    source_for(tmp_path)
    text = theme_of(tmp_path)
    assert "figures/" in text
    files = list((tmp_path / "tree" / "figures").glob("*.png"))
    assert files, "the picture is not beside the source"
    assert len(files) == 1, "the same picture on every slide is copied once"


def test_a_slide_on_a_picture_is_drawn_on_it(tmp_path):
    """A converted deck keeps its theme in each slide's background picture, and so do templates
    that were made from one: without it a white title lands on a white page."""
    pres = presentation()
    pres["slides"][1]["pageProperties"]["pageBackgroundFill"] = {
        "stretchedPictureFill": {"contentUrl": "https://example.invalid/bars.png"}}
    png = a_png(tmp_path / "bars.png")
    ir = deck_ir(pres, foreign=True, fetch=lambda url: png.read_bytes(), images=tmp_path / "images")
    assert ir["slides"][1]["background_file"] and "background_file" not in ir["slides"][0]
    assert "background_file" not in deck_ir(pres)["slides"][1], "pull reads no backdrop"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert text.count(",backdrop=figures/") == 1
    assert (tmp_path / "tree" / "figures").is_dir()


# ---------------------------------------------------------------- the typefaces it is written in

def font_folder(tmp_path: Path, *names: str) -> Path:
    """A folder of fonts by name only: nothing here compiles, and nothing here needs to."""
    folder = tmp_path / "fontshelf"
    folder.mkdir(exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"\x00\x01\x00\x00")
    return folder


def test_without_the_decks_typeface_the_source_says_tex_gyre(tmp_path, monkeypatch):
    """Always fontspec, so the source compiles with lualatex and any script: pdflatex stopped a
    deck at its first IPA letter (U+0263). What the machine lacks is set in TeX Gyre Heros."""
    monkeypatch.setenv("B2S_FONTS", str(font_folder(tmp_path)))
    monkeypatch.setenv("B2S_FONT_FETCH", "0")
    text = source_for(tmp_path)
    assert "\\usepackage{fontspec}" in text and "helvet" not in text
    line = next(l for l in text.splitlines() if l.startswith("\\setsansfont"))
    assert line.startswith("\\setsansfont{texgyreheros}[Extension=.otf,")
    # a machine with no font and no network still gives a source that shows emphasis: TeX Gyre is
    # in every TeX distribution and has all four faces, so none of them has to be synthesised
    for style in ("UprightFont=*-regular", "BoldFont=*-bold", "ItalicFont=*-italic",
                  "BoldItalicFont=*-bolditalic"):
        assert style in line
    assert "Fake" not in line


def test_the_deck_is_set_in_its_own_typeface_when_the_machine_has_it(tmp_path, monkeypatch):
    """A foreign deck is written in the person's fonts, not the converter's three, and helvet in
    place of them is ink in the wrong shape on every slide that has words."""
    monkeypatch.setenv("B2S_FONTS", str(font_folder(
        tmp_path, "GoogleSansFlex-Regular.ttf", "GoogleSansFlex-Bold.ttf", "GoogleSansFlex-Italic.ttf")))
    text = source_for(tmp_path)
    assert "\\usepackage{fontspec}" in text and "helvet" not in text
    line = next(l for l in text.splitlines() if l.startswith("\\setsansfont"))
    assert line.startswith("\\setsansfont{GoogleSansFlex}[Path=fonts/,Extension=.ttf,")
    assert "UprightFont=*-Regular" in line and "BoldFont=*-Bold" in line
    assert "ItalicFont=*-Italic" in line
    # the folder has no bold italic, so it is said out loud: the real bold, slanted. Leaving the
    # style unnamed is what made `\textbf` draw the upright - fontspec then looks for no other file
    assert "BoldItalicFont=*-Bold,BoldItalicFeatures={FakeSlant=" in line
    assert (tmp_path / "tree" / "fonts" / "GoogleSansFlex-Regular.ttf").exists()


def test_a_code_face_is_not_taken_for_the_prose_face(tmp_path, monkeypatch):
    """`GoogleSansCode` begins with "Google Sans" too and is a monospace: without reading the file's
    own name as a kind of typeface, the deck's prose would come back in its code face."""
    monkeypatch.setenv("B2S_FONTS", str(font_folder(
        tmp_path, "GoogleSansCode-Regular.ttf", "GoogleSansFlex-Regular.ttf")))
    text = source_for(tmp_path)
    assert "\\setsansfont{GoogleSansFlex}" in text
    assert "\\setmonofont" not in text, "the deck has no monospaced words to set"


def test_a_typeface_the_machine_lacks_takes_the_nearest_of_its_kind(tmp_path, monkeypatch):
    """The DevFest template's quote slides are Space Mono, which is on no machine here, and LaTeX's
    own typewriter is narrow enough to break every one of their lines somewhere else: 0.42 ink
    overlap against 0.68 for Google Sans Code, which is at least the same kind of face as the rest
    of the deck."""
    pres = presentation()
    pres["slides"][0]["pageElements"].append(
        text_shape("s0_code", "print(1)", 100, 200, 300, 40, font="Space Mono"))
    monkeypatch.setenv("B2S_FONTS", str(font_folder(
        tmp_path, "GoogleSansFlex-Regular.ttf", "GoogleSansCode-Regular.ttf", "Cousine-Regular.ttf")))
    text = adopt.bootstrap(deck_ir(pres, foreign=True), tmp_path / "tree" / "main.tex")
    assert "\\setmonofont{GoogleSansCode}" in text, "the nearest kin of the face the deck is set in"


def test_adopt_refuses_to_write_over_a_source(tmp_path):
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    target = deck_ir(presentation(), foreign=True)
    (tmp_path / "target.json").write_text(json.dumps(target), encoding="utf-8")
    try:
        adopt.cmd_adopt("", tmp_path / "main.tex", tmp_path / "w", False, None, 0, None, False,
                        tmp_path / "target.json")
    except SystemExit as exc:
        assert "exists already" in str(exc)
    else:
        raise AssertionError("adopt overwrote a source that was already there")


def test_lengths_are_written_in_pdf_points_and_the_deck_words_are_left_alone():
    """The IR is in bp; TeX's pt is 72.27 to the inch, so written as pt every element came out 0.37%
    too close to the page corner. A "12pt" typed on a slide is words, not a length."""
    from beamer2slides import inverse
    frame = ("\\begin{textblock*}{100.0pt}(10.5pt,20pt)\\hskip-3.00pt\\fontsize{12.0}{14.4}\\selectfont "
             "\\vrule width0pt height9.00pt")
    assert adopt.to_bp(frame) == ("\\begin{textblock*}{100.0bp}(10.5bp,20bp)\\hskip-3.00bp"
                                  "\\fontsize{12.0bp}{14.4bp}\\selectfont \\vrule width0bp height9.00bp")
    inverse.GUARD_UNITS = True
    try:
        words = adopt.text_escape("set in 12pt Arial")
    finally:
        inverse.GUARD_UNITS = False
    assert adopt.to_bp(words) == words == "set in 12{}pt Arial"
    assert inverse.latex_escape("12pt") == "12pt"             # pull's own sources are untouched


def test_a_see_through_page_background_is_blended_over_white():
    from beamer2slides.deck_ir import page_background
    page = {"pageProperties": {"pageBackgroundFill": {"solidFill": {
        "color": {"rgbColor": {"red": 0x4b / 255, "green": 0xac / 255, "blue": 0xc6 / 255}}, "alpha": 0.247}}}}
    assert page_background(page, {}, {}) == ("#d3eaf1", None)


def test_adopt_reads_every_slide_thumbnail_and_gives_up_on_one_quietly(monkeypatch, tmp_path):
    """Fills the API cannot say come from Google's picture of the slide (deck_fills.py), so a live
    adopt reads one per slide; a slide Google would not render leaves its fills out, nothing more."""
    from beamer2slides import deck_ir as ir, google_auth, gslides

    def save(_service, _pid, page, path):
        if page == "bad":
            raise RuntimeError("HttpError 500")
        path.write_bytes(b"png")
        return 1600, 900

    monkeypatch.setattr(gslides, "save_thumbnail", save)
    monkeypatch.setattr(google_auth, "credentials", lambda: None)
    monkeypatch.setattr(google_auth, "slides_service", lambda creds=None: None)
    pres = {"slides": [{"objectId": "a"}, {"objectId": "bad"}, {"objectId": "c"}]}
    get = ir.slide_thumbnails("pid", pres, tmp_path / "thumbnails")
    assert get(0) == tmp_path / "thumbnails" / "001.png" and get(1) is None
    assert get(2).read_bytes() == b"png" and get(3) is None
