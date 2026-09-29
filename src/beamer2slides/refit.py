"""Merged text into a recreated box: the pictures, the box and the panel made to fit what was written.

When sync recreates a unit, emit builds it for the *source's* words: the box as tall as they need,
each formula picture placed over its hole by measuring the source's text on a scratch slide
(`Sync.measure_places`). Then `Sync.override_requests` writes the *person's* words back in (the
three-way merge of the text), their font and their geometry - and nothing measured again. Slides
does not autofit, so a longer merged text ran out of the bottom of its box and of the block panel
under it, and every formula hole after the first changed word moved while its picture stayed where
the source's text had it (live fuzz r903, r905, r1104, r619, r2703; the layout probe
`layout-stranded-formula`: 72 pt).

This plans the one batch that puts it right, from two read-backs of the slide: `pre`, as the unit
was created (the source's text; its pictures where measuring put them), and `final`, after the
overrides. Both are laid out with `text_layout.layout`, so what is used is how far a hole or a text
*moved* between the two, never the model's absolute answer - on a box emit made, the model's own
error is the same on both sides and cancels:

* a picture paired with a hole in `pre` (the cheapest assignment within a line) keeps its offset
  from that hole, the hole found again in the final text through the characters the merge kept
  (`difflib`). A hole the merge removed (the person deleted the words around it) leaves its picture
  where it is, and the report says so. The hole's move is measured in the box *as the converter
  made it* (the words as written, laid out in `pre`'s box), because that is what the base records:
  where the converter puts the picture for these words. The person's own geometry E (the carried
  move and size, the same on every member of the unit) goes on top of that, as it does on everything
  else of the unit, so the page step is E . T . E^-1. Measured in the person's box instead, a group
  they had scaled by 1.15 took the step unscaled, and a picture that stood where the person had it,
  words unchanged, moved 23 pt against its own text (live fuzz r8011).
* a box whose final text needs more room below than the source's did in `pre` grows downwards by
  the difference (`text_layout.needed_bottom`, emit's rule), top-aligned boxes only (a middle- or
  bottom-anchored one would move its words).
* a filled panel the text sits on (a block's body) grows with it, keeping the margin it had under
  the words in `pre`, as far as the strip below it is clear of anything else and of the page; what
  it cannot give is reported.

Every change is a page-space step S on one object, written as it is: Slides applies a RELATIVE
transform to a group's child in page space, on the child's absolute transform, whatever the group's
own transform is. (Conjugating it by the group's, G^-1 . S . G, was a no-op on the converter's
identity groups and wrong on any group a person had moved or scaled: live fuzz r8011, a picture in a
group scaled by 1.15 moved 134.0 pt for a planned 154.1; r8006, a box grown about its top in a group
moved by (20, -20) had its top rise 4.0 pt.) The new base must record the step as the converter's
doing, not the person's, or the next sync reads a picture that no longer follows its unit's step
and freezes the unit (`merge.geometry_writable`), or reverses the panel's growth: `reshape_base`
moves each such object's base read-back R to R . F^-1 . S . F (F: its transform before the step),
which leaves the person's own step E (deck = E . base) the same on every member of the unit,
recomputes the boxes of the groups holding it, and notes on the read-back how far the step moved
its corner in the base's frame (`refit`: what the loss oracle holds the next sync's carried place
to). The sync's report lists the same moves (`refit` in the report).

Read-backs come in as JSON (`snapshot.read_slide`'s dicts, a base's entries); the jobs, the steps
and the moves are records.
"""

import difflib
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from . import snapshot
from . import text_layout as tl
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_object, as_objects, as_str

MOVE_MIN = 0.3        # pt: a picture closer than this to where it belongs stays put
GROW_MIN = 0.5        # pt: a box or a panel short by less than this stays as it is
PAIR_X = 12.0         # pt: a picture this far across from a hole in `pre` is not over it
PAIR_LINES = 1.0      # of a line: this far up or down
PANEL_SLACK = 3.0     # pt: as `layout_oracle.PANEL_SLACK` - a text inside a panel by this much sits on it
CLEAR = 1.0           # pt: what the grown panel keeps clear of the next thing below it

JsonMap = Mapping[str, Json]
What = Literal["picture", "box", "panel"]


@dataclass(frozen=True, kw_only=True)
class RefitJob:
    """One recreated text unit whose deck edits were written over it. key: for the report; slide
    and names (new id -> element key): where `moves` says it happened; text: its text box's new id;
    pictures: the new ids of the formula pictures anchored to it; own: every new id of the unit,
    groups included; doomed: ids the sync deletes afterwards; theirs: the person's box before the
    sync, or None."""
    key: str
    slide: str | None
    names: Mapping[str, str]
    text: str
    pictures: tuple[str, ...]
    own: frozenset[str]
    doomed: frozenset[str]
    theirs: JsonObject | None


@dataclass(frozen=True, kw_only=True)
class Reshape:
    """A page step on one object and its transform before it."""
    step: list[float]
    before: list[float]


@dataclass(frozen=True, kw_only=True)
class Moved:
    """What `plan` moved or grew, for the sync's report."""
    slide: str | None
    element: str | None
    object: str
    what: What
    shift: list[float]     # how far its corner moved in the converter's frame
    grown: float           # pt of height it gained on the page


def moved_json(m: Moved) -> JsonObject:
    shift: list[Json] = [v for v in m.shift]
    return {"slide": m.slide, "element": m.element, "object": m.object, "what": m.what, "shift": shift,
            "grown": m.grown}


def _objects(slide: JsonMap | None) -> dict[str, JsonObject]:
    """A slide read-back's objects by id."""
    v = slide.get("objects") if slide else None
    return {} if not v else {o: as_object(rb, o) for o, rb in as_object(v, "objects").items()}


def _matrix(rb: JsonMap) -> list[float]:
    return tl.numbers(rb["transform"], "transform")


def _box(rb: JsonMap) -> tl.Rect:
    return tl.rect(rb["box"], "box")


def _step_request(oid: str, step: list[float]) -> JsonObject:
    """The page-space `step` on one object, in or out of a group: Slides applies a RELATIVE
    transform to a child's absolute transform (module docstring; r8011, r8006)."""
    from .sync import matrix_request
    return matrix_request(oid, step)


def carried_step(step: list[float], edit: list[float]) -> list[float]:
    """The page step that moves an object by `step` in the frame the converter made it in, when the
    person's `edit` (deck = edit . converter's) stands on it: edit . step . edit^-1."""
    return snapshot.compose(edit, snapshot.compose(step, snapshot.invert(edit)))


def base_shift(rb: JsonMap, step: list[float], before: list[float]) -> list[float]:
    """How far the page `step` on an object whose transform was `before` moves the corner of its
    base read-back `rb` (`_reshaped`): the step in the converter's frame."""
    _, moved = _moved(rb, step, before)
    b = _box(rb)
    return [round(moved[0] - b[0], 2), round(moved[1] - b[1], 2)]


def _centre_x(b: Sequence[float]) -> float:
    return (b[0] + b[2]) / 2


def pair_pictures(lay: tl.Layout, pictures: Mapping[str, JsonMap]) -> dict[str, int]:
    """Picture object -> index of the hole in `lay` it stands over (as measuring placed it): the
    cheapest assignment by distance, only within `PAIR_X` across and `PAIR_LINES` of a line."""
    from .emit import LINE_EM
    holes = lay.holes

    def cost(oid: str, k: int) -> float | None:
        b, h = _box(pictures[oid]), holes[k].box
        z = lay.lines[holes[k].line].size
        dx = abs(_centre_x(b) - _centre_x(h))
        dy = abs((b[1] + b[3]) / 2 - (h[1] + h[3]) / 2)
        return dx + dy if dx <= PAIR_X and dy <= PAIR_LINES * LINE_EM * z else None

    pics = sorted(pictures)
    if not pics or not holes:
        return {}
    if len(pics) <= 6 and len(holes) <= 8:
        best: tuple[tuple[int, float], dict[str, int]] | None = None
        n = min(len(pics), len(holes))
        for chosen in itertools.combinations(pics, n) if len(pics) > len(holes) else [tuple(pics)]:
            for perm in itertools.permutations(range(len(holes)), n):
                cs = [cost(o, k) for o, k in zip(chosen, perm)]
                pairs = {o: k for o, k, c in zip(chosen, perm, cs) if c is not None}
                total = (-len(pairs), sum(c for c in cs if c is not None))
                if best is None or total < best[0]:
                    best = (total, pairs)
        return {} if best is None else best[1]
    out: dict[str, int] = {}
    used: set[int] = set()
    for _, o, k in sorted((c, o, k) for o in pics for k in range(len(holes)) if (c := cost(o, k)) is not None):
        if o not in out and k not in used:
            out[o] = k
            used.add(k)
    return out


def map_index(matcher: difflib.SequenceMatcher[str], i: int) -> int | None:
    """Where character i of the matcher's first text is in its second, if the diff kept it."""
    for a, b, size in matcher.get_matching_blocks():
        if a <= i < a + size:
            return b + (i - a)
    return None


def find_hole(pre: tl.Layout, fin: tl.Layout, matcher: difflib.SequenceMatcher[str], k: int) -> tl.Hole | None:
    """The final layout's hole that is `pre`'s hole k, through the characters the merge kept."""
    h = pre.holes[k]
    for i in range(h.start, h.end):
        j = map_index(matcher, i)
        if j is None:
            continue
        for fh in fin.holes:
            if fh.start <= j < fh.end:
                return fh
    return None


def _ink(obj: JsonMap) -> list[tl.Rect]:
    """What an object draws, as rectangles: a text's lines, anything else its box."""
    if tl.text_box(obj):
        lay = tl.layout(obj)
        return [] if lay is None else tl.line_rects(lay)
    b = tl.box_of(obj)
    return [b] if b is not None else []


def sits_on(lay: tl.Layout, box: tl.Rect, panel: tl.Rect) -> bool:
    """As `layout_oracle.panel_findings` asks it: the words start inside the panel and mostly across
    it, the box's top is inside it and its bottom by the panel's."""
    left = min(ln.box[0] for ln in lay.lines)
    right = max(ln.box[2] for ln in lay.lines)
    across = min(right, panel[2]) - max(left, panel[0])
    return (panel[0] - PANEL_SLACK <= left and across >= 0.8 * (right - left)
            and panel[1] - PANEL_SLACK <= box[1] < panel[3] - 2 and box[3] <= panel[3] + PANEL_SLACK)


def _words(obj: JsonMap) -> str:
    v = obj.get("text")
    return " ".join((as_str(v, "text") if v else "").split())[:40]


def _area(b: tl.Rect) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def plan(jobs: Sequence[RefitJob], pre: JsonMap, final: JsonMap, page: Sequence[float] | None,
         before: JsonMap | None) -> tuple[list[JsonObject], dict[str, Reshape], list[str]]:
    """The requests that fit one slide's recreated boxes to the text written into them.

    jobs: one per recreated text unit (`RefitJob`). pre / final: `snapshot.read_slide` of the slide
    as created and after the overrides; page: [w, h] pt; before: the slide as the person had it
    before the sync (what their own edits did is theirs to keep), or None.
    Returns (requests, reshaped {oid: its page step and the transform before it}, warnings)."""
    reqs: list[JsonObject] = []
    reshaped: dict[str, Reshape] = {}
    warnings: list[str] = []
    pre_o, fin_o, had_o = _objects(pre), _objects(final), _objects(before)
    for job in jobs:
        t = job.text
        p_rb, f_rb = pre_o.get(t), fin_o.get(t)
        if not p_rb or not f_rb or not tl.upright(f_rb):
            continue
        # geo: the source's words in the box as the overrides left it - what the geometry alone did to
        # the words' extent, which is the person's to keep (a box they narrowed wraps more lines)
        g_rb: JsonObject = {**p_rb, "box": f_rb["box"], "transform": f_rb["transform"],
                            "shape_style": f_rb.get("shape_style")}
        p_lay, g_lay, f_lay = tl.layout(p_rb), tl.layout(g_rb), tl.layout(f_rb)
        if p_lay is None or g_lay is None or f_lay is None:
            continue
        where = f"{job.key} ({_words(f_rb)!r})"

        # -- formula pictures follow their holes from where the source's words had them to the words
        # as written (the same hole in both, through the characters the merge kept), both laid out
        # in the box as the converter made it; the person's geometry goes on top (module docstring)
        pics = {o: pre_o[o] for o in job.pictures if o in pre_o and o in fin_o}
        pairs = pair_pictures(p_lay, pics)
        m_lay = tl.layout({**f_rb, "box": p_rb["box"], "transform": p_rb["transform"]}) if pairs else None
        matcher = difflib.SequenceMatcher(None, p_lay.text, m_lay.text, autojunk=False) if m_lay else None
        for o, k in sorted(pairs.items()):
            ph = p_lay.holes[k]
            mh = find_hole(p_lay, m_lay, matcher, k) if m_lay is not None and matcher is not None else None
            if mh is None or m_lay is None:
                warnings.append(f"{where}: a formula picture lost its place in the words as merged (the deck's edit "
                                "took out the words around it); it stays where it stood")
                continue
            dx = _centre_x(mh.box) - _centre_x(ph.box)
            dy = m_lay.lines[mh.line].baseline - p_lay.lines[ph.line].baseline
            if max(abs(dx), abs(dy)) <= MOVE_MIN:
                continue
            f_pic = _matrix(fin_o[o])
            edit = snapshot.compose(f_pic, snapshot.invert(_matrix(pics[o])))   # the person's, carried
            step = carried_step([1, 0, 0, 1, dx, dy], edit)
            reqs.append(_step_request(o, step))
            reshaped[o] = Reshape(step=step, before=f_pic)

        # -- the box as tall as the words written into it need, beyond what the source's words
        # needed of it (the model's own error on emit's box, and whatever the geometry alone did)
        align = as_object(f_rb.get("shape_style") or {}, "shape_style").get("align")
        bottom = f_lay.box[3]
        slack = max(tl.needed_bottom(p_lay) - p_lay.box[3], tl.needed_bottom(g_lay) - bottom)
        theirs = job.theirs
        t_lay = tl.layout(theirs) if theirs and tl.text_box(theirs) and tl.upright(theirs) else None
        if t_lay is not None:
            slack = max(slack, tl.needed_bottom(t_lay) - t_lay.box[3])   # the person's own overflow stays theirs
        grow = tl.needed_bottom(f_lay) - bottom - slack
        top = f_lay.box[1]
        # A recreated title goes back into its live placeholder, at the box already found there
        # (`sync.update_slide`'s `in_place`) - that box may be the person's own resize, never the
        # converter's to grow (module docstring, house rule: never change the person's own
        # geometry). Only the panel below it, the converter's own object, is fitted.
        if grow > GROW_MIN and not f_rb.get("placeholder"):
            if page and bottom + grow > page[1]:
                warnings.append(f"{where}: the words as merged need {grow:.1f} pt more than the box has, and the page "
                                f"ends {max(0.0, page[1] - bottom):.1f} pt below it")
                grow = page[1] - bottom       # (a box is not made to reach off the page)
            if grow > GROW_MIN and align in (None, "TOP") and bottom - top > 1:
                k_ = (bottom - top + grow) / (bottom - top)
                step = [1, 0, 0, k_, 0, top * (1 - k_)]
                reqs.append(_step_request(t, step))
                reshaped[t] = Reshape(step=step, before=_matrix(f_rb))
            elif grow > GROW_MIN and align not in (None, "TOP"):
                anchor = align.lower() if isinstance(align, str) else str(align)
                warnings.append(f"{where}: the words as merged need {grow:.1f} pt more than the box has; it is anchored "
                                f"{anchor}, so growing it would move them - not resized")

        # -- the panel under it
        panels = [(oid, rb, _box(rb)) for oid, rb in fin_o.items()
                  if oid not in job.own and oid not in job.doomed and tl.filled(rb) and tl.upright(rb)
                  and sits_on(f_lay, f_lay.box, _box(rb))]
        if not panels:
            continue
        pid, panel, pb = min(panels, key=lambda kv: _area(kv[2]))
        # the margin the panel had under the source's words is what the words as merged get, less
        # whatever the geometry alone overran, or the person's own words did before the sync
        # (their larger font say): that look is theirs
        was = pre_o.get(pid)
        was_box = _box(was) if was else None
        pad = max(0.0, was_box[3] - p_lay.bottom) if was_box is not None and sits_on(p_lay, p_lay.box, was_box) else 0.0
        overran = [0.0, g_lay.bottom + pad - pb[3]]
        had = had_o.get(pid)
        if had and t_lay is not None and tl.upright(had):
            overran.append(t_lay.bottom + pad - _box(had)[3])
        need = f_lay.bottom + pad - pb[3] - max(overran)
        if need <= GROW_MIN:
            continue
        room = (page[1] - pb[3]) if page else need
        blocker: JsonObject | None = None
        for oid, rb in fin_o.items():
            if oid in job.own or oid in job.doomed or oid == pid or rb.get("kind") == "elementGroup":
                continue
            rb_box = tl.box_of(rb)
            if rb_box is None or (rb_box[0] <= pb[0] + 0.5 and rb_box[1] <= pb[1] + 0.5
                                  and rb_box[2] >= pb[2] - 0.5 and rb_box[3] >= pb[3] - 0.5):
                continue     # (a backdrop the panel stands on)
            for r in _ink(rb):
                if r[2] <= pb[0] + 0.5 or r[0] >= pb[2] - 0.5 or r[3] <= pb[3] + 0.5:
                    continue
                if r[1] < pb[3] - 1.0 and tl.number(rb, "z") > tl.number(panel, "z"):
                    continue  # (drawn on the panel already: it stays on it)
                if r[1] - CLEAR - pb[3] < room:
                    room, blocker = max(0.0, r[1] - CLEAR - pb[3]), rb
        give = min(need, room)
        if give > GROW_MIN:
            k_ = (pb[3] - pb[1] + give) / (pb[3] - pb[1])
            step = [1, 0, 0, k_, 0, pb[1] * (1 - k_)]
            reqs.append(_step_request(pid, step))
            reshaped[pid] = Reshape(step=step, before=_matrix(panel))
        if need - give > GROW_MIN:
            what = f"{_words(blocker)!r}" if blocker is not None and _words(blocker) else \
                ("a picture" if blocker is not None and blocker.get("kind") == "image" else
                 "another object" if blocker is not None else "the bottom of the page")
            warnings.append(f"{where}: the words as merged run {need - give:.1f} pt past the bottom of the panel they "
                            f"sit on, which could only grow {max(give, 0):.1f} pt: {what} is in the way")
    return reqs, reshaped, warnings


def moves(jobs: Sequence[RefitJob], reshaped: Mapping[str, Reshape], pre: JsonMap, final: JsonMap) -> list[Moved]:
    """What `plan` moved or grew on one slide, for the sync's report (`refit`). A job's slide and
    names (object -> element key) say where; a panel is another unit's, named by its object."""
    names: dict[str, tuple[str | None, str | None, What]] = {}
    for job in jobs:
        for oid in [*job.names, job.text, *job.pictures]:
            what: What = "picture" if oid in job.pictures else "box" if oid == job.text else "panel"
            names[oid] = (job.slide, job.names.get(oid), what)
    pre_o, fin_o = _objects(pre), _objects(final)
    out: list[Moved] = []
    for oid, r in reshaped.items():
        rb, now = pre_o.get(oid), fin_o.get(oid)
        if rb is None or now is None or not rb.get("box") or not rb.get("size"):
            continue
        skey, ekey, what_ = names.get(oid, (jobs[0].slide if jobs else None, None, "panel"))
        w, h = tl.numbers(rb["size"], "size")
        box = snapshot.box(snapshot.compose(r.step, r.before), w, h)
        nb = _box(now)
        out.append(Moved(slide=skey, element=ekey, object=oid, what=what_, shift=base_shift(rb, r.step, r.before),
                         grown=round((box[3] - box[1]) - (nb[3] - nb[1]), 2)))
    return out


def _moved(rb: JsonMap, step: list[float], before: list[float]) -> tuple[list[float], list[float]]:
    """A read-back's transform and box after the page `step` on it (its transform was `before`)."""
    local = snapshot.compose(snapshot.invert(before), snapshot.compose(step, before))
    m = snapshot.compose(_matrix(rb), local)
    w, h = tl.numbers(rb["size"], "size")
    return [round(v, 4) for v in m[:4]] + [round(v, 2) for v in m[4:]], snapshot.box(m, w, h)


def _reshaped(rb: JsonMap, step: list[float], before: list[float]) -> JsonObject:
    transform, box = _moved(rb, step, before)
    out = dict(rb)
    out["transform"] = [v for v in transform]
    out["box"] = [v for v in box]
    return out


def _noted(rb: JsonMap, step: list[float], before: list[float]) -> JsonObject:
    """`_reshaped`, with the corner's move in the base's frame noted as `refit` when there is one."""
    out = _reshaped(rb, step, before)
    if rb.get("box"):
        b, moved = _box(rb), _box(out)
        shift = [round(moved[0] - b[0], 2), round(moved[1] - b[1], 2)]
        if any(abs(v) >= 0.01 for v in shift):
            out["refit"] = [v for v in shift]
    return out


def _readbacks(e: JsonMap) -> dict[str, JsonObject]:
    v = e.get("readback")
    return {} if not v else {oid: as_object(rb, oid) for oid, rb in as_object(v, "readback").items()}


def _raw_box(rb: JsonMap) -> list[int | float]:
    """A box's numbers as they are (an int stays an int in the base's JSON)."""
    out: list[int | float] = []
    for x in as_array(rb["box"], "box"):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise JsonShapeError("box: expected numbers")
        out.append(x)
    return out


def reshape_base(slides: list[JsonObject], reshaped: Mapping[str, Reshape]) -> list[JsonObject]:
    """The new base's slides with the objects `plan` stepped recorded where the step put them (see
    the module docstring), each noted with how far that moved its corner (`refit`, in the base's
    frame), and the boxes of the groups holding them made the union of their children again. Element
    dicts are copied, never changed: a kept unit's are the old base's."""
    if not reshaped:
        return slides
    out: list[JsonObject] = []
    for entry in slides:
        els = as_objects(entry.get("elements") or [], "elements")
        if not any(oid in reshaped for e in els for oid in _readbacks(e)):
            out.append(entry)
            continue
        new: list[JsonObject] = []
        for e in els:
            rbs = _readbacks(e)
            if any(oid in reshaped for oid in rbs):
                stepped: JsonObject = {}
                for oid, rb in rbs.items():
                    r = reshaped.get(oid)
                    stepped[oid] = _noted(rb, r.step, r.before) if r is not None and rb.get("size") \
                        and rb.get("transform") else rb
                e = {**e, "readback": stepped}
            new.append(e)
        every = {oid: rb for e in new for oid, rb in _readbacks(e).items()}
        for i, e in enumerate(new):
            for oid, rb in _readbacks(e).items():
                kids = [as_str(k, "children") for k in as_array(rb.get("children") or [], "children")]
                if kids and any(k in reshaped for k in kids) and all(every.get(k, {}).get("box") for k in kids):
                    boxes = [_raw_box(every[k]) for k in kids]
                    union: list[Json] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                         max(b[2] for b in boxes), max(b[3] for b in boxes)]
                    e = new[i] = {**e, "readback": {**as_object(e["readback"], "readback"), oid: {**rb, "box": union}}}
        elements: list[Json] = [e for e in new]
        out.append({**entry, "elements": elements})
    return out
