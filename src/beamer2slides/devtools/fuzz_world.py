"""A synthetic deck world for the offline sync fuzz (tools/fuzz_sync.py).

Builds the three sides `merge.plan_merge_of` needs - a base (converter output plus Google's
read-back), `ours` (a new conversion of a changed source) and `theirs` (the live deck after a
person's edits) - from a small made-up document, and applies a merge plan the way a correct sync
would write it (`apply_plan`). That applier is the *reference semantics* of docs/sync.md, not a copy
of sync.py: the offline fuzz compares what merge planned against what a faithful writer would
produce, and tools/loss_oracle.py then judges the result.

Documents are JSON, as classify writes them:
    {"slides": [{"page", "label", "title", "notes", "bg", "elements": [IR, ...]}]}
Element IR is the converter's (classify) shape, cut down to what identity and merge look at. The base
and the new conversion are JSON too, being what sync reads and writes. The live deck is typed: a
`LiveDeck` of `LiveSlide`s holding `sync_model.ReadBack` records, the shape `snapshot` reads Google's
answer into. It is the one side the fuzz edits in place, so its slides are mutable and a changed
object is a new record put in its place (`LiveSlide.objects`)."""

import copy
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from beamer2slides import identity, merge, snapshot, sync, sync_model
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, \
    as_optional_str, as_str
from beamer2slides.sync_model import Base, DeckRead, ElementEntry, ElementKey, ImageRead, ObjectId, ReadBack, SlideEntry, \
    SlideRead, deck_read_json, image_read, readback_json, readbacks_json
from beamer2slides.typing_compat import assert_never

SCALE = 2.0  # deck pt per PDF pt (like a 16:9 deck of a 360 pt wide PDF)
WORDS = ("slides", "source", "merge", "author", "deck", "editor", "figure", "policy", "review", "export",
         "wording", "layout", "picture", "notes", "colleague", "revision", "timing", "table", "bullet", "frame")

Shape = Literal["converted", "adopt"]
"""What kind of deck a round draws: a talk `convert` made, or a deck `adopt` took over."""


# ---------------------------------------------------------------- reading JSON

def obj(v: Json, where: str) -> JsonObject:
    return as_object(v, where)


def objs(v: Json, where: str) -> list[JsonObject]:
    """The objects of an array (a new list of the same dicts)."""
    return as_objects(v, where)


def arr(v: Json, where: str) -> list[Json]:
    """The array itself: changing it changes the JSON."""
    return as_array(v, where)


def num(v: Json, where: str) -> float:
    """A number as it came: an int stays an int (a hash of the JSON tells 3 from 3.0)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def nums(v: Json, where: str) -> list[float]:
    return [num(x, where) for x in as_array(v, where)]


def text(v: Json, where: str) -> str:
    return as_str(v, where)


def opt_text(v: Json, where: str) -> str | None:
    return as_optional_str(v, where)


def jlist(xs: Sequence[JsonObject]) -> list[Json]:
    """JSON objects as a JSON array (a new list of the same dicts)."""
    return [x for x in xs]


def jstrs(xs: Sequence[str]) -> list[Json]:
    return [x for x in xs]


def jnums(xs: Sequence[float]) -> list[Json]:
    return [x for x in xs]


def _maps(xs: Sequence[Mapping[str, Json]]) -> list[Json]:
    """Mappings a function handed back as the dicts they are (a copy only of one that is not)."""
    return [x if isinstance(x, dict) else dict(x) for x in xs]


# ---------------------------------------------------------------- IR

def run(words: str) -> JsonObject:
    return {"text": words, "font": "CMSS10", "family": "sans", "size": 10.0, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
            "highlight": None}


def text_ir(eid: str, words: str, bbox: Sequence[float], role: str) -> JsonObject:
    lines = words.split("\n")
    return {"id": eid, "kind": "text", "role": role, "bbox": [float(v) for v in bbox], "panel": None, "code": False,
            "spans": [], "strokes": [], "paragraphs": [
                {"align": "left", "level": 0, "bullet": None, "size": 10.0, "text_x0": bbox[0], "tab_x0": None,
                 "lines": [{"baseline": bbox[1] + 8 + 12 * k, "x0": bbox[0], "x1": bbox[2]}], "wrap_limit": None,
                 "runs": [run(line)]} for k, line in enumerate(lines)]}


def image_ir(eid: str, bbox: Sequence[float], file: str, anchor: str | None, role: str) -> JsonObject:
    el: JsonObject = {"id": eid, "kind": "image", "role": role, "bbox": [float(v) for v in bbox], "file": file}
    if anchor:
        el["anchor"] = anchor
    return el


def shape_ir(eid: str, bbox: Sequence[float], fill: str, block: int | None) -> JsonObject:
    el: JsonObject = {"id": eid, "kind": "shape", "role": "panel", "bbox": [float(v) for v in bbox],
                      "shape": "RECTANGLE", "fill": fill, "flip": False, "radius": 0.0}
    if block is not None:
        el["block"] = block
    return el


def table_ir(eid: str, bbox: Sequence[float], rows: Sequence[Sequence[str]]) -> JsonObject:
    cells: list[Json] = [[[run(c)] for c in row] for row in rows]
    return {"id": eid, "kind": "table", "role": "table", "bbox": [float(v) for v in bbox], "borders": [],
            "cells": cells}


def table_text(el: Mapping[str, Json]) -> str:
    return "\n".join("\t".join(identity.run_text(as_array(c, "cell")).strip() for c in as_array(row, "row"))
                     for row in as_array(el["cells"], "table.cells"))


def element_text(el: Mapping[str, Json]) -> str | None:
    if el["kind"] == "text":
        return merge.predicted_text(el)
    if el["kind"] == "table":
        return table_text(el)
    return None


def elements(s: Mapping[str, Json]) -> list[JsonObject]:
    """A slide's elements (of a document, a base or the new conversion)."""
    return objs(s["elements"], "slide.elements")


def slides(doc: Mapping[str, Json]) -> list[JsonObject]:
    return objs(doc["slides"], "slides")


# ---------------------------------------------------------------- documents

def make_doc(rng: random.Random, out: Path) -> JsonObject:
    """A small talk: a title slide and 2-5 content slides with text, a figure, a block, a table and
    an anchored formula picture."""
    n = rng.randint(3, 6)
    made: list[JsonObject] = []
    for i in range(n):
        title = f"{rng.choice(WORDS).title()} {rng.choice(WORDS)} {i}"
        els = [text_ir(f"p{i}t0", title, (20, 20, 20 + 6 * len(title), 34), "title")]
        y = 60
        for k in range(rng.randint(1, 3)):
            lines = [" ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 7))) for _ in range(rng.randint(1, 3))]
            els.append(text_ir(f"p{i}t{k + 1}", "\n".join(lines), (25, y, 200, y + 12 * len(lines)), "body"))
            y += 12 * len(lines) + 8
        if rng.random() < 0.5:
            path = out / f"fig{i}.bin"
            path.write_bytes(bytes([(i * 37 + j) % 251 for j in range(64)]))
            els.append(image_ir(f"p{i}i0", (220, 60, 320, 140), f"fig{i}.bin", None, "figure"))
        if rng.random() < 0.35:
            els.append(shape_ir(f"p{i}s0", (25, y, 200, y + 30), "#dddddd", None))
            y += 38
        brng = random.Random(f"{title}/block")  # (its own draws: every older seed keeps its deck)
        if brng.random() < 0.35:
            # a beamer block: an opaque panel and the body text on it, which emit groups
            # (`block_groups`) - the panel first, so under the text
            body = " ".join(brng.choice(WORDS) for _ in range(brng.randint(3, 6)))
            els.append(shape_ir(f"p{i}k0", (25, y, 200, y + 28), "#dde4f0", 0))
            els.append({**text_ir(f"p{i}k1", body, (30, y + 8, 195, y + 20), "body"), "block": 0})
            y += 36
        if rng.random() < 0.35:
            rows = [[rng.choice(WORDS) for _ in range(2)] for _ in range(2)]
            els.append(table_ir(f"p{i}b0", (25, y, 180, y + 30), rows))
            y += 38
        if rng.random() < 0.35:
            anchor = text(els[1]["id"], "element.id")
            path = out / f"math{i}.bin"
            path.write_bytes(bytes([(i * 91 + j) % 251 for j in range(48)]))
            els.append(image_ir(f"p{i}m0", (120, 62, 140, 74), f"math{i}.bin", anchor, "math"))
        made.append({"page": i, "label": f"f{i}" if rng.random() < 0.6 else None, "title": title,
                     "notes": " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 6))), "bg": "#ffffff",
                     "elements": jlist(els)})
    return {"slides": jlist(made)}


# What a template deck says over and over: chrome, section headings, calls to action. Short, and
# most of it on more than one slide - which is what makes an adopt-shaped deck hard to key.
ADOPT_PHRASES = ("Agenda", "Thank you", "Questions?", "Get started", "What we shipped", "Roadmap",
                 "Next steps", "Demo", "Resources", "Build with us", "Why it matters", "In short")


def _box(eid: str, x: float, y: float, w: float, words: str, role: str) -> JsonObject:
    """One of the small boxes an adopted slide is made of (a `textblock*` per element)."""
    return text_ir(eid, words, (x, y, x + w, y + 12), role)


def make_adopt_doc(rng: random.Random, out: Path) -> JsonObject:
    r"""A deck `adopt` took over, as the converter reads its source back (docs/sync.md, "Adopt").

    `make_doc` draws a talk: a frame title over a few big paragraphs, a figure, a block. A foreign
    deck is the opposite of that in every way identity depends on, and the source `adopt` writes
    keeps it that way on purpose - a slide is a page of small boxes somebody dragged
    (`textblock*`/`slidebox` per element), often with no title at all, with the deck's chrome
    repeated word for word on every slide, one logo file shared by all of them, tikz clusters, a
    `slidetable`, and whole slides that differ from their neighbour by a line. Every rule downstream
    - `slide_similarity`, `_evidence`, `match_elements`, the fingerprints - has less to go on here
    than anywhere the campaign had been looking.

    Every frame carries a label (`adopt.frame_labels`), because that is what the adopted source now
    writes; the source ops that move, rename and drop one are how the campaign asks what happens
    when that promise is broken.
    """
    n = rng.randint(4, 7)
    chrome = f"{rng.choice(WORDS).title()} Conf {rng.randrange(2020, 2030)}"
    # Pictures adopt copies into `figures/<slug>-<sha8>.<ext>`: one logo on every slide, so several
    # elements point at one file and their fingerprints' `image_sha1` cannot tell them apart.
    pool: list[str] = []
    for k in range(2):
        path = out / f"fig-adopt{k}.bin"
        path.write_bytes(bytes([(k * 71 + j) % 251 for j in range(64)]))
        pool.append(path.name)
    made: list[JsonObject] = []
    for i in range(n):
        els: list[JsonObject] = []
        if rng.random() < 0.45:                       # the layout's title placeholder, when it has one
            els.append(_box(f"p{i}e0", 40, 24, 300, rng.choice(ADOPT_PHRASES), "title"))
        # the deck's chrome: the same words at the same place on every slide
        els.append(_box(f"p{i}e1", 20, 190, 120, chrome, "footer"))
        els.append(image_ir(f"p{i}e2", (300, 186, 330, 198), pool[0], None, "figure"))
        y = 52
        for k in range(rng.randint(4, 9)):
            words = rng.choice(ADOPT_PHRASES) if rng.random() < 0.4 else \
                " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4)))
            x = 30 + 150 * (k % 2)
            els.append(_box(f"p{i}e{k + 3}", x, y, 130, words, "body"))
            y += 16 if k % 2 else 0
        if rng.random() < 0.4:
            # An icon at the head of a line: a picture the converter made while reading one of the
            # person's own *text boxes* back, so it has no object of its own and never will - the
            # box it came out of is the one beside it (`adopt_sync.drawn_from`). Anchored, so it is
            # a member of that box's unit, which is what lets the unit be written at all
            # (`merge.covered`); without it the person's box could never be edited from the source.
            host = els[-1]
            hbox = nums(host["bbox"], "element.bbox")
            hx, hy = hbox[0], hbox[1]
            els.append(image_ir(f"p{i}e94", (hx + 2, hy + 2, hx + 10, hy + 10), pool[0],
                                text(host["id"], "element.id"), "icon"))
        if rng.random() < 0.4:                        # a tikz cluster the deck groups
            els.append(shape_ir(f"p{i}g0", (200, 40, 300, 70), "#e8eaed", 0))
            els.append({**_box(f"p{i}g1", 206, 48, 88, rng.choice(ADOPT_PHRASES), "body"), "block": 0})
            els.append(shape_ir(f"p{i}g2", (200, 78, 300, 100), "#fce8e6", 0))
        if rng.random() < 0.3:
            els.append(image_ir(f"p{i}e90", (210, 110, 300, 170), pool[1], None, "figure"))
        if rng.random() < 0.3:
            rows = [[rng.choice(WORDS) for _ in range(3)] for _ in range(2)]
            els.append(table_ir(f"p{i}b0", (30, 120, 180, 160), rows))
        made.append({"page": i, "label": f"g{i}-0-{rng.randrange(10, 99)}",
                     "title": title_of({"elements": jlist(els)}), "notes": "", "bg": "#ffffff",
                     "elements": jlist(els)})
        # A template deck's neighbours differ by a line. `align_slides` pairs on the words, so two
        # slides this alike are what makes a reorder, an insertion or a moved label ambiguous.
        if i and rng.random() < 0.3:
            twin = copy.deepcopy(made[-2])
            body = [e for e in elements(twin) if e["kind"] == "text" and e.get("role") == "body"]
            if body:
                obj(arr(body[-1]["paragraphs"], "paragraphs")[0], "paragraph")["runs"] = [run("and one line of its own")]
            renamed = [{**e, "id": text(e["id"], "element.id").replace(f"p{i - 1}", f"p{i}", 1)} for e in elements(twin)]
            for e in renamed:
                anchor = opt_text(e.get("anchor"), "element.anchor")
                if anchor:
                    e["anchor"] = anchor.replace(f"p{i - 1}", f"p{i}", 1)
            twin["elements"] = jlist(renamed)
            made[-1] = {**twin, "page": i, "label": made[-1]["label"]}
    return {"slides": jlist(made)}


SHAPES: dict[Shape, Callable[[random.Random, Path], JsonObject]] = {"converted": make_doc, "adopt": make_adopt_doc}


def shape_of(name: str) -> Shape:
    """A shape named on a command line."""
    for s in SHAPES:
        if s == name:
            return s
    raise ValueError(f"no deck shape {name!r}: {', '.join(SHAPES)}")


def make(shape: Shape, rng: random.Random, out: Path) -> JsonObject:
    return SHAPES[shape](rng, out)


def title_of(s: Mapping[str, Json]) -> str:
    """The slide's title as `identity` finds it: the text element whose role says so, and nothing
    else. A converted talk has one on top of every slide, so "the first element" used to be the
    same answer; an adopt-shaped deck is a page of boxes with no title at all, and reading the
    topmost of them as one would hand `align_slides` a title nothing in the deck ever showed."""
    return identity.slide_title(s)


def title_element(s: Mapping[str, Json]) -> JsonObject | None:
    return next((e for e in elements(s) if e["kind"] == "text" and e.get("role") == "title"), None)


def slide_info(s: Mapping[str, Json]) -> JsonObject:
    return {"label": s.get("label"), "title": title_of(s),
            "text": " ".join(identity.plain_text(e) for e in elements(s)), "page": s["page"]}


# ---------------------------------------------------------------- the live deck

@dataclass(kw_only=True)
class LiveSlide:
    """One slide of the live deck (`sync_model.SlideRead`'s fields). Mutable: a person's edits and
    the reference sync change it in place - the page `order`, the `objects` by id - and a changed
    object is a new `ReadBack` in its place, never one changed under whoever else holds it."""
    object_id: ObjectId
    layout_object_id: str | None
    background: JsonObject | None
    notes: str
    notes_id: str | None
    order: list[ObjectId]
    objects: dict[ObjectId, ReadBack]


@dataclass(kw_only=True)
class LiveDeck:
    """The live deck (`sync_model.DeckRead`'s fields), its slides in deck order."""
    presentation_id: str | None
    revision_id: str | None
    page_size: tuple[float, ...] | None
    layouts: JsonObject | None
    master_background: JsonObject | None
    slides: list[LiveSlide]


def slide_read_of(s: LiveSlide) -> SlideRead:
    """The slide as sync reads it (a snapshot: later edits of `s` do not reach it)."""
    return SlideRead(object_id=s.object_id, layout_object_id=s.layout_object_id, background=s.background,
                     notes=s.notes, notes_id=s.notes_id, order=tuple(s.order), objects=dict(s.objects))


def deck_read_of(d: LiveDeck) -> DeckRead:
    return DeckRead(presentation_id=d.presentation_id, revision_id=d.revision_id, page_size=d.page_size,
                    layouts=d.layouts, master_background=d.master_background,
                    slides=tuple(slide_read_of(s) for s in d.slides))


def live_json(d: LiveDeck) -> JsonObject:
    """The deck as `snapshot.read_presentation` writes it (what a dict reader and the archives take)."""
    return deck_read_json(deck_read_of(d))


def copy_slide(s: LiveSlide) -> LiveSlide:
    """A slide nothing done to `s` reaches (records are immutable, so their containers are copied)."""
    return LiveSlide(object_id=s.object_id, layout_object_id=s.layout_object_id,
                     background=copy.deepcopy(s.background), notes=s.notes, notes_id=s.notes_id,
                     order=list(s.order), objects=dict(s.objects))


def copy_deck(d: LiveDeck) -> LiveDeck:
    return LiveDeck(presentation_id=d.presentation_id, revision_id=d.revision_id, page_size=d.page_size,
                    layouts=copy.deepcopy(d.layouts), master_background=copy.deepcopy(d.master_background),
                    slides=[copy_slide(s) for s in d.slides])


# ---------------------------------------------------------------- base, ours, theirs

def picture_signature(data: bytes) -> str:
    return "100x50:" + bytes([(data[0] * 97 + i * 13) % 256 for i in range(1024)]).hex()


def picture_json(data: bytes) -> JsonObject:
    """A picture's read-back as a base entry records it (`picture`)."""
    return {"contentHash": identity.sha1(data)[:16], "sourceUrl": None, "signature": picture_signature(data)}


def panel_fill(el: Mapping[str, Json]) -> JsonObject | None:
    """A shape element's fill as `snapshot.shape_style` reads it (text boxes have none: emit's
    text boxes are transparent, so only panels can hide anything)."""
    return {"color": el["fill"], "alpha": 1.0} if el.get("kind") == "shape" and el.get("fill") else None


def readback(kind: str, box: Sequence[float], *, text: str | None, image: ImageRead | None, parent: ObjectId | None,
             title: str | None, z: int, table: tuple[int, int] | None, fill: JsonObject | None) -> ReadBack:
    """An object as Google reads it back, at `box` (deck pt): the text boxes' default style, and a
    shape's fill (`fill`: None for one that has none)."""
    rounded = (round(box[0], 2), round(box[1], 2), round(box[2], 2), round(box[3], 2))
    return ReadBack(kind=kind, transform=(1.0, 0.0, 0.0, 1.0, round(box[0], 2), round(box[1], 2)),
                    size=(round(box[2] - box[0], 2), round(box[3] - box[1], 2)), box=rounded, parent_group=parent,
                    z=z, title=title, description=None, placeholder=None, table=table, text=text,
                    text_styles=({"fontFamily": "Lato", "fontSize": 18.0},) if text is not None else (),
                    paragraph_styles=({"alignment": "START"},) if text is not None else (), run_spans=(),
                    text_style_hash="s0", shape_style={"fill": fill}, shape_style_hash="h0", image=image,
                    children=None, refit=None)


def group_readback(box: Sequence[float], z: int, children: Sequence[ObjectId]) -> ReadBack:
    return replace(readback("elementGroup", box, text=None, image=None, parent=None, title=None, z=z, table=None,
                            fill=None), children=tuple(children))


def style_hashes(el: Mapping[str, Json]) -> tuple[str, str]:
    """What Google's read-back shows of an element's styling (`snapshot` hashes it): the runs'
    attributes and the shape's fill, so a restyle in the source really changes the object."""
    runs = [[r.get("color"), r.get("bold"), r.get("italic"), r.get("size"), r.get("font"), r.get("underline")]
            for p in objs(el.get("paragraphs") or [], "element.paragraphs")
            for r in objs(p.get("runs") or [], "paragraph.runs")]
    words = identity.sha1(json.dumps(runs))[:8] if runs else "s0"
    shape = identity.sha1(json.dumps([el.get("fill"), el.get("shape")]))[:8] if el["kind"] == "shape" else "h0"
    return words, shape


def styled(rb: ReadBack, el: Mapping[str, Json]) -> ReadBack:
    words, shape = style_hashes(el)
    return replace(rb, text_style_hash=words, shape_style_hash=shape)


def object_readback(el: Mapping[str, Json], out: Path, parent: ObjectId | None, z: int) -> ReadBack:
    box = [v * SCALE for v in nums(el["bbox"], "element.bbox")]
    if el["kind"] == "image":
        data = (out / text(el["file"], "element.file")).read_bytes()
        image = ImageRead(content_hash=identity.sha1(data)[:16], source_url=None, signature=picture_signature(data),
                          unchecked=False)
        return styled(readback("image", box, text=None, image=image, parent=parent, title=None, z=z, table=None,
                               fill=None), el)
    if el["kind"] == "table":
        rows = arr(el["cells"], "table.cells")
        return styled(readback("table", box, text=table_text(el), image=None, parent=parent, title=None, z=z,
                               table=(len(rows), len(arr(rows[0], "table.row"))), fill=None), el)
    if el["kind"] == "shape":
        return styled(readback("shape", box, text="", image=None, parent=parent, title=None, z=z, table=None,
                               fill=panel_fill(el)), el)
    return styled(readback("shape", box, text=merge.predicted_text(el), image=None, parent=parent, title=None, z=z,
                           table=None, fill=None), el)


def entries(doc: Mapping[str, Json], out: Path, keys: Sequence[str], all_ekeys: Sequence[Sequence[str]],
            all_fps: Sequence[Sequence[JsonObject]]) -> list[JsonObject]:
    made: list[JsonObject] = []
    for s, key, ekeys, fps in zip(slides(doc), keys, all_ekeys, all_fps):
        els = elements(s)
        ids = {text(e["id"], "element.id"): k for e, k in zip(els, ekeys)}
        entry_els: list[JsonObject] = []
        for el, ek, fp in zip(els, ekeys, fps):
            a = opt_text(el.get("anchor"), "element.anchor")
            anchor = ids.get(a) if a is not None else None
            h, fields = identity.ir_fields(el, out, anchor)
            entry: JsonObject = {"key": ek, "id": el["id"], "kind": el["kind"], "role": el.get("role"),
                                 "ir_hash": h, "fields": fields, "fingerprint": fp, "anchor": anchor, "ir": el}
            if el["kind"] == "image":
                entry["picture"] = picture_json((out / text(el["file"], "element.file")).read_bytes())
            entry_els.append(entry)
        made.append({"key": key, "label": s.get("label"), "title": title_of(s),
                     "page": s["page"], "text": " ".join(identity.plain_text(e) for e in els),
                     "layout": "TITLE_ONLY", "background": f"color:{text(s['bg'], 'slide.bg')}",
                     "notes": s.get("notes") or "", "elements": jlist(entry_els)})
    return made


def _element_keys(doc: Mapping[str, Json], out: Path) -> tuple[list[list[str]], list[list[JsonObject]]]:
    found = [identity.slide_element_keys(elements(s), out, None) for s in slides(doc)]
    return [[k for k in keys] for keys, _ in found], [fps for _, fps in found]


def build_base(doc: Mapping[str, Json], out: Path) -> JsonObject:
    infos = [slide_info(s) for s in slides(doc)]
    keys = identity.slide_keys(infos)
    ekeys, fps = _element_keys(doc, out)
    made = entries(doc, out, keys, ekeys, fps)
    for n, (s, entry) in enumerate(zip(slides(doc), made)):
        sid = f"b2s_s{n:03}"
        els = elements(s)
        by_id = {text(e["id"], "element.id"): e for e in els}
        ids = list(by_id)
        anchors = [opt_text(e.get("anchor"), "element.anchor") for e in els]
        anchored = {a for a in anchors if a is not None and a in by_id}
        # blocks: emit groups a block's panels with its content (`emit.block_groups`), a group no
        # element owns (the base lists it under `groups`); its children are in element order, so
        # the panel is under the text on it
        blocks: dict[ObjectId, list[ObjectId]] = {}
        for k, el in enumerate(els):
            if el.get("block") is not None:
                blocks.setdefault(ObjectId(f"{sid}_blk{el['block']}"), []).append(ObjectId(f"{sid}_e{k}"))
        blocks = {g: kids for g, kids in blocks.items() if len(kids) >= 2}
        block_of = {oid: g for g, kids in blocks.items() for oid in kids}
        order: list[ObjectId] = []
        rbs: dict[ObjectId, ReadBack] = {}
        z = 0
        for k, (el, e, anchor) in enumerate(zip(els, elements(entry), anchors)):
            oid = ObjectId(f"{sid}_e{k}")
            own = text(el["id"], "element.id") in anchored
            group = ObjectId(f"{sid}_e{ids.index(anchor)}_g") if anchor is not None and anchor in by_id else \
                (ObjectId(f"{oid}_g") if own else block_of.get(oid))
            rb = object_readback(el, out, group, z)
            rbs.setdefault(oid, rb)
            e["objects"] = jstrs([oid] + ([group] if own and group is not None else []))
            e["main"] = oid
            readbacks: JsonObject = {oid: readback_json(rb)}
            if own and group is not None:
                kids = [ObjectId(f"{sid}_e{j}") for j, x in enumerate(anchors) if x == el["id"]] + [oid]
                box = [v * SCALE for v in nums(el["bbox"], "element.bbox")]
                readbacks[group] = readback_json(group_readback(box, z, kids))
                order.append(group)
            elif group is None:
                order.append(oid)
            elif group in blocks and group not in order:
                order.append(group)
            e["readback"] = readbacks
            z += 1
        groups: JsonObject = {}
        for g, kids in blocks.items():
            boxes = [rbs[o].box for o in kids]
            groups[g] = readback_json(group_readback([min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                      max(b[2] for b in boxes), max(b[3] for b in boxes)], 0, kids))
        entry["objectId"] = sid
        entry["layoutObjectId"] = "L"
        entry["background_readback"] = {"color": s["bg"]}
        entry["notes_readback"] = s.get("notes") or ""
        entry["groups"] = jstrs(list(blocks))
        entry["order"] = jstrs(order)
        entry["group_readback"] = groups   # (fuzz world only: what `live_of` puts on the page)
    return {"version": 1, "generation": 1, "presentationId": "P", "revisionId": "r0",
            "source": {"pdf": "talk.pdf", "sha1": "x"}, "scale": SCALE, "page_size": [360.0, 202.5],
            "deck_page_size": [720.0, 405.0], "master_background": "color:#ffffff",
            "master_readback": {"color": "#ffffff"}, "slides": jlist(made)}


def build_adopt_base(doc: Mapping[str, Json], out: Path, rng: random.Random) -> JsonObject:
    """The base `adopt_sync.record` leaves behind, in the shape this world can apply a plan to.

    `build_base` is the base `convert` writes: generation 1, every object made by this converter
    under an id it chose, groups where emit made them. An adopted deck is none of that, and the
    three differences are exactly what the first sync has to survive:

    - `origin` is "adopt" and the generation is 0 - nothing has ever been written to this deck;
    - the objects carry a person's own ids (Slides' own shape, not `b2s_*`), so nothing that
      recognises this converter's work recognises them, and `sync.plan_recovery` can never sweep
      one;
    - some elements have no object at all. `adopt_sync.pair_elements` refuses to guess where two
      boxes are equally close, and an element with `objects: []` is one the source draws and
      nothing in the deck is known to be. How many there are is the deck's own affair - over the
      29-deck corpus, between 24% (gdg24) and 90% (hebrew-lesson) of the elements are unpaired,
      being how far `classify` regroups a person's boxes into one element - so the rate is drawn
      per deck rather than fixed. It was 15% flat, which is gentler than every real deck measured,
      and the campaign's whole job here is the deck that is not gentle.

    Groups are gone as well: adopt records none (`groups: []`), because a group on an adopted slide
    was made by the person and is theirs to keep.

    **And the box an unpaired element could not be tied to is still on the slide.** That is the
    whole shape of an adopted deck and this world used to take it off the page: an element the
    pairing refused had its object deleted along with the pairing, so the campaign ran against a
    deck where the person's own unpaired content simply did not exist - 10-80% of a real one.
    Everything that reads the live deck was blind to it. `merge.user_objects` saw none, so the
    loss oracle guarded none, so nothing could observe a created object landing on somebody's box
    or a second box appearing beside it, which is exactly the harm the first sync into a person's
    deck can do. `adopt.left_alone` is what a real base calls these, and they are what
    `fuzz_sync._doubled` watches; `left_object` on the element is this world saying which box it
    was, a fact about the deck it drew and not a rule about what may be written to it."""
    base = build_base(doc, out)
    rate = rng.uniform(0.0, 0.9)   # this deck's own (see above); 0 is a deck that paired throughout
    unpaired = 0
    alone: list[Json] = []
    base_slides = slides(base)
    for n, entry in enumerate(base_slides):
        sid = f"gx{n:x}{h6(str(n))}"
        rename = {text(entry["objectId"], "slide.objectId"): sid}
        els = elements(entry)
        for k, el in enumerate(els):
            for j, oid in enumerate(arr(el["objects"], "element.objects")):
                was = text(oid, "element.objects")
                rename[was] = f"{sid}_{k:x}{j}{h6(was)[:3]}"
        entry["objectId"] = sid
        entry["groups"] = jstrs(())
        entry["group_readback"] = obj({}, "slide.group_readback")
        # An anchored picture never pairs and leaves nothing behind either: the icon at the head of
        # a line and the picture of a formula in it were drawn out of the person's *text box*, and
        # nothing in the deck is shaped like one alone. So it has no object of its own and names the
        # member that has the box's (`adopt_sync.drawn_from`) - the one blind member a unit may be
        # written over, and only while that member really is tied to something: where the box itself
        # went unpaired below, `merge.covered` finds no object to delete and the unit stays frozen.
        keys = {el["key"] for el in els}
        drawn = {el["key"] for el in els if el.get("anchor") in keys}
        left: JsonObject = {}
        order: list[Json] = []
        for el in els:
            oids: list[str] = [rename[text(o, "element.objects")] for o in arr(el["objects"], "element.objects")][:1]
            main = text(el["main"], "element.main") if oids else ""
            rb: JsonObject | None = {**obj(obj(el["readback"], "element.readback")[main], "readback"),
                                     "parent_group": None} if oids else None
            refused = bool(oids) and rng.random() < rate   # a pairing adopt refused to make
            if el["key"] in drawn:
                oids = []
                rb = None
                el["drawn_from"] = el["anchor"]
            elif refused:
                left[oids[0]] = rb                        # the person's box, standing where it is
                el["left_object"] = oids[0]
                oids = []
                rb = None
                unpaired += 1
            tied: JsonObject = {}
            if oids:
                tied[oids[0]] = rb
            el["readback"] = tied
            el["objects"], el["main"] = jstrs(oids), oids[0] if oids else None
            if oids:
                order += oids
            elif el.get("left_object"):
                order.append(el["left_object"])
        entry["left_readback"] = left   # (fuzz world only: what `live_of` puts on the page)
        entry["order"] = order
        if left:
            entry["left_alone"] = jstrs(list(left))   # (a real base says this too: `adopt_sync.build_base`)
            alone.append({"slide": entry["key"], "objects": jstrs(list(left))})
    return {**base, "generation": 0, "origin": "adopt", "master_background": None,
            "adopt": {"presentationId": base["presentationId"], "deck_page_size": base["deck_page_size"],
                      "frame_width": 720.0, "slides": len(base_slides), "unpaired": unpaired,
                      "left_alone": alone}}


@dataclass(frozen=True, kw_only=True)
class WorldOurs:
    """The new conversion as `sync.build_ours` hands it to the merge: `json` its dict (what sync's
    dict readers take), `typed` the same parsed (`merge.ours_of`), `out` where its pictures are and
    `base_forms` what bringing the base to today's form found (`sync.base_today`)."""
    json: JsonObject
    typed: merge.Ours
    out: Path
    base_forms: list[snapshot.BaseForm]


def build_ours(doc: JsonObject, base: JsonObject, out: Path) -> WorldOurs:
    infos = identity.slide_infos([slide_info(s) for s in slides(doc)])
    base_slides = slides(base)
    base_keys = [text(b["key"], "slide.key") for b in base_slides]
    base_infos = [identity.base_slide_info(b, k) for b, k in zip(base_slides, base_keys)]
    found = identity.label_moves_of(base_infos, infos)
    moves = [identity.reported_move(m, base_keys, infos) for m in found]
    # `weak_pairs` and `near_misses` are what `merge.plan_merge_of` turns into the two warnings about
    # identity - a pairing the words could as well have made elsewhere, and a frame nothing could
    # pair at all. Leaving them out here made the campaign judge a world where the person is never
    # told that, so a frame the alignment put on a look-alike slide read as a loss in silence when
    # the product would have named the slide and asked for a label (`identity.align_slides`).
    weak: dict[int, str] = {}
    keys, pairs = identity.inherit_slide_keys(base_infos, base_keys, infos, found, weak)
    near = [identity.reported_near_miss(m, base_keys, infos)
            for m in identity.near_misses_of(base_infos, infos, pairs)]
    ekeys: list[list[str]] = []
    fps: list[list[JsonObject]] = []
    for j, s in enumerate(slides(doc)):
        matched = identity.base_items(elements(base_slides[pairs[j]])) if j in pairs else None
        k, f = identity.slide_element_keys(elements(s), out, matched)
        ekeys.append([x for x in k])
        fps.append(f)
    made = entries(doc, out, keys, ekeys, fps)
    # (the base read in today's form before the merge compares it, as `sync.build_ours` does; this
    # world's elements are partial IR the parser refuses, so its bases come back as recorded)
    kept = snapshot.PictureFolders(kept=(out,), rendered=None, held=None)
    forms = sync.base_today(base, doc, snapshot.find_base_pictures(base, kept))
    said: JsonObject = {"slides": jlist(made), "pairs": {str(k): v for k, v in pairs.items()},
                        "label_moves": jlist(moves), "weak_pairs": {str(k): v for k, v in weak.items()},
                        "near_misses": jlist(near)}
    return WorldOurs(json=said, typed=merge.ours_of(said), out=out, base_forms=forms)


def live_of(base: Mapping[str, Json]) -> LiveDeck:
    made: list[LiveSlide] = []
    for s in slides(base):
        objects: dict[ObjectId, ReadBack] = {}
        for el in elements(s):
            for oid, rb in obj(el["readback"], "element.readback").items():
                objects[ObjectId(oid)] = sync_readback(rb, oid)
        for oid, rb in obj(s.get("group_readback") or {}, "slide.group_readback").items():
            objects[ObjectId(oid)] = sync_readback(rb, oid)
        # the person's own boxes an adopted base could tie to nothing: on the page, named by no
        # element, and nothing this converter writes may take them away (`build_adopt_base`)
        for oid, rb in obj(s.get("left_readback") or {}, "slide.left_readback").items():
            objects[ObjectId(oid)] = sync_readback(rb, oid)
        sid = text(s["objectId"], "slide.objectId")
        made.append(LiveSlide(object_id=ObjectId(sid), layout_object_id="L",
                              background=copy.deepcopy(obj(s["background_readback"], "slide.background_readback")),
                              notes=text(s["notes_readback"], "slide.notes_readback"), notes_id=f"{sid}_notes",
                              order=[ObjectId(text(o, "slide.order")) for o in arr(s["order"], "slide.order")],
                              objects=objects))
    return LiveDeck(presentation_id="P", revision_id="r0", page_size=(720.0, 405.0), layouts={"L": "TITLE_ONLY"},
                    master_background={"color": "#ffffff"}, slides=made)


def sync_readback(v: Json, oid: str) -> ReadBack:
    """A base's read-back of one object, as a record of the live deck (a deep copy of its JSON)."""
    return sync_model.readback(copy.deepcopy(v), f"object {oid}")


# ---------------------------------------------------------------- the reference sync

def h6(words: str) -> str:
    return identity.sha1(words)[:6]


def made_id(slide_key: str, element_key: str, tok: str) -> ObjectId:
    """The id this world gives the object it creates for an element (`new_object`)."""
    return ObjectId(f"b2s_{h6(slide_key)}_{h6(element_key)}_{tok}")


def shifted(rb: ReadBack, dx: float, dy: float) -> ReadBack:
    """`rb` moved by (dx, dy) deck pt, box and transform alike."""
    b, t = rb.box, rb.transform
    return replace(rb, box=(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy), transform=(*t[:4], t[4] + dx, t[5] + dy))


def _styling_ends(base_rb: ReadBack | None, live_rb: ReadBack, written: str | None) -> bool:
    """Whether the deck's run styling has anything to go back onto. `sync.style_range_requests`
    maps each styled run through the matching blocks of the live text and the text sync is about to
    write, so a run still sitting on something the source kept goes back on; a run whose characters
    are all gone is styling that simply ends. (`merge.styling_lost` decides the same thing in the
    planner, by words; this is the reference applier's own opinion of it, on purpose - but it has to
    be the mechanism's opinion. Reading it word by word had the applier throw away styling sync
    really does re-apply, and the campaign accused the merge of it: offline seed 23599 --shape
    adopt, where the person's own earlier rewording had clipped their bold to two letters inside a
    word, which is not a word and was not looked for in the new text.)"""
    base_styles = base_rb.text_styles if base_rb is not None else ()
    before, after = live_rb.text or "", written or ""
    spans = [(s, e) for s, e, style in (live_rb.run_spans or ()) if style not in base_styles]
    if not spans:
        return False
    blocks = SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
    return any(not any(min(end, i + n) > max(start, i) for i, _, n in blocks) for start, end in spans)


def new_object(skey: str, o_el: Mapping[str, Json], base_el: ElementEntry | None, theirs: Mapping[ObjectId, ReadBack],
               overrides: merge.Overrides | None, tok: str) -> tuple[ObjectId, ReadBack, ReadBack]:
    """The object a correct sync creates for one ours element (docs/sync.md: ours content, with the
    deck's overrides re-applied), and the read-back of it before they were: what the base records
    (`as_created`)."""
    ir = obj(o_el["ir"], "element.ir")
    box = [v * SCALE for v in nums(ir["bbox"], "element.bbox")]
    words = plain = element_text(ir)
    ov = overrides.text if overrides is not None else None
    if ov is not None and words is not None:
        if isinstance(ov, merge.TableOverride):
            cells = merge.table_merge(ov.base, words, ov.theirs, ov.dims, ov.dims)
            words = "\n".join("\t".join(r) for r in cells[0]) if cells else ov.theirs
        else:
            # `sync.override_requests` merges by paragraph, not by word, and carries the
            # paragraphs a person settled for the source: the applier has to do both, or a
            # `--take-source` would look like a loss to the oracle.
            words = merge.text_merge(merge.collapse_holes(ov.base), merge.collapse_holes(words),
                                     merge.collapse_holes(ov.theirs), ov.take)[0]
    main = base_el.main if base_el is not None else None
    parent = theirs[main].parent_group if main is not None and main in theirs else None
    if parent is not None and parent not in theirs:
        parent = None
    key = text(o_el["key"], "element.key")
    oid = made_id(skey, key, tok)
    if ir["kind"] == "image":
        rb = readback("image", box, text=None, image=image_read(o_el["picture"], "element.picture"), parent=parent,
                      title=None, z=0, table=None, fill=None)
    elif ir["kind"] == "table":
        rows = arr(ir["cells"], "table.cells")
        rb = readback("table", box, text=words, image=None, parent=parent, title=None, z=0,
                      table=(len(rows), len(arr(rows[0], "table.row"))), fill=None)
    else:
        rb = readback("shape", box, text=words, image=None, parent=parent, title=None, z=0, table=None,
                      fill=panel_fill(ir))
    rb = styled(replace(rb, title=snapshot.tag(skey, key)), ir)
    pre = replace(rb, text=plain)
    live_rb = theirs.get(main) if main is not None else None
    base_rb = base_el.readback.get(main) if base_el is not None and main is not None else None
    style = overrides.text_style if overrides is not None else None
    if live_rb is not None and style is not None:  # the deck's styling, re-applied
        if style.ranges and _styling_ends(base_rb, live_rb, rb.text):
            pass                       # the words it was on are gone: nothing to put the styling on
        else:
            rb = replace(rb, text_style_hash=live_rb.text_style_hash, text_styles=copy.deepcopy(live_rb.text_styles),
                         run_spans=copy.deepcopy(live_rb.run_spans or ()))
    if live_rb is not None and overrides is not None and overrides.shape_style:
        rb = replace(rb, shape_style_hash=live_rb.shape_style_hash, shape_style=copy.deepcopy(live_rb.shape_style))
    return oid, rb, pre


@dataclass(frozen=True, kw_only=True)
class Applied:
    """The live deck as `apply_plan` leaves it, and per object this sync made its read-back before
    the deck's overrides went back on (`as_created`)."""
    deck: LiveDeck
    as_created: dict[ObjectId, ReadBack]


def apply_plan(base: Base, ours: Mapping[str, Json], theirs: LiveDeck, plan: merge.MergePlan, tok: str) -> Applied:
    """The live deck as a correct sync would leave it after writing `plan` (`ours`: the new
    conversion's JSON, whose elements carry their IR and pictures).

    `as_created` holds, per object this sync made, its read-back before the deck's overrides went
    back on - the text as ours says it, ours' styling, the converter's box. That is what `rebase`
    records, as `sync.Sync.new_base` does from `Sync.created` (docs/sync.md: the base is converter
    output). Recorded from the final deck instead, a merged edit became the base's own: the person's
    move carried onto a recreated box read as unmoved from then on, so the next source change put the
    box back at the converter's place and nothing called that a loss (and the step after it, the
    person moving it again, got carried twice: seed 93863 at chain 4)."""
    after = copy_deck(theirs)
    created: dict[ObjectId, ReadBack] = {}
    by_id = {s.object_id: s for s in after.slides}
    ours_slides = slides(ours)
    made: dict[str, LiveSlide] = {}
    dropped: set[ObjectId] = set()
    for p in plan.slides:
        if isinstance(p, merge.DeleteSlide):
            dropped.add(p.object_id)
        elif isinstance(p, merge.CreateSlide):
            o = ours_slides[p.ours]
            okey = text(o["key"], "slide.key")
            sid = ObjectId(f"b2s_{h6(p.key)}_{tok}")
            objects: dict[ObjectId, ReadBack] = {}
            for el in elements(o):
                oid, rb, _ = new_object(okey, el, None, {}, None, tok)
                objects[oid] = rb
            made[f"new:{p.key}"] = LiveSlide(
                object_id=sid, layout_object_id="L",
                background={"color": text(o["background"], "slide.background").split(":", 1)[1]},
                notes=opt_text(o.get("notes"), "slide.notes") or "", notes_id=f"{sid}_notes",
                order=list(objects), objects=objects)
        elif isinstance(p, merge.UpdateSlide):
            _update_slide(base.slides[p.base], ours_slides[p.ours], by_id[p.object_id], p.units, tok, created)
            if p.background_written and p.background:
                by_id[p.object_id].background = {"color": p.background.split(":", 1)[1]}
            if p.notes is not None:
                by_id[p.object_id].notes = p.notes
            _drop_lonely_groups(by_id[p.object_id])
        elif isinstance(p, merge.HoldSlide):
            # nothing is written to it, but the page is read as a sync would leave it
            _update_slide(base.slides[p.base], ours_slides[p.ours], by_id[p.object_id], (), tok, created)
            _drop_lonely_groups(by_id[p.object_id])
        elif isinstance(p, (merge.GoneSlide, merge.KeepRemovedSlide)):
            pass
        else:
            assert_never(p)
    order: list[LiveSlide] = []
    for x in plan.order:
        if x.startswith("new:"):
            order.append(made[x])
        elif x in by_id and x not in dropped:
            order.append(by_id[ObjectId(x)])
    for s in after.slides:  # (anything the plan forgot keeps its place at the end)
        if s.object_id not in dropped and s not in order:
            order.append(s)
    after.slides = order
    return Applied(deck=after, as_created=created)


def _set(live: LiveSlide, mirror: dict[ObjectId, ReadBack] | None, oid: ObjectId, rb: ReadBack) -> None:
    """`rb` in `oid`'s place, on the page and in `mirror` - the deck's own version of the slide
    `_update_slide` read before it wrote, which sees what happens to an object it still holds."""
    live.objects[oid] = rb
    if mirror is not None and oid in mirror:
        mirror[oid] = rb


def _update_slide(b: SlideEntry, o: Mapping[str, Json], live: LiveSlide, units: Sequence[merge.PlannedUnit],
                  tok: str, created: dict[ObjectId, ReadBack]) -> None:
    bu = merge.units_of(b.elements)
    ou = merge.units(elements(o))
    okey = text(o["key"], "slide.key")
    # The deck's own version of the objects, read before anything is deleted: that is what the
    # overrides (the deck's box, its styling) are re-applied from.
    theirs = dict(live.objects)
    deck_order = list(live.order)   # before anything takes anything's place
    fresh: list[ObjectId] = []   # page objects that went on top because nothing of theirs held their place
    for u in units:
        d = u.decision
        members = bu.get(d.key, [])
        if isinstance(d, (merge.KeepUnit, merge.KeepRemoved, merge.KeptJoined, merge.GoneUnit, merge.AdoptUnit,
                          merge.AdoptObject)):
            continue
        if isinstance(d, merge.DeleteUnit):
            for m in members:
                for oid in m.objects:
                    live.objects.pop(oid, None)
            continue
        if isinstance(d, merge.MoveUnit):
            dx, dy = d.delta[0] * SCALE, d.delta[1] * SCALE
            for m in members:
                for oid in m.objects:
                    rb = live.objects.get(oid)
                    if rb is not None:
                        _set(live, theirs, oid, shifted(rb, dx, dy))
            continue
        if isinstance(d, merge.Recreate):
            overrides: merge.Overrides | None = d.overrides
        elif isinstance(d, merge.CreateUnit):
            overrides = None
        else:
            assert_never(d)
        parents: dict[str, ObjectId | None] = {}
        for m in members:
            now = live.objects.get(m.main) if m.main is not None else None
            parents[m.key] = now.parent_group if now is not None else None
        # where each old object stands in the z-order: its replacement takes that place, in its
        # group too (what `sync.Sync.restack` and `regroup_requests` are there to achieve)
        slots = {m.key: m.main for m in members if m.main is not None and m.main in live.objects}
        if isinstance(d, merge.Recreate):
            # (sync ungroups, deletes, creates and regroups under the same group ids)
            for m in members:
                for oid in m.objects:
                    was = live.objects.get(oid)
                    if was is None or was.kind != "elementGroup":
                        live.objects.pop(oid, None)
        base_by = {m.key: m for m in members}
        made: list[ObjectId] = []
        for m in ou.get(d.key, []):
            mkey = ElementKey(text(m["key"], "element.key"))
            oid, rb, pre = new_object(okey, m, base_by.get(mkey), theirs, overrides if mkey == d.key else None, tok)
            parent = parents.get(mkey)
            parent = parent if parent is not None and parent in live.objects else None
            rb, pre = replace(rb, parent_group=parent), replace(pre, parent_group=parent)
            live.objects[oid] = rb
            created[oid] = pre
            slot = slots.get(mkey)
            if not (slot and _take_place(live, slot, [oid], theirs)):
                # new: on top (of its group)
                if parent is not None:
                    group = live.objects[parent]
                    _set(live, theirs, parent, replace(group, children=(*(group.children or ()), oid)))
                else:
                    live.order.append(oid)
                    fresh.append(oid)
            made.append(oid)
        if overrides is not None and overrides.geometry:
            _place_unit(d.key, members, theirs, live, made, base_by)
    _page_order(live, b, o, deck_order, fresh, tok)
    _restack(live, b, o, fresh, tok)
    _regroup_order(live, b, o, tok)


def _page_order(live: LiveSlide, b: SlideEntry, o: Mapping[str, Json], deck_order: Sequence[ObjectId],
                tok_fresh: Sequence[ObjectId], tok: str) -> None:
    """The outcome of `sync.Sync._by_the_source` on the page: the source's own elements take the
    source's order among themselves, in the places they hold, where the deck still has them in the
    order the base does. A rewritten object takes its old slot (`_take_place`), so without this the
    applier kept an order the last conversion drew and this one contradicts - a panel the source now
    draws under a text came back on top of it (converted seed 610106, chain 10). The elements this
    sync *keeps* count too, or a slide with one rewritten element has nothing to be ordered against
    (seed 1500512 at chain 10). What is ordered are the *page elements*, a converter group standing
    for the elements it carries (the first of them the source draws), or a block's panel could not be
    ordered against a table beside it however the source drew them (seed 1300381 at chain 6). A group
    the person made stands for nobody: moving it moves everything else they put in there, which is
    the one shape of this the sync answers by talking (`sync.folded_hiders`)."""
    order, objects = live.order, live.objects
    okey = text(o["key"], "slide.key")
    keys = [text(el["key"], "element.key") for el in elements(o)]
    rank = {k: i for i, k in enumerate(keys)}
    base_main: dict[str, ObjectId | None] = {el.key: el.main for el in b.elements}
    # A container the base itself draws on the page - a converter group. Not the base's `groups`,
    # which a rebase carries over even after the person took that group apart (converted seed 1500512).
    base_order: list[ObjectId] = list(b.seen.order) if b.seen is not None else []
    groups = set(base_order)
    base_stand: dict[str, ObjectId | None] = {}
    for el in b.elements:
        rb = el.readback.get(el.main) if el.main is not None else None
        parent = rb.parent_group if rb is not None else None
        base_stand[el.key] = parent if parent is not None and parent in groups else el.main
    at: dict[ObjectId, str] = {}
    was: dict[ObjectId, ObjectId | None] = {}
    for k in keys:
        made = made_id(okey, k, tok)
        oid = made if made in objects else base_main.get(k)
        if not oid or not base_main.get(k) or oid in tok_fresh:
            continue
        now = objects.get(oid)
        parent = now.parent_group if now is not None else None
        stands = parent if parent is not None and parent in groups else oid
        if stands not in at:                # the first element the source draws in there speaks
            at[stands], was[stands] = k, base_stand[k]
    slots = [i for i, oid in enumerate(order) if oid in at]
    here = [order[i] for i in slots]
    if len(here) < 2:
        return
    where: dict[ObjectId, int] = {}
    for x in here:
        w = was[x]
        if w is None or w not in deck_order or w not in base_order:
            return
        where[x] = base_order.index(w)
    if here != sorted(here, key=lambda x: where[x]):
        return                                  # the person restacked: their order stands
    for i, oid in zip(slots, sorted(here, key=lambda x: rank[at[x]])):
        order[i] = oid


def _regroup_order(live: LiveSlide, b: SlideEntry, o: Mapping[str, Json], tok: str) -> None:
    """Inside a group a rewrite takes apart, the children that are elements of the source take the
    source's order among themselves, in the places they hold; anything else in there keeps its slot.
    This is the outcome; `sync.Sync.regroup_requests` is the mechanism (BRING_TO_FRONT each child,
    then `groupObjects`), which the campaign replays through `fuzz_sync._stacked`. Without it a
    group kept the deck's child order, which is what the last conversion drew and nobody's edit
    (Slides will not restack inside a group), and the panel the source now draws *under* a text came
    back on top of it (`loss_oracle.text_hidden`: converted seed 79045 over a text the sync wrote,
    then 670146 and adopt-shaped 660326 and 680477 over one it kept)."""
    okey = text(o["key"], "slide.key")
    keys = [text(el["key"], "element.key") for el in elements(o)]
    pos = {k: i for i, k in enumerate(keys)}
    rank: dict[ObjectId, int] = {made_id(okey, k, tok): i for k, i in pos.items()}
    for el in b.elements:                    # the deck's own objects stand for the kept elements
        for oid in el.objects:
            if el.key in pos:
                rank.setdefault(oid, pos[el.key])
    for gid, rb in list(live.objects.items()):
        kids = list(rb.children or ())
        slots = [i for i, c in enumerate(kids) if c in rank]
        if len(slots) > 1:
            for slot, oid in zip(slots, sorted((kids[i] for i in slots), key=lambda c: rank[c])):
                kids[slot] = oid
            live.objects[gid] = replace(rb, children=tuple(kids))


def _page_element(objects: Mapping[ObjectId, ReadBack], oid: ObjectId) -> ObjectId:
    """The page element that carries `oid`: itself, or the outermost group it is in."""
    for _ in range(16):
        rb = objects.get(oid)
        parent = rb.parent_group if rb is not None else None
        if not parent or parent not in objects:
            break
        oid = parent
    return oid


def _restack(live: LiveSlide, b: SlideEntry, o: Mapping[str, Json], fresh: Sequence[ObjectId], tok: str) -> None:
    """An element the source added goes where the source draws it, not on top. Slides puts every
    object it creates in front of everything, so the page order after a rewrite is nothing the
    conversion asked for; a correct sync brings it back, placing each new object after the last
    element before it in ours order (at the bottom when none of them is on the page). This is the
    outcome; the mechanism is `sync.Sync.restack`, which the offline campaign replays apart
    (`fuzz_sync._restacked`), so the rule is also pinned by
    `test_a_created_shape_stays_under_the_text_the_source_draws_above_it` and
    `test_an_element_a_dissolved_group_frees_onto_the_page_takes_the_sources_place`. Without it the applier stacked a
    new panel over body text the conversion draws above it, and the oracle called that `text_hidden`
    - 11 of 1000 adopt-shaped rounds, all of them the harness's own doing."""
    order = live.order
    objects = live.objects
    fresh = [x for x in fresh if x in order]
    base_main: dict[str, ObjectId | None] = {el.key: el.main for el in b.elements}
    okey = text(o["key"], "slide.key")
    keys = [text(el["key"], "element.key") for el in elements(o)]
    rank = {k: i for i, k in enumerate(keys)}
    made = {made_id(okey, k, tok): k for k in keys}
    stands: dict[str, ObjectId] = {}   # key -> its page element
    at_rank: dict[ObjectId, int] = {}  # where the source draws each object
    for k in keys:
        oid = made_id(okey, k, tok)
        for x in (oid, base_main.get(k)):
            if x is not None and x in objects:
                at_rank[x] = min(at_rank.get(x, rank[k]), rank[k])
        standing = oid if oid in objects else base_main.get(k)
        if standing is not None and standing in objects and (t := _page_element(objects, standing)) in order:
            stands[k] = t
    for oid in fresh:
        key = made.get(oid)
        if key is None or oid not in order:
            continue
        order.remove(oid)
        i, pos = keys.index(key), 0
        for k in reversed(keys[:i]):
            prev = stands.get(k)
            if prev is not None and prev in order and prev != oid:
                pos = order.index(prev) + 1
                break
        for k in keys[i + 1:]:          # and never above what the source draws above it
            nxt = stands.get(k)
            if nxt is not None and nxt in order and nxt != oid:
                pos = min(pos, order.index(nxt))
                break
        order.insert(pos, oid)
    _not_over_kept(live, at_rank, made, rank)


def _not_over_kept(live: LiveSlide, at_rank: Mapping[ObjectId, int], made: Mapping[ObjectId, str],
                   rank: Mapping[str, int]) -> None:
    """Nothing the sync wrote ends up above words only the deck has: a text the source dropped and
    the deck's edits kept alive, or one the person drew themselves. The source's order says where an
    element goes among the source's own and nothing at all about those, and a panel that grew over
    such a text hides work nobody can get back, while the price of going under it is z-order
    (`sync.Sync.restack`'s last pass; `loss_oracle.text_hidden`, seed 680477).

    What moves is the page **element** the new shape is drawn inside - the converter group this
    rewrite rebuilt, most often the block the panel belongs to. Asked of the object alone the rule
    reached nothing when the panel was in a block, its id being in no page order, and a panel the
    source had just grown covered a text the source no longer has (converted seed 2300025 at chain
    12). Which words are the deck's own is asked of each **text** (`drawn`), not of the page element
    holding it: a group the person made may hold one of their text boxes beside one of the
    converter's (converted seed 5200496 at chain 12).

    Nor above words the source draws above it and the page order could not carry (`at_rank`): a page
    element stands for every converter element inside it and `_page_order` ranks it by the first of
    them, so a group holding two of them with a panel drawn *between* is a place where the source's
    order cannot be honoured at all - the group goes under the panel for the sake of the text below
    it, taking the text above it with it. There the words are the source's own and this pass was told
    to keep quiet about them (converted seed 8300231 at chain 11)."""
    order, objects = live.order, live.objects
    view = readbacks_json(objects)   # (what `sync.would_hide` reads)
    stand_rank: dict[ObjectId, int] = {}                 # page element -> the first of them it stands for
    for oid, r in at_rank.items():
        el = _page_element(objects, oid)
        stand_rank[el] = min(stand_rank.get(el, r), r)
    for m, key in made.items():
        if m not in objects:
            continue
        oid = _page_element(objects, m)
        if oid not in order:
            continue
        r = rank.get(key)
        drawn: set[str] = {x for x, xr in at_rank.items()
                           if r is None or xr <= r or stand_rank.get(_page_element(objects, x), r) >= r}
        i = order.index(oid)
        for j, other in enumerate(order[:i]):
            if sync.would_hide(view, m, other, drawn):
                order.insert(j, order.pop(i))
                break


OVERRIDDEN = ("text", "text_styles", "paragraph_styles", "run_spans", "text_style_hash", "shape_style",
              "shape_style_hash", "box", "transform", "size")   # what `sync.Sync.override_requests` writes


def _as_made(rb: ReadBack, pre: ReadBack) -> ReadBack:
    """`rb` with the fields an override writes (`OVERRIDDEN`) as the sync made the object."""
    return replace(rb, text=pre.text, text_styles=pre.text_styles, paragraph_styles=pre.paragraph_styles,
                   run_spans=pre.run_spans, text_style_hash=pre.text_style_hash, shape_style=pre.shape_style,
                   shape_style_hash=pre.shape_style_hash, box=pre.box, transform=pre.transform, size=pre.size)


FIELDS: dict[str, tuple[str, ...]] = {"text": ("text",), "geometry": ("box", "transform", "size"),
                                      "image": ("image", "box", "transform", "size")}
"""The read-back keys that show each field a unit adopts from the deck (`merge.AdoptUnit.adopt`)."""


def rebase(base: JsonObject, ours: Mapping[str, Json], after: Applied, plan: merge.MergePlan, tok: str) -> JsonObject:
    """The base a correct sync records for the next one: ours IR plus the read-back of the objects
    as it made them, before the deck's overrides went back on (docs/sync.md, "After writing";
    `apply_plan`'s `as_created`). Units kept from the deck carry their old base,
    so a chain of syncs never forgets what the person's version was."""
    now = {s.object_id: s for s in after.deck.slides}
    base_slides, ours_slides = slides(base), slides(ours)
    made_entries: dict[str, JsonObject] = {}
    sids: dict[int, str] = {}   # the plan's slide at this index -> its slide id in the new base
    for n, p in enumerate(plan.slides):
        if isinstance(p, merge.DeleteSlide):
            continue
        if isinstance(p, (merge.KeepRemovedSlide, merge.GoneSlide)):
            sid = p.object_id if isinstance(p, merge.KeepRemovedSlide) else f"gone:{p.key}"
            # a frame the source dropped keeps no label: it may be on another frame tomorrow, and
            # it says so (`removed`), or it reads as well as the live slide for the frame that
            # carries its words (sync.new_base, identity.align_slides)
            made_entries[sid] = {**base_slides[p.base], "label": None, "removed": True}
            if isinstance(p, merge.GoneSlide):
                sids[n] = sid  # it keeps its place in the source's order (see the ordering below)
                o = ours_slides[p.ours]  # ... and says what the source says (sync.new_base)
                made_entries[sid] = {**made_entries[sid], "removed": False,
                                     **{k: o.get(k) for k in ("label", "title", "text", "page")}}
            continue
        if isinstance(p, merge.HoldSlide):
            # `merge.hold_slide`: nothing was written here, so the base says exactly what it said
            # before. The source's words are not recorded as arrived, or the edit held back would
            # read as already made next time (sync.new_base).
            b = base_slides[p.base]
            bid = text(b["objectId"], "slide.objectId")
            made_entries[bid] = copy.deepcopy(b)
            sids[n] = bid
            continue
        o = ours_slides[p.ours]
        okey = text(o["key"], "slide.key")
        sid = f"b2s_{h6(p.key)}_{tok}" if isinstance(p, merge.CreateSlide) else p.object_id
        sids[n] = sid
        read = now.get(ObjectId(sid))
        read_objects: Mapping[ObjectId, ReadBack] = read.objects if read is not None else {}
        entry: JsonObject = {k: v for k, v in o.items() if k != "elements"}
        els: list[Mapping[str, Json]] = []
        if isinstance(p, merge.CreateSlide):
            for el in elements(o):
                els.append(_rebased_element(el, [made_id(okey, text(el["key"], "element.key"), tok)], read_objects))
            entry["objectId"] = sid
            entry["layoutObjectId"] = "L"
            entry["background_readback"] = read.background if read is not None else None
            entry["notes_readback"] = (read.notes if read is not None else "") or ""
            entry["groups"] = jstrs(())
            entry["order"] = jstrs(read.order if read is not None else [])
        elif isinstance(p, merge.UpdateSlide):
            b = base_slides[p.base]
            bunits, ounits = merge.units(elements(b)), merge.units(elements(o))
            index = {text(e["key"], "element.key"): e for e in elements(o)}
            # what this sync made, as it made it: before the overrides (`apply_plan`)
            # (only what an override writes: grouping and z-order are the final read's, as
            # `Sync.created` is read after the content and order phases)
            pre = after.as_created
            made = {oid: _as_made(rb, pre[oid]) if oid in pre else rb for oid, rb in read_objects.items()}
            for u in p.units:
                d = u.decision
                if isinstance(d, (merge.CreateUnit, merge.Recreate)):
                    for mk in u.ours_members:
                        els.append(_rebased_element(index[mk], [made_id(okey, mk, tok)], made))
                elif isinstance(d, merge.AdoptObject):
                    for mk in u.ours_members:
                        els.append(_rebased_element(index[mk], [d.object_id], read_objects))
                elif isinstance(d, (merge.MoveUnit, merge.AdoptUnit)):
                    # the deck's own objects stay: ours IR with their read-back (moved, or the
                    # fields the deck already showed)
                    for m in ounits[d.key]:
                        old = next((x for x in bunits[d.key] if x["key"] == m["key"]), None)
                        if old is None:
                            continue
                        rbs = obj(copy.deepcopy(old["readback"]), "element.readback")
                        for oid, rb in rbs.items():
                            live_obj = read_objects.get(ObjectId(oid))
                            if live_obj is None:
                                continue
                            shown = readback_json(live_obj)
                            if isinstance(d, merge.MoveUnit):
                                obj(rb, "readback").update({k: shown[k] for k in ("box", "transform") if k in shown})
                            elif oid == old.get("main"):
                                for f in d.adopt:
                                    obj(rb, "readback").update({k: shown[k] for k in FIELDS.get(f, ()) if k in shown})
                        els.append({**m, "objects": old["objects"], "main": old["main"], "readback": rbs})
                elif isinstance(d, (merge.KeepUnit, merge.KeepRemoved, merge.KeptJoined)):
                    # (a unit kept because the source dropped it says so: `merge.plan_unit`)
                    kept = bunits.get(d.key, [])
                    els += [{**m, "removed": True} for m in kept] if isinstance(d, merge.KeepRemoved) else kept
                elif isinstance(d, (merge.DeleteUnit, merge.GoneUnit)):
                    pass                                # gone
                else:
                    assert_never(d)
            entry["objectId"] = sid
            entry["layoutObjectId"] = b.get("layoutObjectId")
            entry["groups"] = list(arr(b.get("groups", []), "slide.groups"))
            entry["order"] = jstrs(read.order) if read is not None and read.order else \
                list(arr(b.get("order", []), "slide.order"))
            if b.get("left_readback"):
                # the person's unpaired boxes are still theirs a sync later (`build_adopt_base`,
                # `sync.new_base`); `left_object` rides along on the kept members, which carry
                # their old base entry
                entry["left_readback"] = copy.deepcopy(b["left_readback"])
                entry["left_alone"] = list(arr(b.get("left_alone") or [], "slide.left_alone"))
            if p.background_written and p.background:
                entry["background_readback"] = read.background if read is not None else None
            else:
                entry["background_readback"] = b.get("background_readback")
                if b.get("background") != o.get("background"):
                    entry["background"] = b.get("background")  # the deck's background stays a conflict
            if p.notes is not None:
                entry["notes_readback"] = o.get("notes") or ""
            else:
                entry["notes_readback"] = b.get("notes_readback", "")
                if (b.get("notes") or "") != (o.get("notes") or ""):
                    entry["notes"] = b.get("notes")
        else:
            assert_never(p)
        entry["elements"] = _maps(merge.keys_the_source_took(els))
        made_entries[sid] = entry
    # The base is converter output, so it is written in the source's order (a slide the deck deleted
    # while the source still has it keeps its place, or the next conversion can no longer align an
    # unlabelled frame with it and syncs the deleted slide back in). This is the product's own
    # ordering on purpose: `second_sync_writes` asks whether the base a sync leaves behind settles,
    # and a base ordered here the way sync *ought* to would hide it when sync stops doing that.
    said = merge.merge_plan_json(plan)
    by_plan = {id(pj): {"sid": sids[n]} for n, pj in enumerate(objs(said["slides"], "plan.slides"))
               if sids.get(n)}
    live_ids: list[str] = [s.object_id for s in after.deck.slides]
    order = sync.base_order(said, by_plan, live_ids)
    ordered = [made_entries.pop(sid) for sid in order if sid in made_entries] + list(made_entries.values())
    return {**base, "generation": as_int(base.get("generation", 0), "base.generation") + 1,
            "revisionId": after.deck.revision_id, "slides": jlist(ordered)}


def _rebased_element(el: Mapping[str, Json], oids: Sequence[ObjectId],
                     objects: Mapping[ObjectId, ReadBack]) -> JsonObject:
    mine = [oid for oid in oids if oid in objects]
    group = objects[mine[0]].parent_group if mine else None
    # like build_base: the unit's anchor owns the converter group its anchored pictures sit in;
    # a group the person drew around converter objects stays the person's.
    if not el.get("anchor") and group is not None and group in objects and group.startswith("b2s_") \
            and group.endswith("_g"):
        mine = mine + [group]
    readbacks: JsonObject = {oid: readback_json(objects[oid]) for oid in mine}
    return {**el, "objects": jstrs(mine), "main": mine[0] if mine else None, "readback": readbacks}


def _place_unit(key: ElementKey, members: Sequence[ElementEntry], theirs: Mapping[ObjectId, ReadBack], live: LiveSlide,
                made: Sequence[ObjectId], base_by: Mapping[ElementKey, ElementEntry]) -> None:
    """Re-apply the deck's move or resize to a rewritten unit: the whole unit takes the step, its
    anchored pictures with their text. The step comes from the old top's base and live read-backs
    (`delta`) and lands on the new unit wherever the source put it. This is the outcome, not the
    mechanism - sync writes one RELATIVE transform on the unit's group and, when the person has
    taken that group apart, one per member - and modelling the outcome is why the offline campaign
    could not see the step reaching only the top object (live seed 903,
    `tests/test_sync.py::test_a_moved_unit_with_no_group_is_moved_member_by_member`)."""
    if not made:
        return
    anchor = base_by.get(key) or (members[0] if members else None)
    read = SlideRead(object_id=live.object_id, layout_object_id=None, background=None, notes="", notes_id=None,
                     order=(), objects=dict(theirs))
    old_top = (merge.unit_top_of(members, read) if members else None) or (anchor.main if anchor is not None else None)
    if old_top is None:
        return
    base_rb = next((m.readback[old_top] for m in members if old_top in m.readback), None)
    theirs_rb = theirs.get(old_top)
    if base_rb is None or theirs_rb is None:
        return
    # The person's step goes on top of wherever the source put the rewritten unit - also when the
    # source moved it too (`sync.carried`; this world's deck edits are moves, never resizes).
    dx, dy = theirs_rb.box[0] - base_rb.box[0], theirs_rb.box[1] - base_rb.box[1]
    for oid in made:
        live.objects[oid] = shifted(live.objects[oid], dx, dy)


def _take_place(live: LiveSlide, old: ObjectId, new: Sequence[ObjectId], mirror: dict[ObjectId, ReadBack] | None) -> bool:
    """`new` takes `old`'s place in the z-order: among the page elements (`order`) or among its
    group's children. False when `old` stands nowhere. (`mirror`: see `_set`.)"""
    found = False
    if old in live.order:
        i = live.order.index(old)
        live.order[i:i + 1] = new
        found = True
    for gid, rb in list(live.objects.items()):
        if rb.kind != "elementGroup":
            continue
        kids = list(rb.children or ())
        if old in kids:
            i = kids.index(old)
            kids[i:i + 1] = new
            found = True
        if rb.children is None or old in rb.children:   # (a group always says its children)
            _set(live, mirror, gid, replace(rb, children=tuple(kids)))
    return found


def _kids(objects: Mapping[ObjectId, ReadBack], gid: ObjectId) -> list[ObjectId]:
    """A group's children bottom to top: the ones its list names, then any that joined it since."""
    listed = [c for c in objects[gid].children or () if (x := objects.get(c)) is not None and x.parent_group == gid]
    return list(dict.fromkeys(listed + [c for c, x in objects.items() if x.parent_group == gid]))


def _drop_lonely_groups(live: LiveSlide) -> None:
    """Slides drops a group left with one child (the child takes its place), and a read-back lists
    what is really there, in paint order: `order` the slide's own page elements, a group's
    `children` its own (`loss_oracle.paint_order` reads both)."""
    changed = True
    while changed:
        changed = False
        for oid in list(live.objects):
            rb = live.objects[oid]
            if rb.kind != "elementGroup":
                continue
            kids = _kids(live.objects, oid)
            if len(kids) <= 1:
                for c in kids:
                    live.objects[c] = replace(live.objects[c], parent_group=rb.parent_group)
                _take_place(live, oid, kids, None)
                live.objects.pop(oid)
                changed = True
    # A group says who its children are, and a unit rewritten under it has new ones: leaving the
    # list naming the objects the recreation deleted makes a read-back no API could hand back, and
    # `merge._descendants` - which sync itself asks what a group carries - then answers with the
    # dead. Everything else here reads `parent_group`, which is why it went unnoticed until
    # `fuzz_sync._movable` asked what one transform on a group would move.
    objects = live.objects
    for oid in list(objects):
        rb = objects[oid]
        if rb.kind == "elementGroup":
            objects[oid] = replace(rb, children=tuple(_kids(objects, oid)))
    top = [o for o in live.order if o in objects and not objects[o].parent_group]
    live.order = list(dict.fromkeys(top + [o for o, x in objects.items() if not x.parent_group]))
