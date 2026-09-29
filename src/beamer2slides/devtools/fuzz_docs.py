"""Fuzz the Google Docs sync: random edits on both sides, judged by the loss oracle.

    python tools/fuzz_docs.py offline --rounds 400
    python tools/fuzz_docs.py offline --rounds 200 --chain 4
    python tools/fuzz_docs.py offline --replay 1234 --chain 4

One round is a whole life of a document: a shape from the corpus is pushed, the reader
types in it, the source rewrites the file, `docs sync` runs, and
`doc_loss_oracle.check` asks whether anything the reader did disappeared without the
report saying so. Then the sync runs once more on a copy and must write **nothing**,
because a sync that does not settle would keep rewriting a document for ever.

Everything here is offline and deterministic. The document is `doc_world.World`, which
applies the real requests `doc_merge.plan` produces under Google's real index rules —
including throwing out a whole batch when one request is refused, which is the failure
that killed Slides syncs and which an applier that merges *state* can never see
(CLAUDE.md, seeds 608/616). The reader is a batch of requests too, so the named ranges
move exactly as Google moves them.

Three things were built in from the first round, all of them lessons the Slides
campaign paid for:

* **`collide`**: a source op that changes exactly what the reader just changed. Drawing
  both sides' targets at random makes the interesting case rare — over there it took
  text overrides from 5 in 200 rounds to 56, and it is the same here.
* **Chains** (`--chain N`): edit, sync, edit, sync. Several of the worst bugs over
  there only showed at depth 4 to 8, because they need a base that a previous sync
  wrote rather than one a push made.
* **Coverage counting**, printed at the end: which corpus shapes, which ops, which
  request kinds and which merge outcomes the campaign actually reached. A campaign that
  never plans an `insertTable` proves nothing about tables, and only counting says so.

The shrinker takes a failing seed apart op by op: each op carries its own salt, so
dropping one leaves the others drawing exactly what they drew before.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from .. import doc_ir, doc_merge, doc_sync
from ..doc_ir import Block, Ir, Kind, Mark, Measures, NamedDefault, Run, Style
from ..doc_sync import SyncReport, Written
from ..google_types import (DocsBatchUpdateResponse, DocsLocation, DocsNamedStyle,
                            DocsParagraphStyle, DocsRangeWrite, DocsRequest,
                            DocsTableCellLocation, DocsTabProperties, DocsTextStyle)
from ..typing_compat import assert_never
from . import doc_loss_oracle as oracle
from .doc_loss_oracle import CellSays, Finding, FrozenKey, Theme
from . import doc_world
from .doc_world import Refused

MIN_EDITS, MAX_EDITS = 1, 4
COLLIDE_CHANCE = 0.45
WORD = re.compile(r"\w+")
MARK_KEYS: set[str] = {key for key, _ in doc_ir.MARK_FIELDS}
#: A stretch of run styling as `_worn` compares it: (field, value) pairs, sorted.
Marks = tuple[tuple[str, object], ...]
#: What a reader op sends: the batches, each a list of requests, in the order they go.
Batches = list[list[DocsRequest]]
#: And what it touched: the keys of the blocks it worked on, and the fresh words it
#: typed, for `collide` to answer (None: a block with no key).
Touched = list[str | None]

FRESH = ["kestrel", "harbour", "lantern", "meadow", "quartz", "ribbon", "signal",
         "thicket", "umbrella", "vellum", "willow", "zephyr"]

# The whole dialect, not the one mark it is easiest to draw. `doc_merge.MANAGED` says
# which run fields a restyle may *clear* and `MANAGED_PARAGRAPH` which paragraph ones,
# so every field named there has to be reachable from both sides or the clearing is
# unfuzzed: a campaign that only ever sets italic proves nothing about a face, a size
# or an indent. `code` and `font` are one field on the wire (both are the run's family),
# so drawing one drops the other. One mark each, drawn whole.
RUN_MARKS: list[Style] = [
    {"bold": True}, {"italic": True}, {"underline": True}, {"strike": True},
    {"smallcaps": True}, {"code": True},
    {"font": "Georgia"}, {"font": "Roboto Mono"},
    {"fontsize": 9.0}, {"fontsize": 14.5},
    {"script": "super"}, {"script": "sub"},
    {"color": "#993333"}, {"highlight": "#ffee88"},
    # Ctrl+K's half of the dialect. `link` is in `doc_merge.MANAGED`, so a source
    # restyle names it whether or not the block asks for it — and no op had ever put
    # one on a run from either side, so what a restyle does to a link was as undrawn
    # as the bullet was (`read_link_word`).
    {"link": "https://example.invalid/anchor"}]

PARA_MARKS: list[Measures] = [
    {"align": "center"}, {"align": "justify"}, {"indent": 18.0},
    {"indent_first": 36.0}, {"line_spacing": 1.5}, {"shading": "#eef2ff"},
    {"space_above": 6.0}, {"space_below": 12.0},
    {"border_bottom": "1pt solid #333333"},
    {"border_bottom": "2.5pt dashed #cc0000 pad 4pt"},
    {"border_left": "3pt dotted #0000ff"},
    {"page_break": True}, {"keep_with_next": True}]


# ---------------------------------------------------------------- the corpus

def _p(text: str) -> Block:
    return {"kind": "paragraph", "runs": [{"text": text}]}


def _h(text: str) -> Block:
    return {"kind": "heading", "level": 1, "runs": [{"text": text}]}


def _i(text: str, level: int) -> Block:
    return {"kind": "item", "level": level, "runs": [{"text": text}]}


def _t(rows: Sequence[Sequence[str]]) -> Block:
    return {"kind": "table",
            "rows": [[[_p(cell)] for cell in row] for row in rows]}


def _shapes() -> dict[str, list[Block]]:
    """One entry per shape that has broken something, plus enough ordinary prose to
    give the merge somewhere to work (docs/google-docs.md names every one of these)."""
    return {
        # Docs' named styles past the heading levels. A Title is what the top of a
        # real document is, and `namedStyleType` is a field the merge names on every
        # paragraph it writes.
        "titled": [{"kind": "title", "runs": [{"text": "The Quarterly Report"}]},
                   {"kind": "subtitle", "runs": [{"text": "and what it does not say"}]},
                   _h("Summary"), _p("One paragraph of it."),
                   _p("And a second one after that.")],
        "prose": [_h("Notes"), _p("The first paragraph says one thing."),
                  _p("The second paragraph says another."),
                  _i("alpha", 0), _i("beta", 0), _i("gamma", 1), _p("A closing line.")],
        "ends_on_table": [_h("Results"), _p("What we found."),
                          _t([["year", "count"], ["2024", "7"], ["2025", "9"]])],
        "opens_on_table": [_t([["key", "value"], ["a", "1"]]),
                           _p("The table above says it all."), _p("And this follows.")],
        "two_tables": [_p("Before both."), _t([["a", "b"], ["1", "2"]]),
                       _t([["c", "d"], ["3", "4"]]), _p("After both.")],
        "between_tables": [_t([["a", "b"], ["1", "2"]]), _p("A paragraph in between."),
                           _t([["c", "d"], ["3", "4"]]), _p("The end.")],
        "equations": [_p("An inline "), {"kind": "paragraph", "runs": [
            {"text": "the value "}, {"chip": "equation", "text": "E=m{c}^{2}"},
            {"text": " holds everywhere"}]},
            {"kind": "paragraph", "runs": [{"chip": "equation", "text": "{\\int}_0^1 x"}]},
            _p("and prose after it.")],
        "imported_list": [_h("Steps"), _i("mix", 0), _i("bake", 0), _i("cool", 0),
                          _p("Follow them in order.")],
        "chips": [{"kind": "paragraph", "runs": [
            {"text": "ask "}, {"chip": "person", "text": "Ada", "value": "ada@example.com"},
            {"text": " before "}, {"chip": "date", "text": "Sep 20, 2026",
                                   "value": "2026-09-20T00:00:00Z"}]},
            {"kind": "paragraph", "runs": [
                {"chip": "image", "text": "", "value": "kix.pic0", "src": "media/plot.png",
                 "uri": "https://example.invalid/plot.png"}]},
            _p("A caption under it.")],
        "toc": [{"kind": "toc"}, _h("One"), _p("First section."),
                _h("Two"), _p("Second section.")],
        "astral": [_p("emoji \U0001F600 and maths \U0001D538 in one line"),
                   _p("a soft­hyphen and a   break"), _p("plain tail")],
        "dropdown": [{"kind": "paragraph", "runs": [
            {"text": "status "}, {"chip": "unknown", "text": ""}, {"text": " today"}]},
            _p("and a line after it.")],
        # A document with a look of its own (`THEME`). Its headings say nothing about
        # their own alignment: the centring is the theme's, and a paragraph reports
        # only what is set on it, so the file cannot say it and must not undo it.
        "themed": [_h("A themed heading"), _p("Under it, a paragraph."),
                   _h("Another heading"), _p("And prose after that."),
                   {"kind": "paragraph", "align": "center",
                    "runs": [{"text": "A line the source can move."}]}],
    }


#: The named styles the `themed` shape's world carries. No request writes one — the
#: API has none — so this is fixed for the life of a round, and every difference it
#: makes is a difference in what a paragraph *inherits*.
THEME: dict[str, DocsNamedStyle] = {
    "HEADING_1": {"paragraphStyle": {"alignment": "CENTER"}, "textStyle": {"bold": True}}}


def corpus(name: str) -> doc_world.World:
    """The world a round starts from. `tabs` is the one shape that is not a block
    list: a second tab makes every index and every key a tab's own."""
    shapes = _shapes()
    if name == "tabs":
        return doc_world.build([{"blocks": shapes["prose"]},
                                {"title": "Appendix", "blocks": shapes["ends_on_table"]}],
                               title="fuzz")
    world = doc_world.build([{"blocks": shapes[name]}], title="fuzz")
    if name == "themed":
        world.theme = {name: style.copy() for name, style in THEME.items()}
    return world


SHAPES = sorted(list(_shapes()) + ["tabs"])


# ---------------------------------------------------------------- the sync, offline

class Stager:
    """`doc_sync.Stager` without Drive: a staged picture gets a URL the world can hold."""

    def resolve(self, requests: Sequence[DocsRequest]) -> list[DocsRequest]:
        out: list[DocsRequest] = []
        for request in requests:
            image = request.get("insertInlineImage")
            if image and image["uri"].startswith(doc_merge.STAGE):
                staged = copy.deepcopy(image)
                staged["uri"] = "https://staged.invalid/" + image["uri"][len(doc_merge.STAGE):]
                fixed: DocsRequest = {"insertInlineImage": staged}
                out.append(fixed)
                continue
            out.append(request)
        return out


def bootstrap(world: doc_world.World) -> Ir:
    """Every block keyed and named in the document, and a file that is the document's
    own read.

    That is `docs adopt`, not `docs push`, and deliberately: `push` would import HTML
    and get back only what an import can carry, while the corpus shapes hold chips,
    equations, dropdowns and a table of contents that no import can make. Starting
    from the document means the campaign measures the journey somebody actually has —
    a document written in the browser for a year, adopted, edited in the file, synced
    back — rather than one this tool made out of its own dialect.
    """
    ir = doc_world.read_ir(world, None, None)
    for part in doc_ir.parts(ir):
        stamp = None if part is ir else part.get("tab")
        doc_ir.key_blocks(part)
        requests = doc_merge.on_tab(doc_ir.name_requests(part), stamp)
        if requests:
            world.apply(requests)
    ir = doc_world.settled_ir(world, ir, ir)
    _fetch_pictures(ir)     # `push` knows every picture's file: it embedded them
    return ir


def sync_once(world: doc_world.World, ours: Ir, base: Ir,
              seen: Counter[str]) -> tuple[SyncReport, Ir, Ir]:
    """`doc_sync.sync` against the world: plan every tab, write it, then settle.

    Returns the report and the file and base the settle leaves behind — which are the
    same read, as they are on a real sync: file, document and base agree from here.
    `seen` counts the requests sent (a fresh `Counter()` when nobody is counting).
    """
    theirs = doc_world.read_ir(world, ours, base)
    tabs = doc_merge.pair_tabs(base, ours, theirs)
    if tabs.requests:
        _send(world, tabs.requests, seen)
    if rename := tabs.rename:
        world.title = rename   # Drive's, not a request (`doc_sync.rename_document`)
    pairs = list(tabs.pairs)
    known = {tab for p in doc_ir.parts(theirs) if (tab := p.get("tab")) is not None}
    siblings = doc_merge.tab_siblings(theirs)
    made: dict[str, str] = {}
    for part in tabs.create:
        if (parent := part.get("parent")) is not None and parent in made:
            part["parent"] = made[parent]
        request = doc_merge.add_tab_request(part, known | set(made.values()),
                                            ours, siblings)
        reply = _send(world, [request], seen)
        tab = doc_sync._new_tab_id(reply)
        if mine := part.get("tab"):
            made[mine] = tab
        part["tab"] = tab
        adding = request.get("addDocumentTab")
        props: DocsTabProperties = adding["tabProperties"] if adding is not None else {}
        if (index := props.get("index")) is None:
            raise ValueError("an addDocumentTab request that names no index")
        siblings.setdefault(props.get("parentTabId"), []).insert(index, tab)
        pairs.append((tab, part, {"blocks": []}))

    stager = Stager()
    written: list[Written] = []
    start: list[tuple[str | None, Ir, Ir]] = [(None, ours, base)]
    for tab, mine, was in start + pairs:
        written.append(_sync_part(world, stager, tab, ours, base, mine, was, seen))
    report = _report(ours, tabs, written)
    live = settle(world, ours, base, {each.stamp: each.result.blocks
                                      for each in written})
    return report, live, copy.deepcopy(live)


def _sync_part(world: doc_world.World, stager: Stager, tab: str | None, ours: Ir, base: Ir,
               mine: Ir, was: Ir, seen: Counter[str]) -> Written:
    theirs = doc_world.part_ir(world, tab, ours, base)
    result = doc_merge.plan(was, mine, theirs)
    was, result, shaped = _write_structure(world, tab, ours, base, mine, was, result, seen)
    if result.requests:
        _send(world, stager.resolve(doc_merge.on_tab(result.requests, tab)), seen)
    return Written(stamp=tab, label=mine.get("title", "") if tab else None,
                   result=result, shaped=shaped, attempts=0)


def _write_structure(world: doc_world.World, tab: str | None, ours: Ir, base: Ir, mine: Ir,
                     was: Ir, result: doc_merge.Plan, seen: Counter[str]
                     ) -> tuple[Ir, doc_merge.Plan, list[doc_merge.Told]]:
    """`doc_sync._write_structure`: the grid first, on its own, then read again and
    plan the words against the grid the document has now."""
    shaped: list[doc_merge.Told] = []
    for _ in range(3):
        if not result.structure:
            break
        _send(world, doc_merge.on_tab(result.structure, tab), seen)
        shaped += result.shaped
        theirs = doc_world.part_ir(world, tab, ours, base)
        found = doc_merge.recover_tables(
            was, theirs, {key for t in result.shaped if (key := t.key)})
        anchored = doc_merge.anchor_tables(theirs, result.shaped)
        anchored += doc_merge.recover_swallowed(theirs, result.shaped)
        anchored += doc_merge.recover_eaten(theirs, result.shaped)
        if anchored or found:
            _send(world, doc_merge.on_tab(doc_ir.name_requests(theirs), tab), seen)
            theirs = doc_world.part_ir(world, tab, ours, base)
        was = doc_merge.rebase_tables(was, theirs, result.shaped)
        result = doc_merge.plan(was, mine, theirs)
    return was, result, shaped


def settle(world: doc_world.World, ours: Ir, base: Ir,
           planned: Mapping[str | None, list[Block]]) -> Ir:
    """`doc_sync.settle` without the filesystem: read, anchor what is new, and let that
    read be the new file and the new base."""
    live = doc_world.read_ir(world, ours, base)
    tidy: list[DocsRequest] = []
    doc_merge.settle_keys(live, planned, base)
    for part in doc_ir.parts(live):
        stamp = None if part is live else part.get("tab")
        tidy += doc_merge.on_tab(doc_merge.tidy_requests(part), stamp)
    if tidy:
        world.apply(tidy)
    named = 0
    for part in doc_ir.parts(live):
        stamp = None if part is live else part.get("tab")
        requests = doc_merge.on_tab(doc_ir.name_requests(part), stamp)
        named += len(requests)
        if requests:
            world.apply(requests)
    if named or tidy:
        live = doc_world.read_ir(world, ours, base)
    for part in doc_ir.parts(live):
        stamp = None if part is live else part.get("tab")
        if wanted := planned.get(stamp):
            doc_merge.place_pictures(part, wanted)
    doc_ir.attach_latex(live, world.latex())
    _fetch_pictures(live)
    return live


def _fetch_pictures(live: Ir) -> None:
    """`doc_sync.fetch_pictures` without the download: a picture the document has and
    the file does not is saved beside the file, so the file can carry it from now on.

    Its `contentUri` lasts about half an hour and names nothing of ours, which is why
    a picture is never known by its URL for long — here the name stands in for the
    bytes, and two pictures with the same object id are the same file.
    """
    for part in doc_ir.parts(live):
        for block in part["blocks"]:
            for run in oracle.runs_of(block):
                value = run.get("value")
                if run.get("chip") != "image" or run.get("src") or not value:
                    continue
                run["src"] = f"media/{value}.png"
                run["sha"] = f"sha-{value}"


def _send(world: doc_world.World, requests: Sequence[DocsRequest],
          seen: Counter[str]) -> DocsBatchUpdateResponse:
    for request in requests:
        seen["request/" + next(iter(request), "?")] += 1
    return world.apply(requests)


def _report(ours: Ir, tabs: doc_merge.TabPlan, written: Sequence[Written]) -> SyncReport:
    """`doc_sync._report`, the same shape, so the oracle reads a real one."""
    info: SyncReport = {
        "document": "world", "url": "world", "dry_run": False, "requests": 0,
        "conflicts": [], "notes": list(ours.get("unsupported", [])) + tabs.notes,
        "applied": list(tabs.applied), "kept": [], "comments": [], "removed": []}
    for each in written:
        result, label = each.result, each.label
        info["requests"] += len(result.requests) + len(result.structure)
        info["conflicts"] += [doc_sync._in_tab(c, label) for c in result.conflicts]
        info["notes"] += [doc_sync._said_in(label, n) for n in result.notes]
        info["applied"] += [doc_sync._said_in(label, t.note) for t in each.shaped]
        for block in result.blocks:
            origin, key = block.get("origin"), block.get("key", "(unkeyed)")
            if block.get("moved") or origin in ("added by the source", "merged"):
                info["applied"].append(doc_sync._said_in(label, f"`{key}` {origin}"))
            elif origin:
                info["kept"].append(doc_sync._said_in(label, f"`{key}` {origin}"))
    info["requests"] += len(tabs.requests) + len(tabs.create)
    return info


# ---------------------------------------------------------------- what the reader does

def _blocks(part: Ir) -> list[Block]:
    return part["blocks"]


def _ends(block: Block) -> tuple[int, int]:
    """Where a block starts and ends in the document it was read from."""
    span = block.get("span")
    if not span:
        raise ValueError(f"a block read with no span: {block.get('key')}")
    return span[0], span[1]


def _rows(block: Block) -> list[list[list[Block]]]:
    return block.get("rows", [])


def _word_spots(block: Block) -> list[tuple[str, int, int]]:
    """(word, start index, end index) for every word of a paragraph-like block."""
    out: list[tuple[str, int, int]] = []
    span = block.get("span")
    at: int = span[0] if span is not None else 1
    for run in block.get("runs", []):
        text = run["text"]
        width = run.get("width")
        if not run.get("frozen"):
            for match in WORD.finditer(text):
                low = at + doc_ir.utf16_len(text[:match.start()])
                out.append((match.group(), low, low + doc_ir.utf16_len(match.group())))
        at += width if width is not None else doc_ir.utf16_len(text)
    return out


def _paragraph_blocks(part: Ir) -> list[Block]:
    return [b for b in _blocks(part) if b["kind"] in doc_ir.TEXT_KINDS and b.get("span")]


def _tables(part: Ir) -> list[Block]:
    return [b for b in _blocks(part) if b["kind"] == "table" and b.get("span")]


def _cells(table: Block) -> Iterator[tuple[int, int, Block]]:
    for r, row in enumerate(_rows(table)):
        for c, cell in enumerate(row):
            if cell:
                yield r, c, cell[0]


#: What a reader op answers: the batches it sends and what it touched.
Reading = tuple[Batches, Touched]


def read_type_word(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    spots = [s for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    word, low, _ = rng.choice(spots)
    fresh = rng.choice(FRESH)
    return [[{"insertText": {"location": _at(low, tab), "text": fresh + " "}}]], [fresh]


def read_reword(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (word, low, high) = rng.choice(spots)
    fresh = rng.choice(FRESH)
    # The insert goes in at the end of what it replaces, and the delete after it: text
    # takes the style of the character in front of it (docs/google-docs.md).
    return [[{"insertText": {"location": _at(high, tab), "text": fresh}},
             {"deleteContentRange": {"range": _span(low, high, tab)}}]], [block.get("key")]


def read_delete_word(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)
             if len(_word_spots(b)) > 1]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    return [[{"deleteContentRange": {"range": _span(low, high, tab)}}]], [block.get("key")]


def read_append_block(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    fresh = rng.choice(FRESH)
    return [[{"insertText": {"location": _at(_ends(block)[1] - 1, tab),
                             "text": f"\nthe reader wrote {fresh}"}}]], [fresh]


def read_delete_block(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    blocks = _paragraph_blocks(part)
    if len(blocks) < 2:
        return [], []
    block = rng.choice(blocks)
    low, high = _ends(block)
    return [[{"deleteContentRange": {"range": _span(low, high, tab)}}]], [block.get("key")]


def read_split_block(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader presses Enter in the middle of a paragraph.

    The commonest editing action there is, and one nothing else here draws. A named
    range is half-open, so the newline typed inside it grows it: one `b2s:` range now
    spans two paragraphs, and `doc_ir.apply_keys` gives the key to the first of them
    (where the range begins, and so where the words it was given to still are). The
    second half is a block nobody has ever seen, which the settle must key and name.
    """
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)
             if s[1] > _ends(b)[0]]
    if not spots:
        return [], []
    block, (_, low, _) = rng.choice(spots)
    return [[{"insertText": {"location": _at(low, tab), "text": "\n"}}]], [block.get("key")]


def read_join_blocks(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader backspaces at the start of a paragraph, joining it to the one above.

    The mirror of the split, and the shape Docs' own merge-on-delete rule is about:
    the paragraph mark that goes is the *first* block's, the two texts become one, and
    the survivor keeps the first block's style. Both named ranges live on — the first
    shrinks by the mark, the second is now inside the merged paragraph — so the read
    finds two keys starting in one block and `apply_keys` keeps the first. The second
    key is gone from the document, which is the reader deleting that block.
    """
    blocks = _blocks(part)
    pairs = [(blocks[i], blocks[i + 1]) for i in range(len(blocks) - 1)
             if all(b["kind"] in doc_ir.TEXT_KINDS and b.get("span")
                    for b in blocks[i:i + 2])]
    if not pairs:
        return [], []
    first, second = rng.choice(pairs)
    mark = _ends(first)[1] - 1
    return [[{"deleteContentRange": {"range": _span(mark, mark + 1, tab)}}]], \
        [first.get("key"), second.get("key")]


def read_paste_block(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader copies a paragraph and pastes it somewhere else in the tab.

    What this makes that nothing else does is **two blocks that say exactly the same
    thing**, one of them keyed and named and the other known to nobody. Identity by
    words is the fallback under every part of the merge — `key_blocks` at the settle,
    `inherit_keys` on the file, `_adopt_by_words` after a write mangles a block — and
    each of them is right only while the words pick a block out. A person pasting a
    paragraph is the everyday way to take that away.
    """
    blocks = _paragraph_blocks(part)
    if len(blocks) < 2:
        return [], []
    block = rng.choice(blocks)
    target = rng.choice([b for b in blocks if b is not block])
    text = doc_ir.runs_text(block.get("runs", []))
    if not text.strip():
        return [], []
    return [[{"insertText": {"location": _at(_ends(target)[1] - 1, tab),
                             "text": "\n" + text}}]], [block.get("key")]


def read_bold_word(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    return [[{"updateTextStyle": {"range": _span(low, high, tab),
                                  "textStyle": {"bold": True}, "fields": "bold"}}]], \
        [block.get("key")]


def read_unmark_word(rng: random.Random, part: Ir, tab: str | None, theme: Theme) -> Reading:
    """A reader pressing Ctrl+B on a word a *theme* made bold.

    This is the run-level twin of the alignment loss: a mark turned off is a run
    saying `bold: false`, which reads back as itself, and a file that could only say
    "bold" or nothing would hand the word back to the theme on the first source
    restyle. Headings first, since that is where a theme's marks live; on a shape
    with no theme the request is written all the same and means nothing, which is
    also what the document says about it.
    """
    blocks = [b for b in _paragraph_blocks(part) if b["kind"] == "heading"] \
        or _paragraph_blocks(part)
    spots = [(b, s) for b in blocks for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    # A mark the theme actually puts on this block, when there is one: turning off a
    # mark nothing puts on is a request that means nothing, and a campaign made of
    # those would say it had drawn this and proved nothing by it.
    worn = theme.get(doc_merge.named_style(block), set())
    wears: list[Mark] = [key for key, _ in sorted(doc_ir.MARK_FIELDS) if key in worn]
    key = rng.choice(wears) if wears else rng.choice(doc_ir.MARK_FIELDS)[0]
    api = dict(doc_ir.MARK_FIELDS)[key]
    style: DocsTextStyle = {}
    doc_merge._set_mark_api(style, api, False)
    return [[{"updateTextStyle": {"range": _span(low, high, tab),
                                  "textStyle": style, "fields": api}}]], \
        [block.get("key")]


def read_link_word(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader presses Ctrl+K on a word, or takes the link off one.

    `link` is one of `doc_merge.MANAGED` — the fields the merge names on a restyle
    whether or not the block asks for them, so that a property the source dropped goes
    away — and it earns its place there by the rule the others do: the file can say it
    (`<a href>`) and a read can see it. What nothing could see is what a *source*
    restyle does to a link the **reader** made, because no op on either side had ever
    put one on a run: a whole `MANAGED` field, drawn by nobody, in the one place where
    naming a field with no value means "back to what you inherit".

    It has found nothing (1,920 rounds over six settings: `mixed`, `themed`, `prose`,
    `two_tables` and `imported_list`, at chains 4 to 10, drawing 150 to 240 times in
    each) and is kept for what it says while it keeps finding nothing, on
    `read_paste_block`'s precedent,
    with three tests in `test_doc_fuzz.py` pinning the behaviour it walks over: a
    reader's link survives the source *moving* the block, which is the one place it
    could be lost (a move is a delete and a write from nothing); a source restyle of
    the same block is settled by the both-sides rule before the field is reached, so
    the link stays and the report says so; and a link the source takes off goes, which
    is the only thing membership of `MANAGED` actually buys — `_text_style` writes a
    link the run *has* whatever `MANAGED` says.
    """
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    # Taking one off is the same request with no url in it, which is how the API says
    # "no link" and how a reader's Ctrl+Shift+K reads back.
    style: DocsTextStyle = {} if rng.random() < 0.25 else \
        {"link": {"url": f"https://example.invalid/{rng.randrange(1 << 16):04x}"}}
    return [[{"updateTextStyle": {"range": _span(low, high, tab),
                                  "textStyle": style, "fields": "link"}}]], \
        [block.get("key")]


def read_indent(rng: random.Random, part: Ir, tab: str | None,
                world: doc_world.World) -> Reading:
    """The reader presses Tab or Shift-Tab on a list item.

    The other half of the bullet button, and the half nothing could draw: a nesting
    level is the one property of a paragraph that **no request writes**, so this op is
    no batch (`doc_world.nest`). It is also the one the campaign most needed, because
    `doc_merge.unwritten_levels` is three rules about a level going astray and every
    level in the corpus was put there by the file — the source's `src_bullet` writes
    level 0 and nothing else moved one, so a level the *reader* chose, which is the
    only kind the file cannot ask for again, had never existed in a round.
    """
    items = [b for b in part["blocks"] if b["kind"] == "item"]
    if not items:
        return [], []
    block = rng.choice(items)
    was = block.get("level", 0)
    # Tab twice as often as Shift-Tab: a person indents to make a sub-list and there
    # is nowhere to go from level 0 but down. Docs allows nine levels; two is as deep
    # as any of this says anything.
    level = max(0, min(2, was + rng.choice([-1, 1, 1])))
    low, high = _ends(block)
    return [], ([block.get("key")] if doc_world.nest(world, _span(low, high, tab), level)
                else [])


def read_heading(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    blocks = [b for b in _paragraph_blocks(part) if b["kind"] != "item"]
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    named = rng.choice(["HEADING_3", "TITLE", "SUBTITLE", "NORMAL_TEXT"])
    low, high = _ends(block)
    return [[{"updateParagraphStyle": {
        "range": _span(low, high, tab),
        "paragraphStyle": {"namedStyleType": named},
        "fields": "namedStyleType"}}]], [block.get("key")]


# What the reader picks in the editor's own menus, written the way Docs writes it.
# Spelled out here rather than taken from `doc_merge`, so the harness is not checking
# the merge against its own idea of a Dimension.
READER_FACES: list[tuple[DocsTextStyle, str]] = [
    ({"weightedFontFamily": {"fontFamily": "Georgia", "weight": 400}}, "weightedFontFamily"),
    ({"weightedFontFamily": {"fontFamily": "Courier New", "weight": 400}},
     "weightedFontFamily"),
    ({"fontSize": {"magnitude": 18, "unit": "PT"}}, "fontSize"),
    ({"fontSize": {"magnitude": 8.5, "unit": "PT"}}, "fontSize"),
    ({"smallCaps": True}, "smallCaps"),
    ({"baselineOffset": "SUPERSCRIPT"}, "baselineOffset"),
    ({"baselineOffset": "SUBSCRIPT"}, "baselineOffset"),
    ({"foregroundColor": {"color": {"rgbColor": {"red": 0.1, "green": 0.3, "blue": 0.7}}}},
     "foregroundColor"),
]

READER_MEASURES: list[tuple[DocsParagraphStyle, str]] = [
    ({"indentStart": {"magnitude": 36, "unit": "PT"}}, "indentStart"),
    ({"indentFirstLine": {"magnitude": 18, "unit": "PT"}}, "indentFirstLine"),
    ({"lineSpacing": 200}, "lineSpacing"),
    ({"spaceAbove": {"magnitude": 12, "unit": "PT"}}, "spaceAbove"),
    ({"spaceBelow": {"magnitude": 3, "unit": "PT"}}, "spaceBelow"),
    ({"shading": {"backgroundColor": {"color": {"rgbColor": {
        "red": 1.0, "green": 0.95, "blue": 0.8}}}}}, "shading"),
    ({"borderBottom": {"width": {"magnitude": 1, "unit": "PT"},
                       "padding": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID",
                       "color": {"color": {"rgbColor": {}}}}}, "borderBottom"),
    ({"borderTop": {"width": {"magnitude": 2.25, "unit": "PT"},
                    "padding": {"magnitude": 6, "unit": "PT"}, "dashStyle": "DOT",
                    "color": {"color": {"rgbColor": {"blue": 0.6}}}}}, "borderTop"),
    ({"pageBreakBefore": True}, "pageBreakBefore"),
    ({"keepWithNext": True}, "keepWithNext"),
    ({"alignment": "CENTER"}, "alignment"),
]


def read_face(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader chooses a face, a size or a colour for a word — the case that
    matters most, because `doc_merge.MANAGED` lets a source restyle clear it."""
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    style, field = rng.choice(READER_FACES)
    return [[{"updateTextStyle": {"range": _span(low, high, tab),
                                  "textStyle": style, "fields": field}}]], [block.get("key")]


def read_measure(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader indents a paragraph, spaces it out or shades it."""
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    style, field = rng.choice(READER_MEASURES)
    low, high = _ends(block)
    return [[{"updateParagraphStyle": {"range": _span(low, high, tab),
                                       "paragraphStyle": style, "fields": field}}]], \
        [block.get("key")]


def read_renumber_list(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    items = [b for b in _blocks(part) if b["kind"] == "item" and b.get("span")]
    if not items:
        return [], []
    low, high = _ends(items[0])[0], _ends(items[-1])[1]
    return [[{"createParagraphBullets": {
        "range": _span(low, high, tab),
        "bulletPreset": doc_world.ORDERED_PRESET}}]], [b.get("key") for b in items]


def read_bullet(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader clicks the bullet button: a paragraph becomes a list item, or stops
    being one.

    After typing, this is about the commonest thing anybody does to a paragraph in a
    browser, and nothing here drew it. `read_heading` moves a block between the *named*
    styles and leaves the items alone; `read_renumber_list` only changes the glyph of a
    list that is one already. A bullet is neither a named style nor a measurement: it is
    `kind` in the IR, one of `doc_merge.SHAPE_KEYS`, carried by two requests of its own
    and repaired in three places (`carry_unimported`, `restore_bullets`,
    `bullet_requests`) — every one of which the campaign had reached from the *source's*
    side alone, where the file says `<li>` from the beginning.
    """
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    low, high = _ends(block)
    span = _span(low, high, tab)
    if block["kind"] == "item":
        return [[{"deleteParagraphBullets": {"range": span}}]], [block.get("key")]
    preset = doc_world.ORDERED_PRESET if rng.random() < 0.4 \
        else "BULLET_DISC_CIRCLE_SQUARE"
    return [[{"createParagraphBullets": {"range": span, "bulletPreset": preset}}]], \
        [block.get("key")]


# What a reader may set on a paragraph *inside a table cell*. `pageBreakBefore` is
# left out: Docs refuses it in a table, and a request the real API would reject is
# the harness's own doing, never the sync's.
CELL_MEASURES: list[tuple[DocsParagraphStyle, str]] = \
    [m for m in READER_MEASURES if m[1] != "pageBreakBefore"]
#: And what a reader may set on a word there: the faces, and bold.
CELL_FACES: list[tuple[DocsTextStyle, str]] = READER_FACES + [({"bold": True}, "bold")]


def read_cell_style(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader styling a word, or setting a paragraph, *inside a table cell*.

    Every reader op that styles anything walks `part["blocks"]`, and a cell is not
    one of those — it hangs off a table's `rows` — so the whole of `_merge_cell`'s
    styling was a path the campaign had never driven: `_merge_block` with a cell
    for a block, `_merged_shape` on it, the marks merge inside it. The tables were
    exercised for their words and their grid alone, on both sides
    (`src_edit_cell` only ever rewrites a cell's words).

    The table's key is what it reports touched, a cell having none of its own, so
    `collide` can answer in the same table.
    """
    spots = [(t, b) for t in _tables(part) for _, _, b in _cells(t) if b.get("span")]
    if not spots:
        return [], []
    table, block = rng.choice(spots)
    if rng.random() < 0.35:
        measure, field = rng.choice(CELL_MEASURES)
        low, high = _ends(block)
        return [[{"updateParagraphStyle": {"range": _span(low, high, tab),
                                           "paragraphStyle": measure,
                                           "fields": field}}]], [table.get("key")]
    words = _word_spots(block)
    if not words:
        return [], []
    _, low, high = rng.choice(words)
    style, field = rng.choice(CELL_FACES)
    return [[{"updateTextStyle": {"range": _span(low, high, tab),
                                  "textStyle": style, "fields": field}}]], [table.get("key")]


def read_cell_chip(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader putting a person chip or a picture *into a table cell*.

    A table of owners is the commonest place in a real document for a chip to be, and
    it was the one place the campaign could not put one: `read_insert_chip` and
    `read_insert_picture` pick from `part["blocks"]` like every other reader op. What
    it drives is a frozen run inside a cell — `_merge_cell`'s side of `_rewritten_runs`,
    `_table_movable`'s refusal to rebuild a table holding one, and the oracle's chip
    and picture accounting where the block that holds them has no key of its own.
    """
    spots = [(t, b) for t in _tables(part) for _, _, b in _cells(t) if b.get("span")]
    if not spots:
        return [], []
    table, block = rng.choice(spots)
    at = _at(_ends(block)[1] - 1, tab)
    if rng.random() < 0.5:
        return [[{"insertPerson": {
            "location": at,
            "personProperties": {"email": "reader@example.com"}}}]], [table.get("key")]
    return [[{"insertInlineImage": {
        "location": at, "uri": "https://example.invalid/reader.png"}}]], [table.get("key")]


def read_split_cell(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader pressing Enter inside a table cell.

    `read_split_block` is the commonest edit there is and it walks `part["blocks"]`,
    where a cell never is — so a cell of two paragraphs was a thing the campaign had
    no way of making from either side, and `_merge_cell`'s `_joined` (a cell whose
    paragraph counts differ merges as one text with the breaks in it) had never once
    been asked for. The table's key is what it reports touched, a cell having none of
    its own, as the other cell ops do.
    """
    spots = [(t, b) for t in _tables(part) for _, _, b in _cells(t)
             if b.get("span") and _ends(b)[1] - _ends(b)[0] > 2]
    if not spots:
        return [], []
    table, block = rng.choice(spots)
    low, high = _ends(block)
    return [[{"insertText": {"location": _at(rng.randrange(low + 1, high - 1), tab),
                             "text": "\n"}}]], [table.get("key")]


def read_cell_type(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    spots = [(t, r, c, b) for t in _tables(part) for r, c, b in _cells(t) if b.get("span")]
    if not spots:
        return [], []
    table, _, _, block = rng.choice(spots)
    fresh = rng.choice(FRESH)
    return [[{"insertText": {"location": _at(_ends(block)[0], tab),
                             "text": fresh + " "}}]], [table.get("key"), fresh]


def _cell_at(table: Block, tab: str | None, row: int, column: int) -> DocsTableCellLocation:
    return {"tableStartLocation": _at(_ends(table)[0], tab),
            "rowIndex": row, "columnIndex": column}


def read_add_row(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    tables = _tables(part)
    if not tables:
        return [], []
    table = rng.choice(tables)
    return [[{"insertTableRow": {"tableCellLocation": _cell_at(table, tab, 0, 0),
                                 "insertBelow": True}}]], [table.get("key")]


def read_delete_row(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    tables = [t for t in _tables(part) if len(_rows(t)) > 1]
    if not tables:
        return [], []
    table = rng.choice(tables)
    row = rng.randrange(len(_rows(table)))
    return [[{"deleteTableRow": {"tableCellLocation": _cell_at(table, tab, row, 0)}}]], \
        [table.get("key")]


def read_add_column(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader adds a column. Rows and columns are not the same thing to the merge:
    a row is one line of the grid, a column is a cell taken out of *every* row, matched
    by its words across the whole table (`doc_merge._column_score`) rather than by its
    place. That half of `_table_lines` had never been drawn — the campaign planned
    `insertTableRow` and `deleteTableRow` and no column request at all."""
    tables = _tables(part)
    if not tables:
        return [], []
    table = rng.choice(tables)
    return [[{"insertTableColumn": {"tableCellLocation": _cell_at(table, tab, 0, 0),
                                    "insertRight": True}}]], [table.get("key")]


def read_delete_column(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    tables = [t for t in _tables(part) if _rows(t) and len(_rows(t)[0]) > 1]
    if not tables:
        return [], []
    table = rng.choice(tables)
    column = rng.randrange(len(_rows(table)[0]))
    return [[{"deleteTableColumn": {
        "tableCellLocation": _cell_at(table, tab, 0, column)}}]], [table.get("key")]


def read_insert_picture(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    return [[{"insertInlineImage": {"location": _at(_ends(block)[1] - 1, tab),
                                    "uri": "https://example.invalid/reader.png"}}]], \
        [block.get("key")]


def read_insert_chip(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    return [[{"insertPerson": {"location": _at(_ends(block)[1] - 1, tab),
                               "personProperties": {"email": "reader@example.com"}}}]], \
        [block.get("key")]


def read_move_block(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """A drag: the reader cuts a block and drops it somewhere else. Its named range
    dies with the cut, which is what a drag really does.

    Two batches, and the second one's index is read off the document the **first**
    leaves behind: a drop after the cut has everything below the cut shifted up by
    what went. Taking it from the part as read, the drop landed that far past where
    the reader let go — in the middle of a word, of a chip, or between the two code
    units of an astral character, which no cursor can be put inside and which left
    the document holding a lone surrogate that `doc_ir.utf16_len` cannot encode at
    all (the campaign died on it at 710370). Everywhere it did not crash it quietly
    handed the judges a document no reader could have made, which is the worse half:
    the loss oracle's two punctuation forgivenesses — `_dressed_up` (seed 500249, a
    stop landing against the `1` in a cell) and `_undressed` (76101, a drag carrying
    a stop away) — were both written for damage this line was doing. The drop is a
    paragraph mark now, so nothing it inserts can land inside a token at all.
    """
    blocks = _paragraph_blocks(part)
    if len(blocks) < 3:
        return [], []
    block = rng.choice(blocks[1:])
    target = rng.choice([b for b in blocks if b is not block])
    text = doc_ir.runs_text(block.get("runs", []))
    if not text.strip():
        return [], []
    low, high = _ends(block)
    at = _ends(target)[1] - 1
    if at >= high:                     # the cut is in front of where it is dropped
        at -= high - low
    return [[{"deleteContentRange": {"range": _span(low, high, tab)}}],
            [{"insertText": {"location": _at(at, tab), "text": "\n" + text}}]], \
        [block.get("key")]


def read_rename_tab(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader renames the tab they are looking at, in the tab strip. The first
    tab's id is not `tab` (a location in it carries none) but the part's own."""
    ident = tab or part.get("tab")
    if not ident:
        return [], []
    return [[{"updateDocumentTabProperties": {
        "tabProperties": {"tabId": ident, "title": f"Reader's {rng.choice(FRESH)}"},
        "fields": "title"}}]], []


def read_add_tab(rng: random.Random, part: Ir, tab: str | None) -> Reading:
    """The reader clicks + in the tab strip. Docs makes it with one empty paragraph
    and hands back its id; nothing here types into it, since a later step's ops draw
    a tab at random and will reach this one.

    Nobody knows of such a tab: it is in neither the file nor the base, so the merge
    must leave it alone and the settle must read it into the file — keys, named
    ranges and all — or the sync after it will see a tab the file never had."""
    return [[{"addDocumentTab": {"tabProperties": {
        "title": f"Reader's {rng.choice(FRESH)}"}}}]], []


def read_drop_tab(rng: random.Random, part: Ir, tab: str | None,
                  tabs: list[str]) -> Reading:
    """The reader deletes a tab in the tab strip. Never the first: that one is the
    body and Docs refuses it (so does the world). What has to follow the tab is its
    base entry and its `<section>` in the file."""
    if not tabs:
        return [], []
    return [[{"deleteTab": {"tabId": rng.choice(tabs)}}]], []


# Most reader ops are a person typing in the tab they look at, and know nothing more
# than that tab. Three know one thing more, and say so by their kind:
@dataclass(frozen=True, kw_only=True)
class Plain:
    op: Callable[[random.Random, Ir, str | None], Reading]


@dataclass(frozen=True, kw_only=True)
class NeedsTheme:
    """An op that turns styling *off* has to know what the theme turns on."""
    op: Callable[[random.Random, Ir, str | None, Theme], Reading]


@dataclass(frozen=True, kw_only=True)
class NeedsTabs:
    """An op about the tab strip rather than a tab: which tabs there are to delete is
    not a thing the part it is looking at can say."""
    op: Callable[[random.Random, Ir, str | None, list[str]], Reading]


@dataclass(frozen=True, kw_only=True)
class NeedsWorld:
    """An op that is no request at all: a nesting level cannot be written by any field
    of the v1 API, so `read_indent` reaches the world itself (`doc_world.nest` says
    why). A draw of it that moved nothing counts as nothing to do, exactly as an empty
    batch does."""
    op: Callable[[random.Random, Ir, str | None, doc_world.World], Reading]


ReaderOp = Plain | NeedsTheme | NeedsTabs | NeedsWorld

READER: dict[str, ReaderOp] = {
    "type_word": Plain(op=read_type_word), "reword": Plain(op=read_reword),
    "delete_word": Plain(op=read_delete_word),
    "append_block": Plain(op=read_append_block), "delete_block": Plain(op=read_delete_block),
    "split_block": Plain(op=read_split_block), "join_blocks": Plain(op=read_join_blocks),
    "paste_block": Plain(op=read_paste_block),
    "bold_word": Plain(op=read_bold_word), "unmark_word": NeedsTheme(op=read_unmark_word),
    "link_word": Plain(op=read_link_word), "indent": NeedsWorld(op=read_indent),
    "heading": Plain(op=read_heading),
    "face": Plain(op=read_face), "measure": Plain(op=read_measure),
    "renumber_list": Plain(op=read_renumber_list), "bullet": Plain(op=read_bullet),
    "cell_type": Plain(op=read_cell_type),
    "cell_style": Plain(op=read_cell_style), "cell_chip": Plain(op=read_cell_chip),
    "split_cell": Plain(op=read_split_cell),
    "add_row": Plain(op=read_add_row), "delete_row": Plain(op=read_delete_row),
    "add_column": Plain(op=read_add_column), "delete_column": Plain(op=read_delete_column),
    "insert_picture": Plain(op=read_insert_picture),
    "insert_chip": Plain(op=read_insert_chip),
    "move_block": Plain(op=read_move_block), "rename_tab": Plain(op=read_rename_tab),
    "add_tab": Plain(op=read_add_tab), "drop_tab": NeedsTabs(op=read_drop_tab),
}


def _at(index: int, tab: str | None) -> DocsLocation:
    location: DocsLocation = {"index": index}
    if tab:
        location["tabId"] = tab
    return location


def _span(low: int, high: int, tab: str | None) -> DocsRangeWrite:
    span: DocsRangeWrite = {"startIndex": low, "endIndex": high}
    if tab:
        span["tabId"] = tab
    return span


def apply_reader(world: doc_world.World, name: str, rng: random.Random,
                 seen: Counter[str]) -> list[str]:
    """One reader edit, on a tab drawn at random. A batch the world refuses is the
    harness's own doing — a person in a browser never sends one — so it is dropped and
    counted, never blamed on the sync."""
    tabs: list[str | None] = [None] + [t.id for t in world.tabs[1:]]
    tab = rng.choice(tabs)
    part = doc_ir.from_document(world.read(), tab)
    doc_ir.apply_keys(part, doc_ir.named_ranges_of(world.read(), part.get("tab")))
    op = READER[name]
    if isinstance(op, Plain):
        batches, touched = op.op(rng, part, tab)
    elif isinstance(op, NeedsTheme):
        batches, touched = op.op(rng, part, tab, theme_fields(world))
    elif isinstance(op, NeedsTabs):
        batches, touched = op.op(rng, part, tab, [t.id for t in world.tabs[1:]])
    elif isinstance(op, NeedsWorld):
        batches, touched = op.op(rng, part, tab, world)
    else:
        assert_never(op)
    if not batches and not (isinstance(op, NeedsWorld) and touched):
        seen["reader/" + name + " (nothing to do)"] += 1
        return []
    for batch in batches:
        try:
            world.apply(batch)
        except Refused:
            seen["reader/" + name + " (refused)"] += 1
            return []
    seen["reader/" + name] += 1
    return [t for t in touched if t]


# ---------------------------------------------------------------- what the source does

def _parts(ir: Ir) -> list[Ir]:
    return doc_ir.parts(ir)


def _runs(block: Block) -> list[Run]:
    """A block's runs, as the list it holds (made when it holds none)."""
    runs = block.get("runs")
    if runs is None:
        runs = []
        block["runs"] = runs
    return runs


def _pick(rng: random.Random, ir: Ir, kinds: Sequence[Kind]) -> tuple[Ir, int, Block] | None:
    spots = [(part, i, b) for part in _parts(ir)
             for i, b in enumerate(part["blocks"]) if b["kind"] in kinds]
    return rng.choice(spots) if spots else None


def src_reword(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot:
        return
    _, _, block = spot
    _swap_word(rng, block)


def _swap_word(rng: random.Random, block: Block) -> bool:
    runs = [r for r in block.get("runs", [])
            if not r.get("frozen") and WORD.search(r["text"])]
    if not runs:
        return False
    run = rng.choice(runs)
    spots = list(WORD.finditer(run["text"]))
    match = rng.choice(spots)
    run["text"] = run["text"][:match.start()] + rng.choice(FRESH) + run["text"][match.end():]
    return True


def src_append(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot:
        return
    part, i, _ = spot
    part["blocks"].insert(i + 1, _p(f"the source added {rng.choice(FRESH)}"))


def src_drop(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot or len(spot[0]["blocks"]) < 2:
        return
    part, i, _ = spot
    part["blocks"].pop(i)


def src_move(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot or len(spot[0]["blocks"]) < 3:
        return
    part, i, block = spot
    part["blocks"].pop(i)
    part["blocks"].insert(rng.randrange(len(part["blocks"]) + 1), block)


def _mark_run(rng: random.Random, block: Block) -> bool:
    """Put one run mark of the dialect on one run of a block."""
    runs = [r for r in block.get("runs", []) if not r.get("frozen") and r["text"].strip()]
    if not runs:
        return False
    run = rng.choice(runs)
    mark = rng.choice(RUN_MARKS)
    doc_ir.apply_style(run, mark)
    if "font" in mark:
        run.pop("code", None)
    elif "code" in mark:
        run.pop("font", None)
    return True


def _mark_paragraph(rng: random.Random, block: Block) -> bool:
    """Put one paragraph measure of the dialect on a block. These are `SHAPE_KEYS`,
    so they travel by `_take_shape`, not by the run restyler: a different code path
    from `_mark_run` and worth drawing on its own."""
    if block["kind"] == "table":
        return False
    doc_ir.apply_measures(block, rng.choice(PARA_MARKS))
    return True


def src_restyle(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot:
        return
    _, _, block = spot
    if rng.random() < 0.35:
        _mark_paragraph(rng, block)
    else:
        _mark_run(rng, block)


#: The named styles `src_retitle` moves a block between.
RETITLE_KINDS: tuple[Kind, ...] = ("paragraph", "heading", "title", "subtitle")


def src_retitle(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """Move a block between the named styles. Every one of Docs' styles is drawn:
    `namedStyleType` is a field the merge names on every paragraph it writes, so a
    style the dialect cannot spell is one it silently writes body text over."""
    spot = _pick(rng, ir, RETITLE_KINDS)
    if not spot:
        return
    _, _, block = spot
    kinds = [k for k in RETITLE_KINDS if k != block["kind"]]
    block["kind"] = rng.choice(kinds)
    if block["kind"] == "heading":
        block["level"] = rng.randint(1, 3)
    else:
        block.pop("level", None)


def src_bullet(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """The source writes a paragraph as a list item, or a list item as a paragraph.

    `src_retitle` moves a block between the named styles and `item` is not one of
    those, so the file's own `<li>` was something only a corpus shape ever had: no
    round had ever asked the merge to *give* a block a bullet, or take one away, from
    the side that can say it. Ordered-ness comes with it, that being the other half of
    what a list item is and the one the file alone can carry (an imported list reads
    back with no glyph at all).
    """
    spot = _pick(rng, ir, ("paragraph", "item"))
    if not spot:
        return
    _, _, block = spot
    if block["kind"] == "item" and rng.random() < 0.5:
        block["kind"] = "paragraph"
        block.pop("ordered", None)
        block.pop("level", None)
        return
    block["kind"] = "item"
    # And at a level, not always at 0. `<li>` inside `<li>` is a thing the file says
    # as easily as `<li>` beside one, and it was the other half of what `read_indent`
    # found undrawn: with both sides fixed at 0 the only levels in a round were the
    # ones the corpus shape was born with, so `unwritten_levels`' first rule — a block
    # the source *turns into* a nested item starts a list of its own at level 0 — had
    # nothing to fire on.
    block["level"] = rng.choice([0, 0, 1, 2])
    block["ordered"] = rng.random() < 0.4


def src_add_table(rng: random.Random, ir: Ir, touched: Touched) -> None:
    part = rng.choice(_parts(ir))
    at = rng.randrange(len(part["blocks"]) + 1)
    part["blocks"].insert(at, _t([["h1", "h2"], [rng.choice(FRESH), "x"]]))


def _source_cells(ir: Ir) -> list[Block]:
    """Every paragraph inside every cell of every table the file has."""
    return [inner for cell in _source_cell_lists(ir) for inner in cell]


def _source_cell_lists(ir: Ir) -> list[list[Block]]:
    """Every cell, as the list of paragraphs it is."""
    return [cell for part in _parts(ir) for b in part["blocks"]
            if b["kind"] == "table" for row in _rows(b) for cell in row]


def src_regrid(rng: random.Random, ir: Ir, touched: Touched) -> None:
    tables = [b for part in _parts(ir) for b in part["blocks"] if b["kind"] == "table"]
    if not tables:
        return
    rows = _rows(rng.choice(tables))
    # Columns as well as rows, and the merge treats the two quite differently: a
    # column is matched across the table by its words, a row by its cells in the
    # columns that matched. Drawing rows alone left `_column_score` and the whole
    # column half of `_table_lines` unreached.
    if rng.random() < 0.5:
        if rng.random() < 0.5 or len(rows) < 2:
            rows.append([[_p(rng.choice(FRESH))] for _ in rows[0]])
        else:
            rows.pop(rng.randrange(len(rows)))
    elif rng.random() < 0.5 or len(rows[0]) < 2:
        for row in rows:
            row.append([_p(rng.choice(FRESH))])
    else:
        column = rng.randrange(len(rows[0]))
        for row in rows:
            row.pop(column)


def src_edit_cell(rng: random.Random, ir: Ir, touched: Touched) -> None:
    cells = _source_cells(ir)
    if not cells:
        return
    cell = rng.choice(cells)
    # Distinctive, not a word out of `FRESH`: a cell edit nobody can tell from the
    # cell beside it is one no judge can follow to where it landed, and following it
    # is the whole of `_arrived`. Real sources write words of their own too.
    cell["runs"] = [{"text": f"{rng.choice(FRESH)}-{rng.randrange(1 << 20):05x}"}]


def src_restyle_cell(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """The source's half of what `read_cell_style` draws: a cell restyled rather
    than reworded. `src_restyle` picks from `part["blocks"]`, where a cell never
    is, so the file had no way of asking for one."""
    cells = _source_cells(ir)
    if not cells:
        return
    cell = rng.choice(cells)
    if rng.random() < 0.35:
        _mark_paragraph(rng, cell)
    else:
        _mark_run(rng, cell)


def src_split_cell(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """The source giving a cell a second paragraph.

    Every cell of every corpus shape holds exactly one paragraph and no op had ever
    made a second, so `_merge_cell`'s answer to a cell whose paragraph counts differ
    — merge it as one text with the breaks in it (`_joined`) — was as undrawn as the
    column requests and the cell styling were before it. `_cells` reads `cell[0]` for
    the same reason: there has never been a `cell[1]`.
    """
    cells = [c for c in _source_cell_lists(ir) if len(c) == 1 and c[0].get("runs")]
    if not cells:
        return
    cell = rng.choice(cells)
    words = "".join(r["text"] for r in cell[0].get("runs", []) if not r.get("frozen"))
    parts = words.split(" ", 1)
    if len(parts) == 2 and parts[1].strip():
        cell[0]["runs"] = [{"text": parts[0]}]
        cell.append(_p(parts[1]))
    else:
        cell.append(_p(rng.choice(FRESH)))


def _grace() -> Run:
    return {"chip": "person", "frozen": True, "text": "Grace", "value": "grace@example.com"}


def _picture(name: str) -> Run:
    return {"chip": "image", "frozen": True, "text": "",
            "src": f"media/{name}.png", "sha": f"sha-{name}"}


def src_cell_chip(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """The source asking for a chip or a picture *in a table cell*.

    No request edits an embedded object, so a block whose frozen runs the source
    changed is written again from nothing (`rewrite`) — and a cell is a block with no
    key, inside a structural element the planner treats as one thing. Whether that
    path exists inside a table at all is the question; the campaign could not ask it,
    `src_add_chip` and `src_add_picture` both picking from `part["blocks"]`.
    """
    cells = _source_cells(ir)
    if not cells:
        return
    cell = rng.choice(cells)
    name = rng.choice(FRESH)
    _runs(cell).append(_grace() if rng.random() < 0.5 else _picture(name))


def src_add_picture(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot:
        return
    part, i, _ = spot
    name = rng.choice(FRESH)
    part["blocks"].insert(i + 1, {"kind": "paragraph", "runs": [_picture(name)]})


def src_add_chip(rng: random.Random, ir: Ir, touched: Touched) -> None:
    spot = _pick(rng, ir, doc_ir.TEXT_KINDS)
    if not spot:
        return
    _, _, block = spot
    _runs(block).append(_grace())


def src_add_tab(rng: random.Random, ir: Ir, touched: Touched) -> None:
    tabs = ir.get("tabs")
    if tabs is None:
        tabs = []
        ir["tabs"] = tabs
    tabs.append({"title": f"Tab {rng.choice(FRESH)}",
                 "blocks": [_p(f"a tab the source asked for, {rng.choice(FRESH)}")]})


def src_rename_tab(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """The first tab is drawn too: it names itself in the file's `b2s-tab` meta and
    nowhere else, and it is the tab everybody is actually looking at."""
    extra = ir.get("tabs") or []
    if not extra or rng.random() < 0.4:
        ir["tab_title"] = f"Renamed {rng.choice(FRESH)}"
        return
    rng.choice(extra)["title"] = f"Renamed {rng.choice(FRESH)}"


def src_drop_tab(rng: random.Random, ir: Ir, touched: Touched) -> None:
    extra = ir.get("tabs") or []
    if not extra:
        return
    extra.pop(rng.randrange(len(extra)))


def src_collide(rng: random.Random, ir: Ir, touched: Touched) -> None:
    """Change exactly what the reader just changed.

    Both sides on one block is what every merge rule is about, and two independent
    random draws almost never land there (CLAUDE.md: 5 text overrides in 200 Slides
    rounds before this op existed, 56 after).
    """
    keys = [k for k in touched if k]
    spots = [(part, i, b) for part in _parts(ir)
             for i, b in enumerate(part["blocks"]) if b.get("key") in keys]
    if not spots:
        return src_reword(rng, ir, touched)
    part, i, block = rng.choice(spots)
    how = rng.choice(["reword", "append", "drop", "cell", "restyle", "move"])
    if how == "drop" and len(part["blocks"]) > 1:
        part["blocks"].pop(i)
    elif how == "move" and len(part["blocks"]) > 2:
        # A move is a delete and a write, so the block the reader just worked on is
        # written again from nothing: every managed field on every run, which is
        # where the marks the reader took *off* are lost if the file cannot say them
        # (`doc_ir.MARK_FIELDS`). Nothing else in the campaign moves a block both
        # sides are on.
        part["blocks"].pop(i)
        part["blocks"].insert(rng.randrange(len(part["blocks"]) + 1), block)
    elif how == "append" and block["kind"] != "table":
        _runs(block).append({"text": f" and {rng.choice(FRESH)}"})
    elif how == "cell" and block["kind"] == "table":
        cells = [inner for row in _rows(block) for cell in row for inner in cell]
        if cells:
            rng.choice(cells)["runs"] = [{"text": rng.choice(FRESH)}]
    elif how == "restyle" and block["kind"] != "table":
        if rng.random() < 0.35:
            _mark_paragraph(rng, block)
        else:
            _mark_run(rng, block)
    elif block["kind"] != "table":
        _swap_word(rng, block)
    elif block["kind"] == "table":
        cells = [inner for row in _rows(block) for cell in row for inner in cell]
        if cells:
            _swap_word(rng, rng.choice(cells))


SourceOp = Callable[[random.Random, Ir, Touched], None]

SOURCE: dict[str, SourceOp] = {
    "reword": src_reword, "append": src_append, "drop": src_drop, "move": src_move,
    "restyle": src_restyle, "retitle": src_retitle, "bullet": src_bullet,
    "add_table": src_add_table,
    "regrid": src_regrid, "edit_cell": src_edit_cell, "restyle_cell": src_restyle_cell,
    "cell_chip": src_cell_chip, "split_cell": src_split_cell,
    "add_picture": src_add_picture,
    "add_chip": src_add_chip, "add_tab": src_add_tab, "rename_tab": src_rename_tab,
    "drop_tab": src_drop_tab, "collide": src_collide,
}


# ---------------------------------------------------------------- a round

#: One op of a script: its name and its salt.
Op = tuple[str, int]


@dataclass(frozen=True, kw_only=True)
class Step:
    """What one step of a round runs: the reader's ops, then the source's."""
    reader: tuple[Op, ...]
    source: tuple[Op, ...]


@dataclass(frozen=True, kw_only=True)
class Script:
    """The script of a round: which shape, and each step's ops."""
    shape: str
    steps: tuple[Step, ...]


def draw(seed: int, chain: int, shape: str | None) -> Script:
    """The script of a round: which shape, and which ops each step runs on each side.

    Every op carries its own salt, so the shrinker can drop one and leave the rest
    drawing exactly what they drew. `shape` None draws one.
    """
    rng = random.Random(seed)
    drawn = shape or rng.choice(SHAPES)
    names = sorted(READER)
    source_names = sorted(n for n in SOURCE if n != "collide")
    steps: list[Step] = []
    for _ in range(chain):
        reader = [(rng.choice(names), rng.randrange(1 << 30))
                  for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
        source = [(rng.choice(source_names), rng.randrange(1 << 30))
                  for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
        if rng.random() < COLLIDE_CHANCE:
            source.append(("collide", rng.randrange(1 << 30)))
        steps.append(Step(reader=tuple(reader), source=tuple(source)))
    return Script(shape=drawn, steps=tuple(steps))


#: A named style's `paragraphStyle` keys, by the IR field each one is.
THEME_FIELDS: dict[str, str] = \
    {"alignment": "align"} | {api: key for key, api in doc_merge.PARAGRAPH_FIELDS}


def theme_fields(world: doc_world.World) -> dict[str, set[str]]:
    """What the world's theme sets, as `doc_loss_oracle.check` wants it: the IR fields
    per named style. Nothing in a read says a paragraph *inherits* — only that it sets
    nothing itself — so the oracle has to be told what there was to inherit.

    A named style's run marks are in here too, for `read_unmark_word`; the oracle asks
    its question of a block's own fields, where a run mark is never one, so naming
    them costs it nothing.
    """
    out: dict[str, set[str]] = {}
    for name, style in world.theme.items():
        paragraph = style.get("paragraphStyle") or {}
        text = style.get("textStyle") or {}
        fields = {THEME_FIELDS[api] for api in paragraph if api in THEME_FIELDS}
        out[name] = fields | {key for key, api in doc_ir.MARK_FIELDS
                              if doc_ir._mark_api(text, api)}
    return out


def theme_values(world: doc_world.World) -> dict[str, NamedDefault]:
    """What the world's theme *says*, by named style, as the reader subtracts it.

    `theme_fields` names the fields for the oracle, which only needs to know that
    there was something to inherit. The campaign's styling judge needs the values:
    a source that centres a heading its theme already centres has asked for nothing,
    and the read-back reports no alignment at all, so comparing names alone reads a
    write that arrived as one that vanished (seed 96300). `doc_ir._named_defaults`
    is the reader's own subtraction, so taking it from there is the only way the
    judge normalises the file's side exactly as a read normalises the document's.
    """
    return doc_ir._named_defaults(world.read(), None)


def run_script(script: Script, seen: Counter[str]) -> list[Finding]:
    """One round: push, then (reader edits, source edits, sync, judge) per step.
    `seen` counts what the round reached (a fresh `Counter()` when nobody asks)."""
    seen["shape/" + script.shape] += 1
    world = corpus(script.shape)
    ours = bootstrap(world)
    base = copy.deepcopy(ours)
    found: list[Finding] = []

    for step, edits in enumerate(script.steps):
        touched: Touched = []
        for name, salt in edits.reader:
            touched += apply_reader(world, name, random.Random(salt), seen)
        for name, salt in edits.source:
            seen["source/" + name] += 1
            SOURCE[name](random.Random(salt), ours, touched)
        was, mine = copy.deepcopy(base), copy.deepcopy(ours)
        before = doc_world.settled_ir(world, ours, base)
        try:
            report, ours, base = sync_once(world, ours, base, seen)
        except Refused as refused:
            found.append(oracle.finding(
                "batch_refused", "loss",
                f"step {step}: Google would have thrown out the whole batch — "
                f"{refused.why} ({json.dumps(refused.request)[:160]})", None, None))
            return found
        except Exception as err:                       # noqa: BLE001 (the campaign's job)
            found.append(oracle.finding("crash", "loss",
                                        f"step {step}: {type(err).__name__}: {err}",
                                        None, None))
            return found
        for conflict in report["conflicts"]:
            seen["conflict/" + str(conflict.get("field", "text"))] += 1
        found += [dataclasses.replace(f, detail=f"step {step}: {f.detail}")
                  for f in oracle.check(was, before, base, report, mine, (),
                                        theme_fields(world))]
        found += _arrived(was, before, mine, ours, report, step, seen,
                          theme_values(world))
        found += _settled(world, ours, base, step, seen)
        if found:
            return found
    return found


def _settled(world: doc_world.World, ours: Ir, base: Ir, step: int,
             seen: Counter[str]) -> list[Finding]:
    """A sync that has run must leave nothing to do: file, document and base agree, so
    the next one writes 0 requests (docs/google-docs.md)."""
    spare = world.copied()
    try:
        report, _, _ = sync_once(spare, copy.deepcopy(ours), copy.deepcopy(base), Counter())
    except Refused as refused:
        return [oracle.finding("unsettled", "report",
                               f"step {step}: the second sync is refused: {refused.why}",
                               None, None)]
    if not report["requests"]:
        seen["settled"] += 1
        return []
    return [oracle.finding(
        "unsettled", "report",
        f"step {step}: a second sync still writes {report['requests']} request(s): "
        f"{'; '.join(report['applied'][:3])}", None, None)]


def _grid(block: Block | None) -> tuple[int, int] | None:
    """A table's shape, or None when it is not a rectangular table — a ragged one
    (merged cells) is a thing the merge reports rather than writes."""
    if not block or block.get("kind") != "table" or not (rows := block.get("rows")):
        return None
    widths = {len(row) for row in rows}
    return (len(rows), widths.pop()) if len(widths) == 1 else None


def _arrived(was: Ir, before: Ir, mine: Ir, after: Ir, report: SyncReport,
             step: int, seen: Counter[str],
             theme: Mapping[str, NamedDefault]) -> list[Finding]:
    """Did the source's regrid arrive? The third judge, and the campaign's own.

    The loss oracle says in its first paragraph that it does not ask this — it asks
    about the *reader's* work — and it is right not to: a column matched to the wrong
    column never deletes anything of the reader's, because a line the base and the
    document disagree about is never `gone` (`doc_merge._merged_lines`), so a wrong
    matching errs towards keeping. Convergence cannot see it either, and for a sharper
    reason: `rebase_tables` writes the matching it used into the base, so the second
    sync makes the same reading of the same table and writes nothing. A merge can be
    wrong and stable at once.

    Measured: with `_table_lines` pairing columns by place instead of by their words,
    200 rounds at chain 6 on `two_tables` came back clean. That is what a judge with
    no opinion about the source looks like from the outside, and it is why the column
    half of the table merge — `_column_score`, `_align`, the column side of
    `_merged_lines` — had no measurement behind it at all until this.

    The question is deliberately the narrowest one that catches it: a table **the
    reader did not touch at all** must come out of the sync with the grid the file
    asks for. There is nothing to merge in that case, so no merge rule can stand in
    the way, and the only excuse is the report naming the table.

    The file side is keyed first. `mine` is the file as it stood before the sync, and
    a block the source has just added has no key there: `doc_merge.plan` gives it one
    (`doc_ir.key_blocks`, in place on the file it is handed) and the report then names
    it by that key. Asked with no key, the excuse "the report says so" could never be
    found and every block the merge refuses to write read as a loss.
    """
    said = oracle.accounted(report)
    doc_ir.key_blocks(mine)
    sides = [oracle.parts_by_tab(ir) for ir in (was, before, mine, after)]
    out: list[Finding] = []
    for tab, part in sides[0].items():
        if any(tab not in side for side in sides[1:]):
            continue
        base_b, doc_b, src_b, end_b = (oracle.keyed(side[tab]) for side in sides)
        for key, block in base_b.items():
            here, file_b, then = doc_b.get(key), src_b.get(key), end_b.get(key)
            if here is None or then is None or file_b is None:
                continue
            if _grid(block) is None:
                out += _words_arrived(key, block, here, file_b, then, said, tab,
                                      step, seen)
                out += _styling_arrived(key, block, here, file_b, then, said, tab,
                                        step, seen, theme)
                continue
            out += _cells_arrived(key, block, here, file_b, then, said, tab,
                                  step, seen)
            if _grid(here) != _grid(block) \
                    or oracle.cells_of(here) != oracle.cells_of(block):
                continue                  # the reader touched it: the merge decides
            want, got = _grid(file_b), _grid(then)
            if want is None or got is None:
                continue
            if want != _grid(block):
                # A judge that never looks is indistinguishable from one that never
                # finds anything, so the chance is counted and not only the miss.
                seen["arrival/grid asked"] += 1
            if want == got:
                continue
            seen["arrival/grid missed"] += 1
            if oracle._named(said, key):
                continue
            out.append(oracle.finding(
                "grid_lost", "loss",
                f"step {step}: the reader left the table {key!r} exactly as the base "
                f"has it and the source asks for {want[0]}x{want[1]}, but the sync "
                f"left the document at {got[0]}x{got[1]} and the report says nothing",
                tab, key))
        out += _order_arrived(part, sides[1][tab], sides[2][tab], sides[3][tab],
                              said, tab, step, seen)
        out += _existence_arrived(part, sides[1][tab], sides[2][tab], sides[3][tab],
                                  said, tab, step, seen)
    return out


def _block_keys(part: Ir) -> list[str]:
    return [key for b in part["blocks"] if (key := b.get("key"))]


def _wordless(part: Ir) -> set[str]:
    """The keys of the blocks of this part that say nothing — a table with no words
    in it among them, since `doc_ir.key_blocks` names that one `table:empty`."""
    return {key for b in part["blocks"]
            if (key := b.get("key")) and not _says(b)[0].strip()}


def _order_arrived(was_p: Ir, doc_p: Ir, src_p: Ir, end_p: Ir, said: str, tab: str | None,
                   step: int, seen: Counter[str]) -> list[Finding]:
    """And the same question about the *order*: where the reader moved nothing, the
    blocks the source moved must come out where the file has them.

    The merged order is the document's, and on top of that the blocks the source
    moved go back where the file has them (`doc_merge.plan_order`). Only the first
    half of that was ever judged — by the loss oracle, which asks whether a block the
    reader put somewhere is still there. A source move that never arrives takes
    nothing of the reader's, and the settle writes the order the document ended up
    with into the base, so the next sync agrees with itself: the same shape as a
    column matched wrongly, and the reason this judge exists.

    Narrow on purpose: only where the reader left the shared keys in exactly the base's
    order, so no merge rule can stand in the way. There are three places where the
    merge refuses a move outright — a block between two tables, one in front of the
    table a body opens on, a table right behind another — and each of them says so.

    What is asked of the report is a **pair**, not a block. Which block "moved" is not
    a fact about two orders: swapping a paragraph and the table after it reads as
    either of them moving, `doc_merge._moved_keys` takes the shorter reading and here
    either is as short as the other, so the sync may refuse — and name — the table
    while the file reads as though the paragraph went. The disagreement is therefore
    counted as the pairs of keys that came out the other way round, and a pair is
    explained where the report names either of its two.

    And a key a block has **no words for** is left out — but only where the four sides
    do not agree on which keys those are. Such a key is not an identity of its own: it
    is that block's number among the wordless ones, so it means the same block on two
    sides exactly as long as the same blocks are wordless on both. Docs keeps a
    paragraph between two tables however it is deleted, so a source that drops one
    there leaves its mark standing empty — which then *is* `paragraph:empty`, and the
    block that carried that name before becomes `paragraph:empty#2`. Nothing moved and
    nothing was lost; two names changed hands, and the judge read it as the source's
    order undone (chain-10 seed 780188, shape `between_tables`). Where the population
    is unchanged the numbering means the same block on every side and the keys stay
    in, which is the whole of the narrowing: a judge gives up as little sight as it
    can, and a wordless block is the commonest thing a source move carries — a
    picture of its own, an empty line between two sections.
    `_existence_arrived` gives the same reason for asking about the words rather
    than the key.
    """
    sides = (was_p, doc_p, src_p, end_p)
    shared = set.intersection(*(set(_block_keys(p)) for p in sides))
    wordless = [_wordless(p) for p in sides]
    if any(each != wordless[0] for each in wordless):
        shared -= set().union(*wordless)
    was, doc, src, end = ([k for k in _block_keys(p) if k in shared] for p in sides)
    if doc != was or src == was:
        return []                 # the reader reordered, or the source did not
    seen["arrival/order asked"] += 1
    place = {key: at for at, key in enumerate(end)}
    astray = [(a, b) for at, a in enumerate(src) for b in src[at + 1:]
              if place[a] > place[b]
              and not oracle._named(said, a) and not oracle._named(said, b)]
    if not astray:
        return []
    seen["arrival/order missed"] += 1
    first, second = astray[0]
    return [oracle.finding(
        "order_lost", "loss",
        f"step {step}: the reader left the order of {tab or 'the body'} exactly as "
        f"the base has it and the file puts {first!r} in front of {second!r}, but the "
        f"sync left the document ordered {end} and the report says nothing",
        tab, first)]


def _existence_arrived(was_p: Ir, doc_p: Ir, src_p: Ir, end_p: Ir, said: str,
                       tab: str | None, step: int, seen: Counter[str]) -> list[Finding]:
    """And the same question about a block *being there at all*: where the reader left
    it alone, a block the source dropped must be gone and one it added must be in.

    The last two things a source edit can ask for, and the two the other judges step
    around: `_words_arrived` and `_styling_arrived` want a key on both sides, and
    `_order_arrived` looks only at the keys every side shares. The loss oracle will
    not ask either, and for the reason it gives in its own first paragraph — a block
    of the source's that never arrives, or one the source wanted gone that stays,
    takes nothing of the *reader's* — and the settle then writes what the document
    holds into file and base alike, so the round converges on it.

    Both halves ask of the **words**, not of the key. A key is made from a block's
    words where no range carries it, so two blocks that say the same thing can trade
    keys and a wordless one (an empty paragraph, a lone picture) has a key the next
    such block would be given too: asking whether the key is still there would accuse
    the merge of aliasing and excuse it of real losses. So a block is gone when what
    it said is gone, and here when what it says is said. Wordless blocks are nobody's
    question — `_order_arrived` has them by their keys, and `oracle.block_gone` has
    the reader's.

    But **a block at a time**, word for word, and by counting (`_saying`): how many
    blocks of the tab say exactly this one's text, before the sync and after. Two
    looser questions were tried and both are answered by coincidence, because the
    campaign's whole vocabulary is a few dozen words: a bag of words over the tab
    let a `collide` rewording another paragraph to `ribbon` stand in for the dropped
    `the reader wrote ribbon` (chain-8 seed 720173), and counting blocks that say
    *at least* this one's words let `What we found.` become `harbour we found.` and
    answer for a heading that said `harbour` (720270). What a drop that failed
    leaves behind is the block itself, verbatim, which is what `_words_arrived`
    already asks of a rewording. Counting handles twins for free, which is what the
    first guard here was for: two blocks saying the same thing go from two to one.

    The count is of the **text** alone, though the guard above — did the reader leave
    this block as the base has it? — is of everything it says, chips included. What is
    being counted is other blocks, and one of those may be having a chip put into it by
    this very step: the source appended `the source added lantern` twice over two
    steps and then gave the first copy a person chip, so the twin stopped saying what
    the new one says and the count stood still while both blocks arrived (chain-8 seed
    720074). A chip is content, and `_words_arrived` and the loss oracle both ask about
    it; existence is about existence.

    And the count is only **one** of two traces, because the confound is the counting
    itself: it is a question about a block asked of every *other* block that says the
    same thing, so anything at all happening to a twin answers it wrongly — the chip
    above, and at chain-12 seed 730061 a `collide` rewording the twin into `the
    umbrella added harbour` in the very step that appended the copy. The other trace
    is the **key**: the plan gives a block it writes a named range of its own, so the
    settle, regenerating the file from the document, keys it back — a block that
    arrived is in `end_p` under the key the file gave it, and one the sync dropped is
    not. Either trace excuses; a finding needs *no* trace of the block at all. The two
    fail in different directions (the count is confounded by twins, the key by a
    repair that re-derives one), and neither is the merge's opinion of its own work.

    Tables are left to `_grid` and `_cells_arrived`, whose question is sharper than a
    bag of words and whose own `dropped-table` defect the oracle found years of seeds
    ago.
    """
    was_b, doc_b = (oracle.keyed(p) for p in (was_p, doc_p))
    file_keys, end_keys = set(_block_keys(src_p)), set(_block_keys(end_p))
    out: list[Finding] = []
    for key, block in was_b.items():
        here, mine = doc_b.get(key), _says(block)
        if key in file_keys or here is None or _grid(block) is not None \
                or not mine[0].strip() or _says(here) != mine:
            continue                  # the source kept it, or the reader wrote in it
        seen["arrival/gone asked"] += 1
        if key not in end_keys or _saying(end_p, mine[0]) < _saying(doc_p, mine[0]) \
                or oracle._named(said, key):
            continue
        seen["arrival/gone missed"] += 1
        out.append(oracle.finding(
            "drop_lost", "loss",
            f"step {step}: the source dropped {key!r} and the reader left it word for "
            f"word as the base has it, but a block still carries that key and as many "
            f"say {_says(block)[0][:60]!r} when the sync is over as before it, and "
            f"the report says nothing", tab, key))
    for block in src_p["blocks"]:
        key, mine = block.get("key"), _says(block)
        if key in was_b or not mine[0].strip() or _grid(block) is not None:
            continue                  # a block the source has added, and it says something
        seen["arrival/added asked"] += 1
        if (key and key in end_keys) \
                or _saying(end_p, mine[0]) > _saying(doc_p, mine[0]) \
                or oracle._named(said, key):
            continue
        seen["arrival/added missed"] += 1
        out.append(oracle.finding(
            "addition_lost", "loss",
            f"step {step}: the source added {key!r} saying {_says(block)[0][:60]!r}, "
            f"but nothing in {tab or 'the body'} carries that key when the sync is "
            f"over and no more blocks of it say that than before, and the report "
            f"says nothing", tab, key))
    return out


def _saying(part: Ir, text: str) -> int:
    """How many blocks of the part say exactly this text."""
    return sum(1 for b in part["blocks"] if _says(b)[0] == text)


def _words_arrived(key: str, block: Block, here: Block, file_b: Block, then: Block,
                   said: str, tab: str | None, step: int,
                   seen: Counter[str]) -> list[Finding]:
    """The same question for a paragraph: one the reader left word for word as the
    base has it, and the source reworded, must say what the file says when the sync
    is over.

    There is nothing to merge in that case — it is the plainest thing a sync does —
    and it was judged by nobody. The oracle asks whether the *reader's* work is still
    there and a source edit that never arrives takes nothing of theirs away;
    convergence is satisfied by any reading the base then agrees with. So an edit
    could be dropped in silence as long as it was dropped consistently.
    """
    if block.get("kind") == "table":
        return []
    mine, was, now, end = (_says(b) for b in (file_b, block, here, then))
    if now != was or mine == was:
        return []                     # the reader wrote in it, or the source did not
    seen["arrival/words asked"] += 1
    if end == mine or oracle._named(said, key):
        return []
    seen["arrival/words missed"] += 1
    return [oracle.finding(
        # Not `words_lost`, which is the oracle's own and asks the opposite question
        # (words of the *reader's* gone from a block). Two checks under one kind make
        # a `KNOWN` entry, and a triage, mean two things at once.
        "wording_lost", "loss",
        f"step {step}: the reader left {key!r} word for word as the base has it and "
        f"the source made it {mine[0]!r}, but the sync left the document saying "
        f"{end[0]!r} and the report says nothing", tab, key)]


def _styling_arrived(key: str, block: Block, here: Block, file_b: Block, then: Block,
                     said: str, tab: str | None, step: int,
                     seen: Counter[str], theme: Mapping[str, NamedDefault]) -> list[Finding]:
    """And the same question about a block's *look*: one the reader left exactly as
    the base has it, words and styling both, must look the way the file asks when the
    sync is over.

    The widest mechanism in the merge had the narrowest judge. `MANAGED` and
    `MANAGED_PARAGRAPH` are the fields a restyle may *clear*, `_take_shape` is what
    makes a dropped property go away, `paragraph_style` subtracts the named style so a
    theme is not pinned, `named_style` says which style a block is, and the settle
    repairs what no import could carry (`carry_unimported`) and what the write itself
    mangled (`_unwritten`). Every one of those is measured by the loss oracle only
    from the reader's side — it asks whether the reader's own face or shading survived
    — and by convergence only for self-consistency. A restyle that never arrived takes
    nothing of the reader's and leaves the base agreeing with the document, so it is
    exactly the shape of thing both other judges are built to miss.

    Two questions, because they are two code paths: the runs travel as
    `updateTextStyle` off `_text_style`, the paragraph as `updateParagraphStyle` off
    `paragraph_style` plus `_take_shape`, and a finding should say which.

    Value and all (`_worn`), which `oracle.marks_on` deliberately does not do: it
    counts a mark per word to answer "is the bold back on?", so a run's face is the
    bare key `font` there and Georgia reads the same as Roboto Mono. And what somebody
    *chose* rather than what a word wears, so no theme values are needed: the named
    style is part of `_shape`, and two blocks of one named style that were given the
    same styling look the same.
    """
    if block.get("kind") == "table":
        return []
    text, mine = _says(block), _says(file_b)
    if _says(here) != text or mine != text:
        return []                     # the reader wrote in it, or the source did
    look = [(_worn(b, theme), _shape(b, theme)) for b in (block, here, file_b, then)]
    was_look, now_look, want_look, got_look = look
    if now_look != was_look:
        return []                     # the reader restyled it: the merge decides
    halves = [("shape", "reshape_lost", was_look[1], want_look[1], got_look[1])]
    if len({doc_merge.named_style(b)
            for b in (block, here, file_b, then)}) == 1:
        halves.insert(0, ("runs", "restyle_lost",
                          was_look[0], want_look[0], got_look[0]))
    else:
        # A mark says what it says against the block's **named style**, which is what
        # `_worn` subtracts it by, so where the source moves a block from one named
        # style to another the two sides are not spelling the same language and the
        # run half is not a comparison at all. Seed 1130023, shape `themed`: the theme
        # bolds HEADING_1, so the reader's bold on one word of a themed heading says
        # nothing there and the guard above read the block as one they had left alone;
        # the source then made it NORMAL_TEXT, where the same run styling spells out
        # differently on either side, and the reader's bold — which the merge kept, as
        # it should — read as the source's restyle vanishing. The named style itself is
        # part of `_shape`, so nothing goes unjudged: the half that can speak does.
        seen["arrival/runs unasked"] += 1
    out: list[Finding] = []
    for what, kind, now, want, got in halves:
        if want == now:
            continue
        seen[f"arrival/{what} asked"] += 1
        if want == got or oracle._named(said, key):
            continue
        seen[f"arrival/{what} missed"] += 1
        out.append(oracle.finding(
            kind, "loss",
            f"step {step}: the reader left {key!r} exactly as the base has it, the "
            f"base had {now}, the source asks for {want}, and the sync left the "
            f"document at {got} with the report saying nothing", tab, key))
    return out


def _worn(block: Block, theme: Mapping[str, NamedDefault]) -> tuple[tuple[Marks, str], ...]:
    """A block's run styling as styled stretches of text, adjacent alike ones merged.

    Per character and not per word, which is what `oracle.marks_on` is and what this
    was first: a word wears only what every character of it wears, so a reader bolding
    `it` inside the word `it.` changed nothing that could be seen, and the block read
    as one they had left alone (seed 110082). That rule is right for the question the
    oracle asks — is the bold *back on*? — and wrong for this one, which is about what
    the merge wrote over which characters. Merging the stretches keeps it blind to the
    one thing that does not matter, how many runs the text was cut into.

    Two normalisations, both because the file and a read-back spell the same thing
    differently: `code` is the file's older word for a monospaced face and reaches the
    document as `weightedFontFamily` (`doc_ir.CODE_FAMILY`), and a mark that says no
    more than the paragraph's named style says is left out of a read
    (`doc_ir._named_defaults`), so it has to go from the file's side too. Both
    directions of that: a `bold: True` under a style that bolds, and a `bold: False`
    under one that does not — the second because a source that *retitles* a heading
    the reader had un-bolded keeps the `False` in the file, where it now says nothing
    (seed 220012), and reading it as a mark accuses the sync of losing an un-bolding
    of a word that was never bold.
    """
    said = theme.get(doc_merge.named_style(block))
    puts_on: set[str] = set(said.get("marks") or ()) if said else set()
    out: list[tuple[Marks, str]] = []
    for run in block.get("runs", []):
        if run.get("frozen"):
            continue
        style: dict[str, object] = dict(run)
        if style.pop("code", None) and not style.get("font"):
            style["font"] = doc_ir.CODE_FAMILY
        # A dict's keys are unique, so sorting by the key alone is the whole order.
        marks = tuple(sorted(((k, _flat(v)) for k, v in style.items()
                              if k not in ("text", "width")
                              and not (k in MARK_KEYS and bool(v) == (k in puts_on))),
                             key=lambda kv: kv[0]))
        if out and out[-1][0] == marks:
            out[-1] = (marks, out[-1][1] + run.get("text", ""))
        else:
            out.append((marks, run.get("text", "")))
    return tuple((marks, text) for marks, text in out if text)


def _flat(value: object) -> object:
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value


def _shape(block: Block, theme: Mapping[str, NamedDefault]) -> tuple[object, ...]:
    """A block's paragraph styling: its named style and every measure the merge owns.

    A paragraph reads back with a property only where it differs from its named
    style's (`doc_ir._named_defaults`), while the file says its own out loud, so what
    the theme already says has to come off the file's side too or a write that arrived
    reads as one that vanished: a source centring a heading its theme centres (seed
    96300), and, commonest of all, an explicit `left` where the style aligns left or
    not at all. Where the theme aligns this style otherwise, an explicit `left` is a
    real choice and is reported as one.

    An item's indents are nobody's to write (`doc_merge.ITEM_PARAGRAPH`): they are the
    list preset's, `doc_ir` leaves them out of a read and `createParagraphBullets`
    would overwrite them anyway. Only `_mark_paragraph` can put one in a file at all.
    """
    name = doc_merge.named_style(block)
    fields = ("level", "ordered") + tuple(k for k, _ in doc_merge.PARAGRAPH_KEYS)
    if block.get("kind") == "item":
        fields = tuple(f for f in fields if not f.startswith("indent"))
    gives: Mapping[str, object] = theme.get(name) or {}
    out: dict[str, object] = {}
    for field in fields:
        value = oracle.field_of(block, field)
        # A theme that sets nothing aligns left, which is what the API's own default
        # is: the two spellings of that are one thing.
        inherited = gives.get(field) or ("left" if field == "align" else None)
        out[field] = None if value == inherited else value
    # A dict's keys are unique, so sorting by the key alone is the whole order.
    return (name,) + tuple(sorted(((k, v) for k, v in out.items() if v is not None),
                                  key=lambda kv: kv[0]))


def _says(block: Block) -> tuple[str, Counter[FrozenKey]]:
    """What a block says, comparably between the file and a read-back: the words of
    its own, and the frozen runs by what they *are*.

    Not `oracle.text_of`, which takes a chip at its face value — and a chip's face is
    the document's to draw. The file asks for a person chip reading `Grace` and Docs
    renders `grace` off the address, so every source `add_chip` read as an edit that
    never arrived. `frozen_key` is the oracle's own answer to the same question.
    """
    return ("".join(r.get("text", "") for r in oracle.runs_of(block)
                    if not r.get("frozen")),
            oracle.frozen_marks(block))


def _cell_says(block: Block) -> dict[tuple[int, int], CellSays]:
    """What each cell of a table says, in `_says`' language rather than `cells_of`'s.

    The same subtraction one size down, and it had to be made twice because the two
    judges were written a week apart: `oracle.cells_of` is `text_of` per cell, so a
    chip in a cell is counted by its **face**, and the face is the document's to draw
    — a person chip the source puts into a cell comes back as the object character, so
    the edit read as one that never arrived and every seed drawing `cell_chip` failed
    at once. The cell is the last place in this file that asked in the old language.
    """
    out: dict[tuple[int, int], CellSays] = {}
    for r, row in enumerate(block.get("rows", [])):
        for c, cell in enumerate(row):
            said = [_says(b) for b in cell]
            marks: Counter[FrozenKey] = Counter()
            for _, m in said:
                marks += m
            out[(r, c)] = (" ".join(w for w, _ in said),
                           tuple(sorted(marks.items())))
    return out


def _cell_text(said: CellSays) -> str:
    """One cell's `_cell_says` as a line a person can read in a finding."""
    words, marks = said
    return words + "".join(f" [{kind} {value}]" for (kind, value), n in marks
                           for _ in range(n))


def _cells_arrived(key: str, block: Block, here: Block, file_b: Block, then: Block,
                   said: str, tab: str | None, step: int,
                   seen: Counter[str]) -> list[Finding]:
    """A cell the source rewrote, in a table whose words the reader did not touch,
    must be somewhere in the table when the sync is over.

    This is the half of `_arrived` that can see a column matched wrongly, and the
    narrow half above cannot: with the reader's hands off the grid entirely, pairing
    columns by place and pairing them by their words agree, because the base grid and
    the document's grid are the same grid. What tells them apart is the reader
    *regridding* while the source edits a cell — the reader takes the first column
    out, the source rewrites a cell in the last one, and by place the file's last
    column pairs with a base column the merge has already accounted for, so the
    source's words are written nowhere at all.

    The reader may add and delete rows and columns here; what they may not do is
    write, and `doc_cells - base_cells` is how that is asked. A cell whose base text
    the reader deleted along with its line is no arrival to wait for, so the old text
    has to still be there before the sync for the new one to be owed.

    And only words the source really *wrote* are waited for: a word the base already
    says somewhere is one a regrid shifted into this cell, not a new one. The half
    above asks about the grid, and it recognises a regrid by the grid's size — so a
    source that takes one row out and puts another in slips past it, and every cell
    below the one it took read as rewritten with the row above's words (seed 570177,
    where the reader deleted a different row, so both deletes stood and the merge was
    right).
    """
    if _grid(block) is None or _grid(file_b) != _grid(block):
        return []                     # the source regridded: the half above asks
    base_cells, doc_cells = (Counter(_cell_says(b).values())
                             for b in (block, here))
    if doc_cells - base_cells:
        return []                     # the reader wrote in it: the merge decides
    was_cells, file_cells = (_cell_says(b) for b in (block, file_b))
    after = Counter(_cell_says(then).values())
    out: list[Finding] = []
    for at, text in file_cells.items():
        old = was_cells.get(at)
        if text == old or base_cells[text] \
                or (old is not None and doc_cells[old] < base_cells[old]):
            continue
        seen["arrival/cell asked"] += 1
        if after[text] or oracle._named(said, key):
            continue
        seen["arrival/cell missed"] += 1
        out.append(oracle.finding(
            "cell_lost", "loss",
            f"step {step}: the source rewrote the cell at {at} of the table {key!r} "
            f"to {_cell_text(text)!r}, the reader wrote nothing in that table, and no "
            f"cell of it says so when the sync is over", tab, key))
    return out


def offline_round(seed: int, chain: int, script: Script | None,
                  seen: Counter[str]) -> list[Finding]:
    """One round of `seed` at `chain` steps, or of `script` when one is given."""
    return run_script(script or draw(seed, chain, None), seen)


# ---------------------------------------------------------------- shrinking

#: What counts as the same failure, to the shrinker: the findings that still fail.
Still = Callable[[list[Finding]], list[Finding]]


def shrink(script: Script, rounds: int, still: Still | None) -> Script:
    """Take a failing script apart: drop steps, then ops, keeping every reduction that
    still fails. Each op's salt travels with it, so the rest draw what they drew.

    `still` says what counts as the same failure (None: any failure at all). A
    reduction that trades an unknown finding for one of the `KNOWN` ones is no
    reduction at all — it would shrink the new defect away and print a reproduction
    of an old one. `rounds` bounds the reductions tried (200 from the command line).
    """
    fails: Still = still or oracle.failures
    best = script
    tried = 0
    changed = True
    while changed and tried < rounds:
        changed = False
        for candidate in _reductions(best):
            tried += 1
            if tried > rounds:
                break
            if fails(_quiet(candidate)):
                best, changed = candidate, True
                break
    return best


def _reductions(script: Script) -> Iterator[Script]:
    steps = script.steps
    for i in range(len(steps)):
        if len(steps) > 1:
            yield dataclasses.replace(script, steps=steps[:i] + steps[i + 1:])
    for i, step in enumerate(steps):
        for j in range(len(step.reader)):
            fewer = dataclasses.replace(step, reader=step.reader[:j] + step.reader[j + 1:])
            yield dataclasses.replace(script, steps=steps[:i] + (fewer,) + steps[i + 1:])
        for j in range(len(step.source)):
            fewer = dataclasses.replace(step, source=step.source[:j] + step.source[j + 1:])
            yield dataclasses.replace(script, steps=steps[:i] + (fewer,) + steps[i + 1:])


def _quiet(script: Script) -> list[Finding]:
    try:
        return run_script(script, Counter())
    except Exception:                                  # noqa: BLE001
        return [oracle.finding("crash", "loss", "the harness itself raised", None, None)]


def describe_script(script: Script) -> str:
    lines = [f"  shape {script.shape}"]
    for i, step in enumerate(script.steps):
        lines.append(f"  step {i}: reader "
                     + ", ".join(f"{n}({s})" for n, s in step.reader)
                     + " | source " + ", ".join(f"{n}({s})" for n, s in step.source))
    return "\n".join(lines)


# ---------------------------------------------------------------- what is known already

# The defects this campaign found and nobody has fixed yet — **empty**, and that is
# the point of the tuple, not an accident of it. They are listed so the hunt can go
# past them: a round that ends on one of these is counted, named and let through, and
# only an unknown finding fails the campaign. Each is pinned by an xfail in
# tests/test_doc_fuzz.py with its minimal reproduction, and `--strict` fails on them
# again — which is how one checks a fix, and how a fix is noticed here at all.
#
# A signature is a finding's kind and a piece of its words. It says which *symptom*
# was seen, not which defect caused it: several of these showed up as a lost key, and
# the tests, not the signature, say which is which. Which is why a fixed entry goes:
# it is let through, so it would swallow the next defect that looks like it.
@dataclass(frozen=True, kw_only=True)
class KnownBug:
    """A defect found and not yet fixed: its id, and the finding's kind and a piece of
    its words that say it is this one."""
    id: str
    kind: str
    has: str


KNOWN: tuple[KnownBug, ...] = (
    # Seven entries stood at the head and the foot of this tuple and are gone, not
    # rewritten: each is fixed and has a test of its own in tests/test_doc_fuzz.py, and
    # each signature was wide enough to swallow the next defect that looks like it —
    # `block_gone` mentioning `table:` had been catching crossed keys on tables all
    # along, and `frozen_gone` with no words at all would catch every way of losing a
    # picture there will ever be. They were `toc-block`, `toc-table-split` and
    # `empty-delete` (all three ways of killing a sync outright — `doc_ir.STRUCTURAL`
    # and `doc_merge.restore_undeletable`), `dropped-table`, `dropped-frozen`,
    # `crossed-frozen`, whose last 9 findings were the oracle's own — two pictures a
    # reader pasted from one url share a name that says which picture it is, so the
    # one that went was paired with the one that stayed and the survivor was named as
    # lost (`doc_loss_oracle.telling_names`), 22 -> 13 -> 9 -> 0 — and
    # `table-in-a-table`, which is `doc_merge.refuse_nowhere`: between two tables the
    # document has no paragraph to write in, and a block written there anyway lands
    # inside the last cell of the table before it.
    # `lost-key` (34 -> 2 -> 0) went the same way once its sixth cause was fixed: an
    # empty paragraph is all mark, so its named range *is* its mark, and "\ntext"
    # written at that mark is handed the range (an insert at a range's own first index
    # pushes it along) - the empty paragraph's key rode onto the block that was written.
    # Reading it back off the wrong block was tried twice and cost the round its
    # convergence; `doc_merge.requests` plants the range again on the mark in the same
    # batch instead (`REPLANT`), which puts it where the write meant it - and the same
    # drift from the reader's side (a chip put into an empty paragraph) is planted back
    # at the settle and at the head of a structural batch (`doc_ir.replant_requests`),
    # since the batch that builds a table swallows that very mark. The five
    # before it: `inherit_keys` reassigning a key the file itself asserts, `settle`
    # keying every tab before those tabs are adopted (`doc_merge.settle_keys`), a delete
    # carrying the style of the block above onto the survivor (the settle writes the
    # named style back), a row delete taking with it the first cell a table is anchored
    # in (`structure` gives a regrid an `after`), and a write changing a block's shape
    # so that `adopt_keys` could never adopt the very blocks a write mangles
    # (`doc_merge._adopt_by_words`). `moved-styling` (2 -> 0) was `_retext` folding a
    # whole stretch into its first writable run; it maps every word's styling now. Its
    # signature had no words, so it hid two more: a block written from nothing
    # inheriting the styling in front of it (`_style_requests` names every managed
    # field), and a styled word the source rewrote, which is a loss that is right and
    # is now said (`doc_merge.reader_styling_gone`).
    # The tenth, `crossed-delete`, is gone the same way and was the last one here: two
    # blocks under one key and none under the other, so the merge read the second as
    # "the source dropped it", deleted the paragraph the reader was reading and wrote
    # the source's new wording nowhere — `lost-key` one step worse (shrunk from chain-8
    # seed 1031). `inherit_keys` crossing the keys the file asserts was how it got
    # there, and that is fixed. It stayed on here after the fix, to catch whatever else
    # could reach the signature, which is backwards: an entry in this tuple is *let
    # through*, so keeping it is the one way to make sure nothing is caught. The rule
    # the rest of this tuple was emptied by holds for the last one too.
)
# Nothing known is outstanding. A campaign that fails now has found something new.
#
# What that was worth, the first time it was tried: at two seeds nobody had used
# (1200 rounds at chain 4 from 90000, 400 at chain 8 from 40000) the campaign came
# back with six findings, and one of them was the `block_gone ... though the file
# still names it` that `crossed-delete` covered word for word. Four were real losses
# — `anchor_tables` looking once where it had to look twice, two tables behind one
# anchor taking each other's keys, a move taken back and left at the file's position
# anyway, and `_edited` blind to a mark the reader put on — and two were the oracle's
# own. Every one of them is a test in tests/test_doc_fuzz.py now.
#
# And the run after that, which went deeper rather than wider — 2000 rounds at chain
# 6 from 500000, six edit-and-sync steps per round instead of one — came back with
# five more in four signatures, three of them `block_gone` on a table again: a table
# anchored on an empty paragraph whose mark the same batch swallows, which takes its
# named range (`doc_merge._swallowed`); a source move whose two ends are one place,
# written anyway, which for a table means built blank and asked for again until
# `_write_structure`'s rounds run out; and the oracle counting the base's word in
# `.1` as one the reader typed (`_dressed_up`, since retired: the drag that pushed
# that stop against the `1` was this file's own bug). A sweep of every reordering of a
# five-block body, written to find a scenario for the second, turned up the fourth
# and worst: a block the source moves past a table to the end of the body was
# written *into* the table — chain-8 seed 189's defect, whose fix a day earlier had
# covered deletes and not moves.


def known_bug(found: Finding) -> str | None:
    """Which known defect this finding is a symptom of, if any."""
    for bug in KNOWN:
        if found.kind == bug.kind and bug.has in found.detail:
            return bug.id
    return None


def triage(found: list[Finding]) -> tuple[list[Finding], list[str]]:
    """The failures nobody knows about yet, and the ids of the known ones."""
    unknown: list[Finding] = []
    known: list[str] = []
    for one in oracle.failures(found):
        bug = known_bug(one)
        if bug:
            known.append(bug)
        else:
            unknown.append(one)
    return unknown, known


def _unknown(found: list[Finding]) -> list[Finding]:
    """The shrinker's question once a round has found something new: is it still new?"""
    return triage(found)[0]


# ---------------------------------------------------------------- the campaign

def run_offline(rounds: int, seed: int, chain: int, do_shrink: bool,
                shape: str | None, strict: bool) -> int:
    seen: Counter[str] = Counter()
    started, bad = time.time(), 0
    hit: Counter[str] = Counter()
    for n in range(rounds):
        script = draw(seed + n, chain, shape)
        found = offline_round(seed + n, chain, script, seen)
        unknown, known = triage(found)
        hit.update(known)
        if not unknown and not (strict and known):
            continue
        bad += 1
        print(f"\nseed {seed + n} FAILED")
        print(oracle.describe(found))
        print(describe_script(script))
        if do_shrink:
            small = shrink(script, 200, _unknown if unknown else None)
            if small != script:
                print("  shrunk to:")
                print(describe_script(small))
                print(oracle.describe(_quiet(small)))
        print("  replay: --replay %d --chain %d" % (seed + n, chain))
    took = time.time() - started
    print(f"\n{rounds} rounds, chain {chain}, {bad} failed, "
          f"{rounds / max(took, 1e-9):.1f} rounds/s")
    print(known_seen(hit))
    print(coverage(seen))
    return 1 if bad else 0


def known_seen(hit: Counter[str]) -> str:
    """What the round hit of what is known already. A known defect nobody reaches any
    more is worth saying out loud: either it is fixed, or the campaign stopped looking."""
    if not KNOWN:
        return ("known defects: none outstanding — every finding fails the campaign, "
                "and --strict has nothing more to add.")
    lines = ["known defects (let through; --strict fails on them):"]
    for bug in KNOWN:
        lines.append(f"  {bug.id}: {hit.get(bug.id, 0)}"
                     + ("" if hit.get(bug.id) else "   (not reached in this run)"))
    return "\n".join(lines)


def coverage(seen: Counter[str]) -> str:
    """What the campaign actually reached. A campaign that never plans an insertTable
    proves nothing about tables, and only counting says so."""
    groups: dict[str, Counter[str]] = {}
    for key, count in seen.items():
        head, _, rest = key.partition("/")
        groups.setdefault(head, Counter())[rest or head] = count
    lines = ["coverage:"]
    for head in sorted(groups):
        items = ", ".join(f"{k} {v}" for k, v in sorted(groups[head].items()))
        lines.append(f"  {head}: {items}")
    return "\n".join(lines)


def main(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("mode", choices=["offline"], nargs="?", default="offline")
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chain", type=int, default=1)
    ap.add_argument("--shape", choices=SHAPES)
    ap.add_argument("--replay", type=int)
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="fail on the known defects too (KNOWN): how a fix is checked")
    args = ap.parse_args(argv)
    chain: int = args.chain
    shape: str | None = args.shape
    strict: bool = args.strict
    shrinking = not args.no_shrink
    if args.replay is not None:
        replay: int = args.replay
        script = draw(replay, chain, shape)
        found = offline_round(replay, chain, script, Counter())
        print(describe_script(script))
        print(oracle.describe(found))
        unknown, known = triage(found)
        for bug in known:
            print(f"  known: {bug}")
        if oracle.failures(found) and shrinking:
            small = shrink(script, 200, _unknown if unknown else None)
            print("shrunk to:")
            print(describe_script(small))
            print(oracle.describe(_quiet(small)))
        return 1 if unknown or (strict and known) else 0
    rounds: int = args.rounds
    seed: int = args.seed
    return run_offline(rounds, seed, chain, shrinking, shape, strict)


if __name__ == "__main__":
    sys.exit(main(None))
