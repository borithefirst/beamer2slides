"""Adopting a deck nobody converted (adopt.py, and what deck_ir(foreign=True) reads for it).

Offline, no TeX and no Google: a hand-built `presentations.get` answer shaped like the template
decks that made this necessary - decoration on the master, more on the layout, a placeholder whose
alignment only the master states, and a connector - then the IR that comes back and the source
`adopt.bootstrap` writes from it.
"""

import json
from pathlib import Path

from beamer2slides import adopt
from beamer2slides.deck_ir import deck_ir, family_of

EMU = 12700


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
                   "line": {"lineProperties": {"lineFill": solid("EA4335"), "weight": pt(3),
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


def source_for(tmp_path: Path, target: dict | None = None) -> str:
    target = target if target is not None else target_with_pictures(tmp_path)
    return adopt.bootstrap(target, tmp_path / "tree" / "main.tex")


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
    assert text.count("\\begin{textblock*}") == 3 * 4        # backdrop, card, connector, words
    assert "\\usepackage[absolute,overlay]{textpos}" in text


def test_a_picture_the_deck_would_not_give_us_is_left_out(tmp_path):
    """Not drawn as an empty box: the loop then reports `element_missing`, which says what happened.
    (The backdrop of the fixture has no file unless the test puts one there.)"""
    text = source_for(tmp_path, deck_ir(presentation(), foreign=True))
    assert "includegraphics" not in text
    assert text.count("\\begin{textblock*}") == 3 * 3


def test_a_slide_that_sits_on_another_colour_says_so(tmp_path):
    """Decoration is often a picture with transparency, so the colour under it is not a detail: one
    deck-wide background would make every such slide the wrong colour end to end."""
    text = source_for(tmp_path)
    assert "\\definecolor{deckbg}{HTML}{F4CCCC}" in text            # what most of the deck sits on
    assert "\\setbeamercolor{background canvas}{bg=deckbg}" in text
    # and the one slide that does not, in a group so the colour ends with its frame
    assert text.count("{\\setbeamercolor{background canvas}{bg=b2s0005DF}\n\\begin{frame}") == 1


def test_a_node_is_drawn_with_its_outline_and_its_rounded_corners(tmp_path):
    text = source_for(tmp_path)
    card = next(l for l in text.splitlines() if "rounded corners" in l)
    assert "fill=" in card and "draw=" in card and "line width=" in card


def test_the_base_style_of_a_box_is_set_where_the_box_is(tmp_path):
    """`inverse.runs_latex` writes a run's style only where it differs from a base, which is true of
    a source being refined and false of one written from nothing: without this the crimson 9 pt
    instruction slides and the blue 26 pt section titles both come out black."""
    text = source_for(tmp_path)
    assert "\\color{" in text


def test_a_picture_is_copied_into_the_tree(tmp_path):
    """The download sits in the work folder, which is scratch: a source tree that referred to it
    would stop compiling the moment that folder went."""
    text = source_for(tmp_path)
    assert "figures/" in text
    files = list((tmp_path / "tree" / "figures").glob("*.png"))
    assert files, "the picture is not beside the source"
    assert len(files) == 1, "the same picture on every slide is copied once"


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
