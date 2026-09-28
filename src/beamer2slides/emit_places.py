"""Pictures placed by measurement: scratch slides with coloured holes and words, read off Google's
thumbnails; panels grown to the words Slides sets.
"""

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .emit_holes import (
    OVERLAY_STRETCH_MEASURED, fit_overlay, hole_neighbours, hole_offset, mark_drifts, slide_holes,
    text_right_limit,
)
from .emit_metrics import LINE_EM, PAD_X, SLIDE_W, SYMBOL_ADVANCE_EM, FontMapper, rgb, u16
from .emit_pptx import api_error, batch, template_key
from .emit_text import box_lines, hole_runs, hugs, in_sentence, text_box_requests
from .gapi import HttpError
from .google_auth import credentials_for_threads, fetcher_for_threads, shared_service, slides_service
from .gslides import per_thread

# The prediction (emit_holes) is off by several points now and then (fallback fonts, kerning,
# wraps). So each slide with holes or overlays gets a scratch copy of those text boxes on a white
# slide, every hole run and every word an overlay's marks lie on highlighted in a mark colour and
# all text black; Google's thumbnail of it shows where the gaps and words really are, and the
# pictures are moved (overlays also stretched) there before they are grouped.

HOLE_MARKS = ["#ff00ff", "#00ffff", "#ffff00", "#00ff00"]  # 0/255 channels only (mark_alpha)
MARK_CORE = 0.9  # a pixel at least this much covered by a mark is inside it


def mark_alpha(img: np.ndarray, color: str) -> np.ndarray:
    """How much of each pixel of an RGB thumbnail a highlight in `color` covers (white page).
    Dark glyph pixels have the colour's full channels low too: they count as uncovered."""
    c = [int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    img = img.astype(np.float32)
    full = np.min([img[..., i] for i in range(3) if c[i] == 255], axis=0)
    alpha = np.mean([(255 - img[..., i]) / 255 for i in range(3) if c[i] == 0], axis=0)
    return np.where(full >= 200, np.clip(alpha, 0.0, 1.0), 0.0)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges [a, b] of consecutive True values."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(idx) > 1)
    return [(int(g[0]), int(g[-1])) for g in np.split(idx, cuts + 1)]


def _edges(profile: np.ndarray, a: int, b: int) -> tuple[float, float]:
    """Sub-pixel extent of a covered range [a, b]: partly covered neighbours add their share."""
    lo = a - (profile[a - 1] if a > 0 else 0.0)
    hi = b + 1 + (profile[b + 1] if b + 1 < profile.size else 0.0)
    return float(lo), float(hi)


def find_marks(alpha: np.ndarray, px_per_pt: float) -> list[tuple[float, float, float, float]]:
    """Highlighted rectangles (x0, y0, x1, y1 in pt) in a mark_alpha map. A glyph lying over a
    highlight hides it only in some rows, so columns count as covered if any row of the band is."""
    out = []
    core = alpha >= MARK_CORE
    for r0, r1 in _runs(core.sum(axis=1) >= 3):
        cols = alpha[r0:r1 + 1].max(axis=0)
        for c0, c1 in _runs(cols >= MARK_CORE):
            if c1 - c0 < 2:
                continue
            rows = alpha[:, c0:c1 + 1].max(axis=1)
            x0, x1 = _edges(cols, c0, c1)
            y0, y1 = _edges(rows, r0, r1)
            out.append((x0 / px_per_pt, y0 / px_per_pt, x1 / px_per_pt, y1 / px_per_pt))
    return out


def pick_gap(marks: list[tuple[float, float, float, float]], x0: float, cy: float, width: float,
             pitch: float) -> tuple[float, float] | None:
    """(dx, dy) from the predicted gap start `x0` and line middle `cy` (slide pt) to the mark as
    wide as the hole that lies nearest; dy is whole line pitches (a line Slides wrapped
    differently), 0 on the predicted line."""
    fits = [m for m in marks if abs((m[2] - m[0]) - width) <= max(1.5, 0.12 * width)]
    if not fits:
        return None
    m = min(fits, key=lambda m: abs(m[0] - x0) + abs((m[1] + m[3]) / 2 - cy))
    lines = round(((m[1] + m[3]) / 2 - cy) / pitch)
    return m[0] - x0, lines * pitch


def ink_end(img: np.ndarray, px_per_pt: float, y0: float, y1: float, x0: float, x1: float) -> float | None:
    """Right end (pt) of the dark text pixels between rows y0..y1 and columns x0..x1 (pt)."""
    h, w = img.shape[:2]
    a, b = max(0, int(x0 * px_per_pt)), min(w, int(math.ceil(x1 * px_per_pt)))
    band = img[max(0, int(y0 * px_per_pt)):min(h, int(math.ceil(y1 * px_per_pt))), a:b]
    cols = np.flatnonzero((band.max(axis=2) < 110).any(axis=0)) if band.size else []
    return (a + cols[-1] + 1) / px_per_pt if len(cols) else None


def slides_texts(el: dict, scale: float, fonts: FontMapper) -> list[str]:
    """Each paragraph's text as its text box ends up holding it (holes as their no-break spaces)."""
    return ["".join(r["text"] for r in hole_runs(p["runs"], scale, fonts)) for p in el["paragraphs"]]


def mark_words(overlay: dict, text: dict, scale: float, fonts: FontMapper) -> list[dict]:
    """The words an overlay's marks lie on, in the final text of its anchor's text box:
    {"range": (start, end), "marks": [(mark index, 0 left edge / 1 right edge)], "width": PDF
    width or None}. A right edge ends the last word before it (the whole run of words before it
    on its line finds it); a left edge starts the word a right edge on the same line ends, or else
    the word after the ones before it. Marks whose words aren't found are left out."""
    paras = slides_texts(text, scale, fonts)
    # (ranges in UTF-16 units, as Slides counts: u16)
    starts = [sum(u16(t) + 1 for t in paras[:i]) for i in range(len(paras))]
    tokens = [[(u16(t[:m.start()]), u16(t[:m.end()]), m.group()) for m in re.finditer(r"\S+", t)] for t in paras]

    def locate(want: list[str]) -> tuple[int, int] | None:
        """(paragraph, index of the last token) of a run of words; shorter tails if unique."""
        for k in range(len(want), 0, -1):
            hits = [(pi, j) for pi, toks in enumerate(tokens) for j in range(k - 1, len(toks))
                    if [t[2] for t in toks[j - k + 1:j + 1]] == want[-k:]]
            if hits and (k == len(want) or len(hits) == 1):
                return hits[0]
        return None

    words: dict[tuple, dict] = {}

    def add(key: tuple[int, int, int], mark: tuple[int, int], width: float | None) -> None:
        pi, j0, j1 = key
        w = words.setdefault(key, {"range": (starts[pi] + tokens[pi][j0][0], starts[pi] + tokens[pi][j1][1]),
                                   "marks": [], "width": width})
        w["marks"].append(mark)
        w["width"] = w["width"] or width

    ends, lefts = [], []  # (mark index, texts before the word, its closed x0, word key)
    for i, m in enumerate(overlay["marks"]):
        before = m["before"]
        if not (before and abs(before[-1][6] + before[-1][0] - m["hole_x0"]) <= 0.3):
            lefts.append(i)
            continue
        n = len(before[-1][5].split())
        hit = locate(" ".join(b[5] for b in before).split())
        if hit and n and hit[1] - n + 1 >= 0:
            key = (hit[0], hit[1] - n + 1, hit[1])
            ends.append((i, tuple(b[5] for b in before[:-1]), before[-1][6], key))
            add(key, (i, 1), before[-1][0])
    for i in lefts:
        m = overlay["marks"][i]
        texts = tuple(b[5] for b in m["before"])
        # (the same words before and x0 on two lines, "Stochastic" and "but" at the left margin:
        # classify lists a word's left edge right before its right edge)
        key = min(((abs(j - i - 1), k) for j, t, x0, k in ends if t == texts and abs(x0 - m["hole_x0"]) <= 0.3),
                  default=(0, None))[1]
        if key is None and texts:
            hit = locate(" ".join(texts).split())
            if hit and hit[1] + 1 < len(tokens[hit[0]]):
                key = (hit[0], hit[1] + 1, hit[1] + 1)
        if key is not None:
            add(key, (i, 0), None)
    return list(words.values())


def pick_word(marks: list[tuple[float, float, float, float]], x0: float, width: float | None,
              top: float, bottom: float) -> tuple[float, float, float, float] | None:
    """The highlighted word (slide pt) starting nearest the predicted `x0`, within its text box's
    rows top..bottom, about as wide as the PDF word if `width` is known, at most 25 pt away."""
    fits = [m for m in marks if top <= (m[1] + m[3]) / 2 <= bottom and abs(m[0] - x0) <= 25
            and (width is None or abs((m[2] - m[0]) - width) <= max(3.0, 0.3 * width))]
    return min(fits, key=lambda m: abs(m[0] - x0)) if fits else None


# The least room (em of the words' size) grown_panels keeps between a line's last word and its
# panel's right edge; beamer's block keeps 4 pt at 10.9 pt (0.37 em).
PANEL_MARGIN_EM = 0.5


def grown_panels(slide: dict, scale: float, fonts: FontMapper) -> dict:
    """The slide with each panel shape widened rightwards by as much as the words on it come
    out wider in Slides than in the PDF, so that they keep the PDF's inner margin (a block
    body in Lato runs a few points longer than in LM Sans, and its last word sat on the
    panel's edge or past it). A block's title bar and body grow together, never past the
    page's right edge; left-aligned text only (centred text grows both ways: its box does).

    A panel grows only where a line needs it: as far as the line keeps the panel's inner margin
    (the least of its words' left margins on it and the PDF's own right margin of that line),
    never further than the words grew. A short or ragged line with room to spare left the
    panel wider than the PDF's for nothing (r2_themes_v5 s3, r3_dense_v1 s10)."""
    els = slide["elements"]
    panels = [(i, e) for i, e in enumerate(els) if e["kind"] == "shape" and e.get("role") == "panel"]
    if not panels:
        return slide
    homes: list[tuple[int, dict, dict]] = []  # (panel index, panel, text element on it)
    for el in els:
        if el["kind"] != "text" or el.get("role") == "title" or el.get("rotation") or not el["paragraphs"]:
            continue
        x0, y0, x1, y1 = el["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        home = [(i, e) for i, e in panels if e["bbox"][0] <= cx <= e["bbox"][2] and e["bbox"][1] <= cy <= e["bbox"][3]]
        if home:
            homes.append((*home[-1], el))  # (the topmost: creation order is z-order, and a block's shadow lies under it)
    # (a panel's inner margin: how close its words come to its left edge; a block's title bar and
    # body are one panel for this)
    group = {i: (e.get("block") if e.get("block") is not None else ("p", i)) for i, e in panels}
    pads: dict = {}
    for i, panel, el in homes:
        pad = el["bbox"][0] - panel["bbox"][0]
        if pad >= 0:
            pads[group[i]] = min(pads.get(group[i], math.inf), pad)
    over: dict[int, float] = {}
    for i, panel, el in homes:
        paras = [{**p, "runs": hole_runs(in_sentence(p["runs"]), scale, fonts)} for p in el["paragraphs"]]
        measured = box_lines(paras, [hugs(p) for p in paras], scale, fonts)
        if not measured or any(g is None for g in measured):
            continue  # (every paragraph measured: an unmeasured one could be the widest)
        right = max(line["x1"] for p in paras for line in p["lines"])
        if right >= panel["bbox"][2]:
            continue
        slides_right = max(g[0] for g in measured) / scale
        # (a list's indent or a tcolorbox's wide padding is no margin the words need: half an em
        # keeps them off the edge)
        em = max(p["size"] for p in paras)
        margin = min(pads.get(group[i], 0.0), panel["bbox"][2] - right, PANEL_MARGIN_EM * em)
        grow = min(slides_right - right, slides_right + margin - panel["bbox"][2])
        if grow > 0.5:
            over[i] = max(over.get(i, 0.0), grow)
    blocks: dict[int, float] = {}
    for i, g in over.items():
        if els[i].get("block") is not None:
            blocks[els[i]["block"]] = max(blocks.get(els[i]["block"], 0.0), g)
    for i, e in panels:
        if e.get("block") in blocks:
            over[i] = blocks[e["block"]]
    # (a block's shadow panel lies under the whole block, a few points right and down: it grows too)
    for b, g in blocks.items():
        box = [e["bbox"] for _, e in panels if e.get("block") == b]
        ux0, uy0, ux1, uy1 = min(r[0] for r in box), min(r[1] for r in box), max(r[2] for r in box), max(r[3] for r in box)
        for i, e in panels:
            x0, y0, x1, y1 = e["bbox"]
            if e.get("block") is None and i not in over and all(0 <= d <= 5 for d in (x0 - ux0, y0 - uy0, x1 - ux1, y1 - uy1)):
                over[i] = g
    # (a panel drawn round a grown one and ending just right of it - a tcolorbox's frame-coloured
    # panel under its body - grows with it, or the frame's right side is covered: r1_design_v1 s6)
    for i, g in list(over.items()):
        ix0, iy0, ix1, iy1 = els[i]["bbox"]
        for j, e in panels:
            x0, y0, x1, y1 = e["bbox"]
            if j not in over and x0 <= ix0 + 0.5 and y0 <= iy0 + 0.5 and iy1 - 0.5 <= y1 and 0 <= x1 - ix1 <= 5:
                over[j] = g
    if not over:
        return slide
    page_w = slide["size"][0]
    out = list(els)
    for i, g in over.items():
        e = els[i]
        g = min(g, page_w - 1 - e["bbox"][2])
        if g <= 0.5:
            continue
        wider = {**e, "bbox": [e["bbox"][0], e["bbox"][1], round(e["bbox"][2] + g, 2), e["bbox"][3]]}
        if e.get("title_bar"):
            bar = e["title_bar"]
            wider["title_bar"] = [bar[0], bar[1], round(bar[2] + g, 2), bar[3]]
        if template_key(wider, scale) == template_key(e, scale):  # (the template shapes are fixed)
            out[i] = wider
    return {**slide, "elements": out}


def title_bar_under(el: dict, slide: dict) -> list[float] | None:
    """The PDF box of the block title bar a text element sits on, if any."""
    cx, cy = (el["bbox"][0] + el["bbox"][2]) / 2, (el["bbox"][1] + el["bbox"][3]) / 2
    return next((e["bbox"] for e in slide["elements"] if e["kind"] == "shape" and e.get("block") is not None
                 and not e.get("title_bar") and e["bbox"][0] <= cx <= e["bbox"][2]
                 and e["bbox"][1] <= cy <= e["bbox"][3]), None)


def measure_jobs(deck: dict, scale: float, fonts: FontMapper, placed, page_slide: dict) -> tuple[list[dict], list[tuple]]:
    """The scratch slides of measure_places: their requests, and per slide (scratch slide id, page,
    [hole to find: picture, expected gap start x0 and line middle cy, width, pitch, colour, hang],
    [overlay: picture, predicted PDF extent, marks' predicted drifts, words (mark_words with their
    colour, predicted slide x0 and text box rows)])."""
    reqs, jobs = [], []
    for slide in deck["slides"]:
        n = slide["page"]
        holes = [h for h in slide_holes(slide) if h[3] is not None]
        texts = {e["id"]: e for e in slide["elements"] if e["kind"] == "text"}
        overlays = [e for e in slide["elements"] if e.get("marks") and e.get("anchor") in texts]
        if not holes and not overlays:
            continue
        sid = f"b2s_m{n:03}"
        reqs += [{"createSlide": {"objectId": sid, "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
                 {"updatePageProperties": {"objectId": sid, "fields": "pageBackgroundFill.solidFill.color",
                                           "pageProperties": {"pageBackgroundFill": {"solidFill": {
                                               "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}}]
        found, measured, taken = [], [], []  # taken: (colour, x0, x1, y0, y1) of every highlight, slide pt
        for i, el in enumerate(slide["elements"]):
            mine = [h for h in holes if h[0] is el]
            marked = [o for o in overlays if o["anchor"] == el["id"]]
            if not mine and not marked:
                continue
            colours = [HOLE_MARKS[(len(found) + k) % len(HOLE_MARKS)] for k in range(len(mine))]
            oid = f"{sid}_t{i}"
            plain = {**el, "paragraphs": [{**p, "runs": [{**r, "highlight": None} for r in p["runs"]]} for p in el["paragraphs"]]}
            reqs += text_box_requests(plain, sid, oid, scale, fonts, None, page_slide, title_bar_under(el, slide),
                                      text_right_limit(el, slide), colours)
            reqs.append({"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"}, "fields": "foregroundColor",
                                             "style": {"foregroundColor": rgb("#000000")}}})
            top, bottom = el["bbox"][1] * scale - 4, el["bbox"][3] * scale + 4
            for (_, _, _, pic), colour in zip(mine, colours):
                x0, y0, x1, y1 = (v * scale for v in placed(pic, n)["bbox"])
                taken.append((colour, x0, x1, y0, y1))
            for o in marked:
                bx0, _, bx1, _ = o["bbox"]
                a0, _, a1, _ = placed(o, n)["bbox"]
                words = []
                for w in mark_words(o, el, scale, fonts):
                    m = o["marks"][w["marks"][0][0]]
                    x = m["x"] - (w["width"] or 0.0) * w["marks"][0][1]  # the word's PDF x0
                    x0 = (a0 + (x - bx0) * (a1 - a0) / (bx1 - bx0)) * scale
                    x1 = x0 + (w["width"] or 0.0) * scale

                    def clash(c):  # how close the nearest highlight of colour c on these rows is
                        return min([max(0.0, t[1] - x1, x0 - t[2]) for t in taken
                                    if t[0] == c and t[3] < bottom and t[4] > top] + [math.inf])
                    colour = max(HOLE_MARKS, key=lambda c: (clash(c), -sum(t[0] == c for t in taken)))
                    taken.append((colour, x0, x1, top, bottom))
                    words.append({**w, "colour": colour, "x0": x0, "top": top, "bottom": bottom})
                    reqs.append({"updateTextStyle": {"objectId": oid, "fields": "backgroundColor",
                                                     "style": {"backgroundColor": rgb(colour)},
                                                     "textRange": {"type": "FIXED_RANGE", "startIndex": w["range"][0],
                                                                   "endIndex": w["range"][1]}}})
                measured.append({"pic": o, "box": (a0, a1), "drifts": mark_drifts(o, scale, fonts), "words": words})
            for (_, p, run, pic), colour in zip(mine, colours):
                x0, y0, _, y1 = placed(pic, n)["bbox"]
                z = fonts(run, scale)[1]
                bl = [line["baseline"] for line in p["lines"]]
                pitch = (bl[-1] - bl[0]) / (len(bl) - 1) * scale if len(bl) > 1 else LINE_EM * z
                prev_end, next_x0 = hole_neighbours(p, run)
                # The middle of the hole's text line, not of the picture: a brace's label below
                # or above the formula would put that a line off.
                line = min(p["lines"], key=lambda l: (not y0 - 0.5 <= l["baseline"] <= y1 + 0.5,
                                                      abs((y0 + y1) / 2 - l["baseline"] + 0.35 * run["size"])))
                found.append({"pic": pic, "x0": x0 * scale - hole_offset(p, run, pic, scale, z),  # expected gap start
                              "cy": (line["baseline"] - 0.35 * run["size"]) * scale, "width": run["hole"] * scale, "pitch": pitch, "colour": colour,
                              # last on its PDF line: a hole Slides lets hang past the box edge isn't drawn
                              "hang": None if run.get("next_x0") is not None else
                              (SYMBOL_ADVANCE_EM[" "] * z if prev_end is not None else 0.0,
                               el["bbox"][0] * scale - PAD_X, el["bbox"][2] * scale + 2 * PAD_X)})
        jobs.append((sid, n, found, measured))
    return reqs, jobs


def overlay_move(o: dict, marks: dict[str, list], scale: float) -> tuple[tuple[float, float, float] | None, list]:
    """(dx, dy, scaleX) taking an overlay picture from its predicted extent to the one its
    measured words give (fit_overlay; marks not measured keep their predicted drift), or None if
    no word was found; and [x, predicted drift, measured drift or None] per mark (PDF pt)."""
    got = {}
    for w in o["words"]:
        rect = pick_word(marks[w["colour"]], w["x0"], w["width"] and w["width"] * scale, w["top"], w["bottom"])
        for i, side in w["marks"] if rect else []:
            got[i] = rect[2 * side] / scale - o["pic"]["marks"][i]["x"]
    xs = [m["x"] for m in o["pic"]["marks"]]
    table = [[x, round(d, 2), round(got[i], 2) if i in got else None] for i, (x, d) in enumerate(zip(xs, o["drifts"]))]
    if not got:
        return None, table
    b0, b1 = fit_overlay(o["pic"]["bbox"], [(x, got.get(i, d)) for i, (x, d) in enumerate(zip(xs, o["drifts"]))],
                         OVERLAY_STRETCH_MEASURED)
    a0, a1 = o["box"]
    return ((b0 - a0) * scale, 0.0, (b1 - b0) / (a1 - a0)), table


def measure_places(slides, pid: str, deck: dict, scale: float, fonts: FontMapper, placed, page_slide: dict,
                   out: Path, page_width: float = SLIDE_W) -> tuple[dict[str, tuple], list[str]]:
    """Moves for hole pictures ((dx, dy) in slide pt) and overlay pictures ((dx, dy, scaleX): dx
    moves the left edge), measured on scratch slides (see above), and the scratch slides to
    delete. What can't be found keeps its predicted place.

    `page_width`: how wide the deck being measured is, in slide pt. A thumbnail is a fixed number
    of pixels wide whatever the page is, so it alone says how many pixels a point is."""
    from PIL import Image
    from .gslides import save_thumbnail

    started = time.monotonic()
    reqs, jobs = measure_jobs(deck, scale, fonts, placed, page_slide)
    if not jobs:
        return {}, []
    from . import net
    if net.downloads_off():  # (no thumbnail could be read: no scratch slides either)
        print("picture places: predicted (downloads are switched off, so no thumbnail can be measured)")
        return {}, []
    try:
        batch(slides, pid, reqs)
    except HttpError as e:
        print(f"warning: could not measure the picture places ({api_error(e)}); keeping the predicted places")
        return {}, []  # (a refused batch created nothing)

    # One client per worker thread, not one per slide: `build(...)` fetches a discovery document
    # every time it is called. Where a caller handed its own client over there is only that one,
    # which is not thread-safe, so the thumbnails are fetched one at a time.
    creds, fetch = credentials_for_threads(), fetcher_for_threads()
    client = per_thread(lambda: slides_service(creds))
    workers = 1 if shared_service("slides", "v1") else 6
    tables = {}

    def measure(job):
        sid, n, found, overlays = job
        path = out / "holes" / f"marks-{n + 1:03}.png"
        try:
            save_thumbnail(client(), pid, sid, path, fetch)
        except Exception as e:  # noqa: BLE001 - HttpError, OSError, or a harness fetcher's own
            print(f"warning: slide {n + 1}: no thumbnail to measure the picture places ({e})")
            return {}
        img = np.asarray(Image.open(path).convert("RGB"))
        px_per_pt = img.shape[1] / page_width
        marks = {c: find_marks(mark_alpha(img, c), px_per_pt)
                 for c in {f["colour"] for f in found} | {w["colour"] for o in overlays for w in o["words"]}}
        moves = {}
        for o in overlays:
            move, tables[o["pic"]["id"]] = overlay_move(o, marks, scale)
            if move is None:
                print(f"warning: slide {n + 1}: words of {o['pic']['id']} not found; keeping its predicted place")
            elif abs(move[0]) >= 0.2 or abs(move[2] - 1) >= 0.002:
                moves[o["pic"]["id"]] = move
        for f in found:
            move = pick_gap(marks[f["colour"]], f["x0"], f["cy"], f["width"], f["pitch"])
            if move is None and f["hang"]:
                space, left, right = f["hang"]
                end = ink_end(img, px_per_pt, f["cy"] - 0.2 * f["pitch"], f["cy"] + 0.2 * f["pitch"], left, right)
                if end is not None and abs(end + space - f["x0"]) < 0.5 * f["width"] + 10:
                    move = (end + space - f["x0"], 0.0)
            if move is None:
                print(f"warning: slide {n + 1}: gap of {f['pic']['id']} not found; keeping its predicted place")
            elif abs(move[0]) >= 0.2 or move[1]:
                moves[f["pic"]["id"]] = move
        return moves

    moves = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(measure, jobs):
            moves.update(result)
    (out / "holes" / "moves.json").write_text(json.dumps({k: [round(v, 4) for v in m] for k, m in moves.items()},
                                                         indent=1), encoding="utf-8")
    # (per overlay mark: PDF x, predicted drift, measured drift)
    (out / "holes" / "overlays.json").write_text(json.dumps(tables), encoding="utf-8")
    n_holes, n_overlays = sum(len(job[2]) for job in jobs), sum(len(job[3]) for job in jobs)
    print(f"  picture places: {n_holes} formula gaps and {n_overlays} overlays measured on {len(jobs)} slides, "
          f"{len(moves)} pictures moved ({time.monotonic() - started:.1f} s)")
    return moves, [job[0] for job in jobs]
