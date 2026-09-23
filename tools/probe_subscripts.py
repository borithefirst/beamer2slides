"""Probe: how to keep a subscript from reaching into the line below (tools/text_fit.py finding
`crowded`, torture frame `inline-math`; docs/calibration.md 2c "Scripts").

Slides sets a `baselineOffset` SUBSCRIPT run at 0.665 of its size and lowers it ~0.375 em of that
size; TeX lowers a subscript 0.15 em (0.25 with a superscript beside it). Measured here, on
Google's renderer:

1. `metrics`: a SUBSCRIPT "1" after a Lato 40 "H" at 1.0 / 0.85 / 0.75 / 0.65 / 0.55 of the size:
   how far below the H's baseline its ink ends and how tall it is (pt).
2. `unicode`: the Unicode sub- and superscript characters (U+2080..U+209C, U+1D62, U+2C7C, ...)
   in Lato, PT Serif and Arial, with no offset: ink top/bottom against the H's baseline, height,
   and whether Lato's glyph is pixel-identical to Arial's (a fallback font) - crops are saved.
3. `crowding`: the torture paragraph's two lines (Lato 21.2, lineSpacing of the PDF's pitch,
   subscripts at their converted sizes) with a soft break between them, as they are, with the
   subscripts' size scaled, with more lineSpacing, and with Unicode subscripts: the white gap
   between line 1's ink and line 2's, and the baseline pitch (pt).
4. `pptx`: a .pptx whose runs carry `baseline="-15000"` / -25000 / -40000 / 30000, imported by
   Drive: what `baselineOffset` the API reads back and where the glyph sits.

Everything it creates is deleted. Usage: python tools/probe_subscripts.py
(prints a JSON summary, also written with the thumbnails to out/probe_subscripts)
"""

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.gapi import media_upload
from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_subscripts"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SUB = "SUBSCRIPT"
SOFT = chr(11)

BIG = 40
FACTORS = (1.0, 0.85, 0.75, 0.65, 0.55)
UNICODE = "₀₁₂₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥᵦᵧᵨᵩᵪ¹²³⁺⁻ⁿⁱ"
FONTS = ("Lato", "PT Serif", "Arial")

# the torture paragraph at its converted sizes (emit.FontMapper on out/.../27_text_fit/deck.json)
Z, R0 = 21.2, 105.7        # body size and the lineSpacing giving the PDF's 13.55 pt pitch
LINE2 = "H fonts, and the line"


def formula(k: float, unicode: bool = False) -> list[tuple]:
    """(text, size, italic, offset) segments of line 1."""
    if unicode:
        return [("H and ", Z, False, None), ("α", 22.1, True, None), ("ₜ₊₁", Z, False, None),
                (" = ", Z, False, None), ("α", 22.1, True, None), ("ₜ", Z, False, None),
                (" − ", Z, False, None), ("η", 22.1, True, None), ("∇", Z, False, None),
                ("L", 22.1, True, None), ("(", Z, False, None), ("θ", 22.1, True, None),
                ("ₜ", Z, False, None), (")", Z, False, None)]
    return [("H and ", Z, False, None), ("α", 22.1, True, None), ("t", 23.4 * k, True, SUB),
            ("+1", 22.5 * k, False, SUB), (" = ", Z, False, None), ("α", 22.1, True, None),
            ("t", 23.4 * k, True, SUB), (" − ", Z, False, None), ("η", 22.1, True, None),
            ("∇", Z, False, None), ("L", 22.1, True, None), ("(", Z, False, None),
            ("θ", 22.1, True, None), ("t", 23.4 * k, True, SUB), (")", Z, False, None)]


CROWDING = [  # (label, segments of line 1, lineSpacing)
    *[(f"size x{k}", formula(k), R0) for k in FACTORS],
    *[(f"lineSpacing {R0 + d:.1f}", formula(1.0), R0 + d) for d in (5, 10, 15)],
    ("size x0.8 + lineSpacing +5", formula(0.8), R0 + 5),
    ("unicode", formula(1.0, unicode=True), R0),
]


def style(oid: str, font: str, size: float, start: int, end: int, italic=False, offset=None) -> dict:
    st = {"fontFamily": font, "fontSize": pt(round(size, 2)), "italic": italic,
          "foregroundColor": {"opaqueColor": {"rgbColor": {}}}, "baselineOffset": offset or "NONE"}
    return {"updateTextStyle": {"objectId": oid, "textRange": {"type": "FIXED_RANGE", "startIndex": start,
                                                               "endIndex": end},
                                "fields": ",".join(st), "style": st}}


def columns(mask: np.ndarray) -> list[tuple[int, int]]:
    cols = np.flatnonzero(mask.any(axis=0))
    out: list[list[int]] = []
    for c in cols:
        if out and c - out[-1][1] <= 1:
            out[-1][1] = c + 1
        else:
            out.append([c, c + 1])
    return [tuple(c) for c in out]


def rows(mask: np.ndarray, join: int = 1) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for r in np.flatnonzero(mask.any(axis=1)):
        if out and r - out[-1][1] < join:
            out[-1][1] = r + 1
        else:
            out.append([r, r + 1])
    return [tuple(r) for r in out]


def after_h(mask: np.ndarray, k: float) -> dict:
    """Ink of whatever follows the leading H, against the H's baseline (pt)."""
    cs = columns(mask)
    h0, h1 = cs[0]
    hy = np.flatnonzero(mask[:, h0:h1].any(axis=1))
    base, cap = (hy[-1] + 1) / k, (hy[-1] + 1 - hy[0]) / k
    rest = mask[:, h1 + 1:]
    ys = np.flatnonzero(rest.any(axis=1))
    xs = np.flatnonzero(rest.any(axis=0))
    if not len(ys):
        return {"missing": True}
    return {"below_baseline": round((ys[-1] + 1) / k - base, 2), "top_above_baseline": round(base - ys[0] / k, 2),
            "height": round((ys[-1] + 1 - ys[0]) / k, 2), "width": round((xs[-1] + 1 - xs[0]) / k, 2),
            "H_cap": round(cap, 2)}


def main() -> None:
    slides, drive = slides_service(), drive_service()
    pres = execute(slides.presentations().create(body={"title": "b2s probe subscripts"}))
    made = [pres["presentationId"]]
    try:
        result = run(slides, pres)
        result["pptx"] = run_pptx(slides, drive, made)
    finally:
        for fid in made:
            execute(drive.files().delete(fileId=fid))
    print(json.dumps(result, indent=1, ensure_ascii=False))
    (OUT / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")


def run(slides, pres: dict) -> dict:
    pid = pres["presentationId"]
    first = pres["slides"][0]["objectId"]
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for e in pres["slides"][0].get("pageElements", [])]
    pages = [first, "page_uni", "page_crowd"]
    reqs += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    boxes = {}  # label -> (page, x, y, w, h)

    def box(label, page, x, y, w, h, segments, spacing=100.0):
        oid = f"box_{len(boxes):03}"
        text = "".join(s[0] for s in segments)
        nonlocal reqs
        reqs += [text_box(oid, page, x, y, w, h), {"insertText": {"objectId": oid, "text": text}},
                 {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                           "style": {"lineSpacing": spacing, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                           "fields": "lineSpacing,spaceAbove,spaceBelow"}}]
        start = 0
        for seg in segments:
            t, font, size, italic, offset = seg if len(seg) == 5 else (seg[0], "Lato", *seg[1:])
            reqs.append(style(oid, font, size, start, start + len(t), italic, offset))
            start += len(t)
        boxes[label] = (page, x, y, w, h)

    # 1. metrics: H + subscript 1 at a fraction of the size
    for i, k in enumerate(FACTORS):
        box(f"metrics x{k}", first, 10 + i * 140, 20, 130, 90, [("H", "Lato", BIG, False, None), ("1", "Lato", BIG * k, False, SUB)])
    for i, k in enumerate(FACTORS):
        box(f"metrics sup x{k}", first, 10 + i * 140, 140, 130, 90, [("H", "Lato", BIG, False, None), ("1", "Lato", BIG * k, False, "SUPERSCRIPT")])
    box("metrics plain 1", first, 10, 260, 130, 90, [("H", "Lato", BIG, False, None), ("1", "Lato", BIG, False, None)])
    box("metrics plain x", first, 150, 260, 130, 90, [("H", "Lato", BIG, False, None), ("x", "Lato", BIG, False, None)])
    # 2. unicode characters, H + c, one box each, per font
    per_row, uw, uh = 12, 58, 32
    rows_per_font = -(-len(UNICODE) // per_row)
    for f, font in enumerate(FONTS):
        for j, ch in enumerate(UNICODE):
            x = 4 + (j % per_row) * uw
            y = 4 + (f * rows_per_font + j // per_row) * uh
            box(f"uni {font} U+{ord(ch):04X}", pages[1], x, y, uw - 2, uh, [("H", font, 20, False, None), (ch, font, 20, False, None)])
    # 3. crowding: two lines, soft break
    for i, (label, segs, spacing) in enumerate(CROWDING):
        x, y = 4 + (i % 2) * 358, 4 + (i // 2) * 78
        box(label, pages[2], x, y, 354, 76, segs[:-1] + [(segs[-1][0] + SOFT + LINE2, *segs[-1][1:])], spacing)
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))

    images = {}
    for page in pages:
        path = OUT / f"{page}.png"
        save_thumbnail(slides, pid, page, path)
        images[page] = np.asarray(Image.open(path).convert("L"))
    k = images[first].shape[1] / 720
    result = {"metrics": {}, "unicode": {}, "crowding": {}}
    crops = {}
    for label, (page, x, y, w, h) in boxes.items():
        crop = images[page][int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)]
        mask = crop < 128
        if label.startswith("metrics"):
            result["metrics"][label] = after_h(mask, k)
        elif label.startswith("uni "):
            result["unicode"][label] = after_h(mask, k)
            crops[label] = mask
        else:
            c0, c1 = columns(mask)[0]  # both lines start with an H
            h_rows = rows(mask[:, c0:c1])
            b1, b2 = h_rows[0][1], h_rows[-1][1]
            prof = mask.any(axis=1)
            # line 1's ink runs down from its baseline without a white row; then the white gap
            r = b1
            while r < b2 and prof[r]:
                r += 1
            white = r
            while white < b2 and not prof[white]:
                white += 1
            result["crowding"][label] = {"pitch": round((b2 - b1) / k, 2), "line1_ink_below_baseline": round((r - b1) / k, 2),
                                         "white_gap": round((white - r) / k, 2)}
    # fallback: Lato's glyph identical to Arial's?
    for label, mask in crops.items():
        if label.startswith("uni Lato "):
            other = crops[label.replace("Lato", "Arial")]
            a, b = mask[:, columns(mask)[0][1] + 1:], other[:, columns(other)[0][1] + 1:]
            ya, yb = np.flatnonzero(a.any(axis=1)), np.flatnonzero(b.any(axis=1))
            xa, xb = np.flatnonzero(a.any(axis=0)), np.flatnonzero(b.any(axis=0))
            same = False
            if len(ya) and len(yb):
                ga = a[ya[0]:ya[-1] + 1, xa[0]:xa[-1] + 1]
                gb = b[yb[0]:yb[-1] + 1, xb[0]:xb[-1] + 1]
                same = ga.shape == gb.shape and bool((ga == gb).mean() > 0.97)
            result["unicode"][label]["same_as_arial"] = same
    return result


def run_pptx(slides, drive, made: list) -> dict:
    """Runs with an arbitrary baseline offset, imported from a .pptx."""
    from lxml import etree
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    offsets = (-15000, -25000, -40000, 30000, 0)
    for i, off in enumerate(offsets):
        tb = slide.shapes.add_textbox(Pt(20 + i * 130), Pt(40), Pt(120), Pt(90))
        tf = tb.text_frame
        tf.word_wrap = False
        para = tf.paragraphs[0]
        for text in ("H", "1"):
            run = para.add_run()
            run.text = text
            run.font.size = Pt(BIG)
            run.font.name = "Lato"
            if text == "1" and off:
                run._r.get_or_add_rPr().set("baseline", str(off))
        tb.name = f"off{off}"
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    fid = execute(drive.files().create(body={"name": "b2s probe subscripts pptx",
                                             "mimeType": "application/vnd.google-apps.presentation"},
                                       media_body=media_upload(buf, PPTX_MIME), fields="id"))["id"]
    made.append(fid)
    pres = execute(slides.presentations().get(presentationId=fid))
    page = pres["slides"][0]
    out = {}
    path = OUT / "pptx.png"
    save_thumbnail(slides, fid, page["objectId"], path)
    img = np.asarray(Image.open(path).convert("L"))
    k = img.shape[1] / (pres["pageSize"]["width"]["magnitude"] / 12700)
    for pe in page.get("pageElements", []):
        runs = [te["textRun"] for te in pe.get("shape", {}).get("text", {}).get("textElements", []) if "textRun" in te]
        tr = pe["transform"]
        x, y = tr.get("translateX", 0) / 12700, tr.get("translateY", 0) / 12700
        w = pe["size"]["width"]["magnitude"] * tr.get("scaleX", 1) / 12700
        h = pe["size"]["height"]["magnitude"] * tr.get("scaleY", 1) / 12700
        crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)] < 128
        label = offsets[round((x - 20) / 130)] if 0 <= round((x - 20) / 130) < len(offsets) else None
        out[f"baseline {label}"] = {
            "runs": [{"text": r.get("content"), "baselineOffset": r.get("style", {}).get("baselineOffset"),
                      "fontSize": r.get("style", {}).get("fontSize")} for r in runs],
            "ink": after_h(crop, k) if crop.any() else None}
    return out


if __name__ == "__main__":
    main()
