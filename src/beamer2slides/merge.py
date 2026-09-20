"""Sync's three-way merge, pure (docs/sync.md): deck edits from read-backs, word-level diff3,
the merge rules per element unit and field, and slide add/delete/reorder planning.

Inputs are the base (snapshot.build_base), ours (the new conversion's slide entries with keys
inherited: snapshot.slide_entries) and theirs (snapshot.read_presentation of the live deck)."""

import re
from difflib import SequenceMatcher

from . import identity, snapshot

GEOMETRY_TOLERANCE = 0.05  # pt
SCALE_TOLERANCE = 1e-3
CONVERGED_PLACE = 2.0  # pt: a deck move the source now reproduces this closely counts as converged
MOVE_TOLERANCE = 0.05  # pt: members within this of the same step moved together (PDF pt)
EDIT_FIELDS = ("geometry", "text", "text_style", "shape_style", "image")
TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


# ---------------------------------------------------------------- text

def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def _hunks(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    sm = SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def text_merge(base: str, ours: str, theirs: str) -> tuple[str, list[dict], bool]:
    """Three-way merge of an element's text, one paragraph at a time. Returns the merged text, the
    paragraphs both sides rewrote (kept as the deck's, whole), and whether the result is safe to
    write.

    A paragraph is a bullet or a line: the unit an edit belongs to. When the source rewrites three
    bullets and the person changed one word in the third, the first two are the source's and the
    third stays the deck's. Merging the box as one run of words instead lets a conflict in one
    bullet decide the fate of all of them - and, where the conflicting words land inside a sentence
    the source rewrote around them, produces a line neither side wrote ("Seconds are rounded,
    milliseconds full dropped"). That is why a conflicting paragraph is taken whole and never
    spliced.

    Prose in one paragraph still merges word by word, and so does a box whose paragraphs cannot be
    lined up (the source added or removed one). `safe` is False only when that word-level fallback
    conflicts: the caller then keeps the deck's text and reports the conflict, as before."""
    bp, op, tp = base.split("\n"), ours.split("\n"), theirs.split("\n")
    if len(bp) < 2 or not len(bp) == len(op) == len(tp):
        merged, clashes = diff3(base, ours, theirs)
        return merged, clashes, not clashes
    out: list[str] = []
    conflicts: list[dict] = []
    for b, o, t in zip(bp, op, tp):
        if b == o or o == t:      # the source left it alone, or both arrived at the same words
            out.append(t)
        elif b == t:              # only the source changed it
            out.append(o)
        else:
            merged, clashes = diff3(b, o, t)
            if clashes:
                out.append(t)
                conflicts.append({"base": b, "ours": o, "theirs": t})
            else:
                out.append(merged)
    return "\n".join(out), conflicts, True


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


def text_edit_requests(object_id: str, current: str, target: str, cell: dict | None = None) -> list[dict]:
    """deleteText / insertText turning an object's text `current` into `target` (UTF-16 indices,
    applied back to front so earlier indices stay valid). `cell`: {"rowIndex", "columnIndex"} of a
    table cell instead of the object's own text.

    The newline every shape's and cell's text ends on is Slides' own and cannot be deleted: the API
    reads it back as part of the text but counts the length without it, so a `deleteText` reaching
    the end comes back as *"The end index (273) should not be greater than the existing text length
    (272)"* and the whole batch is refused (live fuzz seeds 608 and 616: the deck's edit deleted the
    last paragraph of a text box the source had also rewritten, so the merged text ends one
    paragraph earlier and the diff's last hunk ran to the end). It is on both sides, so leaving it
    out of the diff both keeps it where it is and puts an append before it rather than after."""
    where = {"cellLocation": cell} if cell else {}
    if current.endswith("\n"):
        current = current[:-1]
        target = target[:-1] if target.endswith("\n") else target
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
            reqs.append({"deleteText": {"objectId": object_id, **where, "textRange": {
                "type": "FIXED_RANGE", "startIndex": start, "endIndex": end}}})
        insert = "".join(b[j1:j2])
        if insert:
            reqs.append({"insertText": {"objectId": object_id, **where, "insertionIndex": start, "text": insert}})
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
    if ("image" in b or "image" in t) and not snapshot.same_picture(b.get("image"), t.get("image")):
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
            if oid == f"{base_el.get('main')}_g" and base_el.get("main") in objects:
                edits.setdefault("group", []).append(oid)  # its group taken apart, the objects still there
            else:
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


def uniform_changes(base_styles: list[dict], theirs_styles: list[dict], text_changed: bool = False) -> dict | None:
    """Style attributes the deck set on all of an object's text ({} when nothing changed, None
    when the change isn't uniform and so can't be re-applied to new text).

    A read-back holds the *distinct* styles of a text, so deleting a paragraph takes its style out of
    the list - the converter gives every paragraph the line spacing of its own PDF pitch, so that
    list nearly always shrinks. `text_changed` says the deck edited this text as well: a style that
    is only missing is then the style of words that are gone, not something a person set, and there
    is nothing to re-apply. Without that, deleting one bullet made every later source change to that
    box a `text_style` conflict, and the box kept a wording the source had long moved on from
    (scenario `last-paragraph`, and the shape is common: any deleted bullet does it)."""
    def canon(styles):
        return {repr(sorted(s.items())) for s in styles}
    if canon(base_styles) == canon(theirs_styles):
        return {}
    if not theirs_styles:
        return None
    if text_changed and canon(theirs_styles) <= canon(base_styles):
        return {}
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


def table_grid(text: str | None, dims: list | None) -> list[list[str]] | None:
    """A table read-back's cells (rows split at newlines, cells at tabs), or None when they don't
    make `dims` rows x columns: a cell holding a line break is ambiguous in that text."""
    rows = [row.split("\t") for row in (text or "").split("\n")]
    if not dims or len(rows) != dims[0] or any(len(row) != dims[1] for row in rows):
        return None
    return rows


def table_merge(base_text: str | None, ours_text: str, theirs_text: str | None,
                dims: list | None, theirs_dims: list | None) -> tuple[list[list[str]], bool] | None:
    """Word-level diff3 per cell: the merged cells and whether they are already what the source
    says. None when the two sides changed the same cell, or the tables don't line up (a row or
    column added on either side) - then the whole table is a conflict."""
    b = table_grid(base_text, dims)
    o = table_grid(ours_text, dims)
    t = table_grid(theirs_text, theirs_dims)
    if not b or not o or not t or theirs_dims != dims:
        return None
    out, converged = [], True
    for brow, orow, trow in zip(b, o, t):
        row = []
        for bc, oc, tc in zip(brow, orow, trow):
            merged, clashes = diff3(bc, oc, tc)
            if clashes:
                return None
            row.append(merged)
            converged = converged and merged == oc
        out.append(row)
    return out, converged


IR_STYLE_TO_API = {"strike": "strikethrough", "smallcaps": "smallCaps", "script": "baselineOffset", "color": "foregroundColor",
                   "highlight": "backgroundColor", "size": "fontSize", "font": "fontFamily", "family": "fontFamily",
                   "code": "fontFamily", "align": "alignment", "level": "bullet"}


def source_style_keys(base_el: dict, ours_el: dict) -> set[str]:
    """Style attributes (API names) whose values differ between two versions of an element's IR."""
    a, b = set(), set()
    identity._styles(identity.normalise_ir(base_el.get("ir") or {}), a)
    identity._styles(identity.normalise_ir(ours_el.get("ir") or {}), b)
    return {IR_STYLE_TO_API.get(k, k) for k, _ in a ^ b}


def deck_style_keys(base_rb: dict, theirs_rb: dict) -> set[str]:
    """Text style attributes the deck changed somewhere in an object."""
    out = set()
    for field in ("text_styles", "paragraph_styles"):
        a = {(k, repr(v)) for s in base_rb.get(field, []) for k, v in s.items()}
        b = {(k, repr(v)) for s in theirs_rb.get(field, []) for k, v in s.items()}
        out |= {k for k, _ in a ^ b}
    return out


def deck_attributes(style: dict, base_styles: list[dict]) -> dict:
    """The attributes the deck set on a run: how its style differs from the closest style the
    converter wrote into that object ({} if it is one of them)."""
    if not base_styles or style in base_styles:
        return {}
    closest = min(base_styles, key=lambda b: sum(1 for k in set(b) | set(style) if b.get(k) != style.get(k)))
    attrs = {k: v for k, v in style.items() if closest.get(k) != v and k != "link"}
    if "weight" in attrs or "fontFamily" in attrs:
        attrs.update({k: style[k] for k in ("fontFamily", "weight") if k in style})
    return attrs


def styling_lost(base_rb: dict, theirs_rb: dict, merged: str | None) -> bool:
    """Whether re-applying the deck's run styling to the merged text would leave some of it behind.

    `sync.style_range_requests` puts the person's run styles back onto *the same words* of the text
    sync is about to write. Words the source replaced are not there to put them back onto, and a
    word bolded in the deck whose sentence the source has since rewritten is styling that simply
    ends. Nothing can be done about that - the words it was on are gone - but the report has to say
    so instead of promising the styling was kept."""
    spans, before = theirs_rb.get("run_spans"), theirs_rb.get("text") or ""
    if not spans or merged is None or merged == before:
        return False
    base_styles = base_rb.get("text_styles") or []
    was, now = tokens(before), tokens(merged)
    at, ends = 0, []
    for t in was:
        at += len(t)
        ends.append(at)
    kept = {k for i, _, n in SequenceMatcher(None, was, now, autojunk=False).get_matching_blocks()
            for k in range(i, i + n)}
    for start, end, style in spans:
        if not deck_attributes(style, base_styles):
            continue                                        # the converter's own styling, not the person's
        # Word by word, not letter by letter: a bolded word the source deleted is gone even when
        # some of its letters turn up elsewhere in the new sentence.
        on = [k for k, e in enumerate(ends) if e - len(was[k]) < end and e > start and was[k].strip()]
        if on and not any(k in kept for k in on):
            return True
    return False


def converged_fields(anchor: dict, first: dict, base_by: dict, ours_by: dict, edits: dict, theirs_rb: dict,
                     scale: float | None) -> dict | None:
    """Source fields the deck already shows: {"source": {text, size, position}, "deck": {text, geometry}}
    (None if none). Only for a unit whose other members the source left alone."""
    main = anchor.get("main")
    if not main or not theirs_rb or anchor["key"] != first["key"] or set(base_by) != set(ours_by) or \
            any(identity.source_changes(base_by[k], ours_by[k]) for k in base_by if k != anchor["key"]):
        return None
    source, deck = set(), set()
    if "text" in edits and set(edits["text"]) == {main}:
        live = theirs_rb.get("text") or ""
        if anchor["kind"] == "text":
            same = collapse_holes(live) == collapse_holes(predicted_text(first["ir"]))
        elif anchor["kind"] == "table":
            same = [[" ".join(c.split()) for c in row.split("\t")] for row in live.split("\n")] == \
                [[" ".join(c.split()) for c in row.split("\t")] for row in identity.plain_text(first["ir"]).split("\n")]
        else:
            same = False
        if same:
            source |= {"text", "size"}
            deck.add("text")
    if "geometry" in edits and set(edits["geometry"]) == {main} and scale:
        base_rb = anchor.get("readback", {}).get(main, {})
        bb, ob = anchor["fingerprint"]["bbox"], first["fingerprint"]["bbox"]
        box = base_rb.get("box") or [0.0, 0.0]
        want = [box[0] + (ob[0] - bb[0]) * scale, box[1] + (ob[1] - bb[1]) * scale]
        if base_rb.get("box") and max(abs(a - b) for a, b in zip(want, theirs_rb["box"][:2])) <= CONVERGED_PLACE and \
                all(abs(x - y) <= SCALE_TOLERANCE for x, y in zip(base_rb["transform"][:4], theirs_rb["transform"][:4])):
            source.add("position")
            deck.add("geometry")
    return {"source": source, "deck": deck} if deck else None


def unit_shift(base_by: dict, ours_by: dict) -> tuple[float, float] | None:
    """The step by which the source moved the unit as a whole, None when it didn't move that way.

    Sync writes a `move` by moving the unit's *top* object (the group, when the unit is grouped),
    so one step has to fit every member. A unit whose members drifted apart - the source re-placed
    an inline formula picture inside its line, or moved the paragraph while an anchored picture
    stayed - can't be written that way and has to be recreated instead."""
    if set(base_by) != set(ours_by) or not base_by:
        return None
    shifts = []
    for key, base_el in base_by.items():
        bb = (base_el.get("fingerprint") or {}).get("bbox")
        ob = (ours_by[key].get("fingerprint") or {}).get("bbox")
        if not bb or not ob:
            return None
        shifts.append((ob[0] - bb[0], ob[1] - bb[1]))
    for i in (0, 1):
        values = [s[i] for s in shifts]
        if max(values) - min(values) > MOVE_TOLERANCE:
            return None
    dx, dy = shifts[0]  # the anchor's step; every member agrees with it
    return None if max(abs(dx), abs(dy)) <= MOVE_TOLERANCE else (dx, dy)


def geometry_writable(members: list[dict], slide_read: dict | None) -> bool:
    """Whether the deck's move of this unit can be put back onto a rewritten unit.

    Sync re-applies it by transforming the unit's *top* object (`sync.override_requests`), so the
    person's edit only survives when every object of the unit went along with that top. A formula
    picture dragged out of its line inside the group, or a group member nudged on its own, moved
    by itself: recreating the unit would put it back where the converter had it while the report
    says the deck's geometry was kept. Such a unit is kept as the deck has it instead."""
    objects = (slide_read or {}).get("objects", {})
    top = unit_top(members, slide_read) if objects else None
    base_top = next((m["readback"][top] for m in members if top in m.get("readback", {})), None)
    if not top or not base_top or top not in objects:
        return True  # nothing to judge it by: leave the old behaviour
    step = snapshot.compose(objects[top]["transform"], snapshot.invert(base_top["transform"]))
    for m in members:
        for oid, b in (m.get("readback") or {}).items():
            live = objects.get(oid)
            if live is None:
                continue  # a part the person deleted: handled before this ("part_deleted")
            want = snapshot.compose(step, b["transform"])
            if any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(want[:4], live["transform"][:4])) or \
                    any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(want[4:], live["transform"][4:])):
                return False
    return True


def plan_unit(skey: str, ukey: str, base_members: list[dict] | None, ours_members: list[dict] | None,
              slide_read: dict | None, report: dict, scale: float | None = None, adopt=None) -> dict:
    """The action for one element unit: keep, recreate (with deck overrides), create, delete,
    move, adopt (the deck already shows the source's change) or adopt_object (the deck's own
    object is what the source now draws); conflicts and overrides go to `report`. `scale`: deck pt
    per PDF pt. `adopt(skey, ours_members, slide_read, oid=None)`: the live object showing the same
    picture as the unit's new one - a user object, or `oid` itself (sync.picture_adopter)."""
    where = {"slide": skey, "element": ukey}
    if base_members is None:
        oid = adopt(skey, ours_members, slide_read) if adopt and slide_read else None
        if oid:
            # e.g. after a pull: the source draws a picture the person added to the deck.
            report["converged"].append({**where, "field": "image", "how": "the deck already shows it"})
            return {"key": ukey, "action": "adopt_object", "objectId": oid}
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
    same = converged_fields(anchor, first, base_by, ours_by, edits, theirs_rb, scale)
    if same and src <= same["source"]:
        # The deck already shows what the source now says (e.g. deck edits pulled into the source):
        # nothing to write; the base takes the deck's version of those fields.
        for field in same["deck"]:
            report["converged"].append({**where, "field": field, **({"value": theirs_rb.get("text")} if field == "text" else {})})
        rest = edited - same["deck"]
        if rest:
            report["overrides"].append({**where, "fields": sorted(rest)})
        return {**action, "action": "adopt", "adopt": sorted(same["deck"])}
    shift = unit_shift(base_by, ours_by)
    if "position" in src and src <= {"position", "size"} and "geometry" not in edited and shift:
        # Only the place changed in the source, and the whole unit moved by the same step: move the
        # edited deck objects there (sync moves the unit's top object). A member that moved on its
        # own - a formula picture the source re-placed inside its line - can't be written that way,
        # so that unit is recreated instead (`shift` is None).
        report["applied"].append({**where, "fields": ["position"], "how": "deck object moved"})
        report["overrides"].append({**where, "fields": sorted(edited)})
        return {**action, "action": "move", "delta": list(shift)}
    overrides: dict = {}
    conflicts = []

    def conflict(field, base_v, ours_v, theirs_v, resolution="deck kept"):
        conflicts.append({**where, "field": field, "base": base_v, "ours": ours_v, "theirs": theirs_v, "resolution": resolution})

    keep = False
    # What the object will say once this unit is written: the source's new text, or the merge of it
    # with the deck's. `styling_lost` needs it to see whether the styled words are still in there.
    written = collapse_holes(predicted_text(first["ir"])) if first.get("kind") == "text" else None
    if "image" in edited:
        if adopt and slide_read and adopt(skey, ours_members, slide_read, main):
            # The picture in the deck is the one the source now draws (a figure pull replaced).
            report["converged"].append({**where, "field": "image", "how": "the deck's picture is what the source draws"})
            rest = edited - {"image", "geometry"}
            if rest:
                report["overrides"].append({**where, "fields": sorted(rest)})
            return {**action, "action": "adopt", "adopt": sorted(edited & {"image", "geometry"})}
        conflict("image", "picture", sorted(src), "replaced in the deck")
        keep = True
    if "text" in edited and not keep:
        if anchor["kind"] == "table" and set(edits["text"]) == {main}:
            cells = table_merge(base_rb.get("text"), identity.plain_text(first["ir"]), theirs_rb.get("text"),
                                base_rb.get("table"), theirs_rb.get("table"))
            if cells is None:
                conflict("text", anchor["fingerprint"]["text"], first["fingerprint"]["text"], theirs_rb.get("text"))
                keep = True
            else:
                overrides["text"] = {"table": True, "base": base_rb.get("text") or "", "theirs": theirs_rb.get("text") or "",
                                     "dims": base_rb.get("table")}
                if cells[1]:
                    report["converged"].append({**where, "field": "text", "value": theirs_rb.get("text")})
        elif anchor["kind"] != "text" or set(edits["text"]) != {main}:
            conflict("text", anchor["fingerprint"]["text"], first["fingerprint"]["text"], theirs_rb.get("text"))
            keep = True
        else:
            b, o, t = (collapse_holes(x) for x in (base_rb.get("text") or "", predicted_text(first["ir"]), theirs_rb.get("text") or ""))
            merged, clashes, safe = text_merge(b, o, t)
            if not safe or (clashes and merged == t):
                # Nothing of the source's survived the merge (or it cannot be written safely): the
                # deck's text stands as it is, and there is nothing to write.
                conflict("text", b, o, t)
                keep = True
            else:
                # Paragraphs both sides rewrote are conflicts of their own: the deck keeps them,
                # and the rest of the box still takes what the source now says.
                for c in clashes:
                    conflict("text", c["base"], c["ours"], c["theirs"])
                overrides["text"] = {"base": base_rb.get("text") or "", "theirs": theirs_rb.get("text") or ""}
                written = merged
                if merged == o:
                    report["converged"].append({**where, "field": "text", "value": theirs_rb.get("text")})
    if "text_style" in edited and not keep:
        cut = main in edits.get("text", ())  # the deck edited this text too (see `uniform_changes`)
        runs = uniform_changes(base_rb.get("text_styles", []), theirs_rb.get("text_styles", []), cut)
        paras = uniform_changes(base_rb.get("paragraph_styles", []), theirs_rb.get("paragraph_styles", []), cut)
        # (a source "style" change can be list levels or sizes; only the same attributes clash)
        clash = "style" in src and source_style_keys(anchor, first) & deck_style_keys(base_rb, theirs_rb)
        if paras is None or set(edits["text_style"]) != {main} or anchor["kind"] not in ("text", "table"):
            conflict("text_style", "style", sorted(src), "restyled in the deck")
            keep = True
        elif runs is None or anchor["kind"] == "table":
            # Some words restyled (or a table): the deck's run styles go onto the same words of the
            # new text (sync.style_range_requests).
            overrides["text_style"] = {"runs": {}, "paragraphs": paras, "ranges": True}
            if styling_lost(base_rb, theirs_rb, written):
                # ... and some of those words are not in the new text at all. The styling on them
                # ends here, and saying nothing would make the report claim it was kept.
                conflict("text_style", "style", "the words it was on were replaced", "restyled in the deck",
                         "the styling of the replaced words is gone")
            if clash:
                conflict("text_style", "style", "restyled in the source", "restyled in the deck", "deck style re-applied")
        else:
            overrides["text_style"] = {"runs": runs, "paragraphs": paras}
            if clash:
                conflict("text_style", "style", "restyled in the source", "restyled in the deck", "deck style re-applied")
    if "shape_style" in edited and not keep:
        if anchor["kind"] != "shape" or set(edits["shape_style"]) != {main}:
            conflict("shape_style", "style", sorted(src), "restyled in the deck")
            keep = True
        else:
            overrides["shape_style"] = theirs_rb.get("shape_style")
            if "style" in src:
                conflict("shape_style", "style", "restyled in the source", "restyled in the deck", "deck style re-applied")
    if "geometry" in edited and not keep and not geometry_writable(base_members, slide_read):
        # The person moved something inside the unit; a rewritten unit can't be put back that way.
        conflict("geometry", anchor["fingerprint"]["bbox"], first["fingerprint"]["bbox"], theirs_rb.get("box"),
                 "deck kept (the deck moved a part of the element on its own)")
        keep = True
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


def report_label_moves(moves: list[dict], report: dict) -> None:
    r"""Conflicts for the labels `identity.label_moves` found somewhere else than it left them.

    There is no resolution to offer here, which is the point: a three-way merge writes what it can
    and hands back what it cannot, and which frame is which is exactly what it cannot. What sync
    did with the slide is said plainly - either the content decided, or the label was followed
    anyway - so whoever reads this knows what they are looking at before they go and fix the
    `.tex` (docs/ai-authoring.md).
    """
    for m in moves:
        moved = m["verdict"] == "moved"
        ours_says = f"the frame `{m['label']}` now says \"{(m['ours_title'] or '').strip()}\""
        if m.get("frame_is"):
            ours_says += f", which is what the slide `{m['frame_is']}` says"
        base_says = f"the slide `{m['label']}` says \"{(m['base_title'] or '').strip()}\""
        if m.get("slide_is"):
            base_says += f", which the source now has under \"{m['slide_is']}\""
        report["conflicts"].append({
            "slide": m.get("slide") or m["label"], "element": None, "field": "label",
            "base": base_says, "ours": ours_says, "theirs": None,
            "resolution": ("the label moved: identity taken from the content instead" if moved else
                           "either the label moved or that passage did: followed the label, nothing re-paired"),
        })
        # An `unsure` verdict re-pairs nothing, so saying only "check the .tex" leaves the reader
        # to work out what happens if they do not. What happens is that this sync writes the frame
        # now carrying the label onto the slide the label names - the slide with somebody's edits
        # on it - so that slide is the one to look at, and it is named.
        at_risk = (f" This sync writes the frame carrying `{m['label']}` onto the slide "
                   f"`{m.get('slide') or m['label']}`, edits and all, so that is the slide to look at."
                   if not moved else "")
        report["warnings"].append(
            f"label `{m['label']}` is not on the frame this deck's slide was made from" + (
                ". Deck edits belong to the words a person edited, so sync went by the content and not by the "
                "label. Put the label back on its own frame" if moved else
                ", or a passage moved between two frames - from the PDF alone the two look the same. Sync "
                "followed the label. Check the `.tex`: if the label moved, put it back") +
            ": docs/labels.md, \"If a label does change\"." + at_risk)


def plan_merge(base: dict, ours: dict, theirs: dict, adopt=None) -> dict:
    """ours: {"slides": [slide entries with inherited keys], "pairs": {ours index: base index}}.
    `adopt`: see plan_unit (a picture the deck already shows).
    Returns {"slides": [per slide plan], "order": [live slide ids or "new:<key>"], "report"}."""
    report = empty_report()
    live = {s["objectId"]: s for s in theirs["slides"]}
    base_slides = base["slides"]
    pairs = {int(k): v for k, v in ours["pairs"].items()}
    matched_base = set(pairs.values())
    plans = []
    report_label_moves(ours.get("label_moves") or [], report)

    weak = {int(k): v for k, v in (ours.get("weak_pairs") or {}).items()}
    for j, o in enumerate(ours["slides"]):
        i = pairs.get(j)
        b = base_slides[i] if i is not None else None
        if b is not None and weak.get(j) == "place" and not o.get("label"):
            # `identity.gap_pairs`: this frame has no label, and the source changed enough of it
            # that only its place says which frame it is. Nothing was at risk - the alternative was
            # a second slide beside this one - but the next version has one hook fewer to hang on.
            report["warnings"].append(
                f"slide {b['key']}: this frame has no label, and the source changed its title and much of what "
                f"it says; it was matched by where it stands, between the frames around it. Move it too, or "
                f"rewrite the rest of it, and there is nothing left to recognise it by - give it a label "
                f"(`beamer2slides label`), see docs/labels.md.")
        if b is not None and weak.get(j) == "twins" and not o.get("label"):
            # `identity.align_slides`: this frame has no label and says about as much as its
            # neighbour does, so putting it on this slide and putting it on the other one are the
            # same alignment as far as the words go. It was put here; a person who knows which
            # frame is which should say so with a label before the next sync repeats the guess.
            report["warnings"].append(
                f"slide {b['key']}: this frame has no label, and it and the slides around it say so nearly "
                f"the same thing that the match could as well have been one of them; it was matched here. "
                f"Your edits on that slide are safe either way, but which slide this frame writes to next "
                f"time is a coin toss - give it a label (`beamer2slides label`), see docs/labels.md.")
        if b is not None and b.get("label") and o.get("label") != b.get("label"):
            # The label is gone or different, and the content recognised the frame anyway. Nothing
            # is at risk this time; the next version of the source has one hook fewer to hang on.
            now = f"`{o['label']}`" if o.get("label") else "gone"
            report["warnings"].append(
                f"slide {b['key']}: the frame's label is {now} now, not `{b['label']}`; this slide was "
                f"matched by what it says instead. A label is what makes a slide's identity survive "
                f"an edit the content alone cannot explain - see docs/labels.md.")
        read = live.get(b["objectId"]) if b and b.get("objectId") else None
        if b is None:
            report["slides"]["created"].append(o["key"])
            plans.append({"key": o["key"], "action": "create", "ours": j, "base": None, "objectId": None})
            continue
        if read is None:
            # (frame counters change whenever a frame is added before: not worth a conflict)
            content = lambda els: [e for e in els if e.get("role") != "footer"]  # noqa: E731
            changed = any(identity.source_changes(e, oe) for e in content(b["elements"]) for oe in content(o["elements"])
                          if e["key"] == oe["key"]) \
                or {e["key"] for e in content(b["elements"])} != {e["key"] for e in content(o["elements"])}
            if changed:
                report["conflicts"].append({"slide": o["key"], "element": None, "field": "slide", "base": "slide",
                                            "ours": "changed", "theirs": "deleted", "resolution": "kept deleted"})
            plans.append({"key": o["key"], "action": "gone", "ours": j, "base": i, "objectId": None})
            continue
        plans.append(plan_slide(b, o, read, report, base, j, i, adopt))

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

    kept_keys = {k["slide"] for k in report["slides"]["kept"]}
    for m in ours.get("near_misses") or []:
        # `identity.near_misses`: nothing paired these two, and nothing should have - but they say
        # too much of the same for the author not to be told the question came up. Either the source
        # really did write a new frame beside a dropped one, and this is noise, or one frame was
        # changed so far in one version that sync could not follow it.
        if m["slide"] not in kept_keys and m["slide"] not in report["slides"]["deleted"]:
            continue    # the slide is still in the deck for another reason: nothing to report
        where = ("the deck keeps the old slide, with your edits on it, beside the new one"
                 if m["slide"] in kept_keys else "the old slide was untouched, so it is gone")
        report["warnings"].append(
            f"slide {m['slide']}: the source has no frame this slide could be matched to, and the frame "
            f"{m['title']!r} is new - but the two say much of the same thing. If they are one frame, it was "
            f"retitled, reworded and moved too much in one version to be followed, and {where}. Give that "
            f"frame a label (`beamer2slides label`) and this cannot happen to it again, see docs/labels.md.")

    base_ids = {b.get("objectId") for b in base_slides}
    for s in theirs["slides"]:
        if s["objectId"] in base_ids:
            continue
        # (a slide duplicated in Slides carries the tags of the original's objects)
        copies = {rb["title"][4:].rsplit("/", 3)[0] for rb in s["objects"].values() if (rb.get("title") or "").startswith("b2s:")}
        report["slides"]["user_added"].append({"objectId": s["objectId"], "copy_of": sorted(copies) or None})
    order, moved = plan_order(base, ours, theirs, plans, report)
    report["slides"]["moved"] = moved
    return {"slides": plans, "order": order, "report": report}


def background_edited(b: dict, read: dict) -> bool:
    """The deck changed a base slide's background (a picture compares by its pixels: contentUrls change)."""
    return b.get("background_readback") is not None and not snapshot.same_background(b["background_readback"], read.get("background"))


def slide_touched(b: dict, read: dict) -> list[str]:
    """Why a slide counts as edited in the deck (empty: untouched)."""
    why = []
    if any(deck_edits(e, read) for e in b["elements"]):
        why.append("elements edited")
    if user_objects(b, read):
        why.append("objects added")
    if b.get("notes_readback", "") != read.get("notes", ""):
        why.append("notes edited")
    if background_edited(b, read):
        why.append("background changed")
    return why


def deck_scale(base: dict) -> float | None:
    """Deck pt per PDF pt."""
    if base.get("scale"):
        return base["scale"]
    if base.get("deck_page_size") and base.get("page_size"):
        return base["deck_page_size"][0] / base["page_size"][0]
    return None


def plan_slide(b: dict, o: dict, read: dict, report: dict, base: dict, j: int, i: int, adopt=None) -> dict:
    skey = o["key"]
    bu, ou = units(b["elements"]), units(o["elements"])
    unit_plans = []
    for ukey in list(ou) + [k for k in bu if k not in ou]:
        unit_plans.append({**plan_unit(skey, ukey, bu.get(ukey), ou.get(ukey), read, report, deck_scale(base), adopt),
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
    adopted = {u["objectId"] for u in unit_plans if u["action"] == "adopt_object"}
    for u in user_objects(b, read):
        if u["objectId"] not in adopted:  # (an adopted object is the source's element now)
            report["user_objects"].append({"slide": skey, **u})
    plan = {"key": skey, "action": "update", "ours": j, "base": i, "objectId": b["objectId"], "units": unit_plans}
    # Background: the source's picture or colour unless the deck changed it.
    if b.get("background") != o.get("background"):
        if background_edited(b, read):
            report["conflicts"].append({"slide": skey, "element": None, "field": "background", "base": b.get("background"),
                                        "ours": o.get("background"), "theirs": read.get("background"), "resolution": "deck kept"})
        else:
            plan["background"] = o["background"]
            report["applied"].append({"slide": skey, "element": None, "fields": ["background"]})
    elif background_edited(b, read):
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


def has_writes(mplan: dict, live_order: list[str]) -> bool:
    """Whether a merge plan changes the live deck at all (a sync with no changes sends nothing)."""
    for p in mplan["slides"]:
        if p["action"] in ("create", "delete"):
            return True
        if p["action"] == "update" and (any(u["action"] in ("create", "recreate", "delete", "move") for u in p["units"])
                                        or p.get("background") or p.get("notes") is not None):
            return True
    final = [x for x in mplan["order"] if not x.startswith("new:")]
    current = [s for s in live_order if s in final]
    return current != [s for s in final if s in current]


def _out_of_place(was: list, now: list) -> list:
    """The items of `now` that are not in the longest run `was` and `now` agree on - the ones
    somebody picked up and put down elsewhere, rather than the ones that drifted because those did."""
    keep = set()
    for a, b, n in SequenceMatcher(None, was, now, autojunk=False).get_matching_blocks():
        keep.update(now[b:b + n])
    return [x for x in now if x not in keep]


def plan_order(base: dict, ours: dict, theirs: dict, plans: list[dict],
               report: dict | None = None) -> tuple[list[str], list[str]]:
    """Final slide order (live ids, "new:<key>" for slides to create) and the keys of slides
    the source moved. The source's order is the ground; a slide the deck itself picked up goes
    back where the deck put it (deck edits win, and a person who drags one slide does not mean to
    freeze the other forty). Kept slides the source removed stay after their base predecessor,
    user-added slides after their live predecessor."""
    live_order = [s["objectId"] for s in theirs["slides"]]
    base_by_id = {b.get("objectId"): b for b in base["slides"]}
    deleted = {p["objectId"] for p in plans if p["action"] == "delete"}
    existing = [sid for sid in live_order if sid in base_by_id and sid not in deleted]
    base_rank = {b.get("objectId"): k for k, b in enumerate(base["slides"])}
    was = sorted(existing, key=lambda sid: base_rank[sid])

    by_ours = {p["ours"]: p for p in plans if p.get("ours") is not None and p["action"] in ("update", "create")}
    order = [by_ours[j]["objectId"] or f"new:{by_ours[j]['key']}" for j in sorted(by_ours)]
    source_moved = set(_out_of_place(was, [sid for sid in order if sid in base_rank and sid in existing]))
    # converter slides kept though the source removed them: after their base predecessor
    for p in plans:
        if p["action"] != "keep_removed":
            continue
        k = base_rank[p["objectId"]]
        prev = next((base["slides"][q]["objectId"] for q in range(k - 1, -1, -1)
                     if base["slides"][q].get("objectId") in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, p["objectId"])
    # slides the deck moved: out of their base order there, so back beside what they follow now
    for sid in _out_of_place(was, existing):
        if sid not in order:
            continue
        order.remove(sid)
        k = live_order.index(sid)
        prev = next((live_order[q] for q in range(k - 1, -1, -1) if live_order[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
        if report is not None and sid in source_moved:
            report["warnings"].append(
                f"slide {base_by_id[sid]['key']}: both the source and the deck moved this slide; "
                f"it stays where the deck put it.")
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
