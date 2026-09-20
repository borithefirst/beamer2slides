"""`sync`: bring a changed beamer PDF into the live, edited Slides deck (docs/sync.md).

    python -m beamer2slides sync deck.pdf --deck <url|id|out folder> [--out DIR] [--dry-run]

ours = the new conversion (planned like emit, nothing sent), theirs = the live deck, base = what
the converter wrote last time (snapshot). merge.plan_merge decides; this module builds the
requests (emit's builders under fresh object ids), writes them with requiredRevisionId and
records the new base."""

import copy
import json
import os
import random
import re
import string
import subprocess
import time
from pathlib import Path

from googleapiclient.errors import HttpError

from . import faults, identity, merge, snapshot
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
IN_PLACE_FIELDS = ("text", "text_styles", "paragraph_styles", "text_style_hash")


class RevisionMismatch(Exception):
    pass


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
    and every box this plan holds is PDF pt times the scale that width gives."""
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
    (work / "deck.json").write_text(json.dumps(deck, indent=1, ensure_ascii=False), encoding="utf-8")
    plan = DeckPlan({**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]},
                    page_width or SLIDE_W)
    deck = plan.deck
    infos = [identity.slide_info(s) for s in deck["slides"]]
    base_infos = [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "", "page": b["page"]}
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
    return {"source": pdf, "pdf": prepared.pdf, "out": work, "plan": plan, "deck": deck, "slides": entries,
            "pairs": pairs, "label_moves": moves, "weak_pairs": weak, "near_misses": near}


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


BREAK = {"__b2s_break__": True}  # where a batch may be cut: between slides


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


# ---------------------------------------------------------------- the sync

class Sync:
    def __init__(self, slides, drive, pid: str, base: dict, ours: dict, out: Path, dry_run: bool = False,
                 measure: bool = True, trust_generation: bool = True, check_plan=None,
                 follow_labels: bool = False, take_source=()):
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
        self.urls: dict[str, str] = {}  # picture file (str) -> contentUrl from the staging deck
        self.recovery: dict = {}        # what an interrupted earlier sync left (plan_recovery)
        self.cleanup_ids: list[str] = []      # old objects and slides, deleted after everything else
        self.cleanup_requests: list[dict] = []
        self.in_place_readback: dict[str, dict] = {}  # objects rewritten in place, before the write
        self.final_revision: str | None = None

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
        return execute(self.slides.presentations().get(presentationId=self.pid))

    def revision(self) -> str:
        return execute(self.slides.presentations().get(presentationId=self.pid, fields="revisionId"))["revisionId"]

    def send(self, phase: str, reqs: list[dict], rev: str | None) -> str:
        """Batches with requiredRevisionId, chained through the revisions they return. A batch is
        cut at a slide boundary where it can (`batches`), so a run that dies between two batches
        leaves whole slides written, never half of one."""
        for n, chunk in enumerate(batches(reqs)):
            try:
                body = {"requests": chunk}
                if rev:
                    body["writeControl"] = {"requiredRevisionId": rev}
                res = execute(self.slides.presentations().batchUpdate(presentationId=self.pid, body=body))
            except HttpError as e:
                from .emit import api_error
                message = api_error(e)
                if e.resp.status == 400 and "revision" in message.lower() and n == 0:
                    raise RevisionMismatch(message)
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
                raise SystemExit(
                    f"the sync base (generation {self.base.get('generation', 0)}) describes none of the slides in "
                    f"presentation {self.pid}: it belongs to another copy of this deck, or the deck was rebuilt "
                    f"outside sync. Syncing would report every element as deleted. Convert the PDF again "
                    f"(python -m beamer2slides convert) to start a new base, or point --deck at the right deck.")
            pres = self.recover(pres, attempt)
            theirs = snapshot.read_presentation(pres)
            if self.recovery.get("restore"):
                restore_in_place(theirs, self.recovery["restore"])
            self.sign_changed(theirs, pres)
            mplan = merge.plan_merge(self.base, self.ours, theirs, self.picture_adopter(pres),
                                     follow_labels=self.follow_labels, take_source=self.take_source)
            if self.check_plan is not None:
                self.check_plan(mplan, theirs)  # (adopt_sync: an adopted deck this may not be written to)
            work = self.prepare(mplan, pres, theirs)
            result = {"attempts": attempt, "plan": mplan, "work": work, "theirs": theirs}
            if self.dry_run or not work["writes"]:
                self.created, self.final_revision = theirs, theirs["revisionId"]  # (a base that only adopts deck fields)
                return result
            hook = os.environ.pop("B2S_SYNC_BEFORE_WRITE", None)  # (tests: someone edits the deck now)
            if hook:
                subprocess.run(hook, shell=True, check=False)
            self.staging = staging = self.stage(work)  # noted in the pending marker: a run that
            scratch = []                                # dies leaves it for the next one to delete
            try:
                if self.revision() != theirs["revisionId"]:
                    continue  # edited while we planned: plan again
                faults.fail_at("plan")
                moves, scratch = self.measure_places(work, theirs)
                faults.fail_at("measure")
                content, cleanup = self.main_requests(work, theirs, pres, moves, scratch)
                self.cleanup_requests = cleanup
                # What this sync is about to create, recorded before the first write: a run that
                # dies leaves its objects behind, and the next one knows they are its own.
                self.mark_pending(work, theirs)
                faults.fail_at("journal")
                rev = self.send("content", content, self.revision())
                scratch = []
            except RevisionMismatch:
                continue
            finally:
                if scratch:
                    self.delete_scratch(scratch)
                if staging:  # (its pictures are only needed until the live deck has them)
                    execute(self.drive.files().delete(fileId=staging))
                    self.staging = None
                    self.urls.clear()
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

    def mark_pending(self, work: dict, theirs: dict) -> None:
        """Store the base with a `pending` block before the first write: the generation and token of
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
        why = snapshot.store_base(self.base, self.out, self.drive, label="pending")
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

    def sign_changed(self, theirs: dict, pres: dict) -> None:
        """Pixel signatures of the live pictures whose contentUrl differs from the base's (Google
        issues new URLs for unchanged pictures, so only the pixels tell a replaced one)."""
        images = {oid: rb["image"] for s in self.base["slides"] for e in s["elements"]
                  for oid, rb in e.get("readback", {}).items() if "image" in rb}
        backgrounds = {s.get("objectId"): s.get("background_readback") or {} for s in self.base["slides"]}
        objects, slides = set(), set()
        for s in theirs["slides"]:
            for oid, rb in s["objects"].items():
                if "image" in rb and oid in images and images[oid].get("contentHash") != rb["image"].get("contentHash"):
                    objects.add(oid)
            bg, old = s.get("background") or {}, backgrounds.get(s["objectId"], {})
            if "picture" in bg and "picture" in old and old["picture"] != bg["picture"]:
                slides.add(s["objectId"])
        if objects or slides:
            snapshot.sign_pictures(theirs, pres, objects, slides)

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
        base_ids = {oid for s in self.base["slides"] for e in s["elements"] for oid in (e.get("objects") or [])}
        scale = self.scale or merge.deck_scale(self.base) or 1.0
        folder = self.ours["out"] / "deck-pictures"
        deck, mine, taken = {}, {}, set()

        def fingerprints(path: Path):
            return identity.sha1(path.read_bytes()), picture_look(path), picture_hash(path)

        def deck_picture(oid: str):
            """(sha1, look, thumbnail) of a live picture, downloaded once."""
            if oid not in deck:
                deck[oid] = None
                data = snapshot._download(images[oid]) if oid in images else None
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
        master = self.base.get("master_background")
        work = {"slides": [], "pictures": {}, "new_ids": new_ids, "page_slide": page_slide,
                "writes": merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])}
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

    def stage(self, work: dict) -> str | None:
        """Pictures go through a staging deck imported from a .pptx; its images' contentUrls are
        then used in the live deck. Returns the staging file's id: delete it once the live deck
        has the pictures (the URLs stop working with it)."""
        from googleapiclient.http import MediaIoBaseUpload
        from .emit import PPTX_MIME, build_pptx

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
        fid = execute(self.drive.files().create(body={"name": "beamer2slides sync staging (temporary)",
                                                      "mimeType": "application/vnd.google-apps.presentation",
                                                      "appProperties": {"b2sStaging": self.pid}},
                                                media_body=MediaIoBaseUpload(pptx, mimetype=PPTX_MIME), fields="id"))["id"]
        try:
            staged = execute(self.slides.presentations().get(presentationId=fid))
            slides = staged.get("slides", [])
            for s in slides[:len(pages) - len(backgrounds)]:
                for e in s.get("pageElements", []):
                    d = e.get("description") or ""
                    if "image" in e and d.startswith("b2s-stage:"):
                        self.urls[pictures[int(d[10:])]] = e["image"]["contentUrl"]
            for s, f in zip(slides[len(pages) - len(backgrounds):], backgrounds):
                fill = s.get("pageProperties", {}).get("pageBackgroundFill", {})
                if "stretchedPictureFill" in fill:
                    self.urls[f] = fill["stretchedPictureFill"]["contentUrl"]
            missing = [f for f in needed if f not in self.urls]
            if missing:
                raise RuntimeError(f"the staging deck brought no picture for {missing[:3]}")
        except Exception:
            execute(self.drive.files().delete(fileId=fid))
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
        live_ids = {s["objectId"] for s in theirs["slides"]}
        first = theirs["slides"][0]["objectId"]
        page_slide = {k: v if v in live_ids else first for k, v in self.plan.page_slide.items()}
        return measure_places(self.slides, self.pid, {**self.plan.deck, "slides": slides}, self.scale, self.plan.fonts,
                              self.plan.placed, page_slide, self.ours["out"], self.plan.page_width)

    def delete_scratch(self, scratch: list[str]) -> None:
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
                                for i, v in in_place.items()]}
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
                rs = [{"createImage": {"objectId": new_oid[i], "url": self.urls[str(path)], "elementProperties": {
                    "pageObjectId": sid, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU", "translateX": round(x0 * EMU_PER_PT),
                                  "translateY": round(y0 * EMU_PER_PT)}}}},
                      letterbox_fix(new_oid[i], box, png_size(path))] + rs
            if i in in_place and in_place[i].get("text"):
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
        if key != self.base.get("master_background"):
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
        # A new title element (the source's title changed beyond recognition) goes into the
        # placeholder of the title element it replaces.
        from .emit import title_element
        title_idx = title_element(slide)
        if title_idx is not None and title_idx not in in_place and \
                any(title_idx == index[mk] for u in recreated for mk in u["ours_members"]):
            taken = {v["id"] for v in in_place.values()}
            for u in p["units"]:
                if u["action"] not in ("delete", "recreate"):
                    continue
                main = next((m.get("main") for m in bunits.get(u["key"], [])[:1]), None)
                if main and main in objects and main not in taken and \
                        objects[main].get("placeholder") in ("TITLE", "CENTERED_TITLE"):
                    in_place[title_idx] = {"id": main, "size": objects[main]["size"],
                                           "text": (objects[main].get("text") or "").strip()}
                    break

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
        reqs += self.regroup_requests(regroup, depth, objects, tops, keep_ids)
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
    def regroup_requests(regroup: dict, depth: dict, objects: dict, tops: dict, keep_ids: set) -> list[dict]:
        """The groups `regroups` took apart, made again under the same ids, innermost first: a
        rewritten unit's new top object takes its old root's place among the children (`tops`: unit
        key -> new top; `objects`: the read-back before the rewrite)."""
        reqs = []
        replaced: dict[str, str | None] = {}  # regrouped group -> what stands for it now (None: gone)
        for g in sorted(regroup, key=lambda g: -depth[g]):
            info = regroup[g]
            children = []
            for c in objects[g].get("children", []):
                if c in info["remove"]:
                    ukey = info["unit_of"][c]
                    if ukey in tops and tops[ukey] not in children and tops[ukey] not in keep_ids:
                        children.append(tops[ukey])
                elif c in replaced:
                    if replaced[c]:
                        children.append(replaced[c])
                else:
                    children.append(c)
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
        master = self.base.get("master_background")
        if key == master:
            if created:
                return []  # emit leaves these slides inheriting the master, and a new slide already does
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
        url = self.urls[str(self.ours["out"] / slide["background"])]
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
        # read-back of objects as the converter created them, with the new pictures' signatures
        created = {x for w in work["slides"] for oids in (w.get("objects") or {}).values() for x in oids}
        repainted = {w.get("sid") for w in work["slides"] if w["plan"]["action"] == "create" or w["plan"].get("background")}
        snapshot.sign_pictures(now, raw, created, repainted)
        self.created = now
        overrides = self.override_requests(work, theirs, now, raw_objects(pres), raw_objects(raw))
        if overrides:
            rev = self.send("overrides", overrides, rev)
        self.final_revision = rev
        return rev

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
        replace, added = {}, []
        for u in p["units"]:
            if u["action"] == "recreate":
                old = merge.unit_top(bunits[u["key"]], before)
                new = w["tops"].get(u["key"])
                if old and new:
                    replace[old] = new
            elif u["action"] == "create" and u["key"] in w["tops"]:
                added.append(u["key"])
        desired = []
        for oid in before["order"]:
            oid = replace.get(oid, oid)
            if oid in top_now and oid not in desired:
                desired.append(oid)
        keys = [e["key"] for e in o["elements"]]

        def placed(k):
            return w["tops"].get(k) or (merge.unit_top(bunits[k], now) if k in bunits else None)

        for ukey in added:
            new = w["tops"][ukey]
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
                if "geometry" in ov and top in n_read["objects"]:
                    old_top = merge.unit_top(bunits[u["key"]], t_read) or old_main
                    base_rb = next((m["readback"].get(old_top) for m in bunits[u["key"]] if old_top in m.get("readback", {})), None)
                    theirs_rb = t_read["objects"].get(old_top)
                    new_rb = n_read["objects"][top]
                    if not base_rb or not theirs_rb:
                        continue
                    if ov["geometry"]["mode"] == "delta":
                        d = snapshot.compose(theirs_rb["transform"], snapshot.invert(base_rb["transform"]))
                    else:
                        d = [1, 0, 0, 1, theirs_rb["box"][0] - new_rb["box"][0], theirs_rb["box"][1] - new_rb["box"][1]]
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
                entries[f"gone:{p['key']}"] = {**b, **{k: o.get(k) for k in ("label", "title", "text", "page")}}
                continue
            if p["action"] == "keep_removed":
                # The source dropped this frame and the deck's own edits keep the slide alive. Its
                # label goes with the frame: the source may put it on another frame tomorrow, and a
                # slide the source no longer describes must not hold a live label hostage.
                entries[p["objectId"] or f"gone:{p['key']}"] = {**self.base["slides"][p["base"]], "label": None}
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
                    elif a == "move":
                        for m in ounits[u["key"]]:
                            old = next((x for x in bunits[u["key"]] if x["key"] == m["key"]), None)
                            if old is None:
                                continue
                            rb = {oid: {**v, **{k: read["objects"][oid][k] for k in ("box", "transform")}}
                                  if read and oid in read["objects"] else v for oid, v in old["readback"].items()}
                            elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb})
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
                            elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb})
                    elif a in ("keep",):
                        elements += bunits.get(u["key"], [])
                    # delete / none: gone
                bg_conflict = b.get("background") != o.get("background") and not p.get("background")
                notes_kept = (b.get("notes") or "") != (o.get("notes") or "") and p.get("notes") is None
                doomed = w.get("doomed") or set()
                entry.update(objectId=sid, layoutObjectId=b.get("layoutObjectId"), groups=b.get("groups", []),
                             order=[x for x in read["order"] if x not in doomed] if read else b.get("order", []))
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
            entry["elements"] = elements
            entries[sid] = entry
        slides = [entries.pop(sid) for sid in base_order(mplan, by_plan, [s["objectId"] for s in self.created["slides"]])
                  if sid in entries] + list(entries.values())
        return {**self.base, "generation": self.base.get("generation", 0) + 1, "revisionId": self.final_revision,
                "source": snapshot.source_info(self.ours["source"]), "slides": slides}

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
         measure: bool = True, way_back: dict | None = None, backup_mode: str = "auto",
         force_adopted: bool = False, follow_labels: bool = False, take_source=()) -> dict:
    from . import adopt_sync
    from .google_auth import drive_service, slides_service

    started = time.monotonic()
    pid, folder = resolve_deck(deck)
    out = out or folder or out_root() / pdf.stem
    slides, drive = slides_service(), drive_service()
    problems: list[str] = []
    base, where = snapshot.load_base(pid, folder or out, drive, problems)
    if base is None:
        raise SystemExit("\n".join([f"no sync base for presentation {pid}: convert the deck with this version first",
                                    "  A deck this converter never made has one only where `adopt` wrote it: sync it "
                                    "with `--deck <the adopt work folder>`,",
                                    "  not with the deck's URL. `convert` would make a second deck and leave this one "
                                    "with its comments and history behind.",
                                    *problems]))
    stale = snapshot.stale_base_warning(where, drive, pid)
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
        found = adopt_sync.problems(base, mplan, theirs, way_back, backup_mode)
        if found:
            raise adopt_sync.FirstSyncRefused(adopt_sync.refusal_message(pid, out, pdf, found), found)

    check = None
    if base.get("origin") == adopt_sync.ORIGIN and not dry_run and not force_adopted:
        check = check_plan
    # A base that may be behind the deck never decides on its own that an object is a leftover.
    s = Sync(slides, drive, pid, base, ours, out, dry_run, measure, trust_generation=stale is None,
             check_plan=check, follow_labels=follow_labels, take_source=take_source)
    result = s.run()
    report = result["plan"]["report"]
    report["warnings"] += s.warnings + warnings
    report["converged"] += [{**r, "field": "image", "how": "the same picture, written differently"} for r in refreshed]
    info = {"pdf": str(pdf), "presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
            "dry_run": dry_run, "base_from": where, "generation": base.get("generation", 0), "overlays": overlays,
            "attempts": result["attempts"], "requests": s.sent, "seconds": round(time.monotonic() - started, 1),
            "report": report,
            "actions": [{"slide": p["key"], "action": p["action"],
                         "units": [{k: u[k] for k in ("key", "action", "source", "deck") if k in u} for u in p.get("units", [])
                                   if u["action"] not in ("keep", "none") or u.get("deck")]}
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
        why = snapshot.store_base(new, out, drive)
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
                new.pop("cleanup", None)
                snapshot.store_base(new, out, drive)
        info["generation"] = new["generation"]
    elif not dry_run and where == "drive":
        snapshot.save_local(base, out)  # (refresh the cache)
    write_reports(out, info)
    return info
