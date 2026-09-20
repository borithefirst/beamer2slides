"""The pure Python backend: api.py's contract over syntax/document/content/fonts/textpage.

It answers what `pdfium_backend` answers, call for call, from the same rules (each method names
the PDFium calls it stands in for); `docs/pdf-from-scratch.md` has the measurements. Rendering is
being ported (render.py, raster.py: PDFium's AGG renderer, pixel for pixel): pages of paths, clips
and forms draw exactly, anything else raises PdfError, so `renders` stays False until a beamer page
draws; `embedded_image` gives the stream and its dictionary's facts but no pixels. So extract and
classify run on it; render, fidelity and the checks need a backend that draws.

Needs nothing but the standard library, numpy (the api's types) and - for glyph boxes read from
embedded font programs - fontTools."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import numpy as np

from ..api import (COLOR_SPACES, LIGATURES, NO_OBJECT, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, Box, Char,
                   EmbeddedImage, PageObject, PdfError, char_box, font_metrics, join_surrogates, mul,
                   pixel_bounds, render_matrix, trace, transform_box)
from .content import Parser, PObj
from .render import render_page
from . import navigation
from .document import PdfFile, read, write_file
from .filters import ABBREVIATIONS, decode
from .syntax import InlineImage, Name, Stream, String, float32
from .textpage import GENERATED, HYPHEN, NOT_UNICODE, TextPage

_CS_ABBREVIATIONS = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed"}
_CS_CODES = {name: code for code, name in COLOR_SPACES.items()}


def _direction_r2l(pdf: PdfFile) -> bool:
    """CPDF_ViewerPreferences::IsDirectionR2L: the catalog's /ViewerPreferences dictionary has
    /Direction R2L (GetByteStringFor: a name or a string)."""
    prefs = navigation.dict_for(pdf, pdf.catalog, "ViewerPreferences")
    if not prefs:
        return False
    d = pdf.resolve(prefs.get("Direction"))
    return (str(d) if isinstance(d, Name) else bytes(d).decode("latin-1") if isinstance(d, String)
            else "") == "R2L"


def _alpha255(a: float) -> int:
    """FXSYS_GetUnsignedAlpha."""
    return int(min(max(a, 0.0), 1.0) * 255 + 0.5)


def _intersect(box: tuple, clip: tuple) -> tuple:
    return max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3])


def _rect(v, r) -> tuple | None:
    v = r(v)
    if isinstance(v, list) and len(v) == 4 and all(isinstance(r(x), (int, float)) for x in v):
        x0, y0, x1, y1 = (float(r(x)) for x in v)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return None


class Page:
    def __init__(self, doc: "Document", index: int):
        self.doc = doc
        self.index = index
        pdf = doc.pdf
        r = pdf.resolve
        found = pdf.inherited_page(index)
        # FPDF_LoadPage: no dictionary where the tree says a page is, or one typed as no page
        if found is None or ("Type" in found[1] and r(found[1]["Type"]) != "Page"):
            raise PdfError(f"page {index}: no page dictionary")
        self._ref, self.dict = found
        # CPDF_Page: the crop box within the media box (an empty one: the media box; none: Letter)
        media = _rect(self.dict.get("MediaBox"), r)
        if media is None or media[0] >= media[2] or media[1] >= media[3]:
            media = (0.0, 0.0, 612.0, 792.0)
        crop = _rect(self.dict.get("CropBox"), r)
        if crop is None or crop[0] >= crop[2] or crop[1] >= crop[3]:
            box = media
        else:
            box = _intersect(crop, media)
            if box[0] > box[2] or box[1] > box[3]:
                box = (0.0, 0.0, 0.0, 0.0)
        self.box = box
        # CPDF_Page::GetPageRotation: C integer division and remainder
        rot = r(self.dict.get("Rotate"))
        rot = int(math.trunc(rot / 90)) if isinstance(rot, (int, float)) and math.isfinite(rot) else 0
        rot = int(math.fmod(rot, 4))
        self.rotation = rot + 4 if rot < 0 else rot
        left, bottom, right, top = box
        self.left, self.top = left, top
        self.width, self.height = right - left, top - bottom
        self.to_page = (1.0, 0.0, 0.0, -1.0, -left, top)
        self._pobjs: list[PObj] | None = None
        self._objects: list[PageObject] | None = None
        self._bounds: list | None = None
        self._textpage: TextPage | None = None
        self._fonts: list = []                   # font_id -> Font
        self._font_info: dict[int, tuple] = {}   # id(Font) -> (font_id, name, ascent, descent)

    @property
    def rect(self) -> Box:
        return 0.0, 0.0, self.width, self.height

    def point(self, x: float, y: float) -> tuple[float, float]:
        return x - self.left, self.top - y

    # ------------------------------------------------------------------ objects

    def _parse(self) -> list[PObj]:
        if self._pobjs is None:
            pdf = self.doc.pdf
            r = pdf.resolve
            contents = r(self.dict.get("Contents"))
            parts = contents if isinstance(contents, list) else [contents]
            data = b" ".join(pdf.stream_data(r(p)) for p in parts if isinstance(r(p), Stream))
            objs: list[PObj] = []
            try:
                Parser(pdf, r(self.dict.get("Resources")), objs, self.doc._font_cache, {}).parse_page(data, self.box)
            except RecursionError:
                pass  # what was parsed stands, as when PDFium gives up on a stream
            self._pobjs = objs
        return self._pobjs

    def objects(self) -> list[PageObject]:
        if self._objects is None:
            pobjs = self._parse()
            ids = {id(o): k for k, o in enumerate(pobjs)}
            out: list[PageObject] = []
            for k, o in enumerate(pobjs):
                parent = ids[id(o.parent)] if o.parent is not None else None
                container = out[parent].matrix if parent is not None else self.to_page
                out.append(PageObject(k, o.type, mul(o.matrix, container), parent))
                if parent is not None:
                    out[parent].children.append(k)
            self._ids = ids
            self._objects = out
        return self._objects

    def _obj(self, obj: int) -> PObj:
        self.objects()
        if not isinstance(obj, int) or not 0 <= obj < len(self._pobjs):
            raise PdfError(f"page {self.index} has no object {obj!r}")
        return self._pobjs[obj]

    def set_active(self, objects: Sequence[int], active: bool) -> None:
        for obj in objects:
            self._obj(obj).active = bool(active)

    # ------------------------------------------------------------------ text

    def textpage(self) -> TextPage:
        if self._textpage is None:
            self._textpage = TextPage(self._parse(), self.width, self.height, self.to_page,
                                      _direction_r2l(self.doc.pdf))
        return self._textpage

    def _font(self, font) -> tuple[int, str, float, float]:
        """FPDFText_GetFontInfo, FPDFFont_GetAscent/Descent and FPDFFont_GetFontData, as
        pdfium_backend._font reads them."""
        key = id(font)
        if key not in self._font_info:
            name = font.base_name
            if len(name) > 7 and name[6] == "+":
                name = name[7:]
            if not font.base_name:
                name = "Type3"
            # FPDFFont_GetAscent/Descent compute in float: GetTypeAscent() * 1.0f / 1000.f
            ascent, descent = font_metrics(float32(font.ascent / 1000), float32(font.descent / 1000),
                                           font.program_data or b"")
            font_id = len(self._fonts)
            self._fonts.append(font)
            self._font_info[key] = (font_id, name, ascent, descent)
        return self._font_info[key]

    def chars(self) -> list[Char]:
        self.objects()
        out: list[Char] = []
        color = alpha = 0
        for ci in self.textpage().chars:
            if ci.type == GENERATED:
                continue
            obj = ci.obj
            font = obj.font if obj is not None else None
            if font is not None:
                font_id, name, ascent, descent = self._font(font)
            else:
                font_id, name, ascent, descent = -1, "", 0.9, -0.1
            u = ci.unicode
            text = chr(u) if u < 0x110000 else chr(0xFFFD)
            if ci.type == HYPHEN:
                text = "-"
            elif u == 0 or ci.type == NOT_UNICODE and name != "Type3":
                text = chr(0xFFFD)
            m = ci.matrix
            size = ci.font_size * math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
            norm = math.hypot(m[0], m[1]) or 1.0
            ux, uy = m[0] / norm, -m[1] / norm
            ox, oy = self.point(*ci.origin)
            ll, lb, lr, lt = ci.loose
            advance = loose_advance = abs((lr - ll) * ux) + abs((lt - lb) * uy)
            exact = True
            if ux > 0.999 and ci.box[2] >= lr - 0.01 and font is not None:
                width = font.glyph_width(u, size)
                if width > 0:
                    advance, exact = width, False
            if obj is not None:  # FPDFText_GetFillColor; without an object, the colour before it
                color = (obj.fill if obj.fill is not None else 0) & 0xFFFFFF
                alpha = _alpha255(obj.fill_alpha)
            box = char_box(ox, oy, ux, uy, advance, size, ascent, descent)
            oid = self._ids.get(id(obj), NO_OBJECT) if obj is not None else NO_OBJECT
            prev = out[-1] if out else None
            if prev and prev.obj == oid and prev.origin == (ox, oy):
                prev.c = LIGATURES.get(prev.c + text, prev.c + text)
                prev.advance = max(prev.advance, loose_advance)
                prev.exact_advance = True
                prev.box = char_box(ox, oy, ux, uy, prev.advance, size, ascent, descent)
                continue
            out.append(Char(text, name, size, color, alpha, (ox, oy), box, (ux, uy), oid, font_id, advance,
                            ascent=ascent, descent=descent, exact_advance=exact))
        for ch in out:
            if any("\ud800" <= u <= "\udfff" for u in ch.c):
                ch.c = join_surrogates(ch.c)
        return [ch for _, ch in sorted(enumerate(out), key=lambda e: (e[1].obj, e[0]))]

    def glyph_widths(self, requests: Sequence[tuple[int, str, float]]) -> list[float | None]:
        out = []
        for font_id, c, size in requests:
            font = self._fonts[font_id] if isinstance(font_id, int) and 0 <= font_id < len(self._fonts) else None
            ok = font is not None and len(c) == 1 and ord(c) <= 0xFFFF
            out.append(float(font.glyph_width(ord(c), size)) if ok else None)
        return out

    # ------------------------------------------------------------------ paths

    def _segments(self, k: int) -> list[tuple[int, float, float, bool]]:
        a, b, c, d, e, f = self._objects[k].matrix
        return [(kind, a * x + c * y + e, b * x + d * y + f, closes) for x, y, kind, closes in self._pobjs[k].points]

    def drawings(self) -> list[dict]:
        out: list[dict] = []
        for po in self.objects():
            o = self._pobjs[po.id]
            if po.type != OBJ_PATH or not o.active:
                continue
            segments = self._segments(po.id)
            if o.fill_type:
                path = trace(segments, filled=True)
                if path:
                    items, rect = path
                    fill = None if o.fill is None else tuple(((o.fill >> s) & 0xFF) / 255 for s in (16, 8, 0))
                    opacity = _alpha255(o.fill_alpha) / 255 if o.fill is not None else 1.0
                    out.append({"type": "f", "items": items, "rect": rect, "even_odd": o.fill_type == 1,
                                "fill": fill, "fill_opacity": opacity, "object": po.id,
                                "soft_mask": opacity == 1.0 and o.has_transparency})
            if o.stroked:
                a, b, c, d, _, _ = po.matrix
                path = trace(segments, filled=False)
                if path:
                    items, rect = path
                    color = None if o.stroke is None else tuple(((o.stroke >> s) & 0xFF) / 255 for s in (16, 8, 0))
                    entry = {"type": "s", "items": items, "rect": rect, "color": color,
                             "stroke_opacity": _alpha255(o.stroke_alpha) / 255 if o.stroke is not None else 1.0,
                             "width": o.line_width * math.sqrt(abs(a * d - b * c)), "object": po.id}
                    prev = out[-1] if out else None
                    if prev and prev["type"] == "f" and prev["items"] == items:
                        for key, v in entry.items():
                            prev.setdefault(key, v)
                        prev["type"] = "fs"
                    else:
                        out.append(entry)
        return out

    # ------------------------------------------------------------------ bounds, images

    def _container_matrix(self, po: PageObject) -> tuple:
        return self._objects[po.parent].matrix if po.parent is not None else self.to_page

    def object_bounds(self) -> list[Box]:
        if self._bounds is None:
            self._bounds = [self._bounds_of(po) for po in self.objects()]
        return list(self._bounds)

    def _bounds_of(self, po: PageObject) -> Box:
        o = self._pobjs[po.id]
        if po.type == OBJ_PATH:
            path = trace(self._segments(po.id), filled=True)
            if path:
                a, b, c, d, _, _ = po.matrix
                half = o.line_width * math.sqrt(abs(a * d - b * c)) / 2
                x0, y0, x1, y1 = path[1]
                return x0 - half, y0 - half, x1 + half, y1 + half
        return transform_box(o.rect, self._container_matrix(po))

    def _clip_box(self, po: PageObject) -> tuple | None:
        clips = self._pobjs[po.id].clips
        if not clips:
            return None
        m = self._container_matrix(po)
        box = None
        for c in clips:
            b = transform_box(c, m)
            box = b if box is None else _intersect(box, b)
        return box

    def _clipped(self, po: PageObject, box: tuple) -> tuple:
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

    @staticmethod
    def _image_dict(o: PObj) -> dict:
        """The image's dictionary (an inline image's keys come expanded from the lexer)."""
        return o.stream.dict if isinstance(o.stream, (Stream, InlineImage)) else {}

    def _pixel_size(self, o: PObj) -> tuple[int, int]:
        r = self.doc.pdf.resolve
        d = self._image_dict(o)
        w, h = r(d.get("Width")), r(d.get("Height"))
        return (w if isinstance(w, int) and w > 0 else 0), (h if isinstance(h, int) and h > 0 else 0)

    def images(self) -> list[dict]:
        out = []
        for po in self.objects():
            o = self._pobjs[po.id]
            if po.type == OBJ_SHADING and o.active:
                box = self._clipped(po, self._bounds_of(po))
                x0, y0 = math.floor(box[0] + 1e-3), math.floor(box[1] + 1e-3)
                x1, y1 = math.ceil(box[2] - 1e-3), math.ceil(box[3] - 1e-3)
                if x1 > x0 and y1 > y0:
                    out.append({"bbox": (x0, y0, x1, y1), "width": x1 - x0, "height": y1 - y0, "object": po.id})
                continue
            if po.type != OBJ_IMAGE or not o.active:
                continue
            full = self._unit_box(po)
            box = self._clipped(po, full)
            if box[2] <= box[0] or box[3] <= box[1]:
                box = full
            w, h = self._pixel_size(o)
            out.append({"bbox": box, "width": w, "height": h, "object": po.id})
        return out

    def embedded_image(self, obj: int) -> EmbeddedImage | None:
        """The stream, what its dictionary says, and PDFium's two bitmaps (GetBitmap, GetRenderedBitmap)
        where render_image ports what they need; else None, and `transparent` from the dictionaries."""
        o = self._obj(obj)
        po = self._objects[obj]
        if po.type != OBJ_IMAGE:
            return None
        r = self.doc.pdf.resolve
        d = self._image_dict(o)
        f = r(d.get("Filter"))
        filters = [str(r(x)) for x in f] if isinstance(f, list) else [str(f)] if f is not None else []
        filters = [ABBREVIATIONS.get(x, x) for x in filters]
        mask = bool(r(d.get("ImageMask")))
        cs = r(d.get("ColorSpace"))
        family = cs[0] if isinstance(cs, list) and cs else cs
        family = _CS_ABBREVIATIONS.get(str(r(family)), str(r(family))) if family is not None else "unknown"
        if isinstance(cs, (Name, str)) and family not in _CS_CODES:  # a named resource
            res = self.doc.pdf.resolve(self.dict.get("Resources"))
            spaces = r(res.get("ColorSpace")) if isinstance(res, dict) else None
            named = r(spaces.get(str(cs))) if isinstance(spaces, dict) else None
            family = str(r(named[0])) if isinstance(named, list) and named else str(r(named)) if named else "unknown"
        if mask or family not in _CS_CODES:
            family = "unknown"
        bpc = r(d.get("BitsPerComponent"))
        n = {"DeviceGray": 1, "CalGray": 1, "Indexed": 1, "Separation": 1, "DeviceRGB": 3, "CalRGB": 3,
             "Lab": 3, "DeviceCMYK": 4}.get(family, 3)
        # CPDF_DIB's bits per pixel: what it decodes to, not what is stored (CalculateBitsPerPixel)
        bits = 1 if mask else (bpc if isinstance(bpc, int) and bpc > 0 else 8) * n
        bpp = 1 if bits == 1 else 8 if bits <= 8 else 24
        px = self._pixel_size(o)
        # FPDFImageObj_GetImageMetadata: pixels per inch of the object's GetRect (its container's
        # space, the form around it left out), in floats: `px / width * 72`; 0 when either side is 0
        wide, high = float32(o.rect[2] - o.rect[0]), float32(o.rect[3] - o.rect[1])
        dpi = ((float32(float32(px[0] / wide) * 72), float32(float32(px[1] / high) * 72))
               if wide and high else (0.0, 0.0))
        a, b, c, dd, _, _ = po.matrix
        full = self._unit_box(po)
        box = self._clipped(po, full)
        turned = abs(b) >= 1e-6 * max(1.0, abs(a)) or abs(c) >= 1e-6 * max(1.0, abs(dd))
        upright = not turned and a > 0 and dd < 0
        stream = o.stream
        raw = bytes(stream.raw if isinstance(stream, Stream) else stream.data if isinstance(stream, InlineImage)
                    else b"")
        try:  # FPDFImageObj_GetImageDataDecoded: through the filters, up to the image codec
            decoded, _ = decode(raw, d, r)
        except Exception:  # noqa: BLE001 - defensive: decode itself gives the stored bytes when a filter fails
            decoded = b""
        blended = o.has_transparency
        clipped = any(abs(box[k] - full[k]) > 0.01 for k in range(4))
        try:
            pixels = self._image_pixels(o)
        except PdfError:
            pixels = None
        # PDFium's backend reads this from the rasterised object's alpha (FPDFImageObj_GetRenderedBitmap)
        try:
            drawn = self._rendered_image(o)
            see_through = drawn is None or bool((drawn[..., 3] < 255).any())
        except PdfError:  # not drawable here yet: what makes that alpha < 255, as far as the dictionaries say
            drawn = None
            see_through = (mask or r(d.get("SMask")) is not None or r(d.get("Mask")) is not None or blended
                           or turned or clipped)
        return EmbeddedImage(
            px=px, box=box, matrix=po.matrix, filters=filters, colorspace=family, bpp=bpp, dpi=dpi,
            raw=raw, decoded_size=len(decoded), clipped=clipped,
            upright=upright, blended=blended, transparent=see_through, pixels=pixels, rendered=drawn)

    def _image_pixels(self, o) -> np.ndarray | None:
        """FPDFImageObj_GetBitmap: CPDF_Image::LoadDIBBase (CPDF_DIB::Load with no resources, so a
        named colour space doesn't load, and no mask) at the image's native size, a 1 bpp or palette
        format converted to one without a palette. As pdfium_backend gives it: 8bppRgb as gray
        repeated to RGB, kBgr as RGB, kBgra as RGBA. PdfError when the decoder is not ported."""
        from . import decode_image as DI
        try:
            dib = DI.load(self.doc.pdf, o.stream, None, (0, 0), with_mask=False)
        except DI.Unsupported as e:
            raise PdfError(f"the pure reader cannot decode {e} yet")
        if dib is None:
            return None
        if dib.fmt in ("mask1", "rgb1") and not (dib.fmt == "rgb1" and dib.palette):
            gray = dib.rows * np.uint8(255)  # ConvertTo(k8bppRgb): set bits white
        elif dib.fmt == "rgb8" and not dib.palette:
            gray = dib.rows
        elif dib.fmt in ("rgb1", "rgb8"):
            pal = np.array(dib.palette, np.uint32)[dib.rows]  # ConvertTo(kBgr)
            return np.stack([(pal >> 16) & 255, (pal >> 8) & 255, pal & 255], axis=-1).astype(np.uint8)
        elif dib.fmt == "bgr":
            return dib.rows[..., ::-1].copy()
        else:
            return dib.rows[..., [2, 1, 0, 3]].copy()
        return np.repeat(gray[..., None], 3, axis=2)

    def _rendered_image(self, o) -> np.ndarray | None:
        """FPDFImageObj_GetRenderedBitmap: the image object alone through CPDF_ImageRenderer onto a
        clear BGRA bitmap of ceil(hypot) of its own matrix's columns, flipped and moved to its
        lowest corner, under its clip matched onto that bitmap; its ExtGState soft mask and blend
        mode are not applied (no ProcessTransparency), its constant alpha is. RGBA, or None where
        PDFium gives no bitmap. PdfError when the image needs what render_image does not port."""
        from . import raster as R
        from .render import Device, Status
        from .render_image import draw, refusal
        from .render_transparency import Context
        pdf = self.doc.pdf
        ctx = Context(pdf, pdf.resolve(self.dict.get("Resources")), self.doc._font_cache, False)
        ctx.images = {}  # no page cache
        why = refusal(o, ctx)
        if why is not None:
            raise PdfError(f"the pure reader cannot render {why} yet")
        a, b, c, d, e, f = (float32(v) for v in o.matrix)
        w = math.ceil(float32(math.hypot(a, c)))
        h = math.ceil(float32(math.hypot(b, d)))
        if w <= 0 or h <= 0:
            return None
        min_x, min_y = float32(e + min(a, c)), float32(f + min(b, d))
        flip = (1.0, 0.0, 0.0, -1.0, 0.0, float(h))
        m = (1.0, 0.0, 0.0, -1.0, float32(-min_x), float32(h + min_y))
        dev = Device(w, h, True)
        status = Status(dev, ctx=ctx)
        if o.clip_paths or o.clip_texts:
            l, bt, rt, t = R.transform_rect(o.matrix, (0.0, 0.0, 1.0, 1.0))
            # CFX_Matrix::MatchRect((0, 0, w, h), the unit square's box), then the flip
            ma = 1.0 if abs(float32(l - rt)) < 0.001 else float32(float32(-w) / float32(l - rt))
            md = 1.0 if abs(float32(bt - t)) < 0.001 else float32(float32(-h) / float32(bt - t))
            match = (ma, 0.0, 0.0, md, float32(-float32(l * ma)), float32(-float32(bt * md)))
            status.process_clip(o.clip_paths, R.concat(match, flip), o.clip_texts)
        draw(status, o, m)
        return dev.bgra[..., [2, 1, 0, 3]].copy()

    # ------------------------------------------------------------------ links

    def links(self) -> list[dict]:
        """The reference backend's loop over FPDFLink_Enumerate (navigation.links): a link's
        /Rect as written (float32, not normalised) turned into page space."""
        out = []
        for link in navigation.links(self.doc.pdf, self.dict):
            left, bottom, right, top = link.pop("rect")
            x0, y0 = self.point(left, top)
            x1, y1 = self.point(right, bottom)
            out.append({"bbox": (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), **link})
        return out

    # ------------------------------------------------------------------ rendering

    def render(self, zoom: float, clip: Box | None = None, transparent: bool = False) -> np.ndarray:
        """FPDF_RenderPageBitmapWithMatrix as pdfium_backend calls it (render.py)."""
        ix0, iy0, w, h = pixel_bounds(zoom, clip if clip is not None else self.rect)
        fs = render_matrix(zoom, ix0, iy0, self.rotation, self.width, self.height)
        from .render_transparency import Context
        pdf = self.doc.pdf
        group = pdf.resolve(self.dict.get("Group"))
        page_group = isinstance(group, dict) and str(pdf.resolve(group.get("S"))) == "Transparency"
        ctx = Context(pdf, pdf.resolve(self.dict.get("Resources")), self.doc._font_cache, page_group)
        # CPDF_PageImageCache lives as long as the page: a JPEG decoded smaller stays so
        ctx.images = self.__dict__.setdefault("_image_cache", {})
        bgra = render_page(self._parse(), self.box, self.rotation, fs, w, h, transparent, ctx)
        if transparent:
            return bgra[..., [2, 1, 0, 3]].copy()
        return bgra[..., 2::-1].copy()


class Document:
    def __init__(self, source: str | Path | bytes):
        self.path = None if isinstance(source, (bytes, bytearray)) else Path(source)
        try:
            self.pdf: PdfFile = read(source)
        except Exception as e:  # noqa: BLE001 - whatever the file does, the caller sees a PdfError
            raise PdfError(f"unreadable PDF: {e}") from e
        if self.pdf.page_count < 1:
            # PDFium opens it; the reference backend (pypdfium2's PdfDocument) refuses it
            raise PdfError("a PDF without pages")
        self._pages: dict[int, Page] = {}
        from .fonts import doc_fonts
        self._font_cache: dict = doc_fonts(self.pdf)
        self._open = True

    def __len__(self) -> int:
        return self.pdf.page_count

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
        """FPDF_GetMetaText of /Title and /Producer."""
        return {"title": navigation.meta_text(self.pdf, "Title"),
                "producer": navigation.meta_text(self.pdf, "Producer")}

    def label(self, index: int) -> str:
        """FPDF_GetPageLabel."""
        label = navigation.page_label(self.pdf, index)
        return "" if label is None else navigation.text(label)

    def named_dests(self) -> list[tuple[str, int]]:
        """FPDF_CountNamedDests, FPDF_GetNamedDest and FPDFDest_GetDestPageIndex."""
        return [(navigation.text(name).rstrip("\x00"), navigation.dest_page_index(self.pdf, dest))
                for name, dest in navigation.named_dests(self.pdf)]

    def save(self, pages: Sequence[int] | None = None, boxes: dict[int, Box] | None = None) -> bytes:
        keep = list(range(len(self))) if pages is None else sorted({int(p) for p in pages})
        user = {}
        for index, (x0, y0, x1, y1) in (boxes or {}).items():
            page = self[int(index)]
            user[int(index)] = (page.left + x0, page.top - y1, page.left + x1, page.top - y0)
        return write_file(self.pdf, keep, user)

    def close(self) -> None:
        # FPDF_CloseDocument destroys CPDF_DocPageData, and with it the document's fonts and the
        # CFX_Fonts under them: the system faces this document was the last to hold go too
        # (`fontmapper.release`, PDFium's ObservedPtr caches), so the next document opens its own
        # instead of inheriting the charmap this one's LoadGlyphMap left selected.
        self._pages.clear()
        self._font_cache.clear()
        if self._open:
            self._open = False
            from . import fontmapper
            fontmapper.document_closed(self.pdf)


class PureBackend:
    name = "pure"
    renders = False

    def open(self, source: str | Path | bytes) -> Document:
        return Document(source)
