"""Stage 4: build the Google Slides deck from deck.json and the background images."""

import hashlib
import io
import json
import math
from collections import Counter
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .classify import HOLE_PAD
from .fonts import font_info, google_font
from .google_auth import drive_service, slides_service
from .gslides import EMU_PER_PT, emu, execute, pt

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = ROOT / "calibration" / "fonts.json"
SLIDE_W = 720.0
BATCH_MAX_REQUESTS = 400  # slides are sent together until a batch reaches this size
PPTX_MIME ="application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Slides text box model, measured by tools/calibrate.py and the spacing probes
# (docs/calibration.md).
BASELINE_A = 6.48    # box top -> first baseline = A + ASCENT_EM * size
ASCENT_EM = 0.968
LINE_EM = 1.2        # baseline pitch at lineSpacing 100 (before pixel snapping)
PX_PT = 0.75         # Slides snaps line pitches to whole CSS pixels
DESCENT_EM = LINE_EM - ASCENT_EM
PAD_X = 6.7          # box edge -> text start
BULLET_GAP = 1.9     # bullet glyph's right edge sits this far before indentFirstLine
PPTX_TITLE_DY = 3.9  # title placeholders of pptx-imported decks have a smaller top inset
MIDDLE_BASELINE_EM = ASCENT_EM - LINE_EM / 2  # contentAlignment MIDDLE: baseline below the box middle (tools/probe_middle.py: 0.362)
SOFT_BREAK = chr(11)  # vertical tab: a line break inside a paragraph
SMALL_CAPS_LINE = 0.9  # a line of only smallCaps text is laid out as if 90% of its size

FONT_FOR_FAMILY = {"sans": "Lato", "serif": "PT Serif", "mono": "Roboto Mono"}
# Advance widths in Slides text (Lato and its fallback fonts), measured by tools/probe_symbols.py.
SYMBOL_ADVANCE_EM = {
    "=": 0.58, "+": 0.58, "−": 0.58, "<": 0.58, ">": 0.58, "≤": 0.58, "≥": 0.58, "×": 0.58, "·": 0.271,
    "/": 0.313, "∑": 0.682, "∏": 0.682, "∫": 0.397, "∈": 0.984, "∉": 0.492, "⊂": 0.981, "⊆": 0.981,
    "∪": 0.981, "∩": 0.717, "→": 0.998, "←": 0.998, "⇒": 0.981, "⇔": 0.981, "≈": 0.577, "≠": 0.577,
    "±": 0.577, "∞": 0.682, "ℝ": 0.633, "ℕ": 0.65, "ℤ": 0.534, "ℚ": 0.65, "ℂ": 0.65, "α": 0.573,
    "β": 0.57, "γ": 0.496, "δ": 0.552, "ε": 0.443, "θ": 0.552, "λ": 0.496, "μ": 0.573, "π": 0.615,
    "σ": 0.612, "φ": 0.643, "ω": 0.777, "Δ": 0.664, "Σ": 0.615, "Ω": 0.742, "∂": 0.577, "∇": 0.981,
    "′": 0.186, "∀": 0.981, "∃": 0.981, "∧": 0.981, "∨": 0.981, "⊥": 0.981, "∥": 0.981, "∘": 0.489,
    "…": 0.724, " ": 0.19,
}
MATH_SPACE_EM = 0.278  # TeX's \thickmuskip (5 mu) around relations
CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM = 0.525, 0.6

BULLET_PRESETS = {
    "number": "NUMBERED_DIGIT_ALPHA_ROMAN",
    "number_parens": "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS",
}
# A preset's three glyphs are those of nesting levels 0, 1, 2; deeper levels repeat ● ○ ■
# whatever the preset. A bullet shape is a preset plus the level showing it (bullet_level).
# Ink height and the gap between ink and indentFirstLine are per em of the bullet's size
# (tools/probe_bullets.py).
BULLET_SHAPES = {  # shape: (preset, level, ink height em, gap em)
    "disc": ("BULLET_DISC_CIRCLE_SQUARE", 0, 0.413, 0.08),
    "circle": ("BULLET_DISC_CIRCLE_SQUARE", 1, 0.43, 0.08),
    "square": ("BULLET_DISC_CIRCLE_SQUARE", 2, 0.45, 0.07),
    "open_square": ("BULLET_CHECKBOX", 0, 0.69, 0.155),  # ❏
    "triangle": ("BULLET_ARROW3D_CIRCLE_SQUARE", 0, 0.525, 0.07),  # ➢: presets have no ▶
    "star": ("BULLET_STAR_CIRCLE_SQUARE", 0, 0.81, 0.07),
    "diamond": ("BULLET_DIAMOND_CIRCLE_SQUARE", 0, 0.81, 0.08),
    "open_diamond": ("BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE", 1, 0.87, 0.06),
}
GLYPH_SHAPES = {**dict.fromkeys("▶►▸‣", "triangle"), **dict.fromkeys("•●", "disc"), **dict.fromkeys("◦○", "circle"),
                **dict.fromkeys("■▪", "square"), "□": "open_square", **dict.fromkeys("★⋆", "star"),
                **dict.fromkeys("◆♦", "diamond"), **dict.fromkeys("◇⋄", "open_diamond")}


# Width per em of Computer Modern's optical sizes relative to the 10 pt cut, from the glyph
# advances of the Type 1 fonts (cmss8.pfb ... cmss17.pfb) over a sample sentence. EC and
# Latin Modern share these metrics.
DESIGN_WIDTH = {
    "sans": {8: 1.0623, 9: 1.0273, 10: 1.0, 12: 0.9753, 17: 0.9377},
    "serif": {5: 1.3758, 6: 1.2291, 7: 1.1424, 8: 1.0629, 9: 1.0277, 10: 1.0, 12: 0.9786, 17: 0.9136},
    "mono": {8: 1.0114, 9: 1.0, 10: 1.0, 12: 0.979},
}


def design_width(table: dict[int, float], design: float) -> float:
    keys = sorted(table)
    if design <= keys[0]:
        return table[keys[0]]
    if design >= keys[-1]:
        return table[keys[-1]]
    hi = next(k for k in keys if k >= design)
    lo = max(k for k in keys if k <= design)
    if hi == lo:
        return table[lo]
    t = (design - lo) / (hi - lo)
    return table[lo] + t * (table[hi] - table[lo])


class FontMapper:
    """TeX font + size -> Slides font family + size with calibrated width correction."""

    def __init__(self):
        self.factors = {}  # family -> (running text factor, title factor)
        self.style = {}    # family -> {"bold": ratio, "italic": ratio} relative to running text
        for family, path in (("sans", CALIBRATION), ("serif", CALIBRATION.with_name("fonts_serif.json"))):
            cal = json.loads(path.read_text(encoding="utf-8"))["fonts"]
            ratios = cal[FONT_FOR_FAMILY[family]]["width_ratio"]
            self.factors[family] = (ratios["text_mean"], ratios["by_row"]["title"])
            self.style[family] = {k: ratios["relative_to_text"][k] for k in ("bold", "italic")}

    def text_style(self, run: dict, scale: float) -> tuple[dict, list[str]]:
        """Font part of a Slides TextStyle. Google fonts used by the PDF itself keep their
        family and weight (e.g. Fira Sans Light); TeX fonts get a calibrated substitute."""
        family, size = self(run, scale)
        google = google_font(run["font"])
        if google:
            return ({"weightedFontFamily": {"fontFamily": google[0], "weight": google[1]},
                     "fontSize": pt(size), "italic": google[2] or run["italic"]},
                    ["weightedFontFamily", "fontSize", "italic"])
        return ({"fontFamily": family, "fontSize": pt(size), "bold": run["bold"], "italic": run["italic"]},
                ["fontFamily", "fontSize", "bold", "italic"])

    def width_ratio(self, font: str, family: str, bold: bool, italic: bool) -> float:
        """Expected Slides width / PDF width of a run after the size correction: bold and
        italic are only half corrected (see __call__)."""
        if google_font(font) or family not in self.style:
            return 1.0
        ratio = 1.0
        for key, on in (("bold", bold), ("italic", italic)):
            if on:
                rel = self.style[family][key]
                ratio *= rel / (1 + (rel - 1) / 2)
        return ratio

    def __call__(self, run: dict, scale: float) -> tuple[str, float]:
        google = google_font(run["font"])
        if google:  # same font in Slides: no width correction
            return google[0], round(run["size"] * scale, 1)
        info = font_info(run["font"])
        family = FONT_FOR_FAMILY.get(run["family"], "Lato")
        design = info.design_size or 10
        if run["family"] == "mono":
            factor = ROBOTO_MONO_ADVANCE_EM / (CMTT_ADVANCE_EM * design_width(DESIGN_WIDTH["mono"], design))
        else:
            text, title = self.factors.get(run["family"], self.factors["sans"])
            if run["family"] != "serif" and 11.5 <= design < 14:
                factor = title  # calibrated directly on CMSS12 titles
            else:
                # Other optical sizes: CM's small cuts are wider per em, its large ones narrower.
                factor = text / design_width(DESIGN_WIDTH.get(run["family"], DESIGN_WIDTH["sans"]), design)
            # Bold and italic substitutes run 4-8% narrower than CM's; correct half of that, so
            # widths come closer without emphasised words looking visibly larger.
            style = self.style.get(run["family"], self.style["sans"])
            for key in ("bold", "italic"):
                if run[key]:
                    factor *= 1 + (style[key] - 1) / 2
        return family, round(run["size"] * scale / factor, 1)


def bullet_shape(bullet: dict) -> str | None:
    """The BULLET_SHAPES entry for a bullet; None for numbers."""
    text = bullet.get("text", "")
    if bullet["kind"] == "number" or (bullet["kind"] == "image" and text.isdigit()):
        return None
    if bullet["kind"] == "glyph":
        return GLYPH_SHAPES.get(text, "disc")
    return bullet.get("shape") if bullet.get("shape") in BULLET_SHAPES else "disc"


def bullet_preset(bullet: dict) -> str:
    shape = bullet_shape(bullet)
    if shape is None:
        return BULLET_PRESETS["number_parens" if ")" in bullet.get("text", "") else "number"]
    return BULLET_SHAPES[shape][0]


def bullet_level(bullet: dict, level: int) -> int:
    """Slides nesting level: numbers count by depth (1., a., i.), glyphs pick their shape.
    ● ○ ■ keep the depth (they repeat every 3 levels); other glyphs exist at one level only.
    (Indents are set explicitly, so the level only decides the glyph and what Tab does.)"""
    shape = bullet_shape(bullet)
    if shape is None:
        return level
    preset, first = BULLET_SHAPES[shape][:2]
    return 3 * min(level, 2) + first if preset == "BULLET_DISC_CIRCLE_SQUARE" else first


def bullet_size(bullet: dict, size: float, scale: float) -> float:
    """Font size giving the bullet its PDF height (at most the text's: a larger bullet would
    push the line down). Glyph and number boxes are font boxes: their size is the font's."""
    shape = bullet_shape(bullet)
    if bullet["kind"] in ("glyph", "number") or shape is None:
        label = bullet.get("label") or {}
        return round(min(size, label.get("size", size / scale) * scale), 1)
    height = (bullet["bbox"][3] - bullet["bbox"][1]) * scale
    return round(max(0.3 * size, min(size, height / BULLET_SHAPES[shape][2])), 1)


def bullet_gap(bullet: dict, size: float) -> float:
    """Distance from the bullet box's right edge to indentFirstLine."""
    shape = bullet_shape(bullet)
    if bullet["kind"] in ("glyph", "number") or shape is None:
        return BULLET_GAP
    return BULLET_SHAPES[shape][3] * size


def rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"opaqueColor": {"rgbColor": {k: int(h[i:i + 2], 16) / 255 for k, i in
                                         (("red", 0), ("green", 2), ("blue", 4))}}}


# ---------------------------------------------------------------- text boxes

def extra_below(r: float, z: float) -> float:
    """Extra space lineSpacing r adds under a line of size z (negative when r < 1)."""
    return (r - 1) * LINE_EM * z if r >= 1 else -(1 - r) * 0.25 * LINE_EM * z


def extra_above(r: float, z: float) -> float:
    """lineSpacing >= 100% never moves a line down; below 100% it pulls the baseline up."""
    return 0.0 if r >= 1 else -(1 - r) * 0.75 * LINE_EM * z


def snap(v: float) -> float:
    """Slides lays lines out on whole CSS pixels (0.75 pt)."""
    return round(v / PX_PT) * PX_PT


def line_pitch(z: float, r: float) -> float:
    """Baseline distance between wrapped lines of one paragraph."""
    return snap(LINE_EM * z * r)


def pitch_between(z1: float, r1: float, z2: float, r2: float) -> float:
    """Baseline distance from the last line of one paragraph to the first line of the next."""
    return snap(DESCENT_EM * z1 + ASCENT_EM * z2 + extra_below(r1, z1) + extra_above(r2, z2))


def solve_increasing(f, target: float, lo: float = 0.5, hi: float = 3.0) -> float:
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return (lo + hi) / 2


HOLE_FONT, HOLE_SPACE_EM = "Roboto Mono", 0.6  # monospaced: a space is exactly 0.6 em


def hole_run(run: dict, scale: float, fonts: FontMapper) -> dict:
    """The gap under an inline formula picture (and the word space after it): no-break spaces
    in a monospaced font, sized so they are exactly as wide as the formula and no taller than
    the line."""
    z = fonts(run, scale)[1]
    width = run["hole"] * scale
    n = max(1, math.ceil(width / (HOLE_SPACE_EM * z)))
    return {**run, "text": " " * n, "hole_size": round(width / (HOLE_SPACE_EM * n), 2)}


def vertical_layout(paras: list[dict], baselines: list[list[float]], sizes: list[float]):
    """lineSpacing ratio and spaceAbove per paragraph so Slides baselines land on the PDF's.

    Slides ignores spaceAbove/spaceBelow between items of a bulleted list, so there the gap
    to the next item has to come from the item's own lineSpacing; for a wrapped item one
    ratio covers its inner lines plus that gap, spreading the difference evenly.

    Pitches snap to whole pixels, so each paragraph aims at the original position measured
    from where Slides will actually have put the previous one: rounding errors don't add up."""
    estimate = None
    for _ in range(4):  # a paragraph's ratio depends on the next one's (below 100% it moves up)
        ratios, space_above = _vertical_pass(paras, baselines, sizes, estimate)
        if ratios == estimate:
            break
        estimate = ratios
    return ratios, space_above


def _vertical_pass(paras, baselines, sizes, estimate):
    ratios: list[float] = []
    space_above = [0.0] * len(paras)
    pulled: dict[int, float] = {}  # paragraph -> lineSpacing < 1 that pulls it up to its target
    first = baselines[0][0]  # predicted Slides baseline of the current paragraph's first line
    for i, (p, bl, z) in enumerate(zip(paras, baselines, sizes)):
        n = len(bl)
        has_next = i + 1 < len(paras)
        list_link = has_next and p["bullet"] and paras[i + 1]["bullet"]
        next_r = estimate[i + 1] if estimate and has_next else 1.0
        if list_link:
            target = baselines[i + 1][0] - first
            zn = sizes[i + 1]
            r = solve_increasing(lambda r: (n - 1) * LINE_EM * z * r + DESCENT_EM * z + ASCENT_EM * zn +
                                 extra_below(r, z) + extra_above(next_r, zn), target)
        elif n > 1:
            r = (bl[-1] - bl[0]) / (n - 1) / (LINE_EM * z)
        else:
            r = pulled.get(i, 1.0)
        r = round(min(3.0, max(0.5, r)), 4)
        ratios.append(r)
        last = first + (n - 1) * line_pitch(z, r)
        if has_next:
            natural = pitch_between(z, r, sizes[i + 1], next_r)
            if not list_link:
                gap = baselines[i + 1][0] - last - pitch_between(z, r, sizes[i + 1], 1.0)
                nxt = paras[i + 1]
                free = len(baselines[i + 1]) == 1 and not (nxt["bullet"] and i + 2 < len(paras) and paras[i + 2]["bullet"])
                if gap < -PX_PT and free:
                    # Tighter than Slides' natural pitch (block title right above its body):
                    # a lineSpacing below 100% moves the next single line up.
                    zn = sizes[i + 1]
                    rn = max(0.5, 1 + gap / (0.75 * LINE_EM * zn))
                    pulled[i + 1] = rn
                    natural = pitch_between(z, r, zn, rn)
                space_above[i + 1] = max(0.0, baselines[i + 1][0] - last - natural)
            first = last + natural + space_above[i + 1]
    return ratios, space_above


def text_box_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                      placeholder: dict | None = None, page_slide: dict[int, str] | None = None,
                      bar: list[float] | None = None, right_limit: float | None = None) -> list[dict]:
    """A text box for a text element. With `bar` (the PDF box of a block's title bar that this
    one-line text sits on) the box fills the bar and centres its text vertically, so the
    title stays in the middle of the bar when the block is resized. `right_limit` (PDF x) is
    how far a box of unwrapped left-aligned text may extend."""
    paras = [{**p, "runs": [hole_run(r, scale, fonts) if r.get("hole") else r for r in p["runs"]]}
             for p in el["paragraphs"]]
    # A line is as tall as its largest run; small caps runs count at their reduced size.
    base_sizes = [max(fonts(r, scale)[1] for r in p["runs"]) if p["runs"] else p["size"] * scale for p in paras]
    sizes = [max(fonts(r, scale)[1] * (SMALL_CAPS_LINE if r.get("smallcaps") else 1) for r in p["runs"])
             if p["runs"] else p["size"] * scale for p in paras]

    # (a centred or right-aligned paragraph's longest line can start left of its first line)
    left_pdf = min(p["bullet"]["bbox"][0] if p["bullet"] else
                   min([p["text_x0"]] + ([l["x0"] for l in p["lines"]] if p["align"] != "left" else [])) for p in paras)
    right_pdf = max(line["x1"] for p in paras for line in p["lines"])
    first_baseline = paras[0]["lines"][0]["baseline"] * scale
    last_baseline = paras[-1]["lines"][-1]["baseline"] * scale

    baselines = [[line["baseline"] * scale for line in p["lines"]] for p in paras]
    ratios, space_above = vertical_layout(paras, baselines, sizes)

    inner_w = (right_pdf - left_pdf) * scale
    # Titles carry their line breaks as soft breaks (SOFT_BREAK) and need no tight width.
    multiline = any(len(p["lines"]) > 1 and not any(SOFT_BREAK in r["text"] for r in p["runs"]) for p in paras)
    # Wrapped paragraphs need a width that breaks where TeX did: wide enough for the longest
    # line, narrower than where the next line's first word would fit. The middle of that range
    # tolerates the substitute font being a little wider or narrower. Single lines get room so
    # that a slightly wider font never wraps them.
    limits = [p["wrap_limit"] for p in paras if p.get("wrap_limit") and not any(SOFT_BREAK in r["text"] for r in p["runs"])]
    room = (min(limits) - right_pdf) * scale if limits else 0.0
    if not multiline:
        slack = max(0.15 * inner_w, 2 * max(sizes))
    elif room > 4:
        slack = room / 2
    else:
        slack = 2 + 0.01 * inner_w
    x = left_pdf * scale - PAD_X
    aligns = {p["align"] for p in paras}
    if aligns == {"center"}:
        x -= slack / 2
    elif aligns == {"right"}:
        x -= slack
    y = first_baseline - (BASELINE_A + ASCENT_EM * sizes[0] + extra_above(ratios[0], sizes[0]))
    w = inner_w + 2 * PAD_X + slack
    h = last_baseline - y + DESCENT_EM * sizes[-1] + extra_below(ratios[-1], sizes[-1]) + 4
    if right_limit and not multiline and aligns == {"left"} and not any(SOFT_BREAK in r["text"] for p in paras for r in p["runs"]):
        # Room up to the block edge or the next element: text typed later wraps where a user
        # expects, not a few points after the converted words.
        w = max(w, right_limit * scale - x)
    middle = bool(bar) and not placeholder and len(paras) == 1 and len(paras[0]["lines"]) == 1 and ratios[0] == 1
    if middle:
        h = (bar[3] - bar[1]) * scale
        y = first_baseline - MIDDLE_BASELINE_EM * sizes[0] - h / 2
        if aligns == {"left"}:
            w = max(w, (bar[2] - 1) * scale - x)  # to the bar's end: the title wraps with the block

    if placeholder:
        # An existing layout placeholder (the slide title): its size is fixed at creation, so
        # it is resized through the transform's scale. Text is not scaled by that.
        y += placeholder["dy"]
        reqs = [
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": w / placeholder["base_w"], "scaleY": h / placeholder["base_h"], "unit": "EMU",
                "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}},
            {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                       "shapeProperties": {"contentAlignment": "TOP",
                                                           "autofit": {"autofitType": "NONE"}}}},
        ]
    else:
        reqs = [{"createShape": {
            "objectId": object_id, "shapeType": "TEXT_BOX",
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(w), "height": emu(h)},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)},
            },
        }}]
        if middle:
            reqs.append({"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment",
                                                   "shapeProperties": {"contentAlignment": "MIDDLE"}}})

    texts =["".join(r["text"] for r in p["runs"]) for p in paras]
    # Bullets: contiguous ranges with the same preset. createParagraphBullets consumes the
    # leading tabs and sets nesting levels relative to the range's shallowest paragraph, so a
    # range whose levels start above 0 begins with a dummy paragraph, deleted right after.
    levels = [bullet_level(p["bullet"], p["level"]) if p["bullet"] else 0 for p in paras]
    ranges = []
    for i, p in enumerate(paras):
        preset = bullet_preset(p["bullet"]) if p["bullet"] else None
        if preset and ranges and ranges[-1][2] == preset and ranges[-1][1] == i - 1:
            ranges[-1][1] = i
        elif preset:
            ranges.append([i, i, preset])
    dummies = {first for first, last, _ in ranges if min(levels[first:last + 1]) > 0}
    starts_tabbed, parts, pos = [], [], 0
    for i, (p, t) in enumerate(zip(paras, texts)):
        if i in dummies:
            parts.append("-")
            pos += 2
        starts_tabbed.append(pos)
        parts.append("\t" * levels[i] + t)
        pos += levels[i] + len(t) + 1
    reqs.append({"insertText": {"objectId": object_id, "text": "\n".join(parts), "insertionIndex": 0}})
    # A bullet keeps the text style it was created with, unless a later style request covers
    # its whole paragraph. So every paragraph first gets its base family and size, bulleted
    # ones the bullet's size and colour, and the runs are styled below in parts.
    for i, (p, start, level, size) in enumerate(zip(paras, starts_tabbed, levels, base_sizes)):
        family = fonts(p["runs"][0], scale)[0] if p["runs"] else "Lato"
        length = level + len("".join(r["text"] for r in p["runs"]))
        style = {"fontFamily": family, "fontSize": pt(size)}
        if p["bullet"]:
            style["fontSize"] = pt(bullet_size(p["bullet"], size, scale))
            color = p["bullet"].get("color") or (p["runs"][0]["color"] if p["runs"] else None)
            if color:
                style["foregroundColor"] = rgb(color)
        if length:
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "fields": ",".join(style), "style": style,
                "textRange": {"type": "FIXED_RANGE", "startIndex": start - (2 if i in dummies else 0),
                              "endIndex": start + length},
            }})
    for first, last, preset in reversed(ranges):
        start = starts_tabbed[first] - (2 if first in dummies else 0)
        end = starts_tabbed[last] + levels[last] + len(texts[last])
        reqs.append({"createParagraphBullets": {
            "objectId": object_id, "bulletPreset": preset,
            "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end},
        }})
        if first in dummies:
            reqs.append({"deleteText": {"objectId": object_id,
                                        "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + 2}}})

    # From here on indices refer to the final text, without tabs.
    pos = 0
    for p, t, ratio, above, base in zip(paras, texts, ratios, space_above, base_sizes):
        p_start, p_end = pos, pos + len(t)
        pos = p_end + 1
        start = p_start
        for run in p["runs"]:
            if not run["text"]:
                continue
            style, fields = fonts.text_style(run, scale)
            if run.get("hole_size"):
                style, fields = {"fontFamily": HOLE_FONT, "fontSize": pt(run["hole_size"]), "bold": False,
                                 "italic": False}, ["fontFamily", "fontSize", "bold", "italic"]
            style.update({"smallCaps": run["smallcaps"], "foregroundColor": rgb(run["color"]),
                          "underline": bool(run.get("underline")), "strikethrough": bool(run.get("strike")),
                          "baselineOffset": {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT"}.get(run.get("script"), "NONE")})
            fields = fields + ["smallCaps", "foregroundColor", "underline", "strikethrough", "baselineOffset"]
            if run.get("highlight"):
                style["backgroundColor"] = rgb(run["highlight"])
                fields.append("backgroundColor")
            fields = ",".join(fields)
            if run["link"] and run["link"].startswith("#page="):
                target = page_slide.get(int(run["link"][6:])) if page_slide else None
                if target:
                    style["link"] = {"pageObjectId": target}  # TOC entries jump to their slide
                    fields += ",link"
            elif run["link"]:
                style["link"] = {"url": run["link"]}
                fields += ",link"
            end = start + len(run["text"])
            # (one request over the whole paragraph would restyle its bullet too)
            cuts = [start, end - 1, end] if p["bullet"] and start == p_start and end == p_end and end - start > 1 else [start, end]
            for c0, c1 in zip(cuts, cuts[1:]):
                reqs.append({"updateTextStyle": {
                    "objectId": object_id, "style": style, "fields": fields,
                    "textRange": {"type": "FIXED_RANGE", "startIndex": c0, "endIndex": c1},
                }})
            start = end

        # Code lines carry their indentation as leading spaces already.
        # Centred and right-aligned paragraphs place themselves: an indent would only offset them.
        text_indent = 0.0 if el.get("code") or p["align"] != "left" else (p["text_x0"] - left_pdf) * scale
        if p["bullet"]:
            # Slides ends the bullet glyph a little before indentFirstLine.
            first_indent = (p["bullet"]["bbox"][2] - left_pdf) * scale + \
                bullet_gap(p["bullet"], bullet_size(p["bullet"], base, scale))
        elif p.get("tab_x0") and not el.get("code"):
            # "label<TAB>content": a tab after the hanging label jumps to indentStart.
            first_indent, text_indent = text_indent, (p["tab_x0"] - left_pdf) * scale
        else:
            first_indent = text_indent
        reqs.append({"updateParagraphStyle": {
            "objectId": object_id,
            "textRange": {"type": "FIXED_RANGE", "startIndex": p_start, "endIndex": max(p_end, p_start + 1)},
            "style": {
                "alignment": {"left": "START", "center": "CENTER", "right": "END"}[p["align"]],
                "lineSpacing": round(100 * ratio, 1),
                "spaceAbove": pt(round(above, 2)), "spaceBelow": pt(0),
                "indentStart": pt(round(text_indent, 2)), "indentFirstLine": pt(round(first_indent, 2)),
            },
            "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine",
        }})
    return reqs


def number_box_requests(number: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[dict]:
    """A literal list number centred on its ball picture (classify.literal_list_numbers): a
    box around the ball's centre with centred text and contentAlignment MIDDLE. Lato digits
    are 0.72 em tall, so a baseline 0.362 em below the middle puts them in the middle too."""
    run = {**number, "smallcaps": False}
    style, fields = fonts.text_style(run, scale)
    size = fonts(run, scale)[1]
    cx, cy = number["center"][0] * scale, number["center"][1] * scale
    w = number["height"] * scale + 2 * PAD_X + len(number["text"]) * size  # never wraps "(iv)"
    h = max(number["height"] * scale, LINE_EM * size + 2)
    style["foregroundColor"] = rgb(number["color"])
    return [
        {"createShape": {"objectId": object_id, "shapeType": "TEXT_BOX", "elementProperties": {
            "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round((cx - w / 2) * EMU_PER_PT), "translateY": round((cy - h / 2) * EMU_PER_PT)}}}},
        {"updateShapeProperties": {"objectId": object_id, "fields": "contentAlignment,autofit.autofitType",
                                   "shapeProperties": {"contentAlignment": "MIDDLE", "autofit": {"autofitType": "NONE"}}}},
        {"insertText": {"objectId": object_id, "text": number["text"], "insertionIndex": 0}},
        {"updateTextStyle": {"objectId": object_id, "style": style, "fields": ",".join(fields + ["foregroundColor"]),
                             "textRange": {"type": "ALL"}}},
        {"updateParagraphStyle": {"objectId": object_id, "textRange": {"type": "ALL"},
                                  "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0),
                                            "indentStart": pt(0), "indentFirstLine": pt(0)},
                                  "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine"}},
    ]


def merge_blocks(elements: list[dict]) -> list[dict]:
    """A block body (see classify.blocks) reaches up under its title bar, with the outline
    of the whole block: resizing the block as a group can then never open a gap between
    the two, and the body's shadow falls behind the whole block."""
    heads = {e["block"]: e for e in elements if e["kind"] == "shape" and e.get("block") is not None and not e.get("title_bar")}
    out = []
    for el in elements:
        head = heads.get(el.get("block")) if el.get("title_bar") else None
        if head is None:
            out.append(el)
            continue
        top = el["title_bar"][1]
        top_round = head["shape"] == "ROUND_RECTANGLE" or (head["shape"] == "ROUND_2_SAME_RECTANGLE" and not head["flip"])
        bottom_round = el["shape"] == "ROUND_RECTANGLE" or (el["shape"] == "ROUND_2_SAME_RECTANGLE" and el["flip"])
        shape, flip = {(True, True): ("ROUND_RECTANGLE", False), (False, False): ("RECTANGLE", False),
                       (True, False): ("ROUND_2_SAME_RECTANGLE", False),
                       (False, True): ("ROUND_2_SAME_RECTANGLE", True)}[(top_round, bottom_round)]
        out.append({**el, "bbox": [el["bbox"][0], top, el["bbox"][2], el["bbox"][3]], "shape": shape, "flip": flip,
                    "radius": max(el["radius"], head["radius"])})
    # Creation order is z-order: title bars go above every body (shapes come first, then the rest).
    bar = lambda e: e["kind"] == "shape" and e.get("block") is not None and not e.get("title_bar")
    return sorted(out, key=lambda e: 0 if e["kind"] == "shape" and not bar(e) else 1 if bar(e) else 2)


# Native drop shadows, calibrated against beamer's block shadow (tools/calibrate_shadow.py):
# distance and blur per point of the shadow's width in the PDF, at 45°.
SHADOW_DISTANCE = 0.75
SHADOW_BLUR = 1.0
SHADOW_ALPHA = 0.5
TEMPLATE_PRESETS = {"ROUND_RECTANGLE": "roundRect", "ROUND_2_SAME_RECTANGLE": "round2SameRect", "RECTANGLE": "rect"}
TEMPLATE_LAYOUTS = {"TITLE": 0, "TITLE_ONLY": 5, "BLANK": 6}  # python-pptx default template layout indexes


def template_key(el: dict, scale: float) -> tuple | None:
    """Shapes the API can't make exactly: rounded corners of a given radius (the API only
    creates the default rounding) and drop shadows (read-only in the API). They are
    duplicated from template shapes that come with the imported .pptx."""
    if el["kind"] != "shape" or (el["shape"] == "RECTANGLE" and not el.get("shadow")):
        return None
    x0, y0, x1, y1 = el["bbox"]
    adj = 0.0
    if el["shape"] != "RECTANGLE":
        adj = min(0.5, round(el.get("radius", 0.0) / max(min(x1 - x0, y1 - y0), 0.01), 2))
    shadow = round(2 * el["shadow"]["size"] * scale) / 2 if el.get("shadow") else None
    return el["shape"], adj, shadow


NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _set_background(part, c_sld, fill: dict) -> None:
    """A page background in the .pptx: {"color": "#rrggbb"} or {"picture": Path} (stretched)."""
    from lxml import etree

    if "color" in fill:
        inner = f'<a:solidFill><a:srgbClr val="{fill["color"].lstrip("#").upper()}"/></a:solidFill>'
    else:
        _, rid = part.get_or_add_image_part(str(fill["picture"]))  # identical files are stored once
        inner = (f'<a:blipFill dpi="0" rotWithShape="1"><a:blip r:embed="{rid}"/><a:srcRect/>'
                 f'<a:stretch><a:fillRect/></a:stretch></a:blipFill>')
    old = c_sld.find(f"{{{NS_P}}}bg")
    if old is not None:
        c_sld.remove(old)
    c_sld.insert(0, etree.fromstring(
        f'<p:bg xmlns:p="{NS_P}" xmlns:a="{NS_A}" xmlns:r="{NS_R}"><p:bgPr>{inner}<a:effectLst/></p:bgPr></p:bg>'))


def _add_template_shapes(slide, keys: list[tuple]) -> None:
    from lxml import etree
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Pt

    kinds = {"ROUND_RECTANGLE": MSO_SHAPE.ROUNDED_RECTANGLE, "ROUND_2_SAME_RECTANGLE": MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
             "RECTANGLE": MSO_SHAPE.RECTANGLE, "ELLIPSE": MSO_SHAPE.OVAL, "DIAMOND": MSO_SHAPE.DIAMOND,
             "TRIANGLE": MSO_SHAPE.ISOSCELES_TRIANGLE}
    a = NS_A
    for i, (kind, adj, shadow) in enumerate(keys):
        if kind == "BENT_CONNECTOR":
            line = slide.shapes.add_connector(MSO_CONNECTOR.ELBOW, Pt(10), Pt(10), Pt(110), Pt(110))
            geometry = line._element.spPr.find(f"{{{a}}}prstGeom")
            geometry.set("prst", "bentConnector3")
            for old in geometry.findall(f"{{{a}}}avLst"):
                geometry.remove(old)
            geometry.append(etree.fromstring(
                f'<a:avLst xmlns:a="{a}"><a:gd name="adj1" fmla="val {round(adj * 100000)}"/></a:avLst>'))
            line._element.spPr.append(etree.fromstring(f'<a:effectLst xmlns:a="{a}"/>'))
            continue
        shape = slide.shapes.add_shape(kinds[kind], Pt(10 + i % 10 * 20), Pt(10 + i // 10 * 20), Pt(100), Pt(100))
        if adj is not None and kind in ("ROUND_RECTANGLE", "ROUND_2_SAME_RECTANGLE"):
            shape.adjustments[0] = adj
            if kind == "ROUND_2_SAME_RECTANGLE":
                shape.adjustments[1] = 0.0
        shape.fill.solid()
        shape.line.fill.background()
        # No text padding (the API can't set it): a diagram label fits a node as tight as TikZ's.
        body_pr = shape.text_frame._txBody.find(f"{{{a}}}bodyPr")
        for side in ("lIns", "tIns", "rIns", "bIns"):
            body_pr.set(side, "0")
        body_pr.set("anchor", "ctr")
        # python-pptx shapes refer to the theme's effect style, which has a shadow: always
        # give an explicit (possibly empty) effect list.
        effects = (f'<a:outerShdw blurRad="{round(SHADOW_BLUR * shadow * EMU_PER_PT)}" '
                   f'dist="{round(SHADOW_DISTANCE * shadow * EMU_PER_PT)}" dir="2700000" algn="tl" rotWithShape="0">'
                   f'<a:srgbClr val="000000"><a:alpha val="{round(SHADOW_ALPHA * 100000)}"/></a:srgbClr>'
                   f'</a:outerShdw>') if shadow else ""
        shape.element.spPr.append(etree.fromstring(f'<a:effectLst xmlns:a="{a}">{effects}</a:effectLst>'))


def build_pptx(page_w: float, page_h: float, keys: list[tuple], pages: list[dict], master_fill: dict) -> io.BytesIO:
    """The deck's starting point, imported through Drive. It carries everything the Slides API
    could only insert from a public URL, so no picture ever leaves the user's Drive:

    - the PDF's page size (presentations.create ignores pageSize);
    - the master background (`master_fill`), inherited by the layouts and most slides;
    - one source slide per deck slide (`pages`: {"layout", "fill" (None: inherit),
      "pictures": [{"file", "bbox" (slide pt), "alt", "title"}], "templates" (bool)}), holding its
      pictures and, if it needs any, the template shapes (shadows, exact corner radii).

    emit copies each source slide under our own object IDs and then deletes it."""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    height = SLIDE_W * page_h / page_w
    ratio = height / (prs.slide_height / EMU_PER_PT)
    prs.slide_width, prs.slide_height = Emu(round(SLIDE_W * EMU_PER_PT)), Emu(round(height * EMU_PER_PT))
    if abs(ratio - 1) > 1e-3:  # the default template's placeholders are laid out for 4:3
        for page in [prs.slide_master, *prs.slide_layouts]:
            for shape in page.placeholders:
                if shape.top is not None and shape.height is not None:
                    shape.top, shape.height = Emu(round(shape.top * ratio)), Emu(round(shape.height * ratio))
    master = prs.slide_master
    _set_background(master.part, master.element.find(f"{{{NS_P}}}cSld"), master_fill)
    for page in pages:
        slide = prs.slides.add_slide(prs.slide_layouts[TEMPLATE_LAYOUTS[page["layout"]]])
        if page["fill"]:
            _set_background(slide.part, slide.element.find(f"{{{NS_P}}}cSld"), page["fill"])
        for pic in page["pictures"]:
            x0, y0, x1, y1 = pic["bbox"]
            shape = slide.shapes.add_picture(str(pic["file"]), Emu(round(x0 * EMU_PER_PT)), Emu(round(y0 * EMU_PER_PT)),
                                             Emu(round((x1 - x0) * EMU_PER_PT)), Emu(round((y1 - y0) * EMU_PER_PT)))
            if pic.get("alt"):
                shape._element.nvPicPr.cNvPr.set("descr", pic["alt"])
                shape._element.nvPicPr.cNvPr.set("title", pic["title"])
        if page["templates"]:
            _add_template_shapes(slide, keys)
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def shape_requests(el: dict, slide_id: str, object_id: str, scale: float, template: dict | None = None) -> list[dict]:
    """A filled shape without outline. With a template ({"id", "w", "h"}: a template shape
    on this slide and its size in pt) the shape is a duplicate of it, else a new shape."""
    x0, y0, x1, y1 = (v * scale for v in el["bbox"])
    # ROUND_2_SAME_RECTANGLE rounds the top corners; for bottom corners flip both axes
    # (a 180° rotation), which moves the origin to the opposite corner.
    flip = -1 if el["flip"] else 1
    tx, ty = (x1, y1) if el["flip"] else (x0, y0)
    if template:
        reqs = [
            {"duplicateObject": {"objectId": template["id"], "objectIds": {template["id"]: object_id}}},
            {"updatePageElementTransform": {"objectId": object_id, "applyMode": "ABSOLUTE", "transform": {
                "scaleX": flip * (x1 - x0) / template["w"], "scaleY": flip * (y1 - y0) / template["h"], "unit": "EMU",
                "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)}}},
            {"updatePageElementsZOrder": {"pageElementObjectIds": [object_id], "operation": "BRING_TO_FRONT"}},
        ]
    else:
        reqs = [{"createShape": {
            "objectId": object_id, "shapeType": el["shape"],
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                "transform": {"scaleX": flip, "scaleY": flip, "unit": "EMU",
                              "translateX": round(tx * EMU_PER_PT), "translateY": round(ty * EMU_PER_PT)},
            },
        }}]
    return reqs + [
        {"updateShapeProperties": {
            "objectId": object_id,
            "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": rgb(el["fill"])["opaqueColor"]}},
                                "outline": {"propertyState": "NOT_RENDERED"}},
            "fields": "shapeBackgroundFill.solidFill.color,outline.propertyState",
        }},
    ]


TABLE_MIN_COLUMN_PT = 32.0  # the API refuses narrower columns
TABLE_ROW_PAD = 14.4        # cell padding above and below, not settable through the API
TABLE_ROW_EM = 1.195
TABLE_MIN_SPACING = 0.5


def table_line_spacing(pitch: float, z: float) -> float:
    """lineSpacing ratio at which a row of text size z fits into the given row pitch."""
    return min(1.0, max(TABLE_MIN_SPACING, (pitch - TABLE_ROW_PAD) / (TABLE_ROW_EM * z)))


TABLE_CELL_PAD = 7.2  # cell padding left and right


def fit_columns(bounds: list[float], cols: list[dict], scale: float) -> list[float]:
    """Column boundaries (PDF pt) moved just enough that every column's text fits inside the
    Slides cell padding, with a little room for the substitute font. Tables typeset with
    @{} have text touching the frame, which would otherwise wrap in Slides."""
    pad = TABLE_CELL_PAD / scale
    # Text grows away from its alignment edge: room on the right of left-aligned columns, on
    # the left of right-aligned ones, half on each side of centred ones.
    room = [0.08 * (c["x1"] - c["x0"]) + 1 / scale for c in cols]
    right = [r if c["align"] == "left" else r / 2 if c["align"] == "center" else 0.0 for c, r in zip(cols, room)]
    left = [r if c["align"] == "right" else r / 2 if c["align"] == "center" else 0.0 for c, r in zip(cols, room)]
    out = list(bounds)
    out[0] = min(out[0], cols[0]["x0"] - pad - left[0])
    out[-1] = max(out[-1], cols[-1]["x1"] + pad + right[-1])
    for i in range(1, len(cols)):
        lo, hi = cols[i - 1]["x1"] + pad + right[i - 1], cols[i]["x0"] - pad - left[i]
        out[i] = min(max(out[i], lo), hi) if lo <= hi else (lo + hi) / 2
    return out


def table_rows(el: dict, z: float, scale: float) -> tuple[float, list[float], list[float]]:
    """Table top, row heights and per-row lineSpacing (Slides pt).

    A row is at least TABLE_ROW_PAD + 1.195·z·lineSpacing tall (tools/probe_table_rows.py), so
    the line spacing is tightened until rows keep the original pitch. Row boundaries sit on
    the PDF's rules where there are any (booktabs puts extra space around them) and else just
    above the next row's text; each row's lineSpacing then moves its baseline to the PDF's."""
    baselines = [b * scale for b in el["row_baselines"]]
    n = len(baselines)
    pitches = [h * scale for h in el["row_heights"]]
    default = table_line_spacing(min(pitches), z)

    def offset(r: float) -> float:  # row top -> baseline
        return BASELINE_A + ASCENT_EM * z + extra_above(r, z)

    ruled: dict[int, float] = {}
    for rule in el.get("rules", []) + [b for b in el.get("borders", []) if b["position"] in ("TOP", "BOTTOM")]:
        if "y" in rule:
            ruled.setdefault(rule["row"] + (rule["position"] == "BOTTOM"), rule["y"] * scale)
    def target(i: int) -> float:  # where row i should start
        if i in ruled:
            return ruled[i]
        if i < n:
            return baselines[i] - offset(default)
        return baselines[-1] - offset(default) + pitches[-1]

    def clamp(r: float) -> float:
        return min(1.0, max(TABLE_MIN_SPACING, r))

    # Row by row from the actual top: a row's baseline offset and its minimum height both grow
    # with lineSpacing, so when the room above the text (from a rule) asks for more height than
    # the row has, the error is split between this baseline and the rows below.
    top = target(0)
    y, heights, ratios = top, [], []
    for i in range(n):
        room = baselines[i] - y  # offset(r) = offset(1) - (1 - r)·0.9·z
        r_room = clamp(1.0 if room >= offset(1.0) else 1 - (offset(1.0) - room) / (0.75 * LINE_EM * z))
        h_target = target(i + 1) - y
        r_fit = clamp((h_target - TABLE_ROW_PAD) / (TABLE_ROW_EM * z))
        r = r_room if r_room <= r_fit else (r_room + r_fit) / 2
        h = max(h_target, TABLE_ROW_PAD + TABLE_ROW_EM * z * r)
        ratios.append(r)
        heights.append(h)
        y += h
    return top, heights, ratios


def table_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper) -> list[dict]:
    cols = el["columns"]
    fx0, _, fx1, _ = el["frame"]
    bounds = el.get("bounds") or [fx0] + [(a["x1"] + b["x0"]) / 2 for a, b in zip(cols, cols[1:])] + [fx1]
    bounds = fit_columns(bounds, cols, scale)
    widths = [max(TABLE_MIN_COLUMN_PT, (b - a) * scale) for a, b in zip(bounds, bounds[1:])]
    first_run = next((r for row in el["cells"] for cell in row for r in cell), None)
    z = fonts(first_run, scale)[1] if first_run else el["size"] * scale
    n_rows, n_cols = len(el["cells"]), len(cols)
    y, heights, row_ratio = table_rows(el, z, scale)
    x = bounds[0] * scale

    reqs: list[dict] = [
        {"createTable": {"objectId": object_id, "rows": n_rows, "columns": n_cols, "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": emu(sum(widths)), "height": emu(sum(heights))},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(x * EMU_PER_PT), "translateY": round(y * EMU_PER_PT)}}}},
        # No grid: only the rules of the original are drawn.
        {"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": "ALL",
            "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": n_rows, "columnSpan": n_cols},
            "tableBorderProperties": {"tableBorderFill": {"solidFill": {"color": {"rgbColor": {}}, "alpha": 0}}},
            "fields": "tableBorderFill.solidFill.alpha"}},
    ]
    for i, w in enumerate(widths):
        reqs.append({"updateTableColumnProperties": {"objectId": object_id, "columnIndices": [i],
                                                     "tableColumnProperties": {"columnWidth": emu(w)},
                                                     "fields": "columnWidth"}})
    for i, h in enumerate(heights):
        reqs.append({"updateTableRowProperties": {"objectId": object_id, "rowIndices": [i],
                                                  "tableRowProperties": {"minRowHeight": emu(h)},
                                                  "fields": "minRowHeight"}})
    for rule in el["rules"]:
        reqs.append({"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": rule["position"],
            "tableRange": {"location": {"rowIndex": rule["row"], "columnIndex": 0}, "rowSpan": 1, "columnSpan": n_cols},
            "tableBorderProperties": {
                "tableBorderFill": {"solidFill": {"color": rgb(rule["color"])["opaqueColor"], "alpha": 1}},
                "weight": pt(round(max(0.5, rule["weight"] * scale), 2))},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight"}})
    for b in el.get("borders", []):
        reqs.append({"updateTableBorderProperties": {
            "objectId": object_id, "borderPosition": b["position"],
            "tableRange": {"location": {"rowIndex": b["row"], "columnIndex": b["col"]}, "rowSpan": 1, "columnSpan": 1},
            "tableBorderProperties": {
                "tableBorderFill": {"solidFill": {"color": rgb(b["color"])["opaqueColor"], "alpha": 1}},
                "weight": pt(round(max(0.5, b["weight"] * scale), 2))},
            "fields": "tableBorderFill.solidFill.color,tableBorderFill.solidFill.alpha,weight"}})
    for f in el.get("fills", []):
        reqs.append({"updateTableCellProperties": {
            "objectId": object_id, "tableRange": {"location": {"rowIndex": f["row"], "columnIndex": f["col"]},
                                                  "rowSpan": 1, "columnSpan": 1},
            "tableCellProperties": {"tableCellBackgroundFill": {"solidFill": {"color": rgb(f["color"])["opaqueColor"]}}},
            "fields": "tableCellBackgroundFill.solidFill.color"}})
    merged = {(m["row"], m["col"]): m for m in el.get("merges", [])}
    for m in merged.values():
        reqs.append({"mergeTableCells": {"objectId": object_id, "tableRange": {
            "location": {"rowIndex": m["row"], "columnIndex": m["col"]}, "rowSpan": m["rows"], "columnSpan": m["cols"]}}})
        if m["rows"] > 1:  # \multirow centres its text vertically
            reqs.append({"updateTableCellProperties": {
                "objectId": object_id, "tableRange": {"location": {"rowIndex": m["row"], "columnIndex": m["col"]},
                                                      "rowSpan": m["rows"], "columnSpan": m["cols"]},
                "tableCellProperties": {"contentAlignment": "MIDDLE"}, "fields": "contentAlignment"}})

    hidden = {(m["row"] + i, m["col"] + j) for m in merged.values()
              for i in range(m["rows"]) for j in range(m["cols"])} - set(merged)
    for r, row in enumerate(el["cells"]):
        for c, runs in enumerate(row):
            text = "".join(run["text"] for run in runs).strip()
            loc = {"rowIndex": r, "columnIndex": c}
            if not text:
                if (r, c) in hidden or first_run is None:
                    continue
                # An empty cell still has a line of the default font, which would set the row's
                # minimum height: give it a space in the table's font and line spacing.
                style, fields = fonts.text_style(first_run, scale)
                reqs += [
                    {"insertText": {"objectId": object_id, "cellLocation": loc, "text": " "}},
                    {"updateTextStyle": {"objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                                         "style": style, "fields": ",".join(fields)}},
                    {"updateParagraphStyle": {"objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                                              "style": {"lineSpacing": round(100 * row_ratio[r], 1), "spaceAbove": pt(0),
                                                        "spaceBelow": pt(0)},
                                              "fields": "lineSpacing,spaceAbove,spaceBelow"}},
                ]
                continue
            reqs.append({"insertText": {"objectId": object_id, "cellLocation": loc, "text": text}})
            start = 0
            for run in runs:
                piece = run["text"].strip() if len(runs) == 1 else run["text"]
                if start == 0:
                    piece = piece.lstrip()
                if not piece:
                    continue
                style, fields = fonts.text_style(run, scale)
                style.update({"smallCaps": run["smallcaps"], "foregroundColor": rgb(run["color"]),
                              "baselineOffset": {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT"}.get(run.get("script"), "NONE")})
                reqs.append({"updateTextStyle": {
                    "objectId": object_id, "cellLocation": loc,
                    "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": min(len(text), start + len(piece))},
                    "style": style, "fields": ",".join(fields + ["smallCaps", "foregroundColor", "baselineOffset"])}})
                start += len(piece)
            col = cols[c]
            align = col["align"]
            # Line the text up with the original inside the (contiguous) Slides columns.
            indent_start = max(0.0, (col["x0"] - bounds[c]) * scale - PAD_X) if align == "left" else 0.0
            indent_end = max(0.0, (bounds[c + 1] - col["x1"]) * scale - PAD_X) if align == "right" else 0.0
            if (r, c) in merged and merged[(r, c)]["cols"] > 1:
                align, indent_start, indent_end = merged[(r, c)]["align"], 0.0, 0.0
            reqs.append({"updateParagraphStyle": {
                "objectId": object_id, "cellLocation": loc, "textRange": {"type": "ALL"},
                "style": {"alignment": {"left": "START", "center": "CENTER", "right": "END"}[align],
                          "lineSpacing": round(100 * row_ratio[r], 1), "spaceAbove": pt(0), "spaceBelow": pt(0),
                          "indentStart": pt(round(indent_start, 2)), "indentFirstLine": pt(round(indent_start, 2)),
                          "indentEnd": pt(round(indent_end, 2))},
                "fields": "alignment,lineSpacing,spaceAbove,spaceBelow,indentStart,indentFirstLine,indentEnd"}})
    return reqs


# Share of a preset shape's width its text may use: Slides lays text out in the shape's .pptx
# text rectangle (an ellipse's is its inscribed square, a diamond's half its width).
TEXT_RECT_WIDTH = {"RECTANGLE": 1.0, "ROUND_RECTANGLE": 0.9, "ELLIPSE": 0.707, "DIAMOND": 0.5}
# Connection sites of preset shapes in their .pptx order (fractions of the box): a line connected
# to a site follows the shape when it is moved in Slides.
CONNECTION_SITES = {
    "RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ROUND_RECTANGLE": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "DIAMOND": [(0.5, 0), (0, 0.5), (0.5, 1), (1, 0.5)],
    "ELLIPSE": [(0.5, 0), (0.1464, 0.1464), (0, 0.5), (0.1464, 0.8536), (0.5, 1), (0.8536, 0.8536), (1, 0.5), (0.8536, 0.1464)],
    "TRIANGLE": [(0.5, 0), (0.25, 0.5), (0, 1), (0.5, 1), (1, 1), (0.75, 0.5)],
}
LABEL_ROOM = 1.08  # the substitute font may run this much wider


def label_inside(node: dict) -> bool:
    """A node's label goes into the node shape itself (it then moves and resizes with it) when
    it fits the shape's text rectangle without wrapping."""
    x0, _, x1, _ = node["bbox"]
    text = "".join(r["text"] for runs in node["paragraphs"] for r in runs).strip()
    return bool(text) and node["shape"] in TEXT_RECT_WIDTH and \
        node.get("label_w", 0.0) * LABEL_ROOM <= (x1 - x0) * TEXT_RECT_WIDTH[node["shape"]] - 0.5


def node_template_key(node: dict) -> tuple:
    return node["shape"], None, None


def bend_template_key(line: dict) -> tuple:
    """An elbow connector (bentConnector3) turning at its start (|-, adj 0) or its end (-|)."""
    return "BENT_CONNECTOR", 0.0 if line["bend"] == "vh" else 1.0, None


def element_template_keys(el: dict, scale: float) -> list[tuple]:
    if el["kind"] == "diagram":
        return [node_template_key(n) for n in el["nodes"] if n["shape"] and label_inside(n)] + \
               [bend_template_key(ln) for ln in el["lines"] if ln.get("bend")]
    key = template_key(el, scale)
    return [key] if key else []


def connection(point: list[float], nodes: list[dict], oids: list[str]) -> dict | None:
    """The node connection site a line end sits on (PDF pt, within 1.5 pt), if any."""
    best = None
    for node, oid in zip(nodes, oids):
        if not node["shape"] or node["shape"] not in CONNECTION_SITES:
            continue
        x0, y0, x1, y1 = node["bbox"]
        for index, (fx, fy) in enumerate(CONNECTION_SITES[node["shape"]]):
            d = math.hypot(point[0] - (x0 + fx * (x1 - x0)), point[1] - (y0 + fy * (y1 - y0)))
            if d <= 1.5 and (best is None or d < best[0]):
                best = (d, {"connectedObjectId": oid, "connectionSiteIndex": index})
    return best[1] if best else None


def arrow_style(arrow) -> str:
    if not arrow:
        return "NONE"
    return arrow if isinstance(arrow, str) else "OPEN_ARROW"


def diagram_requests(el: dict, slide_id: str, object_id: str, scale: float, fonts: FontMapper,
                     template=None) -> list[dict]:
    """Nodes become shapes, edges become lines with arrow heads; the parts are grouped so the
    diagram moves as one piece but stays editable. A label that fits goes inside its node (a
    template shape without text padding, see label_inside); one that doesn't gets a text box
    grouped with its node. Line ends on a node's connection site are connected to it, so edges
    follow nodes moved in Slides. `template(key)` gives this slide's template shape for a key."""
    reqs: list[dict] = []
    children: list[str] = []
    node_oids = [f"{object_id}_n{j}" for j in range(len(el["nodes"]))]
    segments = []  # (object id, from, to, line): elbows without a template fall back to two lines
    for j, ln in enumerate(el["lines"]):
        if ln.get("bend") and template is None:
            segments += [(f"{object_id}_l{j}", ln["from"], ln["via"], {**ln, "arrow_to": None}),
                         (f"{object_id}_l{j}b", ln["via"], ln["to"], {**ln, "arrow_from": None})]
        else:
            segments.append((f"{object_id}_l{j}", ln["from"], ln["to"], ln))
    for oid, (x1, y1), (x2, y2), ln in segments:
        dx, dy = (x2 - x1) * scale, (y2 - y1) * scale
        if ln.get("bend") and template is not None:
            tpl = template(bend_template_key(ln))
            reqs += [
                {"duplicateObject": {"objectId": tpl["id"], "objectIds": {tpl["id"]: oid}}},
                # Like a straight line: from the transform origin along +size, flipped by negative scales.
                {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
                    "scaleX": dx / tpl["w"], "scaleY": dy / tpl["h"], "unit": "EMU",
                    "translateX": round(x1 * scale * EMU_PER_PT), "translateY": round(y1 * scale * EMU_PER_PT)}}},
                {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}},
            ]
        else:
            reqs.append({"createLine": {"objectId": oid, "lineCategory": "STRAIGHT", "elementProperties": {
                "pageObjectId": slide_id,
                "size": {"width": emu(abs(dx)), "height": emu(abs(dy))},
                # The line runs from the transform origin along +size, flipped by negative scales.
                "transform": {"scaleX": -1 if dx < 0 else 1, "scaleY": -1 if dy < 0 else 1, "unit": "EMU",
                              "translateX": round(x1 * scale * EMU_PER_PT), "translateY": round(y1 * scale * EMU_PER_PT)}}}})
        reqs.append({"updateLineProperties": {"objectId": oid, "fields": "lineFill.solidFill.color,weight,startArrow,endArrow",
                                              "lineProperties": {
                                                  "lineFill": {"solidFill": {"color": rgb(ln["stroke"])["opaqueColor"]}},
                                                  "weight": pt(round(max(0.5, ln["width"] * scale), 2)),
                                                  "startArrow": arrow_style(ln["arrow_from"]),
                                                  "endArrow": arrow_style(ln["arrow_to"])}}})
        children.append(oid)
    for j, node in enumerate(el["nodes"]):
        oid = node_oids[j]
        x0, y0, x1, y1 = (v * scale for v in node["bbox"])
        text = "\n".join("".join(r["text"] for r in runs).strip() for runs in node["paragraphs"])
        card = node.get("text")  # a card's text: a text box on the PDF baselines (classify.card_text)
        inside = not card and bool(node["shape"]) and label_inside(node) and template is not None
        props = None if node["shape"] is None else {"contentAlignment": "MIDDLE", "autofit": {"autofitType": "NONE"},
                 "shapeBackgroundFill": ({"solidFill": {"color": rgb(node["fill"])["opaqueColor"]}} if node["fill"]
                                         else {"propertyState": "NOT_RENDERED"}),
                 "outline": ({"outlineFill": {"solidFill": {"color": rgb(node["stroke"])["opaqueColor"]}},
                              "weight": pt(round(max(0.5, (node["width"] or 0.4) * scale), 2))}
                             if node["stroke"] else {"propertyState": "NOT_RENDERED"})}
        members = []
        if props:  # free labels (edge labels, captions) have no shape, only the text box below
            fields = ["contentAlignment", "autofit.autofitType", "shapeBackgroundFill"]
            fields += ["outline.outlineFill.solidFill.color", "outline.weight"] if node["stroke"] else ["outline.propertyState"]
            if not node["fill"]:
                fields[fields.index("shapeBackgroundFill")] = "shapeBackgroundFill.propertyState"
            else:
                fields[fields.index("shapeBackgroundFill")] = "shapeBackgroundFill.solidFill.color"
            if inside:
                tpl = template(node_template_key(node))
                reqs += [
                    {"duplicateObject": {"objectId": tpl["id"], "objectIds": {tpl["id"]: oid}}},
                    {"updatePageElementTransform": {"objectId": oid, "applyMode": "ABSOLUTE", "transform": {
                        "scaleX": (x1 - x0) / tpl["w"], "scaleY": (y1 - y0) / tpl["h"], "unit": "EMU",
                        "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}},
                    {"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}},
                ]
            else:
                reqs.append({"createShape": {"objectId": oid, "shapeType": node["shape"], "elementProperties": {
                    "pageObjectId": slide_id, "size": {"width": emu(x1 - x0), "height": emu(y1 - y0)},
                    "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                  "translateX": round(x0 * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}})
            reqs.append({"updateShapeProperties": {"objectId": oid, "shapeProperties": props, "fields": ",".join(fields)}})
            members.append(oid)
        if card:
            for k, box in enumerate(card):
                label = f"{object_id}_x{j}" + (f"_{k}" if k else "")
                reqs += text_box_requests(box, slide_id, label, scale, fonts)
                members.append(label)
        elif text:
            if not inside:
                # A label wider than the node's text rectangle would wrap inside the shape: it
                # gets its own wider text box, centred on the node and grouped with it.
                label = f"{object_id}_x{j}"
                cx, w = (x0 + x1) / 2, (x1 - x0) + 2 * PAD_X + 40
                reqs += [
                    {"createShape": {"objectId": label, "shapeType": "TEXT_BOX", "elementProperties": {
                        "pageObjectId": slide_id, "size": {"width": emu(w), "height": emu(y1 - y0)},
                        "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                                      "translateX": round((cx - w / 2) * EMU_PER_PT), "translateY": round(y0 * EMU_PER_PT)}}}},
                    {"updateShapeProperties": {"objectId": label, "fields": "contentAlignment,autofit.autofitType",
                                               "shapeProperties": {"contentAlignment": "MIDDLE",
                                                                   "autofit": {"autofitType": "NONE"}}}},
                ]
                members.append(label)
            target = oid if inside else label
            reqs.append({"insertText": {"objectId": target, "text": text}})
            start = 0
            for runs in node["paragraphs"]:
                line_text = "".join(r["text"] for r in runs).strip()
                offset = 0
                for run in runs:
                    piece = run["text"].strip() if len(runs) == 1 else run["text"]
                    if offset == 0:
                        piece = piece.lstrip()
                    if not piece:
                        continue
                    style, sfields = fonts.text_style(run, scale)
                    style["foregroundColor"] = rgb(run["color"])
                    reqs.append({"updateTextStyle": {
                        "objectId": target, "style": style, "fields": ",".join(sfields + ["foregroundColor"]),
                        "textRange": {"type": "FIXED_RANGE", "startIndex": start + offset,
                                      "endIndex": min(start + len(line_text), start + offset + len(piece))}}})
                    offset += len(piece)
                start += len(line_text) + 1
            reqs.append({"updateParagraphStyle": {
                "objectId": target, "textRange": {"type": "ALL"}, "fields": "alignment,lineSpacing,spaceAbove,spaceBelow",
                "style": {"alignment": "CENTER", "lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)}}})
        if len(members) >= 2:  # a node with its label (or card texts) outside: they move together
            reqs.append({"groupObjects": {"groupObjectId": f"{object_id}_g{j}", "childrenObjectIds": members}})
            members = [f"{object_id}_g{j}"]
        children += members
    # Edges follow the nodes they start or end on.
    for oid, start, end, ln in segments:
        ends = {"startConnection": connection(start, el["nodes"], node_oids) if start == ln["from"] else None,
                "endConnection": connection(end, el["nodes"], node_oids) if end == ln["to"] else None}
        ends = {k: v for k, v in ends.items() if v}
        if ends:
            reqs.append({"updateLineProperties": {"objectId": oid, "fields": ",".join(ends), "lineProperties": ends}})
    if len(children) >= 2:
        reqs.append({"groupObjects": {"groupObjectId": object_id, "childrenObjectIds": children}})
    return reqs


def block_groups(elements: list[dict], object_ids: list[str], title_oid: str | None) -> list[list[str]]:
    """Object ids per block: its panel shapes (title bar and body, see classify.blocks), plus
    the text and pictures lying on them."""
    blocks: dict[int, list] = {}  # block -> [x0, y0, x1, y1, [oids]]
    for el, oid in zip(elements, object_ids):
        if el["kind"] == "shape" and el.get("block") is not None:
            x0, y0, x1, y1 = el["bbox"]
            b = blocks.setdefault(el["block"], [x0, y0, x1, y1, []])
            b[:4] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
            b[4].append(oid)
    out = []
    for x0, y0, x1, y1, members in blocks.values():
        if len(members) < 2:
            continue  # a lone panel is not recognisably a block
        for el, oid in zip(elements, object_ids):
            # Tables can't be grouped in Slides: a table in a block stays on its own.
            if el["kind"] in ("text", "image") and oid != title_oid and not el.get("anchor"):
                ex0, ey0, ex1, ey1 = el["bbox"]
                cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    members.append(oid)
        out.append(members)
    return out


def rule_groups(elements: list[dict], object_ids: list[str]) -> list[list[str]]:
    """Rules lying on one another (a progress bar on its track) move as one."""
    rules = [(el["bbox"], oid) for el, oid in zip(elements, object_ids) if el["kind"] == "shape" and el.get("role") == "rule"]
    out: list[list] = []  # [bbox, [oids]]
    for (x0, y0, x1, y1), oid in rules:
        for g in out:
            gx0, gy0, gx1, gy1 = g[0]
            if x0 < gx1 and gx0 < x1 and y0 < gy1 and gy0 < y1:
                g[0] = [min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)]
                g[1].append(oid)
                break
        else:
            out.append([[x0, y0, x1, y1], [oid]])
    return [g[1] for g in out if len(g[1]) >= 2]


def text_right_limit(el: dict, slide: dict) -> float | None:
    """How far right (PDF x) a text element's box may reach: inside a panel (a block body),
    as far from the panel's right edge as the text is from its left edge; elsewhere the
    mirrored left margin of the page, stopping short of anything to the right on the same
    lines (the other column, a picture)."""
    if el.get("role") not in ("body", "title", None) or not el["paragraphs"]:
        return None
    x0, y0, x1, y1 = el["bbox"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    panels = [e["bbox"] for e in slide["elements"] if e["kind"] == "shape" and e.get("role") == "panel"
              and e["bbox"][0] <= cx <= e["bbox"][2] and e["bbox"][1] <= cy <= e["bbox"][3]]
    if panels:
        px0, _, px1, _ = min(panels, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        limit = px1 - max(x0 - px0, 2.0)
    else:
        margin = min((e["bbox"][0] for e in slide["elements"] if e["kind"] == "text" and e.get("role") in ("body", None)),
                     default=x0)
        limit = slide["size"][0] - margin
        size = el["paragraphs"][0]["size"]
        for o in slide["elements"]:
            if o is el or o["kind"] == "shape":
                continue
            ox0, oy0, ox1, oy1 = o["bbox"]
            if ox0 >= x1 - 1 and oy0 < y1 and oy1 > y0:
                limit = min(limit, ox0 - size)
    return limit if limit > x1 else None


def space_shift(run: dict, em: float) -> float:
    """What word spaces add to a formula gap's position in Slides. The substitute's size
    calibration makes its glyphs wider and its spaces narrower than TeX's, evening out at word
    ends; a formula starts after a space, so it lands one space deficit early. TeX also
    stretches some spaces (after a colon, around math): Slides sets a plain space there."""
    if google_font(run["font"]):
        return 0.0  # the PDF's own font: its spaces too
    words = sorted((b[6], b[6] + b[0]) for b in run.get("before", []) if len(b) >= 7)
    if not words:
        return 0.0
    size = run["size"]
    gaps = [b[0] - a[1] for a, b in zip(words, words[1:])] + [run["hole_x0"] - words[-1][1]]
    spaces = [g for g in gaps if g > 0.15 * size]
    if not spaces:
        return 0.0
    nominal = min(sorted(spaces)[len(spaces) // 2], 0.4 * size)
    shift = sum(nominal - g for g in spaces)
    if gaps[-1] > 0.15 * size:
        shift += SYMBOL_ADVANCE_EM[" "] * em - nominal
    return shift


def formula_shifts(slide: dict, scale: float, fonts: FontMapper) -> dict[str, float]:
    """PDF-point x offsets for inline formula pictures, so each sits over the gap where Slides
    will put it: the words before it on its line come out a little narrower or wider."""
    pictures = [e for e in slide["elements"] if e["kind"] == "image" and e.get("anchor")]
    out = {}
    for el in slide["elements"]:
        if el["kind"] != "text":
            continue
        for p in el["paragraphs"]:
            if p["align"] != "left":
                continue
            for run in p["runs"]:
                if not run.get("hole"):
                    continue
                em = fonts(run, scale)[1] / scale  # the line's Slides font size, in PDF points
                shift = HOLE_PAD  # the gap has room for the picture's padding on its left too
                for w, font, family, bold, italic, *text in run.get("before", []):
                    if family == "math" and text:
                        # Math symbols come from Lato or Slides' fallback fonts, at their own widths.
                        shift += sum(SYMBOL_ADVANCE_EM.get(ch, 0.55) for ch in text[0]) * em - w
                    else:
                        spaces = len(text[0]) - len(text[0].strip()) if text else 0
                        # A math space inside the span (" 2," after ≥): TeX's thick space is wider.
                        pdf_spaces = spaces * MATH_SPACE_EM * run["size"]
                        shift += (w - pdf_spaces) * (fonts.width_ratio(font, family, bold, italic) - 1) + \
                            spaces * SYMBOL_ADVANCE_EM[" "] * em - pdf_spaces
                shift += space_shift(run, em)
                pic = next((e for e in pictures if e["anchor"] == el["id"]
                            and abs(e["bbox"][0] + HOLE_PAD - run["hole_x0"]) < 0.6), None)
                if pic and abs(shift) >= 0.2:
                    out[pic["id"]] = shift
    return out


def subtitle_element(slide: dict, title_idx: int) -> int | None:
    """On the title page, the biggest plain text box below the title (authors, institute,
    date) goes into the TITLE layout's subtitle placeholder."""
    if not slide.get("title_page"):
        return None
    title_bottom = slide["elements"][title_idx]["bbox"][3]
    below = [(sum(len(r["text"]) for p in e["paragraphs"] for r in p["runs"]), i)
             for i, e in enumerate(slide["elements"])
             if e["kind"] == "text" and e["role"] == "body" and e["bbox"][1] > title_bottom
             and not any(p["bullet"] for p in e["paragraphs"])]
    return max(below)[1] if below else None


def title_element(slide: dict) -> int | None:
    """Index of the element that becomes the slide's title placeholder."""
    for i, el in enumerate(slide["elements"]):
        if el["kind"] == "text" and el["role"] == "title":
            return i
    return None


PICTURE_TITLES = {"math": "Formula", "icon": "Icon", "fallback": "Picture"}


# ---------------------------------------------------------------- presentation

def import_presentation(slides, drive, title: str, page_w: float, page_h: float, pptx: io.BytesIO,
                        existing: str | None) -> dict:
    """A new deck from the .pptx, or an existing one (same URL) with its content replaced by it."""
    media = MediaIoBaseUpload(pptx, mimetype=PPTX_MIME)
    if existing:
        execute(drive.files().update(fileId=existing, media_body=media, fields="id"))
        pid = existing
    else:
        pid = execute(drive.files().create(
            body={"name": title, "mimeType": "application/vnd.google-apps.presentation"},
            media_body=media, fields="id"))["id"]
    pres = execute(slides.presentations().get(presentationId=pid))
    got = pres["pageSize"]["height"]["magnitude"] / pres["pageSize"]["width"]["magnitude"]
    if abs(got - page_h / page_w) > 0.003:
        raise RuntimeError(f"page aspect {got:.4f} != PDF aspect {page_h / page_w:.4f}")
    return pres


def background_key(slide: dict, out: Path) -> tuple:
    if slide.get("background_color"):
        return ("color", slide["background_color"].lower())
    return ("png", hashlib.sha1((out / slide["background"]).read_bytes()).hexdigest())


LAYOUT_TEXT_PREFIX = "b2s_L"


def write_layout_texts(slides, pid: str, texts: list[dict], scale: float, fonts: FontMapper) -> None:
    """Header/footer text shared by every slide goes onto the layouts our slides use, so
    it is edited once for the whole deck. Previous runs' layout texts are replaced."""
    pres = execute(slides.presentations().get(
        presentationId=pid, fields="layouts(objectId,layoutProperties,pageElements(objectId))"))
    layouts = pres.get("layouts", [])  # all of them: slides added later in Slides get the footer too
    # All old ones first: object IDs are unique across the whole presentation.
    reqs = [{"deleteObject": {"objectId": e["objectId"]}} for l in pres.get("layouts", [])
            for e in l.get("pageElements", []) if e["objectId"].startswith(LAYOUT_TEXT_PREFIX)]
    for li, layout in enumerate(layouts):
        for ti, el in enumerate(texts):
            reqs += text_box_requests(el, layout["objectId"], f"{LAYOUT_TEXT_PREFIX}{li}_{ti}", scale, fonts)
    if reqs:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))


def style_layout_placeholders(slides, pid: str, deck: dict, scale: float, fonts: FontMapper, dy: float) -> None:
    """Title and body placeholders of every layout take the deck's own look (font, size,
    colour, title position), so slides added later in Slides match the converted ones."""
    texts = [(s, e) for s in deck["slides"] for e in s["elements"] if e["kind"] == "text" and e["paragraphs"][0]["runs"]]
    frame_title = next((e for s, e in texts if e["role"] == "title" and not s.get("title_page")), None)
    page_title = next((e for s, e in texts if e["role"] == "title" and s.get("title_page")), None) or frame_title
    body_runs = Counter((r["font"], r["size"], r["color"], r["family"]) for s, e in texts if e["role"] == "body"
                        for p in e["paragraphs"] for r in p["runs"] for _ in range(len(r["text"])))
    body = None
    if body_runs:
        font, size, color, family = body_runs.most_common(1)[0][0]
        body = {"font": font, "size": size, "color": color, "family": family, "bold": False, "italic": False}
    pres = execute(slides.presentations().get(presentationId=pid, fields=(
        "layouts(objectId,pageElements(objectId,size,transform,shape(placeholder/type,text/textElements)))")))
    reqs = []
    for layout in pres.get("layouts", []):
        for pe in layout.get("pageElements", []):
            kind = pe.get("shape", {}).get("placeholder", {}).get("type")
            if kind in ("TITLE", "CENTERED_TITLE"):
                el = page_title if kind == "CENTERED_TITLE" else frame_title
                if el is None:
                    continue
                p = el["paragraphs"][0]
                run = p["runs"][0]
                z = fonts(run, scale)[1]
                x = p["text_x0"] * scale - PAD_X
                if p["align"] == "center":
                    x = min(x, 0.1 * SLIDE_W)
                w = SLIDE_W - 2 * max(x, 10)
                h = 2 * LINE_EM * z + 2 * BASELINE_A
                y = p["lines"][0]["baseline"] * scale - (BASELINE_A + ASCENT_EM * z) + dy
                reqs.append({"updatePageElementTransform": {"objectId": pe["objectId"], "applyMode": "ABSOLUTE", "transform": {
                    "scaleX": w / (pe["size"]["width"]["magnitude"] / EMU_PER_PT),
                    "scaleY": h / (pe["size"]["height"]["magnitude"] / EMU_PER_PT), "unit": "EMU",
                    "translateX": round(max(x, 10) * EMU_PER_PT), "translateY": round(max(0.0, y) * EMU_PER_PT)}}})
                reqs.append({"updateShapeProperties": {"objectId": pe["objectId"], "fields": "contentAlignment",
                                                       "shapeProperties": {"contentAlignment": "TOP"}}})
                align = {"left": "START", "center": "CENTER", "right": "END"}[p["align"]]
            elif kind == "BODY" and body:
                run, align = body, "START"
            else:
                continue
            style, fields = fonts.text_style(run, scale)
            style["foregroundColor"] = rgb(run["color"])
            styling = [
                {"updateTextStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"}, "style": style,
                                     "fields": ",".join(fields + ["foregroundColor"])}},
                {"updateParagraphStyle": {"objectId": pe["objectId"], "textRange": {"type": "ALL"},
                                          "style": {"alignment": align}, "fields": "alignment"}},
            ]
            if not any(t.get("textRun", {}).get("content", "").strip()
                       for t in pe["shape"].get("text", {}).get("textElements", [])):
                # An empty placeholder can't be styled, and the API refuses to put text into
                # layout placeholders: leave those as the theme has them.
                styling = [r for r in styling if "updateParagraphStyle" in r]
                continue
            reqs += styling
    for i in range(0, len(reqs), 200):
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs[i:i + 200]}))
        except HttpError as e:  # the deck's look for new slides is a nicety, never a reason to fail
            print(f"warning: could not style the layouts ({api_error(e)})")


def api_error(e: HttpError) -> str:
    try:
        return json.loads(e.content)["error"]["message"][:200]
    except (ValueError, KeyError, TypeError):
        return str(e)[:200]


def batch(slides, pid: str, reqs: list[dict]) -> None:
    execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))


# ---------------------------------------------------------------- main entry

def existing_presentation(drive, out: Path) -> str | None:
    """The deck from a previous run of this output folder, if it still exists (not trashed)."""
    state_file = out / "emit.json"
    if not state_file.exists():
        return None
    pid = json.loads(state_file.read_text(encoding="utf-8"))["presentationId"]
    try:
        f = execute(drive.files().get(fileId=pid, fields="id,trashed,mimeType"))
    except HttpError:
        return None
    return pid if not f.get("trashed") and f.get("mimeType") == "application/vnd.google-apps.presentation" else None


def size_pt(element: dict) -> tuple[float, float]:
    size = element["size"]
    return tuple(size[k]["magnitude"] / (EMU_PER_PT if size[k]["unit"] == "EMU" else 1) for k in ("width", "height"))


def fallback_pictures(deck: dict, refused: list[tuple[int, str]], out: Path) -> dict:
    """The deck with every element the API refused ((PDF page, element id)) replaced by a
    picture of its region, cropped from the PDF the deck was built from."""
    from .render import crop_region

    source_pdf = out / "slides.pdf" if (out / "slides.pdf").exists() else Path(deck["source"]["pdf"])
    new_slides = []
    for slide in deck["slides"]:
        ids = {eid for page, eid in refused if page == slide["page"]}
        elements = []
        for el in slide["elements"]:
            if el["id"] in ids and el["kind"] != "image":
                ids.discard(el["id"])
                x0, y0, x1, y1 = el["bbox"]
                bbox = [x0 - 2, y0 - 2, x1 + 2, y1 + 2]
                path = out / "figures" / f"fallback-{el['id']}.png"
                crop_region(source_pdf, slide["page"], bbox, path, 6.0)
                el = {"kind": "image", "id": el["id"], "role": "fallback", "bbox": bbox,
                      "file": str(path.relative_to(out)).replace("\\", "/")}
            elements.append(el)
        new_slides.append({**slide, "elements": elements})
    return {**deck, "slides": new_slides}


def emit(deck: dict, out: Path, title: str, new_deck: bool = False) -> dict:
    slides, drive = slides_service(), drive_service()
    deck = {**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]}
    existing = None if new_deck else existing_presentation(drive, out)
    if existing:
        print(f"updating existing deck {existing}")
    state, refused = build_deck(slides, drive, deck, out, title, existing)
    if refused:
        # A picture can only come with the imported .pptx (the API inserts images from public
        # URLs only), so the deck is built once more with the refused elements as pictures.
        print(f"rebuilding the deck with {len(refused)} refused element(s) as pictures")
        state, again = build_deck(slides, drive, fallback_pictures(deck, refused, out), out, title,
                                  state["presentationId"])
        for page, eid in again:
            print(f"warning: slide {page + 1}: {eid} was refused again and is missing")
    (out / "emit.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state


def build_deck(slides, drive, deck: dict, out: Path, title: str, existing: str | None) -> tuple[dict, list[tuple[int, str]]]:
    """Import the .pptx and fill in the content. Returns the state for emit.json and the
    elements the API refused ((PDF page, element id))."""
    page_w, page_h = deck["slides"][0]["size"]
    scale = SLIDE_W / page_w
    fonts = FontMapper()

    def slide_layout(slide: dict) -> tuple[str, str | None]:
        """The title page uses the TITLE layout (centered title), frames with a title TITLE_ONLY."""
        if title_element(slide) is None:
            return "BLANK", None
        return ("TITLE", "CENTERED_TITLE") if slide.get("title_page") else ("TITLE_ONLY", "TITLE")

    keys = list(dict.fromkeys(k for s in deck["slides"] for e in s["elements"] for k in element_template_keys(e, scale)))
    uses_templates = {s["page"]: any(element_template_keys(e, scale) for e in s["elements"]) for s in deck["slides"]}
    shifts = {s["page"]: formula_shifts(s, scale, fonts) for s in deck["slides"]}

    def placed(el: dict, n: int) -> dict:
        """Inline formula pictures sit over the gap Slides leaves for them (formula_shifts)."""
        dx = shifts[n].get(el["id"])
        return el if dx is None else {**el, "bbox": [el["bbox"][0] + dx, el["bbox"][1], el["bbox"][2] + dx, el["bbox"][3]]}

    # Backgrounds: the most common one becomes the master's (the deck's theme): layouts and
    # slides inherit it, and slides added later too. Identical pictures are stored once.
    bg_key = {s["page"]: background_key(s, out) for s in deck["slides"]}
    bg_file = {bg_key[s["page"]]: out / s["background"] for s in deck["slides"] if not s.get("background_color")}
    counts = Counter(bg_key.values())
    shared = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] >= 2 else None

    def fill(key: tuple) -> dict:
        return {"color": key[1]} if key[0] == "color" else {"picture": bg_file[key]}

    pages = [{
        "layout": slide_layout(s)[0],
        "fill": None if bg_key[s["page"]] == shared else fill(bg_key[s["page"]]),
        "pictures": [{"file": out / e["file"], "bbox": [v * scale for v in placed(e, s["page"])["bbox"]],
                      "alt": e.get("alt"), "title": PICTURE_TITLES.get(e.get("role"), "Figure")}
                     for e in s["elements"] if e["kind"] == "image"],
        "templates": uses_templates[s["page"]],
    } for s in deck["slides"]]
    pptx = build_pptx(page_w, page_h, keys, pages, fill(shared or ("color", "#ffffff")))
    pres = import_presentation(slides, drive, title, page_w, page_h, pptx, existing)
    pid = pres["presentationId"]
    sources = pres.get("slides", [])
    if len(sources) != len(deck["slides"]):
        raise RuntimeError(f"the import brought {len(sources)} slides, expected {len(deck['slides'])}")

    # Phase 1: every source slide is copied under our object IDs (slide, title and subtitle
    # placeholders, pictures, template shapes); the sources are deleted at the end.
    template_sizes: list[tuple[float, float]] = []
    reqs = []
    for slide, source in zip(deck["slides"], sources):
        n = slide["page"]  # PDF page index; slides may skip pages (overlays)
        slide_id = f"b2s_s{n:03}"
        els = source.get("pageElements", [])
        placeholders = {e["shape"]["placeholder"]["type"]: e["objectId"] for e in els if "placeholder" in e.get("shape", {})}
        pictures = [e["objectId"] for e in els if "image" in e]
        shapes = [e for e in els if "image" not in e and "placeholder" not in e.get("shape", {})]
        picture_idx = [i for i, e in enumerate(slide["elements"]) if e["kind"] == "image"]
        if len(pictures) != len(picture_idx) or len(shapes) != (len(keys) if uses_templates[n] else 0):
            raise RuntimeError(f"slide {n + 1}: the import brought {len(pictures)} pictures and {len(shapes)} "
                               f"template shapes, expected {len(picture_idx)} and {len(keys) if uses_templates[n] else 0}")
        ids = {source["objectId"]: slide_id}
        ids.update({oid: f"{slide_id}_f{i}" for oid, i in zip(pictures, picture_idx)})
        ids.update({e["objectId"]: f"{slide_id}_k{j}" for j, e in enumerate(shapes)})
        template_sizes = template_sizes or [size_pt(e) for e in shapes]
        title_idx = title_element(slide)
        if title_idx is not None:
            ids[placeholders[slide_layout(slide)[1]]] = f"{slide_id}_t{title_idx}"
            sub_idx = subtitle_element(slide, title_idx)
            if sub_idx is not None and "SUBTITLE" in placeholders:
                ids[placeholders["SUBTITLE"]] = f"{slide_id}_t{sub_idx}"
        reqs.append({"duplicateObject": {"objectId": source["objectId"], "objectIds": ids}})
    batch(slides, pid, reqs)
    write_layout_texts(slides, pid, deck.get("layout_texts", []), scale, fonts)
    style_layout_placeholders(slides, pid, deck, scale, fonts, PPTX_TITLE_DY)

    state = {"presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
             "scale": scale, "slides": []}
    # Placeholder sizes (needed to resize them) and any extra layout placeholders.
    created = execute(slides.presentations().get(
        presentationId=pid,
        fields="slides(objectId,pageElements(objectId,size),slideProperties/notesPage/notesProperties)"))
    page_elements = {s["objectId"]: s.get("pageElements", []) for s in created["slides"]}
    speaker_notes = {s["objectId"]: s.get("slideProperties", {}).get("notesPage", {})
                     .get("notesProperties", {}).get("speakerNotesObjectId") for s in created["slides"]}
    placeholder_dy = PPTX_TITLE_DY
    # Internal link targets: PDF page -> slide. A skipped overlay step maps to the kept
    # (last) step of its frame, which comes right after it.
    kept = sorted(s["page"] for s in deck["slides"])
    page_slide = {}
    for page in range(kept[-1] + 1):
        target = next(k for k in kept if k >= page)
        page_slide[page] = f"b2s_s{target:03}"

    # Phase 2: content, batched over slides. Each slide's requests come in parts (one per
    # element) so that a rejected batch can be narrowed down to the element at fault.
    refused: list[tuple[int, str]] = []

    def send(items: list[tuple[str, int, list[tuple[dict | None, list[dict]]]]]) -> None:
        reqs = [r for _, _, parts in items for _, rs in parts for r in rs]
        if not reqs:
            return
        try:
            batch(slides, pid, reqs)
            return
        except HttpError as e:
            if len(items) > 1:
                for item in items:
                    send([item])
                return
            print(f"warning: {items[0][0]}: batch rejected ({api_error(e)}); retrying element by element")
        slide_id, page, parts = items[0]
        for el, rs in parts:
            try:
                if rs:
                    batch(slides, pid, rs)
            except HttpError as e:
                print(f"warning: {slide_id}: {el['kind'] + ' ' + el['id'] if el else 'request'} rejected "
                      f"({api_error(e)})" + ("; using a picture of it instead" if el and el["kind"] != "image" else ""))
                if el and el["kind"] != "image":
                    refused.append((page, el["id"]))

    def template_on_slide(slide_id: str, key: tuple) -> dict:
        """The slide's copy of a template shape ({"id", "w", "h"}: its unscaled size in pt)."""
        j = keys.index(key)
        w, h = template_sizes[j]
        return {"id": f"{slide_id}_k{j}", "w": w, "h": h}

    pending: list[tuple[str, int, list]] = []
    pending_size = 0
    for slide in deck["slides"]:
        n = slide["page"]
        slide_id = f"b2s_s{n:03}"
        title_idx = title_element(slide)
        title_oid = f"{slide_id}_t{title_idx}" if title_idx is not None else None
        sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
        subtitle_oid = f"{slide_id}_t{sub_idx}" if sub_idx is not None else None
        ours = (f"{slide_id}_k", f"{slide_id}_f")  # template shapes and pictures from the .pptx
        parts: list[tuple[dict | None, list[dict]]] = [(None, [
            {"deleteObject": {"objectId": e["objectId"]}}
            for e in page_elements.get(slide_id, [])
            if e["objectId"] not in (title_oid, subtitle_oid) and not e["objectId"].startswith(ours)])]
        element_ids = []
        title_bars = [e for e in slide["elements"] if e["kind"] == "shape" and e.get("block") is not None
                      and not e.get("title_bar")]
        for i, el in enumerate(slide["elements"]):  # shapes, then pictures, then text on top
            el = placed(el, n)
            if el["kind"] == "shape":
                oid = f"{slide_id}_s{i}"
                key = template_key(el, scale)
                reqs = shape_requests(el, slide_id, oid, scale, template_on_slide(slide_id, key) if key else None)
            elif el["kind"] == "table":
                oid = f"{slide_id}_tab{i}"
                reqs = table_requests(el, slide_id, oid, scale, fonts)
            elif el["kind"] == "diagram":
                oid = f"{slide_id}_dg{i}"
                reqs = diagram_requests(el, slide_id, oid, scale, fonts,
                                        (lambda key, s=slide_id: template_on_slide(s, key)) if keys else None)
            elif el["kind"] == "image":
                # The picture came with the slide: move it to its place in the z-order.
                oid = f"{slide_id}_f{i}"
                reqs = [{"updatePageElementsZOrder": {"pageElementObjectIds": [oid], "operation": "BRING_TO_FRONT"}}]
                if el.get("number"):
                    reqs += number_box_requests(el["number"], slide_id, f"{oid}n", scale, fonts)
            else:
                oid = f"{slide_id}_t{i}"
                placeholder = None
                if oid in (title_oid, subtitle_oid):
                    size = next(e["size"] for e in page_elements[slide_id] if e["objectId"] == oid)
                    placeholder = {"base_w": size["width"]["magnitude"] / EMU_PER_PT,
                                   "base_h": size["height"]["magnitude"] / EMU_PER_PT, "dy": placeholder_dy}
                cx, cy = (el["bbox"][0] + el["bbox"][2]) / 2, (el["bbox"][1] + el["bbox"][3]) / 2
                bar = next((b["bbox"] for b in title_bars if b["bbox"][0] <= cx <= b["bbox"][2]
                            and b["bbox"][1] <= cy <= b["bbox"][3]), None)
                reqs = text_box_requests(el, slide_id, oid, scale, fonts, placeholder, page_slide, bar,
                                         text_right_limit(el, slide))
            parts.append((el, reqs))
            element_ids.append(oid)
        # The slide's copies of the template shapes have been duplicated from: remove them.
        extra = [{"deleteObject": {"objectId": f"{slide_id}_k{j}"}} for j in range(len(keys)) if uses_templates[n]]
        # Inline formula pictures move with their text: group them (placeholders can't be grouped).
        by_id = {el["id"]: oid for el, oid in zip(slide["elements"], element_ids)}
        anchored: dict[str, list[str]] = {}
        for el, oid in zip(slide["elements"], element_ids):
            if el.get("anchor") in by_id and by_id[el["anchor"]] not in (title_oid, subtitle_oid):
                anchored.setdefault(by_id[el["anchor"]], []).extend([oid, f"{oid}n"] if el.get("number") else [oid])
        grouped = set()
        for text_oid, pictures in anchored.items():
            extra.append({"groupObjects": {"groupObjectId": f"{text_oid}_g", "childrenObjectIds": [text_oid] + pictures}})
            grouped |= {text_oid, *pictures}
        # A beamer block (title bar and body shapes plus everything on them) moves as one.
        for bi, members in enumerate(block_groups(slide["elements"], element_ids, title_oid)):
            children = [f"{m}_g" if m in anchored else m for m in members if m not in grouped or m in anchored]
            if len(children) >= 2:
                extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_blk{bi}", "childrenObjectIds": children}})
                # A group takes the place of its topmost member, above a table lying on the
                # block (tables can't join the group): blocks are backdrops, send them back.
                extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [f"{slide_id}_blk{bi}"],
                                                           "operation": "SEND_TO_BACK"}})
        for ri, members in enumerate(rule_groups(slide["elements"], element_ids)):
            extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_rules{ri}", "childrenObjectIds": members}})
        if slide.get("notes") and speaker_notes.get(slide_id):
            extra.append({"insertText": {"objectId": speaker_notes[slide_id], "text": slide["notes"]}})
        if title_oid and len(slide["elements"]) > 1:
            # The placeholder was created with the slide, below everything added since.
            extra.append({"updatePageElementsZOrder": {"pageElementObjectIds": [o for o in (title_oid, subtitle_oid) if o],
                                                       "operation": "BRING_TO_FRONT"}})
        parts += [(None, [r]) for r in extra]
        size = sum(len(rs) for _, rs in parts)
        # Several slides per round trip; a slide's requests are never split across batches.
        if pending and pending_size + size > BATCH_MAX_REQUESTS:
            send(pending)
            pending, pending_size = [], 0
        pending.append((slide_id, n, parts))
        pending_size += size
        state["slides"].append({"page": n, "objectId": slide_id, "elements": element_ids})
        kinds = [el["kind"] for el in slide["elements"]]
        print(f"  slide {n + 1}: {kinds.count('text')} text boxes, {kinds.count('image')} pictures, "
              f"{kinds.count('shape')} shapes, {kinds.count('table')} tables")
    if pending:
        send(pending)
    batch(slides, pid, [{"deleteObject": {"objectId": s["objectId"]}} for s in sources])
    return state, refused
