"""Fuzz sync: random deck edits against random source changes, judged by tools/loss_oracle.py.

Two modes, the same oracle:

  offline   a made-up deck (tools/fuzz_world.py): random source changes -> `ours`, random deck
            edits -> `theirs`, `merge.plan_merge_of` plans, `fuzz_world.apply_plan` writes the plan
            the way docs/sync.md says a correct sync writes it, and the oracle judges the result.
            Hundreds of rounds in seconds, no Google, no LaTeX. This is what the default test suite
            runs (tests/test_sync_fuzz.py).
  live      a real converted deck: random edits from tools/deck_edits.py, a random source variant
            (tests/decks/sync/build.py), the real `beamer2slides sync`, then the oracle plus
            tools/sync_check.py's integrity checks.

Both modes chain (`--chain N`: edits -> sync -> edits -> sync ...). Offline, each sync starts from
the base the previous one wrote (`fuzz_world.rebase`), so a sync undoing what the last one merged,
or a base that forgot the person's version, shows up as a finding in the next step. Every offline
round also checks that the sync *settles*: with that base, syncing the same source again must write
nothing (`_settled`), or the next sync would rewrite units nobody asked it to touch.

A failing round writes everything needed to reproduce it into its folder (seed, the edits, the
variant, the deck url, both read-backs, the base, the report) and is then *shrunk*: the same round
is replayed with one edit dropped at a time until the smallest failing combination is left.

An offline step is a `Step` record and a round a `Chain` of them; a live round is a `LiveRecord` of
`LiveStep`s, written to round.json by `record_json` in the order that file has always had
(tools/layout_oracle.py and tools/fuzz_reach.py read the archives back).

  python tools/fuzz_sync.py offline --rounds 300 [--seed 0] [--no-shrink]
  python tools/fuzz_sync.py offline --replay 17            # one seed, verbose
  python tools/fuzz_sync.py live --rounds 20 [--parallel 3] [--chain 2] [--keep-decks]
  python tools/fuzz_sync.py live --replay 5                # the recorded edits of that seed
"""

import argparse
import copy
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import IO, Literal, TypeVar

from beamer2slides import adopt_sync, merge, snapshot, sync_model
from beamer2slides.google_types import SlidesRequest, SlidesTableCellLocation
from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_int
from beamer2slides.paths import CHECKOUT as ROOT  # live rounds build tests/decks/sync from the checkout
from beamer2slides.sync import EMU_PER_PT, Sync
from beamer2slides.sync_model import Base, DeckRead, ElementEntry, ImageRead, ObjectId, ReadBack, SlideEntry
from beamer2slides.typing_compat import assert_never

from . import deck_edits
from . import fuzz_world as W
from . import loss_oracle
from . import sync_check as sc
from .loss_oracle import Finding

MAX_EDITS = 8
MIN_EDITS = 2

T = TypeVar("T")


# ---------------------------------------------------------------- source changes (offline)

@dataclass(frozen=True, kw_only=True)
class SourceContext:
    """What a source change may need besides the document: `out`, the round's folder (a redrawn
    figure is written there), and `touched`, the ids of the elements whose text the person has just
    changed in the deck (`src_collide`)."""
    out: Path
    touched: tuple[str, ...]


SourceOp = Callable[[random.Random, JsonObject, SourceContext], "str | None"]


def _bodies(doc: JsonObject) -> list[JsonObject]:
    """Every body text of the document."""
    return [e for s in W.slides(doc) for e in W.elements(s) if e["kind"] == "text" and e.get("role") == "body"]


def _pick(rng: random.Random, items: Sequence[T]) -> T | None:
    return rng.choice(items) if items else None


def _id(el: Mapping[str, Json]) -> str:
    return W.text(el["id"], "element.id")


def _paragraphs(el: Mapping[str, Json]) -> list[Json]:
    """An element's paragraphs, the list itself (changing it changes the document)."""
    return W.arr(el["paragraphs"], "element.paragraphs")


def _grow(el: JsonObject, dy: float) -> None:
    """Its box made taller (or shorter) at the bottom: a paragraph more or less."""
    bbox = W.arr(el["bbox"], "element.bbox")
    bbox[3] = W.num(bbox[3], "element.bbox") + dy


def _shift(e: JsonObject, dx: float, dy: float) -> None:
    b = W.nums(e["bbox"], "element.bbox")
    e["bbox"] = W.jnums([b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy])


def src_reword(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    el = _pick(rng, _bodies(doc))
    if el is None:
        return None
    p = W.obj(rng.choice(_paragraphs(el)), "element.paragraph")
    r = W.objs(p["runs"], "paragraph.runs")[0]
    words = W.text(r["text"], "run.text").split()
    if not words:
        return None
    i = rng.randrange(len(words))
    words[i] = rng.choice(W.WORDS) + "-ours"
    r["text"] = " ".join(words)
    return f"reword {el['id']}"


def src_add_paragraph(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    el = _pick(rng, _bodies(doc))
    if el is None:
        return None
    line = "added by the source " + rng.choice(W.WORDS)
    paragraphs = _paragraphs(el)
    p = copy.deepcopy(W.obj(paragraphs[-1], "element.paragraph"))
    p["runs"] = W.jlist([W.run(line)])
    paragraphs.append(p)
    _grow(el, 12)
    return f"add paragraph to {el['id']}"


def src_remove_paragraph(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    el = _pick(rng, [e for e in _bodies(doc) if len(_paragraphs(e)) > 1])
    if el is None:
        return None
    paragraphs = _paragraphs(el)
    paragraphs.pop(rng.randrange(len(paragraphs)))
    _grow(el, -12)
    return f"remove paragraph from {el['id']}"


def src_move_element(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    """A move takes what is anchored to the element: an inline formula picture is placed by the line
    it sits in, so a source that moves the text moves the picture with it. Moving the text alone
    meant `merge.unit_shift` could never find one step for the unit, and `move` - a write path of its
    own (`sync.Sync.move_requests`) - came up twice in 800 offline steps. One move in four is still
    of a single anchored member, which is the re-placed formula `unit_shift` exists to refuse."""
    options = [(s, e) for s in W.slides(doc) for e in W.elements(s) if e.get("role") != "title"]
    if not options:
        return None
    s, el = rng.choice(options)
    dx, dy = rng.choice([-20, -8, 8, 20]), rng.choice([-16, -6, 6, 16])
    anchored = [e for e in W.elements(s) if e.get("anchor") == el["id"]]
    moving = [rng.choice(anchored)] if anchored and rng.random() < 0.25 else [el] + anchored
    for e in moving:
        _shift(e, dx, dy)
    return f"move {' '.join(_id(e) for e in moving)}"


def src_resize_element(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    el = _pick(rng, [e for s in W.slides(doc) for e in W.elements(s) if e["kind"] in ("image", "shape")])
    if el is None:
        return None
    bbox = W.arr(el["bbox"], "element.bbox")
    bbox[2] = W.num(bbox[2], "element.bbox") + rng.choice([-15, 15, 30])
    bbox[3] = W.num(bbox[3], "element.bbox") + rng.choice([-10, 10])
    return f"resize {el['id']}"


def src_restyle(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    el = _pick(rng, _bodies(doc))
    if el is None:
        return None
    for p in W.objs(el["paragraphs"], "element.paragraphs"):
        for r in W.objs(p["runs"], "paragraph.runs"):
            r["color"] = rng.choice(["#cc0000", "#0000cc", "#008000"])
    return f"recolour {el['id']}"


def src_add_element(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    s = rng.choice(W.slides(doc))
    els = W.arr(s["elements"], "slide.elements")
    eid = f"p{s['page']}t{len(els) + 9}"
    els.append(W.text_ir(eid, "a brand new source paragraph " + rng.choice(W.WORDS), (230, 160, 330, 180), "body"))
    return f"add element {eid}"


def src_delete_element(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    options = [(s, e) for s in W.slides(doc) for e in W.elements(s)
               if e.get("role") not in ("title",) and not e.get("anchor")
               and not any(x.get("anchor") == e["id"] for x in W.elements(s))]
    if not options:
        return None
    s, el = rng.choice(options)
    W.arr(s["elements"], "slide.elements").remove(el)
    return f"delete element {el['id']}"


def src_add_slide(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    slides = W.arr(doc["slides"], "slides")
    i = rng.randrange(len(slides) + 1)
    # a label and element ids nothing else uses: two frames with one \label is a broken source,
    # and two slides with one key would make the whole identity model meaningless. A label a
    # deleted slide took with it is not free either, though nothing living carries it: reissuing
    # it says "this new frame is that frame", which is exactly what a label means, so the sync
    # pairs them and is right to - while a campaign that keeps its own truth reads the pairing as
    # a frame on the wrong slide (fuzz_labels --shape adopt seed 7100523: one step deleted the
    # slide holding `new5` and added a frame that was handed `new5` again).
    labels = {W.opt_text(s.get("label"), "slide.label") for s in W.slides(doc)}
    ids = {_id(e) for s in W.slides(doc) for e in W.elements(s)}
    page = max(len(slides), as_int(doc.get("labelled", 0), "doc.labelled"))
    while f"new{page}" in labels or f"p{page}t0" in ids:
        page += 1
    doc["labelled"] = page + 1
    title = "New source frame " + rng.choice(W.WORDS)
    slide: JsonObject = {"page": page, "label": f"new{page}", "title": title, "notes": "", "bg": "#ffffff",
                         "elements": W.jlist([W.text_ir(f"p{page}t0", title, (20, 20, 200, 34), "title"),
                                              W.text_ir(f"p{page}t1", "what the new frame says", (25, 60, 200, 72),
                                                        "body")])}
    slides.insert(i, slide)
    _renumber(doc)
    return f"add slide {title!r}"


def src_delete_slide(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    slides = W.arr(doc["slides"], "slides")
    if len(slides) < 3:
        return None
    i = rng.randrange(1, len(slides))
    gone = W.obj(slides.pop(i), "slide")
    _renumber(doc)
    return f"delete slide {gone['title']!r}"


def src_move_slide(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    slides = W.arr(doc["slides"], "slides")
    if len(slides) < 3:
        return None
    i = rng.randrange(1, len(slides))
    j = rng.randrange(1, len(slides))
    if i == j:
        return None
    slides.insert(j, slides.pop(i))
    _renumber(doc)
    return f"move slide {i}->{j}"


def _titled(rng: random.Random, doc: JsonObject) -> tuple[JsonObject, JsonObject] | None:
    """A slide with a title element, and that element. An adopt-shaped deck has slides with none -
    that is the shape they come in - and there is no title there to edit."""
    options = [(s, t) for s in W.slides(doc) if (t := W.title_element(s)) is not None]
    return rng.choice(options) if options else None


def set_title(s: JsonObject, el: JsonObject, title: str) -> None:
    """A frame's title rewritten: the slide's own and its title element's one run (fuzz_labels
    revises titles the same way)."""
    s["title"] = title
    first: JsonObject = {**W.objs(el["paragraphs"], "title.paragraphs")[0], "runs": W.jlist([W.run(title)])}
    el["paragraphs"] = W.jlist([first])


def src_retitle(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    found = _titled(rng, doc)
    if found is None:
        return None
    s, el = found
    title = f"{rng.choice(W.WORDS).title()} retitled"
    set_title(s, el, title)
    return f"retitle {title!r}"


def src_amend_title(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    """A title edited rather than replaced ("Results" -> "Results v2"), which is what a source does
    when it revises a whole talk - and what tells a yes-or-no "same title?" nothing at all."""
    found = _titled(rng, doc)
    if found is None:
        return None
    s, el = found
    title = f"{W.title_of(s)} {rng.choice(('v2', 'again', 'revisited'))}"
    set_title(s, el, title)
    return f"amend title to {title!r}"


def src_notes(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    s = rng.choice(W.slides(doc))
    s["notes"] = "source notes " + " ".join(rng.choice(W.WORDS) for _ in range(3))
    return f"notes of {s['title']!r}"


def src_background(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    s = rng.choice(W.slides(doc))
    s["bg"] = rng.choice(["#eeeeee", "#fff2cc", "#e8f0fe"])
    return f"background of {s['title']!r}"


def src_repaint(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    options = [(s, e) for s in W.slides(doc) for e in W.elements(s) if e["kind"] == "image"]
    if not options:
        return None
    s, el = rng.choice(options)
    name = f"{Path(W.text(el['file'], 'image.file')).stem}-v2.bin"
    (ctx.out / name).write_bytes(bytes([(rng.randrange(251) + j) % 251 for j in range(64)]))
    el["file"] = name
    return f"redraw {el['id']}"


def _labelled(doc: JsonObject) -> list[JsonObject]:
    return [s for s in W.slides(doc) if s.get("label")]


def src_move_label(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    """A label pasted onto another frame: the invariant break `identity.label_moves` is about. When
    the other frame had one of its own this is a swap, which is the hardest shape of it."""
    if len(W.slides(doc)) < 2 or not _labelled(doc):
        return None
    src = rng.choice(_labelled(doc))
    dst = rng.choice([s for s in W.slides(doc) if s is not src])
    name = src["label"]
    src["label"], dst["label"] = dst.get("label"), name
    return f"move label {name!r} onto {dst['title']!r}"


def src_rename_label(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    if not _labelled(doc):
        return None
    s = rng.choice(_labelled(doc))
    was, s["label"] = s["label"], f"{s['label']}-renamed"
    return f"rename label {was!r}"


def src_drop_label(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    if not _labelled(doc):
        return None
    s = rng.choice(_labelled(doc))
    was, s["label"] = s["label"], None
    return f"drop label {was!r}"


def _rewrite_cell(rng: random.Random, el: JsonObject) -> tuple[int, int]:
    """One cell of a table element rewritten; which one."""
    rows = W.arr(el["cells"], "table.cells")
    r, c = rng.randrange(len(rows)), rng.randrange(len(W.arr(rows[0], "table.row")))
    W.arr(rows[r], "table.row")[c] = W.jlist([W.run(rng.choice(W.WORDS) + "-ours")])
    return r, c


def src_edit_cell(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    options = [e for s in W.slides(doc) for e in W.elements(s) if e["kind"] == "table"]
    if not options:
        return None
    el = rng.choice(options)
    r, c = _rewrite_cell(rng, el)
    return f"rewrite cell {r},{c} of {el['id']}"


def src_collide(rng: random.Random, doc: JsonObject, ctx: SourceContext) -> str | None:
    """Change exactly what the person has just changed in the deck. Two sides on the same text is
    what every rule in `merge.plan_unit` is about, and drawing both sides' targets at random makes
    it a rarity: 5 text overrides in 200 offline rounds before this, and never once a table, so the
    campaign was barely exercising the merge it exists to test. A real deck is not random either -
    the author revises the frame the reader was reading."""
    ids = set(ctx.touched)
    options = [(s, e) for s in W.slides(doc) for e in W.elements(s) if _id(e) in ids]
    if not options:
        return None
    s, el = rng.choice(options)
    if el["kind"] == "table":
        r, c = _rewrite_cell(rng, el)
        return f"also rewrite cell {r},{c} of {el['id']}"
    paragraphs = _paragraphs(el)
    how = rng.choice(("reword", "append", "drop", "move") if len(paragraphs) > 1 else ("reword", "append", "move"))
    if how == "move":
        # The source moved the very box the person had just edited, and changed nothing else about
        # it: the only shape that makes `merge.plan_unit` write a `move` (the source's place onto the
        # deck's own objects, `sync.Sync.move_requests`). Both sides have to meet on one unit for it,
        # so drawing the two targets apart made that write path come up 2 times in 800 offline steps
        # - and it is the path live seed 903 broke.
        dx, dy = rng.choice([-20, -8, 8, 20]), rng.choice([-16, -6, 6, 16])
        for e in [el] + [x for x in W.elements(s) if x.get("anchor") == el["id"]]:
            _shift(e, dx, dy)
    elif how == "drop":                   # (the shape live seeds 608/616 died on, from the other side)
        paragraphs.pop(rng.randrange(len(paragraphs)))
        _grow(el, -12)
    elif how == "append":
        p = copy.deepcopy(W.obj(paragraphs[-1], "element.paragraph"))
        p["runs"] = W.jlist([W.run("and the source adds " + rng.choice(W.WORDS))])
        paragraphs.append(p)
        _grow(el, 12)
    else:
        p = W.obj(rng.choice(paragraphs), "element.paragraph")
        first = W.objs(p["runs"], "paragraph.runs")[0]
        words = W.text(first["text"], "run.text").split()
        if not words:
            return None
        words[rng.randrange(len(words))] = rng.choice(W.WORDS) + "-ours"
        first["text"] = " ".join(words)
    return f"also {how} {el['id']}"


def _renumber(doc: JsonObject) -> None:
    for i, s in enumerate(W.slides(doc)):
        s["page"] = i


SOURCE_OPS: dict[str, SourceOp] = {
    "reword": src_reword, "add_paragraph": src_add_paragraph, "remove_paragraph": src_remove_paragraph,
    "move_element": src_move_element, "resize_element": src_resize_element, "restyle": src_restyle,
    "add_element": src_add_element, "delete_element": src_delete_element, "add_slide": src_add_slide,
    "delete_slide": src_delete_slide, "move_slide": src_move_slide, "retitle": src_retitle,
    "amend_title": src_amend_title, "notes": src_notes, "background": src_background, "repaint": src_repaint,
    "move_label": src_move_label, "rename_label": src_rename_label, "drop_label": src_drop_label,
    "edit_cell": src_edit_cell, "collide": src_collide}


# ---------------------------------------------------------------- deck edits (offline)

DeckOp = Callable[[random.Random, Base, W.LiveDeck], "str | None"]
Converted = tuple[SlideEntry, W.LiveSlide, ElementEntry, ObjectId, ReadBack]
"""(base slide, live slide, element, object id, read-back) of a converter object still in the deck."""


def _converter_objects(base: Base, live: W.LiveDeck) -> list[Converted]:
    by_id = {s.object_id: s for s in live.slides}
    out: list[Converted] = []
    for b in base.slides:
        s = None if b.object_id is None else by_id.get(b.object_id)
        if s is None:
            continue
        for el in b.elements:
            oid = el.main
            rb = None if oid is None else s.objects.get(oid)
            if oid is not None and rb is not None:
                out.append((b, s, el, oid, rb))
    return out


def _converter_texts(base: Base, live: W.LiveDeck) -> list[Converted]:
    """(base slide, live slide, element, object id, read-back) of every converter text object."""
    return [x for x in _converter_objects(base, live) if x[4].text is not None and x[2].kind in ("text", "table")]


def _retext(rb: ReadBack, text: str) -> ReadBack:
    """A read-back's text changed the way a person changes it in Slides: the run styling moves with
    the words around the edit. Leaving `run_spans` at their old indices makes a read-back no API
    could hand back - the spans then cover letters in the middle of words nobody styled - and the
    campaign duly accused `merge.styling_lost` of losing styling that was never where the spans
    said it was (offline seed 2194: bold on "picture", the deck rewording an earlier word, and the
    stale span landing inside "colleague-theirs")."""
    was = rb.text or ""
    if not rb.run_spans:
        return replace(rb, text=text)
    at: dict[int, int] = {}                  # old index -> new index, at the edit boundaries
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, was, text, autojunk=False).get_opcodes():
        at[i1], at[i2] = j1, j2

    def move(i: int) -> int:
        k = max((x for x in at if x <= i), default=0)
        return max(0, min(at.get(k, 0) + (i - k), len(text)))

    return replace(rb, text=text, run_spans=tuple((move(s), move(e), style) for s, e, style in rb.run_spans
                                                  if move(e) > move(s)))


def _paragraph_lines(text: str | None) -> list[str]:
    return [line for line in (text or "").split("\n") if line.strip()]


def deck_reword(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_texts(base, live) if x[2].kind == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    lines = _paragraph_lines(rb.text)
    if not lines:
        return None
    line = rng.choice(lines)
    words = line.split()
    i = rng.randrange(len(words))
    words[i] = rng.choice(W.WORDS) + "-theirs"
    s.objects[oid] = _retext(rb, (rb.text or "").replace(line, " ".join(words), 1))
    return f"reword {oid}"


def deck_append(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_texts(base, live) if x[2].kind == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    s.objects[oid] = _retext(rb, (rb.text or "").rstrip("\n") + " typed by a person.\n")
    return f"append to {oid}"


def deck_delete_paragraph(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_texts(base, live) if x[2].kind == "text" and len(_paragraph_lines(x[4].text)) > 1]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    lines = _paragraph_lines(rb.text)
    lines.pop(rng.randrange(len(lines)))
    s.objects[oid] = _retext(rb, "\n".join(lines) + "\n")
    return f"delete a paragraph of {oid}"


def deck_edit_cell(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_texts(base, live) if x[2].kind == "table"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rows = [r.split("\t") for r in (rb.text or "").split("\n")]
    rows[rng.randrange(len(rows))][rng.randrange(len(rows[0]))] = rng.choice(W.WORDS) + "-theirs"
    s.objects[oid] = _retext(rb, "\n".join("\t".join(r) for r in rows))
    return f"edit a cell of {oid}"


def deck_move(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = _converter_objects(base, live)
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    top = rb.parent_group or oid
    dx, dy = rng.choice([-30, -10, 10, 30]), rng.choice([-20, 20, 40])
    for o, x in list(s.objects.items()):
        if o == top or x.parent_group == top:
            s.objects[o] = W.shifted(x, dx, dy)
    return f"move {top}"


def deck_restyle(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_texts(base, live) if x[2].kind == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    styles: tuple[JsonObject, ...] = ({**rb.text_styles[0], "bold": True},) if rb.text_styles else ({"bold": True},)
    s.objects[oid] = replace(rb, text_styles=styles, text_style_hash="s-bold")
    return f"bold {oid}"


def deck_bold_word(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    """One word of a box bolded, which is what the `ranges` re-application is for: the style goes
    back onto that same word - if the source has not replaced it in the meantime."""
    options = [x for x in _converter_texts(base, live)
               if x[2].kind == "text" and len(re.findall(r"\w+", x[4].text or "")) > 2]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    text = rb.text or ""
    start, end = rng.choice([(m.start(), m.end()) for m in re.finditer(r"\w+", text)])
    plain: JsonObject = rb.text_styles[0] if rb.text_styles else {"fontFamily": "Lato", "fontSize": 18.0}
    bold: JsonObject = {**plain, "bold": True}
    s.objects[oid] = replace(rb, text_styles=(plain, bold), text_style_hash="s-word-bold",
                             run_spans=((0, start, plain), (start, end, bold), (end, len(text.rstrip("\n")), plain)))
    return f"bold {text[start:end]!r} in {oid}"


def deck_delete_object(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_objects(base, live) if x[2].role != "title"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    s.objects.pop(oid)
    return f"delete {oid}"


def deck_replace_image(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [x for x in _converter_objects(base, live) if x[4].kind == "image"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    # (the hash is drawn before the signature, as the read-back's keys come)
    s.objects[oid] = replace(rb, image=ImageRead(content_hash=f"user{rng.randrange(10 ** 6):06}", source_url=None,
                                                 signature=W.picture_signature(bytes([rng.randrange(200) + 40])),
                                                 unchecked=False))
    return f"replace the picture of {oid}"


def _taken(live: W.LiveDeck) -> set[str]:
    """Every id the deck already carries - slides, their notes pages and their objects."""
    ids: set[str] = set()
    for s in live.slides:
        ids.add(s.object_id)
        if s.notes_id is not None:
            ids.add(s.notes_id)
        ids.update(s.objects)
    return ids


def _fresh(rng: random.Random, live: W.LiveDeck, prefix: str, width: int) -> ObjectId:
    """An id nothing in the deck answers to. Slides never hands out an objectId twice, and a fuzzer
    that does is not fuzzing the sync: two slides of one id read back as one slide, so the oracle
    saw a picture the person had added disappear and the report list one created slide where two
    appeared (converted seed 9200614, three findings, every one of them the harness's own hand). Six
    digits drawn afresh each time is not rare enough for a long campaign - the birthday rule makes a
    collision likely well before a thousand rounds - and the ids a slide's own derive from it, so a
    slide id nobody else has makes its objects and its notes page unique too."""
    taken = _taken(live)
    drawn = oid = f"{prefix}{rng.randrange(10 ** width):0{width}}"
    n = 0
    while oid in taken:      # (a tail rather than another draw: one op takes one number, always)
        n += 1
        oid = f"{drawn}x{n}"
    return ObjectId(oid)


def _text_box(box: Sequence[float], text: str) -> ReadBack:
    """A text box a person drew: no title, no fill, nothing inside a group."""
    return W.readback("shape", box, text=text, image=None, parent=None, title=None, z=0, table=None, fill=None)


def deck_add_text_box(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    s = rng.choice(live.slides)
    oid = _fresh(rng, live, "user", 9)
    s.objects[oid] = _text_box([300, 300, 460, 330], f"a note nobody may lose {oid}\n")
    s.order.append(oid)
    return f"add a text box {oid}"


def deck_add_image(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    s = rng.choice(live.slides)
    oid = _fresh(rng, live, "user", 9)
    picture = ImageRead(content_hash=f"u{rng.randrange(10 ** 6)}", source_url=None,
                        signature=W.picture_signature(bytes([rng.randrange(200)])), unchecked=False)
    s.objects[oid] = W.readback("image", [400, 200, 500, 260], text=None, image=picture, parent=None, title=None,
                                z=0, table=None, fill=None)
    s.order.append(oid)
    return f"add a picture {oid}"


def deck_add_slide(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    i = rng.randrange(len(live.slides) + 1)
    sid = _fresh(rng, live, "user_s", 6)
    oid = ObjectId(f"{sid}_t")
    live.slides.insert(i, W.LiveSlide(object_id=sid, layout_object_id="L", background={"color": "#ffffff"},
                                      notes="a slide the person added", notes_id=f"{sid}_n", order=[oid],
                                      objects={oid: _text_box([40, 40, 300, 80], "my own slide\n")}))
    return f"add a slide {sid}"


def deck_duplicate_slide(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    s = rng.choice(live.slides)
    sid = _fresh(rng, live, "user_s", 6)
    objects = {ObjectId(f"{sid}_{k}"): replace(v, parent_group=None) for k, v in enumerate(s.objects.values())}
    # (a copy gets its own notes page, as it does in Slides)
    dup = W.LiveSlide(object_id=sid, layout_object_id=s.layout_object_id, background=copy.deepcopy(s.background),
                      notes=s.notes, notes_id=f"{sid}_n", order=list(objects), objects=objects)
    live.slides.insert(live.slides.index(s) + 1, dup)
    return f"duplicate slide {s.object_id}"


def deck_delete_slide(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    if len(live.slides) < 2:
        return None
    s = rng.choice(live.slides)
    live.slides.remove(s)
    return f"delete slide {s.object_id}"


def deck_move_slide(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    if len(live.slides) < 3:
        return None
    i, j = rng.randrange(len(live.slides)), rng.randrange(len(live.slides))
    if i == j:
        return None
    live.slides.insert(j, live.slides.pop(i))
    return f"move slide {i}->{j}"


def deck_notes(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    s = rng.choice(live.slides)
    s.notes = "the person's notes " + " ".join(rng.choice(W.WORDS) for _ in range(3))
    return f"notes of {s.object_id}"


def deck_background(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    s = rng.choice(live.slides)
    s.background = {"color": rng.choice(["#ffe0e0", "#e0ffe0", "#e0e0ff"])}
    return f"background of {s.object_id}"


def _ungrouped(s: W.LiveSlide) -> list[ObjectId]:
    return [o for o, rb in s.objects.items() if not rb.parent_group and rb.kind != "elementGroup"]


def deck_group(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [s for s in live.slides if len(_ungrouped(s)) >= 2]
    if not options:
        return None
    s = rng.choice(options)
    tops = _ungrouped(s)
    picked = rng.sample(tops, 2)
    gid = _fresh(rng, live, "user_g", 6)
    boxes = [s.objects[o].box for o in picked]
    # Slides keeps the children's z-order, and the group stands where the topmost of them stood
    order = s.order
    picked.sort(key=lambda o: order.index(o) if o in order else len(order))
    rb = W.group_readback([min(b[0] for b in boxes), min(b[1] for b in boxes),
                           max(b[2] for b in boxes), max(b[3] for b in boxes)], 0, picked)
    # ... and the group takes the topmost child's place *before* it is a group on this slide:
    # `_take_place` puts the new id wherever the old one stands, its own children among them, and
    # a group that lists itself as a child is a deck the API could not describe (its `children` was
    # `picked` itself, so the op even said so out loud: `group [a, g] as g`).
    W._take_place(s, picked[-1], [gid], None)
    s.objects[gid] = rb
    for o in picked:
        s.objects[o] = replace(s.objects[o], parent_group=gid)
    s.order = [o for o in s.order if o not in picked]
    return f"group {picked} as {gid}"


def deck_ungroup(rng: random.Random, base: Base, live: W.LiveDeck) -> str | None:
    options = [(s, o) for s in live.slides for o, rb in s.objects.items() if rb.kind == "elementGroup"]
    if not options:
        return None
    s, gid = rng.choice(options)
    parent = s.objects[gid].parent_group
    kids = W._kids(s.objects, gid)
    for c in kids:
        s.objects[c] = replace(s.objects[c], parent_group=parent)   # (a group inside a group hands them to its parent)
    W._take_place(s, gid, kids, None)                                # ... where the group stood, in their order
    s.objects.pop(gid)
    return f"ungroup {gid}"


DECK_OPS: dict[str, DeckOp] = {
    "reword": deck_reword, "append": deck_append, "delete_paragraph": deck_delete_paragraph,
    "edit_cell": deck_edit_cell, "move": deck_move, "restyle": deck_restyle, "bold_word": deck_bold_word,
    "delete_object": deck_delete_object, "replace_image": deck_replace_image, "add_text_box": deck_add_text_box,
    "add_image": deck_add_image, "add_slide": deck_add_slide, "duplicate_slide": deck_duplicate_slide,
    "delete_slide": deck_delete_slide, "move_slide": deck_move_slide, "notes": deck_notes,
    "background": deck_background, "group": deck_group, "ungroup": deck_ungroup}


def _deck_edited_ir_ids(base: Base, live: W.LiveDeck) -> list[str]:
    """The ids of the source elements whose text the person has just changed in the deck."""
    out: list[str] = []
    for b, s, el, oid, rb in _converter_texts(base, live):
        was = el.readback.get(oid)
        if (rb.text or "") != ((None if was is None else was.text) or ""):
            out.append(el.id)
    return out


# ---------------------------------------------------------------- offline rounds

Held = Literal["element", "slide"]
"""What a sync into a person's deck left alone: an element the base ties to no object, or a slide no
frame accounts for."""


@dataclass(frozen=True, kw_only=True)
class StepState:
    """What a step leaves behind: the base it planned from, the deck before and after, the report,
    the new conversion, the base the next sync plans from (None: the step did not rebase, or the
    sync refused), the source document and the round's folder."""
    base: JsonObject
    before: W.LiveDeck
    after: W.LiveDeck
    report: JsonObject
    ours: W.WorldOurs
    next_base: JsonObject | None
    doc: JsonObject
    work: Path


@dataclass(frozen=True, kw_only=True)
class Step:
    """One edit + sync of an offline round: the op names drawn, what each did (`source`, `deck`: one
    line per op), the oracle's findings, the reasons a sync into an adopted deck refused, what it
    held, and the refusal a person would read."""
    seed: int
    step: int
    source_ops: tuple[str, ...]
    deck_ops: tuple[str, ...]
    source: tuple[str, ...]
    deck: tuple[str, ...]
    findings: tuple[Finding, ...]
    refused: tuple[str, ...]
    held: tuple[Held, ...]
    message: str | None
    state: StepState

    @property
    def failures(self) -> list[Finding]:
        return loss_oracle.failing(self.findings)


@dataclass(frozen=True, kw_only=True)
class Ops:
    """The op names of one step (None: drawn from the seed)."""
    deck: tuple[str, ...] | None
    source: tuple[str, ...] | None


@dataclass(frozen=True, kw_only=True)
class Chain:
    """An offline round: its steps, and the ops each step ran (what a replay or a shrink passes)."""
    seed: int
    chain: int
    shape: W.Shape
    first_sync: bool
    steps: tuple[Step, ...]
    ops: tuple[Ops, ...]

    @property
    def refused(self) -> list[str]:
        return [r for s in self.steps for r in s.refused]

    @property
    def held(self) -> list[Held]:
        return [h for s in self.steps for h in s.held]

    @property
    def failures(self) -> list[Finding]:
        return [f for s in self.steps for f in s.failures]


def _held(plan: merge.MergePlan) -> list[Held]:
    """What this sync left alone because it is a person's deck and nothing here may write there
    (`merge.plan_unit`'s unpaired unit, `plan_merge`'s slide no frame accounts for). Counted for
    the same reason as `refused`: it is how one sees the campaign's own reach."""
    out: list[Held] = []
    for p in plan.slides:
        if isinstance(p, merge.UpdateSlide):
            for u in p.units:
                d = u.decision
                if isinstance(d, merge.KeepUnit) and d.blind is not None and d.blind[1]:
                    out.append("element")
    for k in plan.report.kept:
        if k.reason == ("the deck's own",):
            out.append("slide")
    return out


def _sync_step(seed: int, step: int, doc: JsonObject, base: JsonObject, live: W.LiveDeck, tmp: Path,
               deck_ops: Sequence[str], source_ops: Sequence[str], rebase: bool) -> Step:
    """One edit + sync: the person edits the deck, the author changes the source, merge plans, the
    reference applier writes the plan and the oracle judges what the person is left with."""
    edited = sync_model.base(base)      # (the base as the person's edits find it: `build_ours` rewrites it)
    applied_deck: list[str] = []
    for k, name in enumerate(deck_ops):
        if not live.slides:
            applied_deck.append(f"{name}: (no slides left)")  # a chain that deleted the whole deck
            continue
        done = DECK_OPS[name](random.Random(seed * 1009 + 101 * step + k), edited, live)
        applied_deck.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    for s in live.slides:
        W._drop_lonely_groups(s)  # (Slides drops a group an edit left with one child)
    seen: set[str] = set()        # ... and never hands out one objectId twice (`_fresh`)
    for s in live.slides:
        for oid in [s.object_id, s.notes_id, *s.objects]:
            if oid is None:
                continue
            if oid in seen:
                raise AssertionError(f"the deck edits gave {oid} to two things at once: "
                                     f"a deck no Slides could hand back, and whatever the oracle "
                                     f"says about it is about the fuzzer, not the sync")
            seen.add(oid)
        for gid, rb in s.objects.items():
            if rb.kind == "elementGroup" and gid in (rb.children or ()):
                raise AssertionError(f"the deck edits made {gid} a child of itself, which is the "
                                     f"same kind of deck and the same kind of nonsense")
    doc2 = copy.deepcopy(doc)
    applied_src: list[str] = []
    # what the person just edited, for `collide`; the folder, for `repaint`
    ctx = SourceContext(out=tmp, touched=tuple(_deck_edited_ir_ids(edited, live)))
    for k, name in enumerate(source_ops):
        done = SOURCE_OPS[name](random.Random(seed * 2003 + 101 * step + k), doc2, ctx)
        applied_src.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    ours = W.build_ours(doc2, base, tmp)
    typed_base = sync_model.base(base)
    plan = merge.plan_merge_of(typed_base, ours.typed, W.deck_read_of(live), None, False, merge.Resolutions(()))
    mplan = merge.merge_plan_json(plan)
    report = W.obj(mplan["report"], "plan.report")
    held = tuple(_held(plan))
    # `--backup auto` keeps a .pptx of the deck before sync's first write, so the campaign asks what
    # the other refusals do; the no-way-back one has its own test (tests/test_adopt_sync.py).
    refused = adopt_sync.problems(base, mplan, W.live_json(live), {"drive": {"presentationId": "way-back"}}, "auto")
    if refused:
        # A sync into an adopted deck this one may not write (adopt_sync.problems). Nothing is sent,
        # so the deck is exactly as the person left it - that is the whole answer, and the oracle
        # has nothing to judge. The base does not move either: the next step plans from this one.
        return Step(seed=seed, step=step, source_ops=tuple(source_ops), deck_ops=tuple(deck_ops),
                    source=tuple(applied_src), deck=tuple(applied_deck), findings=(),
                    refused=tuple(W.text(p["reason"], "problem.reason") for p in refused), held=held,
                    message=adopt_sync.refusal_message(W.text(base["presentationId"], "base.presentationId"), tmp,
                                                       "new.pdf", refused),
                    state=StepState(base=base, before=live, after=live, report=report, ours=ours, next_base=None,
                                    doc=doc2, work=tmp))
    tok = f"{step}zz"  # a token per run, like sync's
    applied = W.apply_plan(typed_base, ours.json, live, plan, tok)
    after = applied.deck
    findings = (loss_oracle.check_of(typed_base, W.deck_read_of(live), W.deck_read_of(after),
                                     loss_oracle.report_of(report), ours.typed)
                + _writable(ours, plan) + _movable(base, live, plan) + _stacked(base, typed_base, live, after, ours, plan, tok)
                + _doubled(base, live, after, plan))
    next_base = W.rebase(base, ours.json, applied, plan, tok) if rebase else None
    findings += _settled(doc2, next_base, after, tmp, bool(loss_oracle.uncertain_slides_of(typed_base, ours.typed)))
    return Step(seed=seed, step=step, source_ops=tuple(source_ops), deck_ops=tuple(deck_ops),
                source=tuple(applied_src), deck=tuple(applied_deck), findings=tuple(findings), refused=(),
                held=held, message=None,
                state=StepState(base=base, before=live, after=after, report=report, ours=ours, next_base=next_base,
                                doc=doc2, work=tmp))


def _strs(v: Json, where: str) -> list[str]:
    return [W.text(x, where) for x in W.arr(v, where)]


def _index(v: int | None, where: str) -> int:
    if v is None:
        raise JsonShapeError(f"{where}: missing")
    return v


def _apply_text_requests(current: str, reqs: Sequence[SlidesRequest]) -> str:
    """Slides applying them, refusals included: the newline a shape's text ends on is the API's own
    and is left out of the length it will accept, so a `deleteText` reaching the end is thrown out
    and the batch with it (`merge.text_edit_requests`)."""
    units = list(current)
    for r in reqs:
        length = len(units) - 1 if units and units[-1] == "\n" else len(units)
        delete, insert = r.get("deleteText"), r.get("insertText")
        if delete is not None:
            rng = delete.get("textRange")
            if rng is None:
                raise JsonShapeError("deleteText.textRange: missing")
            start = _index(rng.get("startIndex"), "textRange.startIndex")
            end = _index(rng.get("endIndex"), "textRange.endIndex")
            if end > length:
                raise ValueError(f"the end index ({end}) should not be greater than "
                                 f"the existing text length ({length})")
            del units[start:end]
        elif insert is not None:
            i = _index(insert.get("insertionIndex"), "insertText.insertionIndex")
            if i > length:
                raise ValueError(f"the insertion index ({i}) is past the text ({length})")
            units[i:i] = list(insert["text"])
        else:
            raise JsonShapeError(f"not a text edit: {sorted(r)}")
    return "".join(units)


def _unwritable(skey: str, ekey: str, detail: str) -> Finding:
    return Finding(kind="unwritable_text", severity="report", slide=skey, element=ekey, object=None, detail=detail)


def _writable_cells(skey: str, ekey: str, ir: JsonObject, current: str, ov: merge.TableOverride) -> list[Finding]:
    """The same for a table: sync writes a cell at a time, each cell's text ending on the newline
    Slides keeps (`sync.Sync.override_requests`), so every cell is its own little text with the
    same arithmetic - and a cell emptied to nothing is exactly the shape that kills a batch."""
    if ir.get("kind") != "table" or not ir.get("cells"):   # the source made it something else
        return []
    rows = W.arr(ir["cells"], "table.cells")
    dims = [len(rows), len(W.arr(rows[0], "table.row"))]
    # (the plan's JSON said [rows, columns], and `table_merge` compares the two as it is given them)
    theirs_dims = None if ov.dims is None else [ov.dims[0], ov.dims[1]]
    cells = merge.table_merge(ov.base, current, ov.theirs, dims, theirs_dims)
    grid = merge.table_grid(current, dims)
    if cells is None or grid is None:
        return []
    out: list[Finding] = []
    for r, (crow, mrow) in enumerate(zip(grid, cells[0])):
        for c, (now_cell, want) in enumerate(zip(crow, mrow)):
            if want == now_cell:
                continue
            loc: SlidesTableCellLocation = {"rowIndex": r, "columnIndex": c}
            try:
                written = _apply_text_requests(
                    now_cell + "\n", merge.text_edit_requests("oid", now_cell + "\n", want + "\n", loc))
            except ValueError as e:
                out.append(_unwritable(skey, ekey, f"Slides refuses the edit of cell {r},{c}: {e}"))
                continue
            if written != want + "\n":
                out.append(_unwritable(skey, ekey, f"the requests write {written!r} into cell {r},{c}, "
                                                   f"not the merged {want + chr(10)!r}"))
    return out


def _writable(ours: W.WorldOurs, plan: merge.MergePlan) -> list[Finding]:
    """Every text the deck keeps must be writable as requests. The plan says *what* the merged text
    is; `sync.Sync.override_requests` turns it into deleteText / insertText against the element the
    converter has just recreated, and that arithmetic has to obey rules the plan knows nothing
    about. The reference applier merges the text itself and never looks at a request, so without
    this the campaign proves a sync that cannot be written (live seeds 608 and 616: the deck had
    deleted the last paragraph of a text box the source rewrote, the batch was refused and the sync
    died with it)."""
    out: list[Finding] = []
    for p in plan.slides:
        if not isinstance(p, merge.UpdateSlide):
            continue
        els = {e.key: e for e in ours.typed.slides[p.ours].elements}
        for u in p.units:
            d = u.decision
            if not isinstance(d, merge.Recreate) or d.overrides.text is None or u.key not in els:
                continue
            ov = d.overrides.text
            ir = els[u.key].ir
            current = None if ir is None else W.element_text(ir)
            if ir is None or current is None:
                continue
            if isinstance(ov, merge.TableOverride):
                out += _writable_cells(p.key, u.key, ir, current, ov)
                continue
            current = current if current.endswith("\n") else current + "\n"   # as Slides reads it back
            merged, _, safe = merge.text_merge_of(ov.base, current, ov.theirs, ov.take)
            if not safe:
                continue
            merged = merged if merged.endswith("\n") else merged + "\n"
            try:
                written = _apply_text_requests(current, merge.text_edit_requests("oid", current, merged, None))
            except ValueError as e:
                out.append(_unwritable(p.key, u.key, f"Slides refuses the edit: {e}"))
                continue
            if written != merged:
                out.append(_unwritable(p.key, u.key, f"the requests write {written!r}, not the merged {merged!r}"))
    return out


def _doubled(base: JsonObject, live: W.LiveDeck, after: W.LiveDeck, plan: merge.MergePlan) -> list[Finding]:
    """An adopted deck's element the base could not pair with an object of the deck must not be
    written (`adopt_sync.problems`, reason "unpaired").

    The loss oracle cannot see this one, and that is the point of having it. Sync deletes a
    recreated unit's *old* objects, which it finds through the base - and an unpaired element names
    none, so nothing is deleted and nothing is lost. What happens instead is that the person's own
    box stays where it was and a second one, saying what the source now says, is created on top of
    it. No loss, a wrecked slide: the same shape of problem as a label that moved.

    It is asked of the slide, not of the plan. The base's rule is `merge.blind_members`, which
    `plan_unit` and `adopt_sync.problems` both read; stating it a third time here would only fail
    the campaign whenever somebody changed one of the two, which is a lint and not a measurement.
    What is counted instead is the outcome: the person's box was still standing when this sync
    finished, and beside it stands an object this sync created *for that very element* (the alt
    text `sync.tag_requests` writes says which one it is). Two boxes where the deck had one.

    This world used to take an unpaired element's object off the slide, so there was nothing to
    count and the check had to be the rule restated; `fuzz_world.build_adopt_base` leaves it
    standing now, the way `adopt.left_alone` records it in a real base, and `left_object` says
    which box it is. A picture drawn out of a box another member of the unit is tied to has no
    box of its own (it was never an object of the person's), so a unit written over one of those
    leaves nothing behind - and this check stays silent about it without being told to."""
    if base.get("origin") != adopt_sync.ORIGIN:
        return []
    out: list[Finding] = []
    before = {o for s in live.slides for o in s.objects}
    for p in plan.slides:
        if not isinstance(p, merge.UpdateSlide | merge.HoldSlide):
            continue
        b = W.slides(base)[p.base]
        made = next((s for s in after.slides if s.object_id == p.object_id), None)
        if made is None:
            continue
        standing = {W.text(el["key"], "element.key"): W.text(el["left_object"], "element.left_object")
                    for el in W.elements(b) if el.get("left_object") in made.objects}
        # what this sync created, each saying which element it is (`snapshot.tag`); the slide key
        # in front of it is the source's, which a renamed label makes not the base's
        written = [rb.title or "" for oid, rb in made.objects.items() if oid not in before]
        for ek, oid in sorted(standing.items()):
            if not any(t.startswith(snapshot.TAG_PREFIX) and t.endswith(f"/{ek}") for t in written):
                continue
            # `report`, not `loss`: the sync reported the source change as applied, and what the
            # slide shows is both versions of it.
            out.append(Finding(
                kind="adopt_double", severity="report", slide=W.text(b["key"], "slide.key"), element=ek, object=None,
                detail=f"element {ek} was written into this deck although the base ties it to no object of "
                       f"it: the person's own {oid} is still standing beside what was created"))
    return out


def _movable(base: JsonObject, live: W.LiveDeck, plan: merge.MergePlan) -> list[Finding]:
    """A `move` is written as one RELATIVE transform per root of the unit, and a transform on a group
    carries its children - so whether the step reaches every object of the unit is a question about
    the request, not about the merge. The reference applier moves every member itself, which is the
    outcome and not the mechanism, so without this the campaign cannot see a step that leaves an
    anchored picture behind: exactly what a live chain had to find instead (seed 903, where the
    person had taken the unit's group apart and the formula picture stayed at the converter's box).
    Every object of the unit must take the step, and take it once - a group and its child both
    moving would move the child twice."""
    out: list[Finding] = []
    slides = {s.object_id: s for s in live.slides}
    for p in plan.slides:
        if not isinstance(p, merge.UpdateSlide) or p.object_id not in slides:
            continue
        read = slides[p.object_id]
        read_json = sync_model.slide_read_json(W.slide_read_of(read))
        bunits = merge.units(W.elements(W.slides(base)[p.base]))
        for u in p.units:
            d = u.decision
            if not isinstance(d, merge.MoveUnit):
                continue
            mine = [o for m in bunits.get(u.key, []) for o in _strs(m.get("objects", []), "element.objects")
                    if o in read.objects]
            moved: Counter[str] = Counter()
            want = [round(v * W.SCALE * EMU_PER_PT) for v in d.delta]
            for r in Sync.move_requests([merge.planned_unit_json(u)], bunits, read_json, W.SCALE):
                body = r.get("updatePageElementTransform")
                if body is None:
                    raise JsonShapeError(f"not a move: {sorted(r)}")
                t = body["transform"]
                oid = body["objectId"]
                for x in [oid, *merge._descendants(oid, read_json)]:
                    moved[x] += 1
                got = [t.get("translateX"), t.get("translateY")]
                if got != want:
                    out.append(Finding(kind="unwritten_move", severity="report", slide=p.key, element=u.key,
                                       object=None, detail=f"{oid} is moved by {got}, not the planned {want}"))
            for oid in mine:
                if moved[oid] != 1:
                    out.append(Finding(
                        kind="unwritten_move", severity="report", slide=p.key, element=u.key, object=None,
                        detail=f"the requests move {oid} {moved[oid]} times, not once: the unit's step is "
                               f"written on {sorted(set(moved))}"))
    return out


def _zorder(read: W.LiveSlide, reqs: Sequence[SlidesRequest], created: Sequence[str]) -> dict[str, list[str]]:
    """Slides' z-order under a rewrite, as far as grouping goes: the slide as `read` has it, the
    `ungroupObjects` in `reqs` (a group's children take its place), `created` put on top in that
    order (a new object is created last, so above everything), then `updatePageElementsZOrder`
    BRING_TO_FRONT and `groupObjects` - which keeps the z-order its children have on the page, not
    the order the request lists them in, and stands where the topmost of them stood (the fix of
    dc8523a is exactly that difference). Returns every container's list bottom to top: "" for the
    page, a group id for its children."""
    lists: dict[str, list[str]] = {"": [o for o in read.order]}
    for oid, rb in read.objects.items():
        if rb.kind == "elementGroup":
            lists[oid] = [c for c in rb.children or ()]

    def where(oid: str) -> list[str] | None:
        return next((lst for lst in lists.values() if oid in lst), None)
    for r in reqs:
        ungroup = r.get("ungroupObjects")
        if ungroup is not None:
            for g in ungroup["objectIds"]:
                lst = where(g)
                if lst is not None:
                    i = lst.index(g)
                    lst[i:i + 1] = lists.pop(g, [])
    lists[""] += created
    for r in reqs:
        front, group = r.get("updatePageElementsZOrder"), r.get("groupObjects")
        if front is not None:
            assert front["operation"] == "BRING_TO_FRONT", front
            for oid in front["pageElementObjectIds"]:
                lst = where(oid)
                if lst is not None:
                    lst.remove(oid)
                lists[""].append(oid)
        elif group is not None:
            gid = group.get("groupObjectId")
            if gid is None:
                raise JsonShapeError("groupObjects.groupObjectId: missing")
            page = lists[""]
            kids = sorted((c for c in group["childrenObjectIds"] if c in page),
                          key=page.index)
            if kids:
                at = page.index(kids[-1]) - len(kids) + 1
                lists[""] = page = [c for c in page if c not in kids]
                page.insert(at, gid)
            lists[gid] = kids
    return lists


def _restacked(sync: Sync, p: JsonObject, read: W.LiveSlide, read_json: JsonObject, now: W.LiveSlide,
               lists: Mapping[str, list[str]], tops: Mapping[str, str]) -> None:
    """`now.order` as `Sync.restack`'s requests leave it, replacing the applier's own opinion.

    `restack` runs in `finish`, after the content batch and before the cleanup deletions, so the
    slide it is handed is the deck as it was plus every object this sync created (Slides puts a new
    object in front of everything) with the old ones still standing - `lists`, the containers after
    the ungrouping and regrouping - and `doomed` is what the cleanup will take away, which here is
    simply everything the applier's own write made disappear."""
    doomed = {oid for oid in read.objects if oid not in now.objects}
    objects: dict[str, JsonObject] = {oid: sync_model.readback_json(rb) for oid, rb in now.objects.items()}
    for oid in doomed:
        objects[oid] = sync_model.readback_json(read.objects[oid])
    for g, kids in lists.items():
        if g in objects:
            objects[g]["children"] = W.jstrs([c for c in kids if c in objects])
    page = [x for x in lists[""] if x in objects]
    w: dict[str, object] = {"plan": p, "doomed": doomed, "tops": tops}
    order = list(page)
    slide: JsonObject = {"objectId": now.object_id, "order": W.jstrs(page), "objects": {k: v for k, v in objects.items()}}
    for r in sync.restack(w, read_json, slide):
        front = r.get("updatePageElementsZOrder")
        if front is None:
            raise JsonShapeError(f"not a restack: {sorted(r)}")
        oid, = front["pageElementObjectIds"]
        if oid in order:
            order.append(order.pop(order.index(oid)))
    kept = [ObjectId(x) for x in order if x in now.objects]
    now.order = kept + [x for x in now.order if x not in kept and x in now.objects]


def _stacked(base: JsonObject, typed_base: Base, live: W.LiveDeck, after: W.LiveDeck, ours: W.WorldOurs,
             plan: merge.MergePlan, tok: str) -> list[Finding]:
    """Z-order as sync's own requests write it, rather than as the applier models it.

    A group a rewrite takes apart is made again by `sync.Sync.regroup_requests`, and what order its
    children end up in is a question about those requests and Slides' rules, not about the merge:
    the reference applier gives a rewritten object its old place in the group, which is the outcome,
    not the mechanism. So without this the campaign cannot see a block's panels recreated on top of
    the body text sync kept - exactly what a live sync did (dc8523a), and what nothing but
    `loss_oracle.occlusion_findings` notices, since nothing is deleted.

    The page's own element order is replayed the same way (`Sync.restack`), and for a sharper reason
    than symmetry. The applier's `_page_order` / `_restack` / `_not_over_kept` are a hand-written
    *copy* of that method, so every fix to one has to be made twice and a campaign judging only the
    copy cannot see the two drift apart - the day the occlusion rule missed a person's group holding
    two of the converter's texts (converted seed 8300231 at chain 11) it was found only because both
    halves were wrong in the same way. Now the requests decide and the copy is what the campaign
    fails against."""
    slides = {s.object_id: s for s in live.slides}
    stacked = W.copy_deck(after)
    judged: set[str] = set()
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours.json
    for p in plan.slides:
        if not isinstance(p, merge.UpdateSlide) or p.object_id not in slides:
            continue
        sid = p.object_id
        read = slides[sid]
        read_json = sync_model.slide_read_json(W.slide_read_of(read))
        bunits = merge.units(W.elements(W.slides(base)[p.base]))
        regroup, depth, roots_removed = Sync.regroups([merge.planned_unit_json(u) for u in p.units], bunits, read_json)
        now = next((s for s in stacked.slides if s.object_id == sid), None)
        if now is None:
            continue
        skey = ours.typed.slides[p.ours].key
        tops: dict[str, str] = {}
        created: list[str] = []
        removed = {W.text(k, "unit.key"): _strs(v, "unit.roots") for k, v in roots_removed}
        for u in p.units:
            if not isinstance(u.decision, merge.CreateUnit | merge.Recreate) or u.key not in u.ours_members:
                continue
            kept = [r for r in removed.get(u.key) or [] if r in now.objects]  # (the reference keeps an anchored unit's group)
            tops[u.key] = kept[0] if kept else W.made_id(skey, u.key, tok)
            if not kept:
                created.append(tops[u.key])
        if not regroup and not tops:
            continue
        ungroup: list[SlidesRequest] = [{"ungroupObjects": {"objectIds": [g]}}
                                        for g in sorted(regroup, key=lambda g: depth[g])]
        rank = Sync.zrank(W.obj(W.arr(ours.json["slides"], "ours.slides")[p.ours], "ours.slide"), bunits, tops)
        lists = _zorder(read, ungroup + Sync.regroup_requests(regroup, depth, W.obj(read_json["objects"], "objects"), tops,
                                                              set(), rank), created)
        for g in regroup:
            rb = now.objects.get(g)
            if rb is None or g not in lists:
                continue
            had = rb.children or ()
            mine = [ObjectId(c) for c in lists[g] if c in had]
            now.objects[ObjectId(g)] = replace(rb, children=tuple(mine + [c for c in had if c not in mine]))
        _restacked(sync, merge.slide_plan_json(p), read, read_json, now, lists, tops)
        judged.add(sid)
    if not judged:
        return []

    def pick(d: W.LiveDeck) -> DeckRead:
        read = W.deck_read_of(d)
        return replace(read, slides=tuple(s for s in read.slides if s.object_id in judged))
    return [replace(f, detail=f"stacked as sync.Sync.regroup_requests and Sync.restack stack it: {f.detail}")
            for f in loss_oracle.occlusion_findings_of(typed_base, pick(live), pick(stacked), ours.typed)]


def _settled(doc: JsonObject, next_base: JsonObject | None, after: W.LiveDeck, tmp: Path, unsure: bool) -> list[Finding]:
    """Syncing the same source again must write nothing. The base a sync leaves behind is what the
    next one merges against, so a base that doesn't describe the deck it just wrote would have the
    next sync rewrite those units - and a rewrite is where a person's work gets lost.

    There used to be a second excuse here, `reordered`: the source moved a frame somewhere in this
    chain, the alignment keeps the order, so a frame that crossed another falls out of it, and where
    the content says too little for `identity.cross_pairs` to pick it up the property cannot hold -
    docs/sync.md's "Not supported yet". It never once fired: measured by running the campaign with it
    taken out, 2,200 chained rounds over three shapes and three depths, then 2,900 more after the two
    defects below, and every `second_sync_writes` there was is one the excuse would have let through
    as a note. An excuse nothing needs is the one way to make sure nothing is caught (the empty
    `KNOWN` of `fuzz_docs`, the same argument), so it is gone; should a round ever fail for exactly
    that reason, the paragraph above is why, and it belongs back.

    `unsure`: this sync told the person that some frame may be on the wrong slide
    (`loss_oracle.uncertain_slides`). It is the same excuse, one step further along: a frame put
    where the words allowed and the report asked about goes on being put there, so what it did not
    write into the slide it came from is still outstanding next time. Round-level, not per slide,
    because the settle names the slide the *next* plan would write, which is by construction not
    the one this one was warned about."""
    if next_base is None:
        return []
    ours = W.build_ours(doc, next_base, tmp)
    again = merge.plan_merge_of(sync_model.base(next_base), ours.typed, W.deck_read_of(after), None, False,
                                merge.Resolutions(()))
    if not merge.has_writes(merge.merge_plan_json(again), [s.object_id for s in after.slides]):
        return []
    busy: list[str] = []
    for p in again.slides:
        if isinstance(p, merge.CreateSlide):
            busy.append(f"{p.key}: create")
        elif isinstance(p, merge.DeleteSlide):
            busy.append(f"{p.key}: delete")
        elif isinstance(p, merge.UpdateSlide) and (
                any(isinstance(u.decision, merge.CreateUnit | merge.Recreate | merge.DeleteUnit | merge.MoveUnit)
                    for u in p.units)
                or (p.background_written and p.background) or p.notes is not None):
            keys = [u.key for u in p.units if not isinstance(u.decision, merge.KeepUnit | merge.KeepRemoved | merge.KeptJoined)]
            busy.append(f"{p.key}: update {keys}")
    return [Finding(kind="second_sync_writes", severity="note" if unsure else "report",
                    slide=busy[0].split(":")[0] if busy else "order", element=None, object=None,
                    detail="the same source synced again would write: " + ("; ".join(busy) or "another slide order"))]


def _draw(rng: random.Random, deck_ops: Sequence[str] | None, source_ops: Sequence[str] | None) -> tuple[list[str], list[str]]:
    if deck_ops is None:
        deck = [rng.choice(sorted(DECK_OPS)) for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
    else:
        deck = list(deck_ops)
    if source_ops is None:
        source = [rng.choice(sorted(SOURCE_OPS)) for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.4:
            source.append("collide")   # worth more than any other draw (see `src_collide`)
    else:
        source = list(source_ops)
    return deck, source


def offline_chain(seed: int, chain: int, ops: Sequence[Ops] | None, work: Path | None, shape: W.Shape,
                  first_sync: bool) -> Chain:
    """`chain` edit+sync steps on one deck. Each sync starts from the base the previous one wrote
    (`fuzz_world.rebase`), which is where a sync undoing what the last one merged would show.
    `ops`: per step the op names (None, or a step past its end: drawn from the seed).
    `shape`: which world the deck is drawn from - "converted" (a talk this converter made) or
    "adopt" (a foreign deck `adopt` took over: `fuzz_world.make_adopt_doc`).
    `first_sync`: start from the base `adopt` records rather than the one `convert` writes
    (`fuzz_world.build_adopt_base`) - nothing has ever been written to this deck, its objects are a
    person's own, and some of them are not paired with the source at all."""
    rng = random.Random(seed)
    tmp = work or Path(tempfile.mkdtemp(prefix="b2s-fuzz-"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        doc = W.make(shape, rng, tmp)
        base = W.build_adopt_base(doc, tmp, rng) if first_sync else W.build_base(doc, tmp)
        live = W.live_of(base)
        steps: list[Step] = []
        for step in range(chain):
            want = ops[step] if ops and step < len(ops) else Ops(deck=None, source=None)
            deck_ops, source_ops = _draw(rng, want.deck, want.source)
            # Every step rebases: the next step needs that base, and the last step's base is what
            # the "a second sync writes nothing" check judges.
            record = _sync_step(seed, step, doc, base, live, tmp, deck_ops, source_ops, True)
            steps.append(record)
            doc, live = record.state.doc, record.state.after
            base = record.state.next_base or base
        return Chain(seed=seed, chain=chain, shape=shape, first_sync=first_sync, steps=tuple(steps),
                     ops=tuple(Ops(deck=s.deck_ops, source=s.source_ops) for s in steps))
    finally:
        if work is None:
            shutil.rmtree(tmp, ignore_errors=True)


def offline_round(seed: int, source_ops: Sequence[str] | None, deck_ops: Sequence[str] | None, work: Path | None,
                  shape: W.Shape, first_sync: bool) -> Step:
    """One offline round. `source_ops` / `deck_ops`: op names (None: drawn from the seed)."""
    want = Ops(deck=None if deck_ops is None else tuple(deck_ops),
               source=None if source_ops is None else tuple(source_ops))
    return offline_chain(seed, 1, [want], work, shape, first_sync).steps[0]


OpField = Literal["deck_ops", "source_ops"]


def _step_ops(s: Step, field: OpField) -> tuple[str, ...]:
    if field == "deck_ops":
        return s.deck_ops
    if field == "source_ops":
        return s.source_ops
    assert_never(field)


def shrink_offline(result: Step, limit: int, shape: W.Shape, first_sync: bool) -> Step:
    """Drop edits one at a time while the round keeps failing."""
    best = result
    tries = 0
    fields: tuple[OpField, ...] = ("deck_ops", "source_ops")
    for field in fields:
        changed = True
        while changed and tries < limit:
            changed = False
            for i in range(len(_step_ops(best, field))):
                have = _step_ops(best, field)
                if len(have) <= 1 and field == "deck_ops":
                    break
                ops = have[:i] + have[i + 1:]
                tries += 1
                candidate = offline_round(best.seed, ops if field == "source_ops" else best.source_ops,
                                          ops if field == "deck_ops" else best.deck_ops, None, shape, first_sync)
                if candidate.failures:
                    best, changed = candidate, True
                    break
    return best


Side = Literal["deck", "source"]


def _side(o: Ops, field: Side) -> tuple[str, ...]:
    if field == "deck":
        return o.deck or ()
    if field == "source":
        return o.source or ()
    assert_never(field)


def shrink_chain(result: Chain, limit: int) -> Chain:
    """Drop edits one at a time, in the first failing step and the ones before it."""
    failing = next(i for i, s in enumerate(result.steps) if s.failures)
    best = replace(result, ops=result.ops[:failing + 1], chain=failing + 1)
    tries = 0
    sides: tuple[Side, ...] = ("deck", "source")
    for step in range(failing, -1, -1):
        for field in sides:
            changed = True
            while changed and tries < limit:
                changed = False
                for i in range(len(_side(best.ops[step], field))):
                    have = _side(best.ops[step], field)
                    cut = have[:i] + have[i + 1:]
                    ops = list(best.ops)
                    ops[step] = replace(ops[step], deck=cut) if field == "deck" else replace(ops[step], source=cut)
                    if step == failing and field == "deck" and not cut:
                        continue
                    tries += 1
                    candidate = offline_chain(best.seed, best.chain, ops, None, result.shape, result.first_sync)
                    if candidate.steps[-1].failures:
                        best, changed = candidate, True
                        break
    return best


def describe_chain(result: Chain) -> str:
    lines: list[str] = []
    for s in result.steps:
        lines.append(f"  step {s.step}: deck:   " + "; ".join(s.deck))
        lines.append("          source: " + "; ".join(s.source))
        if s.refused:
            lines.append("          refused: " + ", ".join(s.refused) + " (nothing written)")
        if s.failures:
            lines.append(loss_oracle.described(s.failures))
    return "\n".join(lines)


def run_offline(rounds: int, seed0: int, shrink: bool, quiet: bool, chain: int, shape: W.Shape,
                first_sync: bool) -> list[Chain]:
    bad: list[Chain] = []
    refused: dict[str, int] = {}
    held: dict[str, int] = {}
    for seed in range(seed0, seed0 + rounds):
        result = offline_chain(seed, chain, None, None, shape, first_sync)
        for reason in result.refused:
            refused[reason] = refused.get(reason, 0) + 1
        for what in result.held:
            held[what] = held.get(what, 0) + 1
        if result.failures:
            bad.append(shrink_chain(result, 300) if shrink else result)
            if not quiet:
                print(f"seed {seed}: {len(result.failures)} finding(s)")
                print(describe_chain(bad[-1]))
        elif not quiet and (seed - seed0) % 50 == 49:
            print(f"  ... {seed - seed0 + 1} rounds")
    if held and not quiet:
        # Nor are these: what a sync into a person's deck left alone because nothing here may write
        # there (an element the base could tie to no object, a slide no frame accounts for). The
        # rest of that sync went in and the oracle judged it.
        print("  held: " + ", ".join(f"{k} {v}" for k, v in sorted(held.items())))
    if refused and not quiet:
        # Not failures: a sync that refused wrote nothing, so there was nothing for the oracle to
        # judge. Counting them is how one sees whether the refusals are so broad that the campaign
        # stopped exercising the merge at all.
        print("  refused: " + ", ".join(f"{k} {v}" for k, v in sorted(refused.items())))
    return bad


# ---------------------------------------------------------------- live rounds

MIKTEX = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
       "PATH": os.pathsep.join([os.environ.get("PATH", "")] + ([str(MIKTEX)] if MIKTEX.exists() else []))}
PDFIUM = threading.Lock()

EditsMode = Literal["batched", "reread"]
EDITS_MODES: tuple[EditsMode, ...] = ("batched", "reread")
Focus = Literal["layout", "probes"]
FOCUSES: tuple[Focus, ...] = ("layout", "probes")


def edits_mode_of(v: str) -> EditsMode:
    for m in EDITS_MODES:
        if m == v:
            return m
    raise ValueError(f"no edits mode {v!r}")


def focus_of(v: str | None) -> Focus | None:
    if v is None:
        return None
    for f in FOCUSES:
        if f == v:
            return f
    raise ValueError(f"no focus {v!r}")


def _slide_title(spec: Mapping[str, Json]) -> str | None:
    """The slide title an edit spec points at (`{"slide": {"title": ...}}` or a plain title)."""
    args = spec.get("args")
    sel = args.get("slide") if isinstance(args, dict) else None
    if isinstance(sel, dict):
        title = sel.get("title")
        return title if isinstance(title, str) else None
    return sel if isinstance(sel, str) else None


@dataclass(frozen=True, kw_only=True)
class SyncBuild:
    """What a live round uses of tests/decks/sync/build.py: its variants (name -> flags), what each
    flag or probe edit changes (build.INTENDED, and PROBE_INTENDED's non-empty entries: items
    "<title>: ..."), and `build`, which compiles a variant and returns its PDF."""
    variants: dict[str, tuple[str, ...]]
    intended: dict[str, tuple[str, ...]]
    build: Callable[[str], Path]


def _table(v: object, where: str) -> dict[str, tuple[str, ...]]:
    """One of build.py's tables (name -> a list of strings), checked where it comes in."""
    if not isinstance(v, dict):
        raise TypeError(f"{where} is not a table")
    out: dict[str, tuple[str, ...]] = {}
    for k, items in v.items():
        if not isinstance(k, str) or not isinstance(items, list):
            raise TypeError(f"{where}[{k!r}] is not a list")
        out[k] = tuple(str(i) for i in items)
    return out


def sync_build() -> SyncBuild:
    path = ROOT / "tests" / "decks" / "sync" / "build.py"
    spec = importlib.util.spec_from_file_location("sync_build", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names: dict[str, object] = vars(module)
    fn = names["build"]
    if not callable(fn):
        raise TypeError(f"{path}: build is not a function")

    def build(variant: str) -> Path:
        pdf = fn(variant)
        if not isinstance(pdf, Path):
            raise TypeError(f"{path}: build({variant!r}) returned no path")
        return pdf

    probes = _table(names.get("PROBE_INTENDED", {}), "PROBE_INTENDED")
    return SyncBuild(variants=_table(names["VARIANTS"], "VARIANTS"),
                     intended={**_table(names["INTENDED"], "INTENDED"), **{k: v for k, v in probes.items() if v}},
                     build=build)


def _unique_slide(model: sc.Model, s: sc.Slide) -> JsonObject:
    title = s.title
    if title and len(model.find({"title": title})) == 1:
        return {"title": title}
    return {"index": s.index}


def _lines(el: sc.Element) -> list[str]:
    return [l.strip() for raw, _ in el.texts for l in raw.split("\n") if len(l.strip().split()) >= 3]


def _free_box(rng: random.Random, s: sc.Slide, candidates: Sequence[list[float]], kind: str) -> list[float] | None:
    """One of `candidates` ([x, y, w, h], slide pt) with no element of that kind standing on it, or
    None when they are all taken. Found by the live chain (seed 504): the same picture dropped on
    the same slide at the same box in two steps is a real duplicate of the fuzzer's own making, and
    both the checker (`integrity`) and the edit's own `count: 1` are right to say so. The campaign
    is for what *sync* loses, so the edits have to stay distinguishable from each other."""
    # What `integrity` calls a duplicate is (descriptor, alt text, box), and the descriptor of a
    # picture or of a shape with nothing written in it is the same for all of them: those are the
    # ones a second copy would collide with, not a text box that happens to stand there.
    taken = [e.center for e in s.elements if e.kind == kind and not e.text]
    free = [b for b in candidates
            if not any(abs(c[0] - (b[0] + b[2] / 2)) < 30 and abs(c[1] - (b[1] + b[3] / 2)) < 30 for c in taken)]
    return rng.choice(free) if free else None


def _near(e: sc.Element) -> JsonObject:
    """A picture named by where it stands (`deck_edits`' `image_near` target)."""
    return {"image_near": W.jnums([round(v, 1) for v in e.center])}


def random_spec(model: sc.Model, rng: random.Random, donor: str | None, focus: Focus | None,
                aim: Collection[str]) -> JsonObject | None:
    """One human-like edit spec for the live deck, found by content (tools/deck_edits.py kinds).
    `focus`: a name in FOCUS - draw one of its aims instead of a uniform kind (`focus_spec`), on
    one of the slides in `aim` (objectIds) most of the time."""
    if focus is not None:
        return focus_spec(model, rng, donor, FOCUS[focus], aim)
    slides = [s for s in model.slides if s.elements]
    if not slides:
        return None
    s = rng.choice(slides)
    sel = _unique_slide(model, s)
    texts = [e for e in s.elements if e.kind == "shape" and e.texts and _lines(e)]
    images = [e for e in s.elements if e.kind == "image"]
    kind = rng.choice(["replace_word", "append_sentence", "delete_paragraph", "bold", "recolour", "resize_font",
                       "move", "resize", "delete_element", "add_text_box", "add_shape", "duplicate", "group",
                       "ungroup", "add_slide", "duplicate_slide", "delete_slide", "move_slide", "set_notes",
                       "set_background"] + (["add_image"] if donor else []))
    if kind in ("replace_word", "bold", "recolour", "resize_font", "append_sentence", "delete_paragraph",
                "move", "delete_element", "duplicate") and not texts:
        kind = "add_text_box"
    if kind == "replace_word":
        el = rng.choice(texts)
        line = rng.choice(_lines(el))
        words = [w for w in line.split() if len(w) >= 4 and w.isalpha()]
        if not words or len(model.find(sel)) != 1:
            return None
        old = rng.choice(words)
        return {"edit": "replace_word", "args": {"slide": sel, "text": line, "old": old, "new": old + "ed"}}
    if kind == "append_sentence":
        el = rng.choice(texts)
        return {"edit": "append_sentence", "args": {"slide": sel, "text": rng.choice(_lines(el)),
                                                    "sentence": f"Fuzz sentence {rng.randrange(1000)}."}}
    if kind == "delete_paragraph":
        el = rng.choice([e for e in texts if len(_lines(e)) > 1] or texts)
        if len(_lines(el)) < 2:
            return None
        return {"edit": "delete_paragraph", "args": {"slide": sel, "text": rng.choice(_lines(el))}}
    if kind in ("bold", "recolour"):
        el = rng.choice(texts)
        line = rng.choice(_lines(el))
        words = [w for w in line.split() if len(w) >= 4 and w.isalpha()]
        if not words:
            return None
        args: JsonObject = {"slide": sel, "word": rng.choice(words), "context": line}
        if kind == "recolour":
            args["color"] = rng.choice(["#c00000", "#0070c0"])
        return {"edit": kind, "args": args}
    if kind == "resize_font":
        return {"edit": "resize_font", "args": {"slide": sel, "text": rng.choice(_lines(rng.choice(texts))),
                                                "size": rng.choice([14, 16, 20])}}
    if kind == "move":
        target: JsonObject = ({"image": "largest"} if images and rng.random() < 0.4
                              else {"text": rng.choice(_lines(rng.choice(texts)))})
        return {"edit": "move", "args": {"slide": sel, "target": target, "dx": rng.choice([-20, 0, 20]),
                                         "dy": rng.choice([-20, 15, 30])}}
    if kind == "resize":
        if not images:
            return None
        return {"edit": "resize", "args": {"slide": sel, "target": {"image": "largest"}, "sx": rng.choice([0.8, 1.2])}}
    if kind == "delete_element":
        return {"edit": "delete_element", "args": {"slide": sel, "target": {"text": rng.choice(_lines(rng.choice(texts)))}}}
    if kind == "add_text_box":
        return {"edit": "add_text_box", "args": {"slide": sel, "text": f"fuzz note {rng.randrange(10 ** 6)}",
                                                 "box": [rng.choice([440, 470, 500]), rng.choice([40, 300, 330]), 200, 30]}}
    if kind == "add_shape":
        shape = rng.choice(["STAR_5", "ELLIPSE", "CLOUD"])
        box = _free_box(rng, s, [[x, y, 44, 44] for x in (600, 640) for y in (60, 300)], "shape")
        return None if box is None else {"edit": "add_shape", "args": {"slide": sel, "shape_type": shape,
                                                                      "box": W.jnums(box), "color": "#ffc000"}}
    if kind == "add_image":
        box = _free_box(rng, s, [[x, y, 140, 90] for x in (500, 540) for y in (250, 280)], "image")
        return None if box is None else {"edit": "add_image", "args": {"slide": sel, "url": donor, "box": W.jnums(box)}}
    if kind == "duplicate":
        return {"edit": "duplicate", "args": {"slide": sel, "target": {"text": rng.choice(_lines(rng.choice(texts)))},
                                              "dx": 0, "dy": rng.choice([90, 110])}}
    if kind == "group":
        tops = [e for e in s.elements if not e.groups and e.kind in ("shape", "image") and (e.text or e.kind == "image")]
        if len(tops) < 2:
            return None
        a, b = rng.sample(tops, 2)
        targets: list[Json] = [{"text": a.text[:50]} if a.text else _near(a),
                               {"text": b.text[:50]} if b.text else _near(b)]
        return {"edit": "group", "args": {"slide": sel, "targets": targets}}
    if kind == "ungroup":
        inside = [e for e in s.elements if e.groups and e.kind != "group" and e.text]
        if not inside:
            return None
        el = rng.choice(inside)
        return {"edit": "ungroup", "args": {"slide": sel, "target": {"text": el.text[:50]}}}
    if kind == "add_slide":
        return {"edit": "add_slide", "args": {"after": sel, "title": f"Fuzz slide {rng.randrange(1000)}",
                                              "body": "content only the person knows"}}
    if kind == "duplicate_slide":
        return {"edit": "duplicate_slide", "args": {"slide": sel, "new_title": f"Copy {rng.randrange(1000)}"}}
    if kind == "delete_slide":
        return {"edit": "delete_slide", "args": {"slide": sel}}
    if kind == "move_slide":
        others = [x for x in model.slides if x.id != s.id]
        if not others:
            return None
        return {"edit": "move_slide", "args": {"slide": sel, "after": _unique_slide(model, rng.choice(others))}}
    if kind == "set_notes":
        return {"edit": "set_notes", "args": {"slide": sel, "text": f"speaker note {rng.randrange(10 ** 6)}"}}
    return {"edit": "set_background", "args": {"slide": sel, "color": rng.choice(["#fff2cc", "#eaf1dd"])}}


# ---------------------------------------------------------------- focused generation (--focus)
#
# The uniform draw above spreads a step over twenty kinds, and more than half of them (slides,
# notes, backgrounds, font sizes, colours) can never set up a *layout* defect: that needs the
# person's change and the source's re-layout on the same slide, and the same unit (devtools/
# fuzz_reach.py measures which steps got there). A focused draw picks an aim - an edit made for one
# of those preconditions - and, most of the time, a slide the step's source variant changes
# (`variant_slides`), which is `src_collide`'s argument made live: the author revises the frame the
# reader was editing. The recorded spec carries its `aim`, so the reach tables can tell them apart.

FOOTER = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
REMARKS = ("this is a longer remark the person typed, long enough to wrap onto another line of the box",
           "a sentence added in the deck that runs on well past the width the converter measured")

Aim = Callable[[random.Random, sc.Model, sc.Slide, JsonObject, "str | None"], "JsonObject | None"]
"""(rng, model, slide, its selector, donor picture url) -> an edit spec for one aim, or None."""


def _maybe(rng: random.Random, items: Sequence[T]) -> T | None:
    """`rng.choice(items or [None])`: one of them, or None - drawn either way, so the dice move alike."""
    if not items:
        return rng.choice([None])
    return rng.choice(items)


def _placeholder(e: sc.Element) -> str | None:
    return e.placeholder_type


def _body_texts(s: sc.Slide) -> list[sc.Element]:
    """Text boxes of a slide the person would write in: not its title, not the frame counter."""
    return [e for e in s.elements if e.kind == "shape" and e.texts and _lines(e)
            and _placeholder(e) not in ("TITLE", "CENTERED_TITLE") and not FOOTER.match(e.text)]


def _aim_long_text(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                   donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _body_texts(s))
    if el is None:
        return None
    return {"edit": "append_sentence", "args": {"slide": sel, "text": rng.choice(_lines(el)),
                                                "sentence": f"{rng.choice(REMARKS)} ({rng.randrange(1000)})."}}


def _aim_new_paragraph(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                       donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _body_texts(s))
    if el is None:
        return None
    return {"edit": "add_paragraph", "args": {"slide": sel, "text": rng.choice(_lines(el)),
                                              "paragraph": f"A point the person added, number {rng.randrange(1000)}"}}


def _aim_hole(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
              donor: str | None) -> JsonObject | None:
    """Words typed or changed right in front of an inline formula (a run of no-break spaces)."""
    lines = [l for e in _body_texts(s) for l in _lines(e) if "\xa0" in l]
    if not lines:
        return None
    line = rng.choice(lines)
    if rng.random() < 0.5:
        return {"edit": "insert_before_hole", "args": {"slide": sel, "text": line,
                                                       "words": f"{rng.choice(('roughly', 'exactly', 'plainly'))} {rng.randrange(1000)}"}}
    words = line.split("\xa0")[0].replace("​", "").split()  # (emit.HOLE_BREAK is no word)
    if not words or not words[-1].isalpha():
        return None
    return {"edit": "replace_word", "args": {"slide": sel, "text": line, "old": words[-1],
                                             "new": words[-1] + "-considerably-longer"}}


def _aim_move_text(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                   donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _body_texts(s))
    if el is None:
        return None
    return {"edit": "move", "args": {"slide": sel, "target": {"text": rng.choice(_lines(el))},
                                     "dx": rng.choice([-30, 0, 30]), "dy": rng.choice([-30, 30, 60])}}


def _aim_note_below(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                    donor: str | None) -> JsonObject | None:
    """The person's own note just under a text or a table - where a list grows when the source adds
    to it, and where a table grows when it gains a row."""
    options = _body_texts(s) + [e for e in s.elements if e.kind == "table"]
    if not options:
        return None
    el = rng.choice(options)
    y = el.box[3] + rng.choice([2, 8, 16])
    if y > 370:
        return None
    return {"edit": "add_text_box", "args": {"slide": sel, "text": f"note under it {rng.randrange(10 ** 6)}",
                                             "box": [round(el.box[0] + rng.choice([0, 30]), 1), round(y, 1), 220, 28]}}


def _grouped(s: sc.Slide) -> list[sc.Element]:
    return [e for e in s.elements if e.groups and e.kind in ("shape", "table") and e.text and not FOOTER.match(e.text)]


def _aim_group_move(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                    donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _grouped(s))
    if el is None:
        return None
    return {"edit": "move", "args": {"slide": sel, "target": {"text": el.text[:50]},
                                     "dx": rng.choice([-20, 20]), "dy": rng.choice([-20, 20, 40])}}


def _aim_group_resize(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                      donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _grouped(s))
    if el is None:
        return None
    return {"edit": "resize", "args": {"slide": sel, "target": {"text": el.text[:50]},
                                       "sx": rng.choice([0.85, 1.15]), "sy": rng.choice([0.85, 1.0, 1.15])}}


def _aim_group_pair(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                    donor: str | None) -> JsonObject | None:
    """The person groups a text of the converter's with its neighbour."""
    tops = [e for e in s.elements if not e.groups and e.kind in ("shape", "image") and (e.text or e.kind == "image")
            and not FOOTER.match(e.text) and _placeholder(e) not in ("TITLE", "CENTERED_TITLE")]
    texts = [e for e in tops if e.text]
    if len(tops) < 2 or not texts:
        return None
    a = rng.choice(texts)
    b = rng.choice([e for e in tops if e is not a])
    return {"edit": "group", "args": {"slide": sel, "targets": [
        {"text": a.text[:50]}, {"text": b.text[:50]} if b.text else _near(b)]}}


def _aim_narrow(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                donor: str | None) -> JsonObject | None:
    el = _maybe(rng, _body_texts(s))
    if el is None:
        return None
    return {"edit": "resize", "args": {"slide": sel, "target": {"text": rng.choice(_lines(el))},
                                       "sx": rng.choice([0.7, 0.8]), "sy": 1.0}}


def _aim_cell(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
              donor: str | None) -> JsonObject | None:
    """A table cell made long enough to wrap: the row grows, and the table with it."""
    tables = [e for e in s.elements if e.kind == "table"]
    if not tables:
        return None
    cells = [" ".join(raw.split()) for raw, _ in rng.choice(tables).texts if raw.strip()]
    if not cells:
        return None
    return {"edit": "append_sentence", "args": {"slide": sel, "text": rng.choice(cells),
                                                "sentence": f"(measured again by hand, {rng.randrange(1000)} runs)"}}


def _table_anchor(rng: random.Random, s: sc.Slide) -> tuple[sc.Element, str] | None:
    tables = [e for e in s.elements if e.kind == "table"]
    if len(tables) != 1:
        return None
    cells = [" ".join(raw.split()) for raw, c in tables[0].texts if raw.strip() and c]
    return (tables[0], rng.choice(cells)) if cells else None


def _aim_row(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
             donor: str | None) -> JsonObject | None:
    """A row the person added: the table is taller than the source's before the source moves it."""
    anchor = _table_anchor(rng, s)
    if anchor is None:
        return None
    table, text = anchor
    n = rng.randrange(1000)
    words = [f"Case {n}", f"{rng.randint(50, 100)}%", f"{rng.randint(10, 99) / 10} s", "by hand"]
    return {"edit": "insert_table_row", "args": {"slide": sel, "text": text,
                                                 "cells": W.jstrs(words[:table.table_size[1]])}}


def _aim_column(rng: random.Random, model: sc.Model, s: sc.Slide, sel: JsonObject,
                donor: str | None) -> JsonObject | None:
    anchor = _table_anchor(rng, s)
    if anchor is None:
        return None
    table, text = anchor
    n = rng.randrange(1000)
    cells = [f"Note {n}"] + [f"r{n}-{i}" for i in range(1, table.table_size[0])]
    return {"edit": "insert_table_column", "args": {"slide": sel, "text": text, "cells": W.jstrs(cells)}}


AIMS: dict[str, Aim] = {
    "long_text": _aim_long_text, "new_paragraph": _aim_new_paragraph, "hole": _aim_hole,
    "move_text": _aim_move_text, "note_below": _aim_note_below, "group_move": _aim_group_move,
    "group_resize": _aim_group_resize, "group_pair": _aim_group_pair, "narrow": _aim_narrow,
    "cell": _aim_cell, "row": _aim_row, "column": _aim_column}
# aim -> weight; "uniform" is one draw of the default generator, so a focused campaign still
# touches everything the general one does, only less often.
LAYOUT_AIMS: dict[str, float] = {"long_text": 3, "new_paragraph": 2, "hole": 3, "move_text": 2, "note_below": 3,
                                 "group_move": 2, "group_resize": 1, "group_pair": 1, "narrow": 1, "cell": 2,
                                 "row": 2, "column": 1, "uniform": 3}
# the same aims, on a round that starts from the layout probe frames (`start_variant`)
FOCUS: dict[Focus, dict[str, float]] = {"layout": LAYOUT_AIMS, "probes": LAYOUT_AIMS}
AIMED = 0.75   # how often a focused edit goes to a slide the step's variant changes


def focus_spec(model: sc.Model, rng: random.Random, donor: str | None, weights: Mapping[str, float],
               aim: Collection[str]) -> JsonObject | None:
    """One edit drawn for an aim (`FOCUS`), on a slide of `aim` (objectIds) with probability AIMED."""
    names = sorted(weights)
    name = rng.choices(names, [weights[n] for n in names])[0]
    if name == "uniform":
        return random_spec(model, rng, donor, None, ())
    slides = [s for s in model.slides if s.elements]
    aimed = [s for s in slides if s.id in set(aim)]
    if not slides:
        return None
    s = rng.choice(aimed) if aimed and rng.random() < AIMED else rng.choice(slides)
    spec = AIMS[name](rng, model, s, _unique_slide(model, s), donor)
    if spec is not None:
        spec["aim"] = name
    return spec


# Variants and the slides they change, for the focused draw. A flag that re-lays text on a slide
# (rewords it, adds or removes a line, changes a formula, a block, a table) is what a layout defect
# needs from the source side; one that only renames, reorders or notes is worth less to it.
REFLOWS: dict[str, float] = {"reword": 1, "addbullet": 1, "removebullet": 1, "formula": 1, "blockedit": 1,
                             "tablemove": 1, "tablerow": 1, "numbers": 1, "tablecell": 0.5, "figure": 0.5,
                             "retitle": 0.25, "untitled": 0.25}


def start_variant(focus: Focus | None) -> str:
    """The variant a round converts first: v1, or `probes` for --focus probes (the layout probe
    frames of tests/decks/sync, which only its probes-* variants change)."""
    return "probes" if focus == "probes" else "v1"


def variant_weights(build: SyncBuild, focus: Focus | None) -> dict[str, float]:
    """variant -> weight of being drawn: uniform by default, by what it re-lays when focused."""
    if focus == "probes":
        return {v: 1.0 for v in build.variants if v.startswith("probes-")}
    variants = [v for v in build.variants if v != "v1"]
    if focus is None:
        return {v: 1.0 for v in variants}
    return {v: 0.25 + sum(REFLOWS.get(f, 0) for f in build.variants[v]) for v in variants}


def variant_slides(build: SyncBuild, variant: str) -> set[str]:
    """The titles of the slides `variant` changes (build.INTENDED, whose items are "<title>: ...",
    and PROBE_INTENDED for the probe edits; a slide it adds or deletes is no place for the
    person's edit)."""
    out: set[str] = set()
    for flag in build.variants[variant]:
        for item in build.intended.get(flag, ()):
            if ": " in item and not item.startswith(("slide+", "slide-", "order ")):
                out.add(item.split(": ", 1)[0])
    if "reorder" in build.variants[variant]:
        out |= {"Merge policy", "Results"}
    return out


# Failures whose cause is already known and pinned by a test, so a live round that hits one says
# what it is instead of leaving a reviewer with a stack trace.
KNOWN_CAUSES = (
    ("updatePageElementAltText: The operation is not allowed on group",
     "known: sync.tag_requests alt-texts a diagram's main object, which is the group "
     "emit.diagram_requests creates (tests/test_sync.py::test_sync_does_not_alt_text_a_diagram_group)"),
)


def known_cause(log: Path) -> str | None:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return next((why for sign, why in KNOWN_CAUSES if sign in text), None)


SLIDE_EDITS = ("add_slide", "duplicate_slide", "delete_slide", "move_slide")


def stale(deck: deck_edits.LiveDeck, spec: JsonObject) -> bool:
    """Whether a deferred `deck` must send what it queued and read again before `spec` can be found
    the way a fresh read would find it: the edit's slide has queued changes, slides came, went or
    moved and the edit is about slide order, or its selector cannot be told apart without a read
    (a `contains` selector reads text on every slide, an index counts slides)."""
    if not deck.defer or not deck.pending:
        return False
    given = spec.get("args")
    args: JsonObject = given if isinstance(given, dict) else {}
    sels = [args[k] for k in ("slide", "after") if k in args]
    if deck.reshaped and (spec["edit"] in SLIDE_EDITS or any(isinstance(s, dict) and "index" in s for s in sels)):
        return True
    for sel in sels:
        if isinstance(sel, dict) and "contains" in sel and deck.dirty:
            return True
        found = deck.model.find(sel)
        if len(found) != 1 or found[0].id in deck.dirty:
            return True
    return False


@dataclass(frozen=True, kw_only=True)
class Mark:
    """A phase `Cost.start` began: its name, the clock, and the kept counts of this thread then."""
    name: str
    t0: float
    was: dict[str, float]


Counts = dict[str, float]


def _add(into: Counts, d: Mapping[str, float]) -> None:
    """`Counter.update`: each count added to what is there (an int stays an int)."""
    for k, v in d.items():
        into[k] = v + into.get(k, 0)


class Cost:
    """Where a live round's time goes: seconds, Google calls, retries and the seconds slept backing
    off, per phase. What this thread calls is counted by `gslides.count_thread`; what a `convert` or
    `sync` subprocess calls comes back in the file `devtools.counted` writes (`subprocess`). Each
    phase is also added to the step it ran in, so round.json says both."""

    KEYS = ("calls", "retries", "backoff_s", "rate_limited")

    def __init__(self) -> None:
        from beamer2slides.gslides import count_thread
        self.api = count_thread()   # (a round runs on one thread of the pool, start to end)
        self.phases: dict[str, Counts] = {}
        self.step: dict[str, Counts] | None = None

    @classmethod
    def kept(cls, key: str) -> bool:
        # (call <methodId>: what was asked - writes are what the per-user quota counts; retry
        #  <methodId> <status>: who was refused)
        return key in cls.KEYS or key.startswith(("call ", "retry "))

    def start(self, name: str) -> Mark:
        return Mark(name=name, t0=time.monotonic(), was={k: v for k, v in self.api.items() if self.kept(k)})

    def stop(self, mark: Mark, extra: JsonObject, counts: Mapping[str, float]) -> None:
        d: Counts = {"s": time.monotonic() - mark.t0, "n": 1}
        _add(d, {k: v - mark.was.get(k, 0) for k, v in list(self.api.items()) if self.kept(k)})
        _add(d, {k: W.num(v, k) for k, v in extra.items() if self.kept(k)})
        _add(d, counts)
        for into in (self.phases, self.step):
            if into is not None:
                _add(into.setdefault(mark.name, {}), d)

    @staticmethod
    def subprocess(path: Path) -> JsonObject:
        try:
            loaded: Json = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def plain(phases: Mapping[str, Mapping[str, float]]) -> JsonObject:
        out: JsonObject = {}
        for k, c in phases.items():
            fields: JsonObject = {}
            for f, v in c.items():
                if v:
                    fields[f] = round(v, 2) if isinstance(v, float) else v
            out[k] = fields
        return out


class Template:
    """One converted v1 deck that rounds copy (`--reuse`) instead of each converting its own.

    Drive's `files.copy` of a Slides file keeps every objectId - slides, their notes pages, the
    elements, the layouts - so the base `convert` wrote describes the copy as well as it describes
    the original once its presentationId is replaced. The two things that must not come along are
    the original's *Drive* base (`appProperties.b2sBase`: the copy would sync against, and write
    into, the template's base file) and its cleanup marker; they are cleared on the copy, and the
    copy's first sync stores a base file of its own from the folder's copy (snapshot.load_base
    reads the local one when Drive names none). Measured and checked in docs/project-notes.md,
    "Live fuzzer efficiency"."""

    def __init__(self, out: Path, log: IO[str] | None) -> None:
        self.out, self.log = out, log
        self.pid = ""

    def make(self, build: SyncBuild, variant: str) -> "Template":
        """Convert `variant` into `out` once (the deck is kept until the run drops it)."""
        self.out.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "beamer2slides.devtools.counted", "convert", str(build.build(variant)),
                               "--out", str(self.out)], env=ENV, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        if done.returncode:
            raise RuntimeError(f"the template conversion failed, see {'its log' if self.log is None else self.log.name}")
        self.pid = _presentation_id(self.out)
        return self

    @classmethod
    def at(cls, out: Path) -> "Template":
        """A conversion already in `out` (nobody may edit its deck) as the template."""
        t = cls(out, None)
        t.pid = _presentation_id(out)
        return t

    def copy_into(self, out: Path, name: str) -> str:
        """A copy of the template deck, and `out` set up as its convert folder. Returns its id."""
        from beamer2slides.drive_folder import place
        from beamer2slides.google_auth import drive_service
        from beamer2slides.google_types import file_id
        from beamer2slides.gslides import execute
        drive = drive_service(None)
        pid = file_id(execute(drive.files().copy(fileId=self.pid, fields="id,appProperties",
                                                 body=place({"name": name}, drive, beside=None))), f"a copy of {self.pid}")
        execute(drive.files().update(fileId=pid, fields="id", body={"appProperties": {
            snapshot.BASE_PROPERTY: None, snapshot.CLEANED_PROPERTY: None}}))
        for p in self.out.rglob("*"):
            if p.is_file() and p.suffix != ".log":
                to = out / p.relative_to(self.out)
                to.parent.mkdir(parents=True, exist_ok=True)
                if p.suffix == ".json":
                    to.write_text(p.read_text(encoding="utf-8").replace(self.pid, pid), encoding="utf-8")
                else:
                    shutil.copyfile(p, to)
        return pid


def _load(path: Path) -> JsonObject:
    loaded: Json = json.loads(path.read_text(encoding="utf-8"))
    return W.obj(loaded, path.name)


def _presentation_id(out: Path) -> str:
    return W.text(_load(out / "emit.json")["presentationId"], "emit.json presentationId")


def drop_deck(pid: str, log: IO[str]) -> None:
    """Delete a deck and the base file Drive keeps beside it (`appProperties.b2sBase`)."""
    from beamer2slides.google_auth import drive_service
    from beamer2slides.gslides import execute
    drive = drive_service(None)
    try:
        info = execute(drive.files().get(fileId=pid, fields="appProperties"))
        props = info.get("appProperties")
        fid = props.get(snapshot.BASE_PROPERTY) if isinstance(props, dict) else None
        if isinstance(fid, str) and fid:
            execute(drive.files().delete(fileId=fid))
        execute(drive.files().delete(fileId=pid))
    except Exception as e:  # noqa: BLE001
        log.write(f"could not delete the deck: {e}\n")


# ---------------------------------------------------------------- live records (round.json)

@dataclass(frozen=True, kw_only=True)
class LiveStep:
    """One step of a live round as round.json says it. `cost`, `layout` and `reach` come after the
    judging (`LiveRound.after_step`): a step that failed before has none of them, and one whose
    layout.json or reach could not be read lacks that one."""
    step: int
    variant: str
    edits: tuple[JsonObject, ...]
    findings: tuple[Finding, ...]
    cost: JsonObject | None
    layout: dict[str, int] | None
    reach: JsonObject | None


def live_step_json(s: LiveStep) -> JsonObject:
    out: JsonObject = {"step": s.step, "variant": s.variant, "edits": W.jlist(s.edits),
                       "findings": [loss_oracle.finding_json(f) for f in s.findings]}
    if s.cost is not None:
        out["cost"] = s.cost
    if s.layout is not None:
        layout: JsonObject = {}
        for k, n in s.layout.items():
            layout[k] = n
        out["layout"] = layout
    if s.reach is not None:
        out["reach"] = s.reach
    return out


@dataclass(frozen=True, kw_only=True)
class ReuseIds:
    """How many ids the base names, and how many of them the template's copy lacks (`--reuse`)."""
    named: int
    missing: int


@dataclass(frozen=True, kw_only=True)
class RoundCost:
    """A round's clock and its phases (`Cost.plain`). `seconds` is None when the round broke off
    before it finished: round.json then has the phases alone, written after the problem."""
    seconds: float | None
    phases: JsonObject


@dataclass(frozen=True, kw_only=True)
class LiveRecord:
    """A live round: what round.json says (`record_json`), plus, after `shrink_live`, the edits of
    the smallest failing replay (`shrunk_edits`, never written)."""
    seed: int
    steps: tuple[LiveStep, ...]
    edits_mode: EditsMode
    focus: Focus | None
    reuse: bool
    reuse_ids: ReuseIds | None
    deck: str | None
    cost: RoundCost | None
    problems: tuple[str, ...]
    shrunk_edits: tuple[tuple[JsonObject, ...], ...] | None


def _cost_json(c: RoundCost) -> JsonObject:
    return {"phases": c.phases} if c.seconds is None else {"seconds": c.seconds, "phases": c.phases}


def record_json(r: LiveRecord) -> JsonObject:
    """round.json, in the order it has always had."""
    out: JsonObject = {"seed": r.seed, "steps": [live_step_json(s) for s in r.steps], "edits_mode": r.edits_mode,
                       "focus": r.focus, "reuse": r.reuse}
    if r.reuse_ids is not None:
        out["reuse_ids"] = {"named": r.reuse_ids.named, "missing": r.reuse_ids.missing}
    if r.deck is not None:
        out["deck"] = r.deck
    if r.cost is not None and r.cost.seconds is not None:
        out["cost"] = _cost_json(r.cost)
    out["problems"] = W.jstrs(r.problems)
    if r.cost is not None and r.cost.seconds is None:
        out["cost"] = _cost_json(r.cost)
    if r.shrunk_edits is not None:
        out["shrunk_edits"] = [W.jlist(s) for s in r.shrunk_edits]
    return out


def live_step(v: Json, where: str) -> LiveStep:
    o = W.obj(v, where)
    layout = None if "layout" not in o else {k: as_int(n, f"{where}.layout") for k, n in W.obj(o["layout"], where).items()}
    return LiveStep(step=as_int(o["step"], f"{where}.step"), variant=W.text(o["variant"], f"{where}.variant"),
                    edits=tuple(W.objs(o["edits"], f"{where}.edits")),
                    findings=tuple(loss_oracle.finding_of(f, f"{where}.findings") for f in W.arr(o["findings"], where)),
                    cost=W.obj(o["cost"], f"{where}.cost") if "cost" in o else None, layout=layout,
                    reach=W.obj(o["reach"], f"{where}.reach") if "reach" in o else None)


def live_record(v: Json, where: str) -> LiveRecord:
    """A round.json read back (one written before rounds said their edits mode is refused)."""
    o = W.obj(v, where)
    ids = W.obj(o["reuse_ids"], where) if "reuse_ids" in o else None
    cost = W.obj(o["cost"], where) if "cost" in o else None
    reuse = o["reuse"]
    if not isinstance(reuse, bool):
        raise ValueError(f"{where}.reuse is not true or false")
    return LiveRecord(
        seed=as_int(o["seed"], f"{where}.seed"),
        steps=tuple(live_step(s, f"{where}.steps") for s in W.arr(o["steps"], where)),
        edits_mode=edits_mode_of(W.text(o["edits_mode"], f"{where}.edits_mode")),
        focus=focus_of(W.opt_text(o["focus"], f"{where}.focus")), reuse=reuse,
        reuse_ids=None if ids is None else ReuseIds(named=as_int(ids["named"], where), missing=as_int(ids["missing"], where)),
        deck=W.opt_text(o["deck"], f"{where}.deck") if "deck" in o else None,
        cost=None if cost is None else RoundCost(seconds=W.num(cost["seconds"], where) if "seconds" in cost else None,
                                                 phases=W.obj(cost["phases"], where)),
        problems=tuple(_strs(o["problems"], f"{where}.problems")), shrunk_edits=None)


class LiveRound:
    deck: deck_edits.LiveDeck                 # (made by `start_deck`)
    slide_ids: dict[str | None, str | None]   # v1 title -> slide

    def __init__(self, seed: int, out: Path, chain: int, keep_decks: bool, edits: EditsMode,
                 focus: Focus | None, template: Template | None) -> None:
        self.seed, self.out, self.chain, self.keep = seed, out, chain, keep_decks
        self.edits, self.focus, self.template = edits, focus, template
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(self.out / "fuzz.log", "w", encoding="utf-8")
        self.problems: list[str] = []
        # what round.json will say, as far as the round got
        self.steps: list[LiveStep] = []
        self.reuse_ids: ReuseIds | None = None
        self.deck_url: str | None = None
        self.round_cost: RoundCost | None = None
        self.loose: set[str] = set()  # slides whose groups the person took apart, in any step so far
        self.cost = Cost()
        self.pres_after: JsonObject | None = None   # the last read of the deck after a sync (the next step's model)

    def record(self, problems: Sequence[str]) -> LiveRecord:
        return LiveRecord(seed=self.seed, steps=tuple(self.steps), edits_mode=self.edits, focus=self.focus,
                          reuse=self.template is not None, reuse_ids=self.reuse_ids, deck=self.deck_url,
                          cost=self.round_cost, problems=tuple(problems), shrunk_edits=None)

    def cli(self, args: Sequence[str | Path], check: bool) -> "subprocess.CompletedProcess[bytes]":
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        name = str(args[0])
        stats = self.out / f"api-{name}.json"
        stats.unlink(missing_ok=True)
        mark = self.cost.start(name)
        done = subprocess.run([sys.executable, "-m", "beamer2slides.devtools.counted", *map(str, args)],
                              env={**ENV, "B2S_API_STATS": str(stats)}, cwd=ROOT,
                              stdout=self.log, stderr=subprocess.STDOUT)
        self.cost.stop(mark, Cost.subprocess(stats), {})
        if check and done.returncode:
            raise RuntimeError(f"beamer2slides {name} failed, see {self.out / 'fuzz.log'}")
        return done

    def read(self) -> JsonObject:
        from beamer2slides.google_types import as_json
        from beamer2slides.gslides import execute
        return as_json(execute(self.deck.api.presentations().get(presentationId=self.deck.pid)), self.deck.pid)

    def snapshot(self) -> tuple[JsonObject, JsonObject]:
        from beamer2slides.deck_pictures import WORKERS
        from beamer2slides.google_types import presentation
        pres = self.read()
        typed = presentation(pres, self.deck.pid)
        read = snapshot.read_presentation(typed)
        snapshot.sign_pictures(read, typed, None, None, WORKERS, None, None, None, None, None)
        return pres, read

    def start_deck(self, build: SyncBuild) -> None:
        """The round's v1 deck: converted, or copied from the template (`--reuse`)."""
        if self.template is None:
            self.cli(["convert", build.build(start_variant(self.focus)), "--out", self.out], True)
            pid = None
        else:
            mark = self.cost.start("copy")
            pid = self.template.copy_into(self.out, f"b2s fuzz {self.out.parent.name}/{self.out.name}")
            self.cost.stop(mark, {}, {})
        pid = pid or _presentation_id(self.out)
        mark = self.cost.start("edits")
        self.deck = deck_edits.open_deck(pid, defer=self.edits == "batched")
        self.cost.stop(mark, {}, {"reads": 1})
        base = _load(self.out / "sync" / "base.json")
        slides = W.objs(base["slides"], "base.slides")
        self.slide_ids = {W.opt_text(s.get("title"), "slide.title"): W.opt_text(s.get("objectId"), "slide.objectId")
                          for s in slides}
        if self.template is not None:
            # The copy is only a converted deck if every object the base names is in it by that id.
            have = {s.id for s in self.deck.model.slides} | {e.id for s in self.deck.model.slides for e in s.elements}
            named = {W.text(s["objectId"], "slide.objectId") for s in slides} | {
                o for s in slides for e in W.objs(s.get("elements") or [], "slide.elements")
                for o in _strs(e.get("objects") or [], "element.objects")}
            missing = sorted(named - have)
            self.reuse_ids = ReuseIds(named=len(named), missing=len(missing))
            if missing:
                raise RuntimeError(f"the copy of the template lacks {len(missing)} ids the base names: {missing[:5]}")
        self.deck_url = f"https://docs.google.com/presentation/d/{self.deck.pid}/edit"

    def run(self, specs: Sequence[Sequence[JsonObject]] | None, replay_variants: Sequence[str] | None) -> list[str]:
        build = sync_build()
        rng = random.Random(self.seed)
        weights = variant_weights(build, self.focus)
        variants = sorted(weights)
        self.clear_previous()
        started = time.monotonic()
        self.start_deck(build)
        for step in range(self.chain):
            self.cost.step = {}
            # A focused round knows its source before the person edits, so the edits can go where
            # the source will change things; the default draws it after, as it always has.
            drawn = rng.choices(variants, [weights[v] for v in variants])[0] if self.focus else None
            aim: set[str] = set() if drawn is None else {
                oid for t in variant_slides(build, drawn) if (oid := self.slide_ids.get(t)) is not None}
            mark = self.cost.start("edits")
            reads, writes = self.deck.reads, self.deck.writes
            done = self.edit_step(rng, specs[step] if specs is not None and step < len(specs) else None, aim)
            self.cost.stop(mark, {}, {"reads": self.deck.reads - reads, "writes": self.deck.writes - writes,
                                      "edits": len(done)})
            variant = drawn or rng.choice(variants)
            if replay_variants and step < len(replay_variants):
                variant = replay_variants[step]   # (a replay's edits drew nothing: the dice moved on)
            self.step(step, done, variant, build)
        self.round_cost = RoundCost(seconds=round(time.monotonic() - started, 1), phases=Cost.plain(self.cost.phases))
        return self.problems

    def edit_step(self, rng: random.Random, replay: Sequence[JsonObject] | None, aim: Collection[str]) -> list[JsonObject]:
        """The person's edits of one step: replayed, or drawn (`random_spec`) and applied.

        `edits="reread"` is the way it always was: the deck read at the start, an edit sent as its
        own batchUpdate, the deck read again after each. `"batched"` (`LiveDeck(defer=True)`) reads
        once - the read the last sync step already made, when there is one - queues the edits, and
        reads again only when a drawn edit falls on a slide a queued one has touched (it is then
        drawn again from the new read: every edit is found in a read that is current for its slide,
        which is what the reread way gives too). A step is one or two batchUpdates instead of eight."""
        deck = self.deck
        if deck.defer and self.pres_after is not None:
            deck.adopt(self.pres_after)
        elif deck.defer and deck.pending:
            deck.flush()
        else:
            deck.read()
        try:
            donor: str | None = deck_edits.donor_from(deck.model.presentation, deck.pid)
        except Exception:  # noqa: BLE001 (a deck with no picture left to borrow)
            donor = None
        done: list[JsonObject] = []
        queued: list[JsonObject] = []

        def flush() -> None:
            refused = set(deck.flush())
            for i, spec in enumerate(queued):
                if i in refused:
                    self.log.write(f"refused {json.dumps(spec)}\n")
                else:
                    done.append(spec)
            queued.clear()

        def attempt(spec: JsonObject, what: str) -> None:
            n = len(deck.pending)
            try:
                deck_edits.apply(deck, spec)
            except Exception as e:  # noqa: BLE001 (an edit that doesn't fit this deck)
                self.log.write(f"{what} {json.dumps(spec)}: {e}\n")
                if not deck.defer:
                    deck.read()
                return
            if deck.defer:
                queued.extend([spec] * (len(deck.pending) - n))
            else:
                done.append(spec)

        if replay is not None:
            for spec in replay:
                if stale(deck, spec):
                    flush()
                    deck.read()
                attempt(spec, "replay skipped")
        else:
            wanted = rng.randint(MIN_EDITS, MAX_EDITS)
            for _ in range(wanted * 4):
                if len(done) + len(queued) >= wanted:
                    break
                drawn = random_spec(deck.model, rng, donor, self.focus, aim)
                if drawn is not None and stale(deck, drawn):
                    flush()
                    deck.read()
                    drawn = random_spec(deck.model, rng, donor, self.focus, aim)
                if drawn is not None:
                    attempt(drawn, "skipped")
        flush()
        return done

    def step(self, step: int, specs: Sequence[JsonObject], variant: str, build: SyncBuild) -> None:
        from . import layout_oracle
        folder = self.out / f"step{step}"
        folder.mkdir(exist_ok=True)
        base = _load(self.out / "sync" / "base.json")
        (folder / "base.json").write_text(json.dumps(base), encoding="utf-8")
        mark = self.cost.start("snapshot")
        pres_before, before = self.snapshot()
        self.cost.stop(mark, {}, {})
        (folder / "before.json").write_text(json.dumps(before), encoding="utf-8")
        (folder / "edits.json").write_text(json.dumps(list(specs), indent=1, ensure_ascii=False), encoding="utf-8")
        mark = self.cost.start("build")
        pdf = build.build(variant)
        self.cost.stop(mark, {}, {})
        self.cli(["sync", pdf, "--deck", self.out], True)
        report = _load(self.out / "sync" / "sync-report.json")
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        mark = self.cost.start("snapshot")
        pres_after, after = self.snapshot()
        self.cost.stop(mark, {}, {})
        (folder / "after.json").write_text(json.dumps(after), encoding="utf-8")
        judged = self.cost.start("judge")
        ours = self.ours(pdf, base, folder)
        # (parsed after `ours`: rebuilding the new conversion gives the base the keys it matched)
        found = loss_oracle.check_of(sync_model.base(base), sync_model.deck_read(before), sync_model.deck_read(after),
                                     loss_oracle.report_of(report), loss_oracle.ours_of(ours))
        (folder / "findings.json").write_text(json.dumps([loss_oracle.finding_json(f) for f in found], indent=1,
                                                         ensure_ascii=False), encoding="utf-8")
        failing = loss_oracle.failing(found)
        self.steps.append(LiveStep(step=step, variant=variant, edits=tuple(specs), findings=tuple(failing),
                                   cost=None, layout=None, reach=None))
        self.problems += [f"step {step} ({variant}): {loss_oracle.described([f])}" for f in failing]
        layout = layout_oracle.check(base, before, after, report, ours)
        (folder / "layout.json").write_text(json.dumps(layout, indent=1, ensure_ascii=False), encoding="utf-8")
        self.problems += [f"step {step} ({variant}): layout: {layout_oracle.describe([f]).strip()}"
                          for f in layout_oracle.failures(layout)]
        self.problems += [f"step {step} ({variant}): integrity: {p}" for p in self.integrity(pres_before, pres_after, specs)]
        self.cost.stop(judged, {}, {})
        self.after_step(base, before, after, report, pres_after, folder)

    def after_step(self, base: JsonObject, before: JsonObject, after: JsonObject, report: JsonObject,
                   pres_after: JsonObject, folder: Path) -> None:
        """What a step cost, which layout preconditions it reached (`fuzz_reach`) and what the layout
        oracle said of it (`layout.json`, written by the judging lines above: kind -> severity
        counts), onto the step record; and the read after the sync kept as the next step's model.
        The reach is the proxy, the oracle the verdict: a campaign whose reach is high and whose
        findings stay at zero is one whose oracle to question next."""
        from . import fuzz_reach
        rec = replace(self.steps[-1], cost=Cost.plain(self.cost.step or {}))
        self.steps[-1] = rec
        try:
            layout: Json = json.loads((folder / "layout.json").read_text(encoding="utf-8"))
            counts: dict[str, int] = {}
            for f in W.objs(layout, "layout.json"):
                key = f"{f['kind']}:{f['severity']}"
                counts[key] = counts.get(key, 0) + 1
            rec = replace(rec, layout=counts)
            self.steps[-1] = rec
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            rec = replace(rec, reach=fuzz_reach.reach_json(fuzz_reach.step_reach(base, before, after, report)))
            self.steps[-1] = rec
        except Exception as e:  # noqa: BLE001 (a proxy must never fail a round)
            self.log.write(f"reach: {type(e).__name__}: {e}\n")
        self.pres_after = pres_after

    def ours(self, pdf: Path, base: JsonObject, folder: Path) -> JsonObject | None:
        """The new conversion the sync used, rebuilt offline (no Google call) so the oracle can tell
        a word the source rewrote from a word that vanished."""
        from beamer2slides.emit import SLIDE_W
        from beamer2slides.sync import build_ours
        try:
            with PDFIUM:
                return build_ours(pdf, folder / "ours", base, "last", SLIDE_W, snapshot.NO_PICTURES)
        except Exception as e:  # noqa: BLE001
            self.log.write(f"could not rebuild ours: {e}\n")
            return None

    def integrity(self, pres_before: JsonObject, pres_after: JsonObject, specs: Sequence[JsonObject]) -> list[str]:
        base = _load(self.out / "sync" / "base.json")
        # A group the person took apart (or a member they deleted) is theirs: the sync rebuilding
        # the unit ungrouped is the policy, not a broken deck. It stays theirs for the rest of the
        # chain - a later step must not be accused of the group step 0 dissolved - so the slides
        # add up over the steps. The slide is remembered by its objectId as well as by the title the
        # edit named it with, because the sync of this very step may retitle it: seed 607 ungrouped
        # a figure on "Why decks and sources diverge" and synced `retitle`, which calls that frame
        # "Why decks drift away from their source", and the excuse missed the slide it was written for.
        titles = {t for spec in specs
                  if spec["edit"] in ("ungroup", "group", "delete_element", "delete_group", "duplicate")
                  if (t := _slide_title(spec)) is not None}
        self.loose |= titles | {s.id for s in sc.Model(pres_before).slides if s.title in titles}
        # Ctrl+D on a slide copies it as the person left it, so the copy of a slide whose group they
        # took apart is ungrouped by their hand too, and the excuse written for the original has to
        # follow it (live seed 900, step 4: they ungrouped a figure on "Why decks and sources
        # divergeed" and then duplicated that very slide twice; "Copy 807" - a slide sync never
        # writes a request to - was accused of it at every step after). The copy is named by the
        # title the edit gave it, which is the person's, not the source's.
        self.loose |= {t for spec in specs
                       if spec["edit"] == "duplicate_slide" and _slide_title(spec) in self.loose
                       if (t := W.opt_text(W.obj(spec["args"], "spec.args").get("new_title"), "new_title")) is not None}
        return sc.integrity(sc.Model(pres_after), before=sc.Model(pres_before), base_ids=sc.ids_in(base),
                            allow_ungrouped=self.loose, allow_groups_changed=self.loose)

    def drop_deck(self, pid: str | None) -> None:
        drop_deck(pid or self.deck.pid, self.log)

    def clear_previous(self) -> None:
        """A failing round keeps its folder, and the deck in Drive that goes with it. Running that
        seed again - which is the first thing one does after a finding - would then convert onto a
        deck this harness itself has edited, and `convert` rightly refuses to rebuild over somebody's
        edits. So a round starts from nothing: the deck the last run of this seed made goes first,
        then the folder it wrote (the log stays: it is open, and it is this run's)."""
        emit = self.out / "emit.json"
        if emit.exists():
            pid = None
            try:
                pid = _load(emit).get("presentationId")
            except Exception as e:  # noqa: BLE001
                self.log.write(f"could not read the last run's deck id: {e}\n")
            if isinstance(pid, str) and pid:
                self.log.write(f"dropping the deck the last run of this seed left behind: {pid}\n")
                self.drop_deck(pid)
        for p in self.out.iterdir():
            if p.name == "fuzz.log":
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _write_round(r: LiveRound, record: LiveRecord) -> None:
    (r.out / "round.json").write_text(json.dumps(record_json(record), indent=1, ensure_ascii=False), encoding="utf-8")


def live_round(seed: int, out_root: Path, chain: int, keep_decks: bool, specs: Sequence[Sequence[JsonObject]] | None,
               variants: Sequence[str] | None, edits: EditsMode, focus: Focus | None,
               template: Template | None) -> LiveRecord:
    """One live round. A replay passes the recorded `specs` and `variants` (without them the
    replayed steps would draw other variants)."""
    r = LiveRound(seed, out_root / f"r{seed:03}", chain, keep_decks, edits, focus, template)
    try:
        problems = r.run(specs, variants)
        record = r.record(problems)
        _write_round(r, record)
        if not problems and not keep_decks:
            r.drop_deck(None)
            record = replace(record, deck="(deleted: the round passed)")
        return record
    except Exception as e:  # noqa: BLE001
        r.log.flush()
        why = known_cause(r.out / "fuzz.log")
        if r.round_cost is None:
            r.round_cost = RoundCost(seconds=None, phases=Cost.plain(r.cost.phases))
        record = r.record([f"{type(e).__name__}: {e}" + (f" [{why}]" if why else "")])
        _write_round(r, record)
        return record
    finally:
        r.log.close()


def shrink_live(record: LiveRecord, out_root: Path, chain: int, edits: EditsMode, focus: Focus | None,
                template: Template | None) -> LiveRecord:
    """Replay the round with one edit dropped at a time (the last failing step only)."""
    steps = [list(s.edits) for s in record.steps]
    failing = next((i for i, s in enumerate(record.steps) if s.findings), None)
    if failing is None:
        return record
    steps = steps[:failing + 1]
    best = record
    i = 0
    while i < len(steps[failing]):
        trial = [list(x) for x in steps]
        dropped = trial[failing].pop(i)
        got = live_round(record.seed, out_root / "shrink", failing + 1, True, trial,
                         [s.variant for s in record.steps], edits, focus, template)
        if any(s.findings for s in got.steps):
            steps, best = trial, got
            print(f"  shrink: dropping {dropped['edit']} keeps it failing ({len(steps[failing])} edits left)")
        else:
            i += 1
    return replace(best, shrunk_edits=tuple(tuple(s) for s in steps))


WRITE = "call slides.presentations.batchUpdate"   # what the per-user write quota counts


def summary(records: Sequence[LiveRecord], wall: float, parallel: int) -> str:
    """What the run cost and reached: rounds/hour at this --parallel, seconds per step, each
    phase's seconds per step, Google calls, retries and backoff (all threads and subprocesses),
    and how many steps reached each layout precondition (devtools/fuzz_reach.py)."""
    phases: dict[str, Counts] = {}
    for r in records:
        if r.cost is None:
            continue
        for name, c in r.cost.phases.items():
            _add(phases.setdefault(name, {}), {f: W.num(v, f) for f, v in W.obj(c, name).items()})
    steps = sum(len(r.steps) for r in records) or 1
    total: Counts = {}
    for c in phases.values():
        _add(total, {k: c.get(k, 0) for k in ("calls", "retries", "backoff_s", "rate_limited", WRITE)})
    busy = sum((r.cost.seconds or 0) if r.cost is not None else 0 for r in records)
    order = ("convert", "copy", "edits", "snapshot", "build", "sync", "judge")
    per = "  ".join(f"{k} {phases[k].get('s', 0) / steps:.1f}" for k in order if k in phases)
    reach = Counter(p for r in records for s in r.steps for p in (s.reach or {}))
    layout = Counter(k for r in records for s in r.steps for k in (s.layout or {}))
    from .fuzz_reach import PRECONDITIONS

    def n(k: str) -> float:
        return total.get(k, 0)
    return (f"cost: {len(records)} rounds, {steps} steps in {wall:.0f} s at --parallel {parallel}: "
            f"{3600 * len(records) / max(wall, 1):.1f} rounds/h, {busy / steps:.1f} s per step (one round's clock)\n"
            f"  s per step by phase: {per}\n"
            f"  Google: {n('calls'):.0f} calls ({n('calls') / steps:.0f}/step), {n('retries'):.0f} retries, "
            f"{n('rate_limited'):.0f} rate-limited, {n('backoff_s'):.0f} s backing off, "
            f"{60 * n(WRITE) / max(wall, 1):.0f} batchUpdates/min\n"
            f"  reach (steps): " + ", ".join(f"{p} {reach[p]}" for p in PRECONDITIONS) + "\n"
            "  layout oracle (steps with a finding): " + (", ".join(f"{k} {c}" for k, c in sorted(layout.items())) or "none"))


def run_live(rounds: int, seed0: int, parallel: int, chain: int, keep_decks: bool, out_root: Path, shrink: bool,
             edits: EditsMode, focus: Focus | None, reuse: bool) -> list[LiveRecord]:
    out_root.mkdir(parents=True, exist_ok=True)
    seeds = list(range(seed0, seed0 + rounds))
    started = time.monotonic()
    template = None
    if reuse:
        with open(out_root / "template.log", "w", encoding="utf-8") as log:
            template = Template(out_root / "_template", log).make(sync_build(), start_variant(focus))

    def one(seed: int) -> LiveRecord:
        return live_round(seed, out_root, chain, keep_decks, None, None, edits, focus, template)
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            records = list(pool.map(one, seeds))
        wall = time.monotonic() - started
        bad = [r for r in records if r.problems]
        for r in bad:
            print(f"\nseed {r.seed}: {len(r.problems)} problem(s), {r.deck}")
            for p in r.problems:
                print(f"  {p}")
            if shrink and r.steps:
                shrink_live(r, out_root, chain, edits, focus, template)
    finally:
        if template is not None and not keep_decks:
            drop_deck(template.pid, sys.stderr)
    print(f"\n{len(records) - len(bad)}/{len(records)} live rounds clean")
    line = summary(records, wall, parallel)
    print(line)
    (out_root / "summary.json").write_text(json.dumps({"rounds": len(records), "wall": round(wall, 1), "parallel": parallel,
                                                        "chain": chain, "edits": edits, "focus": focus, "reuse": reuse,
                                                        "seeds": seeds, "summary": line}, indent=1), encoding="utf-8")
    return bad


def _recorded(folder: Path) -> tuple[list[list[JsonObject]] | None, list[str] | None]:
    """The edits and variants round.json recorded, per step (a replay's), or nothing."""
    if not (folder / "round.json").exists():
        return None, None
    steps = W.objs(_load(folder / "round.json")["steps"], "round.json steps")
    return [W.objs(s["edits"], "step.edits") for s in steps], [W.text(s["variant"], "step.variant") for s in steps]


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["offline", "live"])
    ap.add_argument("--rounds", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0, help="the first seed")
    ap.add_argument("--replay", type=int, help="run this one seed and print everything")
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--chain", type=int, default=1, help="how many edit+sync steps per round")
    ap.add_argument("--shape", choices=sorted(W.SHAPES), default="converted",
                    help="offline: which world the deck is drawn from (adopt = a foreign deck adopt took over)")
    ap.add_argument("--first-sync", action="store_true",
                    help="offline: the base is the one `adopt` recorded, not the one `convert` wrote - the "
                         "deck has never been written to, its objects are a person's, and some of them are "
                         "not paired with the source (implies --shape adopt)")
    ap.add_argument("--keep-decks", action="store_true", help="live: don't delete the decks of passing rounds")
    ap.add_argument("--edits", choices=list(EDITS_MODES), default="batched",
                    help="live: queue a step's edits into one batchUpdate and read only when an edit falls on a "
                         "slide a queued one touched (batched), or send each edit and read the deck after it "
                         "(reread, the old way)")
    ap.add_argument("--focus", choices=sorted(FOCUS),
                    help="live: draw edits aimed at layout preconditions, on the slides the step's variant "
                         "changes, and variants by how much they re-lay (default: uniform, as always); "
                         "probes: the same aims on rounds that start from the layout probe frames and sync "
                         "probes-reword / probes-push")
    ap.add_argument("--reuse", action="store_true",
                    help="live: convert v1 once and give each round a Drive copy of that deck (and its base)")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("B2S_FUZZ_OUT", ROOT / "out" / "sync-fuzz")))
    args = ap.parse_args()
    mode: str = args.mode
    rounds: int = args.rounds
    seed: int = args.seed
    replay: int | None = args.replay
    no_shrink: bool = args.no_shrink
    chain: int = args.chain
    first_sync: bool = args.first_sync
    out: Path = args.out
    edits = edits_mode_of(args.edits)
    focus = focus_of(args.focus)

    shape: W.Shape = "adopt" if first_sync else W.shape_of(args.shape)
    if mode == "offline":
        if replay is not None:
            result = offline_chain(replay, chain, None, None, shape, first_sync)
            print(describe_chain(result))
            for s in result.steps:
                if s.message:
                    print(s.message)
            print(loss_oracle.described([f for s in result.steps for f in s.findings]) or "nothing lost")
            return 1 if result.failures else 0
        started = time.monotonic()
        bad = run_offline(rounds, seed, not no_shrink, False, chain, shape, first_sync)
        print(f"{rounds - len(bad)}/{rounds} offline rounds clean in {time.monotonic() - started:.1f} s")
        return 1 if bad else 0

    # A live campaign runs unattended: a token that can no longer be refreshed stops it here, not in
    # a browser tab per round (`counted.never_interactive`; its subprocesses run under it too).
    from .counted import NeedsConsent, never_interactive, quiet_credentials
    try:
        quiet_credentials()
    except NeedsConsent as e:
        print(f"live fuzzing needs Google: {e}", file=sys.stderr)
        return 2
    never_interactive()
    if replay is not None:
        specs, variants = _recorded(out / f"r{replay:03}")
        record = live_round(replay, out, chain, True, specs, variants, edits, focus, None)
        print(json.dumps(W.jstrs(record.problems), indent=1))
        return 1 if record.problems else 0
    bad = run_live(rounds, seed, args.parallel, chain, args.keep_decks, out, not no_shrink, edits, focus, args.reuse)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
