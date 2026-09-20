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
import json
import random
import re
import sys
import time
from collections import Counter

from .. import doc_ir, doc_merge
from . import doc_loss_oracle as oracle
from . import doc_world
from .doc_world import Refused

MIN_EDITS, MAX_EDITS = 1, 4
COLLIDE_CHANCE = 0.45
WORD = re.compile(r"\w+")

FRESH = ["kestrel", "harbour", "lantern", "meadow", "quartz", "ribbon", "signal",
         "thicket", "umbrella", "vellum", "willow", "zephyr"]

# The whole dialect, not the one mark it is easiest to draw. `doc_merge.MANAGED` says
# which run fields a restyle may *clear* and `MANAGED_PARAGRAPH` which paragraph ones,
# so every field named there has to be reachable from both sides or the clearing is
# unfuzzed: a campaign that only ever sets italic proves nothing about a face, a size
# or an indent. `code` and `font` are one field on the wire (both are the run's family),
# so drawing one drops the other.
RUN_MARKS = [("bold", True), ("italic", True), ("underline", True), ("strike", True),
             ("smallcaps", True), ("code", True),
             ("font", "Georgia"), ("font", "Roboto Mono"),
             ("fontsize", 9.0), ("fontsize", 14.5),
             ("color", "#993333"), ("highlight", "#ffee88")]

PARA_MARKS = [("align", "center"), ("align", "justify"), ("indent", 18.0),
              ("indent_first", 36.0), ("line_spacing", 1.5), ("shading", "#eef2ff"),
              ("space_above", 6.0), ("space_below", 12.0)]


# ---------------------------------------------------------------- the corpus

def _p(text, **kw):
    return {"kind": "paragraph", "runs": [{"text": text}], **kw}


def _h(text, level=1):
    return {"kind": "heading", "level": level, "runs": [{"text": text}]}


def _i(text, level=0, glyphs=None):
    return {"kind": "item", "level": level, "glyphs": glyphs, "runs": [{"text": text}]}


def _t(rows):
    return {"kind": "table",
            "rows": [[[_p(cell)] for cell in row] for row in rows]}


def _shapes() -> dict:
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
                  _i("alpha"), _i("beta"), _i("gamma", 1), _p("A closing line.")],
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
        "imported_list": [_h("Steps"), _i("mix"), _i("bake"), _i("cool"),
                          _p("Follow them in order.")],
        "chips": [{"kind": "paragraph", "runs": [
            {"text": "ask "}, {"chip": "person", "text": "Ada", "value": "ada@example.com"},
            {"text": " before "}, {"chip": "date", "text": "Sep 20, 2026",
                                   "value": "2026-09-20T00:00:00Z"}]},
            {"kind": "paragraph", "runs": [
                {"chip": "image", "value": "kix.pic0", "src": "media/plot.png",
                 "uri": "https://example.invalid/plot.png"}]},
            _p("A caption under it.")],
        "toc": [{"kind": "toc"}, _h("One"), _p("First section."),
                _h("Two"), _p("Second section.")],
        "astral": [_p("emoji \U0001F600 and maths \U0001D538 in one line"),
                   _p("a soft­hyphen and a   break"), _p("plain tail")],
        "dropdown": [{"kind": "paragraph", "runs": [
            {"text": "status "}, {"chip": "unknown", "text": ""}, {"text": " today"}]},
            _p("and a line after it.")],
    }


def corpus(name: str) -> doc_world.World:
    """The world a round starts from. `tabs` is the one shape that is not a block
    list: a second tab makes every index and every key a tab's own."""
    shapes = _shapes()
    if name == "tabs":
        return doc_world.build([{"blocks": shapes["prose"]},
                                {"title": "Appendix", "blocks": shapes["ends_on_table"]}],
                               title="fuzz")
    return doc_world.build([{"blocks": shapes[name]}], title="fuzz")


SHAPES = sorted(list(_shapes()) + ["tabs"])


# ---------------------------------------------------------------- the sync, offline

class Stager:
    """`doc_sync.Stager` without Drive: a staged picture gets a URL the world can hold."""

    def resolve(self, requests: list[dict]) -> list[dict]:
        out = []
        for request in requests:
            image = request.get("insertInlineImage")
            if image and image["uri"].startswith(doc_merge.STAGE):
                request = copy.deepcopy(request)
                request["insertInlineImage"]["uri"] = \
                    "https://staged.invalid/" + image["uri"][len(doc_merge.STAGE):]
            out.append(request)
        return out


def bootstrap(world: doc_world.World) -> dict:
    """What `docs push` leaves behind: every block keyed and named in the document, and
    a file that is the document's own read."""
    ir = doc_world.read_ir(world)
    for part in doc_ir.parts(ir):
        stamp = None if part is ir else part.get("tab")
        doc_ir.key_blocks(part)
        requests = doc_merge.on_tab(doc_ir.name_requests(part), stamp)
        if requests:
            world.apply(requests)
    ir = doc_world.settled_ir(world, ir, ir)
    _fetch_pictures(ir)     # `push` knows every picture's file: it embedded them
    return ir


def sync_once(world: doc_world.World, ours: dict, base: dict,
              seen: Counter | None = None) -> tuple[dict, dict, dict]:
    """`doc_sync.sync` against the world: plan every tab, write it, then settle.

    Returns the report and the file and base the settle leaves behind — which are the
    same read, as they are on a real sync: file, document and base agree from here.
    """
    seen = seen if seen is not None else Counter()
    theirs = doc_world.read_ir(world, ours, base)
    tabs = doc_merge.pair_tabs(base, ours, theirs)
    if tabs["requests"]:
        _send(world, tabs["requests"], seen)
    pairs = list(tabs["pairs"])
    known = {p.get("tab") for p in doc_ir.parts(theirs)} - {None}
    made: dict = {}
    for part in tabs["create"]:
        parent = made.get(part.get("parent"), part.get("parent"))
        reply = _send(world, [doc_merge.add_tab_request(
            part | {"parent": parent}, known | set(made.values()))], seen)
        tab = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        if part.get("tab"):
            made[part["tab"]] = tab
        part["tab"] = tab
        if part.get("parent") in made:
            part["parent"] = made[part["parent"]]
        pairs.append((tab, part, {"blocks": []}))

    stager, written = Stager(), []
    for tab, mine, was in [(None, ours, base)] + pairs:
        written.append(_sync_part(world, stager, tab, ours, base, mine, was, seen))
    report = _report(ours, tabs, written)
    live = settle(world, ours, base, {each["stamp"]: each["result"]["blocks"]
                                      for each in written})
    return report, live, copy.deepcopy(live)


def _sync_part(world, stager, tab, ours, base, mine, was, seen) -> dict:
    theirs = doc_world.part_ir(world, tab, ours, base)
    result = doc_merge.plan(was, mine, theirs)
    was, result, shaped = _write_structure(world, tab, ours, base, mine, was, result, seen)
    if result["requests"]:
        _send(world, stager.resolve(doc_merge.on_tab(result["requests"], tab)), seen)
    return {"stamp": tab, "label": mine.get("title", "") if tab else None,
            "result": result, "shaped": shaped}


def _write_structure(world, tab, ours, base, mine, was, result, seen) -> tuple:
    """`doc_sync._write_structure`: the grid first, on its own, then read again and
    plan the words against the grid the document has now."""
    shaped: list[dict] = []
    for _ in range(3):
        if not result["structure"]:
            break
        _send(world, doc_merge.on_tab(result["structure"], tab), seen)
        shaped += result["shaped"]
        theirs = doc_world.part_ir(world, tab, ours, base)
        if doc_merge.anchor_tables(theirs, result["shaped"]):
            _send(world, doc_merge.on_tab(doc_ir.name_requests(theirs), tab), seen)
            theirs = doc_world.part_ir(world, tab, ours, base)
        was = doc_merge.rebase_tables(was, theirs, result["shaped"])
        result = doc_merge.plan(was, mine, theirs)
    return was, result, shaped


def settle(world: doc_world.World, ours: dict, base: dict, planned: dict) -> dict:
    """`doc_sync.settle` without the filesystem: read, anchor what is new, and let that
    read be the new file and the new base."""
    live = doc_world.read_ir(world, ours, base)
    tidy = []
    doc_merge.settle_keys(live, planned)
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
        if planned.get(stamp):
            doc_merge.place_pictures(part, planned[stamp])
    doc_ir.attach_latex(live, world.latex())
    _fetch_pictures(live)
    return live


def _fetch_pictures(live: dict) -> None:
    """`doc_sync.fetch_pictures` without the download: a picture the document has and
    the file does not is saved beside the file, so the file can carry it from now on.

    Its `contentUri` lasts about half an hour and names nothing of ours, which is why
    a picture is never known by its URL for long — here the name stands in for the
    bytes, and two pictures with the same object id are the same file.
    """
    for part in doc_ir.parts(live):
        for block in part.get("blocks", []):
            for run in oracle.runs_of(block):
                if run.get("chip") != "image" or run.get("src") or not run.get("value"):
                    continue
                run["src"] = f"media/{run['value']}.png"
                run["sha"] = f"sha-{run['value']}"


def _send(world: doc_world.World, requests: list[dict], seen: Counter) -> dict:
    for request in requests:
        seen["request/" + next(iter(request), "?")] += 1
    return world.apply(requests)


def _report(ours: dict, tabs: dict, written: list[dict]) -> dict:
    """`doc_sync._report`, the same shape, so the oracle reads a real one."""
    info = {"document": "world", "dry_run": False, "requests": 0, "conflicts": [],
            "notes": list(ours.get("unsupported", [])) + tabs["notes"],
            "applied": list(tabs["applied"]), "kept": [], "comments": []}
    for each in written:
        result, label = each["result"], each["label"]
        say = (lambda line, label=label: f"[{label}] {line}") if label is not None else str
        info["requests"] += len(result["requests"]) + len(result.get("structure", []))
        info["conflicts"] += [c | {"tab": label} if label is not None else c
                              for c in result["conflicts"]]
        info["notes"] += [say(n) for n in result["notes"]]
        info["applied"] += [say(t["note"]) for t in each["shaped"]]
        for block in result["blocks"]:
            origin, key = block.get("origin"), block.get("key", "(unkeyed)")
            if block.get("moved") or origin in ("added by the source", "merged"):
                info["applied"].append(say(f"`{key}` {origin}"))
            elif origin:
                info["kept"].append(say(f"`{key}` {origin}"))
    info["requests"] += len(tabs["requests"]) + len(tabs["create"])
    return info


# ---------------------------------------------------------------- what the reader does

def _blocks(part: dict) -> list[dict]:
    return part.get("blocks", [])


def _word_spots(block: dict) -> list[tuple[str, int, int]]:
    """(word, start index, end index) for every word of a paragraph-like block."""
    out, at = [], block.get("span", [1, 1])[0]
    for run in block.get("runs", []):
        width = run.get("width", doc_ir.utf16_len(run.get("text", "")))
        if not run.get("frozen"):
            for match in WORD.finditer(run.get("text", "")):
                low = at + doc_ir.utf16_len(run["text"][:match.start()])
                out.append((match.group(), low, low + doc_ir.utf16_len(match.group())))
        at += width
    return out


def _paragraph_blocks(part: dict) -> list[dict]:
    return [b for b in _blocks(part) if b.get("kind") in doc_ir.TEXT_KINDS
            and b.get("span")]


def _tables(part: dict) -> list[dict]:
    return [b for b in _blocks(part) if b.get("kind") == "table" and b.get("span")]


def _cells(table: dict):
    for r, row in enumerate(table.get("rows", [])):
        for c, cell in enumerate(row):
            if cell:
                yield r, c, cell[0]


def read_type_word(rng, part, tab):
    spots = [s for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    word, low, _ = rng.choice(spots)
    fresh = rng.choice(FRESH)
    return [{"insertText": {"location": _at(low, tab), "text": fresh + " "}}], [fresh]


def read_reword(rng, part, tab):
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (word, low, high) = rng.choice(spots)
    fresh = rng.choice(FRESH)
    # The insert goes in at the end of what it replaces, and the delete after it: text
    # takes the style of the character in front of it (docs/google-docs.md).
    return [{"insertText": {"location": _at(high, tab), "text": fresh}},
            {"deleteContentRange": {"range": _span(low, high, tab)}}], [block.get("key")]


def read_delete_word(rng, part, tab):
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)
             if len(_word_spots(b)) > 1]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    return [{"deleteContentRange": {"range": _span(low, high, tab)}}], [block.get("key")]


def read_append_block(rng, part, tab):
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    fresh = rng.choice(FRESH)
    return [{"insertText": {"location": _at(block["span"][1] - 1, tab),
                            "text": f"\nthe reader wrote {fresh}"}}], [fresh]


def read_delete_block(rng, part, tab):
    blocks = _paragraph_blocks(part)
    if len(blocks) < 2:
        return [], []
    block = rng.choice(blocks)
    low, high = block["span"]
    return [{"deleteContentRange": {"range": _span(low, high, tab)}}], [block.get("key")]


def read_bold_word(rng, part, tab):
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    return [{"updateTextStyle": {"range": _span(low, high, tab),
                                 "textStyle": {"bold": True}, "fields": "bold"}}], \
        [block.get("key")]


def read_heading(rng, part, tab):
    blocks = [b for b in _paragraph_blocks(part) if b.get("kind") != "item"]
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    named = rng.choice(["HEADING_3", "TITLE", "SUBTITLE", "NORMAL_TEXT"])
    return [{"updateParagraphStyle": {
        "range": _span(*block["span"], tab),
        "paragraphStyle": {"namedStyleType": named},
        "fields": "namedStyleType"}}], [block.get("key")]


# What the reader picks in the editor's own menus, written the way Docs writes it.
# Spelled out here rather than taken from `doc_merge`, so the harness is not checking
# the merge against its own idea of a Dimension.
READER_FACES = [
    ({"weightedFontFamily": {"fontFamily": "Georgia", "weight": 400}}, "weightedFontFamily"),
    ({"weightedFontFamily": {"fontFamily": "Courier New", "weight": 400}},
     "weightedFontFamily"),
    ({"fontSize": {"magnitude": 18, "unit": "PT"}}, "fontSize"),
    ({"fontSize": {"magnitude": 8.5, "unit": "PT"}}, "fontSize"),
    ({"smallCaps": True}, "smallCaps"),
    ({"foregroundColor": {"color": {"rgbColor": {"red": 0.1, "green": 0.3, "blue": 0.7}}}},
     "foregroundColor"),
]

READER_MEASURES = [
    ({"indentStart": {"magnitude": 36, "unit": "PT"}}, "indentStart"),
    ({"indentFirstLine": {"magnitude": 18, "unit": "PT"}}, "indentFirstLine"),
    ({"lineSpacing": 200}, "lineSpacing"),
    ({"spaceAbove": {"magnitude": 12, "unit": "PT"}}, "spaceAbove"),
    ({"spaceBelow": {"magnitude": 3, "unit": "PT"}}, "spaceBelow"),
    ({"shading": {"backgroundColor": {"color": {"rgbColor": {
        "red": 1.0, "green": 0.95, "blue": 0.8}}}}}, "shading"),
    ({"alignment": "CENTER"}, "alignment"),
]


def read_face(rng, part, tab):
    """The reader chooses a face, a size or a colour for a word — the case that
    matters most, because `doc_merge.MANAGED` lets a source restyle clear it."""
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    style, field = rng.choice(READER_FACES)
    return [{"updateTextStyle": {"range": _span(low, high, tab),
                                 "textStyle": style, "fields": field}}], [block.get("key")]


def read_measure(rng, part, tab):
    """The reader indents a paragraph, spaces it out or shades it."""
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    style, field = rng.choice(READER_MEASURES)
    return [{"updateParagraphStyle": {"range": _span(*block["span"], tab),
                                      "paragraphStyle": style, "fields": field}}], \
        [block.get("key")]


def read_renumber_list(rng, part, tab):
    items = [b for b in _blocks(part) if b.get("kind") == "item" and b.get("span")]
    if not items:
        return [], []
    low, high = items[0]["span"][0], items[-1]["span"][1]
    return [{"createParagraphBullets": {
        "range": _span(low, high, tab),
        "bulletPreset": doc_world.ORDERED_PRESET}}], [b.get("key") for b in items]


def read_cell_type(rng, part, tab):
    spots = [(t, r, c, b) for t in _tables(part) for r, c, b in _cells(t) if b.get("span")]
    if not spots:
        return [], []
    table, _, _, block = rng.choice(spots)
    fresh = rng.choice(FRESH)
    return [{"insertText": {"location": _at(block["span"][0], tab),
                            "text": fresh + " "}}], [table.get("key"), fresh]


def read_add_row(rng, part, tab):
    tables = _tables(part)
    if not tables:
        return [], []
    table = rng.choice(tables)
    return [{"insertTableRow": {
        "tableCellLocation": {"tableStartLocation": _at(table["span"][0], tab),
                              "rowIndex": 0, "columnIndex": 0},
        "insertBelow": True}}], [table.get("key")]


def read_delete_row(rng, part, tab):
    tables = [t for t in _tables(part) if len(t.get("rows", [])) > 1]
    if not tables:
        return [], []
    table = rng.choice(tables)
    row = rng.randrange(len(table["rows"]))
    return [{"deleteTableRow": {
        "tableCellLocation": {"tableStartLocation": _at(table["span"][0], tab),
                              "rowIndex": row, "columnIndex": 0}}}], [table.get("key")]


def read_insert_picture(rng, part, tab):
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    return [{"insertInlineImage": {"location": _at(block["span"][1] - 1, tab),
                                   "uri": "https://example.invalid/reader.png"}}], \
        [block.get("key")]


def read_insert_chip(rng, part, tab):
    blocks = _paragraph_blocks(part)
    if not blocks:
        return [], []
    block = rng.choice(blocks)
    return [{"insertPerson": {"location": _at(block["span"][1] - 1, tab),
                              "personProperties": {"email": "reader@example.com"}}}], \
        [block.get("key")]


def read_move_block(rng, part, tab):
    """A drag: the reader cuts a block and drops it somewhere else. Its named range
    dies with the cut, which is what a drag really does."""
    blocks = _paragraph_blocks(part)
    if len(blocks) < 3:
        return [], []
    block = rng.choice(blocks[1:])
    target = rng.choice([b for b in blocks if b is not block])
    text = doc_ir.runs_text(block.get("runs", []))
    if not text.strip():
        return [], []
    low, high = block["span"]
    return [[{"deleteContentRange": {"range": _span(low, high, tab)}}],
            [{"insertText": {"location": _at(target["span"][1] - 1, tab),
                             "text": "\n" + text}}]], [block.get("key")]


READER = {
    "type_word": read_type_word, "reword": read_reword, "delete_word": read_delete_word,
    "append_block": read_append_block, "delete_block": read_delete_block,
    "bold_word": read_bold_word, "heading": read_heading,
    "face": read_face, "measure": read_measure,
    "renumber_list": read_renumber_list, "cell_type": read_cell_type,
    "add_row": read_add_row, "delete_row": read_delete_row,
    "insert_picture": read_insert_picture, "insert_chip": read_insert_chip,
    "move_block": read_move_block,
}


def _at(index: int, tab) -> dict:
    return {"index": index} | ({"tabId": tab} if tab else {})


def _span(low: int, high: int, tab) -> dict:
    return {"startIndex": low, "endIndex": high} | ({"tabId": tab} if tab else {})


def apply_reader(world: doc_world.World, name: str, rng: random.Random,
                 seen: Counter) -> list:
    """One reader edit, on a tab drawn at random. A batch the world refuses is the
    harness's own doing — a person in a browser never sends one — so it is dropped and
    counted, never blamed on the sync."""
    tabs = [None] + [t.id for t in world.tabs[1:]]
    tab = rng.choice(tabs)
    part = doc_ir.from_document(world.read(), tab)
    doc_ir.apply_keys(part, doc_ir.named_ranges_of(world.read(), part.get("tab")))
    batches, touched = READER[name](rng, part, tab)
    if not batches:
        seen["reader/" + name + " (nothing to do)"] += 1
        return []
    if isinstance(batches[0], dict):
        batches = [batches]
    for batch in batches:
        try:
            world.apply(batch)
        except Refused:
            seen["reader/" + name + " (refused)"] += 1
            return []
    seen["reader/" + name] += 1
    return [t for t in touched if t]


# ---------------------------------------------------------------- what the source does

def _parts(ir: dict) -> list[dict]:
    return doc_ir.parts(ir)


def _pick(rng, ir, kinds=doc_ir.TEXT_KINDS):
    spots = [(part, i, b) for part in _parts(ir)
             for i, b in enumerate(part.get("blocks", [])) if b.get("kind") in kinds]
    return rng.choice(spots) if spots else None


def src_reword(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot:
        return
    _, _, block = spot
    _swap_word(rng, block)


def _swap_word(rng, block) -> bool:
    runs = [r for r in block.get("runs", []) if not r.get("frozen") and WORD.search(r["text"])]
    if not runs:
        return False
    run = rng.choice(runs)
    spots = list(WORD.finditer(run["text"]))
    match = rng.choice(spots)
    run["text"] = run["text"][:match.start()] + rng.choice(FRESH) + run["text"][match.end():]
    return True


def src_append(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot:
        return
    part, i, _ = spot
    part["blocks"].insert(i + 1, _p(f"the source added {rng.choice(FRESH)}"))


def src_drop(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot or len(spot[0]["blocks"]) < 2:
        return
    part, i, _ = spot
    part["blocks"].pop(i)


def src_move(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot or len(spot[0]["blocks"]) < 3:
        return
    part, i, block = spot
    part["blocks"].pop(i)
    part["blocks"].insert(rng.randrange(len(part["blocks"]) + 1), block)


def _mark_run(rng, block) -> bool:
    """Put one run mark of the dialect on one run of a block."""
    runs = [r for r in block.get("runs", []) if not r.get("frozen") and r["text"].strip()]
    if not runs:
        return False
    run = rng.choice(runs)
    key, value = rng.choice(RUN_MARKS)
    run[key] = value
    if key in ("font", "code"):
        run.pop("code" if key == "font" else "font", None)
    return True


def _mark_paragraph(rng, block) -> bool:
    """Put one paragraph measure of the dialect on a block. These are `SHAPE_KEYS`,
    so they travel by `_take_shape`, not by the run restyler: a different code path
    from `_mark_run` and worth drawing on its own."""
    if block.get("kind") == "table":
        return False
    key, value = rng.choice(PARA_MARKS)
    block[key] = value
    return True


def src_restyle(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot:
        return
    _, _, block = spot
    if rng.random() < 0.35:
        _mark_paragraph(rng, block)
    else:
        _mark_run(rng, block)


def src_retitle(rng, ir, touched):
    """Move a block between the named styles. Every one of Docs' styles is drawn:
    `namedStyleType` is a field the merge names on every paragraph it writes, so a
    style the dialect cannot spell is one it silently writes body text over."""
    spot = _pick(rng, ir, ("paragraph", "heading", "title", "subtitle"))
    if not spot:
        return
    _, _, block = spot
    kinds = [k for k in ("paragraph", "heading", "title", "subtitle")
             if k != block["kind"]]
    block["kind"] = rng.choice(kinds)
    if block["kind"] == "heading":
        block["level"] = rng.randint(1, 3)
    else:
        block.pop("level", None)


def src_add_table(rng, ir, touched):
    part = rng.choice(_parts(ir))
    at = rng.randrange(len(part.get("blocks", [])) + 1)
    part.setdefault("blocks", []).insert(at, _t([["h1", "h2"], [rng.choice(FRESH), "x"]]))


def src_regrid(rng, ir, touched):
    tables = [b for part in _parts(ir) for b in part.get("blocks", [])
              if b.get("kind") == "table"]
    if not tables:
        return
    table = rng.choice(tables)
    if rng.random() < 0.5 or len(table["rows"]) < 2:
        table["rows"].append([[_p(rng.choice(FRESH))] for _ in table["rows"][0]])
    else:
        table["rows"].pop(rng.randrange(len(table["rows"])))


def src_edit_cell(rng, ir, touched):
    cells = [inner for part in _parts(ir) for b in part.get("blocks", [])
             if b.get("kind") == "table" for row in b["rows"] for cell in row
             for inner in cell]
    if not cells:
        return
    cell = rng.choice(cells)
    cell["runs"] = [{"text": rng.choice(FRESH)}]


def src_add_picture(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot:
        return
    part, i, _ = spot
    name = rng.choice(FRESH)
    part["blocks"].insert(i + 1, {"kind": "paragraph", "runs": [
        {"chip": "image", "frozen": True, "text": "",
         "src": f"media/{name}.png", "sha": f"sha-{name}"}]})


def src_add_chip(rng, ir, touched):
    spot = _pick(rng, ir)
    if not spot:
        return
    _, _, block = spot
    block.setdefault("runs", []).append(
        {"chip": "person", "frozen": True, "text": "Grace", "value": "grace@example.com"})


def src_add_tab(rng, ir, touched):
    ir.setdefault("tabs", []).append(
        {"title": f"Tab {rng.choice(FRESH)}",
         "blocks": [_p(f"a tab the source asked for, {rng.choice(FRESH)}")]})


def src_rename_tab(rng, ir, touched):
    extra = ir.get("tabs") or []
    if not extra:
        return
    rng.choice(extra)["title"] = f"Renamed {rng.choice(FRESH)}"


def src_drop_tab(rng, ir, touched):
    extra = ir.get("tabs") or []
    if not extra:
        return
    extra.pop(rng.randrange(len(extra)))


def src_collide(rng, ir, touched):
    """Change exactly what the reader just changed.

    Both sides on one block is what every merge rule is about, and two independent
    random draws almost never land there (CLAUDE.md: 5 text overrides in 200 Slides
    rounds before this op existed, 56 after).
    """
    keys = [k for k in touched if k]
    spots = [(part, i, b) for part in _parts(ir)
             for i, b in enumerate(part.get("blocks", [])) if b.get("key") in keys]
    if not spots:
        return src_reword(rng, ir, touched)
    part, i, block = rng.choice(spots)
    how = rng.choice(["reword", "append", "drop", "cell", "restyle"])
    if how == "drop" and len(part["blocks"]) > 1:
        part["blocks"].pop(i)
    elif how == "append" and block.get("kind") != "table":
        block.setdefault("runs", []).append({"text": f" and {rng.choice(FRESH)}"})
    elif how == "cell" and block.get("kind") == "table":
        cells = [inner for row in block["rows"] for cell in row for inner in cell]
        if cells:
            rng.choice(cells)["runs"] = [{"text": rng.choice(FRESH)}]
    elif how == "restyle" and block.get("kind") != "table":
        if rng.random() < 0.35:
            _mark_paragraph(rng, block)
        else:
            _mark_run(rng, block)
    elif block.get("kind") != "table":
        _swap_word(rng, block)
    elif block.get("kind") == "table":
        cells = [inner for row in block["rows"] for cell in row for inner in cell]
        if cells:
            _swap_word(rng, rng.choice(cells))


SOURCE = {
    "reword": src_reword, "append": src_append, "drop": src_drop, "move": src_move,
    "restyle": src_restyle, "retitle": src_retitle, "add_table": src_add_table,
    "regrid": src_regrid, "edit_cell": src_edit_cell, "add_picture": src_add_picture,
    "add_chip": src_add_chip, "add_tab": src_add_tab, "rename_tab": src_rename_tab,
    "drop_tab": src_drop_tab, "collide": src_collide,
}


# ---------------------------------------------------------------- a round

def draw(seed: int, chain: int, shape: str | None = None) -> dict:
    """The script of a round: which shape, and which ops each step runs on each side.

    Every op carries its own salt, so the shrinker can drop one and leave the rest
    drawing exactly what they drew.
    """
    rng = random.Random(seed)
    script = {"shape": shape or rng.choice(SHAPES), "steps": []}
    names = sorted(READER)
    source_names = sorted(n for n in SOURCE if n != "collide")
    for _ in range(chain):
        reader = [[rng.choice(names), rng.randrange(1 << 30)]
                  for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
        source = [[rng.choice(source_names), rng.randrange(1 << 30)]
                  for _ in range(rng.randint(MIN_EDITS, MAX_EDITS))]
        if rng.random() < COLLIDE_CHANCE:
            source.append(["collide", rng.randrange(1 << 30)])
        script["steps"].append({"reader": reader, "source": source})
    return script


def run_script(script: dict, seen: Counter | None = None) -> list[dict]:
    """One round: push, then (reader edits, source edits, sync, judge) per step."""
    seen = seen if seen is not None else Counter()
    seen["shape/" + script["shape"]] += 1
    world = corpus(script["shape"])
    ours = bootstrap(world)
    base = copy.deepcopy(ours)
    found: list[dict] = []

    for step, edits in enumerate(script["steps"]):
        touched: list = []
        for name, salt in edits["reader"]:
            touched += apply_reader(world, name, random.Random(salt), seen)
        for name, salt in edits["source"]:
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
                f"{refused.why} ({json.dumps(refused.request)[:160]})"))
            return found
        except Exception as err:                       # noqa: BLE001 (the campaign's job)
            found.append(oracle.finding("crash", "loss",
                                        f"step {step}: {type(err).__name__}: {err}"))
            return found
        for conflict in report["conflicts"]:
            seen["conflict/" + str(conflict.get("field", "text"))] += 1
        found += [f | {"detail": f"step {step}: {f['detail']}"}
                  for f in oracle.check(was, before, base, report, mine)]
        found += _settled(world, ours, base, step, seen)
        if found:
            return found
    return found


def _settled(world, ours, base, step, seen) -> list[dict]:
    """A sync that has run must leave nothing to do: file, document and base agree, so
    the next one writes 0 requests (docs/google-docs.md)."""
    spare = copy.deepcopy(world)
    try:
        report, _, _ = sync_once(spare, copy.deepcopy(ours), copy.deepcopy(base))
    except Refused as refused:
        return [oracle.finding("unsettled", "report",
                               f"step {step}: the second sync is refused: {refused.why}")]
    if not report["requests"]:
        seen["settled"] += 1
        return []
    return [oracle.finding(
        "unsettled", "report",
        f"step {step}: a second sync still writes {report['requests']} request(s): "
        f"{'; '.join(report['applied'][:3])}")]


def offline_round(seed: int, chain: int = 1, script: dict | None = None,
                  seen: Counter | None = None) -> list[dict]:
    return run_script(script or draw(seed, chain), seen)


# ---------------------------------------------------------------- shrinking

def shrink(script: dict, rounds: int = 200, still=None) -> dict:
    """Take a failing script apart: drop steps, then ops, keeping every reduction that
    still fails. Each op's salt travels with it, so the rest draw what they drew.

    `still` says what counts as the same failure. A reduction that trades an unknown
    finding for one of the `KNOWN` ones is no reduction at all — it would shrink the
    new defect away and print a reproduction of an old one.
    """
    still = still or oracle.failures
    best = script
    tried = 0
    changed = True
    while changed and tried < rounds:
        changed = False
        for candidate in _reductions(best):
            tried += 1
            if tried > rounds:
                break
            if still(_quiet(candidate)):
                best, changed = candidate, True
                break
    return best


def _reductions(script: dict):
    for i in range(len(script["steps"])):
        if len(script["steps"]) > 1:
            trimmed = copy.deepcopy(script)
            trimmed["steps"].pop(i)
            yield trimmed
    for i, step in enumerate(script["steps"]):
        for side in ("reader", "source"):
            for j in range(len(step[side])):
                trimmed = copy.deepcopy(script)
                trimmed["steps"][i][side].pop(j)
                yield trimmed


def _quiet(script: dict) -> list[dict]:
    try:
        return run_script(script)
    except Exception:                                  # noqa: BLE001
        return [oracle.finding("crash", "loss", "the harness itself raised")]


def describe_script(script: dict) -> str:
    lines = [f"  shape {script['shape']}"]
    for i, step in enumerate(script["steps"]):
        lines.append(f"  step {i}: reader "
                     + ", ".join(f"{n}({s})" for n, s in step["reader"])
                     + " | source " + ", ".join(f"{n}({s})" for n, s in step["source"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- what is known already

# The defects this campaign found and nobody has fixed yet. They are listed so the
# hunt can go past them: a round that ends on one of these is counted, named and let
# through, and only an unknown finding fails the campaign. Each is pinned by an xfail
# in tests/test_doc_fuzz.py with its minimal reproduction, and `--strict` fails on them
# again — which is how one checks a fix, and how a fix is noticed here at all.
#
# A signature is a finding's kind and a piece of its words. It says which *symptom*
# was seen, not which defect caused it: several of these show up as a lost key, and
# the tests, not the signature, say which is which.
KNOWN = (
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
    {"id": "lost-key",
     "kind": "identity_lost", "has": "",
     "why": "a block keeps its words and loses the key the file gave it. Five causes "
            "are fixed and the count is down from 34 to 2: `inherit_keys` "
            "reassigning a key the file itself asserts, `settle` keying every tab "
            "before those tabs are adopted (`doc_merge.settle_keys`), a delete "
            "carrying the style of the block above onto the survivor (the settle "
            "writes the named style back), and a row delete taking with it the first "
            "cell a table is anchored in (`structure` gives a regrid an `after`, so "
            "`anchor_tables` finds it again), and a write changing a block's *shape* "
            "— a delete hands the block after it the shape of the one that went — "
            "so that `adopt_keys`, matching shape and words together, could never "
            "adopt the very blocks a write mangles (`doc_merge._adopt_by_words`). "
            "The 2 that are left have a cause and no fix yet: an empty paragraph is "
            "all mark, so its named range *is* its mark, and a block written in "
            "front of a table goes in as \"\\ntext\" at the mark of the paragraph "
            "before it — which Docs hands to the new paragraph, a range being pushed "
            "along by an insert at its own first index. The empty paragraph's key "
            "rides onto the block that was written and the moved block's key is "
            "nowhere (chain-8 seed 7029, shrunk to `ends_on_table` + two "
            "`add_table`s + a `move`). There is no index that both appends after an "
            "empty paragraph and leaves its range alone, so the write is right and "
            "the read has to be read — and reading it by taking the key off the "
            "block whose words contradict the plan was tried, twice, and cost that "
            "round its convergence"},
    {"id": "crossed-delete",
     "kind": "block_gone", "has": "though the file still names it",
     "why": "two blocks end up under one key and none under the other, so the merge "
            "reads the second as 'the source dropped it', deletes the paragraph the "
            "reader was reading, and writes the source's new wording nowhere. The "
            "same defect as `lost-key`, one step worse — there the block survives "
            "under a wrong key, here it goes altogether (shrunk from chain-8 seed "
            "1031). `inherit_keys` crossing the keys the file asserts was how it got "
            "there, and that is fixed; nothing has reached this signature since, so "
            "the entry stays to catch whatever else can"},
    {"id": "moved-styling",
     "kind": "styling_lost", "has": "",
     "why": "a block the source both reworded and moved is written again from nothing, "
            "and its runs come from `_retext`, which folds a whole stretch into the "
            "first writable run: every mark the reader put on a word inside it goes, "
            "while the report calls the block merged. A move alone keeps them (the "
            "runs are then the document's own), so it takes both to see it"},
)


def known_bug(found: dict) -> str | None:
    """Which known defect this finding is a symptom of, if any."""
    for bug in KNOWN:
        if found["kind"] == bug["kind"] and bug["has"] in found["detail"]:
            return bug["id"]
    return None


def triage(found: list[dict]) -> tuple[list[dict], list[str]]:
    """The failures nobody knows about yet, and the ids of the known ones."""
    unknown, known = [], []
    for one in oracle.failures(found):
        bug = known_bug(one)
        (known.append(bug) if bug else unknown.append(one))
    return unknown, known


# ---------------------------------------------------------------- the campaign

def run_offline(rounds: int, seed: int = 0, chain: int = 1, do_shrink: bool = True,
                shape: str | None = None, strict: bool = False) -> int:
    seen: Counter = Counter()
    started, bad = time.time(), 0
    hit: Counter = Counter()
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
            small = shrink(script, still=(lambda f: triage(f)[0]) if unknown else None)
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


def known_seen(hit: Counter) -> str:
    """What the round hit of what is known already. A known defect nobody reaches any
    more is worth saying out loud: either it is fixed, or the campaign stopped looking."""
    lines = ["known defects (let through; --strict fails on them):"]
    for bug in KNOWN:
        lines.append(f"  {bug['id']}: {hit.get(bug['id'], 0)}"
                     + ("" if hit.get(bug["id"]) else "   (not reached in this run)"))
    return "\n".join(lines)


def coverage(seen: Counter) -> str:
    """What the campaign actually reached. A campaign that never plans an insertTable
    proves nothing about tables, and only counting says so."""
    groups: dict = {}
    for key, count in seen.items():
        head, _, rest = key.partition("/")
        groups.setdefault(head, Counter())[rest or head] = count
    lines = ["coverage:"]
    for head in sorted(groups):
        items = ", ".join(f"{k} {v}" for k, v in sorted(groups[head].items()))
        lines.append(f"  {head}: {items}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    if args.replay is not None:
        script = draw(args.replay, args.chain, args.shape)
        found = offline_round(args.replay, args.chain, script)
        print(describe_script(script))
        print(oracle.describe(found))
        unknown, known = triage(found)
        for bug in known:
            print(f"  known: {bug}")
        if oracle.failures(found) and not args.no_shrink:
            small = shrink(script, still=(lambda f: triage(f)[0]) if unknown else None)
            print("shrunk to:")
            print(describe_script(small))
            print(oracle.describe(_quiet(small)))
        return 1 if unknown or (args.strict and known) else 0
    return run_offline(args.rounds, args.seed, args.chain, not args.no_shrink,
                       args.shape, args.strict)


if __name__ == "__main__":
    sys.exit(main())
