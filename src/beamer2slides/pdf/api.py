"""The PDF backend contract: what the pipeline asks of a PDF library, and nothing more.

A backend is any object with `open(source) -> PdfDocument`; `beamer2slides.pdf` holds the one in
use (`set_backend`, `use_backend`, `$B2S_PDF_BACKEND`, docs/pdf-backend.md). The PDFium backend
(`pdfium_backend`) is the reference; `sandbox` runs any backend in another process.

Everything that crosses this boundary is plain data - numbers, strings, tuples, lists, dicts with
string keys, bytes, numpy arrays and the dataclasses below - so a backend can live in another
process, container or machine (`sandbox.py` sends it through a non-executable wire format). Page
objects are named by their **id**: their index in `PdfPage.objects()`, stable for the life of the
document. No handle, pointer or library object ever leaves a backend.

Coordinates are PDF points relative to the top left corner of the page's crop box (the visible
area, inherited boxes included), y down, as everywhere else in the pipeline. Text boxes and path
items follow the conventions the pipeline was tuned on:

- a character's box spans from its origin to its advance, between the font's ascender and
  descender, scaled up to a full em when the two add up to less (`char_box`);
- paths are lists of items: ("l", p0, p1) line, ("c", p0, p1, p2, p3) curve, ("re", (x0, y0, x1,
  y1)) axis-aligned rectangle (a closed path of three lines plus the closing one, horizontal edge
  first), ("qu", (p0, p1, p2, p3)) quadrilateral (a stroked path of four lines ending where it
  started); a path both filled and stroked with the same items is one drawing of type "fs".
  `trace` turns move/line/curve segments into those items; a backend should use it.

`tests/test_pdf_backend.py` is the conformance suite: a backend that passes it can replace PDFium.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence, TypedDict, runtime_checkable

import numpy as np

Box = tuple[float, float, float, float]
Matrix = tuple[float, float, float, float, float, float]  # [a b c d e f], PDF row-vector convention

OBJ_TEXT, OBJ_PATH, OBJ_IMAGE, OBJ_SHADING, OBJ_FORM = 1, 2, 3, 4, 5
SEG_LINE, SEG_BEZIER, SEG_MOVE = 0, 1, 2
LIGATURES = {"ff": "ﬀ", "fi": "ﬁ", "fl": "ﬂ", "ffi": "ﬃ", "ffl": "ﬄ", "st": "ﬆ"}
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NO_OBJECT = -1  # Char.obj of a character no text object draws (a synthetic space)


class PdfError(Exception):
    """A PDF the backend cannot read, or a request it refuses."""


# ---------------------------------------------------------------------- data


@dataclass
class Char:
    """One character as drawn: PDFium's generated spaces and line breaks are not characters."""

    c: str              # one character, or several for one glyph (a ligature: "ﬁ", surrogates joined)
    font: str           # base font name, subset prefix removed; "Type3" for a Type 3 font
    size: float         # font size as drawn (text matrix included)
    color: int          # fill colour 0xRRGGBB
    alpha: int          # fill alpha 0-255
    origin: tuple[float, float]
    box: Box            # char_box of origin, dir, advance, size, ascent and descent
    dir: tuple[float, float]  # unit baseline direction, y down
    obj: int            # id of the text object drawing it; NO_OBJECT for synthetic characters
    font_id: int = -1   # page-local font key for PdfPage.glyph_widths; -1 when unknown
    advance: float = 0.0
    synthetic: bool = False   # made by the pipeline (a word space), not drawn
    ascent: float = 0.8       # em, positive
    descent: float = -0.2     # em, negative
    exact_advance: bool = True  # the advance of the glyph actually drawn (not the font's default glyph's)


@dataclass
class PageObject:
    """A page object, form XObject contents included. `id` is its index in `objects()`."""

    id: int
    type: int           # OBJ_*
    matrix: Matrix      # object space -> page space (y down)
    parent: int | None = None       # id of the form it is drawn in
    children: list[int] = field(default_factory=list)  # ids, for a form


@dataclass
class EmbeddedImage:
    """One image XObject as it is drawn on a page: its own file data and pixels, plus what
    decides whether that data may stand in for a render of the page (see render.image_file).

    `pixels` is the library's own decode of the image (its pixel grid, top row first, colour space
    converted, but *without* the image's soft mask); `rendered` applies the object's matrix,
    mask, alpha and clip, at the size the library picks (about page resolution), and is what says
    whether anything is see-through. `raw` is the stream as stored: the author's JPEG file for
    DCTDecode."""

    px: tuple[int, int]                 # the image's own pixel size
    box: Box                            # what shows of it, page space (as PdfPage.images gives it)
    matrix: Matrix                      # unit square -> page space
    filters: list[str]
    colorspace: str                     # a COLOR_SPACES value
    bpp: int
    dpi: tuple[float, float]
    raw: bytes
    decoded_size: int
    clipped: bool                       # a clip path cuts the drawn box
    upright: bool                       # axis aligned and not mirrored (a y flip is the PDF norm)
    blended: bool                       # drawn with a constant alpha or a blend mode
    transparent: bool                   # anything see-through: a soft mask, a stencil mask, `blended`
    pixels: np.ndarray | None = None    # uint8, h x w x 3 or 4
    rendered: np.ndarray | None = None  # uint8, h x w x 3 or 4

    @property
    def jpeg(self) -> bytes:
        """The embedded stream when it is a plain JPEG file (DCTDecode and nothing else)."""
        return self.raw if self.filters == ["DCTDecode"] and self.raw[:3] == JPEG_MAGIC else b""


COLOR_SPACES = {0: "unknown", 1: "DeviceGray", 2: "DeviceRGB", 3: "DeviceCMYK", 4: "CalGray",
                5: "CalRGB", 6: "Lab", 7: "ICCBased", 8: "Separation", 9: "DeviceN",
                10: "Indexed", 11: "Pattern"}


class Drawing(TypedDict, total=False):
    """One painted path. "f" fill, "s" stroke, "fs" both with the same items."""

    type: str
    items: list[tuple]
    rect: Box                   # bounds of the items' points (curves by their extremes)
    object: int                 # id of the path object
    fill: tuple[float, float, float] | None    # 0-1 RGB ("f", "fs")
    fill_opacity: float
    even_odd: bool
    soft_mask: bool             # transparency without alpha: a soft mask or a blend mode
    color: tuple[float, float, float] | None   # 0-1 RGB stroke ("s", "fs")
    stroke_opacity: float
    width: float                # stroke width as drawn


class ImageInfo(TypedDict):
    """An image or a shading. A shading paints its clip area and is reported on whole points,
    one pixel per point; an image's box is what its clips let show."""

    bbox: Box
    width: int
    height: int
    object: int                 # id


class Link(TypedDict, total=False):
    bbox: Box
    page: int                   # an internal link's target page index
    uri: str                    # an external link


# ---------------------------------------------------------------------- the contract


@runtime_checkable
class PdfPage(Protocol):
    index: int
    width: float
    height: float

    @property
    def rect(self) -> Box:
        """(0, 0, width, height)."""

    def objects(self) -> list[PageObject]:
        """All page objects in painting order, form XObject contents included (the forms
        themselves too, before their contents). The list and the ids never change."""

    def object_bounds(self) -> list[Box]:
        """Each object's bounding box in page space, aligned with `objects()`: stroke widths
        included, a path's curves bounded by their extremes (not their control points)."""

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        """Switch objects off (or on again) for `render`, `drawings` and `images`. Nothing is
        rewritten: the page is the same once they are on again."""

    def chars(self) -> list[Char]:
        """The characters drawn on the page, in content order (a big operator's limits and
        accents in the order the content stream draws them, not reading order)."""

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        """For each (font_id, character, size): the advance of the font's default glyph for that
        character (its lowest character code), or None when the font has none."""

    def drawings(self) -> list[Drawing]:
        """Active path objects as drawings, in painting order."""

    def images(self) -> list[ImageInfo]:
        """Active images and shadings, in painting order."""

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        """Everything the library knows about one image object (None for anything else)."""

    def links(self) -> list[Link]:
        """Link annotations with a target page or a URI."""

    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False) -> np.ndarray:
        """uint8 pixels of the page (or of `clip`, page space), `zoom` pixels per point, pixel
        bounds rounded outwards (`pixel_bounds`): h x w x 3 on white, or h x w x 4 on a
        transparent ground if `transparent`. Annotations are drawn; inactive objects are not.
        Page space is unrotated like every other call: /Rotate is not applied (`render_matrix`).
        A backend that cannot draw (`renders(backend)` is False) raises PdfError; one that draws
        only some pages raises it for the others."""


@runtime_checkable
class PdfDocument(Protocol):
    path: Path | None

    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> PdfPage:
        """Page `index` (negative counts from the end); the same object each time."""

    def __iter__(self): ...

    @property
    def metadata(self) -> dict:
        """{"title": str, "producer": str}, "" when absent."""

    def label(self, index: int) -> str:
        """The page label ("" when the document has none; possibly a raw <FEFF...> string)."""

    def named_dests(self) -> list[tuple[str, int]]:
        """(name, page index) of the named destinations; -1 for a deleted page."""

    def save(self, pages: Sequence[int] | None = None, boxes: dict[int, Box] | None = None) -> bytes:
        """A new PDF file: only `pages` (original indices, in document order; all when None),
        each page in `boxes` cut to that area (page space) as its media and crop box. The
        document itself is left as it is."""

    def close(self) -> None: ...


@runtime_checkable
class PdfBackend(Protocol):
    name: str

    def open(self, source: str | Path | bytes) -> PdfDocument:
        """A PDF file by path, or its bytes. Raises PdfError for anything unreadable."""


def renders(backend) -> bool:
    """Whether a backend draws pages. One that does not says so with `renders = False` (the pure
    Python reader: extract and classify run on it; render, fidelity and the checks do not);
    `render` then raises PdfError where it cannot draw, and `EmbeddedImage.pixels`/`rendered` may be
    None."""
    return bool(getattr(backend, "renders", True))


# ---------------------------------------------------------------------- helpers every backend shares


def char_box(ox, oy, ux, uy, advance, size, ascent, descent) -> Box:
    vx, vy = uy, -ux  # "up" in glyph space
    xs, ys = [], []
    for along in (0.0, advance):
        for up in (ascent * size, descent * size):
            xs.append(ox + ux * along + vx * up)
            ys.append(oy + uy * along + vy * up)
    return min(xs), min(ys), max(xs), max(ys)


def cff_font_bbox(data: bytes) -> list[float] | None:
    """FontBBox from the top DICT of a bare CFF font program (FontFile3/Type1C)."""
    try:
        pos = data[2]

        def index(pos):  # -> (entries, position after the INDEX)
            count = int.from_bytes(data[pos:pos + 2], "big")
            if count == 0:
                return [], pos + 2
            size = data[pos + 2]
            offsets = [int.from_bytes(data[pos + 3 + i * size:pos + 3 + (i + 1) * size], "big") for i in range(count + 1)]
            base = pos + 2 + (count + 1) * size
            return [data[base + offsets[i]:base + offsets[i + 1]] for i in range(count)], base + offsets[-1]

        _, pos = index(pos)  # names
        tops, _ = index(pos)
        d, i, operands = tops[0], 0, []
        while i < len(d):
            b0 = d[i]
            if b0 <= 21:  # operator
                if b0 == 5:
                    return operands[:4]
                i += 2 if b0 == 12 else 1
                operands = []
            elif b0 == 28:
                operands.append(int.from_bytes(d[i + 1:i + 3], "big", signed=True)); i += 3
            elif b0 == 29:
                operands.append(int.from_bytes(d[i + 1:i + 5], "big", signed=True)); i += 5
            elif b0 == 30:  # real: nibbles up to 0xf
                i += 1
                while not (d[i] & 0x0F == 0x0F or d[i] >> 4 == 0x0F):
                    i += 1
                operands.append(0.0); i += 1
            elif b0 <= 246:
                operands.append(b0 - 139); i += 1
            elif b0 <= 250:
                operands.append((b0 - 247) * 256 + d[i + 1] + 108); i += 2
            else:
                operands.append(-(b0 - 251) * 256 - d[i + 1] - 108); i += 2
        return [0, 0, 0, 0]  # not given: the default
    except (IndexError, ValueError):
        return None


def font_metrics(ascent: float, descent: float, program: bytes) -> tuple[float, float]:
    """A font's ascent and descent (em) as Char carries them, from what the font reports and its
    program: the rules every backend applies the same way."""
    bbox = cff_font_bbox(program) if program[:1] == b"\x01" else None
    if bbox and abs(ascent - bbox[3] / 1000) < 1e-3 and abs(descent - bbox[1] / 1000) < 1e-3 \
            and ascent - descent > 1.6:
        # Metrics from the bounding box of a math font (xdvipdfmx: CMSY, CMEX) would give every
        # glyph a box reaching far below the line; MuPDF uses its defaults there.
        ascent, descent = 0.8, -0.2
    if ascent < 1e-3 or ascent == descent:     # (a font may say ascent = descent: no height at all)
        ascent, descent = 0.9, -0.1
    if ascent - descent < 1:
        total = ascent - descent
        ascent, descent = ascent / total, descent / total
    return ascent, descent


def pixel_bounds(zoom: float, box: Box) -> tuple[int, int, int, int]:
    """The pixels `render` covers for a box: (ix0, iy0, width, height), rounded outwards."""
    x0, y0, x1, y1 = box
    ix0, iy0 = math.floor(x0 * zoom + 0.001), math.floor(y0 * zoom + 0.001)
    ix1, iy1 = math.ceil(x1 * zoom - 0.001), math.ceil(y1 * zoom - 0.001)
    return ix0, iy0, max(1, ix1 - ix0), max(1, iy1 - iy0)


def render_matrix(zoom: float, ix0: int, iy0: int, rotation: int, width: float, height: float) -> Matrix:
    """The FS_MATRIX for FPDF_RenderPageBitmapWithMatrix that draws the page in *our* page space
    (y down, crop box origin, `rect`), `zoom` pixels per point, pixel (ix0, iy0) at the origin.
    PDFium puts the page's display matrix first, and that one turns the page by /Rotate
    (`rotation` = quarter turns, as FPDFPage_GetRotation), while every other call of the contract
    speaks unrotated page space: this undoes the turn, so a render lines up with the geometry.
    `width`/`height`: the unrotated page size. Computed in double; the backends hand the same
    numbers to PDFium (the pure renderer then composes them as PDFium does, in float32)."""
    z, w, h = zoom, width, height
    if rotation == 1:   # display (u, v) = (y - bottom, x - left): ours = (v, h - u)
        return 0.0, -z, z, 0.0, -ix0, z * h - iy0
    if rotation == 2:   # (right - x, y - bottom): ours = (w - u, h - v)
        return -z, 0.0, 0.0, -z, z * w - ix0, z * h - iy0
    if rotation == 3:   # (top - y, right - x): ours = (w - v, u)
        return 0.0, z, -z, 0.0, z * w - ix0, -iy0
    return z, 0.0, 0.0, z, -ix0, -iy0


def join_surrogates(s: str) -> str:
    """A character outside the BMP (an emoji, a mathematical alphanumeric letter) comes back from
    a UTF-16 text layer as its two surrogates. Join them into the character itself - lone
    surrogates cannot be encoded as UTF-8 and would break every JSON file downstream."""
    try:
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeDecodeError:  # a surrogate without its partner: no character at all
        return "".join(chr(0xFFFD) if "\ud800" <= u <= "\udfff" else u for u in s)


def mul(m: tuple, n: tuple) -> Matrix:
    """m then n (PDF row-vector convention: [a b c d e f])."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def transform_box(box: tuple, m: tuple) -> Box:
    x0, y0, x1, y1 = box
    pts = [(m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]) for x in (x0, x1) for y in (y0, y1)]
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def _commands(segments) -> list[tuple]:
    """Path segments (kind, x, y, closes) as move / line / curve / close commands. A subpath is
    closed with a line back to its start carrying the close flag; that line is the close itself.
    Degenerate curves become lines and lines that go nowhere are dropped."""
    out: list[tuple] = []
    start = current = None
    bezier: list = []
    for kind, x, y, closes in segments:
        p = (x, y)
        if kind == SEG_MOVE:
            if out and out[-1][0] == "m":
                out.pop()
            out.append(("m", p))
            start = current = p
            bezier = []
            continue
        if kind == SEG_BEZIER:
            bezier.append(p)
            if len(bezier) < 3:
                continue
            p1, p2, p3 = bezier
            bezier = []
            if current is not None and ((current == p1 and (p2 == p3 or p1 == p2)) or (p2 == p3 and p1 == p2)):
                kind = SEG_LINE
            else:
                out.append(("c", p1, p2, p3))
                current = p3
                if closes:
                    out.append(("h",))
                    current = start
                continue
        if closes and p == start:
            if not (out and out[-1][0] == "h"):
                out.append(("h",))
            current = start
            continue
        if p == current and out and out[-1][0] != "m":
            if closes:
                out.append(("h",))
                current = start
            continue
        out.append(("l", p))
        current = p
        if closes:
            out.append(("h",))
            current = start
    return out


def curve_extremes(p0, p1, p2, p3) -> list[tuple[float, float]]:
    """The points bounding a cubic Bézier curve: its ends and where it turns in x or y. (Its
    control points can lie far outside: a curved arrow's reach up into the frame title.)"""
    out = [p3]
    for k in (0, 1):
        a = -p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]
        b = 2 * (p0[k] - 2 * p1[k] + p2[k])
        c = p1[k] - p0[k]
        if abs(a) < 1e-9:
            roots = [-c / b] if abs(b) > 1e-9 else []
        else:
            disc = b * b - 4 * a * c
            roots = [(-b + s * math.sqrt(disc)) / (2 * a) for s in (1, -1)] if disc >= 0 else []
        for t in roots:
            if 0 < t < 1:
                u = 1 - t
                out.append(tuple(u ** 3 * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t ** 3 * p3[i]
                                 for i in (0, 1)))
    return out


def trace(segments, filled: bool):
    """Path items and the bounding box of their points, for one way of painting the path, from
    segments (SEG_* kind, x, y, closes) in page space. None for a path with nothing to draw."""
    items: list[tuple] = []
    rect = None
    first = last = (0.0, 0.0)
    have_move = False
    lines = 0  # consecutive lines

    def include(p):
        nonlocal rect
        rect = [p[0], p[1], p[0], p[1]] if rect is None else \
            [min(rect[0], p[0]), min(rect[1], p[1]), max(rect[2], p[0]), max(rect[3], p[1])]

    for cmd in _commands(segments):
        if cmd[0] == "m":
            last = first = cmd[1]
            if rect is None:
                include(last)
            have_move, lines = True, 0
        elif cmd[0] == "l":
            p = cmd[1]
            include(p)
            items.append(("l", last, p))
            last = p
            lines += 1
            if lines == 4 and not filled and _to_quad(items):
                lines = 0
        elif cmd[0] == "c":
            lines = 0
            for q in curve_extremes(last, *cmd[1:]):
                include(q)
            items.append(("c", last, *cmd[1:]))
            last = cmd[3]
        else:  # close
            if lines == 3:
                lines = 0
                if _to_rect(items):
                    continue
            lines = 0
            if have_move and last != first:
                items.append(("l", last, first))
                last = first
            have_move = False
    if not items:
        return None
    return items, tuple(rect)


def _to_rect(items: list) -> bool:
    """The last three lines plus the closing line form an axis-aligned rectangle drawn the way
    the PDF `re` operator draws one (horizontal edge first): one "re" item. Rectangles drawn
    as explicit lines starting with a vertical edge (PGF's) stay four lines."""
    (_, p0, p1), (_, _, p2), (_, _, p3) = items[-3:]
    if not (p0[1] == p1[1] and p1[0] == p2[0] and p2[1] == p3[1] and p3[0] == p0[0]):
        return False
    xs, ys = [p0[0], p2[0]], [p0[1], p2[1]]
    del items[-3:]
    items.append(("re", (min(xs), min(ys), max(xs), max(ys))))
    return True


def _to_quad(items: list) -> bool:
    """Four lines of a stroked path that end where they started: one "qu" item."""
    last4 = items[-4:]
    if last4[-1][2] != last4[0][1]:
        return False
    del items[-4:]
    items.append(("qu", tuple(line[1] for line in last4)))
    return True
