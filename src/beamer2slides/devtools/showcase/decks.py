"""The showcase decks: six small decks written here, every word and picture ours, so the public
gallery shows adopt on decks we may publish.

Deck `hashing` is built natively through the Slides API (Google's own layouts and bullets); the
others are .pptx files imported by Drive, the way most decks people share were made. Each deck is
shared "anyone with the link can view" so the gallery links to the original.

  python tools/showcase.py decks [NAME...]   build (or rebuild in place), share, record decks.json
"""

import io
import json
import math
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.shapes.autoshape import Shape
from pptx.util import Pt

from beamer2slides.devtools.showcase import art
from beamer2slides.paths import CHECKOUT

W, H = 720, 405
MANIFEST = Path(__file__).with_name("decks.json")
ART = CHECKOUT / "out" / "showcase" / "art"
CORPUS = CHECKOUT / "out" / "showcase-corpus"      # adopt_bench's corpus for these decks only


def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h.lstrip("#"))


# --------------------------------------------------------------------------------------- .pptx kit

class Deck:
    def __init__(self):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Pt(W), Pt(H)
        # the default template is 4:3 at the same width: bring placeholders with a box of their own
        # to 16:9 (only those; inherited ones follow)
        for owner in [self.prs.slide_master, *self.prs.slide_layouts]:
            for ph in owner.placeholders:
                if ph._element.xpath("./p:spPr/a:xfrm"):
                    ph.top, ph.height = int(ph.top * 0.75), int(ph.height * 0.75)

    def slide(self, bg: str | None = None, layout: int = 6, notes: str | None = None):
        s = self.prs.slides.add_slide(self.prs.slide_layouts[layout])
        if bg:
            s.background.fill.solid()
            s.background.fill.fore_color.rgb = rgb(bg)
        if notes:
            s.notes_slide.notes_text_frame.text = notes
        return s

    def pptx(self) -> io.BytesIO:
        buf = io.BytesIO()
        self.prs.save(buf)
        buf.seek(0)
        return buf


def set_font(run, name: str) -> None:
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


def fill_text(tf, paras, font="Inter", size=16, color="#222222", bold=False, italic=False, align="l",
              rtl=False, space_after=None, line=None):
    """Paragraphs: a str, or {"runs": [str | (str, {font, size, color, bold, italic, underline,
    link, sup})], "bullet": "•", "number": True, "level": 0, ...overrides}."""
    for i, para in enumerate(paras):
        spec = {"runs": [para]} if isinstance(para, str) else dict(para)
        if "text" in spec:
            spec["runs"] = [spec.pop("text")]
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        al = spec.get("align", align)
        p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT, "j": PP_ALIGN.JUSTIFY}[al]
        sa = spec.get("space_after", space_after)
        if sa is not None:
            p.space_after = Pt(sa)
        if spec.get("space_before") is not None:
            p.space_before = Pt(spec["space_before"])
        ln = spec.get("line", line)
        if ln is not None:
            p.line_spacing = ln
        ppr = p._p.get_or_add_pPr()
        if spec.get("rtl", rtl):
            ppr.set("rtl", "1")
        level = spec.get("level", 0)
        psize = spec.get("size", size)
        if spec.get("bullet") or spec.get("number"):
            indent = spec.get("indent", psize * 1.1)
            mar = Pt(indent * (level + 1))
            ppr.set("marL", str(int(mar)))
            ppr.set("indent", str(-int(Pt(indent))))
            if level:
                ppr.set("lvl", str(level))
            if spec.get("bullet_color"):
                clr = etree.SubElement(ppr, qn("a:buClr"))
                etree.SubElement(clr, qn("a:srgbClr")).set("val", spec["bullet_color"].lstrip("#"))
            if spec.get("number"):
                num = etree.SubElement(ppr, qn("a:buAutoNum"))
                num.set("type", spec.get("scheme", "arabicPeriod"))
            else:
                if spec.get("bullet_font"):
                    etree.SubElement(ppr, qn("a:buFont")).set("typeface", spec["bullet_font"])
                etree.SubElement(ppr, qn("a:buChar")).set("char", spec["bullet"])
        for r in spec["runs"]:
            txt, o = (r, {}) if isinstance(r, str) else r
            run = p.add_run()
            run.text = txt
            set_font(run, o.get("font", spec.get("font", font)))
            run.font.size = Pt(o.get("size", psize))
            run.font.bold = o.get("bold", spec.get("bold", bold))
            run.font.italic = o.get("italic", spec.get("italic", italic))
            if o.get("underline"):
                run.font.underline = True
            run.font.color.rgb = rgb(o.get("color", spec.get("color", color)))
            if o.get("sup"):
                run._r.get_or_add_rPr().set("baseline", "30000")
            if o.get("spacing") is not None:
                run._r.get_or_add_rPr().set("spc", str(int(o["spacing"] * 100)))
            if o.get("link"):
                run.hyperlink.address = o["link"]


def _frame(tf, anchor="t", inset=None, wrap=True):
    tf.word_wrap = wrap
    tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
    if inset is not None:
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Pt(inset)


def text(slide, x, y, w, h, paras, anchor="t", inset=None, wrap=True, **kw):
    tb = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
    _frame(tb.text_frame, anchor, inset, wrap)
    fill_text(tb.text_frame, paras, **kw)
    return tb


def no_shadow(shape) -> None:
    sppr = shape._element.spPr
    if sppr.find(qn("a:effectLst")) is None:
        etree.SubElement(sppr, qn("a:effectLst"))


def _look(s, fill, line, line_w, dash=None):
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = rgb(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(line_w)
        if dash:
            s.line.dash_style = dash
    else:
        s.line.fill.background()
    no_shadow(s)


def shape(slide, kind, x, y, w, h, fill=None, line=None, line_w=1.0, paras=None, adj=None, rot=0.0,
          anchor="m", inset=None, dash=None, **kw):
    s = slide.shapes.add_shape(kind, Pt(x), Pt(y), Pt(w), Pt(h))
    _look(s, fill, line, line_w, dash)
    for i, v in enumerate(adj or []):
        s.adjustments[i] = v
    if rot:
        s.rotation = rot
    if paras:
        _frame(s.text_frame, anchor, inset)
        fill_text(s.text_frame, paras, **{"align": "c", **kw})
    return s


def arrow(slide, x1, y1, x2, y2, color="#333333", w=1.5, head="triangle", tail=None, dash=None):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(x1), Pt(y1), Pt(x2), Pt(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(w)
    if dash:
        c.line.dash_style = dash
    ln = c.line._get_or_add_ln()
    if tail:
        etree.SubElement(ln, qn("a:headEnd")).set("type", tail)
    if head:
        etree.SubElement(ln, qn("a:tailEnd")).set("type", head)
    return c


def freeform(slide, pts, fill=None, line=None, line_w=1.0, closed=True):
    fb = slide.shapes.build_freeform(Pt(pts[0][0]), Pt(pts[0][1]), scale=1.0)
    fb.add_line_segments([(Pt(x), Pt(y)) for x, y in pts[1:]], close=closed)
    s = fb.convert_to_shape()
    _look(s, fill, line, line_w)
    return s


def blob(cx, cy, rx, ry, bumps, depth=0.12, n=96, phase=0.0):
    """A cloud-like closed outline: an ellipse with `bumps` scallops."""
    out = []
    for k in range(n):
        t = 2 * math.pi * k / n
        r = 1 + depth * abs(math.sin(bumps * t / 2 + phase))
        out.append((cx + rx * r * math.cos(t), cy + ry * r * math.sin(t)))
    return out


def star(cx, cy, r_out, r_in, points=5, rot=-90):
    out = []
    for k in range(points * 2):
        r = r_out if k % 2 == 0 else r_in
        a = math.radians(rot + 180 * k / points)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def picture(slide, path, x, y, w=None, h=None, crop=None):
    p = slide.shapes.add_picture(str(path), Pt(x), Pt(y), Pt(w) if w else None, Pt(h) if h else None)
    if crop:
        p.crop_left, p.crop_top, p.crop_right, p.crop_bottom = crop
    return p


def _border(cell, color, w, sides="LRTB"):
    tcpr = cell._tc.get_or_add_tcPr()
    for i, side in enumerate("LRTB"):
        old = tcpr.find(qn(f"a:ln{side}"))
        if old is not None:
            tcpr.remove(old)
    at = 0
    for side in "LRTB":
        ln = etree.Element(qn(f"a:ln{side}"))
        if side in sides:
            ln.set("w", str(int(Pt(w))))
            sf = etree.SubElement(ln, qn("a:solidFill"))
            etree.SubElement(sf, qn("a:srgbClr")).set("val", color.lstrip("#"))
        else:
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        tcpr.insert(at, ln)
        at += 1


def table(slide, x, y, rows, col_w, row_h, fills=None, colors=None, bold_rows=(0,), aligns=None,
          border="#d0d0d0", border_w=0.75, merges=(), **kw):
    """`rows`: lists of cell strings (or paragraph specs); `fills[i][j]`/`colors[i][j]`: per cell."""
    nr, nc = len(rows), len(col_w)
    gf = slide.shapes.add_table(nr, nc, Pt(x), Pt(y), Pt(sum(col_w)), Pt(row_h * nr))
    t = gf.table
    tblpr = gf._element.graphic.graphicData.tbl.tblPr
    for flag in ("firstRow", "bandRow"):
        tblpr.set(flag, "0")
    for j, cw in enumerate(col_w):
        t.columns[j].width = Pt(cw)
    for i in range(nr):
        t.rows[i].height = Pt(row_h)
    for (r0, c0), (r1, c1) in merges:
        t.cell(r0, c0).merge(t.cell(r1, c1))
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            cell = t.cell(i, j)
            if cell.is_spanned:
                continue
            f = fills[i][j] if fills else None
            if f:
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(f)
            else:
                cell.fill.background()
            cell.margin_left = cell.margin_right = Pt(6)
            cell.margin_top = cell.margin_bottom = Pt(3)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            col = colors[i][j] if colors else kw.get("color", "#222222")
            al = aligns[j] if aligns else "l"
            spec = val if isinstance(val, dict) else {"text": val}
            fill_text(cell.text_frame, [spec], **{**kw, "color": col, "align": al, "bold": i in bold_rows})
            if border:
                _border(cell, border, border_w)
    return gf


def decorate(owner, kind: str, x, y, w, h, fill=None, line=None, line_w=1.0, rot=0.0):
    """A shape on a master or layout (python-pptx only adds them to slides)."""
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    s = Shape(tree.add_autoshape(sid, f"Decoration {sid}", kind, Pt(x), Pt(y), Pt(w), Pt(h)), owner.shapes)
    _look(s, fill, line, line_w)
    if rot:
        s.rotation = rot
    return s


def decorate_picture(owner, path, x, y, w, h):
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    _, rid = owner.part.get_or_add_image_part(str(path))
    tree.add_pic(sid, f"Decoration {sid}", "", rid, Pt(x), Pt(y), Pt(w), Pt(h))


def decorate_text(owner, x, y, w, h, paras, **kw):
    tree = owner.shapes._spTree
    sid = owner.shapes._next_shape_id
    sp = tree.add_textbox(sid, f"Decoration {sid}", Pt(x), Pt(y), Pt(w), Pt(h))
    s = Shape(sp, owner.shapes)
    _frame(s.text_frame, "m", 0)
    fill_text(s.text_frame, paras, **kw)


def placeholder_text(ph, paras, box=None, **kw):
    if box:
        ph.left, ph.top, ph.width, ph.height = (Pt(v) for v in box)
    tf = ph.text_frame
    tf.clear()
    fill_text(tf, paras, **kw)


def pictures() -> dict[str, Path]:
    ART.mkdir(parents=True, exist_ok=True)
    made = {
        "truchet": art.truchet(ART / "truchet.png", 7),
        "flow": art.flow(ART / "flow.png", 11),
        "rings": art.rings(ART / "rings.png", 5),
        "marble": art.marble(ART / "marble.png", 3),
        "dusk": art.landscape(ART / "dusk.png", 21),
        "dawn": art.landscape(ART / "dawn.png", 4, sky=("#355070", "#eaac8b"),
                              layers=("#6d597a", "#56445d", "#3d3047", "#241c2b")),
        "rings_title": art.rings(ART / "rings_title.png", 9, size=(600, 900), bg="#16213e",
                                 colours=("#e94560", "#0f3460", "#f5b971", "#53354a")),
        "portrait1": art.portrait(ART / "portrait1.png", 1),
        "portrait2": art.portrait(ART / "portrait2.png", 2, bg="#cdb4db",
                                  colours=("#ffc8dd", "#bde0fe", "#a2d2ff", "#ffafcc")),
        "pie": art.pie(ART / "pie.png", [("Espresso drinks", 46, "#6f4e37"), ("Filter coffee", 21, "#c69c6d"),
                                         ("Pastries", 19, "#e9c46a"), ("Beans to go", 14, "#2a9d8f")]),
    }
    for name, share, col in (("ring_rev", 0.81, "#2a9d8f"), ("ring_loyal", 0.64, "#e9c46a"),
                             ("ring_stores", 0.92, "#8ab17d"), ("ring_ticket", 0.47, "#e76f51")):
        made[name] = art.ring_chart(ART / f"{name}.png", share, colour=col, track="#e8e2d6")
    return made


# ------------------------------------------------------------------------------ B: conference talk

def talk(pics) -> Deck:
    d = Deck()
    navy, coral, sand, ink = "#16213e", "#e94560", "#f5b971", "#1f2433"
    master = d.prs.slide_master
    decorate(master, "rect", 0, 389, W, 16, fill=navy)
    decorate(master, "rect", 0, 389, 150, 16, fill=coral)
    decorate(master, "ellipse", 684, 18, 14, 14, fill=sand)
    decorate_text(master, 470, 389, 236, 16, [{"text": "Lumen Summit 2026 · Lisbon", "align": "r"}],
                  font="Poppins", size=8, color="#ffffff")
    title_layout = d.prs.slide_layouts[0]
    title_layout.background.fill.solid()
    title_layout.background.fill.fore_color.rgb = rgb(navy)
    decorate_picture(title_layout, pics["rings_title"], 480, 0, 240, 360)
    decorate(title_layout, "rect", 40, 250, 60, 5, fill=coral)

    s = d.slide(layout=0, notes="Open with the pager story: three alerts on a Friday deploy.")
    placeholder_text(s.shapes.title, ["Shipping calm software"], box=(40, 110, 420, 110),
                     font="Poppins", size=40, bold=True, color="#ffffff", line=0.95)
    placeholder_text(s.placeholders[1], [{"text": "Small batches, feature flags and the numbers that told us to slow down",
                                          "size": 15}, {"text": "A. Rivera · platform team", "size": 12, "color": sand,
                                                        "space_before": 10}],
                     box=(40, 268, 400, 80), font="Inter", color="#e6e8ef")

    s = d.slide(layout=1)
    placeholder_text(s.shapes.title, ["Agenda"], box=(40, 30, 640, 50), font="Poppins", size=28, bold=True,
                     color=navy)
    items = [("Why deploys feel scary", "a short history of our worst Friday"),
             ("Small batches", "one idea per pull request"),
             ("Feature flags", "ship dark, turn on slowly"),
             ("What we measure now", "and what we stopped measuring")]
    paras = []
    for head, sub in items:
        paras.append({"runs": [(head, {"bold": True}), (" — " + sub, {"color": "#5c6378"})], "number": True,
                      "size": 18, "space_after": 12, "bullet_color": coral})
    placeholder_text(s.placeholders[1], paras, box=(48, 100, 600, 250), font="Inter", color=ink)

    s = d.slide(layout=5)
    placeholder_text(s.shapes.title, ["Three numbers that changed our mind"], box=(40, 30, 640, 50),
                     font="Poppins", size=26, bold=True, color=navy)
    stats = [("14 min", "median time from merge to production", coral),
             ("92%", "of deploys paged nobody", "#0f3460"),
             ("3×", "more releases per week than last year", "#53354a")]
    for k, (big, small, col) in enumerate(stats):
        x = 40 + k * 218
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 110, 202, 200, fill="#f4f5f9", adj=[0.08])
        shape(s, MSO_SHAPE.RECTANGLE, x, 110, 202, 6, fill=col)
        text(s, x + 16, 135, 170, 70, [big], font="Poppins", size=44, bold=True, color=col)
        text(s, x + 16, 215, 170, 80, [small], font="Inter", size=14, color=ink)
    text(s, 40, 330, 640, 30, [{"runs": [("Source: our deploy log, January to August. ", {}),
                                         ("How we count", {"link": "https://example.com/deploy-metrics",
                                                           "color": "#0f3460", "underline": True})]}],
         font="Inter", size=10, color="#8a8fa3")

    s = d.slide(layout=5)
    placeholder_text(s.shapes.title, ["A week of small batches"], box=(40, 30, 640, 50), font="Poppins",
                     size=26, bold=True, color=navy)
    arrow(s, 60, 200, 670, 200, color="#c3c7d4", w=3, head="triangle")
    days = [("Mon", "Design review", "one page, one owner"), ("Tue", "Flag off", "code merged dark"),
            ("Wed", "5% of users", "watch the error budget"), ("Thu", "50%", "compare cohorts"),
            ("Fri", "100%", "delete the flag")]
    for k, (day, what, why) in enumerate(days):
        cx = 90 + k * 132
        col = coral if k == 4 else "#0f3460"
        shape(s, MSO_SHAPE.OVAL, cx - 22, 178, 44, 44, fill=col, paras=[day], font="Poppins", size=11,
              bold=True, color="#ffffff", inset=0)
        y = 110 if k % 2 == 0 else 238
        text(s, cx - 60, y, 120, 56, [{"text": what, "bold": True, "size": 13},
                                     {"text": why, "size": 11, "color": "#5c6378"}],
             font="Inter", color=ink, align="c", anchor="b" if k % 2 == 0 else "t")

    s = d.slide(layout=6, notes="Close on the quote, then the link.")
    picture(s, pics["dusk"], 0, 0, 330, 389, crop=(0.25, 0.0, 0.25, 0.0))
    text(s, 370, 90, 310, 150, [{"text": "“The calmest deploy is the one nobody noticed.”", "size": 26,
                                 "font": "Lora", "italic": True, "line": 1.05},
                                {"text": "— the on-call rota, mid-August", "size": 13, "color": "#5c6378",
                                 "space_before": 14}], font="Inter", color=ink)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 370, 280, 190, 40, fill=coral, adj=[0.5],
          paras=["Slides + notes: lumen.example/calm"], font="Inter", size=11, bold=True, color="#ffffff")
    return d


# ---------------------------------------------------------------------------- C: quarterly review

def review(pics) -> Deck:
    d = Deck()
    brown, cream, ink, green, red = "#3e2c23", "#faf6ef", "#2b2521", "#2a7f62", "#c0392b"

    s = d.slide(bg=cream)
    picture(s, pics["marble"], 430, 0, 290, 405, crop=(0.2, 0.0, 0.2, 0.0))
    shape(s, MSO_SHAPE.RECTANGLE, 40, 120, 8, 120, fill=brown)
    text(s, 60, 110, 360, 140, [{"text": "Harbor & Pine Coffee", "size": 16, "color": "#8c6b4f", "bold": True},
                                {"text": "Q3 business review", "size": 36, "font": "Playfair Display",
                                 "bold": True},
                                {"text": "July – September · prepared for the board", "size": 13,
                                 "color": "#6b5d52", "space_before": 6}], font="Montserrat", color=ink)

    s = d.slide(bg=cream, notes="Revenue is ahead of plan; ticket size slipped with the summer menu.")
    text(s, 40, 26, 640, 40, ["Q3 at a glance"], font="Playfair Display", size=26, bold=True, color=ink)
    kpis = [("Revenue", "$1.42M", "+8.1% vs Q2", green, "ring_rev", "81% of annual plan"),
            ("Loyalty members", "18.4k", "+23% vs Q2", green, "ring_loyal", "64% of target"),
            ("Stores open", "12", "+2 this quarter", green, "ring_stores", "92% of sites staffed"),
            ("Average ticket", "$7.85", "−1.2% vs Q2", red, "ring_ticket", "47% add a pastry")]
    for k, (label, value, delta, col, ring, note) in enumerate(kpis):
        x = 40 + k * 163
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 86, 150, 270, fill="#ffffff", line="#e6dccd", adj=[0.06])
        text(s, x + 12, 96, 126, 20, [label.upper()], font="Montserrat", size=9, bold=True, color="#8c6b4f")
        text(s, x + 12, 116, 126, 44, [value], font="Montserrat", size=28, bold=True, color=ink)
        text(s, x + 12, 160, 126, 20, [delta], font="Montserrat", size=11, bold=True, color=col)
        picture(s, pics[ring], x + 35, 196, 80, 80)
        text(s, x + 8, 290, 134, 50, [note], font="Montserrat", size=10, color="#6b5d52", align="c")

    s = d.slide(bg=cream)
    text(s, 40, 26, 640, 40, ["Sales by store, in $k"], font="Playfair Display", size=26, bold=True, color=ink)
    stores = [("Harbor St", 52, 58, 61), ("Pine Ave", 44, 47, 45), ("Old Mill", 31, 36, 41),
              ("Station", 63, 60, 66), ("Riverside", 22, 28, 35)]
    lo, hi = 20, 70

    def heat(v):
        t = (v - lo) / (hi - lo)
        a, b = (0xf6, 0xe7, 0xd4), (0x8c, 0x4a, 0x2f)
        return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(a, b))

    rows = [["Store", "Jul", "Aug", "Sep", "Q3"]]
    fills = [[brown] * 5]
    colors = [["#ffffff"] * 5]
    for name, *vals in stores:
        rows.append([name] + [str(v) for v in vals] + [str(sum(vals))])
        fills.append(["#ffffff"] + [heat(v) for v in vals] + ["#efe7da"])
        colors.append([ink] + ["#ffffff" if v > 48 else ink for v in vals] + [ink])
    rows.append(["All stores"] + [str(sum(r[i] for r in stores)) for i in (1, 2, 3)]
                + [str(sum(sum(r[1:]) for r in stores))])
    fills.append(["#efe7da"] * 5)
    colors.append([ink] * 5)
    table(s, 40, 84, rows, [150, 80, 80, 80, 90], 34, fills=fills, colors=colors, bold_rows=(0, 6),
          aligns="lrrrr", font="Montserrat", size=13, border="#ffffff", border_w=1.5)
    text(s, 540, 90, 150, 230, [{"text": "Reading the grid", "bold": True, "size": 13, "space_after": 6},
                                {"text": "Darker cells sold more.", "bullet": "•", "size": 11, "space_after": 4},
                                {"text": "Riverside grew 59% since July.", "bullet": "•", "size": 11,
                                 "space_after": 4},
                                {"text": "Pine Ave is flat: roadworks until October.", "bullet": "•", "size": 11}],
         font="Montserrat", color=ink)

    s = d.slide(bg=cream)
    text(s, 40, 26, 640, 40, ["What people buy"], font="Playfair Display", size=26, bold=True, color=ink)
    picture(s, pics["pie"], 30, 80, 420, 280)
    shape(s, MSO_SHAPE.RECTANGLE, 470, 90, 220, 260, fill="#efe7da")
    text(s, 486, 100, 196, 240, [{"text": "Takeaways", "bold": True, "size": 15, "space_after": 8},
                                 {"text": "Espresso drinks are almost half of sales.", "bullet": "–",
                                  "space_after": 6},
                                 {"text": "Beans to go doubled after the subscription launch.", "bullet": "–",
                                  "space_after": 6},
                                 {"text": "Pastries carry the best margin: 68%.", "bullet": "–"}],
         font="Montserrat", size=12, color=ink)

    s = d.slide(bg=brown)
    text(s, 40, 30, 640, 40, ["Next quarter"], font="Playfair Display", size=26, bold=True, color="#ffffff")
    plan = [("✓", "Winter menu tasting, October 4"), ("✓", "Hire two shift leads for Station"),
            ("☐", "Open the Riverside patio heaters"), ("☐", "Loyalty app: pre-order at the counter"),
            ("☐", "Renegotiate the milk contract")]
    text(s, 40, 90, 380, 260, [{"text": t, "bullet": b, "bullet_font": "Segoe UI Symbol", "space_after": 10,
                                "bullet_color": "#e9c46a"} for b, t in plan],
         font="Montserrat", size=16, color="#fdf6ec")
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 450, 100, 230, 190, fill="#fdf6ec", adj=[0.1], anchor="t", inset=14,
          paras=[{"text": "Target for Q4", "bold": True, "size": 13, "color": "#8c6b4f"},
                 {"text": "$1.55M", "size": 40, "bold": True, "font": "Playfair Display"},
                 {"text": "revenue, with ticket size back above $8.00", "size": 12}],
          font="Montserrat", color=ink, align="l")
    return d


# -------------------------------------------------------------------------------- D: bees, for kids

def bees(pics) -> Deck:
    d = Deck()
    sky, honey, dark, leaf = "#fff4d6", "#f6a623", "#3b2a14", "#7cb342"
    font = "Fredoka"

    def hexagon(s, cx, cy, r, fill, line="#ffffff"):
        return shape(s, MSO_SHAPE.HEXAGON, cx - r, cy - r * 0.866, 2 * r, 2 * r * 0.866, fill=fill, line=line,
                     line_w=3, adj=[0.25, 1.1547])

    def bee(s, x, y, k=1.0):
        shape(s, MSO_SHAPE.OVAL, x + 8 * k, y - 16 * k, 26 * k, 22 * k, fill="#dff3ff", line="#9fd3ef", rot=-20)
        shape(s, MSO_SHAPE.OVAL, x + 24 * k, y - 18 * k, 26 * k, 22 * k, fill="#dff3ff", line="#9fd3ef", rot=20)
        shape(s, MSO_SHAPE.OVAL, x, y, 60 * k, 40 * k, fill="#ffd23f", line=dark, line_w=2)
        for sx in (18, 32):
            shape(s, MSO_SHAPE.RECTANGLE, x + sx * k, y + 3 * k, 7 * k, 34 * k, fill=dark)
        shape(s, MSO_SHAPE.OVAL, x + 44 * k, y + 12 * k, 6 * k, 6 * k, fill=dark)

    s = d.slide(bg=sky, notes="Ask: who has seen a honeycomb up close?")
    for (cx, cy), f in zip([(520, 130), (590, 170), (590, 250), (520, 290), (450, 250), (450, 170), (520, 210)],
                           [honey, "#f9c74f", honey, "#f9c74f", honey, "#f9c74f", "#f8961e"]):
        hexagon(s, cx, cy, 46, f)
    freeform(s, star(640, 60, 26, 11), fill="#ffd23f", line=honey, line_w=2)
    freeform(s, blob(120, 70, 70, 26, 7), fill="#ffffff")
    bee(s, 330, 300, 1.2)
    text(s, 40, 120, 360, 150, [{"text": "How bees build", "size": 26, "color": "#8a5a00"},
                                {"text": "hexagons", "size": 54, "bold": True}],
         font=font, color=dark)

    s = d.slide(bg=sky)
    text(s, 40, 22, 640, 40, ["Why not squares or triangles?"], font=font, size=28, bold=True, color=dark)
    cols = [("Triangles", "3 walls meet at each corner", MSO_SHAPE.ISOSCELES_TRIANGLE, "most wax"),
            ("Squares", "4 walls meet at each corner", MSO_SHAPE.RECTANGLE, "more wax"),
            ("Hexagons", "the roundest shape that tiles", MSO_SHAPE.HEXAGON, "least wax!")]
    for k, (name, why, kind, wax) in enumerate(cols):
        x = 50 + k * 215
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 80, 190, 230, fill="#ffffff", line="#f3d9a4", line_w=2,
              adj=[0.12])
        shp = shape(s, kind, x + 55, 102, 80, 70, fill=[leaf, "#4fc3f7", honey][k])
        if kind == MSO_SHAPE.HEXAGON:
            shp.adjustments[0] = 0.25
        text(s, x + 10, 180, 170, 30, [name], font=font, size=18, bold=True, color=dark, align="c")
        text(s, x + 10, 210, 170, 44, [why], font="Nunito", size=12, color="#5b4a33", align="c")
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x + 45, 262, 100, 30, fill=honey if k == 2 else "#efe3c8",
              adj=[0.5], paras=[wax], font=font, size=12, bold=True, color=dark)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGULAR_CALLOUT, 470, 330, 220, 50, fill="#ffffff", line=dark, line_w=1.5,
          adj=[-0.62, 0.1, 0.16667], paras=["Less wax means more honey!"], font=font, size=14, color=dark)
    bee(s, 370, 336)

    s = d.slide(bg="#e8f5e9", notes="Hand out paper circles; everyone squashes six round cells together.")
    text(s, 40, 22, 640, 40, ["Try it: squash the circles"], font=font, size=28, bold=True, color="#1b5e20")
    steps = ["Cut out seven paper circles.", "Put one in the middle, six around it.",
             "Push them all together from the outside.", "Look at the middle one. What shape is it now?"]
    text(s, 40, 86, 330, 260, [{"text": t, "number": True, "space_after": 12, "bullet_color": "#1b5e20"}
                               for t in steps], font="Nunito", size=17, color="#263238")
    for k in range(6):
        a = math.radians(60 * k)
        shape(s, MSO_SHAPE.OVAL, 500 + 58 * math.cos(a) - 30, 200 + 58 * math.sin(a) - 30, 60, 60,
              fill="#ffffff", line="#1b5e20", line_w=2)
    shape(s, MSO_SHAPE.OVAL, 470, 170, 60, 60, fill="#c5e1a5", line="#1b5e20", line_w=2)
    freeform(s, [(395, 330), (445, 310), (445, 320), (470, 300), (445, 280), (445, 290), (395, 310)],
             fill=honey)
    freeform(s, star(640, 330, 24, 10), fill="#ffd23f", line=honey)

    s = d.slide(bg=sky)
    text(s, 40, 22, 640, 40, ["Did you know?"], font=font, size=28, bold=True, color=dark)
    facts = [("6", "sides on every cell", honey, -4), ("30", "days a worker bee lives in summer", leaf, 3),
             ("8", "wax flakes for a single cell", "#4fc3f7", -2)]
    for k, (n, fact, col, rot) in enumerate(facts):
        x = 50 + k * 215
        card = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, 90, 190, 200, fill="#ffffff", line=col, line_w=4,
                     adj=[0.1], rot=rot)
        shape(s, MSO_SHAPE.OVAL, x + 60, 110, 70, 70, fill=col, rot=rot, paras=[n], font=font, size=30,
              bold=True, color="#ffffff")
        text(s, x + 15, 195, 160, 80, [fact], font="Nunito", size=15, color=dark, align="c")
        card.name = f"Fact {k + 1}"
    freeform(s, blob(600, 350, 80, 22, 9, depth=0.18), fill="#ffffff")
    return d


# ----------------------------------------------------------------------- E: water cycle, 4 scripts

WATER = {
    "he": ("מחזור המים", "Noto Sans Hebrew", True,
           ["אידוי: השמש מחממת את המים באוקיינוס", "עיבוי: אדי המים מתקררים ויוצרים עננים",
            "משקעים: גשם ושלג יורדים אל האדמה", "איסוף: המים זורמים חזרה אל הים"]),
    "ar": ("دورة الماء", "Noto Sans Arabic", True,
           ["التبخر: تسخن الشمس مياه المحيط", "التكاثف: يبرد بخار الماء فتتكون السحب",
            "الهطول: يسقط المطر والثلج على الأرض", "التجمع: تعود المياه إلى البحر"]),
    "ja": ("水の循環", "Noto Sans JP", False,
           ["蒸発：太陽が海の水をあたためます", "凝結：水蒸気が冷えて雲になります",
            "降水：雨や雪が地面に降ります", "集水：水は川を通って海にもどります"]),
    "zh": ("水循环", "Noto Sans SC", False,
           ["蒸发：太阳加热海洋中的水", "凝结：水蒸气冷却后形成云", "降水：雨和雪落到地面",
            "汇集：水沿着河流流回大海"]),
}


def water(pics) -> Deck:
    d = Deck()
    sea, deep, sun, cloud, ink = "#4cc9f0", "#1d3557", "#ffb703", "#ffffff", "#1d3557"

    def scene(s, x0, labels, font, mirror=False):
        """Sun, sea, cloud and the four arrows of the cycle, in a 300 x 280 box at x0."""
        def X(x):
            return x0 + (300 - x if mirror else x)
        shape(s, MSO_SHAPE.RECTANGLE, x0, 250, 300, 60, fill=sea)
        freeform(s, [(X(t * 10), 250 - 6 * math.sin(t * 0.9)) for t in range(31)] + [(X(300), 262), (X(0), 262)],
                 fill=sea)
        shape(s, MSO_SHAPE.OVAL, X(40) - 25, 70, 50, 50, fill=sun)
        freeform(s, blob(X(200), 95, 58, 24, 7), fill=cloud, line="#a8dadc", line_w=1.5)
        for x in (180, 200, 220):
            arrow(s, X(x), 128, X(x - 8), 158, color="#457b9d", w=1.5, head="triangle")
        arrow(s, X(70), 240, X(140), 140, color="#e76f51", w=2.5)
        arrow(s, X(250), 180, X(250), 240, color="#457b9d", w=2.5)
        freeform(s, [(X(250), 250), (X(210), 262), (X(150), 258), (X(100), 268)], line="#1d3557", line_w=2,
                 closed=False)
        spots = [(40, 190), (185, 38), (170, 200), (230, 272)]
        for (lx, ly), lab in zip(spots, labels):
            text(s, X(lx) - 55, ly - 10, 110, 22, [lab], font=font, size=11, bold=True, color=ink, align="c",
                 inset=1)

    s = d.slide(bg="#eaf4f4")
    freeform(s, blob(560, 110, 110, 44, 9), fill="#ffffff")
    shape(s, MSO_SHAPE.RECTANGLE, 0, 330, W, 75, fill=sea)
    text(s, 40, 60, 460, 60, ["The water cycle"], font="Nunito", size=40, bold=True, color=deep)
    names = [(v[0], v[1], v[2]) for v in WATER.values()]
    for k, (name, font, rtl) in enumerate(names):
        text(s, 40 + (k % 2) * 230, 150 + (k // 2) * 60, 210, 44, [{"text": name, "rtl": rtl,
                                                                     "align": "r" if rtl else "l"}],
             font=font, size=26, color="#2a6f97")
    text(s, 40, 345, 640, 40, ["A four-language lesson for a mixed classroom"], font="Nunito", size=14,
         color="#ffffff")

    for lang, (title, font, rtl, lines) in WATER.items():
        s = d.slide(bg="#f7fbfc")
        al = "r" if rtl else "l"
        text(s, 40 if not rtl else 360, 24, 320, 50, [{"text": title, "rtl": rtl, "align": al}], font=font,
             size=30, bold=True, color=deep)
        text(s, 40 if not rtl else 360, 90, 320, 260,
             [{"text": t, "number": True, "rtl": rtl, "align": al, "space_after": 14, "bullet_color": "#e76f51",
               "scheme": "arabicPeriod"} for t in lines], font=font, size=16, color=ink)
        labels = [t.split("：" if "：" in t else ":")[0] for t in lines]
        scene(s, 360 if not rtl else 40, labels, font, mirror=rtl)

    s = d.slide(bg="#f7fbfc", notes="Pairs quiz each other: one reads a word, the other points at the picture.")
    text(s, 40, 24, 640, 44, ["Glossary"], font="Nunito", size=30, bold=True, color=deep)
    words = [["English", "עברית", "العربية", "日本語", "中文"],
             ["evaporation", "אידוי", "التبخر", "蒸発", "蒸发"],
             ["condensation", "עיבוי", "التكاثف", "凝結", "凝结"],
             ["precipitation", "משקעים", "الهطول", "降水", "降水"],
             ["collection", "איסוף", "التجمع", "集水", "汇集"]]
    fonts = ["Nunito", "Noto Sans Hebrew", "Noto Sans Arabic", "Noto Sans JP", "Noto Sans SC"]
    rows = [[{"text": w, "font": fonts[j], "rtl": j in (1, 2)} for j, w in enumerate(r)] for r in words]
    fills = [[deep] * 5] + [["#e3f2f7" if i % 2 else "#ffffff"] * 5 for i in range(1, 5)]
    colors = [["#ffffff"] * 5] + [[ink] * 5 for _ in range(4)]
    table(s, 40, 90, rows, [150, 120, 130, 120, 120], 42, fills=fills, colors=colors, aligns="lrrcc",
          size=15, border="#bcd9e3")
    return d


# --------------------------------------------------------------------------- F: art portfolio

def portfolio(pics) -> Deck:
    d = Deck()
    night, paper, ink, rose = "#14121a", "#f4efe8", "#221f26", "#d17a6b"

    s = d.slide(bg=night)
    picture(s, pics["dawn"], 0, 0, W, H)
    shape(s, MSO_SHAPE.RECTANGLE, 0, 250, W, 155, fill="#14121a")
    text(s, 40, 250, 640, 70, ["Field Notes in Code"], font="Dancing Script", size=48, bold=True, color="#fbe3d6")
    text(s, 42, 322, 640, 50, [{"runs": [("Generative studies, 2024 – 2026", {}),
                                         ("   ·   ", {"color": rose}), ("a portfolio", {"italic": True})]}],
         font="Playfair Display", size=15, color="#e8dcd2")

    s = d.slide(bg=paper)
    text(s, 40, 20, 640, 50, ["Four series"], font="Playfair Display", size=28, color=ink)
    works = [("truchet", "Knots", "quarter circles, 2024"), ("flow", "Currents", "a field of angles, 2025"),
             ("rings", "Echoes", "concentric rings, 2025"), ("marble", "Tide", "summed sines, 2026")]
    for k, (pic, name, what) in enumerate(works):
        x = 40 + k * 163
        picture(s, pics[pic], x, 80, 150, 150, crop=(1 / 6, 0, 1 / 6, 0))
        text(s, x, 238, 150, 26, [name], font="Dancing Script", size=20, bold=True, color=rose)
        text(s, x, 264, 150, 40, [what], font="Playfair Display", size=11, italic=True, color="#5a5360")
    shape(s, MSO_SHAPE.RECTANGLE, 40, 330, 640, 1.5, fill="#c9bfb4")
    text(s, 40, 338, 640, 40, ["Every piece is a short Python program; the seed is part of the title."],
         font="Inter", size=11, color="#5a5360")

    s = d.slide(bg=paper, notes="Mention the plotter version: 3 hours of pen on paper.")
    picture(s, pics["flow"], 40, 40, 330, 325, crop=(0.12, 0.0, 0.12, 0.0))
    text(s, 400, 60, 280, 280,
         [{"text": "Currents #11", "font": "Playfair Display", "size": 30},
          {"text": "2025 · pen plotter, 50 × 50 cm", "size": 12, "color": "#5a5360", "space_after": 16},
          {"text": "Seven hundred walkers follow one angle field. Where two colours meet, the "
                   "field turns faster than the pen can.", "size": 13, "line": 1.2, "space_after": 16},
          {"runs": [("Seed ", {}), ("11", {"font": "Roboto Mono", "color": rose}),
                    (" · steps ", {}), ("60", {"font": "Roboto Mono", "color": rose}),
                    (" · walkers ", {}), ("700", {"font": "Roboto Mono", "color": rose})], "size": 12}],
         font="Inter", color=ink)

    s = d.slide(bg=night)
    text(s, 40, 26, 640, 50, ["From seed to print"], font="Playfair Display", size=28, color="#fbe3d6")
    steps = [("1", "Sketch", "a rule in ten lines of code"), ("2", "Search", "hundreds of seeds, a few kept"),
             ("3", "Tune", "palette, weight, margins"), ("4", "Print", "giclée or pen plotter")]
    for k, (n, head, what) in enumerate(steps):
        x = 60 + k * 160
        shape(s, MSO_SHAPE.OVAL, x, 120, 64, 64, fill=None, line=rose, line_w=2, paras=[n],
              font="Playfair Display", size=24, color="#fbe3d6")
        if k < 3:
            arrow(s, x + 74, 152, x + 150, 152, color="#6d6475", w=1.25, head="stealth")
        text(s, x - 30, 200, 124, 30, [head], font="Dancing Script", size=22, bold=True, color=rose, align="c")
        text(s, x - 30, 232, 124, 50, [what], font="Inter", size=11, color="#d8ccc4", align="c")
    picture(s, pics["portrait1"], 560, 300, 60, 60)
    picture(s, pics["portrait2"], 630, 300, 60, 60)

    s = d.slide(bg=paper)
    picture(s, pics["truchet"], 420, 0, 300, 405, crop=(0.2, 0.0, 0.2, 0.0))
    text(s, 40, 120, 360, 80, ["Thank you"], font="Dancing Script", size=54, bold=True, color=rose)
    text(s, 42, 205, 340, 90, [{"text": "Prints and commissions", "size": 13, "bold": True},
                               {"runs": [("hello@fieldnotes.example", {"link": "mailto:hello@fieldnotes.example",
                                                                       "underline": True})], "size": 13},
                               {"text": "All works CC BY 4.0", "size": 11, "color": "#5a5360", "space_before": 8}],
         font="Inter", color=ink)
    return d


PPTX = {"talk": ("Shipping calm software", talk), "review": ("Harbor & Pine — Q3 review", review),
        "bees": ("How bees build hexagons", bees), "water": ("The water cycle in four languages", water),
        "portfolio": ("Field Notes in Code", portfolio)}


# ------------------------------------------------------------------ A: hashing, native Slides API

def hashing(slides, pid: str) -> None:
    """A lecture written with the Slides API alone: Google's layouts, placeholders and bullets."""
    from beamer2slides.gslides import execute, pt
    pres = execute(slides.presentations().get(presentationId=pid))
    reqs = [{"deleteObject": {"objectId": s["objectId"]}} for s in pres.get("slides", [])]
    serif, sans, mono = "Merriweather", "Lato", "Roboto Mono"
    navy, teal = {"red": 0.10, "green": 0.18, "blue": 0.32}, {"red": 0.0, "green": 0.47, "blue": 0.47}

    def colour(c):
        return {"opaqueColor": {"rgbColor": c}}

    def new(sid, layout, roles):
        reqs.append({"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": layout},
                                     "placeholderIdMappings": [{"layoutPlaceholder": {"type": r, "index": 0},
                                                                "objectId": f"{sid}_{r.lower()}"} for r in roles]}})

    def put(oid, txt, font, size, color=None, bold=False, start=0):
        reqs.append({"insertText": {"objectId": oid, "text": txt, "insertionIndex": start}})
        style(oid, 0, len(txt), font=font, size=size, color=color, bold=bold, all_=True)

    def style(oid, a, b, font=None, size=None, color=None, bold=None, italic=None, all_=False):
        st, f = {}, []
        if font:
            st["fontFamily"] = font
            f.append("fontFamily")
        if size:
            st["fontSize"] = pt(size)
            f.append("fontSize")
        if color:
            st["foregroundColor"] = colour(color)
            f.append("foregroundColor")
        if bold is not None:
            st["bold"] = bold
            f.append("bold")
        if italic is not None:
            st["italic"] = italic
            f.append("italic")
        rng = {"type": "ALL"} if all_ else {"type": "FIXED_RANGE", "startIndex": a, "endIndex": b}
        reqs.append({"updateTextStyle": {"objectId": oid, "style": st, "fields": ",".join(f), "textRange": rng}})

    def box(sid, oid, kind, x, y, w, h, fill=None, line=None, weight=1.0):
        reqs.append({"createShape": {"objectId": oid, "shapeType": kind, "elementProperties": {
            "pageObjectId": sid, "size": {"width": pt(w), "height": pt(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"}}}})
        props, f = {}, []
        props["shapeBackgroundFill"] = {"solidFill": {"color": {"rgbColor": fill}}} if fill else {"propertyState": "NOT_RENDERED"}
        f.append("shapeBackgroundFill")
        if line:
            props["outline"] = {"outlineFill": {"solidFill": {"color": {"rgbColor": line}}}, "weight": pt(weight)}
        else:
            props["outline"] = {"propertyState": "NOT_RENDERED"}
        f.append("outline")
        reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props, "fields": ",".join(f)}})

    def line(sid, oid, x1, y1, x2, y2, color, head=True):
        reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT", "elementProperties": {
            "pageObjectId": sid, "size": {"width": pt(abs(x2 - x1) or 0.01), "height": pt(abs(y2 - y1) or 0.01)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": min(x1, x2), "translateY": min(y1, y2),
                          "unit": "PT"}}}})
        lp = {"lineFill": {"solidFill": {"color": {"rgbColor": color}}}, "weight": pt(1.5)}
        if head:
            lp["endArrow"] = "FILL_ARROW"
        reqs.append({"updateLineProperties": {"objectId": oid, "lineProperties": lp,
                                              "fields": "lineFill,weight" + (",endArrow" if head else "")}})

    def bullets(oid, text, preset="BULLET_DISC_CIRCLE_SQUARE", size=16):
        reqs.append({"insertText": {"objectId": oid, "text": text}})
        style(oid, 0, 0, font=sans, size=size, color={"red": 0.13, "green": 0.13, "blue": 0.13}, all_=True)
        reqs.append({"createParagraphBullets": {"objectId": oid, "bulletPreset": preset,
                                                "textRange": {"type": "ALL"}}})
        return text.replace("\t", "")

    # 1. title
    new("hash01", "TITLE", ["CENTERED_TITLE", "SUBTITLE"])
    put("hash01_centered_title", "Hash tables", serif, 44, navy, bold=True)
    put("hash01_subtitle", "Lecture 7 · Data structures, fall term", sans, 18, teal)

    # 2. nested bullets with inline code and math
    new("hash02", "TITLE_AND_BODY", ["TITLE", "BODY"])
    put("hash02_title", "Why hashing?", serif, 28, navy, bold=True)
    t = bullets("hash02_body", "Arrays give O(1) access by index\n\tbut keys are rarely small integers\n"
                            "A hash function turns any key into an index\n\th(k) = k mod m for integer keys\n"
                            "\tPython calls it hash(key)\nCollisions are unavoidable: more keys than slots")
    for frag, kw in (("O(1)", {"italic": True}), ("h(k) = k mod m", {"font": mono}), ("hash(key)", {"font": mono}),
                     ("unavoidable", {"bold": True})):
        a = t.index(frag)
        style("hash02_body", a, a + len(frag), **kw)

    # 3. diagram: separate chaining
    new("hash03", "TITLE_ONLY", ["TITLE"])
    put("hash03_title", "Separate chaining", serif, 28, navy, bold=True)
    chains = {0: [], 1: ["ada", "kai"], 2: [], 3: ["lin"], 4: ["moe", "ian", "zoe"]}
    grey = {"red": 0.93, "green": 0.95, "blue": 0.96}
    for b, keys in chains.items():
        y = 110 + b * 50
        box("hash03", f"hash03_b{b}", "RECTANGLE", 50, y, 44, 36, fill=grey, line=navy)
        put(f"hash03_b{b}", str(b), mono, 14, navy)
        reqs.append({"updateParagraphStyle": {"objectId": f"hash03_b{b}", "style": {"alignment": "CENTER"},
                                              "fields": "alignment", "textRange": {"type": "ALL"}}})
        x = 94
        for k, key in enumerate(keys):
            line("hash03", f"hash03_a{b}{k}", x, y + 18, x + 30, y + 18, navy)
            box("hash03", f"hash03_k{b}{k}", "ROUND_RECTANGLE", x + 30, y + 2, 60, 32,
                fill={"red": 0.85, "green": 0.94, "blue": 0.94}, line=teal)
            put(f"hash03_k{b}{k}", key, mono, 13, navy)
            reqs.append({"updateParagraphStyle": {"objectId": f"hash03_k{b}{k}", "style": {"alignment": "CENTER"},
                                                  "fields": "alignment", "textRange": {"type": "ALL"}}})
            x += 90
    reqs.append({"createShape": {"objectId": "hash03_note", "shapeType": "TEXT_BOX", "elementProperties": {
        "pageObjectId": "hash03", "size": {"width": pt(260), "height": pt(200)},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 420, "translateY": 110, "unit": "PT"}}}})
    t = bullets("hash03_note", "h(k) = position of k in the alphabet mod 5\nEach bucket holds a list\n"
                            "A lookup walks one list\nExpected length: α = n / m", size=15)
    for frag, kw in (("h(k)", {"font": mono}), ("α = n / m", {"italic": True, "bold": True})):
        a = t.index(frag)
        style("hash03_note", a, a + len(frag), **kw)

    # 4. code
    new("hash04", "TITLE_ONLY", ["TITLE"])
    put("hash04_title", "Insert, in code", serif, 28, navy, bold=True)
    box("hash04", "hash04_code", "RECTANGLE", 40, 100, 380, 200, fill={"red": 0.965, "green": 0.965, "blue": 0.95},
        line={"red": 0.85, "green": 0.85, "blue": 0.82})
    code = ("def insert(table, key, value):\n    i = hash(key) % len(table)\n    for pair in table[i]:\n"
            "        if pair[0] == key:\n            pair[1] = value\n            return\n"
            "    table[i].append([key, value])")
    put("hash04_code", code, mono, 12, {"red": 0.15, "green": 0.15, "blue": 0.2})
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
                style("hash04_code", a, a + len(kw_), color={"red": 0.55, "green": 0.1, "blue": 0.45}, bold=True)
            start = a + 1
    reqs.append({"createShape": {"objectId": "hash04_side", "shapeType": "TEXT_BOX", "elementProperties": {
        "pageObjectId": "hash04", "size": {"width": pt(250), "height": pt(200)},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 440, "translateY": 100, "unit": "PT"}}}})
    t = bullets("hash04_side", "Average cost: O(1 + α)\nWorst case: O(n), every key in one bucket\n"
                            "Resize when α passes 0.75\n\tdouble m, re-insert every key", size=15)

    # 5. table
    new("hash05", "TITLE_ONLY", ["TITLE"])
    put("hash05_title", "Load factor vs. expected probes", serif, 26, navy, bold=True)
    data = [["α", "Chaining", "Linear probing"], ["0.25", "1.13", "1.17"], ["0.50", "1.25", "1.50"],
            ["0.75", "1.38", "2.50"], ["0.90", "1.45", "5.50"]]
    reqs.append({"createTable": {"objectId": "hash05_t", "rows": 5, "columns": 3, "elementProperties": {
        "pageObjectId": "hash05", "size": {"width": pt(420), "height": pt(200)},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 40, "translateY": 100, "unit": "PT"}}}})
    for i, row in enumerate(data):
        for j, val in enumerate(row):
            loc = {"rowIndex": i, "columnIndex": j}
            reqs.append({"insertText": {"objectId": "hash05_t", "cellLocation": loc, "text": val}})
            reqs.append({"updateTextStyle": {"objectId": "hash05_t", "cellLocation": loc, "textRange": {"type": "ALL"},
                                             "style": {"fontFamily": sans, "fontSize": pt(15), "bold": i == 0,
                                                       "foregroundColor": colour(
                                                           {"red": 1, "green": 1, "blue": 1} if i == 0 else navy)},
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
    reqs.append({"createShape": {"objectId": "hash05_side", "shapeType": "TEXT_BOX", "elementProperties": {
        "pageObjectId": "hash05", "size": {"width": pt(210), "height": pt(200)},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 480, "translateY": 100, "unit": "PT"}}}})
    put("hash05_side", "Successful searches, uniform hashing. Probing degrades fast past α = 0.75: "
                    "at 0.9 a lookup reads five and a half slots on average.", sans, 14,
        {"red": 0.3, "green": 0.3, "blue": 0.3})

    # 6. numbered list
    new("hash06", "TITLE_AND_BODY", ["TITLE", "BODY"])
    put("hash06_title", "Choosing m", serif, 28, navy, bold=True)
    t = bullets("hash06_body", "Make m prime when h(k) = k mod m\n\tpowers of two keep only the low bits\n"
                            "Keep α below 0.75 for open addressing\nGrow by doubling, so inserts stay O(1) amortised\n"
                            "Test with your real keys, not random ones", preset="NUMBERED_DIGIT_ALPHA_ROMAN")
    for frag in ("h(k) = k mod m",):
        a = t.index(frag)
        style("hash06_body", a, a + len(frag), font=mono)
    reqs.append({"updatePageProperties": {"objectId": "hash01", "pageProperties": {"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 0.93, "green": 0.96, "blue": 0.96}}}}},
        "fields": "pageBackgroundFill.solidFill.color"}})
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))


# -------------------------------------------------------------------------------------- build

TITLES = {"hashing": "Hash tables — lecture 7", **{k: v[0] for k, v in PPTX.items()}}


def build(names: list[str] | None = None) -> dict:
    from beamer2slides.emit import import_presentation
    from beamer2slides.google_auth import drive_service, slides_service
    from beamer2slides.gslides import execute
    slides, drive = slides_service(), drive_service()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    pics = pictures()
    for name in names or list(TITLES):
        pid = manifest.get(name, {}).get("id")
        if name == "hashing":
            if not pid:
                from beamer2slides.drive_folder import place
                pid = execute(drive.files().create(body=place({"name": TITLES[name], "mimeType":
                                                               "application/vnd.google-apps.presentation"}, drive),
                                                   fields="id"))["id"]
                manifest[name] = {"id": pid}
                MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            hashing(slides, pid)
        else:
            title, make = PPTX[name]
            pid = import_presentation(slides, drive, title, W, H, make(pics).pptx(), pid)["presentationId"]
        execute(drive.permissions().create(fileId=pid, body={"type": "anyone", "role": "reader"}, fields="id"))
        pres = execute(slides.presentations().get(presentationId=pid, fields="title,slides.objectId"))
        manifest[name] = {"id": pid, "title": TITLES[name], "pages": [s["objectId"] for s in pres["slides"]]}
        MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{name}: {len(pres['slides'])} slides  https://docs.google.com/presentation/d/{pid}/view")
    return manifest
