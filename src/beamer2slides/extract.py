"""Stage 1: dump each PDF page's text spans, images, drawings and links (raw.json)."""

import math
import re
import unicodedata
from pathlib import Path

from .pdf import NO_OBJECT, Char, Document, Page, char_box

LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                           "ﬅ": "st", "ﬆ": "st"})


def _r(values, nd=2):
    return [round(float(v), nd) for v in values]


def _hex(rgb) -> str | None:
    if rgb is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb[:3])


def _points(item: tuple) -> list:
    op = item[0]
    if op == "re":
        x0, y0, x1, y1 = item[1]
        return [[x0, y0], [x1, y1]]
    if op == "qu":
        p0, p1, p2, p3 = item[1]  # the quad's corners in drawing order: ul, ll, lr, ur
        return [list(p) for p in (p0, p3, p2, p1)]  # ul, ur, lr, ll
    return [list(p) for p in item[1:]]


def _path(d: dict, max_items: int = 20) -> list | None:
    """Path geometry for small drawings (diagram nodes, lines, arrow tips): [op, [[x, y], ...]]."""
    if len(d["items"]) > max_items:
        return None
    return [[item[0], [[round(x, 2), round(y, 2)] for x, y in _points(item)]] for item in d["items"]]


def _rounded_corners(d: dict) -> dict[str, float]:
    """Which bbox corners of a path are drawn with a curve, and the curve's radius."""
    x0, y0, x1, y1 = d["rect"]
    corners: dict[str, float] = {}
    for item in d["items"]:
        if item[0] != "c":
            continue
        p1, p4 = item[1], item[4]
        mx, my = (p1[0] + p4[0]) / 2, (p1[1] + p4[1]) / 2
        key = ("t" if my < (y0 + y1) / 2 else "b") + ("l" if mx < (x0 + x1) / 2 else "r")
        corners[key] = round(max(abs(p4[0] - p1[0]), abs(p4[1] - p1[1])), 2)
    return corners


def _label(label: str | None, index: int) -> str:
    label = label or str(index + 1)
    if label.startswith("<FEFF") and label.endswith(">"):  # raw UTF-16BE hex string
        try:
            label = bytes.fromhex(label[5:-1]).decode("utf-16-be")
        except ValueError:
            pass
    return label


SMALL_CAPS_WIDTH = 0.03  # relative advance difference that marks an alternate glyph


def _small_caps(page: Page, chars: list[Char]) -> bool:
    """OpenType small caps (fontspec \\textsc): lowercase letters drawn with an alternate glyph.
    The text layer only says 'metropolis', the page shows METROPOLIS in small capitals. An
    alternate glyph has another advance than the font's default glyph for the letter."""
    lower = [ch for ch in chars if not ch.synthetic and len(ch.c) == 1 and ch.c.islower() and ch.exact_advance]
    if len(lower) < 2:
        return False
    alternate = 0
    for ch, default in zip(lower, page.glyph_widths([(ch.font_id, ch.c, ch.size) for ch in lower])):
        if default and abs(ch.advance - default) > SMALL_CAPS_WIDTH * max(default, 0.01):
            alternate += 1
    return alternate >= 2 and alternate >= 0.7 * len(lower)


# Gaps between glyphs on a line, in ems of the following glyph. TeX output has no space
# characters: word spaces are pen moves. A small move (thin math spaces) becomes a space inside
# the span; a word space ends the span (classify groups words by geometry).
JOIN_GAP = 0.15
WORD_GAP = 0.3
NEW_LINE_GAP = 1.0
BACK_GAP = -0.6
SAME_BASELINE = 0.05
NEW_BASELINE = 0.8


def combining_mark(c: str) -> bool:
    """The character is nothing but combining marks (a macron, an acute): no width of its own."""
    return bool(c) and all(unicodedata.combining(u) for u in c)


def spans(page: Page) -> list[dict]:
    """Runs of glyphs on one line with the same font, size and colour, split at word gaps."""
    out = []
    run: list[Char] = []
    x0, y0, x1, y1 = page.rect

    def flush():
        if run and any(not ch.synthetic for ch in run):
            text = "".join(ch.c for ch in run)
            # A combining mark sits over the letter before it; its own box (PDFium gives it one,
            # advance included) would stretch the span over the space that follows.
            boxed = [ch for ch in run if not combining_mark(ch.c)] or run
            bx0 = min(ch.box[0] for ch in boxed)
            by0 = min(ch.box[1] for ch in boxed)
            bx1 = max(ch.box[2] for ch in boxed)
            by1 = max(ch.box[3] for ch in boxed)
            first = run[0]
            out.append({"text": text, "font": first.font, "size": first.size, "color": first.color,
                        "alpha": first.alpha, "origin": first.origin, "bbox": (bx0, by0, bx1, by1),
                        "dir": first.dir, "chars": list(run)})
        run.clear()

    prev: Char | None = None
    for ch in page.chars():
        # characters outside the page (e.g. the cut-off half of a notes-on-second-screen page)
        if ch.box[2] <= x0 or ch.box[0] >= x1 or ch.box[3] <= y0 or ch.box[1] >= y1:
            continue
        space = None
        if prev is not None:
            ux, uy = ch.dir
            px, py = prev.origin[0] + prev.dir[0] * prev.advance, prev.origin[1] + prev.dir[1] * prev.advance
            size = max(ch.size, 0.01)
            gap = ((ch.origin[0] - px) * ux + (ch.origin[1] - py) * uy) / size
            offset = abs((ch.origin[0] - px) * uy - (ch.origin[1] - py) * ux) / size
            style = (ch.font, round(ch.size, 3), ch.color, ch.alpha) != (prev.font, round(prev.size, 3), prev.color, prev.alpha)
            new_line = ch.dir != prev.dir or offset > NEW_BASELINE or gap > NEW_LINE_GAP or gap < BACK_GAP
            if new_line or gap >= WORD_GAP:
                flush()
            elif gap >= JOIN_GAP and offset < SAME_BASELINE and prev.c != " " and ch.c != " ":
                if style:
                    flush()
                width = gap * size
                space = Char(" ", ch.font, ch.size, ch.color, ch.alpha, (px, py),
                             char_box(px, py, ux, uy, width, ch.size, ch.ascent, ch.descent),
                             ch.dir, NO_OBJECT, ch.font_id, width, True, ch.ascent, ch.descent)
            elif style:
                flush()
        if space:
            run.append(space)
        run.append(ch)
        # A combining mark is drawn over the letter before it, so the pen stays where that letter
        # left it: lualatex sets \={x} as x plus U+0304, and PDFium gives the mark an advance of
        # its own. Counted, it eats the word space that follows ("x̄and").
        prev = prev if prev is not None and combining_mark(ch.c) else ch
    flush()
    return out


def _shadow_pieces(drawings: list[dict]) -> list[tuple]:
    """Beamer's block shadows: a black rectangle under a soft mask whose shadings fade its
    edges, offset right and down from the panel painted over it. The mask contents are not
    page objects, so the visible parts of the shadow (right of and below the panel) are
    reported as shading pieces on whole points, as the shadings themselves would be."""
    pieces = []
    for i, m in enumerate(drawings):
        if not m.get("soft_mask") or m["type"] != "f":
            continue
        mx0, my0, mx1, my1 = m["rect"]
        for p in drawings[i + 1:]:
            if p["type"] not in ("f", "fs") or p.get("soft_mask") or (p.get("fill_opacity") or 1.0) < 1.0:
                continue
            px0, py0, px1, py1 = p["rect"]
            dx, dy = mx1 - px1, my1 - py1
            if 0.5 <= dx <= 8 and abs(dx - dy) <= 0.25 and abs(mx0 - px0 - dx) <= 0.25 and my0 < py1 - dy:
                pieces.append((math.floor(px1), math.floor(my0), math.ceil(mx1), math.ceil(my1)))
                pieces.append((math.floor(mx0), math.floor(py1), math.ceil(mx1), math.ceil(my1)))
                break
    return pieces


def extract_page(page: Page, label: str) -> dict:
    n = page.index
    out_spans = []
    for s in spans(page):
        if not s["text"].strip():
            continue
        out_spans.append({
            # Ligature code points (xelatex/lualatex text layers) as plain letters, so the
            # text stays searchable and spell-checkable in Slides.
            "id": f"p{n}s{len(out_spans)}", "text": s["text"].translate(LIGATURES), "font": s["font"],
            "size": round(s["size"], 3), "color": f"#{s['color']:06x}", "alpha": s["alpha"],
            "origin": _r(s["origin"]), "bbox": _r(s["bbox"]), "dir": _r(s["dir"], 3),
            "smallcaps": _small_caps(page, s["chars"]),
        })

    page_drawings = page.drawings()
    found = [(info["bbox"], [info["width"], info["height"]]) for info in page.images()]
    found += [(b, [b[2] - b[0], b[3] - b[1]]) for b in _shadow_pieces(page_drawings)]
    images = [{"id": f"p{n}i{i}", "bbox": _r(b), "px": px} for i, (b, px) in enumerate(found)]

    drawings = [{
        "id": f"p{n}d{i}", "type": d["type"], "items": "".join(item[0] for item in d["items"]),
        "bbox": _r(d["rect"]), "fill": _hex(d.get("fill")), "stroke": _hex(d.get("color")),
        "width": round(d["width"], 2) if d.get("width") else None,
        "fill_opacity": round(d.get("fill_opacity") or 1.0, 3),
        "corners": _rounded_corners(d),
        "path": _path(d),
    } for i, d in enumerate(page_drawings)]

    links = [{"bbox": _r(link["bbox"]), **({"uri": link["uri"]} if "uri" in link else {"page": link["page"]})}
             for link in page.links()]

    # Anything entirely outside the page (e.g. the cut-off half of a notes-on-second-screen page).
    x0, y0, x1, y1 = page.rect
    inside = lambda b: b[2] > x0 and b[0] < x1 and b[3] > y0 and b[1] < y1
    out_spans = [s for s in out_spans if inside(s["bbox"])]
    images = [i for i in images if inside(i["bbox"])]
    drawings = [d for d in drawings if inside(d["bbox"])]
    links = [l for l in links if inside(l["bbox"])]

    return {
        "index": n, "label": label,
        "size": _r((page.width, page.height)),
        "spans": out_spans, "images": images, "drawings": drawings, "links": links,
    }


def select_overlays(raw: dict, mode: str) -> dict:
    """Beamer gives every overlay step of a frame its own page, all with the frame number
    as page label. mode 'last' keeps only the final (complete) step of each frame; 'all'
    keeps every page. Handout PDFs have one page per label, so both are the same there."""
    if mode == "all":
        return raw
    pages = raw["pages"]

    def heading(p: dict) -> str:  # text in the top fifth of the page: the frame title
        return " ".join(s["text"].strip() for s in sorted(p["spans"], key=lambda s: s["bbox"][0])
                        if s["bbox"][3] < 0.2 * p["size"][1])

    def words(p: dict) -> list[str]:
        return [w for s in p["spans"] for w in s["text"].split()]

    def same_frame(a: dict, b: dict) -> bool:
        """Overlay steps share the frame number, the heading and most of their text (a later
        step shows what the earlier one did). Themes that don't count some frames (title and
        section pages) share numbers too, but not their text."""
        if a["label"] != b["label"] or heading(a) != heading(b):
            return False
        wa, wb = words(a), set(words(b))
        # \only<n> swaps some text between steps, so require a majority, not everything.
        return not wa or sum(w in wb for w in wa) >= 0.5 * len(wa)

    kept = [p for i, p in enumerate(pages) if i + 1 == len(pages) or not same_frame(p, pages[i + 1])]
    return {**raw, "pages": kept, "overlays": {"mode": mode, "dropped": len(pages) - len(kept)}}


FRAME_STEP = re.compile(r"(.+)<(\d+)>")


def frame_labels(dests: list[tuple[str, int]]) -> dict[int, str]:
    """Page index -> beamer frame label. `\\begin{frame}[label=x]` puts the destination x on the
    frame's first page and x<n> on its n-th overlay step; hyperref's own destinations (page.3,
    Navigation3) have no steps."""
    names = {name for name, _ in dests}
    out = {}
    for name, page in dests:
        m = FRAME_STEP.fullmatch(name)
        if m and m.group(1) in names and page >= 0:
            out.setdefault(page, m.group(1))
    return out


def extract(pdf: Path, labels: list[str] | None = None) -> dict:
    """`labels` replaces the PDF's page labels (notes.prepare deletes pages, and PDFium can't
    rewrite the label tree)."""
    doc = Document(pdf)
    try:
        meta = doc.metadata
        frames = frame_labels(doc.named_dests())
        pages = [extract_page(page, _label(labels[page.index] if labels else doc.label(page.index), page.index))
                 for page in doc]
        for page in pages:
            page["frame_label"] = frames.get(page["index"])
        return {
            "version": 1,
            "source": {"pdf": str(pdf), "producer": meta["producer"], "pages": len(doc), "title": meta["title"]},
            "pages": pages,
        }
    finally:
        doc.close()
