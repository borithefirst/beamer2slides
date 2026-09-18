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


def shape_ir(eid, bbox, fill="#dddddd"):
    return {"id": eid, "kind": "shape", "role": "panel", "bbox": [float(v) for v in bbox], "shape": "RECTANGLE",
            "fill": fill, "flip": False, "radius": 0.0}


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


def slide_info(s):
    return {"label": s.get("label"), "title": identity.plain_text(s["elements"][0]) if s["elements"] else "",
            "text": " ".join(identity.plain_text(e) for e in s["elements"]), "page": s["page"]}


# ---------------------------------------------------------------- base, ours, theirs

def picture_signature(data: bytes) -> str:
    return "100x50:" + bytes([(data[0] * 97 + i * 13) % 256 for i in range(1024)]).hex()


def readback(kind, box, text=None, image=None, parent=None, title=None, z=0, table=None):
    out = {"kind": kind, "transform": [1.0, 0.0, 0.0, 1.0, round(box[0], 2), round(box[1], 2)],
           "size": [round(box[2] - box[0], 2), round(box[3] - box[1], 2)], "box": [round(v, 2) for v in box],
           "parent_group": parent, "z": z, "title": title, "description": None, "text": text,
           "text_styles": [{"fontFamily": "Lato", "fontSize": 18.0}] if text is not None else [],
           "paragraph_styles": [{"alignment": "START"}] if text is not None else [],
           "text_style_hash": "s0", "shape_style": {"fill": {"color": "#dddddd", "alpha": 1.0}},
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
        return styled(readback("shape", box, text="", parent=parent, z=z), el)
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
        slides.append({"key": key, "label": s.get("label"), "title": identity.plain_text(s["elements"][0]) if s["elements"] else "",
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
        order, z = [], 0
        for k, (el, e) in enumerate(zip(s["elements"], entry["elements"])):
            oid = f"{sid}_e{k}"
            group = f"{sid}_e{list(by_id).index(el['anchor'])}_g" if el.get("anchor") in by_id else \
                (f"{oid}_g" if el["id"] in anchored else None)
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
            z += 1
        entry.update(objectId=sid, layoutObjectId="L", background_readback={"color": s["bg"]},
                     notes_readback=s.get("notes") or "", groups=[], order=order)
    return {"version": 1, "generation": 1, "presentationId": "P", "revisionId": "r0",
            "source": {"pdf": "talk.pdf", "sha1": "x"}, "scale": SCALE, "page_size": [360.0, 202.5],
            "deck_page_size": [720.0, 405.0], "master_background": "color:#ffffff",
            "master_readback": {"color": "#ffffff"}, "slides": slides}


def build_ours(doc, base, out: Path) -> dict:
    infos = [slide_info(s) for s in doc["slides"]]
    base_infos = [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "", "page": b["page"]}
                  for b in base["slides"]]
    moves = identity.label_moves(base_infos, infos)
    for m in moves:
        m["slide"] = base["slides"][m["base"]]["key"]
        m["frame_is"] = base["slides"][m["frame_is"]]["key"] if m["frame_is"] is not None else None
        m["slide_is"] = infos[m["slide_is"]]["title"] if m["slide_is"] is not None else None
    keys, pairs = identity.inherit_slide_keys(base_infos, [b["key"] for b in base["slides"]], infos, moves)
    ekeys, fps = [], []
    for j, s in enumerate(doc["slides"]):
        matched = [{"key": e["key"], "kind": e["kind"], "role": e.get("role"), "fingerprint": e["fingerprint"]}
                   for e in base["slides"][pairs[j]]["elements"]] if j in pairs else None
        k, f = identity.slide_element_keys(s["elements"], out, matched)
        ekeys.append(k)
        fps.append(f)
    return {"slides": entries(doc, out, keys, ekeys, fps), "pairs": pairs, "out": out, "label_moves": moves}


def live_of(base) -> dict:
    slides = []
    for s in base["slides"]:
        objects = {}
        for el in s["elements"]:
            for oid, rb in el["readback"].items():
                objects[oid] = copy.deepcopy(rb)
        slides.append({"objectId": s["objectId"], "layoutObjectId": "L",
                       "background": copy.deepcopy(s["background_readback"]), "notes": s["notes_readback"],
                       "notes_id": f"{s['objectId']}_notes", "order": list(s["order"]), "objects": objects})
    return {"presentationId": "P", "revisionId": "r0", "page_size": [720.0, 405.0], "layouts": {"L": "TITLE_ONLY"},
            "master_background": {"color": "#ffffff"}, "slides": slides}


# ---------------------------------------------------------------- the reference sync

def h6(text):
    return identity.sha1(text)[:6]


def new_object(skey, o_el, base_el, live, overrides, tok):
    """The object a correct sync creates for one ours element (docs/sync.md: ours content, with the
    deck's overrides re-applied)."""
    ir = o_el["ir"]
    box = [v * SCALE for v in ir["bbox"]]
    text = element_text(ir)
    ov = (overrides or {}).get("text")
    if ov and text is not None:
        if ov.get("table"):
            cells = merge.table_merge(ov["base"], text, ov["theirs"], ov["dims"], ov["dims"])
            text = "\n".join("\t".join(r) for r in cells[0]) if cells else ov["theirs"]
        else:
            text = merge.diff3(merge.collapse_holes(ov["base"]), merge.collapse_holes(text),
                               merge.collapse_holes(ov["theirs"]))[0]
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
        rb = readback("shape", box, text=text, parent=parent)
    rb["title"] = snapshot.tag(skey, o_el["key"])
    styled(rb, ir)
    live_rb = live["objects"].get(main) if main else None
    if live_rb and (overrides or {}).get("text_style"):  # the deck's styling, re-applied
        rb["text_style_hash"] = live_rb.get("text_style_hash")
        rb["text_styles"] = copy.deepcopy(live_rb.get("text_styles"))
    if live_rb and (overrides or {}).get("shape_style"):
        rb["shape_style_hash"] = live_rb.get("shape_style_hash")
        rb["shape_style"] = copy.deepcopy(live_rb.get("shape_style"))
    return oid, rb


def apply_plan(base, ours, theirs, mplan, tok="2zz") -> dict:
    """The live deck as a correct sync would leave it after writing `mplan`."""
    after = copy.deepcopy(theirs)
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
                oid, rb = new_object(o["key"], el, None, {"objects": {}}, None, tok)
                objects[oid] = rb
            made[f"new:{p['key']}"] = {"objectId": sid, "layoutObjectId": "L",
                                       "background": {"color": o["background"].split(":", 1)[1]},
                                       "notes": o.get("notes") or "", "notes_id": f"{sid}_notes",
                                       "order": list(objects), "objects": objects}
        elif p["action"] == "update":
            _update_slide(base, ours, by_id[p["objectId"]], p, tok)
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


def _update_slide(base, ours, live, p, tok):
    b, o = base["slides"][p["base"]], ours["slides"][p["ours"]]
    bu, ou = merge.units(b["elements"]), merge.units(o["elements"])
    # The deck's own version of the objects, read before anything is deleted: that is what the
    # overrides (the deck's box, its styling) are re-applied from.
    theirs = {"objects": dict(live["objects"])}
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
            oid, rb = new_object(o["key"], m, base_by.get(m["key"]), theirs, overrides if m["key"] == u["key"] else None, tok)
            parent = parents.get(m["key"])
            rb["parent_group"] = parent if parent in live["objects"] else None
            live["objects"][oid] = rb
            made.append((m, rb))
        _place_unit(u, members, theirs, made, base_by)
    if p.get("background"):
        live["background"] = {"color": p["background"].split(":", 1)[1]}
    if p.get("notes") is not None:
        live["notes"] = p["notes"]
    _drop_lonely_groups(live)


def rebase(base, ours, after, mplan, tok="2zz") -> dict:
    """The base a correct sync records for the next one: ours IR plus the read-back of the objects
    as it left them (docs/sync.md, "After writing"). Units kept from the deck carry their old base,
    so a chain of syncs never forgets what the person's version was."""
    now = {s["objectId"]: s for s in after["slides"]}
    entries: dict[str, dict] = {}
    sids = {}
    for p in mplan["slides"]:
        if p["action"] == "delete":
            continue
        if p["action"] in ("keep_removed", "gone"):
            sid = p.get("objectId") or f"gone:{p['key']}"
            entries[sid] = base["slides"][p["base"]]
            if p["action"] == "gone":
                sids[id(p)] = sid  # it keeps its place in the source's order (see the ordering below)
                o = ours["slides"][p["ours"]]  # ... and says what the source says (sync.new_base)
                entries[sid] = {**entries[sid], **{k: o.get(k) for k in ("label", "title", "text", "page")}}
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
            for u in p["units"]:
                action = u["action"]
                if action in ("create", "recreate"):
                    for mk in u["ours_members"]:
                        elements.append(_rebased_element(index[mk], [f"b2s_{h6(o['key'])}_{h6(mk)}_{tok}"], read))
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
                    elements += bunits.get(u["key"], [])
                # delete / none: gone
            entry.update(objectId=sid, layoutObjectId=b.get("layoutObjectId"), groups=list(b.get("groups", [])),
                         order=list(read.get("order") or b.get("order", [])))
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
        entry["elements"] = elements
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
    """Re-apply the deck's move or resize to a rewritten unit. Sync transforms the unit's *top*
    object, so the anchored pictures go along with their text; the step comes from the old top's
    base and live read-backs (`delta`), or puts the new unit where the deck's object stands."""
    ov = (u.get("overrides") or {}).get("geometry")
    if not ov or not made:
        return
    anchor = base_by.get(u["key"]) or (members[0] if members else None)
    old_top = (merge.unit_top(members, theirs) if members else None) or (anchor or {}).get("main")
    base_rb = next((m["readback"][old_top] for m in members if old_top in m.get("readback", {})), None)
    theirs_rb = theirs["objects"].get(old_top)
    if not base_rb or not theirs_rb:
        return
    if ov["mode"] == "delta":
        dx, dy = theirs_rb["box"][0] - base_rb["box"][0], theirs_rb["box"][1] - base_rb["box"][1]
    else:  # "theirs": both sides moved it, the deck's place wins
        ref = next((rb for m, rb in made if m["key"] == u["key"]), made[0][1])
        dx, dy = theirs_rb["box"][0] - ref["box"][0], theirs_rb["box"][1] - ref["box"][1]
    for _, rb in made:
        rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
        rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]


def _drop_lonely_groups(live):
    changed = True
    while changed:
        changed = False
        for oid, rb in list(live["objects"].items()):
            if rb["kind"] != "elementGroup":
                continue
            kids = [c for c, x in live["objects"].items() if x.get("parent_group") == oid]
            if len(kids) <= 1:
                for c in kids:
                    live["objects"][c]["parent_group"] = rb.get("parent_group")
                live["objects"].pop(oid)
                changed = True
