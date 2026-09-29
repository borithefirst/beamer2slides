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
  have a shape emit knows, ids are unique and anchors, spans and drawings exist;
- `junk_text`: no private-use, replacement or picture-font glyphs, control characters or lone
  combining marks in native text.

The checks read the deck as render leaves it, so they work on `ir_types.RenderedSlide`: a slide
given as deck.json's dict is parsed where it comes in. They need what only render makes (the
backgrounds, the pictures grown to their ink), which is why the stage is the rendered one. Of
raw.json they read a few fields of each page's spans, drawings and images (`RawPage`).

Each finding is written as a dict: check, page (0-based), element (or None), bbox (pt, or None)
and detail. tests/test_invariants.py runs them over the test decks; tools can call
`convert_locally` and `run_checks` on any PDF.
"""

import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, Union

import numpy as np

from .arrays import Floats, Ints, Mask, Pixels, RGB
from . import render
from .classify import classify
from .emit import BULLET_SHAPES, SLIDE_W, FontMapper, fit_holes, merge_blocks, slide_holes
from .extract import extract, select_overlays
from .ir_types import (At, Box, DiagramElement, FallbackImage, Fields, HoleRun, NumberBullet, Paragraph,
                       RenderedDrawnBullet, RenderedElement, RenderedGlyphBullet, RenderedImage, RenderedImageBullet,
                       RenderedMarkedShape, RenderedSlide, RenderedText, Run, ShapeBullet, ShapeElement, TableElement,
                       ThemeText, box, element_json, integer, number, parse_deck, parse_rendered_element,
                       parse_rendered_slide, point, run, slide_json, string, tuple_of)
from .json_types import Json, JsonObject, as_int, as_objects
from .notes import prepare
from .pdf import Document
from .raw_types import RawDoc
from .typing_compat import assert_never

INK_DIFF = 48          # colour difference (max channel) from the ground that counts as ink
NEAR_WORDS = 2.5       # pt around line boxes, bullets and anchored pictures
MIN_INK_PX = 12        # pixels of a component worth reporting (background at ~5 px/pt)
REACH = 10             # pt past that: ink running on this far is page structure, not the words'

CheckName = Literal["stray_ink", "stray_labels", "lost_ink", "structure", "junk_text"]


class Finding(TypedDict):
    """What a check found, in the form tools print and tests/invariants_allow.json matches."""
    check: CheckName
    page: int
    element: str | None
    bbox: list[float] | None
    detail: str


def finding(check: CheckName, page: int, element: str | None, bbox: Box | None, detail: str) -> Finding:
    return {"check": check, "page": page, "element": element, "bbox": None if bbox is None else list(bbox),
            "detail": detail}


# ---------------------------------------------------------------- raw.json, as far as the checks read it

@dataclass(frozen=True, kw_only=True)
class RawSpan:
    id: str
    bbox: Box
    text: str
    size: float


@dataclass(frozen=True, kw_only=True)
class RawGraphic:
    """A drawing or an image of the page."""
    id: str
    bbox: Box


@dataclass(frozen=True, kw_only=True)
class RawPage:
    index: int
    spans: tuple[RawSpan, ...]
    drawings: tuple[RawGraphic, ...]
    images: tuple[RawGraphic, ...]


# The keys of raw.json the checks read, as records (raw_types' TypedDicts are extract's whole page;
# these hold what the checks use, read from it once).

def raw_span(v: object, at: At) -> RawSpan:
    f = Fields(v, at, "raw span")
    return RawSpan(id=f.req("id", string), bbox=f.req("bbox", box), text=f.req("text", string),
                   size=f.req("size", number))


def raw_graphic(v: object, at: At) -> RawGraphic:
    f = Fields(v, at, "raw drawing or image")
    return RawGraphic(id=f.req("id", string), bbox=f.req("bbox", box))


def raw_page_of(v: object, at: At) -> RawPage:
    f = Fields(v, at, "raw page")
    return RawPage(index=f.req("index", integer), spans=f.req("spans", tuple_of(raw_span)),
                   drawings=f.req("drawings", tuple_of(raw_graphic)), images=f.req("images", tuple_of(raw_graphic)))


def raw_pages(raw: RawDoc) -> dict[int, RawPage]:
    """raw.json's pages by index (the first of an index, as a search in page order finds it)."""
    out: dict[int, RawPage] = {}
    for page in tuple_of(raw_page_of)(raw["pages"], At(where="raw.json", path="pages")):
        out.setdefault(page.index, page)
    return out


# ---------------------------------------------------------------- the deck, locally

@dataclass(frozen=True, kw_only=True)
class Rendered:
    raw: RawDoc
    deck: JsonObject                  # deck.json as render left it: what callers write and emit plans
    pages: Mapping[int, RawPage]      # raw.json's pages, read
    backgrounds: Mapping[int, RGB]    # page -> RGB, as render wrote it
    originals: Mapping[int, RGB]      # page -> RGB of the PDF page, same scale

    def px_per_pt(self, slide: JsonObject | RenderedSlide) -> float:
        return render.BACKGROUND_WIDTH_PX / slide_size(slide)[0]


def slide_size(slide: JsonObject | RenderedSlide) -> tuple[float, float]:
    if isinstance(slide, RenderedSlide):
        return slide.size
    return point(slide["size"], At(where="slide", path="size"))


def convert_locally(pdf: Path) -> Rendered:
    """extract, classify and render as `convert` does, keeping the pictures in memory (the last
    overlay step of each frame, as `convert` keeps by default)."""
    return convert_pages(pdf, "last")


def convert_pages(pdf: Path, overlays: str) -> Rendered:
    """`convert_locally` keeping the overlay steps `overlays` says (`extract.select_overlays`)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        prepared = prepare(pdf, out)  # (without note pages)
        raw: RawDoc = select_overlays(extract(prepared.pdf, prepared.labels), overlays)
        deck: JsonObject = classify(raw)
        saved: dict[Path, Pixels] = {}

        def keep_png(img: Pixels, path: Path) -> None:
            saved[Path(path)] = img

        keep = render.save_png
        render.save_png = keep_png
        try:
            render.render_backgrounds(prepared.pdf, raw, deck, out)
        finally:
            render.save_png = keep
        backgrounds: dict[int, RGB] = {}
        originals: dict[int, RGB] = {}
        doc = Document(prepared.pdf)
        try:
            # (deck.json is read here only for each slide's page and background file: the checks
            # parse it whole where they start, so a deck that does not parse still converts)
            for slide in deck_slides(deck):
                n = as_int(slide["page"], "slide.page")
                page = doc[n]
                backgrounds[n] = saved[out / string(slide["background"], At(where="slide", path="background"))]
                originals[n] = page.render(render.BACKGROUND_WIDTH_PX / page.width)
        finally:
            doc.close()
    return Rendered(raw=raw, deck=deck, pages=raw_pages(raw), backgrounds=backgrounds, originals=originals)


def deck_slides(deck: JsonObject) -> list[JsonObject]:
    return as_objects(deck["slides"], "deck.json slides")


Check = Callable[[Rendered, Union[JsonObject, RenderedSlide]], list[Finding]]


def run_checks(rendered: Rendered) -> list[Finding]:
    deck = parse_deck(rendered.deck, "rendered")
    return [f for slide in deck.slides for check in CHECKS.values() for f in check(rendered, slide)]


def rendered_slide(slide: JsonObject | RenderedSlide) -> RenderedSlide:
    """The slide a check reads: a dict from deck.json is parsed here."""
    return slide if isinstance(slide, RenderedSlide) else parse_rendered_slide(slide)


def json_box(v: Json, where: str) -> Box | None:
    return None if v is None else box(v, At(where=where, path="bbox"))


def allowed(found: Finding, deck: str, allow: Sequence[JsonObject]) -> JsonObject | None:
    """The allowlist entry covering a finding: same deck, page (1-based) and check, and the same
    element or an overlapping region where the entry names one."""
    region = found["bbox"]
    for entry in allow:
        if entry["deck"] == deck and entry["page"] == found["page"] + 1 and entry["check"] == found["check"] \
                and entry.get("element", found["element"]) == found["element"]:
            wanted = json_box(entry.get("bbox") or None, "allowlist entry")
            if wanted is None or (region and intersects(grow(wanted, 1), box(region, At(where="finding", path="bbox")))):
                return entry
    return None


# ---------------------------------------------------------------- geometry helpers

def grow(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def union(boxes: Iterable[Box]) -> Box:
    bs = list(boxes)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def intersects(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(outer: Box, inner: Box, tol: float) -> bool:
    return outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1] and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol


def px_box(b: Box, k: float, shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    h, w = shape[:2]
    return (max(0, int(np.floor(b[0] * k))), max(0, int(np.floor(b[1] * k))),
            min(w, int(np.ceil(b[2] * k))), min(h, int(np.ceil(b[3] * k))))


def paint(mask: Mask, b: Box, k: float) -> None:
    a0, b0, a1, b1 = px_box(b, k, mask.shape)
    if a1 > a0 and b1 > b0:
        mask[b0:b1, a0:a1] = True


def inked(img: RGB, ground: RGB) -> Mask:
    """Pixels whose colour differs from `ground` (a colour or an image of the same size) by more
    than INK_DIFF in some channel."""
    d = np.maximum(img, ground) - np.minimum(img, ground)
    return (d[..., 0] > INK_DIFF) | (d[..., 1] > INK_DIFF) | (d[..., 2] > INK_DIFF)  # (faster than any(axis=2))


def cell_counts(mask: Mask, cell: int) -> Ints:
    """Marked pixels per cell of a grid of `cell` px squares."""
    h, w = mask.shape
    gh, gw = -(-h // cell), -(-w // cell)
    padded = np.zeros((gh * cell, gw * cell), np.bool_)
    padded[:h, :w] = mask
    counts: Ints = padded.reshape(gh, cell, gw, cell).sum(axis=(1, 3), dtype=np.int64)
    return counts


def labels(cells: Mask) -> tuple[Ints, int]:
    """8-connected labels of a boolean grid, and their number."""
    out = np.zeros(cells.shape, np.int64)
    n = 0
    h, w = cells.shape
    rows, cols = np.nonzero(cells)
    for start in zip(rows.tolist(), cols.tolist()):
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


def cells_box(comp: Mask, cell: int, origin: tuple[int, int], k: float) -> Box:
    """Box in pt of a component of grid cells whose grid starts at pixel `origin` (x, y)."""
    rows, cols = np.nonzero(comp)
    return (round((origin[0] + int(cols.min()) * cell) / k, 1), round((origin[1] + int(rows.min()) * cell) / k, 1),
            round((origin[0] + (int(cols.max()) + 1) * cell) / k, 1),
            round((origin[1] + (int(rows.max()) + 1) * cell) / k, 1))


def components(mask: Mask, k: float) -> list[tuple[Box, int]]:
    """Connected groups of marked pixels, on a grid of about 1 pt cells: (box in pt, pixels)."""
    cell = max(1, round(k))
    counts = cell_counts(mask, cell)
    grid, n = labels(counts > 0)
    return [(cells_box(grid == i, cell, (0, 0), k), int(counts[grid == i].sum())) for i in range(1, n + 1)]


# ---------------------------------------------------------------- what an element refers to

def element_spans(el: RenderedElement) -> tuple[str, ...]:
    match el:
        case RenderedText() | RenderedImage() | ShapeElement() | RenderedMarkedShape() | TableElement() | \
                DiagramElement():
            return el.spans
        case FallbackImage():
            return ()
        case _:
            assert_never(el)


def element_drawings(el: RenderedElement) -> tuple[str, ...]:
    """The page drawings an element takes (a shape's own and its frame's, a figure's, a table's rules)."""
    match el:
        case RenderedImage() | TableElement():
            return el.drawings or ()
        case RenderedMarkedShape():
            return el.drawings
        case ShapeElement():
            return (el.drawing,) if el.drawing else ()
        case RenderedText() | FallbackImage() | DiagramElement():
            return ()
        case _:
            assert_never(el)


def element_tiles(el: RenderedElement) -> tuple[str, ...]:
    return el.tiles or () if isinstance(el, ShapeElement) else ()


def bullet_boxes(el: RenderedElement) -> list[Box]:
    return [p.bullet.bbox for p in el.paragraphs if p.bullet] if isinstance(el, RenderedText) else []


# ---------------------------------------------------------------- what moves with the text

def raw_page(rendered: Rendered, slide: RenderedSlide) -> RawPage:
    return rendered.pages[slide.page]


def text_lines(boxes: Sequence[Box]) -> list[Box]:
    """Span boxes grouped into lines (vertical overlap of their middles)."""
    lines: list[Box] = []
    for b in sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        cy = (b[1] + b[3]) / 2
        for i, l in enumerate(lines):
            if l[1] <= cy <= l[3] and b[1] <= (l[1] + l[3]) / 2 <= b[3]:
                lines[i] = union([l, b])
                break
        else:
            lines.append(b)
    return lines


def moving_units(slide: RenderedSlide, page: RawPage) -> list[tuple[str, Box]]:
    """(element id, box) of what Slides sets or places relative to the text: line boxes of
    native words (text, tables, diagrams), bullets and the pictures anchored to text."""
    spans = {s.id: s for s in page.spans}
    out: list[tuple[str, Box]] = []
    for el in slide.elements:
        match el:
            case RenderedText() | TableElement() | DiagramElement():
                boxes = [spans[i].bbox for i in el.spans if i in spans and spans[i].text.strip()]
                out += [(el.id, l) for l in text_lines(boxes)]
                out += [(el.id, b) for b in bullet_boxes(el)]
            case RenderedImage():
                if el.anchor and not el.overlay:
                    out.append((el.id, el.bbox))
            case FallbackImage() | ShapeElement() | RenderedMarkedShape():
                pass
            case _:
                assert_never(el)
    return out


def covered(slide: RenderedSlide) -> list[Box]:
    """Areas Slides hides behind something that does not move with the text: opaque shapes and
    pictures that are not anchored to text."""
    out: list[Box] = []
    for el in slide.elements:
        match el:
            case ShapeElement():
                if not el.opacity:
                    out.append(el.bbox)
                    if el.title_bar:
                        out.append((el.bbox[0], el.title_bar[1], el.bbox[2], el.bbox[3]))
            case RenderedMarkedShape():
                if not el.opacity:
                    out.append(el.bbox)
            case RenderedImage():
                if not el.anchor and not el.overlay:
                    out.append(el.bbox)
            case FallbackImage():
                out.append(el.bbox)  # (a picture of its own, anchored to nothing)
            case RenderedText() | TableElement() | DiagramElement():
                pass
            case _:
                assert_never(el)
    return out


def on_pictures(page: RawPage) -> list[Box]:
    """Raster images and shadings in the PDF that are large enough for text to sit on."""
    return [im.bbox for im in page.images if min(im.bbox[2] - im.bbox[0], im.bbox[3] - im.bbox[1]) >= 12]


# ---------------------------------------------------------------- invariant 1

def _exits(on_border: Mask) -> int:
    """Separate stretches of a component along the window's border, walked round the perimeter."""
    ring = np.concatenate([on_border[0, :], on_border[1:, -1], on_border[-1, -2::-1], on_border[-2:0:-1, 0]])
    if ring.all():
        return 1
    starts = ring & ~np.roll(ring, 1)
    return int(starts.sum())


def stray_ink(rendered: Rendered, slide: JsonObject | RenderedSlide) -> list[Finding]:
    """Ink left in the background near what moves with the text. Ink that runs on well past the
    words (a panel or bar edge, a rule longer than the line, a gradient) is the page they sit on;
    ink that stays within reach of the words, or leaves it in one place only (an arrow from a
    word), belongs to them."""
    s = rendered_slide(slide)
    img = rendered.backgrounds[s.page]
    k = rendered.px_per_pt(s)
    page = raw_page(rendered, s)
    hidden = np.zeros(img.shape[:2], np.bool_)
    for b in covered(s):
        paint(hidden, b, k)
    textured = on_pictures(page)
    cell = max(1, round(k))  # ~1 pt
    found: list[tuple[str, Box, int]] = []
    for eid, unit in moving_units(s, page):
        if any(contains(t, unit, 0) for t in textured):
            continue  # words on a picture or shading: its pixels are the ground
        zone = grow(unit, NEAR_WORDS)
        c0, d0, c1, d1 = px_box(zone, k, img.shape)
        e0, f0, e1, f1 = px_box(grow(zone, 3), k, img.shape)
        if c1 <= c0 or d1 <= d0:
            continue
        near = img[f0:f1, e0:e1]
        seen = ~hidden[f0:f1, e0:e1]
        inner = np.zeros(near.shape[:2], np.bool_)
        inner[d0 - f0:d1 - f0, c0 - e0:c1 - e0] = True
        ring_px = near[~inner & seen]
        if len(ring_px) < 20:
            continue
        ground: RGB = np.median(ring_px, axis=0).round().astype(np.uint8)
        if not (inked(near[d0 - f0:d1 - f0, c0 - e0:c1 - e0], ground) & seen[d0 - f0:d1 - f0, c0 - e0:c1 - e0]).any():
            continue
        a0, b0, a1, b1 = px_box(grow(zone, REACH), k, img.shape)
        ink = inked(img[b0:b1, a0:a1], ground) & ~hidden[b0:b1, a0:a1]
        counts = cell_counts(ink, cell)
        grid, n = labels(counts >= 2)
        zone_cells = np.zeros(grid.shape, np.bool_)
        zone_cells[(d0 - b0) // cell:-(-(d1 - b0) // cell), (c0 - a0) // cell:-(-(c1 - a0) // cell)] = True
        border = np.zeros(grid.shape, np.bool_)
        border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
        for i in range(1, n + 1):
            comp = grid == i
            in_zone = int(counts[comp & zone_cells].sum())
            if in_zone < MIN_INK_PX:
                continue
            if _exits(comp & border) >= 2 or (comp & border).sum() > 2 * REACH:
                continue  # passes by (or a large area beyond the window): the page under the words
            found.append((eid, cells_box(comp, cell, (a0, b0), k), in_zone))
    graphics: list[tuple[str, Box, int]] = []
    for eid, b, pixels in found:  # one finding per graphic, even when several units are near it
        same = next((i for i, g in enumerate(graphics) if intersects(g[1], b)), None)
        if same is not None:
            first, seen_box, first_pixels = graphics[same]
            graphics[same] = (first, union([seen_box, b]), first_pixels)
            continue
        graphics.append((eid, b, pixels))
    return [finding(check="stray_ink", page=s.page, element=eid, bbox=b, detail=f"{pixels} px near the words")
            for eid, b, pixels in graphics]


def stray_labels(rendered: Rendered, slide: JsonObject | RenderedSlide) -> list[Finding]:
    """Item labels left in the background: a small drawing, image or glyph at x-height just left
    of a paragraph's first line that has no bullet, when no picture, table or diagram takes it.
    (Pixels can't tell a white square on a dark sidebar from the page; the drawings can.)"""
    s = rendered_slide(slide)
    page = raw_page(rendered, s)
    elements = s.elements
    taken = [e.bbox for e in elements if isinstance(e, (RenderedImage, FallbackImage, TableElement, DiagramElement))]
    taken += [b for e in elements for b in bullet_boxes(e)]
    used = {i for e in elements for i in element_spans(e) + element_drawings(e) + element_tiles(e)}
    used |= set(s.on_layout)
    graphics = [(d.id, d.bbox) for d in page.drawings] + [(i.id, i.bbox) for i in page.images] + \
               [(sp.id, sp.bbox) for sp in page.spans if sp.text.strip()]
    out: list[Finding] = []
    for el in elements:
        if not isinstance(el, RenderedText):
            continue
        for p in el.paragraphs:
            if p.bullet or p.tab_x0 is not None or not p.lines:
                continue
            line, size = p.lines[0], p.size
            for gid, g in graphics:
                w, h = g[2] - g[0], g[3] - g[1]
                cy = (g[1] + g[3]) / 2
                if gid in used or not (0.25 * size <= w <= 1.3 * size and 0.25 * size <= h <= 1.6 * size) or \
                        not line.baseline - 0.9 * size <= cy <= line.baseline + 0.1 * size or \
                        not line.x0 - 3 * size <= g[2] <= line.x0 - 0.1 * size or \
                        any(contains(t, g, 1) for t in taken):
                    continue
                out.append(finding(check="stray_labels", page=s.page, element=el.id, bbox=g,
                                   detail=f"{gid} left of a paragraph without bullet"))
    return out


# ---------------------------------------------------------------- invariant 2

def _hex(c: str) -> Floats:
    return np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], np.float64)


def preview(rendered: Rendered, slide: RenderedSlide) -> RGB:
    """The background with the slide's shapes painted over it (boxes, no rounded corners)."""
    img = rendered.backgrounds[slide.page].copy()
    k = rendered.px_per_pt(slide)
    where = f"slide page {slide.page}"
    # (in emit's z-order, title bars over block bodies; emit merges the dicts it writes)
    for merged in merge_blocks([element_json(e) for e in slide.elements]):
        el = parse_rendered_element(merged, where)
        if not isinstance(el, (ShapeElement, RenderedMarkedShape)):
            continue
        a0, b0, a1, b1 = px_box(el.bbox, k, img.shape)
        alpha = el.opacity or 1.0
        img[b0:b1, a0:a1] = np.round((1 - alpha) * img[b0:b1, a0:a1] + alpha * _hex(el.fill)).astype(np.uint8)
    return img


def carried(slide: RenderedSlide, page: RawPage, shape: tuple[int, ...], k: float) -> Mask:
    """Pixels whose content something native or a picture takes over: glyphs of native text
    (and of text moved to the layout), bullets, pictures, table and diagram boxes, text
    decorations, and the rims, corners, strips and shadows of shapes."""
    spans = {s.id: s for s in page.spans}
    mask = np.zeros(shape[:2], np.bool_)

    def glyphs(ids: Iterable[str]) -> None:
        for i in ids:
            s = spans.get(i)
            if s:
                paint(mask, grow(s.bbox, render.GLYPH_MARGIN * s.size + 0.5), k)

    def rims(bbox: Box, top: float, radius: float) -> None:
        x0, _, x1, y1 = bbox
        y0 = top
        rim, corner = 1.5, radius + 1.5
        for r in ((x0 - rim, y0 - rim, x1 + rim, y0 + rim), (x0 - rim, y1 - rim, x1 + rim, y1 + rim),
                  (x0 - rim, y0 - rim, x0 + rim, y1 + rim), (x1 - rim, y0 - rim, x1 + rim, y1 + rim),
                  (x0, y0, x0 + corner, y0 + corner), (x1 - corner, y0, x1, y0 + corner),
                  (x0, y1 - corner, x0 + corner, y1), (x1 - corner, y1 - corner, x1, y1)):
            paint(mask, r, k)

    glyphs(slide.on_layout)
    for el in slide.elements:
        glyphs(element_spans(el))
        for b in bullet_boxes(el):
            paint(mask, grow(b, 1), k)
        match el:
            case RenderedText():
                for r in el.strokes:
                    paint(mask, grow(r, 1.5), k)
            case RenderedImage() | FallbackImage() | TableElement() | DiagramElement():
                paint(mask, grow(el.bbox, 1), k)
            case ShapeElement():
                rims(el.bbox, el.title_bar[1] if el.title_bar else el.bbox[1], el.radius)
                for r in (el.strips or ()) + (el.shadow.pieces if el.shadow else ()):
                    paint(mask, grow(r, 1), k)
            case RenderedMarkedShape():
                rims(el.bbox, el.bbox[1], el.radius)
            case _:
                assert_never(el)
    return mask


def lost_ink(rendered: Rendered, slide: JsonObject | RenderedSlide) -> list[Finding]:
    """Ink of the PDF page that the slide no longer shows (not in the background or under a
    shape) and that nothing native or no picture carries."""
    s = rendered_slide(slide)
    k = rendered.px_per_pt(s)
    shown = preview(rendered, s)
    original = rendered.originals[s.page]
    if original.shape != shown.shape:
        return [finding(check="lost_ink", page=s.page, element=None, bbox=None,
                        detail=f"background {shown.shape} vs page {original.shape}")]
    gone = inked(original, shown) & ~carried(s, raw_page(rendered, s), shown.shape, k)
    return [finding(check="lost_ink", page=s.page, element=None, bbox=b, detail=f"{pixels} px")
            for b, pixels in components(gone, k) if pixels >= MIN_INK_PX]


# ---------------------------------------------------------------- invariant 3

def structure_problems(rendered: Rendered, slide: JsonObject | RenderedSlide) -> list[Finding]:
    """The mechanisms that keep graphics with their words are wired up consistently."""
    s = rendered_slide(slide)
    page = raw_page(rendered, s)
    elements = s.elements
    by_id = {e.id: e for e in elements}
    spans = {sp.id for sp in page.spans}
    drawings = {d.id for d in page.drawings}
    images = {i.id for i in page.images}
    where = f"slide page {s.page}"
    out: list[Finding] = []

    def problem(el: RenderedElement | None, detail: str, bbox: Box | None) -> None:
        out.append(finding(check="structure", page=s.page, element=el.id if el else None,
                           bbox=bbox or (el.bbox if el else None), detail=detail))

    if len(by_id) != len(elements):
        problem(None, "duplicate element ids", None)
    for el in elements:
        anchor = el.anchor if isinstance(el, (RenderedImage, ShapeElement)) else None
        if anchor is not None and not isinstance(by_id.get(anchor), RenderedText):
            problem(el, f"anchor {anchor} is no text element on the slide", None)
        missing = [i for i in element_spans(el) if i not in spans] + \
                  [i for i in element_drawings(el) if i not in drawings]
        if missing:
            problem(el, f"refers to missing spans or drawings {missing[:3]}", None)
        if isinstance(el, RenderedImage):
            if el.overlay and not (el.anchor and el.marks and el.drawings):
                problem(el, "overlay without anchor, marks or drawings", None)
            n = el.number
            if n:
                x0, y0, x1, y1 = el.bbox
                cx, cy = n.center
                balls = [r.bbox for r in page.images + page.drawings
                         if contains(el.bbox, r.bbox, 1) and r.bbox[0] <= cx <= r.bbox[2]
                         and r.bbox[1] <= cy <= r.bbox[3] and (r.bbox[2] - r.bbox[0]) >= 0.6 * (x1 - x0)]
                if not balls:
                    problem(el, f"number {n.text!r} picture lies on no ball", None)
                elif not (x0 <= n.x0 <= x1 and y0 <= n.baseline <= y1 + 0.3 * n.size):
                    problem(el, f"number {n.text!r} label is off its ball", None)
        if isinstance(el, RenderedText):
            for p in el.paragraphs:
                b = p.bullet
                match b:
                    case ShapeBullet():
                        # (the parse holds the colour to '#rrggbb' and the shape to ir's; this is emit's list)
                        if b.shape not in BULLET_SHAPES:
                            problem(el, f"shape bullet without a known shape and colour: {b.shape} {b.color}", b.bbox)
                    case RenderedImageBullet():
                        if b.image not in images:
                            problem(el, f"image bullet refers to missing image {b.image}", b.bbox)
                    case RenderedGlyphBullet() | RenderedDrawnBullet() | NumberBullet() | None:
                        pass
                    case _:
                        assert_never(b)
            decorated = {k for p in el.paragraphs for r in p.runs for k in DECORATIONS if decoration(r, k)}
            for kind in sorted(decorated):
                if not any(_styles(kind, r, p) for r in el.strokes for p in el.paragraphs):
                    problem(el, f"{kind} runs without their drawing in the text's strokes", None)
    pictures = [e for e in elements if isinstance(e, RenderedImage) and e.anchor and e.role == "math"]
    matched: set[str] = set()
    fitted = fit_holes(slide_json(s), SLIDE_W / s.size[0], _fonts())  # the hole widths emit writes
    for el_json, _, run_json, pic_json in slide_holes(fitted):
        el = parse_rendered_element(el_json, where)
        hole = run(run_json, At(where=f"{where}, element {el.id}", path="run"))
        if not isinstance(hole, HoleRun):
            raise ValueError(f"{where}: emit.slide_holes gave a run with no hole")
        if pic_json is None:
            problem(el, f"hole at x {hole.hole_x0} has no picture", None)
            continue
        pic = parse_rendered_element(pic_json, where)
        matched.add(pic.id)
        width = pic.bbox[2] - pic.bbox[0]
        if hole.hole < width - 0.05:
            problem(el, f"hole {hole.hole} pt narrower than its picture {pic.id} ({width:.2f} pt)", pic.bbox)
    for pic in pictures:
        if pic.id not in matched and pic.id[len(f"p{s.page}"):].startswith("h"):
            problem(pic, "hole picture without a hole run in its text", None)
    return out


Decoration = Literal["underline", "strike", "highlight"]
DECORATIONS: tuple[Decoration, ...] = ("underline", "strike", "highlight")


def decoration(r: Run | HoleRun, kind: Decoration) -> bool:
    """Whether a run carries a decoration of that kind (a hole run is never struck)."""
    match kind:
        case "underline":
            return r.underline
        case "strike":
            return bool(r.strike) if isinstance(r, Run) else False
        case "highlight":
            return r.highlight is not None
        case _:
            assert_never(kind)


_font_mapper: list[FontMapper] = []


def _fonts() -> FontMapper:
    if not _font_mapper:
        _font_mapper.append(FontMapper())
    return _font_mapper[0]


def _styles(kind: Decoration, rect: Box, p: Paragraph) -> bool:
    """A decoration drawing where a run style of that kind would be on one of the paragraph's lines."""
    size, cy, h = p.size, (rect[1] + rect[3]) / 2, rect[3] - rect[1]
    for line in p.lines:
        if rect[2] < line.x0 - 1 or rect[0] > line.x1 + 1:
            continue
        below = cy - line.baseline
        if kind == "underline" and h <= 1.5 and 0 < below <= 0.5 * size:
            return True
        if kind == "strike" and h <= 1.5 and -0.45 * size <= below <= -0.1 * size:
            return True
        if kind == "highlight" and h >= 0.8 * size and rect[1] <= line.baseline <= rect[3]:
            return True
    return False


# ---------------------------------------------------------------- invariant 4

PICTURE_FONT_GLYPHS = set("❤❙✭")  # what LINE10 / LCIRCLE picture-mode glyphs came out as


def junk_characters(text: str) -> list[str]:
    bad: list[str] = []
    for i, ch in enumerate(text):
        o = ord(ch)
        if 0xE000 <= o <= 0xF8FF or o >= 0xF0000 or ch == "�" or ch in PICTURE_FONT_GLYPHS:
            bad.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn") and ch not in "\t\n\x0b":
            bad.append(ch)
        elif unicodedata.combining(ch) and (i == 0 or text[i - 1].isspace()):
            bad.append(ch)  # a combining mark with no letter under it
    return bad


def junk_text(rendered: Rendered, slide: JsonObject | RenderedSlide) -> list[Finding]:
    """Characters in native text that no font shows as the PDF did."""
    s = rendered_slide(slide)
    out: list[Finding] = []

    def check(eid: str | None, bbox: Box, texts: Iterable[str]) -> None:
        text = "".join(texts)
        bad = junk_characters(text)
        if bad:
            out.append(finding(check="junk_text", page=s.page, element=eid, bbox=bbox,
                               detail=" ".join(f"U+{ord(c):04X}" for c in dict.fromkeys(bad)) + f" in {text[:60]!r}"))

    def paragraphs(ps: Iterable[Paragraph]) -> Iterator[str]:
        for p in ps:
            yield "".join(r.text for r in p.runs) + "\n"
            if p.bullet and p.bullet.text:
                yield p.bullet.text + "\n"

    items: tuple[RenderedElement | ThemeText, ...] = (*s.elements, *s.theme_texts)
    for el in items:
        match el:
            case RenderedText():
                check(el.id, el.bbox, paragraphs(el.paragraphs))
            case ThemeText():
                check(None, el.bbox, paragraphs(el.paragraphs))
            case TableElement():
                check(el.id, el.bbox, ("".join(r.text for r in cell) + "\n" for row in el.cells for cell in row))
            case DiagramElement():
                for node in el.nodes:
                    check(el.id, el.bbox, ("".join(r.text for r in par) + "\n" for par in node.paragraphs))
                    for card in node.text or ():
                        check(el.id, el.bbox, paragraphs(card.paragraphs))
            case RenderedImage():
                if el.number:
                    check(el.id, el.bbox, [el.number.text])
            case FallbackImage() | ShapeElement() | RenderedMarkedShape():
                pass
            case _:
                assert_never(el)
    return out


CHECKS: dict[CheckName, Check] = {"stray_ink": stray_ink, "stray_labels": stray_labels, "lost_ink": lost_ink,
                                  "structure": structure_problems, "junk_text": junk_text}  # finding "check" -> function
