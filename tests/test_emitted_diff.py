"""Sync compares what emit would write, not only what classify read (`sync.mark_emitted`): an
element whose own IR the source left alone can still be written differently because of what stands
around it. Offline: the built decks (tests/decks/build.py) and the sync test talk
(tests/decks/sync/build.py), no Google."""

import copy
import json
import re
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from beamer2slides import emit, identity, sync
from beamer2slides.classify import classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.notes import prepare

from .test_emit_requests import emitted
from .test_sync import entry, shape_ir, text_ir

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"
SCALE = 2.0


# ---------------------------------------------------------------- helpers

def entries_of(deck: dict) -> list[dict]:
    """Base-like slide entries of a planned deck (keys, hashes, IR), as `snapshot.slide_entries`
    makes them but without the background files."""
    keys = identity.slide_keys([identity.slide_info(s) for s in deck["slides"]])
    out = []
    for slide, key in zip(deck["slides"], keys):
        ekeys, _ = identity.slide_element_keys(slide["elements"], None)
        ids = {e["id"]: k for e, k in zip(slide["elements"], ekeys)}
        elements = []
        for el, k in zip(slide["elements"], ekeys):
            h, fields = identity.ir_fields(el, None, ids.get(el.get("anchor")))
            elements.append({"key": k, "id": el["id"], "kind": el["kind"], "role": el.get("role"), "ir_hash": h,
                             "fields": fields, "anchor": ids.get(el.get("anchor")), "ir": el})
        out.append({"key": key, "page": slide["page"], "layout": emit.slide_layout(slide)[0], "elements": elements})
    return out


def renumbered(value, by: int):
    """The IR as another conversion numbers it: element ids (and anchors) on page N + `by`."""
    if isinstance(value, dict):
        return {k: (re.sub(r"^p(\d+)", lambda m: f"p{int(m.group(1)) + by}", v) if k in ("id", "anchor") and isinstance(v, str)
                    else renumbered(v, by)) for k, v in value.items()}
    if isinstance(value, list):
        return [renumbered(v, by) for v in value]
    return value


def as_saved(base: list[dict], by: int = 7) -> dict:
    """A base as it comes back from Drive: JSON, from a conversion where the slide stood elsewhere."""
    return json.loads(json.dumps({"slides": [{**s, "page": s["page"] + by, "elements": renumbered(s["elements"], by)}
                                             for s in base]}))


@lru_cache(maxsize=None)
def sync_talk() -> tuple:
    """(name, planned deck) of the sync test talk and its variants: extracted and classified only."""
    out = []
    for pdf in sorted(SYNC_DECKS.glob("*.pdf")):
        with tempfile.TemporaryDirectory() as tmp:
            prepared = prepare(pdf, Path(tmp))
            raw = extract(prepared.pdf, prepared.labels)
            deck = classify(select_overlays(raw, "last"))
        plan = emit.DeckPlan({**deck, "slides": [{**s, "elements": emit.merge_blocks(s["elements"])} for s in deck["slides"]]})
        out.append((pdf.stem, plan))
    return tuple(out)


def same_emission(name: str, plan) -> list[str]:
    """Every slide of a deck against a saved base made of its own entries, the fast path off:
    what emit writes must come out the same element by element, and nothing may be marked."""
    deck, found = plan.deck, []
    ours = entries_of(deck)
    base = as_saved(ours)
    for j, (o, b, slide) in enumerate(zip(ours, base["slides"], deck["slides"])):
        names = [e["key"] for e in o["elements"]]
        was_slide = {**slide, "page": b["page"], "elements": [e["ir"] for e in b["elements"]]}
        was = json.loads(json.dumps(sync.emitted_elements(was_slide, names, plan.scale, plan.fonts)))
        now = json.loads(json.dumps(sync.emitted_elements(slide, names, plan.scale, plan.fonts)))
        found += [f"{name} slide {j + 1} {k}: emitted differently" for k, a, c in zip(names, was, now) if a != c]
        found += [f"{name} slide {j + 1} {k}: {sorted(sync.context_changes(a, c, [0.0, 0.0], e['kind'])[0])}"
                  for k, a, c, e in zip(names, was, now, o["elements"]) if any(sync.context_changes(a, c, [0.0, 0.0], e["kind"]))]
    unwritten = sync.mark_emitted(base, ours, deck, {j: j for j in range(len(ours))}, plan.scale, plan.fonts, fast=False)
    found += [f"{name}: unwritten {u}" for u in unwritten]
    found += [f"{name} {s['key']} {e['key']}: marked {f}" for s in ours + base["slides"] for e in s["elements"]
              for f in identity.CONTEXT_FIELDS if f in e["fields"]]
    return found


# ---------------------------------------------------------------- no false positives

@pytest.mark.xdist_group("tests/test_emit_requests.py")  # (its decks, classified once per worker)
def test_every_built_deck_emits_the_same_against_its_own_saved_base():
    """Object ids, element numbering, the slide's page and a JSON round trip are nothing emit writes
    differently: a deck against a base of its own entries, renumbered and saved, marks nothing -
    with the fast path (which would skip every slide here) turned off."""
    decks = emitted()
    if not decks:
        pytest.skip("no PDFs built")
    found = [p for d in decks for p in same_emission(d.name, d.plan)]
    assert not found, f"{len(found)} false positive(s):\n" + "\n".join(found[:40])


def test_the_sync_talk_emits_the_same_against_its_own_saved_base():
    talk = sync_talk()
    if not talk:
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    found = [p for name, plan in talk for p in same_emission(name, plan)]
    assert not found, f"{len(found)} false positive(s):\n" + "\n".join(found[:40])


# ---------------------------------------------------------------- what it finds

def marks(base: dict, ours: list[dict], slide: dict, scale: float = SCALE, fonts=None) -> tuple[dict, list[dict]]:
    """{element key: the context fields `source_changes` reports} of one slide, and what cannot be written."""
    unwritten = sync.mark_emitted(base, ours, {"slides": [slide]}, {0: 0}, scale, fonts or emit.FontMapper())
    base_by = {e["key"]: e for e in base["slides"][0]["elements"]}
    found = {}
    for oe in ours[0]["elements"]:
        if oe["key"] in base_by:
            got = identity.source_changes(base_by[oe["key"]], oe) & set(identity.CONTEXT_FIELDS)
            if got:
                found[oe["key"]] = got
    return found, unwritten


def one_slide(elements: dict[str, dict], title_page: bool = False) -> tuple[dict, dict]:
    """(slide entry, planned slide) of synthetic IR elements, keyed by name."""
    irs = list(elements.values())
    slide = {"page": 0, "size": [360.0, 270.0], "title_page": title_page, "elements": irs}
    return {"key": "s", "page": 0, "layout": emit.slide_layout(slide)[0],
            "elements": [entry(k, ir) for k, ir in elements.items()]}, slide


def test_a_title_whose_box_a_new_neighbour_narrowed_is_a_source_change(tmp_path):
    """A one-line box reaches to the mirror of the slide's leftmost body text (emit.text_right_limit),
    so the centred line the table-moved variant adds narrows the Results title a fresh conversion
    makes, though the title's own IR is the same: sync kept the old box (live scenario table-moved,
    707 against 474 pt). The title is now a `width` change - and nothing is where nothing moved."""
    v1, moved = SYNC_DECKS / "v1.pdf", SYNC_DECKS / "table-moved.pdf"
    if not v1.exists() or not moved.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = sync.build_ours(v1, tmp_path / "v1", {"slides": []})

    def changes(pdf, name):
        base = {"slides": copy.deepcopy(first["slides"])}
        ours = sync.build_ours(pdf, tmp_path / name, base)
        out = {}
        for j, i in ours["pairs"].items():
            base_by = {e["key"]: e for e in base["slides"][i]["elements"]}
            for oe in ours["slides"][j]["elements"]:
                if oe["key"] in base_by and set(identity.CONTEXT_FIELDS) & identity.source_changes(base_by[oe["key"]], oe):
                    out[(ours["slides"][j]["title"], oe["key"])] = identity.source_changes(base_by[oe["key"]], oe)
        return out, ours["context_unwritten"]

    assert changes(moved, "moved") == ({("Results", "text/title/0"): {"width"}}, [])
    assert changes(v1, "same") == ({}, [])
    # a mark an earlier sync left in the base (new_base copies ours' fields) says nothing by itself
    base = {"slides": copy.deepcopy(first["slides"])}
    for s in base["slides"]:
        for e in s["elements"]:
            e["fields"] = {**e["fields"], **{f: "ours" for f in identity.CONTEXT_FIELDS}}
    again = sync.build_ours(v1, tmp_path / "again", base)
    assert not any(set(identity.CONTEXT_FIELDS) & identity.source_changes(b, o) for j, i in again["pairs"].items()
                   for o in again["slides"][j]["elements"] for b in base["slides"][i]["elements"] if b["key"] == o["key"])


def test_a_picture_beside_a_line_narrows_its_box():
    """A one-line box stops short of anything to its right on its lines: a picture the source put
    there narrows the text a fresh conversion makes. The text is a `width` change, the heading
    above it (other lines) is not."""
    heading = text_ir("Heading", [20, 20, 120, 34], "p0t0")
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1")
    base, slide0 = one_slide({"text/body/0": heading, "text/body/1": line})
    picture = {"id": "p0f2", "kind": "image", "role": "figure", "bbox": [200, 55, 300, 80], "file": "figures/x.png"}
    ours, slide = one_slide({"text/body/0": heading, "text/body/1": line, "image/figure/0": picture})
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert found == {"text/body/1": {"width"}} and unwritten == []


def test_a_moved_element_is_compared_less_its_own_step():
    """The source moved everything down 30 pt (a line added above): each box moves by as much and
    says nothing else new, so nothing is marked - `merge`'s move is the whole change."""
    irs = {"text/body/0": text_ir("First line", [20, 60, 90, 72], "p0t0"),
           "shape/panel/0": shape_ir([10, 100, 200, 160], "p0s1")}
    base, _ = one_slide(irs)
    down = {k: {**v, "bbox": [v["bbox"][0], v["bbox"][1] + 30, v["bbox"][2], v["bbox"][3] + 30]} for k, v in irs.items()}
    down["text/body/0"]["paragraphs"] = [{**p, "lines": [{**ln, "baseline": ln["baseline"] + 30} for ln in p["lines"]]}
                                         for p in down["text/body/0"]["paragraphs"]]
    added = text_ir("A new line above", [20, 30, 120, 42], "p0t2")
    ours, slide = one_slide({"text/body/9": added, **down})
    assert all(identity.source_changes(b, o) == {"position"} for b, o in zip(base["elements"], ours["elements"][1:]))
    assert marks({"slides": [base]}, [ours], slide) == ({}, [])


def test_a_new_text_on_a_block_changes_its_grouping_which_sync_cannot_write():
    """emit groups a block's panels with what lies on them (emit.block_groups). A text the source
    added onto a block makes the block's other members' groups another one - which recreating them
    would not write (a recreated unit goes back into its old group): reported, not marked."""
    bar, body = {**shape_ir([10, 40, 200, 55], "p0s0"), "block": 0}, {**shape_ir([10, 55, 200, 120], "p0s1"), "block": 0}
    first = text_ir("Inside", [20, 60, 90, 72], "p0t2")
    base, _ = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first})
    ours, slide = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first,
                             "text/body/1": text_ir("Also inside", [20, 90, 100, 102], "p0t3")})
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert found == {}
    assert {u["element"]: u["fields"] for u in unwritten} == \
        {"shape/panel/0": ["grouping"], "shape/panel/1": ["grouping"], "text/body/0": ["grouping"]}


def test_a_longer_text_below_the_title_takes_the_subtitle_placeholder_which_sync_cannot_write():
    """On the title page the biggest plain text below the title goes into the layout's subtitle
    placeholder (emit.subtitle_element). A longer one the source added takes it from the authors'
    line, which becomes a box of its own in a fresh conversion - a placeholder change a unit's
    recreation cannot make (`Sync.slide_requests` would create a box under the placeholder's id):
    reported, not marked."""
    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    authors = text_ir("Someone", [20, 60, 90, 72], "p0t1")
    base, _ = one_slide({"text/title/0": title, "text/body/0": authors}, title_page=True)
    ours, slide = one_slide({"text/title/0": title, "text/body/0": authors,
                             "text/body/1": text_ir("A much longer institute line", [20, 90, 200, 102], "p0t2")},
                            title_page=True)
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert "text/body/0" not in found
    assert [u for u in unwritten if u["element"] == "text/body/0"] == [
        {"slide": "s", "element": "text/body/0", "fields": ["placeholder"]}]


@pytest.mark.xfail(strict=True, reason="Sync.update_slide: a recreated old subtitle is created under the live "
                                        "SUBTITLE placeholder's id when another text takes the subtitle role")
def test_a_reworded_subtitle_that_lost_the_placeholder_is_not_created_under_its_id():
    """Why a subtitle change is reported and not marked: the source rewords the authors' line and
    adds a longer line below it, which is the subtitle now. The recreated authors' line goes into
    its old placeholder (`in_place`) but is emitted as a box of its own, so the request is a
    createShape under the live placeholder's object id, which Slides refuses with the whole batch.
    (Found writing this file; the fix belongs to `Sync.update_slide`/`slide_requests`.)"""
    from collections import defaultdict

    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    old, new = text_ir("Someone", [20, 60, 90, 72], "p0t1"), text_ir("Someone else", [20, 60, 100, 72], "p0t1")
    longer = text_ir("A much longer institute line", [20, 90, 200, 102], "p0t2")
    plan = emit.DeckPlan({"slides": [{"page": 0, "size": [360.0, 270.0], "title_page": True, "elements": [title, new, longer]}]}, 720.0)
    b = {"key": "s", "elements": [entry("text/title/0", title, "TITLE_PH"), entry("text/body/0", old, "SUB_PH")]}
    objects = {oid: rb for e in b["elements"] for oid, rb in e["readback"].items()}
    objects["TITLE_PH"]["placeholder"], objects["SUB_PH"]["placeholder"] = "TITLE", "SUBTITLE"
    s = sync.Sync.__new__(sync.Sync)
    s.ours = {"slides": [{"key": "s", "elements": [entry("text/title/0", title), entry("text/body/0", new),
                                                   entry("text/body/1", longer)]}], "out": Path(".")}
    s.plan, s.scale, s.tok, s.warnings, s.base = plan, plan.scale, "1zz", [], {"slides": [b]}
    s.recovery, s.urls = {"restore": {}}, defaultdict(lambda: "https://example.com/x.png")
    w = {"plan": {"key": "s", "base": 0, "ours": 0, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": ["text/body/0"], "base_members": ["text/body/0"], "overrides": {}},
        {"key": "text/body/1", "action": "create", "ours_members": ["text/body/1"]}]}, "units": []}
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    assert not [r for r in reqs if r.get("createShape", {}).get("objectId") in objects]


def test_an_old_base_emit_cannot_read_marks_nothing():
    """A base IR an older converter wrote, missing what today's emit reads, says nothing either way."""
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1")
    base, _ = one_slide({"text/body/1": copy.deepcopy(line)})
    for p in base["elements"][0]["ir"]["paragraphs"]:
        del p["lines"]
    picture = {"id": "p0f2", "kind": "image", "role": "figure", "bbox": [200, 55, 300, 80], "file": "figures/x.png"}
    ours, slide = one_slide({"text/body/1": line, "image/figure/0": picture})
    assert marks({"slides": [base]}, [ours], slide) == ({}, [])
