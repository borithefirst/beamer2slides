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


def test_the_oracle_sees_a_picture_the_reader_inserted_disappear():
    shot = {"frozen": True, "chip": "image", "text": "", "value": "kix.i7",
            "uri": "https://example.invalid/reader.png"}
    base = _ir(_p("k1", "look "))
    before = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, shot]})
    after = _ir(_p("k1", "look "))
    assert "frozen_gone" in _kinds(oracle.check(base, before, after, NOTHING))


def test_the_oracle_lets_a_picture_the_settle_named_alone():
    """A picture a reader inserted has only a `uri` until the settle saves it and
    gives it a file and a digest (`doc_sync.fetch_pictures`). The document did not
    change; the name we knew it by did. Judged on one name — and `frozen_key` prefers
    the digest — every such picture read as lost, which was thirteen of the twenty
    findings a chain-8 run had left. A picture is now the same picture under any of
    its names, and the object id is one of them."""
    shot = {"frozen": True, "chip": "image", "text": "", "value": "kix.i7",
            "uri": "https://example.invalid/reader.png"}
    named = shot | {"src": "doc.media/kix.i7.png", "sha": "b9c1f0a2"}
    before = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, shot]})
    after = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, named]})
    assert not oracle.failures(oracle.check(_ir(_p("k1", "look ")), before, after, NOTHING))
    # And the other way round: a block the sync rewrote keeps the file and is given a
    # new object id, which is the case `frozen_key`'s docstring was written for.
    rewritten = named | {"value": "kix.i9", "uri": "https://example.invalid/again.png"}
    settled = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, named]})
    after = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, rewritten]})
    assert not oracle.failures(oracle.check(settled, settled, after, NOTHING))


def test_the_oracle_names_the_right_one_of_two_pictures_from_one_url():
    """A reader who pastes the same image twice gives two pictures one `uri`, and
    that uri then says nothing about which is which. Matched on it, the one that went
    paired with the one that stayed and the survivor was left over and named as lost:
    seven of the nine findings a chain-8 run had left were that, each pointing at a
    picture still standing in the document (`oracle.telling_names`)."""
    def shot(oid, **more):
        return {"frozen": True, "chip": "image", "text": "", "value": oid,
                "uri": "https://example.invalid/reader.png"} | more

    first, second = shot("kix.i4", sha="sha-i4"), shot("kix.i5", sha="sha-i5")
    base = _ir(_p("k1", "look "), _p("k2", "and "))
    before = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "look "}, first]},
                 {"key": "k2", "kind": "paragraph", "runs": [{"text": "and "}, second]})
    after = _ir(_p("k1", "look "),
                {"key": "k2", "kind": "paragraph", "runs": [{"text": "and "}, second]})
    found = oracle.failures(oracle.check(base, before, after, NOTHING))
    assert [one["detail"] for one in found if "image" in one["detail"]] == [
        "the image 'sha-i4' the document held is not in it any more"]
    # And the excuse asks the same question: a file that holds `second` and not
    # `first` is a source that took `first` out, and their shared uri must not make
    # the one it kept answer for the one it dropped.
    ours = _ir(_p("k1", "look "),
               {"key": "k2", "kind": "paragraph", "runs": [{"text": "and "}, second]})
    found = oracle.failures(oracle.check(base, before, after, NOTHING, ours))
    assert not [one for one in found if "image" in one["detail"]]


def test_the_oracle_lets_a_chip_go_with_the_block_the_source_dropped():
    """Chips are counted by value over the whole tab, and the excuse is that the file
    still holds one of that value — which credits a chip the source added *somewhere
    else* against the one it is deleting here. A source that drops the block its
    person chip is in and adds a chip of the same address to another block read as no
    change at all, and then as a loss when the second block turned out to be one no
    request can write (`_source_dropped`; chain-8 seed 9109)."""
    chip = {"frozen": True, "chip": "person", "text": "Grace", "value": "grace@example.com"}
    held = {"key": "k1", "kind": "paragraph", "runs": [{"text": "ask "}, chip]}
    base = _ir(held, _p("k2", "status"))
    after = _ir(_p("k2", "status"))
    # The file has dropped k1 and put a chip of the same address into k2.
    ours = _ir({"key": "k2", "kind": "paragraph", "runs": [{"text": "status"}, chip]})
    assert not oracle.failures(oracle.check(base, base, after, NOTHING, ours))
    # But a chip the reader put there is not the source's to drop.
    theirs = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "ask "}, chip, {"text": " today"}]},
                 _p("k2", "status"))
    assert "frozen_gone" in _kinds(oracle.check(base, theirs, after, NOTHING, ours))


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


def test_a_table_in_a_later_tab_keeps_the_key_the_file_gave_it():
    """A table is anchored in its first cell, so a source that rewords that cell
    destroys its named range and the read-back has no key: only the plan knows one.
    It never got there while the first tab's keying — `doc_ir.key_blocks` recurses
    into every tab — had already named the block after its new words, and the file,
    the base and the document then all agreed on `table:quartz`. One source op and
    no reader at all (offline chain-8 seed 7007, shrunk)."""
    world, ours, base = _push("tabs")
    grid = [b for b in doc_ir.parts(ours)[1]["blocks"] if b["kind"] == "table"][0]
    assert grid["key"] == "table:year"
    grid["rows"][0][0][0]["runs"] = [{"text": "quartz"}]
    _, ours, _ = fuzz_docs.sync_once(world, ours, base)
    assert [b["key"] for b in doc_ir.parts(ours)[1]["blocks"]
            if b["kind"] == "table"] == ["table:year"]


def test_a_table_whose_anchor_row_the_source_deletes_keeps_its_key():
    """A table is anchored in its first cell, and a row delete can take that very
    cell: the structural batch then leaves the table with no named range at all, the
    re-read cannot find it, and the words planned against the new grid are written
    nowhere. `table:year` came back as `table:2024` — gone, as far as the file was
    concerned — and the source's cell edit went with it. `structure` gives a regrid
    the same `after` a new table gets, so `anchor_tables` finds it again and
    `plant_ranges` puts the range back (offline chain-8 seed 7122, shrunk).
    """
    world, ours, base = _push("tabs")
    grid = [b for b in doc_ir.parts(ours)[1]["blocks"] if b["kind"] == "table"][0]
    assert grid["key"] == "table:year" and len(grid["rows"]) == 3
    del grid["rows"][0]                       # the row the named range lives in
    grid["rows"][0][0][0]["runs"] = [{"text": "umbrella"}]
    _, ours, _ = fuzz_docs.sync_once(world, ours, base)
    now = [b for b in doc_ir.parts(ours)[1]["blocks"] if b["kind"] == "table"]
    assert [b["key"] for b in now] == ["table:year"]
    assert doc_merge._match_text(now[0]) == "umbrella | 7 | 2025 | 9"


@pytest.mark.parametrize("shape", sorted(fuzz_docs.SHAPES))
def test_the_world_carries_nothing_the_reader_does_not_read(shape):
    """`doc_ir.unmodelled` and `doc_world` were written apart and from the same API
    reference: one says what the reader reads, the other holds a document the way
    Docs holds it. Walking the second with the first is therefore a real check of
    both — and it comes back with the one thing the world has and we do not model,
    the section break a body opens on.

    It is also what keeps the campaign honest: a defect the world cannot represent
    is a defect the campaign cannot find, and this names the whole of that blind
    spot in one line per shape.
    """
    world, _, _ = _push(shape)
    assert set(doc_ir.unmodelled(world.read())) == {"structural.sectionBreak"}


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


def test_a_block_in_front_of_a_table_of_contents_can_be_deleted():
    """Was `toc-block`: every test for Docs' index rules read `kind == "table"`, so a
    table of contents was an ordinary block to the planner and the delete took the
    newline in front of it. Docs refuses that, and a refusal throws out the whole
    batch — the sync died. `doc_ir.STRUCTURAL` is what those rules are about."""
    world, ours, base = _build([_para("First line."), {"kind": "toc"}, _para("After it.")])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "paragraph:first-line"]
    fuzz_docs.sync_once(world, ours, base)
    assert _keys(doc_world.read_ir(world, ours, base)) == ["toc:empty", "paragraph:after-it"]


def test_a_block_can_be_written_in_front_of_a_table_of_contents():
    """The same, writing rather than deleting: the block went in at the TOC's own
    index, where nothing can be inserted."""
    world, ours, base = _build([_para("First line."), {"kind": "toc"}, _para("After it.")])
    ours["blocks"].insert(1, _para("A new line."))
    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [doc_merge.block_text(b) for b in doc_world.read_ir(world, ours, base)["blocks"]] \
        == ["First line.", "A new line.", "", "After it."]


def test_an_empty_paragraph_between_two_tables_is_kept_and_said_out_loud():
    """Was `empty-delete`, and it killed the sync: such a paragraph has no mark to give
    up — its own is the newline in front of a table and the block before it is a table
    — so `_delete_range` came back with a range of length 0, Docs refused it, and the
    refusal threw out the whole batch. Docs wants a paragraph between two tables
    anyway, so `restore_undeletable` keeps it and the report says why."""
    world, ours, base = _build([_table([["a", "b"]]), _para(""),
                                _table([["c", "d"]]), _para("The end.")])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "paragraph:empty"]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(doc_world.read_ir(world, ours, base)) == [
        "table:a", "paragraph:empty", "table:c", "paragraph:the-end"]
    assert any("no request can delete it" in line for line in report["notes"])


def test_a_table_the_reader_typed_in_survives_the_source_dropping_it():
    """Was `dropped-table`: the 'edited in the document' test used `block_text`, which
    is empty for a table, so the reader's cells counted for nothing and the table went
    with everything in it. `doc_merge._edited` reads the cells and the grid."""
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


def test_a_block_with_an_equation_survives_the_source_dropping_it():
    """Was `dropped-frozen`: the rewrite path in `_merge_block` refuses to delete and
    write back a block holding content no request can make again, and the delete path
    did not check at all — the same loss with nothing written back. It is kept now, and
    the report says the source asked for it to go."""
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


def test_inherit_keys_leaves_the_keys_the_file_asserts_alone():
    """`inherit_keys` used to match every block of the file again by its words,
    although the file had just named them all: two blocks that read alike swapped
    keys, one key ended up on two blocks and none on the other, and the merge read
    the second as dropped by the source. Here the source reworded the first block
    and the second holds a chip, so neither matches exactly and the similarity pass
    is what runs — which is where the two of them used to cross."""
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


def test_a_block_the_source_reworded_and_moved_keeps_the_readers_styling():
    """Was `moved-styling`: a block the source both reworded and moved is written again
    from nothing, and `_retext` folded the whole stretch between two frozen runs into
    the first writable run, so every mark the reader put inside it went while the
    report called the block merged. `_retext` now gives every word the document has
    the document's own styling, through the run builder `_restyled_words` already
    used (`_runs_from_styles`)."""
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
    moved = doc_world.read_ir(world, ours, base)["blocks"][-1]
    assert [(r["text"], r.get("bold")) for r in moved["runs"]] == \
        [("lantern ", None), ("beta", True)]


def test_a_styled_word_the_source_replaced_is_reported_with_the_styling():
    """A word the reader styled and the source then rewrote: the styling has nowhere
    to go, which is right, but nothing said so and the oracle called it lost in
    silence (offline chain-8 seeds 53 and 274). `doc_merge.reader_styling_gone` names
    the word in the report."""
    world, ours, base = _build([_para("alpha beta gamma"), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {
        "range": {"startIndex": start + 6, "endIndex": start + 10},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"][0]["runs"][0]["text"] = "alpha quartz gamma"
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    assert any("'beta'" in line for line in report["notes"])
    assert doc_merge.block_text(doc_world.read_ir(world, ours, base)["blocks"][0]) \
        == "alpha quartz gamma"


def test_an_empty_paragraphs_key_stays_on_it_when_a_block_is_written_at_its_mark():
    """Was the last cause of `lost-key`: an empty paragraph's range is exactly its mark,
    and text written *at* a range's first index pushes the range along (Docs' rule), so
    "\\ntext" appended at that mark left the key on the new block and the empty
    paragraph with none — the plan after read the new block as the old one and the
    empty one as the reader's. Reading it back off the wrong block was tried twice and
    cost the round its convergence; `requests` instead plants the range again on the
    mark in the same batch, after the appends there and before a block inserted in
    front of the paragraph (offline chain-8 seed 66, shrunk: a source move in the
    `between_tables` shape; `doc_world` takes `deleteNamedRange` for it)."""
    world, ours, base = _build([_table([["a", "b"]]), _para(""),
                                _table([["c", "d"]]), _para("The end.")])
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, _para("Willow here."))
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(doc_world.read_ir(world, ours, base)) == [
        "table:a", "paragraph:empty", "paragraph:willow-here", "table:c", "paragraph:the-end"]
    assert _keys(ours) == _keys(base) == _keys(doc_world.read_ir(world, ours, base))
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_key_a_readers_chip_pushed_onto_the_mark_survives_a_structural_batch():
    """The same drift from the reader's side, and the batch of words is too late for
    it: a person chip put into an empty paragraph pushes its range onto the mark, and
    when the source then moves the table after it, the structural batch that builds
    the table swallows that very mark — the chip paragraph loses its key, and the
    rebuilt table, found again by the key of the block it follows (`anchor_tables`),
    settles as `table:empty` while the file still names `table:c` (offline chain-8
    seed 296, shrunk). `structure` now heads its batch with `doc_ir.replant_requests`,
    and `settle` plants a drifted range back too (`name_requests`)."""
    world, ours, base = _build([_table([["a", "b"]]), _para(""), _para("Signal."),
                                _table([["c", "d"]]), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    empty = next(b for b in part["blocks"] if b["key"] == "paragraph:empty")
    world.apply([{"insertPerson": {"location": {"index": empty["span"][1] - 1},
                                   "personProperties": {"email": "reader@example.com"}}}])
    part = doc_world.read_ir(world, ours, base)
    empty = next(b for b in part["blocks"] if b["key"] == "paragraph:empty")
    assert empty["range"][0] == empty["span"][1] - 1, "the chip pushed the range along"
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    # The table goes up, right behind the chip paragraph: it is built at "Signal."'s
    # start, and the empty paragraph that leaves is swallowed by deleting the mark
    # before it — the chip paragraph's, where the drifted range sits.
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(2, ours["blocks"].pop(at))
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert _keys(live) == ["table:a", "paragraph:empty", "table:c", "paragraph:signal",
                           "paragraph:the-end"]
    assert [r.get("chip") for r in live["blocks"][1]["runs"]] == ["person"]
    assert oracle.text_of(live["blocks"][2]) == "c d"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_block_written_from_nothing_does_not_inherit_its_neighbours_styling():
    """Text inserted inherits the styling of the character in front of it (Docs'
    rule), so a block the source moved in front of an underlined heading came out
    underlined with the reader's bold on it, and the oracle called the bold lost
    (offline chain-8 seed 280, shrunk). `_style_requests` names every managed field
    on every run now, so a block written from nothing has the styling the file gives
    it and no other."""
    world = doc_world.build([{"blocks": [
        _para("alpha beta"), _para("gamma"),
        {"kind": "heading", "level": 2, "runs": [{"text": "Underlined", "underline": True}]},
        _para("delta")]}], title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {
        "range": {"startIndex": start + 6, "endIndex": start + 10},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"].insert(3, ours["blocks"].pop(0))     # in front of "delta", after the heading
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    moved = doc_world.read_ir(world, ours, base)["blocks"][3]
    assert [(r["text"], r.get("bold"), r.get("underline")) for r in moved["runs"]] == \
        [("alpha ", None, None), ("beta", True, None)]


def test_a_table_added_between_two_tables_is_refused_and_said_out_loud():
    """Was `table-in-a-table`. A block in front of a table is written at the anchor's
    index less one — the paragraph mark it borrows — and when the block before is a
    table too, that index is inside its last cell: the new table was built inside the
    old one, the words never reached it, `anchor_tables` could not find it and every
    re-plan built another. There is nowhere to write it, so `_new_table_requests`
    asks for nothing and the report says why. Docs keeps a paragraph between two
    tables anyway (the `between_tables` shape), where the borrowed mark is that
    paragraph's and the table goes in."""
    world, ours, base = _push("two_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, _table([["h1", "h2"], ["willow", "x"]]))
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    texts = [oracle.text_of(b) for b in base["blocks"]]
    assert not any("willow" in one for one in texts), \
        f"the new table was written into {texts}"
    assert any("no paragraph to write in" in note for note in report["notes"]), report

    world, ours, base = _push("between_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, _table([["h1", "h2"], ["willow", "x"]]))
    for _ in range(2):                       # a grid is built on one pass, filled on the next
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert "h1 h2 willow x" in [oracle.text_of(b) for b in base["blocks"]]


def test_a_paragraph_the_source_moves_between_two_tables_is_left_where_it_is():
    """The same arithmetic, and the same nowhere, for an ordinary block — but a move
    is a delete and a write, so this one was deleted from its old place and written
    into the last cell of the table before its new one: the paragraph was destroyed
    by a source op no reader had touched (offline chain-8 seed 7008, shrunk). It
    stays where the document has it and the report says why (`refuse_nowhere`)."""
    world, ours, base = _push("two_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, ours["blocks"].pop())
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(base) == ["paragraph:before-both", "table:a", "table:c",
                           "paragraph:after-both"]
    assert any("no paragraph to write in" in note for note in report["notes"]), report


def test_a_block_in_a_second_tab_keeps_its_key_when_the_source_moves_and_rewords_it():
    """A move is a delete and an insert, so the block's named range goes and only the
    plan knows its key. `doc_sync.settle` used to key the body first, and
    `doc_ir.key_blocks` recurses into every tab, so the block was named after its own
    new words before that tab's `adopt_keys` ran — and `adopt_keys` skips a block that
    has a key. The body's block, one line above, always kept its key, which is the
    control here. `doc_merge.settle_keys` adopts every part before it keys any."""
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
