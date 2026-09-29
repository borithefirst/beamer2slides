"""A live Google Slides deck as IR (deck.json-shaped), for comparison with classify's output.

`read_target(pres, ...)` is pure (the `presentations.get` answer in, a `deck_ir_types.TargetDeck`
out); `deck_ir` is the same read as the JSON target.json holds (`target_json`); `read_deck` fetches
the presentation and downloads its pictures. Positions go back to PDF pt through emit's text box
model: a box's text starts PAD_X inside it and its first baseline sits BASELINE_A + ASCENT_EM x size
below its top (MIDDLE boxes: MIDDLE_BASELINE_EM below the middle), so `anchor` = (x by alignment,
first baseline) / scale is the same point classify's paragraphs give (compare.text_anchor).
Font sizes go back through FontMapper's width factors (`pdf_size`).

Slide keys: the `b2s:<slide key>/<element key>` alt-text title of our objects, else a base
snapshot's object map, else none (compare aligns slides by title and text).

Numbers are kept as the answer has them (an int stays an int), because the JSON written is
target.json, which every reader since compares key for key.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard

from .deck_ir_types import (ABSENT, Absent, CellParagraph, Chart, Crop, DeckSource, DiagramNode, Frame, Layout,
                            PageGradient, PictureOutline, Recolor, RecolorStop, SlidesMeasures, TableBorder, TableCell,
                            TargetBullet, TargetDeck, TargetDiagram, TargetElement, TargetImage, TargetLine,
                            TargetParagraph, TargetRun, TargetShape, TargetSlide, TargetTable, TargetText, TextBox,
                            VAlign, Video, target_json)
from .deck_thumbs import (SNAP_PAGE, ink_widths, pptx_insets, side_gap, side_inset, thumbnail_cell_pad,
                          thumbnail_cell_text, thumbnail_insets, thumbnail_picture_masks,
                          thumbnail_picture_places, thumbnail_rows, thumbnail_weights, top_drift)
from .emit import (ASCENT_EM, BASELINE_A, FONT_FOR_FAMILY, MIDDLE_BASELINE_EM, OPTICAL_WEIGHTS_READ, PAD_X,
                   PPTX_TITLE_DY, SOFT_BREAK, ZWSP, FontMapper, extra_above, line_size)
from .fonts import cjk_font
from .google_types import (AffineTransform, Dimension, Page, PageElement, Presentation, SlidesService, children,
                           object_id, part, parts, presentation)
from .gslides import EMU_PER_PT
from .ir import Align, Family, Script
from .ir_types import FAMILIES, Box, Point
from .json_types import Json, JsonObject, JsonShapeError, as_str
from .typing_compat import assert_never

if TYPE_CHECKING:
    from .arrays import SignedRGB

Fetch = Callable[[str], bytes]
Thumbnails = Callable[[int], object]
"""`thumbnails(n)`: Google's picture of slide n (0-based): a path, a PIL image or an array, or None."""
Affine = tuple[float, float, float, float, float, float]
"""[a, b, tx, d, e, ty] in pt."""
IDENTITY: Affine = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
WHERE = "the deck read"

FAMILY_FOR_FONT: dict[str, Family] = {FONT_FOR_FAMILY[f]: f for f in FAMILIES if f in FONT_FOR_FAMILY}
# Only the three fonts the converter itself writes are named above, and everything else used to come
# back as `sans` - so a deck whose person typed in Space Mono read back as prose, and the size came
# through the wrong width factors as well. A font is known by its name here, the way a reader knows
# it: these words appear in the name of nearly every monospaced or serif family Slides offers.
MONO_WORDS = ("mono", "code", "courier", "consol", "typewriter", "cousine")
SERIF_WORDS = ("serif", "times", "georgia", "garamond", "playfair", "slab", "libre baskerville",
               "book", "crimson", "lora", "spectral", "cormorant", "eb garamond", "bodoni", "tinos",
               "caladea", "gelasio", "cambria", "palatino", "antiqua", "merriweather",
               # a formal script has no family of classify's own, and reads nearer a serif's
               # proportions than a grotesque sans (korea-pptx's "Monotype Corsiva"); its google/fonts
               # stand-in (`adopt.SUBSTITUTES`) must read the same way under its own, spaceless stem
               "corsiva", "petitformalscript")
TEX_PREFIX = {"sans": "CMSS", "serif": "CMR", "mono": "CMTT"}
TAG_RE = re.compile(r"^b2s:(?P<slide>[^/]*)/(?P<element>.+)$")
BEAMER_SIZES: dict[tuple[int, int], tuple[float, float]] = {
    (4, 3): (362.83, 272.13), (16, 9): (453.54, 255.12), (16, 10): (453.54, 283.46)}
ALIGN_OF: dict[str, Align] = {"START": "left", "CENTER": "center", "END": "right", "JUSTIFIED": "left"}
SCRIPT_OF: dict[str, Script] = {"SUPERSCRIPT": "super", "SUBSCRIPT": "sub"}
VALIGN: dict[str, VAlign] = {"TOP": "top", "MIDDLE": "middle", "BOTTOM": "bottom"}


# ------------------------------------------------------------------------------ reading the answer


def _obj(v: Json) -> JsonObject:
    return part(v, WHERE)


def _objs(v: Json) -> list[JsonObject]:
    return parts(v, WHERE)


def _wrong(v: Json, what: str) -> JsonShapeError:
    return JsonShapeError(f"{WHERE}: {what} was expected, found {type(v).__name__}")


def _num(v: Json, absent: float) -> float:
    """A number as the answer has it (an int stays an int: the JSON written keeps it); `absent` for none."""
    if v is None:
        return absent
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _wrong(v, "a number")
    return v


def _opt_num(v: Json) -> float | None:
    return None if v is None else _num(v, 0.0)


def _int(v: Json, absent: int) -> int:
    if v is None:
        return absent
    if isinstance(v, bool) or not isinstance(v, int):
        raise _wrong(v, "an integer")
    return v


def _str(v: Json) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise _wrong(v, "a string")
    return v


def _text(v: Json, absent: str) -> str:
    s = _str(v)
    return absent if s is None else s


def _bool(v: Json, absent: bool) -> bool:
    if v is None:
        return absent
    if not isinstance(v, bool):
        raise _wrong(v, "a boolean")
    return v


def dim(d: Json) -> float:
    """A Dimension of the answer's JSON in pt (0 for none)."""
    if not isinstance(d, dict) or "magnitude" not in d:
        return 0.0
    return _num(d["magnitude"], 0.0) / (EMU_PER_PT if d.get("unit", "EMU") == "EMU" else 1)


def _dimension(d: Dimension | None) -> float:
    """`dim` of a typed one (an element's size)."""
    if d is None or "magnitude" not in d:
        return 0.0
    return d["magnitude"] / (EMU_PER_PT if d.get("unit", "EMU") == "EMU" else 1)


def _wh(pe: PageElement) -> tuple[float, float]:
    size = pe.get("size")
    if size is None:
        return 0.0, 0.0
    return _dimension(size.get("width")), _dimension(size.get("height"))


def _layout_id(page: Page) -> str | None:
    props = page.get("slideProperties")
    return None if props is None else props.get("layoutObjectId")


def _layout_master(page: Page) -> str | None:
    props = page.get("layoutProperties")
    return None if props is None else props.get("masterObjectId")


def _shape(pe: PageElement) -> JsonObject:
    return _obj(pe.get("shape"))


def _text_elements(text: JsonObject) -> list[JsonObject]:
    return _objs(text.get("textElements"))


# ------------------------------------------------------------------------------ geometry


def affine(t: AffineTransform | None) -> Affine:
    """[a, b, tx, d, e, ty] in pt; fields the API leaves out are zero, a missing transform is identity."""
    if not t:
        return IDENTITY
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return (t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit,
            t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit)


def compose(p: Affine, c: Affine) -> Affine:
    a, b, tx, d, e, ty = p
    a2, b2, tx2, d2, e2, ty2 = c
    return (a * a2 + b * d2, a * b2 + b * e2, a * tx2 + b * ty2 + tx, d * a2 + e * d2, d * b2 + e * e2,
            d * tx2 + e * ty2 + ty)


def box(m: Affine, w: float, h: float) -> Box:
    xs = [m[0] * x + m[1] * y + m[2] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [m[3] * x + m[4] * y + m[5] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    return min(xs), min(ys), max(xs), max(ys)


def _scaled(b: Box, scale: float) -> Box:
    return round(b[0] / scale, 2), round(b[1] / scale, 2), round(b[2] / scale, 2), round(b[3] / scale, 2)


def frame(m: Affine, w: float, h: float, scale: float) -> Frame:
    """An element's own frame, in PDF pt: `size` (its displayed width and height, before rotation),
    `origin` (where its (0,0) corner lands on the page), `matrix` (the transform's linear part with
    the size taken out: a rotation, possibly mirrored or sheared), `rotation` (degrees clockwise),
    `flip` (mirrored left to right, then turned by `rotation`) and `box` (the upright box about the
    centre that the element would have if it were not turned).

    `box(m, w, h)` - the bounding box - is where a turned element's ink is, but not its size: a text
    box turned 30 degrees drawn in it is upright and too big. Only `adopt` asks for this."""
    a, b, tx, d, e, ty = m
    sx, sy = math.hypot(a, d), math.hypot(b, e)
    if sx < 1e-9 and sy < 1e-9:
        q = (1.0, 0.0, 0.0, 1.0)
    elif sx < 1e-9:                         # a line stored with no width: its first axis is the second's normal
        q = (e / sy, b / sy, -b / sy, e / sy)
    elif sy < 1e-9:                         # ... or no height (most straight connectors)
        q = (a / sx, -d / sx, d / sx, a / sx)
    else:
        q = (a / sx, b / sy, d / sx, e / sy)
    W, H = sx * w, sy * h
    if q[0] * q[3] - q[1] * q[2] >= 0:
        rot, flip = math.degrees(math.atan2(q[2], q[0])), False
    else:                                   # R(rot) . mirror(x): the first axis points the other way
        rot, flip = math.degrees(math.atan2(-q[2], -q[0])), True
    rot = (rot + 180) % 360 - 180
    cx, cy = tx + q[0] * W / 2 + q[1] * H / 2, ty + q[2] * W / 2 + q[3] * H / 2
    return Frame(size=(round(W / scale, 3), round(H / scale, 3)), origin=(round(tx / scale, 3), round(ty / scale, 3)),
                 matrix=(round(q[0], 6), round(q[1], 6), round(q[2], 6), round(q[3], 6)), rotation=round(rot, 3),
                 flip=flip, box=_scaled_to((cx - W / 2, cy - H / 2, cx + W / 2, cy + H / 2), scale, 3),
                 shear=True if abs(q[0] * q[1] + q[2] * q[3]) > 1e-3 else None)


def _scaled_to(b: Box, scale: float, digits: int) -> Box:
    return (round(b[0] / scale, digits), round(b[1] / scale, digits), round(b[2] / scale, digits),
            round(b[3] / scale, digits))


def turned(m: Affine) -> bool:
    """Anything but a plain scale and shift: rotated, mirrored or sheared."""
    return abs(m[1]) > 1e-9 or abs(m[3]) > 1e-9 or m[0] < 0 or m[4] < 0


def walk_elements(elements: Sequence[PageElement]) -> Iterator[PageElement]:
    for pe in elements:
        yield pe
        yield from walk_elements(children(pe, WHERE))


def flatten(elements: Sequence[PageElement], parent: Affine,
            group: str | None) -> Iterator[tuple[PageElement, Affine, str | None]]:
    """(page element, absolute matrix, top group id) for every leaf element."""
    for pe in elements:
        m = compose(parent, affine(pe.get("transform")))
        if "elementGroup" in pe:
            yield from flatten(children(pe, WHERE), m, group or object_id(pe))
        else:
            yield pe, m, group


# ------------------------------------------------------------------------------ colours and styles


@dataclass(frozen=True, kw_only=True)
class Outline:
    """A shape's outline as adopt draws it (`outline_props`)."""
    color: str | None
    """None when not drawn."""
    weight: float
    alpha: float | None
    dash: str | None


def outline_props(outline: JsonObject, scale: float, scheme: Mapping[str, str]) -> Outline:
    """A shape's outline as adopt draws it: colour (None when not drawn), weight in PDF pt (Slides'
    default 0.75 pt when the deck says nothing), alpha and dash style."""
    fill = _obj(_obj(outline.get("outlineFill")).get("solidFill"))
    alpha = _num(fill.get("alpha"), 1.0)
    # an outline at alpha 0 is how Slides hides one without switching it off
    drawn = outline.get("propertyState", "RENDERED") == "RENDERED" and bool(fill) and alpha > 0.004
    return Outline(color=rgb_hex(fill.get("color"), scheme) if drawn else None,
                   weight=round((dim(outline.get("weight")) or 0.75) / scale, 3),
                   alpha=round(alpha, 4) if drawn and alpha < 1.0 else None,
                   dash=_str(outline.get("dashStyle")) if drawn and outline.get("dashStyle", "SOLID") != "SOLID"
                   else None)


def _rgb(rgb: JsonObject) -> str:
    return "#" + "".join(f"{round(_num(rgb.get(k), 0.0) * 255):02x}" for k in ("red", "green", "blue"))


def rgb_hex(color: Json, scheme: Mapping[str, str]) -> str | None:
    if not color:
        return None
    c = _obj(color)
    if "opaqueColor" in c:
        c = _obj(c["opaqueColor"])
    if "themeColor" in c:
        return scheme.get(_text(c["themeColor"], ""))
    rgb = c.get("rgbColor")
    if rgb is None:
        return None
    return _rgb(_obj(rgb))


def design_for(size: float) -> int:
    """Computer Modern optical size LaTeX picks for a font size."""
    return 8 if size < 8.5 else 9 if size < 9.5 else 10 if size < 11.5 else 12 if size < 17 else 17


def family_of(font: str) -> Family:
    """Which of classify's three families a Slides font name belongs to."""
    if font in FAMILY_FOR_FONT:
        return FAMILY_FOR_FONT[font]
    # a Mincho, Song, Ming or Batang face is a serif (ja-schedule's MS Mincho read as sans)
    cjk = cjk_font(font)
    if cjk:
        return "serif" if "Serif" in cjk[0] or "Myeongjo" in cjk[0] else "sans"
    low = font.lower()
    if any(w in low for w in MONO_WORDS):
        return "mono"
    if any(w in low for w in SERIF_WORDS):
        return "serif"
    return "sans"


def pdf_size(fonts: FontMapper, family: str, slides_size: float, bold: bool, italic: bool, scale: float,
             font: str | None, text: str, smallcaps: bool, script: bool) -> tuple[float, str]:
    """Inverse of FontMapper: (PDF font size, a TeX font name that maps like it). `family` is the
    Slides font; `font` the TeX font when it is known (else one of CM's optical cuts is picked). The
    run's text, small caps and script say what FontMapper sized it by (a number, a small-caps face)."""
    tex_family = FAMILY_FOR_FONT.get(family)
    if tex_family is None:  # a Google font used as-is (or Arial for a box the user added)
        return round(slides_size / scale, 2), font or family.replace(" ", "")
    size = slides_size / scale
    name = "" if font is None else font
    for _ in range(4):
        if font is None:
            name = f"{TEX_PREFIX[tex_family]}{design_for(size)}"
        run: JsonObject = {"font": name, "family": tex_family, "size": size, "bold": bold, "italic": italic,
                           "text": text, "smallcaps": smallcaps, "script": "sub" if script else None}
        z = fonts(run, scale)[1]
        if z <= 0:
            break
        size = size * slides_size / z if abs(z - slides_size) > 0.05 else size
    # FontMapper rounds to 0.1 pt: one more exact step without rounding
    return round(size, 2), name


class StyleResolver:
    """Text style fields a run leaves out come from its placeholder's parents (layout, master)."""

    def __init__(self, pres: Presentation) -> None:
        self.by_id: dict[str, PageElement] = {}
        self.bare_imports = False   # deck_ir sets it: `imports_lack_insets`
        layouts, masters = pres.get("layouts") or [], pres.get("masters") or []
        for page in layouts + masters:
            for pe in walk_elements(page.get("pageElements") or []):
                self.by_id[object_id(pe)] = pe
        # Each master has its own colour scheme, and a theme colour means what the scheme of the
        # page's own master says (a foreign deck can carry several masters that disagree: a dark
        # variant, a template's leftover). `scheme` is the current page's (`use_page`); until a
        # page is chosen it is the first master's, filled in by the others where it has no colour.
        self.schemes: dict[str, dict[str, str]] = {}
        self.scheme: dict[str, str] = {}
        for master in masters:
            scheme = _obj(_obj(master.get("pageProperties")).get("colorScheme"))
            own = {as_str(c.get("type"), WHERE): _rgb(_obj(c.get("color"))) for c in _objs(scheme.get("colors"))}
            self.schemes[object_id(master)] = own
            for k, v in own.items():
                self.scheme.setdefault(k, v)
        self.default = self.scheme
        self.master_of: dict[str, str | None] = {object_id(p): _layout_master(p) for p in layouts}

    def scheme_for(self, page: Page) -> dict[str, str]:
        """The colour scheme of the master a slide, layout or master page descends from."""
        oid = page.get("objectId")
        if oid not in self.schemes:
            props = page.get("slideProperties")
            layout = (None if props is None else props.get("layoutObjectId")) or oid
            said = None if props is None else props.get("masterObjectId")
            oid = said or (None if layout is None else self.master_of.get(layout))
        own = None if oid is None else self.schemes.get(oid)
        return {**self.default, **own} if own else self.default

    def use_page(self, page: Page) -> None:
        self.scheme = self.scheme_for(page)

    def inherited_fill(self, pe: PageElement) -> JsonObject | None:
        """A placeholder's fill whose `propertyState` is INHERIT, as its layout (else master) sets
        it; None when no parent sets one. pycon-2019's body boxes are grey and ml-vs-stats' titles
        sit on a dark bar only through their layout's placeholder."""
        for parent in reversed(self.chain(pe)):
            fill = _obj(_shape(parent).get("shapeProperties")).get("shapeBackgroundFill")
            if fill is not None:
                said = _obj(fill)
                if said.get("propertyState") != "INHERIT":
                    return said
        return None

    def chain(self, pe: PageElement) -> list[PageElement]:
        """The placeholder's parents, nearest last (master, then layout)."""
        out: list[PageElement] = []
        cur = pe
        for _ in range(4):
            parent = _str(_obj(_shape(cur).get("placeholder")).get("parentObjectId"))
            if not parent or parent not in self.by_id:
                break
            cur = self.by_id[parent]
            out.append(cur)
        return list(reversed(out))

    @staticmethod
    def _level_paragraph(el: PageElement, level: int) -> tuple[JsonObject, JsonObject]:
        """(paragraph marker, first run style) of a parent placeholder's paragraph for list level
        `level`: a BODY placeholder holds one empty paragraph per nesting level, each styled for
        its level (the cs161 decks' master: 18 pt at level 0, 14 pt below it, and 12 pt below every
        paragraph). Reading only the first one set every sub-item in the level-0 size, which broke
        all their lines in other places."""
        found: list[tuple[JsonObject, JsonObject | None]] = []      # (marker, its first run's style)
        for te in _text_elements(_obj(_shape(el).get("text"))):
            if "paragraphMarker" in te:
                found.append((_obj(te["paragraphMarker"]), None))
            elif "textRun" in te and found and found[-1][1] is None:
                found[-1] = (found[-1][0], _obj(_obj(te["textRun"]).get("style")))
        for pm, st in found:
            if _int(_obj(pm.get("bullet")).get("nestingLevel"), 0) == level:
                return pm, st or {}
        return (found[0][0], found[0][1] or {}) if found else ({}, {})

    def parent_style(self, pe: PageElement, level: int) -> JsonObject:
        style: JsonObject = {}
        for el in self.chain(pe):
            style.update(self._level_paragraph(el, level)[1])
        return style

    def parent_paragraph_style(self, pe: PageElement, level: int) -> JsonObject:
        """What a paragraph's own `paragraphMarker.style` leaves out, from the same parents.

        A placeholder inherits how its paragraphs sit, not only how their letters look: the DevFest
        template centres its subtitle on the master and every slide using it says nothing at all, so
        reading only the slide makes centred text left-aligned - which no later round can put right,
        because the loop has no translator for alignment at all."""
        style: JsonObject = {}
        for el in self.chain(pe):
            style.update(_obj(self._level_paragraph(el, level)[0].get("style")))
        return style

    def parent_bullet_style(self, pe: PageElement, level: int) -> JsonObject:
        """The text style the parents' lists give a bullet at `level` (colour, size, font)."""
        style: JsonObject = {}
        for el in self.chain(pe):
            for lst in _obj(_obj(_shape(el).get("text")).get("lists")).values():
                style.update(_obj(_obj(_obj(_obj(lst).get("nestingLevel")).get(str(level))).get("bulletStyle")))
        return style

    def parent_shape_property(self, pe: PageElement, key: str) -> Json:
        """A shape property the element leaves out (`contentAlignment`, `autofit`), from its parents."""
        value: Json = None
        for el in self.chain(pe):
            value = _obj(_shape(el).get("shapeProperties")).get(key, value)
        return value


@dataclass(frozen=True, kw_only=True)
class BaseStyle:
    """The text style a run of one list level has where it says nothing (`base_style`)."""
    font_family: str
    font_size: float
    bold: bool
    italic: bool
    color: str
    weight: float | None


def base_style(pe: PageElement, resolver: StyleResolver, level: int) -> BaseStyle:
    """The text style a run of list level `level` has where it says nothing, from the parents."""
    parent = resolver.parent_style(pe, level)
    family = _text(parent.get("fontFamily"), "Arial")
    bold = _bool(parent.get("bold"), False)
    weight = None
    if parent.get("weightedFontFamily"):
        weighted = _obj(parent["weightedFontFamily"])
        family = as_str(weighted.get("fontFamily"), WHERE)
        weight = _num(weighted.get("weight"), 400)
        if weight >= 600:
            bold = True
    color = "#000000"
    if parent.get("foregroundColor"):
        color = rgb_hex(parent["foregroundColor"], resolver.scheme) or color
    return BaseStyle(font_family=family, font_size=dim(parent["fontSize"]) if parent.get("fontSize") else 18.0,
                     bold=bold, italic=_bool(parent.get("italic"), False), color=color, weight=weight)


# ------------------------------------------------------------------------------ text


@dataclass(frozen=True, kw_only=True)
class ReadRun:
    """A run as read, with the Slides font and size it is drawn in (a box's lines are sized by them)."""
    run: TargetRun
    slides_font: str
    slides_size: float


@dataclass(frozen=True, kw_only=True)
class ReadParagraph:
    """A paragraph as read, in Slides pt where the deck says pt (`indent_*`, `space_*`)."""
    align: Align
    justified: bool
    level: int
    bullet: TargetBullet | None
    direction: Literal["rtl"] | None
    indent_start: float
    indent_first: float
    indent_end: float
    line_spacing: float
    space_above: float
    space_below: float
    spacing_mode: str | None
    size: float
    tab_x0: float | None
    runs: tuple[ReadRun, ...]
    newline: ReadRun | None
    """An empty paragraph's height is its own newline's style: the run it is filled with, if kept."""


class _Open:
    """A paragraph while its runs are read."""

    def __init__(self, head: ReadParagraph, bullet_style: JsonObject, base: BaseStyle) -> None:
        self.head = head
        self.bullet_style = bullet_style
        self.base = base
        self.runs: list[ReadRun] = []
        self.newline: ReadRun | None = None


def _line_size(r: ReadRun) -> float:
    return line_size({"text": r.run.text, "smallcaps": r.run.smallcaps}, r.slides_size)


def _blank(r: ReadRun) -> ReadRun:
    """A run standing for an empty line: a space in `r`'s style."""
    return replace(r, run=replace(r.run, text=" ", link=None, underline=False, strike=False, highlight=None))


def _nearest(levels: Sequence[float], x: float) -> int:
    return min(range(len(levels)), key=lambda i: abs(levels[i] - x))


def text_paragraphs(pe: PageElement, text: JsonObject, resolver: StyleResolver, fonts: FontMapper, scale: float,
                    keep_blank: bool, font_scale: float, spacing_cut: float, keep_trailing: bool,
                    foreign: bool) -> list[ReadParagraph]:
    """`font_scale` and `spacing_cut` are the box's autofit (`shrink text on overflow`, or a .pptx's
    normAutofit): Slides draws every run at `font_scale` times its size and takes `spacing_cut` off
    every paragraph's line spacing. The cs161 decks' titles say 28 pt and are drawn at 25.2: the
    thumbnail's cap height is 18.0 pt, Arial's is 0.716 em.

    `foreign`: every font is the person's own, Lato, PT Serif and Roboto Mono included - these are
    the converter's stand-ins for Computer Modern only in a deck it wrote, and read as such, a deck
    written in Roboto Mono (intro-lecture's code) came back as CMTT9 and was set in Courier."""
    bases: dict[int, BaseStyle] = {}
    opened: list[_Open] = []
    cur: _Open | None = None
    lists = _obj(text.get("lists"))
    for te in _text_elements(text):
        if "paragraphMarker" in te:
            pm = _obj(te["paragraphMarker"])
            bullet = _obj(pm.get("bullet"))
            nesting = _int(bullet.get("nestingLevel"), 0) if bullet else 0
            base = bases.setdefault(nesting, base_style(pe, resolver, nesting))
            st = {**resolver.parent_paragraph_style(pe, nesting), **_obj(pm.get("style"))}
            glyph = _text(bullet.get("glyph"), "") if bullet else ""
            bstyle: JsonObject = {}
            if bullet:
                # what a bullet looks like: the parents' list level, the box's own list level, then
                # the paragraph's own bullet style; what none of them says is the first run's
                own = _obj(_obj(_obj(_obj(lists.get(_text(bullet.get("listId"), ""))).get("nestingLevel")).get(
                    str(nesting))).get("bulletStyle"))
                bstyle = {**resolver.parent_bullet_style(pe, nesting), **own, **_obj(bullet.get("bulletStyle"))}
            # `align` is where the lines sit on the page. START and END are the paragraph's own
            # start and end, so in a right-to-left paragraph (Hebrew, Arabic) START is flush right.
            rtl = st.get("direction") == "RIGHT_TO_LEFT"
            align: Align = ALIGN_OF.get(_text(st.get("alignment"), "START"), "left")
            if rtl:
                align = "right" if align == "left" else "left" if align == "right" else align
            head = ReadParagraph(
                align=align, justified=st.get("alignment") == "JUSTIFIED", level=0,
                bullet=(TargetBullet(kind="number" if re.search(r"\d|[a-z]\.|[ivx]+\.", glyph) else "glyph",
                                     text=glyph, bbox=ABSENT, color=None, size=None, font_family=None, font=None,
                                     bold=None) if bullet else None),
                direction="rtl" if rtl else None,
                indent_start=dim(st.get("indentStart")), indent_first=dim(st.get("indentFirstLine")),
                indent_end=dim(st.get("indentEnd")),
                line_spacing=max(0.1, (_num(st.get("lineSpacing"), 100) or 100) / 100 - spacing_cut),
                space_above=dim(st.get("spaceAbove")), space_below=dim(st.get("spaceBelow")),
                spacing_mode=_str(st.get("spacingMode")), size=0.0, tab_x0=None, runs=(), newline=None)
            cur = _Open(head, bstyle, base)
            opened.append(cur)
        elif "textRun" in te or "autoText" in te:
            if cur is None:
                continue
            base = cur.base
            tr = _obj(te.get("textRun") or te.get("autoText"))
            content = _text(tr.get("content"), "")
            if ZWSP in content:
                # (the break emit writes in front of a hole, emit.HOLE_BREAK: no character of
                # the text, and pull writes nothing for it)
                content = content.replace(ZWSP, "")
                if not content:
                    continue
            st = _obj(tr.get("style"))
            weighted = _obj(st.get("weightedFontFamily"))
            family = _str(weighted.get("fontFamily")) or _str(st.get("fontFamily")) or base.font_family
            size = (dim(st.get("fontSize")) or base.font_size) * font_scale
            bold: Json = st.get("bold", base.bold)
            weight = _opt_num(weighted.get("weight")) or base.weight or 400
            said_bold = st.get("bold")
            if said_bold is not None and "weightedFontFamily" not in st:
                weight = 700 if said_bold else 400
            # our own decks write OPTICAL_WEIGHT (600) on a regular small sans cut
            # (FontMapper.optical_weight); the decks of wave 4 wrote 800, which Slides reads back
            # `bold: true`: either is a regular 6 pt cut, not a bold one pull would write as \textbf
            # (the converter's own bold is 700)
            optical = not foreign and weight in OPTICAL_WEIGHTS_READ and FAMILY_FOR_FONT.get(family) == "sans"
            if optical:
                bold = False
            elif weight >= 600:
                bold = True
            unsure = foreign and base.bold and said_bold is False and weighted.get("weight") == 400
            italic: Json = st.get("italic", base.italic)
            color = rgb_hex(st.get("foregroundColor"), resolver.scheme) or base.color
            if foreign:
                psize, font = round(size / scale, 2), family.replace(" ", "")
            else:
                # (the weight says the run was a sans cut of 6 pt or less, which every such cut
                # maps alike, OPTICAL_WIDTH_MAX: inverted as CM's 8 pt cut it came back 6% large)
                psize, font = pdf_size(fonts, family, size, bool(bold), bool(italic), scale,
                                       ("SFSI0600" if italic else "SFSS0600") if optical else None, content,
                                       bool(st.get("smallCaps")),
                                       st.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT"))
            link = _obj(st.get("link"))
            page_link = _str(link.get("pageObjectId"))
            text_part = content.rstrip("\n") if content.endswith("\n") else content
            if not text_part:
                if not cur.runs:
                    # an empty line's height is its own newline's style
                    cur.newline = ReadRun(run=TargetRun(
                        text=" ", font=font, family=family_of(family), size=psize, bold=bool(bold),
                        italic=bool(italic), smallcaps=False, color=color, link=None, script=None, underline=False,
                        strike=False, highlight=None, hole=None, weight=None, weight_unsure=None),
                        slides_font=family, slides_size=size)
                continue
            hole = None
            if family == "Roboto Mono" and text_part.strip(" ") == "" and " " in text_part:
                hole = round(len(text_part) * 0.6 * size / scale, 2)
            run = TargetRun(
                text=" " if hole is not None else text_part, font=font, family=family_of(family), size=psize,
                bold=bool(bold), italic=bool(italic), smallcaps=bool(st.get("smallCaps")), color=color,
                link=_str(link.get("url")) or (f"#slide={page_link}" if page_link else None),
                script=SCRIPT_OF.get(_text(st.get("baselineOffset"), "")),
                underline=bool(st.get("underline")), strike=bool(st.get("strikethrough")),
                highlight=rgb_hex(st.get("backgroundColor"), resolver.scheme), hole=hole,
                # a weight between (or beyond) regular and bold, which `bold` can only round: adopt
                # sets it in an instance of that weight where it can cut one (fontfetch.weight_file)
                weight=int(weight) if foreign and weight not in (400, 700) else None,
                weight_unsure=True if unsure else None)     # `thumbnail_weights` decides
            cur.runs.append(ReadRun(run=run, slides_font=family, slides_size=size))
    paragraphs: list[ReadParagraph] = []
    for o in opened:
        runs = merge_runs(o.runs)
        bullet = o.head.bullet
        bstyle = o.bullet_style
        if bullet is not None and runs:
            first = runs[0]
            bsize = dim(bstyle.get("fontSize")) * font_scale
            weighted = _obj(bstyle.get("weightedFontFamily"))
            family = _str(weighted.get("fontFamily")) or _str(bstyle.get("fontFamily"))
            bold_said: Json = bstyle.get("bold", first.run.bold)
            bullet = replace(
                bullet, color=rgb_hex(bstyle.get("foregroundColor"), resolver.scheme) or first.run.color,
                size=round(first.run.size * bsize / first.slides_size, 2) if bsize and first.slides_size
                else first.run.size,
                font_family=family_of(family) if family else first.run.family, font=family or first.run.font,
                bold=bool(bold_said))
        text_of = "".join(r.run.text for r in runs)
        paragraphs.append(replace(
            o.head, runs=tuple(runs), size=max((r.run.size for r in runs), default=0.0), bullet=bullet,
            tab_x0=o.head.indent_start if "\t" in text_of and bullet is None else None, newline=o.newline))
    trailing = keep_trailing and keep_blank and any(p.runs for p in paragraphs)
    if not trailing:
        paragraphs = [p for p in paragraphs if p.runs or p is not paragraphs[-1]]
    if trailing:
        # Under a middle- or bottom-aligned stack the empty lines a person left at the end are
        # height all the same: ap-bio-stats' bodies end on an empty 24 pt line at 80% after 7 pt,
        # and their text sits 15 pt higher than the stack without it would.
        last = max(i for i, p in enumerate(paragraphs) if p.runs)
        for i in range(last + 1, len(paragraphs)):
            p = paragraphs[i]
            paragraphs[i] = replace(p, runs=(_blank(p.newline or paragraphs[last].runs[-1]),))
    if keep_blank:
        # A blank line a person left in a text box is vertical space they chose, and dropping it
        # pulls everything under it up by a line - of the 717 paragraphs of the DevFest template,
        # 282 are blank. A PDF has no empty paragraph, only the gap one leaves, so classify never
        # makes one and `pull`'s IR must not either; a foreign deck is read from the deck itself,
        # where the blank line is still there to be read. It becomes a space in the style of its own
        # newline, which is what Slides sizes it by (creandum-board's 8 pt spacers above 40 pt
        # figures: 0.760 -> 0.845), else of the paragraph it stands above.
        if not trailing:
            last = max((i for i, p in enumerate(paragraphs) if p.runs), default=-1)
            del paragraphs[last + 1:]                   # under a top-aligned stack they push nothing down
        below: ReadRun | None = None
        for i in reversed(range(len(paragraphs))):
            p = paragraphs[i]
            if p.runs:
                below = p.runs[0]
            elif below is not None:
                blank = _blank(p.newline or below)
                paragraphs[i] = replace(p, runs=(blank,), size=blank.run.size)
    # bullet levels as classify counts them: clusters (2 pt apart) of the bullets' left edges
    levels: list[float] = []
    for x in sorted({p.indent_first / scale for p in paragraphs if p.bullet is not None}):
        if not levels or x - levels[-1] > 2:
            levels.append(x)
    return [replace(p, level=_nearest(levels, p.indent_first / scale)) if p.bullet is not None else p
            for p in paragraphs]


MERGE_KEYS = ("font", "size", "bold", "italic", "color", "link", "script", "underline", "strike", "highlight",
              "smallcaps", "weight")


def _merge_key(r: TargetRun) -> tuple[object, ...]:
    return (r.font, r.size, r.bold, r.italic, r.color, r.link, r.script, r.underline, r.strike, r.highlight,
            r.smallcaps, r.weight)


def merge_runs(runs: Sequence[ReadRun]) -> list[ReadRun]:
    """Neighbouring runs that look alike (`MERGE_KEYS`) are one; a hole never merges."""
    out: list[ReadRun] = []
    for r in runs:
        if out and not r.run.hole and not out[-1].run.hole and _merge_key(out[-1].run) == _merge_key(r.run) \
                and out[-1].run.weight_unsure == r.run.weight_unsure:
            prev = out[-1]
            out[-1] = replace(prev, run=replace(prev.run, text=prev.run.text + r.run.text))
        else:
            out.append(r)
    return out


ZERO_INSET_SLACK = 5.0      # Slides pt: less room than this beside the text of a box that fits it = no insets
PITCH_EM = 1.19             # docs/calibration.md: the line pitch at lineSpacing 100, every font


def zero_insets(paragraphs: Sequence[ReadParagraph], height: float) -> bool:
    """Does a box that resizes to fit its text (SHAPE_AUTOFIT) have no insets? The API does not say
    (insets are read-only and never reported), but such a box's stored height is its text's height
    plus its top and bottom insets, and Slides' default ones leave 14.7 pt (median over 418 boxes of
    the corpus measured on the thumbnails). Templates made in PowerPoint or Canva (the SlidesCarnival
    decks, sc-memphis, parts of gdg24 and devfest2020) set every inset to 0: their text sits 6.5 pt
    higher and 6.7 pt further left than Slides' defaults put it, and the box is as tall as its
    lines. Counting each paragraph as one line gives the least height the text can need, so a box
    that leaves less than ZERO_INSET_SLACK beside even that has no room for insets (484 boxes, 94%
    of them moved like that on the thumbnails; the rest are boxes whose height went stale)."""
    need = 0.0
    for p in paragraphs:
        if not p.runs:
            continue
        z = max(_line_size(r) for r in p.runs)
        need += PITCH_EM * z * p.line_spacing + p.space_above + p.space_below
    return need > 0 and height - need < ZERO_INSET_SLACK


def imported(shape: JsonObject) -> bool:
    """Was this text box made by a .pptx import? Drive's importer writes spacingMode NEVER_COLLAPSE on
    the paragraphs, Slides' own boxes say COLLAPSE_LISTS. Among the boxes that resize to fit their
    text and whose height does not give their insets away (wrapped lines), those an import made had
    no insets 169 times in 186 on the corpus thumbnails, Slides' own ones had theirs 375 times in 376."""
    for te in _text_elements(_obj(shape.get("text"))):
        if "paragraphMarker" in te:
            return _obj(_obj(te["paragraphMarker"]).get("style")).get("spacingMode") == "NEVER_COLLAPSE"
    return False


def _autofit(shape: JsonObject) -> JsonObject:
    return _obj(_obj(shape.get("shapeProperties")).get("autofit"))


def imports_lack_insets(pres: Presentation, resolver: StyleResolver, fonts: FontMapper, scale: float) -> bool:
    """Did the .pptx this deck was imported from set its text insets to 0? `imported` alone does not
    say: gdg24's template came through an import too and kept Slides' insets (its 9 imported boxes
    that grow with their text all sit where default insets put them). The deck's own boxes do: among
    its imported boxes that resize to fit their text, those whose height leaves no room for insets
    (`zero_insets`) prove the template's choice - 9 of 15 in cs161-net, 34 to 119 in each
    SlidesCarnival deck, none of gdg24's 9. At least a quarter of them, and two."""
    seen = proven = 0
    for page in (pres.get("slides") or []) + (pres.get("layouts") or []) + (pres.get("masters") or []):
        for pe, m, _ in flatten(page.get("pageElements") or [], IDENTITY, None):
            shape = _shape(pe)
            autofit = _autofit(shape)
            if "size" not in pe or autofit.get("autofitType") != "SHAPE_AUTOFIT" or not imported(shape):
                continue
            paragraphs = text_paragraphs(pe, _obj(shape.get("text")), resolver, fonts, scale, True,
                                         _num(autofit.get("fontScale"), 1.0) or 1.0,
                                         _num(autofit.get("lineSpacingReduction"), 0.0) or 0.0, False, True)
            if not any(p.runs for p in paragraphs):
                continue
            w, h = _wh(pe)
            _, y0, _, y1 = box(m, w, h)
            seen += 1
            proven += zero_insets(paragraphs, y1 - y0)
    return proven >= 2 and proven * 4 >= seen


@dataclass(frozen=True, kw_only=True)
class TextBody:
    """What a text box's words say, before the element around them (`text_element`)."""
    role: str
    bbox: Box
    anchor: Point
    wrap_width: float
    placeholder: str | None
    paragraphs: tuple[TargetParagraph, ...]
    box: TextBox


def text_element(pe: PageElement, m: Affine, resolver: StyleResolver, fonts: FontMapper, scale: float,
                 page_w: float, foreign: bool) -> TextBody | None:
    shape = _shape(pe)
    props = _obj(shape.get("shapeProperties"))
    # Read from the parents for a foreign deck only: emit's placeholders were calibrated against what
    # the slide itself says (PPTX_TITLE_DY), and `pull` must keep reading them that way.
    autofit = _obj(props.get("autofit") or (resolver.parent_shape_property(pe, "autofit") if foreign else None))
    font_scale = _num(autofit.get("fontScale"), 1.0) or 1.0
    spacing_cut = _num(autofit.get("lineSpacingReduction"), 0.0) or 0.0
    # a placeholder sits where its layout says when it says nothing itself (title placeholders are
    # often bottom-aligned there)
    content = _str(props.get("contentAlignment")) or \
        (_str(resolver.parent_shape_property(pe, "contentAlignment")) if foreign else None) or "TOP"
    paragraphs = text_paragraphs(pe, _obj(shape.get("text")), resolver, fonts, scale, foreign, font_scale,
                                 spacing_cut, content in ("MIDDLE", "BOTTOM"), foreign)
    if not any(p.runs for p in paragraphs):
        return None
    w, h = _wh(pe)
    x0, y0, x1, y1 = box(m, w, h)
    placeholder = _str(_obj(shape.get("placeholder")).get("type"))
    first = next(p for p in paragraphs if p.runs)
    z = max(_line_size(r) for r in first.runs)
    aligns = {p.align for p in paragraphs if p.runs}
    align: Align = aligns.pop() if len(aligns) == 1 else "left"
    grows = autofit.get("autofitType") == "SHAPE_AUTOFIT"
    # only a foreign deck: the converter's own boxes are created by the API, with Slides' insets
    bare = foreign and grows and (zero_insets(paragraphs, y1 - y0) or (resolver.bare_imports and imported(shape)))
    pad_x, top = (0.0, 0.0) if bare else (PAD_X, BASELINE_A)
    span = None
    snap = foreign and page_w * scale <= SNAP_PAGE
    if foreign and content in ("MIDDLE", "BOTTOM"):
        baseline, span = stacked_baseline(paragraphs, content, y0, y1, top, snap, _str(shape.get("shapeType")),
                                          grows)
    elif content == "MIDDLE":
        baseline = (y0 + y1) / 2 + MIDDLE_BASELINE_EM * z
    else:
        baseline = y0 + top + ASCENT_EM * z + extra_above(first.line_spacing, z) + first.space_above
        # (the converter's own placeholders, which pull reads: a foreign deck's stand where Slides'
        # insets put them, as adopt lays them out - the thumbnails agree)
        if placeholder in ("TITLE", "CENTERED_TITLE", "SUBTITLE") and not bare and not foreign:
            baseline -= PPTX_TITLE_DY
    # emit puts the box PAD_X left of the text's (or bullet's) left edge; centred and right-aligned
    # boxes are widened symmetrically / to the left
    if align == "left":
        x = x0 + pad_x
    elif align == "center":
        x = (x0 + x1) / 2
    elif align == "right":
        x = x1 - pad_x
    else:
        assert_never(align)
    lines_x1 = round((x1 - pad_x) / scale, 2)
    out: list[TargetParagraph] = []
    for p in paragraphs:
        if not p.runs:
            continue
        tx0 = round((x0 + pad_x + p.indent_start) / scale, 2)
        first_line = not out
        out.append(TargetParagraph(
            align=p.align, level=p.level, bullet=None if p.bullet is None else replace(p.bullet, bbox=None),
            direction=p.direction, size=p.size, text_x0=tx0, tab_x0=p.tab_x0,
            lines=(TargetLine(baseline=round(baseline / scale, 2) if first_line else None, x0=tx0, x1=lines_x1),),
            runs=tuple(r.run for r in p.runs),
            slides=SlidesMeasures(indent_start=p.indent_start, indent_first=p.indent_first,
                                  line_spacing=p.line_spacing, space_above=p.space_above,
                                  space_below=p.space_below, indent_end=p.indent_end,
                                  spacing_mode=p.spacing_mode, justified=p.justified,
                                  font=p.runs[0].slides_font, size=p.runs[0].slides_size)))
    # `box`: what adopt needs to lay the box out as Slides does (adopt.text_box_latex). The
    # paragraphs' `slides` values are in Slides pt, so the scale that turns them into the IR's goes
    # with them.
    return TextBody(role="title" if placeholder in ("TITLE", "CENTERED_TITLE") else "body",
                    bbox=_scaled((x0, y0, x1, y1), scale), anchor=(round(x / scale, 2), round(baseline / scale, 2)),
                    wrap_width=round((x1 - x0 - 2 * pad_x) / scale, 2), placeholder=placeholder,
                    paragraphs=tuple(out),
                    box=TextBox(valign="middle" if content == "MIDDLE" else "bottom" if content == "BOTTOM" else "top",
                                scale=scale, font_scale=font_scale, grows=grows, insets=0 if bare else None,
                                span=None if span is None else round(span / scale, 2), snap=True if snap else None,
                                inset_x=None, inset_y=None))


def stacked_baseline(paragraphs: Sequence[ReadParagraph], content: str, y0: float, y1: float, inset: float,
                     snap: bool, shape_type: str | None, grows: bool) -> tuple[float, float]:
    """(first baseline, span to the last) of a middle- or bottom-aligned box's lines, Slides pt, laid
    out as adopt's `slidebox` stacks them (`adopt.box_parts`: its line boxes, the space between
    paragraphs, the last one's depth and `trailing_space`). The span counts each paragraph's lines
    as they are typed; a paragraph that wraps adds lines the box does not say, so the first
    baseline is only as good as that - but the stack's bottom (a bottom-aligned box) and its middle
    (a middle-aligned one) stand where the box puts them whatever the wrapping, and `compare`
    measures those: the first baseline plus the span, the first baseline plus half of it."""
    from .adopt import WIDE_SPACING, line_box, snapped_line_box
    paras = [p for p in paragraphs if p.runs]
    span = 0.0
    prev: tuple[ReadParagraph, float, float, float] | None = None
    above0 = 0.0
    for p in paras:
        z = max(_line_size(r) for r in p.runs)
        r = p.line_spacing or 1.0
        above, below = snapped_line_box(z, r, 1.0, snap)
        if prev is None:
            above0 = above
        else:
            # between two list items each side collapses by its own spacingMode, and the bigger wins
            listed = prev[0].bullet is not None and p.bullet is not None
            gap_below = 0 if listed and prev[0].spacing_mode != "NEVER_COLLAPSE" else prev[0].space_below or 0
            gap_above = 0 if listed and p.spacing_mode != "NEVER_COLLAPSE" else p.space_above or 0
            span += prev[1] + max(gap_below, gap_above) + above
        breaks = sum(x.run.text.count(SOFT_BREAK) for x in p.runs)
        span += breaks * (above + below)
        prev = (p, below, z, r)
    if prev is None:
        raise ValueError("stacked_baseline: a box with no words")
    last, _, z, r = prev
    depth = line_box(z, 1.0 if r >= WIDE_SPACING else r)[1]
    tail = 0.0 if shape_type == "ELLIPSE" else float(last.space_below or 0.0)
    if content == "BOTTOM":
        return y1 - inset - tail - depth - span, span
    # centred: the stack from the first paragraph's space above (not in a box that grows to fit its
    # text) and its first line's ascent to the last line's depth and tail
    lead = 0.0 if grows else float(paras[0].space_above or 0.0)
    return (y0 + y1) / 2 - (lead + above0 + span + depth + tail) / 2 + lead + above0, span


# ------------------------------------------------------------------------------ pages


def _parent_page(page: Page) -> str | None:
    """The page a page inherits from: a slide's layout, a layout's master."""
    return _layout_id(page) or _layout_master(page)


def page_background(page: Page, resolver_pages: Mapping[str, Page],
                    scheme: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(solid colour, picture url) of a page's background, following INHERIT to layout and master."""
    cur = page
    for _ in range(3):
        fill = _obj(_obj(cur.get("pageProperties")).get("pageBackgroundFill"))
        if fill and fill.get("propertyState", "RENDERED") != "INHERIT":
            if "solidFill" in fill:
                solid = _obj(fill["solidFill"])
                colour = rgb_hex(solid.get("color"), scheme)
                alpha = _num(solid.get("alpha"), 1.0)
                if colour and alpha < 1:
                    # a see-through page background shows white under it (arabic-training's master
                    # is #4bacc6 at alpha 0.247, which Slides draws as a pale #d3eaf1)
                    colour = "#" + "".join(f"{round(255 - (255 - int(colour[i:i + 2], 16)) * alpha):02x}"
                                           for i in (1, 3, 5))
                return colour, None
            if "stretchedPictureFill" in fill:
                return None, _str(_obj(fill["stretchedPictureFill"]).get("contentUrl"))
            return None, None
        parent = _parent_page(cur)
        if not parent or parent not in resolver_pages:
            break
        cur = resolver_pages[parent]
    return None, None


def parent_background(page: Page, pages: Mapping[str, Page], resolver: StyleResolver) -> tuple[str | None, str | None]:
    """The background `page`'s own layout (or its master) would draw on its own, ignoring `page`'s
    own `pageBackgroundFill` - what a slide falls through to when its own reported fill turns out not
    to be what the thumbnail shows (china-pptx: a slide-level solidFill sits over the layout's own
    radial picture, which Slides draws instead of it)."""
    parent = _parent_page(page)
    if not parent or parent not in pages:
        return None, None
    return page_background(pages[parent], pages, resolver.scheme_for(pages[parent]))


def srgb_bytes(data: bytes, fmt: str) -> bytes:
    """`data` with any embedded (non-sRGB) ICC profile applied and dropped, for a PNG or JPEG.

    A picture or a full-page `stretchedPictureFill` background Slides serves through `contentUrl`
    can carry a "Display" ICC profile whose colours are meant to be colour-managed on the way to
    the screen; LaTeX/PDF only ever draws the raw bytes, so left alone the page comes out visibly
    washed out next to Google's own thumbnail (firebase-jam slide 23's NDK background: raw
    (94, 204, 209) reads, once this profile is applied, as (16, 207, 211) - Google's own colour).
    Converting once here, at fetch time, means every downstream reader can keep assuming sRGB."""
    if fmt not in ("png", "jpeg"):
        return data
    try:
        import io
        from PIL import Image, ImageCms
        img = Image.open(io.BytesIO(data))
        icc = img.info.get("icc_profile")
        if not icc:
            return data
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if "srgb" in ImageCms.getProfileDescription(src_profile).lower().replace(" ", ""):
            return data                   # already what PDF assumes: a JPEG is not re-encoded for nothing
        srgb_profile = ImageCms.createProfile("sRGB")
        has_alpha = "A" in img.getbands()
        alpha = img.getchannel("A") if has_alpha else None
        converted = ImageCms.profileToProfile(img.convert("RGB"), src_profile, srgb_profile)
        if converted is None:
            return data
        if alpha is not None:
            converted = converted.convert("RGBA")
            converted.putalpha(alpha)
        # `profileToProfile` embeds the *target* profile's bytes in the result's own info so a
        # naive save would keep tagging it - every downstream reader here already assumes sRGB with
        # no tag at all, so drop it rather than pass it on unasked.
        converted.info.pop("icc_profile", None)
        buf = io.BytesIO()
        if fmt == "jpeg":
            converted.convert("RGB").save(buf, format="JPEG", quality=95)
        else:
            converted.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a picture with a profile PIL can't read stays as fetched
        return data


@dataclass(frozen=True, kw_only=True)
class Stashed:
    """A picture downloaded into the images folder (`stash_picture`): its file, or why not."""
    file: str | None
    sha1: str | None
    format: str | None
    error: str | None


NOT_STASHED = Stashed(file=None, sha1=None, format=None, error=None)


def stash_picture(url: str, fetch: Fetch, images: Path) -> Stashed:
    """Download a picture into `images`, sha1-named: {file, sha1, format}, or {error}."""
    try:
        # contentUrl (=s2048) gives the stored picture byte for byte, as the .pptx export does;
        # crop, transparency, rotation and outline are not baked into it (tools/probe_images.py)
        data = fetch(url)
        fmt = image_format(data)
        data = srgb_bytes(data, fmt)
        sha = hashlib.sha1(data).hexdigest()
        images.mkdir(parents=True, exist_ok=True)
        path = images / f"{sha[:16]}.{FORMAT_EXT.get(fmt, 'img')}"
        if not path.exists():
            path.write_bytes(data)
        return Stashed(file=str(path), sha1=sha, format=fmt, error=None)
    except Exception as e:  # noqa: BLE001 - a harness's fetcher raises its own types
        return Stashed(file=None, sha1=None, format=None, error=str(e)[:120])


def stash_json(got: Stashed) -> JsonObject:
    """A `Stashed` as the keys of an image element's JSON (`pictures_from_pptx` writes them)."""
    out: JsonObject = {}
    for key, value in (("file", got.file), ("sha1", got.sha1), ("format", got.format), ("error", got.error)):
        if value is not None:
            out[key] = value
    return out


def notes_text(slide: Page) -> str | None:
    props = slide.get("slideProperties")
    page = None if props is None else props.get("notesPage")
    if page is None:
        return None
    sid = _obj(page.get("notesProperties")).get("speakerNotesObjectId")
    for pe in page.get("pageElements") or []:
        if pe.get("objectId") == sid:
            text = "".join(_text(_obj(te.get("textRun")).get("content"), "")
                           for te in _text_elements(_obj(_shape(pe).get("text"))))
            text = text.strip()
            return text or None
    return None


# A foreign deck bigger than this many times beamer's page (a 48 x 36 in poster: 9.5) is not a slide
# deck at beamer's scale: its 24 pt body text would be 2.5 pt, below what TeX's fixed skips and struts
# are made for. Such a page keeps half the deck's size, like any page of a ratio beamer has no
# option for; 1920 x 1080 (4.2) is still an ordinary 16:9 deck.
MAX_BEAMER_SCALE = 5.0


def page_size_for(pres: Presentation, pdf_size: Sequence[float] | None, foreign: bool) -> tuple[float, float, float]:
    """(page width, page height, scale) of the IR: the PDF page the deck came from, else beamer's
    page of the deck's aspect, else half the deck's size (a ratio beamer has no option for: adopt
    writes that page with `\\geometry`, `adopt.page_setup`)."""
    size = pres.get("pageSize")
    if size is None:
        raise JsonShapeError(f"{WHERE}: the presentation has no pageSize")
    w, h = _dimension(size.get("width")), _dimension(size.get("height"))
    if pdf_size:
        return pdf_size[0], pdf_size[1], w / pdf_size[0]
    ratio = w / h
    for (a, b), beamer in BEAMER_SIZES.items():
        if abs(ratio - a / b) < 0.01 and not (foreign and w / beamer[0] > MAX_BEAMER_SCALE):
            return beamer[0], beamer[1], w / beamer[0]
    return w / 2, h / 2, 2.0


def inherited_chain(slide: Page, pages: Mapping[str, Page]) -> list[Page]:
    """The master and then the layout a slide draws on top of, in that drawing order.

    A deck a person built in Slides keeps most of its look here: of the 39 slides of the DevFest
    template, the section-title slide carries one element of its own and draws six - the blue dotted
    sheet, the white card, the bar, the dot - from its layout and master. A deck this repository
    converted is the other way round (the source draws its theme and `emit.plan_theme` puts the
    picture on the layouts), which is why `pull` must not see these: it would write the decoration
    into the .tex that already draws it. `adopt` asks for them, `pull` does not."""
    layout = pages.get(_layout_id(slide) or "")
    master = pages.get((None if layout is None else _layout_master(layout)) or "")
    return [p for p in (master, layout) if p]


Votes = dict[str, list[int]]
"""`vote_inherited`'s tally: element id -> [seen, absent]."""


def vote_inherited(elements: Sequence[TargetElement], thumb: SignedRGB, px: float, votes: Votes) -> None:
    """One slide's word on every inherited (master/layout) element it carries: did its own thumbnail
    show that element's own appearance, past whatever this slide itself draws over it?

    `inherited_chain` adds master and layout elements to every slide unchecked - right for a deck a
    person built in Slides, wrong the moment the master or layout itself does not draw one of them on
    a given slide (a `showMasterSp` off in the source .pptx, which the API does not expose at all:
    ua-space's master map-band group is never drawn by Google on any of its 16 slides, yet was always
    added). `votes[id] = [seen, absent]` tallies each element's id (`page~element`, the same on every
    slide that shares the page) so `decide_drops` can judge it on the whole deck's worth of evidence
    rather than one slide's - a mis-read here and there does not move a vote most slides agree on, the
    way one slide's guess would.

    An element only casts a vote when `deck_fills.element_appearance_share` can characterise its
    appearance at all (a flat fill or its own picture, not a gradient or an unread fill) and enough of
    it stood clear of whatever the slide itself draws on top (`deck_fills.masks`, `INHERITED_MIN_PX`)
    - a group partly covered on every slide, or an element this cannot judge, simply never votes and
    so is never dropped on suspicion alone."""
    from . import deck_fills
    h, w = thumb.shape[:2]
    for k, el in enumerate(elements):
        if not el.inherited:
            continue
        at = deck_fills.px_box(el.bbox, px, w, h, deck_fills.MARGIN_PX)
        if at[2] - at[0] < 4 or at[3] - at[1] < 4:
            continue
        sub, region, _ = deck_fills.masks(thumb, at, deck_fills.untraced(elements[k + 1:]), px, False)
        if int(region.sum()) < deck_fills.INHERITED_MIN_PX:
            continue
        share = deck_fills.element_appearance_share(sub, region, el)
        if share is None:
            continue
        vote = votes.setdefault(el.id, [0, 0])
        if share >= deck_fills.INHERITED_VISIBLE:
            vote[0] += 1
        elif share <= deck_fills.INHERITED_ABSENT_MAX:
            vote[1] += 1
        # else: too ambiguous a reading to vote either way


def decide_drops(votes: Votes, groups: Mapping[str, tuple[str, str]]) -> set[str]:
    """Ids of inherited elements the whole deck's thumbnails agree are never actually drawn: never
    once confirmed shown, and confirmed absent on at least one slide that could judge them.

    Decided per `(page, source group)` - `groups[id]`, a singleton of its own id when the element was
    not part of one - not per element: a group is dropped or kept as one piece, because a decorative
    cluster's pieces are meant to be judged together (`deck_ir`'s docstring on `foreign` not folding
    groups) and a picture close to the page's own colour, or a piece another element happens to cover
    on every single slide, should not be split off from siblings that plainly are or are not there.
    One member ever shown clears the whole group; only a group with no such witness at all, but some
    confirmed absent, is dropped whole."""
    by_group: dict[tuple[str, str], list[str]] = {}
    for eid, key in groups.items():
        by_group.setdefault(key, []).append(eid)
    drop: set[str] = set()
    for ids in by_group.values():
        seen = any(votes.get(i, [0, 0])[0] > 0 for i in ids)
        absent = any(votes.get(i, [0, 0])[1] > 0 for i in ids)
        if absent and not seen:
            drop.update(ids)
    return drop


# ------------------------------------------------------------------------------ elements


@dataclass(frozen=True, kw_only=True)
class Ident:
    """Which object an element is read from, and its key (`read_target`)."""
    id: str
    object: str | None
    group: str | None
    key: str | None


def line_element(pe: PageElement, m: Affine, scale: float, scheme: Mapping[str, str],
                 ident: Ident) -> TargetShape | None:
    """A connector, as the two points it runs between. Slides stores a line as the unit segment
    (0,0)-(w,h) under the element's transform, so a line drawn up and to the left comes back as a
    box with a negative scale - which a bounding box alone cannot tell from one drawn down and to
    the right. Only `adopt` asks for these (`deck_ir(foreign=True)`)."""
    w, h = _wh(pe)
    line = _obj(pe.get("line"))
    props = _obj(line.get("lineProperties"))
    said = _obj(props.get("lineFill")).get("solidFill")
    if said is None:
        return None
    solid = _obj(said)
    x0, y0 = m[0] * 0 + m[1] * 0 + m[2], m[3] * 0 + m[4] * 0 + m[5]
    x1, y1 = m[0] * w + m[1] * h + m[2], m[3] * w + m[4] * h + m[5]
    alpha = _num(solid.get("alpha"), 1.0)
    return TargetShape(
        id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="line",
        bbox=_scaled((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), scale), inherited=None,
        shape="line", shape_type=None, start=(round(x0 / scale, 2), round(y0 / scale, 2)),
        end=(round(x1 / scale, 2), round(y1 / scale, 2)), outline=rgb_hex(solid.get("color"), scheme),
        weight=round((dim(props.get("weight")) or 0.75) / scale, 3),
        arrow=props.get("endArrow") not in (None, "NONE"), arrow_start=props.get("startArrow") not in (None, "NONE"),
        fill=None,
        # what the heads are, how the connector runs (elbow and curved connectors are drawn in
        # their own frame), its dashes and its transparency
        start_arrow=_text(props.get("startArrow"), "NONE"), end_arrow=_text(props.get("endArrow"), "NONE"),
        line_type=_str(line.get("lineType")), category=_str(line.get("lineCategory")), frame=frame(m, w, h, scale),
        dash=_str(props.get("dashStyle")) if props.get("dashStyle", "SOLID") != "SOLID" else None,
        outline_alpha=round(alpha, 4) if alpha < 1.0 else None,
        fill_alpha=None, fill_gradient=None, fill_source=None, fill_unread=None, corner_radius=None, pie=None,
        trace=None, no_ramp=None, not_rendered=None, unsaid=None)


def unread_fill(fill: JsonObject | None) -> bool:
    """A fill Slides draws that the API does not describe: `{}` with no `propertyState` (a gradient,
    picture or texture fill - the API only has words for a solid one), or `NOT_RENDERED` (a .pptx
    gradient or theme fill on a shape or text box, the same as a table cell's colour from a .pptx
    table style - "the style is nowhere in the answer at all"; china-pptx's tall text panel is a
    NOT_RENDERED fill with a bogus white `solidFill` alongside it, not a real one). `deck_fills`
    reads either back from the slide's thumbnail when there is one; a shape truly left unfilled
    reads as its background and is dropped there (`worth`), same as the `{}` case always was."""
    if fill is None:
        return False
    state = fill.get("propertyState", "RENDERED")
    return state == "NOT_RENDERED" or (state == "RENDERED" and "solidFill" not in fill)


def foreign_shape(pe: PageElement, m: Affine, w: float, h: float, bbox: Box, fill_hex: str | None,
                  resolver: StyleResolver, fonts: FontMapper, scale: float, page_w: float,
                  inherited: JsonObject | None, ident: Ident) -> TargetText | TargetShape | None:
    """A shape of a deck nobody converted, as adopt draws it: its preset (`shape_type`; a freeform,
    which the API gives no geometry for, is CUSTOM), fill and outline with their transparency, dashes
    and weight, and - when it is turned, mirrored or sheared - its own `frame`, which is what a turned
    text box's words are laid out in (`bbox` stays the bounding box on the page, where the ink is).

    A text box's own fill and outline are read too: `pull` leaves them out because the converter
    never writes one, but a person's text box on a coloured panel is that panel."""
    shape = _shape(pe)
    props = _obj(shape.get("shapeProperties"))
    kind = _str(shape.get("shapeType")) or "CUSTOM"
    upright = m
    if turned(m):
        # the words are laid out in the box the element would have if it were not turned
        fr = frame(m, w, h, 1.0)
        x0, y0 = fr.box[0], fr.box[1]
        upright = (fr.size[0] / w if w else 1.0, 0.0, x0, 0.0, fr.size[1] / h if h else 1.0, y0)
    # `inherited`: a placeholder's INHERIT fill as its layout or master sets it (`inherited_fill`)
    said = props.get("shapeBackgroundFill")
    fill = (None if said is None else _obj(said)) if inherited is None else inherited
    solid = _obj((fill or {}).get("solidFill"))
    alpha = _num(solid.get("alpha"), 1.0)
    if alpha <= 0.004:
        fill_hex = None  # a fill at alpha 0 draws nothing (sc-memphis' rings: black at alpha 0)
    # A placeholder's own NOT_RENDERED is no fill (772 in the corpora), as is every INHERIT chain
    # ending there; its own `{}` is a gradient or picture fill Slides draws (china-pptx 173's white
    # panel under a caption on a photo; 10 in the corpora)
    own_unsaid = fill is not None and fill.get("propertyState", "RENDERED") == "RENDERED" and "solidFill" not in fill
    fill_unread: bool | None = None
    not_rendered: bool | None = None
    if fill is not None and unread_fill(fill) and (inherited is not None or not shape.get("placeholder") or own_unsaid):
        fill_unread = True      # drawn, but not as anything the API says: `deck_fills`
        if fill.get("propertyState") == "NOT_RENDERED":
            # a gradient or theme fill, or no fill at all (Slides' "transparent"): only the thumbnail
            # tells, and an outlined freeform may yet be its outline alone (`deck_fills.outline_only`)
            not_rendered = True
    fill_alpha = round(alpha, 4) if fill_hex and alpha < 1.0 else None
    outline = outline_props(_obj(props.get("outline")), scale, resolver.scheme)
    own_frame = frame(m, w, h, scale) if turned(m) else None
    body = text_element(pe, upright, resolver, fonts, scale, page_w, True) if shape.get("text") else None
    if body is not None:
        # A node with a label is one page element: without its outline here, adopt would write the
        # words of a flow chart and none of the boxes around them.
        return TargetText(
            id=ident.id, object=ident.object, group=ident.group, key=ident.key, role=body.role, bbox=bbox,
            inherited=None, anchor=body.anchor, wrap_width=body.wrap_width, placeholder=body.placeholder,
            shape_type=kind, paragraphs=body.paragraphs, box=body.box, wordart=None, rotation=None,
            outline_color=outline.color, outline_alpha=outline.alpha, weight=outline.weight, dash=outline.dash,
            fill=fill_hex or None, fill_alpha=fill_alpha, fill_gradient=None, fill_source=None,
            fill_unread=fill_unread, not_rendered=not_rendered, frame=own_frame, ink_width=None)
    if shape.get("placeholder") or not (fill_hex or outline.color or fill_unread):
        return None
    return TargetShape(
        id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="panel", bbox=bbox, inherited=None,
        shape=kind.lower(), shape_type=kind, fill=fill_hex, outline=outline.color, weight=outline.weight,
        fill_alpha=fill_alpha, outline_alpha=outline.alpha, dash=outline.dash, fill_gradient=None, fill_source=None,
        fill_unread=fill_unread, frame=own_frame, corner_radius=None, pie=None, trace=None, start=None, end=None,
        arrow=None, arrow_start=None, start_arrow=None, end_arrow=None, line_type=ABSENT, category=ABSENT,
        no_ramp=None, not_rendered=not_rendered, unsaid=None)


def element_of(pe: PageElement, m: Affine, resolver: StyleResolver, fonts: FontMapper, scale: float, page_w: float,
               fetch: Fetch | None, images: Path | None, foreign: bool, ident: Ident) -> TargetElement | None:
    if "size" not in pe and "line" not in pe:
        return None
    w, h = _wh(pe)
    bbox = _scaled(box(m, w, h), scale)
    if "shape" in pe:
        shape = _shape(pe)
        props = _obj(shape.get("shapeProperties"))
        fill = _obj(props.get("shapeBackgroundFill"))
        inherited = None
        if foreign and shape.get("placeholder") and fill.get("propertyState") == "INHERIT":
            inherited = resolver.inherited_fill(pe)
            # `{}` is a fill too (a gradient the layout's placeholder draws): not `inherited or fill`
            fill = fill if inherited is None else inherited
        fill_hex = rgb_hex(_obj(fill.get("solidFill")).get("color"), resolver.scheme) \
            if fill.get("propertyState", "RENDERED") == "RENDERED" and "solidFill" in fill else None
        if foreign:
            return foreign_shape(pe, m, w, h, bbox, fill_hex, resolver, fonts, scale, page_w, inherited, ident)
        shape_type = _str(shape.get("shapeType"))
        body = text_element(pe, m, resolver, fonts, scale, page_w, foreign) if shape.get("text") else None
        if body is not None:
            return TargetText(
                id=ident.id, object=ident.object, group=ident.group, key=ident.key, role=body.role, bbox=body.bbox,
                inherited=None, anchor=body.anchor, wrap_width=body.wrap_width, placeholder=body.placeholder,
                shape_type=shape_type, paragraphs=body.paragraphs, box=body.box, wordart=None, rotation=None,
                outline_color=ABSENT, outline_alpha=None, weight=None, dash=None,
                fill=fill_hex if fill_hex and shape_type != "TEXT_BOX" else None, fill_alpha=None,
                fill_gradient=None, fill_source=None, fill_unread=None, not_rendered=None, frame=None,
                ink_width=None)
        if shape_type == "TEXT_BOX" or shape.get("placeholder"):
            return None
        outline = _obj(props.get("outline"))
        stroke = rgb_hex(_obj(_obj(outline.get("outlineFill")).get("solidFill")).get("color"), resolver.scheme) \
            if outline.get("propertyState", "RENDERED") == "RENDERED" else None
        if not fill_hex and not stroke:
            return None
        return TargetShape(
            id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="panel", bbox=bbox,
            inherited=None, shape=_text(shape.get("shapeType"), "RECTANGLE").lower(), shape_type=None, fill=fill_hex,
            outline=stroke, weight=round(dim(outline.get("weight")) / scale, 2) or None, fill_alpha=None,
            outline_alpha=None, dash=None, fill_gradient=None, fill_source=None, fill_unread=None, frame=None,
            corner_radius=None, pie=None, trace=None, start=None, end=None, arrow=None, arrow_start=None,
            start_arrow=None, end_arrow=None, line_type=ABSENT, category=ABSENT, no_ramp=None, not_rendered=None,
            unsaid=None)
    if "image" in pe:
        image = _obj(pe.get("image"))
        look = picture_props(image, m, w, h, scale, resolver.scheme)
        url = _str(image.get("contentUrl"))
        source = _str(image.get("sourceUrl"))
        got = stash_picture(url, fetch, images) if url and fetch and images is not None else NOT_STASHED
        return _image(ident, bbox, pe.get("description"), look, got,
                      # inserted by URL: maybe a bigger original than Google keeps
                      source if source and "googleusercontent.com" not in source else None, None, None)
    if "table" in pe:
        return table_element(pe, m, resolver, fonts, scale, foreign, ident)
    if "line" in pe:
        return line_element(pe, m, scale, resolver.scheme, ident) if foreign else None
    if foreign:
        return media_element(pe, m, w, h, bbox, resolver, scale, fetch, images, ident)
    return None


@dataclass(frozen=True, kw_only=True)
class PictureLook:
    """What Slides keeps as properties of a picture (`picture_props`)."""
    box: Box
    flip: bool | None
    rotation: float | None
    crop: Crop | None
    crop_angle: float | None
    opacity: float | None
    brightness: float | None
    contrast: float | None
    recolor: Recolor | None
    outline: PictureOutline | None


def _image(ident: Ident, bbox: Box, alt: str | None, look: PictureLook, got: Stashed, source_url: str | None,
           chart: Chart | None, video: Video | None) -> TargetImage:
    return TargetImage(
        id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="figure", bbox=bbox, inherited=None,
        alt=alt, box=look.box, flip=look.flip, rotation=look.rotation, crop=look.crop, crop_angle=look.crop_angle,
        opacity=look.opacity, brightness=look.brightness, contrast=look.contrast, recolor=look.recolor,
        outline=look.outline, source_url=source_url, file=got.file, sha1=got.sha1, format=got.format,
        error=got.error, chart=chart, video=video, number=None, fill_source=None, thumbnail_crop=None,
        thumbnail_of=None, picture_place=None, picture_source=None, poster=None, mask=None, clip=None)


YOUTUBE_THUMB = "https://img.youtube.com/vi/{id}/hqdefault.jpg"


def media_outline(props: JsonObject, scale: float, scheme: Mapping[str, str]) -> PictureOutline | None:
    """A chart's or video's outline, in the shape `picture_props` gives a picture's."""
    outline = _obj(props.get("outline"))
    if not outline or outline.get("propertyState", "RENDERED") != "RENDERED":
        return None
    colour = rgb_hex(_obj(_obj(outline.get("outlineFill")).get("solidFill")).get("color"), scheme)
    if colour is None:
        return None
    return PictureOutline(color=colour, weight=round(dim(outline.get("weight")) / scale, 3),
                          dash=_text(outline.get("dashStyle"), "SOLID"))


def media_element(pe: PageElement, m: Affine, w: float, h: float, bbox: Box, resolver: StyleResolver,
                  scale: float, fetch: Fetch | None, images: Path | None, ident: Ident) -> TargetElement | None:
    """What a person puts on a slide besides shapes, pictures and tables - only `adopt` reads these,
    since nothing this converter writes is one of them.

    - a linked Sheets chart is a picture: Slides keeps its rendering (`contentUrl`) like a picture's,
      and the source has no way to redraw it from the spreadsheet (74 of solidity-survey's 83 slides
      are one chart and its title);
    - a video is the frame Slides shows before it plays, linked to where it plays: YouTube's own
      thumbnail (`hqdefault`, 4:3 with the 16:9 frame letterboxed in it, as the player does), a
      Drive video - whose poster frame no API gives - a dark placeholder with a play symbol;
    - WordArt is its words, drawn stretched to the element's box as Slides draws them. Its fill and
      outline are not in the API (only `renderedText` is), so it is set in the text colour."""
    look = picture_props({}, m, w, h, scale, resolver.scheme)
    alt = pe.get("description") or pe.get("title")
    if "sheetsChart" in pe:
        chart = _obj(pe.get("sheetsChart"))
        outline = media_outline(_obj(_obj(chart.get("sheetsChartProperties")).get("chartImageProperties")), scale,
                                resolver.scheme)
        url = _str(chart.get("contentUrl"))
        got = stash_picture(url, fetch, images) if url and fetch and images is not None else NOT_STASHED
        chart_id = chart.get("chartId")
        return _image(ident, bbox, alt or "chart", replace(look, outline=outline), got, None,
                      Chart(spreadsheet_id=_str(chart.get("spreadsheetId")),
                            chart_id=None if chart_id is None else _int(chart_id, 0)), None)
    if "video" in pe:
        video = _obj(pe.get("video"))
        source, vid = _str(video.get("source")), _str(video.get("id"))
        url = _str(video.get("url")) or (f"https://www.youtube.com/watch?v={vid}" if source == "YOUTUBE" and vid else None)
        vp = _obj(video.get("videoProperties"))
        outline = media_outline(vp, scale, resolver.scheme)
        got = stash_picture(YOUTUBE_THUMB.format(id=vid), fetch, images) \
            if source == "YOUTUBE" and vid and fetch and images is not None else NOT_STASHED
        return _image(ident, bbox, alt or "video", replace(look, outline=outline), got, None, None,
                      Video(source=source, id=vid, url=url, start=_opt_num(vp.get("start")),
                            end=_opt_num(vp.get("end"))))
    if "wordArt" in pe:
        text = (_str(_obj(pe.get("wordArt")).get("renderedText")) or "").replace("\u000b", "\n").strip("\n")
        if not text.strip():
            return None
        lines = text.split("\n")
        b = look.box
        size = round(max((b[3] - b[1]) / len(lines) * 0.8, 1.0), 2)       # what it is stretched to decides nothing
        run = TargetRun(text="", font="", family="sans", size=size, bold=False, italic=False, smallcaps=False,
                        color="#000000", link=None, script=None, underline=False, strike=False, highlight=None,
                        hole=None, weight=None, weight_unsure=None)
        paras = tuple(TargetParagraph(align="center", level=0, bullet=None, direction=None, size=size, text_x0=b[0],
                                      tab_x0=None, lines=(TargetLine(baseline=None, x0=b[0], x1=b[2]),),
                                      runs=(replace(run, text=t or " "),), slides=None) for t in lines)
        return TargetText(
            id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="body", bbox=bbox,
            inherited=None, wordart=True, shape_type="WORD_ART", box=b, rotation=look.rotation,
            anchor=(round((b[0] + b[2]) / 2, 2), b[3]), wrap_width=round(b[2] - b[0], 2), placeholder=None,
            paragraphs=paras, outline_color=ABSENT, outline_alpha=None, weight=None, dash=None, fill=None,
            fill_alpha=None, fill_gradient=None, fill_source=None, fill_unread=None, not_rendered=None, frame=None,
            ink_width=None)
    return None


SLIDES_ID = re.compile(r"^g[0-9a-f]+_\d+_\d+$")


def guess_lines(text: str, width: float, size: float) -> int:
    """How many lines `text` takes at `size` in `width` pt, by a rough 0.5 em per character with
    greedy word wrapping: enough to tell one line from three, which is all `cell_pad` asks."""
    lines = 0
    for para in text.replace("\x0b", "\n").rstrip("\n").split("\n"):
        at, lines = 0.0, lines + 1
        for word in para.split():
            w = 0.5 * size * len(word)
            if at and at + 0.25 * size + w > width:
                lines, at = lines + 1, w
            else:
                at += (0.25 * size if at else 0.0) + w
    return max(lines, 1)


def cell_pad(pe: PageElement) -> tuple[float, float]:
    """A table's cell insets in Slides pt, (left and right, top and bottom), which the API neither
    reports nor lets one set - so they are inferred.

    A table made in Slides has 7.2 pt all round (CLAUDE.md, pitfalls). A table a .pptx brought keeps
    the file's margins, and on the adopt corpus's thumbnails those render from 1 to 4 pt: a 16 pt row
    of comps-analysis grows to 24 pt, where 7.2 pt insets would make it 33.6. Two things the API does
    say narrow it down. Where the object came from: Slides names what it creates g<hex>_<n>_<n>, an
    import keeps the file's ids (i32, p14_i11), and an import is at most 3.6 pt (0.05 in, the .pptx
    default). And the row heights: a stored height is at least its text plus both insets unless the
    row was set smaller than its text (which only Slides allows, growing it back), so the tightest row
    that still holds its line bounds the inset from above - solidity-survey's g-named table, pasted
    from an import, has 15.8 pt rows of 10 pt text and so insets of 1.9, not 7.2 (measured about 2).
    """
    native = bool(SLIDES_ID.match(pe.get("objectId") or ""))
    cap = 7.2 if native else 3.6
    table = _obj(pe.get("table"))
    widths = [dim(c.get("columnWidth")) for c in _objs(table.get("tableColumns"))]
    room: list[float] = []
    for row in _objs(table.get("tableRows")):
        z = 0.0
        for cell in _objs(row.get("tableCells")):
            size, text = 0.0, ""
            for te in _text_elements(_obj(cell.get("text"))):
                if "textRun" in te:
                    tr = _obj(te["textRun"])
                    content = _text(tr.get("content"), "")
                    text += content
                    if content.strip():
                        size = max(size, dim(_obj(tr.get("style")).get("fontSize")))
            z = max(z, size)
            if native and size and _int(cell.get("rowSpan"), 1) == 1:
                col = _int(_obj(cell.get("location")).get("columnIndex"), 0)
                span = sum(widths[col:col + _int(cell.get("columnSpan"), 1)])
                if dim(row.get("rowHeight")) < 1.2 * size * guess_lines(text, span - 2 * cap, size) - 1:
                    # a row stored smaller than its text at Slides' own insets: someone dragged it
                    # smaller and Slides grew it back, so the heights say nothing about the insets
                    # (journey-maps 16 and 17: rows of 7.9 pt hold 12 pt text, a 15.2 pt header
                    # three lines, and the thumbnails show 7.2 pt insets)
                    return 7.2, 7.2
        spare = dim(row.get("rowHeight")) - 1.2 * z
        if z and spare >= 0:
            room.append(spare / 2)
    pad_y = min(cap, max(1.5, min(room))) if room else cap
    if cap - pad_y < 1.0:
        # A row a little short of its text at the cap is one Slides grew by what it lacked, not a
        # sign of smaller insets: creandum-board's 22.0 pt rows of 7 pt text (6.8 pt to spare each
        # side) are 22.8 on the thumbnail, 8.4 + 2 x 7.2.
        pad_y = cap
    # Across it is a little more: journey-maps' header (rows say 2.5 pt) keeps "Channel" whole in
    # 46.9 pt but breaks "custom|ers" in 41.1 and "Succes|s" in 43.9, which puts the inset between
    # 3.7 and 5.7 pt; its first column's ink starts 4.9 pt in.
    return min(7.2, pad_y + 2.2), pad_y


BORDER_ROWS: tuple[tuple[Literal["h", "v"], str], ...] = (("h", "horizontalBorderRows"), ("v", "verticalBorderRows"))


def table_element(pe: PageElement, m: Affine, resolver: StyleResolver, fonts: FontMapper, scale: float,
                  foreign: bool, ident: Ident) -> TargetTable:
    """A table: `rows` (each cell's plain text, what `pull` and sync compare) and its box.

    The box is the grid's, not the element's: Slides reports every table's `size` as 3,000,000 EMU
    square whatever it holds (all 42 tables of the adopt corpus), and the grid is the column widths
    across and the row heights down - minimum heights, since Slides grows a row to fit its text.

    `foreign` adds what `adopt` needs to draw it (`adopt.table_block`): `col_widths` and
    `row_heights` in PDF pt, `table_cells` (only the head cell of a merged range is listed, at its
    `location`; `rowspan`/`colspan`, `fill`, `fill_alpha`, `valign` and `paragraphs` read like a text
    box's) and `table_borders`, the visible segments of the API's `horizontalBorderRows` (one per
    column under row boundary `row`) and `verticalBorderRows` (one per row beside column boundary
    `col`). A cell fill with no `propertyState` is drawn, as a shape's is."""
    t = _obj(pe.get("table"))
    rows: list[tuple[str, ...]] = []
    for row in _objs(t.get("tableRows")):
        cells: list[str] = []
        for cell in _objs(row.get("tableCells")):
            text = "".join(_text(_obj(te.get("textRun")).get("content"), "")
                           for te in _text_elements(_obj(cell.get("text"))))
            cells.append(" ".join(text.split()))
        rows.append(tuple(cells))
    widths = [dim(c.get("columnWidth")) * m[0] for c in _objs(t.get("tableColumns"))]
    heights = [dim(r.get("rowHeight")) * m[4] for r in _objs(t.get("tableRows"))]
    x0, y0 = m[2], m[5]
    if widths and heights:
        bbox = _scaled((x0, y0, x0 + sum(widths), y0 + sum(heights)), scale)
    else:
        w, h = _wh(pe)
        bbox = _scaled(box(m, w, h), scale)
    if not foreign:
        return TargetTable(id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="table", bbox=bbox,
                           inherited=None, rows=tuple(rows), cell_pad=None, col_widths=None, row_heights=None,
                           rows_fixed=None, cell_text_y=None, table_cells=None, table_borders=None)
    pad = cell_pad(pe)
    cells_out: list[TableCell] = []
    for row in _objs(t.get("tableRows")):
        for cell in _objs(row.get("tableCells")):
            loc = _obj(cell.get("location"))
            props = _obj(cell.get("tableCellProperties"))
            fill = _obj(props.get("tableCellBackgroundFill"))
            solid = _obj(fill.get("solidFill") if fill.get("propertyState", "RENDERED") == "RENDERED" else None)
            paras = text_paragraphs(pe, _obj(cell.get("text")), resolver, fonts, scale, True, 1.0, 0.0, False, True)
            cells_out.append(TableCell(
                row=_int(loc.get("rowIndex"), 0), col=_int(loc.get("columnIndex"), 0),
                rowspan=_int(cell.get("rowSpan"), 1), colspan=_int(cell.get("columnSpan"), 1),
                fill=rgb_hex(solid.get("color"), resolver.scheme) if solid else None,
                fill_alpha=round(_num(solid.get("alpha"), 1.0), 3) if solid else None,
                valign=VALIGN.get(_text(props.get("contentAlignment"), ""), "top"),
                # a Hebrew cell is right to left like a Hebrew text box (hebrew-lesson's tables):
                # set left to right, its lines ended on the wrong side of their full stops
                paragraphs=tuple(CellParagraph(align=p.align, level=p.level, bullet=p.bullet, size=p.size,
                                               line_spacing=p.line_spacing, direction=p.direction,
                                               runs=tuple(r.run for r in p.runs)) for p in paras if p.runs),
                fill_source=None,
                # A .pptx table style colours cells the API reports NOT_RENDERED (comps-analysis,
                # creandum-board): the style is nowhere in the answer, so `deck_fills` asks the
                # thumbnail, and a cell that is truly empty shows the page and stays unfilled.
                fill_unread=None if solid else True, empty_size=None))
    borders: list[TableBorder] = []
    for direction, key in BORDER_ROWS:
        for i, brow in enumerate(_objs(t.get(key))):
            for bc in _objs(brow.get("tableBorderCells")):
                loc = _obj(bc.get("location"))
                props = _obj(bc.get("tableBorderProperties"))
                solid = _obj(_obj(props.get("tableBorderFill")).get("solidFill"))
                weight = dim(props.get("weight"))
                if not solid or _num(solid.get("alpha"), 1.0) <= 0 or weight <= 0:
                    continue
                borders.append(TableBorder(
                    dir=direction, row=_int(loc.get("rowIndex"), i), col=_int(loc.get("columnIndex"), 0),
                    color=rgb_hex(solid.get("color"), resolver.scheme), alpha=round(_num(solid.get("alpha"), 1.0), 3),
                    weight=round(weight / scale, 3), dash=_text(props.get("dashStyle"), "SOLID")))
    return TargetTable(
        id=ident.id, object=ident.object, group=ident.group, key=ident.key, role="table", bbox=bbox, inherited=None,
        rows=tuple(rows), cell_pad=(round(pad[0] / scale, 3), round(pad[1] / scale, 3)),
        col_widths=tuple(round(w / scale, 3) for w in widths), row_heights=tuple(round(h / scale, 3) for h in heights),
        rows_fixed=None, cell_text_y=None, table_cells=tuple(cells_out), table_borders=tuple(borders))


FORMAT_EXT = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp", "bmp": "bmp", "tiff": "tif", "svg": "svg",
              "emf": "emf", "wmf": "wmf", "pdf": "pdf"}


def image_format(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[40:44] == b" EMF":
        return "emf"
    if data[:4] == b"\xd7\xcd\xc6\x9a":
        return "wmf"
    if b"<svg" in data[:1000]:
        return "svg"
    return "unknown"


def picture_props(image: JsonObject, m: Affine, w: float, h: float, scale: float,
                  scheme: Mapping[str, str]) -> PictureLook:
    """What Slides keeps as properties of a picture (`image`: the element's `image`), in PDF pt:
    `box` (the unrotated frame around the centre), `rotation` (degrees clockwise), `flip`
    (mirrored), `crop` (fractions cut off the picture file), `opacity`, `brightness`/`contrast`
    (-1..1), `recolor` (gradient stops) and `outline` (colour, weight). Only what differs from an
    untouched picture is set."""
    a, b, _, d, e, _ = m
    cx, cy = m[0] * w / 2 + m[1] * h / 2 + m[2], m[3] * w / 2 + m[4] * h / 2 + m[5]
    bw, bh = math.hypot(a, d) * w, math.hypot(b, e) * h
    rot = math.degrees(math.atan2(d, a))
    flip = None
    if a * e - b * d < 0:  # R(rot)·flip(y) = R(rot + 180)·flip(x)
        flip = True
        rot += 180
    rot = (rot + 180) % 360 - 180
    ip = _obj(image.get("imageProperties"))
    crop = _obj(ip.get("cropProperties"))
    c = Crop(l=round(_num(crop.get("leftOffset"), 0.0), 5), t=round(_num(crop.get("topOffset"), 0.0), 5),
             r=round(_num(crop.get("rightOffset"), 0.0), 5), b=round(_num(crop.get("bottomOffset"), 0.0), 5))
    angle = _num(crop.get("angle"), 0.0)
    transparency = _num(ip.get("transparency"), 0.0)
    brightness = _num(ip.get("brightness"), 0.0)
    contrast = _num(ip.get("contrast"), 0.0)
    recolor = _obj(ip.get("recolor"))
    outline = _obj(ip.get("outline"))
    drawn = bool(outline) and outline.get("propertyState", "RENDERED") == "RENDERED"
    return PictureLook(
        box=_scaled((cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2), scale), flip=flip,
        rotation=round(rot, 2) if abs(rot) > 0.05 else None,
        crop=c if any(abs(v) > 1e-4 for v in (c.l, c.t, c.r, c.b)) else None,
        crop_angle=round(math.degrees(angle), 2) if angle else None,
        opacity=round(1 - transparency, 4) if transparency else None,
        brightness=round(brightness, 4) if brightness else None, contrast=round(contrast, 4) if contrast else None,
        recolor=Recolor(name=_str(recolor.get("name")), stops=tuple(
            RecolorStop(color=rgb_hex(s.get("color"), scheme) or "#000000", alpha=_num(s.get("alpha"), 1.0),
                        position=_num(s.get("position"), 0.0)) for s in _objs(recolor.get("recolorStops"))))
        if recolor else None,
        outline=PictureOutline(
            color=rgb_hex(_obj(_obj(outline.get("outlineFill")).get("solidFill")).get("color"), scheme) or "#000000",
            weight=round(dim(outline.get("weight")) / scale, 3), dash=_text(outline.get("dashStyle"), "SOLID"))
        if drawn else None)


# ------------------------------------------------------------------------------ the deck


def _strings(v: Json) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise _wrong(v, "a list")
    return [as_str(x, WHERE) for x in v]


def _size_of(v: Json) -> tuple[float, float] | None:
    """A page size as a deck.json or a base says it ([w, h] in pt), or None."""
    if v is None:
        return None
    if not isinstance(v, list) or len(v) != 2:
        raise _wrong(v, "a page size [w, h]")
    return _num(v[0], 0.0), _num(v[1], 0.0)


ObjectKey = tuple[str | None, str | None]
"""(slide key, element key) an object had in a base."""


def read_target(pres: Presentation, pdf_size: Sequence[float] | None, base: JsonObject | None, fetch: Fetch | None,
                images: Path | None, foreign: bool, thumbnails: Thumbnails | None) -> TargetDeck:
    """IR of a presentation. `pdf_size`: the PDF page size the deck came from (else beamer's
    default for the aspect). `base`: a sync snapshot (object ids -> keys). `fetch(url) -> bytes`
    downloads pictures into `images` (sha1-named) when both are given.

    `foreign`: read the deck as one nobody converted, which changes three things. Each slide is
    also given what it draws from its layout and master (`inherited_chain`), because that is where
    a deck a person built keeps most of its look. Groups are not folded (`fold_groups`), because
    folding recognises *this converter's* conventions - a group of node shapes joined by lines is
    one `diagram`, a short text on a picture is a number on a ball - and reading someone else's
    grouping that way throws away what adopt needs to draw it: each node's outline, and the lines
    themselves. And lines are kept, for the same reason: `pull` drops them because the source it is
    refining already draws them, and a foreign deck's source does not exist yet.

    `thumbnails(n)`: the picture Google renders of slide n (0-based; a path, PIL image or array, or
    None), with which a foreign read fills in what the API leaves out of fills (`deck_fills`).
    Without it those fills stay unknown, and shapes with nothing else to draw are left out. A
    thumbnail given as a path is kept as the slide's `thumbnail`, for pull's frame guard.

    A foreign slide's thumbnail passes (`deck_fills`, `deck_thumbs`) take the slide's element records
    and hand back new ones: a pass never changes a record, it replaces it."""
    page_w, page_h, scale = page_size_for(pres, pdf_size, foreign)
    fonts = FontMapper()
    resolver = StyleResolver(pres)
    resolver.bare_imports = foreign and imports_lack_insets(pres, resolver, fonts, scale)
    pages: dict[str, Page] = {object_id(p): p for p in (pres.get("layouts") or []) + (pres.get("masters") or [])}
    object_keys: dict[str, ObjectKey] = {}
    slide_keys: dict[str, str | None] = {}
    for s in _objs(None if base is None else base.get("slides")):
        sid = _str(s.get("objectId"))
        if sid:
            slide_keys[sid] = _str(s.get("key"))
        for e in _objs(s.get("elements")):
            for oid in _strings(e.get("objects")):
                object_keys[oid] = (_str(s.get("key")), _str(e.get("key")))
    drifts: list[tuple[TargetElement, float]] = []   # (box, `top_drift`) of every measurable box, for `pptx_insets`
    sides: list[float] = []                       # `side_inset` of every box whose words' start edge could be read
    inherited_votes: Votes = {}                   # `vote_inherited`, tallied over every slide
    inherited_groups: dict[str, tuple[str, str]] = {}   # element id -> `(page, source group)` for `decide_drops`

    def read_page(page: Page, tags: list[str]) -> list[TargetElement]:
        out: list[TargetElement] = []
        lines: dict[str, int] = {}
        for pe, m, group in flatten(page.get("pageElements") or [], IDENTITY, None):
            if "line" in pe and group:
                lines[group] = lines.get(group, 0) + 1
            oid = object_id(pe)
            title = pe.get("title")
            tag = TAG_RE.match(title or "")
            key: ObjectKey | None = (tag.group("slide"), tag.group("element")) if tag else \
                object_keys.get(oid) or (object_keys.get(group) if group else None)
            if key and key[0]:
                tags.append(key[0])
            el = element_of(pe, m, resolver, fonts, scale, page_w, fetch, images, foreign,
                            Ident(id=oid, object=oid, group=group, key=key[1] if key else None))
            if el is None:
                continue
            if isinstance(el, TargetImage):
                ek = key[1] if key else None
                if ek and ek.split("/")[1:2] in (["math"], ["icon"]):
                    el = replace(el, role=ek.split("/")[1])
                elif title in ("Formula", "Icon"):
                    el = replace(el, role="math" if title == "Formula" else "icon")
            if isinstance(el, TargetText) and el.key and "/footer/" in f"/{el.key}/":
                el = replace(el, role="footer")
            out.append(el)
        return out if foreign else fold_groups(out, lines)

    slides: list[TargetSlide] = []
    pending: list[tuple[TargetSlide, list[TargetElement]]] = []    # a foreign slide and its elements
    for n, slide in enumerate(pres.get("slides") or []):
        tags: list[str] = []
        under: list[TargetElement] = []
        resolver.use_page(slide)
        if foreign:
            for page in inherited_chain(slide, pages):
                pid = object_id(page)
                for el in read_page(page, []):
                    # A placeholder on a layout is the slide's to fill - it holds the layout's prompt
                    # text ("Click to edit"), or only the "\n" per list level an import leaves there -
                    # and drawing it would print that over the slide's own words.
                    if isinstance(el, TargetText) and (el.placeholder or not el.paragraphs):
                        continue
                    # `role` math/icon says "this picture belongs inside a line of text", which a
                    # picture on a layout never is: it is the template's decoration, and the
                    # heuristic that reads a picture beside one short paragraph as an icon
                    # (`fold_groups`) would otherwise call the full-page backdrop one.
                    new_id = f"{pid}~{el.id}"
                    role = "figure" if isinstance(el, TargetImage) else el.role
                    under.append(replace(el, role=role, inherited=pid, id=new_id))
                    inherited_groups[new_id] = (pid, el.group or new_id)
        elements = under + read_page(slide, tags)
        color, picture = page_background(slide, pages, resolver.scheme)
        oid = object_id(slide)
        key = slide_keys.get(oid) or (max(set(tags), key=tags.count) if tags else None)
        if not foreign:
            slides.append(TargetSlide(
                page=n, frame=str(n + 1), size=(page_w, page_h), object_id=oid, key=key, notes=notes_text(slide),
                background_color=color, background_picture=picture, background_gradient=None,
                background_file=ABSENT, background_source=None, layout=ABSENT, thumbnail=None,
                elements=tuple(elements)))
            continue
        from . import deck_fills
        gradient: PageGradient | None = None
        bg_file: str | None | Absent = ABSENT
        bg_source: str | None = None
        raw = thumbnails(n) if thumbnails else None
        px = 0.0
        shown = str(raw) if isinstance(raw, (str, Path)) else None
        thumb = None if raw is None else deck_fills.load(raw)
        if thumb is not None:
            px = thumb.shape[1] / page_w
            vote_inherited(elements, thumb, px, inherited_votes)
        if thumb is not None and not picture and color:
            # `pageBackgroundFill` can be wrong two ways: a solid the thumbnail simply is not
            # (a gradient the API has no type for, `deck_fills.page_gradient`), or a solid that
            # sits on the slide's own level while the layout's or master's own fill - a picture
            # included - is what Slides actually draws (china-pptx: a slide-level solidFill over
            # a layout's radial picture). Both are read from the same mismatch, cheaply: most
            # slides' own colour explains their background and neither check goes further.
            bg_mask = deck_fills.page_visible_mask([e.bbox for e in elements], px, thumb.shape[1], thumb.shape[0])
            if not deck_fills.region_matches(thumb, bg_mask, color):
                # A candidate is only taken once confirmed on these pixels: offered on its say-so
                # alone, the layout's or master's *shared* picture wrongly overrode slides whose
                # real background was a gradient of their own, unrelated to it (thai-history and
                # china-pptx's other layouts do not draw the `p5` layout's green radial at all).
                pcolor, ppicture = parent_background(slide, pages, resolver)
                picture_share = 0.0
                if ppicture and ppicture != picture and fetch and images is not None:
                    got = stash_picture(ppicture, fetch, images)
                    if got.file:
                        picture_share = deck_fills.picture_region_share(
                            thumb, bg_mask, got.file, thumb.shape[1], thumb.shape[0])
                pcolor_share = (deck_fills.region_flat_share(thumb, bg_mask, pcolor)
                                if pcolor and pcolor != color else 0.0)
                if picture_share >= deck_fills.FLAT and picture_share >= pcolor_share:
                    color, picture = None, ppicture
                elif pcolor_share >= deck_fills.FLAT:
                    color = pcolor
                else:
                    gradient = deck_fills.page_gradient(thumb, bg_mask, color, px)
                    if gradient is None and images is not None:
                        # Neither a flat colour, a picture the API can point to, nor a clean
                        # ramp explains it - a texture no reference exists for at all
                        # (en-flowchart's fine gold stripes), or any other backdrop
                        # `page_gradient` was right to refuse. The thumbnail's own pixels, with
                        # whatever stands on the page painted back in, beat drawing the flat
                        # colour already shown to be wrong.
                        texture = deck_fills.page_texture_picture(thumb, bg_mask, images)
                        if texture:
                            color, bg_file = None, texture
        if thumb is not None and images is not None:
            elements = deck_fills.recover_pictures(elements, thumb, px, images)
        elements = deck_fills.settle(elements, thumb, px, None if picture else color, bool(picture), images)
        elements = thumbnail_insets(elements, thumb, px)
        elements = thumbnail_rows(elements, thumb, px)
        elements = thumbnail_cell_pad(elements, thumb, px)
        elements = thumbnail_cell_text(elements, thumb, px)
        elements = thumbnail_weights(elements, thumb, px)
        elements = ink_widths(elements, thumb, px)
        for e in elements:
            d = top_drift(e, elements, thumb, px)
            if d is not None:
                drifts.append((e, d))
        for e in elements:
            g = side_gap(e, elements, thumb, px)
            if g is not None:
                sides.append(side_inset(e, g))
        # (these two change pictures only: the text boxes `drifts` names stay the objects the slide holds)
        elements = thumbnail_picture_places(elements, thumb, px)
        elements = thumbnail_picture_masks(elements, thumb, px)
        if picture and fetch and images is not None:
            # adopt draws it (a stretched picture fill is the whole page); pull never does, the
            # source it refines already draws whatever the converter baked into it
            got = stash_picture(picture, fetch, images)
            bg_file = got.file
            if thumb is not None and deck_fills.picture_unusable(got.file, got.format, got.error):
                # none came: the page as the thumbnail shows it around what stands on it
                page_picture = deck_fills.background_from_thumbnail(thumb, elements, px, images)
                if page_picture:
                    bg_file = page_picture
                    bg_source = "thumbnail"
        pending.append((TargetSlide(
            page=n, frame=str(n + 1), size=(page_w, page_h), object_id=oid, key=key, notes=notes_text(slide),
            background_color=color, background_picture=picture, background_gradient=gradient,
            background_file=bg_file, background_source=bg_source, layout=_layout_id(slide), thumbnail=shown,
            elements=()), elements))
    if foreign:
        drop_ids = decide_drops(inherited_votes, inherited_groups)
        kept = [(s, [e for e in els if e.id not in drop_ids] if drop_ids else els) for s, els in pending]
        written = pptx_insets([els for _, els in kept], drifts, sides)
        slides = [replace(s, elements=tuple(els)) for (s, _), els in zip(kept, written)]
    return TargetDeck(
        version=1, source=DeckSource(presentation_id=pres.get("presentationId"), title=pres.get("title"),
                                     revision_id=pres.get("revisionId")),
        page_size=(page_w, page_h), scale=scale, slides=tuple(slides),
        layouts=layouts_of(pres, pages, resolver) if foreign else None, first_slide=None)


def layouts_of(pres: Presentation, pages: Mapping[str, Page], resolver: StyleResolver) -> tuple[tuple[str, Layout], ...]:
    """The deck's layouts and masters by object id: display name, master, and the page background a
    slide inherits from it (`page_background`) - what adopt's recovered theme names and draws."""
    out: list[tuple[str, Layout]] = []
    for page in (pres.get("layouts") or []) + (pres.get("masters") or []):
        lp = page.get("layoutProperties")
        if lp:
            name = lp.get("displayName") or lp.get("name") or ""
        else:
            mp = _obj(page.get("masterProperties"))
            name = _str(mp.get("displayName")) or _str(mp.get("name")) or ""
        colour, picture = page_background(page, pages, resolver.scheme_for(page))
        out.append((object_id(page), Layout(name=name, master=_layout_master(page), background_color=colour,
                                            background_picture=picture)))
    return tuple(out)


def _node(e: TargetElement) -> DiagramNode:
    match e:
        case TargetText():
            return DiagramNode(bbox=e.bbox, fill=e.fill, shape=e.shape_type or None,
                               paragraphs=tuple(p.runs for p in e.paragraphs))
        case TargetShape():
            return DiagramNode(bbox=e.bbox, fill=e.fill, shape=e.shape_type or e.shape, paragraphs=())
        case TargetImage() | TargetTable() | TargetDiagram():
            return DiagramNode(bbox=e.bbox, fill=None, shape=None, paragraphs=())
        case _:
            assert_never(e)


def fold_groups(elements: list[TargetElement], lines: Mapping[str, int]) -> list[TargetElement]:
    """Groups emit builds from one IR element, read back as that element: a number text box
    centred on a picture (a numbered ball) becomes the picture's `number`, and a group of node
    shapes joined by lines becomes a `diagram` with its nodes (texts and fills)."""
    drop: set[int] = set()
    elements = list(elements)
    for i, img in enumerate(elements):
        if not isinstance(img, TargetImage):
            continue
        x0, y0, x1, y1 = img.bbox
        for k, t in enumerate(elements):
            if not isinstance(t, TargetText) or t.group != img.group or k in drop or len(t.paragraphs) != 1:
                continue
            text = "".join(r.text for r in t.paragraphs[0].runs).strip()
            cx, cy = (t.bbox[0] + t.bbox[2]) / 2, (t.bbox[1] + t.bbox[3]) / 2
            if 0 < len(text) <= 3 and x0 <= cx <= x1 and y0 <= cy <= y1:
                elements[i] = replace(img, number=text, role="icon")
                drop.add(k)
                break
    out = [e for k, e in enumerate(elements) if k not in drop]
    groups: dict[str, list[TargetElement]] = {}
    for e in out:
        if e.group:
            groups.setdefault(e.group, []).append(e)
    for gid, members in groups.items():
        nodes = [e for e in members
                 if isinstance(e, TargetShape) or (isinstance(e, TargetText) and e.shape_type not in (None, "TEXT_BOX"))]
        if not lines.get(gid) or len(nodes) < 2 or any(isinstance(e, TargetImage) for e in members):
            continue
        dg = TargetDiagram(
            id=gid, object=gid, group=gid, key=None, role="figure", inherited=None,
            bbox=(min(e.bbox[0] for e in members), min(e.bbox[1] for e in members),
                  max(e.bbox[2] for e in members), max(e.bbox[3] for e in members)),
            nodes=tuple(_node(e) for e in members))
        first = next(i for i, e in enumerate(out) if e is members[0])
        out = [e for e in out if e.group != gid]
        out.insert(min(first, len(out)), dg)
    return out


def deck_ir(pres: JsonObject, pdf_size: Sequence[float] | None, base: JsonObject | None, fetch: Fetch | None,
            images: Path | None, foreign: bool, thumbnails: Thumbnails | None) -> JsonObject:
    """`read_target` of a `presentations.get` answer as JSON, as target.json holds it."""
    return target_json(read_target(presentation(pres, WHERE), pdf_size, base, fetch, images, foreign, thumbnails))


def fetch_url(url: str, fetch: Fetch | None) -> bytes:
    """A picture's bytes through the installed fetcher (`net`); raises when it cannot be had."""
    from . import net
    return net.download(url, fetch, 4)


def presentation_id(ref: str) -> str:
    """A deck URL, id or converted output folder (emit.json) -> presentation id."""
    p = Path(ref)
    if p.is_dir() and (p / "emit.json").exists():
        emitted = json.loads((p / "emit.json").read_text(encoding="utf-8"))
        return as_str(emitted["presentationId"] if isinstance(emitted, dict) else None, str(p / "emit.json"))
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def read_deck(ref: str, images: Path | None, base: JsonObject | None, pdf_size: Sequence[float] | None,
              slides: SlidesService | None, foreign: bool, keep: dict[str, Json] | None,
              pptx: bytes | None) -> TargetDeck:
    """Fetch a live deck and return its IR (pictures downloaded into `images`). `foreign`: read it as
    a deck nobody converted (`read_target`) - what `adopt` asks for and `pull` does not.

    `keep`: a dict the raw `presentations.get` answer is put into under "presentation". The IR says
    where things are, not which object they are; `adopt_sync` needs the read-back (object ids, text
    runs, transforms) to record a sync base, and this is how it gets it without a second fetch.

    `pptx`: a .pptx of this deck a person downloaded (File > Download). Its pictures are taken
    before any download is tried, and no Drive export is made: a process that may fetch nothing
    (`--no-downloads`, a sandbox) still gets them. `keep["pptx_pictures"]`: how many it held."""
    from .google_auth import slides_service
    from .google_types import as_json
    from .gslides import execute
    slides = slides or slides_service(None)
    pid = presentation_id(ref)
    read = execute(slides.presentations().get(presentationId=pid))
    if keep is not None:
        keep["presentation"] = as_json(read, pid)
    p = Path(ref)
    if pdf_size is None and p.is_dir() and (p / "deck.json").exists():
        deck = json.loads((p / "deck.json").read_text(encoding="utf-8"))
        pdf_size = _size_of(_objs(_obj(deck).get("slides"))[0].get("size"))
    if base is None and p.is_dir() and (p / "sync" / "base.json").exists():
        base = _obj(json.loads((p / "sync" / "base.json").read_text(encoding="utf-8")))
    # A foreign deck's gradients, table-style colours and the like are only in Google's own picture of
    # each slide (deck_fills.py), so adopt reads those too, next to the pictures ($B2S_ADOPT_THUMBNAILS=0: not).
    thumbnails = None
    if foreign and images is not None and os.environ.get("B2S_ADOPT_THUMBNAILS", "1") != "0":
        thumbnails = slide_thumbnails(pid, read, Path(images).parent / "thumbnails")
    fetch = picture_fetch(read, pptx, keep, True, True) if images else None
    return read_target(read, pdf_size or _size_of(None if base is None else base.get("page_size")), base, fetch,
                       images, foreign, thumbnails)


def picture_fetch(pres: Presentation, pptx: bytes | None, keep: dict[str, Json] | None, drive: bool,
                  pptx_first: bool) -> Fetch:
    """`fetch(url) -> bytes` for the pictures of `pres`: out of a supplied .pptx first, else
    downloaded where the fetcher allows, else out of one Drive export of the deck (`deck_pictures`:
    a harness that may not fetch a contentUrl still reads the pictures). `drive=False`: no export
    (a read that has no Google, `read_presentation`). `pptx_first=False`: the fetcher is asked
    before the .pptx (it replays recorded downloads, `deck_files`: the bytes Google serves, where a
    .pptx may hold a re-encoded copy). `keep["pptx_pictures"]`: what the .pptx held."""
    from .deck_pictures import WORKERS, LivePictures
    from .google_auth import drive_service, fetcher_for_threads, slides_service
    live = LivePictures(pres, None, fetcher_for_threads(), WORKERS, pptx, None)
    if pptx is not None and keep is not None:
        keep["pptx_pictures"] = len(live.exported or {})

    def fetch(url: str) -> bytes:
        oid = next((i for i, u in live.urls.items() if u == url), None)
        exported = live.exported or {}
        if live.supplied and oid in exported and pptx_first:
            return exported[oid]
        try:
            return fetch_url(url, live.fetch)
        except Exception:  # noqa: BLE001 - a harness's fetcher raises its own types
            if live.supplied and oid is not None and oid in exported:
                return exported[oid]
            if oid is None or live.supplied or not drive:
                raise  # (a supplied .pptx is the export: Drive is not asked for another)
            live.drive = live.drive or drive_service()
            live.slides = live.slides or slides_service  # (made only if the deck needs parts)
            data = live.export().get(oid)
            if not data:
                raise
            return data
    return fetch


def is_presentation(doc: Json) -> TypeGuard[JsonObject]:
    """Whether a JSON document is a raw `presentations.get` answer rather than an IR (whose page
    size is `page_size`, in points)."""
    return isinstance(doc, dict) and "pageSize" in doc and isinstance(doc.get("slides"), list)


def read_presentation(pres: JsonObject, images: Path, pptx: bytes | None, keep: dict[str, Json] | None,
                      thumbnails: Thumbnails | None, pptx_first: bool) -> TargetDeck:
    """A foreign deck's IR from a `presentations.get` answer someone saved, with no Google call:
    what `adopt` reads in a sandbox that was handed the deck as files. Its pictures come from the
    .pptx given (else a download, where the fetcher allows one; never a Drive export; with
    `pptx_first=False` the fetcher first, `picture_fetch`).
    `thumbnails`: the slides' LARGE thumbnails someone saved (`given_thumbnails`); without them
    what only they show - gradients, table-style colours, measured insets (`deck_fills`) - is not
    read, as with `$B2S_ADOPT_THUMBNAILS=0`."""
    if keep is not None:
        keep["presentation"] = pres
    read = presentation(pres, WHERE)
    fetch = picture_fetch(read, pptx, keep, False, pptx_first)
    return read_target(read, None, None, fetch, images, True, thumbnails)


THUMBNAIL_SUFFIXES = (".png", ".jpg", ".jpeg")
THUMBNAIL_ASPECT = 0.02   # a slide picture of another page shape is another deck's


def given_thumbnails(pres: JsonObject, files: Iterable[str | Path] | None,
                     log: Callable[[str], None]) -> tuple[Thumbnails, int]:
    """`read_target`'s `thumbnails` callback over slide pictures someone saved - Google's LARGE
    thumbnails as `slide_thumbnails` saves them - and how many slides got one.

    `files`: pictures or folders of them. A picture is a slide's by its name - the slide's objectId,
    or its number (`003.png`, `slide_thumbnails`' names) - and when no picture is named either way
    and there is one per slide, by order. One whose shape is not the page's is left out: a wrong
    picture would be read as the slide's fills."""
    read = presentation(pres, WHERE)
    slides = read.get("slides") or []
    ids = {object_id(s): i for i, s in enumerate(slides)}
    found: list[Path] = []
    for f in map(Path, files or []):
        found += sorted(p for p in f.iterdir() if p.suffix.lower() in THUMBNAIL_SUFFIXES) if f.is_dir() else [f]
    paths: list[Path | None] = [None] * len(slides)
    loose: list[Path] = []
    for f in found:
        if f.stem in ids:
            paths[ids[f.stem]] = f
        elif f.stem.isdigit() and 1 <= int(f.stem) <= len(slides):
            paths[int(f.stem) - 1] = f
        else:
            loose.append(f)
    if loose and len(loose) == len(found) == len(slides):
        paths = list(loose)
    elif loose:
        log(f"thumbnails: {len(loose)} picture(s) named for no slide were left out "
            f"(name them 001.png... or by the slide's objectId)")
    size = read.get("pageSize")
    if size is None:
        raise JsonShapeError(f"{WHERE}: the presentation has no pageSize")
    page = _dimension(size.get("width")) / _dimension(size.get("height"))
    from PIL import Image
    for n, p in enumerate(paths):
        if p is None:
            continue
        try:
            with Image.open(p) as img:
                w, h = img.size
        except Exception as exc:  # noqa: BLE001 - whatever a person hands over
            log(f"thumbnails: slide {n + 1}'s {p.name} is not a picture ({str(exc)[:60]})")
            paths[n] = None
            continue
        if abs(w / h - page) > THUMBNAIL_ASPECT * page:
            log(f"thumbnails: slide {n + 1}'s {p.name} is {w}x{h}, not the deck's page shape: left out")
            paths[n] = None

    def thumbnail(n: int) -> Path | None:
        return paths[n] if 0 <= n < len(paths) else None
    return thumbnail, sum(p is not None for p in paths)


def pictures_from_pptx(target: JsonObject, pres: JsonObject, data: bytes, images: Path) -> tuple[int, int]:
    """Give a saved IR the pictures it was read without - or read only as a crop of the slide's
    thumbnail (`picture_source`/`background_source` "thumbnail", `deck_fills.recover_pictures`) -
    out of a .pptx of the same deck (File > Download): an image element by the object it was read from (`object`, a layout's own for an
    inherited one), a slide's background picture by its URL. `pres` is the `presentations.get`
    the IR was read from - the .pptx is paired with it page by page (`exported_pictures`), so a
    download of another revision gives only the pages that still pair. -> (filled, held)."""
    from . import deck_fills
    from .deck_pictures import exported_pictures, picture_urls
    read = presentation(pres, "the presentation.json beside the target")
    exported = exported_pictures(data, read, None)
    by_url = {u: oid for oid, u in picture_urls(read).items()}

    def lacks(file: Json) -> bool:
        return not file or not isinstance(file, str) or not Path(file).exists()

    def put(oid: str | None) -> Stashed:
        if oid is None or oid not in exported:
            return NOT_STASHED
        picture = exported[oid]
        return stash_picture(oid, lambda _: picture, images)

    def fill(node: Json) -> int:
        n = 0
        if isinstance(node, dict):
            stand_in = node.get("picture_source") == "thumbnail"
            if node.get("kind") == "image" and not node.get("video") and (lacks(node.get("file")) or stand_in):
                got = put(_str(node.get("object")))
                if got.file:
                    if stand_in:
                        # the thumbnail's crop gives way to the file, drawn as the deck draws it again
                        # (its crop, turn, outline: `deck_fills.recover_pictures` kept them)
                        for key in deck_fills.THUMBNAIL_REPLACES:
                            node.pop(key, None)
                        node.update(_obj(node.pop("thumbnail_of", None)))
                        node.pop("picture_source", None)
                    node.pop("error", None)
                    node.update(stash_json(got))
                    n += 1
            n += sum(fill(v) for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            n += sum(fill(v) for v in node)
        return n

    filled = 0
    for s in _objs(target.get("slides")):
        filled += fill(s.get("elements"))
        bg = _str(s.get("background_picture"))
        if bg and (lacks(s.get("background_file")) or s.get("background_source")):
            got = put(by_url.get(bg))
            if got.file:
                s["background_file"] = got.file
                s.pop("background_source", None)
                filled += 1
    return filled, len(exported)


def slide_thumbnails(pid: str, pres: Presentation, folder: Path) -> Thumbnails:
    """Save every slide's LARGE thumbnail into `folder` (3 at a time: Google allows 60 such reads a
    minute, and a 429 waits) and return `read_target`'s `thumbnails` callback over them. A slide whose
    thumbnail could not be read is None there: its unreported fills stay out, as without any."""
    from concurrent.futures import ThreadPoolExecutor
    from .google_auth import credentials, fetcher_for_threads, slides_service
    from .gslides import save_thumbnail
    creds, fetch = credentials(), fetcher_for_threads()   # here: a worker inherits no context
    folder.mkdir(parents=True, exist_ok=True)

    def one(item: tuple[int, Page]) -> Path | None:
        i, s = item
        path = folder / f"{i + 1:03d}.png"             # read again every time: the deck may have changed
        for attempt in range(6):
            try:
                save_thumbnail(slides_service(creds), pid, object_id(s), path, fetch)
                return path
            except Exception as exc:                                  # noqa: BLE001
                if "429" not in str(exc) or attempt == 5:
                    print(f"  slide {i + 1}: no thumbnail ({str(exc)[:80]})")
                    return None
                time.sleep(20 + 10 * attempt)
        return None

    with ThreadPoolExecutor(3) as pool:
        paths = list(pool.map(one, enumerate(pres.get("slides") or [])))

    def thumbnail(n: int) -> Path | None:
        return paths[n] if 0 <= n < len(paths) else None
    return thumbnail
