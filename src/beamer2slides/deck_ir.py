"""A live Google Slides deck as IR (deck.json-shaped), for comparison with classify's output.

`deck_ir(pres, ...)` is pure (the `presentations.get` JSON in, IR out); `read_deck` fetches the
presentation and downloads its pictures. Positions go back to PDF pt through emit's text box
model: a box's text starts PAD_X inside it and its first baseline sits BASELINE_A + ASCENT_EM x size
below its top (MIDDLE boxes: MIDDLE_BASELINE_EM below the middle), so `anchor` = (x by alignment,
first baseline) / scale is the same point classify's paragraphs give (compare.text_anchor).
Font sizes go back through FontMapper's width factors (`pdf_size`).

Slide keys: the `b2s:<slide key>/<element key>` alt-text title of our objects, else a base
snapshot's object map, else none (compare aligns slides by title and text).
"""

import hashlib
import json
import math
import re
import urllib.request
from pathlib import Path

from .emit import (ASCENT_EM, BASELINE_A, FONT_FOR_FAMILY, MIDDLE_BASELINE_EM, PAD_X, PPTX_TITLE_DY, SLIDE_W,
                   FontMapper, extra_above)
from .gslides import EMU_PER_PT

FAMILY_FOR_FONT = {v: k for k, v in FONT_FOR_FAMILY.items()}
# Only the three fonts the converter itself writes are named above, and everything else used to come
# back as `sans` - so a deck whose person typed in Space Mono read back as prose, and the size came
# through the wrong width factors as well. A font is known by its name here, the way a reader knows
# it: these words appear in the name of nearly every monospaced or serif family Slides offers.
MONO_WORDS = ("mono", "code", "courier", "consol", "typewriter")
SERIF_WORDS = ("serif", "times", "georgia", "garamond", "playfair", "slab", "libre baskerville",
               "book", "crimson", "lora", "spectral", "cormorant", "eb garamond")
TAG_RE = re.compile(r"^b2s:(?P<slide>[^/]*)/(?P<element>.+)$")
BEAMER_SIZES = {(4, 3): (362.83, 272.13), (16, 9): (453.54, 255.12), (16, 10): (453.54, 283.46)}
DEFAULT_STYLE = {"fontFamily": "Arial", "fontSize": 18.0, "bold": False, "italic": False, "color": "#000000"}


def dim(d: dict | None) -> float:
    if not d or "magnitude" not in d:
        return 0.0
    return d["magnitude"] / (EMU_PER_PT if d.get("unit", "EMU") == "EMU" else 1)


def affine(t: dict | None) -> list[float]:
    """[a, b, tx, d, e, ty] in pt; fields the API leaves out are zero, a missing transform is identity."""
    if not t:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    unit = EMU_PER_PT if t.get("unit", "EMU") == "EMU" else 1.0
    return [t.get("scaleX", 0.0), t.get("shearX", 0.0), t.get("translateX", 0.0) / unit,
            t.get("shearY", 0.0), t.get("scaleY", 0.0), t.get("translateY", 0.0) / unit]


def compose(p: list[float], c: list[float]) -> list[float]:
    a, b, tx, d, e, ty = p
    a2, b2, tx2, d2, e2, ty2 = c
    return [a * a2 + b * d2, a * b2 + b * e2, a * tx2 + b * ty2 + tx, d * a2 + e * d2, d * b2 + e * e2, d * tx2 + e * ty2 + ty]


def box(m: list[float], w: float, h: float) -> list[float]:
    xs = [m[0] * x + m[1] * y + m[2] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [m[3] * x + m[4] * y + m[5] for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    return [min(xs), min(ys), max(xs), max(ys)]


def frame(m: list[float], w: float, h: float, scale: float) -> dict:
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
        q = [1.0, 0.0, 0.0, 1.0]
    elif sx < 1e-9:                         # a line stored with no width: its first axis is the second's normal
        q = [e / sy, b / sy, -b / sy, e / sy]
    elif sy < 1e-9:                         # ... or no height (most straight connectors)
        q = [a / sx, -d / sx, d / sx, a / sx]
    else:
        q = [a / sx, b / sy, d / sx, e / sy]
    W, H = sx * w, sy * h
    if q[0] * q[3] - q[1] * q[2] >= 0:
        rot, flip = math.degrees(math.atan2(q[2], q[0])), False
    else:                                   # R(rot) . mirror(x): the first axis points the other way
        rot, flip = math.degrees(math.atan2(-q[2], -q[0])), True
    rot = (rot + 180) % 360 - 180
    cx, cy = tx + q[0] * W / 2 + q[1] * H / 2, ty + q[2] * W / 2 + q[3] * H / 2
    out = {"size": [round(W / scale, 3), round(H / scale, 3)], "origin": [round(tx / scale, 3), round(ty / scale, 3)],
           "matrix": [round(v, 6) for v in q], "rotation": round(rot, 3), "flip": flip,
           "box": [round(v / scale, 3) for v in (cx - W / 2, cy - H / 2, cx + W / 2, cy + H / 2)]}
    if abs(q[0] * q[1] + q[2] * q[3]) > 1e-3:
        out["shear"] = True
    return out


def turned(m: list[float]) -> bool:
    """Anything but a plain scale and shift: rotated, mirrored or sheared."""
    return abs(m[1]) > 1e-9 or abs(m[3]) > 1e-9 or m[0] < 0 or m[4] < 0


def outline_props(outline: dict, scale: float, scheme: dict) -> dict:
    """A shape's outline as adopt draws it: colour (None when not drawn), weight in PDF pt (Slides'
    default 0.75 pt when the deck says nothing), alpha and dash style."""
    fill = outline.get("outlineFill", {}).get("solidFill", {})
    # an outline at alpha 0 is how Slides hides one without switching it off
    drawn = outline.get("propertyState", "RENDERED") == "RENDERED" and bool(fill) and fill.get("alpha", 1.0) > 0.004
    out = {"outline": rgb_hex(fill.get("color"), scheme) if drawn else None,
           "weight": round((dim(outline.get("weight")) or 0.75) / scale, 3)}
    if drawn and fill.get("alpha", 1.0) < 1.0:
        out["outline_alpha"] = round(fill["alpha"], 4)
    if drawn and outline.get("dashStyle", "SOLID") != "SOLID":
        out["dash"] = outline["dashStyle"]
    return out


def rgb_hex(color: dict | None, scheme: dict[str, str]) -> str | None:
    if not color:
        return None
    c = color.get("opaqueColor", color)
    if "themeColor" in c:
        return scheme.get(c["themeColor"])
    rgb = c.get("rgbColor")
    if rgb is None:
        return None
    return "#" + "".join(f"{round(rgb.get(k, 0.0) * 255):02x}" for k in ("red", "green", "blue"))


def design_for(size: float) -> int:
    """Computer Modern optical size LaTeX picks for a font size."""
    return 8 if size < 8.5 else 9 if size < 9.5 else 10 if size < 11.5 else 12 if size < 17 else 17


def family_of(font: str) -> str:
    """Which of classify's three families a Slides font name belongs to."""
    if font in FAMILY_FOR_FONT:
        return FAMILY_FOR_FONT[font]
    low = font.lower()
    if any(w in low for w in MONO_WORDS):
        return "mono"
    if any(w in low for w in SERIF_WORDS):
        return "serif"
    return "sans"


def pdf_size(fonts: FontMapper, family: str, slides_size: float, bold: bool, italic: bool, scale: float,
             font: str | None = None) -> tuple[float, str]:
    """Inverse of FontMapper: (PDF font size, a TeX font name that maps like it)."""
    tex_family = FAMILY_FOR_FONT.get(family)
    if tex_family is None:  # a Google font used as-is (or Arial for a box the user added)
        return round(slides_size / scale, 2), font or family.replace(" ", "")
    size = slides_size / scale
    name = font
    for _ in range(4):
        if font is None:
            prefix = {"sans": "CMSS", "serif": "CMR", "mono": "CMTT"}[tex_family]
            name = f"{prefix}{design_for(size)}"
        run = {"font": name, "family": tex_family, "size": size, "bold": bold, "italic": italic}
        z = fonts(run, scale)[1]
        if z <= 0:
            break
        size = size * slides_size / z if abs(z - slides_size) > 0.05 else size
    # FontMapper rounds to 0.1 pt: one more exact step without rounding
    return round(size, 2), name


class StyleResolver:
    """Text style fields a run leaves out come from its placeholder's parents (layout, master)."""

    def __init__(self, pres: dict):
        self.by_id: dict[str, dict] = {}
        self.bare_imports = False   # deck_ir sets it: `imports_lack_insets`
        for page in pres.get("layouts", []) + pres.get("masters", []):
            for pe in walk_elements(page.get("pageElements", [])):
                self.by_id[pe["objectId"]] = pe
        self.scheme = {}
        for master in pres.get("masters", []):
            for c in master.get("pageProperties", {}).get("colorScheme", {}).get("colors", []):
                self.scheme.setdefault(c["type"], rgb_hex({"rgbColor": c.get("color", {})}, {}))

    def chain(self, pe: dict) -> list[dict]:
        """The placeholder's parents, nearest last (master, then layout)."""
        out = []
        cur = pe
        for _ in range(4):
            parent = cur.get("shape", {}).get("placeholder", {}).get("parentObjectId")
            if not parent or parent not in self.by_id:
                break
            cur = self.by_id[parent]
            out.append(cur)
        return list(reversed(out))

    @staticmethod
    def _level_paragraph(el: dict, level: int) -> tuple[dict, dict]:
        """(paragraph marker, first run style) of a parent placeholder's paragraph for list level
        `level`: a BODY placeholder holds one empty paragraph per nesting level, each styled for
        its level (the cs161 decks' master: 18 pt at level 0, 14 pt below it, and 12 pt below every
        paragraph). Reading only the first one set every sub-item in the level-0 size, which broke
        all their lines in other places."""
        found: list[tuple[dict, dict]] = []
        marker = None
        for te in el.get("shape", {}).get("text", {}).get("textElements", []):
            if "paragraphMarker" in te:
                marker = te["paragraphMarker"]
                found.append((marker, {}))
            elif "textRun" in te and found and not found[-1][1]:
                found[-1] = (found[-1][0], te["textRun"].get("style", {}) or {"_": None})
        for pm, st in found:
            if (pm.get("bullet") or {}).get("nestingLevel", 0) == level:
                return pm, st
        return found[0] if found else ({}, {})

    def parent_style(self, pe: dict, level: int = 0) -> dict:
        style: dict = {}
        for el in self.chain(pe):
            style.update({k: v for k, v in self._level_paragraph(el, level)[1].items() if k != "_"})
        return style

    def parent_paragraph_style(self, pe: dict, level: int = 0) -> dict:
        """What a paragraph's own `paragraphMarker.style` leaves out, from the same parents.

        A placeholder inherits how its paragraphs sit, not only how their letters look: the DevFest
        template centres its subtitle on the master and every slide using it says nothing at all, so
        reading only the slide makes centred text left-aligned - which no later round can put right,
        because the loop has no translator for alignment at all."""
        style: dict = {}
        for el in self.chain(pe):
            style.update(self._level_paragraph(el, level)[0].get("style", {}))
        return style

    def parent_bullet_style(self, pe: dict, level: int = 0) -> dict:
        """The text style the parents' lists give a bullet at `level` (colour, size, font)."""
        style: dict = {}
        for el in self.chain(pe):
            for lst in el.get("shape", {}).get("text", {}).get("lists", {}).values():
                style.update(lst.get("nestingLevel", {}).get(str(level), {}).get("bulletStyle", {}))
        return style

    def parent_shape_property(self, pe: dict, key: str):
        """A shape property the element leaves out (`contentAlignment`, `autofit`), from its parents."""
        value = None
        for el in self.chain(pe):
            value = el.get("shape", {}).get("shapeProperties", {}).get(key, value)
        return value


def walk_elements(elements: list[dict]):
    for pe in elements:
        yield pe
        if "elementGroup" in pe:
            yield from walk_elements(pe["elementGroup"].get("children", []))


def flatten(elements: list[dict], parent: list[float] | None = None, group: str | None = None):
    """(page element, absolute matrix, top group id) for every leaf element."""
    parent = parent or [1, 0, 0, 0, 1, 0]
    for pe in elements:
        m = compose(parent, affine(pe.get("transform")))
        if "elementGroup" in pe:
            yield from flatten(pe["elementGroup"].get("children", []), m, group or pe["objectId"])
        else:
            yield pe, m, group


def base_style(pe: dict, resolver: StyleResolver, level: int) -> dict:
    """The text style a run of list level `level` has where it says nothing, from the parents."""
    base = {**DEFAULT_STYLE}
    parent = resolver.parent_style(pe, level)
    base.update({k: v for k, v in parent.items() if k in ("fontFamily", "bold", "italic")})
    if parent.get("fontSize"):
        base["fontSize"] = dim(parent["fontSize"])
    if parent.get("weightedFontFamily"):
        base["fontFamily"] = parent["weightedFontFamily"]["fontFamily"]
        if parent["weightedFontFamily"].get("weight", 400) >= 600:
            base["bold"] = True
    if parent.get("foregroundColor"):
        base["color"] = rgb_hex(parent["foregroundColor"], resolver.scheme) or base["color"]
    return base


def text_paragraphs(pe: dict, text: dict, resolver: StyleResolver, fonts: FontMapper, scale: float,
                    keep_blank: bool = False, font_scale: float = 1.0, spacing_cut: float = 0.0,
                    keep_trailing: bool = False) -> list[dict]:
    """`font_scale` and `spacing_cut` are the box's autofit (`shrink text on overflow`, or a .pptx's
    normAutofit): Slides draws every run at `font_scale` times its size and takes `spacing_cut` off
    every paragraph's line spacing. The cs161 decks' titles say 28 pt and are drawn at 25.2: the
    thumbnail's cap height is 18.0 pt, Arial's is 0.716 em."""
    bases: dict[int, dict] = {}
    paragraphs: list[dict] = []
    cur = None
    lists = text.get("lists", {})
    for te in text.get("textElements", []):
        if "paragraphMarker" in te:
            pm = te["paragraphMarker"]
            bullet = pm.get("bullet")
            nesting = bullet.get("nestingLevel", 0) if bullet else 0
            base = bases.setdefault(nesting, base_style(pe, resolver, nesting))
            st = {**resolver.parent_paragraph_style(pe, nesting), **pm.get("style", {})}
            glyph = bullet.get("glyph", "") if bullet else ""
            bstyle = {}
            if bullet:
                # what a bullet looks like: the parents' list level, the box's own list level, then
                # the paragraph's own bullet style; what none of them says is the first run's
                bstyle = {**resolver.parent_bullet_style(pe, nesting),
                          **lists.get(bullet.get("listId", ""), {}).get("nestingLevel", {}).get(
                              str(nesting), {}).get("bulletStyle", {}),
                          **(bullet.get("bulletStyle") or {})}
            # `align` is where the lines sit on the page. START and END are the paragraph's own
            # start and end, so in a right-to-left paragraph (Hebrew, Arabic) START is flush right.
            rtl = st.get("direction") == "RIGHT_TO_LEFT"
            cur = {"align": {"START": "left", "CENTER": "center", "END": "right", "JUSTIFIED": "left"}.get(
                       st.get("alignment", "START"), "left"),
                   "justified": st.get("alignment") == "JUSTIFIED",
                   "level": 0, "nesting": nesting,
                   "bullet": ({"kind": "number" if re.search(r"\d|[a-z]\.|[ivx]+\.", glyph) else "glyph",
                               "text": glyph} if bullet else None),
                   "bullet_style": bstyle,
                   "indent_start": dim(st.get("indentStart")), "indent_first": dim(st.get("indentFirstLine")),
                   "indent_end": dim(st.get("indentEnd")),
                   "line_spacing": max(0.1, (st.get("lineSpacing") or 100) / 100 - spacing_cut),
                   "space_above": dim(st.get("spaceAbove")), "space_below": dim(st.get("spaceBelow")),
                   "spacing_mode": st.get("spacingMode"), "base": base,
                   "runs": [], "tab_x0": None}
            if rtl:
                cur["direction"] = "rtl"
                cur["align"] = {"left": "right", "right": "left"}.get(cur["align"], cur["align"])
            paragraphs.append(cur)
        elif "textRun" in te or "autoText" in te:
            if cur is None:
                continue
            base = cur["base"]
            tr = te.get("textRun") or te.get("autoText")
            content = tr.get("content", "")
            st = tr.get("style", {})
            family = (st.get("weightedFontFamily") or {}).get("fontFamily") or st.get("fontFamily") or base["fontFamily"]
            size = (dim(st.get("fontSize")) or base["fontSize"]) * font_scale
            bold = st.get("bold", base["bold"])
            if (st.get("weightedFontFamily") or {}).get("weight", 400) >= 600:
                bold = True
            italic = st.get("italic", base["italic"])
            color = rgb_hex(st.get("foregroundColor"), resolver.scheme) or base["color"]
            psize, font = pdf_size(fonts, family, size, bold, italic, scale)
            link = st.get("link") or {}
            text_part = content.rstrip("\n") if content.endswith("\n") else content
            if not text_part:
                if not cur["runs"]:
                    # an empty line's height is its own newline's style
                    cur["newline"] = {"text": " ", "font": font, "family": family_of(family), "slides_font": family,
                                      "slides_size": size, "size": psize, "bold": bool(bold),
                                      "italic": bool(italic), "smallcaps": False, "color": color, "link": None,
                                      "script": None, "underline": False, "strike": False, "highlight": None}
                continue
            run = {"text": text_part, "font": font, "family": family_of(family),
                   "slides_font": family, "slides_size": size, "size": psize, "bold": bool(bold),
                   "italic": bool(italic), "smallcaps": bool(st.get("smallCaps")), "color": color,
                   "link": link.get("url") or (f"#slide={link['pageObjectId']}" if link.get("pageObjectId") else None),
                   "script": {"SUPERSCRIPT": "super", "SUBSCRIPT": "sub"}.get(st.get("baselineOffset")),
                   "underline": bool(st.get("underline")), "strike": bool(st.get("strikethrough")),
                   "highlight": rgb_hex(st.get("backgroundColor"), resolver.scheme)}
            if family == "Roboto Mono" and text_part.strip("\u00a0") == "" and "\u00a0" in text_part:
                run["hole"] = round(len(text_part) * 0.6 * size / scale, 2)
                run["text"] = " "
            cur["runs"].append(run)
    for p in paragraphs:
        p["runs"] = merge_runs(p["runs"])
        p["size"] = max((r["size"] for r in p["runs"]), default=0.0)
        p.pop("base", None)
        bstyle = p.pop("bullet_style")
        if p["bullet"] and p["runs"]:
            first = p["runs"][0]
            bsize = dim(bstyle.get("fontSize")) * font_scale
            family = (bstyle.get("weightedFontFamily") or {}).get("fontFamily") or bstyle.get("fontFamily")
            p["bullet"].update({
                "color": rgb_hex(bstyle.get("foregroundColor"), resolver.scheme) or first["color"],
                "size": round(first["size"] * bsize / first["slides_size"], 2) if bsize and first["slides_size"]
                else first["size"],
                "font_family": family_of(family) if family else first["family"],
                "bold": bool(bstyle.get("bold", first["bold"]))})
        text = "".join(r["text"] for r in p["runs"])
        if "\t" in text and not p["bullet"]:
            p["tab_x0"] = p["indent_start"]
    trailing = keep_trailing and keep_blank and any(p["runs"] for p in paragraphs)
    if not trailing:
        paragraphs = [p for p in paragraphs if p["runs"] or p is not paragraphs[-1]]
    if trailing:
        # Under a middle- or bottom-aligned stack the empty lines a person left at the end are
        # height all the same: ap-bio-stats' bodies end on an empty 24 pt line at 80% after 7 pt,
        # and their text sits 15 pt higher than the stack without it would.
        last = max(i for i, p in enumerate(paragraphs) if p["runs"])
        for p in paragraphs[last + 1:]:
            p["runs"] = [dict(p.get("newline") or paragraphs[last]["runs"][-1], text=" ", link=None,
                              underline=False, strike=False, highlight=None)]
    for p in paragraphs:
        p.pop("newline", None)
    if keep_blank:
        # A blank line a person left in a text box is vertical space they chose, and dropping it
        # pulls everything under it up by a line - of the 717 paragraphs of the DevFest template,
        # 282 are blank. A PDF has no empty paragraph, only the gap one leaves, so classify never
        # makes one and `pull`'s IR must not either; a foreign deck is read from the deck itself,
        # where the blank line is still there to be read. It becomes a space in the style of the
        # paragraph it stands above, which is the size the person's Return left room for.
        if not trailing:
            last = max((i for i, p in enumerate(paragraphs) if p["runs"]), default=-1)
            del paragraphs[last + 1:]                   # under a top-aligned stack they push nothing down
        below = None
        for p in reversed(paragraphs):
            if p["runs"]:
                below = p["runs"][0]
            elif below is not None:
                p["runs"] = [{**below, "text": " ", "link": None, "underline": False,
                              "strike": False, "highlight": None}]
                p["size"] = below["size"]
    # bullet levels as classify counts them: clusters (2 pt apart) of the bullets' left edges
    levels: list[float] = []
    for x in sorted({p["indent_first"] / scale for p in paragraphs if p["bullet"]}):
        if not levels or x - levels[-1] > 2:
            levels.append(x)
    for p in paragraphs:
        if p["bullet"]:
            p["level"] = min(range(len(levels)), key=lambda i: abs(levels[i] - p["indent_first"] / scale))
    return paragraphs


def merge_runs(runs: list[dict]) -> list[dict]:
    out = []
    keys = ("font", "size", "bold", "italic", "color", "link", "script", "underline", "strike", "highlight", "smallcaps")
    for r in runs:
        if out and not r.get("hole") and not out[-1].get("hole") and all(out[-1][k] == r[k] for k in keys):
            out[-1]["text"] += r["text"]
        else:
            out.append(dict(r))
    return out


ZERO_INSET_SLACK = 5.0      # Slides pt: less room than this beside the text of a box that fits it = no insets
PITCH_EM = 1.19             # docs/calibration.md: the line pitch at lineSpacing 100, every font


def zero_insets(paragraphs: list[dict], height: float) -> bool:
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
        if not p["runs"]:
            continue
        z = max(r["slides_size"] for r in p["runs"])
        need += PITCH_EM * z * p["line_spacing"] + p["space_above"] + p["space_below"]
    return need > 0 and height - need < ZERO_INSET_SLACK


def imported(shape: dict) -> bool:
    """Was this text box made by a .pptx import? Drive's importer writes spacingMode NEVER_COLLAPSE on
    the paragraphs, Slides' own boxes say COLLAPSE_LISTS. Among the boxes that resize to fit their
    text and whose height does not give their insets away (wrapped lines), those an import made had
    no insets 169 times in 186 on the corpus thumbnails, Slides' own ones had theirs 375 times in 376."""
    for te in shape.get("text", {}).get("textElements", []):
        if "paragraphMarker" in te:
            return te["paragraphMarker"].get("style", {}).get("spacingMode") == "NEVER_COLLAPSE"
    return False


def imports_lack_insets(pres: dict, resolver: StyleResolver, fonts: FontMapper, scale: float) -> bool:
    """Did the .pptx this deck was imported from set its text insets to 0? `imported` alone does not
    say: gdg24's template came through an import too and kept Slides' insets (its 9 imported boxes
    that grow with their text all sit where default insets put them). The deck's own boxes do: among
    its imported boxes that resize to fit their text, those whose height leaves no room for insets
    (`zero_insets`) prove the template's choice - 9 of 15 in cs161-net, 34 to 119 in each
    SlidesCarnival deck, none of gdg24's 9. At least a quarter of them, and two."""
    seen = proven = 0
    for page in pres.get("slides", []) + pres.get("layouts", []) + pres.get("masters", []):
        for pe, m, _ in flatten(page.get("pageElements", [])):
            shape = pe.get("shape", {})
            autofit = shape.get("shapeProperties", {}).get("autofit") or {}
            if "size" not in pe or autofit.get("autofitType") != "SHAPE_AUTOFIT" or not imported(shape):
                continue
            paragraphs = text_paragraphs(pe, shape.get("text", {}), resolver, fonts, scale, keep_blank=True,
                                         font_scale=autofit.get("fontScale") or 1.0,
                                         spacing_cut=autofit.get("lineSpacingReduction") or 0.0)
            if not any(p["runs"] for p in paragraphs):
                continue
            _, y0, _, y1 = box(m, dim(pe["size"]["width"]), dim(pe["size"]["height"]))
            seen += 1
            proven += zero_insets(paragraphs, y1 - y0)
    return proven >= 2 and proven * 4 >= seen


def text_element(pe: dict, m: list[float], resolver: StyleResolver, fonts: FontMapper, scale: float,
                 page_w: float, foreign: bool = False) -> dict | None:
    shape = pe.get("shape", {})
    props = shape.get("shapeProperties", {})
    # Read from the parents for a foreign deck only: emit's placeholders were calibrated against what
    # the slide itself says (PPTX_TITLE_DY), and `pull` must keep reading them that way.
    autofit = props.get("autofit") or (resolver.parent_shape_property(pe, "autofit") if foreign else None) or {}
    font_scale = autofit.get("fontScale") or 1.0
    spacing_cut = autofit.get("lineSpacingReduction") or 0.0
    # a placeholder sits where its layout says when it says nothing itself (title placeholders are
    # often bottom-aligned there)
    content = props.get("contentAlignment") or \
        (resolver.parent_shape_property(pe, "contentAlignment") if foreign else None) or "TOP"
    paragraphs = text_paragraphs(pe, shape.get("text", {}), resolver, fonts, scale, keep_blank=foreign,
                                 font_scale=font_scale, spacing_cut=spacing_cut,
                                 keep_trailing=content in ("MIDDLE", "BOTTOM"))
    if not any(p["runs"] for p in paragraphs):
        return None
    w, h = dim(pe["size"]["width"]), dim(pe["size"]["height"])
    x0, y0, x1, y1 = box(m, w, h)
    placeholder = shape.get("placeholder", {}).get("type")
    first = next(p for p in paragraphs if p["runs"])
    z = max(r["slides_size"] for r in first["runs"])
    aligns = {p["align"] for p in paragraphs if p["runs"]}
    align = aligns.pop() if len(aligns) == 1 else "left"
    # only a foreign deck: the converter's own boxes are created by the API, with Slides' insets
    bare = foreign and autofit.get("autofitType") == "SHAPE_AUTOFIT" and \
        (zero_insets(paragraphs, y1 - y0) or (resolver.bare_imports and imported(shape)))
    pad_x, top = (0.0, 0.0) if bare else (PAD_X, BASELINE_A)
    if content == "MIDDLE":
        baseline = (y0 + y1) / 2 + MIDDLE_BASELINE_EM * z
    else:
        baseline = y0 + top + ASCENT_EM * z + extra_above(first["line_spacing"], z) + first["space_above"]
        if placeholder in ("TITLE", "CENTERED_TITLE", "SUBTITLE") and not bare:
            baseline -= PPTX_TITLE_DY
    # emit puts the box PAD_X left of the text's (or bullet's) left edge; centred and right-aligned
    # boxes are widened symmetrically / to the left
    x = {"left": x0 + pad_x, "center": (x0 + x1) / 2, "right": x1 - pad_x}[align]
    lines_x1 = (x1 - pad_x) / scale
    out_paras = []
    for p in paragraphs:
        if not p["runs"]:
            continue
        tx0 = (x0 + pad_x + p["indent_start"]) / scale
        out_paras.append({
            "align": p["align"], "level": p["level"], "bullet": p["bullet"] and {**p["bullet"], "bbox": None},
            **({"direction": "rtl"} if p.get("direction") == "rtl" else {}),
            "size": p["size"], "text_x0": round(tx0, 2), "tab_x0": p["tab_x0"],
            "lines": [{"baseline": None, "x0": round(tx0, 2), "x1": round(lines_x1, 2)}],
            "runs": [{k: v for k, v in r.items() if k not in ("slides_font", "slides_size")} for r in p["runs"]],
            "slides": {"indent_start": p["indent_start"], "indent_first": p["indent_first"],
                       "line_spacing": p["line_spacing"], "space_above": p["space_above"],
                       "space_below": p["space_below"], "indent_end": p["indent_end"],
                       "spacing_mode": p["spacing_mode"], "justified": p["justified"],
                       "font": p["runs"][0]["slides_font"], "size": p["runs"][0]["slides_size"]},
        })
    out_paras[0]["lines"][0]["baseline"] = round(baseline / scale, 2)
    role = "title" if placeholder in ("TITLE", "CENTERED_TITLE") else "body"
    # `box`: what adopt needs to lay the box out as Slides does (adopt.text_box_latex). The
    # paragraphs' `slides` values are in Slides pt, so the scale that turns them into the IR's goes
    # with them.
    return {"kind": "text", "role": role, "bbox": [round(v / scale, 2) for v in (x0, y0, x1, y1)],
            "anchor": [round(x / scale, 2), round(baseline / scale, 2)], "wrap_width": round((x1 - x0 - 2 * pad_x) / scale, 2),
            "placeholder": placeholder, "paragraphs": out_paras,
            "box": {"valign": {"MIDDLE": "middle", "BOTTOM": "bottom"}.get(content, "top"), "scale": scale,
                    "font_scale": font_scale, "grows": autofit.get("autofitType") == "SHAPE_AUTOFIT",
                    **({"insets": 0} if bare else {})}}


def page_background(page: dict, resolver_pages: dict[str, dict], scheme: dict) -> tuple[str | None, str | None]:
    """(solid colour, picture url) of a page's background, following INHERIT to layout and master."""
    cur = page
    for _ in range(3):
        fill = cur.get("pageProperties", {}).get("pageBackgroundFill", {})
        if fill and fill.get("propertyState", "RENDERED") != "INHERIT":
            if "solidFill" in fill:
                colour = rgb_hex(fill["solidFill"].get("color"), scheme)
                alpha = fill["solidFill"].get("alpha", 1.0)
                if colour and alpha < 1:
                    # a see-through page background shows white under it (arabic-training's master
                    # is #4bacc6 at alpha 0.247, which Slides draws as a pale #d3eaf1)
                    colour = "#" + "".join(f"{round(255 - (255 - int(colour[i:i + 2], 16)) * alpha):02x}"
                                           for i in (1, 3, 5))
                return colour, None
            if "stretchedPictureFill" in fill:
                return None, fill["stretchedPictureFill"].get("contentUrl")
            return None, None
        parent = cur.get("slideProperties", {}).get("layoutObjectId") or cur.get("layoutProperties", {}).get("masterObjectId")
        if not parent or parent not in resolver_pages:
            break
        cur = resolver_pages[parent]
    return None, None


def stash_picture(url: str, fetch, images: Path) -> dict:
    """Download a picture into `images`, sha1-named: {file, sha1, format}, or {error}."""
    try:
        # contentUrl (=s2048) gives the stored picture byte for byte, as the .pptx export does;
        # crop, transparency, rotation and outline are not baked into it (tools/probe_images.py)
        data = fetch(url)
        sha = hashlib.sha1(data).hexdigest()
        fmt = image_format(data)
        images.mkdir(parents=True, exist_ok=True)
        path = images / f"{sha[:16]}.{FORMAT_EXT.get(fmt, 'img')}"
        if not path.exists():
            path.write_bytes(data)
        return {"file": str(path), "sha1": sha, "format": fmt}
    except OSError as e:
        return {"error": str(e)[:120]}


def notes_text(slide: dict) -> str | None:
    props = slide.get("slideProperties", {})
    page = props.get("notesPage", {})
    sid = page.get("notesProperties", {}).get("speakerNotesObjectId")
    for pe in page.get("pageElements", []):
        if pe.get("objectId") == sid:
            text = "".join(te.get("textRun", {}).get("content", "")
                           for te in pe.get("shape", {}).get("text", {}).get("textElements", []))
            text = text.strip()
            return text or None
    return None


# A foreign deck bigger than this many times beamer's page (a 48 x 36 in poster: 9.5) is not a slide
# deck at beamer's scale: its 24 pt body text would be 2.5 pt, below what TeX's fixed skips and struts
# are made for. Such a page keeps half the deck's size, like any page of a ratio beamer has no
# option for; 1920 x 1080 (4.2) is still an ordinary 16:9 deck.
MAX_BEAMER_SCALE = 5.0


def page_size_for(pres: dict, pdf_size: list[float] | None, foreign: bool = False) -> tuple[float, float, float]:
    """(page width, page height, scale) of the IR: the PDF page the deck came from, else beamer's
    page of the deck's aspect, else half the deck's size (a ratio beamer has no option for: adopt
    writes that page with `\\geometry`, `adopt.page_setup`)."""
    w = dim(pres["pageSize"]["width"])
    h = dim(pres["pageSize"]["height"])
    if pdf_size:
        return pdf_size[0], pdf_size[1], w / pdf_size[0]
    ratio = w / h
    for (a, b), size in BEAMER_SIZES.items():
        if abs(ratio - a / b) < 0.01 and not (foreign and w / size[0] > MAX_BEAMER_SCALE):
            return size[0], size[1], w / size[0]
    return w / 2, h / 2, 2.0


def inherited_chain(slide: dict, pages: dict[str, dict]) -> list[dict]:
    """The master and then the layout a slide draws on top of, in that drawing order.

    A deck a person built in Slides keeps most of its look here: of the 39 slides of the DevFest
    template, the section-title slide carries one element of its own and draws six - the blue dotted
    sheet, the white card, the bar, the dot - from its layout and master. A deck this repository
    converted is the other way round (the source draws its theme and `emit.plan_theme` puts the
    picture on the layouts), which is why `pull` must not see these: it would write the decoration
    into the .tex that already draws it. `adopt` asks for them, `pull` does not."""
    layout = pages.get(slide.get("slideProperties", {}).get("layoutObjectId") or "")
    master = pages.get((layout or {}).get("layoutProperties", {}).get("masterObjectId") or "")
    return [p for p in (master, layout) if p]


def deck_ir(pres: dict, pdf_size: list[float] | None = None, base: dict | None = None,
            fetch=None, images: Path | None = None, foreign: bool = False, thumbnails=None) -> dict:
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
    Without it those fills stay unknown, and shapes with nothing else to draw are left out."""
    page_w, page_h, scale = page_size_for(pres, pdf_size, foreign)
    fonts = FontMapper()
    resolver = StyleResolver(pres)
    resolver.bare_imports = foreign and imports_lack_insets(pres, resolver, fonts, scale)
    pages = {p["objectId"]: p for p in pres.get("layouts", []) + pres.get("masters", [])}
    object_keys, slide_keys = {}, {}
    for s in (base or {}).get("slides", []):
        if s.get("objectId"):
            slide_keys[s["objectId"]] = s.get("key")
        for e in s.get("elements", []):
            for oid in e.get("objects", []):
                object_keys[oid] = (s.get("key"), e.get("key"))
    slides = []

    def read_page(page: dict, tags: list) -> list[dict]:
        out: list[dict] = []
        lines: dict[str, int] = {}
        for pe, m, group in flatten(page.get("pageElements", [])):
            if "line" in pe and group:
                lines[group] = lines.get(group, 0) + 1
            tag = TAG_RE.match(pe.get("title") or "")
            key = (tag.group("slide"), tag.group("element")) if tag else object_keys.get(pe["objectId"]) or \
                (object_keys.get(group) if group else None)
            if key and key[0]:
                tags.append(key[0])
            el = element_of(pe, m, resolver, fonts, scale, page_w, fetch, images, foreign)
            if el is None:
                continue
            el.update({"id": pe["objectId"], "object": pe["objectId"], "group": group,
                       "key": key[1] if key else None})
            if el["kind"] == "image" and key and key[1] and key[1].split("/")[1:2] in (["math"], ["icon"]):
                el["role"] = key[1].split("/")[1]
            elif el["kind"] == "image" and pe.get("title") in ("Formula", "Icon"):
                el["role"] = {"Formula": "math", "Icon": "icon"}[pe["title"]]
            if el["kind"] == "text" and el["key"] and "/footer/" in f"/{el['key']}/":
                el["role"] = "footer"
            out.append(el)
        return out if foreign else fold_groups(out, lines)

    for n, slide in enumerate(pres.get("slides", [])):
        tags: list = []
        under: list[dict] = []
        if foreign:
            for page in inherited_chain(slide, pages):
                for el in read_page(page, []):
                    # A placeholder on a layout is the slide's to fill - it holds the layout's prompt
                    # text ("Click to edit"), or only the "\n" per list level an import leaves there -
                    # and drawing it would print that over the slide's own words.
                    if el.get("placeholder") or (el["kind"] == "text" and not el.get("paragraphs")):
                        continue
                    # `role` math/icon says "this picture belongs inside a line of text", which a
                    # picture on a layout never is: it is the template's decoration, and the
                    # heuristic that reads a picture beside one short paragraph as an icon
                    # (`fold_groups`) would otherwise call the full-page backdrop one.
                    role = "figure" if el["kind"] == "image" else el.get("role")
                    under.append({**el, "role": role, "inherited": page["objectId"],
                                  "id": f"{page['objectId']}~{el['id']}"})
        elements = under + read_page(slide, tags)
        color, picture = page_background(slide, pages, resolver.scheme)
        if foreign:
            from . import deck_fills
            thumb = thumbnails(n) if thumbnails else None
            px = 0.0
            if thumb is not None:
                thumb = deck_fills.load(thumb)
                px = thumb.shape[1] / page_w
            elements = deck_fills.settle(elements, thumb, px, None if picture else color, bool(picture))
        key = slide_keys.get(slide["objectId"]) or (max(set(tags), key=tags.count) if tags else None)
        slides.append({"page": n, "frame": str(n + 1), "size": [page_w, page_h], "objectId": slide["objectId"],
                       "key": key, "notes": notes_text(slide), "background_color": color,
                       "background_picture": picture, "elements": elements})
        if foreign and picture and fetch and images is not None:
            # adopt draws it (a stretched picture fill is the whole page); pull never does, the
            # source it refines already draws whatever the converter baked into it
            got = stash_picture(picture, fetch, images)
            slides[-1]["background_file"] = got.get("file")
    return {"version": 1, "source": {"presentationId": pres.get("presentationId"), "title": pres.get("title"),
                                     "revisionId": pres.get("revisionId")},
            "page_size": [page_w, page_h], "scale": scale, "slides": slides}


def fold_groups(elements: list[dict], lines: dict[str, int]) -> list[dict]:
    """Groups emit builds from one IR element, read back as that element: a number text box
    centred on a picture (a numbered ball) becomes the picture's `number`, and a group of node
    shapes joined by lines becomes a `diagram` with its nodes (texts and fills)."""
    drop: set[int] = set()
    for img in (e for e in elements if e["kind"] == "image"):
        x0, y0, x1, y1 = img["bbox"]
        for k, t in enumerate(elements):
            if t["kind"] != "text" or t.get("group") != img.get("group") or k in drop or len(t["paragraphs"]) != 1:
                continue
            text = "".join(r["text"] for r in t["paragraphs"][0]["runs"]).strip()
            cx, cy = (t["bbox"][0] + t["bbox"][2]) / 2, (t["bbox"][1] + t["bbox"][3]) / 2
            if 0 < len(text) <= 3 and x0 <= cx <= x1 and y0 <= cy <= y1:
                img["number"] = {"text": text}
                img["role"] = "icon"
                drop.add(k)
                break
    out = [e for k, e in enumerate(elements) if k not in drop]
    groups: dict[str, list[dict]] = {}
    for e in out:
        if e.get("group"):
            groups.setdefault(e["group"], []).append(e)
    for gid, members in groups.items():
        nodes = [e for e in members if e["kind"] == "shape" or (e["kind"] == "text" and e.get("shape_type") not in (None, "TEXT_BOX"))]
        if not lines.get(gid) or len(nodes) < 2 or any(e["kind"] == "image" for e in members):
            continue
        dg = {"kind": "diagram", "role": "figure", "group": gid, "id": gid, "object": gid, "key": None,
              "bbox": [min(e["bbox"][0] for e in members), min(e["bbox"][1] for e in members),
                       max(e["bbox"][2] for e in members), max(e["bbox"][3] for e in members)],
              "nodes": [{"bbox": e["bbox"], "fill": e.get("fill"), "shape": e.get("shape_type") or e.get("shape"),
                         "paragraphs": [p["runs"] for p in e.get("paragraphs", [])]} for e in members]}
        first = out.index(members[0])
        out = [e for e in out if e.get("group") != gid]
        out.insert(min(first, len(out)), dg)
    return out


def line_element(pe: dict, m: list[float], scale: float, scheme: dict) -> dict | None:
    """A connector, as the two points it runs between. Slides stores a line as the unit segment
    (0,0)-(w,h) under the element's transform, so a line drawn up and to the left comes back as a
    box with a negative scale - which a bounding box alone cannot tell from one drawn down and to
    the right. Only `adopt` asks for these (`deck_ir(foreign=True)`)."""
    w, h = dim(pe.get("size", {}).get("width")), dim(pe.get("size", {}).get("height"))
    props = pe["line"].get("lineProperties", {})
    if props.get("lineFill", {}).get("solidFill") is None:
        return None
    x0, y0 = m[0] * 0 + m[1] * 0 + m[2], m[3] * 0 + m[4] * 0 + m[5]
    x1, y1 = m[0] * w + m[1] * h + m[2], m[3] * w + m[4] * h + m[5]
    ends = pe["line"].get("lineProperties", {})
    return {"kind": "shape", "role": "line", "shape": "line",
            "bbox": [round(v / scale, 2) for v in (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))],
            "from": [round(x0 / scale, 2), round(y0 / scale, 2)],
            "to": [round(x1 / scale, 2), round(y1 / scale, 2)],
            "outline": rgb_hex(props["lineFill"]["solidFill"].get("color"), scheme),
            "weight": round((dim(props.get("weight")) or 0.75) / scale, 3),
            "arrow": ends.get("endArrow") not in (None, "NONE"),
            "arrow_start": ends.get("startArrow") not in (None, "NONE"), "fill": None,
            # what the heads are, how the connector runs (elbow and curved connectors are drawn in
            # their own frame), its dashes and its transparency
            "start_arrow": ends.get("startArrow", "NONE"), "end_arrow": ends.get("endArrow", "NONE"),
            "line_type": pe["line"].get("lineType"), "category": pe["line"].get("lineCategory"),
            "frame": frame(m, w, h, scale),
            **({"dash": props["dashStyle"]} if props.get("dashStyle", "SOLID") != "SOLID" else {}),
            **({"outline_alpha": round(props["lineFill"]["solidFill"]["alpha"], 4)}
               if props["lineFill"]["solidFill"].get("alpha", 1.0) < 1.0 else {})}


def unread_fill(fill: dict | None) -> bool:
    """A fill Slides draws that the API does not describe: `{}` with no `propertyState` (a gradient,
    picture or texture fill - the API only has words for a solid one). `deck_fills` reads it back
    from the slide's thumbnail when there is one."""
    return fill is not None and fill.get("propertyState", "RENDERED") == "RENDERED" and "solidFill" not in fill


def foreign_shape(pe: dict, m: list[float], w: float, h: float, bbox: list[float], fill_hex: str | None,
                  resolver: StyleResolver, fonts: FontMapper, scale: float, page_w: float) -> dict | None:
    """A shape of a deck nobody converted, as adopt draws it: its preset (`shape_type`; a freeform,
    which the API gives no geometry for, is CUSTOM), fill and outline with their transparency, dashes
    and weight, and - when it is turned, mirrored or sheared - its own `frame`, which is what a turned
    text box's words are laid out in (`bbox` stays the bounding box on the page, where the ink is).

    A text box's own fill and outline are read too: `pull` leaves them out because the converter
    never writes one, but a person's text box on a coloured panel is that panel."""
    shape = pe["shape"]
    props = shape.get("shapeProperties", {})
    kind = shape.get("shapeType") or "CUSTOM"
    upright = m
    if turned(m):
        # the words are laid out in the box the element would have if it were not turned
        fr = frame(m, w, h, 1.0)
        x0, y0 = fr["box"][:2]
        upright = [fr["size"][0] / w if w else 1.0, 0.0, x0, 0.0, fr["size"][1] / h if h else 1.0, y0]
    solid = props.get("shapeBackgroundFill", {}).get("solidFill", {})
    if solid.get("alpha", 1.0) <= 0.004:
        fill_hex = None  # a fill at alpha 0 draws nothing (sc-memphis' rings: black at alpha 0)
    style: dict = {"fill": fill_hex}
    if unread_fill(props.get("shapeBackgroundFill")) and not shape.get("placeholder"):
        style["fill_unread"] = True      # drawn, but not as anything the API says: `deck_fills`
    if fill_hex and solid.get("alpha", 1.0) < 1.0:
        style["fill_alpha"] = round(solid["alpha"], 4)
    style.update(outline_props(props.get("outline", {}), scale, resolver.scheme))
    if turned(m):
        style["frame"] = frame(m, w, h, scale)
    el = text_element(pe, upright, resolver, fonts, scale, page_w, True) if shape.get("text") else None
    if el is not None:
        # A node with a label is one page element: without its outline here, adopt would write the
        # words of a flow chart and none of the boxes around them.
        el.update({k: v for k, v in style.items() if k != "outline"}, shape_type=kind,
                  outline_color=style["outline"], bbox=bbox)
        if not el["fill"]:
            del el["fill"]
        return el
    if shape.get("placeholder") or not (style["fill"] or style["outline"] or style.get("fill_unread")):
        return None
    return {"kind": "shape", "role": "panel", "bbox": bbox, "shape": kind.lower(), "shape_type": kind, **style}


def element_of(pe: dict, m: list[float], resolver: StyleResolver, fonts: FontMapper, scale: float, page_w: float,
               fetch, images: Path | None, foreign: bool = False) -> dict | None:
    if "size" not in pe and "line" not in pe:
        return None
    w, h = dim(pe.get("size", {}).get("width")), dim(pe.get("size", {}).get("height"))
    bbox = [round(v / scale, 2) for v in box(m, w, h)]
    if "shape" in pe:
        shape = pe["shape"]
        props = shape.get("shapeProperties", {})
        fill = props.get("shapeBackgroundFill", {})
        fill_hex = rgb_hex(fill.get("solidFill", {}).get("color"), resolver.scheme) \
            if fill.get("propertyState", "RENDERED") == "RENDERED" and "solidFill" in fill else None
        if foreign:
            return foreign_shape(pe, m, w, h, bbox, fill_hex, resolver, fonts, scale, page_w)
        el = text_element(pe, m, resolver, fonts, scale, page_w, foreign) if shape.get("text") else None
        if el is not None:
            el["shape_type"] = shape.get("shapeType")
            if fill_hex and shape.get("shapeType") != "TEXT_BOX":
                el["fill"] = fill_hex
            return el
        if shape.get("shapeType") == "TEXT_BOX" or shape.get("placeholder"):
            return None
        outline = props.get("outline", {})
        stroke = rgb_hex(outline.get("outlineFill", {}).get("solidFill", {}).get("color"), resolver.scheme) \
            if outline.get("propertyState", "RENDERED") == "RENDERED" else None
        if not fill_hex and not stroke:
            return None
        return {"kind": "shape", "role": "panel", "bbox": bbox, "shape": shape.get("shapeType", "RECTANGLE").lower(),
                "fill": fill_hex, "outline": stroke,
                "weight": round(dim(outline.get("weight")) / scale, 2) or None}
    if "image" in pe:
        el = {"kind": "image", "role": "figure", "bbox": bbox, "alt": pe.get("description")}
        el.update(picture_props(pe, m, w, h, scale, resolver.scheme))
        url = pe["image"].get("contentUrl")
        source = pe["image"].get("sourceUrl")
        if source and "googleusercontent.com" not in source:
            el["source_url"] = source  # inserted by URL: maybe a bigger original than Google keeps
        if url and fetch and images is not None:
            el.update(stash_picture(url, fetch, images))
        return el
    if "table" in pe:
        return table_element(pe, m, resolver, fonts, scale, foreign)
    if "line" in pe:
        return line_element(pe, m, scale, resolver.scheme) if foreign else None
    if foreign:
        return media_element(pe, m, w, h, bbox, resolver, scale, fetch, images)
    return None


YOUTUBE_THUMB = "https://img.youtube.com/vi/{id}/hqdefault.jpg"


def media_outline(props: dict, scale: float, scheme: dict) -> dict | None:
    """A chart's or video's outline, in the shape `picture_props` gives a picture's."""
    outline = (props or {}).get("outline") or {}
    if not outline or outline.get("propertyState", "RENDERED") != "RENDERED":
        return None
    colour = rgb_hex(outline.get("outlineFill", {}).get("solidFill", {}).get("color"), scheme)
    if colour is None:
        return None
    return {"color": colour, "weight": round(dim(outline.get("weight")) / scale, 3),
            "dash": outline.get("dashStyle", "SOLID")}


def media_element(pe: dict, m: list[float], w: float, h: float, bbox: list[float], resolver: StyleResolver,
                  scale: float, fetch, images: Path | None) -> dict | None:
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
    props: dict = picture_props({}, m, w, h, scale, resolver.scheme)
    alt = pe.get("description") or pe.get("title")
    if "sheetsChart" in pe:
        chart = pe["sheetsChart"]
        el = {"kind": "image", "role": "figure", "bbox": bbox, "alt": alt or "chart", **props,
              "chart": {"spreadsheetId": chart.get("spreadsheetId"), "chartId": chart.get("chartId")}}
        outline = media_outline(chart.get("sheetsChartProperties", {}).get("chartImageProperties"), scale,
                                resolver.scheme)
        if outline:
            el["outline"] = outline
        if chart.get("contentUrl") and fetch and images is not None:
            el.update(stash_picture(chart["contentUrl"], fetch, images))
        return el
    if "video" in pe:
        video = pe["video"]
        source, vid = video.get("source"), video.get("id")
        url = video.get("url") or (f"https://www.youtube.com/watch?v={vid}" if source == "YOUTUBE" and vid else None)
        el = {"kind": "image", "role": "figure", "bbox": bbox, "alt": alt or "video", **props,
              "video": {"source": source, "id": vid, "url": url,
                        "start": video.get("videoProperties", {}).get("start"),
                        "end": video.get("videoProperties", {}).get("end")}}
        outline = media_outline(video.get("videoProperties"), scale, resolver.scheme)
        if outline:
            el["outline"] = outline
        if source == "YOUTUBE" and vid and fetch and images is not None:
            el.update(stash_picture(YOUTUBE_THUMB.format(id=vid), fetch, images))
        return el
    if "wordArt" in pe:
        text = (pe["wordArt"].get("renderedText") or "").replace("\u000b", "\n").strip("\n")
        if not text.strip():
            return None
        lines = [t for t in text.split("\n")] or [text]
        bh = props["box"][3] - props["box"][1]
        size = round(max(bh / len(lines) * 0.8, 1.0), 2)       # what it is stretched to decides nothing
        run = {"text": "", "font": "", "family": "sans", "size": size, "bold": False, "italic": False,
               "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False,
               "strike": False, "highlight": None}
        paras = [{"align": "center", "level": 0, "bullet": None, "size": size, "text_x0": props["box"][0],
                  "tab_x0": None, "lines": [{"baseline": None, "x0": props["box"][0], "x1": props["box"][2]}],
                  "runs": [{**run, "text": t or " "}]} for t in lines]
        return {"kind": "text", "role": "body", "bbox": bbox, "wordart": True, "shape_type": "WORD_ART",
                "box": props["box"], **({"rotation": props["rotation"]} if "rotation" in props else {}),
                "anchor": [round((props["box"][0] + props["box"][2]) / 2, 2), props["box"][3]],
                "wrap_width": round(props["box"][2] - props["box"][0], 2), "placeholder": None,
                "paragraphs": paras}
    return None


VALIGN = {"TOP": "top", "MIDDLE": "middle", "BOTTOM": "bottom"}
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


def cell_pad(pe: dict) -> tuple[float, float]:
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
    widths = [dim(c.get("columnWidth")) for c in pe["table"].get("tableColumns", [])]
    room = []
    for row in pe["table"].get("tableRows", []):
        z = 0.0
        for cell in row.get("tableCells", []):
            size, text = 0.0, ""
            for te in cell.get("text", {}).get("textElements", []):
                if "textRun" in te:
                    text += te["textRun"].get("content", "")
                    if te["textRun"].get("content", "").strip():
                        size = max(size, dim(te["textRun"].get("style", {}).get("fontSize")))
            z = max(z, size)
            if native and size and cell.get("rowSpan", 1) == 1:
                col = cell.get("location", {}).get("columnIndex", 0)
                span = sum(widths[col:col + cell.get("columnSpan", 1)])
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
    # Across it is a little more: journey-maps' header (rows say 2.5 pt) keeps "Channel" whole in
    # 46.9 pt but breaks "custom|ers" in 41.1 and "Succes|s" in 43.9, which puts the inset between
    # 3.7 and 5.7 pt; its first column's ink starts 4.9 pt in.
    return min(7.2, pad_y + 2.2), pad_y


def table_element(pe: dict, m: list[float], resolver: StyleResolver, fonts: FontMapper, scale: float,
                  foreign: bool = False) -> dict:
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
    t = pe["table"]
    rows = []
    for row in t.get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            text = "".join(te.get("textRun", {}).get("content", "")
                           for te in cell.get("text", {}).get("textElements", []))
            cells.append(" ".join(text.split()))
        rows.append(cells)
    widths = [dim(c.get("columnWidth")) * m[0] for c in t.get("tableColumns", [])]
    heights = [dim(r.get("rowHeight")) * m[4] for r in t.get("tableRows", [])]
    x0, y0 = m[2], m[5]
    if widths and heights:
        bbox = [round(v / scale, 2) for v in (x0, y0, x0 + sum(widths), y0 + sum(heights))]
    else:
        w, h = dim(pe.get("size", {}).get("width")), dim(pe.get("size", {}).get("height"))
        bbox = [round(v / scale, 2) for v in box(m, w, h)]
    el = {"kind": "table", "role": "table", "bbox": bbox, "rows": rows}
    if not foreign:
        return el
    el["cell_pad"] = [round(v / scale, 3) for v in cell_pad(pe)]
    el["col_widths"] = [round(w / scale, 3) for w in widths]
    el["row_heights"] = [round(h / scale, 3) for h in heights]
    cells_out = []
    for row in t.get("tableRows", []):
        for cell in row.get("tableCells", []):
            loc = cell.get("location", {})
            props = cell.get("tableCellProperties", {})
            fill = props.get("tableCellBackgroundFill", {})
            solid = fill.get("solidFill") if fill.get("propertyState", "RENDERED") == "RENDERED" else None
            paras = text_paragraphs(pe, cell.get("text", {}), resolver, fonts, scale, keep_blank=True)
            cells_out.append({
                "row": loc.get("rowIndex", 0), "col": loc.get("columnIndex", 0),
                "rowspan": cell.get("rowSpan", 1), "colspan": cell.get("columnSpan", 1),
                "fill": rgb_hex(solid.get("color"), resolver.scheme) if solid else None,
                "fill_alpha": round(solid.get("alpha", 1.0), 3) if solid else None,
                "valign": VALIGN.get(props.get("contentAlignment"), "top"),
                "paragraphs": [{"align": p["align"], "level": p["level"], "bullet": p["bullet"], "size": p["size"],
                                "line_spacing": p["line_spacing"],
                                "runs": [{k: v for k, v in r.items() if k not in ("slides_font", "slides_size")}
                                         for r in p["runs"]]}
                               for p in paras if p["runs"]]})
            if not solid:
                # A .pptx table style colours cells the API reports NOT_RENDERED (comps-analysis,
                # creandum-board): the style is nowhere in the answer, so `deck_fills` asks the
                # thumbnail, and a cell that is truly empty shows the page and stays unfilled.
                cells_out[-1]["fill_unread"] = True
    el["table_cells"] = cells_out
    borders = []
    for direction, key in (("h", "horizontalBorderRows"), ("v", "verticalBorderRows")):
        for i, brow in enumerate(t.get(key, [])):
            for bc in brow.get("tableBorderCells", []):
                loc = bc.get("location", {})
                props = bc.get("tableBorderProperties", {})
                solid = props.get("tableBorderFill", {}).get("solidFill")
                weight = dim(props.get("weight"))
                if not solid or solid.get("alpha", 1.0) <= 0 or weight <= 0:
                    continue
                borders.append({"dir": direction, "row": loc.get("rowIndex", i), "col": loc.get("columnIndex", 0),
                                "color": rgb_hex(solid.get("color"), resolver.scheme),
                                "alpha": round(solid.get("alpha", 1.0), 3),
                                "weight": round(weight / scale, 3), "dash": props.get("dashStyle", "SOLID")})
    el["table_borders"] = borders
    return el


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


def picture_props(pe: dict, m: list[float], w: float, h: float, scale: float, scheme: dict) -> dict:
    """What Slides keeps as properties of a picture, in PDF pt: `box` (the unrotated frame around
    the centre), `rotation` (degrees clockwise), `flip` (mirrored), `crop` (fractions cut off the
    picture file), `opacity`, `brightness`/`contrast` (-1..1), `recolor` (gradient stops) and
    `outline` (colour, weight). Only what differs from an untouched picture is set."""
    a, b, _, d, e, _ = m
    cx, cy = m[0] * w / 2 + m[1] * h / 2 + m[2], m[3] * w / 2 + m[4] * h / 2 + m[5]
    bw, bh = math.hypot(a, d) * w, math.hypot(b, e) * h
    out: dict = {"box": [round(v / scale, 2) for v in (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)]}
    rot = math.degrees(math.atan2(d, a))
    if a * e - b * d < 0:  # R(rot)·flip(y) = R(rot + 180)·flip(x)
        out["flip"] = True
        rot += 180
    rot = (rot + 180) % 360 - 180
    if abs(rot) > 0.05:
        out["rotation"] = round(rot, 2)
    ip = (pe.get("image") or {}).get("imageProperties", {})
    crop = ip.get("cropProperties") or {}
    c = {k: round(crop.get(f"{name}Offset", 0.0), 5) for k, name in (("l", "left"), ("t", "top"), ("r", "right"), ("b", "bottom"))}
    if any(abs(v) > 1e-4 for v in c.values()):
        out["crop"] = c
    if crop.get("angle"):
        out["crop_angle"] = round(math.degrees(crop["angle"]), 2)
    if ip.get("transparency"):
        out["opacity"] = round(1 - ip["transparency"], 4)
    for key in ("brightness", "contrast"):
        if ip.get(key):
            out[key] = round(ip[key], 4)
    recolor = ip.get("recolor")
    if recolor:
        out["recolor"] = {"name": recolor.get("name"), "stops": [
            {"color": rgb_hex(s.get("color"), scheme) or "#000000", "alpha": s.get("alpha", 1.0),
             "position": s.get("position", 0.0)} for s in recolor.get("recolorStops", [])]}
    outline = ip.get("outline") or {}
    if outline and outline.get("propertyState", "RENDERED") == "RENDERED":
        colour = rgb_hex(outline.get("outlineFill", {}).get("solidFill", {}).get("color"), scheme)
        out["outline"] = {"color": colour or "#000000", "weight": round(dim(outline.get("weight")) / scale, 3),
                          "dash": outline.get("dashStyle", "SOLID")}
    return out


def fetch_url(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except OSError:
            if attempt == 3:
                raise
    return b""


def presentation_id(ref: str) -> str:
    """A deck URL, id or converted output folder (emit.json) -> presentation id."""
    p = Path(ref)
    if p.is_dir() and (p / "emit.json").exists():
        return json.loads((p / "emit.json").read_text(encoding="utf-8"))["presentationId"]
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def read_deck(ref: str, images: Path | None = None, base: dict | None = None, pdf_size: list[float] | None = None,
              slides=None, foreign: bool = False) -> dict:
    """Fetch a live deck and return its IR (pictures downloaded into `images`). `foreign`: read it as
    a deck nobody converted (`deck_ir`) - what `adopt` asks for and `pull` does not."""
    from .google_auth import slides_service
    from .gslides import execute
    slides = slides or slides_service()
    pid = presentation_id(ref)
    pres = execute(slides.presentations().get(presentationId=pid))
    p = Path(ref)
    if pdf_size is None and p.is_dir() and (p / "deck.json").exists():
        pdf_size = json.loads((p / "deck.json").read_text(encoding="utf-8"))["slides"][0]["size"]
    if base is None and p.is_dir() and (p / "sync" / "base.json").exists():
        base = json.loads((p / "sync" / "base.json").read_text(encoding="utf-8"))
    return deck_ir(pres, pdf_size or (base or {}).get("page_size"), base, fetch_url if images else None, images,
                   foreign)
