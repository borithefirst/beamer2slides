"""Does a synced deck LOOK broken where it did not before?

`loss_oracle` asks whether anything a person put into the deck was lost. This asks the other
question a sync can get wrong while losing nothing: whether it left the slide looking broken - words
over words, a box's text running out of it onto what is below, a formula picture no longer over its
gap, something pushed off the page. Same inputs, same finding shape:

  base    the sync base the sync started from
  before  `snapshot.read_presentation` of the live deck immediately BEFORE the sync
  after   the same, immediately AFTER
  report  the sync report (only read for which slides are uncertain)
  ours    optional: `sync.build_ours` of the new source - what a fresh conversion draws

They come in as JSON and are read as `sync_model` records (`Base`, `DeckRead`, the new
conversion's `SlideEntry`s); a text is laid out into a `text_layout.Layout`, and what each object
puts on the slide is a `Party`. The findings go out as JSON (`Finding`), which is what the fuzzer,
the edit hunt and the replay write.

Only what THIS sync introduced is judged: a look that was already broken in `before` (the
converter's own or the person's) is not the sync's, and neither is one the new conversion draws
itself. Those come back separately, from `existing(before)`, never as failures.

How a text is laid out (`beamer2slides.text_layout`, shared with sync)
----------------------
A read-back says what a text box holds and how big it is, not where Slides puts the lines, and
Slides boxes do not autofit. So the lines are laid out here the way emit predicts them: the
calibrated Slides advances (`emit.ADVANCES`, per family and style; Roboto Mono's 0.6 em), greedy
wrapping at spaces inside the box less its insets, lines `emit.LINE_EM` x lineSpacing apart, the
first baseline `emit.BASELINE_A + ASCENT_EM` under the box top, paragraphs spaced with
`emit.pitch_between` plus their spaceAbove/spaceBelow (none between two bulleted items). The
read-back lists the distinct paragraph styles in the order they first appear, not which paragraph
has which: one style is everybody's, as many as paragraphs is one each, and otherwise the one that
lays the text out *shortest* stands in, so a miss of the model errs towards "fits". Ink is a band
per line, cap height above the baseline and descender below it, as wide as the words - so a long box
whose short lines stop before a picture does not "overlap" it. On 72 unedited converter boxes of the
archive the model predicts every one of 117 paragraphs' PDF line count, and finds each converter-
placed formula picture within 2 pt of its hole.

Kinds (severity "fail" or "note"):

* `text_overlap` - the ink of a text and the ink of another text, a picture or a table (the source
  table's frame where the deck table stands) overlap in `after`,
  by at least `OVERLAP_MIN` pt each way; the same two things did not meet in `before` (the pair is
  named by element key, or objectId for the person's own objects), and the new conversion does not
  draw them meeting either (their PDF ink, `ours`). A formula picture or icon over its own text is
  by design (that is `stranded_picture`'s business).
* `text_overflow` - the same, where the part of the text that meets the other thing is laid out
  *below its own box*: the text no longer fits its box and runs onto what is under it. Also a text
  that runs out of the bottom of the panel it sits on (a block body out of its block), where it did
  not before and the new conversion does not draw it so.
* `stranded_picture` - an anchored formula picture whose hole (its run of no-break Roboto Mono
  spaces) the layout finds more than `STRANDED_X` pt across or `STRANDED_LINES` of a line up or down
  from it. Judged when it was over its hole before the sync, or when the sync wrote the picture
  (created, recreated or moved it) and the person had not put it where it stands.
* `off_page` - ink (or a picture's box) more than `OFF_PAGE` pt outside the page, where the same
  element was on the page in `before` and the new conversion puts it on the page.

Not a kind: a table growing over what is under it. The read-back of a table has its cells' words
but no column widths or row heights (and its box is Slides' 3,000,000 EMU placeholder), so nothing
here can lay a cell out; the IR frame says only what the source's table measured.

A finding is a failure when nothing excuses it; it is a note when the slide is one the sync says it
may have matched wrongly (`loss_oracle.uncertain_slides`), when there is no `ours` to ask about a new
element, or when the overlap is thin: under `NOTE_BELOW` for a picture (its box has margins), under
`OVERLAP_MIN` for two texts (their bands are glyphs).

    python tools/layout_oracle.py <archive dir> [...] [--json] [--out f.json] [--existing] [--notes]
"""

from __future__ import annotations

import collections
import itertools
import json
import re
import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from beamer2slides import emit, merge
from beamer2slides.devtools import loss_oracle
from beamer2slides.json_types import (Json, JsonObject, JsonShapeError, as_array, as_object, as_objects,
                                      as_optional_str, as_str)
from beamer2slides.sync_model import (Base, DeckRead, ElementEntry, ElementKey, ObjectId, ReadBack, SlideEntry,
                                      SlideKey, SlideRead, deck_read, element_entry_json, readback_json,
                                      slide_entry, slide_entry_json, slide_read_json)
from beamer2slides.sync_model import base as parse_base
# the layout model lives in the library now (sync lays out merged text with it); names kept here
from beamer2slides.text_layout import (  # noqa: F401
    CAP_EM, DESC_EM, INSET_X, INSET_Y, NBSP, SOFT_BREAK, TAB_EM, UNKNOWN_EM, Layout, Rect, _para, _style_name,
    advance, char_styles, filled, layout, layout_at, line_rects, meet, para_style, para_styles, rect, text_box,
    upright, wrap)

if TYPE_CHECKING:
    from typing_extensions import Required

Severity = Literal["fail", "note"]
Kind = Literal["text_overlap", "text_overflow", "stranded_picture", "off_page"]
SEVERITIES: tuple[Severity, ...] = ("fail", "note")
FAIL: tuple[Severity, ...] = ("fail",)

REACHED: collections.Counter[str] = collections.Counter()   # what check() saw and what excused it, over a whole run (`replay`)

OVERLAP_MIN = 2.0              # pt each way before two inks count as overlapping
NOTE_BELOW = 4.0                # an overlap with a picture thinner than this (pt, the lesser side) is a note
OFF_PAGE = 6.0                 # pt outside the page

Placement = tuple[float, float, float]
"""How the conversion's pt land in the deck: `x_deck = s*x + tx` (`loss_oracle.deck_placement`)."""


class History(TypedDict, total=False):
    """What happened to one object in a sync (`Pair.history`): `was` "new", "person" or
    "converter"; `how` (not for a new one) "moved", "text", "recreated", "person_moved",
    "person_text"."""
    name: Required[str]
    was: Required[str]
    how: list[str]


class Finding(TypedDict, total=False):
    """One finding, as the fuzzer, the edit hunt and the replay write it. `other`/`other_element`:
    what it met (not for `off_page`); `existing`/`by`: set by `existing` ("converter"|"person")."""
    kind: Required[Kind]
    severity: Required[Severity]
    slide: Required[str]
    element: Required[str | None]
    object: Required[str | None]
    detail: Required[str]
    other: str
    other_element: str | None
    depth: float
    history: list[History]
    existing: bool
    by: str


def finding(kind: Kind, severity: Severity, detail: str, slide: str, element: str | None, object: str | None,
            other: tuple[str, str | None] | None, depth: float, history: list[History]) -> Finding:
    out: Finding = {"kind": kind, "severity": severity, "slide": slide, "element": element, "object": object,
                    "detail": detail}
    if other is not None:
        out["other"], out["other_element"] = other
    out["depth"] = depth
    out["history"] = history
    return out


def _number(v: Json, where: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{where}: a number was expected, found {type(v).__name__}")


def _frame(ir: JsonObject | None) -> Rect | None:
    """An element's frame in the conversion: its IR's `frame`, else its `bbox` (None: neither)."""
    if not ir:
        return None
    v = ir.get("frame") or ir.get("bbox")
    return rect(v, "ir.frame") if v else None


# ---------------------------------------------------------------- what is drawn where on a slide

def _size_hint(el: ElementEntry | None, scale: float) -> list[float] | None:
    """A text's sizes where its read-back has none (a title placeholder inherits its layout's): each
    paragraph's largest run in the element's IR, at the conversion's scale."""
    ir: JsonObject = el.ir if el is not None and el.ir else {}
    out: list[float] = []
    for p in as_objects(ir.get("paragraphs") or [], "ir.paragraphs"):
        runs: list[JsonObject] = as_objects(p["runs"], "paragraph.runs") if p.get("runs") else []
        sizes = [_number(r.get("size") or p.get("size") or 0.0, "run.size") for r in runs] or \
            [_number(p.get("size") or 0.0, "paragraph.size")]
        out.append(max(sizes, default=0.0) * scale)
    return out if out and all(out) else None


PartyKind = Literal["picture", "text", "table"]


@dataclass(frozen=True, kw_only=True)
class Party:
    """What one object puts on the slide: its ink as rectangles, whether each runs below the
    object's own box (`beyond`), and a text's `layout`."""
    kind: PartyKind
    rects: tuple[Rect, ...]
    beyond: tuple[bool, ...]
    layout: Layout | None


def element_of(skey: str, base_slide: SlideEntry | None, read: SlideRead,
               ours_slide: Sequence[ElementEntry] | None) -> dict[ObjectId, ElementEntry]:
    """`loss_oracle`'s object id -> the element it carries (a base element, or the new conversion's
    for an object the sync just made), over records: asked of their JSON, and each answer taken
    back to the record it was written from."""
    base_els: list[Json] = [element_entry_json(e) for e in base_slide.elements] if base_slide is not None else []
    ours_els: list[JsonObject] = [element_entry_json(e) for e in ours_slide] if ours_slide is not None else []
    base_json: JsonObject | None = None if base_slide is None else {**slide_entry_json(base_slide), "elements": base_els}
    found = loss_oracle._element_of(skey, base_json, slide_read_json(read), None if ours_slide is None else ours_els)
    made: list[tuple[Json, ElementEntry]] = list(zip(base_els, base_slide.elements if base_slide is not None else ()))
    made += list(zip(ours_els, ours_slide or ()))
    out: dict[ObjectId, ElementEntry] = {}
    for oid, el in found.items():
        out[ObjectId(oid)] = next((r for j, r in made if j is el), None) or next(r for j, r in made if j == el)
    return out


class View:
    """One slide read-back as this oracle sees it: each object named (`el:<element key>` for the
    converter's, `obj:<objectId>` for the person's own) and each party's ink as rectangles."""

    def __init__(self, skey: str, read: SlideRead, base_slide: SlideEntry | None,
                 ours_slide: Sequence[ElementEntry] | None, user: Set[str], scale: float,
                 place: Placement | None) -> None:
        self.skey, self.read = skey, read
        els = element_of(skey, base_slide, read, ours_slide)
        self.el: dict[ObjectId, ElementEntry] = {oid: e for oid, e in els.items() if oid not in user}
        self.name: dict[ObjectId, str] = {oid: (f"el:{self.el[oid].key}" if oid in self.el else f"obj:{oid}")
                                          for oid in read.objects}
        self.parties: dict[ObjectId, Party] = {}
        self.panels: dict[ObjectId, Rect] = {}
        for oid, rb in read.objects.items():
            rbj = readback_json(rb)
            if not rb.box or not upright(rbj):
                continue
            el = self.el.get(oid)
            if rb.kind == "image":
                self.parties[oid] = Party(kind="picture", rects=(rb.box,), beyond=(False,), layout=None)
            elif text_box(rbj):
                lay = layout_at(rbj, _size_hint(el, scale))
                if lay is None:
                    continue
                rects = tuple(line_rects(lay))
                self.parties[oid] = Party(kind="text", rects=rects, beyond=tuple(r[3] > rb.box[3] + 1.0 for r in rects),
                                          layout=lay)
            elif rb.kind == "table" and el is not None and place is not None:
                frame = _frame(el.ir)
                if frame:
                    s = place[0]
                    x, y = rb.transform[4], rb.transform[5]
                    self.parties[oid] = Party(kind="table", rects=((x, y, x + s * (frame[2] - frame[0]),
                                                                    y + s * (frame[3] - frame[1])),),
                                              beyond=(False,), layout=None)
            elif filled(rbj):
                self.panels[oid] = rb.box

    def of(self, name: str) -> list[ObjectId]:
        return [oid for oid, n in self.name.items() if n == name]

    def key(self, oid: ObjectId) -> ElementKey | None:
        e = self.el.get(oid)
        return e.key if e is not None else None


def anchored(va: View, a: ObjectId, b: ObjectId) -> bool:
    """`b` is a picture the converter anchors to `a`'s element (a formula over its hole, a bullet
    icon beside its line), or the other way round."""
    ea, eb = va.el.get(a), va.el.get(b)
    if ea is None or eb is None:
        return False
    return eb.anchor == ea.key or ea.anchor == eb.key or (bool(ea.anchor) and ea.anchor == eb.anchor)


def ours_inks(ours_slide: Sequence[ElementEntry] | None, place: Placement | None) -> dict[ElementKey, list[Rect]]:
    """Element key -> the ink the new conversion draws for it (PDF lines and pictures, in deck pt)."""
    if not ours_slide or place is None:
        return {}
    s, tx, ty = place
    out: dict[ElementKey, list[Rect]] = {}
    for el in ours_slide:
        ir = el.ir or {}
        rects: list[Rect] = []
        if el.kind == "text" and ir.get("paragraphs"):
            for p in as_objects(ir["paragraphs"], "ir.paragraphs"):
                for ln in as_objects(p.get("lines") or [], "paragraph.lines"):
                    size = _number(p["size"], "paragraph.size")
                    baseline = _number(ln["baseline"], "line.baseline")
                    rects.append((_number(ln["x0"], "line.x0"), baseline - CAP_EM * size, _number(ln["x1"], "line.x1"),
                                  baseline + DESC_EM * size))
                bullet = p.get("bullet")
                if bullet:
                    bbox = as_object(bullet, "paragraph.bullet").get("bbox")
                    if bbox:
                        rects.append(rect(bbox, "bullet.bbox"))
        else:
            bb = _frame(ir) or el.fingerprint.bbox
            if bb:
                rects.append(bb)
        out[el.key] = [(s * r[0] + tx, s * r[1] + ty, s * r[2] + tx, s * r[3] + ty) for r in rects]
    return out


# ---------------------------------------------------------------- one slide, before and after

class Pair:
    """A slide as the sync found it and as it left it."""

    def __init__(self, skey: str, base_slide: SlideEntry | None, before: SlideRead | None, after: SlideRead,
                 ours_slide: Sequence[ElementEntry] | None, have_ours: bool, unsure: bool, scale: float,
                 place: Placement | None, page: Sequence[float]) -> None:
        self.skey, self.base_slide, self.page = skey, base_slide, page
        self.ours_slide, self.place = ours_slide, place
        self.told: Set[str] = set()    # the person's objects the report says the source ran over
        user: set[str]
        if before is None:
            user = set()
        elif base_slide is None:
            user = set(before.objects)       # a slide the person added: all theirs
        else:
            user = {u.object_id for u in merge.user_objects_of(base_slide, before)}
        self.va = View(skey, after, base_slide, ours_slide, user, scale, place)
        self.vb = View(skey, before, base_slide, None, user, scale, place) if before is not None else None
        self.inks = ours_inks(ours_slide, place)
        self.have_ours, self.unsure = have_ours and ours_slide is not None, unsure
        self.reached: collections.Counter[str] = collections.Counter()

    def before_parties(self, name: str) -> list[Party] | None:
        """The parties carrying `name` before the sync; None when nothing did (a new element)."""
        if self.vb is None:
            return None
        oids = self.vb.of(name)
        if not oids:
            return None
        return [self.vb.parties[o] for o in oids if o in self.vb.parties]

    def ours_rects(self, oid: ObjectId) -> list[Rect] | None:
        k = self.va.key(oid)
        return self.inks.get(k) if k else None

    def source_shift(self, oid: ObjectId) -> tuple[float, float] | None:
        """How far the source moved `oid`'s element (its IR frame, base -> ours, deck pt); None when
        either side has no frame for it."""
        k = self.va.key(oid)
        if not k or self.base_slide is None or not self.ours_slide or self.place is None:
            return None

        def frame(els: Iterable[ElementEntry]) -> Rect | None:
            e = next((e for e in els if e.key == k), None)
            return _frame(e.ir if e is not None else None)
        fb, fo = frame(self.base_slide.elements), frame(self.ours_slide)
        if not fb or not fo:
            return None
        s = self.place[0]
        return s * (fo[0] - fb[0]), s * (fo[1] - fb[1])

    def carried_rects(self, oid: ObjectId) -> list[Rect] | None:
        """Where carrying the deck as it was onto the source's move puts `oid`'s ink: its `before`
        ink moved as the source moved its element (a person's own object: not at all). What sync
        writes for a unit the person moved (`sync.carried`), in this oracle's own layout model."""
        olds = self.before_parties(self.va.name[oid])
        if not olds:
            return None
        d = (0.0, 0.0) if self.va.name[oid].startswith("obj:") else self.source_shift(oid)
        if d is None:
            return None
        return [(r[0] + d[0], r[1] + d[1], r[2] + d[0], r[3] + d[1]) for p in olds for r in p.rects]

    def history(self, oid: ObjectId) -> History:
        """What happened to one object in this sync, for the finding's reader (and the fuzzer)."""
        name = self.va.name[oid]
        rb = self.va.read.objects[oid]
        vb = self.vb
        olds: list[ReadBack] = [vb.read.objects[o] for o in vb.of(name)] if vb is not None else []
        if not olds or vb is None:
            return {"name": name, "was": "new"}
        old = olds[0]
        how: list[str] = []
        if not loss_oracle.same_box(readback_json(old), readback_json(rb)):
            how.append("moved")
        if loss_oracle.norm(old.text) != loss_oracle.norm(rb.text):
            how.append("text")
        if oid not in vb.read.objects:
            how.append("recreated")
        base_rb: ReadBack | None = None
        if self.base_slide is not None and name.startswith("el:"):
            el = next((e for e in self.base_slide.elements if e.key == name[3:]), None)
            base_rb = el.readback.get(el.main) if el is not None and el.main is not None else None
        if base_rb is not None and old.box and not loss_oracle.same_box(readback_json(base_rb), readback_json(old)):
            how.append("person_moved")
        if base_rb is not None and loss_oracle.norm(base_rb.text) != loss_oracle.norm(old.text):
            how.append("person_text")
        return {"name": name, "was": "person" if name.startswith("obj:") else "converter", "how": how}

    def severity(self, depth: float, needs_ours: bool, least: float | None) -> tuple[Severity, str]:
        if self.unsure:
            return "note", " - on a slide the report says may be the wrong one"
        if needs_ours and not self.have_ours:
            return "note", " - no new conversion to ask whether it draws it so"
        if depth < (NOTE_BELOW if least is None else least):
            return "note", f" - by {depth:.1f} pt only"
        return "fail", ""


def _text(rb: ReadBack) -> str:
    return loss_oracle.norm(rb.text)[:50]


def overlap_findings(sp: Pair) -> list[Finding]:
    """Two inks that meet after the sync, not before it, and not in the new conversion."""
    out: list[Finding] = []
    va = sp.va
    done: set[tuple[ObjectId, ObjectId]] = set()
    for a, pa in va.parties.items():
        if pa.kind != "text":
            continue
        for b, pb in va.parties.items():
            if b == a or (b, a) in done:
                continue
            done.add((a, b))
            na, nb = va.name[a], va.name[b]
            if na == nb or anchored(va, a, b):
                continue
            m = meet(pa.rects, pb.rects, OVERLAP_MIN)
            if not m:
                continue
            depth, i, j = m
            sp.reached["overlap"] += 1
            olds_a, olds_b = sp.before_parties(na), sp.before_parties(nb)
            if olds_a is not None and olds_b is not None:
                if not olds_a or not olds_b:
                    sp.reached["overlap: not laid out before"] += 1
                    continue  # there before, but not laid out then: nothing to compare with
                if any(meet(x.rects, y.rects, 0.5) for x in olds_a for y in olds_b):
                    # they met before the sync already. Deliberately also when they now meet
                    # deeper: in the archive that is a person's copy laid over the original and the
                    # original gaining a line - broken before, by the person, not by the sync.
                    sp.reached["overlap: met before"] += 1
                    continue
            ra, rb_ = sp.ours_rects(a), sp.ours_rects(b)
            if ra and rb_ and meet(ra, rb_, 0.5):
                sp.reached["overlap: the conversion draws it"] += 1
                continue  # the new conversion draws them so
            hist = [sp.history(a), sp.history(b)]
            if all(h["was"] == "converter" for h in hist) and any("person_moved" in h.get("how", []) for h in hist):
                ca, cb = sp.carried_rects(a), sp.carried_rects(b)
                if ca and cb and meet(ca, cb, 0.5):
                    # the person's own arrangement carried onto the source's moves, as sync must:
                    # live fuzz r7413, a paragraph the person moved up onto the title, which the
                    # source moved away and then back.
                    sp.reached["overlap: the person's move draws it"] += 1
                    continue
            sp.reached["overlap: judged"] += 1
            new = olds_a is None or olds_b is None
            beyond = pa.beyond[i] or (pb.kind == "text" and pb.beyond[j])
            # two texts' ink bands are glyphs (a picture's box has margins): 2 pt of them is touching
            sev, why = sp.severity(depth, new, OVERLAP_MIN if pb.kind == "text" else None)
            if sev == "fail" and {a, b} & sp.told:
                # the source ran over the person's own object, which sync never moves: the report
                # says so (`sync.warn_about_overruns`), and that is all it can do
                sev, why = "note", " - the report says so"
                sp.reached["overlap: reported overrun"] += 1
            kind: Kind = "text_overflow" if beyond else "text_overlap"
            ra_, rb2 = va.read.objects[a], va.read.objects[b]
            detail = (f"{_text(ra_)!r} and {pb.kind} {(_text(rb2) or b)!r} overlap by {depth:.1f} pt"
                      + (" - the text runs out of its box onto it" if beyond else "") + why)
            out.append(finding(kind, sev, detail, sp.skey, va.key(a), a, (b, va.key(b)), round(depth, 1),
                               [sp.history(a), sp.history(b)]))
    return out


PANEL_SLACK = 3.0   # pt: a text box counts as sitting on a panel when it is inside it by this much


def panel_findings(sp: Pair) -> list[Finding]:
    """A text that runs out of the bottom of the panel it sits on (a block's body out of its block)."""
    out: list[Finding] = []
    va = sp.va

    def spill(view: View, text_oid: ObjectId, panel_oid: ObjectId) -> float:
        return max(r[3] for r in view.parties[text_oid].rects) - view.panels[panel_oid][3]

    for t, pt in va.parties.items():
        if pt.kind != "text" or not pt.rects:
            continue
        box = va.read.objects[t].box
        left, right = min(r[0] for r in pt.rects), max(r[2] for r in pt.rects)
        for p, pbox in va.panels.items():
            # sits on it: its words starting inside the panel and mostly across it (a box, and a
            # line wrapped at its inset, may reach past a panel), its top inside the panel, its
            # box ending by the panel's bottom
            across = min(right, pbox[2]) - max(left, pbox[0])
            if not (pbox[0] - PANEL_SLACK <= left and across >= 0.8 * (right - left)
                    and pbox[1] - PANEL_SLACK <= box[1] < pbox[3] - 2 and box[3] <= pbox[3] + PANEL_SLACK):
                continue
            by = spill(va, t, p)
            if by < OVERLAP_MIN:
                continue
            sp.reached["panel"] += 1
            nt, np_ = va.name[t], va.name[p]
            vb = sp.vb
            if vb is not None and vb.of(nt) and vb.of(np_):
                was = max((spill(vb, x, y) for x in vb.of(nt) for y in vb.of(np_)
                           if x in vb.parties and y in vb.panels), default=None)
                if was is not None and was > 0.5:
                    sp.reached["panel: out before"] += 1
                    continue  # it ran out of that panel before the sync already
            rt, rp = sp.ours_rects(t), sp.ours_rects(p)
            if rt and rp and max(r[3] for r in rt) > max(r[3] for r in rp) + 0.5:
                sp.reached["panel: the conversion draws it"] += 1
                continue  # the new conversion draws it past the panel
            sp.reached["panel: judged"] += 1
            new = not (vb is not None and vb.of(nt) and vb.of(np_))
            sev, why = sp.severity(by, new, None)
            out.append(finding("text_overflow", sev, f"{_text(va.read.objects[t])!r} runs {by:.1f} pt out of the "
                               f"bottom of the panel it sits on" + why, sp.skey, va.key(t), t, (p, va.key(p)),
                               round(by, 1), [sp.history(t), sp.history(p)]))
    return out


def off_page_findings(sp: Pair) -> list[Finding]:
    out: list[Finding] = []
    va = sp.va
    w, h = sp.page[0], sp.page[1]

    def outside(rects: Sequence[Rect]) -> float:
        return max(max(-r[0], r[2] - w, -r[1], r[3] - h) for r in rects) if rects else 0.0

    for oid, p in va.parties.items():
        by = outside(p.rects)
        if by <= OFF_PAGE:
            continue
        sp.reached["off_page"] += 1
        olds = sp.before_parties(va.name[oid])
        if olds and any(outside(x.rects) > OFF_PAGE / 2 for x in olds):
            # off the page before already (the person's: a footer dragged to the edge that then
            # gains a digit is where the person put it, not where the sync did)
            sp.reached["off_page: before"] += 1
            continue
        if olds is not None and not olds:
            sp.reached["off_page: not laid out before"] += 1
            continue
        ours = sp.ours_rects(oid)
        if ours and outside(ours) > OFF_PAGE / 2:
            sp.reached["off_page: the conversion draws it"] += 1
            continue
        sp.reached["off_page: judged"] += 1
        sev, why = sp.severity(by, olds is None, None)
        out.append(finding("off_page", sev, f"{p.kind} {(_text(va.read.objects[oid]) or oid)!r} reaches "
                           f"{by:.1f} pt past the page edge" + why, sp.skey, va.key(oid), oid, None,
                           round(by, 1), [sp.history(oid)]))
    return out


STRANDED_X = 8.0      # pt: a formula picture this far beside its hole is not over it
STRANDED_LINES = 0.6  # of a line: this far above or below

Offset = tuple[float, float, float]
"""A picture's (dx, dy) from its hole, and the size of the hole's line."""


def hole_offsets(view: View, text_oid: ObjectId) -> dict[ObjectId, Offset]:
    """Picture object -> (dx, dy, line size) from the hole the layout pairs it with, for the
    formula pictures anchored to that text (the cheapest assignment of pictures to holes)."""
    p = view.parties.get(text_oid)
    key = view.key(text_oid)
    if p is None or not key or p.kind != "text" or p.layout is None:
        return {}
    lay = p.layout
    holes = lay.holes
    pics = [o for o, q in view.parties.items() if q.kind == "picture" and o in view.el
            and view.el[o].anchor == key and view.el[o].role == "math"]
    if not pics or not holes:
        return {}

    def off(o: ObjectId, hole_at: int) -> Offset:
        hole = holes[hole_at]
        b, hb = view.read.objects[o].box, hole.box
        return ((b[0] + b[2]) / 2 - (hb[0] + hb[2]) / 2, (b[1] + b[3]) / 2 - (hb[1] + hb[3]) / 2,
                lay.lines[hole.line].size)

    def cost(o: ObjectId, hole_at: int) -> float:
        dx, dy, z = off(o, hole_at)
        return abs(dx) + abs(dy)
    if len(pics) <= 6 and len(holes) <= 8:
        # more pictures than holes (the person deleted words holding one): which pictures get the
        # holes is part of the choice - a picture left over has no hole to be judged against
        best: tuple[float, list[tuple[ObjectId, int]]] | None = None
        choices: Iterator[list[tuple[ObjectId, int]]]
        if len(pics) <= len(holes):
            choices = (list(zip(pics, perm)) for perm in itertools.permutations(range(len(holes)), len(pics)))
        else:
            choices = (list(zip((pics[i] for i in perm), range(len(holes))))
                       for perm in itertools.permutations(range(len(pics)), len(holes)))
        for pairs_ in choices:
            c = sum(cost(o, k) for o, k in pairs_)
            if best is None or c < best[0]:
                best = (c, pairs_)
        return {} if best is None else {o: off(o, k) for o, k in best[1]}
    return {o: off(o, min(range(len(holes)), key=lambda hh: cost(o, hh))) for o in pics}


def stranded(dx: float, dy: float, z: float) -> bool:
    return abs(dx) > STRANDED_X or abs(dy) > STRANDED_LINES * emit.LINE_EM * z


def stranded_findings(sp: Pair) -> list[Finding]:
    """A formula picture that is not over its hole after the sync. On what the converter placed the
    layout finds the hole within 2 pt of its picture (72 archived placements), so the absolute
    measure stands. Judged when it was over its hole before the sync, or when this sync wrote the
    picture (created, recreated or moved it): a sync that puts a picture down owes it its hole,
    even one the person's words had already pushed away (`history` says which) - unless the
    person had put the picture where it stands themselves (moved or resized off the base)."""
    out: list[Finding] = []
    va = sp.va
    for t in va.parties:
        offs = hole_offsets(va, t)
        if not offs:
            continue
        olds: dict[str, Offset] = {}
        vb = sp.vb
        if vb is not None:
            for x in vb.of(va.name[t]):
                olds.update({vb.name[o]: v for o, v in hole_offsets(vb, x).items()})
        for pic, (dx, dy, z) in offs.items():
            if not stranded(dx, dy, z):
                continue
            sp.reached["stranded"] += 1
            was = olds.get(va.name[pic])
            h = sp.history(pic)
            how = set(h.get("how") or ())
            wrote = h["was"] == "new" or bool({"moved", "recreated"} & how)
            if was is not None and stranded(*was) and not wrote:
                sp.reached["stranded: before, untouched"] += 1
                continue  # already off before, and this sync did not put it down
            if was is not None and stranded(*was) and "person_moved" in how:
                sp.reached["stranded: before, the person's place"] += 1
                continue  # off before where the person put it; deck edits win, so it stays there
            if was is None and not wrote and vb is not None and vb.of(va.name[pic]):
                sp.reached["stranded: no hole before"] += 1
                continue  # the picture was there, but no hole in that text to measure it by
            sp.reached["stranded: judged"] += 1
            sev, why = sp.severity(max(abs(dx), abs(dy)), was is None, None)
            if was is not None and stranded(*was):
                why = f" - it was {was[0]:+.1f} / {was[1]:+.1f} pt off before, and the sync rewrote it there" + why
            out.append(finding("stranded_picture", sev,
                               f"the formula picture is {dx:+.1f} pt across and {dy:+.1f} pt down from its hole in "
                               f"{_text(va.read.objects[t])!r}" + why, sp.skey, va.key(pic), pic, (t, va.key(t)),
                               round(max(abs(dx), abs(dy)), 1), [h, sp.history(t)]))
    return out


# ---------------------------------------------------------------- the oracle

def _slide_key(slide: SlideRead, base_by_id: Mapping[ObjectId, SlideEntry], ours_keys: Sequence[SlideKey]) -> str:
    """The base's key of a slide, or - for one this sync created - the new conversion's, read off
    the ids sync gives what it creates (`b2s_<h6 slide>_...`)."""
    b = base_by_id.get(slide.object_id)
    if b is not None:
        return b.key
    for k in ours_keys:
        prefix = f"b2s_{loss_oracle.h6(k)}_"
        if any(o.startswith(prefix) for o in slide.objects):
            return k
    return slide.object_id


def ours_slides(ours: JsonObject | None) -> tuple[SlideEntry, ...]:
    """The new conversion's slide entries (none without one)."""
    if ours is None:
        return ()
    return tuple(slide_entry(s, f"the new conversion.slides[{n}]")
                 for n, s in enumerate(as_array(ours.get("slides", []), "the new conversion.slides")))


def pairs(base: JsonObject, before: JsonObject | None, after: JsonObject, report: JsonObject | None,
          ours: JsonObject | None) -> Iterator[Pair]:
    """One `Pair` per slide of `after` (before=None: every slide judged on its own)."""
    b: Base = parse_base(base)
    was: DeckRead | None = deck_read(before) if before else None
    now: DeckRead = deck_read(after)
    unsure = loss_oracle.uncertain_slides(base, ours)
    # A converted deck is the PDF at `scale` from the page's corner (emit); a base without one
    # (the offline fuzz world's) is read off its own boxes.
    place: Placement | None = (b.scale, 0.0, 0.0) if b.scale else loss_oracle.deck_placement(base)
    scale = b.scale or (place[0] if place else 1.0)
    page: Sequence[float] = now.page_size or b.deck_page_size or (720.0, 405.0)
    base_by_id: dict[ObjectId, SlideEntry] = {oid: s for s in b.slides if (oid := s.object_id)}
    before_by_id = {s.object_id: s for s in (was.slides if was is not None else ())}
    ours_by_key = {s.key: s.elements for s in ours_slides(ours)}
    told = {as_str(o["object"], "report.overruns: object")
            for o in as_objects((report or {}).get("overruns") or [], "report.overruns")}
    for a in now.slides:
        skey = _slide_key(a, base_by_id, list(ours_by_key))
        sp = Pair(skey, base_by_id.get(a.object_id), before_by_id.get(a.object_id) if was is not None else None, a,
                  ours_by_key.get(SlideKey(skey)), ours is not None, skey in unsure, scale, place, page)
        sp.told = told
        yield sp


def check(base: JsonObject, before: JsonObject | None, after: JsonObject, report: JsonObject | None,
          ours: JsonObject | None) -> list[Finding]:
    """Findings of one sync: what looks broken after it that did not before it and that the new
    conversion does not draw (`allowed` leaves out the kinds a caller expects)."""
    out: list[Finding] = []
    ours_keys = {s.key for s in ours_slides(ours)}
    for sp in pairs(base, before, after, report, ours):
        if sp.vb is None and sp.skey not in ours_keys:
            continue  # a slide nobody can say anything about (the person's, created meanwhile?)
        out += overlap_findings(sp) + panel_findings(sp) + off_page_findings(sp) + stranded_findings(sp)
        REACHED.update(sp.reached)
    return out


def allowed(findings: Sequence[Finding], allow: Collection[str]) -> list[Finding]:
    """`findings` less the ones `allow` names: kinds, "<kind>/<slide>" or "<kind>/<slide>/<element>"."""
    names = set(allow)
    return [f for f in findings if not ({f["kind"], f"{f['kind']}/{f['slide']}", f"{f['kind']}/{f['slide']}/{f['element']}"}
                                        & names)]


def existing(base: JsonObject, before: JsonObject) -> list[Finding]:
    """What already looks broken in `before`: the same kinds, every one a note, with `by` saying
    whose it is - "converter" when every object involved still is as the base wrote it, "person"
    otherwise. Nothing here is the sync's doing."""
    out: list[Finding] = []
    for sp in pairs(base, None, before, None, None):
        sp.have_ours = True   # "no conversion to ask" is not the question here
        sp.unsure = False
        for f in overlap_findings(sp) + panel_findings(sp) + off_page_findings(sp) + stranded_findings(sp):
            f["severity"] = "note"
            f["existing"] = True
            f["by"] = "converter" if all(_as_written(sp, o) for o in (f.get("object"), f.get("other")) if o) else "person"
            out.append(f)
    return out


def _as_written(sp: Pair, oid: str) -> bool:
    if sp.base_slide is None:
        return False
    for el in sp.base_slide.elements:
        rb = el.readback.get(ObjectId(oid))
        if rb is not None:
            now = sp.va.read.objects.get(ObjectId(oid))
            return now is not None and loss_oracle.same_box(readback_json(rb), readback_json(now)) and \
                loss_oracle.norm(rb.text) == loss_oracle.norm(now.text) and \
                rb.text_style_hash == now.text_style_hash
    return False


def failures(findings: Sequence[Finding]) -> list[Finding]:
    return [f for f in findings if f["severity"] in FAIL]


def describe(findings: Sequence[Finding]) -> str:
    return "\n".join(f"  [{f['severity']}] {f['kind']} {f['slide']}"
                     + (f" / {f['element']}" if f.get("element") else "")
                     + (f" ({f['object']})" if f.get("object") else "") + f": {f['detail']}" for f in findings)


# ---------------------------------------------------------------- replaying an archive of fuzz steps

def ours_from_folder(folder: Path, base: JsonObject) -> JsonObject | None:
    """`sync.build_ours` from the `deck.json` a live fuzz step left in `<step>/ours`, without the
    PDF: the classify the sync used, keyed against this base the way build_ours keys it (the
    extract, classify and render halves are what the folder already holds). Its `pairs` and
    `weak_pairs` are keyed by the index as a string, as the JSON of one would be."""
    from beamer2slides import identity, snapshot
    from beamer2slides.emit import SLIDE_W, DeckPlan, merge_blocks
    from beamer2slides.sync import mark_emitted
    path = folder / "deck.json"
    if not path.exists():
        return None
    source = as_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    merged: list[Json] = []
    for s in as_objects(source["slides"], "deck.slides"):
        blocks: list[Json] = [e for e in merge_blocks(as_objects(s["elements"], "slide.elements"))]
        merged.append({**s, "elements": blocks})
    plan = DeckPlan({**source, "slides": merged}, SLIDE_W, pptx_tables=False, contain=False)
    deck, slides = plan.deck, plan.slides()
    infos = [identity.slide_info_of(s) for s in slides]
    base_slides = as_objects(base["slides"], "base.slides")
    base_keys = [as_str(b["key"], "base slide key") for b in base_slides]
    base_infos = [identity.base_slide_info(b, k) for b, k in zip(base_slides, base_keys)]
    found = identity.label_moves_of(base_infos, infos)
    moves: list[Json] = [identity.reported_move(m, base_keys, infos) for m in found]
    weak: dict[int, str] = {}
    keys, pairs_ = identity.inherit_slide_keys(base_infos, base_keys, infos, found, weak)
    ekeys: list[list[ElementKey]] = []
    fps: list[list[JsonObject]] = []
    for j, slide in enumerate(slides):
        matched = identity.base_items(as_objects(base_slides[pairs_[j]]["elements"], "base slide elements")) \
            if j in pairs_ else None
        k, f = identity.slide_element_keys(as_objects(slide["elements"], "elements"), folder, matched)
        ekeys.append(k)
        fps.append(f)
    entries = snapshot.slide_entries(deck, folder, keys, ekeys, fps)
    mark_emitted(base, entries, deck, pairs_, plan.scale, plan.fonts, fast=True, unread=[])
    ours_entries: list[Json] = [as_object(e, "ours entry") for e in entries]
    return {"slides": ours_entries, "pairs": {str(i): b for i, b in pairs_.items()}, "label_moves": moves,
            "weak_pairs": {str(i): how for i, how in weak.items()}}


def steps_under(paths: Sequence[Path]) -> list[Path]:
    """Every recorded step folder (base, before and after read-backs) at or under these paths."""
    out: list[Path] = []
    for p in paths:
        if (p / "after.json").exists():
            out.append(p)
            continue
        out += sorted({q.parent for q in p.rglob("after.json") if (q.parent / "before.json").exists()
                       and (q.parent / "base.json").exists()}, key=lambda q: (str(q.parent), _num(q.name)))
    return out


def _num(name: str) -> int:
    m = re.search(r"\d+", name)
    return int(m.group()) if m else 0


def _load(path: Path) -> Json:
    """The JSON of a file (None: there is none)."""
    if not path.exists():
        return None
    data: Json = json.loads(path.read_text(encoding="utf-8"))
    return data


def _loaded(path: Path) -> JsonObject | None:
    data = _load(path)
    return None if data is None else as_object(data, str(path))


def edit_summary(spec: JsonObject) -> str:
    """One deck edit as `kind` plus what it aimed at, short."""
    args = as_object(spec.get("args") or {}, "edit.args")
    target = args.get("target") or {}
    what = ",".join(f"{k}={str(v)[:24]}" for k, v in target.items()) if isinstance(target, dict) else str(target)[:24]
    return _edit_kind(spec) + (f"[{what}]" if what else "")


def _edit_kind(spec: JsonObject) -> str:
    kind = spec.get("edit", "?")
    return kind if isinstance(kind, str) else str(kind)


class Step(TypedDict):
    """The oracle over one recorded step (`replay_step`)."""
    round: str
    archive: str
    step: int
    folder: str
    variant: str | None
    edits: list[str]
    edit_kinds: list[str]
    ours: bool
    findings: list[Finding]
    existing: list[Finding]


def replay_step(step: Path, want_ours: bool) -> Step:
    """The oracle over one recorded step."""
    base, before, after = _loaded(step / "base.json"), _loaded(step / "before.json"), _loaded(step / "after.json")
    if base is None or before is None or after is None:
        raise FileNotFoundError(f"{step}: a step needs base.json, before.json and after.json")
    report = _loaded(step / "report.json")
    rnd = _loaded(step.parent / "round.json") or {}
    n = _num(step.name)
    none: JsonObject = {}
    rec = next((s for s in as_objects(rnd.get("steps", []), "round.steps") if s.get("step") == n), none)
    edits = as_objects(_load(step / "edits.json") or rec.get("edits") or [], "edits")
    ours = None
    if want_ours:
        try:
            ours = ours_from_folder(step / "ours", base)
        except ImportError:
            raise  # (our own code moved: never a property of the archived step)
        except Exception as e:  # noqa: BLE001 (an archived conversion today's code cannot key)
            print(f"{step}: no ours ({type(e).__name__}: {e})", file=sys.stderr)
    return {"round": step.parent.name, "archive": step.parent.parent.name, "step": n, "folder": str(step),
            "variant": as_optional_str(rec.get("variant"), "step.variant"), "edits": [edit_summary(e) for e in edits],
            "edit_kinds": sorted({_edit_kind(e) for e in edits}), "ours": ours is not None,
            "findings": check(base, before, after, report, ours), "existing": existing(base, before)}


class Row(TypedDict, total=False):
    """One edit kind's or variant's line of `correlate`: `rate` and `lift` are added last."""
    steps: Required[int]
    with_finding: Required[int]
    kinds: Required[dict[str, int]]
    rate: float
    lift: float | None


def correlate(results: Sequence[Step], level: Severity) -> dict[str, dict[str, Row]]:
    """Which deck edit kinds and source variants precede a finding: per edit kind and per variant,
    how many steps had it and how many of those had a finding of `level` (and of each kind)."""
    def table(of: Callable[[Step], Sequence[str]]) -> dict[str, Row]:
        out: dict[str, Row] = {}
        for r in results:
            hit = [f for f in r["findings"] if f["severity"] == level]
            for x in of(r):
                row = out.setdefault(x, {"steps": 0, "with_finding": 0, "kinds": {}})
                row["steps"] += 1
                if hit:
                    row["with_finding"] += 1
                for k in sorted({f["kind"] for f in hit}):
                    row["kinds"][k] = row["kinds"].get(k, 0) + 1
        overall = sum(1 for r in results if any(f["severity"] == level for f in r["findings"])) / max(1, len(results))
        for row in out.values():
            row["rate"] = round(row["with_finding"] / row["steps"], 3)
            row["lift"] = round(row["rate"] / overall, 2) if overall else None
        return dict(sorted(out.items(), key=lambda kv: (-(kv[1].get("lift") or 0), -kv[1]["steps"])))
    return {"edit_kinds": table(lambda r: r["edit_kinds"]), "variants": table(lambda r: [r["variant"] or "?"])}


def counts(results: Sequence[Step]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in results:
        for f in r["findings"]:
            row = out.setdefault(f["kind"], {"fail": 0, "note": 0, "existing": 0})
            row[f["severity"]] += 1
        for f in r["existing"]:
            out.setdefault(f["kind"], {"fail": 0, "note": 0, "existing": 0})["existing"] += 1
    return out


class Summary(TypedDict):
    steps: int
    with_ours: int
    counts: dict[str, dict[str, int]]
    reached: dict[str, int]
    correlation: dict[str, dict[str, Row]]


def main(argv: Sequence[str] | None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="layout_oracle", description=(__doc__ or "").split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help="archive roots, round folders or step folders")
    ap.add_argument("--json", action="store_true", help="one machine-readable document on stdout")
    ap.add_argument("--existing", action="store_true", help="also list what already looked broken before each sync")
    ap.add_argument("--notes", action="store_true", help="also list note-level findings")
    ap.add_argument("--no-ours", action="store_true", help="don't rebuild the new conversion from <step>/ours")
    ap.add_argument("--out", type=Path, help="also write the --json document to this file")
    args = ap.parse_args(argv)
    paths: list[Path] = args.paths
    as_json: bool = args.json
    show_existing: bool = args.existing
    notes: bool = args.notes
    out_file: Path | None = args.out
    results = [replay_step(s, not args.no_ours) for s in steps_under(paths)]
    summary: Summary = {"steps": len(results), "with_ours": sum(r["ours"] for r in results), "counts": counts(results),
                        "reached": dict(sorted(REACHED.items())), "correlation": correlate(results, "fail")}
    if as_json or out_file:
        doc = json.dumps({"summary": summary, "steps": [{k: v for k, v in r.items() if show_existing or k != "existing"}
                                                         for r in results]}, indent=1, ensure_ascii=False)
        if out_file:
            out_file.write_text(doc, encoding="utf-8")
        if as_json:
            print(doc)
            return 1 if any(failures(r["findings"]) for r in results) else 0
    print(f"{summary['steps']} steps ({summary['with_ours']} with the new conversion)")
    print(f"{'kind':<18}{'fail':>6}{'note':>6}{'existing':>10}")
    for kind, row in sorted(summary["counts"].items()):
        print(f"{kind:<18}{row['fail']:>6}{row['note']:>6}{row['existing']:>10}")
    print("reached: " + ", ".join(f"{k} {v}" for k, v in summary["reached"].items()))
    for r in results:
        shown = [f for f in r["findings"] if notes or f["severity"] == "fail"]
        shown += [f for f in r["existing"]] if show_existing else []
        if not shown:
            continue
        print(f"\n{r['archive']}/{r['round']}/step{r['step']}  variant={r['variant']}  edits: {', '.join(r['edits'])}")
        for f in shown:
            tag = f" [existing, {f.get('by')}]" if f.get("existing") else ""
            print(describe([f]) + tag)
            for h in f.get("history") or []:
                print(f"      {h['name']}: {h['was']} {' '.join(h.get('how') or [])}")
    for what, table in summary["correlation"].items():
        print(f"\nfail findings by {what} (steps, with a finding, lift):")
        for k, row in table.items():
            print(f"  {k:<28}{row['steps']:>5}{row['with_finding']:>5}  {row.get('lift')}  {row['kinds']}")
    return 1 if any(failures(r["findings"]) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(None))
