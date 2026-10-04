"""Sync's three-way merge, pure (docs/sync.md): deck edits from read-backs, word-level diff3,
the merge rules per element unit and field, and slide add/delete/reorder planning.

Inputs are the base (snapshot.build_base), ours (the new conversion's slide entries with keys
inherited: snapshot.slide_entries) and theirs (snapshot.read_presentation of the live deck).

The planner reads records (`sync_model`): `plan_merge` parses its three dicts where they enter and
`plan_merge_of` plans over them. What it decides is records too - a `Conflict`, a unit's
`UnitDecision`, a slide's `SlidePlan`, the `Report` - and `plan_merge` hands back their JSON, key
for key what sync, the oracles and the tests read. The small readers the rest of sync calls with
dicts (`deck_edits`, `units`, `unit_top`, ...) keep taking dicts, and read no more of them than
they did: guard and the oracles hand in the parts they have."""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Literal, TypedDict, TypeVar

from . import identity, snapshot
from .emit_widths import ADDED_SPACE, HOLE_BREAKS, WORD_JOINER
from .google_types import SlidesRange, SlidesRequest, SlidesTableCellLocation
from .ir_types import Box
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_optional_str, as_str
from .sync_model import (Base, DeckRead, ElementEntry, ElementKey, ImageRead, JsonMap, ObjectId, ReadBack, SlideEntry,
                         SlideKey, SlideRead, base as parse_base, deck_read, element_entry_json, readback, readbacks,
                         slide_entry)
from .typing_compat import assert_never

# What `plan_merge` decides for a slide (`SlidePlan`) and for a unit on an updated one
# (`UnitDecision`), as the words of their JSON's `action` - for the readers of that JSON that are
# typed (`sync.contained_report`).
SlideAction = Literal["create", "update", "gone", "keep_removed", "delete"]
UnitAction = Literal["adopt_object", "create", "none", "delete", "keep", "recreate", "adopt", "move"]

GEOMETRY_TOLERANCE = 0.05  # pt
SCALE_TOLERANCE = 1e-3
CONVERGED_PLACE = 2.0  # pt: a deck move the source now reproduces this closely counts as converged
MOVE_TOLERANCE = 0.05  # pt: members within this of the same step moved together (PDF pt)
Edit = Literal["geometry", "text", "text_style", "shape_style", "image", "group", "deleted", "part_deleted"]
"""What the deck did to one of a base element's objects (`deck_edits`)."""
EDIT_FIELDS: tuple[Edit, ...] = ("geometry", "text", "text_style", "shape_style", "image")
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
CONFLICT_COVERS: dict[str, set[str]] = {"text": {"text"}, "text_style": {"style"}, "shape_style": {"style"},
                                        "geometry": {"position", "size"}, "image": {"image"}}

ConflictField = Literal["removed", "deleted", "part_deleted", "inherited", "in_table", "unpaired", "image", "text",
                        "text_style", "shape_style", "geometry", "slide", "background", "notes", "label"]
"""What a conflict of the report is about: an element's field, its existence, or a slide's."""
TakeField = Literal["image", "text", "text_style", "shape_style", "geometry"]
"""A field of an element both an edit and a conflict can name (`plan_unit`'s `taken`)."""
Blind = Literal["inherited", "in_table", "unpaired"]
"""Why an adopted unit has members nothing can be written to (`plan_unit`)."""

K = TypeVar("K", bound=str)
E = TypeVar("E")
M = TypeVar("M", bound=JsonMap)


def _strs(xs: Iterable[str]) -> list[Json]:
    return [x for x in xs]


def _nums(xs: Iterable[float]) -> list[Json]:
    return [x for x in xs]


def _str_items(v: Json, where: str) -> list[str]:
    return [as_str(x, f"{where}[{n}]") for n, x in enumerate(as_array(v, where))]


def _number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected")


# ---------------------------------------------------------------- conflicts a person can settle

def conflict_id(slide: str, element: str | None, field: str, base: Json, ours: Json, theirs: Json) -> str:
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

    def __init__(self, take_source: Iterable[str]) -> None:
        self.wanted = {x.strip().lower() for x in take_source if x.strip()}
        self.used: set[str] = set()

    def take(self, cid: str) -> bool:
        if cid not in self.wanted:
            return False
        self.used.add(cid)
        return True

    @property
    def unused(self) -> list[str]:
        return sorted(self.wanted - self.used)


@dataclass(frozen=True, kw_only=True)
class Conflict:
    """One conflict as the report holds it. `takeable`: the source's version can be written here
    (`--take-source`); the id is on every conflict either way, since it is also how one talks
    about one that cannot."""
    slide: str
    element: str | None
    id: str
    field: ConflictField
    base: Json
    ours: Json
    theirs: Json
    resolution: str
    takeable: bool


def conflict_json(c: Conflict) -> JsonObject:
    out: JsonObject = {"slide": c.slide, "element": c.element, "id": c.id, "field": c.field, "base": c.base,
                       "ours": c.ours, "theirs": c.theirs, "resolution": c.resolution}
    if c.takeable:
        out["takeable"] = True
    return out


def conflict_of(res: Resolutions | None, slide: str, element: str | None, field: ConflictField, base: Json,
                ours: Json, theirs: Json, resolution: str, takeable: bool) -> tuple[Conflict, bool]:
    """One conflict, and whether the person asked to settle it for the source. `takeable` is the
    caller saying the source's version *can* be written here."""
    cid = conflict_id(slide, element, field, base, ours, theirs)
    taken = takeable and res is not None and res.take(cid)
    return Conflict(slide=slide, element=element, id=cid, field=field, base=base, ours=ours, theirs=theirs,
                    resolution=TAKEN_SAYS if taken else resolution, takeable=takeable), taken


def conflict_entry(res: Resolutions | None, slide: str, element: str | None, field: str, base: Json, ours: Json,
                   theirs: Json, resolution: str, takeable: bool) -> tuple[JsonObject, bool]:
    """`conflict_of` as JSON, for a field outside `ConflictField` (`theme_sync`'s: the master's,
    a layout's, the header and footer)."""
    cid = conflict_id(slide, element, field, base, ours, theirs)
    taken = takeable and res is not None and res.take(cid)
    entry: JsonObject = {"slide": slide, "element": element, "id": cid, "field": field, "base": base,
                         "ours": ours, "theirs": theirs, "resolution": TAKEN_SAYS if taken else resolution}
    if takeable:
        entry["takeable"] = True
    return entry, taken


# ---------------------------------------------------------------- text

Side = Literal["ours", "theirs"]
Hunk = tuple[int, int, list[str]]
"""Items [start, end) of the base, and what a side put there."""
SidedHunk = tuple[Side, Hunk]


@dataclass(frozen=True, kw_only=True)
class Clash:
    """Where both sides changed the same words or paragraphs: the three versions of it, and for a
    paragraph-level one its index in the deck's text (`text_merge`); None for `diff3`'s."""
    base: str
    ours: str
    theirs: str
    paragraph: int | None


def clash_json(c: Clash) -> JsonObject:
    out: JsonObject = {"base": c.base, "ours": c.ours, "theirs": c.theirs}
    if c.paragraph is not None:
        out["paragraph"] = c.paragraph
    return out


def tokens(text: str) -> list[str]:
    return [m.group(0) for m in TOKEN.finditer(text)]


def _hunks(base: Sequence[str], other: Sequence[str]) -> list[Hunk]:
    sm = SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, list(other[j1:j2])) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def text_merge(base: str, ours: str, theirs: str, take: Collection[int]) -> tuple[str, list[JsonObject], bool]:
    """`text_merge_of`, its clashes as JSON ({"base", "ours", "theirs", "paragraph"?})."""
    merged, clashes, safe = text_merge_of(base, ours, theirs, take)
    return merged, [clash_json(c) for c in clashes], safe


def text_merge_of(base: str, ours: str, theirs: str, take: Collection[int]) -> tuple[str, list[Clash], bool]:
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
        merged, clashes = diff3_of(base, ours, theirs)
        return merged, clashes, not clashes
    if not len(bp) == len(op) == len(tp):
        joined, conflicts = paragraph_merge(bp, op, tp, take)
        return "\n".join(joined), conflicts, True
    out: list[str] = []
    found: list[Clash] = []
    for k, (b, o, t) in enumerate(zip(bp, op, tp)):
        out.append(_paragraph(b, o, t, k, take, found))
    return "\n".join(out), found, True


def _paragraph(b: str, o: str, t: str, k: int, take: Collection[int], conflicts: list[Clash]) -> str:
    """One paragraph of three versions, merged (`k`: its index in the deck's text, the handle a
    `take` names and a conflict carries)."""
    if b == o or o == t:      # the source left it alone, or both arrived at the same words
        return t
    if b == t:                # only the source changed it
        return o
    merged, clashes = diff3_of(b, o, t)
    if not clashes:
        return merged
    if k in take:
        return o
    conflicts.append(Clash(base=b, ours=o, theirs=t, paragraph=k))
    return t


def _clusters(hunks: list[SidedHunk]) -> list[list[SidedHunk]]:
    """Hunks of both sides grouped where they touch (`_clash`), joined until stable."""
    clusters: list[list[SidedHunk]] = []
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


def _version(base: Sequence[str], c: Sequence[SidedHunk], side: Side, c0: int, c1: int) -> list[str]:
    """What one side made of the base's items c0..c1 (a cluster)."""
    out: list[str] = []
    pos = c0
    for s, (h0, h1, rep) in sorted(c, key=lambda x: (x[1][0], x[1][1])):
        if s != side:
            continue
        out += list(base[pos:h0]) + rep
        pos = h1
    return out + list(base[pos:c1])


SAME_PARAGRAPH = 0.5   # word similarity above which a rewritten paragraph is the old one reworded


def _paragraph_hunks(base: list[str], other: list[str]) -> list[Hunk]:
    """`_hunks` over paragraphs, with a replacement of n by m paragraphs taken apart: a diff reads
    "rewrote the last bullet, then added one" as one bullet replaced by two, and then a rewording on
    the other side clashes with the addition too. Each new paragraph is paired with the old one it
    rewords most (word similarity SAME_PARAGRAPH or more, in order: the best monotone pairing), and
    the rest are paragraphs added or removed on their own."""
    out: list[Hunk] = []
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


def paragraph_merge(bp: list[str], op: list[str], tp: list[str], take: Collection[int]) -> tuple[list[str], list[Clash]]:
    """Three versions of a box's paragraphs lined up as a line-based diff3 does, a paragraph being
    the unit: what one side alone added, removed or rewrote is taken; where both rewrote the same
    paragraphs one for one, each merges as `text_merge` merges one (`_paragraph`); any other
    overlap (both inserted at one place, one rewrote what the other removed) keeps the deck's
    paragraphs there, whole, as one conflict. A conflict's `paragraph` is where its paragraphs start
    in the deck's text, which neither side's choices move: the planner and the sync, merging the
    predicted and the live text, name it alike."""
    ours_side: Side = "ours"
    theirs_side: Side = "theirs"
    hunks = [(ours_side, h) for h in _paragraph_hunks(bp, op)] + [(theirs_side, h) for h in _paragraph_hunks(bp, tp)]
    spans: list[tuple[int, int, list[str]]] = []
    conflicts: list[Clash] = []
    for c in _clusters(hunks):
        c0, c1 = min(h[0] for _, h in c), max(h[1] for _, h in c)
        sides = {s for s, _ in c}
        if len(sides) == 1:
            spans.append((c0, c1, _version(bp, c, sides.pop(), c0, c1)))
            continue
        o, t = _version(bp, c, "ours", c0, c1), _version(bp, c, "theirs", c0, c1)
        # where the cluster starts in the deck's paragraphs: the deck's hunks before it shift it
        k = c0 + sum(len(rep) - (h1 - h0) for side, (h0, h1, rep) in hunks
                     if side == "theirs" and h1 <= c0 and (theirs_side, (h0, h1, rep)) not in c)
        if o == t:
            spans.append((c0, c1, t))
        elif len(o) == len(t) == c1 - c0:
            spans.append((c0, c1, [_paragraph(b, x, y, k + i, take, conflicts)
                                   for i, (b, x, y) in enumerate(zip(bp[c0:c1], o, t))]))
        elif k in take:
            spans.append((c0, c1, o))
        else:
            conflicts.append(Clash(base="\n".join(bp[c0:c1]), ours="\n".join(o), theirs="\n".join(t), paragraph=k))
            spans.append((c0, c1, t))
    out: list[str] = []
    pos = 0
    for c0, c1, items in sorted(spans, key=lambda s: (s[0], s[1])):
        out += bp[pos:c0] + items
        pos = max(pos, c1)
    return out + bp[pos:], conflicts


def _clash(a: Hunk, b: Hunk) -> bool:
    (a0, a1, _), (b0, b1, _) = a, b
    if a0 < b1 and b0 < a1:
        return True
    if a0 == a1 == b0 == b1:
        return True
    return (a0 == a1 and b0 < a0 < b1) or (b0 == b1 and a0 < b0 < a1)


def diff3(base: str, ours: str, theirs: str) -> tuple[str, list[dict[str, str]]]:
    """`diff3_of`, its conflicts as {"base", "ours", "theirs"} (`doc_merge`, the workbench)."""
    merged, clashes = diff3_of(base, ours, theirs)
    return merged, [{"base": c.base, "ours": c.ours, "theirs": c.theirs} for c in clashes]


def diff3_of(base: str, ours: str, theirs: str) -> tuple[str, list[Clash]]:
    """Word-level three-way merge. Returns the merged text and the conflicts (the three sides'
    pieces); conflicting regions keep theirs (the deck wins)."""
    b = tokens(base)
    ours_side: Side = "ours"
    theirs_side: Side = "theirs"
    hunks = [(ours_side, h) for h in _hunks(b, tokens(ours))] + [(theirs_side, h) for h in _hunks(b, tokens(theirs))]
    spans: list[tuple[int, int, list[str]]] = []
    conflicts: list[Clash] = []
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
                conflicts.append(Clash(base="".join(b[c0:c1]), ours="".join(o), theirs="".join(t), paragraph=None))
        spans.append((c0, c1, text))
    out: list[str] = []
    pos = 0
    for c0, c1, text in sorted(spans, key=lambda s: (s[0], s[1])):
        out += b[pos:c0] + text
        pos = max(pos, c1)
    out += b[pos:]
    return "".join(out), conflicts


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def text_edit_requests(object_id: str, current: str, target: str, cell: SlidesTableCellLocation | None
                       ) -> list[SlidesRequest]:
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
    if current.endswith("\n"):
        current = current[:-1]
        target = target[:-1] if target.endswith("\n") else target
    a, b = tokens(current), tokens(target)
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    offsets = [0]
    for t in a:
        offsets.append(offsets[-1] + utf16_len(t))
    reqs: list[SlidesRequest] = []
    for op, i1, i2, j1, j2 in reversed(ops):
        if op == "equal":
            continue
        start, end = offsets[i1], offsets[i2]
        if end > start:
            text_range: SlidesRange = {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end}
            reqs.append({"deleteText": {"objectId": object_id, "textRange": text_range} if not cell else
                         {"objectId": object_id, "cellLocation": cell, "textRange": text_range}})
        insert = "".join(b[j1:j2])
        if insert:
            reqs.append({"insertText": {"objectId": object_id, "insertionIndex": start, "text": insert} if not cell else
                         {"objectId": object_id, "cellLocation": cell, "insertionIndex": start, "text": insert}})
    return reqs


# ---------------------------------------------------------------- deck edits

def _same_image(a: ImageRead | None, b: ImageRead | None) -> bool:
    """`snapshot.same_picture` of two image read-backs."""
    ha, hb = None if a is None else a.content_hash, None if b is None else b.content_hash
    if ha == hb:
        return True
    return bool(snapshot.signatures_match(None if a is None else a.signature, None if b is None else b.signature))


def object_changes_of(b: ReadBack, t: ReadBack) -> set[Edit]:
    """Fields of one object the deck changed (read-backs from snapshot.readback)."""
    out: set[Edit] = set()
    if any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(b.box, t.box)) or \
            any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(b.transform[:4], t.transform[:4])):
        out.add("geometry")
    if (b.text or "") != (t.text or ""):
        out.add("text")
    if b.text_style_hash != t.text_style_hash:
        out.add("text_style")
    if b.shape_style_hash != t.shape_style_hash:
        out.add("shape_style")
    if not _same_image(b.image, t.image):
        out.add("image")
    if b.parent_group != t.parent_group:
        out.add("group")
    return out


def deck_edits(base_el: JsonMap, slide_read: JsonMap | None) -> dict[str, list[str]]:
    """`deck_edits_of` a base element and a live slide as dicts. Only the read-backs of the
    element's own objects are read (guard asks it of every element of a deck)."""
    main = as_optional_str(base_el.get("main"), "element.main")
    mine = readbacks(base_el.get("readback") or {}, "element.readback")
    live = as_object((slide_read or {}).get("objects") or {}, "slide.objects")
    objects = {oid: readback(live[oid], f"slide.objects[{oid}]") for oid in mine if oid in live}
    edits = _deck_edits(None if main is None else ObjectId(main), mine, objects, live)
    return {f: [o for o in oids] for f, oids in edits.items()}


def deck_edits_of(base_el: ElementEntry, slide_read: SlideRead | None) -> dict[Edit, list[ObjectId]]:
    objects: Mapping[ObjectId, ReadBack] = {} if slide_read is None else slide_read.objects
    return _deck_edits(base_el.main, base_el.readback, objects, objects)


def _deck_edits(main: ObjectId | None, readback: Mapping[ObjectId, ReadBack], objects: Mapping[ObjectId, ReadBack],
                live: Collection[str]) -> dict[Edit, list[ObjectId]]:
    """Field -> object ids the deck changed, for one base element: geometry, text, text_style,
    shape_style, image, group, and deleted (its main object is gone) / part_deleted. `objects`:
    the live read-backs of the element's objects, `live` every object on the slide."""
    edits: dict[Edit, list[ObjectId]] = {}
    for oid, b in readback.items():
        t = objects.get(oid)
        if t is None:
            if oid == f"{main}_g" and main in live:
                edits.setdefault("group", []).append(oid)  # its group taken apart, the objects still there
            else:
                edits.setdefault("deleted" if oid == main else "part_deleted", []).append(oid)
            continue
        for field in object_changes_of(b, t):
            edits.setdefault(field, []).append(oid)
    if main and main not in readback:
        edits.setdefault("deleted", []).append(main)
    return edits


class UserObjectJson(TypedDict):
    """`user_objects`' answer to its dict callers."""
    objectId: str
    copy_of: str | None


@dataclass(frozen=True, kw_only=True)
class UserObject:
    """An object on a live slide the converter didn't make. `copy_of`: the tag of a converter
    object it was copied from in Slides."""
    object_id: ObjectId
    copy_of: str | None


def user_objects(base_slide: JsonMap, slide_read: JsonMap) -> list[UserObjectJson]:
    """Objects on a live slide the converter didn't make: {"objectId", "copy_of"} (copy_of: the
    tag of a converter object it was copied from in Slides)."""
    ours = {o for el in as_objects(base_slide["elements"], "slide.elements")
            for o in _str_items(el.get("objects") or [], "element.objects")}
    ours |= set(_str_items(base_slide.get("groups") or [], "slide.groups"))
    out: list[UserObjectJson] = []
    for oid, rb in as_object(slide_read["objects"], "slide.objects").items():
        if oid in ours:
            continue
        title = as_optional_str(as_object(rb, f"slide.objects[{oid}]").get("title"), "title") or ""
        out.append({"objectId": oid, "copy_of": title[4:] if title.startswith("b2s:") else None})
    return out


def user_objects_of(base_slide: SlideEntry, slide_read: SlideRead) -> list[UserObject]:
    ours = {o for el in base_slide.elements for o in el.objects}
    ours |= set(() if base_slide.seen is None else base_slide.seen.groups)
    out: list[UserObject] = []
    for oid, rb in slide_read.objects.items():
        if oid in ours:
            continue
        title = rb.title or ""
        out.append(UserObject(object_id=oid, copy_of=title[4:] if title.startswith("b2s:") else None))
    return out


def uniform_changes(base_styles: Sequence[JsonMap], theirs_styles: Sequence[JsonMap],
                    text_changed: bool) -> JsonObject | None:
    """Style attributes the deck set on all of an object's text ({} when nothing changed, None
    when the change isn't uniform and so can't be re-applied to new text).

    A read-back holds the *distinct* styles of a text, so deleting a paragraph takes its style out of
    the list - the converter gives every paragraph the line spacing of its own PDF pitch, so that
    list nearly always shrinks. `text_changed` says the deck edited this text as well: a style that
    is only missing is then the style of words that are gone, not something a person set, and there
    is nothing to re-apply. Without that, deleting one bullet made every later source change to that
    box a `text_style` conflict, and the box kept a wording the source had long moved on from
    (scenario `last-paragraph`, and the shape is common: any deleted bullet does it)."""
    def canon(styles: Iterable[JsonMap]) -> set[str]:
        return {repr(sorted(s.items())) for s in styles}
    if canon(base_styles) == canon(theirs_styles):
        return {}
    if not theirs_styles:
        return None
    if text_changed and canon(theirs_styles) <= canon(base_styles):
        return {}
    out: JsonObject = {}
    for key in {k for s in theirs_styles for k in s}:
        values = {repr(s.get(key)) for s in theirs_styles}
        if len(values) == 1 and key in theirs_styles[0] and {repr(s.get(key)) for s in base_styles} != values:
            out[key] = theirs_styles[0][key]
    # A family and its weight are written together (a weightedFontFamily without its family is a
    # 400 that refuses the whole batch), as `deck_attributes` pairs them: the other one goes along
    # when it too is one value, or the change cannot be written.
    if "weight" in out or "fontFamily" in out:
        for key in ("fontFamily", "weight"):
            if key not in out and any(key in s for s in theirs_styles):
                if len({repr(s.get(key)) for s in theirs_styles}) != 1:
                    return None
                out[key] = theirs_styles[0][key]
    # Everything else must be as before: re-applying only these attributes gives theirs.
    if not out or canon([{**s, **out} for s in base_styles]) != canon(theirs_styles):
        return None
    return out


# ---------------------------------------------------------------- units

def _units(elements: Sequence[E], key: Callable[[E], K], anchor: Callable[[E], K | None]) -> dict[K, list[E]]:
    """Unit key (the anchor text's key) -> the anchor and the elements anchored to it; an
    element without anchor is a unit of its own."""
    keys = {key(e) for e in elements}
    out: dict[K, list[E]] = {}
    for e in elements:
        a = anchor(e)
        if not (a and a in keys):
            out.setdefault(key(e), []).insert(0, e)
    for e in elements:
        a = anchor(e)
        if a and a in keys:
            out.setdefault(a, []).append(e)
    return out


def _entry_key(e: JsonMap) -> str:
    return as_str(e["key"], "element.key")


def _entry_anchor(e: JsonMap) -> str | None:
    return as_optional_str(e.get("anchor"), "element.anchor")


def units(elements: Sequence[M]) -> dict[str, list[M]]:
    """`units_of` a slide entry's elements as dicts (the same dicts, grouped)."""
    return _units(elements, _entry_key, _entry_anchor)


def _key_of(e: ElementEntry) -> ElementKey:
    return e.key


def _anchor_of(e: ElementEntry) -> ElementKey | None:
    return e.anchor


def units_of(elements: Sequence[ElementEntry]) -> dict[ElementKey, list[ElementEntry]]:
    """Unit key (the anchor text's key) -> the anchor and the elements anchored to it; an
    element without anchor is a unit of its own."""
    return _units(elements, _key_of, _anchor_of)


def keys_the_source_took(elements: Sequence[JsonMap]) -> Sequence[JsonMap]:
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
        counts[_entry_key(e)] = counts.get(_entry_key(e), 0) + 1
    if all(n == 1 for n in counts.values()):
        return elements
    taken = set(counts)
    out: list[JsonMap] = []
    moved: dict[str, str | None] = {}
    for e in elements:
        k = _entry_key(e)
        if counts[k] > 1 and e.get("removed"):
            key = next(f"{k}~{n}" for n in range(2, 10 ** 6) if f"{k}~{n}" not in taken)
            taken.add(key)
            moved[k] = None if k in moved else key   # two of them: no member can say which
            e = {**e, "key": key}
        out.append(e)
    for i, e in enumerate(out):
        was = _entry_anchor(e)
        anchor = None if was is None else moved.get(was)
        if anchor and e.get("removed"):
            fingerprint = as_object(e.get("fingerprint") or {}, "element.fingerprint")
            out[i] = {**e, "anchor": anchor, "fingerprint": {**fingerprint, "anchor": anchor}}
    return out


def _covered(members: Sequence[tuple[str, bool, str | None]]) -> set[str]:
    """`covered` over (key, tied to objects, drawn_from) of each member."""
    tied = {key for key, objects, _ in members if objects}
    return {key for key, objects, drawn in members if not objects and drawn in tied}


def _member(m: JsonMap) -> tuple[str, bool, str | None]:
    return _entry_key(m), bool(m.get("objects")), as_optional_str(m.get("drawn_from"), "element.drawn_from")


def _member_of(m: ElementEntry) -> tuple[str, bool, str | None]:
    return m.key, bool(m.objects), m.drawn_from


def covered(members: Sequence[JsonMap]) -> set[str]:
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
    return _covered([_member(m) for m in members])


def blind_members(members: Sequence[JsonMap]) -> list[str]:
    """The keys of an adopted unit's members that are tied to nothing and nothing accounts for."""
    return _blind([_member(m) for m in members])


def blind_members_of(members: Sequence[ElementEntry]) -> list[str]:
    return _blind([_member_of(m) for m in members])


def _blind(members: Sequence[tuple[str, bool, str | None]]) -> list[str]:
    ok = _covered(members)
    return [key for key, objects, _ in members if not objects and key not in ok]


def _roots(member_objects: Sequence[Sequence[str]], live: Collection[str], parent: Callable[[str], str | None]) -> list[str]:
    """Live object ids of a base unit to delete: those not inside another of its objects."""
    mine = {o for objects in member_objects for o in objects if o in live}
    return [o for objects in member_objects for o in objects if o in live and parent(o) not in mine]


def _top(roots: Sequence[str], main: str | None, children: Callable[[str], Sequence[str]]) -> str | None:
    """The unit's outermost live object (its group with anchored pictures, or its main object)."""
    for r in roots:
        if r == main or main in _below(r, children):
            return r
    return roots[0] if roots else None


def _below(oid: str, children: Callable[[str], Sequence[str]]) -> set[str]:
    out: set[str] = set()
    todo = [oid]
    while todo:
        for c in children(todo.pop()):
            out.add(c)
            todo.append(c)
    return out


class _LiveDict:
    """A live slide as a dict, read only as far as its objects' groups."""

    def __init__(self, slide_read: JsonMap) -> None:
        self.objects = as_object(slide_read["objects"], "slide.objects")

    def parent(self, oid: str) -> str | None:
        return as_optional_str(as_object(self.objects[oid], f"slide.objects[{oid}]").get("parent_group"), "parent_group")

    def children(self, oid: str) -> list[str]:
        o = self.objects.get(oid)
        if o is None:
            return []
        return _str_items(as_object(o, f"slide.objects[{oid}]").get("children") or [], "children")


class _LiveRead:
    """The same of a parsed live slide."""

    def __init__(self, slide_read: SlideRead) -> None:
        self.objects = slide_read.objects

    def parent(self, oid: str) -> str | None:
        return self.objects[ObjectId(oid)].parent_group

    def children(self, oid: str) -> Sequence[str]:
        o = self.objects.get(ObjectId(oid))
        return () if o is None or o.children is None else o.children


def unit_roots(members: Sequence[JsonMap], slide_read: JsonMap) -> list[str]:
    """Live object ids of a base unit to delete: those not inside another of its objects."""
    live = _LiveDict(slide_read)
    return _roots([_str_items(m.get("objects") or [], "element.objects") for m in members], live.objects, live.parent)


def unit_top(members: Sequence[JsonMap], slide_read: JsonMap) -> str | None:
    """The unit's outermost live object (its group with anchored pictures, or its main object)."""
    live = _LiveDict(slide_read)
    roots = _roots([_str_items(m.get("objects") or [], "element.objects") for m in members], live.objects, live.parent)
    return _top(roots, as_optional_str(members[0].get("main"), "element.main"), live.children)


def unit_top_of(members: Sequence[ElementEntry], slide_read: SlideRead) -> ObjectId | None:
    live = _LiveRead(slide_read)
    roots = _roots([m.objects for m in members], live.objects, live.parent)
    top = _top(roots, members[0].main, live.children)
    return None if top is None else ObjectId(top)


def _descendants(oid: str, slide_read: JsonMap) -> set[str]:
    return _below(oid, _LiveDict(slide_read).children)


def predicted_text(el: JsonMap) -> str:
    """The text Slides will hold for a text element (a hole as one no-break space: see collapse_holes)."""
    return "\n".join("".join(" " if r.get("hole") else as_str(r["text"], "run.text")
                             for r in as_objects(p["runs"], "paragraph.runs"))
                     for p in as_objects(el["paragraphs"], "element.paragraphs")) + "\n"


def collapse_holes(text: str) -> str:
    """Hole runs (no-break spaces sized to a formula) as one no-break space: their count follows the formula width.
    The break emit writes in front of a hole (emit.HOLE_BREAK, HOLE_BREAKS) is no character of the
    text either: a deck converted before it and one after say the same; nor is a word joiner
    (emit_widths.joined_runs: π, U+2060, [γ]); nor the spaces classify adds before a word space,
    which emit writes only where the line has room (ADDED_SPACE, emit_text.within_budget)."""
    text = ADDED_SPACE.sub("", "".join(ch for ch in text if ch not in HOLE_BREAKS and ch != WORD_JOINER))
    return re.sub(" +", " ", text)


def table_grid(text: str | None, dims: Sequence[int] | None) -> list[list[str]] | None:
    """A table read-back's cells (rows split at newlines, cells at tabs), or None when they don't
    make `dims` rows x columns: a cell holding a line break is ambiguous in that text."""
    rows = [row.split("\t") for row in (text or "").split("\n")]
    if not dims or len(rows) != dims[0] or any(len(row) != dims[1] for row in rows):
        return None
    return rows


def table_merge(base_text: str | None, ours_text: str, theirs_text: str | None,
                dims: Sequence[int] | None, theirs_dims: Sequence[int] | None) -> tuple[list[list[str]], bool] | None:
    """Word-level diff3 per cell: the merged cells and whether they are already what the source
    says. None when the two sides changed the same cell, or the tables don't line up (a row or
    column added on either side) - then the whole table is a conflict."""
    b = table_grid(base_text, dims)
    o = table_grid(ours_text, dims)
    t = table_grid(theirs_text, theirs_dims)
    if not b or not o or not t or theirs_dims != dims:
        return None
    out: list[list[str]] = []
    converged = True
    for brow, orow, trow in zip(b, o, t):
        row: list[str] = []
        for bc, oc, tc in zip(brow, orow, trow):
            merged, clashes = diff3_of(bc, oc, tc)
            if clashes:
                return None
            row.append(merged)
            converged = converged and merged == oc
        out.append(row)
    return out, converged


IR_STYLE_TO_API = {"strike": "strikethrough", "smallcaps": "smallCaps", "script": "baselineOffset", "color": "foregroundColor",
                   "highlight": "backgroundColor", "size": "fontSize", "font": "fontFamily", "family": "fontFamily",
                   "code": "fontFamily", "align": "alignment", "level": "bullet"}


def source_style_keys(base_el: ElementEntry, ours_el: ElementEntry) -> set[str]:
    """Style attributes (API names) whose values differ between two versions of an element's IR."""
    a: set[tuple[str, str]] = set()
    b: set[tuple[str, str]] = set()
    identity._styles(identity.normalise_ir(base_el.ir or {}, None, None), a)
    identity._styles(identity.normalise_ir(ours_el.ir or {}, None, None), b)
    return {IR_STYLE_TO_API.get(k, k) for k, _ in a ^ b}


def deck_style_keys(base_rb: ReadBack | None, theirs_rb: ReadBack | None) -> set[str]:
    """Text style attributes the deck changed somewhere in an object."""
    out: set[str] = set()
    for field in ("text_styles", "paragraph_styles"):
        a = {(k, repr(v)) for s in _styles_of(base_rb, field) for k, v in s.items()}
        b = {(k, repr(v)) for s in _styles_of(theirs_rb, field) for k, v in s.items()}
        out |= {k for k, _ in a ^ b}
    return out


def _styles_of(rb: ReadBack | None, field: str) -> Sequence[JsonObject]:
    if rb is None:
        return ()
    return rb.text_styles if field == "text_styles" else rb.paragraph_styles


def deck_attributes(style: JsonMap, base_styles: Sequence[JsonMap]) -> JsonObject:
    """The attributes the deck set on a run: how its style differs from the closest style the
    converter wrote into that object ({} if it is one of them)."""
    if not base_styles or style in base_styles:
        return {}
    closest = min(base_styles, key=lambda b: sum(1 for k in set(b) | set(style) if b.get(k) != style.get(k)))
    attrs: JsonObject = {k: v for k, v in style.items() if closest.get(k) != v and k != "link"}
    if "weight" in attrs or "fontFamily" in attrs:
        attrs.update({k: style[k] for k in ("fontFamily", "weight") if k in style})
    return attrs


def styling_lost(base_rb: ReadBack | None, theirs_rb: ReadBack | None, merged: str | None) -> bool:
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
    spans = None if theirs_rb is None else theirs_rb.run_spans
    before = (None if theirs_rb is None else theirs_rb.text) or ""
    if not spans or merged is None or merged == before:
        return False
    base_styles = () if base_rb is None else base_rb.text_styles
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


@dataclass(frozen=True, kw_only=True)
class Converged:
    """Source fields the deck already shows (`converged_fields`): `source` among text, size and
    position, `deck` the deck's fields that show them, text and geometry."""
    source: set[str]
    deck: set[str]


def _ir(e: ElementEntry) -> JsonObject:
    if e.ir is None:
        raise JsonShapeError(f"element {e.key}: its IR is missing")
    return e.ir


def converged_fields(anchor: ElementEntry, first: ElementEntry, base_by: Mapping[ElementKey, ElementEntry],
                     ours_by: Mapping[ElementKey, ElementEntry], edits: Mapping[Edit, list[ObjectId]],
                     theirs_rb: ReadBack | None, scale: float | None) -> Converged | None:
    """Source fields the deck already shows: {"source": {text, size, position}, "deck": {text, geometry}}
    (None if none). Only for a unit whose other members the source left alone."""
    main = anchor.main
    if not main or theirs_rb is None or anchor.key != first.key or set(base_by) != set(ours_by) or \
            any(identity.source_changes_of(base_by[k], ours_by[k]) for k in base_by if k != anchor.key):
        return None
    source: set[str] = set()
    deck: set[str] = set()
    if "text" in edits and set(edits["text"]) == {main}:
        live = theirs_rb.text or ""
        if anchor.kind == "text":
            same = collapse_holes(live) == collapse_holes(predicted_text(_ir(first)))
        elif anchor.kind == "table":
            same = [[" ".join(c.split()) for c in row.split("\t")] for row in live.split("\n")] == \
                [[" ".join(c.split()) for c in row.split("\t")] for row in identity.plain_text(_ir(first)).split("\n")]
        else:
            same = False
        if same:
            source |= {"text", "size"}
            deck.add("text")
    if "geometry" in edits and set(edits["geometry"]) == {main} and scale:
        base_rb = anchor.readback.get(main)
        bb, ob = anchor.fingerprint.bbox, first.fingerprint.bbox
        box = (0.0, 0.0) if base_rb is None else base_rb.box
        want = [box[0] + (ob[0] - bb[0]) * scale, box[1] + (ob[1] - bb[1]) * scale]
        if base_rb is not None and max(abs(a - b) for a, b in zip(want, theirs_rb.box[:2])) <= CONVERGED_PLACE and \
                all(abs(x - y) <= SCALE_TOLERANCE for x, y in zip(base_rb.transform[:4], theirs_rb.transform[:4])):
            source.add("position")
            deck.add("geometry")
    return Converged(source=source, deck=deck) if deck else None


def unit_shift(base_by: Mapping[ElementKey, ElementEntry], ours_by: Mapping[ElementKey, ElementEntry]) -> tuple[float, float] | None:
    """The step by which the source moved the unit as a whole, None when it didn't move that way.

    Sync writes a `move` by moving the unit's *top* object (the group, when the unit is grouped),
    so one step has to fit every member. A unit whose members drifted apart - the source re-placed
    an inline formula picture inside its line, or moved the paragraph while an anchored picture
    stayed - can't be written that way and has to be recreated instead."""
    if set(base_by) != set(ours_by) or not base_by:
        return None
    shifts: list[tuple[float, float]] = []
    for key, base_el in base_by.items():
        bb = base_el.fingerprint.bbox
        ob = ours_by[key].fingerprint.bbox
        shifts.append((ob[0] - bb[0], ob[1] - bb[1]))
    for i in (0, 1):
        values = [s[i] for s in shifts]
        if max(values) - min(values) > MOVE_TOLERANCE:
            return None
    dx, dy = shifts[0]  # the anchor's step; every member agrees with it
    return None if max(abs(dx), abs(dy)) <= MOVE_TOLERANCE else (dx, dy)


def geometry_writable(members: Sequence[ElementEntry], slide_read: SlideRead | None) -> bool:
    """Whether the deck's move of this unit can be put back onto a rewritten unit.

    Sync re-applies it by transforming the unit's *top* object (`sync.override_requests`), so the
    person's edit only survives when every object of the unit went along with that top. A formula
    picture dragged out of its line inside the group, or a group member nudged on its own, moved
    by itself: recreating the unit would put it back where the converter had it while the report
    says the deck's geometry was kept. Such a unit is kept as the deck has it instead."""
    objects: Mapping[ObjectId, ReadBack] = {} if slide_read is None else slide_read.objects
    top = unit_top_of(members, slide_read) if slide_read is not None and objects else None
    base_top = next((m.readback[top] for m in members if top is not None and top in m.readback), None)
    if not top or base_top is None or top not in objects:
        return True  # nothing to judge it by: leave the old behaviour
    step = snapshot.compose(list(objects[top].transform), snapshot.invert(list(base_top.transform)))
    for m in members:
        for oid, b in m.readback.items():
            live = objects.get(oid)
            if live is None:
                continue  # a part the person deleted: handled before this ("part_deleted")
            want = snapshot.compose(step, list(b.transform))
            if any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(want[:4], live.transform[:4])) or \
                    any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(want[4:], live.transform[4:])):
                return False
    return True


# ---------------------------------------------------------------- the report

@dataclass(frozen=True, kw_only=True)
class Applied:
    """Source changes written (`how`: said when not the usual way)."""
    slide: str
    element: str | None
    fields: tuple[str, ...]
    how: str | None


@dataclass(frozen=True, kw_only=True)
class Override:
    """The deck's version of these fields kept (re-applied or left standing)."""
    slide: str
    element: str | None
    fields: tuple[str, ...]


ConvergedField = Literal["image", "text", "geometry"]


@dataclass(frozen=True, kw_only=True)
class ConvergedEntry:
    """A field the deck already shows as the source now has it. `value`: the deck's text, for a
    text field (None: it holds none)."""
    slide: str
    element: str
    field: ConvergedField
    how: str | None
    value: str | None


@dataclass(frozen=True, kw_only=True)
class Resolved:
    """A conflict `--take-source` settled, with the deck's version it wrote over (`was`)."""
    id: str
    slide: str
    element: str | None
    field: ConflictField
    was: Json


@dataclass(frozen=True, kw_only=True)
class SlideUserObject:
    slide: SlideKey
    user: UserObject


@dataclass(frozen=True, kw_only=True)
class KeptSlide:
    """A slide the source dropped, kept for these reasons (`slide_touched`)."""
    slide: SlideKey
    reason: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class HeldEntry:
    """A slide nothing was written to because its label may have moved (`hold_slide`)."""
    slide: SlideKey
    label: str | None


@dataclass(frozen=True, kw_only=True)
class UserSlide:
    """A slide the person added (`copy_of`: the converter slides whose objects it carries)."""
    object_id: ObjectId
    copy_of: tuple[str, ...] | None


class Report:
    """What a merge did and did not do, as it is built up (`plan_merge_of`); `report_json` is what
    sync writes and reads."""

    def __init__(self) -> None:
        self.applied: list[Applied] = []
        self.overrides: list[Override] = []
        self.conflicts: list[Conflict] = []
        self.resolved: list[Resolved] = []
        self.converged: list[ConvergedEntry] = []
        self.user_objects: list[SlideUserObject] = []
        self.created: list[SlideKey] = []
        self.deleted: list[SlideKey] = []
        self.moved: list[SlideKey] = []
        self.kept: list[KeptSlide] = []
        self.held: list[HeldEntry] = []
        self.user_added: list[UserSlide] = []
        self.warnings: list[str] = []


def _applied_json(a: Applied) -> JsonObject:
    out: JsonObject = {"slide": a.slide, "element": a.element, "fields": _strs(a.fields)}
    if a.how is not None:
        out["how"] = a.how
    return out


def _converged_json(c: ConvergedEntry) -> JsonObject:
    out: JsonObject = {"slide": c.slide, "element": c.element, "field": c.field}
    if c.how is not None:
        out["how"] = c.how
    if c.field == "text":
        out["value"] = c.value
    return out


def report_json(r: Report) -> JsonObject:
    return {
        "applied": [_applied_json(a) for a in r.applied],
        "overrides": [{"slide": o.slide, "element": o.element, "fields": _strs(o.fields)} for o in r.overrides],
        "conflicts": [conflict_json(c) for c in r.conflicts],
        "resolved": [{"id": x.id, "slide": x.slide, "element": x.element, "field": x.field, "was": x.was}
                     for x in r.resolved],
        "converged": [_converged_json(c) for c in r.converged],
        "user_objects": [{"slide": u.slide, "objectId": u.user.object_id, "copy_of": u.user.copy_of}
                         for u in r.user_objects],
        "slides": {"created": _strs(r.created), "deleted": _strs(r.deleted), "moved": _strs(r.moved),
                   "kept": [{"slide": k.slide, "reason": _strs(k.reason)} for k in r.kept],
                   "held": [{"slide": h.slide, "reason": "label", "label": h.label} for h in r.held],
                   "user_added": [{"objectId": s.object_id, "copy_of": None if s.copy_of is None else _strs(s.copy_of)}
                                  for s in r.user_added]},
        "warnings": _strs(r.warnings),
    }


def empty_report() -> JsonObject:
    return report_json(Report())


# ---------------------------------------------------------------- what is planned

@dataclass(frozen=True, kw_only=True)
class TextOverride:
    """The deck's text edits to merge into a recreated box (`take`: paragraphs taken for the source)."""
    base: str
    theirs: str
    take: tuple[int, ...]


@dataclass(frozen=True, kw_only=True)
class TableOverride:
    """The deck's cell edits to merge into a recreated table of `dims` [rows, columns]."""
    base: str
    theirs: str
    dims: tuple[int, int] | None


@dataclass(frozen=True, kw_only=True)
class StyleOverride:
    """The deck's uniform text style changes (`uniform_changes`), and whether its run styles go
    onto the same words of the new text instead (`ranges`: sync.style_range_requests)."""
    runs: JsonObject
    paragraphs: JsonObject
    ranges: bool


@dataclass(frozen=True, kw_only=True)
class Overrides:
    """The deck's edits a recreated unit takes back (`sync.override_requests`). `geometry`: the
    person's move and resize, carried (`mode: delta`)."""
    text: TextOverride | TableOverride | None
    text_style: StyleOverride | None
    shape_style: JsonObject | None
    geometry: bool


def overrides_json(o: Overrides) -> JsonObject:
    out: JsonObject = {}
    if isinstance(o.text, TableOverride):
        out["text"] = {"table": True, "base": o.text.base, "theirs": o.text.theirs,
                       "dims": None if o.text.dims is None else [o.text.dims[0], o.text.dims[1]]}
    elif isinstance(o.text, TextOverride):
        text: JsonObject = {"base": o.text.base, "theirs": o.text.theirs}
        if o.text.take:
            text["take"] = [k for k in o.text.take]
        out["text"] = text
    if o.text_style is not None:
        style: JsonObject = {"runs": o.text_style.runs, "paragraphs": o.text_style.paragraphs}
        if o.text_style.ranges:
            style["ranges"] = True
        out["text_style"] = style
    if o.shape_style is not None:
        out["shape_style"] = o.shape_style
    if o.geometry:
        out["geometry"] = {"mode": "delta"}
    return out


@dataclass(frozen=True, kw_only=True)
class AdoptObject:
    """The deck's own object is what the source now draws."""
    key: ElementKey
    object_id: ObjectId


@dataclass(frozen=True, kw_only=True)
class CreateUnit:
    key: ElementKey


@dataclass(frozen=True, kw_only=True)
class GoneUnit:
    """The source dropped it and the deck deleted it: nothing to do."""
    key: ElementKey


@dataclass(frozen=True, kw_only=True)
class DeleteUnit:
    key: ElementKey


@dataclass(frozen=True, kw_only=True)
class KeepRemoved:
    """The source dropped it and the deck's edits keep it (`deck`: what the deck did)."""
    key: ElementKey
    deck: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class KeptJoined:
    """One the source removed, kept because its words live on in a unit kept whole (`plan_slide`)."""
    key: ElementKey


@dataclass(frozen=True, kw_only=True)
class KeepUnit:
    """Kept as the deck has it. `source`, `deck`: what each side changed. `blind`: why an adopted
    unit has members nothing can be written to, and which."""
    key: ElementKey
    source: tuple[str, ...]
    deck: tuple[str, ...]
    blind: tuple[Blind, tuple[str, ...]] | None


@dataclass(frozen=True, kw_only=True)
class Recreate:
    key: ElementKey
    source: tuple[str, ...]
    deck: tuple[str, ...]
    overrides: Overrides


@dataclass(frozen=True, kw_only=True)
class AdoptUnit:
    """The deck already shows the source's change to these fields (`adopt`)."""
    key: ElementKey
    source: tuple[str, ...]
    deck: tuple[str, ...]
    adopt: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class MoveUnit:
    """The deck's objects moved as they are by the source's step (PDF pt)."""
    key: ElementKey
    source: tuple[str, ...]
    deck: tuple[str, ...]
    delta: tuple[float, float]


UnitDecision = AdoptObject | CreateUnit | GoneUnit | DeleteUnit | KeepRemoved | KeptJoined | KeepUnit | Recreate | \
    AdoptUnit | MoveUnit


def unit_json(d: UnitDecision) -> JsonObject:
    if isinstance(d, AdoptObject):
        return {"key": d.key, "action": "adopt_object", "objectId": d.object_id}
    if isinstance(d, CreateUnit):
        return {"key": d.key, "action": "create"}
    if isinstance(d, GoneUnit):
        return {"key": d.key, "action": "none", "gone": True}
    if isinstance(d, DeleteUnit):
        return {"key": d.key, "action": "delete"}
    if isinstance(d, KeepRemoved):
        return {"key": d.key, "action": "keep", "deck": _strs(d.deck), "removed": True}
    if isinstance(d, KeptJoined):
        return {"key": d.key, "action": "keep"}
    out: JsonObject = {"key": d.key, "source": _strs(d.source), "deck": _strs(d.deck)}
    if isinstance(d, KeepUnit):
        out["action"] = "keep"
        if d.blind is not None:
            out[d.blind[0]] = _strs(d.blind[1])
    elif isinstance(d, Recreate):
        out["action"] = "recreate"
        out["overrides"] = overrides_json(d.overrides)
    elif isinstance(d, AdoptUnit):
        out["action"] = "adopt"
        out["adopt"] = _strs(d.adopt)
    elif isinstance(d, MoveUnit):
        out["action"] = "move"
        out["delta"] = [d.delta[0], d.delta[1]]
    else:
        assert_never(d)
    return out


@dataclass(frozen=True, kw_only=True)
class PlannedUnit:
    """A unit's decision on an updated slide, and the keys of its members on each side."""
    decision: UnitDecision
    base_members: tuple[ElementKey, ...]
    ours_members: tuple[ElementKey, ...]

    @property
    def key(self) -> ElementKey:
        return self.decision.key


def planned_unit_json(u: PlannedUnit) -> JsonObject:
    out = unit_json(u.decision)
    out["base_members"] = _strs(u.base_members)
    out["ours_members"] = _strs(u.ours_members)
    return out


@dataclass(frozen=True, kw_only=True)
class CreateSlide:
    key: SlideKey
    ours: int


@dataclass(frozen=True, kw_only=True)
class GoneSlide:
    """Paired, and deleted in the deck: stays deleted."""
    key: SlideKey
    ours: int
    base: int


@dataclass(frozen=True, kw_only=True)
class KeepRemovedSlide:
    """The source dropped it; the deck keeps it (`Report.kept` says why)."""
    key: SlideKey
    base: int
    object_id: ObjectId


@dataclass(frozen=True, kw_only=True)
class DeleteSlide:
    key: SlideKey
    base: int
    object_id: ObjectId


@dataclass(frozen=True, kw_only=True)
class HoldSlide:
    """Nothing written to it: which frame it is is in doubt (`hold_slide`)."""
    key: SlideKey
    ours: int
    base: int
    object_id: ObjectId


@dataclass(frozen=True, kw_only=True)
class UpdateSlide:
    """Its units merged. `background`: the source's, written (`background_written`; None is a
    background taken away); `notes`: the notes to write, None to leave them."""
    key: SlideKey
    ours: int
    base: int
    object_id: ObjectId
    units: tuple[PlannedUnit, ...]
    background_written: bool
    background: str | None
    notes: str | None


SlidePlan = CreateSlide | GoneSlide | KeepRemovedSlide | DeleteSlide | HoldSlide | UpdateSlide


def slide_plan_json(p: SlidePlan) -> JsonObject:
    if isinstance(p, CreateSlide):
        return {"key": p.key, "action": "create", "ours": p.ours, "base": None, "objectId": None}
    if isinstance(p, GoneSlide):
        return {"key": p.key, "action": "gone", "ours": p.ours, "base": p.base, "objectId": None}
    if isinstance(p, KeepRemovedSlide):
        return {"key": p.key, "action": "keep_removed", "ours": None, "base": p.base, "objectId": p.object_id}
    if isinstance(p, DeleteSlide):
        return {"key": p.key, "action": "delete", "ours": None, "base": p.base, "objectId": p.object_id}
    if isinstance(p, HoldSlide):
        return {"key": p.key, "action": "update", "held": "label", "ours": p.ours, "base": p.base,
                "objectId": p.object_id, "units": []}
    if isinstance(p, UpdateSlide):
        out: JsonObject = {"key": p.key, "action": "update", "ours": p.ours, "base": p.base, "objectId": p.object_id,
                           "units": [planned_unit_json(u) for u in p.units]}
        if p.background_written:
            out["background"] = p.background
        if p.notes is not None:
            out["notes"] = p.notes
        return out
    assert_never(p)


@dataclass(frozen=True, kw_only=True)
class MergePlan:
    """What `plan_merge_of` decided: a plan per slide, the final order (live ids, "new:<key>" for
    slides to create) and the report."""
    slides: tuple[SlidePlan, ...]
    order: tuple[str, ...]
    report: Report


def merge_plan_json(p: MergePlan) -> JsonObject:
    return {"slides": [slide_plan_json(s) for s in p.slides], "order": _strs(p.order), "report": report_json(p.report)}


# ---------------------------------------------------------------- the new conversion

Verdict = Literal["moved", "unsure"]


@dataclass(frozen=True, kw_only=True)
class ReportedMove:
    """A label `identity.label_moves` found somewhere else than it left it, as `build_ours` reports
    it (`identity.reported_move`): the base slide's key, and the key and title of what the two
    sides now have (`frame_is`, `slide_is`)."""
    label: str
    verdict: Verdict
    ours: int
    slide: str | None
    frame_is: str | None
    slide_is: str | None
    base_title: str | None
    ours_title: str | None


def _verdict(v: Json, where: str) -> Verdict:
    if v == "moved":
        return "moved"
    if v == "unsure":
        return "unsure"
    raise JsonShapeError(f"{where}: \"moved\" or \"unsure\" was expected")


def reported_move(m: JsonMap, where: str) -> ReportedMove:
    return ReportedMove(label=as_str(m["label"], f"{where}.label"), verdict=_verdict(m["verdict"], f"{where}.verdict"),
                        ours=as_int(m["ours"], f"{where}.ours"),
                        slide=as_optional_str(m.get("slide"), f"{where}.slide"),
                        frame_is=as_optional_str(m.get("frame_is"), f"{where}.frame_is"),
                        slide_is=as_optional_str(m.get("slide_is"), f"{where}.slide_is"),
                        base_title=as_optional_str(m.get("base_title"), f"{where}.base_title"),
                        ours_title=as_optional_str(m.get("ours_title"), f"{where}.ours_title"))


@dataclass(frozen=True, kw_only=True)
class NearMissReport:
    """Two slides nothing paired that say much of the same (`identity.near_misses`): the base
    slide's key, and the new frame's title."""
    slide: str
    title: str | None


@dataclass(frozen=True, kw_only=True)
class Ours:
    """The new conversion as `sync.Sync.build_ours` hands it over: slide entries with inherited
    keys, `pairs` (ours index -> base index), the label moves and near misses it found, and how
    weak a pairing was (`weak`: ours index -> "place", "crossed", "traded", "twins")."""
    slides: tuple[SlideEntry, ...]
    pairs: dict[int, int]
    label_moves: tuple[ReportedMove, ...]
    weak: dict[int, str]
    near_misses: tuple[NearMissReport, ...]


def ours_of(ours: JsonMap) -> Ours:
    where = "the new conversion"
    return Ours(
        slides=tuple(slide_entry(s, f"{where}.slides[{n}]") for n, s in enumerate(as_array(ours["slides"], where))),
        pairs={int(k): as_int(v, f"{where}.pairs") for k, v in as_object(ours["pairs"], f"{where}.pairs").items()},
        label_moves=tuple(reported_move(as_object(m, f"{where}.label_moves"), f"{where}.label_moves[{n}]")
                          for n, m in enumerate(as_array(ours.get("label_moves") or [], f"{where}.label_moves"))),
        weak={int(k): as_str(v, f"{where}.weak_pairs")
              for k, v in as_object(ours.get("weak_pairs") or {}, f"{where}.weak_pairs").items()},
        near_misses=tuple(NearMissReport(slide=as_str(m["slide"], f"{where}.near_misses"),
                                         title=as_optional_str(m.get("title"), f"{where}.near_misses"))
                          for m in as_objects(ours.get("near_misses") or [], f"{where}.near_misses")))


# ---------------------------------------------------------------- units

Adopter = Callable[[SlideKey, Sequence[ElementEntry], SlideRead, ObjectId | None], ObjectId | None]
"""The live object showing the same picture as a unit's new one - a user object, or the object
given itself (sync.picture_adopter)."""
DictAdopter = Callable[[str, list[JsonObject], JsonObject, str | None], str | None]
"""An `Adopter` over dicts, as `plan_merge`'s callers write it."""


def plan_unit(skey: SlideKey, ukey: ElementKey, base_members: Sequence[ElementEntry] | None,
              ours_members: Sequence[ElementEntry] | None, slide_read: SlideRead, report: Report,
              scale: float | None, adopt: Adopter | None, res: Resolutions, adopted: bool) -> UnitDecision:
    """The action for one element unit: keep, recreate (with deck overrides), create, delete,
    move, adopt (the deck already shows the source's change) or adopt_object (the deck's own
    object is what the source now draws); conflicts and overrides go to `report`. `scale`: deck pt
    per PDF pt. `adopt`: the live object showing the same picture as the unit's new one - a user
    object, or the object given itself (sync.picture_adopter).
    `res`: the conflicts a person asked to settle for the source (`Resolutions`).
    `adopted`: the deck is a person's own and the base is a pairing (`ADOPTED`), so an element
    tied to no object is one nothing may be written for."""
    if base_members is None:
        oid = adopt(skey, ours_members or (), slide_read, None) if adopt else None
        if oid:
            # e.g. after a pull: the source draws a picture the person added to the deck.
            report.converged.append(ConvergedEntry(slide=skey, element=ukey, field="image",
                                                   how="the deck already shows it", value=None))
            return AdoptObject(key=ukey, object_id=oid)
        report.applied.append(Applied(slide=skey, element=ukey, fields=("added",), how=None))
        return CreateUnit(key=ukey)
    edits: dict[Edit, list[ObjectId]] = {}
    for m in base_members:
        for f, oids in deck_edits_of(m, slide_read).items():
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
        kept_before = bool(base_members) and all(m.removed for m in base_members)
        if deck and deck <= {"deleted", "part_deleted"}:
            return GoneUnit(key=ukey)
        if not deck and not kept_before:
            report.applied.append(Applied(slide=skey, element=ukey, fields=("removed",), how=None))
            return DeleteUnit(key=ukey)
        report.conflicts.append(conflict_of(res, skey, ukey, "removed", "element", None,
                                            _strs(sorted(deck)) or ["the deck's own"],
                                            "kept (edited in the deck)" if deck else
                                            "kept (the deck's own since the source dropped it)", False)[0])
        return KeepRemoved(key=ukey, deck=tuple(sorted(deck)))
    base_by = {m.key: m for m in base_members}
    ours_by = {m.key: m for m in ours_members}
    src: set[str] = set()
    for k in base_by.keys() | ours_by.keys():
        if k not in base_by or k not in ours_by:
            src.add("layout")
        else:
            src |= identity.source_changes_of(base_by[k], ours_by[k])
    source, deck_said = tuple(sorted(src)), tuple(sorted(deck))
    edited: set[Edit] = deck & set(EDIT_FIELDS)
    applied_fields = set(src)   # less what the deck's re-applied edits write over (edit hunt h5-3)
    if not src:
        said = deck - {"image"} if "image" in deck and pictures_unchecked(slide_read, edits["image"]) else deck
        if said:
            report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(said))))
        return KeepUnit(key=ukey, source=source, deck=deck_said, blind=None)
    if "deleted" in deck:
        report.conflicts.append(conflict_of(res, skey, ukey, "deleted", "element", _strs(source), None,
                                            "kept deleted", False)[0])
        return KeepUnit(key=ukey, source=source, deck=deck_said, blind=None)
    if "part_deleted" in deck:
        report.conflicts.append(conflict_of(res, skey, ukey, "part_deleted", "element", _strs(source),
                                            _strs(edits["part_deleted"]), "deck kept", False)[0])
        return KeepUnit(key=ukey, source=source, deck=deck_said, blind=None)
    blind: list[str] = blind_members_of(base_members) if adopted else []
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
        loose = [m for m in base_members if m.key in set(blind)]
        drawn = all(m.from_layout for m in loose)
        celled = not drawn and all(m.in_table for m in loose)
        why: tuple[Blind, str] = ("inherited", "kept (the deck's layout draws this, not the slide)") if drawn else \
            ("in_table", "kept (a cell of a table of the deck's)") if celled else \
            ("unpaired", "kept (tied to no object of the deck)")
        report.conflicts.append(conflict_of(res, skey, ukey, why[0], "element", _strs(source),
                                            _strs(deck_said) or ["the deck's own"], why[1], False)[0])
        return KeepUnit(key=ukey, source=source, deck=deck_said, blind=(why[0], tuple(blind)))
    if not edited:
        report.applied.append(Applied(slide=skey, element=ukey, fields=source, how=None))
        if "group" in deck:
            report.overrides.append(Override(slide=skey, element=ukey, fields=("group",)))
        return Recreate(key=ukey, source=source, deck=deck_said,
                        overrides=Overrides(text=None, text_style=None, shape_style=None, geometry=False))

    anchor, first = base_members[0], ours_members[0]
    main = anchor.main
    base_rb = None if main is None else anchor.readback.get(main)
    theirs_rb = None if main is None else slide_read.objects.get(main)
    b_text, t_text = (None if base_rb is None else base_rb.text), (None if theirs_rb is None else theirs_rb.text)
    same = converged_fields(anchor, first, base_by, ours_by, edits, theirs_rb, scale)
    if same and src <= same.source:
        # The deck already shows what the source now says (e.g. deck edits pulled into the source):
        # nothing to write; the base takes the deck's version of those fields.
        for field in same.deck:
            report.converged.append(ConvergedEntry(slide=skey, element=ukey, field=_converged_field(field), how=None,
                                                   value=t_text))
        rest = edited - same.deck
        if rest:
            report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(rest))))
        return AdoptUnit(key=ukey, source=source, deck=deck_said, adopt=tuple(sorted(same.deck)))
    shift = unit_shift(base_by, ours_by)
    if "position" in src and src <= {"position", "size"} and "geometry" not in edited and shift:
        # Only the place changed in the source, and the whole unit moved by the same step: move the
        # edited deck objects there (sync moves the unit's top object). A member that moved on its
        # own - a formula picture the source re-placed inside its line - can't be written that way,
        # so that unit is recreated instead (`shift` is None).
        report.applied.append(Applied(slide=skey, element=ukey, fields=("position",), how="deck object moved"))
        report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(edited))))
        return MoveUnit(key=ukey, source=source, deck=deck_said, delta=shift)
    text_ov: TextOverride | TableOverride | None = None
    style_ov: StyleOverride | None = None
    shape_ov: JsonObject | None = None
    geometry_ov = False
    conflicts: list[Conflict] = []

    def conflict(field: ConflictField, base_v: Json, ours_v: Json, theirs_v: Json, resolution: str,
                 takeable: bool) -> bool:
        entry, was_taken = conflict_of(res, skey, ukey, field, base_v, ours_v, theirs_v, resolution, takeable)
        conflicts.append(entry)
        return was_taken

    def taken(field: TakeField, base_v: Json, ours_v: Json, theirs_v: Json, resolution: str) -> bool:
        """Report the conflict, and say whether the person asked for the source's version of this
        field. If they did, the deck's edit to it is not an override any more - nothing of it is
        re-applied, and the report must not promise it was kept."""
        if not conflict(field, base_v, ours_v, theirs_v, resolution, True):
            return False
        edited.discard(field)
        return True

    def on_deck(box: Box) -> list[Json]:
        """A converter box in deck pt, as the deck's own box beside it is (edit hunt h2-5: three boxes
        in two units read as a wrong size)."""
        return _nums([round(v * scale, 2) for v in box]) if scale else _nums(box)

    theirs_box = None if theirs_rb is None else _nums(theirs_rb.box)
    keep = False
    # What the object will say once this unit is written: the source's new text, or the merge of it
    # with the deck's. `styling_lost` needs it to see whether the styled words are still in there.
    written = collapse_holes(predicted_text(_ir(first))) if first.kind == "text" else None
    if "image" in edited:
        if adopt and adopt(skey, ours_members, slide_read, main):
            # The picture in the deck is the one the source now draws (a figure pull replaced).
            report.converged.append(ConvergedEntry(slide=skey, element=ukey, field="image",
                                                   how="the deck's picture is what the source draws", value=None))
            rest = edited - {"image", "geometry"}
            if rest:
                report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(rest))))
            return AdoptUnit(key=ukey, source=source, deck=deck_said, adopt=tuple(sorted(edited & {"image", "geometry"})))
        keep = not taken("image", "picture", _strs(source), "replaced in the deck", "deck kept")
    if "text" in edited and not keep:
        if anchor.kind == "table" and set(edits["text"]) == {main}:
            cells = table_merge(b_text, identity.plain_text(_ir(first)), t_text,
                                None if base_rb is None else base_rb.table, None if theirs_rb is None else theirs_rb.table)
            if cells is None:
                keep = not taken("text", anchor.fingerprint.text, first.fingerprint.text, t_text, "deck kept")
            else:
                text_ov = TableOverride(base=b_text or "", theirs=t_text or "",
                                        dims=None if base_rb is None else base_rb.table)
                if cells[1]:
                    report.converged.append(ConvergedEntry(slide=skey, element=ukey, field="text", how=None,
                                                           value=t_text))
        elif anchor.kind != "text" or set(edits["text"]) != {main}:
            keep = not taken("text", anchor.fingerprint.text, first.fingerprint.text, t_text, "deck kept")
        else:
            b, o, t = (collapse_holes(x) for x in (b_text or "", predicted_text(_ir(first)), t_text or ""))
            merged, clashes, safe = text_merge_of(b, o, t, ())
            if not safe or (clashes and merged == t):
                # Nothing of the source's survived the merge (or it cannot be written safely): the
                # deck's text stands as it is, and there is nothing to write.
                keep = not taken("text", b, o, t, "deck kept")
            else:
                # Paragraphs both sides rewrote are conflicts of their own: the deck keeps them,
                # and the rest of the box still takes what the source now says. A person who read
                # the report can name one of them and have the source's paragraph written instead;
                # the others are untouched, which is the point of merging by paragraph at all.
                take = [c.paragraph for c in clashes
                        if conflict("text", c.base, c.ours, c.theirs, "deck kept", True) and c.paragraph is not None]
                if take:
                    merged, clashes, safe = text_merge_of(b, o, t, take)
                text_ov = TextOverride(base=b_text or "", theirs=t_text or "", take=tuple(take))
                written = merged
                if merged == o:
                    report.converged.append(ConvergedEntry(slide=skey, element=ukey, field="text", how=None,
                                                           value=t_text))
    if "text_style" in edited and not keep:
        cut = main in edits.get("text", ())  # the deck edited this text too (see `uniform_changes`)
        runs = uniform_changes(_styles_of(base_rb, "text_styles"), _styles_of(theirs_rb, "text_styles"), cut)
        paras = uniform_changes(_styles_of(base_rb, "paragraph_styles"), _styles_of(theirs_rb, "paragraph_styles"), cut)
        # (a source "style" change can be list levels or sizes; only the same attributes clash)
        source_keys = source_style_keys(anchor, first) if "style" in src else set()
        deck_keys = deck_style_keys(base_rb, theirs_rb)
        clash = "style" in src and bool(source_keys & deck_keys)
        if paras is None or set(edits["text_style"]) != {main} or anchor.kind not in ("text", "table"):
            keep = not taken("text_style", "style", _strs(source), "restyled in the deck", "deck kept")
        elif runs is None or anchor.kind == "table":
            # Some words restyled (or a table): the deck's run styles go onto the same words of the
            # new text (sync.style_range_requests).
            style_ov = StyleOverride(runs={}, paragraphs=paras, ranges=True)
            if styling_lost(base_rb, theirs_rb, written):
                # ... and some of those words are not in the new text at all. The styling on them
                # ends here, and saying nothing would make the report claim it was kept.
                conflict("text_style", "style", "the words it was on were replaced", "restyled in the deck",
                         "the styling of the replaced words is gone", False)
            if clash and taken("text_style", "style", "restyled in the source", "restyled in the deck",
                               "deck style re-applied"):
                style_ov = None
        else:
            style_ov = StyleOverride(runs=runs, paragraphs=paras, ranges=False)
            if clash and taken("text_style", "style", "restyled in the source", "restyled in the deck",
                               "deck style re-applied"):
                style_ov = None
            elif clash and source_keys <= deck_keys:
                # The deck's own re-applied run styles cover every attribute the source changed
                # (`runs` is the uniform, whole-attribute case, unlike the "ranges" one above): the
                # write leaves this element exactly as the deck had it, so "style" was not applied.
                applied_fields.discard("style")
    if "shape_style" in edited and not keep:
        if anchor.kind != "shape" or set(edits["shape_style"]) != {main}:
            keep = not taken("shape_style", "style", _strs(source), "restyled in the deck", "deck kept")
        else:
            shape_ov = None if theirs_rb is None else theirs_rb.shape_style
            if "style" in src and taken("shape_style", "style", "restyled in the source",
                                        "restyled in the deck", "deck style re-applied"):
                shape_ov = None
    if "geometry" in edited and not keep and not geometry_writable(base_members, slide_read):
        # The person moved something inside the unit; a rewritten unit can't be put back that way.
        keep = not taken("geometry", on_deck(anchor.fingerprint.bbox), on_deck(first.fingerprint.bbox),
                         theirs_box, "deck kept (the deck moved a part of the element on its own)")
    if "geometry" in edited and not keep:
        # The person's move and resize (base -> theirs) go on top of wherever the source now puts the
        # unit (`sync.carried`), whether or not the source moved it too. When it did, it is still a
        # conflict - both sides moved one thing - but the deck's absolute place is not what wins: that
        # dropped the person's size (the recreated box came back at the converter's height, the words
        # ran out of it) and ignored the source's reflow around it (a box that grew by a line ran into
        # the one the person had moved, an equation came down onto the paragraph the person had moved;
        # docs/project-notes.md "Both-moved geometry").
        geometry_ov = True
        if "position" in src and taken("geometry", on_deck(anchor.fingerprint.bbox),
                                       on_deck(first.fingerprint.bbox), theirs_box, GEOMETRY_CARRIED):
            geometry_ov = False
    moving = keep and "position" in src and "geometry" not in edited and shift is not None
    if keep and conflicts:
        # A unit kept whole drops every change the source made to it, not only the one the conflict
        # is about: a moved-apart group member kept the list, and the source's new fourth item with
        # it, while the report named only "geometry" (edit hunt h3-2).
        named: set[str] = set().union(*(CONFLICT_COVERS.get(c.field, {c.field}) for c in conflicts))
        lost = sorted(set(src) - named - ({"position"} if moving else set()))
        if lost:
            k = next((n for n in range(len(conflicts) - 1, -1, -1) if conflicts[n].resolution != TAKEN_SAYS),
                     len(conflicts) - 1)
            conflicts[k] = replace(conflicts[k], resolution=conflicts[k].resolution +
                                   f"; the source's change to its {', '.join(lost)} was not written either")
    report.conflicts += conflicts
    # `edited` can be empty by now: `taken` takes a field out of it when the person asked for the
    # source's version of it, and an entry here says "the deck's version of these fields was kept",
    # which with no fields left would be a promise about nothing.
    if keep:
        if edited:
            report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(edited))))
        if moving and shift is not None:
            # The deck's version of what the unit says stands, but where it stands is the source's
            # alone to change: the source moved the table the person had added a row to, and the
            # unit kept in place left the source's new caption over it (live fuzz r8006,
            # table-moved). The deck's objects move as they are, like a unit only the source moved.
            report.applied.append(Applied(slide=skey, element=ukey, fields=("position",), how="deck object moved"))
            return MoveUnit(key=ukey, source=source, deck=deck_said, delta=shift)
        return KeepUnit(key=ukey, source=source, deck=deck_said, blind=None)
    if applied_fields:
        report.applied.append(Applied(slide=skey, element=ukey, fields=tuple(sorted(applied_fields)), how=None))
    if edited:
        report.overrides.append(Override(slide=skey, element=ukey, fields=tuple(sorted(edited))))
    return Recreate(key=ukey, source=source, deck=deck_said,
                    overrides=Overrides(text=text_ov, text_style=style_ov, shape_style=shape_ov, geometry=geometry_ov))


def _converged_field(field: str) -> ConvergedField:
    if field == "text":
        return "text"
    if field == "geometry":
        return "geometry"
    raise ValueError(f"no converged field {field!r}")


# ---------------------------------------------------------------- slides

ABSORBED = 0.6       # share of a dropped slide's words another frame now says, to be told
ABSORBED_WORDS = 6   # fewer words than this say nothing of where they went


def absorbed_by(slide: SlideEntry, ours_slides: Sequence[SlideEntry], was: Mapping[int, SlideEntry]) -> SlideEntry | None:
    """The source frame that now says most of what a dropped slide said, in words it did not say
    before (`was`: ours index -> the base slide it continues), or None. Words of four letters or
    more, so that "of" and "the" make no two slides alike; and only the frame's new words, because
    an outline or a summary says the whole talk's vocabulary every time (over the edit hunt's 132
    slides, counting every word flagged two outlines that were never moved anywhere)."""
    def words(s: SlideEntry) -> Counter[str]:
        title = set(identity.norm_title(s.title).split())
        return Counter(w for w in identity.norm_title(s.text).split() if w not in title and len(w) >= 4)
    mine = words(slide)
    if sum(mine.values()) < ABSORBED_WORDS:
        return None
    gained = [words(o) - (words(was[n]) if n in was else Counter()) for n, o in enumerate(ours_slides)]
    share, home = max(((sum((mine & g).values()) / sum(mine.values()), n) for n, g in enumerate(gained)),
                      default=(0.0, None))
    return ours_slides[home] if home is not None and share >= ABSORBED else None


def report_label_moves(moves: Sequence[JsonMap], report: JsonObject, held: bool) -> None:
    """`label_move_report` of moves as `build_ours` reports them, onto a report as JSON."""
    found = Report()
    label_move_report([reported_move(m, "label move") for m in moves], found, held)
    as_array(report["conflicts"], "report.conflicts").extend(conflict_json(c) for c in found.conflicts)
    as_array(report["warnings"], "report.warnings").extend(found.warnings)


@dataclass(frozen=True, kw_only=True)
class LabelRenames:
    """Labels the source's frames carry that no base slide does (`fresh`), and base labels no
    frame carries any more (`lost`)."""
    fresh: frozenset[str]
    lost: frozenset[str]


def label_renames(base: Iterable[str | None], ours: Iterable[str | None]) -> LabelRenames:
    """A label is a name, and a name that changes is two facts at once: one the deck knows is
    carried by nothing now, and one nothing here has seen has appeared. Either alone says little
    (a dropped label has its own warning; `beamer2slides label --apply` makes new names by the
    dozen), but together they are a rename, which no PDF can tell from a name pasted onto the
    frame next door. `plan_merge_of` warns on it; `fuzz_labels` counts it as warned."""
    had = {x for x in base if x}
    has = {x for x in ours if x}
    return LabelRenames(fresh=frozenset(has - had), lost=frozenset(had - has))


def label_move_report(moves: Sequence[ReportedMove], report: Report, held: bool) -> None:
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
        moved = m.verdict == "moved"
        ours_says = f"the frame `{m.label}` now says \"{(m.ours_title or '').strip()}\""
        if m.frame_is:
            ours_says += f", which is what the slide `{m.frame_is}` says"
        base_says = f"the slide `{m.label}` says \"{(m.base_title or '').strip()}\""
        if m.slide_is:
            base_says += f", which the source now has under \"{m.slide_is}\""
        slide = m.slide or m.label
        report.conflicts.append(conflict_of(
            None, slide, None, "label", base_says, ours_says, None,
            "the label moved: identity taken from the content instead" if moved else
            "either the label moved or that passage did: nothing written to this slide" if held else
            "either the label moved or that passage did: followed the label, nothing re-paired", False)[0])
        # An `unsure` verdict re-pairs nothing, so saying only "check the .tex" leaves the reader to
        # work out what happens if they do not. What happens is that the frame now carrying the
        # label would be written onto the slide the label names - the slide with somebody's edits on
        # it. That slide is named, and by default it is held back rather than written.
        at_risk = "" if moved else (
            f" Nothing was written to the slide `{slide}`: the frame carrying `{m.label}` would have gone "
            f"onto it, edits and all, and which frame that slide belongs to is the question. The rest of the "
            f"deck was synced. `--follow-labels` writes it anyway."
            if held else
            f" This sync writes the frame carrying `{m.label}` onto the slide `{slide}`, edits and all, "
            f"so that is the slide to look at.")
        report.warnings.append(
            f"label `{m.label}` is not on the frame this deck's slide was made from" + (
                ". Deck edits belong to the words a person edited, so sync went by the content and not by the "
                "label. Put the label back on its own frame" if moved else
                ", or a passage moved between two frames - from the PDF alone the two look the same. Check the "
                "`.tex`: if the label moved, put it back; if the passage did, the labels are right") +
            ": docs/labels.md, \"If a label does change\"." + at_risk)


def _dict_adopter(adopt: DictAdopter, reads: Mapping[str, JsonObject]) -> Adopter:
    """A dict caller's adopter over the records: it is handed the members as JSON and the live slide
    as it was read."""
    def typed(skey: SlideKey, members: Sequence[ElementEntry], read: SlideRead, oid: ObjectId | None) -> ObjectId | None:
        found = adopt(skey, [element_entry_json(m) for m in members], reads[read.object_id], oid)
        return ObjectId(found) if found else None
    return typed


def plan_merge(base: JsonMap, ours: JsonMap, theirs: JsonMap) -> JsonObject:
    """`plan_merge_with` nothing adopted, no unsure label followed and no conflict settled."""
    return plan_merge_with(base, ours, theirs, adopt=None, follow_labels=False, take_source=())


def plan_merge_with(base: JsonMap, ours: JsonMap, theirs: JsonMap, *, adopt: DictAdopter | None,
                    follow_labels: bool, take_source: Iterable[str] | None) -> JsonObject:
    """ours: {"slides": [slide entries with inherited keys], "pairs": {ours index: base index}}.
    `adopt`: see plan_unit (a picture the deck already shows).
    `follow_labels`: write to a slide whose label `identity.label_moves` is unsure about anyway.
    `take_source`: conflict ids from an earlier report to settle for the source (`Resolutions`).
    Returns {"slides": [per slide plan], "order": [live slide ids or "new:<key>"], "report"}: the
    JSON of `plan_merge_of`, the three parsed where they enter."""
    reads = {as_str(s["objectId"], "slide.objectId"): s for s in as_objects(theirs["slides"], "the deck.slides")}
    typed = None if adopt is None else _dict_adopter(adopt, reads)
    return merge_plan_json(plan_merge_of(parse_base(base), ours_of(ours), deck_read(theirs), typed, follow_labels,
                                         Resolutions(take_source or ())))


def plan_merge_of(base: Base, ours: Ours, theirs: DeckRead, adopt: Adopter | None, follow_labels: bool,
                  res: Resolutions) -> MergePlan:
    """The merge of the three (`plan_merge`). `res`: the conflicts to settle for the source."""
    report = Report()
    live = {s.object_id: s for s in theirs.slides}
    base_slides = base.slides
    pairs = ours.pairs
    matched_base = set(pairs.values())
    plans: list[SlidePlan] = []
    moves = ours.label_moves
    label_move_report(moves, report, not follow_labels)
    # An `unsure` label move is the one place where this merge does not know which frame a slide
    # belongs to, and every other rule here assumes it does: the words are merged, the deck's edits
    # kept, the source's changes written - onto whichever slide the pairing names. Get that wrong
    # and nothing is deleted and nothing is lost, but one frame's sentences land beside somebody's
    # edits about another frame, and the way back is by hand. So the slide waits.
    held = set() if follow_labels else {m.ours for m in moves if m.verdict == "unsure"}

    weak = ours.weak
    renamed = label_renames([s.label for s in base_slides], [s.label for s in ours.slides])
    for j, o in enumerate(ours.slides):
        i = pairs.get(j)
        b = base_slides[i] if i is not None else None
        if b is not None and weak.get(j) == "place" and not o.label:
            # `identity.gap_pairs`: this frame has no label, and the source changed enough of it
            # that only its place says which frame it is. Nothing was at risk - the alternative was
            # a second slide beside this one - but the next version has one hook fewer to hang on.
            report.warnings.append(
                f"slide {b.key}: this frame has no label, and the source changed its title and much of what "
                f"it says; it was matched by where it stands, between the frames around it. Move it too, or "
                f"rewrite the rest of it, and there is nothing left to recognise it by - give it a label "
                f"(`beamer2slides label`), see docs/labels.md.")
        if b is not None and weak.get(j) == "crossed":
            # `identity.crossed_twins`: this frame's label and another's changed places over two
            # slides so alike that reading the two frames the other way round loses nothing, so
            # nothing in the words can say whether the author moved the frames or moved a label.
            # The label was followed; it is the promise, and nobody's edits move either way.
            report.warnings.append(
                f"slide {b.key}: this frame's label `{o.label}` and another's have changed places "
                f"over two slides so alike that reading the two frames the other way round says the deck just "
                f"as well, so nothing they say can tell whether you moved the frames or moved a label. The "
                f"labels were followed. If `{o.label}` belongs on the other frame, put it back before "
                f"the next sync - see docs/labels.md.")
        if b is not None and weak.get(j) == "traded":
            # `identity.crossed_twins`: a label and the frame beside it changed places over two
            # slides so alike that reading the two frames the other way round loses nothing, and
            # that frame carries no label of its own - so nothing can say whether the author moved
            # the frames or pasted the label onto the twin. The label was followed; nobody's edits
            # move either way.
            report.warnings.append(
                f"slide {b.key}: this slide and another are so alike that reading their two frames the "
                f"other way round says the deck just as well, and the frames that carry them have changed "
                f"places - so nothing they say can tell whether you moved the frames or moved a label. The "
                f"label was followed. Give the other frame a label too "
                f"(`beamer2slides label`), and the next sync has an answer - see docs/labels.md.")
        if b is not None and weak.get(j) == "twins" and not o.label:
            # `identity.align_slides`: this frame has no label and says about as much as its
            # neighbour does, so putting it on this slide and putting it on the other one are the
            # same alignment as far as the words go. It was put here; a person who knows which
            # frame is which should say so with a label before the next sync repeats the guess.
            report.warnings.append(
                f"slide {b.key}: this frame has no label, and it and the slides around it say so nearly "
                f"the same thing that the match could as well have been one of them; it was matched here. "
                f"Your edits on that slide are safe either way, but which slide this frame writes to next "
                f"time is a coin toss - give it a label (`beamer2slides label`), see docs/labels.md.")
        if b is not None and b.label and o.label != b.label:
            # The label is gone or different, and the content recognised the frame anyway. Nothing
            # is at risk this time; the next version of the source has one hook fewer to hang on.
            now = f"`{o.label}`" if o.label else "gone"
            report.warnings.append(
                f"slide {b.key}: the frame's label is {now} now, not `{b.label}`; this slide was "
                f"matched by what it says instead. A label is what makes a slide's identity survive "
                f"an edit the content alone cannot explain - see docs/labels.md.")
        if b is not None and not b.label and o.label in renamed.fresh and renamed.lost:
            # The mirror of the warning above: the slide carries no label, so that one is silent,
            # while the frame carries a name no slide of this deck was made from and a name the
            # deck knows is on no frame any more. From the PDF that is a rename, and it reads
            # exactly like the name pasted onto the frame next door; the words settled the pairing.
            gone = ", ".join(f"`{x}`" for x in sorted(renamed.lost)[:3])
            report.warnings.append(
                f"slide {b.key}: this frame's label `{o.label}` is one this deck has never seen, "
                f"and nothing carries {gone} any more, which it does know - a label renamed, as far as "
                f"the PDF can say. This slide was matched by what the frame says instead, the label "
                f"telling it nothing. If `{o.label}` was put on another frame rather than renamed, "
                f"this is the slide to look at - see docs/labels.md, \"If a label does change\".")
        read = live.get(b.object_id) if b is not None and b.object_id else None
        if b is None or i is None:
            report.created.append(o.key)
            plans.append(CreateSlide(key=o.key, ours=j))
            continue
        if read is None:
            # (frame counters change whenever a frame is added before: not worth a conflict)
            def content(els: Sequence[ElementEntry]) -> list[ElementEntry]:
                return [e for e in els if e.role != "footer"]
            changed = any(identity.source_changes_of(e, oe) for e in content(b.elements) for oe in content(o.elements)
                          if e.key == oe.key) \
                or {e.key for e in content(b.elements)} != {e.key for e in content(o.elements)}
            if changed:
                report.conflicts.append(conflict_of(res, o.key, None, "slide", "slide", "changed", "deleted",
                                                    "kept deleted", False)[0])
            plans.append(GoneSlide(key=o.key, ours=j, base=i))
            continue
        if j in held:
            plans.append(hold_slide(b, o, read, report, j, i))
            continue
        plans.append(plan_slide(b, o, read, report, base, j, i, adopt, res))

    for i, b in enumerate(base_slides):
        if i in matched_base:
            continue
        read = live.get(b.object_id) if b.object_id is not None else None
        if read is None:
            continue  # removed on both sides
        touched = slide_touched_of(b, read)
        if not touched and base.origin == ADOPTED:
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
            report.kept.append(KeptSlide(slide=b.key, reason=tuple(touched)))
            plans.append(KeepRemovedSlide(key=b.key, base=i, object_id=read.object_id))
        else:
            report.deleted.append(b.key)
            plans.append(DeleteSlide(key=b.key, base=i, object_id=read.object_id))

    kept_keys: set[str] = {k.slide for k in report.kept}
    for m in ours.near_misses:
        # `identity.near_misses`: nothing paired these two, and nothing should have - but they say
        # too much of the same for the author not to be told the question came up. Either the source
        # really did write a new frame beside a dropped one, and this is noise, or one frame was
        # changed so far in one version that sync could not follow it.
        if m.slide not in kept_keys and m.slide not in report.deleted:
            continue    # the slide is still in the deck for another reason: nothing to report
        where = ("the deck keeps the old slide, with your edits on it, beside the new one"
                 if m.slide in kept_keys else "the old slide was untouched, so it is gone")
        report.warnings.append(
            f"slide {m.slide}: the source has no frame this slide could be matched to, and the frame "
            f"{m.title!r} is new - but the two say much of the same thing. If they are one frame, it was "
            f"retitled, reworded and moved too much in one version to be followed, and {where}. Give that "
            f"frame a label (`beamer2slides label`) and this cannot happen to it again, see docs/labels.md.")
    near = {m.slide for m in ours.near_misses}
    was = {j: base_slides[i] for j, i in pairs.items()}
    for k in report.kept:
        kb = next((s for s in base_slides if s.key == k.slide), None)
        home = absorbed_by(kb, ours.slides, was) if kb is not None and k.slide not in near else None
        if home is not None:
            # The source folded this frame into another one (edit hunt h2-1): any edit keeps the old
            # slide, rightly, and then its words are in the deck twice with nothing said.
            report.warnings.append(
                f"slide {k.slide}: the source dropped this frame, and most of what it said is now on "
                f"{home.title or home.key!r}. The deck keeps this slide because of your edits on it, so "
                f"those words are there twice: delete this slide once your edits are where they belong.")

    base_ids = {b.object_id for b in base_slides}
    for s in theirs.slides:
        if s.object_id in base_ids:
            continue
        # (a slide duplicated in Slides carries the tags of the original's objects)
        copies = {(rb.title or "")[4:].rsplit("/", 3)[0] for rb in s.objects.values()
                  if (rb.title or "").startswith("b2s:")}
        report.user_added.append(UserSlide(object_id=s.object_id, copy_of=tuple(sorted(copies)) or None))
    unaccounted = [k.slide for k in report.kept if k.reason == ("the deck's own",)]
    if unaccounted:
        # The leftover-base loop: slides of a person's own deck that no frame of the source explains.
        report.warnings.append(
            f"{len(unaccounted)} slide(s) of this deck are accounted for by no frame of the source, and were "
            f"kept: {', '.join(unaccounted[:3])}{', ...' if len(unaccounted) > 3 else ''}. Nothing here made "
            f"those slides, so a frame gone out of the source is as likely to be a label that moved as a "
            f"slide you meant to drop - put the label back where `adopt` wrote it (docs/labels.md) and the "
            f"frame finds its slide again; if you did mean to drop it, delete the slide in Slides.")
    unpaired = _blind_units(plans, "unpaired")
    if unpaired:
        # `plan_unit`: the source changed elements this deck's pairing cannot place. Each one is a
        # conflict of its own; this says the thing a person has to *do* about them, once.
        report.warnings.append(
            f"{len(unpaired)} element(s) the source changed could not be tied to any object of this deck, so "
            f"they were left exactly as the deck has them: {', '.join(unpaired[:3])}"
            f"{', ...' if len(unpaired) > 3 else ''}. `adopt` paired the source it wrote with the deck's own "
            f"objects by where they stand and what they say, and these it could not place (the base lists "
            f"every one of them under `adopt.unpaired`, with the reason); writing one would put a second "
            f"object beside yours rather than over it. Change them in the deck itself - the rest of this "
            f"sync went in as usual.")
    inherited = _blind_units(plans, "inherited")
    if inherited:
        # The other half of it: these the source *can* draw and this deck does draw - on its
        # layouts, which are the person's own look and which nothing here ever writes to
        # (`adopt_sync.layout_elements`, `build_base`'s `master_background = None`).
        report.warnings.append(
            f"{len(inherited)} element(s) the source changed are drawn by this deck's layouts or its master, "
            f"not by the slide: {', '.join(inherited[:3])}{', ...' if len(inherited) > 3 else ''}. `adopt` "
            f"recovered those as the theme of the source it wrote, so the source draws them on every slide "
            f"that inherits them - but they belong to the template, and writing one would put a copy on this "
            f"one slide over a thing every other slide still shows. Change them in Slides under "
            f"Slide > Edit theme - the rest of this sync went in as usual.")
    celled = _blind_units(plans, "in_table")
    if celled:
        # And the third: a cell of a table of the deck's. The person can see the table, so the
        # thing to say is which part of it this is and that the cell is theirs to change
        # (`adopt_sync.inside_tables` for why nothing here can write into one).
        report.warnings.append(
            f"{len(celled)} element(s) the source changed stand inside a table of this deck: "
            f"{', '.join(celled[:3])}{', ...' if len(celled) > 3 else ''}. This converter reads a table you "
            f"drew back as the loose words of its cells, not as a table, so there is no cell here to write "
            f"into and nothing was written - putting the words back into one has to be right cell by cell or "
            f"it writes one cell's words into another's. Edit those cells in Slides - the rest of this sync "
            f"went in as usual.")
    report_resolutions(res, report)
    order, moved = plan_order(base, theirs, plans, report)
    report.moved = moved
    return MergePlan(slides=tuple(plans), order=tuple(order), report=report)


def _blind_units(plans: Sequence[SlidePlan], why: Blind) -> list[str]:
    """`slide` / `unit` of the units kept for `why` (`plan_unit`'s `blind`)."""
    return [f"`{p.key}` / `{u.key}`" for p in plans if isinstance(p, UpdateSlide) for u in p.units
            if isinstance(u.decision, KeepUnit) and u.decision.blind is not None and u.decision.blind[0] == why]


def report_resolutions(res: Resolutions, report: Report) -> None:
    """What `--take-source` settled, and what it did not reach.

    `resolved` keeps the deck's own version of each spot that was written over, verbatim: that is
    the way back, and it has to be in the report because nowhere else will have it a minute from
    now. An id that matched nothing is the ordinary case of a report read a version too late -
    nothing was written and nothing is lost, and the warning is there so that a person who meant
    to settle something does not read "no conflicts" and believe it happened."""
    for c in report.conflicts:
        if c.resolution == TAKEN_SAYS:
            report.resolved.append(Resolved(id=c.id, slide=c.slide, element=c.element, field=c.field, was=c.theirs))
    for cid in res.unused:
        report.warnings.append(
            f"--take-source {cid}: no conflict in this sync has that id, so nothing was settled for the "
            f"source. An id names one disagreement and stops matching as soon as either side of it moves, "
            f"so a report a version old cannot reach today's conflict - read the report this run wrote and "
            f"take the id from there. Nothing was written on that account and nothing is lost.")


def pictures_unchecked(read: SlideRead, oids: Sequence[ObjectId]) -> bool:
    """Whether these live pictures are ones sync did not read (`sync.Sync.sign_changed`): a new
    URL, and a plan that is the same whether or not the person replaced the picture. It still
    counts as replaced - nothing here ever writes over it - but a report must not say the person
    did something nobody looked at."""
    def flag(oid: ObjectId) -> bool:
        o = read.objects.get(oid)
        return o is not None and o.image is not None and o.image.unchecked
    return bool(oids) and all(flag(oid) for oid in oids)


def background_unchecked(read: SlideRead) -> bool:
    """`pictures_unchecked` of the slide's background."""
    return bool((read.background or {}).get("unchecked"))


def background_edited(b: JsonMap, read: JsonMap) -> bool:
    """The deck changed a base slide's background (a picture compares by its pixels: contentUrls change)."""
    seen = b.get("background_readback")
    live = read.get("background")
    return seen is not None and not snapshot.same_background(as_object(seen, "background_readback"),
                                                             None if live is None else as_object(live, "background"))


def background_edited_of(b: SlideEntry, read: SlideRead) -> bool:
    seen = None if b.seen is None else b.seen.background_readback
    return seen is not None and not snapshot.same_background(seen, read.background)


def slide_touched(b: JsonMap, read: JsonMap) -> list[str]:
    """`slide_touched_of` a base slide and a live one as dicts."""
    why: list[str] = []
    if any(deck_edits(e, read) for e in as_objects(b["elements"], "slide.elements")):
        why.append("elements edited")
    left = set(_str_items(b.get("left_alone") or [], "slide.left_alone"))
    if any(o["objectId"] not in left for o in user_objects(b, read)):
        why.append("objects added")
    if b.get("notes_readback", "") != read.get("notes", ""):
        why.append("notes edited")
    if background_edited(b, read):
        why.append("background changed")
    return why


def slide_touched_of(b: SlideEntry, read: SlideRead) -> list[str]:
    """Why a slide counts as edited in the deck (empty: untouched)."""
    why: list[str] = []
    if any(deck_edits_of(e, read) for e in b.elements):
        why.append("elements edited")
    # An adopted deck's slide is full of objects this converter did not make and nobody added:
    # what the pairing could not tie to any element of the source (`adopt.left_alone`). Counting
    # those as "objects added" says the person edited a slide they have not touched since adopt
    # read it - and, since a foreign deck has them nearly everywhere, it says that about the whole
    # deck and hides the sentence below it ("the deck's own").
    left = set(b.left_alone or ())
    if any(o.object_id not in left for o in user_objects_of(b, read)):
        why.append("objects added")
    if ("" if b.seen is None else b.seen.notes_readback) != read.notes:
        why.append("notes edited")
    if background_edited_of(b, read):
        why.append("background changed")
    return why


def deck_scale(base: JsonMap) -> float | None:
    """Deck pt per PDF pt."""
    scale, deck, page = base.get("scale"), base.get("deck_page_size"), base.get("page_size")
    if scale:
        return _number(scale, "base.scale")
    if deck and page:
        return _number(as_array(deck, "base.deck_page_size")[0], "base.deck_page_size") / \
            _number(as_array(page, "base.page_size")[0], "base.page_size")
    return None


def deck_scale_of(base: Base) -> float | None:
    """Deck pt per PDF pt."""
    if base.scale:
        return base.scale
    if base.deck_page_size and base.page_size:
        return base.deck_page_size[0] / base.page_size[0]
    return None


def hold_slide(b: SlideEntry, o: SlideEntry, read: SlideRead, report: Report, j: int, i: int) -> HoldSlide:
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
    report.held.append(HeldEntry(slide=o.key, label=o.label))
    for u in user_objects_of(b, read):
        report.user_objects.append(SlideUserObject(slide=o.key, user=u))
    return HoldSlide(key=o.key, ours=j, base=i, object_id=read.object_id)


def plan_slide(b: SlideEntry, o: SlideEntry, read: SlideRead, report: Report, base: Base, j: int, i: int,
               adopt: Adopter | None, res: Resolutions) -> UpdateSlide:
    skey = o.key
    bu, ou = units_of(b.elements), units_of(o.elements)
    unit_plans: list[PlannedUnit] = []
    for ukey in list(ou) + [k for k in bu if k not in ou]:
        decision = plan_unit(skey, ukey, bu.get(ukey), ou.get(ukey), read, report, deck_scale_of(base), adopt,
                             res, base.origin == ADOPTED)
        unit_plans.append(PlannedUnit(decision=decision, base_members=tuple(m.key for m in bu.get(ukey, [])),
                                      ours_members=tuple(m.key for m in ou.get(ukey, []))))
    # A unit the source removed may live on inside another one (paragraphs joined): if that one
    # stays as the deck has it (a conflict), deleting the removed one would lose its words.
    kept_texts = [" ".join(" ".join(m.fingerprint.text.split()) for m in ou[u.key])
                  for u in unit_plans if isinstance(u.decision, KeepUnit) and u.decision.source and u.key in ou]
    for n, u in enumerate(unit_plans):
        words = " ".join(" ".join(m.fingerprint.text.split()) for m in bu.get(u.key, []))
        if isinstance(u.decision, DeleteUnit) and words and any(words in t for t in kept_texts):
            unit_plans[n] = replace(u, decision=KeptJoined(key=u.key))
            report.applied = [a for a in report.applied if not (a.slide == skey and a.element == u.key)]
            report.conflicts.append(conflict_of(res, skey, u.key, "removed", words, None, words,
                                                "kept: its text moved into an element in conflict", False)[0])
    adopted = {u.decision.object_id for u in unit_plans if isinstance(u.decision, AdoptObject)}
    for user in user_objects_of(b, read):
        if user.object_id not in adopted:  # (an adopted object is the source's element now)
            report.user_objects.append(SlideUserObject(slide=skey, user=user))
    background_written, background = False, None
    # Background: the source's picture or colour unless the deck changed it.
    if b.background != o.background:
        write = True
        if background_edited_of(b, read):
            entry, write = conflict_of(res, skey, None, "background", b.background, o.background, read.background,
                                       "deck kept", True)
            report.conflicts.append(entry)
        if write:
            background_written, background = True, o.background
            report.applied.append(Applied(slide=skey, element=None, fields=("background",), how=None))
    elif background_edited_of(b, read) and not background_unchecked(read):
        report.overrides.append(Override(slide=skey, element=None, fields=("background",)))
    # Speaker notes: text rules.
    notes: str | None = None
    seen_notes = "" if b.seen is None else b.seen.notes_readback
    bn, on, tn = b.notes or "", o.notes or "", read.notes
    deck_notes = seen_notes != tn
    if bn != on:
        if not deck_notes:
            notes = on
            report.applied.append(Applied(slide=skey, element=None, fields=("notes",), how=None))
        else:
            merged, clashes = diff3_of(seen_notes, on, tn)
            if clashes:
                entry, take_ours = conflict_of(res, skey, None, "notes", bn, on, tn, "deck kept", True)
                report.conflicts.append(entry)
                if take_ours:
                    notes = on
                    report.applied.append(Applied(slide=skey, element=None, fields=("notes",), how=None))
            elif merged != tn:
                notes = merged
                report.applied.append(Applied(slide=skey, element=None, fields=("notes",), how="merged"))
    elif deck_notes:
        report.overrides.append(Override(slide=skey, element=None, fields=("notes",)))
    return UpdateSlide(key=skey, ours=j, base=i, object_id=read.object_id, units=tuple(unit_plans),
                       background_written=background_written, background=background, notes=notes)


def has_writes(mplan: JsonMap, live_order: Sequence[str]) -> bool:
    """Whether a merge plan changes the live deck at all (a sync with no changes sends nothing)."""
    for p in as_objects(mplan["slides"], "plan.slides"):
        action = p["action"]
        if action in ("create", "delete"):
            return True
        if action == "update" and (any(u["action"] in ("create", "recreate", "delete", "move")
                                       for u in as_objects(p["units"], "plan.units"))
                                   or p.get("background") or p.get("notes") is not None):
            return True
    final = [x for x in _str_items(mplan["order"], "plan.order") if not x.startswith("new:")]
    current = [s for s in live_order if s in final]
    return current != [s for s in final if s in current]


def _writes_unit(d: UnitDecision) -> bool:
    """Whether a unit's decision changes the deck: made, made again, deleted or moved."""
    if isinstance(d, CreateUnit | Recreate | DeleteUnit | MoveUnit):
        return True
    if isinstance(d, AdoptObject | GoneUnit | KeepRemoved | KeptJoined | KeepUnit | AdoptUnit):
        return False
    assert_never(d)


def _writes_slide(p: SlidePlan) -> bool:
    """Whether a slide's plan changes the deck (`has_writes_of`): created, deleted, or updated with a
    unit that writes, a background written that is one (none written, or an empty name, is none: as
    `has_writes` reads its JSON) or notes."""
    if isinstance(p, CreateSlide | DeleteSlide):
        return True
    if isinstance(p, GoneSlide | KeepRemovedSlide | HoldSlide):
        return False
    if isinstance(p, UpdateSlide):
        return (any(_writes_unit(u.decision) for u in p.units) or (p.background_written and bool(p.background))
                or p.notes is not None)
    assert_never(p)


def has_writes_of(mplan: MergePlan, live_order: Sequence[str]) -> bool:
    """`has_writes` of the plan `plan_merge_of` returns, read from its records: the same answer as
    `has_writes(merge_plan_json(mplan), live_order)`."""
    if any(_writes_slide(p) for p in mplan.slides):
        return True
    final = [x for x in mplan.order if not x.startswith("new:")]
    current = [s for s in live_order if s in final]
    return current != [s for s in final if s in current]


def _out_of_place(was: Sequence[str], now: Sequence[str]) -> list[str]:
    """The items of `now` that are not in the longest run `was` and `now` agree on - the ones
    somebody picked up and put down elsewhere, rather than the ones that drifted because those did."""
    keep: set[str] = set()
    for a, b, n in SequenceMatcher(None, was, now, autojunk=False).get_matching_blocks():
        keep.update(now[b:b + n])
    return [x for x in now if x not in keep]


def _object_id(p: SlidePlan) -> ObjectId | None:
    if isinstance(p, CreateSlide | GoneSlide):
        return None
    return p.object_id


def plan_order(base: Base, theirs: DeckRead, plans: Sequence[SlidePlan], report: Report) -> tuple[list[str], list[SlideKey]]:
    """Final slide order (live ids, "new:<key>" for slides to create) and the keys of slides
    the source moved. The source's order is the ground; a slide the deck itself picked up goes
    back where the deck put it (deck edits win, and a person who drags one slide does not mean to
    freeze the other forty). Kept slides the source removed stay after their base predecessor,
    user-added slides after their live predecessor."""
    live_order: list[str] = [s.object_id for s in theirs.slides]
    base_by_id = {b.object_id: b for b in base.slides}
    deleted = {p.object_id for p in plans if isinstance(p, DeleteSlide)}
    existing = [sid for sid in live_order if sid in base_by_id and sid not in deleted]
    base_rank = {b.object_id: k for k, b in enumerate(base.slides)}
    was = sorted(existing, key=lambda sid: base_rank[ObjectId(sid)])

    by_ours = {p.ours: p for p in plans if isinstance(p, UpdateSlide | HoldSlide | CreateSlide)}
    order: list[str] = [_object_id(by_ours[j]) or f"new:{by_ours[j].key}" for j in sorted(by_ours)]
    source_moved = set(_out_of_place(was, [sid for sid in order if sid in base_rank and sid in existing]))
    # converter slides kept though the source removed them: after their base predecessor
    for p in plans:
        if not isinstance(p, KeepRemovedSlide):
            continue
        k = base_rank[p.object_id]
        prev = next((base.slides[q].object_id for q in range(k - 1, -1, -1)
                     if base.slides[q].object_id in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, p.object_id)
    # slides the deck moved: out of their base order there, so back beside what they follow now.
    # A slide held back (`hold_slide`) is placed the same way, and for the same reason one step
    # further on: its place in the source's order is its place *as that frame*, and which frame it
    # is is the open question. Nothing about it changes until somebody answers that.
    deck_placed = _out_of_place(was, existing)
    for sid in deck_placed + [p.object_id for p in plans if isinstance(p, HoldSlide) and p.object_id not in deck_placed]:
        if sid not in order:
            continue
        order.remove(sid)
        k = live_order.index(sid)
        prev = next((live_order[q] for q in range(k - 1, -1, -1) if live_order[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
        if sid in source_moved and sid in deck_placed:
            report.warnings.append(
                f"slide {base_by_id[ObjectId(sid)].key}: both the source and the deck moved this slide; "
                f"it stays where the deck put it.")
    # user-added slides (and converter slides not otherwise placed): after their live predecessor
    for k, sid in enumerate(live_order):
        if sid in order or sid in deleted:
            continue
        prev = next((live_order[q] for q in range(k - 1, -1, -1) if live_order[q] in order), None)
        order.insert(order.index(prev) + 1 if prev else 0, sid)
    before = [s for s in live_order if s in order]
    after = [s for s in order if s in before]
    moved = [base_by_id[ObjectId(s)].key for s, t in zip(before, after) if s != t and s in base_by_id] \
        if before != after else []
    return order, moved
