"""PDFium's Type 3 text drawing, ported: CPDF_RenderStatus::ProcessType3Text over CPDF_Type3Font /
CPDF_Type3Char (a glyph procedure parsed as a form), CPDF_Type3Cache and CPDF_Type3GlyphMap.

A glyph is drawn one of two ways, as PDFium decides per char:

- A glyph whose procedure is one image and nothing else, in a font that says d1 (not coloured), is
  a bitmap (LoadBitmapFromSoleImageOfForm: the image mask as PDFium decodes it). Under each matrix
  (the key is a, b, c, d times 10000, rounded) it is stretched to its device size once: the height
  runs between two lines snapped to the glyph map's "blues" (AdjustBlue: a line within 0.8 px of
  one used before is that one, so the baselines and x-heights of a word agree), the width is `a`
  truncated. The bitmaps of one text object are put together in an 8-bit mask at the fill alpha
  and that mask set on the device at the fill colour, so a translucent text's alpha counts twice,
  as it does in PDFium; glyphs are set one by one instead once a char was drawn as a form.
  A glyph that is not upright, or upright with a blank first or last row, goes through
  TransformTo (CFX_ImageTransformer, `render_image.transform`; its kNormal branch for upright
  bitmaps: the unit square's closest rect, the stretch size `a`/`d` rounded away from zero).
- Any other glyph is its objects (paths, images, shadings, forms, Type 3 text), drawn by a render
  status of its own with rect antialiasing, in the text's fill colour unless the glyph is coloured
  (d0) and the object sets a colour of its own (GetFillArgb's Type 3 rule, render.Status.fill_argb;
  image masks too). A translucent text draws each such glyph into a bitmap of its own first,
  composited with SetDIBits. Text in a font whose glyph is being drawn draws nothing (PDFium's
  recursion guard), and CPDF_Type3Font::LoadChar gives up at four levels of loading.

Refused (`unsupported`, else PdfError while drawing), so that a page is drawn exactly or not at
all: glyph procedures with a /Matrix, /BBox or /Group, named resources in a font without
/Resources (PDFium then looks in whichever page last selected the font), transparency, text clips
and text other than Type 3 inside glyphs drawn as forms, Type 3 fonts nested more than two levels,
glyph images that are not image masks, pattern colours, and Type 3 text drawn into a soft mask."""

from __future__ import annotations

import numpy as np

from ..api import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT, PdfError
from . import raster as R
from .raster import F

MAX_BLUES = 16                     # kType3MaxBlues
MODE_INVISIBLE = 3
HUGE = 1 << 26                     # pixels: a bigger scratch bitmap is refused, not allocated
DEVICE_SPACES = ("DeviceGray", "DeviceRGB", "DeviceCMYK", "G", "RGB", "CMYK", "Pattern")
ANYWHERE = (-2147483648, -2147483648, 2147483647, 2147483647)


def _roundf(v: float) -> int:
    from .render_text import _roundf as roundf
    return roundf(v)


# ---------------------------------------------------------------------- CPDF_Type3Char


class Char:
    """CPDF_Type3Char: `colored` (the procedure's last d0 / d1), `objects` (the form's top-level
    objects, None once there is no form), `every` (all of them, nested ones included), and after
    LoadBitmapFromSoleImageOfForm `bitmap` (a decode_image.DIB or None) and `image_matrix`."""

    __slots__ = ("colored", "objects", "every", "bitmap", "image_matrix", "why", "image")

    def __init__(self, colored: bool, objects, every, why):
        self.colored, self.objects, self.every, self.why = colored, objects, every, why
        self.bitmap = None
        self.image_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.image = None


def _fonts(doc) -> dict:
    """The fonts glyph procedures select (CPDF_DocPageData's font map: one font per dictionary)."""
    from .fonts import doc_fonts
    return doc_fonts(doc)


def load_char(font, code: int) -> Char | None:
    """CPDF_Type3Font::LoadChar: the char's procedure parsed as a form, cached on the font."""
    cache = font.__dict__.setdefault("_b2s_type3_chars", {})
    if code not in cache:
        cache[code] = _load_char(font, code)
    return cache[code]


def _load_char(font, code: int) -> Char | None:
    from .content import Parser, State, check_clip
    from .syntax import InlineImage, Name, Stream, operations
    doc = font.doc
    r = doc.resolve
    name = font.char_name(code)
    if not name:
        return None
    stream = r(font.procs.get(name))
    if not isinstance(stream, Stream):
        return None
    d = stream.dict
    font_res = r(font.dict.get("Resources"))
    proc_res = r(d.get("Resources"))
    data = doc.stream_data(stream)
    why = None
    for key in ("Matrix", "BBox", "Group"):
        if r(d.get(key)) is not None:
            why = f"Type 3 glyph procedures with a /{key}"
    colored = False
    for op, args in operations(data):
        if op == "d0":
            colored = True
        elif op == "d1":
            colored = False
        elif not isinstance(font_res, dict) and why is None:
            # the resources would be those of the page that last selected the font
            named = op in ("Tf", "Do", "gs", "sh")
            if op in ("cs", "CS") and args:
                named = str(args[-1]) not in DEVICE_SPACES
            elif op in ("scn", "SCN") and args:
                named = isinstance(args[-1], Name)
            elif op == "BI" and args and isinstance(args[0], InlineImage):
                cs = args[0].dict.get("ColorSpace", args[0].dict.get("CS"))
                named = isinstance(cs, Name) and str(cs) not in DEVICE_SPACES
            if named:
                why = "named resources in a Type 3 font without /Resources"
    objects: list = []
    parser = Parser(doc, font_res if isinstance(font_res, dict) else {}, objects, _fonts(doc), {})
    parser.parsed.append(stream)
    state = State(fill_set=False, stroke_set=False)
    try:
        parser._run(data, proc_res if isinstance(proc_res, dict) else font_res, state,
                    (0.0, 0.0, 0.0, 0.0), None)
    except RecursionError:
        why = why or "Type 3 glyphs nested this deep"
    top = [o for o in objects if o.parent is None]
    check_clip(top)
    return Char(colored, top or None, objects, why)


def _independent_bitmap(font, img):
    """CPDF_ImageObject::GetIndependentBitmap: the image as CPDF_DIB loads it (no resources)."""
    from . import decode_image as DI
    stream = img.stream
    if not getattr(stream, "exact", True):
        raise PdfError("the pure reader cannot render inline images whose codec's end is not "
                       "found as PDFium finds it (DCT, CCITT) yet")
    try:
        dib = DI.load(font.doc, stream, {}, (0, 0))
    except DI.Unsupported as e:
        raise PdfError(f"the pure reader cannot render {e} yet") from e
    if dib is None:
        return None
    if dib.fmt != "mask1" or dib.mask is not None:
        raise PdfError("the pure reader cannot render Type 3 glyph images that are not image "
                       "masks yet")
    if dib.w * dib.h > HUGE:
        raise PdfError("the pure reader cannot render Type 3 glyph images this big yet")
    return dib


def sole_image(font, ch: Char) -> bool:
    """CPDF_Type3Char::LoadBitmapFromSoleImageOfForm: True when the char is drawn as a bitmap
    (or not at all), False when it is drawn as a form."""
    if ch.bitmap is not None or ch.objects is None:
        return True
    if ch.colored:
        return False
    if len(ch.objects) != 1 or ch.objects[0].type != OBJ_IMAGE:
        return False
    img = ch.objects[0]
    ch.bitmap = _independent_bitmap(font, img)
    ch.image_matrix = tuple(F(v) for v in img.matrix)
    ch.objects = None
    return True


# ---------------------------------------------------------------------- what cannot be drawn


def unsupported(obj, chain: tuple = ()) -> str | None:
    """Why Type 3 text object `obj` (not invisible) cannot be drawn exactly, or None. `chain`:
    the fonts (their dictionaries' ids) whose glyphs it is drawn in."""
    font = obj.font
    if obj.pattern:
        return "Type 3 text in a pattern colour"
    key = id(font.dict)
    if key in chain:
        return None                    # ProcessType3Text draws nothing: the font is being drawn
    if len(chain) >= 3:
        return "Type 3 fonts nested more than two levels"
    for code in sorted({c for c, _x in obj.items}):
        ch = load_char(font, code)
        if ch is None:
            continue
        why = _char_refusal(font, ch, chain + (key,))
        if why is not None:
            return why
    return None


def _char_refusal(font, ch: Char, chain: tuple) -> str | None:
    if ch.why is not None:
        return ch.why
    if ch.objects is None and ch.bitmap is None:
        return None
    try:
        if sole_image(font, ch):
            return None
    except PdfError as e:
        return str(e).removeprefix("the pure reader cannot render ").removesuffix(" yet")
    for o in ch.every:
        if o.smask is not None or o.blend != "Normal" or o.transfer is not None or o.soft_mask:
            return "transparency in Type 3 glyphs"
        if getattr(o, "clip_texts", None):
            return "text clips in Type 3 glyphs"
        if o.type in (OBJ_IMAGE, OBJ_SHADING):
            continue                   # drawn by the glyph's render status like any other
        if o.type == OBJ_FORM:
            if o.group or o.fill_alpha != 1.0:
                return "transparency in Type 3 glyphs"
        elif o.type == OBJ_PATH:
            if o.pattern:
                return "pattern colours in Type 3 glyphs"
        elif o.type == OBJ_TEXT:
            if not o.items or o.font is None or o.text_mode == MODE_INVISIBLE:
                continue
            if not o.font.is_type3 or not 0 <= o.text_mode <= 7:
                return "text in Type 3 glyphs"
            why = unsupported(o, chain)
            if why is not None:
                return why
        else:
            return "objects"
    return None


# ---------------------------------------------------------------------- CPDF_Type3Cache


class _GlyphMap:
    """CPDF_Type3GlyphMap."""

    def __init__(self):
        self.top_blue: list = []
        self.bottom_blue: list = []
        self.glyphs: dict = {}

    @staticmethod
    def _adjust(pos: float, blues: list) -> int:
        """AdjustBlueHelper."""
        min_distance = F(1000000.0)
        closest = -1
        for i, blue in enumerate(blues):
            distance = F(abs(F(pos - float(blue))))
            if distance < min(F(0.8), min_distance):
                min_distance = distance
                closest = i
        if closest >= 0:
            return blues[closest]
        new_pos = _roundf(pos)
        if len(blues) < MAX_BLUES:
            blues.append(new_pos)
        return new_pos

    def adjust_blue(self, top: float, bottom: float) -> tuple[int, int]:
        return self._adjust(top, self.top_blue), self._adjust(bottom, self.bottom_blue)


class _Glyph:
    """CFX_GlyphBitmap: `left`, `top` (minus the top line) and the mask ("mask1": 0/1 values,
    "mask8": 0..255) as an (h, w) array."""

    __slots__ = ("left", "top", "kind", "mask")

    def __init__(self, left: int, top: int, kind: str, mask: np.ndarray):
        self.left, self.top, self.kind, self.mask = left, top, kind, mask


class _Cache:
    """CPDF_Type3Cache: glyph maps by the matrix's size key (it lives while a text object that
    found a glyph in it is being drawn: CPDF_DocRenderData holds it weakly)."""

    def __init__(self, font):
        self.font = font
        self.maps: dict = {}

    def load(self, code: int, m) -> _Glyph | None:
        """LoadGlyphBitmap."""
        key = tuple(_roundf(F(v * 10000)) for v in m[:4])
        gm = self.maps.get(key)
        if gm is None:
            gm = self.maps[key] = _GlyphMap()
        g = gm.glyphs.get(code)
        if g is not None:
            return g
        g = gm.glyphs[code] = self._render(gm, code, m)
        return g

    def _render(self, gm: _GlyphMap, code: int, m) -> _Glyph | None:
        """RenderGlyph."""
        ch = load_char(self.font, code)
        if ch is None or ch.bitmap is None:
            return None
        dib = ch.bitmap
        a, b, c, d, e, f = R.concat(ch.image_matrix, (m[0], m[1], m[2], m[3], 0.0, 0.0))
        res = None
        left = top = 0
        if abs(b) < F(abs(a) / 100) and abs(c) < F(abs(d) / 100):
            rows = dib.rows.any(axis=1)
            nz = np.flatnonzero(rows)
            if len(nz) and nz[0] == 0 and nz[-1] == dib.h - 1:
                top_y, bottom_y = F(d + f), f
                flipped = top_y > bottom_y
                if flipped:
                    top_y, bottom_y = bottom_y, top_y
                top_line, bottom_line = gm.adjust_blue(top_y, bottom_y)
                height = top_line - bottom_line if flipped else bottom_line - top_line
                if not -2147483648 <= height <= 2147483647:
                    return None
                if not -2147483648.0 < a < 2147483648.0:
                    raise PdfError("the pure reader cannot render Type 3 glyphs this wide yet")
                res = _stretch_to(dib, int(a), height)
                top = top_line
                left = _roundf(F(e + a)) if a < 0 else _roundf(e)
        if res is None:
            res, left, top = _transform_to(dib, (a, b, c, d, e, f))
            if res is None:
                return None
        kind, mask = res
        return _Glyph(left, -top, kind, mask)


def _stretch_to(dib, dw: int, dh: int):
    """CFX_DIBBase::StretchTo with no clip: ("mask1" | "mask8", pixels) or None."""
    from .render_image import stretch
    if dw == 0 or dh == 0:
        return None
    if dw == dib.w and dh == dib.h:
        return "mask1", dib.rows.astype(np.uint8)
    if abs(dw) * abs(dh) > HUGE:
        raise PdfError("the pure reader cannot render Type 3 glyphs this big yet")
    got = stretch(dib, dw, dh, (0, 0, abs(dw), abs(dh)), False)
    if got is None:
        return None
    block, fmt, _pal = got
    return "mask8", block[..., 0].copy()


def _transform_to(dib, m):
    """CFX_DIBBase::TransformTo (CFX_ImageTransformer with no clip): ((kind, pixels), left, top),
    or (None, 0, 0)."""
    from .render_image import closest_rect, stretch, transform
    a, b, c, d = m[:4]
    quarter = abs(a) < F(abs(b) / 20) and abs(d) < F(abs(c) / 20) and abs(a) < 0.5 and abs(d) < 0.5
    if not quarter and abs(b) < F(0.05) and abs(c) < F(0.05):
        # kNormal: the stretcher at (ceil a, -ceil d), clipped to the unit square's closest rect
        rl, rt, rr, rb = closest_rect(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
        if rr <= rl or rb <= rt:
            return None, 0, 0
        wd = int(np.ceil(a)) if a > 0 else int(np.floor(a))
        hd = -int(np.ceil(d)) if d > 0 else -int(np.floor(d))
        if wd == 0 or hd == 0:
            return None, 0, 0
        if abs(wd) * abs(hd) > HUGE:
            raise PdfError("the pure reader cannot render Type 3 glyphs this big yet")
        clip = (0, 0, min(rr - rl, abs(wd)), min(rb - rt, abs(hd)))
        got = stretch(dib, wd, hd, clip, False)
        if got is None:
            return None, 0, 0
        block = got[0]
        return ("mask8", block[..., 0].copy()), rl, rt
    got = transform(dib, m, ANYWHERE, False)
    if got is None:
        return None, 0, 0
    block, fmt, _pal, box = got
    if fmt != "T:mask8":
        raise PdfError("the pure reader cannot render this Type 3 glyph yet")
    if block.size > HUGE:
        raise PdfError("the pure reader cannot render Type 3 glyphs this big yet")
    return ("mask8", block.reshape(block.shape[:2])), box[0], box[1]


# ---------------------------------------------------------------------- SetBitMask


def set_bit_mask(dev, kind: str, mask: np.ndarray, left: int, top: int, argb: int) -> None:
    """CFX_RenderDevice::SetBitMask -> the AGG driver's SetDIBits -> CFX_DIBitmap::CompositeMask:
    CompositeRow_BitMask2Rgb/Argb or ByteMask2Rgb/Argb through the clip region."""
    alpha = argb >> 24
    if alpha == 0:
        return
    h, w = mask.shape
    if w == 0 or h == 0:
        return
    cb = dev.clip_box()
    rect = R.rect_intersect((left, top, left + w, top + h), (0, 0, dev.w, dev.h))
    rect = R.rect_intersect(rect, cb)
    wh, ww = dev.bgra.shape[:2]
    rect = R.rect_intersect(rect, (dev.ox, dev.oy, dev.ox + ww, dev.oy + wh))
    if R.rect_empty(rect):
        return
    l, t, r, b = rect
    v = mask[t - top:b - top, l - left:r - left].astype(np.int32)
    dest = dev.bgra[t - dev.oy:b - dev.oy, l - dev.ox:r - dev.ox]
    clip = None
    if dev.clip is not None and dev.clip.mask is not None:
        clip = dev.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
    if kind == "mask8":
        src = alpha * v * clip // 255 // 255 if clip is not None else alpha * v // 255
        dev._blend(dest, src, argb, span=False)
        return
    on = v > 0
    if not on.any():
        return
    src = alpha * clip // 255 if clip is not None else np.full(v.shape, alpha, np.int32)
    saved = dest.copy()
    dev._blend(dest, np.where(on, src, 0), argb, span=False)
    dest[~on] = saved[~on]


def _composite_into(mask: np.ndarray, g: _Glyph, x: int, y: int, alpha: int) -> None:
    """CFX_DIBitmap::CompositeMask onto a k8bppMask bitmap: CompositeRow_BitMask2Mask /
    ByteMask2Mask at the colour's alpha."""
    h, w = g.mask.shape
    H, W = mask.shape
    l, t = max(x, 0), max(y, 0)
    r, b = min(x + w, W), min(y + h, H)
    if r <= l or b <= t:
        return
    v = g.mask[t - y:b - y, l - x:r - x].astype(np.int32)
    back = mask[t:b, l:r]
    if g.kind == "mask8":
        src = alpha * v // 255
        on = np.ones(v.shape, bool)
    else:
        on = v > 0
        src = np.full(v.shape, alpha, np.int32)
    new = np.where(back == 0, src, np.where(src > 0, back + src - back * src // 255, back))
    mask[t:b, l:r] = np.where(on, new, back)


# ---------------------------------------------------------------------- ProcessType3Text


def _valid(rect) -> bool:
    return -2147483648 <= rect[2] - rect[0] <= 2147483647 and \
        -2147483648 <= rect[3] - rect[1] <= 2147483647


def _form_status(status, dev, ch: Char, fill_argb: int, key):
    from .render import Status
    sub = Status(dev, (False, False), False, 1.0, status.ctx, None)
    sub.type3_char, sub.t3_fill, sub.rect_aa = ch, fill_argb, True
    sub.type3_fonts = status.type3_fonts + (key,)
    return sub


def process_type3_text(status, obj, matrix) -> None:
    """CPDF_RenderStatus::ProcessType3Text (a display device, not a printer)."""
    from .render import Device, _argb
    from .render_transparency import set_dibits
    font = obj.font
    key = id(font.dict)
    if key in status.type3_fonts:
        return
    dev = status.dev
    if getattr(dev, "mask_format", False):
        raise PdfError("the pure reader cannot render Type 3 text into a soft mask yet")
    if obj.pattern:
        raise PdfError("the pure reader cannot render Type 3 text in a pattern colour yet")
    # GetFillArgbForType3: the object's colour, or the status's initial one
    ref = obj.fill if obj.fill is not None else status.initial_fill
    fill_argb = _argb(ref, obj.fill_alpha, status.transfer(obj))
    fill_alpha = fill_argb >> 24
    size = F(obj.font_size)
    char_matrix = tuple(F(F(v) * size) for v in font.matrix)
    text_matrix = tuple(obj.matrix)
    n = len(obj.items)
    glyphs: list = [None] * n
    listing = n > 0                    # !glyphs.empty()
    kept = None                        # the Type 3 cache a found glyph keeps alive
    for i, (code, x) in enumerate(obj.items):
        if code == 0xFFFFFFFF or code < 0:
            continue
        ch = load_char(font, code)
        if ch is None:
            continue
        if ch.why is not None:
            raise PdfError(f"the pure reader cannot render {ch.why} yet")
        m = char_matrix[:4] + (F(char_matrix[4] + F(x)), char_matrix[5])
        m = R.concat(R.concat(m, text_matrix), matrix)
        if not sole_image(font, ch):
            if listing:
                for g in glyphs[:i]:
                    if g is not None:
                        glyph, (ox, oy) = g
                        set_bit_mask(dev, glyph.kind, glyph.mask, ox + glyph.left, oy - glyph.top,
                                     fill_argb)
                glyphs = []
                listing = False
            if fill_alpha == 255:
                sub = _form_status(status, dev, ch, fill_argb, key)
                dev.save()
                sub.render_list(ch.objects, m)
                dev.restore(False)
                continue
            rects = [o.rect for o in ch.objects]
            box = (min(q[0] for q in rects), min(q[1] for q in rects),
                   max(q[2] for q in rects), max(q[3] for q in rects))
            rect = R.outer_rect(R.transform_rect(m, box))
            if not _valid(rect):
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w <= 0 or h <= 0:
                return                  # CreateForNewBitmap fails: the rest is not drawn
            if w * h > HUGE:
                raise PdfError("the pure reader cannot render Type 3 glyphs this big yet")
            bd = Device(w, h, True)
            sub = _form_status(status, bd, ch, fill_argb, key)
            sub.render_list(ch.objects, m[:4] + (F(m[4] + float(-rect[0])),
                                                 F(m[5] + float(-rect[1]))))
            set_dibits(dev, bd.bgra, "bgra", rect[0], rect[1], "Normal")
            continue
        if ch.bitmap is None:
            continue
        cache = kept if kept is not None else _Cache(font)
        glyph = cache.load(code, m)
        if glyph is None:
            continue
        kept = cache
        origin = (_roundf(m[4]), _roundf(m[5]))
        if not listing:
            set_bit_mask(dev, glyph.kind, glyph.mask, origin[0] + glyph.left,
                         origin[1] - glyph.top, fill_argb)
        else:
            glyphs[i] = (glyph, origin)
    if not listing:
        return
    # GetGlyphsBBox, then the glyphs composited into one 8-bit mask
    placed = [(g, ox + g.left, oy - g.top) for g, (ox, oy) in (p for p in glyphs if p is not None)]
    if not placed:
        return
    left = min(p[1] for p in placed)
    top = min(p[2] for p in placed)
    right = max(p[1] + p[0].mask.shape[1] for p in placed)
    bottom = max(p[2] + p[0].mask.shape[0] for p in placed)
    W, H = right - left, bottom - top
    if W <= 0 or H <= 0:
        return
    if W * H > HUGE:
        raise PdfError("the pure reader cannot render Type 3 text this long yet")
    mask = np.zeros((H, W), np.int32)
    if fill_alpha:
        for g, x, y in placed:
            _composite_into(mask, g, x - left, y - top, fill_alpha)
    set_bit_mask(dev, "mask8", mask, left, top, fill_argb)
