"""Hebrew and Arabic come back from the PDF the wrong way round.

A PDF says nothing about reading order: it draws glyphs at places. Most writers that draw
right-to-left text put the glyphs down in **visual** order (leftmost first), so PDFium's text page
reverses every right-to-left run to hand the reader logical order back - and LuaTeX with
`bidi=basic`, which is what `adopt` and every Hebrew or Arabic beamer talk compile with, draws them
in **logical** order already. Measured on one word of the corpus deck `hebrew-lesson`: the content
stream's own items are `0x5d1 0x5e1 0x5d9 0x5e4 0x5d5 0x5e8` at falling x (בסיפור, right to left as
it is read), and `FPDFText_GetUnicode` gives them back `0x5e8 0x5d5 0x5e4 0x5d9 0x5e1 0x5d1` at
rising x. Every Hebrew word this converter wrote into a deck was spelt backwards, and nothing said
so: the glyphs are all there, the geometry is right, and no check this project has reads a word.

So the pipeline turns **visual order into logical order** itself, in three steps:

- `visual_chars` (extract) makes every line that holds right-to-left letters what the page shows:
  its characters left to right, as the glyphs stand, with the mirroring PDFium's text page added
  taken back off. Which writer drew what in which order stops mattering here: XeTeX draws a line
  left to right and PDFium turns each word round (its letters come back at falling x), LuaTeX
  draws each word in reading order as glyphs of their own.
- `logical_line` (classify, once lines are known) reads such a line: a line's pieces in reading
  order and each piece's own text. It is Unicode's bidirectional algorithm run backwards - the
  islands written the other way (a Latin name, a formula, each number) keep their order, the rest
  turns round, a bracket drawn in a right-to-left run is the other one - and the answer is kept
  when the algorithm itself (`display`, which is what Slides runs on what emit writes) draws the
  page's line from it. Where no plain text can (a formula that starts with a number, a name ending
  on its full stop, inside a Hebrew sentence), an LRM holds the island together.
- which way the line reads (`line_base`) - the page's own direction where it has one (`page_direction`),
  so that a Hebrew deck's line starting with a Latin name still reads right to left.

`logical_text` and `logical_spans` are the same for one span and for spans with no line around them.
A line with no right-to-left letters in it is left untouched, which is every deck this project had
until Hebrew ones came.

Reading a paragraph right is half of it: the deck has to be told which way it reads, or Slides
lays it out left to right and its full stop lands at the wrong end. `reads_rtl` is that question,
asked of words already in reading order - `classify.Paragraph.direction` carries the answer into
deck.json and `emit` writes it as the API's `direction`.
"""
from __future__ import annotations

import dataclasses
import unicodedata

NEUTRAL, LEFT, RIGHT, WEAK = range(4)

# CFX_BidiChar's four classes over Unicode's bidirectional categories. A weak character (a digit,
# a currency sign) is written forwards inside a right-to-left run and does not end it.
_CLASS = {"L": LEFT, "LRE": LEFT, "LRO": LEFT, "LRI": LEFT,
          "R": RIGHT, "AL": RIGHT, "RLE": RIGHT, "RLO": RIGHT, "RLI": RIGHT,
          "EN": WEAK, "AN": WEAK, "ES": WEAK, "ET": WEAK, "CS": WEAK}

# Unicode's own mirroring (Bidi_Mirroring_Glyph), which no `unicodedata` call gives: the pairs a
# line of prose can hold. Quotation marks are not mirrored (PDFium's own table mirrors them).
MIRRORED = {"(": ")", "[": "]", "{": "}", "<": ">", "‹": "›", "«": "»",
            "⁅": "⁆", "≪": "≫", "≤": "≥", "⊂": "⊃",
            "⊆": "⊇", "∈": "∋"}
MIRRORED.update({v: k for k, v in MIRRORED.items()})
_OPENING = {"(": ")", "[": "]", "{": "}"}
_CLOSING = {v: k for k, v in _OPENING.items()}
# The left-to-right and right-to-left marks: invisible strong letters, which is all a logical
# text can say where the page shows a left-to-right island Unicode's rules would cut in two.
LRM, RLM = "‎", "‏"


def _class(c: str) -> int:
    # A combining mark (NSM) is neutral here and never stands alone anyway: `_clusters` keeps it
    # on the letter it is drawn over, which is what decides where it goes.
    return _CLASS.get(unicodedata.bidirectional(c), NEUTRAL)


def has_rtl(text: str) -> bool:
    return any(_class(c) == RIGHT for c in text)


def looks_rtl(text: str) -> bool:
    """The text holds more right-to-left letters than left-to-right ones."""
    right = sum(1 for c in text if _class(c) == RIGHT)
    return right > sum(1 for c in text if _class(c) == LEFT)


def reads_rtl(text: str) -> bool:
    """Which way a paragraph reads, of text that is already in reading order: Unicode's P2,
    the first strong letter (an RLM that `logical_line` put in front of a right-to-left line
    starting with a Latin word is one)."""
    for c in text:
        if _class(c) in (LEFT, RIGHT):
            return _class(c) == RIGHT
    return False


def _clusters(text: str) -> list[tuple[int, int]]:
    """(start, stop) per letter and the marks drawn on it: a vowel point or an Arabic diacritic
    is written after its letter and stays there, or reversing the run would hang it on the
    letter before."""
    out: list[list[int]] = []
    for i, c in enumerate(text):
        if out and unicodedata.combining(c):
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(a, b) for a, b in out]


def logical_text(text: str, base: int | None = None) -> str:
    """One span's characters in reading order, given them as the page draws them: left to right.
    `base`: which way the line it stands in reads, where the caller knows (`logical_line`)."""
    return logical_line([text], base)[0][1]


# ---------------------------------------------------------------- Unicode's algorithm, both ways
# Slides lays a paragraph out with the Unicode bidirectional algorithm (UAX #9: weak types W1-W7,
# bracket pairs N0, neutrals N1-N2, levels I1-I2, trailing spaces L1, reversal L2, mirroring L4),
# so a logical text written into a deck is right when that algorithm, run on it, draws what the
# page draws. `display` is the algorithm for one paragraph level and no explicit embeddings (what
# a text box holds); `logical_line` goes the other way and keeps its answer when `display` agrees.

def _bidi_type(c: str) -> str:
    t = unicodedata.bidirectional(c)
    if t in ("L", "LRE", "LRO", "LRI"):
        return "L"
    if t in ("R", "RLE", "RLO", "RLI"):
        return "R"
    if t in ("AL", "EN", "AN", "ES", "ET", "CS", "WS"):
        return t
    return "ON"   # other neutrals and separators, a mark standing alone


def _strong(t: str) -> str | None:
    """What a resolved type counts as for the neutrals around it: numbers are right to left."""
    return "L" if t == "L" else "R" if t in ("R", "EN", "AN") else None


def _numbers(t: list[str]) -> list[str]:
    """W3-W6: a single separator between two numbers is the number's (20.11, 3.5), a currency or
    percent sign next to one too, and every other separator is a neutral - which reads the same
    whichever way round the text is."""
    t = ["R" if x == "AL" else x for x in t]
    for i in range(1, len(t) - 1):
        if t[i] == "ES" and t[i - 1] == t[i + 1] == "EN" or \
                t[i] == "CS" and t[i - 1] == t[i + 1] and t[i - 1] in ("EN", "AN"):
            t[i] = t[i - 1]
    i = 0
    while i < len(t):
        if t[i] != "ET":
            i += 1
            continue
        j = i
        while j < len(t) and t[j] == "ET":
            j += 1
        if i and t[i - 1] == "EN" or j < len(t) and t[j] == "EN":
            t[i:j] = ["EN"] * (j - i)
        i = j
    return ["ON" if x in ("ES", "ET", "CS") else x for x in t]


def _levels(text: str, cells: list[tuple[int, int]], base: int) -> list[int]:
    """The embedding level of each cluster of a logical text, as UAX #9 resolves it."""
    e = "R" if base == RIGHT else "L"
    t = [_bidi_type(text[a]) for a, _ in cells]
    last = e
    for i, x in enumerate(t):  # W2: a number after Arabic letters is an Arabic number
        if x in ("L", "R", "AL"):
            last = x
        elif x == "EN" and last == "AL":
            t[i] = "AN"
    t = _numbers(t)
    last = e
    for i, x in enumerate(t):  # W7: a number after Latin letters is Latin
        if x in ("L", "R"):
            last = x
        elif x == "EN" and last == "L":
            t[i] = "L"
    stack, pairs = [], []
    for i, (a, _) in enumerate(cells):  # N0: bracket pairs (BD16)
        c = text[a]
        if t[i] != "ON":
            continue
        if c in _OPENING:
            stack.append((c, i))
        elif c in _CLOSING:
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == _CLOSING[c]:
                    pairs.append((stack[k][1], i))
                    del stack[k:]
                    break
    for o, c in sorted(pairs):
        inside = {_strong(x) for x in t[o + 1:c]} - {None}
        if e in inside:
            t[o] = t[c] = e
        elif inside:
            before = next((_strong(x) for x in reversed(t[:o]) if _strong(x)), e)
            t[o] = t[c] = before if before in inside else e
    i = 0
    while i < len(t):  # N1, N2
        if _strong(t[i]):
            i += 1
            continue
        j = i
        while j < len(t) and not _strong(t[j]):
            j += 1
        before = _strong(t[i - 1]) if i else e
        after = _strong(t[j]) if j < len(t) else e
        t[i:j] = [before if before == after else e] * (j - i)
        i = j
    if e == "L":
        levels = [0 if x == "L" else 1 if x == "R" else 2 for x in t]
    else:
        levels = [1 if x == "R" else 2 for x in t]
    k = len(cells)  # L1: whitespace at the end of the line goes back to the paragraph's level
    while k and text[cells[k - 1][0]].isspace():
        k -= 1
        levels[k] = 1 if base == RIGHT else 0
    return levels


def display(text: str, base: int) -> str:
    """A logical text as a paragraph reading `base` shows it, left to right (UAX #9 L2 and L4):
    what Slides draws of what emit writes."""
    cells = _clusters(text)
    levels = _levels(text, cells, base)
    order = list(range(len(cells)))
    for level in range(max(levels, default=0), 0, -1):
        i = 0
        while i < len(order):
            if levels[order[i]] < level:
                i += 1
                continue
            j = i
            while j < len(order) and levels[order[j]] >= level:
                j += 1
            order[i:j] = order[i:j][::-1]
            i = j
    out = []
    for k in order:
        a, b = cells[k]
        if text[a] in (LRM, RLM):
            continue
        out.append((MIRRORED.get(text[a], text[a]) if levels[k] % 2 else text[a]) + text[a + 1:b])
    return "".join(out)


def line_base(texts: list[str], prior: int | None = None) -> int:
    """Which way a line reads, given its pieces as the page draws them, left to right.

    A line of one script's letters reads that script's way. A line with both takes the page's
    way where the page has one (`prior`: a Hebrew deck's Latin names, an English deck's Hebrew
    words); else Unicode's P2 is asked of both readings - reading it right to left puts the last
    strong letter first, left to right the first - and where both hold up, the commoner letters
    decide."""
    text = "".join(texts)
    right = sum(1 for c in text if _class(c) == RIGHT)
    left = sum(1 for c in text if _class(c) == LEFT)
    if not right:
        return LEFT
    if not left:
        return RIGHT
    if prior is not None:
        return prior
    strong = [_class(c) for c in text if _class(c) in (LEFT, RIGHT)]
    right_holds, left_holds = strong[-1] == RIGHT, strong[0] == LEFT
    if right_holds != left_holds:
        return RIGHT if right_holds else LEFT
    return RIGHT if right > left else LEFT


def page_direction(texts) -> int | None:
    """A page's own direction: the one with at least twice the other's letters, else None."""
    right = left = 0
    for text in texts:
        for c in text:
            k = _class(c)
            right += k == RIGHT
            left += k == LEFT
    if right >= 2 * max(left, 1):
        return RIGHT
    if left >= 2 * max(right, 1):
        return LEFT
    return None


def _islands(text: str, cells: list[tuple[int, int]], base: int) -> list[tuple[int, int]]:
    """(start, stop) clusters of a visual line that are written the other way from `base`, each in
    one piece. On a right-to-left line: each number, and each stretch of Latin letters with the
    numbers and neutrals between them (`1.44 log2(n + 2)`), the punctuation touching its end (a
    name's full stop, a closing bracket) and an opening bracket touching its start. On a
    left-to-right line: each stretch of right-to-left letters with what is between them."""
    t = _numbers([_bidi_type(text[a]) for a, _ in cells])
    n = len(t)
    out: list[tuple[int, int]] = []
    i = 0
    if base == LEFT:
        while i < n:
            if t[i] != "R":
                i += 1
                continue
            j = k = i
            while k < n and t[k] != "L":
                if t[k] == "R":
                    j = k
                k += 1
            out.append((i, j + 1))
            i = j + 1
        return out
    while i < n:
        if t[i] not in ("L", "EN", "AN"):
            i += 1
            continue
        members, k = [i], i + 1
        while k < n and t[k] != "R":
            if t[k] in ("L", "EN", "AN"):
                members.append(k)
            k += 1
        if any(t[m] == "L" for m in members):
            start, stop = members[0], members[-1] + 1
            while stop < n and t[stop] == "ON":
                stop += 1
            while start and t[start - 1] == "ON" and text[cells[start - 1][0]] in _OPENING:
                start -= 1
            if out and out[-1][1] > start:
                start = out[-1][1]
            out.append((start, stop))
            i = stop
            continue
        for m in members:  # numbers only: each an island of its own, as 12 and 13 of 12-13
            if out and out[-1][1] > m:
                continue
            stop = m + 1
            while stop < n and t[stop] in ("EN", "AN"):
                stop += 1
            out.append((m, stop))
        i = k
    return out


def _reading(text: str, cells: list[tuple[int, int]], base: int, marks: bool):
    """The clusters of a visual line in reading order, the clusters read at a right-to-left level
    (a bracket there is the other one), and the LRMs to write before and after clusters where
    `marks` asks for them: around a Latin island that does not start with a letter or ends on
    punctuation, which Unicode's rules would otherwise cut off it."""
    islands = _islands(text, cells, base)
    kinds = _numbers([_bidi_type(text[a]) for a, _ in cells])
    blocks: list[list[int]] = []  # in visual order: each island, and every other cluster alone
    turned: set[int] = set()
    before: dict[int, str] = {}
    after: dict[int, str] = {}
    i = 0
    for a, b in islands:
        blocks += [[k] for k in range(i, a)]
        if base == LEFT:
            blocks.append(_reading_island(kinds, a, b))
            turned.update(range(a, b))
        else:
            blocks.append(list(range(a, b)))
            if marks and any(kinds[k] == "L" for k in range(a, b)):
                if kinds[a] != "L":
                    before[a] = LRM
                if kinds[b - 1] in ("ON", "WS"):
                    after[b - 1] = LRM
        i = b
    blocks += [[k] for k in range(i, len(cells))]
    if base == RIGHT:
        inside = {k for a, b in islands for k in range(a, b)}
        turned = set(range(len(cells))) - inside
        blocks.reverse()
    return [k for block in blocks for k in block], turned, before, after


def _reading_island(kinds: list[str], a: int, b: int) -> list[int]:
    """A right-to-left island of a left-to-right line in reading order: turned round, except
    its numbers."""
    runs: list[list[int]] = []
    for k in range(a, b):
        if runs and kinds[k] in ("EN", "AN") and kinds[runs[-1][-1]] in ("EN", "AN"):
            runs[-1].append(k)
        else:
            runs.append([k])
    return [k for run in reversed(runs) for k in run]


def logical_line(texts: list[str], base: int | None = None,
                 joins: list[str] | None = None) -> list[tuple[int, str]]:
    """A line's pieces in reading order, given them left to right as the page draws them: (index
    of the piece, its text as read). `joins[i]` is what stands between pieces i and i + 1 on the
    page and neither holds (a word space: it separates, it is not written). `base` defaults to
    `line_base`. A line with no right-to-left letters that is not said to read right to left
    comes back as it was."""
    joins = list(joins or []) + [""] * len(texts)
    visual = "".join(t + joins[i] for i, t in enumerate(texts))
    if base != RIGHT and not has_rtl(visual):
        return list(enumerate(texts))
    if base is None:
        base = line_base(texts)
    cells, owner, pos = [], [], 0
    for i, t in enumerate(texts):  # (a cluster never crosses from one piece into the next)
        cells += [(pos + a, pos + b) for a, b in _clusters(t)]
        owner += [i] * (len(cells) - len(owner))
        pos += len(t)
        for c in joins[i]:
            cells.append((pos, pos + 1))
            owner.append(None)
            pos += 1
    shown = visual.strip()
    best = None
    for marks in ((), ("before",), ("after",), ("before", "after")) if base == RIGHT else ((),):
        order, turned, before, after = _reading(visual, cells, base, bool(marks))
        before, after = (before if "before" in marks else {}), (after if "after" in marks else {})
        written = "".join(before.get(k, "") + _as_read(visual, cells[k], k in turned) + after.get(k, "")
                          for k in order)
        fits = display(written, base).strip() == shown
        if best is None or fits:
            best = (order, turned, before, after)
        if fits:
            break
    order, turned, before, after = best
    owner = _strays(visual, cells, owner, order)
    pieces: dict[int, list[str]] = {}
    first: dict[int, int] = {}
    for rank, k in enumerate(order):
        i = owner[k]
        if i is None:
            continue
        first.setdefault(i, rank)
        pieces.setdefault(i, []).append(before.get(k, "") + _as_read(visual, cells[k], k in turned) + after.get(k, ""))
    ranked = sorted(range(len(texts)), key=lambda i: first.get(i, -1))
    return [(i, "".join(pieces[i]) if i in pieces else texts[i]) for i in ranked]


def lead_mark(spans: list) -> str:
    """RLM for spans of a right-to-left line (`reading`) whose words start with a Latin one - a
    table cell 'O(1) on average' in Hebrew - which Unicode's P2, all a cell is asked
    (`reads_rtl`), would read left to right; else nothing."""
    bases = {s.reading[2] for s in spans if getattr(s, "reading", None)}
    if bases == {RIGHT} and not reads_rtl("".join(s.text for s in spans)):
        return RLM
    return ""


def spaced(texts: list[str], base: int, joins: list[str]) -> dict[int, int]:
    """Which join stands before each piece as the line is read, where one does: {piece: join}.
    Pieces read one after the other need not stand side by side on the page (the full stop after
    a formula at the left end of a Hebrew line, the formula's first number next to it), so the
    room between two of them is that join's, or none - not the distance between them."""
    joins = list(joins) + [""] * len(texts)
    visual = "".join(t + joins[i] for i, t in enumerate(texts))
    cells, owner, pos = [], [], 0
    for i, t in enumerate(texts):
        cells += [(pos + a, pos + b) for a, b in _clusters(t)]
        owner += [("piece", i)] * (len(cells) - len(owner))
        pos += len(t)
        for c in joins[i]:
            cells.append((pos, pos + 1))
            owner.append(("join", i))
            pos += 1
    order = _reading(visual, cells, base, False)[0]
    out: dict[int, int] = {}
    seen: set[int] = set()
    for prev, k in zip([None] + order, order):
        kind, i = owner[k]
        if kind == "piece" and i not in seen:
            seen.add(i)
            if prev is not None and owner[prev][0] == "join":
                out[i] = owner[prev][1]
    return out


def _strays(text: str, cells: list[tuple[int, int]], owner: list, order: list[int]) -> list:
    """Who holds each cluster as the line is read. A piece's space that the reading takes away
    from the rest of it (' O' before a formula on a Hebrew line: the space is read after the
    formula) goes with the piece read just before it, where it is read."""
    owner = list(owner)
    runs: dict[int, list[list[int]]] = {}
    prev = None
    for k in order:
        i = owner[k]
        if i is not None:
            if runs.get(i) and runs[i][-1][-1] == prev:
                runs[i][-1].append(k)
            else:
                runs.setdefault(i, []).append([k])
        prev = k
    for i, parts in runs.items():
        if len(parts) < 2:
            continue
        main = max(parts, key=len)
        for part in parts:
            if part is main or not all(text[cells[k][0]].isspace() for k in part):
                continue
            where = order.index(part[0])
            host = next((owner[k] for k in reversed(order[:where]) if owner[k] is not None and owner[k] != i), None)
            if host is None:
                host = next((owner[k] for k in order[where + len(part):] if owner[k] is not None and owner[k] != i), None)
            if host is not None:
                for k in part:
                    owner[k] = host
    return owner


def _as_read(text: str, cell: tuple[int, int], turned: bool) -> str:
    """One cluster as it is read: a bracket drawn in a right-to-left run is the other one."""
    a, b = cell
    return MIRRORED.get(text[a], text[a]) + text[a + 1:b] if turned else text[a:b]


# ---------------------------------------------------------------- spans with no line around them

def _base(classes: list[int]) -> int:
    """`line_base` over pieces' classes: P2 asked of both readings, else the commoner direction."""
    strong = [c for c in classes if c in (LEFT, RIGHT)]
    if not strong:
        return LEFT
    right_holds, left_holds = strong[-1] == RIGHT, strong[0] == LEFT
    if right_holds != left_holds:
        return RIGHT if right_holds else LEFT
    return RIGHT if strong.count(RIGHT) > strong.count(LEFT) else LEFT


def _resolved(classes: list[int], base: int) -> list[int]:
    """Every neutral given a direction: the one on both sides of it where those agree (a space
    between two Hebrew words is Hebrew), else the line's own."""
    out = [RIGHT if c == RIGHT else LEFT if c in (LEFT, WEAK) else None for c in classes]
    for i, c in enumerate(out):
        if c is not None:
            continue
        before = next((x for x in reversed(out[:i]) if x is not None), None)
        after = next((x for x in out[i + 1:] if x is not None), None)
        out[i] = before if before is not None and before == after else base
    return out


def _reorder(classes: list[int], base: int) -> list[int]:
    """The places of pieces given left to right, in reading order: on a line that reads right to
    left the order turns round and each left-to-right island turns back inside it; on a line
    that reads left to right only the right-to-left islands turn."""
    where = _resolved(classes, base)
    order = list(range(len(classes)))
    if base == RIGHT:
        order.reverse()
        where = where[::-1]
        odd = LEFT
    else:
        odd = RIGHT
    out: list[int] = []
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and where[j] == odd:
            j += 1
        if j > i:
            out += order[i:j][::-1]
            i = j
        else:
            out.append(order[i])
            i += 1
    return out


def _span_class(text: str) -> int:
    """What a whole span counts as: the strong direction its letters agree on, else neutral."""
    classes = {_class(c) for c in text}
    if RIGHT in classes and LEFT not in classes:
        return RIGHT
    if LEFT in classes and RIGHT not in classes:
        return LEFT
    if RIGHT in classes:  # both: whichever there is more of
        return RIGHT if looks_rtl(text) else LEFT
    return NEUTRAL


def logical_spans(items: list, text=lambda s: s.text) -> list:
    """A line's spans in reading order, given them left to right as the page draws them. Spans
    that `classify` read as one line (`reading`: line, rank, base - see `logical_line`) keep that
    order; others are put in order by their texts alone."""
    if len(items) < 2:
        return list(items)
    keys = [getattr(s, "reading", None) for s in items]
    if all(k is not None for k in keys) and len({k[0] for k in keys}) == 1:
        return [s for _, s in sorted(zip(keys, items), key=lambda p: p[0][1])]
    classes = [_span_class(text(s)) for s in items]
    if RIGHT not in classes:
        return list(items)
    return [items[i] for i in _reorder(classes, _base(classes))]


# ---------------------------------------------------------------- what the text page did
# PDFium's text page (CPDF_TextPage::CloseTempLine, ported in pdf/pure/textpage.py) cuts each line
# into CFX_BidiChar segments and writes the right-to-left ones backwards, every character of them
# mirrored - a neutral one too while the last strong letter was right to left: '(' after a Hebrew
# cell comes back ')' though an English word follows it. `visual_chars` undoes both.
_PDFIUM_NEUTRAL, _PDFIUM_LEFT, _PDFIUM_RIGHT = 0, 1, 2


def _line_groups(chars: list) -> list[list[int]]:
    """Indices of the characters of each line, in the order the text page wrote them: a new line
    where the baseline or the direction changes (as the text page's own temp line ends)."""
    groups: list[list[int]] = []
    first = None
    for i, ch in enumerate(chars):
        if first is not None and ch.dir == first.dir:
            ux, uy = ch.dir
            dx, dy = ch.origin[0] - first.origin[0], ch.origin[1] - first.origin[1]
            if abs(dx * uy - dy * ux) <= 0.3 * max(first.size, ch.size, 0.01):
                groups[-1].append(i)
                continue
        groups.append([i])
        first = ch
    return groups


def _unmirrored(chars: list, idx: list[int]) -> dict[int, str]:
    """The text of the line's characters PDFium mirrored, as the page draws them."""
    from .pdf.pure.unicode_data import direction, mirror  # PDFium's own tables (imported late: pure is big)
    out: dict[int, str] = {}
    current = _PDFIUM_LEFT
    for i in idx:
        units = []
        for u in chars[i].c:
            d = direction(ord(u)) if ord(u) < 0x10000 else _PDFIUM_NEUTRAL
            if d == _PDFIUM_RIGHT:
                current = _PDFIUM_RIGHT
            elif d == _PDFIUM_LEFT:
                current = _PDFIUM_LEFT
            elif d == _PDFIUM_NEUTRAL and current == _PDFIUM_RIGHT:
                u = chr(mirror(ord(u)))
            units.append(u)
        if "".join(units) != chars[i].c:
            out[i] = "".join(units)
    return out


def _along(ch) -> float:
    return ch.origin[0] * ch.dir[0] + ch.origin[1] * ch.dir[1]


def _is_mark(c: str) -> bool:
    return bool(c) and all(unicodedata.combining(u) for u in c)


def _left_to_right(line: list, spaces: list[float]) -> list:
    """A line's characters left to right, where it draws right-to-left letters leftwards (XeTeX,
    whose words PDFium's reversal leaves with their letters at falling x; LuaTeX, which draws
    each word as it is read). A mark goes with its letter. The spaces are left out - XeTeX's are
    the text page's, at the next word's origin over its letters - and the gaps say where words
    end (`extract.spans`). One glyph for several letters holds them in reading order; turned
    round, it reads as the page shows it too.

    A letter drawn leftwards right after another, with no space on the line between them (the
    text page's own, `spaces`: where along the line it put one), touches it: its advance is
    the distance between their origins. PDFium gives Arabic glyphs of a font with no widths for
    them one advance, 0.21 em for every letter, and the gaps that left split words into letters."""
    clusters: list[list] = []
    drawn: dict[int, int] = {}
    for n, ch in enumerate(line):
        if clusters and _is_mark(ch.c):
            clusters[-1].append(ch)
            continue
        text = ch.c.strip(" ")
        if not text:
            continue
        if len(text) > 1 and has_rtl(text):
            text = "".join(text[a:b] for a, b in reversed(_clusters(text)))
        drawn[len(clusters)] = n
        clusters.append([ch if text == ch.c else _with_text(ch, text)])
    turn = sorted(range(len(clusters)), key=lambda k: _along(clusters[k][0]))
    for left, right in zip(turn, turn[1:]):
        a, b = clusters[left][0], clusters[right][0]
        gap = _along(b) - _along(a) - a.advance
        if drawn[right] == drawn[left] - 1 and a.obj == b.obj and a.font == b.font and has_rtl(a.c) and \
                has_rtl(b.c) and 0 < gap < 0.5 * max(a.size, 0.01) and \
                not any(_along(a) < s < _along(b) for s in spaces):
            clusters[left][0] = dataclasses.replace(a, advance=_along(b) - _along(a))
    return [ch for k in turn for ch in clusters[k]]


def _with_text(ch, text: str):
    return dataclasses.replace(ch, c=text)


def visual_chars(chars: list) -> list:
    """The page's characters with each line that holds right-to-left letters as the page shows it:
    the characters PDFium's text page mirrored given back their own text, and a line whose
    letters do not run left to right put so (`_left_to_right`) - which is what `logical_line`
    reads. A page without right-to-left letters comes back as it was."""
    if not any(has_rtl(ch.c) for ch in chars):
        return chars
    out = list(chars)
    result: list = []
    gaps = [ch for ch in out if not ch.c.strip()]
    for idx in _line_groups(out):
        if not any(has_rtl(out[i].c) for i in idx):
            result += [out[i] for i in idx]
            continue
        for i, text in _unmirrored(out, idx).items():
            out[i] = _with_text(out[i], text)
        line = [out[i] for i in idx]
        placed = [ch for ch in line if ch.c.strip() and not _is_mark(ch.c)]
        if any(_along(b) < _along(a) - 0.05 * max(a.size, 0.01) for a, b in zip(placed, placed[1:])):
            first = line[0]
            ux, uy = first.dir
            on_line = lambda s: s.dir == first.dir and abs((s.origin[0] - first.origin[0]) * uy - (
                s.origin[1] - first.origin[1]) * ux) <= 0.3 * max(first.size, 0.01)
            line = _left_to_right(line, [_along(s) for s in gaps if on_line(s)])
        result += line
    return result
