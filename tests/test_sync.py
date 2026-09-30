"""Offline tests of sync (docs/sync.md): identity, deck edit detection, the merge rules, diff3,
slide planning, and the request helpers. No Google calls."""

import copy
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

import pytest

from beamer2slides.emit import SLIDE_W
from beamer2slides import identity, merge, refit, snapshot
from beamer2slides.extract import frame_labels
from beamer2slides.google_types import Page, PageElement, Presentation, object_id
from beamer2slides.json_types import Json, JsonObject, as_optional_str
from beamer2slides.sync import Built, InPlace, Recovery, Refilled, SlideWork, Sync, TableFill, letterbox_fix, rename

from . import sync_work
from .json_reads import jarr, jat, jint, jnum, jnums, jobj, jobjs, jstr, jstrs

DECKS = Path(__file__).resolve().parent / "decks" / "out"

Fetcher = Callable[[Callable[[str], bytes]], None]
"""The `fetcher` fixture (conftest.py): installs `fn(url) -> bytes` for the test's downloads."""


# ---------------------------------------------------------------- helpers

def run(words: str, **kw: Json) -> JsonObject:
    return {"text": words, "font": "CMSS10", "family": "sans", "size": 10.0, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False, "highlight": None, **kw}


def text_ir(words: str, bbox: Sequence[float], eid: str, role: str, **kw: Json) -> JsonObject:
    return {"id": eid, "kind": "text", "role": role, "bbox": list(bbox), "panel": None, "code": False, "spans": [],
            "strokes": [], "paragraphs": [{
                "align": "left", "level": 0, "bullet": None, "size": 10.0, "text_x0": bbox[0], "tab_x0": None,
                "lines": [{"baseline": bbox[1] + 8 + 12 * k, "x0": bbox[0], "x1": bbox[2]}], "wrap_limit": None,
                "runs": [run(line, **kw)]} for k, line in enumerate(words.split("\n"))]}


def shape_ir(bbox: Sequence[float], eid: str, fill: str) -> JsonObject:
    return {"id": eid, "kind": "shape", "role": "panel", "bbox": list(bbox), "shape": "RECTANGLE", "fill": fill, "flip": False,
            "radius": 0.0}


def object_changes(b: JsonObject, t: JsonObject) -> set[merge.Edit]:
    """`merge.object_changes_of` two read-backs as snapshot.readback writes them."""
    from beamer2slides.sync_model import readback as parse
    return merge.object_changes_of(parse(b, "base read-back"), parse(t, "live read-back"))


def readback(box: Sequence[float], words: str | None) -> JsonObject:
    """A shape's read-back as snapshot writes one, holding `words` (None: no text)."""
    x0, y0, x1, y1 = box
    return {"kind": "shape", "transform": [1.0, 0.0, 0.0, 1.0, x0, y0], "size": [x1 - x0, y1 - y0], "box": list(box),
            "parent_group": None, "z": 0, "title": None, "description": None, "text": words,
            "text_styles": [{"fontFamily": "Lato", "fontSize": 18.0}],
            "paragraph_styles": [{"alignment": "START"}], "text_style_hash": "s0",
            "shape_style": {"fill": {"color": "#dddddd", "alpha": 1.0}}, "shape_style_hash": "h0"}


def image_readback(box: Sequence[float], image: str) -> JsonObject:
    """A picture's read-back, its pixels hashing to `image`."""
    return {**readback(box, None), "kind": "image", "image": {"contentHash": image}}


def group_readback(box: Sequence[float]) -> JsonObject:
    return {**readback(box, None), "kind": "elementGroup"}


def _entry(key: str, ir: JsonObject, oid: str | None, anchor: str | None) -> JsonObject:
    h, fields = identity.ir_fields(ir, None, anchor, None)
    el: JsonObject = {"key": key, "id": ir["id"], "kind": ir["kind"], "role": ir.get("role"), "ir_hash": h, "fields": fields,
                      "fingerprint": identity.fingerprint(ir, None, anchor), "anchor": anchor, "ir": ir}
    if oid:
        box = [2 * v for v in jnums(ir, "bbox")]
        words = merge.predicted_text(ir) if ir["kind"] == "text" else None
        el.update(objects=[oid], main=oid, readback={oid: readback(box, words)})
    return el


def entry(key: str, ir: JsonObject, oid: str | None) -> JsonObject:
    """A base element: IR hashes plus a read-back matching what the converter wrote (2x scale);
    `oid` None: an element of the new conversion, written nowhere yet."""
    return _entry(key, ir, oid, None)


def anchored_entry(key: str, ir: JsonObject, oid: str | None, anchor: str) -> JsonObject:
    """`entry` of an element anchored to the one keyed `anchor`."""
    return _entry(key, ir, oid, anchor)


def ours_entry(key: str, ir: JsonObject) -> JsonObject:
    return _entry(key, ir, None, None)


def anchored_ours_entry(key: str, ir: JsonObject, anchor: str) -> JsonObject:
    return _entry(key, ir, None, anchor)


def base_slide(key: str, sid: str | None, elements: list[JsonObject], label: str | None, title: str) -> JsonObject:
    return {"key": key, "label": label, "title": title, "page": 0,
            "text": " ".join(jstr(e, "fingerprint", "text") for e in elements),
            "layout": "TITLE_ONLY", "background": "color:#ffffff", "notes": "", "objectId": sid, "layoutObjectId": "L",
            "background_readback": {"state": "INHERIT"}, "notes_readback": "", "groups": [],
            "order": [e["main"] for e in elements if "main" in e], "elements": list[Json](elements)}


def live(slide: JsonObject) -> JsonObject:
    """The live slide exactly as the base recorded it."""
    return {"objectId": slide["objectId"], "layoutObjectId": "L", "background": slide["background_readback"], "notes": "",
            "notes_id": f"{jstr(slide, 'objectId')}_notes", "order": list(jarr(slide, "order")),
            "objects": {oid: copy.deepcopy(rb) for e in jobjs(slide, "elements") for oid, rb in jobj(e, "readback").items()}}


def ours_of(slide: JsonObject) -> JsonObject:
    return {**{k: v for k, v in slide.items() if k not in ("objectId", "layoutObjectId", "background_readback",
                                                           "notes_readback", "groups", "order")},
            "elements": [{k: v for k, v in e.items() if k not in ("objects", "main", "readback")}
                         for e in jobjs(slide, "elements")]}


def three_slides() -> JsonObject:
    return many_slides(["intro", "results", "end"])


def many_slides(names: Sequence[str]) -> JsonObject:
    slides: list[Json] = []
    for n, name in enumerate(names):
        sid = f"b2s_s{n:03}"
        els = [entry("text/title/0", text_ir(name.title(), (10, 10, 100, 24), f"p{n}t0", "title"), f"{sid}_t0"),
               entry("text/body/0", text_ir(f"First point of {name}\nSecond point of {name}", (20, 60, 200, 90), f"p{n}t1",
                                            "body"), f"{sid}_t1")]
        slides.append(base_slide(name, sid, els, name, name.title()))
    return {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": slides}


def triple(base: JsonObject) -> tuple[JsonObject, JsonObject]:
    """The new conversion and the live deck of `base` untouched: (ours, theirs)."""
    ours: JsonObject = {"slides": [ours_of(s) for s in jobjs(base, "slides")],
                        "pairs": {str(j): j for j in range(len(jarr(base, "slides")))}}
    theirs: JsonObject = {"revisionId": "r", "slides": [live(s) for s in jobjs(base, "slides")]}
    return ours, theirs


def unit(mplan: JsonObject, slide: str, key: str) -> JsonObject:
    return next(u for p in jobjs(mplan, "slides") if p["key"] == slide for u in jobjs(p.get("units") or []) if u["key"] == key)


def slide_ids(deck: JsonObject) -> list[str]:
    """The live deck's slides, by objectId (what `merge.has_writes` compares the order with)."""
    return [jstr(s, "objectId") for s in jobjs(deck, "slides")]


def report(mplan: JsonObject, part: str) -> list[JsonObject]:
    """One list of a merge plan's report: its `conflicts`, `applied`, `overrides`, ..."""
    return jobjs(mplan, "report", part)


def planned_work(mplan: JsonObject, new_oid: dict[int, str]) -> list[SlideWork]:
    """What `Sync.run` made of `mplan`'s slides: each on the slide its plan names (`objectId`), with
    `new_oid` as the element index -> main object it made there."""
    made: list[SlideWork] = []
    for p in jobjs(mplan, "slides"):
        w = sync_work.slide_work(p)
        w.sid = as_optional_str(p["objectId"], "objectId")
        w.new_oid = dict(new_oid)
        made.append(w)
    return made


# ---------------------------------------------------------------- identity

def test_frame_labels_from_named_destinations():
    dests = [("Doc-Start", 0), ("Navigation1", 0), ("Navigation3", 2), ("last", 2), ("last<1>", 2), ("page.3", 2),
             ("steps", 3), ("steps<1>", 3), ("steps<2>", 4), ("steps<3>", 5), ("gone<1>", -1), ("gone", -1)]
    assert frame_labels(dests) == {2: "last", 3: "steps", 4: "steps", 5: "steps"}


def info(title: str, words: str, label: str | None, page: int) -> identity.SlideInfo:
    return identity.SlideInfo(label=label, title=title, text=words, page=page, removed=False)


def test_inserted_frame_shifts_no_keys():
    base = [info("Intro", "why we do this", None, 0), info("Results", "numbers went up a lot", None, 0),
            info("End", "thanks for listening", None, 0)]
    keys = identity.slide_keys(base)
    assert keys == ["title:intro#1", "title:results#1", "title:end#1"]
    ours = [base[0], info("Method", "how we measured the numbers", None, 0), base[1], base[2]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours, moves=None, weak=None)
    assert pairs == {0: 0, 2: 1, 3: 2}
    assert got == ["title:intro#1", "title:method#1", "title:results#1", "title:end#1"]


def test_renamed_title_keeps_key():
    base = [info("Intro", "why we do this and what it costs", None, 0),
            info("Results", "numbers went up a lot this year", None, 0)]
    keys = identity.slide_keys(base)
    ours = [base[0], info("Findings", "numbers went up a lot this year", None, 0)]
    got, pairs = identity.inherit_slide_keys(base, keys, ours, moves=None, weak=None)
    assert pairs == {0: 0, 1: 1} and got[1] == "title:results#1"


def test_repeated_titles_get_occurrences():
    keys = identity.slide_keys([info("Example", "a", None, 0), info("Example", "b", None, 0), info("", "c", None, 2)])
    assert keys == ["title:example#1", "title:example#2", "page:3"]


def test_labelled_frames_match_wherever_they_moved():
    base = [info("A", "alpha text here", "a", 0), info("B", "beta text here", "b", 0), info("C", "gamma text here", "c", 0)]
    keys = identity.slide_keys(base)
    assert keys == ["a", "b", "c"]
    ours = [info("C renamed", "completely different words", "c", 0), base[0], base[1]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours, moves=None, weak=None)
    assert pairs == {0: 2, 1: 0, 2: 1} and got == ["c", "a", "b"]


def test_labelled_and_unlabelled_mixed():
    base = [info("A", "alpha words here", "a", 0), info("Plain", "some plain words", None, 0),
            info("B", "beta words here", "b", 0)]
    keys = identity.slide_keys(base)
    ours = [info("B", "beta words here", "b", 0), info("Plain", "some plain words, edited", None, 0),
            info("New", "brand new", "new", 0)]
    got, pairs = identity.inherit_slide_keys(base, keys, ours, moves=None, weak=None)
    assert pairs == {0: 2, 1: 1}
    assert got == ["b", "title:plain#1", "new"]
    # A label neither side knows on the other is a label renamed, and then the words decide: the
    # deck's slide keeps its identity instead of coming back beside itself (tests/test_label_moves.py).
    got, pairs = identity.inherit_slide_keys([info("X", "same", "x", 0)], ["x"], [info("X", "same", "y", 0)], moves=None, weak=None)
    assert pairs == {0: 0} and got == ["x"]


def test_element_keys_follow_content():
    els = [text_ir("Title", (10, 10, 100, 24), "p0t0", "title"), text_ir("First paragraph of text", (20, 60, 200, 70), "p0t1", role="body"),
           text_ir("Second paragraph of text", (20, 80, 200, 90), "p0t2", role="body")]
    keys, fps = identity.slide_element_keys(els, None, None)
    assert keys == ["text/title/0", "text/body/0", "text/body/1"]
    base: list[JsonObject] = [{"key": k, "kind": e["kind"], "role": e["role"], "fingerprint": f}
                              for k, e, f in zip(keys, els, fps)]
    # a paragraph inserted before the others: they keep their keys, the new one gets a fresh ordinal
    ours = [els[0], text_ir("A brand new opening remark", (20, 45, 200, 55), "p0t1", role="body"),
            {**els[1], "id": "p0t2"}, {**els[2], "id": "p0t3"}]
    got, _ = identity.slide_element_keys(ours, None, base)
    assert got == ["text/title/0", "text/body/2", "text/body/0", "text/body/1"]


def test_a_shifted_block_keeps_its_bar_and_body_apart():
    """Edit hunt h4-3: the source added a block and every block moved up 17 pt. By geometry alone
    the new body (which reaches up under its bar, emit.merge_blocks) was nearer the old title bar
    than its own old body, so bars and bodies swapped keys and the bars came back under the bodies."""
    def panel(bbox: Sequence[float], fill: str, pid: str) -> JsonObject:
        return {"id": pid, "kind": "shape", "role": "panel", "bbox": list(bbox), "fill": fill}
    v1 = [panel((24, 128.7, 338, 171.9), "#f9e6e6", "a"), panel((24, 84.4, 338, 116.7), "#e9e9f3", "b"),
          panel((24, 84.4, 338, 99.0), "#262686", "c"), panel((24, 128.7, 338, 142.7), "#bf0000", "d")]
    keys, fps = identity.slide_element_keys(v1, None, None)
    records: list[JsonObject] = [{"key": k, "kind": "shape", "role": "panel", "fingerprint": f, "ir": e}
                                 for k, e, f in zip(keys, v1, fps)]
    v2 = [panel((24, 111.0, 338, 154.1), "#f9e6e6", "a"), panel((24, 166.1, 338, 198.5), "#e6efe6", "n"),
          panel((24, 66.6, 338, 99.0), "#e9e9f3", "b"), panel((24, 66.6, 338, 81.3), "#262686", "c"),
          panel((24, 166.1, 338, 180.8), "#006000", "m"), panel((24, 111.0, 338, 125.0), "#bf0000", "d")]
    want = ["shape/panel/0", "shape/panel/4", "shape/panel/1", "shape/panel/2", "shape/panel/5", "shape/panel/3"]
    assert identity.slide_element_keys(v2, None, identity.base_items(records))[0] == want
    # a base written before fingerprints had a look: the fill its IR records tells them apart
    old: list[JsonObject] = [{**r, "fingerprint": {k: v for k, v in jobj(r, "fingerprint").items() if k != "look"}}
                             for r in records]
    assert identity.slide_element_keys(v2, None, identity.base_items(old))[0] == want


def test_a_diagram_that_became_a_picture_is_still_the_figure():
    """Edit hunt h3-3: the source added a node and classify refused the busier TikZ figure as a
    diagram. As two elements, the person's edited diagram was kept and the picture stacked on it."""
    diagram: JsonObject = {"id": "p0d0", "kind": "diagram", "role": "figure", "bbox": [45, 90, 318, 155],
                           "nodes": [{"paragraphs": [[{"text": "SP tree"}]]}]}
    keys, fps = identity.slide_element_keys([diagram], None, None)
    base: list[JsonObject] = [{"key": keys[0], "kind": "diagram", "role": "figure", "fingerprint": fps[0]}]
    picture: JsonObject = {"id": "p0f0", "kind": "image", "role": "figure", "bbox": [30, 88, 300, 168]}
    assert identity.slide_element_keys([picture], None, base)[0] == ["diagram/figure/0"]
    # ... but a picture somewhere else is a picture the source added
    elsewhere: JsonObject = {**picture, "bbox": [30, 200, 300, 260]}
    assert identity.slide_element_keys([elsewhere], None, base)[0] == ["image/figure/0"]


def test_anchored_elements_take_their_anchors_key():
    words = text_ir("A formula here and more words", (20, 60, 200, 70), "p0t1", role="body")
    pic: JsonObject = {"id": "p0h0", "kind": "image", "role": "math", "bbox": [80, 60, 100, 70], "anchor": "p0t1"}
    keys, fps = identity.slide_element_keys([words, pic], None, None)
    assert keys == ["text/body/0", "image/math/0"] and fps[1]["anchor"] == "text/body/0"


def test_ir_hash_ignores_ids_and_page_numbers():
    a = text_ir("Go to results", (20, 60, 200, 70), "p3t1", link="#page=7", role="body")
    b: JsonObject = {**text_ir("Go to results", (20, 60, 200, 70), "p4t1", link="#page=8", role="body"),
                     "spans": ["p4s1", "p4s2"]}
    ha, _ = identity.ir_fields(a, None, None, lambda page: "results")
    hb, _ = identity.ir_fields(b, None, None, lambda page: "results")
    assert ha == hb
    hc, _ = identity.ir_fields(b, None, None, lambda page: "method")
    assert hc != ha
    # render output (how a bare image reached its file, its pixel size) is no source change either
    img: JsonObject = {"id": "p1i0", "kind": "image", "role": "figure", "bbox": [10, 10, 60, 40], "file": "figures/p1i0.png"}
    assert identity.ir_fields(img, None, None, None)[0] == \
        identity.ir_fields({**img, "picture": "raw", "px": [800, 480]}, None, None, None)[0]


def test_source_changes_by_field():
    old = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70), eid="p0t1", role="body"), oid=None)
    reworded = entry("text/body/0", text_ir("Authors and their AI keep writing", (20, 60, 160, 70), eid="p0t1", role="body"), oid=None)
    moved = entry("text/body/0", text_ir("Authors keep writing", (20, 90, 120, 100), eid="p0t1", role="body"), oid=None)
    red = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70), color="#ff0000", eid="p0t1", role="body"), oid=None)
    assert identity.source_changes(old, old) == set()
    assert identity.source_changes(old, reworded) == {"text", "size"}
    assert identity.source_changes(old, moved) == {"position"}
    assert identity.source_changes(old, red) == {"style"}


# ---------------------------------------------------------------- deck edits

def test_object_changes():
    b = readback([10, 10, 110, 30], "Hello\n")
    assert object_changes(b, copy.deepcopy(b)) == set()
    moved: JsonObject = {**b, "box": [20, 10, 120, 30], "transform": [1, 0, 0, 1, 20, 10]}
    assert object_changes(b, moved) == {"geometry"}
    assert object_changes(b, {**b, "box": [10.03, 10, 110.03, 30]}) == set()  # read-back noise
    assert object_changes(b, {**b, "text": "Hello world\n"}) == {"text"}
    assert object_changes(b, {**b, "text_style_hash": "s1"}) == {"text_style"}
    assert object_changes(b, {**b, "shape_style_hash": "h1"}) == {"shape_style"}
    assert object_changes(b, {**b, "parent_group": "g"}) == {"group"}


def test_deck_edits_and_user_objects():
    base = three_slides()
    s = jobj(base, "slides", 0)
    theirs = live(s)
    assert merge.deck_edits(jobj(s, "elements", 1), theirs) == {}
    objects = jobj(theirs, "objects")
    del objects["b2s_s000_t1"]
    objects["copy"] = {**readback([0, 0, 5, 5], "x"), "title": "b2s:intro/text/title/0"}
    objects["mine"] = readback([0, 0, 5, 5], "y")
    assert merge.deck_edits(jobj(s, "elements", 1), theirs) == {"deleted": ["b2s_s000_t1"]}
    assert merge.user_objects(s, theirs) == [{"objectId": "copy", "copy_of": "intro/text/title/0"},
                                            {"objectId": "mine", "copy_of": None}]


def test_ungrouped_unit_is_a_group_edit_not_a_part_deletion():
    """Ungrouping a formula's group in Slides removes the `_g` object: a group edit (the unit is
    rebuilt ungrouped), not a part deletion (which kept the whole unit as a conflict)."""
    base = three_slides()
    el = jobj(base, "slides", 0, "elements", 1)
    jarr(el, "objects").append("b2s_s000_t1_g")
    jobj(el, "readback")["b2s_s000_t1_g"] = group_readback([20, 60, 200, 90])
    jobj(el, "readback", "b2s_s000_t1")["parent_group"] = "b2s_s000_t1_g"
    theirs = live(jobj(base, "slides", 0))
    del jobj(theirs, "objects")["b2s_s000_t1_g"]
    jobj(theirs, "objects", "b2s_s000_t1")["parent_group"] = None
    assert {k: sorted(v) for k, v in merge.deck_edits(el, theirs).items()} == {"group": ["b2s_s000_t1", "b2s_s000_t1_g"]}
    ours, live_deck = triple(base)
    jarr(live_deck, "slides")[0] = theirs
    jarr(ours, "slides", 0, "elements")[1] = ours_entry(
        "text/body/0", text_ir("First point of intro, reworded\nSecond point of intro", (20, 60, 230, 90), "p0t1", role="body"))
    u = unit(merge.plan_merge(base, ours, live_deck), "intro", "text/body/0")
    assert u["action"] == "recreate"


def test_uniform_style_changes():
    base: list[JsonObject] = [{"fontFamily": "Lato", "fontSize": 18.0}, {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    red: list[JsonObject] = [{**s, "foregroundColor": "#cc0000"} for s in base]
    assert merge.uniform_changes(base, base, False) == {}
    assert merge.uniform_changes(base, red, False) == {"foregroundColor": "#cc0000"}
    one_word = base + [{"fontFamily": "Lato", "fontSize": 18.0, "italic": True}]
    assert merge.uniform_changes(base, one_word, False) is None


# ---------------------------------------------------------------- diff3 and text edits

def test_diff3_clean_merges():
    base = "Colleagues polish the slides in Google Slides"
    assert merge.diff3(base, base, base) == (base, [])
    assert merge.diff3(base, "Colleagues refine the slides in Google Slides", "Designers polish the slides in Google Slides") == \
        ("Designers refine the slides in Google Slides", [])
    assert merge.diff3(base, "Colleagues polish the slides in Slides", base)[0] == "Colleagues polish the slides in Slides"
    # the same change on both sides is no conflict
    same = "Colleagues polish all the slides in Google Slides"
    assert merge.diff3(base, same, same) == (same, [])
    # paragraphs apart
    merged, clashes = merge.diff3("One\nTwo\nThree\n", "One more\nTwo\nThree\n", "One\nTwo\nThree!\n")
    assert merged == "One more\nTwo\nThree!\n" and not clashes


def test_diff3_conflicts_keep_theirs():
    base = "Colleagues polish the slides"
    merged, clashes = merge.diff3(base, "Teammates polish the slides", "Designers polish the slides")
    assert merged == "Designers polish the slides"
    assert clashes == [{"base": "Colleagues", "ours": "Teammates", "theirs": "Designers"}]
    # insertions at the same place differ
    merged, clashes = merge.diff3("a b", "a x b", "a y b")
    assert clashes and merged == "a y b"


def apply_text_requests(text: str, reqs: Sequence[JsonObject]) -> str:
    """The requests applied the way Slides applies them - including its refusal to touch the
    newline the text ends on, which it reads back but does not count in the length it will accept
    (`merge.text_edit_requests`). An applier that quietly clips instead would have let a batch
    through that the API refuses, and with it the whole sync."""
    units = list(text)  # (ASCII in these tests: UTF-16 indices are character indices)
    length = len(units) - 1 if text.endswith("\n") else len(units)
    for r in reqs:
        if "deleteText" in r:
            rng = jobj(r, "deleteText", "textRange")
            assert jint(rng, "endIndex") <= length, (
                f"Invalid deleteText: the end index ({rng['endIndex']}) should not be greater "
                f"than the existing text length ({length}).")
            del units[jint(rng, "startIndex"):jint(rng, "endIndex")]
        else:
            i = jint(r, "insertText", "insertionIndex")
            assert i <= length, f"Invalid insertText: insertion index {i} past the text ({length})"
            units[i:i] = list(jstr(r, "insertText", "text"))
        length = len(units) - 1 if units and units[-1] == "\n" else len(units)
    return "".join(units)


@pytest.mark.parametrize("current,target", [
    ("Authors keep writing\nThe end\n", "Authors and their AI keep writing\nThe end\n"),
    ("Designers polish the slides\n", "Teammates polish all slides\n"),
    ("One\nTwo\nThree\n", "One\nThree\n"),
    ("abc\n", "abc\n"),
    # the last paragraph is the one the deck deleted, so the diff's last hunk runs to the end
    ("One\nTwo\nThree\n", "One\nTwo\n"),
    ("One\nTwo\n", "One\n"),
    ("The merge is clean.\nStep two writes it back.\n", "The merge is clean.\n"),
    ("The end\n", "The end, rewritten\n"),   # and an append lands before that newline
    ("The end\n", "\n"),
])
def test_text_edit_requests(current: str, target: str) -> None:
    assert apply_text_requests(current, merge.text_edit_requests("t", current, target, None)) == target


def test_text_edit_requests_count_utf16():
    reqs = merge.text_edit_requests("t", "\U0001d465 is x\n", "\U0001d465 is y\n", None)
    assert jat(reqs[0], "deleteText", "textRange") == {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 7}


# ---------------------------------------------------------------- merge rules

def test_no_changes_plans_no_writes():
    base = three_slides()
    ours, theirs = triple(base)
    mplan = merge.plan_merge(base, ours, theirs)
    assert all(u["action"] == "keep" for p in jobjs(mplan, "slides") for u in jobjs(p, "units"))
    assert not merge.has_writes(mplan, slide_ids(theirs))
    assert jat(mplan, "report", "applied") == [] and jat(mplan, "report", "conflicts") == []


def edit_text(slide: Json, oid: str, text: str) -> None:
    jobj(slide, "objects", oid)["text"] = text


def test_source_change_applied_when_deck_untouched():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1", role="body"))
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {}
    assert merge.has_writes(mplan, slide_ids(theirs))


def test_deck_edit_kept_when_source_unchanged():
    base = three_slides()
    ours, theirs = triple(base)
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "Something else entirely\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    assert jat(mplan, "report", "overrides") == [{"slide": "results", "element": "text/body/0", "fields": ["text"]}]
    assert not merge.has_writes(mplan, slide_ids(theirs))


def test_source_text_and_deck_geometry_recreate_at_deck_place():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 60, 200, 90), "p0t1", role="body"))
    obj = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
    obj["box"] = [v + 30 for v in jnums(obj, "box")]
    t = jarr(obj, "transform")
    t[4] = jnum(t, 4) + 30
    t[5] = jnum(t, 5) + 30
    u = unit(merge.plan_merge(base, ours, theirs), "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {"geometry": {"mode": "delta"}}


def test_both_moved_carries_the_person_move_onto_the_source_place():
    """Both sides moved the unit: still a conflict, but what is written is the person's move (and
    size) on top of the source's new place, not the deck's absolute place (docs/project-notes.md
    "Both-moved geometry": that dropped the person's resize and ignored the source's reflow)."""
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 100, 200, 130), "p0t1", role="body"))
    obj = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
    obj["box"] = [v + 30 for v in jnums(obj, "box")]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["overrides"] == {"geometry": {"mode": "delta"}}
    clash, = jobjs(mplan, "report", "conflicts")
    assert clash["field"] == "geometry" and clash["resolution"] == merge.GEOMETRY_CARRIED
    # the three boxes in one unit, the deck's (edit hunt h2-5: base and source were in PDF pt)
    s = base["scale"] = 2.0
    scaled, = jobjs(merge.plan_merge(base, ours, theirs), "report", "conflicts")
    fp = jnums(base, "slides", 0, "elements", 1, "fingerprint", "bbox")
    assert scaled["base"] == [v * s for v in fp] and scaled["ours"] == [20 * s, 100 * s, 200 * s, 130 * s]
    del base["scale"]
    # --take-source on it: the source's place and size, nothing of the person's written.
    again = merge.plan_merge_with(base, ours, theirs, adopt=None, follow_labels=False, take_source=[jstr(clash, "id")])
    assert unit(again, "intro", "text/body/0")["overrides"] == {}


def test_carried_puts_the_person_edit_on_the_source_place():
    """`sync.carried`, the RELATIVE transform written on a recreated unit: the person's move lands on
    the source's new place, and the person's resize keeps its proportions from the new corner
    instead of scaling the source's move with it."""
    from beamer2slides import snapshot
    from beamer2slides.sync import carried
    base_rb: JsonObject = {"box": [100, 100, 300, 150], "transform": [2, 0, 0, 0.5, 100, 100]}   # 100 x 100 at 2 x 0.5
    # the person moved it 40 right and made it 1.5x as tall (from its top edge)
    theirs_rb: JsonObject = {"box": [140, 100, 340, 175], "transform": [2, 0, 0, 0.75, 140, 100]}
    # the source recreated it 31 pt lower, with the converter's size
    new_rb: JsonObject = {"box": [100, 131, 300, 181], "transform": [2, 0, 0, 0.5, 100, 131]}
    d = carried(base_rb, theirs_rb, new_rb)
    t = snapshot.compose(d, jnums(new_rb, "transform"))
    assert snapshot.box(t, 100, 100) == [140, 131, 340, 206]
    # the source did not move it: the person's edit as it is (`delta`, theirs * base^-1)
    same = carried(base_rb, theirs_rb, base_rb)
    assert same == pytest.approx(snapshot.compose(jnums(theirs_rb, "transform"), snapshot.invert(jnums(base_rb, "transform"))))
    # a pure move is the same step wherever the source put it
    moved: JsonObject = {"box": [140, 100, 340, 150], "transform": [2, 0, 0, 0.5, 140, 100]}
    assert carried(base_rb, moved, new_rb) == pytest.approx([1, 0, 0, 1, 40, 0])


def test_both_text_clean_diff3():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point of the intro",
                                                                         (20, 60, 200, 90), "p0t1", role="body"))
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", "First idea of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and set(jobj(u, "overrides")) == {"text"}
    assert not jat(mplan, "report", "conflicts")


def test_both_text_overlapping_is_a_conflict():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1", role="body"))
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "keep"
    (c,) = jobjs(mplan, "report", "conflicts")
    assert c["field"] == "text" and c["resolution"] == "deck kept" and "Best point" in jstr(c, "theirs")


def test_a_conflict_in_one_bullet_does_not_cost_the_others():
    """Found by the stress deck (churn): the source rewrote three bullets of a box while the person
    had changed one word in the third. The box merged as one run of words, so the clash in the
    third decided all three and the source's first two were dropped; sync then reported `deck kept`
    and wrote nothing. Each paragraph is now merged on its own."""
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
        "Every timing comes from this deck\nSeconds are rounded, milliseconds dropped", (20, 60, 200, 90), "p0t1", role="body"))
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1",
              "First point of intro\nThe numbers are rounded to full seconds\n")
    mplan = merge.plan_merge(base, ours, theirs)
    (c,) = jobjs(mplan, "report", "conflicts")
    assert c["field"] == "text" and c["resolution"] == "deck kept"
    # only the bullet both sides rewrote is the conflict, not the whole box
    assert c["ours"] == "Seconds are rounded, milliseconds dropped"
    assert c["theirs"] == "The numbers are rounded to full seconds"
    # ... and the source's other bullet is written after all
    assert unit(mplan, "intro", "text/body/0")["action"] == "recreate"


def test_a_paragraph_both_sides_rewrote_is_taken_whole_from_the_deck():
    """The word-level merge would put the person's word where the source's sentence has another
    one. A paragraph both sides rewrote is the deck's, entire."""
    merged, conflicts, safe = merge.text_merge(
        "Timings are measured on the deck\nThe numbers are rounded to whole seconds\n",
        "Every timing comes from this deck\nSeconds are rounded, milliseconds are dropped\n",
        "Timings are measured on the deck\nThe numbers are rounded to full seconds\n", ())
    assert safe and len(conflicts) == 1
    assert merged == "Every timing comes from this deck\nThe numbers are rounded to full seconds\n"
    # the word-level merge on its own writes a line neither side ever wrote
    spliced, _ = merge.diff3("The numbers are rounded to whole seconds",
                             "Seconds are rounded, milliseconds are dropped",
                             "The numbers are rounded to full seconds")
    assert spliced == "Seconds are rounded, milliseconds full dropped"


def test_a_bullet_the_source_added_arrives_beside_one_both_sides_rewrote():
    """Edit hunt h6b: the person reworded one bullet, the source reworded it too and appended a
    new one. Merged as one run of words, the clash kept the deck's whole box and the new bullet
    never came, at two syncs in a row. Lined up paragraph by paragraph, only the clash is the deck's."""
    base = "Replay is costly\nMaps are dense\nCan maps be compressed losslessly?\n"
    ours = "Replay is costly\nMaps are dense\nCan maps be compressed compactly?\nAblation: which layers need it?\n"
    theirs = "Replay is costly\nMaps are dense\nCan maps be compressed efficiently?\n"
    merged, conflicts, safe = merge.text_merge(base, ours, theirs, ())
    assert safe and merged == ("Replay is costly\nMaps are dense\nCan maps be compressed efficiently?\n"
                               "Ablation: which layers need it?\n")
    assert [(c["paragraph"], c["ours"], c["theirs"]) for c in conflicts] == \
        [(2, "Can maps be compressed compactly?", "Can maps be compressed efficiently?")]
    # the whole-box word merge it replaces lost the new bullet with the clash
    assert merge.diff3(base, ours, theirs)[1]
    # and a person who reads the report can still take the source's words for that one bullet
    taken, left, _ = merge.text_merge(base, ours, theirs, [2])
    assert taken == ours and left == []


def test_paragraphs_line_up_around_what_either_side_added_or_removed():
    # the source added a bullet, the deck edited another: both land, no conflict
    assert merge.text_merge("one\ntwo\n", "one\nfirst\ntwo\n", "one\ntwo edited\n", ()) == \
        ("one\nfirst\ntwo edited\n", [], True)
    # the deck added a bullet in front of a clash: the conflict is named by where it is in the deck
    merged, conflicts, _ = merge.text_merge("a\nb\nc\n", "a\nb\nc source\nd\n", "a\nnew\nb\nc deck\n", ())
    assert merged == "a\nnew\nb\nc deck\nd\n" and [c["paragraph"] for c in conflicts] == [3]
    assert merge.text_merge("a\nb\nc\n", "a\nb\nc source\nd\n", "a\nnew\nb\nc deck\n", [3])[0] == \
        "a\nnew\nb\nc source\nd\n"
    # both inserted at one place: the deck's stays, one conflict
    merged, conflicts, _ = merge.text_merge("a\nb\n", "a\nb\nfrom the source\n", "a\nb\nfrom the deck\n", ())
    assert merged == "a\nb\nfrom the deck\n"
    assert [(c["paragraph"], c["ours"], c["theirs"]) for c in conflicts] == [(2, "from the source", "from the deck")]
    # the person removed a bullet the source rewrote: the removal stands, as a conflict
    merged, conflicts, _ = merge.text_merge("a\nb\nc\n", "a\nb rewritten\nc\nd\n", "a\nc\n", ())
    assert merged == "a\nc\nd\n" and [c["base"] for c in conflicts] == ["b"]


def test_same_text_on_both_sides_converges():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1", role="body"))
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", "Main point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert jat(mplan, "report", "converged") == [{"slide": "intro", "element": "text/body/0", "field": "text",
                                             "value": "Main point of intro\nSecond point of intro\n"}]
    # (nothing to write: the deck already shows it; the base adopts the deck's text)
    assert unit(mplan, "intro", "text/body/0")["action"] == "adopt"
    assert not jat(mplan, "report", "applied") and not merge.has_writes(mplan, slide_ids(theirs))


# ------------------------------------------------- taking the source's version of one conflict

THREE_FROM_THE_BASE = "One from the base\nTwo from the base\nThree from the base"


def take_base(text: str) -> JsonObject:
    """`intro` with a body box of however many paragraphs the case wants."""
    base = three_slides()
    slide = jobj(base, "slides", 0)
    jarr(slide, "elements")[1] = entry("text/body/0", text_ir(text, (20, 60, 200, 110), "p0t1", role="body"), "b2s_s000_t1")
    slide["text"] = " ".join(jstr(e, "fingerprint", "text") for e in jobjs(slide, "elements"))
    slide["order"] = [e["main"] for e in jobjs(slide, "elements") if "main" in e]
    return base


def take_case(base: JsonObject, source: str, deck: str) -> tuple[JsonObject, JsonObject]:
    """The source rewrites intro's body to `source`, a person in the deck to `deck` (which ends on
    the newline Slides keeps)."""
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(source, (20, 60, 200, 110), "p0t1", role="body"))
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", deck + "\n")
    return ours, theirs


def only_conflict(mplan: JsonObject, field: str) -> JsonObject:
    (c,) = [x for x in jobjs(mplan, "report", "conflicts") if x["field"] == field]
    return c


def test_a_conflict_id_names_those_two_changes_and_nothing_else():
    """The id is a hash of the spot and the three versions, so it is the same on every run while
    the same two changes stand against each other, and a different one the moment either moves.
    That is the whole safety of `--take-source`: an id copied out of an older report cannot land
    on a disagreement that has since become another one."""
    base = take_base(THREE_FROM_THE_BASE)
    args = ("One from the source\nTwo from the source\nThree from the base",
            "One from the base\nTwo from the deck\nThree from the base")
    first = only_conflict(merge.plan_merge(base, *take_case(base, *args)), "text")
    again = only_conflict(merge.plan_merge(base, *take_case(base, *args)), "text")
    assert jstr(first, "id") == jstr(again, "id") and len(jstr(first, "id")) == 8
    moved = only_conflict(merge.plan_merge(base, *take_case(
        base, args[0], "One from the base\nTwo from the deck, reworded\nThree from the base")), "text")
    assert jstr(moved, "id") != jstr(first, "id")


def test_take_source_writes_that_paragraph_and_leaves_the_others_alone():
    """Two paragraphs clash; the person reads the report and says the source is right about one of
    them. That one is written, the other stays as the deck has it, and the paragraph only the
    source touched was never in question."""
    base = take_base(THREE_FROM_THE_BASE)
    args = ("One from the source\nTwo from the source\nThree from the source",
            "One from the base\nTwo from the deck\nThree from the deck")
    mplan = merge.plan_merge(base, *take_case(base, *args))
    two = next(c for c in jobjs(mplan, "report", "conflicts") if c["ours"] == "Two from the source")
    ours, theirs = take_case(base, *args)
    mplan = merge.plan_merge_with(base, ours, theirs, adopt=None, follow_labels=False, take_source=[jstr(two, "id")])
    ov = jat(unit(mplan, "intro", "text/body/0"), "overrides", "text")
    assert jat(ov, "take") == [1]
    # What sync writes: `override_requests` re-merges the deck's text back onto the element it has
    # just recreated, which holds the source's words, with the same list of paragraphs.
    current = merge.predicted_text(jobj(ours, "slides", 0, "elements", 1, "ir"))
    written, _, safe = merge.text_merge(jstr(ov, "base"), current, jstr(ov, "theirs"), [jint(t) for t in jarr(ov, "take")])
    assert safe and written == "One from the source\nTwo from the source\nThree from the deck\n"
    resolutions = {c["ours"]: c["resolution"] for c in jobjs(mplan, "report", "conflicts")}
    assert resolutions == {"Two from the source": merge.TAKEN_SAYS, "Three from the source": "deck kept"}


def test_what_take_source_wrote_over_is_kept_verbatim_in_the_report():
    """A person's words are about to stop existing anywhere: the report is the way back, and it has
    to hold them, because a minute from now nothing else will."""
    base = take_base(THREE_FROM_THE_BASE)
    args = ("One from the source\nTwo from the source\nThree from the base",
            "One from the base\nTwo from the deck\nThree from the base")
    cid = jstr(only_conflict(merge.plan_merge(base, *take_case(base, *args)), "text"), "id")
    report = jobj(merge.plan_merge_with(base, *take_case(base, *args), adopt=None, follow_labels=False, take_source=[cid]), "report")
    assert jat(report, "resolved") == [{"id": cid, "slide": "intro", "element": "text/body/0",
                                   "field": "text", "was": "Two from the deck"}]


def test_taking_a_whole_box_makes_the_decks_text_no_override_at_all():
    """Where nothing of the source survived the merge the conflict is the whole box, and taking it
    writes the source's text entire - so the deck's edit is not an override any more and the report
    must not promise it was kept."""
    base = take_base("One from the base\nTwo from the base")
    args = ("One from the source\nTwo from the source", "One from the deck\nTwo from the deck")
    cid = jstr(only_conflict(merge.plan_merge(base, *take_case(base, *args)), "text"), "id")
    mplan = merge.plan_merge_with(base, *take_case(base, *args), adopt=None, follow_labels=False, take_source=[cid])
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {}
    assert jat(mplan, "report", "overrides") == []
    assert jat(mplan, "report", "resolved", 0, "was") == "One from the deck\nTwo from the deck\n"


def test_an_id_that_matches_nothing_settles_nothing_and_says_so():
    """A report a version old cannot reach today's conflict. Nothing is written on that account,
    and the run says which id found no home rather than dropping it."""
    base = take_base(THREE_FROM_THE_BASE)
    mplan = merge.plan_merge_with(base, *take_case(
        base, "One from the source\nTwo from the source\nThree from the base",
        "One from the base\nTwo from the deck\nThree from the base"), adopt=None, follow_labels=False, take_source=["0badcafe"])
    assert only_conflict(mplan, "text")["resolution"] == "deck kept"
    assert jat(mplan, "report", "resolved") == []
    (w,) = jarr(mplan, "report", "warnings")
    assert jstr(w).startswith("--take-source 0badcafe: no conflict in this sync has that id")


def test_a_geometry_conflict_can_be_settled_for_the_source():
    """Both sides moved the element: `--take-source` puts it back where the source draws it."""
    base = three_slides()

    def case():
        ours, theirs = triple(base)
        jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
            "First point of intro\nSecond point, changed", (20, 100, 200, 130), "p0t1", role="body"))
        obj = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
        obj["box"] = [v + 30 for v in jnums(obj, "box")]
        return ours, theirs

    cid = jstr(only_conflict(merge.plan_merge(base, *case()), "geometry"), "id")
    mplan = merge.plan_merge_with(base, *case(), adopt=None, follow_labels=False, take_source=[cid])
    u = unit(mplan, "intro", "text/body/0")
    assert "geometry" not in jobj(u, "overrides") and u["action"] == "recreate"
    assert only_conflict(mplan, "geometry")["resolution"] == merge.TAKEN_SAYS


def test_a_background_and_a_note_can_be_settled_for_the_source():
    base = three_slides()

    def case():
        ours, theirs = triple(base)
        jobj(ours, "slides", 0)["background"] = "color:#eeeeee"
        jobj(ours, "slides", 0)["notes"] = "Say the numbers are new"
        jobj(theirs, "slides", 0)["background"] = {"state": "RENDERED", "solidFill": {"color": "#fff2cc"}}
        jobj(theirs, "slides", 0)["notes"] = "Mention the deadline"
        return ours, theirs

    plain = merge.plan_merge(base, *case())
    fields = {c["field"]: c for c in jobjs(plain, "report", "conflicts")}
    assert set(fields) == {"background", "notes"} and all(c["takeable"] for c in fields.values())
    mplan = merge.plan_merge_with(base, *case(), adopt=None, follow_labels=False, take_source=[jstr(c, "id") for c in fields.values()])
    plan = next(p for p in jobjs(mplan, "slides") if jat(p, "key") == "intro")
    assert jat(plan, "background") == "color:#eeeeee" and jat(plan, "notes") == "Say the numbers are new"
    assert {r["field"] for r in jobjs(mplan, "report", "resolved")} == {"background", "notes"}


def test_existence_is_never_settled_for_the_source():
    """`--take-source` decides what something *says*. Whether it exists at all is another question:
    overwriting text leaves the deck's version in the report as the way back, deleting leaves
    nothing, and a deletion nobody can undo is what `--force-rebuild` is for. So a `removed`, a
    `deleted` or a whole slide the deck kept carries an id to talk about and refuses to be taken."""
    base = three_slides()
    ours, theirs = triple(base)
    del jarr(ours, "slides", 2, "elements")[1]
    edit_text(jat(theirs, "slides", 2), "b2s_s002_t1", "Edited in the deck\n")
    mplan = merge.plan_merge(base, ours, theirs)
    (c,) = jobjs(mplan, "report", "conflicts")
    assert c["field"] == "removed" and "takeable" not in c and c["id"]
    # and naming it anyway settles nothing at all
    ours, theirs = triple(base)
    del jarr(ours, "slides", 2, "elements")[1]
    edit_text(jat(theirs, "slides", 2), "b2s_s002_t1", "Edited in the deck\n")
    again = merge.plan_merge_with(base, ours, theirs, adopt=None, follow_labels=False, take_source=[jstr(c, "id")])
    assert unit(again, "end", "text/body/0")["action"] == "keep"
    assert jat(again, "report", "resolved") == [] and len(jarr(again, "report", "warnings")) == 1


def test_every_takeable_conflict_is_about_what_something_says():
    """The line is drawn once, in `merge.TAKEABLE_FIELDS`, and a field added to a report later has
    to be put on one side of it on purpose."""
    assert set(merge.TAKEABLE_FIELDS).isdisjoint({"removed", "deleted", "part_deleted", "slide", "label"})


def test_every_conflict_carries_an_id():
    """An id is on every conflict, since it is also how one talks about one that cannot be taken.
    Three kinds were built without one until the report became records (`merge.Conflict` has no
    conflict without an id): a unit the source changed whose object the deck deleted (`deleted`),
    one the deck took a part of (`part_deleted`), and a label found on another frame (`label`)."""
    reworded = text_ir("First point of end, reworded\nSecond point of end", (20, 60, 200, 90), "p2t1", role="body")
    # the deck deleted the object the source rewrote
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 2, "elements")[1] = ours_entry("text/body/0", reworded)
    del jobj(theirs, "slides", 2, "objects")["b2s_s002_t1"]
    (c,) = jobjs(merge.plan_merge(base, ours, theirs), "report", "conflicts")
    assert c["field"] == "deleted" and c["id"] and "takeable" not in c
    assert list(c) == ["slide", "element", "id", "field", "base", "ours", "theirs", "resolution"]
    # the deck deleted a part of it
    base = three_slides()
    el = jat(base, "slides", 2, "elements", 1)
    jarr(el, "objects").append("b2s_s002_t1_pic")
    jobj(el, "readback")["b2s_s002_t1_pic"] = image_readback([40, 120, 60, 140], "aaa")
    ours, theirs = triple(base)
    jarr(ours, "slides", 2, "elements")[1] = ours_entry("text/body/0", reworded)
    del jobj(theirs, "slides", 2, "objects")["b2s_s002_t1_pic"]
    (c,) = jobjs(merge.plan_merge(base, ours, theirs), "report", "conflicts")
    assert c["field"] == "part_deleted" and c["id"] and c["theirs"] == ["b2s_s002_t1_pic"]
    # a label on another frame
    report = merge.empty_report()
    merge.report_label_moves([{"label": "end", "verdict": "unsure", "ours": 2, "slide": "end", "frame_is": None,
                               "slide_is": None, "base_title": "End", "ours_title": "Results"}], report, held=True)
    (c,) = jobjs(report, "conflicts")
    assert jat(c, "field") == "label" and jat(c, "id") == merge.conflict_id("end", None, "label", jat(c, "base"), jat(c, "ours"), None)


def test_the_report_shows_three_sides_and_how_to_take_the_source():
    """What a person opens a conflict report to see: the base, what the source now says, what they
    wrote - and, where it can be, the one thing they type to choose the source."""
    from beamer2slides.sync import conflict_lines
    text = conflict_lines({"slide": "intro", "element": "text/body/0", "id": "abc12345", "field": "text",
                           "base": "Two from the base", "ours": "Two from the source",
                           "theirs": "Two from the deck", "resolution": "deck kept", "takeable": True})
    assert "`intro` / `text/body/0`: **text**, deck kept" in text
    assert "    > Two from the deck" in text and "    > Two from the source" in text
    assert "`--take-source abc12345`" in text
    # a conflict already settled does not offer to settle itself again
    taken = conflict_lines({"slide": "intro", "element": None, "id": "abc12345", "field": "notes",
                            "base": "", "ours": "a", "theirs": "b", "resolution": merge.TAKEN_SAYS,
                            "takeable": True})
    assert "to write the source's version here instead" not in taken and "(nothing)" in taken


TABLE_BOX = (20, 60, 200, 120)


def table_ir(rows: Sequence[Sequence[str]], bbox: Sequence[float], eid: str) -> JsonObject:
    return {"id": eid, "kind": "table", "role": "table", "bbox": list(bbox), "frame": list(bbox), "size": 10.0,
            "row_baselines": [bbox[1] + 12 * k for k in range(len(rows))], "row_heights": [12.0] * len(rows),
            "columns": [{"x0": bbox[0] + 60 * c, "x1": bbox[0] + 60 * (c + 1), "align": "left"} for c in range(len(rows[0]))],
            "bounds": list(bbox), "cells": [[[run(cell)] for cell in row] for row in rows], "merges": [], "rules": [],
            "borders": [], "fills": [], "spans": []}


def table_slides(rows: Sequence[Sequence[str]]) -> JsonObject:
    """three_slides with a table on the results slide (element key table/table/0)."""
    base = three_slides()
    el = entry("table/table/0", table_ir(rows, TABLE_BOX, "p1tab0"), "b2s_s001_tab0")
    rb = jobj(el, "readback", "b2s_s001_tab0")
    rb["kind"] = "table"
    rb["table"] = [len(rows), len(rows[0])]
    rb["text"] = identity.plain_text(jobj(el, "ir"))
    jarr(base, "slides", 1, "elements").append(el)
    jarr(base, "slides", 1, "order").append("b2s_s001_tab0")
    return base


ROWS = [["Scenario", "Kept", "Time"], ["Disjoint", "100%", "3.9 s"], ["Conflicts", "92%", "6.0 s"]]


def test_table_cells_edited_on_both_sides_merge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"  # the source says another number
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("table/table/0", table_ir(changed, TABLE_BOX, "p1tab0"))
    edit_text(jat(theirs, "slides", 1), "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nConflicts\t92%\t6.2 s")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "table/table/0")
    assert u["action"] == "recreate" and set(jobj(u, "overrides")) == {"text"}
    assert jat(u, "overrides", "text", "table") and jat(u, "overrides", "text", "dims") == [3, 3]
    assert not jat(mplan, "report", "conflicts")
    # the deck's cell wins where it was edited, the source's where it was
    merged = merge.table_merge(jstr(u, "overrides", "text", "base"), identity.plain_text(table_ir(changed, TABLE_BOX, "p1tab0")),
                                         jstr(u, "overrides", "text", "theirs"), [3, 3], [3, 3])
    assert merged is not None
    cells, converged = merged
    assert [cells[1][1], cells[2][2]] == ["98%", "6.2 s"] and not converged


def test_the_same_cell_edited_on_both_sides_is_a_conflict():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[2][0] = "Clashes"  # the same word the deck renamed, to something else
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("table/table/0", table_ir(changed, TABLE_BOX, "p1tab0"))
    edit_text(jat(theirs, "slides", 1), "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nDisputes\t92%\t6.0 s")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    (c,) = jobjs(mplan, "report", "conflicts")
    assert c["field"] == "text" and c["resolution"] == "deck kept"


def test_a_row_added_in_the_deck_keeps_the_table():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("table/table/0", table_ir(changed, TABLE_BOX, "p1tab0"))
    obj = jobj(theirs, "slides", 1, "objects", "b2s_s001_tab0")
    obj["text"] = jstr(obj, "text") + "\nExtra\t0%\t0.0 s"
    obj["table"] = [4, 3]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    assert [c["field"] for c in jobjs(mplan, "report", "conflicts")] == ["text"]


def test_table_cells_the_deck_already_shows_converge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("table/table/0", table_ir(changed, TABLE_BOX, "p1tab0"))
    edit_text(jat(theirs, "slides", 1), "b2s_s001_tab0", identity.plain_text(table_ir(changed, TABLE_BOX, "p1tab0")))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "adopt"
    assert [c["field"] for c in jobjs(mplan, "report", "converged")] == ["text"]
    assert not merge.has_writes(mplan, slide_ids(theirs))


def test_table_cell_edit_requests_carry_the_cell():
    reqs = merge.text_edit_requests("tab", "6.0 s\n", "6.2 s\n", {"rowIndex": 2, "columnIndex": 2})
    assert all(jat(next(iter(r.values())), "cellLocation") == {"rowIndex": 2, "columnIndex": 2} for r in reqs)
    assert apply_text_requests("6.0 s\n", reqs) == "6.2 s\n"


def test_words_of_one_cell_merge_like_prose():
    b, o, t = "Conflicts\t92%\t6.0 s", "Conflicts\t92%\t6.0 seconds", "Conflicts\t94%\t6.0 s"
    merged = merge.table_merge(b, o, t, [1, 3], [1, 3])
    assert merged is not None
    cells, converged = merged
    assert cells == [["Conflicts", "94%", "6.0 seconds"]] and not converged


def test_a_cell_with_a_line_break_is_not_merged():
    assert merge.table_grid("a\tb\nc\td\te", [2, 2]) is None
    assert merge.table_grid("a\tb\nc\td", [2, 2]) == [["a", "b"], ["c", "d"]]


def test_changed_in_source_deleted_in_deck():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 2, "elements")[1] = ours_entry("text/body/0", text_ir("Other words", (20, 60, 200, 90), "p2t1", role="body"))
    del jobj(theirs, "slides", 2, "objects")["b2s_s002_t1"]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert jat(mplan, "report", "conflicts", 0, "resolution") == "kept deleted"


def test_removed_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    del jarr(ours, "slides", 2, "elements")[1]
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "delete"
    edit_text(jat(theirs, "slides", 2), "b2s_s002_t1", "Edited in the deck\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert jat(mplan, "report", "conflicts", 0, "field") == "removed"


def test_an_element_kept_because_the_deck_edited_it_stays_kept():
    """A decision has to be recorded, or it reverses itself the moment the evidence for it goes.
    The source drops an element, the person's own edit keeps it (a conflict), and the base then said
    only what it had always said - so the next sync, finding the deck no longer different from the
    base, deleted the box this one had promised to keep, and silently, `removed` being an applied
    change rather than a conflict. The evidence is the deck differing from the base and the sync can
    take it away itself: deleting another element the source dropped left the person's group around
    this one with a single child, which Slides dissolves (converted fuzz seed 7700464 at chain 10).
    So a kept unit says `removed` in the base, as a kept *slide* does, and only the person taking it
    out of the deck ends it."""
    base = three_slides()
    ours, theirs = triple(base)
    del jarr(ours, "slides", 2, "elements")[1]                  # the source no longer draws it
    jobj(theirs, "slides", 2, "objects", "b2s_s002_t1")["parent_group"] = "user_g"   # ... the person grouped it
    jobj(theirs, "slides", 2, "objects")["user_g"] = group_readback([0, 0, 400, 200])
    jobj(theirs, "slides", 2, "objects", "user_g")["children"] = ["b2s_s002_t1"]
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "end", "text/body/0")
    assert u["action"] == "keep" and u["removed"] is True
    assert jat(mplan, "report", "conflicts", 0, "field") == "removed"

    s = sync_work.bare_sync()                              # the base such a sync leaves behind
    s.base, s.ours, s.source, s.created, s.final_revision = base, ours, Path("new.pdf"), theirs, "r2"
    nb = s.new_base(sync_work.run_result(planned_work(mplan, {}), theirs))
    el = next(e for e in jobjs(nb, "slides", 2, "elements") if e["key"] == "text/body/0")
    assert el["removed"] is True

    # the person's group is gone (its other child was deleted), so the deck says the base's own
    # words again - and the element is still theirs, still kept, still in the report.
    jobj(theirs, "slides", 2, "objects").pop("user_g")
    jobj(theirs, "slides", 2, "objects", "b2s_s002_t1")["parent_group"] = None
    again = merge.plan_merge(nb, ours, theirs)
    assert unit(again, "end", "text/body/0")["action"] == "keep"
    assert [c["field"] for c in jobjs(again, "report", "conflicts")] == ["removed"]
    assert jat(again, "report", "conflicts", 0, "resolution") == "kept (the deck's own since the source dropped it)"
    assert not merge.has_writes(again, slide_ids(theirs))
    # ... until they take it out of the deck themselves, and then it is gone for good
    jobj(theirs, "slides", 2, "objects").pop("b2s_s002_t1")
    third = merge.plan_merge(nb, ours, theirs)
    assert unit(third, "end", "text/body/0")["action"] == "none"


def test_a_key_the_source_has_given_away_is_not_claimed_twice_in_the_base():
    """The other half of "a label belongs to the source", one dimension down. A unit kept though the
    source dropped it stays in the base under the key it had - and the next conversion hands that
    key to whatever it finds in its place, an icon at the head of *another* line being image/icon/0
    as readily as the one that went. The slide then answers to one key twice, and every
    `{e["key"]: e}` map over a slide's elements reads the second of them: `adopt_sync.problems`
    looked up the person's own unpaired icon as a member of the source's new unit, found it tied to
    nothing and refused the whole sync (adopt-shaped seed 86066 at chain 8). The kept one gives
    way - from here on it is bookkeeping for the deck's version, which the source will never name
    again."""
    base = three_slides()
    end = jat(base, "slides", 2)
    pic: JsonObject = {"id": "p2m0", "kind": "image", "role": "icon", "bbox": [16, 62, 24, 70], "file": None}
    jarr(end, "elements").append(anchored_entry("image/icon/0", pic, "b2s_s002_m0", "text/body/0"))
    ours, theirs = triple(base)
    jobj(theirs, "slides", 2, "objects", "b2s_s002_t1")["text"] = "the person typed this\n"   # ... so it is kept
    # The source drops that box and puts another one on the slide, whose icon inherits the key.
    other = text_ir("A line the source adds", (20, 120, 200, 140), "p2t9", role="body")
    jobj(ours, "slides", 2)["elements"] = [jat(ours, "slides", 2, "elements", 0), ours_entry("text/body/1", other),
                                     anchored_ours_entry("image/icon/0", {**pic, "id": "p2m9", "bbox": [16, 122, 24, 130]},
                                                         "text/body/1")]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["removed"] is True
    assert unit(mplan, "end", "text/body/1")["action"] == "create"

    s = sync_work.bare_sync()
    s.base, s.ours, s.source, s.created, s.final_revision = base, ours, Path("new.pdf"), theirs, "r2"
    work = planned_work(mplan, {2: "b2s_s002_m9", 1: "b2s_s002_t9"})
    written = jobjs(s.new_base(sync_work.run_result(work, theirs)), "slides", 2, "elements")
    keys = [jat(e, "key") for e in written]
    assert len(keys) == len(set(keys)), "the slide answers to each of its keys once"
    assert [jat(e, "key") for e in written if e.get("removed")] == ["text/body/0", "image/icon/0~2"]
    assert next(e for e in written if jat(e, "key") == "image/icon/0~2")["anchor"] == "text/body/0"
    fresh = next(e for e in written if jat(e, "key") == "image/icon/0")
    assert jat(fresh, "anchor") == "text/body/1" and not fresh.get("removed")
    # ... and each icon is a member of its own unit, which is what reading the base by key lost.
    units = merge.units(written)
    assert [m["key"] for m in units["text/body/0"]] == ["text/body/0", "image/icon/0~2"]
    assert [m["key"] for m in units["text/body/1"]] == ["text/body/1", "image/icon/0"]


def test_removed_text_living_on_in_a_conflict_is_kept():
    base = three_slides()
    extra = entry("text/body/1", text_ir("A closing remark", (20, 120, 200, 130), "p0t2", role="body"), "b2s_s000_t2")
    jarr(base, "slides", 0, "elements").append(extra)
    jarr(base, "slides", 0, "order").append("b2s_s000_t2")
    ours, theirs = triple(base)
    # the source joins the remark into the list and rewords the first point; the deck rewords it too
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
        "Main point of intro\nSecond point of intro\nA closing remark", (20, 60, 200, 130), "p0t1", role="body"))
    del jarr(ours, "slides", 0, "elements")[2]
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    # the paragraphs line up: the deck's rewording stays, the joined remark arrives in the list,
    # so the box it came from can go - its words live on in what is written
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate"
    merged, _, _ = merge.text_merge(jstr(u, "overrides", "text", "base"), merge.predicted_text(jobj(ours, "slides", 0, "elements", 1, "ir")),
                                    jstr(u, "overrides", "text", "theirs"), ())
    assert merged.split("\n")[:3] == ["Best point of intro", "Second point of intro", "A closing remark"]
    assert unit(mplan, "intro", "text/body/1")["action"] == "delete"
    # where the list stays the deck's whole (the person added a line where the remark goes), the
    # remark's own box is kept, or its words would be gone
    edit_text(jat(theirs, "slides", 0), "b2s_s000_t1", "Best point of intro\nSecond point of intro\nMy own closing\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/1")["action"] == "keep"
    assert not merge.has_writes(mplan, slide_ids(theirs))


def test_added_in_source_and_moved_only_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements").append(ours_entry("text/body/1", text_ir("New remark", (20, 120, 200, 130), "p1t2", role="body")))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/1")["action"] == "create"
    # moved in the source, text edited in the deck: the deck object moves
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1", role="body"))
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def anchored_picture_base():
    """`results` with an inline formula picture anchored to its body text."""
    base = three_slides()
    pic: JsonObject = {"id": "p1m0", "kind": "image", "role": "math", "bbox": [120, 62, 140, 74], "file": None}
    jarr(base, "slides", 1, "elements").append(anchored_entry("image/math/0", pic, "b2s_s001_m0", "text/body/0"))
    return base, pic


def test_a_unit_kept_for_a_conflict_still_moves_where_the_source_moved_it():
    """Live fuzz r8006 (table-moved): the source moved the results table and changed a cell, the
    person added a row; the table's words were the deck's to keep, and the unit kept where it stood,
    under the caption the source now puts where the table was. Where it stands is the source's alone."""
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point, as the source says\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1", role="body"))
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]
    assert [c["field"] for c in jobjs(mplan, "report", "conflicts") if c["element"] == "text/body/0"] == ["text"]


def test_member_moved_alone_is_recreated_not_moved():
    """Found by the offline fuzz (tools/fuzz_sync.py, seeds 252 and 430): the source re-placed the
    inline formula picture inside its line while the deck edited that paragraph. Sync writes a
    `move` by moving the unit's top object, so a step that fits only one member cannot be written -
    it used to become a move of [0, 0] the report still called applied (nothing moved, no conflict
    raised). Such a unit is recreated instead."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[2] = anchored_ours_entry("image/math/0", {**pic, "bbox": [128, 62, 148, 74]}, "text/body/0")
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "text/body/0")
    assert u["action"] == "recreate" and "delta" not in u
    assert not [a for a in jobjs(mplan, "report", "applied") if a.get("how") == "deck object moved"]


def test_whole_unit_moved_still_moves_the_deck_objects():
    """The counterpart: every member moved by the same step, so one move carries the unit."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1", role="body"))
    jarr(ours, "slides", 1, "elements")[2] = anchored_ours_entry("image/math/0", {**pic, "bbox": [120, 82, 140, 94]}, "text/body/0")
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def move_object(slide: Json, oid: str, dx: float, dy: float) -> None:
    rb = jobj(slide, "objects", oid)
    b, t = jnums(rb, "box"), jarr(rb, "transform")
    rb["box"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
    rb["transform"] = [*t[:4], jnum(t, 4) + dx, jnum(t, 5) + dy]


def test_picture_the_deck_moved_inside_its_unit_is_kept_not_recreated():
    """Found by the offline fuzz (tools/fuzz_sync.py, seed 720): the person dragged the formula
    picture inside its line while the source reworded that paragraph. Sync re-applies a geometry
    override by transforming the unit's *top* object, so a member that moved on its own would land
    back at the converter's box - silently, since the report listed the geometry as an override.
    The unit is kept as the deck has it, and the clash is reported."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1", role="body"))
    move_object(jat(theirs, "slides", 1), "b2s_s001_m0", 0, 40)
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    clash, = [c for c in jobjs(mplan, "report", "conflicts") if c["field"] == "geometry"]
    assert clash["element"] == "text/body/0" and "on its own" in jstr(clash, "resolution")
    # ... and the rewording that is not written either is named (edit hunt h3-2: a list's new item
    # never arrived while the report spoke only of geometry)
    assert jstr(clash, "resolution").endswith("the source's change to its text was not written either")


def test_unit_the_deck_moved_as_a_whole_is_still_recreated():
    """The counterpart: the person moved every object of the unit by the same step (they moved the
    group), so the top's transform carries the whole unit and the source's rewording goes in."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1", role="body"))
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        move_object(jat(theirs, "slides", 1), oid, 0, 40)
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "recreate" and jat(u, "overrides", "geometry") == {"mode": "delta"}


def geometry_override_requests(tops: dict[str, str]) -> list[JsonObject]:
    """What `Sync.override_requests` writes for the unit of `test_unit_the_deck_moved_as_a_whole...`,
    whose objects the deck moved 40 pt down and whose text the source reworded, once sync has
    recreated it as `new_t1` (+ `new_m0`) with `tops` saying what its outermost object is."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1", role="body"))
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        move_object(jat(theirs, "slides", 1), oid, 0, 40)
    mplan = merge.plan_merge(base, ours, theirs)
    p = next(p for p in jobjs(mplan, "slides") if jat(p, "key") == "results")
    assert unit(mplan, "results", "text/body/0")["overrides"] == {"geometry": {"mode": "delta"}}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    w = sync_work.slide_work(p)
    w.new_oid, w.tops = {1: "new_t1", 2: "new_m0"}, dict(tops)
    work = sync_work.work([w], ())
    now: JsonObject = {"slides": [{"objectId": "b2s_s001", "objects": {
        oid: readback([40, 120, 400, 180], words=None) for oid in ("new_t1", "new_m0", *tops.values())}}]}
    return sync.override_requests(work, theirs, now, {}, {})


def test_a_moved_unit_with_no_group_is_moved_member_by_member():
    """A geometry override is written as one RELATIVE transform on the unit's *top* object, which
    for a group carries its children. When the person has taken the group apart, a recreation does
    not put it back: the top is the main object alone, and the anchored picture needs the step
    itself or it stays at the converter's box while the report says the move was kept (live fuzz
    seed 903, chain depth 8: a formula picture 15 pt above the line it belongs to). `merge.
    geometry_writable` has already asked that one step fits every member, so it fits each of them."""
    from beamer2slides.sync import EMU_PER_PT
    moves = {jstr(r, "updatePageElementTransform", "objectId"): jobj(r, "updatePageElementTransform", "transform")
             for r in geometry_override_requests({})}
    assert sorted(moves) == ["new_m0", "new_t1"]
    assert all(t["translateY"] == round(40 * EMU_PER_PT) and t["translateX"] == 0 for t in moves.values())


def test_the_source_move_of_a_group_less_unit_reaches_its_picture_too():
    """The same rule on the other write path: a `move` (the source moved the unit, the deck edited
    its text) is written onto the deck's own objects, and when the person has taken the unit's group
    apart there is no one object that carries the rest."""
    from beamer2slides.sync import EMU_PER_PT, Sync
    base, pic = anchored_picture_base()
    members = merge.units(jobjs(base, "slides", 1, "elements"))["text/body/0"]
    units: list[JsonObject] = [{"key": "text/body/0", "action": "move", "delta": [0, 20]}]
    ungrouped = live(jobj(base, "slides", 1))
    grouped = copy.deepcopy(ungrouped)
    jobj(grouped, "objects")["b2s_s001_t1_g"] = readback([40, 120, 400, 180], words=None)
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        jobj(grouped, "objects", oid)["parent_group"] = "b2s_s001_t1_g"
    members[0] = {**members[0], "objects": [*jarr(members[0], "objects"), "b2s_s001_t1_g"]}
    bunits = {"text/body/0": members}
    assert [jat(r, "updatePageElementTransform", "objectId") for r in Sync.move_requests(units, bunits, grouped, 2.0)] \
        == ["b2s_s001_t1_g"]
    reqs = Sync.move_requests(units, bunits, ungrouped, 2.0)
    assert [jat(r, "updatePageElementTransform", "objectId") for r in reqs] == ["b2s_s001_t1", "b2s_s001_m0"]
    assert all(jat(r, "updatePageElementTransform", "transform", "translateY") == round(40 * EMU_PER_PT) for r in reqs)


def test_a_moved_unit_that_still_has_its_group_is_moved_once():
    """The counterpart, and why one request was ever enough: the group carries the picture, and a
    second step on the picture would move it twice."""
    reqs = geometry_override_requests({"text/body/0": "new_t1_g"})
    assert [jat(r, "updatePageElementTransform", "objectId") for r in reqs] == ["new_t1_g"]


def test_a_kept_table_the_source_now_runs_into_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edit hunt h2-2, h4-1: the person gave a table a row, the table was kept whole for the conflict,
    and the source moved the figure below it up into it. Sync moves neither; it says so."""
    from beamer2slides import snapshot
    table: JsonObject = {"kind": "table", "box": [40, 100, 400, 220], "title": "b2s:s/table/table/0"}

    def fig(box: list[Json]) -> JsonObject:
        return {"kind": "image", "box": box, "title": "b2s:s/image/figure/0"}

    def read_presentation(raw: object) -> JsonObject:
        return {"slides": [after]}

    before: JsonObject = {"objectId": "S", "objects": {"t1": table, "f1": fig([100, 240, 300, 330])}}
    after: JsonObject = {"objectId": "S", "objects": {"t1": table, "f2": fig([100, 180, 300, 270])}}
    base: JsonObject = {"slides": [{"key": "s", "elements": [{"key": "table/table/0", "objects": ["t1"]},
                                                             {"key": "image/figure/0", "objects": ["f1"]}]}]}
    plan: JsonObject = {"action": "update", "key": "s", "base": 0, "objectId": "S",
                        "units": [{"key": "table/table/0", "action": "keep", "deck": ["text"]},
                                  {"key": "image/figure/0", "action": "recreate", "deck": []}]}
    sync = sync_work.bare_sync()
    sync.base = base
    monkeypatch.setattr(sync, "read", lambda: None)
    monkeypatch.setattr(snapshot, "read_presentation", read_presentation)
    sync.warn_about_overruns(sync_work.work([sync_work.slide_work(plan)], ()), {"slides": [before]})
    assert [(jat(o, "object"), jat(o, "other")) for o in sync.overruns] == [("t1", "f2")]
    assert "kept as the deck has it for a conflict" in sync.warnings[0]
    # the same table nobody edited is the converter's own layout, not something to warn about
    jarr(plan, "units")[0] = {"key": "table/table/0", "action": "recreate", "deck": []}
    sync.overruns = []
    sync.warnings = []
    sync.warn_about_overruns(sync_work.work([sync_work.slide_work(plan)], ()), {"slides": [before]})
    assert sync.overruns == []


def test_a_created_shape_stays_under_the_text_the_source_draws_above_it():
    """`Sync.restack` gives a recreated element the deck's place in the z-order (a restack the person
    made survives) and a created one the place the source gives it - right after the element before
    it. Where those two orders disagree - a frame the source rewrote from scratch, or a label that
    moved onto another slide - that put a newly created opaque panel on top of text the source draws
    above it. Nothing is deleted when that happens, so every other check passes; the offline campaign
    saw it as `loss_oracle.text_hidden` on 5 of 1000 adopt-shaped rounds. A created element now also
    stays below the first element the source draws above it."""
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = jobj(ours, "slides", 0)
    panel = ours_entry("shape/panel/0", shape_ir((15, 55, 210, 95), "p0s0", fill="#dddddd"))
    # the source draws body, then the panel, then the title: the deck has the title at the bottom
    o["elements"] = [jat(o, "elements", 1), panel, jat(o, "elements", 0)]
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
                     "units": [{"key": "text/body/0", "action": "recreate"}, {"key": "shape/panel/0", "action": "create"},
                               {"key": "text/title/0", "action": "recreate"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    before = live(jobj(base, "slides", 0))
    doomed = set(jstrs(before, "order"))
    w: dict[str, object] = {"plan": p, "doomed": doomed,
                            "tops": {"text/body/0": "new_t1", "shape/panel/0": "new_s0", "text/title/0": "new_t0"}}
    now: JsonObject = {"order": [*jarr(before, "order"), "new_t1", "new_s0", "new_t0"]}
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):     # BRING_TO_FRONT, bottom to top
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("new_t0")


def test_a_created_panel_stays_under_words_only_the_deck_has():
    """The other half of that rule. The source's order says where a new element goes among the
    source's own; about an element the source dropped and the deck's edits kept alive it says
    nothing at all - so the guard above, which only consults source elements, let a created opaque
    panel land on top of a kept text. Nothing is deleted, so only `loss_oracle.text_hidden` saw it
    (converted seed 78036, chain 4)."""
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = jobj(ours, "slides", 0)
    panel = ours_entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0", fill="#dddddd"))
    o["elements"] = [jat(o, "elements", 0), panel]   # the source dropped the body text; the deck kept it
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
                     "units": [{"key": "text/title/0", "action": "recreate"}, {"key": "shape/panel/0", "action": "create"},
                               {"key": "text/body/0", "action": "keep"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    before = live(jobj(base, "slides", 0))
    before["order"] = ["b2s_s000_t1", "b2s_s000_t0"]     # the kept body is at the bottom
    doomed = {"b2s_s000_t0"}
    w: dict[str, object] = {"plan": p, "doomed": doomed, "tops": {"text/title/0": "new_t0", "shape/panel/0": "new_s0"}}
    now: JsonObject = {"order": ["b2s_s000_t1", "b2s_s000_t0", "new_t0", "new_s0"],
                       "objects": {**jobj(before, "objects"),
                                   "new_t0": readback([20, 20, 200, 48], "Intro"),
                                   "new_s0": readback([30, 160, 420, 260], words=None)}}   # opaque, over the kept body's box
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("b2s_s000_t1")


def test_a_created_panel_goes_under_a_persons_box_grouped_with_one_of_the_converters():
    """Which words are the deck's own is a question about each *text*, not about the page element
    holding it. A person who groups one of their own text boxes with one of the converter's leaves a
    page element that is partly the source's, and reading the element as a whole made it the
    source's: the guard let a created panel land on the box inside it (converted fuzz seed 5200496
    at chain 12, found when the page-element rule above first reached this loop)."""
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = jobj(ours, "slides", 0)
    o["elements"] = [*jarr(o, "elements"), ours_entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0", fill="#dddddd"))]
    slide = jobj(base, "slides", 0)
    slide["order"] = ["user_g", "b2s_s000_t1"]          # the person's group is at the bottom
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
                     "units": [{"key": "text/title/0", "action": "keep"}, {"key": "text/body/0", "action": "keep"},
                               {"key": "shape/panel/0", "action": "create"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    objects: JsonObject = {
        "user_g": {**group_readback([20, 20, 300, 120]),
                   "children": ["b2s_s000_t0", "user_box"]},
        "b2s_s000_t0": {**readback([20, 20, 200, 48], "Intro"), "parent_group": "user_g"},
        "user_box": {**readback([20, 60, 300, 120], "a note nobody may lose"), "parent_group": "user_g"},
        "b2s_s000_t1": readback([40, 120, 400, 180], "First point of intro")}
    before: JsonObject = {"objectId": "b2s_s000", "order": ["user_g", "b2s_s000_t1"], "objects": objects}
    no_doom: set[str] = set()
    w: dict[str, object] = {"plan": p, "doomed": no_doom, "tops": {"shape/panel/0": "new_s0"}}
    now: JsonObject = {"order": ["user_g", "b2s_s000_t1", "new_s0"],
                       "objects": {**objects, "new_s0": readback([10, 10, 400, 190], words=None)}}   # over the lot
    order = jstrs(now, "order")
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("user_g")
    # ... but a group holding nothing but the converter's own is the source's, and the source's
    # order stands: the panel it draws last stays last.
    jobj(objects, "user_g")["children"] = ["b2s_s000_t0"]
    del objects["user_box"]
    now["objects"] = {**objects, "new_s0": jat(now, "objects", "new_s0")}
    order = jstrs(now, "order")
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") > order.index("user_g")


def test_a_created_panel_goes_under_the_text_a_group_carries_above_it():
    """A page element stands for *every* converter element inside it, and `Sync._by_the_source` ranks
    it by the first of them - so a group holding two of the converter's texts with the panel drawn
    between them is a place where the source's order cannot be honoured at all: the group goes below
    the panel for the sake of the text the source draws below it, and takes the text the source draws
    above it down with it. Those words are the source's own, so the guard above had been told to keep
    quiet about them, and a created panel covered a text still on the slide (converted fuzz seed
    8300231 at chain 11). A text the source draws above the shape is spoken for only while its page
    element is not standing below the shape on another text's account."""
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("text/body/1", text_ir("Above the panel", (20, 40, 200, 60), "p0t1", role="body"), "b2s_s000_t1"),
           entry("shape/panel/0", shape_ir((15, 70, 210, 120), "p0s0", fill="#dddddd"), "b2s_s000_s0"),
           entry("text/body/2", text_ir("Under the panel", (20, 130, 200, 150), "p0t2", role="body"), "b2s_s000_t2")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["order"] = ["b2s_s000_t0", "b2s_s000_s0", "user_g"]   # the person grouped the two bodies
    base: JsonObject = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    before: JsonObject = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "b2s_s000_s0", "user_g"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], "Intro"),
        "b2s_s000_s0": readback([30, 140, 420, 200], words=None),
        "user_g": {**group_readback([40, 80, 400, 300]),
                   "children": ["b2s_s000_t1", "b2s_s000_t2"]},
        "b2s_s000_t1": {**readback([40, 80, 400, 120], "Above the panel"), "parent_group": "user_g"},
        "b2s_s000_t2": {**readback([40, 240, 400, 300], "Under the panel"), "parent_group": "user_g"}}}
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "text/body/1", "action": "keep"},
                   {"key": "shape/panel/0", "action": "recreate"}, {"key": "text/body/2", "action": "keep"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    doomed: set[str] = {"b2s_s000_s0"}
    w: dict[str, object] = {"plan": p, "doomed": doomed, "tops": {"shape/panel/0": "new_s0"}}
    # the source grew the panel: it now covers the body the group carries *above* it
    now: JsonObject = {"order": ["b2s_s000_t0", "b2s_s000_s0", "user_g", "new_s0"],
           "objects": {**{k: v for k, v in jobj(before, "objects").items() if k != "b2s_s000_s0"},
                       "new_s0": readback([30, 200, 420, 320], words=None)}}
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("user_g")
    # ... and where the group carries nothing the source draws above the panel, the source's order
    # stands: the panel it draws last stays last, over the words it is meant to sit on.
    jobj(now, "objects")["new_s0"] = readback([30, 90, 420, 130], words=None)      # over the body drawn *under* it
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") > order.index("user_g")


def test_a_panel_in_a_block_takes_the_whole_block_under_words_only_the_deck_has():
    """The same rule, asked of the page element rather than the object. A block's panel is no page
    element of its own - what the page order holds is the group - so the guard above looked up an id
    that is in no order at all, found nothing, and a panel the source had just grown covered a text
    the source no longer has (converted fuzz seed 2300025 at chain 12, adopt-shaped 3200538 at chain
    10). What goes under the text is the block, which is this converter's own container and carries
    only what it drew; a group the *person* made is left where it is (`sync.folded_hiders` names
    that one instead)."""
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("text/body/1", text_ir("Only the deck has this", (20, 80, 200, 100), "p0t1", role="body"), "b2s_s000_t1"),
           entry("shape/panel/0", shape_ir((15, 120, 210, 170), "p0s0", fill="#dddddd"), "b2s_s000_s0"),
           entry("text/body/2", text_ir("In the block", (20, 130, 200, 150), "p0t2", role="body"), "b2s_s000_t2")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["groups"] = ["blk"]
    slide["order"] = ["b2s_s000_t0", "b2s_s000_t1", "blk"]
    base: JsonObject = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    o = jobj(ours, "slides", 0)
    o["elements"] = [jat(o, "elements", 0), jat(o, "elements", 2), jat(o, "elements", 3)]   # the source dropped body/1
    before: JsonObject = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "b2s_s000_t1", "blk"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], "Intro"),
        "b2s_s000_t1": readback([40, 160, 400, 200], "Only the deck has this"),
        "blk": {**group_readback([30, 240, 440, 340]),
                "children": ["b2s_s000_s0", "b2s_s000_t2"]},
        "b2s_s000_s0": {**readback([30, 240, 420, 340], words=None), "parent_group": "blk"},
        "b2s_s000_t2": {**readback([40, 260, 400, 300], "In the block"), "parent_group": "blk"}}}
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "shape/panel/0", "action": "recreate"},
                   {"key": "text/body/2", "action": "keep"}, {"key": "text/body/1", "action": "keep"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    doomed: set[str] = {"b2s_s000_s0"}
    w: dict[str, object] = {"plan": p, "doomed": doomed, "tops": {"shape/panel/0": "new_s0"}}
    # the source grew the panel: it now covers the text the deck alone has, two page elements below
    now: JsonObject = {"order": ["b2s_s000_t0", "b2s_s000_t1", "blk"],
           "objects": {**{k: v for k, v in jobj(before, "objects").items() if k != "b2s_s000_s0"},
                       "blk": {**jobj(before, "objects", "blk"), "children": ["new_s0", "b2s_s000_t2"]},
                       "new_s0": {**readback([30, 150, 420, 340], words=None), "parent_group": "blk"}}}
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("blk") < order.index("b2s_s000_t1")


def test_an_element_a_dissolved_group_frees_onto_the_page_takes_the_sources_place():
    """`restack` rewrites the deck's page order slot by slot, so an object with no slot used to land
    on top of everything - past the ceiling that keeps a new panel under the text the source draws
    above it, and past the guard that keeps it under words only the deck has. A created element was
    given its place for that reason; an element a group this rewrite dissolves *frees onto the page*
    has none either, its old top having stood inside that group, and Slides ungroups before
    rebuilding while a group left with fewer than two members is not rebuilt at all. The offline
    campaign cannot reach it: its applier models the outcome and hands the freed child the group's
    own slot (`fuzz_world._drop_lonely_groups`)."""
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0", fill="#dddddd"), "b2s_s000_s0"),
           entry("text/body/0", text_ir("First point\nSecond point", (20, 100, 200, 120), "p0t1", role="body"), "b2s_s000_t1")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["groups"] = ["blk"]
    slide["order"] = ["b2s_s000_t0", "blk"]   # the panel and the body are a block
    base: JsonObject = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    before: JsonObject = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "blk"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], "Intro"),
        "blk": {**group_readback([30, 160, 440, 280]),
                "children": ["b2s_s000_s0", "b2s_s000_t1"]},
        "b2s_s000_s0": {**readback([30, 160, 420, 260], words=None), "parent_group": "blk"},
        "b2s_s000_t1": {**readback([40, 200, 400, 240], "First point"), "parent_group": "blk"}}}
    p: JsonObject = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "shape/panel/0", "action": "recreate"},
                   {"key": "text/body/0", "action": "keep"}]}
    sync = sync_work.bare_sync()
    sync.base, sync.ours = base, ours
    doomed: set[str] = {"b2s_s000_s0"}
    w: dict[str, object] = {"plan": p, "doomed": doomed, "tops": {"shape/panel/0": "new_s0"}}
    # the group is gone: Slides ungrouped it and one member was left, so both stand on the page
    now: JsonObject = {"order": ["b2s_s000_t0", "b2s_s000_s0", "b2s_s000_t1", "new_s0"],
           "objects": {"b2s_s000_t0": jobj(before, "objects", "b2s_s000_t0"),
                       "b2s_s000_t1": {**jobj(before, "objects", "b2s_s000_t1"), "parent_group": None},
                       "new_s0": readback([30, 160, 420, 270], words=None)}}     # opaque, over the body's box
    order = [x for x in jstrs(now, "order") if x not in doomed]
    for r in sync.restack(w, before, now):
        oid, = jstrs(r, "updatePageElementsZOrder", "pageElementObjectIds")
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("b2s_s000_t1")


def test_the_elements_a_rewrite_replaces_take_the_sources_order():
    """The same sentence at page level. Two recreated elements kept the deck's order, which between
    two converter elements is not an edit anybody made - it is what the last conversion drew - so a
    source that now draws a panel *under* a text (a label that moved brings another frame's elements
    onto this slide) got it back on top (converted seed 610106, chain 10). Where the deck's order is
    no longer the base's, somebody restacked and that survives: `Sync._by_the_source` asks the whole
    set at once, a z-order edit being the one deck edit the merge cannot see."""
    from beamer2slides.sync import Sync
    keys = ["text/title/0", "shape/panel/0", "text/body/0"]   # the source draws the panel under the body
    tops = {"text/title/0": "new_t0", "shape/panel/0": "new_s0", "text/body/0": "new_t1"}
    oldtop = {"text/title/0": "old_t0", "text/body/0": "old_t1", "shape/panel/0": "old_s0"}
    base_order = ["old_t0", "old_t1", "old_s0"]               # the last conversion drew the panel on top
    desired = ["new_t0", "user_pic", "new_t1", "new_s0"]
    Sync._by_the_source(desired, oldtop, tops, keys, base_order, None, None)
    assert desired == ["new_t0", "user_pic", "new_s0", "new_t1"]
    # ... but a deck whose order is no longer the base's is one somebody restacked, and that stands
    theirs = ["new_s0", "user_pic", "new_t0", "new_t1"]
    Sync._by_the_source(theirs, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"], None, None)
    assert theirs == ["new_s0", "user_pic", "new_t0", "new_t1"]


def test_the_source_orders_a_rewrite_against_the_elements_it_keeps():
    """The elements this sync keeps count as much as the ones it rewrites - the same reason
    `Sync.zrank` ranks a group's kept children. Ordering only the rewritten ones among themselves
    means a slide with one of them has nothing to be ordered against and the rule never fires: a
    panel the source draws under a text it did not touch grew over it and stayed on top (converted
    seed 1500512 at chain 10, on a slide the person had ungrouped)."""
    from beamer2slides.sync import Sync
    keys = ["text/title/0", "shape/panel/0", "text/body/0"]   # the source draws the panel under the body
    tops = {"shape/panel/0": "new_s0"}                        # ... and only the panel was rewritten
    oldtop = {"text/title/0": "old_t0", "shape/panel/0": "old_s0", "text/body/0": "old_t1"}
    desired = ["old_t0", "old_t1", "new_s0"]
    Sync._by_the_source(desired, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"], None, None)
    assert desired == ["old_t0", "new_s0", "old_t1"]
    # ... and a deck whose order is no longer the base's is one somebody restacked, and that stands
    theirs = ["old_t1", "old_t0", "new_s0"]
    Sync._by_the_source(theirs, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"], None, None)
    assert theirs == ["old_t1", "old_t0", "new_s0"]


def test_a_converter_group_takes_the_source_order_of_what_it_carries():
    """What the page rule orders are the page *elements*, and a block group is the page element of
    everything in it. Asking it of the objects alone left out everything inside a group, so a
    block's panel could not be ordered against a table beside it however the source drew them: the
    recreated panel stayed in its group above a table the source draws over it (converted seed
    1300381 at chain 6). The group stands for the first element the source draws in there."""
    from beamer2slides.sync import Sync
    keys = ["shape/panel/0", "text/body/0", "table/table/0"]   # the table is drawn over the block
    tops = {"shape/panel/0": "new_s0"}
    oldtop = {"shape/panel/0": "old_s0", "text/body/0": "old_t0", "table/table/0": "tbl"}
    stands_now = {"new_s0": "blk", "old_t0": "blk", "tbl": "tbl", "blk": "blk"}
    stands_before = {"old_s0": "blk", "old_t0": "blk", "tbl": "tbl", "blk": "blk"}
    desired = ["tbl", "blk"]
    Sync._by_the_source(desired, oldtop, tops, keys, ["tbl", "blk"], stands_now, stands_before)
    assert desired == ["blk", "tbl"]
    # ... and a group the person made is not one the base draws, so it stands for nobody: nothing
    # names it, and the one element left has nothing to be ordered against
    folded = ["tbl", "user_g"]
    Sync._by_the_source(folded, oldtop, tops, keys, ["tbl", "blk"],
                        {"new_s0": "user_g", "old_t0": "user_g", "tbl": "tbl"},
                        {"old_s0": "user_g", "old_t0": "user_g", "tbl": "tbl"})
    assert folded == ["tbl", "user_g"]


def test_the_children_of_a_rebuilt_group_take_the_sources_order():
    """A group a rewrite takes apart is made again under the same id, and its children used to go
    back in the order the deck had them. Inside a group that order is nobody's edit - Slides will
    not let a person restack there - so it carries only what the last conversion drew, and a panel
    the source has since put *under* a text came back on top of it (converted seed 79045). The
    children that are elements of the source take the source's order among themselves, whether this
    sync rewrote them or kept them (seeds 670146, 660326, 680477, where the text underneath was one
    the sync kept); anything else in the group holds its slot."""
    from beamer2slides.sync import Regroup, Sync
    from beamer2slides.sync_model import ObjectId
    g = ObjectId("g")
    objects: JsonObject = {"g": {"kind": "elementGroup", "children": ["old_t", "user_pic", "old_s", "kept_t"]}}
    regroup = {g: Regroup(remove={"old_t", "old_s"}, unit_of={"old_t": "text/body/0", "old_s": "shape/panel/0"})}
    tops = {"text/body/0": "new_t", "shape/panel/0": "new_s"}
    # the source draws the panel under both texts; the deck had it on top of them
    rank = {"new_t": 2, "new_s": 1, "kept_t": 3}
    no_ids: set[str] = set()
    reqs = Sync.regroup_requests(regroup, {g: 0}, objects, tops, no_ids, rank)
    group, = [r["groupObjects"] for r in reqs if "groupObjects" in r]
    assert jat(group, "childrenObjectIds") == ["new_s", "user_pic", "new_t", "kept_t"]
    # ... and with no source order to go by, the deck's own order stands
    plain, = [r["groupObjects"] for r in Sync.regroup_requests(regroup, {g: 0}, objects, tops, no_ids, {})
              if "groupObjects" in r]
    assert jat(plain, "childrenObjectIds") == ["new_t", "user_pic", "new_s", "kept_t"]


def test_the_rank_a_rebuilt_group_is_ordered_by_covers_kept_objects_too():
    """`Sync.zrank` is where that order comes from: the objects this sync writes, and the deck's own
    objects, which stand for the elements it is keeping. Leaving the kept ones out was why the first
    version of the rule reached only a group whose children the sync rewrote outright."""
    from beamer2slides.sync import Sync
    base = many_slides(["intro"])
    o = ours_of(jobj(base, "slides", 0))
    bunits = merge.units(jobjs(base, "slides", 0, "elements"))
    rank = Sync.zrank(o, bunits, {"text/title/0": "new_t0"})
    assert rank == {"b2s_s000_t0": 0, "b2s_s000_t1": 1, "new_t0": 0}


def test_words_a_grouping_the_person_made_keeps_covered_are_named_in_the_report():
    """What the three ordering rules above cannot reach. A converter element the person folded into
    a group of their own has that group for its page element, so `restack` cannot put it under a
    text in another one without moving everything else they grouped with it, and `regroup_requests`
    reorders only the children of groups this sync rebuilds. A panel that grew when the source
    redrew it then covers words with no way round it - nothing is deleted, every write went through,
    and no other line of the report would mention it (converted seed 670146 at chain 6, 2 of 2,600
    rounds). `folded_hiders` finds exactly that pair, and only that pair."""
    from beamer2slides import sync as S
    opaque = readback([50, 272, 370, 352], words=None)                      # the source redrew it taller
    read: JsonObject = {"order": ["blk", "ug"], "objects": {
        "blk": {"kind": "elementGroup", "box": [80, 328, 430, 384], "children": ["new_t"]},
        "new_t": readback([90, 344, 420, 368], "bullet editor picture table\n"),
        "ug": {"kind": "elementGroup", "box": [40, 40, 400, 352], "children": ["mine", "new_s"]},
        "mine": readback([40, 40, 256, 68], "the person's own heading\n"),
        "new_s": opaque}}
    made, ours = {"new_t", "new_s"}, {"blk", "old_t", "old_s"}
    assert S.folded_hiders(read, made, ours, set()) == [("new_t", "new_s")]
    # ... and nothing where the shape stands on the page itself: that one `restack` orders away
    page: JsonObject = {"order": ["blk", "new_s"], "objects": {k: v for k, v in jobj(read, "objects").items()
                                                   if k not in ("ug", "mine")}}
    assert S.folded_hiders(page, made, ours, set()) == []
    # ... nor where the person's group is where the text is too: no page order decides that
    together: JsonObject = {"order": ["ug"], "objects": {**jobj(read, "objects"),
                                             "ug": {**jobj(read, "objects", "ug"), "children": ["new_t", "new_s"]}}}
    assert S.folded_hiders(together, made, ours, set()) == []
    # ... nor words the cleanup is about to delete: the order is read before it, and the old block's
    # words still stand under the new one (live scenario nested-group warned about v1's block title)
    assert S.folded_hiders(read, made, ours, {"new_t"}) == []
    # and the report says it, in the person's words: the sync's own `ours`/`made` come from the base
    sync = sync_work.bare_sync()
    sync.base = {"slides": [{"key": "intro", "groups": ["blk"],
                             "elements": [{"key": "text/body/0", "objects": ["old_t"]},
                                          {"key": "shape/panel/0", "objects": ["old_s"]}]}]}
    w = sync_work.slide_work({"action": "update", "base": 0, "objectId": "s1"})
    w.objects = {0: ["new_t"], 1: ["new_s"]}     # element index -> the objects made (text/body/0, shape/panel/0)
    sync.warn_about_folded_hiders(sync_work.work([w], ()), {"slides": [{**read, "objectId": "s1"}]})
    assert len(sync.warnings) == 1 and "bullet editor picture table" in sync.warnings[0]
    assert "group you made" in sync.warnings[0] and "Ungroup it" in sync.warnings[0]


def test_image_replaced_in_deck_is_kept():
    base = three_slides()
    pic: JsonObject = {"id": "p1f0", "kind": "image", "role": "figure", "bbox": [200, 60, 300, 160], "file": None}
    el = entry("image/figure/0", pic, "b2s_s001_f2")
    jobj(el, "readback")["b2s_s001_f2"] = image_readback([400, 120, 600, 320], "aaa")
    jarr(base, "slides", 1, "elements").append(el)
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("image/figure/0", {**pic, "bbox": [200, 60, 320, 160]})
    jobj(theirs, "slides", 1, "objects", "b2s_s001_f2")["image"] = {"contentHash": "bbb"}
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert jat(mplan, "report", "conflicts", 0, "field") == "image"


# ---------------------------------------------------------------- slides

def test_slide_added_deleted_and_reordered():
    base = three_slides()
    ours, theirs = triple(base)
    live_ids = slide_ids(theirs)
    # a new frame after intro
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"), oid=None)], "method", title=""))
    ours2: JsonObject = {"slides": [jat(ours, "slides", 0), new, jat(ours, "slides", 1), jat(ours, "slides", 2)], "pairs": {"0": 0, "2": 1, "3": 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert jat(mplan, "order") == ["b2s_s000", "new:method", "b2s_s001", "b2s_s002"]
    assert jat(mplan, "report", "slides", "created") == ["method"]
    # results removed from the source, untouched in the deck: deleted
    ours3: JsonObject = {"slides": [jat(ours, "slides", 0), jat(ours, "slides", 2)], "pairs": {"0": 0, "1": 2}}
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [jat(p, "action") for p in jobjs(mplan, "slides") if jat(p, "key") == "results"] == ["delete"]
    assert jat(mplan, "order") == ["b2s_s000", "b2s_s002"]
    # ... but kept after its predecessor when the deck added something to it
    jobj(theirs, "slides", 1, "objects")["user_box"] = readback([0, 0, 50, 20], "note")
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [jat(p, "action") for p in jobjs(mplan, "slides") if jat(p, "key") == "results"] == ["keep_removed"]
    assert jat(mplan, "order") == live_ids
    # the source swaps results and end
    ours, theirs = triple(base)
    ours4: JsonObject = {"slides": [jat(ours, "slides", 0), jat(ours, "slides", 2), jat(ours, "slides", 1)], "pairs": {"0": 0, "1": 2, "2": 1}}
    mplan = merge.plan_merge(base, ours4, theirs)
    assert jat(mplan, "order") == ["b2s_s000", "b2s_s002", "b2s_s001"]
    assert merge.has_writes(mplan, live_ids)
    # ... the deck's own move of intro wins over the swap it disagrees with
    theirs["slides"] = [jat(theirs, "slides", 1), jat(theirs, "slides", 0), jat(theirs, "slides", 2)]
    mplan = merge.plan_merge(base, ours4, theirs)
    assert jat(mplan, "order") == ["b2s_s001", "b2s_s000", "b2s_s002"]


def test_a_slide_the_deck_moved_does_not_freeze_the_rest_of_the_order():
    """One slide dragged in Slides is no instruction to leave the other four where they are: the
    source's order is applied around it, and the dragged slide keeps the place the deck gave it."""
    base = many_slides(["a", "b", "c", "d", "e"])
    ours, theirs = triple(base)
    ids = slide_ids(base)
    theirs["slides"] = [jat(theirs, "slides", i) for i in (0, 4, 1, 2, 3)]       # the deck pulls e up after a
    ours2: JsonObject = {"slides": [jat(ours, "slides", i) for i in (0, 1, 3, 2, 4)],        # the source swaps c and d
             "pairs": {"0": 0, "1": 1, "2": 3, "3": 2, "4": 4}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert jat(mplan, "order") == [ids[0], ids[4], ids[1], ids[3], ids[2]]
    assert not [w for w in jstrs(mplan, "report", "warnings") if "moved" in w]
    # ... and when both moved the same slide, the deck's place wins and the report says so
    ours3: JsonObject = {"slides": [jat(ours, "slides", i) for i in (0, 1, 2, 4, 3)], "pairs": {"0": 0, "1": 1, "2": 2, "3": 4, "4": 3}}
    mplan = merge.plan_merge(base, ours3, theirs)
    assert jat(mplan, "order") == [ids[0], ids[4], ids[1], ids[2], ids[3]]
    assert [w for w in jstrs(mplan, "report", "warnings") if "both the source and the deck moved" in w]


def bolded(slide: Json, oid: str, word: str) -> None:
    """The person bolds one word of an object, the way `tools/deck_edits.py bold` does live."""
    rb = jobj(slide, "objects", oid)
    words = jstr(rb, "text")
    at = words.index(word)
    plain = jobj(rb, "text_styles", 0)
    rb["text_styles"] = [plain, {**plain, "bold": True}]
    rb["run_spans"] = [[0, at, plain], [at, at + len(word), {**plain, "bold": True}],
                       [at + len(word), len(words.rstrip("\n")), plain]]
    rb["text_style_hash"] = "bolded"


def test_styling_on_words_the_source_replaced_is_a_conflict_not_a_promise():
    """Live round 404, chained: the person bolded a word of a title and the source then rewrote that
    title. `sync.style_range_requests` puts the deck's run styles back onto the same words, and the
    words are gone - so the bold ends there, which nothing can help. What the report may not do is
    promise the styling was kept, and it used to: an `overrides` entry and not a word more."""
    base = many_slides(["intro"])
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
        "A wholly different sentence about something else\nSecond point of intro", (20, 60, 200, 90), "p0t1", role="body"))
    bolded(jat(theirs, "slides", 0), "b2s_s000_t1", "First")
    report = jobj(merge.plan_merge(base, ours, theirs), "report")
    (c,) = [c for c in jobjs(report, "conflicts") if c["field"] == "text_style"]
    assert c["resolution"] == "the styling of the replaced words is gone"
    # The word survives the rewrite: the styling lands on it again, and there is nothing to report.
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
        "First point of intro, reworded\nSecond point of intro", (20, 60, 200, 90), "p0t1", role="body"))
    quiet = jobj(merge.plan_merge(base, ours, theirs), "report")
    assert not [c for c in jobjs(quiet, "conflicts") if c["field"] == "text_style"]
    assert [o for o in jobjs(quiet, "overrides") if "text_style" in jarr(o, "fields")]


def test_a_word_that_stands_twice_in_the_box_does_not_save_the_styling_on_the_other_one():
    """Offline seed 86044 --shape adopt --first-sync, chained 5 deep: the box says `typed by a
    person` twice, the source replaced the sentence around the first one, and the person had bolded
    a word of that sentence.

    `sync.style_range_requests` maps each styled run through the matching blocks of the live text
    and the text it is about to write, and those anchor on the longest thing the two share - so the
    surviving `typed by a person` is paired with the *second* one and the bolded word has nowhere
    to go. Asking the same question of an alignment of the tokens answered with the first one, the
    report promised an override, and the person's bold was gone with nothing said about it."""
    els = [entry("text/body/0", text_ir("deck wording typed by a person\n"
                                        "and the source adds merge typed by a person",
                                        (20, 60, 200, 90), "p0t1", role="body"), "b2s_s000_t1")]
    base: JsonObject = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None,
            "slides": [base_slide("intro", "b2s_s000", els, label="intro", title="Intro")]}
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[0] = ours_entry("text/body/0", text_ir(
        "and the source adds merge\nand the source adds slides typed by a person",
        (20, 60, 200, 90), "p0t1", role="body"))
    bolded(jat(theirs, "slides", 0), "b2s_s000_t1", "a person")   # the first one: the source replaced it
    report = jobj(merge.plan_merge(base, ours, theirs), "report")
    (c,) = [c for c in jobjs(report, "conflicts") if c["field"] == "text_style"]
    assert c["resolution"] == "the styling of the replaced words is gone"


def test_a_bullet_the_deck_deleted_does_not_freeze_the_box_against_the_source():
    """A read-back keeps the *distinct* paragraph styles of a text, and the converter gives every
    paragraph the line spacing of its own PDF pitch - so deleting one bullet takes an entry out of
    that list. `uniform_changes` read the shorter list as a change it could not re-apply (None), the
    unit became a `text_style` conflict blaming a restyle nobody made, and the box was kept exactly
    as it stood: every later source change to it was dropped, for good. Found by the live scenario
    `last-paragraph`, where a reworded bullet never arrived."""
    three = "First point of intro\nSecond point of intro\nThird point of intro"
    styles: list[Json] = [{"alignment": "START", "lineSpacing": 116.3}, {"alignment": "START", "lineSpacing": 114.3},
              {"alignment": "START", "lineSpacing": 100.0}]   # one per paragraph, as the converter writes them
    base = many_slides(["intro"])
    jarr(base, "slides", 0, "elements")[1] = entry("text/body/0", text_ir(three, (20, 60, 200, 102), "p0t1", role="body"), "b2s_s000_t1")
    jobj(base, "slides", 0, "elements", 1, "readback", "b2s_s000_t1")["paragraph_styles"] = styles
    ours, theirs = triple(base)
    rb = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
    rb["text"] = "First point of intro\nSecond point of intro\n"   # the person deleted the last bullet
    rb["paragraph_styles"] = styles[:2]                            # and its line spacing went with it
    rb["text_style_hash"] = "one paragraph fewer"
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir(
        three.replace("First point of intro", "First point of intro, reworded"), (20, 60, 200, 102), "p0t1", role="body"))
    mplan = merge.plan_merge(base, ours, theirs)
    assert not jat(mplan, "report", "conflicts")
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and "text" in jobj(u, "overrides")
    assert jat(u, "overrides", "text_style") == {"runs": {}, "paragraphs": {}}
    # The deck's deletion stands and the source's rewording still lands.
    merged, _, safe = merge.text_merge(jstr(u, "overrides", "text", "base"),
                                       three.replace("First point of intro", "First point of intro, reworded") + "\n",
                                       jstr(u, "overrides", "text", "theirs"), ())
    assert safe and merged == "First point of intro, reworded\nSecond point of intro\n"
    # A restyle proper still is one: a style the base never had is nothing a deletion can explain,
    # and one centred paragraph among others is not something re-applying an attribute can give back.
    rb["paragraph_styles"] = [*styles[:1], {"alignment": "CENTER", "lineSpacing": 114.3}]
    assert [c for c in jobjs(merge.plan_merge(base, ours, theirs), "report", "conflicts") if c["field"] == "text_style"]


def test_the_two_halves_of_a_frame_nothing_could_follow_are_named():
    """`identity.near_misses` hands the merge a slide the source seems to have dropped and a frame
    it seems to have written that say much of the same thing. The report names both, and says which
    of the two cases this is: the deck kept the old slide (the person had edits on it) or it went."""
    base = many_slides(["a", "b"])
    ours, theirs = triple(base)
    jarr(ours, "slides")[1] = {**jobj(ours, "slides", 1), "key": "fresh"}          # the frame came back as new
    ours["pairs"] = {"0": 0}
    ours["near_misses"] = [{"ours": 1, "base": 1, "evidence": 0.6, "slide": "b", "title": "B, recast"}]
    edit_text(jat(theirs, "slides", 1), "b2s_s001_t1", "First point of b, and a sentence of my own\n")
    kept = jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings")
    assert [w for w in kept if w.startswith("slide b:") and "the deck keeps the old slide" in w
            and "'B, recast'" in w]
    # Nobody had touched that slide, so it is gone - worth saying, and a different sentence.
    _, fresh = triple(base)
    gone = jstrs(merge.plan_merge(base, ours, fresh), "report", "warnings")
    assert [w for w in gone if w.startswith("slide b:") and "it is gone" in w]


def test_a_slide_matched_by_its_place_alone_is_said_out_loud():
    """`identity.gap_pairs` pairs an unlabelled frame the source retitled and half rewrote by the
    two neighbours around it. Nothing is at risk - the alternative was a second slide beside this
    one - but it is an inference the words no longer support, so the report asks for a label. A
    frame that still has its label is paired by the label and gets no such warning."""
    base = many_slides(["a", "b", "c"])
    jobj(base, "slides", 1)["label"] = None
    ours, theirs = triple(base)
    jobj(ours, "slides", 1)["label"] = None
    ours["weak_pairs"] = {"1": "place"}
    warnings = jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings")
    assert [w for w in warnings if w.startswith("slide b:") and "where it stands" in w]
    jobj(ours, "slides", 1)["label"] = "b"      # a frame that kept its label was not matched by place
    assert not [w for w in jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings") if "where it stands" in w]
    # The keys come over JSON in a real run, so the ours index arrives as a string.
    jobj(ours, "slides", 1)["label"] = None
    ours["weak_pairs"] = {"1": "place"}
    assert [w for w in jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings") if "where it stands" in w]


def test_a_frame_matched_between_twins_is_said_out_loud_too():
    """`identity.align_slides` marks a pairing it could as well have made with another slide, for
    the same score, when the frame has no label to settle it. Nothing downstream can tell that from
    a match the words really made, so the report says which way the coin fell and asks for a label."""
    base = many_slides(["a", "b", "c"])
    jobj(base, "slides", 1)["label"] = None
    ours, theirs = triple(base)
    jobj(ours, "slides", 1)["label"] = None
    ours["weak_pairs"] = {"1": "twins"}
    warnings = jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings")
    assert [w for w in warnings if w.startswith("slide b:") and "coin toss" in w]
    jobj(ours, "slides", 1)["label"] = "b"        # a frame with a label of its own was never in doubt
    assert not [w for w in jstrs(merge.plan_merge(base, ours, theirs), "report", "warnings") if "coin toss" in w]


def test_user_added_slide_stays_after_its_predecessor():
    base = three_slides()
    ours, theirs = triple(base)
    jarr(theirs, "slides").insert(2, {"objectId": "user_slide", "layoutObjectId": "L", "background": {"state": "INHERIT"},
                                "notes": "", "notes_id": None, "order": [], "objects": {}})
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"), oid=None)], "method", title=""))
    ours2: JsonObject = {"slides": [new, *jarr(ours, "slides")], "pairs": {"1": 0, "2": 1, "3": 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert jat(mplan, "order") == ["new:method", "b2s_s000", "b2s_s001", "user_slide", "b2s_s002"]
    assert jat(mplan, "report", "slides", "user_added") == [{"objectId": "user_slide", "copy_of": None}]


def test_notes_follow_text_rules():
    base = three_slides()
    ours, theirs = triple(base)
    jobj(ours, "slides", 0)["notes"] = "Say hello"
    mplan = merge.plan_merge(base, ours, theirs)
    assert jat(mplan, "slides", 0, "notes") == "Say hello"
    jobj(theirs, "slides", 0)["notes"] = "Please say hello"
    jobj(ours, "slides", 0)["notes"] = "Say hello to everyone"
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in jobj(mplan, "slides", 0)  # both wrote into empty notes: a conflict, the deck's kept
    assert [c["field"] for c in jobjs(mplan, "report", "conflicts")] == ["notes"]
    jobj(ours, "slides", 0)["notes"] = ""
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in jobj(mplan, "slides", 0)
    assert {"slide": "intro", "element": None, "fields": ["notes"]} in jarr(mplan, "report", "overrides")


# ---------------------------------------------------------------- request helpers

def test_rename_object_ids():
    reqs: list[Json] = [{"createShape": {"objectId": "b2s_s003_t1", "elementProperties": {"pageObjectId": "b2s_s003"}}},
            {"groupObjects": {"groupObjectId": "b2s_s003_t1_g", "childrenObjectIds": ["b2s_s003_t1", "b2s_s003_f12", "b2s_s003_f1n"]}},
            {"duplicateObject": {"objectId": "b2s_s003_k0", "objectIds": {"b2s_s003_k0": "b2s_s003_s0"}}}]
    mapping = sorted({"b2s_s003_t1": "NEW_T", "b2s_s003_f1": "NEW_F", "b2s_s003_k0": "TPL", "b2s_s003": "LIVE",
                      "b2s_s003_s0": "NEW_S"}.items(), key=lambda kv: -len(kv[0]))
    out = rename(reqs, mapping)
    assert jat(out, 0, "createShape") == {"objectId": "NEW_T", "elementProperties": {"pageObjectId": "LIVE"}}
    assert jat(out, 1, "groupObjects") == {"groupObjectId": "NEW_T_g", "childrenObjectIds": ["NEW_T", "LIVE_f12", "NEW_Fn"]}
    assert jat(out, 2, "duplicateObject") == {"objectId": "TPL", "objectIds": {"TPL": "NEW_S"}}


def test_letterbox_fix_stretches_to_the_box():
    fix = jat(letterbox_fix("i", [100, 50, 400, 150], (800, 600)), "updatePageElementTransform", "transform")
    # createImage puts a 4:3 picture into a 300 x 100 box as 133.33 x 100 centred at x = 183.33
    fx0, fx1 = 183.3333, 316.6667
    assert jnum(fix, "scaleX") * fx0 + jnum(fix, "translateX") / 12700 == pytest.approx(100, abs=0.01)
    assert jnum(fix, "scaleX") * fx1 + jnum(fix, "translateX") / 12700 == pytest.approx(400, abs=0.01)
    assert jat(fix, "scaleY") == pytest.approx(1) and jat(fix, "translateY") == pytest.approx(0, abs=1)


def test_a_base_out_of_the_sources_order_costs_the_frames_after_it():
    """Why the new base has to stay in the source's order: frames without a label are paired with
    the base by an order-keeping alignment, and an entry moved to the end falls out of that order.
    What a frame says can still save it (`identity.cross_pairs` picks up what the order left over),
    but only when it says something unmistakable. Frames that talk alike have nothing to be
    recognised by and take each other's keys - and then the sync writes one onto the other."""
    source = [info("Why decks diverge", "decks and sources drift apart over time", None, 0),
              info("The sync algorithm", "base ours theirs three way merge of the deck", None, 0),
              info("Conclusions", "thanks for listening and for the questions", None, 0)]
    keys = identity.slide_keys(source)
    moved = [source[0], source[2], source[1]]  # the middle frame's entry recorded last
    got, _ = identity.inherit_slide_keys(moved, [keys[0], keys[2], keys[1]], source, moves=None, weak=None)
    assert got == keys
    alike = [info(f"Results {n}", "the table below repeats the measured numbers", None, 0) for n in ("one", "two", "three")]
    akeys = identity.slide_keys(alike)
    got, _ = identity.inherit_slide_keys([alike[0], alike[2], alike[1]], [akeys[0], akeys[2], akeys[1]], alike, moves=None, weak=None)
    assert got == [akeys[0], akeys[2], akeys[1]]


def test_a_slide_the_deck_deleted_keeps_its_place_in_the_new_base():
    """Found by the offline fuzz (`second_sync_writes`, seeds 45, 60, 108, 177 of the default run):
    the person deletes a converter slide whose frame has no label and the source still has it. The
    sync is right to leave it deleted, but it used to record that slide's base entry last
    (`new_base` keys it `gone:<key>`, which `base_order` did not return). The base is converter
    output, and the next conversion pairs frames with it by `identity.align_slides` - see the test
    above for what a base out of order costs."""
    from beamer2slides.sync import base_order
    mplan: JsonObject = {"slides": [
        {"key": "deleted_by_the_person", "action": "gone", "ours": 0, "base": 0, "objectId": None},
        {"key": "kept", "action": "update", "ours": 1, "base": 1, "objectId": "b2s_s001"}]}
    plans = jobjs(mplan, "slides")
    by_plan = {id(plans[1]): {"sid": "b2s_s001"}}
    assert base_order(mplan, by_plan, ["b2s_s001"]) == ["gone:deleted_by_the_person", "b2s_s001"]


def test_a_kept_slide_whose_words_the_source_moved_elsewhere_is_said():
    """Edit hunt h2-1: the source folded Background into Approach; the person had resized one
    phrase on Background, so the slide is kept - and its bullets were in the deck twice, unsaid."""
    base = many_slides(["background", "approach", "end"])
    words = "We target search finishing under twenty GPU hours using cheap proxies"
    jobj(base, "slides", 0)["text"] = f"Background {words}"
    ours, theirs = triple(base)
    folded = {**jobj(ours, "slides", 1), "title": "Background and Approach", "text": f"Background and Approach {words} then more"}
    ours3: JsonObject = {"slides": [folded, jat(ours, "slides", 2)], "pairs": {"0": 1, "1": 2}}
    jobj(theirs, "slides", 0, "objects")["user_box"] = readback([0, 0, 50, 20], "note")
    report = jobj(merge.plan_merge(base, ours3, theirs), "report")
    said, = [w for w in jstrs(report, "warnings") if w.startswith("slide background:")]
    assert "'Background and Approach'" in said and "twice" in said
    # a kept slide whose words went nowhere is only kept
    jarr(ours3, "slides")[0] = {**folded, "text": "Background and Approach entirely other sentences about approach methods"}
    assert not [w for w in jstrs(merge.plan_merge(base, ours3, theirs), "report", "warnings") if w.startswith("slide background:")]


def test_a_deleted_slide_in_the_base_does_not_displace_a_kept_one():
    """The two ways a slide can be in the base without being in the deck must not fight. What the
    next conversion aligns against is the entries the source still has, in the source's order: a
    `gone` slide (the person deleted it, the source kept it) is one of those and belongs between
    its source neighbours; a `keep_removed` one (the source dropped it, the deck edited it) is not,
    and only has to be somewhere."""
    from beamer2slides.sync import base_order
    mplan: JsonObject = {"slides": [
        {"key": "one", "action": "update", "ours": 0, "base": 0, "objectId": "b2s_s000"},
        {"key": "gone_one", "action": "gone", "ours": 1, "base": 1, "objectId": None},
        {"key": "two", "action": "update", "ours": 2, "base": 2, "objectId": "b2s_s002"},
        {"key": "kept_one", "action": "keep_removed", "ours": None, "base": 3, "objectId": "b2s_k003"}]}
    by_plan = {id(p): {"sid": p["objectId"]} for p in jobjs(mplan, "slides") if p["objectId"]}
    order = base_order(mplan, by_plan, ["b2s_s000", "b2s_k003", "b2s_s002"])
    assert [x for x in order if x != "b2s_k003"] == ["b2s_s000", "gone:gone_one", "b2s_s002"]
    assert "b2s_k003" in order


def test_sync_does_not_alt_text_a_diagram_group():
    """Found by the live fuzz (tools/fuzz_sync.py, seed 202): a sync that rewrites a slide with a
    diagram died with 'The operation is not allowed on group (b2s_..._<tok>)'. A diagram element's
    main object *is* a group (emit.diagram_requests groups its parts under the element's object id),
    and sync.tag_requests sent an alt-text title for every element it wrote, which took the whole
    batch down with it. snapshot.tag_requests skips element groups for the same reason; the other
    elements on that slide are still tagged."""
    from beamer2slides.emit import DeckPlan
    from beamer2slides.typing_compat import override

    class OneSlide(DeckPlan):
        """A plan whose one slide is given as emit writes it (only `slides()` is read)."""

        def __init__(self, slide: JsonObject) -> None:
            self.one = slide

        @override
        def slides(self) -> list[JsonObject]:
            return [self.one]

    o: JsonObject = {"key": "figures", "elements": [{"key": "diagram/figure/0"}, {"key": "text/body/0"}]}
    stub = sync_work.bare_sync()
    stub.plan = OneSlide({"elements": [{"kind": "diagram", "id": "p0d0"}, {"kind": "text", "id": "p0t1"}]})
    stub.ours = {"slides": [o]}
    oid = "b2s_abcdef_012345_t0k"  # the group emit creates for the diagram, with its nodes and lines inside
    text = "b2s_abcdef_012346_t0k"
    reqs = stub.tag_requests(o, {0: [oid, f"{oid}_n0", f"{oid}_l0"], 1: [text]}, {0: oid, 1: text}, {})
    assert [jat(r, "updatePageElementAltText", "objectId") for r in reqs] == [text]


def test_element_objects_from_emit_plan():
    from beamer2slides.emit import element_objects, plan_offline
    from beamer2slides.checks import convert_locally
    pdf = DECKS / "13_inline_math.pdf"
    if not pdf.exists():
        pytest.skip("build the test decks first (tests/decks/build.py)")
    planned = plan_offline(convert_locally(pdf).deck)
    for slide_id, page, parts, element_ids in planned["slides"]:
        objects, groups = element_objects(parts, element_ids)
        flat = [o for oids in objects for o in oids] + groups
        assert len(flat) == len(set(flat)), slide_id
        assert [oids[0] for oids in objects] == element_ids
        slide = next(s for s in planned["plan"].slides() if jat(s, "page") == page)
        for el, oids in zip(jobjs(slide, "elements"), objects):
            anchored = [e for e in jobjs(slide, "elements") if e.get("anchor") == el["id"]]
            if anchored and el["role"] != "title":
                assert f"{oids[0]}_g" in oids


# ---------------------------------------------------------------- found by the live suite (tests/test_sync_live.py)

def picture(size: tuple[int, int], text: str, fmt: str, scale: int) -> bytes:
    """A tight formula-like picture: `text` drawn at 4x and scaled down to `size`, saved as `fmt`
    (a JPEG at quality 75); `scale` widens what is drawn before it is squeezed into `size`."""
    import io
    from PIL import Image, ImageDraw
    big = Image.new("RGB", (size[0] // 4 * scale, size[1] // 4), "white")
    ImageDraw.Draw(big).text((1, 1), text, fill="black")
    img = big.resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, fmt, quality=75)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def test_pictures_compare_by_pixels_not_content_urls():
    """Google hands out new contentUrls for unchanged pictures: a changed URL hash alone is no
    'replaced in the deck' (it blocked source changes of every slide with a picture)."""
    from beamer2slides import snapshot
    formula = picture((240, 56), "x = a + b", "PNG", 1)
    reencoded = picture((240, 56), "x = a + b", "JPEG", 1)
    other = picture((240, 56), "y - c / d", "PNG", 1)
    stretched = picture((480, 56), "x = a + b", "PNG", 2)
    sig = snapshot.signature
    assert snapshot.signatures_match(sig(formula), sig(reencoded))
    assert not snapshot.signatures_match(sig(formula), sig(other))
    assert not snapshot.signatures_match(sig(formula), sig(stretched))
    b = image_readback([10, 10, 70, 30], "aaa")
    jobj(b, "image")["signature"] = sig(formula)
    same = copy.deepcopy(b)
    same["image"] = {"contentHash": "bbb", "signature": sig(reencoded)}
    assert object_changes(b, same) == set()
    replaced = copy.deepcopy(b)
    replaced["image"] = {"contentHash": "ccc", "signature": sig(other)}
    assert object_changes(b, replaced) == {"image"}
    unsigned = copy.deepcopy(b)
    unsigned["image"] = {"contentHash": "ddd"}
    assert object_changes(b, unsigned) == {"image"}  # (can't tell: counts as replaced)
    # backgrounds too
    base = three_slides()
    s = jobj(base, "slides", 0)
    s["background_readback"] = {"picture": "p1", "signature": sig(formula)}
    read = live(s)
    read["background"] = {"picture": "p2", "signature": sig(reencoded)}
    assert not merge.background_edited(s, read) and not merge.slide_touched(s, read)
    read["background"] = {"picture": "p3", "signature": sig(other)}
    assert merge.background_edited(s, read)


def test_new_base_keeps_the_source_slide_order():
    """The base is converter output: after a sync that kept the deck's slide order, the base
    still lists the source order, or the next sync would move the slides back."""
    from beamer2slides.sync import base_order
    mplan: JsonObject = {"slides": [
        {"key": "a", "action": "update", "ours": 0}, {"key": "b", "action": "update", "ours": 1},
        {"key": "c", "action": "create", "ours": 2}, {"key": "gone", "action": "keep_removed", "ours": None}]}
    plans = jobjs(mplan, "slides")
    by_plan = {id(plans[0]): {"sid": "A"}, id(plans[1]): {"sid": "B"}, id(plans[2]): {"sid": "C"}, id(plans[3]): {}}
    # the deck has B before A, and a kept slide G and a user slide U after B
    order = base_order(mplan, by_plan, ["B", "G", "U", "A", "C"])
    assert [x for x in order if x in "ABC"] == ["A", "B", "C"]
    assert order.index("G") < order.index("U")
    # a second plan on that base: the deck's order still counts as a deck reorder and stays
    base = three_slides()
    ours, theirs = triple(base)
    theirs["slides"] = [jat(theirs, "slides", 2), jat(theirs, "slides", 0), jat(theirs, "slides", 1)]
    mplan = merge.plan_merge(base, ours, theirs)
    assert jat(mplan, "order") == ["b2s_s002", "b2s_s000", "b2s_s001"] and not jat(mplan, "report", "slides", "moved")
    assert not merge.has_writes(mplan, slide_ids(theirs))


def test_a_slide_the_source_dropped_keeps_no_label_in_the_new_base():
    """Found by the offline fuzz (`second_sync_writes`, seed 2218): the source moves label `f0`
    onto another frame and drops the frame that had it, whose slide the deck had edited - so it
    stays, and its base entry used to keep saying `label: f0`. The next conversion's `f0` then
    pairs with that dead entry instead of the frame that carries the label now, and sync rewrites
    the wrong slide. A label belongs to the source; an entry the source no longer describes has
    none."""
    base = three_slides()
    s = sync_work.bare_sync()
    s.base, s.final_revision = base, "r2"
    s.ours = {"slides": []}
    s.source = Path("talk.pdf")
    s.created = {"slides": []}
    w = sync_work.slide_work({"key": "intro", "action": "keep_removed", "ours": None, "base": 0, "objectId": "b2s_s000"})
    w.sid = "b2s_s000"
    written = s.new_base(sync_work.run_result([w], {"slides": [{"objectId": "b2s_s000"}]}))
    assert [jat(b, "key") for b in jobjs(written, "slides")] == ["intro"]
    assert jat(written, "slides", 0, "label") is None and jat(base, "slides", 0, "label") == "intro"
    # ... and the pairing that used to go wrong now finds the frame that carries the label
    ours = [{"label": "intro", "title": "Elsewhere", "text": "another frame entirely"}]
    assert identity.label_pairs(jobjs(written, "slides"), ours) == {}


def test_a_held_slide_keeps_the_base_it_had():
    """`merge.hold_slide` wrote nothing to this slide, so the base must not say the source's words
    arrived. An entry otherwise takes its label, title and text from `ours`, and then the next sync
    would read the edit this one held back as a change already made - the one way to actually lose
    work by waiting."""
    base = three_slides()
    s = sync_work.bare_sync()
    s.base, s.final_revision = base, "r2"
    s.ours = {"slides": [{**ours_of(jobj(base, "slides", 1)), "title": "Results v2", "text": "quite different now"}]}
    s.source = Path("talk.pdf")
    s.created = {"slides": []}
    w = sync_work.slide_work({"key": "results", "action": "update", "held": "label", "ours": 0, "base": 1,
                              "objectId": "b2s_s001", "units": []})
    w.sid = "b2s_s001"
    result = sync_work.run_result([w], {"slides": [{"objectId": "b2s_s001"}]})
    (written,) = jobjs(s.new_base(result), "slides")
    assert jat(written, "title") == "Results" and jat(written, "text") == jat(base, "slides", 1, "text")
    assert jat(written, "elements") == jat(base, "slides", 1, "elements")


def test_renamed_title_keeps_its_key():
    """A retitled frame's title inherits the title key (it was deleted and recreated as a plain text
    box above the placeholder's place)."""
    base = [{"key": "text/title/0", "kind": "text", "role": "title", "fingerprint": identity.fingerprint(
                text_ir("Conclusions", (10, 10, 100, 24), "p9t0", "title"), None, None)},
            {"key": "text/body/0", "kind": "text", "role": "body", "fingerprint": identity.fingerprint(
                text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1", role="body"), None, None)}]
    ours = [text_ir("Takeaways", (10, 10, 90, 24), "p9t0", "title"), text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1", role="body")]
    keys, _ = identity.slide_element_keys(ours, None, base)
    assert keys == ["text/title/0", "text/body/0"]
    # (the slide's one title, wherever it went)
    ours = [text_ir("Takeaways", (300, 150, 380, 164), "p9t0", "title"), ours[1]]
    assert identity.slide_element_keys(ours, None, base)[0] == ["text/title/0", "text/body/0"]


def test_deleted_slide_whose_frame_counter_changed_is_no_conflict():
    base = three_slides()
    for n, s in enumerate(jobjs(base, "slides")):
        foot = entry("text/footer/0", text_ir(f"{n + 1} / 3", (300, 190, 320, 196), f"p{n}t2", "footer"), f"b2s_s{n:03}_t2")
        jarr(s, "elements").append(foot)
    ours, theirs = triple(base)
    del jarr(theirs, "slides")[2]
    jarr(ours, "slides", 2, "elements")[2] = ours_entry("text/footer/0", text_ir("4 / 4", (300, 190, 320, 196), "p3t2", "footer"))
    assert jat(merge.plan_merge(base, ours, theirs), "report", "conflicts") == []
    jarr(ours, "slides", 2, "elements")[1] = ours_entry("text/body/0", text_ir("Changed words", (20, 60, 200, 90), "p2t1", role="body"))
    assert [c["field"] for c in jobjs(merge.plan_merge(base, ours, theirs), "report", "conflicts")] == ["slide"]


def test_words_restyled_in_the_deck_survive_a_source_text_change():
    """One bolded word (a non-uniform style edit) and a reworded source: recreate and re-apply the
    deck's run styles to the same words, instead of dropping the source change as a conflict."""
    base = three_slides()
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1", role="body"))
    obj = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
    obj["text_styles"] = [*jarr(obj, "text_styles"), {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and jat(u, "overrides", "text_style", "ranges")
    assert not jat(mplan, "report", "conflicts")
    # a table: always by ranges (cell by cell)
    def cells(rows: list[list[str]]) -> list[Json]:
        return [[[run(c)] for c in row] for row in rows]

    table: JsonObject = {"id": "p1b0", "kind": "table", "role": "table", "bbox": [20, 100, 200, 160],
             "cells": cells([["Scenario", "Time"], ["Disjoint", "3.9 s"]])}
    base = three_slides()
    el = entry("table/table/0", table, "b2s_s001_tab2")
    jarr(base, "slides", 1, "elements").append(el)
    ours, theirs = triple(base)
    jarr(ours, "slides", 1, "elements")[2] = ours_entry("table/table/0", {**table, "cells": cells([["Scenario", "Time"], ["Disjoint", "4.7 s"]])})
    obj = jobj(theirs, "slides", 1, "objects", "b2s_s001_tab2")
    obj.update(kind="table", text_styles=[*jarr(obj, "text_styles"), {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}],
               text_style_hash="bold cell word")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "table/table/0")
    assert u["action"] == "recreate" and jat(u, "overrides", "text_style", "ranges")


def test_source_style_change_elsewhere_is_no_style_conflict():
    """A bullet removed in the source changes the IR's style set (list levels) while the deck
    bolded a word: no conflict, the bold is re-applied."""
    base = three_slides()
    ours, theirs = triple(base)
    ir = text_ir("First point of intro, reworded\nSecond point of intro", (20, 60, 230, 90), "p0t1", role="body")
    jobj(ir, "paragraphs", 1)["level"] = 1
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", ir)
    obj = jobj(theirs, "slides", 0, "objects", "b2s_s000_t1")
    obj["text_styles"] = [*jarr(obj, "text_styles"), {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "recreate" and not jat(mplan, "report", "conflicts")
    # the source made the text bold too: reported
    jarr(ours, "slides", 0, "elements")[1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1", bold=True, role="body"))
    mplan = merge.plan_merge(base, ours, theirs)
    assert [c["resolution"] for c in jobjs(mplan, "report", "conflicts")] == ["deck style re-applied"]


def test_style_fully_reapplied_by_the_deck_is_not_applied():
    """A theme change recolours a title in the source; the person recoloured the same title in the
    deck, to a different colour, and never asked for the source's version. `deck style re-applied`
    wins (edit hunt h5-3): the recreated title's colour ends up exactly what the deck already had,
    nothing else about it changed, so the report must not claim its style was applied - only that
    the deck's own recolouring was kept."""
    base = three_slides()
    title = jobj(base, "slides", 0, "elements", 0)
    jobj(title, "readback", jstr(title, "main"))["text_styles"] = [{"foregroundColor": "#000000"}]
    jobj(title, "readback", jstr(title, "main"))["text_style_hash"] = "base style"
    ours, theirs = triple(base)
    jarr(ours, "slides", 0, "elements")[0] = ours_entry(
        "text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title", color="#1a73e8"))
    obj = jobj(theirs, "slides", 0, "objects", jstr(title, "main"))
    obj["text_styles"] = [{"foregroundColor": "#006666"}]
    obj["text_style_hash"] = "deck recolour"
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/title/0")
    assert u["action"] == "recreate" and jat(u, "overrides", "text_style", "runs") == {"foregroundColor": "#006666"}
    assert [c["resolution"] for c in jobjs(mplan, "report", "conflicts")] == ["deck style re-applied"]
    # the write leaves the title exactly as the deck had it: nothing of the source's style landed
    assert not [a for a in jobjs(mplan, "report", "applied") if a["slide"] == "intro" and a["element"] == "text/title/0"]
    assert jat(mplan, "report", "overrides") == [{"slide": "intro", "element": "text/title/0", "fields": ["text_style"]}]


def test_deck_move_the_source_reproduces_converges():
    """A deck move written into the source (pull: a \\vspace) gives the same place: adopted, no write."""
    base = three_slides()
    base["scale"] = 2.0
    ours, theirs = triple(base)
    jarr(ours, "slides", 2, "elements")[1] = ours_entry("text/body/0", text_ir("First point of end\nSecond point of end",
                                                                         (20, 75, 200, 105), "p2t1", role="body"))
    obj = jobj(theirs, "slides", 2, "objects", "b2s_s002_t1")
    obj["box"] = [jat(obj, "box", 0), jnum(obj, "box", 1) + 30.8, jat(obj, "box", 2), jnum(obj, "box", 3) + 30.8]
    t = jarr(obj, "transform")
    t[5] = jnum(t, 5) + 30.8
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "end", "text/body/0")
    assert u["action"] == "adopt" and u["adopt"] == ["geometry"]
    assert jat(mplan, "report", "converged") == [{"slide": "end", "element": "text/body/0", "field": "geometry"}]
    assert not jat(mplan, "report", "conflicts") and not merge.has_writes(mplan, slide_ids(theirs))
    # 5 pt off: both moved, the deck's place kept
    b = jarr(obj, "box")
    b[1] = jnum(b, 1) + 5
    t = jarr(obj, "transform")
    t[5] = jnum(t, 5) + 5
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "recreate"


def raw_text(runs: Sequence[tuple[str, JsonObject]]) -> JsonObject:
    """A shape's or a cell's text as presentations.get returns it: runs = [(text, style)]."""
    elements: list[Json] = []
    i = 0
    for text, style in runs:
        elements.append({"startIndex": i, "endIndex": i + len(text), "textRun": {"content": text, "style": style}})
        i += len(text)
    return {"textElements": elements}


def raw_shape(oid: str, runs: Sequence[tuple[str, JsonObject]]) -> PageElement:
    """A page element as presentations.get returns it: runs = [(text, style)]."""
    return PageElement(objectId=oid, shape={"text": raw_text(runs)})


def test_style_range_requests_follow_the_words():
    from beamer2slides.sync import deck_attributes, style_range_requests
    plain: JsonObject = {"fontFamily": "Lato", "fontSize": {"magnitude": 18, "unit": "PT"}}
    bold: JsonObject = {**plain, "bold": True}
    base_styles = [{"fontFamily": "Lato", "fontSize": 18.0}]
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0}, base_styles) == {}
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0, "bold": True}, base_styles) == {"bold": True}
    old = raw_shape("old", [("Written by an ", plain), ("AI", bold), (" assistant\n", plain)])
    new = raw_shape("new", [("Now written by an AI assistant and converted\n", plain)])
    (r,) = style_range_requests("new", old, new, base_styles, None)
    rng = jat(r, "updateTextStyle", "textRange")
    assert "Now written by an AI assistant and converted\n"[jint(rng, "startIndex"):jint(rng, "endIndex")] == "AI"
    assert jat(r, "updateTextStyle", "style") == {"bold": True} and jat(r, "updateTextStyle", "fields") == "bold"
    # a word the source deleted takes its style along; text after the text override counts
    assert style_range_requests("new", old, raw_shape("new", [("Written by an assistant\n", plain)]), base_styles,
                                None) == []
    (r,) = style_range_requests("new", old, new, base_styles, "Written by an AI helper\n")
    assert jat(r, "updateTextStyle", "textRange", "startIndex") == 14
    # table cells, by cellLocation
    def cell(runs: Sequence[tuple[str, JsonObject]]) -> JsonObject:
        return {"text": raw_text(runs)}

    old_t = PageElement(objectId="t", table={"tableRows": [{"tableCells": [cell([("Disjoint\n", bold)]), cell([("3.9 s\n", plain)])]}]})
    new_t = PageElement(objectId="t", table={"tableRows": [{"tableCells": [cell([("Disjoint\n", plain)]), cell([("4.7 s\n", plain)])]}]})
    (r,) = style_range_requests("t", old_t, new_t, base_styles, None)
    assert jat(r, "updateTextStyle", "cellLocation") == {"rowIndex": 0, "columnIndex": 0}
    assert jat(r, "updateTextStyle", "textRange") == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 8}


def test_a_style_on_the_last_word_stops_where_the_text_does():
    """The styling of the *last* word of a box is where a range can run into the newline Slides
    will not let anything touch (`merge.text_edit_requests`): the API measures a `FIXED_RANGE`
    against the same length a delete is measured against, and a range one too long throws the whole
    batch out. Nothing here may reach it - a run's trailing newlines come off its range, and the
    text it is re-applied to ends on one - so the styling of the last word ends exactly at the
    length the API accepts, and the newline keeps the style it already has."""
    from beamer2slides.sync import style_range_requests
    plain: JsonObject = {"fontFamily": "Lato", "fontSize": {"magnitude": 18, "unit": "PT"}}
    bold: JsonObject = {**plain, "bold": True}
    base_styles = [{"fontFamily": "Lato", "fontSize": 18.0}]
    text = "Written by an assistant\n"
    old = raw_shape("old", [("Written by an ", plain), ("assistant\n", bold)])
    (r,) = style_range_requests("new", old, raw_shape("new", [(text, plain)]), base_styles, None)
    rng = jat(r, "updateTextStyle", "textRange")
    assert text[jint(rng, "startIndex"):jint(rng, "endIndex")] == "assistant"
    assert jat(rng, "endIndex") == len(text) - 1   # exactly the length `deleteText` would accept


SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"


def test_retitled_frame_and_right_limits_on_the_sync_talk(tmp_path: Path) -> None:
    """On the sync test talk (tests/decks/sync): the unlabelled frame retitled Takeaways keeps its
    title key, and an element recreated on a slide whose title isn't rewritten gets the same box
    as in a fresh conversion (a title demoted to body text widened text_right_limit)."""
    from beamer2slides.sync import build_ours_of
    v1, untitled = SYNC_DECKS / "v1.pdf", SYNC_DECKS / "untitled.pdf"
    if not (v1.exists() and untitled.exists()):
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py, or pytest -m sync -k variants)")
    first = build_ours_of(v1, tmp_path / "v1", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
    first_slides: list[Json] = [*first.slides]
    second = build_ours_of(untitled, tmp_path / "untitled", {"slides": first_slides}, "last", SLIDE_W, snapshot.NO_PICTURES)
    last = second.slides[-1]
    assert last["key"] == first.slides[-1]["key"]
    assert [jat(e, "key") for e in jobjs(last, "elements") if jat(e, "role") == "title"] == ["text/title/0"]

    s = sync_work.bare_sync()
    ours = sync_work.with_ours(s, first)
    s.tok = "1zz"
    s.urls = {}
    s.warnings = []
    j = next(k for k, o in enumerate(first.slides) if o["key"] == "steps")
    elements = jobjs(first.plan.deck, "slides", j, "elements")
    body = next(i for i, e in enumerate(elements) if e.get("role") == "body" and e["kind"] == "text")
    title = next(i for i, e in enumerate(elements) if e.get("role") == "title")

    def body_box(in_place: Mapping[int, Refilled]) -> Json:
        reqs, _, new_oid, _ = s.slide_requests(j, [body], "LIVE", in_place, {}, {}, False, frozenset())
        shape = next(jobj(r, "createShape") for r in reqs
                     if "createShape" in r and jat(r, "createShape", "objectId") == new_oid[body])
        return jat(shape, "elementProperties", "size", "width", "magnitude")
    placeholder = {title: InPlace(id="LIVE_title", size=(680.0, 36.0), text="The sync algorithm")}
    assert body_box({}) == body_box(placeholder)

    # Two title boxes and no live placeholder (an adopted slide: `marked.py` gives the title role
    # per box): once the first is demoted, the second is no placeholder either. It was looked up
    # in the slide's placeholders with a bare next(), and an apply died on "StopIteration: ".
    deck_slides = jarr(first.plan.deck, "slides")
    slide = jobj(deck_slides, j)
    second_title: JsonObject = {**copy.deepcopy(elements[title]), "id": "second-title"}
    b = jnums(second_title, "bbox")
    second_title["bbox"] = [b[0], b[3] + 2, b[2], 2 * b[3] - b[1] + 2]
    deck_slides[j] = {**slide, "elements": [*elements, second_title]}
    ours_slides = jarr(ours, "slides")
    ours_slide = jobj(ours_slides, j)
    ours_elements = jobjs(ours_slide, "elements")
    ours_slides[j] = {**ours_slide, "elements": [*ours_elements, {**ours_elements[title], "key": "text/title/1"}]}
    try:
        both = len(elements)
        reqs, _, new_oid, _ = s.slide_requests(j, [title, both], "LIVE", {}, {}, {}, False, frozenset())
        made = {jat(r, "createShape", "objectId") for r in reqs if "createShape" in r}
        assert {new_oid[title], new_oid[both]} <= made          # both made as boxes of their own
    finally:
        deck_slides[j] = slide
        ours_slides[j] = first.slides[j]

    # A unit whose group the deck took apart is rebuilt without its group.
    base_slide_ = copy.deepcopy(first.slides[j])
    read_objects: JsonObject = {}
    read: JsonObject = {"objects": read_objects, "notes": "", "notes_id": None}
    for e in jobjs(base_slide_, "elements"):
        oid = f"OLD_{jstr(e, 'key').replace('/', '_')}"
        e["main"] = oid
        e["objects"] = [oid, f"{oid}_g"] if jat(e, "key") == "text/body/0" else [oid]
        e["readback"] = {oid: readback([0, 0, 10, 10], "x")}
        read_objects[oid] = readback([0, 0, 10, 10], "x")
    s.base = {"slides": [base_slide_]}
    from collections import defaultdict
    s.urls = defaultdict(lambda: "https://example.com/staged.png")  # (the staging deck's picture URLs)
    members: list[Json] = [e["key"] for e in jobjs(first.slides[j], "elements")
                           if e["key"] == "text/body/0" or e.get("anchor") == "text/body/0"]
    plan: JsonObject = {"key": "steps", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": members, "base_members": members, "overrides": {}}]}

    def groups_made(live_objects: JsonObject) -> list[Json]:
        reqs = s.update_slide(sync_work.slide_work(plan), {**read, "objects": live_objects}, {}, {})
        return [jat(r, "groupObjects", "groupObjectId") for r in reqs if "groupObjects" in r]
    body_oid = "OLD_text_body_0"
    assert len(groups_made(read_objects)) == 0  # the deck ungrouped it
    grouped: JsonObject = {k: {**jobj(v), "parent_group": f"{body_oid}_g" if k != f"{body_oid}_g" else None}
                           for k, v in read_objects.items()}
    grouped[f"{body_oid}_g"] = {**readback([0, 0, 10, 10], None), "kind": "elementGroup"}
    assert len(groups_made(grouped)) == 1


def test_unit_rebuilt_inside_a_group_nested_in_a_user_group(tmp_path: Path) -> None:
    """A block (converter group) inside a group made in the deck: ungroup outermost first, regroup
    innermost first under the same ids (the rebuilt block title used to stay outside, ungrouped)."""
    from collections import defaultdict
    from beamer2slides.sync import build_ours_of
    v1 = SYNC_DECKS / "v1.pdf"
    if not v1.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours_of(v1, tmp_path / "v1", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
    s = sync_work.bare_sync()
    sync_work.with_ours(s, first)
    s.tok = "1zz"
    s.warnings = []
    s.urls = defaultdict(lambda: "https://example.com/staged.png")
    j = next(k for k, o in enumerate(first.slides) if o["key"] == "policy")
    base_slide_ = copy.deepcopy(first.slides[j])
    objects: JsonObject = {}
    for e in jobjs(base_slide_, "elements"):
        oid = f"OLD_{jstr(e, 'key').replace('/', '_')}"
        e["main"] = oid
        e["objects"] = [oid]
        e["readback"] = {oid: readback([0, 0, 10, 10], "x")}
        objects[oid] = readback([0, 0, 10, 10], "x")
    block = ["OLD_shape_panel_0", "OLD_shape_panel_1", "OLD_text_body_0", "OLD_text_body_1"]
    objects["BLK"] = {**readback([0, 0, 10, 10], None), "kind": "elementGroup", "children": [*block], "parent_group": "USER"}
    objects["USER"] = {**readback([0, 0, 10, 10], None), "kind": "elementGroup", "children": ["BLK", "OLD_text_body_2"]}
    for c in block:
        jobj(objects, c)["parent_group"] = "BLK"
    jobj(objects, "OLD_text_body_2")["parent_group"] = "USER"
    s.base = {"slides": [base_slide_]}
    plan: JsonObject = {"key": "policy", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": ["text/body/0"], "base_members": ["text/body/0"], "overrides": {}}]}
    w = sync_work.slide_work(plan)
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    assert [jat(r, "ungroupObjects", "objectIds") for r in reqs if "ungroupObjects" in r] == [["USER"], ["BLK"]]
    groups = [jobj(r, "groupObjects") for r in reqs if "groupObjects" in r]
    new_title = w.new_oid[next(i for i, e in enumerate(jobjs(first.slides[j], "elements")) if e["key"] == "text/body/0")]
    assert [jat(g, "groupObjectId") for g in groups] == ["BLK", "USER"]
    assert new_title in jarr(groups[0], "childrenObjectIds") and "OLD_text_body_0" not in jarr(groups[0], "childrenObjectIds")
    assert jat(groups[1], "childrenObjectIds") == ["BLK", "OLD_text_body_2"]
    assert not s.warnings
    # Each group's members are stacked in its old order before it is made: the rebuilt title was
    # created last, so on top, and a group keeps the z-order its children had.
    for gi, r in enumerate(reqs):
        if "groupObjects" in r:
            kids = jarr(r, "groupObjects", "childrenObjectIds")
            before = reqs[gi - len(kids):gi]
            assert [jat(x, "updatePageElementsZOrder", "pageElementObjectIds", 0) for x in before] == kids
            assert all(jat(x, "updatePageElementsZOrder", "operation") == "BRING_TO_FRONT" for x in before)


Margins = Callable[[JsonObject], Json]
"""A base table element's `table_margins` (`_table_sync`)."""
TableRun = Callable[[Margins, JsonObject, Sequence[str], Json], tuple[Sync, SlideWork, list[JsonObject], str]]
"""`_table_sync`'s run(margins, overrides, restored, source)."""


def _table_sync(tmp_path: Path, variant: str) -> tuple[TableRun, Built, Built, int]:
    """(run, first, second, j) for the Results table of the sync talk, v1 -> `variant`: run(margins,
    overrides, restored, source) drives Sync.update_slide on it as a recreated unit, `margins(base
    element)` giving the base's table_margins, `overrides` the unit's, `restored` the object ids an
    interrupted sync put back and `source` the unit's `source` (None: not said), and returns (sync,
    work, requests, the table's old object id)."""
    from collections import defaultdict
    from beamer2slides.sync import build_ours_of
    v1, new = SYNC_DECKS / "v1.pdf", SYNC_DECKS / f"{variant}.pdf"
    if not v1.exists() or not new.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours_of(v1, tmp_path / "v1", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
    second = build_ours_of(new, tmp_path / "new", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
    j = next(k for k, o in enumerate(second.slides) if o["title"] == "Results")
    jb = next(k for k, o in enumerate(first.slides) if o["title"] == "Results")

    def run(margins: Margins, overrides: JsonObject, restored: Sequence[str],
            source: Json) -> tuple[Sync, SlideWork, list[JsonObject], str]:
        s = sync_work.bare_sync()
        sync_work.with_ours(s, second)
        s.tok = "1zz"
        s.warnings = []
        s.recovery = Recovery(sweep=[], sweep_slides=[], heal=[], restore={oid: {} for oid in restored})
        s.urls = defaultdict(lambda: "https://example.com/staged.png")
        base_slide_ = copy.deepcopy(first.slides[jb])
        objects: JsonObject = {}
        for e in jobjs(base_slide_, "elements"):
            oid = f"OLD_{jstr(e, 'key').replace('/', '_')}"
            rb: JsonObject = readback([0, 0, 10, 10], "x")
            if e["kind"] == "table":
                cells = [["".join(jstr(r, "text") for r in jobjs(cell)).strip() for cell in jarr(row)]
                         for row in jarr(e, "ir", "cells")]
                rb = {**readback([0, 0, 10, 10], "\n".join("\t".join(row) for row in cells)), "kind": "table",
                      "table": [len(cells), len(cells[0])]}
                e["table_margins"] = margins(e)
            e["main"] = oid
            e["objects"] = [oid]
            e["readback"] = {oid: rb}
            objects[oid] = rb
        s.base = {"slides": [base_slide_]}
        key = next(jstr(e, "key") for e in jobjs(base_slide_, "elements") if e["kind"] == "table")
        unit_plan: JsonObject = {"key": key, "action": "recreate", "ours_members": [key], "base_members": [key],
                                 "overrides": overrides}
        if source is not None:
            unit_plan["source"] = source
        plan: JsonObject = {"key": "results", "base": 0, "ours": j, "objectId": "LIVE", "units": [unit_plan]}
        w = sync_work.slide_work(plan)
        return s, w, s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {}), f"OLD_{key.replace('/', '_')}"

    return run, first, second, j


# (a text box a new neighbour narrowed: tests/test_emitted_diff.py)


def test_refit_jobs_reaches_a_recreated_title_in_its_placeholder() -> None:
    """A recreated title goes back into its live placeholder (`update_slide`'s `in_place`): the
    guard used to skip it on `rb.get("placeholder")` alone, shutting it out of `refit.plan`
    altogether, so a panel a theme swap drew under it was never fitted to the person's larger font
    and nothing was said (edit hunt h5-6). Only a table refilled in place is excluded now; `refit.plan`
    itself leaves a placeholder's own box alone."""
    rb: JsonObject = {"kind": "shape", "shape_style": {"type": "TEXT_BOX", "align": "MIDDLE"},
                      "placeholder": "CENTERED_TITLE", "text": "Thermal Drift in Compact Sensors\n",
                      "run_spans": [[0, 32, {"fontFamily": "Lato", "fontSize": 34.0, "bold": True}]],
                      "paragraph_styles": [{"lineSpacing": 100, "alignment": "CENTER"}],
                      "box": [80.0, 80.0, 480.0, 120.0], "transform": [1.0, 0.0, 0.0, 1.0, 80.0, 80.0],
                      "size": [400.0, 40.0], "parent_group": None, "z": 2}
    s = sync_work.bare_sync()
    s.ours = {"slides": [{"key": "title", "elements": [{"key": "text/title/0"}]}]}
    s.base = {"slides": [{"key": "title", "elements": [{"key": "text/title/0", "main": "TITLE_PH"}]}]}
    plan: JsonObject = {"action": "update", "key": "title", "base": 0, "ours": 0, "objectId": "SID", "units": [
        {"key": "text/title/0", "action": "recreate", "overrides": {"text_style": {"runs": {"bold": True}}}}]}

    def title_work(in_place: Refilled) -> SlideWork:
        w = sync_work.slide_work(plan)
        w.new_oid = {0: "TITLE_PH"}
        w.objects = {0: ["TITLE_PH"]}
        w.in_place = {0: in_place}
        w.doomed = set()
        return w
    work = sync_work.work([title_work(InPlace(id="TITLE_PH", size=(400.0, 40.0), text=jstr(rb, "text").strip()))], ())
    created: JsonObject = {"slides": [{"objectId": "SID", "objects": {"TITLE_PH": rb}}]}
    theirs: JsonObject = {"slides": [{"objectId": "SID", "objects": {"TITLE_PH": rb}}]}
    jobs = s.refit_jobs(work, theirs, created)
    assert jobs == {"SID": [refit.RefitJob(key="slide title: text/title/0", slide="title",
                                           names={"TITLE_PH": "text/title/0"}, text="TITLE_PH", pictures=(),
                                           own=frozenset({"TITLE_PH"}), doomed=frozenset(), theirs=rb)]}

    # A table refilled in place stays excluded: it is not a recreated box either.
    w2 = title_work(TableFill(id="TITLE_PH", cells=[], steps=[], shift=(0.0, 0.0), margins=[]))
    assert s.refit_jobs(sync_work.work([w2], ()), theirs, created) == {}


def pptx_margins(built: Built, lower: float) -> Margins:
    """The base's `table_margins` of a table element as convert's .pptx gives it (`emit.pptx_table`),
    every row's top margin `lower` pt larger."""
    from beamer2slides.emit import pptx_table

    def margins(e: JsonObject) -> Json:
        rows: list[Json] = []
        for m in pptx_table(jobj(e, "ir"), built.plan.scale, built.plan.fonts, SLIDE_W / built.plan.scale)["margins"]:
            row: list[Json] = [m[0], m[1] + lower, m[2], m[3]] if lower else [*m]
            rows.append(row)
        return rows
    return margins


def no_margins(e: JsonObject) -> Json:
    """A table element's `table_margins` when none were recorded (made by createTable)."""
    return None


def test_a_table_whose_words_changed_is_refilled_in_place(tmp_path: Path) -> None:
    """The source changed one cell of a table convert brought with the .pptx: the table keeps its
    object (and the cell margins the API can't set) and only its cells are rewritten; a table
    whose margins no longer fit (or with no margins recorded: made by createTable) is made again.
    A table the source moved as well is refilled and moved."""
    run, first, second, j = _table_sync(tmp_path, "tablecell")
    scale = first.plan.scale
    margins = pptx_margins(first, 0)

    s, w, reqs, old = run(margins, {}, (), None)
    assert not [r for r in reqs if "createTable" in r]
    assert old not in s.cleanup_ids and old in w.new_oid.values()
    cleared = [jobj(r, "deleteText") for r in reqs if "deleteText" in r]
    assert cleared and all(d["objectId"] == old and "cellLocation" in d for d in cleared)
    assert any(jat(r, "insertText", "text") == "4.7 s" for r in reqs if "insertText" in r and jat(r, "insertText", "objectId") == old)
    assert not [r for r in reqs if "updateTableRowProperties" in r and jat(r, "updateTableRowProperties", "objectId") != old]
    assert not [r for r in reqs if "updatePageElementTransform" in r]
    # An interrupted sync already rewrote it (its read-back is the person's version put back): made again.
    s, w, reqs, old = run(margins, {}, [old], None)
    assert [r for r in reqs if "createTable" in r] and old in s.cleanup_ids

    # The source moved it down 20 pt as well (a line added above): refilled, then moved by as much -
    # also when the deck moved it itself (merge's geometry override): the source's move goes on top
    # of the person's place, as `sync.carried` does for a recreated unit.
    table = next(e for e in jobjs(second.plan.deck, "slides", j, "elements") if e["kind"] == "table")
    for field in ("bbox", "frame"):
        box = jarr(table, field)
        box[1] = jnum(box, 1) + 20
        box[3] = jnum(box, 3) + 20
    table["row_baselines"] = [b + 20 for b in jnums(table, "row_baselines")]
    rules = table.get("rules", [])
    borders = table.get("borders", [])
    for rule in [*jobjs(rules), *jobjs(borders)]:
        if "y" in rule:
            rule["y"] = jnum(rule, "y") + 20
    s, w, reqs, old = run(margins, {}, (), None)
    moved = [jobj(r, "updatePageElementTransform") for r in reqs if "updatePageElementTransform" in r]
    assert not [r for r in reqs if "createTable" in r] and old not in s.cleanup_ids
    assert len(moved) == 1 and moved[0]["objectId"] == old and moved[0]["applyMode"] == "RELATIVE"
    assert jat(moved[0], "transform", "translateY") == pytest.approx(20 * scale * 12700, abs=2)
    assert abs(jnum(moved[0], "transform", "translateX")) <= 1
    s, w, reqs, old = run(margins, {"geometry": {"mode": "delta"}}, (), ["position", "text"])
    moved = [jobj(r, "updatePageElementTransform") for r in reqs if "updatePageElementTransform" in r]
    assert len(moved) == 1 and moved[0]["objectId"] == old and old not in s.cleanup_ids
    assert jat(moved[0], "transform", "translateY") == pytest.approx(20 * scale * 12700, abs=2)
    # ... and a plan that says the source did not move it leaves the person's place alone.
    s, w, reqs, old = run(margins, {"geometry": {"mode": "delta"}}, (), ["text"])
    assert not [r for r in reqs if "updatePageElementTransform" in r] and old not in s.cleanup_ids

    for other in (no_margins, pptx_margins(first, 3)):
        s, w, reqs, old = run(other, {}, (), None)
        assert [r for r in reqs if "createTable" in r] and old in s.cleanup_ids


def test_a_table_the_source_added_a_row_to_is_grown_in_place(tmp_path: Path) -> None:
    """A row added at the end of the table: one insertTableRows below the last row (whose margins
    the new row takes), between emptying the cells and filling them; the base keeps the margins."""
    run, first, second, j = _table_sync(tmp_path, "table-row")
    s, w, reqs, old = run(pptx_margins(first, 0), {}, (), None)
    assert not [r for r in reqs if "createTable" in r] and old not in s.cleanup_ids
    kinds = [next(iter(r)) for r in reqs]
    grown = [r["insertTableRows"] for r in reqs if "insertTableRows" in r]
    assert grown == [{"tableObjectId": old, "cellLocation": {"rowIndex": 3, "columnIndex": 0}, "insertBelow": True, "number": 1}]
    assert kinds.index("insertTableRows") > max(k for k, x in enumerate(kinds) if x == "deleteText")
    assert kinds.index("insertTableRows") < kinds.index("insertText")
    assert any(jat(r, "insertText", "text") == "4.2 s" and jat(r, "insertText", "cellLocation", "rowIndex") == 4
               for r in reqs if "insertText" in r and jat(r, "insertText", "objectId") == old)
    i = next(k for k, e in enumerate(jobjs(second.plan.deck, "slides", j, "elements")) if e["kind"] == "table")
    filled = w.in_place[i]
    assert isinstance(filled, TableFill) and len(filled.margins) == 5


def test_table_steps_give_a_table_the_rows_and_columns_it_needs() -> None:
    from beamer2slides.sync import table_steps

    def m(*tops: float) -> list[list[float]]:
        return [[7.2, t, 7.2, 0.0] for t in tops]

    def kinds(steps: tuple[list[JsonObject], list[list[float]]] | None) -> list[tuple[str, Json]]:
        assert steps is not None
        return [(next(iter(r)), jat(next(iter(r.values())), "cellLocation")) for r in steps[0]]

    # A row added at the end of a booktabs table takes the margins of the row above it.
    steps = table_steps(m(5.0, 4.8, 0, 0), m(5.0, 4.8, 0, 0, 0), 3, 3)
    assert kinds(steps) == [("insertTableRows", {"rowIndex": 3, "columnIndex": 0})]
    assert steps is not None and steps[1] == m(5.0, 4.8, 0, 0, 0)
    # ... and in the middle, below the row it copies; the margins stay the table's own.
    steps = table_steps(m(5.0, 4.8, 0), m(5.01, 4.8, 4.8, 0), 3, 3)
    assert kinds(steps) == [("insertTableRows", {"rowIndex": 1, "columnIndex": 0})]
    assert steps is not None and steps[1] == m(5.0, 4.8, 4.8, 0)
    # A row removed; a column added and two removed at the right edge.
    assert kinds(table_steps(m(5.0, 4.8, 0, 0), m(5.0, 4.8, 0), 3, 4)) == [
        ("deleteTableRow", {"rowIndex": 3, "columnIndex": 0}), ("insertTableColumns", {"rowIndex": 0, "columnIndex": 2})]
    assert kinds(table_steps(m(5.0, 0), m(5.0, 0), 4, 2)) == [("deleteTableColumn", {"rowIndex": 0, "columnIndex": 3}),
                                                            ("deleteTableColumn", {"rowIndex": 0, "columnIndex": 2})]
    # A first row no row can give its margins to (a new rule above it): made again.
    assert table_steps(m(5.0, 0), m(3.0, 5.0, 0), 3, 3) is None
    assert table_steps(m(5.0, 0), m(5.0, 2.0), 3, 3) is None
    assert table_steps(m(5.0, 0), m(5.0, 0), 3, 3) == ([], m(5.0, 0))


def _picture_drawn(folder: Path, transparent: bool, mark: tuple[int, int, int, int],
                   ground: tuple[int, int, int]) -> Path:
    """A small picture, a black `mark` on a transparent or a `ground`-painted ground, written to
    <folder>/figures/f1.png."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (60, 20), (*ground, 0 if transparent else 255))
    ImageDraw.Draw(img).rectangle(mark, fill=(0, 0, 0, 255))
    if not transparent:
        img = img.convert("RGB")
    (folder / "figures").mkdir(parents=True, exist_ok=True)
    img.save(folder / "figures" / "f1.png")
    return folder


def _picture_files(folder: Path, transparent: bool) -> Path:
    """`_picture_drawn` of the usual mark on a transparent or a white ground."""
    return _picture_drawn(folder, transparent, (10, 5, 40, 15), (255, 255, 255))


class PictureOurs(TypedDict):
    """`_picture_deck`'s new conversion: its slide entries, ours index -> base index, and the
    folder its pictures were rendered into (`snapshot.refresh_pictures` reads all three)."""
    slides: list[JsonObject]
    pairs: dict[int, int]
    out: Path


def merged_view(ours: PictureOurs) -> JsonObject:
    """What the merge reads of `ours` (`merge.ours_of`): its slides and pairs, as JSON has them."""
    return {"slides": [*ours["slides"]], "pairs": {str(k): v for k, v in ours["pairs"].items()}}


def _picture_deck(deck_out: Path, ours_out: Path) -> tuple[JsonObject, PictureOurs, JsonObject]:
    """A one-slide base with a title and an anchored picture, and ours from `ours_out`'s files."""
    ir: JsonObject = {"id": "p0i0", "kind": "image", "role": "math", "bbox": [20.0, 60.0, 80.0, 80.0],
                      "file": "figures/f1.png", "alt": None}

    def element(out: Path, oid: str | None) -> JsonObject:
        h, fields = identity.ir_fields(ir, out, None, None)
        el: JsonObject = {"key": "image/math/0", "id": ir["id"], "kind": "image", "role": "math", "ir_hash": h,
                          "fields": fields, "fingerprint": identity.fingerprint(ir, out, None), "anchor": None, "ir": ir}
        if oid:
            el["objects"] = [oid]
            el["main"] = oid
            el["readback"] = {oid: image_readback([40, 120, 160, 160], "c1")}
        return el
    title = entry("text/title/0", text_ir("Figures", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0")
    slide = base_slide("figs", "b2s_s000", [title, element(deck_out, "b2s_s000_i0")], title="Figures", label=None)
    base: JsonObject = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None,
                        "slides": [slide]}
    ours = PictureOurs(slides=[{**ours_of(slide),
                                "elements": [{k: v for k, v in title.items() if k not in ("objects", "main", "readback")},
                                             element(ours_out, None)]}],
                       pairs={0: 0}, out=ours_out)
    return base, ours, {"revisionId": "r", "slides": [live(slide)]}


def refreshed(base: JsonObject, ours: PictureOurs, *folders: Path) -> list[snapshot.Refreshed]:
    """`snapshot.refresh_pictures` with the base's pictures looked for in `folders`."""
    pictures = snapshot.find_base_pictures(base, snapshot.PictureFolders(kept=folders, rendered=None, held=None))
    return snapshot.refresh_pictures(base, ours["slides"], ours["pairs"], ours["out"], pictures)


def test_a_picture_written_differently_is_no_source_change(tmp_path: Path) -> None:
    """Anchored pictures became RGBA (a transparent ground instead of a white one): the file sha1
    changes though the slide looks the same. Rewriting every one of them on the first sync after
    that would churn the deck, so the base takes the new hash and nothing is written."""
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=True)
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    el, oe = jobj(base, "slides", 0, "elements", 1), jobj(ours["slides"][0], "elements", 1)
    assert identity.source_changes(el, oe) == {"image"}
    assert merge.has_writes(merge.plan_merge(copy.deepcopy(base), merged_view(ours), theirs), ["b2s_s000"])
    assert refreshed(base, ours, deck_out) == [snapshot.Refreshed(slide="figs", element="image/math/0")]
    assert identity.source_changes(el, oe) == set()
    mplan = merge.plan_merge(base, merged_view(ours), theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "keep"
    assert not merge.has_writes(mplan, ["b2s_s000"])


def test_a_picture_that_really_changed_is_still_rewritten(tmp_path: Path) -> None:
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_drawn(tmp_path / "ours", True, (10, 5, 50, 15), (255, 255, 255))
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    assert refreshed(base, ours, deck_out) == []
    mplan = merge.plan_merge(base, merged_view(ours), theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "recreate"


def test_the_ground_a_picture_dropped_must_be_flat(tmp_path: Path) -> None:
    """The older picture painted the page under it: the same picture on a transparent ground is
    the same only where what it dropped was that one colour."""
    from beamer2slides.snapshot import same_picture_file
    clear = _picture_files(tmp_path / "clear", transparent=True) / "figures" / "f1.png"
    painted = clear
    for ground in ((255, 255, 255), (238, 245, 255)):
        painted = _picture_drawn(tmp_path / f"g{ground[0]}", False, (10, 5, 40, 15), ground) / "figures" / "f1.png"
        assert same_picture_file(painted, clear)
    # the older picture had a second mark where the new one is clear: not the same picture
    from PIL import Image, ImageDraw
    busy = Image.open(painted).convert("RGB")
    ImageDraw.Draw(busy).rectangle((45, 2, 58, 18), fill=(0, 0, 0))
    busy.save(tmp_path / "busy.png")
    assert not same_picture_file(tmp_path / "busy.png", clear)


def _image_entry(out: Path, oid: str | None) -> JsonObject:
    """The entry of figure `image/figure/0` whose picture is <out>/figures/f1.png, at (20, 60, 80, 80);
    with `oid`, as a base records it on that object."""
    bbox = (20.0, 60.0, 80.0, 80.0)
    ir: JsonObject = {"id": "p0i9", "kind": "image", "role": "figure", "bbox": list(bbox), "file": "figures/f1.png",
                      "alt": None}
    h, fields = identity.ir_fields(ir, out, None, None)
    el: JsonObject = {"key": "image/figure/0", "id": ir["id"], "kind": "image", "role": "figure", "ir_hash": h,
                      "fields": fields, "fingerprint": identity.fingerprint(ir, out, None), "anchor": None, "ir": ir}
    if oid:
        el["objects"] = [oid]
        el["main"] = oid
        el["readback"] = {oid: image_readback([2 * v for v in bbox], "c1")}
    return el


def test_a_picture_the_deck_already_shows_is_adopted_not_duplicated(tmp_path: Path) -> None:
    """After a pull the source draws a picture the person put into the deck: keep their object
    (their crop, rotation and outline) instead of creating a second one next to it."""
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    base = three_slides()
    slides: list[Json] = [ours_of(s) for s in jobjs(base, "slides")]
    jarr(slides, 0, "elements").append(_image_entry(ours_out, None))
    ours: JsonObject = {"slides": slides, "pairs": {"0": 0, "1": 1, "2": 2}}
    theirs: JsonObject = {"revisionId": "r", "slides": [live(s) for s in jobjs(base, "slides")]}
    jobj(theirs, "slides", 0, "objects")["USERPIC"] = image_readback([40, 120, 160, 160], "c9")
    live_ids = slide_ids(theirs)
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "create" and merge.has_writes(plain, live_ids)
    assert [u["objectId"] for u in jobjs(plain, "report", "user_objects")] == ["USERPIC"]
    asked: list[tuple[str, list[Json], str | None]] = []

    def adopt(skey: str, members: list[JsonObject], read: JsonObject, oid: str | None) -> str | None:
        asked.append((skey, [m["key"] for m in members], oid))
        return "USERPIC" if oid is None else None
    mplan = merge.plan_merge_with(base, ours, theirs, adopt=adopt, follow_labels=False, take_source=())
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["objectId"]) == ("adopt_object", "USERPIC")
    assert not merge.has_writes(mplan, live_ids)
    assert not jat(mplan, "report", "user_objects")  # (the object belongs to the source's element now)
    assert [(c["element"], c["field"]) for c in jobjs(mplan, "report", "converged")] == [("image/figure/0", "image")]
    assert asked == [("intro", ["image/figure/0"], None)]


def test_the_deck_picture_the_source_now_draws_is_no_conflict(tmp_path: Path) -> None:
    """The person replaced a drawn figure with their own picture and pull put that picture in the
    source: the deck's object is what the source draws now, so nothing is written or reported as
    a conflict."""
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_drawn(tmp_path / "ours", False, (12, 4, 44, 16), (255, 255, 255))
    base = three_slides()
    jarr(base, "slides", 0, "elements").append(_image_entry(deck_out, "b2s_s000_i0"))
    slides: list[Json] = [ours_of(s) for s in jobjs(base, "slides")]
    jarr(slides, 0, "elements")[-1] = _image_entry(ours_out, None)
    ours: JsonObject = {"slides": slides, "pairs": {"0": 0, "1": 1, "2": 2}}
    theirs: JsonObject = {"revisionId": "r", "slides": [live(s) for s in jobjs(base, "slides")]}
    jobj(theirs, "slides", 0, "objects", "b2s_s000_i0")["image"] = {"contentHash": "c2"}  # replaced in the deck
    live_ids = slide_ids(theirs)
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "keep"
    assert [c["field"] for c in jobjs(plain, "report", "conflicts")] == ["image"]

    def given(skey: str, members: list[JsonObject], read: JsonObject, oid: str | None) -> str | None:
        return oid
    mplan = merge.plan_merge_with(base, ours, theirs, adopt=given, follow_labels=False, take_source=())
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["adopt"]) == ("adopt", ["image"])
    assert not jat(mplan, "report", "conflicts") and not merge.has_writes(mplan, live_ids)
    assert [(c["element"], c["field"]) for c in jobjs(mplan, "report", "converged")] == [("image/figure/0", "image")]


def test_the_adopter_matches_by_bytes_and_by_look(tmp_path: Path, fetcher: Fetcher) -> None:
    """sync.picture_adopter on a live slide: the same bytes or the same look in the right place,
    and never a converter object, a copy or a picture inside a group."""
    from beamer2slides.sync import box_overlap
    from beamer2slides.sync_model import ObjectId, SlideKey
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    scaled = _picture_files(tmp_path / "scaled", transparent=False)  # (the same drawing at twice the size)
    from PIL import Image
    with Image.open(ours_out / "figures" / "f1.png") as img:
        img.resize((120, 40), Image.Resampling.LANCZOS).save(scaled / "figures" / "f1.png")
    crop = _picture_files(tmp_path / "crop", transparent=False)  # (the converter renders the region again)
    with Image.open(ours_out / "figures" / "f1.png") as img:
        padded = Image.new("RGB", (66, 24), "white")
        padded.paste(img, (3, 2))
        padded.resize((132, 48), Image.Resampling.LANCZOS).save(crop / "figures" / "f1.png")
    other = _picture_drawn(tmp_path / "other", False, (2, 2, 58, 18), (255, 255, 255))
    files = {"u_same": ours_out, "u_look": scaled, "u_crop": crop, "u_other": other}
    fetcher(lambda url: (files[url] / "figures" / "f1.png").read_bytes())
    pres = Presentation(slides=[Page(objectId="S", pageElements=[
        PageElement(objectId=oid, image={"contentUrl": url})
        for oid, url in [("SAME", "u_same"), ("LOOK", "u_look"), ("CROP", "u_crop"), ("OTHER", "u_other"),
                         ("COPY", "u_same"), ("INGROUP", "u_same"), ("B2S", "u_same")]])])
    s = sync_work.bare_sync()
    s.ours = {}
    s.ours_out = ours_out
    s.scale = 2.0
    s.base = {"slides": [{"key": "intro", "objectId": "S", "groups": [],
                          "elements": [{"key": "image/figure/0", "objects": ["B2S"]}]}]}
    boxes = {"SAME": [40, 120, 160, 160], "LOOK": [44, 124, 164, 164], "CROP": [38, 118, 162, 162],
             "OTHER": [40, 120, 160, 160], "COPY": [40, 120, 160, 160], "INGROUP": [40, 120, 160, 160],
             "B2S": [40, 120, 160, 160]}
    objects: JsonObject = {oid: image_readback(box, oid) for oid, box in boxes.items()}
    read: JsonObject = {"objects": objects}
    jobj(objects, "COPY")["title"] = "b2s:intro/image/figure/0"
    jobj(objects, "INGROUP")["parent_group"] = "G"
    el = _image_entry(ours_out, None)

    def adopt(oid: str | None) -> str | None:  # (the slide's read as it stands now: the checks below take objects out of it)
        from beamer2slides import sync_model
        return s.picture_adopter(pres)(SlideKey("intro"), [sync_model.element_entry(el, "el")],
                                       sync_model.slide_read({**read, "objectId": "S"}, "read"),
                                       None if oid is None else ObjectId(oid))
    assert box_overlap([20, 60, 80, 80], [20, 60, 80, 80]) == 1.0 and box_overlap([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    # the same bytes win; the same look at another resolution is taken too, a different picture isn't
    assert adopt(None) == "SAME"
    del objects["SAME"]
    # the crop the converter rendered again correlates too little for same_look (0.94 live)
    from beamer2slides.inverse import picture_look, same_look
    assert not same_look(picture_look(ours_out / "figures" / "f1.png"), picture_look(crop / "figures" / "f1.png"))
    assert adopt(None) == "CROP"  # (the closest box of the two that match)
    del objects["CROP"]
    assert adopt(None) == "LOOK"
    for oid in ("LOOK", "COPY", "INGROUP", "B2S"):
        del objects[oid]
    assert adopt(None) is None
    # far from the element's box: not the same unit
    objects["SAME"] = image_readback([400, 300, 520, 340], "c1")
    assert adopt(None) is None
    # a picture the deck put in place of the element's own object is found by id
    assert adopt("SAME") == "SAME"
    assert adopt("OTHER") is None


def test_a_new_slide_inherits_the_master_background(tmp_path: Path) -> None:
    """emit leaves the slides with the shared background inheriting the master (now often a plain
    ground colour, with the theme decoration on the layouts), so a slide sync creates must inherit
    it too: an explicit fill of its own differs from a fresh conversion and stops following the
    deck's theme."""
    s = sync_work.bare_sync()
    s.base = {"master_background": "color:#ffffff"}
    s.ours = {}
    s.ours_out = tmp_path
    s.urls = {str(tmp_path / "backgrounds" / "bg-3.png"): "https://content/3"}
    pres = Presentation(masters=[Page(pageProperties={"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}})])
    assert s.background_requests("S", "color:#ffffff", {}, pres, True) == []
    kept = s.background_requests("S", "color:#ffffff", {}, pres, False)  # an edited slide goes back to it
    assert jat(kept[0], "updatePageProperties", "fields") == "pageBackgroundFill.solidFill.color"
    own = s.background_requests("S", "color:#102030", {}, pres, True)
    assert jat(own[0], "updatePageProperties", "pageProperties", "pageBackgroundFill", "solidFill", "color") == \
        {"rgbColor": {"red": 16 / 255, "green": 32 / 255, "blue": 48 / 255}}
    picture = s.background_requests("S", "png:abc", {"background": "backgrounds/bg-3.png"}, pres, True)
    assert jat(picture[0], "updatePageProperties", "pageProperties", "pageBackgroundFill") == \
        {"stretchedPictureFill": {"contentUrl": "https://content/3"}}


def test_a_new_slide_lands_on_the_layout_of_its_background(tmp_path: Path) -> None:
    """The theme decoration now sits on the layouts, and backgrounds that don't show it got a copy
    of their layout without it. A new slide with a background picture of its own therefore goes on
    the layout of the converted slides with that background, not on the decorated one."""
    from beamer2slides.snapshot import background_key
    (tmp_path / "backgrounds").mkdir()
    for name, colour in (("bg-000.png", (250, 250, 250)), ("bg-003.png", (20, 20, 40)), ("bg-new.png", (7, 7, 7))):
        from PIL import Image
        Image.new("RGB", (16, 9), colour).save(tmp_path / "backgrounds" / name)

    def page(name: str) -> JsonObject:
        return {"background": f"backgrounds/{name}"}
    main, standout = (background_key(page(n), tmp_path) for n in ("bg-000.png", "bg-003.png"))
    s = sync_work.bare_sync()
    s.ours = {}
    s.ours_out = tmp_path
    s.base = {"master_background": main, "slides": [
        {"key": "a", "layout": "TITLE_ONLY", "background": main, "layoutObjectId": "L_main"},
        {"key": "b", "layout": "TITLE_ONLY", "background": standout, "layoutObjectId": "L_plain"},
        {"key": "c", "layout": "BLANK", "background": standout, "layoutObjectId": "L_blank"}]}
    names = {"L_main": "TITLE_ONLY", "L_plain": "Title Only (no theme)", "L_blank": "Blank (no theme)", "L_bare": "BLANK"}
    pages = [Page(objectId=oid, layoutProperties={"name": name}) for oid, name in names.items()]
    pres = Presentation(layouts=pages)
    layouts = {name: p for name, p in zip(names.values(), pages)}

    def pick(name: str, kind: str) -> str:
        layout = s.new_layout(page(name), kind, layouts, pres)
        assert layout is not None
        return object_id(layout)
    assert pick("bg-000.png", "TITLE_ONLY") == "L_main"       # the master background: the layout draws the decoration
    assert pick("bg-003.png", "TITLE_ONLY") == "L_plain"      # a standout frame: the copy without it
    assert pick("bg-003.png", "BLANK") == "L_blank"
    assert pick("bg-new.png", "TITLE_ONLY") == "L_main"       # unknown: the named layout, as before


def test_conversion_is_stable():
    """Converting the same PDF twice gives the same keys and hashes (a no-op sync sends nothing)."""
    import tempfile
    from beamer2slides.sync import build_ours_of
    pdf = DECKS / "sync_smoke_v1.pdf"
    if not pdf.exists():
        pytest.skip("build the test decks first (tests/decks/build.py sync_smoke_v1)")
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = build_ours_of(pdf, Path(a), {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)
        base: JsonObject = {"slides": [*first.slides]}
        second = build_ours_of(pdf, Path(b), base, "last", SLIDE_W, snapshot.NO_PICTURES)
    assert second.pairs == {j: j for j in range(len(first.slides))}
    assert [(s["key"], [(jat(e, "key"), jat(e, "ir_hash")) for e in jobjs(s, "elements")]) for s in second.slides] == \
        [(s["key"], [(jat(e, "key"), jat(e, "ir_hash")) for e in jobjs(s, "elements")]) for s in first.slides]


def test_a_stand_in_is_made_at_the_size_slides_keeps():
    """Slides stores a new shape at 3,000,000 EMU whatever size is asked for, and keeps the asked
    size in the scale; a copy of the stand-in is then scaled ABSOLUTE by box / STAND_IN, which is only
    the box when the stand-in's own size is what Slides kept. At 100 pt the copy was 2.36 times too
    big: a block's title bar came back 1649 pt wide on a 720 pt page (live, the front page's demo).
    (tests/slides_sim.py neither normalises sizes nor copies objects, so only this sees it.)"""
    from beamer2slides.sync import STAND_IN, stand_in_request
    size = jat(stand_in_request("b2s_x_k0_1aa", "s", "ROUND_2_SAME_RECTANGLE"), "createShape", "elementProperties", "size")
    assert jat(size, "width", "magnitude") == jat(size, "height", "magnitude") == 3_000_000
    assert jobj(size, "width").get("unit", "EMU") == "EMU"
    box_w = 698.0
    assert round(box_w / STAND_IN * 3_000_000 / 12700, 6) == box_w


def test_has_writes_of_reads_the_records_as_has_writes_reads_their_json():
    """`merge.has_writes_of` (sync's and the fuzz's question, on the typed plan) answers what
    `has_writes` answers of `merge_plan_json` of the same plan: every slide and unit kind, a
    background written or taken away, notes, and the live order."""
    k, o = merge.ElementKey("text/body/0"), merge.ObjectId("b2s_s000")
    none = merge.Overrides(text=None, text_style=None, shape_style=None, geometry=False)
    decisions = [merge.AdoptObject(key=k, object_id=o), merge.CreateUnit(key=k), merge.GoneUnit(key=k),
                 merge.DeleteUnit(key=k), merge.KeepRemoved(key=k, deck=("text",)), merge.KeptJoined(key=k),
                 merge.KeepUnit(key=k, source=("text",), deck=(), blind=None),
                 merge.Recreate(key=k, source=("text",), deck=(), overrides=none),
                 merge.AdoptUnit(key=k, source=("text",), deck=("text",), adopt=("text",)),
                 merge.MoveUnit(key=k, source=("position",), deck=(), delta=(0.0, 3.0))]
    s = merge.SlideKey("intro")

    def update(units: tuple[merge.PlannedUnit, ...], written: bool, background: str | None,
               notes: str | None) -> merge.UpdateSlide:
        return merge.UpdateSlide(key=s, ours=0, base=0, object_id=o, units=units, background_written=written,
                                 background=background, notes=notes)
    slides = [merge.CreateSlide(key=s, ours=0), merge.GoneSlide(key=s, ours=0, base=0),
              merge.KeepRemovedSlide(key=s, base=0, object_id=o), merge.DeleteSlide(key=s, base=0, object_id=o),
              merge.HoldSlide(key=s, ours=0, base=0, object_id=o)]
    slides += [update((merge.PlannedUnit(decision=d, base_members=(k,), ours_members=(k,)),), False, None, None)
               for d in decisions]
    slides += [update((), w, b, n) for w in (False, True) for b in (None, "", "bg.png") for n in (None, "", "said")]
    for p in slides:
        for order, live in (((), ()), (("a", "b"), ["a", "b"]), (("b", "a", "new:x"), ["a", "b"])):
            plan = merge.MergePlan(slides=(p,), order=order, report=merge.Report())
            assert merge.has_writes_of(plan, live) == merge.has_writes(merge.merge_plan_json(plan), live), (p, order)
