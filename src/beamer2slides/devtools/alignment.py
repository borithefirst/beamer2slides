"""Alignment of native text and the graphics that go with it, measured on Google's thumbnails.

Reads a converted output folder (deck.json, emit.json, fidelity/slides-NNN.png from
`fidelity`) and the PDF it came from, rendered locally on the thumbnails' pixel grid. Where
Slides really put each picture comes from `presentations.get` (absolute transforms, cached in
slides_elements.json). Everything is in PDF points; Slides positions are divided by the scale.

- holes: anchored formula and icon pictures (role math/icon). Ink gaps between the picture's
  ink and the text ink left and right of it, on its text line, in Slides minus in the PDF.
  Picture ink comes from the picture file itself (pixels unlike the page colour around its box),
  mapped into its box on either image; text ink is every other pixel of the line band unlike
  that page colour.
  `overlap`: text ink touches the picture ink or ends at the edge of the opaque box (covered),
  where the PDF has a gap.
- numbers: literal numbers on ball pictures. Centroid of the number-coloured ink minus the
  ball box centre, in Slides minus in the PDF.
- bullets: shape, glyph and image bullets. Ink box and colour of the biggest blob in the
  bullet's cell (up to halfway to its text): CIE76 colour difference and width and height
  ratios, Slides over PDF (heights only for SUBSTITUTED glyphs). Flat glyphs take their fully
  covered pixels, balls the median of their ink like render.ink_colour; a translucent highlight
  over the bullet in the PDF is unblended first (Slides puts it under the text).
- overlays: overlay pictures' marks (word edges they meet). The word edge in the ink minus the
  point where the picture meets it (stretched with the picture), in Slides minus in the PDF.
  Picture ink is the picture's alpha, so arrows and ellipses touching words don't count as text.

  python tools/alignment.py out/<deck> [--refresh] [--crops failures|all|none]
      alignment.json in the folder, a summary table, alignment/NNN-item.png evidence crops
      (PDF above, Slides below, measured boxes and edges in red); --refresh re-reads the
      Slides elements
"""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.emit import FontMapper, bullet_shape, fit_holes, merge_blocks, slide_holes
from beamer2slides.gslides import EMU_PER_PT
from beamer2slides.pdf import Document

THRESHOLDS = {"hole_gap": 1.5, "number_offset": 1.0, "bullet_delta_e": 15.0, "bullet_size": 0.30, "overlay_drift": 2.0}
EDGE = 0.5          # ink strength at which a column starts or ends ink
WORD_GAP_EM = 0.12  # gaps narrower than this are inside a word
REACH_EM = 2.0      # how far beside a picture its neighbouring text is looked for
SUBSTITUTED = {"triangle"}  # bullets Slides draws as another glyph (➢ for ▶): only heights compare


# ---------------------------------------------------------------- Slides geometry

def _affine(t: dict) -> np.ndarray:
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return np.array([[t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit],
                     [t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit], [0, 0, 1]])


def element_boxes(page_elements: list[dict], parent: np.ndarray | None = None) -> dict[str, list[float]]:
    """Absolute boxes (slide pt) of page elements, group children included."""
    parent = np.eye(3) if parent is None else parent
    out = {}
    for e in page_elements:
        m = parent @ _affine(e.get("transform", {"scaleX": 1, "scaleY": 1}))
        if "elementGroup" in e:
            out.update(element_boxes(e["elementGroup"].get("children", []), m))
        if "size" not in e:
            continue
        w, h = (e["size"][k]["magnitude"] / (EMU_PER_PT if e["size"][k].get("unit", "EMU") == "EMU" else 1)
                for k in ("width", "height"))
        pts = m @ np.array([[0, w, 0, w], [0, 0, h, h], [1, 1, 1, 1]])
        out[e["objectId"]] = [float(pts[0].min()), float(pts[1].min()), float(pts[0].max()), float(pts[1].max())]
    return out


def slides_elements(out: Path, state: dict, refresh: bool = False) -> dict[str, list[float]]:
    """objectId -> absolute box (slide pt), from presentations.get; cached until emit.json changes."""
    cache = out / "slides_elements.json"
    if not refresh and cache.exists() and cache.stat().st_mtime >= (out / "emit.json").stat().st_mtime:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("presentationId") == state["presentationId"]:
            return data["boxes"]
    from beamer2slides.google_auth import slides_service
    from beamer2slides.gslides import execute
    pres = execute(slides_service().presentations().get(
        presentationId=state["presentationId"], fields="slides(objectId,pageElements)"))
    boxes = {}
    for s in pres.get("slides", []):
        boxes.update(element_boxes(s.get("pageElements", [])))
    cache.write_text(json.dumps({"presentationId": state["presentationId"], "boxes": boxes}), encoding="utf-8")
    return boxes


# ---------------------------------------------------------------- ink

class Image2:
    """An RGB image on a grid of `k` px per PDF point."""

    def __init__(self, rgb: np.ndarray, k: float):
        self.rgb, self.k = rgb.astype(np.float32), k

    def crop(self, x0, y0, x1, y1) -> tuple[np.ndarray, int, int]:
        h, w = self.rgb.shape[:2]
        a0, b0 = max(0, int(np.floor(x0 * self.k))), max(0, int(np.floor(y0 * self.k)))
        a1, b1 = min(w, int(np.ceil(x1 * self.k))), min(h, int(np.ceil(y1 * self.k)))
        return self.rgb[b0:max(b0, b1), a0:max(a0, a1)], a0, b0


def strength(rgb: np.ndarray, ref: np.ndarray | None = None) -> np.ndarray:
    """0..1 ink strength: difference from the reference colour (the crop's median), relative
    to the strongest ink in the crop."""
    if rgb.size == 0:
        return np.zeros(rgb.shape[:2])
    ref = np.median(rgb.reshape(-1, 3), axis=0) if ref is None else ref
    d = np.abs(rgb - ref).max(axis=2)
    return np.clip(d / max(float(np.percentile(d, 99.5)), 80.0), 0, 1)


def ring_colour(img: Image2, box: list[float], width_px: int = 3) -> np.ndarray:
    """Median colour just outside a box: the page under a picture."""
    rgb, a0, b0 = img.crop(box[0] - 2 * width_px / img.k, box[1] - 2 * width_px / img.k,
                           box[2] + 2 * width_px / img.k, box[3] + 2 * width_px / img.k)
    ring = np.concatenate([rgb[:width_px].reshape(-1, 3), rgb[-width_px:].reshape(-1, 3),
                           rgb[:, :width_px].reshape(-1, 3), rgb[:, -width_px:].reshape(-1, 3)])
    return np.median(ring, axis=0)


def picture_ink(png: Path, page: np.ndarray) -> np.ndarray:
    """Ink strength of a picture file: alpha for transparent pictures, else difference from the
    page colour under it (`page`; its own border may be a frame)."""
    im = Image.open(png)
    if im.mode in ("RGBA", "LA", "P"):
        rgba = np.asarray(im.convert("RGBA")).astype(np.float32)
        if (rgba[..., 3] < 250).mean() > 0.01:
            return rgba[..., 3] / 255
    return strength(np.asarray(im.convert("RGB")).astype(np.float32), page)


def place(ink: np.ndarray, box: list[float], img: Image2, region: tuple[int, int, int, int]) -> np.ndarray:
    """A picture's ink resampled into its box (PDF pt) on `img`'s grid, cut to `region` (px)."""
    a0, b0, a1, b1 = region
    out = np.zeros((b1 - b0, a1 - a0), np.float32)
    x0, y0, x1, y1 = (v * img.k for v in box)
    w, h = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    small = np.asarray(Image.fromarray((ink * 255).astype(np.uint8)).resize((w, h), Image.Resampling.BOX)) / 255
    ox, oy = round(x0), round(y0)
    sx0, sy0 = max(a0, ox), max(b0, oy)
    sx1, sy1 = min(a1, ox + w), min(b1, oy + h)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - b0:sy1 - b0, sx0 - a0:sx1 - a0] = small[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox]
    return out


def runs(profile: np.ndarray, gap_px: float = 0.0) -> list[tuple[float, float]]:
    """Sub-pixel (start, end) of the runs where a column profile reaches EDGE, joined across
    gaps narrower than `gap_px`."""
    on = profile >= EDGE
    idx = np.flatnonzero(on)
    if idx.size == 0:
        return []
    out = []
    for g in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
        a, b = int(g[0]), int(g[-1])
        start = a + (0.5 - (profile[a] - EDGE) / (profile[a] - profile[a - 1]) if a > 0 else 0.0)
        end = b + (0.5 + (profile[b] - EDGE) / (profile[b] - profile[b + 1]) if b + 1 < profile.size else 1.0)
        out.append((float(start), float(end)))
    joined = [out[0]]
    for s, e in out[1:]:
        if s - joined[-1][1] < gap_px:
            joined[-1] = (joined[-1][0], e)
        else:
            joined.append((s, e))
    return joined


# ---------------------------------------------------------------- evidence

def draw(region: list[float], dy: float, pdf: list[list[float]], slides: list[list[float]]) -> dict:
    """What an evidence crop shows: a region (PDF pt; `dy` lower in Slides) and the boxes and
    edges measured on either image."""
    r = lambda b: [round(v, 2) for v in b]
    return {"region": r(region), "dy": round(dy, 2), "pdf": [r(b) for b in pdf], "slides": [r(b) for b in slides]}


def save_evidence(path: Path, spec: dict, ref: Image2, got: Image2) -> None:
    """PDF crop above the Slides crop, magnified, measured boxes and edges in red."""
    from PIL import ImageDraw
    x0, y0, x1, y1 = spec["region"]
    k = ref.k
    zoom = max(2, min(8, round(900 / max(1.0, (x1 - x0) * k))))
    tiles = []
    for img, boxes, dy in ((ref, spec["pdf"], 0.0), (got, spec["slides"], spec["dy"])):
        rgb, a0, b0 = img.crop(x0, y0 + dy, x1, y1 + dy)
        tile = Image.fromarray(rgb.astype(np.uint8)).resize((rgb.shape[1] * zoom, rgb.shape[0] * zoom), Image.Resampling.NEAREST)
        pen = ImageDraw.Draw(tile)
        for bx0, by0, bx1, by1 in boxes:
            pen.rectangle([(bx0 * k - a0) * zoom, (by0 * k - b0) * zoom, (bx1 * k - a0) * zoom, (by1 * k - b0) * zoom],
                          outline=(230, 0, 0), width=1)
        tiles.append(tile)
    w = max(t.width for t in tiles)
    sheet = Image.new("RGB", (w, sum(t.height for t in tiles) + 6), (230, 0, 0))
    sheet.paste(tiles[0], (0, 0))
    sheet.paste(tiles[1], (0, tiles[0].height + 6))
    path.parent.mkdir(exist_ok=True)
    sheet.save(path)


# ---------------------------------------------------------------- measurements

def line_of(pic_box: list[float], paragraphs: list[dict]) -> tuple[dict, dict] | None:
    """(paragraph, line) of a text element that a picture sits on: the line whose x-height band
    is nearest the picture's middle, among lines reaching the picture horizontally."""
    x0, y0, x1, y1 = pic_box
    cands = [(p, l) for p in paragraphs for l in p["lines"] if l["x0"] - 3 * p["size"] <= x1 and x0 <= l["x1"] + 3 * p["size"]]
    if not cands:
        return None
    return min(cands, key=lambda c: (not y0 - 0.5 <= c[1]["baseline"] - 0.25 * c[0]["size"] <= y1 + 0.5,
                                     abs((y0 + y1) / 2 - c[1]["baseline"] + 0.35 * c[0]["size"])))


def band_profile(img: Image2, x0, x1, top, bottom, masks: list[tuple[np.ndarray, list[float]]]):
    """Column profile of text ink in a band (PDF pt), picture ink removed; the pictures' own
    column profiles; px origin; and the columns where picture ink hides the text entirely."""
    rgb, a0, b0 = img.crop(x0, top, x1, bottom)
    if rgb.size == 0:
        return None
    # (against the page around the picture: a filled picture can cover most of the band)
    s = strength(rgb, ring_colour(img, masks[0][1]) if masks else None)
    region = (a0, b0, a0 + rgb.shape[1], b0 + rgb.shape[0])
    pics, hidden = [], np.zeros(rgb.shape[1], bool)
    for ink, box in masks:
        m = place(ink, box, img, region)
        grown = np.clip(2 * np.maximum.reduce([m, np.roll(m, 1, 1), np.roll(m, -1, 1), np.roll(m, 1, 0), np.roll(m, -1, 0)]), 0, 1)
        s = s * (1 - grown)
        pics.append(m.max(axis=0))
        hidden |= (grown >= EDGE).mean(axis=0) >= 0.8  # (hidden top to bottom: nothing to see there)
    return s.max(axis=0), pics, a0, hidden


def hole_gaps(img: Image2, pic_ink: np.ndarray, box: list[float], top: float, bottom: float, size: float) -> dict | None:
    """Ink gaps (pt) left and right of a picture on its line band, and whether text ink ends at
    the edge of the picture's box (covered by it)."""
    reach = REACH_EM * size
    got = band_profile(img, box[0] - reach, box[2] + reach, top, bottom, [(pic_ink, box)])
    if got is None:
        return None
    text, (pic,), a0, _ = got
    pr = runs(pic, 0.3 * size * img.k)
    if not pr:
        return None
    ps, pe = pr[0][0], pr[-1][1]
    words = runs(text, 0.0)
    left = [e for s, e in words if s < ps]
    right = [s for s, e in words if e > pe]
    k = img.k
    bx0, bx1 = box[0] * k - a0, box[2] * k - a0
    edges = [(a0 + v) / k for v in (max(left) if left else None, ps, pe, min(right) if right else None) if v is not None]
    return {"left": round((ps - max(left)) / k, 2) if left else None,
            "right": round((min(right) - pe) / k, 2) if right else None,
            "covered_left": bool(left) and abs(max(left) - bx0) < 0.6 and ps - bx0 > 1.0,
            "covered_right": bool(right) and abs(min(right) - bx1) < 0.6 and bx1 - pe > 1.0,
            "marks": [[x, top, x, bottom] for x in edges]}


def measure_holes(slide, pictures, boxes, ref: Image2, got: Image2, out: Path, scale: float, fonts) -> list[dict]:
    texts = {e["id"]: e for e in slide["elements"] if e["kind"] == "text"}
    hole_lines = {}
    for el, p, run, pic in slide_holes(fit_holes(slide, scale, fonts)):
        if pic is not None:
            line = min(p["lines"], key=lambda l: (not pic["bbox"][1] - 0.5 <= l["baseline"] <= pic["bbox"][3] + 0.5,
                                                   abs((pic["bbox"][1] + pic["bbox"][3]) / 2 - l["baseline"] + 0.35 * run["size"])))
            hole_lines[pic["id"]] = (line, run["size"])
    rows = []
    for el, oid in pictures:
        if el.get("role") not in ("math", "icon") or not el.get("anchor") or el.get("number") or el.get("overlay"):
            continue
        anchor = texts.get(el["anchor"])
        if el["id"] in hole_lines:
            line, size = hole_lines[el["id"]]
        elif anchor and line_of(el["bbox"], anchor["paragraphs"]):
            p, line = line_of(el["bbox"], anchor["paragraphs"])
            size = p["size"]
        else:
            continue
        placed = boxes.get(oid)
        row = {"key": el["id"], "role": el["role"], "hole": el["id"] in hole_lines}
        if placed is None:
            rows.append({**row, "missing": "no such element in Slides"})
            continue
        ink = picture_ink(out / el["file"], ring_colour(ref, el["bbox"]))
        top, bottom = line["baseline"] - 0.65 * size, line["baseline"] + 0.05 * size
        sbox = [v / scale for v in placed]
        dy = sbox[1] - el["bbox"][1]
        p_gaps = hole_gaps(ref, ink, el["bbox"], top, bottom, size)
        s_gaps = hole_gaps(got, ink, sbox, top + dy, bottom + dy, size)
        if p_gaps is None or s_gaps is None:
            rows.append({**row, "missing": "no picture ink on the line"})
            continue
        errs = {side: round(s_gaps[side] - p_gaps[side], 2) for side in ("left", "right")
                if s_gaps[side] is not None and p_gaps[side] is not None}
        overlap = [side for side in ("left", "right") if s_gaps[side] is not None and p_gaps[side] is not None
                   and s_gaps[side] < 0.25 and p_gaps[side] >= 0.5]
        overlap += [f"covered {side}" for side in ("left", "right") if s_gaps[f"covered_{side}"] and not p_gaps[f"covered_{side}"]]
        x0, y0, x1, y1 = el["bbox"]
        rows.append({**row, "pdf": [p_gaps["left"], p_gaps["right"]], "slides": [s_gaps["left"], s_gaps["right"]],
                     "moved": [round(sbox[0] - x0, 2), round(dy, 2)],
                     "error": max((abs(v) for v in errs.values()), default=None), "errors": errs, "overlap": overlap,
                     "draw": draw([x0 - 2.5 * size, min(y0, top) - 2, x1 + 2.5 * size, max(y1, bottom) + 2], dy,
                                  [el["bbox"]] + p_gaps["marks"], [sbox] + s_gaps["marks"])})
    return rows


def number_offset(img: Image2, box: list[float], color: str) -> tuple[float, float] | None:
    """Centroid of the number-coloured ink inside a ball minus the ball box's centre (pt)."""
    rgb, a0, b0 = img.crop(*box)
    if rgb.size == 0:
        return None
    h, w = rgb.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = (box[0] + box[2]) / 2 * img.k - a0 - 0.5, (box[1] + box[3]) / 2 * img.k - b0 - 0.5
    inner = (xx - cx) ** 2 + (yy - cy) ** 2 < (0.36 * min(box[2] - box[0], box[3] - box[1]) * img.k) ** 2
    num = hex_rgb(color).astype(np.float32)
    ball = np.median(rgb[inner], axis=0)
    axis = num - ball
    if np.dot(axis, axis) < 30 ** 2:
        return None
    t = np.clip(((rgb - ball) @ axis) / np.dot(axis, axis), 0, 1) * inner
    t = np.where(t >= 0.3, t, 0)
    if t.sum() < 1:
        return None
    return float((xx * t).sum() / t.sum() - cx) / img.k, float((yy * t).sum() / t.sum() - cy) / img.k


def measure_numbers(pictures, boxes, ref: Image2, got: Image2, scale: float) -> list[dict]:
    rows = []
    for el, oid in pictures:
        if not el.get("number"):
            continue
        row = {"key": el["id"], "text": el["number"]["text"]}
        if oid not in boxes:
            rows.append({**row, "missing": "no such element in Slides"})
            continue
        sbox = [v / scale for v in boxes[oid]]
        x0, y0, x1, y1 = el["bbox"]
        pic = draw([x0 - 4, y0 - 4, x1 + 4, y1 + 4], sbox[1] - y0, [el["bbox"]], [sbox])
        p = number_offset(ref, el["bbox"], el["number"]["color"])
        s = number_offset(got, sbox, el["number"]["color"])
        if p is None or s is None:
            rows.append({**row, "missing": "no number ink in " + ("the PDF" if p is None else "Slides"), "draw": pic})
            continue
        d = (s[0] - p[0], s[1] - p[1])
        cp, cs = ((x0 + x1) / 2 + p[0], (y0 + y1) / 2 + p[1]), ((sbox[0] + sbox[2]) / 2 + s[0], (sbox[1] + sbox[3]) / 2 + s[1])
        pic["pdf"].append([cp[0], cp[1], cp[0], cp[1]])
        pic["slides"].append([cs[0], cs[1], cs[0], cs[1]])
        rows.append({**row, "pdf": [round(v, 2) for v in p], "slides": [round(v, 2) for v in s],
                     "delta": [round(v, 2) for v in d], "error": round(float(np.hypot(*d)), 2), "draw": pic})
    return rows


def lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz = xyz / np.array([0.9505, 1.0, 1.089])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def bullet_ink(img: Image2, cell: list[float], shaded: bool) -> dict | None:
    """Ink box (pt) and colour of the biggest ink blob in a bullet's cell (`shaded`: a ball)."""
    rgb, a0, b0 = img.crop(*cell)
    if rgb.size == 0:
        return None
    s = strength(rgb)
    on = s >= EDGE
    if not on.any():
        return None
    cols, rows = runs(s.max(axis=0), 0.6 * img.k), runs(s.max(axis=1), 0.6 * img.k)
    c0, c1 = max(cols, key=lambda r: r[1] - r[0])
    r0, r1 = max(rows, key=lambda r: r[1] - r[0])
    blob = rgb[int(r0):int(np.ceil(r1)), int(c0):int(np.ceil(c1))].reshape(-1, 3)
    if shaded:  # the median of the pixels unlike the page, as render.ink_colour samples a ball
        pix = blob[np.abs(blob - np.median(rgb.reshape(-1, 3), axis=0)).sum(axis=1) > 60]
    else:  # a flat glyph: its fully covered pixels (thin strokes are mostly edges)
        core = s[int(r0):int(np.ceil(r1)), int(c0):int(np.ceil(c1))].reshape(-1)
        pix = blob[core >= 0.8 * core.max()]
    if not len(pix):
        return None
    return {"box": [round((a0 + c0) / img.k, 2), round((b0 + r0) / img.k, 2), round((a0 + c1) / img.k, 2), round((b0 + r1) / img.k, 2)],
            "color": "#%02x%02x%02x" % tuple(int(v) for v in np.median(pix, axis=0))}


def hex_rgb(color: str) -> np.ndarray:
    return np.array([int(color.lstrip("#")[j:j + 2], 16) for j in (0, 2, 4)], np.float64)


def measure_bullets(slide, ref: Image2, got: Image2) -> list[dict]:
    tints = [e for e in slide["elements"] if e["kind"] == "shape" and e.get("fill") and 0 < e.get("opacity", 1) < 1]
    rows = []
    for el in slide["elements"]:
        if el["kind"] != "text":
            continue
        for i, p in enumerate(el["paragraphs"]):
            b = p.get("bullet")
            if not b or bullet_shape(b) is None or not p["lines"]:
                continue
            size, base = p["size"], p["lines"][0]["baseline"]
            x0, _, x1, _ = b["bbox"]
            cell = [x0 - 0.3 * size, min(b["bbox"][1], base - 0.9 * size), (x1 + max(x1, p["text_x0"])) / 2,
                    max(b["bbox"][3], base + 0.25 * size)]
            row = {"key": f"{el['id']}.{i}", "kind": b["kind"], "shape": bullet_shape(b)}
            shaded = b["kind"] == "image"
            pi, si = bullet_ink(ref, cell, shaded), bullet_ink(got, cell, shaded)
            pic = draw([cell[0] - 3, cell[1] - 2, p["text_x0"] + 2 * size, cell[3] + 2], 0.0,
                       [cell] + ([pi["box"]] if pi else []), [cell] + ([si["box"]] if si else []))
            if pi is None or si is None:
                rows.append({**row, "missing": "no bullet ink in " + ("the PDF" if pi is None else "Slides"), "draw": pic})
                continue
            pw, ph = pi["box"][2] - pi["box"][0], pi["box"][3] - pi["box"][1]
            sw, sh = si["box"][2] - si["box"][0], si["box"][3] - si["box"][1]
            ratio = [round(sw / max(pw, 0.1), 2), round(sh / max(ph, 0.1), 2)]
            colour = hex_rgb(pi["color"])
            cx, cy = (cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2
            for tint in tints:
                # A translucent highlight is drawn over the bullet in the PDF, under the text in
                # Slides: compare with the colour it tints.
                if tint["bbox"][0] <= cx <= tint["bbox"][2] and tint["bbox"][1] <= cy <= tint["bbox"][3]:
                    a = tint["opacity"]
                    colour = np.clip((colour - a * hex_rgb(tint["fill"])) / (1 - a), 0, 255)
                    row["untinted"] = "#%02x%02x%02x" % tuple(int(round(v)) for v in colour)
            de = float(np.linalg.norm(lab(hex_rgb(si["color"])) - lab(colour)))
            compared = ratio[1:] if bullet_shape(b) in SUBSTITUTED else ratio
            rows.append({**row, "pdf": pi, "slides": si, "size_ratio": ratio,
                         "size_error": round(max(abs(r - 1) for r in compared), 2), "delta_e": round(de, 1),
                         "centre": [round((si["box"][0] + si["box"][2] - pi["box"][0] - pi["box"][2]) / 2, 2),
                                    round((si["box"][1] + si["box"][3] - pi["box"][1] - pi["box"][3]) / 2, 2)], "draw": pic})
    return rows


def word_runs(img: Image2, x0: float, x1: float, top: float, bottom: float, size: float,
              ink: np.ndarray, box: list[float]) -> list[tuple[float, float]]:
    """Words (x extents, pt) on a line band, an overlay picture's ink removed. A gap the picture
    hides entirely (an arrow crossing a word) doesn't split a word."""
    got = band_profile(img, x0, x1, top, bottom, [(ink, box)])
    if got is None:
        return []
    text, _, a0, hidden = got
    words = []
    for s, e in runs(text, WORD_GAP_EM * size * img.k):
        if words and hidden[int(words[-1][1]):max(int(words[-1][1]) + 1, int(np.ceil(s)))].all():
            words[-1] = (words[-1][0], e)
        else:
            words.append((s, e))
    return [((a0 + s) / img.k, (a0 + e) / img.k) for s, e in words]


def word_edge(img: Image2, x: float, left_edge: bool, top: float, bottom: float, size: float,
              ink: np.ndarray, box: list[float]) -> float | None:
    """The word edge (pt) nearest `x` on a line band."""
    edges = [w[0 if left_edge else 1] for w in word_runs(img, x - 1.5 * size, x + 1.5 * size, top, bottom, size, ink, box)]
    return min(edges, key=lambda v: abs(v - x)) if edges else None


def measure_overlays(slide, pictures, boxes, ref: Image2, got: Image2, out: Path, scale: float) -> list[dict]:
    texts = {e["id"]: e for e in slide["elements"] if e["kind"] == "text"}
    rows = []
    for el, oid in pictures:
        if not el.get("overlay") or not el.get("marks") or el.get("anchor") not in texts:
            continue
        if oid not in boxes:
            rows.append({"key": el["id"], "missing": "no such element in Slides"})
            continue
        ink = picture_ink(out / el["file"], ring_colour(ref, el["bbox"]))
        bx0, _, bx1, _ = el["bbox"]
        sbox = [v / scale for v in boxes[oid]]
        stretch = (sbox[2] - sbox[0]) / max(bx1 - bx0, 0.1)
        for j, m in enumerate(m for m in el["marks"]):
            row = {"key": f"{el['id']}.m{j}", "x": m["x"]}
            before = m["before"]
            left_edge = not before or abs(before[-1][6] + before[-1][0] - m["hole_x0"]) > 0.3
            lines = [(p, l) for p in texts[el["anchor"]]["paragraphs"] for l in p["lines"]
                     if l["x0"] - 1 <= m["x"] <= l["x1"] + 1]
            if not lines:
                rows.append({**row, "missing": "no line at the mark"})
                continue
            y0, y1 = el["bbox"][1], el["bbox"][3]
            # A graphic can span several lines: on the mark's line, the ink has word edges where
            # the words before the mark start and end, and one at the mark.
            wanted = [(b[6], 0) for b in before[-3:] if len(b) >= 7] + [(b[6] + b[0], 1) for b in before[-3:] if len(b) >= 7]
            wanted.append((m["x"], 0 if left_edge else 1))
            found = []
            for p, line in lines:
                top, bottom = line["baseline"] - 0.65 * m["size"], line["baseline"] + 0.05 * m["size"]
                words = word_runs(ref, line["x0"] - 1, m["x"] + 1.5 * m["size"], top, bottom, m["size"], ink, el["bbox"])
                miss = sum(min([abs(w[side] - x) for w in words] + [3.0]) for x, side in wanted)
                found.append((miss, top, bottom))
            _, top, bottom = min(found)
            ep = word_edge(ref, m["x"], left_edge, top, bottom, m["size"], ink, el["bbox"])
            if ep is not None and abs(ep - m["x"]) > 1.5:
                ep = None  # (no visible word edge at the mark)
            meet = sbox[0] + (m["x"] - bx0) * stretch
            es = word_edge(got, meet + (ep - m["x"] if ep is not None else 0.0), left_edge, top, bottom, m["size"], ink, sbox)
            region = [m["x"] - 4 * m["size"], min(y0, top) - 2, m["x"] + 4 * m["size"], max(y1, bottom) + 2]
            pic = draw(region, 0.0, [el["bbox"], [m["x"], top - 3, m["x"], top - 1]] + ([[ep, top, ep, bottom]] if ep is not None else []),
                       [sbox, [meet, top - 3, meet, top - 1]] + ([[es, top, es, bottom]] if es is not None else []))
            if ep is None or es is None:
                rows.append({**row, "missing": "no word edge in " + ("the PDF" if ep is None else "Slides"), "draw": pic})
                continue
            drift = (es - meet) - (ep - m["x"])
            rows.append({**row, "edge": "left" if left_edge else "right", "pdf": round(ep - m["x"], 2),
                         "slides": round(es - meet, 2), "error": round(abs(drift), 2), "drift": round(drift, 2), "draw": pic})
    return rows


# ---------------------------------------------------------------- deck

def measure(out: Path, refresh: bool = False, crops: str = "failures") -> dict:
    """Measure a converted folder into alignment.json. Evidence crops (alignment/NNN-item.png)
    for `crops` = "failures", "all" or "none" items."""
    out = Path(out)
    for old in (out / "alignment").glob("*.png"):
        old.unlink()
    deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
    state = json.loads((out / "emit.json").read_text(encoding="utf-8"))
    pdf = out / "slides.pdf" if (out / "slides.pdf").exists() else Path(deck["source"]["pdf"])
    doc = Document(pdf)
    boxes = slides_elements(out, state, refresh)
    scale = state["scale"]
    fonts = FontMapper()
    report = {"deck": out.name, "presentationId": state["presentationId"], "url": state["url"], "slides": []}
    for slide, emitted in zip(deck["slides"], state["slides"]):
        n = slide["page"]
        slide = {**slide, "elements": merge_blocks(slide["elements"])}
        if len(slide["elements"]) != len(emitted["elements"]):
            report["slides"].append({"page": n, "missing": "deck.json and emit.json disagree"})
            continue
        thumb = Image.open(out / "fidelity" / f"slides-{n + 1:03}.png").convert("RGB")
        k = thumb.width / slide["size"][0]
        got = Image2(np.asarray(thumb), k)
        ref = Image2(doc[n].render(k), k)
        pictures = [(e, oid) for e, oid in zip(slide["elements"], emitted["elements"]) if e["kind"] == "image"]
        measured = {
            "page": n,
            "holes": measure_holes(slide, pictures, boxes, ref, got, out, scale, fonts),
            "numbers": measure_numbers(pictures, boxes, ref, got, scale),
            "bullets": measure_bullets(slide, ref, got),
            "overlays": measure_overlays(slide, pictures, boxes, ref, got, out, scale),
        }
        for kind in ("holes", "numbers", "bullets", "overlays"):
            for row in measured[kind]:
                if "draw" in row and (crops == "all" or crops == "failures" and row_failures(kind, row)):
                    row["evidence"] = f"alignment/{n + 1:03}-{row['key']}.png"
                    save_evidence(out / row["evidence"], row["draw"], ref, got)
        report["slides"].append(measured)
    (out / "alignment.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def items(report: dict):
    """(kind, key, row) for every measured item; keys are `page:item` (page 1-based)."""
    for s in report["slides"]:
        for kind in ("holes", "numbers", "bullets", "overlays"):
            for row in s.get(kind, []):
                yield kind, f"{s['page'] + 1}:{row['key']}", row


def metrics(kind: str, row: dict) -> dict[str, float]:
    """The values thresholds and baselines apply to."""
    if "missing" in row:
        return {}
    if kind == "bullets":
        return {"bullet_delta_e": row["delta_e"], "bullet_size": row["size_error"]}
    if kind == "holes":
        return {"hole_gap": row["error"]} if row["error"] is not None else {}
    return {{"numbers": "number_offset", "overlays": "overlay_drift"}[kind]: row["error"]}


def row_failures(kind: str, row: dict) -> list[str]:
    """What is wrong with one item: thresholds exceeded, overlaps, nothing to measure."""
    out = [f"{name} {value} > {THRESHOLDS[name]}" for name, value in metrics(kind, row).items() if value > THRESHOLDS[name]]
    if row.get("overlap"):
        out.append("overlap " + ", ".join(row["overlap"]))
    if "missing" in row:
        out.append("unmeasured: " + row["missing"])
    return out


def failures(report: dict) -> list[tuple[str, str, str]]:
    """(kind, key, message) for every failing item."""
    return [(kind, key, msg) for kind, key, row in items(report) for msg in row_failures(kind, row)]


def print_summary(report: dict) -> None:
    print(f"{report['deck']}: {report['url']}")
    print(f"{'kind':<9} {'item':<22} {'error':>6}  detail")
    for kind, key, row in items(report):
        if "missing" in row:
            detail, err = f"missing: {row['missing']}", "-"
        elif kind == "holes":
            detail = f"gaps pdf {row['pdf']} slides {row['slides']}" + (f" OVERLAP {row['overlap']}" if row["overlap"] else "")
            err = "-" if row["error"] is None else f"{row['error']:.2f}"
        elif kind == "numbers":
            detail, err = f"'{row['text']}' pdf {row['pdf']} slides {row['slides']}", f"{row['error']:.2f}"
        elif kind == "bullets":
            detail = f"{row['shape']} size x{row['size_ratio']} dE {row['delta_e']} {row['pdf']['color']}->{row['slides']['color']}"
            err = f"{row['size_error']:.2f}"
        else:
            detail, err = f"{row['edge']} edge at {row['x']} pdf {row['pdf']} slides {row['slides']}", f"{row['error']:.2f}"
        print(f"{kind:<9} {key:<22} {err:>6}  {detail}")
    bad = failures(report)
    print(f"{len(bad)} failing" + "".join(f"\n  {k} {key}: {msg}" for k, key, msg in bad))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path, help="a converted output folder with fidelity thumbnails")
    ap.add_argument("--refresh", action="store_true", help="read the Slides elements again")
    ap.add_argument("--crops", choices=["failures", "all", "none"], default="failures",
                    help="evidence crops (alignment/NNN-item.png): PDF above, Slides below")
    args = ap.parse_args()
    print_summary(measure(args.out, args.refresh, args.crops))


if __name__ == "__main__":
    main()
