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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import faults, identity, merge, snapshot
from .gapi import HttpError, status_of
from .gslides import EMU_PER_PT, emu, execute, pt
from .paths import out_root

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


# ------------------------------------------------- recovering from an interrupted earlier sync

def sync_generation(oid: str) -> int | None:
    """The generation of the sync that made this object id (None: not made by a sync)."""
    m = SYNC_ID.fullmatch(oid or "")
    return int(m.group(1)) if m else None


def plan_recovery(base: dict, theirs: dict, ours_keys=(), trust_generation: bool = True) -> dict:
    """What an earlier sync that died halfway left in the deck (docs/sync.md, "If a sync dies").

    `theirs`: snapshot.read_presentation of the live deck. Returns
    `{"sweep", "sweep_slides", "heal", "restore"}`.

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
    gen = base.get("generation", 0)
    pending = base.get("pending") or {}
    named = set(base.get("cleanup") or [])
    for ids in (pending.get("objects") or {}).values():
        named.update(ids)
    named_slides = set(pending.get("slides") or []) | {x for x in (base.get("cleanup") or [])}
    base_objects: set[str] = set()
    alive: dict[tuple[str, str], bool] = {}
    live = {oid for s in theirs["slides"] for oid in s["objects"]}
    for b in base["slides"]:
        for el in b["elements"]:
            oids = [o for o in (el.get("objects") or []) if o]
            base_objects.update(oids)
            alive[(b["key"], el["key"])] = bool(oids) and all(o in live for o in oids)
        base_objects.update(b.get("groups") or [])
    base_slides = {b.get("objectId") for b in base["slides"] if b.get("objectId")}
    wanted = {h6(k) for k in ours_keys}

    sweep, sweep_slides, heal = [], [], []
    for s in theirs["slides"]:
        sid = s["objectId"]
        if sid not in base_slides:
            g = sync_generation(sid)
            if sid in named_slides or (g is not None and g > gen and sid[4:10] in wanted):
                sweep_slides.append(sid)
                continue
        mine, here = [], []
        for oid, rb in s["objects"].items():
            if oid in base_objects:
                continue
            g = sync_generation(oid)
            if oid not in named and (g is None or g <= gen):
                continue  # a person's object, or one of this base's own generation
            title = rb.get("title") or ""
            key = title[len(snapshot.TAG_PREFIX):] if title.startswith(snapshot.TAG_PREFIX) else ""
            skey, _, ekey = key.partition("/")  # the element key has slashes of its own
            if key and ekey and not alive.get((skey, ekey), True):
                here.append({"slide": skey, "element": ekey, "objectId": oid})
            else:
                mine.append(oid)
        healed = [h["objectId"] for h in here]
        # A healed element's own group, number box and anchored pictures carry no tag of their own.
        sweep += [oid for oid in mine if not any(oid.startswith(m) for m in healed)]
        for h in here:
            h["objects"] = [h["objectId"]] + [oid for oid in mine if oid != h["objectId"] and oid.startswith(h["objectId"])]
        heal += here
    # Slides deletes a group's children with the group, so naming them as well would make it refuse
    # the whole batch ("The object ... could not be found") and nothing at all would be swept.
    parent = {oid: rb.get("parent_group") for s in theirs["slides"] for oid, rb in s["objects"].items()}
    doomed = set(sweep)

    def inside_a_doomed_group(oid: str) -> bool:
        seen, p = set(), parent.get(oid)
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
    restore = {oid: rb for oid, rb in ((k, v) for k, v in (pending.get("in_place") or {}).items())}
    return {"sweep": sweep, "sweep_slides": sweep_slides, "heal": heal, "restore": restore}


def heal_base(base: dict, heal: list[dict], theirs: dict, same_source: bool) -> list[str]:
    """Give the base elements in `heal` the objects an interrupted sync made for them, with their
    live read-back (so the merge sees the converter's own work, not a deck edit). When that sync
    converted another PDF than the one being synced now, what those objects show is unknown: the
    element's hashes are cleared so the source is written over them again."""
    by_key = {(b["key"], el["key"]): el for b in base["slides"] for el in b["elements"]}
    objects = {oid: rb for s in theirs["slides"] for oid, rb in s["objects"].items()}
    done = []
    for h in heal:
        el = by_key.get((h["slide"], h["element"]))
        if el is None:
            continue
        oids = h.get("objects") or [h["objectId"]]
        el["objects"] = oids
        el["main"] = oids[0]
        el["readback"] = {oid: objects[oid] for oid in oids if oid in objects}
        if not same_source:
            el["ir_hash"] = "interrupted"
            el["fields"] = {k: "interrupted" for k in el.get("fields", {})}
        done.append(f"{h['slide']}/{h['element']}")
    return done


def restore_in_place(theirs: dict, saved: dict) -> list[str]:
    """Put the text an interrupted sync overwrote in a placeholder back into the read-back (see
    plan_recovery): the merge then re-applies the person's edit to the recreated element."""
    back = []
    for s in theirs["slides"]:
        for oid, rb in list(s["objects"].items()):
            old = saved.get(oid)
            if old and (old.get("text") or "") != (rb.get("text") or ""):
                s["objects"][oid] = {**rb, **{k: old[k] for k in IN_PLACE_FIELDS if k in old}}
                back.append(oid)
    return back


def table_refill(base_el: dict | None, el: dict, objects: dict, scale: float, fonts) -> dict | None:
    """A table the source rewrote that can be refilled where it is: {"id", "margins", "cells"}, or
    None when it has to be made again.

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
    old, margins = base_el.get("ir") or {}, base_el.get("table_margins")
    live = objects.get(base_el.get("main"))
    if not margins or not live or "cells" not in old:
        return None
    was_dims, dims = [len(old["cells"]), len(old["columns"])], [len(el["cells"]), len(el["columns"])]
    if list(live.get("table") or []) != was_dims:
        return None  # (the deck added or removed a row or column)
    if was_dims == dims:
        if old.get("merges", []) != el.get("merges", []) or old.get("fills", []) != el.get("fills", []):
            return None  # (a merge or a fill can't be taken back by filling cells)
    elif old.get("merges") or el.get("merges") or old.get("fills") or el.get("fills"):
        return None  # (they are placed by row and column: another grid moves them)
    grid = merge.table_grid(live.get("text"), live.get("table"))
    if grid is None:
        return None
    new, was = pptx_table(el, scale, fonts), pptx_table(old, scale, fonts)
    steps = table_steps(margins, new["margins"], was_dims[1], dims[1])
    if steps is None:
        return None
    # (the margins the table has - nothing can change them - not the new conversion's, a rounding off)
    return {"id": base_el["main"], "margins": steps[1], "shift": (new["x"] - was["x"], new["y"] - was["y"]),
            "cells": [(r, c) for r, row in enumerate(grid) for c, text in enumerate(row) if text],
            "steps": steps[0]}


def table_steps(have: list, need: list, cols: int, want_cols: int, tol: float = 0.05
                ) -> tuple[list[dict], list] | None:
    """Row and column requests (with the table's id left as None) turning a table whose rows have
    the cell margins `have` into one of len(need) rows with the margins `need` and `want_cols`
    columns, and the margins it then has; None when no such steps exist. A row inserted below
    another takes its margins; a column's cells take those of their row whatever it is inserted
    beside, so columns go and come at the right edge."""
    def same(a, b):
        return all(abs(x - y) <= tol for x, y in zip(a, b))

    cur, reqs = [list(m) for m in have], []
    while True:
        i = next((k for k in range(min(len(cur), len(need))) if not same(cur[k], need[k])), min(len(cur), len(need)))
        if len(cur) == len(need):
            if i < len(cur):
                return None
            break
        if len(cur) > len(need):
            reqs.append({"deleteTableRow": {"tableObjectId": None, "cellLocation": {"rowIndex": i, "columnIndex": 0}}})
            del cur[i]
        elif i > 0 and same(cur[i - 1], need[i]):
            reqs.append({"insertTableRows": {"tableObjectId": None, "cellLocation": {"rowIndex": i - 1, "columnIndex": 0},
                                             "insertBelow": True, "number": 1}})
            cur.insert(i, list(cur[i - 1]))
        else:
            return None
    if want_cols > cols:
        reqs.append({"insertTableColumns": {"tableObjectId": None, "cellLocation": {"rowIndex": 0, "columnIndex": cols - 1},
                                            "insertRight": True, "number": want_cols - cols}})
    for c in range(cols - 1, want_cols - 1, -1):
        reqs.append({"deleteTableColumn": {"tableObjectId": None, "cellLocation": {"rowIndex": 0, "columnIndex": c}}})
    return reqs, cur


def drop_objects(pres: dict, objects, slides) -> dict:
    """A presentations.get without these page elements and slides (leftovers of an interrupted
    sync): everything downstream then plans as if they had never been created."""
    objects, slides = set(objects), set(slides)

    def keep(elements):
        out = []
        for e in elements:
            if e["objectId"] in objects:
                continue
            if "elementGroup" in e:
                children = keep(e["elementGroup"].get("children", []))
                if not children:
                    continue
                e = {**e, "elementGroup": {**e["elementGroup"], "children": children}}
            out.append(e)
        return out
    return {**pres, "slides": [{**s, "pageElements": keep(s.get("pageElements", []))}
                               for s in pres.get("slides", []) if s["objectId"] not in slides]}


# ---------------------------------------------------------------- ours

def build_ours(pdf: Path, work: Path, base: dict, overlays: str = "last",
               page_width: float | None = None) -> dict:
    """extract, classify and render the new PDF into `work`, plan it like emit and give its
    slides and elements the keys of the base they match.

    `page_width`: how wide the deck this will be written into is, in slide pt (default: the
    SLIDE_W frame `convert` makes). A deck `adopt` took over is whatever size the person made it,
    and every box this plan holds is PDF pt times the scale that width gives.

    A base `adopt` recorded also carries the deck's own boxes (`adopt_sync.deck_folds`), and what
    one of them the converter reads back as several is put together again before anything is keyed
    (`adopt_sync.fold_slides`) - the same fold the base's own side was built with, or the two sides
    of the merge would not be describing the same boxes."""
    from . import adopt_sync
    from .classify import classify
    from .emit import SLIDE_W, DeckPlan, merge_blocks
    from .extract import extract, select_overlays
    from .notes import prepare
    from .render import render_backgrounds

    work.mkdir(parents=True, exist_ok=True)
    prepared = prepare(pdf, work)
    raw = extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    raw = select_overlays(raw, overlays)
    deck = classify(raw)
    render_backgrounds(prepared.pdf, raw, deck, work)
    adopt_sync.fold_slides(deck, (base.get("adopt") or {}).get("boxes") or {})
    (work / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    plan = DeckPlan({**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]},
                    page_width or SLIDE_W)
    deck = plan.deck
    infos = [identity.slide_info(s) for s in deck["slides"]]
    base_infos = [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "",
                   "page": b["page"], "removed": b.get("removed")}   # `align_slides`: a slide the source dropped
                  for b in base["slides"]]
    moves = identity.label_moves(base_infos, infos)
    for m in moves:  # indices are no use to a reader of the report; the base's keys are
        m["slide"] = base["slides"][m["base"]]["key"]
        m["frame_is"] = base["slides"][m["frame_is"]]["key"] if m["frame_is"] is not None else None
        m["slide_is"] = infos[m["slide_is"]]["title"] if m["slide_is"] is not None else None
    weak: dict[int, str] = {}
    keys, pairs = identity.inherit_slide_keys(base_infos, [b["key"] for b in base["slides"]], infos, moves, weak)
    near = identity.near_misses(base_infos, infos, pairs)
    for m in near:  # as with the moves: the report reads better with the base's key than an index
        m["slide"] = base["slides"][m["base"]]["key"]
        m["title"] = infos[m["ours"]]["title"]
    ekeys, fps = [], []
    for j, slide in enumerate(deck["slides"]):
        matched = [{"key": e["key"], "kind": e["kind"], "role": e.get("role"), "fingerprint": e["fingerprint"]}
                   for e in base["slides"][pairs[j]]["elements"]] if j in pairs else None
        k, f = identity.slide_element_keys(slide["elements"], work, matched)
        ekeys.append(k)
        fps.append(f)
    entries = snapshot.slide_entries(deck, work, keys, ekeys, fps)
    unwritten = mark_emitted(base, entries, deck, pairs, plan.scale, plan.fonts)
    return {"source": pdf, "pdf": prepared.pdf, "out": work, "plan": plan, "deck": deck, "slides": entries,
            "pairs": pairs, "label_moves": moves, "weak_pairs": weak, "near_misses": near,
            "context_unwritten": unwritten}


def mark_emitted(base: dict, entries: list[dict], deck: dict, pairs: dict, scale: float, fonts,
                 fast: bool = True) -> list[dict]:
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
    (the tests turn it off to prove two emissions of the same slide compare equal)."""
    unwritten = []
    for j, i in pairs.items():
        b, o, slide = base["slides"][i], entries[j], deck["slides"][j]
        for e in b["elements"]:
            if any(f in e.get("fields", {}) for f in identity.CONTEXT_FIELDS):
                e["fields"] = {k: v for k, v in e["fields"].items() if k not in identity.CONTEXT_FIELDS}
        # (an element the deck keeps though the source dropped it is not what emit wrote the others beside)
        was = [e for e in b["elements"] if not e.get("removed")]
        if any("ir" not in e for e in was):
            continue
        base_by = {e["key"]: e for e in was}
        title_page = b.get("layout") == "TITLE" if b.get("layout") in ("TITLE", "TITLE_ONLY") else bool(slide.get("title_page"))
        if fast and title_page == bool(slide.get("title_page")) and len(base_by) == len(o["elements"]) and \
                all(oe["key"] in base_by and base_by[oe["key"]]["ir_hash"] == oe["ir_hash"] for oe in o["elements"]):
            continue  # (nothing on the slide changed, so nothing emit writes for it did)
        candidates = [k for k, oe in enumerate(o["elements"])
                      if oe["key"] in base_by and identity.source_changes(base_by[oe["key"]], oe) <= {"position"}]
        if not candidates:
            continue  # (whatever else changed is rewritten anyway, where the new conversion puts it)
        try:
            before = dict(zip(base_by, emitted_elements({**slide, "title_page": title_page, "elements": [e["ir"] for e in was]},
                                                        list(base_by), scale, fonts)))
        except Exception:  # noqa: BLE001 - an IR an older converter wrote that today's emit cannot read
            continue       # says nothing either way: no marks
        now = emitted_elements(slide, [oe["key"] for oe in o["elements"]], scale, fonts)
        for k in candidates:
            oe, el = o["elements"][k], slide["elements"][k]
            be = base_by[oe["key"]]
            step = [(el["bbox"][q] - be["ir"]["bbox"][q]) * scale for q in (0, 1)]
            marks, cannot = context_changes(before[oe["key"]], now[k], step, el["kind"],
                                            same_layout=title_page == bool(slide.get("title_page")))
            for f in marks:
                be["fields"] = {**be["fields"], f: "base"}
                oe["fields"] = {**oe["fields"], f: "ours"}
            if cannot:
                unwritten.append({"slide": o["key"], "element": oe["key"], "fields": sorted(cannot)})
    return unwritten


UNWRITTEN_SAYS = {
    "placeholder": "the new version puts {what} into another layout placeholder or out of one, because the "
                   "slide changes layout; a sync cannot change a live slide's layout, so {it} stayed where "
                   "{it_was}",
    "grouping": "the new version groups {what} differently with the shapes around {them} (a beamer block or "
                "rules {it} now lies on, or no longer does); a sync keeps the deck's groups, so {it} stayed "
                "grouped as before",
}


def unwritten_warnings(unwritten: list[dict], ours: dict) -> list[str]:
    """The report's words for `mark_emitted`'s changes a sync cannot write: one warning per slide and
    kind, naming the elements by their words (a person does not know "text/body/1"). Nothing is lost
    by them - the deck keeps what it has - but the deck no longer looks like a fresh conversion there,
    and nothing else says so."""
    slides = {s["key"]: s for s in ours.get("slides", [])}
    by: dict[tuple, list[str]] = {}
    for u in unwritten:
        slide = slides.get(u["slide"]) or {}
        el = next((e for e in slide.get("elements", []) if e["key"] == u["element"]), None)
        words = " ".join(identity.plain_text(el["ir"]).split()) if el and el.get("ir") and el["ir"].get("kind") == "text" else ""
        name = f"\"{words[:40]}{'...' if len(words) > 40 else ''}\"" if words else \
            f"the {el['kind'] if el else 'element'} {u['element']}"
        for f in u["fields"]:
            by.setdefault((u["slide"], f), []).append(name)
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


def emitted_elements(slide: dict, names: list[str], scale: float, fonts) -> list[dict]:
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
    out = [{"requests": [r for r in rename(rs, order) if "updatePageElementsZOrder" not in r], "box": e["boxes"][i],
            "role": "title" if i == e["title"] else "subtitle" if i == e["subtitle"] else None, "groups": []}
           for i, (_, rs) in enumerate(e["parts"][1:1 + len(ids)])]
    at = {f"@{name}": i for i, name in enumerate(names)}
    for _, rs in e["parts"][1 + len(ids):]:
        for r in rs:
            gid = r.get("groupObjects", {}).get("groupObjectId", "")
            kind = "block" if gid.startswith(f"{sid}_blk") else "rules" if gid.startswith(f"{sid}_rules") else None
            if kind:
                members = sorted(c[:-2] if c.endswith("_g") else c for c in rename(r["groupObjects"]["childrenObjectIds"], order))
                for c in members:
                    if c in at:
                        out[at[c]]["groups"].append([kind, members])
    for x in out:
        x["groups"].sort()
    return out


def context_changes(was: dict, now: dict, step: list[float], kind: str,
                    same_layout: bool = False) -> tuple[set[str], set[str]]:
    """(fields recreating the unit writes, fields it cannot) in which two `emitted_elements` entries
    of one element differ; `step`: slide pt the source moved the element by, which its requests
    may differ by and still say the same.

    A text that took the title page's subtitle role or lost it (`emit.subtitle_element`: the biggest
    plain text below the title) is written by recreating it when the slide keeps its layout
    (`same_layout`): `Sync.update_slide` hands the live SUBTITLE placeholder to the element that has
    the role now and makes the other a box. Any other role change (the slide changing layout) is not."""
    marks, cannot = set(), set()
    if was["role"] != now["role"]:
        # (and every request differs: a placeholder is transformed, a box created)
        if same_layout and {was["role"], now["role"]} <= {None, "subtitle"}:
            marks.add("emitted")
        else:
            cannot.add("placeholder")
    elif not _close(was["requests"], now["requests"], [v * EMU_PER_PT for v in step]):
        marks.add("width" if kind == "text" else "emitted")
    if (was["box"] is None) != (now["box"] is None) or was["box"] is not None and \
            any(abs(a + step[q % 2] - c) > WIDTH_MOVED for q, (a, c) in enumerate(zip(was["box"], now["box"]))):
        marks.add("placed")
    if was["groups"] != now["groups"]:
        cannot.add("grouping")
    return marks, cannot


def _close(a, b, step: list[float], key: str | None = None) -> bool:
    """Two request trees say the same: equal but for float noise, with EMU lengths within
    WIDTH_MOVED and translations less `step` (EMU) - the source moving an element moves its box."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        if a.get("unit") == "EMU" and isinstance(a.get("magnitude"), (int, float)):
            return abs(a["magnitude"] - b["magnitude"]) <= WIDTH_MOVED * EMU_PER_PT and \
                all(_close(a[k], b[k], step, k) for k in a if k != "magnitude")
        return all(_close(a[k], b[k], step, k) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y, step) for x, y in zip(a, b))
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

def rename(value, mapping: list[tuple[str, str]]):
    """Object ids in requests: a string equal to a key, or a key followed by a non-digit
    suffix (`_g`, `n`, `_n0`), gets the new id. `mapping` is longest key first."""
    if isinstance(value, dict):
        return {rename(k, mapping): rename(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rename(v, mapping) for v in value]
    if isinstance(value, str) and value.startswith("b2s_"):
        for old, new in mapping:
            if value == old or (value.startswith(old) and not value[len(old)].isdigit()):
                return new + value[len(old):]
    return value


def letterbox_fix(oid: str, box: list[float], px: tuple[int, int]) -> dict:
    """createImage fits a picture into its box keeping the aspect ratio; this stretches it to the
    box, as the .pptx import does."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    s = min(w / px[0], h / px[1])
    fw, fh = px[0] * s, px[1] * s
    fx, fy = x0 + (w - fw) / 2, y0 + (h - fh) / 2
    sx, sy = w / fw, h / fh
    return {"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": {
        "scaleX": sx, "scaleY": sy, "unit": "EMU",
        "translateX": round((x0 - sx * fx) * EMU_PER_PT), "translateY": round((y0 - sy * fy) * EMU_PER_PT)}}}


def png_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as img:
        return img.size


def api_colour(hex_or_theme: str | None) -> dict | None:
    if not hex_or_theme:
        return None
    if hex_or_theme.startswith("theme:"):
        return {"themeColor": hex_or_theme[6:]}
    from .emit import rgb
    return rgb(hex_or_theme)["opaqueColor"]


def api_text_style(runs: dict) -> tuple[dict, list[str]]:
    """A normalised run style (snapshot._text_style attributes) as an API TextStyle and its fields."""
    style, fields = {}, []
    for k, v in runs.items():
        if k in ("fontFamily", "weight"):
            if "weight" in runs:
                style["weightedFontFamily"] = {"fontFamily": runs.get("fontFamily"), "weight": runs.get("weight") or 400}
                fields.append("weightedFontFamily")
            else:
                style["fontFamily"] = v
                fields.append("fontFamily")
        elif k == "fontSize":
            style[k] = pt(v)
            fields.append(k)
        elif k in ("foregroundColor", "backgroundColor"):
            style[k] = {"opaqueColor": api_colour(v)} if v else {}
            fields.append(k)
        elif k == "link":
            continue
        else:
            style[k] = v
            fields.append(k)
    return style, list(dict.fromkeys(fields))


def style_override_requests(oid: str, change: dict, cells: list[dict] | None = None) -> list[dict]:
    """Uniform text style changes the deck made (merge.uniform_changes), over all of the text
    (`cells`: the cellLocations of a table, whose text is styled cell by cell)."""
    reqs = []
    runs, paras = change.get("runs") or {}, change.get("paragraphs") or {}
    where = [{"objectId": oid, "cellLocation": c} for c in cells] if cells is not None else [{"objectId": oid}]
    style, fields = api_text_style(runs)
    if fields:
        reqs += [{"updateTextStyle": {**w, "textRange": {"type": "ALL"}, "style": style, "fields": ",".join(fields)}}
                 for w in where]
    pstyle, pfields = {}, []
    for k, v in paras.items():
        if k in ("alignment", "lineSpacing", "direction"):
            pstyle[k] = v
            pfields.append(k)
        elif k in ("indentStart", "indentFirstLine", "spaceAbove", "spaceBelow"):
            pstyle[k] = pt(v)
            pfields.append(k)
    if pfields:
        reqs += [{"updateParagraphStyle": {**w, "textRange": {"type": "ALL"}, "style": pstyle, "fields": ",".join(pfields)}}
                 for w in where]
    return reqs


def raw_objects(pres: dict) -> dict[str, dict]:
    """objectId -> page element of a presentations.get (group children included)."""
    out, stack = {}, [e for s in pres.get("slides", []) for e in s.get("pageElements", [])]
    while stack:
        e = stack.pop()
        out[e["objectId"]] = e
        stack += e.get("elementGroup", {}).get("children", [])
    return out


deck_attributes = merge.deck_attributes   # which attributes on a run are the person's (merge decides)


def text_containers(old: dict, new: dict) -> list[tuple[dict | None, dict | None, dict | None]]:
    """(cellLocation, old text, new text) of two page elements holding text: a shape, or the cells
    of two tables of the same shape."""
    if "shape" in old and "shape" in new:
        return [(None, old["shape"].get("text"), new["shape"].get("text"))]
    if "table" in old and "table" in new:
        orows, nrows = old["table"].get("tableRows", []), new["table"].get("tableRows", [])
        if len(orows) != len(nrows) or any(len(a.get("tableCells", [])) != len(b.get("tableCells", [])) for a, b in zip(orows, nrows)):
            return []
        return [({"rowIndex": r, "columnIndex": c}, oc.get("text"), nc.get("text"))
                for r, (orow, nrow) in enumerate(zip(orows, nrows))
                for c, (oc, nc) in enumerate(zip(orow.get("tableCells", []), nrow.get("tableCells", [])))]
    return []


def _utf16_offsets(text: str) -> list[int]:
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + (2 if ord(ch) > 0xFFFF else 1))
    return offsets


def style_range_requests(oid: str, old: dict, new: dict, base_styles: list[dict], merged: str | None = None) -> list[dict]:
    """The deck's run style edits of `old` (the live object before sync) re-applied to the same
    words in `new` (its recreation; `merged`: the text it will hold after the text override)."""
    from bisect import bisect_left
    from difflib import SequenceMatcher

    reqs = []
    for loc, old_text, new_text in text_containers(old, new):
        before = snapshot.read_text(old_text)[0]
        after = merged if merged is not None and loc is None else snapshot.read_text(new_text)[0]
        b_off, a_off = _utf16_offsets(before), _utf16_offsets(after)
        blocks = SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
        for te in (old_text or {}).get("textElements", []):
            run = te.get("textRun")
            if not run or not run.get("content", "").strip("\n"):
                continue
            style, fields = api_text_style(deck_attributes(snapshot._text_style(run.get("style", {})), base_styles))
            if not fields:
                continue
            trailing = len(run["content"]) - len(run["content"].rstrip("\n"))  # (paragraph ends keep their style)
            start, end = bisect_left(b_off, te.get("startIndex", 0)), bisect_left(b_off, te.get("endIndex", 0) - trailing)
            for i, j, n in blocks:
                lo, hi = max(start, i), min(end, i + n)
                if hi > lo:
                    reqs.append({"updateTextStyle": {"objectId": oid, **({"cellLocation": loc} if loc else {}), "textRange": {
                        "type": "FIXED_RANGE", "startIndex": a_off[j + lo - i], "endIndex": a_off[j + hi - i]},
                        "style": style, "fields": ",".join(fields)}})
    return reqs


def shape_style_requests(oid: str, style: dict) -> list[dict]:
    """A shape's fill and outline as the deck has them (snapshot.shape_style)."""
    props, fields = {}, []
    fill = style.get("fill") or {}
    if "color" in fill:
        props["shapeBackgroundFill"] = {"solidFill": {"color": api_colour(fill["color"]), "alpha": fill.get("alpha", 1.0)}}
        fields += ["shapeBackgroundFill.solidFill.color", "shapeBackgroundFill.solidFill.alpha"]
    elif fill.get("state") == "NOT_RENDERED":
        props["shapeBackgroundFill"] = {"propertyState": "NOT_RENDERED"}
        fields.append("shapeBackgroundFill.propertyState")
    outline = style.get("outline") or {}
    if outline.get("state") == "NOT_RENDERED":
        props["outline"] = {"propertyState": "NOT_RENDERED"}
        fields.append("outline.propertyState")
    elif outline.get("fill") and "color" in outline["fill"]:
        props["outline"] = {"outlineFill": {"solidFill": {"color": api_colour(outline["fill"]["color"]),
                                                          "alpha": outline["fill"].get("alpha", 1.0)}},
                            "weight": pt(outline.get("weight") or 1.0)}
        fields += ["outline.outlineFill.solidFill.color", "outline.outlineFill.solidFill.alpha", "outline.weight"]
        if outline.get("dash"):
            props["outline"]["dashStyle"] = outline["dash"]
            fields.append("outline.dashStyle")
    return [{"updateShapeProperties": {"objectId": oid, "shapeProperties": props, "fields": ",".join(fields)}}] if fields else []


def box_overlap(a: list[float] | None, b: list[float] | None) -> float:
    """Area shared by two boxes, over the larger one's area (0 when either is empty)."""
    if not a or not b:
        return 0.0
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    areas = [(x[2] - x[0]) * (x[3] - x[1]) for x in (a, b)]
    return ix / max(areas) if max(areas) > 0 else 0.0


HIDDEN = 0.2  # of a text's box: an opaque shape covering more of it hides it (loss_oracle.HIDDEN)


def subtree(objects: dict, oid: str) -> list[str]:
    """`oid` and everything it carries, a group's children included."""
    out: list[str] = []

    def walk(x):
        if x in objects and x not in out:
            out.append(x)
            for c in objects[x].get("children") or []:
                walk(c)
    walk(oid)
    return out


def would_hide(objects: dict, top: str, under: str, spoken_for: set[str] | None = None) -> bool:
    """Would drawing `top` above `under` put an opaque shape over words somebody can read? The
    question `loss_oracle.occlusion_findings` asks of a finished sync, asked before the write.

    `spoken_for`: objects whose words the source draws itself, and about whose stacking it therefore
    has an opinion (`Sync.restack`'s last pass asks only about words **only the deck** has). It is
    asked of each text rather than of `under` as a whole, because one page element may hold both - a
    group the person made around one of their own text boxes and one of the converter's. Asking
    whether that element is the source's protected the group and let a created panel cover the
    person's box inside it (converted seed 5200496 at chain 12)."""
    shapes = [objects[x] for x in subtree(objects, top)
              if objects[x].get("kind") == "shape" and objects[x].get("box")
              and ((objects[x].get("shape_style") or {}).get("fill") or {}).get("color") is not None
              and (((objects[x].get("shape_style") or {}).get("fill") or {}).get("alpha") or 0) >= 0.999]
    texts = [objects[x] for x in subtree(objects, under)
             if x not in (spoken_for or ()) and objects[x].get("box")
             and (objects[x].get("text") or "").strip()]
    for t in texts:
        area = (t["box"][2] - t["box"][0]) * (t["box"][3] - t["box"][1])
        for s in shapes:
            w = min(t["box"][2], s["box"][2]) - max(t["box"][0], s["box"][0])
            h = min(t["box"][3], s["box"][3]) - max(t["box"][1], s["box"][1])
            if area > 0 and w > 0 and h > 0 and w * h / area > HIDDEN:
                return True
    return False


def drawn_order(objects: dict, order: list[str]) -> tuple[list[str], dict[str, str]]:
    """Object ids bottom to top, and for each the page element it is drawn inside (itself, when it is
    one): the page's elements in `order`, a group's children where the group stands."""
    out: list[str] = []
    top: dict[str, str] = {}

    def walk(oid, root):
        if oid not in objects or oid in top:
            return
        out.append(oid)
        top[oid] = root
        for c in objects[oid].get("children") or []:
            walk(c, root)
    for t in order:
        walk(t, t)
    return out, top


def folded_hiders(read: dict, made: set[str], ours: set[str], doomed: set[str] = frozenset()) -> list[tuple[str, str]]:
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
    objects = read.get("objects") or {}
    order, top = drawn_order(objects, read.get("order") or [])
    out = []
    for i, oid in enumerate(order):
        rb, stands_in = objects[oid], top[oid]
        fill = (rb.get("shape_style") or {}).get("fill") or {}
        if (oid not in made or stands_in == oid or stands_in in ours or stands_in in made
                or rb.get("kind") != "shape" or not rb.get("box")
                or fill.get("color") is None or (fill.get("alpha") or 0) < 0.999):
            continue
        for under in order[:i]:
            u = objects[under]
            if top[under] == stands_in or under in doomed or not u.get("box") or not (u.get("text") or "").strip():
                continue
            area = (u["box"][2] - u["box"][0]) * (u["box"][3] - u["box"][1])
            w = min(u["box"][2], rb["box"][2]) - max(u["box"][0], rb["box"][0])
            h = min(u["box"][3], rb["box"][3]) - max(u["box"][1], rb["box"][1])
            if area > 0 and w > 0 and h > 0 and w * h / area > HIDDEN:
                out.append((under, oid))
    return out


BREAK = {"__b2s_break__": True}  # where a batch may be cut: between slides
PENDING_URL = "b2s-pending:"     # a picture whose staging URL is still on its way (`Sync.fill_urls`)


def batches(reqs: list[dict], size: int = CHUNK) -> list[list[dict]]:
    """The requests split into batches of at most `size`, cut only where `main_requests` allows it
    (between slides), so a sync that dies between two batches leaves whole slides behind. One slide
    with more than `size` requests is the only thing that is ever split."""
    size = faults.batch_size(size)
    blocks: list[list[dict]] = []
    current: list[dict] = []
    for r in reqs:
        if r == BREAK:
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(r)
    if current:
        blocks.append(current)
    out: list[list[dict]] = []
    for block in blocks:
        if len(block) > size:
            out += [block[i:i + size] for i in range(0, len(block), size)]
        elif out and len(out[-1]) + len(block) <= size:
            out[-1] += block
        else:
            out.append(list(block))
    return out


def matrix_request(oid: str, m: list[float]) -> dict:
    return {"updatePageElementTransform": {"objectId": oid, "applyMode": "RELATIVE", "transform": {
        "scaleX": m[0], "shearX": m[1], "shearY": m[2], "scaleY": m[3], "unit": "EMU",
        "translateX": round(m[4] * EMU_PER_PT), "translateY": round(m[5] * EMU_PER_PT)}}}


def carried(base_rb: dict, theirs_rb: dict, new_rb: dict) -> list[float]:
    """The RELATIVE transform that puts the person's edit of a unit (base -> theirs: a move, a
    resize) onto its recreation `new_rb`, carried along by the source's move of it (base -> new).

    `theirs * base^-1` alone is a page-space step: right for a move, but a resize in it scales about
    the page origin, so it would scale the source's move too. Taken about the unit's corner instead -
    conjugated by the source's step `s` of that corner, `T(s) * d * T(-s)` - a move lands at the
    source's new place plus the person's offset and a resize keeps the person's proportions of the
    source's new box, from its new corner. When the source did not move the unit, `s` is 0 and this
    is `d` itself."""
    d = snapshot.compose(theirs_rb["transform"], snapshot.invert(base_rb["transform"]))
    sx, sy = new_rb["box"][0] - base_rb["box"][0], new_rb["box"][1] - base_rb["box"][1]
    return d[:4] + [d[4] + sx - (d[0] * sx + d[1] * sy), d[5] + sy - (d[2] * sx + d[3] * sy)]


# ---------------------------------------------------------------- the sync

class Sync:
    way_back = None   # the recovery note being made meanwhile; None where there is none to collect
    theme_side = theme_plan = raw_after = None   # theme_sync's part (set in __init__ / plan_theme)

    def __init__(self, slides, drive, pid: str, base: dict, ours: dict, out: Path, dry_run: bool = False,
                 measure: bool = True, trust_generation: bool = True, check_plan=None,
                 follow_labels: bool = False, take_source=(), facts: dict | None = None,
                 way_back=None):
        # check_plan(mplan, theirs): raises instead of letting the write go ahead. It sits between
        # planning and preparing because that is the last point at which nothing has been sent and
        # the whole of what would be written is known (adopt_sync.problems).
        self.check_plan = check_plan
        self.slides, self.drive, self.pid = slides, drive, pid
        self.base, self.ours, self.out = base, ours, out
        self.dry_run, self.measure = dry_run, measure
        self.trust_generation = trust_generation  # may this base's generation decide what is a leftover?
        self.follow_labels = follow_labels        # write to a slide whose label may have moved (merge.hold_slide)
        self.take_source = tuple(take_source or ())  # conflicts to settle for the source (merge.Resolutions)
        self.plan = ours["plan"]
        self.scale = self.plan.scale
        self.tok = self.token()
        self.sent: dict[str, int] = {}
        self.warnings: list[str] = []
        self.overruns: list[dict] = []        # warn_about_overruns, for the report
        self.refit_moves: list[dict] = []     # what `refit` moved or grew, for the report
        self.urls: dict[str, str] = {}  # picture file (str) -> contentUrl from the staging deck
        self.recovery: dict = {}        # what an interrupted earlier sync left (plan_recovery)
        self.cleanup_ids: list[str] = []      # old objects and slides, deleted after everything else
        self.cleanup_requests: list[dict] = []
        self.in_place_readback: dict[str, dict] = {}  # objects rewritten in place, before the write
        self.final_revision: str | None = None
        self.facts = facts              # the deck's Drive facts, read once (snapshot.deck_info)
        self.first_read: dict | None = None  # a `presentations.get` a caller made while we planned
        self.deleting: list = []        # staging decks on their way out (drop_staging)
        self.way_back = way_back        # made meanwhile, collected before the first write (guard.WayBack)
        self.theme_side: dict | None = None   # what a fresh conversion writes on the master and layouts (theme_sync)
        self.theme_plan: dict | None = None   # what this sync writes there (None: nothing, an old base)
        self.raw_after: dict | None = None    # the deck as `finish` last read it

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
        used = {(self.base.get("pending") or {}).get("token")}
        for _ in range(20):
            tok = f"{self.base.get('generation', 0) + 1}{''.join(random.choices(string.ascii_lowercase, k=2))}"
            if tok not in used:
                return tok
        return tok

    # ---- reading and writing

    def read(self) -> dict:
        """The deck, whole. The first one may have been fetched while the base was loaded and the
        PDF converted (`sync`); it is used once and never again, so every later read - after a
        write, or on a second attempt - is a fresh one."""
        if self.first_read is not None:
            pres, self.first_read = self.first_read, None
            return pres
        return execute(self.slides.presentations().get(presentationId=self.pid))

    def revision(self, slides=None) -> str:
        """`slides`: the client to ask with, where this runs on a thread of its own (`in_background`)."""
        return execute((slides or self.slides).presentations().get(
            presentationId=self.pid, fields="revisionId"))["revisionId"]

    def asked_revision(self, asking) -> str:
        """The revision a worker went to fetch, or one fetched here: a question that failed on the
        thread is asked again rather than carried into the write, where an empty
        `requiredRevisionId` would mean "whatever the deck says now"."""
        if asking is not None:
            try:
                return asking.result()
            except (HttpError, OSError, KeyError):
                pass
        return self.revision()

    def send(self, phase: str, reqs: list[dict], rev: str | None) -> str:
        """Batches with requiredRevisionId, chained through the revisions they return. A batch is
        cut at a slide boundary where it can (`batches`), so a run that dies between two batches
        leaves whole slides written, never half of one."""
        self.before_write()
        self.fill_urls(reqs)   # nothing goes out carrying a marker, whoever built it (`picture_url`)
        for n, chunk in enumerate(batches(reqs)):
            for attempt in range(len(FETCH_RETRY) + 1):
                try:
                    body = {"requests": chunk}
                    if rev:
                        body["writeControl"] = {"requiredRevisionId": rev}
                    res = execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body=body))
                    break
                except HttpError as e:
                    from .emit import api_error
                    message = api_error(e)
                    if status_of(e) == 400 and "revision" in message.lower() and n == 0:
                        raise RevisionMismatch(message)
                    if status_of(e) == 400 and PICTURE_FETCH in message and attempt < len(FETCH_RETRY):
                        # Google could not fetch a staged picture this time (live fuzz seed 2603: the
                        # same sync run again went through). A refused batch applied nothing - a
                        # batchUpdate is all or nothing - so the same batch is sent again.
                        time.sleep(FETCH_RETRY[attempt])
                        continue
                    raise RuntimeError(f"sync {phase}: batch refused ({message})") from e
            rev = res.get("writeControl", {}).get("requiredRevisionId") or self.revision()
            self.sent[phase] = self.sent.get(phase, 0) + len(chunk)
            faults.fail_at(phase)
        return rev or self.revision()

    # ---- planning

    def run(self) -> dict:
        from .emit import slide_layout  # noqa: F401 (warm import before timing-sensitive steps)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            pres = self.read()
            leftovers = [s["objectId"] for s in pres.get("slides", []) if SCRATCH.fullmatch(s["objectId"])]
            if leftovers and not self.dry_run:  # measure_places' scratch slides of an interrupted run
                self.delete_scratch(leftovers)
                pres = self.read()
            pres = {**pres, "slides": [s for s in pres.get("slides", []) if not SCRATCH.fullmatch(s["objectId"])]}
            if attempt == 1 and not snapshot.base_matches(self.base, snapshot.read_presentation(pres)):
                raise BaseMismatch(
                    f"the sync base (generation {self.base.get('generation', 0)}) describes none of the slides in "
                    f"presentation {self.pid}: it belongs to another copy of this deck, or the deck was rebuilt "
                    f"outside sync. Syncing would report every element as deleted. Convert the PDF again "
                    f"(python -m beamer2slides convert) to start a new base, or point --deck at the right deck.")
            pres = self.recover(pres, attempt)
            theirs = snapshot.read_presentation(pres)
            if self.recovery.get("restore"):
                restore_in_place(theirs, self.recovery["restore"])
            mplan = self.merge_plan(theirs, pres)
            if self.check_plan is not None:
                self.check_plan(mplan, theirs)  # (adopt_sync: an adopted deck this may not be written to)
            self.plan_theme(mplan, pres)
            work = self.prepare(mplan, pres, theirs)
            result = {"attempts": attempt, "plan": mplan, "work": work, "theirs": theirs}
            if self.dry_run or not work["writes"]:
                self.created, self.final_revision = theirs, theirs["revisionId"]  # (a base that only adopts deck fields)
                return result
            hook = os.environ.pop("B2S_SYNC_BEFORE_WRITE", None)  # (tests: someone edits the deck now)
            if hook:
                subprocess.run(hook, shell=True, check=False)
            if self.revision() != theirs["revisionId"]:
                continue  # edited while we planned: plan again
            faults.fail_at("plan")
            # The staging deck and the scratch slides are two things Google can do at once: the
            # staging deck is a file of its own, the scratch slides are pages nothing else here
            # looks at, and neither reads what the other writes. The worker builds clients of its
            # own, since a service object is one connection - unless a caller lent us one, which
            # is that caller's and is used on this thread alone (`emit.measure_places`' rule).
            staging, scratch = None, []               # noted in the pending marker: a run that
            staged = self.stage_in_background(work)   # dies leaves it for the next one to delete
            asking, pending = None, None
            try:
                if staged is None:
                    self.staging = staging = self.stage(work)
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
            result["revisionId"] = rev
            return result
        raise RuntimeError(f"the deck kept changing while syncing ({MAX_ATTEMPTS} attempts)")

    def recover(self, pres: dict, attempt: int) -> dict:
        """Undo what an earlier sync that died halfway left behind, before anything is planned
        (plan_recovery): its leftover objects are deleted, an element whose objects it deleted takes
        over the ones it created, and a placeholder it rewrote gets the person's text back for the
        merge to re-apply. The deletions are the first thing this sync writes, and they only ever
        remove an object the deck holds a second time."""
        read = snapshot.read_presentation(pres)
        rec = plan_recovery(self.base, read, [s["key"] for s in self.ours["slides"]],
                            getattr(self, "trust_generation", True))
        self.recovery = rec
        # A run that died left its staging deck in Drive. Its id is in `pending.staging` so a person
        # can find it; sync doesn't delete it, because an id read from a file could name anything -
        # only the file this process just created is ever deleted (tests/test_guard.py).
        if rec["heal"]:
            same = (self.base.get("pending") or {}).get("source", {}).get("sha1") == \
                snapshot.source_info(self.ours["source"]).get("sha1")
            healed = heal_base(self.base, rec["heal"], read, same)
            if healed and attempt == 1:
                self.warnings.append(f"an earlier sync was interrupted after it had replaced {len(healed)} element(s) "
                                     f"({', '.join(healed[:3])}{'...' if len(healed) > 3 else ''}): the deck's objects "
                                     f"were taken over" + ("" if same else " and are written over from the source"))
        if rec["sweep"] or rec["sweep_slides"]:
            if self.dry_run:
                self.warnings.append(f"an earlier sync left {len(rec['sweep'])} object(s) and "
                                     f"{len(rec['sweep_slides'])} slide(s) behind; a real sync would delete them")
            else:
                gone = self.delete_leftovers(rec["sweep"] + rec["sweep_slides"])
                if gone and attempt == 1:
                    self.warnings.append(f"deleted {len(gone)} leftover object(s)/slide(s) of an interrupted sync")
                # Only what is really gone may be planned away: anything still in the deck has to
                # stay in the picture, or the merge would create it a second time.
                rec["sweep"] = [oid for oid in rec["sweep"] if oid in gone]
                rec["sweep_slides"] = [sid for sid in rec["sweep_slides"] if sid in gone]
            pres = drop_objects(pres, rec["sweep"], rec["sweep_slides"])
        return pres

    def delete_leftovers(self, ids: list[str]) -> set[str]:
        """Delete what an interrupted sync left behind and say what is really gone. One batch; if
        Google refuses it, one request at a time, so a single id it no longer knows (deleting a
        group takes its children with it) doesn't save every other leftover from being swept."""
        if not ids:
            return set()
        self.before_write()
        reqs = [{"deleteObject": {"objectId": oid}} for oid in ids]
        try:
            execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body={"requests": reqs}))
            return set(ids)
        except HttpError as first:
            gone = set()
            for oid in ids:
                try:
                    execute(self.slides.presentations().batchUpdate(
                        presentationId=self.pid, body={"requests": [{"deleteObject": {"objectId": oid}}]}))
                except HttpError as e:
                    if "could not be found" not in str(e):
                        continue  # still in the deck: the merge has to keep seeing it
                gone.add(oid)
            if len(gone) < len(ids):
                self.warnings.append(f"could not delete {len(ids) - len(gone)} leftover object(s) of an "
                                     f"interrupted sync: {first}")
            return gone

    def pending_in_background(self, work: dict, theirs: dict):
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

    def mark_pending(self, work: dict, theirs: dict) -> None:
        """The marker built and stored here and now (`pending_in_background` is the other order)."""
        self.pending_block(work, theirs)
        self.store_pending(self.drive)

    def pending_block(self, work: dict, theirs: dict) -> None:
        """The base's `pending` block before the first write: the generation and token of
        this run, the objects it is about to create and the read-back of the objects it rewrites in
        place. The base itself is unchanged, so a run that dies leaves a valid base of the old
        generation plus a note of what it started. Only a run that gets to the end removes it."""
        objects: dict[str, list[str]] = {}
        slides: list[str] = []
        for w in work["slides"]:
            p = w["plan"]
            if p["action"] not in ("create", "update") or p.get("ours") is None:
                continue
            o = self.ours["slides"][p["ours"]]
            if p["action"] == "create" and w.get("sid"):
                slides.append(w["sid"])
            for i, oids in (w.get("objects") or {}).items():
                objects[f"{o['key']}/{o['elements'][i]['key']}"] = list(oids)
            if w.get("groups"):
                objects[f"{o['key']}/~groups"] = list(w["groups"])
        self.base["pending"] = {
            "generation": self.base.get("generation", 0) + 1, "token": self.tok,
            "revisionId": theirs.get("revisionId"), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": snapshot.source_info(self.ours["source"]), "objects": objects, "slides": slides,
            "in_place": self.in_place_readback, "staging": getattr(self, "staging", None)}
        if self.theme_plan and self.theme_plan["pending"]:
            # the layout placeholder styles about to be written: a run that dies after them finds
            # its own writes there next time, not a person's (theme_sync.plan)
            self.base["pending"]["theme"] = self.theme_plan["pending"]

    def store_pending(self, drive) -> None:
        """The marker to disk and to Drive. `staging` is whatever this run knows of its staging deck
        when the marker goes up, which where the two are made at once is nothing: it is a note for a
        person and nothing reads it, the staging deck naming itself in Drive
        (`appProperties.b2sStaging`, which is what `tools/drive_usage.py` queries)."""
        why = snapshot.store_base(self.base, self.out, drive, label="pending", info=self.facts)
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

    def sign_changed(self, theirs: dict, pres: dict) -> tuple[set[str], set[str]]:
        """The live pictures whose contentUrl differs from the base's, marked `unchecked` and not
        yet signed: (image ids, slide ids of background pictures).

        Google issues new URLs for unchanged pictures, so only the pixels tell a replaced one - but
        the answer only matters where the plan could write over the picture. A unit the source left
        alone is kept whatever the deck did to it, and so is its slide's background, so its picture
        is never read (a process that may not fetch a URL reads it out of a Drive export, and most
        syncs then need neither). `run` plans first and signs what the plan says is in question
        (`pictures_in_question`), then plans again. An unchecked picture still counts as replaced
        - the answer that never loses one - but is not reported as the person's edit
        (`merge.unchecked`)."""
        images = {oid: rb["image"] for s in self.base["slides"] for e in s["elements"]
                  for oid, rb in e.get("readback", {}).items() if "image" in rb}
        backgrounds = {s.get("objectId"): s.get("background_readback") or {} for s in self.base["slides"]}
        objects, slides = set(), set()
        for s in theirs["slides"]:
            for oid, rb in s["objects"].items():
                if "image" in rb and oid in images and images[oid].get("contentHash") != rb["image"].get("contentHash"):
                    objects.add(oid)
                    rb["image"]["unchecked"] = True
            bg, old = s.get("background") or {}, backgrounds.get(s["objectId"], {})
            if "picture" in bg and "picture" in old and old["picture"] != bg["picture"]:
                slides.add(s["objectId"])
                bg["unchecked"] = True
        return objects, slides

    def pictures_in_question(self, mplan: dict, objects: set[str], slides: set[str]) -> tuple[set[str], set[str]]:
        """Of the unchecked pictures (`sign_changed`), those whose answer the plan depends on:
        every one on a slide the plan does anything but update or hold (a slide the source dropped
        is kept for the person's edits), and on an updated slide every one of a unit the source
        changed or dropped, and the background when the source changed it. What is left out is a
        picture of a unit kept with nothing of the source's to write - the same plan whether or not
        the person replaced it."""
        ask_objects, ask_slides = set(), set()
        for p in mplan["slides"]:
            if p.get("base") is None or p.get("held"):
                continue
            b = self.base["slides"][p["base"]]
            mine = {oid for e in b["elements"] for oid in (e.get("objects") or []) if oid in objects}
            if p["action"] != "update":
                ask_objects |= mine
                if b.get("objectId") in slides:
                    ask_slides.add(b["objectId"])
                continue
            bunits = merge.units(b["elements"])
            for u in p.get("units", []):
                if u["action"] == "keep" and not u.get("source") and not u.get("removed"):
                    continue
                ask_objects |= {oid for m in bunits.get(u["key"]) or [] for oid in (m.get("objects") or [])
                                if oid in objects}
            o = self.ours["slides"][p["ours"]] if p.get("ours") is not None else {}
            if b.get("objectId") in slides and b.get("background") != o.get("background"):
                ask_slides.add(b["objectId"])
        return ask_objects, ask_slides

    def merge_plan(self, theirs: dict, pres: dict) -> dict:
        """The merge plan, with the pictures it depends on signed (`sign_changed`). (`self.plan`
        is the new conversion's DeckPlan.)"""
        objects, slides = self.sign_changed(theirs, pres)
        adopter = self.picture_adopter(pres)
        plan = lambda: merge.plan_merge(self.base, self.ours, theirs, adopter,  # noqa: E731
                                        follow_labels=self.follow_labels, take_source=self.take_source)
        mplan = plan()
        asked_objects, asked_slides = set(), set()
        for _ in range(3):   # (signing can only settle a slide, never put a new one in question)
            ask_objects, ask_slides = self.pictures_in_question(mplan, objects, slides)
            ask_objects, ask_slides = ask_objects - asked_objects, ask_slides - asked_slides
            if not ask_objects and not ask_slides:
                break
            asked_objects |= ask_objects
            asked_slides |= ask_slides
            snapshot.sign_pictures(theirs, pres, ask_objects, ask_slides, pictures=self.live_pictures(pres))
            mplan = plan()
        self.picture_reads = {"unchecked": len(objects) + len(slides), "asked": len(asked_objects) + len(asked_slides)}
        return mplan

    def live_pictures(self, pres: dict):
        """The pictures of this read of the deck (`deck_pictures.LivePictures`): one per read, so a
        Drive export is made at most once for it."""
        from .deck_pictures import LivePictures
        from .google_auth import fetcher_for_threads
        key = (pres.get("presentationId"), pres.get("revisionId"), id(pres))
        if getattr(self, "_live_pictures", (None, None))[0] != key:
            self._live_pictures = (key, LivePictures(pres, getattr(self, "drive", None), fetcher_for_threads()))
        return self._live_pictures[1]

    def picture_adopter(self, pres: dict):
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
        base_ids ={oid for s in self.base["slides"] for e in s["elements"] for oid in (e.get("objects") or [])}
        scale = self.scale or merge.deck_scale(self.base) or 1.0
        folder = self.ours["out"] / "deck-pictures"
        deck, mine, taken = {}, {}, set()

        def fingerprints(path: Path):
            return identity.sha1(path.read_bytes()), picture_look(path), picture_hash(path)

        def deck_picture(oid: str):
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

        def our_picture(path: Path):
            if path not in mine:
                mine[path] = fingerprints(path) if path.exists() else None
            return mine[path]

        def same(ours, oid: str) -> bool:
            got = deck_picture(oid)
            if not got:
                return False
            if got[0] == ours[0]:
                return True
            if same_look(ours[1], got[1]):
                return True
            return ours[2] is not None and got[2] is not None and not picture_differs(ours[2], got[2], TOL["phash"])

        def adopt(skey: str, ours_members: list[dict], read: dict, oid: str | None = None):
            if len(ours_members or ()) != 1 or ours_members[0]["kind"] != "image":
                return None
            el = ours_members[0]
            if not el.get("ir", {}).get("file"):
                return None
            ours = our_picture(self.ours["out"] / el["ir"]["file"])
            if ours is None or ours[1] is None:
                return None
            if oid is not None:  # the deck replaced this element's own picture
                return oid if same(ours, oid) else None
            box = [v * scale for v in el["fingerprint"]["bbox"]]
            best, score = None, PICTURE_OVERLAP
            for cand, rb in read["objects"].items():
                if "image" not in rb or cand in base_ids or cand in taken or rb.get("parent_group") \
                        or (rb.get("title") or "").startswith(snapshot.TAG_PREFIX):
                    continue
                over = box_overlap(box, rb.get("box"))
                if over >= score and same(ours, cand):
                    best, score = cand, over
            if best:
                taken.add(best)
            return best
        return adopt

    # ---- the master and the layouts (theme_sync)

    def plan_theme(self, mplan: dict, pres: dict) -> None:
        """The theme's part of the merge, planned beside the slides' and reported with them. A
        deck `adopt` took over has a theme of the person's own and is left alone; a base from
        before theme sync records nothing of the layouts, so they are left alone too (said so when
        the source's theme changed)."""
        from . import theme_sync
        from .adopt_sync import ORIGIN

        self.theme_plan = None
        if self.base.get("origin") == ORIGIN:
            return
        if self.theme_side is None:
            self.theme_side = theme_sync.ours_side(self.ours)
        report = mplan["report"]
        if not self.base.get("theme"):
            why = theme_sync.old_base_warning(self.base, self.theme_side)
            if why:
                report["warnings"].append(why)
            return
        tp = theme_sync.plan(self.base, self.theme_side, self.ours, pres, self.tok, self.picture_url,
                             lambda page: f"b2s_th_{h6(page)}_{self.tok}", pictures=self.live_pictures(pres))
        self.theme_plan = tp
        report["applied"] += tp["applied"]
        report["conflicts"] += tp["conflicts"]
        report["warnings"] += tp["warnings"]

    def master_key(self) -> str | None:
        """The background a slide shows by inheriting the master: the new source's shared one once
        the base records the theme (this sync writes it there, or the person's own stays, which a
        slide inheriting it shows either way), else what convert put there."""
        if self.base.get("theme") and self.theme_side is not None:
            return self.theme_side["shared"]
        return self.base.get("master_background")

    def prepare(self, mplan: dict, pres: dict, theirs: dict) -> dict:
        """What to write, per slide: units to (re)create with their requests' inputs, deletions,
        moves, backgrounds, notes; pictures needed from the staging deck."""
        ours_slides = self.plan.deck["slides"]
        new_ids = {p["key"]: f"b2s_{h6(p['key'])}_{self.tok}" for p in mplan["slides"] if p["action"] == "create"}
        # Internal links: PDF page -> live slide (existing, or created now).
        kept = sorted((ours_slides[p["ours"]]["page"], p["objectId"] or new_ids.get(p["key"]))
                      for p in mplan["slides"] if p.get("ours") is not None and p["action"] in ("update", "create"))
        page_slide = {}
        if kept:
            for page in range(kept[-1][0] + 1):
                page_slide[page] = next(sid for pg, sid in kept if pg >= page)
        self.plan.page_slide = page_slide
        master = self.master_key()
        work = {"slides": [], "pictures": {}, "new_ids": new_ids, "page_slide": page_slide,
                "writes": merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])}
        if self.theme_plan:
            work["pictures"].update(self.theme_plan["stage"])
            work["writes"] = work["writes"] or bool(self.theme_plan["requests"] or self.theme_plan["cleanup"])
        for p in mplan["slides"]:
            w = {"plan": p, "units": []}
            if p["action"] == "create":
                w["sid"] = new_ids[p["key"]]
                w["units"] = list(range(len(ours_slides[p["ours"]]["elements"])))
            elif p["action"] == "update":
                w["sid"] = p["objectId"]
                index = {e["key"]: k for k, e in enumerate(self.ours["slides"][p["ours"]]["elements"])}
                w["units"] = [index[k] for u in p["units"] if u["action"] in ("create", "recreate") for k in u["ours_members"]]
            if w["units"]:
                slide = ours_slides[p["ours"]]
                for i in w["units"]:
                    if slide["elements"][i]["kind"] == "image":
                        work["pictures"][str(self.ours["out"] / slide["elements"][i]["file"])] = None
            background = p.get("background") or (self.ours["slides"][p["ours"]]["background"] if p["action"] == "create" else "")
            if background.startswith("png:") and background != master:
                work["pictures"][str(self.ours["out"] / ours_slides[p["ours"]]["background"])] = "background"
            work["slides"].append(w)
        work["order"] = [new_ids.get(x[4:], x) if x.startswith("new:") else x for x in mplan["order"]]
        return work

    # ---- pictures

    def in_background(self, fn, name: str = "b2s-work"):
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

    def picture_url(self, path) -> str:
        """The staging deck's URL for a picture, or a marker `fill_urls` replaces before the batch
        goes out.

        The requests are built while the staging deck is still being imported (`write`), and this is
        the **one** field in them that waits for it: every object id a picture brings is the
        element's own (`new_oid`), so what the pending marker has to record - the objects this run
        is about to create - does not wait for Drive at all."""
        return self.urls.get(str(path)) or f"{PENDING_URL}{path}"

    def fill_urls(self, reqs: list[dict]) -> None:
        """Put the staging deck's URLs into the requests built before it existed. A picture the
        staging deck did not bring fails here, loudly, rather than as a batch Google refuses:
        nothing goes out carrying a marker."""
        def fill(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    node[k] = self.urls[v[len(PENDING_URL):]] \
                        if isinstance(v, str) and v.startswith(PENDING_URL) else fill(v)
            elif isinstance(node, list):
                node[:] = [fill(v) for v in node]
            return node

        fill(reqs)   # every marker, wherever it sits: a picture's URL and a slide background's

    def stage_in_background(self, work: dict):
        """`stage` on a thread of its own, or None where there is nothing to stage or it has to be
        run here (`in_background`)."""
        if not [f for f in work["pictures"] if f not in self.urls]:
            return None
        return self.in_background(lambda slides, drive: self.stage(work, drive, slides), "b2s-stage")

    def drop_staging(self, fid: str) -> None:
        """The staging deck has done its work once the live deck holds the pictures, and deleting it
        is the one thing at the end of a write nothing waits for - so it goes on a thread and this
        run carries on. Collected before `sync` returns (`await_deletes`), never left to the
        interpreter: a file nobody deletes is one somebody finds in their Drive."""
        self.urls.clear()
        self.staging = None
        fut = self.in_background(lambda slides, drive: execute(drive.files().delete(fileId=fid)), "b2s-drop")
        if fut is None:
            execute(self.drive.files().delete(fileId=fid))
        else:
            self.deleting.append(fut)

    def await_deletes(self) -> None:
        """Wait for the staging decks this run sent away (`drop_staging`); their failures are the
        caller's to hear about, not to stop on - the deck is written either way."""
        for fut in self.deleting:
            with contextlib.suppress(Exception):
                fut.result()
        self.deleting = []

    def stage(self, work: dict, drive=None, slides=None) -> str | None:
        """Pictures go through a staging deck imported from a .pptx; its images' contentUrls are
        then used in the live deck. Returns the staging file's id: delete it once the live deck
        has the pictures (the URLs stop working with it).

        `drive` / `slides`: the clients to use, where this runs on a thread of its own
        (`stage_in_background`) and may not touch the ones this sync is using meanwhile."""
        from .emit import PPTX_MIME, build_pptx
        from .gapi import media_upload

        drive, slides = drive or self.drive, slides or self.slides
        needed = [f for f in work["pictures"] if f not in self.urls]
        if not needed:
            return None
        page_w, page_h = self.plan.deck["slides"][0]["size"]
        pages = []
        pictures = [f for f in needed if work["pictures"][f] != "background"]
        backgrounds = [f for f in needed if work["pictures"][f] == "background"]
        for k in range(0, len(pictures), 40):
            pages.append({"layout": "BLANK", "fill": None, "templates": False, "pictures": [
                {"file": f, "bbox": [0, 0, *self._fit(f)], "alt": f"b2s-stage:{k + n}", "title": "stage"}
                for n, f in enumerate(pictures[k:k + 40])]})
        for f in backgrounds:
            pages.append({"layout": "BLANK", "fill": {"picture": Path(f)}, "templates": False, "pictures": []})
        pptx = build_pptx(page_w, page_h, [], pages, {"color": "#ffffff"})
        # The marker outlives a killed sync: nothing deletes a staging deck from a file's word, so
        # `tools/drive_usage.py` needs to be able to say which files are certainly leftovers.
        fid = execute(drive.files().create(body={"name": "beamer2slides sync staging (temporary)",
                                                 "mimeType": "application/vnd.google-apps.presentation",
                                                 "appProperties": {"b2sStaging": self.pid}},
                                           media_body=media_upload(pptx, PPTX_MIME), fields="id"))["id"]
        try:
            staged = execute(slides.presentations().get(presentationId=fid))
            made = staged.get("slides", [])
            for s in made[:len(pages) - len(backgrounds)]:
                for e in s.get("pageElements", []):
                    d = e.get("description") or ""
                    if "image" in e and d.startswith("b2s-stage:"):
                        self.urls[pictures[int(d[10:])]] = e["image"]["contentUrl"]
            for s, f in zip(made[len(pages) - len(backgrounds):], backgrounds):
                fill = s.get("pageProperties", {}).get("pageBackgroundFill", {})
                if "stretchedPictureFill" in fill:
                    self.urls[f] = fill["stretchedPictureFill"]["contentUrl"]
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

    def measure_places(self, work: dict, theirs: dict) -> tuple[dict, list[str]]:
        from .emit import measure_places, slide_holes

        if not self.measure:
            return {}, []
        slides = []
        for w in work["slides"]:
            if not w["units"]:
                continue
            slide = self.plan.deck["slides"][w["plan"]["ours"]]
            ids = {slide["elements"][i]["id"] for i in w["units"]}
            if any(h[3] is not None and h[3]["id"] in ids for h in slide_holes(slide)) or \
                    any(e.get("marks") and e["id"] in ids for e in slide["elements"]):
                slides.append(slide)
        if not slides:
            return {}, []
        self.before_write()   # it adds scratch slides to the deck (b2s_mNNN)
        live_ids = {s["objectId"] for s in theirs["slides"]}
        first = theirs["slides"][0]["objectId"]
        page_slide = {k: v if v in live_ids else first for k, v in self.plan.page_slide.items()}
        return measure_places(self.slides, self.pid, {**self.plan.deck, "slides": slides}, self.scale, self.plan.fonts,
                              self.plan.placed, page_slide, self.ours["out"], self.plan.page_width)

    def delete_scratch(self, scratch: list[str]) -> None:
        self.before_write()
        try:
            execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body={
                "requests": [{"deleteObject": {"objectId": s}} for s in scratch]}))
        except HttpError as e:
            self.warnings.append(f"could not delete scratch slides {scratch}: {e}")

    # ---- content

    def main_requests(self, work: dict, theirs: dict, pres: dict, moves: dict, scratch: list[str]
                      ) -> tuple[list[dict], list[dict]]:
        """(content, cleanup). Nothing in `content` destroys anything a person could have edited:
        it creates the new objects, refills placeholders and puts the slides in order. Every
        deletion - the objects a recreated unit replaces, the slides the source removed, the plain
        stand-in shapes - goes into `cleanup`, which is sent after the deck's own edits are back on
        the new objects and a base that no longer mentions the old ones is stored."""
        live = {s["objectId"]: s for s in theirs["slides"]}
        layouts = {l.get("layoutProperties", {}).get("name"): l for l in pres.get("layouts", [])}
        reqs: list[dict] = []
        doomed_slides: list[str] = []
        if self.theme_plan:
            # The master and the layouts first, in the same chain of batches: a layout write and a
            # slide batch in flight together can undo each other's placeholder boxes (the last
            # commit wins), and these go out one after another.
            if self.theme_plan["requests"]:
                reqs += [*self.theme_plan["requests"], BREAK]
            self.cleanup_ids = list(dict.fromkeys([*getattr(self, "cleanup_ids", ()), *self.theme_plan["cleanup"]]))
        for w in work["slides"]:
            p = w["plan"]
            if p["action"] == "delete":
                doomed_slides.append(p["objectId"])
            elif p["action"] == "create":
                reqs += self.new_slide(w, layouts, moves, pres) + [BREAK]
            elif p["action"] == "update":
                reqs += self.update_slide(w, live[p["objectId"]], moves, pres) + [BREAK]
        reqs += [{"deleteObject": {"objectId": s}} for s in scratch]  # (sync's own scratch slides)
        # Slide order: created slides were appended.
        current = [s["objectId"] for s in theirs["slides"] if s["objectId"] not in set(doomed_slides)]
        current += [w["sid"] for w in work["slides"] if w["plan"]["action"] == "create"]
        final = [s for s in dict.fromkeys(work["order"]) if s in current]  # (an id can't be in two places)
        for i, sid in enumerate(final):
            if current[i] != sid:
                reqs.append({"updateSlidesPosition": {"slideObjectIds": [sid], "insertionIndex": i}})
                current.remove(sid)
                current.insert(i, sid)
        self.cleanup_ids = list(dict.fromkeys([*getattr(self, "cleanup_ids", ()), *doomed_slides]))
        cleanup = [{"deleteObject": {"objectId": oid}} for oid in self.cleanup_ids]
        return reqs, cleanup

    def slide_requests(self, w: dict, sid: str, in_place: dict[int, dict], templates: dict[tuple, dict],
                       moves: dict, new_slide: bool, ungrouped: set[int] = frozenset()
                       ) -> tuple[list[dict], dict[int, list[str]], dict[int, str], list[dict]]:
        """emit's requests for the chosen elements of an ours slide, under live object ids.
        in_place: element index -> {"id", "size"} of a live placeholder it goes into; templates:
        template key -> {"id", "w", "h", "text"} of a live object (or stand-in) to duplicate;
        ungrouped: element indices whose own group (with anchored pictures) isn't made.
        Returns (requests, element index -> objects created, element index -> new object id,
        extras: the slide-level requests (groups, z-order) for a new slide)."""
        from .emit import title_element, subtitle_element

        o = self.ours["slides"][w["plan"]["ours"]]
        slide = self.plan.deck["slides"][w["plan"]["ours"]]
        n = slide["page"]
        vsid = f"b2s_s{n:03}"
        # A title with no live placeholder to go into becomes a text box.
        elements = [dict(e) for e in slide["elements"]]
        title_idx = title_element(slide)
        demoted = title_idx is not None and title_idx not in in_place
        if demoted:
            elements[title_idx]["role"] = "body"
        slide_copy = {**slide, "elements": elements}
        title_idx = title_element(slide_copy)
        sub_idx = subtitle_element(slide_copy, title_idx) if title_idx is not None else None
        if sub_idx is not None and sub_idx not in in_place:
            slide_copy["title_page"] = False
        page_elements = {vsid: [{"objectId": f"{vsid}_t{i}", "size": {"width": emu(v["size"][0]), "height": emu(v["size"][1])}}
                                for i, v in in_place.items() if not v.get("table")]}
        keys = self.plan.keys
        sizes = [(templates[k]["w"], templates[k]["h"]) if k in templates else (STAND_IN, STAND_IN) for k in keys]
        parts, element_ids = self.plan.slide_parts(slide_copy, page_elements, {}, moves, sizes)
        if demoted and not new_slide:
            # The other elements as on a slide with its title (a title demoted to body text
            # would widen their right limits, emit.text_right_limit): a stand-in placeholder size.
            orig_title = title_element(slide)
            orig_sub = subtitle_element(slide, orig_title)
            stand = [{"objectId": f"{vsid}_t{i}", "size": {"width": emu(STAND_IN), "height": emu(STAND_IN)}}
                     for i in (orig_title, orig_sub) if i is not None and i not in in_place]
            full, _ = self.plan.slide_parts(slide, {vsid: page_elements[vsid] + stand}, {}, moves, sizes)
            demoted_idx = {i for i in (orig_title, orig_sub) if i is not None and i not in in_place}
            parts = [full[0]] + [parts[1 + i] if i in demoted_idx else full[1 + i] for i in range(len(element_ids))] + \
                parts[1 + len(element_ids):]
        mapping = {}
        new_oid = {}
        for i, (vid, e) in enumerate(zip(element_ids, o["elements"])):
            new_oid[i] = in_place[i]["id"] if i in in_place else f"b2s_{h6(o['key'])}_{h6(e['key'])}_{self.tok}"
            mapping[vid] = new_oid[i]
        for j, k in enumerate(keys):
            if k in templates:
                mapping[f"{vsid}_k{j}"] = templates[k]["id"]
        mapping[vsid] = sid
        order = sorted(mapping.items(), key=lambda kv: -len(kv[0]))
        chosen = set(w["units"])
        reqs, objects = [], {}
        from .emit import created_ids
        for i, (el, rs) in enumerate(parts[1:1 + len(element_ids)]):
            if i not in chosen:
                continue
            rs = rename(rs, order)
            if not new_slide:
                rs = [r for r in rs if "updatePageElementsZOrder" not in r]
            if el["kind"] == "image":
                path = self.ours["out"] / el["file"]
                box = [v * self.scale for v in self.plan.placed(slide["elements"][i], n)["bbox"]]
                x0, y0, x1, y1 = box
                rs = [{"createImage": {"objectId": new_oid[i], "url": self.picture_url(path), "elementProperties": {
                    "pageObjectId": sid, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU", "translateX": round(x0 * EMU_PER_PT),
                                  "translateY": round(y0 * EMU_PER_PT)}}}},
                      letterbox_fix(new_oid[i], box, png_size(path))] + rs
            if i in in_place and in_place[i].get("table"):
                # Refilled where it is (`table_refill`): its cells emptied, then filled as emit
                # fills a table the .pptx brought, whose margins this one has.
                from .emit import table_requests
                rs = [{"deleteText": {"objectId": new_oid[i], "cellLocation": {"rowIndex": r, "columnIndex": c},
                                      "textRange": {"type": "ALL"}}} for r, c in in_place[i]["cells"]] + \
                     [{k: {**v, "tableObjectId": new_oid[i]} for k, v in step.items()} for step in in_place[i].get("steps", [])] + \
                     [r for r in table_requests(self.plan.placed(slide["elements"][i], n), sid, new_oid[i], self.scale,
                                                self.plan.fonts, imported=True) if "updatePageElementsZOrder" not in r]
                dx, dy = in_place[i].get("shift") or (0.0, 0.0)
                if abs(dx) > 0.01 or abs(dy) > 0.01:  # (the source moved it)
                    rs.append({"updatePageElementTransform": {"objectId": new_oid[i], "applyMode": "RELATIVE", "transform": {
                        "scaleX": 1, "scaleY": 1, "unit": "EMU",
                        "translateX": round(dx * EMU_PER_PT), "translateY": round(dy * EMU_PER_PT)}}})
            elif i in in_place and in_place[i].get("text"):
                rs = [{"deleteText": {"objectId": new_oid[i], "textRange": {"type": "ALL"}}}] + rs
            out = []
            for r in rs:  # a duplicated live object brings its text along: clear it
                out.append(r)
                if "duplicateObject" in r:
                    src = r["duplicateObject"]["objectId"]
                    tpl = next((t for t in templates.values() if t["id"] == src), None)
                    if tpl and tpl.get("text"):
                        out += [{"deleteText": {"objectId": v, "textRange": {"type": "ALL"}}}
                                for v in r["duplicateObject"]["objectIds"].values()]
            reqs += out
            objects[i] = list(dict.fromkeys([new_oid[i]] + [x for x in created_ids(rs) if x != new_oid[i]]))
        extras = []
        index = {vid: i for i, vid in enumerate(element_ids)}
        for _, rs in parts[1 + len(element_ids):]:
            for r in rename(rs, order):
                if "groupObjects" in r:
                    gid = r["groupObjects"]["groupObjectId"]
                    owner = next((i for i, v in new_oid.items() if gid == f"{v}_g"), None)
                    if owner is not None and owner in chosen:
                        if owner in ungrouped:
                            continue  # (the deck took this unit's group apart: it stays so)
                        reqs.append(r)
                        objects[owner].append(gid)
                        continue
                extras.append(r)
        return reqs, objects, new_oid, extras

    def tag_requests(self, o: dict, objects: dict[int, list[str]], new_oid: dict[int, str], in_place: dict) -> list[dict]:
        slide = self.plan.deck["slides"][self.ours["slides"].index(o)]
        reqs = []
        for i, oids in objects.items():
            e, el = o["elements"][i], slide["elements"][i]
            if el["kind"] == "diagram" and len(oids) > 1:
                # A diagram's own object id *is* the group emit builds its nodes and lines under
                # (emit.diagram_requests). The API refuses updatePageElementAltText on a group and
                # rejects the whole batch with it, so a sync that rewrote a diagram slide died. It
                # goes untagged, as it does in a converted deck (snapshot.tag_requests skips element
                # groups too); the base names its objects itself, so nothing needs the tag.
                continue
            r = {"objectId": new_oid[i], "title": snapshot.tag(o["key"], e["key"])}
            if el["kind"] == "image" and el.get("alt"):
                r["description"] = el["alt"]
            reqs.append({"updatePageElementAltText": r})
        return reqs

    def new_layout(self, slide: dict, layout_name: str, layouts: dict, pres: dict) -> dict | None:
        """The layout a new slide is created on. In a themed deck emit puts the theme decoration
        on the layouts (emit.plan_theme, `theme.layouts` in emit.json) and gives backgrounds that
        don't show it a copy of their layout without it. A new slide that inherits the master
        background takes the plain layout (the decorated one); one with a background picture of its
        own already carries whatever decoration it shows, so it goes on the same layout as the
        converted slides with that background - found by object id, since the copies' names in the
        deck are the .pptx ones ("Title Only (no theme)"), not the b2s names."""
        key = snapshot.background_key(slide, self.ours["out"])
        served = (self.theme_plan or {}).get("page_group") or {}
        if served and self.theme_side:
            # The layout that serves the decoration a fresh conversion gives this slide now
            # (theme_sync.plan), of the kind it needs: the plain one first, then its copies.
            want = self.theme_side["groups"].get(slide["page"])
            kinds = {b.get("layoutObjectId"): b.get("layout") for b in self.base["slides"]}
            plain = layouts.get(layout_name)
            if plain is not None and served.get(plain["objectId"]) == want:
                return plain
            for l in pres.get("layouts", []):
                if served.get(l["objectId"]) == want and kinds.get(l["objectId"]) == layout_name:
                    return l
        if key != self.master_key():
            by_id = {l["objectId"]: l for l in pres.get("layouts", [])}
            same = [b for b in self.base["slides"] if b.get("background") == key
                    and b.get("layout") == layout_name and b.get("layoutObjectId") in by_id]
            if same:
                return by_id[same[0]["layoutObjectId"]]
        return layouts.get(layout_name) or layouts.get("BLANK")

    def new_slide(self, w: dict, layouts: dict, moves: dict, pres: dict) -> list[dict]:
        from .emit import element_template_keys, slide_layout, subtitle_element, title_element

        p = w["plan"]
        o = self.ours["slides"][p["ours"]]
        slide = self.plan.deck["slides"][p["ours"]]
        sid = w["sid"]
        layout_name, title_kind = slide_layout(slide)
        layout = self.new_layout(slide, layout_name, layouts, pres)
        if layout is None:
            raise RuntimeError(f"the deck has no {layout_name} layout for new slide {o['key']}")
        mappings, in_place, unused = [], {}, []
        title_idx = title_element(slide)
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        for k, e in enumerate(layout.get("pageElements", [])):
            ph = e.get("shape", {}).get("placeholder")
            if not ph:
                continue
            size = [snapshot._unit(e["size"]["width"]), snapshot._unit(e["size"]["height"])] if "size" in e else [STAND_IN, STAND_IN]
            if ph.get("type") == title_kind and title_idx is not None and title_idx not in in_place:
                oid = f"b2s_{h6(o['key'])}_{h6(o['elements'][title_idx]['key'])}_{self.tok}"
                in_place[title_idx] = {"id": oid, "size": size}
            elif ph.get("type") == "SUBTITLE" and sub_idx is not None and sub_idx not in in_place:
                oid = f"b2s_{h6(o['key'])}_{h6(o['elements'][sub_idx]['key'])}_{self.tok}"
                in_place[sub_idx] = {"id": oid, "size": size}
            else:
                continue  # (not every layout placeholder is instantiated: the rest go after a read, in finish)
            mappings.append({"layoutPlaceholder": {"type": ph["type"], "index": ph.get("index", 0)}, "objectId": oid})
        reqs = [{"createSlide": {"objectId": sid, "slideLayoutReference": {"layoutId": layout["objectId"]},
                                 "placeholderIdMappings": mappings}}]
        reqs += self.background_requests(sid, o["background"], slide, pres, created=True)
        templates = {}
        if self.plan.uses_templates[slide["page"]]:
            for j, key in enumerate(self.plan.keys):
                stand = f"b2s_{h6(o['key'])}_k{j}_{self.tok}"
                templates[key] = {"id": stand, "w": STAND_IN, "h": STAND_IN, "stand_in": True}
                reqs.append(stand_in_request(stand, sid, key))
            used = {k for e in slide["elements"] for k in element_template_keys(e, self.scale)}
            if used:
                self.warnings.append(f"slide {o['key']}: {len(used)} template shape(s) (shadows, exact corners) made as plain shapes")
        # Pictures first, as the .pptx brings them; emit's parts then order everything.
        rs, objects, new_oid, extras = self.slide_requests(w, sid, in_place, templates, moves, True)
        reqs += [r for r in rs if "createImage" in r]  # (a picture's other requests follow in element order)
        reqs += [r for r in rs if "createImage" not in r] + extras
        w["objects"], w["new_oid"], w["in_place"], w["tops"], w["doomed"] = objects, new_oid, in_place, {}, set()
        w["groups"] = [r["groupObjects"]["groupObjectId"] for r in extras if "groupObjects" in r]
        reqs += self.tag_requests(o, objects, new_oid, in_place)
        return reqs

    def update_slide(self, w: dict, read: dict, moves: dict, pres: dict) -> list[dict]:
        from .emit import element_template_keys, label_inside, node_template_key, bend_template_key, template_key

        p = w["plan"]
        b = self.base["slides"][p["base"]]
        o = self.ours["slides"][p["ours"]]
        slide = self.plan.deck["slides"][p["ours"]]
        sid = p["objectId"]
        objects = read["objects"]
        bunits = merge.units(b["elements"])
        reqs: list[dict] = []
        w["objects"], w["new_oid"], w["in_place"], w["groups"] = {}, {}, {}, []
        recreated = [u for u in p["units"] if u["action"] in ("create", "recreate")]
        index = {e["key"]: k for k, e in enumerate(o["elements"])}

        # Placeholders: a recreated title goes back into its live placeholder.
        in_place = {}
        for u in recreated:
            for mk in u["ours_members"]:
                i = index[mk]
                base_el = next((m for m in bunits.get(u["key"], []) if m["key"] == mk), None)
                main = base_el.get("main") if base_el else None
                if main and main in objects and objects[main].get("placeholder"):
                    in_place[i] = {"id": main, "size": objects[main]["size"], "text": (objects[main].get("text") or "").strip()}
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
                  if i != (title_idx if objects[v["id"]].get("placeholder") in ("TITLE", "CENTERED_TITLE") else
                           sub_idx if objects[v["id"]].get("placeholder") == "SUBTITLE" else None)]:
            del in_place[i]
        # A new title (the source's title changed beyond recognition) or a text that took the
        # subtitle role goes into the placeholder of the element it replaces, when that one goes.
        for role, kinds in ((title_idx, ("TITLE", "CENTERED_TITLE")), (sub_idx, ("SUBTITLE",))):
            if role is None or role in in_place or not any(role == index[mk] for u in recreated for mk in u["ours_members"]):
                continue
            taken = {v["id"] for v in in_place.values()}
            for u in p["units"]:
                if u["action"] not in ("delete", "recreate"):
                    continue
                main = next((m.get("main") for m in bunits.get(u["key"], [])[:1]), None)
                if main and main in objects and main not in taken and objects[main].get("placeholder") in kinds:
                    in_place[role] = {"id": main, "size": objects[main]["size"],
                                      "text": (objects[main].get("text") or "").strip()}
                    break
        # A table whose words alone changed is refilled where it is (`table_refill`) - but not one an
        # interrupted sync already rewrote: what the merge compares against is then the person's
        # version put back (`restore_in_place`), not what the table holds, and cells emptied or rows
        # inserted by that picture would double words or rows. Made again, it is right whatever it holds.
        restored = set((getattr(self, "recovery", None) or {}).get("restore") or ())
        for u in recreated:
            if u["action"] == "recreate" and len(u["ours_members"]) == 1 and len(bunits.get(u["key"], [])) == 1 \
                    and bunits[u["key"]][0].get("main") not in restored:
                i = index[u["ours_members"][0]]
                refill = table_refill(bunits[u["key"]][0], slide["elements"][i], objects, self.scale, self.plan.fonts)
                if refill:
                    if "geometry" in (u.get("overrides") or {}) and "position" not in u.get("source", ()):
                        # The deck moved it and the source did not: where it is now is what a
                        # recreation's geometry override would give. When both moved it, the source's
                        # move goes on top of the person's place, as `carried` does for a recreation.
                        refill["shift"] = (0.0, 0.0)
                    in_place[i] = {**refill, "table": True}

        # Template shapes: duplicate a live object with the same key on this slide, else a stand-in.
        needed = {k for u in recreated for mk in u["ours_members"] for k in element_template_keys(slide["elements"][index[mk]], self.scale)}
        templates, stand_ins = {}, []
        if needed:
            for el in b["elements"]:
                ir, main = el.get("ir") or {}, el.get("main")
                if not main:
                    continue
                found = []
                if ir.get("kind") == "shape" and template_key(ir, self.scale):
                    found.append((template_key(ir, self.scale), main))
                elif ir.get("kind") == "diagram":
                    found += [(node_template_key(nd), f"{main}_n{j}") for j, nd in enumerate(ir["nodes"]) if nd["shape"] and label_inside(nd)]
                    found += [(bend_template_key(ln), f"{main}_l{j}") for j, ln in enumerate(ir["lines"]) if ln.get("bend")]
                for key, oid in found:
                    if key in needed and key not in templates and oid in objects:
                        templates[key] = {"id": oid, "w": objects[oid]["size"][0], "h": objects[oid]["size"][1],
                                          "text": (objects[oid].get("text") or "").strip()}
            for j, key in enumerate(self.plan.keys):
                if key in needed and key not in templates:
                    stand = f"b2s_{h6(o['key'])}_k{j}_{self.tok}"
                    templates[key] = {"id": stand, "w": STAND_IN, "h": STAND_IN, "stand_in": True}
                    stand_ins.append(stand)
                    reqs.append(stand_in_request(stand, sid, key))
                    self.warnings.append(f"slide {o['key']}: no live {key[0]} to copy (shadow, exact corners): made a plain shape")

        # Groups the old objects were in (blocks, and groups the deck made around them): ungrouped
        # first, outermost first (a group inside a group can't be ungrouped), and regrouped with the
        # new objects under the same ids, innermost first.
        regroup, depth, roots_removed = self.regroups(p["units"], bunits, read)
        reqs = [{"ungroupObjects": {"objectIds": [g]}} for g in sorted(regroup, key=lambda g: depth[g])] + reqs

        # A unit whose group the deck took apart (its pictures and text still there) is rebuilt ungrouped.
        ungrouped = set()
        for u in recreated:
            anchor = (bunits.get(u["key"]) or [{}])[0]
            gid = next((x for x in anchor.get("objects", []) if x == f"{anchor.get('main')}_g"), None)
            if u["action"] == "recreate" and u["key"] in index and gid and gid not in objects and anchor.get("main") in objects:
                ungrouped.add(index[u["key"]])
        rs, created, new_oid, _ = self.slide_requests({**w, "units": [index[mk] for u in recreated for mk in u["ours_members"]]},
                                                      sid, in_place, templates, moves, False, ungrouped)
        reqs += rs
        w["objects"], w["new_oid"], w["in_place"] = created, new_oid, in_place
        # Old objects out - in the cleanup phase, once their replacements are written and the
        # deck's own edits are back on them (a placeholder refilled in place stays).
        keep_ids = {v["id"] for v in in_place.values()}
        doomed = [r for _, roots in roots_removed for r in roots if r not in keep_ids]
        w["doomed"] = set(doomed) | {c for r in doomed for c in merge._descendants(r, read)}
        # (getattr: the offline tests drive this method on a bare Sync object)
        self.cleanup_ids = [*getattr(self, "cleanup_ids", ()), *doomed, *stand_ins]
        # The text of a placeholder this sync overwrites: recorded so an interrupted run can be
        # told what the person had there (plan_recovery / restore_in_place).
        saved = getattr(self, "in_place_readback", None)
        if saved is None:
            saved = self.in_place_readback = {}
        for v in in_place.values():
            rb = objects.get(v["id"])
            if rb:
                saved[v["id"]] = {k: rb[k] for k in IN_PLACE_FIELDS if k in rb}
        # Regroup: the unit's new top object takes its old root's place among the children.
        tops = {}
        for u in recreated:
            if u["key"] in index:
                i = index[u["key"]]
                oids = created.get(i, [])
                tops[u["key"]] = next((x for x in oids if x.endswith("_g") and x[:-2] == new_oid[i]), new_oid[i])
        reqs += self.regroup_requests(regroup, depth, objects, tops, keep_ids, self.zrank(o, bunits, tops))
        # Moves: the deck object goes where the source moved the element.
        reqs += self.move_requests(p["units"], bunits, read, self.scale)
        reqs += self.tag_requests(o, created, new_oid, in_place)
        if p.get("background"):
            reqs += self.background_requests(sid, p["background"], slide, pres)
        if p.get("notes") is not None and read.get("notes_id"):
            if read.get("notes"):
                reqs.append({"deleteText": {"objectId": read["notes_id"], "textRange": {"type": "ALL"}}})
            if p["notes"]:
                reqs.append({"insertText": {"objectId": read["notes_id"], "text": p["notes"]}})
        w["tops"] = tops
        return reqs

    @staticmethod
    def zrank(o: dict, bunits: dict, tops: dict) -> dict[str, int]:
        """Object id -> where the source draws that element, for every object a rewrite may put into
        a group: the ones this sync writes (`tops`) and the deck's own, which stand for the elements
        the sync is keeping."""
        pos = {e["key"]: i for i, e in enumerate(o["elements"])}
        rank = {oid: pos[m["key"]] for members in bunits.values() for m in members
                for oid in m.get("objects", []) if m["key"] in pos}
        rank.update({oid: pos[k] for k, oid in tops.items() if k in pos})
        return rank

    @staticmethod
    def regroups(units: list[dict], bunits: dict, read: dict) -> tuple[dict, dict, list]:
        """The groups a slide's rewrite takes apart: group id -> {"remove": old roots of rewritten
        units in it, "unit_of": root -> unit key}, with every ancestor of such a group (empty), their
        depth, and (unit key, old root ids) of every unit rewritten or deleted."""
        objects = read["objects"]

        def ancestors(g: str) -> list[str]:
            chain, x = [], objects.get(g, {}).get("parent_group")
            while x and x not in chain:
                chain.append(x)
                x = objects.get(x, {}).get("parent_group")
            return chain
        regroup: dict[str, dict] = {}
        roots_removed: list[tuple[str, list[str]]] = []
        for u in units:
            if u["action"] not in ("recreate", "delete"):
                continue
            members = bunits.get(u["key"], [])
            roots = merge.unit_roots(members, read)
            roots_removed.append((u["key"], roots))
            for r in roots:
                g = objects[r].get("parent_group")
                if g and g not in roots:
                    regroup.setdefault(g, {"remove": set(), "unit_of": {}})["remove"].add(r)
                    regroup[g]["unit_of"][r] = u["key"]
                    for a in ancestors(g):
                        regroup.setdefault(a, {"remove": set(), "unit_of": {}})
        return regroup, {g: len(ancestors(g)) for g in regroup}, roots_removed

    @staticmethod
    def regroup_requests(regroup: dict, depth: dict, objects: dict, tops: dict, keep_ids: set,
                         rank: dict | None = None) -> list[dict]:
        """The groups `regroups` took apart, made again under the same ids, innermost first: a
        rewritten unit's new top object takes its old root's place among the children (`tops`: unit
        key -> new top; `objects`: the read-back before the rewrite; `rank`: object id -> where the
        source draws that element, for every child this sync writes and every one it keeps)."""
        reqs = []
        rank = rank or {}
        replaced: dict[str, str | None] = {}  # regrouped group -> what stands for it now (None: gone)
        for g in sorted(regroup, key=lambda g: -depth[g]):
            info = regroup[g]
            children, mine = [], {}   # mine: place among the children -> the object standing there
            for c in objects[g].get("children", []):
                if c in info["remove"]:
                    ukey = info["unit_of"][c]
                    if ukey in tops and tops[ukey] not in children and tops[ukey] not in keep_ids:
                        mine[len(children)] = tops[ukey]
                        children.append(tops[ukey])
                elif c in replaced:
                    if replaced[c]:
                        children.append(replaced[c])
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
                reqs += [{"updatePageElementsZOrder": {"pageElementObjectIds": [c], "operation": "BRING_TO_FRONT"}}
                         for c in children]
                reqs.append({"groupObjects": {"groupObjectId": g, "childrenObjectIds": children}})
                replaced[g] = g
            else:
                replaced[g] = children[0] if children else None
        return reqs

    def background_requests(self, sid: str, key: str, slide: dict, pres: dict, created: bool = False) -> list[dict]:
        master = self.master_key()
        if key == master:
            if created:
                return []  # emit leaves these slides inheriting the master, and a new slide already does
            if self.base.get("theme"):
                # The master may be getting a new background in this very sync (theme_sync), so the
                # slide goes back to inheriting it rather than copying today's. A layout page refuses
                # INHERIT; a slide takes it, named by `propertyState` alone (tools/probe: the whole
                # `pageBackgroundFill` as the field is refused).
                page = next((s for s in pres.get("slides", []) if s["objectId"] == sid), None)
                if page is not None and snapshot.background(page) == {"state": "INHERIT"}:
                    return []
                return [{"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.propertyState",
                                                  "pageProperties": {"pageBackgroundFill": {"propertyState": "INHERIT"}}}}]
            fill = pres["masters"][0].get("pageProperties", {}).get("pageBackgroundFill", {})
            if "stretchedPictureFill" in fill:
                return [{"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.stretchedPictureFill.contentUrl",
                                                  "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {
                                                      "contentUrl": fill["stretchedPictureFill"]["contentUrl"]}}}}}]
            if "solidFill" in fill:
                return [{"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.solidFill.color",
                                                  "pageProperties": {"pageBackgroundFill": {"solidFill": fill["solidFill"]}}}}]
            return []
        if key.startswith("color:"):
            return [{"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.solidFill.color",
                                              "pageProperties": {"pageBackgroundFill": {"solidFill": {
                                                  "color": api_colour(key[6:])}}}}}]
        url = self.picture_url(self.ours["out"] / slide["background"])
        return [{"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.stretchedPictureFill.contentUrl",
                                          "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": url}}}}}]

    # ---- after the content: z-order, notes of new slides, base, deck overrides

    def finish(self, work: dict, mplan: dict, theirs: dict, pres: dict, rev: str) -> str:
        raw = self.read()
        now = snapshot.read_presentation(raw)
        live = {s["objectId"]: s for s in now["slides"]}
        before = {s["objectId"]: s for s in theirs["slides"]}
        reqs = []
        for w in work["slides"]:
            p = w["plan"]
            if p["action"] == "create":
                s = live.get(w["sid"])
                mine = {x for oids in w["objects"].values() for x in oids} | set(w["groups"])
                reqs += [{"deleteObject": {"objectId": oid}} for oid, rb in (s["objects"] if s else {}).items()
                         if rb.get("placeholder") and oid not in mine]
                if s and s.get("notes_id") and self.ours["slides"][p["ours"]].get("notes"):
                    reqs.append({"insertText": {"objectId": s["notes_id"], "text": self.ours["slides"][p["ours"]]["notes"]}})
            if p["action"] == "update" and w["objects"]:
                reqs += self.restack(w, before[p["objectId"]], live[p["objectId"]])
        if reqs:
            rev = self.send("order", reqs, rev)
            raw = self.read()
            now = snapshot.read_presentation(raw)
        self.warn_about_folded_hiders(work, now)
        self.raw_after = raw   # (the layouts and the master as written: theme_sync.new_record)
        # read-back of objects as the converter created them, with the new pictures' signatures
        created = {x for w in work["slides"] for oids in (w.get("objects") or {}).values() for x in oids}
        repainted = {w.get("sid") for w in work["slides"] if w["plan"]["action"] == "create" or w["plan"].get("background")}
        snapshot.sign_pictures(now, raw, created, repainted, files=self.written_files(work), drive=self.drive)
        self.created = now
        overrides = self.override_requests(work, theirs, now, raw_objects(pres), raw_objects(raw))
        if overrides:
            rev = self.send("overrides", overrides, rev)
            rev = self.refit(work, theirs, now, rev)
        self.final_revision = rev
        self.warn_about_overruns(work, theirs)
        return rev

    def written_files(self, work: dict) -> dict[str, Path]:
        """The file each picture this sync created was made from - an image's objectId, or a
        slide's for the background picture written on it - so the new base signs them from their
        bytes (`snapshot.upload_signatures`) instead of downloading what it has just uploaded."""
        ours_slides, master, files = self.plan.deck["slides"], self.master_key(), {}
        for w in work["slides"]:
            p = w["plan"]
            if p.get("ours") is None or not w.get("sid"):
                continue
            slide = ours_slides[p["ours"]]
            for i in w.get("units") or []:
                el = slide["elements"][i]
                if el["kind"] == "image" and el.get("file") and (w.get("new_oid") or {}).get(i):
                    files[w["new_oid"][i]] = self.ours["out"] / el["file"]
            key = p.get("background") or (self.ours["slides"][p["ours"]]["background"] if p["action"] == "create" else "")
            if key.startswith("png:") and key != master and slide.get("background"):
                files[w["sid"]] = self.ours["out"] / slide["background"]
        return files

    def warn_about_overruns(self, work: dict, theirs: dict) -> None:
        """The person's own objects, which sync never moves, that the source's words or pictures
        now run over (`text_layout.overruns`): the source reflowed onto a note the person put under a
        paragraph. Moving the note would guess at what it belongs to, so it is said instead (live
        fuzz r7411, r7414). One read of the deck, only when a written slide holds such an object."""
        from . import text_layout as tl
        before = {s["objectId"]: s for s in theirs["slides"]}
        todo = {}
        for w in work["slides"]:
            p = w["plan"]
            b = before.get(p.get("objectId"))
            if p["action"] != "update" or b is None or \
                    all(u["action"] in ("keep", "none") for u in p.get("units", [])):
                continue
            users = {u["objectId"] for u in merge.user_objects(self.base["slides"][p["base"]], b)}
            if any(tl.ink(b["objects"][u]) for u in users if u in b["objects"]):
                todo[p["objectId"]] = (p["key"], users)
        if not todo:
            return
        def say(rb: dict) -> str:
            words = (rb.get("text") or "").strip().replace("\n", " ")[:40]
            return f"text {words!r}" if words else "picture"

        final = snapshot.read_presentation(self.read())
        for s in final["slides"]:
            if s["objectId"] not in todo:
                continue
            key, users = todo[s["objectId"]]
            for o in tl.overruns(before[s["objectId"]], s, users, set(self.cleanup_ids)):
                self.overruns.append({"slide": key, **o})
                mine, theirs_ = before[s["objectId"]]["objects"][o["object"]], s["objects"][o["other"]]
                self.warnings.append(
                    f"slide {key}: the source's {say(theirs_)} now runs {o['depth']:.0f} pt over your {say(mine)}, "
                    "which stays where you put it (sync never moves your own objects): move one of them")

    def refit_jobs(self, work: dict, theirs: dict, created: dict) -> dict[str, list[dict]]:
        """Slide id -> the recreated text units whose deck edits `override_requests` wrote over
        them (`refit.plan`'s jobs). Only text boxes the layout model can read (explicit sizes: what
        emit makes); placeholders and tables refilled in place are not recreated boxes."""
        from . import text_layout as tl
        pre = {s["objectId"]: s for s in created["slides"]}
        before = {s["objectId"]: s for s in theirs["slides"]}
        jobs: dict[str, list[dict]] = {}
        for w in work["slides"]:
            p = w["plan"]
            s = pre.get(p.get("objectId"))
            if p["action"] != "update" or s is None:
                continue
            o = self.ours["slides"][p["ours"]]
            ounits, bunits = merge.units(o["elements"]), merge.units(self.base["slides"][p["base"]]["elements"])
            index = {e["key"]: k for k, e in enumerate(o["elements"])}
            for u in p["units"]:
                if u["action"] != "recreate" or not u.get("overrides") or u["key"] not in index:
                    continue
                i = index[u["key"]]
                main = w["new_oid"].get(i)
                rb = s["objects"].get(main)
                if (w.get("in_place") or {}).get(i) or not rb or not tl.text_box(rb) or rb.get("placeholder") \
                        or tl.layout(rb) is None:
                    continue
                members = [m for m in ounits.get(u["key"], []) if m["key"] in index]
                pics = [w["new_oid"][index[m["key"]]] for m in members[1:]
                        if m.get("role") == "math" and index[m["key"]] in w["new_oid"]]
                own = {x for m in members for x in w["objects"].get(index[m["key"]], [])} | {main}
                old_main = (bunits.get(u["key"]) or [{}])[0].get("main")
                names = {w["new_oid"][index[m["key"]]]: m["key"] for m in members if index[m["key"]] in w["new_oid"]}
                jobs.setdefault(s["objectId"], []).append({
                    "key": f"slide {p['key']}: {u['key']}", "slide": p["key"], "names": names,
                    "text": main, "pictures": pics, "own": own,
                    "doomed": set(w.get("doomed") or ()),
                    "theirs": before.get(p["objectId"], {}).get("objects", {}).get(old_main)})
        return jobs

    def refit(self, work: dict, theirs: dict, created: dict, rev: str) -> str:
        """The recreated boxes fitted to the words written into them (`refit`, docs/project-notes.md
        "Merged text into recreated boxes"): formula pictures back over their holes, the box and a
        block panel under it as tall as the merged text needs. What moved is recorded for the base
        (`self.reshaped`, `refit.reshape_base`), and said in the report (`self.refit_moves`, report
        `refit`): which object moved how far in the converter's frame - the place the person's
        carried geometry then stands on, which the loss oracle holds the sync to."""
        from . import refit
        self.reshaped = {}
        jobs = self.refit_jobs(work, theirs, created)
        if not jobs:
            return rev
        final = snapshot.read_presentation(self.read())
        fin = {s["objectId"]: s for s in final["slides"]}
        pre = {s["objectId"]: s for s in created["slides"]}
        before = {s["objectId"]: s for s in theirs["slides"]}
        reqs, reshaped, moves = [], {}, []
        for sid, js in jobs.items():
            if sid not in fin:
                continue
            r, shaped, warnings = refit.plan(js, pre[sid], fin[sid], final.get("page_size"), before.get(sid))
            if r:
                reqs += r + [BREAK]
            reshaped.update(shaped)
            self.warnings += warnings
            moves += refit.moves(js, shaped, pre[sid], fin[sid])
        if reqs:
            rev = self.send("refit", reqs, rev)
            self.reshaped = reshaped
            self.refit_moves = moves
        return rev

    def warn_about_folded_hiders(self, work: dict, now: dict) -> None:
        """Words this sync covered and could not uncover: `folded_hiders` says why, and nothing else
        in the report would mention it, since nothing was deleted and every write went through."""
        live = {s["objectId"]: s for s in now["slides"]}
        for w in work["slides"]:
            p = w["plan"]
            s = live.get(p.get("objectId"))
            if p["action"] != "update" or not w.get("objects") or s is None:
                continue
            b = self.base["slides"][p["base"]]
            ours = {o for el in b["elements"] for o in el.get("objects", [])} | set(b.get("groups") or [])
            made = {x for oids in w["objects"].values() for x in oids} | set(w.get("groups") or [])
            for text, shape in folded_hiders(s, made, ours, w.get("doomed") or set()):
                words = (s["objects"][text].get("text") or "").strip().replace("\n", " ")[:40]
                self.warnings.append(
                    f"slide {b['key']}: {words!r} is now under a shape the source redrew, which stands in a "
                    "group you made: its page element cannot be restacked without moving the rest of that "
                    "group, so the source's order could not be followed. Ungroup it, or move the shape, "
                    "to read the words again")

    @staticmethod
    def _by_the_source(desired: list[str], oldtop: dict, tops: dict, keys: list[str], base_order: list[str],
                       stands_now: dict | None = None, stands_before: dict | None = None):
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
        second answer), and a converter group - one the base itself draws on the page - stands for
        the elements it carries, the first the source draws among them, blocks being made of
        consecutive elements. Asking it of the objects alone left out everything inside a group, so
        a block's panel could not be ordered against a table beside it however the source drew them
        (converted seed 1300381 at chain 6). A group the *person* made stands for nobody: it is not
        in the base's order, restacking it would move everything else they put in there, and that is
        the one shape of this the sync answers by talking (`warn_about_folded_hiders`)."""
        rank = {k: i for i, k in enumerate(keys)}
        page = set(base_order)

        def stands(oid, where):
            t = (where or {}).get(oid, oid)
            return t if t == oid or t in page else oid

        at, was = {}, {}
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

    def restack(self, w: dict, before: dict, now: dict) -> list[dict]:
        """BRING_TO_FRONT so recreated elements take their old place in the z-order and new
        ones follow their predecessor in the source. The objects the cleanup phase will delete are
        left out: they are still on the slide, under their replacements, until then."""
        p = w["plan"]
        doomed = w.get("doomed") or set()
        b = self.base["slides"][p["base"]]
        o = self.ours["slides"][p["ours"]]
        bunits = merge.units(b["elements"])
        top_now = {oid for oid in now["order"] if oid not in doomed}
        replace, added, oldtop = {}, [], {}
        for u in p["units"]:
            if u["action"] == "recreate":
                old = merge.unit_top(bunits[u["key"]], before)
                new = w["tops"].get(u["key"])
                if old and new:
                    replace[old] = new
                    oldtop[u["key"]] = old
            elif u["action"] == "create" and u["key"] in w["tops"]:
                added.append(u["key"])
            elif u["action"] not in ("delete", "gone") and u["key"] in bunits:
                if old := merge.unit_top(bunits[u["key"]], before):
                    oldtop[u["key"]] = old      # kept: the deck's own object stands for it
        desired = []
        for oid in before["order"]:
            oid = replace.get(oid, oid)
            if oid in top_now and oid not in desired:
                desired.append(oid)
        keys = [e["key"] for e in o["elements"]]
        shown = now.get("objects") or {}
        stands_now = drawn_order(shown, now.get("order") or [])[1]
        self._by_the_source(desired, oldtop, w["tops"], keys, b.get("order") or [], stands_now,
                            drawn_order(before.get("objects") or {}, before.get("order") or [])[1])

        def placed(k):
            return w["tops"].get(k) or (merge.unit_top(bunits[k], now) if k in bunits else None)

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
            if new not in top_now or new in desired:
                continue
            i = keys.index(ukey)
            pos = 0
            for k in reversed(keys[:i]):
                if (prev := placed(k)) in desired:
                    pos = desired.index(prev) + 1
                    break
            # ... but never above an element the source draws above it. A recreated element keeps the
            # deck's place, a new one follows the source, and where those two orders disagree - a
            # frame the source rewrote, or a label that moved onto another slide - a new opaque panel
            # would land on top of text the source puts above it, and nothing is deleted for any
            # other check to see (`loss_oracle.text_hidden`).
            for k in keys[i + 1:]:
                if (nxt := placed(k)) in desired:
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
        for el in b["elements"]:
            if el["key"] in keyset:
                for oid in el.get("objects") or []:
                    at_rank[oid] = min(at_rank.get(oid, rank[el["key"]]), rank[el["key"]])
        stand_rank: dict[str, int] = {}             # page element -> the first of them it stands for
        for oid, r in at_rank.items():
            el = stands_now.get(oid, oid)
            stand_rank[el] = min(stand_rank.get(el, r), r)
        made_rank: dict[str, int] = {}
        for k, t in w["tops"].items():
            if k in rank:
                made_rank[t] = min(made_rank.get(t, rank[k]), rank[k])
        for made in dict.fromkeys(w["tops"].values()):
            oid = stands_now.get(made, made)
            if oid not in desired:
                continue
            r = made_rank.get(made)
            drawn = {x for x, xr in at_rank.items()
                     if r is None or xr <= r or stand_rank.get(stands_now.get(x, x), r) >= r}
            i = desired.index(oid)
            for j, other in enumerate(desired[:i]):
                if would_hide(shown, made, other, drawn):
                    desired.insert(j, desired.pop(i))
                    break
        desired += [x for x in now["order"] if x not in desired and x not in doomed]
        order_now = [x for x in now["order"] if x not in doomed]
        k = 0
        while k < len(desired) and k < len(order_now) and desired[k] == order_now[k]:
            k += 1
        return [{"updatePageElementsZOrder": {"pageElementObjectIds": [x], "operation": "BRING_TO_FRONT"}} for x in desired[k:]]

    @staticmethod
    def move_requests(units: list[dict], bunits: dict, read: dict, scale: float) -> list[dict]:
        """The source's move written onto the deck's own objects. Every *root* of the unit takes the
        step: normally that is the converter's group, which carries its children, but when the person
        has taken the group apart the roots are the text box and each picture anchored to it, and
        writing on the first of them alone would leave the others where the converter put them while
        the report calls the move applied (`override_requests` had the same assumption, live fuzz seed
        903). `merge.unit_shift` has already asked that one step fits every member of the unit, so the
        same step is what each root wants."""
        reqs = []
        for u in units:
            if u["action"] == "move":
                dx, dy = (v * scale for v in u["delta"])
                for oid in merge.unit_roots(bunits.get(u["key"], []), read):
                    reqs.append(matrix_request(oid, [1, 0, 0, 1, dx, dy]))
        return reqs

    @staticmethod
    def _unit_oids(w: dict, ounits: dict, ukey: str, index: dict) -> list[str]:
        """The new objects of a recreated unit: its main object and the pictures anchored to it."""
        oids = []
        for m in ounits.get(ukey, []):
            oid = w["new_oid"].get(index[m["key"]]) if m["key"] in index else None
            if oid and oid not in oids:
                oids.append(oid)
        return oids

    def override_requests(self, work: dict, theirs: dict, now: dict, raw_before: dict | None = None,
                          raw_now: dict | None = None) -> list[dict]:
        """Deck edits re-applied to recreated elements: geometry, merged text, styles. raw_before /
        raw_now: objectId -> page element of the deck before sync and now (for run styles)."""
        raw_before, raw_now = raw_before or {}, raw_now or {}
        before = {s["objectId"]: s for s in theirs["slides"]}
        after = {s["objectId"]: s for s in now["slides"]}
        reqs = []
        for w in work["slides"]:
            p = w["plan"]
            if p["action"] != "update":
                continue
            b = self.base["slides"][p["base"]]
            bunits = merge.units(b["elements"])
            ounits = merge.units(self.ours["slides"][p["ours"]]["elements"])
            index = {e["key"]: k for k, e in enumerate(self.ours["slides"][p["ours"]]["elements"])}
            t_read, n_read = before[p["objectId"]], after[p["objectId"]]
            for u in p["units"]:
                ov = u.get("overrides") or {}
                if u["action"] != "recreate" or not ov:
                    continue
                i = index[u["key"]]
                main = w["new_oid"][i]
                top = w["tops"].get(u["key"], main)
                anchor = bunits[u["key"]][0]
                old_main = anchor["main"]
                final_text = None
                if "text" in ov and ov["text"].get("table") and main in n_read["objects"]:
                    new_rb = n_read["objects"][main]
                    cells = merge.table_merge(ov["text"]["base"], new_rb.get("text") or "", ov["text"]["theirs"],
                                              new_rb.get("table"), ov["text"]["dims"])
                    current = merge.table_grid(new_rb.get("text"), new_rb.get("table"))
                    if cells is None or current is None:
                        self.warnings.append(f"slide {p['key']}: {u['key']}: deck cell edits clash with the new table; not re-applied")
                    else:
                        for r, (crow, mrow) in enumerate(zip(current, cells[0])):
                            for c, (now_cell, want) in enumerate(zip(crow, mrow)):
                                if want != now_cell:  # the cell text ends in a newline Slides keeps
                                    reqs += merge.text_edit_requests(main, now_cell + "\n", want + "\n",
                                                                     {"rowIndex": r, "columnIndex": c})
                elif "text" in ov and main in n_read["objects"]:
                    current = n_read["objects"][main].get("text") or ""
                    merged, clashes, safe = merge.text_merge(ov["text"]["base"], current, ov["text"]["theirs"],
                                                             ov["text"].get("take") or ())
                    if not safe:
                        self.warnings.append(f"slide {p['key']}: {u['key']}: deck text edits clash with the new text; not re-applied")
                    else:
                        if not merged.endswith("\n"):
                            merged += "\n"
                        reqs += merge.text_edit_requests(main, current, merged)
                        final_text = merged
                if "text_style" in ov:
                    new_raw = raw_now.get(main, {})
                    cells = None
                    if "table" in new_raw:
                        cells = [{"rowIndex": r, "columnIndex": c} for r, row in enumerate(new_raw["table"].get("tableRows", []))
                                 for c, _ in enumerate(row.get("tableCells", []))]
                    reqs += style_override_requests(main, ov["text_style"], cells)
                    if ov["text_style"].get("ranges"):
                        base_styles = anchor.get("readback", {}).get(old_main, {}).get("text_styles", [])
                        if old_main in raw_before and new_raw:
                            reqs += style_range_requests(main, raw_before[old_main], new_raw, base_styles, final_text)
                        else:
                            self.warnings.append(f"slide {p['key']}: {u['key']}: the deck's word styles could not be re-applied")
                if "shape_style" in ov and ov["shape_style"]:
                    reqs += shape_style_requests(main, ov["shape_style"])
                if "geometry" in ov and top in n_read["objects"] and not (w.get("in_place") or {}).get(i, {}).get("table"):
                    # (a table refilled in place is still where, and as large as, the deck has it)
                    old_top = merge.unit_top(bunits[u["key"]], t_read) or old_main
                    base_rb = next((m["readback"].get(old_top) for m in bunits[u["key"]] if old_top in m.get("readback", {})), None)
                    theirs_rb = t_read["objects"].get(old_top)
                    new_rb = n_read["objects"][top]
                    if not base_rb or not theirs_rb:
                        continue
                    d = carried(base_rb, theirs_rb, new_rb)  # (merge: mode "delta", also when both moved it)
                    if any(abs(x - y) > 1e-4 for x, y in zip(d, [1, 0, 0, 1, 0, 0])):
                        # A group carries its children, so one request on it moves the whole unit.
                        # Without one - the person took this unit's group apart, and a recreation
                        # does not put it back - `top` is the main object alone, and the unit's
                        # anchored pictures need the step themselves or they stay at the
                        # converter's boxes while the report says the person's move was kept
                        # (live fuzz seed 903 at chain depth 8: a formula picture back 15 pt above
                        # the line it belongs to). `merge.geometry_writable` has already asked that
                        # one step fits every member, so the same step is what each of them wants.
                        for oid in ([top] if top != main else self._unit_oids(w, ounits, u["key"], index)):
                            reqs.append(matrix_request(oid, d))
        return reqs

    # ---- the new base

    def new_base(self, result: dict) -> dict:
        mplan, work, theirs = result["plan"], result["work"], result["theirs"]
        now = {s["objectId"]: s for s in self.created["slides"]}
        before = {s["objectId"]: s for s in theirs["slides"]}
        by_plan = {id(w["plan"]): w for w in work["slides"]}
        entries = {}
        for p in mplan["slides"]:
            w = by_plan[id(p)]
            if p["action"] in ("delete",):
                continue
            if p["action"] == "gone":
                # The deck deleted this slide while the source still has the frame. The entry holds
                # the frame's key and its place in the order (`base_order`); what it says follows the
                # source, or a frame whose label or title changed after the deletion stops looking
                # like this entry and comes back as a new slide.
                b, o = self.base["slides"][p["base"]], self.ours["slides"][p["ours"]]
                entries[f"gone:{p['key']}"] = {**b, **{k: o.get(k) for k in ("label", "title", "text", "page")},
                                               "removed": False}   # the source has this frame
                continue
            if p["action"] == "keep_removed":
                # The source dropped this frame and the deck's own edits keep the slide alive. Its
                # label goes with the frame: the source may put it on another frame tomorrow, and a
                # slide the source no longer describes must not hold a live label hostage. And the
                # entry says the source dropped it (`removed`), or it competes with the slide the
                # frame really lives on for the frame that looks most like it - see `align_slides`.
                entries[p["objectId"] or f"gone:{p['key']}"] = {**self.base["slides"][p["base"]],
                                                                "label": None, "removed": True}
                continue
            if p.get("held"):
                # Nothing was written here (`merge.hold_slide`), and the entry must not say
                # otherwise. An entry takes its label, title and words from the source, so a held
                # slide recorded the usual way would read next time as a change already made - and
                # the edit this sync held back would be gone for good instead of waiting for the
                # labels to be put right.
                b = self.base["slides"][p["base"]]
                entries[b["objectId"]] = dict(b)
                continue
            o = self.ours["slides"][p["ours"]]
            sid = w["sid"]
            read = now.get(sid)
            entry = {k: v for k, v in o.items() if k != "elements"}
            elements = []
            if p["action"] == "create":
                for i, e in enumerate(o["elements"]):
                    oids = w["objects"].get(i, [w["new_oid"].get(i)])
                    elements.append(self._element(e, oids, read))
                entry.update(objectId=sid, layoutObjectId=read["layoutObjectId"] if read else None,
                             background_readback=read["background"] if read else None,
                             notes_readback=read["notes"] if read else "", groups=w["groups"],
                             order=read["order"] if read else [])
            else:
                b = self.base["slides"][p["base"]]
                bunits, ounits = merge.units(b["elements"]), merge.units(o["elements"])
                index = {e["key"]: k for k, e in enumerate(o["elements"])}
                for u in p["units"]:
                    a = u["action"]
                    if a in ("create", "recreate"):
                        for mk in u["ours_members"]:
                            i = index[mk]
                            elements.append(self._element(o["elements"][i], w["objects"].get(i, [w["new_oid"][i]]), read))
                            if (w.get("in_place") or {}).get(i, {}).get("table"):  # (still the .pptx's table: `table_refill`)
                                elements[-1]["table_margins"] = [list(m) for m in w["in_place"][i]["margins"]]
                    elif a == "move":
                        for m in ounits[u["key"]]:
                            old = next((x for x in bunits[u["key"]] if x["key"] == m["key"]), None)
                            if old is None:
                                continue
                            rb = {oid: {**v, **{k: read["objects"][oid][k] for k in ("box", "transform")}}
                                  if read and oid in read["objects"] else v for oid, v in old["readback"].items()}
                            elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb,
                                             **{k: old[k] for k in ("table_margins",) if k in old}})
                    elif a == "adopt_object":
                        # the deck's own object is what the source now draws (a picture pull put in the source)
                        for mk in u["ours_members"]:
                            oid = u["objectId"]
                            elements.append(self._element(o["elements"][index[mk]], [oid], read))
                    elif a == "adopt":
                        # the source now says what the deck shows: ours IR, the deck's version of those fields
                        fields = {"text": ("text",), "geometry": ("box", "transform", "size"),
                                  "image": ("image", "box", "transform", "size")}
                        for m in ounits[u["key"]]:
                            old = next((x for x in bunits[u["key"]] if x["key"] == m["key"]), None)
                            if old is None:
                                continue
                            rb = copy.deepcopy(old["readback"])
                            live_obj = (read or {}).get("objects", {}).get(old.get("main"))
                            if live_obj and old.get("main") in rb:
                                for f in u.get("adopt", []):
                                    rb[old["main"]].update({k: live_obj[k] for k in fields.get(f, ()) if k in live_obj})
                            elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb,
                                             **{k: old[k] for k in ("table_margins",) if k in old}})
                    elif a in ("keep",):
                        # A unit kept because the source dropped it says so, or the next sync reads
                        # a deck that no longer differs from the base and deletes it (merge.plan_unit).
                        elements += [{**m, "removed": True} for m in bunits.get(u["key"], [])] \
                            if u.get("removed") else bunits.get(u["key"], [])
                    # delete / none: gone
                bg_conflict = b.get("background") != o.get("background") and not p.get("background")
                notes_kept = (b.get("notes") or "") != (o.get("notes") or "") and p.get("notes") is None
                doomed = w.get("doomed") or set()
                entry.update(objectId=sid, layoutObjectId=b.get("layoutObjectId"), groups=b.get("groups", []),
                             order=[x for x in read["order"] if x not in doomed] if read else b.get("order", []))
                if b.get("left_alone"):
                    # the person's own unpaired objects stay theirs at every generation
                    # (`adopt_sync.build_base`, `merge.slide_touched`)
                    entry["left_alone"] = list(b["left_alone"])
                if p.get("background"):
                    entry["background_readback"] = read["background"] if read else None
                else:
                    entry["background_readback"] = b.get("background_readback")
                    if bg_conflict:
                        entry["background"] = b.get("background")
                if p.get("notes") is not None:
                    entry["notes_readback"] = o.get("notes") or ""
                else:
                    entry["notes_readback"] = b.get("notes_readback", "")
                    if notes_kept:
                        entry["notes"] = b.get("notes")
            entry["elements"] = merge.keys_the_source_took(elements)
            entries[sid] = entry
        slides = [entries.pop(sid) for sid in base_order(mplan, by_plan, [s["objectId"] for s in self.created["slides"]])
                  if sid in entries] + list(entries.values())
        # what `refit` moved or grew is the converter's doing, not the person's (`refit.reshape_base`)
        from .refit import reshape_base
        slides = reshape_base(slides, getattr(self, "reshaped", None) or {})
        new = {**self.base, "generation": self.base.get("generation", 0) + 1, "revisionId": self.final_revision,
               "source": snapshot.source_info(self.ours["source"]), "slides": slides}
        pinned = set((self.theme_plan or {}).get("pinned") or ())
        if pinned:
            # The style theme_sync pinned onto placeholders (`inherited_pins`) looks the same and is
            # converter output: where the person had not restyled the object, the base takes it, or
            # the next sync reads the pins as the person's style edit.
            keys = ("text_styles", "paragraph_styles", "run_spans", "text_style_hash")
            pre = {oid: o for s in theirs["slides"] for oid, o in s.get("objects", {}).items() if oid in pinned}
            post = {oid: o for s in self.created["slides"] for oid, o in s.get("objects", {}).items() if oid in pinned}
            for entry in slides:
                elements = list(entry.get("elements", []))
                for i, el in enumerate(elements):   # (copies: a kept element is the old base's own dict)
                    rbs = el.get("readback") or {}
                    for oid in pinned & set(rbs):
                        if oid in pre and oid in post and all(rbs[oid].get(k) == pre[oid].get(k) for k in keys):
                            rbs = {**rbs, oid: {**rbs[oid], **{k: post[oid][k] for k in keys if k in post[oid]}}}
                            elements[i] = {**el, "readback": rbs}
                entry["elements"] = elements
        if self.base.get("theme") and self.theme_side is not None:
            from . import theme_sync
            new["master_background"] = self.master_key()
            new["theme"] = theme_sync.new_record(self.base["theme"], self.theme_side,
                                                 (self.theme_plan or {}).get("written") or {}, self.raw_after)
        return new

    def _element(self, e: dict, oids: list[str], read: dict | None) -> dict:
        objects = (read or {}).get("objects", {})
        return {**e, "objects": oids, "main": oids[0] if oids else None,
                "readback": {oid: objects[oid] for oid in oids if oid in objects}}


def base_order(mplan: dict, by_plan: dict, live: list[str]) -> list[str]:
    """Slide ids of the new base in the source's order (the base is converter output: a slide
    order the deck chose must keep differing from it, or the next sync would undo it). Slides kept
    though the source removed them stay after their live predecessor.

    A slide the person deleted while the source still has it (`gone`) has no live id, but it keeps
    its place here: the next conversion pairs its frames with this base in order
    (`identity.align_slides`), so an entry at the end takes the identity of every frame that
    followed it - they look new and get created again."""
    placed = (p for p in mplan["slides"] if p["action"] in ("update", "create", "gone"))
    order = [f"gone:{p['key']}" if p["action"] == "gone" else by_plan[id(p)]["sid"]
             for p in sorted(placed, key=lambda p: p["ours"])]
    for k, sid in enumerate(live):
        if sid in order:
            continue
        prev = next((live[q] for q in range(k - 1, -1, -1) if live[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
    return order


def stand_in_request(oid: str, sid: str, key: tuple) -> dict:
    size = {"width": emu(STAND_IN), "height": emu(STAND_IN)}
    transform = {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"}
    if key[0] == "BENT_CONNECTOR":
        return {"createLine": {"objectId": oid, "lineCategory": "BENT", "elementProperties": {
            "pageObjectId": sid, "size": size, "transform": transform}}}
    return {"createShape": {"objectId": oid, "shapeType": key[0], "elementProperties": {
        "pageObjectId": sid, "size": size, "transform": transform}}}


# ---------------------------------------------------------------- reports

def _quote(value) -> str:
    """One side of a conflict as a report shows it: a text on its own lines, anything else as the
    JSON these entries have always printed. Three versions of a paragraph one under the other is
    most of what a person opens a conflict report to see."""
    if not isinstance(value, str):
        return f"    {json.dumps(value, ensure_ascii=False)}"
    if not value.strip():
        return "    (nothing)"
    return "\n".join(f"    > {line}" if line.strip() else "    >"
                     for line in value.rstrip("\n").split("\n"))


def conflict_lines(c: dict) -> str:
    """A conflict, its three sides under it, and - where the source's version can be written here
    instead - the one thing a person types to ask for that (`merge.Resolutions`)."""
    where = f"`{c['slide']}`" + (f" / `{c['element']}`" if c.get("element") else "")
    out = [f"- {'`' + c['id'] + '` ' if c.get('id') else ''}{where}: **{c['field']}**, {c['resolution']}",
           "  - base:", _quote(c["base"]), "  - source:", _quote(c["ours"]), "  - deck:", _quote(c["theirs"])]
    if c.get("takeable") and c["resolution"] != merge.TAKEN_SAYS:
        out.append(f"  - to write the source's version here instead: sync again with "
                   f"`--take-source {c['id']}`. What it writes over is the deck's version above.")
    return "\n".join(out)


def write_reports(out: Path, info: dict) -> tuple[Path, Path]:
    folder = out / "sync"
    folder.mkdir(parents=True, exist_ok=True)
    stem = "sync-report" if not info.get("dry_run") else "sync-report-dry-run"
    jpath, mpath = folder / f"{stem}.json", folder / f"{stem}.md"
    r = info["report"]
    data = {k: v for k, v in info.items() if k != "report"}
    data.update({k: v for k, v in r.items() if k != "slides"})
    data.update({f"slides_{k}": v for k, v in r["slides"].items()})
    jpath.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    lines = [f"# Sync report{' (dry run)' if info.get('dry_run') else ''}", "",
             f"- PDF: `{info['pdf']}`", f"- Deck: {info['url']}", f"- Base: {info['base_from']} (generation {info['generation']})",
             f"- Requests sent: {info['requests']}", ""]

    def section(title, items, fmt):
        lines.append(f"## {title} ({len(items)})")
        lines.extend(fmt(x) for x in items) if items else lines.append("none")
        lines.append("")
    loc = lambda x: f"`{x['slide']}`" + (f" / `{x['element']}`" if x.get("element") else "")
    section("Source changes applied", r["applied"], lambda x: f"- {loc(x)}: {', '.join(x['fields'])}" + (f" ({x['how']})" if x.get("how") else ""))
    section("Deck edits kept (overrides)", r["overrides"], lambda x: f"- {loc(x)}: {', '.join(x['fields'])}")
    section("Conflicts", r["conflicts"], conflict_lines)
    section("Settled for the source (--take-source)", r.get("resolved") or [],
            lambda x: f"- `{x['id']}` {loc(x)}: **{x['field']}**. The deck said, and this sync wrote over:\n"
                      f"{_quote(x['was'])}")
    section("Converged", r["converged"], lambda x: f"- {loc(x)}: {x['field']}")
    section("Fitted to the words as merged", r.get("refit") or [],
            lambda x: f"- {loc(x)}: " + (f"the formula picture follows its hole, {x['shift'][0]:+.1f} / {x['shift'][1]:+.1f} pt"
                                         if x["what"] == "picture" else f"the {x['what']} grown {x['grown']:.1f} pt"))
    s = r["slides"]
    lines += ["## Slides", f"- created: {s['created'] or 'none'}", f"- deleted: {s['deleted'] or 'none'}",
              f"- moved: {s['moved'] or 'none'}", f"- kept (removed from the source, edited in the deck): {s['kept'] or 'none'}",
              f"- held (nothing written: which frame the slide is is in doubt): {s.get('held') or 'none'}",
              f"- added in the deck: {s['user_added'] or 'none'}", ""]
    section("Objects added in the deck", r["user_objects"], lambda x: f"- `{x['slide']}`: {x['objectId']}" + (f" (copy of {x['copy_of']})" if x.get("copy_of") else ""))
    section("Warnings", r["warnings"], lambda x: f"- {x}")
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


def sync(pdf: Path, deck: str, out: Path | None = None, dry_run: bool = False, overlays: str | None = None,
         measure: bool = True, way_back=None, backup_mode: str = "auto",
         force_adopted: bool = False, follow_labels: bool = False, take_source=()) -> dict:
    """`way_back`: the backup a caller kept before this sync, as a dict - or a `guard.WayBack`
    still making one on a thread of its own, which is collected before the first write."""
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
    facts = None
    with contextlib.suppress(HttpError, OSError):
        facts = snapshot.deck_info(drive, pid)  # name, parents, appProperties: read once, used four times
    base, where = snapshot.load_base(pid, folder or out, drive, problems, facts)
    if base is None:
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
    stale = snapshot.stale_base_warning(where, drive, pid, facts)
    warnings = problems + ([stale] if stale else [])
    overlays, mismatch = overlay_mode(overlays, base.get("overlays"))
    warnings += [mismatch] if mismatch else []
    for w in warnings:
        print(f"warning: {w}")
    # The deck's own width, not the frame convert writes into: a deck adopt took over is whatever
    # size the person made it, and everything this sync creates or moves is planned in slide pt.
    ours = build_ours(pdf, out / "sync" / "ours", base, overlays, adopt_sync.deck_width(base))
    refreshed = snapshot.refresh_pictures(base, ours, out)

    def check_plan(mplan: dict, theirs: dict) -> None:
        """An adopted deck's objects are a person's, not ours: refuse rather than write beside
        them (adopt_sync.problems). A dry run plans and reports; it writes nothing, so it never
        refuses - that is how a person sees what the sync wanted to do."""
        kept = way_back.backup() if hasattr(way_back, "backup") else way_back
        found = adopt_sync.problems(base, mplan, theirs, kept, backup_mode)
        if found:
            raise adopt_sync.FirstSyncRefused(adopt_sync.refusal_message(pid, out, pdf, found), found)

    check = None
    if base.get("origin") == adopt_sync.ORIGIN and not dry_run and not force_adopted:
        check = check_plan
    # A base that may be behind the deck never decides on its own that an object is a leftover.
    s = Sync(slides, drive, pid, base, ours, out, dry_run, measure, trust_generation=stale is None,
             check_plan=check, follow_labels=follow_labels, take_source=take_source, facts=facts,
             way_back=way_back if hasattr(way_back, "result") else None)
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
    report = result["plan"]["report"]
    # (a slide the deck deleted or the source dropped is nobody's business any more)
    updated = {p["key"] for p in result["plan"]["slides"] if p["action"] == "update"}
    report["warnings"] += s.warnings + warnings + unwritten_warnings(
        [u for u in ours.get("context_unwritten") or [] if u["slide"] in updated], ours)
    report["converged"] += [{**r, "field": "image", "how": "the same picture, written differently"} for r in refreshed]
    report["overruns"] = s.overruns
    report["refit"] = s.refit_moves
    info = {"pdf": str(pdf), "presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
            "dry_run": dry_run, "base_from": where, "generation": base.get("generation", 0), "overlays": overlays,
            "attempts": result["attempts"], "requests": s.sent, "seconds": 0.0,
            "report": report,
            "actions": [{"slide": p["key"], "action": p["action"],
                         "units": [{k: u[k] for k in ("key", "action", "source", "deck", "unpaired", "inherited",
                                                      "in_table") if k in u}
                                   for u in p.get("units", [])
                                   if u["action"] not in ("keep", "none") or u.get("deck") or u.get("unpaired")
                                   or u.get("inherited") or u.get("in_table")]}
                        for p in result["plan"]["slides"]]}
    adopted = any(u["action"] in ("adopt", "adopt_object") for p in result["plan"]["slides"] for u in p.get("units", []))
    recovered = bool(s.recovery.get("sweep") or s.recovery.get("sweep_slides") or s.recovery.get("heal")
                     or base.get("pending") or base.get("cleanup"))
    if not dry_run and (result["work"]["writes"] or adopted or refreshed or recovered):
        new = s.new_base(result) if (result["work"]["writes"] or adopted or refreshed) else dict(base)
        new["overlays"] = overlays  # (the steps the deck holds now)
        new.pop("pending", None)   # this run got to the end, so nothing is half done any more
        new.pop("cleanup", None)
        if s.cleanup_ids:
            # Stored before the old objects are deleted: whatever happens next, the base that the
            # next sync finds either still points at them or knows they have to go.
            new["cleanup"] = list(s.cleanup_ids)
        why = snapshot.store_base(new, out, drive, info=facts)
        if why:
            report["warnings"].append(
                f"could not store the new base in Drive ({why}); kept locally. The {len(s.cleanup_ids)} object(s) this "
                f"sync replaced are left in the deck: deleting them while the base another machine would read still "
                f"points at them could lose deck edits. The next sync removes them.")
        elif s.cleanup_ids:
            try:
                s.run_cleanup(s.final_revision)
            except (RuntimeError, HttpError) as e:  # they stay in the base's `cleanup` list
                report["warnings"].append(f"the objects this sync replaced could not be deleted ({e}); "
                                          f"the next sync removes them")
            else:
                # The objects are gone, so the list naming them must stop being read. That is all
                # this says, and a flag on the deck's own appProperties says it in a third of a
                # second where writing the base again costs about two (`snapshot.mark_cleaned`);
                # where the flag will not land, the base goes up again as it always did.
                new.pop("cleanup", None)
                snapshot.save_local(new, out)
                if snapshot.mark_cleaned(drive, pid, new["generation"], facts):
                    snapshot.store_base(new, out, drive, info=facts)
        info["generation"] = new["generation"]
    elif not dry_run and where == "drive":
        snapshot.save_local(base, out)  # (refresh the cache)
    s.await_deletes()   # the staging deck went on a thread; nobody leaves before it is gone
    # Measured where the run really ends: storing the base is Drive round trips of the sync's own,
    # and a number that stopped before them was a number about something else.
    info["seconds"] = round(time.monotonic() - started, 1)
    write_reports(out, info)
    return info
