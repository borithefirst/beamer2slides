"""A compile in pieces, the parts that need no TeX (`beamer2slides.pieces`): where a deck is cut,
how the pieces' .aux files become the whole's, and the guess a pass that has not settled leaves
for the next. Compiling in pieces against a whole compile: `test_inverse.py`."""

from __future__ import annotations

from pathlib import Path

from beamer2slides import pieces
from beamer2slides.pieces import Cut, Merged


def checkpoint(label: str, page: int) -> str:
    return f"\\btwos@ckpt{{{label}}}{{0={page};5=7}}\n"


def test_a_deck_is_cut_after_labelled_frames_at_even_shares_of_its_pages() -> None:
    aux = "".join(checkpoint(f"f{k}", 2 * k) for k in range(1, 9))   # 8 frames of 2 pages
    plan = pieces.cuts(aux, 4)
    assert [(c.on_after, c.off_after) for c in plan] == [(None, "f2"), ("f2", "f4"), ("f4", "f6"), ("f6", None)]
    assert plan[1].spec() == "f2\nf4\n"


def test_no_cut_without_enough_labels_or_at_a_label_written_twice() -> None:
    assert pieces.cuts("\\relax\n", 4) == []
    assert pieces.cuts(checkpoint("a", 2) + checkpoint("b", 4), 4) == []
    # a label two frames share names no single place: never a cut
    twice = checkpoint("x", 2) + checkpoint("x", 4) + checkpoint("y", 6) + checkpoint("z", 8)
    assert all("x" not in (c.on_after, c.off_after) for c in pieces.cuts(twice, 2))
    assert pieces.cuts(checkpoint("a", 2) + checkpoint("b", 4), 1) == []


def test_the_pieces_own_parts_are_put_end_to_end_and_the_contents_files_written_from_them() -> None:
    head = "\\relax\n\\babel@aux{english}{}\n"
    first = head + "\\@writefile{nav}{one}\n\\@writefile{toc}{\\beamer@sectionintoc {1}{A}{1}{0}{1}}\n" + pieces.OFF + "\n"
    second = head + "\\@writefile{nav}{skipped}\n" + pieces.ON + "\n\\@writefile{nav}{two}\n\\gdef \\@abspage@last{3}\n"
    whole = pieces.merged([first, second], ["nav", "toc"])
    assert isinstance(whole, Merged)
    assert whole.pages == 3
    assert "skipped" not in whole.aux and pieces.ON not in whole.aux and pieces.OFF not in whole.aux
    assert whole.contents["nav"] == "one\ntwo\n"
    # babel writes its language into each contents file it switches, as \BabelContentsFiles says
    assert whole.contents["toc"] == "\\babel@toc {english}{}\\relax \n\\beamer@sectionintoc {1}{A}{1}{0}{1}\n"


def test_what_the_merge_does_not_know_sends_the_compile_back_whole() -> None:
    off, on = pieces.OFF + "\n", pieces.ON + "\n"
    assert isinstance(pieces.merged(["\\relax\n", on], []), str), "the first piece's end is not marked"
    assert isinstance(pieces.merged(["\\relax\n" + off, "\\relax\n"], []), str), "the last's start is not marked"
    unknown = pieces.merged(["\\relax\n\\mypackage@total{3}\n" + off, on], [])
    assert isinstance(unknown, str) and "mypackage@total" in unknown


def test_a_pass_that_has_not_settled_moves_each_later_piece_by_where_the_one_before_it_ended() -> None:
    """A frame of one page came into the first piece: the second began at the stale page 4
    instead of 5 and with register 5 at 1 instead of 2, and every checkpoint it wrote is off by
    as much; a register it set back (5, as a section resets its subsections) is not moved past
    that."""
    plan = [Cut(index=0, on_after=None, off_after="b"), Cut(index=1, on_after="b", off_after=None)]
    base = "\\btwos@base{0=1}\n"
    given = base + "\\btwos@ckpt{a}{0=2}\n\\btwos@ckpt{b}{0=4;5=1}\n\\btwos@ckpt{c}{0=6;5=3}\n"
    aux = (base + "\\btwos@ckpt{a}{0=2}\n\\btwos@ckpt{new}{0=3}\n\\btwos@ckpt{b}{0=5;5=2}\n"
           "\\btwos@ckpt{c}{0=6;5=3}\n\\btwos@ckpt{d}{0=8}\n\\newlabel{x}{{1}{6}}\n")
    guessed = pieces.carried(aux, given, plan)
    assert "\\btwos@ckpt{b}{0=5;5=2}\n" in guessed, "the first piece's own checkpoints are as it wrote them"
    assert "\\btwos@ckpt{c}{0=7;5=4}\n" in guessed
    assert "\\btwos@ckpt{d}{0=9}\n" in guessed, "5 was set back to where the document began: not moved"
    assert "\\newlabel{x}{{1}{6}}\n" in guessed, "only checkpoints are guessed: the next pass writes the rest"
    settled = pieces.carried(given, given, plan)
    assert settled == given


def test_fragile_frames_scratch_files_and_marks_left_for_a_whole_compile(tmp_path: Path) -> None:
    assert pieces.verbatim("talk.vrb", "talk") and pieces.verbatim("talk.2.vrb", "talk")
    assert not pieces.verbatim("talk.aux", "talk") and not pieces.verbatim("other.vrb", "talk")
    aux = tmp_path / "talk.aux"
    aux.write_text("\\relax\n\\btwos@base{0=1}\n\\btwos@ckpt{a}{0=2}\n\\btwos@own{0}\n\\newlabel{x}{{1}{2}}\n",
                   encoding="utf-8")
    pieces.drop_marks(aux)
    assert aux.read_text(encoding="utf-8") == "\\relax\n\\newlabel{x}{{1}{2}}\n"
