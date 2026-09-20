"""PDFium's transparency, ported for `render.py`: CPDF_RenderStatus::ProcessTransparency (soft
masks, transparency groups, a form's constant alpha, blend modes), LoadSMask, and the bitmap
compositing they end in (CFX_DIBitmap::CompositeBitmap through CFX_ScanlineCompositor's row
functions), value for value.

An object that needs any of it is drawn on its own into a zeroed BGRA bitmap the size of its
device box (clipped), then that bitmap's alpha is multiplied by the soft mask, by the group's
constant alpha and by the alpha the form inherited, and the bitmap is composited onto the device
through the device's clip. The soft mask is its /G form drawn into a BGR bitmap cleared to the
backdrop colour (luminosity: FXRGB2GRAY of each pixel) or into an 8-bit mask (alpha).

`unsupported` says what this module does not draw yet, so that `render.unported` refuses it."""

from __future__ import annotations

import numpy as np

from ..api import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT
from . import raster as R
from .colors import load_colorspace
from .raster import F
from .syntax import Stream

SEPARABLE = ("Multiply", "Screen", "Overlay", "Darken", "Lighten", "ColorDodge", "ColorBurn",
             "HardLight", "SoftLight", "Difference", "Exclusion")
NON_SEPARABLE = ("Hue", "Saturation", "Color", "Luminosity")
MAX_MASK_DEPTH = 8


def blend_mode(name: str) -> str:
    """CPDF_GeneralState's GetBlendTypeFromString: an unknown name is Normal."""
    return name if name in SEPARABLE or name in NON_SEPARABLE else "Normal"


def _merge(back, src, alpha):
    """AlphaMerge on int arrays."""
    return (back * (255 - alpha) + src * alpha) // 255


def _union(dest, src):
    """AlphaUnion (uint8 result)."""
    return (dest + src - dest * src // 255) & 0xFF


# ---------------------------------------------------------------------- context


class Context:
    """What a render status reaches through CPDF_RenderContext: the document, the page's
    resources (a soft mask's /G is a CPDF_Form over them) and the page's transparency."""

    def __init__(self, doc, page_resources, fonts: dict, page_group: bool):
        self.doc = doc
        self.page_resources = page_resources if isinstance(page_resources, dict) else {}
        self.fonts = fonts
        self.page_group = page_group
        self._forms: dict = {}
        self.depth = 0
        # what a soft mask's own render status carries (meaningful while depth > 0): SetLoadMask's
        # luminosity, whether SetGroupFamily got kDeviceCMYK, and which objects that status draws
        # itself (a form's children go through ProcessForm, which starts a status with none of it)
        self.mask_lum = True
        self.mask_group_cmyk = False
        self.mask_top: frozenset = frozenset()

    def form(self, stream: Stream):
        """CPDF_Form(doc, page resources, G).ParseContent() with no parent states: (top-level
        objects, every object) of G's content."""
        key = id(stream)
        if key not in self._forms:
            from .content import Parser, State, _Run
            objs: list = []
            parser = Parser(self.doc, self.page_resources, objs, self.fonts, {})
            run = _Run(parser, self.page_resources, State(), (0.0, 0.0, 0.0, 0.0), None)
            try:
                run._form(stream, "")
            except RecursionError:
                pass
            holder = objs[0] if objs else None
            children = list(holder.children) if holder is not None else []
            self._forms[key] = (children, [o for o in objs if o is not holder], stream)
        children, every, _ = self._forms[key]
        return children, every


def form_transparency(obj, ctx) -> tuple[bool, bool]:
    """CPDF_Form::GetTransparency (LoadTransparencyInfo): (group, isolated)."""
    stream = obj.stream
    r = ctx.doc.resolve if ctx is not None else (lambda v: v)
    group = r(stream.get("Group")) if isinstance(stream, Stream) else None
    if not isinstance(group, dict) or str(r(group.get("S"))) != "Transparency":
        return False, False
    iso = r(group.get("I"))       # GetIntegerFor: a boolean or a number, anything else 0
    if isinstance(iso, bool):
        return True, iso
    return True, isinstance(iso, (int, float)) and int(iso) != 0


# ---------------------------------------------------------------------- 8-bit mask device


def mask_device(width: int, height: int):
    """CFX_DefaultRenderDevice over a k8bppMask bitmap (the AGG driver): spans are
    CompositeSpanGray with gray 255, so the colour's alpha is all that counts; a clipped FillRect
    is CompositeMask (ByteMask2Mask), and DrawFillStrokePath puts its sub-bitmap back with colour
    0, which CompositeMask ignores: a translucent-stroke fill-and-stroke draws nothing."""
    from .render import Device

    class MaskDevice(Device):
        def _blend(self, dest, src, color, span):
            Device._blend(self, dest, src, color | 0xFFFFFF, span)

        def draw_fill_stroke(self, *args, **kw):
            return

        def fill_rect(self, rect, color):
            if self.clip is None or self.clip.mask is None:
                Device.fill_rect(self, rect, color | 0xFFFFFF)
                return
            cb = self.clip_box()
            draw = R.rect_intersect(cb, _norm(rect))
            alpha = color >> 24
            if R.rect_empty(draw) or alpha == 0:
                return
            l, t, r, b = draw
            dest = self.bgra[t - self.oy:b - self.oy, l - self.ox:r - self.ox]
            m = self.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
            src = alpha * m // 255
            d = dest[..., 0].astype(np.int32)
            out = np.where(d == 0, src, np.where(src > 0, _union(d, src), d)).astype(np.uint8)
            dest[..., :3] = out[..., None]

    dev = MaskDevice(width, height, False)
    dev.mask_format = True
    return dev


def _norm(r):
    l, t, rt, b = r
    return min(l, rt), min(t, b), max(l, rt), max(t, b)


# ---------------------------------------------------------------------- ProcessTransparency


def effective_smask(obj):
    """The graphics state's soft mask as ProcessTransparency reads it: an image with an /SMask of
    its own drops it (`pSMaskDict = nullptr`), so that mask is never rendered at all."""
    smask = obj.smask
    if smask is not None and obj.type == OBJ_IMAGE:
        d = getattr(getattr(obj, "stream", None), "dict", None)
        if isinstance(d, dict) and "SMask" in d:
            return None
    return smask


def transparency_status(obj) -> bool:
    """Whether ProcessTransparency draws `obj` into a bitmap of its own, whose CPDF_RenderStatus
    has SetStdCS(true) and neither the load-mask flag nor a group family. Asked about images, whose
    fill alpha and group flags never reach that test (they are a form object's)."""
    return effective_smask(obj) is not None or blend_mode(obj.blend) != "Normal"


def alpha_mode(ctx) -> bool:
    """CPDF_RenderOptions::ColorModeIs(kAlpha): set by LoadSMask for an alpha mask and inherited by
    every status under it (ProcessForm and ProcessTransparency both copy the options), until a
    luminosity mask inside it renders with kNormal again."""
    return ctx is not None and ctx.depth > 0 and not ctx.mask_lum


def process_transparency(status, obj, matrix) -> bool:
    """CPDF_RenderStatus::ProcessTransparency: True when the object was drawn here."""
    from .render import Device, Status
    blend = blend_mode(obj.blend)
    smask = effective_smask(obj)
    group_alpha = 1.0
    initial_alpha = 1.0
    transparency = status.transparency
    group_transparent = False
    if obj.type == OBJ_FORM:
        group_alpha = F(obj.fill_alpha)
        transparency = form_transparency(obj, status.ctx)
        group_transparent = transparency[1]
        initial_alpha = F(status.initial_alpha)
    if smask is None and group_alpha == 1.0 and blend == "Normal" and not group_transparent \
            and initial_alpha == 1.0:
        return False
    dev = status.dev
    rect = R.rect_intersect(R.outer_rect(R.transform_rect(matrix, obj.rect)), dev.clip_box())
    if R.rect_empty(rect):
        return True
    left, top = rect[0], rect[1]
    width, height = rect[2] - left, rect[3] - top
    sub = Device(width, height, True)
    if not transparency[1]:
        # CreateForNewBitmapWithBackdrop: what the device holds under the box (GetDIBits)
        sub.group_backdrop = (get_dibits(dev, rect), kind(dev))
    new_matrix = matrix[:4] + (F(matrix[4] + float(-left)), F(matrix[5] + float(-top)))
    bitmap_render = Status(sub, (False, False), transparency[0], 1.0, status.ctx, status.stop)
    bitmap_render.process_no_clip(obj, new_matrix)
    status.stopped = bitmap_render.stopped
    if smask is not None:
        smask_matrix = R.concat(obj.smask_matrix, matrix)
        mask = load_smask(status, smask, rect, smask_matrix)
        if mask is not None:
            a = sub.bgra[..., 3].astype(np.int32)
            sub.bgra[..., 3] = (a * mask.astype(np.int32) // 255).astype(np.uint8)
    if transparency[0]:
        multiply_alpha(sub.bgra, group_alpha)
    if initial_alpha != 1.0 and not status.in_group:
        multiply_alpha(sub.bgra, initial_alpha)
    group = status.transparency[0] or obj.type == OBJ_FORM
    composite_dibitmap(status, obj, sub.bgra, left, top, blend, (group, status.transparency[1]))
    return True


def multiply_alpha(bgra: np.ndarray, alpha: float) -> None:
    """CFX_DIBitmap::MultiplyAlpha on a BGRA bitmap."""
    if alpha == 1.0:
        return
    ba = int(F(F(alpha) * 255.0))
    bgra[..., 3] = (bgra[..., 3].astype(np.int32) * ba // 255).astype(np.uint8)


# ---------------------------------------------------------------------- compositing


# Bitmaps are (h, w, 4) uint8 arrays of one of three formats: "bgra", "bgrx" (the fourth byte
# unused) and "mask" (k8bppMask, the value in bytes 0-2).


def kind(dev) -> str:
    """The format of a device's bitmap."""
    if getattr(dev, "mask_format", False):
        return "mask"
    return "bgra" if dev.alpha else "bgrx"


def composite_dibitmap(status, obj, bitmap: np.ndarray, left: int, top: int, blend: str,
                       transparency) -> None:
    """CPDF_RenderStatus::CompositeDIBitmap of a BGRA bitmap (`transparency`: the status's
    (group, isolated), a form making it a group)."""
    dev = status.dev
    if blend == "Normal":
        set_dibits(dev, bitmap, "bgra", left, top, "Normal")
        return
    group, isolated = transparency
    dkind = kind(dev)
    # bBackAlphaRequired = isolated; bGetBackGround = AlphaOutput || (GetBits && !required)
    if dkind == "bgra" or not isolated:
        if isolated or not group:
            set_dibits(dev, bitmap, "bgra", left, top, blend)
            return
        h, w = bitmap.shape[:2]
        rect = R.rect_intersect((left, top, left + w, top + h), dev.clip_box())
        backdrop = getattr(dev, "group_backdrop", None)
        if backdrop is None:
            set_dibits(dev, bitmap, "bgra", rect[0], rect[1], blend)
            return
        barr, bkind = backdrop
        cr = R.rect_intersect(rect, (0, 0, barr.shape[1], barr.shape[0]))
        if R.rect_empty(cr):
            return                                       # ClipTo gives nothing
        clone = barr[cr[1]:cr[3], cr[0]:cr[2]].copy()
        composite_bitmap(clone, bkind, 0, 0, dev.bgra, dkind, rect[0], rect[1], "Normal")
        composite_bitmap(clone, bkind, 0, 0, bitmap, "bgra", min(left, 0), min(top, 0), blend)
        set_dibits(dev, clone, bkind, rect[0], rect[1], "Normal")
        return
    # GetBackdrop: the page drawn again up to this object, on a clear BGRA bitmap
    h, w = bitmap.shape[:2]
    bbox = R.rect_intersect((left, top, left + w, top + h), dev.clip_box())
    backdrop = render_backdrop(status, obj, bbox)
    if backdrop is None:
        return
    composite_bitmap(backdrop, "bgra", left - bbox[0], top - bbox[1], bitmap, "bgra", 0, 0, blend,
                     width=w, height=h)
    white = np.full(backdrop.shape, 255, np.uint8)
    composite_bitmap(white, "bgrx", 0, 0, backdrop, "bgra", 0, 0, "Normal")
    set_dibits(dev, white, "bgrx", bbox[0], bbox[1], "Normal")


def render_backdrop(status, obj, bbox):
    """GetBackdrop with a backdrop needing alpha on a device without alpha output: a BGRA bitmap
    over `bbox` holding the page drawn up to `obj` (CPDF_RenderContext::Render, stop object)."""
    from .render import Device, Status
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width <= 0 or height <= 0:
        return None
    ctx = status.ctx
    dev = Device(width, height, True)
    matrix = R.concat(ctx.page_matrix, (1.0, 0.0, 0.0, 1.0, float(-bbox[0]), float(-bbox[1])))
    dev.save()
    page = Status(dev, (bool(ctx.page_group), True), ctx=ctx, stop=obj)
    page.render_list(ctx.page_objects, matrix)
    dev.restore(False)
    return dev.bgra


def get_dibits(dev, rect) -> np.ndarray:
    """CFX_AggDeviceDriver::GetDIBits into a new bitmap of the device's format over `rect`: over
    a group backdrop, the device's bitmap composited onto the backdrop's piece (from the bitmap's
    origin, as PDFium does), else the device's pixels; a format change keeps the new bitmap's
    fourth byte (zero)."""
    l, t, r, b = rect
    dkind = kind(dev)
    out = np.zeros((b - t, r - l, 4), np.uint8)
    backdrop = getattr(dev, "group_backdrop", None)
    if backdrop is not None:
        barr, pkind = backdrop
        cr = R.rect_intersect(rect, (0, 0, barr.shape[1], barr.shape[0]))
        if R.rect_empty(cr):
            return out
        piece = barr[cr[1]:cr[3], cr[0]:cr[2]].copy()
        composite_bitmap(piece, pkind, 0, 0, dev.bgra, dkind, 0, 0, "Normal")
    else:
        wh, ww = dev.bgra.shape[:2]
        cr = R.rect_intersect(rect, (dev.ox, dev.oy, dev.ox + ww, dev.oy + wh))
        if R.rect_empty(cr):
            return out
        piece = dev.bgra[cr[1] - dev.oy:cr[3] - dev.oy, cr[0] - dev.ox:cr[2] - dev.ox]
        pkind = dkind
    ph, pw = piece.shape[:2]
    tgt = out[:ph, :pw]
    if pkind == dkind:
        tgt[...] = piece
    elif pkind == "mask":
        tgt[..., :3] = piece[..., :1]
    elif dkind == "mask":
        p = piece.astype(np.int32)
        tgt[..., :3] = ((p[..., 2] * 30 + p[..., 1] * 59 + p[..., 0] * 11) // 100)[..., None]
    else:
        tgt[..., :3] = piece[..., :3]
    return out


def set_dibits(dev, src: np.ndarray, skind: str, left: int, top: int, blend: str) -> None:
    """CFX_RenderDevice::SetDIBitsWithBlend (the AGG driver's SetDIBits): the part inside the
    clip box, composited through the clip mask."""
    h, w = src.shape[:2]
    cb = dev.clip_box()
    dr = R.rect_intersect((left, top, left + w, top + h), cb)
    if R.rect_empty(dr):
        return
    wh, ww = dev.bgra.shape[:2]
    dr = R.rect_intersect(dr, (dev.ox, dev.oy, dev.ox + ww, dev.oy + wh))
    if R.rect_empty(dr):
        return
    l, t, r, b = dr
    s = src[t - top:b - top, l - left:r - left]
    dest = dev.bgra[t - dev.oy:b - dev.oy, l - dev.ox:r - dev.ox]
    clip = None
    if dev.clip is not None and dev.clip.mask is not None:
        clip = dev.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int64)
    blit(dest, kind(dev), s, skind, blend, clip)


def composite_bitmap(dest: np.ndarray, dkind: str, dest_left: int, dest_top: int, src: np.ndarray,
                     skind: str, src_left: int, src_top: int, blend: str, width=None,
                     height=None) -> None:
    """CFX_DIBitmap::CompositeBitmap with no clip (GetOverlapRect, then the row compositor);
    `width`, `height` default to the destination's."""
    dh, dw = dest.shape[:2]
    sh, sw = src.shape[:2]
    width = dw if width is None else width
    height = dh if height is None else height
    if width == 0 or height == 0 or dest_left > dw or dest_top > dh:
        return
    sr = R.rect_intersect((src_left, src_top, src_left + width, src_top + height), (0, 0, sw, sh))
    xo, yo = dest_left - src_left, dest_top - src_top
    dr = R.rect_intersect((sr[0] + xo, sr[1] + yo, sr[2] + xo, sr[3] + yo), (0, 0, dw, dh))
    if R.rect_empty(dr):
        return
    l, t, r, b = dr
    blit(dest[t:b, l:r], dkind, src[t - yo:b - yo, l - xo:r - xo], skind, blend, None)


def blit(d: np.ndarray, dkind: str, s: np.ndarray, skind: str, blend: str, clip) -> None:
    """CFX_ScanlineCompositor's rows over aligned pixels `d` (written) and `s`; `clip` the clip
    mask's values or None."""
    D = d.astype(np.int64)
    S = s.astype(np.int64)
    if skind == "mask":
        # a mask-format bitmap through SetDIBits is CompositeMask with colour 0: only a BGRA
        # destination changes (CompositeRow_ByteMask2Bgra: a clear pixel takes the colour)
        if dkind == "bgra":
            d[D[..., 3] == 0] = 0
        return
    if skind == "bgra":
        sa = S[..., 3] if clip is None else S[..., 3] * clip // 255
        if dkind == "mask":                              # CompositeRowBgra2Mask (no blend)
            o = D[..., 0]
            out = np.where(o == 0, sa, np.where(sa > 0, _union(o, sa), o))
            d[..., :3] = out.astype(np.uint8)[..., None]
            return
        drgb, srgb = D[..., :3], S[..., :3]
        if dkind == "bgrx":                              # CompositeRowBgra2Bgr
            k = sa[..., None]
            if blend == "Normal":
                out = np.where(k == 255, srgb, np.where(k > 0, _merge(drgb, srgb, k), drgb))
            else:
                out = np.where(k > 0, _mergec(drgb, blended(blend, drgb, srgb), k), drgb)
            d[..., :3] = (out & 0xFF).astype(np.uint8)
            return
        da = D[..., 3]                                   # CompositeRowBgra2Bgra
        fresh = da == 0
        mix = ~fresh & (sa > 0)
        union = _union(da, sa)
        ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)[..., None]
        if blend != "Normal":
            srgb = _mergec(srgb, blended(blend, drgb, srgb), da[..., None])
        out = np.where(mix[..., None], _mergec(drgb, srgb, ratio), drgb)
        out = np.where(fresh[..., None], S[..., :3], out)
        d[..., :3] = (out & 0xFF).astype(np.uint8)
        d[..., 3] = np.where(fresh, sa, np.where(mix, union, da)).astype(np.uint8)
        return
    # a BGRx source
    if dkind == "mask":                                  # CompositeRow_Rgb2Mask
        d[..., :3] = 255 if clip is None else _union(D[..., 0], clip).astype(np.uint8)[..., None]
        return
    drgb, srgb = D[..., :3], S[..., :3]
    if dkind == "bgrx":                                  # CompositeRow_Rgb2Rgb_*
        if clip is None:
            if blend == "Normal":
                d[...] = s
                return
            out = blended(blend, drgb, srgb)
        else:
            k = clip[..., None]
            if blend == "Normal":
                out = np.where(k == 255, srgb, np.where(k > 0, _merge(drgb, srgb, k), drgb))
            else:
                out = np.where(k > 0, _mergec(drgb, blended(blend, drgb, srgb), k), drgb)
        d[..., :3] = (out & 0xFF).astype(np.uint8)
        return
    da = D[..., 3]                                       # CompositeRow_Bgr2Bgra_*
    if clip is None:
        if blend == "Normal":
            d[..., :3] = s[..., :3]
        else:
            out = np.where((da == 0)[..., None], srgb,
                           _mergec(srgb, blended(blend, drgb, srgb), da[..., None]))
            d[..., :3] = (out & 0xFF).astype(np.uint8)
        d[..., 3] = 255
        return
    sa = clip
    if blend == "Normal":
        full, mix = sa == 255, (sa > 0) & (sa < 255)
        union = _union(da, sa)
        ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)[..., None]
        out = np.where(full[..., None], srgb, np.where(mix[..., None], _merge(drgb, srgb, ratio), drgb))
        d[..., :3] = out.astype(np.uint8)
        d[..., 3] = np.where(full, 255, np.where(mix, union, da)).astype(np.uint8)
        return
    empty = da == 0
    mix = ~empty & (sa > 0)
    union = _union(da, sa)
    ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)[..., None]
    mixed = _mergec(drgb, _mergec(srgb, blended(blend, drgb, srgb), da[..., None]), ratio)
    out = np.where(empty[..., None], srgb, np.where(mix[..., None], mixed, drgb))
    d[..., :3] = (out & 0xFF).astype(np.uint8)
    d[..., 3] = np.where(mix, union, da).astype(np.uint8)


# ---------------------------------------------------------------------- blend formulas

_COLOR_SQRT = np.array([
    0x00, 0x03, 0x07, 0x0B, 0x0F, 0x12, 0x16, 0x19, 0x1D, 0x20, 0x23, 0x26,
    0x29, 0x2C, 0x2F, 0x32, 0x35, 0x37, 0x3A, 0x3C, 0x3F, 0x41, 0x43, 0x46,
    0x48, 0x4A, 0x4C, 0x4E, 0x50, 0x52, 0x54, 0x56, 0x57, 0x59, 0x5B, 0x5C,
    0x5E, 0x60, 0x61, 0x63, 0x64, 0x65, 0x67, 0x68, 0x69, 0x6B, 0x6C, 0x6D,
    0x6E, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A,
    0x7B, 0x7C, 0x7D, 0x7E, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x91,
    0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C,
    0x9C, 0x9D, 0x9E, 0x9F, 0xA0, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA4, 0xA5,
    0xA6, 0xA7, 0xA7, 0xA8, 0xA9, 0xAA, 0xAA, 0xAB, 0xAC, 0xAD, 0xAD, 0xAE,
    0xAF, 0xB0, 0xB0, 0xB1, 0xB2, 0xB3, 0xB3, 0xB4, 0xB5, 0xB5, 0xB6, 0xB7,
    0xB7, 0xB8, 0xB9, 0xBA, 0xBA, 0xBB, 0xBC, 0xBC, 0xBD, 0xBE, 0xBE, 0xBF,
    0xC0, 0xC0, 0xC1, 0xC2, 0xC2, 0xC3, 0xC4, 0xC4, 0xC5, 0xC6, 0xC6, 0xC7,
    0xC7, 0xC8, 0xC9, 0xC9, 0xCA, 0xCB, 0xCB, 0xCC, 0xCC, 0xCD, 0xCE, 0xCE,
    0xCF, 0xD0, 0xD0, 0xD1, 0xD1, 0xD2, 0xD3, 0xD3, 0xD4, 0xD4, 0xD5, 0xD6,
    0xD6, 0xD7, 0xD7, 0xD8, 0xD9, 0xD9, 0xDA, 0xDA, 0xDB, 0xDC, 0xDC, 0xDD,
    0xDD, 0xDE, 0xDE, 0xDF, 0xE0, 0xE0, 0xE1, 0xE1, 0xE2, 0xE2, 0xE3, 0xE4,
    0xE4, 0xE5, 0xE5, 0xE6, 0xE6, 0xE7, 0xE7, 0xE8, 0xE9, 0xE9, 0xEA, 0xEA,
    0xEB, 0xEB, 0xEC, 0xEC, 0xED, 0xED, 0xEE, 0xEE, 0xEF, 0xF0, 0xF0, 0xF1,
    0xF1, 0xF2, 0xF2, 0xF3, 0xF3, 0xF4, 0xF4, 0xF5, 0xF5, 0xF6, 0xF6, 0xF7,
    0xF7, 0xF8, 0xF8, 0xF9, 0xF9, 0xFA, 0xFA, 0xFB, 0xFB, 0xFC, 0xFC, 0xFD,
    0xFD, 0xFE, 0xFE, 0xFF], np.int64)                  # blend.cpp's kColorSqrt


def _cdiv(a, b):
    """C's integer division (truncating toward zero); a zero divisor gives 0 (never used)."""
    a = np.asarray(a, np.int64)
    b = np.asarray(b, np.int64)
    safe = np.where(b == 0, 1, b)
    q = np.abs(a) // np.abs(safe)
    return np.where((a < 0) != (safe < 0), -q, q)


def _mergec(back, src, alpha):
    """AlphaMerge on ints that may leave 0..255 (a blend result)."""
    return _cdiv(back * (255 - alpha) + src * alpha, 255)


def blend_channel(mode: str, b, s):
    """fxge::Blend(mode, back, src) on int arrays, a separable mode."""
    if mode == "Multiply":
        return s * b // 255
    if mode == "Screen":
        return s + b - s * b // 255
    if mode == "Overlay":
        return blend_channel("HardLight", s, b)
    if mode == "Darken":
        return np.minimum(s, b)
    if mode == "Lighten":
        return np.maximum(s, b)
    if mode == "ColorDodge":
        return np.where(s == 255, 255, np.minimum(b * 255 // np.maximum(255 - s, 1), 255))
    if mode == "ColorBurn":
        return np.where(s == 0, 0, 255 - np.minimum((255 - b) * 255 // np.maximum(s, 1), 255))
    if mode == "HardLight":
        s2 = 2 * s - 255
        return np.where(s < 128, s * b * 2 // 255, s2 + b - _cdiv(s2 * b, 255))
    if mode == "SoftLight":
        low = b - _cdiv(_cdiv((255 - 2 * s) * b * (255 - b), 255), 255)
        high = b + _cdiv((2 * s - 255) * (_COLOR_SQRT[b] - b), 255)
        return np.where(s < 128, low, high)
    if mode == "Difference":
        return np.abs(b - s)
    if mode == "Exclusion":
        return b + s - 2 * b * s // 255
    return s


def _lum(r, g, b):
    return _cdiv(r * 30 + g * 59 + b * 11, 100)


def _clip_color(r, g, b):
    """ClipColor (the second test reads the first one's max, as PDFium does)."""
    lum = _lum(r, g, b)
    n = np.minimum(r, np.minimum(g, b))
    x = np.maximum(r, np.maximum(g, b))
    low = n < 0
    r, g, b = (np.where(low, lum + _cdiv((c - lum) * lum, lum - n), c) for c in (r, g, b))
    high = x > 255
    return tuple(np.where(high, lum + _cdiv((c - lum) * (255 - lum), x - lum), c) for c in (r, g, b))


def _set_lum(r, g, b, lum):
    d = lum - _lum(r, g, b)
    return _clip_color(r + d, g + d, b + d)


def _sat(r, g, b):
    return np.maximum(r, np.maximum(g, b)) - np.minimum(r, np.minimum(g, b))


def _set_sat(r, g, b, s):
    lo = np.minimum(r, np.minimum(g, b))
    hi = np.maximum(r, np.maximum(g, b))
    flat = lo == hi
    span = np.where(flat, 1, hi - lo)
    return tuple(np.where(flat, 0, (c - lo) * s // span) for c in (r, g, b))


def blended(mode: str, back, src):
    """The blended colour of BGR int arrays: Blend per channel, or RgbBlend (non-separable)."""
    if mode not in NON_SEPARABLE:
        return blend_channel(mode, back, src)
    sr, sg, sb = src[..., 2], src[..., 1], src[..., 0]
    br, bg, bb = back[..., 2], back[..., 1], back[..., 0]
    if mode == "Hue":
        r, g, b = _set_lum(*_set_sat(sr, sg, sb, _sat(br, bg, bb)), _lum(br, bg, bb))
    elif mode == "Saturation":
        r, g, b = _set_lum(*_set_sat(br, bg, bb, _sat(sr, sg, sb)), _lum(br, bg, bb))
    elif mode == "Color":
        r, g, b = _set_lum(sr, sg, sb, _lum(br, bg, bb))
    else:                                                # Luminosity
        r, g, b = _set_lum(br, bg, bb, _lum(sr, sg, sb))
    return np.stack([b, g, r], axis=-1)


# ---------------------------------------------------------------------- LoadSMask


def _smask_parts(doc, smask: dict):
    r = doc.resolve
    g = r(smask.get("G"))
    lum = str(r(smask.get("S"))) != "Alpha"
    return (g if isinstance(g, Stream) else None), lum


def _srgb_backdrop(doc, smask: dict, cs) -> bool:
    """A backdrop in an sRGB ICCBased space is DeviceRGB without the clamp (GetBackgroundColor
    takes it because IsNormal() is true for such a profile, and GetRGB hands the components back):
    exact while each one lands in a byte, refused when one does not."""
    if not getattr(cs, "srgb", False):
        return False
    bc = doc.resolve(smask.get("BC"))
    vals = [doc.resolve(v) for v in bc[:3]] if isinstance(bc, list) else []
    vals = [v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0 for v in vals]
    return all(0.0 <= float(v) <= 1.0 for v in vals)


def group_cs(doc, smask: dict, g: Stream):
    """The colour space GetBackgroundColor reads /BC in, or None where it takes the default colour
    and leaves *pCSFamily at kUnknown (no /BC, a space that doesn't load, Lab, a special one or an
    ICC profile that is not sRGB). That family is what TransMask() later asks about."""
    r = doc.resolve
    if not isinstance(r(smask.get("BC")), list):
        return None
    group = r(g.get("Group"))
    cs_obj = r(group.get("CS")) if isinstance(group, dict) else None
    cs = load_colorspace(doc, cs_obj, None) if cs_obj is not None else None
    if cs is None or cs.family in ("Lab", "Indexed", "Separation", "DeviceN", "Pattern"):
        return None
    if cs.family == "ICCBased" and not cs.srgb:   # kICCBased && !IsNormal()
        return None
    return cs


def background_color(doc, smask: dict, g: Stream) -> int:
    """GetBackgroundColor: /BC in the group's /CS, as 0xAARRGGBB (black when anything is off)."""
    default = 0xFF000000
    r = doc.resolve
    bc = r(smask.get("BC"))
    cs = group_cs(doc, smask, g)
    if cs is None:
        return default
    vals = []
    for v in bc[:8]:
        v = r(v)
        vals.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0)
    vals += [0.0] * (max(8, cs.n) - len(vals))
    rgb = cs.rgb([F(v) for v in vals])
    if rgb is None:
        rgb = (0.0, 0.0, 0.0)
    cr, cg, cb = (int(F(F(v) * 255.0)) for v in rgb)
    return 0xFF000000 | (cr << 16) | (cg << 8) | cb


def _enter_mask(ctx, doc, smask: dict, g: Stream, lum: bool):
    """The mask's own CPDF_RenderStatus: SetLoadMask(bLuminosity), SetGroupFamily(nCSFamily) and
    SetStdCS(true). Not sticky - a luminosity mask inside an alpha one is a kNormal status again,
    and only the objects this status draws itself (the group's top-level ones) carry its flags."""
    keep = (ctx.mask_lum, ctx.mask_group_cmyk, ctx.mask_top)
    ctx.depth += 1
    ctx.mask_lum = lum
    cs = group_cs(doc, smask, g) if lum else None
    ctx.mask_group_cmyk = lum and cs is not None and cs.family == "DeviceCMYK"
    children, _ = ctx.form(g)
    ctx.mask_top = frozenset(id(o) for o in children)
    return keep


def _leave_mask(ctx, keep) -> None:
    ctx.depth -= 1
    ctx.mask_lum, ctx.mask_group_cmyk, ctx.mask_top = keep


def load_smask(status, smask: dict, rect, smask_matrix):
    """CPDF_RenderStatus::LoadSMask: the 8-bit mask over `rect`, or None."""
    from .render import Device, Status
    ctx = status.ctx
    doc = ctx.doc
    g, lum = _smask_parts(doc, smask)
    if g is None:
        return None
    matrix = smask_matrix[:4] + (F(smask_matrix[4] + float(-rect[0])), F(smask_matrix[5] + float(-rect[1])))
    children, _ = ctx.form(g)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if lum:
        dev = Device(width, height, False)
        bg = background_color(doc, smask, g)
        dev.bgra[..., 0], dev.bgra[..., 1], dev.bgra[..., 2] = bg & 0xFF, (bg >> 8) & 0xFF, (bg >> 16) & 0xFF
        dev.bgra[..., 3] = 255
    else:
        dev = mask_device(width, height)
    keep = _enter_mask(ctx, doc, smask, g, lum)
    try:
        Status(dev, (False, False), False, 1.0, ctx).render_list(children, matrix)
    finally:
        _leave_mask(ctx, keep)
    px = dev.bgra.astype(np.int32)
    if lum:
        out = (px[..., 0] * 11 + px[..., 1] * 59 + px[..., 2] * 30) // 100
    else:
        out = px[..., 0]
    from .transfer import smask_table
    table = smask_table(doc, doc.resolve(smask.get("TR")))
    if table is not None:
        out = np.asarray(table, dtype=np.uint8)[out]
    return out.astype(np.uint8)


# ---------------------------------------------------------------------- what is refused


def unsupported(obj, ctx, check) -> str | None:
    """What `obj`'s transparency needs that is not ported, if anything; `check(objects, ctx)` is
    render.unported, run over a soft mask's contents."""
    from . import transfer
    from .render_shading import Unsupported
    if obj.transfer is not None:
        if ctx is None:
            return "transfer functions"
        try:
            t = transfer.of(ctx.doc, obj.transfer)
        except Unsupported as e:
            return str(e)
        if t is not None and not t.identity and obj.type not in (OBJ_PATH, OBJ_TEXT, OBJ_FORM, OBJ_SHADING):
            return "transfer functions on images"
        if (t is not None and not t.identity and obj.type == OBJ_TEXT
                and getattr(getattr(obj, "font", None), "is_type3", False)):
            return "transfer functions on Type 3 text"     # the glyphs' own colours: not checked
    smask = effective_smask(obj)
    if smask is None:
        return None
    if ctx is None:
        return "soft masks"
    doc = ctx.doc
    g, lum = _smask_parts(doc, smask)
    if g is None:
        return None
    r = doc.resolve
    try:
        transfer.smask_table(doc, r(smask.get("TR")))
    except Unsupported as e:
        return str(e)
    if lum and isinstance(r(smask.get("BC")), list):
        group = r(g.get("Group"))
        cs_obj = r(group.get("CS")) if isinstance(group, dict) else None
        cs = load_colorspace(doc, cs_obj, None) if cs_obj is not None else None
        if (cs is not None and cs.family not in ("DeviceGray", "DeviceRGB", "DeviceCMYK")
                and not _srgb_backdrop(doc, smask, cs)):
            return "soft mask backdrop colour spaces"
    if ctx.depth >= MAX_MASK_DEPTH:
        return "soft masks nested this deep"
    _, every = ctx.form(g)
    keep = _enter_mask(ctx, doc, smask, g, lum)
    try:
        return check(every, ctx)
    finally:
        _leave_mask(ctx, keep)
