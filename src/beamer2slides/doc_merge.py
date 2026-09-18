"""Three-way merge for Google Docs, and the `batchUpdate` edits it plans. Pure.

The same bargain as `merge.py`: base is what the converter last pushed (and read
back), *ours* is the canonical HTML as it stands now, *theirs* is the live
document. Where both moved, the document wins, and the clash is reported.

Two things are different from the Slides side, and both come from measurement
(docs/google-docs.md):

* A push after the first can never be a re-import — `files.update` destroys every
  named range — so everything here plans **incremental edits** against the live
  document's own index space.
* A document holds objects no import can create: chips, equations, dropdowns,
  a table of contents. They read back as `frozen` runs, and the rule is absolute:
  **a frozen run is never rewritten**. A block whose frozen runs differ between
  the merge and the document is left alone and reported, so a chip a human put
  there outlives every sync.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from . import doc_ir
from .merge import diff3, tokens

# One character standing for a frozen run while text is diffed, so a merge can move
# words around it but never through it. U+FFFC is the object replacement character.
FROZEN = "￼"
BLOCK_MATCH = 0.5  # least similarity for an unkeyed block to inherit a key
BULLETS = {False: "BULLET_DISC_CIRCLE_SQUARE", True: "NUMBERED_DECIMAL_ALPHA_ROMAN"}
# The textStyle fields this merge owns: named on a restyle whether or not the run
# carries them, so that a mark the source took away is taken away in the document.
MANAGED = ("backgroundColor", "bold", "foregroundColor", "italic", "link",
           "strikethrough", "underline")
# What is written first when two edits are planned at one and the same index
# (`requests` says why each one sits where it does).
APPEND, DELETE, EDIT, BEFORE = 0, 1, 2, 3


# ---------------------------------------------------------------- block text

def block_text(block: dict) -> str:
    """A block's text as the merge sees it: frozen runs are one opaque character."""
    return "".join(FROZEN if r.get("frozen") else r["text"] for r in block.get("runs", []))


def frozen_of(block: dict) -> tuple:
    """What the frozen runs are, in order. Two blocks may only be merged if equal."""
    return tuple((r["chip"], r.get("text", ""), r.get("value", ""))
                 for r in block.get("runs", []) if r.get("frozen"))


def restore_unreadable(live: dict, *sources: dict) -> dict:
    """Fill in what a read cannot see, from the files that can.

    The mirror image of the frozen runs. A chip is content the file cannot carry, so
    the document wins; a list's ordered-ness is content the *document* cannot report
    once Drive's importer has built it (`doc_ir._ordered`), so the file wins — for as
    long as the block lives. A list the document itself made reads properly and never
    reaches here, and a block nobody knows falls back to bullets.
    """
    known = {}
    for source in sources:
        for block in source["blocks"]:
            if block.get("key") and block.get("ordered") is not None:
                known.setdefault(block["key"], block["ordered"])
    for block in live["blocks"]:
        if block.get("kind") == "item" and block.get("ordered") is None:
            block["ordered"] = bool(known.get(block.get("key"), False))
    return live


def styles_of(block: dict) -> tuple:
    """A block's styling, run by run, without its words.

    What changes when somebody marks a word bold or takes a colour away — and not
    when somebody rewrites a word, which is the text merge's business. A mark applied
    to part of a run splits it, so the run count carries the boundaries.
    """
    return tuple(("chip" if r.get("frozen") else "text",
                  frozenset((k, v) for k, v in r.items()
                            if k not in ("text", "width", "frozen", "chip", "value",
                                         "format", "locale", "mime")))
                 for r in block.get("runs", []))


def _shape(block: dict) -> tuple:
    """The part of a block that is not its words."""
    return (block["kind"], block.get("level"), block.get("ordered"), block.get("align"))


def _match_shape(block: dict) -> tuple:
    """What two blocks must share to be the same block.

    Ordered-ness is left out on purpose: an imported list cannot say whether it is
    numbered (`doc_ir._ordered`), so a document's list item would never match the
    file's, and the keys would be lost on the very first read-back.
    """
    return (block["kind"], block.get("level"), block.get("align"))


# ---------------------------------------------------------------- keys

def inherit_keys(base: dict, ours: dict) -> dict:
    """Give `ours` the keys of the base blocks it matches, so edits line up.

    Exact text first, then a similarity pass over what is left — `identity.py`'s
    rule, one dimension smaller because a document is a list, not a grid.
    """
    doc_ir.key_blocks(ours)
    free = [b for b in base["blocks"] if b.get("key")]
    taken: set[str] = set()
    by_text: dict[tuple, list] = {}
    for block in free:
        by_text.setdefault((_match_shape(block), block_text(block)), []).append(block)
    pending = []
    for block in ours["blocks"]:
        same = by_text.get((_match_shape(block), block_text(block)))
        if same:
            block["key"] = same.pop(0)["key"]
            taken.add(block["key"])
        else:
            pending.append(block)
    rest = [b for b in free if b["key"] not in taken]
    for block in pending:
        best, score = None, BLOCK_MATCH
        for candidate in rest:
            if _match_shape(candidate) != _match_shape(block):
                continue
            ratio = SequenceMatcher(None, block_text(candidate), block_text(block)).ratio()
            if ratio > score:
                best, score = candidate, ratio
        if best is not None:
            block["key"] = best["key"]
            rest.remove(best)
    return ours


# ---------------------------------------------------------------- merge

def merge(base: dict, ours: dict, theirs: dict) -> dict:
    """Merge the three sides. Returns the merged blocks, conflicts and notes.

    The merged list follows the *document's* order — a reader who moved a
    paragraph meant it — with blocks the source added spliced in where the source
    put them.
    """
    base_by = {b["key"]: b for b in base["blocks"] if b.get("key")}
    ours_by = {b["key"]: b for b in ours["blocks"] if b.get("key")}
    theirs_by = {b["key"]: b for b in theirs["blocks"] if b.get("key")}
    conflicts: list[dict] = []
    notes: list[str] = []
    merged: list[dict] = []

    for block in theirs["blocks"]:
        key = block.get("key")
        if key is None:
            merged.append(dict(block) | {"origin": "added in the document"})
            continue
        mine, was = ours_by.get(key), base_by.get(key)
        if was is None:
            merged.append(dict(block) | {"origin": "unknown to the base"})
            continue
        if mine is None:
            # The source dropped it. A document edit outranks that.
            if block_text(block) != block_text(was):
                notes.append(f"{key}: dropped by the source but edited in the document — kept")
                merged.append(dict(block) | {"origin": "kept over a source delete"})
            continue
        merged.append(_merge_block(was, mine, block, conflicts, notes))

    for index, block in enumerate(ours["blocks"]):
        key = block.get("key")
        if key in theirs_by or (key in base_by and key not in theirs_by):
            continue  # already placed, or deleted in the document: the delete stands
        # Whatever span this block had is an index into a different document: drop it,
        # and let `origin` be what says this one has to be written from nothing.
        fresh = {k: v for k, v in block.items() if k != "span"}
        merged.insert(_place(ours, theirs, merged, index),
                      fresh | {"origin": "added by the source"})
    return {"blocks": merged, "conflicts": conflicts, "notes": notes}


def _place(ours: dict, theirs: dict, merged: list, index: int) -> int:
    """Where a source-added block goes: after the merged block that precedes it in ours."""
    for before in reversed(ours["blocks"][:index]):
        at = next((i for i, b in enumerate(merged) if b.get("key") == before.get("key")), None)
        if at is not None:
            return at + 1
    return 0


def _merge_block(was: dict, mine: dict, live: dict, conflicts: list, notes: list) -> dict:
    key = live.get("key") or "a table cell"
    out = dict(live)
    if live.get("kind") == "table":
        return _merge_table(was, mine, live, conflicts, notes)
    if frozen_of(mine) != frozen_of(live):
        notes.append(f"{key}: the source would rewrite a chip or equation — left alone")
        return out | {"origin": "frozen content differs"}
    text, clashes = diff3(block_text(was), block_text(mine), block_text(live))
    for clash in clashes:
        conflicts.append(dict(clash) | {"key": key})
    if _shape(mine) != _shape(was) and _shape(live) == _shape(was):
        out |= {k: v for k, v in mine.items() if k in ("kind", "level", "ordered", "align")}
    if text != block_text(live):
        out["runs"] = _retext(live, text)
        out["origin"] = "merged"
    # Styling is merged the same way the words are, one step coarser: the source's
    # marks are taken when the source changed them and the document left the block
    # alone. A document that touched the block at all keeps its own styling, because
    # matching the source's marks onto words the document rewrote would be a guess.
    if styles_of(mine) != styles_of(was) and styles_of(live) == styles_of(was):
        if text == block_text(mine):
            out["runs"] = _restyled(live, mine)
            out["restyle"] = True
            out["origin"] = "merged"
        else:
            notes.append(f"{key}: the source restyled words the document rewrote — styling left alone")
    if not out.get("origin") and (block_text(live) != block_text(was)
                                  or styles_of(live) != styles_of(was)):
        # Nothing to write: the document says this, and the merge agrees. It is said
        # out loud all the same, so a sync report shows what the reader's side kept.
        out["origin"] = "kept from the document"
    return out


def _merge_table(was: dict, mine: dict, live: dict, conflicts: list, notes: list) -> dict:
    """A table merges cell by cell; its grid is the document's.

    Inside a table a block's identity is its place — row, column, and how far down the
    cell — not a named range, because a cell's paragraph cannot be deleted or moved
    without changing the grid. A source that changed the grid is left alone: adding a
    row or a column is a structural edit this merge does not attempt.
    """
    key = live.get("key") or "a table"
    out = dict(live)
    if _grid(mine) != _grid(live) or _grid(was) != _grid(live):
        notes.append(f"{key}: the table's rows and columns differ between the sides — left alone")
        return out | {"origin": "table grid differs"}
    rows = []
    for r, row in enumerate(live.get("rows", [])):
        cells = []
        for c, cell in enumerate(row):
            merged = []
            for i, block in enumerate(cell):
                was_cell, my_cell = _at(was, r, c, i), _at(mine, r, c, i)
                merged.append(dict(block) if was_cell is None or my_cell is None
                              else _merge_block(was_cell, my_cell, block, conflicts, notes))
            cells.append(merged)
        rows.append(cells)
    # A table has no words of its own, so what its cells did is what it did: the
    # report and the write side both ask the table, not the blocks inside it.
    inside = {b.get("origin") for row in rows for cell in row for b in cell}
    return out | {"rows": rows} | ({"origin": "merged"} if "merged" in inside
                                   else {"origin": "kept from the document"}
                                   if "kept from the document" in inside else {})


def _grid(block: dict) -> tuple:
    """How many cells each row has, and how many blocks each cell holds."""
    return tuple(tuple(len(cell) for cell in row) for row in block.get("rows", []))


def _at(block: dict, row: int, cell: int, index: int) -> dict | None:
    try:
        return block["rows"][row][cell][index]
    except (KeyError, IndexError):
        return None


def _restyled(live: dict, mine: dict) -> list[dict]:
    """The source's runs, with the document's frozen runs put back where they were."""
    frozen = iter([r for r in live.get("runs", []) if r.get("frozen")])
    out = []
    for run in mine.get("runs", []):
        if run.get("frozen"):
            kept = next(frozen, None)
            out.append(dict(kept if kept is not None else run))
        else:
            out.append(dict(run) | {"width": doc_ir.utf16_len(run["text"])})
    return out


def _retext(live: dict, text: str) -> list[dict]:
    """Put merged text back into the live block's runs, frozen runs untouched.

    The styled pieces keep their share by following the frozen runs: text between
    two frozen runs stays in the first writable run of that stretch, which is the
    honest thing a word-level merge can promise.
    """
    pieces = text.split(FROZEN)
    runs, piece = [], iter(pieces)
    stretch = next(piece, "")
    writable_seen = False
    for run in live.get("runs", []):
        if run.get("frozen"):
            runs.append(dict(run))
            stretch, writable_seen = next(piece, ""), False
            continue
        if writable_seen:
            continue  # its words were folded into the first run of this stretch
        runs.append(dict(run) | {"text": stretch, "width": doc_ir.utf16_len(stretch)})
        writable_seen = True
    if not writable_seen and stretch:
        runs.append({"text": stretch, "width": doc_ir.utf16_len(stretch)})
    return [r for r in runs if r.get("frozen") or r["text"]]


# ---------------------------------------------------------------- edits

def _positions(block: dict) -> list[int]:
    """Document index of every character of `block_text`, plus the end."""
    at = block["span"][0]
    out = []
    for run in block.get("runs", []):
        if run.get("frozen"):
            out.append(at)
            at += run.get("width", 1)
        else:
            for char in run["text"]:
                out.append(at)
                at += doc_ir.utf16_len(char)
    out.append(at)
    return out


def text_requests(live: dict, target: str) -> list[dict]:
    """deleteContentRange / insertText turning a live block's text into `target`.

    Back to front, so the indices of the edits still to come stay valid, and never
    across a frozen run: those characters are equal on both sides by construction,
    so the diff never puts a hunk through one.
    """
    current = block_text(live)
    if current == target:
        return []
    at = _positions(live)
    a, b = tokens(current), tokens(target)
    offsets = [0]
    for token in a:
        offsets.append(offsets[-1] + len(token))
    out = []
    for op, i1, i2, j1, j2 in reversed(SequenceMatcher(None, a, b, autojunk=False).get_opcodes()):
        if op == "equal":
            continue
        start, end = at[offsets[i1]], at[offsets[i2]]
        insert = "".join(b[j1:j2])
        if FROZEN in current[offsets[i1]:offsets[i2]] or FROZEN in insert:
            continue  # never rewrite a chip
        # The new words go in at the *end* of the hunk, before the old ones leave.
        # Docs gives inserted text the style of the character in front of it, so
        # inserting there makes the replacement inherit from the last word it
        # replaces — a word rewritten inside a bold run or a link stays inside it —
        # while inserting at the start would inherit from whatever came before the
        # hunk. Deleting afterwards is safe: the insert moved nothing below `end`.
        if insert:
            out.append({"insertText": {"location": {"index": end}, "text": insert}})
        if end > start:
            out.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
    return out


def _width(block: dict) -> int:
    """How many index units a block's text holds, the paragraph mark apart."""
    return sum(run.get("width", doc_ir.utf16_len(run["text"])) for run in block.get("runs", []))


def _paragraph_requests(start: int, end: int, block: dict, was_item: bool) -> list[dict]:
    """The paragraph's kind, alignment and bullet, in the only order that works."""
    out: list[dict] = []
    if block["kind"] != "item" and was_item:
        # Text inserted at the start of a list item joins that item, bullet and all;
        # and a block the source turned back into a paragraph must lose its glyph.
        out.append({"deleteParagraphBullets": {"range": {"startIndex": start, "endIndex": end}}})
    style = {"namedStyleType": doc_ir.NAMED_STYLE[block.get("level", 0)
                                                  if block["kind"] == "heading" else 0],
             "alignment": doc_ir.TO_ALIGNMENT[block.get("align") or "left"]}
    out.append({"updateParagraphStyle": {
        "range": {"startIndex": start, "endIndex": end}, "paragraphStyle": style,
        "fields": "namedStyleType,alignment"}})
    if block["kind"] == "item":
        # Bullets last: a style request covering the whole paragraph would restyle
        # the glyph too, the same trap as the Slides pipeline's createParagraphBullets.
        out.append({"createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end},
            "bulletPreset": BULLETS[bool(block.get("ordered"))]}})
    return out


def _run_requests(start: int, block: dict, reset: bool = False) -> list[dict]:
    """`updateTextStyle` per run of a block laid out from `start`.

    `reset` is for a block that already exists: the fields are named whether or not
    the run carries them, so a mark the source took away is taken away in the
    document too. The font is only named when one of the sides asks for the code
    face — resetting `weightedFontFamily` on every sync would undo a font the
    person chose in the document, which is not ours to touch.
    """
    fonts = any(run.get("code") for run in block.get("runs", []))
    out, at = [], start
    for run in block.get("runs", []):
        width = run.get("width", doc_ir.utf16_len(run["text"]))
        marks = _text_style(run)
        if width and not run.get("frozen") and (marks or reset):
            fields = sorted(set(marks) | (set(MANAGED + (("weightedFontFamily",) if fonts else ()))
                                          if reset else set()))
            out.append({"updateTextStyle": {
                "range": {"startIndex": at, "endIndex": at + width},
                "textStyle": marks, "fields": ",".join(fields)}})
        at += width
    return out


def _bullets_last(paragraph: list[dict], runs: list[dict]) -> list[dict]:
    """The run styling goes between the paragraph's style and its bullet, always."""
    made = [r for r in paragraph if "createParagraphBullets" in r]
    return [r for r in paragraph if "createParagraphBullets" not in r] + runs + made


def _style_requests(start: int, block: dict) -> list[dict]:
    """Everything but the words, for a block written at `start` from nothing."""
    end = start + _width(block) + 1
    return _bullets_last(_paragraph_requests(start, end, block, was_item=True),
                         _run_requests(start, block))


def _restyle_requests(live: dict, want: dict) -> list[dict]:
    """What to write when the merge changed a block's styling rather than its words.

    Planned against the block's own start and sent after that block's text edits —
    which is why the two live in one plan: by then the block says what the merge
    says, and the runs line up.
    """
    start = live["span"][0]
    end = start + _width(want) + 1
    paragraph = (_paragraph_requests(start, end, want, was_item=live.get("kind") == "item")
                 if _shape(want) != _shape(live) else [])
    runs = _run_requests(start, want, reset=True) if want.get("restyle") else []
    return _bullets_last(paragraph, runs)


def _text_style(run: dict) -> dict:
    style: dict = {}
    for key, api in (("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
                     ("strike", "strikethrough")):
        if run.get(key):
            style[api] = True
    if run.get("code"):
        style["weightedFontFamily"] = {"fontFamily": "Courier New"}
    if run.get("color"):
        style["foregroundColor"] = {"color": {"rgbColor": _rgb(run["color"])}}
    if run.get("highlight"):
        style["backgroundColor"] = {"color": {"rgbColor": _rgb(run["highlight"])}}
    if run.get("link"):
        style["link"] = {"url": run["link"]}
    return style


def _rgb(value: str) -> dict:
    value = value.lstrip("#")
    return {name: int(value[i:i + 2], 16) / 255
            for name, i in (("red", 0), ("green", 2), ("blue", 4))}


def requests(theirs: dict, merged: list[dict]) -> list[dict]:
    """Every edit that turns the live document into the merge, back to front.

    Blocks are addressed by the spans `theirs` was read at, so nothing here may be
    sent against a document that has moved on — `sync.py`'s plan/send/re-plan with
    `requiredRevisionId` is the guard, and the same rule holds here.
    """
    by_key = {b["key"]: b for b in merged if b.get("key")}
    plans: list[tuple[int, int, list[dict]]] = []  # (index, order within, requests)

    for live in theirs["blocks"]:
        want = by_key.get(live.get("key"))
        if want is None and live.get("key") is not None:
            plans.append((live["span"][0], DELETE, [{"deleteContentRange": {"range": {
                "startIndex": live["span"][0], "endIndex": live["span"][1]}}}]))
            continue
        if want is None or want.get("span") != live.get("span"):
            continue
        for one, other in _pairs(live, want):
            edits = text_requests(one, block_text(other)) + _restyle_requests(one, other)
            if edits:
                plans.append((one["span"][0], EDIT, edits))

    # The paragraph mark of the last block that survives this sync: where a block
    # with nothing after it is appended. Blocks the source deleted are past it, and
    # they are deleted first (higher indices come first), so it still holds then.
    kept = [b for b in theirs["blocks"] if b.get("key") is None or b["key"] in by_key]
    tail = kept[-1]["span"][1] - 1 if kept else 1
    # Back to front here too: two blocks added at one index both insert there, and
    # what is written last ends up in front, so the later block is planned first.
    for position in range(len(merged) - 1, -1, -1):
        block = merged[position]
        if block.get("origin") != "added by the source" or block.get("kind") == "table":
            continue  # a table is a grid, not text: nothing here can write one
        text = block_text(block)
        if FROZEN in text:
            continue  # a chip cannot be written; `plan` says so in its notes
        at = _insert_index(merged, position)
        if at is not None:
            plans.append((at, BEFORE, [{"insertText": {"location": {"index": at},
                                                       "text": text + "\n"}}]
                          + _style_requests(at, block)))
        elif kept:
            # Nothing follows it, so it is appended after the document's last
            # paragraph — and the break goes in *first*, the words after it. The
            # body's final newline cannot be written past, so "text\n" at `tail`
            # would join the last paragraph and leave an empty one behind instead.
            plans.append((tail, APPEND, [{"insertText": {"location": {"index": tail},
                                                         "text": "\n" + text}}]
                          + _style_requests(tail + 1, block)))
        else:
            # Nothing of the document survives: write into the empty paragraph Docs
            # always keeps, and let the empty one end up at the bottom.
            plans.append((tail, APPEND, [{"insertText": {"location": {"index": tail},
                                                         "text": text + "\n"}}]
                          + _style_requests(tail, block)))

    out: list[dict] = []
    # Back to front, so an earlier edit never moves a later one's indices, and at one
    # index by what the edits do there: a block appended at the last paragraph's mark
    # must go in before that paragraph's own edits, which end where it begins; a block
    # inserted before another pushes it down, so that block's edits go first, and a
    # delete of it first of all.
    for _, _, reqs in sorted(plans, key=lambda p: (-p[0], p[1])):
        out += reqs
    return out


def _pairs(live: dict, want: dict):
    """(live block, merged block) for a block and, if it is a table, for every block
    in its cells — where identity is the cell's place, not a named range."""
    yield live, want
    for r, row in enumerate(live.get("rows", [])):
        for c, cell in enumerate(row):
            for i, inner in enumerate(cell):
                other = _at(want, r, c, i)
                if other is not None and inner.get("span"):
                    yield inner, other


def _insert_index(merged: list[dict], position: int) -> int | None:
    """Where a new block's text goes: at the start of the block that follows it,
    or None when nothing follows and it is appended to the document instead."""
    for block in merged[position + 1:]:
        if block.get("span"):
            return block["span"][0]
    return None


def plan(base: dict, ours: dict, theirs: dict) -> dict:
    """The whole planning step: keys, merge, edits."""
    doc_ir.key_blocks(ours)
    restore_unreadable(base, ours)
    restore_unreadable(theirs, ours, base)
    inherit_keys(base, ours)
    result = merge(base, ours, theirs)
    for block in result["blocks"]:
        if block.get("origin") != "added by the source":
            continue
        if block.get("kind") == "table":
            result["notes"].append(f"{block.get('key')}: a table the source added cannot be "
                                   f"written — add it in the document, then sync")
        elif FROZEN in block_text(block):
            result["notes"].append(f"{block.get('key')}: a new block with a chip in it cannot be "
                                   f"written — no import can create one")
    result["requests"] = requests(theirs, result["blocks"])
    return result
