"""The layout oracle on synthetic read-backs: each kind fires on what a sync broke and stays quiet
on what was already so before it, on what the new conversion draws itself, and on what the person
did. The two live positives found on real decks (a grown box running into a box the person moved;
a formula picture rewritten off the hole the person's words had moved) are rebuilt here in their
own numbers."""

import copy
from collections.abc import Sequence

from beamer2slides import snapshot
from beamer2slides.devtools import layout_oracle as L
from beamer2slides.json_types import Json, JsonObject
from beamer2slides.text_layout import Layout, Rect

from .json_reads import jarr, jnums, jobj

SKEY = "s"
BODY: JsonObject = {"fontFamily": "Lato", "bold": False, "italic": False, "baselineOffset": "NONE", "fontSize": 17.0}
MONO: JsonObject = {**BODY, "fontFamily": "Roboto Mono"}
PARA: JsonObject = {"alignment": "START", "lineSpacing": 100, "indentStart": 0.0, "indentFirstLine": 0.0,
                    "spaceAbove": 0.0, "spaceBelow": 0.0}
# what an element entry carries besides its place (`sync_model.element_entry` reads it strictly)
HASHES: JsonObject = {"ir_hash": "i", "fields": {"text": "t", "position": "p", "size": "s", "style": "y", "image": "m"}}

Span = tuple[int, int, JsonObject]
"""(start, end, style) of a run."""


def text_rb(key: str, box: Sequence[float], text: str) -> JsonObject:
    """A text box read-back in one style and one paragraph style: `text` without Slides' own final newline."""
    return styled_rb(key, box, text, [(0, len(text), BODY)], [PARA])


def styled_rb(key: str, box: Sequence[float], text: str, spans: Sequence[Span], paras: Sequence[JsonObject]) -> JsonObject:
    """A text box read-back: `text` without Slides' own final newline; `spans` (start, end, style)."""
    return {"kind": "shape", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": [*box], "parent_group": None, "z": 1, "title": snapshot.tag(SKEY, key), "text": text + "\n",
            "run_spans": [[start, end, style] for start, end, style in spans],
            "text_styles": [BODY], "paragraph_styles": [*paras], "text_style_hash": f"h{len(text)}",
            "shape_style": {"type": "TEXT_BOX", "align": "TOP"}, "shape_style_hash": "box"}


def picture_rb(key: str, box: Sequence[float]) -> JsonObject:
    return {"kind": "image", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": [*box], "parent_group": None, "z": 2, "title": snapshot.tag(SKEY, key), "text": None,
            "text_style_hash": "", "shape_style_hash": ""}


def panel_rb(key: str, box: Sequence[float]) -> JsonObject:
    return {"kind": "shape", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": [*box], "parent_group": None, "z": 0, "title": snapshot.tag(SKEY, key), "text": "\n",
            "text_style_hash": "", "shape_style": {"type": "RECTANGLE", "fill": {"color": "#dddddd", "alpha": 1.0},
                                                   "align": "TOP"}, "shape_style_hash": "panel"}


def moved(rb: JsonObject, dx: float, dy: float) -> JsonObject:
    out = copy.deepcopy(rb)
    x0, y0, x1, y1 = jnums(rb, "box")
    out["box"] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
    transform = jnums(rb, "transform")
    out["transform"] = [*transform[:4], transform[4] + dx, transform[5] + dy]
    return out


def laid(rb: JsonObject) -> Layout:
    """The layout of a text read-back the model can lay out."""
    lay = L.layout(rb)
    assert lay is not None
    return lay


def rect_of(rb: JsonObject) -> Rect:
    x0, y0, x1, y1 = jnums(rb, "box")
    return (x0, y0, x1, y1)


def box_copy(rb: JsonObject) -> Json:
    """A copy of the read-back's box, as `list(rb["box"])` made it."""
    return [*jarr(rb, "box")]


def page_read(objs: JsonObject) -> JsonObject:
    """A read-back of one slide `p1` holding `objs` (object id -> read-back)."""
    return {"page_size": [720.0, 405.0], "slides": [{"objectId": "p1", "objects": objs}]}


class Deck:
    """One slide: what convert wrote (the base), and the new conversion drawing each element where
    the base has it unless told otherwise."""

    def __init__(self) -> None:
        self.elements: list[JsonObject] = []     # base elements
        self.ours: list[JsonObject] = []

    def add(self, key: str, kind: str, oid: str, rb: JsonObject) -> JsonObject:
        return self.add_as(key, kind, oid, rb, None, None, None)

    def add_as(self, key: str, kind: str, oid: str, rb: JsonObject, anchor: str | None, role: str | None,
               ours_rb: JsonObject | None) -> JsonObject:
        self.elements.append({"key": key, "kind": kind, "role": role or key.split("/")[1], "anchor": anchor,
                              "objects": [oid], "main": oid, "readback": {oid: rb}, **HASHES,
                              "fingerprint": {"text": "", "bbox": box_copy(rb)}, "ir": {}})
        drawn = ours_rb or rb
        ir: JsonObject = {"bbox": box_copy(drawn)}
        if kind == "text":
            lay = laid(drawn)
            ir = {"paragraphs": [{"size": 17.0, "runs": [{"size": 17.0}],
                                  "lines": [{"x0": ln.box[0], "x1": ln.box[2], "baseline": ln.baseline}
                                            for ln in lay.lines if ln.para == p]}
                                 for p in sorted({ln.para for ln in lay.lines})]}
        self.ours.append({"key": key, "kind": kind, "role": role, "anchor": anchor, "ir": ir, **HASHES,
                          "fingerprint": {"text": "", "bbox": box_copy(drawn)}})
        return rb

    def base(self) -> JsonObject:
        return {"scale": 1.0, "deck_page_size": [720.0, 405.0],
                "slides": [{"key": SKEY, "objectId": "p1", "page": 0, "elements": [*self.elements]}]}

    def conversion(self) -> JsonObject:
        return {"slides": [{"key": SKEY, "elements": [*self.ours]}], "pairs": {}, "label_moves": [], "weak_pairs": {}}

    def check(self, before: JsonObject, after: JsonObject) -> list[L.Finding]:
        """With the new conversion and no report."""
        return self.check_with(before, after, True, None)

    def check_with(self, before: JsonObject, after: JsonObject, ours: bool, report: JsonObject | None) -> list[L.Finding]:
        return L.check(self.base(), page_read(before), page_read(after), report, self.conversion() if ours else None)

    def existing(self, before: JsonObject) -> list[L.Finding]:
        return L.existing(self.base(), page_read(before))


def kinds(findings: Sequence[L.Finding], severity: str) -> list[str]:
    return sorted(f["kind"] for f in findings if f["severity"] == severity)


def fails(findings: Sequence[L.Finding]) -> list[str]:
    return kinds(findings, "fail")


# ---------------------------------------------------------------- the model

def test_a_line_that_fits_is_one_line_and_a_long_one_wraps() -> None:
    short = laid(text_rb("text/body/0", [10, 50, 710, 90], "A short line."))
    assert len(short.lines) == 1
    long = laid(text_rb("text/body/0", [10, 50, 310, 90], "A line far too long for a box this narrow, so it wraps"))
    assert len(long.lines) >= 2
    assert long.lines[1].baseline > long.lines[0].baseline
    assert long.lines[1].box[3] > 90          # and runs out of its box


def test_a_word_before_a_formula_hole_goes_down_with_it_unless_a_zero_width_space_parts_them():
    """Slides keeps a space and the no-break spaces of a hole after it together (r8, r1_math_v2
    s6: "but" went down with the integral's hole in a box with room for it). A zero-width space
    between them is a break opportunity (UAX #14 LB8): the break TeX made, pending a live check."""
    from beamer2slides import text_layout
    hole = "\xa0" * 10
    for gap, first in ((" ", "Then it tends to zero, "), (" ​", "Then it tends to zero, but ​")):
        text = "Then it tends to zero, but" + gap + hole + " for all n."
        styles = [MONO if ch == "\xa0" else BODY for ch in text]
        lines = text_layout.wrap(text, styles, 260.0)
        assert text[lines[0][0]:lines[0][1]] == first, (gap, lines)


def test_each_paragraph_takes_its_own_style_when_the_read_back_can_say_which() -> None:
    two = [PARA, {**PARA, "spaceAbove": 10.75}]

    def two_styles(key: str, box: Sequence[float], text: str) -> JsonObject:
        return styled_rb(key, box, text, [(0, len(text), BODY)], two)
    rb = two_styles("text/body/0", [10, 50, 710, 150], "First.\nSecond.")
    spaced = laid(rb).lines[1].baseline
    tight = laid(text_rb("text/body/0", [10, 50, 710, 150], "First.\nSecond.")).lines[1].baseline
    # (the step snaps to whole pixels with its space, emit.pitch_between: within a pixel)
    assert abs(spaced - tight - 10.75) < 0.75
    # three paragraphs, two distinct styles: which one has the space is unknown, so none gets it
    three = laid(two_styles("text/body/0", [10, 50, 710, 150], "A.\nB.\nC."))
    assert L.para_styles(two_styles("x/y/0", [0, 0, 1, 1], "A.\nB.\nC."), 3)[2].space_above == 0.0
    assert three.lines[1].baseline - three.lines[0].baseline < 21


# ---------------------------------------------------------------- text_overlap / text_overflow

def reflow_deck() -> tuple[Deck, JsonObject, JsonObject, JsonObject]:
    """Agent D's `layout-reflow`: box 1 gains a paragraph from the source and grows; box 2 stays
    where the person had moved it, so box 1's new line lands on box 2's first line."""
    box2_y = 85.0
    d = Deck()
    b1 = d.add("text/body/0", "text", "t1", text_rb("text/body/0", [10.62, 55.66, 720.6, 90.0], "The first box, one line."))
    b2 = d.add("text/body/1", "text", "t2", text_rb("text/body/1", [100.62, box2_y + 40, 702.7, box2_y + 80], "The second box."))
    person = moved(b2, 40, -40)
    before: JsonObject = {"t1": b1, "t2": person}
    grown = text_rb("text/body/0", [10.62, 55.66, 720.6, 110.0], "The source adds this line.\nThe first box, one line.")
    return d, before, grown, person


def test_a_grown_box_running_into_a_box_the_person_moved_is_a_text_overlap() -> None:
    d, before, grown, person = reflow_deck()
    after: JsonObject = {"n1": {**grown, "title": snapshot.tag(SKEY, "text/body/0")}, "t2": person}
    fs = d.check(before, after)
    assert fails(fs) == ["text_overlap"]
    f = L.failures(fs)[0]
    assert {f["element"], f.get("other_element")} == {"text/body/0", "text/body/1"}
    history = f.get("history")
    assert history is not None
    how = history[0].get("how")
    assert history[0]["was"] == "converter" and how is not None and "recreated" in how


def test_the_same_run_past_its_own_box_is_a_text_overflow() -> None:
    d, before, grown, person = reflow_deck()
    short_box: JsonObject = {**grown,"box": [10.62, 55.66, 720.6, 90.0]}   # the box kept its one-line height
    fs = d.check(before, {"t1": short_box, "t2": person})
    assert fails(fs) == ["text_overflow"]


def test_texts_that_met_before_the_sync_are_not_the_syncs() -> None:
    d, _, grown, person = reflow_deck()
    before: JsonObject = {"t1": {**grown}, "t2": person}      # the person had typed that line in already
    assert d.check(before, {"t1": grown, "t2": person}) == []
    assert kinds(d.existing(before), "note") == ["text_overlap"]
    assert d.existing(before)[0].get("by") == "person"


def test_what_the_new_conversion_draws_overlapping_is_not_the_syncs() -> None:
    d, before, grown, person = reflow_deck()
    # the source itself draws a line of box 1 across box 2's first line (at the source's place)
    jarr(d.ours[0], "ir", "paragraphs").append({"size": 17.0, "runs": [{"size": 17.0}],
                                                "lines": [{"x0": 150.0, "x1": 400.0, "baseline": 150.0}]})
    assert d.check(before, {"t1": grown, "t2": person}) == []


def test_boxes_that_overlap_with_their_words_clear_of_each_other_are_fine() -> None:
    d, before, grown, person = reflow_deck()
    beside = text_rb("text/body/1", [300.0, 80.0, 702.7, 120.0], "The second box.")   # right of box 1's short lines
    assert L.meet([rect_of(grown)], [rect_of(beside)], L.OVERLAP_MIN)       # the boxes do meet
    assert d.check({"t1": before["t1"], "t2": beside}, {"t1": grown, "t2": beside}) == []


def test_without_the_new_conversion_only_a_new_elements_overlap_is_a_note() -> None:
    d, before, grown, person = reflow_deck()
    # both stood before the sync: the conversion is not needed to say they did not meet then
    assert fails(d.check_with(before, {"t1": grown, "t2": person}, False, None)) == ["text_overlap"]
    fs = d.check_with({"t1": before["t1"]}, {"t1": grown, "t2": person}, False, None)   # box 2 is new
    assert fails(fs) == [] and kinds(fs, "note") == ["text_overlap"]


def note_deck() -> tuple[Deck, JsonObject, JsonObject]:
    """Live fuzz r7411: the person put a note of their own under a paragraph; the source adds a line
    to the paragraph, which grows onto the note. Sync never moves the person's own objects."""
    d = Deck()
    para = d.add("text/body/0", "text", "t1", text_rb("text/body/0", [10.62, 55.66, 720.6, 90.0], "The paragraph, one line."))
    note: JsonObject = {**text_rb("x", [40.0, 78.0, 260.0, 106.0], "note under it"), "title": None}
    grown = text_rb("text/body/0", [10.62, 55.66, 720.6, 110.0], "The source adds this line.\nThe paragraph, one line.")
    return d, {"t1": para, "u1": note}, {"n1": grown, "u1": note}


def test_the_source_running_over_the_persons_own_note_fails_unless_the_report_says_so() -> None:
    d, before, after = note_deck()
    assert fails(d.check(before, after)) == ["text_overlap"]
    told: JsonObject = {"overruns": [{"slide": SKEY, "object": "u1", "other": "n1", "depth": 7.8}]}
    fs = d.check_with(before, after, True, told)
    assert fails(fs) == [] and kinds(fs, "note") == ["text_overlap"]


def test_overruns_names_the_persons_object_the_source_now_runs_over() -> None:
    from beamer2slides import text_layout
    d, before, after = note_deck()
    found = text_layout.overruns({"objects": before}, {"objects": after}, {"u1"}, set())
    assert [(o.object, o.other) for o in found] == [("u1", "n1")] and found[0].depth > 2
    # already over it before the sync: not this sync's doing; about to be deleted: not there
    assert text_layout.overruns({"objects": after}, {"objects": after}, {"u1"}, set()) == []
    assert text_layout.overruns({"objects": before}, {"objects": after}, {"u1"}, {"n1"}) == []
    # recreated under a new id but over it as far before: found by its title, not new
    was_over: JsonObject = {"t1": after["n1"], "u1": after["u1"]}
    assert text_layout.overruns({"objects": was_over}, {"objects": after}, {"u1"}, set()) == []


def test_the_sources_picture_grown_over_the_persons_copy_is_an_overrun() -> None:
    """Edit hunt h3-1: the person duplicated a figure (Ctrl+D) and set the copy beside it; the source
    drew a larger figure and the recreated one now covers the copy. Once skipped as a collage - but a
    collage overlapped before the sync too, and that one still does not count."""
    from beamer2slides import text_layout

    def pic(box: Sequence[float], title: str | None) -> JsonObject:
        return {"kind": "image", "box": [*box], "title": title}
    before: JsonObject = {"f1": pic([150, 90, 400, 240], "b2s:ex/image/figure/0"),
                          "u1": pic([420, 250, 620, 310], "b2s:ex/image/figure/0")}
    after: JsonObject = {"f2": pic([150, 90, 640, 470], "b2s:ex/image/figure/0"), "u1": before["u1"]}
    found = text_layout.overruns({"objects": before}, {"objects": after}, {"u1"}, set())
    assert [(o.object, o.other) for o in found] == [("u1", "f2")]
    # a sticker the person put on the figure: over it as deep before as after
    collage: JsonObject = {**before, "u1": pic([300, 200, 380, 230], None)}
    assert text_layout.overruns({"objects": collage}, {"objects": {**after, "u1": collage["u1"]}}, {"u1"}, set()) == []


def test_an_old_deep_overlap_does_not_hide_the_new_one() -> None:
    """Live fuzz r7411: the person's note already ran over the frame counter, deeper than the
    paragraph now runs over it; the paragraph's overrun is still this sync's."""
    from beamer2slides import text_layout
    d, before, after = note_deck()
    counter: JsonObject = {**text_rb("x", [40.0, 80.0, 260.0, 104.0], "slide 3 of 9 in words"), "title": "b2s:s/counter"}
    found = text_layout.overruns({"objects": {**before, "c1": counter}}, {"objects": {**after, "c1": counter}}, {"u1"}, set())
    assert [(o.object, o.other) for o in found] == [("u1", "n1")]


def test_a_paragraphs_indent_end_narrows_its_lines() -> None:
    """emit.paragraph_ends keeps a paragraph's right edge short of the box's with indentEnd: the
    line model breaks and aligns against that edge, not the box's."""
    from beamer2slides import text_layout
    words = "one two three four five six seven eight nine ten"
    free = laid(text_rb("x", [0.0, 0.0, 400.0, 200.0], words))
    held = laid(styled_rb("x", [0.0, 0.0, 400.0, 200.0], words, [(0, len(words), BODY)], [{**PARA, "indentEnd": 300.0}]))
    assert len(free.lines) == 1 and len(held.lines) > 1
    end = laid(styled_rb("x", [0.0, 0.0, 400.0, 200.0], "one", [(0, len("one"), BODY)],
                         [{**PARA, "alignment": "END", "indentEnd": 50.0}]))
    assert abs(end.lines[0].box[2] - (400.0 - text_layout.INSET_X - 50.0)) < 0.01


def test_the_persons_own_arrangement_carried_onto_the_sources_move_is_theirs() -> None:
    """Live fuzz r7413: the person moved a paragraph up 30 pt onto the title; the source had moved
    it down, which kept them apart, and now moves it back up: sync carries the person's move on top
    of the source's (`sync.carried`), so the paragraph is where the person put it over the title."""
    d = Deck()
    title = d.add("text/title/0", "text", "t0", text_rb("text/title/0", [6.8, 14.0, 702.7, 50.0], "Room to grow"))
    at_source = text_rb("text/body/0", [10.62, 81.65, 720.6, 110.0], "The paragraph the person moved.")
    para = d.add_as("text/body/0", "text", "t1", at_source, None, None, moved(at_source, 0.0, -26.0))
    for el, ours, rb in ((d.elements[0], d.ours[0], title), (d.elements[1], d.ours[1], at_source)):
        jobj(el, "ir")["bbox"] = box_copy(rb)
        jobj(ours, "ir")["bbox"] = box_copy(moved(rb, 0.0, -26.0 if rb is at_source else 0.0))
    before: JsonObject = {"t0": title, "t1": moved(para, 0.0, -30.0)}
    after: JsonObject = {"t0": title, "n1": moved(para, 0.0, -56.0)}
    from beamer2slides import text_layout
    ink_after, ink_title = text_layout.ink(jobj(after, "n1")), text_layout.ink(title)
    assert ink_after is not None and ink_title is not None
    assert L.meet(ink_after, ink_title, 2.0)     # they do meet now
    assert d.check(before, after) == []
    # the same overlap where the source did not move it back is the sync's
    jobj(d.ours[1], "ir")["bbox"] = box_copy(at_source)
    assert fails(d.check(before, after)) == ["text_overlap"]


# ---------------------------------------------------------------- text_overflow out of a panel

def block_deck() -> tuple[Deck, JsonObject]:
    d = Deck()
    d.add("shape/panel/0", "shape", "p0", panel_rb("shape/panel/0", [11.0, 150.4, 709.0, 201.9]))
    body = d.add("text/body/1", "text", "t4", text_rb("text/body/1", [10.62, 170.1, 702.7, 201.0],
                                                     "A source change to a field the deck also edited is reported."))
    return d, body


def test_a_merged_text_running_out_of_its_block_is_a_text_overflow() -> None:
    """fuzz-live-refill r2700 step1: the person's 20 pt font merged into a box sized for the
    source's 17 pt line; recreated wider than its panel, it wraps and hangs out of the block."""
    d, body = block_deck()
    big: JsonObject = {**BODY, "fontSize": 20.0}
    text = "A source change to a field the deck also edited is reported as a conflict, side by side."
    before: JsonObject = {"p0": jobj(d.elements[0], "readback", "p0"), "t4": body}
    after: JsonObject = {"p0": before["p0"], "n4": styled_rb("text/body/1", [10.62, 170.1, 722.7, 201.0], text,
                                                             [(0, len(text), big)], [PARA])}
    fs = d.check(before, after)
    assert fails(fs) == ["text_overflow"]
    assert "panel" in L.failures(fs)[0]["detail"]


def test_a_text_already_out_of_its_block_before_is_not_the_syncs() -> None:
    d, body = block_deck()
    big: JsonObject = {**BODY, "fontSize": 20.0}
    text = "A source change to a field the deck also edited is reported as a conflict, side by side."
    out = styled_rb("text/body/1", [10.62, 170.1, 702.7, 201.0], text, [(0, len(text), big)], [PARA])
    panel = jobj(d.elements[0], "readback", "p0")
    assert d.check({"p0": panel, "t4": out}, {"p0": panel, "t4": {**out, "text": text + " More.\n"}}) == []


# ---------------------------------------------------------------- stranded_picture

HOLE = NBSP4 = L.NBSP * 4
FORMULA_BOX = (10.62, 140.8, 709.5, 200.0)


def formula_rb(words_before_hole: str, box: Sequence[float]) -> JsonObject:
    text = f"{words_before_hole} {NBSP4} and after."
    at = len(words_before_hole) + 1
    return styled_rb("text/body/0", [*box], text, [(0, at, BODY), (at, at + 4, MONO), (at + 4, len(text), BODY)], [PARA])


def hole_box(rb: JsonObject) -> Rect:
    return laid(rb).holes[0].box


def formula_deck() -> tuple[Deck, JsonObject, JsonObject]:
    d = Deck()
    text = d.add("text/body/0", "text", "t3", formula_rb("The merge is clean when", FORMULA_BOX))
    h = hole_box(text)
    pic = d.add_as("image/math/0", "image", "f0", picture_rb("image/math/0", [h[0] + 1, h[1] - 3, h[2] - 1, h[3] + 3]),
                   "text/body/0", "math", None)
    return d, text, pic


def test_a_formula_picture_the_sync_moved_off_its_hole_is_stranded() -> None:
    d, text, pic = formula_deck()
    fs = d.check({"t3": text, "f0": pic}, {"t3": text, "f0": moved(pic, 30, 0.0)})
    assert fails(fs) == ["stranded_picture"]


def test_a_picture_rewritten_where_the_persons_words_had_moved_its_hole_is_stranded() -> None:
    """Agent D's `layout-stranded-formula`: the person wrote "perfectly" before the hole; the
    sync recreated the picture where the source has it, 70 pt short of the hole."""
    d, text, pic = formula_deck()
    edited = formula_rb("The merge is perfectly clean when", FORMULA_BOX)
    before: JsonObject = {"t3": edited, "f0": pic}
    after: JsonObject = {"n3": edited, "n0": {**pic}}
    fs = d.check(before, after)
    assert fails(fs) == ["stranded_picture"]
    assert "rewrote" in L.failures(fs)[0]["detail"]
    assert fails(d.check(before, before)) == []             # left alone by the sync: the person's
    assert kinds(d.existing(before), "note") == ["stranded_picture"]


def test_a_picture_the_person_put_off_its_hole_stays_theirs() -> None:
    d, text, pic = formula_deck()
    theirs = moved(pic, 120, 0.0)
    fs = d.check({"t3": text, "f0": theirs}, {"t3": text, "n0": {**theirs}})
    assert fs == []


def test_a_picture_over_its_hole_is_not_stranded_nor_an_overlap() -> None:
    d, text, pic = formula_deck()
    assert d.check({"t3": text, "f0": pic}, {"n3": {**text}, "n0": {**pic}}) == []


# ---------------------------------------------------------------- off_page

def test_a_text_the_sync_moved_off_the_page_is_off_page() -> None:
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    fs = d.check({"t9": footer}, {"t9": moved(footer, 40, 0.0)})
    assert fails(fs) == ["off_page"]


def test_a_text_already_off_the_page_is_not_the_syncs() -> None:
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    out = moved(footer, 60, 0.0)
    assert d.check({"t9": out}, {"t9": {**out, "text": "4 / 100\n"}}) == []
    assert kinds(d.existing({"t9": out}), "note") == ["off_page"]


def test_allow_silences_a_kind_on_a_slide() -> None:
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    found = L.check(d.base(), page_read({"t9": footer}), page_read({"t9": moved(footer, 40, 0.0)}), None, d.conversion())
    assert [f["kind"] for f in found] == ["off_page"]
    assert L.allowed(found, ["off_page/s"]) == []
    assert L.allowed(found, ["off_page/s/text/footer/0"]) == [] and L.allowed(found, ["off_page/other"]) == found


# ---------------------------------------------------------------- the replay's tables

def replayed(findings: list[L.Finding], existing: list[L.Finding], edit_kinds: list[str], variant: str) -> L.Step:
    """A replayed step as the tables read it (the rest of its record left empty)."""
    return {"round": "", "archive": "", "step": 0, "folder": "", "variant": variant, "edits": [],
            "edit_kinds": edit_kinds, "ours": True, "findings": findings, "existing": existing}


def test_correlate_counts_steps_with_a_finding_per_edit_kind_and_variant() -> None:
    fail: L.Finding = {"kind": "off_page", "severity": "fail", "slide": "s", "element": None, "object": None, "detail": ""}
    results = [replayed([fail], [], ["move"], "a"), replayed([], [fail], ["move", "bold"], "b")]
    table = L.correlate(results, "fail")
    assert table["edit_kinds"]["move"] == {"steps": 2, "with_finding": 1, "kinds": {"off_page": 1}, "rate": 0.5, "lift": 1.0}
    assert table["variants"]["b"]["with_finding"] == 0
    assert L.counts(results) == {"off_page": {"fail": 1, "note": 0, "existing": 1}}
