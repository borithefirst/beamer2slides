"""Content streams -> page objects, the way PDFium's CPDF_StreamContentParser builds them.

What is kept is what the backend contract reports: each object's type, its own matrix (PDFium's:
the text matrix with the text position, a path's CTM, an image's CTM, a form's CTM), its clip
boxes, colours, alphas, line width, its bounding rectangle (CPDF_PageObject::GetRect), and per
type the path points, the text items or the image/form stream. Coordinates are PDF user space of
the *container* (the page, or the form the object is drawn in), y up, as in PDFium.

The rules follow PDFium closely, quirks included (a clip is applied after the path that sets it
is painted, `h` on a closed point only flags it, a TJ with no strings moves by its kerning without
the horizontal scale, a Type 3 font always fills...), because the pipeline's thresholds were
tuned on what PDFium reports."""

from __future__ import annotations

import copy
import math
import struct
from dataclasses import dataclass, field

from ..api import OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT
from .colors import DEVICE, PATTERN, ColorSpace, load_colorspace
from .fonts import Font, load_font
from .raster import concat, path_is_rect
from .syntax import F32X2, F32X3, F32X4, F32X6, InlineImage, Name, Ref, Stream, String, float32 as f32, operations

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# Several floats rounded in one pack/unpack (syntax.F32X*); `pack` refuses what `f32` makes an
# infinity, and the one-at-a-time rounding answers instead.
_p2, _u2 = F32X2.pack, F32X2.unpack
_p3, _u3 = F32X3.pack, F32X3.unpack
_p4, _u4 = F32X4.pack, F32X4.unpack
_p6, _u6 = F32X6.pack, F32X6.unpack


def f32m(m: tuple) -> tuple:
    """A CFX_Matrix: six floats."""
    try:
        return _u6(_p6(*m))
    except (OverflowError, TypeError, struct.error):
        return tuple(f32(v) for v in m)


def f32p(p: tuple) -> tuple:
    """A CFX_PointF."""
    try:
        return _u2(_p2(p[0], p[1]))
    except (OverflowError, TypeError, struct.error):
        return f32(p[0]), f32(p[1])
PT_MOVE, PT_LINE, PT_BEZIER = 2, 0, 1   # api.SEG_* values
FILL_NONE, FILL_EVENODD, FILL_WINDING = 0, 1, 2   # FPDFPath_GetDrawMode
MAX_FORM_LEVEL = 40
WHITE = 0xFFFFFF


def transform(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def transform_rect(m, rect):
    """CFX_Matrix::TransformRect of (left, bottom, right, top), in float: corners in its order,
    std::min/max keeping the first of equals (and a NaN out, unless it comes first)."""
    l, b, r, t = rect
    pts = [transform32(m, x, y) for x, y in ((l, t), (l, b), (r, t), (r, b))]
    right = left = pts[0][0]
    top = bottom = pts[0][1]
    for x, y in pts[1:]:
        right, left = (x if right < x else right), (x if x < left else left)
        top, bottom = (y if top < y else top), (y if y < bottom else bottom)
    return left, bottom, right, top


def intersect(a, b):
    """CFX_FloatRect::Intersect: an empty result collapses to zero."""
    l, bt, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if l > r or bt > t:
        return 0.0, 0.0, 0.0, 0.0
    return l, bt, r, t


def point_bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def is_identity(m) -> bool:
    return tuple(m) == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def transform32(m, x, y):
    """CFX_Matrix::Transform in float, one rounding per operation."""
    a, b, c, d, e, f = m
    try:
        ax, cy, bx, dy = _u4(_p4(a * x, c * y, b * x, d * y))
        sx, sy = _u2(_p2(ax + cy, bx + dy))
        return _u2(_p2(sx + e, sy + f))
    except OverflowError:
        return f32(f32(f32(a * x) + f32(c * y)) + e), f32(f32(f32(b * x) + f32(d * y)) + f)


def rect_points(left, bottom, right, top) -> tuple:
    """CFX_Path::AppendRect."""
    return ((left, bottom, PT_MOVE, False), (left, top, PT_LINE, False), (right, top, PT_LINE, False),
            (right, bottom, PT_LINE, False), (left, bottom, PT_LINE, True))


def append_clip(clip_paths: tuple, points: tuple, fill_type: int) -> tuple:
    """CPDF_ClipPath::AppendPathWithAutoMerge: a new path inside the last one, when that one is
    a rectangle, replaces it."""
    if clip_paths:
        old = clip_paths[-1][0]
        if path_is_rect(old):
            ox0, ox1 = sorted((old[0][0], old[2][0]))
            oy0, oy1 = sorted((old[0][1], old[2][1]))
            nx0, ny0, nx1, ny1 = point_bbox(points) if points else (0.0, 0.0, 0.0, 0.0)
            if nx0 >= ox0 and nx1 <= ox1 and ny0 >= oy0 and ny1 <= oy1:
                clip_paths = clip_paths[:-1]
    return clip_paths + ((points, fill_type),)


MAX_CLIP_TEXTS = 1024       # CPDF_ClipPath::AppendTexts' kMaxTextObjects


def append_texts(clip_texts: tuple, texts: list) -> tuple:
    """CPDF_ClipPath::AppendTexts: one BT..ET's clip-mode texts and a None closing the group
    (ProcessClipPath clips once per group), unless the list would pass 1024 entries: then none
    (the reference is made private all the same, which only a renderer comparing clips sees)."""
    if len(clip_texts) + len(texts) <= MAX_CLIP_TEXTS:
        return clip_texts + tuple(texts) + (None,)
    return tuple(clip_texts)


# ---------------------------------------------------------------------- page objects


@dataclass(eq=False)
class PObj:
    type: int
    matrix: tuple                  # PDFium's matrix for the object (identity for a shading)
    parent: "PObj | None" = None
    clips: list | None = None      # point bounding boxes of the clip paths, container space
    clip_paths: tuple = ()         # ((points, fill type), ...) in container space, float32 (render)
    clip_texts: tuple = ()         # CPDF_ClipPath's text list: text PObj copies, None ends a group
    fill: int | None = None       # 0xRRGGBB; None when the object has no colour state
    stroke: int | None = None
    fill_alpha: float = 1.0
    stroke_alpha: float = 1.0
    blend: str = "Normal"
    soft_mask: bool = False
    line_width: float = 1.0
    line_cap: int = 0
    line_join: int = 0
    miter: float = 10.0
    dash: tuple = ()
    dash_phase: float = 0.0
    smask: dict | None = None      # the ExtGState's /SMask dictionary (render)
    smask_matrix: tuple = IDENTITY  # the CTM when it was set
    transfer: object = None        # /TR or /TR2 (not a name)
    pattern: bool = False          # a pattern colour space for fill or stroke (render)
    # render_shading: the pattern each side paints with (None: no pattern colour; False: a pattern
    # colour space without a pattern), and a shading object's CTM and pattern record
    fill_pattern: object = None
    stroke_pattern: object = None
    shading_matrix: tuple = IDENTITY
    shading_record: object = None
    rect: tuple = (0.0, 0.0, 0.0, 0.0)   # GetRect, container space
    # paths
    points: list = field(default_factory=list)   # (x, y, PT_*, closes), path space
    fill_type: int = FILL_NONE
    stroked: bool = False
    # text
    font: Font | None = None
    font_size: float = 0.0
    items: list = field(default_factory=list)    # [code, x] per character (x in text space)
    kernings: list = field(default_factory=list)  # TJ kerning after each character (0 inside a string)
    text_mode: int = 0
    char_space: float = 0.0
    word_space: float = 0.0
    text_ctm: tuple = (1.0, 0.0, 0.0, 1.0)   # CPDF_TextState's CTM: (a, c, b, d), stroke modes only
    original_rect: tuple = (0.0, 0.0, 0.0, 0.0)
    # images and forms
    stream: object = None          # Stream, or InlineImage
    name: str = ""
    resources: object = None       # an inline image's: its colour space is looked up there
    children: list = field(default_factory=list)
    group: bool = False            # a form with a transparency group (/Group /S /Transparency)
    active: bool = True

    @property
    def has_transparency(self) -> bool:
        """FPDFPageObj_HasTransparency."""
        if self.blend != "Normal" or self.soft_mask or self.fill_alpha != 1.0:
            return True
        if self.type == OBJ_PATH and self.stroke_alpha != 1.0:
            return True
        return self.type == OBJ_FORM and self.group


# ---------------------------------------------------------------------- graphics state


@dataclass
class State:
    ctm: tuple = IDENTITY
    clips: tuple = ()                  # point bboxes (container space), tuple so copies are cheap
    clip_paths: tuple = ()             # CPDF_ClipPath's paths: ((points, fill type), ...)
    clip_texts: tuple = ()             # and its texts (append_texts)
    fill_cs: ColorSpace = DEVICE["DeviceGray"]
    fill_values: tuple = (0.0,)
    fill_ref: int = 0
    stroke_cs: ColorSpace = DEVICE["DeviceGray"]
    stroke_values: tuple = (0.0,)
    stroke_ref: int = 0
    fill_alpha: float = 1.0
    stroke_alpha: float = 1.0
    blend: str = "Normal"
    soft_mask: bool = False
    line_width: float = 1.0
    line_cap: int = 0
    line_join: int = 0
    miter: float = 10.0
    dash: tuple = ()
    dash_phase: float = 0.0
    smask: dict | None = None
    smask_matrix: tuple = IDENTITY
    transfer: object = None
    font: Font | None = None
    font_size: float = 0.0
    char_space: float = 0.0
    word_space: float = 0.0
    horz_scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0
    text_mode: int = 0
    # text positioning (not saved by q/Q in PDFium's parser either: it lives on cur_states_,
    # which q copies, so it is saved - kept here for the same effect)
    text_matrix: tuple = IDENTITY
    text_pos: tuple = (0.0, 0.0)
    text_line_pos: tuple = (0.0, 0.0)
    # render_shading: the colour state's patterns (see PObj) and the parser's parent matrix
    fill_pattern: object = None
    stroke_pattern: object = None
    parent_matrix: tuple = IDENTITY
    # whether the colour is set (CPDF_Color::IsNull): a Type 3 glyph starts without one, which
    # the renderer replaces with the text's fill colour (render_type3)
    fill_set: bool = True
    stroke_set: bool = True

    def copy(self) -> "State":
        return State(**self.__dict__)


def _num(v, default=0.0) -> float:
    """GetNumber: an integer operand as a float (16777217 -> 16777216), a real as it is."""
    if isinstance(v, float):
        return v
    return f32(float(v)) if isinstance(v, int) and not isinstance(v, bool) else default


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


PATH_FAST = frozenset(("m", "l", "c", "v", "y", "h", "re"))


class Parser:
    """One page's (or form's) content, appended to `objects` in painting order."""

    def __init__(self, doc, page_resources: dict, objects: list, fonts: dict, colorspaces: dict):
        self.doc = doc
        self.page_resources = page_resources if isinstance(page_resources, dict) else {}
        self.objects = objects
        self.fonts = fonts
        self.colorspaces = colorspaces
        self.parsed: list = []          # streams being parsed (the recursion chain)

    # ------------------------------------------------------------------ entry points

    def parse_page(self, contents: bytes, bbox: tuple) -> None:
        start = len(self.objects)
        self._run(contents, self.page_resources, State(), bbox, None)
        check_clip([o for o in self.objects[start:] if o.parent is None])

    def _run(self, data: bytes, resources: dict, state: State, bbox: tuple, parent: PObj | None) -> None:
        run = _Run(self, resources if isinstance(resources, dict) else self.page_resources, state, bbox, parent)
        run.execute(data)

    # ------------------------------------------------------------------ resources

    def font(self, obj) -> Font | None:
        d = self.doc.resolve(obj)
        if not isinstance(d, dict):
            return None
        key = id(d)
        if key not in self.fonts:
            self.fonts[key] = load_font(self.doc, d)
        return self.fonts[key]

    def stock_font(self) -> Font | None:
        """CPDF_Font::GetStockFont(kDefaultAnsiFontName): a Type1 Helvetica with no file."""
        if "stock" not in self.fonts:
            self.fonts["stock"] = load_font(self.doc, {Name("Type"): Name("Font"), Name("Subtype"): Name("Type1"),
                                                       Name("BaseFont"): Name("Helvetica")})
        return self.fonts["stock"]


class _Run:
    """The state of one content stream being executed."""

    def __init__(self, parser: Parser, resources: dict, state: State, bbox: tuple, parent: PObj | None):
        self.p = parser
        self.doc = parser.doc
        self.resources = resources
        self.state = state
        self.stack: list[State] = []
        self.bbox = bbox
        self.parent = parent
        self.path: list = []
        self.path_start = (0.0, 0.0)
        self.path_current = (0.0, 0.0)
        self.clip_type = FILL_NONE
        self.last_image_name = None
        self.last_image = None
        self.clip_text_list: list = []   # clip_text_list_: this stream's clip-mode texts since ET

    # ------------------------------------------------------------------ resources

    def _direct(self, value):
        """CPDF_Object::GetDirect: one reference followed; one leading to another reference is nothing."""
        if isinstance(value, Ref):
            value = self.doc.get(value.num)
            return None if isinstance(value, Ref) else value
        return value

    def resource(self, category: str, name) -> object:
        """FindResourceObj: the name in FindResourceHolder's dictionary for the category - this stream's
        own when it has one (even without the name: the page's is not asked then), else the page's."""
        holder = None
        for res in (self.resources, self.p.page_resources):
            if not isinstance(res, dict):
                continue
            group = self._direct(res.get(category))
            holder = group.dict if isinstance(group, Stream) else group
            if isinstance(holder, dict) or res is self.p.page_resources:
                break
        if not isinstance(holder, dict) or name is None:
            return None
        return self._direct(holder.get(str(name)))

    def colorspace(self, name) -> ColorSpace | None:
        name = str(name)
        if name == "Pattern":
            return PATTERN
        if name in ("DeviceGray", "DeviceRGB", "DeviceCMYK", "G", "RGB", "CMYK"):
            return load_colorspace(self.doc, Name(name), self.resources)
        obj = self.resource("ColorSpace", name)
        if obj is None:
            return None
        key = id(obj)
        if key not in self.p.colorspaces:
            self.p.colorspaces[key] = load_colorspace(self.doc, obj, None)
        return self.p.colorspaces[key]

    def _inline_components(self, cs):
        """Handle_BeginImage's colour space object for ReadInlineStream: a name other than the three
        device ones is looked up in the resources (None: not there, and the data is then read as
        one bit per pixel), and GetColorSpace(obj, nullptr) gives the component count, 3 when it
        does not load."""
        if isinstance(cs, Name) and str(cs) not in ("DeviceRGB", "DeviceGray", "DeviceCMYK"):
            cs = self.resource("ColorSpace", cs)
            if cs is None:
                return None
        try:
            space = load_colorspace(self.doc, cs, None)
        except Exception:  # noqa: BLE001 - a colour space that does not load
            space = None
        return space.n if space is not None else 3

    # ------------------------------------------------------------------ objects

    def add(self, obj: PObj, color: bool, graph: bool) -> PObj:
        """SetGraphicStates + AppendPageObject."""
        s = self.state
        obj.parent = self.parent
        obj.clips = list(s.clips) if s.clips else None
        obj.clip_paths = s.clip_paths
        obj.clip_texts = s.clip_texts
        obj.fill_alpha, obj.stroke_alpha = s.fill_alpha, s.stroke_alpha
        obj.blend, obj.soft_mask = s.blend, s.soft_mask
        obj.smask, obj.smask_matrix, obj.transfer = s.smask, s.smask_matrix, s.transfer
        if color:
            obj.fill = s.fill_ref if s.fill_set else None
            obj.stroke = s.stroke_ref if s.stroke_set else None
            obj.pattern = s.fill_cs.is_pattern or s.stroke_cs.is_pattern
            obj.fill_pattern = s.fill_pattern if s.fill_cs.is_pattern else None
            obj.stroke_pattern = s.stroke_pattern if s.stroke_cs.is_pattern else None
        if graph:
            obj.line_width, obj.line_cap, obj.line_join, obj.miter = s.line_width, s.line_cap, s.line_join, s.miter
            obj.dash, obj.dash_phase = s.dash, s.dash_phase
        self.p.objects.append(obj)
        if self.parent is not None:
            self.parent.children.append(obj)
        return obj

    # ------------------------------------------------------------------ execution

    def execute(self, data: bytes) -> None:
        fast = None  # ParsePathObject's params while in its fast path
        for op, args in operations(data, self._inline_components):
            if fast is not None:
                raw = getattr(args, "raw", args)  # the fast path reads numbers, not the buffer
                if op in PATH_FAST and all(_is_number(a) for a in raw):
                    self._fast_path(op, raw, fast)
                    continue
                fast = None
            handler = OPS.get(op)
            if handler is None:
                continue
            try:
                handler(self, args)
            except (TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError, KeyError):
                continue  # malformed operands: PDFium reads zeros or skips; nothing is drawn wrongly
            if op == "m" and len(args) == 2:
                fast = [0.0] * 6

    def _fast_path(self, op: str, args: list, params: list) -> None:
        """CPDF_StreamContentParser::ParsePathObject, entered after a valid `m`: path operators
        take the *first* numbers read (up to six; more are dropped), without counting them - a
        missing one is whatever the previous operator left in `params` (zeros at first) - until
        anything but a number or a path operator hands back to the normal parser."""
        for k, a in enumerate(args[:6]):
            params[k] = _num(a)
        p = params
        if op == "m":
            self._point(p[0], p[1], PT_MOVE)
        elif op == "l":
            self._point(p[0], p[1], PT_LINE)
        elif op == "c":
            self._point(p[0], p[1], PT_BEZIER)
            self._point(p[2], p[3], PT_BEZIER)
            self._point(p[4], p[5], PT_BEZIER)
        elif op == "v":
            self._point(*self.path_current, PT_BEZIER)
            self._point(p[0], p[1], PT_BEZIER)
            self._point(p[2], p[3], PT_BEZIER)
        elif op == "y":
            self._point(p[0], p[1], PT_BEZIER)
            self._point(p[2], p[3], PT_BEZIER)
            self._point(p[2], p[3], PT_BEZIER)
        elif op == "h":
            self.op_h([])
        else:  # re
            self.op_re(p[:4])

    @staticmethod
    def number(args, i: int) -> float:
        """GetNumber(i): the i-th operand from the end, 0 when missing."""
        return _num(args[-1 - i]) if i < len(args) else 0.0

    def numbers(self, args, n: int) -> list[float]:
        return [self.number(args, n - 1 - k) for k in range(n)]

    # ---- graphics state
    def op_q(self, args):
        self.stack.append(self.state.copy())

    def op_Q(self, args):
        if self.stack:
            self.state = self.stack.pop()

    def op_cm(self, args):
        m = tuple(self.numbers(args, 6))
        # CFX_Matrix::operator* in float: rounding after every step, not once at the end
        self.state.ctm = concat(f32m(m), self.state.ctm)
        self._text_matrix_changed()

    def op_w(self, args):
        self.state.line_width = self.number(args, 0)

    def op_J(self, args):
        self.state.line_cap = int(self.number(args, 0))

    def op_j(self, args):
        self.state.line_join = int(self.number(args, 0))

    def op_M(self, args):
        self.state.miter = self.number(args, 0)

    def op_d(self, args):
        dash = args[-2] if len(args) >= 2 else None
        if not isinstance(dash, list):
            return
        self.state.dash = tuple(_num(self.doc.resolve(v)) for v in dash)
        self.state.dash_phase = self.number(args, 0)

    def op_gs(self, args):
        gs = self.resource("ExtGState", args[-1]) if args else None
        if not isinstance(gs, dict):
            return
        r = self.doc.resolve
        s = self.state
        for key, value in gs.items():
            value = r(value)
            if key == "LW":
                s.line_width = _num(value)
            elif key == "LC":
                s.line_cap = int(_num(value))
            elif key == "LJ":
                s.line_join = int(_num(value))
            elif key == "ML":
                s.miter = _num(value)
            elif key == "Font" and isinstance(value, list) and len(value) >= 2:
                s.font_size = _num(r(value[1]))
                s.font = self.p.font(value[0])
            elif key == "BM":
                mode = r(value[0]) if isinstance(value, list) and value else value
                s.blend = str(mode) if isinstance(mode, Name) and str(mode) != "Compatible" else "Normal"
            elif key == "SMask":
                s.soft_mask = isinstance(value, dict)
                s.smask = value if isinstance(value, dict) else None
                if s.smask is not None:
                    s.smask_matrix = s.ctm
            elif key == "D" and isinstance(value, list) and value and isinstance(r(value[0]), list):
                s.dash = tuple(_num(r(v)) for v in r(value[0]))
                s.dash_phase = _num(r(value[1])) if len(value) > 1 else 0.0
            elif key == "TR2" or key == "TR" and "TR2" not in gs:
                s.transfer = None if isinstance(value, Name) else value
            elif key == "CA":
                s.stroke_alpha = min(1.0, max(0.0, _num(value, 1.0)))
            elif key == "ca":
                s.fill_alpha = min(1.0, max(0.0, _num(value, 1.0)))

    # ---- colour
    def _set_color(self, fill: bool, cs: ColorSpace | None, values: list[float]) -> None:
        """CPDF_ColorState::SetColor."""
        s = self.state
        if fill:
            s.fill_set = True
        else:
            s.stroke_set = True
        current = s.fill_cs if fill else s.stroke_cs
        if cs is not None:
            current = cs
            if fill:
                s.fill_pattern = None
            else:
                s.stroke_pattern = None
        if current.n > len(values):
            if fill:
                s.fill_cs = current
            else:
                s.stroke_cs = current
            return
        if current.is_pattern:
            ref = None
        else:
            ref = current.colorref(values)
        ref = WHITE if ref is None else ref
        if fill:
            s.fill_cs, s.fill_values, s.fill_ref = current, tuple(values), ref
        else:
            s.stroke_cs, s.stroke_values, s.stroke_ref = current, tuple(values), ref

    def op_g(self, args):
        self._set_color(True, DEVICE["DeviceGray"], self.numbers(args, 1))

    def op_G(self, args):
        self._set_color(False, DEVICE["DeviceGray"], self.numbers(args, 1))

    def op_rg(self, args):
        if len(args) == 3:
            self._set_color(True, DEVICE["DeviceRGB"], self.numbers(args, 3))

    def op_RG(self, args):
        if len(args) == 3:
            self._set_color(False, DEVICE["DeviceRGB"], self.numbers(args, 3))

    def op_k(self, args):
        if len(args) == 4:
            self._set_color(True, DEVICE["DeviceCMYK"], self.numbers(args, 4))

    def op_K(self, args):
        if len(args) == 4:
            self._set_color(False, DEVICE["DeviceCMYK"], self.numbers(args, 4))

    def _set_space(self, fill: bool, args):
        cs = self.colorspace(args[-1]) if args else None
        if cs is None:
            return
        # CPDF_Color::SetColorSpace: the initial colour of the space; the colour reference stays
        if fill:
            self.state.fill_cs, self.state.fill_values = cs, tuple(cs.initial())
            self.state.fill_pattern = False if cs.is_pattern else None
            self.state.fill_set = True
        else:
            self.state.stroke_cs, self.state.stroke_values = cs, tuple(cs.initial())
            self.state.stroke_pattern = False if cs.is_pattern else None
            self.state.stroke_set = True

    def op_cs(self, args):
        self._set_space(True, args)

    def op_CS(self, args):
        self._set_space(False, args)

    def _colors(self, args) -> list[float]:
        n = min(len(args), 4)
        return self.numbers(args, n)

    def op_sc(self, args):
        if args:
            self._set_color(True, None, self._colors(args))
        else:
            self.state.fill_set = True      # SetColor with no values: the colour is no longer null

    def op_SC(self, args):
        if args:
            self._set_color(False, None, self._colors(args))
        else:
            self.state.stroke_set = True

    def _set_pattern(self, fill: bool, args):
        if not args:
            return
        if not isinstance(args[-1], Name):
            self._set_color(fill, None, self._colors(args))
            return
        pattern = self.resource("Pattern", args[-1])
        if pattern is None:
            return
        values = [_num(a) for a in args[:-1]][-4:]
        s = self.state
        from .render_shading import find_pattern
        record = find_pattern(self.p, pattern, s.parent_matrix)
        side = s.fill_pattern if fill else s.stroke_pattern
        if record is None:
            # PDFium finds no pattern and leaves the colour as it was, which this parser does not
            record = side if side is not None else "?"
        if fill:
            s.fill_pattern, s.fill_set = record, True
        else:
            s.stroke_pattern, s.stroke_set = record, True
        cs = s.fill_cs if fill else s.stroke_cs
        if not cs.is_pattern:
            cs = PATTERN
        ref = None
        if cs.base is not None and len(values) >= cs.base.n:
            ref = cs.base.colorref(values)
        if ref is None:
            d = pattern.dict if isinstance(pattern, Stream) else pattern
            colored = isinstance(d, dict) and self.doc.resolve(d.get("PatternType")) == 1 \
                and self.doc.resolve(d.get("PaintType")) == 1
            ref = 0xBFBFBF if colored else WHITE
        if fill:
            s.fill_cs, s.fill_values, s.fill_ref = cs, tuple(values), ref
        else:
            s.stroke_cs, s.stroke_values, s.stroke_ref = cs, tuple(values), ref

    def op_scn(self, args):
        self._set_pattern(True, args)

    def op_SCN(self, args):
        self._set_pattern(False, args)

    # ---- path construction
    def _point(self, x, y, kind):
        """AddPathPoint."""
        pt = (x, y)
        path = self.path
        if kind == PT_MOVE and path and not path[-1][3] and path[-1][2] == PT_MOVE and self.path_current == pt:
            return
        self.path_current = pt
        if kind == PT_MOVE:
            self.path_start = pt
            if path and path[-1][2] == PT_MOVE and not path[-1][3]:
                path[-1] = (x, y, PT_MOVE, False)
                return
        elif not path:
            return
        path.append((x, y, kind, False))

    def _point_close(self, x, y, kind):
        self.path_current = (x, y)
        if self.path:
            self.path.append((x, y, kind, True))

    def op_m(self, args):
        if len(args) == 2:
            self._point(self.number(args, 1), self.number(args, 0), PT_MOVE)

    def op_l(self, args):
        if len(args) == 2:
            self._point(self.number(args, 1), self.number(args, 0), PT_LINE)

    def op_c(self, args):
        n = self.numbers(args, 6)
        self._point(n[0], n[1], PT_BEZIER)
        self._point(n[2], n[3], PT_BEZIER)
        self._point(n[4], n[5], PT_BEZIER)

    def op_v(self, args):
        n = self.numbers(args, 4)
        self._point(*self.path_current, PT_BEZIER)
        self._point(n[0], n[1], PT_BEZIER)
        self._point(n[2], n[3], PT_BEZIER)

    def op_y(self, args):
        n = self.numbers(args, 4)
        self._point(n[0], n[1], PT_BEZIER)
        self._point(n[2], n[3], PT_BEZIER)
        self._point(n[2], n[3], PT_BEZIER)

    def op_h(self, args):
        if not self.path:
            return
        if self.path_start != self.path_current:
            self._point_close(*self.path_start, PT_LINE)
        else:
            x, y, kind, _ = self.path[-1]
            self.path[-1] = (x, y, kind, True)

    def op_re(self, args):
        x, y, w, h = self.numbers(args, 4)
        right, top = f32(x + w), f32(y + h)
        self._point(x, y, PT_MOVE)
        self._point(right, y, PT_LINE)
        self._point(right, top, PT_LINE)
        self._point(x, top, PT_LINE)
        self._point_close(x, y, PT_LINE)

    def op_W(self, args):
        self.clip_type = FILL_WINDING

    def op_Wstar(self, args):
        self.clip_type = FILL_EVENODD

    # ---- path painting
    def _paint(self, fill_type: int, stroke: bool, close: bool = False):
        if close:
            self.op_h([])
        points, self.path = self.path, []
        clip_type, self.clip_type = self.clip_type, FILL_NONE
        if not points:
            return
        s = self.state
        if len(points) == 1:
            if clip_type != FILL_NONE:
                s.clips = s.clips + ((0.0, 0.0, 0.0, 0.0),)  # AppendRect(0, 0, 0, 0), not transformed
                s.clip_paths = append_clip(s.clip_paths, rect_points(0.0, 0.0, 0.0, 0.0), FILL_WINDING)
                return
            x, y, kind, closes = points[0]
            if kind != PT_MOVE or not closes or s.line_cap != 1:
                return
            # a round-capped move closed at once: a dot, drawn from the point to itself
            points[0] = (x, y, kind, False)
            points.append((x, y, PT_LINE, True))
        if points[-1][2] == PT_MOVE and not points[-1][3]:
            points.pop()
        matrix = s.ctm
        if stroke or fill_type != FILL_NONE:
            obj = PObj(OBJ_PATH, matrix, points=points, fill_type=fill_type, stroked=stroke)
            self.add(obj, True, True)
            obj.rect = path_rect(obj)
        if clip_type != FILL_NONE:
            pts = [transform(matrix, x, y) for x, y, _, _ in points]
            box = point_bbox(pts) if pts else (0.0, 0.0, 0.0, 0.0)
            s.clips = s.clips + (box,)
            if not is_identity(matrix):
                points = [(*transform32(matrix, x, y), k, c) for x, y, k, c in points]
            s.clip_paths = append_clip(s.clip_paths, tuple(points), clip_type)

    def op_f(self, args):
        self._paint(FILL_WINDING, False)

    op_F = op_f

    def op_fstar(self, args):
        self._paint(FILL_EVENODD, False)

    def op_S(self, args):
        self._paint(FILL_NONE, True)

    def op_s(self, args):
        self._paint(FILL_NONE, True, close=True)

    def op_B(self, args):
        self._paint(FILL_WINDING, True)

    def op_Bstar(self, args):
        self._paint(FILL_EVENODD, True)

    def op_b(self, args):
        self._paint(FILL_WINDING, True, close=True)

    def op_bstar(self, args):
        # Handle_CloseEOFillStrokePath: unlike b and s, a closing line to the start always
        # (a lone `m` then paints a dot)
        self._point_close(*self.path_start, PT_LINE)
        self._paint(FILL_EVENODD, True)

    def op_n(self, args):
        self._paint(FILL_NONE, False)

    # ---- text state
    def _text_matrix_changed(self):
        pass  # the text object's matrix is computed when it is made (OnChangeTextMatrix's result)

    def op_BT(self, args):
        s = self.state
        s.text_matrix = IDENTITY
        s.text_pos = s.text_line_pos = (0.0, 0.0)

    def op_ET(self, args):
        """Handle_EndText: the clip-mode texts shown since the last ET join the clip path, if the
        mode is still a clip mode now."""
        if not self.clip_text_list:
            return
        s = self.state
        if s.text_mode >= 4:
            s.clip_texts = append_texts(s.clip_texts, self.clip_text_list)
        self.clip_text_list = []

    def op_Tc(self, args):
        self.state.char_space = self.number(args, 0)

    def op_Tw(self, args):
        self.state.word_space = self.number(args, 0)

    def op_Tz(self, args):
        if len(args) == 1:
            self.state.horz_scale = f32(self.number(args, 0) / 100)

    def op_TL(self, args):
        self.state.leading = self.number(args, 0)

    def op_Tr(self, args):
        # SetTextRenderingModeFromInt: a mode outside 0..7 leaves the current one
        mode = int(self.number(args, 0))
        if 0 <= mode <= 7:
            self.state.text_mode = mode

    def op_Ts(self, args):
        self.state.rise = self.number(args, 0)

    def op_Tf(self, args):
        s = self.state
        s.font_size = self.number(args, 0)
        # FindFont: no font dictionary under the name (a stream is none either) is the stock Helvetica;
        # a dictionary no font loads from leaves the current font as it was.
        name = args[-2] if len(args) >= 2 else None
        font_dict = self.resource("Font", name)
        font = self.p.font(font_dict) if isinstance(font_dict, dict) else self.p.stock_font()
        if font is not None:
            s.font = font
            if font.is_type3:
                font.check_metrics()

    def op_Td(self, args):
        s = self.state
        x, y = self.number(args, 1), self.number(args, 0)
        s.text_line_pos = (f32(s.text_line_pos[0] + x), f32(s.text_line_pos[1] + y))
        s.text_pos = s.text_line_pos

    def op_TD(self, args):
        self.state.leading = -self.number(args, 0)
        self.op_Td(args)

    def op_Tm(self, args):
        s = self.state
        s.text_matrix = tuple(self.numbers(args, 6))
        s.text_pos = s.text_line_pos = (0.0, 0.0)

    def op_Tstar(self, args):
        s = self.state
        s.text_line_pos = (s.text_line_pos[0], f32(s.text_line_pos[1] - s.leading))
        s.text_pos = s.text_line_pos

    # ---- text showing
    def op_Tj(self, args):
        if args and isinstance(args[-1], (bytes, String)) and len(args[-1]):
            self._add_text([bytes(args[-1])], 0.0, [])

    def op_quote(self, args):
        self.op_Tstar([])
        self.op_Tj(args)

    def op_dquote(self, args):
        self.state.word_space = self.number(args, 2)
        self.state.char_space = self.number(args, 1)
        self.op_quote(args)

    def op_TJ(self, args):
        array = args[-1] if args and isinstance(args[-1], list) else None
        if array is None:
            return
        s = self.state
        if not any(isinstance(v, (bytes, String)) for v in array):
            for v in array:
                k = f32(_num(v))
                if k != 0:
                    s.text_pos = (f32(s.text_pos[0] - self._horizontal_size(k)), s.text_pos[1])
            return
        strings, kernings, initial = [], [], 0.0
        for v in array:
            if isinstance(v, (bytes, String)):
                if not len(v):
                    continue
                strings.append(bytes(v))
                kernings.append(0.0)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                if not strings:
                    initial = f32(initial + f32(v))
                else:
                    kernings[-1] = f32(kernings[-1] + f32(v))
        self._add_text(strings, initial, kernings)

    def _horizontal_size(self, kerning: float) -> float:
        """GetHorizontalTextSize: kerning * font size / 1000 * Tz, in float."""
        s = self.state
        return f32(f32(f32(kerning * s.font_size) / 1000) * s.horz_scale)

    def _add_text(self, strings: list[bytes], initial: float, kernings: list[float]) -> None:
        """AddTextObject."""
        s = self.state
        font = s.font
        if font is None:
            return
        if initial != 0:
            self._kern(initial)
        if not strings:
            return
        # a Type 3 font is filled whatever Tr says (the stroke CTM, the clip list), but the object
        # keeps the text state's mode, and CalcPositionData inflates its box by it
        mode = 0 if font.is_type3 else s.text_mode
        # OnChangeTextMatrix: [Tz 0 0 1] x Tm x CTM (content_to_user is the identity here)
        tm = concat(concat((f32(s.horz_scale), 0.0, 0.0, 1.0, 0.0, 0.0), s.text_matrix), s.ctm)
        pos = transform32(s.ctm, *transform32(s.text_matrix, s.text_pos[0], f32(s.text_pos[1] + s.rise)))
        items: list = []
        kerns: list = []
        for k, string in enumerate(strings):
            for code in font.codes(string):
                items.append([code, 0.0])
                kerns.append(0.0)
            if k != len(strings) - 1 and kerns:
                kerns[-1] = kernings[k]
        if not items:
            return
        obj = PObj(OBJ_TEXT, (tm[0], tm[1], tm[2], tm[3], pos[0], pos[1]), font=font, font_size=s.font_size,
                   items=items, kernings=kerns, text_mode=s.text_mode, char_space=s.char_space,
                   word_space=s.word_space)
        if mode in (1, 2, 5, 6):
            obj.text_ctm = (s.ctm[0], s.ctm[2], s.ctm[1], s.ctm[3])
        self.add(obj, True, True)
        advance = text_positions(obj)
        if font.vertical:   # CalcPositionData: (0, advance), no Tz
            s.text_pos = (s.text_pos[0], f32(s.text_pos[1] + advance))
        else:
            s.text_pos = (f32(s.text_pos[0] + f32(advance * s.horz_scale)), s.text_pos[1])
        if mode >= 4:
            # a clone: switching the object off later leaves the clip as it is
            self.clip_text_list.append(copy.copy(obj))
        if kernings and kernings[-1] != 0:
            self._kern(kernings[-1])

    def _kern(self, kerning: float) -> None:
        """AddTextObject's kerning before and after the strings: down the line (GetVerticalTextSize,
        no Tz) in vertical writing, else along it."""
        s = self.state
        if s.font.vertical:
            s.text_pos = (s.text_pos[0], f32(s.text_pos[1] - f32(f32(kerning * s.font_size) / 1000)))
        else:
            s.text_pos = (f32(s.text_pos[0] - self._horizontal_size(kerning)), s.text_pos[1])

    # ---- XObjects, images, shadings
    def op_Do(self, args):
        if not args:
            return
        name = args[-1]
        if name == self.last_image_name and self.last_image is not None:
            self._image(self.last_image, name)
            return
        xobj = self.resource("XObject", name)
        if not isinstance(xobj, Stream):
            return
        subtype = self.doc.resolve(xobj.get("Subtype"))
        if subtype == "Form":
            self._form(xobj, name)
        elif subtype == "Image":
            self._image(xobj, name)
            self.last_image_name, self.last_image = name, xobj

    def _image(self, stream, name):
        d = stream.dict if isinstance(stream, Stream) else stream.dict
        mask = bool(self.doc.resolve(d.get("ImageMask")))
        obj = PObj(OBJ_IMAGE, self.state.ctm, stream=stream, name=str(name),
                   resources=None if isinstance(stream, Stream) else self.resources)
        self.add(obj, mask, False)
        if not mask:
            obj.fill = obj.stroke = None
        obj.rect = transform_rect(obj.matrix, (0.0, 0.0, 1.0, 1.0))

    def op_BI(self, args):
        if args and isinstance(args[0], InlineImage):
            self._image(args[0], "")

    def op_sh(self, args):
        from .render_shading import find_shading
        shading = self.resource("Shading", args[-1]) if args else None
        if not isinstance(shading, (dict, Stream)):
            return
        s = self.state
        # Handle_ShadeFill: nothing unless the pattern is a shading that loads; one that fails
        # only Validate is drawn from its second `sh` on (the type is set by then)
        record = find_shading(self.p, shading, s.parent_matrix)
        if record.kind != "shading" or not record.shade_load():
            return
        obj = PObj(OBJ_SHADING, IDENTITY, stream=shading)
        self.add(obj, False, False)
        obj.shading_matrix = s.ctm
        obj.shading_record = record
        rect = self.bbox
        if s.clips:
            rect = s.clips[0]
            for c in s.clips[1:]:
                rect = intersect(rect, c)
        if getattr(record, "type", 0) >= 4:
            from .render_mesh import shading_bbox
            from .render_shading import float_intersect
            obj.mesh_box = shading_bbox(record, s.ctm)
            rect = float_intersect(rect, obj.mesh_box)
        obj.rect = rect

    def _form(self, stream: Stream, name) -> None:
        """AddForm: the form object first, its contents after it (pre-order), parsed with a
        fresh CTM (/Matrix), its /BBox as the clip and only the general, graph, colour and text
        states of the caller."""
        r = self.doc.resolve
        s = self.state
        obj = PObj(OBJ_FORM, s.ctm, stream=stream, name=str(name))
        group = r(stream.get("Group"))
        # LoadTransparencyInfo: /I counts only in a /S /Transparency group
        obj.group = isinstance(group, dict) and r(group.get("S")) == "Transparency"
        self.add(obj, True, True)
        data = self.doc.stream_data(stream)
        chain = self.p.parsed
        if len(chain) <= MAX_FORM_LEVEL and not any(x is stream for x in chain):
            child = State(fill_cs=s.fill_cs, fill_values=s.fill_values, fill_ref=s.fill_ref,
                          stroke_cs=s.stroke_cs, stroke_values=s.stroke_values, stroke_ref=s.stroke_ref,
                          fill_alpha=s.fill_alpha, stroke_alpha=s.stroke_alpha, blend=s.blend,
                          soft_mask=s.soft_mask, line_width=s.line_width, line_cap=s.line_cap,
                          line_join=s.line_join, miter=s.miter, font=s.font, font_size=s.font_size,
                          char_space=s.char_space, word_space=s.word_space, horz_scale=s.horz_scale,
                          leading=s.leading, rise=s.rise, text_mode=s.text_mode,
                          dash=s.dash, dash_phase=s.dash_phase, smask=s.smask,
                          smask_matrix=s.smask_matrix, transfer=s.transfer,
                          fill_pattern=s.fill_pattern, stroke_pattern=s.stroke_pattern,
                          fill_set=s.fill_set, stroke_set=s.stroke_set)
            m = r(stream.get("Matrix"))
            fm = tuple(_num(r(v)) for v in m[:6]) if isinstance(m, list) and len(m) >= 6 else IDENTITY
            child.ctm = fm
            child.parent_matrix = fm
            bbox = (0.0, 0.0, 0.0, 0.0)
            b = r(stream.get("BBox"))
            if isinstance(b, list) and len(b) >= 4:
                v = [_num(r(x)) for x in b[:4]]
                rect = (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))
                pts = [transform(fm, x, y) for x, y in ((rect[0], rect[1]), (rect[2], rect[1]),
                                                       (rect[2], rect[3]), (rect[0], rect[3]))]
                child.clips = (point_bbox(pts),)
                # CPDF_Array::GetRect does not normalise: the clip starts at the first corner
                # given, which decides the order the rasteriser walks its edges in
                clip = tuple((*transform32(fm, x, y), k, c) for x, y, k, c in rect_points(*v))
                child.clip_paths = append_clip((), clip, FILL_WINDING)
                bbox = transform_rect(fm, rect)
            if obj.group:
                child.blend, child.stroke_alpha, child.fill_alpha, child.soft_mask = "Normal", 1.0, 1.0, False
                child.smask = None
            res = r(stream.get("Resources"))
            chain.append(stream)
            try:
                _Run(self.p, res if isinstance(res, dict) else self.resources, child, bbox, obj).execute(data)
            finally:
                chain.pop()
            check_clip([c for c in obj.children if c.parent is obj])
        obj.rect = form_rect(obj)


def _op_name(op: str) -> str:
    return {"W*": "Wstar", "f*": "fstar", "B*": "Bstar", "b*": "bstar", "T*": "Tstar",
            "'": "quote", '"': "dquote"}.get(op, op)


OPS = {}
for _op in ("q Q cm w J j M d gs g G rg RG k K cs CS sc SC scn SCN m l c v y h re W W* f F f* S s B B* b b* n "
            "BT ET Tc Tw Tz TL Tr Ts Tf Td TD Tm T* Tj ' \" TJ Do BI sh").split():
    OPS[_op] = getattr(_Run, "op_" + _op_name(_op))


# ---------------------------------------------------------------------- bounds


def item_origin(obj: PObj, item) -> tuple[float, float]:
    """CPDF_TextObject::GetItemInfo's origin (text space): (x, 0), or in vertical writing
    (0, y) less the font size times the char's vertical origin / 1000, in floats."""
    font = obj.font
    if not font.vertical:
        return item[1], 0.0
    vx, vy = font.vert_origin(item[0])
    size = f32(obj.font_size)
    return f32(0.0 - f32(f32(size * vx) / 1000)), f32(item[1] - f32(f32(size * vy) / 1000))


def text_positions(obj: PObj) -> float:
    """CPDF_TextObject::CalcPositionDataInternal: fills each item's x (text space, before the
    horizontal scale), sets the original and page rectangles, returns the advance. Every `a * size
    / 1000` is C float arithmetic: rounded after the product and again after the division."""
    font, size = obj.font, f32(obj.font_size)

    def scaled(v):
        return f32(f32(v * size) / 1000)
    cur = 0.0
    min_x, max_x, min_y, max_y = 10000.0, -10000.0, 10000.0, -10000.0
    cid = font.subtype == "Type0"
    vertical = font.vertical
    for item, kerning in zip(obj.items, obj.kernings):
        code = item[0]
        item[1] = cur
        l, b, r, t = font.char_bbox(code)
        if vertical:
            # the box moved by minus the vertical origin (FX_RECT::Offset, in ints), x unscaled
            vx, vy = font.vert_origin(code)
            l, r, b, t = l - vx, r - vx, b - vy, t - vy
            min_x, max_x = min(min_x, l, r), max(max_x, l, r)
            top, bottom = f32(cur + scaled(t)), f32(cur + scaled(b))
            min_y, max_y = min(min_y, top, bottom), max(max_y, top, bottom)
            cur = f32(cur + scaled(font.vert_width(code)))
        else:
            min_y, max_y = min(min_y, min(t, b)), max(max_y, max(t, b))
            w = font.char_width(code)
            try:   # the three `cur + scaled(v)` below, rounded three at a time
                sl, sr, sw = _u3(_p3(l * size, r * size, w * size))
                sl, sr, sw = _u3(_p3(sl / 1000, sr / 1000, sw / 1000))
                left, right, nxt = _u3(_p3(cur + sl, cur + sr, cur + sw))
            except OverflowError:
                left, right = f32(cur + scaled(l)), f32(cur + scaled(r))
                nxt = f32(cur + scaled(w))
            min_x, max_x = min(min_x, left, right), max(max_x, left, right)
            cur = nxt
        if code == 32 and (not cid or font.char_size(32) == 1):
            cur = f32(cur + obj.word_space)
        cur = f32(cur + obj.char_space)
        if kerning:
            cur = f32(cur - scaled(kerning))
    if vertical:
        min_x, max_x = scaled(min_x), scaled(max_x)
    else:
        min_y, max_y = scaled(min_y), scaled(max_y)
    obj.original_rect = (min_x, min_y, max_x, max_y)
    rect = transform_rect(obj.matrix, obj.original_rect)
    if obj.text_mode in (1, 2, 5, 6):
        h = f32(f32(obj.line_width) / 2)   # CFX_FloatRect::Inflate, in floats
        rect = (f32(rect[0] - h), f32(rect[1] - h), f32(rect[2] + h), f32(rect[3] + h))
    obj.rect = rect
    return cur


def path_rect(obj: PObj) -> tuple:
    """CPDF_PathObject::CalcBoundingBox."""
    width = obj.line_width
    if obj.stroked and width != 0:
        rect = stroke_bbox(obj.points, width)
    elif obj.points:
        rect = point_bbox(obj.points)
    else:
        rect = (0.0, 0.0, 0.0, 0.0)
    rect = transform_rect(obj.matrix, rect)
    if width == 0 and obj.stroked:
        rect = (f32(rect[0] - 0.5), f32(rect[1] - 0.5), f32(rect[2] + 0.5), f32(rect[3] + 0.5))
    return rect


def check_clip(objects) -> None:
    """CPDF_ContentParser::CheckClip, run over one holder's objects (a page's top level, a form's
    direct children) when its content is parsed: an object whose only clip path is a rectangle
    containing the object's rectangle loses that clip. It changes pixels where the object's edge
    lies on the clip's: an antialiased edge is then covered once, not twice. Only the clip the
    renderer uses (`clip_paths`) is dropped; `clips`, which the extraction reads, is kept.
    A clip with texts in it is kept whole."""
    for o in objects:
        if not o.active or len(o.clip_paths) != 1 or o.clip_texts or o.type == OBJ_SHADING:
            continue
        pts = o.clip_paths[0][0]
        if not path_is_rect(pts):
            continue
        (x0, y0), (x2, y2) = pts[0][:2], pts[2][:2]
        l, r, b, t = min(x0, x2), max(x0, x2), min(y0, y2), max(y0, y2)
        ol, ob, orr, ot = o.rect
        ol, orr, ob, ot = min(ol, orr), max(ol, orr), min(ob, ot), max(ob, ot)
        if ol >= l and orr <= r and ob >= b and ot <= t:
            o.clip_paths = ()


def form_rect(obj: PObj) -> tuple:
    """CPDF_FormObject::CalcBoundingBox: the form matrix over the union of its children's
    rectangles (only the direct children: theirs already include their own)."""
    kids = [c for c in obj.children if c.parent is obj and c.active]
    if not obj.children:
        return transform_rect(obj.matrix, (0.0, 0.0, 0.0, 0.0))
    if not kids:
        big = 3.4028234663852886e38
        return transform_rect(obj.matrix, (big, big, -big, -big))
    l = min(c.rect[0] for c in kids)
    b = min(c.rect[1] for c in kids)
    r = max(c.rect[2] for c in kids)
    t = max(c.rect[3] for c in kids)
    return transform_rect(obj.matrix, (l, b, r, t))


class _Rect:
    __slots__ = ("l", "b", "r", "t")

    def __init__(self):
        self.l, self.b, self.r, self.t = 100000.0, 100000.0, -100000.0, -100000.0

    def update(self, x, y):
        """CFX_FloatRect::UpdateRect: std::min/max, so a NaN never gets in."""
        if x < self.l:
            self.l = x
        if self.r < x:
            self.r = x
        if y < self.b:
            self.b = y
        if self.t < y:
            self.t = y


def _div(a: float, b: float) -> float:
    """A float division as C does it: by zero is ±inf, or NaN for 0/0."""
    try:
        return f32(a / b)
    except ZeroDivisionError:
        if a != a or a == 0:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)


def _hypot(x: float, y: float) -> float:
    return f32(math.hypot(x, y))


# The stroke bounds below are CFX_Path's, in float and one rounding per operation: whether a point
# lies above or below a line decides which side of a join the rectangle grows, and a point on
# the line (a Bezier ending on its own control point) is decided by the last bit.


def _end_points(rect: _Rect, start, end, hw):
    """UpdateLineEndPoints (cfx_path.cpp), in float32 like every step there."""
    if start[0] == end[0]:
        if start[1] == end[1]:
            rect.update(f32(end[0] + hw), f32(end[1] + hw))
            rect.update(f32(end[0] - hw), f32(end[1] - hw))
            return
        y = f32(end[1] - hw) if end[1] < start[1] else f32(end[1] + hw)
        rect.update(f32(end[0] + hw), y)
        rect.update(f32(end[0] - hw), y)
        return
    if start[1] == end[1]:
        x = f32(end[0] - hw) if end[0] < start[0] else f32(end[0] + hw)
        rect.update(x, f32(end[1] + hw))
        rect.update(x, f32(end[1] - hw))
        return
    dx, dy = f32(end[0] - start[0]), f32(end[1] - start[1])
    ll = _hypot(dx, dy)
    mx = f32(end[0] + _div(f32(hw * dx), ll))
    my = f32(end[1] + _div(f32(hw * dy), ll))
    dx1, dy1 = _div(f32(hw * dy), ll), _div(f32(hw * dx), ll)
    rect.update(f32(mx - dx1), f32(my + dy1))
    rect.update(f32(mx + dx1), f32(my - dy1))


def _join_points(rect: _Rect, start, mid, end, hw):
    """UpdateLineJoinPoints (cfx_path.cpp); the miter limit is not used there either."""
    tw = f32(1.0 / 20)
    start_vert = abs(f32(start[0] - mid[0])) < tw
    end_vert = abs(f32(mid[0] - end[0])) < tw
    if start_vert and end_vert:
        d = 1 if mid[1] > start[1] else -1
        y = f32(mid[1] + f32(hw * d))
        rect.update(f32(mid[0] + hw), y)
        rect.update(f32(mid[0] - hw), y)
        return
    start_k = start_c = end_k = end_c = start_dc = end_dc = 0.0
    if not start_vert:
        sx, sy = f32(start[0] - mid[0]), f32(start[1] - mid[1])
        start_k = _div(f32(mid[1] - start[1]), f32(mid[0] - start[0]))
        start_c = f32(mid[1] - f32(start_k * mid[0]))
        start_dc = abs(_div(f32(hw * _hypot(sx, sy)), sx))
    if not end_vert:
        ex, ey = f32(end[0] - mid[0]), f32(end[1] - mid[1])
        end_k = _div(ey, ex)
        end_c = f32(mid[1] - f32(end_k * mid[0]))
        end_dc = abs(_div(f32(hw * _hypot(ex, ey)), ex))

    def line(k, x, c):
        return f32(f32(k * x) + c)

    if start_vert:
        ox = f32(start[0] + hw) if end[0] < start[0] else f32(start[0] - hw)
        if start[1] < line(end_k, start[0], end_c):
            oy = f32(line(end_k, ox, end_c) + end_dc)
        else:
            oy = f32(line(end_k, ox, end_c) - end_dc)
        rect.update(ox, oy)
        return
    if end_vert:
        ox = f32(end[0] + hw) if start[0] < end[0] else f32(end[0] - hw)
        if end[1] < line(start_k, end[0], start_c):
            oy = f32(line(start_k, ox, start_c) + start_dc)
        else:
            oy = f32(line(start_k, ox, start_c) - start_dc)
        rect.update(ox, oy)
        return
    if abs(f32(start_k - end_k)) < tw:
        sd = 1 if mid[0] > start[0] else -1
        ed = 1 if end[0] > mid[0] else -1
        if sd == ed:
            _end_points(rect, mid, end, hw)
        else:
            _end_points(rect, start, mid, hw)
        return
    so = f32(start_c + start_dc) if end[1] < line(start_k, end[0], start_c) else f32(start_c - start_dc)
    eo = f32(end_c + end_dc) if start[1] < line(end_k, start[0], end_c) else f32(end_c - end_dc)
    jx = _div(f32(eo - so), f32(start_k - end_k))
    rect.update(jx, line(start_k, jx, so))


def stroke_bbox(points: list, line_width: float) -> tuple:
    """CFX_Path::GetBoundingBoxForStrokePath: half_width is the whole line width there."""
    rect = _Rect()
    hw = line_width
    n = len(points)
    i = 0
    start = end = mid = 0
    join = False
    while i < n:
        x, y, kind, closes = points[i]
        if kind == PT_MOVE:
            if i + 1 == n:
                if closes:
                    rect.update(x, y)
                break
            start, end, join = i + 1, i, False
        else:
            if kind == PT_BEZIER and not closes:
                if i + 2 >= n:
                    break
                rect.update(points[i][0], points[i][1])
                rect.update(points[i + 1][0], points[i + 1][1])
                i += 2
            if i + 1 == n or points[i + 1][2] == PT_MOVE:
                start, end, join = i - 1, i, False
            else:
                start, mid, end, join = i - 1, i, i + 1, True
        p = lambda k: (points[k][0], points[k][1])  # noqa: E731
        if join:
            _join_points(rect, p(start), p(mid), p(end), hw)
        else:
            _end_points(rect, p(start), p(end), hw)
        i += 1
    return rect.l, rect.b, rect.r, rect.t
