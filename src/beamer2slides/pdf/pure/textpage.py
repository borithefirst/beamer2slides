"""PDFium's text page (core/fpdftext/cpdf_textpage.cpp) over the page objects of content.py.

`FPDFText_*` answers come from this list of characters: which ones exist (a text object drawn
twice for a fake bold is dropped, a glyph drawn twice within 0.07 em is dropped, runs of spaces
collapse), in what order (lines of objects sorted by x), which are generated (spaces and line
breaks the page never drew), and which hyphen ends a line (reported as U+0002, `kHyphen`). The
backend's `chars()` reads the same fields PDFium's accessors do, so every rule here is ported as
written, oddities included (a zero-height glyph box grows by font_size/1000, not by the size).

Right to left: a line is cut into CFX_BidiChar's segments and right-to-left ones are written
backwards with mirrored characters, as CloseTempLine does; the bidi classes are Python's
`unicodedata` (PDFium's table is its own copy of the same Unicode data).

Not ported: vertical writing (CID fonts with a -V CMap are laid out horizontally) and /ActualText
marked content."""

from __future__ import annotations

import math
import unicodedata

from .content import OBJ_FORM, OBJ_TEXT, PObj, f32m, f32p
from .syntax import float32 as f32
from .fonts import INVALID_CODE

NORMAL, GENERATED, NOT_UNICODE, HYPHEN, PIECE, ACTUAL_TEXT = range(6)
SIZE_EPSILON = 0.01
TIE = 1e-6  # relative: well under float32's resolution
DEFAULT_FONT_SIZE = 1.0
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
EMPTY = (0.0, 0.0, 0.0, 0.0)
# GetUnicodeNormalization for the Latin ligatures, the only characters it touches left to right
LIGATURE_PIECES = {0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl",
                   0xFB05: "st", 0xFB06: "st"}


# ---------------------------------------------------------------------- CFX_BidiChar / CFX_BidiString

BIDI_NEUTRAL, BIDI_LEFT, BIDI_RIGHT, BIDI_LEFT_WEAK = range(4)
_BIDI = {"L": BIDI_LEFT, "AN": BIDI_LEFT_WEAK, "EN": BIDI_LEFT_WEAK, "R": BIDI_RIGHT, "AL": BIDI_RIGHT}
# BidiMirroring.txt, the pairs a text page can meet (pdfium::unicode::GetMirrorChar)
_MIRROR_PAIRS = "()<>[]{}«»‹›⁅⁆⁽⁾₍₎∈∋∉∌∊∍≤≥≦≧≨≩≪≫≮≯≰≱≲≳≺≻≼≽⊂⊃⊄⊅⊆⊇⊈⊉⊊⊋⊏⊐⊑⊒⊢⊣⌈⌉⌊⌋〈〉⟨⟩⟪⟫⟦⟧⟮⟯⦃⦄⦅⦆《》「」『』【】〔〕〖〗〘〙〚〛"
MIRROR = {}
for _a, _b in zip(_MIRROR_PAIRS[::2], _MIRROR_PAIRS[1::2]):
    MIRROR[ord(_a)], MIRROR[ord(_b)] = ord(_b), ord(_a)


def bidi_direction(u: int) -> int:
    return _BIDI.get(unicodedata.bidirectional(chr(u)) if u < 0x110000 else "", BIDI_NEUTRAL)


def bidi_segments(units: list[int]) -> tuple[list[tuple[int, int, int]], bool]:
    """CFX_BidiString: (start, count, direction) segments in the order they are written, and
    whether the overall direction is right to left (as many right segments as left ones, or more).
    Like PDFium's, the list starts with the empty segment a first change of direction closes."""
    order, start, count, direction = [], 0, 0, BIDI_NEUTRAL
    for u in units:
        d = bidi_direction(u)
        if d != direction:
            order.append((start, count, direction))
            start, count, direction = start + count, 0, d
        count += 1
    if count:
        order.append((start, count, direction))
    rights = sum(1 for s in order if s[2] == BIDI_RIGHT)
    lefts = sum(1 for s in order if s[2] == BIDI_LEFT)
    rtl = rights > 0 and rights >= lefts
    return (order[::-1] if rtl else order), rtl


def normalization(u: int) -> list[int]:
    """GetUnicodeNormalization: a compatibility decomposition, else the character itself (never
    empty, so every character of a right-to-left run becomes a kPiece - a generated space too)."""
    if u >= 0x110000 or not unicodedata.decomposition(chr(u)):
        return [u]
    return [ord(c) for c in unicodedata.normalize("NFKD", chr(u))]


H_NONE, H_SPACE, H_LINEBREAK, H_HYPHEN = range(4)      # GenerateCharacter
DIR_UNKNOWN, DIR_HORIZONTAL, DIR_VERTICAL = range(3)   # TextOrientation


# ---------------------------------------------------------------------- CFX_Matrix / CFX_FloatRect


def concat(m, n):
    """PDFium's `m * n`: m first, then n."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def inverse(m):
    a, b, c, d, e, f = m
    i = a * d - b * c
    if i == 0:
        return IDENTITY
    j = -i
    return d / i, b / j, c / j, a / i, (c * f - d * e) / i, (a * f - b * e) / j


def x_unit(m):
    a, b = m[0], m[1]
    return abs(a) if b == 0 else abs(b) if a == 0 else math.hypot(a, b)


def y_unit(m):
    c, d = m[2], m[3]
    return abs(d) if c == 0 else abs(c) if d == 0 else math.hypot(c, d)


def distance(m, v):
    """TransformDistance."""
    return v * (x_unit(m) + y_unit(m)) / 2


def x_distance(m, v):
    """TransformXDistance."""
    return math.hypot(m[0] * v, m[1] * v)


def transform_rect(m, r):
    l, b, rt, t = r
    pts = [apply(m, x, y) for x, y in ((l, t), (l, b), (rt, t), (rt, b))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def normalize(r):
    l, b, rt, t = r
    return min(l, rt), min(b, t), max(l, rt), max(b, t)


def is_empty(r):
    return r[0] >= r[2] or r[1] >= r[3]


def intersect(r, o):
    r, o = normalize(r), normalize(o)
    out = max(r[0], o[0]), max(r[1], o[1]), min(r[2], o[2]), min(r[3], o[3])
    return EMPTY if out[0] > out[2] or out[1] > out[3] else out


def union(r, o):
    r, o = normalize(r), normalize(o)
    return min(r[0], o[0]), min(r[1], o[1]), max(r[2], o[2]), max(r[3], o[3])


def contains(r, x, y):
    l, b, rt, t = normalize(r)
    return l <= x <= rt and b <= y <= t


# ---------------------------------------------------------------------- font helpers


def char_width(code: int, font) -> int:
    """GetCharWidth in cpdf_textpage.cpp: the width, else the glyph box's."""
    if code == INVALID_CODE:
        return 0
    w = font.char_width(code)
    if w > 0:
        return w
    l, b, r, t = font.char_bbox(code)
    return max(r - l, 0)


def unicode_of(font, code: int) -> list[int]:
    return font.unicode(code) if code != INVALID_CODE else []


def first_unicode(font, code: int) -> int:
    """UnicodeFromCharCode's first unit, or the code itself when there is none."""
    u = unicode_of(font, code)
    return u[0] if u else code & 0xFFFF


def normalize_threshold(v: float, t1: int, t2: int, t3: int) -> float:
    if v < t1:
        return v / 2
    if v < t2:
        return v / 4
    if v < t3:
        return v / 5
    return v / 6


def font_size_h(obj: PObj) -> float:
    return abs(math.hypot(obj.matrix[0], obj.matrix[1]) * obj.font_size)


def text_matrix(obj: PObj):
    return obj.matrix


def pos(obj: PObj):
    return obj.matrix[4], obj.matrix[5]


def space_threshold(font, fsh: float, code: int) -> float:
    """CalculateSpaceThreshold."""
    space = font.code_from_unicode(0x20)
    threshold = 0.0
    if space != INVALID_CODE:
        threshold = fsh * font.char_width(space) / 1000
    if threshold > fsh / 3:
        threshold = 0.0
    else:
        threshold /= 2
    if threshold == 0:
        threshold = normalize_threshold(char_width(code, font), 300, 500, 700)
        threshold = fsh * threshold / 1000
    return threshold


def generate_space(px: float, last_pos: float, this_width: float, last_width: float, threshold: float) -> bool:
    if abs(last_pos + last_width - px) <= threshold:
        return False
    threshold_pos = threshold + last_width
    diff = px - last_pos
    if abs(diff) > threshold_pos:
        return True
    if px < 0 and -threshold_pos > diff:
        return True
    return diff > this_width + last_width


def end_horizontal_line(this_rect, prev_rect) -> bool:
    if this_rect[3] - this_rect[1] <= 4.5 or prev_rect[3] - prev_rect[1] <= 4.5:
        return False
    return max(this_rect[1], prev_rect[1]) >= min(this_rect[3], prev_rect[3])


def end_vertical_line(this_rect, prev_rect, line_rect, this_size, prev_size) -> bool:
    if this_rect[2] - this_rect[0] <= this_size * 0.1 or prev_rect[2] - prev_rect[0] <= prev_size * 0.1:
        return False
    return min(this_rect[2], line_rect[2]) <= max(this_rect[0], line_rect[0])


def is_hyphen_code(c: int) -> bool:
    return c in (0x2D, 0xAD)


def _isalpha(c: int) -> bool:
    return 0 <= c < 0x110000 and chr(c).isalpha()


def _isalnum(c: int) -> bool:
    return 0 <= c < 0x110000 and chr(c).isalnum()


# ---------------------------------------------------------------------- characters


class CharInfo:
    __slots__ = ("type", "code", "unicode", "origin", "box", "matrix", "obj", "loose")

    def __init__(self, ctype, code, unicode, origin, box, matrix, obj):
        self.type, self.code, self.unicode = ctype, code, unicode
        self.origin, self.box, self.matrix, self.obj = origin, box, matrix, obj
        self.loose = loose_bounds(self)

    def copy(self) -> "CharInfo":
        c = CharInfo.__new__(CharInfo)
        for k in CharInfo.__slots__:
            setattr(c, k, getattr(self, k))
        return c

    @property
    def font_size(self) -> float:
        return self.obj.font_size if self.obj is not None and self.obj.font is not None else DEFAULT_FONT_SIZE


def loose_bounds(ci: CharInfo):
    """GetLooseBounds: the advance by the font's ascent and descent, with the glyph box."""
    if is_empty(ci.box):
        return ci.box
    obj = ci.obj
    size = ci.font_size
    if obj is not None and abs(size) >= 0.0001 and ci.code != INVALID_CODE:
        font = obj.font
        ascent, descent = font.ascent, font.descent
        fb = font.font_bbox
        if fb[3] > fb[1]:
            ascent, descent = min(ascent, fb[3]), max(descent, fb[1])
        if ascent != descent:
            width = f32(font.char_width(ci.code) * obj.font_size / 1000)
            ox, oy = f32p(apply(f32m(inverse(ci.matrix)), *ci.origin))
            box = transform_rect(ci.matrix, (ox, f32(oy + descent * size / 1000), f32(ox + width),
                                             f32(oy + ascent * size / 1000)))
            return union(tuple(f32(v) for v in box), ci.box)
    return ci.box


def is_control(ci: CharInfo) -> bool:
    if ci.unicode in (0x2, 0x3, 0x93, 0x94, 0x96, 0x97, 0x98, 0xFFFE):
        return ci.type != HYPHEN
    return False


def is_normal(ci: CharInfo) -> bool:
    return not is_control(ci) if ci.unicode != 0 else ci.code != 0


class TextPage:
    """CPDF_TextPage over `objects` (content.py's page objects, pre-order)."""

    def __init__(self, objects: list[PObj], width: float, height: float, display=None):
        self.objects = objects
        self.width, self.height = width, height
        self.display = display or (1.0, 0.0, 0.0, -1.0, 0.0, height)
        self.chars: list[CharInfo] = []
        self.buf: list[int] = []
        self.temp: list[CharInfo] = []
        self.temp_buf: list[int] = []
        self.text_objects: list[tuple[PObj, tuple]] = []
        self.prev_obj: PObj | None = None
        self.prev_matrix = IDENTITY
        self.line_rect = EMPTY
        self.textline_dir = DIR_UNKNOWN
        self._holders: dict[int, list[PObj]] = {}
        self._index: dict[int, int] = {}
        self._build()

    # ---- ProcessObject
    def _build(self) -> None:
        top = [o for o in self.objects if o.parent is None]
        if not any(o.active for o in top):
            return
        for holder in [top] + [o.children for o in self.objects if o.type == OBJ_FORM]:
            for k, o in enumerate(holder):
                self._holders[id(o)] = holder
                self._index[id(o)] = k
        self.textline_dir = self._flow_orientation(top)
        self._walk(top, IDENTITY)
        self._process_transformed()
        self.text_objects = []
        self._close_temp_line()

    def _walk(self, holder: list[PObj], form_matrix) -> None:
        for o in holder:
            if not o.active:
                continue
            if o.type == OBJ_TEXT:
                self._process_text_object(o, form_matrix)
            elif o.type == OBJ_FORM:
                self._walk(o.children, concat(o.matrix, form_matrix))

    def _flow_orientation(self, top: list[PObj]) -> int:
        """FindTextlineFlowOrientation (top-level text objects only)."""
        pw, ph = int(self.width), int(self.height)
        if pw <= 0 or ph <= 0:
            return DIR_UNKNOWN
        hmask = [False] * pw
        vmask = [False] * ph
        line_height = 0.0
        start_h, end_h, start_v, end_v = pw, 0, ph, 0
        for o in top:
            if not o.active or o.type != OBJ_TEXT:
                continue
            l, b, r, t = o.rect
            min_h = int(min(max(l, 0.0), pw))
            max_h = int(min(max(r, 0.0), pw))
            min_v = int(min(max(b, 0.0), ph))
            max_v = int(min(max(t, 0.0), ph))
            if min_h >= max_h or min_v >= max_v:
                continue
            for i in range(min_h, max_h):
                hmask[i] = True
            for i in range(min_v, max_v):
                vmask[i] = True
            start_h, end_h = min(start_h, min_h), max(end_h, max_h)
            start_v, end_v = min(start_v, min_v), max(end_v, max_v)
            if line_height <= 0:
                line_height = t - b
        double = int(2 * line_height)
        if end_v - start_v < double:
            return DIR_HORIZONTAL
        if end_h - start_h < double:
            return DIR_VERTICAL

        def filled(mask, s, e):
            return 0.0 if s >= e else sum(mask[s:e]) / (e - s)

        sum_h = filled(hmask, start_h, end_h)
        if sum_h > 0.8:
            return DIR_HORIZONTAL
        sum_v = filled(vmask, start_v, end_v)
        if sum_h > sum_v:
            return DIR_HORIZONTAL
        if sum_h < sum_v:
            return DIR_VERTICAL
        return DIR_UNKNOWN

    # ---- ProcessTextObject
    def _process_text_object(self, obj: PObj, form_matrix) -> None:
        if abs(obj.rect[2] - obj.rect[0]) < SIZE_EPSILON:
            return
        new = (obj, form_matrix)
        if not self.text_objects:
            self.text_objects.append(new)
            return
        if self._same_as_previous(obj):
            return
        prev, prev_form = self.text_objects[-1]
        if not prev.items:
            return
        prev_width = char_width(prev.items[-1][0], prev.font) * prev.font_size / 1000
        prev_width = distance(concat(text_matrix(prev), prev_form), abs(prev_width))
        this_width = abs(char_width(obj.items[0][0], obj.font) * obj.font_size / 1000)
        this_width = distance(concat(text_matrix(obj), form_matrix), abs(this_width))
        threshold = max(prev_width, this_width) / 4
        prev_pos = apply(self.display, *apply(prev_form, *pos(prev)))
        this_pos = apply(self.display, *apply(form_matrix, *pos(obj)))
        if abs(this_pos[1] - prev_pos[1]) > threshold * 2:
            self._process_transformed()
            self.text_objects = [new]
            return
        for i in range(len(self.text_objects), 0, -1):
            o, fm = self.text_objects[i - 1]
            p = apply(self.display, *apply(fm, *pos(o)))
            # PDFium works in float32: objects at the same x come out equal there, and the
            # later one goes after (float64 can put it a hair to the left)
            if this_pos[0] >= p[0] - TIE * max(1.0, abs(p[0])):
                self.text_objects.insert(i, new)
                return
        self.text_objects.insert(0, new)

    def _same_as_previous(self, obj: PObj) -> bool:
        """IsSameAsPreTextObject: the last five text objects before it in its container."""
        holder = self._holders.get(id(obj))
        if holder is None:
            return False
        k = self._index[id(obj)]
        i = 0
        while i < 5 and k > 0:
            k -= 1
            other = holder[k]
            if other is obj or other.type != OBJ_TEXT:
                continue
            if self._same_text_object(other, obj):
                return True
            i += 1
        return False

    def _same_text_object(self, obj1: PObj, obj2: PObj) -> bool:
        """IsSameTextObject(obj1 = the earlier one, obj2 = the current one)."""
        prev_rect = obj2.rect
        cur_rect = obj1.rect
        if is_empty(prev_rect) and is_empty(cur_rect):
            xdiff = abs(prev_rect[0] - cur_rect[0])
            if len(self.chars) >= 2:
                c = self.chars[-2].box
                if xdiff > c[2] - c[0]:
                    return False
        if not is_empty(prev_rect) or not is_empty(cur_rect):
            prev_rect = intersect(prev_rect, cur_rect)
            if is_empty(prev_rect):
                return False
            cur_w = cur_rect[2] - cur_rect[0]
            if abs((prev_rect[2] - prev_rect[0]) - cur_w) > cur_w / 2:
                return False
            if obj2.font_size != obj1.font_size:
                return False
        n = len(obj2.items)
        if n != len(obj1.items):
            return False
        if n == 0:
            return True
        for a, b in zip(obj2.items, obj1.items):
            if a[0] != b[0]:
                return False
        dx = pos(obj1)[0] - pos(obj2)[0]
        dy = pos(obj1)[1] - pos(obj2)[1]
        size = obj2.font_size
        cw = char_width(obj2.items[-1][0], obj2.font)
        max_pre = max(prev_rect[3] - prev_rect[1], prev_rect[2] - prev_rect[0], size)
        return abs(dx) <= 0.9 * cw * size / 1000 and abs(dy) <= max_pre / 8

    # ---- ProcessTransformedTextObjects
    def _process_transformed(self) -> None:
        for obj, form_matrix in self.text_objects:
            if abs(obj.rect[2] - obj.rect[0]) < SIZE_EPSILON:
                continue
            if self.prev_obj is not None:
                kind = self._insert_object(obj, form_matrix)
                if kind == H_LINEBREAK:
                    self.line_rect = obj.rect
                else:
                    self.line_rect = union(self.line_rect, obj.rect)
                if not self._generate(kind, obj, form_matrix):
                    continue
            else:
                self.line_rect = obj.rect
            self.prev_obj, self.prev_matrix = obj, form_matrix
            self._items(obj, form_matrix, f32m(concat(text_matrix(obj), form_matrix)))

    def _writing_mode(self, obj: PObj) -> int:
        n = len(obj.items)
        if n <= 1:
            return self.textline_dir
        m = text_matrix(obj)
        fx, fy = apply(m, obj.items[0][1], 0.0)
        lx, ly = apply(m, obj.items[-1][1], 0.0)
        dx, dy = abs(lx - fx), abs(ly - fy)
        if dx <= 0.0001 and dy <= 0.0001:
            return DIR_UNKNOWN
        length = math.hypot(dx, dy)
        vx, vy = dx / length, dy / length
        under = vx <= 0.0872
        if vy <= 0.0872:
            return self.textline_dir if under else DIR_HORIZONTAL
        return DIR_VERTICAL if under else self.textline_dir

    def _prev_char(self) -> CharInfo | None:
        if self.temp:
            return self.temp[-1]
        return self.chars[-1] if self.chars else None

    def _is_hyphen(self, current: int) -> bool:
        text = self.temp_buf or self.buf
        if not text:
            return False
        k = len(text) - 1
        while k > 0 and text[k] == 0x20:
            k -= 1
        if not is_hyphen_code(text[k]):
            return False
        if k > 0 and _isalpha(text[k - 1]) and _isalnum(current):
            return True
        prev = self._prev_char()
        return prev is not None and prev.type in (PIECE, ACTUAL_TEXT) and is_hyphen_code(prev.unicode)

    def _insert_object(self, obj: PObj, form_matrix) -> int:
        """ProcessInsertObject: what goes between the previous object and this one."""
        prev = self._prev_char()
        if prev is not None and prev.obj is not None:
            self.prev_obj = prev.obj
        prev_obj = self.prev_obj
        mode = self._writing_mode(obj)
        if mode == DIR_UNKNOWN:
            mode = self._writing_mode(prev_obj)
        n = len(prev_obj.items)
        if n == 0:
            return H_NONE
        prev_code, prev_x = prev_obj.items[-1]
        code = obj.items[0][0]
        this_rect, prev_rect = obj.rect, prev_obj.rect
        current = first_unicode(obj.font, code)
        if mode == DIR_HORIZONTAL:
            if end_horizontal_line(this_rect, prev_rect):
                return H_HYPHEN if self._is_hyphen(current) else H_LINEBREAK
        elif mode == DIR_VERTICAL:
            if end_vertical_line(this_rect, prev_rect, self.line_rect, obj.font_size, prev_obj.font_size):
                return H_HYPHEN if self._is_hyphen(current) else H_LINEBREAK

        last_pos = prev_x
        last_w = char_width(prev_code, prev_obj.font)
        last_width = abs(last_w * prev_obj.font_size / 1000)
        this_w = char_width(code, obj.font)
        this_width = abs(this_w * obj.font_size / 1000)
        threshold = max(last_width, this_width) / 4
        prev_matrix = concat(text_matrix(prev_obj), self.prev_matrix)
        prev_inv = inverse(prev_matrix)
        px, py = apply(prev_inv, *apply(form_matrix, *pos(obj)))
        if last_width < this_width:
            threshold = distance(prev_inv, threshold)

        newline = False
        if mode == DIR_HORIZONTAL:
            rect = prev_obj.rect
            rect_height = rect[3] - rect[1]
            rect = normalize(rect)
            if (is_empty(rect) and rect_height > 5) or \
                    ((py > threshold * 2 or py < threshold * -3) and (abs(py) >= 1 or abs(py) > abs(px))):
                newline = True
                if n > 1:
                    first_x = prev_obj.items[0][1]
                    m = text_matrix(prev_obj)
                    d = self.display
                    if prev_x > first_x and d[0] > 0.9 and d[1] < 0.1 and d[2] < 0.1 and d[3] < -0.9 \
                            and m[1] < 0.1 and m[2] < 0.1:
                        pr = prev_obj.rect
                        if contains((0, pr[1], 1000, pr[3]), *pos(obj)):
                            newline = False
                        elif contains((0, obj.rect[1], 1000, obj.rect[3]), *pos(prev_obj)):
                            newline = False
        if newline:
            return H_HYPHEN if self._is_hyphen(current) else H_LINEBREAK

        if len(obj.items) == 1 and is_hyphen_code(current) and self._is_hyphen(current):
            return H_HYPHEN
        if current == 0x20:
            return H_NONE
        prev_str = unicode_of(prev_obj.font, prev_code)
        if prev_str and prev_str[-1] == 0x20:
            return H_NONE

        matrix = concat(text_matrix(obj), form_matrix)
        threshold2 = normalize_threshold(float(max(last_w, this_w)), 400, 700, 800)
        if last_w >= this_w:
            threshold2 *= abs(prev_obj.font_size)
        else:
            threshold2 *= abs(obj.font_size)
            threshold2 = distance(matrix, threshold2)
            threshold2 = distance(prev_inv, threshold2)
        threshold2 /= 1000
        if 1.4879 < threshold2 < 1.4881 or 1.38999 < threshold2 < 1.39001:
            threshold2 *= 1.5
        return H_SPACE if generate_space((px, py)[0], last_pos, this_width, last_width, threshold2) else H_NONE

    def _generated(self, unicode: int, form_matrix) -> CharInfo | None:
        """GenerateCharInfo: a character placed after the previous one."""
        prev = self._prev_char()
        if prev is None:
            return None
        width = 0
        if prev.obj is not None and prev.code != INVALID_CODE:
            width = char_width(prev.code, prev.obj.font)
        size = prev.obj.font_size if prev.obj is not None else prev.box[3] - prev.box[1]
        if not size:
            size = DEFAULT_FONT_SIZE
        x, y = prev.origin[0] + width * size / 1000, prev.origin[1]
        return CharInfo(GENERATED, INVALID_CODE, unicode, (x, y), (x, y, x, y), form_matrix, None)

    def _append_generated(self, unicode: int, form_matrix, temp: bool) -> None:
        ci = self._generated(unicode, form_matrix)
        if ci is None:
            return
        if temp:
            self.temp_buf.append(unicode)
            self.temp.append(ci)
        else:
            self.buf.append(unicode)
            self.chars.append(ci)

    def _generate(self, kind: int, obj: PObj, form_matrix) -> bool:
        """ProcessGenerateCharacter: False skips the object."""
        if kind == H_SPACE:
            self._append_generated(0x20, form_matrix, True)
        elif kind == H_LINEBREAK:
            self._close_temp_line()
            if self.buf:
                self._append_generated(0x0D, form_matrix, False)
                self._append_generated(0x0A, form_matrix, False)
        elif kind == H_HYPHEN:
            if len(obj.items) == 1 and is_hyphen_code(first_unicode(obj.font, obj.items[0][0])):
                return False
            while self.temp_buf and self.temp_buf[-1] == 0x20:
                self.temp_buf.pop()
                self.temp.pop()
            if not self.temp:
                return True  # PDFium reads past an empty list here
            ci = self.temp[-1]
            self.temp_buf.pop()
            ci.type = HYPHEN
            ci.unicode = 0x2
            self.temp_buf.append(0xFFFE)
        return True

    # ---- ProcessTextObjectItems
    def _items(self, obj: PObj, form_matrix, matrix) -> None:
        font = obj.font
        n = len(obj.items)
        fsh = font_size_h(obj)
        base_space = 0.0
        cs = obj.char_space
        if cs != 0 and n >= 2:  # CalculateBaseSpace
            has_kerning = False
            spacing = distance(matrix, cs)
            base_space = spacing
            for k in obj.kernings:
                if k != 0:
                    base_space = min(base_space, -fsh * k / 1000 + spacing)
                    has_kerning = True
            if base_space < 0 or (n == 2 and has_kerning):
                base_space = 0.0
        if cs > 0.001:  # CalculateBaseSpaceAdjustment
            base_space += -distance(matrix, cs)
        elif cs < -0.001:
            base_space += distance(matrix, abs(cs))

        # IsRightToLeft: a mirrored right-to-left object's characters are put back in order
        mirrored = matrix[0] * matrix[3] - matrix[1] * matrix[2] < 0
        if mirrored:
            firsts = [first_unicode(font, code) for code, _ in obj.items if code != INVALID_CODE]
            mirrored = bidi_segments([u for u in firsts if u])[1]
        start_chars = len(self.temp)
        self._items_in_order(obj, form_matrix, matrix, fsh, base_space)
        if mirrored:  # SwapTempTextBuf
            self.temp[start_chars:] = self.temp[start_chars:][::-1]
            self.temp_buf[start_chars:] = self.temp_buf[start_chars:][::-1]

    def _items_in_order(self, obj: PObj, form_matrix, matrix, fsh: float, base_space: float) -> None:
        font = obj.font
        spacing = 0.0
        size = obj.font_size / 1000
        for i, (code, x) in enumerate(obj.items):
            if i > 0 and obj.kernings[i - 1] != 0:
                text = self.temp_buf or self.buf
                if text and text[-1] != 0x20:
                    spacing = -fsh * obj.kernings[i - 1] / 1000
            spacing -= base_space
            if spacing and i > 0:
                threshold = space_threshold(font, fsh, code)
                if threshold and spacing >= threshold:
                    self.temp_buf.append(0x20)
                    ox, oy = f32p(apply(matrix, x, 0.0))
                    self.temp.append(CharInfo(GENERATED, INVALID_CODE, 0x20, (ox, oy), (ox, oy, ox, oy),
                                              form_matrix, obj))
            spacing = 0.0
            units = list(unicode_of(font, code))
            ctype = NORMAL
            if not units and code:
                units = [code & 0xFFFF]
                ctype = NOT_UNICODE
            l, b, r, t = font.char_bbox(code)
            box = [f32(l * size + x), f32(b * size), f32(r * size + x), f32(t * size)]
            if abs(box[3] - box[1]) < SIZE_EPSILON:
                box[3] = f32(box[1] + size)
            if abs(box[2] - box[0]) < SIZE_EPSILON:
                box[2] = f32(box[0] + font.char_width(code) * size)
            box = tuple(f32(v) for v in transform_rect(matrix, box))
            ci = CharInfo(ctype, code, 0, f32p(apply(matrix, x, 0.0)), box, matrix, obj)
            if not units:
                self.temp.append(ci)
                self.temp_buf.append(0xFFFE)
                continue
            add = True
            count = min(len(self.temp), 7)
            threshold = x_distance(ci.matrix, 0.07 * obj.font_size)
            for cand in self.temp[len(self.temp) - count:][::-1]:
                if cand.code != ci.code:
                    continue
                if cand.obj is None or cand.obj.font is not font:
                    continue
                if abs(cand.origin[0] - ci.origin[0]) < threshold and abs(cand.origin[1] - ci.origin[1]) < threshold:
                    add = False
                    break
            if add:
                for u in units:
                    c = ci.copy()
                    c.unicode = u
                    self.temp_buf.append(u if u else 0xFFFE)
                    self.temp.append(c)
            elif i == 0 and self.temp_buf and self.temp_buf[-1] == 0x20:
                self.temp_buf.pop()
                self.temp.pop()

    # ---- CloseTempLine
    def _close_temp_line(self) -> None:
        if not self.temp:
            return
        buf, chars = [], []
        prev_space = False
        for u, ci in zip(self.temp_buf, self.temp):
            if u != 0x20:
                prev_space = False
            elif prev_space:
                continue
            else:
                prev_space = True
            buf.append(u)
            chars.append(ci)
        segments, rtl = bidi_segments(buf)
        current = BIDI_RIGHT if rtl else BIDI_LEFT
        for start, count, direction in segments:
            if direction == BIDI_RIGHT or (direction == BIDI_NEUTRAL and current == BIDI_RIGHT):
                current = BIDI_RIGHT
                for m in range(start + count - 1, start - 1, -1):
                    self._add_char_rtl(buf[m], chars[m])
            else:
                if direction != BIDI_LEFT_WEAK:
                    current = BIDI_LEFT
                for m in range(start, start + count):
                    self._add_char(buf[m], chars[m])
        self.temp, self.temp_buf = [], []

    def _add_char_rtl(self, u: int, ci: CharInfo) -> None:
        """AddCharInfoByRLDirection: mirrored, and decomposed into pieces."""
        if not is_normal(ci):
            self.chars.append(ci)
            return
        for p in normalization(MIRROR.get(u, u)):
            c = ci.copy()
            c.type = PIECE
            c.unicode = p
            self.buf.append(p)
            self.chars.append(c)

    def _add_char(self, u: int, ci: CharInfo) -> None:
        if not is_normal(ci):
            self.chars.append(ci)
            return
        pieces = LIGATURE_PIECES.get(u)
        if not pieces:
            self.buf.append(u)
            self.chars.append(ci)
            return
        for p in pieces:
            c = ci.copy()
            c.type = PIECE
            c.unicode = ord(p)
            self.buf.append(ord(p))
            self.chars.append(c)
