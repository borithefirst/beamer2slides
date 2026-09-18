"""The PDFium backend (pypdfium2): the reference implementation of `api.PdfBackend`.

PDFium's handles stay in here; what leaves is plain data and object ids (api.py). Findings about
what PDFium reports and how it is read are in CLAUDE.md ("Pitfalls found so far")."""

from __future__ import annotations

import ctypes
import io
import math
from pathlib import Path
from typing import Sequence

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as R

from .api import (COLOR_SPACES, LIGATURES, NO_OBJECT, OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, Box, Char,
                  EmbeddedImage, PageObject, PdfError, char_box, join_surrogates, mul, pixel_bounds,
                  trace, transform_box)


def _addr(handle) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


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


def _is_active(obj) -> bool:
    active = ctypes.c_long()
    return bool(R.FPDFPageObj_GetIsActive(obj, active)) and bool(active.value)


def _rgba(getter, obj) -> tuple[int, int, int, int] | None:
    r, g, b, a = (ctypes.c_ulong() for _ in range(4))
    if not getter(obj, r, g, b, a):
        return None
    return r.value, g.value, b.value, a.value


def _bitmap_array(bitmap) -> np.ndarray | None:
    """A PDFium bitmap as RGB or RGBA pixels (uint8, h x w x 3/4). Unknown formats give None."""
    if not bitmap:
        return None
    fmt = R.FPDFBitmap_GetFormat(bitmap)
    w, h = R.FPDFBitmap_GetWidth(bitmap), R.FPDFBitmap_GetHeight(bitmap)
    stride = R.FPDFBitmap_GetStride(bitmap)
    if w <= 0 or h <= 0 or stride <= 0:
        return None
    buf = R.FPDFBitmap_GetBuffer(bitmap)
    data = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), shape=(h * stride,))
    rows = data.reshape(h, stride)
    if fmt == R.FPDFBitmap_Gray:
        return np.repeat(rows[:, :w, None], 3, axis=2).copy()
    if fmt == R.FPDFBitmap_BGR:
        return rows[:, :w * 3].reshape(h, w, 3)[..., ::-1].copy()
    if fmt in (R.FPDFBitmap_BGRA, R.FPDFBitmap_BGRx):
        px = rows[:, :w * 4].reshape(h, w, 4)
        return px[..., [2, 1, 0, 3]].copy() if fmt == R.FPDFBitmap_BGRA else px[..., 2::-1].copy()
    return None


def _buffer(getter, *args) -> bytes:
    """A PDFium byte getter called twice: once for the length, once for the data."""
    n = getter(*args, None, 0)
    if not n:
        return b""
    buf = ctypes.create_string_buffer(n)
    n = getter(*args, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), n)
    return buf.raw[:n]


def _intersect(box: tuple, clip: tuple) -> tuple:
    return max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3])


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
        self._objects: list[PageObject] | None = None
        self._handles: list = []
        self._ids: dict[int, int] = {}      # handle address -> id
        self._bounds: list | None = None
        self._fonts: list = []               # font_id -> PDFium font handle
        self._font_info: dict[int, tuple] = {}  # font address -> (font_id, name, ascent, descent)
        self._textpage = None

    @property
    def rect(self) -> Box:
        return 0.0, 0.0, self.width, self.height

    def point(self, x: float, y: float) -> tuple[float, float]:
        return x - self.left, self.top - y

    # ------------------------------------------------------------------ objects

    def objects(self) -> list[PageObject]:
        if self._objects is None:
            out: list[PageObject] = []

            def walk(count, get, parent_matrix, parent):
                for i in range(count):
                    obj = get(i)
                    kind = R.FPDFPageObj_GetType(obj)
                    matrix = mul(_obj_matrix(obj), parent_matrix)
                    po = PageObject(len(out), kind, matrix, parent.id if parent else None)
                    out.append(po)
                    self._handles.append(obj)
                    if parent is not None:
                        parent.children.append(po.id)
                    if kind == OBJ_FORM:
                        walk(R.FPDFFormObj_CountObjects(obj), lambda j, o=obj: R.FPDFFormObj_GetObject(o, j), matrix, po)

            walk(R.FPDFPage_CountObjects(self.raw), lambda i: R.FPDFPage_GetObject(self.raw, i), self.to_page, None)
            self._ids = {_addr(h): k for k, h in enumerate(self._handles)}
            self._objects = out
        return self._objects

    def _handle(self, obj: int):
        self.objects()
        if not isinstance(obj, int) or not 0 <= obj < len(self._handles):
            raise PdfError(f"page {self.index} has no object {obj!r}")
        return self._handles[obj]

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        for obj in objects:
            R.FPDFPageObj_SetIsActive(self._handle(obj), bool(active))

    # ------------------------------------------------------------------ text

    def textpage(self):
        if self._textpage is None:
            self._textpage = self.page.get_textpage()
        return self._textpage

    def _font(self, font, tp, i, fonts: dict, name_buf, flags) -> tuple[int, str, float, float]:
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
            font_id = -1
            if font:
                font_id = len(self._fonts)
                self._fonts.append(font)
            fonts[key] = (font_id, name, ascent, descent)
        return fonts[key]

    def chars(self) -> list[Char]:
        self.objects()
        tp = self.textpage().raw
        n = R.FPDFText_CountChars(tp)
        name_buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        fonts = self._font_info
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
            font_id, name, ascent, descent = self._font(font, tp, i, fonts, name_buf, flags)
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
            box = char_box(ox, oy, ux, uy, advance, size, ascent, descent)
            oid = self._ids.get(_addr(obj), NO_OBJECT) if obj else NO_OBJECT
            prev = out[-1] if out else None
            if prev and prev.obj == oid and prev.origin == (ox, oy):
                # One glyph for several characters (a ligature): PDFium repeats the glyph's
                # position and box for each of them. The glyph's advance is the loose box.
                prev.c = LIGATURES.get(prev.c + text, prev.c + text)
                prev.advance = max(prev.advance, loose_advance)
                prev.exact_advance = True
                prev.box = char_box(ox, oy, ux, uy, prev.advance, size, ascent, descent)
                continue
            out.append(Char(text, name, size, color, a.value, (ox, oy), box, (ux, uy), oid, font_id, advance,
                            ascent=ascent, descent=descent, exact_advance=exact))
        for ch in out:
            if any("\ud800" <= u <= "\udfff" for u in ch.c):
                ch.c = join_surrogates(ch.c)
        # PDFium's text page puts the objects of a line in reading order; keep content order
        # (a big operator's limits, accents), as the span rules expect. Ids are content order.
        return [ch for _, ch in sorted(enumerate(out), key=lambda e: (e[1].obj, e[0]))]

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        out = []
        w = ctypes.c_float()
        for font_id, c, size in requests:
            font = self._fonts[font_id] if isinstance(font_id, int) and 0 <= font_id < len(self._fonts) else None
            ok = font and len(c) == 1 and R.FPDFFont_GetGlyphWidth(font, ord(c), ctypes.c_float(size), w)
            out.append(w.value if ok else None)
        return out

    # ------------------------------------------------------------------ paths

    def drawings(self) -> list[dict]:
        out: list[dict] = []
        for po in self.objects():
            handle = self._handles[po.id]
            if po.type != OBJ_PATH or not _is_active(handle):
                continue
            fillmode, stroke = ctypes.c_int(), ctypes.c_int()
            if not R.FPDFPath_GetDrawMode(handle, fillmode, stroke):
                continue
            segments = self._segments(po)
            if fillmode.value:
                fill = _rgba(R.FPDFPageObj_GetFillColor, handle)
                path = trace(segments, filled=True)
                if path:
                    items, rect = path
                    opacity = fill[3] / 255 if fill else 1.0
                    out.append({"type": "f", "items": items, "rect": rect, "even_odd": fillmode.value == 1,
                                "fill": tuple(v / 255 for v in fill[:3]) if fill else None,
                                "fill_opacity": opacity, "object": po.id,
                                # transparency without alpha: a soft mask (or a blend mode)
                                "soft_mask": opacity == 1.0 and bool(R.FPDFPageObj_HasTransparency(handle))})
            if stroke.value:
                color = _rgba(R.FPDFPageObj_GetStrokeColor, handle)
                width = ctypes.c_float()
                R.FPDFPageObj_GetStrokeWidth(handle, width)
                a, b, c, d, _, _ = po.matrix
                path = trace(segments, filled=False)
                if path:
                    items, rect = path
                    entry = {"type": "s", "items": items, "rect": rect,
                             "color": tuple(v / 255 for v in color[:3]) if color else None,
                             "stroke_opacity": color[3] / 255 if color else 1.0,
                             "width": width.value * math.sqrt(abs(a * d - b * c)), "object": po.id}
                    prev = out[-1] if out else None
                    if prev and prev["type"] == "f" and prev["items"] == items:
                        for k, v in entry.items():
                            prev.setdefault(k, v)
                        prev["type"] = "fs"
                    else:
                        out.append(entry)
        return out

    def _segments(self, po: PageObject) -> list[tuple[int, float, float, bool]]:
        handle = self._handles[po.id]
        a, b, c, d, e, f = po.matrix
        x, y = ctypes.c_float(), ctypes.c_float()
        out = []
        for i in range(R.FPDFPath_CountSegments(handle)):
            seg = R.FPDFPath_GetPathSegment(handle, i)
            R.FPDFPathSegment_GetPoint(seg, x, y)
            px, py = x.value, y.value
            out.append((R.FPDFPathSegment_GetType(seg), a * px + c * py + e, b * px + d * py + f,
                        bool(R.FPDFPathSegment_GetClose(seg))))
        return out

    # ------------------------------------------------------------------ images, links

    def _container_matrix(self, po: PageObject) -> tuple:
        return self._objects[po.parent].matrix if po.parent is not None else self.to_page

    def object_bounds(self) -> list[Box]:
        if self._bounds is None:
            self._bounds = [self._bounds_of(po) for po in self.objects()]
        return list(self._bounds)

    def _bounds_of(self, po: PageObject) -> Box:
        handle = self._handles[po.id]
        if po.type == OBJ_PATH:
            path = trace(self._segments(po), filled=True)
            width = ctypes.c_float()
            if path and R.FPDFPageObj_GetStrokeWidth(handle, width):
                a, b, c, d, _, _ = po.matrix
                half = width.value * math.sqrt(abs(a * d - b * c)) / 2
                x0, y0, x1, y1 = path[1]
                return x0 - half, y0 - half, x1 + half, y1 + half
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        if not R.FPDFPageObj_GetBounds(handle, l, b, r, t):
            return 0.0, 0.0, 0.0, 0.0
        return transform_box((l.value, b.value, r.value, t.value), self._container_matrix(po))

    def _clip_box(self, po: PageObject) -> tuple | None:
        """Bounding box of an object's clip path in page space (paths of a clip intersect)."""
        clip = R.FPDFPageObj_GetClipPath(self._handles[po.id])
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
            b = transform_box((min(p[0] for p in pts), min(p[1] for p in pts),
                               max(p[0] for p in pts), max(p[1] for p in pts)), m)
            box = b if box is None else _intersect(box, b)
        return box

    def _clipped(self, po: PageObject, box: tuple) -> tuple:
        """The box cut by the object's clip and those of the forms around it."""
        node = po
        while node is not None:
            clip = self._clip_box(node)
            if clip:
                box = _intersect(box, clip)
            node = self._objects[node.parent] if node.parent is not None else None
        return box

    @staticmethod
    def _unit_box(po: PageObject) -> tuple:
        a, b, c, d, e, f = po.matrix
        xs, ys = [e, a + e, c + e, a + c + e], [f, b + f, d + f, b + d + f]
        return min(xs), min(ys), max(xs), max(ys)

    def images(self) -> list[dict]:
        out = []
        w, h = ctypes.c_ulong(), ctypes.c_ulong()
        for po in self.objects():
            handle = self._handles[po.id]
            if po.type == OBJ_SHADING and _is_active(handle):
                # A shading paints its clip area. Like MuPDF, report it as an image on whole
                # points, one pixel per point.
                box = self._clipped(po, self._bounds_of(po))
                x0, y0 = math.floor(box[0] + 1e-3), math.floor(box[1] + 1e-3)
                x1, y1 = math.ceil(box[2] - 1e-3), math.ceil(box[3] - 1e-3)
                if x1 > x0 and y1 > y0:
                    out.append({"bbox": (x0, y0, x1, y1), "width": x1 - x0, "height": y1 - y0, "object": po.id})
                continue
            if po.type != OBJ_IMAGE or not _is_active(handle):
                continue
            full = self._unit_box(po)
            box = self._clipped(po, full)  # \includegraphics[trim, clip]: only the clipped part shows
            if box[2] <= box[0] or box[3] <= box[1]:
                box = full
            R.FPDFImageObj_GetImagePixelSize(handle, w, h)
            out.append({"bbox": box, "width": w.value, "height": h.value, "object": po.id})
        return out

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        handle = self._handle(obj)
        po = self._objects[obj]
        if po.type != OBJ_IMAGE:
            return None
        w, h = ctypes.c_ulong(), ctypes.c_ulong()
        R.FPDFImageObj_GetImagePixelSize(handle, w, h)
        meta = R.FPDF_IMAGEOBJ_METADATA()
        R.FPDFImageObj_GetImageMetadata(handle, self.raw, meta)
        filters = []
        for i in range(R.FPDFImageObj_GetImageFilterCount(handle)):
            size = R.FPDFImageObj_GetImageFilter(handle, i, None, 0)
            buf = ctypes.create_string_buffer(size)
            R.FPDFImageObj_GetImageFilter(handle, i, buf, size)
            filters.append(buf.raw[:max(0, size - 1)].decode("ascii", "replace"))
        a, b, c, d, _, _ = po.matrix
        full = self._unit_box(po)
        box = self._clipped(po, full)
        # In page space (y down) an upright image has a > 0 and d < 0: PDF's unit square starts
        # at the image's bottom left, so its rows run the other way. b/c turn it, a < 0 mirrors
        # it left to right, d > 0 top to bottom.
        upright = abs(b) < 1e-6 * max(1.0, abs(a)) and abs(c) < 1e-6 * max(1.0, abs(d)) and a > 0 and d < 0
        img = self._image_pixels(handle)
        # A soft mask is not in `pixels` (FPDFImageObj_GetBitmap leaves it out) and not in
        # FPDFPageObj_HasTransparency either; the rasterisation is what shows it. An opaque
        # image renders alpha 255 everywhere, even at its rim.
        drawn = self._rendered_image(handle)
        return EmbeddedImage(
            px=(w.value, h.value), box=box, matrix=po.matrix, filters=filters,
            colorspace=COLOR_SPACES.get(meta.colorspace, str(meta.colorspace)),
            bpp=meta.bits_per_pixel, dpi=(meta.horizontal_dpi, meta.vertical_dpi),
            raw=_buffer(R.FPDFImageObj_GetImageDataRaw, handle),
            decoded_size=R.FPDFImageObj_GetImageDataDecoded(handle, None, 0),
            clipped=any(abs(box[k] - full[k]) > 0.01 for k in range(4)),
            upright=upright, blended=bool(R.FPDFPageObj_HasTransparency(handle)),
            transparent=drawn is None or drawn.shape[2] == 4 and bool((drawn[..., 3] < 255).any()),
            pixels=img, rendered=drawn)

    @staticmethod
    def _image_pixels(handle) -> np.ndarray | None:
        bitmap = R.FPDFImageObj_GetBitmap(handle)
        try:
            return _bitmap_array(bitmap)
        finally:
            if bitmap:
                R.FPDFBitmap_Destroy(bitmap)

    def _rendered_image(self, handle) -> np.ndarray | None:
        """The image object rasterised by PDFium with its matrix, mask and colour space applied:
        upright, in page orientation, on a transparent ground where a mask makes it see-through."""
        bitmap = R.FPDFImageObj_GetRenderedBitmap(self.doc.pdf.raw, self.raw, handle)
        try:
            return _bitmap_array(bitmap)
        finally:
            if bitmap:
                R.FPDFBitmap_Destroy(bitmap)

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

    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False) -> np.ndarray:
        ix0, iy0, w, h = pixel_bounds(zoom, clip if clip is not None else self.rect)
        bitmap = R.FPDFBitmap_Create(w, h, 1 if transparent else 0)
        try:
            R.FPDFBitmap_FillRect(bitmap, 0, 0, w, h, 0x00000000 if transparent else 0xFFFFFFFF)
            matrix = R.FS_MATRIX(zoom, 0, 0, zoom, -ix0, -iy0)
            clipping = R.FS_RECTF(0, 0, w, h)
            R.FPDF_RenderPageBitmapWithMatrix(bitmap, self.raw, matrix, clipping, R.FPDF_ANNOT)
            stride = R.FPDFBitmap_GetStride(bitmap)
            buf = R.FPDFBitmap_GetBuffer(bitmap)
            data = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), shape=(h * stride,))
            bgrx = data.reshape(h, stride)[:, :w * 4].reshape(h, w, 4)
            if transparent:
                return bgrx[..., [2, 1, 0, 3]].copy()
            return bgrx[..., 2::-1].copy()
        finally:
            R.FPDFBitmap_Destroy(bitmap)


class Document:
    def __init__(self, source: str | Path | bytes):
        self.path = None if isinstance(source, (bytes, bytearray)) else Path(source)
        self._source = bytes(source) if self.path is None else str(source)
        try:
            self.pdf = pdfium.PdfDocument(self._source)
        except pdfium.PdfiumError as e:
            raise PdfError(str(e)) from e
        self._pages: dict[int, Page] = {}

    def __len__(self) -> int:
        return len(self.pdf)

    def __getitem__(self, index: int) -> Page:
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index not in self._pages:
            self._pages[index] = Page(self, index)
        return self._pages[index]

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def metadata(self) -> dict:
        meta = self.pdf.get_metadata_dict()
        return {"title": meta.get("Title") or "", "producer": meta.get("Producer") or ""}

    def label(self, index: int) -> str:
        return self.pdf.get_page_label(index)

    def named_dests(self) -> list[tuple[str, int]]:
        out = []
        for i in range(R.FPDF_CountNamedDests(self.pdf.raw)):
            size = ctypes.c_long(0)
            R.FPDF_GetNamedDest(self.pdf.raw, i, None, ctypes.byref(size))
            if size.value <= 0:
                continue
            buf = ctypes.create_string_buffer(size.value)
            dest = R.FPDF_GetNamedDest(self.pdf.raw, i, buf, ctypes.byref(size))
            if dest:
                name = buf.raw[:size.value].decode("utf-16-le", "replace").rstrip("\x00")
                out.append((name, R.FPDFDest_GetDestPageIndex(self.pdf.raw, dest)))
        return out

    def save(self, pages: Sequence[int] | None = None, boxes: dict[int, Box] | None = None) -> bytes:
        keep = set(range(len(self))) if pages is None else {int(p) for p in pages}
        edited = pdfium.PdfDocument(self._source)  # a copy: this document stays as it is
        try:
            for index, (x0, y0, x1, y1) in (boxes or {}).items():
                page = self[int(index)]
                box = (page.left + x0, page.top - y1, page.left + x1, page.top - y0)
                raw = edited[int(index)].raw
                R.FPDFPage_SetMediaBox(raw, *box)
                R.FPDFPage_SetCropBox(raw, *box)
            for index in reversed(range(len(self))):
                if index not in keep:
                    edited.del_page(index)
            buf = io.BytesIO()
            edited.save(buf)
            return buf.getvalue()
        finally:
            edited.close()

    def close(self) -> None:
        self._pages.clear()
        self.pdf.close()


class PdfiumBackend:
    name = "pdfium"

    def open(self, source: str | Path | bytes) -> Document:
        return Document(source)
