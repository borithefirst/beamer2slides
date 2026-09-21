"""Fuzzing the Google Docs sync against its loss oracle: does a sync lose a reader's work?

Three parts, all offline and all fast (no Google call anywhere in this file).

1. **The campaign on fixed seeds.** `beamer2slides.devtools.fuzz_docs` builds a document
   from the corpus, edits it as a reader would (`doc_world` applies the real requests
   under Docs' index rules), changes the canonical file as an author would, runs the real
   `doc_merge.plan` and the real settle, and hands the two read-backs, the base and the
   report to `doc_loss_oracle`. A round is clean when the oracle finds nothing the report
   does not name, and when a second sync writes nothing. Defects nobody has fixed yet
   would be listed in `fuzz_docs.KNOWN` — a round that ends on one of those is counted
   and let through — and that tuple is **empty**: every finding fails the campaign now.

2. **The oracle is not vacuous.** A clean round is taken apart again with a loss put in on
   purpose — a block the reader added deleted, a word swallowed, styling dropped, a chip
   gone, a cell overwritten, the reader's order undone, a whole tab removed — and the
   oracle has to catch every one of them, and to stay silent when the report owns up to
   the same thing. An oracle nobody has tried to fool proves nothing.

3. **One test per defect the campaign found**, each a hand-written scenario rather than a
   seed, so it says what is wrong rather than which dice fell. Each was an xfail while its
   defect stood and became a plain test when it was fixed, its `KNOWN` entry going in the
   same commit — an entry that stays on after the fix is let through, so it hides the next
   defect that reaches the same signature rather than catching it.

The campaign itself, past these seeds:
    python -m beamer2slides.devtools.fuzz_docs offline --rounds 300
    python -m beamer2slides.devtools.fuzz_docs offline --rounds 400 --chain 4
"""

import copy
import os
from collections import Counter

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
# accusing a token both sides had edited half of, and seven at chain 6: 970711 a table
# the reader beheaded in a tab the merge plans nothing for, 970228 a mark the source
# asked for on an un-bolding the base already had, 970528 a word the merge split into
# two runs, 980193 two copies of one picture with one dropped by the source, 993608 a
# beheaded table taking the key of one the source regrids, 912452 a paragraph keeping
# the centring of the one deleted above it. And two at chain 8: 994410 the surviving copy
# of a picture answering for the one the reader deleted, 994424 a kept paragraph following
# the table the source moved away from it, 41000 an empty heading in front of a table
# whose named range outlived the delete that borrowed the mark before it. Then 63138
# (chain 4) a row the reader deleted put back by the round after the regrid, and 64166
# (chain 8) the oracle calling a dragged block that held a chip a resurrection, and 65370
# (chain 8) two `add_tab` draws picking one name, the second read as the first coming back,
# and 70140 (chain 4) a named range left in the paragraph a join swallowed, which stole the
# survivor's key the moment the source rewrote its words. Then, once the campaign pressed
# Enter and Backspace and pasted for itself: 74230 (chain 8) a table the source moved and
# regridded at once, built again from the file's grid and so from the row the reader had
# deleted; 76101 (chain 4) the oracle reading a base word the reader's drag had taken the
# full stop off as one they typed; 77064 (chain 8) that same orphan range stealing a key
# one write earlier than the settle could take it away.
# A seed names a *script*, not a defect: growing `fuzz_docs.PARA_MARKS` or
# `READER_MEASURES` (paragraph borders, `pageBreakBefore` and `keepWithNext` went in with
# the dialect) makes every draw come out different, so these rounds no longer replay the
# scripts they were found on. They stay because a passing seed costs nothing and a whole
# corner of the space is cheap to keep walking; what actually pins each defect is its own
# hand-built test below.
REGRESSIONS = ((60, 1), (181, 1), (309, 4), (1031, 8), (1147, 8),
               (5099, 8), (5130, 8), (5167, 8),
               (970228, 6), (970528, 6), (970711, 6), (980193, 6),
               (912452, 6), (993608, 6), (994410, 8), (994424, 8), (41000, 8),
               (63138, 4), (64166, 8), (65370, 8), (66195, 4), (70140, 4),
               (74230, 8), (76101, 4), (77064, 8),
               # The first seeds a judge of the campaign's own caught: a table the
               # source regrids *and* moves, built again at the grid the sync was
               # about to change (`doc_merge._built_size`). Both shapes of it —
               # `two_tables` with a reader in the round, and the same thing with no
               # reader at all.
               (88033, 6), (88075, 6),
               # The key the reader's pasted twin took off the block the source had
               # just written a chip into (`doc_merge._adopt_in_order`): once with
               # the copy in front of a rewritten paragraph, once with the chip in an
               # empty one, and once — 96300 — with the heading the source moved and
               # restyled, whose shape the write changed under it. Between them,
               # 93212 is the welded token and 94030 the pared-down one whose joiner
               # went with it.
               (93212, 8), (94030, 6), (94465, 6), (94577, 6), (96300, 4),
               # The styling judge's own first three: 110149 the world sharing one
               # measures dict between a paragraph and the one split off it, 110265
               # a picture the reader inserted read as the reader restyling the
               # block, and 220012 the judge itself calling a `bold: False` a mark
               # after the source had retitled the heading that made it one.
               (110149, 4), (110265, 4), (220012, 8),
               # And the oracle counting a deleted row's words rather than looking
               # them up: the source rewrote the row beside it into exactly what the
               # deleted one said. Then 280039, a table the source moved right behind
               # another, and 280398, a chip's face read as words the reader typed.
               # And 290010, where Docs' merge-on-delete made an indented paragraph
               # an item, so the settle read its indent as the list preset's; then
               # 330127, a new table's swallow taking an empty paragraph's name.
               (260208, 6), (280039, 8), (280398, 8), (290010, 10), (330127, 4),
               # The order judge's own first four: 380191 a table whose refused move
               # left it standing, in `merged`, where only the file has it; 400186
               # `_moved_keys` reading the file against the base with
               # `SequenceMatcher`'s contiguous blocks rather than the longest common
               # subsequence, and the additions placed before the moves; 400044 a
               # bold the reader moved from one word to another, written over by a
               # source restyle that never asked about it; 450252 a paragraph the
               # file puts behind a table that could not move, stuck with it and
               # nothing saying so. Between them 430296 and 430587, the two doors
               # onto a nesting level no request can write.
               # 530265 is the third door onto a level: an item moved to the end
               # behind one the reader had nested came out nested, which the note
               # was too narrow to say. 480066 is a row the reader deleted and the
               # source had written in, and 550667 a range two deletes of one batch
               # both claimed, which Google refuses and which killed the sync. Then
               # 570181, the settle's own bullet run reaching over the block it had
               # just unbulletted, and 570177, the cell judge counting a regrid's
               # shifted words as words the source wrote.
               (380191, 4), (400044, 6), (400186, 6), (430296, 4), (430587, 4),
               (450252, 10), (480066, 10), (530265, 8), (550667, 6),
               (570177, 4), (570181, 4),
               # And two the fresh campaigns found: 600784 a paragraph the reader
               # dragged against the word standing in a cell, welded to it, and
               # 630138 an empty paragraph of the source's own promoted into the
               # place Docs keeps in front of a body's first table. Then 660085,
               # the existence judge's own first: a block the merge refuses to
               # write, asked about with no key to look the refusal up by. Then the
               # harness's own drag, whose drop index was read off the document
               # before the cut (710370 died on the lone surrogate it left), and the
               # two coincidences that made the existence judge ask its question a
               # block at a time and word for word (720173, 720270), a twin given a
               # chip and one reworded while their copy arrived (720074, 730061),
               # and a blank table recovered onto the blank one just built (730384).
               # 780188 is two empty paragraphs trading names under a real move,
               # and 790329 a regridded table anchored onto the one the reader
               # had beheaded.
               (600784, 4), (630138, 12), (660085, 4),
               (710370, 4), (720074, 8), (720173, 8), (720270, 8),
               (730061, 12), (730384, 12), (780188, 10), (790329, 6))

# And the seeds whose script only exists on one shape, the campaign having been run
# with `--shape`: a shape-restricted round draws its tables and its readers every
# time, so 800 rounds of `two_tables` press on the table repairs the way 1,600 mixed
# ones do not. The three of them: a full stop the source parked against a bolded
# word (the oracle's), the key of a table this batch regrids given away by the pass
# that guesses (`recover_tables`), and a table moved in front of the empty paragraph
# the body ends behind (`refuse_eaten_anchor`). Then `themed`, where the theme is
# what a paragraph *inherits*: a heading justified by the delete above it
# (`paragraph_merged`), and a bold the theme already puts on read as a restyle that
# vanished once the source moved the block to another named style. Then the two the
# oracle owed at chain 10: an un-bolding on a block the source dropped, and one the
# source reworded away that the word's own twin answered for. And one more from
# `between_tables`: the empty block a new table swallows, recovered only when it read
# as a plain paragraph. And one from `astral`: an orphan named range handed to the
# empty paragraph a new table leaves behind, resurrecting a block the reader deleted.
# And one from `tabs`: the nesting level an item written in front of a nested one
# takes, which `unwritten_levels` predicted from the wrong side. And two more of one
# defect, seen from either side: the empty paragraph a table's delete eats the mark of
# came back unnamed, so the source's restyle of it went nowhere (1730265, chain 16)
# and the order it asks for was never reached (1740158, chain 14, mixed shapes).
# And one from `prose`, which is a report rather than a loss: a cell both sides wrote
# in raised its conflict under the name `a table cell`, so nobody — the person reading
# the report, or the oracle looking the cell up in it — could tell which table. And
# 2000188, which is the first thing `_shape_findings` saw the first time it was run:
# how the reader set a paragraph, thrown away by the rewrite a source chip asks for.
SHAPED = ((870308, 8, "two_tables"), (870368, 8, "two_tables"),
          (890070, 8, "ends_on_table"), (1130023, 6, "themed"),
          (1140022, 8, "themed"), (1150196, 6, "themed"),
          (1180145, 10, "themed"), (1180151, 10, "themed"),
          (1270233, 8, "between_tables"), (1430231, 10, "astral"),
          (1640036, 12, "tabs"), (1710213, 12, "prose"), (1740158, 14, None),
          (1730265, 16, "tabs"), (2000188, 6, None))


def _round(seed: int, chain: int, shape: str | None = None) -> None:
    script = fuzz_docs.draw(seed, chain, shape=shape)
    found = fuzz_docs.offline_round(seed, chain, script=script)
    unknown, known = fuzz_docs.triage(found)
    if unknown:
        small = fuzz_docs.shrink(script, still=lambda f: fuzz_docs.triage(f)[0])
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


@pytest.mark.parametrize("seed,chain,shape", SHAPED)
def test_a_shaped_seed_that_once_failed_still_passes(seed, chain, shape):
    _round(seed, chain, shape=shape)


@pytest.mark.parametrize("seed", (6, 15, 23, 29))
def test_a_round_on_a_document_with_a_theme_of_its_own_loses_nothing(seed):
    """A document's look lives in its named styles, and a paragraph that sets nothing
    of its own wears them. The `themed` shape is drawn so that a heading's centring is
    the theme's: seeds the injected defect below is known to reach, so that these four
    say something rather than pass by luck."""
    _round(seed, 2, shape="themed")


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


def _grid(rows, **kw):
    return {"kind": "table", "rows": [[[_p(None, c)] for c in row] for row in rows],
            **kw}


def test_the_oracle_knows_a_table_the_reader_beheaded_from_one_they_made():
    """Deleting a table's first row takes its named range with it
    (`doc_ir.anchor_span`), so the read-back has the table unkeyed — and an unkeyed
    block is the oracle's word for "the reader added this", whose every word then has
    to survive. It is the base's table, and once the merge knows it again
    (`doc_merge.recover_tables`) the source's own edit to a cell the reader kept is a
    change and not a loss."""
    base = _ir(_grid([["a", "b"], ["1", "2"], ["x", "y"]], key="t1"))
    before = _ir(_grid([["1", "2"], ["x", "y"]]))           # the reader beheaded it
    after = _ir(_grid([["1", "ribbon"], ["x", "y"]], key="t1"))   # the source's cell
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # And it is judged, not excused: a word the reader typed into a row they kept has
    # to be there afterwards, exactly as in any other table.
    before = _ir(_grid([["1", "willow"], ["x", "y"]]))
    assert _kinds(oracle.check(base, before, after, NOTHING)) == {
        "cell_words_lost", "words_lost"}


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


def test_a_mark_the_source_adds_beside_the_readers_is_not_the_readers_going():
    """The reader's colour on a word, and the source then strikes that word through.
    Counted as whole *sets* of marks, `('color',)` was nowhere to be found afterwards
    — `('color', 'strike')` is another key — so the colour read as lost while it sat
    there in the document. One mark at a time (fresh seed 970528, chain 6)."""
    base = _ir(_p("k1", "one two"))
    before = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "one "}, {"text": "two", "color": "#993333"}]})
    after = _ir({"key": "k1", "kind": "paragraph",
                 "runs": [{"text": "one "},
                          {"text": "two", "color": "#993333", "strike": True}]})
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # And the colour really going is still a loss, strike or no strike.
    gone = _ir({"key": "k1", "kind": "paragraph",
                "runs": [{"text": "one "}, {"text": "two", "strike": True}]})
    assert _kinds(oracle.check(base, before, gone, NOTHING)) == {"styling_lost"}


def test_a_word_the_merge_split_into_two_runs_still_wears_what_it_wore():
    r"""A word is what a reader sees, not what a run holds. The reader typed `vellum`
    after a soft hyphen, so `\xadvellum` is one word to `\S+`; the merge then wrote
    the source's strike on the words the file has and left the typed word in a run of
    its own. Asked run by run, the coloured word `\xadvellum` was gone and `\xad` and
    `vellum` had taken its place (fresh seed 970528, chain 6)."""
    base = _ir(_p("k1", "a \xadhyphen"))
    before = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "a \xadvellum hyphen", "color": "#993333"}]})
    after = _ir({"key": "k1", "kind": "paragraph",
                 "runs": [{"text": "a \xad", "color": "#993333", "strike": True},
                          {"text": "vellum ", "color": "#993333"},
                          {"text": "hyphen", "color": "#993333", "strike": True}]})
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # The colour off the half the reader typed is a loss again: the word wears a mark
    # only where every character of it does.
    half = copy.deepcopy(after)
    del half["blocks"][0]["runs"][1]["color"]
    assert _kinds(oracle.check(base, before, half, NOTHING)) == {"styling_lost"}


def _head(key, text, **kw):
    return {"key": key, "kind": "heading", "level": 1, "runs": [{"text": text}], **kw}


THEME = {"HEADING_1": {"align"}}


def test_the_oracle_sees_a_paragraph_stop_following_the_documents_theme():
    """The heading says nothing about its alignment, which is how a paragraph wearing
    the theme's centring reads; afterwards it says `left` of its own. Nothing was
    deleted and no word moved, so every other check here is happy, and so is the
    convergence check: the file is regenerated from the document, so the next sync
    writes nothing at all. Only the theme is gone."""
    base = _ir(_head("k1", "A heading"))
    before = _ir(_head("k1", "A heading"))
    after = _ir(_head("k1", "A heading", align="left"))
    found = oracle.check(base, before, after, NOTHING, theme=THEME)
    assert _kinds(found) == {"theme_undone"}
    assert all(f["severity"] == "loss" for f in oracle.failures(found))
    # The file asking for it is the reason a sync may write one.
    mine = _ir(_head("k1", "A heading", align="left"))
    assert not oracle.failures(oracle.check(base, before, after, NOTHING, mine,
                                            theme=THEME))


def test_a_twin_the_key_lands_on_is_not_a_paragraph_that_stopped_following_the_theme():
    """A key is not a block. The reader pastes a copy of a heading beside the
    original, and the copy lands in the style of what it was dropped into — Docs' own
    rule, and in the fuzz world that is a heading centred of its own. The source then
    drops the block the key was on, and the settle keys the copy by its words to the
    name that went.

    The key therefore names a different block before and after the sync, and asked by
    key the field did stop being inherited — but on nobody's doing: the copy was set
    that way before the sync, nothing was written to it, and the block that really
    wore the theme is one the source dropped and the reader left alone. Asked of the
    words the key names afterwards, so a twin saying something else answers for
    nothing (themed seed 2030066, chain 6)."""
    base = _ir(_head("k1", "A heading"), _p("k2", "Prose."))
    before = _ir(_head("k1", "A heading"), _p("k2", "Prose."),
                 _head(None, "A heading", align="center"))   # the reader's own copy
    after = _ir(_head("k1", "A heading", align="center"), _p("k2", "Prose."))
    mine = _ir(_p("k2", "Prose."))                           # the file drops the heading
    assert not oracle.failures(oracle.check(base, before, after, NOTHING, mine,
                                            theme=THEME))
    # A twin that says something else is no twin at all, and the question stands.
    other = _ir(_head("k1", "A heading"), _p("k2", "Prose."),
                _head(None, "Another heading", align="center"))
    stands = _ir(_head("k1", "A heading", align="center"), _p("k2", "Prose."),
                 _head(None, "Another heading", align="center"))
    assert _kinds(oracle.check(base, other, stands, NOTHING, mine,
                               theme=THEME)) == {"theme_undone"}


def test_the_oracle_keeps_out_of_what_the_document_does_to_its_own_paragraphs():
    """The check needs to be told what the theme sets, and that is not pedantry: Docs
    merges a deleted paragraph into the one behind it and hands over its style, so a
    paragraph really does change alignment with nobody writing one. Asked about a
    field no named style sets, the oracle says nothing."""
    base = _ir(_p("k1", "A line."))
    before = _ir(_p("k1", "A line."))
    after = _ir(_p("k1", "A line.", align="center"))
    assert not oracle.failures(oracle.check(base, before, after, NOTHING, theme=THEME))
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))


def test_the_campaign_sees_a_theme_undone(monkeypatch):
    """The defect this check was built for, put back on purpose.

    `doc_merge.paragraph_style` used to give `alignment` a value always — START
    whenever the file said nothing, which is also what a heading centred by the
    document's theme says. Measured over the first 40 `themed` seeds at chain 2, the
    campaign catches 6 of them; these sixteen hold two.

    The defect now sits behind two doors, and the probe opens both: the settle puts
    the plan's whole paragraph style back on a block this run wrote whose style the
    write did not leave as the plan asked (`_unwritten`), which heals a plan that
    pins an alignment as surely as it heals the one Docs' merge-on-delete pins. That
    repair is a second line and not this check's subject — what is measured here is
    the oracle's reach, so both are put back and the question stays the one it
    always was: with a theme undone in the document, does the campaign say so?
    """
    real = doc_merge.paragraph_style

    def broken(block):
        style, fields = real(block)
        style["alignment"] = doc_ir.TO_ALIGNMENT[block.get("align") or "left"]
        return style, fields

    monkeypatch.setattr(doc_merge, "paragraph_style", broken)
    monkeypatch.setattr(doc_merge, "_unwritten", lambda mine, live: [])
    caught = 0
    for seed in range(16):
        found = fuzz_docs.offline_round(
            seed, script=fuzz_docs.draw(seed, 2, shape="themed"))
        caught += "theme_undone" in {f["kind"] for f in oracle.failures(found)}
    assert caught >= 2, "the campaign no longer reaches the defect it was built for"


MARKED = {"HEADING_1": {"align", "bold"}}


def _run_head(key, text, **run):
    return {"key": key, "kind": "heading", "level": 1,
            "runs": [{"text": text, **run}]}


def test_the_oracle_sees_a_mark_the_reader_took_off_handed_back():
    """A reader who un-bolds a word of a bold heading has done something the file has
    to be able to say. Afterwards the run says nothing about bold, which is how a word
    wearing the theme's bold reads — so the word is bold again and nobody said so.

    Nothing was deleted and no word moved, and the file is regenerated from the
    document, so the convergence check is happy too: this is the only thing that sees
    it."""
    base = _run_head("k1", "A heading")
    before = _run_head("k1", "A heading", bold=False)
    after = _run_head("k1", "A heading")
    found = oracle.check(_ir(base), _ir(before), _ir(after), NOTHING, theme=MARKED)
    assert _kinds(found) == {"styling_restored"}
    # And silent when the report owns up to it.
    said = {"conflicts": [{"key": "k1", "field": "text"}], "notes": []}
    assert not oracle.failures(oracle.check(_ir(base), _ir(before), _ir(after), said,
                                            theme=MARKED))


def test_a_mark_the_source_asks_for_on_an_agreed_un_bolding_is_the_sources_to_ask():
    """The reader pressed Ctrl+B a sync ago and the settle wrote that into the file
    and the base, so the un-bolding is what both sides last agreed on. The source now
    marks that very word bold in the file: a source restyle of a block the document
    has not restyled since, which the merge's own rule gives to the source (fresh seed
    970228, chain 6).
    """
    base = _run_head("k1", "A heading", bold=False)     # agreed a sync ago
    before = _run_head("k1", "A heading", bold=False)   # and untouched since
    after = _run_head("k1", "A heading", bold=True)
    mine = _run_head("k1", "A heading", bold=True)      # the file asks for it
    assert not oracle.failures(oracle.check(_ir(base), _ir(before), _ir(after),
                                            NOTHING, _ir(mine), theme=MARKED))
    # Only the file *saying* the mark excuses it. A file that says nothing there is a
    # block the source moved, written again from nothing and handed to the theme.
    quiet = _run_head("k1", "A heading")
    assert _kinds(oracle.check(_ir(base), _ir(before), _ir(quiet), NOTHING,
                               _ir(quiet), theme=MARKED)) == {"styling_restored"}
    # And an un-bolding of this very round is the reader's news, whatever the file
    # asks for: both sides on one word is the document's, or a conflict.
    assert _kinds(oracle.check(_ir(_run_head("k1", "A heading")), _ir(before),
                               _ir(after), NOTHING, _ir(mine),
                               theme=MARKED)) == {"styling_restored"}


def test_a_mark_no_named_style_puts_on_is_not_one_a_reader_took_off():
    """`bold: False` on an ordinary paragraph is not a choice against anything: there
    is no bold to take off, so there is nothing to hand back."""
    before = {"key": "k1", "kind": "paragraph", "runs": [{"text": "A line.",
                                                          "bold": False}]}
    after = _p("k1", "A line.")
    assert not oracle.failures(oracle.check(_ir(_p("k1", "A line.")), _ir(before),
                                            _ir(after), NOTHING, theme=MARKED))


def test_a_block_that_stopped_being_a_heading_took_no_mark_off_anybody():
    """The word lost the bold because the block lost the named style that put it on —
    which the source is allowed to do, and which `theme_undone` is the check for. The
    reader's choice was honoured, not undone."""
    before = _run_head("k1", "A heading", bold=False)
    after = {"key": "k1", "kind": "paragraph", "runs": [{"text": "A heading"}]}
    assert "styling_restored" not in _kinds(
        oracle.check(_ir(before), _ir(before), _ir(after), NOTHING, theme=MARKED))


def test_the_bold_a_heading_inherits_is_not_bold_the_reader_put_on():
    """Docs merges the paragraph behind a deleted one into it and hands over its style,
    so a paragraph really does become a heading with nobody writing one — and wears the
    theme's bold from then on. Counted as the reader's, the next source restyle of that
    block reads as losing it (themed seed 283)."""
    base = _p("k1", "And prose after that.")
    before = _run_head("k1", "And prose after that.")          # merged into a heading
    after = _run_head("k1", "And prose after that.", fontsize=14.5)
    assert not oracle.failures(oracle.check(_ir(base), _ir(before), _ir(after),
                                            NOTHING, theme=MARKED))


def test_a_second_copy_of_the_un_bolded_word_is_not_the_readers_coming_back():
    """Inside the block the question is asked by *occurrence*: how many of this word
    the reader un-marked, against how many are still un-marked afterwards.

    Asked instead as "does this word wear the bold anywhere in the block?", a second
    occurrence the source had just appended answered yes, while the word the reader
    pressed Ctrl+B on stood exactly as they left it (themed seed 40254: the heading
    said "and willow" and the source added " and harbour").
    """
    base = _run_head("k1", "A heading and willow")
    before = {"key": "k1", "kind": "heading", "level": 1,
              "runs": [{"text": "A heading "}, {"text": "and", "bold": False},
                       {"text": " willow"}]}
    after = copy.deepcopy(before)
    after["runs"].append({"text": " and harbour"})          # the source's own "and"
    assert not oracle.failures(oracle.check(_ir(base), _ir(before), _ir(after),
                                            NOTHING, theme=MARKED))
    # And the reader's own occurrence going back to the theme is still a loss.
    handed = copy.deepcopy(after)
    handed["runs"][1] = {"text": "and"}
    assert _kinds(oracle.check(_ir(base), _ir(before), _ir(handed), NOTHING,
                               theme=MARKED)) == {"styling_restored"}


def test_the_un_bolded_word_is_asked_about_in_its_own_block():
    """The theme bolds every heading, so the same word in the heading next door wears
    the bold whatever happens here. Asked tab-wide, every un-bolding a sync honoured
    read as a loss (themed seeds 9, 32, 40)."""
    other = _run_head("k2", "Another heading")
    base = _ir(_run_head("k1", "A heading"), other)
    before = _ir(_run_head("k1", "A heading", bold=False), other)
    assert not oracle.failures(oracle.check(base, before, copy.deepcopy(before),
                                            NOTHING, theme=MARKED))


def test_the_campaign_sees_a_mark_the_file_cannot_say_is_off(monkeypatch):
    """The other half of the theme defect, put back on purpose.

    `doc_merge._text_style` used to write a mark only when it was on, which is all the
    file could say before `doc_ir.MARK_FIELDS`: the first source edit to rewrite the
    block then names no bold, and the word goes back to wearing the theme's. Measured
    over 300 `themed` seeds at chain 3 the campaign catches 6 (7 before `cell_style`
    and `restyle_cell`, 11 before `paste_block`, 13 before that: every op added to
    the campaign changes what every seed draws, and a window that held four can come
    to hold none — this one did, twice). These 300 hold six: 184, 215, 225, 233, 340
    and 386, of which four are in the window.
    """
    real = doc_merge._text_style

    def broken(run):
        style = real(run)
        for key, api in doc_ir.MARK_FIELDS:
            if run.get(key) is False:
                style.pop(api, None)
        return style

    monkeypatch.setattr(doc_merge, "_text_style", broken)
    caught = 0
    for seed in range(180, 300):
        found = fuzz_docs.offline_round(
            seed, script=fuzz_docs.draw(seed, 3, shape="themed"))
        caught += "styling_restored" in {f["kind"] for f in oracle.failures(found)}
    assert caught >= 3, "the campaign no longer reaches the defect it was built for"


def test_the_oracle_sees_the_first_tab_renamed_back_under_the_reader():
    """Only the reader's own rename is theirs to lose. A source rename over a title
    the document left alone lands in `applied`, which `accounted` does not read, so
    the oracle has to ask the narrow question: did the *reader* name it, and is it
    called something else now?"""
    base = _ir(_p("k1", "x")) | {"tab_title": "Draft"}
    before = _ir(_p("k1", "x")) | {"tab_title": "The reader's name"}
    after = _ir(_p("k1", "x")) | {"tab_title": "The source's name"}
    assert "tab_renamed" in _kinds(oracle.check(base, before, after, NOTHING))
    # Said out loud, it is no longer a loss...
    told = {"conflicts": [], "notes": ["the first tab was renamed on both sides — it "
                                       "keeps 'The reader's name', not 'x'"], "applied": []}
    assert "tab_renamed" not in _kinds(oracle.check(base, before, after, told))
    # ...and neither is a rename the reader never made.
    kept = _ir(_p("k1", "x")) | {"tab_title": "Draft"}
    assert "tab_renamed" not in _kinds(oracle.check(base, kept, after, NOTHING))


def test_the_oracle_sees_a_block_the_reader_deleted_come_back():
    """`tab_resurrected` one level down, and the commoner journey by far. Nothing of
    the reader's disappears when their deletion is undone, so every other question
    here passes it; what is gone is the decision, which the document must win."""
    base = _ir(_p("k1", "Kept."), _p("k2", "Struck out by the reader."))
    before = _ir(_p("k1", "Kept."))
    after = _ir(_p("k1", "Kept."), _p("k2", "Struck out by the reader."))
    ours = _ir(_p("k1", "Kept."), _p("k2", "Struck out by the reader."))
    assert "block_resurrected" in _kinds(oracle.check(base, before, after, NOTHING, ours))
    # Said out loud it is no longer silent — in the notes, which is where `accounted`
    # looks; "created", like "applied", says one line per block and would excuse all.
    told = {"conflicts": [], "applied": [],
            "notes": ["k2 was deleted in the document and the source still asks for it"]}
    assert "block_resurrected" not in _kinds(oracle.check(base, before, after, told, ours))
    # ...and the source dropping it too leaves nothing for the file to ask for.
    gone = _ir(_p("k1", "Kept."))
    assert "block_resurrected" not in _kinds(oracle.check(base, before, after, NOTHING, gone))


def test_the_oracle_does_not_call_a_block_the_reader_moved_a_resurrection():
    """A move in the browser is a delete and a retype, so the named range goes and the
    key with it — and the settle names the block from its own words again, exactly as
    a resurrected one would be named. The words say which it was: they never left the
    document. Without this the campaign cried wolf on 7 of 300 rounds, every one of
    them shrinking to a lone `move_block`."""
    base = _ir(_p("k1", "Kept."), _p("k2", "Dragged somewhere else."))
    before = _ir({"kind": "paragraph", "runs": [{"text": "Dragged somewhere else."}]},
                 _p("k1", "Kept."))
    after = _ir(_p("k2", "Dragged somewhere else."), _p("k1", "Kept."))
    ours = _ir(_p("k1", "Kept."), _p("k2", "Dragged somewhere else."))
    assert "block_resurrected" not in _kinds(oracle.check(base, before, after, NOTHING, ours))


def test_the_oracle_sees_a_row_the_reader_deleted_come_back():
    """The same question at the third size. A row carries no key of its own, so it is
    known by what it says — which is how the merge knows it too
    (`doc_merge._table_lines`)."""
    base = _ir(_grid([["h1", "h2"], ["ribbon", "x"]], key="t1"))
    before = _ir(_grid([["h1", "h2"]], key="t1"))            # the reader deleted it
    after = _ir(_grid([["h1", "h2"], ["ribbon", "x"]], key="t1"))
    ours = _ir(_grid([["h1", "h2"], ["ribbon", "x"]], key="t1"))
    assert "row_resurrected" in _kinds(oracle.check(base, before, after, NOTHING, ours))
    told = {"conflicts": [], "applied": [],
            "notes": ["t1: the row 'ribbon | x' the document deleted is written again"]}
    assert "row_resurrected" not in _kinds(oracle.check(base, before, after, told, ours))
    # A row whose words are still in the table is one they moved or reworded, not one
    # they deleted — the same forgiveness a block gets.
    moved = _ir(_grid([["ribbon", "x"], ["h1", "h2"]], key="t1"))
    assert "row_resurrected" not in _kinds(oracle.check(base, moved, after, NOTHING, ours))


def test_the_oracle_lets_the_source_spend_a_deleted_rows_words_elsewhere():
    """A row's words are evidence, not the row. The source may write into a row the
    reader *kept* exactly what a row they deleted used to say, and then the row left
    saying it is the source's own asking. Counted rather than looked up: the reader
    took the count to what the document shows and the source raised it by one of its
    own accord, so one row saying it is right. Chain-6 seed 260208, shrunk to a
    `delete_row` against a `collide` on the row beside it."""
    base = _ir(_grid([["thicket"], ["meadow"]], key="t1"))
    before = _ir(_grid([["meadow"]], key="t1"))          # the reader deleted 'thicket'
    ours = _ir(_grid([["thicket"], ["thicket"]], key="t1"))   # the source rewrote 'meadow'
    after = _ir(_grid([["thicket"]], key="t1"))
    assert "row_resurrected" not in _kinds(oracle.check(base, before, after, NOTHING, ours))
    # Two of them is one row more than the source ever asked for.
    twice = _ir(_grid([["thicket"], ["thicket"]], key="t1"))
    assert "row_resurrected" in _kinds(oracle.check(base, before, twice, NOTHING, ours))


def test_a_chips_face_is_not_words_the_reader_typed():
    """A chip's face is the document's to draw: the file says `Grace`, Docs renders
    `grace` off the address, and a date chip inserted again from its value comes back
    in whatever form the document spells a date in. So a block the source merely
    *moved* — a delete and a write, every chip in it made again — read as losing the
    words the reader's own Backspace had brought into it (chain-8 seed 280398). The
    chip itself is counted by what identifies it (`frozen_marks`), which is the
    question that really guards it."""
    chip = {"frozen": True, "chip": "date", "value": "2026-09-20T00:00:00Z"}
    base = _ir(_p("k1", "ask "))
    before = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "ask "}, chip | {"text": "Sep 20, 2026"}]})
    after = _ir({"key": "k1", "kind": "paragraph",
                 "runs": [{"text": "ask "}, chip | {"text": "2026-09-20"}]})
    assert "words_lost" not in _kinds(oracle.check(base, before, after, NOTHING, base))
    # The chip going is another matter, and that one is still heard.
    gone = _ir(_p("k1", "ask "))
    assert "frozen_gone" in _kinds(
        oracle.check(base, before, gone, NOTHING, _ir(*before["blocks"])))


def test_the_oracle_forgives_the_words_a_drag_glues_to_their_neighbours():
    """A reader's drag lands where they dropped it: inside the full stop of the
    paragraph before (`section.` -> `section..`), or, when the block held a chip no
    `insertText` can retype, with the gap closed (`harbour grace` -> `harbourgrace`).
    Both read as a block whose words are gone and came back. Chain-8 seeds 64057,
    64084 and 64166, each shrinking to a lone `move_block`."""
    chip = {"frozen": True, "chip": "person", "text": "", "value": "ada@example.com"}
    base = _ir(_p("k1", "Second section."),
               {"key": "k2", "kind": "paragraph",
                "runs": [{"text": "harbour"}, chip, {"text": "grace"}]})
    before = _ir(_p("k1", "Second section"),
                 {"kind": "paragraph", "runs": [{"text": "harbourgrace"}]})
    after = _ir(_p("k1", "Second section"),
                _p("k2", "harbourgrace"))
    assert "block_resurrected" not in _kinds(
        oracle.check(base, before, after, NOTHING, _ir(*base["blocks"])))
    # And a word that stands nowhere at all is still heard.
    struck = _ir(_p("k1", "Second section"))
    assert "block_resurrected" in _kinds(
        oracle.check(base, struck, after, NOTHING, _ir(*base["blocks"])))


def test_the_oracle_forgives_a_block_the_reader_dragged_away_from_a_chip():
    """The same drag, one step on: the chip was the *source's*, put there by the sync
    before, and the reader's own retype dropped it — so the base says a word the
    document has not held since. Asked of the base's text that reads as a block gone
    and come back; asked of what carries the key after the sync, which stood there
    before it and unchanged, nothing came back at all (chain-4 seed 66195)."""
    chip = {"frozen": True, "chip": "person", "text": "", "value": "grace@example.com"}
    base = _ir(_p("k1", "First."),
               {"key": "k2", "kind": "paragraph",
                "runs": [{"text": "And this follows."}, chip]})
    before = _ir(_p("k1", "First."),
                 {"kind": "paragraph", "runs": [{"text": "And this follows."}]})
    after = _ir(_p("k1", "First."), _p("k2", "And this follows."))
    assert "block_resurrected" not in _kinds(
        oracle.check(base, before, after, NOTHING, _ir(*base["blocks"])))
    # And a block that really was gone before the sync is still heard.
    struck = _ir(_p("k1", "First."))
    assert "block_resurrected" in _kinds(
        oracle.check(base, struck, after, NOTHING, _ir(*base["blocks"])))


def test_the_oracle_sees_a_tab_the_reader_deleted_come_back():
    """The mirror of `tab_gone`, and invisible to every other question here: nothing
    of the reader's disappears when a tab they deleted is created again, and the tab
    that comes back carries a new id, so an id-shaped check never notices. What is
    undone is a decision the reader made, which the document is supposed to win."""
    appendix = {"tab": "t.2", "title": "Appendix", "blocks": [_p("k2", "Later.")]}
    base = _ir(_p("k1", "x"), tabs=[appendix])
    before = _ir(_p("k1", "x"))                        # the reader deleted it
    after = _ir(_p("k1", "x"), tabs=[dict(appendix, tab="t.9")])
    ours = _ir(_p("k1", "x"), tabs=[appendix])         # the file still asks for it
    assert "tab_resurrected" in _kinds(oracle.check(base, before, after, NOTHING, ours))
    # Said out loud it is no longer silent...
    told = {"conflicts": [], "notes": ["tab 'Appendix' was created again"], "applied": []}
    assert "tab_resurrected" not in _kinds(oracle.check(base, before, after, told, ours))
    # ...nor is it a resurrection when the source gave the tab up too...
    gone = _ir(_p("k1", "x"))
    assert "tab_resurrected" not in _kinds(oracle.check(base, before, after, NOTHING, gone))
    # ...nor when what stands there now says something else under that name...
    other = _ir(_p("k1", "x"), tabs=[dict(appendix, tab="t.9",
                                          blocks=[_p("k2", "Something else.")])])
    assert "tab_resurrected" not in _kinds(oracle.check(base, before, other, NOTHING, ours))
    # ...nor when the source is asking for a fresh tab of that name. A `<section>` with
    # no `data-tab` is a tab the document never had, and two new tabs saying the same
    # thing are word-for-word twins by construction (chain-8 seed 65370: two `add_tab`
    # draws picked one name).
    asks = _ir(_p("k1", "x"), tabs=[appendix, {"title": "Appendix",
                                               "blocks": [_p("k2", "Later.")]}])
    assert "tab_resurrected" not in _kinds(oracle.check(base, before, after, NOTHING, asks))
    # One ask does not answer for two tabs, though.
    twice = _ir(_p("k1", "x"), tabs=[dict(appendix, tab="t.9"), dict(appendix, tab="t.10")])
    assert "tab_resurrected" in _kinds(oracle.check(base, before, twice, NOTHING, asks))


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


def test_two_copies_of_one_picture_and_the_source_drops_one():
    """The document holds the same picture twice and the source drops one of the two
    blocks. Which copy survived and which the file still asks for are told apart by
    their object ids, and the survivor did not keep its own — so name-matching put
    the two roles on different copies and named the one that went, with the picture
    still in the document (fresh seed 980193, chain 6). The question is how many the
    file asks for, not which."""
    def shot(oid):
        return {"frozen": True, "chip": "image", "text": "", "value": oid,
                "sha": "sha-quartz", "src": "figures/quartz.png"}

    def held(key, oid):
        return {"key": key, "kind": "paragraph", "runs": [{"text": "  "}, shot(oid)]}

    base = _ir(held("k1", "kix.i6"), held("k2", "kix.i15"))
    after = _ir(held("k2", "kix.i17"))          # rewritten, so a new object id
    ours = _ir(held("k2", "kix.i15"))           # the file dropped the first block
    assert not oracle.failures(oracle.check(base, copy.deepcopy(base), after,
                                            NOTHING, ours))
    # Both copies going is still a loss: the file asks for one of them.
    assert "frozen_gone" in _kinds(oracle.check(base, copy.deepcopy(base),
                                                _ir(_p("k2", "  ")), NOTHING, ours))


def test_the_copy_the_reader_deleted_does_not_answer_for_the_one_the_source_drops():
    """And which the file asks for is told by the object id, because the file's ids
    are the document's own: the settle regenerates it from the document it wrote, so
    a picture the file names by id is that very object and one it names by file alone
    is a picture the source has just added.

    Two copies of one figure, the reader deletes one block in the browser and the
    source drops the other. Nothing is lost by either — but the copy still standing
    paired, by their shared digest, with the file's entry for the copy the reader had
    already taken away, so nothing was excused and the picture the source itself gave
    up was named as lost (fresh seed 994410, chain 8)."""
    def shot(oid):
        return {"frozen": True, "chip": "image", "text": "", "value": oid,
                "sha": "sha-zephyr", "src": "media/zephyr.png"}

    def held(key, oid):
        return {"key": key, "kind": "paragraph", "runs": [{"text": "  "}, shot(oid)]}

    base = _ir(held("k1", "kix.i5"), held("k2", "kix.i7"))
    before = _ir(held("k2", "kix.i7"))          # the reader deleted the first block
    ours = _ir(held("k1", "kix.i5"))            # and the source dropped the second
    assert not oracle.failures(oracle.check(base, before, _ir(), NOTHING, ours))
    # The control: a file that still asks for the copy the document holds is a
    # picture that has to survive.
    assert "frozen_gone" in _kinds(oracle.check(base, before, _ir(), NOTHING,
                                                _ir(held("k2", "kix.i7"))))


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


def test_the_oracle_lets_a_token_the_reader_pared_down_alone():
    """The reader can also *delete* one of the joined words, and then the token left
    over is one of one word: `joined_differently` stopped at that and the leftover read
    as a word the reader had typed, so the source rewriting its other half — which is
    the source's own word — was called a loss (fresh-seed 91197).

    It holds nothing of theirs. `_pared_down` says exactly that: the token is a token
    of the base with the span of one of its words cut out, and nothing looser.
    """
    base = _ir(_p("k1", "a soft­hyphen here"))
    before = _ir(_p("k1", "a ­hyphen here"))                # the reader deleted "soft"
    after = _ir(_p("k1", "a ­willow here"))                 # the source reworded it
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # A word of the base the reader typed again somewhere new is still their own work.
    typed = _ir(_p("k1", "a ­hyphen here hyphen"))
    assert _kinds(oracle.check(base, typed, after, NOTHING)) == {"words_lost"}


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


def test_the_first_tab_renamed_in_the_file_is_renamed_and_stays_renamed():
    """End to end: the source names the first tab, the sync writes it, and the settle
    — which regenerates the file from the document it just wrote — reads it back. A
    rename nothing wrote would be taken back out of the file here, twice over."""
    world, ours, base = _push("tabs")
    assert ours["tab_title"] == world.tabs[0].title
    ours["tab_title"] = "Chapter one"
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert world.tabs[0].title == "Chapter one"
    assert ours["tab_title"] == "Chapter one" and base["tab_title"] == "Chapter one"
    assert any("first tab renamed 'Chapter one'" in a for a in report["applied"])
    # And the sync after it writes nothing: the three sides agree.
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_tab_the_reader_added_is_read_into_the_file_and_left_alone():
    """Somebody clicks + in the tab strip and writes in the new tab. Nobody knows of
    it — it is in neither the file nor the base — so the merge must plan nothing for
    it and the settle must read it into the file with keys and named ranges of its
    own, or the sync after would see a tab the file has and the document does not."""
    world, ours, base = _push("tabs")
    reply = world.apply([{"addDocumentTab": {"tabProperties": {"title": "Reader's tab"}}}])
    ident = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    world.apply([{"insertText": {"location": {"index": 1, "tabId": ident},
                                 "text": "A thought of my own."}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    added = [p for p in doc_ir.parts(ours) if p.get("tab") == ident]
    assert len(added) == 1 and added[0]["title"] == "Reader's tab"
    assert doc_merge.block_text(added[0]["blocks"][0]) == "A thought of my own."
    # Keyed and named: the second sync finds it again rather than reading it as new.
    assert added[0]["blocks"][0]["key"]
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_range_left_behind_by_a_join_does_not_steal_the_blocks_key():
    """The reader backspaces at the start of a paragraph and Docs merges it into the
    one above. Both named ranges are now inside the paragraph that survives, and
    `apply_keys` keeps the first — nothing is wrong, and nothing says anything is,
    until the source rewrites the words the winner covers: the delete takes the
    winner's range with it, the loser is all that is left, and the block comes back
    under the name of the paragraph that was swallowed. The file's key then names
    nothing (chain-4 seed 70140, 1 of 200 rounds at chain 4 and at chain 8, which is
    why this test exists)."""
    world, ours, base = _build([_p("a", "Results here."), _p("b", "What we found.")])
    first, second = (b["key"] for b in ours["blocks"])
    mark = ours["blocks"][0]["span"][1] - 1
    world.apply([{"deleteContentRange": {
        "range": {"startIndex": mark, "endIndex": mark + 1}}}])
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [b["key"] for b in ours["blocks"]] == [first]
    assert doc_merge.block_text(ours["blocks"][0]) == "Results here.What we found."
    # The swallowed block's range is gone from the document, not merely unread.
    assert [r["name"] for r in world.tabs[0].named] == [doc_ir.KEY_PREFIX + first]
    assert second not in [r["name"][len(doc_ir.KEY_PREFIX):] for r in world.tabs[0].named]
    # And now the source rewrites the very words the surviving range sits on.
    ours["blocks"][0]["runs"] = [{"text": "Rewritten entirely."}]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [b["key"] for b in ours["blocks"]] == [first]
    assert doc_merge.block_text(ours["blocks"][0]) == "Rewritten entirely."
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_range_left_by_a_join_is_deleted_before_the_words_it_would_steal_are_written():
    """`orphan_requests` at the settle is too late when this very sync also rewrites
    the surviving block. The reader joins a picture paragraph into an item, so both
    ranges are in the one paragraph left; the source rewords the item, and the write
    that replaces those words takes the *surviving* range with them — the picture is
    untouched, so the orphan is the only name left in the paragraph, and the read-back
    names the block after the paragraph that was swallowed. The plan's own key is then
    nowhere, and the settle plants the wrong one (chain-8 seed 77064)."""
    picture = {"kind": "paragraph", "runs": [{"chip": "image", "text": "￼"}]}
    world, ours, base = _build([_p("a", "mix"), picture])
    first = ours["blocks"][0]["key"]
    mark = ours["blocks"][0]["span"][1] - 1
    world.apply([{"deleteContentRange": {
        "range": {"startIndex": mark, "endIndex": mark + 1}}}])
    ours["blocks"][0]["runs"] = [{"text": "meadow"}]
    del ours["blocks"][1]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [b["key"] for b in ours["blocks"]] == [first]
    assert doc_merge.block_text(ours["blocks"][0]).startswith("meadow")
    assert [r["name"] for r in world.tabs[0].named] == [doc_ir.KEY_PREFIX + first]
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_block_retitled_behind_a_deleted_item_is_not_re_bulletted_by_the_settle():
    """The settle's three repairs are one batch and they must not undo each other.

    Docs merges two paragraphs keeping the first one's style, so a paragraph the
    source retitles behind an item the source deletes comes back a bulleted
    NORMAL_TEXT line. `carry_unimported` sees both, `restore_bullets` takes the
    bullet off and `unimported_requests` writes TITLE — and then `bullet_requests`,
    reading the block as the *read-back* has it, counted it in the run of items
    behind it and re-bulletted the lot, a paragraph-wide request that flattened the
    named style again (offline chain-4 seed 570181). What a run is made of is what
    the settle leaves behind, not what it found.
    """
    world, ours, base = _push("imported_list")
    by_key = {b["key"]: b for b in ours["blocks"]}
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "item:cool"]
    by_key["paragraph:follow-them-in-order"]["kind"] = "title"
    fuzz_docs.sync_once(world, ours, base)
    last = doc_world.read_ir(world, ours, base)["blocks"][-1]
    assert (last["key"], last["kind"]) == ("paragraph:follow-them-in-order", "title")


def test_the_cell_judge_waits_only_for_words_the_source_really_wrote():
    """`_cells_arrived` recognises a regrid by the grid's *size*, so a source that
    takes one row out and puts another in slips past it — and then every cell below
    the one that went reads as rewritten with the row above's words. The reader had
    deleted a different row, both deletes stood, and the merge was right (seed
    570177). A word the base already says is one a regrid shifted, not a new one."""
    seen = Counter()
    was = _table([["year", "count"], ["2024", "7"], ["2025", "9"]])
    here = _table([["year", "count"], ["2024", "7"]])       # the reader dropped a row
    shifted = _table([["2024", "7"], ["2025", "9"], ["umbrella", "thicket"]])
    then = _table([["2024", "7"], ["umbrella", "thicket"]])
    assert fuzz_docs._cells_arrived("table:year", was, here, shifted, then,
                                    {}, None, 0, seen) == []
    written = _table([["year", "count"], ["2024", "vellum"], ["2025", "9"]])
    out = fuzz_docs._cells_arrived("table:year", was, here, written, then,
                                   {}, None, 0, seen)
    assert [f["kind"] for f in out] == ["cell_lost"], out


def test_two_paragraphs_deleted_in_front_of_a_table_do_not_delete_one_range_twice():
    """A range is destroyed with its text, so a `deleteNamedRange` for one already
    gone is refused — and a refusal throws out the whole batch and kills the sync.

    `_orphan_range` asked only whether *this* delete covers the range, and a run of
    deletes hands each mark leftwards: the block in front of the table gives up its
    neighbour's mark and keeps its own, which the neighbour's own delete takes
    (offline chain-6 seed 550667). Every cut of the batch is asked now, so nothing is
    named twice.
    """
    block = {"key": "paragraph:empty", "rangeId": "nr.15", "range": [90, 91]}
    assert doc_merge._orphan_range(block, 89, 90) \
        == [{"deleteNamedRange": {"namedRangeId": "nr.15"}}]
    assert doc_merge._orphan_range(block, 89, 90, [(89, 90), (90, 99)]) == []

    world, ours, base = _build([_para("Head."), _para(""), _para(""),
                                _table([["a"]]), _para("End.")])
    ours["blocks"] = [b for b in ours["blocks"]
                      if b.get("key") not in ("paragraph:empty", "paragraph:empty#2")]
    fuzz_docs.sync_once(world, ours, base)
    assert _keys(doc_world.read_ir(world, ours, base)) \
        == ["paragraph:head", "table:a", "paragraph:end"]


def test_an_empty_paragraph_of_the_sources_own_is_never_a_tables_lead():
    """A body may not open on a table, so Docs keeps an empty paragraph in front of
    the first one and `doc_ir._hide_trailer` leaves it out of the IR. It knows it by
    its shape alone — and an empty paragraph the *file* asks for can be pushed into
    that place by a delete: the opening table goes with its lead, and the paragraph
    behind it is now the one in front of the next table.

    It then vanished out of the IR, its named range read as an orphan and was deleted,
    and the settle wrote the file with that paragraph behind the table instead — the
    source's order undone in silence, which only the campaign's own judge could see
    (offline chain-12 seed 630138). A mark somebody has planted an identity on is not
    Docs' scaffolding.
    """
    world, ours, base = _build([_table([["a"]]), _para(""), _table([["b"]])])
    first, rest = ours["blocks"][0], ours["blocks"][1:]
    ours["blocks"] = rest + [first]            # the source moves the opening table down
    fuzz_docs.sync_once(world, ours, base)
    keys = _keys(doc_world.read_ir(world, ours, base))
    assert "paragraph:empty" in keys, keys
    assert keys.index("paragraph:empty") < keys.index("table:b") < keys.index("table:a")


def test_a_paragraph_dragged_against_a_cells_word_is_not_a_word_of_the_readers():
    r"""`WORD` is `\S+`, and a cell is the one place a reader's drag can weld text
    from anywhere else in the tab onto a word standing in the table. "…after it."
    dropped against the `x` in a cell makes the token `it.x`, which is a token of
    neither the base's table nor the base's paragraph — so it read as a word the
    reader had typed, and the source rewriting its own half of it read as a loss
    (offline chain-4 seed 600784). `_welded` sees it once it is given the tab's base
    words, which is why `_words_findings` has them too.
    """
    was = _table([["h1", "h2"], ["signal", "x"]])
    here = _table([["h1", "h2"], ["signal", " and a line after it.x"]])
    then = _table([["h1", "h2"], ["signal", "\nand a line after it.meadow"]])
    tab_was = oracle.words("and a line after it. " + oracle.text_of(was))
    after = oracle.words(oracle.text_of(then))
    assert oracle._cell_findings("table:h1", here, was, then, after, {}, None,
                                 tab_was) == []
    # Without the tab's own base words the weld is invisible, and the source rewriting
    # its half of the token reads as the reader's work gone.
    assert [f["kind"] for f in
            oracle._cell_findings("table:h1", here, was, then, after, {}, None)] \
        == ["cell_words_lost"]


def test_a_table_the_source_moves_and_regrids_at_once_keeps_the_row_the_reader_deleted():
    """A move is a delete and a table built again, and what it built was the *file's*
    grid. That is the same table as the merged one until this very sync writes the
    grid: a regrid goes in a batch of its own, the base takes the new grid, and the
    round after plans the move against a file that still holds the row the reader
    deleted — so the row came back, and the report said only that the table had moved
    (chain-8 seed 74230). It builds the merged grid now, and the base of a moved table
    keeps the matching that says which of the file's lines that grid stands for."""
    world, ours, base = _build([_p("a", "a line before it."),
                                _table([["h1", "h2"], ["umbrella", "x"]]),
                                _p("b", "and a line after it.")])
    table = ours["blocks"][1]
    world.apply([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": table["span"][0]},
        "rowIndex": 1, "columnIndex": 0}}}])
    # The source moves the table to the front — one block moved, so it is the table
    # the move is written for — and gives it a row of its own.
    table["rows"].append([[_para("zephyr")], [_para("willow")]])
    ours["blocks"] = [table, ours["blocks"][0], ours["blocks"][2]]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [b["kind"] for b in ours["blocks"]] == ["table", "paragraph", "paragraph"]
    assert [[doc_merge.block_text(c[0]) for c in row] for row in ours["blocks"][0]["rows"]] \
        == [["h1", "h2"], ["zephyr", "willow"]]
    assert any("moved" in note for note in report["applied"])
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_paragraph_the_reader_pasted_twice_over_keeps_the_originals_identity():
    """Identity by words is the fallback under every part of the merge, and a reader
    pasting a paragraph is the everyday way to take it away: two blocks now say
    exactly the same thing, one named by the file and one known to nobody. The named
    one must keep its key however many copies stand beside it, the copies must be
    read into the file as blocks of their own, and a source edit to the original must
    land on the original."""
    world, ours, base = _build([_p("a", "Results here."), _p("b", "What we found.")])
    first = ours["blocks"][0]["key"]
    text = doc_merge.block_text(ours["blocks"][0])
    for _ in range(2):
        end = ours["blocks"][-1]["span"][1] - 1
        world.apply([{"insertText": {"location": {"index": end},
                                     "text": "\n" + text}}])
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [doc_merge.block_text(b) for b in ours["blocks"]] == \
        [text, "What we found.", text, text]
    # The copies are keyed apart, and the original's key never moved to one of them.
    assert ours["blocks"][0]["key"] == first
    assert len(set(_keys(ours))) == 4
    # A source edit to the original lands on the original.
    ours["blocks"][0]["runs"] = [{"text": "Results, revised."}]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [doc_merge.block_text(b) for b in ours["blocks"]] == \
        ["Results, revised.", "What we found.", text, text]
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_tab_the_source_adds_in_the_middle_is_made_in_the_middle():
    """End to end, and the one thing about tab order that *is* written: a new tab
    goes where the file puts it. It used to land at the end, and the settle then
    read that order back into the file, so the source's own placing disappeared
    twice over — the same way a rename did before `first_tab_title`."""
    world, ours, base = _push("tabs")
    assert [t.title for t in world.tabs[1:]] == ["Appendix"]
    ours["tabs"].insert(0, {"title": "Notes", "blocks": [_p("p:notes", "In between.")]})
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    assert [t.title for t in world.tabs[1:]] == ["Notes", "Appendix"]
    assert [p.get("title") for p in doc_ir.parts(ours)[1:]] == ["Notes", "Appendix"]
    assert doc_merge.block_text(doc_ir.parts(ours)[1]["blocks"][0]) == "In between."
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0
    # And nothing goes in front of the body, which is root index 0 and the one tab
    # that cannot be deleted: the world refuses it so that a planner asking would be
    # heard rather than quietly making a second body.
    with pytest.raises(doc_world.Refused):
        world.apply([{"addDocumentTab": {"tabProperties": {"title": "?", "index": 0}}}])


def test_a_tab_the_reader_deleted_stays_deleted_and_goes_out_of_the_file():
    """The other side of the strip. Document wins, so the tab does not come back —
    and its `<section>` has to leave the file and its entry the base, or the sync
    after this one reads the file as asking for a tab the document never had."""
    world, ours, base = _push("tabs")
    ident, title, count = world.tabs[1].id, world.tabs[1].title, len(world.tabs)
    assert any(p.get("tab") == ident for p in doc_ir.parts(ours))
    world.apply([{"deleteTab": {"tabId": ident}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    # By title, not by id: a tab created again to answer for the deleted one would
    # carry a new id and slip past every check that asks for the old one.
    assert [t.title for t in world.tabs].count(title) == 0
    assert len(world.tabs) == count - 1
    assert [p.get("title") for p in doc_ir.parts(ours)[1:]].count(title) == 0
    assert [p.get("title") for p in doc_ir.parts(base)[1:]].count(title) == 0
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


def test_a_tab_the_reader_deleted_takes_the_sources_changes_to_it_with_it():
    """And when the source did change that tab, the change has nowhere to go. That is
    right — the document wins — but it is a thing the person must be told, so the
    report says it rather than the file quietly losing the words."""
    world, ours, base = _push("tabs")
    ident = world.tabs[1].id
    part = [p for p in doc_ir.parts(ours) if p.get("tab") == ident][0]
    part["blocks"][0]["runs"] = [{"text": "A sentence the source rewrote."}]
    world.apply([{"deleteTab": {"tabId": ident}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    assert any("deleted in the document" in line for line in report["notes"])
    again, _, _ = fuzz_docs.sync_once(world, ours, base)
    assert again["requests"] == 0


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


def test_a_table_whose_anchor_row_the_reader_deletes_keeps_its_key():
    """The same wound from the other side, and nothing was looking at it. Every other
    repair in `doc_merge` is for a named range one of *our own* writes destroyed; this
    one a person destroys in the browser, by deleting the first row of a table — the
    one cell the range is planted in (`doc_ir.anchor_span`).

    The read-back then has a table with no key, the merge reads the key the file and
    the base both name as a table the reader deleted, and the source's own edit to a
    row the reader kept is written nowhere. `doc_merge.recover_tables` pairs the two
    again, and only where nothing is in doubt.
    """
    world, ours, base = _push("between_tables")
    grid = [b for b in ours["blocks"] if b["kind"] == "table"][1]
    assert grid["key"] == "table:c" and len(grid["rows"]) == 2
    world.apply([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": grid["span"][0]}, "rowIndex": 0}}}])
    grid["rows"][1][1][0]["runs"] = [{"text": "kestrel"}]     # the source edits a cell
    _, ours, _ = fuzz_docs.sync_once(world, ours, base)
    now = [b for b in ours["blocks"] if b["kind"] == "table"]
    assert [b["key"] for b in now] == ["table:a", "table:c"]
    assert doc_merge._match_text(now[1]) == "3 | kestrel"


def test_a_table_built_out_of_nothing_is_never_taken_for_one_that_had_words():
    """`recover_tables` asks the words and not `_match_text`, whose " | " between
    every cell is most of a small table's characters: a blank 2×2 `insertTable` had
    just built scored 0.55 against `c | d | 3 | 4` and took its key, which is the
    crossing this whole family is about — one of the two tables then holds the
    other's identity and the sync writes each one's words into the other."""
    def table(rows):
        return {"kind": "table", "rows": [[[fuzz_docs._p(c)] for c in row]
                                          for row in rows]}

    base = {"blocks": [table([["c", "d"], ["3", "4"]]) | {"key": "table:c"}]}
    theirs = {"blocks": [table([["", ""], ["", ""]])]}
    assert doc_merge.recover_tables(base, theirs) == 0
    assert theirs["blocks"][0].get("key") is None
    # And the one it is for: the reader's row delete leaves the rest of the words.
    theirs = {"blocks": [table([["3", "4"]])]}
    assert doc_merge.recover_tables(base, theirs) == 1
    assert theirs["blocks"][0]["key"] == "table:c"


def test_a_table_the_insert_put_in_front_of_its_anchor_is_found_there():
    """`insertTable` splits the paragraph it goes into, and the paragraph's named
    range stays with the half *after* the table — so a table goes in front of the
    block the plan anchored it on. `anchor_tables` looked forward only, and where the
    reader had just made the next table along anonymous (the row delete above), it
    handed that table's identity to the one `insertTable` had built: the source's rows
    were written into the reader's table and a blank one was left for the rest, with
    the reader's row gone and the report saying nothing.

    The shape is what makes it reachable: between two tables the leftover empty
    paragraph cannot be deleted at all (`restore_undeletable`), so it stays and the
    two halves stand on either side of the new table (fresh seed 970567, chain 6).
    """
    world, ours, base = _push("between_tables")
    # First the source drops the paragraph that stands between the two tables. Docs
    # will not let the mark go, so what is left there is an empty paragraph — the one
    # the next insert splits.
    ours["blocks"] = [b for b in ours["blocks"]
                      if b.get("key") != "paragraph:a-paragraph-in-between"]
    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [b.get("key") for b in ours["blocks"]] == [
        "table:a", "paragraph:empty", "table:c", "paragraph:the-end"]

    grid = [b for b in ours["blocks"] if b["kind"] == "table"][1]
    at = [i for i, b in enumerate(ours["blocks"]) if b["kind"] == "table"][1]
    world.apply([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": grid["span"][0]}, "rowIndex": 0}}}])
    ours["blocks"].insert(at, {"kind": "table", "rows": [
        [[fuzz_docs._p("h1")], [fuzz_docs._p("h2")]],
        [[fuzz_docs._p("kestrel")], [fuzz_docs._p("x")]]]})
    _, ours, _ = fuzz_docs.sync_once(world, ours, base)
    now = [b for b in ours["blocks"] if b["kind"] == "table"]
    assert [doc_merge._match_text(b) for b in now] == [
        "a | b | 1 | 2", "h1 | h2 | kestrel | x", "3 | 4"]
    assert [b["key"] for b in now] == ["table:a", "table:h1", "table:c"]


def test_a_beheaded_table_keeps_its_key_in_a_tab_the_merge_plans_nothing_for():
    """`recover_tables` in the plan repairs a tab the merge writes; this is the tab it
    does not. A tab the source deleted and the document changed is kept, and kept is
    a note and no pair at all (`doc_merge.pair_tabs`), so nothing plans that tab and
    the settle has no planned blocks to adopt from. The table the reader had beheaded
    there settled under a name made from its surviving first word — file, base and
    document agreeing on an identity the file never gave it, and the next sync
    building a second table beside it. `settle_keys` asks the base as well, and
    `name_requests` then plants the range back, so the repair reaches the document
    (fresh seeds 970705 and 970711, chain 6).
    """
    world, ours, base = _push("tabs")
    part = doc_ir.parts(ours)[1]
    grid = [b for b in part["blocks"] if b["kind"] == "table"][0]
    assert grid["key"] == "table:year" and len(grid["rows"]) == 3
    world.apply(doc_merge.on_tab([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": grid["span"][0]}, "rowIndex": 0}}}], part["tab"]))
    ours["tabs"] = [t for t in ours["tabs"] if t is not part]   # the source drops the tab
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("kept" in note for note in report["notes"])
    kept = doc_ir.parts(ours)[1]
    now = [b for b in kept["blocks"] if b["kind"] == "table"]
    assert [b["key"] for b in now] == ["table:year"]
    # And the range is really in the document: a second sync reads the key back.
    _, ours, _ = fuzz_docs.sync_once(world, ours, base)
    assert [b["key"] for b in doc_ir.parts(ours)[1]["blocks"]
            if b["kind"] == "table"] == ["table:year"]


def test_a_table_the_reader_beheaded_does_not_take_the_key_of_one_the_source_regrids():
    """The third read in a sync had nobody to recover a beheaded table for it.

    `_write_structure` writes the grid, reads the tab again and hands what it finds
    to `anchor_tables`, whose job is to name a table whose range one of our own
    requests destroyed — a regrid that deletes the row the table is anchored in. A
    table the *reader* beheaded in the browser has no range either, and that read was
    the one place `recover_tables` was not run: the free table `anchor_tables` found
    was the reader's, and it took the regridded table's key. The reader's rows then
    stood under the source's table's name, the real one settled as `table:empty`, and
    the file's key was gone (fresh seed 993608, chain 6).
    """
    world, ours, base = _push("between_tables")
    live = doc_world.read_ir(world, ours, base)
    first, second = [b for b in live["blocks"] if b["kind"] == "table"]
    # The reader empties the second table's last row and beheads the first table,
    # whose anchor cell goes with the row.
    for inner in reversed([b for row in second["rows"][1:] for cell in row for b in cell]):
        start, end = inner["span"]
        if end - 1 > start:
            world.apply([{"deleteContentRange": {
                "range": {"startIndex": start, "endIndex": end - 1}}}])
    world.apply([{"deleteTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": first["span"][0]}, "rowIndex": 0}}}])
    # And the source takes the first row off the *second* table, which takes that
    # one's range in the structural batch and leaves it blank: two tables, neither
    # named, and the one with the words in it is the reader's.
    mine = next(b for b in ours["blocks"] if b["key"] == second["key"])
    mine["rows"] = mine["rows"][1:]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    grids = [b for b in base["blocks"] if b["kind"] == "table"]
    assert [b["key"] for b in grids] == [first["key"], second["key"]]
    assert [doc_merge._table_words(b) for b in grids] == ["1 2", ""]


def test_a_paragraph_does_not_keep_the_centring_of_the_one_deleted_above_it():
    """Docs merges two paragraphs on a delete keeping the FIRST one's style, and that
    reaches the measurements, not only the named style and the bullet.

    The reader centres a paragraph; the source deletes that paragraph and restyles the
    block behind it. The merge writes that block's paragraph style with `alignment`
    named and no value — which is how a paragraph is told to follow its named style,
    the whole point of a theme — and the delete, one request later in the same batch,
    hands it the centring of the paragraph that went. It said `center` of its own from
    then on and no longer followed the document's theme, and neither side had asked
    for it (fresh seed 912452, chain 6). And the source's own restyle, written one
    request earlier, went the same way. The settle writes the plan's whole paragraph
    style back on a block this run wrote (`_unwritten`), so both come right.
    """
    world, ours, base = _push("themed")
    live = doc_world.read_ir(world, ours, base)
    above = next(b for b in live["blocks"] if b["key"] == "heading:another-heading")
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": above["span"][0], "endIndex": above["span"][1]},
        "paragraphStyle": {"alignment": "CENTER"}, "fields": "alignment"}}])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != above["key"]]
    behind = next(b for b in ours["blocks"] if b["key"] == "paragraph:and-prose-after-that")
    behind["line_spacing"] = 1.5
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    now = next(b for b in base["blocks"] if b["key"] == "paragraph:and-prose-after-that")
    assert now.get("kind") == "paragraph"     # the heading's named style, already put back
    assert now.get("line_spacing") == 1.5     # the restyle the source did ask for
    assert now.get("align") is None           # and no centring of its own, which nobody asked for


def test_a_paragraph_under_a_deleted_one_of_its_own_kind_is_not_centred_either():
    """The same wound where the two blocks agree on their named style.

    `carry_unimported`'s named-style repair does not fire then — both are
    NORMAL_TEXT — so nothing would look at the paragraph at all, and the centring
    the delete handed over would stay. `_unwritten` asks instead whether the write
    left the paragraph as the plan asked, which is a question about the
    measurements and not about the kind.
    """
    world, ours, base = _push("prose")
    live = doc_world.read_ir(world, ours, base)
    above = next(b for b in live["blocks"]
                 if b["key"] == "paragraph:the-first-paragraph-says-one-thing")
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": above["span"][0], "endIndex": above["span"][1]},
        "paragraphStyle": {"alignment": "CENTER"}, "fields": "alignment"}}])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != above["key"]]
    behind = next(b for b in ours["blocks"]
                  if b["key"] == "paragraph:the-second-paragraph-says-another")
    behind["line_spacing"] = 1.5
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    now = next(b for b in base["blocks"]
               if b["key"] == "paragraph:the-second-paragraph-says-another")
    assert now.get("line_spacing") == 1.5
    assert now.get("align") is None


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


def test_the_world_applies_every_run_field_the_merge_writes():
    """The other side of the same honesty. `doc_world` applies an `updateTextStyle`
    field by field, so a field of `doc_merge.MANAGED` it does not know is applied
    nowhere and read back never — the campaign would draw that styling on both sides
    and see it agree for the one reason that proves nothing. `baselineOffset` was
    exactly that for an afternoon."""
    assert set(doc_merge.MANAGED) <= set(doc_world.API_TO_IR)


def test_the_campaign_sees_a_restyle_that_never_arrived(monkeypatch):
    """The fourth half of the campaign's own judge, measured the way the other three
    are: put a defect in and count the rounds that say so.

    `_styling_arrived` asks the plainest question there is — the reader left the block
    word for word as the base has it and the source restyled it, so there is nothing
    to merge — and nobody was asking it. Neither other judge can: the loss oracle asks
    only about the *reader's* work, and the source's marks going nowhere costs the
    reader nothing; convergence is satisfied by any self-consistent reading, and the
    settle regenerates the file from the document it wrote, so the next sync agrees.

    The defect injected here is the merge writing the document's runs back where it
    means to write the source's, which is what dropping a restyle looks like from the
    outside and says nothing in the report.
    """
    monkeypatch.setattr(doc_merge, "_restyled",
                        lambda live, mine: [dict(r) for r in live.get("runs", [])])
    caught = 0
    for seed in range(32):
        found = fuzz_docs.offline_round(seed, 3, script=fuzz_docs.draw(seed, 3))
        caught += "restyle_lost" in {f["kind"] for f in oracle.failures(found)}
    assert caught >= 2, "the campaign no longer reaches the defect it was built for"


# ------------------------------------------------- one test per defect the campaign found

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


def _item(text, level=0):
    return {"kind": "item", "level": level, "glyphs": None, "runs": [{"text": text}]}


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


def test_a_block_whose_only_change_was_a_mark_is_not_one_the_source_may_delete():
    """A block the source drops is kept when the document changed it, and `_edited`
    asked the words, the grid and the frozen runs — never the marks.

    So a block whose only change was styling read as untouched: the delete went
    through and took the bold the reader had just put on with it, in silence
    (fresh-seed 90175 at chain 4, where `collide` drops the very block the reader
    bolded a word of). Bolding a word is a choice a reader made in the document, as
    much as typing one, and `styling_lost` says so everywhere else.
    """
    world, ours, base = _build([_para("alpha beta gamma"), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {
        "range": {"startIndex": start + 6, "endIndex": start + 10},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"].pop(0)                          # the source drops that very block
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert [(r["text"], r.get("bold")) for r in live["blocks"][0]["runs"]] == \
        [("alpha ", None), ("beta", True), (" gamma", None)]
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_table_the_reader_styled_a_cell_of_is_not_one_the_source_may_move():
    """A move is a delete and a build from nothing, and a rebuild carries words.

    `_table_movable` asked the cells' *words* alone, so a reader who small-capped
    one word of a cell — or centred it — had that taken off by a move the source
    asked for, with nothing in the report (offline chain-4 seed 3000027, shape
    `two_tables`, the first round the campaign ever styled a cell in). It is
    `_edited`'s rule at the size of a table, and it needs `_styled` and `_shapes`
    to descend into the cells, a table being what its cells are.
    """
    for edit in ("mark", "shape"):
        world, ours, base = _build([_para("One."), _table([["a", "b"]]), _para("Two."),
                                    _para("Three."), _para("Four.")])
        cell = doc_world.read_ir(world, ours, base)["blocks"][1]["rows"][0][1][0]
        low, high = cell["span"]
        world.apply([{"updateTextStyle": {
            "range": {"startIndex": low, "endIndex": high - 1},
            "textStyle": {"smallCaps": True}, "fields": "smallCaps"}}]
            if edit == "mark" else
            [{"updateParagraphStyle": {"range": {"startIndex": low, "endIndex": high},
                                       "paragraphStyle": {"alignment": "CENTER"},
                                       "fields": "alignment"}}])
        was, mine = copy.deepcopy(base), copy.deepcopy(ours)
        ours["blocks"].insert(3, ours["blocks"].pop(1))       # the source moves it
        before = doc_world.settled_ir(world, ours, base)
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
        assert not oracle.failures(oracle.check(was, before, base, report, mine))
        live = doc_world.read_ir(world, ours, base)
        assert [b["kind"] for b in live["blocks"]] == \
            ["paragraph", "table", "paragraph", "paragraph", "paragraph"], edit
        kept = live["blocks"][1]["rows"][0][1][0]
        assert (kept["runs"][0].get("smallcaps") if edit == "mark"
                else kept.get("align")) == (True if edit == "mark" else "center"), edit
        assert any("the document changed it" in note for note in report["notes"]), edit
        again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
        assert again["applied"] == [], edit


def test_a_style_written_in_a_cell_stays_out_of_the_paragraph_after_the_table():
    """The harness's own, and the reason nothing had ever caught it: a range aimed at
    a cell.

    `doc_world.paragraphs` took a paragraph's first index to be its mark less
    everything since the mark before it, tables included — so the paragraph standing
    after a table began, as far as the world was concerned, at the table's own start,
    and `_paragraphs` answered a range inside any cell with that paragraph too. One
    source restyle of one cell then took the alignment a reader had given it, and a
    `createParagraphBullets` inside a cell would have bulleted it. A table ends the
    paragraph in front of it (`documents.get` says so, and the named range this world
    plants starts after the table), which is `_own`. Offline chain-10 seed 3400048,
    shrunk to one step: the reader centres the paragraph after two tables, the source
    gives a cell a bottom rule.
    """
    world, ours, base = _build([_para("Before."), _table([["a", "b"]]), _para("After.")])
    live = doc_world.read_ir(world, ours, base)
    after, cell = live["blocks"][2], live["blocks"][1]["rows"][0][1][0]
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": after["span"][0], "endIndex": after["span"][1]},
        "paragraphStyle": {"alignment": "CENTER"}, "fields": "alignment"}}])
    assert doc_world.read_ir(world, ours, base)["blocks"][2]["align"] == "center"
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": cell["span"][0], "endIndex": cell["span"][1]},
        "paragraphStyle": {"alignment": "END"}, "fields": "alignment"}}])
    live = doc_world.read_ir(world, ours, base)
    assert live["blocks"][1]["rows"][0][1][0]["align"] == "right"
    assert live["blocks"][2]["align"] == "center"


def test_a_bold_the_source_grew_over_the_word_before_it_is_written():
    """`marks_of` drops a block's words so that a chip splitting a run says nothing
    about marks — and drops the boundaries with them.

    So a source that takes its bold one word further left asks for a restyle whose
    alternation of mark sets is the one that was already there, the merge read the
    file as saying nothing, and the sync wrote nothing and reported nothing
    (offline chain-10 seed 2040246, shape `equations`). `_remarked` asks the words
    both sides have, which is where a boundary lives.
    """
    world, ours, base = _build([_para("the value holds everywhere"), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {                    # the reader bolds the last word
        "range": {"startIndex": start + 16, "endIndex": start + 26},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, doc_world.settled_ir(world, mine, was),
                                            base, report, mine))
    # The source now asks for the bold one word wider, and for nothing else at all.
    ours["blocks"][0]["runs"] = [{"text": "the value"},
                                 {"text": " holds everywhere", "bold": True}]
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert [(r["text"], r.get("bold")) for r in live["blocks"][0]["runs"]] == \
        [("the value", None), (" holds everywhere", True)]
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_word_the_reader_marked_is_not_a_restyle_when_the_source_only_rewords():
    """And the noise `_remarked` must not make: it is asked of the words both sides
    have, so a source that only rewords says nothing about marks.

    Were it asked of the text as it stands, a reader who bolds a word in a
    paragraph the source is rewording elsewhere would come back as both sides
    restyling it — a note in the report about a restyle the source never asked
    for, and the merge's word-by-word path taken for nothing.
    """
    world, ours, base = _build([_para("alpha beta gamma"), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    start = part["blocks"][0]["span"][0]
    world.apply([{"updateTextStyle": {
        "range": {"startIndex": start + 6, "endIndex": start + 10},
        "textStyle": {"bold": True}, "fields": "bold"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"][0]["runs"][0]["text"] = "alpha beta epsilon"
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    assert not [note for note in report["notes"] if "restyled" in note]
    live = doc_world.read_ir(world, ours, base)
    assert [(r["text"], r.get("bold")) for r in live["blocks"][0]["runs"]] == \
        [("alpha ", None), ("beta", True), (" epsilon", None)]


def test_a_block_the_reader_only_centred_is_not_one_the_source_may_delete():
    """And the same one size up: how the paragraph is *set*.

    `_edited` learned to ask the marks (the test above) and still did not ask
    `_shape` — the block's kind, its heading or list level, its bullets'
    ordered-ness, its alignment, its indents, its spacing, its shading, its rules.
    A reader who centres a line has chosen something as deliberate as bolding a
    word, and the whole of that choice lives in properties no word of the block
    carries, so a block changed that way and no other read as untouched and the
    source's delete took it, with nothing in the report (offline chain-6 seed
    2050019, shape `imported_list`: the reader centres `item:bake` while the same
    step's source drops it).
    """
    world, ours, base = _build([_para("alpha beta gamma"), _para("The end.")])
    part = doc_world.read_ir(world, ours, base)
    start, end = part["blocks"][0]["span"]
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": start, "endIndex": end},
        "paragraphStyle": {"alignment": "CENTER"}, "fields": "alignment"}}])
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    ours["blocks"].pop(0)                          # the source drops that very block
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert [doc_merge.block_text(b) for b in live["blocks"]] == \
        ["alpha beta gamma", "The end."]
    assert live["blocks"][0].get("align") == "center"
    assert any("dropped by the source but edited in the document" in note
               for note in report["notes"])
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


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


def test_a_block_appended_where_a_table_is_now_last_goes_after_it_not_into_it():
    """A body may not end on a table, so the paragraph after a final one keeps its
    mark however it is deleted (`_delete_range`): its words go and an empty paragraph
    stays exactly where it stood. But the append index was taken from the last block
    the sync *keeps*, which is then the table — and a table's own last index is inside
    its last cell, so the new block was written into the table, swallowing it and the
    table's named range with it (chain-8 seed 189, shrunk: the source drops the last
    paragraph and adds one in the same step).

    That empty paragraph is a trailer like the one a body ending on a table already
    has, and the first block appended is written into it.
    """
    world, ours, base = _build([_table([["key", "value"]]),
                                _para("The table above says it all.")])
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] == "table:key"]
    ours["blocks"].append(_para("The source added thicket."))
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert _keys(live) == ["table:key", "paragraph:the-source-added-thicket"]
    assert oracle.text_of(live["blocks"][0]) == "key value"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_range_a_reader_stretched_over_the_next_block_does_not_take_its_key():
    """The other way a range drifts, and the last of `lost-key`: a reader who presses
    Enter in the middle of a paragraph — or, as the campaign did it, drags a block
    into one — writes inside that paragraph's named range, which grows, so one name
    now covers both halves.

    Nothing shows while both stand: `apply_keys` gives the name to the first, which is
    where the text it was given to still is, and the second is keyed by its words at
    the settle. But when the source then drops that first block, the delete takes only
    its own span, the stretched range lives on over the second block's words, and the
    second block reads back under the first one's key — its own key gone with neither
    side dropping the block (campaign seeds 279 and 361 at chain 4).

    `anchor_range` was built for the mirror image (a range must not end *on* the mark,
    or deleting the newline between two paragraphs stretches it), so `replant_requests`
    plants a range that ends past its block again, as it does one that starts too late.
    """
    world, ours, base = _build([_para("A line."), _para("A closing line.")])
    closing = doc_world.read_ir(world, ours, base)["blocks"][-1]
    world.apply([{"insertText": {"location": {"index": closing["span"][0] + 5},
                                 "text": "\n"}}])       # Enter, in the middle of it
    stretched = doc_world.read_ir(world, ours, base)["blocks"][-2]
    assert stretched["range"][1] > doc_ir.anchor_range(stretched)[1], \
        "the split has to grow the range for this to be the case it is about"
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(ours) == ["paragraph:a-line", "paragraph:a-closing-line",
                           "paragraph:sing-line"]
    ours["blocks"] = [b for b in ours["blocks"]
                      if b["key"] != "paragraph:a-closing-line"]
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    assert _keys(doc_world.read_ir(world, ours, base)) == [
        "paragraph:a-line", "paragraph:sing-line"]


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


def test_an_empty_paragraph_a_new_tables_swallow_unnames_keeps_its_key():
    """`insertTable` leaves an empty paragraph in front of the table, which
    `_new_table_requests` gets rid of by deleting the mark of the block before — and
    a block that is *itself* an empty paragraph is all mark, so the delete takes its
    named range whole. Being keyed again from its words at the settle is too late:
    a structural batch is followed by a re-plan against the document it just wrote,
    and there the file's key names nothing, so the block reads as one the reader
    deleted and everything the source asks of it is dropped in silence. Here that is
    a chip, and the round converges with file, base and document all agreeing on an
    empty paragraph (offline chain-4 seed 330127)."""
    chip = {"frozen": True, "chip": "person", "text": "Grace", "value": "grace@example.com"}
    world, ours, base = _build([_para("Head."), _para(""), _para("Tail.")])
    ours["blocks"][1]["runs"] = [chip]
    ours["blocks"].insert(2, _table([["h1", "h2"]]))
    for _ in range(2):                       # a grid is built on one pass, filled on the next
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(base) == ["paragraph:head", "paragraph:empty", "table:h1",
                           "paragraph:tail"]
    assert list(oracle.frozen_marks(base["blocks"][1])) == [("person", "grace@example.com")]


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


def test_a_table_the_source_moves_right_behind_another_is_left_where_it_is():
    """Docs keeps an undeletable paragraph between two tables, so a file asking for
    two with nothing between them asks for what the document cannot hold: the
    `insertTable` splits the paragraph it goes to and the half in front of the new
    table *is* that mandatory paragraph. Nothing then says which of the two empty
    paragraphs is which — and where the file's own is the body's last it is hidden
    altogether — so the settle keyed the leftover with its name, the order read as the
    base's, the move was undone in silence, and the `align` the source gave that
    paragraph in the same run was planned onto nothing next time round (chain-8 seed
    280039). `refuse_back_to_back` leaves the table where the document has it and the
    report says why."""
    world, ours, base = _push("between_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at - 1, ours["blocks"].pop(at))   # right behind `table:a`
    ours["blocks"][at]["align"] = "justify"                # the paragraph it passed
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("right behind another table" in note for note in report["notes"]), report
    assert _keys(base) == ["table:a", "paragraph:a-paragraph-in-between", "table:c",
                           "paragraph:the-end"]
    assert base["blocks"][1]["align"] == "justify"


def test_a_table_whose_move_structure_refuses_is_no_anchor_where_the_file_has_it():
    """`structure` refuses a move of its own — a table the file puts where the
    document has no paragraph to write in — and it was the one of the three refusals
    that did not put the block back. So `merged` went on saying the table stood
    where the file wants it while its span said the other end of the document, and
    every index the sync computes comes from a span: the paragraph the source moved
    *in front of* that table took it for an anchor, was written at its span instead,
    and came out exactly where it already was. The move was undone in silence, and
    on the next pass the merge planned it again (offline chain-4 seed 380191, found
    by `fuzz_docs._order_arrived`).

    Pinned as the invariant rather than as that seed's story, which the two order
    fixes made after it reach by another road: a refusal puts the block back, all
    three of them, because every index the sync computes comes from a span."""
    world, ours, base = _build([_table([["a", "b"]]), _para("Mid."),
                                _table([["c", "d"]]), _para("End.")])
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(0, ours["blocks"].pop(at))   # in front of the table it opens on
    result = doc_merge.plan(base, ours, doc_world.read_ir(world, ours, base))
    assert any("no paragraph to write in" in note for note in result["notes"]), result
    assert [b.get("key") for b in result["blocks"]] == [
        "table:a", "paragraph:mid", "table:c", "paragraph:end"]


def test_a_block_the_file_puts_behind_a_table_that_cannot_move_is_said_to_stay_too():
    """A source that moves a section moves its blocks one by one, and where the first
    of them cannot be moved — a table the reader has regridded is built again blank,
    so it is left where the document has it — every block behind it is placed after
    it and lands exactly where it already was. `_apply_source_moves` then took the
    silent branch: a move whose two ends are one place is no move, which is true and
    is not the reason here. The report named the table and nothing said the rest of
    the section had stayed with it (offline chain-10 seed 450252, found by
    `fuzz_docs._order_arrived`)."""
    world, ours, base = _build([_para("Status today."), _table([["h1", "h2"]]),
                                _para(""), _para("One."), _para("Two."), _para("Three.")])
    live = doc_world.read_ir(world, ours, base)
    row = [b for b in live["blocks"] if b["kind"] == "table"][0]["rows"][0]
    world.apply([{"insertText": {"location": {"index": row[0][0]["span"][0]}, "text": "x"}}])
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:h1"][0]
    ours["blocks"] += [ours["blocks"].pop(at), ours["blocks"].pop(at)]   # table + empty
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(base) == ["paragraph:status-today", "table:h1", "paragraph:empty",
                           "paragraph:one", "paragraph:two", "paragraph:three"]
    assert any("left where the document has it" in note for note in report["notes"]), report
    assert any("not where the file has it" in note for note in report["notes"]), report


def test_an_item_written_from_nothing_cannot_be_given_its_nesting_level():
    """No request sets a bullet's nesting level: `createParagraphBullets` says nothing
    about one and Docs reads it off leading tabs the merge does not write. So an item
    the source moves — a move being a delete and a write — comes out at the level of
    the list it lands in, and nothing else could see it: the reader left the block
    alone, so the loss oracle has no question, and the base agrees with the document
    afterwards, so the round converges (offline chain-4 seed 430296)."""
    world, ours, base = _push("prose")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "item:gamma"][0]
    ours["blocks"].insert(1, ours["blocks"].pop(at))      # the nested item, up front
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("written from nothing as a list item" in note
               for note in report["notes"]), report


def test_an_item_whose_mark_a_delete_hands_over_says_the_level_it_comes_out_at():
    """The same loss by the other door, and this one takes no move of its own: Docs
    merges two paragraphs keeping the first one's style, so the block behind a deleted
    one wears the deleted one's. The named style, the bullet and the measures are all
    put back by the settle, and the level alone cannot be — a nested item behind an
    item the source moved away came out at the moved one's level (offline chain-4 seed
    430587)."""
    world, ours, base = _push("prose")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "item:beta"][0]
    ours["blocks"].append(ours["blocks"].pop(at))         # `beta`, level 0, to the end
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("it comes out at level 0, not at 1" in note
               for note in report["notes"]), report
    assert [b["key"] for b in base["blocks"] if b["kind"] == "item"] \
        == ["item:alpha", "item:gamma", "item:beta"]


def test_an_item_appended_behind_a_deeper_one_says_so_although_it_asks_for_level_0():
    """The level a block written from nothing comes out at can as easily be deeper
    than the source asks for as shallower — and the note fired only for an item asking
    to be nested, so the commoner half was silent: a level-0 item appended behind a
    nested one comes out nested (offline chain-8 seed 530265, where the source moved
    an item to the end of a document whose last item the reader had indented).

    Nothing follows it, so this is the one shape that really does wear the style of
    the block in *front* — and the live level is asserted beside the note, because a
    note nobody checks against the document is a prediction that can go quietly
    wrong."""
    world, ours, base = _build([_item("alpha"), _item("beta"), _item("gamma", 1)])
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "item:alpha"][0]
    ours["blocks"].append(ours["blocks"].pop(at))           # level 0, past level 1
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("it comes out at level 1, the level of the item in front of it, not at 0"
               in note for note in report["notes"]), report
    live = doc_world.read_ir(world, ours, base)
    assert [(b.get("key"), b.get("level")) for b in live["blocks"]] == \
        [("item:beta", 0), ("item:gamma", 1), ("item:alpha", 1)]


def test_an_item_written_in_front_of_a_nested_one_takes_that_ones_level():
    """Which list a block written from nothing lands in is which way round it goes in.
    The usual way is "text\\n" at the start of the block that follows it: Docs splits
    that paragraph and the new block — the half in front — keeps the style that was
    already there, bullet and nesting level among it. Only where nothing follows, or a
    table does, is it the block in *front* whose style it wears.

    `unwritten_levels` said the second of those for every case, so it predicted the
    wrong level for the commonest one and said nothing at all: a level-0 item the
    source moved to just in front of a nested one came out nested, the reader having
    touched neither (offline chain-12 seed 1640036, shape `tabs`, shrunk to two
    appends and a restyle-and-move with no reader in it)."""
    world, ours, base = _build([_item("alpha"), _item("beta"), _para("A line."),
                                _para("And another."), _item("gamma", 1),
                                _para("Tail.")])
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "item:beta"][0]
    ours["blocks"].insert(3, ours["blocks"].pop(at))        # beta, to just before gamma
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert any("it comes out at level 1, the level of the item it is written in front "
               "of, not at 0" in note for note in report["notes"]), report
    live = doc_world.read_ir(world, ours, base)
    assert [(b.get("key"), b.get("level")) for b in live["blocks"] if b["kind"] == "item"] \
        == [("item:alpha", 0), ("item:beta", 1), ("item:gamma", 1)]
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_one_block_sent_past_a_list_is_one_move_and_not_every_block_it_passed():
    """`_moved_keys` is the longest common *subsequence*, not `SequenceMatcher`'s
    matching blocks, which are contiguous: a closing paragraph sent to the front made
    every block it passed read as moved instead. Four moves where one would do — and
    the table among them, whose move was then refused because the reader had regridded
    it, so the order came out neither side's with nothing saying so (offline chain-6
    seed 400186)."""
    was = ["a", "b", "c", "d", "e"]
    assert doc_merge._moved_keys(was, ["e", "a", "b", "c", "d"]) == ["e"]
    assert doc_merge._moved_keys(was, ["b", "c", "d", "e", "a"]) == ["a"]
    assert doc_merge._moved_keys(was, ["d", "e", "a", "b", "c"]) == ["d", "e"]


def test_a_mark_the_reader_moved_to_another_word_survives_a_source_restyle():
    """`marks_of` is a sequence of distinct mark *sets* with no words in it, so a
    reader who takes a mark off one word and puts it on another says nothing to it:
    the merge reads the restyle as the source's alone and `_restyled_words` runs. It
    then wrote the file's styling on every word the file also has — including the
    word the reader had just marked, whose styling the file has no opinion about. The
    file's styling is written only where the source really *changed* it (offline
    chain-6 seed 400044)."""
    world, ours, base = _build([{"kind": "paragraph", "runs": [
        {"text": "alpha "}, {"text": "bravo", "bold": True},
        {"text": " charlie delta"}]}])
    start = doc_world.read_ir(world, ours, base)["blocks"][0]["span"][0]
    world.apply([{"deleteContentRange": {                    # `bravo `, the bold run
        "range": {"startIndex": start + 6, "endIndex": start + 12}}},
        {"updateTextStyle": {                                # and `charlie` bold now
            "range": {"startIndex": start + 6, "endIndex": start + 13},
            "textStyle": {"bold": True}, "fields": "bold"}}])
    ours["blocks"][0]["runs"] = [
        {"text": "alpha "}, {"text": "bravo", "bold": True},
        {"text": " charlie "}, {"text": "delta", "italic": True}]
    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    runs = [(r["text"], r.get("bold"), r.get("italic"))
            for r in base["blocks"][0]["runs"]]
    assert runs == [("alpha ", None, None), ("charlie", True, None),
                    (" ", None, None), ("delta", None, True)], runs


def test_a_block_the_source_adds_behind_one_it_moves_goes_with_it():
    """A block the source adds is placed after the block the file puts it behind, and
    a move does not carry what stands behind it — so the additions have to wait for
    the moves. A picture added behind a list item the same source moved was left where
    the item had been, and the block after *it* then read as standing in its place
    already, so its own move was refused as one whose two ends are one place: the order
    came out neither side's and nothing said so (offline chain-6 seed 400186)."""
    world, ours, base = _build([_para("One."), _para("Two."), _para("Three.")])
    ours["blocks"].insert(0, ours["blocks"].pop())              # `Three.` to the front
    ours["blocks"].insert(1, _para("Right behind it."))
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert _keys(base) == ["paragraph:three", "paragraph:right-behind-it",
                           "paragraph:one", "paragraph:two"]


def test_two_tables_after_one_anchor_are_told_apart_by_what_they_say():
    """A table the source adds in front of one it regrids shares its anchor, and
    `shaped`'s order then decided which was which — although what the batch did with
    them is the requests' order and not that one.

    They came out crossed: the regridded table's key went on the blank table
    `insertTable` had just built and the new table's key on the one with all the words
    in it. The base took each other's content, so the next round read the real table
    as one the source had moved and emptied it — the reader's cells and the source's
    both gone (fresh-seed 40204 at chain 4). A table built from nothing is blank and a
    regridded one still says what it said, so the words are asked first
    (`_blank_table`) and `shaped`'s order is only the tie-break it always was.
    """
    world, ours, base = _build([_para("Before both."),
                                _table([["a", "b"], ["1", "2"]]), _para("After both.")])
    grid = next(b for b in ours["blocks"] if b["key"] == "table:a")
    grid["rows"] = grid["rows"][1:]      # the source drops the row table:a is anchored
    ours["blocks"].insert(1, _table([["h1", "h2"], ["harbour", "x"]]))   # in, and adds
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)                 # one in front
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    for _ in range(2):        # a grid is built on one pass and filled on the next
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
    live = doc_world.read_ir(world, ours, base)
    assert [(b["key"], oracle.text_of(b))
            for b in live["blocks"] if b["kind"] == "table"] == \
        [("table:h1", "h1 h2 harbour x"), ("table:a", "1 2")]


def test_a_table_moved_behind_one_the_same_batch_regrids_is_found_again():
    """`anchor_tables` names the tables a structural batch built by what they follow —
    and one of those anchors may be a table the same batch has just stripped of its
    key, since a regrid that deletes row 0 takes the cell the table is anchored in.

    The pass went through `shaped` once, in order: the moved table's anchor was not
    there yet, the regrid put it back a moment later, and nothing looked again. The
    moved table stayed blank and unkeyed, the re-plan read the key the file still
    names as a table the *reader* had deleted, and the sync wrote its words nowhere —
    a whole table of the source's gone with no conflict and no note (fresh-seed 90190
    at chain 4, shrunk to one step). It runs to a fixed point now.
    """
    world, ours, base = _push("between_tables")
    grid = next(b for b in ours["blocks"] if b["key"] == "table:a")
    grid["rows"] = grid["rows"][1:]                       # the source deletes row 0
    moved = ours["blocks"].pop(next(i for i, b in enumerate(ours["blocks"])
                                    if b["key"] == "table:c"))
    ours["blocks"].insert(1, moved)                       # and moves table:c up to it
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert [k for k in _keys(live) if k.startswith("table:")] == ["table:a", "table:c"]
    tables = [b for b in live["blocks"] if b["kind"] == "table"]
    assert oracle.text_of(tables[0]) == "1 2"             # the row is gone
    assert oracle.text_of(tables[1]) == "c d 3 4"         # and the words came along
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_move_the_merge_takes_back_leaves_the_block_where_the_document_has_it():
    """"Left where the document has it" has to be true of the merged list too.

    An empty paragraph between two tables can be deleted in no way at all, so a move
    of it is taken back (`restore_undeletable`; `refuse_nowhere` takes back the other
    kind). Both only cleared `moved` and left the block at the *file's* position — and
    every index the sync computes comes from a block's span, so the table in front of
    it was written at that paragraph's old index, which is where the table already
    stood. The document came back unchanged, the next round planned the same move,
    and the three rounds `_write_structure` allows ran out with the table blank, its
    words nowhere and an empty paragraph left over from each attempt (fresh-seed
    40344 at chain 4).
    """
    world, ours, base = _build([_para("A paragraph in between."),
                                _table([["a", "b"], ["1", "2"]]), _para(""),
                                _table([["c", "d"], ["3", "4"]]), _para("The end.")])
    assert _keys(ours)[2] == "paragraph:empty", _keys(ours)
    # The source moves the table and the empty paragraph behind it up to the front,
    # and adds a table where they came from — which is what makes the merge carry the
    # table rather than the paragraph the other way.
    ours["blocks"][:3] = [ours["blocks"][1], ours["blocks"][2], ours["blocks"][0]]
    ours["blocks"].insert(3, _table([["h1", "h2"], ["harbour", "x"]]))
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert oracle.text_of(next(b for b in live["blocks"] if b["key"] == "table:a")) \
        == "a b 1 2"
    assert sum(1 for b in live["blocks"] if not oracle.text_of(b)) == 2, \
        "one empty paragraph per attempt at the move would be left over"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_kept_paragraph_does_not_follow_the_table_the_source_moves_away_from_it():
    """The other half of it. A block the source dropped between two tables is kept
    where the document has it (`restore_undeletable`), and `_after_live` puts it back
    into the merged list behind the block in front of it there — which may be the very
    table the source is moving somewhere else.

    The kept paragraph then stood in the merged order as the table's own next block,
    so `_insert_index` read the table's new place off a span that is right behind
    where the table already was: it was deleted and built again in its own place,
    blank, and the next two passes did it again. The words were written nowhere and
    the file's key was gone, with nothing in the report (fresh-seed 994424 at chain 8,
    shrunk). A block kept because nothing can move it follows nothing that moves.
    """
    world, ours, base = _build([_para("Before both."), _para("Quartz next."),
                                _table([["h1", "h2"], ["thicket", "x"]]), _para(""),
                                _table([["a", "b"], ["1", "2"]]), _para("After both.")])
    assert _keys(ours)[3] == "paragraph:empty", _keys(ours)
    ours["blocks"].pop(3)                            # the source drops it, and moves
    ours["blocks"].insert(1, ours["blocks"].pop(2))  # the table up past the paragraph
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    # The paragraph is kept on the pass that moves the table and deleted on the next,
    # where it stands between two ordinary blocks and has a mark to borrow again.
    assert _keys(live) == ["paragraph:before-both", "table:h1", "paragraph:quartz-next",
                           "table:a", "paragraph:after-both"]
    assert oracle.text_of(live["blocks"][1]) == "h1 h2 thicket x"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


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


def test_a_table_anchored_on_an_empty_paragraph_the_batch_swallows_is_found_again():
    """`insertTable` in front of an ordinary block leaves an empty paragraph, which
    `_new_table_requests` gets rid of by deleting the mark of the block before —
    Docs' merge-on-delete keeps the first one's style, so both blocks come out as the
    file has them. Unless that block is *itself* an empty paragraph, which is all
    mark: the delete then covers its named range whole and it comes back unnamed.

    Which is nothing by itself — the settle keys it again from its words — but the
    new table is found between the batch and the settle, by the key of the block it
    follows, and that key was this one. `anchor_tables` found no anchor, the blank
    table settled under a name made from its own emptiness, the re-plan read the key
    the file still names as a table the reader had deleted, and the source's table
    was gone with all its words (fresh-seed 501271 at chain 6). `_swallowed` names
    the block the batch is about to unname and `_after_key` looks past it.
    """
    world, ours, base = _build([_para("Before it."), _para(""), _para("After it.")])
    assert _keys(ours)[1] == "paragraph:empty", _keys(ours)
    at = _keys(ours).index("paragraph:after-it")
    ours["blocks"].insert(at, _table([["h1", "h2"], ["harbour", "x"]]))
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    for _ in range(2):        # a grid is built on one pass and filled on the next
        report, ours, base = fuzz_docs.sync_once(world, ours, base)
    live = doc_world.read_ir(world, ours, base)
    assert _keys(live) == ["paragraph:before-it", "paragraph:empty", "table:h1",
                           "paragraph:after-it"]
    assert oracle.text_of(live["blocks"][2]) == "h1 h2 harbour x"


def test_a_source_move_onto_the_place_the_document_already_has_is_not_written():
    """`_moved_keys` reads the file's order against the *base*, but the merged order
    is the document's — so a block the source moved can come out exactly where the
    document already has it, and the move is a delete and a build from nothing for
    no gain at all.

    For a table that is destructive: it is built again blank and its words wait for
    the next pass, which asks for the same move again, since nothing changed. The
    three rounds `_write_structure` allows ran out with the table still blank and its
    words nowhere (fresh-seed 501429 at chain 6); a paragraph merely lost its key
    (fresh-seed 500077). A move whose two ends are one place is no move.
    """
    world, ours, base = _build([_para("Status today."), _para("Harbour next."),
                                _para("A line after it."),
                                _table([["h1", "h2"], ["harbour", "x"]]),
                                _para("Kestrel last.")])
    keys = _keys(ours)
    ours["blocks"] = [ours["blocks"][keys.index(k)] for k in
                      ["paragraph:a-line-after-it", "paragraph:harbour-next", "table:h1",
                       "paragraph:status-today", "paragraph:kestrel-last"]]
    ours["blocks"].insert(2, _para("The source added zephyr."))
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert oracle.text_of(next(b for b in live["blocks"] if b["kind"] == "table")) \
        == "h1 h2 harbour x"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_a_block_moved_to_the_end_past_a_table_goes_after_it_not_into_it():
    """The move half of `test_a_block_appended_where_a_table_is_now_last_...`: the
    append index is the mark of the last block the sync *keeps*, and a block the
    source moved away is not kept either. Move everything after a table to somewhere
    in front of it and the last kept block is the table, whose own last index is
    inside its last cell — so the moved block was written into the table, which
    swallowed it and took its key.

    The body cannot end on a table, so the last block's mark stays behind however it
    goes (`_delete_range`), and that leftover empty paragraph is the trailer. Found
    by sweeping every reordering of one five-block body.
    """
    world, ours, base = _build([_para("Alpha one."), _para("Beta two."),
                                _table([["a", "b"], ["1", "2"]]),
                                _para("Delta four."), _para("Echo five.")])
    keys = _keys(ours)
    ours["blocks"] = [ours["blocks"][keys.index(k)] for k in
                      ["paragraph:delta-four", "paragraph:echo-five", "paragraph:beta-two",
                       "table:a", "paragraph:alpha-one"]]
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert not oracle.failures(oracle.check(was, before, base, report, mine))
    live = doc_world.read_ir(world, ours, base)
    assert _keys(live) == ["paragraph:delta-four", "paragraph:echo-five",
                           "paragraph:beta-two", "table:a", "paragraph:alpha-one"]
    assert oracle.text_of(live["blocks"][3]) == "a b 1 2"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["applied"] == []


def test_the_oracle_forgives_nothing_for_punctuation_a_reader_cannot_move():
    """There were two more forgivenesses here — `_dressed_up`, for a base word wearing
    a full stop the reader had pushed against it (`.1` in a cell, fresh-seed 500249),
    and `_undressed`, for one whose stop a drag had carried away (`section` where the
    base said `section.`, chain-4 seed 76101). Both were written for damage the
    *harness* was doing: `read_move_block` read its drop index off the document before
    the cut, so a drag landed some way past where the reader let go, inside a word.

    The drop is a paragraph mark now and nothing a drag inserts can land inside a token
    at all, so neither excuse fires any more — 0 of 800 rounds at chain 4, 0 of 500 at
    chain 8, 0 of 400 at chain 12 and 0 of the 60 regression seeds — and both are gone.
    A forgiveness for something that no longer happens is a blind spot waiting, which
    is what `_twin_unmarks` was; this test is here to say that a bare word is a word
    again. The two that remain, `_welded` and `_pared_down`, are for edits a reader
    really makes, and their own tests follow."""
    assert not hasattr(oracle, "_dressed_up") and not hasattr(oracle, "_undressed")
    was = oracle.words("a b 1 2 kestrel")
    assert not oracle.joined_differently(".1", oracle.words("a b .thicket 2"), was)
    was = oracle.words("Second section. and kestrel. stands")
    assert not oracle.joined_differently("section", oracle.words("Second kestrel."), was)


def test_the_oracle_lets_two_base_words_a_join_welded_together_go():
    r"""A reader who joins two paragraphs welds the last token of the first to the
    first token of the second with nothing between them, and `theirs - was` compares
    whole tokens, so `that.A` reads as a word they typed. Both its words are the
    base's and the source may rewrite either — chain-8 seed 93212, where `collide`
    made `that.` into `vellum.` and the merge said `vellum.A`, the reader's join and
    the source's wording both in it.

    `joined_differently`'s general rule covers this whenever both halves carry a word
    of two letters or more; `_split` drops a one-letter piece, so a paragraph
    beginning "A" falls straight through it. The other half has to be measured against
    the *tab's* base and not the block's: a join is the one reader edit that makes one
    token out of two blocks."""
    was = oracle.words("And prose after that. A line the source can move.")
    after = oracle.words("And prose after vellum.A line the source can move.")
    assert oracle.joined_differently("that.A", after, was, was)
    assert not oracle.joined_differently("that.Z", after, was, was), \
        "`Z` is no token of the base: a word the reader typed"
    assert not oracle.joined_differently("line.A", after, was, was), \
        "both halves still stand in the tab, so nothing made this token disappear"


# ---------------------------------------------------------------- the third judge

def _grid_block(key: str, rows: list[list[str]]) -> dict:
    return {"kind": "table", "key": key,
            "rows": [[[{"kind": "paragraph", "runs": [{"text": text}]}] for text in row]
                     for row in rows]}


def _asked(was, before, mine, after, report=None):
    from collections import Counter
    return fuzz_docs._arrived(was, before, mine, after,
                              report or {"conflicts": [], "notes": []}, 0, Counter())


def test_a_table_the_source_shrinks_and_moves_is_built_at_the_shape_it_asked_for():
    """The source deletes a row from a table *and* moves it, with no reader anywhere.

    A move is a delete and a table built again blank, so the shape it is built at is
    the whole of the question, and `_merge_table` lays a trap for it: when the merge
    also regrids, it returns before merging the cells, so the block's own `rows` are
    still the document's. `_size` of those is the grid the sync was about to change,
    and the table came back 2x2 with an empty row on the end. `_built_size` counts the
    lines the matching settled instead (offline seed 88075, shrunk to three source ops
    and no reader at all).
    """
    world, ours, base = _push("two_tables")
    table = [b for b in ours["blocks"] if b.get("key") == "table:a"][0]
    table["rows"].pop(0)                       # the source drops the header row
    ours["blocks"].remove(table)               # ... and moves the table to the end
    ours["blocks"].append(table)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert "`table:a`: moved where the source has it" in report["applied"]
    assert [b.get("key") for b in ours["blocks"]][-1] == "table:a"
    grid = [b for b in ours["blocks"] if b.get("key") == "table:a"][0]
    assert [[doc_ir.runs_text(cell[0].get("runs", [])) for cell in row]
            for row in grid["rows"]] == [["1", "2"]]
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_the_campaign_asks_whether_the_sources_regrid_arrived():
    """The judge the campaign did not have. The loss oracle asks about the *reader's*
    work and says so in its first paragraph, and convergence cannot see a wrong grid
    either, because `rebase_tables` writes the matching it used into the base and the
    second sync makes the same reading. So a table the reader did not touch at all
    must simply come out at the shape the file asks for."""
    was = {"blocks": [_grid_block("table:x", [["a", "b"], ["1", "2"]])]}
    before = copy.deepcopy(was)                      # the reader touched nothing
    mine = {"blocks": [_grid_block("table:x", [["a", "b"]])]}   # the source drops a row
    assert [f["kind"] for f in _asked(was, before, mine, was)] == ["grid_lost"]
    assert not _asked(was, before, mine, mine), "the regrid arrived: nothing to say"
    assert not _asked(was, before, mine, was,
                      {"conflicts": [{"key": "table:x"}], "notes": []}), \
        "a report that names the table is the escape hatch, as everywhere else"
    assert not _asked(was, was, was, was), "the source asked for nothing"


def test_the_campaign_asks_whether_the_sources_cell_edit_arrived():
    """The half that can see a column matched wrongly, which the half above cannot:
    with the reader's hands off the grid, pairing columns by place and pairing them by
    their words agree. Here the reader takes the first column out and the source
    rewrites a cell in the one that is left."""
    was = {"blocks": [_grid_block("table:x", [["a", "b"], ["1", "2"]])]}
    before = {"blocks": [_grid_block("table:x", [["b"], ["2"]])]}   # a column deleted
    mine = {"blocks": [_grid_block("table:x", [["a", "zephyr"], ["1", "2"]])]}
    assert [f["kind"] for f in _asked(was, before, mine, before)] == ["cell_lost"]
    arrived = {"blocks": [_grid_block("table:x", [["zephyr"], ["2"]])]}
    assert not _asked(was, before, mine, arrived)
    # And the cell the reader took away with its column is no arrival to wait for.
    gone = {"blocks": [_grid_block("table:x", [["kestrel", "b"], ["1", "2"]])]}
    assert not _asked(was, before, gone, before), \
        "the reader deleted the column that cell was in"
    # Nor is anything owed once the reader has written in the table themselves.
    wrote = {"blocks": [_grid_block("table:x", [["b"], ["typed"]])]}
    assert not _asked(was, wrote, mine, wrote), "the reader wrote in it: the merge decides"


def test_the_campaign_asks_whether_the_sources_wording_arrived():
    """The same question for a paragraph, and the plainest thing a sync does: the
    reader left the block word for word as the base has it, so there is nothing to
    merge and the file's words must simply be there at the end.

    Nobody asked it. The oracle asks whether the *reader's* work survived and a
    source edit that never lands takes nothing of theirs away; convergence is
    satisfied by any reading the base then agrees with. An edit could be dropped for
    ever as long as it was dropped consistently."""
    was = {"blocks": [_p("k1", "Results here.")]}
    before = copy.deepcopy(was)                      # the reader touched nothing
    mine = {"blocks": [_p("k1", "Results, revised.")]}
    assert [f["kind"] for f in _asked(was, before, mine, was)] == ["wording_lost"]
    assert not _asked(was, before, mine, mine), "the wording arrived"
    assert not _asked(was, before, mine, was,
                      {"conflicts": [], "notes": ["k1: left alone"]}), \
        "a report that names the block is the escape hatch, as everywhere else"
    typed = {"blocks": [_p("k1", "Results here, and more.")]}
    assert not _asked(was, typed, mine, typed), "the reader wrote in it: the merge decides"
    # A chip is read by what it *is*: its face is the document's to draw, and Docs
    # renders a person chip off the address, so comparing the text called every
    # source `add_chip` an edit that never arrived.
    chip = {"chip": "person", "frozen": True, "text": "Grace", "value": "g@example.com"}
    asks = {"blocks": [_p("k1", "Results here.") | {"runs": [{"text": "Results here."},
                                                             chip]}]}
    drawn = copy.deepcopy(asks)
    drawn["blocks"][0]["runs"][1] = dict(chip, text="grace")
    assert not _asked(was, before, asks, drawn), "the chip arrived, under its own face"


def test_a_heading_behind_a_paragraph_the_source_drops_still_follows_the_theme():
    """Docs merges a deleted paragraph into the one behind it and hands over its
    style, and that block need not be one this run wrote: the source drops a
    justified paragraph, the heading after it comes out justified *of its own*, and
    a heading that sets its own alignment has stopped following the theme's centring
    for ever — the file is regenerated from the document afterwards, so the next sync
    agrees and nothing ever says it happened (offline chain-6 seed 1130023, shape
    `themed`; 1 of 400 rounds with `paragraph_merged` taken out).

    `carry_unimported` already put the named style and the bullet back for every
    block; only the measurements were held to a paragraph the write itself touched,
    because taking styling away is the one dangerous thing a settle does. The
    neighbour of a block this batch deleted is the one place where it is the write's
    own doing.
    """
    world = doc_world.build([{"blocks": [
        fuzz_docs._p("Justified prose.", align="justify"),
        fuzz_docs._h("A heading"), fuzz_docs._p("And prose after that.")]}],
        title="fuzz")
    world.theme = {name: dict(style) for name, style in fuzz_docs.THEME.items()}
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    ours["blocks"] = [b for b in ours["blocks"] if b["key"] != "paragraph:justified-prose"]
    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    live = doc_world.read_ir(world, ours, base)
    assert _keys(live) == ["heading:a-heading", "paragraph:and-prose-after-that"]
    assert live["blocks"][0].get("align") is None, \
        "the heading wears the theme's centring; nobody asked it to justify itself"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_a_mark_the_theme_already_puts_on_is_no_restyle_when_the_named_style_changes():
    """The campaign's own judge, and its own false alarm. `_worn` subtracts a run's
    marks by the block's **named style**, because the file spells them out and a read
    leaves out what the style already says — so where the source moves a block from
    one named style to another, the two sides are not spelling the same language.

    The theme bolds HEADING_1, so the reader bolding one word of a themed heading
    says nothing there and the guard read the block as one they had left alone; the
    source then made it body text, where the same run styling spells out differently
    on either side, and the reader's bold — which the merge kept, as it should — read
    as the source's restyle vanishing (chain-8 seed 1140022, chain-6 seed 1150196).
    The named style itself is part of `_shape`, so nothing goes unjudged.
    """
    from collections import Counter
    theme = fuzz_docs.theme_values(fuzz_docs.corpus("themed"))

    def block(runs, kind="heading"):
        return {"kind": kind, "level": 1, "key": "k1", "runs": runs}

    plain, unbold = {"text": "A themed "}, {"text": "heading", "bold": False}
    base = block([plain, unbold])
    doc = block([{"text": "A "}, {"text": "themed", "bold": True},
                 {"text": " "}, unbold])                   # the reader bolds a word
    mine = block([plain, unbold], "paragraph")             # the source demotes it
    after = block([{"text": "A "}, {"text": "themed", "bold": True},
                   {"text": " heading"}], "paragraph")
    asked = dict(said="", tab=None, step=0, theme=theme)
    assert not fuzz_docs._styling_arrived("k1", base, doc, mine, after,
                                          seen=Counter(), **asked)
    # And it still speaks where all four sides are one named style: the source asks
    # for an italic that never arrives.
    italic = block([plain, {"text": "heading", "italic": True}])
    assert [f["kind"] for f in fuzz_docs._styling_arrived(
        "k1", base, base, italic, base, seen=Counter(), **asked)] == ["restyle_lost"]


def test_a_paragraph_the_reader_pasted_in_front_does_not_take_the_originals_key():
    """The other way round from `..._pasted_twice_over`, and the way that broke: the
    copy stands *before* the original and the same sync rewrites the original.

    A rewrite is a delete and a write, so the block gives up its named range, and
    `adopt_keys` was left matching the plan to the read-back through a dictionary of
    words. Two blocks said the same thing, the walk reached the reader's copy first,
    and the key the file had carried since the push went to it — the source's chip
    landed correctly and then settled under a name nobody asked for, with file, base
    and document all agreeing on it (offline chain-6 seed 94577). `_adopt_in_order`
    pairs the two sequences by their order first, which is the information the
    dictionary threw away."""
    world, ours, base = _build([_p("a", "Results here."), _p("b", "What we found.")])
    first = ours["blocks"][0]["key"]
    text = doc_merge.block_text(ours["blocks"][0])
    start = ours["blocks"][0]["span"][0]
    world.apply([{"insertText": {"location": {"index": start}, "text": text + "\n"}}])
    ours["blocks"][0]["runs"] = [{"text": text},
                                 {"chip": "person", "frozen": True, "text": "Grace",
                                  "value": "grace@example.com"}]
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [doc_merge.block_text(b) for b in ours["blocks"]] == \
        [text, text + "￼", "What we found."]
    assert _keys(ours)[1] == first, "the chip's block is the one the file named"
    assert _keys(ours)[0] != first
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_the_oracle_lets_a_pared_down_token_whose_joiner_went_too_alone():
    """`_pared_down` recognised the leftover only when the reader had stopped at the
    word: deleting `soft` out of `soft\xadhyphen` leaves `\xadhyphen`, and sweeping the
    soft hyphen up with it leaves plain `hyphen`. The same deletion, and only the
    first was known, so the second read as a word the reader had typed and the
    source's rewriting of the other half looked like a loss (chain-6 seed 94030).

    The guard is that the narrow leftover must not be standing there as well: a base
    token pared down once leaves one token, so `hyphen` beside `\xadhyphen` is a word
    the reader typed and still has to survive."""
    base = _ir(_p("k1", "a soft\xadhyphen here"))
    before = _ir(_p("k1", "a hyphen here"))         # the reader deleted "a soft\xad"
    after = _ir(_p("k1", "a kestrel here"))         # the source reworded the other half
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    both = _ir(_p("k1", "a \xadhyphen here hyphen"))
    assert _kinds(oracle.check(base, both, after, NOTHING)) == {"words_lost"}, \
        "the paring is the one with the joiner still on it; the bare word is theirs"


def test_a_mark_taken_off_a_block_the_source_dropped_is_not_handed_back():
    """The block the reader un-bolded a word of is one the *source* dropped, and they
    left it word for word as the base has it — so it goes, and the mark goes with it.
    That is the bargain `block_gone` states in the same words, and it is the only
    excuse the taken-off half has (a mark the reader *put on* keeps the whole block
    alive, `doc_merge._styled`, so the question cannot arise there).

    What made it visible is that the key does not go with the block: the reader's own
    pasted copy stands there saying the same words in the theme's own bold, takes the
    name, and the un-bolding read as handed back (offline chain-10 seed 1180145,
    shape `themed`; 1 of 200 rounds without the excuse)."""
    base = _ir(_run_head("k1", "A themed heading"), _p("k2", "Prose."))
    before = _ir({"key": "k1", "kind": "heading", "level": 1,
                  "runs": [{"text": "A themed "}, {"text": "heading", "bold": False}]},
                 _p("k2", "Prose."),
                 _run_head(None, "A themed heading"))       # the reader's own copy
    after = _ir(_run_head("k1", "A themed heading"), _p("k2", "Prose."))
    dropped = _ir(_p("k2", "Prose."))                       # the file drops the heading
    assert not oracle.failures(oracle.check(base, before, after, NOTHING, dropped,
                                            theme=MARKED))
    # And the excuse is the file saying so. A file that still asks for the heading is
    # a block nobody dropped, and the un-bolding is owed.
    kept = _ir(_run_head("k1", "A themed heading"), _p("k2", "Prose."))
    assert _kinds(oracle.check(base, before, after, NOTHING, kept,
                               theme=MARKED)) == {"styling_restored"}


def test_an_un_marked_word_the_source_reworded_away_is_not_owed_by_its_twin():
    """Asked by occurrence, and a block may say one word twice. The heading said
    `thicket` twice, the reader un-bolded the first, the source reworded that one to
    `vellum` — which came out un-bold, exactly as asked — and the plain `thicket` left
    over answered for it (offline chain-10 seed 1180151, shape `themed`; 1 of 200
    rounds without the cap).

    Counted against the *file*, because that says whose doing it was: an occurrence
    the source has just **added** is the mirror case and must still be no excuse
    (themed seed 40254), so the two cannot be told apart by the document alone."""
    def heading(runs):
        return {"key": "k1", "kind": "heading", "level": 1, "runs": runs}

    base = _ir(_run_head("k1", "A thicket and a thicket"))
    before = _ir(heading([{"text": "A "}, {"text": "thicket", "bold": False},
                          {"text": " and a thicket"}]))
    mine = _ir(_run_head("k1", "A vellum and a thicket"))
    after = _ir(heading([{"text": "A "}, {"text": "vellum", "bold": False},
                         {"text": " and a thicket"}]))
    assert not oracle.failures(oracle.check(base, before, after, NOTHING, mine,
                                            theme=MARKED))
    # The mirror: the source adds a second `and` and the reader's own goes back bold.
    was = _ir(_run_head("k1", "A heading and willow"))
    read = _ir(heading([{"text": "A heading "}, {"text": "and", "bold": False},
                        {"text": " willow"}]))
    asks = _ir(_run_head("k1", "A heading and willow and harbour"))
    back = _ir(_run_head("k1", "A heading and willow and harbour"))
    assert _kinds(oracle.check(was, read, back, NOTHING, asks,
                               theme=MARKED)) == {"styling_restored"}


def test_the_empty_block_a_new_table_swallows_is_recovered_whatever_it_wears():
    """`insertTable` in front of a block leaves an empty paragraph, and
    `_new_table_requests` gets rid of it by deleting the mark of the block in front —
    which, when that block is itself empty, is all the block there was, so its named
    range goes with it. `recover_swallowed` gives the key back before the re-plan,
    which otherwise reads the block as one the reader deleted.

    It asked for a *paragraph*, and Docs' merge keeps the first one's style: an empty
    subtitle hands the survivor its own named style, so the block comes back a
    subtitle and the recovery passed it by. The one thing the source was asking of
    it — to stop being a subtitle — then went nowhere, and the settle keyed it from
    its words to the very name it had, so file, base and document all agreed and the
    next sync wrote nothing (offline chain-8 seed 1270233, shape `between_tables`).
    """
    world = doc_world.build([{"blocks": [
        fuzz_docs._t([["a", "b"], ["1", "2"]]), {"kind": "subtitle", "runs": []},
        fuzz_docs._p("After it."), fuzz_docs._t([["c", "d"], ["3", "4"]])]}],
        title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    assert _keys(ours)[1] == "subtitle:empty"
    ours["blocks"][1]["kind"] = "paragraph"          # the source demotes it
    ours["blocks"].insert(2, fuzz_docs._t([["h1", "h2"], ["quartz", "x"]]))
    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    live = doc_world.read_ir(world, ours, base)
    assert doc_merge.named_style(live["blocks"][1]) == "NORMAL_TEXT", \
        "the empty block the table swallowed still wears the style the source dropped"
    assert [doc_merge._table_words(b) for b in live["blocks"] if b["kind"] == "table"] \
        == ["a b 1 2", "h1 h2 quartz x", "c d 3 4"]
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_the_orphan_a_backspace_leaves_is_not_handed_to_a_new_tables_paragraph():
    """A reader backspacing at the start of an empty paragraph leaves that
    paragraph's named range inside the one that survives, naming nothing — the
    orphan `doc_ir.orphan_requests` is about — and an empty paragraph is all mark, so
    the orphan sits exactly on the survivor's paragraph mark.

    `insertTable` in front of another table goes at that very mark, splits the
    paragraph, and the range goes with the half after the table: the empty paragraph
    the insert leaves behind is read back under the dead name. The re-plan reads the
    document again between the structural batch and the words, so it sees the block
    the reader deleted standing there for the source to write into — the deletion
    undone and the source's words in it (offline chain-10 seed 1430231, shape
    `astral`; 1 of 250 rounds). The orphans head the structural batch too, for the
    same reason they head the batch of words one step later."""
    world = doc_world.build([{"blocks": [
        fuzz_docs._p("One."), {"kind": "paragraph", "runs": []},
        fuzz_docs._t([["c", "d"], ["3", "4"]])]}], title="fuzz")
    ours = fuzz_docs.bootstrap(world)
    base = copy.deepcopy(ours)
    assert _keys(ours) == ["paragraph:one", "paragraph:empty", "table:c"]

    mark = doc_world.read_ir(world)["blocks"][0]["span"][1] - 1
    world.apply([{"deleteContentRange": {"range": {"startIndex": mark,
                                                   "endIndex": mark + 1}}}])
    joined = doc_world.read_ir(world, ours, base)
    assert _keys(joined) == ["paragraph:one", "table:c"] and joined["orphans"]

    # The source never saw that: it rewords the block and puts a table in front of it.
    ours["blocks"][1]["runs"] = [{"text": " and harbour"}]
    ours["blocks"].insert(2, fuzz_docs._t([["h1", "h2"], ["quartz", "x"]]))
    _, ours, base = fuzz_docs.sync_once(world, ours, base)

    live = doc_world.read_ir(world, ours, base)
    assert [doc_merge.block_text(b) for b in live["blocks"]] == ["One.", "", "", ""], \
        "the block the reader deleted is back, with the source's words written into it"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_a_block_the_merge_will_not_write_is_asked_about_by_the_key_it_is_refused_by():
    """The existence judge's own first finding, and the judge's own fault. `_arrived`
    is handed the file **as it stood before the sync**, where a block the source has
    just added has no key: `doc_merge.plan` gives it one (`doc_ir.key_blocks`, in
    place on the file it is handed) and the report names the refusal by that key. So
    the one excuse there is — the report says so — could never be found, and every
    block the merge refuses to write read as an addition lost in silence (offline
    chain-4 seed 660085).

    The other half is the note itself. The block is not merely unwritten: the settle
    regenerates the file from the document, so the paragraph goes out of the file too
    and the next sync will not try again. "Not written" reads as a thing still
    waiting, so `refuse_nowhere` says where it went and how to get it in."""
    world, ours, base = _push("two_tables")
    at = [i for i, b in enumerate(ours["blocks"]) if b.get("key") == "table:c"][0]
    ours["blocks"].insert(at, _para("The source added willow."))
    was, mine = copy.deepcopy(base), copy.deepcopy(ours)
    before = doc_world.settled_ir(world, ours, base)
    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    note = next(n for n in report["notes"] if "no paragraph to write in" in n)
    assert "takes it back out of the file" in note, note
    assert not any("willow" in oracle.text_of(b) for b in ours["blocks"]), \
        "the settle regenerates the file from the document, which has no such block"
    assert fuzz_docs._arrived(was, before, copy.deepcopy(mine), ours, report, 0,
                              Counter()) == []
    # Asked the way it used to be — the file unkeyed — the refusal cannot be looked up.
    assert [f["kind"] for f in fuzz_docs._existence_arrived(
        was, before, copy.deepcopy(mine), ours, oracle.accounted(report), None, 0,
        Counter())] == ["addition_lost"]


class _PickKeys:
    """An rng that picks the blocks it is told to, by key."""

    def __init__(self, *keys):
        self.keys = list(keys)

    def choice(self, seq):
        key = self.keys.pop(0)
        return next(b for b in seq if b.get("key") == key)


def test_a_block_dragged_below_the_cut_lands_where_the_reader_let_go_of_it():
    """The harness's own, and the oldest of them. `read_move_block` is a drag: a
    delete and a retype, in two batches — and the second's index was read off the
    document the reader saw, not off the one the first batch leaves behind. Drop a
    block *below* the cut and everything down there has moved up by what went, so the
    text landed that far past where the reader let go: inside a word, inside a chip,
    or between the two code units of an astral character, which no cursor can be put
    inside.

    That last one killed the campaign outright — the world keeps the lone surrogate a
    cut pair leaves, as it should, and `doc_ir.utf16_len` encodes strictly, as the
    API's own JSON does (seed 710370, shape `astral`). The rest of it never crashed
    and is the worse half: every judge was being handed a document no reader could
    have made, and both of the loss oracle's punctuation forgivenesses — `_dressed_up`
    and `_undressed` — were written for damage this line was doing.
    `doc_world.splits_a_pair` is the net under it."""
    world, ours, base = _build([_para("First one."), _para("Second one."),
                                _para("Third one \U0001d538."), _para("Fourth one.")])
    part = doc_ir.from_document(world.read(), None)
    doc_ir.apply_keys(part, doc_ir.named_ranges_of(world.read(), None))
    batches, _ = fuzz_docs.read_move_block(
        _PickKeys("paragraph:second-one", "paragraph:third-one"), part, None)
    for batch in batches:
        world.apply(batch)
    assert [oracle.text_of(b) for b in doc_world.read_ir(world, ours, base)["blocks"]] \
        == ["First one.", "Third one \U0001d538.", "Second one.", "Fourth one."]


def test_a_tab_the_file_only_asks_for_does_not_stand_in_for_the_body():
    """`parts_by_tab` keyed a part by `part.get("tab")`, and a `<section>` with no
    `data-tab` is a tab the *file* asks for that the document has never had
    (`fuzz_docs._fresh_asks`), so it has no id: it landed on `None`, where the body
    is, and the last one written won. Every question asked of the body then got
    another tab's part — the file side of it in `oracle.check`, and all four sides in
    `fuzz_docs._arrived`.

    Measured with the collision put back: 107 of 200 rounds at chain 4 fail, and 0 of
    the same 200 with `_existence_arrived` switched off. So it was invisible until
    the existence judge existed, which is the argument for that judge as much as for
    this fix."""
    ir = {"blocks": [_para("The body.")],
          "tabs": [{"tab": "t.1", "blocks": [_para("A named tab.")]},
                   {"title": "New", "blocks": [_para("A tab the file asks for.")]}]}
    by_tab = oracle.parts_by_tab(ir)
    assert set(by_tab) == {None, "t.1", "?2"}
    assert oracle.part_text(by_tab[None]) == "The body."
    assert oracle.part_text(by_tab["?2"]) == "A tab the file asks for."


def test_anything_at_all_happening_to_a_twin_does_not_lose_the_copy_that_arrived():
    """The existence judge counts blocks saying what this one says, which handles twins
    for free — but the count is a question about *this* block asked of every other one
    that says the same thing, so anything at all happening to a twin answers it wrongly.
    The source appended the same paragraph twice over two steps and then, in the second,
    gave the first copy a person chip (offline chain-8 seed 720074) or reworded it with
    a `collide` (chain-12 seed 730061): the twin stopped saying what the new one says,
    the count stood still, and an addition that had arrived read as lost.

    Two fixes, one per case. A chip is content but not words, and existence is about
    existence, so the count is of the text alone — which the first assertion here is.
    A rewording changes the text, and no counting survives that: the second trace is
    the key the settle gives back to a block the plan wrote a named range for. Either
    one excuses; the finding needs no trace at all."""
    chip = {"chip": "person", "frozen": True, "text": "Grace", "value": "grace@example.com"}
    base = {"blocks": [_para("One paragraph of it."), _para("the source added lantern")]}
    for i, block in enumerate(base["blocks"]):
        block["key"] = f"paragraph:b{i}"
    added = dict(_para("the source added lantern"), key="paragraph:lantern#2")
    chipped = {"kind": "paragraph", "key": "paragraph:b1",
               "runs": [{"text": "the source added lantern"}, chip]}
    file_p = {"blocks": [base["blocks"][0], chipped, added]}
    end_p = copy.deepcopy(file_p)
    seen = Counter()
    assert fuzz_docs._saying(end_p, "the source added lantern") \
        > fuzz_docs._saying(base, "the source added lantern")
    assert fuzz_docs._existence_arrived(base, base, file_p, end_p, "", None, 0,
                                        seen) == []
    assert seen["arrival/added asked"] == 1

    # The twin reworded instead: nothing says what the copy says any more, and only
    # the key it was written under says it is there.
    reworded = dict(_para("the umbrella added harbour"), key="paragraph:b1")
    file_p = {"blocks": [base["blocks"][0], reworded, added]}
    end_p = copy.deepcopy(file_p)
    assert fuzz_docs._saying(end_p, "the source added lantern") \
        == fuzz_docs._saying(base, "the source added lantern")
    assert fuzz_docs._existence_arrived(base, base, file_p, end_p, "", None, 0,
                                        Counter()) == []
    end_p["blocks"] = end_p["blocks"][:2]          # the copy really is gone
    assert [f["kind"] for f in fuzz_docs._existence_arrived(
        base, base, file_p, end_p, "", None, 0, Counter())] == ["addition_lost"]


def test_a_table_that_never_said_anything_is_recovered_onto_nothing():
    """`recover_tables` pairs a table whose range the reader destroyed with the base
    entry that names it, on the words the two hold — and two empty strings are each
    other's perfect match. A base table that never said anything paired at 1.0 with
    the blank table `insertTable` had just built in the same batch, before
    `anchor_tables` (the pass that knows about that one) had run.

    What it cost is the whole of the reader's table: the beheaded one, holding the
    only word either of them had, was then the one free table left for the new table's
    key, so the merge saw its own blank grid where the reader's table stood, deleted
    it to build the grid again, and the word went with no note (chain-12 seed
    730384)."""
    base = {"blocks": [dict(_table([[""], [""]]), key="table:empty")]}
    built, beheaded = _table([["", ""], ["", ""]]), _table([["vellum "]])
    theirs = {"blocks": [built, beheaded]}
    assert doc_merge.recover_tables(base, theirs) == 0
    assert not built.get("key") and not beheaded.get("key")
    # A table with words is still recovered by them.
    base = {"blocks": [dict(_table([["alpha", "beta"]]), key="table:alpha")]}
    theirs = {"blocks": [_table([["", ""], ["", ""]]), _table([["alpha", "beta"]])]}
    assert doc_merge.recover_tables(base, theirs) == 1
    assert theirs["blocks"][1]["key"] == "table:alpha"


def test_a_table_the_batch_regridded_is_not_the_one_the_reader_beheaded():
    """`anchor_tables` names the table a structural batch wrote by what it follows,
    and took the first unkeyed table after that block. Usually there is only one —
    the batch's own, whose named range its row delete took with it. But a reader can
    behead a table too, in the browser, by deleting the row it is anchored in, and
    `recover_tables` refuses to pair that one when the words leave any doubt
    (`TABLE_MATCH`): two free tables then stand on the page and the anchor decides by
    place alone. It picked the reader's, so the regridded table's key went onto it,
    the source's rows were planned against the reader's grid, and the two the sync
    had just written stood there under no name at all (chain-6 seed 790329).

    Where the batch wrote a grid, that grid is what its table has: a free table of
    another shape is not it, whatever it follows. And where none of them has that
    shape, nothing is claimed — the key comes back at the re-plan, where
    `recover_tables` sees both tables at once and the words tell them apart."""
    lines = {"row": [doc_merge._Line(0, 0, None, True), doc_merge._Line(1, 1, 0),
                     doc_merge._Line(2, 2, 1)],
             "column": [doc_merge._Line(0, 0, 0)]}
    told = {"key": "table:c", "after": "paragraph:before-both",
            "ops": [{"deleteTableRow": {}}], "lines": lines}
    beheaded, regridded = _table([["b"]]), _table([[""], ["4"]])
    live = {"blocks": [_p("paragraph:before-both", "Before both."),
                       beheaded, regridded]}
    assert doc_merge.anchor_tables(live, [told]) == 1
    assert regridded["key"] == "table:c" and not beheaded.get("key")

    # Nothing of that shape on the page: the anchor claims nothing at all.
    other = _table([["b"]])
    live = {"blocks": [_p("paragraph:before-both", "Before both."), other]}
    assert doc_merge.anchor_tables(live, [told]) == 0
    assert not other.get("key")


def test_two_empty_paragraphs_trading_names_is_not_an_order_undone():
    """A key made from a block's words is no identity where the block has none: it is
    that block's number among the wordless ones, and the next one to lose its words
    takes it. Docs keeps a paragraph between two tables however it is deleted, so a
    source that drops one there leaves its mark standing empty — which is then the
    body's *first* wordless block, and the one that carried `paragraph:empty` before
    becomes `paragraph:empty#2`. Nothing moved and nothing was lost; two names changed
    hands under a genuine move somewhere else, and the order judge read the renaming
    as the source's order undone (chain-10 seed 780188, shape `between_tables`).

    Only where the four sides disagree about which keys are wordless, which is the
    narrowest the exclusion can be: a wordless block — a picture of its own, an empty
    line between two sections — is an ordinary thing for a source move to carry, and
    a judge gives up as little sight as it can."""
    tables = [dict(_table([["h1"]]), key="table:h1"),
              dict(_table([["h2"]]), key="table:h1#2")]
    tail = [_p("paragraph:moved", "moved"), _p("paragraph:tail", "tail")]
    was = {"blocks": [tables[0], _p("paragraph:the", "The "), tables[1],
                      _p("paragraph:empty", ""), *tail]}
    # The source dropped "The " and moved one paragraph past the other.
    src = {"blocks": [tables[0], tables[1], _p("paragraph:empty", ""),
                      tail[1], tail[0]]}
    # Docs kept the mark between the tables, so the numbering shifted along.
    end = {"blocks": [tables[0], _p("paragraph:empty", ""), tables[1],
                      _p("paragraph:empty#2", ""), tail[1], tail[0]]}
    seen = Counter()
    assert fuzz_docs._order_arrived(was, was, src, end, "", None, 0, seen) == []
    assert seen["arrival/order asked"] == 1, "the real move is still a question"

    # The same shape with the wordless population unchanged: the judge still sees it.
    src = {"blocks": [tables[0], tail[0], _p("paragraph:the", "The "), tables[1],
                      _p("paragraph:empty", ""), tail[1]]}
    assert [f["kind"] for f in fuzz_docs._order_arrived(
        was, was, src, was, "", None, 0, Counter())] == ["order_lost"]


def test_the_stop_the_source_parks_against_a_word_does_not_take_its_bold_away():
    """A word is what a reader sees, and `WORD` is `\\S+`: the full stop the source
    writes after a bolded word comes along in the token, so "a word wears what every
    character of it wears" read the bold as gone while it sat there. The reader
    bolded `zephyr`, the source reworded the sentence around it, and the merge wrote
    `lantern zephyr` bold and `.` plain — nothing lost, and the oracle cried wolf
    (chain-8 seed 870308, shape `two_tables`).

    Punctuation only, and only where it clings to an end: a soft hyphen joins two
    words rather than dressing one, and that is `joined_differently`'s business."""
    base = _ir(_p("k1", "zephyr both."))
    before = _ir({"key": "k1", "kind": "paragraph",
                  "runs": [{"text": "zephyr", "bold": True}, {"text": " both."}]})
    after = _ir({"key": "k1", "kind": "paragraph",
                 "runs": [{"text": "lantern zephyr", "bold": True}, {"text": "."}]})
    assert not oracle.failures(oracle.check(base, before, after, NOTHING))
    # And the bold really going is a loss still, stop or no stop.
    gone = _ir({"key": "k1", "kind": "paragraph", "runs": [{"text": "lantern zephyr."}]})
    assert _kinds(oracle.check(base, before, gone, NOTHING)) == {"styling_lost"}


def test_a_key_this_batch_is_about_to_place_is_not_the_recoverys_to_give():
    """Two repairs look at the same free tables after a structural write:
    `recover_tables`, for a range the *reader* destroyed by deleting the row a table
    is anchored in, and `anchor_tables`, for one of our own writes. The recovery goes
    first, so the reader's table is not free to be taken — and where the same table
    was regridded by us and beheaded by them, the two can cross.

    A regrid that deletes a table's first column leaves it saying almost nothing,
    while the reader's beheaded table still says most of what the base recorded: the
    regridded table's key scored higher on the reader's table than on its own
    remnant and took it, `anchor_tables` then found that key already placed and left
    the remnant unnamed, and the source's own regrid went nowhere (chain-8 seed
    870368, shape `two_tables`). The batch knows where it put its tables; this pass
    only guesses, so it goes second on those."""
    base = {"blocks": [dict(_table([["a", "b"], ["1", "2"]]), key="table:a"),
                       dict(_table([["h1", "h2"]]), key="table:h1")]}
    # What the read-back holds: the reader's beheaded table, and ours with its first
    # column gone — which is the one this batch is about to name.
    scene = {"blocks": [_table([["1", "2"]]), _table([["h2"]])]}
    theirs = copy.deepcopy(scene)
    assert doc_merge.recover_tables(base, theirs, {"table:h1"}) == 1
    assert [b.get("key") for b in theirs["blocks"]] == ["table:a", None]
    # Without it, the batch's own key is the one the guess gives away.
    theirs = copy.deepcopy(scene)
    assert doc_merge.recover_tables(base, theirs) == 1
    assert [b.get("key") for b in theirs["blocks"]] == ["table:h1", None]


def test_a_table_moved_in_front_of_the_empty_paragraph_it_ends_behind_is_refused():
    """A body may not end on a table, so Docs keeps an empty paragraph after one and
    no request deletes it: a trailing table goes out by its own span *and the mark in
    front of it*. That block keeps its words — unless it is itself an empty
    paragraph, which is all mark. Then the file putting the table in *front* of it
    asks for a place that the table's own delete takes away: `insertTable` splits
    what is left of the swallowed paragraph, the table lands behind it again, and the
    document comes back in the base's order with the move undone in silence (chain-8
    seed 890070, shape `ends_on_table`)."""
    subtitle = {"key": "subtitle:empty", "kind": "subtitle", "runs": [], "span": [32, 33]}
    table = dict(_table([["year", "count"]]), key="table:year", span=[33, 68])
    heading = dict(_p("heading:results", "Results"), span=[1, 9])
    theirs = {"blocks": [heading, subtitle, table], "trailer": [68, 69]}
    merged = [heading, dict(table, moved=True), subtitle]
    notes: list = []
    doc_merge.refuse_eaten_anchor(theirs, merged, notes)
    assert [b["key"] for b in merged] == ["heading:results", "subtitle:empty",
                                          "table:year"]
    assert not merged[-1].get("moved")
    assert len(notes) == 1 and "table:year" in notes[0]

    # A paragraph with words of its own keeps them, and so its name: the move stands.
    words = dict(_p("paragraph:tail", "Tail."), span=[32, 38])
    theirs = {"blocks": [heading, words, dict(table, span=[38, 73])],
              "trailer": [73, 74]}
    merged = [heading, dict(table, span=[38, 73], moved=True), words]
    notes = []
    doc_merge.refuse_eaten_anchor(theirs, merged, notes)
    assert [b["key"] for b in merged] == ["heading:results", "table:year",
                                          "paragraph:tail"]
    assert merged[1]["moved"] and not notes


def test_the_empty_paragraph_a_moved_tables_delete_eats_gets_its_name_back():
    """The other half of the one above: where the table moves somewhere *else*, the
    move can go ahead — and the mark the delete reaches back over is still that empty
    paragraph's whole self, so its named range goes with it. The re-plan that follows
    the structural batch then reads the block as one the reader deleted, and what the
    source asked of it is dropped in silence: a restyle (offline chain-16 seed
    1730265, shape `tabs`) and, with the block coming back under a fresh name between
    the two tables, the order the file asks for (chain-14 seed 1740158).

    The trailer takes its place — at the end of a body there is nowhere else for an
    empty paragraph to be — and `doc_ir._hide_trailer` leaves one that has an
    identity planted on it out of the scaffolding, so the name makes it a block
    again."""
    world, ours, base = _build([_para("One."), _table([["a", "b"]]),
                                {"kind": "paragraph", "runs": []},
                                _table([["c", "d"]])])
    assert _keys(ours) == ["paragraph:one", "table:a", "paragraph:empty", "table:c"]

    # The source moves the table the body ends on up in front of the other one, and
    # makes the empty paragraph it ends behind a heading.
    at = _keys(ours).index("table:c")
    ours["blocks"].insert(1, ours["blocks"].pop(at))
    empty = next(b for b in ours["blocks"] if b["key"] == "paragraph:empty")
    empty["kind"], empty["level"] = "heading", 1

    _, ours, base = fuzz_docs.sync_once(world, ours, base)
    live = doc_world.read_ir(world, ours, base)
    # `paragraph:empty#2` is the one `insertTable` leaves between the two tables,
    # which no request can delete and Docs wants there anyway.
    assert _keys(live) == ["paragraph:one", "table:c", "paragraph:empty#2",
                           "table:a", "paragraph:empty"]
    kept = live["blocks"][-1]
    assert doc_merge.named_style(kept) == "HEADING_1", \
        "the block is there, so the source's restyle of it has somewhere to go"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0


def test_a_cell_both_sides_wrote_in_is_reported_under_the_table_it_is_in():
    """A conflict is the one place the merge hands a person the words it did not
    write, and it is only any use if they can find where those words were. A cell has
    no key of its own — its identity is its place in a table — so `_merge_block` has
    always had a fallback name for one, and the fallback was the literal
    `a table cell`: true, and an address for nothing. A document with three tables
    says it three times and points at none of them.

    `_merge_cell`'s *other* branch, where the three sides disagree on how many
    paragraphs a cell holds, has named the table since it was written. The branch that
    merges a cell paragraph by paragraph is the commoner one by far — one paragraph
    per cell is what a table looks like — and it went through `_merge_block`, which
    had nothing to name, so the crisper the case the vaguer the report. Found from the
    other end (offline chain-12 seed 1710213, shape `prose`): the loss oracle asks the
    report whether a cell it misses was spoken for, and a conflict with no table's
    name in it answers for nothing."""
    world, ours, base = _build([_para("One."), _table([["a", "b"]])])
    assert _keys(ours) == ["paragraph:one", "table:a"]

    # The reader writes in the first cell, and the source writes something else there.
    cell = doc_world.read_ir(world)["blocks"][1]["rows"][0][0][0]
    world.apply([{"insertText": {"location": {"index": cell["span"][0]},
                                 "text": "meadow "}}])
    ours["blocks"][1]["rows"][0][0][0]["runs"] = [{"text": "thicket a"}]

    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    assert [(c["ours"], c["theirs"], c["key"]) for c in report["conflicts"]] \
        == [("thicket ", "meadow ", "table:a")], \
        "the cell is named by the table it is in, which is the only address it has"


def test_how_the_reader_set_a_paragraph_survives_the_source_moving_a_chip_in_it():
    """A block whose chip or picture the source changed is written again from the
    file, because no request edits one — and it was written again *from the file's
    shape too*, on the grounds that a document which left the block's words, run
    styles and frozen runs exactly as the base has them has nothing of its own in it.
    It may have: how a paragraph is set is a choice made without touching a word.
    Centre a quotation, indent it, space it out, make a line a heading — and the first
    time the source so much as moves a chip in that block, all of it goes, in silence.

    Nothing could see it. The words are all there, so the loss oracle passed it; the
    source's own change arrived, so the campaign's judge passed it; and the settle
    regenerates the file from the document, so the file then says what was written and
    the next sync writes nothing. `doc_loss_oracle._shape_findings` is the question
    being asked at all, and it fails 7 of 200 rounds at chain 6 without the fix (seed
    2000188 shrinks to exactly this: the reader spaces a paragraph out, the source
    adds a chip to it). `_merged_shape` is the one rule both ways into a block now
    share: the document's where both sides changed it, with a note."""
    world, ours, base = _build([_para("One."), _para("The middle one."),
                                _para("Three.")])
    key = "paragraph:the-middle-one"

    # The reader centres it and spaces it out, without touching a word of it.
    span = next(b for b in doc_world.read_ir(world, ours, base)["blocks"]
                if b.get("key") == key)["span"]
    world.apply([{"updateParagraphStyle": {
        "range": {"startIndex": span[0], "endIndex": span[1]},
        "paragraphStyle": {"alignment": "CENTER",
                           "spaceAbove": {"magnitude": 12, "unit": "PT"}},
        "fields": "alignment,spaceAbove"}}])

    # The source adds a person chip to that same block, which is a rewrite.
    block = next(b for b in ours["blocks"] if b.get("key") == key)
    block["runs"] = block["runs"] + [{"chip": "person", "frozen": True,
                                      "text": "Grace", "value": "grace@example.com"}]

    report, ours, base = fuzz_docs.sync_once(world, ours, base)
    after = next(b for b in doc_world.read_ir(world, ours, base)["blocks"]
                 if b.get("key") == key)
    assert (after.get("align"), after.get("space_above")) == ("center", 12.0), \
        "the reader set the paragraph and said nothing about the chip"
    assert any(r.get("frozen") for r in after["runs"]), \
        "and the source's chip went in all the same"
    assert not report["notes"], "neither side contradicted the other, so nothing to say"
    again, _, _ = fuzz_docs.sync_once(world, copy.deepcopy(ours), copy.deepcopy(base))
    assert again["requests"] == 0
