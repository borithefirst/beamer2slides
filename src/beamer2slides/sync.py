"""`sync`: bring a changed beamer PDF into the live, edited Slides deck (docs/sync.md).

    python -m beamer2slides sync deck.pdf --deck <url|id|out folder> [--out DIR] [--dry-run]

ours = the new conversion (planned like emit, nothing sent), theirs = the live deck, base = what
the converter wrote last time (snapshot). merge.plan_merge decides; this module builds the
requests (emit's builders under fresh object ids), writes them with requiredRevisionId and
records the new base."""

import contextlib
import copy
import json
import os
import random
import re
import string
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, TypeVar

from . import faults, google_types, identity, merge, refit, snapshot
from .deck_pictures import WORKERS as PICTURE_WORKERS, LivePictures
from .gapi import HttpError, status_of
from .google_types import DriveFile, DriveService, Page, PageElement, Presentation, SlidesService, object_id
from .gslides import EMU_PER_PT, execute, pt
from .json_types import (Json, JsonArray, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects,
                         as_optional_str, as_str)
from .paths import out_root
from .sync_model import (ElementEntry, ElementKey, JsonMap, ObjectId, SlideKey, SlideRead, base as parse_base, deck_read)
from .typing_compat import assert_never

if TYPE_CHECKING:
    from .compare import PicHash
    from .emit import DeckPlan, FontMapper
    from .emit_model import Place, TemplateKey
    from .guard import WayBack
    from .theme_sync import ThemeMerge, ThemeOurs, ThemeSide

    PictureFingerprints = tuple[str, object, PicHash | None]   # (sha1, look, thumbnail hash): picture_adopter

T = TypeVar("T")

MAX_ATTEMPTS = 3
CHUNK = 450
PICTURE_OVERLAP = 0.6  # of the larger box: a deck picture the source now draws sits where it does
SCRATCH = re.compile(r"b2s_m\d{3}")  # emit.measure_jobs' scratch slides
# pt: size of plain shapes standing in for template shapes. Exactly the 3,000,000 EMU Slides stores
# every new shape at (it keeps the size asked for in the scale, not the size), because a copy of the
# stand-in is scaled ABSOLUTE by (box / STAND_IN): at 100 pt the copy came out 2.36 times too big
# (a block's title bar 1649 pt wide on a 720 pt page, found by the front page's sync demo).
STAND_IN = 3_000_000 / 12700
# Object ids sync gives what it creates: b2s_<h6 slide>[_<h6 element>|_k<n>]_<generation><2 letters>
# plus emit's own suffixes (_g, n, _n0). Nothing a person can make in Slides looks like this.
SYNC_ID = re.compile(r"b2s_[0-9a-f]{6}(?:_(?:[0-9a-f]{6}|k\d+))?_(\d+)[a-z]{2}[a-z0-9_]*")
IN_PLACE_FIELDS = ("text", "text_styles", "paragraph_styles", "text_style_hash", "table")  # (table: its grid)
# Google's words when it could not fetch a createImage URL, and the waits before sending again (s).
PICTURE_FETCH = "problem retrieving the image"
FETCH_RETRY = (3, 10)
WIDTH_MOVED = 0.5  # slide pt: a box emit now places further than this has moved (mark_emitted)


class RevisionMismatch(Exception):
    pass


class NoSyncBase(SystemExit):
    """There is no base for this presentation: nothing to merge against. Still a `SystemExit`,
    so the CLI says it and stops as before; a caller that branches (`agent.deck_tools`) can tell
    it from the refusal below, whose way forward is the opposite."""


class BaseMismatch(SystemExit):
    """There is a base, and it describes none of the deck's slides: it belongs to another copy of
    the deck, or the deck was rebuilt outside sync. The deck exists and may be edited, so
    converting - the way forward from `NoSyncBase` - would make a second deck beside it."""


def resolve_deck(arg: str) -> tuple[str, Path | None]:
    """--deck as a URL, a presentation id or an output folder of `convert` (its emit.json)."""
    path = Path(arg)
    if path.is_dir():
        state = path / "emit.json"
        if state.exists():
            return json.loads(state.read_text(encoding="utf-8"))["presentationId"], path
        base = snapshot.local_path(path)
        if base.exists():
            return json.loads(base.read_text(encoding="utf-8"))["presentationId"], path
        raise SystemExit(f"{arg}: no emit.json or sync/base.json in this folder")
    m = re.search(r"/presentation/d/([\w-]+)", arg)
    return (m.group(1) if m else arg), None


def h6(text: str) -> str:
    return identity.sha1(text)[:6]


# ---------------------------------------------------------------- reading the JSON sync keeps
# The base, the live read-back and the created objects stay JSON: sync changes them in place and
# writes them back as they are. These read one value of them as `x or {}` / `x or []` / `x or ""`
# did: absent or empty is a new empty value, another shape an error naming where it was.

def _obj(v: Json, where: str) -> JsonObject:
    return as_object(v, where) if v else {}


def _arr(v: Json, where: str) -> JsonArray:
    return as_array(v, where) if v else []


def _objs(v: Json, where: str) -> list[JsonObject]:
    return as_objects(v, where) if v else []


def _strs(v: Json, where: str) -> list[str]:
    return [as_str(x, f"{where}[{i}]") for i, x in enumerate(_arr(v, where))]


def _text(v: Json, where: str) -> str:
    return as_str(v, where) if v else ""


def _nums(v: Json, where: str) -> list[float]:
    return [_number(x, f"{where}[{i}]") for i, x in enumerate(as_array(v, where))]


def _json_strs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def _json_nums(xs: Sequence[float]) -> list[Json]:
    return [x for x in xs]


def _slides(v: JsonMap, where: str) -> list[JsonObject]:
    """A base's or a read-back's `slides` (the same dicts, so a change to one is kept)."""
    return as_objects(v["slides"], f"{where}.slides")


def _objects(s: JsonMap, where: str) -> JsonObject:
    """A read-back slide's objects, objectId -> read-back (the same dict)."""
    return as_object(s["objects"], f"{where}.objects")


def _by_id(read: JsonMap, where: str) -> dict[str, JsonObject]:
    """A read-back's slides by objectId (the same dicts)."""
    return {as_str(s["objectId"], f"{where}: slide objectId"): s for s in _slides(read, where)}


def _str_set(v: object, where: str) -> set[str]:
    """A collection of ids a replay handed over as plain data (absent or empty: none)."""
    if not v:
        return set()
    if not isinstance(v, (set, frozenset, list, tuple)):
        raise JsonShapeError(f"{where}: a collection of ids was expected, found {type(v).__name__}")
    out: set[str] = set()
    for x in v:
        if not isinstance(x, str):
            raise JsonShapeError(f"{where}: an id was expected, found {type(x).__name__}")
        out.add(x)
    return out


# ------------------------------------------------- recovering from an interrupted earlier sync

def sync_generation(oid: str) -> int | None:
    """The generation of the sync that made this object id (None: not made by a sync)."""
    m = SYNC_ID.fullmatch(oid or "")
    return int(m.group(1)) if m else None


def base_generation(base: JsonMap) -> int:
    """The generation of the sync that wrote `base` (0: convert's own)."""
    return as_int(base.get("generation", 0), "base.generation")


def delete_request(oid: str) -> JsonObject:
    return google_types.slides_json({"deleteObject": {"objectId": oid}})


def delete_file(drive: DriveService, fid: str) -> None:
    execute(drive.files().delete(fileId=fid))


def without_scratch(pres: Presentation) -> Presentation:
    """The deck without `measure_places`' scratch slides (SCRATCH)."""
    kept = pres.copy()
    kept["slides"] = [s for s in pres.get("slides", []) if not SCRATCH.fullmatch(object_id(s))]
    return kept


@dataclass(frozen=True, kw_only=True)
class Heal:
    """A base element whose own objects are gone, and the objects an interrupted sync made for it
    (`object_id` the one carrying its tag, first of `objects`)."""
    slide: str
    element: str
    object_id: str
    objects: list[str]


@dataclass(frozen=True, kw_only=True)
class Recovery:
    """What `plan_recovery` found an interrupted earlier sync left in the deck."""
    sweep: list[str]
    sweep_slides: list[str]
    heal: list[Heal]
    restore: dict[str, JsonObject]


def no_recovery() -> Recovery:
    """Nothing to recover (a sync before it read the deck)."""
    return Recovery(sweep=[], sweep_slides=[], heal=[], restore={})


def plan_recovery(base: JsonObject, theirs: JsonObject, ours_keys: Sequence[str], trust_generation: bool) -> Recovery:
    """What an earlier sync that died halfway left in the deck (docs/sync.md, "If a sync dies").

    `theirs`: snapshot.read_presentation of the live deck. `ours_keys`: the new conversion's slide
    keys. `trust_generation`: see below.

    - **sweep**: objects (and slides) to delete. They were created by a sync of a later generation
      than this base - only our own code makes such ids - and the base's own objects for that
      element are all still there, so they are duplicates of something the deck already has. The
      base's `cleanup` list (written with a new base, before the old objects were deleted) and the
      `pending` block (written before the first write) name them; the id is the fallback for a deck
      whose base was lost or is behind.
    - **heal**: an element whose own objects are gone but whose replacement is on the slide, tagged
      with its key. A sync of an older beamer2slides deleted before the new base was stored, so the
      base doesn't know this object; without healing the merge would report the element as deleted
      in the deck and keep nothing. The element takes the object over.
    - **restore**: objects an interrupted sync rewrote in place (title placeholders): the person's
      version was recorded in `pending.in_place` before the write and is put back into the read-back
      the merge compares against, so their edit is re-applied instead of quietly adopted.

    `trust_generation` is False when this base may be behind the deck (the deck names a base in
    Drive that cannot be read, `snapshot.stale_base_warning`, docs/sync.md "Two checkouts"). A
    later generation then says nothing - the objects may be the finished work of a sync from the
    other checkout - so only what this base itself names is swept. Healing still happens: it takes
    an object over instead of deleting it, which a base that is behind cannot make wrong."""
    gen = as_int(base.get("generation", 0), "base.generation")
    pending = _obj(base.get("pending"), "base.pending")
    cleanup = _strs(base.get("cleanup"), "base.cleanup")
    named = set(cleanup)
    for ids in _obj(pending.get("objects"), "base.pending.objects").values():
        named.update(_strs(ids, "base.pending.objects"))
    named_slides = set(_strs(pending.get("slides"), "base.pending.slides")) | set(cleanup)
    base_objects: set[str] = set()
    alive: dict[tuple[str, str], bool] = {}
    live = {oid for s in _slides(theirs, "theirs") for oid in _objects(s, "theirs slide")}
    base_slides_json = _slides(base, "base")
    for b in base_slides_json:
        bkey = as_str(b["key"], "base slide key")
        for el in as_objects(b["elements"], f"base slide {bkey}: elements"):
            oids = [o for o in _strs(el.get("objects"), f"base slide {bkey}: objects") if o]
            base_objects.update(oids)
            alive[(bkey, as_str(el["key"], "element key"))] = bool(oids) and all(o in live for o in oids)
        base_objects.update(_strs(b.get("groups"), f"base slide {bkey}: groups"))
    base_slides = {as_str(b["objectId"], "base slide objectId") for b in base_slides_json if b.get("objectId")}
    wanted = {h6(k) for k in ours_keys}

    sweep: list[str] = []
    sweep_slides: list[str] = []
    heal: list[Heal] = []
    parent: dict[str, str | None] = {}
    for s in _slides(theirs, "theirs"):
        sid = as_str(s["objectId"], "theirs slide objectId")
        objects = _objects(s, f"theirs slide {sid}")
        for oid, rb in objects.items():
            parent[oid] = as_optional_str(as_object(rb, f"read-back {oid}").get("parent_group"), f"read-back {oid}")
        if sid not in base_slides:
            g = sync_generation(sid)
            if sid in named_slides or (g is not None and g > gen and sid[4:10] in wanted):
                sweep_slides.append(sid)
                continue
        mine: list[str] = []
        here: list[tuple[str, str, str]] = []
        for oid, rb in objects.items():
            if oid in base_objects:
                continue
            g = sync_generation(oid)
            if oid not in named and (g is None or g <= gen):
                continue  # a person's object, or one of this base's own generation
            title = _text(as_object(rb, f"read-back {oid}").get("title"), f"read-back {oid}: title")
            key = title[len(snapshot.TAG_PREFIX):] if title.startswith(snapshot.TAG_PREFIX) else ""
            skey, _, ekey = key.partition("/")  # the element key has slashes of its own
            if key and ekey and not alive.get((skey, ekey), True):
                here.append((skey, ekey, oid))
            else:
                mine.append(oid)
        healed = [oid for _, _, oid in here]
        # A healed element's own group, number box and anchored pictures carry no tag of their own.
        sweep += [oid for oid in mine if not any(oid.startswith(m) for m in healed)]
        heal += [Heal(slide=skey, element=ekey, object_id=oid,
                      objects=[oid] + [o for o in mine if o != oid and o.startswith(oid)]) for skey, ekey, oid in here]
    # Slides deletes a group's children with the group, so naming them as well would make it refuse
    # the whole batch ("The object ... could not be found") and nothing at all would be swept.
    doomed = set(sweep)

    def inside_a_doomed_group(oid: str) -> bool:
        seen: set[str] = set()
        p = parent.get(oid)
        while p and p not in seen:
            if p in doomed:
                return True
            seen.add(p)
            p = parent.get(p)
        return False

    sweep = [oid for oid in sweep if not inside_a_doomed_group(oid)]
    if not trust_generation:
        sweep = [oid for oid in sweep if oid in named]
        sweep_slides = [sid for sid in sweep_slides if sid in named_slides]
    restore = {oid: as_object(rb, f"base.pending.in_place.{oid}")
               for oid, rb in _obj(pending.get("in_place"), "base.pending.in_place").items()}
    return Recovery(sweep=sweep, sweep_slides=sweep_slides, heal=heal, restore=restore)


def heal_base(base: JsonObject, heal: Sequence[Heal], theirs: JsonObject, same_source: bool) -> list[str]:
    """Give the base elements in `heal` the objects an interrupted sync made for them, with their
    live read-back (so the merge sees the converter's own work, not a deck edit). When that sync
    converted another PDF than the one being synced now, what those objects show is unknown: the
    element's hashes are cleared so the source is written over them again."""
    by_key = {(as_str(b["key"], "base slide key"), as_str(el["key"], "element key")): el
              for b in _slides(base, "base") for el in as_objects(b["elements"], "base slide elements")}
    objects = {oid: rb for s in _slides(theirs, "theirs") for oid, rb in _objects(s, "theirs slide").items()}
    done: list[str] = []
    for h in heal:
        el = by_key.get((h.slide, h.element))
        if el is None:
            continue
        oids = h.objects
        el["objects"] = _json_strs(oids)
        el["main"] = oids[0]
        readback: JsonObject = {oid: objects[oid] for oid in oids if oid in objects}
        el["readback"] = readback
        if not same_source:
            el["ir_hash"] = "interrupted"
            fields: JsonObject = {k: "interrupted" for k in as_object(el.get("fields", {}), "element fields")}
            el["fields"] = fields
        done.append(f"{h.slide}/{h.element}")
    return done


def restore_in_place(theirs: JsonObject, saved: Mapping[str, JsonObject]) -> list[str]:
    """Put the text an interrupted sync overwrote in a placeholder back into the read-back (see
    plan_recovery): the merge then re-applies the person's edit to the recreated element."""
    back: list[str] = []
    for s in _slides(theirs, "theirs"):
        objects = _objects(s, "theirs slide")
        for oid, rb_json in list(objects.items()):
            rb = as_object(rb_json, f"read-back {oid}")
            old = saved.get(oid)
            if old and _text(old.get("text"), "saved text") != _text(rb.get("text"), f"read-back {oid}: text"):
                restored: JsonObject = {**rb, **{k: old[k] for k in IN_PLACE_FIELDS if k in old}}
                objects[oid] = restored
                back.append(oid)
    return back


def table_refill(base_el: JsonObject | None, el: JsonObject, objects: Mapping[str, JsonObject], scale: float,
                 fonts: "FontMapper") -> "TableFill | None":
    """A table the source rewrote that can be refilled where it is (`TableFill`), or None when it
    has to be made again.

    A table `convert` wrote came with the .pptx and carries cell margins (`emit.pptx_table`) the API
    can neither read nor set, and that keep the PDF's row pitch; one made by createTable has 7.2 pt
    above and below every line (docs/calibration.md "Table cells"), so a recreated table's rows sit
    up to ~7 pt from where a fresh conversion puts them. The base records the margins
    (`table_margins`), and when the grid, the merges, the fills and the margins are what the
    source's new version needs, only the words change: the cells are emptied and filled as emit
    fills an imported table. `cells`: the (row, column) of cells with text now; `shift`: how far the
    source moved the table's corner (Slides pt), which the caller applies unless the deck's own
    position wins (`update_slide`).

    A table without merges or fills whose rows or columns the source added or removed is refilled
    too when inserted and deleted rows can give it the margins it needs (`table_steps`: a new row
    or column takes those of the one it is inserted beside, tools/probe_pptx_table_margins.py);
    `steps` are those requests, sent after the cells are emptied and before they are filled."""
    from .emit import pptx_table
    if not base_el or el.get("kind") != "table" or base_el.get("kind") != "table":
        return None
    old = _obj(base_el.get("ir"), "base element ir")
    margins = base_el.get("table_margins")
    main = base_el.get("main")
    live = objects.get(main) if isinstance(main, str) else None
    if not margins or not live or "cells" not in old:
        return None
    was_dims = [len(as_array(old["cells"], "ir.cells")), len(as_array(old["columns"], "ir.columns"))]
    dims = [len(as_array(el["cells"], "ir.cells")), len(as_array(el["columns"], "ir.columns"))]
    live_dims = [as_int(n, "read-back table") for n in _arr(live.get("table"), "read-back table")]
    if live_dims != was_dims:
        return None  # (the deck added or removed a row or column)
    if was_dims == dims:
        if old.get("merges", []) != el.get("merges", []) or old.get("fills", []) != el.get("fills", []):
            return None  # (a merge or a fill can't be taken back by filling cells)
    elif old.get("merges") or el.get("merges") or old.get("fills") or el.get("fills"):
        return None  # (they are placed by row and column: another grid moves them)
    grid = merge.table_grid(as_optional_str(live.get("text"), "read-back text"), live_dims)
    if grid is None:
        return None
    new, was = pptx_table(el, scale, fonts), pptx_table(old, scale, fonts)
    have = [_nums(m, "table_margins") for m in as_array(margins, "table_margins")]
    steps = table_steps(have, new["margins"], was_dims[1], dims[1])
    if steps is None:
        return None
    # (the margins the table has - nothing can change them - not the new conversion's, a rounding off)
    return TableFill(id=as_str(main, "base element main"), margins=steps[1],
                     shift=(new["x"] - was["x"], new["y"] - was["y"]),
                     cells=[(r, c) for r, row in enumerate(grid) for c, text in enumerate(row) if text],
                     steps=steps[0])


TABLE_MARGIN_TOL = 0.05  # pt: margins this close are the same (a rounding of the .pptx's EMU)


def table_steps(have: Sequence[Sequence[float]], need: Sequence[Sequence[float]], cols: int, want_cols: int
                ) -> tuple[list[JsonObject], list[list[float]]] | None:
    """Row and column requests (with the table's id left as None) turning a table whose rows have
    the cell margins `have` into one of len(need) rows with the margins `need` and `want_cols`
    columns, and the margins it then has; None when no such steps exist. A row inserted below
    another takes its margins; a column's cells take those of their row whatever it is inserted
    beside, so columns go and come at the right edge."""
    def same(a: Sequence[float], b: Sequence[float]) -> bool:
        return all(abs(x - y) <= TABLE_MARGIN_TOL for x, y in zip(a, b))

    cur = [list(m) for m in have]
    reqs: list[JsonObject] = []
    while True:
        i = next((k for k in range(min(len(cur), len(need))) if not same(cur[k], need[k])), min(len(cur), len(need)))
        if len(cur) == len(need):
            if i < len(cur):
                return None
            break
        if len(cur) > len(need):
            reqs.append(google_types.slides_json({"deleteTableRow": {
                "tableObjectId": None, "cellLocation": {"rowIndex": i, "columnIndex": 0}}}))
            del cur[i]
        elif i > 0 and same(cur[i - 1], need[i]):
            reqs.append(google_types.slides_json({"insertTableRows": {
                "tableObjectId": None, "cellLocation": {"rowIndex": i - 1, "columnIndex": 0},
                "insertBelow": True, "number": 1}}))
            cur.insert(i, list(cur[i - 1]))
        else:
            return None
    if want_cols > cols:
        reqs.append(google_types.slides_json({"insertTableColumns": {
            "tableObjectId": None, "cellLocation": {"rowIndex": 0, "columnIndex": cols - 1},
            "insertRight": True, "number": want_cols - cols}}))
    for c in range(cols - 1, want_cols - 1, -1):
        reqs.append(google_types.slides_json({"deleteTableColumn": {
            "tableObjectId": None, "cellLocation": {"rowIndex": 0, "columnIndex": c}}}))
    return reqs, cur


def drop_objects(pres: Presentation, objects: Collection[str], slides: Collection[str]) -> Presentation:
    """A presentations.get without these page elements and slides (leftovers of an interrupted
    sync): everything downstream then plans as if they had never been created."""
    gone, gone_slides = set(objects), set(slides)

    def keep(elements: list[PageElement], where: str) -> list[PageElement]:
        out: list[PageElement] = []
        for e in elements:
            if object_id(e) in gone:
                continue
            group = e.get("elementGroup")
            if group is not None:
                kids = keep(google_types.children(e, where), f"{where}/{object_id(e)}")
                if not kids:
                    continue
                kept: list[Json] = [google_types.as_json(k, where) for k in kids]
                trimmed: PageElement = {**e, "elementGroup": {**group, "children": kept}}
                out.append(trimmed)
                continue
            out.append(e)
        return out
    return {**pres, "slides": [{**s, "pageElements": keep(s.get("pageElements", []), object_id(s))}
                               for s in pres.get("slides", []) if object_id(s) not in gone_slides]}


# ---------------------------------------------------------------- ours

@dataclass(frozen=True, kw_only=True)
class Built:
    """The new conversion (`build_ours_of`): the PDF synced (`source`) and the one converted (`pdf`,
    notes taken out), the folder it was rendered into (`out`), its plan and deck.json, its keyed
    slide entries (`snapshot.slide_entries`), `pairs` (ours index -> base index), the label moves,
    weak pairs and near misses the keying found, and what the report says of it: context changes
    sync cannot write or could not read, elements emit could not plan, and the base's old forms."""
    source: Path
    pdf: Path
    out: Path
    plan: "DeckPlan"
    deck: JsonObject
    slides: list[JsonObject]
    pairs: dict[int, int]
    label_moves: list[JsonObject]
    weak_pairs: dict[int, str]
    near_misses: list[JsonObject]
    context_unwritten: "list[Unwritten]"
    context_unread: list["Unread"]
    contained: list["Contained"]
    base_forms: list[snapshot.BaseForm]


def ours_json(b: Built) -> JsonObject:
    """What the merge reads of the new conversion (`merge.ours_of`), as JSON: the same slide entries,
    and the pairs' keys as JSON has them. `Sync.ours` is this view."""
    pairs: JsonObject = {str(k): v for k, v in b.pairs.items()}
    weak: JsonObject = {str(k): v for k, v in b.weak_pairs.items()}
    slides: list[Json] = [s for s in b.slides]
    moves: list[Json] = [m for m in b.label_moves]
    near: list[Json] = [m for m in b.near_misses]
    return {"slides": slides, "pairs": pairs, "label_moves": moves, "weak_pairs": weak, "near_misses": near}


def theme_ours_of(b: Built) -> "ThemeOurs":
    """What theme sync reads of the new conversion (`theme_sync.theme_ours` of the dict entry)."""
    from .theme_sync import ThemeOurs

    return ThemeOurs(deck=b.deck, out=b.out, scale=b.plan.scale, fonts=b.plan.fonts, pairs=dict(b.pairs),
                     keys=tuple(as_str(s["key"], "slides.key") for s in b.slides))


def build_ours(pdf: Path, work: Path, base: JsonObject, overlays: str, page_width: float,
               pictures: snapshot.BasePictures) -> JsonObject:
    """`build_ours_of` as the JSON the merge and the oracles read (`ours_json`): the dict entry the
    fuzzers and `edit_hunt` call. What else the conversion holds (its plan, deck, folder, what the
    report says of it) is the record's: `build_ours_of`."""
    return ours_json(build_ours_of(pdf, work, base, overlays, page_width, pictures))


def build_ours_of(pdf: Path, work: Path, base: JsonObject, overlays: str, page_width: float,
                  pictures: snapshot.BasePictures) -> Built:
    """extract, classify and render the new PDF into `work`, plan it like emit and give its
    slides and elements the keys of the base they match.

    `page_width`: how wide the deck this will be written into is, in slide pt (emit's SLIDE_W for
    the frame `convert` makes). A deck `adopt` took over is whatever size the person made it,
    and every box this plan holds is PDF pt times the scale that width gives.

    `base` is brought to today's form in place before anything is compared with it (`base_today`;
    `pictures`: where its picture files are, found before this render could write over them -
    `snapshot.hold_base_pictures`), and what that found is `base_forms`.

    A base `adopt` recorded also carries the deck's own boxes (`adopt_sync.deck_folds`), and what
    one of them the converter reads back as several is put together again before anything is keyed
    (`adopt_sync.fold_slides`) - the same fold the base's own side was built with, or the two sides
    of the merge would not be describing the same boxes.

    An element emit cannot plan is the picture of its region, as `convert` makes it (`planned`):
    listed in "contained" with its slide and element keys, for the report."""
    from . import adopt_sync
    from .classify import classify
    from .extract import extract, select_overlays
    from .marked import shape_marks
    from .notes import prepare
    from .render import render_backgrounds

    work.mkdir(parents=True, exist_ok=True)
    prepared = prepare(pdf, work)
    raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, overlays)
    deck = classify(raw)
    render_backgrounds(prepared.pdf, raw, deck, work, shape_marks(base))
    adopt = base.get("adopt")
    boxes = as_object(adopt, "base.adopt").get("boxes") if adopt is not None else None
    folds: dict[str, list[JsonObject]] = {}
    for label, objects in (as_object(boxes, "base.adopt.boxes").items() if boxes is not None else ()):
        folds[label] = as_objects(objects, f"base.adopt.boxes.{label}")
    adopt_sync.fold_slides(deck, folds)
    (work / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    plan = planned(deck, prepared.pdf, work, page_width)
    originals = contained_originals(deck, [(c["page"], c["id"]) for c in plan.contained])
    deck = plan.deck
    ours_slides = plan.slides()
    infos = [identity.slide_info_of(s) for s in ours_slides]
    base_slides = as_objects(base["slides"], "base.slides")
    base_keys = [as_str(b["key"], "base slide key") for b in base_slides]
    base_infos = [identity.base_slide_info(b, k) for b, k in zip(base_slides, base_keys)]
    found = identity.label_moves_of(base_infos, infos)
    moves = [identity.reported_move(m, base_keys, infos) for m in found]
    weak: dict[int, str] = {}
    keys, pairs = identity.inherit_slide_keys(base_infos, base_keys, infos, found, weak)
    near = [identity.reported_near_miss(m, base_keys, infos)
            for m in identity.near_misses_of(base_infos, infos, pairs)]
    ekeys: list[list[ElementKey]] = []
    fps: list[list[JsonObject]] = []
    for j, slide in enumerate(ours_slides):
        matched = identity.base_items(as_objects(base_slides[pairs[j]]["elements"], f"base slide {base_keys[pairs[j]]}: "
                                                 "elements")) if j in pairs else None
        k, f = identity.slide_element_keys(as_objects(slide["elements"], "elements"), work, matched)
        ekeys.append(k)
        fps.append(f)
    entries = snapshot.slide_entries(deck, work, keys, ekeys, fps)
    # (the old forms an adopt base recorded are brought up to what the source says, contained or not)
    view_slides: list[Json] = []
    for s in ours_slides:
        page = as_int(s["page"], "slide.page")
        elements: list[Json] = [originals.get((page, as_str(e["id"], "element id")), e)
                                for e in as_objects(s["elements"], "elements")]
        view_slide: JsonObject = {**s, "elements": elements}
        view_slides.append(view_slide)
    view: JsonObject = {"slides": view_slides}  # (its readers take the slides alone)
    forms = base_today(base, view, pictures)
    base_as_contained(base, entries, view, pairs, keys, work, originals)
    unread: list[Unread] = []
    unwritten = mark_emitted(base, entries, deck, pairs, plan.scale, plan.fonts, True, unread)
    at: dict[tuple[int, str], tuple[str | None, str | None]] = {
        (as_int(s["page"], "slide.page"), as_str(el["id"], "element id")):
            (as_str(o["key"], "slide key"), as_str(e["key"], "element key"))
        for s, o in zip(ours_slides, entries)
        for el, e in zip(as_objects(s["elements"], "elements"), as_objects(o["elements"], "entry elements"))}
    contained: list[Contained] = []
    for c in plan.contained:
        page, eid, kind = c["page"], c["id"], c["kind"]
        slide_key, element_key = at.get((page, eid), (None, None))
        was = originals.get((page, eid)) or {}
        words = " ".join(identity.plain_text(was).split()) if was.get("kind") == "text" else ""
        contained.append(Contained(slide=slide_key, element=element_key, page=page, id=eid,
                                   kind=kind, error=c["error"], words=words))
    return Built(source=pdf, pdf=prepared.pdf, out=work, plan=plan, deck=deck, slides=entries, pairs=pairs,
                 label_moves=moves, weak_pairs=weak, near_misses=near, context_unwritten=unwritten,
                 context_unread=unread, contained=contained, base_forms=forms)


def base_today(base: JsonObject, view: JsonObject, pictures: snapshot.BasePictures) -> list[snapshot.BaseForm]:
    """In place: `base` brought to today's form, the one place a base is, before anything compares
    its hashes with the new conversion's. An adopt base's marked shapes and tables from before
    688ebf4 first, whose missing fields come from our element of the same mark (`view`, the new
    conversion as classify read it); then every element through the IR parser
    (`snapshot.rehash_base`; `pictures`: where its picture files may be). A form the converter now
    writes differently is then a rewrite of the base, not a change of the source."""
    from . import adopt_sync

    page_key = snapshot.base_page_key(base)
    return [*adopt_sync.upgrade_shapes(base, view, page_key), *adopt_sync.upgrade_tables(base, view, page_key),
            *snapshot.rehash_base(base, pictures)]


@dataclass(frozen=True, kw_only=True)
class Contained:
    """An element of the new conversion emit could not plan, the picture of its region now
    (`planned`). `slide` / `element`: its keys (None when it is not in the keyed deck); `page`, `id`:
    where emit has it; `words`: what it said when it was text, since a person does not know
    "image/fallback/0"."""
    slide: str | None
    element: str | None
    page: int
    id: str
    kind: str | None
    error: str
    words: str


@dataclass(frozen=True, kw_only=True)
class ContainedFound:
    """A `Contained` element, and whether this sync writes its unit (else the deck keeps what the
    last sync or `convert` wrote there)."""
    item: Contained
    written: bool


# Which of `mark_emitted`'s two emissions of a slide could not be worked out.
UnreadSide = Literal["base", "ours"]


@dataclass(frozen=True, kw_only=True)
class Unread:
    """A slide `mark_emitted` could not compare, and why (`unread_warnings` says it)."""
    slide: str
    side: UnreadSide
    error: str


# ---------------------------------------------------------------- the merge plan, as sync reads it

def slide_units(p: merge.SlidePlan) -> tuple[merge.PlannedUnit, ...]:
    """The units a slide's plan decides: only an updated slide has any (a held one is written nothing)."""
    match p:
        case merge.UpdateSlide():
            return p.units
        case merge.CreateSlide() | merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide():
            return ()
        case _:
            assert_never(p)


def ours_index(p: merge.SlidePlan) -> int | None:
    """The new conversion's slide a plan writes or keeps its place for (None: the source dropped it)."""
    match p:
        case merge.CreateSlide() | merge.GoneSlide() | merge.HoldSlide() | merge.UpdateSlide():
            return p.ours
        case merge.KeepRemovedSlide() | merge.DeleteSlide():
            return None
        case _:
            assert_never(p)


def base_index(p: merge.SlidePlan) -> int | None:
    """The base's slide a plan is about (None: a slide the source adds)."""
    match p:
        case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide() | merge.UpdateSlide():
            return p.base
        case merge.CreateSlide():
            return None
        case _:
            assert_never(p)


def live_id(p: merge.SlidePlan) -> str | None:
    """The live slide a plan is about (None: one to create, or one the deck deleted)."""
    match p:
        case merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide() | merge.UpdateSlide():
            return p.object_id
        case merge.CreateSlide() | merge.GoneSlide():
            return None
        case _:
            assert_never(p)


def new_background(p: merge.SlidePlan) -> str | None:
    """The source's background this sync writes onto an updated slide; None where it writes none (a
    background the source took away is not written either: `merge.has_writes_of`, `update_slide`)."""
    match p:
        case merge.UpdateSlide():
            return p.background if p.background_written and p.background else None
        case merge.CreateSlide() | merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide():
            return None
        case _:
            assert_never(p)


def written(d: merge.UnitDecision) -> bool:
    """Whether the unit's objects are made anew: created, or recreated with the deck's edits back on."""
    match d:
        case merge.CreateUnit() | merge.Recreate():
            return True
        case merge.AdoptObject() | merge.GoneUnit() | merge.DeleteUnit() | merge.KeepRemoved() | merge.KeptJoined() | \
                merge.KeepUnit() | merge.AdoptUnit() | merge.MoveUnit():
            return False
        case _:
            assert_never(d)


def kept_whole(d: merge.UnitDecision) -> bool:
    """A unit this sync writes nothing to (the plan's JSON says `keep` or `none`)."""
    match d:
        case merge.GoneUnit() | merge.KeepRemoved() | merge.KeptJoined() | merge.KeepUnit():
            return True
        case merge.AdoptObject() | merge.CreateUnit() | merge.DeleteUnit() | merge.Recreate() | merge.AdoptUnit() | \
                merge.MoveUnit():
            return False
        case _:
            assert_never(d)


_ACTION_FIELDS = ("key", "action", "source", "deck", "unpaired", "inherited", "in_table")


def plan_actions(mplan: merge.MergePlan) -> list[Json]:
    """What the report's `actions` list says of each slide: its action and the units anything
    happened to, in the words of the plan's JSON (`merge.slide_plan_json`)."""
    out: list[Json] = []
    for p in mplan.slides:
        units: list[Json] = []
        for u in slide_units(p):
            j = merge.planned_unit_json(u)
            if kept_whole(u.decision) and not any(j.get(k) for k in ("deck", "unpaired", "inherited", "in_table")):
                continue
            units.append({k: j[k] for k in _ACTION_FIELDS if k in j})
        out.append({"slide": p.key, "action": merge.slide_plan_json(p)["action"], "units": units})
    return out


# The plan as JSON, read back into its records: for the callers that still hand sync a plan written as
# `merge.plan_merge` writes it (the offline fuzz replays `restack`, `regroups` and `move_requests` over
# its own plans, `fuzz_sync._stacked`), and the tests that write one by hand.

_BLIND: tuple[merge.Blind, ...] = ("inherited", "in_table", "unpaired")


def _keys(v: Json, where: str) -> tuple[str, ...]:
    return tuple(as_str(x, f"{where}[{i}]") for i, x in enumerate(as_array(v, where)))


def overrides_of(ov: JsonMap, where: str) -> merge.Overrides:
    """`merge.overrides_json` read back."""
    text: merge.TextOverride | merge.TableOverride | None = None
    if ov.get("text") is not None:
        t = as_object(ov["text"], f"{where}.text")
        if t.get("table"):
            dims = t.get("dims")
            d = None if dims is None else as_array(dims, f"{where}.text.dims")
            text = merge.TableOverride(base=as_str(t["base"], f"{where}.text.base"),
                                       theirs=as_str(t["theirs"], f"{where}.text.theirs"),
                                       dims=None if d is None else (as_int(d[0], f"{where}.dims"), as_int(d[1], f"{where}.dims")))
        else:
            text = merge.TextOverride(base=as_str(t["base"], f"{where}.text.base"),
                                      theirs=as_str(t["theirs"], f"{where}.text.theirs"),
                                      take=tuple(as_int(k, f"{where}.text.take") for k in as_array(t.get("take") or [], where)))
    style: merge.StyleOverride | None = None
    if ov.get("text_style") is not None:
        s = as_object(ov["text_style"], f"{where}.text_style")
        style = merge.StyleOverride(runs=as_object(s.get("runs") or {}, f"{where}.text_style.runs"),
                                    paragraphs=as_object(s.get("paragraphs") or {}, f"{where}.text_style.paragraphs"),
                                    ranges=bool(s.get("ranges")))
    shape = ov.get("shape_style")
    return merge.Overrides(text=text, text_style=style,
                           shape_style=None if shape is None else as_object(shape, f"{where}.shape_style"),
                           geometry="geometry" in ov)


def unit_decision_of(u: JsonMap, where: str) -> merge.UnitDecision:
    """`merge.unit_json` read back. A hand-written unit may leave out what its readers do not read."""
    key = ElementKey(as_str(u["key"], f"{where}.key"))
    source, deck = _keys(u.get("source") or [], f"{where}.source"), _keys(u.get("deck") or [], f"{where}.deck")
    action = u["action"]
    if action == "adopt_object":
        return merge.AdoptObject(key=key, object_id=ObjectId(as_str(u["objectId"], f"{where}.objectId")))
    if action == "create":
        return merge.CreateUnit(key=key)
    if action == "none":
        return merge.GoneUnit(key=key)
    if action == "delete":
        return merge.DeleteUnit(key=key)
    if action == "keep":
        if u.get("removed"):
            return merge.KeepRemoved(key=key, deck=deck)
        if "source" not in u and "deck" not in u:
            return merge.KeptJoined(key=key)
        blind: tuple[merge.Blind, tuple[str, ...]] | None = None
        for why in _BLIND:
            if why in u:
                blind = (why, _keys(u[why], f"{where}.{why}"))
        return merge.KeepUnit(key=key, source=source, deck=deck, blind=blind)
    if action == "recreate":
        return merge.Recreate(key=key, source=source, deck=deck,
                              overrides=overrides_of(as_object(u.get("overrides") or {}, f"{where}.overrides"),
                                                     f"{where}.overrides"))
    if action == "adopt":
        return merge.AdoptUnit(key=key, source=source, deck=deck, adopt=_keys(u.get("adopt") or [], f"{where}.adopt"))
    if action == "move":
        delta = as_array(u["delta"], f"{where}.delta")
        return merge.MoveUnit(key=key, source=source, deck=deck,
                              delta=(_number(delta[0], f"{where}.delta"), _number(delta[1], f"{where}.delta")))
    raise JsonShapeError(f"{where}.action: a unit's action was expected, found {action!r}")


def planned_unit_of(u: JsonMap, where: str) -> merge.PlannedUnit:
    """`merge.planned_unit_json` read back."""
    return merge.PlannedUnit(
        decision=unit_decision_of(u, where),
        base_members=tuple(ElementKey(k) for k in _keys(u.get("base_members") or [], f"{where}.base_members")),
        ours_members=tuple(ElementKey(k) for k in _keys(u.get("ours_members") or [], f"{where}.ours_members")))


def slide_plan_of(p: JsonMap, where: str) -> merge.SlidePlan:
    """`merge.slide_plan_json` read back."""
    key = SlideKey(as_str(p["key"], f"{where}.key"))
    action = p["action"]
    if action == "create":
        return merge.CreateSlide(key=key, ours=as_int(p["ours"], f"{where}.ours"))
    if action == "gone":
        return merge.GoneSlide(key=key, ours=as_int(p["ours"], f"{where}.ours"), base=as_int(p["base"], f"{where}.base"))
    oid = ObjectId(as_str(p["objectId"], f"{where}.objectId"))
    if action == "keep_removed":
        return merge.KeepRemovedSlide(key=key, base=as_int(p["base"], f"{where}.base"), object_id=oid)
    if action == "delete":
        return merge.DeleteSlide(key=key, base=as_int(p["base"], f"{where}.base"), object_id=oid)
    if action != "update":
        raise JsonShapeError(f"{where}.action: a slide's action was expected, found {action!r}")
    if p.get("held"):
        return merge.HoldSlide(key=key, ours=as_int(p["ours"], f"{where}.ours"), base=as_int(p["base"], f"{where}.base"),
                               object_id=oid)
    return merge.UpdateSlide(
        key=key, ours=as_int(p["ours"], f"{where}.ours"), base=as_int(p["base"], f"{where}.base"), object_id=oid,
        units=tuple(planned_unit_of(u, f"{where}.units[{n}]") for n, u in enumerate(as_objects(p["units"], f"{where}.units"))),
        background_written="background" in p, background=as_optional_str(p.get("background"), f"{where}.background"),
        notes=as_optional_str(p.get("notes"), f"{where}.notes"))


def _number(v: Json, where: str) -> float:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected")


@dataclass(frozen=True, kw_only=True)
class InPlace:
    """A live placeholder an element of the new conversion is written into instead of a new box:
    `size` its box (pt), `text` what it holds now (emptied first when there is any)."""
    id: str
    size: tuple[float, float]
    text: str


@dataclass(frozen=True, kw_only=True)
class TableFill:
    """A live table refilled where it is (`table_refill`): the cells holding words now are emptied,
    the row and column `steps` sent, then it is filled as emit fills an imported table. `shift`:
    the source's move of it (slide pt); `margins`: the cell margins it then has, for the base."""
    id: str
    cells: list[tuple[int, int]]
    steps: list[JsonObject]
    shift: tuple[float, float]
    margins: list[list[float]]


Refilled = InPlace | TableFill
"""A live object an element is written into instead of a new one (`SlideWork.in_place`)."""


class Regroup(TypedDict):
    """A group a slide's rewrite takes apart (`Sync.regroups_of`): the old roots of rewritten units
    in it (`remove`), root -> unit key (`unit_of`). A dict, as the offline fuzz's replay reads it."""
    remove: set[str]
    unit_of: dict[str, str]


def placeholder_in_place(oid: str, rb: JsonMap) -> InPlace:
    """A live placeholder (`rb`: its read-back) written into as it is."""
    size = _nums(rb["size"], f"read-back {oid}: size")
    return InPlace(id=oid, size=(size[0], size[1]), text=_text(rb.get("text"), f"read-back {oid}: text").strip())


def updated(p: merge.SlidePlan) -> merge.UpdateSlide | merge.HoldSlide:
    """A plan for a slide the deck keeps and the new conversion has (`Sync.update_slide`)."""
    match p:
        case merge.UpdateSlide() | merge.HoldSlide():
            return p
        case merge.CreateSlide() | merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide():
            raise ValueError(f"slide {p.key}: only a kept slide is updated")
        case _:
            assert_never(p)


class SlideWork:
    """One slide's part of what a sync writes (`Sync.prepare`): its plan, the slide it is written on
    (`sid`: None for one nothing is written to) and the new conversion's elements made on it
    (`units`). `new_slide` / `update_slide` fill in what they made, which the later phases read:
    element index -> the objects made (`objects`) and its main object (`new_oid`), the live objects
    written into (`in_place`), unit key -> its new outermost object (`tops`), the old objects the
    cleanup deletes (`doomed`) and the slide-level groups made (`groups`)."""

    def __init__(self, plan: merge.SlidePlan, sid: str | None, units: list[int]) -> None:
        self.plan = plan
        self.sid = sid
        self.units = units
        self.objects: dict[int, list[str]] = {}
        self.new_oid: dict[int, str] = {}
        self.in_place: dict[int, Refilled] = {}
        self.tops: dict[str, str] = {}
        self.doomed: set[str] = set()
        self.groups: list[str] = []


@dataclass(frozen=True, kw_only=True)
class Work:
    """What a sync writes (`Sync.prepare`): per slide, the pictures the staging deck brings (file ->
    "background" for a slide background, else None), the ids of the slides it creates (key -> id),
    PDF page -> live slide for internal links, whether anything is written, and the final slide order."""
    slides: list[SlideWork]
    pictures: dict[str, str | None]
    new_ids: dict[str, str]
    page_slide: dict[int, str]
    writes: bool
    order: list[str]


@dataclass(frozen=True, kw_only=True)
class RunResult:
    """What `Sync.run` planned and wrote: `theirs` is the deck it planned against
    (`snapshot.read_presentation`), `revision_id` the deck's after the write (None: nothing sent)."""
    attempts: int
    plan: merge.MergePlan
    work: Work
    theirs: JsonObject
    revision_id: str | None


def planned(deck: JsonObject, pdf: Path, work: Path, page_width: float) -> "DeckPlan":
    """`deck` (as classify wrote it) planned as sync writes it, its blocks merged (`emit.DeckPlan`).
    An element emit cannot plan (a field its producer never wrote, 688ebf4) is the picture of its
    region, as `convert` makes it (`DeckPlan.contain`, the plan's `contained`), cut out of `pdf` into
    `work`: the rest of the sync goes ahead instead of the whole of it dying before a write, and
    the picture is what a fresh conversion puts there, so a base convert recorded with it is no
    change. `adopt_sync.convert_source` plans the same way, for the same reason. Under
    `emit.strict()` (the offline suite) the failure is raised."""
    from .emit import DeckPlan
    from .emit_model import box_of
    from .render import crop_region

    plan = DeckPlan(deck, page_width, contain=True)
    gone = {(c["page"], c["id"]) for c in plan.contained}
    # (from the PDF this deck was read from: `emit.crop_fallbacks` would take a slides.pdf an
    # earlier sync's notes left in `work`)
    for slide in plan.slides():
        page = as_int(slide["page"], "slide.page")
        for el in as_objects(slide["elements"], "elements"):
            if (page, el["id"]) in gone and el.get("role") == "fallback":
                file = work / as_str(el["file"], "file")
                file.parent.mkdir(parents=True, exist_ok=True)
                crop_region(pdf, page, list(box_of(el["bbox"], "bbox")), file, 6.0)
    return plan


def contained_originals(deck: JsonObject, gone: list[tuple[int, str]]) -> dict[tuple[int, str], JsonObject]:
    """(page, element id) -> the element `planned` made a picture of, as classify wrote it (a block's
    merged: the plan merges them; `emit.fallback_element` keeps the id)."""
    from .emit import merge_blocks

    wanted = set(gone)
    out: dict[tuple[int, str], JsonObject] = {}
    for slide in as_objects(deck["slides"], "deck.slides") if wanted else []:
        page = as_int(slide["page"], "slide.page")
        elements = as_objects(slide["elements"], f"slide {page}: elements")
        merged: list[JsonObject] = merge_blocks(elements)
        for el in [*merged, *elements]:
            at = (page, as_str(el["id"], f"slide {page}: element id"))
            if at in wanted and at not in out:
                out[at] = el
    return out


def base_as_contained(base: JsonObject, entries: list[JsonObject], view: JsonObject, pairs: dict[int, int],
                      keys: Sequence[str], work: Path, originals: dict[tuple[int, str], JsonObject]
                      ) -> list[tuple[str, str]]:
    """In place: a base element the source has not changed since, which `planned` made a picture of
    this time, recorded as that picture - so the merge keeps what the deck has there.

    Containment happens before anything is keyed, and a picture is another element than the one the
    base records (another key), so an element the base holds in a form today's emit cannot write -
    an adopt base's line kept a shape by `marked.shape_marks`, a text an older converter wrote - was
    deleted from the deck and its picture put in, though neither side had changed it: the person's
    own objects in an adopted deck (hashing's six lines, audit 2026-09-29). The element as classify
    read it (`view`: the plan with each picture's original back) is keyed and hashed as the base's
    were; where the base's element of that key says the same, the base takes the picture's key and
    IR, and its objects and read-back stay (the deck side is compared by those alone). A source that
    did change it still has its picture written. Returns [(slide key, element key)] so recorded."""
    page_key = snapshot.page_keys(view, keys)
    base_slides = as_objects(base["slides"], "base.slides")
    done: list[tuple[str, str]] = []
    for j, slide in enumerate(as_objects(view["slides"], "deck.slides")):
        page = as_int(slide["page"], "slide.page")
        elements = as_objects(slide["elements"], f"slide {page}: elements")
        ids = [as_str(el["id"], f"slide {page}: element id") for el in elements]
        here = [i for i, eid in enumerate(ids) if (page, eid) in originals]
        if not here or j not in pairs:
            continue
        b_slide = base_slides[pairs[j]]
        slide_key = as_str(b_slide["key"], "base slide key")
        b_elements = as_objects(b_slide["elements"], f"base slide {slide_key}: elements")
        n_elements = as_objects(entries[j]["elements"], f"slide {page}: keyed elements")
        okeys, _ = identity.slide_element_keys(elements, work, identity.base_items(b_elements))
        key_of = dict(zip(ids, okeys))
        base_by = {as_str(e["key"], f"base slide {slide_key}: element key"): e for e in b_elements}
        for i in here:
            el, now = elements[i], n_elements[i]
            now_key = as_str(now["key"], f"slide {page}: element key")
            b = base_by.get(okeys[i])
            if b is None or b.get("removed"):
                continue  # (not the base's)
            b_key = as_str(b["key"], f"base slide {slide_key}: element key")
            if now_key != b_key and now_key in base_by:
                continue  # (another base element holds the picture's key)
            anchor = el.get("anchor")
            h, _ = identity.ir_fields(el, work, key_of.get(anchor) if isinstance(anchor, str) else None, page_key)
            if h != b.get("ir_hash"):
                continue  # the source changed it: its picture is written
            for other in b_elements:  # (what is anchored to it follows its key)
                if other.get("anchor") == b_key:
                    other["anchor"] = now_key
            base_by.pop(b_key)
            b.update({f: now[f] for f in ("key", "kind", "role", "ir", "ir_hash", "fields", "fingerprint", "anchor")})
            base_by[now_key] = b
            done.append((slide_key, now_key))
    return done


def contained_report(contained: list[Contained], slides: Sequence[JsonMap],
                     mplan: merge.MergePlan) -> tuple[list[ContainedFound], list[str]]:
    """build_ours' `contained` (`slides`: its keyed slides), each with whether this sync writes its
    unit, and the report's words for those it writes. A unit it leaves alone is in the deck as the
    last sync or `convert` wrote it: contained then too, or a source that has not changed it since."""
    made: set[tuple[str, str]] = set()
    for p in mplan.slides:
        match p:
            case merge.CreateSlide():
                o = slides[p.ours]
                made |= {(as_str(o["key"], "slide key"), as_str(e["key"], "element key"))
                         for e in as_objects(o["elements"], "slide elements")}
            case merge.UpdateSlide():
                o = slides[p.ours]
                made |= {(as_str(o["key"], "slide key"), k) for u in p.units if written(u.decision) for k in u.ours_members}
            case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide():
                pass  # (nothing of the new conversion goes onto it)
            case _:
                assert_never(p)
    found = [ContainedFound(item=c, written=c.slide is not None and c.element is not None and (c.slide, c.element) in made)
             for c in contained]

    def name(c: Contained) -> str:  # (by its words: a person does not know "image/fallback/0")
        if not c.words:
            return f"the {c.kind or 'element'} {c.id}"
        return f"the text \"{c.words[:40]}{'...' if len(c.words) > 40 else ''}\""
    says = [f"slide {f.item.slide}: this version of the converter could not lay out {name(f.item)} ({f.item.error}); "
            f"it goes into the deck as a picture of its region, as a fresh conversion makes it"
            for f in found if f.written]
    return found, says


def contained_json(found: list[ContainedFound]) -> list[Json]:
    """`contained_report`'s entries as the report (JSON) carries them."""
    return [{**asdict(f.item), "written": f.written} for f in found]


def unread_warnings(unread: list[Unread], slides: Sequence[JsonMap]) -> list[str]:
    """The report's words for the slides `mark_emitted` could not compare (`slides`: build_ours'
    keyed slides, for their titles): nothing is lost, but boxes the source left alone there keep
    the size and place they had, even where the source's changes beside them give them others in
    a fresh conversion - and nothing else says so."""
    titles = {as_str(s["key"], "slide key"): s.get("title") for s in slides}
    out = []
    for u in unread:
        title = titles.get(u.slide)
        where = f"slide {u.slide}" + (f" ({title})" if title and title != u.slide else "")
        match u.side:
            case "base":
                why = ("the sync base records it in a form this version of the converter cannot lay out (an "
                       "older version wrote it)")
            case "ours":
                why = "this version of the converter could not lay it out"
            case _:
                assert_never(u.side)
        out.append(f"{where}: {why} ({u.error}), so the boxes the source did not change there keep their "
                   f"size and place, even where the source's changes beside them would give them others")
    return out


def mark_emitted(base: JsonObject, entries: Sequence[JsonObject], deck: JsonMap, pairs: Mapping[int, int], scale: float,
                 fonts: "FontMapper", fast: bool, unread: list[Unread]) -> list["Unwritten"]:
    """Give an element whose own IR the source left alone (or only moved) a source change when emit
    now writes it differently all the same, because of what stands around it.

    Sync decides what the source changed element by element (`identity.source_changes`), but where
    emit puts an element is not its own business alone. A one-line box reaches to the mirror of the
    slide's leftmost body text (`emit.text_right_limit`), so a centred line the source added below a
    title narrowed a fresh conversion's title from 707 to 474 pt while sync, seeing the title's IR
    unchanged, kept the old box (live scenario table-moved). So for every paired slide on which
    anything changed, what emit would write for it (`emitted_elements`) is worked out twice with
    today's code - from the base's IR of the slide and from the new one - and compared element by
    element, a moved element's requests less its own step (`_close`). Both sides are marked, in
    memory only, with the `identity.CONTEXT_FIELDS` that differ; `source_changes` counts a field
    only when both carry it, so a stale mark an earlier sync saved into the base says nothing (and
    is cleared here). An old base needs nothing it lacks.

    What recreating the unit cannot write is not marked but returned, [{"slide", "element",
    "fields"}]: "placeholder" (the element goes into another layout placeholder, or out of one,
    because the slide changes layout; the subtitle role moving between texts of a title page that
    stays one is written, `context_changes`) and "grouping"
    (the block or rule groups it belongs to, `emit.block_groups` / `rule_groups`: a recreated unit
    goes back into its old group, `Sync.regroups`). `fast`: skip slides on which nothing changed
    (the tests turn it off to prove two emissions of the same slide compare equal).

    A slide whose emission cannot be worked out on one side is left unmarked and put in `unread`
    (`Unread`; `unread_warnings` says it): a base an older converter wrote is a real deck's
    everyday case, and the marks are all it costs."""
    from .emit import strict

    unwritten: list[Unwritten] = []
    base_slides, deck_slides = _slides(base, "base"), _slides(deck, "deck")
    for j, i in pairs.items():
        b, o, slide = base_slides[i], entries[j], deck_slides[j]
        okey = str(o["key"])
        b_elements = as_objects(b["elements"], f"base slide {okey}: elements")
        for e in b_elements:
            fields = as_object(e.get("fields", {}), "element fields")
            if any(f in fields for f in identity.CONTEXT_FIELDS):
                kept: JsonObject = {k: v for k, v in fields.items() if k not in identity.CONTEXT_FIELDS}
                e["fields"] = kept
        # (an element the deck keeps though the source dropped it is not what emit wrote the others beside)
        was = [e for e in b_elements if not e.get("removed")]
        if any("ir" not in e for e in was):
            continue
        base_by = {as_str(e["key"], "element key"): e for e in was}
        o_elements = as_objects(o["elements"], f"slide {okey}: elements")
        layout = b.get("layout")
        title_page = layout == "TITLE" if layout in ("TITLE", "TITLE_ONLY") else bool(slide.get("title_page"))
        if fast and title_page == bool(slide.get("title_page")) and len(base_by) == len(o_elements) and \
                all(oe["key"] in base_by and base_by[oe["key"]]["ir_hash"] == oe["ir_hash"] for oe in o_elements):
            continue  # (nothing on the slide changed, so nothing emit writes for it did)
        candidates = [k for k, oe in enumerate(o_elements)
                      if oe["key"] in base_by and identity.source_changes(base_by[oe["key"]], oe) <= {"position"}]
        if not candidates:
            continue  # (whatever else changed is rewritten anyway, where the new conversion puts it)
        was_irs: list[Json] = [e["ir"] for e in was]
        try:
            before = dict(zip(base_by, emitted_elements({**slide, "title_page": title_page, "elements": was_irs},
                                                        list(base_by), scale, fonts)))
        except Exception as e:  # noqa: BLE001 - an IR an older converter wrote that today's emit cannot read
            # says nothing either way: no marks - but said, since nothing else would
            unread.append(Unread(slide=okey, side="base", error=f"{type(e).__name__}: {e}"))
            continue
        try:
            now = emitted_elements(slide, [str(oe["key"]) for oe in o_elements], scale, fonts)
        except Exception as e:  # noqa: BLE001 - the plan's own slide, which `planned` rehearsed
            # (a slide emit could not write would have had the element at fault made a picture, so
            # this is a difference between that rehearsal and `slide_emission`: a bug to see where
            # the suite runs, and in a person's sync the marks it costs, said, not the whole sync)
            if strict():
                raise
            unread.append(Unread(slide=okey, side="ours", error=f"{type(e).__name__}: {e}"))
            continue
        s_elements = as_objects(slide["elements"], f"slide {okey}: elements")
        for k in candidates:
            oe, el = o_elements[k], s_elements[k]
            ekey = str(oe["key"])
            be = base_by[ekey]
            box, was_box = _nums(el["bbox"], "bbox"), _nums(as_object(be["ir"], "ir")["bbox"], "ir.bbox")
            step = [(box[q] - was_box[q]) * scale for q in (0, 1)]
            marks, cannot = context_changes(before[ekey], now[k], step, as_str(el["kind"], "kind"),
                                            title_page == bool(slide.get("title_page")))
            for f in marks:
                be["fields"] = {**as_object(be["fields"], "element fields"), f: "base"}
                oe["fields"] = {**as_object(oe["fields"], "element fields"), f: "ours"}
            if cannot:
                unwritten.append(Unwritten(slide=okey, element=ekey, fields=tuple(sorted(cannot))))
    return unwritten


@dataclass(frozen=True, kw_only=True)
class Unwritten:
    """An element whose emission changed with what stands around it in a way recreating its unit
    cannot write (`mark_emitted`; `fields`: "placeholder", "grouping"). `unwritten_warnings` says it."""
    slide: str
    element: str
    fields: tuple[str, ...]


UNWRITTEN_SAYS = {
    "placeholder": "the new version puts {what} into another layout placeholder or out of one, because the "
                   "slide changes layout; a sync cannot change a live slide's layout, so {it} stayed where "
                   "{it_was}",
    "grouping": "the new version groups {what} differently with the shapes around {them} (a beamer block or "
                "rules {it} now lies on, or no longer does); a sync keeps the deck's groups, so {it} stayed "
                "grouped as before",
}


def unwritten_warnings(unwritten: list[Unwritten], ours: JsonObject) -> list[str]:
    """The report's words for `mark_emitted`'s changes a sync cannot write: one warning per slide and
    kind, naming the elements by their words (a person does not know "text/body/1"). Nothing is lost
    by them - the deck keeps what it has - but the deck no longer looks like a fresh conversion there,
    and nothing else says so."""
    slides = {as_str(s["key"], "slide key"): s for s in as_objects(ours.get("slides", []), "slides")}
    by: dict[tuple[str, str], list[str]] = {}
    for u in unwritten:
        slide = slides.get(u.slide)
        elements: list[JsonObject] = as_objects(slide.get("elements", []), f"slide {u.slide}: elements") if slide else []
        el = next((e for e in elements if e["key"] == u.element), None)
        ir = as_object(el["ir"], f"{u.element}: ir") if el and el.get("ir") else None
        words = " ".join(identity.plain_text(ir).split()) if ir and ir.get("kind") == "text" else ""
        name = f"\"{words[:40]}{'...' if len(words) > 40 else ''}\"" if words else \
            f"the {el['kind'] if el else 'element'} {u.element}"
        for f in u.fields:
            by.setdefault((u.slide, f), []).append(name)
    out = []
    for (key, field), names in by.items():
        title = (slides.get(key) or {}).get("title")
        one = len(names) == 1
        what = names[0] if one else ", ".join(names[:-1]) + " and " + names[-1]
        says = UNWRITTEN_SAYS.get(field, "the new version writes {what} differently in a way a sync cannot "
                                         "write ({field}); {it} stayed as {it_was}")
        out.append(f"slide {key}" + (f" ({title})" if title and title != key else "") + ": " +
                   says.format(what=what, field=field, it="it" if one else "they", them="it" if one else "them",
                               it_was="it was" if one else "they were"))
    return out


def emitted_elements(slide: JsonObject, names: Sequence[str], scale: float, fonts: "FontMapper") -> list[JsonObject]:
    """Per element of `slide` (a DeckPlan slide), what emit writes for it there, with nothing in it
    that says which deck the slide stands in: object ids by the element's name (`names`, its key),
    the slide's as `@slide`, the template shapes' copies by their template key (`emit.slide_emission`
    also takes links and placeholder sizes out). Z-order requests are left out: a rewritten unit
    goes where the source's order puts it (`Sync.restack`), whatever emit asks for.
    {"requests", "box": a picture's predicted place (slide pt), "role": "title" / "subtitle" (the
    layout placeholder it goes into), "groups": the block and rule groups it is in, as [kind,
    members' names]}. A text's own group with its anchored pictures is left out: those are one unit,
    and a picture added or dropped is a change of the unit itself."""
    from .emit import slide_emission
    e = slide_emission(slide, scale, fonts)
    sid, ids = e["slide_id"], e["element_ids"]
    mapping = [(oid, f"@{name}") for oid, name in zip(ids, names)] + \
        [(oid, f"@template{key!r}") for oid, key in e["templates"].items()] + [(sid, "@slide")]
    order = sorted(mapping, key=lambda kv: -len(kv[0]))
    groups: list[list[list[Json]]] = [[] for _ in ids]
    at = {f"@{name}": i for i, name in enumerate(names)}
    for _, rs in e["parts"][1 + len(ids):]:
        for r in rs:
            group = as_object(r.get("groupObjects", {}), "groupObjects")
            gid = as_str(group.get("groupObjectId", ""), "groupObjectId")
            kind = "block" if gid.startswith(f"{sid}_blk") else "rules" if gid.startswith(f"{sid}_rules") else None
            if kind:
                children = _strs(rename(group["childrenObjectIds"], order), "childrenObjectIds")
                members = sorted(c[:-2] if c.endswith("_g") else c for c in children)
                for c in members:
                    if c in at:
                        groups[at[c]].append([kind, _json_strs(members)])
    out: list[JsonObject] = []
    for i, (_, rs) in enumerate(e["parts"][1:1 + len(ids)]):
        box = e["boxes"][i]
        role = "title" if i == e["title"] else "subtitle" if i == e["subtitle"] else None
        mine: list[Json] = [g for g in sorted(groups[i], key=_group_order)]
        out.append({"requests": [r for r in rename_requests(rs, order) if "updatePageElementsZOrder" not in r],
                    "box": None if box is None else _json_nums(box), "role": role, "groups": mine})
    return out


def _group_order(g: list[Json]) -> tuple[str, list[str]]:
    """A [kind, members] group entry's sort key, as the list sorted before (kind, then members)."""
    return as_str(g[0], "group kind"), _strs(g[1], "group members")


def context_changes(was: JsonMap, now: JsonMap, step: Sequence[float], kind: str,
                    same_layout: bool) -> tuple[set[str], set[str]]:
    """(fields recreating the unit writes, fields it cannot) in which two `emitted_elements` entries
    of one element differ; `step`: slide pt the source moved the element by, which its requests
    may differ by and still say the same.

    A text that took the title page's subtitle role or lost it (`emit.subtitle_element`: the biggest
    plain text below the title) is written by recreating it when the slide keeps its layout
    (`same_layout`): `Sync.update_slide` hands the live SUBTITLE placeholder to the element that has
    the role now and makes the other a box. Any other role change (the slide changing layout) is not."""
    marks: set[str] = set()
    cannot: set[str] = set()
    if was["role"] != now["role"]:
        # (and every request differs: a placeholder is transformed, a box created)
        if same_layout and {was["role"], now["role"]} <= {None, "subtitle"}:
            marks.add("emitted")
        else:
            cannot.add("placeholder")
    elif not _close(was["requests"], now["requests"], [v * EMU_PER_PT for v in step], None):
        marks.add("width" if kind == "text" else "emitted")
    was_box, now_box = was["box"], now["box"]
    if (was_box is None) != (now_box is None) or was_box is not None and \
            any(abs(a + step[q % 2] - c) > WIDTH_MOVED
                for q, (a, c) in enumerate(zip(_nums(was_box, "box"), _nums(now_box, "box")))):
        marks.add("placed")
    if was["groups"] != now["groups"]:
        cannot.add("grouping")
    return marks, cannot


def _close(a: Json, b: Json, step: Sequence[float], key: str | None) -> bool:
    """Two request trees say the same: equal but for float noise, with EMU lengths within
    WIDTH_MOVED and translations less `step` (EMU) - the source moving an element moves its box.
    `key`: the key `a` and `b` stand under (None: a list item, or the top)."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        magnitude = a.get("magnitude")
        if a.get("unit") == "EMU" and isinstance(magnitude, (int, float)):
            return abs(magnitude - _number(b["magnitude"], "magnitude")) <= WIDTH_MOVED * EMU_PER_PT and \
                all(_close(a[k], b[k], step, k) for k in a if k != "magnitude")
        return all(_close(a[k], b[k], step, k) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y, step, None) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        if key in ("translateX", "translateY"):
            return abs(a + step[key == "translateY"] - b) <= WIDTH_MOVED * EMU_PER_PT
        if key in ("scaleX", "scaleY", "shearX", "shearY"):  # (a placeholder's size is its scale)
            return abs(a - b) <= 1e-3 * max(abs(a), abs(b)) + 1e-9
        if isinstance(a, int) and isinstance(b, int):
            return a == b  # (text indices)
        return abs(a - b) <= 0.05 + 1e-3 * max(abs(a), abs(b))
    return a == b


# ---------------------------------------------------------------- requests

def rename(value: Json, mapping: Sequence[tuple[str, str]]) -> Json:
    """Object ids in requests: a string equal to a key, or a key followed by a non-digit
    suffix (`_g`, `n`, `_n0`), gets the new id. `mapping` is longest key first."""
    if isinstance(value, dict):
        return {_renamed(k, mapping): rename(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename(v, mapping) for v in value]
    if isinstance(value, str):
        return _renamed(value, mapping)
    return value


def _renamed(value: str, mapping: Sequence[tuple[str, str]]) -> str:
    if value.startswith("b2s_"):
        for old, new in mapping:
            if value == old or (value.startswith(old) and not value[len(old)].isdigit()):
                return new + value[len(old):]
    return value


def rename_requests(reqs: Sequence[JsonObject], mapping: Sequence[tuple[str, str]]) -> list[JsonObject]:
    """`rename` over a list of requests."""
    return [as_object(rename(r, mapping), "request") for r in reqs]


def letterbox_fix(oid: str, box: Sequence[float], px: tuple[int, int]) -> JsonObject:
    """createImage fits a picture into its box keeping the aspect ratio; this stretches it to the
    box, as the .pptx import does."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    s = min(w / px[0], h / px[1])
    fw, fh = px[0] * s, px[1] * s
    fx, fy = x0 + (w - fw) / 2, y0 + (h - fh) / 2
    sx, sy = w / fw, h / fh
    return google_types.slides_json({"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": {
        "scaleX": sx, "scaleY": sy, "unit": "EMU",
        "translateX": round((x0 - sx * fx) * EMU_PER_PT), "translateY": round((y0 - sy * fy) * EMU_PER_PT)}}})


def png_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as img:
        return img.size


def api_colour(hex_or_theme: str | None) -> JsonObject | None:
    if not hex_or_theme:
        return None
    if hex_or_theme.startswith("theme:"):
        return {"themeColor": hex_or_theme[6:]}
    from .emit import rgb
    return as_object(rgb(hex_or_theme)["opaqueColor"], "rgb")


def api_text_style(runs: JsonMap) -> tuple[JsonObject, list[str]]:
    """A normalised run style (snapshot._text_style attributes) as an API TextStyle and its fields."""
    style: JsonObject = {}
    fields: list[str] = []
    for k, v in runs.items():
        if k in ("fontFamily", "weight"):
            if "weight" in runs:
                style["weightedFontFamily"] = {"fontFamily": runs.get("fontFamily"), "weight": runs.get("weight") or 400}
                fields.append("weightedFontFamily")
            else:
                style["fontFamily"] = v
                fields.append("fontFamily")
        elif k == "fontSize":
            style[k] = pt(_number(v, "fontSize"))
            fields.append(k)
        elif k in ("foregroundColor", "backgroundColor"):
            style[k] = {"opaqueColor": api_colour(as_str(v, k))} if v else {}
            fields.append(k)
        elif k == "link":
            continue
        else:
            style[k] = v
            fields.append(k)
    return style, list(dict.fromkeys(fields))


def style_override_requests(oid: str, change: JsonMap, cells: Sequence[google_types.SlidesTableCellLocation] | None
                            ) -> list[JsonObject]:
    """Uniform text style changes the deck made (merge.uniform_changes), over all of the text
    (`cells`: the cellLocations of a table, whose text is styled cell by cell; None for a shape)."""
    reqs: list[JsonObject] = []
    runs, paras = _obj(change.get("runs"), "runs"), _obj(change.get("paragraphs"), "paragraphs")
    style, fields = api_text_style(runs)
    pstyle: JsonObject = {}
    pfields: list[str] = []
    for k, v in paras.items():
        if k in ("alignment", "lineSpacing", "direction"):
            pstyle[k] = v
            pfields.append(k)
        elif k in ("indentStart", "indentFirstLine", "spaceAbove", "spaceBelow"):
            pstyle[k] = pt(_number(v, k))
            pfields.append(k)
    every: list[google_types.SlidesTableCellLocation | None] = [None] if cells is None else [c for c in cells]
    if fields:
        reqs += [text_style_request(oid, c, {"type": "ALL"}, style, ",".join(fields)) for c in every]
    if pfields:
        reqs += [paragraph_style_request(oid, c, {"type": "ALL"}, pstyle, ",".join(pfields)) for c in every]
    return reqs


def text_style_request(oid: str, cell: google_types.SlidesTableCellLocation | None, text_range: google_types.SlidesRange,
                       style: JsonObject, fields: str) -> JsonObject:
    """An updateTextStyle, its keys in the order sync always wrote them."""
    request: google_types.SlidesUpdateTextStyleRequest = (
        {"objectId": oid, "textRange": text_range, "style": style, "fields": fields} if cell is None else
        {"objectId": oid, "cellLocation": cell, "textRange": text_range, "style": style, "fields": fields})
    return google_types.slides_json({"updateTextStyle": request})


def paragraph_style_request(oid: str, cell: google_types.SlidesTableCellLocation | None,
                            text_range: google_types.SlidesRange, style: JsonObject, fields: str) -> JsonObject:
    """An updateParagraphStyle, its keys in the order sync always wrote them."""
    request: google_types.SlidesUpdateParagraphStyleRequest = (
        {"objectId": oid, "textRange": text_range, "style": style, "fields": fields} if cell is None else
        {"objectId": oid, "cellLocation": cell, "textRange": text_range, "style": style, "fields": fields})
    return google_types.slides_json({"updateParagraphStyle": request})


def raw_objects(pres: Presentation) -> dict[str, PageElement]:
    """objectId -> page element of a presentations.get (group children included)."""
    return {object_id(e): e for s in pres.get("slides", [])
            for e in google_types.all_elements(s.get("pageElements", []), object_id(s))}


deck_attributes = merge.deck_attributes   # which attributes on a run are the person's (merge decides)


TextContainer = tuple[google_types.SlidesTableCellLocation | None, JsonObject | None, JsonObject | None]
"""(cellLocation, old text, new text) of one place holding text (`text_containers`)."""


def _text_of(v: Json, where: str) -> JsonObject | None:
    return None if v is None else as_object(v, where)


def text_containers(old: PageElement, new: PageElement) -> list[TextContainer]:
    """(cellLocation, old text, new text) of two page elements holding text: a shape, or the cells
    of two tables of the same shape."""
    old_shape, new_shape = old.get("shape"), new.get("shape")
    if old_shape is not None and new_shape is not None:
        return [(None, _text_of(old_shape.get("text"), "shape.text"), _text_of(new_shape.get("text"), "shape.text"))]
    old_table, new_table = old.get("table"), new.get("table")
    if old_table is not None and new_table is not None:
        orows = google_types.parts(old_table.get("tableRows"), "table.tableRows")
        nrows = google_types.parts(new_table.get("tableRows"), "table.tableRows")

        def cells(row: JsonObject) -> list[JsonObject]:
            return google_types.parts(row.get("tableCells"), "tableRows.tableCells")
        if len(orows) != len(nrows) or any(len(cells(a)) != len(cells(b)) for a, b in zip(orows, nrows)):
            return []
        return [({"rowIndex": r, "columnIndex": c}, _text_of(oc.get("text"), "cell.text"), _text_of(nc.get("text"), "cell.text"))
                for r, (orow, nrow) in enumerate(zip(orows, nrows))
                for c, (oc, nc) in enumerate(zip(cells(orow), cells(nrow)))]
    return []


def _utf16_offsets(text: str) -> list[int]:
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + (2 if ord(ch) > 0xFFFF else 1))
    return offsets


def style_range_requests(oid: str, old: PageElement, new: PageElement, base_styles: Sequence[JsonMap],
                         merged: str | None) -> list[JsonObject]:
    """The deck's run style edits of `old` (the live object before sync) re-applied to the same
    words in `new` (its recreation; `merged`: the text it will hold after the text override, None
    when there is no text override)."""
    from bisect import bisect_left
    from difflib import SequenceMatcher

    reqs: list[JsonObject] = []
    for loc, old_text, new_text in text_containers(old, new):
        before = snapshot.read_text(old_text)[0]
        after = merged if merged is not None and loc is None else snapshot.read_text(new_text)[0]
        b_off, a_off = _utf16_offsets(before), _utf16_offsets(after)
        blocks = SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
        for k, te in enumerate(_objs((old_text or {}).get("textElements"), "text.textElements")):
            where = f"text.textElements[{k}]"
            run = _obj(te.get("textRun"), f"{where}.textRun")
            content = _text(run.get("content"), f"{where}.textRun.content")
            if not run or not content.strip("\n"):
                continue
            style, fields = api_text_style(deck_attributes(
                snapshot._text_style(_obj(run.get("style"), f"{where}.textRun.style")), base_styles))
            if not fields:
                continue
            trailing = len(content) - len(content.rstrip("\n"))  # (paragraph ends keep their style)
            start = bisect_left(b_off, as_int(te.get("startIndex", 0), f"{where}.startIndex"))
            end = bisect_left(b_off, as_int(te.get("endIndex", 0), f"{where}.endIndex") - trailing)
            for i, j, n in blocks:
                lo, hi = max(start, i), min(end, i + n)
                if hi > lo:
                    reqs.append(text_style_request(oid, loc or None, {
                        "type": "FIXED_RANGE", "startIndex": a_off[j + lo - i], "endIndex": a_off[j + hi - i]},
                        style, ",".join(fields)))
    return reqs


def shape_style_requests(oid: str, style: JsonMap) -> list[JsonObject]:
    """A shape's fill and outline as the deck has them (snapshot.shape_style)."""
    props: JsonObject = {}
    fields: list[str] = []
    fill = _obj(style.get("fill"), "shape_style.fill")
    if "color" in fill:
        props["shapeBackgroundFill"] = {"solidFill": {"color": api_colour(as_optional_str(fill["color"], "fill.color")),
                                                      "alpha": fill.get("alpha", 1.0)}}
        fields += ["shapeBackgroundFill.solidFill.color", "shapeBackgroundFill.solidFill.alpha"]
    elif fill.get("state") == "NOT_RENDERED":
        props["shapeBackgroundFill"] = {"propertyState": "NOT_RENDERED"}
        fields.append("shapeBackgroundFill.propertyState")
    outline = _obj(style.get("outline"), "shape_style.outline")
    outline_fill = _obj(outline.get("fill"), "outline.fill")
    if outline.get("state") == "NOT_RENDERED":
        props["outline"] = {"propertyState": "NOT_RENDERED"}
        fields.append("outline.propertyState")
    elif outline_fill and "color" in outline_fill:
        line: JsonObject = {"outlineFill": {"solidFill": {"color": api_colour(as_optional_str(outline_fill["color"], "outline.fill.color")),
                                                          "alpha": outline_fill.get("alpha", 1.0)}},
                            "weight": pt(_number(outline.get("weight") or 1.0, "outline.weight"))}
        fields += ["outline.outlineFill.solidFill.color", "outline.outlineFill.solidFill.alpha", "outline.weight"]
        if outline.get("dash"):
            line["dashStyle"] = outline["dash"]
            fields.append("outline.dashStyle")
        props["outline"] = line
    if not fields:
        return []
    return [google_types.slides_json({"updateShapeProperties": {"objectId": oid, "shapeProperties": props,
                                                                "fields": ",".join(fields)}})]


def box_overlap(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    """Area shared by two boxes, over the larger one's area (0 when either is empty)."""
    if not a or not b:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    areas = [(x[2] - x[0]) * (x[3] - x[1]) for x in (a, b)]
    return ix / max(areas) if max(areas) > 0 else 0.0


HIDDEN = 0.2  # of a text's box: an opaque shape covering more of it hides it (loss_oracle.HIDDEN)


def subtree(objects: JsonMap, oid: str) -> list[str]:
    """`oid` and everything it carries, a group's children included."""
    out: list[str] = []

    def walk(x: str) -> None:
        if x in objects and x not in out:
            out.append(x)
            for c in _strs(as_object(objects[x], x).get("children"), f"{x}.children"):
                walk(c)
    walk(oid)
    return out


def _opaque_fill(rb: JsonMap, where: str) -> bool:
    """Is this read-back's shape fill a colour at full alpha?"""
    fill = _obj(_obj(rb.get("shape_style"), f"{where}.shape_style").get("fill"), f"{where}.shape_style.fill")
    return fill.get("color") is not None and _number(fill.get("alpha") or 0, f"{where}.fill.alpha") >= 0.999


def _covered(t: Sequence[float], s: Sequence[float]) -> bool:
    """Does box `s` cover more than HIDDEN of box `t`?"""
    area = (t[2] - t[0]) * (t[3] - t[1])
    w = min(t[2], s[2]) - max(t[0], s[0])
    h = min(t[3], s[3]) - max(t[1], s[1])
    return area > 0 and w > 0 and h > 0 and w * h / area > HIDDEN


def would_hide(objects: JsonMap, top: str, under: str, spoken_for: Collection[str] | None) -> bool:
    """Would drawing `top` above `under` put an opaque shape over words somebody can read? The
    question `loss_oracle.occlusion_findings` asks of a finished sync, asked before the write.

    `spoken_for`: objects whose words the source draws itself, and about whose stacking it therefore
    has an opinion (`Sync.restack`'s last pass asks only about words **only the deck** has). It is
    asked of each text rather than of `under` as a whole, because one page element may hold both - a
    group the person made around one of their own text boxes and one of the converter's. Asking
    whether that element is the source's protected the group and let a created panel cover the
    person's box inside it (converted seed 5200496 at chain 12)."""
    shapes: list[list[float]] = []
    for x in subtree(objects, top):
        rb = as_object(objects[x], x)
        if rb.get("kind") == "shape" and rb.get("box") and _opaque_fill(rb, x):
            shapes.append(_nums(rb["box"], f"{x}.box"))
    texts: list[list[float]] = []
    for x in subtree(objects, under):
        rb = as_object(objects[x], x)
        if x not in (spoken_for or ()) and rb.get("box") and _text(rb.get("text"), f"{x}.text").strip():
            texts.append(_nums(rb["box"], f"{x}.box"))
    return any(_covered(t, s) for t in texts for s in shapes)


def drawn_order(objects: JsonMap, order: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Object ids bottom to top, and for each the page element it is drawn inside (itself, when it is
    one): the page's elements in `order`, a group's children where the group stands."""
    out: list[str] = []
    top: dict[str, str] = {}

    def walk(oid: str, root: str) -> None:
        if oid not in objects or oid in top:
            return
        out.append(oid)
        top[oid] = root
        for c in _strs(as_object(objects[oid], oid).get("children"), f"{oid}.children"):
            walk(c, root)
    for t in order:
        walk(t, t)
    return out, top


def folded_hiders(read: JsonMap, made: Collection[str], ours: Collection[str],
                  doomed: Collection[str]) -> list[tuple[str, str]]:
    """(text, shape) pairs this sync could not order its way out of, for the report to name.

    Z-order is written in two places: the page's element order (`Sync.restack`) and the children of a
    group this sync rebuilds (`regroup_requests`). Neither reaches a converter element the person has
    folded into a group of *their own*: its page element is that group, so restacking it moves
    everything else they put in there, and the children of a group nobody rebuilds cannot be
    reordered at all. A panel that grew when the source redrew it can then cover a text in another
    page element with no way round it, and the person is told rather than left to find the words gone
    (`loss_oracle.text_hidden`, converted seed 670146 at chain 6: 2 of 2,600 rounds).

    `ours` is what the converter's own containers are (the base's groups and its elements' objects),
    `made` what this sync created, whose containers are its own too. `doomed`: what the cleanup is
    about to delete (`w["doomed"]`) - the order is read before it, and the words of a block the
    source redrew, still under the new block, are going (live scenario nested-group)."""
    objects = _obj(read.get("objects"), "read.objects")
    order, top = drawn_order(objects, _strs(read.get("order"), "read.order"))
    out: list[tuple[str, str]] = []
    for i, oid in enumerate(order):
        rb, stands_in = as_object(objects[oid], oid), top[oid]
        if (oid not in made or stands_in == oid or stands_in in ours or stands_in in made
                or rb.get("kind") != "shape" or not rb.get("box") or not _opaque_fill(rb, oid)):
            continue
        shape_box = _nums(rb["box"], f"{oid}.box")
        for under in order[:i]:
            u = as_object(objects[under], under)
            if (top[under] == stands_in or under in doomed or not u.get("box")
                    or not _text(u.get("text"), f"{under}.text").strip()):
                continue
            if _covered(_nums(u["box"], f"{under}.box"), shape_box):
                out.append((under, oid))
    return out


BREAK: JsonObject = {"__b2s_break__": True}  # where a batch may be cut: between slides
PENDING_URL = "b2s-pending:"     # a picture whose staging URL is still on its way (`Sync.fill_urls`)


def batches(reqs: Sequence[JsonObject], size: int) -> list[list[JsonObject]]:
    """The requests split into batches of at most `size` (`CHUNK` in a sync), cut only where
    `main_requests` allows it (between slides), so a sync that dies between two batches leaves whole
    slides behind. One slide with more than `size` requests is the only thing that is ever split."""
    size = faults.batch_size(size)
    blocks: list[list[JsonObject]] = []
    current: list[JsonObject] = []
    for r in reqs:
        if r == BREAK:
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(r)
    if current:
        blocks.append(current)
    out: list[list[JsonObject]] = []
    for block in blocks:
        if len(block) > size:
            out += [block[i:i + size] for i in range(0, len(block), size)]
        elif out and len(out[-1]) + len(block) <= size:
            out[-1] += block
        else:
            out.append(list(block))
    return out


def matrix_request(oid: str, m: Sequence[float]) -> JsonObject:
    return google_types.slides_json({"updatePageElementTransform": {
        "objectId": oid, "applyMode": "RELATIVE", "transform": {
            "scaleX": m[0], "shearX": m[1], "shearY": m[2], "scaleY": m[3], "unit": "EMU",
            "translateX": round(m[4] * EMU_PER_PT), "translateY": round(m[5] * EMU_PER_PT)}}})


def _emu(v: float) -> google_types.Dimension:
    return {"magnitude": round(v * EMU_PER_PT), "unit": "EMU"}


def emu_size(w: float, h: float) -> google_types.Size:
    """A size in slide pt as the API takes it (EMU)."""
    return {"width": _emu(w), "height": _emu(h)}


def emu_size_json(w: float, h: float) -> JsonObject:
    """`emu_size` as the JSON of a read-back emit plans against."""
    return {"width": {"magnitude": round(w * EMU_PER_PT), "unit": "EMU"},
            "height": {"magnitude": round(h * EMU_PER_PT), "unit": "EMU"}}


def delete_text_request(oid: str, cell: google_types.SlidesTableCellLocation | None) -> JsonObject:
    """deleteText of all an object's words (`cell`: of that table cell)."""
    if cell is None:
        return google_types.slides_json({"deleteText": {"objectId": oid, "textRange": {"type": "ALL"}}})
    return google_types.slides_json({"deleteText": {"objectId": oid, "cellLocation": cell, "textRange": {"type": "ALL"}}})


def named_step(step: JsonMap, oid: str) -> JsonObject:
    """A `table_steps` request for the table `oid`."""
    return {k: {**as_object(v, "a table step"), "tableObjectId": oid} for k, v in step.items()}


def front_request(oid: str) -> JsonObject:
    return google_types.slides_json({"updatePageElementsZOrder": {"pageElementObjectIds": [oid],
                                                                  "operation": "BRING_TO_FRONT"}})


def ungroup_request(gid: str) -> JsonObject:
    return google_types.slides_json({"ungroupObjects": {"objectIds": [gid]}})


def page_properties_request(sid: str, fields: str, properties: JsonObject) -> JsonObject:
    return google_types.slides_json({"updatePageProperties": {"objectId": sid, "fields": fields,
                                                              "pageProperties": properties}})


@dataclass(frozen=True, kw_only=True)
class Template:
    """What a template shape (a shadow, exact corners: `emit.template_key`) is duplicated from on a
    slide sync writes (`Sync.slide_requests`): a live object of that key, or a plain stand-in
    (`stand_in_request`, `text` ""); `w`, `h` its size (pt), `text` the words a copy brings along
    and that are then cleared."""
    id: str
    w: float
    h: float
    text: str


def carried(base_rb: JsonMap, theirs_rb: JsonMap, new_rb: JsonMap) -> list[float]:
    """The RELATIVE transform that puts the person's edit of a unit (base -> theirs: a move, a
    resize) onto its recreation `new_rb`, carried along by the source's move of it (base -> new).

    `theirs * base^-1` alone is a page-space step: right for a move, but a resize in it scales about
    the page origin, so it would scale the source's move too. Taken about the unit's corner instead -
    conjugated by the source's step `s` of that corner, `T(s) * d * T(-s)` - a move lands at the
    source's new place plus the person's offset and a resize keeps the person's proportions of the
    source's new box, from its new corner. When the source did not move the unit, `s` is 0 and this
    is `d` itself."""
    d = snapshot.compose(_nums(theirs_rb["transform"], "theirs.transform"),
                         snapshot.invert(_nums(base_rb["transform"], "base.transform")))
    new_box, base_box = _nums(new_rb["box"], "new.box"), _nums(base_rb["box"], "base.box")
    sx, sy = new_box[0] - base_box[0], new_box[1] - base_box[1]
    return d[:4] + [d[4] + sx - (d[0] * sx + d[1] * sy), d[5] + sy - (d[2] * sx + d[3] * sy)]


# ---------------------------------------------------------------- the sync

class Sync:
    way_back: "WayBack | None" = None   # the recovery note being made meanwhile; None where there is none to collect
    theme_side: "ThemeSide | None" = None   # theme_sync's part (set in __init__ / plan_theme)
    theme_plan: "ThemeMerge | None" = None
    raw_after: Presentation | None = None

    def __init__(self, slides: SlidesService, drive: DriveService, pid: str, base: JsonObject, ours: Built, out: Path,
                 dry_run: bool, measure: bool, trust_generation: bool,
                 check_plan: "Callable[[merge.MergePlan, JsonObject], None] | None",
                 follow_labels: bool, take_source: Sequence[str], facts: DriveFile | None,
                 way_back: "WayBack | None") -> None:
        # check_plan(mplan, theirs): raises instead of letting the write go ahead. It sits between
        # planning and preparing because that is the last point at which nothing has been sent and
        # the whole of what would be written is known (adopt_sync.problems).
        self.check_plan = check_plan
        self.slides, self.drive, self.pid = slides, drive, pid
        self.base, self.out = base, out
        self.built = ours
        self.ours: JsonObject = ours_json(ours)   # what the merge reads, and the offline fuzz's replay
        self.source, self.ours_out = ours.source, ours.out   # the PDF synced, the folder it was rendered into
        self.dry_run, self.measure = dry_run, measure
        self.trust_generation = trust_generation  # may this base's generation decide what is a leftover?
        self.follow_labels = follow_labels        # write to a slide whose label may have moved (merge.hold_slide)
        self.take_source = tuple(take_source)     # conflicts to settle for the source (merge.Resolutions)
        self.plan: DeckPlan = ours.plan
        self.scale = self.plan.scale
        self.tok = self.token()
        self.sent: dict[str, int] = {}
        self.warnings: list[str] = []
        self.overruns: list[Json] = []        # warn_about_overruns, for the report
        self.refit_moves: list[Json] = []     # what `refit` moved or grew, for the report
        self.urls: dict[str, str] = {}  # picture file (str) -> contentUrl from the staging deck
        self.recovery = no_recovery()   # what an interrupted earlier sync left (plan_recovery)
        self.cleanup_ids: list[str] = []      # old objects and slides, deleted after everything else
        self.cleanup_requests: list[JsonObject] = []
        self.in_place_readback: JsonObject = {}  # objects rewritten in place, before the write
        self.final_revision: str | None = None
        self.facts = facts              # the deck's Drive facts, read once (snapshot.deck_info)
        self.first_read: Presentation | None = None  # a `presentations.get` a caller made while we planned
        self.deleting: list[Future[None]] = []   # staging decks on their way out (drop_staging)
        self.way_back = way_back        # made meanwhile, collected before the first write (guard.WayBack)
        self.theme_side = None   # what a fresh conversion writes on the master and layouts (theme_sync)
        self.theme_plan = None   # what this sync writes there (None: nothing, an old base)
        self.theme_applied: list[JsonObject] = []   # theme_sync's part of the report (plan_theme)
        self.theme_conflicts: list[JsonObject] = []
        self.raw_after = None    # the deck as `finish` last read it
        self.created: JsonObject = {}   # the deck as read after the write (`finish`), or as planned against
        self.staging: str | None = None       # the staging deck of this attempt, until it is dropped
        self.picture_reads: dict[str, int] = {}   # live pictures in question, and read (merge_plan)
        self.live_read: tuple[tuple[object, ...], LivePictures] | None = None   # (live_pictures)
        self.reshaped: dict[str, refit.Reshape] = {}   # what `refit` moved or grew, for the base

    def before_write(self) -> None:
        """Collect the way back (`guard.WayBack`): the deck's revision and the .pptx backup are
        made on a thread while this sync reads and plans, and the promise they carry is that they
        are finished before anything in the deck moves. So every place that writes asks here
        first - the leftovers of an interrupted run, `measure_places`' scratch slides and the
        batches themselves - and the answer is made once."""
        if self.way_back is not None:
            self.way_back.result()

    def token(self) -> str:
        """The two letters that make this sync's object ids unique. Never the token of a sync that
        was interrupted (its objects may still be in the deck, and an id can only exist once)."""
        used = _obj(self.base.get("pending"), "base.pending").get("token")
        generation = base_generation(self.base) + 1
        tok = ""
        for _ in range(20):
            tok = f"{generation}{''.join(random.choices(string.ascii_lowercase, k=2))}"
            if tok != used:
                return tok
        return tok

    # ---- reading and writing

    def read(self) -> Presentation:
        """The deck, whole. The first one may have been fetched while the base was loaded and the
        PDF converted (`sync`); it is used once and never again, so every later read - after a
        write, or on a second attempt - is a fresh one."""
        if self.first_read is not None:
            pres, self.first_read = self.first_read, None
            return pres
        return execute(self.slides.presentations().get(presentationId=self.pid))

    def revision(self, slides: SlidesService | None) -> str:
        """`slides`: the client to ask with, where this runs on a thread of its own (`in_background`);
        None: this sync's own."""
        answer = execute((slides or self.slides).presentations().get(presentationId=self.pid, fields="revisionId"))
        rev = answer.get("revisionId")
        if rev is None:
            raise KeyError("revisionId")   # (as a read of the key did: `asked_revision` asks again)
        return rev

    def asked_revision(self, asking: "Future[str] | None") -> str:
        """The revision a worker went to fetch, or one fetched here: a question that failed on the
        thread is asked again rather than carried into the write, where an empty
        `requiredRevisionId` would mean "whatever the deck says now"."""
        if asking is not None:
            try:
                return asking.result()
            except (HttpError, OSError, KeyError):
                pass
        return self.revision(None)

    def send(self, phase: str, reqs: list[JsonObject], rev: str | None) -> str:
        """Batches with requiredRevisionId, chained through the revisions they return. A batch is
        cut at a slide boundary where it can (`batches`), so a run that dies between two batches
        leaves whole slides written, never half of one."""
        self.before_write()
        self.fill_urls(reqs)   # nothing goes out carrying a marker, whoever built it (`picture_url`)
        for n, chunk in enumerate(batches(reqs, CHUNK)):
            control = self.send_batch(phase, chunk, rev, n == 0).get("writeControl")
            rev = (control.get("requiredRevisionId") if control is not None else None) or self.revision(None)
            self.sent[phase] = self.sent.get(phase, 0) + len(chunk)
            faults.fail_at(phase)
        return rev or self.revision(None)

    def send_batch(self, phase: str, chunk: list[JsonObject], rev: str | None, first: bool
                   ) -> google_types.BatchUpdateResponse:
        """One batch of `send`, sent again while Google could not fetch a staged picture."""
        attempt = 0
        while True:
            try:
                body: google_types.BatchUpdateBody = {"requests": chunk}
                if rev:
                    body["writeControl"] = {"requiredRevisionId": rev}
                return execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body=body))
            except HttpError as e:
                from .emit import api_error
                message = api_error(e)
                if status_of(e) == 400 and "revision" in message.lower() and first:
                    raise RevisionMismatch(message) from e
                if status_of(e) == 400 and PICTURE_FETCH in message and attempt < len(FETCH_RETRY):
                    # Google could not fetch a staged picture this time (live fuzz seed 2603: the
                    # same sync run again went through). A refused batch applied nothing - a
                    # batchUpdate is all or nothing - so the same batch is sent again.
                    time.sleep(FETCH_RETRY[attempt])
                    attempt += 1
                    continue
                raise RuntimeError(f"sync {phase}: batch refused ({message})") from e

    # ---- planning

    def run(self) -> RunResult:
        from .emit import slide_layout  # noqa: F401 (warm import before timing-sensitive steps)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            pres: Presentation = self.read()
            leftovers = [object_id(s) for s in pres.get("slides", []) if SCRATCH.fullmatch(object_id(s))]
            if leftovers and not self.dry_run:  # measure_places' scratch slides of an interrupted run
                self.delete_scratch(leftovers)
                pres = self.read()
            pres = without_scratch(pres)
            if attempt == 1 and not snapshot.base_matches(self.base, snapshot.read_presentation(pres)):
                raise BaseMismatch(
                    f"the sync base (generation {base_generation(self.base)}) describes none of the slides in "
                    f"presentation {self.pid}: it belongs to another copy of this deck, or the deck was rebuilt "
                    f"outside sync. Syncing would report every element as deleted. Convert the PDF again "
                    f"(python -m beamer2slides convert) to start a new base, or point --deck at the right deck.")
            pres = self.recover(pres, attempt)
            theirs = snapshot.read_presentation(pres)
            if self.recovery.restore:
                restore_in_place(theirs, self.recovery.restore)
            mplan = self.merge_plan(theirs, pres)
            if self.check_plan is not None:
                self.check_plan(mplan, theirs)  # (adopt_sync: an adopted deck this may not be written to)
            self.plan_theme(mplan, pres)
            work = self.prepare(mplan, pres, theirs)
            revision_read = as_optional_str(theirs["revisionId"], "theirs.revisionId")
            if self.dry_run or not work.writes:
                self.created, self.final_revision = theirs, revision_read  # (a base that only adopts deck fields)
                return RunResult(attempts=attempt, plan=mplan, work=work, theirs=theirs, revision_id=None)
            hook = os.environ.pop("B2S_SYNC_BEFORE_WRITE", None)  # (tests: someone edits the deck now)
            if hook:
                subprocess.run(hook, shell=True, check=False)
            if self.revision(None) != revision_read:
                continue  # edited while we planned: plan again
            faults.fail_at("plan")
            # The staging deck and the scratch slides are two things Google can do at once: the
            # staging deck is a file of its own, the scratch slides are pages nothing else here
            # looks at, and neither reads what the other writes. The worker builds clients of its
            # own, since a service object is one connection - unless a caller lent us one, which
            # is that caller's and is used on this thread alone (`emit.measure_places`' rule).
            staging: str | None = None                # noted in the pending marker: a run that
            scratch: list[str] = []                   # dies leaves it for the next one to delete
            staged = self.stage_in_background(work)
            asking: Future[str] | None = None
            pending: Future[None] | None = None
            try:
                if staged is None:
                    self.staging = staging = self.stage(work, None, None)
                moves, scratch = self.measure_places(work, theirs)
                # The revision the write will require, asked for while the staging deck is still
                # being made: the scratch slides were the last thing to move the deck, so from here
                # it is the same answer whenever it is asked - and if somebody types meanwhile, the
                # write is refused and planned again, which is what an older revision is for.
                asking = self.in_background(lambda slides, drive: self.revision(slides), "b2s-rev")
                faults.fail_at("measure")
                # The requests are built while the staging deck is still being imported: the only
                # thing in them that waits for it is a picture's URL (`picture_url`), and every
                # object id is the element's own - so what the marker has to record is known now.
                content, cleanup = self.main_requests(work, theirs, pres, moves, scratch)
                self.cleanup_requests = cleanup
                # What this sync is about to create, recorded before the first write: a run that
                # dies leaves its objects behind, and the next one knows they are its own. Its
                # upload rides beside the staging deck; both are collected before anything is sent.
                pending = self.pending_in_background(work, theirs)
                if staged is not None:
                    self.staging = staging = staged.result()
                self.fill_urls(content)
                if pending is not None:
                    pending.result()      # durable before the first write, which is the whole of it
                else:
                    self.store_pending(self.drive)
                faults.fail_at("journal")
                rev = self.send("content", content, self.asked_revision(asking))
                asking = None
                scratch = []
            except RevisionMismatch:
                continue
            finally:
                if staging is None and staged is not None:
                    # Whatever went wrong here, a staging deck the worker did make is ours to delete.
                    with contextlib.suppress(Exception):
                        self.staging = staging = staged.result()
                if asking is not None:
                    asking.cancel()
                if pending is not None:
                    # However this ended, the marker is on its way and nobody else will collect it.
                    with contextlib.suppress(Exception):
                        pending.result()
                if scratch:
                    self.delete_scratch(scratch)
                if staging:  # (its pictures are only needed until the live deck has them)
                    self.drop_staging(staging)
            rev = self.finish(work, mplan, theirs, pres, rev)
            return RunResult(attempts=attempt, plan=mplan, work=work, theirs=theirs, revision_id=rev)
        raise RuntimeError(f"the deck kept changing while syncing ({MAX_ATTEMPTS} attempts)")

    def recover(self, pres: Presentation, attempt: int) -> Presentation:
        """Undo what an earlier sync that died halfway left behind, before anything is planned
        (plan_recovery): its leftover objects are deleted, an element whose objects it deleted takes
        over the ones it created, and a placeholder it rewrote gets the person's text back for the
        merge to re-apply. The deletions are the first thing this sync writes, and they only ever
        remove an object the deck holds a second time."""
        read = snapshot.read_presentation(pres)
        rec = plan_recovery(self.base, read, [as_str(s["key"], "ours slide key") for s in _slides(self.ours, "ours")],
                            self.trust_generation)
        self.recovery = rec
        # A run that died left its staging deck in Drive. Its id is in `pending.staging` so a person
        # can find it; sync doesn't delete it, because an id read from a file could name anything -
        # only the file this process just created is ever deleted (tests/test_guard.py).
        if rec.heal:
            pending_source = _obj(_obj(self.base.get("pending"), "base.pending").get("source"), "base.pending.source")
            same = pending_source.get("sha1") == snapshot.source_info(self.source).get("sha1")
            healed = heal_base(self.base, rec.heal, read, same)
            if healed and attempt == 1:
                self.warnings.append(f"an earlier sync was interrupted after it had replaced {len(healed)} element(s) "
                                     f"({', '.join(healed[:3])}{'...' if len(healed) > 3 else ''}): the deck's objects "
                                     f"were taken over" + ("" if same else " and are written over from the source"))
        if rec.sweep or rec.sweep_slides:
            if self.dry_run:
                self.warnings.append(f"an earlier sync left {len(rec.sweep)} object(s) and "
                                     f"{len(rec.sweep_slides)} slide(s) behind; a real sync would delete them")
            else:
                gone = self.delete_leftovers(rec.sweep + rec.sweep_slides)
                if gone and attempt == 1:
                    self.warnings.append(f"deleted {len(gone)} leftover object(s)/slide(s) of an interrupted sync")
                # Only what is really gone may be planned away: anything still in the deck has to
                # stay in the picture, or the merge would create it a second time.
                rec = replace(rec, sweep=[oid for oid in rec.sweep if oid in gone],
                              sweep_slides=[sid for sid in rec.sweep_slides if sid in gone])
                self.recovery = rec
            pres = drop_objects(pres, rec.sweep, rec.sweep_slides)
        return pres

    def delete_leftovers(self, ids: list[str]) -> set[str]:
        """Delete what an interrupted sync left behind and say what is really gone. One batch; if
        Google refuses it, one request at a time, so a single id it no longer knows (deleting a
        group takes its children with it) doesn't save every other leftover from being swept."""
        if not ids:
            return set()
        self.before_write()
        reqs = [delete_request(oid) for oid in ids]
        try:
            execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body={"requests": reqs}))
            return set(ids)
        except HttpError as first:
            gone: set[str] = set()
            for oid in ids:
                try:
                    execute(self.slides.presentations().batchUpdate(
                        presentationId=self.pid, body={"requests": [delete_request(oid)]}))
                except HttpError as e:
                    if "could not be found" not in str(e):
                        continue  # still in the deck: the merge has to keep seeing it
                gone.add(oid)
            if len(gone) < len(ids):
                self.warnings.append(f"could not delete {len(ids) - len(gone)} leftover object(s) of an "
                                     f"interrupted sync: {first}")
            return gone

    def pending_in_background(self, work: Work, theirs: JsonObject) -> "Future[None] | None":
        """`mark_pending` on a thread while the staging deck is still being imported, or None where
        it has to be run here (`in_background`).

        The marker is the one thing that has to be **durable before the first write**, and that is
        all it has to be: what it records - the objects this run is about to create - comes from the
        requests, which no longer wait for the staging deck (`picture_url`). So it goes up beside it
        and `write` collects it before `send`. Worth the whole of a media upload, which is ~1.9 s
        whatever it carries (docs/sync.md): interleaved A/B on a 13-frame talk, **17.3 -> 14.2 s**
        over five pairs, every one of six favouring it (the sixth is a round where the old code
        stalled at 57 s, dropped rather than counted).

        The block is built here, on this thread, so the worker only serialises and uploads a base
        nothing else touches until it is joined."""
        self.pending_block(work, theirs)
        return self.in_background(lambda slides, drive: self.store_pending(drive), "b2s-pend")

    def mark_pending(self, work: Work, theirs: JsonObject) -> None:
        """The marker built and stored here and now (`pending_in_background` is the other order)."""
        self.pending_block(work, theirs)
        self.store_pending(self.drive)

    def pending_block(self, work: Work, theirs: JsonObject) -> None:
        """The base's `pending` block before the first write: the generation and token of
        this run, the objects it is about to create and the read-back of the objects it rewrites in
        place. The base itself is unchanged, so a run that dies leaves a valid base of the old
        generation plus a note of what it started. Only a run that gets to the end removes it."""
        objects: JsonObject = {}
        slides: list[str] = []
        ours_slides = _slides(self.ours, "ours")
        for w in work.slides:
            p = w.plan
            match p:
                case merge.CreateSlide():
                    if w.sid:
                        slides.append(w.sid)
                case merge.UpdateSlide() | merge.HoldSlide():
                    pass
                case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide():
                    continue  # (nothing is made on it)
                case _:
                    assert_never(p)
            o = ours_slides[p.ours]
            okey = as_str(o["key"], "ours slide key")
            elements = as_objects(o["elements"], f"ours slide {okey}: elements")
            for i, oids in w.objects.items():
                objects[f"{okey}/{as_str(elements[i]['key'], 'element key')}"] = _json_strs(oids)
            if w.groups:
                objects[f"{okey}/~groups"] = _json_strs(w.groups)
        pending: JsonObject = {
            "generation": base_generation(self.base) + 1, "token": self.tok,
            "revisionId": theirs.get("revisionId"), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": snapshot.source_info(self.source), "objects": objects, "slides": _json_strs(slides),
            "in_place": self.in_place_readback, "staging": self.staging}
        if self.theme_plan is not None and self.theme_plan.pending:
            # the layout placeholder styles about to be written: a run that dies after them finds
            # its own writes there next time, not a person's (theme_sync.plan)
            pending["theme"] = self.theme_plan.pending
        self.base["pending"] = pending

    def store_pending(self, drive: DriveService | None) -> None:
        """The marker to disk and to Drive. `staging` is whatever this run knows of its staging deck
        when the marker goes up, which where the two are made at once is nothing: it is a note for a
        person and nothing reads it, the staging deck naming itself in Drive
        (`appProperties.b2sStaging`, which is what `tools/drive_usage.py` queries)."""
        why = snapshot.store_base(self.base, self.out, drive, "pending", self.facts)
        if why:
            self.warnings.append(f"could not note the started sync in Drive ({why}); noted locally only")

    def run_cleanup(self, rev: str | None) -> str | None:
        """The last phase: the objects and slides this sync replaced are deleted only now, once
        their replacements are written, the deck's own edits are back on them and a base that no
        longer mentions them is stored. A run that dies before this leaves the old objects in the
        deck, which is what lets the next sync do the whole merge again."""
        if not self.cleanup_requests:
            return rev
        try:
            return self.send("cleanup", self.cleanup_requests, rev)
        except RevisionMismatch:  # someone edited the deck meanwhile; the objects are stale either way
            return self.send("cleanup", self.cleanup_requests, None)

    def sign_changed(self, theirs: JsonObject, pres: Presentation) -> tuple[set[str], set[str]]:
        """The live pictures whose contentUrl differs from the base's, marked `unchecked` and not
        yet signed: (image ids, slide ids of background pictures).

        Google issues new URLs for unchanged pictures, so only the pixels tell a replaced one - but
        the answer only matters where the plan could write over the picture. A unit the source left
        alone is kept whatever the deck did to it, and so is its slide's background, so its picture
        is never read (a process that may not fetch a URL reads it out of a Drive export, and most
        syncs then need neither). `run` plans first and signs what the plan says is in question
        (`pictures_in_question`), then plans again. An unchecked picture still counts as replaced
        - the answer that never loses one - but is not reported as the person's edit
        (`merge.pictures_unchecked`)."""
        images: dict[str, JsonObject] = {}
        backgrounds: dict[str | None, JsonObject] = {}
        for s in _slides(self.base, "base"):
            for e in as_objects(s["elements"], "base slide elements"):
                for oid, rb in _obj(e.get("readback"), "element readback").items():
                    rbo = as_object(rb, f"read-back {oid}")
                    if "image" in rbo:
                        images[oid] = as_object(rbo["image"], f"read-back {oid}: image")
            backgrounds[as_optional_str(s.get("objectId"), "base slide objectId")] = \
                _obj(s.get("background_readback"), "base slide background_readback")
        objects: set[str] = set()
        slides: set[str] = set()
        for s in _slides(theirs, "theirs"):
            sid = as_str(s["objectId"], "theirs slide objectId")
            for oid, rb in _objects(s, f"theirs slide {sid}").items():
                rbo = as_object(rb, f"read-back {oid}")
                if "image" in rbo and oid in images:
                    image = as_object(rbo["image"], f"read-back {oid}: image")
                    if images[oid].get("contentHash") != image.get("contentHash"):
                        objects.add(oid)
                        image["unchecked"] = True
            bg, old = _obj(s.get("background"), f"theirs slide {sid}: background"), backgrounds.get(sid, {})
            if "picture" in bg and "picture" in old and old["picture"] != bg["picture"]:
                slides.add(sid)
                bg["unchecked"] = True
        return objects, slides

    def pictures_in_question(self, mplan: merge.MergePlan, objects: set[str],
                             slides: set[str]) -> tuple[set[str], set[str]]:
        """Of the unchecked pictures (`sign_changed`), those whose answer the plan depends on:
        every one on a slide the plan does anything but update or hold (a slide the source dropped
        is kept for the person's edits), and on an updated slide every one of a unit the source
        changed or dropped, and the background when the source changed it. What is left out is a
        picture of a unit kept with nothing of the source's to write - the same plan whether or not
        the person replaced it."""
        ask_objects: set[str] = set()
        ask_slides: set[str] = set()
        base_slides = _slides(self.base, "base")
        for p in mplan.slides:
            match p:
                case merge.CreateSlide() | merge.HoldSlide():
                    continue
                case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide():
                    b = base_slides[p.base]
                    ask_objects |= {oid for e in as_objects(b["elements"], "base slide elements")
                                    for oid in _strs(e.get("objects"), "element objects") if oid in objects}
                    bid = as_optional_str(b.get("objectId"), "base slide objectId")
                    if bid is not None and bid in slides:
                        ask_slides.add(bid)
                case merge.UpdateSlide():
                    b = base_slides[p.base]
                    bunits = merge.units(as_objects(b["elements"], "base slide elements"))
                    for u in p.units:
                        match u.decision:
                            case merge.KeptJoined():
                                continue
                            case merge.KeepUnit(source=source) if not source:
                                continue
                            case merge.AdoptObject() | merge.CreateUnit() | merge.GoneUnit() | merge.DeleteUnit() | \
                                    merge.KeepRemoved() | merge.KeepUnit() | merge.Recreate() | merge.AdoptUnit() | \
                                    merge.MoveUnit():
                                pass
                            case _:
                                assert_never(u.decision)
                        ask_objects |= {oid for m in bunits.get(u.key) or []
                                        for oid in _strs(m.get("objects"), "element objects") if oid in objects}
                    o = _slides(self.ours, "ours")[p.ours]
                    bid = as_optional_str(b.get("objectId"), "base slide objectId")
                    if bid is not None and bid in slides and b.get("background") != o.get("background"):
                        ask_slides.add(bid)
                case _:
                    assert_never(p)
        return ask_objects, ask_slides

    def merge_plan(self, theirs: JsonObject, pres: Presentation) -> merge.MergePlan:
        """The merge plan, with the pictures it depends on signed (`sign_changed`). (`self.plan`
        is the new conversion's DeckPlan.)"""
        objects, slides = self.sign_changed(theirs, pres)
        adopter = self.picture_adopter(pres)

        def plan() -> merge.MergePlan:  # (all read again each time: signing writes into theirs)
            return merge.plan_merge_of(parse_base(self.base), merge.ours_of(self.ours), deck_read(theirs), adopter,
                                       self.follow_labels, merge.Resolutions(self.take_source))
        mplan = plan()
        asked_objects: set[str] = set()
        asked_slides: set[str] = set()
        for _ in range(3):   # (signing can only settle a slide, never put a new one in question)
            ask_objects, ask_slides = self.pictures_in_question(mplan, objects, slides)
            ask_objects, ask_slides = ask_objects - asked_objects, ask_slides - asked_slides
            if not ask_objects and not ask_slides:
                break
            asked_objects |= ask_objects
            asked_slides |= ask_slides
            snapshot.sign_pictures(theirs, pres, ask_objects, ask_slides, PICTURE_WORKERS, None, None, None, None,
                                   self.live_pictures(pres))
            mplan = plan()
        self.picture_reads = {"unchecked": len(objects) + len(slides), "asked": len(asked_objects) + len(asked_slides)}
        return mplan

    def live_pictures(self, pres: Presentation) -> LivePictures:
        """The pictures of this read of the deck (`deck_pictures.LivePictures`): one per read, so a
        Drive export is made at most once for it."""
        from .google_auth import fetcher_for_threads
        key: tuple[object, ...] = (pres.get("presentationId"), pres.get("revisionId"), id(pres))
        if self.live_read is None or self.live_read[0] != key:
            self.live_read = (key, LivePictures(pres, self.drive, fetcher_for_threads(),
                                                PICTURE_WORKERS, None, self.slides))
        return self.live_read[1]

    def picture_adopter(self, pres: Presentation) -> merge.Adopter:
        """Finds the live object that already shows a picture the source now draws, for
        merge.plan_unit. After a pull the source has an `\\includegraphics` for a picture the
        person put into the deck (or put over a figure), so the new conversion offers a picture the
        deck already has: the same bytes (pull saves the deck's own picture), else the same picture
        (`inverse.same_look` for the same file at another resolution, and pull's own
        `compare.picture_differs` because the converter renders the region again rather than keeping
        the file: the deck's picture and that crop correlate 0.94, too little for same_look) and
        boxes overlapping over most of their area. Its object is kept with the person's crop,
        rotation and outline instead of being duplicated."""
        from .compare import TOL, picture_differs, picture_hash
        from .inverse import picture_look, same_look

        images, _ = snapshot.picture_urls(pres)
        pictures = self.live_pictures(pres)
        base_ids = {oid for s in _slides(self.base, "base") for e in as_objects(s["elements"], "base slide elements")
                    for oid in _strs(e.get("objects"), "element objects")}
        scale = self.scale or merge.deck_scale(self.base) or 1.0
        folder = self.ours_out / "deck-pictures"
        deck: dict[str, PictureFingerprints | None] = {}
        mine: dict[Path, PictureFingerprints | None] = {}
        taken: set[str] = set()

        def fingerprints(path: Path) -> "PictureFingerprints":
            return identity.sha1(path.read_bytes()), picture_look(path), picture_hash(path)

        def deck_picture(oid: str) -> "PictureFingerprints | None":
            """(sha1, look, thumbnail) of a live picture, downloaded once."""
            if oid not in deck:
                deck[oid] = None
                data = pictures.get([oid]).get(oid) if oid in images else None
                if data:
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{oid}.img"
                    path.write_bytes(data)
                    deck[oid] = fingerprints(path)
            return deck[oid]

        def our_picture(path: Path) -> "PictureFingerprints | None":
            if path not in mine:
                mine[path] = fingerprints(path) if path.exists() else None
            return mine[path]

        def same(ours: "PictureFingerprints", oid: str) -> bool:
            got = deck_picture(oid)
            if not got:
                return False
            if got[0] == ours[0]:
                return True
            if same_look(ours[1], got[1]):
                return True
            return ours[2] is not None and got[2] is not None and not picture_differs(ours[2], got[2], TOL["phash"])

        def adopt(skey: SlideKey, ours_members: Sequence[ElementEntry], read: SlideRead,
                  oid: ObjectId | None) -> ObjectId | None:
            if len(ours_members) != 1 or ours_members[0].kind != "image":
                return None
            el = ours_members[0]
            file = (el.ir or {}).get("file")
            if not file or not isinstance(file, str):
                return None
            ours = our_picture(self.ours_out / file)
            if ours is None or ours[1] is None:
                return None
            if oid is not None:  # the deck replaced this element's own picture
                return oid if same(ours, oid) else None
            box = [v * scale for v in el.fingerprint.bbox]
            best: ObjectId | None = None
            score = PICTURE_OVERLAP
            for cand, rb in read.objects.items():
                if rb.image is None or cand in base_ids or cand in taken or rb.parent_group \
                        or (rb.title or "").startswith(snapshot.TAG_PREFIX):
                    continue
                over = box_overlap(box, rb.box)
                if over >= score and same(ours, cand):
                    best, score = cand, over
            if best:
                taken.add(best)
            return best
        return adopt

    # ---- the master and the layouts (theme_sync)

    def plan_theme(self, mplan: merge.MergePlan, pres: Presentation) -> None:
        """The theme's part of the merge, planned beside the slides' and reported with them. A
        deck `adopt` took over has a theme of the person's own and is left alone; a base from
        before theme sync records nothing of the layouts, so they are left alone too (said so when
        the source's theme changed). What it applied and its conflicts are JSON already, kept apart
        and put behind the slides' in the report (`report_of`)."""
        from . import theme_sync
        from .adopt_sync import ORIGIN

        self.theme_plan = None
        self.theme_applied = []
        self.theme_conflicts = []
        if self.base.get("origin") == ORIGIN:
            return
        ours = theme_ours_of(self.built)
        if self.theme_side is None:
            self.theme_side = theme_sync.ours_side(ours)
        report = mplan.report
        if not self.base.get("theme"):
            why = theme_sync.old_base_warning(self.base, self.theme_side)
            if why:
                report.warnings.append(why)
            return
        tp = theme_sync.plan(self.base, self.theme_side, ours, pres, self.tok, self.picture_url,
                             lambda page: f"b2s_th_{h6(page)}_{self.tok}", self.live_pictures(pres))
        self.theme_plan = tp
        self.theme_applied = list(tp.applied)
        self.theme_conflicts = list(tp.conflicts)
        report.warnings += tp.warnings

    def master_key(self) -> str | None:
        """The background a slide shows by inheriting the master: the new source's shared one once
        the base records the theme (this sync writes it there, or the person's own stays, which a
        slide inheriting it shows either way), else what convert put there."""
        if self.base.get("theme") and self.theme_side is not None:
            return self.theme_side.shared
        return as_optional_str(self.base.get("master_background"), "base.master_background")

    def prepare(self, mplan: merge.MergePlan, pres: Presentation, theirs: JsonObject) -> Work:
        """What to write, per slide: units to (re)create with their requests' inputs, deletions,
        moves, backgrounds, notes; pictures needed from the staging deck."""
        ours_slides = self.plan.slides()
        pages = [as_int(s["page"], "slide.page") for s in ours_slides]
        new_ids: dict[str, str] = {p.key: f"b2s_{h6(p.key)}_{self.tok}" for p in mplan.slides
                                   if isinstance(p, merge.CreateSlide)}
        # Internal links: PDF page -> live slide (existing, or created now).
        kept: list[tuple[int, str]] = []
        for p in mplan.slides:
            match p:
                case merge.CreateSlide():
                    kept.append((pages[p.ours], new_ids[p.key]))
                case merge.UpdateSlide() | merge.HoldSlide():
                    kept.append((pages[p.ours], p.object_id))
                case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide():
                    pass
                case _:
                    assert_never(p)
        kept.sort()
        page_slide: dict[int, str] = {}
        if kept:
            for page in range(kept[-1][0] + 1):
                page_slide[page] = next(sid for pg, sid in kept if pg >= page)
        self.plan.page_slide = page_slide
        master = self.master_key()
        pictures: dict[str, str | None] = {}
        writes = merge.has_writes_of(mplan, [as_str(s["objectId"], "theirs slide objectId") for s in _slides(theirs, "theirs")])
        if self.theme_plan is not None:
            pictures.update(self.theme_plan.stage)
            writes = writes or bool(self.theme_plan.requests or self.theme_plan.cleanup)
        entries = _slides(self.ours, "ours")
        slides: list[SlideWork] = []
        for p in mplan.slides:
            match p:
                case merge.CreateSlide():
                    w = SlideWork(p, new_ids[p.key], list(range(len(as_array(ours_slides[p.ours]["elements"], "elements")))))
                case merge.UpdateSlide():
                    index = {as_str(e["key"], "element key"): k
                             for k, e in enumerate(as_objects(entries[p.ours]["elements"], "ours slide elements"))}
                    w = SlideWork(p, p.object_id, [index[k] for u in p.units if written(u.decision) for k in u.ours_members])
                case merge.HoldSlide():
                    w = SlideWork(p, p.object_id, [])
                case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide():
                    w = SlideWork(p, None, [])
                case _:
                    assert_never(p)
            j = ours_index(p)
            if w.units and j is not None:
                elements = as_objects(ours_slides[j]["elements"], "elements")
                for i in w.units:
                    if elements[i]["kind"] == "image":
                        pictures[str(self.ours_out / as_str(elements[i]["file"], "image file"))] = None
            background = self.background_written(p)
            if background.startswith("png:") and background != master and j is not None:
                pictures[str(self.ours_out / as_str(ours_slides[j]["background"], "slide background"))] = "background"
            slides.append(w)
        order = [new_ids.get(x[4:], x) if x.startswith("new:") else x for x in mplan.order]
        return Work(slides=slides, pictures=pictures, new_ids=new_ids, page_slide=page_slide, writes=writes, order=order)

    # ---- pictures

    def in_background(self, fn: "Callable[[SlidesService, DriveService], T]", name: str) -> "Future[T] | None":
        """`fn(slides, drive)` on a thread of its own with clients of its own, or None where it has
        to be run here: a client a caller lent us is that caller's and this thread is about to use
        it (`emit.measure_places`' rule). The pool is left to the interpreter; every future this
        returns is collected by its caller, in `run`'s own `finally` if nowhere else."""
        from .google_auth import credentials_for_threads, drive_service, shared_service, slides_service

        if shared_service("drive", "v3") or shared_service("slides", "v1"):
            return None
        creds = credentials_for_threads()   # resolved here: a worker inherits no context
        pool = ThreadPoolExecutor(1, thread_name_prefix=name)
        try:
            return pool.submit(lambda: fn(slides_service(creds), drive_service(creds)))
        finally:
            pool.shutdown(wait=False)

    def picture_url(self, path: str | Path) -> str:
        """The staging deck's URL for a picture, or a marker `fill_urls` replaces before the batch
        goes out.

        The requests are built while the staging deck is still being imported (`write`), and this is
        the **one** field in them that waits for it: every object id a picture brings is the
        element's own (`new_oid`), so what the pending marker has to record - the objects this run
        is about to create - does not wait for Drive at all."""
        return self.urls.get(str(path)) or f"{PENDING_URL}{path}"

    def fill_urls(self, reqs: list[JsonObject]) -> None:
        """Put the staging deck's URLs into the requests built before it existed. A picture the
        staging deck did not bring fails here, loudly, rather than as a batch Google refuses:
        nothing goes out carrying a marker."""
        def fill(node: Json) -> Json:
            if isinstance(node, dict):
                for k, v in node.items():
                    node[k] = self.urls[v[len(PENDING_URL):]] \
                        if isinstance(v, str) and v.startswith(PENDING_URL) else fill(v)
            elif isinstance(node, list):
                node[:] = [fill(v) for v in node]
            return node

        for r in reqs:   # every marker, wherever it sits: a picture's URL and a slide background's
            fill(r)

    def stage_in_background(self, work: Work) -> "Future[str | None] | None":
        """`stage` on a thread of its own, or None where there is nothing to stage or it has to be
        run here (`in_background`)."""
        if not [f for f in work.pictures if f not in self.urls]:
            return None
        return self.in_background(lambda slides, drive: self.stage(work, drive, slides), "b2s-stage")

    def drop_staging(self, fid: str) -> None:
        """The staging deck has done its work once the live deck holds the pictures, and deleting it
        is the one thing at the end of a write nothing waits for - so it goes on a thread and this
        run carries on. Collected before `sync` returns (`await_deletes`), never left to the
        interpreter: a file nobody deletes is one somebody finds in their Drive."""
        self.urls.clear()
        self.staging = None
        fut = self.in_background(lambda slides, drive: delete_file(drive, fid), "b2s-drop")
        if fut is None:
            delete_file(self.drive, fid)
        else:
            self.deleting.append(fut)

    def await_deletes(self) -> None:
        """Wait for the staging decks this run sent away (`drop_staging`); their failures are the
        caller's to hear about, not to stop on - the deck is written either way."""
        for fut in self.deleting:
            with contextlib.suppress(Exception):
                fut.result()
        self.deleting = []

    def stage(self, work: Work, drive: DriveService | None, slides: SlidesService | None) -> str | None:
        """Pictures go through a staging deck imported from a .pptx; its images' contentUrls are
        then used in the live deck. Returns the staging file's id: delete it once the live deck
        has the pictures (the URLs stop working with it).

        `drive` / `slides`: the clients to use, where this runs on a thread of its own
        (`stage_in_background`) and may not touch the ones this sync is using meanwhile; None:
        this sync's own."""
        from .emit import PPTX_MIME, build_pptx
        from .gapi import media_upload

        drive, slides = drive or self.drive, slides or self.slides
        needed = [f for f in work.pictures if f not in self.urls]
        if not needed:
            return None
        page_w, page_h = _nums(self.plan.slides()[0]["size"], "slide size")
        pages: list[dict[str, object]] = []
        pictures = [f for f in needed if work.pictures[f] != "background"]
        backgrounds = [f for f in needed if work.pictures[f] == "background"]
        for k in range(0, len(pictures), 40):
            pages.append({"layout": "BLANK", "fill": None, "templates": False, "pictures": [
                {"file": f, "bbox": [0, 0, *self._fit(f)], "alt": f"b2s-stage:{k + n}", "title": "stage"}
                for n, f in enumerate(pictures[k:k + 40])]})
        for f in backgrounds:
            pages.append({"layout": "BLANK", "fill": {"picture": Path(f)}, "templates": False, "pictures": []})
        pptx = build_pptx(page_w, page_h, [], pages, {"color": "#ffffff"}, None)
        # The marker outlives a killed sync: nothing deletes a staging deck from a file's word, so
        # `tools/drive_usage.py` needs to be able to say which files are certainly leftovers.
        from .drive_folder import place
        fid = google_types.file_id(execute(drive.files().create(
            body=place({"name": "beamer2slides sync staging (temporary)",
                        "mimeType": "application/vnd.google-apps.presentation",
                        "appProperties": {"b2sStaging": self.pid}}, drive),
            media_body=media_upload(pptx, PPTX_MIME), fields="id")), "the staging deck")
        try:
            staged = execute(slides.presentations().get(presentationId=fid))
            made = staged.get("slides", [])
            for s in made[:len(pages) - len(backgrounds)]:
                for e in s.get("pageElements", []):
                    d = e.get("description") or ""
                    url = google_types.image_url(e)
                    if url is not None and d.startswith("b2s-stage:"):
                        self.urls[pictures[int(d[10:])]] = url
            for s, f in zip(made[len(pages) - len(backgrounds):], backgrounds):
                url = google_types.background_url(s)
                if url is not None:
                    self.urls[f] = url
            missing = [f for f in needed if f not in self.urls]
            if missing:
                raise RuntimeError(f"the staging deck brought no picture for {missing[:3]}")
        except Exception:
            execute(drive.files().delete(fileId=fid))
            raise
        return fid

    def _fit(self, path: str) -> list[float]:
        w, h = png_size(Path(path))
        s = min(700 / w, 390 / h, 1.0)
        return [max(1.0, w * s), max(1.0, h * s)]

    # ---- measuring

    def measure_places(self, work: Work, theirs: JsonObject) -> "tuple[dict[str, Place], list[str]]":
        from .emit import measure_places, slide_holes

        if not self.measure:
            return {}, []
        ours_slides = self.plan.slides()
        slides: list[Json] = []
        for w in work.slides:
            j = ours_index(w.plan)
            if not w.units or j is None:
                continue
            slide = ours_slides[j]
            elements = as_objects(slide["elements"], "elements")
            ids = {elements[i]["id"] for i in w.units}
            if any(h[3] is not None and h[3]["id"] in ids for h in slide_holes(slide)) or \
                    any(e.get("marks") and e["id"] in ids for e in elements):
                slides.append(slide)
        from . import net
        if not slides:
            return {}, []
        if net.downloads_off():
            print("picture places: predicted (downloads are switched off, so no thumbnail can be measured)")
            return {}, []
        self.before_write()   # it adds scratch slides to the deck (b2s_mNNN)
        live_slides = _slides(theirs, "theirs")
        live_ids = {as_str(s["objectId"], "theirs slide objectId") for s in live_slides}
        first = as_str(live_slides[0]["objectId"], "theirs slide objectId")
        page_slide = {k: v if v in live_ids else first for k, v in self.plan.page_slide.items()}
        return measure_places(self.slides, self.pid, {**self.plan.deck, "slides": slides}, self.scale, self.plan.fonts,
                              self.plan.placed, page_slide, self.ours_out, self.plan.page_width)

    def delete_scratch(self, scratch: list[str]) -> None:
        self.before_write()
        try:
            execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body={
                "requests": [delete_request(s) for s in scratch]}))
        except HttpError as e:
            self.warnings.append(f"could not delete scratch slides {scratch}: {e}")

    # ---- content

    def main_requests(self, work: Work, theirs: JsonObject, pres: Presentation, moves: "Mapping[str, Place]",
                      scratch: list[str]) -> tuple[list[JsonObject], list[JsonObject]]:
        """(content, cleanup). Nothing in `content` destroys anything a person could have edited:
        it creates the new objects, refills placeholders and puts the slides in order. Every
        deletion - the objects a recreated unit replaces, the slides the source removed, the plain
        stand-in shapes - goes into `cleanup`, which is sent after the deck's own edits are back on
        the new objects and a base that no longer mentions the old ones is stored."""
        read = _slides(theirs, "theirs")
        ids = [as_str(s["objectId"], "theirs slide objectId") for s in read]
        live = dict(zip(ids, read))
        layouts = {name: l for l in pres.get("layouts", [])
                   if (name := l.get("layoutProperties", google_types.LayoutProperties()).get("name")) is not None}
        reqs: list[JsonObject] = []
        doomed_slides: list[str] = []
        if self.theme_plan is not None:
            # The master and the layouts first, in the same chain of batches: a layout write and a
            # slide batch in flight together can undo each other's placeholder boxes (the last
            # commit wins), and these go out one after another.
            if self.theme_plan.requests:
                reqs += [*self.theme_plan.requests, BREAK]
            self.cleanup_ids = list(dict.fromkeys([*self.cleanup_ids, *self.theme_plan.cleanup]))
        created: list[str] = []
        for w in work.slides:
            p = w.plan
            match p:
                case merge.DeleteSlide():
                    doomed_slides.append(p.object_id)
                case merge.CreateSlide():
                    reqs += self.new_slide(w, layouts, moves, pres) + [BREAK]
                    if w.sid is not None:
                        created.append(w.sid)
                case merge.UpdateSlide() | merge.HoldSlide():
                    reqs += self.update_slide(w, live[p.object_id], moves, pres) + [BREAK]
                case merge.GoneSlide() | merge.KeepRemovedSlide():
                    pass
                case _:
                    assert_never(p)
        reqs += [delete_request(s) for s in scratch]  # (sync's own scratch slides)
        # Slide order: created slides were appended.
        current = [sid for sid in ids if sid not in set(doomed_slides)]
        current += created
        final = [s for s in dict.fromkeys(work.order) if s in current]  # (an id can't be in two places)
        for i, sid in enumerate(final):
            if current[i] != sid:
                reqs.append(google_types.slides_json({"updateSlidesPosition": {"slideObjectIds": [sid],
                                                                               "insertionIndex": i}}))
                current.remove(sid)
                current.insert(i, sid)
        self.cleanup_ids = list(dict.fromkeys([*self.cleanup_ids, *doomed_slides]))
        cleanup = [delete_request(oid) for oid in self.cleanup_ids]
        return reqs, cleanup

    def slide_requests(self, ours: int, units: Sequence[int], sid: str, in_place: Mapping[int, Refilled],
                       templates: "Mapping[TemplateKey, Template]", moves: "Mapping[str, Place]", new_slide: bool,
                       ungrouped: Collection[int]
                       ) -> tuple[list[JsonObject], dict[int, list[str]], dict[int, str], list[JsonObject]]:
        """emit's requests for the chosen elements (`units`) of the new conversion's slide `ours`,
        under live object ids. in_place: element index -> the live placeholder or table it goes
        into; templates: template key -> the live object (or stand-in) to duplicate (`Template`);
        ungrouped: element indices whose own group (with anchored pictures) isn't made.
        Returns (requests, element index -> objects created, element index -> new object id,
        extras: the slide-level requests (groups, z-order) for a new slide)."""
        from .emit import created_ids, subtitle_element, table_requests, title_element

        o = _slides(self.ours, "ours")[ours]
        okey = as_str(o["key"], "ours slide key")
        slide = self.plan.slides()[ours]
        source = as_objects(slide["elements"], "elements")
        n = as_int(slide["page"], "slide.page")
        vsid = f"b2s_s{n:03}"
        # A title with no live placeholder to go into becomes a text box.
        elements: list[JsonObject] = [dict(e) for e in source]
        title_idx = title_element(slide)
        demoted = title_idx is not None and title_idx not in in_place
        if title_idx is not None and title_idx not in in_place:
            elements[title_idx]["role"] = "body"
        copied: list[Json] = [e for e in elements]
        slide_copy: JsonObject = {**slide, "elements": copied}
        title_idx = title_element(slide_copy)
        sub_idx = subtitle_element(slide_copy, title_idx) if title_idx is not None else None
        if sub_idx is not None and sub_idx not in in_place:
            slide_copy["title_page"] = False
        page_elements: dict[str, list[JsonObject]] = {
            vsid: [{"objectId": f"{vsid}_t{i}", "size": emu_size_json(v.size[0], v.size[1])}
                   for i, v in in_place.items() if isinstance(v, InPlace)]}
        keys = self.plan.keys
        sizes = [(templates[k].w, templates[k].h) if k in templates else (STAND_IN, STAND_IN) for k in keys]
        parts, element_ids = self.plan.slide_parts(slide_copy, page_elements, {}, moves, sizes)
        if demoted and not new_slide:
            # The other elements as on a slide with its title (a title demoted to body text
            # would widen their right limits, emit.text_right_limit): a stand-in placeholder size.
            orig_title = title_element(slide)
            orig_sub = subtitle_element(slide, orig_title) if orig_title is not None else None
            stand: list[JsonObject] = [{"objectId": f"{vsid}_t{i}", "size": emu_size_json(STAND_IN, STAND_IN)}
                                       for i in (orig_title, orig_sub) if i is not None and i not in in_place]
            full, _ = self.plan.slide_parts(slide, {vsid: page_elements[vsid] + stand}, {}, moves, sizes)
            demoted_idx = {i for i in (orig_title, orig_sub) if i is not None and i not in in_place}
            parts = [full[0]] + [parts[1 + i] if i in demoted_idx else full[1 + i] for i in range(len(element_ids))] + \
                parts[1 + len(element_ids):]
        mapping: dict[str, str] = {}
        new_oid: dict[int, str] = {}
        for i, (vid, e) in enumerate(zip(element_ids, as_objects(o["elements"], f"ours slide {okey}: elements"))):
            new_oid[i] = in_place[i].id if i in in_place else \
                f"b2s_{h6(okey)}_{h6(as_str(e['key'], 'element key'))}_{self.tok}"
            mapping[vid] = new_oid[i]
        for j, k in enumerate(keys):
            if k in templates:
                mapping[f"{vsid}_k{j}"] = templates[k].id
        mapping[vsid] = sid
        order = sorted(mapping.items(), key=lambda kv: -len(kv[0]))
        chosen = set(units)
        reqs: list[JsonObject] = []
        objects: dict[int, list[str]] = {}
        for i, (el, rs) in enumerate(parts[1:1 + len(element_ids)]):
            if i not in chosen:
                continue
            rs = rename_requests(rs, order)
            if not new_slide:
                rs = [r for r in rs if "updatePageElementsZOrder" not in r]
            if el is not None and el["kind"] == "image":
                path = self.ours_out / as_str(el["file"], "image file")
                box = [v * self.scale for v in _nums(self.plan.placed(source[i], n)["bbox"], "image bbox")]
                x0, y0, x1, y1 = box
                image: google_types.CreateImageRequest = {
                    "objectId": new_oid[i], "url": self.picture_url(path), "elementProperties": {
                        "pageObjectId": sid, "size": emu_size(x1 - x0, y1 - y0),
                        "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU", "translateX": round(x0 * EMU_PER_PT),
                                      "translateY": round(y0 * EMU_PER_PT)}}}
                rs = [google_types.slides_json({"createImage": image}),
                      letterbox_fix(new_oid[i], box, png_size(path))] + rs
            live = in_place.get(i)
            if isinstance(live, TableFill):
                # Refilled where it is (`table_refill`): its cells emptied, then filled as emit
                # fills a table the .pptx brought, whose margins this one has.
                rs = [delete_text_request(new_oid[i], {"rowIndex": r, "columnIndex": c}) for r, c in live.cells] + \
                     [named_step(step, new_oid[i]) for step in live.steps] + \
                     [r for r in table_requests(self.plan.placed(source[i], n), sid, new_oid[i], self.scale,
                                                self.plan.fonts, imported=True) if "updatePageElementsZOrder" not in r]
                dx, dy = live.shift
                if abs(dx) > 0.01 or abs(dy) > 0.01:  # (the source moved it)
                    rs.append(google_types.slides_json({"updatePageElementTransform": {
                        "objectId": new_oid[i], "applyMode": "RELATIVE", "transform": {
                            "scaleX": 1, "scaleY": 1, "unit": "EMU",
                            "translateX": round(dx * EMU_PER_PT), "translateY": round(dy * EMU_PER_PT)}}}))
            elif isinstance(live, InPlace) and live.text:
                rs = [delete_text_request(new_oid[i], None)] + rs
            out: list[JsonObject] = []
            for r in rs:  # a duplicated live object brings its text along: clear it
                out.append(r)
                if "duplicateObject" in r:
                    dup = as_object(r["duplicateObject"], "duplicateObject")
                    src = dup["objectId"]
                    tpl = next((t for t in templates.values() if t.id == src), None)
                    if tpl and tpl.text:
                        out += [delete_text_request(as_str(v, "duplicateObject.objectIds"), None)
                                for v in as_object(dup["objectIds"], "duplicateObject.objectIds").values()]
            reqs += out
            objects[i] = list(dict.fromkeys([new_oid[i]] + [x for x in created_ids(rs) if x != new_oid[i]]))
        extras: list[JsonObject] = []
        for _, rs in parts[1 + len(element_ids):]:
            for r in rename_requests(rs, order):
                if "groupObjects" in r:
                    gid = as_str(as_object(r["groupObjects"], "groupObjects")["groupObjectId"], "groupObjectId")
                    owner = next((i for i, v in new_oid.items() if gid == f"{v}_g"), None)
                    if owner is not None and owner in chosen:
                        if owner in ungrouped:
                            continue  # (the deck took this unit's group apart: it stays so)
                        reqs.append(r)
                        objects[owner].append(gid)
                        continue
                extras.append(r)
        return reqs, objects, new_oid, extras

    def tag_requests(self, o: JsonObject, objects: Mapping[int, list[str]], new_oid: Mapping[int, str],
                     in_place: Mapping[int, Refilled]) -> list[JsonObject]:
        slide = self.plan.slides()[_slides(self.ours, "ours").index(o)]
        okey = as_str(o["key"], "ours slide key")
        entries = as_objects(o["elements"], f"ours slide {okey}: elements")
        source = as_objects(slide["elements"], "elements")
        reqs: list[JsonObject] = []
        for i, oids in objects.items():
            e, el = entries[i], source[i]
            if el["kind"] == "diagram" and len(oids) > 1:
                # A diagram's own object id *is* the group emit builds its nodes and lines under
                # (emit.diagram_requests). The API refuses updatePageElementAltText on a group and
                # rejects the whole batch with it, so a sync that rewrote a diagram slide died. It
                # goes untagged, as it does in a converted deck (snapshot.tag_requests skips element
                # groups too); the base names its objects itself, so nothing needs the tag.
                continue
            r: google_types.UpdatePageElementAltTextRequest = {
                "objectId": new_oid[i], "title": snapshot.tag(okey, as_str(e["key"], "element key"))}
            alt = el.get("alt")
            if el["kind"] == "image" and alt:
                r["description"] = as_str(alt, "image alt")
            reqs.append(google_types.slides_json({"updatePageElementAltText": r}))
        return reqs

    def new_layout(self, slide: JsonObject, layout_name: str, layouts: Mapping[str, Page],
                   pres: Presentation) -> Page | None:
        """The layout a new slide is created on. In a themed deck emit puts the theme decoration
        on the layouts (emit.plan_theme, `theme.layouts` in emit.json) and gives backgrounds that
        don't show it a copy of their layout without it. A new slide that inherits the master
        background takes the plain layout (the decorated one); one with a background picture of its
        own already carries whatever decoration it shows, so it goes on the same layout as the
        converted slides with that background - found by object id, since the copies' names in the
        deck are the .pptx ones ("Title Only (no theme)"), not the b2s names."""
        key = snapshot.background_key(slide, self.ours_out)
        base_slides = _slides(self.base, "base")
        served = self.theme_plan.page_group if self.theme_plan is not None else None
        if served and self.theme_side is not None:
            # The layout that serves the decoration a fresh conversion gives this slide now
            # (theme_sync.plan), of the kind it needs: the plain one first, then its copies.
            want = self.theme_side.groups.get(as_int(slide["page"], "slide.page"))
            kinds = {lid: b.get("layout") for b in base_slides if isinstance(lid := b.get("layoutObjectId"), str)}
            plain = layouts.get(layout_name)
            if plain is not None and served.get(object_id(plain)) == want:
                return plain
            for l in pres.get("layouts", []):
                if served.get(object_id(l)) == want and kinds.get(object_id(l)) == layout_name:
                    return l
        if key != self.master_key():
            by_id = {object_id(l): l for l in pres.get("layouts", [])}
            same = [b for b in base_slides if b.get("background") == key
                    and b.get("layout") == layout_name and b.get("layoutObjectId") in by_id]
            if same:
                return by_id[as_str(same[0]["layoutObjectId"], "base slide layoutObjectId")]
        return layouts.get(layout_name) or layouts.get("BLANK")

    def new_slide(self, w: SlideWork, layouts: Mapping[str, Page], moves: "Mapping[str, Place]",
                  pres: Presentation) -> list[JsonObject]:
        from .emit import element_template_keys, slide_layout, subtitle_element, title_element

        p = w.plan
        j, sid = ours_index(p), w.sid
        if j is None or sid is None:
            raise ValueError(f"slide {p.key}: only a slide of the new conversion is created")
        o = _slides(self.ours, "ours")[j]
        okey = as_str(o["key"], "ours slide key")
        entries = as_objects(o["elements"], f"ours slide {okey}: elements")
        slide = self.plan.slides()[j]
        layout_name, title_kind = slide_layout(slide)
        layout = self.new_layout(slide, layout_name, layouts, pres)
        if layout is None:
            raise RuntimeError(f"the deck has no {layout_name} layout for new slide {okey}")
        mappings: list[JsonObject] = []
        in_place: dict[int, Refilled] = {}
        title_idx = title_element(slide)
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        for e in layout.get("pageElements", []):
            ph = google_types.part(google_types.part(e.get("shape"), "shape").get("placeholder"), "placeholder")
            if not ph:
                continue
            box = e.get("size")
            size = (snapshot._unit(box.get("width")), snapshot._unit(box.get("height"))) if box is not None \
                else (STAND_IN, STAND_IN)
            kind = ph.get("type")
            if kind == title_kind and title_idx is not None and title_idx not in in_place:
                oid = f"b2s_{h6(okey)}_{h6(as_str(entries[title_idx]['key'], 'element key'))}_{self.tok}"
                in_place[title_idx] = InPlace(id=oid, size=size, text="")  # (a new slide's placeholder is empty)
            elif kind == "SUBTITLE" and sub_idx is not None and sub_idx not in in_place:
                oid = f"b2s_{h6(okey)}_{h6(as_str(entries[sub_idx]['key'], 'element key'))}_{self.tok}"
                in_place[sub_idx] = InPlace(id=oid, size=size, text="")
            else:
                continue  # (not every layout placeholder is instantiated: the rest go after a read, in finish)
            mappings.append({"layoutPlaceholder": {"type": ph["type"], "index": ph.get("index", 0)}, "objectId": oid})
        reqs = [google_types.slides_json({"createSlide": {
            "objectId": sid, "slideLayoutReference": {"layoutId": object_id(layout)}, "placeholderIdMappings": mappings}})]
        reqs += self.background_requests(sid, as_str(o["background"], "ours slide background"), slide, pres, True)
        templates: dict[TemplateKey, Template] = {}
        if self.plan.uses_templates[as_int(slide["page"], "slide.page")]:
            for k, key in enumerate(self.plan.keys):
                stand = f"b2s_{h6(okey)}_k{k}_{self.tok}"
                templates[key] = Template(id=stand, w=STAND_IN, h=STAND_IN, text="")
                reqs.append(stand_in_request(stand, sid, key[0]))
            used = {k for e in as_objects(slide["elements"], "elements") for k in element_template_keys(e, self.scale)}
            if used:
                self.warnings.append(f"slide {okey}: {len(used)} template shape(s) (shadows, exact corners) made as plain shapes")
        # Pictures first, as the .pptx brings them; emit's parts then order everything.
        rs, objects, new_oid, extras = self.slide_requests(j, w.units, sid, in_place, templates, moves, True, frozenset())
        reqs += [r for r in rs if "createImage" in r]  # (a picture's other requests follow in element order)
        reqs += [r for r in rs if "createImage" not in r] + extras
        w.objects, w.new_oid, w.in_place = objects, new_oid, in_place
        w.tops = {}
        w.doomed = set()
        w.groups = [as_str(as_object(r["groupObjects"], "groupObjects")["groupObjectId"], "groupObjectId")
                    for r in extras if "groupObjects" in r]
        reqs += self.tag_requests(o, objects, new_oid, in_place)
        return reqs

    def update_slide(self, w: SlideWork, read: JsonObject, moves: "Mapping[str, Place]",
                     pres: Presentation) -> list[JsonObject]:
        """A kept slide's requests: its units the plan (re)creates, deletes or moves, its background
        and notes. A held slide (`merge.HoldSlide`) has none of those, and gets nothing."""
        from .emit import bend_template_key, element_template_keys, label_inside, node_template_key, template_key

        p = updated(w.plan)
        units = slide_units(p)
        b = _slides(self.base, "base")[p.base]
        o = _slides(self.ours, "ours")[p.ours]
        okey = as_str(o["key"], "ours slide key")
        slide = self.plan.slides()[p.ours]
        source = as_objects(slide["elements"], "elements")
        sid = p.object_id
        objects = _objects(read, f"slide {sid}")
        objs = {oid: as_object(rb, f"read-back {oid}") for oid, rb in objects.items()}
        bunits = merge.units(as_objects(b["elements"], "base slide elements"))
        reqs: list[JsonObject] = []
        w.objects = {}
        w.new_oid = {}
        w.in_place = {}
        w.groups = []
        recreated = [u for u in units if written(u.decision)]
        index = {as_str(e["key"], "element key"): k for k, e in enumerate(as_objects(o["elements"], "ours elements"))}

        # Placeholders: a recreated title goes back into its live placeholder.
        in_place: dict[int, Refilled] = {}
        for u in recreated:
            for mk in u.ours_members:
                i = index[mk]
                base_el = next((m for m in bunits.get(u.key, []) if m["key"] == mk), None)
                main = base_el.get("main") if base_el else None
                if isinstance(main, str) and main and main in objs and objs[main].get("placeholder"):
                    in_place[i] = placeholder_in_place(main, objs[main])
        from .emit import subtitle_element, title_element
        title_idx = title_element(slide)
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        # Only what emit writes into a placeholder goes into one: the title, and on the title page
        # the subtitle (its biggest plain text, `emit.subtitle_element`). Anything else emit makes a
        # box of, and a box made under a live placeholder's id is a createShape Slides refuses with
        # the whole batch - which a reworded authors' line that a longer line below it took the
        # subtitle role from used to be. It is made a box of its own; the placeholder goes to the
        # element that has the role now (below), or out with the old unit.
        for i in [i for i, v in in_place.items()
                  if i != (title_idx if objs[v.id].get("placeholder") in ("TITLE", "CENTERED_TITLE") else
                           sub_idx if objs[v.id].get("placeholder") == "SUBTITLE" else None)]:
            del in_place[i]
        # A new title (the source's title changed beyond recognition) or a text that took the
        # subtitle role goes into the placeholder of the element it replaces, when that one goes.
        roles: tuple[tuple[int | None, tuple[str, ...]], ...] = (
            (title_idx, ("TITLE", "CENTERED_TITLE")), (sub_idx, ("SUBTITLE",)))
        for role, kinds in roles:
            if role is None or role in in_place or not any(role == index[mk] for u in recreated for mk in u.ours_members):
                continue
            taken = {v.id for v in in_place.values()}
            for u in units:
                if not isinstance(u.decision, merge.DeleteUnit | merge.Recreate):
                    continue
                main = next((m.get("main") for m in bunits.get(u.key, [])[:1]), None)
                if isinstance(main, str) and main and main in objs and main not in taken \
                        and objs[main].get("placeholder") in kinds:
                    in_place[role] = placeholder_in_place(main, objs[main])
                    break
        # A table whose words alone changed is refilled where it is (`table_refill`) - but not one an
        # interrupted sync already rewrote: what the merge compares against is then the person's
        # version put back (`restore_in_place`), not what the table holds, and cells emptied or rows
        # inserted by that picture would double words or rows. Made again, it is right whatever it holds.
        restored = set(self.recovery.restore)
        for u in recreated:
            d = u.decision
            if isinstance(d, merge.Recreate) and len(u.ours_members) == 1 and len(bunits.get(u.key, [])) == 1 \
                    and bunits[u.key][0].get("main") not in restored:
                i = index[u.ours_members[0]]
                refill = table_refill(bunits[u.key][0], source[i], objs, self.scale, self.plan.fonts)
                if refill is not None:
                    if d.overrides.geometry and "position" not in d.source:
                        # The deck moved it and the source did not: where it is now is what a
                        # recreation's geometry override would give. When both moved it, the source's
                        # move goes on top of the person's place, as `carried` does for a recreation.
                        refill = replace(refill, shift=(0.0, 0.0))
                    in_place[i] = refill

        # Template shapes: duplicate a live object with the same key on this slide, else a stand-in.
        needed = {k for u in recreated for mk in u.ours_members
                  for k in element_template_keys(source[index[mk]], self.scale)}
        templates: dict[TemplateKey, Template] = {}
        stand_ins: list[str] = []
        if needed:
            for el in as_objects(b["elements"], "base slide elements"):
                ir, main = _obj(el.get("ir"), "base element ir"), el.get("main")
                if not isinstance(main, str) or not main:
                    continue
                found: list[tuple[TemplateKey, str]] = []
                if ir.get("kind") == "shape" and (tk := template_key(ir, self.scale)):
                    found.append((tk, main))
                elif ir.get("kind") == "diagram":
                    found += [(node_template_key(nd), f"{main}_n{j}") for j, nd in enumerate(as_objects(ir["nodes"], "ir.nodes"))
                              if nd["shape"] and label_inside(nd)]
                    found += [(bend_template_key(ln), f"{main}_l{j}") for j, ln in enumerate(as_objects(ir["lines"], "ir.lines"))
                              if ln.get("bend")]
                for key, oid in found:
                    if key in needed and key not in templates and oid in objs:
                        size = _nums(objs[oid]["size"], f"read-back {oid}: size")
                        templates[key] = Template(id=oid, w=size[0], h=size[1],
                                                  text=_text(objs[oid].get("text"), f"read-back {oid}: text").strip())
            for j, key in enumerate(self.plan.keys):
                if key in needed and key not in templates:
                    stand = f"b2s_{h6(okey)}_k{j}_{self.tok}"
                    templates[key] = Template(id=stand, w=STAND_IN, h=STAND_IN, text="")
                    stand_ins.append(stand)
                    reqs.append(stand_in_request(stand, sid, key[0]))
                    self.warnings.append(f"slide {okey}: no live {key[0]} to copy (shadow, exact corners): made a plain shape")

        # Groups the old objects were in (blocks, and groups the deck made around them): ungrouped
        # first, outermost first (a group inside a group can't be ungrouped), and regrouped with the
        # new objects under the same ids, innermost first.
        regroup, depth, roots_removed = self.regroups_of(units, bunits, read)
        reqs[:0] = [ungroup_request(g) for g in sorted(regroup, key=lambda g: depth[g])]

        # A unit whose group the deck took apart (its pictures and text still there) is rebuilt ungrouped.
        ungrouped: set[int] = set()
        for u in recreated:
            members = bunits.get(u.key) or []
            anchor: JsonObject = members[0] if members else {}
            anchor_main = anchor.get("main")
            gid = next((x for x in _strs(anchor.get("objects"), "element objects") if x == f"{anchor_main}_g"), None)
            if isinstance(u.decision, merge.Recreate) and u.key in index and gid and gid not in objs \
                    and isinstance(anchor_main, str) and anchor_main in objs:
                ungrouped.add(index[u.key])
        rs, created, new_oid, _ = self.slide_requests(p.ours, [index[mk] for u in recreated for mk in u.ours_members],
                                                      sid, in_place, templates, moves, False, ungrouped)
        reqs += rs
        w.objects, w.new_oid, w.in_place = created, new_oid, in_place
        # Old objects out - in the cleanup phase, once their replacements are written and the
        # deck's own edits are back on them (a placeholder refilled in place stays).
        keep_ids = {v.id for v in in_place.values()}
        doomed = [r for _, roots in roots_removed for r in roots if r not in keep_ids]
        w.doomed = set(doomed) | {c for r in doomed for c in merge._descendants(r, read)}
        self.cleanup_ids = [*self.cleanup_ids, *doomed, *stand_ins]
        # The text of a placeholder this sync overwrites: recorded so an interrupted run can be
        # told what the person had there (plan_recovery / restore_in_place).
        for v in in_place.values():
            rb = objs.get(v.id)
            if rb:
                saved: JsonObject = {k: rb[k] for k in IN_PLACE_FIELDS if k in rb}
                self.in_place_readback[v.id] = saved
        # Regroup: the unit's new top object takes its old root's place among the children.
        tops: dict[str, str] = {}
        for u in recreated:
            if u.key in index:
                i = index[u.key]
                oids = created.get(i, [])
                tops[u.key] = next((x for x in oids if x.endswith("_g") and x[:-2] == new_oid[i]), new_oid[i])
        reqs += self.regroup_requests(regroup, depth, objects, tops, keep_ids, self.zrank(o, bunits, tops))
        # Moves: the deck object goes where the source moved the element.
        reqs += self.move_requests_of(units, bunits, read, self.scale)
        reqs += self.tag_requests(o, created, new_oid, in_place)
        background = new_background(p)
        if background:
            reqs += self.background_requests(sid, background, slide, pres, False)
        notes = p.notes if isinstance(p, merge.UpdateSlide) else None
        notes_id = read.get("notes_id")
        if notes is not None and isinstance(notes_id, str) and notes_id:
            if read.get("notes"):
                reqs.append(delete_text_request(notes_id, None))
            if notes:
                reqs.append(google_types.slides_json({"insertText": {"objectId": notes_id, "text": notes}}))
        w.tops = tops
        return reqs

    @staticmethod
    def zrank(o: JsonMap, bunits: Mapping[str, Sequence[JsonMap]], tops: Mapping[str, str]) -> dict[str, int]:
        """Object id -> where the source draws that element, for every object a rewrite may put into
        a group: the ones this sync writes (`tops`) and the deck's own, which stand for the elements
        the sync is keeping."""
        pos = {as_str(e["key"], "element key"): i for i, e in enumerate(as_objects(o["elements"], "ours elements"))}
        rank = {oid: pos[key] for members in bunits.values() for m in members
                if isinstance(key := m["key"], str) and key in pos
                for oid in _strs(m.get("objects"), "element objects")}
        rank.update({oid: pos[k] for k, oid in tops.items() if k in pos})
        return rank

    @staticmethod
    def regroups(units: Sequence[JsonMap], bunits: Mapping[str, Sequence[JsonMap]], read: JsonMap
                 ) -> tuple[dict[ObjectId, Regroup], dict[ObjectId, int], list[tuple[str, list[Json]]]]:
        """`regroups_of` over units as `merge.plan_merge` writes them (the offline fuzz's replay), the
        old roots as JSON."""
        regroup, depth, roots_removed = Sync.regroups_of([planned_unit_of(u, f"unit {n}") for n, u in enumerate(units)],
                                                         bunits, read)
        return regroup, depth, [(k, [r for r in roots]) for k, roots in roots_removed]

    @staticmethod
    def regroups_of(units: Sequence[merge.PlannedUnit], bunits: Mapping[str, Sequence[JsonMap]],
                    read: JsonMap) -> tuple[dict[ObjectId, Regroup], dict[ObjectId, int], list[tuple[str, list[str]]]]:
        """The groups a slide's rewrite takes apart: group id -> {"remove": old roots of rewritten
        units in it, "unit_of": root -> unit key}, with every ancestor of such a group (empty), their
        depth, and (unit key, old root ids) of every unit rewritten or deleted."""
        objects = _objects(read, "slide read")

        def parent(oid: str) -> str | None:
            x = _obj(objects.get(oid), f"read-back {oid}").get("parent_group")
            return x if isinstance(x, str) and x else None

        def ancestors(g: str) -> list[str]:
            chain: list[str] = []
            x = parent(g)
            while x and x not in chain:
                chain.append(x)
                x = parent(x)
            return chain
        regroup: dict[ObjectId, Regroup] = {}
        roots_removed: list[tuple[str, list[str]]] = []
        for u in units:
            if not isinstance(u.decision, merge.Recreate | merge.DeleteUnit):
                continue
            members = bunits.get(u.key, [])
            roots = merge.unit_roots(members, read)
            roots_removed.append((u.key, roots))
            for r in roots:
                g = as_object(objects[r], f"read-back {r}").get("parent_group")
                if isinstance(g, str) and g and g not in roots:
                    regroup.setdefault(ObjectId(g), Regroup(remove=set(), unit_of={}))["remove"].add(r)
                    regroup[ObjectId(g)]["unit_of"][r] = u.key
                    for a in ancestors(g):
                        regroup.setdefault(ObjectId(a), Regroup(remove=set(), unit_of={}))
        return regroup, {g: len(ancestors(g)) for g in regroup}, roots_removed

    @staticmethod
    def regroup_requests(regroup: Mapping[ObjectId, Regroup], depth: Mapping[ObjectId, int], objects: JsonMap,
                         tops: Mapping[str, str], keep_ids: Collection[str], rank: Mapping[str, int]
                         ) -> list[JsonObject]:
        """The groups `regroups` took apart, made again under the same ids, innermost first: a
        rewritten unit's new top object takes its old root's place among the children (`tops`: unit
        key -> new top; `objects`: the read-back before the rewrite; `rank`: object id -> where the
        source draws that element, for every child this sync writes and every one it keeps; `{}`:
        the children keep their order)."""
        reqs: list[JsonObject] = []
        replaced: dict[str, str | None] = {}  # regrouped group -> what stands for it now (None: gone)
        for g in sorted(regroup, key=lambda g: -depth[g]):
            info = regroup[g]
            children: list[str] = []
            mine: dict[int, str] = {}   # mine: place among the children -> the object standing there
            for c in _strs(as_object(objects[g], f"read-back {g}").get("children"), f"read-back {g}: children"):
                if c in info["remove"]:
                    ukey = info["unit_of"][c]
                    if ukey in tops and tops[ukey] not in children and tops[ukey] not in keep_ids:
                        mine[len(children)] = tops[ukey]
                        children.append(tops[ukey])
                elif c in replaced:
                    if (stands := replaced[c]):
                        children.append(stands)
                else:
                    mine[len(children)] = c
                    children.append(c)
            # The children that are elements of the source take the source's order among themselves,
            # in the places they hold; anything else in the group (a picture the person dropped in)
            # keeps its slot. Inside a group the child order is nobody's edit - Slides will not let
            # a person restack there - so it carries only what the last conversion drew, and keeping
            # it is keeping an opinion nobody holds. That is how a panel the source now draws
            # *under* a text came back on top of it, over words the person could read and over words
            # the sync kept for them (`loss_oracle.text_hidden`).
            if (slots := [i for i, oid in mine.items() if oid in rank]) and len(slots) > 1:
                for slot, oid in zip(slots, sorted((mine[i] for i in slots), key=lambda x: rank[x])):
                    children[slot] = oid
            if len(children) >= 2:
                # A group keeps its children's z-order, and the new members were created last, so
                # on top: a block's recreated panels covered the body text the person had edited and
                # sync kept (live, the front page's demo). Once grouped, a child can't be restacked
                # (`restack` orders what is on the page), so the old order goes back now, while they
                # are all still on the page. (The offline fuzz replays this: `fuzz_sync._stacked`.)
                reqs += [front_request(c) for c in children]
                reqs.append(google_types.slides_json({"groupObjects": {"groupObjectId": g,
                                                                       "childrenObjectIds": children}}))
                replaced[g] = g
            else:
                replaced[g] = children[0] if children else None
        return reqs

    def background_requests(self, sid: str, key: str, slide: JsonMap, pres: Presentation,
                            created: bool) -> list[JsonObject]:
        master = self.master_key()
        if key == master:
            if created:
                return []  # emit leaves these slides inheriting the master, and a new slide already does
            if self.base.get("theme"):
                # The master may be getting a new background in this very sync (theme_sync), so the
                # slide goes back to inheriting it rather than copying today's. A layout page refuses
                # INHERIT; a slide takes it, named by `propertyState` alone (tools/probe: the whole
                # `pageBackgroundFill` as the field is refused).
                page = next((s for s in pres.get("slides", []) if object_id(s) == sid), None)
                if page is not None and snapshot.background(page) == {"state": "INHERIT"}:
                    return []
                return [page_properties_request(sid, "pageBackgroundFill.propertyState",
                                                {"pageBackgroundFill": {"propertyState": "INHERIT"}})]
            masters = pres.get("masters", [])
            if not masters:   # (every deck has one; an answer without it has nothing to copy)
                return []
            fill = google_types.background_fill(masters[0])
            url = google_types.background_url(masters[0])
            if url is not None:
                return [page_properties_request(sid, "pageBackgroundFill.stretchedPictureFill.contentUrl",
                                                {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": url}}})]
            if "solidFill" in fill:
                return [page_properties_request(sid, "pageBackgroundFill.solidFill.color",
                                                {"pageBackgroundFill": {"solidFill": fill["solidFill"]}})]
            return []
        if key.startswith("color:"):
            return [page_properties_request(sid, "pageBackgroundFill.solidFill.color",
                                            {"pageBackgroundFill": {"solidFill": {"color": api_colour(key[6:])}}})]
        url = self.picture_url(self.ours_out / as_str(slide["background"], "slide background"))
        return [page_properties_request(sid, "pageBackgroundFill.stretchedPictureFill.contentUrl",
                                        {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": url}}})]

    # ---- after the content: z-order, notes of new slides, base, deck overrides

    def finish(self, work: Work, mplan: merge.MergePlan, theirs: JsonObject, pres: Presentation, rev: str) -> str:
        raw = self.read()
        now = snapshot.read_presentation(raw)
        live = _by_id(now, "the deck written")
        before = _by_id(theirs, "theirs")
        reqs: list[JsonObject] = []
        for w in work.slides:
            p = w.plan
            match p:
                case merge.CreateSlide():
                    s = live.get(w.sid) if w.sid is not None else None
                    mine = {x for oids in w.objects.values() for x in oids} | set(w.groups)
                    shown: JsonObject = _objects(s, f"slide {w.sid}") if s else {}
                    reqs += [delete_request(oid) for oid, rb in shown.items()
                             if as_object(rb, f"read-back {oid}").get("placeholder") and oid not in mine]
                    notes_id = s.get("notes_id") if s else None
                    if s and notes_id and (notes := _slides(self.ours, "ours")[p.ours].get("notes")):
                        reqs.append(google_types.slides_json({"insertText": {
                            "objectId": as_str(notes_id, "notes_id"), "text": as_str(notes, "ours slide notes")}}))
                case merge.UpdateSlide():
                    if w.objects:
                        reqs += self.restack_of(p.base, p.ours, p.units, w.doomed, w.tops, before[p.object_id],
                                                live[p.object_id])
                case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide():
                    pass
                case _:
                    assert_never(p)
        if reqs:
            rev = self.send("order", reqs, rev)
            raw = self.read()
            now = snapshot.read_presentation(raw)
        self.warn_about_folded_hiders(work, now)
        self.raw_after = raw   # (the layouts and the master as written: theme_sync.new_record)
        # read-back of objects as the converter created them, with the new pictures' signatures
        created = {x for w in work.slides for oids in w.objects.values() for x in oids}
        repainted = {w.sid for w in work.slides if w.sid is not None
                     and (isinstance(w.plan, merge.CreateSlide) or new_background(w.plan) is not None)}
        snapshot.sign_pictures(now, raw, created, repainted, PICTURE_WORKERS, None, None, self.drive,
                               self.written_files(work), None)
        self.created = now
        overrides = self.override_requests(work, theirs, now, raw_objects(pres), raw_objects(raw))
        if overrides:
            rev = self.send("overrides", overrides, rev)
            rev = self.refit(work, theirs, now, rev)
        self.final_revision = rev
        self.warn_about_overruns(work, theirs)
        return rev

    def written_files(self, work: Work) -> dict[str, Path]:
        """The file each picture this sync created was made from - an image's objectId, or a
        slide's for the background picture written on it - so the new base signs them from their
        bytes (`snapshot.upload_signatures`) instead of downloading what it has just uploaded."""
        ours_slides, master = self.plan.slides(), self.master_key()
        files: dict[str, Path] = {}
        for w in work.slides:
            p = w.plan
            j, sid = ours_index(p), w.sid
            if j is None or not sid:
                continue
            slide = ours_slides[j]
            source = as_objects(slide["elements"], "elements")
            for i in w.units:
                el = source[i]
                file = el.get("file")
                if el["kind"] == "image" and file and w.new_oid.get(i):
                    files[w.new_oid[i]] = self.ours_out / as_str(file, "image file")
            key = self.background_written(p)
            background = slide.get("background")
            if key.startswith("png:") and key != master and background:
                files[sid] = self.ours_out / as_str(background, "slide background")
        return files

    def background_written(self, p: merge.SlidePlan) -> str:
        """The background key this sync writes onto a slide: a new slide's own, an updated slide's
        when the source changed it; "" for none."""
        match p:
            case merge.CreateSlide():
                return _text(_slides(self.ours, "ours")[p.ours]["background"], "ours slide background")
            case merge.UpdateSlide():
                return new_background(p) or ""
            case merge.GoneSlide() | merge.KeepRemovedSlide() | merge.DeleteSlide() | merge.HoldSlide():
                return ""
            case _:
                assert_never(p)

    def warn_about_overruns(self, work: Work, theirs: JsonMap) -> None:
        """The person's own objects, which sync never moves, that the source's words or pictures
        now run over (`text_layout.overruns`): the source reflowed onto a note the person put under a
        paragraph. Moving the note would guess at what it belongs to, so it is said instead (live
        fuzz r7411, r7414). One read of the deck, only when a written slide holds such an object."""
        from . import text_layout as tl
        before = _by_id(theirs, "theirs")
        todo: dict[str, tuple[str, set[str], set[str]]] = {}
        for w in work.slides:
            p = w.plan
            if not isinstance(p, merge.UpdateSlide):
                continue  # (only an updated slide has units this sync wrote)
            b = before.get(p.object_id)
            if b is None or all(kept_whole(u.decision) for u in p.units):
                continue
            base_slide = _slides(self.base, "base")[p.base]
            users = {u["objectId"] for u in merge.user_objects(base_slide, b)}
            # A unit kept as the deck has it (a conflict the deck's version won) is the deck's too:
            # a table the person gave a row, kept whole, and the source's figure moved up under it
            # (edit hunt h2-2, h4-1). Sync does not move it either.
            kept = {u.key for u in p.units if isinstance(u.decision, merge.KeepRemoved | merge.KeepUnit) and u.decision.deck}
            shown = _objects(b, f"slide {p.object_id}")
            kept_objects = {o for e in as_objects(base_slide["elements"], "base slide elements")
                            if e["key"] in kept or e.get("anchor") in kept
                            for o in _strs(e.get("objects"), "element objects") if o in shown}
            if any(tl.ink(as_object(shown[u], f"read-back {u}")) for u in users | kept_objects if u in shown):
                todo[p.object_id] = (p.key, users, kept_objects)
        if not todo:
            return

        def say(rb: JsonMap) -> str:
            words = _text(rb.get("text"), "read-back text").strip().replace("\n", " ")[:40]
            return f"text {words!r}" if words else "picture"

        final = snapshot.read_presentation(self.read())
        for s in _slides(final, "final"):
            sid = as_str(s["objectId"], "final slide objectId")
            if sid not in todo:
                continue
            key, users, kept_objects = todo[sid]
            for o in tl.overruns(before[sid], s, users | kept_objects, set(self.cleanup_ids)):
                self.overruns.append({"slide": key, **tl.overrun_json(o)})
                mine = as_object(_objects(before[sid], f"slide {sid}")[o.object], f"read-back {o.object}")
                theirs_ = as_object(_objects(s, f"slide {sid}")[o.other], f"read-back {o.other}")
                self.warnings.append(
                    f"slide {key}: the source's {say(theirs_)} now runs {o.depth:.0f} pt over your {say(mine)}, "
                    + ("which stays where you put it (sync never moves your own objects): move one of them"
                       if o.object in users else
                       "kept as the deck has it for a conflict, so not where the source would put it: move one of them"))

    def refit_jobs(self, work: Work, theirs: JsonMap, created: JsonMap) -> dict[str, list[refit.RefitJob]]:
        """Slide id -> the recreated text units whose deck edits `override_requests` wrote over
        them (`refit.plan`'s jobs). Only text boxes the layout model can read (explicit sizes: what
        emit makes); a table refilled in place is not a recreated box. A recreated title goes back
        into its live placeholder at the box it already had there (`update_slide`'s `in_place`) -
        that box may be the person's own resize, so `refit.plan` leaves it alone - but a job still
        goes out for it: a panel the sync drew or grew under it is the converter's own object, and
        was never fitted to the person's enlarged font otherwise (edit hunt h5-6)."""
        from . import text_layout as tl
        pre = _by_id(created, "created")
        before = _by_id(theirs, "theirs")
        jobs: dict[str, list[refit.RefitJob]] = {}
        for w in work.slides:
            p = w.plan
            if not isinstance(p, merge.UpdateSlide):
                continue  # (only an updated slide has recreated units)
            s = pre.get(p.object_id)
            if s is None:
                continue
            o_elements = as_objects(_slides(self.ours, "ours")[p.ours]["elements"], "ours slide elements")
            ounits = merge.units(o_elements)
            bunits = merge.units(as_objects(_slides(self.base, "base")[p.base]["elements"], "base slide elements"))
            index = {as_str(e["key"], "element key"): k for k, e in enumerate(o_elements)}
            shown = _objects(s, f"slide {p.object_id}")
            for u in p.units:
                d = u.decision
                if not isinstance(d, merge.Recreate) or not merge.overrides_json(d.overrides) or u.key not in index:
                    continue
                i = index[u.key]
                main = w.new_oid.get(i)
                if main is None:
                    continue  # (nothing was made for it, so there is no box to fit)
                rb = _obj(shown.get(main), f"read-back {main}")
                if isinstance(w.in_place.get(i), TableFill) or not rb or not tl.text_box(rb) \
                        or tl.layout(rb) is None:
                    continue
                members = [(m, index[mk]) for m in ounits.get(u.key, [])
                           if isinstance(mk := m["key"], str) and mk in index]
                pics = [w.new_oid[k] for m, k in members[1:] if m.get("role") == "math" and k in w.new_oid]
                own = {x for _, k in members for x in w.objects.get(k, [])} | {main}
                bmembers = bunits.get(u.key) or []
                old_main = bmembers[0].get("main") if bmembers else None
                names = {w.new_oid[k]: as_str(m["key"], "element key") for m, k in members if k in w.new_oid}
                had = before.get(p.object_id)
                had_objects: JsonObject = _obj(had.get("objects"), f"slide {p.object_id}: objects") if had is not None else {}
                theirs_rb = had_objects.get(old_main) if isinstance(old_main, str) else None
                jobs.setdefault(as_str(s["objectId"], "slide objectId"), []).append(refit.RefitJob(
                    key=f"slide {p.key}: {u.key}", slide=p.key, names=names,
                    text=main, pictures=tuple(pics), own=frozenset(own),
                    doomed=frozenset(w.doomed),
                    theirs=None if theirs_rb is None else as_object(theirs_rb, f"read-back {old_main}")))
        return jobs

    def refit(self, work: Work, theirs: JsonMap, created: JsonMap, rev: str) -> str:
        """The recreated boxes fitted to the words written into them (`refit`, docs/project-notes.md
        "Merged text into recreated boxes"): formula pictures back over their holes, the box and a
        block panel under it as tall as the merged text needs. What moved is recorded for the base
        (`self.reshaped`, `refit.reshape_base`), and said in the report (`self.refit_moves`, report
        `refit`): which object moved how far in the converter's frame - the place the person's
        carried geometry then stands on, which the loss oracle holds the sync to."""
        self.reshaped = {}
        jobs = self.refit_jobs(work, theirs, created)
        if not jobs:
            return rev
        final = snapshot.read_presentation(self.read())
        fin = _by_id(final, "final")
        pre = _by_id(created, "created")
        before = _by_id(theirs, "theirs")
        page = final.get("page_size")
        page_size = None if page is None else _nums(page, "page_size")
        reqs: list[JsonObject] = []
        reshaped: dict[str, refit.Reshape] = {}
        moves: list[refit.Moved] = []
        for sid, js in jobs.items():
            if sid not in fin:
                continue
            r, shaped, warnings = refit.plan(js, pre[sid], fin[sid], page_size, before.get(sid))
            if r:
                reqs += r + [BREAK]
            reshaped.update(shaped)
            self.warnings += warnings
            moves += refit.moves(js, shaped, pre[sid], fin[sid])
        if reqs:
            rev = self.send("refit", reqs, rev)
            self.reshaped = reshaped
            self.refit_moves = [refit.moved_json(m) for m in moves]
        return rev

    def warn_about_folded_hiders(self, work: Work, now: JsonMap) -> None:
        """Words this sync covered and could not uncover: `folded_hiders` says why, and nothing else
        in the report would mention it, since nothing was deleted and every write went through."""
        live = _by_id(now, "now")
        for w in work.slides:
            p = w.plan
            if not isinstance(p, merge.UpdateSlide):
                continue  # (only an updated slide has objects made among the deck's)
            s = live.get(p.object_id)
            if not w.objects or s is None:
                continue
            b = _slides(self.base, "base")[p.base]
            ours = {o for el in as_objects(b["elements"], "base slide elements")
                    for o in _strs(el.get("objects"), "element objects")} | set(_strs(b.get("groups"), "base slide groups"))
            made = {x for oids in w.objects.values() for x in oids} | set(w.groups)
            shown = _objects(s, f"slide {p.object_id}")
            for text, _ in folded_hiders(s, made, ours, w.doomed):
                words = _text(as_object(shown[text], f"read-back {text}").get("text"),
                              "read-back text").strip().replace("\n", " ")[:40]
                self.warnings.append(
                    f"slide {b['key']}: {words!r} is now under a shape the source redrew, which stands in a "
                    "group you made: its page element cannot be restacked without moving the rest of that "
                    "group, so the source's order could not be followed. Ungroup it, or move the shape, "
                    "to read the words again")

    @staticmethod
    def _by_the_source(desired: list[str], oldtop: Mapping[str, str], tops: Mapping[str, str], keys: Sequence[str],
                       base_order: Sequence[str], stands_now: Mapping[str, str] | None,
                       stands_before: Mapping[str, str] | None) -> None:
        """The source's own elements take the source's order among themselves, in the places they
        hold on the page, whether this sync rewrote them or kept them - but only where the deck still
        has them in the order the base does.

        Between two converter elements the deck's order is not an edit anybody made: it is whatever
        the last conversion drew, and this conversion may draw them the other way round (a label that
        moved brings another frame's elements onto this slide). Keeping it there put a panel the
        source now draws *under* a text back on top of it, hiding words somebody could read
        (`loss_oracle.text_hidden`, converted seed 610106 at chain 10). Where the deck's order is
        *not* the base's, somebody restacked and that survives untouched - asked of the whole set at
        once, since a z-order edit is the one deck edit `merge.deck_edits` cannot see, so there is
        nothing finer to go on.

        The elements this sync *keeps* count as much as the ones it rewrites - the same reason
        `Sync.zrank` ranks a group's kept children - or a slide with one rewritten element has
        nothing to be ordered against and the rule never fires: a panel the source draws under a text
        it did not touch grew over it and stayed on top (converted seed 1500512 at chain 10, on a
        slide the person had ungrouped, so every element stood on the page).

        What is ordered are the *page elements* (`stands_now` / `stands_before`, `drawn_order`'s
        second answer; None: every object stands for itself), and a converter group - one the base
        itself draws on the page - stands for the elements it carries, the first the source draws
        among them, blocks being made of consecutive elements. Asking it of the objects alone left
        out everything inside a group, so a block's panel could not be ordered against a table beside
        it however the source drew them (converted seed 1300381 at chain 6). A group the *person*
        made stands for nobody: it is not in the base's order, restacking it would move everything
        else they put in there, and that is the one shape of this the sync answers by talking
        (`warn_about_folded_hiders`)."""
        rank = {k: i for i, k in enumerate(keys)}
        page = set(base_order)

        def stands(oid: str, where: Mapping[str, str] | None) -> str:
            t = where.get(oid, oid) if where else oid
            return t if t == oid or t in page else oid

        at: dict[str, str] = {}
        was: dict[str, str] = {}
        for k in sorted((k for k in oldtop if k in rank), key=lambda k: rank[k]):
            el = stands(tops.get(k) or oldtop[k], stands_now)
            if el not in at:                # the first element the source draws in there speaks
                at[el] = k
                was[el] = stands(oldtop[k], stands_before)
        here = [oid for oid in desired if oid in at]
        if len(here) < 2 or any(was[oid] not in base_order for oid in here):
            return
        if here != sorted(here, key=lambda oid: base_order.index(was[oid])):
            return                                  # the person restacked: their order stands
        slots = [i for i, oid in enumerate(desired) if oid in at]
        for i, oid in zip(slots, sorted(here, key=lambda o: rank[at[o]])):
            desired[i] = oid

    def restack(self, w: Mapping[str, object], before: JsonMap, now: JsonMap) -> list[JsonObject]:
        """`restack_of` over a slide's work as JSON: {"plan" (as `merge.plan_merge` writes it),
        "doomed", "tops"} (the offline fuzz's replay, `fuzz_sync._stacked`)."""
        p = updated(slide_plan_of(google_types.json_object(w["plan"], "plan"), "plan"))
        tops = google_types.json_object(w["tops"], "tops")
        return self.restack_of(p.base, p.ours, slide_units(p), _str_set(w.get("doomed"), "doomed"),
                               {k: as_str(v, f"tops[{k}]") for k, v in tops.items()}, before, now)

    def restack_of(self, base: int, ours: int, units: Sequence[merge.PlannedUnit], doomed: Collection[str],
                   tops: Mapping[str, str], before: JsonMap, now: JsonMap) -> list[JsonObject]:
        """BRING_TO_FRONT so recreated elements take their old place in the z-order and new
        ones follow their predecessor in the source. The objects the cleanup phase will delete are
        left out: they are still on the slide, under their replacements, until then. `base`, `ours`:
        the slide's index in the base and in the new conversion; `tops`: `SlideWork.tops`."""
        b = _slides(self.base, "base")[base]
        o = _slides(self.ours, "ours")[ours]
        bunits = merge.units(as_objects(b["elements"], "base slide elements"))
        now_order = _strs(now["order"], "now.order")
        top_now = {oid for oid in now_order if oid not in doomed}
        swap: dict[str, str] = {}
        added: list[str] = []
        oldtop: dict[str, str] = {}
        for u in units:
            d = u.decision
            match d:
                case merge.Recreate():
                    old = merge.unit_top(bunits[u.key], before)
                    new = tops.get(u.key)
                    if old and new:
                        swap[old] = new
                        oldtop[u.key] = old
                case merge.CreateUnit() if u.key in tops:
                    added.append(u.key)
                case merge.DeleteUnit():
                    pass
                case merge.AdoptObject() | merge.CreateUnit() | merge.GoneUnit() | merge.KeepRemoved() | \
                        merge.KeptJoined() | merge.KeepUnit() | merge.AdoptUnit() | merge.MoveUnit():
                    # kept (or gone from the deck, or a creation that made nothing): the deck's own
                    # object, where there is one, stands for it
                    if u.key in bunits and (old := merge.unit_top(bunits[u.key], before)):
                        oldtop[u.key] = old
                case _:
                    assert_never(d)
        desired: list[str] = []
        for oid in _strs(before["order"], "before.order"):
            oid = swap.get(oid, oid)
            if oid in top_now and oid not in desired:
                desired.append(oid)
        keys = [as_str(e["key"], "element key") for e in as_objects(o["elements"], "ours slide elements")]
        shown = _obj(now.get("objects"), "now.objects")
        stands_now = drawn_order(shown, _strs(now.get("order"), "now.order"))[1]
        self._by_the_source(desired, oldtop, tops, keys, _strs(b.get("order"), "base slide order"), stands_now,
                            drawn_order(_obj(before.get("objects"), "before.objects"),
                                        _strs(before.get("order"), "before.order"))[1])

        def placed(k: str) -> str | None:
            return tops.get(k) or (merge.unit_top(bunits[k], now) if k in bunits else None)

        # An element with no place in the deck's order takes the place the source gives it. That is
        # one this sync created - and also one a group this rewrite dissolved has freed onto the
        # page: its old top stood *inside* that group, so nothing of the deck's held a slot for it,
        # and it landed on top of everything, past the ceiling below and past the guard that keeps a
        # panel under words only the deck has. The offline campaign cannot reach this, its applier
        # modelling the outcome (`fuzz_world._drop_lonely_groups` gives the freed child the group's
        # own slot), so the rule is pinned by a test of its own.
        homeless = [k for k in keys if k not in added and (oid := placed(k))
                    and oid in top_now and oid not in desired]
        for ukey in sorted(added + homeless, key=keys.index):
            new = placed(ukey)
            if new is None or new not in top_now or new in desired:
                continue
            i = keys.index(ukey)
            pos = 0
            for k in reversed(keys[:i]):
                prev = placed(k)
                if prev is not None and prev in desired:
                    pos = desired.index(prev) + 1
                    break
            # ... but never above an element the source draws above it. A recreated element keeps the
            # deck's place, a new one follows the source, and where those two orders disagree - a
            # frame the source rewrote, or a label that moved onto another slide - a new opaque panel
            # would land on top of text the source puts above it, and nothing is deleted for any
            # other check to see (`loss_oracle.text_hidden`).
            for k in keys[i + 1:]:
                nxt = placed(k)
                if nxt is not None and nxt in desired:
                    pos = min(pos, desired.index(nxt))
                    break
            desired.insert(pos, new)
        # ... and nothing this sync wrote ends up above words only the deck has. The source's order
        # places an element among the source's own; about an element the source dropped and the
        # deck's edits kept alive, or one the person drew themselves, it says nothing at all. A
        # panel that grew over such a text hides work nobody can get back, and the price of going
        # under it is z-order, so that is the way round to be wrong (`loss_oracle.text_hidden`).
        #
        # What moves is the page **element** the new shape is drawn inside - a converter group this
        # rewrite rebuilt, most often the block the panel belongs to - because that is what the page
        # order holds. Asking it of the object alone reached nothing at all when the panel was in a
        # block: its id is in no page order, so the loop looked at an empty list and a panel the
        # source had just grown covered a text the source no longer has (converted seed 2300025 at
        # chain 12). Which words are the deck's own is asked of each **text**, not of the page
        # element holding it: a group the person made may hold one of their text boxes beside one of
        # the converter's, and reading the group as the source's let a created panel cover their box
        # inside it (converted seed 5200496 at chain 12).
        # ... nor above words the source draws above it and the page order could not carry. One page
        # element stands for every converter element inside it, and `_by_the_source` ranks it by the
        # first of them, so a group holding two of them with a panel drawn *between* is a place where
        # the source's order cannot be honoured at all: the group goes under the panel for the sake of
        # the text below it, taking the text above it with it. There the words are the source's own
        # and the last pass was told to keep quiet about them, so a created panel covered a text still
        # on the slide (converted seed 8300231 at chain 11, a person's group around two of the
        # converter's texts). A text the source draws *above* this shape is therefore spoken for only
        # while its page element is not standing below the shape on another text's account.
        keyset = set(keys)
        rank = {k: i for i, k in enumerate(keys)}
        at_rank: dict[str, int] = {}                # object -> where the source draws it
        for k in keys:
            if (t := placed(k)) is not None:
                at_rank[t] = min(at_rank.get(t, rank[k]), rank[k])
        for el in as_objects(b["elements"], "base slide elements"):
            ek = el["key"]
            if isinstance(ek, str) and ek in keyset:
                for oid in _strs(el.get("objects"), "element objects"):
                    at_rank[oid] = min(at_rank.get(oid, rank[ek]), rank[ek])
        stand_rank: dict[str, int] = {}             # page element -> the first of them it stands for
        for oid, r in at_rank.items():
            el_id = stands_now.get(oid, oid)
            stand_rank[el_id] = min(stand_rank.get(el_id, r), r)
        made_rank: dict[str, int] = {}
        for k, t in tops.items():
            if k in rank:
                made_rank[t] = min(made_rank.get(t, rank[k]), rank[k])
        for made in dict.fromkeys(tops.values()):
            oid = stands_now.get(made, made)
            if oid not in desired:
                continue
            mr = made_rank.get(made)
            drawn = {x for x, xr in at_rank.items()
                     if mr is None or xr <= mr or stand_rank.get(stands_now.get(x, x), mr) >= mr}
            i = desired.index(oid)
            for j, other in enumerate(desired[:i]):
                if would_hide(shown, made, other, drawn):
                    desired.insert(j, desired.pop(i))
                    break
        desired += [x for x in now_order if x not in desired and x not in doomed]
        order_now = [x for x in now_order if x not in doomed]
        k = 0
        while k < len(desired) and k < len(order_now) and desired[k] == order_now[k]:
            k += 1
        return [front_request(x) for x in desired[k:]]

    @staticmethod
    def move_requests(units: Sequence[JsonMap], bunits: Mapping[str, Sequence[JsonMap]], read: JsonMap,
                      scale: float) -> list[JsonObject]:
        """`move_requests_of` over units as `merge.plan_merge` writes them (the offline fuzz's replay)."""
        return Sync.move_requests_of([planned_unit_of(u, f"unit {n}") for n, u in enumerate(units)], bunits, read, scale)

    @staticmethod
    def move_requests_of(units: Sequence[merge.PlannedUnit], bunits: Mapping[str, Sequence[JsonMap]], read: JsonMap,
                         scale: float) -> list[JsonObject]:
        """The source's move written onto the deck's own objects. Every *root* of the unit takes the
        step: normally that is the converter's group, which carries its children, but when the person
        has taken the group apart the roots are the text box and each picture anchored to it, and
        writing on the first of them alone would leave the others where the converter put them while
        the report calls the move applied (`override_requests` had the same assumption, live fuzz seed
        903). `merge.unit_shift` has already asked that one step fits every member of the unit, so the
        same step is what each root wants."""
        reqs: list[JsonObject] = []
        for u in units:
            if isinstance(u.decision, merge.MoveUnit):
                dx, dy = (v * scale for v in u.decision.delta)
                for oid in merge.unit_roots(bunits.get(u.key, []), read):
                    reqs.append(matrix_request(oid, [1, 0, 0, 1, dx, dy]))
        return reqs

    @staticmethod
    def _unit_oids(w: SlideWork, ounits: Mapping[str, Sequence[JsonMap]], ukey: str,
                   index: Mapping[str, int]) -> list[str]:
        """The new objects of a recreated unit: its main object and the pictures anchored to it."""
        oids: list[str] = []
        for m in ounits.get(ukey, []):
            mk = m["key"]
            oid = w.new_oid.get(index[mk]) if isinstance(mk, str) and mk in index else None
            if oid and oid not in oids:
                oids.append(oid)
        return oids

    def override_requests(self, work: Work, theirs: JsonMap, now: JsonMap, raw_before: Mapping[str, PageElement],
                          raw_now: Mapping[str, PageElement]) -> list[JsonObject]:
        """Deck edits re-applied to recreated elements: geometry, merged text, styles. raw_before /
        raw_now: objectId -> page element of the deck before sync and now (for run styles)."""
        before = _by_id(theirs, "theirs")
        after = _by_id(now, "now")
        reqs: list[JsonObject] = []
        for w in work.slides:
            p = w.plan
            if not isinstance(p, merge.UpdateSlide):
                continue  # (only an updated slide has recreated units)
            b = _slides(self.base, "base")[p.base]
            bunits = merge.units(as_objects(b["elements"], "base slide elements"))
            o_elements = as_objects(_slides(self.ours, "ours")[p.ours]["elements"], "ours slide elements")
            ounits = merge.units(o_elements)
            index = {as_str(e["key"], "element key"): k for k, e in enumerate(o_elements)}
            t_read, n_read = before[p.object_id], after[p.object_id]
            t_objects, n_objects = _objects(t_read, f"slide {p.object_id}"), _objects(n_read, f"slide {p.object_id}")
            for u in p.units:
                d = u.decision
                if not isinstance(d, merge.Recreate) or not merge.overrides_json(d.overrides):
                    continue
                ov = d.overrides
                i = index[u.key]
                main = w.new_oid[i]
                top = w.tops.get(u.key, main)
                anchor = bunits[u.key][0]
                old_main = as_optional_str(anchor["main"], "base element main")
                final_text: str | None = None
                if isinstance(ov.text, merge.TableOverride) and main in n_objects:
                    new_rb = as_object(n_objects[main], f"read-back {main}")
                    new_text = as_optional_str(new_rb.get("text"), f"read-back {main}: text")
                    table = new_rb.get("table")
                    dims = None if table is None else [as_int(x, "read-back table") for x in as_array(table, "read-back table")]
                    cells = merge.table_merge(ov.text.base, new_text or "", ov.text.theirs, dims, ov.text.dims)
                    current = merge.table_grid(new_text, dims)
                    if cells is None or current is None:
                        self.warnings.append(f"slide {p.key}: {u.key}: deck cell edits clash with the new table; not re-applied")
                    else:
                        for r, (crow, mrow) in enumerate(zip(current, cells[0])):
                            for c, (now_cell, want) in enumerate(zip(crow, mrow)):
                                if want != now_cell:  # the cell text ends in a newline Slides keeps
                                    reqs += merge.text_edit_requests(main, now_cell + "\n", want + "\n",
                                                                     {"rowIndex": r, "columnIndex": c})
                elif isinstance(ov.text, merge.TextOverride) and main in n_objects:
                    current_text = _text(as_object(n_objects[main], f"read-back {main}").get("text"),
                                         f"read-back {main}: text")
                    merged, _, safe = merge.text_merge(ov.text.base, current_text, ov.text.theirs, ov.text.take)
                    if not safe:
                        self.warnings.append(f"slide {p.key}: {u.key}: deck text edits clash with the new text; not re-applied")
                    else:
                        if not merged.endswith("\n"):
                            merged += "\n"
                        reqs += merge.text_edit_requests(main, current_text, merged, None)
                        final_text = merged
                if ov.text_style is not None:
                    new_raw = raw_now.get(main)
                    locations: list[google_types.SlidesTableCellLocation] | None = None
                    if new_raw is not None and "table" in new_raw:
                        rows = google_types.parts(new_raw["table"].get("tableRows"), "table.tableRows")
                        locations = [{"rowIndex": r, "columnIndex": c} for r, row in enumerate(rows)
                                     for c, _ in enumerate(google_types.parts(row.get("tableCells"), "tableRow.tableCells"))]
                    reqs += style_override_requests(main, {"runs": ov.text_style.runs, "paragraphs": ov.text_style.paragraphs},
                                                    locations)
                    if ov.text_style.ranges:
                        readback = _obj(anchor.get("readback"), "base element readback")
                        had = _obj(readback.get(old_main), f"base readback {old_main}") if old_main is not None else {}
                        base_styles = _objs(had.get("text_styles"), "text_styles")
                        if old_main is not None and old_main in raw_before and new_raw:
                            reqs += style_range_requests(main, raw_before[old_main], new_raw, base_styles, final_text)
                        else:
                            self.warnings.append(f"slide {p.key}: {u.key}: the deck's word styles could not be re-applied")
                if ov.shape_style:
                    reqs += shape_style_requests(main, ov.shape_style)
                if ov.geometry and top in n_objects and not isinstance(w.in_place.get(i), TableFill):
                    # (a table refilled in place is still where, and as large as, the deck has it)
                    old_top = merge.unit_top(bunits[u.key], t_read) or old_main
                    base_rb: JsonObject = {}
                    for m in bunits[u.key]:
                        readbacks = _obj(m.get("readback"), "base element readback")
                        if old_top is not None and old_top in readbacks:
                            base_rb = _obj(readbacks[old_top], f"base readback {old_top}")
                            break
                    theirs_rb: JsonObject = _obj(t_objects.get(old_top), f"read-back {old_top}") if old_top is not None else {}
                    new_rb = as_object(n_objects[top], f"read-back {top}")
                    if not base_rb or not theirs_rb:
                        continue
                    step = carried(base_rb, theirs_rb, new_rb)  # (merge: mode "delta", also when both moved it)
                    if any(abs(x - y) > 1e-4 for x, y in zip(step, [1, 0, 0, 1, 0, 0])):
                        # A group carries its children, so one request on it moves the whole unit.
                        # Without one - the person took this unit's group apart, and a recreation
                        # does not put it back - `top` is the main object alone, and the unit's
                        # anchored pictures need the step themselves or they stay at the
                        # converter's boxes while the report says the person's move was kept
                        # (live fuzz seed 903 at chain depth 8: a formula picture back 15 pt above
                        # the line it belongs to). `merge.geometry_writable` has already asked that
                        # one step fits every member, so the same step is what each of them wants.
                        for oid in ([top] if top != main else self._unit_oids(w, ounits, u.key, index)):
                            reqs.append(matrix_request(oid, step))
        return reqs

    # ---- the new base

    def new_base(self, result: RunResult) -> JsonObject:
        work, theirs = result.work, result.theirs
        now = _by_id(self.created, "created")
        base_slides, ours_slides = _slides(self.base, "base"), _slides(self.ours, "ours")
        entries: dict[str, JsonObject] = {}
        for w in work.slides:
            p = w.plan
            match p:
                case merge.DeleteSlide():
                    continue
                case merge.GoneSlide():
                    # The deck deleted this slide while the source still has the frame. The entry
                    # holds the frame's key and its place in the order (`base_order`); what it says
                    # follows the source, or a frame whose label or title changed after the deletion
                    # stops looking like this entry and comes back as a new slide.
                    b, o = base_slides[p.base], ours_slides[p.ours]
                    entries[f"gone:{p.key}"] = {**b, **{k: o.get(k) for k in ("label", "title", "text", "page")},
                                                "removed": False}   # the source has this frame
                    continue
                case merge.KeepRemovedSlide():
                    # The source dropped this frame and the deck's own edits keep the slide alive. Its
                    # label goes with the frame: the source may put it on another frame tomorrow, and
                    # a slide the source no longer describes must not hold a live label hostage. And
                    # the entry says the source dropped it (`removed`), or it competes with the slide
                    # the frame really lives on for the frame that looks most like it - see
                    # `align_slides`.
                    entries[p.object_id or f"gone:{p.key}"] = {**base_slides[p.base], "label": None, "removed": True}
                    continue
                case merge.HoldSlide():
                    # Nothing was written here (`merge.hold_slide`), and the entry must not say
                    # otherwise. An entry takes its label, title and words from the source, so a held
                    # slide recorded the usual way would read next time as a change already made -
                    # and the edit this sync held back would be gone for good instead of waiting for
                    # the labels to be put right.
                    b = base_slides[p.base]
                    entries[as_str(b["objectId"], "base slide objectId")] = dict(b)
                    continue
                case merge.CreateSlide() | merge.UpdateSlide():
                    pass
                case _:
                    assert_never(p)
            o = ours_slides[p.ours]
            o_elements = as_objects(o["elements"], "ours slide elements")
            sid = w.sid
            if sid is None:
                raise ValueError(f"slide {p.key}: written on no slide")
            read = now.get(sid)
            entry: JsonObject = {k: v for k, v in o.items() if k != "elements"}
            elements: list[JsonObject] = []
            if isinstance(p, merge.CreateSlide):
                for i, e in enumerate(o_elements):
                    made: Sequence[str | None] = w.objects.get(i, [w.new_oid.get(i)])
                    elements.append(self._element(e, made, read))
                groups: list[Json] = [g for g in w.groups]
                no_order: list[Json] = []
                entry.update(objectId=sid, layoutObjectId=read["layoutObjectId"] if read else None,
                             background_readback=read["background"] if read else None,
                             notes_readback=read["notes"] if read else "", groups=groups,
                             order=read["order"] if read else no_order)
            else:
                b = base_slides[p.base]
                bunits, ounits = merge.units(as_objects(b["elements"], "base slide elements")), merge.units(o_elements)
                index = {as_str(e["key"], "element key"): k for k, e in enumerate(o_elements)}
                none: list[JsonObject] = []
                read_objects = _obj(read.get("objects"), f"slide {sid}: objects") if read else {}
                for u in p.units:
                    d = u.decision
                    match d:
                        case merge.CreateUnit() | merge.Recreate():
                            for mk in u.ours_members:
                                i = index[mk]
                                made_here: Sequence[str | None] = w.objects.get(i, [w.new_oid[i]])
                                elements.append(self._element(o_elements[i], made_here, read))
                                live = w.in_place.get(i)
                                if isinstance(live, TableFill):  # (still the .pptx's table: `table_refill`)
                                    margins: list[Json] = [_json_nums(m) for m in live.margins]
                                    elements[-1]["table_margins"] = margins
                        case merge.MoveUnit():
                            for m in ounits[u.key]:
                                old = next((x for x in bunits[u.key] if x["key"] == m["key"]), None)
                                if old is None:
                                    continue
                                rb: JsonObject = {}
                                for oid, v in as_object(old["readback"], "base element readback").items():
                                    if read and oid in read_objects:
                                        shown = as_object(read_objects[oid], f"read-back {oid}")
                                        rb[oid] = {**as_object(v, f"base readback {oid}"),
                                                   **{k: shown[k] for k in ("box", "transform")}}
                                    else:
                                        rb[oid] = v
                                elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb,
                                                 **{k: old[k] for k in ("table_margins",) if k in old}})
                        case merge.AdoptObject():
                            # the deck's own object is what the source now draws (a picture pull put in the source)
                            for mk in u.ours_members:
                                elements.append(self._element(o_elements[index[mk]], [d.object_id], read))
                        case merge.AdoptUnit():
                            # the source now says what the deck shows: ours IR, the deck's version of those fields
                            fields: dict[str, tuple[str, ...]] = {
                                "text": ("text",), "geometry": ("box", "transform", "size"),
                                "image": ("image", "box", "transform", "size")}
                            for m in ounits[u.key]:
                                old = next((x for x in bunits[u.key] if x["key"] == m["key"]), None)
                                if old is None:
                                    continue
                                rb = as_object(copy.deepcopy(old["readback"]), "base element readback")
                                old_main = old.get("main")
                                live_obj = read_objects.get(old_main) if isinstance(old_main, str) else None
                                if live_obj and isinstance(old_main, str) and old_main in rb:
                                    target = as_object(rb[old_main], f"base readback {old_main}")
                                    shown = as_object(live_obj, f"read-back {old_main}")
                                    for f in d.adopt:
                                        target.update({k: shown[k] for k in fields.get(f, ()) if k in shown})
                                elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb,
                                                 **{k: old[k] for k in ("table_margins",) if k in old}})
                        case merge.KeepRemoved():
                            # A unit kept because the source dropped it says so, or the next sync reads
                            # a deck that no longer differs from the base and deletes it (merge.plan_unit).
                            elements += [{**m, "removed": True} for m in bunits.get(u.key, none)]
                        case merge.KeptJoined() | merge.KeepUnit():
                            elements += bunits.get(u.key, none)
                        case merge.DeleteUnit() | merge.GoneUnit():
                            pass
                        case _:
                            assert_never(d)
                background = new_background(p)
                bg_conflict = b.get("background") != o.get("background") and not background
                notes_kept = (b.get("notes") or "") != (o.get("notes") or "") and p.notes is None
                order: Json = (_json_strs([x for x in _strs(read["order"], "read.order") if x not in w.doomed])
                               if read else _or_new_list(b, "order"))
                entry.update(objectId=sid, layoutObjectId=b.get("layoutObjectId"), groups=_or_new_list(b, "groups"),
                             order=order)
                if b.get("left_alone"):
                    # the person's own unpaired objects stay theirs at every generation
                    # (`adopt_sync.build_base`, `merge.slide_touched`)
                    entry["left_alone"] = list(as_array(b["left_alone"], "base slide left_alone"))
                if background:
                    entry["background_readback"] = read["background"] if read else None
                else:
                    entry["background_readback"] = b.get("background_readback")
                    if bg_conflict:
                        entry["background"] = b.get("background")
                if p.notes is not None:
                    entry["notes_readback"] = o.get("notes") or ""
                else:
                    entry["notes_readback"] = b["notes_readback"] if "notes_readback" in b else ""
                    if notes_kept:
                        entry["notes"] = b.get("notes")
            # (the list `keys_the_source_took` gives back, as the base's JSON takes it)
            kept_elements: list[Json] = [dict(e) for e in merge.keys_the_source_took(elements)]
            entry["elements"] = kept_elements
            entries[sid] = entry
        live_order = [as_str(s["objectId"], "created slide objectId") for s in _slides(self.created, "created")]
        slides = [entries.pop(sid) for sid in base_order_of(placed_slides(work), live_order)
                  if sid in entries] + list(entries.values())
        # what `refit` moved or grew is the converter's doing, not the person's (`refit.reshape_base`)
        from .refit import reshape_base
        slides = reshape_base(slides, self.reshaped)
        new_slides: list[Json] = [s for s in slides]
        new: JsonObject = {**self.base, "generation": base_generation(self.base) + 1, "revisionId": self.final_revision,
                           "source": snapshot.source_info(self.source), "slides": new_slides}
        pinned = set(self.theme_plan.pinned) if self.theme_plan is not None else set[str]()
        if pinned:
            # The style theme_sync pinned onto placeholders (`inherited_pins`) looks the same and is
            # converter output: where the person had not restyled the object, the base takes it, or
            # the next sync reads the pins as the person's style edit.
            keys = ("text_styles", "paragraph_styles", "run_spans", "text_style_hash")
            pre = {oid: as_object(o, oid) for s in _slides(theirs, "theirs")
                   for oid, o in _obj(s.get("objects"), "objects").items() if oid in pinned}
            post = {oid: as_object(o, oid) for s in _slides(self.created, "created")
                    for oid, o in _obj(s.get("objects"), "objects").items() if oid in pinned}
            for entry in slides:
                elements = _objs(entry.get("elements"), "elements")
                for i, el in enumerate(elements):   # (copies: a kept element is the old base's own dict)
                    rbs = _obj(el.get("readback"), "readback")
                    for oid in pinned & set(rbs):
                        rb = as_object(rbs[oid], oid)
                        if oid in pre and oid in post and all(rb.get(k) == pre[oid].get(k) for k in keys):
                            rbs = {**rbs, oid: {**rb, **{k: post[oid][k] for k in keys if k in post[oid]}}}
                            elements[i] = {**el, "readback": rbs}
                kept: list[Json] = [e for e in elements]
                entry["elements"] = kept
        if self.base.get("theme") and self.theme_side is not None:
            from . import theme_sync
            new["master_background"] = self.master_key()
            # (a copy: the new base shares nothing with the old one)
            rec = theme_sync.theme_record(copy.deepcopy(self.base["theme"]), "the sync base's theme")
            written = self.theme_plan.written if self.theme_plan is not None else {}
            new["theme"] = theme_sync.theme_json(theme_sync.new_record(rec, self.theme_side, written, self.raw_after))
        return new

    def _element(self, e: JsonMap, oids: Sequence[str | None], read: JsonMap | None) -> JsonObject:
        # (a created slide's element nothing was made for is recorded with [None]: as it always was)
        objects = _obj(read.get("objects"), "read.objects") if read else {}
        made: list[Json] = [oid for oid in oids]
        return {**e, "objects": made, "main": oids[0] if oids else None,
                "readback": {oid: objects[oid] for oid in oids if oid is not None and oid in objects}}


def _or_new_list(m: JsonMap, key: str) -> Json:
    """`m.get(key, [])`: the value where there is one (even None), else a new empty list."""
    if key in m:
        return m[key]
    empty: list[Json] = []
    return empty


def base_order(mplan: JsonMap, by_plan: Mapping[int, JsonMap], live: Sequence[str]) -> list[str]:
    """`base_order_of` over a plan as JSON (`merge.merge_plan_json`) and plan id -> {"sid": ...}."""
    placed = [(as_int(p["ours"], "plan slide ours"),
               f"gone:{as_str(p['key'], 'plan slide key')}" if p["action"] == "gone"
               else as_str(by_plan[id(p)]["sid"], "plan slide sid"))
              for p in as_objects(mplan["slides"], "plan slides") if p["action"] in ("update", "create", "gone")]
    return base_order_of(placed, live)


def placed_slides(work: Work) -> list[tuple[int, str]]:
    """The slides of the new conversion the new base holds, as (frame index, entry id): the
    updated, held and created ones by the slide they are written on, a deleted one by `gone:<key>`."""
    placed: list[tuple[int, str]] = []
    for w in work.slides:
        p = w.plan
        match p:
            case merge.GoneSlide():
                placed.append((p.ours, f"gone:{p.key}"))
            case merge.CreateSlide() | merge.UpdateSlide() | merge.HoldSlide():
                if w.sid is None:
                    raise ValueError(f"slide {p.key}: written on no slide")
                placed.append((p.ours, w.sid))
            case merge.KeepRemovedSlide() | merge.DeleteSlide():
                pass
            case _:
                assert_never(p)
    return placed


def base_order_of(placed: Sequence[tuple[int, str]], live: Sequence[str]) -> list[str]:
    """Slide ids of the new base in the source's order (the base is converter output: a slide
    order the deck chose must keep differing from it, or the next sync would undo it). Slides kept
    though the source removed them stay after their live predecessor. `placed`: (frame index,
    entry id) of the slides the new conversion has (`placed_slides`), in plan order.

    A slide the person deleted while the source still has it (`gone`) has no live id, but it keeps
    its place here: the next conversion pairs its frames with this base in order
    (`identity.align_slides`), so an entry at the end takes the identity of every frame that
    followed it - they look new and get created again."""
    order = [sid for _, sid in sorted(placed, key=lambda x: x[0])]
    for k, sid in enumerate(live):
        if sid in order:
            continue
        prev = next((live[q] for q in range(k - 1, -1, -1) if live[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
    return order


def stand_in_request(oid: str, sid: str, preset: str) -> JsonObject:
    """A plain shape of `preset` (a template key's first part) standing in for a template no live
    object gives: created at 3,000,000 EMU, the size Slides stores every shape at."""
    size = emu_size(STAND_IN, STAND_IN)
    transform: google_types.AffineTransform = {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0,
                                               "unit": "EMU"}
    if preset == "BENT_CONNECTOR":
        return google_types.slides_json({"createLine": {"objectId": oid, "lineCategory": "BENT", "elementProperties": {
            "pageObjectId": sid, "size": size, "transform": transform}}})
    return google_types.slides_json({"createShape": {"objectId": oid, "shapeType": preset, "elementProperties": {
        "pageObjectId": sid, "size": size, "transform": transform}}})


# ---------------------------------------------------------------- reports

@dataclass(frozen=True, kw_only=True)
class SlidesSaid:
    """The report's `slides` as the merge wrote them (`merge.report_json`): what happened to whole
    slides - created, deleted, moved, kept though the source dropped them, held back, added in the deck."""
    created: list[Json]
    deleted: list[Json]
    moved: list[Json]
    kept: list[JsonObject]
    held: list[JsonObject]
    user_added: list[JsonObject]


@dataclass(frozen=True, kw_only=True)
class SyncReport:
    """What a sync did and did not do: the merge's report of the slides with theme sync's changes and
    conflicts after them (`report_of`), then what the run adds - every warning, what emit could
    only make a picture of (`contained`; None: nothing, and the key is left out), the base's older
    forms (`base_forms`, the same), the person's objects the source now runs over (`overruns`), what
    `refit` moved. Its JSON is `sync_report_json`'s alone: the report files, the CLI's lines and the
    agent's `data` all read it."""
    applied: list[JsonObject]
    overrides: list[JsonObject]
    conflicts: list[JsonObject]
    resolved: list[JsonObject]
    converged: list[JsonObject]
    user_objects: list[JsonObject]
    slides: SlidesSaid
    warnings: list[str]
    contained: list[Json] | None
    base_forms: list[JsonObject] | None
    overruns: list[Json]
    refit: list[Json]


@dataclass(frozen=True, kw_only=True)
class SyncResult:
    """A sync, done (`sync`): which PDF went into which deck, whether it was a dry run, the base it
    merged against (`base_from`: "drive" or "local"; `generation`: the one it wrote, or read when
    it wrote none), the overlay steps it converted, how often it planned (`attempts`), the requests
    it sent per phase, how long it took, its report, each slide's action (`plan_actions`) and the
    recovery note a caller added (`__main__.add_recovery`; None: none). `sync_result_json` is its
    JSON, and `write_reports` the one place it is written."""
    pdf: str
    presentation_id: str
    url: str
    dry_run: bool
    base_from: str
    generation: int
    overlays: str
    attempts: int
    requests: dict[str, int]
    seconds: float
    report: SyncReport
    actions: list[Json]
    recovery: JsonObject | None


def _jsons(xs: Sequence[JsonObject]) -> list[Json]:
    return [x for x in xs]


def slides_said_json(s: SlidesSaid) -> JsonObject:
    return {"created": s.created, "deleted": s.deleted, "moved": s.moved, "kept": _jsons(s.kept),
            "held": _jsons(s.held), "user_added": _jsons(s.user_added)}


def sync_report_json(r: SyncReport) -> JsonObject:
    """The report as the files and the callers have always had it, key for key and in that order."""
    out: JsonObject = {"applied": _jsons(r.applied), "overrides": _jsons(r.overrides), "conflicts": _jsons(r.conflicts),
                       "resolved": _jsons(r.resolved), "converged": _jsons(r.converged),
                       "user_objects": _jsons(r.user_objects), "slides": slides_said_json(r.slides),
                       "warnings": _json_strs(r.warnings)}
    if r.contained is not None:
        out["contained"] = r.contained
    if r.base_forms is not None:
        out["base_forms"] = _jsons(r.base_forms)
    out["overruns"] = r.overruns
    out["refit"] = r.refit
    return out


def sync_result_json(r: SyncResult) -> JsonObject:
    """What `sync` answered as a dict, key for key: the report nested under `report`."""
    requests: JsonObject = {k: v for k, v in r.requests.items()}
    out: JsonObject = {"pdf": r.pdf, "presentationId": r.presentation_id, "url": r.url, "dry_run": r.dry_run,
                       "base_from": r.base_from, "generation": r.generation, "overlays": r.overlays,
                       "attempts": r.attempts, "requests": requests, "seconds": r.seconds,
                       "report": sync_report_json(r.report), "actions": r.actions}
    if r.recovery is not None:
        out["recovery"] = r.recovery
    return out


def report_of_merge(merged: JsonMap, theme_applied: Sequence[JsonObject], theme_conflicts: Sequence[JsonObject],
                    warnings: list[str], contained: list[Json] | None, base_forms: list[JsonObject] | None,
                    converged: Sequence[JsonObject], overruns: list[Json], refit_moves: list[Json]) -> SyncReport:
    """The sync report over the merge's (`merge.report_json`): theme sync's changes and conflicts
    after the slides', and what the run adds (`warnings`: every one, the merge's first;
    `converged`: those after the merge's)."""
    slides = as_object(merged["slides"], "report.slides")
    return SyncReport(
        applied=[*as_objects(merged["applied"], "report.applied"), *theme_applied],
        overrides=as_objects(merged["overrides"], "report.overrides"),
        conflicts=[*as_objects(merged["conflicts"], "report.conflicts"), *theme_conflicts],
        resolved=as_objects(merged["resolved"], "report.resolved"),
        converged=[*as_objects(merged["converged"], "report.converged"), *converged],
        user_objects=as_objects(merged["user_objects"], "report.user_objects"),
        slides=SlidesSaid(created=list(as_array(slides["created"], "report.slides.created")),
                          deleted=list(as_array(slides["deleted"], "report.slides.deleted")),
                          moved=list(as_array(slides["moved"], "report.slides.moved")),
                          kept=as_objects(slides["kept"], "report.slides.kept"),
                          held=as_objects(slides["held"], "report.slides.held"),
                          user_added=as_objects(slides["user_added"], "report.slides.user_added")),
        warnings=warnings, contained=contained, base_forms=base_forms, overruns=overruns, refit=refit_moves)


def _quote(value: Json) -> str:
    """One side of a conflict as a report shows it: a text on its own lines, anything else as the
    JSON these entries have always printed. Three versions of a paragraph one under the other is
    most of what a person opens a conflict report to see."""
    if not isinstance(value, str):
        return f"    {json.dumps(value, ensure_ascii=False)}"
    if not value.strip():
        return "    (nothing)"
    return "\n".join(f"    > {line}" if line.strip() else "    >"
                     for line in value.rstrip("\n").split("\n"))


def conflict_lines(c: JsonMap) -> str:
    """A conflict, its three sides under it, and - where the source's version can be written here
    instead - the one thing a person types to ask for that (`merge.Resolutions`)."""
    where = f"`{c['slide']}`" + (f" / `{c['element']}`" if c.get("element") else "")
    cid = c.get("id")
    out = [f"- {'`' + as_str(cid, 'conflict id') + '` ' if cid else ''}{where}: **{c['field']}**, {c['resolution']}",
           "  - base:", _quote(c["base"]), "  - source:", _quote(c["ours"]), "  - deck:", _quote(c["theirs"])]
    if c.get("takeable") and c["resolution"] != merge.TAKEN_SAYS:
        out.append(f"  - to write the source's version here instead: sync again with "
                   f"`--take-source {c['id']}`. What it writes over is the deck's version above.")
    return "\n".join(out)


def _section(lines: list[str], title: str, items: Sequence[T], fmt: Callable[[T], str]) -> None:
    lines.append(f"## {title} ({len(items)})")
    if items:
        lines.extend(fmt(x) for x in items)
    else:
        lines.append("none")
    lines.append("")


def _loc(x: JsonMap) -> str:
    return f"`{x['slide']}`" + (f" / `{x['element']}`" if x.get("element") else "")


def _fields(x: JsonMap) -> str:
    return ", ".join(_strs(x["fields"], "report entry fields"))


def _applied_line(x: JsonMap) -> str:
    return f"- {_loc(x)}: {_fields(x)}" + (f" ({x['how']})" if x.get("how") else "")


def _resolved_line(x: JsonMap) -> str:
    return (f"- `{x['id']}` {_loc(x)}: **{x['field']}**. The deck said, and this sync wrote over:\n"
            f"{_quote(x['was'])}")


def _refit_line(x: JsonMap) -> str:
    if x["what"] == "picture":
        shift = _nums(x["shift"], "refit shift")
        return f"- {_loc(x)}: the formula picture follows its hole, {shift[0]:+.1f} / {shift[1]:+.1f} pt"
    return f"- {_loc(x)}: the {x['what']} grown {_number(x['grown'], 'refit grown'):.1f} pt"


def _user_object_line(x: JsonMap) -> str:
    return f"- `{x['slide']}`: {x['objectId']}" + (f" (copy of {x['copy_of']})" if x.get("copy_of") else "")


def _or_none(xs: Sequence[Json]) -> str:
    return f"{xs}" if xs else "none"


def write_reports(out: Path, result: SyncResult) -> tuple[Path, Path]:
    """sync-report.json and sync-report.md (a dry run's under names of their own): the result's
    JSON with the report's parts at the top level (`slides_*` for its slides), and the same for a
    person to read."""
    folder = out / "sync"
    folder.mkdir(parents=True, exist_ok=True)
    stem = "sync-report" if not result.dry_run else "sync-report-dry-run"
    jpath, mpath = folder / f"{stem}.json", folder / f"{stem}.md"
    whole = sync_result_json(result)
    report = sync_report_json(result.report)
    data: JsonObject = {k: v for k, v in whole.items() if k not in ("report", "recovery")}
    data.update({k: v for k, v in report.items() if k != "slides"})
    data.update({f"slides_{k}": v for k, v in slides_said_json(result.report.slides).items()})
    if result.recovery is not None:   # (added after the run: `__main__.add_recovery`)
        data["recovery"] = result.recovery
    jpath.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    r, s = result.report, result.report.slides
    lines = [f"# Sync report{' (dry run)' if result.dry_run else ''}", "",
             f"- PDF: `{result.pdf}`", f"- Deck: {result.url}",
             f"- Base: {result.base_from} (generation {result.generation})",
             f"- Requests sent: {result.requests}", ""]
    _section(lines, "Source changes applied", r.applied, _applied_line)
    _section(lines, "Deck edits kept (overrides)", r.overrides, lambda x: f"- {_loc(x)}: {_fields(x)}")
    _section(lines, "Conflicts", r.conflicts, conflict_lines)
    _section(lines, "Settled for the source (--take-source)", r.resolved, _resolved_line)
    _section(lines, "Converged", r.converged, lambda x: f"- {_loc(x)}: {x['field']}")
    _section(lines, "Fitted to the words as merged", [as_object(x, "refit") for x in r.refit], _refit_line)
    lines += ["## Slides", f"- created: {_or_none(s.created)}", f"- deleted: {_or_none(s.deleted)}",
              f"- moved: {_or_none(s.moved)}", f"- kept (removed from the source, edited in the deck): {_or_none(_jsons(s.kept))}",
              f"- held (nothing written: which frame the slide is is in doubt): {_or_none(_jsons(s.held))}",
              f"- added in the deck: {_or_none(_jsons(s.user_added))}", ""]
    _section(lines, "Objects added in the deck", r.user_objects, _user_object_line)
    _section(lines, "Warnings", r.warnings, lambda x: f"- {x}")
    mpath.write_text("\n".join(lines), encoding="utf-8")
    return jpath, mpath


def overlay_mode(asked: str | None, recorded: str | None) -> tuple[str, str | None]:
    """(the overlay steps to convert the new source with, a warning). A deck converted with
    `--overlays all` holds a slide per step; syncing the same source with `last` would leave the
    steps in between out of `ours`, and sync would read them as slides the source dropped and
    delete the ones nobody had edited. So the deck's own mode is the default, and asking for the
    other one is allowed but said out loud."""
    if asked is None:
        return recorded or "last", None
    if recorded and asked != recorded:
        return asked, (f"this deck was converted with --overlays {recorded}, and you asked for {asked}: "
                       f"slides of the other kind read as slides the source dropped")
    return asked, None


def sync(pdf: Path, deck: str, out: Path | None, dry_run: bool, overlays: str | None, measure: bool,
         way_back: "WayBack | None", backup_mode: str, force_adopted: bool, follow_labels: bool,
         take_source: Sequence[str]) -> SyncResult:
    """Merge `pdf` into the deck `deck` names, three ways (docs/sync.md). `out`: the folder its
    base and reports go to (None: the deck's own, else the PDF's under `out_root`); `way_back`: the
    recovery note a caller is making meanwhile (`guard.WayBack`), collected before the first write,
    and asked what was kept before an adopted deck's first sync is refused (`backup_mode`: what the
    person asked for); `take_source`: conflict ids to settle for the source."""
    from . import adopt_sync
    from .google_auth import drive_service, shared_service, slides_service

    started = time.monotonic()
    pid, folder = resolve_deck(deck)
    out = out or folder or out_root() / pdf.stem
    slides, drive = slides_service(), drive_service()
    problems: list[str] = []
    # The deck is read while the base is loaded and the new PDF is converted: three things that
    # need nothing of each other, and a round trip costs about the same whatever else is in the
    # air. The worker uses the Slides client and this thread the Drive one - a service object
    # carries one connection and belongs to one thread at a time - unless a caller lent us its
    # own, which is that caller's and is used here alone (`emit.measure_places`' rule).
    alone = shared_service("slides", "v1") or shared_service("drive", "v3")
    pool = None if alone else ThreadPoolExecutor(1, thread_name_prefix="b2s-sync")
    reading = pool.submit(lambda: execute(slides.presentations().get(presentationId=pid))) if pool else None
    facts: DriveFile | None = None
    with contextlib.suppress(HttpError, OSError):
        facts = snapshot.deck_info(drive, pid)  # name, parents, appProperties: read once, used four times
    loaded, where = snapshot.load_base(pid, folder or out, drive, problems, facts)
    if loaded is None:
        if reading is not None:
            reading.cancel()
        if pool is not None:
            pool.shutdown(wait=False)
        raise NoSyncBase("\n".join([f"no sync base for presentation {pid}:convert the deck with this version first",
                                    "  A deck this converter never made has one only where `adopt` wrote it: sync it "
                                    "with `--deck <the adopt work folder>`,",
                                    "  not with the deck's URL. `convert` would make a second deck and leave this one "
                                    "with its comments and history behind.",
                                    *problems]))
    base: JsonObject = loaded
    stale = snapshot.stale_base_warning(where, drive, pid, facts)
    warnings = problems + ([stale] if stale else [])
    overlays, mismatch = overlay_mode(overlays, as_optional_str(base.get("overlays"), "base.overlays"))
    warnings += [mismatch] if mismatch else []
    for w in warnings:
        print(f"warning: {w}")
    # The base's picture files are held first: a base the last sync wrote names the files that sync
    # rendered into the folder this one renders into, and the render writes over them. Planned in
    # the deck's own width, not the frame convert writes into: a deck adopt took over is whatever
    # size the person made it, and everything this sync creates or moves is planned in slide pt.
    pictures = snapshot.hold_base_pictures(base, out)
    ours = build_ours_of(pdf, snapshot.sync_work(out), base, overlays, adopt_sync.deck_width(base), pictures)
    refreshed = snapshot.refresh_pictures(base, ours.slides, ours.pairs, ours.out, pictures)
    report_to = out

    def check_plan(mplan: merge.MergePlan, theirs: JsonObject) -> None:
        """An adopted deck's objects are a person's, not ours: refuse rather than write beside
        them (adopt_sync.problems). A dry run plans and reports; it writes nothing, so it never
        refuses - that is how a person sees what the sync wanted to do."""
        kept = way_back.backup() if way_back is not None else None
        found = adopt_sync.problems_of(base, mplan, theirs, kept, backup_mode)
        if found:
            raise adopt_sync.FirstSyncRefused(adopt_sync.refusal_message(pid, report_to, pdf, found), found)

    check: "Callable[[merge.MergePlan, JsonObject], None] | None" = None
    if base.get("origin") == adopt_sync.ORIGIN and not dry_run and not force_adopted:
        check = check_plan
    # A base that may be behind the deck never decides on its own that an object is a leftover.
    s = Sync(slides, drive, pid, base, ours, out, dry_run, measure, trust_generation=stale is None,
             check_plan=check, follow_labels=follow_labels, take_source=tuple(take_source), facts=facts,
             way_back=way_back)
    try:
        s.first_read = reading.result() if reading is not None else None
    except (HttpError, OSError):
        s.first_read = None   # it is read again on the main thread, as it always was
    finally:
        if pool is not None:
            pool.shutdown()
    try:
        result = s.run()
    except BaseException:
        s.await_deletes()   # (a run that ends badly still owns the file it sent away)
        raise
    merged = merge.report_json(result.plan.report)
    # (every warning, the merge's first; what is said after the base is stored lands in it too)
    said = [*_strs(merged["warnings"], "report.warnings")]
    # (a slide the deck deleted or the source dropped is nobody's business any more)
    updated = {p.key for p in result.plan.slides if isinstance(p, (merge.UpdateSlide, merge.HoldSlide))}
    said += s.warnings + warnings + unwritten_warnings(
        [u for u in ours.context_unwritten if u.slide in updated], s.ours)
    said += unread_warnings([u for u in ours.context_unread if u.slide in updated], ours.slides)
    contained, says = contained_report(ours.contained, ours.slides, result.plan)
    said += says
    forms: list[snapshot.BaseForm] = ours.base_forms
    said += snapshot.base_form_warnings(forms)
    converged: list[JsonObject] = [{"slide": r.slide, "element": r.element, "field": "image",
                                    "how": "the same picture, written differently"} for r in refreshed]
    generation = base_generation(base)
    adopted = any(isinstance(u.decision, (merge.AdoptUnit, merge.AdoptObject))
                  for p in result.plan.slides for u in slide_units(p))
    recovered = bool(s.recovery.sweep or s.recovery.sweep_slides or s.recovery.heal
                     or base.get("pending") or base.get("cleanup"))
    if not dry_run and (result.work.writes or adopted or refreshed or recovered):
        new = s.new_base(result) if (result.work.writes or adopted or refreshed) else dict(base)
        new["overlays"] = overlays  # (the steps the deck holds now)
        new.pop("pending", None)   # this run got to the end, so nothing is half done any more
        new.pop("cleanup", None)
        if s.cleanup_ids:
            # Stored before the old objects are deleted: whatever happens next, the base that the
            # next sync finds either still points at them or knows they have to go.
            new["cleanup"] = _json_strs(s.cleanup_ids)
        why = snapshot.store_base(new, out, drive, "base", facts)
        if why:
            said.append(
                f"could not store the new base in Drive ({why}); kept locally. The {len(s.cleanup_ids)} object(s) this "
                f"sync replaced are left in the deck: deleting them while the base another machine would read still "
                f"points at them could lose deck edits. The next sync removes them.")
        elif s.cleanup_ids:
            try:
                s.run_cleanup(s.final_revision)
            except (RuntimeError, HttpError) as e:  # they stay in the base's `cleanup` list
                said.append(f"the objects this sync replaced could not be deleted ({e}); "
                            f"the next sync removes them")
            else:
                # The objects are gone, so the list naming them must stop being read. That is all
                # this says, and a flag on the deck's own appProperties says it in a third of a
                # second where writing the base again costs about two (`snapshot.mark_cleaned`);
                # where the flag will not land, the base goes up again as it always did.
                new.pop("cleanup", None)
                snapshot.save_local(new, out)
                if snapshot.mark_cleaned(drive, pid, as_int(new["generation"], "base.generation"), facts):
                    snapshot.store_base(new, out, drive, "base", facts)
        generation = as_int(new["generation"], "base.generation")
    elif not dry_run and where == "drive":
        snapshot.save_local(base, out)  # (refresh the cache)
    s.await_deletes()   # the staging deck went on a thread; nobody leaves before it is gone
    # (what emit could not plan and made a picture: `planned`; a base in an older form, read in
    # today's: `base_today`)
    report = report_of_merge(merged, s.theme_applied, s.theme_conflicts, said,
                             contained_json(contained) if contained else None,
                             snapshot.base_form_json(forms) if forms else None,
                             converged, s.overruns, s.refit_moves)
    # Measured where the run really ends: storing the base is Drive round trips of the sync's own,
    # and a number that stopped before them was a number about something else.
    done = SyncResult(pdf=str(pdf), presentation_id=pid, url=f"https://docs.google.com/presentation/d/{pid}/edit",
                      dry_run=dry_run, base_from=where, generation=generation, overlays=overlays,
                      attempts=result.attempts, requests=s.sent, seconds=round(time.monotonic() - started, 1),
                      report=report, actions=plan_actions(result.plan), recovery=None)
    write_reports(out, done)
    return done
