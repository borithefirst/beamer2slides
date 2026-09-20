r"""Does a source `adopt` writes survive being edited? The same edits, in both forms, judged by compiling.

`adopt_bench` measures fidelity (does the source draw the deck) and `readability` measures how the
source reads. Neither asks the question the source exists for: a person opens it, changes a line, and
the slide still stands up. This measures that, on the corpus, by making the change and compiling.

A sample of slides is drawn from the corpus (stratified over prose, list, table, shape, picture and
title slides, deterministic from a seed), and each edit is applied to the **same slide in both
forms** - `m6-a`, the absolute `textblock*` + `\vbox to` writer, and `ls-a`, the `slidebox` /
`\slidepar` / `itemize` one. The edit is found by the words it changes, so both forms get the same
edit on the same element of the same slide:

  reword-longer   the line's words +50%      does it wrap inside its box or run out of it?
  reword-shorter  half the words
  restyle-run     two words into \textbf
  change-title    another title of about the same length
  move-box        the element's box 20 pt right
  add-item        one more list item
  delete-item     one list item less
  add-paragraph   one more paragraph in a text box
  add-table-row   one more row at the end of a table

Each (edit, form) is compiled **as a one-frame document** with the deck's own preamble and .sty files
(as `adopt_bench.broken_frames` does), the page is rendered, and three things are asked:

  compiles   lualatex produced a PDF, of one page
  confined   every pixel that differs from the unedited page lies in the room the edit is allowed:
             the element's own box out of the IR plus PAD, **together with the pixels that element
             already covers** on the unedited page (measured by compiling the frame once without
             it). Slides lets a box's last line hang out of it and the forms reproduce that, so a
             breach is room the edit *took*, never room the slide already used. move-box allows the
             box that much further right; an edit that *adds* content (add-item, add-paragraph,
             add-table-row) allows the box's column as far as the edge its flow grows towards
             (`box_align`: a bottom-aligned box grows upward, as it does in Slides), since a box
             given more to say does grow - but it must not grow sideways nor disturb its
             neighbours. A reword gets no such room: whether the words still fit is the question.
             A table's box is the one the source draws (`table_of`), not the IR's: a
             Slides table sizes itself to its rows, so the box the API reports is not the table on
             the slide. IR boxes are put in the page's units first (`on_page`).
  visible    the page really changed, and the words the edit writes are on it while the words it
             takes away are not (an edit that compiles because LaTeX swallowed it is a failure)

`confined` is the whole of "the layout holds", and it is fair because it is one criterion, measured
the same way on both forms, against the same element box, with no reference to how either form is
written. TeX's own overfull warnings cannot serve: both forms set `\hfuzz=\maxdimen` and
`\vfuzz=\maxdimen` inside `\slidesbox`, so neither ever reports a box it overflows. Where a box is
breached the sides are reported, because "right" (the words ran on instead of wrapping) and "bottom"
(the flow outgrew its box) read very differently.

The fourth number is the cost of the form: `lines`, how many source lines a person must add, delete
or change to make that edit. It counts the edit as written here, which is the edit a person types:
where one list item is one line it costs 1, where it is a four-line `\vbox` paragraph it costs 4.

  sample   the slides the seed draws, and what each one can take
  run      apply, compile and judge; --jobs compiles in parallel (one deck and form per worker)
  report   the table per form and per edit kind, from a finished run

Results go to `out/edit-robustness/<tag>`; the numbers live in `docs/adopt-bench.md`.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from beamer2slides.paths import CHECKOUT

from .adopt_bench import CORPUS, split_frames

OLD = "m6-a"          # absolute textblock* per element, \vbox to, \leftskip, per-paragraph plumbing
NEW = "ls-a"          # slidebox / \slidepar / \slidetext / itemize / \frametitle / a recovered theme
FORMS = (OLD, NEW)

PAD = 3.0             # bp of room around an element's box: a descender is not a breach
WIDTH_PX = 1000       # the page is rendered this wide
BREACH_PX = 40        # fewer changed pixels outside the box than this is antialiasing at the rim
MIN_CHANGE_PX = 40    # fewer changed pixels than this: nothing happened
DIFF_LEVEL = 16       # a channel must differ by this much for a pixel to count as changed
SHIFT = 20.0          # bp the move-box edit moves a box

CATEGORIES = ("prose", "list", "table", "shape", "picture", "title")

# words no adopted deck says, so the judgement can find what an edit wrote (and no "fi"/"fl", which
# come back out of the PDF as one ligature character)
TITLE_WORDS = ("Revised", "heading", "put", "here", "to", "stand", "next", "to", "the", "rest")
LINE_WORDS = ("Newly", "added", "remark", "about", "the", "topic", "under", "study", "here", "today")
CELL_WORDS = ("Newly", "added")
FILLER = ("Furthermore", "the", "same", "remark", "applies", "to", "every", "other",
          "case", "that", "we", "have", "met", "up", "to", "now")


# ------------------------------------------------------------------------------- the words of a line

def longer(text: str) -> str:
    """The line's words and half as many again: clearly longer, still prose."""
    words = text.split()
    extra = [FILLER[k % len(FILLER)] for k in range(max(2, round(0.5 * len(words))))]
    return " ".join(words + extra)


def shorter(text: str) -> str:
    words = text.split()
    return " ".join(words[:max(2, len(words) // 2)])


def like(text: str, pool: tuple[str, ...]) -> str:
    """A different phrase of about `text`'s length: the edit is about change, not about length."""
    out: list[str] = []
    while len(out) < 24:
        nxt = pool[len(out) % len(pool)]
        if out and len(" ".join(out + [nxt])) > len(text):
            break
        out.append(nxt)
    return " ".join(out if len(out) >= 2 else list(pool[:2]))


PLAIN = re.compile(r"^[\w \-,.:;'’‘“”()/…]+$", re.UNICODE)


def plain(text: str) -> bool:
    """Words a person retypes and a program splices, and this tool's own edits are written in the
    same script: no LaTeX special, no command, no ligature (a ligature comes back out of the PDF as
    one character), nothing outside Latin - dropping a Latin phrase into a right-to-left line asks
    a question about bidi, not about the form the source is written in."""
    return (bool(text) and PLAIN.match(text) is not None
            and all(ord(c) < 0x250 for c in text)
            and not any(lig in text for lig in ("fi", "fl", "ff")))


def sole_line(lines: list[str], text: str) -> int | None:
    """The one line holding `text`, or None when no line or several do."""
    hits = [i for i, ln in enumerate(lines) if text in ln]
    return hits[0] if len(hits) == 1 else None


def clean(line: str) -> bool:
    """A line whose words a person edits without touching measurements: no per-word struts."""
    return "\\slidestrut" not in line and "\\hskip" not in line and "\\llap" not in line


def retext(line: str, old: str, new: str) -> str:
    return line.replace(old, new, 1)


# ------------------------------------------------------------------------------------- the two forms

TEXTBLOCK = re.compile(r"\\begin\{textblock\*\}\{[^}]*\}\(\s*(-?[\d.]+)bp\s*,")
SLIDEBOX = re.compile(r"\\begin\{slidebox\}(?:\[[^\]]*\])?\{\s*(-?[\d.]+)\s*,")
SLIDEONE = re.compile(r"\\(?:slidetext|slidepicture|slideshape|sliderect|slideellipse)"
                      r"(?:\[[^\]]*\])?\{\s*(-?[\d.]+)\s*,")
SLIDETABLE = re.compile(r"\\begin\{slidetable\}(?:\[.*\])?\{\s*(-?[\d.]+)\s*,")
PARSTART = re.compile(r"\{\\leftskip=")
PAREND = re.compile(r"\\par\}")
ITEM = re.compile(r"^\s*\\item\b")
SLIDEPAR = re.compile(r"^\s*\\slidepar\b")
LISTSTART = re.compile(r"^\s*\\begin\{(?:itemize|enumerate)\}")
ITEMWORDS = re.compile(r"^\\(?:item|slidepar)\b(?:\[[^\]]*\])?\s*(.*)$")
OPTS = re.compile(r"\\(?:begin\{slidebox\}|slidetext)\[([^\]]*)\]")


def place_line(tag: str, lines: list[str], i: int) -> tuple[int, re.Pattern] | None:
    """The line that puts the element carrying line `i` on the page, and the pattern whose first
    group is its x. In `ls-a` that is the box the paragraph is in, or the one-shot macro on the line
    itself; in `m6-a` the `textblock*` around it."""
    if tag == OLD:
        for k in range(i, -1, -1):
            if k != i and "\\end{textblock*}" in lines[k]:
                return None
            if TEXTBLOCK.search(lines[k]):
                return k, TEXTBLOCK
        return None
    for pat in (SLIDEONE, SLIDETABLE):
        if pat.search(lines[i]):
            return i, pat
    for k in range(i, -1, -1):
        if k != i and "\\end{slidebox}" in lines[k]:
            return None
        if SLIDEBOX.search(lines[k]):
            return k, SLIDEBOX
    return None


def element_span(tag: str, lines: list[str], i: int) -> tuple[int, int] | None:
    r"""The source lines that draw the element carrying line `i`, so that a compile without them
    shows what the rest of the slide draws: the `textblock*` in `m6-a`, the `slidebox` in `ls-a`,
    or the single line a `\slidetext` (or another one-shot macro) is."""
    found = place_line(tag, lines, i)
    if found is None:
        return None
    k, pat = found
    if k == i and pat in (SLIDEONE, SLIDETABLE):
        return i, i
    close = "\\end{textblock*}" if tag == OLD else "\\end{slidebox}"
    end = next((j for j in range(k, len(lines)) if close in lines[j]), None)
    return None if end is None else (k, end)


def box_align(tag: str, lines: list[str], i: int) -> str:
    r"""Which way the box holding line `i` lays its text out, in each form's own words: `m6-a` puts
    `\vss` where the slack goes (before the paragraphs = bottom, after = top, both = middle), `ls-a`
    names it as a box key. That is the way an added paragraph pushes the flow, so the judgement has
    to know it: a bottom-aligned box given one more line grows *upward*, as it does in Slides."""
    span = element_span(tag, lines, i)
    if span is None:
        return "top"
    text = "\n".join(lines[span[0]:span[1] + 1])
    if tag == NEW:
        opts = OPTS.search(text)
        keys = [k.strip() for k in (opts.group(1) if opts else "").split(",")]
        return next((k for k in ("bottom", "middle") if k in keys), "top")
    body = text.split("\\slidesbox", 1)[-1]
    first = PARSTART.search(body)
    head, rest = (body[:first.start()], body[first.start():]) if first else (body, "")
    above, below = "\\vss" in head, "\\vss" in rest
    return "middle" if above and below else "bottom" if above else "top"


def turned(lines: list[str], place: int) -> bool:
    r"""A box inside `\adoptturned` is placed by the turn as well: moving it alone means nothing."""
    return any("\\adoptturned" in lines[k] for k in range(max(0, place - 2), place + 1))


def para_block(tag: str, lines: list[str], i: int) -> tuple[int, int] | None:
    r"""The source lines that *are* the paragraph holding line `i`: one line in `ls-a` (an `\item` or
    a `\slidepar`), the `{\leftskip=...}` ... `\par}` group in `m6-a`."""
    if tag == NEW:
        return (i, i) if (ITEM.match(lines[i]) or SLIDEPAR.match(lines[i])) else None
    start = next((k for k in range(i, -1, -1) if PARSTART.search(lines[k])), None)
    end = next((k for k in range(i, len(lines)) if PAREND.search(lines[k])), None)
    return None if start is None or end is None else (start, end)


def leaf_item(lines: list[str], i: int) -> bool:
    r"""An `ls-a` item with no list of its own under it: one a person deletes by deleting its line."""
    for ln in lines[i + 1:]:
        if ln.strip():
            return not LISTSTART.match(ln)
    return True


def words_of(line: str) -> str:
    r"""What a paragraph line prints, and nothing else: the whole line in `m6-a`, the text after the
    macro in `ls-a` - `\slidepar`'s inside its braces, `\item`'s bare - so that replacing it with
    other words leaves the line's own syntax standing."""
    s = line.strip()
    m = ITEMWORDS.match(s)
    if m is None:
        return s
    rest = m.group(1)
    if rest.startswith("{") and group_at(rest, 0) == len(rest):
        return rest[1:-1]
    return rest


# ------------------------------------------------------------------------------------------- an edit

@dataclass
class Anchor:
    """One paragraph of one element, found in both forms by the words it prints."""
    text: str                              # the words, on exactly one clean line of each form
    kind: str                              # item | par | single
    bbox: list                             # the element's box, page pt, out of the IR
    title: str | None = None               # the slide's title, when both forms can take a new one
    title_bbox: list | None = None
    table_bbox: list | None = None         # a table both forms can take a row


@dataclass
class Applied:
    lines: list[str]
    touched: int                           # source lines a person adds, deletes or changes
    added: str | None = None               # words that must be on the page afterwards
    removed: str | None = None             # words that must not be
    where: str = "anchor"                  # the element the change belongs to: anchor|title|table
    grow: str = ""                         # content was added: the box's own alignment, or "" for none
    shift: float = 0.0                     # bp the box moves: the region reaches that much further


def ed_reword_longer(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    i = sole_line(lines, a.text)
    if i is None:
        return None
    new = longer(a.text)
    out = list(lines)
    out[i] = retext(out[i], a.text, new)
    return Applied(out, 1, added=" ".join(new.split()[len(a.text.split()):]))


def ed_reword_shorter(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    i = sole_line(lines, a.text)
    if i is None:
        return None
    new = shorter(a.text)
    out = list(lines)
    out[i] = retext(out[i], a.text, new)
    return Applied(out, 1, added=new, removed=" ".join(a.text.split()[-4:]))


def ed_restyle_run(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    i = sole_line(lines, a.text)
    if i is None:
        return None
    words = a.text.split()
    out = list(lines)
    out[i] = retext(out[i], a.text, "\\textbf{" + " ".join(words[:2]) + "} " + " ".join(words[2:]))
    return Applied(out, 1)


def ed_change_title(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    if not a.title or not a.title_bbox:
        return None
    new = like(a.title, TITLE_WORDS)
    out = list(lines)
    i = sole_line(lines, "\\frametitle{" + a.title + "}")       # ls-a: the frame's own title
    if i is None:
        i = sole_line(lines, a.title)
    if i is None:
        return None
    out[i] = retext(out[i], a.title, new)
    return Applied(out, 1, added=new, removed=a.title, where="title")


def ed_move_box(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    i = sole_line(lines, a.text)
    if i is None:
        return None
    found = place_line(tag, lines, i)
    if found is None:
        return None
    k, pat = found
    if turned(lines, k):
        return None
    m = pat.search(lines[k])
    out = list(lines)
    out[k] = lines[k][:m.start(1)] + f"{float(m.group(1)) + SHIFT:.2f}" + lines[k][m.end(1):]
    return Applied(out, 1, shift=SHIFT)


def copy_paragraph(tag: str, lines: list[str], i: int, words: str) -> Applied | None:
    """One more paragraph like the one on line `i`, straight after it, saying `words`."""
    block = para_block(tag, lines, i)
    if block is None:
        return None
    start, end = block
    if not start <= i <= end:
        return None
    copy = list(lines[start:end + 1])
    copy[i - start] = retext(copy[i - start], words_of(lines[i]), words)
    if copy[i - start] == lines[i]:
        return None
    return Applied(lines[:end + 1] + copy + lines[end + 1:], len(copy), added=words,
                   grow=box_align(tag, lines, i))


def ed_add_item(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    if a.kind != "item":
        return None
    i = sole_line(lines, a.text)
    if i is None or (tag == NEW and not leaf_item(lines, i)):
        return None
    return copy_paragraph(tag, lines, i, like(a.text, LINE_WORDS))


# \slidetext's options that belong to its box and not to its paragraph - slides.sty's own split
BOX_KEYS = ("top", "middle", "bottom", "inset", "tail")
SLIDETEXT = re.compile(r"^(\s*)\\slidetext(\[[^\]]*\])?\{([^}]*)\}\{([^}]*)\}\{")


def group_at(line: str, k: int) -> int | None:
    """The index just past the brace group opening at `line[k]`."""
    depth = 0
    for j in range(k, len(line)):
        depth += (line[j] == "{") - (line[j] == "}")
        if depth == 0:
            return j + 1
    return None


def ed_add_paragraph(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    """One more paragraph in the box. In `m6-a` every box is a `\\vbox` of paragraph groups, so it
    is one more group; in `ls-a` a box that says one thing is a `\\slidetext`, which has no room for
    a second paragraph, so it has to become the `slidebox` it is short for - the same four lines."""
    if a.kind not in ("par", "single"):
        return None
    i = sole_line(lines, a.text)
    if i is None:
        return None
    words = like(a.text, LINE_WORDS)
    if tag == OLD or a.kind == "par":
        return copy_paragraph(tag, lines, i, words)
    m = SLIDETEXT.match(lines[i])
    end = group_at(lines[i], m.end() - 1) if m else None
    if m is None or end is None or lines[i][end:].strip() or turned(lines, i):
        return None
    opts = [o.strip() for o in (m.group(2) or "[]")[1:-1].split(",") if o.strip()]
    box = [o for o in opts if o.split("=")[0].strip() in BOX_KEYS]
    par = ", ".join([f"style={m.group(4)}"] + [o for o in opts if o not in box])
    pad, body = m.group(1), lines[i][m.end():end - 1]
    out = (lines[:i]
           + [f"{pad}\\begin{{slidebox}}[{', '.join(box)}]{{{m.group(3)}}}",
              f"{pad}  \\slidepar[{par}]{{{body}}}",
              f"{pad}  \\slidepar[{par}]{{{words}}}",
              f"{pad}\\end{{slidebox}}"]
           + lines[i + 1:])
    return Applied(out, 4, added=words, grow=box_align(tag, lines, i))


def ed_delete_item(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    if a.kind != "item":
        return None
    i = sole_line(lines, a.text)
    if i is None or (tag == NEW and not leaf_item(lines, i)):
        return None
    block = para_block(tag, lines, i)
    if block is None:
        return None
    start, end = block
    return Applied(lines[:start] + lines[end + 1:], end - start + 1,
                   removed=" ".join(a.text.split()[:5]))


# ------------------------------------------------------------------------------------ a table's row

ADOPTROW = re.compile(r"^(\s*)\\adoptrow\{(\d+)\}\{([^}]*)\}")
ADOPTFIX = re.compile(r"^(\s*)\\adoptfix\{(\d+)\}")
ADOPTCELL = re.compile(r"^(\s*)\\adoptcell\{(\d+)\}\{(\d+)\}\{(\d+)\}"
                       r"\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{%\s*$")
ADOPTTOPS = re.compile(r"^(\s*)\\adopttops\{(\d+)\}")
ADOPTNODE = re.compile(r"^\s*\\node\[anchor=.*\\adoptbox\{(\d+)\}\};\s*$")
ADOPTY = re.compile(r"\\adopty\{(\d+)\}")
TABLEBOX = re.compile(r"^\\begin\{slidetable\}(\[[^\]]*\])?\{([^}]*)\}\{([^}]*)\}")
ROWEND = re.compile(r"\\\\\s*$")
TABLEBOUND = re.compile(r"use as bounding box|useasboundingbox")


def ed_add_table_row(tag: str, lines: list[str], a: Anchor) -> Applied | None:
    if not a.table_bbox:
        return None
    got = _new_row_ls(lines) if tag == NEW else _new_row_m6(lines)
    if got is not None:
        got.where, got.grow = "table", "top"      # a table's rows are drawn downward from its top
    return got


def _new_row_ls(lines: list[str]) -> Applied | None:
    r"""One more row at the end of the first `slidetable`: the environment splits its body on `\\`,
    so a row is a line."""
    start = next((k for k, ln in enumerate(lines) if "\\begin{slidetable}" in ln), None)
    end = next((k for k, ln in enumerate(lines) if "\\end{slidetable}" in ln), None)
    if start is None or end is None or end < start:
        return None
    rows = [k for k in range(start + 1, end) if ROWEND.search(lines[k])]
    if not rows:
        return None
    cells = lines[rows[-1]].count("&") + 1
    body = " & ".join([CELL_WORDS[k] if k < len(CELL_WORDS) else "" for k in range(cells)])
    indent = re.match(r"\s*", lines[rows[-1]]).group(0)
    row = f"{indent}{body} \\\\"
    return Applied(lines[:rows[-1] + 1] + [row] + lines[rows[-1] + 1:], 1, added=CELL_WORDS[0])


def _cell_end(lines: list[str], k: int) -> int:
    r"""The last line of the `\adoptcell` opening on line `k` (its braces close)."""
    depth = 0
    for j in range(k, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if depth <= 0:
            return j
    return k


def _new_row_m6(lines: list[str]) -> Applied | None:
    r"""One more row at the end of the first `\adoptrow` table. A row there is a height, a fix, a
    cell box per column, the tops count, the bounding box, the foot rule, the far end of every
    vertical rule, and a node per cell."""
    rows = [(k, m) for k, ln in enumerate(lines) if (m := ADOPTROW.match(ln))]
    tops = next(((k, m) for k, ln in enumerate(lines) if (m := ADOPTTOPS.match(ln))), None)
    cells = [(k, m) for k, ln in enumerate(lines) if (m := ADOPTCELL.match(ln))]
    nodes = [(k, m) for k, ln in enumerate(lines) if (m := ADOPTNODE.match(ln))]
    if not rows or tops is None or not cells or not nodes:
        return None
    n = max(int(m.group(2)) for _, m in rows) + 1          # rows 0..n-1; the foot is \adopty{n}
    if int(tops[1].group(2)) != n:
        return None
    serial = max(int(m.group(2)) for _, m in cells)
    last = max(int(m.group(4)) for _, m in cells)          # the row the last cells sit in
    mine = [(k, m) for k, m in cells if int(m.group(4)) == last]
    node_of = {int(m.group(1)): k for k, m in nodes}
    if any(int(m.group(2)) not in node_of for _, m in mine):
        return None
    shift = n - last                                       # the new row stands `shift` rows lower

    new_cells, new_nodes = [], []
    for c, (k, m) in enumerate(mine):
        me, old = serial + c + 1, m.group(2)
        text = CELL_WORDS[c] if c < len(CELL_WORDS) else ""
        style = lines[k + 1] if k + 1 <= _cell_end(lines, k) else ""
        new_cells += [f"{m.group(1)}\\adoptcell{{{me}}}{{{n}}}{{{n}}}{{{m.group(5)}}}{{{m.group(6)}}}"
                      f"{{{m.group(7)}}}{{{m.group(8)}}}{{%", style, f"{m.group(1)}  {text}}}"]
        # the node the last row's cell has, one row lower and naming the new box
        node = ADOPTY.sub(lambda mm: f"\\adopty{{{int(mm.group(1)) + shift}}}", lines[node_of[int(old)]])
        for what in ("adoptbox", "adoptht", "adoptdrop"):
            node = node.replace(f"\\{what}{{{old}}}", f"\\{what}{{{me}}}")
        new_nodes.append(node)

    out, touched = list(lines), 0
    # what named the old foot names the new one: the tops count, the bounding box, the verticals
    for k, ln in enumerate(out):
        if ADOPTTOPS.match(ln):
            out[k] = ADOPTTOPS.sub(lambda mm: f"{mm.group(1)}\\adopttops{{{n + 1}}}", ln)
            touched += 1
        elif f"\\adopty{{{n}}}" in ln and (TABLEBOUND.search(ln)
                                           or ("\\draw" in ln and ADOPTY.findall(ln) == ["0", str(n)])):
            out[k] = ln.replace(f"\\adopty{{{n}}}", f"\\adopty{{{n + 1}}}")
            touched += 1
    fixes = [(k, m) for k, ln in enumerate(lines) if (m := ADOPTFIX.match(ln))]
    foot = next((k for k in range(len(lines) - 1, -1, -1)
                 if "\\draw" in lines[k] and ADOPTY.findall(lines[k]) == [str(n), str(n)]), None)
    inserts = [(rows[-1][0] + 1, [f"{rows[-1][1].group(1)}\\adoptrow{{{n}}}{{{rows[-1][1].group(3)}}}"]),
               (_cell_end(lines, cells[-1][0]) + 1, new_cells),
               (nodes[-1][0] + 1, new_nodes)]
    if fixes:
        inserts.append((fixes[-1][0] + 1, [f"{fixes[-1][1].group(1)}\\adoptfix{{{n}}}"]))
    if foot is not None:
        inserts.append((foot + 1, [ADOPTY.sub(lambda mm: f"\\adopty{{{n + 1}}}", lines[foot])]))
    for at, block in sorted(inserts, key=lambda p: -p[0]):
        out = out[:at] + block + out[at:]
        touched += len(block)
    return Applied(out, touched, added=CELL_WORDS[0])


EDITS = {
    "reword-longer": ed_reword_longer,
    "reword-shorter": ed_reword_shorter,
    "restyle-run": ed_restyle_run,
    "change-title": ed_change_title,
    "move-box": ed_move_box,
    "add-item": ed_add_item,
    "delete-item": ed_delete_item,
    "add-paragraph": ed_add_paragraph,
    "add-table-row": ed_add_table_row,
}


# ------------------------------------------------------------------------------------ the judgement

TEX_ERROR = re.compile(r"^(?:.*?:\d+:\s*|!\s*)(.+)$")


def compile_verdict(pdf: Path | None, err: str) -> tuple[bool, str]:
    """Did the frame compile, and if not, what did TeX say? (`err` is `Workspace.compile`'s excerpt.)"""
    if pdf is not None:
        return True, ""
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("!") or re.match(r"^.*?:\d+:\s*\S", line):
            m = TEX_ERROR.match(line)
            if m and m.group(1).strip():
                return False, m.group(1).strip()[:120]
    tail = [ln for ln in err.strip().splitlines() if ln.strip()]
    return False, (tail[-1][:120] if tail else "no output")


def on_page(a: Anchor, size: list | None, page: tuple[float, float]) -> Anchor:
    """The IR's boxes in the page's own units. Usually the two agree, but a deck whose slide size is
    not a paper size beamer knows (poster-48x36: 362.8 x 272.1 in the IR, 1728 x 1296 bp on paper)
    is written at the paper's scale, and an unscaled box would be judged against the wrong part of
    the page. The table's box is not touched: the source already says it in the page's units."""
    if not size or (abs(size[0] - page[0]) < 0.01 and abs(size[1] - page[1]) < 0.01):
        return a
    kx, ky = page[0] / size[0], page[1] / size[1]

    def fix(box):
        return None if not box else [box[0] * kx, box[1] * ky, box[2] * kx, box[3] * ky]

    return replace(a, bbox=fix(a.bbox), title_bbox=fix(a.title_bbox))


def box_mask(region: list, page: tuple[float, float], shape: tuple[int, int], grow: str = "",
             ink: tuple[int, int, int, int] | None = None, shift: float = 0.0) -> np.ndarray:
    """The room the edit is allowed, on the render's pixel grid: the element's box with PAD bp
    around it, together with `ink` - the pixels the element already covers on the unedited page,
    which can reach past its box, since Slides lets a text box's last line hang out of it and the
    forms reproduce that. With `grow` (the box's own alignment), the column as far as the edge the
    flow grows towards: a top-aligned box down to the page's foot, a bottom-aligned one up to its
    head, a middle-aligned one both ways - content that was added pushes the flow, never sideways.
    With `shift`, that much more room to the right (the box was moved there). So a breach is room
    the edit *took*, never room the slide already used."""
    h, w = shape
    sx, sy = w / page[0], h / page[1]
    x0, y0, x1, y1 = region
    a0, b0 = (x0 - PAD) * sx, (y0 - PAD) * sy
    a1, b1 = (x1 + PAD) * sx, (y1 + PAD) * sy
    if ink is not None:
        i0, j0, i1, j1 = ink
        a0, b0, a1, b1 = min(a0, i0 - 1), min(b0, j0 - 1), max(a1, i1 + 1), max(b1, j1 + 1)
    a1 += shift * sx
    if grow in ("top", "middle"):
        b1 = h
    if grow in ("bottom", "middle"):
        b0 = 0.0
    m = np.zeros((h, w), dtype=bool)
    m[max(0, int(b0)):min(h, int(np.ceil(b1))), max(0, int(a0)):min(w, int(np.ceil(a1)))] = True
    return m


def ink_hull(base: np.ndarray, without: np.ndarray | None) -> tuple[int, int, int, int] | None:
    """The pixels one element covers: the unedited page against the same page compiled without that
    element. Both forms place every box absolutely, so taking one out moves nothing else."""
    if without is None or without.shape != base.shape:
        return None
    d = np.abs(base.astype(np.int16) - without.astype(np.int16)).max(axis=2) >= DIFF_LEVEL
    ys, xs = np.nonzero(d)
    return None if not len(ys) else (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def judge_pixels(before: np.ndarray, after: np.ndarray, region: np.ndarray) -> dict:
    """What changed, and whether it stayed in the box. `breach` names the sides it left by."""
    if before.shape != after.shape:
        return {"changed": -1, "outside": -1, "confined": False, "visible": True, "breach": ["size"]}
    diff = np.abs(before.astype(np.int16) - after.astype(np.int16)).max(axis=2) >= DIFF_LEVEL
    out = diff & ~region
    n_out = int(out.sum())
    breach: list[str] = []
    if n_out > BREACH_PX:
        ys, xs = np.nonzero(out)
        ry, rx = np.nonzero(region)
        if not len(ry):
            breach = ["nowhere"]
        else:
            breach = [side for side, hit in (("right", xs.max() > rx.max()), ("left", xs.min() < rx.min()),
                                             ("bottom", ys.max() > ry.max()), ("top", ys.min() < ry.min()))
                      if hit] or ["beside"]
    return {"changed": int(diff.sum()), "outside": n_out, "confined": n_out <= BREACH_PX,
            "visible": int(diff.sum()) >= MIN_CHANGE_PX, "breach": breach}


def squash(text: str) -> str:
    """The page's text with its spaces out: TeX draws word gaps as kerns, not as space characters."""
    return "".join(text.split())


def judge_text(text: str, base: str, added: str | None, removed: str | None) -> tuple[bool, str]:
    """The words the edit writes must be on the page and the ones it takes away must not - each
    checked only where the baseline page makes the answer mean something."""
    now, was = squash(text), squash(base)
    if added and squash(added) not in was and squash(added) not in now:
        return False, "the new words are not on the page"
    if removed and was.count(squash(removed)) == 1 and squash(removed) in now:
        return False, "the old words are still on the page"
    return True, ""


def verdict(res: dict) -> str:
    if not res.get("compiled"):
        return "compile"
    if not res.get("text_ok", True) or not res.get("visible"):
        return "invisible"
    if not res.get("confined"):
        return "breach"
    return "pass"


# --------------------------------------------------------------------------------------- the sample

def slide_category(slide: dict) -> str | None:
    els = slide["elements"]
    page = slide["size"][0] * slide["size"][1]

    def area(e):
        x0, y0, x1, y1 = e["bbox"]
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    if any(e["kind"] == "table" for e in els):
        return "table"
    if sum(1 for e in els for p in e.get("paragraphs") or [] if p.get("bullet")) >= 3:
        return "list"
    if sum(1 for e in els if e["kind"] == "shape") >= 4:
        return "shape"
    if any(e["kind"] in ("image", "picture") and area(e) >= 0.12 * page for e in els):
        return "picture"
    sizes = [r.get("size") or 0 for e in els for p in e.get("paragraphs") or [] for r in p["runs"]]
    if len(els) <= 4 and sizes and max(sizes) >= 20:
        return "title"
    if any(len(e.get("paragraphs") or []) >= 2 for e in els):
        return "prose"
    return None


def title_of(slide: dict) -> tuple[str, list] | None:
    """The slide's title as both forms print it: the TITLE placeholder, else the biggest one-liner."""
    best, best_size = None, 0.0
    for e in slide["elements"]:
        paras = e.get("paragraphs") or []
        if e["kind"] != "text" or len(paras) != 1 or not paras[0]["runs"]:
            continue
        text = "".join(r["text"] for r in paras[0]["runs"]).strip()
        size = max((r.get("size") or 0) for r in paras[0]["runs"])
        if e.get("role") == "title" or e.get("placeholder") == "TITLE":
            size += 100
        if plain(text) and 8 <= len(text) <= 70 and size > best_size:
            best, best_size = (text, list(e["bbox"])), size
    return best


def table_of(slide: dict, frames: dict[str, list[str]]) -> list | None:
    """The box the first table is **drawn** in, when both forms wrote a table this tool can add a
    row to. Not the IR element's bbox: a Slides table sizes itself to its rows, so the box the API
    reports is not the table the deck shows (cs161-net slide 48: bbox 148.8 wide, table 269.3), and
    adopt measures the real one off the thumbnail. The source says it exactly - `slidetable`'s corner
    and column widths - and both forms draw the same table, pixel for pixel, so taking it from the
    newer form's header describes the older one's table just as well."""
    if not any(ADOPTROW.match(ln) for ln in frames[OLD]):
        return None
    m = next((m for ln in frames[NEW] if (m := TABLEBOX.match(ln.strip()))), None)
    if m is None:
        return None
    try:
        x, y = (float(v) for v in m.group(2).split(","))
        widths = [float(v) for v in m.group(3).split(",")]
    except ValueError:
        return None
    if not widths or not any(e["kind"] == "table" for e in slide["elements"]):
        return None
    return [x, y, x + sum(widths), y]


def anchor_of(slide: dict, frames: dict[str, list[str]]) -> Anchor | None:
    """The paragraph both forms will be edited on: a plain one that sits on exactly one clean line
    of each form and whose words name one element only. A list item wins, it takes the most edits."""
    owners: dict[str, list] = {}
    for e in slide["elements"]:
        if e["kind"] != "text":
            continue
        for p in e.get("paragraphs") or []:
            text = "".join(r["text"] for r in p["runs"]).strip()
            if plain(text) and 5 <= len(text.split()) and len(text) <= 110:
                owners.setdefault(text, []).append(e)
    best = None
    for text, es in sorted(owners.items(), key=lambda kv: -len(kv[0])):
        if len(es) != 1:
            continue
        spots = {tag: sole_line(lines, text) for tag, lines in frames.items()}
        if any(i is None for i in spots.values()):
            continue
        if not all(clean(frames[tag][i]) for tag, i in spots.items()):
            continue
        line = frames[NEW][spots[NEW]]
        kind = ("item" if ITEM.match(line) else "par" if SLIDEPAR.match(line)
                else "single" if "\\slidetext" in line else None)
        if kind is None:
            continue
        cand = Anchor(text=text, kind=kind, bbox=list(es[0]["bbox"]))
        if kind == "item" and leaf_item(frames[NEW], spots[NEW]):
            return cand                        # a leaf item takes every edit there is
        best = best or cand
    return best


def build_index(corpus: Path = CORPUS) -> list[dict]:
    """Every corpus slide whose two forms share an editable paragraph, with its category."""
    out = []
    for deck in sorted(p.name for p in corpus.iterdir() if (p / "target.json").exists()):
        try:
            frames = {tag: dict(split_frames((corpus / deck / "runs" / tag / "tree" / "main.tex")
                                             .read_text(encoding="utf-8"))[1]) for tag in FORMS}
        except OSError:
            continue
        target = json.loads((corpus / deck / "target.json").read_text(encoding="utf-8"))
        for n, slide in enumerate(target["slides"], 1):
            if not all(n in frames[tag] for tag in FORMS):
                continue
            cat = slide_category(slide)
            if cat is None:
                continue
            body = {tag: frames[tag][n].split("\n") for tag in FORMS}
            a = anchor_of(slide, body)
            if a is None:
                continue
            title = title_of(slide)
            if title and all(sole_line(body[tag], title[0]) is not None for tag in FORMS):
                a.title, a.title_bbox = title
            a.table_bbox = table_of(slide, body)
            out.append({"deck": deck, "slide": n, "category": cat, "size": list(slide["size"]),
                        "anchor": a.__dict__})
    return out


def both_forms(pick: dict, frames: dict[str, list[str]]) -> list[str]:
    """The edits that can be written in *both* forms on this slide. An edit only one form can take
    is no comparison, so it is left out of the sample rather than scored against the other."""
    a = Anchor(**pick["anchor"])
    out = []
    for name, fn in EDITS.items():
        try:
            if all(fn(tag, frames[tag], a) is not None for tag in FORMS):
                out.append(name)
        except Exception:                                                # noqa: BLE001
            pass
    return out


def frames_of(pick: dict, corpus: Path = CORPUS) -> dict[str, list[str]]:
    out = {}
    for tag in FORMS:
        text = (corpus / pick["deck"] / "runs" / tag / "tree" / "main.tex").read_text(encoding="utf-8")
        out[tag] = dict(split_frames(text)[1])[pick["slide"]].split("\n")
    return out


def pick_sample(index: list[dict], seed: int, n: int) -> list[dict]:
    """`n` slides, round-robin over the categories, every deck used once before any twice. Pure: the
    same index and seed always give the same slides."""
    rng = random.Random(seed)
    pools: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for row in sorted(index, key=lambda r: (r["deck"], r["slide"])):
        pools.setdefault(row["category"], []).append(row)
    for c in pools:
        rng.shuffle(pools[c])
    out: list[dict] = []
    used: dict[str, int] = {}
    for turn in range(1000):
        if len(out) >= n:
            break
        took = False
        for c in CATEGORIES:
            if len(out) >= n:
                break
            pool = pools.get(c) or []
            k = next((j for j, r in enumerate(pool) if used.get(r["deck"], 0) <= turn), None)
            if k is None:
                continue
            row = pool.pop(k)
            used[row["deck"]] = used.get(row["deck"], 0) + 1
            out.append(row)
            took = True
        if not took:
            break
    return out


# ------------------------------------------------------------------------------------------ the run

def render(pdf: Path) -> tuple[np.ndarray, str, int, tuple[float, float]]:
    from beamer2slides.pdf import Document
    doc = Document(pdf)
    try:
        page = doc[0]
        return (page.render(WIDTH_PX / page.width), "".join(c.c for c in page.chars()),
                len(doc), (page.width, page.height))
    finally:
        doc.close()


class Builder:
    """One copy of a source tree, compiled again and again with a different main.tex."""

    def __init__(self, tree: Path, work: Path):
        from beamer2slides.inverse import Workspace
        shutil.rmtree(work, ignore_errors=True)
        self.ws = Workspace(tree / "main.tex", work)

    def build(self, text: str) -> tuple[Path | None, str]:
        pdf = self.ws.build_dir / "main.pdf"
        pdf.unlink(missing_ok=True)
        self.ws.main.write_text(text, encoding="utf-8")
        out, err = self.ws.compile()
        return (out if out and out.exists() else None), err


def element_inks(builder: "Builder", head: str, tail: str, lines: list[str], tag: str, a: Anchor,
                 base: np.ndarray) -> dict:
    """What the anchor's element, and the title's, already cover on the unedited page: the frame
    compiled once without each of them. Two compiles per slide and form, and they make `confined`
    say what it means - the edit took room the element did not already have."""
    out: dict = {}
    for what, text in (("anchor", a.text), ("title", a.title)):
        i = None if not text else sole_line(lines, text)
        span = None if i is None else element_span(tag, lines, i)
        if span is None:
            continue
        pdf, err = builder.build(head + "\n".join(lines[:span[0]] + lines[span[1] + 1:]) + tail)
        if pdf is None:
            continue
        got, _, _, _ = render(pdf)
        out[what] = ink_hull(base, got)
    return out


def run_form(deck: str, tag: str, picks: list[dict], out: Path, corpus: Path = CORPUS) -> list[dict]:
    """Every sampled slide of one deck in one form: the frame alone, then the frame with each edit."""
    tree = corpus / deck / "runs" / tag / "tree"
    head, frames, tail = split_frames((tree / "main.tex").read_text(encoding="utf-8"))
    bodies = dict(frames)
    builder = Builder(tree, out / "work" / f"{deck}-{tag}")
    results = []
    for pick in picks:
        n = pick["slide"]
        a = Anchor(**pick["anchor"])
        lines = bodies[n].split("\n")
        pdf, err = builder.build(head + bodies[n] + tail)
        ok, why = compile_verdict(pdf, err)
        if not ok:
            results.append({"deck": deck, "tag": tag, "slide": n, "category": pick["category"],
                            "edit": "(baseline)", "compiled": False, "reason": why})
            continue
        base, base_text, pages, page = render(pdf)
        a = on_page(a, pick.get("size"), page)
        inks = element_inks(builder, head, tail, lines, tag, a, base)
        for name in pick.get("edits") or list(EDITS):
            fn = EDITS[name]
            row = {"deck": deck, "tag": tag, "slide": n, "category": pick["category"], "edit": name}
            try:
                applied = fn(tag, lines, a)
            except Exception as exc:                                     # noqa: BLE001
                applied, row["error"] = None, f"{type(exc).__name__}: {exc}"[:200]
            if applied is None:
                row["applies"] = False
                results.append(row)
                continue
            row["applies"], row["lines"] = True, applied.touched
            pdf2, err2 = builder.build(head + "\n".join(applied.lines) + tail)
            ok2, why2 = compile_verdict(pdf2, err2)
            row["compiled"] = ok2
            if not ok2:
                row["reason"], row["verdict"] = why2, "compile"
                results.append(row)
                continue
            got, text, pages2, _ = render(pdf2)
            box = {"anchor": a.bbox, "title": a.title_bbox, "table": a.table_bbox}[applied.where]
            region = box_mask(box, page, base.shape[:2], applied.grow,
                              inks.get(applied.where), applied.shift)
            row["where"] = applied.where
            row.update(judge_pixels(base, got, region))
            row["pages"] = pages2
            if pages2 != pages:
                row["confined"], row["breach"] = False, ["pages"]
            row["text_ok"], why3 = judge_text(text, base_text, applied.added, applied.removed)
            row["verdict"] = verdict(row)
            if row["verdict"] == "invisible":
                row["reason"] = why3 or "the page did not change"
            elif row["verdict"] == "breach":
                row["reason"] = "ink outside the box: " + ", ".join(row["breach"] or ["?"])
            results.append(row)
    return results


def _safe_run(deck: str, form: str, picks: list[dict], out: Path) -> list[dict]:
    try:
        return run_form(deck, form, picks, out)
    except Exception as exc:                                             # noqa: BLE001
        return [{"deck": deck, "tag": form, "edit": "(crash)", "error": f"{type(exc).__name__}: {exc}",
                 "traceback": traceback.format_exc()[-1200:]}]


def sample(seed: int, n: int) -> list[dict]:
    picks = pick_sample(build_index(), seed, n)
    for p in picks:
        p["edits"] = both_forms(p, frames_of(p))
    return picks


def run(seed: int, n: int, jobs: int, out: Path) -> None:
    picks = sample(seed, n)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sample.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
    by_deck: dict[str, list[dict]] = {}
    for p in picks:
        by_deck.setdefault(p["deck"], []).append(p)
    tasks = [(deck, form, rows) for deck, rows in sorted(by_deck.items()) for form in FORMS]
    rows: list[dict] = []
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max(1, min(jobs, len(tasks)))) as pool:
        futs = {pool.submit(_safe_run, deck, form, sub, out): (deck, form) for deck, form, sub in tasks}
        for f in as_completed(futs):
            deck, form = futs[f]
            got = f.result()
            rows += got
            print(f"{deck:<22} {form:<6} {len(got):3} rows  "
                  f"{sum(1 for r in got if r.get('verdict') == 'pass'):3} pass", flush=True)
    (out / "results.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"\n{len(rows)} rows in {time.perf_counter() - t0:.0f}s -> {out}\n")
    report(out)


# -------------------------------------------------------------------------------------- the report

def summary(rows: list[dict]) -> dict:
    """Pass rate and lines touched, per edit kind and per form. Pure, so a test can check it."""
    done = [r for r in rows if r.get("applies") and r.get("verdict")]
    out: dict = {"edits": {}, "forms": {}}
    for name in EDITS:
        mine = [r for r in done if r["edit"] == name]
        if not mine:
            continue
        cell = {}
        for form in FORMS:
            f = [r for r in mine if r["tag"] == form]
            cell[form] = {"n": len(f), "pass": sum(1 for r in f if r["verdict"] == "pass"),
                          "lines": sum(r.get("lines", 0) for r in f) / len(f) if f else 0.0}
        out["edits"][name] = cell
    for form in FORMS:
        f = [r for r in done if r["tag"] == form]
        out["forms"][form] = {"n": len(f), "pass": sum(1 for r in f if r["verdict"] == "pass"),
                              "lines": sum(r.get("lines", 0) for r in f)}
    return out


def report(out: Path) -> None:
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    s = summary(rows)
    print(f"{'edit':<16} {'slides':>6} | " + " | ".join(f"{f:^20}" for f in FORMS))
    for name, cell in s["edits"].items():
        parts = [f"{cell[f]['pass']:2}/{cell[f]['n']:<2} pass{cell[f]['lines']:7.1f} lines" for f in FORMS]
        print(f"{name:<16} {cell[FORMS[0]]['n']:>6} | " + " | ".join(parts))
    print()
    for form, c in s["forms"].items():
        print(f"{form}: {c['pass']}/{c['n']} pass ({100 * c['pass'] / max(c['n'], 1):.0f}%), "
              f"{c['lines']} lines over {c['n']} edits ({c['lines'] / max(c['n'], 1):.1f} each)")
    print("\nwhat failed:")
    for r in sorted((r for r in rows if r.get("verdict") and r["verdict"] != "pass"),
                    key=lambda r: (r["edit"], r["tag"], r["deck"])):
        print(f"  {r['tag']:<6} {r['edit']:<16} {r['deck']:<20} sl {r['slide']:<3} "
              f"{r['category']:<8} {r['verdict']:<9} {r.get('reason', '')[:70]}")
    for r in rows:
        if r.get("error") or r.get("edit") == "(baseline)":
            print(f"  ! {r.get('tag')} {r.get('deck')} {r.get('edit')} "
                  f"{r.get('error') or r.get('reason')}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sample", "run"):
        p = sub.add_parser(name)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--n", type=int, default=24)      # four slides of each category
        p.add_argument("--tag", default="a")
        if name == "run":
            p.add_argument("--jobs", type=int, default=5)
    r = sub.add_parser("report")
    r.add_argument("--tag", default="a")
    args = ap.parse_args(argv)
    out = CHECKOUT / "out" / "edit-robustness" / args.tag
    if args.cmd == "sample":
        picks = sample(args.seed, args.n)
        for p in picks:
            a = p["anchor"]
            print(f"{p['deck']:<22} {p['slide']:>3} {p['category']:<8} {a['kind']:<7} "
                  f"{len(p['edits'])} edits  {a['text'][:40]!r}")
        print(f"\n{len(picks)} slides: "
              + ", ".join(f"{c} {sum(1 for p in picks if p['category'] == c)}" for c in CATEGORIES))
        print("edits: " + ", ".join(f"{e} {sum(1 for p in picks if e in p['edits'])}" for e in EDITS))
    elif args.cmd == "run":
        run(args.seed, args.n, args.jobs, out)
    else:
        report(out)


if __name__ == "__main__":
    main()
