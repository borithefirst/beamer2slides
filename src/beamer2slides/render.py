"""Stage 3: background images with the converted content removed, plus figure pictures.

The PDF is never rewritten. Page objects that go away entirely (text objects whose glyphs
all became native, vector paths inside a removed area, images inside a figure) are switched
off before rendering. Objects that lose only part of their content (a text object with one
converted word, an image partly under a figure) are handled on pixels: the page is rendered
a second time without them, and those pixels replace the removed glyphs' boxes.

Text becomes native in Slides, so its glyphs leave first; images and vector graphics under
it stay. Figures are cropped from that text-free page (so native text never ends up inside a
picture) and then removed: their labels, vector paths inside the figure box and image pixels
within it.
"""

from pathlib import Path

import numpy as np
from PIL import Image

from .pdf import OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, Char, Document, Page, _addr

BACKGROUND_WIDTH_PX = 2000
FIGURE_PX_PER_PT = 8.0     # ~ 4 px per Slides point on a 4:3 deck: sharp on high-DPI screens
SMALL_FIGURE_PX_PER_PT = 12.0  # inline formulas and other small pictures: crisper text
FIGURE_MAX_PX = 3000
GLYPH_MARGIN = 0.15        # em around a removed glyph's box that its ink may reach (accents, italics)

Box = tuple[float, float, float, float]


def _intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(inner, outer) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _grow(r, d: float) -> Box:
    return r[0] - d, r[1] - d, r[2] + d, r[3] + d


def save_png(img: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path)


def load_png(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


class Eraser:
    """What has been removed from one page so far, and renders of the page without it."""

    def __init__(self, page: Page):
        self.page = page
        self.removed: set[int] = set()
        self.partial: dict[int, list[Box]] = {}   # object -> areas whose pixels come from the render without it
        self.objects = {_addr(po.handle): po for po in page.objects()}
        self.chars: dict[int, list[Char]] = {}    # text object -> its glyphs still drawn
        for ch in page.chars():
            if not ch.synthetic and ch.obj in self.objects:
                self.chars.setdefault(ch.obj, []).append(ch)

    def _remove(self, key: int) -> None:
        self.removed.add(key)
        self.partial.pop(key, None)
        self.chars.pop(key, None)

    def remove_chars(self, hit) -> None:
        """Glyphs for which `hit(char)` is true. A text object whose glyphs all go is switched off."""
        for key, chars in list(self.chars.items()):
            gone = [ch for ch in chars if hit(ch)]
            if len(gone) == len(chars):
                self._remove(key)
            elif gone:
                self.partial.setdefault(key, []).extend(_grow(ch.box, GLYPH_MARGIN * ch.size) for ch in gone)
                self.chars[key] = [ch for ch in chars if not hit(ch)]

    def remove_paths_inside(self, area: Box) -> None:
        for key, po in self.objects.items():
            if po.type == OBJ_PATH and key not in self.removed and _inside(self.page.bounds(po), area):
                self._remove(key)

    def remove_images_in(self, area: Box) -> None:
        """Images and shadings: switched off inside the area, their pixels there removed when they
        reach out of it."""
        for key, po in self.objects.items():
            if po.type not in (OBJ_IMAGE, OBJ_SHADING) or key in self.removed:
                continue
            b = self.page.bounds(po)
            if _inside(b, _grow(area, 0.5)):
                self._remove(key)
            elif _intersects(b, area):
                self.partial.setdefault(key, []).append((max(b[0], area[0]), max(b[1], area[1]),
                                                         min(b[2], area[2]), min(b[3], area[3])))

    def filled_paths(self) -> list[Box]:
        return [d["rect"] for d in self.page.drawings()
                if d["type"] in ("f", "fs") and _addr(d["object"].handle) not in self.removed]

    def render(self, zoom: float, clip: Box | None = None) -> np.ndarray:
        page = self.page
        handles = lambda keys: [self.objects[k] for k in keys]
        page.set_active(handles(self.removed), False)
        try:
            img = page.render(zoom, clip)
            if self.partial:
                page.set_active(handles(self.partial), False)
                without = page.render(zoom, clip)
                page.set_active(handles(self.partial), True)
                x0, y0 = clip[:2] if clip else (0.0, 0.0)
                ox, oy = np.floor(x0 * zoom + 0.001), np.floor(y0 * zoom + 0.001)
                h, w = img.shape[:2]
                mask = np.zeros((h, w), bool)

                def paint(r, value):
                    a0, b0 = max(0, int(np.floor(r[0] * zoom - ox))), max(0, int(np.floor(r[1] * zoom - oy)))
                    a1, b1 = min(w, int(np.ceil(r[2] * zoom - ox))), min(h, int(np.ceil(r[3] * zoom - oy)))
                    if a1 > a0 and b1 > b0:
                        mask[b0:b1, a0:a1] = value

                for areas in self.partial.values():
                    for r in areas:
                        paint(r, True)
                for key in self.partial:  # glyphs that stay, in those areas
                    for ch in self.chars.get(key, []):
                        paint(ch.box, False)
                img[mask] = without[mask]
            return img
        finally:
            page.set_active(handles(self.removed), True)


def _band(bbox: list[float], baseline: float, size: float) -> Box:
    """A thin horizontal band through the glyphs' x-height: it touches this line's
    characters but never the ascenders or descenders of neighbouring lines."""
    x0, _, x1, _ = bbox
    return x0 + 0.2, baseline - 0.45 * size, x1 - 0.2, baseline - 0.2 * size


def crop_figure(eraser: Eraser, bbox: list[float], raw_images: list[dict], path: Path) -> list[int]:
    x0, y0, x1, y1 = bbox
    width, height = x1 - x0, y1 - y0
    zoom = FIGURE_PX_PER_PT if max(width, height) > 60 else SMALL_FIGURE_PX_PER_PT
    # A raster image filling the figure keeps its native resolution (up to the cap).
    for im in raw_images:
        ix0, iy0, ix1, iy1 = im["bbox"]
        if ix1 > ix0 and _inside(im["bbox"], bbox) and (ix1 - ix0) * (iy1 - iy0) > 0.8 * width * height:
            zoom = max(zoom, im["px"][0] / (ix1 - ix0))
    zoom = min(zoom, FIGURE_MAX_PX / max(width, height))
    img = eraser.render(zoom, tuple(bbox))
    save_png(img, path)
    return [img.shape[1], img.shape[0]]


def crop_region(pdf: Path, page: int, bbox: list[float], path: Path, zoom: float) -> None:
    """A picture of a page region (emit's stand-in for an element the Slides API refused)."""
    doc = Document(pdf)
    try:
        save_png(doc[page].render(zoom, tuple(bbox)), path)
    finally:
        doc.close()


SHAPE_SAMPLE_ZOOM = 2.0


def _fill_fraction(page: Page, rect: Box, fill: str, avoid: list[Box]) -> float:
    """Share of sample points inside `rect` (away from text and pictures) that render in `fill`."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    zoom = max(SHAPE_SAMPLE_ZOOM, 8 / max(min(width, height), 0.01))  # hairlines: enough pixels across
    img = page.render(zoom, rect).astype(int)
    h, w = img.shape[:2]
    target = np.array([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
    ys = np.linspace(h * 0.2, h * 0.8 - 1, 12).astype(int)
    xs = np.linspace(w * 0.02, w * 0.98 - 1, 40).astype(int)
    hits = total = 0
    for y in ys:
        for x in xs:
            px, py = rect[0] + x / zoom, rect[1] + y / zoom
            if any(a[0] <= px <= a[2] and a[1] <= py <= a[3] for a in avoid):
                continue
            total += 1
            hits += int(np.abs(img[y, x] - target).max() <= 12)
    return hits / total if total else 0.0


def _probe(el: dict) -> Box:
    """The inside of a shape, clear of rounded corners and anti-aliased edges (thin bars keep
    their middle)."""
    x0, y0, x1, y1 = el["bbox"]
    dx, dy = min(el["radius"] + 1.5, (x1 - x0) / 4), min(1.5, (y1 - y0) / 4)
    return x0 + dx, y0 + dy, x1 - dx, y1 - dy


def verify_and_remove_shapes(original: Page, eraser: Eraser, slide: dict, raw_page: dict) -> None:
    """Keep only shapes whose panel visibly shows its fill colour (beamer draws shadows as
    black rectangles under a soft mask), then remove those panels from the background."""
    avoid = [s["bbox"] for s in raw_page["spans"]] + [i["bbox"] for i in raw_page["images"]] + \
            [e["bbox"] for e in slide["elements"] if e["kind"] == "image"]
    keep = []
    for i, el in enumerate(slide["elements"]):
        if el["kind"] != "shape":
            keep.append(el)
            continue
        probe = _probe(el)
        # Rules drawn on top of this one (a progress bar on its track) hide its colour there.
        above = [e["bbox"] for e in slide["elements"][i + 1:] if e["kind"] == "shape" and e.get("role") == "rule"]
        if probe[2] <= probe[0] or probe[3] <= probe[1] or _fill_fraction(original, probe, el["fill"], avoid + above) < 0.9:
            continue
        keep.append(el)
    slide["elements"] = keep
    remaining = [e for e in keep if e["kind"] == "shape"]
    for margin in (1.5, 5.0):  # a stroked outline reaches past the panel; widen if a panel survived
        if not remaining:
            break
        for el in remaining:
            eraser.remove_paths_inside(_grow(el["bbox"], margin))
            if el.get("shadow"):
                # The shadow's black rectangles (drawn under a soft mask) would render solid
                # once the panels above them are gone.
                x0, y0, x1, y1 = el["bbox"]
                y0 = el["title_bar"][1] if el.get("title_bar") else y0
                size = el["shadow"]["size"]
                eraser.remove_paths_inside((x0 - 1.5, y0 - 1.5, x1 + size + 1.5, y1 + size + 1.5))
        # Still drawn: the panel's path is still on the page. (Its colour showing is no proof: a
        # white block on a white page looks the same with or without its panel.)
        drawn = eraser.filled_paths()
        remaining = [el for el in remaining if any(all(abs(r[k] - el["bbox"][k]) < 0.5 for k in range(4)) for r in drawn)]
    if remaining:  # could not be removed: leave those panels in the background only
        slide["elements"] = [e for e in slide["elements"] if e not in remaining]


def render_backgrounds(pdf: Path, raw: dict, deck: dict, out: Path) -> list[Path]:
    doc = Document(pdf)
    original = Document(pdf)
    raw_pages = {p["index"]: p for p in raw["pages"]}  # by PDF page (overlay steps may be skipped)
    spans = {s["id"]: s for page in raw["pages"] for s in page["spans"]}
    images = {i["id"]: i for page in raw["pages"] for i in page["images"]}
    (out / "background.pdf").unlink(missing_ok=True)  # written by earlier versions
    paths = []
    for slide in deck["slides"]:
        eraser = Eraser(doc[slide["page"]])
        texts = [e for e in slide["elements"] if e["kind"] == "text"]
        figures = [e for e in slide["elements"] if e["kind"] == "image"]

        bands = [_band(s["bbox"], s["origin"][1], s["size"])
                 for s in (spans[sid] for sid in [sid for el in texts for sid in el["spans"]] + slide.get("on_layout", []))]
        if bands:
            eraser.remove_chars(lambda ch: any(_intersects(ch.box, b) for b in bands))
        for x0, y0, x1, y1 in (st for el in texts for st in el.get("strokes", [])):
            eraser.remove_paths_inside((x0 - 1.5, y0 - 1.5, x1 + 1.5, y1 + 1.5))  # bars of fractions converted to text

        for fig in figures:
            path = out / "figures" / f"{fig['id']}.png"
            fig["px"] = crop_figure(eraser, fig["bbox"], raw_pages[slide["page"]]["images"], path)
            fig["file"] = str(path.relative_to(out)).replace("\\", "/")
        # Native tables leave the background the same way pictures do (text and rules), without a crop.
        figures += [e for e in slide["elements"] if e["kind"] in ("table", "diagram")]
        if figures:
            boxes = [tuple(fig["bbox"]) for fig in figures]
            eraser.remove_chars(lambda ch: any(_intersects(ch.box, b) for b in boxes))
            for b in boxes:
                eraser.remove_images_in(b)
                # Stroked paths reach past the figure box by half their width, arrow tips further.
                eraser.remove_paths_inside(_grow(b, 5))

        # Panels last: figure crops taken above still show the panel colour behind them.
        verify_and_remove_shapes(original[slide["page"]], eraser, slide, raw_pages[slide["page"]])

        page = doc[slide["page"]]
        zoom = BACKGROUND_WIDTH_PX / page.width
        path = out / "backgrounds" / f"bg-{slide['page'] + 1:03}.png"
        img = eraser.render(zoom)

        bullets = []
        for el in slide["elements"]:
            for p in el.get("paragraphs", []):
                b = p["bullet"]
                if b and b["kind"] == "image":
                    bullets.append(images[b["image"]]["bbox"])
                elif b and b.get("patch"):  # number drawn on a vector box
                    x0, y0, x1, y1 = b["bbox"]
                    bullets.append([x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5])
        px_per_pt = BACKGROUND_WIDTH_PX / slide["size"][0]
        if bullets:
            patch_rects(img, bullets, px_per_pt)
        paint_out_leftovers(img, slide, px_per_pt)
        save_png(img, path)
        paths.append(path)
        slide["background"] = str(path.relative_to(out)).replace("\\", "/")
        slide["background_color"] = uniform_color(img)
    doc.close()
    original.close()
    return paths


def uniform_color(img: np.ndarray, tolerance: int = 3) -> str | None:
    """The single colour of a background with nothing left on it, else None. Such slides get
    a plain Slides background colour instead of a picture."""
    ref = np.median(img[::17, ::17].reshape(-1, 3), axis=0)
    if np.abs(img.astype(np.int16) - ref.astype(np.int16)).max() > tolerance:
        return None
    return "#" + "".join(f"{int(v):02x}" for v in ref)


def flat_colour(img: np.ndarray, area: Box, px_per_pt: float, ring: int = 4,
                ignore: np.ndarray | None = None) -> np.ndarray | None:
    """The page colour around an area (a thin ring just outside it), if that ring is flat.
    Pixels marked in `ignore` (other converted elements, a neighbour's shadow) don't count."""
    h, w = img.shape[:2]
    a0, b0 = max(0, int(area[0] * px_per_pt) - 2), max(0, int(area[1] * px_per_pt) - 2)
    a1, b1 = min(w, int(np.ceil(area[2] * px_per_pt)) + 2), min(h, int(np.ceil(area[3] * px_per_pt)) + 2)
    parts = [(slice(max(0, b0 - ring), b0), slice(max(0, a0 - ring), a1 + ring)),
             (slice(b1, b1 + ring), slice(max(0, a0 - ring), a1 + ring)),
             (slice(b0, b1), slice(max(0, a0 - ring), a0)),
             (slice(b0, b1), slice(a1, a1 + ring))]
    keep = [~ignore[ys, xs].reshape(-1) if ignore is not None else np.ones(img[ys, xs].shape[0] * img[ys, xs].shape[1], bool)
            for ys, xs in parts]
    pixels = np.concatenate([img[ys, xs].reshape(-1, img.shape[2])[k] for (ys, xs), k in zip(parts, keep)]).astype(int)
    if len(pixels) < 20:
        return None
    colour = np.median(pixels, axis=0)
    return colour if (np.abs(pixels - colour).max(axis=1) <= 6).mean() >= 0.97 else None


def paint_out_leftovers(img: np.ndarray, slide: dict, px_per_pt: float) -> None:
    """Paint out of the background what would otherwise stay behind when a native element
    is moved, with the page colour around it (only where that is flat):

    - pictures: soft-masked pixels and outlines reaching past the figure box (beamer buttons,
      ball icons);
    - blocks whose title bar and body both became shapes: the gradient strip between the two
      (emit lays the body under the title bar) and the shadow pieces (emit gives the body a
      native drop shadow). Where the page is not flat the shadow stays in the background and
      the body gets none."""
    shapes = [e for e in slide["elements"] if e["kind"] == "shape"]
    kept = {e["block"] for e in shapes if e.get("block") is not None and "title_bar" not in e} & \
           {e["block"] for e in shapes if e.get("title_bar")}
    for el in shapes:
        if el.get("block") is not None and el["block"] not in kept:  # one half was not verified
            for key in ("block", "title_bar", "strips"):
                el.pop(key, None)
    jobs = []  # (area whose surroundings give the colour, rects to paint, element)
    for el in shapes:
        rects = list(el.get("strips", [])) + (el["shadow"]["pieces"] if el.get("shadow") else [])
        if rects:
            x0, y0, x1, y1 = el["bbox"]
            area = [x0, el["title_bar"][1] if el.get("title_bar") else y0, x1, y1]
            for r in rects:
                area = [min(area[0], r[0]), min(area[1], r[1]), max(area[2], r[2]), max(area[3], r[3])]
            jobs.append((area, rects, el))
    for el in slide["elements"]:
        if el["kind"] == "image":
            jobs.append((el["bbox"], [el["bbox"]], el))
    # Other elements and everything about to be painted don't count as the page around an area.
    ignore = np.zeros(img.shape[:2], bool)
    covered = [e["bbox"] for e in slide["elements"] if e["kind"] in ("shape", "image", "table", "diagram")]
    covered += [r for _, rects, _ in jobs for r in rects]
    covered += [[x0, e["title_bar"][1], x1, y1] for e in shapes if e.get("title_bar") for x0, _, x1, y1 in [e["bbox"]]]
    for rx0, ry0, rx1, ry1 in covered:
        ignore[max(0, int(ry0 * px_per_pt) - 2):int(np.ceil(ry1 * px_per_pt)) + 2,
               max(0, int(rx0 * px_per_pt) - 2):int(np.ceil(rx1 * px_per_pt)) + 2] = True
    colours = [flat_colour(img, area, px_per_pt, ignore=ignore) for area, _, _ in jobs]  # before any painting
    for (area, rects, el), colour in zip(jobs, colours):
        if colour is None:
            if el["kind"] == "shape":
                el.pop("shadow", None)  # keep the background's shadow; the strip is under the shapes anyway
            continue
        for rx0, ry0, rx1, ry1 in rects:
            c0, d0 = max(0, int(np.floor(rx0 * px_per_pt)) - 1), max(0, int(np.floor(ry0 * px_per_pt)) - 1)
            c1, d1 = int(np.ceil(rx1 * px_per_pt)) + 1, int(np.ceil(ry1 * px_per_pt)) + 1
            img[d0:d1, c0:c1] = colour.astype(np.uint8)


def patch_rects(img: np.ndarray, rects_pt: list[list[float]], px_per_pt: float) -> None:
    """Paint rectangles with the median colour of a thin ring around them.

    Used for ball bullets: removing those images from the PDF is unreliable (themes draw
    them with soft masks shared with shadows), but they sit on flat panel colours."""
    for x0, y0, x1, y1 in rects_pt:
        a0, b0 = int(np.floor(x0 * px_per_pt)) - 1, int(np.floor(y0 * px_per_pt)) - 1
        a1, b1 = int(np.ceil(x1 * px_per_pt)) + 1, int(np.ceil(y1 * px_per_pt)) + 1
        r = 3
        ring = np.concatenate([
            img[max(0, b0 - r):b0, max(0, a0 - r):a1 + r].reshape(-1, 3),
            img[b1:b1 + r, max(0, a0 - r):a1 + r].reshape(-1, 3),
            img[b0:b1, max(0, a0 - r):a0].reshape(-1, 3),
            img[b0:b1, a1:a1 + r].reshape(-1, 3),
        ])
        img[max(0, b0):b1, max(0, a0):a1] = np.median(ring, axis=0).astype(np.uint8)
