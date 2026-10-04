"""Probe: how Slides draws TeX's calligraphic capitals and which space keeps TeX's math spacing.

1. \\mathcal / \\mathscr. classify writes a CMSY (rsfs, eusm, txsys...) capital as Unicode's script
   letter (`classify_text.math_pieces`: ℒ, 𝒪, 𝒲). Lato has none, and Slides draws them from a
   fallback face of its own: a heavy swash with another advance (big-o-for-weighted s10,
   linear-attention-a s19-21, defense-defense s46). Named on the run, a served math face might
   draw them instead. For Lato (today: the fallback) and each candidate face this measures, on
   Google's renderer, the 26 capitals as math_pieces writes them, plain and with U+FE00 (Unicode's
   chancery variation selector, \\mathcal's style where a face has it):
   - the mean advance (em; the "|  text  |" bars of probe_symbols);
   - the ink height of a row of 13 capitals (em) and the stroke width (2 x ink area / ink
     perimeter, per em: emit_metrics' measure for the 6 pt cut's weight);
   - whether the face is served at all: its ASCII row against the same row in Arial, which Slides
     draws for a family it does not know;
   and the same for TeX's own CMSY10, RSFS10 and EUSM10, rendered here from MiKTeX's .pfb by
   FreeType at the thumbnail's scale. sheet.png puts every face's capitals under TeX's to look at.
   It decides: **name a face for script capitals** when one is served, draws them (no box), and is
   within 12% of CMSY10's advance, 15% of its ink height and 25% of its stroke; else **keep the
   fallback**, the nearest face named with its numbers.

2. Math spacing. TeX sets a relation between thick spaces (5/18 em), a binary operator between
   medium ones (4/18) and a thin space after a comma (3/18); classify writes a no-break space
   there, which Lato draws 0.19 em wide. For Lato and PT Serif and each candidate space (the
   Unicode ones of those widths, alone and between word joiners) this measures its advance (em),
   any ink (a fallback's box), whether Slides breaks a line at it (a box too narrow for
   "xxxxxxxx?yyyyyyyy": the first line's width against a space's and against no separator), and
   whether it changes the line pitch (six soft-broken lines "H?H" against "HH"). It decides, per
   TeX space, the character nearest its width that draws nothing, keeps the pitch within 1% and
   does not break (inside a formula nothing may), when it is nearer than the no-break space by
   0.03 em or more; else the no-break space stays.

One slide per face and two for the spaces, deleted after. Results: out/probe_math_glyphs/result.json,
thumbnails and sheet.png beside it.

Usage: python tools/probe_math_glyphs.py
"""

import json
import os
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from beamer2slides.google_auth import drive_service, slides_service
from beamer2slides.google_types import SlidesRequest, object_id, presentation_id
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_math_glyphs"
SLIDE_W = 720.0  # pt; a LARGE thumbnail is 1600 px wide
# Lato first (today: Slides' fallback draws the letters), then served faces with Mathematical
# Script letters, then Arial (what Slides draws for a family it does not serve).
FACES = ["Lato", "PT Serif", "Noto Sans Math", "STIX Two Math", "Libertinus Math", "Noto Serif", "Arial"]
# The 26 capitals as classify_text.math_pieces writes \mathcal's (Letterlike Symbols where Unicode has them)
LETTERLIKE = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
CAPITALS = "".join(LETTERLIKE.get(c, chr(0x1D49C + ord(c) - 65)) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CHANCERY = "︀"
ASCII = "Hamburgefonstiv HAMBURG 0123"
BAR_SIZE = 20.0
INK_SIZE = 32.0
# TeX's references: (name, MiKTeX path under fonts/type1/public)
TEX_FACES = [("CMSY10", "amsfonts/cm/cmsy10.pfb"), ("RSFS10", "rsfs/rsfs10.pfb"), ("EUSM10", "amsfonts/euler/eusm10.pfb")]

SPACE_FACES = ["Lato", "PT Serif"]
WJ = "⁠"
SPACES = {"space": " ", "nbsp": " ", "thin": " ", "hair": " ", "narrow nbsp": " ",
          "medium math": " ", "four-per-em": " ", "three-per-em": " ", "six-per-em": " ",
          "punctuation": "\u2008", "wj four-per-em wj": WJ + " " + WJ, "wj medium math wj": WJ + " " + WJ,
          "wj thin wj": WJ + " " + WJ, "none": ""}
TEX_SPACES = {"thick (relation)": 5 / 18, "medium (binary operator)": 4 / 18, "thin (after a comma)": 3 / 18}
SPACE_SIZE = 20.0
LINES = 6
SOFT_BREAK = chr(11)
BREAK_BOX_W = 90.0  # 76.6 pt inside: one 14 pt word of eight x fits, two do not


def style(oid: str, text: str, font: str, size: float) -> list[SlidesRequest]:
    return [{"insertText": {"objectId": oid, "text": text}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                 "fields": "fontFamily,fontSize,bold,italic,foregroundColor",
                                 "style": {"fontFamily": font, "fontSize": pt(size), "bold": False, "italic": False,
                                           "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}},
            {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                      "style": {"lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                      "fields": "lineSpacing,spaceAbove,spaceBelow"}}]


def box(oid: str, page: str, x: float, y: float, w: float, h: float, text: str, font: str,
        size: float) -> list[SlidesRequest]:
    return [text_box(oid, page, x, y, w, h)] + style(oid, text, font, size)


def gray(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L"), dtype=np.float64)


def ink_columns(img: np.ndarray, k: float, x: float, y: float, w: float, h: float) -> list[int]:
    crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)]
    return [int(c) for c in np.where((crop < 128).any(axis=0))[0]]


def ink_rows(img: np.ndarray, k: float, x: float, y: float, w: float, h: float) -> list[tuple[int, int]]:
    """(top, bottom) pixel rows of each run of inked rows inside a box (pt)."""
    crop = img[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)]
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, on in enumerate((crop < 128).any(axis=1).tolist()):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, int(crop.shape[0]) - 1))
    return runs


@dataclass(frozen=True, kw_only=True)
class Bars:
    """A "|text|" row: the distance between the bars' inner edges (pt) and whether anything is
    inked between them."""
    inner: float
    inked: bool


def bars(img: np.ndarray, k: float, x: float, y: float, w: float, size: float) -> Bars:
    cols = ink_columns(img, k, x, y + 0.2 * size, w, size)
    if not cols:
        raise SystemExit(f"no bars at ({x}, {y})")
    left, right = cols[0], cols[-1]
    while left + 1 in cols:
        left += 1
    while right - 1 in cols:
        right -= 1
    return Bars(inner=(right - left) / k, inked=any(left < c < right for c in cols))


def stroke(mask: np.ndarray) -> tuple[float, float]:
    """(ink area, ink perimeter) of a boolean mask, px: the perimeter counts ink/paper edges."""
    m = mask.astype(np.int8)
    edges = np.abs(np.diff(m, axis=0)).sum() + np.abs(np.diff(m, axis=1)).sum()
    edges += m[0, :].sum() + m[-1, :].sum() + m[:, 0].sum() + m[:, -1].sum()
    return float(m.sum()), float(edges)


@dataclass(frozen=True, kw_only=True)
class Capitals:
    """One face's 26 capitals (em of the size they are set at)."""
    face: str
    advance: float           # mean advance
    advance_chancery: float  # with U+FE00 after each letter
    ink_height: float        # mean of the two 13-letter rows' ink extent
    stroke: float            # 2 x ink area / perimeter, per em
    ascii: float             # the ASCII row's width (em), to tell a served face from Arial


@dataclass(frozen=True, kw_only=True)
class Space:
    """One candidate space in one face."""
    face: str
    name: str
    advance: float     # em
    inked: bool        # something drawn (a fallback's box)
    breaks: bool       # Slides breaks a line at it
    pitch_ratio: float  # line pitch with it over without


# Where things sit on a capitals slide (pt)
CAP_ROWS = {"reference": 10.0, "plain": 45.0, "chancery": 80.0, "ascii": 115.0}
INK_ROWS = (160.0, 240.0)
ROW_W, INK_H = 700.0, 70.0


def capital_requests(face: str, page: str, n: int) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    # (13 letters with selectors: a face that draws a selector as a box still keeps to one line)
    texts = {"reference": "", "plain": CAPITALS, "chancery": "".join(c + CHANCERY for c in CAPITALS[:13]),
             "ascii": ASCII}
    for key, y in CAP_ROWS.items():
        reqs += box(f"cap_{n}_{key}", page, 10.0, y, ROW_W, 32.0, "|  " + texts[key] + "  |", face, BAR_SIZE)
    for i, y in enumerate(INK_ROWS):
        reqs += box(f"cap_{n}_ink{i}", page, 10.0, y, ROW_W, INK_H, CAPITALS[13 * i:13 * (i + 1)], face, INK_SIZE)
    return reqs


def measure_capitals(face: str, path: Path) -> Capitals:
    img = gray(path)
    k = img.shape[1] / SLIDE_W
    ref = bars(img, k, 10.0, CAP_ROWS["reference"], ROW_W, BAR_SIZE)

    def row(key: str, letters: int) -> float:
        return (bars(img, k, 10.0, CAP_ROWS[key], ROW_W, BAR_SIZE).inner - ref.inner) / BAR_SIZE / letters

    heights: list[float] = []
    area, edges = 0.0, 0.0
    for y in INK_ROWS:
        rows = ink_rows(img, k, 10.0, y, ROW_W, INK_H)
        if rows:
            heights.append((rows[-1][1] - rows[0][0] + 1) / k / INK_SIZE)
        crop = img[int(y * k):int((y + INK_H) * k), int(10.0 * k):int((10.0 + ROW_W) * k)]
        a, e = stroke(crop < 128)
        area, edges = area + a, edges + e
    em_px = INK_SIZE * k
    return Capitals(face=face, advance=round(row("plain", 26), 4), advance_chancery=round(row("chancery", 13), 4),
                    ink_height=round(statistics.mean(heights), 4) if heights else 0.0,
                    stroke=round(2 * area / edges / em_px, 5) if edges else 0.0,
                    ascii=round(row("ascii", 1), 4))


def miktex_type1() -> Path:
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/MiKTeX", "MiKTeX"):
            p = Path(base) / sub / "fonts" / "type1" / "public"
            if p.is_dir():
                return p
    raise SystemExit("MiKTeX's fonts/type1/public not found")


def tex_capitals(name: str, path: Path, em_px: float) -> tuple[Capitals, Image.Image]:
    """TeX's own capitals A-Z, rendered by FreeType at the thumbnail's em (px), measured as
    measure_capitals measures Slides' rows; and the two rows as an image for the sheet."""
    font = ImageFont.truetype(str(path), round(em_px))
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    advance = sum(font.getlength(c) for c in letters) / len(letters) / em_px
    heights: list[float] = []
    area, edges = 0.0, 0.0
    pictures: list[Image.Image] = []
    for i in range(2):
        text = letters[13 * i:13 * (i + 1)]
        pic = Image.new("L", (int(em_px * 16), int(em_px * 2)), 255)
        ImageDraw.Draw(pic).text((int(em_px * 0.2), int(em_px * 0.3)), text, font=font, fill=0)
        arr = np.asarray(pic, dtype=np.float64)
        inked = np.where((arr < 128).any(axis=1))[0]
        heights.append((int(inked[-1]) - int(inked[0]) + 1) / em_px)
        a, e = stroke(arr < 128)
        area, edges = area + a, edges + e
        pictures.append(pic)
    sheet = Image.new("L", (pictures[0].width, pictures[0].height * 2), 255)
    for i, pic in enumerate(pictures):
        sheet.paste(pic, (0, i * pic.height))
    return Capitals(face=name, advance=round(advance, 4), advance_chancery=round(advance, 4),
                    ink_height=round(statistics.mean(heights), 4), stroke=round(2 * area / edges / em_px, 5),
                    ascii=0.0), sheet


# Where things sit on a spaces slide (pt): a cell per candidate, five to a column; in a cell the
# bars row (20 pt) over the break box (14 pt), the pitch paragraph (8 pt) to their right
SPACE_COL_W = 180.0
SPACE_PER_COL = 5
BARS_W = 120.0
BREAK_Y, BREAK_H, BREAK_SIZE = 32.0, 46.0, 14.0
PITCH_X, PITCH_W, PITCH_H = 124.0, 50.0, 78.0


def space_spot(i: int) -> tuple[float, float]:
    return 5.0 + (i // SPACE_PER_COL) * (SPACE_COL_W + 4.0), 5.0 + (i % SPACE_PER_COL) * 80.0


def space_requests(face: str, page: str, n: int) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    for i, (name, c) in enumerate(SPACES.items()):
        x, y = space_spot(i)
        oid = f"sp_{n}_{i}"
        reqs += box(oid + "_bars", page, x, y, BARS_W, 30.0, "|" + c * 8 + "|", face, SPACE_SIZE)
        reqs += box(oid + "_break", page, x, y + BREAK_Y, BREAK_BOX_W, BREAK_H, "xxxxxxxx" + c + "yyyyyyyy", face,
                    BREAK_SIZE)
        lines = SOFT_BREAK.join(["H" + c + "H"] * LINES)
        reqs += box(oid + "_pitch", page, x + PITCH_X, y, PITCH_W, PITCH_H, lines, face, 8.0)
    return reqs


def measure_spaces(face: str, path: Path) -> list[Space]:
    img = gray(path)
    k = img.shape[1] / SLIDE_W
    found: dict[str, tuple[float, bool, float, float]] = {}
    for i, name in enumerate(SPACES):
        x, y = space_spot(i)
        b = bars(img, k, x, y, BARS_W, SPACE_SIZE)
        rows = ink_rows(img, k, x, y + BREAK_Y, BREAK_BOX_W, BREAK_H)
        first = rows[0] if rows else (0, 0)
        crop = img[int((y + BREAK_Y) * k) + first[0]:int((y + BREAK_Y) * k) + first[1] + 1,
                   int(x * k):int((x + BREAK_BOX_W) * k)]
        cols = np.where((crop < 128).any(axis=0))[0]
        first_w = (int(cols[-1]) - int(cols[0]) + 1) / k if len(cols) else 0.0
        tops = [top for top, _ in ink_rows(img, k, x + PITCH_X, y, PITCH_W, PITCH_H)]
        pitch = statistics.median(b2 - a2 for a2, b2 in zip(tops, tops[1:])) / k if len(tops) >= 2 else 0.0
        found[name] = (b.inner, b.inked, first_w, pitch)
    bare = found["none"]
    spaced = found["space"]
    out = []
    for name, (inner, inked, first_w, pitch) in found.items():
        # (a space's first line holds the x word alone; with no separator Slides cuts the long word)
        breaks = abs(first_w - spaced[2]) < abs(first_w - bare[2])
        out.append(Space(face=face, name=name, advance=round((inner - bare[0]) / 8 / SPACE_SIZE, 4), inked=inked,
                         breaks=breaks, pitch_ratio=round(pitch / bare[3], 4) if bare[3] else 0.0))
    return out


def capital_verdict(faces: list[Capitals], cmsy: Capitals) -> str:
    arial = next(f for f in faces if f.face == "Arial")
    served = [f for f in faces if f.face not in ("Arial", "Lato", "PT Serif") and abs(f.ascii - arial.ascii) > 0.003]

    def off(f: Capitals) -> tuple[float, float, float]:
        return f.advance / cmsy.advance - 1, f.ink_height / cmsy.ink_height - 1, f.stroke / cmsy.stroke - 1

    def score(f: Capitals) -> float:
        a, h, s = off(f)
        return abs(a) / 0.12 + abs(h) / 0.15 + abs(s) / 0.25

    lato = next(f for f in faces if f.face == "Lato")
    la, lh, ls = off(lato)
    today = f"today (Lato's fallback): advance {la:+.0%}, ink height {lh:+.0%}, stroke {ls:+.0%} of CMSY10's"
    good = [f for f in served if all(abs(v) <= m for v, m in zip(off(f), (0.12, 0.15, 0.25)))]
    if good:
        best = min(good, key=score)
        a, h, s = off(best)
        return (f"name {best.face} on script-capital runs: advance {a:+.0%}, ink height {h:+.0%}, stroke {s:+.0%} "
                f"of CMSY10's; {today}")
    if not served:
        return f"keep the fallback: no candidate face is served; {today}"
    best = min(served, key=score)
    a, h, s = off(best)
    return f"keep the fallback; nearest served face {best.face}: advance {a:+.0%}, height {h:+.0%}, stroke {s:+.0%}; {today}"


def space_verdict(spaces: list[Space]) -> dict[str, str]:
    out: dict[str, str] = {}
    for face in SPACE_FACES:
        mine = [s for s in spaces if s.face == face]
        nbsp = next(s for s in mine if s.name == "nbsp")
        usable = [s for s in mine if s.name not in ("none", "space") and not s.inked and not s.breaks
                  and abs(s.pitch_ratio - 1) <= 0.01]
        for tex, width in TEX_SPACES.items():
            best = min(usable, key=lambda s: abs(s.advance - width)) if usable else None
            if best is not None and abs(nbsp.advance - width) - abs(best.advance - width) >= 0.03:
                out[f"{face}: {tex}"] = f"{best.name} ({best.advance} em, TeX {width:.3f}; nbsp {nbsp.advance})"
            else:
                out[f"{face}: {tex}"] = f"keep nbsp ({nbsp.advance} em, TeX {width:.3f})"
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe math glyphs"}))
    pid = presentation_id(pres)
    slide = pres.get("slides", [])[0]
    first = object_id(slide)
    reqs: list[SlidesRequest] = [{"deleteObject": {"objectId": object_id(e)}} for e in slide.get("pageElements", [])]
    cap_pages = [first] + [f"cap_page_{i}" for i in range(1, len(FACES))]
    space_pages = [f"space_page_{i}" for i in range(len(SPACE_FACES))]
    reqs += [{"createSlide": {"objectId": p}} for p in cap_pages[1:] + space_pages]
    for n, (face, page) in enumerate(zip(FACES, cap_pages)):
        reqs += capital_requests(face, page, n)
    for n, (face, page) in enumerate(zip(SPACE_FACES, space_pages)):
        reqs += space_requests(face, page, n)
    try:
        execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        capitals: list[Capitals] = []
        for face, page in zip(FACES, cap_pages):
            path = OUT / f"capitals-{face.replace(' ', '')}.png"
            save_thumbnail(slides, pid, page, path, None)
            capitals.append(measure_capitals(face, path))
            print(capitals[-1])
        spaces: list[Space] = []
        for face, page in zip(SPACE_FACES, space_pages):
            path = OUT / f"spaces-{face.replace(' ', '')}.png"
            save_thumbnail(slides, pid, page, path, None)
            spaces += measure_spaces(face, path)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))
    for s in spaces:
        print(s)
    first_img = gray(OUT / f"capitals-{FACES[0].replace(' ', '')}.png")
    k = first_img.shape[1] / SLIDE_W
    em_px = INK_SIZE * k
    type1 = miktex_type1()
    tex: list[Capitals] = []
    pictures: list[tuple[str, Image.Image]] = []
    for name, rel in TEX_FACES:
        found, pic = tex_capitals(name, type1 / rel, em_px)
        tex.append(found)
        pictures.append((name, pic))
        print(found)
    for face in FACES:  # Slides' two ink rows, cropped, under TeX's
        img = Image.open(OUT / f"capitals-{face.replace(' ', '')}.png").convert("L")
        top, bottom = int(INK_ROWS[0] * k), int((INK_ROWS[1] + INK_H) * k)
        pictures.append((face, img.crop((int(10 * k), top, int((10 + ROW_W) * k), bottom))))
    width = max(p.width for _, p in pictures)
    sheet = Image.new("L", (width + 220, sum(p.height + 10 for _, p in pictures)), 255)
    y = 0
    for name, pic in pictures:
        ImageDraw.Draw(sheet).text((5, y + 5), name, fill=0)
        sheet.paste(pic, (220, y))
        y += pic.height + 10
    sheet.save(OUT / "sheet.png")
    verdict = capital_verdict(capitals, tex[0])
    spacing = space_verdict(spaces)
    print("capitals:", verdict)
    for key, v in spacing.items():
        print("spacing:", key, "->", v)
    (OUT / "result.json").write_text(json.dumps({
        "source": "tools/probe_math_glyphs.py", "capitals": [asdict(c) for c in capitals],
        "tex": [asdict(c) for c in tex], "spaces": [asdict(s) for s in spaces],
        "capitals_decision": verdict, "spacing_decision": spacing}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote", OUT / "result.json", "and", OUT / "sheet.png")


if __name__ == "__main__":
    main()
