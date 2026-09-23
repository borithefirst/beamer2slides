"""The layout oracle on synthetic read-backs: each kind fires on what a sync broke and stays quiet
on what was already so before it, on what the new conversion draws itself, and on what the person
did. The two live positives found on real decks (a grown box running into a box the person moved;
a formula picture rewritten off the hole the person's words had moved) are rebuilt here in their
own numbers."""

import copy

from beamer2slides import snapshot
from beamer2slides.devtools import layout_oracle as L

SKEY = "s"
BODY = {"fontFamily": "Lato", "bold": False, "italic": False, "baselineOffset": "NONE", "fontSize": 17.0}
MONO = {**BODY, "fontFamily": "Roboto Mono"}
PARA = {"alignment": "START", "lineSpacing": 100, "indentStart": 0.0, "indentFirstLine": 0.0,
        "spaceAbove": 0.0, "spaceBelow": 0.0}


def text_rb(key, box, text, spans=None, paras=None):
    """A text box read-back: `text` without Slides' own final newline; `spans` (start, end, style)."""
    return {"kind": "shape", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": list(box), "parent_group": None, "z": 1, "title": snapshot.tag(SKEY, key), "text": text + "\n",
            "run_spans": [list(s) for s in (spans or [(0, len(text), BODY)])],
            "text_styles": [BODY], "paragraph_styles": paras or [PARA], "text_style_hash": f"h{len(text)}",
            "shape_style": {"type": "TEXT_BOX", "align": "TOP"}}


def picture_rb(key, box):
    return {"kind": "image", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": list(box), "parent_group": None, "z": 2, "title": snapshot.tag(SKEY, key), "text": None}


def panel_rb(key, box):
    return {"kind": "shape", "transform": [1.0, 0.0, 0.0, 1.0, box[0], box[1]], "size": [box[2] - box[0], box[3] - box[1]],
            "box": list(box), "parent_group": None, "z": 0, "title": snapshot.tag(SKEY, key), "text": "\n",
            "shape_style": {"type": "RECTANGLE", "fill": {"color": "#dddddd", "alpha": 1.0}, "align": "TOP"}}


def moved(rb, dx=0.0, dy=0.0, **more):
    out = copy.deepcopy(rb)
    out["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
    out["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]
    out.update(more)
    return out


class Deck:
    """One slide: what convert wrote (the base), and the new conversion drawing each element where
    the base has it unless told otherwise."""

    def __init__(self):
        self.elements = []     # base elements
        self.ours = []

    def add(self, key, kind, oid, rb, anchor=None, role=None, ours_rb=None):
        self.elements.append({"key": key, "kind": kind, "role": role or key.split("/")[1], "anchor": anchor,
                              "objects": [oid], "main": oid, "readback": {oid: rb},
                              "fingerprint": {"bbox": list(rb["box"])}, "ir": {}})
        drawn = ours_rb or rb
        ir = {"bbox": list(drawn["box"])}
        if kind == "text":
            lay = L.layout(drawn)
            ir = {"paragraphs": [{"size": 17.0, "runs": [{"size": 17.0}],
                                  "lines": [{"x0": ln["box"][0], "x1": ln["box"][2], "baseline": ln["baseline"]}
                                            for ln in lay["lines"] if ln["para"] == p]}
                                 for p in sorted({ln["para"] for ln in lay["lines"]})]}
        self.ours.append({"key": key, "kind": kind, "role": role, "anchor": anchor, "ir": ir,
                          "fingerprint": {"bbox": list(drawn["box"])}})
        return rb

    def base(self):
        return {"scale": 1.0, "deck_page_size": [720.0, 405.0],
                "slides": [{"key": SKEY, "objectId": "p1", "page": 0, "elements": self.elements}]}

    def conversion(self):
        return {"slides": [{"key": SKEY, "elements": self.ours}], "pairs": {}, "label_moves": [], "weak_pairs": {}}

    def check(self, before: dict, after: dict, ours=True, report=None):
        read = lambda objs: {"page_size": [720.0, 405.0], "slides": [{"objectId": "p1", "objects": objs}]}
        return L.check(self.base(), read(before), read(after), report, self.conversion() if ours else None)

    def existing(self, before: dict):
        return L.existing(self.base(), {"page_size": [720.0, 405.0], "slides": [{"objectId": "p1", "objects": before}]})


def kinds(findings, severity="fail"):
    return sorted(f["kind"] for f in findings if f["severity"] == severity)


# ---------------------------------------------------------------- the model

def test_a_line_that_fits_is_one_line_and_a_long_one_wraps():
    short = L.layout(text_rb("text/body/0", [10, 50, 710, 90], "A short line."))
    assert len(short["lines"]) == 1
    long = L.layout(text_rb("text/body/0", [10, 50, 310, 90], "A line far too long for a box this narrow, so it wraps"))
    assert len(long["lines"]) >= 2
    assert long["lines"][1]["baseline"] > long["lines"][0]["baseline"]
    assert long["lines"][1]["box"][3] > 90          # and runs out of its box


def test_each_paragraph_takes_its_own_style_when_the_read_back_can_say_which():
    two = [PARA, {**PARA, "spaceAbove": 10.75}]
    rb = text_rb("text/body/0", [10, 50, 710, 150], "First.\nSecond.", paras=two)
    spaced = L.layout(rb)["lines"][1]["baseline"]
    tight = L.layout(text_rb("text/body/0", [10, 50, 710, 150], "First.\nSecond."))["lines"][1]["baseline"]
    assert abs(spaced - tight - 10.75) < 0.01
    # three paragraphs, two distinct styles: which one has the space is unknown, so none gets it
    three = L.layout(text_rb("text/body/0", [10, 50, 710, 150], "A.\nB.\nC.", paras=two))
    assert L.para_styles(text_rb("x/y/0", [0, 0, 1, 1], "A.\nB.\nC.", paras=two), 3)[2]["spaceAbove"] == 0.0
    assert three["lines"][1]["baseline"] - three["lines"][0]["baseline"] < 21


# ---------------------------------------------------------------- text_overlap / text_overflow

def reflow_deck(box2_y=85.0):
    """Agent D's `layout-reflow`: box 1 gains a paragraph from the source and grows; box 2 stays
    where the person had moved it, so box 1's new line lands on box 2's first line."""
    d = Deck()
    b1 = d.add("text/body/0", "text", "t1", text_rb("text/body/0", [10.62, 55.66, 720.6, 90.0], "The first box, one line."))
    b2 = d.add("text/body/1", "text", "t2", text_rb("text/body/1", [100.62, box2_y + 40, 702.7, box2_y + 80], "The second box."))
    person = moved(b2, dx=40, dy=-40)
    before = {"t1": b1, "t2": person}
    grown = text_rb("text/body/0", [10.62, 55.66, 720.6, 110.0], "The source adds this line.\nThe first box, one line.")
    return d, before, grown, person


def test_a_grown_box_running_into_a_box_the_person_moved_is_a_text_overlap():
    d, before, grown, person = reflow_deck()
    after = {"n1": {**grown, "title": snapshot.tag(SKEY, "text/body/0")}, "t2": person}
    fs = d.check(before, after)
    assert kinds(fs) == ["text_overlap"]
    f = L.failures(fs)[0]
    assert {f["element"], f["other_element"]} == {"text/body/0", "text/body/1"}
    assert f["history"][0]["was"] == "converter" and "recreated" in f["history"][0]["how"]


def test_the_same_run_past_its_own_box_is_a_text_overflow():
    d, before, grown, person = reflow_deck()
    short_box = {**grown, "box": [10.62, 55.66, 720.6, 90.0]}   # the box kept its one-line height
    fs = d.check(before, {"t1": short_box, "t2": person})
    assert kinds(fs) == ["text_overflow"]


def test_texts_that_met_before_the_sync_are_not_the_syncs():
    d, before, grown, person = reflow_deck()
    before = {"t1": {**grown}, "t2": person}      # the person had typed that line in already
    assert d.check(before, {"t1": grown, "t2": person}) == []
    assert kinds(d.existing(before), "note") == ["text_overlap"]
    assert d.existing(before)[0]["by"] == "person"


def test_what_the_new_conversion_draws_overlapping_is_not_the_syncs():
    d, before, grown, person = reflow_deck()
    # the source itself draws a line of box 1 across box 2's first line (at the source's place)
    d.ours[0]["ir"]["paragraphs"].append({"size": 17.0, "runs": [{"size": 17.0}],
                                          "lines": [{"x0": 150.0, "x1": 400.0, "baseline": 150.0}]})
    assert d.check(before, {"t1": grown, "t2": person}) == []


def test_boxes_that_overlap_with_their_words_clear_of_each_other_are_fine():
    d, before, grown, person = reflow_deck()
    beside = text_rb("text/body/1", [300.0, 80.0, 702.7, 120.0], "The second box.")   # right of box 1's short lines
    assert L.meet([grown["box"]], [beside["box"]])       # the boxes do meet
    assert d.check({"t1": before["t1"], "t2": beside}, {"t1": grown, "t2": beside}) == []


def test_without_the_new_conversion_only_a_new_elements_overlap_is_a_note():
    d, before, grown, person = reflow_deck()
    # both stood before the sync: the conversion is not needed to say they did not meet then
    assert kinds(d.check(before, {"t1": grown, "t2": person}, ours=False)) == ["text_overlap"]
    fs = d.check({"t1": before["t1"]}, {"t1": grown, "t2": person}, ours=False)   # box 2 is new
    assert kinds(fs) == [] and kinds(fs, "note") == ["text_overlap"]


def note_deck():
    """Live fuzz r7411: the person put a note of their own under a paragraph; the source adds a line
    to the paragraph, which grows onto the note. Sync never moves the person's own objects."""
    d = Deck()
    para = d.add("text/body/0", "text", "t1", text_rb("text/body/0", [10.62, 55.66, 720.6, 90.0], "The paragraph, one line."))
    note = {**text_rb("x", [40.0, 78.0, 260.0, 106.0], "note under it"), "title": None}
    grown = text_rb("text/body/0", [10.62, 55.66, 720.6, 110.0], "The source adds this line.\nThe paragraph, one line.")
    return d, {"t1": para, "u1": note}, {"n1": grown, "u1": note}


def test_the_source_running_over_the_persons_own_note_fails_unless_the_report_says_so():
    d, before, after = note_deck()
    assert kinds(d.check(before, after)) == ["text_overlap"]
    told = {"overruns": [{"slide": SKEY, "object": "u1", "other": "n1", "depth": 7.8}]}
    fs = d.check(before, after, report=told)
    assert kinds(fs) == [] and kinds(fs, "note") == ["text_overlap"]


def test_overruns_names_the_persons_object_the_source_now_runs_over():
    from beamer2slides import text_layout
    d, before, after = note_deck()
    found = text_layout.overruns({"objects": before}, {"objects": after}, {"u1"})
    assert [(o["object"], o["other"]) for o in found] == [("u1", "n1")] and found[0]["depth"] > 2
    # already over it before the sync: not this sync's doing; about to be deleted: not there
    assert text_layout.overruns({"objects": after}, {"objects": after}, {"u1"}) == []
    assert text_layout.overruns({"objects": before}, {"objects": after}, {"u1"}, skip={"n1"}) == []
    # recreated under a new id but over it as far before: found by its title, not new
    was_over = {"t1": after["n1"], "u1": after["u1"]}
    assert text_layout.overruns({"objects": was_over}, {"objects": after}, {"u1"}) == []


def test_an_old_deep_overlap_does_not_hide_the_new_one():
    """Live fuzz r7411: the person's note already ran over the frame counter, deeper than the
    paragraph now runs over it; the paragraph's overrun is still this sync's."""
    from beamer2slides import text_layout
    d, before, after = note_deck()
    counter = {**text_rb("x", [40.0, 80.0, 260.0, 104.0], "slide 3 of 9 in words"), "title": "b2s:s/counter"}
    found = text_layout.overruns({"objects": {**before, "c1": counter}}, {"objects": {**after, "c1": counter}}, {"u1"})
    assert [(o["object"], o["other"]) for o in found] == [("u1", "n1")]


def test_the_persons_own_arrangement_carried_onto_the_sources_move_is_theirs():
    """Live fuzz r7413: the person moved a paragraph up 30 pt onto the title; the source had moved
    it down, which kept them apart, and now moves it back up: sync carries the person's move on top
    of the source's (`sync.carried`), so the paragraph is where the person put it over the title."""
    d = Deck()
    title = d.add("text/title/0", "text", "t0", text_rb("text/title/0", [6.8, 14.0, 702.7, 50.0], "Room to grow"))
    at_source = text_rb("text/body/0", [10.62, 81.65, 720.6, 110.0], "The paragraph the person moved.")
    para = d.add("text/body/0", "text", "t1", at_source, ours_rb=moved(at_source, dy=-26.0))
    for el, ours, rb in ((d.elements[0], d.ours[0], title), (d.elements[1], d.ours[1], at_source)):
        el["ir"]["bbox"] = list(rb["box"])
        ours["ir"]["bbox"] = list(moved(rb, dy=-26.0 if rb is at_source else 0.0)["box"])
    before = {"t0": title, "t1": moved(para, dy=-30.0)}
    after = {"t0": title, "n1": moved(para, dy=-56.0)}
    from beamer2slides import text_layout
    assert L.meet(text_layout.ink(after["n1"]), text_layout.ink(title), 2.0)     # they do meet now
    assert d.check(before, after) == []
    # the same overlap where the source did not move it back is the sync's
    d.ours[1]["ir"]["bbox"] = list(at_source["box"])
    assert kinds(d.check(before, after)) == ["text_overlap"]


# ---------------------------------------------------------------- text_overflow out of a panel

def block_deck():
    d = Deck()
    d.add("shape/panel/0", "shape", "p0", panel_rb("shape/panel/0", [11.0, 150.4, 709.0, 201.9]))
    body = d.add("text/body/1", "text", "t4", text_rb("text/body/1", [10.62, 170.1, 702.7, 201.0],
                                                     "A source change to a field the deck also edited is reported."))
    return d, body


def test_a_merged_text_running_out_of_its_block_is_a_text_overflow():
    """fuzz-live-refill r2700 step1: the person's 20 pt font merged into a box sized for the
    source's 17 pt line; recreated wider than its panel, it wraps and hangs out of the block."""
    d, body = block_deck()
    big = {**BODY, "fontSize": 20.0}
    text = "A source change to a field the deck also edited is reported as a conflict, side by side."
    before = {"p0": d.elements[0]["readback"]["p0"], "t4": body}
    after = {"p0": before["p0"], "n4": text_rb("text/body/1", [10.62, 170.1, 722.7, 201.0], text,
                                                spans=[(0, len(text), big)])}
    fs = d.check(before, after)
    assert kinds(fs) == ["text_overflow"]
    assert "panel" in L.failures(fs)[0]["detail"]


def test_a_text_already_out_of_its_block_before_is_not_the_syncs():
    d, body = block_deck()
    big = {**BODY, "fontSize": 20.0}
    text = "A source change to a field the deck also edited is reported as a conflict, side by side."
    out = text_rb("text/body/1", [10.62, 170.1, 702.7, 201.0], text, spans=[(0, len(text), big)])
    panel = d.elements[0]["readback"]["p0"]
    assert d.check({"p0": panel, "t4": out}, {"p0": panel, "t4": {**out, "text": text + " More.\n"}}) == []


# ---------------------------------------------------------------- stranded_picture

HOLE = NBSP4 = L.NBSP * 4


def formula_rb(words_before_hole: str, box=(10.62, 140.8, 709.5, 200.0)):
    text = f"{words_before_hole} {NBSP4} and after."
    at = len(words_before_hole) + 1
    return text_rb("text/body/0", list(box), text, spans=[(0, at, BODY), (at, at + 4, MONO), (at + 4, len(text), BODY)])


def hole_box(rb):
    return L.layout(rb)["holes"][0]["box"]


def formula_deck():
    d = Deck()
    text = d.add("text/body/0", "text", "t3", formula_rb("The merge is clean when"))
    h = hole_box(text)
    pic = d.add("image/math/0", "image", "f0", picture_rb("image/math/0", [h[0] + 1, h[1] - 3, h[2] - 1, h[3] + 3]),
                anchor="text/body/0", role="math")
    return d, text, pic


def test_a_formula_picture_the_sync_moved_off_its_hole_is_stranded():
    d, text, pic = formula_deck()
    fs = d.check({"t3": text, "f0": pic}, {"t3": text, "f0": moved(pic, dx=30)})
    assert kinds(fs) == ["stranded_picture"]


def test_a_picture_rewritten_where_the_persons_words_had_moved_its_hole_is_stranded():
    """Agent D's `layout-stranded-formula`: the person wrote "perfectly" before the hole; the
    sync recreated the picture where the source has it, 70 pt short of the hole."""
    d, text, pic = formula_deck()
    edited = formula_rb("The merge is perfectly clean when")
    before = {"t3": edited, "f0": pic}
    after = {"n3": edited, "n0": {**pic}}
    fs = d.check(before, after)
    assert kinds(fs) == ["stranded_picture"]
    assert "rewrote" in L.failures(fs)[0]["detail"]
    assert kinds(d.check(before, before)) == []             # left alone by the sync: the person's
    assert kinds(d.existing(before), "note") == ["stranded_picture"]


def test_a_picture_the_person_put_off_its_hole_stays_theirs():
    d, text, pic = formula_deck()
    theirs = moved(pic, dx=120)
    fs = d.check({"t3": text, "f0": theirs}, {"t3": text, "n0": {**theirs}})
    assert fs == []


def test_a_picture_over_its_hole_is_not_stranded_nor_an_overlap():
    d, text, pic = formula_deck()
    assert d.check({"t3": text, "f0": pic}, {"n3": {**text}, "n0": {**pic}}) == []


# ---------------------------------------------------------------- off_page

def test_a_text_the_sync_moved_off_the_page_is_off_page():
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    fs = d.check({"t9": footer}, {"t9": moved(footer, dx=40)})
    assert kinds(fs) == ["off_page"]


def test_a_text_already_off_the_page_is_not_the_syncs():
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    out = moved(footer, dx=60)
    assert d.check({"t9": out}, {"t9": {**out, "text": "4 / 100\n"}}) == []
    assert kinds(d.existing({"t9": out}), "note") == ["off_page"]


def test_allow_silences_a_kind_on_a_slide():
    d = Deck()
    footer = d.add("text/footer/0", "text", "t9", text_rb("text/footer/0", [640.0, 370.0, 700.0, 400.0], "4 / 10"))
    read = lambda objs: {"page_size": [720.0, 405.0], "slides": [{"objectId": "p1", "objects": objs}]}
    assert L.check(d.base(), read({"t9": footer}), read({"t9": moved(footer, dx=40)}), None, d.conversion(),
                   allow=["off_page/s"]) == []


# ---------------------------------------------------------------- the replay's tables

def test_correlate_counts_steps_with_a_finding_per_edit_kind_and_variant():
    fail = {"kind": "off_page", "severity": "fail"}
    results = [{"findings": [fail], "existing": [], "edit_kinds": ["move"], "variant": "a"},
               {"findings": [], "existing": [fail], "edit_kinds": ["move", "bold"], "variant": "b"}]
    table = L.correlate(results)
    assert table["edit_kinds"]["move"] == {"steps": 2, "with_finding": 1, "kinds": {"off_page": 1}, "rate": 0.5, "lift": 1.0}
    assert table["variants"]["b"]["with_finding"] == 0
    assert L.counts(results) == {"off_page": {"fail": 1, "note": 0, "existing": 1}}
