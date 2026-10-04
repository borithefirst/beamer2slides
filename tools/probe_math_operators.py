"""Probe: how Slides draws TeX's math operators, and which served face draws them nearest TeX.

classify writes an inline formula's operators as Unicode (⊤ ⊥ ⊙ ⊗ ⊕ ∈ ∉ ⊂ ⊆ ∪ ∩ ∀ ∃ ∇ ∂ ∞ ≤ ≥ ≠ ≈ ≡
→ ⇒ ↦ ⟶ ℝ ℕ ℤ ℂ · × ∑ ∏ ∫). Lato has none of them, and Slides draws them from a fallback face of its
own: on real decks the ⊤ of QK^⊤ came out about half the height of CMSY's. Named on the operator, a
served math face might draw them nearer TeX (as `fonts.letter_face` names STIX Two Math for script
capitals, tools/probe_math_glyphs.py).

For Lato (today: the fallback), PT Serif (serif decks' face) and each candidate face this measures,
on Google's renderer, every operator written as classify writes it:
- in "H op H" at INK_SIZE, the H's in Lato and the operator alone in the face (as emit would name
  a face per glyph): the operator's ink width and height, its vertical centre above the baseline
  (the H's foot) - TeX centres operators on the math axis, 0.25 em - and its stroke width (2 x ink
  area / ink perimeter), all per em;
- its advance: "| op op op op |" against "|  |" in the face (the bars of probe_math_glyphs);
- whether the face is served at all: an ASCII row against the same row in Arial, which Slides draws
  for a family it does not know;
and the same for TeX's own glyph, rendered here by FreeType from MiKTeX's .pfb at the thumbnail's
scale: CMSY10 (OMS), CMMI10 (∂), CMR10 (the = of \\neq), MSBM10 (ℝℕℤℂ) and CMEX10's text-style ∑∏∫,
which TeX centres on the axis (their centre is the axis). \\not, \\mapstochar and \\joinrel build ∉ ≠ ↦
⟶ as TeX does (LaTeX's \\notin centres its slash; here it is \\not\\in).

Per operator it ranks the faces that draw it by height and axis first (in units of 15% of TeX's
height and 0.05 em), advance and stroke shown beside: result.json, and summary.txt one line per
operator; sheet.png puts every face's operators under TeX's. Two slides per face in a scratch deck
of its own, deleted after (as probe_math_glyphs). Results: out/probe_math_operators/.

--dry-run: no Google. Builds every request (checked: ids unique, 5-50 characters, boxes on the
slide; out/probe_math_operators/dry-run/requests.json), renders TeX's references, and runs the whole
measurement on a stand-in thumbnail drawn here in local faces (Segoe UI Symbol, Cambria Math):
what it measures against what FreeType says of the same glyphs proves the cells, baselines and bars
are read right. Ranking and sheet as a live run, under dry-run/.

Usage: python tools/probe_math_operators.py [--dry-run]
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_math_glyphs import SLIDE_W, bars, box, gray, miktex_type1, stroke  # noqa: E402

from beamer2slides.google_auth import drive_service, slides_service  # noqa: E402
from beamer2slides.google_types import SlidesRequest, object_id, presentation_id, slides_json  # noqa: E402
from beamer2slides.gslides import execute, save_thumbnail  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_math_operators"
SLIDE_H = 405.0
THUMB_W = 1600  # a LARGE thumbnail's width (px)
# Lato first (today: Slides' fallback draws the operators), PT Serif (the serif face), Arial (a
# face Slides always has, and what it draws for a family it does not serve), then served faces
# with math operators.
FACES = ["Lato", "PT Serif", "Arial", "STIX Two Math", "STIX Two Text", "Noto Sans Math", "Libertinus Math",
         "Noto Sans Symbols", "Noto Sans Symbols 2", "Noto Sans", "Noto Serif"]
ALWAYS_SERVED = ("Lato", "PT Serif", "Arial")
NEIGHBOUR = "Lato"  # the H's either side of an operator
ASCII = "Hamburg 0123"
NBSP = "\xa0"
AXIS = 0.25  # CMSY10's axis_height (em)

# TeX's fonts: name -> path under MiKTeX's fonts/type1/public
TEX_FONTS = {"CMSY10": "amsfonts/cm/cmsy10.pfb", "CMMI10": "amsfonts/cm/cmmi10.pfb", "CMR10": "amsfonts/cm/cmr10.pfb",
             "MSBM10": "amsfonts/symbols/msbm10.pfb", "CMEX10": "amsfonts/cm/cmex10.pfb"}


@dataclass(frozen=True, kw_only=True)
class Piece:
    """One glyph of TeX's: `char` is the TeX code through the font's own encoding, or with
    `unicode` a character through FreeType's Unicode map (PIL reads code 10 as a line break)."""
    font: str
    char: str
    unicode: bool
    kern: float  # em after it (\joinrel's -3mu)


@dataclass(frozen=True, kw_only=True)
class Operator:
    char: str     # as classify writes it
    tex: str      # the command
    pieces: tuple[Piece, ...]
    on_axis: bool  # CMEX: TeX centres it on the math axis


def code(font: str, n: int) -> Piece:
    return Piece(font=font, char=chr(n), unicode=False, kern=0.0)


def op(char: str, tex: str, pieces: tuple[Piece, ...]) -> Operator:
    return Operator(char=char, tex=tex, pieces=pieces, on_axis=False)


def sy(char: str, tex: str, n: int) -> Operator:
    return op(char, tex, (code("CMSY10", n),))


def bb(char: str, letter: str) -> Operator:
    return op(char, "\\mathbb{" + letter + "}", (code("MSBM10", ord(letter)),))


def big(char: str, tex: str, n: int) -> Operator:
    return Operator(char=char, tex=tex, pieces=(code("CMEX10", n),), on_axis=True)


NOT, MAPSTOCHAR, RIGHTARROW, MINUS = code("CMSY10", 0x36), code("CMSY10", 0x37), code("CMSY10", 0x21), 0x00
OPERATORS = [
    sy("⊤", "\\top", 0x3E), sy("⊥", "\\bot", 0x3F), sy("⊙", "\\odot", 0x0C),
    op("⊗", "\\otimes", (Piece(font="CMSY10", char="⊗", unicode=True, kern=0.0),)), sy("⊕", "\\oplus", 0x08),
    sy("∈", "\\in", 0x32), op("∉", "\\notin", (NOT, code("CMSY10", 0x32))), sy("⊂", "\\subset", 0x1A),
    sy("⊆", "\\subseteq", 0x12), sy("∪", "\\cup", 0x5B), sy("∩", "\\cap", 0x5C), sy("∀", "\\forall", 0x38),
    sy("∃", "\\exists", 0x39), sy("∇", "\\nabla", 0x72), op("∂", "\\partial", (code("CMMI10", 0x40),)),
    sy("∞", "\\infty", 0x31), sy("≤", "\\leq", 0x14), sy("≥", "\\geq", 0x15),
    op("≠", "\\neq", (NOT, code("CMR10", 0x3D))), sy("≈", "\\approx", 0x19), sy("≡", "\\equiv", 0x11),
    sy("→", "\\to", 0x21), sy("⇒", "\\Rightarrow", 0x29), op("↦", "\\mapsto", (MAPSTOCHAR, RIGHTARROW)),
    op("⟶", "\\longrightarrow", (Piece(font="CMSY10", char=chr(MINUS), unicode=False, kern=-3 / 18), RIGHTARROW)),
    bb("ℝ", "R"), bb("ℕ", "N"), bb("ℤ", "Z"), bb("ℂ", "C"),
    sy("·", "\\cdot", 0x01), sy("×", "\\times", 0x02),
    big("∑", "\\sum", 0x50), big("∏", "\\prod", 0x51), big("∫", "\\int", 0x52),
]

# Where things sit (pt). The ink slide: a cell per operator, "H op H" at INK_SIZE
INK_SIZE = 28.0
INK_COLS = 5
INK_CELL_W, INK_CELL_H = 142.0, 57.0
INK_BOX_W, INK_BOX_H = 138.0, 55.0
# The advance slide: a cell per operator, then the bars' reference and the ASCII row
BAR_SIZE = 20.0
BARS = 4  # operators between the bars
BAR_COLS = 4
BAR_CELL_W, BAR_CELL_H = 178.0, 45.0
BAR_BOX_W, BAR_BOX_H = 174.0, 40.0
INSET = 7.2  # a text box's inset, each side
TOLERANCE = {"height": 0.15, "centre": 0.05, "advance": 0.12, "stroke": 0.25}


def ink_spot(i: int) -> tuple[float, float]:
    return 5.0 + (i % INK_COLS) * INK_CELL_W, 3.0 + (i // INK_COLS) * INK_CELL_H


def bar_spot(i: int) -> tuple[float, float]:
    return 5.0 + (i % BAR_COLS) * BAR_CELL_W, 0.0 + (i // BAR_COLS) * BAR_CELL_H


def ink_text(o: Operator) -> str:
    return "H" + NBSP + o.char + NBSP + "H"


def bar_texts() -> list[str]:
    """The advance slide's rows: one per operator, the bars' reference, the ASCII row."""
    return (["|" + NBSP + o.char * BARS + NBSP + "|" for o in OPERATORS] + ["|" + NBSP + NBSP + "|"]
            + ["|" + NBSP + ASCII + NBSP + "|"])


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def ink_requests(face: str, page: str, n: int) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    for i, o in enumerate(OPERATORS):
        x, y = ink_spot(i)
        oid = f"opi_{n}_{i}"
        reqs += box(oid, page, x, y, INK_BOX_W, INK_BOX_H, ink_text(o), NEIGHBOUR, INK_SIZE)
        start = u16("H" + NBSP)
        reqs.append({"updateTextStyle": {"objectId": oid, "fields": "fontFamily",
                                         "textRange": {"type": "FIXED_RANGE", "startIndex": start,
                                                       "endIndex": start + u16(o.char)},
                                         "style": {"fontFamily": face}}})
    return reqs


def bar_requests(face: str, page: str, n: int) -> list[SlidesRequest]:
    reqs: list[SlidesRequest] = []
    for i, text in enumerate(bar_texts()):
        x, y = bar_spot(i)
        reqs += box(f"opa_{n}_{i}", page, x, y, BAR_BOX_W, BAR_BOX_H, text, face, BAR_SIZE)
    return reqs


@dataclass(frozen=True, kw_only=True)
class Pages:
    ink: str
    advance: str


def pages_of(first: str) -> list[Pages]:
    """Each face's two slides: the deck's first slide is the first face's ink slide."""
    return [Pages(ink=first if n == 0 else f"ops_ink_{n}", advance=f"ops_adv_{n}") for n in range(len(FACES))]


def all_requests(first: str, stale: list[str]) -> list[list[SlidesRequest]]:
    """One batch per face; the first also clears the first slide and makes every other slide."""
    pages = pages_of(first)
    setup: list[SlidesRequest] = [{"deleteObject": {"objectId": e}} for e in stale]
    setup += [{"createSlide": {"objectId": p.ink}} for p in pages[1:]]
    setup += [{"createSlide": {"objectId": p.advance}} for p in pages]
    batches: list[list[SlidesRequest]] = []
    for n, (face, p) in enumerate(zip(FACES, pages)):
        batches.append((setup if n == 0 else []) + ink_requests(face, p.ink, n) + bar_requests(face, p.advance, n))
    return batches


# ------------------------------------------------------------------ measuring


@dataclass(frozen=True, kw_only=True)
class Ink:
    """An operator's ink, per em: centre is its vertical middle above the baseline."""
    width: float
    height: float
    centre: float
    stroke: float


@dataclass(frozen=True, kw_only=True)
class Glyph:
    """One operator in one face (or TeX's font), per em of its size."""
    face: str
    op: str
    advance: float
    drawn: bool   # ink found between the H's (False: the ink figures are 0)
    width: float
    height: float
    centre: float
    stroke: float


def glyph(face: str, o: Operator, advance: float, ink: Ink | None) -> Glyph:
    if ink is None:
        return Glyph(face=face, op=o.char, advance=round(advance, 4), drawn=False, width=0.0, height=0.0, centre=0.0,
                     stroke=0.0)
    return Glyph(face=face, op=o.char, advance=round(advance, 4), drawn=True, width=ink.width, height=ink.height,
                 centre=ink.centre, stroke=ink.stroke)


def ink_of(mask: np.ndarray, baseline: int, em_px: float) -> Ink | None:
    """The ink of a mask whose baseline lies above row `baseline` (rows above it are above the line)."""
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if len(rows) == 0:
        return None
    top, bottom = int(rows[0]), int(rows[-1])
    area, edges = stroke(mask)
    return Ink(width=round((int(cols[-1]) - int(cols[0]) + 1) / em_px, 4), height=round((bottom - top + 1) / em_px, 4),
               centre=round((baseline - (top + bottom + 1) / 2) / em_px, 4),
               stroke=round(2 * area / edges / em_px, 5) if edges else 0.0)


def column_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) columns of each run of inked columns."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    on = mask.any(axis=0).tolist()
    for i, c in enumerate(on):
        if c and start is None:
            start = i
        elif not c and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(on) - 1))
    return runs


@dataclass(frozen=True, kw_only=True)
class Cell:
    """An "H op H" cell read off a thumbnail: the baseline (the row under the first H's foot) and
    the operator's columns, in the cell's crop (px)."""
    baseline: int
    left: int
    right: int


def read_cell(mask: np.ndarray) -> Cell | None:
    """None when nothing stands apart between the two H's (no ink, or ink touching an H)."""
    runs = column_runs(mask)
    if len(runs) < 3:
        return None
    first, last = runs[0], runs[-1]
    h_rows = np.where(mask[:, first[0]:first[1] + 1].any(axis=1))[0]
    return Cell(baseline=int(h_rows[-1]) + 1, left=first[1] + 1, right=last[0])


@dataclass(frozen=True, kw_only=True)
class Tile:
    """Where an operator's picture is, to put on the sheet: its image, baseline row and ink middle column."""
    image: Image.Image
    baseline: int
    middle: int
    em_px: float


@dataclass(frozen=True, kw_only=True)
class FaceRead:
    glyphs: list[Glyph]
    ascii: float  # the ASCII row's width (em), to tell a served face from Arial
    tiles: list[Tile | None]


def crop_box(x: float, y: float, w: float, h: float, k: float) -> tuple[int, int, int, int]:
    return int(x * k), int(y * k), int((x + w) * k), int((y + h) * k)


def measure_face(face: str, ink_path: Path, advance_path: Path) -> FaceRead:
    picture = Image.open(ink_path).convert("L")
    img = gray(ink_path)
    k = img.shape[1] / SLIDE_W
    em_px = INK_SIZE * k
    adv = gray(advance_path)
    ka = adv.shape[1] / SLIDE_W

    def bar_inner(i: int) -> float:
        x, y = bar_spot(i)
        return bars(adv, ka, x, y, BAR_BOX_W, BAR_SIZE).inner

    ref = bar_inner(len(OPERATORS))
    glyphs: list[Glyph] = []
    tiles: list[Tile | None] = []
    for i, o in enumerate(OPERATORS):
        x0, y0, x1, y1 = crop_box(*ink_spot(i), INK_BOX_W, INK_BOX_H, k)
        mask = img[y0:y1, x0:x1] < 128
        cell = read_cell(mask)
        ink = ink_of(mask[:, cell.left:cell.right], cell.baseline, em_px) if cell is not None else None
        glyphs.append(glyph(face, o, (bar_inner(i) - ref) / BARS / BAR_SIZE, ink))
        if cell is None or ink is None:
            tiles.append(None)
            continue
        cols = np.where(mask[:, cell.left:cell.right].any(axis=0))[0]
        alone = picture.crop((x0, y0, x1, y1))  # (the H's whited out)
        ImageDraw.Draw(alone).rectangle((0, 0, cell.left - 1, alone.height), fill=255)
        ImageDraw.Draw(alone).rectangle((cell.right, 0, alone.width, alone.height), fill=255)
        middle = cell.left + (int(cols[0]) + int(cols[-1])) // 2
        tiles.append(Tile(image=alone, baseline=cell.baseline, middle=middle, em_px=em_px))
    ascii_em = (bar_inner(len(OPERATORS) + 1) - ref) / BAR_SIZE
    return FaceRead(glyphs=glyphs, ascii=round(ascii_em, 4), tiles=tiles)


# ------------------------------------------------------------------ TeX's references


def tex_font(type1: Path, piece: Piece, px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(type1 / TEX_FONTS[piece.font]), px, encoding="unic" if piece.unicode else "ADBC",
                              layout_engine=ImageFont.Layout.BASIC)


@dataclass(frozen=True, kw_only=True)
class Run:
    """Glyphs drawn one after the other on a baseline: a font, what it is handed, the kern after (em)."""
    pieces: list[tuple[ImageFont.FreeTypeFont, str, float]]


def draw_run(run: Run, em_px: float) -> tuple[Image.Image, int]:
    """The run drawn black on white with its baseline at a row of its own: (picture, baseline)."""
    pic = Image.new("L", (int(em_px * 4), int(em_px * 4)), 255)
    draw = ImageDraw.Draw(pic)
    x, base = em_px * 0.5, int(em_px * 2.5)
    for font, text, kern in run.pieces:
        draw.text((x, base), text, font=font, fill=0, anchor="ls")
        x += font.getlength(text) + kern * em_px
    return pic, base


def tex_reference(type1: Path, o: Operator, em_px: float) -> tuple[Glyph, Tile]:
    """TeX's glyph at the thumbnail's em (px), measured as measure_face measures Slides'; its advance
    from a 1000 px em (the drawn size rounds advances to whole pixels)."""
    pic, base = draw_run(Run(pieces=[(tex_font(type1, p, em_px), p.char, p.kern) for p in o.pieces]), em_px)
    advance = sum(tex_font(type1, p, 1000.0).getlength(p.char) / 1000.0 + p.kern for p in o.pieces)
    mask = np.asarray(pic, dtype=np.float64) < 128
    ink = ink_of(mask, base, em_px)
    if ink is None:
        raise SystemExit(f"TeX's {o.tex} drew nothing")
    if o.on_axis:  # TeX shifts it so that its middle is on the axis
        base -= round((ink.centre - AXIS) * em_px)
        ink = Ink(width=ink.width, height=ink.height, centre=AXIS, stroke=ink.stroke)
    cols = np.where(mask.any(axis=0))[0]
    middle = (int(cols[0]) + int(cols[-1])) // 2
    tex_face = "TeX " + "+".join(dict.fromkeys(p.font for p in o.pieces))
    return glyph(tex_face, o, advance, ink), Tile(image=pic, baseline=base, middle=middle, em_px=em_px)


# ------------------------------------------------------------------ ranking


@dataclass(frozen=True, kw_only=True)
class Rank:
    """A face's operator against TeX's: height, advance and stroke as ratios less one, the
    centre as em apart; score in units of TOLERANCE, height and centre only."""
    face: str
    height: float
    centre: float
    advance: float
    stroke: float
    score: float


def rank_of(g: Glyph, tex: Glyph) -> Rank:
    h = g.height / tex.height - 1
    c = g.centre - tex.centre
    a = g.advance / tex.advance - 1 if tex.advance else 0.0
    s = g.stroke / tex.stroke - 1 if tex.stroke else 0.0
    return Rank(face=g.face, height=round(h, 3), centre=round(c, 3), advance=round(a, 3), stroke=round(s, 3),
                score=round(abs(h) / TOLERANCE["height"] + abs(c) / TOLERANCE["centre"], 3))


def within(r: Rank) -> bool:
    return all(abs(v) <= TOLERANCE[k] for k, v in
               (("height", r.height), ("centre", r.centre), ("advance", r.advance), ("stroke", r.stroke)))


def ranking(glyphs: list[Glyph], tex: list[Glyph], served: list[str]) -> dict[str, list[Rank]]:
    """Per operator, the served faces that draw it, nearest TeX first."""
    out: dict[str, list[Rank]] = {}
    for o, t in zip(OPERATORS, tex):
        found = [rank_of(g, t) for g in glyphs if g.op == o.char and g.face in served and g.drawn]
        out[o.char] = sorted(found, key=lambda r: r.score)
    return out


def describe(r: Rank) -> str:
    return (f"{r.face} h{r.height:+.0%} c{r.centre:+.3f} a{r.advance:+.0%} s{r.stroke:+.0%}"
            + (" (within)" if within(r) else ""))


def summary(ranks: dict[str, list[Rank]], tex: list[Glyph], today: str) -> list[str]:
    lines = [f"per operator: TeX's height h, centre c (em above the baseline; axis {AXIS}), advance a; then the "
             "faces nearest by height and axis, as height %, centre em apart, advance %, stroke % of TeX's; "
             f"(within) = all four inside {TOLERANCE}"]
    wins: dict[str, int] = {}
    for o, t in zip(OPERATORS, tex):
        ranked = ranks[o.char]
        now = next((r for r in ranked if r.face == today), None)
        head = f"{o.char} {o.tex:<16} TeX h{t.height:.3f} c{t.centre:.3f} a{t.advance:.3f} | "
        if not ranked:
            lines.append(head + "no face draws it")
            continue
        wins[ranked[0].face] = wins.get(ranked[0].face, 0) + 1
        best = "; ".join(describe(r) for r in ranked[:3])
        lines.append(head + best + (f" | today {describe(now)}" if now is not None else f" | today: {today} drew nothing"))
    lines.append("nearest face per operator: " + ", ".join(f"{f} {n}" for f, n in
                                                            sorted(wins.items(), key=lambda kv: -kv[1])))
    return lines


# ------------------------------------------------------------------ the sheet


def tile_image(t: Tile | None, em_px: float) -> Image.Image:
    """2 em square around an operator: 1.4 em above its baseline, 0.6 below, centred on its ink."""
    side = int(em_px * 2)
    out = Image.new("L", (side, side), 255)
    if t is None:
        ImageDraw.Draw(out).line([(0, 0), (side, side)], fill=160)
        return out
    left, top = t.middle - side // 2, t.baseline - int(em_px * 1.4)
    box_ = (max(left, 0), max(top, 0), min(left + side, t.image.width), min(top + side, t.image.height))
    out.paste(t.image.crop(box_), (box_[0] - left, box_[1] - top))
    return out


def write_sheet(rows: list[tuple[str, list[Tile | None]]], em_px: float, path: Path) -> None:
    side, label = int(em_px * 2), 170
    sheet = Image.new("L", (label + side * len(OPERATORS), 20 + side * len(rows)), 255)
    draw = ImageDraw.Draw(sheet)
    for j, o in enumerate(OPERATORS):
        draw.text((label + j * side + 4, 4), o.tex, fill=0)
    for i, (name, tiles) in enumerate(rows):
        y = 20 + i * side
        draw.text((4, y + side // 2), name, fill=0)
        for j, t in enumerate(tiles):
            sheet.paste(tile_image(t, em_px), (label + j * side, y))
        draw.line([(0, y), (sheet.width, y)], fill=200)
    sheet.save(path)


# ------------------------------------------------------------------ dry run


def check_requests(batches: list[list[SlidesRequest]]) -> int:
    """Every object id unique and 5-50 characters, every box on the slide; the request count."""
    ids: list[str] = []
    for reqs in batches:
        for r in reqs:
            if "createShape" in r:
                shape = r["createShape"]
                ids.append(shape.get("objectId", ""))
            if "createSlide" in r:
                ids.append(r["createSlide"].get("objectId", ""))
    bad = [i for i in ids if not 5 <= len(i) <= 50]
    if bad or len(set(ids)) != len(ids):
        raise SystemExit(f"bad object ids: {bad or 'duplicates'}")
    for i in range(len(OPERATORS)):
        x, y = ink_spot(i)
        if x + INK_BOX_W > SLIDE_W or y + INK_BOX_H > SLIDE_H:
            raise SystemExit(f"ink cell {i} leaves the slide")
    for i in range(len(bar_texts())):
        x, y = bar_spot(i)
        if x + BAR_BOX_W > SLIDE_W or y + BAR_BOX_H > SLIDE_H:
            raise SystemExit(f"advance cell {i} leaves the slide")
    return sum(len(b) for b in batches)


def stand_in(font: ImageFont.FreeTypeFont, ink_path: Path, advance_path: Path) -> None:
    """A thumbnail as Slides would draw both slides, in one local face (the H's too)."""
    k = THUMB_W / SLIDE_W
    size = (THUMB_W, int(SLIDE_H * k))
    for path, texts, spot, px in ((ink_path, [ink_text(o) for o in OPERATORS], ink_spot, INK_SIZE),
                                  (advance_path, bar_texts(), bar_spot, BAR_SIZE)):
        pic = Image.new("L", size, 255)
        draw = ImageDraw.Draw(pic)
        sized = font.font_variant(size=px * k)
        for i, text in enumerate(texts):
            x, y = spot(i)
            draw.text(((x + INSET) * k, (y + INSET + px) * k), text, font=sized, fill=0, anchor="ls")
        pic.save(path)


def font_glyph(font: ImageFont.FreeTypeFont, name: str, o: Operator, em_px: float) -> Glyph:
    """What FreeType says of a local face's operator: the stand-in's truth."""
    sized = font.font_variant(size=em_px)
    pic, base = draw_run(Run(pieces=[(sized, o.char, 0.0)]), em_px)
    advance = font.font_variant(size=1000.0).getlength(o.char) / 1000.0
    return glyph(name, o, advance, ink_of(np.asarray(pic, dtype=np.float64) < 128, base, em_px))


LOCAL_FACES = [("Segoe UI Symbol", "C:/Windows/Fonts/seguisym.ttf", 0), ("Cambria Math", "C:/Windows/Fonts/cambria.ttc", 1)]


def dry_run() -> None:
    out = OUT / "dry-run"
    out.mkdir(parents=True, exist_ok=True)
    batches = all_requests("dry_first_page", ["dry_placeholder_title", "dry_placeholder_body"])
    count = check_requests(batches)
    (out / "requests.json").write_text(json.dumps([[slides_json(r) for r in b] for b in batches], ensure_ascii=False,
                                                  indent=1) + "\n", encoding="utf-8")
    print(f"{count} requests in {len(batches)} batches ({len(FACES)} faces x 2 slides) -> {out / 'requests.json'}")
    em_px = INK_SIZE * THUMB_W / SLIDE_W
    type1 = miktex_type1()
    refs = [tex_reference(type1, o, em_px) for o in OPERATORS]
    tex = [g for g, _ in refs]
    for g in tex:
        print(f"  {g.op} {g.face:<16} advance {g.advance:.3f} width {g.width:.3f} height {g.height:.3f} "
              f"centre {g.centre:+.3f} stroke {g.stroke:.4f}")
    rows: list[tuple[str, list[Tile | None]]] = [("TeX", [t for _, t in refs])]
    glyphs: list[Glyph] = []
    worst = {"height": 0.0, "centre": 0.0, "advance": 0.0, "width": 0.0}
    for name, path, index in LOCAL_FACES:
        if not Path(path).exists():
            print("  (no", path, "here: skipped)")
            continue
        font = ImageFont.truetype(path, 10, index=index, layout_engine=ImageFont.Layout.BASIC)
        ink_path, advance_path = out / f"ink-{name.replace(' ', '')}.png", out / f"advance-{name.replace(' ', '')}.png"
        stand_in(font, ink_path, advance_path)
        read = measure_face(name, ink_path, advance_path)
        glyphs += read.glyphs
        rows.append((name, read.tiles))
        for o, g in zip(OPERATORS, read.glyphs):
            truth = font_glyph(font, name, o, em_px)
            if g.drawn != truth.drawn:
                raise SystemExit(f"{name} {o.tex}: read drawn={g.drawn}, FreeType {truth.drawn}")
            for key, a, b in (("height", g.height, truth.height), ("centre", g.centre, truth.centre),
                              ("advance", g.advance, truth.advance), ("width", g.width, truth.width)):
                worst[key] = max(worst[key], abs(a - b))
    print("  stand-in thumbnails read against FreeType, worst |difference| (em):",
          ", ".join(f"{k} {v:.4f}" for k, v in worst.items()))
    if max(worst.values()) > 0.03:
        raise SystemExit("the stand-in's cells were misread")
    served = [name for name, path, _ in LOCAL_FACES if Path(path).exists()]
    ranks = ranking(glyphs, tex, served)
    lines = summary(ranks, tex, served[0] if served else "Lato")
    write_sheet(rows, em_px, out / "sheet.png")
    write_results(out, tex, glyphs, {}, ranks, lines)
    for line in lines:
        print(line)


# ------------------------------------------------------------------ live


def write_results(out: Path, tex: list[Glyph], glyphs: list[Glyph], ascii_rows: dict[str, float],
                  ranks: dict[str, list[Rank]], lines: list[str]) -> None:
    (out / "result.json").write_text(json.dumps({
        "source": "tools/probe_math_operators.py", "size": INK_SIZE, "axis": AXIS,
        "operators": [{"char": o.char, "tex": o.tex} for o in OPERATORS],
        "tex": [asdict(g) for g in tex], "glyphs": [asdict(g) for g in glyphs], "ascii": ascii_rows,
        "ranking": {k: [asdict(r) for r in v] for k, v in ranks.items()}, "summary": lines},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", out / "result.json", out / "summary.txt", "and", out / "sheet.png")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe math operators"}))
    pid = presentation_id(pres)
    slide = pres.get("slides", [])[0]
    first = object_id(slide)
    batches = all_requests(first, [object_id(e) for e in slide.get("pageElements", [])])
    reads: list[FaceRead] = []
    try:
        for reqs in batches:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        for face, p in zip(FACES, pages_of(first)):
            stem = face.replace(" ", "")
            ink_path, advance_path = OUT / f"ink-{stem}.png", OUT / f"advance-{stem}.png"
            save_thumbnail(slides, pid, p.ink, ink_path, None)
            save_thumbnail(slides, pid, p.advance, advance_path, None)
            reads.append(measure_face(face, ink_path, advance_path))
            print(face, "ascii", reads[-1].ascii, "drawn", sum(g.drawn for g in reads[-1].glyphs), "of", len(OPERATORS))
    finally:
        execute(drive_service(None).files().delete(fileId=pid))
    k = gray(OUT / f"ink-{FACES[0].replace(' ', '')}.png").shape[1] / SLIDE_W
    em_px = INK_SIZE * k
    type1 = miktex_type1()
    refs = [tex_reference(type1, o, em_px) for o in OPERATORS]
    tex = [g for g, _ in refs]
    ascii_rows = {face: r.ascii for face, r in zip(FACES, reads)}
    arial = ascii_rows["Arial"]
    served = [f for f in FACES if f in ALWAYS_SERVED or abs(ascii_rows[f] - arial) > 0.003]
    print("served:", ", ".join(served), "| not served:", ", ".join(f for f in FACES if f not in served) or "none")
    glyphs = [g for r in reads for g in r.glyphs]
    ranks = ranking(glyphs, tex, served)
    lines = summary(ranks, tex, "Lato")
    write_sheet([("TeX", [t for _, t in refs])] + [(f, r.tiles) for f, r in zip(FACES, reads)], em_px,
                OUT / "sheet.png")
    write_results(OUT, tex, glyphs, ascii_rows, ranks, lines)
    for line in lines:
        print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true", help="build the requests and TeX's references, no Google")
    if parser.parse_args().dry_run:
        dry_run()
    else:
        main()
