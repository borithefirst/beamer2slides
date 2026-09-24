"""Fuzz sync: random deck edits against random source changes, judged by tools/loss_oracle.py.

Two modes, the same oracle:

  offline   a made-up deck (tools/fuzz_world.py): random source changes -> `ours`, random deck
            edits -> `theirs`, `merge.plan_merge` plans, `fuzz_world.apply_plan` writes the plan the
            way docs/sync.md says a correct sync writes it, and the oracle judges the result.
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

  python tools/fuzz_sync.py offline --rounds 300 [--seed 0] [--no-shrink]
  python tools/fuzz_sync.py offline --replay 17            # one seed, verbose
  python tools/fuzz_sync.py live --rounds 20 [--parallel 3] [--chain 2] [--keep-decks]
  python tools/fuzz_sync.py live --replay 5                # the recorded edits of that seed
"""

import argparse
import copy
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
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from beamer2slides import adopt_sync, merge, snapshot
from beamer2slides.paths import CHECKOUT as ROOT  # live rounds build tests/decks/sync from the checkout

from . import fuzz_world as W
from . import loss_oracle

MAX_EDITS = 8
MIN_EDITS = 2


# ---------------------------------------------------------------- source changes (offline)

def _text_elements(slide, role=None):
    return [e for e in slide["elements"] if e["kind"] == "text" and (role is None or e.get("role") == role)]


def _pick(rng, items):
    return rng.choice(items) if items else None


def src_reword(rng, doc):
    el = _pick(rng, [e for s in doc["slides"] for e in _text_elements(s, "body")])
    if not el:
        return None
    p = rng.choice(el["paragraphs"])
    r = p["runs"][0]
    words = r["text"].split()
    if not words:
        return None
    i = rng.randrange(len(words))
    words[i] = rng.choice(W.WORDS) + "-ours"
    r["text"] = " ".join(words)
    return f"reword {el['id']}"


def src_add_paragraph(rng, doc):
    el = _pick(rng, [e for s in doc["slides"] for e in _text_elements(s, "body")])
    if not el:
        return None
    line = "added by the source " + rng.choice(W.WORDS)
    p = copy.deepcopy(el["paragraphs"][-1])
    p["runs"] = [W.run(line)]
    el["paragraphs"].append(p)
    el["bbox"][3] += 12
    return f"add paragraph to {el['id']}"


def src_remove_paragraph(rng, doc):
    el = _pick(rng, [e for s in doc["slides"] for e in _text_elements(s, "body") if len(e["paragraphs"]) > 1])
    if not el:
        return None
    el["paragraphs"].pop(rng.randrange(len(el["paragraphs"])))
    el["bbox"][3] -= 12
    return f"remove paragraph from {el['id']}"


def src_move_element(rng, doc):
    """A move takes what is anchored to the element: an inline formula picture is placed by the line
    it sits in, so a source that moves the text moves the picture with it. Moving the text alone
    meant `merge.unit_shift` could never find one step for the unit, and `move` - a write path of its
    own (`sync.Sync.move_requests`) - came up twice in 800 offline steps. One move in four is still
    of a single anchored member, which is the re-placed formula `unit_shift` exists to refuse."""
    options = [(s, e) for s in doc["slides"] for e in s["elements"] if e.get("role") != "title"]
    if not options:
        return None
    s, el = rng.choice(options)
    dx, dy = rng.choice([-20, -8, 8, 20]), rng.choice([-16, -6, 6, 16])
    anchored = [e for e in s["elements"] if e.get("anchor") == el["id"]]
    moving = [rng.choice(anchored)] if anchored and rng.random() < 0.25 else [el] + anchored
    for e in moving:
        e["bbox"] = [e["bbox"][0] + dx, e["bbox"][1] + dy, e["bbox"][2] + dx, e["bbox"][3] + dy]
    return f"move {' '.join(e['id'] for e in moving)}"


def src_resize_element(rng, doc):
    el = _pick(rng, [e for s in doc["slides"] for e in s["elements"] if e["kind"] in ("image", "shape")])
    if not el:
        return None
    el["bbox"][2] += rng.choice([-15, 15, 30])
    el["bbox"][3] += rng.choice([-10, 10])
    return f"resize {el['id']}"


def src_restyle(rng, doc):
    el = _pick(rng, [e for s in doc["slides"] for e in _text_elements(s, "body")])
    if not el:
        return None
    for p in el["paragraphs"]:
        for r in p["runs"]:
            r["color"] = rng.choice(["#cc0000", "#0000cc", "#008000"])
    return f"recolour {el['id']}"


def src_add_element(rng, doc):
    s = rng.choice(doc["slides"])
    eid = f"p{s['page']}t{len(s['elements']) + 9}"
    s["elements"].append(W.text_ir(eid, "a brand new source paragraph " + rng.choice(W.WORDS), (230, 160, 330, 180)))
    return f"add element {eid}"


def src_delete_element(rng, doc):
    options = [(s, e) for s in doc["slides"] for e in s["elements"]
               if e.get("role") not in ("title",) and not e.get("anchor")
               and not any(x.get("anchor") == e["id"] for x in s["elements"])]
    if not options:
        return None
    s, el = rng.choice(options)
    s["elements"].remove(el)
    return f"delete element {el['id']}"


def src_add_slide(rng, doc):
    i = rng.randrange(len(doc["slides"]) + 1)
    # a label and element ids nothing else uses: two frames with one \label is a broken source,
    # and two slides with one key would make the whole identity model meaningless. A label a
    # deleted slide took with it is not free either, though nothing living carries it: reissuing
    # it says "this new frame is that frame", which is exactly what a label means, so the sync
    # pairs them and is right to - while a campaign that keeps its own truth reads the pairing as
    # a frame on the wrong slide (fuzz_labels --shape adopt seed 7100523: one step deleted the
    # slide holding `new5` and added a frame that was handed `new5` again).
    labels = {s.get("label") for s in doc["slides"]}
    ids = {e["id"] for s in doc["slides"] for e in s["elements"]}
    page = max(len(doc["slides"]), doc.get("labelled", 0))
    while f"new{page}" in labels or f"p{page}t0" in ids:
        page += 1
    doc["labelled"] = page + 1
    title = "New source frame " + rng.choice(W.WORDS)
    slide = {"page": page, "label": f"new{page}", "title": title, "notes": "", "bg": "#ffffff",
             "elements": [W.text_ir(f"p{page}t0", title, (20, 20, 200, 34), role="title"),
                          W.text_ir(f"p{page}t1", "what the new frame says", (25, 60, 200, 72))]}
    doc["slides"].insert(i, slide)
    _renumber(doc)
    return f"add slide {title!r}"


def src_delete_slide(rng, doc):
    if len(doc["slides"]) < 3:
        return None
    i = rng.randrange(1, len(doc["slides"]))
    gone = doc["slides"].pop(i)
    _renumber(doc)
    return f"delete slide {gone['title']!r}"


def src_move_slide(rng, doc):
    if len(doc["slides"]) < 3:
        return None
    i = rng.randrange(1, len(doc["slides"]))
    j = rng.randrange(1, len(doc["slides"]))
    if i == j:
        return None
    doc["slides"].insert(j, doc["slides"].pop(i))
    _renumber(doc)
    return f"move slide {i}->{j}"


def _titled(rng, doc):
    """A slide with a title element, and that element. An adopt-shaped deck has slides with none -
    that is the shape they come in - and there is no title there to edit."""
    options = [(s, W.title_element(s)) for s in doc["slides"] if W.title_element(s)]
    return rng.choice(options) if options else (None, None)


def src_retitle(rng, doc):
    s, el = _titled(rng, doc)
    if not s:
        return None
    title = f"{rng.choice(W.WORDS).title()} retitled"
    s["title"] = title
    el["paragraphs"] = [{**el["paragraphs"][0], "runs": [W.run(title)]}]
    return f"retitle {title!r}"


def src_amend_title(rng, doc):
    """A title edited rather than replaced ("Results" -> "Results v2"), which is what a source does
    when it revises a whole talk - and what tells a yes-or-no "same title?" nothing at all."""
    s, el = _titled(rng, doc)
    if not s:
        return None
    title = f"{W.title_of(s)} {rng.choice(('v2', 'again', 'revisited'))}"
    s["title"] = title
    el["paragraphs"] = [{**el["paragraphs"][0], "runs": [W.run(title)]}]
    return f"amend title to {title!r}"


def src_notes(rng, doc):
    s = rng.choice(doc["slides"])
    s["notes"] = "source notes " + " ".join(rng.choice(W.WORDS) for _ in range(3))
    return f"notes of {s['title']!r}"


def src_background(rng, doc):
    s = rng.choice(doc["slides"])
    s["bg"] = rng.choice(["#eeeeee", "#fff2cc", "#e8f0fe"])
    return f"background of {s['title']!r}"


def src_repaint(rng, doc, out=None):
    options = [(s, e) for s in doc["slides"] for e in s["elements"] if e["kind"] == "image"]
    if not options:
        return None
    s, el = rng.choice(options)
    name = f"{Path(el['file']).stem}-v2.bin"
    (out / name).write_bytes(bytes([(rng.randrange(251) + j) % 251 for j in range(64)]))
    el["file"] = name
    return f"redraw {el['id']}"


def _labelled(doc):
    return [s for s in doc["slides"] if s.get("label")]


def src_move_label(rng, doc):
    """A label pasted onto another frame: the invariant break `identity.label_moves` is about. When
    the other frame had one of its own this is a swap, which is the hardest shape of it."""
    if len(doc["slides"]) < 2 or not _labelled(doc):
        return None
    src = rng.choice(_labelled(doc))
    dst = rng.choice([s for s in doc["slides"] if s is not src])
    name = src["label"]
    src["label"], dst["label"] = dst.get("label"), name
    return f"move label {name!r} onto {dst['title']!r}"


def src_rename_label(rng, doc):
    if not _labelled(doc):
        return None
    s = rng.choice(_labelled(doc))
    was, s["label"] = s["label"], f"{s['label']}-renamed"
    return f"rename label {was!r}"


def src_drop_label(rng, doc):
    if not _labelled(doc):
        return None
    s = rng.choice(_labelled(doc))
    was, s["label"] = s["label"], None
    return f"drop label {was!r}"


def src_edit_cell(rng, doc):
    options = [e for s in doc["slides"] for e in s["elements"] if e["kind"] == "table"]
    if not options:
        return None
    el = rng.choice(options)
    r, c = rng.randrange(len(el["cells"])), rng.randrange(len(el["cells"][0]))
    el["cells"][r][c] = [W.run(rng.choice(W.WORDS) + "-ours")]
    return f"rewrite cell {r},{c} of {el['id']}"


def _deck_edited_ir_ids(base, live) -> list[str]:
    """The ids of the source elements whose text the person has just changed in the deck."""
    out = []
    for b, s, el, oid, rb in _converter_texts(base, live):
        if (rb.get("text") or "") != ((el.get("readback", {}).get(oid) or {}).get("text") or ""):
            out.append(el["id"])
    return out


def src_collide(rng, doc, touched=()):
    """Change exactly what the person has just changed in the deck. Two sides on the same text is
    what every rule in `merge.plan_unit` is about, and drawing both sides' targets at random makes
    it a rarity: 5 text overrides in 200 offline rounds before this, and never once a table, so the
    campaign was barely exercising the merge it exists to test. A real deck is not random either -
    the author revises the frame the reader was reading."""
    ids = set(touched)
    options = [(s, e) for s in doc["slides"] for e in s["elements"] if e["id"] in ids]
    if not options:
        return None
    s, el = rng.choice(options)
    if el["kind"] == "table":
        r, c = rng.randrange(len(el["cells"])), rng.randrange(len(el["cells"][0]))
        el["cells"][r][c] = [W.run(rng.choice(W.WORDS) + "-ours")]
        return f"also rewrite cell {r},{c} of {el['id']}"
    how = rng.choice(("reword", "append", "drop", "move") if len(el["paragraphs"]) > 1 else ("reword", "append", "move"))
    if how == "move":
        # The source moved the very box the person had just edited, and changed nothing else about
        # it: the only shape that makes `merge.plan_unit` write a `move` (the source's place onto the
        # deck's own objects, `sync.Sync.move_requests`). Both sides have to meet on one unit for it,
        # so drawing the two targets apart made that write path come up 2 times in 800 offline steps
        # - and it is the path live seed 903 broke.
        dx, dy = rng.choice([-20, -8, 8, 20]), rng.choice([-16, -6, 6, 16])
        for e in [el] + [x for x in s["elements"] if x.get("anchor") == el["id"]]:
            e["bbox"] = [e["bbox"][0] + dx, e["bbox"][1] + dy, e["bbox"][2] + dx, e["bbox"][3] + dy]
    elif how == "drop":                   # (the shape live seeds 608/616 died on, from the other side)
        el["paragraphs"].pop(rng.randrange(len(el["paragraphs"])))
        el["bbox"][3] -= 12
    elif how == "append":
        p = copy.deepcopy(el["paragraphs"][-1])
        p["runs"] = [W.run("and the source adds " + rng.choice(W.WORDS))]
        el["paragraphs"].append(p)
        el["bbox"][3] += 12
    else:
        p = rng.choice(el["paragraphs"])
        words = p["runs"][0]["text"].split()
        if not words:
            return None
        words[rng.randrange(len(words))] = rng.choice(W.WORDS) + "-ours"
        p["runs"][0]["text"] = " ".join(words)
    return f"also {how} {el['id']}"


def _renumber(doc):
    for i, s in enumerate(doc["slides"]):
        s["page"] = i


SOURCE_OPS = {f.__name__[4:]: f for f in (src_reword, src_add_paragraph, src_remove_paragraph, src_move_element,
                                          src_resize_element, src_restyle, src_add_element, src_delete_element,
                                          src_add_slide, src_delete_slide, src_move_slide, src_retitle,
                                          src_amend_title, src_notes, src_background, src_repaint,
                                          src_move_label, src_rename_label, src_drop_label,
                                          src_edit_cell, src_collide)}


# ---------------------------------------------------------------- deck edits (offline)

def _converter_texts(base, live):
    """(base slide, live slide, element, object id, read-back) of every converter text object."""
    by_id = {s["objectId"]: s for s in live["slides"]}
    out = []
    for b in base["slides"]:
        s = by_id.get(b["objectId"])
        if not s:
            continue
        for el in b["elements"]:
            rb = s["objects"].get(el.get("main"))
            if rb and rb.get("text") is not None and el["kind"] in ("text", "table"):
                out.append((b, s, el, el["main"], rb))
    return out


def _bump(rb, dx, dy):
    rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
    rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]


def _retext(rb, text):
    """A read-back's text changed the way a person changes it in Slides: the run styling moves with
    the words around the edit. Leaving `run_spans` at their old indices makes a read-back no API
    could hand back - the spans then cover letters in the middle of words nobody styled - and the
    campaign duly accused `merge.styling_lost` of losing styling that was never where the spans
    said it was (offline seed 2194: bold on "picture", the deck rewording an earlier word, and the
    stale span landing inside "colleague-theirs")."""
    was, rb["text"] = rb.get("text") or "", text
    if not rb.get("run_spans"):
        return
    at = {}                                  # old index -> new index, at the edit boundaries
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, was, text, autojunk=False).get_opcodes():
        at[i1], at[i2] = j1, j2

    def move(i):
        k = max((x for x in at if x <= i), default=0)
        return max(0, min(at.get(k, 0) + (i - k), len(text)))

    rb["run_spans"] = [[move(s), move(e), style] for s, e, style in rb["run_spans"] if move(e) > move(s)]


def deck_reword(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    lines = [l for l in (rb["text"] or "").split("\n") if l.strip()]
    if not lines:
        return None
    line = rng.choice(lines)
    words = line.split()
    i = rng.randrange(len(words))
    words[i] = rng.choice(W.WORDS) + "-theirs"
    _retext(rb, (rb["text"] or "").replace(line, " ".join(words), 1))
    return f"reword {oid}"


def deck_append(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    _retext(rb, (rb["text"] or "").rstrip("\n") + " typed by a person.\n")
    return f"append to {oid}"


def deck_delete_paragraph(rng, base, live):
    options = [x for x in _converter_texts(base, live)
               if x[2]["kind"] == "text" and len([l for l in (x[4]["text"] or "").split("\n") if l.strip()]) > 1]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    lines = [l for l in (rb["text"] or "").split("\n") if l.strip()]
    lines.pop(rng.randrange(len(lines)))
    _retext(rb, "\n".join(lines) + "\n")
    return f"delete a paragraph of {oid}"


def deck_edit_cell(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "table"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rows = [r.split("\t") for r in rb["text"].split("\n")]
    rows[rng.randrange(len(rows))][rng.randrange(len(rows[0]))] = rng.choice(W.WORDS) + "-theirs"
    _retext(rb, "\n".join("\t".join(r) for r in rows))
    return f"edit a cell of {oid}"


def deck_move(rng, base, live):
    options = _converter_objects(base, live)
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    top = rb.get("parent_group") or oid
    dx, dy = rng.choice([-30, -10, 10, 30]), rng.choice([-20, 20, 40])
    for o, x in s["objects"].items():
        if o == top or x.get("parent_group") == top:
            _bump(x, dx, dy)
    return f"move {top}"


def deck_restyle(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rb["text_styles"] = [{**rb["text_styles"][0], "bold": True}] if rb["text_styles"] else [{"bold": True}]
    rb["text_style_hash"] = "s-bold"
    return f"bold {oid}"


def deck_bold_word(rng, base, live):
    """One word of a box bolded, which is what the `ranges` re-application is for: the style goes
    back onto that same word - if the source has not replaced it in the meantime."""
    options = [x for x in _converter_texts(base, live)
               if x[2]["kind"] == "text" and len(re.findall(r"\w+", x[4].get("text") or "")) > 2]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    text = rb["text"]
    start, end = rng.choice([(m.start(), m.end()) for m in re.finditer(r"\w+", text)])
    plain = rb["text_styles"][0] if rb["text_styles"] else {"fontFamily": "Lato", "fontSize": 18.0}
    bold = {**plain, "bold": True}
    rb["text_styles"] = [plain, bold]
    rb["run_spans"] = [[0, start, plain], [start, end, bold], [end, len(text.rstrip("\n")), plain]]
    rb["text_style_hash"] = "s-word-bold"
    return f"bold {text[start:end]!r} in {oid}"


def _converter_objects(base, live):
    by_id = {s["objectId"]: s for s in live["slides"]}
    out = []
    for b in base["slides"]:
        s = by_id.get(b["objectId"])
        if not s:
            continue
        for el in b["elements"]:
            rb = s["objects"].get(el.get("main"))
            if rb:
                out.append((b, s, el, el["main"], rb))
    return out


def deck_delete_object(rng, base, live):
    options = [x for x in _converter_objects(base, live) if x[2].get("role") != "title"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    s["objects"].pop(oid)
    return f"delete {oid}"


def deck_replace_image(rng, base, live):
    options = [x for x in _converter_objects(base, live) if x[4]["kind"] == "image"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rb["image"] = {"contentHash": f"user{rng.randrange(10 ** 6):06}", "sourceUrl": None,
                   "signature": W.picture_signature(bytes([rng.randrange(200) + 40]))}
    return f"replace the picture of {oid}"


def _taken(live: dict) -> set[str]:
    """Every id the deck already carries - slides, their notes pages and their objects."""
    ids: set[str] = set()
    for s in live["slides"]:
        ids.add(s["objectId"])
        ids.add(s.get("notes_id"))
        ids.update(s["objects"])
    return ids - {None}


def _fresh(rng, live: dict, prefix: str, width: int = 6) -> str:
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
    return oid


def deck_add_text_box(rng, base, live):
    s = rng.choice(live["slides"])
    oid = _fresh(rng, live, "user", 9)
    s["objects"][oid] = W.readback("shape", [300, 300, 460, 330], text=f"a note nobody may lose {oid}\n")
    s["order"].append(oid)
    return f"add a text box {oid}"


def deck_add_image(rng, base, live):
    s = rng.choice(live["slides"])
    oid = _fresh(rng, live, "user", 9)
    s["objects"][oid] = W.readback("image", [400, 200, 500, 260],
                                   image={"contentHash": f"u{rng.randrange(10 ** 6)}", "sourceUrl": None,
                                          "signature": W.picture_signature(bytes([rng.randrange(200)]))})
    s["order"].append(oid)
    return f"add a picture {oid}"


def deck_add_slide(rng, base, live):
    i = rng.randrange(len(live["slides"]) + 1)
    sid = _fresh(rng, live, "user_s")
    oid = f"{sid}_t"
    live["slides"].insert(i, {"objectId": sid, "layoutObjectId": "L", "background": {"color": "#ffffff"},
                              "notes": "a slide the person added", "notes_id": f"{sid}_n", "order": [oid],
                              "objects": {oid: W.readback("shape", [40, 40, 300, 80], text="my own slide\n")}})
    return f"add a slide {sid}"


def deck_duplicate_slide(rng, base, live):
    s = rng.choice(live["slides"])
    sid = _fresh(rng, live, "user_s")
    copy_ = copy.deepcopy(s)
    copy_["objectId"] = sid
    copy_["notes_id"] = f"{sid}_n"   # (a copy gets its own notes page, as it does in Slides)
    copy_["objects"] = {f"{sid}_{k}": v for k, v in enumerate(copy_["objects"].values())}
    copy_["order"] = list(copy_["objects"])
    for rb in copy_["objects"].values():
        rb["parent_group"] = None
    live["slides"].insert(live["slides"].index(s) + 1, copy_)
    return f"duplicate slide {s['objectId']}"


def deck_delete_slide(rng, base, live):
    if len(live["slides"]) < 2:
        return None
    s = rng.choice(live["slides"])
    live["slides"].remove(s)
    return f"delete slide {s['objectId']}"


def deck_move_slide(rng, base, live):
    if len(live["slides"]) < 3:
        return None
    i, j = rng.randrange(len(live["slides"])), rng.randrange(len(live["slides"]))
    if i == j:
        return None
    live["slides"].insert(j, live["slides"].pop(i))
    return f"move slide {i}->{j}"


def deck_notes(rng, base, live):
    s = rng.choice(live["slides"])
    s["notes"] = "the person's notes " + " ".join(rng.choice(W.WORDS) for _ in range(3))
    return f"notes of {s['objectId']}"


def deck_background(rng, base, live):
    s = rng.choice(live["slides"])
    s["background"] = {"color": rng.choice(["#ffe0e0", "#e0ffe0", "#e0e0ff"])}
    return f"background of {s['objectId']}"


def deck_group(rng, base, live):
    options = [s for s in live["slides"] if len([o for o, rb in s["objects"].items()
                                                 if not rb.get("parent_group") and rb["kind"] != "elementGroup"]) >= 2]
    if not options:
        return None
    s = rng.choice(options)
    tops = [o for o, rb in s["objects"].items() if not rb.get("parent_group") and rb["kind"] != "elementGroup"]
    picked = rng.sample(tops, 2)
    gid = _fresh(rng, live, "user_g")
    boxes = [s["objects"][o]["box"] for o in picked]
    # Slides keeps the children's z-order, and the group stands where the topmost of them stood
    order = s.setdefault("order", [])
    picked.sort(key=lambda o: order.index(o) if o in order else len(order))
    rb = {**W.readback("elementGroup", [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                        max(b[2] for b in boxes), max(b[3] for b in boxes)]),
          "children": list(picked)}
    # ... and the group takes the topmost child's place *before* it is a group on this slide:
    # `_take_place` puts the new id wherever the old one stands, its own children among them, and
    # a group that lists itself as a child is a deck the API could not describe (its `children` was
    # `picked` itself, so the op even said so out loud: `group [a, g] as g`).
    W._take_place(s, picked[-1], [gid])
    s["objects"][gid] = rb
    for o in picked:
        s["objects"][o]["parent_group"] = gid
    s["order"] = [o for o in s["order"] if o not in picked]
    return f"group {picked} as {gid}"


def deck_ungroup(rng, base, live):
    options = [(s, o) for s in live["slides"] for o, rb in s["objects"].items() if rb["kind"] == "elementGroup"]
    if not options:
        return None
    s, gid = rng.choice(options)
    parent = s["objects"][gid].get("parent_group")
    kids = W._kids(s["objects"], gid)
    for c in kids:
        s["objects"][c]["parent_group"] = parent   # (a group inside a group hands them to its parent)
    W._take_place(s, gid, kids)                    # ... where the group stood, in their order
    s["objects"].pop(gid)
    return f"ungroup {gid}"


DECK_OPS = {f.__name__[5:]: f for f in (deck_reword, deck_append, deck_delete_paragraph, deck_edit_cell, deck_move,
                                        deck_restyle, deck_bold_word, deck_delete_object, deck_replace_image, deck_add_text_box,
                                        deck_add_image, deck_add_slide, deck_duplicate_slide, deck_delete_slide,
                                        deck_move_slide, deck_notes, deck_background, deck_group, deck_ungroup)}


# ---------------------------------------------------------------- offline rounds

def _sync_step(seed: int, step: int, doc: dict, base: dict, live: dict, tmp: Path,
               deck_ops: list[str], source_ops: list[str], rebase: bool) -> dict:
    """One edit + sync: the person edits the deck, the author changes the source, merge plans, the
    reference applier writes the plan and the oracle judges what the person is left with."""
    applied_deck = []
    for k, name in enumerate(deck_ops):
        if not live["slides"]:
            applied_deck.append(f"{name}: (no slides left)")  # a chain that deleted the whole deck
            continue
        done = DECK_OPS[name](random.Random(seed * 1009 + 101 * step + k), base, live)
        applied_deck.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    for s in live["slides"]:
        W._drop_lonely_groups(s)  # (Slides drops a group an edit left with one child)
    seen: set[str] = set()        # ... and never hands out one objectId twice (`_fresh`)
    for s in live["slides"]:
        for oid in [s["objectId"], s.get("notes_id"), *s["objects"]]:
            if oid is not None and oid in seen:
                raise AssertionError(f"the deck edits gave {oid} to two things at once: "
                                     f"a deck no Slides could hand back, and whatever the oracle "
                                     f"says about it is about the fuzzer, not the sync")
            seen.add(oid)
        for gid, rb in s["objects"].items():
            if rb["kind"] == "elementGroup" and gid in (rb.get("children") or []):
                raise AssertionError(f"the deck edits made {gid} a child of itself, which is the "
                                     f"same kind of deck and the same kind of nonsense")
    doc2 = copy.deepcopy(doc)
    applied_src = []
    touched = _deck_edited_ir_ids(base, live)   # what the person just edited, for `collide`
    for k, name in enumerate(source_ops):
        fn = SOURCE_OPS[name]
        r = random.Random(seed * 2003 + 101 * step + k)
        extra = {"repaint": (tmp,), "collide": (touched,)}.get(name, ())
        done = fn(r, doc2, *extra)
        applied_src.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    ours = W.build_ours(doc2, base, tmp)
    mplan = merge.plan_merge(base, ours, live)
    # What this sync left alone because it is a person's deck and nothing here may write there
    # (`merge.plan_unit`'s unpaired unit, `plan_merge`'s slide no frame accounts for). Counted for
    # the same reason as `refused` below: it is how one sees the campaign's own reach.
    held = (["element"] * sum(1 for p in mplan["slides"] for u in p.get("units") or []
                              if u.get("unpaired") or u.get("inherited") or u.get("in_table"))
            + ["slide"] * sum(1 for k in mplan["report"]["slides"]["kept"] if k["reason"] == ["the deck's own"]))
    # `--backup auto` keeps a .pptx of the deck before sync's first write, so the campaign asks what
    # the other refusals do; the no-way-back one has its own test (tests/test_adopt_sync.py).
    refused = adopt_sync.problems(base, mplan, live, {"drive": {"presentationId": "way-back"}})
    if refused:
        # A sync into an adopted deck this one may not write (adopt_sync.problems). Nothing is sent,
        # so the deck is exactly as the person left it - that is the whole answer, and the oracle
        # has nothing to judge. The base does not move either: the next step plans from this one.
        return {"seed": seed, "step": step, "source_ops": list(source_ops), "deck_ops": list(deck_ops),
                "source": applied_src, "deck": applied_deck, "findings": [], "failures": [],
                "refused": [p["reason"] for p in refused], "held": held, "doc": doc2,
                "message": adopt_sync.refusal_message(base["presentationId"], tmp, "new.pdf", refused),
                "state": {"base": base, "before": live, "after": live, "report": mplan["report"],
                          "ours": ours, "next_base": None, "doc": doc2, "work": tmp}}
    tok = f"{step}zz"  # a token per run, like sync's
    after = W.apply_plan(base, ours, live, mplan, tok)
    report = mplan["report"]
    findings = (loss_oracle.check(base, live, after, report, ours) + _writable(ours, mplan)
                + _movable(base, live, mplan) + _stacked(base, live, after, ours, mplan, tok)
                + _doubled(base, live, after, mplan))
    next_base = W.rebase(base, ours, after, mplan, tok) if rebase else None
    findings += _settled(doc2, next_base, after, tmp, bool(loss_oracle.uncertain_slides(base, ours)))
    return {"seed": seed, "step": step, "source_ops": list(source_ops), "deck_ops": list(deck_ops),
            "source": applied_src, "deck": applied_deck, "findings": findings,
            "failures": loss_oracle.failures(findings), "doc": doc2, "refused": [], "held": held,
            "state": {"base": base, "before": live, "after": after, "report": report, "ours": ours,
                      "next_base": next_base, "doc": doc2, "work": tmp}}


def _apply_text_requests(current: str, reqs: list[dict]) -> str:
    """Slides applying them, refusals included: the newline a shape's text ends on is the API's own
    and is left out of the length it will accept, so a `deleteText` reaching the end is thrown out
    and the batch with it (`merge.text_edit_requests`)."""
    units = list(current)
    for r in reqs:
        length = len(units) - 1 if units and units[-1] == "\n" else len(units)
        if "deleteText" in r:
            rng = r["deleteText"]["textRange"]
            if rng["endIndex"] > length:
                raise ValueError(f"the end index ({rng['endIndex']}) should not be greater than "
                                 f"the existing text length ({length})")
            del units[rng["startIndex"]:rng["endIndex"]]
        else:
            i = r["insertText"]["insertionIndex"]
            if i > length:
                raise ValueError(f"the insertion index ({i}) is past the text ({length})")
            units[i:i] = list(r["insertText"]["text"])
    return "".join(units)


def _writable_cells(skey: str, ekey: str, ir: dict, current: str, ov: dict) -> list[dict]:
    """The same for a table: sync writes a cell at a time, each cell's text ending on the newline
    Slides keeps (`sync.Sync.override_requests`), so every cell is its own little text with the
    same arithmetic - and a cell emptied to nothing is exactly the shape that kills a batch."""
    if ir.get("kind") != "table" or not ir.get("cells"):   # the source made it something else
        return []
    dims = [len(ir["cells"]), len(ir["cells"][0])]
    cells = merge.table_merge(ov["base"], current, ov["theirs"], dims, ov.get("dims"))
    grid = merge.table_grid(current, dims)
    if cells is None or grid is None:
        return []
    out = []
    for r, (crow, mrow) in enumerate(zip(grid, cells[0])):
        for c, (now_cell, want) in enumerate(zip(crow, mrow)):
            if want == now_cell:
                continue
            loc = {"rowIndex": r, "columnIndex": c}
            try:
                written = _apply_text_requests(
                    now_cell + "\n", merge.text_edit_requests("oid", now_cell + "\n", want + "\n", loc))
            except ValueError as e:
                out.append(loss_oracle.finding("unwritable_text", "report",
                                               f"Slides refuses the edit of cell {r},{c}: {e}",
                                               slide=skey, element=ekey))
                continue
            if written != want + "\n":
                out.append(loss_oracle.finding("unwritable_text", "report",
                                               f"the requests write {written!r} into cell {r},{c}, "
                                               f"not the merged {want + chr(10)!r}", slide=skey, element=ekey))
    return out


def _writable(ours: dict, mplan: dict) -> list[dict]:
    """Every text the deck keeps must be writable as requests. The plan says *what* the merged text
    is; `sync.Sync.override_requests` turns it into deleteText / insertText against the element the
    converter has just recreated, and that arithmetic has to obey rules the plan knows nothing
    about. The reference applier merges the text itself and never looks at a request, so without
    this the campaign proves a sync that cannot be written (live seeds 608 and 616: the deck had
    deleted the last paragraph of a text box the source rewrote, the batch was refused and the sync
    died with it)."""
    out = []
    for p in mplan["slides"]:
        if p["action"] != "update" or p.get("ours") is None:
            continue
        els = {e["key"]: e for e in ours["slides"][p["ours"]]["elements"]}
        for u in p["units"]:
            ov = (u.get("overrides") or {}).get("text")
            if u["action"] != "recreate" or not ov or u["key"] not in els:
                continue
            ir = els[u["key"]]["ir"]
            current = W.element_text(ir)
            if current is None:
                continue
            if ov.get("table"):
                out += _writable_cells(p["key"], u["key"], ir, current, ov)
                continue
            current = current if current.endswith("\n") else current + "\n"   # as Slides reads it back
            merged, _, safe = merge.text_merge(ov["base"], current, ov["theirs"], ov.get("take") or ())
            if not safe:
                continue
            merged = merged if merged.endswith("\n") else merged + "\n"
            try:
                written = _apply_text_requests(current, merge.text_edit_requests("oid", current, merged))
            except ValueError as e:
                out.append(loss_oracle.finding("unwritable_text", "report", f"Slides refuses the edit: {e}",
                                               slide=p["key"], element=u["key"]))
                continue
            if written != merged:
                out.append(loss_oracle.finding("unwritable_text", "report",
                                               f"the requests write {written!r}, not the merged {merged!r}",
                                               slide=p["key"], element=u["key"]))
    return out


def _doubled(base: dict, live: dict, after: dict, mplan: dict) -> list[dict]:
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
    out = []
    before = {o for s in live["slides"] for o in s["objects"]}
    for p in mplan["slides"]:
        if p["action"] != "update" or p.get("base") is None:
            continue
        b = base["slides"][p["base"]]
        made = next((s for s in after["slides"] if s["objectId"] == p.get("objectId")), None)
        if made is None:
            continue
        standing = {el["key"]: el["left_object"] for el in b["elements"]
                    if el.get("left_object") in made["objects"]}
        # what this sync created, each saying which element it is (`snapshot.tag`); the slide key
        # in front of it is the source's, which a renamed label makes not the base's
        written = [rb.get("title") or "" for oid, rb in made["objects"].items() if oid not in before]
        for ek, oid in sorted(standing.items()):
            if not any(t.startswith(snapshot.TAG_PREFIX) and t.endswith(f"/{ek}") for t in written):
                continue
            # `report`, not `loss`: the sync reported the source change as applied, and what the
            # slide shows is both versions of it.
            out.append(loss_oracle.finding(
                "adopt_double", "report",
                f"element {ek} was written into this deck although the base ties it to no object of "
                f"it: the person's own {oid} is still standing beside what was created",
                slide=b["key"], element=ek))
    return out


def _movable(base: dict, live: dict, mplan: dict) -> list[dict]:
    """A `move` is written as one RELATIVE transform per root of the unit, and a transform on a group
    carries its children - so whether the step reaches every object of the unit is a question about
    the request, not about the merge. The reference applier moves every member itself, which is the
    outcome and not the mechanism, so without this the campaign cannot see a step that leaves an
    anchored picture behind: exactly what a live chain had to find instead (seed 903, where the
    person had taken the unit's group apart and the formula picture stayed at the converter's box).
    Every object of the unit must take the step, and take it once - a group and its child both
    moving would move the child twice."""
    from beamer2slides.sync import EMU_PER_PT, Sync
    out = []
    slides = {s["objectId"]: s for s in live["slides"]}
    for p in mplan["slides"]:
        if p["action"] != "update" or p.get("base") is None or p.get("objectId") not in slides:
            continue
        read = slides[p["objectId"]]
        bunits = merge.units(base["slides"][p["base"]]["elements"])
        for u in p["units"]:
            if u["action"] != "move":
                continue
            mine = [o for m in bunits.get(u["key"], []) for o in m.get("objects", []) if o in read["objects"]]
            moved = Counter()
            for r in Sync.move_requests([u], bunits, read, W.SCALE):
                t = r["updatePageElementTransform"]["transform"]
                oid = r["updatePageElementTransform"]["objectId"]
                for x in [oid, *merge._descendants(oid, read)]:
                    moved[x] += 1
                want = [round(v * W.SCALE * EMU_PER_PT) for v in u["delta"]]
                if [t["translateX"], t["translateY"]] != want:
                    out.append(loss_oracle.finding(
                        "unwritten_move", "report",
                        f"{oid} is moved by {[t['translateX'], t['translateY']]}, not the planned {want}",
                        slide=p["key"], element=u["key"]))
            for oid in mine:
                if moved[oid] != 1:
                    out.append(loss_oracle.finding(
                        "unwritten_move", "report",
                        f"the requests move {oid} {moved[oid]} times, not once: the unit's step is "
                        f"written on {sorted(set(moved))}", slide=p["key"], element=u["key"]))
    return out


def _zorder(read: dict, reqs: list[dict], created: list[str]) -> dict[str, list[str]]:
    """Slides' z-order under a rewrite, as far as grouping goes: the slide as `read` has it, the
    `ungroupObjects` in `reqs` (a group's children take its place), `created` put on top in that
    order (a new object is created last, so above everything), then `updatePageElementsZOrder`
    BRING_TO_FRONT and `groupObjects` - which keeps the z-order its children have on the page, not
    the order the request lists them in, and stands where the topmost of them stood (the fix of
    dc8523a is exactly that difference). Returns every container's list bottom to top: "" for the
    page, a group id for its children."""
    objects = read["objects"]
    lists = {"": list(read.get("order") or [])}
    for oid, rb in objects.items():
        if rb.get("kind") == "elementGroup":
            lists[oid] = list(rb.get("children") or [])

    def where(oid):
        return next(((k, lst) for k, lst in lists.items() if oid in lst), (None, None))
    for r in reqs:
        if "ungroupObjects" in r:
            for g in r["ungroupObjects"]["objectIds"]:
                _, lst = where(g)
                if lst is not None:
                    i = lst.index(g)
                    lst[i:i + 1] = lists.pop(g, [])
    lists[""] += created
    for r in reqs:
        if "updatePageElementsZOrder" in r:
            body = r["updatePageElementsZOrder"]
            assert body["operation"] == "BRING_TO_FRONT", body
            for oid in body["pageElementObjectIds"]:
                _, lst = where(oid)
                if lst is not None:
                    lst.remove(oid)
                lists[""].append(oid)
        elif "groupObjects" in r:
            page = lists[""]
            kids = sorted((c for c in r["groupObjects"]["childrenObjectIds"] if c in page), key=page.index)
            if kids:
                at = page.index(kids[-1]) - len(kids) + 1
                lists[""] = page = [c for c in page if c not in kids]
                page.insert(at, r["groupObjects"]["groupObjectId"])
            lists[r["groupObjects"]["groupObjectId"]] = kids
    return lists


def _restacked(sync, p: dict, read: dict, now: dict, lists: dict[str, list[str]], tops: dict) -> None:
    """`now["order"]` as `Sync.restack`'s requests leave it, replacing the applier's own opinion.

    `restack` runs in `finish`, after the content batch and before the cleanup deletions, so the
    slide it is handed is the deck as it was plus every object this sync created (Slides puts a new
    object in front of everything) with the old ones still standing - `lists`, the containers after
    the ungrouping and regrouping - and `doomed` is what the cleanup will take away, which here is
    simply everything the applier's own write made disappear."""
    doomed = {oid for oid in read["objects"] if oid not in now["objects"]}
    objects = {oid: dict(rb) for oid, rb in now["objects"].items()}
    for oid in doomed:
        objects[oid] = dict(read["objects"][oid])
    for g, kids in lists.items():
        if g in objects:
            objects[g]["children"] = [c for c in kids if c in objects]
    page = [x for x in lists[""] if x in objects]
    w = {"plan": p, "doomed": doomed, "tops": tops}
    order = list(page)
    for r in sync.restack(w, read, {"objectId": p["objectId"], "order": page, "objects": objects}):
        oid, = r["updatePageElementsZOrder"]["pageElementObjectIds"]
        if oid in order:
            order.append(order.pop(order.index(oid)))
    kept = [x for x in order if x in now["objects"]]
    now["order"] = kept + [x for x in now.get("order") or [] if x not in kept and x in now["objects"]]


def _stacked(base: dict, live: dict, after: dict, ours: dict, mplan: dict, tok: str) -> list[dict]:
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
    from beamer2slides.sync import Sync
    slides = {s["objectId"]: s for s in live["slides"]}
    stacked = copy.deepcopy(after)
    judged = set()
    sync = Sync.__new__(Sync)
    sync.base, sync.ours = base, ours
    for p in mplan["slides"]:
        sid = p.get("objectId")
        if p["action"] != "update" or p.get("base") is None or sid not in slides:
            continue
        read = slides[sid]
        bunits = merge.units(base["slides"][p["base"]]["elements"])
        regroup, depth, roots_removed = Sync.regroups(p["units"], bunits, read)
        now = next((s for s in stacked["slides"] if s["objectId"] == sid), None)
        if now is None:
            continue
        skey = ours["slides"][p["ours"]]["key"]
        tops, created = {}, []
        for u in p["units"]:
            if u["action"] not in ("create", "recreate") or u["key"] not in u.get("ours_members", []):
                continue
            old = dict(roots_removed).get(u["key"]) or []
            kept = [r for r in old if r in now["objects"]]  # (the reference keeps an anchored unit's group)
            tops[u["key"]] = kept[0] if kept else f"b2s_{W.h6(skey)}_{W.h6(u['key'])}_{tok}"
            if not kept:
                created.append(tops[u["key"]])
        if not regroup and not tops:
            continue
        ungroup = [{"ungroupObjects": {"objectIds": [g]}} for g in sorted(regroup, key=lambda g: depth[g])]
        rank = Sync.zrank(ours["slides"][p["ours"]], bunits, tops)
        lists = _zorder(read, ungroup + Sync.regroup_requests(regroup, depth, read["objects"], tops, set(), rank),
                        created)
        for g in regroup:
            rb = now["objects"].get(g)
            if rb is None or g not in lists:
                continue
            mine = [c for c in lists[g] if c in rb.get("children", [])]
            rb["children"] = mine + [c for c in rb.get("children", []) if c not in mine]
        _restacked(sync, p, read, now, lists, tops)
        judged.add(sid)
    if not judged:
        return []
    pick = lambda read: {**read, "slides": [s for s in read["slides"] if s["objectId"] in judged]}  # noqa: E731
    out = loss_oracle.occlusion_findings(base, pick(live), pick(stacked), ours)
    for f in out:
        f["detail"] = f"stacked as sync.Sync.regroup_requests and Sync.restack stack it: {f['detail']}"
    return out


def _settled(doc: dict, next_base: dict | None, after: dict, tmp: Path, unsure: bool = False) -> list[dict]:
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
    again = merge.plan_merge(next_base, W.build_ours(doc, next_base, tmp), after)
    if not merge.has_writes(again, [s["objectId"] for s in after["slides"]]):
        return []
    busy = [f"{p['key']}: {p['action']}" + (f" {[u['key'] for u in p['units'] if u['action'] != 'keep']}"
                                            if p["action"] == "update" else "")
            for p in again["slides"]
            if p["action"] in ("create", "delete") or (p["action"] == "update" and (
                any(u["action"] in ("create", "recreate", "delete", "move") for u in p["units"])
                or p.get("background") or p.get("notes") is not None))]
    return [loss_oracle.finding("second_sync_writes", "note" if unsure else "report",
                                "the same source synced again would write: " + ("; ".join(busy) or "another slide order"),
                                slide=(busy[0].split(":")[0] if busy else "order"))]


def _draw(rng: random.Random, deck_ops, source_ops):
    if deck_ops is None:
        deck_ops = [rng.choice(sorted(DECK_OPS)) for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
    if source_ops is None:
        source_ops = [rng.choice(sorted(SOURCE_OPS)) for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.4:
            source_ops.append("collide")   # worth more than any other draw (see `src_collide`)
    return list(deck_ops), list(source_ops)


def offline_chain(seed: int, chain: int = 1, ops=None, work: Path | None = None,
                  shape: str = "converted", first_sync: bool = False) -> dict:
    """`chain` edit+sync steps on one deck. Each sync starts from the base the previous one wrote
    (`fuzz_world.rebase`), which is where a sync undoing what the last one merged would show.
    `ops`: per step `{"deck": [...], "source": [...]}` (default: drawn from the seed).
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
        steps = []
        for step in range(chain):
            want = (ops[step] if ops and step < len(ops) else None) or {}
            deck_ops, source_ops = _draw(rng, want.get("deck"), want.get("source"))
            # Every step rebases: the next step needs that base, and the last step's base is what
            # the "a second sync writes nothing" check judges.
            record = _sync_step(seed, step, doc, base, live, tmp, deck_ops, source_ops, rebase=True)
            steps.append(record)
            doc, live = record.pop("doc"), record["state"]["after"]
            base = record["state"]["next_base"] or base
        return {"seed": seed, "chain": chain, "shape": shape, "steps": steps, "first_sync": first_sync,
                "ops": [{"deck": s["deck_ops"], "source": s["source_ops"]} for s in steps],
                "refused": [r for s in steps for r in s.get("refused") or []],
                "held": [h for s in steps for h in s.get("held") or []],
                "failures": [f for s in steps for f in s["failures"]]}
    finally:
        if work is None:
            shutil.rmtree(tmp, ignore_errors=True)


def offline_round(seed: int, source_ops=None, deck_ops=None, work: Path | None = None,
                  shape: str = "converted", first_sync: bool = False) -> dict:
    """One offline round. `source_ops` / `deck_ops`: op names (default: drawn from the seed)."""
    return offline_chain(seed, 1, [{"deck": deck_ops, "source": source_ops}], work, shape,
                         first_sync)["steps"][0]


def shrink_offline(result: dict, limit: int = 200, shape: str = "converted",
                   first_sync: bool = False) -> dict:
    """Drop edits one at a time while the round keeps failing."""
    best = result
    tries = 0
    for field in ("deck_ops", "source_ops"):
        changed = True
        while changed and tries < limit:
            changed = False
            for i in range(len(best[field])):
                if len(best[field]) <= 1 and field == "deck_ops":
                    break
                ops = best[field][:i] + best[field][i + 1:]
                tries += 1
                candidate = offline_round(best["seed"], shape=shape, first_sync=first_sync,
                                          **{"source_ops": best["source_ops"], "deck_ops": best["deck_ops"],
                                             field: ops})
                if candidate["failures"]:
                    best, changed = candidate, True
                    break
    return best


def shrink_chain(result: dict, limit: int = 300) -> dict:
    """Drop edits one at a time, in the first failing step and the ones before it."""
    failing = next(i for i, s in enumerate(result["steps"]) if s["failures"])
    shape, first_sync = result.get("shape", "converted"), result.get("first_sync", False)
    best = {**result, "ops": result["ops"][:failing + 1], "chain": failing + 1}
    tries = 0
    for step in range(failing, -1, -1):
        for field in ("deck", "source"):
            changed = True
            while changed and tries < limit:
                changed = False
                for i in range(len(best["ops"][step][field])):
                    ops = [dict(o) for o in best["ops"]]
                    ops[step][field] = ops[step][field][:i] + ops[step][field][i + 1:]
                    if step == failing and field == "deck" and not ops[step][field]:
                        continue
                    tries += 1
                    candidate = offline_chain(best["seed"], best["chain"], ops, shape=shape,
                                              first_sync=first_sync)
                    if candidate["steps"][-1]["failures"]:
                        best, changed = candidate, True
                        break
    return best


def describe_chain(result: dict) -> str:
    lines = []
    for s in result["steps"]:
        lines.append(f"  step {s['step']}: deck:   " + "; ".join(s["deck"]))
        lines.append(f"          source: " + "; ".join(s["source"]))
        if s.get("refused"):
            lines.append("          refused: " + ", ".join(s["refused"]) + " (nothing written)")
        if s["failures"]:
            lines.append(loss_oracle.describe(s["failures"]))
    return "\n".join(lines)


def run_offline(rounds: int, seed0: int, shrink: bool, quiet: bool = False, chain: int = 1,
                shape: str = "converted", first_sync: bool = False) -> list[dict]:
    bad = []
    refused: dict[str, int] = {}
    held: dict[str, int] = {}
    for seed in range(seed0, seed0 + rounds):
        result = offline_chain(seed, chain, shape=shape, first_sync=first_sync)
        for reason in result.get("refused") or []:
            refused[reason] = refused.get(reason, 0) + 1
        for what in result.get("held") or []:
            held[what] = held.get(what, 0) + 1
        if result["failures"]:
            bad.append(shrink_chain(result) if shrink else result)
            if not quiet:
                print(f"seed {seed}: {len(result['failures'])} finding(s)")
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


def _slide_title(spec: dict) -> str | None:
    """The slide title an edit spec points at (`{"slide": {"title": ...}}` or a plain title)."""
    sel = (spec.get("args") or {}).get("slide")
    return sel.get("title") if isinstance(sel, dict) else sel


def sync_build():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_build", ROOT / "tests" / "decks" / "sync" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unique_slide(model, s):
    title = s.title
    if title and len(model.find({"title": title})) == 1:
        return {"title": title}
    return {"index": s.index}


def _lines(el):
    return [l.strip() for raw, _ in el.texts for l in raw.split("\n") if len(l.strip().split()) >= 3]


def _free_box(rng, s, candidates, kind):
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


def random_spec(model, rng, donor=None, focus=None, aim=()):
    """One human-like edit spec for the live deck, found by content (tools/deck_edits.py kinds).
    `focus`: a name in FOCUS - draw one of its aims instead of a uniform kind (`focus_spec`), on
    one of the slides in `aim` (objectIds) most of the time."""
    if focus:
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
        args = {"slide": sel, "word": rng.choice(words), "context": line}
        if kind == "recolour":
            args["color"] = rng.choice(["#c00000", "#0070c0"])
        return {"edit": kind, "args": args}
    if kind == "resize_font":
        return {"edit": "resize_font", "args": {"slide": sel, "text": rng.choice(_lines(rng.choice(texts))),
                                                "size": rng.choice([14, 16, 20])}}
    if kind == "move":
        target = {"image": "largest"} if images and rng.random() < 0.4 else {"text": rng.choice(_lines(rng.choice(texts)))}
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
                                                                      "box": box, "color": "#ffc000"}}
    if kind == "add_image":
        box = _free_box(rng, s, [[x, y, 140, 90] for x in (500, 540) for y in (250, 280)], "image")
        return None if box is None else {"edit": "add_image", "args": {"slide": sel, "url": donor, "box": box}}
    if kind == "duplicate":
        return {"edit": "duplicate", "args": {"slide": sel, "target": {"text": rng.choice(_lines(rng.choice(texts)))},
                                              "dx": 0, "dy": rng.choice([90, 110])}}
    if kind == "group":
        tops = [e for e in s.elements if not e.groups and e.kind in ("shape", "image") and (e.text or e.kind == "image")]
        if len(tops) < 2:
            return None
        a, b = rng.sample(tops, 2)
        targets = [{"text": a.text[:50]} if a.text else {"image_near": [round(v, 1) for v in a.center]},
                   {"text": b.text[:50]} if b.text else {"image_near": [round(v, 1) for v in b.center]}]
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


def _placeholder(e) -> str | None:
    return (e.obj.get("shape", {}).get("placeholder") or {}).get("type") if e.kind == "shape" else None


def _body_texts(s):
    """Text boxes of a slide the person would write in: not its title, not the frame counter."""
    return [e for e in s.elements if e.kind == "shape" and e.texts and _lines(e)
            and _placeholder(e) not in ("TITLE", "CENTERED_TITLE") and not FOOTER.match(e.text)]


def _aim_long_text(rng, model, s, sel, donor):
    el = rng.choice(_body_texts(s) or [None])
    return el and {"edit": "append_sentence", "args": {"slide": sel, "text": rng.choice(_lines(el)),
                                                       "sentence": f"{rng.choice(REMARKS)} ({rng.randrange(1000)})."}}


def _aim_new_paragraph(rng, model, s, sel, donor):
    el = rng.choice(_body_texts(s) or [None])
    return el and {"edit": "add_paragraph", "args": {"slide": sel, "text": rng.choice(_lines(el)),
                                                     "paragraph": f"A point the person added, number {rng.randrange(1000)}"}}


def _aim_hole(rng, model, s, sel, donor):
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


def _aim_move_text(rng, model, s, sel, donor):
    el = rng.choice(_body_texts(s) or [None])
    return el and {"edit": "move", "args": {"slide": sel, "target": {"text": rng.choice(_lines(el))},
                                            "dx": rng.choice([-30, 0, 30]), "dy": rng.choice([-30, 30, 60])}}


def _aim_note_below(rng, model, s, sel, donor):
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


def _grouped(s):
    return [e for e in s.elements if e.groups and e.kind in ("shape", "table") and e.text and not FOOTER.match(e.text)]


def _aim_group_move(rng, model, s, sel, donor):
    el = rng.choice(_grouped(s) or [None])
    return el and {"edit": "move", "args": {"slide": sel, "target": {"text": el.text[:50]},
                                            "dx": rng.choice([-20, 20]), "dy": rng.choice([-20, 20, 40])}}


def _aim_group_resize(rng, model, s, sel, donor):
    el = rng.choice(_grouped(s) or [None])
    return el and {"edit": "resize", "args": {"slide": sel, "target": {"text": el.text[:50]},
                                              "sx": rng.choice([0.85, 1.15]), "sy": rng.choice([0.85, 1.0, 1.15])}}


def _aim_group_pair(rng, model, s, sel, donor):
    """The person groups a text of the converter's with its neighbour."""
    tops = [e for e in s.elements if not e.groups and e.kind in ("shape", "image") and (e.text or e.kind == "image")
            and not FOOTER.match(e.text) and _placeholder(e) not in ("TITLE", "CENTERED_TITLE")]
    texts = [e for e in tops if e.text]
    if len(tops) < 2 or not texts:
        return None
    a = rng.choice(texts)
    b = rng.choice([e for e in tops if e is not a])
    return {"edit": "group", "args": {"slide": sel, "targets": [
        {"text": a.text[:50]}, {"text": b.text[:50]} if b.text else {"image_near": [round(v, 1) for v in b.center]}]}}


def _aim_narrow(rng, model, s, sel, donor):
    el = rng.choice(_body_texts(s) or [None])
    return el and {"edit": "resize", "args": {"slide": sel, "target": {"text": rng.choice(_lines(el))},
                                              "sx": rng.choice([0.7, 0.8]), "sy": 1.0}}


def _aim_cell(rng, model, s, sel, donor):
    """A table cell made long enough to wrap: the row grows, and the table with it."""
    tables = [e for e in s.elements if e.kind == "table"]
    if not tables:
        return None
    cells = [" ".join(raw.split()) for raw, _ in rng.choice(tables).texts if raw.strip()]
    if not cells:
        return None
    return {"edit": "append_sentence", "args": {"slide": sel, "text": rng.choice(cells),
                                                "sentence": f"(measured again by hand, {rng.randrange(1000)} runs)"}}


def _table_anchor(rng, s):
    tables = [e for e in s.elements if e.kind == "table"]
    if len(tables) != 1:
        return None, None
    cells = [" ".join(raw.split()) for raw, c in tables[0].texts if raw.strip() and c]
    return (tables[0], rng.choice(cells)) if cells else (None, None)


def _aim_row(rng, model, s, sel, donor):
    """A row the person added: the table is taller than the source's before the source moves it."""
    table, text = _table_anchor(rng, s)
    if not table:
        return None
    n = rng.randrange(1000)
    words = [f"Case {n}", f"{rng.randint(50, 100)}%", f"{rng.randint(10, 99) / 10} s", "by hand"]
    return {"edit": "insert_table_row", "args": {"slide": sel, "text": text,
                                                 "cells": words[:table.obj["table"]["columns"]]}}


def _aim_column(rng, model, s, sel, donor):
    table, text = _table_anchor(rng, s)
    if not table:
        return None
    n = rng.randrange(1000)
    cells = [f"Note {n}"] + [f"r{n}-{i}" for i in range(1, table.obj["table"]["rows"])]
    return {"edit": "insert_table_column", "args": {"slide": sel, "text": text, "cells": cells}}


AIMS = {"long_text": _aim_long_text, "new_paragraph": _aim_new_paragraph, "hole": _aim_hole,
        "move_text": _aim_move_text, "note_below": _aim_note_below, "group_move": _aim_group_move,
        "group_resize": _aim_group_resize, "group_pair": _aim_group_pair, "narrow": _aim_narrow,
        "cell": _aim_cell, "row": _aim_row, "column": _aim_column}
# aim -> weight; "uniform" is one draw of the default generator, so a focused campaign still
# touches everything the general one does, only less often.
FOCUS = {"layout": {"long_text": 3, "new_paragraph": 2, "hole": 3, "move_text": 2, "note_below": 3,
                    "group_move": 2, "group_resize": 1, "group_pair": 1, "narrow": 1, "cell": 2,
                    "row": 2, "column": 1, "uniform": 3}}
# the same aims, on a round that starts from the layout probe frames (`start_variant`)
FOCUS["probes"] = FOCUS["layout"]
AIMED = 0.75   # how often a focused edit goes to a slide the step's variant changes


def focus_spec(model, rng, donor, weights: dict, aim=()):
    """One edit drawn for an aim (`FOCUS`), on a slide of `aim` (objectIds) with probability AIMED."""
    names = sorted(weights)
    name = rng.choices(names, [weights[n] for n in names])[0]
    if name == "uniform":
        return random_spec(model, rng, donor)
    slides = [s for s in model.slides if s.elements]
    aimed = [s for s in slides if s.id in set(aim)]
    if not slides:
        return None
    s = rng.choice(aimed) if aimed and rng.random() < AIMED else rng.choice(slides)
    spec = AIMS[name](rng, model, s, _unique_slide(model, s), donor)
    if spec:
        spec["aim"] = name
    return spec


# Variants and the slides they change, for the focused draw. A flag that re-lays text on a slide
# (rewords it, adds or removes a line, changes a formula, a block, a table) is what a layout defect
# needs from the source side; one that only renames, reorders or notes is worth less to it.
REFLOWS = {"reword": 1, "addbullet": 1, "removebullet": 1, "formula": 1, "blockedit": 1, "tablemove": 1,
           "tablerow": 1, "numbers": 1, "tablecell": 0.5, "figure": 0.5, "retitle": 0.25, "untitled": 0.25}


def start_variant(focus: str | None) -> str:
    """The variant a round converts first: v1, or `probes` for --focus probes (the layout probe
    frames of tests/decks/sync, which only its probes-* variants change)."""
    return "probes" if focus == "probes" else "v1"


def variant_weights(build, focus: str | None) -> dict[str, float]:
    """variant -> weight of being drawn: uniform by default, by what it re-lays when focused."""
    if focus == "probes":
        return {v: 1.0 for v in build.VARIANTS if v.startswith("probes-")}
    variants = [v for v in build.VARIANTS if v != "v1"]
    if not focus:
        return {v: 1.0 for v in variants}
    return {v: 0.25 + sum(REFLOWS.get(f, 0) for f in build.VARIANTS[v]) for v in variants}


def variant_slides(build, variant: str) -> set[str]:
    """The titles of the slides `variant` changes (build.INTENDED, whose items are "<title>: ...",
    and PROBE_INTENDED for the probe edits; a slide it adds or deletes is no place for the
    person's edit)."""
    out = set()
    intended = {**build.INTENDED, **{k: v for k, v in getattr(build, "PROBE_INTENDED", {}).items() if v}}
    for flag in build.VARIANTS[variant]:
        for item in intended.get(flag, []):
            if ": " in item and not item.startswith(("slide+", "slide-", "order ")):
                out.add(item.split(": ", 1)[0])
    if "reorder" in build.VARIANTS[variant]:
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


def stale(deck, spec: dict) -> bool:
    """Whether a deferred `deck` must send what it queued and read again before `spec` can be found
    the way a fresh read would find it: the edit's slide has queued changes, slides came, went or
    moved and the edit is about slide order, or its selector cannot be told apart without a read
    (a `contains` selector reads text on every slide, an index counts slides)."""
    if not getattr(deck, "defer", False) or not deck.pending:
        return False
    args = spec.get("args") or {}
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


class Cost:
    """Where a live round's time goes: seconds, Google calls, retries and the seconds slept backing
    off, per phase. What this thread calls is counted by `gslides.count_thread`; what a `convert` or
    `sync` subprocess calls comes back in the file `devtools.counted` writes (`subprocess`). Each
    phase is also added to the step it ran in, so round.json says both."""

    KEYS = ("calls", "retries", "backoff_s", "rate_limited")

    def __init__(self):
        from beamer2slides.gslides import count_thread
        self.api = count_thread()   # (a round runs on one thread of the pool, start to end)
        self.phases: dict[str, Counter] = {}
        self.step: dict[str, Counter] | None = None

    @classmethod
    def kept(cls, key: str) -> bool:
        # (call <methodId>: what was asked - writes are what the per-user quota counts; retry
        #  <methodId> <status>: who was refused)
        return key in cls.KEYS or key.startswith(("call ", "retry "))

    def start(self, name: str):
        return name, time.monotonic(), Counter({k: v for k, v in self.api.items() if self.kept(k)})

    def stop(self, mark, extra: dict | None = None, **counts) -> None:
        name, t0, was = mark
        d = Counter({"s": time.monotonic() - t0, "n": 1})
        d.update({k: v - was[k] for k, v in list(self.api.items()) if self.kept(k)})
        d.update({k: v for k, v in (extra or {}).items() if self.kept(k)})
        d.update(counts)
        for into in (self.phases, self.step):
            if into is not None:
                into.setdefault(name, Counter()).update(d)

    @staticmethod
    def subprocess(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def plain(phases: dict) -> dict:
        return {k: {f: round(v, 2) if isinstance(v, float) else v for f, v in c.items() if v}
                for k, c in phases.items()}


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

    def __init__(self, out: Path, log):
        self.out, self.log = out, log

    def make(self, build, variant: str = "v1"):
        """Convert `variant` into `out` once (the deck is kept until the run drops it)."""
        self.out.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "beamer2slides.devtools.counted", "convert", str(build.build(variant)),
                               "--out", str(self.out)], env=ENV, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        if done.returncode:
            raise RuntimeError(f"the template conversion failed, see {self.log.name}")
        self.pid = json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        return self

    def copy_into(self, out: Path, name: str) -> str:
        """A copy of the template deck, and `out` set up as its convert folder. Returns its id."""
        from beamer2slides import snapshot
        from beamer2slides.google_auth import drive_service
        from beamer2slides.gslides import execute
        drive = drive_service()
        pid = execute(drive.files().copy(fileId=self.pid, fields="id,appProperties", body={"name": name}))["id"]
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


class LiveRound:
    def __init__(self, seed: int, out: Path, chain: int, keep_decks: bool, edits: str = "batched",
                 focus: str | None = None, template: Template | None = None):
        self.seed, self.out, self.chain, self.keep = seed, out, chain, keep_decks
        self.edits, self.focus, self.template = edits, focus, template
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(self.out / "fuzz.log", "w", encoding="utf-8")
        self.problems: list[str] = []
        self.record: dict = {"seed": seed, "steps": [], "edits_mode": edits, "focus": focus,
                             "reuse": template is not None}
        self.loose: set[str] = set()  # slides whose groups the person took apart, in any step so far
        self.cost = Cost()
        self.pres_after = None        # the last read of the deck after a sync (the next step's model)

    def cli(self, *args, check=True):
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        stats = self.out / f"api-{args[0]}.json"
        stats.unlink(missing_ok=True)
        mark = self.cost.start(args[0])
        done = subprocess.run([sys.executable, "-m", "beamer2slides.devtools.counted", *map(str, args)],
                              env={**ENV, "B2S_API_STATS": str(stats)}, cwd=ROOT,
                              stdout=self.log, stderr=subprocess.STDOUT)
        self.cost.stop(mark, Cost.subprocess(stats))
        if check and done.returncode:
            raise RuntimeError(f"beamer2slides {args[0]} failed, see {self.out / 'fuzz.log'}")
        return done

    def read(self):
        from beamer2slides.gslides import execute
        return execute(self.deck.api.presentations().get(presentationId=self.deck.pid))

    def snapshot(self):
        from beamer2slides import snapshot
        pres = self.read()
        read = snapshot.read_presentation(pres)
        snapshot.sign_pictures(read, pres)
        return pres, read

    def start_deck(self, build):
        """The round's v1 deck: converted, or copied from the template (`--reuse`)."""
        from .deck_edits import LiveDeck
        if self.template is None:
            self.cli("convert", build.build(start_variant(self.focus)), "--out", self.out)
            pid = None
        else:
            mark = self.cost.start("copy")
            pid = self.template.copy_into(self.out, f"b2s fuzz {self.out.parent.name}/{self.out.name}")
            self.cost.stop(mark)
        pid = pid or json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"]
        mark = self.cost.start("edits")
        self.deck = LiveDeck(pid, defer=self.edits == "batched")
        self.cost.stop(mark, reads=1)
        base = json.loads((self.out / "sync" / "base.json").read_text(encoding="utf-8"))
        self.slide_ids = {s.get("title"): s.get("objectId") for s in base["slides"]}  # v1 title -> slide
        if self.template is not None:
            # The copy is only a converted deck if every object the base names is in it by that id.
            have = {s.id for s in self.deck.model.slides} | {e.id for s in self.deck.model.slides for e in s.elements}
            named = {s["objectId"] for s in base["slides"]} | {
                o for s in base["slides"] for e in s.get("elements") or [] for o in e.get("objects") or []}
            missing = sorted(named - have)
            self.record["reuse_ids"] = {"named": len(named), "missing": len(missing)}
            if missing:
                raise RuntimeError(f"the copy of the template lacks {len(missing)} ids the base names: {missing[:5]}")
        self.record["deck"] = f"https://docs.google.com/presentation/d/{self.deck.pid}/edit"

    def run(self, specs: list[list[dict]] | None = None, replay_variants: list[str] | None = None):
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
            variant = rng.choices(variants, [weights[v] for v in variants])[0] if self.focus else None
            aim = {self.slide_ids.get(t) for t in variant_slides(build, variant)} - {None} if variant else set()
            mark = self.cost.start("edits")
            reads, writes = self.deck.reads, self.deck.writes
            done = self.edit_step(rng, specs[step] if specs is not None and step < len(specs) else None, aim)
            self.cost.stop(mark, reads=self.deck.reads - reads, writes=self.deck.writes - writes, edits=len(done))
            variant = variant or rng.choice(variants)
            if replay_variants and step < len(replay_variants):
                variant = replay_variants[step]   # (a replay's edits drew nothing: the dice moved on)
            self.step(step, done, variant, build)
        self.record["cost"] = {"seconds": round(time.monotonic() - started, 1), "phases": Cost.plain(self.cost.phases)}
        return self.problems

    def edit_step(self, rng, replay: list[dict] | None, aim=()) -> list[dict]:
        """The person's edits of one step: replayed, or drawn (`random_spec`) and applied.

        `edits="reread"` is the way it always was: the deck read at the start, an edit sent as its
        own batchUpdate, the deck read again after each. `"batched"` (`LiveDeck(defer=True)`) reads
        once - the read the last sync step already made, when there is one - queues the edits, and
        reads again only when a drawn edit falls on a slide a queued one has touched (it is then
        drawn again from the new read: every edit is found in a read that is current for its slide,
        which is what the reread way gives too). A step is one or two batchUpdates instead of eight."""
        from .deck_edits import apply as apply_edit, donor_from
        deck = self.deck
        if deck.defer and self.pres_after is not None:
            deck.adopt(self.pres_after)
        elif deck.defer and deck.pending:
            deck.flush()
        else:
            deck.read()
        try:
            donor = donor_from(deck.model.pres)
        except Exception:  # noqa: BLE001 (a deck with no picture left to borrow)
            donor = None
        done, queued = [], []

        def flush():
            refused = set(deck.flush())
            for i, spec in enumerate(queued):
                if i in refused:
                    self.log.write(f"refused {json.dumps(spec)}\n")
                else:
                    done.append(spec)
            queued.clear()

        def attempt(spec, what):
            n = len(deck.pending)
            try:
                apply_edit(deck, spec)
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
                spec = random_spec(deck.model, rng, donor, self.focus, aim)
                if spec and stale(deck, spec):
                    flush()
                    deck.read()
                    spec = random_spec(deck.model, rng, donor, self.focus, aim)
                if spec:
                    attempt(spec, "skipped")
        flush()
        return done

    def step(self, step: int, specs: list[dict], variant: str, build):
        from beamer2slides import snapshot
        folder = self.out / f"step{step}"
        folder.mkdir(exist_ok=True)
        base = json.loads((self.out / "sync" / "base.json").read_text(encoding="utf-8"))
        (folder / "base.json").write_text(json.dumps(base), encoding="utf-8")
        mark = self.cost.start("snapshot")
        pres_before, before = self.snapshot()
        self.cost.stop(mark)
        (folder / "before.json").write_text(json.dumps(before), encoding="utf-8")
        (folder / "edits.json").write_text(json.dumps(specs, indent=1, ensure_ascii=False), encoding="utf-8")
        mark = self.cost.start("build")
        pdf = build.build(variant)
        self.cost.stop(mark)
        self.cli("sync", pdf, "--deck", self.out)
        report = json.loads((self.out / "sync" / "sync-report.json").read_text(encoding="utf-8"))
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        mark = self.cost.start("snapshot")
        pres_after, after = self.snapshot()
        self.cost.stop(mark)
        (folder / "after.json").write_text(json.dumps(after), encoding="utf-8")
        judged = self.cost.start("judge")
        ours = self.ours(pdf, base, folder)
        findings = loss_oracle.check(base, before, after, report, ours)
        (folder / "findings.json").write_text(json.dumps(findings, indent=1, ensure_ascii=False), encoding="utf-8")
        self.record["steps"].append({"step": step, "variant": variant, "edits": specs,
                                     "findings": loss_oracle.failures(findings)})
        self.problems += [f"step {step} ({variant}): {loss_oracle.describe([f])}" for f in loss_oracle.failures(findings)]
        from beamer2slides.devtools import layout_oracle
        layout = layout_oracle.check(base, before, after, report, ours)
        (folder / "layout.json").write_text(json.dumps(layout, indent=1, ensure_ascii=False), encoding="utf-8")
        self.problems += [f"step {step} ({variant}): layout: {layout_oracle.describe([f]).strip()}"
                          for f in layout_oracle.failures(layout)]
        self.problems += [f"step {step} ({variant}): integrity: {p}" for p in self.integrity(pres_before, pres_after, specs)]
        self.cost.stop(judged)
        self.after_step(base, before, after, report, pres_after, folder)

    def after_step(self, base, before, after, report, pres_after, folder: Path):
        """What a step cost, which layout preconditions it reached (`fuzz_reach`) and what the layout
        oracle said of it (`layout.json`, written by the judging lines above: kind -> severity
        counts), onto the step record; and the read after the sync kept as the next step's model.
        The reach is the proxy, the oracle the verdict: a campaign whose reach is high and whose
        findings stay at zero is one whose oracle to question next."""
        from . import fuzz_reach
        rec = self.record["steps"][-1]
        rec["cost"] = Cost.plain(self.cost.step or {})
        try:
            layout = json.loads((folder / "layout.json").read_text(encoding="utf-8"))
            rec["layout"] = {f"{f['kind']}:{f['severity']}": sum(1 for g in layout if (g["kind"], g["severity"]) ==
                                                                   (f["kind"], f["severity"])) for f in layout}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            rec["reach"] = {k: v for k, v in fuzz_reach.step_reach(base, before, after, report).items() if v}
        except Exception as e:  # noqa: BLE001 (a proxy must never fail a round)
            self.log.write(f"reach: {type(e).__name__}: {e}\n")
        self.pres_after = pres_after

    def ours(self, pdf: Path, base: dict, folder: Path):
        """The new conversion the sync used, rebuilt offline (no Google call) so the oracle can tell
        a word the source rewrote from a word that vanished."""
        from beamer2slides.sync import build_ours
        try:
            with PDFIUM:
                return build_ours(pdf, folder / "ours", base)
        except Exception as e:  # noqa: BLE001
            self.log.write(f"could not rebuild ours: {e}\n")
            return None

    def integrity(self, pres_before, pres_after, specs: list[dict] = ()):
        from . import sync_check as sc
        base = json.loads((self.out / "sync" / "base.json").read_text(encoding="utf-8"))
        # A group the person took apart (or a member they deleted) is theirs: the sync rebuilding
        # the unit ungrouped is the policy, not a broken deck. It stays theirs for the rest of the
        # chain - a later step must not be accused of the group step 0 dissolved - so the slides
        # add up over the steps. The slide is remembered by its objectId as well as by the title the
        # edit named it with, because the sync of this very step may retitle it: seed 607 ungrouped
        # a figure on "Why decks and sources diverge" and synced `retitle`, which calls that frame
        # "Why decks drift away from their source", and the excuse missed the slide it was written for.
        titles = {_slide_title(spec) for spec in specs
                  if spec["edit"] in ("ungroup", "group", "delete_element", "delete_group", "duplicate")}
        titles.discard(None)
        self.loose |= titles | {s.id for s in sc.Model(pres_before).slides if s.title in titles}
        # Ctrl+D on a slide copies it as the person left it, so the copy of a slide whose group they
        # took apart is ungrouped by their hand too, and the excuse written for the original has to
        # follow it (live seed 900, step 4: they ungrouped a figure on "Why decks and sources
        # divergeed" and then duplicated that very slide twice; "Copy 807" - a slide sync never
        # writes a request to - was accused of it at every step after). The copy is named by the
        # title the edit gave it, which is the person's, not the source's.
        self.loose |= {spec["args"].get("new_title") for spec in specs
                       if spec["edit"] == "duplicate_slide" and _slide_title(spec) in self.loose}
        self.loose.discard(None)
        return sc.integrity(sc.Model(pres_after), before=sc.Model(pres_before), base_ids=sc.ids_in(base),
                            allow_ungrouped=self.loose, allow_groups_changed=self.loose)

    def drop_deck(self, pid: str | None = None):
        from beamer2slides import snapshot
        from beamer2slides.google_auth import drive_service
        from beamer2slides.gslides import execute
        drive = drive_service()
        pid = pid or self.deck.pid
        try:
            info = execute(drive.files().get(fileId=pid, fields="appProperties"))
            fid = (info.get("appProperties") or {}).get(snapshot.BASE_PROPERTY)
            if fid:
                execute(drive.files().delete(fileId=fid))
            execute(drive.files().delete(fileId=pid))
        except Exception as e:  # noqa: BLE001
            self.log.write(f"could not delete the deck: {e}\n")

    def clear_previous(self):
        """A failing round keeps its folder, and the deck in Drive that goes with it. Running that
        seed again - which is the first thing one does after a finding - would then convert onto a
        deck this harness itself has edited, and `convert` rightly refuses to rebuild over somebody's
        edits. So a round starts from nothing: the deck the last run of this seed made goes first,
        then the folder it wrote (the log stays: it is open, and it is this run's)."""
        emit = self.out / "emit.json"
        if emit.exists():
            pid = None
            try:
                pid = json.loads(emit.read_text(encoding="utf-8")).get("presentationId")
            except Exception as e:  # noqa: BLE001
                self.log.write(f"could not read the last run's deck id: {e}\n")
            if pid:
                self.log.write(f"dropping the deck the last run of this seed left behind: {pid}\n")
                self.drop_deck(pid)
        for p in self.out.iterdir():
            if p.name == "fuzz.log":
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def live_round(seed: int, out_root: Path, chain: int, keep_decks: bool, specs=None, variants=None, **opts) -> dict:
    """One live round; `opts` are LiveRound's (edits, focus, template). A replay passes the recorded
    `specs` and `variants` (without them the replayed steps would draw other variants)."""
    r = LiveRound(seed, out_root / f"r{seed:03}", chain, keep_decks, **opts)
    try:
        problems = r.run(specs, variants)
        r.record["problems"] = problems
        (r.out / "round.json").write_text(json.dumps(r.record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        if not problems and not keep_decks:
            r.drop_deck()
            r.record["deck"] = "(deleted: the round passed)"
        return r.record
    except Exception as e:  # noqa: BLE001
        r.log.flush()
        why = known_cause(r.out / "fuzz.log")
        r.record["problems"] = [f"{type(e).__name__}: {e}" + (f" [{why}]" if why else "")]
        r.record.setdefault("cost", {"phases": Cost.plain(r.cost.phases)})
        (r.out / "round.json").write_text(json.dumps(r.record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        return r.record
    finally:
        r.log.close()


def shrink_live(record: dict, out_root: Path, chain: int, **opts) -> dict:
    """Replay the round with one edit dropped at a time (the last failing step only)."""
    steps = [s["edits"] for s in record["steps"]]
    failing = next((i for i, s in enumerate(record["steps"]) if s["findings"]), None)
    if failing is None:
        return record
    steps = steps[:failing + 1]
    best = record
    i = 0
    while i < len(steps[failing]):
        trial = [list(x) for x in steps]
        dropped = trial[failing].pop(i)
        got = live_round(record["seed"], out_root / "shrink", failing + 1, True, trial,
                         [s["variant"] for s in record["steps"]], **opts)
        if any(s["findings"] for s in got["steps"]):
            steps, best = trial, got
            print(f"  shrink: dropping {dropped['edit']} keeps it failing ({len(steps[failing])} edits left)")
        else:
            i += 1
    best["shrunk_edits"] = steps
    return best


WRITE = "call slides.presentations.batchUpdate"   # what the per-user write quota counts


def summary(records: list[dict], wall: float, parallel: int) -> str:
    """What the run cost and reached: rounds/hour at this --parallel, seconds per step, each
    phase's seconds per step, Google calls, retries and backoff (all threads and subprocesses),
    and how many steps reached each layout precondition (devtools/fuzz_reach.py)."""
    phases: dict[str, Counter] = {}
    for r in records:
        for name, c in ((r.get("cost") or {}).get("phases") or {}).items():
            phases.setdefault(name, Counter()).update(c)
    steps = sum(len(r.get("steps") or []) for r in records) or 1
    total = Counter()
    for c in phases.values():
        total.update({k: c.get(k, 0) for k in ("calls", "retries", "backoff_s", "rate_limited", WRITE)})
    busy = sum((r.get("cost") or {}).get("seconds", 0) for r in records)
    order = ("convert", "copy", "edits", "snapshot", "build", "sync", "judge")
    per = "  ".join(f"{k} {phases[k]['s'] / steps:.1f}" for k in order if k in phases)
    reach = Counter(p for r in records for s in r.get("steps") or [] for p in (s.get("reach") or {}))
    layout = Counter(k for r in records for s in r.get("steps") or [] for k in (s.get("layout") or {}))
    from .fuzz_reach import PRECONDITIONS
    return (f"cost: {len(records)} rounds, {steps} steps in {wall:.0f} s at --parallel {parallel}: "
            f"{3600 * len(records) / max(wall, 1):.1f} rounds/h, {busy / steps:.1f} s per step (one round's clock)\n"
            f"  s per step by phase: {per}\n"
            f"  Google: {total['calls']:.0f} calls ({total['calls'] / steps:.0f}/step), {total['retries']:.0f} retries, "
            f"{total['rate_limited']:.0f} rate-limited, {total['backoff_s']:.0f} s backing off, "
            f"{60 * total[WRITE] / max(wall, 1):.0f} batchUpdates/min\n"
            f"  reach (steps): " + ", ".join(f"{p} {reach[p]}" for p in PRECONDITIONS) + "\n"
            f"  layout oracle (steps with a finding): " + (", ".join(f"{k} {n}" for k, n in sorted(layout.items())) or "none"))


def run_live(rounds: int, seed0: int, parallel: int, chain: int, keep_decks: bool, out_root: Path, shrink: bool,
             edits: str = "batched", focus: str | None = None, reuse: bool = False):
    out_root.mkdir(parents=True, exist_ok=True)
    seeds = list(range(seed0, seed0 + rounds))
    started = time.monotonic()
    template = None
    if reuse:
        with open(out_root / "template.log", "w", encoding="utf-8") as log:
            template = Template(out_root / "_template", log).make(sync_build(), start_variant(focus))
    opts = {"edits": edits, "focus": focus, "template": template}
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            records = list(pool.map(lambda s: live_round(s, out_root, chain, keep_decks, **opts), seeds))
        wall = time.monotonic() - started
        bad = [r for r in records if r.get("problems")]
        for r in bad:
            print(f"\nseed {r['seed']}: {len(r['problems'])} problem(s), {r.get('deck')}")
            for p in r["problems"]:
                print(f"  {p}")
            if shrink and r.get("steps"):
                shrink_live(r, out_root, chain, **opts)
    finally:
        if template is not None and not keep_decks:
            dropper = LiveRound.__new__(LiveRound)
            dropper.log = sys.stderr
            dropper.drop_deck(template.pid)
    print(f"\n{len(records) - len(bad)}/{len(records)} live rounds clean")
    line = summary(records, wall, parallel)
    print(line)
    (out_root / "summary.json").write_text(json.dumps({"rounds": len(records), "wall": round(wall, 1), "parallel": parallel,
                                                        "chain": chain, "edits": edits, "focus": focus, "reuse": reuse,
                                                        "seeds": seeds, "summary": line}, indent=1), encoding="utf-8")
    return bad


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
    ap.add_argument("--edits", choices=["batched", "reread"], default="batched",
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

    shape = "adopt" if args.first_sync else args.shape
    if args.mode == "offline":
        if args.replay is not None:
            result = offline_chain(args.replay, args.chain, shape=shape, first_sync=args.first_sync)
            print(describe_chain(result))
            for s in result["steps"]:
                if s.get("message"):
                    print(s["message"])
            print(loss_oracle.describe([f for s in result["steps"] for f in s["findings"]]) or "nothing lost")
            return 1 if result["failures"] else 0
        started = time.monotonic()
        bad = run_offline(args.rounds, args.seed, not args.no_shrink, chain=args.chain, shape=shape,
                          first_sync=args.first_sync)
        print(f"{args.rounds - len(bad)}/{args.rounds} offline rounds clean in {time.monotonic() - started:.1f} s")
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
    if args.replay is not None:
        folder = args.out / f"r{args.replay:03}"
        specs = variants = None
        if (folder / "round.json").exists():
            steps = json.loads((folder / "round.json").read_text(encoding="utf-8"))["steps"]
            specs, variants = [s["edits"] for s in steps], [s["variant"] for s in steps]
        record = live_round(args.replay, args.out, args.chain, True, specs, variants, edits=args.edits, focus=args.focus)
        print(json.dumps(record.get("problems"), indent=1))
        return 1 if record.get("problems") else 0
    bad = run_live(args.rounds, args.seed, args.parallel, args.chain, args.keep_decks, args.out, not args.no_shrink,
                   edits=args.edits, focus=args.focus, reuse=args.reuse)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
