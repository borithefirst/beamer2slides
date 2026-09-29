"""The showcase decks: six small decks written here, every word and picture ours, so the public
gallery shows adopt on decks we may publish.

Deck `hashing` is built natively through the Slides API (Google's own layouts and bullets); the
others are .pptx files imported by Drive, the way most decks people share were made. Each deck is
shared "anyone with the link can view" so the gallery links to the original.

  python tools/showcase.py decks [NAME...]   build (or rebuild in place), share, record decks.json

Text is styled as a cascade: a run's `RunLook` over its paragraph's `Para`, over its box's
`BoxLook` (the keywords of `text`, `words`, `table`...), over `BODY`. Each look says only what it
changes, so those are `total=False` TypedDicts - every key named and typed, none defaulted: an
absent key means "as the level above", resolved in one place (`styled`, `fill_text`).
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Union

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.dml.fill import FillFormat
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_VERTICAL_ANCHOR, PP_ALIGN, PP_PARAGRAPH_ALIGNMENT
from pptx.oxml.ns import qn
from pptx.parts.slide import BaseSlidePart
from pptx.shapes.autoshape import Shape
from pptx.shapes.connector import Connector
from pptx.shapes.graphfrm import GraphicFrame
from pptx.shapes.picture import Picture
from pptx.slide import Slide, SlideLayout, SlideMaster
from pptx.table import _Cell
from pptx.text.text import TextFrame, _Run
from pptx.util import Emu, Pt

from beamer2slides.devtools.showcase import art
from beamer2slides.json_types import Json, JsonObject, as_array, as_object, as_optional_str, as_str
from beamer2slides.paths import CHECKOUT
from beamer2slides.typing_compat import assert_never

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import SlidesService

W, H = 720, 405
MANIFEST = Path(__file__).with_name("decks.json")
ART = CHECKOUT / "out" / "showcase" / "art"
CORPUS = CHECKOUT / "out" / "showcase-corpus"      # adopt_bench's corpus for these decks only


def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h.lstrip("#"))


# --------------------------------------------------------------------------------------- decks.json

@dataclass(frozen=True, kw_only=True)
class Built:
    """A deck of decks.json once `build` has made it: its id, title and page ids in order."""
    id: str
    title: str
    pages: list[str]


def built_decks(path: Path) -> dict[str, Built]:
    """decks.json, every deck in it built (the gallery and `capture` read it)."""
    where = str(path)
    out: dict[str, Built] = {}
    for name, entry in as_object(json.loads(path.read_text(encoding="utf-8")), where).items():
        deck = as_object(entry, f"{where}: {name}")
        out[name] = Built(id=as_str(deck.get("id"), f"{where}: {name}.id"),
                          title=as_str(deck.get("title"), f"{where}: {name}.title"),
                          pages=[as_str(p, f"{where}: {name}.pages") for p in
                                 as_array(deck.get("pages"), f"{where}: {name}.pages")])
    return out


# --------------------------------------------------------------------------------------- .pptx kit

Align = Literal["l", "c", "r", "j"]
Anchor = Literal["t", "m", "b"]
ArrowHead = Literal["triangle", "stealth"]


class RunLook(TypedDict, total=False):
    """What one run sets over its paragraph."""
    font: str
    size: float
    color: str
    bold: bool
    italic: bool
    underline: bool
    link: str
    sup: bool
    spacing: float


Run = Union[str, tuple[str, RunLook]]


class BoxLook(TypedDict, total=False):
    """What a text box sets over `BODY`, and a paragraph over its box."""
    font: str
    size: float
    color: str
    bold: bool
    italic: bool
    align: Align
    rtl: bool
    space_after: float
    line: float


class Para(BoxLook, total=False):
    """One paragraph: its words (`text`, or `runs` of their own looks) and what it sets over its
    box, including a bullet (`bullet` glyph, or `number` in `scheme`) at `level`."""
    text: str
    runs: list[Run]
    space_before: float
    level: int
    indent: float
    bullet: str
    number: bool
    scheme: str
    bullet_color: str
    bullet_font: str


@dataclass(frozen=True, kw_only=True)
class Style:
    """A text box's look with nothing left to say (`styled`)."""
    font: str
    size: float
    color: str
    bold: bool
    italic: bool
    align: Align
    rtl: bool
    space_after: float | None
    line: float | None


BODY = Style(font="Inter", size=16, color="#222222", bold=False, italic=False, align="l", rtl=False,
             space_after=None, line=None)


def styled(base: Style, look: BoxLook) -> Style:
    """`base` with what `look` says over it."""
    return Style(font=look.get("font", base.font), size=look.get("size", base.size),
                 color=look.get("color", base.color), bold=look.get("bold", base.bold),
                 italic=look.get("italic", base.italic), align=look.get("align", base.align),
                 rtl=look.get("rtl", base.rtl), space_after=look.get("space_after", base.space_after),
                 line=look.get("line", base.line))


@dataclass(frozen=True, kw_only=True)
class Frame:
    """Where a box's words sit: `anchor` to its top, middle or bottom, `inset` pt from every edge
    (None: python-pptx's own insets)."""
    anchor: Anchor
    inset: float | None


TOP = Frame(anchor="t", inset=None)
MIDDLE = Frame(anchor="m", inset=None)


@dataclass(frozen=True, kw_only=True)
class Outline:
    colour: str
    width: float           # pt


@dataclass(frozen=True, kw_only=True)
class Paint:
    """A shape's fill (None: none) and outline (None: none)."""
    fill: str | None
    outline: Outline | None


def filled(colour: str) -> Paint:
    return Paint(fill=colour, outline=None)


def outlined(fill: str | None, colour: str, width: float) -> Paint:
    return Paint(fill=fill, outline=Outline(colour=colour, width=width))


def solid(fill: FillFormat, colour: str) -> None:
    """`fill.solid()` then `fill.fore_color.rgb = rgb(colour)`, which python-pptx types as a write
    to `Never` (its `_Fill.fore_color` raises): the same `a:srgbClr` written the way `ColorFormat`
    writes it, into the `a:solidFill` of the element the fill belongs to."""
    fill.solid()
    fill._xPr.find(qn("a:solidFill")).get_or_change_to_srgbClr().val = str(rgb(colour))


Crop = tuple[float, float, float, float]      # left, top, right, bottom, as shares of the picture


@dataclass(frozen=True, kw_only=True)
class Pictures:
    """The pictures the decks show (`pictures`)."""
    truchet: Path
    flow: Path
    rings: Path
    marble: Path
    dusk: Path
    dawn: Path
    rings_title: Path
    portrait1: Path
    portrait2: Path
    pie: Path
    ring_rev: Path
    ring_loyal: Path
    ring_stores: Path
    ring_ticket: Path


BLANK = 6          # the default template's layouts: 0 title, 1 title and body, 5 title only, 6 blank


class Deck:
    def __init__(self) -> None:
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Pt(W), Pt(H)
        # the default template is 4:3 at the same width: bring placeholders with a box of their own
        # to 16:9 (only those; inherited ones follow)
        for owner in [self.prs.slide_master, *self.prs.slide_layouts]:
            for ph in owner.placeholders:
                if ph._element.xpath("./p:spPr/a:xfrm"):
                    top, height = ph.top, ph.height
                    if top is None or height is None:
                        raise ValueError(f"placeholder {ph.name}: an a:xfrm without its offset or extent")
                    ph.top, ph.height = Emu(int(top * 0.75)), Emu(int(height * 0.75))

    def slide(self, *, bg: str | None, layout: int, notes: str | None) -> Slide:
        s = self.prs.slides.add_slide(self.prs.slide_layouts[layout])
        if bg:
            solid(s.background.fill, bg)
        if notes:
            frame = s.notes_slide.notes_text_frame
            if frame is None:
                raise ValueError("the default template's notes master has no body placeholder")
            frame.text = notes
        return s

    def pptx(self) -> io.BytesIO:
        buf = io.BytesIO()
        self.prs.save(buf)
        buf.seek(0)
        return buf


def title_of(slide: Slide) -> Shape:
    title = slide.shapes.title
    if title is None:
        raise ValueError("a slide whose layout has no title placeholder")
    return title


def placeholder(slide: Slide, idx: int) -> Shape:
    ph = slide.placeholders[idx]
    if not isinstance(ph, Shape):
        raise TypeError(f"placeholder {idx} holds no text")
    return ph


def set_font(run: _Run, name: str) -> None:
    run.font.name = name
    rpr = run._r.get_or_add_rPr()
    latin = rpr.find(qn("a:latin"))
    for tag in ("a:cs", "a:ea"):
        old = rpr.find(qn(tag))
        if old is not None:
            rpr.remove(old)
        el = etree.SubElement(rpr, qn(tag))
        el.set("typeface", name)
        latin.addnext(el)


def alignment(align: Align) -> PP_PARAGRAPH_ALIGNMENT:
    match align:
        case "l":
            return PP_ALIGN.LEFT
        case "c":
            return PP_ALIGN.CENTER
        case "r":
            return PP_ALIGN.RIGHT
        case "j":
            return PP_ALIGN.JUSTIFY
        case _:
            assert_never(align)


def vertical(anchor: Anchor) -> MSO_VERTICAL_ANCHOR:
    match anchor:
        case "t":
            return MSO_ANCHOR.TOP
        case "m":
            return MSO_ANCHOR.MIDDLE
        case "b":
            return MSO_ANCHOR.BOTTOM
        case _:
            assert_never(anchor)


def fill_text(tf: TextFrame, paras: Sequence[str | Para], box: Style) -> None:
    """Paragraphs into `tf`, each a str or a `Para`, styled over `box`."""
    for i, para in enumerate(paras):
        spec: Para = {"text": para} if isinstance(para, str) else para
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        st = styled(box, spec)
        p.alignment = alignment(st.align)
        if st.space_after is not None:
            p.space_after = Pt(st.space_after)
        space_before = spec.get("space_before")
        if space_before is not None:
            p.space_before = Pt(space_before)
        if st.line is not None:
            p.line_spacing = st.line
        ppr = p._p.get_or_add_pPr()
        if st.rtl:
            ppr.set("rtl", "1")
        level = spec.get("level", 0)
        bullet, number = spec.get("bullet"), spec.get("number", False)
        if bullet or number:
            indent = spec.get("indent", st.size * 1.1)
            mar = Pt(indent * (level + 1))
            ppr.set("marL", str(int(mar)))
            ppr.set("indent", str(-int(Pt(indent))))
            if level:
                ppr.set("lvl", str(level))
            bullet_color = spec.get("bullet_color")
            if bullet_color:
                clr = etree.SubElement(ppr, qn("a:buClr"))
                etree.SubElement(clr, qn("a:srgbClr")).set("val", bullet_color.lstrip("#"))
            if number:
                num = etree.SubElement(ppr, qn("a:buAutoNum"))
                num.set("type", spec.get("scheme", "arabicPeriod"))
            elif bullet:
                bullet_font = spec.get("bullet_font")
                if bullet_font:
                    etree.SubElement(ppr, qn("a:buFont")).set("typeface", bullet_font)
                etree.SubElement(ppr, qn("a:buChar")).set("char", bullet)
        text = spec.get("text")
        runs = [text] if text is not None else spec.get("runs")
        if runs is None:
            raise ValueError("a paragraph with neither text nor runs")
        for r in runs:
            words, o = (r, RunLook()) if isinstance(r, str) else r
            run = p.add_run()
            run.text = words
            set_font(run, o.get("font", st.font))
            run.font.size = Pt(o.get("size", st.size))
            run.font.bold = o.get("bold", st.bold)
            run.font.italic = o.get("italic", st.italic)
            if o.get("underline"):
                run.font.underline = True
            solid(run.font.fill, o.get("color", st.color))
            if o.get("sup"):
                run._r.get_or_add_rPr().set("baseline", "30000")
            spacing = o.get("spacing")
            if spacing is not None:
                run._r.get_or_add_rPr().set("spc", str(int(spacing * 100)))
            link = o.get("link")
            if link:
                run.hyperlink.address = link


def _frame(tf: TextFrame, frame: Frame) -> None:
    tf.word_wrap = True
    tf.vertical_anchor = vertical(frame.anchor)
    if frame.inset is not None:
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Pt(frame.inset)


def text(slide: Slide, x: float, y: float, w: float, h: float, frame: Frame, paras: Sequence[str | Para],
         **look: Unpack[BoxLook]) -> Shape:
    tb = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
    _frame(tb.text_frame, frame)
    fill_text(tb.text_frame, paras, styled(BODY, look))
    return tb


def no_shadow(sp: Shape) -> None:
    sppr = sp._sp.spPr
    if sppr.find(qn("a:effectLst")) is None:
        etree.SubElement(sppr, qn("a:effectLst"))


def _look(s: Shape, paint: Paint) -> None:
    if paint.fill:
        solid(s.fill, paint.fill)
    else:
        s.fill.background()
    if paint.outline:
        solid(s.line.fill, paint.outline.colour)
        s.line.width = Pt(paint.outline.width)
    else:
        s.line.fill.background()
    no_shadow(s)


def shape(slide: Slide, kind: MSO_AUTO_SHAPE_TYPE, x: float, y: float, w: float, h: float, paint: Paint) -> Shape:
    s = slide.shapes.add_shape(kind, Pt(x), Pt(y), Pt(w), Pt(h))
    _look(s, paint)
    return s


def adjust(s: Shape, values: Sequence[float]) -> Shape:
    """`s` with its preset's adjustment values, in order."""
    for i, v in enumerate(values):
        s.adjustments[i] = v
    return s


def turn(s: Shape, degrees: float) -> Shape:
    s.rotation = degrees
    return s


def words(s: Shape, frame: Frame, paras: Sequence[str | Para], **look: Unpack[BoxLook]) -> Shape:
    """`s` with words in it."""
    _frame(s.text_frame, frame)
    fill_text(s.text_frame, paras, styled(BODY, look))
    return s


def arrow(slide: Slide, x1: float, y1: float, x2: float, y2: float, *, color: str, w: float,
          head: ArrowHead) -> Connector:
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
    solid(c.line.fill, color)
    c.line.width = Pt(w)
    ln = c.line._get_or_add_ln()
    etree.SubElement(ln, qn("a:tailEnd")).set("type", head)
    return c


def freeform(slide: Slide, pts: Sequence[tuple[float, float]], paint: Paint, *, closed: bool) -> Shape:
    fb = slide.shapes.build_freeform(Pt(pts[0][0]), Pt(pts[0][1]), scale=1.0)
    fb.add_line_segments([(Pt(x), Pt(y)) for x, y in pts[1:]], close=closed)
    s = fb.convert_to_shape()
    if not isinstance(s, Shape):
        raise TypeError(f"a freeform came back as {type(s).__name__}")
    _look(s, paint)
    return s


def blob(cx: float, cy: float, rx: float, ry: float, bumps: int, *, depth: float, n: int,
         phase: float) -> list[tuple[float, float]]:
    """A cloud-like closed outline: an ellipse with `bumps` scallops."""
    out: list[tuple[float, float]] = []
    for k in range(n):
        t = 2 * math.pi * k / n
        r = 1 + depth * abs(math.sin(bumps * t / 2 + phase))
        out.append((cx + rx * r * math.cos(t), cy + ry * r * math.sin(t)))
    return out


def cloud(cx: float, cy: float, rx: float, ry: float, bumps: int) -> list[tuple[float, float]]:
    """`blob` as most clouds here are: scallops 0.12 deep, 96 points, no phase."""
    return blob(cx, cy, rx, ry, bumps, depth=0.12, n=96, phase=0.0)


def star(cx: float, cy: float, r_out: float, r_in: float) -> list[tuple[float, float]]:
    """A five-pointed star, a point up."""
    points, rot = 5, -90
    out: list[tuple[float, float]] = []
    for k in range(points * 2):
        r = r_out if k % 2 == 0 else r_in
        a = math.radians(rot + 180 * k / points)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def picture(slide: Slide, path: Path, x: float, y: float, w: float, h: float, *, crop: Crop | None) -> Picture:
    p = slide.shapes.add_picture(str(path), Pt(x), Pt(y), Pt(w), Pt(h))
    if crop:
        p.crop_left, p.crop_top, p.crop_right, p.crop_bottom = crop
    return p


def _border(cell: _Cell, color: str, w: float) -> None:
    """A `w` pt rule of `color` on every side of `cell`."""
    tcpr = cell._tc.get_or_add_tcPr()
    for side in "LRTB":
        old = tcpr.find(qn(f"a:ln{side}"))
        if old is not None:
            tcpr.remove(old)
    for at, side in enumerate("LRTB"):
        ln = etree.Element(qn(f"a:ln{side}"))
        ln.set("w", str(int(Pt(w))))
        sf = etree.SubElement(ln, qn("a:solidFill"))
        etree.SubElement(sf, qn("a:srgbClr")).set("val", color.lstrip("#"))
        tcpr.insert(at, ln)


def table(slide: Slide, x: float, y: float, rows: Sequence[Sequence[str | Para]], col_w: Sequence[float],
          row_h: float, *, fills: Sequence[Sequence[str]], colors: Sequence[Sequence[str]], bold_rows: Sequence[int],
          aligns: Sequence[Align], border: str, border_w: float, **look: Unpack[BoxLook]) -> GraphicFrame:
    """`rows` of cells (a str or a paragraph); `fills[i][j]` / `colors[i][j]`: cell (i, j)'s ground and
    words; `aligns[j]`: column j's; rows `bold_rows` bold; `border` rules `border_w` pt wide."""
    nr, nc = len(rows), len(col_w)
    gf = slide.shapes.add_table(nr, nc, Pt(x), Pt(y), Pt(sum(col_w)), Pt(row_h * nr))
    t = gf.table
    tblpr = t._tbl.get_or_add_tblPr()      # a new table's own `firstRow="1" bandRow="1"` one
    for flag in ("firstRow", "bandRow"):
        tblpr.set(flag, "0")
    for j, cw in enumerate(col_w):
        t.columns[j].width = Pt(cw)
    for i in range(nr):
        t.rows[i].height = Pt(row_h)
    base = styled(BODY, look)
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            cell = t.cell(i, j)
            if cell.is_spanned:
                continue
            f = fills[i][j]
            if f:
                solid(cell.fill, f)
            else:
                cell.fill.background()
            cell.margin_left = cell.margin_right = Pt(6)
            cell.margin_top = cell.margin_bottom = Pt(3)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            fill_text(cell.text_frame, [val], styled(base, {"color": colors[i][j], "align": aligns[j],
                                                            "bold": i in bold_rows}))
            _border(cell, border, border_w)
    return gf


Owner = Union[SlideMaster, SlideLayout]


def decorate(owner: Owner, kind: str, x: float, y: float, w: float, h: float, paint: Paint) -> Shape:
    """A shape on a master or layout (python-pptx only adds them to slides); `kind` a preset's name."""
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    s = Shape(tree.add_autoshape(sid, f"Decoration {sid}", kind, Pt(x), Pt(y), Pt(w), Pt(h)), owner.shapes)
    _look(s, paint)
    return s


def decorate_picture(owner: Owner, path: Path, x: float, y: float, w: float, h: float) -> None:
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    part = owner.part
    if not isinstance(part, BaseSlidePart):
        raise TypeError(f"a master or layout whose part is a {type(part).__name__}")
    _, rid = part.get_or_add_image_part(str(path))
    tree.add_pic(sid, f"Decoration {sid}", "", rid, Pt(x), Pt(y), Pt(w), Pt(h))


def decorate_text(owner: Owner, x: float, y: float, w: float, h: float, paras: Sequence[str | Para],
                  **look: Unpack[BoxLook]) -> None:
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    sp = tree.add_textbox(sid, f"Decoration {sid}", Pt(x), Pt(y), Pt(w), Pt(h))
    s = Shape(sp, owner.shapes)
    _frame(s.text_frame, Frame(anchor="m", inset=0))
    fill_text(s.text_frame, paras, styled(BODY, look))


Box = tuple[float, float, float, float]      # x, y, w, h (pt)


def placeholder_text(ph: Shape, paras: Sequence[str | Para], box: Box, **look: Unpack[BoxLook]) -> None:
    """`ph` moved to `box`, holding `paras` instead of its prompt."""
    ph.left, ph.top, ph.width, ph.height = (Pt(v) for v in box)
    tf = ph.text_frame
    tf.clear()
    fill_text(tf, paras, styled(BODY, look))


CANVAS = (900, 600)       # px: most pictures' size


def pictures() -> Pictures:
    ART.mkdir(parents=True, exist_ok=True)

    def ring(name: str, share: float, colour: str) -> Path:
        return art.ring_chart(ART / f"{name}.png", share, colour=colour, track="#e8e2d6", size=400, width=40)

    return Pictures(
        truchet=art.truchet(ART / "truchet.png", 7, size=CANVAS, cell=60, fg="#f2c14e", bg="#1d2d44", width=9),
        flow=art.flow(ART / "flow.png", 11, size=CANVAS, bg="#0f1a20",
                      colours=("#e76f51", "#f4a261", "#e9c46a", "#2a9d8f")),
        rings=art.rings(ART / "rings.png", 5, size=CANVAS, bg="#f6efe6",
                        colours=("#264653", "#2a9d8f", "#e9c46a", "#e76f51")),
        marble=art.marble(ART / "marble.png", 3, size=CANVAS, a="#a8dadc", b="#457b9d", c="#f1faee"),
        dusk=art.landscape(ART / "dusk.png", 21, size=(1600, 900), sky=("#1b263b", "#e07a5f"),
                           layers=("#3d405b", "#2b2d42", "#1d1e2c", "#11121a")),
        dawn=art.landscape(ART / "dawn.png", 4, size=(1600, 900), sky=("#355070", "#eaac8b"),
                           layers=("#6d597a", "#56445d", "#3d3047", "#241c2b")),
        rings_title=art.rings(ART / "rings_title.png", 9, size=(600, 900), bg="#16213e",
                              colours=("#e94560", "#0f3460", "#f5b971", "#53354a")),
        portrait1=art.portrait(ART / "portrait1.png", 1, size=(600, 600), bg="#e9edc9",
                               colours=("#ccd5ae", "#d4a373", "#faedcd", "#a3b18a")),
        portrait2=art.portrait(ART / "portrait2.png", 2, size=(600, 600), bg="#cdb4db",
                               colours=("#ffc8dd", "#bde0fe", "#a2d2ff", "#ffafcc")),
        pie=art.pie(ART / "pie.png", [("Espresso drinks", 46, "#6f4e37"), ("Filter coffee", 21, "#c69c6d"),
                                      ("Pastries", 19, "#e9c46a"), ("Beans to go", 14, "#2a9d8f")],
                    size=CANVAS, bg="#ffffff", ink="#2f3640"),
        ring_rev=ring("ring_rev", 0.81, "#2a9d8f"),
        ring_loyal=ring("ring_loyal", 0.64, "#e9c46a"),
        ring_stores=ring("ring_stores", 0.92, "#8ab17d"),
        ring_ticket=ring("ring_ticket", 0.47, "#e76f51"),
    )


# ------------------------------------------------------------------------------ B: conference talk

def talk(pics: Pictures) -> Deck:
    d = Deck()
    navy, coral, sand, ink = "#16213e", "#e94560", "#f5b971", "#1f2433"
    master = d.prs.slide_master
    decorate(master, "rect", 0, 389, W, 16, filled(navy))
    decorate(master, "rect", 0, 389, 150, 16, filled(coral))
    decorate(master, "ellipse", 684, 18, 14, 14, filled(sand))
    decorate_text(master, 470, 389, 236, 16, [{"text": "Lumen Summit 2026 · Lisbon", "align": "r"}],
                  font="Poppins", size=8, color="#ffffff")
    title_layout = d.prs.slide_layouts[0]
    solid(title_layout.background.fill, navy)
    decorate_picture(title_layout, pics.rings_title, 480, 0, 240, 360)
    decorate(title_layout, "rect", 40, 250, 60, 5, filled(coral))

    s = d.slide(bg=None, layout=0, notes="Open with the pager story: three alerts on a Friday deploy.")
    placeholder_text(title_of(s), ["Shipping calm software"], (40, 110, 420, 110),
                     font="Poppins", size=40, bold=True, color="#ffffff", line=0.95)
    placeholder_text(placeholder(s, 1), [{"text": "Small batches, feature flags and the numbers that told us to slow down",
                                          "size": 15}, {"text": "A. Rivera · platform team", "size": 12, "color": sand,
                                                        "space_before": 10}],
                     (40, 268, 400, 80), font="Inter", color="#e6e8ef")

    s = d.slide(bg=None, layout=1, notes=None)
    placeholder_text(title_of(s), ["Agenda"], (40, 30, 640, 50), font="Poppins", size=28, bold=True,
                     color=navy)
    items = [("Why deploys feel scary", "a short history of our worst Friday"),
             ("Small batches", "one idea per pull request"),
             ("Feature flags", "ship dark, turn on slowly"),
             ("What we measure now", "and what we stopped measuring")]
    paras: list[str | Para] = []
    for head, sub in items:
        paras.append({"runs": [(head, {"bold": True}), (" — " + sub, {"color": "#5c6378"})], "number": True,
                      "size": 18, "space_after": 12, "bullet_color": coral})
    placeholder_text(placeholder(s, 1), paras, (48, 100, 600, 250), font="Inter", color=ink)

    s = d.slide(bg=None, layout=5, notes=None)
    placeholder_text(title_of(s), ["Three numbers that changed our mind"], (40, 30, 640, 50),
                     font="Poppins", size=26, bold=True, color=navy)
    stats = [("14 min", "median time from merge to production", coral),
             ("92%", "of deploys paged nobody", "#0f3460"),
             ("3×", "more releases per week than last year", "#53354a")]
    for k, (big, small, col) in enumerate(stats):
        x = 40 + k * 218
        adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 110, 202, 200, filled("#f4f5f9")), [0.08])
        shape(s, MSO_SHAPE.RECTANGLE, x, 110, 202, 6, filled(col))
        text(s, x + 16, 135, 170, 70, TOP, [big], font="Poppins", size=44, bold=True, color=col)
        text(s, x + 16, 215, 170, 80, TOP, [small], font="Inter", size=14, color=ink)
    text(s, 40, 330, 640, 30, TOP, [{"runs": [("Source: our deploy log, January to August. ", {}),
                                              ("How we count", {"link": "https://example.com/deploy-metrics",
                                                                "color": "#0f3460", "underline": True})]}],
         font="Inter", size=10, color="#8a8fa3")

    s = d.slide(bg=None, layout=5, notes=None)
    placeholder_text(title_of(s), ["A week of small batches"], (40, 30, 640, 50), font="Poppins",
                     size=26, bold=True, color=navy)
    arrow(s, 60, 200, 670, 200, color="#c3c7d4", w=3, head="triangle")
    days = [("Mon", "Design review", "one page, one owner"), ("Tue", "Flag off", "code merged dark"),
            ("Wed", "5% of users", "watch the error budget"), ("Thu", "50%", "compare cohorts"),
            ("Fri", "100%", "delete the flag")]
    for k, (day, what, why) in enumerate(days):
        cx = 90 + k * 132
        col = coral if k == 4 else "#0f3460"
        words(shape(s, MSO_SHAPE.OVAL, cx - 22, 178, 44, 44, filled(col)), Frame(anchor="m", inset=0), [day],
              font="Poppins", size=11, bold=True, color="#ffffff", align="c")
        y = 110 if k % 2 == 0 else 238
        text(s, cx - 60, y, 120, 56, Frame(anchor="b" if k % 2 == 0 else "t", inset=None),
             [{"text": what, "bold": True, "size": 13}, {"text": why, "size": 11, "color": "#5c6378"}],
             font="Inter", color=ink, align="c")

    s = d.slide(bg=None, layout=BLANK, notes="Close on the quote, then the link.")
    picture(s, pics.dusk, 0, 0, 330, 389, crop=(0.25, 0.0, 0.25, 0.0))
    text(s, 370, 90, 310, 150, TOP, [{"text": "“The calmest deploy is the one nobody noticed.”", "size": 26,
                                      "font": "Lora", "italic": True, "line": 1.05},
                                     {"text": "— the on-call rota, mid-August", "size": 13, "color": "#5c6378",
                                      "space_before": 14}], font="Inter", color=ink)
    words(adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 370, 280, 190, 40, filled(coral)), [0.5]), MIDDLE,
          ["Slides + notes: lumen.example/calm"], font="Inter", size=11, bold=True, color="#ffffff", align="c")
    return d


# ---------------------------------------------------------------------------- C: quarterly review

def review(pics: Pictures) -> Deck:
    d = Deck()
    brown, cream, ink, green, red = "#3e2c23", "#faf6ef", "#2b2521", "#2a7f62", "#c0392b"

    s = d.slide(bg=cream, layout=BLANK, notes=None)
    picture(s, pics.marble, 430, 0, 290, 405, crop=(0.2, 0.0, 0.2, 0.0))
    shape(s, MSO_SHAPE.RECTANGLE, 40, 120, 8, 120, filled(brown))
    text(s, 60, 110, 360, 140, TOP, [{"text": "Harbor & Pine Coffee", "size": 16, "color": "#8c6b4f", "bold": True},
                                     {"text": "Q3 business review", "size": 36, "font": "Playfair Display",
                                      "bold": True},
                                     {"text": "July – September · prepared for the board", "size": 13,
                                      "color": "#6b5d52", "space_before": 6}], font="Montserrat", color=ink)

    s = d.slide(bg=cream, layout=BLANK, notes="Revenue is ahead of plan; ticket size slipped with the summer menu.")
    text(s, 40, 26, 640, 40, TOP, ["Q3 at a glance"], font="Playfair Display", size=26, bold=True, color=ink)
    kpis = [("Revenue", "$1.42M", "+8.1% vs Q2", green, pics.ring_rev, "81% of annual plan"),
            ("Loyalty members", "18.4k", "+23% vs Q2", green, pics.ring_loyal, "64% of target"),
            ("Stores open", "12", "+2 this quarter", green, pics.ring_stores, "92% of sites staffed"),
            ("Average ticket", "$7.85", "−1.2% vs Q2", red, pics.ring_ticket, "47% add a pastry")]
    for k, (label, value, delta, col, ring, note) in enumerate(kpis):
        x = 40 + k * 163
        adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 86, 150, 270, outlined("#ffffff", "#e6dccd", 1.0)), [0.06])
        text(s, x + 12, 96, 126, 20, TOP, [label.upper()], font="Montserrat", size=9, bold=True, color="#8c6b4f")
        text(s, x + 12, 116, 126, 44, TOP, [value], font="Montserrat", size=28, bold=True, color=ink)
        text(s, x + 12, 160, 126, 20, TOP, [delta], font="Montserrat", size=11, bold=True, color=col)
        picture(s, ring, x + 35, 196, 80, 80, crop=None)
        text(s, x + 8, 290, 134, 50, TOP, [note], font="Montserrat", size=10, color="#6b5d52", align="c")

    s = d.slide(bg=cream, layout=BLANK, notes=None)
    text(s, 40, 26, 640, 40, TOP, ["Sales by store, in $k"], font="Playfair Display", size=26, bold=True, color=ink)
    stores = [("Harbor St", [52, 58, 61]), ("Pine Ave", [44, 47, 45]), ("Old Mill", [31, 36, 41]),
              ("Station", [63, 60, 66]), ("Riverside", [22, 28, 35])]
    lo, hi = 20, 70

    def heat(v: int) -> str:
        t = (v - lo) / (hi - lo)
        a, b = (0xf6, 0xe7, 0xd4), (0x8c, 0x4a, 0x2f)
        return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(a, b))

    rows = [["Store", "Jul", "Aug", "Sep", "Q3"]]
    fills = [[brown] * 5]
    colors = [["#ffffff"] * 5]
    for name, vals in stores:
        rows.append([name] + [str(v) for v in vals] + [str(sum(vals))])
        fills.append(["#ffffff"] + [heat(v) for v in vals] + ["#efe7da"])
        colors.append([ink] + ["#ffffff" if v > 48 else ink for v in vals] + [ink])
    rows.append(["All stores"] + [str(sum(vals[i] for _, vals in stores)) for i in (0, 1, 2)]
                + [str(sum(sum(vals) for _, vals in stores))])
    fills.append(["#efe7da"] * 5)
    colors.append([ink] * 5)
    table(s, 40, 84, rows, [150, 80, 80, 80, 90], 34, fills=fills, colors=colors, bold_rows=(0, 6),
          aligns=["l", "r", "r", "r", "r"], border="#ffffff", border_w=1.5, font="Montserrat", size=13)
    text(s, 540, 90, 150, 230, TOP, [{"text": "Reading the grid", "bold": True, "size": 13, "space_after": 6},
                                     {"text": "Darker cells sold more.", "bullet": "•", "size": 11, "space_after": 4},
                                     {"text": "Riverside grew 59% since July.", "bullet": "•", "size": 11,
                                      "space_after": 4},
                                     {"text": "Pine Ave is flat: roadworks until October.", "bullet": "•",
                                      "size": 11}],
         font="Montserrat", color=ink)

    s = d.slide(bg=cream, layout=BLANK, notes=None)
    text(s, 40, 26, 640, 40, TOP, ["What people buy"], font="Playfair Display", size=26, bold=True, color=ink)
    picture(s, pics.pie, 30, 80, 420, 280, crop=None)
    shape(s, MSO_SHAPE.RECTANGLE, 470, 90, 220, 260, filled("#efe7da"))
    text(s, 486, 100, 196, 240, TOP, [{"text": "Takeaways", "bold": True, "size": 15, "space_after": 8},
                                      {"text": "Espresso drinks are almost half of sales.", "bullet": "–",
                                       "space_after": 6},
                                      {"text": "Beans to go doubled after the subscription launch.", "bullet": "–",
                                       "space_after": 6},
                                      {"text": "Pastries carry the best margin: 68%.", "bullet": "–"}],
         font="Montserrat", size=12, color=ink)

    s = d.slide(bg=brown, layout=BLANK, notes=None)
    text(s, 40, 30, 640, 40, TOP, ["Next quarter"], font="Playfair Display", size=26, bold=True, color="#ffffff")
    plan = [("✓", "Winter menu tasting, October 4"), ("✓", "Hire two shift leads for Station"),
            ("☐", "Open the Riverside patio heaters"), ("☐", "Loyalty app: pre-order at the counter"),
            ("☐", "Renegotiate the milk contract")]
    text(s, 40, 90, 380, 260, TOP, [{"text": t, "bullet": b, "bullet_font": "Segoe UI Symbol", "space_after": 10,
                                     "bullet_color": "#e9c46a"} for b, t in plan],
         font="Montserrat", size=16, color="#fdf6ec")
    words(adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 450, 100, 230, 190, filled("#fdf6ec")), [0.1]),
          Frame(anchor="t", inset=14),
          [{"text": "Target for Q4", "bold": True, "size": 13, "color": "#8c6b4f"},
           {"text": "$1.55M", "size": 40, "bold": True, "font": "Playfair Display"},
           {"text": "revenue, with ticket size back above $8.00", "size": 12}],
          font="Montserrat", color=ink, align="l")
    return d


# -------------------------------------------------------------------------------- D: bees, for kids

def bees(pics: Pictures) -> Deck:
    d = Deck()
    sky, honey, dark, leaf = "#fff4d6", "#f6a623", "#3b2a14", "#7cb342"
    font = "Fredoka"

    def hexagon(s: Slide, cx: float, cy: float, r: float, fill: str) -> Shape:
        return adjust(shape(s, MSO_SHAPE.HEXAGON, cx - r, cy - r * 0.866, 2 * r, 2 * r * 0.866,
                            outlined(fill, "#ffffff", 3)), [0.25, 1.1547])

    def bee(s: Slide, x: float, y: float, k: float) -> None:
        wing = outlined("#dff3ff", "#9fd3ef", 1.0)
        turn(shape(s, MSO_SHAPE.OVAL, x + 8 * k, y - 16 * k, 26 * k, 22 * k, wing), -20)
        turn(shape(s, MSO_SHAPE.OVAL, x + 24 * k, y - 18 * k, 26 * k, 22 * k, wing), 20)
        shape(s, MSO_SHAPE.OVAL, x, y, 60 * k, 40 * k, outlined("#ffd23f", dark, 2))
        for sx in (18, 32):
            shape(s, MSO_SHAPE.RECTANGLE, x + sx * k, y + 3 * k, 7 * k, 34 * k, filled(dark))
        shape(s, MSO_SHAPE.OVAL, x + 44 * k, y + 12 * k, 6 * k, 6 * k, filled(dark))

    s = d.slide(bg=sky, layout=BLANK, notes="Ask: who has seen a honeycomb up close?")
    for (cx, cy), f in zip([(520, 130), (590, 170), (590, 250), (520, 290), (450, 250), (450, 170), (520, 210)],
                           [honey, "#f9c74f", honey, "#f9c74f", honey, "#f9c74f", "#f8961e"]):
        hexagon(s, cx, cy, 46, f)
    freeform(s, star(640, 60, 26, 11), outlined("#ffd23f", honey, 2), closed=True)
    freeform(s, cloud(120, 70, 70, 26, 7), filled("#ffffff"), closed=True)
    bee(s, 330, 300, 1.2)
    text(s, 40, 120, 360, 150, TOP, [{"text": "How bees build", "size": 26, "color": "#8a5a00"},
                                     {"text": "hexagons", "size": 54, "bold": True}],
         font=font, color=dark)

    s = d.slide(bg=sky, layout=BLANK, notes=None)
    text(s, 40, 22, 640, 40, TOP, ["Why not squares or triangles?"], font=font, size=28, bold=True, color=dark)
    cols = [("Triangles", "3 walls meet at each corner", MSO_SHAPE.ISOSCELES_TRIANGLE, "most wax"),
            ("Squares", "4 walls meet at each corner", MSO_SHAPE.RECTANGLE, "more wax"),
            ("Hexagons", "the roundest shape that tiles", MSO_SHAPE.HEXAGON, "least wax!")]
    for k, (name, why, kind, wax) in enumerate(cols):
        x = 50 + k * 215
        adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 80, 190, 230, outlined("#ffffff", "#f3d9a4", 2)), [0.12])
        shp = shape(s, kind, x + 55, 102, 80, 70, filled([leaf, "#4fc3f7", honey][k]))
        if kind == MSO_SHAPE.HEXAGON:
            shp.adjustments[0] = 0.25
        text(s, x + 10, 180, 170, 30, TOP, [name], font=font, size=18, bold=True, color=dark, align="c")
        text(s, x + 10, 210, 170, 44, TOP, [why], font="Nunito", size=12, color="#5b4a33", align="c")
        words(adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x + 45, 262, 100, 30,
                           filled(honey if k == 2 else "#efe3c8")), [0.5]),
              MIDDLE, [wax], font=font, size=12, bold=True, color=dark, align="c")
    words(adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGULAR_CALLOUT, 470, 330, 220, 50, outlined("#ffffff", dark, 1.5)),
                 [-0.62, 0.1, 0.16667]),
          MIDDLE, ["Less wax means more honey!"], font=font, size=14, color=dark, align="c")
    bee(s, 370, 336, 1.0)

    s = d.slide(bg="#e8f5e9", layout=BLANK, notes="Hand out paper circles; everyone squashes six round cells together.")
    text(s, 40, 22, 640, 40, TOP, ["Try it: squash the circles"], font=font, size=28, bold=True, color="#1b5e20")
    steps = ["Cut out seven paper circles.", "Put one in the middle, six around it.",
             "Push them all together from the outside.", "Look at the middle one. What shape is it now?"]
    text(s, 40, 86, 330, 260, TOP, [{"text": t, "number": True, "space_after": 12, "bullet_color": "#1b5e20"}
                                    for t in steps], font="Nunito", size=17, color="#263238")
    for k in range(6):
        a = math.radians(60 * k)
        shape(s, MSO_SHAPE.OVAL, 500 + 58 * math.cos(a) - 30, 200 + 58 * math.sin(a) - 30, 60, 60,
              outlined("#ffffff", "#1b5e20", 2))
    shape(s, MSO_SHAPE.OVAL, 470, 170, 60, 60, outlined("#c5e1a5", "#1b5e20", 2))
    freeform(s, [(395, 330), (445, 310), (445, 320), (470, 300), (445, 280), (445, 290), (395, 310)],
             filled(honey), closed=True)
    freeform(s, star(640, 330, 24, 10), outlined("#ffd23f", honey, 1.0), closed=True)

    s = d.slide(bg=sky, layout=BLANK, notes=None)
    text(s, 40, 22, 640, 40, TOP, ["Did you know?"], font=font, size=28, bold=True, color=dark)
    facts = [("6", "sides on every cell", honey, -4), ("30", "days a worker bee lives in summer", leaf, 3),
             ("8", "wax flakes for a single cell", "#4fc3f7", -2)]
    for k, (n, fact, col, rot) in enumerate(facts):
        x = 50 + k * 215
        card = turn(adjust(shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 90, 190, 200, outlined("#ffffff", col, 4)),
                           [0.1]), rot)
        words(turn(shape(s, MSO_SHAPE.OVAL, x + 60, 110, 70, 70, filled(col)), rot), MIDDLE, [n], font=font,
              size=30, bold=True, color="#ffffff", align="c")
        text(s, x + 15, 195, 160, 80, TOP, [fact], font="Nunito", size=15, color=dark, align="c")
        card.name = f"Fact {k + 1}"
    freeform(s, blob(600, 350, 80, 22, 9, depth=0.18, n=96, phase=0.0), filled("#ffffff"), closed=True)
    return d


# ----------------------------------------------------------------------- E: water cycle, 4 scripts

@dataclass(frozen=True, kw_only=True)
class Lesson:
    """The water cycle in one language: its title, the font and direction it is set in, the four steps."""
    title: str
    font: str
    rtl: bool
    steps: list[str]


WATER = {
    "he": Lesson(title="מחזור המים", font="Noto Sans Hebrew", rtl=True,
                 steps=["אידוי: השמש מחממת את המים באוקיינוס", "עיבוי: אדי המים מתקררים ויוצרים עננים",
                        "משקעים: גשם ושלג יורדים אל האדמה", "איסוף: המים זורמים חזרה אל הים"]),
    "ar": Lesson(title="دورة الماء", font="Noto Sans Arabic", rtl=True,
                 steps=["التبخر: تسخن الشمس مياه المحيط", "التكاثف: يبرد بخار الماء فتتكون السحب",
                        "الهطول: يسقط المطر والثلج على الأرض", "التجمع: تعود المياه إلى البحر"]),
    "ja": Lesson(title="水の循環", font="Noto Sans JP", rtl=False,
                 steps=["蒸発：太陽が海の水をあたためます", "凝結：水蒸気が冷えて雲になります",
                        "降水：雨や雪が地面に降ります", "集水：水は川を通って海にもどります"]),
    "zh": Lesson(title="水循环", font="Noto Sans SC", rtl=False,
                 steps=["蒸发：太阳加热海洋中的水", "凝结：水蒸气冷却后形成云", "降水：雨和雪落到地面",
                        "汇集：水沿着河流流回大海"]),
}


def water(pics: Pictures) -> Deck:
    d = Deck()
    sea, deep, sun, cloud_white, ink = "#4cc9f0", "#1d3557", "#ffb703", "#ffffff", "#1d3557"

    def scene(s: Slide, x0: float, labels: Sequence[str], font: str, mirror: bool) -> None:
        """Sun, sea, cloud and the four arrows of the cycle, in a 300 x 280 box at x0."""
        def X(x: float) -> float:
            return x0 + (300 - x if mirror else x)
        shape(s, MSO_SHAPE.RECTANGLE, x0, 250, 300, 60, filled(sea))
        freeform(s, [(X(t * 10), 250 - 6 * math.sin(t * 0.9)) for t in range(31)] + [(X(300), 262), (X(0), 262)],
                 filled(sea), closed=True)
        shape(s, MSO_SHAPE.OVAL, X(40) - 25, 70, 50, 50, filled(sun))
        freeform(s, cloud(X(200), 95, 58, 24, 7), outlined(cloud_white, "#a8dadc", 1.5), closed=True)
        for x in (180, 200, 220):
            arrow(s, X(x), 128, X(x - 8), 158, color="#457b9d", w=1.5, head="triangle")
        arrow(s, X(70), 240, X(140), 140, color="#e76f51", w=2.5, head="triangle")
        arrow(s, X(250), 180, X(250), 240, color="#457b9d", w=2.5, head="triangle")
        freeform(s, [(X(250), 250), (X(210), 262), (X(150), 258), (X(100), 268)], outlined(None, "#1d3557", 2),
                 closed=False)
        spots = [(40, 190), (185, 38), (170, 200), (230, 272)]
        for (lx, ly), lab in zip(spots, labels):
            text(s, X(lx) - 55, ly - 10, 110, 22, Frame(anchor="t", inset=1), [lab], font=font, size=11, bold=True,
                 color=ink, align="c")

    s = d.slide(bg="#eaf4f4", layout=BLANK, notes=None)
    freeform(s, cloud(560, 110, 110, 44, 9), filled("#ffffff"), closed=True)
    shape(s, MSO_SHAPE.RECTANGLE, 0, 330, W, 75, filled(sea))
    text(s, 40, 60, 460, 60, TOP, ["The water cycle"], font="Nunito", size=40, bold=True, color=deep)
    for k, lesson in enumerate(WATER.values()):
        text(s, 40 + (k % 2) * 230, 150 + (k // 2) * 60, 210, 44, TOP,
             [{"text": lesson.title, "rtl": lesson.rtl, "align": "r" if lesson.rtl else "l"}],
             font=lesson.font, size=26, color="#2a6f97")
    text(s, 40, 345, 640, 40, TOP, ["A four-language lesson for a mixed classroom"], font="Nunito", size=14,
         color="#ffffff")

    for lesson in WATER.values():
        s = d.slide(bg="#f7fbfc", layout=BLANK, notes=None)
        rtl = lesson.rtl
        al: Align = "r" if rtl else "l"
        text(s, 40 if not rtl else 360, 24, 320, 50, TOP, [{"text": lesson.title, "rtl": rtl, "align": al}],
             font=lesson.font, size=30, bold=True, color=deep)
        text(s, 40 if not rtl else 360, 90, 320, 260, TOP,
             [{"text": t, "number": True, "rtl": rtl, "align": al, "space_after": 14, "bullet_color": "#e76f51",
               "scheme": "arabicPeriod"} for t in lesson.steps], font=lesson.font, size=16, color=ink)
        labels = [t.split("：" if "：" in t else ":")[0] for t in lesson.steps]
        scene(s, 360 if not rtl else 40, labels, lesson.font, rtl)

    s = d.slide(bg="#f7fbfc", layout=BLANK,
                notes="Pairs quiz each other: one reads a word, the other points at the picture.")
    text(s, 40, 24, 640, 44, TOP, ["Glossary"], font="Nunito", size=30, bold=True, color=deep)
    glossary = [["English", "עברית", "العربية", "日本語", "中文"],
                ["evaporation", "אידוי", "التبخر", "蒸発", "蒸发"],
                ["condensation", "עיבוי", "التكاثف", "凝結", "凝结"],
                ["precipitation", "משקעים", "الهطول", "降水", "降水"],
                ["collection", "איסוף", "التجمع", "集水", "汇集"]]
    fonts = ["Nunito", "Noto Sans Hebrew", "Noto Sans Arabic", "Noto Sans JP", "Noto Sans SC"]
    rows: list[list[str | Para]] = [[{"text": w, "font": fonts[j], "rtl": j in (1, 2)} for j, w in enumerate(r)]
                                    for r in glossary]
    fills = [[deep] * 5] + [["#e3f2f7" if i % 2 else "#ffffff"] * 5 for i in range(1, 5)]
    colors = [["#ffffff"] * 5] + [[ink] * 5 for _ in range(4)]
    table(s, 40, 90, rows, [150, 120, 130, 120, 120], 42, fills=fills, colors=colors, bold_rows=(0,),
          aligns=["l", "r", "r", "c", "c"], border="#bcd9e3", border_w=0.75, size=15)
    return d


# --------------------------------------------------------------------------- F: art portfolio

def portfolio(pics: Pictures) -> Deck:
    d = Deck()
    night, paper, ink, rose = "#14121a", "#f4efe8", "#221f26", "#d17a6b"

    s = d.slide(bg=night, layout=BLANK, notes=None)
    picture(s, pics.dawn, 0, 0, W, H, crop=None)
    shape(s, MSO_SHAPE.RECTANGLE, 0, 250, W, 155, filled("#14121a"))
    text(s, 40, 250, 640, 70, TOP, ["Field Notes in Code"], font="Dancing Script", size=48, bold=True,
         color="#fbe3d6")
    text(s, 42, 322, 640, 50, TOP, [{"runs": [("Generative studies, 2024 – 2026", {}),
                                              ("   ·   ", {"color": rose}), ("a portfolio", {"italic": True})]}],
         font="Playfair Display", size=15, color="#e8dcd2")

    s = d.slide(bg=paper, layout=BLANK, notes=None)
    text(s, 40, 20, 640, 50, TOP, ["Four series"], font="Playfair Display", size=28, color=ink)
    works = [(pics.truchet, "Knots", "quarter circles, 2024"), (pics.flow, "Currents", "a field of angles, 2025"),
             (pics.rings, "Echoes", "concentric rings, 2025"), (pics.marble, "Tide", "summed sines, 2026")]
    for k, (pic, name, what) in enumerate(works):
        x = 40 + k * 163
        picture(s, pic, x, 80, 150, 150, crop=(1 / 6, 0, 1 / 6, 0))
        text(s, x, 238, 150, 26, TOP, [name], font="Dancing Script", size=20, bold=True, color=rose)
        text(s, x, 264, 150, 40, TOP, [what], font="Playfair Display", size=11, italic=True, color="#5a5360")
    shape(s, MSO_SHAPE.RECTANGLE, 40, 330, 640, 1.5, filled("#c9bfb4"))
    text(s, 40, 338, 640, 40, TOP, ["Every piece is a short Python program; the seed is part of the title."],
         font="Inter", size=11, color="#5a5360")

    s = d.slide(bg=paper, layout=BLANK, notes="Mention the plotter version: 3 hours of pen on paper.")
    picture(s, pics.flow, 40, 40, 330, 325, crop=(0.12, 0.0, 0.12, 0.0))
    text(s, 400, 60, 280, 280, TOP,
         [{"text": "Currents #11", "font": "Playfair Display", "size": 30},
          {"text": "2025 · pen plotter, 50 × 50 cm", "size": 12, "color": "#5a5360", "space_after": 16},
          {"text": "Seven hundred walkers follow one angle field. Where two colours meet, the "
                   "field turns faster than the pen can.", "size": 13, "line": 1.2, "space_after": 16},
          {"runs": [("Seed ", {}), ("11", {"font": "Roboto Mono", "color": rose}),
                    (" · steps ", {}), ("60", {"font": "Roboto Mono", "color": rose}),
                    (" · walkers ", {}), ("700", {"font": "Roboto Mono", "color": rose})], "size": 12}],
         font="Inter", color=ink)

    s = d.slide(bg=night, layout=BLANK, notes=None)
    text(s, 40, 26, 640, 50, TOP, ["From seed to print"], font="Playfair Display", size=28, color="#fbe3d6")
    steps = [("1", "Sketch", "a rule in ten lines of code"), ("2", "Search", "hundreds of seeds, a few kept"),
             ("3", "Tune", "palette, weight, margins"), ("4", "Print", "giclée or pen plotter")]
    for k, (n, head, what) in enumerate(steps):
        x = 60 + k * 160
        words(shape(s, MSO_SHAPE.OVAL, x, 120, 64, 64, outlined(None, rose, 2)), MIDDLE, [n],
              font="Playfair Display", size=24, color="#fbe3d6", align="c")
        if k < 3:
            arrow(s, x + 74, 152, x + 150, 152, color="#6d6475", w=1.25, head="stealth")
        text(s, x - 30, 200, 124, 30, TOP, [head], font="Dancing Script", size=22, bold=True, color=rose, align="c")
        text(s, x - 30, 232, 124, 50, TOP, [what], font="Inter", size=11, color="#d8ccc4", align="c")
    picture(s, pics.portrait1, 560, 300, 60, 60, crop=None)
    picture(s, pics.portrait2, 630, 300, 60, 60, crop=None)

    s = d.slide(bg=paper, layout=BLANK, notes=None)
    picture(s, pics.truchet, 420, 0, 300, 405, crop=(0.2, 0.0, 0.2, 0.0))
    text(s, 40, 120, 360, 80, TOP, ["Thank you"], font="Dancing Script", size=54, bold=True, color=rose)
    text(s, 42, 205, 340, 90, TOP, [{"text": "Prints and commissions", "size": 13, "bold": True},
                                    {"runs": [("hello@fieldnotes.example", {"link": "mailto:hello@fieldnotes.example",
                                                                            "underline": True})], "size": 13},
                                    {"text": "All works CC BY 4.0", "size": 11, "color": "#5a5360",
                                     "space_before": 8}],
         font="Inter", color=ink)
    return d


PPTX: dict[str, tuple[str, Callable[[Pictures], Deck]]] = {
    "talk": ("Shipping calm software", talk), "review": ("Harbor & Pine — Q3 review", review),
    "bees": ("How bees build hexagons", bees), "water": ("The water cycle in four languages", water),
    "portfolio": ("Field Notes in Code", portfolio)}


# ------------------------------------------------------------------ A: hashing, native Slides API

class TextLook(TypedDict, total=False):
    """What one `updateTextStyle` of `hashing` sets."""
    font: str
    size: float
    color: JsonObject
    bold: bool
    italic: bool


def hashing(slides: SlidesService, pid: str) -> None:
    """A lecture written with the Slides API alone: Google's layouts, placeholders and bullets."""
    from beamer2slides.google_types import object_id
    from beamer2slides.gslides import execute, pt
    pres = execute(slides.presentations().get(presentationId=pid))
    reqs: list[JsonObject] = [{"deleteObject": {"objectId": object_id(s)}} for s in pres.get("slides", [])]
    serif, sans, mono = "Merriweather", "Lato", "Roboto Mono"
    navy: JsonObject = {"red": 0.10, "green": 0.18, "blue": 0.32}
    teal: JsonObject = {"red": 0.0, "green": 0.47, "blue": 0.47}

    def colour(c: JsonObject) -> JsonObject:
        return {"opaqueColor": {"rgbColor": c}}

    def whole() -> JsonObject:
        return {"type": "ALL"}

    def span(a: int, b: int) -> JsonObject:
        return {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}

    def new(sid: str, layout: str, roles: Sequence[str]) -> None:
        mappings: list[Json] = [{"layoutPlaceholder": {"type": r, "index": 0}, "objectId": f"{sid}_{r.lower()}"}
                                for r in roles]
        reqs.append({"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": layout},
                                     "placeholderIdMappings": mappings}})

    def style(oid: str, rng: JsonObject, look: TextLook) -> None:
        st: JsonObject = {}
        f: list[str] = []
        font = look.get("font")
        if font:
            st["fontFamily"] = font
            f.append("fontFamily")
        size = look.get("size")
        if size:
            st["fontSize"] = pt(size)
            f.append("fontSize")
        color = look.get("color")
        if color:
            st["foregroundColor"] = colour(color)
            f.append("foregroundColor")
        bold = look.get("bold")
        if bold is not None:
            st["bold"] = bold
            f.append("bold")
        italic = look.get("italic")
        if italic is not None:
            st["italic"] = italic
            f.append("italic")
        reqs.append({"updateTextStyle": {"objectId": oid, "style": st, "fields": ",".join(f), "textRange": rng}})

    def put(oid: str, txt: str, *, font: str, size: float, color: JsonObject, bold: bool) -> None:
        reqs.append({"insertText": {"objectId": oid, "text": txt, "insertionIndex": 0}})
        style(oid, whole(), {"font": font, "size": size, "color": color, "bold": bold})

    def box(sid: str, oid: str, kind: str, x: float, y: float, w: float, h: float, *, fill: JsonObject,
            line: JsonObject) -> None:
        reqs.append({"createShape": {"objectId": oid, "shapeType": kind, "elementProperties": {
            "pageObjectId": sid, "size": {"width": pt(w), "height": pt(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"}}}})
        props: JsonObject = {"shapeBackgroundFill": {"solidFill": {"color": {"rgbColor": fill}}},
                             "outline": {"outlineFill": {"solidFill": {"color": {"rgbColor": line}}}, "weight": pt(1.0)}}
        reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props,
                                               "fields": "shapeBackgroundFill,outline"}})

    def arrow_line(sid: str, oid: str, x1: float, y1: float, x2: float, y2: float, color: JsonObject) -> None:
        """A 1.5 pt line from (x1, y1) to (x2, y2) ending in a filled arrow."""
        reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT", "elementProperties": {
            "pageObjectId": sid, "size": {"width": pt(abs(x2 - x1) or 0.01), "height": pt(abs(y2 - y1) or 0.01)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": min(x1, x2), "translateY": min(y1, y2),
                          "unit": "PT"}}}})
        lp: JsonObject = {"lineFill": {"solidFill": {"color": {"rgbColor": color}}}, "weight": pt(1.5),
                          "endArrow": "FILL_ARROW"}
        reqs.append({"updateLineProperties": {"objectId": oid, "lineProperties": lp,
                                              "fields": "lineFill,weight,endArrow"}})

    def bullets(oid: str, txt: str, *, preset: str, size: float) -> str:
        reqs.append({"insertText": {"objectId": oid, "text": txt}})
        style(oid, whole(), {"font": sans, "size": size, "color": {"red": 0.13, "green": 0.13, "blue": 0.13}})
        reqs.append({"createParagraphBullets": {"objectId": oid, "bulletPreset": preset,
                                                "textRange": {"type": "ALL"}}})
        return txt.replace("\t", "")

    def centre(oid: str) -> None:
        reqs.append({"updateParagraphStyle": {"objectId": oid, "style": {"alignment": "CENTER"},
                                              "fields": "alignment", "textRange": {"type": "ALL"}}})

    def text_box(sid: str, oid: str, x: float, y: float, w: float, h: float) -> None:
        reqs.append({"createShape": {"objectId": oid, "shapeType": "TEXT_BOX", "elementProperties": {
            "pageObjectId": sid, "size": {"width": pt(w), "height": pt(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"}}}})

    disc, numbered = "BULLET_DISC_CIRCLE_SQUARE", "NUMBERED_DIGIT_ALPHA_ROMAN"

    # 1. title
    new("hash01", "TITLE", ["CENTERED_TITLE", "SUBTITLE"])
    put("hash01_centered_title", "Hash tables", font=serif, size=44, color=navy, bold=True)
    put("hash01_subtitle", "Lecture 7 · Data structures, fall term", font=sans, size=18, color=teal, bold=False)

    # 2. nested bullets with inline code and math
    new("hash02", "TITLE_AND_BODY", ["TITLE", "BODY"])
    put("hash02_title", "Why hashing?", font=serif, size=28, color=navy, bold=True)
    t = bullets("hash02_body", "Arrays give O(1) access by index\n\tbut keys are rarely small integers\n"
                               "A hash function turns any key into an index\n\th(k) = k mod m for integer keys\n"
                               "\tPython calls it hash(key)\nCollisions are unavoidable: more keys than slots",
                preset=disc, size=16)
    looks: list[tuple[str, TextLook]] = [("O(1)", {"italic": True}), ("h(k) = k mod m", {"font": mono}),
                                         ("hash(key)", {"font": mono}), ("unavoidable", {"bold": True})]
    for frag, look in looks:
        a = t.index(frag)
        style("hash02_body", span(a, a + len(frag)), look)

    # 3. diagram: separate chaining
    new("hash03", "TITLE_ONLY", ["TITLE"])
    put("hash03_title", "Separate chaining", font=serif, size=28, color=navy, bold=True)
    chains: dict[int, list[str]] = {0: [], 1: ["ada", "kai"], 2: [], 3: ["lin"], 4: ["moe", "ian", "zoe"]}
    grey: JsonObject = {"red": 0.93, "green": 0.95, "blue": 0.96}
    for b, keys in chains.items():
        y = 110 + b * 50
        box("hash03", f"hash03_b{b}", "RECTANGLE", 50, y, 44, 36, fill=grey, line=navy)
        put(f"hash03_b{b}", str(b), font=mono, size=14, color=navy, bold=False)
        centre(f"hash03_b{b}")
        x = 94
        for k, key in enumerate(keys):
            arrow_line("hash03", f"hash03_a{b}{k}", x, y + 18, x + 30, y + 18, navy)
            box("hash03", f"hash03_k{b}{k}", "ROUND_RECTANGLE", x + 30, y + 2, 60, 32,
                fill={"red": 0.85, "green": 0.94, "blue": 0.94}, line=teal)
            put(f"hash03_k{b}{k}", key, font=mono, size=13, color=navy, bold=False)
            centre(f"hash03_k{b}{k}")
            x += 90
    text_box("hash03", "hash03_note", 420, 110, 260, 200)
    t = bullets("hash03_note", "h(k) = position of k in the alphabet mod 5\nEach bucket holds a list\n"
                               "A lookup walks one list\nExpected length: α = n / m", preset=disc, size=15)
    looks = [("h(k)", {"font": mono}), ("α = n / m", {"italic": True, "bold": True})]
    for frag, look in looks:
        a = t.index(frag)
        style("hash03_note", span(a, a + len(frag)), look)

    # 4. code
    new("hash04", "TITLE_ONLY", ["TITLE"])
    put("hash04_title", "Insert, in code", font=serif, size=28, color=navy, bold=True)
    box("hash04", "hash04_code", "RECTANGLE", 40, 100, 380, 200, fill={"red": 0.965, "green": 0.965, "blue": 0.95},
        line={"red": 0.85, "green": 0.85, "blue": 0.82})
    code = ("def insert(table, key, value):\n    i = hash(key) % len(table)\n    for pair in table[i]:\n"
            "        if pair[0] == key:\n            pair[1] = value\n            return\n"
            "    table[i].append([key, value])")
    put("hash04_code", code, font=mono, size=12, color={"red": 0.15, "green": 0.15, "blue": 0.2}, bold=False)
    reqs.append({"updateParagraphStyle": {"objectId": "hash04_code", "style": {"alignment": "START"},
                                          "fields": "alignment", "textRange": {"type": "ALL"}}})
    reqs.append({"updateShapeProperties": {"objectId": "hash04_code", "shapeProperties": {"contentAlignment": "TOP"},
                                           "fields": "contentAlignment"}})
    for kw_ in ("def", "for", "if", "return", "in"):
        start = 0
        while True:
            a = code.find(kw_ + " ", start)
            if a < 0:
                break
            if a == 0 or not code[a - 1].isalnum():
                style("hash04_code", span(a, a + len(kw_)), {"color": {"red": 0.55, "green": 0.1, "blue": 0.45},
                                                             "bold": True})
            start = a + 1
    text_box("hash04", "hash04_side", 440, 100, 250, 200)
    bullets("hash04_side", "Average cost: O(1 + α)\nWorst case: O(n), every key in one bucket\n"
                           "Resize when α passes 0.75\n\tdouble m, re-insert every key", preset=disc, size=15)

    # 5. table
    new("hash05", "TITLE_ONLY", ["TITLE"])
    put("hash05_title", "Load factor vs. expected probes", font=serif, size=26, color=navy, bold=True)
    data = [["α", "Chaining", "Linear probing"], ["0.25", "1.13", "1.17"], ["0.50", "1.25", "1.50"],
            ["0.75", "1.38", "2.50"], ["0.90", "1.45", "5.50"]]
    reqs.append({"createTable": {"objectId": "hash05_t", "rows": 5, "columns": 3, "elementProperties": {
        "pageObjectId": "hash05", "size": {"width": pt(420), "height": pt(200)},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 40, "translateY": 100, "unit": "PT"}}}})
    white: JsonObject = {"red": 1, "green": 1, "blue": 1}
    for i, row in enumerate(data):
        for j, val in enumerate(row):
            loc: JsonObject = {"rowIndex": i, "columnIndex": j}
            reqs.append({"insertText": {"objectId": "hash05_t", "cellLocation": loc, "text": val}})
            reqs.append({"updateTextStyle": {"objectId": "hash05_t", "cellLocation": loc, "textRange": {"type": "ALL"},
                                             "style": {"fontFamily": sans, "fontSize": pt(15), "bold": i == 0,
                                                       "foregroundColor": colour(white if i == 0 else navy)},
                                             "fields": "fontFamily,fontSize,bold,foregroundColor"}})
            if j:
                reqs.append({"updateParagraphStyle": {"objectId": "hash05_t", "cellLocation": loc,
                                                      "textRange": {"type": "ALL"}, "style": {"alignment": "END"},
                                                      "fields": "alignment"}})
    reqs.append({"updateTableCellProperties": {"objectId": "hash05_t", "tableRange": {
        "location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": 1, "columnSpan": 3},
        "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {"color": {"rgbColor": navy}}}},
        "fields": "tableCellBackgroundFill"}})
    reqs.append({"updateTableCellProperties": {"objectId": "hash05_t", "tableRange": {
        "location": {"rowIndex": 4, "columnIndex": 2}, "rowSpan": 1, "columnSpan": 1},
        "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {"color": {"rgbColor": {
            "red": 1.0, "green": 0.87, "blue": 0.8}}}}}, "fields": "tableCellBackgroundFill"}})
    text_box("hash05", "hash05_side", 480, 100, 210, 200)
    put("hash05_side", "Successful searches, uniform hashing. Probing degrades fast past α = 0.75: "
                       "at 0.9 a lookup reads five and a half slots on average.", font=sans, size=14,
        color={"red": 0.3, "green": 0.3, "blue": 0.3}, bold=False)

    # 6. numbered list
    new("hash06", "TITLE_AND_BODY", ["TITLE", "BODY"])
    put("hash06_title", "Choosing m", font=serif, size=28, color=navy, bold=True)
    t = bullets("hash06_body", "Make m prime when h(k) = k mod m\n\tpowers of two keep only the low bits\n"
                               "Keep α below 0.75 for open addressing\nGrow by doubling, so inserts stay O(1) amortised\n"
                               "Test with your real keys, not random ones", preset=numbered, size=16)
    for frag in ("h(k) = k mod m",):
        a = t.index(frag)
        style("hash06_body", span(a, a + len(frag)), {"font": mono})
    reqs.append({"updatePageProperties": {"objectId": "hash01", "pageProperties": {"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 0.93, "green": 0.96, "blue": 0.96}}}}},
        "fields": "pageBackgroundFill.solidFill.color"}})
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))


# -------------------------------------------------------------------------------------- build

TITLES = {"hashing": "Hash tables — lecture 7", **{k: v[0] for k, v in PPTX.items()}}


def write_manifest(manifest: JsonObject) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def build(names: Sequence[str]) -> JsonObject:
    """Build (or rebuild in place) the decks `names` (all when empty), share each view-only and
    record it in decks.json; the manifest as written."""
    from beamer2slides.drive_folder import place
    from beamer2slides.emit import import_presentation
    from beamer2slides.google_auth import drive_service, slides_service
    from beamer2slides.google_types import file_id, object_id
    from beamer2slides.gslides import execute
    slides, drive = slides_service(None), drive_service(None)
    where = str(MANIFEST)
    manifest: JsonObject = (as_object(json.loads(MANIFEST.read_text(encoding="utf-8")), where)
                            if MANIFEST.exists() else {})
    pics = pictures()
    for name in names or list(TITLES):
        entry = manifest.get(name)
        pid = None if entry is None else as_optional_str(as_object(entry, f"{where}: {name}").get("id"),
                                                         f"{where}: {name}.id")
        if name == "hashing":
            if not pid:
                pid = file_id(execute(drive.files().create(body=place({"name": TITLES[name], "mimeType":
                                                                       "application/vnd.google-apps.presentation"},
                                                                      drive, None), fields="id")), TITLES[name])
                manifest[name] = {"id": pid}
                write_manifest(manifest)
            hashing(slides, pid)
        else:
            title, make = PPTX[name]
            pid = as_str(import_presentation(slides, drive, title, W, H, make(pics).pptx(), pid)["presentationId"],
                         f"{name}'s id")
        execute(drive.permissions().create(fileId=pid, body={"type": "anyone", "role": "reader"}, fields="id"))
        pres = execute(slides.presentations().get(presentationId=pid, fields="title,slides.objectId"))
        pages: list[Json] = [object_id(s) for s in pres.get("slides", [])]
        manifest[name] = {"id": pid, "title": TITLES[name], "pages": pages}
        write_manifest(manifest)
        print(f"{name}: {len(pages)} slides  https://docs.google.com/presentation/d/{pid}/view")
    return manifest
