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

Only what THIS sync introduced is judged: a look that was already broken in `before` (the
converter's own or the person's) is not the sync's, and neither is one the new conversion draws
itself. Those come back separately, from `existing(before)`, never as failures.

How a text is laid out
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

import collections
import json
import re
import sys
from pathlib import Path

from beamer2slides import emit
from beamer2slides.devtools import loss_oracle

SEVERITIES = ("fail", "note")
FAIL = ("fail",)

INSET_X = emit.PAD_X            # box edge -> text, left and right
INSET_Y = 7.2                   # Slides' top and bottom text insets
CAP_EM = 0.72                   # ink above the baseline (Lato's capitals)
DESC_EM = 0.2                   # ink below it
NBSP = "\u00a0"
SOFT_BREAK = emit.SOFT_BREAK
UNKNOWN_EM = 0.5                # a character nobody measured
TAB_EM = 2.0

REACHED = collections.Counter()   # what check() saw and what excused it, over a whole run (`replay`)

OVERLAP_MIN = 2.0              # pt each way before two inks count as overlapping
NOTE_BELOW = 4.0                # an overlap with a picture thinner than this (pt, the lesser side) is a note
OFF_PAGE = 6.0                 # pt outside the page


def finding(kind: str, severity: str, detail: str, slide=None, element=None, object=None, **more) -> dict:
    out = {"kind": kind, "severity": severity, "slide": slide, "element": element, "object": object, "detail": detail}
    out.update(more)
    return out


# ---------------------------------------------------------------- laying a text out

def _style_name(st: dict) -> str:
    return {(False, False): "regular", (True, False): "bold", (False, True): "italic",
            (True, True): "bold_italic"}[(bool(st.get("bold")), bool(st.get("italic")))]


def advance(ch: str, st: dict, size: float) -> float:
    """Slides' advance of one character (pt) in a read-back run style at `size`."""
    family = st.get("fontFamily") or "Lato"
    if st.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT"):
        size *= emit.SCRIPT_SIZE
    if ch == "\t":
        return TAB_EM * size
    if family == emit.FONT_FOR_FAMILY["mono"]:
        return emit.ROBOTO_MONO_ADVANCE_EM * size
    table = (emit.ADVANCES.get(family) or emit.ADVANCES["Lato"])[_style_name(st)]
    if ch == NBSP:
        ch = " "
    if st.get("smallCaps") and ch.islower():
        return table.get(ch.upper(), UNKNOWN_EM) * size * emit.SMALL_CAPS_SIZE
    em = table.get(ch)
    if em is None:
        em = emit.SYMBOL_ADVANCE_EM.get(ch, UNKNOWN_EM)
    return em * size


def char_styles(rb: dict, size: "float | list[float] | None") -> list[dict]:
    """The run style of every character of the read-back's text (a newline between runs takes the
    style before it). `size`: the font size where the read-back has none (a placeholder inherits
    its layout's), one for all or one per paragraph; None when no size can be known."""
    text = rb.get("text") or ""
    out: list[dict | None] = [None] * len(text)
    for a, b, st in rb.get("run_spans") or []:
        for i in range(max(0, a), min(b, len(text))):
            out[i] = st
    last = next((s for s in out if s is not None), {})
    for i, s in enumerate(out):
        if s is None:
            out[i] = last
        else:
            last = s
    if size:
        sizes = size if isinstance(size, list) else [size]
        para, filled_ = 0, []
        for ch, s in zip(text, out):
            filled_.append(s if s.get("fontSize") else {**s, "fontSize": sizes[min(para, len(sizes) - 1)]})
            para += ch == "\n"
        out = filled_
    return out


def _para(s: dict) -> dict:
    return {"lineSpacing": (s.get("lineSpacing") or 100) / 100, "indentStart": s.get("indentStart") or 0.0,
            "indentFirstLine": s.get("indentFirstLine") or 0.0, "spaceAbove": s.get("spaceAbove") or 0.0,
            "spaceBelow": s.get("spaceBelow") or 0.0, "bullet": bool(s.get("bullet")),
            "alignment": s.get("alignment") or "START"}


def para_style(rb: dict) -> dict:
    """The paragraph style that lays the text out shortest: the read-back lists its distinct
    paragraph styles, not which paragraph has which, and an estimate that errs should err towards
    "it fits"."""
    styles = rb.get("paragraph_styles") or [{}]
    aligns = {s.get("alignment") or "START" for s in styles}
    return {"lineSpacing": min((s.get("lineSpacing") or 100) for s in styles) / 100,
            "indentStart": min((s.get("indentStart") or 0.0) for s in styles),
            "indentFirstLine": min((s.get("indentFirstLine") or 0.0) for s in styles),
            "spaceAbove": min((s.get("spaceAbove") or 0.0) for s in styles),
            "spaceBelow": min((s.get("spaceBelow") or 0.0) for s in styles),
            "bullet": any(s.get("bullet") for s in styles),
            "alignment": aligns.pop() if len(aligns) == 1 else "START"}


def para_styles(rb: dict, n: int) -> list[dict]:
    """Each of the text's `n` paragraphs' style, as far as the read-back says it: it lists the
    distinct styles in the order they first appear, so one style is everybody's, as many styles as
    paragraphs is one each, and the first paragraph always has the first. Anywhere else the
    shortest (`para_style`) stands in."""
    styles = rb.get("paragraph_styles") or [{}]
    if len(styles) == 1 or len(styles) == n:
        return [_para(styles[min(i, len(styles) - 1)]) for i in range(n)]
    least = para_style(rb)
    return [_para(styles[0])] + [least] * (n - 1)


def upright(rb: dict) -> bool:
    t = rb.get("transform") or [1, 0, 0, 1]
    return abs(t[1]) < 1e-6 and abs(t[2]) < 1e-6 and t[0] > 0 and t[3] > 0


def wrap(chars: str, styles: list[dict], width: float) -> list[tuple[int, int, float]]:
    """Greedy line breaks of one paragraph: (start, end, ink width) per line. Breaks at spaces and
    after hyphens, never at a no-break space; a soft break ends a line; a word wider than the line
    is cut where it no longer fits, as Slides does."""
    lines = []
    start, n = 0, len(chars)
    while start <= n:
        w, last_break, i = 0.0, None, start
        ink_at_break = 0.0
        ink = 0.0
        end = None
        while i < n:
            ch = chars[i]
            if ch == SOFT_BREAK:
                end, nxt = i, i + 1
                break
            adv = advance(ch, styles[i], styles[i].get("fontSize") or 0.0)
            if ch == " ":
                w += adv
                last_break, ink_at_break = i + 1, ink
                i += 1
                continue
            if w + adv > width and i > start:
                if last_break is not None and last_break > start:
                    end, nxt, ink = last_break, last_break, ink_at_break
                else:
                    end, nxt = i, i
                break
            w += adv
            ink = w
            if ch == "-":
                last_break, ink_at_break = i + 1, ink
            i += 1
        if end is None:
            lines.append((start, n, ink))
            break
        lines.append((start, end, ink))
        start = nxt
        if start >= n and (end == n or chars[end:end + 1] != SOFT_BREAK):
            break
    return lines


def layout(rb: dict, size: float | None = None) -> dict | None:
    """Where Slides sets the lines of a text box (see the module docstring): {"lines": [{"box",
    "baseline", "size", "para", "start", "end"}], "holes": [{"box", "start", "end"}], "bottom":
    ink bottom}. None for no text, a turned box, or a text whose size can't be known."""
    text = (rb.get("text") or "")
    if text.endswith("\n"):
        text = text[:-1]
    if not text.strip() or not rb.get("box") or not upright(rb):
        return None
    styles = char_styles({**rb, "text": text}, size)
    if any(not s.get("fontSize") for s in styles):
        return None
    x0, y0, x1, y1 = rb["box"]
    left = x0 + INSET_X
    right = x1 - INSET_X
    lines = []
    at = 0
    paras = text.split("\n")
    pss = para_styles(rb, len(paras))
    previous = None   # size of the last line so far
    baseline = None
    r_prev, below, bullet_prev = None, 0.0, False
    for pi, para in enumerate(paras):
        ps = pss[pi]
        r = ps["lineSpacing"]
        pst = styles[at:at + len(para)] or [styles[min(at, len(styles) - 1)]]
        indent = ps["indentStart"]
        broken = wrap(para, pst, max(1.0, right - left - indent)) if para else [(0, 0, 0.0)]
        for li, (a, b, ink) in enumerate(broken):
            sizes = [pst[k]["fontSize"] for k in range(a, b) if not para[k].isspace()] if b > a else []
            z = max(sizes) if sizes else pst[min(a, len(pst) - 1)]["fontSize"]
            if baseline is None:
                baseline = y0 + emit.BASELINE_A + emit.ASCENT_EM * z + emit.extra_above(r, z)
            elif li == 0:
                # Slides ignores the space between two bulleted items
                gap = 0.0 if ps["bullet"] and bullet_prev else ps["spaceAbove"] + below
                baseline += emit.pitch_between(previous, r_prev, z, r) + gap
            else:
                baseline += emit.line_pitch(previous, r, z)
            previous, r_prev = z, r
            if ps["alignment"] == "CENTER":
                lx = left + indent + (right - left - indent - ink) / 2
            elif ps["alignment"] == "END":
                lx = right - ink
            else:
                lx = left + indent
            bx = left + min(ps["indentFirstLine"], indent) if ps["bullet"] and li == 0 and ink > 0 else lx
            lines.append({"box": [min(bx, lx), baseline - CAP_EM * z, lx + ink, baseline + DESC_EM * z],
                          "baseline": baseline, "size": z, "para": pi, "start": at + a, "end": at + b,
                          "x": lx})
        at += len(para) + 1
        below, bullet_prev = ps["spaceBelow"], ps["bullet"]
    if not lines:
        return None
    align = (rb.get("shape_style") or {}).get("align")
    if align in ("MIDDLE", "BOTTOM"):
        top = lines[0]["baseline"] - emit.ASCENT_EM * lines[0]["size"]
        bottom = lines[-1]["baseline"] + emit.DESCENT_EM * lines[-1]["size"]
        want = (y0 + y1) / 2 - (bottom - top) / 2 if align == "MIDDLE" else y1 - emit.BASELINE_A - (bottom - top)
        shift = want - top
        for ln in lines:
            ln["baseline"] += shift
            ln["box"][1] += shift
            ln["box"][3] += shift
    holes = []
    for ln in lines:
        k = ln["start"]
        while k < ln["end"]:
            if text[k] == NBSP and styles[k].get("fontFamily") == emit.HOLE_FONT:
                j = k
                while j < ln["end"] and text[j] == NBSP and styles[j].get("fontFamily") == emit.HOLE_FONT:
                    j += 1
                hx = ln["x"] + sum(advance(text[m], styles[m], styles[m]["fontSize"]) for m in range(ln["start"], k))
                hw = sum(advance(text[m], styles[m], styles[m]["fontSize"]) for m in range(k, j))
                holes.append({"box": [hx, ln["baseline"] - CAP_EM * ln["size"], hx + hw, ln["baseline"] + DESC_EM * ln["size"]],
                              "start": k, "end": j, "line": lines.index(ln)})
                k = j
            else:
                k += 1
    return {"lines": lines, "holes": holes, "bottom": max(ln["box"][3] for ln in lines),
            "box": list(rb["box"])}


# ---------------------------------------------------------------- what is drawn where on a slide

def meet(ra: list, rb: list, tol: float = OVERLAP_MIN) -> tuple[float, int, int] | None:
    """The deepest overlap of two lists of rectangles: (its lesser side in pt, index in ra, index in
    rb), or None when no two overlap by `tol` pt each way."""
    best = None
    for i, a in enumerate(ra):
        for j, b in enumerate(rb):
            w = min(a[2], b[2]) - max(a[0], b[0])
            h = min(a[3], b[3]) - max(a[1], b[1])
            if w >= tol and h >= tol and (best is None or min(w, h) > best[0]):
                best = (min(w, h), i, j)
    return best


def _size_hint(el: dict | None, scale: float) -> list[float] | None:
    """A text's sizes where its read-back has none (a title placeholder inherits its layout's): each
    paragraph's largest run in the element's IR, at the conversion's scale."""
    paras = ((el or {}).get("ir") or {}).get("paragraphs") or []
    out = [max((r.get("size") or p.get("size") or 0.0 for r in p.get("runs") or [{}]), default=0.0) * scale
           for p in paras]
    return out if out and all(out) else None


def text_box(rb: dict) -> bool:
    st = rb.get("shape_style") or {}
    return rb.get("kind") == "shape" and (st.get("type") == "TEXT_BOX" or bool(rb.get("placeholder")))


def filled(rb: dict) -> bool:
    fill = (rb.get("shape_style") or {}).get("fill") or {}
    return rb.get("kind") == "shape" and not text_box(rb) and fill.get("color") is not None and (fill.get("alpha") or 0) > 0.5


class View:
    """One slide read-back as this oracle sees it: each object named (`el:<element key>` for the
    converter's, `obj:<objectId>` for the person's own) and each party's ink as rectangles."""

    def __init__(self, skey: str, read: dict, base_slide: dict | None, ours_slide: list | None,
                 user: set[str], scale: float, place):
        self.skey, self.read = skey, read
        els = loss_oracle._element_of(skey, base_slide, read, ours_slide)
        self.el = {oid: e for oid, e in els.items() if oid not in user}
        self.name = {oid: (f"el:{self.el[oid]['key']}" if oid in self.el else f"obj:{oid}") for oid in read["objects"]}
        self.parties: dict[str, dict] = {}
        self.panels: dict[str, list] = {}
        for oid, rb in read["objects"].items():
            if not rb.get("box") or not upright(rb):
                continue
            el = self.el.get(oid)
            if rb["kind"] == "image":
                self.parties[oid] = {"kind": "picture", "rects": [list(rb["box"])], "beyond": [False]}
            elif text_box(rb):
                lay = layout(rb, _size_hint(el, scale))
                if lay is None:
                    continue
                rects = [ln["box"] for ln in lay["lines"] if ln["box"][2] - ln["box"][0] > 0.5]
                self.parties[oid] = {"kind": "text", "rects": rects, "layout": lay,
                                     "beyond": [r[3] > rb["box"][3] + 1.0 for r in rects]}
            elif rb["kind"] == "table" and el is not None and place is not None:
                frame = (el.get("ir") or {}).get("frame") or (el.get("ir") or {}).get("bbox")
                if frame:
                    s = place[0]
                    x, y = rb["transform"][4], rb["transform"][5]
                    self.parties[oid] = {"kind": "table", "rects": [[x, y, x + s * (frame[2] - frame[0]),
                                                                     y + s * (frame[3] - frame[1])]],
                                         "beyond": [False]}
            elif filled(rb):
                self.panels[oid] = list(rb["box"])

    def of(self, name: str) -> list[str]:
        return [oid for oid, n in self.name.items() if n == name]

    def key(self, oid: str) -> str | None:
        e = self.el.get(oid)
        return e["key"] if e else None


def anchored(va: "View", a: str, b: str) -> bool:
    """`b` is a picture the converter anchors to `a`'s element (a formula over its hole, a bullet
    icon beside its line), or the other way round."""
    ea, eb = va.el.get(a), va.el.get(b)
    if not ea or not eb:
        return False
    return eb.get("anchor") == ea["key"] or ea.get("anchor") == eb["key"] or \
        (bool(ea.get("anchor")) and ea.get("anchor") == eb.get("anchor"))


def ours_inks(ours_slide: list | None, place) -> dict[str, list]:
    """Element key -> the ink the new conversion draws for it (PDF lines and pictures, in deck pt)."""
    if not ours_slide or place is None:
        return {}
    s, tx, ty = place
    out = {}
    for el in ours_slide:
        ir = el.get("ir") or {}
        rects = []
        if el.get("kind") == "text" and ir.get("paragraphs"):
            for p in ir["paragraphs"]:
                for ln in p.get("lines") or []:
                    rects.append([ln["x0"], ln["baseline"] - CAP_EM * p["size"], ln["x1"], ln["baseline"] + DESC_EM * p["size"]])
                if p.get("bullet") and p["bullet"].get("bbox"):
                    rects.append(list(p["bullet"]["bbox"]))
        else:
            bb = ir.get("frame") or ir.get("bbox") or (el.get("fingerprint") or {}).get("bbox")
            if bb:
                rects.append(list(bb))
        out[el["key"]] = [[s * r[0] + tx, s * r[1] + ty, s * r[2] + tx, s * r[3] + ty] for r in rects]
    return out


# ---------------------------------------------------------------- one slide, before and after

class Pair:
    """A slide as the sync found it and as it left it."""

    def __init__(self, skey: str, base_slide: dict | None, before: dict | None, after: dict,
                 ours_slide: list | None, have_ours: bool, unsure: bool, scale: float, place, page):
        from beamer2slides import merge
        self.skey, self.base_slide, self.page = skey, base_slide, page
        if before is None:
            user = set()
        elif base_slide is None:
            user = set(before["objects"])       # a slide the person added: all theirs
        else:
            user = {u["objectId"] for u in merge.user_objects(base_slide, before)}
        self.va = View(skey, after, base_slide, ours_slide, user, scale, place)
        self.vb = View(skey, before, base_slide, None, user, scale, place) if before is not None else None
        self.inks = ours_inks(ours_slide, place)
        self.have_ours, self.unsure = have_ours and ours_slide is not None, unsure
        self.reached = collections.Counter()

    def before_parties(self, name: str) -> list[dict] | None:
        """The parties carrying `name` before the sync; None when nothing did (a new element)."""
        if self.vb is None:
            return None
        oids = self.vb.of(name)
        if not oids:
            return None
        return [self.vb.parties[o] for o in oids if o in self.vb.parties]

    def ours_rects(self, oid: str) -> list | None:
        k = self.va.key(oid)
        return self.inks.get(k) if k else None

    def history(self, oid: str) -> dict:
        """What happened to one object in this sync, for the finding's reader (and the fuzzer)."""
        name = self.va.name[oid]
        rb = self.va.read["objects"][oid]
        olds = [self.vb.read["objects"][o] for o in self.vb.of(name)] if self.vb else []
        if not olds:
            return {"name": name, "was": "new"}
        old = olds[0]
        how = []
        if not loss_oracle.same_box(old, rb):
            how.append("moved")
        if loss_oracle.norm(old.get("text")) != loss_oracle.norm(rb.get("text")):
            how.append("text")
        if oid not in self.vb.read["objects"]:
            how.append("recreated")
        base_rb = None
        if self.base_slide is not None and name.startswith("el:"):
            el = next((e for e in self.base_slide["elements"] if e["key"] == name[3:]), None)
            base_rb = el and (el.get("readback") or {}).get(el.get("main"))
        if base_rb is not None and old.get("box") and not loss_oracle.same_box(base_rb, old):
            how.append("person_moved")
        if base_rb is not None and loss_oracle.norm(base_rb.get("text")) != loss_oracle.norm(old.get("text")):
            how.append("person_text")
        return {"name": name, "was": "person" if name.startswith("obj:") else "converter", "how": how}

    def severity(self, depth: float, needs_ours: bool, least: float = None) -> tuple[str, str]:
        if self.unsure:
            return "note", " - on a slide the report says may be the wrong one"
        if needs_ours and not self.have_ours:
            return "note", " - no new conversion to ask whether it draws it so"
        if depth < (NOTE_BELOW if least is None else least):
            return "note", f" - by {depth:.1f} pt only"
        return "fail", ""


def _text(rb: dict) -> str:
    return loss_oracle.norm(rb.get("text"))[:50]


def overlap_findings(sp: Pair) -> list[dict]:
    """Two inks that meet after the sync, not before it, and not in the new conversion."""
    out, va, done = [], sp.va, set()
    for a, pa in va.parties.items():
        if pa["kind"] != "text":
            continue
        for b, pb in va.parties.items():
            if b == a or (b, a) in done:
                continue
            done.add((a, b))
            na, nb = va.name[a], va.name[b]
            if na == nb or anchored(va, a, b):
                continue
            m = meet(pa["rects"], pb["rects"])
            if not m:
                continue
            depth, i, j = m
            sp.reached["overlap"] += 1
            olds_a, olds_b = sp.before_parties(na), sp.before_parties(nb)
            if olds_a is not None and olds_b is not None:
                if not olds_a or not olds_b:
                    sp.reached["overlap: not laid out before"] += 1
                    continue  # there before, but not laid out then: nothing to compare with
                if any(meet(x["rects"], y["rects"], 0.5) for x in olds_a for y in olds_b):
                    # they met before the sync already. Deliberately also when they now meet
                    # deeper: in the archive that is a person's copy laid over the original and the
                    # original gaining a line - broken before, by the person, not by the sync.
                    sp.reached["overlap: met before"] += 1
                    continue
            ra, rb_ = sp.ours_rects(a), sp.ours_rects(b)
            if ra and rb_ and meet(ra, rb_, 0.5):
                sp.reached["overlap: the conversion draws it"] += 1
                continue  # the new conversion draws them so
            sp.reached["overlap: judged"] += 1
            new = olds_a is None or olds_b is None
            beyond = pa["beyond"][i] or (pb["kind"] == "text" and pb["beyond"][j])
            # two texts' ink bands are glyphs (a picture's box has margins): 2 pt of them is touching
            sev, why = sp.severity(depth, needs_ours=new, least=OVERLAP_MIN if pb["kind"] == "text" else None)
            kind = "text_overflow" if beyond else "text_overlap"
            ra_, rb2 = va.read["objects"][a], va.read["objects"][b]
            detail = (f"{_text(ra_)!r} and {pb['kind']} {(_text(rb2) or b)!r} overlap by {depth:.1f} pt"
                      + (" - the text runs out of its box onto it" if beyond else "") + why)
            out.append(finding(kind, sev, detail, slide=sp.skey, element=va.key(a), object=a,
                               other=b, other_element=va.key(b), depth=round(depth, 1),
                               history=[sp.history(a), sp.history(b)]))
    return out


PANEL_SLACK = 3.0   # pt: a text box counts as sitting on a panel when it is inside it by this much


def panel_findings(sp: Pair) -> list[dict]:
    """A text that runs out of the bottom of the panel it sits on (a block's body out of its block)."""
    out, va = [], sp.va

    def spill(view, text_oid, panel_oid) -> float:
        return max(r[3] for r in view.parties[text_oid]["rects"]) - view.panels[panel_oid][3]

    for t, pt in va.parties.items():
        if pt["kind"] != "text" or not pt["rects"]:
            continue
        box = va.read["objects"][t]["box"]
        left, right = min(r[0] for r in pt["rects"]), max(r[2] for r in pt["rects"])
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
            if sp.vb is not None and sp.vb.of(nt) and sp.vb.of(np_):
                was = max((spill(sp.vb, x, y) for x in sp.vb.of(nt) for y in sp.vb.of(np_)
                           if x in sp.vb.parties and y in sp.vb.panels), default=None)
                if was is not None and was > 0.5:
                    sp.reached["panel: out before"] += 1
                    continue  # it ran out of that panel before the sync already
            rt, rp = sp.ours_rects(t), sp.ours_rects(p)
            if rt and rp and max(r[3] for r in rt) > max(r[3] for r in rp) + 0.5:
                sp.reached["panel: the conversion draws it"] += 1
                continue  # the new conversion draws it past the panel
            sp.reached["panel: judged"] += 1
            new = not (sp.vb is not None and sp.vb.of(nt) and sp.vb.of(np_))
            sev, why = sp.severity(by, needs_ours=new)
            out.append(finding("text_overflow", sev, f"{_text(va.read['objects'][t])!r} runs {by:.1f} pt out of the "
                               f"bottom of the panel it sits on" + why, slide=sp.skey, element=va.key(t), object=t,
                               other=p, other_element=va.key(p), depth=round(by, 1),
                               history=[sp.history(t), sp.history(p)]))
    return out


def off_page_findings(sp: Pair) -> list[dict]:
    out, va = [], sp.va
    w, h = sp.page

    def outside(rects) -> float:
        return max(max(-r[0], r[2] - w, -r[1], r[3] - h) for r in rects) if rects else 0.0

    for oid, p in va.parties.items():
        by = outside(p["rects"])
        if by <= OFF_PAGE:
            continue
        sp.reached["off_page"] += 1
        olds = sp.before_parties(va.name[oid])
        if olds and any(outside(x["rects"]) > OFF_PAGE / 2 for x in olds):
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
        sev, why = sp.severity(by, needs_ours=olds is None)
        out.append(finding("off_page", sev, f"{p['kind']} {(_text(va.read['objects'][oid]) or oid)!r} reaches "
                           f"{by:.1f} pt past the page edge" + why, slide=sp.skey, element=va.key(oid), object=oid,
                           depth=round(by, 1), history=[sp.history(oid)]))
    return out


STRANDED_X = 8.0      # pt: a formula picture this far beside its hole is not over it
STRANDED_LINES = 0.6  # of a line: this far above or below


def hole_offsets(view: View, text_oid: str) -> dict[str, tuple[float, float, float]]:
    """Picture object -> (dx, dy, line size) from the hole the layout pairs it with, for the
    formula pictures anchored to that text (the cheapest assignment of pictures to holes)."""
    import itertools
    p = view.parties.get(text_oid)
    key = view.key(text_oid)
    if not p or not key or p["kind"] != "text":
        return {}
    holes = p["layout"]["holes"]
    pics = [o for o, q in view.parties.items() if q["kind"] == "picture" and view.el.get(o)
            and view.el[o].get("anchor") == key and view.el[o].get("role") == "math"]
    if not pics or not holes:
        return {}

    def off(o, hole):
        b, hb = view.read["objects"][o]["box"], hole["box"]
        return ((b[0] + b[2]) / 2 - (hb[0] + hb[2]) / 2, (b[1] + b[3]) / 2 - (hb[1] + hb[3]) / 2,
                p["layout"]["lines"][hole["line"]]["size"])

    def cost(o, hole):
        dx, dy, z = off(o, hole)
        return abs(dx) + abs(dy)
    if len(pics) <= 6 and len(holes) <= 8:
        best = None
        for perm in itertools.permutations(range(len(holes)), min(len(pics), len(holes))):
            c = sum(cost(o, holes[k]) for o, k in zip(pics, perm))
            if best is None or c < best[0]:
                best = (c, perm)
        return {o: off(o, holes[k]) for o, k in zip(pics, best[1])}
    return {o: off(o, min(holes, key=lambda hh: cost(o, hh))) for o in pics}


def stranded(dx: float, dy: float, z: float) -> bool:
    return abs(dx) > STRANDED_X or abs(dy) > STRANDED_LINES * emit.LINE_EM * z


def stranded_findings(sp: Pair) -> list[dict]:
    """A formula picture that is not over its hole after the sync. On what the converter placed the
    layout finds the hole within 2 pt of its picture (72 archived placements), so the absolute
    measure stands. Judged when it was over its hole before the sync, or when this sync wrote the
    picture (created, recreated or moved it): a sync that puts a picture down owes it its hole,
    even one the person's words had already pushed away (`history` says which) - unless the
    person had put the picture where it stands themselves (moved or resized off the base)."""
    out, va = [], sp.va
    for t in va.parties:
        offs = hole_offsets(va, t)
        if not offs:
            continue
        olds = {}
        if sp.vb is not None:
            for x in sp.vb.of(va.name[t]):
                olds.update({sp.vb.name[o]: v for o, v in hole_offsets(sp.vb, x).items()})
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
            if was is None and not wrote and sp.vb is not None and sp.vb.of(va.name[pic]):
                sp.reached["stranded: no hole before"] += 1
                continue  # the picture was there, but no hole in that text to measure it by
            sp.reached["stranded: judged"] += 1
            sev, why = sp.severity(max(abs(dx), abs(dy)), needs_ours=was is None)
            if was is not None and stranded(*was):
                why = f" - it was {was[0]:+.1f} / {was[1]:+.1f} pt off before, and the sync rewrote it there" + why
            out.append(finding("stranded_picture", sev,
                               f"the formula picture is {dx:+.1f} pt across and {dy:+.1f} pt down from its hole in "
                               f"{_text(va.read['objects'][t])!r}" + why, slide=sp.skey, element=va.key(pic),
                               object=pic, other=t, other_element=va.key(t), depth=round(max(abs(dx), abs(dy)), 1),
                               history=[h, sp.history(t)]))
    return out


# ---------------------------------------------------------------- the oracle

def _slide_key(slide: dict, base_by_id: dict, ours_keys: list[str]) -> str:
    """The base's key of a slide, or - for one this sync created - the new conversion's, read off
    the ids sync gives what it creates (`b2s_<h6 slide>_...`)."""
    b = base_by_id.get(slide["objectId"])
    if b is not None:
        return b["key"]
    for k in ours_keys:
        prefix = f"b2s_{loss_oracle.h6(k)}_"
        if any(o.startswith(prefix) for o in slide["objects"]):
            return k
    return slide["objectId"]


def pairs(base: dict, before: dict | None, after: dict, report: dict | None, ours: dict | None):
    """One `Pair` per slide of `after` (before=None: every slide judged on its own)."""
    unsure = loss_oracle.uncertain_slides(base, ours)
    # A converted deck is the PDF at `scale` from the page's corner (emit); a base without one
    # (the offline fuzz world's) is read off its own boxes.
    place = (base["scale"], 0.0, 0.0) if base.get("scale") else loss_oracle.deck_placement(base)
    scale = base.get("scale") or (place[0] if place else 1.0)
    page = after.get("page_size") or base.get("deck_page_size") or [720.0, 405.0]
    base_by_id = {s["objectId"]: s for s in base["slides"] if s.get("objectId")}
    before_by_id = {s["objectId"]: s for s in (before or {}).get("slides", [])}
    ours_by_key = {s["key"]: s["elements"] for s in (ours or {}).get("slides", [])}
    for a in after["slides"]:
        skey = _slide_key(a, base_by_id, list(ours_by_key))
        yield Pair(skey, base_by_id.get(a["objectId"]), before_by_id.get(a["objectId"]) if before else None, a,
                   ours_by_key.get(skey), ours is not None, skey in unsure, scale, place, page)


def check(base: dict, before: dict, after: dict, report: dict | None = None, ours: dict | None = None,
          allow: list[str] = ()) -> list[dict]:
    """Findings of one sync: what looks broken after it that did not before it and that the new
    conversion does not draw. `allow`: kinds, "<kind>/<slide>" or "<kind>/<slide>/<element>" to ignore."""
    out = []
    for sp in pairs(base, before, after, report, ours):
        if sp.vb is None and sp.skey not in {s["key"] for s in (ours or {}).get("slides", [])}:
            continue  # a slide nobody can say anything about (the person's, created meanwhile?)
        out += overlap_findings(sp) + panel_findings(sp) + off_page_findings(sp) + stranded_findings(sp)
        REACHED.update(sp.reached)
    allowed = set(allow or ())
    return [f for f in out if not ({f["kind"], f"{f['kind']}/{f['slide']}", f"{f['kind']}/{f['slide']}/{f['element']}"}
                                   & allowed)]


def existing(base: dict, before: dict) -> list[dict]:
    """What already looks broken in `before`: the same kinds, every one a note, with `by` saying
    whose it is - "converter" when every object involved still is as the base wrote it, "person"
    otherwise. Nothing here is the sync's doing."""
    out = []
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
    for el in sp.base_slide["elements"]:
        rb = (el.get("readback") or {}).get(oid)
        if rb is not None:
            now = sp.va.read["objects"].get(oid)
            return now is not None and loss_oracle.same_box(rb, now) and \
                loss_oracle.norm(rb.get("text")) == loss_oracle.norm(now.get("text")) and \
                rb.get("text_style_hash") == now.get("text_style_hash")
    return False


def failures(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in FAIL]


def describe(findings: list[dict]) -> str:
    return "\n".join(f"  [{f['severity']}] {f['kind']} {f['slide']}"
                     + (f" / {f['element']}" if f.get("element") else "")
                     + (f" ({f['object']})" if f.get("object") else "") + f": {f['detail']}" for f in findings)


# ---------------------------------------------------------------- replaying an archive of fuzz steps

def ours_from_folder(folder: Path, base: dict) -> dict | None:
    """`sync.build_ours` from the `deck.json` a live fuzz step left in `<step>/ours`, without the
    PDF: the classify the sync used, keyed against this base the way build_ours keys it (the
    extract, classify and render halves are what the folder already holds)."""
    from beamer2slides import identity, snapshot
    from beamer2slides.emit import SLIDE_W, DeckPlan, merge_blocks
    from beamer2slides.sync import mark_emitted
    path = folder / "deck.json"
    if not path.exists():
        return None
    deck = json.loads(path.read_text(encoding="utf-8"))
    plan = DeckPlan({**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]}, SLIDE_W)
    deck = plan.deck
    infos = [identity.slide_info(s) for s in deck["slides"]]
    base_infos = [{"label": b.get("label"), "title": b.get("title") or "", "text": b.get("text") or "",
                   "page": b["page"], "removed": b.get("removed")} for b in base["slides"]]
    moves = identity.label_moves(base_infos, infos)
    for m in moves:
        m["slide"] = base["slides"][m["base"]]["key"]
        m["frame_is"] = base["slides"][m["frame_is"]]["key"] if m["frame_is"] is not None else None
    weak: dict[int, str] = {}
    keys, pairs_ = identity.inherit_slide_keys(base_infos, [b["key"] for b in base["slides"]], infos, moves, weak)
    ekeys, fps = [], []
    for j, slide in enumerate(deck["slides"]):
        matched = [{"key": e["key"], "kind": e["kind"], "role": e.get("role"), "fingerprint": e["fingerprint"]}
                   for e in base["slides"][pairs_[j]]["elements"]] if j in pairs_ else None
        k, f = identity.slide_element_keys(slide["elements"], folder, matched)
        ekeys.append(k)
        fps.append(f)
    entries = snapshot.slide_entries(deck, folder, keys, ekeys, fps)
    mark_emitted(base, entries, deck, pairs_, plan.scale, plan.fonts)
    return {"slides": entries, "pairs": pairs_, "label_moves": moves, "weak_pairs": weak}


def steps_under(paths: list[Path]) -> list[Path]:
    """Every recorded step folder (base, before and after read-backs) at or under these paths."""
    out = []
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


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def edit_summary(spec: dict) -> str:
    """One deck edit as `kind` plus what it aimed at, short."""
    args = spec.get("args") or {}
    target = args.get("target") or {}
    what = ",".join(f"{k}={str(v)[:24]}" for k, v in target.items()) if isinstance(target, dict) else str(target)[:24]
    return spec.get("edit", "?") + (f"[{what}]" if what else "")


def replay_step(step: Path, want_ours: bool = True) -> dict:
    """The oracle over one recorded step: {"round", "step", "variant", "edits", "findings", "existing"}."""
    base, before, after = _load(step / "base.json"), _load(step / "before.json"), _load(step / "after.json")
    report = _load(step / "report.json")
    rnd = _load(step.parent / "round.json") or {}
    n = _num(step.name)
    rec = next((s for s in rnd.get("steps", []) if s.get("step") == n), {})
    edits = _load(step / "edits.json") or rec.get("edits") or []
    ours = None
    if want_ours:
        try:
            ours = ours_from_folder(step / "ours", base)
        except ImportError:
            raise  # (our own code moved: never a property of the archived step)
        except Exception as e:  # noqa: BLE001 (an archived conversion today's code cannot key)
            print(f"{step}: no ours ({type(e).__name__}: {e})", file=sys.stderr)
    return {"round": step.parent.name, "archive": step.parent.parent.name, "step": n, "folder": str(step),
            "variant": rec.get("variant"), "edits": [edit_summary(e) for e in edits],
            "edit_kinds": sorted({e.get("edit", "?") for e in edits}), "ours": ours is not None,
            "findings": check(base, before, after, report, ours), "existing": existing(base, before)}


def correlate(results: list[dict], level: str = "fail") -> dict:
    """Which deck edit kinds and source variants precede a finding: per edit kind and per variant,
    how many steps had it and how many of those had a finding of `level` (and of each kind)."""
    def table(of):
        out: dict[str, dict] = {}
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
        return dict(sorted(out.items(), key=lambda kv: (-(kv[1]["lift"] or 0), -kv[1]["steps"])))
    return {"edit_kinds": table(lambda r: r["edit_kinds"]), "variants": table(lambda r: [r["variant"] or "?"])}


def counts(results: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for r in results:
        for f in r["findings"]:
            row = out.setdefault(f["kind"], {"fail": 0, "note": 0, "existing": 0})
            row[f["severity"]] += 1
        for f in r["existing"]:
            out.setdefault(f["kind"], {"fail": 0, "note": 0, "existing": 0})["existing"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="layout_oracle", description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help="archive roots, round folders or step folders")
    ap.add_argument("--json", action="store_true", help="one machine-readable document on stdout")
    ap.add_argument("--existing", action="store_true", help="also list what already looked broken before each sync")
    ap.add_argument("--notes", action="store_true", help="also list note-level findings")
    ap.add_argument("--no-ours", action="store_true", help="don't rebuild the new conversion from <step>/ours")
    ap.add_argument("--out", type=Path, help="also write the --json document to this file")
    args = ap.parse_args(argv)
    results = [replay_step(s, not args.no_ours) for s in steps_under(args.paths)]
    summary = {"steps": len(results), "with_ours": sum(r["ours"] for r in results), "counts": counts(results),
               "reached": dict(sorted(REACHED.items())), "correlation": correlate(results)}
    if args.json or args.out:
        doc = json.dumps({"summary": summary, "steps": [{k: v for k, v in r.items() if args.existing or k != "existing"}
                                                         for r in results]}, indent=1, ensure_ascii=False)
        if args.out:
            args.out.write_text(doc, encoding="utf-8")
        if args.json:
            print(doc)
            return 1 if any(failures(r["findings"]) for r in results) else 0
    print(f"{summary['steps']} steps ({summary['with_ours']} with the new conversion)")
    print(f"{'kind':<18}{'fail':>6}{'note':>6}{'existing':>10}")
    for kind, row in sorted(summary["counts"].items()):
        print(f"{kind:<18}{row['fail']:>6}{row['note']:>6}{row['existing']:>10}")
    print("reached: " + ", ".join(f"{k} {v}" for k, v in summary["reached"].items()))
    for r in results:
        shown = [f for f in r["findings"] if args.notes or f["severity"] == "fail"]
        shown += [f for f in r["existing"]] if args.existing else []
        if not shown:
            continue
        print(f"\n{r['archive']}/{r['round']}/step{r['step']}  variant={r['variant']}  edits: {', '.join(r['edits'])}")
        for f in shown:
            tag = f" [existing, {f['by']}]" if f.get("existing") else ""
            print(describe([f]) + tag)
            for h in f.get("history") or []:
                print(f"      {h['name']}: {h['was']} {' '.join(h.get('how') or [])}")
    for what, table in summary["correlation"].items():
        print(f"\nfail findings by {what} (steps, with a finding, lift):")
        for k, row in table.items():
            print(f"  {k:<28}{row['steps']:>5}{row['with_finding']:>5}  {row['lift']}  {row['kinds']}")
    return 1 if any(failures(r["findings"]) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
