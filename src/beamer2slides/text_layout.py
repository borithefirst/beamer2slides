"""Where Slides sets the lines of a text box, from its read-back alone.

A read-back (`snapshot.readback`) says what a text box holds and how big it is, not where Slides
puts the lines, and Slides boxes do not autofit (none survives the .pptx import). So the lines are
laid out here the way emit predicts them: the calibrated Slides advances (`emit.ADVANCES`, per
family and style; Roboto Mono's 0.6 em), greedy wrapping at spaces inside the box less its insets,
lines `emit.LINE_EM` x lineSpacing apart, the first baseline `emit.BASELINE_A + ASCENT_EM` under
the box top, paragraphs spaced with `emit.pitch_between` plus their spaceAbove/spaceBelow (none
between two bulleted items).

The read-back lists the distinct paragraph styles in the order they first appear, not which
paragraph has which: one style is everybody's, as many as paragraphs is one each, and otherwise the
one that lays the text out *shortest* stands in, so a miss errs towards "fits". On 72 unedited
converter boxes of the fuzz archive this predicts every one of 117 paragraphs' PDF line count, and
finds each converter-placed formula picture within 2 pt of its hole (docs/project-notes.md
"Layout oracle").

Two readers: `devtools/layout_oracle.py` (does a synced slide look broken?) and `sync` (a unit
recreated for the source's words and then given the person's: where its formula holes went, and
how tall the box and the block panel under it now have to be - `Sync.refit_requests`).
"""

from . import bidi, emit

INSET_X = emit.PAD_X            # box edge -> text, left and right
INSET_Y = 7.2                   # Slides' top and bottom text insets
CAP_EM = 0.72                   # ink above the baseline (Lato's capitals)
DESC_EM = 0.2                   # ink below it
NBSP = " "
SOFT_BREAK = emit.SOFT_BREAK
UNKNOWN_EM = 0.5                # a character nobody measured
TAB_EM = 2.0
BOX_ROOM = 4.0                  # pt emit leaves under the last line's descent (emit.text_box_requests)


def _style_name(st: dict) -> str:
    # (Slides draws a weight of 700 or more with the family's bold face, whatever `bold` says:
    # emit.OPTICAL_WEIGHT's footlines)
    bold = bool(st.get("bold")) or (st.get("weight") or 0) >= 700
    return {(False, False): "regular", (True, False): "bold", (False, True): "italic",
            (True, True): "bold_italic"}[(bold, bool(st.get("italic")))]


def advance(ch: str, st: dict, size: float) -> float:
    """Slides' advance of one character (pt) in a read-back run style at `size`."""
    family = st.get("fontFamily") or "Lato"
    if st.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT"):
        size *= emit.SCRIPT_SIZE
    if ch == "\t":
        return TAB_EM * size
    if ch in bidi.MARKS:
        return 0.0  # (an LRM or RLM draws nothing)
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
            "indentFirstLine": s.get("indentFirstLine") or 0.0, "indentEnd": s.get("indentEnd") or 0.0,
            "spaceAbove": s.get("spaceAbove") or 0.0,
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
            "indentEnd": min((s.get("indentEnd") or 0.0) for s in styles),
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
    "baseline", "size", "para", "start", "end", "x", "spacing"}], "holes": [{"box", "start", "end",
    "line"}], "bottom": ink bottom, "box", "text", "styles"}. None for no text, a turned box, or a
    text whose size can't be known."""
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
        # a paragraph keeps its indentEnd free before the box's edge (emit.paragraph_ends)
        end = right - ps["indentEnd"]
        broken = wrap(para, pst, max(1.0, end - left - indent)) if para else [(0, 0, 0.0)]
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
                lx = left + indent + (end - left - indent - ink) / 2
            elif ps["alignment"] == "END":
                lx = end - ink
            else:
                lx = left + indent
            bx = left + min(ps["indentFirstLine"], indent) if ps["bullet"] and li == 0 and ink > 0 else lx
            lines.append({"box": [min(bx, lx), baseline - CAP_EM * z, lx + ink, baseline + DESC_EM * z],
                          "baseline": baseline, "size": z, "para": pi, "start": at + a, "end": at + b,
                          "x": lx, "spacing": r})
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
            "box": list(rb["box"]), "text": text, "styles": styles}


def span_box(lay: dict, a: int, b: int) -> dict | None:
    """Where characters [a, b) of a laid-out text stand, when they are on one line: {"box" (their
    ink band), "line" (index), "baseline"}. None when they are not all on one line."""
    text, styles = lay["text"], lay["styles"]
    for i, ln in enumerate(lay["lines"]):
        if ln["start"] <= a < max(ln["end"], ln["start"] + 1) and b <= ln["end"]:
            x = ln["x"] + sum(advance(text[m], styles[m], styles[m]["fontSize"]) for m in range(ln["start"], a))
            w = sum(advance(text[m], styles[m], styles[m]["fontSize"]) for m in range(a, b))
            return {"box": [x, ln["baseline"] - CAP_EM * ln["size"], x + w, ln["baseline"] + DESC_EM * ln["size"]],
                    "line": i, "baseline": ln["baseline"]}
    return None


def needed_bottom(lay: dict) -> float:
    """Where a box has to end to hold its last line the way emit sizes a box it creates: that
    line's descent, the room its line spacing adds below it, and 4 pt (`emit.text_box_requests`)."""
    last = lay["lines"][-1]
    z, r = last["size"], last["spacing"]
    return last["baseline"] + emit.DESCENT_EM * z + emit.extra_below(r, z) + BOX_ROOM


def text_box(rb: dict) -> bool:
    st = rb.get("shape_style") or {}
    return rb.get("kind") == "shape" and (st.get("type") == "TEXT_BOX" or bool(rb.get("placeholder")))


def ink(rb: dict) -> list[list[float]] | None:
    """Where an object puts ink, slide pt: a text box's laid-out lines, a picture's box; None for
    anything else or what the model cannot lay out."""
    if not rb.get("box") or not upright(rb):
        return None
    if rb.get("kind") == "image":
        return [list(rb["box"])]
    if text_box(rb):
        lay = layout(rb)
        return None if lay is None else [ln["box"] for ln in lay["lines"] if ln["box"][2] - ln["box"][0] > 0.5]
    return None


def meet(ra: list, rb: list, tol: float) -> tuple[float, int, int] | None:
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


OVERRUN_MIN = 2.0   # pt each way before the source's ink counts as over a person's object


def overruns(before: dict, after: dict, users: set[str], skip: set[str] = frozenset()) -> list[dict]:
    """The person's own objects (`users`, untouched by the sync) that the source's text or pictures
    now run over, where they did not before: {"object", "other", "depth"}, the deepest per object.
    One side is always text (a picture on a picture is a collage, not an accident). `skip`: objects
    about to be deleted. Each other object is judged against its own meet before (a recreated one
    found by its b2s title): a note already over the frame counter still counts the paragraph that
    now reaches it (live fuzz r7411)."""
    out = []
    inks_a = {o: ink(rb) for o, rb in after["objects"].items() if o not in skip}
    inks_b = {o: ink(rb) for o, rb in before["objects"].items()}
    by_title = {rb["title"]: o for o, rb in before["objects"].items() if (rb.get("title") or "").startswith("b2s:")}
    for u in sorted(users):
        ua, ub = after["objects"].get(u), before["objects"].get(u)
        if ua is None or ub is None or not inks_a.get(u) or ua.get("box") != ub.get("box"):
            continue
        picture = ua.get("kind") == "image"
        best = None
        for o, r in inks_a.items():
            if o == u or o in users or not r or (picture and after["objects"][o].get("kind") == "image"):
                continue
            now = meet(inks_a[u], r, OVERRUN_MIN)
            if now is None:
                continue
            old = o if o in before["objects"] else by_title.get(after["objects"][o].get("title"))
            was = meet(inks_b[u], inks_b[old], OVERRUN_MIN) if old and inks_b.get(u) and inks_b.get(old) else None
            if (was is None or now[0] > was[0] + OVERRUN_MIN) and (best is None or now[0] > best[0]):
                best = (now[0], o)
        if best:
            out.append({"object": u, "other": best[1], "depth": round(best[0], 1)})
    return out


def filled(rb: dict) -> bool:
    """A shape that hides what is under it (a block's panel): an opaque fill, and not a text box."""
    fill = (rb.get("shape_style") or {}).get("fill") or {}
    return rb.get("kind") == "shape" and not text_box(rb) and fill.get("color") is not None and (fill.get("alpha") or 0) > 0.5
