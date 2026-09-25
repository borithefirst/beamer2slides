"""Sync's three-way merge, pure (docs/sync.md): deck edits from read-backs, word-level diff3,
the merge rules per element unit and field, and slide add/delete/reorder planning.

Inputs are the base (snapshot.build_base), ours (the new conversion's slide entries with keys
inherited: snapshot.slide_entries) and theirs (snapshot.read_presentation of the live deck)."""

import hashlib
import json
import re
from difflib import SequenceMatcher

from . import identity, snapshot

GEOMETRY_TOLERANCE = 0.05  # pt
SCALE_TOLERANCE = 1e-3
CONVERGED_PLACE = 2.0  # pt: a deck move the source now reproduces this closely counts as converged
MOVE_TOLERANCE = 0.05  # pt: members within this of the same step moved together (PDF pt)
EDIT_FIELDS = ("geometry", "text", "text_style", "shape_style", "image")
ADOPTED = "adopt"  # a base's `origin` when the deck is a person's own (`adopt_sync.ORIGIN`, which imports this)
TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
TAKEN_SAYS = "the source's version was written (asked for by `--take-source`)"
# Fields whose conflict is about what something *says*, and so can be settled for the source.
# Existence and identity are not among them on purpose: see `Resolutions`.
TAKEABLE_FIELDS = ("text", "text_style", "shape_style", "image", "geometry", "background", "notes")
# How a unit both sides moved is written (`plan_unit`): the person's move and resize, carried to where
# the source now has it. The loss oracle holds sync to this wording's promise.
GEOMETRY_CARRIED = "the deck's move and size kept, on top of the source's move"
# The source's changed fields (identity.source_changes) a unit's conflict on each field is about.
CONFLICT_COVERS = {"text": {"text"}, "text_style": {"style"}, "shape_style": {"style"},
                   "geometry": {"position", "size"}, "image": {"image"}}


# ---------------------------------------------------------------- conflicts a person can settle

def conflict_id(slide: str, element: str | None, field: str, base, ours, theirs) -> str:
    """A name for one disagreement: short enough to read out of a report and type back in.

    It is a digest of the disagreement itself - where it is, which field, and what each of the
    three sides says - so it is the same id for as long as the same two changes stand against
    each other, and a different one the moment either side moves. That is the whole safety of
    `--take-source`: an id copied out of yesterday's report cannot land on a disagreement that
    has since become another one. It matches nothing, the deck keeps what it has, and the report
    says the id was not found."""
    blob = json.dumps([slide, element, field, base, ours, theirs], sort_keys=True,
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


class Resolutions:
    """What a person decided after reading a sync report.

    A three-way merge can settle everything the two sides did not both touch, and where they did
    it keeps the deck's version and says so - the deck is the side that cannot be recompiled.
    That rule is right by default and wrong sometimes, and until now the only ways out were to
    retype the passage in Slides or to `pull` the deck's wording back into the `.tex`. So a
    conflict carries an id (`conflict_id`) and `--take-source <id>` says: at that one spot, the
    source is right.

    What it will settle is what a conflict is *about*: `TAKEABLE_FIELDS` are the fields that say
    what something says - its words, its styling, its picture, its place. A conflict about
    whether something exists at all (an element or a slide the source removed and somebody
    edited) is not settled here however loudly it is asked for. Overwriting a paragraph leaves
    the deck's version in the report, verbatim, and the way back is to paste it; deleting leaves
    nothing, and that is what `--force-rebuild` is for, with its backup and its refusal.

    Ids that matched nothing are `unused` - reported, never silently dropped."""

    def __init__(self, take_source=()):
        self.wanted = {str(x).strip().lower() for x in (take_source or ()) if str(x).strip()}
        self.used: set[str] = set()

    def take(self, cid: str) -> bool:
        if cid not in self.wanted:
            return False
        self.used.add(cid)
        return True

    @property
    def unused(self) -> list[str]:
        return sorted(self.wanted - self.used)


def conflict_entry(res, slide: str, element: str | None, field: str, base, ours, theirs,
                   resolution: str = "deck kept", takeable: bool = False) -> tuple[dict, bool]:
    """One conflict as the report holds it, and whether the person asked to settle it for the
    source. `takeable` is the caller saying the source's version *can* be written here: the id is
    on every conflict either way, since it is also how one talks about one that cannot."""
    cid = conflict_id(slide, element, field, base, ours, theirs)
    taken = bool(takeable and res is not None and res.take(cid))
    entry = {"slide": slide, "element": element, "id": cid, "field": field, "base": base,
             "ours": ours, "theirs": theirs, "resolution": TAKEN_SAYS if taken else resolution}
    if takeable:
        entry["takeable"] = True
    return entry, taken


# ---------------------------------------------------------------- text

def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def _hunks(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    sm = SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def text_merge(base: str, ours: str, theirs: str, take=()) -> tuple[str, list[dict], bool]:
    """Three-way merge of an element's text, one paragraph at a time. Returns the merged text, the
    paragraphs both sides rewrote (kept as the deck's, whole, and carrying their index), and
    whether the result is safe to write.

    A paragraph is a bullet or a line: the unit an edit belongs to. When the source rewrites three
    bullets and the person changed one word in the third, the first two are the source's and the
    third stays the deck's. Merging the box as one run of words instead lets a conflict in one
    bullet decide the fate of all of them - and, where the conflicting words land inside a sentence
    the source rewrote around them, produces a line neither side wrote ("Seconds are rounded,
    milliseconds full dropped"). That is why a conflicting paragraph is taken whole and never
    spliced.

    Prose in one paragraph still merges word by word. When a side added or removed a paragraph the
    paragraphs are lined up three ways first (`paragraph_merge`), so a bullet the source appended
    still arrives beside one both sides reworded: merged as one run of words instead, that one
    clash kept the deck's whole box and the new bullet never came (edit hunt h6b, twice in a row;
    h4b). Only a box with a single paragraph merges word by word as a whole. `safe` is False only
    when that word-level merge conflicts: the caller then keeps the deck's text and reports the
    conflict, as before.

    `take`: paragraph indices a person asked to settle for the source after reading the report
    (`Resolutions`, docs/sync.md "Taking the source's version"). Such a paragraph takes the
    source's words whole, exactly as a kept one takes the deck's, and is no longer a conflict.
    The index is the handle because it is the one thing both sides of the write agree on: the
    planner merges the *predicted* text and sync merges what the deck actually holds, and
    `collapse_holes` is the only difference between them - it never adds or drops a newline."""
    bp, op, tp = base.split("\n"), ours.split("\n"), theirs.split("\n")
    if len(bp) < 2:
        merged, clashes = diff3(base, ours, theirs)
        return merged, clashes, not clashes
    if not len(bp) == len(op) == len(tp):
        out, conflicts = paragraph_merge(bp, op, tp, take)
        return "\n".join(out), conflicts, True
    out: list[str] = []
    conflicts: list[dict] = []
    for k, (b, o, t) in enumerate(zip(bp, op, tp)):
        out.append(_paragraph(b, o, t, k, take, conflicts))
    return "\n".join(out), conflicts, True


def _paragraph(b: str, o: str, t: str, k: int, take, conflicts: list[dict]) -> str:
    """One paragraph of three versions, merged (`k`: its index in the deck's text, the handle a
    `take` names and a conflict carries)."""
    if b == o or o == t:      # the source left it alone, or both arrived at the same words
        return t
    if b == t:                # only the source changed it
        return o
    merged, clashes = diff3(b, o, t)
    if not clashes:
        return merged
    if k in take:
        return o
    conflicts.append({"base": b, "ours": o, "theirs": t, "paragraph": k})
    return t


def _clusters(hunks: list[tuple[str, tuple]]) -> list[list]:
    """Hunks of both sides grouped where they touch (`_clash`), joined until stable."""
    clusters: list[list] = []
    for side, h in sorted(hunks, key=lambda s: (s[1][0], s[1][1], s[0])):
        for c in clusters:
            if any(_clash(h, other) for _, other in c):
                c.append((side, h))
                break
        else:
            clusters.append([(side, h)])
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
    return clusters


def _version(base: list, c: list, side: str, c0: int, c1: int) -> list:
    """What one side made of the base's items c0..c1 (a cluster)."""
    out, pos = [], c0
    for s, (h0, h1, rep) in sorted(c, key=lambda x: (x[1][0], x[1][1])):
        if s != side:
            continue
        out += base[pos:h0] + rep
        pos = h1
    return out + base[pos:c1]


SAME_PARAGRAPH = 0.5   # word similarity above which a rewritten paragraph is the old one reworded


def _paragraph_hunks(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    """`_hunks` over paragraphs, with a replacement of n by m paragraphs taken apart: a diff reads
    "rewrote the last bullet, then added one" as one bullet replaced by two, and then a rewording on
    the other side clashes with the addition too. Each new paragraph is paired with the old one it
    rewords most (word similarity SAME_PARAGRAPH or more, in order: the best monotone pairing), and
    the rest are paragraphs added or removed on their own."""
    out = []
    sm = SequenceMatcher(None, base, other, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op != "replace" or i2 - i1 == j2 - j1:
            out.append((i1, i2, other[j1:j2]))
            continue
        a, b = base[i1:i2], other[j1:j2]
        sim = [[SequenceMatcher(None, tokens(x), tokens(y), autojunk=False).ratio() for y in b] for x in a]
        best = [[0.0] * (len(b) + 1) for _ in range(len(a) + 1)]
        for i in range(len(a) - 1, -1, -1):
            for j in range(len(b) - 1, -1, -1):
                pair = best[i + 1][j + 1] + sim[i][j] if sim[i][j] >= SAME_PARAGRAPH else -1.0
                best[i][j] = max(best[i + 1][j], best[i][j + 1], pair)
        i = j = 0
        while i < len(a) or j < len(b):
            if i < len(a) and j < len(b) and sim[i][j] >= SAME_PARAGRAPH and \
                    best[i][j] == best[i + 1][j + 1] + sim[i][j]:
                out.append((i1 + i, i1 + i + 1, [b[j]]))
                i, j = i + 1, j + 1
            elif j < len(b) and (i == len(a) or best[i][j] == best[i][j + 1]):
                out.append((i1 + i, i1 + i, [b[j]]))          # a paragraph added here
                j += 1
            else:
                out.append((i1 + i, i1 + i + 1, []))          # one removed
                i += 1
    return out


def paragraph_merge(bp: list[str], op: list[str], tp: list[str], take=()) -> tuple[list[str], list[dict]]:
    """Three versions of a box's paragraphs lined up as a line-based diff3 does, a paragraph being
    the unit: what one side alone added, removed or rewrote is taken; where both rewrote the same
    paragraphs one for one, each merges as `text_merge` merges one (`_paragraph`); any other
    overlap (both inserted at one place, one rewrote what the other removed) keeps the deck's
    paragraphs there, whole, as one conflict. A conflict's `paragraph` is where its paragraphs start
    in the deck's text, which neither side's choices move: the planner and the sync, merging the
    predicted and the live text, name it alike."""
    hunks = [("ours", h) for h in _paragraph_hunks(bp, op)] + [("theirs", h) for h in _paragraph_hunks(bp, tp)]
    spans, conflicts = [], []
    for c in _clusters(hunks):
        c0, c1 = min(h[0] for _, h in c), max(h[1] for _, h in c)
        sides = {s for s, _ in c}
        if len(sides) == 1:
            spans.append((c0, c1, _version(bp, c, sides.pop(), c0, c1)))
            continue
        o, t = _version(bp, c, "ours", c0, c1), _version(bp, c, "theirs", c0, c1)
        # where the cluster starts in the deck's paragraphs: the deck's hunks before it shift it
        k = c0 + sum(len(rep) - (h1 - h0) for side, (h0, h1, rep) in hunks
                     if side == "theirs" and h1 <= c0 and ("theirs", (h0, h1, rep)) not in c)
        if o == t:
            spans.append((c0, c1, t))
        elif len(o) == len(t) == c1 - c0:
            spans.append((c0, c1, [_paragraph(b, x, y, k + i, take, conflicts)
                                   for i, (b, x, y) in enumerate(zip(bp[c0:c1], o, t))]))
        elif k in take:
            spans.append((c0, c1, o))
        else:
            conflicts.append({"base": "\n".join(bp[c0:c1]), "ours": "\n".join(o), "theirs": "\n".join(t),
                              "paragraph": k})
            spans.append((c0, c1, t))
    out, pos = [], 0
    for c0, c1, items in sorted(spans, key=lambda s: (s[0], s[1])):
        out += bp[pos:c0] + items
        pos = max(pos, c1)
    return out + bp[pos:], conflicts


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
    spans = []
    conflicts = []
    for c in _clusters(hunks):
        c0 = min(h[0] for _, h in c)
        c1 = max(h[1] for _, h in c)
        sides = {s for s, _ in c}
        if len(sides) == 1:
            text = _version(b, c, sides.pop(), c0, c1)
        else:
            o, t = _version(b, c, "ours", c0, c1), _version(b, c, "theirs", c0, c1)
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


def keys_the_source_took(elements: list[dict]) -> list[dict]:
    """A base slide's elements, with a unit kept though the source dropped it moved off any key
    the source has since given to something else.

    A key belongs to the source, the way a label does (`sync.new_base` clears the label of a frame
    the source dropped, so that tomorrow's frame may carry it). A unit the source removed and the
    deck's edits keep alive stays in the base under the key it had - and the next conversion's
    `identity.slide_element_keys` hands that very key to whatever it finds in its place, an icon at
    the head of *another* line being image/icon/0 as readily as the one that went. The slide then
    answers to one key twice, and every `{e["key"]: e}` map over a slide's elements silently reads
    the second of them: `adopt_sync.problems` looked up the person's unpaired icon as a member of
    the source's new unit, found it tied to nothing and refused the **whole sync** (adopt-shaped
    seed 86066 at chain 8, 5 slides of 1,997 rebases); `identity.match_elements` cannot see the
    element the other one hides, and `fuzz_world` ties the wrong object to it.

    The kept one gives way. From here on it is bookkeeping for the deck's own version - the source
    will never name it again - while the new element's key is the one the next conversion has to
    inherit. Its own members follow it (an anchored picture names its anchor by key), and nothing
    the source still draws is renamed, so no pairing moves."""
    counts: dict[str, int] = {}
    for e in elements:
        counts[e["key"]] = counts.get(e["key"], 0) + 1
    if all(n == 1 for n in counts.values()):
        return elements
    taken = set(counts)
    out, moved = [], {}
    for e in elements:
        if counts[e["key"]] > 1 and e.get("removed"):
            key = next(f"{e['key']}~{k}" for k in range(2, 10 ** 6) if f"{e['key']}~{k}" not in taken)
            taken.add(key)
            moved[e["key"]] = None if e["key"] in moved else key   # two of them: no member can say which
            e = {**e, "key": key}
        out.append(e)
    for i, e in enumerate(out):
        anchor = moved.get(e.get("anchor"))
        if anchor and e.get("removed"):
            out[i] = {**e, "anchor": anchor,
                      "fingerprint": {**e.get("fingerprint", {}), "anchor": anchor}}
    return out


def covered(members: list[dict]) -> set[str]:
    """Of an adopted unit's members, those with no object of their own that another member of this
    same unit accounts for (`adopt_sync.drawn_from`).

    A member naming no object is what makes a unit unwritable: sync deletes a unit's old objects
    through the base, so one that names none leaves the person's own box standing under whatever
    is created for it. The icon at the head of a line and the picture of a formula in it name none
    - they were never objects - but the box they came out of is named, by the words beside them,
    and those words are in this very unit (`units`: an anchored picture belongs to its text). So
    that one delete takes the object away and the unit is written whole, with nothing left behind.

    Only inside the unit. The same picture drawn out of an object some *other* unit is tied to
    would be created while that object stayed where it is, which is the duplicate this is about."""
    tied = {m["key"] for m in members if m.get("objects")}
    return {m["key"] for m in members
            if not m.get("objects") and m.get("drawn_from") in tied}


def blind_members(members: list[dict]) -> list[str]:
    """The keys of an adopted unit's members that are tied to nothing and nothing accounts for."""
    ok = covered(members)
    return [m["key"] for m in members if not m.get("objects") and m["key"] not in ok]


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
    """Hole runs (no-break spaces sized to a formula) as one no-break space: their count follows the formula width.
    The zero-width space emit writes in front of a hole (emit.HOLE_BREAK) is no character of the
    text either: a deck converted before it and one after say the same."""
    return re.sub("\u00a0+", "\u00a0", text.replace("\u200b", ""))


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

    `sync.style_range_requests` maps each styled run of the live text through the matching blocks of
    that text and the text sync is about to write, and writes the run's style onto whatever the
    blocks carry across. A run they carry nowhere is styling that simply ends: nothing can be done
    about that - the characters it was on are gone - but the report has to say so instead of
    promising the styling was kept.

    So the question is that **alignment's**, and it used to be asked of an alignment of the *tokens*
    instead - whether the words the run sat on turn up in the new text as words. That said the bold
    was safe where the mechanism drops it: the deck's box said `... typed by a person.` twice, the
    source replaced the first sentence, and the tokens paired the *first* one's words with the tail
    of the new text while the characters pair the second, so the bold on a word of the replaced
    sentence had a token to land on and no request ever wrote it, with an `overrides` entry
    promising it was kept (offline seed 86044 --shape adopt --first-sync, chained 5 deep;
    `fuzz_world._styling_ends` is the reference applier's own reading of the same mechanism).

    What the alignment is asked is whether it carries a **word** of the run across whole, not
    whether it carries anything at all: between two sentences that share no words it still matches
    the odd letter, and a bold put back on the `i` and the `r` of another word is the styling gone
    as surely as nothing at all (live round 404, the test below). A run clipped to a few letters
    inside a word by the person's own earlier rewording is its own word here, so styling sync
    really does re-apply is not reported as lost (seed 23599, which is `_styling_ends`' lesson)."""
    spans, before = theirs_rb.get("run_spans"), theirs_rb.get("text") or ""
    if not spans or merged is None or merged == before:
        return False
    base_styles = base_rb.get("text_styles") or []
    blocks = SequenceMatcher(None, before, merged, autojunk=False).get_matching_blocks()
    for start, end, style in spans:
        end -= len(before[start:end]) - len(before[start:end].rstrip("\n"))  # (paragraph ends keep theirs)
        if end <= start or not before[start:end].strip():
            continue                                        # a run of nothing but the line's end
        if not deck_attributes(style, base_styles):
            continue                                        # the converter's own styling, not the person's
        words = [(start + m.start(), start + m.end()) for m in re.finditer(r"\S+", before[start:end])]
        if not any(any(i <= s and e <= i + n for i, _, n in blocks) for s, e in words):
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
              slide_read: dict | None, report: dict, scale: float | None = None, adopt=None,
              res=None, adopted: bool = False) -> dict:
    """The action for one element unit: keep, recreate (with deck overrides), create, delete,
    move, adopt (the deck already shows the source's change) or adopt_object (the deck's own
    object is what the source now draws); conflicts and overrides go to `report`. `scale`: deck pt
    per PDF pt. `adopt(skey, ours_members, slide_read, oid=None)`: the live object showing the same
    picture as the unit's new one - a user object, or `oid` itself (sync.picture_adopter).
    `res`: the conflicts a person asked to settle for the source (`Resolutions`).
    `adopted`: the deck is a person's own and the base is a pairing (`ADOPTED`), so an element
    tied to no object is one nothing may be written for."""
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
        # The source no longer draws this element. Deck edits on it make it the person's and it
        # stays, with a conflict - and the base has to *record* that (`removed`), the way a slide
        # the source dropped does (`keep_removed`), or the decision reverses itself the moment the
        # evidence for it goes. The evidence is the deck differing from the base, and the sync can
        # take that away with its own hands: deleting another element this source dropped left the
        # person's group around this one with a single child, which Slides then dissolves, so the
        # next sync found the deck exactly as the base had it and deleted the box this one had
        # promised to keep - silently, as `removed` is an applied change and not a conflict
        # (converted fuzz seed 7700464 at chain 10). Once kept, kept: only the person taking it
        # out of the deck ends it.
        kept_before = bool(base_members) and all(m.get("removed") for m in base_members)
        if deck and deck <= {"deleted", "part_deleted"}:
            return {"key": ukey, "action": "none", "gone": True}
        if not deck and not kept_before:
            report["applied"].append({**where, "fields": ["removed"]})
            return {"key": ukey, "action": "delete"}
        report["conflicts"].append(conflict_entry(res, skey, ukey, "removed", "element", None,
                                                  sorted(deck) or ["the deck's own"],
                                                  "kept (edited in the deck)" if deck else
                                                  "kept (the deck's own since the source dropped it)")[0])
        return {"key": ukey, "action": "keep", "deck": sorted(deck), "removed": True}
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
        said = deck - {"image"} if "image" in deck and unchecked(slide_read, edits["image"]) else deck
        if said - {"z"}:
            report["overrides"].append({**where, "fields": sorted(said)})
        return {**action, "action": "keep"}
    if "deleted" in deck:
        report["conflicts"].append({**where, "field": "deleted", "base": "element", "ours": sorted(src), "theirs": None,
                                    "resolution": "kept deleted"})
        return {**action, "action": "keep"}
    if "part_deleted" in deck:
        report["conflicts"].append({**where, "field": "part_deleted", "base": "element", "ours": sorted(src),
                                    "theirs": edits["part_deleted"], "resolution": "deck kept"})
        return {**action, "action": "keep"}
    blind = blind_members(base_members) if adopted else []
    if blind:
        # A person's deck, and this element is one the pairing could not tie to any object of it
        # (`adopt_sync.pair_elements`). Sync deletes a recreated unit's old objects through the
        # base and this one names none, so writing it would leave the person's own box standing
        # and put a second one saying the source's new words on top of it: no loss, a wrecked
        # slide (`fuzz_sync._doubled`). Nor does it heal - no sync ever gives such an element an
        # object - so the unit is kept as the deck has it and the person is told, once per sync
        # (`plan_merge`'s warning) and here by name. It used to refuse the whole sync, which is
        # the wrong size of answer: over 400 first-sync campaign rounds, 734 of ~2,400 syncs
        # wrote nothing at all because one element of one slide could not be paired. One element
        # nothing can be written to freezes that element, not the talk - `hold_slide`'s rule one
        # dimension down. `adopt_sync.problems` still stands between a plan and a write.
        # Two kinds of nothing to write to, and they are not the same thing to the person reading
        # this. An element the deck's *layout* draws (`adopt_sync.explained_by_layout`) has an
        # object; it stands one level up, under every other slide that inherits it, and the way to
        # change it is Slide > Edit theme - not a pairing that failed, and not a thing to go
        # looking for on the slide.
        # And a third kind, for the same reason: an element standing inside one of the deck's own
        # tables (`adopt_sync.inside_tables`). The converter reads a table somebody drew back as
        # loose words, so the cell is there in front of the person and the pairing has nothing
        # shaped like it - "nothing stands where it does" is true of the element and the wrong
        # thing to hear about a table you are looking at.
        # What is *not* here any more is the fourth kind, because it is no longer a kind of
        # nothing: an element drawn out of an object this unit is already tied to (`covered`).
        # The icon at the head of a person's line has no object and never had one, and freezing
        # their whole box over it meant the source could never say another word in that box.
        loose = [m for m in base_members if m["key"] in set(blind)]
        drawn = all(m.get("from_layout") for m in loose)
        celled = not drawn and all(m.get("in_table") for m in loose)
        field, why = ("inherited", "kept (the deck's layout draws this, not the slide)") if drawn else \
            ("in_table", "kept (a cell of a table of the deck's)") if celled else \
            ("unpaired", "kept (tied to no object of the deck)")
        report["conflicts"].append(conflict_entry(res, skey, ukey, field, "element", sorted(src),
                                                  sorted(deck) or ["the deck's own"], why)[0])
        return {**action, "action": "keep",
                **({"inherited": blind} if drawn else {"in_table": blind} if celled else {"unpaired": blind})}
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

    def conflict(field, base_v, ours_v, theirs_v, resolution="deck kept", takeable=False) -> bool:
        entry, was_taken = conflict_entry(res, skey, ukey, field, base_v, ours_v, theirs_v, resolution, takeable)
        conflicts.append(entry)
        return was_taken

    def taken(field, base_v, ours_v, theirs_v, resolution="deck kept") -> bool:
        """Report the conflict, and say whether the person asked for the source's version of this
        field. If they did, the deck's edit to it is not an override any more - nothing of it is
        re-applied, and the report must not promise it was kept."""
        if not conflict(field, base_v, ours_v, theirs_v, resolution, takeable=True):
            return False
        edited.discard(field)
        return True

    def on_deck(box):
        """A converter box in deck pt, as the deck's own box beside it is (edit hunt h2-5: three boxes
        in two units read as a wrong size)."""
        return [round(v * scale, 2) for v in box] if scale else box

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
        keep = not taken("image", "picture", sorted(src), "replaced in the deck")
    if "text" in edited and not keep:
        if anchor["kind"] == "table" and set(edits["text"]) == {main}:
            cells = table_merge(base_rb.get("text"), identity.plain_text(first["ir"]), theirs_rb.get("text"),
                                base_rb.get("table"), theirs_rb.get("table"))
            if cells is None:
                keep = not taken("text", anchor["fingerprint"]["text"], first["fingerprint"]["text"],
                                 theirs_rb.get("text"))
            else:
                overrides["text"] = {"table": True, "base": base_rb.get("text") or "", "theirs": theirs_rb.get("text") or "",
                                     "dims": base_rb.get("table")}
                if cells[1]:
                    report["converged"].append({**where, "field": "text", "value": theirs_rb.get("text")})
        elif anchor["kind"] != "text" or set(edits["text"]) != {main}:
            keep = not taken("text", anchor["fingerprint"]["text"], first["fingerprint"]["text"],
                             theirs_rb.get("text"))
        else:
            b, o, t = (collapse_holes(x) for x in (base_rb.get("text") or "", predicted_text(first["ir"]), theirs_rb.get("text") or ""))
            merged, clashes, safe = text_merge(b, o, t)
            if not safe or (clashes and merged == t):
                # Nothing of the source's survived the merge (or it cannot be written safely): the
                # deck's text stands as it is, and there is nothing to write.
                keep = not taken("text", b, o, t)
            else:
                # Paragraphs both sides rewrote are conflicts of their own: the deck keeps them,
                # and the rest of the box still takes what the source now says. A person who read
                # the report can name one of them and have the source's paragraph written instead;
                # the others are untouched, which is the point of merging by paragraph at all.
                take = [c["paragraph"] for c in clashes
                        if conflict("text", c["base"], c["ours"], c["theirs"], takeable=True)]
                if take:
                    merged, clashes, safe = text_merge(b, o, t, take)
                overrides["text"] = {"base": base_rb.get("text") or "", "theirs": theirs_rb.get("text") or "",
                                     **({"take": take} if take else {})}
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
            keep = not taken("text_style", "style", sorted(src), "restyled in the deck")
        elif runs is None or anchor["kind"] == "table":
            # Some words restyled (or a table): the deck's run styles go onto the same words of the
            # new text (sync.style_range_requests).
            overrides["text_style"] = {"runs": {}, "paragraphs": paras, "ranges": True}
            if styling_lost(base_rb, theirs_rb, written):
                # ... and some of those words are not in the new text at all. The styling on them
                # ends here, and saying nothing would make the report claim it was kept.
                conflict("text_style", "style", "the words it was on were replaced", "restyled in the deck",
                         "the styling of the replaced words is gone")
            if clash and taken("text_style", "style", "restyled in the source", "restyled in the deck",
                               "deck style re-applied"):
                overrides.pop("text_style", None)
        else:
            overrides["text_style"] = {"runs": runs, "paragraphs": paras}
            if clash and taken("text_style", "style", "restyled in the source", "restyled in the deck",
                               "deck style re-applied"):
                overrides.pop("text_style", None)
    if "shape_style" in edited and not keep:
        if anchor["kind"] != "shape" or set(edits["shape_style"]) != {main}:
            keep = not taken("shape_style", "style", sorted(src), "restyled in the deck")
        else:
            overrides["shape_style"] = theirs_rb.get("shape_style")
            if "style" in src and taken("shape_style", "style", "restyled in the source",
                                        "restyled in the deck", "deck style re-applied"):
                overrides.pop("shape_style", None)
    if "geometry" in edited and not keep and not geometry_writable(base_members, slide_read):
        # The person moved something inside the unit; a rewritten unit can't be put back that way.
        keep = not taken("geometry", on_deck(anchor["fingerprint"]["bbox"]), on_deck(first["fingerprint"]["bbox"]),
                         theirs_rb.get("box"), "deck kept (the deck moved a part of the element on its own)")
    if "geometry" in edited and not keep:
        # The person's move and resize (base -> theirs) go on top of wherever the source now puts the
        # unit (`sync.carried`), whether or not the source moved it too. When it did, it is still a
        # conflict - both sides moved one thing - but the deck's absolute place is not what wins: that
        # dropped the person's size (the recreated box came back at the converter's height, the words
        # ran out of it) and ignored the source's reflow around it (a box that grew by a line ran into
        # the one the person had moved, an equation came down onto the paragraph the person had moved;
        # docs/project-notes.md "Both-moved geometry").
        overrides["geometry"] = {"mode": "delta"}
        if "position" in src and taken("geometry", on_deck(anchor["fingerprint"]["bbox"]),
                                       on_deck(first["fingerprint"]["bbox"]), theirs_rb.get("box"), GEOMETRY_CARRIED):
            overrides.pop("geometry", None)
    moving = keep and "position" in src and "geometry" not in edited and shift
    if keep and conflicts:
        # A unit kept whole drops every change the source made to it, not only the one the conflict
        # is about: a moved-apart group member kept the list, and the source's new fourth item with
        # it, while the report named only "geometry" (edit hunt h3-2).
        named = set().union(*(CONFLICT_COVERS.get(c["field"], {c["field"]}) for c in conflicts))
        lost = sorted(set(src) - named - ({"position"} if moving else set()))
        if lost:
            c = next((c for c in reversed(conflicts) if c["resolution"] != TAKEN_SAYS), conflicts[-1])
            c["resolution"] += f"; the source's change to its {', '.join(lost)} was not written either"
    report["conflicts"] += conflicts
    # `edited` can be empty by now: `taken` takes a field out of it when the person asked for the
    # source's version of it, and an entry here says "the deck's version of these fields was kept",
    # which with no fields left would be a promise about nothing.
    if keep:
        if edited:
            report["overrides"].append({**where, "fields": sorted(edited)})
        if moving:
            # The deck's version of what the unit says stands, but where it stands is the source's
            # alone to change: the source moved the table the person had added a row to, and the
            # unit kept in place left the source's new caption over it (live fuzz r8006,
            # table-moved). The deck's objects move as they are, like a unit only the source moved.
            report["applied"].append({**where, "fields": ["position"], "how": "deck object moved"})
            return {**action, "action": "move", "delta": list(shift)}
        return {**action, "action": "keep"}
    report["applied"].append({**where, "fields": sorted(src)})
    if edited:
        report["overrides"].append({**where, "fields": sorted(edited)})
    return {**action, "action": "recreate", "overrides": overrides}


# ---------------------------------------------------------------- slides

def empty_report() -> dict:
    return {"applied": [], "overrides": [], "conflicts": [], "resolved": [], "converged": [], "user_objects": [],
            "slides": {"created": [], "deleted": [], "moved": [], "kept": [], "held": [], "user_added": []},
            "warnings": []}


def report_label_moves(moves: list[dict], report: dict, held: bool = True) -> None:
    r"""Conflicts for the labels `identity.label_moves` found somewhere else than it left them.

    There is no resolution to offer here, which is the point: a three-way merge writes what it can
    and hands back what it cannot, and which frame is which is exactly what it cannot. What sync
    did with the slide is said plainly - either the content decided, or the slide was left alone -
    so whoever reads this knows what they are looking at before they go and fix the
    `.tex` (docs/ai-authoring.md).

    `held` is whether an `unsure` verdict stops this sync writing to that slide (`plan_merge`,
    `--follow-labels` turns it off).
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
                           "either the label moved or that passage did: nothing written to this slide" if held else
                           "either the label moved or that passage did: followed the label, nothing re-paired"),
        })
        # An `unsure` verdict re-pairs nothing, so saying only "check the .tex" leaves the reader to
        # work out what happens if they do not. What happens is that the frame now carrying the
        # label would be written onto the slide the label names - the slide with somebody's edits on
        # it. That slide is named, and by default it is held back rather than written.
        slide = m.get("slide") or m["label"]
        at_risk = "" if moved else (
            f" Nothing was written to the slide `{slide}`: the frame carrying `{m['label']}` would have gone "
            f"onto it, edits and all, and which frame that slide belongs to is the question. The rest of the "
            f"deck was synced. `--follow-labels` writes it anyway."
            if held else
            f" This sync writes the frame carrying `{m['label']}` onto the slide `{slide}`, edits and all, "
            f"so that is the slide to look at.")
        report["warnings"].append(
            f"label `{m['label']}` is not on the frame this deck's slide was made from" + (
                ". Deck edits belong to the words a person edited, so sync went by the content and not by the "
                "label. Put the label back on its own frame" if moved else
                ", or a passage moved between two frames - from the PDF alone the two look the same. Check the "
                "`.tex`: if the label moved, put it back; if the passage did, the labels are right") +
            ": docs/labels.md, \"If a label does change\"." + at_risk)


def plan_merge(base: dict, ours: dict, theirs: dict, adopt=None, follow_labels: bool = False,
               take_source=()) -> dict:
    """ours: {"slides": [slide entries with inherited keys], "pairs": {ours index: base index}}.
    `adopt`: see plan_unit (a picture the deck already shows).
    `follow_labels`: write to a slide whose label `identity.label_moves` is unsure about anyway.
    `take_source`: conflict ids from an earlier report to settle for the source (`Resolutions`).
    Returns {"slides": [per slide plan], "order": [live slide ids or "new:<key>"], "report"}."""
    report = empty_report()
    res = Resolutions(take_source)
    live = {s["objectId"]: s for s in theirs["slides"]}
    base_slides = base["slides"]
    pairs = {int(k): v for k, v in ours["pairs"].items()}
    matched_base = set(pairs.values())
    plans = []
    moves = ours.get("label_moves") or []
    report_label_moves(moves, report, held=not follow_labels)
    # An `unsure` label move is the one place where this merge does not know which frame a slide
    # belongs to, and every other rule here assumes it does: the words are merged, the deck's edits
    # kept, the source's changes written - onto whichever slide the pairing names. Get that wrong
    # and nothing is deleted and nothing is lost, but one frame's sentences land beside somebody's
    # edits about another frame, and the way back is by hand. So the slide waits.
    held = set() if follow_labels else {m["ours"] for m in moves if m["verdict"] == "unsure"}

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
        if b is not None and weak.get(j) == "crossed":
            # `identity.crossed_twins`: this frame's label and another's changed places over two
            # slides so alike that reading the two frames the other way round loses nothing, so
            # nothing in the words can say whether the author moved the frames or moved a label.
            # The label was followed; it is the promise, and nobody's edits move either way.
            report["warnings"].append(
                f"slide {b['key']}: this frame's label `{o.get('label')}` and another's have changed places "
                f"over two slides so alike that reading the two frames the other way round says the deck just "
                f"as well, so nothing they say can tell whether you moved the frames or moved a label. The "
                f"labels were followed. If `{o.get('label')}` belongs on the other frame, put it back before "
                f"the next sync - see docs/labels.md.")
        if b is not None and weak.get(j) == "traded":
            # `identity.crossed_twins`: a label and the frame beside it changed places over two
            # slides so alike that reading the two frames the other way round loses nothing, and
            # that frame carries no label of its own - so nothing can say whether the author moved
            # the frames or pasted the label onto the twin. The label was followed; nobody's edits
            # move either way.
            report["warnings"].append(
                f"slide {b['key']}: this slide and another are so alike that reading their two frames the "
                f"other way round says the deck just as well, and the frames that carry them have changed "
                f"places - so nothing they say can tell whether you moved the frames or moved a label. The "
                f"label was followed. Give the other frame a label too "
                f"(`beamer2slides label`), and the next sync has an answer - see docs/labels.md.")
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
                report["conflicts"].append(conflict_entry(res, o["key"], None, "slide", "slide", "changed",
                                                          "deleted", "kept deleted")[0])
            plans.append({"key": o["key"], "action": "gone", "ours": j, "base": i, "objectId": None})
            continue
        if j in held:
            plans.append(hold_slide(b, o, read, report, j, i))
            continue
        plans.append(plan_slide(b, o, read, report, base, j, i, adopt, res))

    for i, b in enumerate(base_slides):
        if i in matched_base:
            continue
        read = live.get(b.get("objectId"))
        if read is None:
            continue  # removed on both sides
        touched = slide_touched(b, read)
        if not touched and base.get("origin") == ADOPTED:
            # A slide of a deck a person built. Nothing here made it, and a frame that has gone out
            # of the source is as likely to be a label that did not survive the round trip as a
            # slide the author meant to drop (`adopt.frame_labels` writes one per slide from its
            # objectId, and a person editing the .tex can move or lose one). Deleting it would take
            # somebody's own slide - its pictures, the comments hanging on it - on that evidence, so
            # it is kept and said out loud instead, at every generation: a decision the base does not
            # record reverses itself, and the base records this one by not accounting for the slide.
            # The way to really drop it is to delete it in Slides, which is one click and no guess.
            # It used to refuse the whole first sync (`adopt_sync.problems`, "slides-deleted"): 109
            # of 600 campaign rounds at chain 8 wrote nothing at all on that account.
            touched = ["the deck's own"]
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
    unaccounted = [k["slide"] for k in report["slides"]["kept"] if k["reason"] == ["the deck's own"]]
    if unaccounted:
        # The leftover-base loop: slides of a person's own deck that no frame of the source explains.
        report["warnings"].append(
            f"{len(unaccounted)} slide(s) of this deck are accounted for by no frame of the source, and were "
            f"kept: {', '.join(unaccounted[:3])}{', ...' if len(unaccounted) > 3 else ''}. Nothing here made "
            f"those slides, so a frame gone out of the source is as likely to be a label that moved as a "
            f"slide you meant to drop - put the label back where `adopt` wrote it (docs/labels.md) and the "
            f"frame finds its slide again; if you did mean to drop it, delete the slide in Slides.")
    unpaired = [f"`{p['key']}` / `{u['key']}`" for p in plans for u in p.get("units") or [] if u.get("unpaired")]
    if unpaired:
        # `plan_unit`: the source changed elements this deck's pairing cannot place. Each one is a
        # conflict of its own; this says the thing a person has to *do* about them, once.
        report["warnings"].append(
            f"{len(unpaired)} element(s) the source changed could not be tied to any object of this deck, so "
            f"they were left exactly as the deck has them: {', '.join(unpaired[:3])}"
            f"{', ...' if len(unpaired) > 3 else ''}. `adopt` paired the source it wrote with the deck's own "
            f"objects by where they stand and what they say, and these it could not place (the base lists "
            f"every one of them under `adopt.unpaired`, with the reason); writing one would put a second "
            f"object beside yours rather than over it. Change them in the deck itself - the rest of this "
            f"sync went in as usual.")
    inherited = [f"`{p['key']}` / `{u['key']}`" for p in plans for u in p.get("units") or [] if u.get("inherited")]
    if inherited:
        # The other half of it: these the source *can* draw and this deck does draw - on its
        # layouts, which are the person's own look and which nothing here ever writes to
        # (`adopt_sync.layout_elements`, `build_base`'s `master_background = None`).
        report["warnings"].append(
            f"{len(inherited)} element(s) the source changed are drawn by this deck's layouts or its master, "
            f"not by the slide: {', '.join(inherited[:3])}{', ...' if len(inherited) > 3 else ''}. `adopt` "
            f"recovered those as the theme of the source it wrote, so the source draws them on every slide "
            f"that inherits them - but they belong to the template, and writing one would put a copy on this "
            f"one slide over a thing every other slide still shows. Change them in Slides under "
            f"Slide > Edit theme - the rest of this sync went in as usual.")
    celled = [f"`{p['key']}` / `{u['key']}`" for p in plans for u in p.get("units") or [] if u.get("in_table")]
    if celled:
        # And the third: a cell of a table of the deck's. The person can see the table, so the
        # thing to say is which part of it this is and that the cell is theirs to change
        # (`adopt_sync.inside_tables` for why nothing here can write into one).
        report["warnings"].append(
            f"{len(celled)} element(s) the source changed stand inside a table of this deck: "
            f"{', '.join(celled[:3])}{', ...' if len(celled) > 3 else ''}. This converter reads a table you "
            f"drew back as the loose words of its cells, not as a table, so there is no cell here to write "
            f"into and nothing was written - putting the words back into one has to be right cell by cell or "
            f"it writes one cell's words into another's. Edit those cells in Slides - the rest of this sync "
            f"went in as usual.")
    report_resolutions(res, report)
    order, moved = plan_order(base, ours, theirs, plans, report)
    report["slides"]["moved"] = moved
    return {"slides": plans, "order": order, "report": report}


def report_resolutions(res: Resolutions, report: dict) -> None:
    """What `--take-source` settled, and what it did not reach.

    `resolved` keeps the deck's own version of each spot that was written over, verbatim: that is
    the way back, and it has to be in the report because nowhere else will have it a minute from
    now. An id that matched nothing is the ordinary case of a report read a version too late -
    nothing was written and nothing is lost, and the warning is there so that a person who meant
    to settle something does not read "no conflicts" and believe it happened."""
    for c in report["conflicts"]:
        if c.get("resolution") == TAKEN_SAYS:
            report["resolved"].append({"id": c["id"], "slide": c["slide"], "element": c["element"],
                                       "field": c["field"], "was": c["theirs"]})
    for cid in res.unused:
        report["warnings"].append(
            f"--take-source {cid}: no conflict in this sync has that id, so nothing was settled for the "
            f"source. An id names one disagreement and stops matching as soon as either side of it moves, "
            f"so a report a version old cannot reach today's conflict - read the report this run wrote and "
            f"take the id from there. Nothing was written on that account and nothing is lost.")


def unchecked(read: dict | None, oids: list[str] | None = None) -> bool:
    """Whether these live pictures (`oids`; None: the slide's background) are ones sync did not
    read (`sync.Sync.sign_changed`): a new URL, and a plan that is the same whether or not the
    person replaced the picture. It still counts as replaced - nothing here ever writes over it -
    but a report must not say the person did something nobody looked at."""
    if read is None:
        return False
    if oids is None:
        return bool((read.get("background") or {}).get("unchecked"))
    objects = read.get("objects", {})
    return bool(oids) and all((objects.get(oid, {}).get("image") or {}).get("unchecked") for oid in oids)


def background_edited(b: dict, read: dict) -> bool:
    """The deck changed a base slide's background (a picture compares by its pixels: contentUrls change)."""
    return b.get("background_readback") is not None and not snapshot.same_background(b["background_readback"], read.get("background"))


def slide_touched(b: dict, read: dict) -> list[str]:
    """Why a slide counts as edited in the deck (empty: untouched)."""
    why = []
    if any(deck_edits(e, read) for e in b["elements"]):
        why.append("elements edited")
    # An adopted deck's slide is full of objects this converter did not make and nobody added:
    # what the pairing could not tie to any element of the source (`adopt.left_alone`). Counting
    # those as "objects added" says the person edited a slide they have not touched since adopt
    # read it - and, since a foreign deck has them nearly everywhere, it says that about the whole
    # deck and hides the sentence below it ("the deck's own").
    left = set(b.get("left_alone") or ())
    if any(o["objectId"] not in left for o in user_objects(b, read)):
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


def hold_slide(b: dict, o: dict, read: dict, report: dict, j: int, i: int) -> dict:
    r"""A slide this sync writes nothing to, because which frame it is is in doubt.

    `identity.label_moves` says `unsure` when something in the deck looks exactly like what this
    label used to name: either the label moved onto another frame, or the author moved that passage
    between two frames, and from the PDF alone the two are the same picture. The label was followed
    anyway until now - the safest thing a *pairing* can do, since re-pairing on a guess is how edits
    land on the wrong slide - but following it is a write, and a write onto the wrong slide is the
    same mistake one step later: the frame's new sentences merged into the person's edits about a
    different frame, with no deletion, no loss and no way back but by hand.

    Nothing is lost by waiting. The source still says what it says, the deck still says what it
    says, the base is left as it was (`sync.Sync.new_base`), so the next sync plans this slide from
    scratch - and if the label was put back, it plans it right. The rest of the deck is synced as
    usual: one ambiguous label freezes one slide, not the talk. A person who has looked and knows
    the labels are right says so with `--follow-labels`.
    """
    report["slides"]["held"].append({"slide": o["key"], "reason": "label", "label": o.get("label")})
    for u in user_objects(b, read):
        report["user_objects"].append({"slide": o["key"], **u})
    return {"key": o["key"], "action": "update", "held": "label", "ours": j, "base": i,
            "objectId": b["objectId"], "units": []}


def plan_slide(b: dict, o: dict, read: dict, report: dict, base: dict, j: int, i: int, adopt=None,
               res=None) -> dict:
    skey = o["key"]
    bu, ou = units(b["elements"]), units(o["elements"])
    unit_plans = []
    for ukey in list(ou) + [k for k in bu if k not in ou]:
        unit_plans.append({**plan_unit(skey, ukey, bu.get(ukey), ou.get(ukey), read, report, deck_scale(base), adopt,
                                       res, base.get("origin") == ADOPTED),
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
            report["conflicts"].append(conflict_entry(res, skey, u["key"], "removed", words, None, words,
                                                      "kept: its text moved into an element in conflict")[0])
    adopted = {u["objectId"] for u in unit_plans if u["action"] == "adopt_object"}
    for u in user_objects(b, read):
        if u["objectId"] not in adopted:  # (an adopted object is the source's element now)
            report["user_objects"].append({"slide": skey, **u})
    plan = {"key": skey, "action": "update", "ours": j, "base": i, "objectId": b["objectId"], "units": unit_plans}
    # Background: the source's picture or colour unless the deck changed it.
    if b.get("background") != o.get("background"):
        write = True
        if background_edited(b, read):
            entry, write = conflict_entry(res, skey, None, "background", b.get("background"),
                                          o.get("background"), read.get("background"), takeable=True)
            report["conflicts"].append(entry)
        if write:
            plan["background"] = o["background"]
            report["applied"].append({"slide": skey, "element": None, "fields": ["background"]})
    elif background_edited(b, read) and not unchecked(read):
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
                entry, take_ours = conflict_entry(res, skey, None, "notes", bn, on, tn, takeable=True)
                report["conflicts"].append(entry)
                if take_ours:
                    plan["notes"] = on
                    report["applied"].append({"slide": skey, "element": None, "fields": ["notes"]})
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
    # slides the deck moved: out of their base order there, so back beside what they follow now.
    # A slide held back (`hold_slide`) is placed the same way, and for the same reason one step
    # further on: its place in the source's order is its place *as that frame*, and which frame it
    # is is the open question. Nothing about it changes until somebody answers that.
    deck_placed = _out_of_place(was, existing)
    for sid in deck_placed + [p["objectId"] for p in plans if p.get("held") and p["objectId"] not in deck_placed]:
        if sid not in order:
            continue
        order.remove(sid)
        k = live_order.index(sid)
        prev = next((live_order[q] for q in range(k - 1, -1, -1) if live_order[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
        if report is not None and sid in source_moved and sid in deck_placed:
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
