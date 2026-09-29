"""Does text fit in Slides as it does in the PDF? Wraps, drift, growth and collisions per slide.

Run after `convert` and `fidelity` (which saves Google's thumbnails):

    python tools/text_fit.py tests/decks/out/27_text_fit.pdf --out out/27_text_fit

The substitute fonts are a few per cent wider or narrower than the PDF's, and each way that shows
is a finding here, measured on Google's own renderer against the PDF page, in PDF points:

- `wrap`    a text element has more (or fewer) lines in Slides than in the PDF;
- `drift`   the edge a paragraph is aligned to moved (left edge for left/justified text, the
            centre for centred text, the right edge for flush-right text), or its top, by more
            than `DRIFT_PT`;
- `width`   with the same lines, the text is more than `WIDTH_TOL` wider or narrower (a face
            the substitute sets at another size - small caps - or measures differently);
- `crowded` the same lines, but runs of ink that are apart in the PDF touch in Slides (Slides
            sets subscripts deeper than TeX, so a script reaches into the line below);
- `grown`   a table or picture ends lower than in the PDF by more than `GROWN_PT` (a wrapped
            cell doubles its row, and the table grows down over what is below), or a text with
            the same lines is taller (Slides' line pitch or paragraph spacing is larger);
- `touch`   the white space between two elements in the PDF (one above or left of the other)
            has closed in Slides to under a quarter of itself and under 1.5 pt: the caption a table
            grew over, a column run into the next one.

Writes `<out>/text_fit.json`; the exit status is the number of slides with a finding.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from ..arrays import Ints, Mask, SignedRGB
from ..fidelity import rgb_array, rgb_array_at, bands
from ..json_types import Json, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_optional_str, as_str
from ..pdf import Document

DRIFT_PT = 2.0
GROWN_PT = 2.0
WIDTH_TOL = 0.08  # the calibrated substitutes land within a few per cent
NO_SIZE = 10.0    # the size an element without paragraphs (a table, a picture) is measured at

# A box in pixels: x0, y0, x1, y1.
PxBox = tuple[int, int, int, int]
# A box in PDF points: x0, y0, x1, y1.
PtBox = tuple[float, float, float, float]

# The element kinds measured: what Slides sets or scales (a shape is only its fill).
FitKind = Literal["text", "table", "image", "diagram"]


# --- deck.json, as far as text_fit reads it ---------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class FitParagraph:
    size: float
    align: str               # "left" when the paragraph says none
    baselines: list[float]   # its PDF lines' baselines, pt


@dataclass(frozen=True, kw_only=True)
class FitElement:
    id: str
    kind: FitKind
    bbox: PtBox
    paragraphs: list[FitParagraph]
    name: str                # how findings name it: its id and its first words, or its kind


@dataclass(frozen=True, kw_only=True)
class FitSlide:
    page: int                # 0-based
    label: str | None
    width: float             # pt
    elements: list[FitElement]   # the measured kinds, footers left out


def _number(value: Json, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raise JsonShapeError(f"{where}: a number was expected")


def _fit_kind(value: str) -> FitKind | None:
    if value == "text" or value == "table" or value == "image" or value == "diagram":
        return value
    return None


def _paragraph(o: JsonObject, where: str) -> FitParagraph:
    lines: list[JsonObject] = as_objects(o["lines"], f"{where}.lines") if "lines" in o else []
    return FitParagraph(size=_number(o["size"], f"{where}.size"),
                        align=as_optional_str(o.get("align"), f"{where}.align") or "left",
                        baselines=[_number(ln["baseline"], f"{where}.lines[{i}].baseline") for i, ln in enumerate(lines)])


def _element(o: JsonObject, kind: FitKind, where: str) -> FitElement:
    eid = as_str(o["id"], f"{where}.id")
    box = [_number(v, f"{where}.bbox") for v in as_array(o["bbox"], f"{where}.bbox")]
    if len(box) != 4:
        raise JsonShapeError(f"{where}.bbox: four numbers were expected")
    raw: list[JsonObject] = as_objects(o["paragraphs"], f"{where}.paragraphs") if "paragraphs" in o else []
    name = f"{eid} [{kind}]"
    if kind == "text" and raw:
        runs = as_objects(raw[0]["runs"], f"{where}.paragraphs[0].runs")
        text = "".join(as_str(r.get("text", ""), f"{where}.paragraphs[0].runs.text") for r in runs).strip()
        name = f"{eid} {text[:30]!r}"
    return FitElement(id=eid, kind=kind, bbox=(box[0], box[1], box[2], box[3]),
                      paragraphs=[_paragraph(p, f"{where}.paragraphs[{i}]") for i, p in enumerate(raw)],
                      name=name)


def fit_slide(o: JsonObject, where: str) -> FitSlide:
    """A deck.json slide as text_fit reads it: its page, label, width and the elements measured."""
    elements: list[FitElement] = []
    for i, e in enumerate(as_objects(o["elements"], f"{where}.elements")):
        kind = _fit_kind(as_str(e["kind"], f"{where}.elements[{i}].kind"))
        if kind is None or e.get("role") == "footer":
            continue
        elements.append(_element(e, kind, f"{where}.elements[{i}]"))
    size = as_array(o["size"], f"{where}.size")
    return FitSlide(page=as_int(o["page"], f"{where}.page"), label=as_optional_str(o.get("label"), f"{where}.label"),
                    width=_number(size[0], f"{where}.size"), elements=elements)


# --- findings --------------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class Missing:
    """The element has no ink on one side."""
    element: str
    where: Literal["slides", "pdf"]

    @property
    def kind(self) -> Literal["missing"]:
        return "missing"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "where": self.where}


@dataclass(frozen=True, kw_only=True)
class Wrap:
    element: str
    lines_pdf: int
    lines_slides: int
    height_pt: float

    @property
    def kind(self) -> Literal["wrap"]:
        return "wrap"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "lines_pdf": self.lines_pdf,
                "lines_slides": self.lines_slides, "height_pt": self.height_pt}


@dataclass(frozen=True, kw_only=True)
class Crowded:
    element: str
    lines_pdf: int
    lines_slides: int

    @property
    def kind(self) -> Literal["crowded"]:
        return "crowded"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "lines_pdf": self.lines_pdf,
                "lines_slides": self.lines_slides}


@dataclass(frozen=True, kw_only=True)
class Drift:
    element: str
    line: int      # 1-based
    align: str     # the paragraph's alignment, or "top" for the first line's vertical drift
    pt: float

    @property
    def kind(self) -> Literal["drift"]:
        return "drift"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "line": self.line, "align": self.align, "pt": self.pt}


@dataclass(frozen=True, kw_only=True)
class Width:
    element: str
    line: int      # 1-based
    ratio: float   # Slides' width over the PDF's

    @property
    def kind(self) -> Literal["width"]:
        return "width"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "line": self.line, "ratio": self.ratio}


@dataclass(frozen=True, kw_only=True)
class Grown:
    element: str
    pt: float

    @property
    def kind(self) -> Literal["grown"]:
        return "grown"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "pt": self.pt}


@dataclass(frozen=True, kw_only=True)
class Touch:
    element: str
    other: str
    direction: Literal["below", "right"]
    gap_pdf_pt: float
    gap_slides_pt: float

    @property
    def kind(self) -> Literal["touch"]:
        return "touch"

    def json(self) -> JsonObject:
        return {"kind": self.kind, "element": self.element, "other": self.other, "direction": self.direction,
                "gap_pdf_pt": self.gap_pdf_pt, "gap_slides_pt": self.gap_slides_pt}


Finding = Missing | Wrap | Crowded | Drift | Width | Grown | Touch


@dataclass(frozen=True, kw_only=True)
class SlideFit:
    page: int
    label: str | None
    findings: list[Finding]

    def json(self) -> JsonObject:
        return {"page": self.page, "label": self.label, "findings": [f.json() for f in self.findings]}


@dataclass(frozen=True, kw_only=True)
class FitReport:
    pdf: str
    slides: list[SlideFit]

    def json(self) -> JsonObject:
        return {"pdf": self.pdf, "slides": [s.json() for s in self.slides]}


# --- measuring -------------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class _Measured:
    """One element's ink on both sides (`r` the PDF's, `s` Slides'), within its window `win`."""
    el: FitElement
    size: float
    ref: PxBox | None
    slides: PxBox | None
    r: Mask
    s: Mask
    win: PxBox


@dataclass(frozen=True, kw_only=True)
class _Worst:
    """The worst line so far of one kind of line finding."""
    value: float
    line: int      # 0-based
    align: str


def _box(mask: Mask) -> PxBox | None:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _ink(ref: SignedRGB, sl: SignedRGB, win: PxBox, box: PxBox) -> tuple[Mask, Mask]:
    """Ink in a window of both images, against the ground the text stands on: the commonest
    colour inside its own box (`box`, pixels) in the PDF render - a block's title bar, not the
    body panel the window also reaches into. A native panel is in both images and in neither
    background, so ink measured against the uploaded background would count its fill as text."""
    a0, b0, a1, b1 = win
    r, s = ref[b0:b1, a0:a1].astype(int), sl[b0:b1, a0:a1].astype(int)
    own = ref[max(0, box[1]):box[3], max(0, box[0]):box[2]].astype(int)
    colours, counts = np.unique((own // 8).reshape(-1, 3), axis=0, return_counts=True)
    ground = colours[counts.argmax()] * 8 + 4
    mr = np.abs(r - ground).max(axis=2) > 60
    ms = np.abs(s - ground).max(axis=2) > 60
    for m in (mr, ms):  # rules and rims: rows inked across most of the window, columns down all
        m[m.mean(axis=1) > 0.6, :] = False  # of it (a glyph's stem is most of a one-line window)
        m[:, m.mean(axis=0) > 0.95] = False
    return mr, ms


def _runs(on: Ints, join: int) -> list[tuple[int, int]]:
    """Runs of True in a profile, given as the indices `on` where it is True, joined across gaps
    shorter than `join`."""
    out: list[tuple[int, int]] = []
    for v in on:
        i = int(v)
        if out and i - out[-1][1] < join:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
    return out


def _on(profile: Mask | np.bool_) -> Ints:
    """The indices where a profile is True (numpy's older stubs, those Python 3.10 gets, say a
    mask's `any(axis=...)` may be a scalar)."""
    return np.flatnonzero(profile).astype(np.int64)


def _own(m: Mask, x0: int, x1: int, size_px: float) -> None:
    """Keep only the ink that is this element's, in place: lines at least 0.3 em tall (not a
    panel's corner or rim), and in each line the runs of words - joined across word spaces -
    that reach into the element's own columns. A line spilling past its box stays whole; a
    neighbour's word beyond a gap does not come with it."""
    for top, bottom in _runs(_on(m.any(axis=1)), max(1, round(0.12 * size_px))):
        if bottom - top < 0.3 * size_px:
            m[top:bottom] = False
            continue
        for a, b in _runs(_on(m[top:bottom].any(axis=0)), max(1, round(0.6 * size_px))):
            if b < x0 - 0.3 * size_px or a > x1 + 0.3 * size_px:
                m[top:bottom, a:b] = False


def _pt(px: float, scale: float) -> float:
    return round(px / scale, 1)


def _line_findings(e: FitElement, r: Mask, s: Mask, gap: int, size: float, scale: float) -> list[Finding]:
    """With the same number of lines on both sides, line i of the PDF against line i of Slides:
    the worst drift of the edge its paragraph is aligned to, the worst width ratio, the first
    line's vertical drift and how much further the last one went (the pitch)."""
    rows_r, rows_s = _runs(_on(r.any(axis=1)), gap), _runs(_on(s.any(axis=1)), gap)
    marks = [(baseline * scale, p.align) for p in e.paragraphs for baseline in p.baselines]
    first_align = e.paragraphs[0].align if e.paragraphs else "left"
    drift_worst: _Worst | None = None
    width_worst: _Worst | None = None
    for i, ((t, b), (t2, b2)) in enumerate(zip(rows_r, rows_s)):
        xr, xs = _on(r[t:b].any(axis=0)), _on(s[t2:b2].any(axis=0))
        if not len(xr) or not len(xs):
            continue
        r0, r1 = int(xr[0]), int(xr[-1]) + 1
        s0, s1 = int(xs[0]), int(xs[-1]) + 1
        near = min(marks, key=lambda m: abs(m[0] - b), default=None)
        align = near[1] if near else first_align
        drift = ((s0 + s1) - (r0 + r1)) / 2 if align == "center" else (s1 - r1) if align == "right" else (s0 - r0)
        ratio = (s1 - s0) / max(1, r1 - r0)
        if abs(drift) / scale > DRIFT_PT and (drift_worst is None or abs(drift) > abs(drift_worst.value)):
            drift_worst = _Worst(value=drift, line=i, align=align)
        if abs(ratio - 1) > WIDTH_TOL and (r1 - r0) > 2 * size * scale and \
                (width_worst is None or abs(ratio - 1) > abs(width_worst.value - 1)):
            width_worst = _Worst(value=ratio, line=i, align=align)
    out: list[Finding] = []
    if drift_worst is not None:
        out.append(Drift(element=e.name, line=drift_worst.line + 1, align=drift_worst.align,
                         pt=_pt(drift_worst.value, scale)))
    if width_worst is not None:
        out.append(Width(element=e.name, line=width_worst.line + 1, ratio=round(width_worst.value, 3)))
    if rows_r and rows_s:
        top = rows_s[0][0] - rows_r[0][0]
        if abs(top) / scale > DRIFT_PT:
            out.append(Drift(element=e.name, line=1, align="top", pt=_pt(top, scale)))
        pitch = (rows_s[-1][1] - rows_r[-1][1]) - top
        if abs(pitch) / scale > GROWN_PT:
            out.append(Grown(element=e.name, pt=_pt(pitch, scale)))
    return out


def measure_slide(slide: FitSlide, ref: SignedRGB, sl: SignedRGB, scale: float, crops: Path | None) -> SlideFit:
    """Findings for one slide. `scale` = pixels per PDF point; `ref` and `sl` are RGB arrays of
    the PDF page and Google's thumbnail at the same size; each finding's window is saved to
    `crops` when given."""
    els = slide.elements
    info: list[_Measured] = []
    h, w = ref.shape[:2]
    for e in els:
        size = max((p.size for p in e.paragraphs), default=NO_SIZE)
        x0, y0, x1, y1 = e.bbox
        # The element's own window: its box, generous to the right and a line and a half below,
        # where a wider font spills and a wrapped line lands, but stopping at the nearest element on each side (their ink is theirs).
        l, t, rt, bt = x0 - 0.5 * size, y0 - 0.3 * size, x1 + 3 * size, y1 + 1.5 * size
        for o in els:
            if o is e:
                continue
            ox0, oy0, ox1, oy1 = o.bbox
            if oy0 < y1 and y0 < oy1:  # beside it
                if ox0 >= x1 - 0.5:
                    rt = min(rt, ox0)
                elif ox1 <= x0 + 0.5:
                    l = max(l, ox1, x0 - 0.5)  # its own box: what grows across the gap is the other's
            if ox0 < x1 and x0 < ox1:  # above or below it
                if oy0 >= y1 - 0.5:
                    bt = min(bt, oy0)
                elif oy1 <= y0 + 0.5:
                    t = max(t, oy1, y0 - 0.5)
        win = (max(0, int(l * scale)), max(0, int(t * scale)), min(w, int(rt * scale)), min(h, int(bt * scale)))
        mr, ms = _ink(ref, sl, win, (int(x0 * scale), int(y0 * scale), int(x1 * scale) + 1, int(y1 * scale) + 1))
        if e.kind == "text":
            for m in (mr, ms):
                _own(m, int(x0 * scale) - win[0], int(x1 * scale) - win[0], size * scale)
        r, s = np.zeros((h, w), bool), np.zeros((h, w), bool)
        r[win[1]:win[3], win[0]:win[2]], s[win[1]:win[3], win[0]:win[2]] = mr, ms
        info.append(_Measured(el=e, size=size, ref=_box(r), slides=_box(s), r=r, s=s, win=win))

    findings: list[Finding] = []
    for it in info:
        e, br, bs, size = it.el, it.ref, it.slides, it.size
        if br is None or bs is None:
            findings.append(Missing(element=e.name, where="slides" if bs is None else "pdf"))
            continue
        gap = max(1, round(0.12 * size * scale))
        if e.kind == "text":
            lr, ls = bands(it.r, gap), bands(it.s, gap)
            # A wrap adds (or takes away) a line of height too: bands alone also change where
            # Slides' deeper subscripts or a panel's corner join or split two runs of rows.
            grew = (bs[3] - bs[1]) - (br[3] - br[1])
            if lr != ls and abs(grew) >= 0.5 * size * scale:
                findings.append(Wrap(element=e.name, lines_pdf=lr, lines_slides=ls, height_pt=_pt(grew, scale)))
            elif ls < lr:
                findings.append(Crowded(element=e.name, lines_pdf=lr, lines_slides=ls))
            if lr == ls:
                findings += _line_findings(e, it.r, it.s, gap, size, scale)
        elif (bs[3] - br[3]) / scale > GROWN_PT:
            findings.append(Grown(element=e.name, pt=_pt(bs[3] - br[3], scale)))

    # Collisions: A before B (above it, or left of it, overlapping across the other axis). A's
    # window runs up to B's box and B's starts at it, so A's ink in Slides is measured right up
    # to B: the white space between them in the PDF has (nearly) closed.
    for a in info:
        for b in info:
            ra, sa, rb = a.ref, a.slides, b.ref
            if a is b or ra is None or sa is None or rb is None:
                continue
            for axis in (1, 0):  # 1: A above B; 0: A left of B
                lo, hi = (0, 2) if axis == 1 else (1, 3)
                if min(ra[hi], rb[hi]) - max(ra[lo], rb[lo]) < 5 * scale:
                    continue  # not facing each other across this axis
                gap_pdf, gap_sl = rb[axis] - ra[axis + 2], rb[axis] - sa[axis + 2]
                if gap_pdf >= scale and gap_sl < min(1.5 * scale, 0.25 * gap_pdf):
                    findings.append(Touch(element=a.el.name, other=b.el.name,
                                          direction="below" if axis == 1 else "right",
                                          gap_pdf_pt=_pt(gap_pdf, scale), gap_slides_pt=_pt(gap_sl, scale)))
    if crops is not None:
        crops.mkdir(parents=True, exist_ok=True)
        named = {it.el.name: it for it in info}
        for i, f in enumerate(findings):
            found = named.get(f.element)
            if found is None:
                continue
            a0, b0, a1, b1 = found.win
            pair = np.vstack([found.r[b0:b1, a0:a1], np.ones((3, a1 - a0), bool), found.s[b0:b1, a0:a1]])
            Image.fromarray(~pair).save(crops / f"{slide.page + 1:03}-{i}-{f.kind}.png")
    return SlideFit(page=slide.page, label=slide.label, findings=findings)


def measure(pdf: Path, out: Path, crops: bool) -> FitReport:
    """Every slide of the deck converted into `out` (its deck.json, the thumbnails `fidelity`
    saved and the backgrounds' size) against the PDF; writes `<out>/text_fit.json`, and each
    finding's window into `<out>/text_fit/` when `crops`."""
    where = str(out / "deck.json")
    deck = as_object(json.loads((out / "deck.json").read_text(encoding="utf-8")), where)
    doc = Document(pdf)
    slides: list[SlideFit] = []
    for k, raw in enumerate(as_objects(deck["slides"], f"{where}: slides")):
        slide = fit_slide(raw, f"{where}: slides[{k}]")
        n = slide.page
        thumb_path = out / "fidelity" / f"slides-{n + 1:03}.png"
        if not thumb_path.exists():
            raise SystemExit(f"{thumb_path} missing: run `python -m beamer2slides fidelity {pdf} --out {out}` first")
        thumb = rgb_array(Image.open(thumb_path))
        h, w = thumb.shape[:2]
        # The PDF page rendered as the background was, then scaled to the thumbnail's size.
        bg_width = Image.open(out / "backgrounds" / f"bg-{n + 1:03}.png").width
        ref = rgb_array_at(Image.fromarray(doc[n].render(bg_width / doc[n].width)), (w, h))
        slides.append(measure_slide(slide, ref, thumb, w / slide.width, out / "text_fit" if crops else None))
    report = FitReport(pdf=str(pdf), slides=slides)
    (out / "text_fit.json").write_text(json.dumps(report.json(), indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def print_report(report: FitReport) -> int:
    bad = 0
    for s in report.slides:
        if not s.findings:
            continue
        bad += 1
        print(f"slide {s.page + 1}" + (f" ({s.label})" if s.label else ""))
        for f in s.findings:
            rest = {k: v for k, v in f.json().items() if k not in ("kind", "element")}
            print(f"  {f.kind:<7} {f.element}  {rest}")
    counts: dict[str, int] = {}
    for s in report.slides:
        for f in s.findings:
            counts[f.kind] = counts.get(f.kind, 0) + 1
    print(f"{bad} of {len(report.slides)} slides with findings: {counts or 'none'}")
    return bad


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="the convert/fidelity output folder")
    ap.add_argument("--crops", action="store_true",
                    help="save each finding's window to <out>/text_fit/: the PDF's ink above, Slides' below")
    args = ap.parse_args(argv)
    pdf: Path = args.pdf
    out: Path = args.out
    crops: bool = args.crops
    return print_report(measure(pdf, out, crops))


if __name__ == "__main__":
    raise SystemExit(main(None))
