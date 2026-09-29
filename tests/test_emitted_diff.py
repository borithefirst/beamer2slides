"""Sync compares what emit would write, not only what classify read (`sync.mark_emitted`): an
element whose own IR the source left alone can still be written differently because of what stands
around it. Offline: the built decks (tests/decks/build.py) and the sync test talk
(tests/decks/sync/build.py), no Google."""

import copy
import json
import re
import tempfile
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import pytest

from beamer2slides.emit import SLIDE_W
from beamer2slides import emit, identity, snapshot, sync
from beamer2slides.classify import classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.ir import deck_json
from beamer2slides.json_types import Json, JsonObject, as_optional_str
from beamer2slides.notes import prepare

from . import sync_work
from .json_reads import jint,jnum, jnums, jobj, jobjs, jstr
from .test_emit_requests import emitted
from .test_sync import entry, text_ir
from .test_sync import shape_ir as bare_shape_ir

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"
SCALE = 2.0
# A figure as render leaves it (emit parses each element, so a picture carries what render writes).
PICTURE: JsonObject = {"id": "p0f2", "kind": "image", "role": "figure", "bbox": [200, 55, 300, 80], "spans": [],
                       "file": "figures/x.png", "px": [200, 50]}


def shape_ir(bbox: Sequence[float], eid: str) -> JsonObject:
    """test_sync's panel with the fields classify writes that its merge tests do without."""
    return {**bare_shape_ir(bbox, eid, fill="#dddddd"), "spans": [], "drawing": f"d-{eid}"}


# ---------------------------------------------------------------- helpers

def entries_of(deck: JsonObject) -> list[JsonObject]:
    """Base-like slide entries of a planned deck (keys, hashes, IR), as `snapshot.slide_entries`
    makes them but without the background files."""
    slides = jobjs(deck, "slides")
    keys = identity.slide_keys([identity.slide_info(s) for s in slides])
    out: list[JsonObject] = []
    for slide, key in zip(slides, keys):
        irs = jobjs(slide, "elements")
        ekeys, _ = identity.slide_element_keys(irs, None)
        ids = {jstr(e, "id"): k for e, k in zip(irs, ekeys)}
        elements: list[Json] = []
        for el, k in zip(irs, ekeys):
            anchor = el.get("anchor")
            anchor_key = ids.get(anchor) if isinstance(anchor, str) else None
            h, fields = identity.ir_fields(el, None, anchor_key)
            elements.append({"key": k, "id": el["id"], "kind": el["kind"], "role": el.get("role"), "ir_hash": h,
                             "fields": fields, "anchor": anchor_key, "ir": el})
        out.append({"key": key, "page": slide["page"], "layout": emit.slide_layout(slide)[0], "elements": elements})
    return out


def renumbered(value: Json, by: int) -> Json:
    """The IR as another conversion numbers it: element ids (and anchors) on page N + `by`."""
    def page(m: re.Match[str]) -> str:
        return f"p{int(m.group(1)) + by}"

    if isinstance(value, dict):
        out: JsonObject = {}
        for k, v in value.items():
            out[k] = re.sub(r"^p(\d+)", page, v) if k in ("id", "anchor") and isinstance(v, str) else renumbered(v, by)
        return out
    if isinstance(value, list):
        return [renumbered(v, by) for v in value]
    return value


def as_saved(base: list[JsonObject], by: int) -> JsonObject:
    """A base as it comes back from Drive: JSON, from a conversion where the slide stood elsewhere."""
    slides: list[Json] = [{**s, "page": jint(s, "page") + by, "elements": renumbered(s["elements"], by)} for s in base]
    saved: Json = json.loads(json.dumps({"slides": slides}))
    return jobj(saved)


@lru_cache(maxsize=None)
def sync_talk() -> tuple[tuple[str, emit.DeckPlan], ...]:
    """(name, planned deck) of the sync test talk and its variants: extracted and classified only."""
    out: list[tuple[str, emit.DeckPlan]] = []
    for pdf in sorted(SYNC_DECKS.glob("*.pdf")):
        with tempfile.TemporaryDirectory() as tmp:
            prepared = prepare(pdf, Path(tmp))
            raw = extract(prepared.pdf, prepared.labels)
            deck = deck_json(classify(select_overlays(raw, "last")))
        slides: list[Json] = []
        for s in jobjs(deck, "slides"):
            merged: list[Json] = [e for e in emit.merge_blocks(jobjs(s, "elements"))]
            slides.append({**s, "elements": merged})
        plan = emit.DeckPlan({**deck, "slides": slides})
        out.append((pdf.stem, plan))
    return tuple(out)


def same_emission(name: str, plan: emit.DeckPlan) -> list[str]:
    """Every slide of a deck against a saved base made of its own entries, the fast path off:
    what emit writes must come out the same element by element, and nothing may be marked."""
    deck = plan.deck
    found: list[str] = []
    ours = entries_of(deck)
    base = as_saved(ours, 7)
    for j, (o, b, slide) in enumerate(zip(ours, jobjs(base, "slides"), jobjs(deck, "slides"))):
        names = [jstr(e, "key") for e in jobjs(o, "elements")]
        was_slide: JsonObject = {**slide, "page": b["page"], "elements": [e["ir"] for e in jobjs(b, "elements")]}
        was_read: Json = json.loads(json.dumps(sync.emitted_elements(was_slide, names, plan.scale, plan.fonts)))
        now_read: Json = json.loads(json.dumps(sync.emitted_elements(slide, names, plan.scale, plan.fonts)))
        was, now = jobjs(was_read), jobjs(now_read)
        found += [f"{name} slide {j + 1} {k}: emitted differently" for k, a, c in zip(names, was, now) if a != c]
        found += [f"{name} slide {j + 1} {k}: {sorted(sync.context_changes(a, c, [0.0, 0.0], jstr(e, 'kind'), False)[0])}"
                  for k, a, c, e in zip(names, was, now, jobjs(o, "elements"))
                  if any(sync.context_changes(a, c, [0.0, 0.0], jstr(e, "kind"), False))]
    unwritten = sync.mark_emitted(base, ours, deck, {j: j for j in range(len(ours))}, plan.scale, plan.fonts, fast=False, unread=[])
    found += [f"{name}: unwritten {u}" for u in unwritten]
    found += [f"{name} {jstr(s, 'key')} {jstr(e, 'key')}: marked {f}" for s in ours + jobjs(base, "slides")
              for e in jobjs(s, "elements") for f in identity.CONTEXT_FIELDS if f in jobj(e, "fields")]
    return found


# ---------------------------------------------------------------- no false positives

@pytest.mark.xdist_group("tests/test_emit_requests.py")  # (its decks, classified once per worker)
def test_every_built_deck_emits_the_same_against_its_own_saved_base() -> None:
    """Object ids, element numbering, the slide's page and a JSON round trip are nothing emit writes
    differently: a deck against a base of its own entries, renumbered and saved, marks nothing -
    with the fast path (which would skip every slide here) turned off."""
    decks = emitted()
    if not decks:
        pytest.skip("no PDFs built")
    found = [p for d in decks for p in same_emission(d.name, d.plan)]
    assert not found, f"{len(found)} false positive(s):\n" + "\n".join(found[:40])


def test_the_sync_talk_emits_the_same_against_its_own_saved_base() -> None:
    talk = sync_talk()
    if not talk:
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    found = [p for name, plan in talk for p in same_emission(name, plan)]
    assert not found, f"{len(found)} false positive(s):\n" + "\n".join(found[:40])


# ---------------------------------------------------------------- what it finds

def marks(base: JsonObject, ours: list[JsonObject],
          slide: JsonObject) -> tuple[dict[str, set[identity.Change]], list[sync.Unwritten]]:
    """{element key: the context fields `source_changes` reports} of one slide, and what cannot be
    written (at `SCALE`, with a fresh `FontMapper`)."""
    unwritten = sync.mark_emitted(base, ours, {"slides": [slide]}, {0: 0}, SCALE, emit.FontMapper(),
                                  fast=True, unread=[])
    base_by = {jstr(e, "key"): e for e in jobjs(base, "slides", 0, "elements")}
    found: dict[str, set[identity.Change]] = {}
    for oe in jobjs(ours[0], "elements"):
        key = jstr(oe, "key")
        if key in base_by:
            got = identity.source_changes(base_by[key], oe) & set(identity.CONTEXT_FIELDS)
            if got:
                found[key] = got
    return found, unwritten


def one_slide(elements: dict[str, JsonObject], title_page: bool) -> tuple[JsonObject, JsonObject]:
    """(slide entry, planned slide) of synthetic IR elements, keyed by name. A planned slide is a
    rendered one (its background says so), and emit parses its elements as such."""
    irs: list[Json] = list(elements.values())
    slide: JsonObject = {"page": 0, "size": [360.0, 270.0], "title_page": title_page, "elements": irs,
                         "background": "backgrounds/bg-001.png"}
    entries: list[Json] = [entry(k, ir, oid=None) for k, ir in elements.items()]
    return {"key": "s", "page": 0, "layout": emit.slide_layout(slide)[0], "elements": entries}, slide


def test_a_title_whose_box_a_new_neighbour_narrowed_is_a_source_change(tmp_path: Path) -> None:
    """A one-line box reaches to the mirror of the slide's leftmost body text (emit.text_right_limit),
    so the centred line the table-moved variant adds narrows the Results title a fresh conversion
    makes, though the title's own IR is the same: sync kept the old box (live scenario table-moved,
    707 against 474 pt). The title is now a `width` change - and nothing is where nothing moved."""
    v1, moved = SYNC_DECKS / "v1.pdf", SYNC_DECKS / "table-moved.pdf"
    if not v1.exists() or not moved.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = sync.build_ours_of(v1, tmp_path / "v1", {"slides": []}, "last", SLIDE_W, snapshot.NO_PICTURES)

    def first_base() -> JsonObject:
        slides: list[Json] = [s for s in copy.deepcopy(first.slides)]
        return {"slides": slides}

    def changes(pdf: Path, name: str) -> tuple[dict[tuple[str | None, str], set[identity.Change]], list[sync.Unwritten]]:
        base = first_base()
        ours = sync.build_ours_of(pdf, tmp_path / name, base, "last", SLIDE_W, snapshot.NO_PICTURES)
        out: dict[tuple[str | None, str], set[identity.Change]] = {}
        for j, i in ours.pairs.items():
            base_by = {jstr(e, "key"): e for e in jobjs(base, "slides", i, "elements")}
            for oe in jobjs(ours.slides[j], "elements"):
                key = jstr(oe, "key")
                if key in base_by and set(identity.CONTEXT_FIELDS) & identity.source_changes(base_by[key], oe):
                    title = as_optional_str(ours.slides[j].get("title"), "title")
                    out[(title, key)] = identity.source_changes(base_by[key], oe)
        return out, ours.context_unwritten

    assert changes(moved, "moved") == ({("Results", "text/title/0"): {"width"}}, [])
    assert changes(v1, "same") == ({}, [])
    # a mark an earlier sync left in the base (new_base copies ours' fields) says nothing by itself
    base = first_base()
    for s in jobjs(base, "slides"):
        for e in jobjs(s, "elements"):
            fields: JsonObject = dict(jobj(e, "fields"))
            for f in identity.CONTEXT_FIELDS:
                fields[f] = "ours"
            e["fields"] = fields
    again = sync.build_ours_of(v1, tmp_path / "again", base, "last", SLIDE_W, snapshot.NO_PICTURES)
    assert not any(set(identity.CONTEXT_FIELDS) & identity.source_changes(b, o) for j, i in again.pairs.items()
                   for o in jobjs(again.slides[j], "elements") for b in jobjs(base, "slides", i, "elements")
                   if b["key"] == o["key"])


def test_a_picture_beside_a_line_narrows_its_box() -> None:
    """A one-line box stops short of anything to its right on its lines: a picture the source put
    there narrows the text a fresh conversion makes. The text is a `width` change, the heading
    above it (other lines) is not."""
    heading = text_ir("Heading", [20, 20, 120, 34], "p0t0", role="body")
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1", role="body")
    base, _ = one_slide({"text/body/0": heading, "text/body/1": line}, title_page=False)
    picture = PICTURE
    ours, slide = one_slide({"text/body/0": heading, "text/body/1": line, "image/figure/0": picture}, title_page=False)
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert found == {"text/body/1": {"width"}} and unwritten == []


def moved_down(ir: JsonObject, by: float) -> JsonObject:
    """`ir` moved down `by` pt: its box, and its lines' baselines when it has any."""
    x0, y0, x1, y1 = jnums(ir, "bbox")
    out: JsonObject = {**ir, "bbox": [x0, y0 + by, x1, y1 + by]}
    if "paragraphs" in ir:
        paragraphs: list[Json] = []
        for p in jobjs(ir, "paragraphs"):
            lines: list[Json] = [{**ln, "baseline": jnum(ln, "baseline") + by} for ln in jobjs(p, "lines")]
            paragraphs.append({**p, "lines": lines})
        out["paragraphs"] = paragraphs
    return out


def test_a_moved_element_is_compared_less_its_own_step() -> None:
    """The source moved everything down 30 pt (a line added above): each box moves by as much and
    says nothing else new, so nothing is marked - `merge`'s move is the whole change."""
    irs = {"text/body/0": text_ir("First line", [20, 60, 90, 72], "p0t0", role="body"),
           "shape/panel/0": shape_ir([10, 100, 200, 160], "p0s1")}
    base, _ = one_slide(irs, title_page=False)
    down = {k: moved_down(v, 30) for k, v in irs.items()}
    added = text_ir("A new line above", [20, 30, 120, 42], "p0t2", role="body")
    ours, slide = one_slide({"text/body/9": added, **down}, title_page=False)
    assert all(identity.source_changes(b, o) == {"position"}
               for b, o in zip(jobjs(base, "elements"), jobjs(ours, "elements")[1:]))
    assert marks({"slides": [base]}, [ours], slide) == ({}, [])


def test_a_new_text_on_a_block_changes_its_grouping_which_sync_cannot_write() -> None:
    """emit groups a block's panels with what lies on them (emit.block_groups). A text the source
    added onto a block makes the block's other members' groups another one - which recreating them
    would not write (a recreated unit goes back into its old group): reported, not marked."""
    bar: JsonObject = {**shape_ir([10, 40, 200, 55], "p0s0"), "block": 0}
    body: JsonObject = {**shape_ir([10, 55, 200, 120], "p0s1"), "block": 0}
    first = text_ir("Inside", [20, 60, 90, 72], "p0t2", role="body")
    base, _ = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first}, title_page=False)
    ours, slide = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first,
                             "text/body/1": text_ir("Also inside", [20, 90, 100, 102], "p0t3", role="body")},
                            title_page=False)
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert found == {}
    assert {u.element: u.fields for u in unwritten} == \
        {"shape/panel/0": ("grouping",), "shape/panel/1": ("grouping",), "text/body/0": ("grouping",)}


def test_a_longer_text_below_the_title_takes_the_subtitle_placeholder() -> None:
    """On the title page the biggest plain text below the title goes into the layout's subtitle
    placeholder (emit.subtitle_element). A longer one the source added takes it from the authors'
    line, which becomes a box of its own in a fresh conversion. `Sync.update_slide` hands the
    placeholder over (test below), so the authors' line is an `emitted` change: recreated, as a box."""
    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    authors = text_ir("Someone", [20, 60, 90, 72], "p0t1", role="body")
    base, _ = one_slide({"text/title/0": title, "text/body/0": authors}, title_page=True)
    ours, slide = one_slide({"text/title/0": title, "text/body/0": authors,
                             "text/body/1": text_ir("A much longer institute line", [20, 90, 200, 102], "p0t2", role="body")},
                            title_page=True)
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert found.get("text/body/0") == {"emitted"}
    assert unwritten == []


def test_a_slide_that_changes_layout_is_a_placeholder_change_sync_cannot_write() -> None:
    """A slide that stops being the title page (the TITLE layout, a centred title and a subtitle
    placeholder) keeps its layout in the deck whatever units are recreated: the subtitle's move out
    of its placeholder is reported, not marked."""
    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    authors = text_ir("Someone", [20, 60, 90, 72], "p0t1", role="body")
    base, _ = one_slide({"text/title/0": title, "text/body/0": authors}, title_page=True)
    ours, slide = one_slide({"text/title/0": title, "text/body/0": authors}, title_page=False)
    found, unwritten = marks({"slides": [base]}, [ours], slide)
    assert "text/body/0" not in found
    assert [u for u in unwritten if u.element == "text/body/0"] == [
        sync.Unwritten(slide="s", element="text/body/0", fields=("placeholder",))]


def test_what_sync_cannot_write_is_a_warning_in_words() -> None:
    """`context_unwritten` reaches the sync report (`sync.unwritten_warnings`, added to
    `report["warnings"]`, which the agent's deck_sync relays one by one): one line per slide and kind,
    naming the elements by their words, not their keys."""
    bar: JsonObject = {**shape_ir([10, 40, 200, 55], "p0s0"), "block": 0}
    body: JsonObject = {**shape_ir([10, 55, 200, 120], "p0s1"), "block": 0}
    first = text_ir("Inside the block", [20, 60, 90, 72], "p0t2", role="body")
    base, _ = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first}, title_page=False)
    ours, slide = one_slide({"shape/panel/0": bar, "shape/panel/1": body, "text/body/0": first,
                             "text/body/1": text_ir("Also inside", [20, 90, 100, 102], "p0t3", role="body")},
                            title_page=False)
    _, unwritten = marks({"slides": [base]}, [ours], slide)
    ours["title"] = "Policy"
    said = sync.unwritten_warnings(unwritten, {"slides": [ours]})
    assert len(said) == 1
    assert said[0].startswith("slide s (Policy): the new version groups the shape shape/panel/0, the shape "
                              "shape/panel/1 and \"Inside the block\" differently")
    assert "they stayed grouped as before" in said[0]
    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    base, _ = one_slide({"text/title/0": title, "text/body/0": text_ir("Someone", [20, 60, 90, 72], "p0t1", role="body")},
                        title_page=True)
    ours, slide = one_slide({"text/title/0": title, "text/body/0": text_ir("Someone", [20, 60, 90, 72], "p0t1", role="body")},
                            title_page=False)
    _, unwritten = marks({"slides": [base]}, [ours], slide)
    said = sync.unwritten_warnings(unwritten, {"slides": [ours]})
    assert said == ["slide s: the new version puts \"Someone\" into another layout placeholder or out of one, "
                    "because the slide changes layout; a sync cannot change a live slide's layout, so it stayed "
                    "where it was"]
    assert sync.unwritten_warnings([], {"slides": [ours]}) == []


def subtitle_page(authors_action: str) -> tuple[sync.Sync, sync.SlideWork, JsonObject, list[JsonObject]]:
    """(Sync, work item, live objects, requests) of `update_slide` on a title page whose source
    reworded the authors' line (in the SUBTITLE placeholder) and added a longer line below it: the
    authors' unit as the merge decided (`authors_action`), the new line created."""
    from collections import defaultdict

    title = text_ir("A talk", [20, 20, 200, 40], "p0t0", role="title")
    old, new = text_ir("Someone", [20, 60, 90, 72], "p0t1", role="body"), text_ir("Someone else", [20, 60, 100, 72], "p0t1", role="body")
    longer = text_ir("A much longer institute line", [20, 90, 200, 102], "p0t2", role="body")
    plan = emit.DeckPlan({"slides": [{"page": 0, "size": [360.0, 270.0], "title_page": True, "elements": [title, new, longer]}]}, 720.0)
    b: JsonObject = {"key": "s", "elements": [entry("text/title/0", title, "TITLE_PH"), entry("text/body/0", old, "SUB_PH")]}
    objects: JsonObject = {oid: rb for e in jobjs(b, "elements") for oid, rb in jobj(e, "readback").items()}
    jobj(objects, "TITLE_PH")["placeholder"], jobj(objects, "SUB_PH")["placeholder"] = "TITLE", "SUBTITLE"
    s = sync_work.bare_sync()
    s.ours = {"slides": [{"key": "s", "elements": [entry("text/title/0", title, oid=None), entry("text/body/0", new, oid=None),
                                                   entry("text/body/1", longer, oid=None)]}]}
    s.ours_out = Path(".")
    s.plan, s.scale, s.tok = plan, plan.scale, "1zz"
    s.warnings = []
    s.base = {"slides": [b]}
    s.recovery, s.urls = sync.no_recovery(), defaultdict(lambda: "https://example.com/x.png")
    w = sync_work.slide_work({"key": "s", "base": 0, "ours": 0, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": authors_action, "ours_members": ["text/body/0"], "base_members": ["text/body/0"],
         "overrides": {}},
        {"key": "text/body/1", "action": "create", "ours_members": ["text/body/1"]}]})
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    return s, w, objects, reqs


def made_id(request: JsonObject, kind: str) -> Json:
    """The objectId a request of `kind` names, None for a request of another kind."""
    body = request.get(kind)
    return body.get("objectId") if isinstance(body, dict) else None


def test_a_subtitle_the_deck_keeps_keeps_its_placeholder() -> None:
    """The person's edit to the authors' line conflicted and the deck's version stays (`keep`): the
    placeholder is theirs, so the longer line the source added is a box of its own, not written
    into it - and not created under its id either."""
    s, w, objects, reqs = subtitle_page("keep")
    assert w.in_place == {}
    assert not [r for r in reqs if any(v.get("objectId") in objects for v in r.values() if isinstance(v, dict))]
    assert any(made_id(r, "createShape") == w.new_oid[2] for r in reqs)
    assert "SUB_PH" not in s.cleanup_ids


def test_a_reworded_subtitle_that_lost_the_placeholder_is_not_created_under_its_id() -> None:
    """The source rewords the authors' line and adds a longer line below it, which is the subtitle
    now. The recreated authors' line used to go into its old placeholder (`in_place`) but was
    emitted as a box of its own: a createShape under the live placeholder's object id, which Slides
    refuses with the whole batch. Now the authors' line is a box under a new id and the longer line,
    the subtitle, is written into the placeholder (emptied first), as a fresh conversion has it."""
    s, w, objects, reqs = subtitle_page("recreate")
    assert not [r for r in reqs if made_id(r, "createShape") in objects]
    size = jnums(objects, "SUB_PH", "size")
    assert w.in_place == {2: sync.InPlace(id="SUB_PH", size=(size[0], size[1]), text="Someone")}
    authors = w.new_oid[1]
    assert authors != "SUB_PH" and any(made_id(r, "createShape") == authors for r in reqs)
    inserted = [jstr(r, "insertText", "text") for r in reqs if made_id(r, "insertText") == "SUB_PH"]
    assert "".join(inserted) == "A much longer institute line"
    assert reqs.index({"deleteText": {"objectId": "SUB_PH", "textRange": {"type": "ALL"}}}) < \
        min(i for i, r in enumerate(reqs) if made_id(r, "insertText") == "SUB_PH")
    assert "SUB_PH" not in s.cleanup_ids and "SUB_PH" not in w.doomed


def test_an_old_base_emit_cannot_read_marks_nothing() -> None:
    """A base IR an older converter wrote, missing what today's emit reads, says nothing either way."""
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1", role="body")
    base, _ = one_slide({"text/body/1": copy.deepcopy(line)}, title_page=False)
    for p in jobjs(base, "elements", 0, "ir", "paragraphs"):
        del p["lines"]
    picture = PICTURE
    ours, slide = one_slide({"text/body/1": line, "image/figure/0": picture}, title_page=False)
    assert marks({"slides": [base]}, [ours], slide) == ({}, [])
