"""Offline tests of sync (docs/sync.md): identity, deck edit detection, the merge rules, diff3,
slide planning, and the request helpers. No Google calls."""

import copy
from pathlib import Path

import pytest

from beamer2slides import identity, merge
from beamer2slides.extract import frame_labels
from beamer2slides.sync import letterbox_fix, rename

DECKS = Path(__file__).resolve().parent / "decks" / "out"


# ---------------------------------------------------------------- helpers

def run(text, **kw):
    return {"text": text, "font": "CMSS10", "family": "sans", "size": 10.0, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False, "highlight": None, **kw}


def text_ir(text, bbox, eid="p0t1", role="body", **kw):
    return {"id": eid, "kind": "text", "role": role, "bbox": list(bbox), "panel": None, "code": False, "spans": [],
            "strokes": [], "paragraphs": [{
                "align": "left", "level": 0, "bullet": None, "size": 10.0, "text_x0": bbox[0], "tab_x0": None,
                "lines": [{"baseline": bbox[1] + 8 + 12 * k, "x0": bbox[0], "x1": bbox[2]}], "wrap_limit": None,
                "runs": [run(line, **kw)]} for k, line in enumerate(text.split("\n"))]}


def shape_ir(bbox, eid="p0s0", fill="#dddddd"):
    return {"id": eid, "kind": "shape", "role": "panel", "bbox": list(bbox), "shape": "RECTANGLE", "fill": fill, "flip": False,
            "radius": 0.0}


def readback(box, text=None, kind="shape", parent=None, title=None, style="s0", shape="h0", image=None, styles=None):
    x0, y0, x1, y1 = box
    out = {"kind": kind, "transform": [1.0, 0.0, 0.0, 1.0, x0, y0], "size": [x1 - x0, y1 - y0], "box": list(box),
           "parent_group": parent, "z": 0, "title": title, "description": None, "text": text,
           "text_styles": styles if styles is not None else [{"fontFamily": "Lato", "fontSize": 18.0}],
           "paragraph_styles": [{"alignment": "START"}], "text_style_hash": style, "shape_style": {"fill": {"color": "#dddddd", "alpha": 1.0}},
           "shape_style_hash": shape}
    if image:
        out["image"] = {"contentHash": image}
    return out


def entry(key, ir, oid=None, anchor=None):
    """A base element: IR hashes plus a read-back matching what the converter wrote (2x scale)."""
    h, fields = identity.ir_fields(ir, None, anchor)
    el = {"key": key, "id": ir["id"], "kind": ir["kind"], "role": ir.get("role"), "ir_hash": h, "fields": fields,
          "fingerprint": identity.fingerprint(ir, None, anchor), "anchor": anchor, "ir": ir}
    if oid:
        box = [2 * v for v in ir["bbox"]]
        text = merge.predicted_text(ir) if ir["kind"] == "text" else None
        el.update(objects=[oid], main=oid, readback={oid: readback(box, text)})
    return el


def ours_entry(key, ir, anchor=None):
    return {k: v for k, v in entry(key, ir, None, anchor).items()}


def base_slide(key, sid, elements, label=None, title=""):
    return {"key": key, "label": label, "title": title, "page": 0, "text": " ".join(e["fingerprint"]["text"] for e in elements),
            "layout": "TITLE_ONLY", "background": "color:#ffffff", "notes": "", "objectId": sid, "layoutObjectId": "L",
            "background_readback": {"state": "INHERIT"}, "notes_readback": "", "groups": [],
            "order": [e["main"] for e in elements if "main" in e], "elements": elements}


def live(slide):
    """The live slide exactly as the base recorded it."""
    return {"objectId": slide["objectId"], "layoutObjectId": "L", "background": slide["background_readback"], "notes": "",
            "notes_id": f"{slide['objectId']}_notes", "order": list(slide["order"]),
            "objects": {oid: copy.deepcopy(rb) for e in slide["elements"] for oid, rb in e["readback"].items()}}


def ours_of(slide):
    return {**{k: v for k, v in slide.items() if k not in ("objectId", "layoutObjectId", "background_readback",
                                                           "notes_readback", "groups", "order")},
            "elements": [{k: v for k, v in e.items() if k not in ("objects", "main", "readback")} for e in slide["elements"]]}


def three_slides():
    return many_slides(["intro", "results", "end"])


def many_slides(names):
    slides = []
    for n, name in enumerate(names):
        sid = f"b2s_s{n:03}"
        els = [entry("text/title/0", text_ir(name.title(), (10, 10, 100, 24), f"p{n}t0", "title"), f"{sid}_t0"),
               entry("text/body/0", text_ir(f"First point of {name}\nSecond point of {name}", (20, 60, 200, 90), f"p{n}t1"), f"{sid}_t1")]
        slides.append(base_slide(name, sid, els, label=name, title=name.title()))
    return {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": slides}


def triple(base):
    ours = {"slides": [ours_of(s) for s in base["slides"]], "pairs": {j: j for j in range(len(base["slides"]))}}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    return ours, theirs


def unit(mplan, slide, key):
    return next(u for p in mplan["slides"] if p["key"] == slide for u in p.get("units", []) if u["key"] == key)


# ---------------------------------------------------------------- identity

def test_frame_labels_from_named_destinations():
    dests = [("Doc-Start", 0), ("Navigation1", 0), ("Navigation3", 2), ("last", 2), ("last<1>", 2), ("page.3", 2),
             ("steps", 3), ("steps<1>", 3), ("steps<2>", 4), ("steps<3>", 5), ("gone<1>", -1), ("gone", -1)]
    assert frame_labels(dests) == {2: "last", 3: "steps", 4: "steps", 5: "steps"}


def info(title, text, label=None, page=0):
    return {"label": label, "title": title, "text": text, "page": page}


def test_inserted_frame_shifts_no_keys():
    base = [info("Intro", "why we do this"), info("Results", "numbers went up a lot"), info("End", "thanks for listening")]
    keys = identity.slide_keys(base)
    assert keys == ["title:intro#1", "title:results#1", "title:end#1"]
    ours = [base[0], info("Method", "how we measured the numbers"), base[1], base[2]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 0, 2: 1, 3: 2}
    assert got == ["title:intro#1", "title:method#1", "title:results#1", "title:end#1"]


def test_renamed_title_keeps_key():
    base = [info("Intro", "why we do this and what it costs"), info("Results", "numbers went up a lot this year")]
    keys = identity.slide_keys(base)
    ours = [base[0], info("Findings", "numbers went up a lot this year")]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 0, 1: 1} and got[1] == "title:results#1"


def test_repeated_titles_get_occurrences():
    keys = identity.slide_keys([info("Example", "a"), info("Example", "b"), info("", "c", page=2)])
    assert keys == ["title:example#1", "title:example#2", "page:3"]


def test_labelled_frames_match_wherever_they_moved():
    base = [info("A", "alpha text here", "a"), info("B", "beta text here", "b"), info("C", "gamma text here", "c")]
    keys = identity.slide_keys(base)
    assert keys == ["a", "b", "c"]
    ours = [info("C renamed", "completely different words", "c"), base[0], base[1]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 2, 1: 0, 2: 1} and got == ["c", "a", "b"]


def test_labelled_and_unlabelled_mixed():
    base = [info("A", "alpha words here", "a"), info("Plain", "some plain words"), info("B", "beta words here", "b")]
    keys = identity.slide_keys(base)
    ours = [info("B", "beta words here", "b"), info("Plain", "some plain words, edited"), info("New", "brand new", "new")]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 2, 1: 1}
    assert got == ["b", "title:plain#1", "new"]
    # A label neither side knows on the other is a label renamed, and then the words decide: the
    # deck's slide keeps its identity instead of coming back beside itself (tests/test_label_moves.py).
    got, pairs = identity.inherit_slide_keys([info("X", "same", "x")], ["x"], [info("X", "same", "y")])
    assert pairs == {0: 0} and got == ["x"]


def test_element_keys_follow_content():
    els = [text_ir("Title", (10, 10, 100, 24), "p0t0", "title"), text_ir("First paragraph of text", (20, 60, 200, 70), "p0t1"),
           text_ir("Second paragraph of text", (20, 80, 200, 90), "p0t2")]
    keys, fps = identity.slide_element_keys(els, None)
    assert keys == ["text/title/0", "text/body/0", "text/body/1"]
    base = [{"key": k, "kind": e["kind"], "role": e["role"], "fingerprint": f} for k, e, f in zip(keys, els, fps)]
    # a paragraph inserted before the others: they keep their keys, the new one gets a fresh ordinal
    ours = [els[0], text_ir("A brand new opening remark", (20, 45, 200, 55), "p0t1"),
            {**els[1], "id": "p0t2"}, {**els[2], "id": "p0t3"}]
    got, _ = identity.slide_element_keys(ours, None, base)
    assert got == ["text/title/0", "text/body/2", "text/body/0", "text/body/1"]


def test_anchored_elements_take_their_anchors_key():
    text = text_ir("A formula here and more words", (20, 60, 200, 70), "p0t1")
    pic = {"id": "p0h0", "kind": "image", "role": "math", "bbox": [80, 60, 100, 70], "anchor": "p0t1"}
    keys, fps = identity.slide_element_keys([text, pic], None)
    assert keys == ["text/body/0", "image/math/0"] and fps[1]["anchor"] == "text/body/0"


def test_ir_hash_ignores_ids_and_page_numbers():
    a = text_ir("Go to results", (20, 60, 200, 70), "p3t1", link="#page=7")
    b = {**text_ir("Go to results", (20, 60, 200, 70), "p4t1", link="#page=8"), "spans": ["p4s1", "p4s2"]}
    ha, _ = identity.ir_fields(a, None, None, lambda page: "results")
    hb, _ = identity.ir_fields(b, None, None, lambda page: "results")
    assert ha == hb
    hc, _ = identity.ir_fields(b, None, None, lambda page: "method")
    assert hc != ha
    # render output (how a bare image reached its file, its pixel size) is no source change either
    img = {"id": "p1i0", "kind": "image", "role": "figure", "bbox": [10, 10, 60, 40], "file": "figures/p1i0.png"}
    assert identity.ir_fields(img)[0] == identity.ir_fields({**img, "picture": "raw", "px": [800, 480]})[0]


def test_source_changes_by_field():
    old = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70)))
    reworded = entry("text/body/0", text_ir("Authors and their AI keep writing", (20, 60, 160, 70)))
    moved = entry("text/body/0", text_ir("Authors keep writing", (20, 90, 120, 100)))
    red = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70), color="#ff0000"))
    assert identity.source_changes(old, old) == set()
    assert identity.source_changes(old, reworded) == {"text", "size"}
    assert identity.source_changes(old, moved) == {"position"}
    assert identity.source_changes(old, red) == {"style"}


# ---------------------------------------------------------------- deck edits

def test_object_changes():
    b = readback([10, 10, 110, 30], "Hello\n")
    assert merge.object_changes(b, copy.deepcopy(b)) == set()
    moved = {**b, "box": [20, 10, 120, 30], "transform": [1, 0, 0, 1, 20, 10]}
    assert merge.object_changes(b, moved) == {"geometry"}
    assert merge.object_changes(b, {**b, "box": [10.03, 10, 110.03, 30]}) == set()  # read-back noise
    assert merge.object_changes(b, {**b, "text": "Hello world\n"}) == {"text"}
    assert merge.object_changes(b, {**b, "text_style_hash": "s1"}) == {"text_style"}
    assert merge.object_changes(b, {**b, "shape_style_hash": "h1"}) == {"shape_style"}
    assert merge.object_changes(b, {**b, "parent_group": "g"}) == {"group"}


def test_deck_edits_and_user_objects():
    base = three_slides()
    s = base["slides"][0]
    theirs = live(s)
    assert merge.deck_edits(s["elements"][1], theirs) == {}
    del theirs["objects"]["b2s_s000_t1"]
    theirs["objects"]["copy"] = readback([0, 0, 5, 5], "x", title="b2s:intro/text/title/0")
    theirs["objects"]["mine"] = readback([0, 0, 5, 5], "y")
    assert merge.deck_edits(s["elements"][1], theirs) == {"deleted": ["b2s_s000_t1"]}
    assert merge.user_objects(s, theirs) == [{"objectId": "copy", "copy_of": "intro/text/title/0"},
                                            {"objectId": "mine", "copy_of": None}]


def test_ungrouped_unit_is_a_group_edit_not_a_part_deletion():
    """Ungrouping a formula's group in Slides removes the `_g` object: a group edit (the unit is
    rebuilt ungrouped), not a part deletion (which kept the whole unit as a conflict)."""
    base = three_slides()
    el = base["slides"][0]["elements"][1]
    el["objects"].append("b2s_s000_t1_g")
    el["readback"]["b2s_s000_t1_g"] = {**readback([20, 60, 200, 90]), "kind": "elementGroup"}
    el["readback"]["b2s_s000_t1"]["parent_group"] = "b2s_s000_t1_g"
    theirs = live(base["slides"][0])
    del theirs["objects"]["b2s_s000_t1_g"]
    theirs["objects"]["b2s_s000_t1"]["parent_group"] = None
    assert {k: sorted(v) for k, v in merge.deck_edits(el, theirs).items()} == {"group": ["b2s_s000_t1", "b2s_s000_t1_g"]}
    ours, live_deck = triple(base)
    live_deck["slides"][0] = theirs
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    u = unit(merge.plan_merge(base, ours, live_deck), "intro", "text/body/0")
    assert u["action"] == "recreate"


def test_uniform_style_changes():
    base = [{"fontFamily": "Lato", "fontSize": 18.0}, {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    red = [{**s, "foregroundColor": "#cc0000"} for s in base]
    assert merge.uniform_changes(base, base) == {}
    assert merge.uniform_changes(base, red) == {"foregroundColor": "#cc0000"}
    one_word = base + [{"fontFamily": "Lato", "fontSize": 18.0, "italic": True}]
    assert merge.uniform_changes(base, one_word) is None


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


def apply_text_requests(text: str, reqs: list[dict]) -> str:
    """The requests applied the way Slides applies them - including its refusal to touch the
    newline the text ends on, which it reads back but does not count in the length it will accept
    (`merge.text_edit_requests`). An applier that quietly clips instead would have let a batch
    through that the API refuses, and with it the whole sync."""
    units = list(text)  # (ASCII in these tests: UTF-16 indices are character indices)
    length = len(units) - 1 if text.endswith("\n") else len(units)
    for r in reqs:
        if "deleteText" in r:
            rng = r["deleteText"]["textRange"]
            assert rng["endIndex"] <= length, (
                f"Invalid deleteText: the end index ({rng['endIndex']}) should not be greater "
                f"than the existing text length ({length}).")
            del units[rng["startIndex"]:rng["endIndex"]]
        else:
            i = r["insertText"]["insertionIndex"]
            assert i <= length, f"Invalid insertText: insertion index {i} past the text ({length})"
            units[i:i] = list(r["insertText"]["text"])
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
def test_text_edit_requests(current, target):
    assert apply_text_requests(current, merge.text_edit_requests("t", current, target)) == target


def test_text_edit_requests_count_utf16():
    reqs = merge.text_edit_requests("t", "\U0001d465 is x\n", "\U0001d465 is y\n")
    assert reqs[0]["deleteText"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 7}


# ---------------------------------------------------------------- merge rules

def test_no_changes_plans_no_writes():
    base = three_slides()
    ours, theirs = triple(base)
    mplan = merge.plan_merge(base, ours, theirs)
    assert all(u["action"] == "keep" for p in mplan["slides"] for u in p["units"])
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])
    assert mplan["report"]["applied"] == [] and mplan["report"]["conflicts"] == []


def edit_text(slide, oid, text):
    slide["objects"][oid]["text"] = text


def test_source_change_applied_when_deck_untouched():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {}
    assert merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_deck_edit_kept_when_source_unchanged():
    base = three_slides()
    ours, theirs = triple(base)
    edit_text(theirs["slides"][1], "b2s_s001_t1", "Something else entirely\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    assert mplan["report"]["overrides"] == [{"slide": "results", "element": "text/body/0", "fields": ["text"]}]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_source_text_and_deck_geometry_recreate_at_deck_place():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 60, 200, 90), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["box"] = [v + 30 for v in obj["box"]]
    obj["transform"][4] += 30
    obj["transform"][5] += 30
    u = unit(merge.plan_merge(base, ours, theirs), "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {"geometry": {"mode": "delta"}}


def test_both_moved_deck_position_wins():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 100, 200, 130), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["box"] = [v + 30 for v in obj["box"]]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["overrides"] == {"geometry": {"mode": "theirs"}}
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["geometry"]


def test_both_text_clean_diff3():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point of the intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "First idea of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and set(u["overrides"]) == {"text"}
    assert not mplan["report"]["conflicts"]


def test_both_text_overlapping_is_a_conflict():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "keep"
    (c,) = mplan["report"]["conflicts"]
    assert c["field"] == "text" and c["resolution"] == "deck kept" and "Best point" in c["theirs"]


def test_a_conflict_in_one_bullet_does_not_cost_the_others():
    """Found by the stress deck (churn): the source rewrote three bullets of a box while the person
    had changed one word in the third. The box merged as one run of words, so the clash in the
    third decided all three and the source's first two were dropped; sync then reported `deck kept`
    and wrote nothing. Each paragraph is now merged on its own."""
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        "Every timing comes from this deck\nSeconds are rounded, milliseconds dropped", (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1",
              "First point of intro\nThe numbers are rounded to full seconds\n")
    mplan = merge.plan_merge(base, ours, theirs)
    (c,) = mplan["report"]["conflicts"]
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
        "Timings are measured on the deck\nThe numbers are rounded to full seconds\n")
    assert safe and len(conflicts) == 1
    assert merged == "Every timing comes from this deck\nThe numbers are rounded to full seconds\n"
    # the word-level merge on its own writes a line neither side ever wrote
    spliced, _ = merge.diff3("The numbers are rounded to whole seconds",
                             "Seconds are rounded, milliseconds are dropped",
                             "The numbers are rounded to full seconds")
    assert spliced == "Seconds are rounded, milliseconds full dropped"


def test_bullets_the_source_added_fall_back_to_the_word_level_merge():
    """Paragraphs only line up when there are the same number of them; otherwise the old
    whole-box merge decides, and a clash there still keeps the deck's text."""
    args = ("one\ntwo\n", "one\nfirst\ntwo\n", "one\ntwo edited\n")
    merged, conflicts, safe = merge.text_merge(*args)
    assert (merged, conflicts) == merge.diff3(*args) and safe is (not conflicts)


def test_same_text_on_both_sides_converges():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Main point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["report"]["converged"] == [{"slide": "intro", "element": "text/body/0", "field": "text",
                                             "value": "Main point of intro\nSecond point of intro\n"}]
    # (nothing to write: the deck already shows it; the base adopts the deck's text)
    assert unit(mplan, "intro", "text/body/0")["action"] == "adopt"
    assert not mplan["report"]["applied"] and not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


# ------------------------------------------------- taking the source's version of one conflict

def take_base(text="One from the base\nTwo from the base\nThree from the base"):
    """`intro` with a body box of however many paragraphs the case wants."""
    base = three_slides()
    slide = base["slides"][0]
    slide["elements"][1] = entry("text/body/0", text_ir(text, (20, 60, 200, 110), "p0t1"), "b2s_s000_t1")
    slide["text"] = " ".join(e["fingerprint"]["text"] for e in slide["elements"])
    slide["order"] = [e["main"] for e in slide["elements"] if "main" in e]
    return base


def take_case(base, source, deck):
    """The source rewrites intro's body to `source`, a person in the deck to `deck` (which ends on
    the newline Slides keeps)."""
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(source, (20, 60, 200, 110), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", deck + "\n")
    return ours, theirs


def only_conflict(mplan, field="text"):
    (c,) = [x for x in mplan["report"]["conflicts"] if x["field"] == field]
    return c


def test_a_conflict_id_names_those_two_changes_and_nothing_else():
    """The id is a hash of the spot and the three versions, so it is the same on every run while
    the same two changes stand against each other, and a different one the moment either moves.
    That is the whole safety of `--take-source`: an id copied out of an older report cannot land
    on a disagreement that has since become another one."""
    base = take_base()
    args = ("One from the source\nTwo from the source\nThree from the base",
            "One from the base\nTwo from the deck\nThree from the base")
    first = only_conflict(merge.plan_merge(base, *take_case(base, *args)))
    again = only_conflict(merge.plan_merge(base, *take_case(base, *args)))
    assert first["id"] == again["id"] and len(first["id"]) == 8
    moved = only_conflict(merge.plan_merge(base, *take_case(
        base, args[0], "One from the base\nTwo from the deck, reworded\nThree from the base")))
    assert moved["id"] != first["id"]


def test_take_source_writes_that_paragraph_and_leaves_the_others_alone():
    """Two paragraphs clash; the person reads the report and says the source is right about one of
    them. That one is written, the other stays as the deck has it, and the paragraph only the
    source touched was never in question."""
    base = take_base()
    args = ("One from the source\nTwo from the source\nThree from the source",
            "One from the base\nTwo from the deck\nThree from the deck")
    mplan = merge.plan_merge(base, *take_case(base, *args))
    two = next(c for c in mplan["report"]["conflicts"] if c["ours"] == "Two from the source")
    ours, theirs = take_case(base, *args)
    mplan = merge.plan_merge(base, ours, theirs, take_source=[two["id"]])
    ov = unit(mplan, "intro", "text/body/0")["overrides"]["text"]
    assert ov["take"] == [1]
    # What sync writes: `override_requests` re-merges the deck's text back onto the element it has
    # just recreated, which holds the source's words, with the same list of paragraphs.
    current = merge.predicted_text(ours["slides"][0]["elements"][1]["ir"])
    written, _, safe = merge.text_merge(ov["base"], current, ov["theirs"], ov["take"])
    assert safe and written == "One from the source\nTwo from the source\nThree from the deck\n"
    resolutions = {c["ours"]: c["resolution"] for c in mplan["report"]["conflicts"]}
    assert resolutions == {"Two from the source": merge.TAKEN_SAYS, "Three from the source": "deck kept"}


def test_what_take_source_wrote_over_is_kept_verbatim_in_the_report():
    """A person's words are about to stop existing anywhere: the report is the way back, and it has
    to hold them, because a minute from now nothing else will."""
    base = take_base()
    args = ("One from the source\nTwo from the source\nThree from the base",
            "One from the base\nTwo from the deck\nThree from the base")
    cid = only_conflict(merge.plan_merge(base, *take_case(base, *args)))["id"]
    report = merge.plan_merge(base, *take_case(base, *args), take_source=[cid])["report"]
    assert report["resolved"] == [{"id": cid, "slide": "intro", "element": "text/body/0",
                                   "field": "text", "was": "Two from the deck"}]


def test_taking_a_whole_box_makes_the_decks_text_no_override_at_all():
    """Where nothing of the source survived the merge the conflict is the whole box, and taking it
    writes the source's text entire - so the deck's edit is not an override any more and the report
    must not promise it was kept."""
    base = take_base("One from the base\nTwo from the base")
    args = ("One from the source\nTwo from the source", "One from the deck\nTwo from the deck")
    cid = only_conflict(merge.plan_merge(base, *take_case(base, *args)))["id"]
    mplan = merge.plan_merge(base, *take_case(base, *args), take_source=[cid])
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {}
    assert mplan["report"]["overrides"] == []
    assert mplan["report"]["resolved"][0]["was"] == "One from the deck\nTwo from the deck\n"


def test_an_id_that_matches_nothing_settles_nothing_and_says_so():
    """A report a version old cannot reach today's conflict. Nothing is written on that account,
    and the run says which id found no home rather than dropping it."""
    base = take_base()
    mplan = merge.plan_merge(base, *take_case(
        base, "One from the source\nTwo from the source\nThree from the base",
        "One from the base\nTwo from the deck\nThree from the base"), take_source=["0badcafe"])
    assert only_conflict(mplan)["resolution"] == "deck kept"
    assert mplan["report"]["resolved"] == []
    (w,) = mplan["report"]["warnings"]
    assert w.startswith("--take-source 0badcafe: no conflict in this sync has that id")


def test_a_geometry_conflict_can_be_settled_for_the_source():
    """Both sides moved the element: `--take-source` puts it back where the source draws it."""
    base = three_slides()

    def case():
        ours, theirs = triple(base)
        ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
            "First point of intro\nSecond point, changed", (20, 100, 200, 130), "p0t1"))
        obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
        obj["box"] = [v + 30 for v in obj["box"]]
        return ours, theirs

    cid = only_conflict(merge.plan_merge(base, *case()), "geometry")["id"]
    mplan = merge.plan_merge(base, *case(), take_source=[cid])
    u = unit(mplan, "intro", "text/body/0")
    assert "geometry" not in u["overrides"] and u["action"] == "recreate"
    assert only_conflict(mplan, "geometry")["resolution"] == merge.TAKEN_SAYS


def test_a_background_and_a_note_can_be_settled_for_the_source():
    base = three_slides()

    def case():
        ours, theirs = triple(base)
        ours["slides"][0]["background"] = "color:#eeeeee"
        ours["slides"][0]["notes"] = "Say the numbers are new"
        theirs["slides"][0]["background"] = {"state": "RENDERED", "solidFill": {"color": "#fff2cc"}}
        theirs["slides"][0]["notes"] = "Mention the deadline"
        return ours, theirs

    plain = merge.plan_merge(base, *case())
    fields = {c["field"]: c for c in plain["report"]["conflicts"]}
    assert set(fields) == {"background", "notes"} and all(c["takeable"] for c in fields.values())
    mplan = merge.plan_merge(base, *case(), take_source=[c["id"] for c in fields.values()])
    plan = next(p for p in mplan["slides"] if p["key"] == "intro")
    assert plan["background"] == "color:#eeeeee" and plan["notes"] == "Say the numbers are new"
    assert {r["field"] for r in mplan["report"]["resolved"]} == {"background", "notes"}


def test_existence_is_never_settled_for_the_source():
    """`--take-source` decides what something *says*. Whether it exists at all is another question:
    overwriting text leaves the deck's version in the report as the way back, deleting leaves
    nothing, and a deletion nobody can undo is what `--force-rebuild` is for. So a `removed`, a
    `deleted` or a whole slide the deck kept carries an id to talk about and refuses to be taken."""
    base = three_slides()
    ours, theirs = triple(base)
    del ours["slides"][2]["elements"][1]
    edit_text(theirs["slides"][2], "b2s_s002_t1", "Edited in the deck\n")
    mplan = merge.plan_merge(base, ours, theirs)
    (c,) = mplan["report"]["conflicts"]
    assert c["field"] == "removed" and "takeable" not in c and c["id"]
    # and naming it anyway settles nothing at all
    ours, theirs = triple(base)
    del ours["slides"][2]["elements"][1]
    edit_text(theirs["slides"][2], "b2s_s002_t1", "Edited in the deck\n")
    again = merge.plan_merge(base, ours, theirs, take_source=[c["id"]])
    assert unit(again, "end", "text/body/0")["action"] == "keep"
    assert again["report"]["resolved"] == [] and len(again["report"]["warnings"]) == 1


def test_every_takeable_conflict_is_about_what_something_says():
    """The line is drawn once, in `merge.TAKEABLE_FIELDS`, and a field added to a report later has
    to be put on one side of it on purpose."""
    assert set(merge.TAKEABLE_FIELDS).isdisjoint({"removed", "deleted", "part_deleted", "slide", "label"})


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


def table_ir(rows, bbox=(20, 60, 200, 120), eid="p1tab0"):
    return {"id": eid, "kind": "table", "role": "table", "bbox": list(bbox), "frame": list(bbox), "size": 10.0,
            "row_baselines": [bbox[1] + 12 * k for k in range(len(rows))], "row_heights": [12.0] * len(rows),
            "columns": [{"x0": bbox[0] + 60 * c, "x1": bbox[0] + 60 * (c + 1), "align": "left"} for c in range(len(rows[0]))],
            "bounds": list(bbox), "cells": [[[run(cell)] for cell in row] for row in rows], "merges": [], "rules": [],
            "borders": [], "fills": [], "spans": []}


def table_slides(rows):
    """three_slides with a table on the results slide (element key table/table/0)."""
    base = three_slides()
    el = entry("table/table/0", table_ir(rows), "b2s_s001_tab0")
    rb = el["readback"]["b2s_s001_tab0"]
    rb["kind"], rb["table"] = "table", [len(rows), len(rows[0])]
    rb["text"] = identity.plain_text(el["ir"])
    base["slides"][1]["elements"].append(el)
    base["slides"][1]["order"].append("b2s_s001_tab0")
    return base


ROWS = [["Scenario", "Kept", "Time"], ["Disjoint", "100%", "3.9 s"], ["Conflicts", "92%", "6.0 s"]]


def test_table_cells_edited_on_both_sides_merge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"  # the source says another number
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nConflicts\t92%\t6.2 s")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "table/table/0")
    assert u["action"] == "recreate" and set(u["overrides"]) == {"text"}
    assert u["overrides"]["text"]["table"] and u["overrides"]["text"]["dims"] == [3, 3]
    assert not mplan["report"]["conflicts"]
    # the deck's cell wins where it was edited, the source's where it was
    cells, converged = merge.table_merge(u["overrides"]["text"]["base"], identity.plain_text(table_ir(changed)),
                                         u["overrides"]["text"]["theirs"], [3, 3], [3, 3])
    assert [cells[1][1], cells[2][2]] == ["98%", "6.2 s"] and not converged


def test_the_same_cell_edited_on_both_sides_is_a_conflict():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[2][0] = "Clashes"  # the same word the deck renamed, to something else
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nDisputes\t92%\t6.0 s")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    (c,) = mplan["report"]["conflicts"]
    assert c["field"] == "text" and c["resolution"] == "deck kept"


def test_a_row_added_in_the_deck_keeps_the_table():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    obj = theirs["slides"][1]["objects"]["b2s_s001_tab0"]
    obj["text"] += "\nExtra\t0%\t0.0 s"
    obj["table"] = [4, 3]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["text"]


def test_table_cells_the_deck_already_shows_converge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", identity.plain_text(table_ir(changed)))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "adopt"
    assert [c["field"] for c in mplan["report"]["converged"]] == ["text"]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_table_cell_edit_requests_carry_the_cell():
    reqs = merge.text_edit_requests("tab", "6.0 s\n", "6.2 s\n", {"rowIndex": 2, "columnIndex": 2})
    assert all(next(iter(r.values()))["cellLocation"] == {"rowIndex": 2, "columnIndex": 2} for r in reqs)
    assert apply_text_requests("6.0 s\n", reqs) == "6.2 s\n"


def test_words_of_one_cell_merge_like_prose():
    b, o, t = "Conflicts\t92%\t6.0 s", "Conflicts\t92%\t6.0 seconds", "Conflicts\t94%\t6.0 s"
    cells, converged = merge.table_merge(b, o, t, [1, 3], [1, 3])
    assert cells == [["Conflicts", "94%", "6.0 seconds"]] and not converged


def test_a_cell_with_a_line_break_is_not_merged():
    assert merge.table_grid("a\tb\nc\td\te", [2, 2]) is None
    assert merge.table_grid("a\tb\nc\td", [2, 2]) == [["a", "b"], ["c", "d"]]


def test_changed_in_source_deleted_in_deck():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("Other words", (20, 60, 200, 90), "p2t1"))
    del theirs["slides"][2]["objects"]["b2s_s002_t1"]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["resolution"] == "kept deleted"


def test_removed_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    del ours["slides"][2]["elements"][1]
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "delete"
    edit_text(theirs["slides"][2], "b2s_s002_t1", "Edited in the deck\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["field"] == "removed"


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
    from beamer2slides.sync import Sync
    base = three_slides()
    ours, theirs = triple(base)
    del ours["slides"][2]["elements"][1]                  # the source no longer draws it
    theirs["slides"][2]["objects"]["b2s_s002_t1"]["parent_group"] = "user_g"   # ... the person grouped it
    theirs["slides"][2]["objects"]["user_g"] = readback([0, 0, 400, 200], kind="elementGroup")
    theirs["slides"][2]["objects"]["user_g"]["children"] = ["b2s_s002_t1"]
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "end", "text/body/0")
    assert u["action"] == "keep" and u["removed"] is True
    assert mplan["report"]["conflicts"][0]["field"] == "removed"

    s = Sync.__new__(Sync)                                 # the base such a sync leaves behind
    s.base, s.ours, s.created, s.final_revision = base, {**ours, "source": Path("new.pdf")}, theirs, "r2"
    work = [{"plan": p, "sid": p["objectId"], "objects": {}, "new_oid": {}, "groups": [], "doomed": set()}
            for p in mplan["slides"]]
    nb = s.new_base({"plan": mplan, "work": {"slides": work}, "theirs": theirs})
    el = next(e for e in nb["slides"][2]["elements"] if e["key"] == "text/body/0")
    assert el["removed"] is True

    # the person's group is gone (its other child was deleted), so the deck says the base's own
    # words again - and the element is still theirs, still kept, still in the report.
    theirs["slides"][2]["objects"].pop("user_g")
    theirs["slides"][2]["objects"]["b2s_s002_t1"]["parent_group"] = None
    again = merge.plan_merge(nb, ours, theirs)
    assert unit(again, "end", "text/body/0")["action"] == "keep"
    assert [c["field"] for c in again["report"]["conflicts"]] == ["removed"]
    assert again["report"]["conflicts"][0]["resolution"] == "kept (the deck's own since the source dropped it)"
    assert not merge.has_writes(again, [x["objectId"] for x in theirs["slides"]])
    # ... until they take it out of the deck themselves, and then it is gone for good
    theirs["slides"][2]["objects"].pop("b2s_s002_t1")
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
    from beamer2slides.sync import Sync
    base = three_slides()
    end = base["slides"][2]
    pic = {"id": "p2m0", "kind": "image", "role": "icon", "bbox": [16, 62, 24, 70], "file": None}
    end["elements"].append(entry("image/icon/0", pic, "b2s_s002_m0", anchor="text/body/0"))
    ours, theirs = triple(base)
    theirs["slides"][2]["objects"]["b2s_s002_t1"]["text"] = "the person typed this\n"   # ... so it is kept
    # The source drops that box and puts another one on the slide, whose icon inherits the key.
    other = text_ir("A line the source adds", (20, 120, 200, 140), "p2t9")
    ours["slides"][2]["elements"] = [ours["slides"][2]["elements"][0], ours_entry("text/body/1", other),
                                     ours_entry("image/icon/0", {**pic, "id": "p2m9", "bbox": [16, 122, 24, 130]},
                                                anchor="text/body/1")]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["removed"] is True
    assert unit(mplan, "end", "text/body/1")["action"] == "create"

    s = Sync.__new__(Sync)
    s.base, s.ours, s.created, s.final_revision = base, {**ours, "source": Path("new.pdf")}, theirs, "r2"
    work = [{"plan": p, "sid": p["objectId"], "objects": {}, "new_oid": {2: "b2s_s002_m9", 1: "b2s_s002_t9"},
             "groups": [], "doomed": set()} for p in mplan["slides"]]
    written = s.new_base({"plan": mplan, "work": {"slides": work}, "theirs": theirs})["slides"][2]["elements"]
    keys = [e["key"] for e in written]
    assert len(keys) == len(set(keys)), "the slide answers to each of its keys once"
    assert [e["key"] for e in written if e.get("removed")] == ["text/body/0", "image/icon/0~2"]
    assert next(e for e in written if e["key"] == "image/icon/0~2")["anchor"] == "text/body/0"
    fresh = next(e for e in written if e["key"] == "image/icon/0")
    assert fresh["anchor"] == "text/body/1" and not fresh.get("removed")
    # ... and each icon is a member of its own unit, which is what reading the base by key lost.
    units = merge.units(written)
    assert [m["key"] for m in units["text/body/0"]] == ["text/body/0", "image/icon/0~2"]
    assert [m["key"] for m in units["text/body/1"]] == ["text/body/1", "image/icon/0"]


def test_removed_text_living_on_in_a_conflict_is_kept():
    base = three_slides()
    extra = entry("text/body/1", text_ir("A closing remark", (20, 120, 200, 130), "p0t2"), "b2s_s000_t2")
    base["slides"][0]["elements"].append(extra)
    base["slides"][0]["order"].append("b2s_s000_t2")
    ours, theirs = triple(base)
    # the source joins the remark into the list and rewords the first point; the deck rewords it too
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        "Main point of intro\nSecond point of intro\nA closing remark", (20, 60, 200, 130), "p0t1"))
    del ours["slides"][0]["elements"][2]
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/1")["action"] == "keep"
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_added_in_source_and_moved_only_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"].append(ours_entry("text/body/1", text_ir("New remark", (20, 120, 200, 130), "p1t2")))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/1")["action"] == "create"
    # moved in the source, text edited in the deck: the deck object moves
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1"))
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def anchored_picture_base():
    """`results` with an inline formula picture anchored to its body text."""
    base = three_slides()
    pic = {"id": "p1m0", "kind": "image", "role": "math", "bbox": [120, 62, 140, 74], "file": None}
    base["slides"][1]["elements"].append(entry("image/math/0", pic, "b2s_s001_m0", anchor="text/body/0"))
    return base, pic


def test_member_moved_alone_is_recreated_not_moved():
    """Found by the offline fuzz (tools/fuzz_sync.py, seeds 252 and 430): the source re-placed the
    inline formula picture inside its line while the deck edited that paragraph. Sync writes a
    `move` by moving the unit's top object, so a step that fits only one member cannot be written -
    it used to become a move of [0, 0] the report still called applied (nothing moved, no conflict
    raised). Such a unit is recreated instead."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("image/math/0", {**pic, "bbox": [128, 62, 148, 74]}, "text/body/0")
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "text/body/0")
    assert u["action"] == "recreate" and "delta" not in u
    assert not [a for a in mplan["report"]["applied"] if a.get("how") == "deck object moved"]


def test_whole_unit_moved_still_moves_the_deck_objects():
    """The counterpart: every member moved by the same step, so one move carries the unit."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1"))
    ours["slides"][1]["elements"][2] = ours_entry("image/math/0", {**pic, "bbox": [120, 82, 140, 94]}, "text/body/0")
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def move_object(slide, oid, dx, dy):
    rb = slide["objects"][oid]
    rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
    rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]


def test_picture_the_deck_moved_inside_its_unit_is_kept_not_recreated():
    """Found by the offline fuzz (tools/fuzz_sync.py, seed 720): the person dragged the formula
    picture inside its line while the source reworded that paragraph. Sync re-applies a geometry
    override by transforming the unit's *top* object, so a member that moved on its own would land
    back at the converter's box - silently, since the report listed the geometry as an override.
    The unit is kept as the deck has it, and the clash is reported."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1"))
    move_object(theirs["slides"][1], "b2s_s001_m0", 0, 40)
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    clash, = [c for c in mplan["report"]["conflicts"] if c["field"] == "geometry"]
    assert clash["element"] == "text/body/0" and "on its own" in clash["resolution"]


def test_unit_the_deck_moved_as_a_whole_is_still_recreated():
    """The counterpart: the person moved every object of the unit by the same step (they moved the
    group), so the top's transform carries the whole unit and the source's rewording goes in."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1"))
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        move_object(theirs["slides"][1], oid, 0, 40)
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"]["geometry"] == {"mode": "delta"}


def geometry_override_requests(tops):
    """What `Sync.override_requests` writes for the unit of `test_unit_the_deck_moved_as_a_whole...`,
    whose objects the deck moved 40 pt down and whose text the source reworded, once sync has
    recreated it as `new_t1` (+ `new_m0`) with `tops` saying what its outermost object is."""
    from beamer2slides.sync import Sync
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1"))
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        move_object(theirs["slides"][1], oid, 0, 40)
    mplan = merge.plan_merge(base, ours, theirs)
    p = next(p for p in mplan["slides"] if p["key"] == "results")
    assert unit(mplan, "results", "text/body/0")["overrides"] == {"geometry": {"mode": "delta"}}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours, sync.warnings = base, ours, []
    work = {"slides": [{"plan": p, "new_oid": {1: "new_t1", 2: "new_m0"}, "tops": dict(tops)}]}
    now = {"slides": [{"objectId": "b2s_s001", "objects": {
        oid: readback([40, 120, 400, 180]) for oid in ("new_t1", "new_m0", *tops.values())}}]}
    return sync.override_requests(work, theirs, now)


def test_a_moved_unit_with_no_group_is_moved_member_by_member():
    """A geometry override is written as one RELATIVE transform on the unit's *top* object, which
    for a group carries its children. When the person has taken the group apart, a recreation does
    not put it back: the top is the main object alone, and the anchored picture needs the step
    itself or it stays at the converter's box while the report says the move was kept (live fuzz
    seed 903, chain depth 8: a formula picture 15 pt above the line it belongs to). `merge.
    geometry_writable` has already asked that one step fits every member, so it fits each of them."""
    from beamer2slides.sync import EMU_PER_PT
    moves = {r["updatePageElementTransform"]["objectId"]: r["updatePageElementTransform"]["transform"]
             for r in geometry_override_requests({})}
    assert sorted(moves) == ["new_m0", "new_t1"]
    assert all(t["translateY"] == round(40 * EMU_PER_PT) and t["translateX"] == 0 for t in moves.values())


def test_the_source_move_of_a_group_less_unit_reaches_its_picture_too():
    """The same rule on the other write path: a `move` (the source moved the unit, the deck edited
    its text) is written onto the deck's own objects, and when the person has taken the unit's group
    apart there is no one object that carries the rest."""
    from beamer2slides.sync import EMU_PER_PT, Sync
    base, pic = anchored_picture_base()
    members = merge.units(base["slides"][1]["elements"])["text/body/0"]
    units = [{"key": "text/body/0", "action": "move", "delta": [0, 20]}]
    ungrouped = live(base["slides"][1])
    grouped = copy.deepcopy(ungrouped)
    grouped["objects"]["b2s_s001_t1_g"] = readback([40, 120, 400, 180])
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        grouped["objects"][oid]["parent_group"] = "b2s_s001_t1_g"
    members[0] = {**members[0], "objects": [*members[0]["objects"], "b2s_s001_t1_g"]}
    bunits = {"text/body/0": members}
    assert [r["updatePageElementTransform"]["objectId"] for r in Sync.move_requests(units, bunits, grouped, 2.0)] \
        == ["b2s_s001_t1_g"]
    reqs = Sync.move_requests(units, bunits, ungrouped, 2.0)
    assert [r["updatePageElementTransform"]["objectId"] for r in reqs] == ["b2s_s001_t1", "b2s_s001_m0"]
    assert all(r["updatePageElementTransform"]["transform"]["translateY"] == round(40 * EMU_PER_PT) for r in reqs)


def test_a_moved_unit_that_still_has_its_group_is_moved_once():
    """The counterpart, and why one request was ever enough: the group carries the picture, and a
    second step on the picture would move it twice."""
    reqs = geometry_override_requests({"text/body/0": "new_t1_g"})
    assert [r["updatePageElementTransform"]["objectId"] for r in reqs] == ["new_t1_g"]


def test_a_created_shape_stays_under_the_text_the_source_draws_above_it():
    """`Sync.restack` gives a recreated element the deck's place in the z-order (a restack the person
    made survives) and a created one the place the source gives it - right after the element before
    it. Where those two orders disagree - a frame the source rewrote from scratch, or a label that
    moved onto another slide - that put a newly created opaque panel on top of text the source draws
    above it. Nothing is deleted when that happens, so every other check passes; the offline campaign
    saw it as `loss_oracle.text_hidden` on 5 of 1000 adopt-shaped rounds. A created element now also
    stays below the first element the source draws above it."""
    from beamer2slides.sync import Sync
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = ours["slides"][0]
    panel = ours_entry("shape/panel/0", shape_ir((15, 55, 210, 95), "p0s0"))
    # the source draws body, then the panel, then the title: the deck has the title at the bottom
    o["elements"] = [o["elements"][1], panel, o["elements"][0]]
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/body/0", "action": "recreate"}, {"key": "shape/panel/0", "action": "create"},
                   {"key": "text/title/0", "action": "recreate"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    before = live(base["slides"][0])
    w = {"plan": p, "doomed": set(before["order"]),
         "tops": {"text/body/0": "new_t1", "shape/panel/0": "new_s0", "text/title/0": "new_t0"}}
    now = {"order": [*before["order"], "new_t1", "new_s0", "new_t0"]}
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):     # BRING_TO_FRONT, bottom to top
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("new_t0")


def test_a_created_panel_stays_under_words_only_the_deck_has():
    """The other half of that rule. The source's order says where a new element goes among the
    source's own; about an element the source dropped and the deck's edits kept alive it says
    nothing at all - so the guard above, which only consults source elements, let a created opaque
    panel land on top of a kept text. Nothing is deleted, so only `loss_oracle.text_hidden` saw it
    (converted seed 78036, chain 4)."""
    from beamer2slides.sync import Sync
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = ours["slides"][0]
    panel = ours_entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0"))
    o["elements"] = [o["elements"][0], panel]   # the source dropped the body text; the deck kept it
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "recreate"}, {"key": "shape/panel/0", "action": "create"},
                   {"key": "text/body/0", "action": "keep"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    before = live(base["slides"][0])
    before["order"] = ["b2s_s000_t1", "b2s_s000_t0"]     # the kept body is at the bottom
    w = {"plan": p, "doomed": {"b2s_s000_t0"}, "tops": {"text/title/0": "new_t0", "shape/panel/0": "new_s0"}}
    now = {"order": ["b2s_s000_t1", "b2s_s000_t0", "new_t0", "new_s0"],
           "objects": {**before["objects"],
                       "new_t0": readback([20, 20, 200, 48], text="Intro"),
                       "new_s0": readback([30, 160, 420, 260])}}   # opaque, over the kept body's box
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("b2s_s000_t1")


def test_a_created_panel_goes_under_a_persons_box_grouped_with_one_of_the_converters():
    """Which words are the deck's own is a question about each *text*, not about the page element
    holding it. A person who groups one of their own text boxes with one of the converter's leaves a
    page element that is partly the source's, and reading the element as a whole made it the
    source's: the guard let a created panel land on the box inside it (converted fuzz seed 5200496
    at chain 12, found when the page-element rule above first reached this loop)."""
    from beamer2slides.sync import Sync
    base = many_slides(["intro"])
    ours, _ = triple(base)
    o = ours["slides"][0]
    o["elements"] = [*o["elements"], ours_entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0"))]
    slide = base["slides"][0]
    slide["order"] = ["user_g", "b2s_s000_t1"]          # the person's group is at the bottom
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "text/body/0", "action": "keep"},
                   {"key": "shape/panel/0", "action": "create"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    before = {"objectId": "b2s_s000", "order": ["user_g", "b2s_s000_t1"], "objects": {
        "user_g": {**readback([20, 20, 300, 120], kind="elementGroup"),
                   "children": ["b2s_s000_t0", "user_box"]},
        "b2s_s000_t0": {**readback([20, 20, 200, 48], text="Intro"), "parent_group": "user_g"},
        "user_box": {**readback([20, 60, 300, 120], text="a note nobody may lose"), "parent_group": "user_g"},
        "b2s_s000_t1": readback([40, 120, 400, 180], text="First point of intro")}}
    w = {"plan": p, "doomed": set(), "tops": {"shape/panel/0": "new_s0"}}
    now = {"order": ["user_g", "b2s_s000_t1", "new_s0"],
           "objects": {**before["objects"], "new_s0": readback([10, 10, 400, 190])}}   # over the lot
    order = list(now["order"])
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("user_g")
    # ... but a group holding nothing but the converter's own is the source's, and the source's
    # order stands: the panel it draws last stays last.
    before["objects"]["user_g"]["children"] = ["b2s_s000_t0"]
    del before["objects"]["user_box"]
    now["objects"] = {**before["objects"], "new_s0": now["objects"]["new_s0"]}
    order = list(now["order"])
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
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
    from beamer2slides.sync import Sync
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("text/body/1", text_ir("Above the panel", (20, 40, 200, 60), "p0t1"), "b2s_s000_t1"),
           entry("shape/panel/0", shape_ir((15, 70, 210, 120), "p0s0"), "b2s_s000_s0"),
           entry("text/body/2", text_ir("Under the panel", (20, 130, 200, 150), "p0t2"), "b2s_s000_t2")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["order"] = ["b2s_s000_t0", "b2s_s000_s0", "user_g"]   # the person grouped the two bodies
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    before = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "b2s_s000_s0", "user_g"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], text="Intro"),
        "b2s_s000_s0": readback([30, 140, 420, 200]),
        "user_g": {**readback([40, 80, 400, 300], kind="elementGroup"),
                   "children": ["b2s_s000_t1", "b2s_s000_t2"]},
        "b2s_s000_t1": {**readback([40, 80, 400, 120], text="Above the panel"), "parent_group": "user_g"},
        "b2s_s000_t2": {**readback([40, 240, 400, 300], text="Under the panel"), "parent_group": "user_g"}}}
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "text/body/1", "action": "keep"},
                   {"key": "shape/panel/0", "action": "recreate"}, {"key": "text/body/2", "action": "keep"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    w = {"plan": p, "doomed": {"b2s_s000_s0"}, "tops": {"shape/panel/0": "new_s0"}}
    # the source grew the panel: it now covers the body the group carries *above* it
    now = {"order": ["b2s_s000_t0", "b2s_s000_s0", "user_g", "new_s0"],
           "objects": {**{k: v for k, v in before["objects"].items() if k != "b2s_s000_s0"},
                       "new_s0": readback([30, 200, 420, 320])}}
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
        order.append(order.pop(order.index(oid)))
    assert order.index("new_s0") < order.index("user_g")
    # ... and where the group carries nothing the source draws above the panel, the source's order
    # stands: the panel it draws last stays last, over the words it is meant to sit on.
    now["objects"]["new_s0"] = readback([30, 90, 420, 130])      # over the body drawn *under* it
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
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
    from beamer2slides.sync import Sync
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("text/body/1", text_ir("Only the deck has this", (20, 80, 200, 100), "p0t1"), "b2s_s000_t1"),
           entry("shape/panel/0", shape_ir((15, 120, 210, 170), "p0s0"), "b2s_s000_s0"),
           entry("text/body/2", text_ir("In the block", (20, 130, 200, 150), "p0t2"), "b2s_s000_t2")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["groups"], slide["order"] = ["blk"], ["b2s_s000_t0", "b2s_s000_t1", "blk"]
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    o = ours["slides"][0]
    o["elements"] = [o["elements"][0], o["elements"][2], o["elements"][3]]   # the source dropped body/1
    before = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "b2s_s000_t1", "blk"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], text="Intro"),
        "b2s_s000_t1": readback([40, 160, 400, 200], text="Only the deck has this"),
        "blk": {**readback([30, 240, 440, 340], kind="elementGroup"),
                "children": ["b2s_s000_s0", "b2s_s000_t2"]},
        "b2s_s000_s0": {**readback([30, 240, 420, 340]), "parent_group": "blk"},
        "b2s_s000_t2": {**readback([40, 260, 400, 300], text="In the block"), "parent_group": "blk"}}}
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "shape/panel/0", "action": "recreate"},
                   {"key": "text/body/2", "action": "keep"}, {"key": "text/body/1", "action": "keep"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    w = {"plan": p, "doomed": {"b2s_s000_s0"}, "tops": {"shape/panel/0": "new_s0"}}
    # the source grew the panel: it now covers the text the deck alone has, two page elements below
    now = {"order": ["b2s_s000_t0", "b2s_s000_t1", "blk"],
           "objects": {**{k: v for k, v in before["objects"].items() if k != "b2s_s000_s0"},
                       "blk": {**before["objects"]["blk"], "children": ["new_s0", "b2s_s000_t2"]},
                       "new_s0": {**readback([30, 150, 420, 340]), "parent_group": "blk"}}}
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
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
    from beamer2slides.sync import Sync
    els = [entry("text/title/0", text_ir("Intro", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0"),
           entry("shape/panel/0", shape_ir((15, 80, 210, 130), "p0s0"), "b2s_s000_s0"),
           entry("text/body/0", text_ir("First point\nSecond point", (20, 100, 200, 120), "p0t1"), "b2s_s000_t1")]
    slide = base_slide("intro", "b2s_s000", els, label="intro", title="Intro")
    slide["groups"], slide["order"] = ["blk"], ["b2s_s000_t0", "blk"]   # the panel and the body are a block
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": [slide]}
    ours, _ = triple(base)
    before = {"objectId": "b2s_s000", "order": ["b2s_s000_t0", "blk"], "objects": {
        "b2s_s000_t0": readback([20, 20, 200, 48], text="Intro"),
        "blk": {**readback([30, 160, 440, 280], kind="elementGroup"),
                "children": ["b2s_s000_s0", "b2s_s000_t1"]},
        "b2s_s000_s0": {**readback([30, 160, 420, 260]), "parent_group": "blk"},
        "b2s_s000_t1": {**readback([40, 200, 400, 240], text="First point"), "parent_group": "blk"}}}
    p = {"action": "update", "key": "intro", "base": 0, "ours": 0, "objectId": "b2s_s000",
         "units": [{"key": "text/title/0", "action": "keep"}, {"key": "shape/panel/0", "action": "recreate"},
                   {"key": "text/body/0", "action": "keep"}]}
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    w = {"plan": p, "doomed": {"b2s_s000_s0"}, "tops": {"shape/panel/0": "new_s0"}}
    # the group is gone: Slides ungrouped it and one member was left, so both stand on the page
    now = {"order": ["b2s_s000_t0", "b2s_s000_s0", "b2s_s000_t1", "new_s0"],
           "objects": {"b2s_s000_t0": before["objects"]["b2s_s000_t0"],
                       "b2s_s000_t1": {**before["objects"]["b2s_s000_t1"], "parent_group": None},
                       "new_s0": readback([30, 160, 420, 270])}}     # opaque, over the body's box
    order = [x for x in now["order"] if x not in w["doomed"]]
    for r in sync.restack(w, before, now):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
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
    Sync._by_the_source(desired, oldtop, tops, keys, base_order)
    assert desired == ["new_t0", "user_pic", "new_s0", "new_t1"]
    # ... but a deck whose order is no longer the base's is one somebody restacked, and that stands
    theirs = ["new_s0", "user_pic", "new_t0", "new_t1"]
    Sync._by_the_source(theirs, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"])
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
    Sync._by_the_source(desired, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"])
    assert desired == ["old_t0", "new_s0", "old_t1"]
    # ... and a deck whose order is no longer the base's is one somebody restacked, and that stands
    theirs = ["old_t1", "old_t0", "new_s0"]
    Sync._by_the_source(theirs, oldtop, tops, keys, ["old_t0", "old_t1", "old_s0"])
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
    from beamer2slides.sync import Sync
    objects = {"g": {"kind": "elementGroup", "children": ["old_t", "user_pic", "old_s", "kept_t"]}}
    regroup = {"g": {"remove": {"old_t", "old_s"}, "unit_of": {"old_t": "text/body/0", "old_s": "shape/panel/0"}}}
    tops = {"text/body/0": "new_t", "shape/panel/0": "new_s"}
    # the source draws the panel under both texts; the deck had it on top of them
    rank = {"new_t": 2, "new_s": 1, "kept_t": 3}
    reqs = Sync.regroup_requests(regroup, {"g": 0}, objects, tops, set(), rank)
    group, = [r["groupObjects"] for r in reqs if "groupObjects" in r]
    assert group["childrenObjectIds"] == ["new_s", "user_pic", "new_t", "kept_t"]
    # ... and with no source order to go by, the deck's own order stands
    plain, = [r["groupObjects"] for r in Sync.regroup_requests(regroup, {"g": 0}, objects, tops, set())
              if "groupObjects" in r]
    assert plain["childrenObjectIds"] == ["new_t", "user_pic", "new_s", "kept_t"]


def test_the_rank_a_rebuilt_group_is_ordered_by_covers_kept_objects_too():
    """`Sync.zrank` is where that order comes from: the objects this sync writes, and the deck's own
    objects, which stand for the elements it is keeping. Leaving the kept ones out was why the first
    version of the rule reached only a group whose children the sync rewrote outright."""
    from beamer2slides.sync import Sync
    base = many_slides(["intro"])
    o = ours_of(base["slides"][0])
    bunits = merge.units(base["slides"][0]["elements"])
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
    opaque = readback([50, 272, 370, 352])                      # the source redrew it taller
    read = {"order": ["blk", "ug"], "objects": {
        "blk": {"kind": "elementGroup", "box": [80, 328, 430, 384], "children": ["new_t"]},
        "new_t": readback([90, 344, 420, 368], text="bullet editor picture table\n"),
        "ug": {"kind": "elementGroup", "box": [40, 40, 400, 352], "children": ["mine", "new_s"]},
        "mine": readback([40, 40, 256, 68], text="the person's own heading\n"),
        "new_s": opaque}}
    made, ours = {"new_t", "new_s"}, {"blk", "old_t", "old_s"}
    assert S.folded_hiders(read, made, ours) == [("new_t", "new_s")]
    # ... and nothing where the shape stands on the page itself: that one `restack` orders away
    page = {"order": ["blk", "new_s"], "objects": {k: v for k, v in read["objects"].items()
                                                   if k not in ("ug", "mine")}}
    assert S.folded_hiders(page, made, ours) == []
    # ... nor where the person's group is where the text is too: no page order decides that
    together = {"order": ["ug"], "objects": {**read["objects"],
                                             "ug": {**read["objects"]["ug"], "children": ["new_t", "new_s"]}}}
    assert S.folded_hiders(together, made, ours) == []
    # ... nor words the cleanup is about to delete: the order is read before it, and the old block's
    # words still stand under the new one (live scenario nested-group warned about v1's block title)
    assert S.folded_hiders(read, made, ours, doomed={"new_t"}) == []
    # and the report says it, in the person's words: the sync's own `ours`/`made` come from the base
    sync = S.Sync.__new__(S.Sync)
    sync.warnings = []
    sync.base = {"slides": [{"key": "intro", "groups": ["blk"],
                             "elements": [{"key": "text/body/0", "objects": ["old_t"]},
                                          {"key": "shape/panel/0", "objects": ["old_s"]}]}]}
    work = {"slides": [{"plan": {"action": "update", "base": 0, "objectId": "s1"},
                        "objects": {"text/body/0": ["new_t"], "shape/panel/0": ["new_s"]}}]}
    sync.warn_about_folded_hiders(work, {"slides": [{**read, "objectId": "s1"}]})
    assert len(sync.warnings) == 1 and "bullet editor picture table" in sync.warnings[0]
    assert "group you made" in sync.warnings[0] and "Ungroup it" in sync.warnings[0]


def test_image_replaced_in_deck_is_kept():
    base = three_slides()
    pic = {"id": "p1f0", "kind": "image", "role": "figure", "bbox": [200, 60, 300, 160], "file": None}
    el = entry("image/figure/0", pic, "b2s_s001_f2")
    el["readback"]["b2s_s001_f2"] = readback([400, 120, 600, 320], kind="image", image="aaa")
    base["slides"][1]["elements"].append(el)
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("image/figure/0", {**pic, "bbox": [200, 60, 320, 160]})
    theirs["slides"][1]["objects"]["b2s_s001_f2"]["image"] = {"contentHash": "bbb"}
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["field"] == "image"


# ---------------------------------------------------------------- slides

def test_slide_added_deleted_and_reordered():
    base = three_slides()
    ours, theirs = triple(base)
    live_ids = [s["objectId"] for s in theirs["slides"]]
    # a new frame after intro
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"))], "method"))
    ours2 = {"slides": [ours["slides"][0], new, ours["slides"][1], ours["slides"][2]], "pairs": {0: 0, 2: 1, 3: 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert mplan["order"] == ["b2s_s000", "new:method", "b2s_s001", "b2s_s002"]
    assert mplan["report"]["slides"]["created"] == ["method"]
    # results removed from the source, untouched in the deck: deleted
    ours3 = {"slides": [ours["slides"][0], ours["slides"][2]], "pairs": {0: 0, 1: 2}}
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [p["action"] for p in mplan["slides"] if p["key"] == "results"] == ["delete"]
    assert mplan["order"] == ["b2s_s000", "b2s_s002"]
    # ... but kept after its predecessor when the deck added something to it
    theirs["slides"][1]["objects"]["user_box"] = readback([0, 0, 50, 20], "note")
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [p["action"] for p in mplan["slides"] if p["key"] == "results"] == ["keep_removed"]
    assert mplan["order"] == live_ids
    # the source swaps results and end
    ours, theirs = triple(base)
    ours4 = {"slides": [ours["slides"][0], ours["slides"][2], ours["slides"][1]], "pairs": {0: 0, 1: 2, 2: 1}}
    mplan = merge.plan_merge(base, ours4, theirs)
    assert mplan["order"] == ["b2s_s000", "b2s_s002", "b2s_s001"]
    assert merge.has_writes(mplan, live_ids)
    # ... the deck's own move of intro wins over the swap it disagrees with
    theirs["slides"] = [theirs["slides"][1], theirs["slides"][0], theirs["slides"][2]]
    mplan = merge.plan_merge(base, ours4, theirs)
    assert mplan["order"] == ["b2s_s001", "b2s_s000", "b2s_s002"]


def test_a_slide_the_deck_moved_does_not_freeze_the_rest_of_the_order():
    """One slide dragged in Slides is no instruction to leave the other four where they are: the
    source's order is applied around it, and the dragged slide keeps the place the deck gave it."""
    base = many_slides(["a", "b", "c", "d", "e"])
    ours, theirs = triple(base)
    ids = [s["objectId"] for s in base["slides"]]
    theirs["slides"] = [theirs["slides"][i] for i in (0, 4, 1, 2, 3)]       # the deck pulls e up after a
    ours2 = {"slides": [ours["slides"][i] for i in (0, 1, 3, 2, 4)],        # the source swaps c and d
             "pairs": {0: 0, 1: 1, 2: 3, 3: 2, 4: 4}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert mplan["order"] == [ids[0], ids[4], ids[1], ids[3], ids[2]]
    assert not [w for w in mplan["report"]["warnings"] if "moved" in w]
    # ... and when both moved the same slide, the deck's place wins and the report says so
    ours3 = {"slides": [ours["slides"][i] for i in (0, 1, 2, 4, 3)], "pairs": {0: 0, 1: 1, 2: 2, 3: 4, 4: 3}}
    mplan = merge.plan_merge(base, ours3, theirs)
    assert mplan["order"] == [ids[0], ids[4], ids[1], ids[2], ids[3]]
    assert [w for w in mplan["report"]["warnings"] if "both the source and the deck moved" in w]


def bolded(slide, oid, word):
    """The person bolds one word of an object, the way `tools/deck_edits.py bold` does live."""
    rb = slide["objects"][oid]
    at = rb["text"].index(word)
    plain = rb["text_styles"][0]
    rb["text_styles"] = [plain, {**plain, "bold": True}]
    rb["run_spans"] = [[0, at, plain], [at, at + len(word), {**plain, "bold": True}],
                       [at + len(word), len(rb["text"].rstrip("\n")), plain]]
    rb["text_style_hash"] = "bolded"


def test_styling_on_words_the_source_replaced_is_a_conflict_not_a_promise():
    """Live round 404, chained: the person bolded a word of a title and the source then rewrote that
    title. `sync.style_range_requests` puts the deck's run styles back onto the same words, and the
    words are gone - so the bold ends there, which nothing can help. What the report may not do is
    promise the styling was kept, and it used to: an `overrides` entry and not a word more."""
    base = many_slides(["intro"])
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        "A wholly different sentence about something else\nSecond point of intro", (20, 60, 200, 90), "p0t1"))
    bolded(theirs["slides"][0], "b2s_s000_t1", "First")
    report = merge.plan_merge(base, ours, theirs)["report"]
    (c,) = [c for c in report["conflicts"] if c["field"] == "text_style"]
    assert c["resolution"] == "the styling of the replaced words is gone"
    # The word survives the rewrite: the styling lands on it again, and there is nothing to report.
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        "First point of intro, reworded\nSecond point of intro", (20, 60, 200, 90), "p0t1"))
    quiet = merge.plan_merge(base, ours, theirs)["report"]
    assert not [c for c in quiet["conflicts"] if c["field"] == "text_style"]
    assert [o for o in quiet["overrides"] if "text_style" in o["fields"]]


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
                                        (20, 60, 200, 90), "p0t1"), "b2s_s000_t1")]
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None,
            "slides": [base_slide("intro", "b2s_s000", els, label="intro", title="Intro")]}
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][0] = ours_entry("text/body/0", text_ir(
        "and the source adds merge\nand the source adds slides typed by a person",
        (20, 60, 200, 90), "p0t1"))
    bolded(theirs["slides"][0], "b2s_s000_t1", "a person")   # the first one: the source replaced it
    report = merge.plan_merge(base, ours, theirs)["report"]
    (c,) = [c for c in report["conflicts"] if c["field"] == "text_style"]
    assert c["resolution"] == "the styling of the replaced words is gone"


def test_a_bullet_the_deck_deleted_does_not_freeze_the_box_against_the_source():
    """A read-back keeps the *distinct* paragraph styles of a text, and the converter gives every
    paragraph the line spacing of its own PDF pitch - so deleting one bullet takes an entry out of
    that list. `uniform_changes` read the shorter list as a change it could not re-apply (None), the
    unit became a `text_style` conflict blaming a restyle nobody made, and the box was kept exactly
    as it stood: every later source change to it was dropped, for good. Found by the live scenario
    `last-paragraph`, where a reworded bullet never arrived."""
    three = "First point of intro\nSecond point of intro\nThird point of intro"
    styles = [{"alignment": "START", "lineSpacing": 116.3}, {"alignment": "START", "lineSpacing": 114.3},
              {"alignment": "START", "lineSpacing": 100.0}]   # one per paragraph, as the converter writes them
    base = many_slides(["intro"])
    base["slides"][0]["elements"][1] = entry("text/body/0", text_ir(three, (20, 60, 200, 102), "p0t1"), "b2s_s000_t1")
    base["slides"][0]["elements"][1]["readback"]["b2s_s000_t1"]["paragraph_styles"] = styles
    ours, theirs = triple(base)
    rb = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    rb["text"] = "First point of intro\nSecond point of intro\n"   # the person deleted the last bullet
    rb["paragraph_styles"] = styles[:2]                            # and its line spacing went with it
    rb["text_style_hash"] = "one paragraph fewer"
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        three.replace("First point of intro", "First point of intro, reworded"), (20, 60, 200, 102), "p0t1"))
    mplan = merge.plan_merge(base, ours, theirs)
    assert not mplan["report"]["conflicts"]
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and "text" in u["overrides"]
    assert u["overrides"]["text_style"] == {"runs": {}, "paragraphs": {}}
    # The deck's deletion stands and the source's rewording still lands.
    merged, _, safe = merge.text_merge(u["overrides"]["text"]["base"],
                                       three.replace("First point of intro", "First point of intro, reworded") + "\n",
                                       u["overrides"]["text"]["theirs"])
    assert safe and merged == "First point of intro, reworded\nSecond point of intro\n"
    # A restyle proper still is one: a style the base never had is nothing a deletion can explain,
    # and one centred paragraph among others is not something re-applying an attribute can give back.
    rb["paragraph_styles"] = styles[:1] + [{"alignment": "CENTER", "lineSpacing": 114.3}]
    assert [c for c in merge.plan_merge(base, ours, theirs)["report"]["conflicts"] if c["field"] == "text_style"]


def test_the_two_halves_of_a_frame_nothing_could_follow_are_named():
    """`identity.near_misses` hands the merge a slide the source seems to have dropped and a frame
    it seems to have written that say much of the same thing. The report names both, and says which
    of the two cases this is: the deck kept the old slide (the person had edits on it) or it went."""
    base = many_slides(["a", "b"])
    ours, theirs = triple(base)
    ours["slides"][1] = {**ours["slides"][1], "key": "fresh"}          # the frame came back as new
    ours["pairs"] = {0: 0}
    ours["near_misses"] = [{"ours": 1, "base": 1, "evidence": 0.6, "slide": "b", "title": "B, recast"}]
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point of b, and a sentence of my own\n")
    kept = merge.plan_merge(base, ours, theirs)["report"]["warnings"]
    assert [w for w in kept if w.startswith("slide b:") and "the deck keeps the old slide" in w
            and "'B, recast'" in w]
    # Nobody had touched that slide, so it is gone - worth saying, and a different sentence.
    _, fresh = triple(base)
    gone = merge.plan_merge(base, ours, fresh)["report"]["warnings"]
    assert [w for w in gone if w.startswith("slide b:") and "it is gone" in w]


def test_a_slide_matched_by_its_place_alone_is_said_out_loud():
    """`identity.gap_pairs` pairs an unlabelled frame the source retitled and half rewrote by the
    two neighbours around it. Nothing is at risk - the alternative was a second slide beside this
    one - but it is an inference the words no longer support, so the report asks for a label. A
    frame that still has its label is paired by the label and gets no such warning."""
    base = many_slides(["a", "b", "c"])
    base["slides"][1]["label"] = None
    ours, theirs = triple(base)
    ours["slides"][1]["label"] = None
    ours["weak_pairs"] = {1: "place"}
    warnings = merge.plan_merge(base, ours, theirs)["report"]["warnings"]
    assert [w for w in warnings if w.startswith("slide b:") and "where it stands" in w]
    ours["slides"][1]["label"] = "b"      # a frame that kept its label was not matched by place
    assert not [w for w in merge.plan_merge(base, ours, theirs)["report"]["warnings"] if "where it stands" in w]
    # The keys come over JSON in a real run, so the ours index arrives as a string.
    ours["slides"][1]["label"] = None
    ours["weak_pairs"] = {"1": "place"}
    assert [w for w in merge.plan_merge(base, ours, theirs)["report"]["warnings"] if "where it stands" in w]


def test_a_frame_matched_between_twins_is_said_out_loud_too():
    """`identity.align_slides` marks a pairing it could as well have made with another slide, for
    the same score, when the frame has no label to settle it. Nothing downstream can tell that from
    a match the words really made, so the report says which way the coin fell and asks for a label."""
    base = many_slides(["a", "b", "c"])
    base["slides"][1]["label"] = None
    ours, theirs = triple(base)
    ours["slides"][1]["label"] = None
    ours["weak_pairs"] = {1: "twins"}
    warnings = merge.plan_merge(base, ours, theirs)["report"]["warnings"]
    assert [w for w in warnings if w.startswith("slide b:") and "coin toss" in w]
    ours["slides"][1]["label"] = "b"        # a frame with a label of its own was never in doubt
    assert not [w for w in merge.plan_merge(base, ours, theirs)["report"]["warnings"] if "coin toss" in w]


def test_user_added_slide_stays_after_its_predecessor():
    base = three_slides()
    ours, theirs = triple(base)
    theirs["slides"].insert(2, {"objectId": "user_slide", "layoutObjectId": "L", "background": {"state": "INHERIT"},
                                "notes": "", "notes_id": None, "order": [], "objects": {}})
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"))], "method"))
    ours2 = {"slides": [new] + ours["slides"], "pairs": {1: 0, 2: 1, 3: 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert mplan["order"] == ["new:method", "b2s_s000", "b2s_s001", "user_slide", "b2s_s002"]
    assert mplan["report"]["slides"]["user_added"] == [{"objectId": "user_slide", "copy_of": None}]


def test_notes_follow_text_rules():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["notes"] = "Say hello"
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["slides"][0]["notes"] == "Say hello"
    theirs["slides"][0]["notes"] = "Please say hello"
    ours["slides"][0]["notes"] = "Say hello to everyone"
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in mplan["slides"][0]  # both wrote into empty notes: a conflict, the deck's kept
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["notes"]
    ours["slides"][0]["notes"] = ""
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in mplan["slides"][0]
    assert {"slide": "intro", "element": None, "fields": ["notes"]} in mplan["report"]["overrides"]


# ---------------------------------------------------------------- request helpers

def test_rename_object_ids():
    reqs = [{"createShape": {"objectId": "b2s_s003_t1", "elementProperties": {"pageObjectId": "b2s_s003"}}},
            {"groupObjects": {"groupObjectId": "b2s_s003_t1_g", "childrenObjectIds": ["b2s_s003_t1", "b2s_s003_f12", "b2s_s003_f1n"]}},
            {"duplicateObject": {"objectId": "b2s_s003_k0", "objectIds": {"b2s_s003_k0": "b2s_s003_s0"}}}]
    mapping = sorted({"b2s_s003_t1": "NEW_T", "b2s_s003_f1": "NEW_F", "b2s_s003_k0": "TPL", "b2s_s003": "LIVE",
                      "b2s_s003_s0": "NEW_S"}.items(), key=lambda kv: -len(kv[0]))
    out = rename(reqs, mapping)
    assert out[0]["createShape"] == {"objectId": "NEW_T", "elementProperties": {"pageObjectId": "LIVE"}}
    assert out[1]["groupObjects"] == {"groupObjectId": "NEW_T_g", "childrenObjectIds": ["NEW_T", "LIVE_f12", "NEW_Fn"]}
    assert out[2]["duplicateObject"] == {"objectId": "TPL", "objectIds": {"TPL": "NEW_S"}}


def test_letterbox_fix_stretches_to_the_box():
    fix = letterbox_fix("i", [100, 50, 400, 150], (800, 600))["updatePageElementTransform"]["transform"]
    # createImage puts a 4:3 picture into a 300 x 100 box as 133.33 x 100 centred at x = 183.33
    fx0, fx1 = 183.3333, 316.6667
    assert fix["scaleX"] * fx0 + fix["translateX"] / 12700 == pytest.approx(100, abs=0.01)
    assert fix["scaleX"] * fx1 + fix["translateX"] / 12700 == pytest.approx(400, abs=0.01)
    assert fix["scaleY"] == pytest.approx(1) and fix["translateY"] == pytest.approx(0, abs=1)


def test_a_base_out_of_the_sources_order_costs_the_frames_after_it():
    """Why the new base has to stay in the source's order: frames without a label are paired with
    the base by an order-keeping alignment, and an entry moved to the end falls out of that order.
    What a frame says can still save it (`identity.cross_pairs` picks up what the order left over),
    but only when it says something unmistakable. Frames that talk alike have nothing to be
    recognised by and take each other's keys - and then the sync writes one onto the other."""
    source = [info("Why decks diverge", "decks and sources drift apart over time"),
              info("The sync algorithm", "base ours theirs three way merge of the deck"),
              info("Conclusions", "thanks for listening and for the questions")]
    keys = identity.slide_keys(source)
    moved = [source[0], source[2], source[1]]  # the middle frame's entry recorded last
    got, _ = identity.inherit_slide_keys(moved, [keys[0], keys[2], keys[1]], source)
    assert got == keys
    alike = [info(f"Results {n}", "the table below repeats the measured numbers") for n in ("one", "two", "three")]
    akeys = identity.slide_keys(alike)
    got, _ = identity.inherit_slide_keys([alike[0], alike[2], alike[1]], [akeys[0], akeys[2], akeys[1]], alike)
    assert got == [akeys[0], akeys[2], akeys[1]]


def test_a_slide_the_deck_deleted_keeps_its_place_in_the_new_base():
    """Found by the offline fuzz (`second_sync_writes`, seeds 45, 60, 108, 177 of the default run):
    the person deletes a converter slide whose frame has no label and the source still has it. The
    sync is right to leave it deleted, but it used to record that slide's base entry last
    (`new_base` keys it `gone:<key>`, which `base_order` did not return). The base is converter
    output, and the next conversion pairs frames with it by `identity.align_slides` - see the test
    above for what a base out of order costs."""
    from beamer2slides.sync import base_order
    plans = [{"key": "deleted_by_the_person", "action": "gone", "ours": 0, "base": 0, "objectId": None},
             {"key": "kept", "action": "update", "ours": 1, "base": 1, "objectId": "b2s_s001"}]
    by_plan = {id(plans[1]): {"sid": "b2s_s001"}}
    assert base_order({"slides": plans}, by_plan, ["b2s_s001"]) == ["gone:deleted_by_the_person", "b2s_s001"]


def test_a_deleted_slide_in_the_base_does_not_displace_a_kept_one():
    """The two ways a slide can be in the base without being in the deck must not fight. What the
    next conversion aligns against is the entries the source still has, in the source's order: a
    `gone` slide (the person deleted it, the source kept it) is one of those and belongs between
    its source neighbours; a `keep_removed` one (the source dropped it, the deck edited it) is not,
    and only has to be somewhere."""
    from beamer2slides.sync import base_order
    plans = [{"key": "one", "action": "update", "ours": 0, "base": 0, "objectId": "b2s_s000"},
             {"key": "gone_one", "action": "gone", "ours": 1, "base": 1, "objectId": None},
             {"key": "two", "action": "update", "ours": 2, "base": 2, "objectId": "b2s_s002"},
             {"key": "kept_one", "action": "keep_removed", "ours": None, "base": 3, "objectId": "b2s_k003"}]
    by_plan = {id(p): {"sid": p["objectId"]} for p in plans if p["objectId"]}
    order = base_order({"slides": plans}, by_plan, ["b2s_s000", "b2s_k003", "b2s_s002"])
    assert [x for x in order if x != "b2s_k003"] == ["b2s_s000", "gone:gone_one", "b2s_s002"]
    assert "b2s_k003" in order


def test_sync_does_not_alt_text_a_diagram_group():
    """Found by the live fuzz (tools/fuzz_sync.py, seed 202): a sync that rewrites a slide with a
    diagram died with 'The operation is not allowed on group (b2s_..._<tok>)'. A diagram element's
    main object *is* a group (emit.diagram_requests groups its parts under the element's object id),
    and sync.tag_requests sent an alt-text title for every element it wrote, which took the whole
    batch down with it. snapshot.tag_requests skips element groups for the same reason; the other
    elements on that slide are still tagged."""
    from types import SimpleNamespace

    from beamer2slides.sync import Sync
    o = {"key": "figures", "elements": [{"key": "diagram/figure/0"}, {"key": "text/body/0"}]}
    stub = SimpleNamespace(plan=SimpleNamespace(deck={"slides": [{"elements": [
        {"kind": "diagram", "id": "p0d0"}, {"kind": "text", "id": "p0t1"}]}]}), ours={"slides": [o]})
    oid = "b2s_abcdef_012345_t0k"  # the group emit creates for the diagram, with its nodes and lines inside
    text = "b2s_abcdef_012346_t0k"
    reqs = Sync.tag_requests(stub, o, {0: [oid, f"{oid}_n0", f"{oid}_l0"], 1: [text]},
                             {0: oid, 1: text}, {})
    assert [r["updatePageElementAltText"]["objectId"] for r in reqs] == [text]


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
        slide = next(s for s in planned["plan"].deck["slides"] if s["page"] == page)
        for el, oids in zip(slide["elements"], objects):
            anchored = [e for e in slide["elements"] if e.get("anchor") == el["id"]]
            if anchored and el["role"] != "title":
                assert f"{oids[0]}_g" in oids


# ---------------------------------------------------------------- found by the live suite (tests/test_sync_live.py)

def picture(size, text, fmt="PNG", scale=1):
    """A tight formula-like picture: `text` drawn at 4x and scaled down to `size`."""
    import io
    from PIL import Image, ImageDraw
    big = Image.new("RGB", (size[0] // 4 * scale, size[1] // 4), "white")
    ImageDraw.Draw(big).text((1, 1), text, fill="black")
    img = big.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, fmt, **({"quality": 75} if fmt == "JPEG" else {}))
    return buf.getvalue()


def test_pictures_compare_by_pixels_not_content_urls():
    """Google hands out new contentUrls for unchanged pictures: a changed URL hash alone is no
    'replaced in the deck' (it blocked source changes of every slide with a picture)."""
    from beamer2slides import snapshot
    formula = picture((240, 56), "x = a + b")
    reencoded = picture((240, 56), "x = a + b", "JPEG")
    other = picture((240, 56), "y - c / d")
    stretched = picture((480, 56), "x = a + b", scale=2)
    sig = snapshot.signature
    assert snapshot.signatures_match(sig(formula), sig(reencoded))
    assert not snapshot.signatures_match(sig(formula), sig(other))
    assert not snapshot.signatures_match(sig(formula), sig(stretched))
    b = readback([10, 10, 70, 30], kind="image", image="aaa")
    b["image"]["signature"] = sig(formula)
    same = copy.deepcopy(b)
    same["image"] = {"contentHash": "bbb", "signature": sig(reencoded)}
    assert merge.object_changes(b, same) == set()
    replaced = copy.deepcopy(b)
    replaced["image"] = {"contentHash": "ccc", "signature": sig(other)}
    assert merge.object_changes(b, replaced) == {"image"}
    unsigned = copy.deepcopy(b)
    unsigned["image"] = {"contentHash": "ddd"}
    assert merge.object_changes(b, unsigned) == {"image"}  # (can't tell: counts as replaced)
    # backgrounds too
    base = three_slides()
    s = base["slides"][0]
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
    plans = [{"key": "a", "action": "update", "ours": 0}, {"key": "b", "action": "update", "ours": 1},
             {"key": "c", "action": "create", "ours": 2}, {"key": "gone", "action": "keep_removed", "ours": None}]
    works = [{"plan": plans[0], "sid": "A"}, {"plan": plans[1], "sid": "B"}, {"plan": plans[2], "sid": "C"},
             {"plan": plans[3]}]
    by_plan = {id(w["plan"]): w for w in works}
    # the deck has B before A, and a kept slide G and a user slide U after B
    order = base_order({"slides": plans}, by_plan, ["B", "G", "U", "A", "C"])
    assert [x for x in order if x in "ABC"] == ["A", "B", "C"]
    assert order.index("G") < order.index("U")
    # a second plan on that base: the deck's order still counts as a deck reorder and stays
    base = three_slides()
    ours, theirs = triple(base)
    theirs["slides"] = [theirs["slides"][2], theirs["slides"][0], theirs["slides"][1]]
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["order"] == ["b2s_s002", "b2s_s000", "b2s_s001"] and not mplan["report"]["slides"]["moved"]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_a_slide_the_source_dropped_keeps_no_label_in_the_new_base():
    """Found by the offline fuzz (`second_sync_writes`, seed 2218): the source moves label `f0`
    onto another frame and drops the frame that had it, whose slide the deck had edited - so it
    stays, and its base entry used to keep saying `label: f0`. The next conversion's `f0` then
    pairs with that dead entry instead of the frame that carries the label now, and sync rewrites
    the wrong slide. A label belongs to the source; an entry the source no longer describes has
    none."""
    from beamer2slides.sync import Sync
    base = three_slides()
    s = Sync.__new__(Sync)
    s.warnings, s.base, s.final_revision = [], base, "r2"
    s.ours = {"slides": [], "source": Path("talk.pdf")}
    s.created = {"slides": []}
    plans = [{"key": "intro", "action": "keep_removed", "ours": None, "base": 0, "objectId": "b2s_s000"}]
    result = {"plan": {"slides": plans}, "theirs": {"slides": [{"objectId": "b2s_s000"}]},
              "work": {"slides": [{"plan": plans[0], "sid": "b2s_s000"}]}}
    written = s.new_base(result)
    assert [b["key"] for b in written["slides"]] == ["intro"]
    assert written["slides"][0]["label"] is None and base["slides"][0]["label"] == "intro"
    # ... and the pairing that used to go wrong now finds the frame that carries the label
    ours = [{"label": "intro", "title": "Elsewhere", "text": "another frame entirely"}]
    assert identity.label_pairs(written["slides"], ours) == {}


def test_a_held_slide_keeps_the_base_it_had():
    """`merge.hold_slide` wrote nothing to this slide, so the base must not say the source's words
    arrived. An entry otherwise takes its label, title and text from `ours`, and then the next sync
    would read the edit this one held back as a change already made - the one way to actually lose
    work by waiting."""
    from beamer2slides.sync import Sync
    base = three_slides()
    s = Sync.__new__(Sync)
    s.warnings, s.base, s.final_revision = [], base, "r2"
    s.ours = {"slides": [{**ours_of(base["slides"][1]), "title": "Results v2", "text": "quite different now"}],
              "source": Path("talk.pdf")}
    s.created = {"slides": []}
    plans = [{"key": "results", "action": "update", "held": "label", "ours": 0, "base": 1,
              "objectId": "b2s_s001", "units": []}]
    result = {"plan": {"slides": plans}, "theirs": {"slides": [{"objectId": "b2s_s001"}]},
              "work": {"slides": [{"plan": plans[0], "sid": "b2s_s001"}]}}
    (written,) = s.new_base(result)["slides"]
    assert written["title"] == "Results" and written["text"] == base["slides"][1]["text"]
    assert written["elements"] == base["slides"][1]["elements"]


def test_renamed_title_keeps_its_key():
    """A retitled frame's title inherits the title key (it was deleted and recreated as a plain text
    box above the placeholder's place)."""
    base = [{"key": "text/title/0", "kind": "text", "role": "title", "fingerprint": identity.fingerprint(
                text_ir("Conclusions", (10, 10, 100, 24), "p9t0", "title"))},
            {"key": "text/body/0", "kind": "text", "role": "body", "fingerprint": identity.fingerprint(
                text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1"))}]
    ours = [text_ir("Takeaways", (10, 10, 90, 24), "p9t0", "title"), text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1")]
    keys, _ = identity.slide_element_keys(ours, None, base)
    assert keys == ["text/title/0", "text/body/0"]
    # (the slide's one title, wherever it went)
    ours = [text_ir("Takeaways", (300, 150, 380, 164), "p9t0", "title"), ours[1]]
    assert identity.slide_element_keys(ours, None, base)[0] == ["text/title/0", "text/body/0"]


def test_deleted_slide_whose_frame_counter_changed_is_no_conflict():
    base = three_slides()
    for n, s in enumerate(base["slides"]):
        foot = entry("text/footer/0", text_ir(f"{n + 1} / 3", (300, 190, 320, 196), f"p{n}t2", "footer"), f"b2s_s{n:03}_t2")
        s["elements"].append(foot)
    ours, theirs = triple(base)
    del theirs["slides"][2]
    ours["slides"][2]["elements"][2] = ours_entry("text/footer/0", text_ir("4 / 4", (300, 190, 320, 196), "p3t2", "footer"))
    assert merge.plan_merge(base, ours, theirs)["report"]["conflicts"] == []
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("Changed words", (20, 60, 200, 90), "p2t1"))
    assert [c["field"] for c in merge.plan_merge(base, ours, theirs)["report"]["conflicts"]] == ["slide"]


def test_words_restyled_in_the_deck_survive_a_source_text_change():
    """One bolded word (a non-uniform style edit) and a reworded source: recreate and re-apply the
    deck's run styles to the same words, instead of dropping the source change as a conflict."""
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["text_styles"] = obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"]["text_style"]["ranges"]
    assert not mplan["report"]["conflicts"]
    # a table: always by ranges (cell by cell)
    cells = lambda rows: [[[run(c)] for c in row] for row in rows]  # noqa: E731
    table = {"id": "p1b0", "kind": "table", "role": "table", "bbox": [20, 100, 200, 160],
             "cells": cells([["Scenario", "Time"], ["Disjoint", "3.9 s"]])}
    base = three_slides()
    el = entry("table/table/0", table, "b2s_s001_tab2")
    base["slides"][1]["elements"].append(el)
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", {**table, "cells": cells([["Scenario", "Time"], ["Disjoint", "4.7 s"]])})
    obj = theirs["slides"][1]["objects"]["b2s_s001_tab2"]
    obj.update(kind="table", text_styles=obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}],
               text_style_hash="bold cell word")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "table/table/0")
    assert u["action"] == "recreate" and u["overrides"]["text_style"]["ranges"]


def test_source_style_change_elsewhere_is_no_style_conflict():
    """A bullet removed in the source changes the IR's style set (list levels) while the deck
    bolded a word: no conflict, the bold is re-applied."""
    base = three_slides()
    ours, theirs = triple(base)
    ir = text_ir("First point of intro, reworded\nSecond point of intro", (20, 60, 230, 90), "p0t1")
    ir["paragraphs"][1]["level"] = 1
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", ir)
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["text_styles"] = obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "recreate" and not mplan["report"]["conflicts"]
    # the source made the text bold too: reported
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1", bold=True))
    mplan = merge.plan_merge(base, ours, theirs)
    assert [c["resolution"] for c in mplan["report"]["conflicts"]] == ["deck style re-applied"]


def test_deck_move_the_source_reproduces_converges():
    """A deck move written into the source (pull: a \\vspace) gives the same place: adopted, no write."""
    base = three_slides()
    base["scale"] = 2.0
    ours, theirs = triple(base)
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("First point of end\nSecond point of end",
                                                                         (20, 75, 200, 105), "p2t1"))
    obj = theirs["slides"][2]["objects"]["b2s_s002_t1"]
    obj["box"] = [obj["box"][0], obj["box"][1] + 30.8, obj["box"][2], obj["box"][3] + 30.8]
    obj["transform"][5] += 30.8
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "end", "text/body/0")
    assert u["action"] == "adopt" and u["adopt"] == ["geometry"]
    assert mplan["report"]["converged"] == [{"slide": "end", "element": "text/body/0", "field": "geometry"}]
    assert not mplan["report"]["conflicts"] and not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])
    # 5 pt off: both moved, the deck's place kept
    obj["box"][1] += 5
    obj["transform"][5] += 5
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "recreate"


def raw_shape(oid, runs):
    """A page element as presentations.get returns it: runs = [(text, style)]."""
    elements, i = [], 0
    for text, style in runs:
        elements.append({"startIndex": i, "endIndex": i + len(text), "textRun": {"content": text, "style": style}})
        i += len(text)
    return {"objectId": oid, "shape": {"text": {"textElements": elements}}}


def test_style_range_requests_follow_the_words():
    from beamer2slides.sync import deck_attributes, style_range_requests
    plain = {"fontFamily": "Lato", "fontSize": {"magnitude": 18, "unit": "PT"}}
    bold = {**plain, "bold": True}
    base_styles = [{"fontFamily": "Lato", "fontSize": 18.0}]
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0}, base_styles) == {}
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0, "bold": True}, base_styles) == {"bold": True}
    old = raw_shape("old", [("Written by an ", plain), ("AI", bold), (" assistant\n", plain)])
    new = raw_shape("new", [("Now written by an AI assistant and converted\n", plain)])
    (r,) = style_range_requests("new", old, new, base_styles)
    rng = r["updateTextStyle"]["textRange"]
    assert "Now written by an AI assistant and converted\n"[rng["startIndex"]:rng["endIndex"]] == "AI"
    assert r["updateTextStyle"]["style"] == {"bold": True} and r["updateTextStyle"]["fields"] == "bold"
    # a word the source deleted takes its style along; text after the text override counts
    assert style_range_requests("new", old, raw_shape("new", [("Written by an assistant\n", plain)]), base_styles) == []
    (r,) = style_range_requests("new", old, new, base_styles, merged="Written by an AI helper\n")
    assert r["updateTextStyle"]["textRange"]["startIndex"] == 14
    # table cells, by cellLocation
    cell = lambda runs: {"text": raw_shape("x", runs)["shape"]["text"]}  # noqa: E731
    old_t = {"objectId": "t", "table": {"tableRows": [{"tableCells": [cell([("Disjoint\n", bold)]), cell([("3.9 s\n", plain)])]}]}}
    new_t = {"objectId": "t", "table": {"tableRows": [{"tableCells": [cell([("Disjoint\n", plain)]), cell([("4.7 s\n", plain)])]}]}}
    (r,) = style_range_requests("t", old_t, new_t, base_styles)
    assert r["updateTextStyle"]["cellLocation"] == {"rowIndex": 0, "columnIndex": 0}
    assert r["updateTextStyle"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 8}


def test_a_style_on_the_last_word_stops_where_the_text_does():
    """The styling of the *last* word of a box is where a range can run into the newline Slides
    will not let anything touch (`merge.text_edit_requests`): the API measures a `FIXED_RANGE`
    against the same length a delete is measured against, and a range one too long throws the whole
    batch out. Nothing here may reach it - a run's trailing newlines come off its range, and the
    text it is re-applied to ends on one - so the styling of the last word ends exactly at the
    length the API accepts, and the newline keeps the style it already has."""
    from beamer2slides.sync import style_range_requests
    plain = {"fontFamily": "Lato", "fontSize": {"magnitude": 18, "unit": "PT"}}
    bold = {**plain, "bold": True}
    base_styles = [{"fontFamily": "Lato", "fontSize": 18.0}]
    text = "Written by an assistant\n"
    old = raw_shape("old", [("Written by an ", plain), ("assistant\n", bold)])
    (r,) = style_range_requests("new", old, raw_shape("new", [(text, plain)]), base_styles)
    rng = r["updateTextStyle"]["textRange"]
    assert text[rng["startIndex"]:rng["endIndex"]] == "assistant"
    assert rng["endIndex"] == len(text) - 1   # exactly the length `deleteText` would accept


SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"


def test_retitled_frame_and_right_limits_on_the_sync_talk(tmp_path):
    """On the sync test talk (tests/decks/sync): the unlabelled frame retitled Takeaways keeps its
    title key, and an element recreated on a slide whose title isn't rewritten gets the same box
    as in a fresh conversion (a title demoted to body text widened text_right_limit)."""
    from beamer2slides.sync import Sync, build_ours
    v1, untitled = SYNC_DECKS / "v1.pdf", SYNC_DECKS / "untitled.pdf"
    if not (v1.exists() and untitled.exists()):
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py, or pytest -m sync -k variants)")
    first = build_ours(v1, tmp_path / "v1", {"slides": []})
    second = build_ours(untitled, tmp_path / "untitled", {"slides": first["slides"]})
    last = second["slides"][-1]
    assert last["key"] == first["slides"][-1]["key"]
    assert [e["key"] for e in last["elements"] if e["role"] == "title"] == ["text/title/0"]

    s = Sync.__new__(Sync)
    s.ours, s.plan, s.scale, s.tok, s.urls, s.warnings = first, first["plan"], first["plan"].scale, "1zz", {}, []
    j = next(k for k, o in enumerate(first["slides"]) if o["key"] == "steps")
    elements = first["plan"].deck["slides"][j]["elements"]
    body = next(i for i, e in enumerate(elements) if e.get("role") == "body" and e["kind"] == "text")
    title = next(i for i, e in enumerate(elements) if e.get("role") == "title")

    def body_box(in_place):
        reqs, _, new_oid, _ = s.slide_requests({"plan": {"ours": j}, "units": [body]}, "LIVE", in_place, {}, {}, False)
        shape = next(r["createShape"] for r in reqs if r.get("createShape", {}).get("objectId") == new_oid[body])
        return shape["elementProperties"]["size"]["width"]["magnitude"]
    placeholder = {title: {"id": "LIVE_title", "size": [680.0, 36.0], "text": "The sync algorithm"}}
    assert body_box({}) == body_box(placeholder)

    # A unit whose group the deck took apart is rebuilt without its group.
    base_slide_ = copy.deepcopy(first["slides"][j])
    read = {"objects": {}, "notes": "", "notes_id": None}
    for e in base_slide_["elements"]:
        oid = f"OLD_{e['key'].replace('/', '_')}"
        e.update(main=oid, objects=[oid, f"{oid}_g"] if e["key"] == "text/body/0" else [oid])
        e["readback"] = {oid: readback([0, 0, 10, 10], "x")}
        read["objects"][oid] = readback([0, 0, 10, 10], "x", parent=None)
    s.base = {"slides": [base_slide_]}
    from collections import defaultdict
    s.urls = defaultdict(lambda: "https://example.com/staged.png")  # (the staging deck's picture URLs)
    members = [e["key"] for e in first["slides"][j]["elements"] if e["key"] == "text/body/0" or e.get("anchor") == "text/body/0"]
    plan = {"key": "steps", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": members, "base_members": members, "overrides": {}}]}

    def groups_made(live_objects):
        reqs = s.update_slide({"plan": plan, "units": []}, {**read, "objects": live_objects}, {}, {})
        return [r["groupObjects"]["groupObjectId"] for r in reqs if "groupObjects" in r]
    body_oid = "OLD_text_body_0"
    assert len(groups_made(read["objects"])) == 0  # the deck ungrouped it
    grouped = {k: {**v, "parent_group": f"{body_oid}_g" if k != f"{body_oid}_g" else None} for k, v in read["objects"].items()}
    grouped[f"{body_oid}_g"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup"}
    assert len(groups_made(grouped)) == 1


def test_unit_rebuilt_inside_a_group_nested_in_a_user_group(tmp_path):
    """A block (converter group) inside a group made in the deck: ungroup outermost first, regroup
    innermost first under the same ids (the rebuilt block title used to stay outside, ungrouped)."""
    from collections import defaultdict
    from beamer2slides.sync import Sync, build_ours
    v1 = SYNC_DECKS / "v1.pdf"
    if not v1.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours(v1, tmp_path / "v1", {"slides": []})
    s = Sync.__new__(Sync)
    s.ours, s.plan, s.scale, s.tok, s.warnings = first, first["plan"], first["plan"].scale, "1zz", []
    s.urls = defaultdict(lambda: "https://example.com/staged.png")
    j = next(k for k, o in enumerate(first["slides"]) if o["key"] == "policy")
    base_slide_ = copy.deepcopy(first["slides"][j])
    objects = {}
    for e in base_slide_["elements"]:
        oid = f"OLD_{e['key'].replace('/', '_')}"
        e.update(main=oid, objects=[oid], readback={oid: readback([0, 0, 10, 10], "x")})
        objects[oid] = readback([0, 0, 10, 10], "x")
    block = ["OLD_shape_panel_0", "OLD_shape_panel_1", "OLD_text_body_0", "OLD_text_body_1"]
    objects["BLK"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup", "children": block, "parent_group": "USER"}
    objects["USER"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup", "children": ["BLK", "OLD_text_body_2"]}
    for c in block:
        objects[c]["parent_group"] = "BLK"
    objects["OLD_text_body_2"]["parent_group"] = "USER"
    s.base = {"slides": [base_slide_]}
    plan = {"key": "policy", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": ["text/body/0"], "base_members": ["text/body/0"], "overrides": {}}]}
    w = {"plan": plan, "units": []}
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    assert [r["ungroupObjects"]["objectIds"] for r in reqs if "ungroupObjects" in r] == [["USER"], ["BLK"]]
    groups = [r["groupObjects"] for r in reqs if "groupObjects" in r]
    new_title = w["new_oid"][next(i for i, e in enumerate(first["slides"][j]["elements"]) if e["key"] == "text/body/0")]
    assert [g["groupObjectId"] for g in groups] == ["BLK", "USER"]
    assert new_title in groups[0]["childrenObjectIds"] and "OLD_text_body_0" not in groups[0]["childrenObjectIds"]
    assert groups[1]["childrenObjectIds"] == ["BLK", "OLD_text_body_2"]
    assert not s.warnings
    # Each group's members are stacked in its old order before it is made: the rebuilt title was
    # created last, so on top, and a group keeps the z-order its children had.
    for gi, r in enumerate(reqs):
        if "groupObjects" in r:
            kids = r["groupObjects"]["childrenObjectIds"]
            before = reqs[gi - len(kids):gi]
            assert [x["updatePageElementsZOrder"]["pageElementObjectIds"][0] for x in before] == kids
            assert all(x["updatePageElementsZOrder"]["operation"] == "BRING_TO_FRONT" for x in before)


def _table_sync(tmp_path, variant: str):
    """(run, first, second, j) for the Results table of the sync talk, v1 -> `variant`: run(margins,
    overrides) drives Sync.update_slide on it as a recreated unit, `margins(base element)` giving
    the base's table_margins, and returns (sync, work, requests, the table's old object id)."""
    from collections import defaultdict
    from beamer2slides.sync import Sync, build_ours
    v1, new = SYNC_DECKS / "v1.pdf", SYNC_DECKS / f"{variant}.pdf"
    if not v1.exists() or not new.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours(v1, tmp_path / "v1", {"slides": []})
    second = build_ours(new, tmp_path / "new", {"slides": []})
    j = next(k for k, o in enumerate(second["slides"]) if o["title"] == "Results")
    jb = next(k for k, o in enumerate(first["slides"]) if o["title"] == "Results")

    def run(margins, overrides=None, restored=()):
        s = Sync.__new__(Sync)
        s.ours, s.plan, s.scale, s.tok, s.warnings = second, second["plan"], second["plan"].scale, "1zz", []
        s.recovery = {"restore": {oid: {} for oid in restored}}
        s.urls = defaultdict(lambda: "https://example.com/staged.png")
        base_slide_ = copy.deepcopy(first["slides"][jb])
        objects = {}
        for e in base_slide_["elements"]:
            oid = f"OLD_{e['key'].replace('/', '_')}"
            rb = readback([0, 0, 10, 10], "x")
            if e["kind"] == "table":
                cells = [["".join(r["text"] for r in cell).strip() for cell in row] for row in e["ir"]["cells"]]
                rb = {**readback([0, 0, 10, 10], "\n".join("\t".join(row) for row in cells), kind="table"),
                      "table": [len(cells), len(cells[0])]}
                e["table_margins"] = margins(e)
            e.update(main=oid, objects=[oid], readback={oid: rb})
            objects[oid] = rb
        s.base = {"slides": [base_slide_]}
        key = next(e["key"] for e in base_slide_["elements"] if e["kind"] == "table")
        plan = {"key": "results", "base": 0, "ours": j, "objectId": "LIVE", "units": [
            {"key": key, "action": "recreate", "ours_members": [key], "base_members": [key], "overrides": overrides or {}}]}
        w = {"plan": plan, "units": []}
        return s, w, s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {}), f"OLD_{key.replace('/', '_')}"

    return run, first, second, j


# (a text box a new neighbour narrowed: tests/test_emitted_diff.py)


def test_a_table_whose_words_changed_is_refilled_in_place(tmp_path):
    """The source changed one cell of a table convert brought with the .pptx: the table keeps its
    object (and the cell margins the API can't set) and only its cells are rewritten; a table
    whose margins no longer fit (or with no margins recorded: made by createTable) is made again.
    A table the source moved as well is refilled and moved."""
    from beamer2slides.emit import pptx_table
    run, first, second, j = _table_sync(tmp_path, "tablecell")
    scale, fonts = first["plan"].scale, first["plan"].fonts

    s, w, reqs, old = run(lambda e: [list(m) for m in pptx_table(e["ir"], scale, fonts)["margins"]])
    assert not [r for r in reqs if "createTable" in r]
    assert old not in s.cleanup_ids and old in w["new_oid"].values()
    cleared = [r["deleteText"] for r in reqs if "deleteText" in r]
    assert cleared and all(d["objectId"] == old and "cellLocation" in d for d in cleared)
    assert any(r["insertText"]["text"] == "4.7 s" for r in reqs if "insertText" in r and r["insertText"]["objectId"] == old)
    assert not [r for r in reqs if "updateTableRowProperties" in r and r["updateTableRowProperties"]["objectId"] != old]
    assert not [r for r in reqs if "updatePageElementTransform" in r]
    # An interrupted sync already rewrote it (its read-back is the person's version put back): made again.
    s, w, reqs, old = run(lambda e: [list(m) for m in pptx_table(e["ir"], scale, fonts)["margins"]], restored=[old])
    assert [r for r in reqs if "createTable" in r] and old in s.cleanup_ids

    # The source moved it down 20 pt as well (a line added above): refilled, then moved by as much -
    # unless the deck moved it itself, whose position then stands (merge's geometry override).
    table = next(e for e in second["plan"].deck["slides"][j]["elements"] if e["kind"] == "table")
    table["bbox"][1] += 20
    table["bbox"][3] += 20
    table["frame"][1] += 20
    table["frame"][3] += 20
    table["row_baselines"] = [b + 20 for b in table["row_baselines"]]
    for rule in table.get("rules", []) + table.get("borders", []):
        if "y" in rule:
            rule["y"] += 20
    margins = lambda e: [list(m) for m in pptx_table(e["ir"], scale, fonts)["margins"]]  # noqa: E731
    s, w, reqs, old = run(margins)
    moved = [r["updatePageElementTransform"] for r in reqs if "updatePageElementTransform" in r]
    assert not [r for r in reqs if "createTable" in r] and old not in s.cleanup_ids
    assert len(moved) == 1 and moved[0]["objectId"] == old and moved[0]["applyMode"] == "RELATIVE"
    assert moved[0]["transform"]["translateY"] == pytest.approx(20 * scale * 12700, abs=2)
    assert abs(moved[0]["transform"]["translateX"]) <= 1
    s, w, reqs, old = run(margins, {"geometry": {"mode": "theirs"}})
    assert not [r for r in reqs if "updatePageElementTransform" in r] and old not in s.cleanup_ids

    for margins in (lambda e: None, lambda e: [[m[0], m[1] + 3, m[2], m[3]] for m in pptx_table(e["ir"], scale, fonts)["margins"]]):
        s, w, reqs, old = run(margins)
        assert [r for r in reqs if "createTable" in r] and old in s.cleanup_ids


def test_a_table_the_source_added_a_row_to_is_grown_in_place(tmp_path):
    """A row added at the end of the table: one insertTableRows below the last row (whose margins
    the new row takes), between emptying the cells and filling them; the base keeps the margins."""
    from beamer2slides.emit import pptx_table
    run, first, second, j = _table_sync(tmp_path, "table-row")
    scale, fonts = first["plan"].scale, first["plan"].fonts
    s, w, reqs, old = run(lambda e: [list(m) for m in pptx_table(e["ir"], scale, fonts)["margins"]])
    assert not [r for r in reqs if "createTable" in r] and old not in s.cleanup_ids
    kinds = [next(iter(r)) for r in reqs]
    grown = [r["insertTableRows"] for r in reqs if "insertTableRows" in r]
    assert grown == [{"tableObjectId": old, "cellLocation": {"rowIndex": 3, "columnIndex": 0}, "insertBelow": True, "number": 1}]
    assert kinds.index("insertTableRows") > max(k for k, x in enumerate(kinds) if x == "deleteText")
    assert kinds.index("insertTableRows") < kinds.index("insertText")
    assert any(r["insertText"]["text"] == "4.2 s" and r["insertText"]["cellLocation"]["rowIndex"] == 4
               for r in reqs if "insertText" in r and r["insertText"]["objectId"] == old)
    i = next(k for k, e in enumerate(second["plan"].deck["slides"][j]["elements"]) if e["kind"] == "table")
    assert len(w["in_place"][i]["margins"]) == 5


def test_table_steps_give_a_table_the_rows_and_columns_it_needs():
    from beamer2slides.sync import table_steps

    def m(*tops):
        return [[7.2, t, 7.2, 0.0] for t in tops]

    def kinds(reqs):
        return [(next(iter(r)), next(iter(r.values()))["cellLocation"]) for r in reqs]

    # A row added at the end of a booktabs table takes the margins of the row above it.
    reqs, cur = table_steps(m(5.0, 4.8, 0, 0), m(5.0, 4.8, 0, 0, 0), 3, 3)
    assert kinds(reqs) == [("insertTableRows", {"rowIndex": 3, "columnIndex": 0})] and cur == m(5.0, 4.8, 0, 0, 0)
    # ... and in the middle, below the row it copies; the margins stay the table's own.
    reqs, cur = table_steps(m(5.0, 4.8, 0), m(5.01, 4.8, 4.8, 0), 3, 3)
    assert kinds(reqs) == [("insertTableRows", {"rowIndex": 1, "columnIndex": 0})] and cur == m(5.0, 4.8, 4.8, 0)
    # A row removed; a column added and two removed at the right edge.
    reqs, _ = table_steps(m(5.0, 4.8, 0, 0), m(5.0, 4.8, 0), 3, 4)
    assert kinds(reqs) == [("deleteTableRow", {"rowIndex": 3, "columnIndex": 0}),
                           ("insertTableColumns", {"rowIndex": 0, "columnIndex": 2})]
    reqs, _ = table_steps(m(5.0, 0), m(5.0, 0), 4, 2)
    assert kinds(reqs) == [("deleteTableColumn", {"rowIndex": 0, "columnIndex": 3}),
                           ("deleteTableColumn", {"rowIndex": 0, "columnIndex": 2})]
    # A first row no row can give its margins to (a new rule above it): made again.
    assert table_steps(m(5.0, 0), m(3.0, 5.0, 0), 3, 3) is None
    assert table_steps(m(5.0, 0), m(5.0, 2.0), 3, 3) is None
    assert table_steps(m(5.0, 0), m(5.0, 0), 3, 3) == ([], m(5.0, 0))


def _picture_files(folder: Path, transparent: bool, mark=(10, 5, 40, 15), ground=(255, 255, 255)):
    """A small picture on a transparent or a painted ground, written to <folder>/figures/f1.png."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (60, 20), (*ground, 0 if transparent else 255))
    ImageDraw.Draw(img).rectangle(mark, fill=(0, 0, 0, 255))
    if not transparent:
        img = img.convert("RGB")
    (folder / "figures").mkdir(parents=True, exist_ok=True)
    img.save(folder / "figures" / "f1.png")
    return folder


def _picture_deck(deck_out: Path, ours_out: Path):
    """A one-slide base with a title and an anchored picture, and ours from `ours_out`'s files."""
    ir = {"id": "p0i0", "kind": "image", "role": "math", "bbox": [20.0, 60.0, 80.0, 80.0], "file": "figures/f1.png",
          "alt": None}

    def element(out: Path, oid=None):
        h, fields = identity.ir_fields(ir, out)
        el = {"key": "image/math/0", "id": ir["id"], "kind": "image", "role": "math", "ir_hash": h, "fields": fields,
              "fingerprint": identity.fingerprint(ir, out), "anchor": None, "ir": ir}
        if oid:
            el.update(objects=[oid], main=oid,
                      readback={oid: readback([40, 120, 160, 160], kind="image", image="c1")})
        return el
    title = entry("text/title/0", text_ir("Figures", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0")
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None,
            "slides": [base_slide("figs", "b2s_s000", [title, element(deck_out, "b2s_s000_i0")], title="Figures")]}
    ours = {"slides": [{**ours_of(base["slides"][0]),
                        "elements": [{k: v for k, v in title.items() if k not in ("objects", "main", "readback")},
                                     element(ours_out)]}],
            "pairs": {0: 0}, "out": ours_out}
    return base, ours, {"revisionId": "r", "slides": [live(base["slides"][0])]}


def test_a_picture_written_differently_is_no_source_change(tmp_path):
    """Anchored pictures became RGBA (a transparent ground instead of a white one): the file sha1
    changes though the slide looks the same. Rewriting every one of them on the first sync after
    that would churn the deck, so the base takes the new hash and nothing is written."""
    from beamer2slides import snapshot
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=True)
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    el, oe = base["slides"][0]["elements"][1], ours["slides"][0]["elements"][1]
    assert identity.source_changes(el, oe) == {"image"}
    assert merge.has_writes(merge.plan_merge(copy.deepcopy(base), ours, theirs), ["b2s_s000"])
    assert snapshot.refresh_pictures(base, ours, deck_out) == [{"slide": "figs", "element": "image/math/0"}]
    assert identity.source_changes(el, oe) == set()
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "keep"
    assert not merge.has_writes(mplan, ["b2s_s000"])


def test_a_picture_that_really_changed_is_still_rewritten(tmp_path):
    from beamer2slides import snapshot
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=True, mark=(10, 5, 50, 15))
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    assert snapshot.refresh_pictures(base, ours, deck_out) == []
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "recreate"


def test_the_ground_a_picture_dropped_must_be_flat(tmp_path):
    """The older picture painted the page under it: the same picture on a transparent ground is
    the same only where what it dropped was that one colour."""
    from beamer2slides.snapshot import same_picture_file
    clear = _picture_files(tmp_path / "clear", transparent=True) / "figures" / "f1.png"
    for ground in ((255, 255, 255), (238, 245, 255)):
        painted = _picture_files(tmp_path / f"g{ground[0]}", transparent=False, ground=ground) / "figures" / "f1.png"
        assert same_picture_file(painted, clear)
    # the older picture had a second mark where the new one is clear: not the same picture
    from PIL import Image, ImageDraw
    busy = Image.open(painted).convert("RGB")
    ImageDraw.Draw(busy).rectangle((45, 2, 58, 18), fill=(0, 0, 0))
    busy.save(tmp_path / "busy.png")
    assert not same_picture_file(tmp_path / "busy.png", clear)


def _image_entry(out: Path, key="image/figure/0", bbox=(20.0, 60.0, 80.0, 80.0), oid=None):
    ir = {"id": "p0i9", "kind": "image", "role": "figure", "bbox": list(bbox), "file": "figures/f1.png", "alt": None}
    h, fields = identity.ir_fields(ir, out)
    el = {"key": key, "id": ir["id"], "kind": "image", "role": "figure", "ir_hash": h, "fields": fields,
          "fingerprint": identity.fingerprint(ir, out), "anchor": None, "ir": ir}
    if oid:
        el.update(objects=[oid], main=oid, readback={oid: readback([2 * v for v in bbox], kind="image", image="c1")})
    return el


def test_a_picture_the_deck_already_shows_is_adopted_not_duplicated(tmp_path):
    """After a pull the source draws a picture the person put into the deck: keep their object
    (their crop, rotation and outline) instead of creating a second one next to it."""
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    base = three_slides()
    slides = [ours_of(s) for s in base["slides"]]
    slides[0]["elements"].append(_image_entry(ours_out))
    ours = {"slides": slides, "pairs": {0: 0, 1: 1, 2: 2}, "out": ours_out}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    theirs["slides"][0]["objects"]["USERPIC"] = readback([40, 120, 160, 160], kind="image", image="c9")
    live_ids = [s["objectId"] for s in theirs["slides"]]
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "create" and merge.has_writes(plain, live_ids)
    assert [u["objectId"] for u in plain["report"]["user_objects"]] == ["USERPIC"]
    asked = []

    def adopt(skey, members, read, oid=None):
        asked.append((skey, [m["key"] for m in members], oid))
        return "USERPIC" if oid is None else None
    mplan = merge.plan_merge(base, ours, theirs, adopt)
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["objectId"]) == ("adopt_object", "USERPIC")
    assert not merge.has_writes(mplan, live_ids)
    assert not mplan["report"]["user_objects"]  # (the object belongs to the source's element now)
    assert [(c["element"], c["field"]) for c in mplan["report"]["converged"]] == [("image/figure/0", "image")]
    assert asked == [("intro", ["image/figure/0"], None)]


def test_the_deck_picture_the_source_now_draws_is_no_conflict(tmp_path):
    """The person replaced a drawn figure with their own picture and pull put that picture in the
    source: the deck's object is what the source draws now, so nothing is written or reported as
    a conflict."""
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=False, mark=(12, 4, 44, 16))
    base = three_slides()
    base["slides"][0]["elements"].append(_image_entry(deck_out, oid="b2s_s000_i0"))
    slides = [ours_of(s) for s in base["slides"]]
    slides[0]["elements"][-1] = _image_entry(ours_out)
    ours = {"slides": slides, "pairs": {0: 0, 1: 1, 2: 2}, "out": ours_out}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    theirs["slides"][0]["objects"]["b2s_s000_i0"]["image"] = {"contentHash": "c2"}  # replaced in the deck
    live_ids = [s["objectId"] for s in theirs["slides"]]
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "keep"
    assert [c["field"] for c in plain["report"]["conflicts"]] == ["image"]
    mplan = merge.plan_merge(base, ours, theirs, lambda skey, members, read, oid=None: oid)
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["adopt"]) == ("adopt", ["image"])
    assert not mplan["report"]["conflicts"] and not merge.has_writes(mplan, live_ids)
    assert [(c["element"], c["field"]) for c in mplan["report"]["converged"]] == [("image/figure/0", "image")]


def test_the_adopter_matches_by_bytes_and_by_look(tmp_path, monkeypatch):
    """sync.picture_adopter on a live slide: the same bytes or the same look in the right place,
    and never a converter object, a copy or a picture inside a group."""
    from beamer2slides import snapshot
    from beamer2slides.sync import Sync, box_overlap
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    scaled = _picture_files(tmp_path / "scaled", transparent=False)  # (the same drawing at twice the size)
    from PIL import Image
    with Image.open(ours_out / "figures" / "f1.png") as img:
        img.resize((120, 40), Image.LANCZOS).save(scaled / "figures" / "f1.png")
    crop = _picture_files(tmp_path / "crop", transparent=False)  # (the converter renders the region again)
    with Image.open(ours_out / "figures" / "f1.png") as img:
        padded = Image.new("RGB", (66, 24), "white")
        padded.paste(img, (3, 2))
        padded.resize((132, 48), Image.LANCZOS).save(crop / "figures" / "f1.png")
    other = _picture_files(tmp_path / "other", transparent=False, mark=(2, 2, 58, 18))
    files = {"u_same": ours_out, "u_look": scaled, "u_crop": crop, "u_other": other}
    monkeypatch.setattr(snapshot, "_download", lambda url: (files[url] / "figures" / "f1.png").read_bytes())
    pres = {"slides": [{"objectId": "S", "pageElements": [{"objectId": oid, "image": {"contentUrl": url}}
                                                          for oid, url in [("SAME", "u_same"), ("LOOK", "u_look"),
                                                                           ("CROP", "u_crop"),
                                                                           ("OTHER", "u_other"), ("COPY", "u_same"),
                                                                           ("INGROUP", "u_same"), ("B2S", "u_same")]]}]}
    s = Sync.__new__(Sync)
    s.ours, s.scale = {"out": ours_out}, 2.0
    s.base = {"slides": [{"key": "intro", "objectId": "S", "groups": [],
                          "elements": [{"key": "image/figure/0", "objects": ["B2S"]}]}]}
    boxes = {"SAME": [40, 120, 160, 160], "LOOK": [44, 124, 164, 164], "CROP": [38, 118, 162, 162],
             "OTHER": [40, 120, 160, 160], "COPY": [40, 120, 160, 160], "INGROUP": [40, 120, 160, 160],
             "B2S": [40, 120, 160, 160]}
    read = {"objects": {oid: readback(box, kind="image", image=oid) for oid, box in boxes.items()}}
    read["objects"]["COPY"]["title"] = "b2s:intro/image/figure/0"
    read["objects"]["INGROUP"]["parent_group"] = "G"
    el = _image_entry(ours_out)
    assert box_overlap([20, 60, 80, 80], [20, 60, 80, 80]) == 1.0 and box_overlap([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    # the same bytes win; the same look at another resolution is taken too, a different picture isn't
    assert s.picture_adopter(pres)("intro", [el], read) == "SAME"
    del read["objects"]["SAME"]
    # the crop the converter rendered again correlates too little for same_look (0.94 live)
    from beamer2slides.inverse import picture_look, same_look
    assert not same_look(picture_look(ours_out / "figures" / "f1.png"), picture_look(crop / "figures" / "f1.png"))
    assert s.picture_adopter(pres)("intro", [el], read) == "CROP"  # (the closest box of the two that match)
    del read["objects"]["CROP"]
    assert s.picture_adopter(pres)("intro", [el], read) == "LOOK"
    for oid in ("LOOK", "COPY", "INGROUP", "B2S"):
        del read["objects"][oid]
    assert s.picture_adopter(pres)("intro", [el], read) is None
    # far from the element's box: not the same unit
    read["objects"]["SAME"] = readback([400, 300, 520, 340], kind="image", image="c1")
    assert s.picture_adopter(pres)("intro", [el], read) is None
    # a picture the deck put in place of the element's own object is found by id
    assert s.picture_adopter(pres)("intro", [el], read, "SAME") == "SAME"
    assert s.picture_adopter(pres)("intro", [el], read, "OTHER") is None


def test_a_new_slide_inherits_the_master_background(tmp_path):
    """emit leaves the slides with the shared background inheriting the master (now often a plain
    ground colour, with the theme decoration on the layouts), so a slide sync creates must inherit
    it too: an explicit fill of its own differs from a fresh conversion and stops following the
    deck's theme."""
    from beamer2slides.sync import Sync
    s = Sync.__new__(Sync)
    s.base = {"master_background": "color:#ffffff"}
    s.ours = {"out": tmp_path}
    s.urls = {str(tmp_path / "backgrounds" / "bg-3.png"): "https://content/3"}
    pres = {"masters": [{"pageProperties": {"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}]}
    assert s.background_requests("S", "color:#ffffff", {}, pres, created=True) == []
    kept = s.background_requests("S", "color:#ffffff", {}, pres)  # an edited slide goes back to it
    assert kept[0]["updatePageProperties"]["fields"] == "pageBackgroundFill.solidFill.color"
    own = s.background_requests("S", "color:#102030", {}, pres, created=True)
    assert own[0]["updatePageProperties"]["pageProperties"]["pageBackgroundFill"]["solidFill"]["color"] == \
        {"rgbColor": {"red": 16 / 255, "green": 32 / 255, "blue": 48 / 255}}
    picture = s.background_requests("S", "png:abc", {"background": "backgrounds/bg-3.png"}, pres, created=True)
    assert picture[0]["updatePageProperties"]["pageProperties"]["pageBackgroundFill"] == \
        {"stretchedPictureFill": {"contentUrl": "https://content/3"}}


def test_a_new_slide_lands_on_the_layout_of_its_background(tmp_path):
    """The theme decoration now sits on the layouts, and backgrounds that don't show it got a copy
    of their layout without it. A new slide with a background picture of its own therefore goes on
    the layout of the converted slides with that background, not on the decorated one."""
    from beamer2slides.snapshot import background_key
    from beamer2slides.sync import Sync
    (tmp_path / "backgrounds").mkdir()
    for name, colour in (("bg-000.png", (250, 250, 250)), ("bg-003.png", (20, 20, 40)), ("bg-new.png", (7, 7, 7))):
        from PIL import Image
        Image.new("RGB", (16, 9), colour).save(tmp_path / "backgrounds" / name)
    page = lambda name: {"background": f"backgrounds/{name}"}
    main, standout = (background_key(page(n), tmp_path) for n in ("bg-000.png", "bg-003.png"))
    s = Sync.__new__(Sync)
    s.ours = {"out": tmp_path}
    s.base = {"master_background": main, "slides": [
        {"key": "a", "layout": "TITLE_ONLY", "background": main, "layoutObjectId": "L_main"},
        {"key": "b", "layout": "TITLE_ONLY", "background": standout, "layoutObjectId": "L_plain"},
        {"key": "c", "layout": "BLANK", "background": standout, "layoutObjectId": "L_blank"}]}
    pres = {"layouts": [{"objectId": "L_main", "layoutProperties": {"name": "TITLE_ONLY"}},
                        {"objectId": "L_plain", "layoutProperties": {"name": "Title Only (no theme)"}},
                        {"objectId": "L_blank", "layoutProperties": {"name": "Blank (no theme)"}},
                        {"objectId": "L_bare", "layoutProperties": {"name": "BLANK"}}]}
    layouts = {l["layoutProperties"]["name"]: l for l in pres["layouts"]}
    pick = lambda name, kind="TITLE_ONLY": s.new_layout(page(name), kind, layouts, pres)["objectId"]
    assert pick("bg-000.png") == "L_main"       # the master background: the layout draws the decoration
    assert pick("bg-003.png") == "L_plain"      # a standout frame: the copy without it
    assert pick("bg-003.png", "BLANK") == "L_blank"
    assert pick("bg-new.png") == "L_main"       # unknown: the named layout, as before


def test_conversion_is_stable():
    """Converting the same PDF twice gives the same keys and hashes (a no-op sync sends nothing)."""
    import tempfile
    from beamer2slides.sync import build_ours
    pdf = DECKS / "sync_smoke_v1.pdf"
    if not pdf.exists():
        pytest.skip("build the test decks first (tests/decks/build.py sync_smoke_v1)")
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = build_ours(pdf, Path(a), {"slides": []})
        base = {"slides": first["slides"]}
        second = build_ours(pdf, Path(b), base)
    assert second["pairs"] == {j: j for j in range(len(first["slides"]))}
    assert [(s["key"], [(e["key"], e["ir_hash"]) for e in s["elements"]]) for s in second["slides"]] == \
        [(s["key"], [(e["key"], e["ir_hash"]) for e in s["elements"]]) for s in first["slides"]]


def test_a_stand_in_is_made_at_the_size_slides_keeps():
    """Slides stores a new shape at 3,000,000 EMU whatever size is asked for, and keeps the asked
    size in the scale; a copy of the stand-in is then scaled ABSOLUTE by box / STAND_IN, which is only
    the box when the stand-in's own size is what Slides kept. At 100 pt the copy was 2.36 times too
    big: a block's title bar came back 1649 pt wide on a 720 pt page (live, the front page's demo).
    (tests/slides_sim.py neither normalises sizes nor copies objects, so only this sees it.)"""
    from beamer2slides.sync import STAND_IN, stand_in_request
    size = stand_in_request("b2s_x_k0_1aa", "s", ("ROUND_2_SAME_RECTANGLE",))["createShape"]["elementProperties"]["size"]
    assert size["width"]["magnitude"] == size["height"]["magnitude"] == 3_000_000
    assert size["width"].get("unit", "EMU") == "EMU"
    box_w = 698.0
    assert round(box_w / STAND_IN * 3_000_000 / 12700, 6) == box_w
