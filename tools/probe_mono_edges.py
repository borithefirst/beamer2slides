"""Probe: what gap Slides draws between a prose word and an inline code word, per separator.

In the PDF, the space at either edge of a \\texttt run inside prose is the prose font's word space
(0.326-0.334 em of CM Sans on the real decks, before and after the code alike). In Slides the
prose is Lato at 0.98 of its PDF size and the code Roboto Mono at 0.875, and a plain space in the
Lato run is 0.192 Lato em: each edge comes out about 0.14 em narrow, code words crowding their
neighbours. `mono_edges` writes thin spaces before the space in the prose run to bring the gap
back; their width is what this measures.

For Lato and PT Serif prose, at 11, 18 and 28 pt (Roboto Mono at MONO_RATIO of that), both ways
(prose -> code, code -> prose), and each separator - none, a space, a no-break space, U+2009 thin,
U+200A hair, U+2008 punctuation, U+2002 en, U+2006/U+2005/U+2004 sixth/fourth/third - written
in the prose run's font or in the code run's, plus the prose-run pairs `mono_edges` could write
(thin+space, thin+thin+space, hair+space, hair+hair+space, sixth+space): a row "H s H H s H ..."
of REPEAT units (one Lato H, the separator, one Roboto Mono H; units side by side), and the
period of the prose H's ink centroids less the same row's with no separator is the separator's
width, per em of the prose size. A separator drawing ink (a fallback's box) is flagged.

Prints the recommendation for `mono_edges` (EDGE_SPACE, EDGE_SPACE_EM, PLAIN_SPACE_EM): the
separator in the Lato run that, after the plain space, brings the gap nearest TeX's
(TEX_GAP_EM / LATO_SIZE Lato em), with the widths it was measured at. Results:
out/probe_mono_edges/ (result.json, summary.txt, the thumbnails). The live run makes one scratch
deck and deletes that deck only.

--dry-run: no Google. Builds every request (checked: ids unique, 5-50 characters, boxes on the
slide, style ranges within their text; out/probe_mono_edges/dry-run/requests.json), draws a
stand-in thumbnail per slide in local faces (LatoWeb, Times, Consolas) and runs the whole
measurement on it: what it reads against FreeType's advances of the same characters proves the
rows, units and centroids are read right.

Usage: python tools/probe_mono_edges.py [--dry-run]
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_math_glyphs import SLIDE_W, gray, style  # noqa: E402

from beamer2slides.google_auth import drive_service, slides_service  # noqa: E402
from beamer2slides.google_types import SlidesRequest, object_id, presentation_id, slides_json  # noqa: E402
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box  # noqa: E402
from beamer2slides.mono_edges import LATO_SIZE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_mono_edges"
SLIDE_H = 405.0
THUMB_W = 1600
PROSE_FACES = ["Lato", "PT Serif"]
MONO = "Roboto Mono"
MONO_RATIO = 0.875 / 0.98  # Roboto Mono's size against Lato's, as FontMapper sets CMTT and CMSS
SIZES = [11.0, 18.0, 28.0]
REPEAT = 8
INSET = 7.2
TEX_GAP_EM = 0.333  # TeX's word space beside inline code (CM Sans; PDF em)
SEPARATORS = {"space": " ", "nbsp": "\xa0", "thin": "\u2009", "hair": "\u200a", "punct": "\u2008",
              "en": "\u2002", "sixth": "\u2006", "fourth": "\u2005", "third": "\u2004"}
PAIRS = {"thin+space": "\u2009 ", "thin+thin+space": "\u2009\u2009 ", "hair+space": "\u200a ",
         "hair+hair+space": "\u200a\u200a ", "sixth+space": "\u2006 "}
Way = Literal["into", "out"]  # prose -> code, code -> prose
Side = Literal["prose", "mono", "-"]  # the run the separator is written in
WAYS: list[Way] = ["into", "out"]


@dataclass(frozen=True, kw_only=True)
class Case:
    face: str
    size: float
    way: Way
    name: str
    sep: str
    side: Side


def cases() -> list[Case]:
    out: list[Case] = []
    for face in PROSE_FACES:
        for size in SIZES:
            for way in WAYS:
                out.append(Case(face=face, size=size, way=way, name="none", sep="", side="-"))
                for name, sep in SEPARATORS.items():
                    for side in ("prose", "mono"):
                        out.append(Case(face=face, size=size, way=way, name=name, sep=sep, side=side))
                for name, sep in PAIRS.items():
                    out.append(Case(face=face, size=size, way=way, name=name, sep=sep, side="prose"))
    return out


@dataclass(frozen=True, kw_only=True)
class Piece:
    text: str
    mono: bool


def pieces(c: Case) -> list[Piece]:
    """A row's characters and the run each is in: REPEAT units side by side."""
    sep = [Piece(text=c.sep, mono=c.side == "mono")] if c.sep else []
    prose, code = [Piece(text="H", mono=False)], [Piece(text="H", mono=True)]
    unit = prose + sep + code if c.way == "into" else code + sep + prose
    return unit * REPEAT


def row_text(ps: list[Piece]) -> str:
    return "".join(p.text for p in ps)


def mono_ranges(ps: list[Piece]) -> list[tuple[int, int]]:
    """UTF-16 [start, end) of each stretch in the code run."""
    out: list[tuple[int, int]] = []
    at = 0
    for p in ps:
        n = len(p.text.encode("utf-16-le")) // 2
        if p.mono and out and out[-1][1] == at:
            out[-1] = (out[-1][0], at + n)
        elif p.mono:
            out.append((at, at + n))
        at += n
    return out


def mono_size(size: float) -> float:
    return round(size * MONO_RATIO, 2)


# ------------------------------------------------------------------ layout


@dataclass(frozen=True, kw_only=True)
class Spot:
    page: int
    x: float
    y: float
    w: float
    h: float


def box_size(c: Case) -> tuple[float, float]:
    """Room for a row: H's at up to 0.75 em, Roboto Mono's 0.6, separators up to 0.6 of the code's size."""
    unit = 0.75 + 0.6 * MONO_RATIO + 0.6
    return min(SLIDE_W - 10.0, REPEAT * unit * c.size * 1.15 + 2 * INSET), c.size * 1.7 + 2 * INSET


def layout(cs: list[Case]) -> list[Spot]:
    """Rows on shelves, left to right and top to bottom, a new slide when one is full."""
    spots: list[Spot] = []
    page, x, y, shelf = 0, 5.0, 3.0, 0.0
    for c in cs:
        w, h = box_size(c)
        if x + w > SLIDE_W - 5.0:
            x, y, shelf = 5.0, y + shelf + 2.0, 0.0
        if y + h > SLIDE_H - 3.0:
            page, x, y, shelf = page + 1, 5.0, 3.0, 0.0
        spots.append(Spot(page=page, x=x, y=y, w=w, h=h))
        x, shelf = x + w + 4.0, max(shelf, h)
    return spots


def page_ids(first: str, count: int) -> list[str]:
    return [first] + [f"monoedge_p{n}" for n in range(1, count)]


def row_requests(c: Case, i: int, spot: Spot, page: str) -> list[SlidesRequest]:
    oid = f"monoedge_r{i}"
    ps = pieces(c)
    reqs = [text_box(oid, page, spot.x, spot.y, spot.w, spot.h)] + style(oid, row_text(ps), c.face, c.size)
    for start, end in mono_ranges(ps):
        reqs.append({"updateTextStyle": {"objectId": oid, "fields": "fontFamily,fontSize",
                                         "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end},
                                         "style": {"fontFamily": MONO, "fontSize": pt(mono_size(c.size))}}})
    return reqs


def all_requests(first: str, stale: list[str], cs: list[Case], spots: list[Spot]) -> list[list[SlidesRequest]]:
    """One batch per slide; the first clears the deck's first slide and makes the others."""
    pages = page_ids(first, max(s.page for s in spots) + 1)
    setup: list[SlidesRequest] = [{"deleteObject": {"objectId": e}} for e in stale]
    setup += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    batches: list[list[SlidesRequest]] = [[] for _ in pages]
    batches[0] += setup
    for i, (c, s) in enumerate(zip(cs, spots)):
        batches[s.page] += row_requests(c, i, s, pages[s.page])
    return batches


def check_requests(batches: list[list[SlidesRequest]], cs: list[Case], spots: list[Spot]) -> int:
    """Ids unique and 5-50 characters, boxes on the slide, style ranges inside their text."""
    ids: list[str] = []
    for reqs in batches:
        for r in reqs:
            if "createShape" in r:
                ids.append(r["createShape"].get("objectId", ""))
            if "createSlide" in r:
                ids.append(r["createSlide"].get("objectId", ""))
    bad = [i for i in ids if not 5 <= len(i) <= 50]
    if bad or len(set(ids)) != len(ids):
        raise SystemExit(f"bad object ids: {bad or 'duplicates'}")
    for c, s in zip(cs, spots):
        if s.x < 0 or s.y < 0 or s.x + s.w > SLIDE_W or s.y + s.h > SLIDE_H:
            raise SystemExit(f"{c} leaves the slide at {s}")
        length = len(row_text(pieces(c)).encode("utf-16-le")) // 2
        if any(not 0 <= a < b <= length for a, b in mono_ranges(pieces(c))):
            raise SystemExit(f"{c}: a style range outside its text")
    return sum(len(b) for b in batches)


# ------------------------------------------------------------------ measuring


@dataclass(frozen=True, kw_only=True)
class Read:
    """One row off a thumbnail: the prose H's period (px), or why there is none."""
    period: float | None
    inked: bool  # more ink runs than H's: the separator draws something
    runs: int


def column_runs(on: list[bool]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(on) - 1))
    return out


def read_row(img: np.ndarray, k: float, c: Case, s: Spot) -> Read:
    crop = img[int(s.y * k):int((s.y + s.h) * k), int(s.x * k):int((s.x + s.w) * k)]
    dark = 255.0 - crop
    runs = column_runs((crop < 128).any(axis=0).tolist())
    if len(runs) != 2 * REPEAT:
        return Read(period=None, inked=len(runs) > 2 * REPEAT, runs=len(runs))
    weight = dark.sum(axis=0)
    first = 0 if c.way == "into" else 1
    centres: list[float] = []
    for a, b in runs[first::2]:
        cols = np.arange(a, b + 1, dtype=np.float64)
        w = weight[a:b + 1]
        centres.append(float((cols * w).sum() / w.sum()))
    return Read(period=(centres[-1] - centres[0]) / (REPEAT - 1), inked=False, runs=len(runs))


@dataclass(frozen=True, kw_only=True)
class Width:
    face: str
    size: float
    way: Way
    name: str
    side: Side
    em: float | None  # the separator's width per em of the prose size (None: unreadable)
    inked: bool


def widths(cs: list[Case], reads: list[Read], k: float) -> list[Width]:
    base = {(c.face, c.size, c.way): r.period for c, r in zip(cs, reads) if c.name == "none"}
    out: list[Width] = []
    for c, r in zip(cs, reads):
        if c.name == "none":
            continue
        none = base[(c.face, c.size, c.way)]
        em = None if r.period is None or none is None else round((r.period - none) / (k * c.size), 4)
        out.append(Width(face=c.face, size=c.size, way=c.way, name=c.name, side=c.side, em=em, inked=r.inked))
    return out


def measure(cs: list[Case], spots: list[Spot], paths: list[Path]) -> tuple[list[Read], float]:
    imgs = [gray(p) for p in paths]
    k = imgs[0].shape[1] / SLIDE_W
    return [read_row(imgs[s.page], k, c, s) for c, s in zip(cs, spots)], k


# ------------------------------------------------------------------ summary


@dataclass(frozen=True, kw_only=True)
class Mean:
    face: str
    name: str
    side: Side
    em: float | None
    spread: float  # max - min over sizes and ways
    inked: bool


def means(ws: list[Width]) -> list[Mean]:
    keys = list(dict.fromkeys((w.face, w.name, w.side) for w in ws))
    out: list[Mean] = []
    for face, name, side in keys:
        mine = [w for w in ws if (w.face, w.name, w.side) == (face, name, side)]
        got = [w.em for w in mine if w.em is not None]
        out.append(Mean(face=face, name=name, side=side, em=round(sum(got) / len(got), 4) if got else None,
                        spread=round(max(got) - min(got), 4) if got else 0.0, inked=any(w.inked for w in mine)))
    return out


def recommend(ms: list[Mean]) -> list[str]:
    """The single separator in the Lato run that, after a plain space, comes nearest TeX's gap."""
    lato = {m.name: m for m in ms if m.face == "Lato" and m.side == "prose"}
    space = lato["space"].em
    if space is None:
        return ["no plain space was read in Lato: no recommendation"]
    target = TEX_GAP_EM / LATO_SIZE
    lines = [f"target: {TEX_GAP_EM} PDF em at Lato {LATO_SIZE} = {target:.3f} Lato em; a plain space is {space:.3f}"]
    best: tuple[float, str, float, int] | None = None
    for name, sep in SEPARATORS.items():
        m = lato.get(name)
        if name in ("space", "nbsp") or m is None or m.em is None or m.inked or m.em <= 0.01 or m.spread > 0.03:
            continue
        n = max(1, round((target - space) / m.em))
        miss = space + n * m.em - target
        lines.append(f"  {name:<7} U+{ord(sep):04X} {m.em:.3f} em: space + {n} x = {space + n * m.em:.3f} ({miss:+.3f})")
        if best is None or abs(miss) < abs(best[0]):
            best = (miss, sep, m.em, n)
    for name in PAIRS:
        m = lato.get(name)
        if m is not None and m.em is not None:
            lines.append(f"  measured pair {name:<16} {m.em:.3f} em")
    if best is None:
        lines.append("no separator drew nothing at a steady width: no recommendation")
        return lines
    miss, sep, em, n = best
    lines.append(f"mono_edges: EDGE_SPACE = \"\\u{ord(sep):04x}\", EDGE_SPACE_EM = {em:.3f}, PLAIN_SPACE_EM = "
                 f"{space:.3f} (one edge: space + {n} x, {miss:+.3f} Lato em from TeX)")
    return lines


def summary(ms: list[Mean]) -> list[str]:
    lines = ["separator widths, em of the prose size, mean over 11/18/28 pt and both ways (spread; inked = draws ink)"]
    for m in ms:
        em = "unread" if m.em is None else f"{m.em:+.4f}"
        lines.append(f"{m.face:<9} {m.name:<16} in {m.side:<5} {em} (spread {m.spread:.4f})" + (" inked" if m.inked else ""))
    return lines + recommend(ms)


def write_results(out: Path, ws: list[Width], lines: list[str]) -> None:
    (out / "result.json").write_text(json.dumps({
        "source": "tools/probe_mono_edges.py", "mono": MONO, "mono_ratio": MONO_RATIO, "repeat": REPEAT,
        "widths": [asdict(w) for w in ws], "summary": lines}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", out / "result.json", "and", out / "summary.txt")


# ------------------------------------------------------------------ dry run

LOCAL = {"Lato": "C:/Windows/Fonts/LatoWeb-Regular.ttf", "PT Serif": "C:/Windows/Fonts/times.ttf",
         MONO: "C:/Windows/Fonts/consola.ttf"}


def local_font(face: str, px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(LOCAL[face], px, layout_engine=ImageFont.Layout.BASIC)


def stand_in(cs: list[Case], spots: list[Spot], paths: list[Path]) -> None:
    """Each slide as Slides would draw it, in local faces, one character at a time at its advance."""
    k = THUMB_W / SLIDE_W
    pics = [Image.new("L", (THUMB_W, int(SLIDE_H * k)), 255) for _ in paths]
    for c, s in zip(cs, spots):
        draw = ImageDraw.Draw(pics[s.page])
        prose, code = local_font(c.face, c.size * k), local_font(MONO, mono_size(c.size) * k)
        x, base = (s.x + INSET) * k, (s.y + INSET + c.size) * k
        for p in pieces(c):
            font = code if p.mono else prose
            for ch in p.text:
                draw.text((x, base), ch, font=font, fill=0, anchor="ls")
                x += font.getlength(ch)
    for pic, path in zip(pics, paths):
        pic.save(path)


def truth(c: Case, k: float) -> tuple[float, bool]:
    """FreeType's width of the separator (prose em) and whether it draws ink, in the stand-in's faces
    at the size drawn (hinting rounds small advances)."""
    px = (mono_size(c.size) if c.side == "mono" else c.size) * k
    font = local_font(MONO if c.side == "mono" else c.face, px)
    inked = False
    for ch in c.sep:
        x0, y0, x1, y1 = font.getbbox(ch)
        inked = inked or (x1 > x0 and y1 > y0)
    return sum(font.getlength(ch) for ch in c.sep) / (c.size * k), inked


def dry_run() -> None:
    out = OUT / "dry-run"
    out.mkdir(parents=True, exist_ok=True)
    cs = cases()
    spots = layout(cs)
    batches = all_requests("dry_first_page", ["dry_placeholder_title", "dry_placeholder_body"], cs, spots)
    count = check_requests(batches, cs, spots)
    (out / "requests.json").write_text(json.dumps([[slides_json(r) for r in b] for b in batches], ensure_ascii=False,
                                                  indent=1) + "\n", encoding="utf-8")
    print(f"{len(cs)} rows, {count} requests on {len(batches)} slides -> {out / 'requests.json'}")
    if not all(Path(p).exists() for p in LOCAL.values()):
        print("  (a stand-in face is missing here: measurement not tried)")
        return
    paths = [out / f"slide-{n:02d}.png" for n in range(len(batches))]
    stand_in(cs, spots, paths)
    reads, k = measure(cs, spots, paths)
    ws = widths(cs, reads, k)
    worst, wrong = 0.0, 0
    for c, w in zip([c for c in cs if c.name != "none"], ws):
        em, inked = truth(c, k)
        if inked != w.inked:
            wrong += 1
            print(f"  {c.face} {c.size} {c.way} {c.name} in {c.side}: read inked={w.inked}, FreeType {inked}")
        elif not inked:
            if w.em is None:
                raise SystemExit(f"{c}: unread on the stand-in")
            worst = max(worst, abs(w.em - em))
    print(f"  stand-in read against FreeType: worst |difference| {worst:.4f} prose em, {wrong} ink flags wrong")
    if worst > 0.02 or wrong:
        raise SystemExit("the stand-in's rows were misread")
    lines = summary(means(ws))
    write_results(out, ws, lines)
    for line in lines:
        print(line)


# ------------------------------------------------------------------ live


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cs = cases()
    spots = layout(cs)
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe mono edges"}))
    pid = presentation_id(pres)
    slide = pres.get("slides", [])[0]
    first = object_id(slide)
    batches = all_requests(first, [object_id(e) for e in slide.get("pageElements", [])], cs, spots)
    pages = page_ids(first, len(batches))
    paths = [OUT / f"slide-{n:02d}.png" for n in range(len(batches))]
    try:
        for reqs in batches:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        for page, path in zip(pages, paths):
            save_thumbnail(slides, pid, page, path, None)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))
    reads, k = measure(cs, spots, paths)
    unread = [c for c, r in zip(cs, reads) if r.period is None and not r.inked]
    for c in unread:
        print(f"  unread: {c.face} {c.size} {c.way} {c.name} in {c.side} (a row wrapped or letters touch)")
    ws = widths(cs, reads, k)
    lines = summary(means(ws))
    write_results(OUT, ws, lines)
    for line in lines:
        print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true", help="build and check the requests, read a stand-in; no Google")
    if parser.parse_args().dry_run:
        dry_run()
    else:
        main()
