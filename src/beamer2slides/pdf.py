"""PDF access through PDFium (pypdfium2): characters, vector paths, images, links, page
labels and rendering.

Coordinates are PDF points relative to the top left corner of the page's crop box, y down,
as everywhere else in the pipeline. Text boxes and path items follow the conventions the
pipeline was tuned on:

- a character's box spans from its origin to its advance, between the font's ascender and
  descender, scaled up to a full em when the two add up to less;
- paths are lists of items: "l" line, "c" curve, "re" axis-aligned rectangle (a closed
  path of three lines plus the closing one, horizontal edge first), "qu" quadrilateral (a
  stroked path of four lines ending where it started); a path both filled and stroked with the
  same items is one path of type "fs".
"""

import ctypes
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as R

OBJ_TEXT, OBJ_PATH, OBJ_IMAGE, OBJ_SHADING, OBJ_FORM = 1, 2, 3, 4, 5
SEG_LINE, SEG_BEZIER, SEG_MOVE = 0, 1, 2
LIGATURES = {"ff": "ﬀ", "fi": "ﬁ", "fl": "ﬂ", "ffi": "ﬃ", "ffl": "ﬄ", "st": "ﬆ"}


def _addr(handle) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


def _mul(m: tuple, n: tuple) -> tuple:
    """m then n (PDF row-vector convention: [a b c d e f])."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _obj_matrix(obj) -> tuple:
    m = R.FS_MATRIX()
    if not R.FPDFPageObj_GetMatrix(obj, m):
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _cff_font_bbox(data: bytes) -> list[float] | None:
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


def _font_program(font) -> bytes:
    n = ctypes.c_size_t()
    if not R.FPDFFont_GetFontData(font, None, 0, n) or not n.value:
        return b""
    buf = ctypes.create_string_buffer(n.value)
    R.FPDFFont_GetFontData(font, ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), n.value, n)
    return buf.raw


def _transform_box(box: tuple, m: tuple) -> tuple:
    x0, y0, x1, y1 = box
    pts = [(m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]) for x in (x0, x1) for y in (y0, y1)]
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def _is_active(obj) -> bool:
    active = ctypes.c_long()
    return bool(R.FPDFPageObj_GetIsActive(obj, active)) and bool(active.value)


def _rgba(getter, obj) -> tuple[int, int, int, int] | None:
    r, g, b, a = (ctypes.c_ulong() for _ in range(4))
    if not getter(obj, r, g, b, a):
        return None
    return r.value, g.value, b.value, a.value


@dataclass
class Char:
    c: str
    font: str
    size: float
    color: int          # 0xRRGGBB
    alpha: int
    origin: tuple[float, float]
    box: tuple[float, float, float, float]
    dir: tuple[float, float]
    obj: int            # address of the text object drawing it (0 for synthetic spaces)
    font_handle: object = None
    advance: float = 0.0
    synthetic: bool = False
    ascent: float = 0.8
    descent: float = -0.2
    exact_advance: bool = True  # the advance of the glyph actually drawn (not the font's default glyph's)


@dataclass
class PageObject:
    handle: object
    type: int
    matrix: tuple       # object space -> page space (y down)
    parent: object = None
    children: list = field(default_factory=list)


class Page:
    def __init__(self, doc: "Document", index: int):
        self.doc = doc
        self.index = index
        self.page = doc.pdf[index]
        self.raw = self.page.raw
        box = R.FS_RECTF()  # the visible area: crop box within media box, inherited boxes included
        R.FPDF_GetPageBoundingBox(self.raw, box)
        left, bottom, right, top = box.left, box.bottom, box.right, box.top
        self.left, self.top = left, top
        self.width, self.height = right - left, top - bottom
        # PDF user space (y up) -> our page space (y down, crop box origin)
        self.to_page = (1.0, 0.0, 0.0, -1.0, -left, top)
        self._objects = None
        self._textpage = None

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return 0.0, 0.0, self.width, self.height

    def point(self, x: float, y: float) -> tuple[float, float]:
        return x - self.left, self.top - y

    # ------------------------------------------------------------------ objects

    def objects(self) -> list[PageObject]:
        """All page objects in painting order, form XObject contents included (the forms
        themselves too, before their contents)."""
        if self._objects is None:
            out: list[PageObject] = []

            def walk(count, get, parent_matrix, parent):
                for i in range(count):
                    obj = get(i)
                    kind = R.FPDFPageObj_GetType(obj)
                    matrix = _mul(_obj_matrix(obj), parent_matrix)
                    po = PageObject(obj, kind, matrix, parent)
                    out.append(po)
                    if parent is not None:
                        parent.children.append(po)
                    if kind == OBJ_FORM:
                        walk(R.FPDFFormObj_CountObjects(obj), lambda j, o=obj: R.FPDFFormObj_GetObject(o, j), matrix, po)

            walk(R.FPDFPage_CountObjects(self.raw), lambda i: R.FPDFPage_GetObject(self.raw, i), self.to_page, None)
            self._objects = out
        return self._objects

    def set_active(self, objects, active: bool) -> None:
        for po in objects:
            R.FPDFPageObj_SetIsActive(po.handle, active)

    # ------------------------------------------------------------------ text

    def textpage(self):
        if self._textpage is None:
            self._textpage = self.page.get_textpage()
        return self._textpage

    def chars(self) -> list[Char]:
        """The characters drawn on the page, in content order (PDFium's generated spaces and
        line breaks left out)."""
        tp = self.textpage().raw
        n = R.FPDFText_CountChars(tp)
        name_buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        fonts: dict[int, tuple[str, float, float]] = {}
        x, y = ctypes.c_double(), ctypes.c_double()
        loose = R.FS_RECTF()
        m = R.FS_MATRIX()
        r, g, b, a = (ctypes.c_ulong() for _ in range(4))
        tl, tr, tb, tt = (ctypes.c_double() for _ in range(4))
        out: list[Char] = []
        for i in range(n):
            u = R.FPDFText_GetUnicode(tp, i)
            if R.FPDFText_IsGenerated(tp, i):
                continue
            obj = R.FPDFText_GetTextObject(tp, i)
            font = R.FPDFTextObj_GetFont(obj) if obj else None
            key = _addr(font)
            if key not in fonts:
                length = R.FPDFText_GetFontInfo(tp, i, name_buf, 256, flags)
                name = name_buf.raw[:max(0, length - 1)].decode("utf-8", "replace")
                if len(name) > 7 and name[6] == "+":
                    name = name[7:]
                if font and R.FPDFFont_GetBaseFontName(font, None, 0) <= 1:
                    name = "Type3"  # no base font: a Type 3 font (glyphs drawn by procedures)
                asc, dsc = ctypes.c_float(), ctypes.c_float()
                if font:
                    R.FPDFFont_GetAscent(font, ctypes.c_float(1.0), asc)
                    R.FPDFFont_GetDescent(font, ctypes.c_float(1.0), dsc)
                ascent, descent = asc.value, dsc.value
                program = _font_program(font) if font else b""
                bbox = _cff_font_bbox(program) if program[:1] == b"\x01" else None
                if bbox and abs(ascent - bbox[3] / 1000) < 1e-3 and abs(descent - bbox[1] / 1000) < 1e-3 \
                        and ascent - descent > 1.6:
                    # Metrics from the bounding box of a math font (xdvipdfmx: CMSY, CMEX)
                    # would give every glyph a box reaching far below the line; MuPDF uses
                    # its defaults there.
                    ascent, descent = 0.8, -0.2
                if ascent < 1e-3:
                    ascent, descent = 0.9, -0.1
                if ascent - descent < 1:
                    total = ascent - descent
                    ascent, descent = ascent / total, descent / total
                fonts[key] = (name, ascent, descent)
            name, ascent, descent = fonts[key]
            text = chr(u)
            if u == 0 or R.FPDFText_HasUnicodeMapError(tp, i) and name != "Type3":
                text = chr(0xFFFD)  # no Unicode for the glyph (TeX math extension fonts): not its character code
            R.FPDFText_GetCharOrigin(tp, i, x, y)
            R.FPDFText_GetLooseCharBox(tp, i, loose)
            R.FPDFText_GetMatrix(tp, i, m)
            size = R.FPDFText_GetFontSize(tp, i) * math.sqrt(abs(m.a * m.d - m.b * m.c))  # as drawn
            norm = math.hypot(m.a, m.b) or 1.0
            ux, uy = m.a / norm, -m.b / norm  # baseline direction, y down
            ox, oy = self.point(x.value, y.value)
            # PDFium's loose box reaches to the advance or to the glyph's ink, whichever is
            # further (an italic f overhangs). Where the ink stops short, it is the advance;
            # otherwise the font's width for the character is (alternate glyphs aside).
            advance = loose_advance = abs((loose.right - loose.left) * ux) + abs((loose.top - loose.bottom) * uy)
            exact = True
            if ux > 0.999:
                R.FPDFText_GetCharBox(tp, i, tl, tr, tb, tt)
                if tr.value >= loose.right - 0.01:
                    width = ctypes.c_float()
                    if font and R.FPDFFont_GetGlyphWidth(font, u, ctypes.c_float(size), width) and width.value > 0:
                        advance, exact = width.value, False
            R.FPDFText_GetFillColor(tp, i, r, g, b, a)
            color = (r.value << 16) | (g.value << 8) | b.value
            box = self.char_box(ox, oy, ux, uy, advance, size, ascent, descent)
            prev = out[-1] if out else None
            if prev and prev.obj == _addr(obj) and prev.origin == (ox, oy):
                # One glyph for several characters (a ligature): PDFium repeats the glyph's
                # position and box for each of them. The glyph's advance is the loose box.
                prev.c = LIGATURES.get(prev.c + text, prev.c + text)
                prev.advance = max(prev.advance, loose_advance)
                prev.exact_advance = True
                prev.box = self.char_box(ox, oy, ux, uy, prev.advance, size, ascent, descent)
                continue
            out.append(Char(text, name, size, color, a.value, (ox, oy), box, (ux, uy), _addr(obj), font, advance,
                            ascent=ascent, descent=descent, exact_advance=exact))
        # PDFium's text page puts the objects of a line in reading order; keep content order
        # (a big operator's limits, accents), as the span rules expect.
        order = {_addr(po.handle): k for k, po in enumerate(self.objects())}
        return [ch for _, ch in sorted(enumerate(out), key=lambda e: (order.get(e[1].obj, -1), e[0]))]

    @staticmethod
    def char_box(ox, oy, ux, uy, advance, size, ascent, descent):
        vx, vy = uy, -ux  # "up" in glyph space
        xs, ys = [], []
        for along in (0.0, advance):
            for up in (ascent * size, descent * size):
                xs.append(ox + ux * along + vx * up)
                ys.append(oy + uy * along + vy * up)
        return min(xs), min(ys), max(xs), max(ys)

    def glyph_width(self, font, c: str, size: float) -> float | None:
        """Advance of the font's default glyph for a character (its lowest character code)."""
        w = ctypes.c_float()
        if not font or not R.FPDFFont_GetGlyphWidth(font, ord(c), ctypes.c_float(size), w):
            return None
        return w.value

    # ------------------------------------------------------------------ paths

    def drawings(self) -> list[dict]:
        out: list[dict] = []
        for po in self.objects():
            if po.type != OBJ_PATH or not _is_active(po.handle):
                continue
            fillmode, stroke = ctypes.c_int(), ctypes.c_int()
            if not R.FPDFPath_GetDrawMode(po.handle, fillmode, stroke):
                continue
            segments = self._segments(po)
            if fillmode.value:
                fill = _rgba(R.FPDFPageObj_GetFillColor, po.handle)
                path = _trace(segments, filled=True)
                if path:
                    items, rect = path
                    opacity = fill[3] / 255 if fill else 1.0
                    out.append({"type": "f", "items": items, "rect": rect, "even_odd": fillmode.value == 1,
                                "fill": tuple(v / 255 for v in fill[:3]) if fill else None,
                                "fill_opacity": opacity, "object": po,
                                # transparency without alpha: a soft mask (or a blend mode)
                                "soft_mask": opacity == 1.0 and bool(R.FPDFPageObj_HasTransparency(po.handle))})
            if stroke.value:
                color = _rgba(R.FPDFPageObj_GetStrokeColor, po.handle)
                width = ctypes.c_float()
                R.FPDFPageObj_GetStrokeWidth(po.handle, width)
                a, b, c, d, _, _ = po.matrix
                path = _trace(segments, filled=False)
                if path:
                    items, rect = path
                    entry = {"type": "s", "items": items, "rect": rect,
                             "color": tuple(v / 255 for v in color[:3]) if color else None,
                             "stroke_opacity": color[3] / 255 if color else 1.0,
                             "width": width.value * math.sqrt(abs(a * d - b * c)), "object": po}
                    prev = out[-1] if out else None
                    if prev and prev["type"] == "f" and prev["items"] == items:
                        for k, v in entry.items():
                            prev.setdefault(k, v)
                        prev["type"] = "fs"
                    else:
                        out.append(entry)
        return out

    def _segments(self, po: PageObject) -> list[tuple[int, float, float, bool]]:
        a, b, c, d, e, f = po.matrix
        x, y = ctypes.c_float(), ctypes.c_float()
        out = []
        for i in range(R.FPDFPath_CountSegments(po.handle)):
            seg = R.FPDFPath_GetPathSegment(po.handle, i)
            R.FPDFPathSegment_GetPoint(seg, x, y)
            px, py = x.value, y.value
            out.append((R.FPDFPathSegment_GetType(seg), a * px + c * py + e, b * px + d * py + f,
                        bool(R.FPDFPathSegment_GetClose(seg))))
        return out

    # ------------------------------------------------------------------ images, links

    def _container_matrix(self, po: PageObject) -> tuple:
        return po.parent.matrix if po.parent else self.to_page

    def bounds(self, po: PageObject) -> tuple[float, float, float, float]:
        """An object's bounding box in page space (stroke widths included)."""
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        if not R.FPDFPageObj_GetBounds(po.handle, l, b, r, t):
            return 0.0, 0.0, 0.0, 0.0
        return _transform_box((l.value, b.value, r.value, t.value), self._container_matrix(po))

    def _clip_box(self, po: PageObject) -> tuple | None:
        """Bounding box of an object's clip path in page space (paths of a clip intersect)."""
        clip = R.FPDFPageObj_GetClipPath(po.handle)
        if not clip:
            return None
        m = self._container_matrix(po)
        x, y = ctypes.c_float(), ctypes.c_float()
        box = None
        for k in range(R.FPDFClipPath_CountPaths(clip)):
            pts = []
            for s in range(R.FPDFClipPath_CountPathSegments(clip, k)):
                R.FPDFPathSegment_GetPoint(R.FPDFClipPath_GetPathSegment(clip, k, s), x, y)
                pts.append((x.value, y.value))
            if not pts:
                continue
            b = _transform_box((min(p[0] for p in pts), min(p[1] for p in pts),
                                max(p[0] for p in pts), max(p[1] for p in pts)), m)
            box = b if box is None else (max(box[0], b[0]), max(box[1], b[1]), min(box[2], b[2]), min(box[3], b[3]))
        return box

    def images(self) -> list[dict]:
        out = []
        w, h = ctypes.c_ulong(), ctypes.c_ulong()
        for po in self.objects():
            if po.type == OBJ_SHADING and _is_active(po.handle):
                # A shading paints its clip area. Like MuPDF, report it as an image on whole
                # points, one pixel per point.
                box = self.bounds(po)
                node = po
                while node is not None:  # the shading's clip and those of the forms around it
                    clip = self._clip_box(node)
                    if clip:
                        box = (max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3]))
                    node = node.parent
                x0, y0 = math.floor(box[0] + 1e-3), math.floor(box[1] + 1e-3)
                x1, y1 = math.ceil(box[2] - 1e-3), math.ceil(box[3] - 1e-3)
                if x1 > x0 and y1 > y0:
                    out.append({"bbox": (x0, y0, x1, y1), "width": x1 - x0, "height": y1 - y0, "object": po})
                continue
            if po.type != OBJ_IMAGE or not _is_active(po.handle):
                continue
            a, b, c, d, e, f = po.matrix
            xs = [e, a + e, c + e, a + c + e]
            ys = [f, b + f, d + f, b + d + f]
            R.FPDFImageObj_GetImagePixelSize(po.handle, w, h)
            out.append({"bbox": (min(xs), min(ys), max(xs), max(ys)), "width": w.value, "height": h.value, "object": po})
        return out

    def links(self) -> list[dict]:
        pdf = self.doc.pdf.raw
        out = []
        pos = ctypes.c_int(0)
        link = R.FPDF_LINK()
        rect = R.FS_RECTF()
        while R.FPDFLink_Enumerate(self.raw, pos, link):
            if not R.FPDFLink_GetAnnotRect(link, rect):
                continue
            x0, y0 = self.point(rect.left, rect.top)
            x1, y1 = self.point(rect.right, rect.bottom)
            bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            dest = R.FPDFLink_GetDest(pdf, link)
            action = R.FPDFLink_GetAction(link)
            if not dest and action and R.FPDFAction_GetType(action) == R.PDFACTION_GOTO:
                dest = R.FPDFAction_GetDest(pdf, action)
            if dest:
                page = R.FPDFDest_GetDestPageIndex(pdf, dest)
                if page >= 0:
                    out.append({"bbox": bbox, "page": page})
            elif action and R.FPDFAction_GetType(action) == R.PDFACTION_URI:
                size = R.FPDFAction_GetURIPath(pdf, action, None, 0)
                buf = ctypes.create_string_buffer(size)
                R.FPDFAction_GetURIPath(pdf, action, buf, size)
                uri = buf.raw[:max(0, size - 1)].decode("utf-8", "replace")
                if uri:
                    out.append({"bbox": bbox, "uri": uri})
        return out

    # ------------------------------------------------------------------ rendering

    def render(self, zoom: float, clip: tuple[float, float, float, float] | None = None) -> np.ndarray:
        """RGB pixels (uint8, h x w x 3) of the page or of a clip rectangle, `zoom` pixels per
        point, on white. Pixel bounds round outwards."""
        x0, y0, x1, y1 = clip if clip is not None else self.rect
        ix0, iy0 = math.floor(x0 * zoom + 0.001), math.floor(y0 * zoom + 0.001)
        ix1, iy1 = math.ceil(x1 * zoom - 0.001), math.ceil(y1 * zoom - 0.001)
        w, h = max(1, ix1 - ix0), max(1, iy1 - iy0)
        bitmap = R.FPDFBitmap_Create(w, h, 0)
        try:
            R.FPDFBitmap_FillRect(bitmap, 0, 0, w, h, 0xFFFFFFFF)
            matrix = R.FS_MATRIX(zoom, 0, 0, zoom, -ix0, -iy0)
            clipping = R.FS_RECTF(0, 0, w, h)
            R.FPDF_RenderPageBitmapWithMatrix(bitmap, self.raw, matrix, clipping, R.FPDF_ANNOT)
            stride = R.FPDFBitmap_GetStride(bitmap)
            buf = R.FPDFBitmap_GetBuffer(bitmap)
            data = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), shape=(h * stride,))
            bgrx = data.reshape(h, stride)[:, :w * 4].reshape(h, w, 4)
            return bgrx[..., 2::-1].copy()
        finally:
            R.FPDFBitmap_Destroy(bitmap)


def _commands(segments) -> list[tuple]:
    """PDFium path segments as move / line / curve / close commands. PDFium closes a subpath
    with a line back to its start carrying the close flag; that line is the close itself.
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


def _trace(segments, filled: bool):
    """Path items and the bounding box of their points, for one way of painting the path."""
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
            for q in cmd[1:]:
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


class Document:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.pdf = pdfium.PdfDocument(str(path))
        self._pages: dict[int, Page] = {}

    def __len__(self) -> int:
        return len(self.pdf)

    def __getitem__(self, index: int) -> Page:
        if index < 0:
            index += len(self)
        if index not in self._pages:
            self._pages[index] = Page(self, index)
        return self._pages[index]

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    @property
    def metadata(self) -> dict:
        meta = self.pdf.get_metadata_dict()
        return {"title": meta.get("Title") or "", "producer": meta.get("Producer") or ""}

    def label(self, index: int) -> str:
        return self.pdf.get_page_label(index)

    def close(self) -> None:
        self._pages.clear()
        self.pdf.close()
