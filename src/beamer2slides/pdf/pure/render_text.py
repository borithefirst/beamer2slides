"""PDFium's text drawing, ported: CPDF_RenderStatus::ProcessText over CPDF_TextRenderer and
CFX_RenderDevice::DrawNormalText / DrawTextPath on the AGG back end, value for value.

A glyph is drawn the way PDFium draws it on a display bitmap without FPDF_LCD_TEXT: FreeType
renders it in FT_RENDER_MODE_LCD (`ftgrays.render_lcd`, the outline from `ftoutline`: Adobe's CFF
engine, no hinting, or for glyf fonts `truetype`: FreeType's loader and bytecode hinter at 64 ppem,
under FT_Set_Transform), and DrawNormalTextHelper folds each pixel's three
subpixel values into one coverage (their average, shifted by the origin's third of a pixel),
gamma-adjusts it with kTextGammaAdjust and merges the fill colour into a copy of the pixels under
the text (GetDIBits; zeros on a BGRA device), which SetDIBits then puts back through the clip.
Glyph bitmaps are cached per font under PDFium's key (the matrix times 10000, truncated), so the
first rendering under a key is the one every later one reuses, as in CFX_GlyphCache.

Big text (|a| + |b| of the glyph matrix above 50 device pixels) and every stroked mode go through
DrawTextPath: the glyph outlines (LoadGlyphPath) filled / stroked as paths by `render.Device`.

A font the PDF does not embed is drawn from the face PDFium's font mapper picked (`_SubstFace`):
Foxit's Symbol / ZapfDingbats CFF faces, the multiple master FoxitSansMM / FoxitSerifMM blended
per glyph to the font's weight and the glyph's /Widths width, or a system TrueType face GDI picked
(`truetype_face`, hinted like an embedded one), skewed by the italic angle, with GetCharPosList's
spacing heuristic and a glyph cache per face and document.

A code whose glyph the font itself must not draw (`_uses_font` = CPDF_Font::ShouldUseFont) is drawn
from the font's one fallback face instead (`fallback_font` = FallbackFontFromCharcode: LoadSubstFace
of "Arial" at the descriptor's StemV times 5), at the glyph that face's charmap gives the code's
first Unicode unit (`fallback_glyph`); consecutive chars with the same fallback position are one
device call, as CPDF_TextRenderer splits the char pos list.

Refused (`unsupported`), so that a page is drawn exactly or not at all: what `truetype` refuses
(tricky and variable fonts, hinting that depends on earlier loads...), a fallback for vertical
writing or one the mapper cannot give a drawable face,
pattern colours, render modes outside 0..7, and text
drawn into a soft mask (a mask device renders glyphs in FT_RENDER_MODE_NORMAL). Text clip modes
(4..7) draw like 0..3, and their glyph outlines become a clip (`clip_text_path`, called by
render.Status.process_clip): the AGG device has soft clips, so ProcessClipPath follows a clip's
texts, one winding clip per BT..ET group. Vertical writing only moves the origins (`_origin`,
GetCharPosList): an embedded font's glyphs are drawn as they are."""

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
    """The face the font's glyphs are drawn from, or the reason there is none (remembered on the
    font): the embedded program's ftoutline.Face, or for a font that is not embedded the face
    PDFium's font mapper chose (`subst_face`)."""
    why = font.__dict__.get("_b2s_face_refused")
    if why is not None:
        return None, why
    try:
        return (ftoutline.face_of(font) if font.embedded else subst_face(font)), None
    except ftoutline.Unported as e:
        what = "a fallback font" if getattr(font, "is_fallback", False) else "text in a font"
        why = f"{what} the port cannot draw ({e})"
        font.__dict__["_b2s_face_refused"] = why
        return None, why


def subst_face(font):
    """CFX_Font's face for a substituted font: PDFium's built-in faces are shared by every font the
    mapper hands them to (the generic multiple master face keeps its blend between fonts). A system
    TrueType face is dispatched to `truetype_face`."""
    prog, subst = font.program, getattr(font, "subst", None)
    if prog is None or subst is None:
        raise ftoutline.Unported("a substituted font without PDFium's font mapper (no Foxit cache)")
    if font.subst_generic:
        return prog.face
    if prog.kind == "truetype" or prog.sfnt:
        return truetype_face(font)
    face = prog.__dict__.get("_b2s_draw_face")
    if face is None:
        data = getattr(prog, "face_data", None)
        if data is None:
            raise ftoutline.Unported("a substitute face that is not one of PDFium's")
        face = prog.__dict__["_b2s_draw_face"] = ftoutline.Face.from_cff(data, prog.order)
    return face


def truetype_face(font):
    """A system TrueType substitute (GDI's face), drawn like an embedded TrueType font (`truetype`):
    one face per face program, shared by every font the mapper hands it to, as CFX_FontMgr shares
    the FT_Face (its size, hinting state and twilight zone included)."""
    from .truetype import TrueTypeFace
    prog = font.program
    face = prog.__dict__.get("_b2s_draw_face")
    if face is None:
        if prog.kind != "truetype":
            raise ftoutline.Unported("a system substitute with CFF outlines")
        face = prog.__dict__["_b2s_draw_face"] = TrueTypeFace(program=prog)
    return face


class _SubstCache:
    """CFX_GlyphCache of a shared substitute face: one per face while the document's fonts live,
    so fonts drawn with one face share glyphs under PDFium's keys (UniqueKeyGen with the
    substitute's weight and italic angle; PathMapKey; WidthMapKey)."""

    def __init__(self):
        self.bitmaps: dict = {}
        self.paths: dict = {}
        self.widths: dict = {}


def _subst_cache(font, face) -> _SubstCache:
    caches = font.doc.__dict__.setdefault("_b2s_subst_glyph_caches", {})
    cache = caches.get(id(face))
    if cache is None:
        cache = caches[id(face)] = _SubstCache()
    return cache


def glyph_of(font, code: int) -> int:
    """CPDF_Font::GlyphFromCharCode: -1 when the font has no glyph for the code."""
    if font.subtype == "Type0":
        return font._glyph(code)
    if code > 0xFF:
        return -1
    g = font.glyphs[code]
    return -1 if g is None or g == 0xFFFF else g


class _Fallback:
    """The one CFX_Font a CPDF_Font falls back to (`font_fallbacks_`, always position 0), made by
    CPDF_Font::FallbackFontFromCharcode: LoadSubstFace("Arial", IsTrueTypeFont(), flags_,
    stem_v_ * 5 (kFontWeightNormal when that overflows an int32), italic_angle_, kDefANSI,
    IsVertWriting()). It stands in for a CPDF_Font wherever the drawing code asks one for its face,
    its CFX_SubstFont and its document's glyph caches."""

    is_fallback = True
    embedded = False
    vertical = False
    subtype = ""
    subst_generic = False

    def __init__(self, font):
        from . import fontmapper
        self.doc = font.doc
        weight = font.stem_v * 5
        if not -0x80000000 <= weight <= 0x7FFFFFFF:          # FX_SAFE_INT32::ValueOrDefault
            weight = 400
        face = fontmapper.load_subst_face("Arial", font.subtype == "TrueType",
                                          font.flags & 0xFFFFFFFF, weight, font.italic_angle, font.doc)
        self.program = face.program if face is not None else None
        self.subst = face.subst if face is not None else None
        self.subst_generic = face is not None and face.generic


def fallback_font(font) -> _Fallback:
    """CPDF_Font::FallbackFontFromCharcode: the fallback CFX_Font, made once per font."""
    fb = font.__dict__.get("_b2s_fallback")
    if fb is None:
        fb = font.__dict__["_b2s_fallback"] = _Fallback(font)
    return fb


def fallback_face(font) -> "_SubstFace":
    """The fallback CFX_Font's face, as `_SubstFace` draws a substituted font's."""
    fb = fallback_font(font)
    face = fb.__dict__.get("_b2s_subst")
    if face is None:
        got, why = _face(fb)
        if got is None:
            raise ftoutline.Unported(why)
        face = fb.__dict__["_b2s_subst"] = _SubstFace(got, fb, font.subtype == "Type0")
    return face


def fallback_glyph(font, code: int, face: "_SubstFace") -> int:
    """CPDF_Font::FallbackGlyphFromCharcode: the first UTF-16 unit of the code's Unicode (the code
    itself when it has none) through the fallback face's charmap (CFX_Font::GetCharIndex, which is
    FT_Get_Char_Index on whatever charmap the shared face has selected); 0 comes back as -1."""
    units = font.unicode(code)
    g = face.font.program.char_index(units[0] if units else code)
    return -1 if g == 0 else g


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
        from . import render_type3
        return None if mode == MODE_INVISIBLE else render_type3.unsupported(obj)
    if mode in (MODE_INVISIBLE, MODE_CLIP):
        return None
    if obj.pattern:
        return "text in a pattern colour"
    return _outline_refusal(obj)


def _outline_refusal(obj) -> str | None:
    """Why the glyph outlines of text object `obj` cannot be had exactly, or None."""
    font = obj.font
    if not font.embedded and font.vertical:
        return "vertical text in a substituted font (CID transform)"
    _, why = _face(font)
    if why is not None:
        return why
    for code, _x in obj.items:
        if _uses_font(font, glyph_of(font, code)):
            continue
        if font.vertical:      # LoadSubstFace(..., IsVertWriting()): a vertical face is not ported
            return "a fallback font for vertical writing"
        try:
            fallback_face(font)
        except ftoutline.Unported as e:
            return str(e)
        break
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


def _uses_font(font, glyph: int) -> bool:
    """CPDF_Font::ShouldUseFont: whether the glyph is drawn with the font itself (else PDFium falls
    back to another font, which is not ported)."""
    if glyph < 0:
        return False
    if font.embedded or font.subtype != "TrueType":
        return True
    return glyph != 0 or font.to_unicode is not None


# ---------------------------------------------------------------------- substitutes


def _cdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class _SubstFace:
    """A substituted font's face as CFX_Font draws it: the CFX_SubstFont's skew (the italic angle),
    the multiple master blend at the font's weight and each glyph's /Widths width
    (AdjustVariationParams), synthetic bold (FT_Outline_Embolden), and the shared glyph cache."""

    def __init__(self, face, font, is_cid: bool = False):
        self.face, self.font, self.subst = face, font, font.subst
        self.is_cid = is_cid            # CFX_TextRenderOptions::font_is_cid, for GetEffective*
        self.cache = _subst_cache(font, face)

    def _key(self):
        s = self.subst
        return (s.weight, s.italic_angle, False)                # vertical: refused before

    def bitmap(self, glyph: int, matrix, dest_width: int):
        """CFX_GlyphCache::LoadGlyphBitmap with a substitute's key."""
        if glyph < 0:                   # kInvalidGlyphIndex: no cache entry, no blend
            return None
        key = (_key_part(matrix[0]), _key_part(matrix[1]), _key_part(matrix[2]), _key_part(matrix[3]),
               dest_width) + self._key()
        sizes = self.cache.bitmaps.setdefault(key, {})
        if glyph not in sizes:
            sizes[glyph] = render_glyph(self.face, glyph, matrix, self.subst, dest_width, self.is_cid)
        return sizes[glyph]

    def path(self, glyph: int, dest_width: int):
        """CFX_GlyphCache::LoadGlyphPath -> CFX_Face::LoadGlyphPath."""
        if glyph < 0:                   # kInvalidGlyphIndex, before the cache and the blend
            return None
        key = (glyph, dest_width) + self._key()
        if key not in self.cache.paths:
            s = self.subst
            xy = 0
            skew = s.skew()
            if skew:
                xy = ftoutline.i32(xy - _cdiv(0x10000 * skew, 100))
            if s.flag_mm:
                self.face.adjust_variation(glyph, dest_width, s.weight)
            matrix = (0x10000, xy, 0, 0x10000)
            level = s.embolden_level_for_load()
            if level > 0:
                # LoadGlyphPath never hints: a TrueType face's path outline is its unhinted one
                load = getattr(self.face, "unhinted", self.face.outline)
                outline = load(glyph, matrix)
                path = None if outline is None else \
                    ftoutline._glyph_path(ftoutline.embolden(outline, level))
            else:
                path = self.face.path(glyph, matrix)
            self.cache.paths[key] = path
        return self.cache.paths[key]

    def glyph_width(self, glyph: int) -> int:
        """CFX_Font::GetGlyphWidth(glyph) = GetGlyphWidth(glyph, 0, 0): the unscaled advance.
        Unlike LoadGlyphPath / LoadGlyphBitmap this has no kInvalidGlyphIndex guard: FT_Load_Glyph
        simply fails on it, so the width is 0."""
        key = (glyph, 0, 0)
        if key not in self.cache.widths:
            if self.subst.flag_mm:
                raise ftoutline.Unported("the width of a multiple master glyph")
            self.cache.widths[key] = 0 if glyph < 0 else self.font.program.em_width(glyph)
        return self.cache.widths[key]


def _spacing_heuristic(font, face: _SubstFace) -> bool:
    """CPDF_Font::ShouldApplyGlyphSpacingHeuristic (horizontal, not embedded)."""
    from .fonts import standard_font_index
    if font.embedded or getattr(font, "use_font_width", True):
        return False
    base = font.base_name.lower()
    if standard_font_index(base) is not None or face.subst.flag_mm:
        return False
    family = face.subst.family.replace(" ", "").lower()
    if not family:
        # CFX_SubstFont::IsActualFontLoaded is ByteString::Find, which finds no empty needle: a
        # face with no family - UseInternalSubst's standard Foxit faces, which set none - has not
        # loaded the actual font, so the heuristic applies. Only a fallback font reaches this
        # (a base font on a standard face is refused a line above, by its standard name), and only
        # where the platform has no Arial: the folder scan of Linux, never GDI.
        return True
    return not base.startswith(family)                           # IsActualFontLoaded


def char_pos_list(obj, face=None) -> list:
    """CPDF_Font::GetCharPosList: (glyph, x, y, font_char_width, adjust, fallback position) per
    item. The origin is (x, 0), or in vertical writing (0, y) (`_origin`); a substitute's glyphs are
    drawn at their /Widths width (dest_width) and, for a substitute that is not multiple master,
    moved or narrowed where /Widths disagree with the face (horizontal simple fonts only), the face
    being the one the char is drawn from. A char the font itself must not draw (ShouldUseFont) takes
    the font's fallback face (position 0) and that face's glyph for the char's Unicode."""
    font, size = obj.font, F(obj.font_size)
    subst = not font.embedded and font.subtype != "Type0"
    heuristic = subst and face is not None and not font.vertical and _spacing_heuristic(font, face)
    fb_face, fb_heuristic = None, False
    out = []
    for item in obj.items:
        code = item[0]
        glyph, adjust = glyph_of(font, code), None
        cur, position, cur_heuristic = face, -1, heuristic
        if not _uses_font(font, glyph):
            if fb_face is None:
                fb_face = fallback_face(font)
                fb_heuristic = subst and not font.vertical and _spacing_heuristic(font, fb_face)
            cur, position, cur_heuristic = fb_face, 0, fb_heuristic
            glyph = fallback_glyph(font, code, fb_face)
        x, y = _origin(obj, item)
        dest_width = font.char_width(code) if subst else 0
        if cur_heuristic:
            pdf_w, font_w = font.char_width(code), cur.glyph_width(glyph)
            if font_w and pdf_w > font_w + 1:
                x = F(x + F(F(F(float(pdf_w - font_w)) * size) / 2000.0))
            elif pdf_w and font_w and pdf_w < font_w:
                adjust = (F(F(pdf_w) / F(font_w)), 0.0, 0.0, 1.0)
        out.append((glyph, x, y, dest_width, adjust, position))
    return out


def _runs(chars: list):
    """CPDF_TextRenderer::DrawNormalText / DrawTextPath: the char pos list is cut into runs of
    consecutive chars with the same fallback_font_position_, one device call each."""
    start = 0
    for i in range(1, len(chars)):
        if chars[i][5] != chars[start][5]:
            yield chars[start][5], chars[start:i]
            start = i
    if chars:
        yield chars[start][5], chars[start:]


def _effective(adjust, m):
    """TextCharPos::GetEffectiveMatrix."""
    return R.concat(IDENTITY if adjust is None else (*adjust, 0.0, 0.0), m)


def _glyph_path(face, glyph: int, dest_width: int):
    """CFX_Font::LoadGlyphPath: a substitute's path goes through its skew, blend and embolden."""
    if isinstance(face, _SubstFace):
        return face.path(glyph, dest_width)
    return None if glyph < 0 else face.path(glyph)          # kInvalidGlyphIndex


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
    if obj.font is not None and obj.font.is_type3:     # every mode but 3 fills a Type 3 font
        if mode != MODE_INVISIBLE:
            from . import render_type3
            render_type3.process_type3_text(status, obj, matrix)
        return
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
    if not font.embedded:
        face = _SubstFace(face, font)
    chars = char_pos_list(obj, face if not font.embedded else None)
    if is_stroke:
        device_matrix = matrix
        ctm = obj.text_ctm
        if ctm[0] != 1.0 or ctm[3] != 1.0:
            m = (F(ctm[0]), F(ctm[1]), F(ctm[2]), F(ctm[3]), 0.0, 0.0)
            text_matrix = R.concat(text_matrix, R.inverse(m))
            device_matrix = R.concat(m, matrix)
        graph = (F(obj.line_width), obj.line_cap, obj.line_join, F(obj.miter),
                 tuple(F(v) for v in obj.dash), F(obj.dash_phase))
        for position, run in _runs(chars):
            draw_text_path(status.dev, face if position < 0 else fallback_face(font), run, size,
                           text_matrix, device_matrix, graph, fill_argb, stroke_argb,
                           is_stroke and is_fill)
        return
    text2device = R.concat(text_matrix, matrix)
    for position, run in _runs(chars):
        draw_normal_text(status.dev, face if position < 0 else fallback_face(font), run, size,
                         text2device, fill_argb)


# ---------------------------------------------------------------------- DrawTextPath


_KINDS = {"M": R.PT_MOVE, "L": R.PT_LINE, "C": R.PT_BEZIER}


def draw_text_path(dev, face, chars, size, text2user, user2device, graph, fill_argb: int,
                   stroke_argb: int, stroke: bool) -> None:
    """CFX_RenderDevice::DrawTextPath: each glyph's outline as a path."""
    from .render import FILL_NONE, FILL_WINDING
    if not (fill_argb or stroke_argb):
        return
    fill_type = FILL_WINDING if fill_argb else FILL_NONE
    for glyph, x, y, dest_width, adjust, _position in chars:
        path = _glyph_path(face, glyph, dest_width)
        if path is None:
            continue
        m = _effective(adjust, (size, 0.0, 0.0, size, x, y))       # GetEffectiveMatrix
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
    if not font.embedded:
        face = _SubstFace(face, font)
    for glyph, x, y, dest_width, adjust, position in char_pos_list(obj, face if not font.embedded
                                                                   else None):
        path = _glyph_path(face if position < 0 else fallback_face(font), glyph, dest_width)
        if path is None:
            continue
        m = _effective(adjust, (size, 0.0, 0.0, size, x, y))          # GetEffectiveMatrix
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
    if glyph < 0:                                           # kInvalidGlyphIndex
        return None
    cache = face.__dict__.setdefault("_b2s_glyph_bitmaps", {})
    key = (_key_part(matrix[0]), _key_part(matrix[1]), _key_part(matrix[2]), _key_part(matrix[3]))
    sizes = cache.setdefault(key, {})
    if glyph in sizes:
        return sizes[glyph]
    sizes[glyph] = bm = render_glyph(face, glyph, matrix)
    return bm


def _ft_fixed(v: float) -> int:
    """The float -> FT_Fixed conversion of RenderGlyph's `matrix.a / 64 * 65536`."""
    return int(v)


def render_glyph(face, glyph: int, matrix, subst=None, dest_width: int = 0, is_cid: bool = False):
    """CFX_Face::RenderGlyph in FT_RENDER_MODE_LCD (`subst`: the font's CFX_SubstFont; `is_cid`:
    CFX_TextRenderOptions::font_is_cid, which picks the CJK skew and weight of a CJK substitute)."""
    a, b, c, d = matrix[:4]
    xx, xy, yx, yy = (_ft_fixed(F(F(a / 64.0) * 65536.0)), _ft_fixed(F(F(c / 64.0) * 65536.0)),
                      _ft_fixed(F(F(b / 64.0) * 65536.0)), _ft_fixed(F(F(d / 64.0) * 65536.0)))
    if subst is not None:
        skew = subst.effective_skew(is_cid)
        if skew:
            xy = ftoutline.i32(xy - _cdiv(ftoutline.i32(xx * skew), 100))
        if subst.flag_mm:
            face.adjust_variation(glyph, dest_width, subst.weight)
    outline = face.outline(glyph, (xx, xy, yx, yy))
    if outline is None:
        return None
    if subst is not None:
        level = subst.embolden_level_for_render(is_cid, xx, xy)
        if level < 0:
            return None
        if level > 0:
            outline = ftoutline.embolden(outline, level)
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
    for glyph, x, y, dest_width, adjust, _position in chars:
        matrix = _effective(adjust, char2device)              # GetEffectiveMatrix
        ox, oy = R.transform(text2device, x, y)
        if isinstance(face, _SubstFace):
            bm = face.bitmap(glyph, matrix, dest_width)
        else:
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
    n = end - start
    # Whatever the subpixel shift, an output column averages three *consecutive* glyph columns,
    # and the next output column's three sit 3 further on - so the three taps are strides of the
    # row, and slicing them costs nothing where a fancy index built an array and gathered. A glyph
    # here is 64 pixels at the median, so the call overhead of a numpy op is the whole cost.
    # The C branches `if (x_subpixel == 0) ... else if (x_subpixel == 1) ... else ...`, and the
    # last branch also takes the -1 and -2 that C's % gives a glyph placed left of its origin: the
    # shift is which branch is taken, never the number itself.
    shift = 0 if x_subpixel == 0 else 1 if x_subpixel == 1 else 2
    base = (start - px) * 3 - shift                      # the first column that column 0 averages

    def taps(first: int, cols: int):
        return (g[:, first:first + 3 * cols:3], g[:, first + 1:first + 1 + 3 * cols:3],
                g[:, first + 2:first + 2 + 3 * cols:3])

    if base >= 0:
        a, b, c = taps(base, n)
        v = (a + b + c) // 3
    else:
        # base < 0 is exactly `start == px and x_subpixel != 0`: the taps of column 0 reach past
        # the glyph's left edge, where NormalizeSrc drops them (the C read g8[0] and the fix-up
        # below overwrote the column anyway), so column 0 is written on its own.
        v = np.empty((r1 - r0, n), np.int32)
        a, b, c = taps(base + 3, n - 1)
        v[:, 1:] = (a + b + c) // 3
        v[:, 0] = (g[:, 0] + g[:, 1]) // 3 if x_subpixel == 1 else g[:, 0] // 3
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
