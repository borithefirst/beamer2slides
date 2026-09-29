"""PDFium's tiling patterns, ported for `render.py`: CPDF_TilingPattern::Load (the pattern stream
parsed as a form of its own, over its own resources only, with the painted object's general state)
and CPDF_RenderTiling::Draw (one cell drawn into a bitmap and stamped over the clip box, or, when
a cell is bigger than the clip, the cell's objects drawn again per tile), value for value.

The cell's own drawing is `render.py`'s: the cell is a form like any other, so whatever it holds
that is not ported yet is refused by `unsupported` with that content's own reason. What this
module refuses on its own: uncoloured cells (PaintType 2, which PDFium draws through an 8-bit
mask device this reader has no compositor for) and a pattern used inside a form (the parser gives
the cell's /BBox clip the form's matrix but its objects only the pattern's, and nothing here
reproduces that yet)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

import numpy as np

from ...arrays import BGRA, Pixels
from ..api import OBJ_IMAGE, OBJ_PATH, Matrix
from . import raster as R
from .crt import cdiv, roundf
from .raster import F, FloatRect, IntRect
from .render import IDENTITY, Device, Status, unported
from .render_image import HUGE_IMAGE, stretch
from .render_transparency import Context, composite_bitmap, form_transparency, set_dibits
from .syntax import Stream

if TYPE_CHECKING:
    from .content import PObj
    from .render_shading import Record

INT_MAX = 2147483647


# ---------------------------------------------------------------------- the cell


@dataclass(frozen=True, kw_only=True)
class Tiling:
    """A tiling pattern's dictionary half of CPDF_TilingPattern::Load: its stream, steps and
    /BBox."""
    stream: Stream
    x_step: float
    y_step: float
    bbox: FloatRect


@dataclass(frozen=True, kw_only=True)
class Cell:
    """What CPDF_TilingPattern::Load gives back: the pattern's measures and its form's objects
    (the form's children, and every object the parse made but the form itself)."""
    children: list[PObj]
    every: list[PObj]
    transparency: tuple[bool, bool]
    bbox: FloatRect
    colored: bool
    x_step: float
    y_step: float


General: TypeAlias = "tuple[float, float, str, int, Matrix, int]"
"""The painted object's general state a cell is parsed with (fill and stroke alpha, blend mode,
the soft mask's and transfer function's identities and the soft mask's matrix): the cell cache's
key."""


def _general(obj: PObj) -> General:
    """The page object's general state, which CPDF_TilingPattern::Load hands the cell's parse."""
    return (F(obj.fill_alpha), F(obj.stroke_alpha), obj.blend, id(obj.smask),
            obj.smask_matrix, id(obj.transfer))


def load(rec: Record) -> bool:
    """CPDF_TilingPattern::Load's dictionary half (the cell itself is parsed per painted object,
    `cell`): false when the pattern object is not a stream, which draws nothing."""
    from .render_shading import Unsupported
    a, obj = rec.a, rec.obj
    d = a.dict_of(obj)
    if not isinstance(obj, Stream) or d is None:
        return False
    if a.integer_for(d, "PaintType") != 1:
        raise Unsupported("uncoloured tiling patterns")
    if rec.parent_matrix != IDENTITY:
        raise Unsupported("a tiling pattern inside a form")
    rec.tiling = Tiling(stream=obj, x_step=abs(a.number(a.r(d.get("XStep")))),
                        y_step=abs(a.number(a.r(d.get("YStep")))), bbox=a.rect_for(d, "BBox"))
    return True


def cell(rec: Record, tiling: Tiling, obj: PObj, ctx: Context) -> Cell:
    """The pattern stream parsed as CPDF_Form(doc, no page resources, stream) does it: the /Matrix
    as the objects' first CTM, the /BBox as their clip, the painted object's general state on top
    of default states, and the pattern's own /Resources reaching nothing else."""
    from dataclasses import replace

    from .content import Parser, _Run, initial_state
    key = _general(obj)
    cells = rec.cells
    got = cells.get(key)
    if got is None:
        objs: list[PObj] = []
        parser = Parser(rec.a.doc, {}, objs, ctx.fonts, {})
        state = replace(initial_state(), fill_alpha=F(obj.fill_alpha), stroke_alpha=F(obj.stroke_alpha),
                        blend=obj.blend, soft_mask=obj.smask is not None, smask=obj.smask,
                        smask_matrix=obj.smask_matrix, transfer=obj.transfer)
        run = _Run(parser, {}, state, (0.0, 0.0, 0.0, 0.0), None)
        try:
            run._form(tiling.stream, "")
        except RecursionError:
            pass
        holder = objs[0] if objs else None
        children = list(holder.children) if holder is not None else []
        every = [o for o in objs if o is not holder]
        transparency = form_transparency(holder, ctx) if holder is not None else (False, False)
        got = cells[key] = Cell(children=children, every=every, transparency=transparency, bbox=tiling.bbox,
                                colored=True, x_step=tiling.x_step, y_step=tiling.y_step)
    return got


def unsupported(rec: Record, obj: PObj, ctx: Context | None) -> str | None:
    """What a tiling-pattern fill or stroke of `obj` needs that this reader does not draw yet: the
    cell is a form, so its own content answers (a cell painted with its own pattern would keep
    parsing itself, which PDFium's cache stops one level lower: refused by name)."""
    if ctx is None:
        return "tiling patterns"
    tiling = rec.tiling
    if tiling is None:      # a loaded tiling pattern: never
        return None
    busy = ctx.tiling
    key = id(rec.obj)
    if key in busy:
        return "a tiling pattern painted with itself"
    busy.add(key)
    try:
        cel = cell(rec, tiling, obj, ctx)
        return unported(cel.every, ctx) or _huge_image(cel.every, ctx)
    finally:
        busy.discard(key)


def _huge_image(objs: list[PObj], ctx: Context) -> str | None:
    """DrawPatternBitmap renders the cell with bForceHalftone, and that flag alone keeps an image
    over kHugeImageSize from being resampled bilinearly (CPDF_ImageRenderer::StartDIBBase). The
    flag is not modelled, so such an image in a cell is refused rather than drawn differently."""
    probes = ctx.image_probes
    for o in objs:
        if o.type != OBJ_IMAGE:
            continue
        probe = probes.get(id(o.stream))
        dib = probe.dib if probe is not None else None
        if dib is not None and dib.bpp > 1 and dib.bpp // 8 * dib.w * dib.h > HUGE_IMAGE:
            return "a huge image in a tiling pattern"
    return None


# ---------------------------------------------------------------------- CPDF_RenderTiling::Draw


def _int_range(v: float) -> bool:
    """pdfium::IsValueInRangeForNumericType<int> of a float (NaN is out of range)."""
    return -2147483648.0 <= v <= 2147483647.0


def _ceilf(v: float) -> float:
    """std::ceil of a float, infinities and NaN left as they are (for the range checks)."""
    return float(math.ceil(v)) if math.isfinite(v) else v


def _floorf(v: float) -> float:
    return float(math.floor(v)) if math.isfinite(v) else v


def _checked_int(v: float) -> int | None:
    """CheckedFloatToInt."""
    return int(v) if _int_range(v) else None


def _match_rect(dest: FloatRect, src: FloatRect) -> Matrix:
    """CFX_Matrix::MatchRect of (left, bottom, right, top) rects."""
    diff = F(src[0] - src[2])
    a = 1.0 if abs(diff) < 0.001 else F(F(dest[0] - dest[2]) / diff)
    diff = F(src[1] - src[3])
    d = 1.0 if abs(diff) < 0.001 else F(F(dest[1] - dest[3]) / diff)
    return a, 0.0, 0.0, d, F(dest[0] - F(src[0] * a)), F(dest[1] - F(src[1] * d))


def _is_scaled(m: Matrix) -> bool:
    return abs(m[1] * 1000) < abs(m[0]) and abs(m[2] * 1000) < abs(m[3])


def _is_90_rotated(m: Matrix) -> bool:
    return abs(m[0] * 1000) < abs(m[1]) and abs(m[3] * 1000) < abs(m[2])


def draw(status: Status, obj: PObj, matrix: Matrix, rec: Record, stroke: bool) -> None:
    """CPDF_RenderStatus::DrawTilingPattern, the object's clip already selected by the caller."""
    dev = status.dev
    clip_box = dev.clip_box()
    if R.rect_empty(clip_box):
        return
    screen = _screen(status, obj, matrix, rec, clip_box)
    if screen is None:
        return
    # CompositeDIBitmap(screen, left, top, mask 0, alpha 1, Normal): SetDIBits
    set_dibits(dev, screen, "bgra", clip_box[0], clip_box[1], "Normal")


def _screen(status: Status, obj: PObj, matrix: Matrix, rec: Record, clip_box: IntRect) -> BGRA | None:
    """CPDF_RenderTiling::Draw: the BGRA bitmap over the clip box the tiles are stamped into, or
    None when the tiles were drawn onto the device one by one (or nothing is to be drawn)."""
    tiling = rec.tiling
    if tiling is None:      # a loaded tiling pattern: never
        return None
    cel = cell(rec, tiling, obj, status.ctx)
    p2d = R.concat(rec.pattern_to_form, matrix)
    cell_bbox = R.transform_rect(p2d, cel.bbox)
    ceil_h = _ceilf(F(cell_bbox[3] - cell_bbox[1]))
    ceil_w = _ceilf(F(cell_bbox[2] - cell_bbox[0]))
    if not _int_range(ceil_h) or not _int_range(ceil_w):
        return None
    width, height = max(int(ceil_w), 1), max(int(ceil_h), 1)
    x_step, y_step = cel.x_step, cel.y_step
    if not math.isfinite(x_step) or not math.isfinite(y_step) or x_step == 0.0 or y_step == 0.0:
        return None
    bbox = cel.bbox
    cbp = R.transform_rect(R.inverse(p2d), (float(clip_box[0]), float(clip_box[3]),
                                            float(clip_box[2]), float(clip_box[1])))
    min_col = _checked_int(_ceilf(F(F(cbp[0] - bbox[2]) / x_step)))
    max_col = _checked_int(_floorf(F(F(cbp[2] - bbox[0]) / x_step)))
    min_row = _checked_int(_ceilf(F(F(cbp[1] - bbox[3]) / y_step)))
    max_row = _checked_int(_floorf(F(F(cbp[3] - bbox[1]) / y_step)))
    if min_col is None or max_col is None or min_row is None or max_row is None:
        return None
    if height > INT_MAX // width:
        return None
    clip_w, clip_h = clip_box[2] - clip_box[0], clip_box[3] - clip_box[1]
    if width > clip_w or height > clip_h or width * height > clip_w * clip_h:
        _draw_tiles(status, obj, cel, matrix, p2d, (min_col, max_col, min_row, max_row))
        return None
    aligned = (bbox[0] == 0.0 and bbox[1] == 0.0 and bbox[2] == x_step and bbox[3] == y_step
               and (_is_scaled(p2d) or _is_90_rotated(p2d)))
    if aligned:
        orig_x, orig_y = roundf(p2d[4]), roundf(p2d[5])
        min_col = cdiv(clip_box[0] - orig_x, width) - (1 if clip_box[0] < orig_x else 0)
        max_col = cdiv(clip_box[2] - orig_x, width) - (1 if clip_box[2] <= orig_x else 0)
        min_row = cdiv(clip_box[1] - orig_y, height) - (1 if clip_box[1] < orig_y else 0)
        max_row = cdiv(clip_box[3] - orig_y, height) - (1 if clip_box[3] <= orig_y else 0)
    left_offset = F(cell_bbox[0] - p2d[4])
    top_offset = F(cell_bbox[1] - p2d[5])
    tile: Pixels | None
    if width * height < 16:
        big = _cell_bitmap(status, rec, cel, matrix, 8, 8)
        tile = _stretched(big, width, height)
    else:
        tile = _cell_bitmap(status, rec, cel, matrix, width, height)
    if tile is None:
        return None
    screen = np.zeros((clip_h, clip_w, 4), np.uint8)
    for col in range(min_col, max_col + 1):
        for row in range(min_row, max_row + 1):
            if aligned:
                start_x = roundf(p2d[4]) + col * width - clip_box[0]
                start_y = roundf(p2d[5]) + row * height - clip_box[1]
            else:
                ox, oy = R.transform(p2d, F(col * x_step), F(row * y_step))
                start_x = roundf(F(ox + left_offset)) - clip_box[0]
                start_y = roundf(F(oy + top_offset)) - clip_box[1]
                if not (-INT_MAX <= start_x <= INT_MAX and -INT_MAX <= start_y <= INT_MAX):
                    return None
            if width == 1 and height == 1:
                if start_x < 0 or start_x >= clip_w or start_y < 0 or start_y >= clip_h:
                    continue
                screen[start_y, start_x] = tile[0, 0]
            else:
                composite_bitmap(screen, "bgra", start_x, start_y, tile, "bgra", 0, 0, "Normal", width, height)
    return screen


def _draw_tiles(status: Status, obj: PObj, cel: Cell, matrix: Matrix, p2d: Matrix,
                span: tuple[int, int, int, int]) -> None:
    """The branch for a cell bigger than the clip: the cell's objects drawn again per tile, each
    with mtObj2Device moved to that tile's origin (`span`: the first and last column and row). A
    coloured pattern painted on a path hands the tiles default states carrying the path's own fill
    alpha (CloneObjStates is for the uncoloured ones): a form in the cell is then drawn through
    ProcessTransparency and multiplied by it."""
    min_col, max_col, min_row, max_row = span
    dev = status.dev
    initial = F(obj.fill_alpha) if obj.type == OBJ_PATH else 1.0
    a, b, c, d, e, f = matrix
    for col in range(min_col, max_col + 1):
        for row in range(min_row, max_row + 1):
            ox, oy = R.transform(p2d, F(col * cel.x_step), F(row * cel.y_step))
            m = (a, b, c, d, F(e + F(ox - p2d[4])), F(f + F(oy - p2d[5])))
            dev.save()
            tile = Status(dev, cel.transparency, False, initial, status.ctx, status.stop)
            tile.render_list(cel.children, m)
            dev.restore(False)


def _cell_bitmap(status: Status, rec: Record, cel: Cell, matrix: Matrix, width: int, height: int) -> BGRA:
    """DrawPatternBitmap: the cell drawn into a BGRA bitmap of its own, through a render context
    of its own (the page's resources out of reach), the cell box matched to the bitmap."""
    dev = Device(width, height, True, None)
    box = R.transform_rect(matrix, R.transform_rect(rec.pattern_to_form, cel.bbox))
    adjust = _match_rect((0.0, 0.0, float(width), float(height)), box)
    m = R.concat(matrix, adjust)
    ctx = status.ctx
    # a CPDF_RenderContext of its own over the pattern form alone, and render options of its
    # own: the colour mode starts at kNormal, so a coloured cell inside an alpha soft mask is
    # drawn in colour, and no status under it carries the mask's flags
    cell_ctx = Context(ctx.doc, {}, ctx.fonts, False)
    cell_ctx.page_matrix, cell_ctx.page_objects = m, cel.children
    cell_status = Status(dev, cel.transparency, False, 1.0, cell_ctx, None)
    dev.save()
    cell_status.render_list(cel.children, m)
    dev.restore(False)
    return dev.bgra


@dataclass(frozen=True, kw_only=True)
class _Bitmap:
    """A rendered cell as CFX_DIBBase::StretchTo reads it (render_image.Stretchable)."""
    rows: Pixels
    fmt: str
    palette: Sequence[int] | None
    w: int
    h: int


def _stretched(bgra: BGRA, width: int, height: int) -> Pixels | None:
    """StretchTo(width, height) of a cell under 16 pixels (PDFium draws it at 8 x 8 first)."""
    h, w = bgra.shape[:2]
    got = stretch(_Bitmap(rows=bgra, fmt="bgra", palette=None, w=w, h=h), width, height, (0, 0, width, height),
                  False)
    if got is None:
        return None
    block, fmt, _ = got
    if fmt != "bgra" or block.shape[:2] != (height, width):
        return None
    return block
