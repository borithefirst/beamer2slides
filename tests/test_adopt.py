"""Adopting a deck nobody converted (adopt.py, and what deck_ir(foreign=True) reads for it).

Offline, no TeX and no Google: a hand-built `presentations.get` answer shaped like the template
decks that made this necessary - decoration on the master, more on the layout, a placeholder whose
alignment only the master states, and a connector - then the IR that comes back and the source
`adopt.bootstrap` writes from it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from beamer2slides import adopt
from beamer2slides.deck_ir import family_of
from beamer2slides.google_types import Page, Pages, Presentations, Request, Thumbnail
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.typing_compat import override
from .deck_records import deck, dicts, records
from .fake_google import Answer, Fetcher, NoPages, NoPresentations, NoSlides
from .irs import deck_ir
from .json_reads import jarr, jnum, jnums, jobj, jobjs, jstr

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import GetThumbnail

EMU = 12700


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Whether this machine has Google Sans installed must not decide what the tests read: every
    test names the fonts it has through `$B2S_FONTS` (`adopt.font_dirs`), and by default none."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def pt(v: float) -> JsonObject:
    return {"magnitude": v * EMU, "unit": "EMU"}


def at(x: float, y: float) -> JsonObject:
    return {"scaleX": 1.0, "scaleY": 1.0, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


# `solid`, `text_shape` and `presentation` answer a bare `dict` for now: test_adopt_text,
# test_adopt_media, test_adopt_theme and test_deck_ir_types index what they return key by key, which
# a `JsonObject` refuses; once those read it through json_reads, these become `JsonObject` too.
def solid(hexc: str) -> JsonObject:
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return {"solidFill": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}}


def shape(oid: str, kind: str, x: float, y: float, w: float, h: float, *, fill: str | None,
          outline: str | None) -> JsonObject:
    props: JsonObject = {"shapeBackgroundFill": solid(fill) if fill else {"propertyState": "NOT_RENDERED"}}
    if outline:
        props["outline"] = {"outlineFill": solid(outline), "weight": pt(2), "propertyState": "RENDERED"}
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y),
            "shape": {"shapeType": kind, "shapeProperties": props}}


# The defaults stay: test_adopt_media, test_adopt_theme, test_deck_ir_types and
# test_picture_recovery build their boxes with the few keywords their case is about.
def text_shape(oid: str, words: str, x: float, y: float, w: float, h: float, *, kind: str = "TEXT_BOX",
               placeholder: JsonObject | None = None, align: str | None = None, font: str = "Google Sans",
               size: float = 20.0, colour: str = "0000FF", fill: str | None = None,
               outline: str | None = None) -> JsonObject:
    style: JsonObject = {}
    if align:
        style["alignment"] = align
    sh = shape(oid, kind, x, y, w, h, fill=fill, outline=outline)
    body = jobj(sh, "shape")
    body["text"] = {"textElements": [
        {"paragraphMarker": {"style": style}},
        {"textRun": {"content": words + "\n", "style": {"fontFamily": font, "fontSize": pt(size),
                                                        "foregroundColor": {"opaqueColor": {"rgbColor": {
                                                            "red": int(colour[0:2], 16) / 255,
                                                            "green": int(colour[2:4], 16) / 255,
                                                            "blue": int(colour[4:6], 16) / 255}}}}}}]}
    if placeholder:
        body["placeholder"] = placeholder
    return sh


def presentation() -> JsonObject:
    """A master with a backdrop, a layout with a card and a connector, two slides on it."""
    placeholder = text_shape("m1_ph", "", 100, 100, 400, 60, placeholder={"type": "SUBTITLE"}, align="CENTER")
    # the master's placeholder prompt: the text a slide fills in, never drawn
    jobj(placeholder, "shape")["text"] = {"textElements": [
        {"paragraphMarker": {"style": {"alignment": "CENTER"}}},
        {"textRun": {"content": "\n", "style": {}}}]}
    master: JsonObject = {"objectId": "m1", "pageElements": [
        {"objectId": "m1_bg", "size": {"width": pt(720), "height": pt(405)}, "transform": at(0, 0),
         "image": {"contentUrl": "https://example.invalid/backdrop.png"}},
        placeholder]}
    layout: JsonObject = {"objectId": "L1", "layoutProperties": {"masterObjectId": "m1", "displayName": "Section"},
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
    slides: list[Json] = [{"objectId": f"s{n}", "slideProperties": {"layoutObjectId": "L1"},
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
    # Slides itself drops "Monotype" and calls the font "Corsiva" (korea-pptx's presentation.json):
    # a formal script reads nearer a serif's proportions than a grotesque sans
    assert family_of("Corsiva") == "serif"


def test_a_slide_is_given_what_its_layout_and_master_draw():
    ir = deck_ir(presentation(), foreign=True)
    kinds = [(e["kind"], e.get("role"), e.get("inherited")) for e in jobjs(ir, "slides", 0, "elements")]
    assert kinds == [("image", "figure", "m1"), ("shape", "panel", "L1"), ("shape", "line", "L1"),
                     ("text", "body", None)]
    # the inherited ones come first, so the slide's own words are drawn on top of the decoration
    assert [e["id"] for e in jobjs(ir, "slides", 0, "elements")][:1] == ["m1~m1_bg"]


def test_a_theme_colour_means_what_the_slides_own_master_says():
    """applied-ml carries two masters: the first says DARK1 is orange, the one most slides sit on
    says black. The schemes were merged first-master-wins, and 60 slides of black words came out
    orange (hunt 2026-09-26; gdg24, apps-edu-zh, firebase-jam alike)."""
    def scheme(dark1: str) -> JsonObject:
        return {"colorScheme": {"colors": [{"type": "DARK1", "color": {
            "red": int(dark1[0:2], 16) / 255, "green": int(dark1[2:4], 16) / 255,
            "blue": int(dark1[4:6], 16) / 255}}]}}

    pres = presentation()
    jobj(pres, "masters", 0)["pageProperties"] = scheme("F46524")
    jarr(pres, "masters").append({"objectId": "m2", "pageProperties": scheme("000000"), "pageElements": []})
    jarr(pres, "layouts").append({"objectId": "L2", "layoutProperties": {"masterObjectId": "m2"},
                                  "pageElements": []})
    for n, s in enumerate(jobjs(pres, "slides")):
        s["slideProperties"] = {"layoutObjectId": "L2" if n else "L1"}
        run = jobj(s, "pageElements", 0, "shape", "text", "textElements", 1, "textRun")
        jobj(run, "style")["foregroundColor"] = {"opaqueColor": {"themeColor": "DARK1"}}
    ir = deck_ir(pres, foreign=True)
    colours = [jstr(next(e for e in jobjs(s, "elements") if e["kind"] == "text"), "paragraphs", 0, "runs", 0,
                    "color")
               for s in jobjs(ir, "slides")]
    assert [c.upper() for c in colours] == ["#F46524", "#000000", "#000000"]


def test_a_placeholder_that_inherits_its_fill_is_drawn_on_its_layouts_panel():
    """pycon-2019: every body box is grey only through its layout's placeholder (the slide's own
    fill says INHERIT), and adopt set the dark words on the dark blue page (hunt 2026-09-26)."""
    pres = presentation()
    layout_ph = text_shape("L1_body", "", 100, 100, 400, 60, placeholder={"type": "BODY"})
    jobj(layout_ph, "shape")["shapeProperties"] = {"shapeBackgroundFill": solid("F0F0F0")}
    jarr(pres, "layouts", 0, "pageElements").append(layout_ph)
    for s in jobjs(pres, "slides"):
        ph = jobj(s, "pageElements", 0, "shape")
        ph["placeholder"] = {"type": "BODY", "parentObjectId": "L1_body"}
        ph["shapeProperties"] = {"shapeBackgroundFill": {"propertyState": "INHERIT"}}
    ir = deck_ir(pres, foreign=True)
    text = next(e for e in jobjs(ir, "slides", 0, "elements") if e["kind"] == "text" and not e.get("inherited"))
    assert jstr(text, "fill").upper() == "#F0F0F0"


def test_the_layouts_own_placeholder_is_not_drawn():
    """It is the slide's to fill, and holds the layout's prompt - printing it would put the
    template's words under the person's."""
    ir = deck_ir(presentation(), foreign=True)
    assert not any(e.get("inherited") and e["kind"] == "text" for e in jobjs(ir, "slides", 0, "elements"))


def test_pull_still_sees_only_the_slide():
    """The same deck read the way `pull` reads it: a source being refined already draws its theme,
    so giving it the layout's decoration would have it drawn twice."""
    ir = deck_ir(presentation())
    assert [e["kind"] for e in jobjs(ir, "slides", 0, "elements")] == ["text"]
    assert not any(e.get("inherited") for s in jobjs(ir, "slides") for e in jobjs(s, "elements"))


def test_a_connector_keeps_the_two_points_it_runs_between():
    """A line's box cannot say which way it points; `adopt` needs the ends, and the arrow."""
    line = next(e for e in jobjs(deck_ir(presentation(), foreign=True), "slides", 0, "elements")
                if e.get("role") == "line")
    start, end = jnums(line, "from"), jnums(line, "to")
    assert start == [200 / 720 * 453.54, 350 / 720 * 453.54] or start[0] > 0
    assert end[0] > start[0] and line["arrow"] is True
    assert line["outline"] == "#ea4335"


def blank_line_deck() -> JsonObject:
    """A box whose person pressed Return twice: a blank line above the words and one between."""
    pres = presentation()
    box = text_shape("s0_b", "first", 60, 150, 300, 90)
    text = jobj(box, "shape", "text")
    style = dict(jobj(text, "textElements", 1, "textRun", "style"))
    elements: list[Json] = [
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
    text["textElements"] = elements
    jarr(pres, "slides", 0, "pageElements").append(box)
    return pres


def words(box: JsonObject) -> list[str]:
    """Each paragraph of a text element as the words of its runs."""
    return ["".join(jstr(r, "text") for r in jobjs(p, "runs")) for p in jobjs(box, "paragraphs")]


def test_a_blank_line_someone_typed_is_a_line(tmp_path: Path) -> None:
    """Of the 717 paragraphs of the DevFest template, 282 are blank, and dropping one pulls
    everything under it up by a line. A PDF has only the gap a blank line leaves, so classify never
    makes one and `pull`'s IR must not either."""
    ir = deck_ir(blank_line_deck(), foreign=True)
    box = next(e for e in jobjs(ir, "slides", 0, "elements") if e["kind"] == "text" and jnum(e, "bbox", 1) > 80)
    assert words(box) == [" ", "first", " ", "second"], "the blank lines are kept, the trailing one is not"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    blank = re.findall(r"\\slide(?:par(?:\[[^\]]*\])?|text(?:\[[^\]]*\])?(?:\{[^}]*\})+)\{\}$", text, re.M)
    assert len(blank) == 2, "each blank line takes a line of its own"


def test_pull_still_sees_no_blank_paragraph():
    box = next(e for e in jobjs(deck_ir(blank_line_deck()), "slides", 0, "elements")
               if e["kind"] == "text" and jnum(e, "bbox", 1) > 80)
    assert words(box) == ["first", "second"]


def test_alignment_comes_from_the_placeholder_it_inherits():
    """The template centres its subtitle on the master and the slide says nothing at all. Reading
    only the slide made centred text left-aligned, which no later round can put right: the loop has
    no translator for alignment."""
    ir = deck_ir(presentation(), foreign=True)
    body = next(e for e in jobjs(ir, "slides", 0, "elements") if e["kind"] == "text")
    assert [p["align"] for p in jobjs(body, "paragraphs")] == ["center"]


# ---------------------------------------------------------------- the source it writes

def a_png(path: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (8, 4), (255, 255, 255)).save(path)
    return path


def target_with_pictures(tmp_path: Path) -> JsonObject:
    """The IR as it comes back when the deck did give us the picture files."""
    target = jobj(deck_ir(presentation(), foreign=True))
    png = a_png(tmp_path / "backdrop.png")
    for s in jobjs(target, "slides"):
        for e in jobjs(s, "elements"):
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


def source_of(tmp_path: Path, target: JsonObject) -> str:
    return adopt.bootstrap(target, tmp_path / "tree" / "main.tex", False, None)


def source_for(tmp_path: Path) -> str:
    """The source of the fixture deck, its pictures given."""
    return source_of(tmp_path, target_with_pictures(tmp_path))


def theme_of(tmp_path: Path) -> str:
    """The beamer theme the bootstrap wrote beside main.tex (adopt_theme.py): what the layout draws."""
    (sty,) = (tmp_path / "tree").glob("beamertheme*.sty")
    return sty.read_text(encoding="utf-8")


def test_the_source_has_a_frame_per_slide_and_compiles_as_beamer(tmp_path: Path) -> None:
    text = source_for(tmp_path)
    assert text.count("\\begin{frame}") == 3 and text.count("\\end{frame}") == 3
    assert "\\documentclass[aspectratio=169]{beamer}" in text
    assert text.index("\\begin{document}") < text.index("\\begin{frame}") < text.index("\\end{document}")


def test_every_frame_carries_a_label_of_the_decks_own_name_for_the_slide(tmp_path: Path) -> None:
    r"""A frame's `label=` is the only identity that survives compiling (docs/labels.md), and the
    fallback without one - title, occurrence, position - is weakest on exactly these decks: slides
    with no title, the same words twice, reordered by their owner. So `adopt` writes one per frame,
    from the slide's `objectId`, which the deck itself keeps unique and says again on every read."""
    text = source_for(tmp_path)
    found = re.findall(r"\\begin\{frame\}\[[^\]]*\blabel=([^,\]]+)", text)
    assert found == ["s0", "s1", "s2"], "every frame, named as the deck names the slide"
    assert len(set(found)) == len(found)


def test_a_label_is_never_written_twice_however_the_deck_names_its_slides(tmp_path: Path) -> None:
    r"""Slugging is lossy - case, length, punctuation - so two objectIds can land on one name, and a
    label written twice never reaches the PDF twice: hyperref keeps the first destination and drops
    the second, so the second frame comes back with *no* label and nothing downstream can tell
    (CLAUDE.md, `labels.survey`). The second one has to take a name of its own here."""
    pres = presentation()
    for s, oid in zip(jobjs(pres, "slides"), ["Same_Id", "same-id", "SLIDES_API1638390000000000000_0"]):
        s["objectId"] = oid
    names = adopt.frame_labels(deck_ir(pres, foreign=True))
    assert names[0] != names[1] and len(set(names)) == 3
    assert all(re.fullmatch(r"[a-z][a-z0-9-]*", n) for n in names), "a beamer option list carries it"


def test_a_slide_with_no_words_at_all_is_still_labelled():
    """Half of a template deck's slides say nothing an identity could be built from; the deck's own
    name for them does not care."""
    pres = presentation()
    jobj(pres, "slides", 1)["pageElements"] = []
    assert adopt.frame_labels(deck_ir(pres, foreign=True))[1] == "s1"


def test_every_element_keeps_its_own_place(tmp_path: Path) -> None:
    """A foreign deck's geometry is boxes a person dragged, not flow text a theme laid out, so the
    bootstrap places each one; the loop would otherwise spend a round per element escalating flow
    text back into a textblock (`inverse.Planner.geometry`)."""
    text = source_for(tmp_path)
    assert "\\usepackage[absolute,overlay]{textpos}" in text
    # backdrop, card, connector and the subtitle placeholder: the layout's, so said once in its
    # template, and each frame names the layout and hands it its words
    assert places(theme_of(tmp_path)) == 4
    assert placed(text) == 0
    assert text.count(",layout=section") == 3
    assert "\\framesubtitle{Slide 0}" in text


def test_a_picture_the_deck_would_not_give_us_is_left_out(tmp_path: Path) -> None:
    """Not drawn as an empty box: the loop then reports `element_missing`, which says what happened.
    (The backdrop of the fixture has no file unless the test puts one there.)"""
    text = source_of(tmp_path, jobj(deck_ir(presentation(), foreign=True))) + theme_of(tmp_path)
    assert "figures/" not in text
    assert places(text) == 3


def test_a_slide_that_sits_on_another_colour_says_so(tmp_path: Path) -> None:
    """Decoration is often a picture with transparency, so the colour under it is not a detail: one
    deck-wide background would make every such slide the wrong colour end to end."""
    text = source_for(tmp_path)
    assert "\\definecolor{deckbg}{HTML}{F4CCCC}" in text            # what most of the deck sits on
    assert "\\setbeamercolor{background canvas}{bg=deckbg}" in text
    # and the one slide that does not says so in its options, which last until the next frame
    defined = re.search(r"\\definecolor\{(\w+)\}\{HTML\}\{0005DF\}", text)
    assert defined is not None
    assert text.count(f"background={defined.group(1)}]") == 1
    assert "bg=deckbg}}" in theme_of(tmp_path), "the next frame starts from the deck's colour again"


def test_a_node_is_drawn_with_its_outline_and_its_rounded_corners(tmp_path: Path) -> None:
    text = source_for(tmp_path) + theme_of(tmp_path)
    # the corners are quarter circles of the preset's radius: TikZ's rounded corners, named once
    card = next(l for l in text.splitlines() if "\\sliderect[" in l and "rounded=" in l)
    # its fill and outline are the deck's own style, named once (`adopt_shapes.survey_styles`)
    name = re.match(r"\s*\\sliderect\[([\w-]+),rounded=", card)
    assert name is not None
    keys = re.search(r"\\slideshapestyle\{" + name.group(1) + r"\}\{([^}]*)\}", text)
    assert keys is not None
    assert "fill=" in keys.group(1) and "draw=" in keys.group(1) and "line width=" in keys.group(1)


def test_the_base_style_of_a_box_is_set_where_the_box_is(tmp_path: Path) -> None:
    """`inverse.runs_latex` writes a run's style only where it differs from a base, which is true of
    a source being refined and false of one written from nothing: without this the crimson 9 pt
    instruction slides and the blue 26 pt section titles both come out black."""
    text = source_for(tmp_path)
    styles = re.findall(r"\\slidestyle\{[^}]*\}\{[^}]*\}", text)
    assert any("color=" in s for s in styles)
    assert "\\color{\\slides@k@color}" in (Path(tmp_path) / "tree" / "slides.sty").read_text(encoding="utf-8")


def test_a_picture_is_copied_into_the_tree(tmp_path: Path) -> None:
    """The download sits in the work folder, which is scratch: a source tree that referred to it
    would stop compiling the moment that folder went."""
    source_for(tmp_path)
    text = theme_of(tmp_path)
    assert "figures/" in text
    files = list((tmp_path / "tree" / "figures").glob("*.png"))
    assert files, "the picture is not beside the source"
    assert len(files) == 1, "the same picture on every slide is copied once"


def test_a_slide_on_a_picture_is_drawn_on_it(tmp_path: Path) -> None:
    """A converted deck keeps its theme in each slide's background picture, and so do templates
    that were made from one: without it a white title lands on a white page."""
    pres = presentation()
    jobj(pres, "slides", 1, "pageProperties")["pageBackgroundFill"] = {
        "stretchedPictureFill": {"contentUrl": "https://example.invalid/bars.png"}}
    png = a_png(tmp_path / "bars.png")

    def fetch(url: str) -> bytes:
        return png.read_bytes()
    ir = deck_ir(pres, foreign=True, fetch=fetch, images=tmp_path / "images")
    assert jobj(ir, "slides", 1)["background_file"] and "background_file" not in jobj(ir, "slides", 0)
    assert "background_file" not in jobj(deck_ir(pres), "slides", 1), "pull reads no backdrop"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
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


def test_without_the_decks_typeface_the_source_says_tex_gyre(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_the_deck_is_set_in_its_own_typeface_when_the_machine_has_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_a_code_face_is_not_taken_for_the_prose_face(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GoogleSansCode` begins with "Google Sans" too and is a monospace: without reading the file's
    own name as a kind of typeface, the deck's prose would come back in its code face."""
    monkeypatch.setenv("B2S_FONTS", str(font_folder(
        tmp_path, "GoogleSansCode-Regular.ttf", "GoogleSansFlex-Regular.ttf")))
    text = source_for(tmp_path)
    assert "\\setsansfont{GoogleSansFlex}" in text
    assert "\\setmonofont" not in text, "the deck has no monospaced words to set"


def test_a_typeface_the_machine_lacks_takes_the_nearest_of_its_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The DevFest template's quote slides are Space Mono, which is on no machine here, and LaTeX's
    own typewriter is narrow enough to break every one of their lines somewhere else: 0.42 ink
    overlap against 0.68 for Google Sans Code, which is at least the same kind of face as the rest
    of the deck."""
    pres = presentation()
    jarr(pres, "slides", 0, "pageElements").append(
        text_shape("s0_code", "print(1)", 100, 200, 300, 40, font="Space Mono"))
    monkeypatch.setenv("B2S_FONTS", str(font_folder(
        tmp_path, "GoogleSansFlex-Regular.ttf", "GoogleSansCode-Regular.ttf", "Cousine-Regular.ttf")))
    text = adopt.bootstrap(deck_ir(pres, foreign=True), tmp_path / "tree" / "main.tex", False, None)
    assert "\\setmonofont{GoogleSansCode}" in text, "the nearest kin of the face the deck is set in"


def test_a_trailing_width_qualifier_is_a_different_narrower_font(tmp_path: Path) -> None:
    """"Arial Narrow" merely starts with "Arial", but it is not Arial: taken for the machine's own
    font, it was set too wide and unreported (ua-space:1's title ran off the slide). A trailing
    qualifier now makes it a stand-in, so `stood_in` reports it and `stretch` can narrow it back -
    while a genuine alias ("Arial MT") still merges."""
    from .test_adopt_media import tiny_font
    path = tmp_path / "Arial-Regular.ttf"
    path.write_bytes(tiny_font("Arial"))
    files = adopt.FontFiles(faces={"UprightFont": path}, index=0)
    family = adopt.FontFamily(stem="Arial", match="Arial", standin=None, files=files)
    assert adopt.same_font_name("arial", "arial")
    assert adopt.same_font_name("arial", "arialmt"), "a name-table alias stays merged"
    assert not adopt.same_font_name("arial", "arialnarrow")
    assert adopt.stood_in("Arial Narrow", family) == "Arial"
    assert adopt.stood_in("Arial MT", family) is None, "a genuine alias, not a stand-in"

    def sample(width: float) -> JsonObject:
        return {"kind": "text", "bbox": [0, 0, 100, 20], "ink_width": width,
                "paragraphs": [{"runs": [{"text": "AAAAA", "size": 10.0, "font": "Arial Narrow"}]}]}
    # 5 "A"s at 10 pt is 29 pt of ink at this machine's Arial (600 advance, 100 right bearing); the
    # deck's own thumbnails show Arial Narrow's words about 10% narrower than that
    target = deck({"slides": [{"elements": [sample(26.1), sample(26.2), sample(15.0)]}]})
    assert adopt.stretch("Arial Narrow", "Arial", files, target) == ",FakeStretch=0.902"
    assert adopt.stretch("Arial", "Arial", files, target) == "", "the deck's own font is never stretched"
    # too few lines measured: Arial Narrow is Arial at 82% by design (offline, ua-space's title)
    assert adopt.stretch("Arial Narrow", "Arimo", files, deck({"slides": []})) == ",FakeStretch=0.82"
    assert adopt.same_font_name("calibri", "calibrilight"), "a weight is the same family, not a stand-in"


def test_a_formal_script_the_machine_lacks_is_set_in_its_google_fonts_stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """korea-pptx types slide titles in "Monotype Corsiva" (Slides itself renames the run's font to
    "Corsiva"): a Windows-only chancery italic with no metric twin. A serif's italic stands in for
    it (`adopt.SUBSTITUTES`, `SLANTED`: its upright is the italic), reported as a stand-in rather
    than silently falling to an upright sans."""
    from beamer2slides import fontfetch
    from .test_adopt_media import tiny_font
    assert family_of("Corsiva") == "serif"
    folder = tmp_path / "fonts"
    folder.mkdir()
    monkeypatch.setenv("B2S_FONTS", str(folder))
    monkeypatch.setattr(adopt, "fetching", lambda: True)

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        if name != "Tinos":
            return None
        got: dict[str, Path] = {}
        for style, key in (("Regular", "UprightFont"), ("Italic", "ItalicFont")):
            path = folder / f"Tinos-{style}.ttf"
            path.write_bytes(tiny_font("Tinos"))
            got[key] = path
        return got
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    adopt._FAMILIES.clear()
    try:
        got = adopt.font_family("Corsiva", "serif", "")
    finally:
        adopt._FAMILIES.clear()
    assert got is not None and got.standin == "Tinos"
    assert got.files.upright.name == "Tinos-Italic.ttf"
    assert adopt.stood_in("Corsiva", got) == "Tinos"


def test_screen_and_office_fonts_stand_in_by_measured_width():
    """Tahoma, Verdana, Trebuchet MS, Consolas, Corbel, Helvetica Neue, Proxima Nova, Book Antiqua
    and Palatino Linotype are all proprietary and off google/fonts by their own names (the offline
    corpus's missing-font census): each maps to a fetchable family, picked by comparing the Windows
    original's advance widths (an English-frequency-weighted sample of letters) against the
    candidates' - not merely the first plausible name. Book Antiqua and Palatino Linotype are the
    same design (the former is Microsoft's licensed Palatino) and neither is on google/fonts, so
    both take the same stand-in rather than one naming the other, which used to fail twice over
    (hebrew-lesson's census)."""
    for flat, standin in [
        ("tahoma", "PT Sans"), ("verdana", "Noto Sans"), ("trebuchetms", "Fira Sans"),
        ("consolas", "Inconsolata"), ("corbel", "Carlito"), ("helveticaneue", "Arimo"),
        ("proximanova", "Figtree"), ("bookantiqua", "PT Serif"), ("palatinolinotype", "PT Serif"),
    ]:
        assert adopt.SUBSTITUTES[flat] == [standin]
    # Verdana is drawn noticeably wider than any of its google/fonts stand-ins, Consolas noticeably
    # narrower than Inconsolata: both measured over 3% off and get a `DESIGN_WIDTHS` correction for
    # a deck whose own thumbnails are too few to measure it (`font_widths`, `stretch`).
    assert adopt.DESIGN_WIDTHS["verdana"] > 1.0
    assert adopt.DESIGN_WIDTHS["consolas"] > 1.0
    # neither Tahoma nor the rest measured 3% off their stand-in, so none of them needs one
    for flat in ("tahoma", "trebuchetms", "corbel", "helveticaneue", "proximanova", "bookantiqua",
                 "palatinolinotype"):
        assert flat not in adopt.DESIGN_WIDTHS


def test_verdana_is_set_wide_and_consolas_narrow_with_too_few_lines_to_measure(tmp_path: Path) -> None:
    """`stretch` falls back to `DESIGN_WIDTHS` when a deck has no (or too few) lines of its own font
    to measure (`font_widths` needs at least two): Verdana's stand-in is stretched out to match its
    screen-legible width, Consolas' the other way, same as Arial Narrow's 82% (ua-space, offline)."""
    noto = adopt.FontFiles(faces={"UprightFont": tmp_path / "NotoSans-Regular.ttf"}, index=0)
    assert adopt.stretch("Verdana", "NotoSans", noto, deck({"slides": []})) == ",FakeStretch=1.07"
    inconsolata = adopt.FontFiles(faces={"UprightFont": tmp_path / "Inconsolata-Regular.ttf"}, index=0)
    assert adopt.stretch("Consolas", "Inconsolata", inconsolata, deck({"slides": []})) == ",FakeStretch=1.1"
    # the deck's own font is never stretched against itself
    assert adopt.stretch("Verdana", "Verdana", noto, deck({"slides": []})) == ""


def test_verdana_stands_in_as_noto_sans_when_the_machine_lacks_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through `font_family`: a deck in Verdana with no Verdana on the machine is set in
    Noto Sans, reported as a stand-in (`stood_in`), the way Corsiva stands in as Tinos above."""
    from beamer2slides import fontfetch
    from .test_adopt_media import tiny_font
    folder = tmp_path / "fonts"
    folder.mkdir()
    monkeypatch.setenv("B2S_FONTS", str(folder))
    monkeypatch.setattr(adopt, "fetching", lambda: True)

    def fetch(name: str, log: Callable[[str], None]) -> dict[str, Path] | None:
        if name != "Noto Sans":
            return None
        path = folder / "NotoSans-Regular.ttf"
        path.write_bytes(tiny_font("Noto Sans"))
        return {"UprightFont": path}
    monkeypatch.setattr(fontfetch, "fetch_family", fetch)
    adopt._FAMILIES.clear()
    try:
        got = adopt.font_family("Verdana", "sans", "")
    finally:
        adopt._FAMILIES.clear()
    assert got is not None and got.standin == "NotoSans"
    assert adopt.stood_in("Verdana", got) == "NotoSans"


def test_adopt_refuses_to_write_over_a_source(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("\\documentclass{beamer}\n", encoding="utf-8")
    target = deck_ir(presentation(), foreign=True)
    (tmp_path / "target.json").write_text(json.dumps(target), encoding="utf-8")
    try:
        adopt.cmd_adopt("", tmp_path / "main.tex", tmp_path / "w", False, None, 0, None, False,
                        tmp_path / "target.json", base=True, base_in_drive=False, log=print, fonts=None, found=None,
                        pptx=None, files=None)
    except SystemExit as exc:
        assert "exists already" in str(exc)
    else:
        raise AssertionError("adopt overwrote a source that was already there")


def test_a_file_with_nothing_in_it_is_a_name_and_not_a_source(tmp_path: Path) -> None:
    """Making the file first is what a person does when asked to name one - and in the
    playground's editor it is the only way to name one. An empty file is no work to lose."""
    (tmp_path / "main.tex").write_text("", encoding="utf-8")
    assert not adopt.written_already(tmp_path / "main.tex")
    assert not adopt.written_already(tmp_path / "nothing-here.tex")
    (tmp_path / "main.tex").write_text("%\n", encoding="utf-8")
    assert adopt.written_already(tmp_path / "main.tex")


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
    page: Page = {"pageProperties": {"pageBackgroundFill": {"solidFill": {
        "color": {"rgbColor": {"red": 0x4b / 255, "green": 0xac / 255, "blue": 0xc6 / 255}}, "alpha": 0.247}}}}
    assert page_background(page, {}, {}) == ("#d3eaf1", None)


class Thumbs(NoPages):
    """Google's slide pictures: every slide has one but the one called "bad", which it will not render."""

    @override
    def getThumbnail(self, **kw: Unpack[GetThumbnail]) -> Request[Thumbnail]:
        page = kw["pageObjectId"]
        if page == "bad":
            raise RuntimeError("HttpError 500")
        return Answer(Thumbnail(contentUrl=f"https://thumbs/{page}", width=1600, height=900))


class ThumbPresentations(NoPresentations):
    @override
    def pages(self) -> Pages:
        return Thumbs()


class ThumbSlides(NoSlides):
    @override
    def presentations(self) -> Presentations:
        return ThumbPresentations()


def urllib_refused(url: str) -> bytes:
    pytest.fail(f"urllib fetched {url}")


def test_adopt_reads_every_slide_thumbnail_and_gives_up_on_one_quietly(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fetcher: Fetcher) -> None:
    """Fills the API cannot say come from Google's picture of the slide (deck_fills.py), so a live
    adopt reads one per slide; a slide Google would not render leaves its fills out, nothing more.
    The thumbnails are downloaded on three workers, through the fetcher the caller installed."""
    from beamer2slides import deck_ir as ir, google_auth, net
    from beamer2slides.google_types import Presentation

    def slides_service(creds: object) -> ThumbSlides:
        return ThumbSlides()

    def served(url: str) -> bytes:
        return f"png of {url}".encode()

    monkeypatch.setattr(google_auth, "credentials", lambda: None)
    monkeypatch.setattr(google_auth, "slides_service", slides_service)
    monkeypatch.setattr(net, "urllib_fetch", urllib_refused)
    fetcher(served)
    pres: Presentation = {"slides": [{"objectId": "a"}, {"objectId": "bad"}, {"objectId": "c"}]}
    get = ir.slide_thumbnails("pid", pres, tmp_path / "thumbnails")
    assert get(0) == tmp_path / "thumbnails" / "001.png" and get(1) is None
    third = get(2)
    assert isinstance(third, Path) and third.read_bytes() == b"png of https://thumbs/c" and get(3) is None


def test_a_right_to_left_one_line_box_is_measured_too():
    """hebrew-lesson's Hebrew paragraphs are flush right, direction rtl (`deck_ir` mirrors a
    right-to-left paragraph's own START to align=right): that is its equivalent of a left-to-right
    paragraph's align=left, not a paragraph to exclude - `ink_widths`' pixel scan is blind to which
    edge the words sit against. Before this, no right-to-left box was ever measured, so `font_widths`
    always answered None for a deck's Hebrew or Arabic text and no stand-in was ever stretched to it."""
    import numpy as np

    from beamer2slides.deck_thumbs import ink_widths
    px = 4.0
    im = np.full((int(100 * px), int(200 * px), 3), 240, dtype=np.int16)
    im[int(24 * px):int(30 * px), int(120 * px):int(180 * px)] = 20   # flush right, not left

    def element(align: str, direction: str | None) -> JsonObject:
        p: JsonObject = {"align": align, "bullet": None, "runs": [{"text": "מוסר השכל", "size": 8.0}]}
        if direction:
            p["direction"] = direction
        return {"kind": "text", "bbox": [10.0, 10.0, 190.0, 60.0], "anchor": [16.7, 30.0],
                "box": {"scale": 1.0}, "paragraphs": [p]}

    rtl, = dicts(ink_widths(records([element("right", "rtl")]), im, px))
    assert rtl["ink_width"] == pytest.approx(60.0, abs=0.3)

    # a left-to-right paragraph that merely happens to be right-aligned is unrelated and still
    # excluded (this is not a blanket relaxation of the alignment check)
    plain_right, = dicts(ink_widths(records([element("right", None)]), im, px))
    assert "ink_width" not in plain_right

    # a right-to-left paragraph flush with its OWN start (align=right) is measured, but one written
    # centered, or mistakenly left, is not - only the mirrored equivalent of "left" qualifies
    off_start, = dicts(ink_widths(records([element("left", "rtl")]), im, px))
    assert "ink_width" not in off_start


def test_font_widths_ignores_bidi_marks_in_a_right_to_left_line(tmp_path: Path) -> None:
    """A right-to-left line's logical text carries LRM/RLM marks where `bidi.logical_line` needed to
    hold a direction island together (`bidi.MARKS`): they draw nothing and take no room, and are not
    on any real machine font's cmap - so a mark anywhere in the measured line, not only at an edge,
    used to cost `font_widths` the whole line (`None in names`), or its first/last glyph having no
    outline to bound. hebrew-lesson mixes Hebrew and Latin: "1. <hebrew words>" needs an LRM to keep
    the digit's run together with the right-to-left line around it."""
    from beamer2slides.bidi import LRM, RLM
    from .test_adopt_media import tiny_font
    path = tmp_path / "TinySans-Regular.ttf"
    path.write_bytes(tiny_font("Tiny Sans"))    # covers ASCII only: no glyph for LRM/RLM
    files = adopt.FontFiles(faces={"UprightFont": path}, index=0)

    def sample(width: float, text: str) -> JsonObject:
        return {"kind": "text", "bbox": [0, 0, 100, 20], "ink_width": width,
                "paragraphs": [{"runs": [{"text": text, "size": 10.0, "font": "Deck Serif"}]}]}
    # 5 "A"s at 10 pt is 29 pt of ink (600 advance, 100 right bearing); marks add no glyph and no width
    plain = deck({"slides": [{"elements": [sample(26.1, "AAAAA"), sample(26.2, "AAAAA")]}]})
    marked = deck({"slides": [{"elements": [
        sample(26.1, f"{RLM}AA{LRM}AAA"), sample(26.2, f"{LRM}{RLM}AAAAA{RLM}")]}]})
    assert adopt.font_widths("Deck Serif", files, marked) == adopt.font_widths("Deck Serif", files, plain)
    assert adopt.font_widths("Deck Serif", files, marked) == pytest.approx(0.9, abs=0.005)


def test_the_adopt_summary_reads_what_the_agent_reports(tmp_path: Path) -> None:
    """`adopt_sync.adopt_summary` is the typed read of a base's `adopt` ties, the numbers
    `deck_adopt` reports: counts of the lists, 0 where a base says nothing."""
    from beamer2slides import adopt_sync
    path = tmp_path / "base.json"
    path.write_text(json.dumps({"adopt": {"slides": 3, "paired": 5, "unpaired": ["a", "b"], "from_layout": ["c"]}}),
                    encoding="utf-8")
    assert adopt_sync.adopt_summary(path) == adopt_sync.AdoptSummary(slides=3, paired=5, unpaired=2, from_layout=1)
    path.write_text(json.dumps({"slides": []}), encoding="utf-8")
    assert adopt_sync.adopt_summary(path) == adopt_sync.AdoptSummary(slides=None, paired=0, unpaired=0, from_layout=0)
