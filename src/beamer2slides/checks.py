"""Invariants of a classified and rendered deck, checked locally (no Google calls).

Native text is set in substitute fonts, so it moves a little in Slides; whatever stays in the
background picture right at its words then drifts off them. These checks catch that class of
problem from deck.json and the rendered backgrounds:

- `stray_ink`: no ink in the background within a few pt of native words, bullets and the
  pictures anchored to them (unless hidden under an opaque shape or unanchored picture, or the
  words sit on a raster image or shading); ink that runs on past the words is the page;
- `stray_labels`: no item label (small drawing, image or glyph) left in the background beside a
  paragraph without a bullet;
- `lost_ink`: whatever the slide no longer shows (not in the background, or hidden under a
  shape) is carried by something: native text, a bullet, a picture, a table or diagram, a text
  decoration, a shape's rim, strip or shadow;
- `structure_problems`: hole runs each have one picture no wider than the hole (after
  emit.fit_holes), number pictures lie on their ball, overlays have anchor, marks and drawings,
  underline / strike / highlight runs have their drawing in the text's strokes, shape bullets
  have a known shape and colour, ids are unique and anchors, spans and drawings exist;
- `junk_text`: no private-use, replacement or picture-font glyphs, control characters or lone
  combining marks in native text.

Each finding is a dict: check, page (0-based), element (or None), bbox (pt, or None) and detail.
tests/test_invariants.py runs them over the test decks; tools can call `convert_locally` and
`run_checks` on any PDF.
"""

import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import render
from .classify import classify
from .emit import BULLET_SHAPES, SLIDE_W, FontMapper, fit_holes, merge_blocks, slide_holes
from .extract import extract, select_overlays
from .notes import prepare
from .pdf import Document

INK_DIFF = 48          # colour difference (max channel) from the ground that counts as ink
NEAR_WORDS = 2.5       # pt around line boxes, bullets and anchored pictures
MIN_INK_PX = 12        # pixels of a component worth reporting (background at ~5 px/pt)
REACH = 10             # pt past that: ink running on this far is page structure, not the words'


@dataclass
class Rendered:
    raw: dict
    deck: dict
    backgrounds: dict[int, np.ndarray] = field(default_factory=dict)  # page -> RGB, as render wrote it
    originals: dict[int, np.ndarray] = field(default_factory=dict)    # page -> RGB of the PDF page, same scale

    def px_per_pt(self, slide: dict) -> float:
        return render.BACKGROUND_WIDTH_PX / slide["size"][0]


def convert_locally(pdf: Path, overlays: str = "last") -> Rendered:
    """extract, classify and render as `convert` does, keeping the pictures in memory."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        prepared = prepare(pdf, out)  # (without note pages)
        raw = select_overlays(extract(prepared.pdf, prepared.labels), overlays)
        deck = classify(raw)
        saved: dict[Path, np.ndarray] = {}
        keep = render.save_png
        render.save_png = lambda img, path: saved.__setitem__(Path(path), img)
        try:
            render.render_backgrounds(prepared.pdf, raw, deck, out)
        finally:
            render.save_png = keep
        result = Rendered(raw, deck)
        doc = Document(prepared.pdf)
        try:
            for slide in deck["slides"]:
                page = doc[slide["page"]]
                result.backgrounds[slide["page"]] = saved[out / slide["background"]]
                result.originals[slide["page"]] = page.render(render.BACKGROUND_WIDTH_PX / page.width)
        finally:
            doc.close()
    return result


def run_checks(rendered: Rendered) -> list[dict]:
    return [f for slide in rendered.deck["slides"] for check in CHECKS.values() for f in check(rendered, slide)]


def allowed(finding: dict, deck: str, allow: list[dict]) -> dict | None:
    """The allowlist entry covering a finding: same deck, page (1-based) and check, and the same
    element or an overlapping region where the entry names one."""
    for entry in allow:
        if entry["deck"] == deck and entry["page"] == finding["page"] + 1 and entry["check"] == finding["check"] \
                and entry.get("element", finding["element"]) == finding["element"] \
                and (not entry.get("bbox") or (finding["bbox"] and intersects(grow(entry["bbox"], 1), finding["bbox"]))):
            return entry
    return None


# ---------------------------------------------------------------- geometry helpers

Box = list[float]


def grow(b, d: float) -> Box:
    return [b[0] - d, b[1] - d, b[2] + d, b[3] + d]


def union(boxes) -> Box:
    boxes = list(boxes)
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(outer, inner, tol: float = 0.5) -> bool:
    return outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1] and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol


def px_box(b, k: float, shape) -> tuple[int, int, int, int]:
    h, w = shape[:2]
    return (max(0, int(np.floor(b[0] * k))), max(0, int(np.floor(b[1] * k))),
            min(w, int(np.ceil(b[2] * k))), min(h, int(np.ceil(b[3] * k))))


def paint(mask: np.ndarray, b, k: float, value=True) -> None:
    a0, b0, a1, b1 = px_box(b, k, mask.shape)
    if a1 > a0 and b1 > b0:
        mask[b0:b1, a0:a1] = value


def inked(img: np.ndarray, ground) -> np.ndarray:
    """Pixels whose colour differs from `ground` (a colour or an image of the same size) by more
    than INK_DIFF in some channel."""
    ground = np.asarray(ground, np.uint8)
    d = np.maximum(img, ground) - np.minimum(img, ground)
    return (d[..., 0] > INK_DIFF) | (d[..., 1] > INK_DIFF) | (d[..., 2] > INK_DIFF)  # (faster than any(axis=2))


def cell_counts(mask: np.ndarray, cell: int) -> np.ndarray:
    """Marked pixels per cell of a grid of `cell` px squares."""
    h, w = mask.shape
    gh, gw = -(-h // cell), -(-w // cell)
    padded = np.zeros((gh * cell, gw * cell), bool)
    padded[:h, :w] = mask
    return padded.reshape(gh, cell, gw, cell).sum(axis=(1, 3))


def labels(cells: np.ndarray) -> tuple[np.ndarray, int]:
    """8-connected labels of a boolean grid, and their number."""
    out = np.zeros(cells.shape, int)
    n = 0
    h, w = cells.shape
    for start in zip(*np.nonzero(cells)):
        if out[start]:
            continue
        n += 1
        out[start] = n
        stack = [start]
        while stack:
            r, c = stack.pop()
            for rr in (r - 1, r, r + 1):
                for cc in (c - 1, c, c + 1):
                    if 0 <= rr < h and 0 <= cc < w and cells[rr, cc] and not out[rr, cc]:
                        out[rr, cc] = n
                        stack.append((rr, cc))
    return out, n


def cells_box(comp: np.ndarray, cell: int, origin: tuple[int, int], k: float) -> Box:
    """Box in pt of a component of grid cells whose grid starts at pixel `origin` (x, y)."""
    rows, cols = np.nonzero(comp)
    return [round((origin[0] + cols.min() * cell) / k, 1), round((origin[1] + rows.min() * cell) / k, 1),
            round((origin[0] + (cols.max() + 1) * cell) / k, 1), round((origin[1] + (rows.max() + 1) * cell) / k, 1)]


def components(mask: np.ndarray, k: float) -> list[tuple[Box, int]]:
    """Connected groups of marked pixels, on a grid of about 1 pt cells: (box in pt, pixels)."""
    cell = max(1, int(round(k)))
    counts = cell_counts(mask, cell)
    grid, n = labels(counts > 0)
    return [(cells_box(grid == i, cell, (0, 0), k), int(counts[grid == i].sum())) for i in range(1, n + 1)]


# ---------------------------------------------------------------- what moves with the text

def raw_page(rendered: Rendered, slide: dict) -> dict:
    return next(p for p in rendered.raw["pages"] if p["index"] == slide["page"])


def text_lines(boxes: list[Box]) -> list[Box]:
    """Span boxes grouped into lines (vertical overlap of their middles)."""
    lines: list[Box] = []
    for b in sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        cy = (b[1] + b[3]) / 2
        for i, l in enumerate(lines):
            if l[1] <= cy <= l[3] and b[1] <= (l[1] + l[3]) / 2 <= b[3]:
                lines[i] = union([l, b])
                break
        else:
            lines.append(list(b))
    return lines


def moving_units(slide: dict, page: dict) -> list[tuple[str, Box]]:
    """(element id, box) of what Slides sets or places relative to the text: line boxes of
    native words (text, tables, diagrams), bullets and the pictures anchored to text."""
    spans = {s["id"]: s for s in page["spans"]}
    out = []
    for el in slide["elements"]:
        if el["kind"] in ("text", "table", "diagram"):
            boxes = [spans[i]["bbox"] for i in el.get("spans", []) if i in spans and spans[i]["text"].strip()]
            out += [(el["id"], l) for l in text_lines(boxes)]
            for p in el.get("paragraphs", []):
                if p["bullet"] and p["bullet"].get("bbox"):
                    out.append((el["id"], p["bullet"]["bbox"]))
        elif el["kind"] == "image" and el.get("anchor") and not el.get("overlay"):
            out.append((el["id"], el["bbox"]))
    return out


def covered(slide: dict) -> list[Box]:
    """Areas Slides hides behind something that does not move with the text: opaque shapes and
    pictures that are not anchored to text."""
    out = []
    for el in slide["elements"]:
        if el["kind"] == "shape" and not el.get("opacity"):
            out.append(el["bbox"])
            if el.get("title_bar"):
                out.append([el["bbox"][0], el["title_bar"][1], el["bbox"][2], el["bbox"][3]])
        elif el["kind"] == "image" and not el.get("anchor") and not el.get("overlay"):
            out.append(el["bbox"])
    return out


def on_pictures(page: dict) -> list[Box]:
    """Raster images and shadings in the PDF that are large enough for text to sit on."""
    return [im["bbox"] for im in page["images"] if min(im["bbox"][2] - im["bbox"][0], im["bbox"][3] - im["bbox"][1]) >= 12]


# ---------------------------------------------------------------- invariant 1

def _exits(on_border: np.ndarray) -> int:
    """Separate stretches of a component along the window's border, walked round the perimeter."""
    ring = np.concatenate([on_border[0, :], on_border[1:, -1], on_border[-1, -2::-1], on_border[-2:0:-1, 0]])
    if ring.all():
        return 1
    starts = ring & ~np.roll(ring, 1)
    return int(starts.sum())


def stray_ink(rendered: Rendered, slide: dict) -> list[dict]:
    """Ink left in the background near what moves with the text. Ink that runs on well past the
    words (a panel or bar edge, a rule longer than the line, a gradient) is the page they sit on;
    ink that stays within reach of the words, or leaves it in one place only (an arrow from a
    word), belongs to them."""
    img = rendered.backgrounds[slide["page"]]
    k = rendered.px_per_pt(slide)
    page = raw_page(rendered, slide)
    hidden = np.zeros(img.shape[:2], bool)
    for b in covered(slide):
        paint(hidden, b, k)
    textured = on_pictures(page)
    cell = max(1, int(round(k)))  # ~1 pt
    found: list[tuple[str, Box, int]] = []
    for eid, box in moving_units(slide, page):
        if any(contains(t, box, tol=0) for t in textured):
            continue  # words on a picture or shading: its pixels are the ground
        zone = grow(box, NEAR_WORDS)
        c0, d0, c1, d1 = px_box(zone, k, img.shape)
        e0, f0, e1, f1 = px_box(grow(zone, 3), k, img.shape)
        if c1 <= c0 or d1 <= d0:
            continue
        near = img[f0:f1, e0:e1]
        seen = ~hidden[f0:f1, e0:e1]
        inner = np.zeros(near.shape[:2], bool)
        inner[d0 - f0:d1 - f0, c0 - e0:c1 - e0] = True
        ring_px = near[~inner & seen]
        if len(ring_px) < 20:
            continue
        ground = np.median(ring_px, axis=0).round().astype(np.uint8)
        if not (inked(near[d0 - f0:d1 - f0, c0 - e0:c1 - e0], ground) & seen[d0 - f0:d1 - f0, c0 - e0:c1 - e0]).any():
            continue
        a0, b0, a1, b1 = px_box(grow(zone, REACH), k, img.shape)
        ink = inked(img[b0:b1, a0:a1], ground) & ~hidden[b0:b1, a0:a1]
        counts = cell_counts(ink, cell)
        grid, n = labels(counts >= 2)
        zone_cells = np.zeros(grid.shape, bool)
        zone_cells[(d0 - b0) // cell:-(-(d1 - b0) // cell), (c0 - a0) // cell:-(-(c1 - a0) // cell)] = True
        border = np.zeros(grid.shape, bool)
        border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
        for i in range(1, n + 1):
            comp = grid == i
            in_zone = int(counts[comp & zone_cells].sum())
            if in_zone < MIN_INK_PX:
                continue
            if _exits(comp & border) >= 2 or (comp & border).sum() > 2 * REACH:
                continue  # passes by (or a large area beyond the window): the page under the words
            found.append((eid, cells_box(comp, cell, (a0, b0), k), in_zone))
    findings = []
    for eid, box, pixels in found:  # one finding per graphic, even when several units are near it
        same = next((f for f in findings if intersects(f["bbox"], box)), None)
        if same:
            same["bbox"] = union([same["bbox"], box])
            continue
        findings.append({"check": "stray_ink", "page": slide["page"], "element": eid, "bbox": box,
                         "detail": f"{pixels} px near the words"})
    return findings


def stray_labels(rendered: Rendered, slide: dict) -> list[dict]:
    """Item labels left in the background: a small drawing, image or glyph at x-height just left
    of a paragraph's first line that has no bullet, when no picture, table or diagram takes it.
    (Pixels can't tell a white square on a dark sidebar from the page; the drawings can.)"""
    page = raw_page(rendered, slide)
    elements = slide["elements"]
    taken = [e["bbox"] for e in elements if e["kind"] in ("image", "table", "diagram")]
    taken += [p["bullet"]["bbox"] for e in elements for p in e.get("paragraphs", []) if p["bullet"] and p["bullet"].get("bbox")]
    used = {i for e in elements for i in e.get("spans", []) + e.get("drawings", []) + [e.get("drawing")]}
    used |= set(slide.get("on_layout", []))
    graphics = [(d["id"], d["bbox"]) for d in page["drawings"]] + [(i["id"], i["bbox"]) for i in page["images"]] + \
               [(s["id"], s["bbox"]) for s in page["spans"] if s["text"].strip()]
    out = []
    for el in elements:
        if el["kind"] != "text":
            continue
        for p in el["paragraphs"]:
            if p["bullet"] or p.get("tab_x0") is not None or not p["lines"]:
                continue
            line, size = p["lines"][0], p["size"]
            for gid, g in graphics:
                w, h = g[2] - g[0], g[3] - g[1]
                cy = (g[1] + g[3]) / 2
                if gid in used or not (0.25 * size <= w <= 1.3 * size and 0.25 * size <= h <= 1.6 * size) or \
                        not line["baseline"] - 0.9 * size <= cy <= line["baseline"] + 0.1 * size or \
                        not line["x0"] - 3 * size <= g[2] <= line["x0"] - 0.1 * size or \
                        any(contains(t, g, tol=1) for t in taken):
                    continue
                out.append({"check": "stray_labels","page": slide["page"], "element": el["id"], "bbox": g,
                            "detail": f"{gid} left of a paragraph without bullet"})
    return out


# ---------------------------------------------------------------- invariant 2

def _hex(c: str) -> np.ndarray:
    return np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], float)


def preview(rendered: Rendered, slide: dict) -> np.ndarray:
    """The background with the slide's shapes painted over it (boxes, no rounded corners)."""
    img = rendered.backgrounds[slide["page"]].copy()
    k = rendered.px_per_pt(slide)
    for el in merge_blocks(slide["elements"]):  # in emit's z-order: title bars over block bodies
        if el["kind"] != "shape" or not el.get("fill"):
            continue
        a0, b0, a1, b1 = px_box(el["bbox"], k, img.shape)
        alpha = el.get("opacity") or 1.0
        img[b0:b1, a0:a1] = np.round((1 - alpha) * img[b0:b1, a0:a1] + alpha * _hex(el["fill"])).astype(np.uint8)
    return img


def carried(slide: dict, page: dict, shape: tuple, k: float) -> np.ndarray:
    """Pixels whose content something native or a picture takes over: glyphs of native text
    (and of text moved to the layout), bullets, pictures, table and diagram boxes, text
    decorations, and the rims, corners, strips and shadows of shapes."""
    spans = {s["id"]: s for s in page["spans"]}
    mask = np.zeros(shape[:2], bool)

    def glyphs(ids):
        for i in ids:
            s = spans.get(i)
            if s:
                paint(mask, grow(s["bbox"], render.GLYPH_MARGIN * s["size"] + 0.5), k)

    glyphs(slide.get("on_layout", []))
    for el in slide["elements"]:
        glyphs(el.get("spans", []))
        for p in el.get("paragraphs", []):
            if p["bullet"] and p["bullet"].get("bbox"):
                paint(mask, grow(p["bullet"]["bbox"], 1), k)
        for r in el.get("strokes", []):
            paint(mask, grow(r, 1.5), k)
        if el["kind"] in ("image", "table", "diagram"):
            paint(mask, grow(el["bbox"], 1), k)
        elif el["kind"] == "shape":
            x0, y0, x1, y1 = el["bbox"]
            y0 = el["title_bar"][1] if el.get("title_bar") else y0
            rim, corner = 1.5, el.get("radius", 0) + 1.5
            for r in ([x0 - rim, y0 - rim, x1 + rim, y0 + rim], [x0 - rim, y1 - rim, x1 + rim, y1 + rim],
                      [x0 - rim, y0 - rim, x0 + rim, y1 + rim], [x1 - rim, y0 - rim, x1 + rim, y1 + rim],
                      [x0, y0, x0 + corner, y0 + corner], [x1 - corner, y0, x1, y0 + corner],
                      [x0, y1 - corner, x0 + corner, y1], [x1 - corner, y1 - corner, x1, y1]):
                paint(mask, r, k)
            for r in list(el.get("strips", [])) + (el["shadow"]["pieces"] if el.get("shadow") else []):
                paint(mask, grow(r, 1), k)
    return mask


def lost_ink(rendered: Rendered, slide: dict) -> list[dict]:
    """Ink of the PDF page that the slide no longer shows (not in the background or under a
    shape) and that nothing native or no picture carries."""
    k = rendered.px_per_pt(slide)
    shown = preview(rendered, slide)
    original = rendered.originals[slide["page"]]
    if original.shape != shown.shape:
        return [{"check": "lost_ink", "page": slide["page"], "element": None, "bbox": None,
                 "detail": f"background {shown.shape} vs page {original.shape}"}]
    gone = inked(original, shown) & ~carried(slide, raw_page(rendered, slide), shown.shape, k)
    return [{"check": "lost_ink", "page": slide["page"], "element": None, "bbox": box, "detail": f"{pixels} px"}
            for box, pixels in components(gone, k) if pixels >= MIN_INK_PX]


# ---------------------------------------------------------------- invariant 3

def structure_problems(rendered: Rendered, slide: dict) -> list[dict]:
    """The mechanisms that keep graphics with their words are wired up consistently."""
    page = raw_page(rendered, slide)
    elements = slide["elements"]
    by_id = {e["id"]: e for e in elements}
    spans = {s["id"]: s for s in page["spans"]}
    drawings = {d["id"]: d for d in page["drawings"]}
    images = {i["id"] for i in page["images"]}
    out = []

    def problem(el: dict | None, detail: str, bbox=None) -> None:
        out.append({"check": "structure", "page": slide["page"], "element": el["id"] if el else None,
                    "bbox": bbox or (el["bbox"] if el else None), "detail": detail})

    if len(by_id) != len(elements):
        problem(None, "duplicate element ids")
    for el in elements:
        if el.get("anchor") is not None and by_id.get(el["anchor"], {}).get("kind") != "text":
            problem(el, f"anchor {el['anchor']} is no text element on the slide")
        missing = [i for i in el.get("spans", []) if i not in spans] + \
                  [i for i in el.get("drawings", []) + ([el["drawing"]] if el.get("drawing") else []) if i not in drawings]
        if missing:
            problem(el, f"refers to missing spans or drawings {missing[:3]}")
        if el.get("overlay") and not (el.get("anchor") and el.get("marks") and el.get("drawings")):
            problem(el, "overlay without anchor, marks or drawings")
        if el.get("number"):
            n = el["number"]
            x0, y0, x1, y1 = el["bbox"]
            cx, cy = n["center"]
            balls = [r["bbox"] for r in page["images"] + page["drawings"]
                     if contains(el["bbox"], r["bbox"], tol=1) and r["bbox"][0] <= cx <= r["bbox"][2]
                     and r["bbox"][1] <= cy <= r["bbox"][3] and (r["bbox"][2] - r["bbox"][0]) >= 0.6 * (x1 - x0)]
            if not balls:
                problem(el, f"number {n['text']!r} picture lies on no ball")
            elif not (x0 <= n["x0"] <= x1 and y0 <= n["baseline"] <= y1 + 0.3 * n["size"]):
                problem(el, f"number {n['text']!r} label is off its ball")
        for p in el.get("paragraphs", []):
            b = p["bullet"]
            if not b:
                continue
            if b["kind"] == "shape" and (b.get("shape") not in BULLET_SHAPES or not str(b.get("color", "")).startswith("#")):
                problem(el, f"shape bullet without a known shape and colour: {b.get('shape')} {b.get('color')}", b["bbox"])
            if b["kind"] == "image" and b.get("image") not in images:
                problem(el, f"image bullet refers to missing image {b.get('image')}", b["bbox"])
        if el["kind"] == "text":
            decorated = {k for p in el["paragraphs"] for r in p["runs"] for k in ("underline", "strike", "highlight") if r.get(k)}
            for kind in sorted(decorated):
                if not any(_styles(kind, r, p) for r in el.get("strokes", []) for p in el["paragraphs"]):
                    problem(el, f"{kind} runs without their drawing in the text's strokes")
    pictures = [e for e in elements if e["kind"] == "image" and e.get("anchor") and e.get("role") == "math"]
    matched = set()
    fitted = fit_holes(slide, SLIDE_W / slide["size"][0], _fonts())  # the hole widths emit writes
    for el, p, run, pic in slide_holes(fitted):
        if pic is None:
            problem(el, f"hole at x {run['hole_x0']} has no picture")
            continue
        matched.add(pic["id"])
        width = pic["bbox"][2] - pic["bbox"][0]
        if run["hole"] < width - 0.05:
            problem(el, f"hole {run['hole']} pt narrower than its picture {pic['id']} ({width:.2f} pt)", pic["bbox"])
    for pic in pictures:
        if pic["id"] not in matched and pic["id"][len(f"p{slide['page']}"):].startswith("h"):
            problem(pic, "hole picture without a hole run in its text")
    return out


_font_mapper: list = []


def _fonts() -> FontMapper:
    if not _font_mapper:
        _font_mapper.append(FontMapper())
    return _font_mapper[0]


def _styles(kind: str, rect: Box, p: dict) -> bool:
    """A decoration drawing where a run style of that kind would be on one of the paragraph's lines."""
    size, cy, h = p["size"], (rect[1] + rect[3]) / 2, rect[3] - rect[1]
    for line in p["lines"]:
        if rect[2] < line["x0"] - 1 or rect[0] > line["x1"] + 1:
            continue
        below = cy - line["baseline"]
        if kind == "underline" and h <= 1.5 and 0 < below <= 0.5 * size:
            return True
        if kind == "strike" and h <= 1.5 and -0.45 * size <= below <= -0.1 * size:
            return True
        if kind == "highlight" and h >= 0.8 * size and rect[1] <= line["baseline"] <= rect[3]:
            return True
    return False


# ---------------------------------------------------------------- invariant 4

PICTURE_FONT_GLYPHS = set("❤❙✭")  # what LINE10 / LCIRCLE picture-mode glyphs came out as


def junk_characters(text: str) -> list[str]:
    bad = []
    for i, ch in enumerate(text):
        o = ord(ch)
        if 0xE000 <= o <= 0xF8FF or o >= 0xF0000 or ch == "�" or ch in PICTURE_FONT_GLYPHS:
            bad.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn") and ch not in "\t\n\x0b":
            bad.append(ch)
        elif unicodedata.combining(ch) and (i == 0 or text[i - 1].isspace()):
            bad.append(ch)  # a combining mark with no letter under it
    return bad


def junk_text(rendered: Rendered, slide: dict) -> list[dict]:
    """Characters in native text that no font shows as the PDF did."""
    out = []

    def check(el: dict, texts) -> None:
        text = "".join(texts)
        bad = junk_characters(text)
        if bad:
            out.append({"check": "junk_text", "page": slide["page"], "element": el.get("id"), "bbox": el.get("bbox"),
                        "detail": " ".join(f"U+{ord(c):04X}" for c in dict.fromkeys(bad)) + f" in {text[:60]!r}"})

    def paragraphs(ps):
        for p in ps:
            yield "".join(r["text"] for r in p["runs"]) + "\n"
            if p.get("bullet") and p["bullet"].get("text"):
                yield p["bullet"]["text"] + "\n"

    for el in slide["elements"] + slide.get("theme_texts", []):
        if el["kind"] == "text":
            check(el, paragraphs(el["paragraphs"]))
        elif el["kind"] == "table":
            check(el, ("".join(r["text"] for r in cell) + "\n" for row in el["cells"] for cell in row))
        elif el["kind"] == "diagram":
            for node in el["nodes"]:
                check(el, ("".join(r["text"] for r in par) + "\n" for par in node.get("paragraphs") or []))
                for box in node.get("text") or []:
                    check(el, paragraphs(box["paragraphs"]))
        if el.get("number"):
            check(el, [el["number"]["text"]])
    return out



CHECKS = {"stray_ink": stray_ink, "stray_labels": stray_labels, "lost_ink": lost_ink,
          "structure": structure_problems, "junk_text": junk_text}  # finding "check" -> function
