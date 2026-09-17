"""Measure how closely the Google Slides rendering matches the original PDF.

Both renderings contain the same background picture, so subtracting the background
isolates the text in each: `text_ref` from the original PDF page and `text_slides`
from Google's thumbnail of the emitted slide. Per text element we compare the two
masks: offsets of the ink box, width ratio and number of text lines.
"""

import json
from pathlib import Path

import numpy as np
import pymupdf

from .google_auth import slides_service
from .gslides import save_thumbnail

DIFF_THRESHOLD = 70  # max channel difference that counts as text rather than resampling noise


def rgb_array(pix: pymupdf.Pixmap) -> np.ndarray:
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).astype(np.int16)


def text_mask(img: np.ndarray, background: np.ndarray) -> np.ndarray:
    """Pixels that differ from the background, minus 1-px resampling outlines.

    Google rescales the uploaded background, so edges of things left in the background
    (math, panel borders) differ by a pixel between the two renderings. Glyph strokes of
    converted text are several pixels thick; keep pixels with >= 4 set neighbours (3x3)."""
    raw = np.abs(img - background).max(axis=2) > DIFF_THRESHOLD
    padded = np.pad(raw.astype(np.uint8), 1)
    count = sum(padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx]
                for dy in (-1, 0, 1) for dx in (-1, 0, 1))
    return raw & (count >= 4)


MIN_ROW_PX, MIN_COL_PX = 4, 2  # rows/columns with fewer text pixels are specks, not text


def bands(mask: np.ndarray, min_gap: int) -> int:
    """Text lines = runs of text rows; gaps shorter than `min_gap` px are within a line."""
    rows = np.flatnonzero(mask.sum(axis=1) >= MIN_ROW_PX)
    if rows.size == 0:
        return 0
    return int((np.diff(rows) > min_gap).sum()) + 1


def ink_box(mask: np.ndarray):
    rows = np.flatnonzero(mask.sum(axis=1) >= MIN_ROW_PX)
    cols = np.flatnonzero(mask.sum(axis=0) >= MIN_COL_PX)
    if rows.size == 0:
        return None
    return cols[0], rows[0], cols[-1] + 1, rows[-1] + 1


def dilate(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:] |= mask[:-1]
    out[:-1] |= mask[1:]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def measure(pdf: Path, out: Path, refresh: bool = False) -> dict:
    deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
    state = json.loads((out / "emit.json").read_text(encoding="utf-8"))
    original = pymupdf.open(pdf)
    slides = None
    fdir = out / "fidelity"
    fdir.mkdir(exist_ok=True)

    report = {"presentationId": state["presentationId"], "slides": []}
    for slide, emitted in zip(deck["slides"], state["slides"]):
        n = slide["page"]
        thumb_path = fdir / f"slides-{n + 1:03}.png"
        # Saved thumbnails are reused unless the deck was emitted again since.
        if refresh or not thumb_path.exists() or thumb_path.stat().st_mtime < (out / "emit.json").stat().st_mtime:
            slides = slides or slides_service()
            save_thumbnail(slides, state["presentationId"], emitted["objectId"], thumb_path)
        thumb = rgb_array(pymupdf.Pixmap(str(thumb_path)))
        h, w = thumb.shape[:2]
        # Background: the uploaded PNG (with its patches). The reference goes through the same
        # render-at-upload-size-then-rescale path, so content left in the background cancels out.
        bg_png = pymupdf.Pixmap(str(out / "backgrounds" / f"bg-{n + 1:03}.png"))
        bg = rgb_array(pymupdf.Pixmap(bg_png, w, h))
        zoom = bg_png.width / original[n].rect.width
        ref_full = original[n].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        ref = rgb_array(pymupdf.Pixmap(ref_full, w, h))
        m_ref, m_sl = text_mask(ref, bg), text_mask(thumb, bg)

        px_per_pdf_pt = w / slide["size"][0]
        px_per_slide_pt = w / 720.0
        elements = []
        covered = np.zeros_like(m_ref)
        for el, oid in zip(slide["elements"], emitted["elements"]):
            is_text = el["kind"] == "text"
            size = max(p["size"] for p in el["paragraphs"]) if is_text else 4.0
            x0, y0, x1, y1 = el["bbox"]
            # Tight vertically (neighbouring boxes are often only a line apart), generous to
            # the right where a wider font would spill.
            a0, b0 = max(0, int((x0 - 0.8 * size) * px_per_pdf_pt)), max(0, int((y0 - 0.35 * size) * px_per_pdf_pt))
            a1, b1 = min(w, int((x1 + 3 * size) * px_per_pdf_pt)), min(h, int((y1 + 0.5 * size) * px_per_pdf_pt))
            covered[b0:b1, a0:a1] = True
            r, s = m_ref[b0:b1, a0:a1], m_sl[b0:b1, a0:a1]
            br, bs = ink_box(r), ink_box(s)
            if br is None or bs is None:
                elements.append({"id": oid, "missing": "reference" if br is None else "slides"})
                continue
            to_pt = 1 / px_per_slide_pt
            elements.append({
                "id": oid,
                "text": "".join(run["text"] for run in el["paragraphs"][0]["runs"])[:40] if is_text
                        else f"[{ {'image': 'picture', 'table': 'table', 'diagram': 'diagram'}.get(el['kind']) or el['shape'].lower()}]",
                "dx_pt": round((bs[0] - br[0]) * to_pt, 2),
                "dy_top_pt": round((bs[1] - br[1]) * to_pt, 2),
                "dy_bottom_pt": round((bs[3] - br[3]) * to_pt, 2),
                "width_ratio": round((bs[2] - bs[0]) / max(1, br[2] - br[0]), 3),
                "lines_ref": bands(r, round(0.12 * size * px_per_pdf_pt)),
                "lines_slides": bands(s, round(0.12 * size * px_per_pdf_pt)),
            })

        inter = (dilate(m_ref) & m_sl & covered).sum() + (m_ref & dilate(m_sl) & covered).sum()
        total = (m_ref & covered).sum() + (m_sl & covered).sum()
        overlap = float(inter / total) if total else 1.0

        diff = np.full((h, w, 3), 255, dtype=np.uint8)
        diff[m_ref & ~m_sl] = (220, 40, 40)     # only in the PDF: red
        diff[m_sl & ~m_ref] = (30, 110, 230)    # only in Slides: blue
        diff[m_ref & m_sl] = (0, 0, 0)          # both: black
        pymupdf.Pixmap(pymupdf.csRGB, w, h, diff.tobytes(), False).save(fdir / f"diff-{n + 1:03}.png")

        report["slides"].append({"page": n, "text_overlap": round(overlap, 3), "elements": elements})

    (out / "fidelity.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def print_report(report: dict) -> None:
    print(f"{'slide':>5} {'overlap':>8}  {'dx':>6} {'dy top':>7} {'dy bot':>7} {'width':>6} {'lines':>6}  text")
    for s in report["slides"]:
        print(f"{s['page'] + 1:>5} {s['text_overlap']:>8.1%}")
        for e in s["elements"]:
            if "missing" in e:
                print(f"{'':>15}missing in {e['missing']}: {e['id']}")
                continue
            lines = f"{e['lines_slides']}/{e['lines_ref']}"
            flag = "  <-- line count" if e["lines_slides"] != e["lines_ref"] and not e["text"].startswith("[") else ""
            print(f"{'':>15}{e['dx_pt']:>6.1f} {e['dy_top_pt']:>7.1f} {e['dy_bottom_pt']:>7.1f} "
                  f"{e['width_ratio']:>6.3f} {lines:>6}  {e['text']!r}{flag}")
