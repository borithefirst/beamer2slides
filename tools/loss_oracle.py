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
  coincidence, and then nothing was reverted).
* **A picture the person put into a converter element** (the read-back's picture differing from the
  base's, compared by `snapshot.signature`, never by URL) must still be on one of that element's
  objects afterwards.
* **Styling a person applied to a converter object** (the read-back's `text_style_hash` /
  `shape_style_hash` differing from the base's) must not come back as the converter's: after the
  sync at least one of that element's objects must still show the person's hash.
  These three are excused by a `conflicts` or a `converged` entry for the element, and by nothing
  else - in particular not by `overrides`, which *claims* the deck's version was kept: a sync that
  reverts what it lists as an override is exactly the silent loss this oracle looks for.
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
* **An honest report**: every `applied` entry must have changed something (ids, text, geometry,
  style or picture of that element; `added` must have produced objects, `removed` must have removed
  them), and every `converged` entry must have changed nothing. A report that claims work it did not
  do hides the work it did do, so a lying report is itself a finding.

Findings are dicts `{kind, severity, slide, element, object, detail}`. `severity` is "loss" (a
person's or the source's content is gone), "undo" (an edit was reverted), "report" (the report is
not honest) or "note" (could not be verified, e.g. picture bytes without signatures).

    python tools/loss_oracle.py <folder>   # reads base(-before).json, before.json, after.json, (sync-)report.json and ours.json, there or in its sync/
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides import identity, merge, snapshot  # noqa: E402

WORD = re.compile(r"\w+", re.UNICODE)  # the same pieces `merge.tokens` diffs, so the two agree
GEOMETRY_TOLERANCE = 0.05   # pt, as merge's own
SCALE_TOLERANCE = 1e-3
SEVERITIES = ("loss", "undo", "report", "note")
FAIL = ("loss", "undo", "report")


def h6(text: str) -> str:
    return identity.sha1(text)[:6]


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def words(text: str | None) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "")}


def finding(kind: str, severity: str, detail: str, slide=None, element=None, object=None) -> dict:
    return {"kind": kind, "severity": severity, "slide": slide, "element": element, "object": object, "detail": detail}


# ---------------------------------------------------------------- inputs

def normalise_report(report: dict | None) -> dict:
    """The report as written by `sync.write_reports` (flattened) or as `merge.empty_report` has it."""
    r = dict(report or {})
    if isinstance(r.get("slides"), dict):
        for k, v in r["slides"].items():
            r.setdefault(f"slides_{k}", v)
    for k in ("applied", "overrides", "conflicts", "converged", "user_objects", "warnings"):
        r.setdefault(k, [])
    for k in ("created", "deleted", "moved", "kept", "user_added"):
        r.setdefault(f"slides_{k}", [])
    return r


def unit_key(el: dict) -> str:
    """The report names an element by its *unit*: the anchor of a formula picture or number ball,
    which is planned and written together with it (`merge.units`)."""
    return el.get("anchor") or el["key"]


def _at(entries: list, slide, element=None) -> list[dict]:
    keys = None if element is None else ({element} if isinstance(element, str) else set(element))
    return [e for e in entries if isinstance(e, dict) and e.get("slide") == slide
            and (keys is None or e.get("element") in keys)]


def _mentions(entries: list, word: str) -> bool:
    text = json.dumps(entries, ensure_ascii=False).lower()
    return word.lower() in text


def slide_words(slide_read: dict) -> set[str]:
    return words("\n".join(rb.get("text") or "" for rb in slide_read["objects"].values()))


def object_state(rb: dict) -> tuple:
    """What a read-back shows, for "did this object change"."""
    return (norm(rb.get("text")), tuple(round(v, 2) for v in rb.get("box", ())),
            tuple(round(v, 4) for v in rb.get("transform", ())), rb.get("text_style_hash"),
            rb.get("shape_style_hash"), (rb.get("image") or {}).get("contentHash"))


def same_box(a: dict, b: dict) -> bool:
    return not (any(abs(x - y) > GEOMETRY_TOLERANCE for x, y in zip(a.get("box", ()), b.get("box", ())))
                or any(abs(x - y) > SCALE_TOLERANCE for x, y in zip(a.get("transform", ())[:4], b.get("transform", ())[:4])))


def same_image(a: dict, b: dict) -> bool | None:
    """True/False, or None when the bytes can't be compared (Google hands out new contentUrls, so
    only a pixel signature decides; `snapshot.sign_pictures` puts those in)."""
    ia, ib = a.get("image"), b.get("image")
    if ia is None and ib is None:
        return True
    if ia is None or ib is None:
        return False
    if ia.get("signature") and ib.get("signature"):
        return snapshot.same_picture(ia, ib)
    if ia.get("contentHash") == ib.get("contentHash"):
        return True
    return None


# ---------------------------------------------------------------- slides

def slide_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    out = []
    base_by_id = {s["objectId"]: s for s in base["slides"] if s.get("objectId")}
    after_ids = {s["objectId"] for s in after["slides"]}
    for bs in before["slides"]:
        sid = bs["objectId"]
        if sid in after_ids:
            continue
        b = base_by_id.get(sid)
        if b is None:
            out.append(finding("user_slide_deleted", "loss", "a slide the person added is gone", slide=sid, object=sid))
            continue
        key = b["key"]
        if key not in rep["slides_deleted"]:
            out.append(finding("slide_deleted_unreported", "loss", f"slide {key} is gone but the report doesn't say so",
                               slide=key, object=sid))
            continue
        touched = merge.slide_touched(b, bs)
        if touched:
            out.append(finding("deleted_touched_slide", "loss", f"slide {key} was deleted although the deck had it: {touched}",
                               slide=key, object=sid))
    # the report's own claims about slides
    for key in rep["slides_deleted"]:
        sid = next((s["objectId"] for s in base["slides"] if s.get("key") == key), None)
        if sid and sid in after_ids:
            out.append(finding("reported_delete_not_done", "report", f"the report deletes slide {key} but it is still there",
                               slide=key, object=sid))
    created = len(after["slides"]) - len({s["objectId"] for s in before["slides"]} & after_ids)
    if created != len(rep["slides_created"]):
        out.append(finding("slide_count_mismatch", "report",
                           f"{created} slide(s) appeared, the report lists {len(rep['slides_created'])} created"))
    out += order_findings(base, before, after, rep)
    return out


def order_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    key_of = {s["objectId"]: s["key"] for s in base["slides"] if s.get("objectId")}
    after_ids = [s["objectId"] for s in after["slides"]]
    kept = set(after_ids)
    b = [s["objectId"] for s in before["slides"] if s["objectId"] in kept]
    a = [sid for sid in after_ids if sid in set(b)]
    if a == b:
        return []
    reported = set(rep["slides_moved"])
    if [s for s in b if key_of.get(s) not in reported] == [s for s in a if key_of.get(s) not in reported]:
        return []  # taking the reported moves out leaves the same order: nothing else moved
    moved = set(a) - set(_lcs(b, a))
    before_prev = {sid: (b[i - 1] if i else None) for i, sid in enumerate(b)}
    after_prev = {sid: (a[i - 1] if i else None) for i, sid in enumerate(a)}
    out = []
    for sid in a:
        # A slide that kept the slide it followed didn't move by itself: it went along with its
        # neighbour (the source reordering the frames around a slide the person added).
        if sid not in moved or before_prev[sid] == after_prev[sid]:
            continue
        key = key_of.get(sid)
        if key is None:
            out.append(finding("user_slide_moved", "undo", "a slide the person added changed place", slide=sid, object=sid))
        elif key not in rep["slides_moved"]:
            out.append(finding("slide_moved_unreported", "undo", f"slide {key} changed place, the report doesn't say so",
                               slide=key, object=sid))
    return out


def _lcs(a: list, b: list) -> list:
    n, m = len(a), len(b)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            table[i][j] = table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
    out, i, j = [], 0, 0
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

def user_object_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    out = []
    base_by_id = {s["objectId"]: s for s in base["slides"] if s.get("objectId")}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    for bs in before["slides"]:
        a = after_by_id.get(bs["objectId"])
        if a is None:
            continue  # the slide itself is gone: slide_findings said so
        b = base_by_id.get(bs["objectId"])
        skey = b["key"] if b else bs["objectId"]
        if b is None:
            users = [{"objectId": oid} for oid in bs["objects"]]
        else:
            users = merge.user_objects(b, bs)
        for u in users:
            oid = u["objectId"]
            rb, now = bs["objects"][oid], a["objects"].get(oid)
            if now is None:
                if rb["kind"] == "elementGroup" and len([c for c in rb.get("children", []) if c in a["objects"]]) <= 1:
                    continue  # Slides drops a group left with one child
                out.append(finding("user_object_deleted", "loss", f"{rb['kind']} the person added is gone",
                                   slide=skey, object=oid))
                continue
            if norm(rb.get("text")) != norm(now.get("text")):
                out.append(finding("user_text_changed", "loss", f"{norm(rb.get('text'))!r} became {norm(now.get('text'))!r}",
                                   slide=skey, object=oid))
            if not same_box(rb, now):
                out.append(finding("user_object_moved", "undo", f"box {rb.get('box')} became {now.get('box')}",
                                   slide=skey, object=oid))
            same = same_image(rb, now)
            if same is False:
                out.append(finding("user_image_changed", "loss", "the picture the person put there is another one now",
                                   slide=skey, object=oid))
            elif same is None:
                out.append(finding("user_image_unverified", "note", "no pixel signature: the picture bytes weren't compared",
                                   slide=skey, object=oid))
            old, new = rb.get("parent_group"), now.get("parent_group")
            if old != new and (new is not None or old in a["objects"]):
                # A group that is gone can't hold anything; that it went is judged above, where a
                # group left with one child is Slides' doing and a group with more is a loss.
                out.append(finding("user_object_regrouped", "undo",
                                   f"parent group {old} became {new}", slide=skey, object=oid))
    return out


def _base_ids(base_slide: dict) -> set[str]:
    return {o for el in base_slide["elements"] for o in el.get("objects", [])} | set(base_slide.get("groups", []))


# ---------------------------------------------------------------- the person's words

def hunks(base_text: str | None, live_text: str | None) -> list[tuple[int, int, list[str]]]:
    return merge._hunks(merge.tokens(base_text or ""), merge.tokens(live_text or ""))


def text_findings(base: dict, before: dict, after: dict, rep: dict, ours: dict | None) -> list[dict]:
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    ours_by_key = _ours_by_key(base, ours)
    for b in base["slides"]:
        sid = b.get("objectId")
        bs, a = before_by_id.get(sid), after_by_id.get(sid)
        if bs is None or a is None:
            continue
        skey = b["key"]
        before_words, after_words = slide_words(bs), slide_words(a)
        conflicts = _at(rep["conflicts"], skey)
        for el in b["elements"]:
            main = el.get("main")
            was, now = el.get("readback", {}).get(main), bs["objects"].get(main)
            if not main or was is None or now is None:
                continue
            here = words("\n".join((a["objects"].get(o) or {}).get("text") or ""
                                   for o in element_objects(skey, el["key"], el, a)))
            out += word_findings(skey, el, was, now, before_words, after_words, here, conflicts,
                                 (ours_by_key.get(skey) or {}).get(el["key"]))
        out += notes_findings(skey, b, bs, a, conflicts)
        out += background_findings(skey, b, bs, a)
    return out


def word_findings(skey, el, was, now, before_words, after_words, after_el_words, conflicts, ours_el) -> list[dict]:
    """The person's words in one converter object: gone without a trace, or overwritten silently."""
    out = []
    base_tokens = merge.tokens(was.get("text") or "")
    theirs = hunks(was.get("text"), now.get("text"))
    if not theirs:
        return out
    source = hunks(was.get("text"), _ours_text(ours_el)) if ours_el else None
    for hunk in theirs:
        typed = "".join(hunk[2])
        clash = source is not None and any(merge._clash(hunk, s) for s in source)
        for w in sorted(words(typed)):
            if w in after_words:
                continue
            if _mentions(conflicts, w):
                continue
            if clash:
                out.append(finding("overwritten_unreported", "loss",
                                   f"the source overwrote {w!r} which the person had typed into {norm(now.get('text'))[:60]!r}, "
                                   "without a conflict in the report", slide=skey, element=el["key"], object=el.get("main")))
            else:
                out.append(finding("word_lost", "loss", f"{w!r}, typed by the person, is nowhere on the slide any more",
                                   slide=skey, element=el["key"], object=el.get("main")))
        removed = words("".join(base_tokens[hunk[0]:hunk[1]]))
        for w in sorted(removed - before_words):
            # back in *this* element: the same word turning up in another element on the slide (the
            # source retitled the frame with it) is the source's word, not the person's deletion.
            if w not in after_el_words or _mentions(conflicts, w):
                continue
            if ours_el and w in words(_ours_text(ours_el)):
                continue  # the source says it again: an applied source change, not an undo
            out.append(finding("deletion_undone", "undo", f"{w!r}, which the person had deleted, is back",
                               slide=skey, element=el["key"], object=el.get("main")))
    return out


def deck_placement(base: dict) -> tuple[float, float, float] | None:
    """How a conversion's coordinates (`fingerprint.bbox`) land in the deck: `x_deck = s*x + tx`.
    Read off the base itself - every element the converter wrote stands at its own bbox - as the
    median over all of them, so the few boxes a person has since moved don't move the estimate.
    (1, 0, 0 in a real deck; the fuzz world uses a different scale on purpose, so that a confusion
    between the two coordinate systems cannot pass unnoticed.)"""
    scales, xs, ys = [], [], []
    for s in base["slides"]:
        for el in s.get("elements", []):
            bb = (el.get("fingerprint") or {}).get("bbox")
            box = ((el.get("readback") or {}).get(el.get("main")) or {}).get("box")
            if not bb or not box or bb[2] - bb[0] < 1 or bb[3] - bb[1] < 1:
                continue
            scales.append(((box[2] - box[0]) / (bb[2] - bb[0]) + (box[3] - box[1]) / (bb[3] - bb[1])) / 2)
    if len(scales) < 3:
        return None
    s = sorted(scales)[len(scales) // 2]
    for sl in base["slides"]:
        for el in sl.get("elements", []):
            bb = (el.get("fingerprint") or {}).get("bbox")
            box = ((el.get("readback") or {}).get(el.get("main")) or {}).get("box")
            if bb and box:
                xs.append(box[0] - s * bb[0])
                ys.append(box[1] - s * bb[1])
    return s, sorted(xs)[len(xs) // 2], sorted(ys)[len(ys) // 2]


def _fresh_box(place, bbox) -> list[float] | None:
    if not place or not bbox:
        return None
    s, tx, ty = place
    return [s * bbox[0] + tx, s * bbox[1] + ty, s * bbox[2] + tx, s * bbox[3] + ty]


def geometry_findings(base: dict, before: dict, after: dict, rep: dict, ours: dict | None = None) -> list[dict]:
    """A converter element the person moved or resized, back where the converter had put it. A sync
    that writes the unit re-applies the deck's transform (docs/sync.md: `overrides["geometry"]`), so
    the box afterwards is the person's or, when the source moved it too, somewhere else - never the
    base's again.

    "Somewhere else" has to be taken literally: when the source moved the element as well, the
    rewritten element lands at the conversion's new box *plus* the step the person moved it by, and
    that can fall exactly on the base's old box by coincidence. With `ours` the oracle works that
    place out (`deck_placement`) and lets the element stand there; without it, such a round reads as
    a revert."""
    out = []
    place = deck_placement(base)
    ours_by_key = _ours_by_key(base, ours)
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    for b in base["slides"]:
        bs, a = before_by_id.get(b.get("objectId")), after_by_id.get(b.get("objectId"))
        if bs is None or a is None:
            continue
        skey = b["key"]
        for el in b["elements"]:
            main = el.get("main")
            was, now = (el.get("readback") or {}).get(main), bs["objects"].get(main)
            if not main or was is None or now is None or not was.get("box") or same_box(was, now):
                continue  # the person left it where it was
            objects = [a["objects"][o] for o in element_objects(skey, el["key"], el, a)]
            if not objects or any(same_box(now, rb) for rb in objects) or not any(same_box(was, rb) for rb in objects):
                continue  # still where the person put it, or somewhere the source asked for
            keys = {el["key"], unit_key(el)}
            if _at(rep["conflicts"], skey, keys) or _at(rep["converged"], skey, keys):
                continue
            fresh = _fresh_box(place, ((ours_by_key.get(skey, {}).get(el["key"]) or {})
                                       .get("fingerprint") or {}).get("bbox"))
            if fresh is not None:
                step = [n - w for n, w in zip(now["box"], was["box"])]
                moved = [f + d for f, d in zip(fresh, step)]
                if any(all(abs(x - y) <= GEOMETRY_TOLERANCE for x, y in zip(moved, rb.get("box", ())))
                       for rb in objects):
                    continue  # the conversion's new box, moved the way the person moved it
            out.append(finding("geometry_reverted", "undo",
                               f"back at the converter's box {was.get('box')}, the person had it at {now.get('box')}",
                               slide=skey, element=el["key"], object=main))
    return out


def picture_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    """A picture the person put into a converter element (by pixel signature, never by URL: Google
    hands out new contentUrls for unchanged pictures), overwritten with the source's."""
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    for b in base["slides"]:
        bs, a = before_by_id.get(b.get("objectId")), after_by_id.get(b.get("objectId"))
        if bs is None or a is None:
            continue
        skey = b["key"]
        for el in b["elements"]:
            main = el.get("main")
            was, now = (el.get("readback") or {}).get(main), bs["objects"].get(main)
            if not main or was is None or now is None or same_image(was, now) is not False:
                continue  # the person didn't put another picture there
            objects = [a["objects"][o] for o in element_objects(skey, el["key"], el, a)]
            states = [same_image(now, rb) for rb in objects]
            if True in states or not objects:
                continue  # their picture is still there (or the element is gone: another check)
            keys = {el["key"], unit_key(el)}
            if _at(rep["conflicts"], skey, keys) or _at(rep["converged"], skey, keys):
                continue  # (an `overrides` entry says the deck's version was kept: it excuses nothing here)
            if None in states:
                out.append(finding("picture_unverified", "note", "no pixel signature: the picture bytes weren't compared",
                                   slide=skey, element=el["key"], object=main))
            else:
                out.append(finding("picture_reverted", "loss",
                                   "the picture the person put into this element is another one now, "
                                   "with nothing in the report", slide=skey, element=el["key"], object=main))
    return out


def style_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    """Styling the person applied to a converter object, silently back to the converter's."""
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    for b in base["slides"]:
        bs, a = before_by_id.get(b.get("objectId")), after_by_id.get(b.get("objectId"))
        if bs is None or a is None:
            continue
        skey = b["key"]
        for el in b["elements"]:
            main = el.get("main")
            was, now = (el.get("readback") or {}).get(main), bs["objects"].get(main)
            if not main or was is None or now is None:
                continue
            objects = [a["objects"][o] for o in element_objects(skey, el["key"], el, a)]
            if not objects:
                continue  # gone: the other checks judge that
            for field in ("text_style_hash", "shape_style_hash"):
                converter, person = was.get(field), now.get(field)
                if converter is None or person == converter or not any(rb.get(field) == converter for rb in objects):
                    continue  # the person didn't restyle it, or the converter's styling isn't back
                if any(rb.get(field) == person for rb in objects):
                    continue  # their styling is still on one of the element's objects
                keys = {el["key"], unit_key(el)}
                if _at(rep["conflicts"], skey, keys) or _at(rep["converged"], skey, keys):
                    continue
                out.append(finding("style_reverted", "undo",
                                   f"{field} is the converter's again: the person's styling is gone with nothing in the report",
                                   slide=skey, element=el["key"], object=main))
    return out


def notes_findings(skey, b, bs, a, conflicts) -> list[dict]:
    out = []
    was, now, then = b.get("notes_readback") or "", bs.get("notes") or "", a.get("notes") or ""
    for hunk in hunks(was, now):
        for w in sorted(words("".join(hunk[2])) - words(then)):
            if _mentions(conflicts, w):
                continue
            out.append(finding("notes_word_lost", "loss", f"{w!r} of the speaker notes the person wrote is gone", slide=skey))
    return out


def user_slide_findings(base: dict, before: dict, after: dict) -> list[dict]:
    """The notes and background of a slide the person added themselves: the base never saw that
    slide, so nothing the sync does to it can be called a merge - it must come through untouched."""
    theirs = {s["objectId"] for s in base["slides"] if s.get("objectId")}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    out = []
    for bs in before["slides"]:
        a = after_by_id.get(bs["objectId"])
        if bs["objectId"] in theirs or a is None:
            continue  # a converter slide, or one that vanished: the slide checks judge that
        for w in sorted(words(bs.get("notes") or "") - words(a.get("notes") or "")):
            out.append(finding("notes_word_lost", "loss", f"{w!r} of the speaker notes on a slide the person added is gone",
                               slide=bs["objectId"]))
        if not snapshot.same_background(bs.get("background"), a.get("background")):
            out.append(finding("background_lost", "loss", f"the background of a slide the person added "
                               f"({bs.get('background')}) became {a.get('background')}", slide=bs["objectId"]))
    return out


def background_findings(skey, b, bs, a) -> list[dict]:
    if b.get("background_readback") is None:
        return []
    if snapshot.same_background(b["background_readback"], bs.get("background")):
        return []  # the person didn't touch it
    if snapshot.same_background(bs.get("background"), a.get("background")):
        return []
    return [finding("background_lost", "loss", f"the background the person set ({bs.get('background')}) became "
                    f"{a.get('background')}", slide=skey)]


# ---------------------------------------------------------------- converter content

def _ours_by_key(base: dict, ours: dict | None) -> dict:
    """slide key -> {element key: ours element}."""
    if not ours:
        return {}
    out = {}
    for s in ours["slides"]:
        out[s["key"]] = {e["key"]: e for e in s["elements"]}
    return out


def _ours_text(el: dict | None) -> str:
    if not el:
        return ""
    ir = el.get("ir") or {}
    if el.get("kind") == "text" and ir.get("paragraphs"):
        return merge.predicted_text(ir)
    return identity.plain_text(ir) if ir else (el.get("fingerprint") or {}).get("text") or ""


def element_objects(skey: str, ekey: str, base_el: dict | None, slide_read: dict) -> set[str]:
    """The live objects that carry one element after a sync: its own, ones the sync made for it, or
    ones tagged with its key."""
    prefix = f"b2s_{h6(skey)}_{h6(ekey)}_"
    tag = snapshot.tag(skey, ekey)
    mine = set(base_el.get("objects", [])) if base_el else set()
    return {oid for oid, rb in slide_read["objects"].items()
            if oid in mine or oid.startswith(prefix) or rb.get("title") == tag}


def content_findings(base: dict, before: dict, after: dict, rep: dict, ours: dict | None) -> list[dict]:
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    ours_by_key = _ours_by_key(base, ours)
    for b in base["slides"]:
        sid = b.get("objectId")
        bs, a = before_by_id.get(sid), after_by_id.get(sid)
        if bs is None or a is None or b["key"] not in ours_by_key:
            continue
        skey = b["key"]
        for el in b["elements"]:
            if el["key"] not in ours_by_key[skey]:
                continue  # the source removed it: sync may delete it (word_findings guards the words)
            if not element_objects(skey, el["key"], el, bs):
                continue  # the person had already deleted it
            if element_objects(skey, el["key"], el, a):
                continue
            if _at(rep["conflicts"], skey, {el["key"], unit_key(el)}):
                continue
            out.append(finding("element_vanished", "loss",
                               f"{el['kind']}/{el.get('role')} is in the new conversion but has no object left",
                               slide=skey, element=el["key"]))
    return out


# ---------------------------------------------------------------- an honest report

def report_findings(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    base_by_key = {s["key"]: s for s in base["slides"]}
    applied_keys = {(e.get("slide"), e.get("element")) for e in rep["applied"]}
    for e in rep["applied"]:
        skey, ekey = e.get("slide"), e.get("element")
        b = base_by_key.get(skey)
        bs = before_by_id.get(b and b.get("objectId"))
        a = after_by_id.get(b and b.get("objectId"))
        if b is None or bs is None or a is None:
            continue  # a created or deleted slide: slide_findings judges those
        fields = list(e.get("fields") or [])
        el = next((x for x in b["elements"] if x["key"] == ekey), None)
        if ekey is None:
            for field in fields:
                if field == "background" and snapshot.same_background(bs.get("background"), a.get("background")):
                    out.append(finding("applied_no_change", "report", "the report applies a background that didn't change",
                                       slide=skey))
                if field == "notes" and norm(bs.get("notes")) == norm(a.get("notes")):
                    out.append(finding("applied_no_change", "report", "the report applies notes that didn't change", slide=skey))
            continue
        if fields == ["added"]:
            if not element_objects(skey, ekey, el, a):
                out.append(finding("added_element_missing", "report", "the report adds an element that isn't there",
                                   slide=skey, element=ekey))
            continue
        if fields == ["removed"]:
            if el and element_objects(skey, ekey, el, a) & set(el.get("objects", [])):
                out.append(finding("reported_remove_not_done", "report", "the report removes an element that is still there",
                                   slide=skey, element=ekey))
            continue
        if el and not _element_changed(skey, ekey, el, bs, a):
            out.append(finding("applied_no_change", "report", f"the report applies {fields} but nothing on that element changed",
                               slide=skey, element=ekey))
    for e in rep["converged"]:
        skey, ekey = e.get("slide"), e.get("element")
        b = base_by_key.get(skey)
        bs = before_by_id.get(b and b.get("objectId"))
        a = after_by_id.get(b and b.get("objectId"))
        if b is None or bs is None or a is None or ekey is None or (skey, ekey) in applied_keys:
            continue
        el = next((x for x in b["elements"] if x["key"] == ekey), None)
        if el and element_objects(skey, ekey, el, bs) and _element_changed(skey, ekey, el, bs, a):
            out.append(finding("converged_but_changed", "report",
                               f"the report calls {e.get('field')} converged (nothing written) but the element changed",
                               slide=skey, element=ekey))
    out += unreported_deletions(base, before, after, rep)
    return out


def _element_changed(skey: str, ekey: str, el: dict, bs: dict, a: dict) -> bool:
    was, now = element_objects(skey, ekey, el, bs), element_objects(skey, ekey, el, a)
    if was != now:
        return True
    return any(object_state(bs["objects"][oid]) != object_state(a["objects"][oid]) for oid in was)


def unreported_deletions(base: dict, before: dict, after: dict, rep: dict) -> list[dict]:
    """Converter objects that were alive before the sync and are gone after it, with no entry in the
    report for their element."""
    out = []
    before_by_id = {s["objectId"]: s for s in before["slides"]}
    after_by_id = {s["objectId"]: s for s in after["slides"]}
    for b in base["slides"]:
        bs, a = before_by_id.get(b.get("objectId")), after_by_id.get(b.get("objectId"))
        if bs is None or a is None:
            continue
        skey = b["key"]
        for el in b["elements"]:
            alive = [o for o in el.get("objects", []) if o in bs["objects"]]
            gone = [o for o in alive if o not in a["objects"]]
            if not gone or len(gone) < len(alive) and element_objects(skey, el["key"], el, a):
                continue
            keys = {el["key"], unit_key(el)}
            if _at(rep["applied"], skey, keys) or _at(rep["conflicts"], skey, keys):
                continue
            out.append(finding("object_deleted_unreported", "loss",
                               f"{gone} of {el['kind']}/{el.get('role')} were deleted with nothing in the report",
                               slide=skey, element=el["key"], object=gone[0]))
    return out


# ---------------------------------------------------------------- the oracle

def check(base: dict, before: dict, after: dict, report: dict, ours: dict | None = None,
          allow: list[str] = ()) -> list[dict]:
    """Findings of one sync; empty means nothing a person (or the source) put in the deck was lost.
    `allow`: finding kinds, or "<kind>/<slide>" or "<kind>/<slide>/<element>", to ignore."""
    rep = normalise_report(report)
    out = (slide_findings(base, before, after, rep) + user_object_findings(base, before, after, rep)
           + text_findings(base, before, after, rep, ours) + style_findings(base, before, after, rep)
           + picture_findings(base, before, after, rep) + geometry_findings(base, before, after, rep, ours)
           + content_findings(base, before, after, rep, ours) + user_slide_findings(base, before, after)
           + report_findings(base, before, after, rep))
    allowed = set(allow or ())
    return [f for f in out if not ({f["kind"], f"{f['kind']}/{f['slide']}", f"{f['kind']}/{f['slide']}/{f['element']}"}
                                   & allowed)]


def failures(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in FAIL]


def describe(findings: list[dict]) -> str:
    return "\n".join(f"  [{f['severity']}] {f['kind']} {f['slide']}"
                     + (f" / {f['element']}" if f.get("element") else "")
                     + (f" ({f['object']})" if f.get("object") else "") + f": {f['detail']}" for f in findings)


NAMES = {"base": ("base-before", "base"), "before": ("before",), "after": ("after",),
         "report": ("report", "sync-report"), "ours": ("ours",)}


def _load(folder: Path, name: str):
    """One snapshot, in a fuzz run folder or in a deck's `<out>/sync` (`sync-report.json`, and the
    base before the sync under `base-before.json`: `base.json` there is the one the sync wrote)."""
    for where in (folder, folder / "sync"):
        for stem in NAMES[name]:
            path = where / f"{stem}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    folder = Path(sys.argv[1])
    missing = [n for n in ("base", "before", "after") if _load(folder, n) is None]
    if missing:
        print(f"{folder}: no {', '.join(m + '.json' for m in missing)} to judge (a sync records the "
              f"read-backs only when it is run by tools/fuzz_sync.py)")
        return 2
    found = check(_load(folder, "base"), _load(folder, "before"), _load(folder, "after"),
                  _load(folder, "report"), _load(folder, "ours"))
    print(describe(found) if found else "nothing lost")
    return 1 if failures(found) else 0


if __name__ == "__main__":
    sys.exit(main())
