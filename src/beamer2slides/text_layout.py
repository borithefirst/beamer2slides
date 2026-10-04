"""Where Slides sets the lines of a text box, from its read-back alone.

A read-back (`snapshot.readback`) says what a text box holds and how big it is, not where Slides
puts the lines, and Slides boxes do not autofit (none survives the .pptx import). So the lines are
laid out here the way emit predicts them: the calibrated Slides advances (`emit.ADVANCES`, per
family and style; Roboto Mono's 0.6 em), greedy wrapping at spaces inside the box less its insets,
lines `emit.LINE_EM` x lineSpacing apart, the first baseline `emit.BASELINE_A + ASCENT_EM` under
the box top, paragraphs spaced with `emit.pitch_between` plus their spaceAbove/spaceBelow (between
two bulleted items only the lower one's spaceAbove, which emit writes with `emit_text.LIST_SPACING`).

The read-back lists the distinct paragraph styles in the order they first appear, not which
paragraph has which: one style is everybody's, as many as paragraphs is one each, and otherwise the
one that lays the text out *shortest* stands in, so a miss errs towards "fits". On 72 unedited
converter boxes of the fuzz archive this predicts every one of 117 paragraphs' PDF line count, and
finds each converter-placed formula picture within 2 pt of its hole (docs/project-notes.md
"Layout oracle").

The read-back comes in as JSON (a `snapshot` dict, an archived one, a test's), read through the
narrowings below; what comes out is records: `Layout` of `Line`s and `Hole`s, `SpanBox`, `Overrun`.

Two readers: `devtools/layout_oracle.py` (does a synced slide look broken?) and `sync` (a unit
recreated for the source's words and then given the person's: where its formula holes went, and
how tall the box and the block panel under it now have to be - `Sync.refit_requests`).
"""

import dataclasses
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass

from . import bidi, emit
from .emit_widths import HOLE_BREAKS, WORD_JOINER, breaks_before
from .fonts import face_advance
from .json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object

INSET_X = emit.PAD_X            # box edge -> text, left and right
INSET_Y = 7.2                   # Slides' top and bottom text insets
CAP_EM = 0.72                   # ink above the baseline (Lato's capitals)
DESC_EM = 0.2                   # ink below it
NBSP = "\xa0"
THICK_SPACE = "\u2008"          # a relation's thick space, set as a no-break space (classify_text.THICK_SPACE)
SOFT_BREAK = emit.SOFT_BREAK
UNKNOWN_EM = 0.5                # a character nobody measured
TAB_EM = 2.0
BOX_ROOM = 4.0                  # pt emit leaves under the last line's descent (emit.text_box_requests)

JsonMap = Mapping[str, Json]
Rect = tuple[float, float, float, float]   # x0, y0, x1, y1 in pt


@dataclass(frozen=True, kw_only=True)
class ParaStyle:
    """One paragraph's style as the layout reads it (lineSpacing as a factor)."""
    line_spacing: float
    indent_start: float
    indent_first_line: float
    indent_end: float
    space_above: float
    space_below: float
    bullet: bool
    alignment: str


@dataclass(frozen=True, kw_only=True)
class Line:
    """One laid-out line: its ink band, baseline, size (its largest run), paragraph, character
    range [start, end) of the layout's text, x where its words start, and its line spacing;
    `tab_to`: where its first tab takes the words after it, a hanging label's (`hanging_tab`), or
    None for a tab of TAB_EM."""
    box: Rect
    baseline: float
    size: float
    para: int
    start: int
    end: int
    x: float
    spacing: float
    tab_to: float | None


@dataclass(frozen=True, kw_only=True)
class Hole:
    """A formula hole (no-break spaces in the hole font): its band, characters and line index."""
    box: Rect
    start: int
    end: int
    line: int


@dataclass(frozen=True, kw_only=True)
class Layout:
    lines: tuple[Line, ...]
    holes: tuple[Hole, ...]
    bottom: float                      # the ink's bottom
    box: Rect
    text: str                          # the text, less Slides' own last newline
    styles: tuple[JsonObject, ...]     # each character's run style, its size filled in


@dataclass(frozen=True, kw_only=True)
class SpanBox:
    box: Rect
    line: int
    baseline: float


@dataclass(frozen=True, kw_only=True)
class Overrun:
    """A person's object (`object`) the source's `other` now runs over by `depth` pt."""
    object: str
    other: str
    depth: float


def number(m: JsonMap, key: str) -> float:
    """A number of the read-back; absent or null is 0 (as the old `get(key) or 0` read it)."""
    v = m.get(key)
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonShapeError(f"{key}: expected a number, found {type(v).__name__}")
    return float(v)


def _obj(m: JsonMap, key: str) -> JsonMap:
    """An object of the read-back; absent or null is empty."""
    v = m.get(key)
    return {} if v is None else as_object(v, key)


def _text(m: JsonMap, key: str) -> str:
    """A string of the read-back; absent, null or empty is ""."""
    v = m.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise JsonShapeError(f"{key}: expected a string, found {type(v).__name__}")
    return v


def numbers(v: Json, where: str) -> list[float]:
    out: list[float] = []
    for x in as_array(v, where):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise JsonShapeError(f"{where}: expected numbers")
        out.append(float(x))
    return out


def rect(v: Json, where: str) -> Rect:
    """A read-back's [x0, y0, x1, y1]."""
    b = numbers(v, where)
    if len(b) != 4:
        raise JsonShapeError(f"{where}: expected 4 numbers, found {len(b)}")
    return (b[0], b[1], b[2], b[3])


def box_of(rb: JsonMap) -> Rect | None:
    """The read-back's box, None when it has none."""
    v = rb.get("box")
    return None if not v else rect(v, "box")


def _style_name(st: JsonMap) -> str:
    # (Slides draws a weight of 700 or more with the family's bold face, whatever `bold` says:
    # emit.OPTICAL_WEIGHT's footlines)
    bold = bool(st.get("bold")) or number(st, "weight") >= 700
    italic = bool(st.get("italic"))
    if bold:
        return "bold_italic" if italic else "bold"
    return "italic" if italic else "regular"


def advance(ch: str, st: JsonMap, size: float) -> float:
    """Slides' advance of one character (pt) in a read-back run style at `size`."""
    family = _text(st, "fontFamily") or "Lato"
    if st.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT"):
        size *= emit.SCRIPT_SIZE
    if ch == "\t":
        return TAB_EM * size
    if ch in bidi.MARKS or ch in HOLE_BREAKS or ch == WORD_JOINER:
        return 0.0  # (an LRM or RLM draws nothing, nor does a hole's break or a word joiner)
    if family == emit.FONT_FOR_FAMILY["mono"]:
        return emit.ROBOTO_MONO_ADVANCE_EM * size
    written = face_advance(ch, family)  # (a script capital or operator in its face: emit_metrics.letter_faces)
    if written is not None:
        return written * size
    table = (emit.ADVANCES.get(family) or emit.ADVANCES["Lato"])[_style_name(st)]
    if ch == NBSP:
        ch = " "
    if st.get("smallCaps") and ch.islower():
        return table.get(ch.upper(), UNKNOWN_EM) * size * emit.SMALL_CAPS_SIZE
    em = table.get(ch)
    if em is None:
        em = emit.SYMBOL_ADVANCE_EM.get(ch, UNKNOWN_EM)
    return em * size


def font_size(st: JsonMap) -> float:
    """A run style's size; 0 when it has none."""
    return number(st, "fontSize")


def char_styles(rb: JsonMap, size: float | list[float] | None) -> list[JsonObject]:
    """The run style of every character of the read-back's text (a newline between runs takes the
    style before it). `size`: the font size where the read-back has none (a placeholder inherits
    its layout's), one for all or one per paragraph; None when no size can be known."""
    text = _text(rb, "text")
    out: list[JsonObject | None] = [None] * len(text)
    for k, span in enumerate(as_array(rb.get("run_spans") or [], "run_spans")):
        a, b, st = as_array(span, f"run_spans[{k}]")
        style = as_object(st, f"run_spans[{k}]")
        for i in range(max(0, as_int(a, "run span start")), min(as_int(b, "run span end"), len(text))):
            out[i] = style
    last: JsonObject = next((s for s in out if s is not None), {})
    styles: list[JsonObject] = []
    for s in out:
        if s is not None:
            last = s
        styles.append(last)
    if size:
        sizes = size if isinstance(size, list) else [size]
        para, filled_ = 0, list[JsonObject]()
        for ch, s in zip(text, styles):
            filled_.append(s if s.get("fontSize") else {**s, "fontSize": sizes[min(para, len(sizes) - 1)]})
            para += ch == "\n"
        styles = filled_
    return styles


def _para(s: JsonMap) -> ParaStyle:
    return ParaStyle(line_spacing=(number(s, "lineSpacing") or 100) / 100, indent_start=number(s, "indentStart"),
                     indent_first_line=number(s, "indentFirstLine"), indent_end=number(s, "indentEnd"),
                     space_above=number(s, "spaceAbove"), space_below=number(s, "spaceBelow"),
                     bullet=bool(s.get("bullet")), alignment=_text(s, "alignment") or "START")


def _paragraph_styles(rb: JsonMap) -> list[JsonObject]:
    return [as_object(s, "paragraph_styles") for s in as_array(rb.get("paragraph_styles") or [{}], "paragraph_styles")]


def para_style(rb: JsonMap) -> ParaStyle:
    """The paragraph style that lays the text out shortest: the read-back lists its distinct
    paragraph styles, not which paragraph has which, and an estimate that errs should err towards
    "it fits"."""
    styles = _paragraph_styles(rb)
    aligns = {_text(s, "alignment") or "START" for s in styles}
    return ParaStyle(line_spacing=min((number(s, "lineSpacing") or 100) for s in styles) / 100,
                     indent_start=min(number(s, "indentStart") for s in styles),
                     indent_first_line=min(number(s, "indentFirstLine") for s in styles),
                     indent_end=min(number(s, "indentEnd") for s in styles),
                     space_above=min(number(s, "spaceAbove") for s in styles),
                     space_below=min(number(s, "spaceBelow") for s in styles),
                     bullet=any(s.get("bullet") for s in styles),
                     alignment=aligns.pop() if len(aligns) == 1 else "START")


def para_styles(rb: JsonMap, n: int) -> list[ParaStyle]:
    """Each of the text's `n` paragraphs' style, as far as the read-back says it: it lists the
    distinct styles in the order they first appear, so one style is everybody's, as many styles as
    paragraphs is one each, and the first paragraph always has the first. Anywhere else the
    shortest (`para_style`) stands in."""
    styles = _paragraph_styles(rb)
    if len(styles) == 1 or len(styles) == n:
        return [_para(styles[min(i, len(styles) - 1)]) for i in range(n)]
    least = para_style(rb)
    return [_para(styles[0])] + [least] * (n - 1)


def upright(rb: JsonMap) -> bool:
    v = rb.get("transform")
    t = numbers(v, "transform") if v else [1.0, 0.0, 0.0, 1.0]
    return abs(t[1]) < 1e-6 and abs(t[2]) < 1e-6 and t[0] > 0 and t[3] > 0


def wrap(chars: str, styles: Sequence[JsonMap], width: float) -> list[tuple[int, int, float]]:
    """Greedy line breaks of one paragraph: (start, end, ink width) per line. Breaks at spaces,
    after hyphens and before a bracket opening after a Greek letter (`emit_widths.breaks_before`),
    never at a no-break space; a soft break ends a line; a word wider than the line
    is cut where it no longer fits, as Slides does.

    Nor before a no-break space: Slides keeps a space and the no-break spaces after it together
    (UAX #14's old "× GL"), so a word before a formula hole goes down with it (visual hunt r8,
    r1_math_v2 s6: "pointwise, / but ∫..." in a box 54 pt wider than "... pointwise, but"). The
    break emit writes before a hole (a LINE SEPARATOR, emit.HOLE_BREAK) breaks there."""
    lines: list[tuple[int, int, float]] = []
    start, n = 0, len(chars)
    while start <= n:
        w, last_break, i = 0.0, None, start
        ink_at_break = 0.0
        ink = 0.0
        end: int | None = None
        nxt = n
        while i < n:
            ch = chars[i]
            if ch == SOFT_BREAK:
                end, nxt = i, i + 1
                break
            adv = advance(ch, styles[i], font_size(styles[i]))
            if ch == " " or ch in HOLE_BREAKS:
                w += adv
                if ch in HOLE_BREAKS or chars[i + 1:i + 2] not in (NBSP, THICK_SPACE):
                    last_break, ink_at_break = i + 1, ink
                i += 1
                continue
            if i > start and breaks_before(chars, i):
                last_break, ink_at_break = i, ink
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


def hanging_tab(para: str, styles: Sequence[JsonMap], ps: ParaStyle) -> int | None:
    """Where the words after a hanging label start (the index after its tab), when the paragraph
    is `label<TAB>text` with the label inside its hang (indentFirstLine to indentStart): Slides
    takes that tab to indentStart, as emit writes hanging labels and tabbed lines (27_text_fit
    s7). None otherwise: a bullet, another alignment, no hang, a label running past it."""
    if ps.bullet or ps.alignment not in ("START", "JUSTIFIED") or ps.indent_first_line >= ps.indent_start:
        return None
    t = para.find("\t")
    if t < 0 or SOFT_BREAK in para[:t]:
        return None
    label = sum(advance(para[k], styles[k], font_size(styles[k])) for k in range(t))
    return t + 1 if ps.indent_first_line + label <= ps.indent_start else None


def x_at(ln: Line, text: str, styles: Sequence[JsonMap], k: int) -> float:
    """Where character `k` of a laid-out line starts: the advances before it from the line's x,
    its first tab taking the words to `tab_to` when it has one."""
    x, tabbed = ln.x, False
    for m in range(ln.start, k):
        if text[m] == "\t" and ln.tab_to is not None and not tabbed:
            x, tabbed = ln.tab_to, True
        else:
            x += advance(text[m], styles[m], font_size(styles[m]))
    return x


def layout(rb: JsonMap) -> Layout | None:
    """`layout_at` with the read-back's own sizes."""
    return layout_at(rb, None)


def _band(x0: float, x1: float, baseline: float, z: float) -> Rect:
    return (x0, baseline - CAP_EM * z, x1, baseline + DESC_EM * z)


def layout_at(rb: JsonMap, size: float | list[float] | None) -> Layout | None:
    """Where Slides sets the lines of a text box (see the module docstring); `size` as
    `char_styles` takes it. None for no text, a turned box, or a text whose size can't be known."""
    text = _text(rb, "text")
    if text.endswith("\n"):
        text = text[:-1]
    if not text.strip():
        return None
    frame = box_of(rb)
    if frame is None or not upright(rb):
        return None
    styles = char_styles({**rb, "text": text}, size)
    if any(not s.get("fontSize") for s in styles):
        return None
    x0, y0, x1, y1 = frame
    left = x0 + INSET_X
    right = x1 - INSET_X
    lines: list[Line] = []
    at = 0
    paras = text.split("\n")
    pss = para_styles(rb, len(paras))
    previous = 0.0   # size of the last line so far (read once `baseline` is set)
    baseline: float | None = None
    r_prev, below, bullet_prev = 1.0, 0.0, False
    for pi, para in enumerate(paras):
        ps = pss[pi]
        r = ps.line_spacing
        pst = styles[at:at + len(para)] or [styles[min(at, len(styles) - 1)]]
        indent = ps.indent_start
        # a paragraph keeps its indentEnd free before the box's edge (emit.paragraph_ends)
        end = right - ps.indent_end
        width = max(1.0, end - left - indent)
        hang = hanging_tab(para, pst, ps)
        if hang is None:
            broken = wrap(para, pst, width) if para else [(0, 0, 0.0)]
        else:  # the label stands in its hang, the words flow from indentStart on every line
            broken = [(a + hang if k else 0, b + hang, ink + (indent - ps.indent_first_line if k == 0 else 0.0))
                      for k, (a, b, ink) in enumerate(wrap(para[hang:], pst[hang:], width))]
        for li, (a, b, ink) in enumerate(broken):
            sizes: list[float] = [font_size(pst[k]) for k in range(a, b) if not para[k].isspace()] if b > a else []
            z = max(sizes) if sizes else font_size(pst[min(a, len(pst) - 1)])
            if baseline is None:
                baseline = y0 + emit.BASELINE_A + emit.ASCENT_EM * z + emit.extra_above(r, z)
            elif li == 0:
                # Between two list items a collapsing list drops both spaces; emit writes the gap as
                # the lower item's spaceAbove under `emit_text.LIST_SPACING`, which keeps it, and never
                # spaceAbove on an item it leaves collapsing (the read-back does not say spacingMode)
                gap = ps.space_above if ps.bullet and bullet_prev else ps.space_above + below
                baseline += emit.pitch_between(previous, r_prev, z, r, gap)  # (snapped with its gap)
            else:
                baseline += emit.line_pitch(previous, r, z)
            previous, r_prev = z, r
            if ps.alignment == "CENTER":
                lx = left + indent + (end - left - indent - ink) / 2
            elif ps.alignment == "END":
                lx = end - ink
            else:
                lx = left + (ps.indent_first_line if hang is not None and li == 0 else indent)
            bx = left + min(ps.indent_first_line, indent) if ps.bullet and li == 0 and ink > 0 else lx
            lines.append(Line(box=_band(min(bx, lx), lx + ink, baseline, z), baseline=baseline, size=z, para=pi,
                              start=at + a, end=at + b, x=lx, spacing=r,
                              tab_to=left + indent if hang is not None and li == 0 else None))
        at += len(para) + 1
        below, bullet_prev = ps.space_below, ps.bullet
    if not lines:
        return None
    align = _obj(rb, "shape_style").get("align")
    if align in ("MIDDLE", "BOTTOM"):
        top = lines[0].baseline - emit.ASCENT_EM * lines[0].size
        bottom = lines[-1].baseline + emit.DESCENT_EM * lines[-1].size
        want = (y0 + y1) / 2 - (bottom - top) / 2 if align == "MIDDLE" else y1 - emit.BASELINE_A - (bottom - top)
        shift = want - top
        lines = [dataclasses.replace(ln, baseline=ln.baseline + shift,
                                     box=(ln.box[0], ln.box[1] + shift, ln.box[2], ln.box[3] + shift))
                 for ln in lines]
    holes: list[Hole] = []
    for li, ln in enumerate(lines):
        k = ln.start
        while k < ln.end:
            if text[k] == NBSP and styles[k].get("fontFamily") == emit.HOLE_FONT:
                j = k
                while j < ln.end and text[j] == NBSP and styles[j].get("fontFamily") == emit.HOLE_FONT:
                    j += 1
                hx = x_at(ln, text, styles, k)
                holes.append(Hole(box=_band(hx, x_at(ln, text, styles, j), ln.baseline, ln.size), start=k, end=j,
                                  line=li))
                k = j
            else:
                k += 1
    return Layout(lines=tuple(lines), holes=tuple(holes), bottom=max(ln.box[3] for ln in lines), box=frame,
                  text=text, styles=tuple(styles))


def span_box(lay: Layout, a: int, b: int) -> SpanBox | None:
    """Where characters [a, b) of a laid-out text stand, when they are on one line: their ink band,
    the line's index and baseline. None when they are not all on one line."""
    text, styles = lay.text, lay.styles
    for i, ln in enumerate(lay.lines):
        if ln.start <= a < max(ln.end, ln.start + 1) and b <= ln.end:
            return SpanBox(box=_band(x_at(ln, text, styles, a), x_at(ln, text, styles, b), ln.baseline, ln.size),
                           line=i, baseline=ln.baseline)
    return None


def needed_bottom(lay: Layout) -> float:
    """Where a box has to end to hold its last line the way emit sizes a box it creates: that
    line's descent, the room its line spacing adds below it, and 4 pt (`emit.text_box_requests`)."""
    last = lay.lines[-1]
    z, r = last.size, last.spacing
    return last.baseline + emit.DESCENT_EM * z + emit.extra_below(r, z) + BOX_ROOM


def text_box(rb: JsonMap) -> bool:
    st = _obj(rb, "shape_style")
    return rb.get("kind") == "shape" and (st.get("type") == "TEXT_BOX" or bool(rb.get("placeholder")))


def line_rects(lay: Layout) -> list[Rect]:
    """The laid-out lines' ink bands, less the empty ones."""
    return [ln.box for ln in lay.lines if ln.box[2] - ln.box[0] > 0.5]


def ink(rb: JsonMap) -> list[Rect] | None:
    """Where an object puts ink, slide pt: a text box's laid-out lines, a picture's or a table's box;
    None for anything else or what the model cannot lay out."""
    frame = box_of(rb)
    if frame is None or not upright(rb):
        return None
    if rb.get("kind") in ("image", "table"):
        return [frame]
    if text_box(rb):
        lay = layout(rb)
        return None if lay is None else line_rects(lay)
    return None


def meet(ra: Sequence[Rect], rb: Sequence[Rect], tol: float) -> tuple[float, int, int] | None:
    """The deepest overlap of two lists of rectangles: (its lesser side in pt, index in ra, index in
    rb), or None when no two overlap by `tol` pt each way."""
    best: tuple[float, int, int] | None = None
    for i, a in enumerate(ra):
        for j, b in enumerate(rb):
            w = min(a[2], b[2]) - max(a[0], b[0])
            h = min(a[3], b[3]) - max(a[1], b[1])
            if w >= tol and h >= tol and (best is None or min(w, h) > best[0]):
                best = (min(w, h), i, j)
    return best


OVERRUN_MIN = 2.0   # pt each way before the source's ink counts as over a person's object


def overruns(before: JsonMap, after: JsonMap, users: Set[str], skip: Set[str]) -> list[Overrun]:
    """The person's own objects (`users`, untouched by the sync) that the source's text or pictures
    now run over, where they did not before, the deepest per object. `skip`: objects about to be
    deleted. Each other object is judged against its own meet before (a
    recreated one found by its b2s title): a note already over the frame counter still counts the
    paragraph that now reaches it (live fuzz r7411). That is also what lets a picture on a picture
    count: a collage the person made overlapped before the sync too, while the source's figure grown
    over the person's copy of the old one did not (edit hunt h3-1, once skipped as a collage)."""
    objs_a = {o: as_object(rb, o) for o, rb in as_object(after["objects"], "objects").items()}
    objs_b = {o: as_object(rb, o) for o, rb in as_object(before["objects"], "objects").items()}
    out: list[Overrun] = []
    inks_a = {o: ink(rb) for o, rb in objs_a.items() if o not in skip}
    inks_b = {o: ink(rb) for o, rb in objs_b.items()}
    # (not the person's own: a Ctrl+D copy carries its original's title, and read as the recreated
    # original's old self it was "already over" itself)
    by_title = {_text(rb, "title"): o for o, rb in objs_b.items()
                if _text(rb, "title").startswith("b2s:") and o not in users}
    for u in sorted(users):
        ua, ub = objs_a.get(u), objs_b.get(u)
        mine = inks_a.get(u)
        if ua is None or ub is None or not mine or ua.get("box") != ub.get("box"):
            continue
        best: tuple[float, str] | None = None
        for o, r in inks_a.items():
            if o == u or o in users or not r:
                continue
            now = meet(mine, r, OVERRUN_MIN)
            if now is None:
                continue
            title = objs_a[o].get("title")
            old = o if o in objs_b else by_title.get(title) if isinstance(title, str) else None
            was_u, was_o = inks_b.get(u), inks_b.get(old) if old else None
            was = meet(was_u, was_o, OVERRUN_MIN) if old and was_u and was_o else None
            if (was is None or now[0] > was[0] + OVERRUN_MIN) and (best is None or now[0] > best[0]):
                best = (now[0], o)
        if best:
            out.append(Overrun(object=u, other=best[1], depth=round(best[0], 1)))
    return out


def overrun_json(o: Overrun) -> JsonObject:
    return {"object": o.object, "other": o.other, "depth": o.depth}


def filled(rb: JsonMap) -> bool:
    """A shape that hides what is under it (a block's panel): an opaque fill, and not a text box."""
    fill = _obj(_obj(rb, "shape_style"), "fill")
    return rb.get("kind") == "shape" and not text_box(rb) and fill.get("color") is not None and number(fill, "alpha") > 0.5
