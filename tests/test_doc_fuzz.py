"""Fuzzing the Google Docs sync against its loss oracle: does a sync lose a reader's work?

Three parts, all offline and all fast (no Google call anywhere in this file).

1. **The campaign on fixed seeds.** `beamer2slides.devtools.fuzz_docs` builds a document
   from the corpus, edits it as a reader would (`doc_world` applies the real requests
   under Docs' index rules), changes the canonical file as an author would, runs the real
   `doc_merge.plan` and the real settle, and hands the two read-backs, the base and the
   report to `doc_loss_oracle`. A round is clean when the oracle finds nothing the report
   does not name, and when a second sync writes nothing. Defects nobody has fixed yet are
   listed in `fuzz_docs.KNOWN`: a round that ends on one of those is counted and let
   through, and each of them is pinned by an xfail below.

2. **The oracle is not vacuous.** A clean round is taken apart again with a loss put in on
   purpose — a block the reader added deleted, a word swallowed, styling dropped, a chip
   gone, a cell overwritten, the reader's order undone, a whole tab removed — and the
   oracle has to catch every one of them, and to stay silent when the report owns up to
   the same thing. An oracle nobody has tried to fool proves nothing.

3. **One xfail per defect the campaign found**, each a hand-written scenario rather than a
   seed, so it says what is wrong rather than which dice fell. `--strict` on the campaign
   (`python -m beamer2slides.devtools.fuzz_docs --strict`) is the other half of that: when
   one of these is fixed, both the xfail and the KNOWN entry have to go.

The campaign itself, past these seeds:
    python -m beamer2slides.devtools.fuzz_docs offline --rounds 300
    python -m beamer2slides.devtools.fuzz_docs offline --rounds 400 --chain 4
"""

import copy
import os

import pytest

from beamer2slides import doc_ir, doc_merge
from beamer2slides.devtools import doc_loss_oracle as oracle
from beamer2slides.devtools import doc_world, fuzz_docs

# One round is a few milliseconds at chain 1 and about 30 at chain 4, so the default run
# stays well under a second. The campaign proper runs hundreds.
ROUNDS = int(os.environ.get("B2S_DOC_FUZZ_ROUNDS", "30"))
CHAINED = (0, 1, 2, 3, 4, 5)
# Seeds that once failed and are kept as regressions: 60 a block the source renamed read
# as a block gone, 181 a cell judged by its place after the reader inserted a row above
# it, 309 (chain 4) one of two identical person chips counted as lost, 1031 and 1147
# (chain 8) the two defects the chained campaign found last, 5099/5167 (chain 8) the
# world not moving a named range when a table row went, 5130 (chain 8) the oracle
# accusing a token both sides had edited half of.
REGRESSIONS = ((60, 1), (181, 1), (309, 4), (1031, 8), (1147, 8),
               (5099, 8), (5130, 8), (5167, 8))


def _round(seed: int, chain: int) -> None:
    found = fuzz_docs.offline_round(seed, chain)
    unknown, known = fuzz_docs.triage(found)
    if unknown:
        small = fuzz_docs.shrink(fuzz_docs.draw(seed, chain),
                                 still=lambda f: fuzz_docs.triage(f)[0])
        pytest.fail(f"seed {seed} (chain {chain}) lost something no one knows about\n"
                    f"{oracle.describe(unknown)}\n{fuzz_docs.describe_script(small)}")


@pytest.mark.parametrize("seed", range(ROUNDS))
def test_a_round_of_the_campaign_loses_nothing(seed):
    _round(seed, 1)


@pytest.mark.parametrize("seed", CHAINED)
def test_a_chained_round_loses_nothing(seed):
    """Four syncs on one document, each on what the last one left: the base, the keys and
    the named ranges have to survive being written again and again."""
    _round(seed, 4)


@pytest.mark.parametrize("seed,chain", REGRESSIONS)
def test_a_seed_that_once_failed_still_passes(seed, chain):
    _round(seed, chain)


def test_the_campaign_reaches_what_it_claims_to_reach():
    """Coverage is the campaign's own honesty check: a run that never plans an
    `insertTable` proves nothing about tables (`fuzz_docs.coverage`)."""
    from collections import Counter

    seen: Counter = Counter()
    for seed in range(ROUNDS):
        fuzz_docs.offline_round(seed, 1, seen=seen)
    for request in ("insertText", "deleteContentRange", "insertTable", "insertPerson",
                    "insertInlineImage", "updateTextStyle", "updateParagraphStyle"):
        assert seen[f"request/{request}"], f"{ROUNDS} rounds never sent {request}"
    assert len({k for k in seen if k.startswith("shape/")}) >= 8
    assert seen["settled"] >= ROUNDS - 5, "most rounds must end with nothing left to do"


# ---------------------------------------------------------------- the oracle's own tests

def _p(key, text, **kw):
    return {"key": key, "kind": "paragraph", "runs": [{"text": text}], **kw}


def _ir(*blocks, tabs=()):
    out = {"blocks": list(blocks)}
    if tabs:
        out["tabs"] = [dict(t) for t in tabs]
    return out


def _kinds(found):
    return {f["kind"] for f in oracle.failures(found)}


NOTHING = {"conflicts": [], "notes": [], "applied": []}


def test_the_oracle_sees_a_block_the_reader_added_disappear():
    base = _ir(_p("k1", "Kept."))
    before = _ir(_p("k1", "Kept."), {"kind": "paragraph",
                                     "runs": [{"text": "Typed by the reader."}]})
    after = _ir(_p("k1", "Kept."))
    assert _kinds(oracle.check(base, before, after, NOTHING)) == {"block_gone"}
    said = {"conflicts": [], "notes": ["Typed by the reader. — left out, see the report"]}
    assert not oracle.failures(oracle.check(base, before, after, said))


def test_the_oracle_sees_a_word_the_reader_typed_swallowed():
    base = _ir(_p("k1", "The line."))
    before = _ir(_p("k1", "The willow line."))
    after = _ir(_p("k1", "The line."))
    assert _kinds(oracle.check(base, before, after, NOTHING)) == {"words_lost"}
    said = {"conflicts": [{"key": "k1", "field": "text"}], "notes": []}
    assert not oracle.failures(oracle.check(base, before, after, said))


def test_the_oracle_sees_styling_the_reader_put_on_a_word_dropped():
    base = _ir(_p("k1", "one two"))
    before = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "one "}, {"text": "two", "bold": True}]})
    after = _ir(_p("k1", "one two"))
    assert _kinds(oracle.check(base, before, after, NOTHING)) == {"styling_lost"}


def test_the_oracle_sees_a_chip_the_reader_inserted_disappear():
    chip = {"frozen": True, "chip": "person", "text": "Ada", "value": "ada@example.com"}
    base = _ir(_p("k1", "ask "))
    before = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "ask "}, chip]})
    after = _ir(_p("k1", "ask "))
    assert "frozen_gone" in _kinds(oracle.check(base, before, after, NOTHING))


def test_the_oracle_sees_a_cell_the_reader_typed_in_overwritten():
    def table(second):
        return {"key": "t1", "kind": "table",
                "rows": [[[_p(None, "a")], [_p(None, "b")]],
                         [[_p(None, "1")], [_p(None, second)]]]}

    base, before, after = _ir(table("2")), _ir(table("2 willow")), _ir(table("2"))
    assert "cell_words_lost" in _kinds(oracle.check(base, before, after, NOTHING))


def test_the_oracle_sees_the_readers_order_undone():
    base = _ir(_p("k1", "One."), _p("k2", "Two."))
    before = _ir(_p("k2", "Two."), _p("k1", "One."))
    after = _ir(_p("k1", "One."), _p("k2", "Two."))
    found = oracle.check(base, before, after, NOTHING)
    assert _kinds(found) == {"order_undone"}
    assert not oracle.failures(oracle.check(base, before, before, NOTHING))


def test_the_oracle_sees_a_tab_the_reader_wrote_in_disappear():
    tab = {"tab": "t.1", "title": "Appendix", "blocks": [_p("k2", "Tab words.")]}
    base = _ir(_p("k1", "Body."), tabs=[dict(tab, blocks=[_p("k2", "Tab words.")])])
    before = _ir(_p("k1", "Body."),
                 tabs=[dict(tab, blocks=[_p("k2", "Tab words and the reader's.")])])
    after = _ir(_p("k1", "Body."))
    ours = _ir(_p("k1", "Body."), tabs=[tab])        # the file still asks for the tab
    assert _kinds(oracle.check(base, before, after, NOTHING, ours)) == {"tab_gone"}
    # A tab the source dropped and the reader left alone is not a loss.
    quiet = _ir(_p("k1", "Body."), tabs=[dict(tab, blocks=[_p("k2", "Tab words.")])])
    assert not oracle.failures(
        oracle.check(base, quiet, after, NOTHING, _ir(_p("k1", "Body."))))


def test_the_oracle_catches_a_loss_put_into_a_real_round():
    """The same judgement on the campaign's own data, not on dicts written by hand: a
    round that passes is made to fail by taking the reader's block out of the settled
    read, which is exactly what a sync that deletes too much would leave behind."""
    world, ours, base = _push("prose")
    part = doc_world.read_ir(world, ours, base)
    block = [b for b in part["blocks"] if b.get("key") == "paragraph:a-closing-line"][0]
    world.apply([{"insertText": {"location": {"index": block["span"][0]},
                                 "text": "willow "}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))

    hurt = copy.deepcopy(base)
    hurt["blocks"] = [b for b in hurt["blocks"] if b.get("key") != "paragraph:a-closing-line"]
    assert _kinds(oracle.check(was, before, hurt, report, mine)) == {"block_gone"}


def test_the_oracle_lets_a_token_both_sides_edited_half_of_alone():
    """`WORD` is `\\S+`, so a soft hyphen makes one token out of two words and each side
    can rewrite one half. The merge is then right to write a token neither side typed,
    and the reader's half is in it (chain-8 seed 5130). What must survive is only what
    the reader added — the other half is the source's to change."""
    base = _ir(_p("k1", "a soft­hyphen here"))
    before = _ir(_p("k1", "a quartz­hyphen here"))          # the reader took "soft"
    after = _ir(_p("k1", "a quartz­zephyr here"))           # the source took "hyphen"
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # And the reader's own half going is still a loss.
    gone = _ir(_p("k1", "a soft­zephyr here"))
    assert _kinds(oracle.check(base, before, gone, NOTHING)) == {"words_lost"}


# ---------------------------------------------------------------- the world is the rules

def test_deleting_a_table_row_moves_the_named_ranges_below_it():
    """A row's content leaves the document, so every index after it moves up and Google
    moves the anchors with it. The world shifted by 0, so after a source regrid every key
    below the table slid onto the block above (chain-8 seeds 5099 and 5167)."""
    def said(ir):
        return [(b.get("key"), "".join(r.get("text", "") for r in b.get("runs", [])))
                for b in ir["blocks"] if b.get("kind") != "table"]

    # Short blocks under the table, so a row's worth of units is more than one of them:
    # that is what makes a key land on the wrong block rather than merely on the right
    # one's wrong end.
    world = doc_world.build([{"blocks": [
        fuzz_docs._t([["k", "v"], ["a", "1"], ["b", "2"]]),
        fuzz_docs._p("one"), fuzz_docs._p("two"), fuzz_docs._p("six"),
        fuzz_docs._p("ten"), fuzz_docs._p("end")]}], title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    was = said(doc_world.read_ir(world, ours, base))
    assert was and all(key for key, _ in was)
    table = [b for b in ours["blocks"] if b.get("kind") == "table"][0]

    world.apply([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": table["span"][0]}, "rowIndex": 1}}}])
    assert said(doc_world.read_ir(world, ours, base)) == was


# ---------------------------------------------------------------- what is still broken

def _push(shape: str):
    """`docs push` on a corpus shape: the world, the file and the base it leaves."""
    world = fuzz_docs.corpus(shape)
    ours = fuzz_docs.bootstrap(world)
    return world, ours, copy.deepcopy(ours)


def _build(blocks: list[dict]):
    world = doc_world.build([{"blocks": blocks}], title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    return world, ours, copy.deepcopy(ours)


def _para(text):
    return {"kind": "paragraph", "runs": [{"text": text}]}


def _table(rows):
    return {"kind": "table", "rows": [[[_para(cell)] for cell in row] for row in rows]}


def _keys(ir):
    return [b.get("key") for b in ir["blocks"]]


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'toc-block': every test for one "
                   "of these reads kind == 'table', so a table of contents is an "
                   "ordinary block to the planner and the delete takes the newline in "
                   "front of it — which Docs refuses, and a refusal throws out the "
                   "whole batch")
def test_a_block_in_front_of_a_table_of_contents_can_be_deleted():
    world, ours, base = _build([_para("First line."), {"kind": "toc"}, _para("After it.")])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "paragraph:first-line"]
    fuzz_docs.sync_once(world, ours, base)


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'toc-block': the same, writing "
                   "rather than deleting — the block goes in at the TOC's own index, "
                   "where nothing can be inserted")
def test_a_block_can_be_written_in_front_of_a_table_of_contents():
    world, ours, base = _build([_para("First line."), {"kind": "toc"}, _para("After it.")])
    ours["blocks"].insert(1, _para("A new line."))
    fuzz_docs.sync_once(world, ours, base)


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'empty-delete': an empty "
                   "paragraph between two tables has no mark to give up — its own is "
                   "the one in front of a table and the block before it is a table — so "
                   "`_delete_range` returns a range of length 0 and Docs refuses it")
def test_an_empty_paragraph_between_two_tables_can_be_deleted():
    world, ours, base = _build([_table([["a", "b"]]), _para(""),
                                _table([["c", "d"]]), _para("The end.")])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "paragraph:empty"]
    fuzz_docs.sync_once(world, ours, base)


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'dropped-table': the 'edited in "
                   "the document' test at doc_merge.py:469 uses block_text, which is "
                   "empty for a table, so the reader's cells count for nothing")
def test_a_table_the_reader_typed_in_survives_the_source_dropping_it():
    world, ours, base = _push("ends_on_table")
    part = doc_world.read_ir(world, ours, base)
    table = [b for b in part["blocks"] if b["kind"] == "table"][0]
    cell = table["rows"][1][1][0]
    world.apply([{"insertText": {"location": {"index": cell["span"][0]},
                                 "text": "willow "}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"] = [b for b in ours["blocks"] if b["kind"] != "table"]
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'dropped-frozen': a block the "
                   "source dropped is deleted although no request could ever make its "
                   "equation again — the rewrite path checks that, the delete path "
                   "does not")
def test_a_block_with_an_equation_survives_the_source_dropping_it():
    world, ours, base = _push("equations")
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"] = [b for b in ours["blocks"]
                      if not any(r.get("chip") == "equation" for r in b.get("runs", []))]
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))


def test_a_paragraph_stays_a_paragraph_when_the_heading_above_it_is_deleted():
    """Deleting a block takes its paragraph mark, and Docs merges the two keeping the
    FIRST one's style, so the paragraph under a deleted heading became a heading: the
    merge writes its style before the delete above it, not after. The settle repairs
    it, because `carry_unimported` now compares the plan's named style with the
    read-back's and `tidy_requests` writes the difference."""
    world, ours, base = _push("prose")
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "heading:notes"]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert base["blocks"][0]["kind"] == "paragraph"


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'lost-key'/'crossed-delete': "
                   "`inherit_keys` hands a key the file itself asserts to another "
                   "block, so two blocks end up under one key and the paragraph the "
                   "first one named is deleted as 'dropped by the source'")
def test_inherit_keys_leaves_the_keys_the_file_asserts_alone():
    base = {"blocks": [_para("Before both.") | {"key": "paragraph:before-both"},
                       _para("After both.") | {"key": "paragraph:after-both"}]}
    ours = {"blocks": [_para("lantern both.") | {"key": "paragraph:before-both"},
                       {"key": "paragraph:after-both", "kind": "paragraph",
                        "runs": [{"text": "After both."},
                                 {"text": "", "frozen": True, "chip": "person",
                                  "value": "grace@example.com"}]}]}
    doc_merge.inherit_keys(base, ours)
    assert _keys(ours) == ["paragraph:before-both", "paragraph:after-both"]


def test_a_heading_the_source_turned_into_a_paragraph_is_a_paragraph():
    """Found by the campaign as a defect and fixed before it was ever reported:
    `_merge_block` used to patch the shape fields the file has instead of replacing
    them, so a heading the source demoted kept its `level` and `adopt_keys` could
    not pair a block whose shape said one thing and whose kind said another.
    `doc_merge._take_shape` now takes the source's shape whole, absences included."""
    was = {"blocks": [{"key": "k", "kind": "heading", "level": 1,
                       "runs": [{"text": "Results"}]}]}
    mine = {"blocks": [{"key": "k", "kind": "paragraph", "runs": [{"text": "Results"}]}]}
    live = copy.deepcopy(was)
    merged = doc_merge.merge(was, mine, live)["blocks"][0]
    assert merged["kind"] == "paragraph" and merged.get("level") is None


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'moved-styling': a block the "
                   "source both reworded and moved is written again from nothing, and "
                   "`_retext` folds the whole stretch into the first writable run, so "
                   "every mark the reader put inside it goes while the report calls the "
                   "block merged")
def test_a_block_the_source_reworded_and_moved_keeps_the_readers_styling():
    world = doc_world.build([{"blocks": [_para("alpha beta"), _para("gamma"),
                                         _para("delta")]}], title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {
        "range": {"startIndex": start + 6, "endIndex": start + 10},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"][0]["runs"][0]["text"] = "lantern beta"
    ours["blocks"].append(ours["blocks"].pop(0))
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))


@pytest.mark.xfail(strict=True, reason="fuzz_docs.KNOWN 'table-in-a-table': a table the "
                   "source adds in front of another table is written at the anchor's "
                   "index less one — the rule that lets a paragraph borrow the mark in "
                   "front of a table — which is inside the table before it")
def test_a_table_added_in_front_of_a_table_is_not_written_inside_the_one_before_it():
    world, ours, base = _push("two_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, _table([["h1", "h2"], ["willow", "x"]]))
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    texts = [oracle.text_of(b) for b in base["blocks"]]
    assert "h1 h2 willow x" in texts, f"the new table was written into {texts}"


@pytest.mark.xfail(strict=True, reason="`doc_sync.settle` keys the body first, and "
                   "`doc_ir.key_blocks` recurses into every tab, so a block in the "
                   "second tab is keyed from its own words before that tab's "
                   "`adopt_keys` runs — and adopt_keys skips a block that has a key. "
                   "The body's block, one line above, keeps its key")
def test_a_block_in_a_second_tab_keeps_its_key_when_the_source_moves_and_rewords_it():
    world = fuzz_docs.corpus("tabs")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    for blocks in (ours["blocks"], ours["tabs"][0]["blocks"]):
        moved = blocks.pop(0)
        moved["runs"] = [{"text": "Quite another title"}]
        blocks.append(moved)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    body = {b["key"] for b in base["blocks"]}
    appendix = {b["key"] for b in doc_ir.parts(base)[1]["blocks"]}
    assert "heading:notes" in body                      # the control: the body is right
    assert "heading:results" in appendix
