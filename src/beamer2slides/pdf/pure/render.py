"""PDFium's page renderer, ported for what a beamer page draws: CPDF_ProgressiveRenderer and
CPDF_RenderStatus walking the page objects over CFX_RenderDevice and CFX_AggDeviceDriver (the
AGG back end, a BGRx or BGRA bitmap), value for value.

`raster.py` makes the coverage PDFium's rasteriser makes; this module decides what gets
rasterised with which colour under which clip, and composites it into the bitmap with the same
integer arithmetic (AlphaMerge, AlphaUnion, the clip mask's gray8 blend), so that the pixels come
out equal to PDFium's, not merely close. Each function names the PDFium code it stands for.

Drawn: paths (fill, stroke, dashes, constant alpha, fill-and-stroke with a translucent stroke
through DrawFillStrokePath's knockout sub-bitmap), clip paths, forms, and transparency
(ProcessTransparency: soft masks, transparency groups, group alpha, blend modes;
`render_transparency.py`), shadings of every type and shading patterns (`render_shading.py`,
`render_mesh.py`), coloured tiling patterns (`render_pattern.py`), transfer functions on colours
and soft masks (`transfer.py`), text (`render_text.py`), images at any angle with their own masks
(`render_image.py`, decoded by `decode_image.py`).
Not yet: uncoloured tiling patterns and tiling patterns inside a form, transfer functions on
images; a page holding any of them raises PdfError (`unported`) rather than coming back drawn
differently."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Protocol, TypeAlias

import numpy as np

from ...arrays import BGRA, Gray, Int32
from ..api import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT, Matrix, PdfError
from . import raster as R
from .raster import F, FloatRect, IntRect, PathPoint, Point

if TYPE_CHECKING:
    from .content import PObj
    from .render_transparency import BitmapKind, Context
    from .transfer import Transfer

FILL_NONE, FILL_EVENODD, FILL_WINDING = 0, 1, 2
PT_MOVE, PT_LINE, PT_BEZIER = R.PT_MOVE, R.PT_LINE, R.PT_BEZIER

Graph: TypeAlias = tuple[float, int, int, float, tuple[float, ...], float]
"""CFX_GraphStateData: line width, cap, join, miter limit, dash array, dash phase."""
DEFAULT_GRAPH: Graph = (1.0, 0, 0, 10.0, (), 0.0)   # CFX_GraphStateData()
ZERO_GRAPH: Graph = (0.0, 0, 0, 10.0, (), 0.0)
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _normalize(r: IntRect) -> IntRect:
    """FX_RECT::Normalize."""
    l, t, rt, b = r
    return min(l, rt), min(t, b), max(l, rt), max(t, b)


def _float_intersect(a: FloatRect, b: FloatRect) -> FloatRect:
    """CFX_FloatRect::Intersect of (left, bottom, right, top)."""
    l, bt, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if l > r or bt > t:
        return 0.0, 0.0, 0.0, 0.0
    return l, bt, r, t


def _merge(back: Int32, src: Int32 | int, alpha: Int32 | int) -> Int32:
    """AlphaMerge on int arrays."""
    return (back * (255 - alpha) + src * alpha) // 255


# ---------------------------------------------------------------------- clip region


class Clip:
    """CFX_AggClipRgn: a box and, inside it, an optional 8-bit mask (never written in place, so
    copies share it)."""

    __slots__ = ("box", "mask")

    def __init__(self, box: IntRect, mask: Gray | None) -> None:
        self.box = box
        self.mask = mask

    def copy(self) -> Clip:
        return Clip(self.box, self.mask)

    def intersect_rect(self, rect: IntRect) -> None:
        if self.mask is None:
            self.box = R.rect_intersect(self.box, _normalize(rect))
        else:
            self._mask_and_rect(rect, self.box, self.mask)

    def intersect_mask(self, left: int, top: int, mask: Gray) -> None:
        mbox = (left, top, left + mask.shape[1], top + mask.shape[0])
        if self.mask is None:
            self._mask_and_rect(self.box, mbox, mask)
            return
        nb = R.rect_intersect(self.box, mbox)
        if R.rect_empty(nb):
            self.mask, self.box = None, nb
            return
        bl, bt = self.box[0], self.box[1]
        old = self.mask[nb[1] - bt:nb[3] - bt, nb[0] - bl:nb[2] - bl].astype(np.int32)
        new = mask[nb[1] - top:nb[3] - top, nb[0] - left:nb[2] - left].astype(np.int32)
        self.box, self.mask = nb, (old * new // 255).astype(np.uint8)

    def _mask_and_rect(self, rect: IntRect, mrect: IntRect, mask: Gray) -> None:
        box = R.rect_intersect(_normalize(rect), _normalize(mrect))
        self.box = box
        if R.rect_empty(box):
            self.mask = None
            return
        if box == mrect:
            self.mask = mask
            return
        self.mask = mask[box[1] - mrect[1]:box[3] - mrect[1], box[0] - mrect[0]:box[2] - mrect[0]]


# ---------------------------------------------------------------------- device


class Device:
    """CFX_RenderDevice over CFX_AggDeviceDriver: `bgra` is the bitmap (B, G, R, A/x bytes)."""

    def __init__(self, width: int, height: int, alpha: bool, window: IntRect | None) -> None:
        """`window` (x, y, w, h): only that part of the bitmap is kept (the rest can never reach
        the page); pixels are addressed in the whole bitmap's coordinates all the same."""
        self.w, self.h, self.alpha = width, height, alpha
        self.ox, self.oy, ww, wh = window if window is not None else (0, 0, width, height)
        self.bgra: BGRA = np.zeros((wh, ww, 4), np.uint8)
        self.backdrop: BGRA | None = None      # a knockout device's backdrop
        self.clip: Clip | None = None
        self.stack: list[Clip | None] = []
        # a soft mask's device (render_transparency.mask_device): what may not be drawn into it
        self.mask_format = False
        # a non-isolated group's device: the pixels under it and their kind (GetBackdrop)
        self.group_backdrop: tuple[BGRA, BitmapKind] | None = None

    # ---- state
    def clip_box(self) -> IntRect:
        return self.clip.box if self.clip is not None else (0, 0, self.w, self.h)

    def save(self) -> None:
        self.stack.append(self.clip.copy() if self.clip is not None else None)

    def restore(self, keep: bool) -> None:
        self.clip = None
        if not self.stack:
            return
        if keep:
            top = self.stack[-1]
            self.clip = top.copy() if top is not None else None
        else:
            self.clip = self.stack.pop()

    # ---- clipping
    def set_clip_rect(self, rect: IntRect) -> None:
        """SetClip_Rect (an FX_RECT: left, top, right, bottom)."""
        l, t, r, b = rect
        self.set_clip_fill(R.rect_path(float(l), float(b), float(r), float(t)), None, False)

    def set_clip_fill(self, points: Sequence[PathPoint], matrix: Matrix | None, even_odd: bool) -> None:
        """CFX_AggDeviceDriver::SetClip_PathFill."""
        clip = self.clip
        if clip is None:
            clip = self.clip = Clip((0, 0, self.w, self.h), None)
        rect = R.path_get_rect(points, matrix)
        if rect is not None:
            rect = _float_intersect(rect, (0.0, 0.0, float(self.w), float(self.h)))
            clip.intersect_rect(R.outer_rect(rect))
            return
        path = R.build_path(points, matrix)
        R.end_poly(path)
        rz = R.Rasterizer(self.w, self.h)
        rz.add_path(path)
        _set_clip_mask(clip, rz, even_odd)

    # ---- CFX_RenderDevice::DrawPath
    def draw_path(self, points: Sequence[PathPoint], matrix: Matrix | None, graph: Graph | None, fill_argb: int,
                  stroke_argb: int, fill_type: int, stroke: bool, text_mode: bool, full_cover: bool,
                  rect_aa: bool) -> None:
        fill = fill_type != FILL_NONE
        fill_alpha = fill_argb >> 24 if fill else 0
        stroke_alpha = stroke_argb >> 24 if graph is not None else 0
        if stroke_alpha == 0 and len(points) == 2:
            p1: Point = points[0][:2]
            p2: Point = points[1][:2]
            if matrix is not None:
                p1, p2 = R.transform(matrix, *p1), R.transform(matrix, *p2)
            # DrawCosmeticLine: the AGG driver has none, so a one-unit stroke
            line: list[PathPoint] = [(p1[0], p1[1], PT_MOVE, False), (p2[0], p2[1], PT_LINE, False)]
            self.driver_draw_path(line, None, DEFAULT_GRAPH, 0, fill_argb, fill_type, False, False)
            return
        if stroke_alpha == 0 and not rect_aa:
            rf = R.path_get_rect(points, matrix)
            if rf is not None:
                self.fill_rect(_adjusted_rect(rf), fill_argb)
                return
        if fill and stroke_alpha == 0 and not stroke and not text_mode:
            sub: list[PathPoint] = []
            i, n = 0, len(points)
            while i < n:
                kind = points[i][2]
                if kind == PT_MOVE:
                    self._zero_area(sub, matrix, fill_argb, fill_alpha)
                    sub = [points[i]]
                elif kind == PT_BEZIER:
                    sub.extend(points[i:i + 3])
                    i += 2
                else:
                    sub.append(points[i])
                i += 1
            self._zero_area(sub, matrix, fill_argb, fill_alpha)
        if fill and fill_alpha and stroke_alpha < 0xFF and stroke:
            self.draw_fill_stroke(points, matrix, graph, fill_argb, stroke_argb, fill_type)
            return
        self.driver_draw_path(points, matrix, graph, fill_argb, stroke_argb, fill_type, False, full_cover)

    def draw_fill_stroke(self, points: Sequence[PathPoint], matrix: Matrix | None, graph: Graph | None,
                         fill_argb: int, stroke_argb: int, fill_type: int) -> None:
        """DrawFillStrokePath: fill and stroke drawn into a copy of the pixels under the path
        (GetDIBits; zeros on a BGRA device) with the stroke knocking out the fill, then put back
        through the clip (SetDIBits)."""
        bbox = stroke_bbox(points, graph[0] if graph is not None else 0.0)
        if matrix is not None:
            bbox = R.transform_rect(matrix, bbox)
        if not all(np.isfinite(bbox)):
            return
        rect = R.outer_rect(bbox)
        rw, rh = rect[2] - rect[0], rect[3] - rect[1]
        if rw <= 0 or rh <= 0 or rw > 0x7FFFFFFF or rh > 0x7FFFFFFF or rw * 4 * rh >= 1 << 32:
            return                                 # CreateCompatibleBitmap fails
        vis = R.rect_intersect(self.clip_box(), rect)
        if R.rect_empty(vis):
            return                                 # nothing reaches the device (GetOverlapRect)
        l, t, r, b = vis
        sub = Device(rw, rh, self.alpha, (l - rect[0], t - rect[1], r - l, b - t))
        if not self.alpha:
            sub.bgra = self.bgra[t:b, l:r].copy()
        sub.backdrop = sub.bgra.copy()
        m = matrix if matrix is not None else IDENTITY
        m = m[:4] + (F(m[4] + float(-rect[0])), F(m[5] + float(-rect[1])))
        sub.driver_draw_path(points, m, graph, fill_argb, stroke_argb, fill_type, False, False)
        # CompositeBitmap, Normal: CompositeRow_Rgb2Rgb_NoBlend_(No)Clip / CompositeRowBgra2Bgra
        dest, src = self.bgra[t:b, l:r], sub.bgra
        cb = self.clip_box()
        mask: Int32 | None = None
        if self.clip is not None and self.clip.mask is not None:
            mask = self.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
        s = src.astype(np.int32)
        d = dest.astype(np.int32)
        if not self.alpha:
            if mask is None:
                dest[...] = src
                return
            k = mask[..., None]
            out = np.where(k == 255, s[..., :3], np.where(k > 0, _merge(d[..., :3], s[..., :3], k),
                                                          d[..., :3]))
            dest[..., :3] = out.astype(np.uint8)
            return
        sa = s[..., 3] if mask is None else s[..., 3] * mask // 255
        da = d[..., 3]
        fresh = da == 0
        mix = ~fresh & (sa > 0)
        union = (da + sa - da * sa // 255) & 0xFF
        ratio = np.where(mix, sa * 255 // np.maximum(union, 1), 0)
        out = np.where(mix[..., None], _merge(d[..., :3], s[..., :3], ratio[..., None]), d[..., :3])
        out = np.where(fresh[..., None], s[..., :3], out)
        dest[..., :3] = out.astype(np.uint8)
        dest[..., 3] = np.where(fresh, sa, np.where(mix, union, da)).astype(np.uint8)

    def _zero_area(self, sub: Sequence[PathPoint], matrix: Matrix | None, fill_argb: int, fill_alpha: int) -> None:
        """DrawZeroAreaPath."""
        if not sub:
            return
        found = zero_area_path(sub, matrix, True)
        if found is None:
            return
        new_path, thin, set_identity = found
        color = fill_argb
        if thin:
            color = ((fill_alpha >> 2) << 24) | (color & 0xFFFFFF)
        m = matrix if matrix is not None and not R.is_identity(matrix) and not set_identity else None
        self.driver_draw_path(new_path, m, ZERO_GRAPH, 0, color, FILL_NONE, True, False)

    # ---- CFX_AggDeviceDriver::DrawPath
    def driver_draw_path(self, points: Sequence[PathPoint], matrix: Matrix | None, graph: Graph | None,
                         fill_argb: int, stroke_argb: int, fill_type: int, zero_area: bool,
                         full_cover: bool) -> None:
        if fill_type != FILL_NONE and fill_argb:
            rz = R.Rasterizer(self.w, self.h)
            rz.add_path(R.build_path(points, matrix))
            self._render(rz.coverage(fill_type != FILL_WINDING, False), fill_argb, None, full_cover)
        if graph is None or not stroke_argb >> 24:
            return
        width, cap, join, miter, dash, phase = graph
        if zero_area:
            path = R.build_path(points, matrix)
            verts = R.stroke_vertices(path, None, width, cap, join, miter, dash, phase, 1.0)
        else:
            m1: Matrix = IDENTITY
            m2 = m1
            if matrix is not None:
                a = max(abs(matrix[0]), abs(matrix[1]))
                m2 = (F(matrix[0] / a), F(matrix[1] / a), F(matrix[2] / a), F(matrix[3] / a), 0.0, 0.0)
                m1 = R.concat(matrix, R.inverse(m2))
            path = R.build_path(points, m1)
            verts = R.stroke_vertices(path, m2, width, cap, join, miter, dash, phase, m1[0])
        rz = R.Rasterizer(self.w, self.h)
        rz.add_path(verts)
        self._render(rz.coverage(False, False), stroke_argb, self.backdrop, False)

    def _render(self, cov: R.Coverage | None, color: int, knockout: BGRA | None, full_cover: bool) -> None:
        """RenderRasterizer: CFX_AggRenderer's CompositeSpanRGB / CompositeSpanARGB, or its
        CompositeSpan over the backdrop `knockout` for a knockout group. `full_cover` (mesh
        patches): every covered pixel takes the colour's whole alpha."""
        if cov is None:
            return
        x0, y0, a = cov
        box = self.clip_box()
        wh, ww = self.bgra.shape[:2]
        win = R.rect_intersect(box, (self.ox, self.oy, self.ox + ww, self.oy + wh))
        found = _window(a, x0, y0, win)
        if found is None:
            return
        sub, l, t = found
        h, w = sub.shape
        alpha = color >> 24
        cover = sub.astype(np.int32)
        mask = self.clip.mask if self.clip is not None else None
        m: Int32 | None = None
        if mask is not None:
            m = mask[t - box[1]:t - box[1] + h, l - box[0]:l - box[0] + w].astype(np.int32)
        dl, dt = l - self.ox, t - self.oy
        dest = self.bgra[dt:dt + h, dl:dl + w]
        if knockout is not None:
            self._knockout(dest, knockout[dt:dt + h, dl:dl + w], cover,
                           alpha if m is None else alpha * m // 255, color)
            return
        if full_cover:
            src = np.where(cover > 0, alpha * m // 255 if m is not None else alpha, 0)
        else:
            src = alpha * cover * m // 255 // 255 if m is not None else alpha * cover // 255
        self._blend(dest, src, color, True)

    def _knockout(self, dest: BGRA, backdrop: BGRA, cover: Int32, src_alpha: Int32 | int, color: int) -> None:
        """CFX_AggRenderer::CompositeSpan (group knockout; only covered pixels are spans)."""
        c = np.array([color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF], np.int32)
        d = dest[..., :3].astype(np.int32)
        on = cover > 0
        if not self.alpha:
            sa = np.broadcast_to(np.asarray(src_alpha), cover.shape)[..., None]
            merged = _merge(backdrop[..., :3].astype(np.int32), c, sa)
            out = _merge(d, merged, cover[..., None])
            dest[..., :3] = np.where(on[..., None], out, d).astype(np.uint8)
            return
        sa = np.broadcast_to(np.asarray(src_alpha), cover.shape)
        covered = sa * cover // 255
        da = dest[..., 3].astype(np.int32)
        live = covered > 0
        fresh = live & ((cover == 255) | (da == 0))
        mix = live & ~fresh
        out = np.where(mix[..., None], _merge(d, c, cover[..., None]), d)
        out = np.where(fresh[..., None], c, out)
        dest[..., :3] = out.astype(np.uint8)
        dest[..., 3] = np.where(fresh, covered, np.where(mix, _merge(da, sa, cover), da)).astype(np.uint8)

    def _blend(self, dest: BGRA, src: Int32, color: int, span: bool) -> None:
        """Normal-mode compositing of `color` at per-pixel alpha `src` (CompositeSpanRGB/ARGB when
        `span`, CompositeRow_ByteMask2Rgb/Bgra otherwise: they differ only on transparent pixels)."""
        c = np.array([color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF], np.int32)
        d = dest[..., :3].astype(np.int32)
        if not self.alpha:
            on = src > 0
            if not on.any():
                return
            s = src[..., None]
            merged = _merge(d, c, s)
            dest[..., :3] = np.where(on[..., None], merged, d).astype(np.uint8)
            dest[..., 3] = np.where(src == 255, 255, dest[..., 3])
            return
        da = dest[..., 3].astype(np.int32)
        empty = da == 0
        if span:
            fresh = (src > 0) & (empty | (src == 255))
        else:
            fresh = empty
        mix = (src > 0) & ~fresh
        union = (da + src - da * src // 255) & 0xFF
        ratio = np.where(mix, src * 255 // np.maximum(union, 1), 0)
        out = np.where(mix[..., None], _merge(d, c, ratio[..., None]), d)
        out = np.where(fresh[..., None], c, out)
        dest[..., :3] = out.astype(np.uint8)
        dest[..., 3] = np.where(fresh, src, np.where(mix, union, da)).astype(np.uint8)

    # ---- FillRect
    def fill_rect(self, rect: IntRect, color: int) -> None:
        """CFX_AggDeviceDriver::FillRect: CompositeMask under a clip mask, else CompositeRect."""
        cb = self.clip_box()
        draw = R.rect_intersect(cb, _normalize(rect))
        if R.rect_empty(draw):
            return
        alpha = color >> 24
        if alpha == 0:
            return
        l, t, r, b = draw
        dest = self.bgra[t:b, l:r]
        if self.clip is not None and self.clip.mask is not None:
            m = self.clip.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int32)
            self._blend(dest, alpha * m // 255, color, span=False)
            return
        c = np.array([color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF], np.int32)
        if alpha == 255:
            dest[..., :3] = c
            dest[..., 3] = 255
            return
        src = np.full(dest.shape[:2], alpha, np.int32)
        if self.alpha:
            self._blend(dest, src, color, span=False)
        else:
            dest[..., :3] = _merge(dest[..., :3].astype(np.int32), c, alpha).astype(np.uint8)
            dest[..., 3] = 255


def _set_clip_mask(clip: Clip, rz: R.Rasterizer, even_odd: bool) -> None:
    """SetClip_PathFill's mask: the rasterised path intersected into `clip`."""
    pr = R.rect_intersect(_normalize((rz.min_x, rz.min_y, rz.max_x + 1, rz.max_y + 1)), clip.box)
    if R.rect_empty(pr):
        mask = np.zeros((0, 0), np.uint8)
    else:
        mask = np.zeros((pr[3] - pr[1], pr[2] - pr[0]), np.uint8)
        cov = rz.coverage(even_odd, False)
        if cov is not None:
            x0, y0, a = cov
            found = _window(a, x0, y0, pr)
            if found is not None:
                sub, l, t = found
                alpha = (255 * (sub.astype(np.int32) + 1)) >> 8
                val = np.where(alpha == 255, 255, (255 * alpha) >> 8)
                mask[t - pr[1]:t - pr[1] + sub.shape[0], l - pr[0]:l - pr[0] + sub.shape[1]] = val
    clip.intersect_mask(pr[0], pr[1], mask)


def _window(a: Gray, x0: int, y0: int, box: IntRect) -> tuple[Gray, int, int] | None:
    """The part of coverage `a` (placed at x0, y0) inside FX_RECT `box`: (array, left, top), or
    None when nothing of it is."""
    h, w = a.shape
    l, t = max(x0, box[0]), max(y0, box[1])
    r, b = min(x0 + w, box[2]), min(y0 + h, box[3])
    if r <= l or b <= t:
        return None
    return a[t - y0:b - y0, l - x0:r - x0], l, t


def _adjusted_rect(rf: FloatRect) -> IntRect:
    """DrawPath's pixel-snapped rectangle for a fill-only rectangle path."""
    l, t, r, b = R.outer_rect(rf)
    fl, fb, fr, ft = rf                       # float: left, min y, right, max y
    width = int(np.ceil(F(fr - fl)))
    if width < 1:
        width = 1
        if l == r:
            r += 1
    height = int(np.ceil(F(ft - fb)))
    if height < 1:
        height = 1
        if b == t:
            b += 1
    if r - l >= width + 1:
        if F(fl - float(l)) > F(float(r) - fr):
            l += 1
        else:
            r -= 1
    if b - t >= height + 1:
        if F(ft - float(t)) > F(float(b) - fb):
            t += 1
        else:
            b -= 1
    return l, t, r, b


# ---------------------------------------------------------------------- stroke bounds


def _hyp(x: float, y: float) -> float:
    return F(np.hypot(np.float32(x), np.float32(y)))


def _end_points(rect: list[float], start: Point, end: Point, hw: float) -> None:
    """UpdateLineEndPoints (cfx_path.cpp), in float."""
    if start[0] == end[0]:
        if start[1] == end[1]:
            _update(rect, F(end[0] + hw), F(end[1] + hw))
            _update(rect, F(end[0] - hw), F(end[1] - hw))
            return
        y = F(end[1] - hw) if end[1] < start[1] else F(end[1] + hw)
        _update(rect, F(end[0] + hw), y)
        _update(rect, F(end[0] - hw), y)
        return
    if start[1] == end[1]:
        x = F(end[0] - hw) if end[0] < start[0] else F(end[0] + hw)
        _update(rect, x, F(end[1] + hw))
        _update(rect, x, F(end[1] - hw))
        return
    dx, dy = F(end[0] - start[0]), F(end[1] - start[1])
    ll = _hyp(dx, dy)
    mx, my = F(end[0] + F(F(hw * dx) / ll)), F(end[1] + F(F(hw * dy) / ll))
    dx1, dy1 = F(F(hw * dy) / ll), F(F(hw * dx) / ll)
    _update(rect, F(mx - dx1), F(my + dy1))
    _update(rect, F(mx + dx1), F(my - dy1))


def _join_points(rect: list[float], start: Point, mid: Point, end: Point, hw: float) -> None:
    """UpdateLineJoinPoints (cfx_path.cpp), in float; the miter limit goes unused there."""
    tw = F(1.0 / 20)
    start_vert = abs(F(start[0] - mid[0])) < tw
    end_vert = abs(F(mid[0] - end[0])) < tw
    if start_vert and end_vert:
        y = F(mid[1] + (hw if mid[1] > start[1] else -hw))
        _update(rect, F(mid[0] + hw), y)
        _update(rect, F(mid[0] - hw), y)
        return
    start_k = start_c = end_k = end_c = start_dc = end_dc = 0.0
    if not start_vert:
        sx, sy = F(start[0] - mid[0]), F(start[1] - mid[1])
        start_k = F(F(mid[1] - start[1]) / F(mid[0] - start[0]))
        start_c = F(mid[1] - F(start_k * mid[0]))
        start_dc = abs(F(F(hw * _hyp(sx, sy)) / sx))
    if not end_vert:
        ex, ey = F(end[0] - mid[0]), F(end[1] - mid[1])
        end_k = F(ey / ex)
        end_c = F(mid[1] - F(end_k * mid[0]))
        end_dc = abs(F(F(hw * _hyp(ex, ey)) / ex))
    if start_vert:
        ox = F(start[0] + (hw if end[0] < start[0] else -hw))
        base = F(F(end_k * ox) + end_c)
        oy = F(base + end_dc) if start[1] < F(F(end_k * start[0]) + end_c) else F(base - end_dc)
        _update(rect, ox, oy)
        return
    if end_vert:
        ox = F(end[0] + (hw if start[0] < end[0] else -hw))
        base = F(F(start_k * ox) + start_c)
        oy = F(base + start_dc) if end[1] < F(F(start_k * end[0]) + start_c) else F(base - start_dc)
        _update(rect, ox, oy)
        return
    if abs(F(start_k - end_k)) < tw:
        sd = 1 if mid[0] > start[0] else -1
        ed = 1 if end[0] > mid[0] else -1
        if sd == ed:
            _end_points(rect, mid, end, hw)
        else:
            _end_points(rect, start, mid, hw)
        return
    so = F(start_c + start_dc) if end[1] < F(F(start_k * end[0]) + start_c) else F(start_c - start_dc)
    eo = F(end_c + end_dc) if start[1] < F(F(end_k * start[0]) + end_c) else F(end_c - end_dc)
    jx = F(F(eo - so) / F(start_k - end_k))
    _update(rect, jx, F(F(start_k * jx) + so))


def _update(rect: list[float], x: float, y: float) -> None:
    """CFX_FloatRect::UpdateRect (std::min / std::max: a NaN never replaces)."""
    if x < rect[0]:
        rect[0] = x
    if y < rect[1]:
        rect[1] = y
    if rect[2] < x:
        rect[2] = x
    if rect[3] < y:
        rect[3] = y


def stroke_bbox(points: Sequence[PathPoint], line_width: float) -> FloatRect:
    """CFX_Path::GetBoundingBoxForStrokePath (half_width is the whole line width there), in
    float: (left, bottom, right, top)."""
    rect = [100000.0, 100000.0, -100000.0, -100000.0]
    hw = F(line_width)
    n = len(points)
    i = start = end = mid = 0
    join = False
    with np.errstate(all="ignore"):
        while i < n:
            kind = points[i][2]
            if kind == PT_MOVE:
                if i + 1 == n:
                    if points[i][3]:
                        _update(rect, points[i][0], points[i][1])
                    break
                start, end, join = i + 1, i, False
            else:
                if kind == PT_BEZIER and not points[i][3]:
                    _update(rect, points[i][0], points[i][1])
                    _update(rect, points[i + 1][0], points[i + 1][1])
                    i += 2
                if i + 1 == n or points[i + 1][2] == PT_MOVE:
                    start, end, join = i - 1, i, False
                else:
                    start, mid, end, join = i - 1, i, i + 1, True
            if join:
                _join_points(rect, _pt(points[start]), _pt(points[mid]), _pt(points[end]), hw)
            else:
                _end_points(rect, _pt(points[start]), _pt(points[end]), hw)
            i += 1
    return rect[0], rect[1], rect[2], rect[3]


# ---------------------------------------------------------------------- zero-area paths


def _pt(p: PathPoint) -> Point:
    return p[0], p[1]


def zero_area_path(points: Sequence[PathPoint], matrix: Matrix | None,
                   adjust: bool) -> tuple[list[PathPoint], bool, bool] | None:
    """GetZeroAreaPath: (new points, thin, set_identity) or None."""
    if len(points) < 2:
        return None
    # CheckSimpleLinePath
    n = len(points)
    out: list[PathPoint] = []
    if n in (2, 3) and points[0][2] == PT_MOVE and points[1][2] == PT_LINE and \
            (n == 2 or (points[2][2] == PT_LINE and _pt(points[0]) == _pt(points[2]))):
        if _pt(points[0]) == _pt(points[1]):
            return [], False, False
        for p in points[:2]:
            x, y = p[0], p[1]
            if adjust:
                if matrix is not None:
                    x, y = R.transform(matrix, x, y)
                x, y = F(int(x) + 0.5), F(int(y) + 0.5)
            out.append((x, y, p[2], False))
        return out, True, adjust and matrix is not None
    # CheckPalindromicPath
    if n > 3 and n % 2:
        mid = n // 2
        temp: list[PathPoint] = []
        ok = True
        for i in range(mid):
            left, right = points[mid - i - 1], points[mid + i + 1]
            if not (_pt(left) == _pt(right) and left[2] != PT_BEZIER and right[2] != PT_BEZIER):
                ok = False
                break
            temp.append((points[mid - i][0], points[mid - i][1], PT_MOVE, False))
            temp.append((left[0], left[1], PT_LINE, False))
        if ok:
            return temp, True, False
    i = 0
    while i < n:
        kind = points[i][2]
        if kind == PT_MOVE:
            i += 1
            continue
        if kind == PT_BEZIER:
            i += 3
            continue
        nxt = points[(i + 1) % n]
        if nxt[2] != PT_LINE:
            i += 1
            continue
        prev, cur = points[i - 1], points[i]
        (ax, ay), (bx, by), (cx, cy) = _pt(prev), _pt(cur), _pt(nxt)
        if ax == bx == cx and F(F(by - ay) * F(by - cy)) > 0:
            use_prev = abs(F(by - ay)) < abs(F(by - cy))
            s, e = (prev, cur) if use_prev else (cur, nxt)
            out.append((s[0], s[1], PT_MOVE, False))
            out.append((e[0], e[1], PT_LINE, False))
        elif (ay == by == cy and F(F(bx - ax) * F(bx - cx)) > 0) or \
                (ax != bx and cx != bx and ay != by and cy != by and
                 F(F(ay - by) * F(cx - bx)) == F(F(cy - by) * F(ax - bx))):
            use_prev = abs(F(bx - ax)) < abs(F(bx - cx))
            s, e = (prev, cur) if use_prev else (cur, nxt)
            out.append((s[0], s[1], PT_MOVE, False))
            out.append((e[0], e[1], PT_LINE, False))
        i += 1
    if not out:
        return None
    return out, n > 3, False


# ---------------------------------------------------------------------- render status


def _argb(ref: int | None, alpha: float, transfer: Transfer | None) -> int:
    """GetFillArgb / GetStrokeArgb: alpha * 255 truncated, with the colour reference run through
    the transfer function (transfer.Transfer) if there is one."""
    if ref is None:
        ref = 0
    ref &= 0xFFFFFF
    if transfer is not None:
        ref = transfer.translate(ref)
    return (int(F(F(alpha) * 255.0)) << 24) | ref


def _available(m: Matrix) -> bool:
    """IsAvailableMatrix."""
    a, b, c, d = m[:4]
    if a == 0 or d == 0:
        return b != 0 and c != 0
    if b == 0 or c == 0:
        return a != 0 and d != 0
    return True


class Type3Glyph(Protocol):
    """What a status reads of the Type 3 char it draws (render_type3.Char)."""

    @property
    def colored(self) -> bool: ...


ClipPaths: TypeAlias = Sequence[tuple[Sequence[PathPoint], int]]
"""CPDF_ClipPath's paths: (points, fill type) each, in the container's space."""
ClipTexts: TypeAlias = "Sequence[PObj | None]"
"""CPDF_ClipPath's texts: text objects, a None ending each group."""


class Status:
    """CPDF_RenderStatus."""

    def __init__(self, device: Device, transparency: tuple[bool, bool], in_group: bool,
                 initial_alpha: float, ctx: Context, stop: PObj | None) -> None:
        """`transparency` (group, isolated), `in_group` and `initial_alpha` (the fill alpha of
        the form object whose contents this status draws) are what ProcessTransparency reads;
        `ctx` is render_transparency.Context; `stop` the stop object (GetBackdrop's re-render
        draws the page up to the object being blended)."""
        self.dev = device
        self.last_clip: ClipPaths = ()
        self.last_texts: ClipTexts = ()
        self.transparency, self.in_group = transparency, in_group
        self.initial_alpha, self.ctx = initial_alpha, ctx
        self.stop, self.stopped = stop, False
        # Type 3 glyph drawing (render_type3): the char being drawn, the text's fill colour,
        # the fonts being drawn (ProcessType3Text's recursion guard), rect AA (options), and
        # m_InitialStates' colours, which an object whose colour was never set falls back to.
        self.type3_char: Type3Glyph | None = None
        self.t3_fill, self.rect_aa = 0, False
        self.type3_fonts: tuple[int, ...] = ()
        self.initial_fill = self.initial_stroke = 0

    def fill_argb(self, obj: PObj) -> int:
        """GetFillArgb (with a Type 3 char: its fill unless the glyph is coloured)."""
        if self.type3_char is not None and (not self.type3_char.colored or obj.fill is None):
            return self.t3_fill
        return _argb(self.initial_fill if obj.fill is None else obj.fill, obj.fill_alpha,
                     self.transfer(obj))

    def stroke_argb(self, obj: PObj) -> int:
        """GetStrokeArgb."""
        if self.type3_char is not None and (not self.type3_char.colored or obj.stroke is None):
            return self.t3_fill
        return _argb(self.initial_stroke if obj.stroke is None else obj.stroke,
                     obj.stroke_alpha, self.transfer(obj))

    def transfer(self, obj: PObj) -> Transfer | None:
        """The object's transfer function (transfer.Transfer) or None."""
        if obj.transfer is None:
            return None
        from . import transfer
        return transfer.of(self.ctx.doc, obj.transfer)

    def render_list(self, objs: Iterable[PObj], matrix: Matrix) -> None:
        """RenderObjectList."""
        l, t, r, b = self.dev.clip_box()
        cr = R.transform_rect(R.inverse(matrix), (float(l), float(t), float(r), float(b)))
        for obj in objs:
            if obj is self.stop:
                self.stopped = True
                return
            if not obj.active:
                continue
            rl, rb, rr, rt = obj.rect
            if rl > cr[2] or rr < cr[0] or rb > cr[3] or rt < cr[1]:
                continue
            self.render_single(obj, matrix)
            if self.stopped:
                return

    def render_single(self, obj: PObj, matrix: Matrix) -> None:
        self.process_clip(obj.clip_paths, matrix, obj.clip_texts)
        if obj.smask is not None or obj.blend != "Normal" or obj.type == OBJ_FORM:
            from .render_transparency import process_transparency
            if process_transparency(self, obj, matrix):
                return
        self.process_no_clip(obj, matrix)

    def process_clip(self, clip_paths: ClipPaths, matrix: Matrix, clip_texts: ClipTexts) -> None:
        """ProcessClipPath. The AGG device has soft clips (RenderCapSoftClip), so a clip's texts
        count: each group's glyph outlines, in device space, are one winding clip."""
        dev = self.dev
        if not clip_paths and not clip_texts:
            if self.last_clip or self.last_texts:
                dev.restore(True)
                self.last_clip, self.last_texts = (), ()
            return
        if clip_paths is self.last_clip and clip_texts is self.last_texts:
            return
        self.last_clip, self.last_texts = clip_paths, clip_texts
        dev.restore(True)
        for points, fill_type in clip_paths:
            if not points:
                dev.set_clip_fill(R.rect_path(-1.0, -1.0, 0.0, 0.0), None, False)
            else:
                dev.set_clip_fill(points, matrix, fill_type != FILL_WINDING)
        if not clip_texts:
            return
        from .render_text import clip_text_path
        path: list[PathPoint] | None = None
        for text in clip_texts:
            if text is not None:
                if path is None:
                    path = []
                clip_text_path(text, matrix, path)
                continue
            if path is None:
                continue
            dev.set_clip_fill(path, None, False)
            path = None

    def process_no_clip(self, obj: PObj, matrix: Matrix) -> None:
        if obj.type == OBJ_PATH:
            self.process_path(obj, matrix)
        elif obj.type == OBJ_FORM:
            self.process_form(obj, matrix)
        elif obj.type == OBJ_SHADING:
            from . import render_shading
            render_shading.process_shading(self, obj, matrix)
        elif obj.type == OBJ_TEXT:
            from .render_text import process_text
            process_text(self, obj, matrix)
        elif obj.type == OBJ_IMAGE:
            from . import render_image
            render_image.draw(self, obj, matrix)

    def process_path(self, obj: PObj, matrix: Matrix) -> None:
        from . import render_shading
        fill_type, stroke = render_shading.path_pattern(self, obj, matrix)
        if fill_type == FILL_NONE and not stroke:
            return
        fill_argb = self.fill_argb(obj) if fill_type != FILL_NONE else 0
        stroke_argb = self.stroke_argb(obj) if stroke else 0
        pm = R.concat(obj.matrix, matrix)
        if not _available(pm):
            return
        graph: Graph = (F(obj.line_width), obj.line_cap, obj.line_join, F(obj.miter),
                        tuple(F(v) for v in obj.dash), F(obj.dash_phase))
        self.dev.draw_path(obj.points, pm, graph, fill_argb, stroke_argb, fill_type, stroke,
                           self.type3_char is not None, False, self.rect_aa and fill_type != FILL_NONE)

    def process_form(self, obj: PObj, matrix: Matrix) -> None:
        m = R.concat(obj.matrix, matrix)
        status = Status(self.dev, self.transparency, self.in_group, obj.fill_alpha, self.ctx,
                        self.stop)
        status.rect_aa, status.type3_fonts = self.rect_aa, self.type3_fonts
        status.initial_fill = self.initial_fill if obj.fill is None else obj.fill
        status.initial_stroke = self.initial_stroke if obj.stroke is None else obj.stroke
        self.dev.save()
        status.render_list(obj.children, m)
        self.stopped = status.stopped
        self.dev.restore(False)


# ---------------------------------------------------------------------- page


def display_matrix(box: FloatRect, rotation: int) -> Matrix:
    """CPDF_Page::GetDisplayMatrix: UpdateDimensions' page_matrix_ (turned by /Rotate, the page
    size swapped on quarter turns) times the rect matrix of (0, 0, width, height), rotation 0."""
    left, bottom, right, top = (F(v) for v in box)
    w, h = F(right - left), F(top - bottom)
    if w == 0 or h == 0:
        return IDENTITY
    pm: Matrix
    if rotation == 1:
        w, h = h, w
        pm = (0.0, -1.0, 1.0, 0.0, -bottom, right)
    elif rotation == 2:
        pm = (-1.0, 0.0, 0.0, -1.0, right, top)
    elif rotation == 3:
        w, h = h, w
        pm = (0.0, 1.0, -1.0, 0.0, top, -left)
    else:
        pm = (1.0, 0.0, 0.0, 1.0, -left, -bottom)
    # ((x2 - x0) / w, (y2 - y0) / w, (x1 - x0) / h, (y1 - y0) / h, x0, y0) with x0 = 0, y0 = h
    return R.concat(pm, (F(w / w), 0.0, 0.0, F(-h / h), 0.0, h))


def page_matrix(box: FloatRect, rotation: int, fs: Matrix) -> Matrix:
    """FPDF_RenderPageBitmapWithMatrix: GetDisplayMatrix times the caller's FS_MATRIX (floats)."""
    a, b, c, d, e, f = fs
    return R.concat(display_matrix(box, rotation),
                    (F(float(a)), F(float(b)), F(float(c)), F(float(d)), F(float(e)), F(float(f))))


def unported(objects: Iterable[PObj], ctx: Context | None) -> str | None:
    """What an active object on the page needs that this module does not draw yet, if anything:
    a page is drawn exactly or not at all (PdfError), never approximately. `ctx`
    (render_transparency.Context) lets soft masks be looked into; without it they are refused."""
    for o in objects:
        p: PObj | None = o
        while p is not None and p.active:
            p = p.parent
        if p is not None:
            continue
        if o.clip_texts:
            from .render_text import clip_unsupported
            why = clip_unsupported(o.clip_texts)
            if why is not None:
                return why
        if o.type == OBJ_TEXT:
            from .render_text import unsupported as text_unsupported
            why = text_unsupported(o)
            if why is not None:
                return why
        elif o.type == OBJ_IMAGE:
            from .render_image import refusal as image_refusal
            why = image_refusal(o, ctx)
            if why is not None:
                return why
        elif o.type not in (OBJ_PATH, OBJ_FORM, OBJ_SHADING):
            return "objects"
        if o.smask is not None or o.blend != "Normal" or o.transfer is not None:
            from .render_transparency import unsupported
            why = unsupported(o, ctx, unported)
            if why is not None:
                return why
        if o.type == OBJ_FORM and o.group and ctx is None:
            return "transparency groups"
        if o.type in (OBJ_PATH, OBJ_SHADING):
            from . import render_shading
            reason = render_shading.refusal(o, ctx)
            if reason is not None:
                return reason
    return None


def render_page(objects: Sequence[PObj], box: FloatRect, rotation: int, fs: Matrix, width: int, height: int,
                transparent: bool, ctx: Context) -> BGRA:
    """FPDF_RenderPageBitmapWithMatrix onto a fresh bitmap (white, or clear when
    `transparent`) with FS_MATRIX `fs` (api.render_matrix): the BGRA bytes. `ctx`:
    render_transparency.Context (the document, for soft masks and groups)."""
    missing = unported(objects, ctx)
    if missing is not None:
        raise PdfError(f"the pure reader cannot render {missing} yet")
    dev = Device(width, height, transparent, None)
    if not transparent:
        dev.bgra[...] = 255
    matrix = page_matrix(box, rotation, fs)
    top = [o for o in objects if o.parent is None]
    ctx.page_objects, ctx.page_matrix = top, matrix     # for GetBackdrop's re-render
    dev.save()
    dev.set_clip_rect((0, 0, width, height))
    # CPDF_ProgressiveRenderer: one layer, the page's top-level objects
    dev.save()
    # the page's CPDF_Transparency: isolated always, a group when /Group /S /Transparency
    status = Status(dev, (ctx.page_group, True), False, 1.0, ctx, None)
    status.render_list(top, matrix)
    dev.restore(False)
    dev.restore(False)
    return dev.bgra
