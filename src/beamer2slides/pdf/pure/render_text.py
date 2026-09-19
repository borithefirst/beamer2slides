"""PDFium's text drawing, ported: CPDF_RenderStatus::ProcessText over CPDF_TextRenderer and
CFX_RenderDevice::DrawNormalText / DrawTextPath on the AGG back end, value for value.

A glyph is drawn the way PDFium draws it on a display bitmap without FPDF_LCD_TEXT: FreeType
renders it in FT_RENDER_MODE_LCD (`ftgrays.render_lcd`, the outline from `ftoutline`: Adobe's CFF
engine, no hinting, under FT_Set_Transform), and DrawNormalTextHelper folds each pixel's three
subpixel values into one coverage (their average, shifted by the origin's third of a pixel),
gamma-adjusts it with kTextGammaAdjust and merges the fill colour into a copy of the pixels under
the text (GetDIBits; zeros on a BGRA device), which SetDIBits then puts back through the clip.
Glyph bitmaps are cached per font under PDFium's key (the matrix times 10000, truncated), so the
first rendering under a key is the one every later one reuses, as in CFX_GlyphCache.

Big text (|a| + |b| of the glyph matrix above 50 device pixels) and every stroked mode go through
DrawTextPath: the glyph outlines (LoadGlyphPath) filled / stroked as paths by `render.Device`.

Refused (`unsupported`), so that a page is drawn exactly or not at all: Type 3 fonts, fonts
without an embedded Type 1 / CFF program (standard 14 and the other substituted fonts, TrueType),
codes whose glyph the font lacks (PDFium falls back to another font), pattern colours, render modes outside 0..7, and text drawn into a soft mask (a mask device renders glyphs
in FT_RENDER_MODE_NORMAL). Text clip modes (4..7) draw like 0..3, and their glyph outlines become
a clip (`clip_text_path`, called by render.Status.process_clip): the AGG device has soft clips, so
ProcessClipPath follows a clip's texts, one winding clip per BT..ET group. Vertical writing only
moves the origins (`_origin`, GetCharPosList): an embedded font's glyphs are drawn as they are."""

from __future__ import annotations

import math

import numpy as np

from ..api import PdfError
from . import ftgrays, ftoutline
from . import raster as R
from .raster import F

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_GLYPH_DIMENSION = 2048                  # kMaxGlyphDimension

# kTextGammaAdjust (cfx_renderdevice.cpp)
GAMMA = np.array([
    0, 2, 3, 4, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 29, 30,
    31, 32, 33, 34, 35, 36, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 52, 53, 54, 55,
    56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 77, 78, 79,
    80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102,
    103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121,
    122, 123, 124, 125, 126, 127, 128, 129, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139,
    140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 156, 157,
    158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 174, 175,
    176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 190, 191, 192, 193,
    194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 204, 205, 206, 207, 208, 209, 210, 211,
    212, 213, 214, 215, 216, 217, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 228,
    229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 239, 240, 241, 242, 243, 244, 245, 246,
    247, 248, 249, 250, 250, 251, 252, 253, 254, 255], np.int32)
assert len(GAMMA) == 256

MODE_FILL, MODE_STROKE, MODE_FILL_STROKE, MODE_INVISIBLE = 0, 1, 2, 3
MODE_CLIP = 7


# ---------------------------------------------------------------------- what cannot be drawn


def _face(font):
    """The font's ftoutline.Face, or the reason there is none (remembered on the font)."""
    why = font.__dict__.get("_b2s_face_refused")
    if why is not None:
        return None, why
    try:
        return ftoutline.face_of(font), None
    except ftoutline.Unported as e:
        why = f"text in a font the port cannot draw ({e})"
        font.__dict__["_b2s_face_refused"] = why
        return None, why


def glyph_of(font, code: int) -> int:
    """CPDF_Font::GlyphFromCharCode: -1 when the font has no glyph for the code."""
    if font.subtype == "Type0":
        return font._glyph(code)
    if code > 0xFF:
        return -1
    g = font.glyphs[code]
    return -1 if g is None or g == 0xFFFF else g


def _origin(obj, item) -> tuple[float, float]:
    """GetCharPosList's origin: (x, 0), or in vertical writing (0, y) less the font size times the
    char's vertical origin / 1000 (GetItemInfo's rule; embedded fonts have no CID transform)."""
    from .content import item_origin
    x, y = item_origin(obj, item)
    return F(x), F(y)


def unsupported(obj) -> str | None:
    """Why text object `obj` cannot be drawn exactly, or None."""
    mode = obj.text_mode
    if not 0 <= mode <= 7:
        return f"text render mode {mode}"
    font = obj.font
    if not obj.items or font is None:
        return None
    if font.is_type3:
        return "Type 3 text"
    if mode in (MODE_INVISIBLE, MODE_CLIP):
        return None
    if obj.pattern:
        return "text in a pattern colour"
    return _outline_refusal(obj)


def _outline_refusal(obj) -> str | None:
    """Why the glyph outlines of text object `obj` cannot be had exactly, or None."""
    font = obj.font
    if not font.embedded:
        return "text in a font that is not embedded"
    _, why = _face(font)
    if why is not None:
        return why
    for code, _x in obj.items:
        if glyph_of(font, code) < 0:
            return "text needing a fallback font"
    return None


def clip_unsupported(clip_texts: tuple) -> str | None:
    """Why a clip's texts (content.append_texts) cannot be turned into a clip exactly, or None.
    Only their outlines count: colours, patterns and soft masks play no part in a clip."""
    seen = _CLIP_VERDICTS.get(id(clip_texts))
    if seen is not None and seen[0] is clip_texts:
        return seen[1]
    why = None
    for text in clip_texts:
        if text is None or not text.items or text.font is None:
            continue
        why = _outline_refusal(text)
        if why is not None:
            why = f"a text clip: {why}"
            break
    if len(_CLIP_VERDICTS) > 4096:
        _CLIP_VERDICTS.clear()
    _CLIP_VERDICTS[id(clip_texts)] = (clip_texts, why)
    return why


_CLIP_VERDICTS: dict = {}


# ---------------------------------------------------------------------- ProcessText


def process_text(status, obj, matrix) -> None:
    """CPDF_RenderStatus::ProcessText (no clipping path: text clips never reach AGG)."""
    try:
        _process_text(status, obj, matrix)
    except ftoutline.Unported as e:
        raise PdfError(f"the pure reader cannot render this text yet ({e})") from e


def _process_text(status, obj, matrix) -> None:
    from .render import _argb, _available
    if not obj.items:
        return
    mode = obj.text_mode
    if mode in (MODE_INVISIBLE, MODE_CLIP):
        return
    if getattr(status.dev, "mask_format", False):
        raise PdfError("the pure reader cannot render text into a soft mask yet")
    font = obj.font
    face, why = _face(font)
    if face is None:
        raise PdfError(f"the pure reader cannot render {why}")
    base = mode & 3
    is_fill = base in (MODE_FILL, MODE_FILL_STROKE)
    is_stroke = base in (MODE_STROKE, MODE_FILL_STROKE)
    tr = status.transfer(obj)
    stroke_argb = _argb(obj.stroke, obj.stroke_alpha, tr) if is_stroke else 0
    fill_argb = _argb(obj.fill, obj.fill_alpha, tr) if is_fill else 0
    text_matrix = tuple(obj.matrix)
    if not _available(text_matrix):
        return
    size = F(obj.font_size)
    chars = [(glyph_of(font, item[0]), *_origin(obj, item)) for item in obj.items]
    if is_stroke:
        device_matrix = matrix
        ctm = obj.text_ctm
        if ctm[0] != 1.0 or ctm[3] != 1.0:
            m = (F(ctm[0]), F(ctm[1]), F(ctm[2]), F(ctm[3]), 0.0, 0.0)
            text_matrix = R.concat(text_matrix, R.inverse(m))
            device_matrix = R.concat(m, matrix)
        graph = (F(obj.line_width), obj.line_cap, obj.line_join, F(obj.miter),
                 tuple(F(v) for v in obj.dash), F(obj.dash_phase))
        draw_text_path(status.dev, face, chars, size, text_matrix, device_matrix, graph,
                       fill_argb, stroke_argb, is_stroke and is_fill)
        return
    draw_normal_text(status.dev, face, chars, size, R.concat(text_matrix, matrix), fill_argb)


# ---------------------------------------------------------------------- DrawTextPath


_KINDS = {"M": R.PT_MOVE, "L": R.PT_LINE, "C": R.PT_BEZIER}


def draw_text_path(dev, face, chars, size, text2user, user2device, graph, fill_argb: int,
                   stroke_argb: int, stroke: bool) -> None:
    """CFX_RenderDevice::DrawTextPath: each glyph's outline as a path."""
    from .render import FILL_NONE, FILL_WINDING
    if not (fill_argb or stroke_argb):
        return
    fill_type = FILL_WINDING if fill_argb else FILL_NONE
    for glyph, x, y in chars:
        path = face.path(glyph)
        if path is None:
            continue
        m = R.concat(IDENTITY, (size, 0.0, 0.0, size, x, y))       # GetEffectiveMatrix
        m = R.concat(m, text2user)
        points = []
        for px, py, kind, close in path:
            tx, ty = R.transform(m, px, py)
            points.append((tx, ty, _KINDS[kind], close))
        dev.draw_path(points, user2device, graph, fill_argb, stroke_argb, fill_type, stroke,
                      text_mode=True)


def clip_text_path(obj, matrix, out: list) -> None:
    """ProcessText with a clipping path: DrawTextPath appends each glyph's outline, through the
    text matrix (the CTM is not taken out, whatever the mode) and then `matrix` (CFX_Path::Append
    with the object-to-device matrix), to `out`, a device-space path."""
    from .render import _available
    if not obj.items:
        return
    text_matrix = tuple(obj.matrix)
    if not _available(text_matrix):
        return
    font = obj.font
    face, why = _face(font)
    if face is None:
        raise PdfError(f"the pure reader cannot render {why}")
    size = F(obj.font_size)
    for item in obj.items:
        path = face.path(glyph_of(font, item[0]))
        if path is None:
            continue
        x, y = _origin(obj, item)
        m = R.concat(IDENTITY, (size, 0.0, 0.0, size, x, y))          # GetEffectiveMatrix
        m = R.concat(m, text_matrix)
        for px, py, kind, close in path:
            tx, ty = R.transform(m, px, py)
            dx, dy = R.transform(matrix, tx, ty)
            out.append((dx, dy, _KINDS[kind], close))


# ---------------------------------------------------------------------- DrawNormalText


def _roundf(v: float) -> int:
    """FXSYS_roundf."""
    if v != v:
        return 0
    if v < -2147483648.0:
        return -2147483648
    if v >= 2147483647.0:
        return 2147483647
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def _floor_int(v: float) -> int:
    """static_cast<int>(floor(v)), saturated."""
    if v != v:
        return 0
    return int(max(-2147483648, min(2147483647, math.floor(v))))


def _key_part(v: float) -> int:
    """static_cast<int>(v * 10000)."""
    p = F(v * 10000.0)
    if p != p or math.isinf(p):
        return 0
    return int(p)


def load_glyph_bitmap(face, glyph: int, matrix):
    """CFX_GlyphCache::LoadGlyphBitmap (kLcd): (left, top, width_bytes, rows, array) or None."""
    cache = face.__dict__.setdefault("_b2s_glyph_bitmaps", {})
    key = (_key_part(matrix[0]), _key_part(matrix[1]), _key_part(matrix[2]), _key_part(matrix[3]))
    sizes = cache.setdefault(key, {})
    if glyph in sizes:
        return sizes[glyph]
    sizes[glyph] = bm = render_glyph(face, glyph, matrix)
    return bm


def render_glyph(face, glyph: int, matrix):
    """CFX_Face::RenderGlyph in FT_RENDER_MODE_LCD."""
    a, b, c, d = matrix[:4]
    ft = (int(F(F(a / 64.0) * 65536.0)), int(F(F(c / 64.0) * 65536.0)),
          int(F(F(b / 64.0) * 65536.0)), int(F(F(d / 64.0) * 65536.0)))
    outline = face.outline(glyph, ft)
    if outline is None:
        return None
    got = ftgrays.render_lcd(outline)
    if got is None:
        return None
    left, top, width, rows, pitch, buf = got
    if width > MAX_GLYPH_DIMENSION or rows > MAX_GLYPH_DIMENSION:
        return None
    arr = np.frombuffer(buf, np.uint8).reshape(rows, pitch)[:, :width]
    return left, top, width, rows, arr


def draw_normal_text(dev, face, chars, size, text2device, fill_argb: int) -> None:
    """CFX_RenderDevice::DrawNormalText on a display device (kLcd, normalize)."""
    from .render_transparency import get_dibits, kind, set_dibits
    a, b, c, d, e, f = text2device
    char2device = (F(a * size), F(b * -size), F(c * size), F(d * -size), F(e * size), F(f * -size))
    if F(abs(char2device[0]) + abs(char2device[1])) > 50.0:
        draw_text_path(dev, face, chars, size, text2device, None, None, fill_argb, 0, False)
        return
    glyphs = []
    matrix = R.concat(IDENTITY, char2device)                  # GetEffectiveMatrix
    for glyph, x, y in chars:
        ox, oy = R.transform(text2device, x, y)
        bm = load_glyph_bitmap(face, glyph, matrix)
        glyphs.append((ox, _floor_int(ox), _roundf(oy), bm))
    # GetGlyphsBBox
    rect = None
    for _dx, gx, gy, bm in glyphs:
        if bm is None:
            continue
        left, top, width, rows, _ = bm
        x, y = gx + left, gy - top
        r = (x, y, x + width // 3, y + rows)
        rect = r if rect is None else (min(rect[0], r[0]), min(rect[1], r[1]),
                                       max(rect[2], r[2]), max(rect[3], r[3]))
    if rect is None:
        rect = (0, 0, 0, 0)
    rect = R.rect_intersect(rect, dev.clip_box())
    if R.rect_empty(rect):
        return
    pl, pt, pr, pb = rect
    pw, ph = pr - pl, pb - pt
    if dev.alpha:
        bitmap = np.zeros((ph, pw, 4), np.uint8)
        bkind = "bgra"
    else:
        bitmap = get_dibits(dev, rect)
        bkind = kind(dev)
    color = (fill_argb & 0xFF, (fill_argb >> 8) & 0xFF, (fill_argb >> 16) & 0xFF, fill_argb >> 24)
    for dx, gx, gy, bm in glyphs:
        if bm is None:
            continue
        left, top, width, rows, arr = bm
        px, py = gx + left - pl, gy - top - pt
        t = int(F(dx * 3.0))
        x_subpixel = t % 3 if t >= 0 else -((-t) % 3)          # C's %
        _helper(bitmap, dev.alpha, arr, width // 3, rows, px, py, x_subpixel, color)
    set_dibits(dev, bitmap, bkind, pl, pt, "Normal")


def _helper(bitmap, has_alpha: bool, g8, ncols: int, nrows: int, px: int, py: int,
            x_subpixel: int, color) -> None:
    """DrawNormalTextHelper with normalize (NormalizeSrc on the first column of a shifted glyph,
    NormalizeDest elsewhere); each destination pixel is written once per glyph."""
    H, W = bitmap.shape[:2]
    start, end = max(px, 0), min(px + ncols, W)
    if start >= end:
        return
    r0, r1 = max(0, -py), min(nrows, H - py)
    if r0 >= r1:
        return
    g = g8[r0:r1].astype(np.int32)
    s = (np.arange(start, end) - px) * 3
    if x_subpixel == 0:
        v = (g[:, s] + g[:, s + 1] + g[:, s + 2]) // 3
    elif x_subpixel == 1:
        v = (g[:, np.maximum(s - 1, 0)] + g[:, s] + g[:, s + 1]) // 3
        if start == px:
            v[:, 0] = (g[:, 0] + g[:, 1]) // 3
    else:
        v = (g[:, np.maximum(s - 2, 0)] + g[:, np.maximum(s - 1, 0)] + g[:, s]) // 3
        if start == px:
            v[:, 0] = g[:, 0] // 3
    cb, cg, cr, ca = color
    sa = GAMMA[v] * ca // 255
    dest = bitmap[py + r0:py + r1, start:end]
    d = dest.astype(np.int32)
    col = np.array([cb, cg, cr], np.int32)
    if not has_alpha:
        k = sa[..., None]
        dest[..., :3] = ((d[..., :3] * (255 - k) + col * k) // 255).astype(np.uint8)
        return
    back = d[..., 3]
    fresh = back == 0
    if x_subpixel != 0:
        fresh[:, 0] &= sa[:, 0] != 0            # NormalizeSrc leaves sa == 0 alone
    mix = (back != 0) & (sa != 0)
    da = back + sa - back * sa // 255
    ratio = np.where(mix, sa * 255 // np.maximum(da, 1), 0)[..., None]
    merged = (d[..., :3] * (255 - ratio) + col * ratio) // 255
    rgb = np.where(fresh[..., None], col, np.where(mix[..., None], merged, d[..., :3]))
    dest[..., :3] = rgb.astype(np.uint8)
    dest[..., 3] = np.where(fresh, sa, np.where(mix, da, back)).astype(np.uint8)
