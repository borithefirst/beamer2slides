"""Pictures placed by prediction: a formula picture over its hole, an overlay stretched over the
words it marks.
"""

from .classify import HOLE_PAD
from .emit_metrics import MATH_SPACE_EM, SYMBOL_ADVANCE_EM, FontMapper
from .fonts import google_font


def text_right_limit(el: dict, slide: dict) -> float | None:
    """How far right (PDF x) a text element's box may reach: inside a panel (a block body),
    as far from the panel's right edge as the text is from its left edge; elsewhere the
    mirrored left margin of the page, stopping short of anything to the right on the same
    lines (the other column, a picture)."""
    if el.get("role") not in ("body", "title", None) or not el["paragraphs"] or el.get("rotation"):
        return None
    x0, y0, x1, y1 = el["bbox"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    panels = [e["bbox"] for e in slide["elements"] if e["kind"] == "shape" and e.get("role") == "panel"
              and e["bbox"][0] <= cx <= e["bbox"][2] and e["bbox"][1] <= cy <= e["bbox"][3]]
    if panels:
        px0, _, px1, _ = min(panels, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        limit = px1 - max(x0 - px0, 2.0)
    else:
        margin = min((e["bbox"][0] for e in slide["elements"] if e["kind"] == "text" and e.get("role") in ("body", None)
                      and not e.get("rotation")),
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


def earlier_holes(p: dict, run: dict) -> list[tuple[float, float]]:
    """(x0, width) of the holes before `run` on its line (PDF pt)."""
    words = [b[6] for b in run.get("before", []) if len(b) >= 7]
    if not words:
        return []
    i = next(k for k, r in enumerate(p["runs"]) if r is run)
    return [(r["hole_x0"], r["hole"]) for r in p["runs"][:i]
            if r.get("hole") and min(words) <= r["hole_x0"] < run["hole_x0"]]


def space_shift(run: dict, em: float, holes: list[tuple[float, float]] = ()) -> float:
    """What word spaces add to a formula gap's position in Slides. The substitute's size
    calibration makes its glyphs wider and its spaces narrower than TeX's, evening out at word
    ends; a formula starts after a space, so it lands one space deficit early. TeX also
    stretches some spaces (after a colon, around math): Slides sets a plain space there.
    A gap holding an earlier hole (`holes`) becomes a space and that hole's no-break spaces."""
    if google_font(run["font"]):
        return 0.0  # the PDF's own font: its spaces too
    words = sorted((b[6], b[6] + b[0]) for b in run.get("before", []) if len(b) >= 7)
    if not words:
        return 0.0
    size, space = run["size"], SYMBOL_ADVANCE_EM[" "] * em
    ends = [(a[1], b[0]) for a, b in zip(words, words[1:])] + [(words[-1][1], run["hole_x0"])]
    shift, gaps = 0.0, []
    for a, b in ends:
        inside = [w for x, w in holes if a - 0.5 <= x < b]
        shift += sum(space + w for w in inside) - (b - a) if inside else 0.0
        gaps.append(None if inside else b - a)
    spaces = [g for g in gaps if g is not None and g > 0.15 * size]
    nominal = min(sorted(spaces)[len(spaces) // 2], 0.4 * size) if spaces else 0.0
    shift += sum(nominal - g for g in spaces)
    if gaps[-1] is None:
        shift += space  # (the gap's own PDF space went with the hole before)
    elif gaps[-1] > 0.15 * size:
        shift += space - nominal
    return shift


def slide_holes(slide: dict) -> list[tuple[dict, dict, dict, dict | None]]:
    """(text element, paragraph, hole run, its picture) for every hole on a slide, in text order.
    A picture covers most of its hole (it usually starts where the hole does, but a radical's
    sign can reach further left) on one of the paragraph's lines; holes on two lines can have
    the same x: each takes the nearest picture not taken yet."""
    pictures = [e for e in slide["elements"] if e["kind"] == "image" and e.get("anchor")]
    out, taken = [], set()
    for el in slide["elements"]:
        if el["kind"] != "text":
            continue
        for p in el["paragraphs"]:
            for run in p["runs"]:
                if not run.get("hole"):
                    continue
                a, b = run["hole_x0"] - HOLE_PAD, run["hole_x0"] - HOLE_PAD + run["hole"]
                near = [e for e in pictures if e["anchor"] == el["id"] and e["id"] not in taken
                        and min(b, e["bbox"][2]) - max(a, e["bbox"][0]) > 0.5 * min(b - a, e["bbox"][2] - e["bbox"][0])]
                pic = min(near, default=None, key=lambda e: (min(
                    abs((e["bbox"][1] + e["bbox"][3]) / 2 - line["baseline"] + 0.35 * run["size"]) for line in p["lines"]) // 4,
                    abs(e["bbox"][0] + HOLE_PAD - run["hole_x0"])))
                if pic:
                    taken.add(pic["id"])
                out.append((el, p, run, pic))
    return out


def hole_neighbours(p: dict, run: dict) -> tuple[float | None, float | None]:
    """PDF x where the word before a hole ends, if a space separates them (Slides has a word
    space there too), and where the word after it starts, if nothing separates them in the
    text (the hole reaches up to it)."""
    i = next(k for k, r in enumerate(p["runs"]) if r is run)
    words = [b[6] + b[0] for b in run.get("before", []) if len(b) >= 7]
    prev_end = None
    if i > 0 and p["runs"][i - 1]["text"].endswith(" ") and words and \
            not any(x >= max(words) for x, _ in earlier_holes(p, run)):
        prev_end = max(words)
    nxt = p["runs"][i + 1]["text"] if i + 1 < len(p["runs"]) else ""
    return prev_end, (run.get("next_x0") if not nxt[:1].isspace() else None)


def fit_holes(slide: dict, scale: float, fonts: FontMapper) -> dict:
    """The slide with each hole as wide as the PDF's room between the words around it, less the
    Slides word space before it, so the words after it keep their place. Never narrower than
    the picture."""
    widths = {}
    for _, p, run, pic in slide_holes(slide):
        prev_end, next_x0 = hole_neighbours(p, run)
        if pic is None:
            continue
        if next_x0 is None:
            # (render grows a formula picture to its glyphs' ink: an integral's overhang)
            if pic["bbox"][2] - pic["bbox"][0] > run["hole"]:
                widths[id(run)] = round(pic["bbox"][2] - pic["bbox"][0], 2)
            continue
        start = prev_end + SYMBOL_ADVANCE_EM[" "] * fonts(run, scale)[1] / scale if prev_end is not None else pic["bbox"][0]
        widths[id(run)] = round(max(pic["bbox"][2] - pic["bbox"][0], next_x0 - start), 2)
    if not widths:
        return slide
    return {**slide, "elements": [
        {**el, "paragraphs": [{**p, "runs": [{**r, "hole": widths[id(r)]} if id(r) in widths else r for r in p["runs"]]}
                              for p in el["paragraphs"]]} if el["kind"] == "text" else el
        for el in slide["elements"]]}


def hole_offset(p: dict, run: dict, pic: dict, scale: float, z: float) -> float:
    """Where a picture sits in its gap (slide pt from the gap's start): the Slides word space
    before the gap and the gap's room after the picture are shared in the PDF's proportion."""
    prev_end, next_x0 = hole_neighbours(p, run)
    if prev_end is None:
        return 0.0
    x0, _, x1, _ = pic["bbox"]
    room = max(0.0, run["hole"] - (x1 - x0)) * scale
    left = max(0.0, x0 - prev_end) * scale
    right = max(0.0, next_x0 - x1) * scale if next_x0 is not None else room
    space = SYMBOL_ADVANCE_EM[" "] * z
    return (space + room) * left / (left + right) - space if left + right > 0 else 0.0


def formula_shifts(slide: dict, scale: float, fonts: FontMapper) -> dict[str, float]:
    """PDF-point x offsets for inline formula pictures, so each sits over the gap where Slides
    will put it: the words before it on its line come out a little narrower or wider."""
    out = {}
    for el, p, run, pic in slide_holes(slide):
        if p["align"] != "left" or pic is None:
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
        shift += space_shift(run, em, earlier_holes(p, run)) + hole_offset(p, run, pic, scale, em * scale) / scale
        if abs(shift) >= 0.2:
            out[pic["id"]] = shift
    return out


OVERLAY_STRETCH = 0.1  # how much a graphic between words may widen or narrow with predicted words
OVERLAY_STRETCH_MEASURED = 0.3  # ... with measured ones (a brace spans its phrase)


def mark_drifts(el: dict, scale: float, fonts: FontMapper) -> list[float]:
    """Predicted drift (PDF pt, Slides minus PDF) of each of an overlay's marks: a formula gap
    starting at the mark (formula_shifts)."""
    drifts = []
    for m in el["marks"]:
        run = {"text": " ", "font": m["font"], "family": m["family"], "size": m["size"], "bold": m["bold"],
               "italic": m["italic"], "hole": 1.0, "hole_x0": m["hole_x0"], "before": m["before"]}
        probe = {"elements": [{"kind": "text", "id": "mark", "paragraphs": [
            {"align": "left", "runs": [run], "lines": [{"baseline": 0.35 * m["size"]}]}]},
                              {"kind": "image", "id": "gap", "anchor": "mark",
                               "bbox": [m["hole_x0"] - HOLE_PAD, 0, m["hole_x0"], 0]}]}
        # (the gap's picture starts HOLE_PAD before the gap; shifts under 0.2 pt are not reported)
        drifts.append(formula_shifts(probe, scale, fonts).get("gap", HOLE_PAD) - HOLE_PAD + m.get("pads", 0))
    return drifts


def fit_overlay(bbox: list[float], drifts: list[tuple[float, float]], cap: float) -> tuple[float, float]:
    """PDF x extent of a picture following its marks' drifts ((x, drift)): moved by the mean
    drift and stretched by the least-squares slope (at most `cap`) when the marks are 10 pt apart or more."""
    xs = [x for x, _ in drifts]
    mx, md = sum(xs) / len(xs), sum(d for _, d in drifts) / len(drifts)
    spread = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (d - md) for x, d in drifts) / spread if max(xs) - min(xs) >= 10 else 0.0
    slope = max(-cap, min(cap, slope))
    x0, _, x1, _ = bbox
    return x0 + md + slope * (x0 - mx), x1 + md + slope * (x1 - mx)


def overlay_boxes(slide: dict, scale: float, fonts: FontMapper) -> dict[str, tuple[float, float]]:
    """PDF x extents for graphics drawn over or at words (classify.overlay: arrows, braces,
    callouts), following where Slides sets those words, as predicted (mark_drifts); measure_places
    corrects them on the thumbnail."""
    return {el["id"]: fit_overlay(el["bbox"], list(zip([m["x"] for m in el["marks"]], mark_drifts(el, scale, fonts))),
                                  OVERLAY_STRETCH)
            for el in slide["elements"] if el.get("marks")}
