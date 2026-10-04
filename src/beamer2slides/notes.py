"""Beamer speaker notes: find them in the PDF, take them out of the slides (docs/speaker-notes.md).

Two beamer modes are read:
  \\setbeameroption{show notes}                      a note page after each slide that has a note
  \\setbeameroption{show notes on second screen=right} double-width pages, the note on the right
with the note page templates beamer ships: `default` (a grey header band with the frame's title,
the date and a quarter-size thumbnail of the frame), `compressed` (a narrow band, an eighth-size
thumbnail) and `plain` (the note alone, nothing drawn: told apart by the page labels).

`prepare` writes a notes-free slides.pdf and returns the note text per slide page: one string
per slide, paragraphs and list items one per line ("• " or the item's number in front), words
joined as TeX set them, ligatures, accents, quotes and inline math as Unicode, a link's address
after its words, and every word of a note that runs past the bottom of its page. A frame's
overlay steps each get their note page; the last step of the frame (the one `convert` keeps)
carries every step's note (`carried`).

A third way needs nothing but the PDF one presents from: a deck using our b2snotes.sty
(`notes-package` writes it) has its notes hidden, each page carrying its slide's notes beside it,
off the page (`carried_notes`). Its pages are the slides, untouched.

A PDF compiled without its notes gets them from its source: `notes_from_source` compiles the
.tex the person names once more with notes shown and pairs those note pages with the PDF's pages.
"""

from __future__ import annotations

import functools
import re
import shutil
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Literal

from .classify_text import BULLET_GLYPHS, ENUM_RE, RAISED_MARKS, compose_accents, math_text
from .extract import (JOIN_GAP, PageSpan, Reading, _label, extract, extract_read, page_chars, page_marks, readable,
                      shown_spans)
from .fonts import font_info
from .pdf import Char, Document, Drawing, Page, PdfDocument
from .raw_types import RawDoc

Box = tuple[float, float, float, float]
NotesMode = Literal["note pages", "second screen", "carried"]


def _contains(outer: Box, inner: Box) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _spans_in(spans: list[PageSpan], area: Box) -> list[PageSpan]:
    return [s for s in spans if s.text.strip() and
            area[0] <= (s.bbox[0] + s.bbox[2]) / 2 <= area[2] and area[1] <= (s.bbox[1] + s.bbox[3]) / 2 <= area[3]]


# The note page's header band, as a share of the page's height: beamer's `default` template draws
# it a quarter of the page high, `compressed` an eighth (with an eighth-size thumbnail).
HEADER_BAND = (0.15, 0.35)
COMPRESSED_BAND = (0.10, 0.15)


def _note_header(page: Page, spans: list[PageSpan], area: Box) -> float | None:
    """Bottom of the note page header band inside `area`, or None if this is no note page."""
    return _header_in(page.drawings(), lambda: spans, area)


def _header_in(drawings: list[Drawing], read_spans: Callable[[], list[PageSpan]], area: Box) -> float | None:
    """`_note_header` over the page's `drawings`. The words (`read_spans`, the page's
    `shown_spans`) are read only when a band could be the default template's: most pages have
    none, and reading a page's words was most of what finding its notes cost."""
    x0, y0, x1, y1 = area
    width, height = x1 - x0, y1 - y0
    read: list[list[PageSpan]] = []
    for d in drawings:
        r = d["rect"]
        if d.get("fill") is None or abs(r[0] - x0) > 1 or abs(r[2] - x1) > 1 or r[1] > y0 + 1:
            continue
        band = (r[3] - y0) / height
        if COMPRESSED_BAND[0] < band <= COMPRESSED_BAND[1]:
            # (a narrow band with small words at its right end is also a theme's headline: only
            # the thumbnail's own canvas says it is the compressed note page)
            if _thumbnail_canvas(drawings, area, r[3], 0.125):
                return r[3]
            continue
        if not HEADER_BAND[0] < band < HEADER_BAND[1]:
            continue
        header = (x0 + 0.6 * width, y0, x1, r[3])
        if not read:
            read.append(read_spans())
        inside = _spans_in(read[0], area)
        # Text size of the note itself: the frame thumbnail can hold more words than a short note.
        body = [s for s in inside if not _contains(header, s.bbox)] or inside
        sizes = sorted(s.size for s in body)
        if not sizes:
            return None
        tiny = [s for s in inside if s.size < 0.45 * sizes[len(sizes) // 2] and _contains(header, s.bbox)]
        if len(tiny) >= 1 or _thumbnail_canvas(drawings, area, r[3], 0.25):  # the frame thumbnail
            return r[3]
    return None


def _thumbnail_canvas(drawings: list[Drawing], area: Box, header_bottom: float, scale: float) -> bool:
    """The note template's frame thumbnail painted empty: `\\insertslideintonotes{0.25}` fills a
    quarter-size canvas at the header's right end (`compressed`: an eighth) and draws the frame's
    own box into it - which holds nothing when a frame's content is placed at shipout (textpos'
    absolute blocks: every frame `adopt` writes). Its words never reach the thumbnail, so only the
    canvas says so."""
    x0, y0, x1, y1 = area
    w, h = scale * (x1 - x0), scale * (y1 - y0)
    for d in drawings:
        r = d["rect"]
        if d.get("fill") is not None and abs(r[2] - r[0] - w) <= 1 and abs(r[3] - r[1] - h) <= 1 and \
                r[2] >= x1 - 0.1 * w and r[1] >= y0 - 1 and r[3] <= header_bottom + 1:
            return True
    return False


# --- reading a note ---------------------------------------------------------------------------------

# Unicode's superscript and subscript forms; a script with a character outside them is written
# ^(...) / _(...) (one character: ^x).
SUPERSCRIPTS: dict[str, str] = dict(zip("0123456789+-−=()aábcdeéfghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVWαβγδεθιφχ",
                        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ᵃᵃᵇᶜᵈᵉᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂᵅᵝᵞᵟᵋᶿᶥᵠᵡ"))
SUBSCRIPTS: dict[str, str] = dict(zip("0123456789+-−=()aehijklmnoprstuvxβγρφχ",
                      "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ"))
PRIMES = set("′″‴'")
Script = Literal["base", "super", "sub"]
SCRIPT_SIZE = 0.85    # a script is smaller than its line's text (classify_text.script_of)
SUPER_SHIFT = 0.12    # em above the baseline
SUB_SHIFT = 0.10      # em below
NEW_LINE_DROP = 0.9   # em down from the glyph before: another line (a script moves half that)
NEW_LINE_BACK = 2.0   # em back from the glyph before: another line (an OT1 accent backs up < 1)
LABEL_INDENT = 0.5    # em: a list item's label stands in from the note's left edge
# Two \note commands on one frame are set one after the other with nothing between them: "...on
# every step.Only on the second step." A sentence's end and a capitalised word, unspaced.
RUN_ON = re.compile(r"(?<=[a-z][.!?])(?=[A-Z][a-z])")


def _family(font: str) -> str:
    return font_info(font).family


@dataclass(frozen=True, kw_only=True)
class NoteLine:
    """A line of a note as read: where it starts and ends, its baseline and text size, the item
    label it starts with (None: none), where its words start after that label, the width of its
    first word, and its text."""
    x0: float
    x1: float
    baseline: float
    size: float
    label: str | None
    text_x: float
    first_word: float
    text: str


def note_glyphs(chars: list[Char], area: Box, top: float) -> list[Char]:
    """The glyphs of the note in `area` below `top`, in the order they are drawn: upright ones
    (the default template's date stands turned at the page's left edge), seen or not - a note runs
    off the bottom of its page rather than onto another, and those words are its words too."""
    return [c for c in chars if c.dir[0] > 0.99 and not c.synthetic and area[0] - 1 <= c.origin[0] <= area[2] + 1
            and c.origin[1] > top and c.c]


def split_lines(glyphs: list[Char]) -> list[list[Char]]:
    """The glyphs line by line: a line ends where the next glyph goes down a line or back to the
    left edge (TeX draws a paragraph line after line, each from its start)."""
    lines: list[list[Char]] = []
    for c in glyphs:
        if lines:
            prev = lines[-1][-1]
            em = max(c.size, prev.size, 1.0)
            if c.origin[1] - prev.origin[1] < NEW_LINE_DROP * em and prev.origin[0] - c.origin[0] < NEW_LINE_BACK * em:
                lines[-1].append(c)
                continue
        lines.append([c])
    return lines


def superscript(text: str) -> str:
    if all(c in RAISED_MARKS or c in PRIMES for c in text):
        return "".join(RAISED_MARKS.get(c, c) for c in text)
    if all(c in SUPERSCRIPTS for c in text):
        return "".join(SUPERSCRIPTS[c] for c in text)
    return "^" + text if len(text) == 1 else f"^({text})"


def subscript(text: str) -> str:
    if all(c in SUBSCRIPTS for c in text):
        return "".join(SUBSCRIPTS[c] for c in text)
    return "_" + text if len(text) == 1 else f"_({text})"


def glyph_text(chars: list[Char]) -> str:
    """The text of glyphs of one font and script: a math font's as Unicode
    (`classify_text.math_text`), a text font's with its ligatures as letters and OT1's accents
    composed with their letters."""
    font = chars[0].font
    raw = "".join(c.c for c in chars).replace("\x02", "-")
    family = _family(font)
    if family == "math":
        return math_text(font, raw)[0]
    return compose_accents(readable(raw, font), family == "mono")


def _scripts(line: list[Char]) -> tuple[float, float, list[Script]]:
    """The line's text size and baseline (those of most of its glyphs at the largest size), and
    each glyph's script."""
    size = max(c.size for c in line)
    sizes = Counter(round(c.size, 1) for c in line if c.size >= SCRIPT_SIZE * size)
    body = sizes.most_common(1)[0][0]
    bases = Counter(round(c.origin[1], 1) for c in line if c.size >= SCRIPT_SIZE * body)
    baseline = bases.most_common(1)[0][0]
    out: list[Script] = []
    for c in line:
        shift = (c.origin[1] - baseline) / body
        if c.size >= SCRIPT_SIZE * body or -SUPER_SHIFT <= shift <= SUB_SHIFT:
            out.append("base")
        else:
            out.append("super" if shift < 0 else "sub")
    return body, baseline, out


def read_line(line: list[Char], links: dict[int, str]) -> NoteLine:
    """One line of a note as text: glyphs of one font and script are read together
    (`glyph_text`), a gap of `extract.JOIN_GAP` em or more is a space (a style change inside a
    word is none), scripts are Unicode super- or subscripts, and a link's address follows its
    last word when its words do not say it (`links`: glyph index -> address)."""
    body, baseline, scripts = _scripts(line)
    pieces: list[str] = []
    run: list[Char] = []
    run_script: Script = "base"
    first_word = None
    label: str | None = None
    text_x = line[0].box[0]

    def flush() -> None:
        if run:
            text = glyph_text(run)
            match run_script:
                case "base":
                    pieces.append(text)
                case "super":
                    pieces.append(superscript(text))
                case "sub":
                    pieces.append(subscript(text))
        run.clear()

    for k, c in enumerate(line):
        if k:
            prev = line[k - 1]
            # (in ems of the larger glyph: a script stands a point or so off its letter, which in
            # the script's own ems would be a space)
            gap = (c.origin[0] - (prev.origin[0] + prev.advance)) / max(prev.size, c.size, 1.0)
            spaced = gap >= JOIN_GAP or prev.c.isspace()
            if spaced or c.font != prev.font or scripts[k] != scripts[k - 1] or c.c.isspace():
                flush()
            uri = links.get(id(prev))
            if uri is not None and links.get(id(c)) != uri:
                flush()
                pieces.append(f" ({uri})")
            if spaced:
                if first_word is None:
                    first_word = prev.box[2] - line[0].box[0]
                    word = "".join(pieces).strip()
                    if word in BULLET_GLYPHS or ENUM_RE.match(word):
                        label, text_x = word, c.box[0]
                pieces.append(" ")
        if not c.c.isspace():
            run.append(c)
            run_script = scripts[k]
    flush()
    uri = links.get(id(line[-1]))
    if uri is not None:
        pieces.append(f" ({uri})")
    text = re.sub(r" {2,}", " ", "".join(pieces)).strip()
    return NoteLine(x0=line[0].box[0], x1=max(c.box[2] for c in line), baseline=baseline, size=body, label=label,
                    text_x=text_x, first_word=first_word if first_word is not None else line[-1].box[2] - line[0].box[0],
                    text=text)


def uri_links(page: Page) -> list[tuple[Box, str]]:
    """The page's web links: where each lies and its address."""
    out: list[tuple[Box, str]] = []
    for link in page.links():
        uri = link.get("uri")
        if isinstance(uri, str):
            out.append((link["bbox"], uri))
    return out


def links_in(links: list[tuple[Box, str]], area: Box) -> list[tuple[Box, str]]:
    """The `links` within `area`'s columns."""
    return [(box, uri) for box, uri in links if box[0] >= area[0] - 1 and box[2] <= area[2] + 1]


def web_links(page: Page, area: Box, chars: list[Char]) -> list[tuple[Box, str]]:
    """The page's web links over `area`. On a second screen's right half (`area` not starting at
    the page's left edge) pgfpages leaves a note's links where the note page had them, over the
    slide's half: such a link, over none of the slide's own glyphs, is moved onto the note."""
    links = uri_links(page)
    out = links_in(links, area)
    shift = area[0]
    for (x0, y0, x1, y1), uri in links:
        if shift > 0 and x1 <= shift + 1 and x0 < area[0] - 1 and not any(
                x0 <= (c.box[0] + c.box[2]) / 2 <= x1 and y0 <= (c.box[1] + c.box[3]) / 2 <= y1 for c in chars):
            out.append(((x0 + shift, y0, x1 + shift, y1), uri))
    return out


def link_marks(glyphs: list[Char], uris: list[tuple[Box, str]]) -> dict[int, str]:
    """Glyph (by id) -> the web address of the link over it (`uris`: `web_links`), for the links
    whose words are not their address already (\\url's are; \\href's words are not)."""
    if not uris:
        return {}
    over: dict[int, str] = {}
    words: dict[str, str] = {}
    for c in glyphs:
        cx, cy = (c.box[0] + c.box[2]) / 2, (c.box[1] + c.box[3]) / 2
        for (x0, y0, x1, y1), uri in uris:
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                over[id(c)] = uri
                words[uri] = words.get(uri, "") + c.c
                break

    def bare(text: str) -> str:
        return re.sub(r"^(https?://|mailto:)|[/.,;:]+$", "", text.strip().lower().replace(" ", ""))
    said = {uri for uri, text in words.items() if bare(text) == bare(uri)}
    return {k: uri for k, uri in over.items() if uri not in said}


def _qualifies(line: NoteLine, lines: list[NoteLine], left: float) -> bool:
    """A line starting with a bullet glyph or a number is a list item when it stands in from the
    note's left edge, or when another line starts with a label at the same place (a note that is
    all items: `\\note[item]`). A bullet glyph alone is enough, but a dash starting a sentence is
    not."""
    if line.label is None:
        return False
    if line.label in BULLET_GLYPHS and line.label not in "–∗":
        return True
    return line.x0 > left + LABEL_INDENT * line.size or \
        any(o is not line and o.label is not None and abs(o.x0 - line.x0) < 1.0 for o in lines)


@dataclass(frozen=True, kw_only=True)
class _Paragraph:
    lines: list[NoteLine]
    item: bool
    level: int


def paragraphs(lines: list[NoteLine]) -> list[_Paragraph]:
    """The note's lines as paragraphs. A line starts a paragraph when it is a list item
    (`_qualifies`), when it steps out of the list it follows, when the line before ended short
    enough to have taken its first word (TeX would have: a paragraph's last line), or when a
    blank line's room lies above it. Items nest by where their labels stand."""
    if not lines:
        return []
    left = min(l.x0 for l in lines)
    right = max(l.x1 for l in lines)
    pitches = sorted(b.baseline - a.baseline for a, b in zip(lines, lines[1:]) if b.baseline > a.baseline)
    pitch = pitches[0] if pitches else 0.0
    out: list[_Paragraph] = []
    open_items: list[float] = []  # text_x of the items a line may still continue or nest in
    for line in lines:
        item = _qualifies(line, lines, left)
        prev = out[-1] if out else None
        last = prev.lines[-1] if prev else None
        new = prev is None or last is None or item
        if not new and prev is not None and last is not None:
            room = right - last.x1
            stepped_out = prev.item and line.x0 < last.text_x - LABEL_INDENT * line.size and \
                line.x0 < prev.lines[0].text_x - LABEL_INDENT * line.size
            new = stepped_out or room > line.first_word + 0.5 * line.size or \
                (pitch > 0 and line.baseline - last.baseline > 1.5 * pitch)
        if item:
            while open_items and open_items[-1] > line.x0 + 1.0:
                open_items.pop()
            out.append(_Paragraph(lines=[line], item=True, level=len(open_items)))
            open_items.append(line.text_x)
        elif new:
            while open_items and open_items[-1] > line.x0 + 1.0:
                open_items.pop()
            out.append(_Paragraph(lines=[line], item=False, level=len(open_items)))
        elif prev is not None:
            out[-1] = replace(prev, lines=prev.lines + [line])
    return out


def _joined(lines: list[NoteLine]) -> str:
    text = ""
    for line in lines:
        if not text:
            text = line.text
        elif text.endswith("-") and line.text[:1].islower():  # a word TeX hyphenated
            text = text[:-1] + line.text
        else:
            text += " " + line.text
    return text


def note_text(lines: list[NoteLine]) -> str:
    """The note as one string: a line per paragraph or list item, an item's label and a space in
    front of its words, a nested item indented two spaces per level."""
    out: list[str] = []
    for p in paragraphs(lines):
        text = _joined(p.lines)
        head = p.lines[0]
        if p.item and head.label is not None:
            words = text[len(head.label):].strip()
            text = f"{head.label} {words}"
        out.append("  " * p.level + RUN_ON.sub(" ", text))
    return unicodedata.normalize("NFC", "\n".join(l.rstrip() for l in out if l.strip()))


def read_note(page: Page, area: Box, top: float) -> str:
    """The note on `page` within `area`'s columns below `top` (its header), as one string."""
    chars, _ = page_chars(page)
    glyphs = note_glyphs(chars, area, top)
    if not glyphs:
        return ""
    links = link_marks(glyphs, web_links(page, area, chars))
    return note_text([read_line(line, links) for line in split_lines(glyphs) if any(not c.c.isspace() for c in line)])


# --- notes carried beside the page (b2snotes.sty) ------------------------------------------------------

# The first line of each block b2snotes.sty sets beside a page (its \bsnotes@markhere and
# \bsnotes@markbefore): this page's notes, and those of a \note written after the frame before.
CARRIED = "beamer2slides notes"
CARRIED_BEFORE = "beamer2slides notes before"
CARRIED_FROM = 1.5   # page widths: the blocks stand two page widths right of the page's left edge
PACKAGE = "b2snotes.sty"


@dataclass(frozen=True, kw_only=True)
class CarriedNotes:
    """The notes a page carries (b2snotes.sty): its own, and those it carries for the page before
    ("" for none)."""
    here: str
    before: str


def carried_notes(page: Page) -> CarriedNotes:
    """The b2snotes blocks beside `page` (`carried_blocks`)."""
    return carried_blocks(page_chars(page)[0], page.width, uri_links(page))


def carried_blocks(chars: list[Char], width: float, uris: list[tuple[Box, str]]) -> CarriedNotes:
    """The b2snotes blocks among a page's glyphs (`width`: the page's), read as a note page's note
    is. A block is the lines below its first line, in that line's column (one page wide): beamer's
    hidden overlay steps are also drawn far off the page, but elsewhere."""
    off = [c for c in chars if c.dir[0] > 0.99 and not c.synthetic and c.c and c.origin[0] >= CARRIED_FROM * width]
    lines = [line for line in split_lines(off) if any(not c.c.isspace() for c in line)]
    heads = [line for line in lines if read_line(line, {}).text in (CARRIED, CARRIED_BEFORE)]
    if not heads:
        return CarriedNotes(here="", before="")
    left = min(line[0].box[0] for line in heads)
    area: Box = (left, min(line[0].box[1] for line in heads), left + width, float("inf"))
    column = [line for line in lines if area[0] - 1 <= line[0].box[0] <= area[2] and line[0].box[1] >= area[1]]
    links = link_marks([c for line in column for c in line], links_in(uris, area))
    blocks: dict[str, list[NoteLine]] = {CARRIED: [], CARRIED_BEFORE: []}
    into: list[NoteLine] | None = None
    for line in column:
        read = read_line(line, links)
        if any(line is head for head in heads):
            into = blocks[read.text]
        elif into is not None:
            into.append(read)
    return CarriedNotes(here=note_text(blocks[CARRIED]), before=note_text(blocks[CARRIED_BEFORE]))


def write_package(folder: Path) -> Path:
    """b2snotes.sty written into `folder` (made when missing): what a preamble's
    `\\usepackage{b2snotes}` finds beside the .tex."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / PACKAGE
    path.write_bytes((resources.files("beamer2slides") / "tex" / PACKAGE).read_bytes())
    return path


# --- which pages are notes ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class Prepared:
    """What `prepare` found: the slides alone, the note text by page of that PDF, how beamer
    showed the notes, the page labels of the slides when the PDF's own no longer say them, and the
    page of the given PDF each page of `pdf` is (`kept`)."""
    pdf: Path                    # the slides without note pages (the given PDF when it had none)
    notes: dict[int, str]        # note text by page of `pdf`
    mode: NotesMode | None
    labels: list[str] | None     # page labels of `pdf` when its own are stale (pages were deleted)
    kept: list[int]


def prepare(pdf: Path, out: Path) -> Prepared:
    with Reading(pdf) as reading:
        return prepare_read(reading, out)


def prepare_read(reading: Reading, out: Path) -> Prepared:
    """`prepare` of the PDF `reading` holds open: when its `pdf` is the one read (no note pages),
    `extract.extract_read` reads it on from where this left it."""
    return _prepare(reading, reading.pdf, out)


def read_with_notes(pdf: Path, out: Path) -> tuple[Prepared, RawDoc]:
    """`prepare` then `extract` of what it prepared, each page carrying its notes: the PDF opened
    once when it has no note pages (`Reading`)."""
    with Reading(pdf) as reading:
        prepared = prepare_read(reading, out)
        raw = extract_read(reading, prepared.labels) if prepared.pdf == pdf else extract(prepared.pdf, prepared.labels)
    for page in raw["pages"]:
        page["notes"] = prepared.notes.get(page["index"])
    return prepared, raw


def _frame_view(shown: list[PageSpan], page: Page, area: Box) -> tuple[str, list[str]]:
    """A slide's title (its largest words in the top fifth) and its words in `area`, as
    `extract.select_overlays` compares overlay steps (`shown`: the page's `shown_spans`)."""
    spans = [s for s in shown if area[0] - 1 <= s.bbox[0] and s.bbox[2] <= area[2] + 1]
    band = [s for s in spans if s.bbox[3] < 0.2 * page.height and s.text.strip()]
    title = ""
    if band:
        big = max(s.size for s in band)
        title = " ".join(s.text.strip() for s in sorted(band, key=lambda s: (round(s.origin[1]), s.bbox[0]))
                         if s.size >= 0.9 * big)
    return title, [w for s in spans for w in s.text.split()]


def same_frame(a: tuple[str, list[str]], b: tuple[str, list[str]]) -> bool:
    """`extract.select_overlays`' test for two steps of one frame, on their (title, words)."""
    (ta, wa), (tb, wb) = a, b
    if not wa:
        return ta == tb
    have = set(wb)
    share = sum(w in have for w in wa) / len(wa)
    return share >= 0.8 or (ta == tb and share >= 0.3)


def union(a: str, b: str) -> str:
    """Two steps' notes as the frame's: a step's note that holds the other's is both."""
    if a in b:
        return b
    if b in a:
        return a
    return a + "\n" + b


def carried(notes: dict[int, str], labels: list[str], views: dict[int, tuple[str, list[str]]]) -> dict[int, str]:
    """Each frame's last overlay step carries the notes of all its steps (`\\note<2>{...}` is on
    the second step's note page only, and `convert` keeps the last step). Steps are pages next to
    each other with one label and the words `same_frame` asks for (`views`: page -> title and
    words, for the pages in question)."""
    out = dict(notes)
    for k in range(len(labels) - 1):
        if k in out and labels[k] == labels[k + 1] and k in views and k + 1 in views \
                and same_frame(views[k], views[k + 1]):
            out[k + 1] = union(out[k], out[k + 1]) if k + 1 in out else out[k]
    return out


def _steps_in_question(notes: dict[int, str], labels: list[str]) -> set[int]:
    """Pages a note may have to be carried along: in a run of one label holding a note."""
    out: set[int] = set()
    start = 0
    for k in range(1, len(labels) + 1):
        if k == len(labels) or labels[k] != labels[start]:
            if k - start > 1 and any(j in notes for j in range(start, k)):
                out.update(range(start, k))
            start = k
    return out


def plain_notes(candidates: list[int], labels: list[str]) -> list[int]:
    """The plain template's note pages among `candidates`, pages with words and nothing drawn
    (`labels`: every page's label, "" where it has none). Beamer labels a slide with its frame
    number and a note page with its page number, so a note page's label is its own number while
    the slides around it count frames - fewer. In a PDF without notes only the first pages of the
    deck can be labelled with their own number (one frame each), all of them in a row: a lone page
    so labelled, or a row of them, is no note page (so one plain note in a deck is not found)."""
    if not all(labels) or len(candidates) < 2:
        return []
    notes = [k for k in candidates if k > 0 and labels[k] == str(k + 1)]
    if len(notes) < 2 or any(b == a + 1 for a, b in zip(notes, notes[1:])):
        return []
    slides = 0
    for k in range(len(labels)):
        if k in notes:
            continue
        slides += 1
        if not labels[k].isdigit() or int(labels[k]) > slides:
            return []
    return notes


def _bare(page: Page, drawings: list[Drawing]) -> bool:
    """Words and nothing drawn (the plain note template: `drawings`, the page's, are none), and no
    adopt marks."""
    return not drawings and not page.images() and bool(page.chars()) and not page_marks(page)


def _second_screen(doc: PdfDocument, spans: dict[int, list[PageSpan]]) -> dict[int, float] | None:
    """Note headers by page when `doc` is a second-screen PDF (each page a slide with its note
    page beside it on the right), else None. (The plain template's right halves, which have no
    header, are not told apart from a wide slide: docs/speaker-notes.md.)"""
    headers: dict[int, float] = {}
    for page in doc:
        header = _note_header(page, spans[page.index], (page.width / 2, 0.0, page.width, page.height))
        if header is not None:
            headers[page.index] = header
    return headers if len(headers) >= 0.5 * len(doc) else None


def _prepare(reading: Reading, pdf: Path, out: Path) -> Prepared:
    doc = reading.doc
    everything = list(range(len(doc)))
    if not len(doc):
        return Prepared(pdf=pdf, notes={}, mode=None, labels=None, kept=everything)
    notes: dict[int, str] = {}
    path = out / "slides.pdf"

    first = doc[0]
    wide = {page.index: shown_spans(page) for page in doc} if first.width / first.height >= 2.2 else None
    screen = _second_screen(doc, wide) if wide is not None else None
    if wide is not None and screen is not None:
        views = {page.index: _frame_view(wide[page.index], page, (0.0, 0.0, page.width / 2, page.height))
                 for page in doc}
        for page in doc:
            header = screen.get(page.index)
            if header is not None:
                text = read_note(page, (page.width / 2, 0.0, page.width, page.height), header)
                if text:
                    notes[page.index] = text
        # The pages' labels count pgfpages' sheets, not frames: a frame is a run of steps.
        labels: list[str] = []
        for k in everything:
            same = k > 0 and same_frame(views[k - 1], views[k])
            labels.append(labels[-1] if same else str(len(set(labels)) + 1))
        # keep the left half: the slide
        path.write_bytes(doc.save(pages=None, boxes={page.index: (0.0, 0.0, page.width / 2, page.height) for page in doc}))
        return Prepared(pdf=path, notes=carried(notes, labels, views), mode="second screen", labels=labels,
                        kept=everything)

    given = [_label(doc.label(k), k) for k in everything]
    headers: dict[int, float] = {}
    bare: list[int] = []
    for page in doc:
        # a page whose objects say what they are (adopt's marks) is a frame: a note page's words are
        # the note's, unmarked, and a slide with a band across its top and small words at its right
        # end (drawing-workshop 28 and 50) read as the note template
        if not page.index or page_marks(page):
            continue
        drawings = page.drawings()
        header = _header_in(drawings, functools.partial(shown_spans, page), page.rect)
        if header is not None:
            headers[page.index] = header
        elif _bare(page, drawings):
            bare.append(page.index)
    if not headers:
        headers = {k: 0.0 for k in plain_notes(bare, [_label(doc.label(k), k) if doc.label(k) else ""
                                                      for k in everything])}
    if not headers:
        return _carried(reading, pdf, given)
    keep = [k for k in everything if k not in headers]
    for k, header in headers.items():
        page = doc[k]
        text = read_note(page, page.rect, header)
        before = [j for j in keep if j < k]
        if before and text:
            notes[before[-1]] = (notes.get(before[-1], "") + "\n" + text).strip()
    path.write_bytes(doc.save(pages=keep, boxes=None))
    # The saved page label tree still counts the deleted pages.
    labels = [given[k] for k in keep]
    found = {keep.index(k): v for k, v in notes.items()}
    asked = _steps_in_question(found, labels)
    views = {i: _frame_view(shown_spans(doc[keep[i]]), doc[keep[i]], doc[keep[i]].rect) for i in sorted(asked)}
    return Prepared(pdf=path, notes=carried(found, labels, views), mode="note pages", labels=labels, kept=keep)


def _carried(reading: Reading, pdf: Path, labels: list[str]) -> Prepared:
    """The notes b2snotes.sty carries beside the pages, or none: the PDF is the slides as it is
    (extract never reads beyond a page's edges)."""
    doc = reading.doc
    found: dict[int, str] = {}
    for page in doc:
        notes = carried_blocks(reading.page_chars(page)[0], page.width, uri_links(page))  # carried_notes
        if notes.before and page.index:
            # a \note after a frame: beamer's note page would follow that frame's own
            found[page.index - 1] = (found.get(page.index - 1, "") + "\n" + notes.before).strip()
        if notes.here:
            found[page.index] = notes.here
    everything = list(range(len(doc)))
    if not found:
        return Prepared(pdf=pdf, notes={}, mode=None, labels=None, kept=everything)
    asked = _steps_in_question(found, labels)
    views = {k: _frame_view(shown_spans(doc[k]), doc[k], doc[k].rect) for k in sorted(asked)}
    return Prepared(pdf=pdf, notes=carried(found, labels, views), mode="carried", labels=None, kept=everything)


# --- notes from the source ---------------------------------------------------------------------------

SourceOutcome = Literal["found", "no-engine", "no-notes", "failed", "mismatch"]


@dataclass(frozen=True, kw_only=True)
class SourceNotes:
    """What compiling the source with its notes shown gave: the notes by page of the given PDF
    (`found`), or why there are none - no TeX engine, a source without \\note (`no-notes`; both
    leave the PDF to convert without notes), a compile error or a source whose pages are not the
    PDF's (`failed`, `mismatch`: the person asked for notes the PDF cannot be given)."""
    outcome: SourceOutcome
    notes: dict[int, str]
    message: str


def pair_pages(given: list[str], compiled: list[str]) -> list[int] | None:
    """For each page of the given PDF (by its labels), the page of the compiled one that is the
    same slide, or None when they cannot be paired. Equal counts pair in order; else frame by
    frame (a run of one label), each run of the given PDF no longer than the compiled one's and
    taking its last steps (a handout's one page per frame: the frame's last step)."""
    if len(given) == len(compiled):
        return list(range(len(given)))

    def runs(labels: list[str]) -> list[tuple[str, int, int]]:
        out: list[tuple[str, int, int]] = []
        for k, label in enumerate(labels):
            if out and out[-1][0] == label:
                out[-1] = (label, out[-1][1], k + 1)
            else:
                out.append((label, k, k + 1))
        return out
    a, b = runs(given), runs(compiled)
    if [r[0] for r in a] != [r[0] for r in b]:
        return None
    out: list[int] = []
    for (_, a0, a1), (_, b0, b1) in zip(a, b):
        m, n = a1 - a0, b1 - b0
        if m > n:
            return None
        out += [b1 - m + i for i in range(m)]
    return out


def word_share(a: list[str], b: list[str]) -> float:
    """How much of two pages' words they share (1: the same words; two empty pages too)."""
    if not a and not b:
        return 1.0
    common = sum((Counter(a) & Counter(b)).values())
    return common / max(len(a), len(b))


PAIRED_WORDS = 0.5  # a page and its pair share at least this much of their words


def _words(doc: PdfDocument) -> list[list[str]]:
    return [[w for s in shown_spans(page) for w in readable(s.text, s.font).split()] for page in doc]


def match_notes(pdf: Path, prepared: Prepared, tex: Path) -> SourceNotes:
    """The compiled source's notes (`prepared`) on the pages of the given `pdf`, when its pages
    are the same slides: paired by `pair_pages`, every pair sharing PAIRED_WORDS of its words."""
    given_doc = Document(pdf)
    compiled_doc = Document(prepared.pdf)
    try:
        given = [_label(given_doc.label(k), k) for k in range(len(given_doc))]
        compiled = prepared.labels or [_label(compiled_doc.label(k), k) for k in range(len(compiled_doc))]
        pairs = pair_pages(given, compiled)
        if pairs is None:
            return SourceNotes(outcome="mismatch", notes={}, message=(
                f"{tex.name} compiles to {len(compiled)} slides (frames {_frames(compiled)}), but {pdf.name} has "
                f"{len(given)} (frames {_frames(given)}): compile {pdf.name} from {tex.name} again, or convert "
                "without --tex"))
        words_given, words_compiled = _words(given_doc), _words(compiled_doc)
    finally:
        given_doc.close()
        compiled_doc.close()
    for k, j in enumerate(pairs):
        if word_share(words_given[k], words_compiled[j]) < PAIRED_WORDS:
            return SourceNotes(outcome="mismatch", notes={}, message=(
                f"page {k + 1} of {pdf.name} is not page {j + 1} of {tex.name} compiled (their words differ): "
                f"is {pdf.name} made from this source? Compile it again, or convert without --tex"))
    notes = {k: prepared.notes[j] for k, j in enumerate(pairs) if j in prepared.notes}
    return SourceNotes(outcome="found", notes=notes, message=(
        f"speaker notes from {tex.name}: {len(notes)} of {len(pairs)} slides have one"))


def _frames(labels: list[str]) -> str:
    distinct = list(dict.fromkeys(labels))
    return f"{distinct[0]}-{distinct[-1]}" if len(distinct) > 1 else (distinct[0] if distinct else "none")


def notes_from_source(tex: Path, pdf: Path, work: Path) -> SourceNotes:
    """Compile `tex` once more with its notes shown (`inverse.Workspace`, a copy of its folder
    under `work`) and read them onto the pages of `pdf`, which was compiled from it without them.
    Only ever for a source the person named: compiling runs their TeX."""
    compiled = compile_notes(tex, work)
    return compiled if isinstance(compiled, SourceNotes) else match_notes(pdf, compiled, tex)


def compile_notes(tex: Path, work: Path) -> Prepared | SourceNotes:
    """`tex` compiled with its notes shown, prepared (`prepare`), or why it cannot be."""
    from .inverse import Workspace, tex_env, uses_notes

    ws = Workspace(tex, work, handout=False, engine=None, fresh=True)
    if not uses_notes(ws.source):
        return SourceNotes(outcome="no-notes", notes={}, message=f"{tex.name} has no \\note: no speaker notes to bring")
    engine = ws.source.engine()
    if shutil.which(engine, path=tex_env()["PATH"]) is None:
        return SourceNotes(outcome="no-engine", notes={}, message=(
            f"{engine} is not installed here: the speaker notes in {tex.name} cannot be compiled, the deck "
            "is converted without them"))
    ws.notes = True
    compiled, error = ws.compile()
    if compiled is None:
        return SourceNotes(outcome="failed", notes={}, message=f"{tex.name} did not compile with its notes shown:\n{error}")
    prepared = prepare(compiled, work)
    if prepared.mode is None:
        return SourceNotes(outcome="no-notes", notes={}, message=(
            f"{tex.name} compiled with notes shown has no note pages (does it hide them, "
            "\\setbeameroption{hide notes}?)"))
    return prepared


def source_beside(pdf: Path) -> Path | None:
    """The .tex of the PDF's name beside it, when it holds a \\note: what convert names as a hint.
    It is never compiled unasked (it may not be this PDF's source, and compiling runs it)."""
    tex = pdf.with_suffix(".tex")
    try:
        text = tex.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return tex if re.search(r"(?m)^[^%\n]*\\note\b", text) else None
