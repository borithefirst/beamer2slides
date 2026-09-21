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

from bisect import bisect_left
from collections import Counter
from difflib import SequenceMatcher
from collections.abc import Iterable
from typing import NamedTuple

from . import doc_ir
from .merge import diff3, tokens

# One character standing for a frozen run while text is diffed, so a merge can move
# words around it but never through it. U+FFFC is the object replacement character.
FROZEN = "￼"
BLOCK_MATCH = 0.5  # least similarity for an unkeyed block to inherit a key
TABLE_MATCH = 0.5  # least similarity for a table that lost its anchor to be known again
TABLE_MARGIN = 0.1  # and how far clear of the runner-up, on both sides, it has to be
BULLETS = {False: "BULLET_DISC_CIRCLE_SQUARE", True: "NUMBERED_DECIMAL_ALPHA_ROMAN"}
# The textStyle fields this merge owns: named on a restyle whether or not the run
# carries them, so that a mark the source took away is taken away in the document.
#
# A field belongs here only when the canonical file can say it *and* a read can see
# it: naming a field the file cannot carry would clear, on every source restyle,
# something a reader set in the browser and nothing on our side ever knew about.
# `fontSize`, `weightedFontFamily` and `smallCaps` joined the list when `doc_ir`
# began carrying them (`font`, `fontsize`, `smallcaps`) — the face a reader chooses now
# round-trips through the file, so writing the merge's answer back is writing the
# reader's own choice back, and a run that only repeats its named style carries
# nothing, so clearing the field there leaves the same face on the page.
# `baselineOffset` joined it the same way (`script`): a superscript is content, not
# decoration — the 2 of a footnote marker or of x², and the file says it with `<sup>`
# and `<sub>`. Until it did, a block written again from nothing came back with every
# raised character back on the baseline, and nothing said so.
MANAGED = ("backgroundColor", "baselineOffset", "bold", "fontSize", "foregroundColor",
           "italic", "link", "smallCaps", "strikethrough", "underline",
           "weightedFontFamily")
# The paragraph properties the merge owns, and the `paragraphStyle` field each is.
# The first three reach a document from HTML as well (measured: "Paragraph CSS:
# `text-align`, `margin-left`, `text-indent`, `line-height`"); the last three only
# through `batchUpdate` (`doc_ir.PARAGRAPH_DATA` says why), which the study measured
# working for `updateParagraphStyle` with `shading`.
PARAGRAPH_FIELDS = ((("indent", "indentStart"), ("indent_first", "indentFirstLine"),
                     ("line_spacing", "lineSpacing"), ("shading", "shading"),
                     ("space_above", "spaceAbove"), ("space_below", "spaceBelow"))
                    + tuple(doc_ir.BORDER_SIDES.items())
                    + tuple(doc_ir.PARAGRAPH_FLAGS.items()))
# What is written on a paragraph, named whether or not the block asks for it, so
# that a property the source took away goes away. Unset-and-named is the API's own
# way of saying "back to the default" (documented, not measured here).
MANAGED_PARAGRAPH = ("namedStyleType", "alignment") + tuple(a for _, a in PARAGRAPH_FIELDS)
# Those of them a block says in its own words, and how each is spelled in the API.
# The named style is not one: it is the block's kind (`named_style`).
PARAGRAPH_KEYS = (("align", "alignment"),) + PARAGRAPH_FIELDS
# A bullet's indents are the list preset's, not a choice anybody made: `doc_ir`
# leaves them out of an item and `createParagraphBullets` would overwrite them.
ITEM_PARAGRAPH = tuple(f for f in MANAGED_PARAGRAPH if not f.startswith("indent"))
# Everything about a block that is not its words, its styling or its identity.
SHAPE_KEYS = ("kind", "level", "ordered", "align") + tuple(k for k, _ in PARAGRAPH_FIELDS)
# What no HTML import can put in a document, so a push has to write it afterwards
# (`carry_unimported`, `tidy_requests`).
UNIMPORTABLE = (("shading", "space_above", "space_below")
                + tuple(doc_ir.BORDER_SIDES) + tuple(doc_ir.PARAGRAPH_FLAGS))
# What is written first when two edits are planned at one and the same index
# (`requests` says why each one sits where it does).
DELETE, APPEND, REPLANT, EDIT, BEFORE = 0, 1, 2, 3, 4


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


def _edited(live: dict, was: dict) -> bool:
    """Whether the document changed a block since the base — the test that outranks a
    source delete, and so the last thing standing between a reader's words and a
    `deleteContentRange`.

    It cannot be `block_text`: that is empty for a table, so a table the reader filled
    in cell by cell read as untouched and went with everything in it. A table says
    what it is through its cells and its grid, and a block holding a picture the
    reader replaced says it through its frozen runs.

    Nor can it be the words alone. Bolding a word is a choice a reader made in the
    document, as much as typing one, and `styling_lost` says so everywhere else —
    but a block whose only change was a mark read as untouched, so the source's
    delete went through and took the reader's styling with the block (fresh-seed
    90175: the reader bolds a word, `collide` drops that very block in the same
    step).

    And the same one size up, which is `_shape`: how the paragraph is *set*. A
    reader who centres a line, shades it, spaces it out, makes it a heading or an
    item, or turns a list's bullets into numbers has made a choice exactly as
    deliberate as bolding a word — and the whole of it fits in properties no word
    of the block carries, so a block changed that way and no other read as
    untouched here too and the delete took it (offline chain-6 seed 2050019, shape
    `imported_list`: the reader centres `item:bake` and the source drops it).
    `ordered` counts, unlike in `doc_loss_oracle.SHAPE_FIELDS`: what an import
    cannot *report* is neither side's fault, but both readings here are the
    document's own, one sync apart, so a list the reader renumbered says so.
    """
    return (_match_text(live) != _match_text(was) or _grid(live) != _grid(was)
            or frozen_of(live) != frozen_of(was) or _styled(live) != _styled(was)
            or _shape(live) != _shape(was))


def _styled(block: dict) -> tuple:
    """The styling of a block's words, in the order they wear it.

    By the stretch of text a style covers, not by the runs it is written in: a reader
    bolding a word splits one run into three and a rewrite joins them again, and
    neither is a change of styling. A frozen run is a place in the text, never a mark.
    """
    if block.get("kind") == "table":
        return tuple(_styled(inner) for row in block.get("rows", [])
                     for cell in row for inner in cell)
    out: list[list] = []
    for run in block.get("runs", []):
        text = FROZEN if run.get("frozen") else run.get("text", "")
        style = () if run.get("frozen") else tuple(sorted(_text_style(run).items()))
        if out and out[-1][1] == style:
            out[-1][0] += text
        else:
            out.append([text, style])
    return tuple((text, style) for text, style in out)


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
    if block.get("kind") == "toc":
        return False                       # no insertTableOfContents in the v1 API
    return all(writable(r) for r in block.get("runs", []))


# A structural element that is not a paragraph. Docs' index rules are about *these*,
# not about tables: nothing can be inserted at one's own index, the newline in front of
# one cannot be deleted, and one is deleted by its own span. Every test for them read
# `kind == "table"`, so a table of contents was an ordinary block to the planner — a
# block written in front of one went at its own index and a block deleted in front of
# one gave up its own mark. Docs refuses both, a refusal throws out the whole batch,
# and the sync died (the campaign's 'toc-block', fixed and out of `fuzz_docs.KNOWN`).
def _structural(block: dict | None) -> bool:
    return block is not None and block.get("kind") in doc_ir.STRUCTURAL


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


def unwritten_pictures(base: dict, ours: dict, theirs: dict, merged: list[dict],
                       notes: list[str]) -> None:
    """A picture's size and its alt text are read and never written.

    No request in the v1 API changes an embedded object. `insertInlineImage` carries
    an `objectSize`, so a size the source changes does reach the document when that
    block is written from nothing *and* the run the plan writes is the file's — a
    block whose picture the source regenerated, not one merely moved, where the run
    written is the document's copy and its size the document's. An alt text never
    reaches it at all, at any size. The settle then regenerates the file from the
    document, so such an edit is not merely unwritten: it is taken back out of the
    file, and the next sync sees nothing to say. Something that disappears twice
    over has to be said out loud.

    Deleting the picture from the file and writing it again is the way to have it at
    another size, because a picture with no `data-object` is a new one and goes in
    with its `objectSize` — at the price of whatever the browser put on the old one
    (a crop, a recolour: `doc_ir.unmodelled`), which is why the sync will not do it
    of its own accord over a number.
    """
    was = {r["value"]: r for r in _image_runs(base["blocks"]) if r.get("value")}
    live = {r["value"]: r for r in _image_runs(theirs["blocks"]) if r.get("value")}
    written = {r.get("value"): r.get("size") for block in merged
               if block.get("rewrite") or block.get("moved") or block.get("origin")
               == "added by the source" for r in _image_runs([block])}
    for block in ours["blocks"]:
        for run in _image_runs([block]):
            before = was.get(run.get("value"))
            if before is None:
                continue
            for what, key in (("size", "size"), ("alt text", "alt"), ("title", "title")):
                if run.get(key) == before.get(key) or (
                        key == "size" and run["value"] in written
                        and written[run["value"]] == run.get("size")):
                    continue
                way = (". Write the picture into the file again without its data-object "
                       "to have it inserted at that size" if key == "size" else "")
                notes.append(
                    f"{block.get('key')}: the source gave the picture the {what} "
                    f"{_said(key, run.get(key))} and no request writes one — the document "
                    f"keeps {_said(key, live.get(run['value'], before).get(key))} and the "
                    f"file goes back to it{way}")


def _said(key: str, value) -> str:
    if value is None:
        return "none"
    return f"{value[0]}×{value[1]}" if key == "size" else repr(value)


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
    # An equation's LaTeX is what no `documents.get` says (only the export does, and
    # only the settling read asks it: `doc_sync.equation_latex`), so a read's equation
    # is blank where the file's and the base's hold its LaTeX, and would look changed.
    # A block with as many equations as its namesake gets theirs, in order.
    latex = {}
    for source in reversed(sources):   # the base first: it is the last read
        for block in source["blocks"]:
            said = [r.get("text", "") for r in _equations(block)]
            if block.get("key") and any(said):
                latex.setdefault(block["key"], said)
    for block in live["blocks"]:
        runs, said = list(_equations(block)), latex.get(block.get("key"))
        if said and len(said) == len(runs):
            for run, text in zip(runs, said):
                if not run.get("text"):
                    run["text"] = text
    return live


def _equations(block: dict):
    """A block's equation runs in order, those in its table cells included."""
    for run in block.get("runs", []):
        if run.get("chip") == "equation":
            yield run
    for row in block.get("rows", []):
        for cell in row:
            for inner in cell:
                yield from _equations(inner)


def tidy_requests(live: dict) -> list[dict]:
    """What a sync writes after the merge so the document reads back as it looks:
    the styling no import could carry (`carry_unimported`), bullets for the lists it
    cannot describe, and a plain paragraph after a final table that took over a list
    item's glyph or a heading's style.

    The unimportable styling goes first, for the same reason bullets go last: a
    paragraph-wide style request restyles the glyph with the words. A bullet the
    settle *takes off* goes before it, though, which is the order `_paragraph_requests`
    writes as well: `deleteParagraphBullets` keeps the nesting by adding indent of its
    own, so a style written before it is a style the delete then edits.
    """
    out = (restore_bullets(live, taking_off=True) + unimported_requests(live)
           + restore_bullets(live, taking_off=False) + bullet_requests(live))
    for part in ("trailer", "lead"):
        if live.get(f"{part}_kind"):
            start, end = live[part]
            span = {"startIndex": start, "endIndex": end}
            out += [{"deleteParagraphBullets": {"range": span}},
                    {"updateParagraphStyle": {"range": span, "fields": "namedStyleType,alignment",
                                              "paragraphStyle": {"namedStyleType": "NORMAL_TEXT",
                                                                 "alignment": "START"}}}]
    return out


def unimported_requests(live: dict) -> list[dict]:
    """The styling `carry_unimported` found missing, written with `batchUpdate`.

    A paragraph's own span for the shading and the space around it; the exact
    stretch of words for small caps and for a raised or lowered run, which is why
    those ranges were measured character by character and not run by run; and the
    named style, for a Title or a Subtitle, which the importer flattens to body text
    whatever the file says (`class="title"` reaches nothing).
    """
    out = []
    for block in live["blocks"]:
        want = block.get("unimported")
        if not want:
            continue
        if want.get("whole"):
            # A paragraph this run wrote: the whole style the plan asked for, fields
            # named whether or not it asks for them, so what a write left on the
            # block and nobody asked for goes away with the rest (`carry_unimported`).
            style, fields = want["whole"]["style"], want["whole"]["fields"]
            out.append({"updateParagraphStyle": {
                "range": {"startIndex": block["span"][0], "endIndex": block["span"][1]},
                "paragraphStyle": style, "fields": fields}})
        elif want["paragraph"] or want.get("named"):
            style = {api: (doc_ir.TO_ALIGNMENT[want["paragraph"][key]] if key == "align"
                           else _paragraph_value(key, want["paragraph"][key]))
                     for key, api in PARAGRAPH_KEYS if key in want["paragraph"]}
            if want.get("named"):
                style["namedStyleType"] = want["named"]
            out.append({"updateParagraphStyle": {
                "range": {"startIndex": block["span"][0], "endIndex": block["span"][1]},
                "paragraphStyle": style, "fields": ",".join(sorted(style))}})
        for start, end, style in want["runs"]:
            out.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": style, "fields": ",".join(sorted(style))}})
    return out


def restore_bullets(live: dict, taking_off: bool) -> list[dict]:
    """Put back the bullet a write took off a block, or take off one it put on.

    `carry_unimported` found it. The half that creates one comes after the paragraph
    styling, because a paragraph-wide style request restyles the glyph with the
    words, and before `bullet_requests`, which looks at what the read-back says a
    list is and would not see this block in the run at all; the half that takes one
    off comes first, `deleteParagraphBullets` being a request that writes indents of
    its own (`tidy_requests`).
    """
    out = []
    for block in live["blocks"]:
        want = (block.get("unimported") or {}).get("bullet")
        if want is None or not block.get("span") or (want == "none") != taking_off:
            continue
        span = {"startIndex": block["span"][0], "endIndex": block["span"][1]}
        out.append({"deleteParagraphBullets": {"range": span}} if want == "none" else
                   {"createParagraphBullets": {
                       "range": span, "bulletPreset": BULLETS[want == "ordered"]}})
    return out


def bullet_requests(live: dict) -> list[dict]:
    """Bullets of the document's own for the lists it cannot describe.

    Measured: `createParagraphBullets` over a list the importer built replaces its
    unspecified glyphs with real ones — a `glyphSymbol` for bullets, a `glyphType`
    for numbers — and keeps every item's nesting level. From then on the list reads
    back as what it is, so a reader who switches it from bullets to numbers in the
    toolbar is seen doing so, where before the file silently won. One request per run
    of neighbouring items that agree on being numbered.

    What counts as an item is what the block will be **once this settle has
    finished**, not what the read-back says: the settle may be taking a bullet off it
    (`restore_bullets`), and a range reaching over such a block puts the bullet
    straight back on — and, being a paragraph-wide request, flattens the named style
    the settle wrote in the same breath. A paragraph the source retitled behind an
    item the source deleted came out a plain bulleted line, the whole repair undone
    by the request after it (offline chain-4 seed 570181).
    """
    out, run = [], []
    for block in live["blocks"] + [{"kind": "end"}]:
        item = _settles_as_item(block)
        if (run and (not item or not block.get("span")
                     or block.get("ordered") != run[0].get("ordered"))):
            if any(b.get("guessed") for b in run):
                out.append({"createParagraphBullets": {
                    "range": {"startIndex": run[0]["span"][0], "endIndex": run[-1]["span"][1]},
                    "bulletPreset": BULLETS[bool(run[0].get("ordered"))]}})
            run = []
        if item and block.get("span"):
            run.append(block)
    return out


def _settles_as_item(block: dict) -> bool:
    """Whether a read-back block is a list item once the settle has written what
    `carry_unimported` found: a bullet it is about to take off is already gone."""
    if (block.get("unimported") or {}).get("bullet") == "none":
        return False
    return block.get("kind") == "item"


def styles_of(block: dict) -> tuple:
    """A block's styling, run by run, without its words.

    What changes when somebody marks a word bold or takes a colour away — and not
    when somebody rewrites a word, which is the text merge's business. A mark applied
    to part of a run splits it, so the run count carries the boundaries.
    """
    return tuple(("chip", frozenset()) if r.get("frozen") else
                 ("text", frozenset((k, v) for k, v in r.items() if k not in ("text", "width")))
                 for r in block.get("runs", []))


def marks_of(block: dict) -> tuple:
    """What a block's words are marked with: the chips left out, alike runs joined.

    `styles_of` answers "is this block styled exactly as it was", boundaries and
    all. Asked of the *document* it answers something else as well, and that was
    the defect: a reader who inserts a picture or a person chip splits a run in
    two and puts a frozen one between the halves, which is content and not a
    mark — the text merge carries it — yet the merge read it as the reader
    restyling the block, decided both sides had, and dropped the source's marks
    with nobody told. A boundary with the same marks either side says nothing
    about marks, and a frozen run says nothing about them at all.

    So this is the question "did somebody change what these words are marked
    with", and `styles_of` stays the question "is this block's styling untouched"
    that a rewrite has to ask (`_merge_block`'s frozen-content path).

    It answers it too coarsely to answer it alone: with the words gone there is
    nothing left to say *where* a mark starts, so a bold that grew leftwards over
    the word before it reads as the same alternation of mark sets it always was.
    `_remarked` is the other half, and `_merge_block` asks both.
    """
    out: list[frozenset] = []
    for run in block.get("runs", []):
        if run.get("frozen"):
            continue
        marks = frozenset((k, v) for k, v in run.items() if k not in ("text", "width"))
        if not out or out[-1] != marks:
            out.append(marks)
    return tuple(out)


def _remarked(was: dict, now: dict) -> bool:
    """Whether a word both blocks have is marked differently in them.

    `marks_of` drops the words to stay deaf to a chip splitting a run, and drops
    the boundaries with them: two blocks whose bold begins at different words read
    alike whenever the alternation of mark sets is the same. So a source that took
    its bold one word further left — `((), 'the value  holds '), (bold,
    'everywhere')` becoming `((), 'the value '), (bold, ' holds everywhere')` —
    asked for a restyle the merge could not see, and the sync wrote nothing and
    said nothing (offline chain-10 seed 2040246, shape `equations`).

    The words are what carries the boundary, so the question is asked of them, and
    only of the ones both sides have: a word one side typed or deleted is the text
    merge's business, and reading a changed word as a changed mark is exactly the
    noise `marks_of` exists to keep out — a reader who bolds a word while the
    source rewords the same paragraph would come back as both sides restyling it.
    `_restyled_words` asks its own half of this the same way and for the same
    reason.
    """
    was_styles, now_styles = _char_styles(was), _char_styles(now)
    now_text = block_text(now)
    return any(now_text[i] != FROZEN and now_styles[i] != was_styles[w]
               for w, i in _word_pairs(block_text(was), now_text))


def _shape(block: dict) -> tuple:
    """The part of a block that is not its words.

    The paragraph's own measurements count: a source that changes nothing but a
    block's line spacing has changed the block, and this is what says so, so that
    `_restyle_requests` writes the paragraph again.
    """
    return tuple(block.get(key) for key in SHAPE_KEYS)


def _take_shape(out: dict, mine: dict) -> dict:
    """Give a block the source's shape — the whole of it, absences included.

    A dict update can only add: for as long as this was one, a source that took a
    block's centring (or its shading) away left the document centred for good,
    because the key it no longer writes said nothing at all.
    """
    for key in SHAPE_KEYS:
        if key in mine:
            out[key] = mine[key]
        else:
            out.pop(key, None)
    return out


def _merged_shape(out: dict, was: dict, mine: dict, live: dict, a_block: bool,
                  key: str, notes: list) -> dict:
    """How a block is *set*, merged the way its words are: the source's where the
    document left it alone, the document's where both sides changed it, and a note
    saying so when they did.

    One rule in one place, because there are two ways into a block. The ordinary one
    merges the words and settles the shape beside them; the other rewrites the block
    from the file, because the source changed a chip or a picture and no request can
    edit one — and that one used to take the source's shape outright, on the grounds
    that a document which left the block's words, run styles and frozen runs exactly
    as the base has them "has nothing of its own in it". It may have: the reader
    centred the paragraph, or indented it, or made it a heading, without touching a
    word. Every one of those went, in silence, the first time the source so much as
    moved a chip in that block — 7 of 200 rounds at chain 6 once
    `doc_loss_oracle._shape_findings` was written to ask (seed 2000188 shrinks to the
    whole of it: the reader spaces a paragraph out, the source adds a chip to it).
    """
    if _shape(mine) != _shape(was):
        if _shape(live) == _shape(was):
            _take_shape(out, mine)
        elif a_block:
            # Both sides set the paragraph differently. The document wins, as it does
            # everywhere — but it used to win in silence, and a source that centres a
            # paragraph or gives it a rule has said something a person will look for.
            # The words have had this since the beginning (`diff3` raises a conflict on
            # every clash); the styling, which is settled the same way, said nothing.
            notes.append(f"{key}: both sides changed how the paragraph is set — the "
                         f"document's setting is kept")
    return out


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

    A key the file itself asserts is never matched again. `doc_ir.key_blocks` says
    why: a key from the canonical file or from a named range outranks a guess from
    the text. This used to break that rule for every block at once — `ours` in a
    plan is the file, every block of it already keyed by its own `id=` — and
    matching them all again by their words handed one block's key to another
    whenever two of them read alike. Two blocks then stood under one key and none
    under the other, and the merge read the second as "the source dropped it": it
    deleted the paragraph the reader was reading (`crossed-delete`), or wrote the
    file's words over a block that was holding a picture or a chip and took it with
    them (`crossed-frozen`), or at best left the block alive under a name nobody
    meant (`lost-key`). Two wordless blocks were enough to do it.

    What is left for the matching is what the file does *not* name: a block somebody
    wrote into the HTML by hand, and every block of a document just imported, which
    is the case `push` calls this for.
    """
    asserted = [block.get("key") for block in ours["blocks"]]
    doc_ir.key_blocks(ours)
    taken: set[str] = {key for key in asserted if key}
    free = [b for b in base["blocks"] if b.get("key") and b["key"] not in taken]
    by_text: dict[tuple, list] = {}
    for block in free:
        by_text.setdefault((_match_shape(block), _match_text(block)), []).append(block)
    pending = []
    for block, mine in zip(ours["blocks"], asserted):
        if mine:
            continue
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


def recover_tables(base: dict, theirs: dict, spoken_for: set | None = None) -> int:
    """Give back the key of a table whose anchor the *reader* deleted in the browser.

    A table is anchored in its first cell (`doc_ir.anchor_span`), because the cells
    are the only text a table has of its own — so a reader who deletes its first row,
    or its first column, or merely the words in that cell, takes its named range with
    them. Every other repair in this file is for a range one of *our own* writes
    destroyed (`anchor_tables`, `adopt_keys`, `settle_keys`); this one is for a range
    a person destroyed between two syncs, and nothing was looking at that.

    What it cost: the read-back had a table with no key, the merge read the key the
    file and the base both name as a table the reader had deleted, and the whole table
    went — the rows the reader kept with it, and the report said nothing (fresh seed
    970567 at chain 6, shape `between_tables`, shrunk to a reader's row delete and a
    source's added table).

    Only where nothing is in doubt, which is this family's rule throughout: a base
    table missing from the read-back and an unkeyed table in it pair when they are
    each other's best match, above `TABLE_MATCH` and `TABLE_MARGIN` clear of the
    runner-up on both sides. So a reader who deleted one table and built another keeps
    both of those facts, and two tables that read alike pair with neither.

    And a table with **no words at all** is nobody's candidate, on either side, which
    is the same rule taken to its limit: two empty strings are each other's perfect
    match, so a base table that never said anything paired at 1.0 with the blank table
    `insertTable` had just built in this very batch — before `anchor_tables` ran, which
    is the pass that knows about that one. The reader's table, beheaded and holding the
    only word either of them had, was then the one free table left for the new table's
    key: the merge saw its own blank grid where the reader's table should be, deleted
    the reader's table to rebuild it and the word went (chain-12 seed 730384, shape
    `titled`). This is a pairing on words, so a side with no words is not a pairing.

    And a key **this batch is about to place itself** is not one to give away
    (`spoken_for`): after the structural write both repairs look at the same free
    tables, and where the same table was regridded by us and beheaded by the reader
    they can cross. A regrid that deletes a table's first column leaves it saying
    almost nothing, while the reader's beheaded table next to it still says most of
    what the base recorded — so the regridded table's key scored higher on the
    reader's table than on its own remnant and took it, `anchor_tables` then found
    its key already placed and left the remnant unnamed, and the source's own regrid
    went nowhere (chain-8 seed 870368, shape `two_tables`). The batch knows where it
    put its tables; this pass only guesses, so it goes second on those.
    """
    claimed = {block.get("key") for block in theirs["blocks"]}
    missing = [b for b in base["blocks"]
               if b.get("kind") == "table" and b.get("key") and b["key"] not in claimed
               and b["key"] not in (spoken_for or ()) and _table_words(b)]
    free = [b for b in theirs["blocks"]
            if b.get("kind") == "table" and not b.get("key") and _table_words(b)]
    if not missing or not free:
        return 0
    # The words, not `_match_text` — whose " | " between every cell is most of a
    # small table's characters, so a table `insertTable` had just built out of
    # nothing scored 0.55 against one with four words in it.
    score = {(i, j): SequenceMatcher(None, _table_words(was), _table_words(now)).ratio()
             for i, was in enumerate(missing) for j, now in enumerate(free)}
    done = 0
    for i, was in enumerate(missing):
        row = sorted(((score[i, j], j) for j in range(len(free))), reverse=True)
        best, j = row[0]
        column = sorted(score[k, j] for k in range(len(missing)))
        if best < TABLE_MATCH or best != column[-1]:
            continue
        if len(row) > 1 and best - row[1][0] < TABLE_MARGIN:
            continue
        if len(column) > 1 and best - column[-2] < TABLE_MARGIN:
            continue
        free[j]["key"] = was["key"]
        done += 1
    return done


def anchor_tables(live: dict, shaped: list[dict]) -> int:
    """Name the tables the structural batch created, so the next plan knows them.

    A table `insertTable` built carries no named range, and a read cannot tell it from
    one a reader made in the browser. It is found by what it follows: the block the
    plan put it after, which the document already had.

    One of those anchors may be a table this very batch has just stripped of its key —
    a regrid that deletes the row a table is anchored in (`doc_ir.anchor_span`) is in
    `shaped` for exactly that reason. So the pass runs to a fixed point, and a told
    whose anchor is already there goes first in each: a moved table anchored on the
    regridded one used to be skipped, the regrid took its key back a moment later,
    and nothing looked again. The moved table then stayed blank and unkeyed, the
    re-plan read the key the file still names as a table the reader had deleted, and
    the sync wrote the source's words nowhere (fresh-seed 90190, shape
    `between_tables`).

    Two tables may also share one anchor — a table the source adds in front of one it
    regrids — and then `shaped`'s order decides which is which, although what the
    batch did with them is the requests' order and not that one. They came out
    crossed: the regridded table's key went on the blank table `insertTable` had just
    built and the new table's key on the one with all the words in it, the base took
    each other's content, and the next round "moved" the table it had just named,
    which emptied the real one (fresh-seed 40204, shape `ends_on_table`). What tells
    them apart is that a table built from nothing is blank and a regridded one still
    says what it said, so the words are asked first and `shaped`'s order is only the
    tie-break it always was.
    """
    done = 0
    todo = [told for told in shaped if "after" in told and told["key"]]
    moved = True
    while moved:
        moved = False
        for told in sorted(todo, key=lambda t: t["after"] is None):
            if any(b.get("key") == told["key"] for b in live["blocks"]):
                continue
            start = 0
            if told["after"] is not None:
                at = next((i for i, b in enumerate(live["blocks"])
                           if b.get("key") == told["after"]), None)
                if at is None:
                    continue
                start = at + 1
            # Forward from the anchor first, and then *backwards* — because
            # `insertTable` splits the paragraph it goes into, and the paragraph's
            # named range stays with the half after the table. So a table inserted
            # at its anchor's own index lands in front of the block the plan
            # anchored it on, and looking only forward found the next table along:
            # the one the reader had just made anonymous by deleting the row it was
            # anchored in. Its key went onto the new table, and the sync wrote the
            # source's rows into the reader's table and built a blank one for the
            # rest (fresh seed 970567, shape `between_tables`, where the empty
            # paragraph between two tables cannot be deleted and so stays).
            free = [b for b in live["blocks"][start:]
                    if b.get("kind") == "table" and not b.get("key")]
            free += [b for b in reversed(live["blocks"][:start])
                     if b.get("kind") == "table" and not b.get("key")]
            # And where this batch wrote the table's grid, that grid is what the
            # table it belongs to has: a free table of another shape is not it,
            # whatever it follows. The reader can behead a table too — deleting the
            # row it is anchored in, in the browser, which `recover_tables` refuses
            # to pair when it is not sure — and then the batch's own beheaded table
            # is not the only unkeyed one on the page. Taking the first free table
            # after the anchor put the regridded table's key on the reader's table
            # and left the reader's rows standing where the sync had just written
            # two of its own (chain-6 seed 790329, shape `two_tables`). Claiming
            # nothing is the safe half of it: the key comes back at the re-plan,
            # where `recover_tables` has both tables in front of it at once and the
            # words tell them apart.
            want = _built_size(told) if told.get("lines") else None
            if want is not None:
                free = [b for b in free if _size(b.get("rows", [])) == want]
            built = not (told.get("ops") or told.get("lines"))
            found = next((b for b in free if _blank_table(b) == built), None) \
                or (free[0] if free else None)
            if found is not None:
                found["key"] = told["key"]
                done += 1
                moved = True
    return done


def recover_swallowed(live: dict, shaped: list[dict]) -> int:
    """Give back the key a new table's swallow took off an empty paragraph.

    `_new_table_requests` gets rid of the empty paragraph `insertTable` leaves by
    deleting the mark of the block in front of it, and a block that is *itself* an
    empty paragraph is all mark: the delete covers its named range whole and Docs
    drops it (`_swallowed`). Being keyed again from its words at the settle is not
    soon enough, because a structural batch is followed by a **re-plan** against the
    document it has just written — and there the file's key names nothing, so the
    block reads as one the reader deleted and everything the source asked of it is
    dropped in silence. A chip the source put in the paragraph between two tables
    went that way, the round converging with file, base and document all agreeing on
    a paragraph with nothing in it (offline chain-4 seed 330127).

    The survivor is the empty paragraph standing right in front of the table whose
    insert swallowed it: the two are merged into one, and that one keeps neither's
    name. So this runs after `anchor_tables`, which is what gives that table its key.

    It need not read as a *paragraph*: Docs' merge keeps the first one's style, so
    an empty block wearing a named style hands it to the survivor and that one comes
    back a subtitle, a heading, a title. Asked for the kind, this recovered nothing
    there, and the one thing the source was asking of that block — to stop being a
    subtitle — went nowhere, the settle keying it from its words to the very name it
    had and file, base and document agreeing on it (offline chain-8 seed 1270233,
    shape `between_tables`). Anything but a structural element, then: what makes it
    the survivor is that it is empty, unnamed and right in front of the table.
    """
    have = {b.get("key") for b in live["blocks"] if b.get("key")}
    done = 0
    for told in shaped:
        key = told.get("swallowed")
        if not key or key in have:
            continue
        at = next((i for i, b in enumerate(live["blocks"])
                   if b.get("key") == told["key"]), None)
        if not at:                      # not found, or nothing in front of it
            continue
        before = live["blocks"][at - 1]
        if before.get("key") or _structural(before) or _match_text(before).strip():
            continue
        before["key"] = key
        have.add(key)
        done += 1
    return done


def recover_eaten(live: dict, shaped: list[dict]) -> int:
    """Give back the key the delete of a body's last table ate off the empty
    paragraph in front of it: the trailer takes that paragraph's place.

    A table that ends the body is deleted with the paragraph mark in *front* of it,
    or the empty trailer behind it would be left standing as a second paragraph
    (`_delete_range`, measured). The block that mark belongs to keeps its words, and
    so its named range — unless it is *itself* an empty paragraph, which is all mark:
    then the delete covers the range whole and Docs drops it, exactly as a new
    table's swallow does (`_swallowed`). The block the merge means to keep is
    therefore gone from the re-plan that follows the structural batch, where it reads
    as one the reader deleted, and everything the source asked of it goes in silence:
    an empty paragraph the source had made a heading (offline chain-16 seed 1730265,
    shape `tabs`) and an empty list item the source moved past the table in front of
    it, which came back between the two tables under a name of its own and left the
    file's order unreached (offline chain-14 seed 1740158, the same shape).

    The survivor is the trailer. With the table in front of it gone, that empty
    paragraph *is* the block — at the end of a body there is nowhere else for one to
    be — and `doc_ir._hide_trailer` leaves a paragraph somebody has planted an
    identity on out of the scaffolding, so the name is what makes it a block again.
    Where the batch left the body ending on something other than a table the trailer
    is no longer hidden and stands there as a plain empty block; that one is the same
    paragraph and takes the key in place.

    After `anchor_tables`, as `recover_swallowed` is: a table anchored on the block
    this recovers must be found by the name the batch really left behind.
    """
    have = {b.get("key") for b in live["blocks"] if b.get("key")}
    done = 0
    for told in shaped:
        key = told.get("eaten")
        if not key or key in have:
            continue
        block, span = _trailing_empty(live)
        if block is None and span is None:
            continue
        if block is not None:
            block["key"] = key
        else:
            live["blocks"].append({"kind": "paragraph", "runs": [], "key": key,
                                   "span": list(span)})
            live.pop("trailer", None)
            live.pop("trailer_kind", None)
        have.add(key)
        done += 1
    return done


def _trailing_empty(live: dict) -> tuple[dict | None, list | None]:
    """The empty paragraph the body ends on: the hidden trailer, or — where the body
    no longer ends on a table — the unnamed block that same paragraph reads as."""
    if live.get("trailer"):
        return None, list(live["trailer"])
    last = live["blocks"][-1] if live["blocks"] else None
    if last is not None and not last.get("key") and not _structural(last) \
            and not _match_text(last).strip():
        return last, None
    return None, None


def _table_words(block: dict) -> str:
    """A table's cells as one stretch of words, with nothing of the grid in it."""
    return " ".join(block_text(inner) for row in block.get("rows", [])
                    for cell in row for inner in cell).strip()


def _blank_table(block: dict) -> bool:
    """Whether a table says nothing at all — which is how `insertTable` leaves one."""
    return not _match_text(block).strip(" |")


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
        if at is None or told.get("moved"):
            # Built from nothing — a table the source added, or one it moved, which
            # was deleted where it stood: the base has it where the document now does,
            # and as blank as it is there.
            fresh = theirs["blocks"][index]
            if told.get("moved") and told.get("lines"):
                fresh = _moved_table(fresh, told["lines"])
            if at is not None:
                del blocks[at]
            blocks.insert(_place(theirs, blocks, index), fresh)
        elif told.get("lines"):
            blocks[at] = _rebased_table(blocks[at], told["lines"], theirs["blocks"][index])
        elif told.get("ops"):
            blocks[at] = _regridded(blocks[at], told["ops"])
    return dict(base) | {"blocks": blocks}


def _rebased_table(was: dict, lines: dict, now: dict) -> dict:
    """The base's table on the grid that was just written, and how its lines match.

    A row the source added is in it, blank; a row the document added is not, since
    nothing agreed on it; the rest keep the base's words. `aligned` records which of
    its lines is which line of the document's new grid and of the file, because
    that is known here for certain and would only be guessed again from the words —
    and, with them, the file's lines this table no longer has room for
    (`_table_lines`), which are exactly the ones no line of the rebased base can
    speak for.
    """
    names = ("row", "column")
    kept = {name: [line for line in lines[name] if not line.gone] for name in names}
    agreed = {name: [line for line in kept[name] if line.was is not None or line.mine is not None]
              for name in names}
    rows = [[[dict(b) for b in (_cell(was, row.was, column.was) or _blank_cell())]
             for column in agreed["column"]] for row in agreed["row"]]
    aligned = {"live": _size(now.get("rows", [])), "mine": lines["mine"]}
    for name in names:
        aligned[f"{name}_dropped"] = lines.get("dropped", {}).get(name, [])
        where = {id(line): k for k, line in enumerate(kept[name])}
        aligned[f"{name}_live"] = [(i, where[id(line)]) for i, line in enumerate(agreed[name])]
        aligned[f"{name}_mine"] = [(i, line.mine) for i, line in enumerate(agreed[name])
                                   if line.mine is not None]
    return dict(was) | {"rows": rows, "aligned": aligned}


def _moved_table(now: dict, lines: dict) -> dict:
    """The base's table for one this sync moved: as blank as the document has it, and
    with the matching a regrid records.

    A move is a delete and a table built again, so the base can only be the blank grid
    the document now shows — the base's own words are nowhere any more. But the grid
    that was built is the *merged* one, which is not the file's whenever the document
    had a hand in it, and a blank row matches nothing by its words: without the
    matching, the round after reads the file's extra line as one the source has just
    added and puts back the row a reader deleted. Its rows are the document's, so each
    stands for the merged line of the same number.
    """
    names = ("row", "column")
    kept = {name: [line for line in lines[name] if not line.gone] for name in names}
    aligned = {"live": _size(now.get("rows", [])), "mine": lines["mine"]}
    for name in names:
        aligned[f"{name}_dropped"] = lines.get("dropped", {}).get(name, [])
        aligned[f"{name}_live"] = [(i, i) for i in range(len(kept[name]))]
        aligned[f"{name}_mine"] = [(i, line.mine) for i, line in enumerate(kept[name])
                                   if line.mine is not None]
    return dict(now) | {"aligned": aligned}


def _regridded(was: dict, ops: list[tuple]) -> dict:
    """The base's table with the rows and columns that were just written in it."""
    rows = [list(row) for row in was.get("rows", [])]
    for line, how, at in sorted(ops, key=_op_order):
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

    Being the one place that holds the plan and the read-back side by side, this is
    also where `carry_unimported` notes the styling no import could carry.
    """
    taken = {b["key"] for b in live["blocks"] if b.get("key")}
    done = _adopt_in_order(live["blocks"], planned, taken)
    free: dict[tuple, list[dict]] = {}
    for block in planned:
        if block.get("key") and block["key"] not in taken:
            free.setdefault((_match_shape(block), _match_text(block)), []).append(block)
    for block in live["blocks"]:
        if block.get("key"):
            continue
        if same := free.get((_match_shape(block), _match_text(block))):
            block["key"] = same.pop(0)["key"]
            done += 1
    done += _adopt_by_words(live, free)
    carry_unimported(live, planned)
    return done


def _adopt_in_order(live: list[dict], planned: list[dict], taken: set) -> int:
    """The plan and the read-back, side by side and *in order*.

    Words alone cannot say which block is which, and a document is full of blocks
    that say the same thing: a reader who pastes a paragraph makes a second one
    word for word, and that is the one the dictionary pass below reaches first,
    because it walks the document and the pasted copy stands earlier on the page.
    So the key of the block the source had just written a chip into went to the
    reader's copy, the block that had carried that key since the push settled under
    a name made from its own words, and file, base and document all agreed on it —
    the `lost-key` shape again, with the added twist that the source's edit was
    written correctly and landed under a name nobody asked for (offline chain-6
    seed 94577, and 94465 with the chip in an empty paragraph).

    Order is the information the dictionary threw away, and there is plenty of it:
    the plan is the merged block list and the read-back is the document that was
    written from it, so the two are very nearly the same sequence. Aligned on their
    words, every block inside a matching run is paired with the one the plan meant,
    whatever its twins say. What the alignment does not cover — a block whose
    *words* the write itself mangled — is what the two passes after this one are
    for.

    On the words and not on the shape, which is what `_adopt_by_words` had already
    learnt the hard way: Docs merges two paragraphs keeping the first one's style,
    so the shape is the very thing a write changes, and a heading the source moved
    and restyled came back under neither pass — the alignment refused it for its
    shape and the words pass for its two pasted twins (offline chain-4 seed 96300,
    which fails the same way with this pass taken out). Structural elements are
    still never paired with anything else: a table is not a paragraph however alike
    they read.
    """
    a = [(b["kind"] in doc_ir.STRUCTURAL, _match_text(b)) for b in planned]
    b = [(x["kind"] in doc_ir.STRUCTURAL, _match_text(x)) for x in live]
    # The alignment is only worth listening to where the two sequences hold the same
    # number of blocks saying that thing. One plan block against three identical ones
    # in the read-back has an alignment, and it is a guess — which is the defect this
    # whole family is about. Where the counts agree, they are the same blocks.
    mine, theirs = Counter(a), Counter(b)
    done = 0
    for op, i1, _, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            continue
        for offset in range(j2 - j1):
            want, got = planned[i1 + offset], live[j1 + offset]
            if got.get("key") or not want.get("key") or want["key"] in taken:
                continue
            if mine[a[i1 + offset]] != theirs[a[i1 + offset]]:
                continue
            got["key"] = want["key"]
            taken.add(want["key"])
            done += 1
    return done


def _adopt_by_words(live: dict, free: dict) -> int:
    """A second pass on the words alone, for a block whose *shape* the write changed.

    Docs merges two paragraphs keeping the first one's style, so deleting a block
    hands the block after it the shape of the one that went: a list item under a
    deleted paragraph comes back a plain paragraph, a heading under a deleted
    subtitle comes back a subtitle. Matched on shape and words together, the very
    blocks a write mangles are the ones that can never be adopted — and the repair
    that would put the shape back (`carry_unimported`) is itself keyed by the key
    this pass restores, so the two failures hold each other up: the block settled
    under a name made from its new shape and its new words, and the file's key read
    as gone (offline chain-8 seeds 7034 and 7048, shrunk to one source op pair and
    no reader at all).

    Only where nothing is in doubt: one free key says those words, one block without
    a key says them, and the two agree on being a structural element or not. A guess
    here would hand one block's identity to another, which is the defect this whole
    family is about.
    """
    by_words: dict[str, list[dict]] = {}
    for (_, words), blocks in free.items():
        by_words.setdefault(words, []).extend(blocks)
    orphans: dict[str, list[dict]] = {}
    for block in live["blocks"]:
        if not block.get("key"):
            orphans.setdefault(_match_text(block), []).append(block)
    done = 0
    for words, mine in by_words.items():
        theirs = orphans.get(words, [])
        if len(mine) == 1 and len(theirs) == 1 and (
                (mine[0]["kind"] in doc_ir.STRUCTURAL)
                == (theirs[0]["kind"] in doc_ir.STRUCTURAL)):
            theirs[0]["key"] = mine[0]["key"]
            done += 1
    return done


def settle_keys(live: dict, planned: dict, base: dict | None = None) -> None:
    """Give every part the keys the plan meant it to have, then key what is left.

    Two passes and not one, which is the whole of it: `doc_ir.key_blocks` recurses
    into the tabs, because a key belongs to its tab, so adopting and keying one part
    at a time let the *first* part's keying name every later tab's blocks after
    their words — and `adopt_keys`, reaching that tab afterwards, found them keyed
    and left them alone.

    What that cost: a block whose named range the write had taken with it came back
    under a name made from its new text. A table is anchored in its first cell
    (`doc_ir.anchor_span`), so a source that rewords that cell destroys the range
    every time; the table then settled as `table:<its new first word>`, and the
    file, the base and the document all agreed on an identity the file never gave
    it. `planned` is by stamp — None for the first part, else its tab id — which is
    `doc_sync.stamp_of`'s rule.

    With `base`, a table whose anchor the *reader* destroyed is known again here too
    (`recover_tables`), and then `doc_ir.name_requests` plants its range back, so the
    repair reaches the document and not only this run's plan. The merge does the same
    on the tabs it plans; this is for the ones it does not — a tab the source deleted
    and the document kept is a note and no plan at all (`pair_tabs`), so a table the
    reader had beheaded in it settled under a name made from its surviving first
    word, and the next sync would read the file's key as gone and build a second
    table beside it (offline chain-6 seeds 970705 and 970711).
    """
    parts = doc_ir.parts(live)
    for part in parts:
        stamp = None if part is live else part.get("tab")
        if planned.get(stamp):
            adopt_keys(part, planned[stamp])
    if base:
        was = {None if p is base else p.get("tab"): p
               for p in doc_ir.parts(base) if p.get("blocks") is not None}
        for part in parts:
            stamp = None if part is live else part.get("tab")
            if stamp in was:
                recover_tables(was[stamp], part)
    for part in parts:
        doc_ir.key_blocks(part)


def carry_unimported(live: dict, planned: list[dict]) -> int:
    """Note on each read-back block what the plan asked for that no import can write.

    Paragraph shading and the space above and below a paragraph are not in what
    Drive's importer keeps (measured: `background-color` on a `<p>` arrives as a
    character highlight on its runs, and the study's list of paragraph CSS has no
    margins), and small caps has no CSS at all; all four are written by
    `updateTextStyle` / `updateParagraphStyle`, which the study measured working.
    A `push` is an import and nothing else, so its document comes back without them:
    what is missing is recorded here and `tidy_requests` writes it, in the batch a
    settle sends anyway. After a sync this is almost always empty — the merge's own
    batch wrote those fields — and it costs one comparison to be sure.

    Only what the plan asks for and the document has not got is carried. A property
    the plan does not mention is left alone: in a read, "absent" is also what a
    reader who took the styling off looks like, and a settle must never undo that.
    """
    want = {b["key"]: b for b in planned if b.get("key")}
    done = 0
    for block in live["blocks"]:
        mine = want.get(block.get("key"))
        if mine is None or not block.get("span"):
            continue
        missing = {key: mine[key] for key in UNIMPORTABLE
                   if mine.get(key) is not None and mine[key] != block.get(key)}
        ranges = _unimportable_runs(mine, block)
        # A named style the import could not carry. Compared as the style and not as
        # the kind, so a list item the importer left a plain paragraph — both
        # NORMAL_TEXT — says nothing, and only a real difference is written. It is
        # safe against a reader who demoted a heading in the browser for the reason
        # the rest of this is: `mine` is the merged plan, and `_take_shape` gives it
        # the source's shape only where the document kept the base's.
        named = named_style(mine)
        if named == named_style(block):
            named = None
        # And the bullet, which no named style carries: a list item and a plain
        # paragraph are both NORMAL_TEXT, so the line above is blind to exactly the
        # thing a delete takes away most often. Docs merges two paragraphs keeping
        # the first one's style, so the item under a deleted paragraph comes back
        # with no bullet at all, and the settle then wrote that plain paragraph into
        # the file: the source's own list, quietly one item shorter. Safe against a
        # reader who took the bullet off in the browser for the same reason `named`
        # is — `_take_shape` gives the plan the source's shape only where the
        # document kept the base's.
        bullet = None
        if (mine["kind"] == "item") != (block["kind"] == "item"):
            bullet = ("ordered" if mine.get("ordered") else "unordered") \
                if mine["kind"] == "item" else "none"
        # And, for a paragraph this run wrote, every measurement the write did not
        # leave as the plan asked — in either direction, because Docs' merge-on-delete
        # gives as well as takes. The style goes back whole rather than as the
        # difference: the repairs would otherwise read each other's work, the named
        # style deciding what "inherited" means. A block still read as a HEADING_1
        # under a theme that centres headings reports no alignment of its own, and
        # the centring the delete above it handed over shows only once `named` has
        # written NORMAL_TEXT back — which is in this very batch (seed 912452).
        whole = None
        if (mine.get("paragraph_written") or mine.get("paragraph_merged")) \
                and (named or _unwritten(mine, block)):
            style, fields = paragraph_style(mine)
            whole = {"style": style, "fields": fields}
        if missing or ranges or named or bullet is not None or whole:
            block["unimported"] = {"paragraph": missing, "runs": ranges, "named": named,
                                   "bullet": bullet, "whole": whole}
            done += 1
    return done


def _unwritten(mine: dict, live: dict) -> list[str]:
    """Which measurements the write did not leave as the plan asked.

    Deleting a paragraph hands the block behind it the style of the one that went,
    which `carry_unimported` already repairs for the named style and the bullet. It
    reaches the measurements too, in both directions: a paragraph the reader
    centred, deleted by the source in the same batch, leaves the block after it
    centred *of its own* — although the merge had written that field named-and-unset
    one request earlier, because following its named style is the whole point of a
    theme — and the source's own restyle of that block, written in the same breath,
    is handed back the spacing of the paragraph that went (offline chain-6 seed
    912452).

    Only a paragraph this run wrote is asked about (`_paragraph_requests` says so on
    the block it writes), because putting the style back is the one thing in the
    settle that can take styling away. It is the write's own field being taken back,
    and a block nobody wrote is left alone — which keeps the rule the rest of this
    pass is built on: in a read, "absent" is also what a reader who took the styling
    off looks like, and a settle must never undo that. An item's indents are the
    list preset's and belong to neither side (`ITEM_PARAGRAPH`), so they are left
    alone whichever side is one.

    The one block nobody wrote that this run *did* change is the neighbour of one it
    deleted (`paragraph_merged`, set in `requests`): Docs merges the two and keeps
    the first one's style, so a heading standing behind a justified paragraph the
    source dropped comes out justified of its own and stops following the theme,
    and the file is regenerated from the document afterwards, so nothing ever says
    it did (offline chain-6 seed 1130023, shape `themed`). Taking that back is the
    same thing as taking back a field the write itself mangled.
    """
    return [api for key, api in PARAGRAPH_KEYS
            if api in _paragraph_fields(mine) and mine.get(key) != live.get(key)]


def _paragraph_fields(mine: dict) -> tuple:
    """Which paragraph properties the settle may write on a block. A bullet's own
    indents are the list preset's, so they belong to neither side — but the question
    is whether the block is an **item once this settle has finished**, not what the
    write happened to leave. Docs' merge-on-delete hands a block the style of the
    paragraph deleted in front of it, bullet and all, so a plain paragraph the source
    had just indented came back an item, its indent was read as the list preset's and
    left alone, and the bullet the settle took off in the same batch (`restore_bullets`)
    left it with neither (offline chain-10 seed 290010). The plan's kind decides,
    because the settle writes the bullet to match it — and the plan's kind is the
    source's only where the document kept the base's (`_take_shape`), so a reader who
    made a list item in the browser still keeps it.
    """
    return ITEM_PARAGRAPH if mine["kind"] == "item" else MANAGED_PARAGRAPH


def _unimportable_runs(mine: dict, live: dict) -> list[tuple[int, int, dict]]:
    """Where the plan wants run styling the document has none of, in its index space.

    Two things are asked, both invisible to an HTML import: small caps, which has no
    CSS at all, and a raised or lowered run, which has `<sup>` and `<sub>` — tags the
    dialect writes because they keep the file readable, and which this repairs
    whether or not Drive's importer carries them. A pass that only ever *adds* is
    what makes that safe: if the import did carry them, nothing is found and nothing
    is written.

    Character by character, because a mark on part of a run is a run boundary on one
    side and not on the other. Only a block whose words came through unchanged and
    holds nothing frozen is looked at: an equation is several index units where a
    character is one, so anywhere else an offset would be a guess.
    """
    if block_text(mine) != block_text(live) or any(
            r.get("frozen") for b in (mine, live) for r in b.get("runs", [])):
        return []
    out = []
    for key, api, value in (("smallcaps", "smallCaps", True),
                            ("script", "baselineOffset", "SUPERSCRIPT"),
                            ("script", "baselineOffset", "SUBSCRIPT")):
        want = "super" if value == "SUPERSCRIPT" else "sub" if value == "SUBSCRIPT" else True
        for start, end in _gaps(_mask(mine, key, want), _mask(live, key, want),
                                live["span"][0]):
            out.append((start, end, {api: value}))
    return out


def _mask(block: dict, key: str, want) -> list[bool]:
    return [run.get(key) == want for run in block.get("runs", [])
            for _ in range(doc_ir.utf16_len(run.get("text", "")))]


def _gaps(mine: list[bool], live: list[bool], at: int) -> list[list[int]]:
    """The stretches the plan asks for and the read-back has not got."""
    out, start = [], None
    for spot, want in enumerate(mine):
        if want and not live[spot]:
            start = at + spot if start is None else start
        elif start is not None:
            out.append([start, at + spot])
            start = None
    if start is not None:
        out.append([start, at + len(mine)])
    return out


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
            if _edited(block, was):
                notes.append(f"{key}: dropped by the source but edited in the document — kept")
                merged.append(dict(block) | {"origin": "kept over a source delete"})
            elif not _writable_block(block):
                # And so does content no request could ever make again: the rewrite path
                # in `_merge_block` refuses to delete-and-write such a block, and a plain
                # delete is the same loss with nothing written back. The source's author
                # is told and can take it out in the document, where it is theirs to lose.
                notes.append(f"{key}: dropped by the source but holds an equation, a "
                             f"dropdown or a table of contents no request can make "
                             f"again — kept")
                merged.append(dict(block) | {"origin": "kept over a source delete"})
            continue
        merged.append(_merge_block(was, mine, block, conflicts, notes))

    # The source's moves before its additions, because an addition is placed after the
    # block the file puts it behind and a move does not carry what stands behind it: a
    # picture added behind a list item the same source moved was left where the item had
    # been, and then the block after *it* read as standing in its place already, so its
    # own move was refused as one whose two ends are one place — the order came out
    # neither side's and nothing said so (offline chain-6 seed 400186). A block still to
    # be added is no anchor either way: `_place` looks further back for one that is
    # there, which is where the file's order puts it all the same.
    _apply_source_moves(base, ours, theirs, merged, notes)
    for index, block in enumerate(ours["blocks"]):
        key = block.get("key")
        if key in theirs_by or (key in base_by and key not in theirs_by):
            continue  # already placed, or deleted in the document: the delete stands
        # Whatever span this block had is an index into a different document: drop it,
        # and let `origin` be what says this one has to be written from nothing.
        fresh = {k: v for k, v in block.items() if k != "span"}
        merged.insert(_place(ours, merged, index), fresh | {"origin": "added by the source"})
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

    A move whose two ends are one place is no move, and writing it is destructive:
    the guard at the bottom is what says so.
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
    sides = {name: {b.get("key"): b for b in side["blocks"]}
             for name, side in (("was", base), ("mine", ours), ("live", theirs))}
    stuck: set[str] = set()          # moves refused, and the blocks that follow them
    for key in _moved_keys(was, mine):
        block = placed[key]
        # A move is written as a delete and a fresh insert, so it can only carry what
        # an insert can write: not a chip no request creates, and a table only when
        # the document has nothing of its own in it — it is built again blank, and
        # its words are the file's.
        if block.get("kind") == "table":
            # Built again from the *merged* grid, never from the file's. The two are
            # the same table until this very sync writes the grid: a regrid goes in a
            # batch of its own, the base takes the new grid (`rebase_tables`), and the
            # round after it plans the move — with a file that still holds the row the
            # reader deleted. Building from the file puts that row back.
            build = _built_size(block)
            if build is None \
                    or not _table_movable(sides["was"][key], sides["live"][key]):
                notes.append(f"{key}: the source moved the table, but the document changed "
                             f"it or it holds a chip — left where the document has it")
                stuck.add(key)
                continue
            block["build"] = build
        elif not _writable_block(block):
            notes.append(f"{key}: the source moved it, but a block with an equation-like "
                         f"chip in it cannot be written from nothing — left where the "
                         f"document has it")
            stuck.add(key)
            continue
        was_at = merged.index(block)
        merged.remove(block)
        index = next(i for i, b in enumerate(ours["blocks"]) if b.get("key") == key)
        # A source that moves a section moves its blocks one by one, and each is
        # placed after the one the file puts it behind. Where that one's own move was
        # just refused it is not where the file has it, so this block does not go
        # where the file has it either: it follows its anchor to wherever the document
        # keeps it, and stays put altogether when the anchor is the block in front of
        # it. The report names the block that could not move; that the rest of the
        # section went with it is a consequence nobody could read off that line, so it
        # is said too — and the next block along is stuck on this one in its turn
        # (offline chain-10 seed 450252: a table the reader had regridded, once with
        # the empty paragraph behind it and once with a paragraph sent past it).
        anchor = next((b.get("key") for b in reversed(ours["blocks"][:index])
                       if any(m.get("key") == b.get("key") for m in merged)), None)
        where = _place(ours, merged, index)
        merged.insert(where, block)
        if anchor in stuck:
            notes.append(f"{key}: the file puts it after {anchor}, which stays where the "
                         f"document has it — so this one goes there too, and not where "
                         f"the file has it")
            stuck.add(key)
        if where == was_at:
            # The file asks for a move whose two ends are one place. `_moved_keys`
            # reads the file against the *base*, and the merged order is the
            # document's, so a block the source moved can come out exactly where the
            # document already has it. Writing it anyway is a delete and a build from
            # nothing, which for a table throws its words away for a round — and,
            # nothing having changed, the next round asks for the same move again,
            # until `_write_structure`'s three rounds are spent and the table is left
            # blank (the campaign's 'block_gone', seed 501429).
            continue
        block["moved"] = True


def _table_movable(was: dict, live: dict) -> bool:
    """Whether a table can be deleted and built again with nothing lost: the
    document's cells say what the base's do, and nothing in them is frozen. That the
    grid to build is whole rows is the caller's question, since what it builds is the
    merged grid and not the file's."""
    cells = [b for row in live.get("rows", []) for cell in row for b in cell]
    return (_texts(was.get("rows", [])) == _texts(live.get("rows", []))
            and not any(r.get("frozen") for b in cells for r in b.get("runs", [])))


def _moved_keys(was: list[str], mine: list[str]) -> list[str]:
    """The fewest blocks whose move turns one order into the other, in the file's order.

    Whatever the longest run of blocks that kept their order is, stays; the rest are
    what somebody moved. Taking it the other way round — moving everything that is
    not where it was — would rewrite a whole document because its first paragraph
    went to the end.

    "The longest run" is the longest common *subsequence*, not `SequenceMatcher`'s
    matching blocks, which are contiguous: a closing paragraph sent past a list made
    every block in front of it read as moved instead — four moves where one would do,
    and the table among them, which the reader had regridded, so that move was refused
    and the order came out neither side's with nothing saying so (offline chain-6 seed
    400186). Both sides hold the same keys, so the subsequence is the longest run of
    the file's blocks whose places in the base keep going up.
    """
    place = {key: at for at, key in enumerate(was)}
    rising = [(at, place[key]) for at, key in enumerate(mine) if key in place]
    tails: list[int] = []            # the smallest end a run of each length has
    ends: list[int] = []             # and which block of `mine` that end is
    came: dict[int, int] = {}
    for at, value in rising:
        length = bisect_left(tails, value)
        came[at] = ends[length - 1] if length else -1
        if length == len(tails):
            tails.append(value)
            ends.append(at)
        else:
            tails[length], ends[length] = value, at
    kept, at = set(), ends[-1] if ends else -1
    while at >= 0:
        kept.add(mine[at])
        at = came[at]
    return [key for key in mine if key not in kept]


def _merge_block(was: dict, mine: dict, live: dict, conflicts: list, notes: list,
                 inside: str | None = None) -> dict:
    key = live.get("key") or inside or "a table cell"
    # A cell has no key, and the base a cell is merged against may be a stand-in: a row
    # or a column one side has just added pairs with nothing, so every cell of it reads
    # as both sides having styled it. The styling notes below are for blocks, where the
    # base really is what that block said last time; a cell's own report is the table's
    # (`_merge_table`), which is what `inside` carries down: a person told their words
    # lost a fight needs to know where, and a report that says `a table cell` names no
    # table at all — the reader of it cannot find the cell and the loss oracle cannot
    # tell which table was spoken for (offline chain-12 seed 1710213, shape `prose`).
    a_block = bool(live.get("key"))
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
                return _merged_shape(dict(out), was, mine, live, a_block, key, notes) \
                    | {"runs": runs, "rewrite": True, "origin": "merged"}
            notes.append(f"{key}: the source changed a chip or picture no request can write "
                         f"— left alone")
            return out | {"origin": "frozen content differs"}
        notes.append(f"{key}: the source would rewrite a chip or equation — left alone")
        return out | {"origin": "frozen content differs"}
    text, clashes = diff3(block_text(was), block_text(mine), block_text(live))
    for clash in clashes:
        conflicts.append(dict(clash) | {"key": key})
    _merged_shape(out, was, mine, live, a_block, key, notes)
    if text != block_text(live):
        out["runs"] = _retext(live, text)
        out["origin"] = "merged"
        if styles_of(live) != styles_of(was):
            # The reader styled a word and the source replaced that very word: the
            # styling is rightly gone, but gone with nothing said it reads as lost.
            word = reader_styling_gone(was, live, text)
            if word:
                notes.append(f"{key}: the source rewrote {word!r}, a word the document "
                             f"had styled — that styling is gone with the word")
    # Styling is merged the same way the words are: the source's marks are taken when
    # the source changed them and the document's styling is as the base has it. Where
    # the words differ too, the marks follow the words — each word of the merged text
    # the file also has takes the file's marks, the rest keep the document's.
    # The two sides are not asked the same question, and only the source's may be
    # asked the finer one. "Is the source asking for different marks?" decides
    # whether its restyle is written at all, so a boundary it moved has to be heard
    # (`_remarked`). "Did the reader restyle?" guards the branch below, which gives
    # up on the *whole block* — asked as finely, a reader who moved a bold from one
    # word to another would take the source's italic on a third word down with it,
    # where `_restyled_words` merges the two perfectly well (chain-6 seed 400044,
    # the test below it). Where that branch is not taken, a boundary the reader
    # moved is decided downstream, as it is today.
    if marks_of(mine) != marks_of(was) or _remarked(was, mine):
        if marks_of(live) != marks_of(was):
            # And the same the other way round: a reader who marked one word of a
            # paragraph the source has just made italic keeps their mark and the whole
            # italic goes. That is the document winning, which is this project's rule,
            # but the source's restyle disappearing with nobody told is not: the
            # neighbouring note above says as much for the words that carry it.
            if a_block:
                notes.append(f"{key}: both sides restyled it — the document's styling is "
                             f"kept and the source's is not written")
        elif text == block_text(mine):
            out["runs"] = _restyled(live, mine)
            out["restyle"] = True
            out["origin"] = "merged"
        else:
            out["runs"], lost = _restyled_words(was, mine, live, text)
            if lost:
                notes.append(f"{key}: the source restyled words the document rewrote — "
                             f"those keep the document's styling")
            out["restyle"] = True
            out["origin"] = "merged"
    if not out.get("origin") and (block_text(live) != block_text(was)
                                  or styles_of(live) != styles_of(was)):
        # Nothing to write: the document says this, and the merge agrees. It is said
        # out loud all the same, so a sync report shows what the reader's side kept.
        out["origin"] = "kept from the document"
    return out


def _merge_table(was: dict, mine: dict, live: dict, conflicts: list, notes: list) -> dict:
    """A table merges three ways twice: its rows and columns, then its cells.

    Inside a table a block has no named range: a cell is known by the row and column
    it is in, and a row or a column by its words (`_table_lines`), so both sides may
    add and take away rows and columns — one side rows, the other columns, or both at
    once. Lines the document added stay, lines the source added are written, a line
    the source took away goes unless the document wrote in it. Rows and columns are
    written in a batch of their own, before any words (`structure`), and the cells are
    merged on the pass after that, against the grid the document then has.
    """
    key = live.get("key") or "a table"
    out = dict(live)
    if _hint(was, mine, live) is None and _grid(was) == _grid(mine) == _grid(live):
        # One shape on all three sides: a cell is its place, so a row the source
        # rewrote from end to end is still that row.
        lines = [[(r, c, r, c, r, c) for c in range(len(row))]
                 for r, row in enumerate(live.get("rows", []))]
    else:
        found = _table_lines(was, mine, live, notes, key)
        if found is None:
            notes.append(f"{key}: the table's rows and columns differ between the sides "
                         f"— left alone")
            return out | {"origin": "table grid differs"}
        rows, columns, settled = found
        ops = _grid_ops(rows, "row") + _grid_ops(columns, "column")
        matched = {"row": rows, "column": columns, "dropped": settled,
                   "mine": _size(mine.get("rows", []))}
        if ops:
            return out | {"origin": "the grid the source has", "regrid": ops,
                          "lines": matched}
        # No line is added or taken away, so the document's grid is the merged one,
        # and every live cell is merged where it stands. The matching is carried all
        # the same: a table the source *moved* is built again blank, and the round
        # after has to be told which of the file's lines that grid stands for
        # (`rebase_tables`) rather than guess it from words that are not there yet.
        out = out | {"lines": matched}
        lines = [[(row.live, column.live, row.was, column.was, row.mine, column.mine)
                  for column in columns] for row in rows]
    merged = []
    for row in lines:
        cells = []
        for r, c, wr, wc, mr, mc in row:
            here = live["rows"][r][c]
            then, want = _cell(was, wr, wc), _cell(mine, mr, mc)
            cells.append([dict(b) for b in here] if then is None or want is None
                         else _merge_cell(then, want, here, conflicts, notes, key))
        merged.append(cells)
    # A table has no words of its own, so what its cells did is what it did: the
    # report and the write side both ask the table, not the blocks inside it.
    inside = {b.get("origin") for row in merged for cell in row for b in cell}
    return out | {"rows": merged} | ({"origin": "merged"} if "merged" in inside
                                     else {"origin": "kept from the document"}
                                     if "kept from the document" in inside else {})


def _merge_cell(was: list, mine: list, live: list, conflicts: list, notes: list,
                key: str) -> list[dict]:
    """One cell, three ways: paragraph by paragraph while the three agree on how many
    there are, and as one text with its paragraph breaks in it when they do not."""
    if len(was) == len(mine) == len(live):
        return [_merge_block(w, m, l, conflicts, notes, key)
                for w, m, l in zip(was, mine, live)]
    then, want, now = (_cell_text(cell) for cell in (was, mine, live))
    text, clashes = diff3(then, want, now)
    for clash in clashes:
        conflicts.append(dict(clash) | {"key": key})
    if text == now:
        return [dict(b) | ({"origin": "kept from the document"} if now != then else {})
                for b in live]
    # `_pairs` sets this against the live cell read as one block (`_joined`), so the
    # breaks are written as the newlines they are.
    return [{"kind": "paragraph", "joined": True, "runs": [{"text": text}], "origin": "merged"}]


class _Line(NamedTuple):
    """One row or column of the merged table: where it is in each side, if anywhere,
    and whether it is about to be taken out of the document."""
    was: int | None
    live: int | None
    mine: int | None
    gone: bool = False


ALIKE = 0.5    # least score for two rows (or columns) to be the same line
BLANK = 0.6    # two lines with nothing written in them: alike, but less than equal


def _table_lines(was: dict, mine: dict, live: dict, notes: list,
                 key: str) -> tuple[list[_Line], list[_Line], dict] | None:
    """The merged rows and columns of a table, or None when there is no telling.

    The columns are matched by the words in them and the rows by their cells in those
    columns, each side against the base, and the two matchings are merged
    (`_merged_lines`). A base the grid was just written to says how its lines match
    (`rebase_tables`), so the pass after a regrid does not guess them a second time.
    The third thing it gives back is what that base cannot hold: the file's lines this
    merge has settled as *not* in the grid, because the document deleted the base line
    they pair with. The rebase takes such a line out of the base altogether, so the
    round after it would find the file's line matched to nothing and read it as one the
    source has just added — and put back the row a reader deleted.
    """
    here, old, src = (b.get("rows", []) for b in (live, was, mine))
    if not all(_size(rows) for rows in (here, old, src)):
        return None                            # a row out of step with the others
    now, then, want = (_texts(rows) for rows in (here, old, src))
    hint = _hint(was, mine, live)
    dropped = {name: frozenset((hint or {}).get(f"{name}_dropped", ()))
               for name in ("row", "column")}
    if hint:
        live_columns, mine_columns = hint["column_live"], hint["column_mine"]
        live_rows, mine_rows = hint["row_live"], hint["row_mine"]
    else:
        live_columns = _align(_columns(then), _columns(now), _column_score)
        mine_columns = _align(_columns(then), _columns(want), _column_score)
        live_rows = _align(then, now, _row_score(live_columns))
        mine_rows = _align(then, want, _row_score(mine_columns))

    def row_kept(w, l):
        return _line_unchanged(then[w], now[l], live_columns, len(now[l]))

    def column_kept(w, l):
        return _line_unchanged([row[w] for row in then], [row[l] for row in now],
                               live_rows, len(now))

    rows = _merged_lines(len(now), len(want), live_rows, mine_rows, row_kept,
                         dropped["row"])
    columns = _merged_lines(len(now[0]), len(want[0]), live_columns, mine_columns,
                            column_kept, dropped["column"])
    if rows is None or columns is None:
        return None
    settled = {}
    for name, merged, pairs, across in (("row", rows, mine_rows, mine_columns),
                                        ("column", columns, mine_columns, mine_rows)):
        for line in merged:
            if line.was is not None and line.mine is None and not line.gone:
                notes.append(f"{key}: the source took away a {name}, but the document wrote "
                             f"in it — kept")
        have = {line.mine for line in merged if line.mine is not None}
        settled[name] = sorted(dropped[name] | {m for _, m in pairs if m not in have})
        # And the other way round, which nothing said: the document deleted a line the
        # source had written in. The words go with the line — deleting a row is a change
        # to the grid, and the grid is the document's — so there is nothing to merge and
        # nothing to keep, but the author asked for those words and only the report can
        # tell them they went (offline chain-10 seed 480066: the reader deleted the very
        # row whose cell the source was rewriting, and the file's line was settled out of
        # the merge in silence).
        was_of = {m: w for w, m in pairs}
        for m in settled[name]:
            w = was_of.get(m)
            if w is None or not _source_wrote_in(then, want, name, w, m, across):
                continue
            notes.append(f"{key}: the document deleted a {name} the source wrote in — "
                         f"that {name} is gone and the words with it")
    return rows, columns, settled


def _source_wrote_in(then: list[list[str]], want: list[list[str]], name: str,
                     w: int, m: int, across: list[tuple[int, int]]) -> bool:
    """Whether the source changed a cell of a row (or column) of the base.

    Only the cells of the *other* dimension's lines both sides share are asked, as in
    `_line_unchanged`: a column the source added is a line of its own, and what it says
    is not news about every row it crosses.
    """
    if name == "row":
        return any(then[w][a] != want[m][b] for a, b in across)
    return any(then[a][w] != want[b][m] for a, b in across)


def _hint(was: dict, mine: dict, live: dict) -> dict | None:
    """How the base's lines match, when `rebase_tables` left that said for the grids
    the two sides have now."""
    hint = was.get("aligned")
    if hint and hint["live"] == _size(live.get("rows", [])) \
            and hint["mine"] == _size(mine.get("rows", [])):
        return hint
    return None


def _size(rows: list) -> tuple[int, int] | None:
    """A grid's rows and columns, when every row has as many cells as the first."""
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        return None
    return len(rows), len(rows[0])


def _built_size(block: dict) -> tuple[int, int] | None:
    """The shape a table the source moved has to be built again at.

    Not the file's — the file may still hold a row the reader deleted — and not
    `_size` of the block's own rows either, which is the trap: when the merge also
    regrids, `_merge_table` returns *before* it merges the cells, so `rows` is still
    the document's grid, and `structure` builds the moved table at the very shape the
    sync was about to change. The source deletes a row and moves the table, and it
    comes back with an empty row on the end (offline seed 88075, shrunk to three
    source ops and no reader at all).

    The merged shape is in the matching instead: every line but the ones the merge
    settled as `gone`.
    """
    lines = block.get("lines")
    if not lines:
        return _size(block.get("rows", []))
    rows, columns = (sum(not line.gone for line in lines[name])
                     for name in ("row", "column"))
    return (rows, columns) if rows and columns else None


def _cell_text(cell: list[dict]) -> str:
    return "\n".join(block_text(block) for block in cell)


def _texts(rows: list) -> list[list[str]]:
    return [[_cell_text(cell) for cell in row] for row in rows]


def _columns(texts: list[list[str]]) -> list[list[str]]:
    return [list(column) for column in zip(*texts)]


def _column_score(a: list[str], b: list[str]) -> float:
    """How alike two columns are: the share of their words that both have, wherever
    in the column — a row added or taken away shifts every cell below it."""
    one, other = (Counter(t for t in side if t.strip()) for side in (a, b))
    if not one and not other:
        return BLANK
    if not one or not other:
        return 0.0
    return sum((one & other).values()) / max(sum(one.values()), sum(other.values()))


def _row_score(columns: list[tuple[int, int]]):
    """How alike two rows are: the share of their cells, in the columns that match,
    that say the same thing — cells nobody wrote in left out of the count."""
    def score(a: list[str], b: list[str]) -> float:
        cells = [(a[i], b[j]) for i, j in columns if a[i].strip() or b[j].strip()]
        if not cells:
            return BLANK
        return sum(x == y for x, y in cells) / len(cells)
    return score


def _align(here: list, there: list, score) -> list[tuple[int, int]]:
    """Which line of `here` is which line of `there`, keeping both orders.

    The pairs that are at least ALIKE and add up to the most, and then, between two
    such pairs, the lines left over paired by place: a row reworded from end to end
    between two rows that kept their words is still that row.
    """
    n, m = len(here), len(there)
    alike = [[score(a, b) for b in there] for a in here]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = best[i + 1][j + 1] + alike[i][j] if alike[i][j] >= ALIKE else -1.0
            best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
    sure, i, j = [], 0, 0
    while i < n and j < m:
        if alike[i][j] >= ALIKE and best[i][j] == best[i + 1][j + 1] + alike[i][j]:
            sure.append((i, j))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    out = []
    for (i0, j0), (i1, j1) in zip([(-1, -1)] + sure, sure + [(n, m)]):
        out += [(i0 + 1 + k, j0 + 1 + k) for k in range(min(i1 - i0, j1 - j0) - 1)]
        if i1 < n:
            out.append((i1, j1))
    return out


def _line_unchanged(was: list[str], live: list[str], across: list[tuple[int, int]],
                    width: int) -> bool:
    """Whether the document left a row (or column) as the base has it: every cell the
    two share says the same, and nothing is written in a cell the base did not have."""
    matched = {l for _, l in across}
    return (all(was[w] == live[l] for w, l in across)
            and all(not live[l].strip() for l in range(width) if l not in matched))


def _merged_lines(n_live: int, n_mine: int, live: list[tuple[int, int]],
                  mine: list[tuple[int, int]], unchanged,
                  dropped: frozenset = frozenset()) -> list[_Line] | None:
    """The rows (or columns) of the merged table, in the document's order.

    Every line the document has is there: one the source took away is marked `gone`
    when the document left it as it was, and kept otherwise. Lines the source added
    go in after the line that precedes them in the file — all but the ones in
    `dropped`, which the file has and nobody is adding: they pair with a base line
    the *document* deleted, and are only here at all because a line with no line of
    the document's leaves no `_Line` behind to say so. A table the merge would
    leave with no line of the document's is not a merge a grid request can write
    (the last row cannot be deleted), and says so with None.
    """
    was_of = {l: w for w, l in live}
    mine_of = dict(mine)
    lines = []
    for l in range(n_live):
        w = was_of.get(l)
        if w is None:
            lines.append(_Line(None, l, None))
        elif w in mine_of:
            lines.append(_Line(w, l, mine_of[w]))
        else:
            lines.append(_Line(w, l, None, gone=unchanged(w, l)))
    known = set(mine_of.values()) | set(dropped)
    for m in range(n_mine):
        if m in known:
            continue
        at = next((k + 1 for k in range(len(lines) - 1, -1, -1)
                   if lines[k].mine is not None and lines[k].mine < m), 0)
        lines.insert(at, _Line(None, None, m))
    if n_live and all(line.gone for line in lines if line.live is not None):
        return None
    return lines


def _grid_ops(lines: list[_Line], name: str) -> list[tuple[str, str, int]]:
    """The rows (or columns) to delete and insert, at the document's indices: a line
    goes in in front of the document's next line, whichever side that one is on."""
    width = sum(line.live is not None for line in lines)
    ops = []
    for k, line in enumerate(lines):
        if line.gone:
            ops.append((name, "delete", line.live))
        elif line.live is None:
            ops.append((name, "insert", next((later.live for later in lines[k + 1:]
                                             if later.live is not None), width)))
    return ops


def _kept(lines: list[_Line]) -> list[_Line]:
    return [line for line in lines if not line.gone]


def _cell(block: dict, row: int | None, column: int | None) -> list[dict] | None:
    if row is None or column is None:
        return None
    try:
        return block["rows"][row][column]
    except (KeyError, IndexError):
        return None


def _grid(block: dict) -> tuple:
    """How many cells each row has."""
    return tuple(len(row) for row in block.get("rows", []))


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


def _char_styles(block: dict) -> list:
    """The styling of every character of `block_text`: a run's marks, or the frozen
    run itself for the one character that stands for it."""
    out: list = []
    for run in block.get("runs", []):
        if run.get("frozen"):
            out.append(run)
        else:
            out += [{k: v for k, v in run.items() if k not in ("text", "width")}] * len(run["text"])
    return out


def _word_pairs(one: str, other: str):
    """(i, j) for every character of `one` whose word is also in `other`, in order."""
    a, b = tokens(one), tokens(other)
    at_a, at_b = [0], [0]
    for token in a:
        at_a.append(at_a[-1] + len(token))
    for token in b:
        at_b.append(at_b[-1] + len(token))
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(at_a[i2] - at_a[i1]):
                yield at_a[i1] + k, at_b[j1] + k


def _restyled_words(was: dict, mine: dict, live: dict, text: str) -> tuple[list[dict], bool]:
    """Runs for merged `text` whose words both sides changed, and one side the marks.

    Every character takes the document's styling where its word is the document's,
    and then the file's where the source really *changed* that word's, so a word the
    source made bold is bold wherever the merge kept it. Only where it changed it:
    the source's other words say nothing, and writing the file's styling on them as
    well takes back a mark the reader put somewhere the file has no mark, which is
    the reader's edit and outranks it. Nothing above catches that, because a reader
    who marks one word and un-marks its neighbour leaves `marks_of` — a sequence of
    mark sets, with no words in it — saying exactly what it said before, so the merge
    reads the restyle as the source's alone (offline chain-6 seed 400044: a bold
    moved from the word the reader deleted to the word they typed). A character in a
    word neither side has whole (the merge joined two edits inside one) takes the one
    before it, as Docs gives a typed character. Frozen runs are always the document's
    own. The second value says whether a word the source restyled is gone from the
    merge — the document rewrote it, and its new words keep the document's styling.
    """
    styles: list = [None] * len(text)
    live_styles, mine_styles = _char_styles(live), _char_styles(mine)
    was_styles = _char_styles(was)
    for i, j in _word_pairs(block_text(live), text):
        styles[j] = live_styles[i]
    # A word of the file the base has too, and set as the base has it: the source is
    # not asking for anything there. A word it does not have is one the source typed,
    # and that one comes with the file's styling.
    said = {i: was_styles[w] for w, i in _word_pairs(block_text(was), block_text(mine))}
    kept = set()
    for i, j in _word_pairs(block_text(mine), text):
        kept.add(i)
        if text[j] != FROZEN and mine_styles[i] != said.get(i):
            styles[j] = mine_styles[i]
    lost = any(i not in kept and mine_styles[i] != was_styles[w]
               for w, i in _word_pairs(block_text(was), block_text(mine))
               if block_text(mine)[i] != FROZEN)
    return _runs_from_styles(text, styles, live), lost


def _runs_from_styles(text: str, styles: list, live: dict) -> list[dict]:
    """Runs for `text` from one style per character: a character with none takes the
    one before it, as Docs gives a typed character, and the frozen runs are the
    document's own, in its order — the merge adds or drops none the document does
    not have (`_merge_block` checks the source's)."""
    runs: list[dict] = []
    frozen = iter([r for r in live.get("runs", []) if r.get("frozen")])
    previous: dict = {}
    for char, style in zip(text, styles):
        if char == FROZEN:
            chip = next(frozen, None)
            if chip is not None:
                runs.append(dict(chip))
            continue
        style = previous if style is None or style.get("frozen") else style
        previous = style
        if runs and not runs[-1].get("frozen") and \
                {k: v for k, v in runs[-1].items() if k not in ("text", "width")} == style:
            runs[-1]["text"] += char
        else:
            runs.append(dict(style) | {"text": char})
    for run in runs:
        if not run.get("frozen"):
            run["width"] = doc_ir.utf16_len(run["text"])
    return runs


def _retext(live: dict, text: str) -> list[dict]:
    """Put merged text back into the live block's runs, frozen runs untouched.

    Every word the document has keeps the document's styling on it, wherever the
    merge put it; a word neither side has whole takes the styling before it. It used
    to fold each stretch between two frozen runs into the first writable run of that
    stretch, which threw away every mark the reader had put inside the stretch as
    soon as the block was written again from nothing (a block the source both
    reworded and moved, `moved-styling`).
    """
    styles: list = [None] * len(text)
    live_styles = _char_styles(live)
    for i, j in _word_pairs(block_text(live), text):
        styles[j] = live_styles[i]
    return _runs_from_styles(text, styles, live)


def reader_styling_gone(was: dict, live: dict, text: str) -> str | None:
    """The first word the reader styled that the merged `text` no longer has.

    A word carries the reader's styling when its marks differ from the base's on
    that same word; it is gone when no word of the merged text pairs with it. Both
    sides are read-backs, so a named style's own defaults are absent on both alike
    (`_named_defaults`) and never read as a mark. Frozen runs are not words.
    """
    live_styles, was_styles = _char_styles(live), _char_styles(was)
    live_text = block_text(live)
    theirs = {i for w, i in _word_pairs(block_text(was), live_text)
              if live_text[i] != FROZEN and live_styles[i] != was_styles[w]}
    kept = {i for i, _ in _word_pairs(live_text, text)}
    lost = sorted(theirs - kept)
    if not lost:
        return None
    start = live_text.rfind(" ", 0, lost[0]) + 1
    end = live_text.find(" ", lost[0])
    return live_text[start:end if end >= 0 else None]


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
    # The settle reads this back: only a paragraph this run wrote may have a field
    # of its own written again from the plan (`_unwritten`).
    block["paragraph_written"] = True
    if block["kind"] != "item" and was_item:
        # Text inserted at the start of a list item joins that item, bullet and all;
        # and a block the source turned back into a paragraph must lose its glyph.
        out.append({"deleteParagraphBullets": {"range": {"startIndex": start, "endIndex": end}}})
    style, fields = paragraph_style(block)
    out.append({"updateParagraphStyle": {
        "range": {"startIndex": start, "endIndex": end}, "paragraphStyle": style,
        "fields": fields}})
    if block["kind"] == "item":
        # Bullets last: a style request covering the whole paragraph would restyle
        # the glyph too, the same trap as the Slides pipeline's createParagraphBullets.
        out.append({"createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end},
            "bulletPreset": BULLETS[bool(block.get("ordered"))]}})
    return out


def paragraph_style(block: dict) -> tuple[dict, str]:
    """A block's `paragraphStyle` and the fields to write it under.

    Every field the merge owns is named; only the ones the block asks for are given
    a value. Named-and-unset is how the API is told to put a property back to its
    default, which is what a source that dropped an indent means.

    The alignment goes the same way, and used to be the one exception: START was
    written whenever the file said nothing, which is also what a heading centred by
    the document's *theme* says (`doc_ir._named_defaults` reads that now). So a
    source restyle that never mentioned alignment left-aligned every such heading.
    Unset, the paragraph falls back to its named style, which is the whole point of
    a theme; a reader who left-aligned a centred heading in the browser still reads
    back as `align: left` and is written as START.
    """
    style = {"namedStyleType": named_style(block)}
    if block.get("align"):
        style["alignment"] = doc_ir.TO_ALIGNMENT[block["align"]]
    fields = ITEM_PARAGRAPH if block["kind"] == "item" else MANAGED_PARAGRAPH
    for key, api in PARAGRAPH_FIELDS:
        if api in fields and block.get(key) is not None:
            style[api] = _paragraph_value(key, block[key])
    return style, ",".join(fields)


def named_style(block: dict) -> str:
    """Which of Docs' named styles a block is. `namedStyleType` is named on every
    paragraph the merge writes, so a kind missing from here is silently written as
    body text: that is what happened to Title and Subtitle (`doc_ir.NAMED_KINDS`)."""
    if block["kind"] == "heading":
        return doc_ir.NAMED_STYLE[block.get("level", 0)]
    return doc_ir.KIND_STYLE.get(block["kind"], "NORMAL_TEXT")


def _paragraph_value(key: str, value):
    if key == "line_spacing":
        # A Docs lineSpacing is the multiplier × 100 (single spacing is 100).
        return float(value) * 100
    if key == "shading":
        return {"backgroundColor": {"color": {"rgbColor": _rgb(value)}}}
    if key in doc_ir.PARAGRAPH_FLAGS:
        return bool(value)
    if key in doc_ir.BORDER_SIDES:
        return _border_value(value)
    return {"magnitude": float(value), "unit": "PT"}


def _border_value(said: str) -> dict:
    """A `ParagraphBorder` from the way the file spells one (`doc_ir._border`).

    The padding is always named: a rule the source moved back against the text has
    no `pad`, and a border written without a padding field would keep whatever gap
    the document had — an unset field inside a border is not the API's "back to the
    default", because the border itself is the field being written.
    """
    width, dash, colour, *pad = (said or "").replace(" pad ", " ").split()
    return {"color": {"color": {"rgbColor": _rgb(colour)}},
            "width": {"magnitude": float(width.removesuffix("pt")), "unit": "PT"},
            "padding": {"magnitude": float(pad[0].removesuffix("pt")) if pad else 0.0,
                        "unit": "PT"},
            "dashStyle": doc_ir.TO_DASH_STYLE.get(dash, "SOLID")}


def _run_requests(start: int, block: dict, reset: bool = False) -> list[dict]:
    """`updateTextStyle` per run of a block laid out from `start`.

    `reset` is for a block that already exists: the fields are named whether or not
    the run carries them, so a mark the source took away is taken away in the
    document too. The face and the size are named with the rest of `MANAGED` — the
    file carries them now, so what is written back is what the last read saw, and a
    run that only repeats its named style names the field with no value, which puts
    the paragraph's own face back rather than some face of ours.
    """
    out, at = [], start
    for run in block.get("runs", []):
        width = _run_width(run)
        marks = _text_style(run)
        if width and not run.get("frozen") and (marks or reset):
            fields = sorted(set(marks) | (set(MANAGED) if reset else set()))
            out.append({"updateTextStyle": {
                "range": {"startIndex": at, "endIndex": at + width},
                "textStyle": marks, "fields": ",".join(fields)}})
        at += width
    return out


def _bullets_last(paragraph: list[dict], runs: list[dict]) -> list[dict]:
    """The run styling goes between the paragraph's style and its bullet, always."""
    made = [r for r in paragraph if "createParagraphBullets" in r]
    return [r for r in paragraph if "createParagraphBullets" not in r] + runs + made


def _style_requests(start: int, block: dict, reset: bool = True) -> list[dict]:
    """Everything but the words, for a block written at `start` from nothing.

    The runs are written with `reset`: text inserted inherits the styling of the
    character in front of it (Docs' rule), so a block moved in front of an underlined
    heading came out underlined, with the reader's bold on it, and the oracle called
    the bold lost. Naming every managed field puts the paragraph's own face back.
    """
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
    for key, api in doc_ir.MARK_FIELDS:
        # False is a value, not an absence: it is how a run says it is *not* bold
        # against a theme whose headings are, and naming the field with no value
        # would hand it back to the theme (`doc_ir.MARK_FIELDS`).
        if run.get(key) is not None:
            style[api] = bool(run[key])
    # The face the run says it is; `<code>` in a file written before faces were
    # carried still means the one face it always meant (`doc_ir.CODE_FAMILY`).
    if run.get("font") or run.get("code"):
        style["weightedFontFamily"] = {"fontFamily": run.get("font") or doc_ir.CODE_FAMILY}
    if run.get("script"):
        # "none" is a value here, as `False` is for a mark: it is how a run says it is
        # *not* raised against a named style that raises the whole paragraph.
        style["baselineOffset"] = {"super": "SUPERSCRIPT", "sub": "SUBSCRIPT",
                                   "none": "NONE"}[run["script"]]
    if run.get("fontsize"):
        style["fontSize"] = {"magnitude": float(run["fontsize"]), "unit": "PT"}
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
    filled = any(_written_here(b) and _insert_index(merged, p) is None
                 for p, b in enumerate(merged))
    left_empty = None
    # Every one of them first, because a range one delete leaves behind another may
    # already have taken (`_orphan_range`).
    cuts = {index: _delete_range(theirs["blocks"], index, going, ends,
                                 theirs.get("lead"), filled) for index in sorted(going)}
    for index in sorted(going):
        live = theirs["blocks"][index]
        start, end = cuts[index]
        plans.append((start, DELETE, [{"deleteContentRange": {
            "range": {"startIndex": start, "endIndex": end}}}]
            + _orphan_range(live, start, end, cuts.values())))
        if index == len(theirs["blocks"]) - 1 and end == theirs["blocks"][index]["span"][1] - 1:
            # The body's last block, whose words go and whose own mark stays
            # (`_delete_range`): the document ends on an empty paragraph exactly
            # where it was. It has to, since a body may not end on a table.
            left_empty = start
        # Docs merges a deleted paragraph into its neighbour and keeps the first
        # one's style, so a block **nobody wrote** can come out of this batch wearing
        # the style of the one that went: a heading behind a justified paragraph the
        # source dropped stops following the theme's centring for good, and the file
        # is regenerated from the document afterwards, so nothing says it ever did
        # (offline chain-6 seed 1130023, shape `themed`). `carry_unimported` already
        # puts the named style and the bullet back for every block; the measurements
        # it may only take back where this run is what changed them, and this is
        # where. The block behind takes the style; the one in front does only where
        # the delete borrowed *its* mark (`_delete_range`, a block in front of a
        # table), so that neighbour is named only then.
        sides = [1, -1] if start < live["span"][0] else [1]
        for block in (_neighbour(theirs["blocks"], index, side, going) for side in sides):
            want = by_key.get((block or {}).get("key"))
            if want is not None:
                want["paragraph_merged"] = True

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
    if trailer is None and left_empty is not None and kept and _structural(kept[-1]):
        # Everything the body ended on is going — deleted by the source, or moved to
        # somewhere in front — and the last block that stays is a table, whose own
        # last index is *inside* its last cell. The mark left behind at the end is
        # where anything appended goes. Without it the appended block was written
        # into the table: the table swallowed it and the write took the table's named
        # range with it (chain-8 seed 189 for the delete, the permutation sweep of
        # `_apply_source_moves` for the move).
        trailer = [left_empty]
    if trailer:
        # The body ends on a table and the empty paragraph after it: the first block
        # appended is written *into* that paragraph, and the rest after it.
        tail = trailer[0]
    first = min((p for p, b in enumerate(merged)
                 if _written_here(b) and _insert_index(merged, p) is None), default=None)
    # A body that begins with a table begins with the empty paragraph in front of it
    # (`doc_ir._hide_trailer`): the first block written before that table goes into it.
    lead = theirs.get("lead")
    lead_first = min((p for p, b in enumerate(merged) if _written_here(b) and lead
                      and (_anchor(merged, p) or {}).get("span", [None])[0] == lead[1]),
                     default=None)
    # An empty paragraph is all mark, so its named range *is* its mark, and text
    # written at a range's first index pushes the range along (Docs' rule): "\ntext"
    # appended at that mark leaves the key on the new block's mark and the empty
    # paragraph with none, so the plan after reads the new block as the old one. The
    # same for a range a reader's chip or word pushed onto the mark of a paragraph
    # that was empty (`apply_keys` records where a range is). The range is planted
    # again where it belongs in the same batch — once, after every append there
    # (`REPLANT`), and before a block inserted in front of the paragraph, which
    # pushes the fresh range along as it should.
    empties = {b["span"][1] - 1: b for i, b in enumerate(theirs["blocks"])
               if i not in going and b.get("key") and b.get("rangeId") and b.get("span")
               and (b["range"][0] == b["span"][1] - 1 if b.get("range")
                    else b["span"][1] == b["span"][0] + 1 and not b.get("runs"))}
    replant: dict[int, dict] = {}
    # Back to front here too: two blocks added at one index both insert there, and
    # what is written last ends up in front, so the later block is planned first.
    for position in range(len(merged) - 1, -1, -1):
        block = merged[position]
        if not _written_here(block):
            continue
        at = _insert_index(merged, position)
        anchor = _anchor(merged, position)
        if _structural(anchor):
            # Nothing can be written at a table's or a table of contents' own index
            # (measured: refused), so a block in front of one goes after the paragraph
            # before it — "\ntext" at that paragraph's mark, ahead of its own edits there.
            at -= 1
            if position == lead_first:
                plans.append((at, APPEND, _content_requests(at, block)
                              + _style_requests(at, block)))
            else:
                plans.append((at, APPEND, _content_requests(at, block, before="\n")
                              + _style_requests(at + 1, block)))
                if at in empties:
                    replant[at] = empties[at]
        elif at is not None:
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
            if tail in empties:
                replant[tail] = empties[tail]
        else:
            # Nothing of the document survives: write into the empty paragraph Docs
            # always keeps, and let the empty one end up at the bottom.
            plans.append((tail, APPEND, _content_requests(tail, block, after="\n")
                          + _style_requests(tail, block)))
    for at, live in replant.items():
        # The block's own indices are below the mark, so nothing written there has
        # moved them and its anchor is where `theirs` read it.
        low, high = doc_ir.anchor_range(live)
        plans.append((at, REPLANT, [
            {"deleteNamedRange": {"namedRangeId": live["rangeId"]}},
            {"createNamedRange": {"name": doc_ir.KEY_PREFIX + live["key"],
                                  "range": {"startIndex": low, "endIndex": high}}}]))

    out: list[dict] = []
    # Back to front, so an earlier edit never moves a later one's indices, and at one
    # index by what the edits do there: a block appended at the last paragraph's mark
    # must go in before that paragraph's own edits, which end where it begins; a block
    # inserted before another pushes it down, so that block's edits go first, and a
    # delete of it first of all.
    for _, _, reqs in sorted(plans, key=lambda p: (-p[0], p[1])):
        out += reqs
    # A range no block is known by any more (`doc_ir.apply_keys`) goes at the head of
    # the batch, and not only at the settle: a reader's join leaves two ranges in one
    # paragraph, and when this sync also rewrites the words the surviving range sits
    # on, that range dies with them — leaving the orphan the only name in the
    # paragraph, so the read-back names the block after the paragraph that was
    # swallowed and the plan's own key is nowhere (chain-8 seed 77064, a picture
    # paragraph joined into a list item the source reworded). It moves no index, and
    # a sync that writes nothing needs it no sooner than the settle.
    return doc_ir.orphan_requests(theirs) + out if out else out


def _written_here(block: dict) -> bool:
    """Whether `requests` writes this block from nothing: one the source added or
    moved — but not a table, which is a grid (`structure` builds it), and not a block
    with a chip no request can create (`plan` says so in its notes)."""
    return ((block.get("origin") == "added by the source" or bool(block.get("moved")))
            and block.get("kind") != "table" and not block.get("nowhere")
            and _writable_block(block))


def structure(theirs: dict, merged: list[dict],
              notes: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """The requests that change a table's shape, and what each of them does.

    A grid is not text. `insertTable`, `insertTableRow` and their deletes are the only
    way to build one, and none of them belongs in the same batch as the words: every
    index below a grid that changes moves. So a sync that needs one of these sends
    them on their own, reads the document again — which is also how the new table gets
    its key and its anchor — and plans the text against the grid it then has
    (`doc_sync.sync`). Back to front, so the indices of the ones still to come hold.
    """
    plans: list[tuple[int, list[dict], dict]] = []
    deletes: dict[str, tuple[int, int, list[dict]]] = {}
    for block in list(merged):                   # `_put_back` below rearranges it
        if block.get("kind") != "table":
            continue
        position = next(i for i, b in enumerate(merged) if b is block)
        key = block.get("key")
        if block.get("moved"):
            # A move is a delete and a table built again where the file has it, blank:
            # its words are written on the next pass, like a new table's. Only a table
            # the document left as the base has it gets here (`_apply_source_moves`),
            # so nothing of the reader's is in what is deleted.
            rows, columns = block["build"]
            at, reqs = _new_table_requests(theirs, _insert_index(merged, position),
                                           rows, columns)
            if not reqs:
                block["moved"] = False
                _put_back(merged, block)
                if notes is not None:
                    notes.append(f"{key}: the source moved it where the document has no "
                                 f"paragraph to write in — before the table it opens on, "
                                 f"or between two tables — and it stays where the document "
                                 f"has it")
                continue
            index = next(i for i, b in enumerate(theirs["blocks"])
                         if b.get("span") == block.get("span"))
            start, end = _delete_range(theirs["blocks"], index, {index},
                                       not theirs.get("trailer"), theirs.get("lead"))
            deletes[key] = (start, 0, [{"deleteContentRange": {
                "range": {"startIndex": start, "endIndex": end}}}]
                + _orphan_range(theirs["blocks"][index], start, end))
            swallowed = _swallowed(theirs, reqs)
            eaten = _eaten(theirs, index, start)
            plans.append((at, reqs,
                          {"key": key,
                           "after": _after_key(merged, position, swallowed, eaten),
                           "moved": True, "lines": block.get("lines"),
                           **({"swallowed": swallowed} if swallowed else {}),
                           **({"eaten": eaten} if eaten else {}),
                           "note": f"`{key}`: moved where the source has it"}))
        elif block.get("regrid"):
            what = ", ".join(f"{how}s a {line}" for line, how, _ in block["regrid"])
            # `after` although nothing is built: a table is anchored in its first cell
            # (`doc_ir.anchor_span`), and a row or column delete can take that very
            # cell, so a regrid can leave the table with no named range at all. It is
            # then found again exactly as a new one is, and the range planted back.
            plans.append((block["span"][0], _grid_requests(block["span"][0], block["regrid"]),
                          {"key": key, "ops": block["regrid"], "lines": block.get("lines"),
                           "after": _after_key(merged, position),
                           "note": f"`{key}`: {what} — the grid the source has"}))
        elif block.get("origin") == "added by the source":
            rows = len(block.get("rows", []))
            columns = len(block["rows"][0]) if rows else 0
            if not rows or not columns or block.get("nowhere"):
                continue
            at, reqs = _new_table_requests(theirs, _insert_index(merged, position),
                                           rows, columns)
            if reqs:
                swallowed = _swallowed(theirs, reqs)
                plans.append((at, reqs,
                              {"key": key,
                               "after": _after_key(merged, position, swallowed),
                               **({"swallowed": swallowed} if swallowed else {}),
                               "note": f"`{key}`: a table of {rows}×{columns} "
                                       f"added by the source"}))
            elif notes is not None:
                notes.append(f"{key}: a table the source adds where the document has no "
                             f"paragraph to write in — before the table it opens on, or "
                             f"between two tables — cannot be built")
    shaped: list[dict] = []
    steps: list[tuple[int, int, list[dict]]] = []
    seen: set[int] = set()
    for at, reqs, told in sorted(plans, key=lambda p: -p[0]):
        if at in seen:
            continue  # two tables added at one index: one send can place one of them
        seen.add(at)
        shaped.append(told)
        steps.append((at, 1, reqs))
        if told.get("moved"):
            # Its delete only with its insert: a table deleted this round and built
            # the next would read, in between, as a table the document deleted.
            steps.append(deletes[told["key"]])
    out: list[dict] = []
    for _, _, reqs in sorted(steps, key=lambda p: (-p[0], p[1])):
        out += reqs
    if out:
        # A range a reader's chip pushed onto an empty paragraph's mark goes with the
        # mark when a new table's empty paragraph is swallowed — and a table built
        # after that paragraph is found again by that very key (`anchor_tables`).
        # Planted back first: `requests` does the same for the batch of words, but
        # this batch goes before it, against a document read again in between.
        #
        # And an orphan goes first for the same reason it heads `requests`, one step
        # earlier: a reader's backspace at the start of an empty paragraph leaves that
        # paragraph's range inside the one that survives, naming nothing, and
        # `insertTable` at the mark it sits on splits the paragraph and hands it to the
        # empty one the insert leaves behind. That paragraph is then read back under
        # the dead name, and the re-plan — which reads the document again between this
        # batch and the words — sees the very block the reader deleted standing there
        # for the source to write into, so the deletion is undone and the source's
        # words go in (offline chain-10 seed 1430231, shape `astral`).
        out = doc_ir.orphan_requests(theirs) + doc_ir.replant_requests(theirs) + out
    return out, shaped


_END = 1 << 30  # a table appended at the end of the body: after every index there is


def _after_key(merged: list[dict], position: int, swallowed: str | None = None,
               eaten: str | None = None) -> str | None:
    """The key of the nearest block in front of this one that the document already
    has — where a table written from nothing will be found again once it exists.

    Never a block whose name this very batch takes away: the one whose mark a new
    table's insert swallows (`_swallowed`) and the one whose mark the delete of the
    body's last table eats (`_eaten`). An anchor is read back *after* the batch, and
    both of those come back unnamed.
    """
    for block in reversed(merged[:position]):
        if block.get("key") and block.get("span") and not block.get("moved"):
            if block["key"] in (swallowed, eaten):
                continue
            return block["key"]
    return None


def _eaten(theirs: dict, index: int, start: int) -> str | None:
    """The key the delete of the body's last table takes with the mark in front of it.

    `_delete_range` reaches back over that mark so the trailer is not left standing
    as a second empty paragraph, and the block it belongs to keeps its words and so
    its named range — unless the block is *itself* an empty paragraph, which is all
    mark: the delete then covers its range whole and Docs drops it
    (`recover_eaten`).
    """
    if index == 0 or start >= theirs["blocks"][index]["span"][0]:
        return None
    before = theirs["blocks"][index - 1]
    span = before.get("span")
    if before.get("key") and span and span[0] >= start:
        return before["key"]
    return None


def _swallowed(theirs: dict, reqs: list[dict]) -> str | None:
    """The key a new table's swallow takes with the mark it deletes, if it takes one.

    `_new_table_requests` gets rid of the empty paragraph `insertTable` leaves by
    deleting the mark of the block in front, which merges the two the way the Delete
    key does. That block keeps its words, and so its named range — unless it is
    *itself* an empty paragraph, which is all mark: then the delete covers its range
    whole and Docs drops it, and the block comes back unnamed. Two things cannot wait
    for the settle to key it again from its words (`_adopt_by_words`): a table
    anchored on it, which `anchor_tables` looks for in between and would never find,
    and the block itself, which the re-plan after this batch reads as one the reader
    deleted (`recover_swallowed`).
    """
    for req in reqs:
        span = req.get("deleteContentRange", {}).get("range")
        if not span:
            continue
        gone = [span["startIndex"], span["endIndex"]]
        block = next((b for b in theirs["blocks"] if b.get("span") == gone), None)
        if block is not None and block.get("key"):
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
      leaves both blocks exactly as the file has them — all but that block's named
      range, when the block is an empty paragraph and its whole range is that mark
      (`_swallowed`);
    - written in front of a table there is no paragraph at that index at all. It goes
      at the mark of the paragraph before it, and the empty half lands *after* the new
      table — between the two, which is where Docs wants a paragraph anyway;
    - written after everything, it goes to the end of the segment, and Docs keeps a
      paragraph after it, because a document ends on one.

    Which leaves the place where no paragraph is to be had at all: between two tables,
    or at the very start of a document that opens on one. There is nowhere to write
    and no requests come back — the caller says so and leaves the document alone.
    Written anyway at `at - 1`, the new table was built inside the last cell of the
    table before it, the words never reached it, `anchor_tables` could not find it and
    every re-plan built another (the campaign's 'table-in-a-table', fixed and out of
    `fuzz_docs.KNOWN`).
    """
    table = {"rows": rows, "columns": columns}
    if at is None:
        return _END, [{"insertTable": table | {"endOfSegmentLocation": {}}}]
    after = next((b for b in theirs["blocks"] if b["span"][0] == at), None)
    before = next((b for b in theirs["blocks"] if b["span"][1] == at), None)
    if _structural(after):
        if before is None or _structural(before):
            return at, []                              # nowhere to write it
        return at - 1, [{"insertTable": table | {"location": {"index": at - 1}}}]
    out = [{"insertTable": table | {"location": {"index": at}}}]
    if at > 1 and before is not None and not _structural(before):
        out.append({"deleteContentRange": {"range": {"startIndex": at - 1, "endIndex": at}}})
    return at, out


def _op_order(op: tuple) -> tuple:
    """Rows before columns, each back to front, and at one index the delete first.

    An insert is written beside the line in front of it (`_grid_requests`), so every
    op leaves the lines in front of it where they were and the ones still to come
    hold. The row ops need column 0 and the column ops row 0, and both are there as
    long as the table keeps a line of the document's (`_merged_lines`).
    """
    line, how, index = op
    return line != "row", -index, how != "delete"


def _grid_requests(start: int, ops: list[tuple]) -> list[dict]:
    """Rows and columns added or taken away, back to front so the indices hold."""
    where = {"index": start}
    out = []
    for line, how, index in sorted(ops, key=_op_order):
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


def _neighbour(blocks: list[dict], index: int, step: int,
               going: set[int]) -> dict | None:
    """The nearest block on one side of `index` that this batch is not deleting."""
    at = index + step
    while 0 <= at < len(blocks) and at in going:
        at += step
    return blocks[at] if 0 <= at < len(blocks) else None


def _delete_range(blocks: list[dict], index: int, going: set[int],
                  ends: bool = True, lead: list | None = None,
                  filled: bool = False) -> tuple[int, int]:
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

    A structural element — a table, or a table of contents — is deleted by its own
    span, which leaves the paragraphs on both sides of it as they were (measured).
    Two paragraphs no request can delete come with a table,
    though: the empty `lead` in front of a table a body opens on, and the empty
    trailer after one it ends on. The first goes with the table from its start; the
    second stays, and the mark in front of the table goes instead — which, measured,
    leaves the paragraph before the table as the body's last, and no empty one after.
    Unless a block is being written into that trailer (`filled`): the two would then
    be merged into one paragraph, so the table goes alone.
    """
    start, end = blocks[index]["span"]
    if _structural(blocks[index]):
        if index == 0 and lead and lead[1] == start:
            return lead[0], end
        if (index == len(blocks) - 1 and not ends and not filled and index > 0
                and not _structural(blocks[index - 1])):
            return start - 1, end
        return start, end
    if not _mark_is_taken(blocks, index, going, ends, lead, filled):
        return start, end
    if index == 0 or _structural(blocks[index - 1]):
        # No mark to take: the block's words go and an empty paragraph stays in front
        # of the table. Docs wants one between two tables anyway.
        return start, end - 1
    return start - 1, end - 1


def _orphan_range(block: dict, start: int, end: int,
                  cuts: Iterable[tuple[int, int]] = ()) -> list[dict]:
    """The `deleteNamedRange` a delete needs when the block's own range outlives it.

    A block standing in front of a table gives up the *previous* block's paragraph
    mark and keeps its own (`_delete_range`), and a range may live on that mark: an
    empty paragraph is all mark, and a reader's chip or word pushes a range onto the
    mark of one (`doc_ir.apply_keys` records where a range really is). Nothing of the
    range's own text is deleted then, so Docs keeps it — and the document goes on
    saying this block is there, on a mark that now belongs to the paragraph the two
    were merged into.

    What that costs is identity, twice over. A *move* plants the block's range again
    where the block went, so the document holds two ranges of one name; and the stale
    one sits where the next block written will be, so the sync after hands that block
    this one's key and the block that owned the key is renamed from its words
    (chain-8 seed 41000: an empty heading left in front of a table the source added
    after a table of contents, moved one step later, ended up wearing
    `paragraph:second-section`'s identity). A range is destroyed with its text or not
    at all, so where the text does not go the range is named and deleted.

    Named only where it really outlives *the batch*, not this one delete: a run of
    blocks that go together hands each mark leftwards, so the block in front of the
    table gives up its neighbour's mark while its own is covered by the delete of the
    block behind it — and a `deleteNamedRange` for a range Docs has already dropped
    with its text is refused, which throws out the whole batch and kills the sync
    (offline chain-6 seed 550667: two empty paragraphs in front of a table, both
    deleted in one step). So every cut of the batch is asked, this one among them.
    """
    span = block.get("range") or doc_ir.anchor_range(block)
    if not block.get("rangeId") or not span:
        return []
    if any(low <= span[0] and span[1] <= high
           for low, high in [(start, end), *cuts]):
        return []
    return [{"deleteNamedRange": {"namedRangeId": block["rangeId"]}}]


def _mark_is_taken(blocks: list[dict], index: int, going: set[int], ends: bool,
                   lead: list | None = None, filled: bool = False) -> bool:
    """Whether this block's own paragraph mark must survive the delete — because a
    structural element follows it (a table or a table of contents: the newline in
    front of one cannot be deleted), because it ends the body, or because the deleted
    block that follows it takes this one's."""
    after = index + 1
    if after >= len(blocks):
        return ends
    if after in going:
        return (_delete_range(blocks, after, going, ends, lead, filled)[0]
                < blocks[after]["span"][0])
    return _structural(blocks[after])


def _pairs(live: dict, want: dict):
    """(live block, merged block) for a block and, if it is a table, for every block
    in its cells — where identity is the cell's place, not a named range."""
    yield live, want
    for r, row in enumerate(live.get("rows", [])):
        for c, cell in enumerate(row):
            other = _cell(want, r, c) or []
            if other and other[0].get("joined"):
                # The sides disagree on how many paragraphs the cell has: its text is
                # merged as one, with the breaks in it, and written the same way.
                if cell and all(inner.get("span") for inner in cell):
                    yield _joined(cell), other[0]
                continue
            for inner, merged in zip(cell, other):
                if inner.get("span"):
                    yield inner, merged


def _joined(cell: list[dict]) -> dict:
    """A cell's paragraphs read as one block: the marks between them are newlines in
    its text, one unit each, and the last one — the cell's own — stays outside."""
    runs: list[dict] = []
    for i, block in enumerate(cell):
        if i:
            runs.append({"text": "\n", "width": 1})
        runs += block.get("runs", [])
    return {"kind": "paragraph", "runs": runs,
            "span": [cell[0]["span"][0], cell[-1]["span"][1]]}


def _insert_index(merged: list[dict], position: int) -> int | None:
    """Where a new block's text goes: at the start of the block that follows it,
    or None when nothing follows and it is appended to the document instead.

    A block that is being moved is no anchor: the span it still has is the place it
    is about to be deleted from, which says nothing about where the new one goes.
    """
    anchor = _anchor(merged, position)
    return anchor["span"][0] if anchor else None


def _anchor(merged: list[dict], position: int) -> dict | None:
    """The block `_insert_index` writes in front of."""
    for block in merged[position + 1:]:
        if block.get("span") and not block.get("moved"):
            return block
    return None


def refuse_back_to_back(merged: list[dict], notes: list[str]) -> None:
    """Refuse to write a table where it would stand right behind another one.

    Docs keeps an undeletable paragraph between two tables (the `between_tables`
    shape), so a file asking for two tables with nothing between them asks for
    something the document cannot hold: `insertTable` splits the paragraph at the
    index it goes to, the half in front of the new table becomes that mandatory
    paragraph, and the file has no block for it. Nothing says which of the two empty
    paragraphs is which afterwards, and where the second one is the body's last it is
    hidden altogether (`doc_ir._hide_trailer`), so the settle keys the leftover with
    the name of the paragraph the file wanted *after* the table — the order reads as
    the base's, the move is undone in silence, and a restyle planned for that
    paragraph in the same run is planned onto nothing next time round (chain-8 seed
    280039, shrunk to a `move` and a `restyle`).

    So the table stays where the document has it and the report says why. The mirror
    case — a table written in *front* of another — is not this: it goes at the mark
    of the paragraph before it and the empty half lands between the two, which is
    where Docs wants a paragraph anyway (`_new_table_requests`).
    """
    for block in list(merged):
        position = next(i for i, b in enumerate(merged) if b is block)
        built = block.get("moved") or block.get("origin") == "added by the source"
        if block.get("kind") != "table" or not built or not position:
            continue
        if not _structural(merged[position - 1]) or block.get("nowhere"):
            continue
        # Only where there *is* a paragraph to split. With a table or nothing at all in
        # front of it the insert has no index to take and `_new_table_requests` refuses
        # it already, in the words that fit that case — the one below would steal them.
        anchor = _anchor(merged, position)
        if anchor is None or _structural(anchor):
            continue
        key = block.get("key")
        if block.get("moved"):
            block["moved"] = False
            _put_back(merged, block)
            notes.append(f"{key}: the source moved it right behind another table, where "
                         f"the document keeps a paragraph of its own — left where the "
                         f"document has it")
        else:
            block["nowhere"] = True
            notes.append(f"{key}: the source adds it right behind another table, where "
                         f"the document keeps a paragraph of its own — not written")


def refuse_eaten_anchor(theirs: dict, merged: list[dict], notes: list[str]) -> None:
    """Refuse to move the body's last table in front of the paragraph it ends behind.

    A body may not end on a table, so Docs keeps an empty paragraph after one and no
    request deletes it: a trailing table goes out by its own span *and the mark in
    front of it*, which leaves the block before it as the body's last and no stray
    empty one after (`_delete_range`). That block keeps its words, and so its named
    range — unless it is itself an empty paragraph, which is all mark. Then it is
    destroyed, and where the file puts the table in *front* of that very block, the
    place the table is written goes with it: `insertTable` splits the paragraph now
    standing there, which is what is left of the block the delete swallowed, and the
    table lands behind it again. The document comes back in the base's order with
    the paragraph unkeyed, the settle keys it from its words — it is empty, so its
    name is its kind and it fits — and the move is undone in silence (chain-8 seed
    890070, shape `ends_on_table`, shrunk to a `move` and a `collide`).

    The mirror of `refuse_back_to_back`, and refused for the same reason: the
    document cannot hold what the file asks for, and saying so leaves the person
    with one empty paragraph on the wrong side of a table rather than a sync that
    writes the same two requests every time it runs.
    """
    live = theirs.get("blocks", [])
    if len(live) < 2 or not theirs.get("trailer") or not _structural(live[-1]):
        return
    last, before = live[-1], live[-2]
    if _structural(before) or block_text(before).strip() or not last.get("key"):
        return
    position = next((i for i, b in enumerate(merged)
                     if b.get("key") == last["key"]), None)
    if position is None or not merged[position].get("moved"):
        return
    anchor = _anchor(merged, position)
    if anchor is None or anchor.get("key") != before.get("key"):
        return
    merged[position]["moved"] = False
    _put_back(merged, merged[position])
    notes.append(f"{last['key']}: the source moves it in front of "
                 f"{before.get('key')}, and the body may not end on a table — the mark "
                 f"the table gives up on its way out is that empty paragraph's own, so "
                 f"the place the table would be written goes with it. Left where the "
                 f"document has it")


def refuse_nowhere(theirs: dict, merged: list[dict], notes: list[str]) -> None:
    """Refuse to write a block whose place in the document has no paragraph in it.

    Nothing can be written at a table's or a table of contents' own index, so a block
    in front of one goes in as "\\ntext" at the paragraph mark before it — and when
    the block before is structural too, there is no such mark: that index is inside
    its last cell. The paragraph went into the table and was never seen again, and a
    *moved* block was deleted from its old place first, so the move destroyed it
    outright (offline chain-8 seed 7008, shrunk to one source op and no reader).

    Docs keeps an undeletable paragraph between two tables anyway — that is the
    `between_tables` shape, where the borrowed mark is that paragraph's and the
    arithmetic is right. A document that has none has nowhere for this block, so it
    is left unwritten and the report says why. Before `restore_undeletable`, which
    decides what can be deleted and must see a move this one has taken back.

    A block the source *adds* there is not merely unwritten: the settle regenerates
    the file from the document it wrote, so the paragraph goes out of the file too
    and the next sync will not try again. "Not written" on its own reads as a thing
    still waiting, so the note says where it went and how to get it in — a paragraph
    between the tables in the browser, or somewhere else in the file. A refused
    *move* keeps its block, which is why only this half says it.
    """
    for block in list(merged):
        position = next(i for i, b in enumerate(merged) if b is block)
        if not _written_here(block) or not _nowhere(theirs, merged, position):
            continue
        key = block.get("key")
        if block.get("moved"):
            block["moved"] = False
            _put_back(merged, block)
            notes.append(f"{key}: the source moved it between two tables, where the "
                         f"document has no paragraph to write in — left where the "
                         f"document has it")
        else:
            block["nowhere"] = True
            notes.append(f"{key}: the source adds it between two tables, where the "
                         f"document has no paragraph to write in — not written, and "
                         f"the settle takes it back out of the file: put a paragraph "
                         f"between the tables in the document, or move it in the file")


def _put_back(merged: list[dict], block: dict) -> None:
    """Put a block whose move was refused back where the document has it.

    "Left where the document has it" has to be true of the list as well, not only of
    the requests: every index the sync computes comes from a block's span, and a span
    says where a block *is*. A block that stays put while `merged` keeps it at the
    file's position is an anchor pointing at the wrong end of the document — the move
    of a table in front of such a paragraph was written at the paragraph's old index,
    which is where that table already stood, so the document came back unchanged, the
    next round planned the same move again, and the three rounds `_write_structure`
    allows ran out with the table blank, its words nowhere and an empty paragraph left
    over from each attempt (fresh-seed 40344, shape `between_tables`).
    """
    merged.pop(next(i for i, b in enumerate(merged) if b is block))
    span = block.get("span")
    where = len(merged)
    if span:
        where = next((i for i, b in enumerate(merged)
                      if b.get("span") and not b.get("moved") and b["span"][0] > span[0]),
                     len(merged))
    merged.insert(where, block)


def _nowhere(theirs: dict, merged: list[dict], position: int) -> bool:
    """Whether the place this block is written at is inside a table's last cell."""
    anchor = _anchor(merged, position)
    if not _structural(anchor):
        return False
    at = next((i for i, b in enumerate(theirs["blocks"])
               if b.get("span") == anchor.get("span")), None)
    # The first block of a body that opens on a table has the hidden paragraph in
    # front of it to write in (`doc_ir._hide_trailer`), and that is `lead`.
    return at is not None and at > 0 and _structural(theirs["blocks"][at - 1])


def restore_undeletable(theirs: dict, merged: list[dict], notes: list[str]) -> None:
    """Put back a block the merge means to delete and that no request can delete.

    An empty paragraph between two tables is the case: its own mark is the newline in
    front of a table, and the block before it is a table with no mark to lend, so
    `_delete_range` comes back with a range of length zero. Docs refuses that, and a
    refusal throws out the whole batch — a sync died over a paragraph Docs wants to be
    there anyway. It stays where it is, and the report says so.

    Round by round, because keeping one block changes what the next delete may take:
    `_mark_is_taken` asks whether the block after this one is going too.
    """
    for _ in range(len(theirs["blocks"]) + 1):
        by_key = {b["key"]: b for b in merged if b.get("key")}
        going = {i for i, live in enumerate(theirs["blocks"]) if _goes(live, by_key)}
        ends = not theirs.get("trailer")
        filled = any(_written_here(b) and _insert_index(merged, p) is None
                     for p, b in enumerate(merged))
        stuck = next((i for i in sorted(going)
                      if _empty_range(theirs["blocks"], i, going, ends,
                                      theirs.get("lead"), filled)), None)
        if stuck is None:
            return
        live = theirs["blocks"][stuck]
        key = live.get("key")
        if by_key.get(key, {}).get("moved"):
            # It is not being dropped but moved, and the move is a delete and a write:
            # leaving it where the document has it is what `_apply_source_moves` does
            # for everything else it cannot carry.
            by_key[key]["moved"] = False
            _put_back(merged, by_key[key])
            notes.append(f"{key}: the source moved it, but it stands between two tables "
                         f"where nothing can be deleted — left where the document has it")
            continue
        notes.append(f"{key}: dropped by the source, but it stands between two tables "
                     f"where no request can delete it — kept")
        merged.insert(_after_live(theirs, merged, stuck),
                      dict(live) | {"origin": "kept from the document"})


def _empty_range(blocks: list[dict], index: int, going: set[int], ends: bool,
                 lead: list | None, filled: bool) -> bool:
    start, end = _delete_range(blocks, index, going, ends, lead, filled)
    return end <= start


def _after_live(theirs: dict, merged: list[dict], index: int) -> int:
    """Where a block of the document goes back into the merge: behind the merged block
    that carries the key of the one in front of it there, or at the front.

    Never behind one the source moves: this block is kept because nothing can move it,
    and following the neighbour that *is* moving says it goes along. A table moved up
    past a paragraph then had the empty paragraph behind it for its own anchor, so it
    was built again exactly where it stood — and again on the next pass, blank each
    time (offline chain-8 seed 994424).
    """
    for live in reversed(theirs["blocks"][:index]):
        at = next((i for i, b in enumerate(merged)
                   if b.get("key") == live.get("key") and not b.get("moved")), None)
        if at is not None:
            return at + 1
    return 0


def plan(base: dict, ours: dict, theirs: dict) -> dict:
    """The whole planning step: keys, merge, the grid, the edits."""
    doc_ir.key_blocks(ours)
    restore_unreadable(base, ours)
    restore_unreadable(theirs, ours, base)
    recover_tables(base, theirs)
    _unseen_pictures(ours, base)
    inherit_keys(base, ours)
    result = merge(base, ours, theirs)
    for block in result["blocks"]:
        if block.get("origin") == "added by the source" and not _writable_block(block):
            result["notes"].append(f"{block.get('key')}: a new block with a chip in it that no "
                                   f"request can create (or a picture file that is not there) "
                                   f"cannot be written")
    unwritten_pictures(base, ours, theirs, result["blocks"], result["notes"])
    refuse_back_to_back(result["blocks"], result["notes"])
    refuse_eaten_anchor(theirs, result["blocks"], result["notes"])
    refuse_nowhere(theirs, result["blocks"], result["notes"])
    restore_undeletable(theirs, result["blocks"], result["notes"])
    result["structure"], result["shaped"] = structure(theirs, result["blocks"], result["notes"])
    unwritten_levels(theirs, result["blocks"], result["notes"])
    result["requests"] = requests(theirs, result["blocks"])
    return result


def unwritten_levels(theirs: dict, merged: list[dict], notes: list[str]) -> None:
    """Say when an item cannot be given the nesting level the source asks for.

    No request sets one. `createParagraphBullets` says nothing about a level: Docs
    reads it off the paragraph's leading tabs, which the merge does not write. So a
    level lives on the paragraph mark that carries it and survives exactly as long as
    that mark does — and there are two ways for it not to.

    A block **written from nothing** — one the source added, or moved, a move being a
    delete and a write — comes out at the level of the list it lands in, whether that
    is deeper than the source asks for or shallower (offline chain-4 seed 430296,
    chain-8 seed 530265).

    Which list that is, is which way round the block is written. The usual way is
    "text\\n" at the start of the block that follows it (`_insert_index`): Docs splits
    that paragraph and the half in front — the new block — keeps the style of the one
    that was already there, bullet and nesting level among it. It is only where
    nothing follows, or a table does, that the text goes in as "\\ntext" at the mark
    behind it and wears the style of the block in *front* of it. This said the second
    of those for every case and so predicted the wrong level for the commonest one:
    a level-0 item moved to just before a nested one came out nested, in silence
    (offline chain-12 seed 1640036, shape `tabs`, shrunk to two appends and a
    restyle-and-move with no reader at all).

    And a block whose **paragraph mark a delete in front of it hands over**: Docs
    merges the two paragraphs keeping the first one's style, so the block after a
    deleted one wears the deleted one's, and a run of deletes passes the first one's
    along. Everything else about that style is put back — the named style and the
    bullet by `carry_unimported` / `restore_bullets`, the measures by the block's own
    restyle — and the level alone cannot be (offline chain-4 seed 430587: the source
    moved the item standing in front of a nested one, and the nested one came out at
    the moved item's level, the reader having touched neither).

    Saying so before the write is the only honest thing left: nothing else could see
    it, the reader having left the block alone and the base agreeing with the
    document afterwards, so both the loss oracle and the convergence check are
    satisfied (`fuzz_docs._shape_arrived` is the judge that is not). Writing the tabs
    is the alternative and waits for a live measurement: a Docs that does not eat
    them leaves them in the words, which is a corruption where this is a level
    (docs/google-docs.md, "Remaining risks").

    After `structure`, which is where a move can still be taken back.
    """
    def level_of(block: dict) -> int:
        return block.get("level", 0) if block.get("kind") == "item" else 0

    lands: list[int | None] = []            # the level each item really comes out at
    for at, block in enumerate(merged):
        if block.get("kind") != "item":
            lands.append(None)
            continue
        if not _written_here(block):
            lands.append(block.get("level", 0))
            continue
        # Whose paragraph style the new text wears, and so whose place in a list; where
        # no list stands there, `createParagraphBullets` starts one at level 0. Either
        # way the level can be deeper than the source asks for as easily as shallower —
        # one nested item beside it is all it takes (offline chain-8 seed 530265: the
        # source moved a level-0 item to the end, behind an item the reader had nested;
        # chain-12 seed 1640036: it moved one to just in front of a nested item).
        anchor = _anchor(merged, at)
        splits = anchor is not None and not _structural(anchor)
        out = level_of(anchor) if splits else \
            (lands[at - 1] if at and lands[at - 1] is not None else 0)
        lands.append(out)
        if out != block.get("level", 0):
            whose = "the item it is written in front of" if splits \
                else "the item in front of it"
            notes.append(f"{block.get('key')}: written from nothing as a list item, and no "
                         f"request gives a bullet its nesting level — it comes out at level "
                         f"{out}, the level of {whose}, not at "
                         f"{block.get('level', 0)}")
    by_key = {b["key"]: b for b in merged if b.get("key")}
    live = theirs.get("blocks", [])
    going = {i for i, block in enumerate(live) if _goes(block, by_key)}
    for index, block in enumerate(live):
        want = by_key.get(block.get("key"))
        if index in going or want is None or _written_here(want):
            continue
        donor = None                      # the first of the run of deletes in front
        at = index - 1
        while at in going and not _mark_is_taken(live, at, going, True, theirs.get("lead")):
            donor, at = live[at], at - 1
        if donor is None or level_of(donor) == level_of(want):
            continue
        notes.append(f"{want.get('key')}: the paragraph in front of it goes, and Docs hands "
                     f"its style to this one — no request gives a bullet its nesting level, "
                     f"so it comes out at level {level_of(donor)}, not at {level_of(want)}")


# ---------------------------------------------------------------- tabs

def on_tab(requests: list[dict], tab: str | None) -> list[dict]:
    """The requests aimed at one tab: `tabId` in every location and range.

    A request without one goes to the first tab (measured), so the first tab's are
    sent as they are and every other tab's are stamped: an `index` is a Location,
    a `startIndex` a Range, and `endOfSegmentLocation` names no index at all.
    """
    if not tab:
        return requests

    def stamp(value, key=None):
        if isinstance(value, list):
            return [stamp(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {k: stamp(v, k) for k, v in value.items()}
        if "index" in value or "startIndex" in value or key == "endOfSegmentLocation":
            out["tabId"] = tab
        return out

    return [stamp(r) for r in requests]


def pair_tabs(base: dict, ours: dict, theirs: dict) -> dict:
    """Which tab of the file is which tab of the document, and what happens to tabs.

    The same three-way rule as for blocks, one level up, with the tab id as identity:
    a tab the source added is created (`create`, written by the sync like any tab
    whose base is empty), one it deleted goes if the document left it as it was, one
    it renamed is renamed if the document kept the old title — and where the document
    also moved, the document wins, with a note. A tab the reader added is theirs and is
    simply read into the file; one the reader deleted stays deleted.

    `pairs` is `(tab id, our tab, base tab)` for every tab past the first that is
    written; `requests` the tab edits that go before any text, `applied` and `notes`
    what the report says about them, and `rename` the document's new name, which is
    no request at all (`document_title`).
    """
    live = {p["tab"]: p for p in theirs.get("tabs", []) if p.get("tab")}
    was = {p["tab"]: p for p in base.get("tabs", []) if p.get("tab")}
    ours_ids = {p.get("tab") for p in ours.get("tabs", [])}
    out: dict = {"pairs": [], "create": [], "requests": [], "applied": [], "notes": [],
                 "rename": None}
    taken: set = set()
    for part in ours.get("tabs", []):
        tab, name = part.get("tab"), part.get("title", "")
        if tab in live:
            taken.add(tab)
            old, now = was.get(tab, {}), live[tab]
            if name != now.get("title", "") and name:
                if now.get("title", "") == old.get("title", now.get("title", "")):
                    out["requests"].append({"updateDocumentTabProperties": {
                        "tabProperties": {"tabId": tab, "title": name}, "fields": "title"}})
                    out["applied"].append(f"tab {now.get('title')!r} renamed {name!r}")
                elif name != old.get("title"):
                    out["notes"].append(f"tab {now.get('title')!r}: renamed on both sides — "
                                        f"the document's title is kept, not {name!r}")
            out["pairs"].append((tab, part, old or {"blocks": []}))
        elif tab in was:
            if doc_ir.blocks_html(part["blocks"]) != doc_ir.blocks_html(was[tab]["blocks"]):
                out["notes"].append(f"tab {name!r} was deleted in the document; the source's "
                                    f"changes to it are not written")
        else:
            # A tab of that title nobody knew of and nothing is in: the one a sync that
            # died after creating it left behind. Anything else would be a second tab.
            same = [t for t, p in live.items() if t not in was and t not in taken
                    and p.get("title") == name and not p["blocks"]]
            if same:
                taken.add(same[0])
                part["tab"] = same[0]
                out["pairs"].append((same[0], part, {"blocks": []}))
            else:
                out["create"].append(part)
    for tab, old in was.items():
        if tab in ours_ids or tab not in live:
            continue
        now, name = live[tab], old.get("title", "")
        children = [p for p in live.values() if p.get("parent") == tab and p["tab"] in ours_ids]
        if (doc_ir.blocks_html(now["blocks"]) != doc_ir.blocks_html(old["blocks"])
                or now.get("title") != old.get("title") or children):
            out["notes"].append(f"tab {name!r} was deleted in the source, but the document "
                                f"changed it (or keeps tabs inside it) — kept")
        else:
            out["requests"].append({"deleteTab": {"tabId": tab}})
            out["applied"].append(f"tab {name!r} deleted")
    first_tab_title(base, ours, theirs, out)
    document_title(base, ours, theirs, out)
    tab_order(base, ours, theirs, out)
    return out


def first_tab_title(base: dict, ours: dict, theirs: dict, out: dict) -> None:
    """The first tab's own name, which the file says in its `b2s-tab` meta.

    Every other tab names itself on its `<section>`; the first tab is the file's body
    and had nowhere to say it, because the file's `<title>` is the *document's* name
    and the two are different things — a document of one tab has both. So the source
    could rename any tab but the one everybody actually looks at, and a rename written
    into the file went twice over: dropped by the sync and then taken back out by the
    settle, which reads the document's name back.

    The rule is the one the other tabs follow, with one difference at the beginning:
    a `push` has no base, and unlike the document's name — which the import takes from
    the file's `<title>` at birth — the first tab's title is Drive's own default, which
    nothing but this has ever said. So with no base the file's name is written rather
    than treated as a side of a disagreement nobody can settle.
    """
    mine, now = ours.get("tab_title"), theirs.get("tab_title")
    was, tab = base.get("tab_title"), theirs.get("tab")
    if not mine or not tab or mine == now or mine == was:
        return
    if was is not None and now != was:
        out["notes"].append(f"the first tab was renamed on both sides — it keeps {now!r}, "
                            f"not {mine!r}")
        return
    out["requests"].append({"updateDocumentTabProperties": {
        "tabProperties": {"tabId": tab, "title": mine}, "fields": "title"}})
    out["applied"].append(f"the first tab renamed {mine!r}")


def document_title(base: dict, ours: dict, theirs: dict, out: dict) -> None:
    """The document's name, which the file says in its `<title>`.

    A Google Doc's title *is* its name in Drive, and no `batchUpdate` request writes
    one — `push` gives the document the file's title at birth and nothing said it
    again, so a source that renamed the document had the rename dropped and then
    taken back out of the file by the settle, which reads the old name back. The
    three-way rule is the one everything else follows: renamed in the file alone and
    it is written (through Drive, `doc_sync.rename_document`, which is why this is
    `rename` and not a request); renamed in the document alone and the file simply
    follows at the settle; renamed on both sides and the document's name stands,
    with a note.

    A base with no title at all — one an older version of this tool wrote — cannot
    say who moved, so the document's name stands and the note says that too. No base
    at all is `push` making the document out of this very file: it is named from the
    file's title there, and there is nothing to say.
    """
    mine, now = ours.get("title"), theirs.get("title")
    was = base.get("title")
    if not mine or mine == now or mine == was:
        return
    if was is None and not (base.get("blocks") or base.get("tabs")):
        return
    if was is None:
        out["notes"].append(f"the file calls the document {mine!r} and the document calls "
                            f"itself {now!r}; the base does not say which of them renamed "
                            f"it, so the document's name is kept")
    elif now != was:
        out["notes"].append(f"the document was renamed on both sides — it keeps {now!r}, "
                            f"not {mine!r}")
    else:
        out["rename"] = mine
        out["applied"].append(f"the document renamed {mine!r} (in Drive: no request "
                              f"writes a document's title)")


def tab_order(base: dict, ours: dict, theirs: dict, out: dict) -> None:
    """A tab the source moved, which is not written.

    Blocks the source moved go back where the file has them, because a move is a
    delete and a write — and a tab cannot be written from nothing (everything in it
    would have to be made again, chips and equations and all), so the order of the
    tabs is the document's, whole. Said out loud rather than dropped: the settle
    rewrites the file in the document's order, so a reorder in the file disappears
    twice over.

    A tab *can* be moved: `TabProperties.index` carries no "Output only" marker, and
    `updateDocumentTabProperties` takes any field of it (discovery document, revision
    20260427). What is not known is what happens to the tabs it passes — `addDocumentTab`
    says it pushes the later ones along and nothing says that an update does — and a
    wrong guess rearranges somebody's tab strip, which is the one thing this tool does
    not do on a hunch. So the reorder waits for a live measurement (docs/google-docs.md)
    and the note says the order stands, not that nothing could move it.

    Only what the *source* moved: where the file still has the base's order, the
    reader moved a tab and the file is simply following it.
    """
    def order(ir, known):
        return [p["tab"] for p in ir.get("tabs", []) if p.get("tab") in known]

    known = ({p.get("tab") for p in ours.get("tabs", [])}
             & {p.get("tab") for p in theirs.get("tabs", [])}
             & {p.get("tab") for p in base.get("tabs", [])})
    mine, now = order(ours, known), order(theirs, known)
    if len(mine) < 2 or mine == now or mine == order(base, known):
        return
    titles = {p["tab"]: p.get("title", "") for p in theirs.get("tabs", []) if p.get("tab")}
    out["notes"].append(
        "the source puts the tabs in the order " + ", ".join(repr(titles.get(t, t))
                                                             for t in mine)
        + "; moving a tab is not written, so the document's order stands")


def tab_siblings(theirs: dict) -> dict:
    """The document's tabs by parent, each in the order the document shows them.

    The first tab is a root tab like any other — index 0, and the one every other
    root tab counts from — so it heads the root row under whatever id the read gave
    it, `None` for a read that holds only that tab. A place is all the arithmetic
    wants, and that is one thing the file cannot name anyway.
    """
    out: dict = {None: [theirs.get("tab")]}
    for part in theirs.get("tabs", []):
        if part.get("tab"):
            out.setdefault(part.get("parent") or None, []).append(part["tab"])
    return out


def tab_index(part: dict, ours: dict, parent: str | None, siblings: list) -> int:
    """Where among its parent's tabs a tab the source added goes.

    `addDocumentTab` takes the index the new tab is to have and pushes the later ones
    along, so a tab lands where the file puts it rather than always at the end: after
    the nearest tab in front of it in the file that the document already has — or
    that this run has just made, the creates going in file order with each new id
    written back into the part before the next one is placed.

    The file's body is the document's first tab, so a root tab whose only forerunner
    is the body goes at 1: the file has no way of saying anything stands in front of
    the body, and nothing may.
    """
    parts = doc_ir.parts(ours)
    before: list = []
    for other in parts:
        if other is part:
            break
        before.append(other)
    for other in reversed(before):
        if (other.get("parent") or None) != parent:
            continue
        if other is parts[0]:
            return 1
        if other.get("tab") in siblings:
            return siblings.index(other["tab"]) + 1
    return 0


def add_tab_request(part: dict, parents: dict, ours: dict | None = None,
                    siblings: dict | None = None) -> dict:
    """`addDocumentTab` for a tab the source added, under its parent if that exists
    and where the file puts it among that parent's tabs.

    With no `ours` and `siblings` to say where that is, the request names no index
    and the tab lands at the end, which is where every tab this made used to land.
    """
    props = {"title": part.get("title") or "Tab"}
    if part.get("parent") in parents:
        props["parentTabId"] = part["parent"]
    if ours is not None and siblings is not None:
        under = props.get("parentTabId")
        props["index"] = tab_index(part, ours, under, siblings.get(under, []))
    return {"addDocumentTab": {"tabProperties": props}}
