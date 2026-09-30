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
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from beamer2slides.json_types import Json, JsonObject, JsonShapeError, as_object, as_objects, as_str  # noqa: E402
from beamer2slides.pdf import Document, Page  # noqa: E402
from tools.pdf_lines import text_lines  # noqa: E402

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def num(v: Json) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonShapeError(f"a number was expected, found {v!r}")
    return v


def rgb(image: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(image, Image.Image):
        image = np.asarray(image.convert("RGB"))
    return image.astype(np.int16)


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
    Image.fromarray(arr.astype(np.uint8)).save(path)


EMU = 12700


def template_lines(slide: JsonObject) -> list[tuple[str, float, float, float]]:
    """(first chars, x of the text origin, first baseline, size) of each text box on a slide,
    using Slides' text box model: 7.2 pt inset, first baseline 6.48 pt + 0.968 em below the top."""
    out: list[tuple[str, float, float, float]] = []

    def walk(els: list[JsonObject], parent: Matrix) -> None:
        for el in els:
            t = as_object(el.get("transform", {}), "transform")
            k = EMU if t.get("unit", "EMU") == "EMU" else 1
            a, b = num(t.get("scaleX", 0)), num(t.get("shearY", 0))
            c, d = num(t.get("shearX", 0)), num(t.get("scaleY", 0))
            e, f = num(t.get("translateX", 0)) / k, num(t.get("translateY", 0)) / k
            pa, pb, pc, pd, pe, pf = parent
            m = (pa * a + pc * b, pb * a + pd * b, pa * c + pc * d, pb * c + pd * d,
                 pa * e + pc * f + pe, pb * e + pd * f + pf)
            if "elementGroup" in el:
                walk(as_objects(as_object(el["elementGroup"], "elementGroup")["children"], "children"), m)
                continue
            shape = as_object(el.get("shape", {}), "shape")
            elements = as_objects(as_object(shape.get("text", {}), "text").get("textElements", []), "textElements")
            runs = [as_object(te["textRun"], "textRun") for te in elements if "textRun" in te]
            contents = [as_str(r["content"], "textRun.content") for r in runs]
            if not "".join(contents).strip():
                continue
            first = next(r for r, content in zip(runs, contents) if content.strip())
            style = as_object(first["style"], "textRun.style")
            size = num(as_object(style.get("fontSize", {}), "fontSize").get("magnitude", 14))
            text = "".join(contents).strip().split("\n")[0].replace("\x0b", " ")
            valign = as_object(shape.get("shapeProperties", {}), "shapeProperties").get("contentAlignment", "TOP")
            y = m[5] + 6.48 + 0.968 * size
            if valign != "TOP":
                continue  # only top-anchored boxes have a simple first baseline
            out.append((text[:24], m[4] + 7.2, y, size))

    walk(as_objects(slide.get("pageElements", []), "pageElements"), IDENTITY)
    return out


def pdf_lines(page: Page, scale: float) -> list[tuple[str, float, float, float]]:
    """(text, x, baseline, size) of every PDF line, in template pt."""
    return [(l.text, l.origin[0] * scale, l.origin[1] * scale, l.size * scale) for l in text_lines(page)]


def main() -> int:
    pdf, tex, template = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    pairs = [int(n) for n in re.findall(r"%\s*template slide (\d+)", tex.read_text(encoding="utf-8"))]
    doc = Document(pdf)
    presentation = as_object(json.loads((ROOT / "out" / "templates" / template / "presentation.json")
                                        .read_text(encoding="utf-8")), "presentation.json")
    slides = as_objects(presentation["slides"], "presentation.json's slides")
    out = pdf.parent / "compare"
    out.mkdir(exist_ok=True)
    print(f"{'page':>4} {'slide':>5} {'overlap':>8} {'dx0':>6} {'dy0':>6} {'dx1':>6} {'dy1':>6}  (template pt; + = PDF right/lower)")
    for i, (page, n) in enumerate(zip(doc, pairs), 1):
        t = rgb(Image.open(ROOT / "out" / "templates" / template / "slides" / f"{n:03d}.png"))
        h, w = t.shape[:2]
        zoom = w / page.width
        p = rgb(page.render(zoom, None, False))[:h, :w]
        mt, mp = ink(t), ink(p)
        inter = (dilate(mt) & mp).sum() + (mt & dilate(mp)).sum()
        total = mt.sum() + mp.sum()
        overlap = inter / total if total else 1.0
        k = w / 720

        def box(m: np.ndarray) -> tuple[int, int, int, int]:
            ys, xs = np.nonzero(m)
            return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) else (0, 0, 0, 0)

        bt, bp = box(mt), box(mp)
        d = [(b - a) / k for a, b in zip(bt, bp)]
        print(f"{i:>4} {n:>5} {overlap:>8.3f} {d[0]:>6.1f} {d[1]:>6.1f} {d[2]:>6.1f} {d[3]:>6.1f}")
        mine = pdf_lines(page, 720 / page.width)
        for text, x, y, size in template_lines(slides[n - 1]):
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
