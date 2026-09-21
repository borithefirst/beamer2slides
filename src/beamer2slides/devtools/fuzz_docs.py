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
from difflib import SequenceMatcher

from .. import doc_ir, doc_merge
from . import doc_loss_oracle as oracle
from . import doc_world
from .doc_world import Refused

MIN_EDITS, MAX_EDITS = 1, 4
COLLIDE_CHANCE = 0.45
WORD = re.compile(r"\w+")
MARK_KEYS = {key for key, _ in doc_ir.MARK_FIELDS}

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
             ("script", "super"), ("script", "sub"),
             ("color", "#993333"), ("highlight", "#ffee88")]

PARA_MARKS = [("align", "center"), ("align", "justify"), ("indent", 18.0),
              ("indent_first", 36.0), ("line_spacing", 1.5), ("shading", "#eef2ff"),
              ("space_above", 6.0), ("space_below", 12.0),
              ("border_bottom", "1pt solid #333333"),
              ("border_bottom", "2.5pt dashed #cc0000 pad 4pt"),
              ("border_left", "3pt dotted #0000ff"),
              ("page_break", True), ("keep_with_next", True)]


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
        # A document with a look of its own (`THEME`). Its headings say nothing about
        # their own alignment: the centring is the theme's, and a paragraph reports
        # only what is set on it, so the file cannot say it and must not undo it.
        "themed": [_h("A themed heading"), _p("Under it, a paragraph."),
                   _h("Another heading"), _p("And prose after that."),
                   _p("A line the source can move.", align="center")],
    }


#: The named styles the `themed` shape's world carries. No request writes one — the
#: API has none — so this is fixed for the life of a round, and every difference it
#: makes is a difference in what a paragraph *inherits*.
THEME = {"HEADING_1": {"paragraphStyle": {"alignment": "CENTER"},
                       "textStyle": {"bold": True}}}


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
        world.theme = {name: dict(style) for name, style in THEME.items()}
    return world


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
    """Every block keyed and named in the document, and a file that is the document's
    own read.

    That is `docs adopt`, not `docs push`, and deliberately: `push` would import HTML
    and get back only what an import can carry, while the corpus shapes hold chips,
    equations, dropdowns and a table of contents that no import can make. Starting
    from the document means the campaign measures the journey somebody actually has —
    a document written in the browser for a year, adopted, edited in the file, synced
    back — rather than one this tool made out of its own dialect.
    """
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
    if tabs["rename"]:
        world.title = tabs["rename"]   # Drive's, not a request (`doc_sync.rename_document`)
    pairs = list(tabs["pairs"])
    known = {p.get("tab") for p in doc_ir.parts(theirs)} - {None}
    siblings, made = doc_merge.tab_siblings(theirs), {}
    for part in tabs["create"]:
        if part.get("parent") in made:
            part["parent"] = made[part["parent"]]
        request = doc_merge.add_tab_request(part, known | set(made.values()),
                                            ours, siblings)
        reply = _send(world, [request], seen)
        tab = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        if part.get("tab"):
            made[part["tab"]] = tab
        part["tab"] = tab
        props = request["addDocumentTab"]["tabProperties"]
        siblings.setdefault(props.get("parentTabId"), []).insert(props["index"], tab)
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
        found = doc_merge.recover_tables(was, theirs)
        anchored = doc_merge.anchor_tables(theirs, result["shaped"])
        anchored += doc_merge.recover_swallowed(theirs, result["shaped"])
        if anchored or found:
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


def read_split_block(rng, part, tab):
    """The reader presses Enter in the middle of a paragraph.

    The commonest editing action there is, and one nothing else here draws. A named
    range is half-open, so the newline typed inside it grows it: one `b2s:` range now
    spans two paragraphs, and `doc_ir.apply_keys` gives the key to the first of them
    (where the range begins, and so where the words it was given to still are). The
    second half is a block nobody has ever seen, which the settle must key and name.
    """
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)
             if s[1] > b["span"][0]]
    if not spots:
        return [], []
    block, (_, low, _) = rng.choice(spots)
    return [{"insertText": {"location": _at(low, tab), "text": "\n"}}], [block.get("key")]


def read_join_blocks(rng, part, tab):
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
             if all(b.get("kind") in doc_ir.TEXT_KINDS and b.get("span")
                    for b in blocks[i:i + 2])]
    if not pairs:
        return [], []
    first, second = rng.choice(pairs)
    mark = first["span"][1] - 1
    return [{"deleteContentRange": {"range": _span(mark, mark + 1, tab)}}], \
        [first.get("key"), second.get("key")]


def read_paste_block(rng, part, tab):
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
    return [{"insertText": {"location": _at(target["span"][1] - 1, tab),
                            "text": "\n" + text}}], [block.get("key")]


def read_bold_word(rng, part, tab):
    spots = [(b, s) for b in _paragraph_blocks(part) for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    return [{"updateTextStyle": {"range": _span(low, high, tab),
                                 "textStyle": {"bold": True}, "fields": "bold"}}], \
        [block.get("key")]


def read_unmark_word(rng, part, tab, theme=None):
    """A reader pressing Ctrl+B on a word a *theme* made bold.

    This is the run-level twin of the alignment loss: a mark turned off is a run
    saying `bold: false`, which reads back as itself, and a file that could only say
    "bold" or nothing would hand the word back to the theme on the first source
    restyle. Headings first, since that is where a theme's marks live; on a shape
    with no theme the request is written all the same and means nothing, which is
    also what the document says about it.
    """
    blocks = [b for b in _paragraph_blocks(part) if b.get("kind") == "heading"] \
        or _paragraph_blocks(part)
    spots = [(b, s) for b in blocks for s in _word_spots(b)]
    if not spots:
        return [], []
    block, (_, low, high) = rng.choice(spots)
    # A mark the theme actually puts on this block, when there is one: turning off a
    # mark nothing puts on is a request that means nothing, and a campaign made of
    # those would say it had drawn this and proved nothing by it.
    wears = sorted((theme or {}).get(doc_merge.named_style(block), set())
                   & {key for key, _ in doc_ir.MARK_FIELDS})
    key = rng.choice(wears) if wears else rng.choice(doc_ir.MARK_FIELDS)[0]
    api = dict(doc_ir.MARK_FIELDS)[key]
    return [{"updateTextStyle": {"range": _span(low, high, tab),
                                 "textStyle": {api: False}, "fields": api}}], \
        [block.get("key")]


read_unmark_word.wants_theme = True


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
    ({"baselineOffset": "SUPERSCRIPT"}, "baselineOffset"),
    ({"baselineOffset": "SUBSCRIPT"}, "baselineOffset"),
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


def read_add_column(rng, part, tab):
    """The reader adds a column. Rows and columns are not the same thing to the merge:
    a row is one line of the grid, a column is a cell taken out of *every* row, matched
    by its words across the whole table (`doc_merge._column_score`) rather than by its
    place. That half of `_table_lines` had never been drawn — the campaign planned
    `insertTableRow` and `deleteTableRow` and no column request at all."""
    tables = _tables(part)
    if not tables:
        return [], []
    table = rng.choice(tables)
    return [{"insertTableColumn": {
        "tableCellLocation": {"tableStartLocation": _at(table["span"][0], tab),
                              "rowIndex": 0, "columnIndex": 0},
        "insertRight": True}}], [table.get("key")]


def read_delete_column(rng, part, tab):
    tables = [t for t in _tables(part) if len(t.get("rows", [[]])[0]) > 1]
    if not tables:
        return [], []
    table = rng.choice(tables)
    column = rng.randrange(len(table["rows"][0]))
    return [{"deleteTableColumn": {
        "tableCellLocation": {"tableStartLocation": _at(table["span"][0], tab),
                              "rowIndex": 0, "columnIndex": column}}}], [table.get("key")]


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
    low, high = block["span"]
    at = target["span"][1] - 1
    if at >= high:                     # the cut is in front of where it is dropped
        at -= high - low
    return [[{"deleteContentRange": {"range": _span(low, high, tab)}}],
            [{"insertText": {"location": _at(at, tab), "text": "\n" + text}}]], \
        [block.get("key")]


def read_rename_tab(rng, part, tab):
    """The reader renames the tab they are looking at, in the tab strip. The first
    tab's id is not `tab` (a location in it carries none) but the part's own."""
    ident = tab or part.get("tab")
    if not ident:
        return [], []
    return [{"updateDocumentTabProperties": {
        "tabProperties": {"tabId": ident, "title": f"Reader's {rng.choice(FRESH)}"},
        "fields": "title"}}], []


def read_add_tab(rng, part, tab):
    """The reader clicks + in the tab strip. Docs makes it with one empty paragraph
    and hands back its id; nothing here types into it, since a later step's ops draw
    a tab at random and will reach this one.

    Nobody knows of such a tab: it is in neither the file nor the base, so the merge
    must leave it alone and the settle must read it into the file — keys, named
    ranges and all — or the sync after it will see a tab the file never had."""
    return [{"addDocumentTab": {"tabProperties": {
        "title": f"Reader's {rng.choice(FRESH)}"}}}], []


def read_drop_tab(rng, part, tab, tabs):
    """The reader deletes a tab in the tab strip. Never the first: that one is the
    body and Docs refuses it (so does the world). What has to follow the tab is its
    base entry and its `<section>` in the file."""
    if not tabs:
        return [], []
    return [{"deleteTab": {"tabId": rng.choice(tabs)}}], []


read_drop_tab.wants_tabs = True


READER = {
    "type_word": read_type_word, "reword": read_reword, "delete_word": read_delete_word,
    "append_block": read_append_block, "delete_block": read_delete_block,
    "split_block": read_split_block, "join_blocks": read_join_blocks,
    "paste_block": read_paste_block,
    "bold_word": read_bold_word, "unmark_word": read_unmark_word,
    "heading": read_heading,
    "face": read_face, "measure": read_measure,
    "renumber_list": read_renumber_list, "cell_type": read_cell_type,
    "add_row": read_add_row, "delete_row": read_delete_row,
    "add_column": read_add_column, "delete_column": read_delete_column,
    "insert_picture": read_insert_picture, "insert_chip": read_insert_chip,
    "move_block": read_move_block, "rename_tab": read_rename_tab,
    "add_tab": read_add_tab, "drop_tab": read_drop_tab,
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
    op = READER[name]
    # An op that turns styling *off* has to know what the theme turns on, and only
    # that one does; the rest are a reader typing, who knows nothing of the sort.
    wants = {"theme": theme_fields(world)} if getattr(op, "wants_theme", False) else {}
    # And one op is about the tab strip rather than about a tab: which tabs there are
    # to delete is not a thing the part it is looking at can say.
    if getattr(op, "wants_tabs", False):
        wants["tabs"] = [t.id for t in world.tabs[1:]]
    batches, touched = op(rng, part, tab, **wants)
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
    # Columns as well as rows, and the merge treats the two quite differently: a
    # column is matched across the table by its words, a row by its cells in the
    # columns that matched. Drawing rows alone left `_column_score` and the whole
    # column half of `_table_lines` unreached.
    if rng.random() < 0.5:
        if rng.random() < 0.5 or len(table["rows"]) < 2:
            table["rows"].append([[_p(rng.choice(FRESH))] for _ in table["rows"][0]])
        else:
            table["rows"].pop(rng.randrange(len(table["rows"])))
    elif rng.random() < 0.5 or len(table["rows"][0]) < 2:
        for row in table["rows"]:
            row.append([_p(rng.choice(FRESH))])
    else:
        column = rng.randrange(len(table["rows"][0]))
        for row in table["rows"]:
            row.pop(column)


def src_edit_cell(rng, ir, touched):
    cells = [inner for part in _parts(ir) for b in part.get("blocks", [])
             if b.get("kind") == "table" for row in b["rows"] for cell in row
             for inner in cell]
    if not cells:
        return
    cell = rng.choice(cells)
    # Distinctive, not a word out of `FRESH`: a cell edit nobody can tell from the
    # cell beside it is one no judge can follow to where it landed, and following it
    # is the whole of `_arrived`. Real sources write words of their own too.
    cell["runs"] = [{"text": f"{rng.choice(FRESH)}-{rng.randrange(1 << 20):05x}"}]


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
    """The first tab is drawn too: it names itself in the file's `b2s-tab` meta and
    nowhere else, and it is the tab everybody is actually looking at."""
    extra = ir.get("tabs") or []
    if not extra or rng.random() < 0.4:
        ir["tab_title"] = f"Renamed {rng.choice(FRESH)}"
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


#: A named style's `paragraphStyle` keys, by the IR field each one is.
THEME_FIELDS = {"alignment": "align"} | {api: key for key, api in doc_merge.PARAGRAPH_FIELDS}


def theme_fields(world: doc_world.World) -> dict:
    """What the world's theme sets, as `doc_loss_oracle.check` wants it: the IR fields
    per named style. Nothing in a read says a paragraph *inherits* — only that it sets
    nothing itself — so the oracle has to be told what there was to inherit.

    A named style's run marks are in here too, for `read_unmark_word`; the oracle asks
    its question of a block's own fields, where a run mark is never one, so naming
    them costs it nothing.
    """
    return {name: {THEME_FIELDS[api] for api in (style.get("paragraphStyle") or {})
                   if api in THEME_FIELDS}
            | {key for key, api in doc_ir.MARK_FIELDS
               if (style.get("textStyle") or {}).get(api)}
            for name, style in (world.theme or {}).items()}


def theme_values(world: doc_world.World) -> dict:
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
                  for f in oracle.check(was, before, base, report, mine,
                                        theme=theme_fields(world))]
        found += _arrived(was, before, mine, ours, report, step, seen,
                          theme_values(world))
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


def _grid(block: dict | None) -> tuple[int, int] | None:
    """A table's shape, or None when it is not a rectangular table — a ragged one
    (merged cells) is a thing the merge reports rather than writes."""
    rows = (block or {}).get("rows")
    if (block or {}).get("kind") != "table" or not rows:
        return None
    widths = {len(row) for row in rows}
    return (len(rows), widths.pop()) if len(widths) == 1 else None


def _arrived(was: dict, before: dict, mine: dict, after: dict, report: dict,
             step: int, seen: Counter, theme: dict | None = None) -> list[dict]:
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
    out = []
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
                                        step, seen, theme or {})
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
                tab=tab, key=key))
        out += _order_arrived(part, sides[1][tab], sides[2][tab], sides[3][tab],
                              said, tab, step, seen)
        out += _existence_arrived(part, sides[1][tab], sides[2][tab], sides[3][tab],
                                  said, tab, step, seen)
    return out


def _block_keys(part: dict) -> list[str]:
    return [b["key"] for b in part.get("blocks", []) if b.get("key")]


def _wordless(part: dict) -> set:
    """The keys of the blocks of this part that say nothing — a table with no words
    in it among them, since `doc_ir.key_blocks` names that one `table:empty`."""
    return {b["key"] for b in part.get("blocks", [])
            if b.get("key") and not _says(b)[0].strip()}


def _order_arrived(was_p, doc_p, src_p, end_p, said, tab, step,
                   seen: Counter) -> list[dict]:
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
        tab=tab, key=first)]


def _existence_arrived(was_p, doc_p, src_p, end_p, said, tab, step,
                       seen: Counter) -> list[dict]:
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
    out = []
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
            f"the report says nothing", tab=tab, key=key))
    for block in src_p.get("blocks", []):
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
            f"says nothing", tab=tab, key=key))
    return out


def _saying(part: dict, text: str) -> int:
    """How many blocks of the part say exactly this text."""
    return sum(1 for b in part.get("blocks", []) if _says(b)[0] == text)


def _words_arrived(key, block, here, file_b, then, said, tab, step,
                   seen: Counter) -> list[dict]:
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
        f"{end[0]!r} and the report says nothing", tab=tab, key=key)]


def _styling_arrived(key, block, here, file_b, then, said, tab, step,
                     seen: Counter, theme: dict) -> list[dict]:
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
    out = []
    for what, kind, now, want, got in (
            ("runs", "restyle_lost", was_look[0], want_look[0], got_look[0]),
            ("shape", "reshape_lost", was_look[1], want_look[1], got_look[1])):
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
            f"document at {got} with the report saying nothing", tab=tab, key=key))
    return out


def _worn(block: dict, theme: dict) -> tuple:
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
    puts_on = set(theme.get(doc_merge.named_style(block), {}).get("marks") or ())
    out: list[list] = []
    for run in block.get("runs", []):
        if run.get("frozen"):
            continue
        style = dict(run)
        if style.pop("code", None) and not style.get("font"):
            style["font"] = doc_ir.CODE_FAMILY
        marks = tuple(sorted((k, _flat(v)) for k, v in style.items()
                             if k not in ("text", "width")
                             and not (k in MARK_KEYS and bool(v) == (k in puts_on))))
        if out and out[-1][0] == marks:
            out[-1][1] += run.get("text", "")
        else:
            out.append([marks, run.get("text", "")])
    return tuple((marks, text) for marks, text in out if text)


def _flat(value):
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value


def _shape(block: dict, theme: dict) -> tuple:
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
    gives = theme.get(name, {})
    out = {}
    for field in fields:
        value = block.get(field)
        # A theme that sets nothing aligns left, which is what the API's own default
        # is: the two spellings of that are one thing.
        inherited = gives.get(field) or ("left" if field == "align" else None)
        out[field] = None if value == inherited else value
    return (name,) + tuple(sorted((k, v) for k, v in out.items() if v is not None))


def _says(block: dict) -> tuple:
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


def _cells_arrived(key, block, here, file_b, then, said, tab, step,
                   seen: Counter) -> list[dict]:
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
    base_cells, doc_cells = (Counter(oracle.cells_of(b).values())
                             for b in (block, here))
    if doc_cells - base_cells:
        return []                     # the reader wrote in it: the merge decides
    was_cells, file_cells = (oracle.cells_of(b) for b in (block, file_b))
    after = Counter(oracle.cells_of(then).values())
    out = []
    for at, text in file_cells.items():
        old = was_cells.get(at)
        if text == old or base_cells[text] or doc_cells[old] < base_cells[old]:
            continue
        seen["arrival/cell asked"] += 1
        if after[text] or oracle._named(said, key):
            continue
        seen["arrival/cell missed"] += 1
        out.append(oracle.finding(
            "cell_lost", "loss",
            f"step {step}: the source rewrote the cell at {at} of the table {key!r} "
            f"to {text!r}, the reader wrote nothing in that table, and no cell of it "
            f"says so when the sync is over", tab=tab, key=key))
    return out


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
    if not KNOWN:
        return ("known defects: none outstanding — every finding fails the campaign, "
                "and --strict has nothing more to add.")
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
