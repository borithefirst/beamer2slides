"""A sync meeting an element emit cannot plan writes the rest and says so (`sync.planned`).

Convert contains such an element (688ebf4, `emit.DeckPlan(contain=True)`): the picture of its region,
with a warning. Sync planned the new conversion without containment, so the same element killed the
whole sync in `build_ours`, before a single write - the person's deck untouched but the source's other
changes lost with it. Now sync plans as convert does: the element is its picture, cut from the new
PDF, the merge treats it as any other change (the deck's edits still win), and the report lists it.
`mark_emitted`'s slides it could not compare are said too (`context_unread`). Under
`emit.STRICT_ENV` (the offline suite's setting) both failures raise. Offline: the sync test talk
(tests/decks/sync/build.py) through slides_sim, no Google.
"""

import copy
import inspect
from pathlib import Path

import pytest

from beamer2slides.emit import SLIDE_W
from beamer2slides import adopt_sync, emit, identity, merge, snapshot, sync

from . import ir_sources as S
from .test_emitted_diff import PICTURE, one_slide
from .test_sync import text_ir

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"
POLICY = "Deck edits come first"  # (the policy slide's text the `disjoint` variant rewrites)


@pytest.fixture
def lenient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Containment on, whatever the suite's default."""
    monkeypatch.setenv(emit.STRICT_ENV, "0")


def unplannable(monkeypatch: pytest.MonkeyPatch, words: str) -> None:
    """emit's text boxes failing, as a field no producer wrote would, for the text holding `words`.
    (emit plans a parsed text element: `el` is its ir_types record.)"""
    real = emit.text_element_requests

    def text_element_requests(el, *args, **kwargs):
        if words in " ".join("".join(r.text for r in p.runs) for p in el.paragraphs):
            raise KeyError("lines")
        return real(el, *args, **kwargs)
    monkeypatch.setattr(emit, "text_element_requests", text_element_requests)


def talk_base(tmp_path: Path) -> tuple[dict, dict]:
    """The base `convert` records for the sync talk's v1, and the deck it wrote (slides_sim)."""
    deck, _raw, used = S._convert_pdf(SYNC_DECKS / "v1.pdf", tmp_path / "v1")
    return S.base_of(S.Made(deck, "rendered", tmp_path / "v1", used))


def requests_of(base: dict, ours: dict, pres: dict, home: Path) -> tuple[dict, list[dict], list[dict]]:
    """(the merge plan, the content requests, the objects left to delete) of a dry sync."""
    theirs = snapshot.read_presentation(pres)
    mplan = merge.plan_merge(base, ours, theirs)
    s = sync.Sync(None, None, "offline", base, ours, home, dry_run=True, measure=False)
    s.theme_plan = None
    work = s.prepare(mplan, pres, theirs)
    content, cleanup = s.main_requests(work, theirs, pres, {}, [])
    return mplan, content, cleanup


@pytest.mark.needs_decks("sync/out/v1.pdf", "sync/out/disjoint.pdf")
def test_a_sync_writes_everything_else_and_reports_the_element_it_made_a_picture(
        lenient: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base, pres = talk_base(tmp_path)
    unplannable(monkeypatch, POLICY)
    ours = sync.build_ours(SYNC_DECKS / "disjoint.pdf", tmp_path / "ours", base, "last", SLIDE_W, ())

    [c] = ours["contained"]
    assert (c.slide, c.element, c.kind, c.error) == ("policy", "image/fallback/0", "text", "KeyError: 'lines'")
    assert POLICY in c.words
    [el] = [e for s in ours["deck"]["slides"] for e in s["elements"] if e["id"] == c.id]
    assert el["role"] == "fallback" and (tmp_path / "ours" / el["file"]).exists(), "its picture, cut from the new PDF"

    mplan, content, cleanup = requests_of(base, ours, pres, tmp_path / "ours")
    pictures = [r["createImage"]["url"] for r in content if "createImage" in r]
    assert any(u.endswith(f"fallback-{c.id}.png") for u in pictures), "the picture goes in its place"
    written = {(p["key"], u["key"]) for p in mplan["slides"] if p["action"] == "update"
               for u in p["units"] if u["action"] in ("create", "recreate")}
    assert {("steps", "text/body/0"), ("convergence", "image/figure/0")} <= written, \
        "the source's other changes are still written"
    inserted = " ".join(r["insertText"]["text"] for r in content if "insertText" in r)
    assert "Deck edits" not in inserted, "the policy words are its picture, not text"
    [policy] = [p for p in mplan["slides"] if p["key"] == "policy"]
    gone = {u["key"] for u in policy["units"] if u["action"] == "delete"}
    old = [o for s in base["slides"] if s["key"] == "policy" for e in s["elements"] if e["key"] in gone
           for o in e["objects"]]
    deleted = {r["deleteObject"]["objectId"] for r in cleanup}
    assert gone and old and set(old) <= deleted, "the box it replaces goes, as any rewritten unit's does"

    found, says = sync.contained_report(ours["contained"], ours["slides"], mplan)
    assert [f.written for f in found] == [True]
    assert says and says[0].startswith("slide policy: this version of the converter could not lay out the text")
    assert sync.contained_json(found)[0]["written"] is True


@pytest.mark.needs_decks("sync/out/v1.pdf", "sync/out/disjoint.pdf")
def test_under_strict_the_unplannable_element_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(emit.STRICT_ENV, "1")
    base, _ = talk_base(tmp_path)
    unplannable(monkeypatch, POLICY)
    with pytest.raises(KeyError):
        sync.build_ours(SYNC_DECKS / "disjoint.pdf", tmp_path / "ours", base, "last", SLIDE_W, ())


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_an_element_convert_contained_the_same_way_is_no_change(
        lenient: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deck convert made with the element already a picture, and a source that has not changed:
    the sync's own picture is the same, so nothing is written and nothing is said."""
    unplannable(monkeypatch, "Both versions go into the report")  # (v1's policy slide)
    deck, _raw, used = S._convert_pdf(SYNC_DECKS / "v1.pdf", tmp_path / "v1")
    off = emit.plan_offline(copy.deepcopy(deck))
    assert [c["kind"] for c in off["contained"]] == ["text"]
    emit.crop_fallbacks(off["plan"].deck, [(c["page"], c["id"]) for c in off["contained"]], tmp_path / "v1", "test")
    base, pres = S.base_of(S.Made(deck, "rendered", tmp_path / "v1", used))
    ours = sync.build_ours(SYNC_DECKS / "v1.pdf", tmp_path / "ours", base, "last", SLIDE_W, ())

    mplan, content, _ = requests_of(base, ours, pres, tmp_path / "ours")
    found, says = sync.contained_report(ours["contained"], ours["slides"], mplan)
    assert [f.written for f in found] == [False] and says == []
    assert not [r for r in content if "createImage" in r], "no picture written again"


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_an_element_the_base_holds_that_emit_now_cannot_plan_is_kept(
        lenient: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The deck holds the element as convert wrote it; this converter cannot plan it, and the source
    has not changed it. Its picture is another element (another key), so the merge deleted the
    person's box and put the picture in - an adopt base's kept lines, six of hashing's objects
    replaced on an unchanged source (audit 2026-09-29). The base now records it as the picture
    (`base_as_contained`): nothing is written, the deck's box and its read-back stay."""
    base, pres = talk_base(tmp_path)
    unplannable(monkeypatch, "Both versions go into the report")
    [held] = [e for s in base["slides"] for e in s["elements"]
              if e["kind"] == "text" and "Both versions go into the report" in identity.plain_text(e["ir"])]
    objects, read = list(held["objects"]), copy.deepcopy(held["readback"])
    ours = sync.build_ours(SYNC_DECKS / "v1.pdf", tmp_path / "ours", base, "last", SLIDE_W, ())

    mplan, content, cleanup = requests_of(base, ours, pres, tmp_path / "ours")
    assert {u["action"] for p in mplan["slides"] for u in p.get("units", [])} <= {"keep"}
    assert not [r for r in content if "createImage" in r] and cleanup == []
    [c] = ours["contained"]
    assert (held["kind"], held["key"], held["objects"], held["readback"]) == ("image", c.element, objects, read)
    found, says = sync.contained_report(ours["contained"], ours["slides"], mplan)
    assert [f.written for f in found] == [False] and says == []


def test_both_sides_plan_with_containment() -> None:
    """The adopt base is `convert_source`'s plan and a later sync's is `build_ours`': both through
    `planned`, or one side has a picture where the other has the element, and every sync rewrites it."""
    assert "planned(" in inspect.getsource(sync.build_ours)
    assert "planned(" in inspect.getsource(adopt_sync.convert_source)


# ---------------------------------------------------------------- an adopt base's old tables

def test_an_adopt_base_older_than_its_tables_layout_is_no_source_change() -> None:
    """A marked table (`slidetable`) an adopt base recorded before 688ebf4 has its cells and box but
    no layout: today's same source hashes differently, and an unchanged source recreated hashing's
    table. `upgrade_tables` takes our layout when all the old form says is the same; a changed cell
    still shows."""
    run = {"text": "Chaining", "font": "TeXGyreHeros-Bold", "family": "sans", "size": 9.4, "bold": True,
           "italic": False, "smallcaps": False, "color": "#ffffff", "link": None, "script": None,
           "underline": False, "strike": False, "highlight": None}
    old = {"id": "p4m2", "kind": "table", "role": "table", "bbox": [25.2, 63.0, 289.8, 189.0],
           "cells": [[[run], [{**run, "text": "Probing"}]]], "spans": ["p4s1", "p4s2"], "drawings": ["p4d1"],
           "mark": "hash05_t"}
    ours = {**old, "id": "p4m7", "columns": [[25.2, 150.0], [150.0, 289.8]], "row_heights": [126.0],
            "rules": [], "fills": [], "merges": [], "borders": {}, "bands": [], "size": 9.4}
    edited = {**old, "cells": [[[run], [{**run, "text": "Hopscotch"}]]], "mark": "hash05_u"}
    deck = {"slides": [{"elements": [ours, {**ours, "mark": "hash05_u"}]}]}

    def entry(ir: dict) -> dict:
        h, fields = identity.ir_fields(ir, None, None, None)
        return {"key": "table/table/0", "kind": "table", "anchor": None, "ir": ir, "ir_hash": h, "fields": fields}
    base = {"adopt": {"boxes": {}}, "slides": [{"key": "s", "elements": [entry(old), entry(edited)]}]}
    assert [(r.slide, r.how) for r in adopt_sync.upgrade_tables(base, deck, None)] == [("s", "adopt_table")]
    same, changed = base["slides"][0]["elements"]
    assert same["ir"]["id"] == "p4m2" and same["ir"]["columns"] == ours["columns"]
    assert identity.source_changes(same, entry(ours)) == set()
    assert changed["ir"] is edited, "a table the source changed is left for the merge to see"
    plain = {"slides": [{"elements": [entry(old)]}]}
    assert adopt_sync.upgrade_tables(plain, deck, None) == []  # (a convert base has no marks to upgrade)
    assert plain["slides"][0]["elements"][0]["ir"] is old


# ---------------------------------------------------------------- mark_emitted's slides it cannot compare

def test_an_old_base_emit_cannot_read_is_said_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A base an older converter wrote is a real deck's everyday case: no marks, never a raise (even
    under strict), and a warning, since nothing else would say the boxes kept their old places."""
    monkeypatch.setenv(emit.STRICT_ENV, "1")
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1")
    base, _ = one_slide({"text/body/1": copy.deepcopy(line)})
    for p in base["elements"][0]["ir"]["paragraphs"]:
        del p["lines"]
    ours, slide = one_slide({"text/body/1": line, "image/figure/0": PICTURE})
    unread: list[sync.Unread] = []
    assert sync.mark_emitted({"slides": [base]}, [ours], {"slides": [slide]}, {0: 0}, 2.0, emit.FontMapper(),
                             fast=True, unread=unread) == []
    # (emit parses each element before planning it: the missing field is said where it is read)
    assert [(u.slide, u.side) for u in unread] == [("s", "base")]
    assert unread[0].error.startswith("IRError") and "lacks required key 'lines'" in unread[0].error
    [said] = sync.unread_warnings(unread, [{"key": "s", "title": "Results", "elements": []}])
    assert said.startswith("slide s (Results): the sync base records it in a form this version")


def failing_new_element(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict, dict]:
    """A slide whose new version holds an element emit's writer fails on (id p0s9), beside an
    unchanged line: (base, ours entry, planned slide)."""
    real = emit.shape_element_requests

    def shape_element_requests(el, *args, **kwargs):
        if el.id == "p0s9":
            raise KeyError("flip")
        return real(el, *args, **kwargs)
    monkeypatch.setattr(emit, "shape_element_requests", shape_element_requests)
    line = text_ir("A short line", [20, 60, 90, 72], "p0t1")
    base, _ = one_slide({"text/body/1": copy.deepcopy(line)})
    panel = {"kind": "shape", "id": "p0s9", "role": "panel", "shape": "RECTANGLE", "flip": False, "radius": 0.0,
             "bbox": [200, 55, 300, 80], "fill": "#3366cc", "spans": [], "drawing": "p0d9"}
    ours, slide = one_slide({"text/body/1": line, "shape/panel/0": panel})
    return {"slides": [base]}, ours, slide


def test_a_new_slide_emit_cannot_write_raises_under_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """`planned` rehearsed the plan's slides, so this is a difference between the rehearsal and
    `slide_emission`: a bug the suite must see."""
    monkeypatch.setenv(emit.STRICT_ENV, "1")
    base, ours, slide = failing_new_element(monkeypatch)
    with pytest.raises(KeyError):
        sync.mark_emitted(base, [ours], {"slides": [slide]}, {0: 0}, 2.0, emit.FontMapper(), fast=True, unread=[])


def test_a_new_slide_emit_cannot_write_is_said_in_a_persons_sync(lenient: None, monkeypatch: pytest.MonkeyPatch) -> None:
    base, ours, slide = failing_new_element(monkeypatch)
    unread: list[sync.Unread] = []
    assert sync.mark_emitted(base, [ours], {"slides": [slide]}, {0: 0}, 2.0, emit.FontMapper(),
                             fast=True, unread=unread) == []
    assert [(u.slide, u.side, u.error) for u in unread] == [("s", "ours", "KeyError: 'flip'")]
    [said] = sync.unread_warnings(unread, [{"key": "s", "elements": []}])
    assert said.startswith("slide s: this version of the converter could not lay it out (KeyError")
