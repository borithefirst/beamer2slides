"""Did a sync lose anything a person put into the deck?

`check(base, before, after, report, ours)` answers that one question for a single sync, from
snapshots only (no Google call):

  base    the sync base the sync started from (`<out>/sync/base.json`)
  before  `snapshot.read_presentation` of the live deck immediately BEFORE the sync
  after   the same, immediately AFTER
  report  `<out>/sync/sync-report.json` (nested or flattened)
  ours    optional: the new conversion's slide entries and pairs (`sync.build_ours`), which is
          what tells a word the *source* deliberately rewrote from a word that just vanished

It returns findings; an empty list means nothing was lost. Unlike tools/sync_check.py (which
evaluates hand-written expectations per scenario) this makes no assumption about what was edited:
it derives the person's work from the three snapshots themselves, so it can judge a sync nobody
wrote a scenario for.

What "accounted for" means
--------------------------
A difference between `before` and `after` is accounted for when the report names it, or when the
merge policy of docs/sync.md says it is not the person's work. Concretely:

* **User objects** (`merge.user_objects`: live objects the base does not own, plus everything on a
  slide the base never saw) are the person's, always. They must exist after the sync with the same
  text, the same picture, the same box and the same parent group. Nothing in a report excuses
  changing one: the sync never has a reason to touch an object it did not create. Two allowances,
  both forced by the Slides API rather than by policy: a *group* may disappear when the sync
  removed its converter children and at most one child is left (Slides drops a one-child group),
  and a child may lose its parent group when that group is gone - a group that no longer exists
  cannot hold anything, and whether it was allowed to go is judged by the rule above. Being put
  *into* a group, or moved from one group into another that is still there, is never allowed.
* **A person's words inside a converter object** are the token runs that the live text has and the
  base read-back does not (`merge.tokens` + the diff hunks the merge itself uses). Every word of
  such a run must, after the sync, still be readable *somewhere on that slide* - it need not be in
  the same object, because classify may have joined or split paragraphs and the merge may have
  interleaved it with the source's own words. A word that is gone is only accounted for when the
  report reproduces it verbatim in a `conflicts` entry for that slide (the author can then still
  see what was replaced). This is the difference the task turns on:
    - the person's word survives, possibly next to new source words: a clean merge, fine;
    - the source rewrote the same words and the report says so: a reported conflict, fine;
    - the source rewrote the same words and the report is silent: `overwritten_unreported`;
    - nobody claims it and it is gone: `word_lost`.
  Whether the source rewrote the same region is decided with `ours`: the person's hunk and the
  source's hunk over the same base tokens clash (`merge._clash`), exactly the test the merge uses.
  Without `ours` every disappearance is a `word_lost`.
  The reverse also counts as the person's work: text the person *deleted* must not come back into
  that element unless the source says it again (`deletion_undone`).
* **A converter element the person moved or resized** must not be back at the base's box: a sync
  that rewrites the unit re-applies the deck's transform, so afterwards it stands where the person
  put it, or - when the source moved it too - at the conversion's new box with the person's move on
  top of it (`deck_placement` works that box out from `ours`; the old box can fall on it by
  coincidence, and then nothing was reverted). A person's resize must not be back at the converter's
  size either, wherever the element went, when the source left that size alone. When the report
  says both moved it and the person's move was carried (`merge.GEOMETRY_CARRIED`), its corner must
  be the person's plus the source's move (`geometry_not_carried`) - plus, for a formula picture the
  sync's refit moved to follow its hole into the merged words, the move the report names (`refit`)
  in place of the one the base had recorded, both under the person's own edit.
* **A picture the person put into a converter element** (the read-back's picture differing from the
  base's, compared by `snapshot.signature`, never by URL) must still be on one of that element's
  objects afterwards.
* **Styling a person applied to a converter object** (the read-back's `text_style_hash` /
  `shape_style_hash` differing from the base's) must not come back as the converter's: after the
  sync at least one of that element's objects must still show the person's hash.
  These three are excused by a `conflicts` or a `converged` entry for the element, and by nothing
  else - in particular not by `overrides`, which *claims* the deck's version was kept: a sync that
  reverts what it lists as an override is exactly the silent loss this oracle looks for. Nor, for
  the place and size, by the geometry conflict that promises them (`merge.GEOMETRY_CARRIED`).
* **Speaker notes and slide backgrounds** follow the same rules, per slide. On a slide the person
  added themselves the base has nothing to merge against, so the notes and the background must come
  through the sync word for word.
* **Slides**: a slide present before must be present after unless the report lists it under
  `slides_deleted` *and* the base plus the live read-back show nobody had touched it
  (`merge.slide_touched`). A slide the base never saw (the person added it) may never disappear,
  whatever the report says. The relative order of the surviving slides may only change for slides
  the report lists under `slides_moved`.
* **Converter content the source still asks for**: every base element that the new conversion still
  has, and that had a live object before, must have one after - its own objects, an object the sync
  created for it (`b2s_<h6 slide>_<h6 element>_...`) or an object tagged `b2s:<slide>/<element>`.
  Unless a conflict says the deck had deleted it.
* **Text that could be read must stay readable**: a text object nobody could see through before the
  sync (no opaque shape above it in paint order - `paint_order`: the slide's page elements in
  order, a group's children in order inside it) must not end up under an opaque shape the sync
  created (`text_hidden`). Opaque is a shape whose fill is a solid colour at alpha 1. The text may be
  the same object or the element's new one; the only excuse is the new conversion itself stacking
  that shape above that text (`ours`: the shape's element after the text's). Nothing
  is deleted when this goes wrong, so every other check here passes: a sync that rebuilt a block's
  panels around the body text the person had edited put them on top of it (group child order, the
  front page's demo; `sync.Sync.regroup_requests`).
* **An honest report**: every `applied` entry must have changed something (ids, text, geometry,
  style or picture of that element; `added` must have produced objects, `removed` must have removed
  them), and every `converged` entry must have changed nothing. A report that claims work it did not
  do hides the work it did do, so a lying report is itself a finding.

Findings are `Finding` records (`finding_json`: `{kind, severity, slide, element, object, detail}`).
`severity` is "loss" (a person's or the source's content is gone), "undo" (an edit was reverted),
"report" (the report is not honest) or "note" (could not be verified, e.g. picture bytes without
signatures). The checks read the base as `sync_model.Base`, the read-backs as `DeckRead`, the new
conversion as `merge.Ours` and the report as an `OracleReport`; the dict entries of the same names
(`check`, `geometry_findings`, ...) parse their arguments and hand back findings as JSON.

    python tools/loss_oracle.py <folder>   # reads base(-before).json, before.json, after.json, (sync-)report.json and ours.json, there or in its sync/
"""

import json
import re
import sys
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from beamer2slides import identity, merge, snapshot
from beamer2slides.ir_types import Box
from beamer2slides.json_types import Json, JsonObject, as_array, as_object, as_optional_str, as_str
from beamer2slides.sync_model import (Base, DeckRead, ElementEntry, ElementKey, JsonMap, ObjectId, ReadBack,
                                      SlideEntry, SlideRead, base as parse_base, deck_read, image_read_json)
from beamer2slides.typing_compat import assert_never

WORD = re.compile(r"\w+", re.UNICODE)  # the same pieces `merge.tokens` diffs, so the two agree
GEOMETRY_TOLERANCE = 0.05   # pt, as merge's own
SCALE_TOLERANCE = 1e-3
HIDDEN = 0.2                # of a text's box: an opaque shape above it covering more hides it

Severity = Literal["loss", "undo", "report", "note"]
SEVERITIES: tuple[Severity, ...] = ("loss", "undo", "report", "note")
FindingKind = Literal[
    # slides
    "user_slide_deleted", "slide_deleted_unreported", "deleted_touched_slide", "reported_delete_not_done",
    "slide_count_mismatch", "user_slide_moved", "slide_moved_unreported",
    # the person's own objects
    "user_object_deleted", "user_text_changed", "user_object_moved", "user_image_changed", "user_image_unverified",
    "user_object_regrouped",
    # the person's work in converter objects
    "overwritten_unreported", "word_lost", "deletion_undone", "geometry_reverted", "geometry_not_carried",
    "picture_unverified", "picture_reverted", "style_reverted", "notes_word_lost", "background_lost",
    # what the source still asks for, and what can be read
    "element_vanished", "text_hidden",
    # an honest report
    "applied_no_change", "added_element_missing", "reported_remove_not_done", "converged_but_changed",
    "object_deleted_unreported",
    # the offline campaign's own judges (`fuzz_sync`), reported in the same form
    "unwritable_text", "adopt_double", "unwritten_move", "second_sync_writes"]
"""Every kind of finding there is: a new judge adds its kind here."""


def fails(severity: Severity) -> bool:
    """A note is what could not be verified; everything else fails a sync."""
    if severity == "note":
        return False
    if severity == "loss" or severity == "undo" or severity == "report":
        return True
    assert_never(severity)


@dataclass(frozen=True, kw_only=True)
class Finding:
    """One thing a sync lost, undid or misreported. `slide`: a base key (or the objectId of a slide
    the base never saw); `element`, `object`: where on it, when it is about one."""
    kind: FindingKind
    severity: Severity
    slide: str | None
    element: str | None
    object: str | None
    detail: str


def finding_json(f: Finding) -> JsonObject:
    return {"kind": f.kind, "severity": f.severity, "slide": f.slide, "element": f.element, "object": f.object,
            "detail": f.detail}


KINDS: tuple[FindingKind, ...] = (
    "user_slide_deleted", "slide_deleted_unreported", "deleted_touched_slide", "reported_delete_not_done",
    "slide_count_mismatch", "user_slide_moved", "slide_moved_unreported",
    "user_object_deleted", "user_text_changed", "user_object_moved", "user_image_changed", "user_image_unverified",
    "user_object_regrouped",
    "overwritten_unreported", "word_lost", "deletion_undone", "geometry_reverted", "geometry_not_carried",
    "picture_unverified", "picture_reverted", "style_reverted", "notes_word_lost", "background_lost",
    "element_vanished", "text_hidden",
    "applied_no_change", "added_element_missing", "reported_remove_not_done", "converged_but_changed",
    "object_deleted_unreported",
    "unwritable_text", "adopt_double", "unwritten_move", "second_sync_writes")
"""`FindingKind` as values, for reading findings back (a test holds the two equal)."""
_KIND: dict[str, FindingKind] = {k: k for k in KINDS}
_SEVERITY: dict[str, Severity] = {s: s for s in SEVERITIES}


def finding_of(v: Json, where: str) -> Finding:
    """A finding as `finding_json` wrote it (findings.json, a live round's round.json)."""
    o = as_object(v, where)
    kind = _KIND.get(as_str(o["kind"], f"{where}.kind"))
    severity = _SEVERITY.get(as_str(o["severity"], f"{where}.severity"))
    if kind is None or severity is None:
        raise ValueError(f"{where}: no finding of kind {o['kind']!r} and severity {o['severity']!r}")
    return Finding(kind=kind, severity=severity, slide=as_optional_str(o["slide"], f"{where}.slide"),
                   element=as_optional_str(o["element"], f"{where}.element"),
                   object=as_optional_str(o["object"], f"{where}.object"), detail=as_str(o["detail"], f"{where}.detail"))


def h6(text: str) -> str:
    return identity.sha1(text)[:6]


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def words(text: str | None) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "")}


# ---------------------------------------------------------------- the report

@dataclass(frozen=True, kw_only=True)
class Entry:
    """One entry of a report list (applied, conflicts, converged) as the checks ask it. `raw`: the
    entry as written, which is what a conflict "reproducing" a word is searched in (`_mentions`)."""
    slide: str | None
    element: str | None
    fields: tuple[str, ...]
    field: str | None
    resolution: str | None
    page: str | None
    raw: JsonObject


@dataclass(frozen=True, kw_only=True)
class RefitEntry:
    """What the sync's refit moved an element by, in the converter's frame (the report's `refit`)."""
    slide: str | None
    element: str | None
    shift: tuple[float, float] | None


@dataclass(frozen=True, kw_only=True)
class OracleReport:
    """The report as the checks read it (`report_of`): the entries that excuse or claim something,
    and the slides it says were created, deleted and moved."""
    applied: tuple[Entry, ...]
    conflicts: tuple[Entry, ...]
    converged: tuple[Entry, ...]
    refit: tuple[RefitEntry, ...]
    slides_created: int
    slides_deleted: tuple[str, ...]
    slides_moved: tuple[str, ...]


def normalise_report(report: JsonMap | None) -> JsonObject:
    """The report as written by `sync.write_reports` (flattened) or as `merge.empty_report` has it."""
    r: JsonObject = dict(report or {})
    slides = r.get("slides")
    if isinstance(slides, dict):
        for k, v in slides.items():
            r.setdefault(f"slides_{k}", v)
    for k in ("applied", "overrides", "conflicts", "converged", "user_objects", "warnings"):
        r.setdefault(k, [])
    for k in ("created", "deleted", "moved", "kept", "user_added"):
        r.setdefault(f"slides_{k}", [])
    return r


def _text(v: Json) -> str | None:
    """A report's name for something: a string, or nothing (an entry about no element)."""
    return v if isinstance(v, str) else None


def _entries(r: JsonMap, key: str) -> tuple[Entry, ...]:
    # (an entry that is not an object names nothing, and is passed over as the checks always did)
    out: list[Entry] = []
    for e in as_array(r[key], f"the report's {key}"):
        if not isinstance(e, dict):
            continue
        fields = e.get("fields")
        out.append(Entry(slide=_text(e.get("slide")), element=_text(e.get("element")),
                         fields=tuple(f for f in fields if isinstance(f, str)) if isinstance(fields, list) else (),
                         field=_text(e.get("field")), resolution=_text(e.get("resolution")),
                         page=_text(e.get("page")), raw=e))
    return tuple(out)


def _shift(v: Json) -> tuple[float, float] | None:
    if not isinstance(v, list) or len(v) != 2:
        return None
    x, y = v
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (x, y)
    return None


def report_of(report: JsonMap | None) -> OracleReport:
    r = normalise_report(report)
    refit = r.get("refit") or []
    return OracleReport(
        applied=_entries(r, "applied"), conflicts=_entries(r, "conflicts"), converged=_entries(r, "converged"),
        refit=tuple(RefitEntry(slide=_text(m.get("slide")), element=_text(m.get("element")), shift=_shift(m.get("shift")))
                    for m in as_array(refit, "the report's refit") if isinstance(m, dict)),
        slides_created=len(as_array(r["slides_created"], "the report's created slides")),
        slides_deleted=tuple(k for k in as_array(r["slides_deleted"], "the report's deleted slides")
                             if isinstance(k, str)),
        slides_moved=tuple(k for k in as_array(r["slides_moved"], "the report's moved slides") if isinstance(k, str)))


def unit_key(el: ElementEntry) -> ElementKey:
    """The report names an element by its *unit*: the anchor of a formula picture or number ball,
    which is planned and written together with it (`merge.units`)."""
    return el.anchor or el.key


def _at(entries: Sequence[Entry], slide: str, element: Collection[str] | None) -> list[Entry]:
    return [e for e in entries if e.slide == slide and (element is None or e.element in element)]


def _mentions(entries: Sequence[Entry], word: str) -> bool:
    text = json.dumps([e.raw for e in entries], ensure_ascii=False).lower()
    return word.lower() in text


# ---------------------------------------------------------------- the read-backs

def slide_words(slide_read: SlideRead) -> set[str]:
    return words("\n".join(rb.text or "" for rb in slide_read.objects.values()))


ObjectState = tuple[str, tuple[float, ...], tuple[float, ...], str, str, str | None]


def object_state(rb: ReadBack) -> ObjectState:
    """What a read-back shows, for "did this object change"."""
    return (norm(rb.text), tuple(round(v, 2) for v in rb.box), tuple(round(v, 4) for v in rb.transform),
            rb.text_style_hash, rb.shape_style_hash, None if rb.image is None else rb.image.content_hash)


def _same_geometry(box_a: Sequence[float], box_b: Sequence[float], t_a: Sequence[float], t_b: Sequence[float]) -> bool:
    return not (any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(box_a, box_b))
                or any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(t_a[:4], t_b[:4])))


def same_box_of(a: ReadBack, b: ReadBack) -> bool:
    return _same_geometry(a.box, b.box, a.transform, b.transform)


def _numbers(v: Json) -> tuple[float, ...]:
    """A read-back's box or transform as a dict caller hands it: numbers, or nothing to compare."""
    if not isinstance(v, list):
        return ()
    return tuple(x for x in v if isinstance(x, (int, float)))


def same_box(a: JsonMap, b: JsonMap) -> bool:
    """`same_box_of` over read-back dicts (layout_oracle's view): what a side does not say is not compared."""
    return _same_geometry(_numbers(a.get("box")), _numbers(b.get("box")), _numbers(a.get("transform")),
                          _numbers(b.get("transform")))


def same_image(a: ReadBack, b: ReadBack) -> bool | None:
    """True/False, or None when the bytes can't be compared (Google hands out new contentUrls, so
    only a pixel signature decides; `snapshot.sign_pictures` puts those in)."""
    ia, ib = a.image, b.image
    if ia is None and ib is None:
        return True
    if ia is None or ib is None:
        return False
    if ia.signature and ib.signature:
        return snapshot.same_picture(image_read_json(ia), image_read_json(ib))
    if ia.content_hash == ib.content_hash:
        return True
    return None


def _by_id(read: DeckRead) -> dict[ObjectId, SlideRead]:
    return {s.object_id: s for s in read.slides}


def _live(slide: SlideEntry, before: Mapping[ObjectId, SlideRead],
          after: Mapping[ObjectId, SlideRead]) -> tuple[SlideRead, SlideRead] | None:
    """A base slide's read-back before and after the sync, when both have it."""
    sid = slide.object_id
    if sid is None:
        return None
    bs, a = before.get(sid), after.get(sid)
    return None if bs is None or a is None else (bs, a)


def _tied(el: ElementEntry, read: SlideRead) -> tuple[ObjectId, ReadBack, ReadBack] | None:
    """A base element's main object, the base's read-back of it and the live one, when all three are there."""
    main = el.main
    if not main:
        return None
    was, now = el.readback.get(main), read.objects.get(main)
    return None if was is None or now is None else (main, was, now)


# ---------------------------------------------------------------- slides

def slide_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    out: list[Finding] = []
    base_by_id = {s.object_id: s for s in base.slides if s.object_id}
    after_ids = {s.object_id for s in after.slides}
    for bs in before.slides:
        sid = bs.object_id
        if sid in after_ids:
            continue
        b = base_by_id.get(sid)
        if b is None:
            out.append(Finding(kind="user_slide_deleted", severity="loss", slide=sid, element=None, object=sid,
                               detail="a slide the person added is gone"))
            continue
        key = b.key
        if key not in rep.slides_deleted:
            out.append(Finding(kind="slide_deleted_unreported", severity="loss", slide=key, element=None, object=sid,
                               detail=f"slide {key} is gone but the report doesn't say so"))
            continue
        touched = merge.slide_touched_of(b, bs)
        if touched:
            out.append(Finding(kind="deleted_touched_slide", severity="loss", slide=key, element=None, object=sid,
                               detail=f"slide {key} was deleted although the deck had it: {touched}"))
    # the report's own claims about slides
    for key in rep.slides_deleted:
        sid = next((s.object_id for s in base.slides if s.key == key), None)
        if sid and sid in after_ids:
            out.append(Finding(kind="reported_delete_not_done", severity="report", slide=key, element=None, object=sid,
                               detail=f"the report deletes slide {key} but it is still there"))
    created = len(after.slides) - len({s.object_id for s in before.slides} & after_ids)
    if created != rep.slides_created:
        out.append(Finding(kind="slide_count_mismatch", severity="report", slide=None, element=None, object=None,
                           detail=f"{created} slide(s) appeared, the report lists {rep.slides_created} created"))
    out += order_findings_of(base, before, after, rep)
    return out


def order_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    key_of = {s.object_id: s.key for s in base.slides if s.object_id}
    after_ids = [s.object_id for s in after.slides]
    kept = set(after_ids)
    b = [s.object_id for s in before.slides if s.object_id in kept]
    a = [sid for sid in after_ids if sid in set(b)]
    if a == b:
        return []
    reported = set(rep.slides_moved)
    before_prev = {sid: (b[i - 1] if i else None) for i, sid in enumerate(b)}
    after_prev = {sid: (a[i - 1] if i else None) for i, sid in enumerate(a)}
    # Taking the reported moves out - and whatever travelled with them - has to leave the same
    # order. A slide travelled with the one before it if it still follows the same slide (the copy
    # a person made of a slide the source then moved keeps sitting behind its original). Which of
    # two slides that swapped "moved" has no single answer, and the report is free to name either,
    # as long as putting its choice back explains the rest of the order.
    gone = {sid for sid in b if key_of.get(sid) in reported}
    while True:
        rode = {sid for sid in a if sid not in gone and after_prev[sid] in gone
                and before_prev[sid] == after_prev[sid]}
        if not rode:
            break
        gone |= rode
    if [s for s in b if s not in gone] == [s for s in a if s not in gone]:
        return []
    moved = set(a) - set(_lcs(b, a))
    out: list[Finding] = []
    for sid in a:
        # A slide that kept the slide it followed didn't move by itself: it went along with its
        # neighbour (the source reordering the frames around a slide the person added).
        if sid not in moved or before_prev[sid] == after_prev[sid]:
            continue
        key = key_of.get(sid)
        if key is None:
            out.append(Finding(kind="user_slide_moved", severity="undo", slide=sid, element=None, object=sid,
                               detail="a slide the person added changed place"))
        elif key not in rep.slides_moved:
            out.append(Finding(kind="slide_moved_unreported", severity="undo", slide=key, element=None, object=sid,
                               detail=f"slide {key} changed place, the report doesn't say so"))
    return out


def _lcs(a: Sequence[ObjectId], b: Sequence[ObjectId]) -> list[ObjectId]:
    n, m = len(a), len(b)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            table[i][j] = table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
    out: list[ObjectId] = []
    i, j = 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            out.append(a[i])
            i, j = i + 1, j + 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


# ---------------------------------------------------------------- user objects

def user_object_findings_of(base: Base, before: DeckRead, after: DeckRead) -> list[Finding]:
    out: list[Finding] = []
    base_by_id = {s.object_id: s for s in base.slides if s.object_id}
    after_by_id = _by_id(after)
    for bs in before.slides:
        a = after_by_id.get(bs.object_id)
        if a is None:
            continue  # the slide itself is gone: slide_findings said so
        b = base_by_id.get(bs.object_id)
        skey = b.key if b else bs.object_id
        users = list(bs.objects) if b is None else [u.object_id for u in merge.user_objects_of(b, bs)]
        for oid in users:
            rb, now = bs.objects[oid], a.objects.get(oid)
            if now is None:
                if rb.kind == "elementGroup" and len([c for c in rb.children or () if c in a.objects]) <= 1:
                    continue  # Slides drops a group left with one child
                out.append(Finding(kind="user_object_deleted", severity="loss", slide=skey, element=None, object=oid,
                                   detail=f"{rb.kind} the person added is gone"))
                continue
            if norm(rb.text) != norm(now.text):
                out.append(Finding(kind="user_text_changed", severity="loss", slide=skey, element=None, object=oid,
                                   detail=f"{norm(rb.text)!r} became {norm(now.text)!r}"))
            if rb.kind != "elementGroup" and not same_box_of(rb, now):
                # A group's box is its children's union: converter children that follow the
                # source move it (live fuzz r7403, r7407). Every child is judged on its own -
                # the person's here, the converter's by geometry_reverted.
                out.append(Finding(kind="user_object_moved", severity="undo", slide=skey, element=None, object=oid,
                                   detail=f"box {list(rb.box)} became {list(now.box)}"))
            same = same_image(rb, now)
            if same is False:
                out.append(Finding(kind="user_image_changed", severity="loss", slide=skey, element=None, object=oid,
                                   detail="the picture the person put there is another one now"))
            elif same is None:
                out.append(Finding(kind="user_image_unverified", severity="note", slide=skey, element=None, object=oid,
                                   detail="no pixel signature: the picture bytes weren't compared"))
            old, new = rb.parent_group, now.parent_group
            if old != new and (new is not None or old in a.objects):
                # A group that is gone can't hold anything; that it went is judged above, where a
                # group left with one child is Slides' doing and a group with more is a loss.
                out.append(Finding(kind="user_object_regrouped", severity="undo", slide=skey, element=None, object=oid,
                                   detail=f"parent group {old} became {new}"))
    return out


# ---------------------------------------------------------------- the person's words

def hunks(base_text: str | None, live_text: str | None) -> list[merge.Hunk]:
    return merge._hunks(merge.tokens(base_text or ""), merge.tokens(live_text or ""))


OursByKey = dict[str, dict[ElementKey, ElementEntry]]


def text_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport,
                     ours: merge.Ours | None) -> list[Finding]:
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    ours_by_key = _ours_by_key(ours)
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None:
            continue
        bs, a = live
        skey = b.key
        before_words, after_words = slide_words(bs), slide_words(a)
        conflicts = _at(rep.conflicts, skey, None)
        for el in b.elements:
            tied = _tied(el, bs)
            if tied is None:
                continue
            main, was, now = tied
            here = words("\n".join((a.objects[o].text or "") for o in element_objects_of(skey, el.key, el, a)))
            out += word_findings(skey, el.key, main, was.text, now.text, before_words, after_words, here, conflicts,
                                 ours_by_key.get(skey, {}).get(el.key))
        out += notes_findings(skey, b, bs, a, conflicts)
        out += background_findings(skey, b, bs, a)
    return out


def word_findings(skey: str, ekey: ElementKey, main: ObjectId | None, was: str | None, now: str | None,
                  before_words: set[str], after_words: set[str], after_el_words: set[str],
                  conflicts: Sequence[Entry], ours_el: ElementEntry | None) -> list[Finding]:
    """The person's words in one converter object (`was`: its text in the base, `now`: live before
    the sync): gone without a trace, or overwritten silently."""
    out: list[Finding] = []
    base_tokens = merge.tokens(was or "")
    theirs = hunks(was, now)
    if not theirs:
        return out
    source = hunks(was, _ours_text(ours_el)) if ours_el else None
    for hunk in theirs:
        typed = "".join(hunk[2])
        clash = source is not None and any(merge._clash(hunk, s) for s in source)
        for w in sorted(words(typed)):
            if w in after_words:
                continue
            if _mentions(conflicts, w):
                continue
            if clash:
                out.append(Finding(kind="overwritten_unreported", severity="loss", slide=skey, element=ekey, object=main,
                                   detail=f"the source overwrote {w!r} which the person had typed into "
                                          f"{norm(now)[:60]!r}, without a conflict in the report"))
            else:
                out.append(Finding(kind="word_lost", severity="loss", slide=skey, element=ekey, object=main,
                                   detail=f"{w!r}, typed by the person, is nowhere on the slide any more"))
        removed = words("".join(base_tokens[hunk[0]:hunk[1]]))
        for w in sorted(removed - before_words):
            # back in *this* element: the same word turning up in another element on the slide (the
            # source retitled the frame with it) is the source's word, not the person's deletion.
            if w not in after_el_words or _mentions(conflicts, w):
                continue
            if ours_el and w in words(_ours_text(ours_el)):
                continue  # the source says it again: an applied source change, not an undo
            out.append(Finding(kind="deletion_undone", severity="undo", slide=skey, element=ekey, object=main,
                               detail=f"{w!r}, which the person had deleted, is back"))
    return out


# ---------------------------------------------------------------- geometry

Placement = tuple[float, float, float]
"""How a conversion's coordinates land in the deck: `x_deck = s*x + tx`, `y_deck = s*y + ty`."""


def deck_placement_of(base: Base) -> Placement | None:
    """How a conversion's coordinates (`fingerprint.bbox`) land in the deck: `x_deck = s*x + tx`.
    Read off the base itself - every element the converter wrote stands at its own bbox - as the
    median over all of them, so the few boxes a person has since moved don't move the estimate.
    (the deck's width over the PDF's, 0, 0 in a real deck; the fuzz world uses its own scale on
    purpose, so that a confusion between the two coordinate systems cannot pass unnoticed.)

    Pictures, when there are three, are asked alone: emit draws a picture at its bbox, but a text
    box is wider than its words (up to the mirror of the slide's leftmost text) and taller (insets),
    and with them in the median a real deck read 3.09 for 1.59 (archived live fuzz, 2026-09-23).

    A base that records the scale emit drew with (`scale`, every convert since the sync base had one)
    is taken at its word: the fit was the only guess left, and a deck of two pictures still fitted
    its text boxes - the edit hunt's talks read 3.08 and 4.61 for 1.98, and three syncs that carried
    a person's move exactly were accused of `geometry_not_carried` by 26, 37 and 8 pt."""
    if base.scale:
        return float(base.scale), 0.0, 0.0

    def measured(only_pictures: bool) -> Iterator[tuple[Box, Box]]:
        for sl in base.slides:
            for el in sl.elements:
                if only_pictures and el.kind != "image":
                    continue
                bb = el.fingerprint.bbox
                rb = el.readback.get(el.main) if el.main else None
                if rb is not None and bb[2] - bb[0] >= 1 and bb[3] - bb[1] >= 1:
                    yield bb, rb.box
    pairs = list(measured(True))
    if len(pairs) < 3:
        pairs = list(measured(False))
    if len(pairs) < 3:
        return None
    scales = sorted(((box[2] - box[0]) / (bb[2] - bb[0]) + (box[3] - box[1]) / (bb[3] - bb[1])) / 2 for bb, box in pairs)
    s = scales[len(scales) // 2]
    xs, ys = sorted(box[0] - s * bb[0] for bb, box in pairs), sorted(box[1] - s * bb[1] for bb, box in pairs)
    return s, xs[len(xs) // 2], ys[len(ys) // 2]


def _fresh_box(place: Placement | None, bbox: Box | None) -> list[float] | None:
    if not place or not bbox:
        return None
    s, tx, ty = place
    return [s * bbox[0] + tx, s * bbox[1] + ty, s * bbox[2] + tx, s * bbox[3] + ty]


RESIZED = 1.0      # pt: a person's resize the oracle holds a sync to
SAME_SIZE = 0.5    # pt: a recreated box this close to the base's size is the converter's size again
CARRIED_PLACE = 2.0  # pt: how close a carried corner must land (in the archive every element the source
                     # moved and the report lists as applied stood within 0.3 pt of its corner plus that move)


def _frame_drop(rb: ReadBack) -> float:
    """How much lower than a text box emit sets a text's frame when the text goes into a layout
    placeholder (`emit.PPTX_TITLE_DY`: an imported placeholder's top inset is that much smaller), so
    that its words stand where a box's would."""
    from beamer2slides.emit import PPTX_TITLE_DY
    return PPTX_TITLE_DY if rb.placeholder else 0.0


def _corner(rb: ReadBack) -> tuple[float, float]:
    """Where a text object's words start, as the corner of the text box that would hold them there:
    a placeholder's corner less `_frame_drop`."""
    return rb.box[0], rb.box[1] - _frame_drop(rb)


def _carried_to(place: Placement | None, base_el: ElementEntry, ours_el: ElementEntry | None, now: ReadBack,
                was: ReadBack, anchor: tuple[ElementEntry, ElementEntry] | None,
                refit_new: tuple[float, float] | None) -> list[float] | None:
    """Where the corner of an element both sides moved belongs after a sync that carried the
    person's edit (`sync.carried`): the person's corner plus the source's move of the element's IR
    corner, in deck pt. Taken from the person's own box, so a text box's frame around its words
    (wider and taller than the bbox) does not enter into it. None without `ours` or a placement.

    Corners are the words' (`_corner`): the source can move a text into the title page's subtitle
    placeholder or out of it (a longer line taking the role over, or losing it again), and emit sets a
    placeholder's frame `_frame_drop` lower around the same words. The sync carries the person's step
    onto the frame the new conversion writes, so the words land at their new place plus that step
    whichever frame holds them; the frame itself moves by the bbox step and the drop (live fuzz
    r8009, variants subtitle and deletions: 3.9 pt off both ways).

    `sync.carried` adds the source's move of the unit's *top* as it is, and the person's edit D
    (was -> now) acts on the rest: on how far this member moved apart from its unit's anchor
    (`anchor`: its base and ours elements), and on what `refit` moved it by in the converter's frame
    - this sync's step (`refit_new`, the report's `refit`) in place of the one the base had recorded
    (`refit` on the base read-back). With D a move both are plain additions; a group the person had
    scaled scales them (r8011: 1.15)."""
    if not place or ours_el is None:
        return None
    bb, ob = base_el.fingerprint.bbox, ours_el.fingerprint.bbox
    s = place[0]
    move = [s * (ob[0] - bb[0]), s * (ob[1] - bb[1])]
    if anchor is not None:
        ab, ao = anchor[0].fingerprint.bbox, anchor[1].fingerprint.bbox
        top = [s * (ao[0] - ab[0]), s * (ao[1] - ab[1])]
    else:
        top = move
    apart = [move[0] - top[0], move[1] - top[1]]
    old, new = was.refit or (0.0, 0.0), refit_new or (0.0, 0.0)
    extra = [apart[0] + new[0] - old[0], apart[1] + new[1] - old[1]]
    d = [1.0, 0.0, 0.0, 1.0]
    if was.transform and now.transform:
        d = snapshot.compose(list(now.transform), snapshot.invert(list(was.transform)))[:4]
    x, y = _corner(now)
    return [x + top[0] + d[0] * extra[0] + d[1] * extra[1],
            y + top[1] + d[2] * extra[0] + d[3] * extra[1]]


def _refit_shift(rep: OracleReport, skey: str, ekey: str) -> tuple[float, float] | None:
    """The report's word on what `refit` moved this element by, in the converter's frame."""
    for m in rep.refit:
        if m.slide == skey and m.element == ekey and m.shift:
            return m.shift
    return None


def _size(rb: ReadBack) -> list[float]:
    box = rb.box
    return [round(box[2] - box[0], 2), round(box[3] - box[1], 2)]


def _resize_dropped(was: ReadBack, now: ReadBack, made: Sequence[ReadBack], base_el: ElementEntry,
                    ours_el: ElementEntry | None) -> bool:
    """The person resized the element, the source left its size alone, and of the objects the sync
    made for it none has the person's size while one is back at the converter's: the resize was
    dropped wherever the element went (a place check alone misses it when the source moved the
    element, as geometry mode `theirs` once did, layout-grown-box-moved). Only objects the sync made
    count: a copy the person made keeps the element's tag and the converter's size. Without `ours`
    nothing is said."""
    if ours_el is None:
        return False
    bb, ob = base_el.fingerprint.bbox, ours_el.fingerprint.bbox
    if any(abs((ob[i + 2] - ob[i]) - (bb[i + 2] - bb[i])) > 0.01 for i in (0, 1)):
        return False
    w, n = _size(was), _size(now)
    if all(abs(x - y) <= RESIZED for x, y in zip(w, n)):
        return False
    sizes = [_size(rb) for rb in made]
    return not any(all(abs(x - y) <= SAME_SIZE for x, y in zip(s, n)) for s in sizes) and \
        any(all(abs(x - y) <= SAME_SIZE for x, y in zip(s, w)) for s in sizes)


def geometry_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport,
                         ours: merge.Ours | None) -> list[Finding]:
    """A converter element the person moved or resized, back where the converter had put it. A sync
    that writes the unit re-applies the deck's transform (docs/sync.md: `overrides["geometry"]`), so
    the box afterwards is the person's or, when the source moved it too, somewhere else - never the
    base's again.

    "Somewhere else" has to be taken literally: when the source moved the element as well, the
    rewritten element lands at the conversion's new box *plus* the step the person moved it by, and
    that can fall exactly on the base's old box by coincidence. With `ours` the oracle works that
    place out (`deck_placement`) and lets the element stand there; without it, such a round reads as
    a revert.

    A conflict on the element excuses it, except the geometry conflict `merge.GEOMETRY_CARRIED`: that
    one says the person's move and size went on top of the source's move, so the element is held to
    it - its corner where `_carried_to` says (`geometry_not_carried`; the deck's absolute place,
    which geometry mode `theirs` used to write, is not it). What refit moved is taken from what the
    sync recorded - the report's `refit` for this sync, the base read-back's `refit` for the last
    one - never forgiven for being a formula picture: a picture refit moved to the wrong place, or
    one it says nothing about, is still found (r8006, r8011). A person's resize is judged apart from
    the place (`_resize_dropped`)."""
    out: list[Finding] = []
    place = deck_placement_of(base)
    ours_by_key = _ours_by_key(ours)
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None:
            continue
        bs, a = live
        skey = b.key
        mine = ours_by_key.get(skey, {})
        for el in b.elements:
            tied = _tied(el, bs)
            if tied is None or same_box_of(tied[1], tied[2]):
                continue  # the person left it where it was
            main, was, now = tied
            carriers = element_objects_of(skey, el.key, el, a)
            objects = [a.objects[o] for o in carriers]
            keys = {el.key, unit_key(el)}
            ours_el = mine.get(el.key)
            # A conflict excuses the element, except the one that says the person's move and size were
            # carried onto the source's new place (both moved it): that one promises them.
            conflicts = _at(rep.conflicts, skey, keys)
            promised = [c for c in conflicts if c.field == "geometry" and c.resolution == merge.GEOMETRY_CARRIED]
            if len(promised) < len(conflicts) or _at(rep.converged, skey, keys):
                continue
            made = [a.objects[o] for o in carriers if o not in bs.objects]
            if made and _resize_dropped(was, now, made, el, ours_el):
                out.append(Finding(kind="geometry_reverted", severity="undo", slide=skey, element=el.key, object=main,
                                   detail=f"back at the converter's size {_size(was)}, the person had made it {_size(now)}"))
                continue
            a_base = next((x for x in b.elements if x.key == el.anchor), None) if el.anchor else None
            a_ours = mine.get(el.anchor) if a_base and el.anchor else None
            to = _carried_to(place, el, ours_el, now, was, (a_base, a_ours) if a_base and a_ours else None,
                             _refit_shift(rep, skey, el.key))
            if promised and objects and to is not None and \
                    not any(max(abs(c[0] - to[0]), abs(c[1] - to[1])) <= CARRIED_PLACE
                            for c in (_corner(rb) for rb in objects)):
                out.append(Finding(kind="geometry_not_carried", severity="undo", slide=skey, element=el.key, object=main,
                                   detail=f"at {list(objects[0].box)}; the person's move on top of the source's puts its "
                                          f"corner at {[round(v, 2) for v in to]}, as the report promises"))
                continue
            if not objects or any(same_box_of(now, rb) for rb in objects) or not any(same_box_of(was, rb) for rb in objects):
                continue  # still where the person put it, or somewhere the source asked for
            fresh = _fresh_box(place, None if ours_el is None else ours_el.fingerprint.bbox)
            if fresh is not None:
                step = [n - w for n, w in zip(now.box, was.box)]
                moved = [f + d for f, d in zip(fresh, step)]
                if any(all(abs(x - y) <= GEOMETRY_TOLERANCE for x, y in zip(moved, rb.box)) for rb in objects):
                    continue  # the conversion's new box, moved the way the person moved it
            out.append(Finding(kind="geometry_reverted", severity="undo", slide=skey, element=el.key, object=main,
                               detail=f"back at the converter's box {list(was.box)}, the person had it at {list(now.box)}"))
    return out


# ---------------------------------------------------------------- pictures, styles, notes, backgrounds

def picture_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    """A picture the person put into a converter element (by pixel signature, never by URL: Google
    hands out new contentUrls for unchanged pictures), overwritten with the source's."""
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None:
            continue
        bs, a = live
        skey = b.key
        for el in b.elements:
            tied = _tied(el, bs)
            if tied is None or same_image(tied[1], tied[2]) is not False:
                continue  # the person didn't put another picture there
            main, _, now = tied
            objects = [a.objects[o] for o in element_objects_of(skey, el.key, el, a)]
            states = [same_image(now, rb) for rb in objects]
            if True in states or not objects:
                continue  # their picture is still there (or the element is gone: another check)
            keys = {el.key, unit_key(el)}
            if _at(rep.conflicts, skey, keys) or _at(rep.converged, skey, keys):
                continue  # (an `overrides` entry says the deck's version was kept: it excuses nothing here)
            if None in states:
                out.append(Finding(kind="picture_unverified", severity="note", slide=skey, element=el.key, object=main,
                                   detail="no pixel signature: the picture bytes weren't compared"))
            else:
                out.append(Finding(kind="picture_reverted", severity="loss", slide=skey, element=el.key, object=main,
                                   detail="the picture the person put into this element is another one now, "
                                          "with nothing in the report"))
    return out


StyleField = Literal["text_style_hash", "shape_style_hash"]


def _style(rb: ReadBack, field: StyleField) -> str:
    if field == "text_style_hash":
        return rb.text_style_hash
    if field == "shape_style_hash":
        return rb.shape_style_hash
    assert_never(field)


def style_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    """Styling the person applied to a converter object, silently back to the converter's."""
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    fields: tuple[StyleField, ...] = ("text_style_hash", "shape_style_hash")
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None:
            continue
        bs, a = live
        skey = b.key
        for el in b.elements:
            tied = _tied(el, bs)
            if tied is None:
                continue
            main, was, now = tied
            objects = [a.objects[o] for o in element_objects_of(skey, el.key, el, a)]
            if not objects:
                continue  # gone: the other checks judge that
            for field in fields:
                converter, person = _style(was, field), _style(now, field)
                if person == converter or not any(_style(rb, field) == converter for rb in objects):
                    continue  # the person didn't restyle it, or the converter's styling isn't back
                if any(_style(rb, field) == person for rb in objects):
                    continue  # their styling is still on one of the element's objects
                keys = {el.key, unit_key(el)}
                if _at(rep.conflicts, skey, keys) or _at(rep.converged, skey, keys):
                    continue
                out.append(Finding(kind="style_reverted", severity="undo", slide=skey, element=el.key, object=main,
                                   detail=f"{field} is the converter's again: the person's styling is gone with "
                                          "nothing in the report"))
    return out


def notes_findings(skey: str, b: SlideEntry, bs: SlideRead, a: SlideRead, conflicts: Sequence[Entry]) -> list[Finding]:
    out: list[Finding] = []
    was = "" if b.seen is None else b.seen.notes_readback
    for hunk in hunks(was, bs.notes):
        for w in sorted(words("".join(hunk[2])) - words(a.notes)):
            if _mentions(conflicts, w):
                continue
            out.append(Finding(kind="notes_word_lost", severity="loss", slide=skey, element=None, object=None,
                               detail=f"{w!r} of the speaker notes the person wrote is gone"))
    return out


def user_slide_findings_of(base: Base, before: DeckRead, after: DeckRead) -> list[Finding]:
    """The notes and background of a slide the person added themselves: the base never saw that
    slide, so nothing the sync does to it can be called a merge - it must come through untouched."""
    theirs = {s.object_id for s in base.slides if s.object_id}
    after_by_id = _by_id(after)
    out: list[Finding] = []
    for bs in before.slides:
        a = after_by_id.get(bs.object_id)
        if bs.object_id in theirs or a is None:
            continue  # a converter slide, or one that vanished: the slide checks judge that
        for w in sorted(words(bs.notes) - words(a.notes)):
            out.append(Finding(kind="notes_word_lost", severity="loss", slide=bs.object_id, element=None, object=None,
                               detail=f"{w!r} of the speaker notes on a slide the person added is gone"))
        if not snapshot.same_background(bs.background, a.background):
            out.append(Finding(kind="background_lost", severity="loss", slide=bs.object_id, element=None, object=None,
                               detail=f"the background of a slide the person added ({bs.background}) became "
                                      f"{a.background}"))
    return out


def background_findings(skey: str, b: SlideEntry, bs: SlideRead, a: SlideRead) -> list[Finding]:
    was = None if b.seen is None else b.seen.background_readback
    if was is None:
        return []
    if snapshot.same_background(was, bs.background):
        return []  # the person didn't touch it
    if snapshot.same_background(bs.background, a.background):
        return []
    return [Finding(kind="background_lost", severity="loss", slide=skey, element=None, object=None,
                    detail=f"the background the person set ({bs.background}) became {a.background}")]


# ---------------------------------------------------------------- converter content

def _ours_by_key(ours: merge.Ours | None) -> OursByKey:
    """slide key -> {element key: ours element}."""
    if ours is None:
        return {}
    return {s.key: {e.key: e for e in s.elements} for s in ours.slides}


def _ours_text(el: ElementEntry | None) -> str:
    if not el:
        return ""
    ir = el.ir or {}
    if el.kind == "text" and ir.get("paragraphs"):
        return merge.predicted_text(ir)
    return identity.plain_text(ir) if ir else el.fingerprint.text or ""


def _carriers(skey: str, ekey: str, mine: Collection[str], titles: Iterable[tuple[str, str | None]]) -> set[str]:
    prefix = f"b2s_{h6(skey)}_{h6(ekey)}_"
    tag = snapshot.tag(skey, ekey)
    others = f"{snapshot.TAG_PREFIX}{skey}/"

    def handed_on(title: str) -> bool:
        return title.startswith(others) and title != tag
    return {oid for oid, title in titles
            if (oid in mine and not handed_on(title or "")) or oid.startswith(prefix) or title == tag}


def element_objects_of(skey: str, ekey: str, base_el: ElementEntry | None, slide_read: SlideRead) -> set[ObjectId]:
    """The live objects that carry one element after a sync: its own, ones the sync made for it, or
    ones tagged with its key.

    One of its own that now carries *another* element's tag of this slide is that element's: a sync
    hands a live layout placeholder, object id and all, to the text that has its role now (the
    subtitle of a title page, `sync.Sync.update_slide`'s `in_place`) and retitles it. Counting it
    still as the old element's put the other text's box under this one's name (a subtitle taken over
    by a longer line read as `geometry_not_carried`) and a removed element as still there
    (`reported_remove_not_done`, live fuzz r8009)."""
    mine: set[str] = set(base_el.objects) if base_el else set()
    found = _carriers(skey, ekey, mine, ((oid, rb.title) for oid, rb in slide_read.objects.items()))
    return {oid for oid in slide_read.objects if oid in found}


def _titles(slide_read: JsonMap) -> Iterator[tuple[str, str | None]]:
    for oid, rb in as_object(slide_read["objects"], "slide.objects").items():
        yield oid, as_optional_str(as_object(rb, f"slide.objects[{oid}]").get("title"), f"slide.objects[{oid}].title")


def _strings(v: Json) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def element_objects(skey: str, ekey: str, base_el: JsonMap | None, slide_read: JsonMap) -> set[str]:
    """`element_objects_of` over dicts: an element entry's `objects` and a read-back's titles are all it reads."""
    return _carriers(skey, ekey, set(_strings(base_el.get("objects"))) if base_el else set(), _titles(slide_read))


def content_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport,
                        ours: merge.Ours | None) -> list[Finding]:
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    ours_by_key = _ours_by_key(ours)
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None or b.key not in ours_by_key:
            continue
        bs, a = live
        skey = b.key
        for el in b.elements:
            if el.key not in ours_by_key[skey]:
                continue  # the source removed it: sync may delete it (word_findings guards the words)
            if not element_objects_of(skey, el.key, el, bs):
                continue  # the person had already deleted it
            if element_objects_of(skey, el.key, el, a):
                continue
            if _at(rep.conflicts, skey, {el.key, unit_key(el)}):
                continue
            out.append(Finding(kind="element_vanished", severity="loss", slide=skey, element=el.key, object=None,
                               detail=f"{el.kind}/{el.role} is in the new conversion but has no object left"))
    return out


# ---------------------------------------------------------------- readable text

def paint_order(slide_read: SlideRead) -> list[ObjectId]:
    """Object ids bottom to top: the slide's page elements in `order`, and a group's children in
    their order where the group stands. (Objects the order doesn't reach - a hand-made read-back -
    come last, by `z`.)"""
    objects = slide_read.objects
    out: list[ObjectId] = []
    seen: set[ObjectId] = set()

    def walk(ids: Iterable[ObjectId]) -> None:
        for oid in ids:
            if oid in seen or oid not in objects:
                continue
            seen.add(oid)
            out.append(oid)
            walk(objects[oid].children or ())
    walk(slide_read.order)
    walk(sorted((o for o in objects if o not in seen), key=lambda o: objects[o].z))
    return out


def opaque(rb: ReadBack) -> bool:
    fill = rb.shape_style.get("fill")
    if rb.kind != "shape" or not isinstance(fill, dict) or fill.get("color") is None:
        return False
    alpha = fill.get("alpha") or 0
    return isinstance(alpha, (int, float)) and alpha >= 0.999


def readable(rb: ReadBack) -> bool:
    return rb.kind in ("shape", "table") and bool(norm(rb.text))


def _cover(inner: Box, outer: Box) -> float:
    """The part of box `inner` that box `outer` covers."""
    w = min(inner[2], outer[2]) - max(inner[0], outer[0])
    h = min(inner[3], outer[3]) - max(inner[1], outer[1])
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return w * h / area if w > 0 and h > 0 and area > 0 else 0.0


def hiders(slide_read: SlideRead, oid: ObjectId, order: Sequence[ObjectId]) -> list[ObjectId]:
    """The opaque shapes above `oid` (in paint `order`) that cover more than `HIDDEN` of its box."""
    if oid not in order:
        return []
    box = slide_read.objects[oid].box
    return [x for x in order[order.index(oid) + 1:] if opaque(rb := slide_read.objects[x])
            and _cover(box, rb.box) > HIDDEN]


def page_elements(slide_read: SlideRead) -> dict[ObjectId, ObjectId]:
    """Object id -> the page element it is drawn inside (itself, when it is one)."""
    out: dict[ObjectId, ObjectId] = {}

    def walk(oid: ObjectId, root: ObjectId) -> None:
        if oid not in slide_read.objects or oid in out:
            return
        out[oid] = root
        for c in slide_read.objects[oid].children or ():
            walk(c, root)
    for t in slide_read.order:
        walk(t, t)
    return out


def _folded_into_a_group_of_their_own(base_slide: SlideEntry | None, slide_read: SlideRead, made: set[ObjectId],
                                      text: ObjectId, shape: ObjectId) -> bool:
    """The sync could not have drawn that shape under that text without undoing an edit.

    Z-order is written in two places: the page's element order (`sync.Sync.restack`) and the
    children of a group this sync rebuilds (`regroup_requests`). Neither reaches a converter element
    the person has folded into a group of *their own*: its page element is that group, so restacking
    it moves everything else the person put in there, and the children of a group nobody rebuilds
    cannot be reordered at all. With the hidden text in another page element, the source's order
    across the two is simply unrealisable, and the sync honouring the grouping is not what hid the
    words - so this is a note, and `Sync.finish` names it in the report (`sync.folded_hiders`).

    The converter's own containers are known from the base, which records its groups
    (`merge.user_objects` asks it the same question) and its elements' objects; `made` is what this
    very sync created, whose containers are its own too."""
    ours: set[ObjectId] = set()
    if base_slide is not None:
        ours = {o for el in base_slide.elements for o in el.objects}
        ours |= set(() if base_slide.seen is None else base_slide.seen.groups)
    top = page_elements(slide_read)
    stands_in = top.get(shape)
    return (stands_in is not None and stands_in != shape and stands_in not in ours
            and stands_in not in made and top.get(text) != stands_in)


def element_of(skey: str, base_slide: SlideEntry | None, slide_read: SlideRead,
               ours_slide: Sequence[ElementEntry] | None) -> dict[ObjectId, ElementEntry]:
    """object id -> the element it carries on this read-back: a base element, or - for an object the
    sync has just created - the *ours* element it was made for. Naming only base elements left an
    element the source added nameless, and `_source_stacks_above` can excuse nothing it cannot name:
    a panel a new source draws over its own text was then reported as hiding it, every time."""
    out: dict[ObjectId, ElementEntry] = {}
    for el in () if base_slide is None else base_slide.elements:
        for oid in element_objects_of(skey, el.key, el, slide_read):
            out.setdefault(oid, el)
    for el in ours_slide or ():
        for oid in element_objects_of(skey, el.key, None, slide_read):
            out.setdefault(oid, el)
    return out


def _element_of(skey: str, base_slide: JsonMap | None, slide_read: JsonMap,
                ours_slide: Sequence[JsonObject] | None) -> dict[str, JsonObject]:
    """`element_of` over dicts (layout_oracle's view): the element entries as they came."""
    out: dict[str, JsonObject] = {}
    titles = list(_titles(slide_read))
    base_elements = as_array(base_slide["elements"], "slide.elements") if base_slide else []
    for el in base_elements:
        entry = as_object(el, "slide.elements[]")
        key = as_str(entry["key"], "element.key")
        for oid in _carriers(skey, key, set(_strings(entry.get("objects"))), titles):
            out.setdefault(oid, entry)
    for entry in ours_slide or ():
        key = as_str(entry["key"], "element.key")
        for oid in _carriers(skey, key, set(), titles):
            out.setdefault(oid, entry)
    return out


def _source_stacks_above(ours_slide: Sequence[ElementEntry] | None, text_key: str | None, shape_key: str | None) -> bool:
    """The new conversion itself stacks that shape above that text (emit creates elements in order).
    Only the order is asked, not whether the boxes meet there: a text the person dragged under a
    panel the source then moved meets it only in the deck, and the sync keeping the source's
    stacking is not what hid it."""
    keys: list[str] = [e.key for e in ours_slide or ()]
    return text_key in keys and shape_key in keys and shape_key is not None and text_key is not None \
        and keys.index(shape_key) > keys.index(text_key)


def uncertain_slides_of(base: Base, ours: merge.Ours | None) -> set[str]:
    """The base keys of the slides this very sync told the person it may have matched wrongly.

    Which frame is which is the one question a three-way merge cannot answer for itself, and the
    two passes that come closest both say so out loud: a label the content says is on another
    frame now (`identity.label_moves` -> a `label` conflict per slide) and a pairing the words
    could as well have made with the slide next door (`identity.align_slides`' weak pairs -> a
    warning naming the slide and asking for a label). What follows on such a slide - the frame's
    panels drawn over the person's text there - is the frame landing where the report said it
    might land, so it is not a loss in silence, which is the only thing this oracle judges. A
    slide nobody was warned about is not in here, and a finding on it still fails."""
    out: set[str] = set()
    if ours is None:
        return out
    for m in ours.label_moves:
        # `sync.build_ours` puts the base's keys on these before the report is written
        out.update(k for k in (m.slide, m.frame_is) if k is not None)
    for j, how in ours.weak.items():
        i, o = ours.pairs.get(j), ours.slides[j]
        # exactly the warning `merge.plan_merge` writes. `crossed` - two labels that changed
        # places over slides that say the same thing - is a warning too and is deliberately left
        # out: nothing in the campaigns has ever failed on such a slide, and an excuse nothing
        # needs is the one way to make sure nothing is caught. Put it in when a round asks for it.
        if i is not None and how in ("place", "twins") and not o.label:
            out.add(base.slides[i].key)
    return out


def ours_of(ours: JsonMap | None) -> merge.Ours | None:
    """The new conversion a dict caller hands in (nothing when it has none)."""
    return merge.ours_of(ours) if ours else None


def uncertain_slides(base: JsonMap, ours: JsonMap | None) -> set[str]:
    return uncertain_slides_of(parse_base(base), ours_of(ours))


def occlusion_findings_of(base: Base, before: DeckRead, after: DeckRead, ours: merge.Ours | None) -> list[Finding]:
    """A text that could be read before the sync, under an opaque shape the sync created after it.
    The text is the same object, or the object the sync made for the same element; it counts as
    readable before when at least one of its objects then was.

    A shape lands on a slide because a frame was written there, so on a slide whose identity the
    report itself calls uncertain (`uncertain_slides`) this is a note, not a failure. So is a shape
    the person folded into a group of their own (`_folded_into_a_group_of_their_own`), which no
    ordering the sync may write can reach."""
    out: list[Finding] = []
    unsure = uncertain_slides_of(base, ours)
    base_by_id = {s.object_id: s for s in base.slides if s.object_id}
    after_by_id = _by_id(after)
    ours_by_key: dict[str, tuple[ElementEntry, ...]] = {} if ours is None else {s.key: s.elements for s in ours.slides}
    for bs in before.slides:
        a = after_by_id.get(bs.object_id)
        if a is None:
            continue
        b = base_by_id.get(bs.object_id)
        skey = b.key if b else bs.object_id
        new = set(a.objects) - set(bs.objects)
        if not any(opaque(a.objects[x]) for x in new):
            continue
        order_before, order_after = paint_order(bs), paint_order(a)
        ours_slide = ours_by_key.get(skey)
        el_after, el_before = element_of(skey, b, a, ours_slide), element_of(skey, b, bs, None)
        for oid in order_after:
            rb = a.objects[oid]
            if not readable(rb):
                continue
            over = [x for x in hiders(a, oid, order_after) if x in new]
            if not over:
                continue
            el = el_after.get(oid)
            was = [oid] if oid in bs.objects else \
                [x for x, e in el_before.items() if el is not None and e.key == el.key]
            was = [x for x in was if readable(bs.objects[x])]
            if not was or all(hiders(bs, x, order_before) for x in was):
                continue  # nothing to read there before, or it was hidden already
            ekey = None if el is None else el.key
            over = [x for x in over
                    if not _source_stacks_above(ours_slide, ekey, None if (e := el_after.get(x)) is None else e.key)]
            folded = [x for x in over if _folded_into_a_group_of_their_own(b, a, new, oid, x)]
            said = skey in unsure
            groups: tuple[tuple[list[ObjectId], Severity, str], ...] = (
                (folded, "note", " - in a group the person made, where the "
                                 "source's order across two page elements cannot be realised"),
                ([x for x in over if x not in folded], "note" if said else "loss",
                 " - on a slide the report says may be the wrong one" if said else ""))
            for over_, sev, why in groups:
                if over_:
                    out.append(Finding(kind="text_hidden", severity=sev, slide=skey, element=ekey, object=oid,
                                       detail=f"{norm(rb.text)[:60]!r} is under {over_}, which the sync created "
                                              "(opaque, above it in z-order); it could be read before" + why))
    return out


# ---------------------------------------------------------------- an honest report

def through_theme(before: SlideRead, after: SlideRead, rep: OracleReport) -> bool:
    """A slide inheriting its background shows the master's and its layout's decoration: the
    source's new one reaches it by the theme batch (theme_sync), with nothing written on the slide
    itself - honestly applied when the report writes the master or that very layout."""
    inherit: JsonObject = {"state": "INHERIT"}
    if before.background != inherit or after.background != inherit:
        return False
    pages = {after.layout_object_id, before.layout_object_id}
    return any(e.page in pages or "master background" in e.fields
               for e in rep.applied if {"theme decoration", "master background"} & set(e.fields))


def report_findings_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    base_by_key: dict[str, SlideEntry] = {s.key: s for s in base.slides}
    applied_keys = {(e.slide, e.element) for e in rep.applied}
    for e in rep.applied:
        skey, ekey = e.slide, e.element
        b = None if skey is None else base_by_key.get(skey)
        live = None if b is None else _live(b, before_by_id, after_by_id)
        if b is None or live is None or skey is None:
            continue  # a created or deleted slide: slide_findings judges those
        bs, a = live
        fields = list(e.fields)
        el = next((x for x in b.elements if x.key == ekey), None)
        if ekey is None:
            for field in fields:
                if (field == "background" and snapshot.same_background(bs.background, a.background)
                        and not through_theme(bs, a, rep)):
                    out.append(Finding(kind="applied_no_change", severity="report", slide=skey, element=None, object=None,
                                       detail="the report applies a background that didn't change"))
                if field == "notes" and norm(bs.notes) == norm(a.notes):
                    out.append(Finding(kind="applied_no_change", severity="report", slide=skey, element=None, object=None,
                                       detail="the report applies notes that didn't change"))
            continue
        if fields == ["added"]:
            if not element_objects_of(skey, ekey, el, a):
                out.append(Finding(kind="added_element_missing", severity="report", slide=skey, element=ekey, object=None,
                                   detail="the report adds an element that isn't there"))
            continue
        if fields == ["removed"]:
            if el and element_objects_of(skey, ekey, el, a) & set(el.objects):
                out.append(Finding(kind="reported_remove_not_done", severity="report", slide=skey, element=ekey,
                                   object=None, detail="the report removes an element that is still there"))
            continue
        if el and not _element_changed(skey, el, bs, a):
            out.append(Finding(kind="applied_no_change", severity="report", slide=skey, element=ekey, object=None,
                               detail=f"the report applies {fields} but nothing on that element changed"))
    for e in rep.converged:
        skey, ekey = e.slide, e.element
        b = None if skey is None else base_by_key.get(skey)
        live = None if b is None else _live(b, before_by_id, after_by_id)
        if b is None or live is None or skey is None or ekey is None or (skey, ekey) in applied_keys:
            continue
        bs, a = live
        el = next((x for x in b.elements if x.key == ekey), None)
        if el and element_objects_of(skey, ekey, el, bs) and _element_changed(skey, el, bs, a):
            out.append(Finding(kind="converged_but_changed", severity="report", slide=skey, element=ekey, object=None,
                               detail=f"the report calls {e.field} converged (nothing written) but the element changed"))
    out += unreported_deletions(base, before, after, rep)
    return out


def _element_changed(skey: str, el: ElementEntry, bs: SlideRead, a: SlideRead) -> bool:
    was, now = element_objects_of(skey, el.key, el, bs), element_objects_of(skey, el.key, el, a)
    if was != now:
        return True
    return any(object_state(bs.objects[oid]) != object_state(a.objects[oid]) for oid in was)


def unreported_deletions(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport) -> list[Finding]:
    """Converter objects that were alive before the sync and are gone after it, with no entry in the
    report for their element."""
    out: list[Finding] = []
    before_by_id, after_by_id = _by_id(before), _by_id(after)
    for b in base.slides:
        live = _live(b, before_by_id, after_by_id)
        if live is None:
            continue
        bs, a = live
        skey = b.key
        for el in b.elements:
            alive = [o for o in el.objects if o in bs.objects]
            gone = [o for o in alive if o not in a.objects]
            if not gone or len(gone) < len(alive) and element_objects_of(skey, el.key, el, a):
                continue
            keys = {el.key, unit_key(el)}
            if _at(rep.applied, skey, keys) or _at(rep.conflicts, skey, keys):
                continue
            out.append(Finding(kind="object_deleted_unreported", severity="loss", slide=skey, element=el.key,
                               object=gone[0], detail=f"{gone} of {el.kind}/{el.role} were deleted with nothing in the report"))
    return out


# ---------------------------------------------------------------- the oracle

def check_of(base: Base, before: DeckRead, after: DeckRead, rep: OracleReport, ours: merge.Ours | None) -> list[Finding]:
    """Findings of one sync; empty means nothing a person (or the source) put in the deck was lost."""
    return (slide_findings_of(base, before, after, rep) + user_object_findings_of(base, before, after)
            + text_findings_of(base, before, after, rep, ours) + style_findings_of(base, before, after, rep)
            + picture_findings_of(base, before, after, rep) + geometry_findings_of(base, before, after, rep, ours)
            + content_findings_of(base, before, after, rep, ours) + user_slide_findings_of(base, before, after)
            + occlusion_findings_of(base, before, after, ours) + report_findings_of(base, before, after, rep))


def allowing(findings: Sequence[Finding], allow: Collection[str]) -> list[Finding]:
    """Findings less the ones `allow` names: kinds, "<kind>/<slide>" or "<kind>/<slide>/<element>"."""
    allowed = set(allow)
    return [f for f in findings if not ({f.kind, f"{f.kind}/{f.slide}", f"{f.kind}/{f.slide}/{f.element}"} & allowed)]


def failing(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if fails(f.severity)]


def described(findings: Iterable[Finding]) -> str:
    return "\n".join(f"  [{f.severity}] {f.kind} {f.slide}" + (f" / {f.element}" if f.element else "")
                     + (f" ({f.object})" if f.object else "") + f": {f.detail}" for f in findings)


# ---------------------------------------------------------------- the dict entries

def _inputs(base: JsonMap, before: JsonMap, after: JsonMap) -> tuple[Base, DeckRead, DeckRead]:
    return parse_base(base), deck_read(before), deck_read(after)


def _json(findings: Iterable[Finding]) -> list[JsonObject]:
    return [finding_json(f) for f in findings]


def check(base: JsonMap, before: JsonMap, after: JsonMap, report: JsonMap | None, ours: JsonMap | None) -> list[JsonObject]:
    """`check_of` over the snapshots as JSON (`report`: nested or flattened), findings as JSON."""
    return _json(check_of(*_inputs(base, before, after), report_of(report), ours_of(ours)))


def order_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
    return _json(order_findings_of(*_inputs(base, before, after), report_of(rep)))


def geometry_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap,
                      ours: JsonMap | None) -> list[JsonObject]:
    return _json(geometry_findings_of(*_inputs(base, before, after), report_of(rep), ours_of(ours)))


def picture_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
    return _json(picture_findings_of(*_inputs(base, before, after), report_of(rep)))


def style_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
    return _json(style_findings_of(*_inputs(base, before, after), report_of(rep)))


def user_slide_findings(base: JsonMap, before: JsonMap, after: JsonMap) -> list[JsonObject]:
    return _json(user_slide_findings_of(*_inputs(base, before, after)))


def occlusion_findings(base: JsonMap, before: JsonMap, after: JsonMap, ours: JsonMap | None) -> list[JsonObject]:
    return _json(occlusion_findings_of(*_inputs(base, before, after), ours_of(ours)))


def report_findings(base: JsonMap, before: JsonMap, after: JsonMap, rep: JsonMap) -> list[JsonObject]:
    return _json(report_findings_of(*_inputs(base, before, after), report_of(rep)))


def deck_placement(base: JsonMap) -> Placement | None:
    return deck_placement_of(parse_base(base))


def _severity(f: JsonMap) -> Json:
    return f.get("severity")


def failures(findings: Sequence[JsonObject]) -> list[JsonObject]:
    """The findings (as JSON) that fail a sync."""
    return [f for f in findings if any(_severity(f) == s and fails(s) for s in SEVERITIES)]


def describe(findings: Sequence[JsonObject]) -> str:
    return "\n".join(f"  [{f['severity']}] {f['kind']} {f['slide']}"
                     + (f" / {f['element']}" if f.get("element") else "")
                     + (f" ({f['object']})" if f.get("object") else "") + f": {f['detail']}" for f in findings)


NAMES = {"base": ("base-before", "base"), "before": ("before",), "after": ("after",),
         "report": ("report", "sync-report"), "ours": ("ours",)}


def _load(folder: Path, name: str) -> JsonObject | None:
    """One snapshot, in a fuzz run folder or in a deck's `<out>/sync` (`sync-report.json`, and the
    base before the sync under `base-before.json`: `base.json` there is the one the sync wrote)."""
    for where in (folder, folder / "sync"):
        for stem in NAMES[name]:
            path = where / f"{stem}.json"
            if path.exists():
                return as_object(json.loads(path.read_text(encoding="utf-8")), str(path))
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print((__doc__ or "").strip().splitlines()[-1].strip())
        return 2
    folder = Path(sys.argv[1])
    base, before, after = _load(folder, "base"), _load(folder, "before"), _load(folder, "after")
    if base is None or before is None or after is None:
        missing = [n for n, v in (("base", base), ("before", before), ("after", after)) if v is None]
        print(f"{folder}: no {', '.join(m + '.json' for m in missing)} to judge (a sync records the "
              f"read-backs only when it is run by tools/fuzz_sync.py)")
        return 2
    found = check_of(parse_base(base), deck_read(before), deck_read(after), report_of(_load(folder, "report")),
                     ours_of(_load(folder, "ours")))
    print(described(found) if found else "nothing lost")
    return 1 if failing(found) else 0


if __name__ == "__main__":
    sys.exit(main())
