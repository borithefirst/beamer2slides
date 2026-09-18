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
DELETE, APPEND, EDIT, BEFORE = 0, 1, 2, 3


# ---------------------------------------------------------------- block text

def block_text(block: dict) -> str:
    """A block's text as the merge sees it: frozen runs are one opaque character."""
    return "".join(FROZEN if r.get("frozen") else r["text"] for r in block.get("runs", []))


def _match_text(block: dict) -> str:
    """What a block is recognised by. A table has no words of its own, so its cells
    stand in for it — without them every table in a document looks like every other,
    and two of them would swap keys the moment one was added."""
    if block.get("kind") == "table":
        return " | ".join(block_text(inner) for row in block.get("rows", [])
                          for cell in row for inner in cell)
    return block_text(block)


def frozen_of(block: dict) -> tuple:
    """What the frozen runs are, in order. Two blocks may only be merged if equal."""
    return tuple(_frozen_id(r) for r in block.get("runs", []) if r.get("frozen"))


def _frozen_id(run: dict) -> tuple:
    """A frozen run's identity. A picture is its object *and* the file it shows — the
    file's name and a digest of its bytes — so a figure the source regenerated under
    the same name is a picture that changed."""
    if run.get("chip") == "image":
        # By its bytes where they are known, so a file the source only renamed is the
        # same picture; by its name where they are not (a URL, a file not checked out).
        return ("image", run.get("value", ""), run.get("sha") or run.get("src", ""))
    return (run["chip"], run.get("text", ""), run.get("value", ""))


def writable(run: dict) -> bool:
    """Whether a request can create this frozen run (measured, docs/google-docs.md):
    a picture from a file or a URL, a person chip from its email, a date chip from its
    timestamp. A rich link is refused by the API; an equation, a dropdown, a footnote
    and a table of contents have no request at all."""
    if not run.get("frozen"):
        return True
    if run.get("chip") == "image":
        return bool(run.get("uri") or (run.get("src") and not run.get("missing")))
    return run.get("chip") in ("person", "date") and bool(run.get("value"))


def _writable_block(block: dict) -> bool:
    return all(writable(r) for r in block.get("runs", []))


def restore_pictures(live: dict, base: dict | None, ours: dict | None = None) -> dict:
    """Tell each picture in the document which file it shows, from the files that know.

    The document knows a picture by its object id and nothing else of ours, so the
    canonical file carries the id (`data-object`) and the base remembers it with the
    file's name and digest. The base decides: it says what the picture was when both
    sides last agreed, which is what makes a figure the source regenerated a change —
    unless the file shows the same bytes under another name, which is a rename and
    nothing to write. What the document cannot carry (an alt text on a picture a sync
    inserted) is filled in the same way; the document's own wins wherever it has one.
    """
    was = {r["value"]: r for r in _image_runs((base or {}).get("blocks", [])) if r.get("value")}
    now = {r["value"]: r for r in _image_runs((ours or {}).get("blocks", [])) if r.get("value")}
    for run in _image_runs(live["blocks"]):
        before, after = was.get(run.get("value")), now.get(run.get("value"))
        if after is not None and (before is None or after.get("sha") == before.get("sha")):
            _fill_picture(run, after)
        _fill_picture(run, before)
    return live


def place_pictures(live: dict, planned: list[dict]) -> int:
    """Pictures the sync just inserted, told their files by where they stand: a new
    object id is unknown to every file, but it is the n-th picture of a block the
    plan wrote, and so is the file it was made from."""
    by_key = {b["key"]: b for b in planned if b.get("key")}
    done = 0
    for block in live["blocks"]:
        wanted = by_key.get(block.get("key"))
        if wanted is None:
            continue
        mine, theirs = list(_image_runs([wanted])), list(_image_runs([block]))
        if len(mine) != len(theirs):
            continue
        for run, source in zip(theirs, mine):
            if not run.get("src") and source.get("src"):
                _fill_picture(run, source)
                done += 1
    return done


def _unseen_pictures(ours: dict, base: dict) -> None:
    """A picture file the checkout does not have says nothing about its bytes: take the
    base's word for them, or every sync would see it change."""
    was = {r["value"]: r for r in _image_runs(base["blocks"]) if r.get("value")}
    for run in _image_runs(ours["blocks"]):
        if run.get("missing") and not run.get("sha") and run.get("value") in was:
            if was[run["value"]].get("sha"):
                run["sha"] = was[run["value"]]["sha"]


def _fill_picture(run: dict, source: dict | None) -> None:
    if source is None:
        return
    for key in ("src", "sha", "alt", "title"):
        if not run.get(key) and source.get(key):
            run[key] = source[key]


def _image_runs(blocks: list[dict]):
    for block in blocks:
        for row in block.get("rows", []):
            for cell in row:
                yield from _image_runs(cell)
        for run in block.get("runs", []):
            if run.get("chip") == "image":
                yield run


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
            block["guessed"] = True   # `bullet_requests` makes the document say it itself
    return live


def tidy_requests(live: dict) -> list[dict]:
    """What a sync writes after the merge so the document reads back as it looks:
    bullets for the lists it cannot describe, and a plain paragraph after a final
    table that took over a list item's glyph or a heading's style."""
    out = bullet_requests(live)
    if live.get("trailer_kind"):
        start, end = live["trailer"]
        span = {"startIndex": start, "endIndex": end}
        out += [{"deleteParagraphBullets": {"range": span}},
                {"updateParagraphStyle": {"range": span, "fields": "namedStyleType,alignment",
                                          "paragraphStyle": {"namedStyleType": "NORMAL_TEXT",
                                                             "alignment": "START"}}}]
    return out


def bullet_requests(live: dict) -> list[dict]:
    """Bullets of the document's own for the lists it cannot describe.

    Measured: `createParagraphBullets` over a list the importer built replaces its
    unspecified glyphs with real ones — a `glyphSymbol` for bullets, a `glyphType`
    for numbers — and keeps every item's nesting level. From then on the list reads
    back as what it is, so a reader who switches it from bullets to numbers in the
    toolbar is seen doing so, where before the file silently won. One request per run
    of neighbouring items that agree on being numbered.
    """
    out, run = [], []
    for block in live["blocks"] + [{"kind": "end"}]:
        if (run and (block.get("kind") != "item" or not block.get("span")
                     or block.get("ordered") != run[0].get("ordered"))):
            if any(b.get("guessed") for b in run):
                out.append({"createParagraphBullets": {
                    "range": {"startIndex": run[0]["span"][0], "endIndex": run[-1]["span"][1]},
                    "bulletPreset": BULLETS[bool(run[0].get("ordered"))]}})
            run = []
        if block.get("kind") == "item" and block.get("span"):
            run.append(block)
    return out


def styles_of(block: dict) -> tuple:
    """A block's styling, run by run, without its words.

    What changes when somebody marks a word bold or takes a colour away — and not
    when somebody rewrites a word, which is the text merge's business. A mark applied
    to part of a run splits it, so the run count carries the boundaries.
    """
    return tuple(("chip", frozenset()) if r.get("frozen") else
                 ("text", frozenset((k, v) for k, v in r.items() if k not in ("text", "width")))
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
        by_text.setdefault((_match_shape(block), _match_text(block)), []).append(block)
    pending = []
    for block in ours["blocks"]:
        same = by_text.get((_match_shape(block), _match_text(block)))
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
            ratio = SequenceMatcher(None, _match_text(candidate), _match_text(block)).ratio()
            if ratio > score:
                best, score = candidate, ratio
        if best is not None:
            block["key"] = best["key"]
            rest.remove(best)
    return ours


def anchor_tables(live: dict, shaped: list[dict]) -> int:
    """Name the tables the structural batch created, so the next plan knows them.

    A table `insertTable` built carries no named range, and a read cannot tell it from
    one a reader made in the browser. It is found by what it follows: the block the
    plan put it after, which the document already had.
    """
    done = 0
    for told in shaped:
        if "after" not in told or not told["key"]:
            continue
        if any(b.get("key") == told["key"] for b in live["blocks"]):
            continue
        start = 0
        if told["after"] is not None:
            at = next((i for i, b in enumerate(live["blocks"])
                       if b.get("key") == told["after"]), None)
            if at is None:
                continue
            start = at + 1
        found = next((b for b in live["blocks"][start:]
                      if b.get("kind") == "table" and not b.get("key")), None)
        if found is not None:
            found["key"] = told["key"]
            done += 1
    return done


def rebase_tables(base: dict, theirs: dict, shaped: list[dict]) -> dict:
    """What both sides agree on once the grid has been written.

    The grid the document now has is the one this sync gave it on the source's behalf,
    so it is no longer a difference between the sides and the base says it too. Only
    the grid: the base's cells keep the words they had, and the ones that were just
    made are empty. Taking the table as the document *reports* it would swallow into
    the base whatever a reader has typed in it — and a base is what both sides agreed
    on, not what one of them did a moment ago.
    """
    blocks = list(base["blocks"])
    for told in shaped:
        index = next((i for i, b in enumerate(theirs["blocks"])
                      if b.get("key") == told["key"]), None)
        if index is None:
            continue
        at = next((i for i, b in enumerate(blocks) if b.get("key") == told["key"]), None)
        if at is None:
            blocks.insert(_place(theirs, blocks, index), theirs["blocks"][index])
        elif told.get("ops"):
            blocks[at] = _regridded(blocks[at], told["ops"])
    return dict(base) | {"blocks": blocks}


def _regridded(was: dict, ops: list[tuple]) -> dict:
    """The base's table with the rows and columns that were just written in it."""
    rows = [list(row) for row in was.get("rows", [])]
    for line, how, at in sorted(ops, key=lambda o: -o[2]):
        if line == "row":
            width = len(rows[0]) if rows else 0
            if how == "delete":
                del rows[at]
            else:
                rows.insert(at, [_blank_cell() for _ in range(width)])
            continue
        for row in rows:
            if how == "delete":
                del row[at]
            else:
                row.insert(at, _blank_cell())
    return dict(was) | {"rows": rows}


def _blank_cell() -> list[dict]:
    return [{"kind": "paragraph", "runs": []}]


def adopt_keys(live: dict, planned: list[dict]) -> int:
    """Give a block written from nothing the key the merge meant it to have.

    A move is a delete and an insert, and the delete takes the block's named range
    with it, so the read-back after the write has no key for that block. Keyed from
    its own words instead, it would lose the identity the canonical file carries —
    the `id=` its author wrote — and the next diff would rename a paragraph nobody
    touched. The same holds for a block the source added with an id of its own.
    """
    taken = {b["key"] for b in live["blocks"] if b.get("key")}
    free: dict[tuple, list[str]] = {}
    for block in planned:
        if block.get("key") and block["key"] not in taken:
            free.setdefault((_match_shape(block), _match_text(block)), []).append(block["key"])
    done = 0
    for block in live["blocks"]:
        if block.get("key"):
            continue
        if same := free.get((_match_shape(block), _match_text(block))):
            block["key"] = same.pop(0)
            done += 1
    return done


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
        merged.insert(_place(ours, merged, index), fresh | {"origin": "added by the source"})
    _apply_source_moves(base, ours, theirs, merged, notes)
    return {"blocks": merged, "conflicts": conflicts, "notes": notes}


def _place(ours: dict, merged: list, index: int) -> int:
    """Where a source-added block goes: after the merged block that precedes it in ours."""
    for before in reversed(ours["blocks"][:index]):
        at = next((i for i, b in enumerate(merged) if b.get("key") == before.get("key")), None)
        if at is not None:
            return at + 1
    return 0


def _order_of(blocks: list[dict], keys: set) -> list[str]:
    return [b["key"] for b in blocks if b.get("key") in keys]


def _apply_source_moves(base: dict, ours: dict, theirs: dict, merged: list, notes: list) -> None:
    """Put back where the file has them the blocks the *source* moved.

    The merged order is the document's, because a reader who moved a paragraph meant
    it. But a source that moves a section means it too, and nothing else would ever
    say so — the file's order is otherwise only read, never written. So when the
    file's order of the blocks all three sides know differs from the base's and the
    document's does not, those blocks are moved to where the file has them. Where
    both sides reordered, the document keeps its order, as everywhere else.
    """
    placed = {b["key"]: b for b in merged if b.get("key")}
    common = placed.keys() & {b.get("key") for b in base["blocks"]} \
        & {b.get("key") for b in ours["blocks"]} & {b.get("key") for b in theirs["blocks"]}
    was, mine, live = (_order_of(side["blocks"], common) for side in (base, ours, theirs))
    if mine == was:
        return
    if live != was:
        notes.append("both sides moved blocks around — the document's order is kept")
        return
    for key in _moved_keys(was, mine):
        block = placed[key]
        # A move is written as a delete and a fresh insert, so it can only carry what
        # an insert can write: not a table's grid, and not a chip no request creates.
        if block.get("kind") == "table" or not _writable_block(block):
            notes.append(f"{key}: the source moved it, but a block with a table or an "
                         f"equation-like chip in it cannot be written from nothing — "
                         f"left where the document has it")
            continue
        merged.remove(block)
        index = next(i for i, b in enumerate(ours["blocks"]) if b.get("key") == key)
        merged.insert(_place(ours, merged, index), block)
        block["moved"] = True


def _moved_keys(was: list[str], mine: list[str]) -> list[str]:
    """The fewest blocks whose move turns one order into the other, in the file's order.

    Whatever the longest run of blocks that kept their order is, stays; the rest are
    what somebody moved. Taking it the other way round — moving everything that is
    not where it was — would rewrite a whole document because its first paragraph
    went to the end.
    """
    kept = {mine[j] for op, _, _, j1, j2 in
            SequenceMatcher(None, was, mine, autojunk=False).get_opcodes()
            if op == "equal" for j in range(j1, j2)}
    return [key for key in mine if key not in kept]


def _merge_block(was: dict, mine: dict, live: dict, conflicts: list, notes: list) -> dict:
    key = live.get("key") or "a table cell"
    out = dict(live)
    if live.get("kind") == "table":
        return _merge_table(was, mine, live, conflicts, notes)
    # Only the source's own chip changes need a decision: one the document made (a
    # picture a reader replaced, a chip inserted) is merged like its words, and the
    # edits planned for the source's words never touch it.
    if frozen_of(mine) != frozen_of(live) and frozen_of(mine) != frozen_of(was):
        if (frozen_of(live) == frozen_of(was) and block_text(live) == block_text(was)
                and styles_of(live) == styles_of(was)):
            # The source added, removed or replaced a picture or a chip in a block the
            # document left exactly as it was: the block is written again from the
            # file, which is a merge, since the document has nothing of its own in it.
            runs = _rewritten_runs(mine, live)
            # Only when nothing is lost for good: the block is deleted before it is
            # written, so an equation in it — which no request can make again — keeps
            # the whole block as the document has it.
            if _writable_block(live) and all(writable(r) for r in runs):
                return out | {k: mine[k] for k in ("kind", "level", "ordered", "align")
                              if k in mine} | {"runs": runs, "rewrite": True, "origin": "merged"}
            notes.append(f"{key}: the source changed a chip or picture no request can write "
                         f"— left alone")
            return out | {"origin": "frozen content differs"}
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
    without changing the grid. A source that changed the grid while the document left
    it alone has its rows and columns written, in a batch of their own and before the
    words (`structure`); where both sides changed it, the document's grid stands, as
    everywhere else.
    """
    key = live.get("key") or "a table"
    out = dict(live)
    if _grid(mine) != _grid(live) or _grid(was) != _grid(live):
        if _grid(was) == _grid(live) and (ops := _grid_ops(mine, live)):
            return out | {"origin": "the grid the source has", "regrid": ops}
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


def _grid_ops(mine: dict, live: dict) -> list[tuple[str, str, int]] | None:
    """How to turn the document's grid into the file's, as whole rows and columns.

    One dimension at a time: when the number of rows changed, the rows are matched by
    their words, and when the number of columns changed, the columns are. Both at once
    leaves nothing to match on — every row differs because every row is a column
    longer — and so does a cell holding more than one paragraph or a row of its own
    length. Those are reported instead, which is what this did with every grid change
    before.
    """
    here, there = live.get("rows", []), mine.get("rows", [])
    if not here or not there:
        return None
    if len({len(row) for row in here}) != 1 or len({len(row) for row in there}) != 1:
        return None                        # a row out of step with the others
    if any(len(cell) != 1 for row in here + there for cell in row):
        return None                        # a cell with more than one paragraph in it
    if len(here[0]) == len(there[0]):
        return [("row", how, at) for how, at in
                _line_ops([_line_text(row) for row in here], [_line_text(row) for row in there])]
    if len(here) == len(there):
        return [("column", how, at) for how, at in
                _line_ops([_line_text(column) for column in zip(*here)],
                          [_line_text(column) for column in zip(*there)])]
    return None


def _line_text(cells) -> tuple:
    return tuple(block_text(cell[0]) for cell in cells)


def _line_ops(here: list, there: list) -> list[tuple[str, int]]:
    """Which rows (or columns) to add or take away, matched by their words.

    Only the *count* a stretch is out by is written. A row whose words the source
    changed is a text edit, and text is merged afterwards cell by cell, so a stretch
    that differs on both sides adds or removes at its end and leaves the rest alone —
    a row rewritten by the source is never deleted and written again, which would
    throw away whatever the document put in it.
    """
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, here, there, autojunk=False).get_opcodes():
        extra = (j2 - j1) - (i2 - i1)
        if op == "equal" or extra == 0:
            continue
        out += ([("insert", i2)] * extra if extra > 0
                else [("delete", at) for at in range(i2 + extra, i2)])
    return out


def _at(block: dict, row: int, cell: int, index: int) -> dict | None:
    try:
        return block["rows"][row][cell][index]
    except (KeyError, IndexError):
        return None


def _rewritten_runs(mine: dict, live: dict) -> list[dict]:
    """The file's runs, with every frozen run the document already has taken from the
    document — it knows where that picture's pixels are, and what a chip says."""
    have: dict[tuple, list] = {}
    for run in live.get("runs", []):
        if run.get("frozen"):
            have.setdefault(_frozen_id(run), []).append(run)
    out = []
    for run in mine.get("runs", []):
        if run.get("frozen"):
            same = have.get(_frozen_id(run))
            out.append(dict(same.pop(0)) if same else dict(run) | {"width": 1})
        else:
            out.append(dict(run) | {"width": doc_ir.utf16_len(run["text"])})
    return out


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


def _run_width(run: dict) -> int:
    """How many index units a run holds: a chip or a picture is one."""
    if "width" in run:
        return run["width"]
    return 1 if run.get("frozen") else doc_ir.utf16_len(run["text"])


def _width(block: dict) -> int:
    """How many index units a block's text holds, the paragraph mark apart."""
    return sum(_run_width(run) for run in block.get("runs", []))


# The `uri` of a picture that has to be staged first: `doc_sync.Stager` swaps it for a
# URL Google can fetch before the batch is sent, which keeps this module pure.
STAGE = "b2s-stage:"


def _content_requests(at: int, block: dict, before: str = "", after: str = "") -> list[dict]:
    """A block's words and objects written at `at`, as `before + words + after`.

    The words go in as one piece with the objects left out, and the objects are then
    inserted into it back to front, each at its offset in those words: a later object
    is pushed right by the ones in front of it, which is exactly the one unit each of
    them holds.
    """
    words, objects = "", []
    for run in block.get("runs", []):
        if run.get("frozen"):
            objects.append((doc_ir.utf16_len(words), run))
        else:
            words += run["text"]
    text = before + words + after
    out = [{"insertText": {"location": {"index": at}, "text": text}}] if text else []
    at += doc_ir.utf16_len(before)
    for offset, run in reversed(objects):
        out.append(_object_request(at + offset, run))
    return out


def _object_request(index: int, run: dict) -> dict:
    location = {"index": index}
    if run.get("chip") == "person":
        return {"insertPerson": {"location": location, "personProperties": {"email": run["value"]}}}
    if run.get("chip") == "date":
        return {"insertDate": {"location": location,
                               "dateElementProperties": {"timestamp": run["value"]}}}
    src = run.get("src", "")
    uri = run.get("uri") or (src if src.startswith(("https://", "http://")) else STAGE + src)
    request = {"location": location, "uri": uri}
    if run.get("size"):
        width, height = run["size"]
        request["objectSize"] = {"width": {"magnitude": width * doc_ir.PT_PER_PX, "unit": "PT"},
                                 "height": {"magnitude": height * doc_ir.PT_PER_PX, "unit": "PT"}}
    return {"insertInlineImage": request}


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
        width = _run_width(run)
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


def _style_requests(start: int, block: dict, reset: bool = False) -> list[dict]:
    """Everything but the words, for a block written at `start` from nothing."""
    end = start + _width(block) + 1
    return _bullets_last(_paragraph_requests(start, end, block, was_item=True),
                         _run_requests(start, block, reset))


def _block_edits(live: dict, want: dict) -> list[dict]:
    """What turns one block of the document into what the merge says, in place."""
    if not want.get("rewrite"):
        return text_requests(live, block_text(want)) + _restyle_requests(live, want)
    # Written again from the file: everything but the paragraph mark goes, which keeps
    # the block where it is (and a table after it happy), and the file's runs go in.
    start, end = live["span"]
    out = ([{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end - 1}}}]
           if end - 1 > start else [])
    return out + _content_requests(start, want) + _style_requests(start, want, reset=True)


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

    # Which of the document's blocks go: the ones the source deleted, and the ones it
    # moved (a move is a delete here and a write further down). Their ranges are
    # decided together, because a delete in front of a table borrows the mark of the
    # block before it and two of them must not ask for the same one.
    going = {i for i, live in enumerate(theirs["blocks"]) if _goes(live, by_key)}
    # Whether the last block read is the body's last paragraph, whose mark is the
    # body's own: not when an empty paragraph after a final table was left out.
    ends = not theirs.get("trailer")
    for index in sorted(going):
        start, end = _delete_range(theirs["blocks"], index, going, ends)
        plans.append((start, DELETE, [{"deleteContentRange": {
            "range": {"startIndex": start, "endIndex": end}}}]))

    for index, live in enumerate(theirs["blocks"]):
        want = by_key.get(live.get("key"))
        if index in going or want is None or want.get("span") != live.get("span"):
            continue
        for one, other in _pairs(live, want):
            edits = _block_edits(one, other)
            if edits:
                plans.append((one["span"][0], EDIT, edits))

    # The paragraph mark of the last block that survives this sync: where a block
    # with nothing after it is appended. Blocks the source deleted are past it, and
    # they are deleted first (higher indices come first), so it still holds then.
    kept = [b for b in theirs["blocks"]
            if b.get("key") is None or (b["key"] in by_key and not by_key[b["key"]].get("moved"))]
    tail = kept[-1]["span"][1] - 1 if kept else 1
    trailer = theirs.get("trailer")
    if trailer:
        # The body ends on a table and the empty paragraph after it: the first block
        # appended is written *into* that paragraph, and the rest after it.
        tail = trailer[0]
    first = min((p for p, b in enumerate(merged)
                 if _written_here(b) and _insert_index(merged, p) is None), default=None)
    # Back to front here too: two blocks added at one index both insert there, and
    # what is written last ends up in front, so the later block is planned first.
    for position in range(len(merged) - 1, -1, -1):
        block = merged[position]
        if not _written_here(block):
            continue
        at = _insert_index(merged, position)
        if at is not None:
            plans.append((at, BEFORE, _content_requests(at, block, after="\n")
                          + _style_requests(at, block)))
        elif trailer and position == first:
            plans.append((tail, APPEND, _content_requests(tail, block)
                          + _style_requests(tail, block)))
        elif kept or trailer:
            # Nothing follows it, so it is appended after the document's last
            # paragraph — and the break goes in *first*, the words after it. The
            # body's final newline cannot be written past, so "text\n" at `tail`
            # would join the last paragraph and leave an empty one behind instead.
            plans.append((tail, APPEND, _content_requests(tail, block, before="\n")
                          + _style_requests(tail + 1, block)))
        else:
            # Nothing of the document survives: write into the empty paragraph Docs
            # always keeps, and let the empty one end up at the bottom.
            plans.append((tail, APPEND, _content_requests(tail, block, after="\n")
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


def _written_here(block: dict) -> bool:
    """Whether `requests` writes this block from nothing: one the source added or
    moved — but not a table, which is a grid (`structure` builds it), and not a block
    with a chip no request can create (`plan` says so in its notes)."""
    return ((block.get("origin") == "added by the source" or bool(block.get("moved")))
            and block.get("kind") != "table" and _writable_block(block))


def structure(theirs: dict, merged: list[dict]) -> tuple[list[dict], list[dict]]:
    """The requests that change a table's shape, and what each of them does.

    A grid is not text. `insertTable`, `insertTableRow` and their deletes are the only
    way to build one, and none of them belongs in the same batch as the words: every
    index below a grid that changes moves. So a sync that needs one of these sends
    them on their own, reads the document again — which is also how the new table gets
    its key and its anchor — and plans the text against the grid it then has
    (`doc_sync.sync`). Back to front, so the indices of the ones still to come hold.
    """
    plans: list[tuple[int, list[dict], dict]] = []
    for position, block in enumerate(merged):
        if block.get("kind") != "table":
            continue
        key = block.get("key")
        if block.get("regrid"):
            what = ", ".join(f"{how}s a {line}" for line, how, _ in block["regrid"])
            plans.append((block["span"][0], _grid_requests(block["span"][0], block["regrid"]),
                          {"key": key, "ops": block["regrid"],
                           "note": f"`{key}`: {what} — the grid the source has"}))
        elif block.get("origin") == "added by the source":
            rows = len(block.get("rows", []))
            columns = len(block["rows"][0]) if rows else 0
            if not rows or not columns:
                continue
            at, reqs = _new_table_requests(theirs, _insert_index(merged, position),
                                           rows, columns)
            if reqs:
                plans.append((at, reqs, {"key": key, "after": _after_key(merged, position),
                                         "note": f"`{key}`: a table of {rows}×{columns} "
                                                 f"added by the source"}))
    out: list[dict] = []
    shaped: list[dict] = []
    seen: set[int] = set()
    for at, reqs, told in sorted(plans, key=lambda p: -p[0]):
        if at in seen:
            continue  # two tables added at one index: one send can place one of them
        seen.add(at)
        out += reqs
        shaped.append(told)
    return out, shaped


_END = 1 << 30  # a table appended at the end of the body: after every index there is


def _after_key(merged: list[dict], position: int) -> str | None:
    """The key of the nearest block in front of this one that the document already
    has — where a table written from nothing will be found again once it exists."""
    for block in reversed(merged[:position]):
        if block.get("key") and block.get("span") and not block.get("moved"):
            return block["key"]
    return None


def _new_table_requests(theirs: dict, at: int | None, rows: int,
                        columns: int) -> tuple[int, list[dict]]:
    """A table where the file puts it, without the empty paragraph that comes with it.

    All of this was measured on a live document. `insertTable` splits the paragraph
    its index is in: what was before the index stays a paragraph, then comes the
    table, then the rest. The index must be *inside a paragraph*, so:

    - written in front of an ordinary block, it goes at that block's start, which
      leaves an empty paragraph in front of the table. The mark of the block before
      that one goes instead, which merges the two the way the Delete key does and
      leaves both blocks exactly as the file has them;
    - written in front of a table there is no paragraph at that index at all. It goes
      at the mark of the paragraph before it, and the empty half lands *after* the new
      table — between the two, which is where Docs wants a paragraph anyway;
    - written after everything, it goes to the end of the segment, and Docs keeps a
      paragraph after it, because a document ends on one.
    """
    table = {"rows": rows, "columns": columns}
    if at is None:
        return _END, [{"insertTable": table | {"endOfSegmentLocation": {}}}]
    after = next((b for b in theirs["blocks"] if b["span"][0] == at), None)
    before = next((b for b in theirs["blocks"] if b["span"][1] == at), None)
    if after is not None and after.get("kind") == "table":
        if before is None:
            return at, []                  # a document that opens on a table: nowhere to write
        return at - 1, [{"insertTable": table | {"location": {"index": at - 1}}}]
    out = [{"insertTable": table | {"location": {"index": at}}}]
    if at > 1 and before is not None and before.get("kind") != "table":
        out.append({"deleteContentRange": {"range": {"startIndex": at - 1, "endIndex": at}}})
    return at, out


def _grid_requests(start: int, ops: list[tuple]) -> list[dict]:
    """Rows and columns added or taken away, back to front so the indices hold."""
    where = {"index": start}
    out = []
    for line, how, index in sorted(ops, key=lambda o: -o[2]):
        row, column = (index, 0) if line == "row" else (0, index)
        cell = {"tableStartLocation": where, "rowIndex": row, "columnIndex": column}
        if how == "delete":
            out.append({f"deleteTable{line.capitalize()}": {"tableCellLocation": cell}})
            continue
        # There is no "insert at 0": the API adds beside a cell, so the first line is
        # written above or left of the one that is there now.
        cell |= {"rowIndex": max(row - 1, 0)} if line == "row" else {"columnIndex": max(column - 1, 0)}
        out.append({f"insertTable{line.capitalize()}": {
            "tableCellLocation": cell,
            ("insertBelow" if line == "row" else "insertRight"): index > 0}})
    return out


def _goes(live: dict, by_key: dict) -> bool:
    """Whether this block of the document is deleted: the source dropped it, or the
    source moved it and it is written again where the file puts it."""
    want = by_key.get(live.get("key"))
    if want is None:
        return live.get("key") is not None
    return bool(want.get("moved")) and want.get("span") == live.get("span")


def _delete_range(blocks: list[dict], index: int, going: set[int],
                  ends: bool = True) -> tuple[int, int]:
    """What to delete for block `index`, without touching a newline that must stay.

    "Deleting the newline character before a Table, TableOfContents or SectionBreak"
    is one of the deletes the API refuses outright — and a refused request throws out
    the whole batch — so a block that stands right in front of a table gives up the
    *previous* block's paragraph mark instead of its own. Docs then merges the two the
    way the Delete key does, keeping the first one's style, and the table still has a
    paragraph in front of it. A run of deleted blocks passes that leftwards one by
    one, so no two ranges ever ask for the same mark. The body's last mark is the
    same kind of newline (measured: refused), so the last block does the same when
    `ends` says it is the body's last paragraph.
    """
    start, end = blocks[index]["span"]
    if not _mark_is_taken(blocks, index, going, ends):
        return start, end
    if index == 0 or blocks[index - 1].get("kind") == "table":
        # No mark to take: the block's words go and an empty paragraph stays in front
        # of the table. Docs wants one between two tables anyway.
        return start, end - 1
    return start - 1, end - 1


def _mark_is_taken(blocks: list[dict], index: int, going: set[int], ends: bool) -> bool:
    """Whether this block's own paragraph mark must survive the delete — because a
    table follows it, because it ends the body, or because the deleted block that
    follows it takes this one's."""
    after = index + 1
    if after >= len(blocks):
        return ends
    if after in going:
        return _delete_range(blocks, after, going, ends)[0] < blocks[after]["span"][0]
    return blocks[after].get("kind") == "table"


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
    or None when nothing follows and it is appended to the document instead.

    A block that is being moved is no anchor: the span it still has is the place it
    is about to be deleted from, which says nothing about where the new one goes.
    """
    for block in merged[position + 1:]:
        if block.get("span") and not block.get("moved"):
            return block["span"][0]
    return None


def plan(base: dict, ours: dict, theirs: dict) -> dict:
    """The whole planning step: keys, merge, the grid, the edits."""
    doc_ir.key_blocks(ours)
    restore_unreadable(base, ours)
    restore_unreadable(theirs, ours, base)
    _unseen_pictures(ours, base)
    inherit_keys(base, ours)
    result = merge(base, ours, theirs)
    for block in result["blocks"]:
        if block.get("origin") == "added by the source" and not _writable_block(block):
            result["notes"].append(f"{block.get('key')}: a new block with a chip in it that no "
                                   f"request can create (or a picture file that is not there) "
                                   f"cannot be written")
    result["structure"], result["shaped"] = structure(theirs, result["blocks"])
    result["requests"] = requests(theirs, result["blocks"])
    return result
