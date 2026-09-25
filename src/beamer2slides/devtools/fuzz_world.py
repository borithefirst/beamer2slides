"""A synthetic deck world for the offline sync fuzz (tools/fuzz_sync.py).

Builds the three sides `merge.plan_merge` needs - a base (converter output plus Google's read-back),
`ours` (a new conversion of a changed source) and `theirs` (the live deck after a person's edits) -
from a small made-up document, and applies a merge plan the way a correct sync would write it
(`apply_plan`). That applier is the *reference semantics* of docs/sync.md, not a copy of sync.py:
the offline fuzz compares what merge planned against what a faithful writer would produce, and
tools/loss_oracle.py then judges the result.

Documents are dicts:
    {"slides": [{"page", "label", "title", "notes", "bg", "elements": [IR, ...]}]}
Element IR is the converter's (classify) shape, cut down to what identity and merge look at.
"""

import copy
import json
import random
import re
from difflib import SequenceMatcher
from pathlib import Path

from beamer2slides import identity, merge, snapshot, sync

SCALE = 2.0  # deck pt per PDF pt (like a 16:9 deck of a 360 pt wide PDF)
WORDS = ("slides", "source", "merge", "author", "deck", "editor", "figure", "policy", "review", "export",
         "wording", "layout", "picture", "notes", "colleague", "revision", "timing", "table", "bullet", "frame")


# ---------------------------------------------------------------- IR

def run(text, **kw):
    return {"text": text, "font": "CMSS10", "family": "sans", "size": 10.0, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
            "highlight": None, **kw}


def text_ir(eid, text, bbox, role="body", **kw):
    lines = text.split("\n")
    return {"id": eid, "kind": "text", "role": role, "bbox": [float(v) for v in bbox], "panel": None, "code": False,
            "spans": [], "strokes": [], "paragraphs": [
                {"align": "left", "level": 0, "bullet": None, "size": 10.0, "text_x0": bbox[0], "tab_x0": None,
                 "lines": [{"baseline": bbox[1] + 8 + 12 * k, "x0": bbox[0], "x1": bbox[2]}], "wrap_limit": None,
                 "runs": [run(line, **kw)]} for k, line in enumerate(lines)]}


def image_ir(eid, bbox, file, anchor=None, role="figure"):
    el = {"id": eid, "kind": "image", "role": role, "bbox": [float(v) for v in bbox], "file": file}
    if anchor:
        el["anchor"] = anchor
    return el


def shape_ir(eid, bbox, fill="#dddddd", block=None):
    el = {"id": eid, "kind": "shape", "role": "panel", "bbox": [float(v) for v in bbox], "shape": "RECTANGLE",
          "fill": fill, "flip": False, "radius": 0.0}
    if block is not None:
        el["block"] = block
    return el


def table_ir(eid, bbox, rows):
    return {"id": eid, "kind": "table", "role": "table", "bbox": [float(v) for v in bbox], "borders": [],
            "cells": [[[run(c)] for c in row] for row in rows]}


def table_text(el):
    return "\n".join("\t".join(identity.run_text(c).strip() for c in row) for row in el["cells"])


def element_text(el):
    if el["kind"] == "text":
        return merge.predicted_text(el)
    if el["kind"] == "table":
        return table_text(el)
    return None


# ---------------------------------------------------------------- documents

def make_doc(rng: random.Random, out: Path) -> dict:
    """A small talk: a title slide and 2-5 content slides with text, a figure, a block, a table and
    an anchored formula picture."""
    n = rng.randint(3, 6)
    slides = []
    for i in range(n):
        title = f"{rng.choice(WORDS).title()} {rng.choice(WORDS)} {i}"
        els = [text_ir(f"p{i}t0", title, (20, 20, 20 + 6 * len(title), 34), role="title")]
        y = 60
        for k in range(rng.randint(1, 3)):
            lines = [" ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 7))) for _ in range(rng.randint(1, 3))]
            els.append(text_ir(f"p{i}t{k + 1}", "\n".join(lines), (25, y, 200, y + 12 * len(lines))))
            y += 12 * len(lines) + 8
        if rng.random() < 0.5:
            path = out / f"fig{i}.bin"
            path.write_bytes(bytes([(i * 37 + j) % 251 for j in range(64)]))
            els.append(image_ir(f"p{i}i0", (220, 60, 320, 140), f"fig{i}.bin"))
        if rng.random() < 0.35:
            els.append(shape_ir(f"p{i}s0", (25, y, 200, y + 30)))
            y += 38
        brng = random.Random(f"{title}/block")  # (its own draws: every older seed keeps its deck)
        if brng.random() < 0.35:
            # a beamer block: an opaque panel and the body text on it, which emit groups
            # (`block_groups`) - the panel first, so under the text
            body = " ".join(brng.choice(WORDS) for _ in range(brng.randint(3, 6)))
            els.append(shape_ir(f"p{i}k0", (25, y, 200, y + 28), fill="#dde4f0", block=0))
            els.append({**text_ir(f"p{i}k1", body, (30, y + 8, 195, y + 20)), "block": 0})
            y += 36
        if rng.random() < 0.35:
            rows = [[rng.choice(WORDS) for _ in range(2)] for _ in range(2)]
            els.append(table_ir(f"p{i}b0", (25, y, 180, y + 30), rows))
            y += 38
        if rng.random() < 0.35:
            anchor = els[1]["id"]
            path = out / f"math{i}.bin"
            path.write_bytes(bytes([(i * 91 + j) % 251 for j in range(48)]))
            els.append(image_ir(f"p{i}m0", (120, 62, 140, 74), f"math{i}.bin", anchor=anchor, role="math"))
        slides.append({"page": i, "label": f"f{i}" if rng.random() < 0.6 else None, "title": title,
                       "notes": " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 6))), "bg": "#ffffff",
                       "elements": els})
    return {"slides": slides}


# What a template deck says over and over: chrome, section headings, calls to action. Short, and
# most of it on more than one slide - which is what makes an adopt-shaped deck hard to key.
ADOPT_PHRASES = ("Agenda", "Thank you", "Questions?", "Get started", "What we shipped", "Roadmap",
                 "Next steps", "Demo", "Resources", "Build with us", "Why it matters", "In short")


def _box(eid, x, y, w, words, role="body"):
    """One of the small boxes an adopted slide is made of (a `textblock*` per element)."""
    return text_ir(eid, words, (x, y, x + w, y + 12), role=role)


def make_adopt_doc(rng: random.Random, out: Path) -> dict:
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
    pool = []
    for k in range(2):
        path = out / f"fig-adopt{k}.bin"
        path.write_bytes(bytes([(k * 71 + j) % 251 for j in range(64)]))
        pool.append(path.name)
    slides: list[dict] = []
    for i in range(n):
        els: list[dict] = []
        if rng.random() < 0.45:                       # the layout's title placeholder, when it has one
            els.append(_box(f"p{i}e0", 40, 24, 300, rng.choice(ADOPT_PHRASES), role="title"))
        # the deck's chrome: the same words at the same place on every slide
        els.append(_box(f"p{i}e1", 20, 190, 120, chrome, role="footer"))
        els.append(image_ir(f"p{i}e2", (300, 186, 330, 198), pool[0], role="figure"))
        y = 52
        for k in range(rng.randint(4, 9)):
            words = rng.choice(ADOPT_PHRASES) if rng.random() < 0.4 else \
                " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4)))
            x = 30 + 150 * (k % 2)
            els.append(_box(f"p{i}e{k + 3}", x, y, 130, words))
            y += 16 if k % 2 else 0
        if rng.random() < 0.4:
            # An icon at the head of a line: a picture the converter made while reading one of the
            # person's own *text boxes* back, so it has no object of its own and never will - the
            # box it came out of is the one beside it (`adopt_sync.drawn_from`). Anchored, so it is
            # a member of that box's unit, which is what lets the unit be written at all
            # (`merge.covered`); without it the person's box could never be edited from the source.
            host = els[-1]
            hx, hy = host["bbox"][0], host["bbox"][1]
            els.append(image_ir(f"p{i}e94", (hx + 2, hy + 2, hx + 10, hy + 10), pool[0],
                                anchor=host["id"], role="icon"))
        if rng.random() < 0.4:                        # a tikz cluster the deck groups
            els.append(shape_ir(f"p{i}g0", (200, 40, 300, 70), fill="#e8eaed", block=0))
            els.append({**_box(f"p{i}g1", 206, 48, 88, rng.choice(ADOPT_PHRASES)), "block": 0})
            els.append(shape_ir(f"p{i}g2", (200, 78, 300, 100), fill="#fce8e6", block=0))
        if rng.random() < 0.3:
            els.append(image_ir(f"p{i}e90", (210, 110, 300, 170), pool[1], role="figure"))
        if rng.random() < 0.3:
            rows = [[rng.choice(WORDS) for _ in range(3)] for _ in range(2)]
            els.append(table_ir(f"p{i}b0", (30, 120, 180, 160), rows))
        slides.append({"page": i, "label": f"g{i}-0-{rng.randrange(10, 99)}", "title": title_of({"elements": els}),
                       "notes": "", "bg": "#ffffff", "elements": els})
        # A template deck's neighbours differ by a line. `align_slides` pairs on the words, so two
        # slides this alike are what makes a reorder, an insertion or a moved label ambiguous.
        if i and rng.random() < 0.3:
            twin = copy.deepcopy(slides[-2])
            body = [e for e in twin["elements"] if e["kind"] == "text" and e.get("role") == "body"]
            if body:
                body[-1]["paragraphs"][0]["runs"] = [run("and one line of its own")]
            twin["elements"] = [{**e, "id": e["id"].replace(f"p{i - 1}", f"p{i}", 1)} for e in twin["elements"]]
            for e in twin["elements"]:
                if e.get("anchor"):
                    e["anchor"] = e["anchor"].replace(f"p{i - 1}", f"p{i}", 1)
            slides[-1] = {**twin, "page": i, "label": slides[-1]["label"]}
    return {"slides": slides}


SHAPES = {"converted": make_doc, "adopt": make_adopt_doc}


def make(shape: str, rng: random.Random, out: Path) -> dict:
    return SHAPES[shape](rng, out)


def title_of(s) -> str:
    """The slide's title as `identity` finds it: the text element whose role says so, and nothing
    else. A converted talk has one on top of every slide, so "the first element" used to be the
    same answer; an adopt-shaped deck is a page of boxes with no title at all, and reading the
    topmost of them as one would hand `align_slides` a title nothing in the deck ever showed."""
    return identity.slide_title(s)


def title_element(s):
    return next((e for e in s["elements"] if e["kind"] == "text" and e.get("role") == "title"), None)


def slide_info(s):
    return {"label": s.get("label"), "title": title_of(s),
            "text": " ".join(identity.plain_text(e) for e in s["elements"]), "page": s["page"]}


# ---------------------------------------------------------------- base, ours, theirs

def picture_signature(data: bytes) -> str:
    return "100x50:" + bytes([(data[0] * 97 + i * 13) % 256 for i in range(1024)]).hex()


def panel_fill(el) -> dict | None:
    """A shape element's fill as `snapshot.shape_style` reads it (text boxes have none: emit's
    text boxes are transparent, so only panels can hide anything)."""
    return {"color": el["fill"], "alpha": 1.0} if el.get("kind") == "shape" and el.get("fill") else None


def readback(kind, box, text=None, image=None, parent=None, title=None, z=0, table=None, fill=None):
    out = {"kind": kind, "transform": [1.0, 0.0, 0.0, 1.0, round(box[0], 2), round(box[1], 2)],
           "size": [round(box[2] - box[0], 2), round(box[3] - box[1], 2)], "box": [round(v, 2) for v in box],
           "parent_group": parent, "z": z, "title": title, "description": None, "text": text,
           "text_styles": [{"fontFamily": "Lato", "fontSize": 18.0}] if text is not None else [],
           "paragraph_styles": [{"alignment": "START"}] if text is not None else [], "run_spans": [],
           "text_style_hash": "s0", "shape_style": {"fill": fill},
           "shape_style_hash": "h0"}
    if image:
        out["image"] = image
    if table:
        out["table"] = table
    return out


def style_hashes(el) -> tuple[str, str]:
    """What Google's read-back shows of an element's styling (`snapshot` hashes it): the runs'
    attributes and the shape's fill, so a restyle in the source really changes the object."""
    runs = [[r.get("color"), r.get("bold"), r.get("italic"), r.get("size"), r.get("font"), r.get("underline")]
            for p in el.get("paragraphs") or [] for r in p.get("runs") or []]
    text = identity.sha1(json.dumps(runs))[:8] if runs else "s0"
    shape = identity.sha1(json.dumps([el.get("fill"), el.get("shape")]))[:8] if el["kind"] == "shape" else "h0"
    return text, shape


def styled(rb: dict, el: dict) -> dict:
    rb["text_style_hash"], rb["shape_style_hash"] = style_hashes(el)
    return rb


def object_readback(el, oid, out: Path, parent=None, z=0):
    box = [v * SCALE for v in el["bbox"]]
    if el["kind"] == "image":
        data = (out / el["file"]).read_bytes()
        image = {"contentHash": identity.sha1(data)[:16], "sourceUrl": None, "signature": picture_signature(data)}
        return styled(readback("image", box, image=image, parent=parent, z=z), el)
    if el["kind"] == "table":
        rows = el["cells"]
        return styled(readback("table", box, text=table_text(el), parent=parent, z=z, table=[len(rows), len(rows[0])]), el)
    if el["kind"] == "shape":
        return styled(readback("shape", box, text="", parent=parent, z=z, fill=panel_fill(el)), el)
    return styled(readback("shape", box, text=merge.predicted_text(el), parent=parent, z=z), el)


def entries(doc, out: Path, keys, all_ekeys, all_fps):
    slides = []
    for s, key, ekeys, fps in zip(doc["slides"], keys, all_ekeys, all_fps):
        ids = {e["id"]: k for e, k in zip(s["elements"], ekeys)}
        elements = []
        for el, ek, fp in zip(s["elements"], ekeys, fps):
            anchor = ids.get(el.get("anchor"))
            h, fields = identity.ir_fields(el, out, anchor)
            entry = {"key": ek, "id": el["id"], "kind": el["kind"], "role": el.get("role"), "ir_hash": h,
                     "fields": fields, "fingerprint": fp, "anchor": anchor, "ir": el}
            if el["kind"] == "image":
                data = (out / el["file"]).read_bytes()
                entry["picture"] = {"contentHash": identity.sha1(data)[:16], "sourceUrl": None,
                                    "signature": picture_signature(data)}
            elements.append(entry)
        slides.append({"key": key, "label": s.get("label"), "title": title_of(s),
                       "page": s["page"], "text": " ".join(identity.plain_text(e) for e in s["elements"]),
                       "layout": "TITLE_ONLY", "background": f"color:{s['bg']}", "notes": s.get("notes") or "",
                       "elements": elements})
    return slides


def build_base(doc, out: Path) -> dict:
    infos = [slide_info(s) for s in doc["slides"]]
    keys = identity.slide_keys(infos)
    ekeys, fps = zip(*[identity.slide_element_keys(s["elements"], out) for s in doc["slides"]])
    slides = entries(doc, out, keys, list(ekeys), list(fps))
    for n, (s, entry) in enumerate(zip(doc["slides"], slides)):
        sid = f"b2s_s{n:03}"
        by_id = {e["id"]: e for e in s["elements"]}
        anchored = {e.get("anchor") for e in s["elements"] if e.get("anchor") in by_id}
        # blocks: emit groups a block's panels with its content (`emit.block_groups`), a group no
        # element owns (the base lists it under `groups`); its children are in element order, so
        # the panel is under the text on it
        blocks: dict[str, list[str]] = {}
        for k, el in enumerate(s["elements"]):
            if el.get("block") is not None:
                blocks.setdefault(f"{sid}_blk{el['block']}", []).append(f"{sid}_e{k}")
        blocks = {g: kids for g, kids in blocks.items() if len(kids) >= 2}
        block_of = {oid: g for g, kids in blocks.items() for oid in kids}
        order, z = [], 0
        for k, (el, e) in enumerate(zip(s["elements"], entry["elements"])):
            oid = f"{sid}_e{k}"
            group = f"{sid}_e{list(by_id).index(el['anchor'])}_g" if el.get("anchor") in by_id else \
                (f"{oid}_g" if el["id"] in anchored else block_of.get(oid))
            e["objects"] = [oid] + ([group] if el["id"] in anchored else [])
            e["main"] = oid
            e["readback"] = {oid: object_readback(el, oid, out, parent=group, z=z)}
            if el["id"] in anchored:
                kids = [f"{sid}_e{j}" for j, x in enumerate(s["elements"]) if x.get("anchor") == el["id"]] + [oid]
                e["readback"][group] = {**readback("elementGroup", [v * SCALE for v in el["bbox"]], z=z),
                                        "children": kids}
                order.append(group)
            elif group is None:
                order.append(oid)
            elif group in blocks and group not in order:
                order.append(group)
            z += 1
        group_readback = {}
        for g, kids in blocks.items():
            boxes = [next(e["readback"][o] for e in entry["elements"] if o in e["readback"])["box"] for o in kids]
            group_readback[g] = {**readback("elementGroup", [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                             max(b[2] for b in boxes), max(b[3] for b in boxes)]),
                                 "children": kids}
        entry.update(objectId=sid, layoutObjectId="L", background_readback={"color": s["bg"]},
                     notes_readback=s.get("notes") or "", groups=list(blocks), order=order,
                     group_readback=group_readback)  # (fuzz world only: what `live_of` puts on the page)
    return {"version": 1, "generation": 1, "presentationId": "P", "revisionId": "r0",
            "source": {"pdf": "talk.pdf", "sha1": "x"}, "scale": SCALE, "page_size": [360.0, 202.5],
            "deck_page_size": [720.0, 405.0], "master_background": "color:#ffffff",
            "master_readback": {"color": "#ffffff"}, "slides": slides}


def build_adopt_base(doc, out: Path, rng: random.Random) -> dict:
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
    unpaired, alone = 0, []
    for n, entry in enumerate(base["slides"]):
        sid = f"gx{n:x}{h6(str(n))}"
        rename = {entry["objectId"]: sid}
        for k, el in enumerate(entry["elements"]):
            for j, oid in enumerate(el["objects"]):
                rename[oid] = f"{sid}_{k:x}{j}{h6(oid)[:3]}"
        entry["objectId"] = sid
        entry["groups"] = []
        entry["group_readback"] = {}
        # An anchored picture never pairs and leaves nothing behind either: the icon at the head of
        # a line and the picture of a formula in it were drawn out of the person's *text box*, and
        # nothing in the deck is shaped like one alone. So it has no object of its own and names the
        # member that has the box's (`adopt_sync.drawn_from`) - the one blind member a unit may be
        # written over, and only while that member really is tied to something: where the box itself
        # went unpaired below, `merge.covered` finds no object to delete and the unit stays frozen.
        keys = {el["key"] for el in entry["elements"]}
        drawn = {el["key"] for el in entry["elements"] if el.get("anchor") in keys}
        left, order = {}, []
        for el in entry["elements"]:
            oids = [rename[o] for o in el["objects"]][:1]  # one object per element: no groups
            rb = {**el["readback"][el["main"]], "parent_group": None} if oids else None
            refused = bool(oids) and rng.random() < rate   # a pairing adopt refused to make
            if el["key"] in drawn:
                oids, rb = [], None
                el["drawn_from"] = el["anchor"]
            elif refused:
                left[oids[0]] = rb                        # the person's box, standing where it is
                el["left_object"] = oids[0]
                oids, rb, unpaired = [], None, unpaired + 1
            el["readback"] = {oids[0]: rb} if oids else {}
            el["objects"], el["main"] = oids, oids[0] if oids else None
            order += oids or ([el["left_object"]] if el.get("left_object") else [])
        entry["left_readback"] = left   # (fuzz world only: what `live_of` puts on the page)
        entry["order"] = order
        if left:
            entry["left_alone"] = list(left)   # (a real base says this too: `adopt_sync.build_base`)
            alone.append({"slide": entry["key"], "objects": list(left)})
    return {**base, "generation": 0, "origin": "adopt", "master_background": None,
            "adopt": {"presentationId": base["presentationId"], "deck_page_size": base["deck_page_size"],
                      "frame_width": 720.0, "slides": len(base["slides"]), "unpaired": unpaired,
                      "left_alone": alone}}


def build_ours(doc, base, out: Path) -> dict:
    infos = [slide_info(s) for s in doc["slides"]]
    base_infos = [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "",
                   "page": b["page"], "removed": b.get("removed")}   # `align_slides`: a slide the source dropped
                  for b in base["slides"]]
    moves = identity.label_moves(base_infos, infos)
    for m in moves:
        m["slide"] = base["slides"][m["base"]]["key"]
        m["frame_is"] = base["slides"][m["frame_is"]]["key"] if m["frame_is"] is not None else None
        m["slide_is"] = infos[m["slide_is"]]["title"] if m["slide_is"] is not None else None
    # `weak_pairs` and `near_misses` are what `merge.plan_merge` turns into the two warnings about
    # identity - a pairing the words could as well have made elsewhere, and a frame nothing could
    # pair at all. Leaving them out here made the campaign judge a world where the person is never
    # told that, so a frame the alignment put on a look-alike slide read as a loss in silence when
    # the product would have named the slide and asked for a label (`identity.align_slides`).
    weak: dict[int, str] = {}
    keys, pairs = identity.inherit_slide_keys(base_infos, [b["key"] for b in base["slides"]], infos, moves, weak)
    near = identity.near_misses(base_infos, infos, pairs)
    for m in near:
        m["slide"] = base["slides"][m["base"]]["key"]
        m["title"] = infos[m["ours"]]["title"]
    ekeys, fps = [], []
    for j, s in enumerate(doc["slides"]):
        matched = identity.base_items(base["slides"][pairs[j]]["elements"]) if j in pairs else None
        k, f = identity.slide_element_keys(s["elements"], out, matched)
        ekeys.append(k)
        fps.append(f)
    return {"slides": entries(doc, out, keys, ekeys, fps), "pairs": pairs, "out": out, "label_moves": moves,
            "weak_pairs": weak, "near_misses": near}


def live_of(base) -> dict:
    slides = []
    for s in base["slides"]:
        objects = {}
        for el in s["elements"]:
            for oid, rb in el["readback"].items():
                objects[oid] = copy.deepcopy(rb)
        objects.update(copy.deepcopy(s.get("group_readback") or {}))
        # the person's own boxes an adopted base could tie to nothing: on the page, named by no
        # element, and nothing this converter writes may take them away (`build_adopt_base`)
        objects.update(copy.deepcopy(s.get("left_readback") or {}))
        slides.append({"objectId": s["objectId"], "layoutObjectId": "L",
                       "background": copy.deepcopy(s["background_readback"]), "notes": s["notes_readback"],
                       "notes_id": f"{s['objectId']}_notes", "order": list(s["order"]), "objects": objects})
    return {"presentationId": "P", "revisionId": "r0", "page_size": [720.0, 405.0], "layouts": {"L": "TITLE_ONLY"},
            "master_background": {"color": "#ffffff"}, "slides": slides}


# ---------------------------------------------------------------- the reference sync

def h6(text):
    return identity.sha1(text)[:6]


def _styling_ends(base_rb, live_rb, text) -> bool:
    """Whether the deck's run styling has anything to go back onto. `sync.style_range_requests`
    maps each styled run through the matching blocks of the live text and the text sync is about to
    write, so a run still sitting on something the source kept goes back on; a run whose characters
    are all gone is styling that simply ends. (`merge.styling_lost` decides the same thing in the
    planner, by words; this is the reference applier's own opinion of it, on purpose - but it has to
    be the mechanism's opinion. Reading it word by word had the applier throw away styling sync
    really does re-apply, and the campaign accused the merge of it: offline seed 23599 --shape
    adopt, where the person's own earlier rewording had clipped their bold to two letters inside a
    word, which is not a word and was not looked for in the new text.)"""
    base_styles = base_rb.get("text_styles") or []
    before, after = live_rb.get("text") or "", text or ""
    spans = [(s, e) for s, e, style in (live_rb.get("run_spans") or []) if style not in base_styles]
    if not spans:
        return False
    blocks = SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
    return any(not any(min(end, i + n) > max(start, i) for i, _, n in blocks) for start, end in spans)


def new_object(skey, o_el, base_el, live, overrides, tok):
    """The object a correct sync creates for one ours element (docs/sync.md: ours content, with the
    deck's overrides re-applied), and the read-back of it before they were: what the base records
    (`as_created`)."""
    ir = o_el["ir"]
    box = [v * SCALE for v in ir["bbox"]]
    text = plain = element_text(ir)
    ov = (overrides or {}).get("text")
    if ov and text is not None:
        if ov.get("table"):
            cells = merge.table_merge(ov["base"], text, ov["theirs"], ov["dims"], ov["dims"])
            text = "\n".join("\t".join(r) for r in cells[0]) if cells else ov["theirs"]
        else:
            # `sync.override_requests` merges by paragraph, not by word, and carries the
            # paragraphs a person settled for the source: the applier has to do both, or a
            # `--take-source` would look like a loss to the oracle.
            text = merge.text_merge(merge.collapse_holes(ov["base"]), merge.collapse_holes(text),
                                    merge.collapse_holes(ov["theirs"]), ov.get("take") or ())[0]
    main = base_el.get("main") if base_el else None
    parent = live["objects"].get(main, {}).get("parent_group") if main else None
    if parent not in live["objects"]:
        parent = None
    oid = f"b2s_{h6(skey)}_{h6(o_el['key'])}_{tok}"
    if ir["kind"] == "image":
        rb = readback("image", box, image=copy.deepcopy(o_el["picture"]), parent=parent)
    elif ir["kind"] == "table":
        rb = readback("table", box, text=text, parent=parent, table=[len(ir["cells"]), len(ir["cells"][0])])
    else:
        rb = readback("shape", box, text=text, parent=parent, fill=panel_fill(ir))
    rb["title"] = snapshot.tag(skey, o_el["key"])
    styled(rb, ir)
    pre = {**copy.deepcopy(rb), "text": plain}
    live_rb = live["objects"].get(main) if main else None
    base_rb = (base_el or {}).get("readback", {}).get(main, {}) if main else {}
    if live_rb and (overrides or {}).get("text_style"):  # the deck's styling, re-applied
        if (overrides["text_style"] or {}).get("ranges") and _styling_ends(base_rb, live_rb, rb.get("text")):
            pass                       # the words it was on are gone: nothing to put the styling on
        else:
            rb["text_style_hash"] = live_rb.get("text_style_hash")
            rb["text_styles"] = copy.deepcopy(live_rb.get("text_styles"))
            rb["run_spans"] = copy.deepcopy(live_rb.get("run_spans") or [])
    if live_rb and (overrides or {}).get("shape_style"):
        rb["shape_style_hash"] = live_rb.get("shape_style_hash")
        rb["shape_style"] = copy.deepcopy(live_rb.get("shape_style"))
    return oid, rb, pre


def apply_plan(base, ours, theirs, mplan, tok="2zz") -> dict:
    """The live deck as a correct sync would leave it after writing `mplan`.

    `after["as_created"]` holds, per object this sync made, its read-back before the deck's
    overrides went back on - the text as ours says it, ours' styling, the converter's box. That is
    what `rebase` records, as `sync.Sync.new_base` does from `Sync.created` (docs/sync.md: the base
    is converter output). Recorded from the final deck instead, a merged edit became the base's own:
    the person's move carried onto a recreated box read as unmoved from then on, so the next source
    change put the box back at the converter's place and nothing called that a loss (and the step
    after it, the person moving it again, got carried twice: seed 93863 at chain 4)."""
    after = copy.deepcopy(theirs)
    after["as_created"] = created = {}
    by_id = {s["objectId"]: s for s in after["slides"]}
    made = {}
    dropped = set()
    for p in mplan["slides"]:
        if p["action"] == "delete":
            dropped.add(p["objectId"])
        elif p["action"] == "create":
            o = ours["slides"][p["ours"]]
            sid = f"b2s_{h6(p['key'])}_{tok}"
            objects = {}
            for el in o["elements"]:
                oid, rb, _ = new_object(o["key"], el, None, {"objects": {}}, None, tok)
                objects[oid] = rb
            made[f"new:{p['key']}"] = {"objectId": sid, "layoutObjectId": "L",
                                       "background": {"color": o["background"].split(":", 1)[1]},
                                       "notes": o.get("notes") or "", "notes_id": f"{sid}_notes",
                                       "order": list(objects), "objects": objects}
        elif p["action"] == "update":
            _update_slide(base, ours, by_id[p["objectId"]], p, tok, created)
    order = []
    for x in mplan["order"]:
        if x.startswith("new:"):
            order.append(made[x])
        elif x in by_id and x not in dropped:
            order.append(by_id[x])
    for s in after["slides"]:  # (anything the plan forgot keeps its place at the end)
        if s["objectId"] not in dropped and s not in order:
            order.append(s)
    after["slides"] = order
    return after


def _update_slide(base, ours, live, p, tok, created=None):
    b, o = base["slides"][p["base"]], ours["slides"][p["ours"]]
    bu, ou = merge.units(b["elements"]), merge.units(o["elements"])
    # The deck's own version of the objects, read before anything is deleted: that is what the
    # overrides (the deck's box, its styling) are re-applied from.
    theirs = {"objects": dict(live["objects"])}
    deck_order = list(live.get("order") or [])   # before anything takes anything's place
    fresh: list[str] = []   # page objects that went on top because nothing of theirs held their place
    for u in p["units"]:
        action = u["action"]
        members = bu.get(u["key"], [])
        if action in ("keep", "none", "adopt", "adopt_object"):
            continue
        if action == "delete":
            for m in members:
                for oid in m.get("objects", []):
                    live["objects"].pop(oid, None)
            continue
        if action == "move":
            dx, dy = (v * SCALE for v in u["delta"])
            for m in members:
                for oid in m.get("objects", []):
                    rb = live["objects"].get(oid)
                    if rb:
                        rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
                        rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]
            continue
        parents = {m["key"]: live["objects"].get(m.get("main"), {}).get("parent_group") for m in members}
        # where each old object stands in the z-order: its replacement takes that place, in its
        # group too (what `sync.Sync.restack` and `regroup_requests` are there to achieve)
        slots = {m["key"]: m.get("main") for m in members if m.get("main") in live["objects"]}
        if action == "recreate":
            # (sync ungroups, deletes, creates and regroups under the same group ids)
            for m in members:
                for oid in m.get("objects", []):
                    if live["objects"].get(oid, {}).get("kind") != "elementGroup":
                        live["objects"].pop(oid, None)
        overrides = u.get("overrides") or {}
        base_by = {m["key"]: m for m in members}
        made = []
        for m in ou.get(u["key"], []):
            oid, rb, pre = new_object(o["key"], m, base_by.get(m["key"]), theirs, overrides if m["key"] == u["key"] else None, tok)
            parent = parents.get(m["key"])
            rb["parent_group"] = pre["parent_group"] = parent if parent in live["objects"] else None
            live["objects"][oid] = rb
            if created is not None:
                created[oid] = pre
            if not (slots.get(m["key"]) and _take_place(live, slots[m["key"]], [oid])):
                # new: on top (of its group)
                (live["objects"][rb["parent_group"]].setdefault("children", []) if rb["parent_group"]
                 else live.setdefault("order", [])).append(oid)
                if not rb["parent_group"]:
                    fresh.append(oid)
            made.append((m, rb))
        _place_unit(u, members, theirs, made, base_by)
    _page_order(live, b, o, deck_order, fresh, tok)
    _restack(live, b, o, fresh, tok)
    _regroup_order(live, b, o, tok)
    if p.get("background"):
        live["background"] = {"color": p["background"].split(":", 1)[1]}
    if p.get("notes") is not None:
        live["notes"] = p["notes"]
    _drop_lonely_groups(live)


def _page_order(live, b, o, deck_order, tok_fresh, tok):
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
    order, objects = live.get("order") or [], live["objects"]
    keys = [el["key"] for el in o["elements"]]
    rank = {k: i for i, k in enumerate(keys)}
    base_main = {el["key"]: el.get("main") for el in b["elements"]}
    # A container the base itself draws on the page - a converter group. Not `b["groups"]`, which a
    # rebase carries over even after the person took that group apart (converted seed 1500512).
    groups = set(b.get("order") or [])
    base_stand = {}
    for el in b["elements"]:
        parent = ((el.get("readback") or {}).get(el.get("main")) or {}).get("parent_group")
        base_stand[el["key"]] = parent if parent in groups else el.get("main")
    at, was = {}, {}
    for k in keys:
        made = f"b2s_{h6(o['key'])}_{h6(k)}_{tok}"
        oid = made if made in objects else base_main.get(k)
        if not oid or not base_main.get(k) or oid in tok_fresh:
            continue
        parent = (objects.get(oid) or {}).get("parent_group")
        stands = parent if parent in groups else oid
        if stands not in at:                # the first element the source draws in there speaks
            at[stands], was[stands] = k, base_stand[k]
    slots = [i for i, oid in enumerate(order) if oid in at]
    here = [order[i] for i in slots]
    if len(here) < 2 or any(was[x] not in deck_order or was[x] not in (b.get("order") or [])
                            for x in here):
        return
    if here != sorted(here, key=lambda x: b["order"].index(was[x])):
        return                                  # the person restacked: their order stands
    for i, oid in zip(slots, sorted(here, key=lambda x: rank[at[x]])):
        order[i] = oid


def _regroup_order(live, b, o, tok):
    """Inside a group a rewrite takes apart, the children that are elements of the source take the
    source's order among themselves, in the places they hold; anything else in there keeps its slot.
    This is the outcome; `sync.Sync.regroup_requests` is the mechanism (BRING_TO_FRONT each child,
    then `groupObjects`), which the campaign replays through `fuzz_sync._stacked`. Without it a
    group kept the deck's child order, which is what the last conversion drew and nobody's edit
    (Slides will not restack inside a group), and the panel the source now draws *under* a text came
    back on top of it (`loss_oracle.text_hidden`: converted seed 79045 over a text the sync wrote,
    then 670146 and adopt-shaped 660326 and 680477 over one it kept)."""
    keys = [el["key"] for el in o["elements"]]
    pos = {k: i for i, k in enumerate(keys)}
    rank = {f"b2s_{h6(o['key'])}_{h6(k)}_{tok}": i for k, i in pos.items()}
    for el in b["elements"]:                    # the deck's own objects stand for the kept elements
        for oid in el.get("objects", []):
            if el["key"] in pos:
                rank.setdefault(oid, pos[el["key"]])
    for rb in live["objects"].values():
        kids = rb.get("children") or []
        slots = [i for i, c in enumerate(kids) if c in rank]
        if len(slots) > 1:
            for slot, oid in zip(slots, sorted((kids[i] for i in slots), key=lambda c: rank[c])):
                kids[slot] = oid


def _page_element(objects: dict, oid: str) -> str:
    """The page element that carries `oid`: itself, or the outermost group it is in."""
    for _ in range(16):
        parent = objects.get(oid, {}).get("parent_group")
        if not parent or parent not in objects:
            break
        oid = parent
    return oid


def _restack(live, b, o, fresh, tok):
    """An element the source added goes where the source draws it, not on top. Slides puts every
    object it creates in front of everything, so the page order after a rewrite is nothing the
    conversion asked for; a correct sync brings it back, placing each new object after the last
    element before it in ours order (at the bottom when none of them is on the page). This is the
    outcome; the mechanism is `sync.Sync.restack`, which the offline campaign does not replay (as it
    does not replay a move's transforms - see `_place_unit`), so the rule is pinned separately by
    `test_a_created_shape_stays_under_the_text_the_source_draws_above_it` and
    `test_an_element_a_dissolved_group_frees_onto_the_page_takes_the_sources_place`. Without it the applier stacked a
    new panel over body text the conversion draws above it, and the oracle called that `text_hidden`
    - 11 of 1000 adopt-shaped rounds, all of them the harness's own doing."""
    order = live.setdefault("order", [])
    objects = live["objects"]
    fresh = [x for x in fresh if x in order]
    base_main = {el["key"]: el.get("main") for el in b["elements"]}

    def top(oid):
        return _page_element(objects, oid)

    keys = [el["key"] for el in o["elements"]]
    rank = {k: i for i, k in enumerate(keys)}
    made = {f"b2s_{h6(o['key'])}_{h6(k)}_{tok}": k for k in keys}
    stands, at_rank = {}, {}    # key -> its page element; and where the source draws each object
    for k in keys:
        oid = f"b2s_{h6(o['key'])}_{h6(k)}_{tok}"
        for x in (oid, base_main.get(k)):
            if x in objects:
                at_rank[x] = min(at_rank.get(x, rank[k]), rank[k])
        oid = oid if oid in objects else base_main.get(k)
        if oid in objects and (t := top(oid)) in order:
            stands[k] = t
    for oid in fresh:
        key = made.get(oid)
        if key is None or oid not in order:
            continue
        order.remove(oid)
        i, pos = keys.index(key), 0
        for k in reversed(keys[:i]):
            if (prev := stands.get(k)) in order and prev != oid:
                pos = order.index(prev) + 1
                break
        for k in keys[i + 1:]:          # and never above what the source draws above it
            if (nxt := stands.get(k)) in order and nxt != oid:
                pos = min(pos, order.index(nxt))
                break
        order.insert(pos, oid)
    _not_over_kept(live, at_rank, made, rank)


def _not_over_kept(live, at_rank, made, rank):
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
    order, objects = live.get("order") or [], live["objects"]
    stand_rank = {}                 # page element -> the first of them it stands for
    for oid, r in at_rank.items():
        el = _page_element(objects, oid)
        stand_rank[el] = min(stand_rank.get(el, r), r)
    for m, key in made.items():
        if m not in objects or (oid := _page_element(objects, m)) not in order:
            continue
        r = rank.get(key)
        drawn = {x for x, xr in at_rank.items()
                 if r is None or xr <= r or stand_rank.get(_page_element(objects, x), r) >= r}
        i = order.index(oid)
        for j, other in enumerate(order[:i]):
            if sync.would_hide(objects, m, other, drawn):
                order.insert(j, order.pop(i))
                break


OVERRIDDEN = ("text", "text_styles", "paragraph_styles", "run_spans", "text_style_hash", "shape_style",
              "shape_style_hash", "box", "transform", "size")   # what `sync.Sync.override_requests` writes


def rebase(base, ours, after, mplan, tok="2zz") -> dict:
    """The base a correct sync records for the next one: ours IR plus the read-back of the objects
    as it made them, before the deck's overrides went back on (docs/sync.md, "After writing";
    `apply_plan`'s `as_created`). Units kept from the deck carry their old base,
    so a chain of syncs never forgets what the person's version was."""
    now = {s["objectId"]: s for s in after["slides"]}
    entries: dict[str, dict] = {}
    sids = {}
    for p in mplan["slides"]:
        if p["action"] == "delete":
            continue
        if p["action"] in ("keep_removed", "gone"):
            sid = p.get("objectId") or f"gone:{p['key']}"
            # a frame the source dropped keeps no label: it may be on another frame tomorrow, and
            # it says so (`removed`), or it reads as well as the live slide for the frame that
            # carries its words (sync.new_base, identity.align_slides)
            entries[sid] = {**base["slides"][p["base"]], "label": None, "removed": True}
            if p["action"] == "gone":
                sids[id(p)] = sid  # it keeps its place in the source's order (see the ordering below)
                o = ours["slides"][p["ours"]]  # ... and says what the source says (sync.new_base)
                entries[sid] = {**entries[sid], "removed": False,
                                **{k: o.get(k) for k in ("label", "title", "text", "page")}}
            continue
        if p.get("held"):
            # `merge.hold_slide`: nothing was written here, so the base says exactly what it said
            # before. The source's words are not recorded as arrived, or the edit held back would
            # read as already made next time (sync.new_base).
            b = base["slides"][p["base"]]
            entries[b["objectId"]] = copy.deepcopy(b)
            sids[id(p)] = b["objectId"]
            continue
        o = ours["slides"][p["ours"]]
        sid = f"b2s_{h6(p['key'])}_{tok}" if p["action"] == "create" else p["objectId"]
        sids[id(p)] = sid
        read = now.get(sid) or {"objects": {}, "order": [], "notes": "", "background": None}
        entry = {k: v for k, v in o.items() if k != "elements"}
        elements = []
        if p["action"] == "create":
            for el in o["elements"]:
                elements.append(_rebased_element(el, [f"b2s_{h6(o['key'])}_{h6(el['key'])}_{tok}"], read))
            entry.update(objectId=sid, layoutObjectId="L", background_readback=read.get("background"),
                         notes_readback=read.get("notes") or "", groups=[], order=list(read.get("order") or []))
        else:
            b = base["slides"][p["base"]]
            bunits, ounits = merge.units(b["elements"]), merge.units(o["elements"])
            index = {e["key"]: e for e in o["elements"]}
            # what this sync made, as it made it: before the overrides (`apply_plan`)
            # (only what an override writes: grouping and z-order are the final read's, as
            # `Sync.created` is read after the content and order phases)
            pre = after.get("as_created") or {}
            made = {**read, "objects": {oid: {**rb, **{k: pre[oid][k] for k in OVERRIDDEN if k in pre[oid]}}
                                        if oid in pre else rb for oid, rb in read["objects"].items()}}
            for u in p["units"]:
                action = u["action"]
                if action in ("create", "recreate"):
                    for mk in u["ours_members"]:
                        elements.append(_rebased_element(index[mk], [f"b2s_{h6(o['key'])}_{h6(mk)}_{tok}"], made))
                elif action == "adopt_object":
                    for mk in u["ours_members"]:
                        elements.append(_rebased_element(index[mk], [u["objectId"]], read))
                elif action in ("move", "adopt"):
                    # the deck's own objects stay: ours IR with their read-back (moved, or the
                    # fields the deck already showed)
                    fields = {"text": ("text",), "geometry": ("box", "transform", "size"),
                              "image": ("image", "box", "transform", "size")}
                    for m in ounits[u["key"]]:
                        old = next((x for x in bunits[u["key"]] if x["key"] == m["key"]), None)
                        if old is None:
                            continue
                        rb = copy.deepcopy(old["readback"])
                        for oid in rb:
                            live_obj = read["objects"].get(oid)
                            if not live_obj:
                                continue
                            if action == "move":
                                rb[oid].update({k: live_obj[k] for k in ("box", "transform") if k in live_obj})
                            elif oid == old.get("main"):
                                for f in u.get("adopt", []):
                                    rb[oid].update({k: live_obj[k] for k in fields.get(f, ()) if k in live_obj})
                        elements.append({**m, "objects": old["objects"], "main": old["main"], "readback": rb})
                elif action == "keep":
                    # (a unit kept because the source dropped it says so: `merge.plan_unit`)
                    elements += [{**m, "removed": True} for m in bunits.get(u["key"], [])] \
                        if u.get("removed") else bunits.get(u["key"], [])
                # delete / none: gone
            entry.update(objectId=sid, layoutObjectId=b.get("layoutObjectId"), groups=list(b.get("groups", [])),
                         order=list(read.get("order") or b.get("order", [])))
            if b.get("left_readback"):
                # the person's unpaired boxes are still theirs a sync later (`build_adopt_base`,
                # `sync.new_base`); `left_object` rides along on the kept members, which carry
                # their old base entry
                entry["left_readback"] = copy.deepcopy(b["left_readback"])
                entry["left_alone"] = list(b.get("left_alone") or ())
            if p.get("background"):
                entry["background_readback"] = read.get("background")
            else:
                entry["background_readback"] = b.get("background_readback")
                if b.get("background") != o.get("background"):
                    entry["background"] = b.get("background")  # the deck's background stays a conflict
            if p.get("notes") is not None:
                entry["notes_readback"] = o.get("notes") or ""
            else:
                entry["notes_readback"] = b.get("notes_readback", "")
                if (b.get("notes") or "") != (o.get("notes") or ""):
                    entry["notes"] = b.get("notes")
        entry["elements"] = merge.keys_the_source_took(elements)
        entries[sid] = entry
    # The base is converter output, so it is written in the source's order (a slide the deck deleted
    # while the source still has it keeps its place, or the next conversion can no longer align an
    # unlabelled frame with it and syncs the deleted slide back in). This is the product's own
    # ordering on purpose: `second_sync_writes` asks whether the base a sync leaves behind settles,
    # and a base ordered here the way sync *ought* to would hide it when sync stops doing that.
    order = sync.base_order(mplan, {id(p): {"sid": sid} for p, sid in
                                    ((p, sids.get(id(p))) for p in mplan["slides"]) if sid},
                            [s["objectId"] for s in after["slides"]])
    slides = [entries.pop(sid) for sid in order if sid in entries] + list(entries.values())
    return {**base, "generation": base.get("generation", 0) + 1, "revisionId": after.get("revisionId"), "slides": slides}


def _rebased_element(el: dict, oids: list[str], read: dict) -> dict:
    objects = read.get("objects") or {}
    mine = [oid for oid in oids if oid in objects]
    group = objects.get(mine[0], {}).get("parent_group") if mine else None
    # like build_base: the unit's anchor owns the converter group its anchored pictures sit in;
    # a group the person drew around converter objects stays the person's.
    if not el.get("anchor") and group in objects and group.startswith("b2s_") and group.endswith("_g"):
        mine = mine + [group]
    return {**el, "objects": mine, "main": mine[0] if mine else None,
            "readback": {oid: copy.deepcopy(objects[oid]) for oid in mine}}


def _place_unit(u, members, theirs, made, base_by):
    """Re-apply the deck's move or resize to a rewritten unit: the whole unit takes the step, its
    anchored pictures with their text. The step comes from the old top's base and live read-backs
    (`delta`) and lands on the new unit wherever the source put it. This is the outcome, not the
    mechanism - sync writes one RELATIVE transform on the unit's group and, when the person has
    taken that group apart, one per member - and modelling the outcome is why the offline campaign
    could not see the step reaching only the top object (live seed 903,
    `tests/test_sync.py::test_a_moved_unit_with_no_group_is_moved_member_by_member`)."""
    ov = (u.get("overrides") or {}).get("geometry")
    if not ov or not made:
        return
    anchor = base_by.get(u["key"]) or (members[0] if members else None)
    old_top = (merge.unit_top(members, theirs) if members else None) or (anchor or {}).get("main")
    base_rb = next((m["readback"][old_top] for m in members if old_top in m.get("readback", {})), None)
    theirs_rb = theirs["objects"].get(old_top)
    if not base_rb or not theirs_rb:
        return
    # The person's step goes on top of wherever the source put the rewritten unit - also when the
    # source moved it too (`sync.carried`; this world's deck edits are moves, never resizes).
    dx, dy = theirs_rb["box"][0] - base_rb["box"][0], theirs_rb["box"][1] - base_rb["box"][1]
    for _, rb in made:
        rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
        rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]


def _take_place(live, old: str, new: list[str]) -> bool:
    """`new` takes `old`'s place in the z-order: among the page elements (`order`) or among its
    group's children. False when `old` stands nowhere."""
    found = False
    for lst in [live.setdefault("order", [])] + [rb.setdefault("children", []) for rb in live["objects"].values()
                                                if rb["kind"] == "elementGroup"]:
        if old in lst:
            i = lst.index(old)
            lst[i:i + 1] = new
            found = True
    return found


def _kids(objects: dict, gid: str) -> list[str]:
    """A group's children bottom to top: the ones its list names, then any that joined it since."""
    listed = [c for c in objects[gid].get("children") or [] if objects.get(c, {}).get("parent_group") == gid]
    return list(dict.fromkeys(listed + [c for c, x in objects.items() if x.get("parent_group") == gid]))


def _drop_lonely_groups(live):
    """Slides drops a group left with one child (the child takes its place), and a read-back lists
    what is really there, in paint order: `order` the slide's own page elements, a group's
    `children` its own (`loss_oracle.paint_order` reads both)."""
    changed = True
    while changed:
        changed = False
        for oid, rb in list(live["objects"].items()):
            if rb["kind"] != "elementGroup":
                continue
            kids = _kids(live["objects"], oid)
            if len(kids) <= 1:
                for c in kids:
                    live["objects"][c]["parent_group"] = rb.get("parent_group")
                _take_place(live, oid, kids)
                live["objects"].pop(oid)
                changed = True
    # A group says who its children are, and a unit rewritten under it has new ones: leaving the
    # list naming the objects the recreation deleted makes a read-back no API could hand back, and
    # `merge._descendants` - which sync itself asks what a group carries - then answers with the
    # dead. Everything else here reads `parent_group`, which is why it went unnoticed until
    # `fuzz_sync._movable` asked what one transform on a group would move.
    objects = live["objects"]
    for oid, rb in objects.items():
        if rb["kind"] == "elementGroup":
            rb["children"] = _kids(objects, oid)
    top = [o for o in live.get("order") or [] if o in objects and not objects[o].get("parent_group")]
    live["order"] = list(dict.fromkeys(top + [o for o, x in objects.items() if not x.get("parent_group")]))
