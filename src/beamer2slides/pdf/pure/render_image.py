"""PDFium's image drawing, ported for `render.py`: CPDF_ImageRenderer (StartRenderDIBBase,
DrawMaskedImage and CalculateDrawImage for an image's own /SMask or /Mask stream), the AGG driver's
CFX_AggImageRenderer (upright and quarter-turned images, flips included), CFX_ImageStretcher and
CStretchEngine (its weight tables, the horizontal and vertical passes, uint32 sums that wrap as C's
do), CFX_ImageTransformer for any other angle or skew (the stretch to the unit vectors' lengths,
then 8.8 fixed-point bilinear sampling through the inverse matrix), CFX_AggBitmapComposer and the
CFX_ScanlineCompositor rows it ends in, value for value.

The stretch engine works a row (or a column) at a time in PDFium; the passes here are the same sums
over whole arrays, and compositing, being per pixel, is done on the whole stretched block at once.
The Darken blend CPDF_ImageRenderer picks for an overprinted CMYK image never reaches the pixels:
the AGG driver's StartDIBits drops its blend mode. What is not ported (an 8-bit mask device, blend
modes on images, pattern-filled stencils) raises PdfError: an image comes out exactly or not at all."""

from __future__ import annotations

import numpy as np

from ..api import PdfError
from . import decode_image as DI
from . import raster as R
from .raster import F
from .render_shading import outer, fx_intersect

M32 = 0xFFFFFFFF
HUGE_IMAGE = 60000000


# ---------------------------------------------------------------------- CStretchEngine


def _fixed(d: float) -> int:
    """FixedFromDouble: uint32(FXSYS_round(d * 65536))."""
    v = d * 65536
    if v != v:
        return 0
    r = int(abs(v) + 0.5) if abs(v) < 4503599627370496.0 else int(abs(v))
    r = r if v >= 0 else -r
    r = max(-2147483648, min(2147483647, r))
    return r & M32


def weights(dest_len, dmin, dmax, src_len, smin, smax, bilinear):
    """WeightTable::CalculateWeights: (starts, weight matrix) for dest pixels dmin..dmax-1."""
    scale = float(src_len) / dest_len
    base = float(src_len) if dest_len < 0 else 0.0
    wc = int(np.ceil(abs(scale))) + 1
    n = dmax - dmin
    starts = np.zeros(n, np.int64)
    table = np.zeros((n, wc), np.int64)
    if abs(scale) < 1.0:
        for k, dp in enumerate(range(dmin, dmax)):
            pos = dp * scale + scale / 2 + base
            if bilinear:
                s = max(int(np.floor(pos - 0.5)), smin)
                e = min(int(np.floor(pos + 0.5)), smax - 1)
                starts[k] = s
                if s >= e:
                    table[k, 0] = 65536
                else:
                    w1 = _fixed(pos - s - 0.5)
                    table[k, 1] = w1
                    table[k, 0] = (65536 - w1) & M32
            else:
                p = int(np.floor(pos))
                starts[k] = max(p, smin)
                table[k, 0] = 65536
        return starts, table
    for k, dp in enumerate(range(dmin, dmax)):
        s0 = dp * scale + base
        s1 = s0 + scale
        start = max(int(np.floor(min(s0, s1))), smin)
        end = min(int(np.floor(max(s0, s1))), smax - 1)
        if start > end:
            starts[k] = min(start, smax - 1)
            continue
        starts[k] = start
        remaining = 65536
        err = 0.0
        for j in range(start, end):
            ds = (j - base) / scale
            de = (j + 1 - base) / scale
            if ds > de:
                ds, de = de, ds
            w = max(0.0, min(de, float(dp + 1)) - max(ds, float(dp)))
            fw = _fixed(w + err)
            table[k, j - start] = fw
            remaining = (remaining - fw) & M32
            err = w - fw / 65536.0
        if remaining and remaining <= 65536:
            table[k, end - start] = remaining
        else:
            table[k, end - 1 - start] = (table[k, end - 1 - start] + remaining) & M32
    return starts, table


def _gather(src: np.ndarray, starts, table, axis_len: int):
    """Sum_j w_j * src[..., start + j, :] over the weight table: (n, ...) uint32 sums, wrapping
    as C's uint32 arithmetic does (numpy's uint32 products and sums wrap mod 2^32 the same way).
    `src` is (len, rest...) along the axis being resampled."""
    wc = table.shape[1]
    idx = np.clip(starts[:, None] + np.arange(wc)[None, :], 0, max(axis_len - 1, 0))
    out = np.zeros((len(starts),) + src.shape[1:], np.uint32)
    tab = table.astype(np.uint32)
    shape = (-1,) + (1,) * (src.ndim - 1)
    for k in range(wc):
        w = tab[:, k]
        if not w.any():
            continue
        out += src[idx[:, k]] * w.reshape(shape)
    return out


def _pixel(v):
    return ((v >> 16) & 255).astype(np.uint8)


def stretch(dib, dest_w: int, dest_h: int, clip, bilinear_opt: bool):
    """CFX_ImageStretcher::Start + CStretchEngine: (block, format, palette) with block the
    (clip height, clip width, channels) pixels handed to ComposeScanline, or None."""
    if dest_w == 0 or dest_h == 0:
        return None
    fmt, rows, pal = dib.fmt, dib.rows, dib.palette
    has_alpha = False
    if fmt in ("mask1", "rgb1"):
        src = (rows * 255).astype(np.uint8)[..., None]
        out_fmt = "mask8" if fmt == "mask1" else "rgb8"
        if fmt == "rgb1" and pal is not None:
            c0, c1 = pal
            out_pal = []
            for i in range(256):
                ch = []
                for sh in (16, 8, 0):
                    a, b = (c0 >> sh) & 255, (c1 >> sh) & 255
                    q = abs((b - a) * i) // 255
                    ch.append(a + (q if b >= a else -q))
                out_pal.append(DI.argb(255, *ch))
            pal = out_pal
        else:
            pal = None
    elif fmt == "rgb8":
        if pal is not None:
            p = np.array(pal, np.uint32)[rows]
            src = np.stack([p & 255, (p >> 8) & 255, (p >> 16) & 255], -1).astype(np.uint8)
            out_fmt, pal = "bgr", None
        else:
            src, out_fmt = rows[..., None], "rgb8"
    elif fmt == "bgr":
        src, out_fmt = rows, "bgr"
    else:
        src, out_fmt, has_alpha = rows, "bgra", True
    sh, sw = dib.h, dib.w
    if abs(dest_w) > 1048576 or abs(dest_h) > 1048576:
        return None
    l, t, r, b = clip
    if r - l <= 0:
        return None        # inter_pitch 0: StartStretchHorz fails
    bilinear = bilinear_opt
    if not bilinear and abs(dest_h) // 8 < (sw * sh) // abs(dest_w):
        bilinear = True
    scale_x = float(np.float32(sw) / np.float32(dest_w))
    scale_y = float(np.float32(sh) / np.float32(dest_h))
    base_x = 0.0 if dest_w > 0 else float(dest_w)
    base_y = 0.0 if dest_h > 0 else float(dest_h)
    sl, sr = scale_x * (l + base_x), scale_x * (r + base_x)
    st, sb = scale_y * (t + base_y), scale_y * (b + base_y)
    sl, sr = min(sl, sr), max(sl, sr)
    st, sb = min(st, sb), max(st, sb)
    sc = fx_intersect((int(np.floor(sl)), int(np.floor(st)), int(np.ceil(sr)), int(np.ceil(sb))), (0, 0, sw, sh))
    if sc[3] - sc[1] <= 0:
        return None
    hs, hw = weights(dest_w, l, r, sw, sc[0], sc[2], bilinear)
    band = src[sc[1]:sc[3]]                                   # (rows, sw, ch)
    cols = np.moveaxis(band, 1, 0)                            # (sw, rows, ch)
    if has_alpha:
        idx = np.clip(hs[:, None] + np.arange(hw.shape[1])[None, :], 0, sw - 1)
        acc = np.zeros((len(hs), band.shape[0], 4), np.uint32)   # uint32: C's wrapping sums
        for k in range(hw.shape[1]):
            w = hw[:, k].astype(np.uint32)
            if not w.any():
                continue
            px = cols[idx[:, k]]                                  # (n, rows, 4)
            pw = (w[:, None] * px[..., 3]) // 255
            acc[..., :3] += pw[..., None] * px[..., :3]
            acc[..., 3] += pw
        inter = np.zeros(acc.shape, np.uint8)
        inter[..., :3] = _pixel(acc[..., :3])
        inter[..., 3] = _pixel(acc[..., 3] * np.uint32(255))
    else:
        inter = _pixel(_gather(cols, hs, hw, sw))                # (n, rows, ch)
    inter = np.moveaxis(inter, 0, 1)                          # (rows, n, ch): the inter buffer
    vs, vw = weights(dest_h, t, b, sh, sc[1], sc[3], bilinear)
    vs = vs - sc[1]
    if not has_alpha:
        block = _pixel(_gather(inter, vs, vw, inter.shape[0]))
        return block, out_fmt, pal
    idx = np.clip(vs[:, None] + np.arange(vw.shape[1])[None, :], 0, inter.shape[0] - 1)
    sums = np.zeros((len(vs), inter.shape[1], 4), np.uint32)
    for k in range(vw.shape[1]):
        w = vw[:, k].astype(np.uint32)
        if not w.any():
            continue
        sums += inter[idx[:, k]] * w[:, None, None]
    block = np.zeros(sums.shape, np.uint8)
    line = np.zeros((inter.shape[1], 3), np.uint8)            # dest_scanline_: stale where a == 0
    for y in range(len(vs)):
        a = sums[y, :, 3]
        nz = a != 0
        if nz.any():
            q = (sums[y, :, :3][nz] * np.uint32(255)) // a[nz][:, None]
            q = q.astype(np.int64)
            q = np.where(q >= 2147483648, q - 4294967296, q)
            line[nz] = np.clip(q, 0, 255).astype(np.uint8)
        block[y, :, :3] = line
        block[y, :, 3] = _pixel(a)
    return block, out_fmt, pal


# ---------------------------------------------------------------------- compositing


def _union(d, s):
    return d + s - d * s // 255


def _merge(back, src, alpha):
    return (back * (255 - alpha) + src * alpha) // 255


def compose(dest: np.ndarray, dkind: str, block, sfmt: str, pal, clip, mask_argb: int):
    """CFX_ScanlineCompositor's row functions (no blending) over a whole block: `dest` (h, w, 4)
    uint8 view, `clip` (h, w) int array or None (after the composer's alpha)."""
    d = dest.astype(np.int64)
    if sfmt == "mask8":
        s = block[..., 0].astype(np.int64)
        ma = (mask_argb >> 24) & 255
        sa = ma * s * clip // 255 // 255 if clip is not None else ma * s // 255
        col = np.array([mask_argb & 255, (mask_argb >> 8) & 255, (mask_argb >> 16) & 255], np.int64)
        if dkind == "bgrx":
            m = sa > 0
            for c in range(3):
                d[..., c] = np.where(m, _merge(d[..., c], col[c], sa), d[..., c])
        else:
            back = d[..., 3]
            da = _union(back, sa) & 255
            ratio = np.where(da > 0, sa * 255 // np.maximum(da, 1), 0)
            fresh = back == 0
            upd = ~fresh & (sa != 0)
            for c in range(3):
                d[..., c] = np.where(fresh, col[c], np.where(upd, _merge(d[..., c], col[c], ratio), d[..., c]))
            d[..., 3] = np.where(fresh, sa, np.where(upd, da, back))
        dest[...] = d.astype(np.uint8)
        return
    if sfmt == "rgb8":
        if pal is not None:
            p = np.array(pal, np.int64)[block[..., 0]]
            s = np.stack([p & 255, (p >> 8) & 255, (p >> 16) & 255], -1)
        else:
            s = np.repeat(block[..., :1].astype(np.int64), 3, -1)
        sa = None
    elif sfmt == "bgr":
        s, sa = block.astype(np.int64), None
    else:
        s = block[..., :3].astype(np.int64)
        sa = block[..., 3].astype(np.int64)
    if sa is None:
        if dkind == "bgrx":
            if clip is None:
                d[..., :3] = s
            else:
                full = (clip == 255)[..., None]
                d[..., :3] = np.where(full, s, _merge(d[..., :3], s, clip[..., None]))
        else:
            if clip is None:
                d[..., :3] = s
                d[..., 3] = 255
            else:
                back = d[..., 3]
                da = _union(back, clip) & 255
                ratio = clip * 255 // np.maximum(da, 1)
                full, none = clip == 255, clip == 0
                merged = _merge(d[..., :3], s, ratio[..., None])
                d[..., :3] = np.where(full[..., None], s, np.where(none[..., None], d[..., :3], merged))
                d[..., 3] = np.where(full, 255, np.where(none, back, da))
    else:
        c = clip if clip is not None else 255
        a = sa * c // 255
        if dkind == "bgrx":
            full, none = (a == 255)[..., None], (a == 0)[..., None]
            d[..., :3] = np.where(full, s, np.where(none, d[..., :3], _merge(d[..., :3], s, a[..., None])))
        else:
            back = d[..., 3]
            fresh = back == 0
            da = _union(back, a) & 255
            ratio = a * 255 // np.maximum(da, 1)
            upd = ~fresh & (a != 0)
            d[..., :3] = np.where(fresh[..., None], s, np.where(upd[..., None], _merge(d[..., :3], s, ratio[..., None]),
                                                                 d[..., :3]))
            d[..., 3] = np.where(fresh, a, np.where(upd, da, back))
    dest[...] = d.astype(np.uint8)


def _kind(dev) -> str:
    from .render_transparency import kind
    return kind(dev)


def start_dibits(dev, dib, alpha: float, mask_argb: int, m, bilinear: bool) -> None:
    """CPDF_ImageRenderer::StartDIBBase -> CFX_AggDeviceDriver::StartDIBits ->
    CFX_AggImageRenderer: stretch (or turn a quarter), then compose through the clip."""
    got = _block(dib, m, dev.clip_box(), bilinear)
    if got is not None:
        _compose_at(dev, got[0], got[1], got[2], got[3], alpha, mask_argb)


def _block(dib, m, device_clip, bilinear):
    """The image stretched (or turned a quarter) to the device pixels it covers inside
    `device_clip`: (block, format, palette, box) or None."""
    if dib.bpp > 1 and dib.bpp // 8 * dib.w * dib.h > HUGE_IMAGE:
        bilinear = True
    a, b, c, d = m[:4]
    image_rect = outer(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
    clip_box = fx_intersect(device_clip, image_rect)
    if clip_box[2] <= clip_box[0] or clip_box[3] <= clip_box[1]:
        return None
    iw, ih = image_rect[2] - image_rect[0], image_rect[3] - image_rect[1]
    off = (clip_box[0] - image_rect[0], clip_box[1] - image_rect[1],
           clip_box[2] - image_rect[0], clip_box[3] - image_rect[1])
    if abs(b) >= 0.5 or a == 0 or abs(c) >= 0.5 or d == 0:
        if not (abs(a) < F(abs(b) / 20) and abs(d) < F(abs(c) / 20) and abs(a) < 0.5 and abs(d) < 0.5):
            return transform(dib, m, clip_box, bilinear)
        flip_x, flip_y = c > 0, b < 0
        l, t, r, bt = off
        nl, nr = (ih - t, ih - bt) if flip_y else (t, bt)
        nt, nb = (iw - l, iw - r) if flip_x else (l, r)
        bclip = (min(nl, nr), min(nt, nb), max(nl, nr), max(nt, nb))
        got = stretch(dib, ih, iw, bclip, bilinear)
        if got is None:
            return None
        block, sfmt, pal = got
        block = np.swapaxes(block, 0, 1)
        if flip_x:
            block = block[:, ::-1]
        if flip_y:
            block = block[::-1]
    else:
        dw = -iw if a < 0 else iw
        dh = -ih if d > 0 else ih
        if dw == 0 or dh == 0:
            return None
        got = stretch(dib, dw, dh, off, bilinear)
        if got is None:
            return None
        block, sfmt, pal = got
    return block, sfmt, pal, clip_box


# ---------------------------------------------------------------------- CFX_ImageTransformer


def _i32(v: float) -> bool:
    return v == v and -2147483648.0 <= v <= 2147483647.0


def _match_range(f1: float, f2: float):
    """MatchFloatRange (float32)."""
    with np.errstate(invalid="ignore", over="ignore"):
        length = F(float(np.ceil(F(f2 - f1))))
        lo, hi = float(np.floor(f1)), float(np.ceil(f1))
        e1 = F(F(f1 - lo) + abs(F(F(f2 - lo) - length)))
        e2 = F(F(hi - f1) + abs(F(F(f2 - hi) - length)))
        start = hi if e1 > e2 else lo
        end = F(start + length)
    if not (_i32(start) and _i32(end)):
        return 0, 0
    return int(start), int(end)


def closest_rect(rect):
    """CFX_FloatRect::GetClosestRect of (left, bottom, right, top) -> FX_RECT (l, t, r, b)."""
    l, r = _match_range(rect[0], rect[2])
    t, b = _match_range(rect[1], rect[3])
    return min(l, r), min(t, b), max(l, r), max(t, b)


def _valid(rect) -> bool:
    """FX_RECT::Valid: width and height fit an int32."""
    return -2147483648 <= rect[2] - rect[0] <= 2147483647 and -2147483648 <= rect[3] - rect[1] <= 2147483647


def _fixed256(v: float) -> int:
    from .render_shading import roundf
    return roundf(F(v * 256.0))


def _fix_split(val: np.ndarray):
    """CFX_BilinearMatrix::Transform's integer part (saturated) and remainder (0..255)."""
    with np.errstate(invalid="ignore", over="ignore"):
        q = np.trunc(val / np.float32(256)).astype(np.float64)
        whole = np.where(q != q, 0, np.clip(q, -2147483648.0, 2147483647.0)).astype(np.int64)
        v = np.trunc(val.astype(np.float64))
        ok = (v == v) & (v >= -2147483648.0) & (v <= 2147483647.0)
        iv = np.where(ok, v, -2147483648.0).astype(np.int64)
    res = np.fmod(iv, 256)
    res = np.where(res < 0, res + 256, res)
    return whole, res


def transform(dib, m, clip_box, bilinear):
    """CFX_ImageTransformer for an image neither upright nor a quarter turn: the image stretched
    to its unit vectors' lengths, then sampled for every result pixel through the inverse matrix in
    8.8 fixed point (BilinearInterpolate, whose weights are 255 - r and r). Returns (block, "T:bgra"
    or "T:mask8", None, result box) or None."""
    a, b, c, d, e, f = m
    result_rect = closest_rect(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
    result = fx_intersect(result_rect, clip_box)
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    if abs(a) < F(abs(b) / 20) and abs(d) < F(abs(c) / 20) and abs(a) < 0.5 and abs(d) < 0.5:
        raise PdfError("the pure reader cannot render this quarter turn yet (CFX_ImageTransformer)")
    if abs(b) < F(0.05) and abs(c) < F(0.05):
        # kNormal: reached only with a or d exactly 0, which stretches to nothing
        wd = int(np.ceil(a)) if a > 0 else int(np.floor(a))
        hd = -int(np.ceil(d)) if d > 0 else -int(np.floor(d))
        if wd != 0 and hd != 0:
            raise PdfError("the pure reader cannot render this nearly upright image yet (CFX_ImageTransformer)")
        return None
    sw = int(np.ceil(R._hypotf(a, b)))
    if sw == 0:
        return None
    sh = int(np.ceil(R._hypotf(c, d)))
    if sh == 0:
        return None
    s2d = R.concat((1.0, 0.0, 0.0, -1.0, 0.0, F(sh)),
                   (F(a / sw), F(b / sw), F(c / sh), F(d / sh), e, f))
    d2s = R.inverse(s2d)
    rl, rt, rr, rb = result
    sclip = outer(R.transform_rect(d2s, (float(rl), float(rt), float(rr), float(rb))))
    if not _valid(sclip):
        return None
    sclip = fx_intersect(sclip, (0, 0, sw, sh))
    if sclip[2] <= sclip[0] or sclip[3] <= sclip[1]:
        return None
    got = stretch(dib, sw, sh, sclip, bilinear)
    if got is None:
        raise PdfError("the pure reader cannot render an image the stretcher leaves blank yet")
    src, sfmt, pal = got
    r2s = R.concat((1.0, 0.0, 0.0, 1.0, F(rl), F(rt)), d2s)
    r2s = r2s[:4] + (F(r2s[4] + float(-sclip[0])), F(r2s[5] + float(-sclip[1])))
    fa, fb, fc, fd, fe, ff = (np.float32(_fixed256(v)) for v in r2s)
    W, H = rr - rl, rb - rt
    xs = np.arange(W, dtype=np.float32)[None, :]
    ys = np.arange(H, dtype=np.float32)[:, None]
    with np.errstate(invalid="ignore", over="ignore"):
        vx = ((fa * xs + fc * ys) + fe) + np.float32(128)
        vy = ((fb * xs + fd * ys) + ff) + np.float32(128)
    col, rx = _fix_split(vx)
    row, ry = _fix_split(vy)
    cw, chh = sclip[2] - sclip[0], sclip[3] - sclip[1]
    inside = (col >= 0) & (col <= cw) & (row >= 0) & (row <= chh)
    cl = np.where(col == cw, col - 1, col)
    rw = np.where(row == chh, row - 1, row)
    cr = np.where(cl + 1 == cw, cl, cl + 1)
    rr_ = np.where(rw + 1 == chh, rw, rw + 1)
    cl, cr = np.clip(cl, 0, cw - 1), np.clip(cr, 0, cw - 1)
    rw, rr_ = np.clip(rw, 0, chh - 1), np.clip(rr_, 0, chh - 1)
    px = src.astype(np.int64)
    irx, iry = (255 - rx)[..., None], (255 - ry)[..., None]
    rx, ry = rx[..., None], ry[..., None]
    r0 = ((px[rw, cl] * irx + px[rw, cr] * rx) >> 8) & 255
    r1 = ((px[rr_, cl] * irx + px[rr_, cr] * rx) >> 8) & 255
    val = ((r0 * iry + r1 * ry) >> 8) & 255
    val = np.where(inside[..., None], val, 0)
    if sfmt == "mask8":
        return val.astype(np.uint8), "T:mask8", None, result
    out = np.zeros((H, W, 4), np.uint8)
    if sfmt == "rgb8":
        idx = val[..., 0]
        if pal is not None:
            p = np.array((list(pal) + [0] * 256)[:256], np.int64)[idx]
            out[...] = np.stack([p & 255, (p >> 8) & 255, (p >> 16) & 255, (p >> 24) & 255], -1)
        else:
            out[..., 0] = out[..., 1] = out[..., 2] = idx
            out[..., 3] = 255
    elif sfmt == "bgr":
        out[..., :3] = val
        out[..., 3] = 255
    else:
        out[...] = val
    out[~inside] = 0
    return out, "T:bgra", None, result


def _compose_transformed(dev, dkind, block, sfmt, box, alpha: float, mask_argb: int) -> None:
    """CFX_AggImageRenderer::Continue after a transform: CompositeMask with the alpha in the mask
    colour, or MultiplyAlpha then CompositeBitmap; the clip region's mask as the clip scan."""
    from .render_shading import roundf
    l, t, r, b = box
    clip = None
    cl = dev.clip
    if cl is not None and cl.mask is not None:
        cb = cl.box
        clip = cl.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int64)
    if sfmt == "T:mask8":
        if alpha != 1.0:
            k = roundf(F(alpha * 255))
            mask_argb = ((((mask_argb >> 24) * k // 255) & 255) << 24) | (mask_argb & 0xFFFFFF)
        if (mask_argb >> 24) == 0:
            return
        fmt = "mask8"
    else:
        if alpha != 1.0:
            k = int(F(alpha * 255))
            block = block.copy()
            block[..., 3] = (block[..., 3].astype(np.int64) * k // 255).astype(np.uint8)
        fmt = "bgra"
    wl, wt = max(l, dev.ox), max(t, dev.oy)
    wr, wb = min(r, dev.ox + dev.bgra.shape[1]), min(b, dev.oy + dev.bgra.shape[0])
    if wr <= wl or wb <= wt:
        return
    sub = (slice(wt - t, wb - t), slice(wl - l, wr - l))
    dest = dev.bgra[wt - dev.oy:wb - dev.oy, wl - dev.ox:wr - dev.ox]
    compose(dest, dkind, block[sub], fmt, None, clip[sub] if clip is not None else None, mask_argb)


def _compose_at(dev, block, sfmt, pal, box, alpha: float, mask_argb: int) -> None:
    """CFX_AggBitmapComposer over `box` of the device (clip mask and constant alpha)."""
    dkind = _kind(dev)
    if dkind == "mask":
        raise PdfError("the pure reader cannot render images onto 8-bit masks yet")
    if sfmt.startswith("T:"):
        _compose_transformed(dev, dkind, block, sfmt, box, alpha, mask_argb)
        return
    l, t, r, b = box
    clip = None
    cl = dev.clip
    if cl is not None and cl.mask is not None:
        cb = cl.box
        clip = cl.mask[t - cb[1]:b - cb[1], l - cb[0]:r - cb[0]].astype(np.int64)
    if alpha != 1.0:
        if clip is not None:
            clip = (clip.astype(np.float32) * np.float32(alpha)).astype(np.int64)
        else:
            clip = np.full((b - t, r - l), DI.roundf(F(alpha * 255)), np.int64)
    # the device keeps only its window
    wl, wt = max(l, dev.ox), max(t, dev.oy)
    wr, wb = min(r, dev.ox + dev.bgra.shape[1]), min(b, dev.oy + dev.bgra.shape[0])
    if wr <= wl or wb <= wt:
        return
    sub = (slice(wt - t, wb - t), slice(wl - l, wr - l))
    dest = dev.bgra[wt - dev.oy:wb - dev.oy, wl - dev.ox:wr - dev.ox]
    compose(dest, dkind, block[sub], sfmt, pal, clip[sub] if clip is not None else None, mask_argb)


# ---------------------------------------------------------------------- CPDF_ImageRenderer


def draw(status, obj, matrix) -> None:
    """CPDF_RenderStatus::ProcessImage -> CPDF_ImageRenderer::Start."""
    from .render import _argb
    dev = status.dev
    m = R.concat(obj.matrix, matrix)
    rect = outer(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
    dib = get_dib(status.ctx, obj, dev)
    if dib is None:
        return
    alpha = F(obj.fill_alpha)
    mask_argb = 0
    if dib.fmt == "mask1":
        # GetFillArgb: in a Type 3 glyph the text's colour unless a d0 glyph set its own
        typed3 = obj.fill is None or getattr(status, "type3_char", None) is not None
        mask_argb = status.fill_argb(obj) if typed3 else _argb(obj.fill, obj.fill_alpha)
    if dib.mask is not None:
        draw_masked(dev, dib, alpha, m, rect)
        return
    start_dibits(dev, dib, alpha, mask_argb, m, dib.interpolate)


def draw_masked(dev, dib, alpha: float, m, rect) -> None:
    """DrawMaskedImage: the image on a white BGRx bitmap, its mask on a gray one, the mask as
    alpha, then SetDIBitsWithBlend."""
    from .render import Device
    from .render_transparency import multiply_alpha, set_dibits
    rect = fx_intersect(rect, dev.clip_box())
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    nm = m[:4] + (F(m[4] + float(-rect[0])), F(m[5] + float(-rect[1])))
    img = Device(w, h, False)
    img.bgra[...] = 255
    start_dibits(img, dib, 1.0, 0, nm, dib.interpolate)
    gray = Device(w, h, False)
    got = _stretch_for(gray, dib.mask, nm, dib.interpolate)
    mask = np.zeros((h, w), np.int64)
    if got is not None:
        block, sfmt, pal, box = got
        if sfmt in ("mask8", "T:mask8"):
            # a stencil /Mask drawn in white (0xffffffff) onto black: its coverage
            v = block[..., 0].astype(np.int64)
        elif sfmt == "T:bgra":
            # CompositeRowBgra2Gray onto zeros: AlphaMerge(0, gray, alpha)
            px = block.astype(np.int64)
            v = (px[..., 2] * 30 + px[..., 1] * 59 + px[..., 0] * 11) // 100 * px[..., 3] // 255
        elif sfmt == "rgb8":
            v = block[..., 0].astype(np.int64)
            if pal is not None:
                p = np.array(pal, np.int64)
                g = (((p >> 16) & 255) * 30 + ((p >> 8) & 255) * 59 + (p & 255) * 11) // 100
                v = g[v]
        else:
            px = block.astype(np.int64)
            v = (px[..., 2] * 30 + px[..., 1] * 59 + px[..., 0] * 11) // 100
        mask[box[1]:box[3], box[0]:box[2]] = v
    if dib.matte != 0xFFFFFFFF:
        mr, mg, mb = (dib.matte >> 16) & 255, (dib.matte >> 8) & 255, dib.matte & 255
        px = img.bgra.astype(np.int64)
        nz = mask != 0
        dm = np.maximum(mask, 1)
        for ch, mt in ((0, mb), (1, mg), (2, mr)):
            num = (px[..., ch] - mt) * 255
            q = np.where(num < 0, -((-num) // dm), num // dm) + mt
            px[..., ch] = np.where(nz, np.clip(q, 0, 255), px[..., ch])
        img.bgra[...] = px.astype(np.uint8)
    out = img.bgra.copy()
    out[..., 3] = mask.astype(np.uint8)
    multiply_alpha(out, alpha)
    set_dibits(dev, out, "bgra", rect[0], rect[1], "Normal")


def _stretch_for(dev, dib, m, bilinear):
    """The mask drawn onto CalculateDrawImage's 8bppRgb bitmap: (block, format, palette, box) in
    that bitmap, or None. Composited onto a zeroed gray bitmap without a clip, it is set as is."""
    return _block(dib, m, (0, 0, dev.w, dev.h), bilinear)


# ---------------------------------------------------------------------- loading, refusing


def get_dib(ctx, obj, dev):
    """CPDF_ImageLoader through the page's CPDF_PageImageCache (a JPEG is decoded at 1/2, 1/4 or
    1/8 of its size when the device is that much smaller, and the cached bitmap is reused while it
    is at least the device's size)."""
    cache = getattr(ctx, "images", None)
    if cache is None:
        cache = ctx.images = {}
    key = id(obj.stream)
    need = (dev.w, dev.h)
    hit = cache.get(key)
    if hit is not None:
        dib, set_max = hit[1], hit[2]
        if dib is None or not set_max or (dib.w >= need[0] and dib.h >= need[1]):
            if hit[0] is obj.stream:
                return dib
    dib = _probed(ctx, obj, need)
    if dib is _NOT_PROBED:
        try:
            dib = DI.load(ctx.doc, obj.stream, _resources(ctx, obj), need)
        except DI.Unsupported as e:
            raise PdfError(f"the pure reader cannot render {e} yet")
    cache[key] = (obj.stream, dib, need[0] != 0 and need[1] != 0)
    return dib


_NOT_PROBED = object()


def _probed(ctx, obj, need):
    """The bitmap `refusal` loaded for this image (at no device size), when loading it for `need`
    gives the same: the device size only picks a JPEG's scale, and only for a DCT image at least
    twice the device's size both ways (decode_image._jpeg; masks load at no size either way)."""
    probes = getattr(ctx, "image_probes", None)
    got = probes.get(id(obj.stream)) if probes else None
    if got is None or got[0] is not obj.stream or got[1] is not None or len(got) < 4:
        return _NOT_PROBED
    d = obj.stream.dict
    r = ctx.doc.resolve
    w, h = r(d.get("Width")), r(d.get("Height"))
    if need[0] and need[1] and isinstance(w, int) and isinstance(h, int) and w >= 2 * need[0] and h >= 2 * need[1]:
        # a DCTDecode anywhere in the chain is where decode_image.image_bytes goes to `_jpeg`
        decoders = DI.FL.decoder_array(d, r)
        if decoders is None or any(DI.FL.ABBREVIATIONS.get(n, n) == "DCTDecode" for n, _p in decoders):
            return _NOT_PROBED
    probes[id(obj.stream)] = got[:3]      # handed on: the page cache holds the bitmap now
    return got[3]


def _resources(ctx, obj):
    res = getattr(obj, "resources", None)
    return res if isinstance(res, dict) else ctx.page_resources


def refusal(obj, ctx) -> str | None:
    """What drawing this image object needs that is not ported, if anything."""
    if ctx is None:
        return "images"
    if ctx.depth > 0:
        return "images in soft masks"
    if obj.blend != "Normal":
        return "images with blend modes"
    if not getattr(obj.stream, "exact", True):
        return "inline images whose codec's end is not found as PDFium finds it (DCT, CCITT)"
    d = obj.stream.dict
    r = ctx.doc.resolve
    if obj.smask is not None and d.get("SMask") is not None:
        return "images with a soft mask of their own under a soft mask"
    probes = ctx.__dict__.setdefault("image_probes", {})
    key = id(obj.stream)
    if key not in probes:
        why = None
        stencil = False
        dib = None
        try:
            dib = DI.load(ctx.doc, obj.stream, _resources(ctx, obj), (0, 0))
            stencil = dib is not None and dib.fmt == "mask1"
            if stencil and dib.mask is not None:
                why = "image masks with masks"
        except DI.Unsupported as e:
            why = str(e)
        # the bitmap too, for `get_dib` to take instead of loading the image again (`_probed`)
        probes[key] = (obj.stream, why, stencil, dib)
    _, why, stencil = probes[key][:3]
    if why is None and stencil and obj.fill_pattern is not None:
        return "pattern-filled image masks"
    return why
