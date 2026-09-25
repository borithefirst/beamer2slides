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
"""

import difflib
import itertools

from . import snapshot

MOVE_MIN = 0.3        # pt: a picture closer than this to where it belongs stays put
GROW_MIN = 0.5        # pt: a box or a panel short by less than this stays as it is
PAIR_X = 12.0         # pt: a picture this far across from a hole in `pre` is not over it
PAIR_LINES = 1.0      # of a line: this far up or down
PANEL_SLACK = 3.0     # pt: as `layout_oracle.PANEL_SLACK` - a text inside a panel by this much sits on it
CLEAR = 1.0           # pt: what the grown panel keeps clear of the next thing below it


def _identity(m) -> bool:
    return all(abs(x - y) < 1e-6 for x, y in zip(m, (1, 0, 0, 1, 0, 0)))


def _step_request(oid: str, step: list[float]) -> dict:
    """The page-space `step` on one object, in or out of a group: Slides applies a RELATIVE
    transform to a child's absolute transform (module docstring; r8011, r8006)."""
    from .sync import matrix_request
    return matrix_request(oid, step)


def carried_step(step: list[float], edit: list[float]) -> list[float]:
    """The page step that moves an object by `step` in the frame the converter made it in, when the
    person's `edit` (deck = edit . converter's) stands on it: edit . step . edit^-1."""
    return snapshot.compose(edit, snapshot.compose(step, snapshot.invert(edit)))


def base_shift(rb: dict, step: list[float], before: list[float]) -> list[float]:
    """How far the page `step` on an object whose transform was `before` moves the corner of its
    base read-back `rb` (`_reshaped`): the step in the converter's frame."""
    moved = _reshaped(rb, step, before)
    return [round(moved["box"][0] - rb["box"][0], 2), round(moved["box"][1] - rb["box"][1], 2)]


def _centre_x(b) -> float:
    return (b[0] + b[2]) / 2


def pair_pictures(lay: dict, pictures: dict[str, dict]) -> dict[str, int]:
    """Picture object -> index of the hole in `lay` it stands over (as measuring placed it): the
    cheapest assignment by distance, only within `PAIR_X` across and `PAIR_LINES` of a line."""
    from .emit import LINE_EM
    holes = lay["holes"]

    def cost(oid, k):
        b, h = pictures[oid]["box"], holes[k]["box"]
        z = lay["lines"][holes[k]["line"]]["size"]
        dx = abs(_centre_x(b) - _centre_x(h))
        dy = abs((b[1] + b[3]) / 2 - (h[1] + h[3]) / 2)
        return dx + dy if dx <= PAIR_X and dy <= PAIR_LINES * LINE_EM * z else None

    pics = sorted(pictures)
    if not pics or not holes:
        return {}
    if len(pics) <= 6 and len(holes) <= 8:
        best = None
        n = min(len(pics), len(holes))
        for chosen in itertools.combinations(pics, n) if len(pics) > len(holes) else [pics]:
            for perm in itertools.permutations(range(len(holes)), n):
                cs = [cost(o, k) for o, k in zip(chosen, perm)]
                pairs = {o: k for o, k, c in zip(chosen, perm, cs) if c is not None}
                total = (-len(pairs), sum(c for c in cs if c is not None))
                if best is None or total < best[0]:
                    best = (total, pairs)
        return best[1]
    out, used = {}, set()
    for c, o, k in sorted((c, o, k) for o in pics for k in range(len(holes)) if (c := cost(o, k)) is not None):
        if o not in out and k not in used:
            out[o] = k
            used.add(k)
    return out


def map_index(matcher: difflib.SequenceMatcher, i: int) -> int | None:
    """Where character i of the matcher's first text is in its second, if the diff kept it."""
    for a, b, size in matcher.get_matching_blocks():
        if a <= i < a + size:
            return b + (i - a)
    return None


def find_hole(pre: dict, fin: dict, matcher, k: int) -> dict | None:
    """The final layout's hole that is `pre`'s hole k, through the characters the merge kept."""
    h = pre["holes"][k]
    for i in range(h["start"], h["end"]):
        j = map_index(matcher, i)
        if j is None:
            continue
        for fh in fin["holes"]:
            if fh["start"] <= j < fh["end"]:
                return fh
    return None


def _ink(obj: dict, size=None) -> list[list[float]]:
    """What an object draws, as rectangles: a text's lines, anything else its box."""
    from . import text_layout as tl
    if tl.text_box(obj):
        lay = tl.layout(obj, size)
        if lay is None:
            return []
        return [ln["box"] for ln in lay["lines"] if ln["box"][2] - ln["box"][0] > 0.5]
    return [list(obj["box"])] if obj.get("box") else []


def sits_on(lay: dict, box: list[float], panel: list[float]) -> bool:
    """As `layout_oracle.panel_findings` asks it: the words start inside the panel and mostly across
    it, the box's top is inside it and its bottom by the panel's."""
    left = min(ln["box"][0] for ln in lay["lines"])
    right = max(ln["box"][2] for ln in lay["lines"])
    across = min(right, panel[2]) - max(left, panel[0])
    return (panel[0] - PANEL_SLACK <= left and across >= 0.8 * (right - left)
            and panel[1] - PANEL_SLACK <= box[1] < panel[3] - 2 and box[3] <= panel[3] + PANEL_SLACK)


def _words(obj: dict) -> str:
    return " ".join((obj.get("text") or "").split())[:40]


def plan(jobs: list[dict], pre: dict, final: dict, page: list[float] | None, before: dict | None = None
         ) -> tuple[list[dict], dict, list[str]]:
    """The requests that fit one slide's recreated boxes to the text written into them.

    jobs: one per recreated text unit whose deck edits were written over it: {"key" (for the
    report), "text" (its text box's new id), "pictures" (the new ids of the formula pictures
    anchored to it), "own" (every new id of the unit, groups included), "doomed" (ids the sync
    deletes afterwards), "theirs" (the person's box before the sync, or None); for the report,
    `moves` also reads "slide" and "names" (new id -> element key)}. pre / final:
    `snapshot.read_slide` of the slide as created and after the overrides; page: [w, h] pt; before:
    the slide as the person had it before the sync (what their own edits did is theirs to keep).
    Returns (requests, reshaped {oid: (page step, transform before it)}, warnings)."""
    from . import text_layout as tl
    reqs, reshaped, warnings = [], {}, []
    for job in jobs:
        t = job["text"]
        p_rb, f_rb = pre["objects"].get(t), final["objects"].get(t)
        if not p_rb or not f_rb or not tl.upright(f_rb):
            continue
        # geo: the source's words in the box as the overrides left it - what the geometry alone did to
        # the words' extent, which is the person's to keep (a box they narrowed wraps more lines)
        g_rb = {**p_rb, "box": f_rb["box"], "transform": f_rb["transform"], "shape_style": f_rb.get("shape_style")}
        p_lay, g_lay, f_lay = tl.layout(p_rb), tl.layout(g_rb), tl.layout(f_rb)
        if p_lay is None or g_lay is None or f_lay is None:
            continue
        where = f"{job['key']} ({_words(f_rb)!r})"

        # -- formula pictures follow their holes from where the source's words had them to the words
        # as written (the same hole in both, through the characters the merge kept), both laid out
        # in the box as the converter made it; the person's geometry goes on top (module docstring)
        pics = {o: pre["objects"][o] for o in job["pictures"] if o in pre["objects"] and o in final["objects"]}
        pairs = pair_pictures(p_lay, pics)
        m_lay = tl.layout({**f_rb, "box": p_rb["box"], "transform": p_rb["transform"]}) if pairs else None
        matcher = difflib.SequenceMatcher(None, p_lay["text"], m_lay["text"], autojunk=False) if m_lay else None
        for o, k in sorted(pairs.items()):
            ph = p_lay["holes"][k]
            mh = find_hole(p_lay, m_lay, matcher, k) if m_lay else None
            if mh is None:
                warnings.append(f"{where}: a formula picture lost its place in the words as merged (the deck's edit "
                                "took out the words around it); it stays where it stood")
                continue
            dx = _centre_x(mh["box"]) - _centre_x(ph["box"])
            dy = m_lay["lines"][mh["line"]]["baseline"] - p_lay["lines"][ph["line"]]["baseline"]
            if max(abs(dx), abs(dy)) <= MOVE_MIN:
                continue
            f_pic = list(final["objects"][o]["transform"])
            edit = snapshot.compose(f_pic, snapshot.invert(pics[o]["transform"]))   # the person's, carried
            step = carried_step([1, 0, 0, 1, dx, dy], edit)
            reqs.append(_step_request(o, step))
            reshaped[o] = (step, f_pic)

        # -- the box as tall as the words written into it need, beyond what the source's words
        # needed of it (the model's own error on emit's box, and whatever the geometry alone did)
        align = (f_rb.get("shape_style") or {}).get("align")
        bottom = f_rb["box"][3]
        slack = max(tl.needed_bottom(p_lay) - p_rb["box"][3], tl.needed_bottom(g_lay) - bottom)
        theirs = job.get("theirs")
        t_lay = tl.layout(theirs) if theirs and tl.text_box(theirs) and tl.upright(theirs) else None
        if t_lay is not None:
            slack = max(slack, tl.needed_bottom(t_lay) - theirs["box"][3])   # the person's own overflow stays theirs
        grow = tl.needed_bottom(f_lay) - bottom - slack
        top = f_rb["box"][1]
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
                reshaped[t] = (step, list(f_rb["transform"]))
            elif grow > GROW_MIN and align not in (None, "TOP"):
                warnings.append(f"{where}: the words as merged need {grow:.1f} pt more than the box has; it is anchored "
                                f"{align.lower()}, so growing it would move them - not resized")

        # -- the panel under it
        panels = [(oid, rb) for oid, rb in final["objects"].items()
                  if oid not in job["own"] and oid not in job["doomed"] and tl.filled(rb) and tl.upright(rb)
                  and sits_on(f_lay, f_rb["box"], rb["box"])]
        if not panels:
            continue
        pid, panel = min(panels, key=lambda kv: (kv[1]["box"][2] - kv[1]["box"][0]) * (kv[1]["box"][3] - kv[1]["box"][1]))
        # the margin the panel had under the source's words is what the words as merged get, less
        # whatever the geometry alone overran, or the person's own words did before the sync
        # (their larger font say): that look is theirs
        was = pre["objects"].get(pid)
        pad = max(0.0, was["box"][3] - p_lay["bottom"]) if was and sits_on(p_lay, p_rb["box"], was["box"]) else 0.0
        overran = [0.0, g_lay["bottom"] + pad - panel["box"][3]]
        had = ((before or {}).get("objects") or {}).get(pid)
        if had and t_lay is not None and tl.upright(had):
            overran.append(t_lay["bottom"] + pad - had["box"][3])
        need = f_lay["bottom"] + pad - panel["box"][3] - max(overran)
        if need <= GROW_MIN:
            continue
        pb = panel["box"]
        room = (page[1] - pb[3]) if page else need
        blocker = None
        for oid, rb in final["objects"].items():
            if oid in job["own"] or oid in job["doomed"] or oid == pid or rb.get("kind") == "elementGroup":
                continue
            if not rb.get("box") or (rb["box"][0] <= pb[0] + 0.5 and rb["box"][1] <= pb[1] + 0.5
                                     and rb["box"][2] >= pb[2] - 0.5 and rb["box"][3] >= pb[3] - 0.5):
                continue     # (a backdrop the panel stands on)
            for r in _ink(rb):
                if r[2] <= pb[0] + 0.5 or r[0] >= pb[2] - 0.5 or r[3] <= pb[3] + 0.5:
                    continue
                if r[1] < pb[3] - 1.0 and rb.get("z", 0) > panel.get("z", 0):
                    continue  # (drawn on the panel already: it stays on it)
                if r[1] - CLEAR - pb[3] < room:
                    room, blocker = max(0.0, r[1] - CLEAR - pb[3]), rb
        give = min(need, room)
        if give > GROW_MIN:
            k_ = (pb[3] - pb[1] + give) / (pb[3] - pb[1])
            step = [1, 0, 0, k_, 0, pb[1] * (1 - k_)]
            reqs.append(_step_request(pid, step))
            reshaped[pid] = (step, list(panel["transform"]))
        if need - give > GROW_MIN:
            what = f"{_words(blocker)!r}" if blocker is not None and _words(blocker) else \
                ("a picture" if blocker is not None and blocker.get("kind") == "image" else
                 "another object" if blocker is not None else "the bottom of the page")
            warnings.append(f"{where}: the words as merged run {need - give:.1f} pt past the bottom of the panel they "
                            f"sit on, which could only grow {max(give, 0):.1f} pt: {what} is in the way")
    return reqs, reshaped, warnings


def moves(jobs: list[dict], reshaped: dict, pre: dict, final: dict) -> list[dict]:
    """What `plan` moved or grew on one slide, for the sync's report (`refit`): {slide, element,
    object, what (picture / box / panel), shift (how far its corner moved in the converter's frame,
    as `reshape_base` records it), grown (pt of height it gained on the page)}. A job's "slide" and
    "names" (object -> element key) say where; a panel is another unit's, named by its object."""
    names = {oid: (job.get("slide"), (job.get("names") or {}).get(oid),
                   "picture" if oid in job["pictures"] else "box" if oid == job["text"] else "panel")
             for job in jobs for oid in [*(job.get("names") or {}), job["text"], *job["pictures"]]}
    out = []
    for oid, (step, before) in reshaped.items():
        rb, now = pre["objects"].get(oid), final["objects"].get(oid)
        if rb is None or now is None or not rb.get("box") or not rb.get("size"):
            continue
        skey, ekey, what = names.get(oid, (jobs[0].get("slide") if jobs else None, None, "panel"))
        box = snapshot.box(snapshot.compose(step, before), *rb["size"])
        out.append({"slide": skey, "element": ekey, "object": oid, "what": what,
                    "shift": base_shift(rb, step, before),
                    "grown": round((box[3] - box[1]) - (now["box"][3] - now["box"][1]), 2)})
    return out


def _reshaped(rb: dict, step: list[float], before: list[float]) -> dict:
    local = snapshot.compose(snapshot.invert(before), snapshot.compose(step, before))
    m = snapshot.compose(rb["transform"], local)
    out = dict(rb)
    out["transform"] = [round(v, 4) for v in m[:4]] + [round(v, 2) for v in m[4:]]
    out["box"] = snapshot.box(m, *rb["size"])
    return out


def _noted(rb: dict, step: list[float], before: list[float]) -> dict:
    """`_reshaped`, with the corner's move in the base's frame noted as `refit` when there is one."""
    out = _reshaped(rb, step, before)
    if rb.get("box"):
        shift = [round(out["box"][0] - rb["box"][0], 2), round(out["box"][1] - rb["box"][1], 2)]
        if any(abs(v) >= 0.01 for v in shift):
            out["refit"] = shift
    return out


def reshape_base(slides: list[dict], reshaped: dict[str, tuple]) -> list[dict]:
    """The new base's slides with the objects `plan` stepped recorded where the step put them (see
    the module docstring), each noted with how far that moved its corner (`refit`, in the base's
    frame), and the boxes of the groups holding them made the union of their children again. Element
    dicts are copied, never changed: a kept unit's are the old base's."""
    if not reshaped:
        return slides
    out = []
    for entry in slides:
        els = entry.get("elements") or []
        if not any(oid in reshaped for e in els for oid in (e.get("readback") or {})):
            out.append(entry)
            continue
        new = []
        for e in els:
            rbs = e.get("readback") or {}
            if any(oid in reshaped for oid in rbs):
                e = {**e, "readback": {oid: _noted(rb, *reshaped[oid]) if oid in reshaped and rb.get("size")
                                       and rb.get("transform") else rb for oid, rb in rbs.items()}}
            new.append(e)
        every = {oid: rb for e in new for oid, rb in (e.get("readback") or {}).items()}
        for i, e in enumerate(new):
            rbs = e.get("readback") or {}
            for oid, rb in rbs.items():
                kids = rb.get("children") or []
                if kids and any(k in reshaped for k in kids) and all(every.get(k, {}).get("box") for k in kids):
                    boxes = [every[k]["box"] for k in kids]
                    e = new[i] = {**e, "readback": {**e["readback"], oid: {
                        **rb, "box": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                      max(b[2] for b in boxes), max(b[3] for b in boxes)]}}}
        out.append({**entry, "elements": new})
    return out
