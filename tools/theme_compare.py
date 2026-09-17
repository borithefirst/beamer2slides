"""Compare a beamer replica of a Google Slides template with the template itself.

Each PDF page is paired with a template slide through "% template slide N" comments in
the .tex source (in page order). For every pair it writes, into out/themes/<stem>/compare/:
  pair-NNN.png  template thumbnail | PDF page, side by side
  diff-NNN.png  red = ink only in the template, blue = only in the PDF, black = both
and prints the ink overlap (1.0 = identical, 1 px tolerance) and the vertical/horizontal
offset of the ink bounding boxes.

Usage: python tools/theme_compare.py <replica.pdf> <replica.tex> <template name>
(template name = folder under out/templates/, made by tools/theme_capture.py)
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def rgb(pix: pymupdf.Pixmap) -> np.ndarray:
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).astype(np.int16)


def ink(img: np.ndarray) -> np.ndarray:
    """Pixels that differ clearly from the page's dominant colour."""
    flat = img.reshape(-1, 3)
    colours, counts = np.unique(flat[:: 7], axis=0, return_counts=True)
    bg = colours[counts.argmax()]
    return np.abs(img - bg).max(axis=2) > 60


def dilate(m: np.ndarray) -> np.ndarray:
    out = m.copy()
    out[1:] |= m[:-1]
    out[:-1] |= m[1:]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def save(arr: np.ndarray, path: Path) -> None:
    h, w = arr.shape[:2]
    pymupdf.Pixmap(pymupdf.csRGB, w, h, arr.astype(np.uint8).tobytes(), False).save(path)


EMU = 12700


def template_lines(slide: dict) -> list[tuple[str, float, float, float]]:
    """(first chars, x of the text origin, first baseline, size) of each text box on a slide,
    using Slides' text box model: 7.2 pt inset, first baseline 6.48 pt + 0.968 em below the top."""
    out = []

    def walk(els, parent=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)):
        for el in els:
            t = el.get("transform", {})
            k = EMU if t.get("unit", "EMU") == "EMU" else 1
            a, b, c, d = t.get("scaleX", 0), t.get("shearY", 0), t.get("shearX", 0), t.get("scaleY", 0)
            e, f = t.get("translateX", 0) / k, t.get("translateY", 0) / k
            pa, pb, pc, pd, pe, pf = parent
            m = (pa * a + pc * b, pb * a + pd * b, pa * c + pc * d, pb * c + pd * d,
                 pa * e + pc * f + pe, pb * e + pd * f + pf)
            if "elementGroup" in el:
                walk(el["elementGroup"]["children"], m)
                continue
            runs = [te["textRun"] for te in el.get("shape", {}).get("text", {}).get("textElements", [])
                    if "textRun" in te]
            if not "".join(r["content"] for r in runs).strip():
                continue
            size = next(r for r in runs if r["content"].strip())["style"].get("fontSize", {}).get("magnitude", 14)
            text = "".join(r["content"] for r in runs).strip().split("\n")[0].replace("\x0b", " ")
            h = el["size"]["height"]["magnitude"] / EMU * m[3]
            valign = el["shape"].get("shapeProperties", {}).get("contentAlignment", "TOP")
            y = m[5] + 6.48 + 0.968 * size
            if valign != "TOP":
                continue  # only top-anchored boxes have a simple first baseline
            out.append((text[:24], m[4] + 7.2, y, size))

    walk(slide.get("pageElements", []))
    return out


def pdf_lines(page: pymupdf.Page, scale: float) -> list[tuple[str, float, float, float]]:
    """(text, x, baseline, size) of every PDF line, in template pt."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                text = "".join(s["text"] for s in line["spans"]).strip()
                lines.append((text, spans[0]["origin"][0] * scale, spans[0]["origin"][1] * scale,
                              spans[0]["size"] * scale))
    return lines


def main() -> int:
    pdf, tex, template = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    pairs = [int(n) for n in re.findall(r"%\s*template slide (\d+)", tex.read_text(encoding="utf-8"))]
    doc = pymupdf.open(pdf)
    presentation = json.loads((ROOT / "out" / "templates" / template / "presentation.json").read_text(encoding="utf-8"))
    out = pdf.parent / "compare"
    out.mkdir(exist_ok=True)
    print(f"{'page':>4} {'slide':>5} {'overlap':>8} {'dx0':>6} {'dy0':>6} {'dx1':>6} {'dy1':>6}  (template pt; + = PDF right/lower)")
    for i, (page, n) in enumerate(zip(doc, pairs), 1):
        thumb = pymupdf.Pixmap(str(ROOT / "out" / "templates" / template / "slides" / f"{n:03d}.png"))
        t = rgb(thumb)
        h, w = t.shape[:2]
        zoom = w / page.rect.width
        p = rgb(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False))[:h, :w]
        mt, mp = ink(t), ink(p)
        inter = (dilate(mt) & mp).sum() + (mt & dilate(mp)).sum()
        total = mt.sum() + mp.sum()
        overlap = inter / total if total else 1.0
        k = w / 720

        def box(m):
            ys, xs = np.nonzero(m)
            return (xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else (0, 0, 0, 0)

        bt, bp = box(mt), box(mp)
        d = [(b - a) / k for a, b in zip(bt, bp)]
        print(f"{i:>4} {n:>5} {overlap:>8.3f} {d[0]:>6.1f} {d[1]:>6.1f} {d[2]:>6.1f} {d[3]:>6.1f}")
        mine = pdf_lines(page, 720 / page.rect.width)
        for text, x, y, size in template_lines(presentation["slides"][n - 1]):
            key = re.sub(r"\W", "", text)[:9]
            match = next((m for m in mine if re.sub(r"\W", "", m[0]).startswith(key)), None)
            if match:
                print(f"            {text!r:28} dx {match[1] - x:6.1f}  dy {match[2] - y:6.1f}  size {match[3]:.1f}/{size}")
            else:
                print(f"            {text!r:28} not found in PDF")
        diff = np.full((h, w, 3), 255, dtype=np.int16)
        diff[mt & ~mp] = (220, 40, 40)
        diff[mp & ~mt] = (30, 110, 230)
        diff[mt & mp] = (0, 0, 0)
        save(diff, out / f"diff-{i:03d}.png")
        save(np.concatenate([t, np.full((h, 8, 3), 255, dtype=np.int16), p], axis=1), out / f"pair-{i:03d}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
