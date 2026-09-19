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

from ..api import OBJ_FORM
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


def process_transparency(status, obj, matrix) -> bool:
    """CPDF_RenderStatus::ProcessTransparency: True when the object was drawn here."""
    from .render import Device, Status
    blend = blend_mode(obj.blend)
    smask = obj.smask
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
    if blend != "Normal":
        raise NotImplementedError("blend modes")       # unported refuses them first
    sub = Device(width, height, True)
    new_matrix = matrix[:4] + (F(matrix[4] + float(-left)), F(matrix[5] + float(-top)))
    bitmap_render = Status(sub, (False, False), transparency[0], 1.0, status.ctx)
    bitmap_render.process_no_clip(obj, new_matrix)
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
    composite(dev, sub.bgra, left, top)
    return True


def multiply_alpha(bgra: np.ndarray, alpha: float) -> None:
    """CFX_DIBitmap::MultiplyAlpha on a BGRA bitmap."""
    if alpha == 1.0:
        return
    ba = int(F(F(alpha) * 255.0))
    bgra[..., 3] = (bgra[..., 3].astype(np.int32) * ba // 255).astype(np.uint8)


# ---------------------------------------------------------------------- compositing


def composite(dev, src: np.ndarray, left: int, top: int) -> None:
    """CFX_RenderDevice::SetDIBits of a BGRA bitmap, Normal blend: the part inside the clip box,
    through the clip mask (CompositeRowBgra2Bgr / Bgra2Bgra / Bgra2Mask)."""
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
    s = src[t - top:b - top, l - left:r - left].astype(np.int32)
    dest = dev.bgra[t - dev.oy:b - dev.oy, l - dev.ox:r - dev.ox]
    sa = s[..., 3]
    if dev.clip is not None and dev.clip.mask is not None:
        m = dev.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
        sa = sa * m // 255
    d = dest.astype(np.int32)
    if getattr(dev, "mask_format", False):
        o = d[..., 0]
        out = np.where(o == 0, sa, np.where(sa > 0, _union(o, sa), o)).astype(np.uint8)
        dest[..., :3] = out[..., None]
        return
    if not dev.alpha:
        k = sa[..., None]
        out = np.where(k == 255, s[..., :3], np.where(k > 0, _merge(d[..., :3], s[..., :3], k), d[..., :3]))
        dest[..., :3] = out.astype(np.uint8)
        return
    da = d[..., 3]
    fresh = da == 0
    mix = ~fresh & (sa > 0)
    union = _union(da, sa)
    ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)
    out = np.where(mix[..., None], _merge(d[..., :3], s[..., :3], ratio[..., None]), d[..., :3])
    out = np.where(fresh[..., None], s[..., :3], out)
    dest[..., :3] = out.astype(np.uint8)
    dest[..., 3] = np.where(fresh, sa, np.where(mix, union, da)).astype(np.uint8)


# ---------------------------------------------------------------------- LoadSMask


def _smask_parts(doc, smask: dict):
    r = doc.resolve
    g = r(smask.get("G"))
    lum = str(r(smask.get("S"))) != "Alpha"
    return (g if isinstance(g, Stream) else None), lum


def background_color(doc, smask: dict, g: Stream) -> int:
    """GetBackgroundColor: /BC in the group's /CS, as 0xAARRGGBB (black when anything is off)."""
    default = 0xFF000000
    r = doc.resolve
    bc = r(smask.get("BC"))
    if not isinstance(bc, list):
        return default
    group = r(g.get("Group"))
    cs_obj = r(group.get("CS")) if isinstance(group, dict) else None
    cs = load_colorspace(doc, cs_obj, None) if cs_obj is not None else None
    if cs is None or cs.family in ("Lab", "Indexed", "Separation", "DeviceN", "Pattern", "ICCBased"):
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
    ctx.depth += 1
    try:
        Status(dev, (False, False), False, 1.0, ctx).render_list(children, matrix)
    finally:
        ctx.depth -= 1
    px = dev.bgra.astype(np.int32)
    if lum:
        out = (px[..., 0] * 11 + px[..., 1] * 59 + px[..., 2] * 30) // 100
    else:
        out = px[..., 0]
    return out.astype(np.uint8)


# ---------------------------------------------------------------------- what is refused


def unsupported(obj, ctx, check) -> str | None:
    """What `obj`'s transparency needs that is not ported, if anything; `check(objects, ctx)` is
    render.unported, run over a soft mask's contents."""
    if blend_mode(obj.blend) != "Normal":
        return "blend modes"
    if obj.transfer is not None:
        return "transfer functions"
    if obj.smask is None:
        return None
    if ctx is None:
        return "soft masks"
    doc = ctx.doc
    g, lum = _smask_parts(doc, obj.smask)
    if g is None:
        return None
    r = doc.resolve
    tr = r(obj.smask.get("TR"))
    if isinstance(tr, (dict, Stream)):
        return "soft mask transfer functions"
    if lum and isinstance(r(obj.smask.get("BC")), list):
        group = r(g.get("Group"))
        cs_obj = r(group.get("CS")) if isinstance(group, dict) else None
        cs = load_colorspace(doc, cs_obj, None) if cs_obj is not None else None
        if cs is not None and cs.family not in ("DeviceGray", "DeviceRGB", "DeviceCMYK"):
            return "soft mask backdrop colour spaces"
    if ctx.depth >= MAX_MASK_DEPTH:
        return "soft masks nested this deep"
    _, every = ctx.form(g)
    ctx.depth += 1
    try:
        return check(every, ctx)
    finally:
        ctx.depth -= 1
