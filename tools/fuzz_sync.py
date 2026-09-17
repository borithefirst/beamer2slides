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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import fuzz_world as W  # noqa: E402
import loss_oracle  # noqa: E402
from beamer2slides import merge  # noqa: E402

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
    el = _pick(rng, [e for s in doc["slides"] for e in s["elements"] if e.get("role") != "title"])
    if not el:
        return None
    dx, dy = rng.choice([-20, -8, 8, 20]), rng.choice([-16, -6, 6, 16])
    el["bbox"] = [el["bbox"][0] + dx, el["bbox"][1] + dy, el["bbox"][2] + dx, el["bbox"][3] + dy]
    return f"move {el['id']}"


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
    # and two slides with one key would make the whole identity model meaningless
    labels = {s.get("label") for s in doc["slides"]}
    ids = {e["id"] for s in doc["slides"] for e in s["elements"]}
    page = len(doc["slides"])
    while f"new{page}" in labels or f"p{page}t0" in ids:
        page += 1
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


def src_retitle(rng, doc):
    s = rng.choice(doc["slides"])
    title = f"{rng.choice(W.WORDS).title()} retitled"
    s["title"] = title
    s["elements"][0]["paragraphs"] = [{**s["elements"][0]["paragraphs"][0], "runs": [W.run(title)]}]
    return f"retitle {title!r}"


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


def _renumber(doc):
    for i, s in enumerate(doc["slides"]):
        s["page"] = i


SOURCE_OPS = {f.__name__[4:]: f for f in (src_reword, src_add_paragraph, src_remove_paragraph, src_move_element,
                                          src_resize_element, src_restyle, src_add_element, src_delete_element,
                                          src_add_slide, src_delete_slide, src_move_slide, src_retitle, src_notes,
                                          src_background, src_repaint)}


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
    rb["text"] = (rb["text"] or "").replace(line, " ".join(words), 1)
    return f"reword {oid}"


def deck_append(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "text"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rb["text"] = (rb["text"] or "").rstrip("\n") + " typed by a person.\n"
    return f"append to {oid}"


def deck_delete_paragraph(rng, base, live):
    options = [x for x in _converter_texts(base, live)
               if x[2]["kind"] == "text" and len([l for l in (x[4]["text"] or "").split("\n") if l.strip()]) > 1]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    lines = [l for l in (rb["text"] or "").split("\n") if l.strip()]
    lines.pop(rng.randrange(len(lines)))
    rb["text"] = "\n".join(lines) + "\n"
    return f"delete a paragraph of {oid}"


def deck_edit_cell(rng, base, live):
    options = [x for x in _converter_texts(base, live) if x[2]["kind"] == "table"]
    if not options:
        return None
    b, s, el, oid, rb = rng.choice(options)
    rows = [r.split("\t") for r in rb["text"].split("\n")]
    rows[rng.randrange(len(rows))][rng.randrange(len(rows[0]))] = rng.choice(W.WORDS) + "-theirs"
    rb["text"] = "\n".join("\t".join(r) for r in rows)
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


def deck_add_text_box(rng, base, live):
    s = rng.choice(live["slides"])
    oid = f"user{rng.randrange(10 ** 9):09}"
    s["objects"][oid] = W.readback("shape", [300, 300, 460, 330], text=f"a note nobody may lose {oid}\n")
    s["order"].append(oid)
    return f"add a text box {oid}"


def deck_add_image(rng, base, live):
    s = rng.choice(live["slides"])
    oid = f"user{rng.randrange(10 ** 9):09}"
    s["objects"][oid] = W.readback("image", [400, 200, 500, 260],
                                   image={"contentHash": f"u{rng.randrange(10 ** 6)}", "sourceUrl": None,
                                          "signature": W.picture_signature(bytes([rng.randrange(200)]))})
    s["order"].append(oid)
    return f"add a picture {oid}"


def deck_add_slide(rng, base, live):
    i = rng.randrange(len(live["slides"]) + 1)
    sid = f"user_s{rng.randrange(10 ** 6):06}"
    oid = f"{sid}_t"
    live["slides"].insert(i, {"objectId": sid, "layoutObjectId": "L", "background": {"color": "#ffffff"},
                              "notes": "a slide the person added", "notes_id": f"{sid}_n", "order": [oid],
                              "objects": {oid: W.readback("shape", [40, 40, 300, 80], text="my own slide\n")}})
    return f"add a slide {sid}"


def deck_duplicate_slide(rng, base, live):
    s = rng.choice(live["slides"])
    sid = f"user_s{rng.randrange(10 ** 6):06}"
    copy_ = copy.deepcopy(s)
    copy_["objectId"] = sid
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
    gid = f"user_g{rng.randrange(10 ** 6):06}"
    boxes = [s["objects"][o]["box"] for o in picked]
    s["objects"][gid] = {**W.readback("elementGroup", [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                       max(b[2] for b in boxes), max(b[3] for b in boxes)]),
                         "children": picked}
    for o in picked:
        s["objects"][o]["parent_group"] = gid
    return f"group {picked} as {gid}"


def deck_ungroup(rng, base, live):
    options = [(s, o) for s in live["slides"] for o, rb in s["objects"].items() if rb["kind"] == "elementGroup"]
    if not options:
        return None
    s, gid = rng.choice(options)
    for rb in s["objects"].values():
        if rb.get("parent_group") == gid:
            rb["parent_group"] = None
    s["objects"].pop(gid)
    return f"ungroup {gid}"


DECK_OPS = {f.__name__[5:]: f for f in (deck_reword, deck_append, deck_delete_paragraph, deck_edit_cell, deck_move,
                                        deck_restyle, deck_delete_object, deck_replace_image, deck_add_text_box,
                                        deck_add_image, deck_add_slide, deck_duplicate_slide, deck_delete_slide,
                                        deck_move_slide, deck_notes, deck_background, deck_group, deck_ungroup)}


# ---------------------------------------------------------------- offline rounds

def _sync_step(seed: int, step: int, doc: dict, base: dict, live: dict, tmp: Path,
               deck_ops: list[str], source_ops: list[str], rebase: bool, reordered: bool = False) -> dict:
    """One edit + sync: the person edits the deck, the author changes the source, merge plans, the
    reference applier writes the plan and the oracle judges what the person is left with.
    `reordered`: an earlier step of the chain moved a frame in the source (see `_settled`)."""
    applied_deck = []
    for k, name in enumerate(deck_ops):
        if not live["slides"]:
            applied_deck.append(f"{name}: (no slides left)")  # a chain that deleted the whole deck
            continue
        done = DECK_OPS[name](random.Random(seed * 1009 + 101 * step + k), base, live)
        applied_deck.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    for s in live["slides"]:
        W._drop_lonely_groups(s)  # (Slides drops a group an edit left with one child)
    doc2 = copy.deepcopy(doc)
    applied_src = []
    for k, name in enumerate(source_ops):
        fn = SOURCE_OPS[name]
        r = random.Random(seed * 2003 + 101 * step + k)
        done = fn(r, doc2, tmp) if name == "repaint" else fn(r, doc2)
        applied_src.append(f"{name}: {done}" if done else f"{name}: (not applicable)")
    ours = W.build_ours(doc2, base, tmp)
    mplan = merge.plan_merge(base, ours, live)
    tok = f"{step}zz"  # a token per run, like sync's
    after = W.apply_plan(base, ours, live, mplan, tok)
    report = mplan["report"]
    findings = loss_oracle.check(base, live, after, report, ours)
    next_base = W.rebase(base, ours, after, mplan, tok) if rebase else None
    findings += _settled(doc2, next_base, after, tmp, reordered or "move_slide" in source_ops)
    return {"seed": seed, "step": step, "source_ops": list(source_ops), "deck_ops": list(deck_ops),
            "source": applied_src, "deck": applied_deck, "findings": findings,
            "failures": loss_oracle.failures(findings), "doc": doc2,
            "state": {"base": base, "before": live, "after": after, "report": report, "ours": ours,
                      "next_base": next_base, "doc": doc2, "work": tmp}}


def _settled(doc: dict, next_base: dict | None, after: dict, tmp: Path, reordered: bool = False) -> list[dict]:
    """Syncing the same source again must write nothing. The base a sync leaves behind is what the
    next one merges against, so a base that doesn't describe the deck it just wrote would have the
    next sync rewrite those units - and a rewrite is where a person's work gets lost.

    `reordered`: the source moved a frame this round. A crossing reorder of frames without labels is
    what docs/sync.md lists under "Not supported yet" (`identity.align_slides` keeps the order), and
    it leaves the source's frame paired with another slide, so the property can't hold. The finding
    is still recorded, as a note rather than a failure."""
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
    return [loss_oracle.finding("second_sync_writes", "note" if reordered else "report",
                                "the same source synced again would write: " + ("; ".join(busy) or "another slide order"),
                                slide=(busy[0].split(":")[0] if busy else "order"))]


def _draw(rng: random.Random, deck_ops, source_ops):
    if deck_ops is None:
        deck_ops = [rng.choice(sorted(DECK_OPS)) for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
    if source_ops is None:
        source_ops = [rng.choice(sorted(SOURCE_OPS)) for _ in range(rng.randint(1, 4))]
    return list(deck_ops), list(source_ops)


def offline_chain(seed: int, chain: int = 1, ops=None, work: Path | None = None) -> dict:
    """`chain` edit+sync steps on one deck. Each sync starts from the base the previous one wrote
    (`fuzz_world.rebase`), which is where a sync undoing what the last one merged would show.
    `ops`: per step `{"deck": [...], "source": [...]}` (default: drawn from the seed)."""
    rng = random.Random(seed)
    tmp = work or Path(tempfile.mkdtemp(prefix="b2s-fuzz-"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        doc = W.make_doc(rng, tmp)
        base = W.build_base(doc, tmp)
        live = W.live_of(base)
        steps = []
        reordered = False  # a frame moved in the source, in this step or an earlier one
        for step in range(chain):
            want = (ops[step] if ops and step < len(ops) else None) or {}
            deck_ops, source_ops = _draw(rng, want.get("deck"), want.get("source"))
            # Every step rebases: the next step needs that base, and the last step's base is what
            # the "a second sync writes nothing" check judges.
            record = _sync_step(seed, step, doc, base, live, tmp, deck_ops, source_ops, rebase=True,
                                reordered=reordered)
            reordered = reordered or "move_slide" in source_ops
            steps.append(record)
            doc, live = record.pop("doc"), record["state"]["after"]
            base = record["state"]["next_base"] or base
        return {"seed": seed, "chain": chain, "steps": steps,
                "ops": [{"deck": s["deck_ops"], "source": s["source_ops"]} for s in steps],
                "failures": [f for s in steps for f in s["failures"]]}
    finally:
        if work is None:
            shutil.rmtree(tmp, ignore_errors=True)


def offline_round(seed: int, source_ops=None, deck_ops=None, work: Path | None = None) -> dict:
    """One offline round. `source_ops` / `deck_ops`: op names (default: drawn from the seed)."""
    return offline_chain(seed, 1, [{"deck": deck_ops, "source": source_ops}], work)["steps"][0]


def shrink_offline(result: dict, limit: int = 200) -> dict:
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
                candidate = offline_round(best["seed"], **{"source_ops": best["source_ops"], "deck_ops": best["deck_ops"],
                                                           field: ops})
                if candidate["failures"]:
                    best, changed = candidate, True
                    break
    return best


def shrink_chain(result: dict, limit: int = 300) -> dict:
    """Drop edits one at a time, in the first failing step and the ones before it."""
    failing = next(i for i, s in enumerate(result["steps"]) if s["failures"])
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
                    candidate = offline_chain(best["seed"], best["chain"], ops)
                    if candidate["steps"][-1]["failures"]:
                        best, changed = candidate, True
                        break
    return best


def describe_chain(result: dict) -> str:
    lines = []
    for s in result["steps"]:
        lines.append(f"  step {s['step']}: deck:   " + "; ".join(s["deck"]))
        lines.append(f"          source: " + "; ".join(s["source"]))
        if s["failures"]:
            lines.append(loss_oracle.describe(s["failures"]))
    return "\n".join(lines)


def run_offline(rounds: int, seed0: int, shrink: bool, quiet: bool = False, chain: int = 1) -> list[dict]:
    bad = []
    for seed in range(seed0, seed0 + rounds):
        result = offline_chain(seed, chain)
        if result["failures"]:
            bad.append(shrink_chain(result) if shrink else result)
            if not quiet:
                print(f"seed {seed}: {len(result['failures'])} finding(s)")
                print(describe_chain(bad[-1]))
        elif not quiet and (seed - seed0) % 50 == 49:
            print(f"  ... {seed - seed0 + 1} rounds")
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


def random_spec(model, rng, donor=None):
    """One human-like edit spec for the live deck, found by content (tools/deck_edits.py kinds)."""
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
        return {"edit": "add_shape", "args": {"slide": sel, "shape_type": rng.choice(["STAR_5", "ELLIPSE", "CLOUD"]),
                                              "box": [rng.choice([600, 640]), rng.choice([60, 300]), 44, 44],
                                              "color": "#ffc000"}}
    if kind == "add_image":
        return {"edit": "add_image", "args": {"slide": sel, "url": donor,
                                              "box": [rng.choice([500, 540]), rng.choice([250, 280]), 140, 90]}}
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


class LiveRound:
    def __init__(self, seed: int, out: Path, chain: int, keep_decks: bool):
        self.seed, self.out, self.chain, self.keep = seed, out, chain, keep_decks
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(self.out / "fuzz.log", "w", encoding="utf-8")
        self.problems: list[str] = []
        self.record: dict = {"seed": seed, "steps": []}
        self.loose: set[str] = set()  # slides whose groups the person took apart, in any step so far

    def cli(self, *args, check=True):
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        done = subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)], env=ENV, cwd=ROOT,
                              stdout=self.log, stderr=subprocess.STDOUT)
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

    def run(self, specs: list[list[dict]] | None = None):
        from deck_edits import LiveDeck, apply as apply_edit
        build = sync_build()
        rng = random.Random(self.seed)
        variants = [v for v in build.VARIANTS if v != "v1"]
        self.cli("convert", build.build("v1"), "--out", self.out)
        self.deck = LiveDeck(json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"])
        self.record["deck"] = f"https://docs.google.com/presentation/d/{self.deck.pid}/edit"
        for step in range(self.chain):
            self.deck.read()
            donor = self._donor()
            done = []
            if specs is not None and step < len(specs):
                for spec in specs[step]:
                    try:
                        apply_edit(self.deck, spec)
                        done.append(spec)
                    except Exception as e:  # noqa: BLE001 (a replayed edit may no longer fit)
                        self.log.write(f"replay skipped {json.dumps(spec)}: {e}\n")
            else:
                wanted = rng.randint(MIN_EDITS, MAX_EDITS)
                for _ in range(wanted * 4):
                    if len(done) >= wanted:
                        break
                    spec = random_spec(self.deck.model, rng, donor)
                    if not spec:
                        continue
                    try:
                        apply_edit(self.deck, spec)
                        done.append(spec)
                    except Exception as e:  # noqa: BLE001 (an edit that doesn't fit this deck)
                        self.log.write(f"skipped {json.dumps(spec)}: {e}\n")
                        self.deck.read()
            variant = rng.choice(variants)
            self.step(step, done, variant, build)
        return self.problems

    def _donor(self):
        from deck_edits import donor_image_url
        try:
            return donor_image_url(self.deck.api, self.deck.pid)
        except Exception:  # noqa: BLE001
            return None

    def step(self, step: int, specs: list[dict], variant: str, build):
        from beamer2slides import snapshot
        folder = self.out / f"step{step}"
        folder.mkdir(exist_ok=True)
        base = json.loads((self.out / "sync" / "base.json").read_text(encoding="utf-8"))
        (folder / "base.json").write_text(json.dumps(base), encoding="utf-8")
        pres_before, before = self.snapshot()
        (folder / "before.json").write_text(json.dumps(before), encoding="utf-8")
        (folder / "edits.json").write_text(json.dumps(specs, indent=1, ensure_ascii=False), encoding="utf-8")
        pdf = build.build(variant)
        self.cli("sync", pdf, "--deck", self.out)
        report = json.loads((self.out / "sync" / "sync-report.json").read_text(encoding="utf-8"))
        (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
        pres_after, after = self.snapshot()
        (folder / "after.json").write_text(json.dumps(after), encoding="utf-8")
        ours = self.ours(pdf, base, folder)
        findings = loss_oracle.check(base, before, after, report, ours)
        (folder / "findings.json").write_text(json.dumps(findings, indent=1, ensure_ascii=False), encoding="utf-8")
        self.record["steps"].append({"step": step, "variant": variant, "edits": specs,
                                     "findings": loss_oracle.failures(findings)})
        self.problems += [f"step {step} ({variant}): {loss_oracle.describe([f])}" for f in loss_oracle.failures(findings)]
        self.problems += [f"step {step} ({variant}): integrity: {p}" for p in self.integrity(pres_before, pres_after, specs)]

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
        import sync_check as sc
        base = json.loads((self.out / "sync" / "base.json").read_text(encoding="utf-8"))
        # A group the person took apart (or a member they deleted) is theirs: the sync rebuilding
        # the unit ungrouped is the policy, not a broken deck. It stays theirs for the rest of the
        # chain - a later step must not be accused of the group step 0 dissolved - so the slides
        # add up over the steps.
        self.loose |= {_slide_title(spec) for spec in specs
                       if spec["edit"] in ("ungroup", "group", "delete_element", "delete_group", "duplicate")}
        self.loose.discard(None)
        return sc.integrity(sc.Model(pres_after), before=sc.Model(pres_before), base_ids=sc.ids_in(base),
                            allow_ungrouped=self.loose, allow_groups_changed=self.loose)

    def drop_deck(self):
        from beamer2slides import snapshot
        from beamer2slides.google_auth import drive_service
        from beamer2slides.gslides import execute
        drive = drive_service()
        try:
            info = execute(drive.files().get(fileId=self.deck.pid, fields="appProperties"))
            fid = (info.get("appProperties") or {}).get(snapshot.BASE_PROPERTY)
            if fid:
                execute(drive.files().delete(fileId=fid))
            execute(drive.files().delete(fileId=self.deck.pid))
        except Exception as e:  # noqa: BLE001
            self.log.write(f"could not delete the deck: {e}\n")


def live_round(seed: int, out_root: Path, chain: int, keep_decks: bool, specs=None) -> dict:
    r = LiveRound(seed, out_root / f"r{seed:03}", chain, keep_decks)
    try:
        problems = r.run(specs)
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
        (r.out / "round.json").write_text(json.dumps(r.record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        return r.record
    finally:
        r.log.close()


def shrink_live(record: dict, out_root: Path, chain: int) -> dict:
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
        got = live_round(record["seed"], out_root / "shrink", failing + 1, True, trial)
        if any(s["findings"] for s in got["steps"]):
            steps, best = trial, got
            print(f"  shrink: dropping {dropped['edit']} keeps it failing ({len(steps[failing])} edits left)")
        else:
            i += 1
    best["shrunk_edits"] = steps
    return best


def run_live(rounds: int, seed0: int, parallel: int, chain: int, keep_decks: bool, out_root: Path, shrink: bool):
    out_root.mkdir(parents=True, exist_ok=True)
    seeds = list(range(seed0, seed0 + rounds))
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        records = list(pool.map(lambda s: live_round(s, out_root, chain, keep_decks), seeds))
    bad = [r for r in records if r.get("problems")]
    for r in bad:
        print(f"\nseed {r['seed']}: {len(r['problems'])} problem(s), {r.get('deck')}")
        for p in r["problems"]:
            print(f"  {p}")
        if shrink and r.get("steps"):
            shrink_live(r, out_root, chain)
    print(f"\n{len(records) - len(bad)}/{len(records)} live rounds clean")
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
    ap.add_argument("--keep-decks", action="store_true", help="live: don't delete the decks of passing rounds")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("B2S_FUZZ_OUT", ROOT / "out" / "sync-fuzz")))
    args = ap.parse_args()

    if args.mode == "offline":
        if args.replay is not None:
            result = offline_chain(args.replay, args.chain)
            print(describe_chain(result))
            print(loss_oracle.describe([f for s in result["steps"] for f in s["findings"]]) or "nothing lost")
            return 1 if result["failures"] else 0
        started = time.monotonic()
        bad = run_offline(args.rounds, args.seed, not args.no_shrink, chain=args.chain)
        print(f"{args.rounds - len(bad)}/{args.rounds} offline rounds clean in {time.monotonic() - started:.1f} s")
        return 1 if bad else 0

    if args.replay is not None:
        folder = args.out / f"r{args.replay:03}"
        specs = None
        if (folder / "round.json").exists():
            specs = [s["edits"] for s in json.loads((folder / "round.json").read_text(encoding="utf-8"))["steps"]]
        record = live_round(args.replay, args.out, args.chain, True, specs)
        print(json.dumps(record.get("problems"), indent=1))
        return 1 if record.get("problems") else 0
    bad = run_live(args.rounds, args.seed, args.parallel, args.chain, args.keep_decks, args.out, not args.no_shrink)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
