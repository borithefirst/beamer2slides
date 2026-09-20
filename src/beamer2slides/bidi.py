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

So the pipeline turns **visual order into logical order** itself, which is right whichever side did
the reversing - PDFium's pass or the PDF's own drawing order - because both leave the same thing on
the page: a right-to-left run whose characters run left to right.

Two sizes, because a line is cut into spans by geometry (`extract.spans`) long before anything asks
what it says, and one rule (`_reorder`) for both: the order turns round and each island of the
other direction turns back inside it.

- `logical_text` puts one span's characters back. A whole line often *is* one span - Hebrew word
  spaces are under `extract`'s word gap - so reversing each right-to-left run in place, which is
  `CPDF_TextPage::CloseTempLine`'s own rule, would spell every word right and leave the sentence
  backwards. It was measured doing exactly that.
- `logical_spans` puts a line's spans back, given them left to right as the page draws them.

Both are their own inverse, so they are right whichever side did the reversing - and both leave a
line with no right-to-left letters in it untouched, which is every deck this project had until now.

Mirrored characters go back too (a run written backwards has its brackets mirrored with it), a mark
stays on the letter it is drawn over (`_clusters`), and which way the line reads is `_base`.

Reading a paragraph right is half of it: the deck has to be told which way it reads, or Slides
lays it out left to right and its full stop lands at the wrong end. `reads_rtl` is that question,
asked of words already in reading order - `classify.Paragraph.direction` carries the answer into
deck.json and `emit` writes it as the API's `direction`.
"""
from __future__ import annotations

import unicodedata

NEUTRAL, LEFT, RIGHT, WEAK = range(4)

# CFX_BidiChar's four classes over Unicode's bidirectional categories. A weak character (a digit,
# a currency sign) is written forwards inside a right-to-left run and does not end it.
_CLASS = {"L": LEFT, "LRE": LEFT, "LRO": LEFT, "LRI": LEFT,
          "R": RIGHT, "AL": RIGHT, "RLE": RIGHT, "RLO": RIGHT, "RLI": RIGHT,
          "EN": WEAK, "AN": WEAK, "ES": WEAK, "ET": WEAK, "CS": WEAK}

# Unicode's own mirroring, which no `unicodedata` call gives: the pairs a line of prose can hold.
MIRRORED = {"(": ")", "[": "]", "{": "}", "<": ">", "‹": "›", "«": "»",
            "‘": "’", "“": "”", "⁅": "⁆", "≪": "≫",
            "≤": "≥", "⊂": "⊃", "⊆": "⊇"}
MIRRORED.update({v: k for k, v in MIRRORED.items()})


def _class(c: str) -> int:
    # A combining mark (NSM) is neutral here and never stands alone anyway: `_clusters` keeps it
    # on the letter it is drawn over, which is what decides where it goes.
    return _CLASS.get(unicodedata.bidirectional(c), NEUTRAL)


def looks_rtl(text: str) -> bool:
    """The text holds more right-to-left letters than left-to-right ones."""
    right = sum(1 for c in text if _class(c) == RIGHT)
    return right > sum(1 for c in text if _class(c) == LEFT)


def reads_rtl(text: str) -> bool:
    """Which way a paragraph reads, of text that is already in reading order: Unicode's P2,
    the first strong letter, asked of the question this time and not of the answer (`_base`
    is the same rule where the order is what is in doubt)."""
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


def _base(classes: list[int]) -> int:
    """Which way the line reads, given its things left to right as the page draws them.

    Unicode's P2 says a paragraph takes the direction of its first strong character - of the text
    *as it is read*, which is the answer and not the question. So each reading is asked whether it
    obeys that rule: reading it right to left puts the last strong thing first, left to right the
    first. Where only one reading holds up it is the answer, whichever side has more letters;
    where both do (a Latin sentence with a Hebrew phrase in it, or the other way round) nothing
    but the letters can say, so the commoner direction decides."""
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
    """The places of things given left to right, in reading order.

    On a line that reads right to left the whole order turns round and each left-to-right island
    (a name in Latin letters, a year) turns back inside it; on a line that reads left to right only
    the right-to-left islands turn. Which is the Unicode algorithm's L2 for one level of nesting,
    and all a converter can honestly do with a page that has no levels written on it."""
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


def logical_text(text: str) -> str:
    """One span's characters in reading order, given them as the page draws them: left to right."""
    cells = _clusters(text)
    classes = [_class(text[a]) for a, _ in cells]
    if RIGHT not in classes:
        return text
    base = _base(classes)
    where = _resolved(classes, base)
    out = []
    for i in _reorder(classes, base):
        a, b = cells[i]
        # A bracket in a right-to-left run was mirrored when the run was written backwards.
        out.append(MIRRORED.get(text[a], text[a]) + text[a + 1:b] if where[i] == RIGHT else text[a:b])
    return "".join(out)


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
    """A line's spans in reading order, given them left to right as the page draws them."""
    if len(items) < 2:
        return list(items)
    classes = [_span_class(text(s)) for s in items]
    if RIGHT not in classes:
        return list(items)
    return [items[i] for i in _reorder(classes, _base(classes))]
