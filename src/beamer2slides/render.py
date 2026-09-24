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

import io
from pathlib import Path

import numpy as np
from PIL import Image

from .pdf import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, Char, Document, Page, PdfError

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
        self.removed: set[int] = set()            # object ids
        self.partial: dict[int, list[Box]] = {}   # object -> areas whose pixels come from the render without it
        self.objects = {po.id: po for po in page.objects()}
        self.bounds = page.object_bounds()        # by object id
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
            if po.type == OBJ_PATH and key not in self.removed and _inside(self.bounds[key], area):
                self._remove(key)

    def remove_images_in(self, area: Box) -> None:
        """Images and shadings: switched off inside the area, their pixels there removed when they
        reach out of it."""
        for key, po in self.objects.items():
            if po.type not in (OBJ_IMAGE, OBJ_SHADING) or key in self.removed:
                continue
            b = self.bounds[key]
            if _inside(b, _grow(area, 0.5)):
                self._remove(key)
            elif _intersects(b, area):
                self.partial.setdefault(key, []).append((max(b[0], area[0]), max(b[1], area[1]),
                                                         min(b[2], area[2]), min(b[3], area[3])))

    def filled_paths(self) -> list[Box]:
        return [d["rect"] for d in self.page.drawings()
                if d["type"] in ("f", "fs") and d["object"] not in self.removed]

    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False, hide: list = ()) -> np.ndarray:
        """The page without what was removed (and without the objects in `hide`, ids)."""
        page = self.page
        off = sorted(self.removed) + list(hide)
        page.set_active(off, False)
        try:
            img = page.render(zoom, clip, transparent)
            if self.partial:
                page.set_active(list(self.partial), False)
                without = page.render(zoom, clip, transparent)
                page.set_active(list(self.partial), True)
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
            page.set_active(off, True)


def _band(bbox: list[float], baseline: float, size: float) -> Box:
    """A thin horizontal band through the glyphs' x-height: it touches this line's
    characters but never the ascenders or descenders of neighbouring lines."""
    x0, _, x1, _ = bbox
    return x0 + 0.2, baseline - 0.45 * size, x1 - 0.2, baseline - 0.2 * size


def _span_band(span: dict) -> Box:
    """_band for a raw span, also for text turned by 90° (the x-height lies beside its baseline)."""
    dx, dy = span["dir"]
    if abs(dx) > 0.01 or abs(dy) < 0.99:
        return _band(span["bbox"], span["origin"][1], span["size"])
    _, y0, _, y1 = span["bbox"]
    x, up, size = span["origin"][0], dy < 0, span["size"]
    return (x - 0.45 * size, y0 + 0.2, x - 0.2 * size, y1 - 0.2) if up else (x + 0.2 * size, y0 + 0.2, x + 0.45 * size, y1 - 0.2)


# ------------------------------------------------------------------ embedded images

# A figure region that is nothing but one `\includegraphics` reaches Slides as the image's own
# file instead of a render of the page: the author's 3000 x 2000 photo arrives as 3000 x 2000
# pixels (Google keeps up to ~2046 on the long side), and `pull` can hand the very bytes back.
IMAGE_MAX_PX = 4096        # a rebuilt PNG wider than this: the capped page crop instead
IMAGE_PIXEL_DIFF = 6       # levels: Pillow's and PDFium's JPEG decoders round differently
IMAGE_CHECK_PX_PER_PT = 2.0  # the file is verified against the page at this scale
IMAGE_CHECK_DIFF = 12      # levels of mean difference allowed there


def image_file(page: Page, obj: int) -> tuple[bytes, str, tuple[int, int], str] | None:
    """The file to write for an image object drawn on its own, as (bytes, extension, pixel size,
    route), or None where only a render of the page will do.

    - `raw`: the embedded stream is a plain JPEG (DCTDecode) that Pillow decodes exactly like
      PDFium, so it is the author's file byte for byte;
    - `decoded`: PDFium's own pixels (palette expanded, colour space converted) as a PNG, at
      the image's native resolution.

    Anything that makes the stored pixels mean something else than what the page shows gives
    None: a turned or mirrored matrix, a clip cutting the image, an exotic colour space or bit
    depth, and anything see-through (a soft or stencil mask is not in PDFium's pixels, and a
    constant alpha is not in them either). A y flip is how every PDF draws an image, not one of
    those."""
    im = page.embedded_image(obj)
    if im is None or not im.upright or im.clipped or im.transparent or im.blended or im.bpp < 8 \
            or im.px[0] < 1 or im.px[1] < 1 or im.colorspace in ("unknown", "Pattern"):
        return None
    pixels = im.pixels
    if pixels is None or pixels.shape[1::-1] != im.px:
        return None
    if im.jpeg and im.colorspace in ("DeviceRGB", "DeviceGray", "ICCBased"):
        # The very bytes of the author's file, if they decode to what PDFium draws (a decode
        # array, a CMYK or Lab JPEG or an Adobe inversion would not).
        decoded = _pillow_rgb(im.jpeg)
        if decoded is not None and decoded.shape[:2] == pixels.shape[:2] and \
                np.abs(decoded.astype(int) - pixels[..., :3].astype(int)).mean() <= IMAGE_PIXEL_DIFF:
            return im.jpeg, "jpg", im.px, "raw"
    if max(im.px) > IMAGE_MAX_PX:
        return None
    buf = io.BytesIO()
    Image.fromarray(pixels[..., :3]).save(buf, format="PNG")
    return buf.getvalue(), "png", im.px, "decoded"


def _pillow_rgb(data: bytes) -> np.ndarray | None:
    try:
        return np.array(Image.open(io.BytesIO(data)).convert("RGB"))
    except Exception:
        return None


def sole_image(page: Page, bbox: list[float]) -> int | None:
    """The id of the image object a figure region consists of: it covers the region and nothing
    else is drawn there (classify marks such regions, but the page decides)."""
    found = None
    objects = page.objects()
    for im in page.images():
        if not _intersects(im["bbox"], bbox):
            continue
        if found is not None or objects[im["object"]].type != OBJ_IMAGE or \
                any(abs(im["bbox"][k] - bbox[k]) > 0.5 for k in range(4)):
            return None
        found = im["object"]
    return found


def _looks_like(data: bytes, page: Page, obj: int, bbox: list[float]) -> bool:
    """Does the file, laid on the page without the image, show what the page shows there? The
    last check on PDFium's decode: a palette, colour space or mask read differently would come
    out as another picture."""
    want = page.render(IMAGE_CHECK_PX_PER_PT, tuple(bbox)).astype(int)
    h, w = want.shape[:2]
    if w < 2 or h < 2:
        return True
    page.set_active([obj], False)
    try:
        under = page.render(IMAGE_CHECK_PX_PER_PT, tuple(bbox)).astype(float)
    finally:
        page.set_active([obj], True)
    img = Image.open(io.BytesIO(data)).convert("RGBA").resize((w, h), Image.BILINEAR)
    px = np.array(img).astype(float)
    alpha = px[..., 3:] / 255
    got = px[..., :3] * alpha + under * (1 - alpha)
    return bool(np.abs(got - want).mean() <= IMAGE_CHECK_DIFF)


def embedded_picture(eraser: Eraser, fig: dict, path: Path) -> Path | None:
    """The figure's picture written from the image object's own data (`image_file`), or None
    where the page has to be rendered after all. Sets `px` and `picture` on the element."""
    obj = sole_image(eraser.page, fig["bbox"])
    if obj is None:
        return None
    chosen = image_file(eraser.page, obj)
    if chosen is None:
        return None
    data, ext, px, route = chosen
    if not _looks_like(data, eraser.page, obj, fig["bbox"]):
        return None
    path = path.with_suffix("." + ext)
    save_bytes(data, path)
    fig["px"], fig["picture"] = [px[0], px[1]], route
    return path


def save_bytes(data: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def crop_figure(eraser: Eraser, bbox: list[float], raw_images: list[dict], path: Path,
                transparent: bool = False) -> list[int]:
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
    if transparent:
        img = clear_ground(eraser, bbox, zoom, img)
    save_png(img, path)
    return [img.shape[1], img.shape[0]]


GROUND_FLAT = 6        # levels: the page under a picture counts as one colour
GROUND_MATCH = 16      # levels: the transparent crop laid on that colour shows the opaque crop


def clear_ground(eraser: Eraser, bbox: list[float], zoom: float, opaque: np.ndarray) -> np.ndarray:
    """A picture anchored to text (inline formula, icon, number ball) on a transparent ground, so it
    shows the slide under it when the background colour changes or it is moved onto a shape.
    What stays in the background (paths reaching out of the box as render_backgrounds leaves them,
    images and shadings not inside it) is left out. Kept only where the page under the picture is
    flat and the result laid on that colour shows the opaque crop; else the opaque crop."""
    bounds = eraser.bounds
    ground = [key for key, po in eraser.objects.items() if key not in eraser.removed and (
        (po.type == OBJ_PATH and not _inside(bounds[key], _grow(bbox, 5))) or
        (po.type in (OBJ_IMAGE, OBJ_SHADING) and not _inside(bounds[key], _grow(bbox, 0.5))))]
    rgba = eraser.render(zoom, tuple(bbox), transparent=True, hide=ground)
    if rgba.shape[:2] != opaque.shape[:2]:
        return opaque
    clear = rgba[..., 3] == 0
    if clear.sum() < 20:
        return opaque
    page_px = opaque[clear].astype(int)
    colour = np.median(page_px, axis=0)
    if (np.abs(page_px - colour).max(axis=1) > GROUND_FLAT).mean() > 0.002:
        return opaque
    rgba = unblend_rim(rgba, clear, colour, max(1, round(GROUND_RIM * zoom)))
    alpha = rgba[..., 3:].astype(float) / 255
    laid = rgba[..., :3] * alpha + colour * (1 - alpha)
    if (np.abs(laid - opaque).max(axis=2) > GROUND_MATCH).mean() > 0.002:
        return opaque
    return rgba


GROUND_RIM = 0.5  # pt


def unblend_rim(rgba: np.ndarray, clear: np.ndarray, ground: np.ndarray, width: int) -> np.ndarray:
    """Opaque pixels near the transparent ground that fade into the page colour (a ball's shading
    ends in it, and clips have no soft edge on a transparent bitmap) become that much transparent
    (colour to alpha), so no light fringe shows on another background."""
    near = clear.copy()
    for _ in range(width):
        grown = near.copy()
        grown[1:] |= near[:-1]
        grown[:-1] |= near[1:]
        grown[:, 1:] |= near[:, :-1]
        grown[:, :-1] |= near[:, 1:]
        near = grown
    rim = near & (rgba[..., 3] == 255)
    if not rim.any():
        return rgba
    px = rgba[rim][:, :3].astype(float)
    up = np.where(px > ground, (px - ground) / np.maximum(255 - ground, 1), 0)
    down = np.where(px < ground, (ground - px) / np.maximum(ground, 1), 0)
    a = np.clip(np.maximum(up, down).max(axis=1), 0, 1)[:, None]
    ink = np.where(a > 0, ground + (px - ground) / np.maximum(a, 1e-6), 0)
    out = rgba.copy()
    out[rim] = np.concatenate([np.clip(np.rint(ink), 0, 255), np.rint(a * 255)], axis=1).astype(np.uint8)
    return out


def crop_overlay(eraser: Eraser, fig: dict, labels: list[dict], path: Path) -> list[int]:
    """A graphic drawn over text (classify.overlay): only its own drawings and labels, on a
    transparent ground, so neither the page nor text left in the background under it comes
    along. They leave the background right away: no other crop shows them either."""
    page = eraser.page
    objects = [d["object"] for d in page.drawings()]  # in the order extract numbered them
    paths = {objects[int(i.rsplit("d", 1)[1])] for i in fig["drawings"]}
    bands = [_span_band(s) for s in labels]
    hit = lambda ch: any(_intersects(ch.box, b) for b in bands)
    texts = {key for key, chars in eraser.chars.items() if any(map(hit, chars))}
    off = [key for key, po in eraser.objects.items() if key not in paths | texts and po.type != OBJ_FORM]
    x0, y0, x1, y1 = fig["bbox"]
    zoom = min(FIGURE_PX_PER_PT if max(x1 - x0, y1 - y0) > 60 else SMALL_FIGURE_PX_PER_PT,
               FIGURE_MAX_PX / max(x1 - x0, y1 - y0))
    page.set_active(off, False)
    try:
        img = page.render(zoom, tuple(fig["bbox"]), transparent=True)
    finally:
        page.set_active(off, True)
    for key in paths:
        eraser._remove(key)
    eraser.remove_chars(hit)
    save_png(img, path)
    return [img.shape[1], img.shape[0]]


INK_ZOOM = 4.0      # px per pt at which a formula's ink is measured
INK_REACH = 3.0     # em beyond its box that a formula glyph's ink is looked for (a \left( of three rows)
INK_PAD = 0.75      # pt around the ink found, for anti-aliasing


def grow_to_ink(eraser: Eraser, bbox: list[float], members: list[dict]) -> list[float]:
    """A formula picture's box grown to the ink of its own glyphs. Glyph boxes are font boxes
    (ascender to descender), not ink: a display \\sum or \\int from CMEX hangs from its origin
    and reaches an em past its box, a \\left( of a matrix three rows, and a picture of the box
    cut the sign to a chevron while its switched-off glyph left the background too. The ink is
    what the text objects holding only the formula's glyphs paint: the page rendered around the
    box with and without them."""
    if not members:
        return bbox
    rects = [s["bbox"] for s in members]
    mine = lambda ch: any(r[0] - 0.1 <= (ch.box[0] + ch.box[2]) / 2 <= r[2] + 0.1 and
                          r[1] - 0.1 <= (ch.box[1] + ch.box[3]) / 2 <= r[3] + 0.1 for r in rects)
    own = [key for key, chars in eraser.chars.items() if chars and all(map(mine, chars))]
    if not own:
        return bbox
    reach = INK_REACH * max(s["size"] for s in members)
    page = eraser.page
    area = (max(0.0, bbox[0] - reach), max(0.0, bbox[1] - reach),
            min(page.width, bbox[2] + reach), min(page.height, bbox[3] + reach))
    if area[2] <= area[0] or area[3] <= area[1]:
        return bbox
    drawn = eraser.render(INK_ZOOM, area).astype(np.int16)
    bare = eraser.render(INK_ZOOM, area, hide=own).astype(np.int16)
    if drawn.shape != bare.shape:
        return bbox
    ink = np.abs(drawn - bare).max(axis=2) > 24
    if not ink.any():
        return bbox
    ys, xs = np.nonzero(ink)
    ox, oy = np.floor(area[0] * INK_ZOOM + 0.001), np.floor(area[1] * INK_ZOOM + 0.001)
    box = ((xs.min() + ox) / INK_ZOOM - INK_PAD, (ys.min() + oy) / INK_ZOOM - INK_PAD,
           (xs.max() + 1 + ox) / INK_ZOOM + INK_PAD, (ys.max() + 1 + oy) / INK_ZOOM + INK_PAD)
    return [round(float(v), 2) for v in (min(bbox[0], box[0]), min(bbox[1], box[1]),
                                  max(bbox[2], box[2]), max(bbox[3], box[3]))]


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
    keep_visible_shapes(original, slide, raw_page)
    remaining = [e for e in slide["elements"] if e["kind"] == "shape"]
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


def keep_visible_shapes(original: Page, slide: dict, raw_page: dict) -> None:
    """Drop shape candidates whose fill doesn't show on the page."""
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
        # (a translucent highlight shows its colour mixed with the page: not a shadow's black box)
        if probe[2] <= probe[0] or probe[3] <= probe[1] or \
                (not el.get("opacity") and _fill_fraction(original, probe, el["fill"], avoid + above) < 0.9):
            continue
        keep.append(el)
    slide["elements"] = keep


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

        bands = [_span_band(s)
                 for s in (spans[sid] for sid in [sid for el in texts for sid in el["spans"]] + slide.get("on_layout", []))]
        if bands:
            eraser.remove_chars(lambda ch: any(_intersects(ch.box, b) for b in bands))
        for x0, y0, x1, y1 in (st for el in texts for st in el.get("strokes", [])):
            eraser.remove_paths_inside((x0 - 1.5, y0 - 1.5, x1 + 1.5, y1 + 1.5))  # bars of fractions converted to text

        # Graphics drawn over text first: the other crops must not show them.
        for fig in sorted(figures, key=lambda f: not f.get("overlay")):
            path = out / "figures" / f"{fig['id']}.png"
            if fig.get("overlay"):
                fig["px"] = crop_overlay(eraser, fig, [spans[sid] for sid in fig["spans"]], path)
            else:
                # A region that is one `\includegraphics` keeps the embedded file itself.
                own = None if fig.get("anchor") or not fig.get("image") else embedded_picture(eraser, fig, path)
                path = own or path
                if own is None:
                    if fig.get("role") == "math":
                        fig["bbox"] = grow_to_ink(eraser, fig["bbox"], [spans[sid] for sid in fig["spans"] if sid in spans])
                    fig["px"] = crop_figure(eraser, fig["bbox"], raw_pages[slide["page"]]["images"], path,
                                            transparent=bool(fig.get("anchor")))
            fig["file"] = str(path.relative_to(out)).replace("\\", "/")
        # Native tables leave the background the same way pictures do (text and rules), without a crop.
        figures = [f for f in figures if not f.get("overlay")]
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

        px_per_pt = BACKGROUND_WIDTH_PX / slide["size"][0]
        bullets = []
        for el in slide["elements"]:
            for p in el.get("paragraphs", []):
                b = p["bullet"]
                if b and b["kind"] == "glyph" and "ink" not in b:
                    measured = glyph_ink(original[slide["page"]], b["bbox"])
                    if measured:  # (emit sizes and shapes the Slides bullet by it)
                        b["ink"], b["fill"] = measured
                if b and b["kind"] == "image":
                    bullets.append(images[b["image"]]["bbox"])
                    b.setdefault("color", ink_colour(img, b["bbox"], px_per_pt))  # the Slides bullet's colour
                elif b and b.get("patch"):  # number drawn on a vector box
                    x0, y0, x1, y1 = b["bbox"]
                    bullets.append([x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5])
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


DECORATION_MIN = 0.002    # share of the page: less is no theme decoration
DECORATION_AGREE = 0.9    # share of the decoration a background must show to be decorated alike
DECORATION_TOLERANCE = 2  # levels


def page_ground(img: np.ndarray) -> np.ndarray:
    """The most common colour of a background (the page ground)."""
    px = img[::4, ::4].reshape(-1, 3).astype(np.int64)
    packed = (px[:, 0] << 16) | (px[:, 1] << 8) | px[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    v = values[counts.argmax()]
    return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255])


def theme_decoration(images, ground: np.ndarray) -> tuple[np.ndarray | None, list[bool], bool]:
    """The theme decoration a layout can carry for backgrounds that share it (`images`: distinct
    backgrounds, the most used first): an RGBA picture of the first one, opaque where it differs
    from the ground and every background taking it shows the same pixels, transparent elsewhere.
    Drawn over those backgrounds it changes nothing, and over another ground colour it keeps the
    bars and lines. Returns (picture or None, per image whether it shows the decoration, whether
    the first image is exactly the ground plus the picture)."""
    images = iter(images)
    first = next(images)
    ref = first.astype(np.int16)
    mask = np.abs(ref - ground).max(axis=2) > DECORATION_TOLERANCE
    total = int(mask.sum())
    if total < DECORATION_MIN * mask.size:
        return None, [False] * (1 + sum(1 for _ in images)), False
    inside = [True]
    for img in images:
        same = np.abs(img.astype(np.int16) - ref).max(axis=2) <= DECORATION_TOLERANCE if img.shape == ref.shape else None
        ok = same is not None and (same & mask).sum() >= DECORATION_AGREE * mask.sum()
        inside.append(bool(ok))
        if ok:
            mask &= same
    # (binary alpha: over a background showing the same pixels, any soft edge would change them)
    return np.dstack([first, np.where(mask, 255, 0).astype(np.uint8)]), inside, int(mask.sum()) == total


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
        if el["kind"] == "image" and not el.get("overlay"):  # (an overlay's box holds the page under it)
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


def ink_colour(img: np.ndarray, rect_pt: list[float], px_per_pt: float) -> str | None:
    """Typical colour of what is drawn in a small area (a shaded ball bullet): the median of
    the pixels that differ from what is behind it (`ring_background`: on a picture's edge the
    picture's pixels in the box are not the ball's), or from the page around it."""
    x0, y0, x1, y1 = rect_pt
    a0, b0 = max(0, int(np.floor(x0 * px_per_pt))), max(0, int(np.floor(y0 * px_per_pt)))
    a1, b1 = int(np.ceil(x1 * px_per_pt)), int(np.ceil(y1 * px_per_pt))
    area = img[b0:b1, a0:a1, :3].reshape(-1, 3).astype(int)
    ring = np.concatenate([img[max(0, b0 - 3):b0, a0:a1, :3].reshape(-1, 3), img[b1:b1 + 3, a0:a1, :3].reshape(-1, 3)]).astype(int)
    if not len(area) or not len(ring):
        return None
    behind = ring_background(img, a0, b0, a1, b1)
    behind = np.median(ring, axis=0) if behind is None else behind.reshape(-1, 3)
    ink = area[np.abs(area - behind).sum(axis=1) > 60]
    if len(ink) < 0.2 * len(area):
        return None
    return "#" + "".join(f"{int(v):02x}" for v in np.median(ink, axis=0))


GLYPH_INK_ZOOM = 16  # px per pt: a 0.15 em dot of 11 pt is 26 px tall


def glyph_ink(page: Page, box: list[float]) -> tuple[list[float], float] | None:
    """The ink of a glyph bullet, from the page as the PDF draws it: its box in pt and the share
    of that box it fills (a disc 0.79, a square 1, a triangle 0.54). A glyph's box is its font's
    (1-1.2 em tall whatever the glyph): Fira Sans Light's bullet is a 0.15 em dot, LM Sans's
    \\textbullet a 0.29 em square, where Slides' disc preset is 0.41 em of its size. The blob
    around the most inked row and column, so a neighbour's descender at the box edge is not
    counted. None when the backend does not draw or nothing is there."""
    x0, y0, x1, y1 = box
    clip = (x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
    try:
        img = page.render(GLYPH_INK_ZOOM, clip=clip)
    except PdfError:
        return None
    px = img[..., :3].astype(int)
    border = np.concatenate([px[0], px[-1], px[:, 0], px[:, -1]])
    distance = np.abs(px - np.median(border, axis=0)).sum(axis=2)
    if distance.max() <= 60:
        return None
    ink = distance > max(60, distance.max() / 2)

    def blob(profile: np.ndarray) -> tuple[int, int]:
        a = b = int(profile.argmax())
        while a > 0 and profile[a - 1]:
            a -= 1
        while b + 1 < len(profile) and profile[b + 1]:
            b += 1
        return a, b + 1

    r0, r1 = blob(ink.sum(axis=1))
    c0, c1 = blob(ink[r0:r1].sum(axis=0))
    fill = float(ink[r0:r1, c0:c1].mean())
    ix0 = np.floor(clip[0] * GLYPH_INK_ZOOM + 0.001) / GLYPH_INK_ZOOM  # (pixel_bounds rounds outwards)
    iy0 = np.floor(clip[1] * GLYPH_INK_ZOOM + 0.001) / GLYPH_INK_ZOOM
    return [round(float(ix0 + c0 / GLYPH_INK_ZOOM), 2), round(float(iy0 + r0 / GLYPH_INK_ZOOM), 2),
            round(float(ix0 + c1 / GLYPH_INK_ZOOM), 2), round(float(iy0 + r1 / GLYPH_INK_ZOOM), 2)], round(fill, 2)


def ring_background(img: np.ndarray, a0: int, b0: int, a1: int, b1: int, r: int = 3) -> np.ndarray | None:
    """What is behind a small rectangle of pixels [b0:b1, a0:a1], from a ring `r` px wide around
    it: each column running from the strip above to the strip below, each row from the strip on
    the left to the one on the right, the two weighted by how well their ends agree. A bullet
    on a picture's edge or on a gradient (a colorbar) takes that edge and gradient through it;
    a flat median painted a square of the mixed colour there. (h, w, 3) floats, or None when
    the ring is off the image."""
    h, w = b1 - b0, a1 - a0
    if a0 < 0 or b0 < 0 or a1 > img.shape[1] or b1 > img.shape[0] or h <= 0 or w <= 0:
        return None
    px = img[..., :3].astype(float)
    t = (np.arange(h) + 0.5) / h
    s = (np.arange(w) + 0.5) / w
    guesses, errors = [], []
    if b0 >= r and b1 + r <= img.shape[0]:
        top, bottom = np.median(px[b0 - r:b0, a0:a1], axis=0), np.median(px[b1:b1 + r, a0:a1], axis=0)
        guesses.append(top[None] * (1 - t)[:, None, None] + bottom[None] * t[:, None, None])
        errors.append(np.abs(top - bottom).sum(axis=1).mean())
    if a0 >= r and a1 + r <= img.shape[1]:
        left, right = np.median(px[b0:b1, a0 - r:a0], axis=1), np.median(px[b0:b1, a1:a1 + r], axis=1)
        guesses.append(left[:, None] * (1 - s)[None, :, None] + right[:, None] * s[None, :, None])
        errors.append(np.abs(left - right).sum(axis=1).mean())
    if not guesses:
        return None
    weights = [1.0 / (e + 1.0) ** 2 for e in errors]  # (squared: an edge's 5% ghost showed)
    return sum(g * wt for g, wt in zip(guesses, weights)) / sum(weights)


def patch_rects(img: np.ndarray, rects_pt: list[list[float]], px_per_pt: float) -> None:
    """Paint rectangles with what the ring around them says is behind (`ring_background`), or
    its median colour at the image's edge.

    Used for ball bullets: removing those images from the PDF is unreliable (themes draw
    them with soft masks shared with shadows). Most sit on flat panel colours, some on a
    picture's edge or a gradient."""
    for x0, y0, x1, y1 in rects_pt:
        a0, b0 = int(np.floor(x0 * px_per_pt)) - 1, int(np.floor(y0 * px_per_pt)) - 1
        a1, b1 = int(np.ceil(x1 * px_per_pt)) + 1, int(np.ceil(y1 * px_per_pt)) + 1
        r = 3
        behind = ring_background(img, a0, b0, a1, b1, r)
        if behind is not None:
            img[b0:b1, a0:a1, :3] = np.clip(np.round(behind), 0, 255).astype(np.uint8)
            continue
        ring = np.concatenate([
            img[max(0, b0 - r):b0, max(0, a0 - r):a1 + r].reshape(-1, 3),
            img[b1:b1 + r, max(0, a0 - r):a1 + r].reshape(-1, 3),
            img[b0:b1, max(0, a0 - r):a0].reshape(-1, 3),
            img[b0:b1, a1:a1 + r].reshape(-1, 3),
        ])
        img[max(0, b0):b1, max(0, a0):a1] = np.median(ring, axis=0).astype(np.uint8)
