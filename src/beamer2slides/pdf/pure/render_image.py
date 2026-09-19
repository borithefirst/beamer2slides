"""PDFium's image drawing, ported for `render.py`: CPDF_ImageRenderer (StartRenderDIBBase,
DrawMaskedImage and CalculateDrawImage for an image's own /SMask or /Mask stream), the AGG driver's
CFX_AggImageRenderer (upright and quarter-turned images, flips included), CFX_ImageStretcher and
CStretchEngine (its weight tables, the horizontal and vertical passes, uint32 sums that wrap as C's
do), CFX_AggBitmapComposer and the CFX_ScanlineCompositor rows it ends in, value for value.

The stretch engine works a row (or a column) at a time in PDFium; the passes here are the same sums
over whole arrays, and compositing, being per pixel, is done on the whole stretched block at once.
What is not ported (CFX_ImageTransformer for any other angle, an 8-bit mask device, blend modes on
images, pattern-filled stencils) raises PdfError: an image comes out exactly or not at all."""

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
    """Sum_j w_j * src[..., start + j, :] over the weight table: (n, ...) uint64 sums mod 2^32.
    `src` is (len, rest...) along the axis being resampled."""
    wc = table.shape[1]
    idx = np.clip(starts[:, None] + np.arange(wc)[None, :], 0, max(axis_len - 1, 0))
    out = np.zeros((len(starts),) + src.shape[1:], np.uint64)
    tab = table.astype(np.uint64)
    for k in range(wc):
        w = tab[:, k]
        if not w.any():
            continue
        vals = src[idx[:, k]].astype(np.uint64)
        wk = w.reshape((-1,) + (1,) * (src.ndim - 1))
        out = (out + ((vals * wk) & M32)) & M32
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
        acc = np.zeros((len(hs), band.shape[0], 4), np.uint64)
        for k in range(hw.shape[1]):
            w = hw[:, k].astype(np.uint64)
            if not w.any():
                continue
            px = cols[idx[:, k]].astype(np.uint64)            # (n, rows, 4)
            pw = ((w[:, None] * px[..., 3]) & M32) // 255
            for c in range(3):
                acc[..., c] = (acc[..., c] + ((pw * px[..., c]) & M32)) & M32
            acc[..., 3] = (acc[..., 3] + pw) & M32
        inter = np.zeros(acc.shape, np.uint8)
        inter[..., :3] = _pixel(acc[..., :3])
        inter[..., 3] = _pixel((acc[..., 3] * 255) & M32)
    else:
        inter = _pixel(_gather(cols, hs, hw, sw))                # (n, rows, ch)
    inter = np.moveaxis(inter, 0, 1)                          # (rows, n, ch): the inter buffer
    vs, vw = weights(dest_h, t, b, sh, sc[1], sc[3], bilinear)
    vs = vs - sc[1]
    if not has_alpha:
        block = _pixel(_gather(inter, vs, vw, inter.shape[0]))
        return block, out_fmt, pal
    idx = np.clip(vs[:, None] + np.arange(vw.shape[1])[None, :], 0, inter.shape[0] - 1)
    sums = np.zeros((len(vs), inter.shape[1], 4), np.uint64)
    for k in range(vw.shape[1]):
        w = vw[:, k].astype(np.uint64)
        if not w.any():
            continue
        px = inter[idx[:, k]].astype(np.uint64)
        sums = (sums + ((px * w[:, None, None]) & M32)) & M32
    block = np.zeros(sums.shape, np.uint8)
    line = np.zeros((inter.shape[1], 3), np.uint8)            # dest_scanline_: stale where a == 0
    for y in range(len(vs)):
        a = sums[y, :, 3]
        nz = a != 0
        if nz.any():
            q = ((sums[y, :, :3][nz] * 255) & M32) // a[nz][:, None]
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
    if dib.bpp > 1 and dib.bpp // 8 * dib.w * dib.h > HUGE_IMAGE:
        bilinear = True
    a, b, c, d = m[:4]
    image_rect = outer(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
    clip_box = fx_intersect(dev.clip_box(), image_rect)
    if clip_box[2] <= clip_box[0] or clip_box[3] <= clip_box[1]:
        return
    iw, ih = image_rect[2] - image_rect[0], image_rect[3] - image_rect[1]
    off = (clip_box[0] - image_rect[0], clip_box[1] - image_rect[1],
           clip_box[2] - image_rect[0], clip_box[3] - image_rect[1])
    if abs(b) >= 0.5 or a == 0 or abs(c) >= 0.5 or d == 0:
        if not (abs(a) < F(abs(b) / 20) and abs(d) < F(abs(c) / 20) and abs(a) < 0.5 and abs(d) < 0.5):
            raise PdfError("the pure reader cannot render images at this angle yet (CFX_ImageTransformer)")
        flip_x, flip_y = c > 0, b < 0
        l, t, r, bt = off
        nl, nr = (ih - t, ih - bt) if flip_y else (t, bt)
        nt, nb = (iw - l, iw - r) if flip_x else (l, r)
        bclip = (min(nl, nr), min(nt, nb), max(nl, nr), max(nt, nb))
        got = stretch(dib, ih, iw, bclip, bilinear)
        if got is None:
            return
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
            return
        got = stretch(dib, dw, dh, off, bilinear)
        if got is None:
            return
        block, sfmt, pal = got
    _compose_at(dev, block, sfmt, pal, clip_box, alpha, mask_argb)


def _compose_at(dev, block, sfmt, pal, box, alpha: float, mask_argb: int) -> None:
    """CFX_AggBitmapComposer over `box` of the device (clip mask and constant alpha)."""
    dkind = _kind(dev)
    if dkind == "mask":
        raise PdfError("the pure reader cannot render images onto 8-bit masks yet")
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
    mask_argb = _argb(obj.fill, obj.fill_alpha) if dib.fmt == "mask1" else 0
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
        if sfmt == "rgb8":
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
    if dib.bpp > 1 and dib.bpp // 8 * dib.w * dib.h > HUGE_IMAGE:
        bilinear = True
    a, b, c, d = m[:4]
    image_rect = outer(R.transform_rect(m, (0.0, 0.0, 1.0, 1.0)))
    box = fx_intersect((0, 0, dev.w, dev.h), image_rect)
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    if abs(b) >= 0.5 or a == 0 or abs(c) >= 0.5 or d == 0:
        raise PdfError("the pure reader cannot render turned soft-masked images yet")
    iw, ih = image_rect[2] - image_rect[0], image_rect[3] - image_rect[1]
    dw = -iw if a < 0 else iw
    dh = -ih if d > 0 else ih
    if dw == 0 or dh == 0:
        return None
    off = (box[0] - image_rect[0], box[1] - image_rect[1], box[2] - image_rect[0], box[3] - image_rect[1])
    got = stretch(dib, dw, dh, off, bilinear)
    if got is None:
        return None
    return got + (box,)


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
    try:
        dib = DI.load(ctx.doc, obj.stream, _resources(ctx, obj), need)
    except DI.Unsupported as e:
        raise PdfError(f"the pure reader cannot render {e} yet")
    cache[key] = (obj.stream, dib, need[0] != 0 and need[1] != 0)
    return dib


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
    d = obj.stream.dict
    r = ctx.doc.resolve
    if obj.smask is not None and d.get("SMask") is not None:
        return "images with a soft mask of their own under a soft mask"
    probes = ctx.__dict__.setdefault("image_probes", {})
    key = id(obj.stream)
    if key not in probes:
        why = None
        stencil = False
        try:
            dib = DI.load(ctx.doc, obj.stream, _resources(ctx, obj), (0, 0))
            stencil = dib is not None and dib.fmt == "mask1"
            if stencil and dib.mask is not None:
                why = "image masks with masks"
        except DI.Unsupported as e:
            why = str(e)
        probes[key] = (obj.stream, why, stencil)
    _, why, stencil = probes[key]
    if why is None and stencil and obj.fill_pattern is not None:
        return "pattern-filled image masks"
    return why
