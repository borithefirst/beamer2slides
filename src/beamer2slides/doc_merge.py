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
    key = live["key"]
    out = dict(live)
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


def _style_requests(start: int, block: dict) -> list[dict]:
    """Paragraph kind and run styling for a block written at `start`."""
    text = block_text(block)
    end = start + doc_ir.utf16_len(text) + 1
    out: list[dict] = []
    if block["kind"] != "item":
        # Text inserted at the start of a list item joins that item, bullet and all;
        # the paragraph this block was written into may well have been one.
        out.append({"deleteParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end}}})
    style = {"namedStyleType": doc_ir.NAMED_STYLE[block.get("level", 0)
                                                  if block["kind"] == "heading" else 0]}
    fields = ["namedStyleType"]
    if block.get("align"):
        style["alignment"] = doc_ir.TO_ALIGNMENT[block["align"]]
        fields.append("alignment")
    out.append({"updateParagraphStyle": {
        "range": {"startIndex": start, "endIndex": end}, "paragraphStyle": style,
        "fields": ",".join(fields)}})
    at = start
    for run in block.get("runs", []):
        width = doc_ir.utf16_len(run["text"])
        marks = _text_style(run)
        if marks and width:
            out.append({"updateTextStyle": {
                "range": {"startIndex": at, "endIndex": at + width},
                "textStyle": marks, "fields": ",".join(sorted(marks))}})
        at += width
    if block["kind"] == "item":
        # Bullets last: a style request covering the whole paragraph would restyle
        # the glyph too, the same trap as the Slides pipeline's createParagraphBullets.
        out.append({"createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end},
            "bulletPreset": BULLETS[bool(block.get("ordered"))]}})
    return out


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
            plans.append((live["span"][0], 0, [{"deleteContentRange": {"range": {
                "startIndex": live["span"][0], "endIndex": live["span"][1]}}}]))
        elif want is not None and want.get("span") == live.get("span"):
            edits = text_requests(live, block_text(want))
            if edits:
                plans.append((live["span"][0], 1, edits))

    tail = theirs["blocks"][-1]["span"][1] - 1 if theirs["blocks"] else 1
    for position, block in enumerate(merged):
        if block.get("origin") != "added by the source":
            continue
        at = _insert_index(theirs, merged, position, tail)
        text = block_text(block)
        plans.append((at, 2, [{"insertText": {"location": {"index": at}, "text": text + "\n"}}]
                      + _style_requests(at, block)))

    out: list[dict] = []
    # Back to front, so an earlier edit never moves a later one's indices. At one
    # index the order matters too: a block inserted there pushes the block that
    # starts there down the document, so that block's own edits go first (order 1
    # before order 2), and a delete of it goes first of all.
    for _, _, reqs in sorted(plans, key=lambda p: (-p[0], p[1])):
        out += reqs
    return out


def _insert_index(theirs: dict, merged: list[dict], position: int, tail: int) -> int:
    """Where a new block's text goes: at the start of the block that follows it."""
    for block in merged[position + 1:]:
        if block.get("span"):
            return block["span"][0]
    return tail


def plan(base: dict, ours: dict, theirs: dict) -> dict:
    """The whole planning step: keys, merge, edits."""
    doc_ir.key_blocks(ours)
    restore_unreadable(base, ours)
    restore_unreadable(theirs, ours, base)
    inherit_keys(base, ours)
    result = merge(base, ours, theirs)
    result["requests"] = requests(theirs, result["blocks"])
    return result
