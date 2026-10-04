"""Probe: the hair space (U+200A) `script_space` writes at the end of a sub/superscript run.

After a script Slides' advance falls short of where TeX sets the next glyph (0.12 em of the line
on the real decks), so classify closes the script run with hair spaces (`script_space`), which
emit and the layout oracle measure at SCRIPT_SPACE_EM of the run's drawn size (0.665 of its size,
`emit_widths.SCRIPT_SIZE`), the width the mono-edges probe read for U+200A in a Lato run at its
size. Two questions only Google answers:

1. widths: is a hair space inside a SUBSCRIPT/SUPERSCRIPT run drawn at the script's size? For
   Lato and PT Serif italic at 11, 18 and 28 pt, rows of REPEAT units "H" + script "o" followed by
   0, 1, 2 or 4 hair spaces in the script run; the H's period less the bare row's is the hair
   spaces' width, per em of the run's size (expected SCRIPT_SPACE_EM x SCRIPT_SIZE).
2. breaks: does Slides break a line at a hair space? "aaaa aaaa D" + script "k" + three hair
   spaces + ",bbbb..." (a word longer than the box) against the same with no hair space: where
   Slides breaks only at the word space the first line ends after "aaaa aaaa" in both; where it
   breaks at a hair space the first line holds "Dk" too. Like a thin space or ZWSP (r10) it should
   not; if it does, `script_space` must glue the hair spaces (or write them only before a word
   space TeX would have broken at anyway).

Results: out/probe_script_space/ (result.json, summary.txt, thumbnails). The live run makes one
scratch deck and deletes that deck only.

--dry-run: no Google. Builds and checks every request (ids unique, 5-50 characters, boxes on the
slide, style ranges within their text; out/probe_script_space/dry-run/requests.json), draws a
stand-in thumbnail in local faces (scripts at SCRIPT_SIZE of their size, no break at a hair space)
and reads it as the live run would, against FreeType's advances.

Usage: python tools/probe_script_space.py [--dry-run]
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_math_glyphs import SLIDE_W, gray  # noqa: E402
from tools.probe_mono_edges import LOCAL, column_runs, local_font  # noqa: E402

from beamer2slides.emit_widths import SCRIPT_SIZE  # noqa: E402
from beamer2slides.google_auth import drive_service, slides_service  # noqa: E402
from beamer2slides.google_types import SlidesRequest, object_id, presentation_id, slides_json  # noqa: E402
from beamer2slides.gslides import execute, pt, save_thumbnail, text_box  # noqa: E402
from beamer2slides.script_space import SCRIPT_SPACE, SCRIPT_SPACE_EM  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "probe_script_space"
SLIDE_H = 405.0
THUMB_W = 1600
FACES = ["Lato", "PT Serif"]
SIZES = [11.0, 18.0, 28.0]
COUNTS = [0, 1, 2, 4]
REPEAT = 8
INSET = 7.2
BREAK_SIZE = 18.0
BREAK_W = 300.0
LEAD, BASE, SCRIPT_LETTER, AFTER = "aaaa aaaa D", "H", "o", "," + "b" * 60
Offset = Literal["SUBSCRIPT", "SUPERSCRIPT"]
OFFSETS: list[Offset] = ["SUBSCRIPT", "SUPERSCRIPT"]


@dataclass(frozen=True, kw_only=True)
class Row:
    """A row of units 'H' + script 'o' +`count` hair spaces (in the script run)."""
    face: str
    size: float
    offset: Offset
    count: int


@dataclass(frozen=True, kw_only=True)
class Wrap:
    """'aaaa aaaa D' + script 'k' (+ three hair spaces when `hair`) + ',bbbb...' in a BREAK_W box."""
    face: str
    offset: Offset
    hair: bool


def rows() -> list[Row]:
    return [Row(face=f, size=s, offset=o, count=n) for f in FACES for s in SIZES for o in OFFSETS for n in COUNTS]


def wraps() -> list[Wrap]:
    return [Wrap(face=f, offset=o, hair=h) for f in FACES for o in OFFSETS for h in (False, True)]


def row_text(r: Row) -> tuple[str, list[tuple[int, int]]]:
    """The row's text and the [start, end) of each script stretch (all BMP: UTF-16 = str index)."""
    unit_script = SCRIPT_LETTER + SCRIPT_SPACE * r.count
    text = ""
    ranges: list[tuple[int, int]] = []
    for _ in range(REPEAT):
        text += BASE
        ranges.append((len(text), len(text) + len(unit_script)))
        text += unit_script
    return text, ranges


def wrap_text(w: Wrap) -> tuple[str, tuple[int, int]]:
    script = "k" + (SCRIPT_SPACE * 3 if w.hair else "")
    return LEAD + script + AFTER, (len(LEAD), len(LEAD) + len(script))


# ------------------------------------------------------------------ layout


@dataclass(frozen=True, kw_only=True)
class Spot:
    page: int
    x: float
    y: float
    w: float
    h: float


def shelves(sizes: list[tuple[float, float]]) -> list[Spot]:
    """Boxes of these (w, h) on shelves, left to right and top to bottom, a new slide when full."""
    spots: list[Spot] = []
    page, x, y, shelf = 0, 5.0, 3.0, 0.0
    for w, h in sizes:
        if x + w > SLIDE_W - 5.0:
            x, y, shelf = 5.0, y + shelf + 2.0, 0.0
        if y + h > SLIDE_H - 3.0:
            page, x, y, shelf = page + 1, 5.0, 3.0, 0.0
        spots.append(Spot(page=page, x=x, y=y, w=w, h=h))
        x, shelf = x + w + 4.0, max(shelf, h)
    return spots


def row_box(r: Row) -> tuple[float, float]:
    return min(SLIDE_W - 10.0, REPEAT * (0.8 + 0.5 + 0.1 * r.count) * r.size + 2 * INSET), r.size * 2.0 + 2 * INSET


def wrap_box() -> tuple[float, float]:
    return BREAK_W, BREAK_SIZE * 4.0 + 2 * INSET


def styled(oid: str, text: str, face: str, size: float, italic: bool) -> list[SlidesRequest]:
    return [{"insertText": {"objectId": oid, "text": text}},
            {"updateTextStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                 "fields": "fontFamily,fontSize,bold,italic,foregroundColor",
                                 "style": {"fontFamily": face, "fontSize": pt(size), "bold": False, "italic": italic,
                                           "foregroundColor": {"opaqueColor": {"rgbColor": {}}}}}},
            {"updateParagraphStyle": {"objectId": oid, "textRange": {"type": "ALL"},
                                      "style": {"lineSpacing": 100, "spaceAbove": pt(0), "spaceBelow": pt(0)},
                                      "fields": "lineSpacing,spaceAbove,spaceBelow"}}]


def offset_request(oid: str, start: int, end: int, offset: Offset) -> SlidesRequest:
    return {"updateTextStyle": {"objectId": oid, "fields": "baselineOffset",
                                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end},
                                "style": {"baselineOffset": offset}}}


def italic_of(face: str) -> bool:
    return face != "Lato"  # (classify writes CM's math letters in PT Serif italic)


def page_ids(first: str, count: int) -> list[str]:
    return [first] + [f"scrspace_p{n}" for n in range(1, count)]


def all_requests(first: str, stale: list[str], rs: list[Row], ws: list[Wrap],
                 spots: list[Spot]) -> list[list[SlidesRequest]]:
    pages = page_ids(first, max(s.page for s in spots) + 1)
    batches: list[list[SlidesRequest]] = [[] for _ in pages]
    batches[0] += [{"deleteObject": {"objectId": e}} for e in stale]
    batches[0] += [{"createSlide": {"objectId": p}} for p in pages[1:]]
    for i, (r, s) in enumerate(zip(rs, spots)):
        oid, page = f"scrspace_r{i}", pages[s.page]
        text, ranges = row_text(r)
        batches[s.page] += [text_box(oid, page, s.x, s.y, s.w, s.h)] + styled(oid, text, r.face, r.size,
                                                                               italic_of(r.face))
        batches[s.page] += [offset_request(oid, a, b, r.offset) for a, b in ranges]
    for i, (w, s) in enumerate(zip(ws, spots[len(rs):])):
        oid, page = f"scrspace_w{i}", pages[s.page]
        text, (a, b) = wrap_text(w)
        batches[s.page] += [text_box(oid, page, s.x, s.y, s.w, s.h)] + styled(oid, text, w.face, BREAK_SIZE,
                                                                               italic_of(w.face))
        batches[s.page].append(offset_request(oid, a, b, w.offset))
    return batches


def check_requests(batches: list[list[SlidesRequest]], rs: list[Row], ws: list[Wrap], spots: list[Spot]) -> int:
    ids: list[str] = []
    for reqs in batches:
        for q in reqs:
            if "createShape" in q:
                ids.append(q["createShape"].get("objectId", ""))
            if "createSlide" in q:
                ids.append(q["createSlide"].get("objectId", ""))
    bad = [i for i in ids if not 5 <= len(i) <= 50]
    if bad or len(set(ids)) != len(ids):
        raise SystemExit(f"bad object ids: {bad or 'duplicates'}")
    for s in spots:
        if s.x < 0 or s.y < 0 or s.x + s.w > SLIDE_W or s.y + s.h > SLIDE_H:
            raise SystemExit(f"a box leaves the slide at {s}")
    for r in rs:
        text, ranges = row_text(r)
        if any(not 0 <= a < b <= len(text) for a, b in ranges):
            raise SystemExit(f"{r}: a script range outside its text")
    for w in ws:
        text, (a, b) = wrap_text(w)
        if not 0 <= a < b <= len(text):
            raise SystemExit(f"{w}: the script range outside its text")
    return sum(len(b) for b in batches)


# ------------------------------------------------------------------ reading


def period(img: np.ndarray, k: float, s: Spot) -> float | None:
    """The H's period in px (ink runs alternate H, script), None when the row does not read."""
    crop = img[int(s.y * k):int((s.y + s.h) * k), int(s.x * k):int((s.x + s.w) * k)]
    runs = column_runs((crop < 128).any(axis=0).tolist())
    if len(runs) != 2 * REPEAT:
        return None
    weight = (255.0 - crop).sum(axis=0)
    centres: list[float] = []
    for a, b in runs[0::2]:
        cols = np.arange(a, b + 1, dtype=np.float64)
        w = weight[a:b + 1]
        centres.append(float((cols * w).sum() / w.sum()))
    return (centres[-1] - centres[0]) / (REPEAT - 1)


def first_line_end(img: np.ndarray, k: float, s: Spot) -> float | None:
    """The right end (pt from the box's left) of the ink on the box's first line."""
    crop = img[int(s.y * k):int((s.y + s.h) * k), int(s.x * k):int((s.x + s.w) * k)]
    inked = (crop < 128).any(axis=1).tolist()
    top = next((i for i, v in enumerate(inked) if v), None)
    if top is None:
        return None
    band = crop[top:top + int(BREAK_SIZE * 0.9 * k)]
    cols = np.where((band < 128).any(axis=0))[0]
    return round(float(cols.max()) / k, 2) if cols.size else None


@dataclass(frozen=True, kw_only=True)
class Width:
    face: str
    size: float
    offset: Offset
    count: int
    em: float | None  # the hair spaces' width, per hair space, em of the run's size


@dataclass(frozen=True, kw_only=True)
class Break:
    face: str
    offset: Offset
    plain_end: float | None  # pt: the first line's ink end with no hair space
    hair_end: float | None   # and with three
    breaks: bool | None      # whether Slides broke the line at a hair space


def widths(rs: list[Row], periods: list[float | None], k: float) -> list[Width]:
    bare = {(r.face, r.size, r.offset): p for r, p in zip(rs, periods) if r.count == 0}
    out: list[Width] = []
    for r, p in zip(rs, periods):
        if r.count == 0:
            continue
        b = bare[(r.face, r.size, r.offset)]
        em = None if p is None or b is None else round((p - b) / (k * r.size * r.count), 4)
        out.append(Width(face=r.face, size=r.size, offset=r.offset, count=r.count, em=em))
    return out


def breaks(ws: list[Wrap], ends: list[float | None]) -> list[Break]:
    plain = {(w.face, w.offset): e for w, e in zip(ws, ends) if not w.hair}
    out: list[Break] = []
    for w, e in zip(ws, ends):
        if not w.hair:
            continue
        p = plain[(w.face, w.offset)]
        broke = None if p is None or e is None else e > p + 0.5 * BREAK_SIZE
        out.append(Break(face=w.face, offset=w.offset, plain_end=p, hair_end=e, breaks=broke))
    return out


def summary(ws: list[Width], bs: list[Break]) -> list[str]:
    expected = SCRIPT_SPACE_EM * SCRIPT_SIZE
    lines = [f"hair space in a script run, em of the run's size (emit assumes {expected:.4f} = "
             f"{SCRIPT_SPACE_EM} x {SCRIPT_SIZE:.4f})"]
    for face in FACES:
        for offset in OFFSETS:
            got = [w.em for w in ws if w.face == face and w.offset == offset and w.em is not None]
            mean = f"{sum(got) / len(got):.4f}" if got else "unread"
            spread = f" (spread {max(got) - min(got):.4f})" if got else ""
            lines.append(f"  {face:<9} {offset:<11} {mean}{spread}")
    lines.append("line breaks at a hair space after a script (should be none)")
    for b in bs:
        lines.append(f"  {b.face:<9} {b.offset:<11} {'unread' if b.breaks is None else 'BREAKS' if b.breaks else 'no'}"
                     f" (first line ends {b.plain_end} / {b.hair_end} pt)")
    return lines


def write_results(out: Path, ws: list[Width], bs: list[Break], lines: list[str]) -> None:
    (out / "result.json").write_text(json.dumps({
        "source": "tools/probe_script_space.py", "repeat": REPEAT, "widths": [asdict(w) for w in ws],
        "breaks": [asdict(b) for b in bs], "summary": lines}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", out / "result.json", "and", out / "summary.txt")


def measure(rs: list[Row], ws: list[Wrap], spots: list[Spot], paths: list[Path]) -> tuple[list[Width], list[Break]]:
    imgs = [gray(p) for p in paths]
    k = imgs[0].shape[1] / SLIDE_W
    periods = [period(imgs[s.page], k, s) for s in spots[:len(rs)]]
    ends = [first_line_end(imgs[s.page], k, s) for s in spots[len(rs):]]
    return widths(rs, periods, k), breaks(ws, ends)


# ------------------------------------------------------------------ dry run


def stand_in(rs: list[Row], ws: list[Wrap], spots: list[Spot], paths: list[Path]) -> None:
    """Each slide drawn in local faces, a script at SCRIPT_SIZE of its size (its hair spaces too),
    one character at a time at its advance; a line broken only at its word space."""
    k = THUMB_W / SLIDE_W
    pics = [Image.new("L", (THUMB_W, int(SLIDE_H * k)), 255) for _ in paths]
    for r, s in zip(rs, spots):
        draw = ImageDraw.Draw(pics[s.page])
        body, small = local_font(r.face, r.size * k), local_font(r.face, r.size * SCRIPT_SIZE * k)
        x, base = (s.x + INSET) * k, (s.y + INSET + r.size) * k
        text, ranges = row_text(r)
        for i, ch in enumerate(text):
            font = small if any(a <= i < b for a, b in ranges) else body
            draw.text((x, base), ch, font=font, fill=0, anchor="ls")
            x += font.getlength(ch)
    for w, s in zip(ws, spots[len(rs):]):
        draw = ImageDraw.Draw(pics[s.page])
        body = local_font(w.face, BREAK_SIZE * k)
        x, base = (s.x + INSET) * k, (s.y + INSET + BREAK_SIZE) * k
        draw.text((x, base), LEAD.rsplit(" ", 1)[0], font=body, fill=0, anchor="ls")
    for pic, path in zip(pics, paths):
        pic.save(path)


def dry_run() -> None:
    out = OUT / "dry-run"
    out.mkdir(parents=True, exist_ok=True)
    rs, ws = rows(), wraps()
    spots = shelves([row_box(r) for r in rs] + [wrap_box() for _ in ws])
    batches = all_requests("dry_first_page", ["dry_placeholder_title", "dry_placeholder_body"], rs, ws, spots)
    count = check_requests(batches, rs, ws, spots)
    (out / "requests.json").write_text(json.dumps([[slides_json(q) for q in b] for b in batches], ensure_ascii=False,
                                                  indent=1) + "\n", encoding="utf-8")
    print(f"{len(rs)} rows, {len(ws)} wraps, {count} requests on {len(batches)} slides -> {out / 'requests.json'}")
    if not all(Path(LOCAL[f]).exists() for f in FACES):
        print("  (a stand-in face is missing here: measurement not tried)")
        return
    paths = [out / f"slide-{n:02d}.png" for n in range(len(batches))]
    stand_in(rs, ws, spots, paths)
    width_reads, break_reads = measure(rs, ws, spots, paths)
    k = THUMB_W / SLIDE_W
    worst = 0.0
    for w in width_reads:
        if w.em is None:
            raise SystemExit(f"{w}: unread on the stand-in")
        hair = local_font(w.face, w.size * SCRIPT_SIZE * k).getlength(SCRIPT_SPACE) / (w.size * k)
        worst = max(worst, abs(w.em - hair))
    print(f"  stand-in widths against FreeType: worst |difference| {worst:.4f} em")
    if worst > 0.01 or any(b.breaks is not False for b in break_reads):
        raise SystemExit("the stand-in was misread")
    lines = summary(width_reads, break_reads)
    write_results(out, width_reads, break_reads, lines)
    for line in lines:
        print(line)


# ------------------------------------------------------------------ live


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rs, ws = rows(), wraps()
    spots = shelves([row_box(r) for r in rs] + [wrap_box() for _ in ws])
    slides = slides_service(None)
    pres = execute(slides.presentations().create(body={"title": "b2s probe script space"}))
    pid = presentation_id(pres)
    slide = pres.get("slides", [])[0]
    first = object_id(slide)
    batches = all_requests(first, [object_id(e) for e in slide.get("pageElements", [])], rs, ws, spots)
    pages = page_ids(first, len(batches))
    paths = [OUT / f"slide-{n:02d}.png" for n in range(len(batches))]
    try:
        for reqs in batches:
            execute(slides.presentations().batchUpdate(presentationId=pid, body={"requests": reqs}))
        for page, path in zip(pages, paths):
            save_thumbnail(slides, pid, page, path, None)
    finally:
        execute(drive_service(None).files().delete(fileId=pid))
    width_reads, break_reads = measure(rs, ws, spots, paths)
    lines = summary(width_reads, break_reads)
    write_results(OUT, width_reads, break_reads, lines)
    for line in lines:
        print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true", help="build and check the requests, read a stand-in; no Google")
    if parser.parse_args().dry_run:
        dry_run()
    else:
        main()
