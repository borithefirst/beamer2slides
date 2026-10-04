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
import os
from collections import deque
from collections.abc import Callable, Collection, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from .arrays import Floats, Ints, Mask, Pixels, RGB, RGBA
from .json_types import Json, JsonArray, JsonObject, JsonShapeError, as_array, as_int, as_object, as_objects, as_str
from .pdf import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, Char, Document, Page, PdfError
from .raw_types import RawDoc, RawImage, RawPage, RawSpan
from .typing_compat import assert_never

BACKGROUND_WIDTH_PX = 2000
FIGURE_PX_PER_PT = 8.0     # ~ 4 px per Slides point on a 4:3 deck: sharp on high-DPI screens
SMALL_FIGURE_PX_PER_PT = 12.0  # inline formulas and other small pictures: crisper text
FIGURE_MAX_PX = 3000
# A PDF viewer draws no stroke thinner than one of its pixels; a picture rendered at 8 px/pt and
# shown at a slide's size has its 1-px hairlines averaged to a pale, broken grey (Slides' 1600-px
# thumbnail: the same ink as a box filter, 40% short of the PDF's on network maps and trees). So
# every picture for Slides draws each stroke at least one pixel of the slide shown this wide.
HAIRLINE_SLIDE_PX = 1600
GLYPH_MARGIN = 0.15        # em around a removed glyph's box that its ink may reach (accents, italics)
PAGE_GROUND = 0.95         # share of the page an image or shading covers to be its ground (classify's too)
RAISED_SLACK = 0.5         # pt a glyph raised off its span's baseline may reach past the span's box

Box = tuple[float, float, float, float]
GlyphTest = Callable[[Char], bool]


def _intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside(inner: Sequence[float], outer: Sequence[float]) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _box(b: Sequence[float]) -> Box:
    """A JSON box, [x0, y0, x1, y1], as a Box."""
    return b[0], b[1], b[2], b[3]


def _grow(r: Sequence[float], d: float) -> Box:
    return r[0] - d, r[1] - d, r[2] + d, r[3] + d


# ------------------------------------------------------------------ deck.json as render reads it
# The deck render is handed is JSON (deck.json, or classify's deck as `ir.deck_json` writes it),
# changed in place into rendered.json; its numbers are read as they are (an int stays one).

def _number(value: Json, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JsonShapeError(f"{where}: a number was expected, found {type(value).__name__}")
    return value


def _numbers(value: Json, where: str) -> list[float]:
    return [_number(v, where) for v in as_array(value, where)]


def _json_box(value: Json, where: str) -> Box:
    x = _numbers(value, where)
    if len(x) != 4:
        raise JsonShapeError(f"{where}: a box of 4 numbers was expected, found {len(x)}")
    return x[0], x[1], x[2], x[3]


def _array_at(obj: JsonObject, key: str) -> JsonArray:
    """`obj[key]`, an array, or none where it is missing."""
    value = obj.get(key)
    return [] if value is None else as_array(value, key)


def _ids(obj: JsonObject, key: str) -> list[str]:
    """`obj[key]`, a list of ids (span ids), or none where it is missing."""
    return [as_str(v, key) for v in _array_at(obj, key)]


def _json_numbers(values: Sequence[float]) -> list[Json]:
    out: list[Json] = []
    out.extend(values)
    return out


def save_png(img: Pixels, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path)


def load_png(path: Path) -> RGB:
    return np.array(Image.open(path).convert("RGB"))


# PNG encoding (zlib, outside the GIL) took a quarter of the render stage on the main thread: the
# backgrounds and crops are written by threads of their own while the next page is worked on.
# Nothing they do touches the PDF (PDFium stays on the main thread); the bytes are the same.
PNG_WRITERS = max(0, min(4, (os.cpu_count() or 1) - 1))  # 0: written where they are made
PNG_IN_FLIGHT = 8   # pictures waiting to be written at most (a page is 9 MB of pixels)


class PngWriter:
    """`save_png` (or another write, `submit`) on worker threads. A picture handed over is never
    changed afterwards. `finish` waits for every write and raises the first that failed; `close`
    stops the threads."""

    def __init__(self, workers: int) -> None:
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="b2s-png") if workers else None
        self.pending: deque[Future[None]] = deque()

    def save(self, img: Pixels, path: Path) -> None:
        # (save_png as the module holds it now: checks.convert_locally keeps pictures in memory)
        self.submit(lambda: save_png(img, path))

    def submit(self, job: Callable[[], None]) -> None:
        if self.pool is None:
            job()
            return
        while len(self.pending) >= PNG_IN_FLIGHT:
            self.pending.popleft().result()
        self.pending.append(self.pool.submit(job))

    def finish(self) -> None:
        while self.pending:
            self.pending.popleft().result()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)


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

    def remove_chars(self, hit: GlyphTest) -> None:
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

    def remove_images_in(self, area: Box, keep: Collection[int]) -> None:
        """Images and shadings: switched off inside the area, their pixels there removed when they
        reach out of it. What is in `keep` (ids) stays whole, and so does the page's own ground
        (`PAGE_GROUND` of it or more: a background canvas's shading or picture, no figure's): cut,
        it left a white box on the page under a native table or behind a picture."""
        page_area = self.page.width * self.page.height
        for key, po in self.objects.items():
            if po.type not in (OBJ_IMAGE, OBJ_SHADING) or key in self.removed or key in keep:
                continue
            b = self.bounds[key]
            if (b[2] - b[0]) * (b[3] - b[1]) >= PAGE_GROUND * page_area:
                continue
            if _inside(b, _grow(area, 0.5)):
                self._remove(key)
            elif _intersects(b, area):
                self.partial.setdefault(key, []).append((max(b[0], area[0]), max(b[1], area[1]),
                                                         min(b[2], area[2]), min(b[3], area[3])))

    def filled_paths(self) -> list[Box]:
        return [d["rect"] for d in self.page.drawings()
                if d["type"] in ("f", "fs") and d["object"] not in self.removed]

    def render(self, zoom: float, clip: Box | None, transparent: bool, hide: Sequence[int]) -> Pixels:
        """The page without what was removed (and without the objects in `hide`, ids), the whole
        page where `clip` is None."""
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

                def paint(r: Sequence[float], value: bool) -> None:
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


def _same_dir(a: Sequence[float], b: Sequence[float]) -> bool:
    return abs(a[0] - b[0]) <= 0.05 and abs(a[1] - b[1]) <= 0.05


def owned_by(spans: list[RawSpan]) -> GlyphTest:
    """A test of whether a drawn glyph is one of these raw spans' own: same font and size, on the
    span's baseline, between its start and its end along the baseline. (Glyph boxes say nothing
    of the kind: a hanging radical's box lies in the line above.) A glyph of the span's own text
    raised off its baseline is the span's when its box lies inside the span's: txfonts set a
    radical sign from an origin 0.65 em above the formula's baseline (`\\pm\\sqrt{z_0}`), and the
    sign went with the words of the line above, in neither the hole's crop nor the background."""
    by_font: dict[tuple[str, float], list[RawSpan]] = {}
    for s in spans:
        by_font.setdefault((s["font"], round(s["size"], 2)), []).append(s)

    def test(ch: Char) -> bool:
        for s in by_font.get((ch.font, round(ch.size, 2)), ()):
            if not _same_dir(ch.dir, s["dir"]):
                continue
            dx, dy = s["dir"]
            rx, ry = ch.origin[0] - s["origin"][0], ch.origin[1] - s["origin"][1]
            if abs(rx * dy - ry * dx) > 0.05 * s["size"]:
                if ch.c and ch.c in s["text"] and _inside(ch.box, _grow(s["bbox"], RAISED_SLACK)):
                    return True
                continue  # another baseline
            x0, y0, x1, y1 = s["bbox"]
            reach = max((cx - s["origin"][0]) * dx + (cy - s["origin"][1]) * dy for cx in (x0, x1) for cy in (y0, y1))
            if -0.1 <= rx * dx + ry * dy <= reach + 0.1:
                return True
        return False

    return test


def others_glyphs(eraser: "Eraser", figures: list[JsonObject], spans: dict[str, RawSpan]) -> dict[str, list[int]]:
    """Per picture, the text objects another picture owns, to leave out of its crop when one of
    the two moves with its words (a hole): a display formula's crop reaching into the line above
    showed the hanging tail of that line's inline integral at the PDF place, a piece floating
    under the hole's own integral once Slides set the words elsewhere (r1_math_v2 s5)."""
    owners = {as_str(fig["id"], "id"): owned_by([spans[sid] for sid in _ids(fig, "spans") if sid in spans])
              for fig in figures if fig.get("spans") and not fig.get("overlay")}
    if len(owners) < 2 or not any(fig.get("anchor") for fig in figures):
        return {}
    anchored = {as_str(fig["id"], "id") for fig in figures if fig.get("anchor")}
    out: dict[str, list[int]] = {}
    for key, chars in eraser.chars.items():
        if not chars:
            continue
        holders = {fid for fid, test in owners.items() if any(map(test, chars))}
        if len(holders) != 1:
            continue
        (owner,) = holders
        if not all(map(owners[owner], chars)):
            continue
        for fig in figures:
            fid = as_str(fig["id"], "id")
            if fid != owner and (fid in anchored or owner in anchored) and \
                    any(_intersects(ch.box, _grow(_json_box(fig["bbox"], "bbox"), INK_REACH * ch.size)) for ch in chars):
                out.setdefault(fid, []).append(key)
    return out


def _span_band(span: RawSpan) -> Box:
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


@dataclass(frozen=True, kw_only=True)
class ImageChoice:
    """`image_file`'s answer, with the file as it decodes (`image`, loaded once: `_shows` lays
    it on the page without decoding the file a second time)."""
    data: bytes
    ext: str
    px: tuple[int, int]
    route: str
    image: Image.Image


def image_file(page: Page, obj: int) -> tuple[bytes, str, tuple[int, int], str] | None:
    """The file to write for an image object drawn on its own, as (bytes, extension, pixel size,
    route), or None where only a render of the page will do (`image_choice`)."""
    chosen = image_choice(page, obj)
    return None if chosen is None else (chosen.data, chosen.ext, chosen.px, chosen.route)


def image_choice(page: Page, obj: int) -> ImageChoice | None:
    """The file to write for an image object drawn on its own, or None where only a render of
    the page will do. Its route:

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
        opened = _pillow_rgb(im.jpeg)
        # (the mean difference in bytes: a photo widened to int64 twice took 0.4 s)
        if opened is not None and opened[1].shape[:2] == pixels.shape[:2] and \
                _differences(opened[1], pixels[..., :3]).mean() <= IMAGE_PIXEL_DIFF:
            return ImageChoice(data=im.jpeg, ext="jpg", px=im.px, route="raw", image=opened[0])
    if max(im.px) > IMAGE_MAX_PX:
        return None
    buf = io.BytesIO()
    image = Image.fromarray(pixels[..., :3])
    image.save(buf, format="PNG")
    # (the PNG decodes to these very pixels)
    return ImageChoice(data=buf.getvalue(), ext="png", px=im.px, route="decoded", image=image)


def _differences(a: Pixels, b: Pixels) -> Pixels:
    """|a - b| per byte of two pictures of one shape, without widening them."""
    return np.maximum(a, b) - np.minimum(a, b)


def _pillow_rgb(data: bytes) -> tuple[Image.Image, RGB] | None:
    """The file as Pillow decodes it (loaded), and its RGB pixels; None where it does not."""
    try:
        image = Image.open(io.BytesIO(data))
        return image, np.array(image.convert("RGB"))
    except Exception:
        return None


def sole_image(page: Page, bbox: Sequence[float]) -> int | None:
    """The id of the image object a figure region consists of: it covers the region and nothing
    else is drawn there (classify marks such regions, but the page decides)."""
    found: int | None = None
    objects = page.objects()
    for im in page.images():
        if not _intersects(im["bbox"], bbox):
            continue
        if found is not None or objects[im["object"]].type != OBJ_IMAGE or \
                any(abs(im["bbox"][k] - bbox[k]) > 0.5 for k in range(4)):
            return None
        found = im["object"]
    return found


def _looks_like(data: bytes, page: Page, obj: int, bbox: Sequence[float]) -> bool:
    """Does the file, laid on the page without the image, show what the page shows there? The
    last check on PDFium's decode: a palette, colour space or mask read differently would come
    out as another picture."""
    return _shows(Image.open(io.BytesIO(data)), page, obj, bbox)


def _shows(image: Image.Image, page: Page, obj: int, bbox: Sequence[float]) -> bool:
    """`_looks_like` for the file as it decodes."""
    want = page.render(IMAGE_CHECK_PX_PER_PT, _box(bbox), transparent=False).astype(int)
    h, w = want.shape[:2]
    if w < 2 or h < 2:
        return True
    img = image.convert("RGBA").resize((w, h), Image.Resampling.BILINEAR)
    px = np.array(img).astype(float)
    alpha = px[..., 3:] / 255
    if (alpha == 1).all():
        # Opaque (a JPEG, a PNG of RGB pixels: every file image_file writes) shows nothing of the
        # page under it, which is not rendered: px * 1 + under * 0 is px to the last bit.
        got = px[..., :3]
    else:
        page.set_active([obj], False)
        try:
            under = page.render(IMAGE_CHECK_PX_PER_PT, _box(bbox), transparent=False).astype(float)
        finally:
            page.set_active([obj], True)
        got = px[..., :3] * alpha + under * (1 - alpha)
    return bool(np.abs(got - want).mean() <= IMAGE_CHECK_DIFF)


def embedded_picture(eraser: Eraser, fig: JsonObject, path: Path) -> Path | None:
    """The figure's picture written from the image object's own data (`image_file`), or None
    where the page has to be rendered after all. Sets `px` and `picture` on the element."""
    bbox = _numbers(fig["bbox"], "bbox")
    obj = sole_image(eraser.page, bbox)
    if obj is None:
        return None
    chosen = image_choice(eraser.page, obj)
    if chosen is None or not _shows(chosen.image, eraser.page, obj, bbox):
        return None
    path = path.with_suffix("." + chosen.ext)
    save_bytes(chosen.data, path)
    fig["px"] = [chosen.px[0], chosen.px[1]]
    fig["picture"] = chosen.route
    return path


def save_bytes(data: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


CropGround = Literal["opaque", "anchored", "in_place"]
"""How a figure's crop takes the page under it: `opaque` painted in; `anchored` transparent for
a picture Slides moves with its words (`clear_ground`); `in_place` transparent for one that stays
over the slide's own background but covers words the layout carries (`clear_in_place`)."""


def crop_figure(eraser: Eraser, bbox: Sequence[float], raw_images: list[RawImage], path: Path,
                ground: CropGround, hide: Sequence[int], writer: PngWriter) -> tuple[int, int]:
    """The picture of a figure region on the page as erased so far, at a resolution for its
    size, handed to `writer`; its (width, height) in pixels."""
    x0, y0, x1, y1 = bbox
    width, height = x1 - x0, y1 - y0
    zoom = FIGURE_PX_PER_PT if max(width, height) > 60 else SMALL_FIGURE_PX_PER_PT
    # A raster image filling the figure keeps its native resolution (up to the cap).
    for im in raw_images:
        ix0, iy0, ix1, iy1 = im["bbox"]
        if ix1 > ix0 and _inside(im["bbox"], bbox) and (ix1 - ix0) * (iy1 - iy0) > 0.8 * width * height:
            zoom = max(zoom, im["px"][0] / (ix1 - ix0))
    zoom = min(zoom, FIGURE_MAX_PX / max(width, height))
    img = eraser.render(zoom, _box(bbox), False, hide)
    if ground == "anchored":
        img = clear_ground(eraser, bbox, zoom, img, hide)
    elif ground == "in_place":
        img = clear_in_place(eraser, bbox, zoom, img, hide)
    elif ground != "opaque":
        assert_never(ground)
    writer.save(img, path)
    return img.shape[1], img.shape[0]


def crop_ground(fig: JsonObject, bbox: Sequence[float], furniture: Sequence[Box]) -> CropGround:
    """How `fig`'s crop takes its ground: transparent when anchored to words, or when its box
    reaches over header or footer words the layouts carry (`furniture`: the bands of the slide's
    `on_layout` glyphs, `promote_theme_text`). Those words lie on the layout, under every slide
    element, so an opaque crop hid them where the PDF draws them over the figure's ground: a frame
    running down to the page foot cut 'Институт Геог|рафии' off at its edge
    (real_africa-remote-sens-30 s2)."""
    if fig.get("anchor"):
        return "anchored"
    return "in_place" if any(_intersects(bbox, b) for b in furniture) else "opaque"


GROUND_FLAT = 6        # levels: the page under a picture counts as one colour
GROUND_MATCH = 16      # levels: the transparent crop laid on that colour shows the opaque crop


def _ground_objects(eraser: Eraser, bbox: Sequence[float]) -> list[int]:
    """What stays in the background under a figure's box: paths reaching out of the box as
    render_backgrounds leaves them, images and shadings not inside it."""
    bounds = eraser.bounds
    return [key for key, po in eraser.objects.items() if key not in eraser.removed and (
        (po.type == OBJ_PATH and not _inside(bounds[key], _grow(bbox, 5))) or
        (po.type in (OBJ_IMAGE, OBJ_SHADING) and not _inside(bounds[key], _grow(bbox, 0.5))))]


def clear_in_place(eraser: Eraser, bbox: Sequence[float], zoom: float, opaque: RGB, hide: Sequence[int]) -> Pixels:
    """A figure that keeps its place over the slide's own background, see-through where it paints
    nothing (`crop_ground`'s `in_place`), so the layout's words show there. The background holds
    the page under the figure (`_ground_objects`, in any colours: the title bar's edge, a block's
    shadow), so a pixel is clear only where the figure's own objects leave it empty and the opaque
    crop shows that ground alone; every other pixel is the opaque crop's (binary alpha: a ground
    drawn over the figure, a block's shading over its body, stays as the page shows it)."""
    ground = _ground_objects(eraser, bbox)
    rgba = eraser.render(zoom, _box(bbox), True, ground + list(hide))
    drawn = [key for key in eraser.objects if key not in eraser.removed and key not in set(ground)]
    under = eraser.render(zoom, _box(bbox), False, drawn + list(hide))
    if rgba.shape[:2] != opaque.shape[:2] or under.shape[:2] != opaque.shape[:2]:
        return opaque
    clear = (rgba[..., 3] == 0) & \
        (np.abs(opaque[..., :3].astype(int) - under[..., :3].astype(int)).max(axis=2) <= GROUND_FLAT)
    if int(np.count_nonzero(clear)) < 20:
        return opaque
    return np.dstack([opaque[..., :3], np.where(clear, 0, 255).astype(np.uint8)])


def clear_ground(eraser: Eraser, bbox: Sequence[float], zoom: float, opaque: RGB, hide: Sequence[int]) -> Pixels:
    """A picture anchored to text (inline formula, icon, number ball) on a transparent ground, so it
    shows the slide under it when the background colour changes or it is moved onto a shape.
    What stays in the background (`_ground_objects`) is left out. Kept only where the page under
    the picture is flat and the result laid on that colour shows the opaque crop; else the opaque
    crop."""
    ground = _ground_objects(eraser, bbox)
    rgba = eraser.render(zoom, _box(bbox), True, ground + list(hide))
    if rgba.shape[:2] != opaque.shape[:2]:
        return opaque
    clear = rgba[..., 3] == 0
    if clear.sum() < 20:
        return opaque
    page_px = opaque[clear].astype(int)
    colour = np.median(page_px, axis=0)
    if (np.abs(page_px - colour).max(axis=1) > GROUND_FLAT).mean() > 0.002:
        return on_picture_ground(eraser, bbox, zoom, opaque, rgba, ground, hide)
    rgba = unblend_rim(rgba, clear, colour, max(1, round(GROUND_RIM * zoom)))
    alpha = rgba[..., 3:].astype(float) / 255
    laid = rgba[..., :3] * alpha + colour * (1 - alpha)
    if (np.abs(laid - opaque).max(axis=2) > GROUND_MATCH).mean() > 0.002:
        return opaque
    return rgba


def on_picture_ground(eraser: Eraser, bbox: Sequence[float], zoom: float, opaque: RGB, rgba: RGBA,
                      ground: list[int], hide: Sequence[int]) -> Pixels:
    """An anchored picture on a photo or a shading reaching out of its box (a `\\textbar\\quad
    \\faIcon` hole on a full-bleed title photo, r1_design_v2 s1): the ground is no one colour,
    but it stays in the background under the picture (render_backgrounds keeps an image that
    holds an anchored box), so the transparent crop is right wherever Slides sets the words.
    An opaque crop showed its piece of the photo out of line with the photo behind it, a boxed
    patch, once the re-set words moved it. Kept when the crop laid on the ground alone shows
    the opaque crop, and only on a ground of images and shadings."""
    if not any(eraser.objects[key].type in (OBJ_IMAGE, OBJ_SHADING) and _inside(_grow(bbox, -0.5), eraser.bounds[key])
               for key in ground):
        return opaque
    drawn = [key for key in eraser.objects if key not in eraser.removed and key not in set(ground)]
    under = eraser.render(zoom, _box(bbox), False, drawn + list(hide))
    if under.shape[:2] != opaque.shape[:2]:
        return opaque
    alpha = rgba[..., 3:].astype(float) / 255
    laid = rgba[..., :3] * alpha + under[..., :3].astype(float) * (1 - alpha)
    if (np.abs(laid - opaque[..., :3]).max(axis=2) > GROUND_MATCH).mean() > 0.002:
        return opaque
    return rgba


GROUND_RIM = 0.5  # pt


def unblend_rim(rgba: RGBA, clear: Mask, ground: Floats, width: int) -> RGBA:
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


def crop_overlay(eraser: Eraser, fig: JsonObject, labels: list[RawSpan], path: Path,
                 writer: PngWriter) -> tuple[int, int]:
    """A graphic drawn over text (classify.overlay): only its own drawings and labels, on a
    transparent ground, so neither the page nor text left in the background under it comes
    along. They leave the background right away: no other crop shows them either. Handed to
    `writer`; its (width, height) in pixels."""
    page = eraser.page
    objects = [d["object"] for d in page.drawings()]  # in the order extract numbered them
    paths = {objects[int(i.rsplit("d", 1)[1])] for i in _ids(fig, "drawings")}
    bands = [_span_band(s) for s in labels]

    def hit(ch: Char) -> bool:
        return any(_intersects(ch.box, b) for b in bands)

    texts = {key for key, chars in eraser.chars.items() if any(map(hit, chars))}
    off = [key for key, po in eraser.objects.items() if key not in paths | texts and po.type != OBJ_FORM]
    box = _json_box(fig["bbox"], "bbox")
    x0, y0, x1, y1 = box
    zoom = min(FIGURE_PX_PER_PT if max(x1 - x0, y1 - y0) > 60 else SMALL_FIGURE_PX_PER_PT,
               FIGURE_MAX_PX / max(x1 - x0, y1 - y0))
    page.set_active(off, False)
    try:
        img = page.render(zoom, box, transparent=True)
    finally:
        page.set_active(off, True)
    for key in paths:
        eraser._remove(key)
    eraser.remove_chars(hit)
    writer.save(img, path)
    return img.shape[1], img.shape[0]


INK_ZOOM = 4.0      # px per pt at which a formula's ink is measured
INK_REACH = 3.0     # em beyond its box that a formula glyph's ink is looked for (a \left( of three rows)
INK_PAD = 0.75      # pt around the ink found, for anti-aliasing


def formula_objects(eraser: Eraser, members: list[RawSpan]) -> list[int]:
    """The text objects (ids) holding only these spans' glyphs, those `grow_to_ink` measures."""
    rects = [s["bbox"] for s in members]

    def mine(ch: Char) -> bool:
        return any(r[0] - 0.1 <= (ch.box[0] + ch.box[2]) / 2 <= r[2] + 0.1 and
                   r[1] - 0.1 <= (ch.box[1] + ch.box[3]) / 2 <= r[3] + 0.1 for r in rects)

    return [key for key, chars in eraser.chars.items() if chars and all(map(mine, chars))]


def grow_to_ink(eraser: Eraser, bbox: list[float], members: list[RawSpan]) -> list[float]:
    """A formula picture's box grown to the ink of its own glyphs. Glyph boxes are font boxes
    (ascender to descender), not ink: a display \\sum or \\int from CMEX hangs from its origin
    and reaches an em past its box, a \\left( of a matrix three rows, and a picture of the box
    cut the sign to a chevron while its switched-off glyph left the background too. The ink is
    what the text objects holding only the formula's glyphs paint: the page rendered around the
    box with and without them."""
    own = formula_objects(eraser, members)
    if not own:
        return bbox
    reach = INK_REACH * max(s["size"] for s in members)
    page = eraser.page
    area = (max(0.0, bbox[0] - reach), max(0.0, bbox[1] - reach),
            min(page.width, bbox[2] + reach), min(page.height, bbox[3] + reach))
    if area[2] <= area[0] or area[3] <= area[1]:
        return bbox
    drawn = eraser.render(INK_ZOOM, area, False, ()).astype(np.int16)
    bare = eraser.render(INK_ZOOM, area, False, own).astype(np.int16)
    if drawn.shape != bare.shape:
        return bbox
    box = _ink_bounds(np.abs(drawn - bare).max(axis=2) > 24, area)
    if box is None:
        return bbox
    box = _grow(box, INK_PAD)
    return [round(float(v), 2) for v in (min(bbox[0], box[0]), min(bbox[1], box[1]),
                                  max(bbox[2], box[2]), max(bbox[3], box[3]))]


def _ink_bounds(ink: Mask, area: Box) -> Box | None:
    """The page box of the ink found in a render of `area` at INK_ZOOM, None without any."""
    if not ink.any():
        return None
    ys, xs = np.nonzero(ink)
    ox, oy = np.floor(area[0] * INK_ZOOM + 0.001), np.floor(area[1] * INK_ZOOM + 0.001)
    return (float(xs.min() + ox) / INK_ZOOM, float(ys.min() + oy) / INK_ZOOM,
            float(xs.max() + 1 + ox) / INK_ZOOM, float(ys.max() + 1 + oy) / INK_ZOOM)


def _past(bbox: list[float], ink: Box) -> list[float]:
    """`bbox` grown, on each side the ink runs past, to INK_PAD beyond the ink."""
    return [round(v, 2) for v in (min(bbox[0], ink[0] - INK_PAD) if ink[0] < bbox[0] else bbox[0],
                                  min(bbox[1], ink[1] - INK_PAD) if ink[1] < bbox[1] else bbox[1],
                                  max(bbox[2], ink[2] + INK_PAD) if ink[2] > bbox[2] else bbox[2],
                                  max(bbox[3], ink[3] + INK_PAD) if ink[3] > bbox[3] else bbox[3])]


def holds(box: Sequence[float], held: GlyphTest) -> GlyphTest:
    """The glyphs the background loses to a picture with this box: those whose centre the box
    holds, and those `held` (the picture's own spans' glyphs) that the box reaches."""

    def test(ch: Char) -> bool:
        if not _intersects(ch.box, box):
            return False
        cx, cy = (ch.box[0] + ch.box[2]) / 2, (ch.box[1] + ch.box[3]) / 2
        return (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]) or held(ch)

    return test


INK_ROUNDS = 3  # times a picture's box is grown to the ink of the glyphs it takes (a grown box takes more)


def reach_held_ink(eraser: Eraser, bbox: list[float], held: GlyphTest, reached: Collection[int]) -> list[float]:
    """A picture's box grown to the ink of every glyph the background loses to it (`holds`).
    Glyph boxes run from origin to advance, not ink: an italic label's tail or overhang lies
    outside its box, and a crop of the box cut the glyph the background no longer showed (the
    `q` beside a tikz-cd arrow, real_beamer-monodromy s11). A text object whose glyphs all go
    is measured whole (the page with and without it); a glyph that leaves an object partly goes
    from the background within GLYPH_MARGIN of its box (`Eraser.remove_chars`), so only its ink
    there is reached. The text objects in `reached` (ids: a formula's own, which `grow_to_ink`
    has just measured) the box already holds the ink of."""
    measured: list[Char] = []
    for _ in range(INK_ROUNDS):
        taken = holds(bbox, held)
        chars = [ch for chs in eraser.chars.values() for ch in chs if taken(ch)]
        if not chars or chars == measured:
            return bbox  # (the box grown takes no other glyph: their ink is reached)
        measured = chars
        whole = {key for key, chs in eraser.chars.items() if chs and all(map(taken, chs))}
        alone = [ch for ch in chars if ch.obj in whole and ch.obj not in reached]
        partly = [ch for ch in chars if ch.obj not in whole]
        grown = bbox
        if alone:
            reach = INK_REACH * max(ch.size for ch in alone)
            grown = _past_ink(eraser, grown, [_grow(ch.box, reach) for ch in alone], sorted({ch.obj for ch in alone}), False)
        if partly:
            grown = _past_ink(eraser, grown, [_grow(ch.box, GLYPH_MARGIN * ch.size) for ch in partly],
                              sorted({ch.obj for ch in partly}), True)
        if grown == bbox:
            return bbox
        bbox = grown
    return bbox


def _past_ink(eraser: Eraser, bbox: list[float], reaches: list[Box], objects: list[int], within: bool) -> list[float]:
    """`bbox` grown to the ink `objects` (text object ids) paint within the bounds of `reaches`
    (within the `reaches` themselves when `within`) where it runs past the box."""
    page = eraser.page
    area = (max(0.0, min(r[0] for r in reaches)), max(0.0, min(r[1] for r in reaches)),
            min(page.width, max(r[2] for r in reaches)), min(page.height, max(r[3] for r in reaches)))
    if area[2] <= area[0] or area[3] <= area[1] or _inside(area, bbox):
        return bbox  # (no ink of theirs can lie past the box)
    drawn = eraser.render(INK_ZOOM, area, False, ()).astype(np.int16)
    bare = eraser.render(INK_ZOOM, area, False, objects).astype(np.int16)
    if bare.shape != drawn.shape:
        return bbox
    ink = np.abs(drawn - bare).max(axis=2) > 24
    if within:
        near = np.zeros_like(ink)
        ox, oy = np.floor(area[0] * INK_ZOOM + 0.001), np.floor(area[1] * INK_ZOOM + 0.001)
        h, w = near.shape
        for g in reaches:
            a0, b0 = max(0, int(np.floor(g[0] * INK_ZOOM - ox))), max(0, int(np.floor(g[1] * INK_ZOOM - oy)))
            a1, b1 = min(w, int(np.ceil(g[2] * INK_ZOOM - ox))), min(h, int(np.ceil(g[3] * INK_ZOOM - oy)))
            near[b0:b1, a0:a1] = True
        ink &= near
    found = _ink_bounds(ink, area)
    return bbox if found is None else _past(bbox, found)


def crop_region(pdf: Path, page: int, bbox: list[float], path: Path, zoom: float) -> None:
    """A picture of a page region (emit's stand-in for an element the Slides API refused)."""
    doc = Document(pdf)
    try:
        doc[page].set_hairline(hairline(doc[page]))
        save_png(doc[page].render(zoom, _box(bbox), transparent=False), path)
    finally:
        doc.close()


def hairline(page: Page) -> float:
    """The thinnest stroke (pt) a picture of `page` for Slides draws (`HAIRLINE_SLIDE_PX`): one
    pixel of the slide shown that wide. Renders at that scale or coarser draw as before (a
    viewer's one-pixel minimum is already as wide)."""
    return page.width / HAIRLINE_SLIDE_PX


SHAPE_SAMPLE_ZOOM = 2.0


def _fill_fraction(page: Page, rect: Box, fill: str, avoid: list[Box]) -> float:
    """Share of sample points inside `rect` (away from text and pictures) that render in `fill`."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    zoom = max(SHAPE_SAMPLE_ZOOM, 8 / max(min(width, height), 0.01))  # hairlines: enough pixels across
    img = page.render(zoom, rect, transparent=False).astype(int)
    h, w = img.shape[:2]
    target = np.array([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
    ys = np.linspace(h * 0.2, h * 0.8 - 1, 12).astype(int)
    xs = np.linspace(w * 0.02, w * 0.98 - 1, 40).astype(int)
    # The 12 x 40 samples at once (one by one against every word's box: 1.4 s on a 57-slide deck)
    px, py = rect[0] + xs / zoom, rect[1] + ys / zoom
    counted = np.ones((len(ys), len(xs)), bool)
    if avoid:
        a = np.array(avoid, dtype=float).reshape(-1, 4)
        rows = (a[:, 1, None] <= py) & (py <= a[:, 3, None])  # (box, sample row)
        cols = (a[:, 0, None] <= px) & (px <= a[:, 2, None])  # (box, sample column)
        counted = ~(rows[:, :, None] & cols[:, None, :]).any(axis=0)
    total = int(np.count_nonzero(counted))
    hits = int(np.count_nonzero(counted & (np.abs(img[np.ix_(ys, xs)] - target).max(axis=2) <= 12)))
    return hits / total if total else 0.0


def _probe(el: JsonObject) -> Box:
    """The inside of a shape, clear of rounded corners and anti-aliased edges (thin bars keep
    their middle)."""
    x0, y0, x1, y1 = _json_box(el["bbox"], "bbox")
    dx, dy = min(_number(el["radius"], "radius") + 1.5, (x1 - x0) / 4), min(1.5, (y1 - y0) / 4)
    return x0 + dx, y0 + dy, x1 - dx, y1 - dy


def _elements(slide: JsonObject) -> list[JsonObject]:
    return as_objects(slide["elements"], "slide.elements")


def _title_top(el: JsonObject) -> float:
    """Where a block's title bar starts (`title_bar`: its box)."""
    return _numbers(el["title_bar"], "title_bar")[1]


def verify_and_remove_shapes(original: Page, eraser: Eraser, slide: JsonObject, raw_page: RawPage) -> None:
    """Keep only shapes whose panel visibly shows its fill colour (beamer draws shadows as
    black rectangles under a soft mask), then remove those panels from the background."""
    keep_visible_shapes(original, slide, raw_page)
    remaining = [e for e in _elements(slide) if e["kind"] == "shape"]
    for margin in (1.5, 5.0):  # a stroked outline reaches past the panel; widen if a panel survived
        if not remaining:
            break
        for el in remaining:
            eraser.remove_paths_inside(_grow(_json_box(el["bbox"], "bbox"), margin))
            if el.get("shadow"):
                # The shadow's black rectangles (drawn under a soft mask) would render solid
                # once the panels above them are gone.
                x0, y0, x1, y1 = _json_box(el["bbox"], "bbox")
                y0 = _title_top(el) if el.get("title_bar") else y0
                size = _number(as_object(el["shadow"], "shadow")["size"], "shadow.size")
                eraser.remove_paths_inside((x0 - 1.5, y0 - 1.5, x1 + size + 1.5, y1 + size + 1.5))
        # Still drawn: the panel's path is still on the page. (Its colour showing is no proof: a
        # white block on a white page looks the same with or without its panel.)
        drawn = eraser.filled_paths()
        remaining = [el for el in remaining
                     if any(all(abs(r[k] - _json_box(el["bbox"], "bbox")[k]) < 0.5 for k in range(4)) for r in drawn)]
    if remaining:  # could not be removed: leave those panels in the background only
        kept: list[Json] = [e for e in _elements(slide) if e not in remaining]
        slide["elements"] = kept


def keep_visible_shapes(original: Page, slide: JsonObject, raw_page: RawPage) -> None:
    """Drop shape candidates whose fill doesn't show on the page. (A shape the page marks as one -
    adopt's slides.sty, `marked.py` - is one whatever covers it.)"""
    elements = _elements(slide)
    avoid: list[Box] = [_box(s["bbox"]) for s in raw_page["spans"]] + [_box(i["bbox"]) for i in raw_page["images"]] + \
        [_json_box(e["bbox"], "bbox") for e in elements if e["kind"] == "image"]
    keep: list[Json] = []
    for i, el in enumerate(elements):
        if el["kind"] != "shape" or el.get("mark"):
            keep.append(el)
            continue
        probe = _probe(el)
        # Rules drawn on top of this one (a progress bar on its track) hide its colour there.
        above = [_json_box(e["bbox"], "bbox") for e in elements[i + 1:] if e["kind"] == "shape" and e.get("role") == "rule"]
        # (a translucent highlight shows its colour mixed with the page: not a shadow's black box)
        if probe[2] <= probe[0] or probe[3] <= probe[1] or \
                (not el.get("opacity") and _fill_fraction(original, probe, as_str(el["fill"], "fill"), avoid + above) < 0.9):
            continue
        keep.append(el)
    slide["elements"] = keep


def _paragraphs(el: JsonObject) -> list[JsonObject]:
    return [as_object(p, "paragraph") for p in _array_at(el, "paragraphs")]


def render_backgrounds(pdf: Path, raw: RawDoc, deck: JsonObject, out: Path,
                       kept_shapes: frozenset[str]) -> list[Path]:
    """Each slide's background picture and its figures' crops, written under `out`; `deck`
    (deck.json) is changed in place into rendered.json. `kept_shapes`: marks
    `marked.pictured_shapes` leaves shapes (sync over an old adopt base), else none. Every file
    is written when it returns."""
    writer = PngWriter(PNG_WRITERS)
    try:
        paths = _render_slides(pdf, raw, deck, out, kept_shapes, writer)
        writer.finish()
    finally:
        writer.close()
    return paths


def _render_slides(pdf: Path, raw: RawDoc, deck: JsonObject, out: Path, kept_shapes: frozenset[str],
                   writer: PngWriter) -> list[Path]:
    """render_backgrounds' work, its pictures handed to `writer`."""
    from .marked import pictured_shapes

    doc = Document(pdf)
    original = Document(pdf)
    raw_pages = {p["index"]: p for p in raw["pages"]}  # by PDF page (overlay steps may be skipped)
    spans = {s["id"]: s for page in raw["pages"] for s in page["spans"]}
    images = {i["id"]: i for page in raw["pages"] for i in page["images"]}
    (out / "background.pdf").unlink(missing_ok=True)  # written by earlier versions
    paths: list[Path] = []
    for slide in as_objects(deck["slides"], "deck.slides"):
        index = as_int(slide["page"], "slide.page")
        doc[index].set_hairline(hairline(doc[index]))  # (`original`, read for its fills, draws as the PDF)
        eraser = Eraser(doc[index])
        pictured_shapes(slide, raw_pages[index], kept_shapes)
        texts = [e for e in _elements(slide) if e["kind"] == "text"]
        figures = [e for e in _elements(slide) if e["kind"] == "image"]

        # A glyph goes with a native line only when it runs the line's way: a stamp turned 25°
        # across the bullets ("DRAFT", tikz overlay) has glyph boxes far larger than its ink,
        # which met the words' bands and lost letters under them.
        native = [sid for el in texts for sid in _ids(el, "spans")] + _ids(slide, "on_layout")
        bands = [(_span_band(s), tuple(s["dir"])) for s in (spans[sid] for sid in native)]
        if bands:
            # A glyph a picture owns stays for its crop, whatever line's band it reaches into: a
            # radical sign hangs from its origin an em above its formula's baseline, into the
            # words of the line above, and went with them (neither text nor picture showed it).
            mine = set(native)
            pictured = owned_by([spans[sid] for el in figures for sid in _ids(el, "spans")
                                 if sid in spans and sid not in mine])
            # So does a glyph classify left in the background: a long arrow's txsys pieces have
            # boxes an em tall around a stroke at the math axis, and the box of the second of
            # two `\xrightarrow{words}` reached its label's x-height band - the arrow went with
            # the label, in neither the background nor the deck (real cat s7).
            kept = owned_by([spans[sid] for left in _array_at(slide, "left_in_background")
                             for sid in _ids(as_object(left, "left_in_background"), "spans")
                             if sid in spans and sid not in mine])
            ours = owned_by([spans[sid] for sid in native])

            def in_line(ch: Char) -> bool:
                return any(_intersects(ch.box, b) and _same_dir(ch.dir, d) for b, d in bands) \
                    and not ((pictured(ch) or kept(ch)) and not ours(ch))

            eraser.remove_chars(in_line)
        for x0, y0, x1, y1 in (_json_box(st, "strokes") for el in texts for st in _array_at(el, "strokes")):
            eraser.remove_paths_inside((x0 - 1.5, y0 - 1.5, x1 + 1.5, y1 + 1.5))  # bars of fractions converted to text

        # A native list's image bullets are the list's, whatever picture box reaches them: a pie's
        # pin label ending beside a ball-bullet column showed slivers of the balls at its edge,
        # beside the Slides bullets.
        bullet_boxes: list[Box] = []
        for el in texts:
            for p in _paragraphs(el):
                bullet = p["bullet"]
                if isinstance(bullet, dict) and bullet and bullet.get("kind") == "image" and bullet.get("image") in images:
                    bullet_boxes.append(_grow(images[as_str(bullet["image"], "bullet.image")]["bbox"], 0.5))
        bullet_objects = [key for key, po in eraser.objects.items() if po.type in (OBJ_IMAGE, OBJ_SHADING)
                          and any(_inside(eraser.bounds[key], b) for b in bullet_boxes)]

        others = others_glyphs(eraser, figures, spans)
        furniture = [_span_band(spans[sid]) for sid in _ids(slide, "on_layout") if sid in spans]
        # Graphics drawn over text first: the other crops must not show them.
        for fig in sorted(figures, key=lambda f: not f.get("overlay")):
            fid = as_str(fig["id"], "id")
            path = out / "figures" / f"{fid}.png"
            if fig.get("overlay"):
                w, h = crop_overlay(eraser, fig, [spans[sid] for sid in _ids(fig, "spans")], path, writer)
                fig["px"] = [w, h]
            else:
                # A region that is one `\includegraphics` keeps the embedded file itself.
                own = None if fig.get("anchor") or not fig.get("image") else embedded_picture(eraser, fig, path)
                path = own or path
                if own is None:
                    # (an icon glyph too: FontAwesome's advance box under xelatex is half its
                    # warning triangle, and the crop of the box cut the '!' off)
                    members = [spans[sid] for sid in _ids(fig, "spans") if sid in spans]
                    reached: list[int] = []
                    if fig.get("role") == "math" or (fig.get("role") == "icon" and fig.get("spans")):
                        fig["bbox"] = _json_numbers(grow_to_ink(eraser, _numbers(fig["bbox"], "bbox"), members))
                        reached = formula_objects(eraser, members)
                    # (whatever glyph the background loses to the picture, the crop shows whole)
                    fig["bbox"] = _json_numbers(reach_held_ink(eraser, _numbers(fig["bbox"], "bbox"),
                                                               owned_by(members), frozenset(reached)))
                    box = _numbers(fig["bbox"], "bbox")
                    w, h = crop_figure(eraser, box, raw_pages[index]["images"], path, crop_ground(fig, box, furniture),
                                       bullet_objects + others.get(fid, []), writer)
                    fig["px"] = [w, h]
            fig["file"] = str(path.relative_to(out)).replace("\\", "/")
        # Native tables leave the background the same way pictures do (text and rules), without a crop.
        figures = [f for f in figures if not f.get("overlay")]
        figures += [e for e in _elements(slide) if e["kind"] in ("table", "diagram")]
        if figures:
            boxes = [_json_box(fig["bbox"], "bbox") for fig in figures]
            # The glyphs a picture holds, not those its box grazes: the words just above a thin
            # figure (a rule under them) reach into it by their descent, and went from the
            # background while the crop showed only the rule. (What a grazed glyph has inside the
            # box the picture shows over it, in place.)
            held = owned_by([spans[sid] for fig in figures for sid in _ids(fig, "spans") if sid in spans])
            taken = [holds(b, held) for b in boxes]

            def in_figure(ch: Char) -> bool:
                return any(test(ch) for test in taken)

            eraser.remove_chars(in_figure)
            for fig, b in zip(figures, boxes):
                # (a native list's ball stays whole for its colour and its patch: a pie's label
                # widening the figure over the ball column took half of each ball, and the Slides
                # bullets came out in each item's first word's colour; and a photo under a hole
                # stays whole, the hole's ground: cut out, it left a white box that showed, over
                # the next word, wherever Slides set the words off the PDF's place)
                ground = [key for key, po in eraser.objects.items() if fig.get("anchor") and
                          po.type in (OBJ_IMAGE, OBJ_SHADING) and _inside(_grow(b, -0.5), eraser.bounds[key])]
                eraser.remove_images_in(b, bullet_objects + ground)
                # Stroked paths reach past the figure box by half their width, arrow tips further.
                eraser.remove_paths_inside(_grow(b, 5))

        # Panels last: figure crops taken above still show the panel colour behind them.
        verify_and_remove_shapes(original[index], eraser, slide, raw_pages[index])

        page = doc[index]
        zoom = BACKGROUND_WIDTH_PX / page.width
        path = out / "backgrounds" / f"bg-{index + 1:03}.png"
        img = eraser.render(zoom, None, False, ())

        px_per_pt = BACKGROUND_WIDTH_PX / _numbers(slide["size"], "slide.size")[0]
        bullets: list[list[float]] = []
        for el in _elements(slide):
            for p in _paragraphs(el):
                b = p["bullet"]
                if not isinstance(b, dict) or not b:
                    continue
                # (a ball's image box is rounded out to whole points around a transparent margin:
                # emit sizes the Slides disc by the ball's ink, a numbered ball by its label)
                ball = b["kind"] == "image" and not as_str(b.get("text", ""), "bullet.text").strip()
                if (b["kind"] == "glyph" or ball) and "ink" not in b:
                    measured = glyph_ink(original[index], _numbers(b["bbox"], "bullet.bbox"))
                    if measured:  # (emit sizes and shapes the Slides bullet by it)
                        ink, fill = measured
                        b["ink"] = _json_numbers(ink)
                        b["fill"] = fill
                if b["kind"] == "image":
                    bullets.append(images[as_str(b["image"], "bullet.image")]["bbox"])
                    # (the Slides bullet's colour)
                    b.setdefault("color", ink_colour(img, _numbers(b["bbox"], "bullet.bbox"), px_per_pt))
                elif b.get("patch"):  # number drawn on a vector box
                    x0, y0, x1, y1 = _json_box(b["bbox"], "bullet.bbox")
                    bullets.append([x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5])
        if bullets:
            patch_rects(img, bullets, px_per_pt)
        paint_out_leftovers(img, slide, px_per_pt)
        writer.save(img, path)
        paths.append(path)
        slide["background"] = str(path.relative_to(out)).replace("\\", "/")
        slide["background_color"] = uniform_color(img, 3)
    doc.close()
    original.close()
    return paths


DECORATION_MIN = 0.002    # share of the page: less is no theme decoration
DECORATION_AGREE = 0.9    # share of the decoration a background must show to be decorated alike
DECORATION_TOLERANCE = 2  # levels
DECORATION_HOLES = 0.01   # share of the page: more cut out of decoration is slide content, not the theme's


def page_ground(img: RGB) -> Ints:
    """The most common colour of a background (the page ground)."""
    px = img[::4, ::4].reshape(-1, 3).astype(np.int64)
    packed = (px[:, 0] << 16) | (px[:, 1] << 8) | px[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    v = values[counts.argmax()]
    return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255])


def theme_decoration(images: Iterable[RGB], ground: Ints) -> tuple[RGBA | None, list[bool], bool]:
    """The theme decoration a layout can carry for backgrounds that share it (`images`: distinct
    backgrounds, the most used first): an RGBA picture of the first one, opaque where it differs
    from the ground and every background taking it shows the same pixels, transparent elsewhere.
    Drawn over those backgrounds it changes nothing, and over another ground colour it keeps the
    bars and lines. Returns (picture or None, per image whether it shows the decoration, whether
    the first image is exactly the ground plus the picture).

    Where the backgrounds taking it differ the picture is cut out, and a slide made in Slides shows
    the master's ground there: right where most of them show the ground (a formula one of them
    keeps), wrong where most show decoration. Cut out of decoration over more than
    `DECORATION_HOLES` of the page, it is what every slide keeps in its background etched into the
    theme (a page border read as a figure left every slide's words there), and is no decoration."""
    rest = iter(images)
    whole_first = next(rest)
    full = differs(whole_first, np.broadcast_to(np.asarray(ground, dtype=np.uint8), whole_first.shape),
                   DECORATION_TOLERANCE)
    total = int(np.count_nonzero(full))
    if total < DECORATION_MIN * full.size:
        return None, [False] * (1 + sum(1 for _ in rest)), False
    # Everything below is about pixels the first background shows decoration on, so it is worked
    # out on the rows and columns that hold some (a headline and a footline: a fifth of the page).
    rows, cols = np.flatnonzero(full.any(axis=1)), np.flatnonzero(full.any(axis=0))

    def sub(a: RGB) -> RGB:
        a = a if len(rows) == a.shape[0] else a[rows]
        return a if len(cols) == a.shape[1] else a[:, cols]

    first = sub(whole_first)
    colour = np.broadcast_to(np.asarray(ground, dtype=np.uint8), first.shape)
    start = differs(first, colour, DECORATION_TOLERANCE)
    mask = start.copy()
    inside = [True]
    shown = start.astype(np.int32)  # per pixel: how many of the backgrounds taking it show no ground there
    for whole in rest:
        img = sub(whole) if whole.shape == whole_first.shape else None
        same = None if img is None else ~differs(img, first, DECORATION_TOLERANCE)
        ok = same is not None and np.count_nonzero(same & mask) >= DECORATION_AGREE * np.count_nonzero(mask)
        inside.append(bool(ok))
        if ok and img is not None and same is not None:
            mask &= same
            shown += differs(img, colour, DECORATION_TOLERANCE)
    if np.count_nonzero(start & ~mask & (2 * shown > sum(inside))) > DECORATION_HOLES * full.size:
        return None, [False] * len(inside), False
    alpha = np.zeros(full.shape, dtype=np.uint8)
    alpha[np.ix_(rows, cols)] = np.where(mask, 255, 0)
    # (binary alpha: over a background showing the same pixels, any soft edge would change them)
    return np.dstack([whole_first, alpha]), inside, int(np.count_nonzero(mask)) == total


def differs(a: RGB, b: RGB, tolerance: int) -> Mask:
    """Per pixel, whether any channel of two pictures of one size is more than `tolerance` apart.
    (In uint8, channel by channel: a whole page cast to int16 and reduced over its last axis took
    4.5 s a call on an 86-slide deck, `theme_decoration` three calls a conversion.)"""
    d = np.maximum(a, b) - np.minimum(a, b)
    return (d[..., 0] > tolerance) | (d[..., 1] > tolerance) | (d[..., 2] > tolerance)


def uniform_color(img: RGB, tolerance: int) -> str | None:
    """The single colour of a background with nothing left on it, else None. Such slides get
    a plain Slides background colour instead of a picture."""
    sample = img[::17, ::17]
    ref = np.median(sample.reshape(-1, 3), axis=0)
    colour = ref.astype(np.uint8)
    # (the sample first: a page that is not one colour mostly shows it there, without a pass over
    # every pixel)
    if differs(sample, np.broadcast_to(colour, sample.shape), tolerance).any() or \
            differs(img, np.broadcast_to(colour, img.shape), tolerance).any():
        return None
    return "#" + "".join(f"{int(v):02x}" for v in ref)


def flat_colour(img: RGB, area: Box, px_per_pt: float, ring: int, ignore: Mask | None) -> Floats | None:
    """The page colour around an area (a thin ring `ring` px wide just outside it), if that ring
    is flat. Pixels marked in `ignore` (other converted elements, a neighbour's shadow) don't
    count."""
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


def paint_out_leftovers(img: RGB, slide: JsonObject, px_per_pt: float) -> None:
    """Paint out of the background what would otherwise stay behind when a native element
    is moved, with the page colour around it (only where that is flat):

    - pictures: soft-masked pixels and outlines reaching past the figure box (beamer buttons,
      ball icons);
    - blocks whose title bar and body both became shapes: the gradient strip between the two
      (emit lays the body under the title bar) and the shadow pieces (emit gives the body a
      native drop shadow). Where the page is not flat the shadow stays in the background and
      the body gets none."""
    shapes = [e for e in _elements(slide) if e["kind"] == "shape"]
    kept = {e["block"] for e in shapes if e.get("block") is not None and "title_bar" not in e} & \
           {e["block"] for e in shapes if e.get("title_bar")}
    for el in shapes:
        if el.get("block") is not None and el["block"] not in kept:  # one half was not verified
            for key in ("block", "title_bar", "strips"):
                el.pop(key, None)
    jobs: list[tuple[Box, list[Box], JsonObject]] = []  # (area whose surroundings give the colour, rects to paint, element)
    for el in shapes:
        rects = [_json_box(r, "strips") for r in _array_at(el, "strips")] + \
            ([_json_box(r, "shadow.pieces") for r in as_array(as_object(el["shadow"], "shadow")["pieces"], "shadow.pieces")]
             if el.get("shadow") else [])
        if rects:
            x0, y0, x1, y1 = _json_box(el["bbox"], "bbox")
            area = (x0, _title_top(el) if el.get("title_bar") else y0, x1, y1)
            for r in rects:
                area = (min(area[0], r[0]), min(area[1], r[1]), max(area[2], r[2]), max(area[3], r[3]))
            jobs.append((area, rects, el))
    for el in _elements(slide):
        if el["kind"] == "image" and not el.get("overlay"):  # (an overlay's box holds the page under it)
            box = _json_box(el["bbox"], "bbox")
            jobs.append((box, [box], el))
    # Other elements and everything about to be painted don't count as the page around an area.
    ignore = np.zeros(img.shape[:2], bool)
    covered = [_json_box(e["bbox"], "bbox") for e in _elements(slide) if e["kind"] in ("shape", "image", "table", "diagram")]
    covered += [r for _, rects, _ in jobs for r in rects]
    covered += [(x0, _title_top(e), x1, y1) for e in shapes if e.get("title_bar")
                for x0, _, x1, y1 in [_json_box(e["bbox"], "bbox")]]
    for rx0, ry0, rx1, ry1 in covered:
        ignore[max(0, int(ry0 * px_per_pt) - 2):int(np.ceil(ry1 * px_per_pt)) + 2,
               max(0, int(rx0 * px_per_pt) - 2):int(np.ceil(rx1 * px_per_pt)) + 2] = True
    colours = [flat_colour(img, area, px_per_pt, 4, ignore) for area, _, _ in jobs]  # before any painting
    # A drawing the background keeps that runs into a picture's box from outside (a git graph's
    # main line through the branch picture) stays where it crosses: painted out, it stopped a
    # pixel short of the crop on either side, a white nick in the line in Slides (and under an
    # anchored picture's transparent ground, which leaves such paths to the background, a gap).
    crossed = [picture_crossings(img, area, colour, px_per_pt, ignore, 4) if colour is not None and el["kind"] == "image"
               else (np.zeros(0, int), np.zeros(0, int)) for (area, _, el), colour in zip(jobs, colours)]
    for (area, rects, el), colour, (rows, cols) in zip(jobs, colours, crossed):
        if colour is None:
            if el["kind"] == "shape":
                el.pop("shadow", None)  # keep the background's shadow; the strip is under the shapes anyway
            continue
        for rx0, ry0, rx1, ry1 in rects:
            c0, d0 = max(0, int(np.floor(rx0 * px_per_pt)) - 1), max(0, int(np.floor(ry0 * px_per_pt)) - 1)
            c1, d1 = int(np.ceil(rx1 * px_per_pt)) + 1, int(np.ceil(ry1 * px_per_pt)) + 1
            keep_rows, keep_cols = rows[(rows >= d0) & (rows < d1)], cols[(cols >= c0) & (cols < c1)]
            saved_rows, saved_cols = img[keep_rows, c0:c1].copy(), img[d0:d1, keep_cols].copy()
            img[d0:d1, c0:c1] = colour.astype(np.uint8)
            img[keep_rows, c0:c1] = saved_rows
            img[d0:d1, keep_cols] = saved_cols


def picture_crossings(img: RGB, area: Box, colour: Floats, px_per_pt: float, ignore: Mask,
                      ring: int) -> tuple[Ints, Ints]:
    """The pixel rows and columns (image indices) where something drawn in the background runs
    into `area` across its edge: rows crossed on its left or right, columns on its top or
    bottom, in the ring `flat_colour` judged (a pixel either side for antialiasing)."""
    h, w = img.shape[:2]
    a0, b0 = max(0, int(area[0] * px_per_pt) - 2), max(0, int(area[1] * px_per_pt) - 2)
    a1, b1 = min(w, int(np.ceil(area[2] * px_per_pt)) + 2), min(h, int(np.ceil(area[3] * px_per_pt)) + 2)

    def drawn(ys: slice, xs: slice) -> Mask:
        return (np.abs(img[ys, xs, :3].astype(int) - colour[:3]).max(axis=-1) > 6) & ~ignore[ys, xs]

    rows = np.zeros(max(0, b1 - b0), bool)
    for xs in (slice(max(0, a0 - ring), a0), slice(a1, min(w, a1 + ring))):
        if xs.stop > xs.start and b1 > b0:
            rows |= drawn(slice(b0, b1), xs).any(axis=1)
    cols = np.zeros(max(0, a1 - a0), bool)
    for ys in (slice(max(0, b0 - ring), b0), slice(b1, min(h, b1 + ring))):
        if ys.stop > ys.start and a1 > a0:
            cols |= drawn(ys, slice(a0, a1)).any(axis=0)
    def grow(m: Mask) -> Mask:
        out = m.copy()
        out[1:] |= m[:-1]
        out[:-1] |= m[1:]
        return out

    return b0 + np.nonzero(grow(rows))[0], a0 + np.nonzero(grow(cols))[0]


def ink_colour(img: Pixels, rect_pt: list[float], px_per_pt: float) -> str | None:
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
    behind = ring_background(img, a0, b0, a1, b1, 3)
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
        img = page.render(GLYPH_INK_ZOOM, clip=clip, transparent=False)
    except PdfError:
        return None
    px = img[..., :3].astype(int)
    border = np.concatenate([px[0], px[-1], px[:, 0], px[:, -1]])
    distance = np.abs(px - np.median(border, axis=0)).sum(axis=2)
    if distance.max() <= 60:
        return None
    ink = distance > max(60, distance.max() / 2)

    def blob(profile: Ints) -> tuple[int, int]:
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


def ring_background(img: Pixels, a0: int, b0: int, a1: int, b1: int, r: int) -> Floats | None:
    """What is behind a small rectangle of pixels [b0:b1, a0:a1], from a ring `r` px wide around
    it: each column running from the strip above to the strip below, each row from the strip on
    the left to the one on the right, the two weighted by how well their ends agree. A bullet
    on a picture's edge or on a gradient (a colorbar) takes that edge and gradient through it;
    a flat median painted a square of the mixed colour there. (h, w, 3) floats, or None when
    the ring is off the image."""
    h, w = b1 - b0, a1 - a0
    if a0 < 0 or b0 < 0 or a1 > img.shape[1] or b1 > img.shape[0] or h <= 0 or w <= 0:
        return None

    def px(ys: slice, xs: slice) -> Floats:
        # (only the strips read are made floats: the whole page took 10 ms a bullet)
        return img[ys, xs, :3].astype(float)

    t = (np.arange(h) + 0.5) / h
    s = (np.arange(w) + 0.5) / w
    guesses: list[Floats] = []
    errors: list[float] = []
    if b0 >= r and b1 + r <= img.shape[0]:
        top = np.median(px(slice(b0 - r, b0), slice(a0, a1)), axis=0)
        bottom = np.median(px(slice(b1, b1 + r), slice(a0, a1)), axis=0)
        guesses.append(top[None] * (1 - t)[:, None, None] + bottom[None] * t[:, None, None])
        errors.append(np.abs(top - bottom).sum(axis=1).mean())
    if a0 >= r and a1 + r <= img.shape[1]:
        left = np.median(px(slice(b0, b1), slice(a0 - r, a0)), axis=1)
        right = np.median(px(slice(b0, b1), slice(a1, a1 + r)), axis=1)
        guesses.append(left[:, None] * (1 - s)[None, :, None] + right[:, None] * s[None, :, None])
        errors.append(np.abs(left - right).sum(axis=1).mean())
    if not guesses:
        return None
    weights = [1.0 / (e + 1.0) ** 2 for e in errors]  # (squared: an edge's 5% ghost showed)
    blended = guesses[0] * weights[0]
    for g, wt in zip(guesses[1:], weights[1:]):
        blended = blended + g * wt
    return blended / sum(weights)


def patch_rects(img: Pixels, rects_pt: list[list[float]], px_per_pt: float) -> None:
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
