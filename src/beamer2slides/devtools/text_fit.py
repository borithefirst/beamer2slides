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
from pathlib import Path

import numpy as np
from PIL import Image

from ..fidelity import rgb_array, bands
from ..pdf import Document

DRIFT_PT = 2.0
GROWN_PT = 2.0
WIDTH_TOL = 0.08  # the calibrated substitutes land within a few per cent


def _box(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _ink(ref: np.ndarray, sl: np.ndarray, win: tuple, box: tuple) -> tuple[np.ndarray, np.ndarray]:
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


def _runs(profile: np.ndarray, join: int) -> list[tuple[int, int]]:
    """Runs of True in a profile, joined across gaps shorter than `join`."""
    out: list[list[int]] = []
    for i in np.flatnonzero(profile):
        if out and i - out[-1][1] < join:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [tuple(r) for r in out]


def _own(m: np.ndarray, x0: int, x1: int, size_px: float) -> None:
    """Keep only the ink that is this element's, in place: lines at least 0.3 em tall (not a
    panel's corner or rim), and in each line the runs of words - joined across word spaces -
    that reach into the element's own columns. A line spilling past its box stays whole; a
    neighbour's word beyond a gap does not come with it."""
    for top, bottom in _runs(m.any(axis=1), max(1, round(0.12 * size_px))):
        if bottom - top < 0.3 * size_px:
            m[top:bottom] = False
            continue
        for a, b in _runs(m[top:bottom].any(axis=0), max(1, round(0.6 * size_px))):
            if b < x0 - 0.3 * size_px or a > x1 + 0.3 * size_px:
                m[top:bottom, a:b] = False


def _line_findings(e: dict, name: str, r: np.ndarray, s: np.ndarray, gap: int, size: float,
                   scale: float) -> list[dict]:
    """With the same number of lines on both sides, line i of the PDF against line i of Slides:
    the worst drift of the edge its paragraph is aligned to, the worst width ratio, the first
    line's vertical drift and how much further the last one went (the pitch)."""
    pt = lambda px: round(float(px) / scale, 1)
    rows_r, rows_s = _runs(r.any(axis=1), gap), _runs(s.any(axis=1), gap)
    marks = [(ln["baseline"] * scale, p.get("align") or "left", ln.get("x0"), ln.get("x1"))
             for p in e.get("paragraphs", []) for ln in p.get("lines", [])]
    worst: dict[str, tuple] = {}
    for i, ((t, b), (t2, b2)) in enumerate(zip(rows_r, rows_s)):
        xr, xs = np.flatnonzero(r[t:b].any(axis=0)), np.flatnonzero(s[t2:b2].any(axis=0))
        if not len(xr) or not len(xs):
            continue
        (r0, r1), (s0, s1) = (xr[0], xr[-1] + 1), (xs[0], xs[-1] + 1)
        near = min(marks, key=lambda m: abs(m[0] - b), default=None)
        align = near[1] if near else ((e.get("paragraphs") or [{}])[0].get("align") or "left")
        drift = ((s0 + s1) - (r0 + r1)) / 2 if align == "center" else (s1 - r1) if align == "right" else (s0 - r0)
        ratio = (s1 - s0) / max(1, r1 - r0)
        for kind, value, bad in (("drift", drift, abs(drift) / scale > DRIFT_PT),
                                 ("width", ratio, abs(ratio - 1) > WIDTH_TOL and (r1 - r0) > 2 * size * scale)):
            if bad and (kind not in worst or abs(value - (kind == "width")) > abs(worst[kind][0] - (kind == "width"))):
                worst[kind] = (value, i, align)
    out = []
    if "drift" in worst:
        v, i, align = worst["drift"]
        out.append({"kind": "drift", "element": name, "line": i + 1, "align": align, "pt": pt(float(v))})
    if "width" in worst:
        v, i, _ = worst["width"]
        out.append({"kind": "width", "element": name, "line": i + 1, "ratio": round(float(v), 3)})
    if rows_r and rows_s:
        top = rows_s[0][0] - rows_r[0][0]
        if abs(top) / scale > DRIFT_PT:
            out.append({"kind": "drift", "element": name, "line": 1, "align": "top", "pt": pt(top)})
        pitch = (rows_s[-1][1] - rows_r[-1][1]) - top
        if abs(pitch) / scale > GROWN_PT:
            out.append({"kind": "grown", "element": name, "pt": pt(pitch)})
    return out


def measure_slide(slide: dict, ref: np.ndarray, sl: np.ndarray, scale: float, crops: Path | None = None) -> dict:
    """Findings for one slide. `scale` = pixels per PDF point; `ref` and `sl` are RGB arrays of
    the PDF page and Google's thumbnail at the same size."""
    pt = lambda px: round(px / scale, 1)
    els = [e for e in slide["elements"] if e["kind"] in ("text", "table", "image", "diagram")
           and e.get("role") not in ("footer",)]
    info = []
    h, w = ref.shape[:2]
    full_r = np.zeros((h, w), bool)
    full_s = np.zeros((h, w), bool)
    for e in els:
        size = max((p["size"] for p in e.get("paragraphs", [])), default=10.0)
        x0, y0, x1, y1 = e["bbox"]
        # The element's own window: its box, generous to the right and a line and a half below,
        # where a wider font spills and a wrapped line lands, but stopping at the nearest element on each side (their ink is theirs).
        l, t, rt, bt = x0 - 0.5 * size, y0 - 0.3 * size, x1 + 3 * size, y1 + 1.5 * size
        for o in els:
            if o is e:
                continue
            ox0, oy0, ox1, oy1 = o["bbox"]
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
        if e["kind"] == "text":
            for m in (mr, ms):
                _own(m, int(x0 * scale) - win[0], int(x1 * scale) - win[0], size * scale)
        r, s = np.zeros((h, w), bool), np.zeros((h, w), bool)
        r[win[1]:win[3], win[0]:win[2]], s[win[1]:win[3], win[0]:win[2]] = mr, ms
        full_r |= r
        full_s |= s
        info.append({"el": e, "size": size, "ref": _box(r), "slides": _box(s), "r": r, "s": s, "win": win})
    ref, sl = full_r, full_s  # for the collision walks: the elements' own ink, nothing else

    findings = []
    for it in info:
        e, br, bs, size = it["el"], it["ref"], it["slides"], it["size"]
        name = _name(e)
        if br is None or bs is None:
            findings.append({"kind": "missing", "element": name, "where": "slides" if bs is None else "pdf"})
            continue
        gap = max(1, round(0.12 * size * scale))
        if e["kind"] == "text":
            lr, ls = bands(it["r"], gap), bands(it["s"], gap)
            # A wrap adds (or takes away) a line of height too: bands alone also change where
            # Slides' deeper subscripts or a panel's corner join or split two runs of rows.
            grew = (bs[3] - bs[1]) - (br[3] - br[1])
            if lr != ls and abs(grew) >= 0.5 * size * scale:
                findings.append({"kind": "wrap", "element": name, "lines_pdf": lr, "lines_slides": ls,
                                 "height_pt": pt(grew)})
            elif ls < lr:
                findings.append({"kind": "crowded", "element": name, "lines_pdf": lr, "lines_slides": ls})
            if lr == ls:
                findings += _line_findings(e, name, it["r"], it["s"], gap, size, scale)
        elif (bs[3] - br[3]) / scale > GROWN_PT:
            findings.append({"kind": "grown", "element": name, "pt": pt(bs[3] - br[3])})

    # Collisions: A before B (above it, or left of it, overlapping across the other axis). A's
    # window runs up to B's box and B's starts at it, so A's ink in Slides is measured right up
    # to B: the white space between them in the PDF has (nearly) closed.
    for a in info:
        for b in info:
            ra, sa, rb = a["ref"], a["slides"], b["ref"]
            if a is b or ra is None or sa is None or rb is None:
                continue
            for axis in (1, 0):  # 1: A above B; 0: A left of B
                lo, hi = (0, 2) if axis == 1 else (1, 3)
                if min(ra[hi], rb[hi]) - max(ra[lo], rb[lo]) < 5 * scale:
                    continue  # not facing each other across this axis
                gap_pdf, gap_sl = rb[axis] - ra[axis + 2], rb[axis] - sa[axis + 2]
                if gap_pdf >= scale and gap_sl < min(1.5 * scale, 0.25 * gap_pdf):
                    findings.append({"kind": "touch", "element": _name(a["el"]), "other": _name(b["el"]),
                                     "direction": "below" if axis == 1 else "right",
                                     "gap_pdf_pt": pt(gap_pdf), "gap_slides_pt": pt(gap_sl)})
    if crops is not None:
        crops.mkdir(parents=True, exist_ok=True)
        named = {_name(it["el"]): it for it in info}
        for i, f in enumerate(findings):
            it = named.get(f["element"])
            if it is None:
                continue
            a0, b0, a1, b1 = it["win"]
            pair = np.vstack([it["r"][b0:b1, a0:a1], np.ones((3, a1 - a0), bool), it["s"][b0:b1, a0:a1]])
            Image.fromarray(~pair).save(crops / f"{slide['page'] + 1:03}-{i}-{f['kind']}.png")
    return {"page": slide["page"], "label": slide.get("label"), "findings": findings}


def _name(e: dict) -> str:
    if e["kind"] == "text" and e.get("paragraphs"):
        text = "".join(r.get("text", "") for r in e["paragraphs"][0]["runs"]).strip()
        return f"{e['id']} {text[:30]!r}"
    return f"{e['id']} [{e['kind']}]"


def measure(pdf: Path, out: Path, crops: bool = False) -> dict:
    deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
    doc = Document(pdf)
    slides = []
    for slide in deck["slides"]:
        n = slide["page"]
        thumb_path = out / "fidelity" / f"slides-{n + 1:03}.png"
        if not thumb_path.exists():
            raise SystemExit(f"{thumb_path} missing: run `python -m beamer2slides fidelity {pdf} --out {out}` first")
        thumb = rgb_array(Image.open(thumb_path))
        h, w = thumb.shape[:2]
        # The PDF page rendered as the background was, then scaled to the thumbnail's size.
        bg_width = Image.open(out / "backgrounds" / f"bg-{n + 1:03}.png").width
        ref = rgb_array(Image.fromarray(doc[n].render(bg_width / doc[n].width)), (w, h))
        slides.append(measure_slide(slide, ref, thumb, w / slide["size"][0], out / "text_fit" if crops else None))
    report = {"pdf": str(pdf), "slides": slides}
    (out / "text_fit.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def print_report(report: dict) -> int:
    bad = 0
    for s in report["slides"]:
        if not s["findings"]:
            continue
        bad += 1
        print(f"slide {s['page'] + 1}" + (f" ({s['label']})" if s.get("label") else ""))
        for f in s["findings"]:
            rest = {k: v for k, v in f.items() if k not in ("kind", "element")}
            print(f"  {f['kind']:<7} {f['element']}  {rest}")
    counts = {}
    for s in report["slides"]:
        for f in s["findings"]:
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    print(f"{bad} of {len(report['slides'])} slides with findings: {counts or 'none'}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="the convert/fidelity output folder")
    ap.add_argument("--crops", action="store_true",
                    help="save each finding's window to <out>/text_fit/: the PDF's ink above, Slides' below")
    args = ap.parse_args(argv)
    return print_report(measure(args.pdf, args.out, args.crops))


if __name__ == "__main__":
    raise SystemExit(main())
