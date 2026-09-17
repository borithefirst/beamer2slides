"""Stage 4: build the Google Slides deck from deck.json and the background images."""

import hashlib
import io
import json
import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from .fonts import font_info, google_font
from .google_auth import credentials, drive_service, slides_service
from .gslides import EMU_PER_PT, emu, execute, pt

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = ROOT / "calibration" / "fonts.json"
SLIDE_W = 720.0
UPLOAD_THREADS = 6
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
CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM = 0.525, 0.6

BULLET_PRESETS = {
    "arrow": "BULLET_ARROW3D_CIRCLE_SQUARE",  # ➢ is the closest preset glyph to beamer's ▶
    "disc": "BULLET_DISC_CIRCLE_SQUARE",
    "number": "NUMBERED_DIGIT_ALPHA_ROMAN",
    "number_parens": "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS",
}


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


def bullet_preset(bullet: dict) -> str:
    text = bullet.get("text", "")
    if bullet["kind"] == "number" or (bullet["kind"] == "image" and text.isdigit()):
        return BULLET_PRESETS["number_parens" if ")" in text else "number"]
    if bullet["kind"] == "glyph" and text in "▶►▸‣":
        return BULLET_PRESETS["arrow"]
    return BULLET_PRESETS["disc"]


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
    """The gap under an inline formula picture: no-break spaces in a monospaced font, sized so
    they are exactly as wide as the formula and no taller than the line."""
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

    left_pdf = min(p["bullet"]["bbox"][0] if p["bullet"] else p["text_x0"] for p in paras)
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
    if right_limit and not multiline and aligns == {"left"} and not placeholder:
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
    tabbed = "\n".join(("\t" * p["level"] if p["bullet"] else "") + t for p, t in zip(paras, texts))
    reqs.append({"insertText": {"objectId": object_id, "text": tabbed, "insertionIndex": 0}})

    # Bullets: contiguous ranges with the same preset. createParagraphBullets consumes the
    # leading tabs (they set the nesting level), so ranges are applied last-to-first.
    starts_tabbed, pos = [], 0
    for p, t in zip(paras, texts):
        starts_tabbed.append(pos)
        pos += (p["level"] if p["bullet"] else 0) + len(t) + 1
    ranges = []
    for i, p in enumerate(paras):
        preset = bullet_preset(p["bullet"]) if p["bullet"] else None
        if preset and ranges and ranges[-1][2] == preset and ranges[-1][1] == i - 1:
            ranges[-1][1] = i
        elif preset:
            ranges.append([i, i, preset])
    # A bullet keeps the text style it was created with unless its whole paragraph later
    # shares one style (mixed paragraphs, e.g. with inline math, never update it). So give
    # every paragraph its base family and size before the bullets exist.
    for p, start, size in zip(paras, starts_tabbed, base_sizes):
        family = fonts(p["runs"][0], scale)[0] if p["runs"] else "Lato"
        length = (p["level"] if p["bullet"] else 0) + len("".join(r["text"] for r in p["runs"]))
        if length:
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "fields": "fontFamily,fontSize",
                "style": {"fontFamily": family, "fontSize": pt(size)},
                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + length},
            }})
    for first, last, preset in reversed(ranges):
        end = starts_tabbed[last] + (paras[last]["level"]) + len(texts[last])
        reqs.append({"createParagraphBullets": {
            "objectId": object_id, "bulletPreset": preset,
            "textRange": {"type": "FIXED_RANGE", "startIndex": starts_tabbed[first], "endIndex": end},
        }})

    # From here on indices refer to the final text, without tabs.
    pos = 0
    for p, t, ratio, above in zip(paras, texts, ratios, space_above):
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
                          "underline": bool(run.get("underline")),
                          "baselineOffset": {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT"}.get(run.get("script"), "NONE")})
            fields = fields + ["smallCaps", "foregroundColor", "underline", "baselineOffset"]
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
            reqs.append({"updateTextStyle": {
                "objectId": object_id, "style": style, "fields": fields,
                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": start + len(run["text"])},
            }})
            start += len(run["text"])

        # Code lines carry their indentation as leading spaces already.
        # Centred and right-aligned paragraphs place themselves: an indent would only offset them.
        text_indent = 0.0 if el.get("code") or p["align"] != "left" else (p["text_x0"] - left_pdf) * scale
        if p["bullet"]:
            # Slides ends the bullet glyph BULLET_GAP before indentFirstLine, whatever the glyph.
            first_indent = (p["bullet"]["bbox"][2] - left_pdf) * scale + BULLET_GAP
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


def build_pptx(page_w: float, page_h: float, keys: list[tuple], layouts: list[str]) -> io.BytesIO:
    """The deck's starting point: an empty .pptx with the PDF's page size (presentations.create
    ignores pageSize) and one template slide per layout holding the template shapes."""
    from lxml import etree
    from pptx import Presentation
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Emu, Pt

    prs = Presentation()
    height = SLIDE_W * page_h / page_w
    ratio = height / (prs.slide_height / EMU_PER_PT)
    prs.slide_width, prs.slide_height = Emu(round(SLIDE_W * EMU_PER_PT)), Emu(round(height * EMU_PER_PT))
    if abs(ratio - 1) > 1e-3:  # the default template's placeholders are laid out for 4:3
        for page in [prs.slide_master, *prs.slide_layouts]:
            for shape in page.placeholders:
                if shape.top is not None and shape.height is not None:
                    shape.top, shape.height = Emu(round(shape.top * ratio)), Emu(round(shape.height * ratio))
    kinds = {"ROUND_RECTANGLE": MSO_SHAPE.ROUNDED_RECTANGLE, "ROUND_2_SAME_RECTANGLE": MSO_SHAPE.ROUND_2_SAME_RECTANGLE,
             "RECTANGLE": MSO_SHAPE.RECTANGLE, "ELLIPSE": MSO_SHAPE.OVAL, "DIAMOND": MSO_SHAPE.DIAMOND,
             "TRIANGLE": MSO_SHAPE.ISOSCELES_TRIANGLE}
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    for layout in layouts:
        slide = prs.slides.add_slide(prs.slide_layouts[TEMPLATE_LAYOUTS[layout]])
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
        inside = bool(node["shape"]) and label_inside(node) and template is not None
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
        if text:
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
        if len(members) == 2:  # a node with its label outside: they move together
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
    the text, pictures and tables lying on them."""
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
            if el["kind"] in ("text", "image", "table") and oid != title_oid and not el.get("anchor"):
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
    if el.get("role") not in ("body", None) or not el["paragraphs"]:
        return None
    x0, y0, x1, y1 = el["bbox"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    panels = [e["bbox"] for e in slide["elements"] if e["kind"] == "shape" and e.get("role") == "panel"
              and e["bbox"][0] <= cx <= e["bbox"][2] and e["bbox"][1] <= cy <= e["bbox"][3]]
    if panels:
        px0, _, px1, _ = min(panels, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        limit = px1 - max(x0 - px0, 2.0)
    else:
        margin = min(e["bbox"][0] for e in slide["elements"] if e["kind"] == "text" and e.get("role") in ("body", None))
        limit = slide["size"][0] - margin
        size = el["paragraphs"][0]["size"]
        for o in slide["elements"]:
            if o is el or o["kind"] == "shape":
                continue
            ox0, oy0, ox1, oy1 = o["bbox"]
            if ox0 >= x1 - 1 and oy0 < y1 and oy1 > y0:
                limit = min(limit, ox0 - size)
    return limit if limit > x1 else None


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
                shift = sum(w * (fonts.width_ratio(font, family, bold, italic) - 1)
                            for w, font, family, bold, italic in run.get("before", []))
                pic = next((e for e in pictures if e["anchor"] == el["id"]
                            and abs(e["bbox"][0] + 1 - run["hole_x0"]) < 0.6), None)
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


def image_request(el: dict, slide_id: str, object_id: str, scale: float, url: str) -> dict:
    x0, y0, x1, y1 = el["bbox"]
    return {"createImage": {
        "objectId": object_id, "url": url,
        "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": emu((x1 - x0) * scale), "height": emu((y1 - y0) * scale)},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": round(x0 * scale * EMU_PER_PT), "translateY": round(y0 * scale * EMU_PER_PT)},
        },
    }}


# ---------------------------------------------------------------- presentation + assets

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


def asset_folder(drive, name: str) -> str:
    q = (f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    found = execute(drive.files().list(q=q, fields="files(id)")).get("files", [])
    if found:
        return found[0]["id"]
    return execute(drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id"))["id"]


def upload_public_png(drive, path: Path, folder: str) -> tuple[str, str]:
    f = execute(drive.files().create(
        body={"name": path.name, "parents": [folder]},
        media_body=MediaFileUpload(str(path), mimetype="image/png"), fields="id"))
    perm = execute(drive.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}, fields="id"))
    return f["id"], perm["id"]


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


def batch_with_image_retry(slides, pid: str, reqs: list[dict], attempts: int = 4) -> None:
    for attempt in range(attempts):
        try:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
            return
        except HttpError as e:
            if "problem retrieving the image" not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))


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


def emit(deck: dict, out: Path, title: str, new_deck: bool = False, keep_assets: bool = False) -> dict:
    slides, drive = slides_service(), drive_service()
    page_w, page_h = deck["slides"][0]["size"]
    scale = SLIDE_W / page_w
    fonts = FontMapper()
    deck = {**deck, "slides": [{**s, "elements": merge_blocks(s["elements"])} for s in deck["slides"]]}

    def slide_layout(slide: dict) -> tuple[str, str | None]:
        """The title page uses the TITLE layout (centered title), frames with a title TITLE_ONLY."""
        if title_element(slide) is None:
            return "BLANK", None
        return ("TITLE", "CENTERED_TITLE") if slide.get("title_page") else ("TITLE_ONLY", "TITLE")

    # Every run starts from an imported .pptx: it sets the page size and brings template
    # shapes (shadows, exact corner radii) on one template slide per layout. Slides needing
    # templates are duplicates of their layout's template slide; the templates go at the end.
    keys = list(dict.fromkeys(k for s in deck["slides"] for e in s["elements"] for k in element_template_keys(e, scale)))
    uses_templates = {s["page"]: any(element_template_keys(e, scale) for e in s["elements"]) for s in deck["slides"]}
    layouts = [l for l in TEMPLATE_LAYOUTS if any(uses_templates[s["page"]] and slide_layout(s)[0] == l
                                                  for s in deck["slides"])]
    existing = None if new_deck else existing_presentation(drive, out)
    if existing:
        print(f"updating existing deck {existing}")
    pres = import_presentation(slides, drive, title, page_w, page_h, build_pptx(page_w, page_h, keys, layouts), existing)
    pid = pres["presentationId"]
    templates = {}
    for layout, tpl_slide in zip(layouts, pres.get("slides", [])):
        els = tpl_slide.get("pageElements", [])
        shapes = [e for e in els if "placeholder" not in e.get("shape", {})]
        if len(shapes) != len(keys):
            raise RuntimeError(f"template slide {layout}: {len(shapes)} shapes imported, expected {len(keys)}")
        templates[layout] = {
            "id": tpl_slide["objectId"], "shapes": [e["objectId"] for e in shapes],
            "placeholders": {e["shape"]["placeholder"]["type"]: e["objectId"] for e in els if "placeholder" in e.get("shape", {})},
            "sizes": [(e["size"]["width"]["magnitude"] / (EMU_PER_PT if e["size"]["width"]["unit"] == "EMU" else 1),
                       e["size"]["height"]["magnitude"] / (EMU_PER_PT if e["size"]["height"]["unit"] == "EMU" else 1))
                      for e in shapes],
        }
    folder = asset_folder(drive, "beamer2slides assets")

    state = {"presentationId": pid, "url": f"https://docs.google.com/presentation/d/{pid}/edit",
             "scale": scale, "slides": []}
    uploaded: list[tuple[str, str]] = []
    urls: dict[str, str] = {}  # local file -> public URL
    try:
        # Backgrounds: identical ones are uploaded once, and the most common one becomes the
        # master's background (the deck's theme): its slides inherit it, and new slides too.
        bg_key = {s["page"]: background_key(s, out) for s in deck["slides"]}
        bg_file = {bg_key[s["page"]]: s["background"] for s in deck["slides"] if not s.get("background_color")}
        counts = Counter(bg_key.values())
        shared = counts.most_common(1)[0][0] if counts and counts.most_common(1)[0][1] >= 2 else None
        files = list(bg_file.values()) + \
                [e["file"] for s in deck["slides"] for e in s["elements"] if e["kind"] == "image"]
        files = list(dict.fromkeys(files))
        creds = credentials()
        local = threading.local()

        def upload(f: str) -> tuple[str, str]:
            if not hasattr(local, "drive"):
                local.drive = drive_service(creds)
            return upload_public_png(local.drive, out / f, folder)

        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            for f, (file_id, perm_id) in zip(files, pool.map(upload, files)):
                uploaded.append((file_id, perm_id))
                urls[f] = f"https://drive.google.com/uc?export=view&id={file_id}"
        time.sleep(5)  # a fresh "anyone with the link" permission takes a moment to apply

        def fill(key: tuple) -> dict:
            if key[0] == "color":
                return {"solidFill": {"color": rgb(key[1])["opaqueColor"]}}
            return {"stretchedPictureFill": {"contentUrl": urls[bg_file[key]]}}

        # Phase 1: slides with backgrounds. Slides with a frame title use the TITLE_ONLY layout
        # and get their title placeholder mapped to our object ID.
        # Layouts can't be switched back to inheriting through the API, so they get the fill too.
        reqs = [{"updatePageProperties": {
            "objectId": page["objectId"], "fields": "pageBackgroundFill",
            "pageProperties": {"pageBackgroundFill": fill(shared or ("color", "#ffffff"))}}}
            for page in pres.get("masters", []) + pres.get("layouts", [])]
        for position, slide in enumerate(deck["slides"]):
            n = slide["page"]  # PDF page index; slides may skip pages (overlays)
            slide_id = f"b2s_s{n:03}"
            title_idx = title_element(slide)
            layout, placeholder = slide_layout(slide)
            sub_idx = subtitle_element(slide, title_idx) if title_idx is not None else None
            if uses_templates[n]:
                tpl = templates[layout]
                ids = {tpl["id"]: slide_id, **{oid: f"{slide_id}_k{j}" for j, oid in enumerate(tpl["shapes"])}}
                if title_idx is not None:
                    ids[tpl["placeholders"][placeholder]] = f"{slide_id}_t{title_idx}"
                if sub_idx is not None and "SUBTITLE" in tpl["placeholders"]:
                    ids[tpl["placeholders"]["SUBTITLE"]] = f"{slide_id}_t{sub_idx}"
                reqs.append({"duplicateObject": {"objectId": tpl["id"], "objectIds": ids}})
                reqs.append({"updateSlidesPosition": {"slideObjectIds": [slide_id], "insertionIndex": position}})
            else:
                create = {"objectId": slide_id, "insertionIndex": position,
                          "slideLayoutReference": {"predefinedLayout": layout}}
                if title_idx is not None:
                    create["placeholderIdMappings"] = [{"layoutPlaceholder": {"type": placeholder, "index": 0},
                                                        "objectId": f"{slide_id}_t{title_idx}"}]
                    if sub_idx is not None:  # authors, institute, date: the title slide's subtitle
                        create["placeholderIdMappings"].append({"layoutPlaceholder": {"type": "SUBTITLE", "index": 0},
                                                                "objectId": f"{slide_id}_t{sub_idx}"})
                reqs.append({"createSlide": create})
            if bg_key[n] != shared:
                reqs.append({"updatePageProperties": {"objectId": slide_id, "fields": "pageBackgroundFill",
                                                      "pageProperties": {"pageBackgroundFill": fill(bg_key[n])}}})
        batch_with_image_retry(slides, pid, reqs)
        write_layout_texts(slides, pid, deck.get("layout_texts", []), scale, fonts)
        style_layout_placeholders(slides, pid, deck, scale, fonts, PPTX_TITLE_DY)

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
        source_pdf = out / "slides.pdf" if (out / "slides.pdf").exists() else Path(deck["source"]["pdf"])

        def fallback_picture(el: dict, page: int, slide_id: str) -> None:
            """An element the API refused, as a picture cropped from the original page."""
            path = out / "figures" / f"fallback-{el['id']}.png"
            rect = pymupdf.Rect(el["bbox"]) + (-2, -2, 2, 2)
            src = pymupdf.open(source_pdf)
            path.parent.mkdir(parents=True, exist_ok=True)
            src[page].get_pixmap(matrix=pymupdf.Matrix(6, 6), clip=rect, alpha=False).save(path)
            file_id, perm_id = upload_public_png(drive, path, folder)
            uploaded.append((file_id, perm_id))
            time.sleep(5)
            picture = {**el, "bbox": [rect.x0, rect.y0, rect.x1, rect.y1]}
            url = f"https://drive.google.com/uc?export=view&id={file_id}"
            batch_with_image_retry(slides, pid, [image_request(picture, slide_id, f"{slide_id}_fb_{el['id']}", scale, url)])

        def send(batch: list[tuple[str, int, list[tuple[dict | None, list[dict]]]]]) -> None:
            reqs = [r for _, _, parts in batch for _, rs in parts for r in rs]
            if not reqs:
                return
            try:
                batch_with_image_retry(slides, pid, reqs)
                return
            except HttpError as e:
                if len(batch) > 1:
                    for item in batch:
                        send([item])
                    return
                print(f"warning: {batch[0][0]}: batch rejected ({api_error(e)}); retrying element by element")
            slide_id, page, parts = batch[0]
            for el, rs in parts:
                try:
                    if rs:
                        batch_with_image_retry(slides, pid, rs)
                except HttpError as e:
                    print(f"warning: {slide_id}: {el['kind'] + ' ' + el['id'] if el else 'request'} rejected "
                          f"({api_error(e)})" + ("; using a picture of it instead" if el and el["kind"] != "image" else ""))
                    if el and el["kind"] != "image":
                        fallback_picture(el, page, slide_id)

        def template_on_slide(slide_id: str, key: tuple) -> dict:
            """The slide's copy of a template shape ({"id", "w", "h"}: its unscaled size in pt)."""
            j = keys.index(key)
            w, h = next(iter(templates.values()))["sizes"][j]
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
            parts: list[tuple[dict | None, list[dict]]] = [(None, [
                {"deleteObject": {"objectId": e["objectId"]}}
                for e in page_elements.get(slide_id, [])
                if e["objectId"] not in (title_oid, subtitle_oid) and not e["objectId"].startswith(f"{slide_id}_k")])]
            element_ids = []
            shifts = formula_shifts(slide, scale, fonts)
            title_bars = [e for e in slide["elements"] if e["kind"] == "shape" and e.get("block") is not None
                          and not e.get("title_bar")]
            for i, el in enumerate(slide["elements"]):  # shapes, then pictures, then text on top
                if el["id"] in shifts:
                    el = {**el, "bbox": [el["bbox"][0] + shifts[el["id"]], el["bbox"][1],
                                         el["bbox"][2] + shifts[el["id"]], el["bbox"][3]]}
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
                                            (lambda key, s=slide_id: template_on_slide(s, key)) if templates else None)
                elif el["kind"] == "image":
                    oid = f"{slide_id}_f{i}"
                    reqs = [image_request(el, slide_id, oid, scale, urls[el["file"]])]
                    if el.get("alt"):
                        reqs.append({"updatePageElementAltText": {"objectId": oid, "description": el["alt"],
                                                                  "title": {"math": "Formula", "icon": "Icon"}.get(el["role"], "Figure")}})
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
                    anchored.setdefault(by_id[el["anchor"]], []).append(oid)
            grouped = set()
            for text_oid, pictures in anchored.items():
                extra.append({"groupObjects": {"groupObjectId": f"{text_oid}_g", "childrenObjectIds": [text_oid] + pictures}})
                grouped |= {text_oid, *pictures}
            # A beamer block (title bar and body shapes plus everything on them) moves as one.
            for bi, members in enumerate(block_groups(slide["elements"], element_ids, title_oid)):
                children = [f"{m}_g" if m in anchored else m for m in members if m not in grouped or m in anchored]
                if len(children) >= 2:
                    extra.append({"groupObjects": {"groupObjectId": f"{slide_id}_blk{bi}", "childrenObjectIds": children}})
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
        if templates:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": [
                {"deleteObject": {"objectId": t["id"]}} for t in templates.values()]}))
    finally:
        creds = credentials()
        local = threading.local()

        def revoke(item: tuple[str, str]) -> None:
            file_id, perm_id = item
            if not hasattr(local, "drive"):
                local.drive = drive_service(creds)
            try:
                execute(local.drive.permissions().delete(fileId=file_id, permissionId=perm_id))
            except Exception as e:  # keep revoking the others
                print(f"warning: could not revoke public link on {file_id}: {e}")
            if not keep_assets:
                # Slides keeps its own copy of every inserted image: the upload was only a
                # transport. Into the trash (recoverable), not deleted.
                try:
                    execute(local.drive.files().update(fileId=file_id, body={"trashed": True}))
                except Exception as e:
                    print(f"warning: could not move upload {file_id} to the trash: {e}")

        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            list(pool.map(revoke, uploaded))
    (out / "emit.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state
