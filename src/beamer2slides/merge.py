"""Sync's three-way merge, pure (docs/sync.md): deck edits from read-backs, word-level diff3,
the merge rules per element unit and field, and slide add/delete/reorder planning.

Inputs are the base (snapshot.build_base), ours (the new conversion's slide entries with keys
inherited: snapshot.slide_entries) and theirs (snapshot.read_presentation of the live deck)."""

import re
from difflib import SequenceMatcher

from . import identity

GEOMETRY_TOLERANCE = 0.05  # pt
SCALE_TOLERANCE = 1e-3
EDIT_FIELDS = ("geometry", "text", "text_style", "shape_style", "image")
TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


# ---------------------------------------------------------------- text

def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def _hunks(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    sm = SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def _clash(a: tuple, b: tuple) -> bool:
    (a0, a1, _), (b0, b1, _) = a, b
    if a0 < b1 and b0 < a1:
        return True
    if a0 == a1 == b0 == b1:
        return True
    return (a0 == a1 and b0 < a0 < b1) or (b0 == b1 and a0 < b0 < a1)


def diff3(base: str, ours: str, theirs: str) -> tuple[str, list[dict]]:
    """Word-level three-way merge. Returns the merged text and the conflicts ({"base", "ours",
    "theirs"} pieces); conflicting regions keep theirs (the deck wins)."""
    b = tokens(base)
    hunks = [("ours", h) for h in _hunks(b, tokens(ours))] + [("theirs", h) for h in _hunks(b, tokens(theirs))]
    hunks.sort(key=lambda s: (s[1][0], s[1][1], s[0]))
    clusters: list[list] = []
    for side, h in hunks:
        for c in clusters:
            if any(_clash(h, other) for _, other in c):
                c.append((side, h))
                break
        else:
            clusters.append([(side, h)])
    # (clusters can touch after growing: join them until stable)
    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if any(_clash(x, y) for _, x in clusters[i] for _, y in clusters[j]):
                    clusters[i] += clusters.pop(j)
                    merged_any = True
                    break
            if merged_any:
                break
    spans = []
    conflicts = []
    for c in clusters:
        c0 = min(h[0] for _, h in c)
        c1 = max(h[1] for _, h in c)

        def version(side):
            out, pos = [], c0
            for s, (h0, h1, rep) in sorted(c, key=lambda x: (x[1][0], x[1][1])):
                if s != side:
                    continue
                out += b[pos:h0] + rep
                pos = h1
            return out + b[pos:c1]
        sides = {s for s, _ in c}
        if len(sides) == 1:
            text = version(sides.pop())
        else:
            o, t = version("ours"), version("theirs")
            text = t
            if o != t:
                conflicts.append({"base": "".join(b[c0:c1]), "ours": "".join(o), "theirs": "".join(t)})
        spans.append((c0, c1, text))
    out, pos = [], 0
    for c0, c1, text in sorted(spans, key=lambda s: (s[0], s[1])):
        out += b[pos:c0] + text
        pos = max(pos, c1)
    out += b[pos:]
    return "".join(out), conflicts


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def text_edit_requests(object_id: str, current: str, target: str) -> list[dict]:
    """deleteText / insertText turning an object's text `current` into `target` (UTF-16 indices,
    applied back to front so earlier indices stay valid)."""
    a, b = tokens(current), tokens(target)
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    offsets = [0]
    for t in a:
        offsets.append(offsets[-1] + utf16_len(t))
    reqs = []
    for op, i1, i2, j1, j2 in reversed(ops):
        if op == "equal":
            continue
        start, end = offsets[i1], offsets[i2]
        if end > start:
            reqs.append({"deleteText": {"objectId": object_id, "textRange": {
                "type": "FIXED_RANGE", "startIndex": start, "endIndex": end}}})
        insert = "".join(b[j1:j2])
        if insert:
            reqs.append({"insertText": {"objectId": object_id, "insertionIndex": start, "text": insert}})
    return reqs


# ---------------------------------------------------------------- deck edits

def object_changes(b: dict, t: dict) -> set[str]:
    """Fields of one object the deck changed (read-backs from snapshot.readback)."""
    out = set()
    if any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(b["box"], t["box"])) or \
            any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(b["transform"][:4], t["transform"][:4])):
        out.add("geometry")
    if (b.get("text") or "") != (t.get("text") or ""):
        out.add("text")
    if b.get("text_style_hash") != t.get("text_style_hash"):
        out.add("text_style")
    if b.get("shape_style_hash") != t.get("shape_style_hash"):
        out.add("shape_style")
    if (b.get("image") or {}).get("contentHash") != (t.get("image") or {}).get("contentHash"):
        out.add("image")
    if b.get("parent_group") != t.get("parent_group"):
        out.add("group")
    return out


def deck_edits(base_el: dict, slide_read: dict | None) -> dict[str, list[str]]:
    """Field -> object ids the deck changed, for one base element: geometry, text, text_style,
    shape_style, image, group, and deleted (its main object is gone) / part_deleted."""
    edits: dict[str, list[str]] = {}
    objects = (slide_read or {}).get("objects", {})
    for oid, b in base_el.get("readback", {}).items():
        t = objects.get(oid)
        if t is None:
            edits.setdefault("deleted" if oid == base_el.get("main") else "part_deleted", []).append(oid)
            continue
        for field in object_changes(b, t):
            edits.setdefault(field, []).append(oid)
    if base_el.get("main") and base_el["main"] not in base_el.get("readback", {}):
        edits.setdefault("deleted", []).append(base_el["main"])
    return edits


def user_objects(base_slide: dict, slide_read: dict) -> list[dict]:
    """Objects on a live slide the converter didn't make: {"objectId", "copy_of"} (copy_of: the
    tag of a converter object it was copied from in Slides)."""
    ours = {o for el in base_slide["elements"] for o in el.get("objects", [])} | set(base_slide.get("groups", []))
    out = []
    for oid, rb in slide_read["objects"].items():
        if oid in ours:
            continue
        title = rb.get("title") or ""
        out.append({"objectId": oid, "copy_of": title[4:] if title.startswith("b2s:") else None})
    return out


def uniform_changes(base_styles: list[dict], theirs_styles: list[dict]) -> dict | None:
    """Style attributes the deck set on all of an object's text ({} when nothing changed, None
    when the change isn't uniform and so can't be re-applied to new text)."""
    def canon(styles):
        return {repr(sorted(s.items())) for s in styles}
    if canon(base_styles) == canon(theirs_styles):
        return {}
    if not theirs_styles:
        return None
    out = {}
    for key in {k for s in theirs_styles for k in s}:
        values = {repr(s.get(key)) for s in theirs_styles}
        if len(values) == 1 and key in theirs_styles[0] and {repr(s.get(key)) for s in base_styles} != values:
            out[key] = theirs_styles[0][key]
    # Everything else must be as before: re-applying only these attributes gives theirs.
    if not out or canon([{**s, **out} for s in base_styles]) != canon(theirs_styles):
        return None
    return out


# ---------------------------------------------------------------- units

def units(elements: list[dict]) -> dict[str, list[dict]]:
    """Unit key (the anchor text's key) -> the anchor and the elements anchored to it; an
    element without anchor is a unit of its own."""
    keys = {e["key"] for e in elements}
    out: dict[str, list[dict]] = {}
    for e in elements:
        if not (e.get("anchor") and e["anchor"] in keys):
            out.setdefault(e["key"], []).insert(0, e)
    for e in elements:
        if e.get("anchor") and e["anchor"] in keys:
            out.setdefault(e["anchor"], []).append(e)
    return out


def unit_roots(members: list[dict], slide_read: dict) -> list[str]:
    """Live object ids of a base unit to delete: those not inside another of its objects."""
    objects = slide_read["objects"]
    mine = {o for m in members for o in m.get("objects", []) if o in objects}
    return [o for m in members for o in m.get("objects", []) if o in objects and objects[o].get("parent_group") not in mine]


def unit_top(members: list[dict], slide_read: dict) -> str | None:
    """The unit's outermost live object (its group with anchored pictures, or its main object)."""
    roots = unit_roots(members, slide_read)
    main = members[0].get("main")
    for r in roots:
        if r == main or main in _descendants(r, slide_read):
            return r
    return roots[0] if roots else None


def _descendants(oid: str, slide_read: dict) -> set[str]:
    out, todo = set(), [oid]
    while todo:
        for c in slide_read["objects"].get(todo.pop(), {}).get("children", []):
            out.add(c)
            todo.append(c)
    return out


def predicted_text(el: dict) -> str:
    """The text Slides will hold for a text element (a hole as one no-break space: see collapse_holes)."""
    return "\n".join("".join("\u00a0" if r.get("hole") else r["text"] for r in p["runs"]) for p in el["paragraphs"]) + "\n"


def collapse_holes(text: str) -> str:
    """Hole runs (no-break spaces sized to a formula) as one no-break space: their count follows the formula width."""
    return re.sub("\u00a0+", "\u00a0", text)


def plan_unit(skey: str, ukey: str, base_members: list[dict] | None, ours_members: list[dict] | None,
              slide_read: dict | None, report: dict) -> dict:
    """The action for one element unit: keep, recreate (with deck overrides), create, delete or
    move; conflicts and overrides go to `report`."""
    where = {"slide": skey, "element": ukey}
    if base_members is None:
        report["applied"].append({**where, "fields": ["added"]})
        return {"key": ukey, "action": "create"}
    edits: dict[str, list[str]] = {}
    for m in base_members:
        for f, oids in deck_edits(m, slide_read).items():
            edits.setdefault(f, []).extend(oids)
    deck = set(edits)
    if ours_members is None:
        if not deck:
            report["applied"].append({**where, "fields": ["removed"]})
            return {"key": ukey, "action": "delete"}
        if deck <= {"deleted", "part_deleted"}:
            return {"key": ukey, "action": "none", "gone": True}
        report["conflicts"].append({**where, "field": "removed", "base": "element", "ours": None,
                                    "theirs": sorted(deck), "resolution": "kept (edited in the deck)"})
        return {"key": ukey, "action": "keep", "deck": sorted(deck)}
    base_by = {m["key"]: m for m in base_members}
    ours_by = {m["key"]: m for m in ours_members}
    src: set[str] = set()
    for k in base_by.keys() | ours_by.keys():
        if k not in base_by or k not in ours_by:
            src.add("layout")
        else:
            src |= identity.source_changes(base_by[k], ours_by[k])
    action = {"key": ukey, "source": sorted(src), "deck": sorted(deck)}
    edited = deck & set(EDIT_FIELDS)
    if not src:
        if deck - {"z"}:
            report["overrides"].append({**where, "fields": sorted(deck)})
        return {**action, "action": "keep"}
    if "deleted" in deck:
        report["conflicts"].append({**where, "field": "deleted", "base": "element", "ours": sorted(src), "theirs": None,
                                    "resolution": "kept deleted"})
        return {**action, "action": "keep"}
    if "part_deleted" in deck:
        report["conflicts"].append({**where, "field": "part_deleted", "base": "element", "ours": sorted(src),
                                    "theirs": edits["part_deleted"], "resolution": "deck kept"})
        return {**action, "action": "keep"}
    if not edited:
        report["applied"].append({**where, "fields": sorted(src)})
        if "group" in deck:
            report["overrides"].append({**where, "fields": ["group"]})
        return {**action, "action": "recreate", "overrides": {}}

    anchor, first = base_members[0], ours_members[0]
    main = anchor.get("main")
    base_rb = anchor.get("readback", {}).get(main, {})
    theirs_rb = (slide_read or {}).get("objects", {}).get(main, {})
    if "position" in src and src <= {"position", "size"} and "geometry" not in edited:
        # Only the place changed in the source: move the edited deck object there.
        bb, ob = anchor["fingerprint"]["bbox"], first["fingerprint"]["bbox"]
        report["applied"].append({**where, "fields": ["position"], "how": "deck object moved"})
        report["overrides"].append({**where, "fields": sorted(edited)})
        return {**action, "action": "move", "delta": [ob[0] - bb[0], ob[1] - bb[1]]}
    overrides: dict = {}
    conflicts = []

    def conflict(field, base_v, ours_v, theirs_v, resolution="deck kept"):
        conflicts.append({**where, "field": field, "base": base_v, "ours": ours_v, "theirs": theirs_v, "resolution": resolution})

    keep = False
    if "image" in edited:
        conflict("image", "picture", sorted(src), "replaced in the deck")
        keep = True
    if "text" in edited and not keep:
        if anchor["kind"] != "text" or set(edits["text"]) != {main}:
            conflict("text", anchor["fingerprint"]["text"], first["fingerprint"]["text"], theirs_rb.get("text"))
            keep = True
        else:
            b, o, t = (collapse_holes(x) for x in (base_rb.get("text") or "", predicted_text(first["ir"]), theirs_rb.get("text") or ""))
            merged, clashes = diff3(b, o, t)
            if clashes:
                conflict("text", b, o, t)
                keep = True
            else:
                overrides["text"] = {"base": base_rb.get("text") or "", "theirs": theirs_rb.get("text") or ""}
                if merged == o:
                    report["converged"].append({**where, "field": "text"})
    if "text_style" in edited and not keep:
        runs = uniform_changes(base_rb.get("text_styles", []), theirs_rb.get("text_styles", []))
        paras = uniform_changes(base_rb.get("paragraph_styles", []), theirs_rb.get("paragraph_styles", []))
        if runs is None or paras is None or set(edits["text_style"]) != {main}:
            conflict("text_style", "style", sorted(src), "restyled in the deck")
            keep = True
        else:
            overrides["text_style"] = {"runs": runs, "paragraphs": paras}
            if "style" in src:
                conflict("text_style", "style", "restyled in the source", "restyled in the deck", "deck style re-applied")
    if "shape_style" in edited and not keep:
        if anchor["kind"] != "shape" or set(edits["shape_style"]) != {main}:
            conflict("shape_style", "style", sorted(src), "restyled in the deck")
            keep = True
        else:
            overrides["shape_style"] = theirs_rb.get("shape_style")
            if "style" in src:
                conflict("shape_style", "style", "restyled in the source", "restyled in the deck", "deck style re-applied")
    if "geometry" in edited and not keep:
        both = "position" in src
        overrides["geometry"] = {"mode": "theirs" if both else "delta"}
        if both:
            conflict("geometry", anchor["fingerprint"]["bbox"], first["fingerprint"]["bbox"], theirs_rb.get("box"),
                     "deck position kept")
    report["conflicts"] += conflicts
    if keep:
        report["overrides"].append({**where, "fields": sorted(edited)})
        return {**action, "action": "keep"}
    report["applied"].append({**where, "fields": sorted(src)})
    report["overrides"].append({**where, "fields": sorted(edited)})
    return {**action, "action": "recreate", "overrides": overrides}


# ---------------------------------------------------------------- slides

def empty_report() -> dict:
    return {"applied": [], "overrides": [], "conflicts": [], "converged": [], "user_objects": [],
            "slides": {"created": [], "deleted": [], "moved": [], "kept": [], "user_added": []}, "warnings": []}


def plan_merge(base: dict, ours: dict, theirs: dict) -> dict:
    """ours: {"slides": [slide entries with inherited keys], "pairs": {ours index: base index}}.
    Returns {"slides": [per slide plan], "order": [live slide ids or "new:<key>"], "report"}."""
    report = empty_report()
    live = {s["objectId"]: s for s in theirs["slides"]}
    base_slides = base["slides"]
    pairs = {int(k): v for k, v in ours["pairs"].items()}
    matched_base = set(pairs.values())
    plans = []

    for j, o in enumerate(ours["slides"]):
        i = pairs.get(j)
        b = base_slides[i] if i is not None else None
        read = live.get(b["objectId"]) if b and b.get("objectId") else None
        if b is None:
            report["slides"]["created"].append(o["key"])
            plans.append({"key": o["key"], "action": "create", "ours": j, "base": None, "objectId": None})
            continue
        if read is None:
            changed = any(identity.source_changes(e, oe) for e in b["elements"] for oe in o["elements"] if e["key"] == oe["key"]) \
                or {e["key"] for e in b["elements"]} != {e["key"] for e in o["elements"]}
            if changed:
                report["conflicts"].append({"slide": o["key"], "element": None, "field": "slide", "base": "slide",
                                            "ours": "changed", "theirs": "deleted", "resolution": "kept deleted"})
            plans.append({"key": o["key"], "action": "gone", "ours": j, "base": i, "objectId": None})
            continue
        plans.append(plan_slide(b, o, read, report, base, j, i))

    for i, b in enumerate(base_slides):
        if i in matched_base:
            continue
        read = live.get(b.get("objectId"))
        if read is None:
            continue  # removed on both sides
        touched = slide_touched(b, read)
        if touched:
            report["slides"]["kept"].append({"slide": b["key"], "reason": touched})
            plans.append({"key": b["key"], "action": "keep_removed", "ours": None, "base": i, "objectId": b["objectId"]})
        else:
            report["slides"]["deleted"].append(b["key"])
            plans.append({"key": b["key"], "action": "delete", "ours": None, "base": i, "objectId": b["objectId"]})

    base_ids = {b.get("objectId") for b in base_slides}
    for s in theirs["slides"]:
        if s["objectId"] in base_ids:
            continue
        # (a slide duplicated in Slides carries the tags of the original's objects)
        copies = {rb["title"][4:].rsplit("/", 3)[0] for rb in s["objects"].values() if (rb.get("title") or "").startswith("b2s:")}
        report["slides"]["user_added"].append({"objectId": s["objectId"], "copy_of": sorted(copies) or None})
    order, moved = plan_order(base, ours, theirs, plans)
    report["slides"]["moved"] = moved
    return {"slides": plans, "order": order, "report": report}


def slide_touched(b: dict, read: dict) -> list[str]:
    """Why a slide counts as edited in the deck (empty: untouched)."""
    why = []
    if any(deck_edits(e, read) for e in b["elements"]):
        why.append("elements edited")
    if user_objects(b, read):
        why.append("objects added")
    if b.get("notes_readback", "") != read.get("notes", ""):
        why.append("notes edited")
    if b.get("background_readback") is not None and b["background_readback"] != read.get("background"):
        why.append("background changed")
    return why


def plan_slide(b: dict, o: dict, read: dict, report: dict, base: dict, j: int, i: int) -> dict:
    skey = o["key"]
    bu, ou = units(b["elements"]), units(o["elements"])
    unit_plans = []
    for ukey in list(ou) + [k for k in bu if k not in ou]:
        unit_plans.append({**plan_unit(skey, ukey, bu.get(ukey), ou.get(ukey), read, report),
                           "base_members": [m["key"] for m in bu.get(ukey, [])],
                           "ours_members": [m["key"] for m in ou.get(ukey, [])]})
    # A unit the source removed may live on inside another one (paragraphs joined): if that one
    # stays as the deck has it (a conflict), deleting the removed one would lose its words.
    kept_texts = [" ".join(" ".join(m["fingerprint"]["text"].split()) for m in ou[u["key"]])
                  for u in unit_plans if u["action"] == "keep" and u.get("source") and u["key"] in ou]
    for u in unit_plans:
        words = " ".join(" ".join(m["fingerprint"]["text"].split()) for m in bu.get(u["key"], []))
        if u["action"] == "delete" and words and any(words in t for t in kept_texts):
            u["action"] = "keep"
            report["applied"] = [a for a in report["applied"] if not (a["slide"] == skey and a["element"] == u["key"])]
            report["conflicts"].append({"slide": skey, "element": u["key"], "field": "removed", "base": words, "ours": None,
                                        "theirs": words, "resolution": "kept: its text moved into an element in conflict"})
    for u in user_objects(b, read):
        report["user_objects"].append({"slide": skey, **u})
    plan = {"key": skey, "action": "update", "ours": j, "base": i, "objectId": b["objectId"], "units": unit_plans}
    # Background: the source's picture or colour unless the deck changed it.
    if b.get("background") != o.get("background"):
        if b.get("background_readback") is not None and b["background_readback"] != read.get("background"):
            report["conflicts"].append({"slide": skey, "element": None, "field": "background", "base": b.get("background"),
                                        "ours": o.get("background"), "theirs": read.get("background"), "resolution": "deck kept"})
        else:
            plan["background"] = o["background"]
            report["applied"].append({"slide": skey, "element": None, "fields": ["background"]})
    elif b.get("background_readback") is not None and b["background_readback"] != read.get("background"):
        report["overrides"].append({"slide": skey, "element": None, "fields": ["background"]})
    # Speaker notes: text rules.
    bn, on, tn = b.get("notes") or "", o.get("notes") or "", read.get("notes") or ""
    deck_notes = (b.get("notes_readback") or "") != tn
    if bn != on:
        if not deck_notes:
            plan["notes"] = on
            report["applied"].append({"slide": skey, "element": None, "fields": ["notes"]})
        else:
            merged, clashes = diff3(b.get("notes_readback") or "", on, tn)
            if clashes:
                report["conflicts"].append({"slide": skey, "element": None, "field": "notes", "base": bn, "ours": on,
                                            "theirs": tn, "resolution": "deck kept"})
            elif merged != tn:
                plan["notes"] = merged
                report["applied"].append({"slide": skey, "element": None, "fields": ["notes"], "how": "merged"})
    elif deck_notes:
        report["overrides"].append({"slide": skey, "element": None, "fields": ["notes"]})
    return plan


def plan_order(base: dict, ours: dict, theirs: dict, plans: list[dict]) -> tuple[list[str], list[str]]:
    """Final slide order (live ids, "new:<key>" for slides to create) and the keys of slides
    the source moved. The source order is applied unless the deck reordered converter slides;
    kept slides the source removed stay after their base predecessor, user-added slides after
    their live predecessor."""
    live_order = [s["objectId"] for s in theirs["slides"]]
    base_by_id = {b.get("objectId"): b for b in base["slides"]}
    deleted = {p["objectId"] for p in plans if p["action"] == "delete"}
    existing = [sid for sid in live_order if sid in base_by_id and sid not in deleted]
    base_rank = {b.get("objectId"): k for k, b in enumerate(base["slides"])}
    deck_reordered = [base_rank[s] for s in existing] != sorted(base_rank[s] for s in existing)

    by_ours = {p["ours"]: p for p in plans if p.get("ours") is not None and p["action"] in ("update", "create")}
    source = [by_ours[j]["objectId"] or f"new:{by_ours[j]['key']}" for j in sorted(by_ours)]
    if deck_reordered:
        order = [sid for sid in live_order if sid not in deleted]
        # new slides after the live position of their ours predecessor
        for j in sorted(by_ours):
            p = by_ours[j]
            if p["action"] != "create":
                continue
            prev = next((by_ours[k]["objectId"] for k in range(j - 1, -1, -1) if k in by_ours and by_ours[k]["objectId"]), None)
            pos = order.index(prev) + 1 if prev in order else 0
            order.insert(pos, f"new:{p['key']}")
        moved = []
    else:
        order = list(source)
        # converter slides kept though the source removed them: after their base predecessor
        for p in plans:
            if p["action"] != "keep_removed":
                continue
            k = base_rank[p["objectId"]]
            prev = next((base["slides"][q]["objectId"] for q in range(k - 1, -1, -1)
                         if base["slides"][q].get("objectId") in order), None)
            order.insert(order.index(prev) + 1 if prev else 0, p["objectId"])
        # user-added slides (and converter slides not otherwise placed): after their live predecessor
        for k, sid in enumerate(live_order):
            if sid in order or sid in deleted:
                continue
            prev = next((live_order[q] for q in range(k - 1, -1, -1) if live_order[q] in order), None)
            order.insert(order.index(prev) + 1 if prev else 0, sid)
        before = [s for s in live_order if s in order]
        after = [s for s in order if s in before]
        moved = [base_by_id[s]["key"] for s, t in zip(before, after) if s != t and s in base_by_id] if before != after else []
    return order, moved
