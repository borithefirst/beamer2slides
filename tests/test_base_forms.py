"""A sync base is read through the IR parser before anything compares it (`sync.base_today`).

A base stores each element's IR and a hash of its JSON form (`identity.ir_fields`), key set
included. When the converter comes to write an element's JSON differently - a key left out, a
false flag no longer written - every element of every base would read as changed by the source,
and every unchanged unit of every deck would be recreated. So a base is read as `ir_types` writes
it (`snapshot.rehash_base`): an element whose form differs takes today's and is hashed again, which
makes a changed form a rewrite of the base, not a source change. Where the recorded hash cannot be
worked out again from the recorded IR (the picture file gone, other pages), the element is kept and
said. An adopt base's old marked shapes and tables are brought up in the same place, first.

Offline: the sync test talk (tests/decks/sync/build.py) converted as `convert` records it."""

import copy
import json
from pathlib import Path

import pytest

from beamer2slides import adopt_sync, identity, merge, snapshot, sync
from beamer2slides.emit import SLIDE_W

from .test_sync_containment import SYNC_DECKS, requests_of, talk_base


@pytest.fixture(scope="module")
def talk(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict, Path]:
    """(the base convert records for the sync talk's v1, the deck it wrote, the folder of its files)."""
    home = tmp_path_factory.mktemp("talk")
    base, pres = talk_base(home)
    return base, pres, home / "v1"


def element(base: dict, kind: str, words: str | None) -> tuple[str, dict]:
    """(slide key, base element) of the first `kind` element (holding `words`, for text)."""
    return next((s["key"], e) for s in base["slides"] for e in s["elements"]
                if e["kind"] == kind and (words is None or words in identity.plain_text(e["ir"])))


def recorded_as(base: dict, e: dict, ir: dict, folder: Path | None) -> None:
    """`e` holding `ir`, hashed as an older converter that wrote this form would have recorded it."""
    h, fields = identity.ir_fields(ir, folder, e.get("anchor"), snapshot.base_page_key(base))
    e.update(ir=ir, ir_hash=h, fields={**e["fields"], **fields})


def dumped(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def at(base: dict, *folders: Path) -> snapshot.BasePictures:
    """`base`'s pictures looked for in `folders`, in order."""
    return snapshot.find_base_pictures(base, snapshot.PictureFolders(kept=folders, rendered=None, held=None))


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_a_base_in_todays_form_is_read_as_it_is(talk: tuple[dict, dict, Path], tmp_path: Path) -> None:
    """What convert records today: nothing to rewrite, not a hash touched - by the reading alone and
    by a whole `build_ours` of the same source."""
    base, _, v1 = copy.deepcopy(talk[0]), talk[1], talk[2]
    before = dumped(base)
    assert snapshot.rehash_base(base, at(base, v1)) == []
    assert dumped(base) == before
    ours = sync.build_ours_of(SYNC_DECKS / "v1.pdf", tmp_path / "ours", base, "last", SLIDE_W, at(base, v1))
    assert ours.base_forms == []
    assert [e["ir_hash"] for s in base["slides"] for e in s["elements"]] == \
        [e["ir_hash"] for s in json.loads(before)["slides"] for e in s["elements"]]


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_a_form_the_converter_no_longer_writes_is_a_rewrite_of_the_base_not_a_source_change(
        talk: tuple[dict, dict, Path], tmp_path: Path) -> None:
    """A text an older converter recorded with `"composite": false` written out: the same element,
    another JSON form, another hash. Read in today's form, the base's element hashes as the new
    conversion's does, and an unchanged source plans no write."""
    base, pres, v1 = copy.deepcopy(talk[0]), talk[1], talk[2]
    slide, e = element(base, "text", "Both versions go into the report")
    canonical = e["ir"]
    recorded_as(base, e, {**canonical, "composite": False}, v1)
    assert e["ir_hash"] != identity.ir_fields(canonical, v1, e.get("anchor"), snapshot.base_page_key(base))[0], \
        "(the old form hashes otherwise: without the reading, a source change)"

    ours = sync.build_ours_of(SYNC_DECKS / "v1.pdf", tmp_path / "ours", base, "last", SLIDE_W, at(base, v1))
    assert ours.base_forms == [snapshot.Rewritten(slide=slide, element=e["key"], how="form", hashed=True)]
    assert "composite" not in e["ir"]
    [now] = [o for s in ours.slides if s["key"] == slide for o in s["elements"] if o["key"] == e["key"]]
    assert identity.source_changes(e, now) == set()
    mplan, content, cleanup = requests_of(base, ours, pres, tmp_path / "ours")
    assert {u["action"] for p in merge.merge_plan_json(mplan)["slides"] for u in p.get("units", [])} <= {"keep"}
    assert [r for r in content if "__b2s_break__" not in r] == [] and cleanup == []  # (breaks: batch bounds)
    [said] = snapshot.base_form_warnings(ours.base_forms)
    assert said.startswith("1 element(s) of the sync base were recorded in an older form") and e["key"] in said


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_an_element_the_parser_refuses_is_kept_and_said(talk: tuple[dict, dict, Path]) -> None:
    base, _, v1 = copy.deepcopy(talk[0]), talk[1], talk[2]
    slide, e = element(base, "text", "Both versions go into the report")
    recorded_as(base, e, {**e["ir"], "wobble": 1}, v1)
    kept = copy.deepcopy(e)
    [found] = snapshot.rehash_base(base, at(base, v1))
    assert isinstance(found, snapshot.Unparsed) and (found.slide, found.element) == (slide, e["key"])
    assert "'wobble'" in found.error
    assert e == kept, "kept as recorded"
    [said] = snapshot.base_form_warnings([found])
    assert said.startswith(f"slide {slide}: the sync base records {e['key']} in a form this version cannot read")
    assert snapshot.base_form_json([found])[0]["outcome"] == "unparsed"


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_a_picture_whose_file_is_gone_keeps_its_hash(talk: tuple[dict, dict, Path], tmp_path: Path) -> None:
    """A picture's hash holds its file's, and the base keeps only 12 characters of that: with the
    file gone the hash cannot be made again, so the element stays as recorded and is said. Where the
    file is, the new hash is made with it."""
    base, _, v1 = copy.deepcopy(talk[0]), talk[1], talk[2]
    slide, e = element(base, "image", None)
    canonical = e["ir"]
    recorded_as(base, e, {**canonical, "overlay": False}, v1)
    kept = copy.deepcopy(e)
    assert snapshot.rehash_base(base, at(base, tmp_path)) == [
        snapshot.PictureGone(slide=slide, element=e["key"], file=canonical["file"])]
    assert e == kept

    # (the file where a later sync's work folder has it: the second folder given)
    assert snapshot.rehash_base(base, at(base, tmp_path, v1)) == [
        snapshot.Rewritten(slide=slide, element=e["key"], how="form", hashed=True)]
    assert e["ir"] == canonical
    assert e["ir_hash"] == identity.ir_fields(canonical, v1, e.get("anchor"), snapshot.base_page_key(base))[0]
    assert e["ir_hash"] != identity.ir_fields(canonical, None, e.get("anchor"), snapshot.base_page_key(base))[0], \
        "(the file's bytes are in the hash)"


@pytest.mark.needs_decks("sync/out/v1.pdf")
def test_a_recorded_hash_its_ir_does_not_give_is_kept(talk: tuple[dict, dict, Path]) -> None:
    """Hashed against other inputs than these (a link to other pages, a picture written again): a new
    hash from these would say a change nobody made."""
    base, _, v1 = copy.deepcopy(talk[0]), talk[1], talk[2]
    slide, e = element(base, "text", "Both versions go into the report")
    recorded_as(base, e, {**e["ir"], "composite": False}, v1)
    e["ir_hash"] = "0" * 16
    kept = copy.deepcopy(e)
    assert snapshot.rehash_base(base, at(base, v1)) == [snapshot.Unreproduced(slide=slide, element=e["key"])]
    assert e == kept


# ---------------------------------------------------------------- an adopt base's old forms, first

RUN = {"text": "Chaining", "font": "TeXGyreHeros-Bold", "family": "sans", "size": 9.4, "bold": True,
       "italic": False, "smallcaps": False, "color": "#ffffff", "link": None, "script": None,
       "underline": False, "strike": False, "highlight": None}


def adopt_entry(key: str, ir: dict, page_key: object) -> dict:
    h, fields = identity.ir_fields(ir, None, None, page_key)
    return {"key": key, "kind": ir["kind"], "anchor": None, "ir": ir, "ir_hash": h, "fields": fields}


def test_an_adopt_tables_link_to_a_slide_is_hashed_against_the_bases_pages() -> None:
    """`upgrade_tables` took our table's layout and hashed it without the base's pages, where our
    side (`snapshot.slide_entries`) hashes a link to a slide as that slide's key: a slidetable
    whose cell links to another slide read as changed on every sync of an unchanged source, and
    the person's table was recreated. Now both sides hash against the pages."""
    linked = {**RUN, "link": "#page=0"}
    old = {"id": "p1m2", "kind": "table", "role": "table", "bbox": [25.2, 63.0, 289.8, 189.0],
           "cells": [[[linked], [{**RUN, "text": "Probing"}]]], "spans": ["p1s1", "p1s2"], "drawings": ["p1d1"],
           "mark": "hash05_t"}
    ours = {**old, "id": "p1m7", "columns": [[25.2, 150.0], [150.0, 289.8]], "row_heights": [126.0],
            "rules": [], "fills": [], "merges": [], "borders": {}, "bands": [], "size": 9.4}
    view = {"slides": [{"page": 0, "elements": []}, {"page": 1, "elements": [ours]}]}
    ours_key = snapshot.page_keys(view, ["intro", "table"])  # (as `slide_entries` hashes our side)
    assert ours_key(0) == "intro"

    def base_of() -> dict:
        return {"adopt": {"boxes": {}}, "slides": [{"key": "intro", "page": 0, "elements": []},
                                                   {"key": "table", "page": 1,
                                                    "elements": [adopt_entry("table/table/0", old, None)]}]}
    theirs = adopt_entry("table/table/0", {**ours, "id": old["id"]}, ours_key)

    unpaged = base_of()
    adopt_sync.upgrade_tables(unpaged, view, None)  # (the call as it was)
    assert identity.source_changes(unpaged["slides"][1]["elements"][0], theirs) != set()
    base = base_of()
    assert [(r.slide, r.how) for r in adopt_sync.upgrade_tables(base, view, snapshot.base_page_key(base))] == \
        [("table", "adopt_table")]
    assert identity.source_changes(base["slides"][1]["elements"][0], theirs) == set()


def test_an_adopt_bases_old_shape_is_brought_up_before_it_is_read() -> None:
    """`base_today`'s order: the old marked shape (no `flip`, its outline a colour) is upgraded, and
    the parser then reads it - a freeform emit cannot draw, which an older adopt base holds as a
    classified shape - with nothing more to say."""
    ours = {"id": "p1m3", "kind": "shape", "role": "panel", "bbox": [10, 20, 60, 50], "fill": "#ff0000",
            "outline": {"color": "#00ff00", "width": 2.02}, "shape": "custom", "flip": False, "radius": 0.0,
            "drawings": ["d1"], "spans": [], "mark": "p80_i12"}
    old = {k: v for k, v in ours.items() if k not in ("flip", "radius")} | {"outline": "#00ff00"}
    base = {"adopt": {"boxes": {}}, "slides": [{"key": "s", "page": 0,
                                                "elements": [adopt_entry("shape/panel/0", old, None)]}]}
    found = sync.base_today(base, {"slides": [{"page": 0, "elements": [ours]}]}, snapshot.NO_PICTURES)
    assert found == [snapshot.Rewritten(slide="s", element="shape/panel/0", how="adopt_shape", hashed=True)]
    assert base["slides"][0]["elements"][0]["ir"] == {**ours, "id": "p1m3"}
