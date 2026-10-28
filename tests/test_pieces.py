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


def test_a_register_set_to_a_page_moves_with_the_pages_and_a_counter_by_what_it_moved() -> None:
    """A frame of one page came in ahead of the second piece. Register 7 is set to the page a
    section starts on (beamer's `\\beamer@sectionstartpage`): set again at frame d, to its page, it
    moves by the page the piece is off, not by what it moved before. Register 9 counts frames
    since the section (the slide in section): it moves by what it moved."""
    plan = [Cut(index=0, on_after=None, off_after="b"), Cut(index=1, on_after="b", off_after=None)]
    base = "\\btwos@base{0=1}\n"
    given = base + "\\btwos@ckpt{a}{0=2}\n\\btwos@ckpt{b}{0=4;7=2;9=2}\n\\btwos@ckpt{c}{0=5;7=2;9=3}\n" \
                   "\\btwos@ckpt{d}{0=6;7=5;9=1}\n"
    aux = (base + "\\btwos@ckpt{a}{0=2}\n\\btwos@ckpt{new}{0=3}\n\\btwos@ckpt{b}{0=5;7=2;9=3}\n"
           "\\btwos@ckpt{c}{0=5;7=2;9=3}\n\\btwos@ckpt{d}{0=6;7=5;9=1}\n")
    guessed = pieces.carried(aux, given, plan)
    assert "\\btwos@ckpt{c}{0=6;7=2;9=4}\n" in guessed, "a page and a count both moved by as much"
    assert "\\btwos@ckpt{d}{0=7;7=6;9=1}\n" in guessed, "7 now a page of frame d: one page on, not none"




def test_a_page_register_stops_moving_when_set_again_where_no_page_came() -> None:
    """A section came in without a page: where the second piece began, the page is as it was and
    the section's start page (7) is 3 instead of 1. Set again at frame d to its page, 7 is that
    page: it moves no more."""
    plan = [Cut(index=0, on_after=None, off_after="b"), Cut(index=1, on_after="b", off_after=None)]
    base = "\\btwos@base{0=1}\n"
    given = base + "\\btwos@ckpt{b}{0=4;7=1}\n\\btwos@ckpt{c}{0=5;7=1}\n\\btwos@ckpt{d}{0=6;7=6}\n"
    aux = base + "\\btwos@ckpt{b}{0=4;7=3}\n\\btwos@ckpt{c}{0=5;7=1}\n\\btwos@ckpt{d}{0=6;7=6}\n"
    guessed = pieces.carried(aux, given, plan)
    assert "\\btwos@ckpt{c}{0=5;7=3}\n\\btwos@ckpt{d}{0=6;7=6}\n" in guessed

def test_a_register_counting_along_with_the_pages_is_no_page_number() -> None:
    """A frame of two pages came in: the pages moved by two, the frame number (11, here one frame a
    page) by one. Counting up at each frame it is at the frame's page every time, as a page number
    would be once set, yet it moves by what it moved."""
    plan = [Cut(index=0, on_after=None, off_after="b"), Cut(index=1, on_after="b", off_after=None)]
    base = "\\btwos@base{0=1}\n"
    given = base + "\\btwos@ckpt{b}{0=4;11=4}\n\\btwos@ckpt{c}{0=5;11=5}\n\\btwos@ckpt{d}{0=6;11=6}\n"
    aux = (base + "\\btwos@ckpt{new}{0=3;11=3}\n\\btwos@ckpt{b}{0=6;11=5}\n"
           "\\btwos@ckpt{c}{0=5;11=5}\n\\btwos@ckpt{d}{0=6;11=6}\n")
    guessed = pieces.carried(aux, given, plan)
    assert "\\btwos@ckpt{c}{0=7;11=6}\n\\btwos@ckpt{d}{0=8;11=7}\n" in guessed

def test_each_piece_counts_the_pages_it_shipped_on_from_the_pieces_before_it() -> None:
    off, on = pieces.OFF + "\n", pieces.ON + "\n"
    first = "\\relax\n\\btwos@shipped{a}{2}\n\\btwos@shipped{b}{3}\n" + off
    second = "\\relax\n\\btwos@shipped{a}{2}\n" + on + "\\btwos@shipped{c}{1}\n\\btwos@shipped{d}{4}\n" + off
    third = "\\relax\n" + on + "\\btwos@shipped{e}{2}\n\\gdef \\@abspage@last{9}\n"
    whole = pieces.merged([first, second, third], [])
    assert isinstance(whole, Merged)
    assert [line for line in whole.aux.splitlines() if "shipped" in line] == [
        "\\btwos@shipped{a}{2}", "\\btwos@shipped{b}{3}", "\\btwos@shipped{c}{4}", "\\btwos@shipped{d}{7}",
        "\\btwos@shipped{e}{9}"]


PATCHED_AUX = ("\\relax\n" + "".join(f"\\btwos@ckpt{{f{k}}}{{0={2 * k}}}\n\\btwos@shipped{{f{k}}}{{{2 * k}}}\n"
                                     for k in range(1, 9)) + "\\gdef \\@abspage@last{16}\n")
PATCHED_TEX = ("\\documentclass{beamer}\n\\begin{document}\n\\section{S}\n"
               + "".join(f"\\begin{{frame}}[label=f{k}]{{T{k}}}words {k}\\end{{frame}}\n" for k in range(1, 9))
               + "\\end{document}\n")


def test_an_edit_inside_frames_typesets_them_again_and_keeps_every_other_page() -> None:
    edited = PATCHED_TEX.replace("words 3", "other words").replace("words 4", "more")
    found = pieces.patches(PATCHED_TEX, edited, PATCHED_AUX, 4)
    assert isinstance(found, list) and len(found) == 1, found
    run = found[0]
    assert (run.cut.on_after, run.cut.off_after, run.first, run.pages) == ("f2", "f4", 4, 4)
    two = pieces.patches(PATCHED_TEX, edited.replace("words 8", "end"), PATCHED_AUX, 4)
    assert isinstance(two, list)
    assert [(p.cut.on_after, p.cut.off_after, p.first, p.pages) for p in two] == [("f2", "f4", 4, 4), ("f7", "f8", 14, 2)]
    # what follows a frame, up to the next label, is typeset after its checkpoint: the next frame's
    assert pieces.patches(PATCHED_TEX, PATCHED_TEX, PATCHED_AUX, 4) == "no frame's text changed"
    between = pieces.patches(PATCHED_TEX, PATCHED_TEX.replace("words 5\\end{frame}\n", "words 5\\end{frame}\n\\relax\n"),
                             PATCHED_AUX, 4)
    assert isinstance(between, list) and [(p.cut.on_after, p.cut.off_after) for p in between] == [("f5", "f6")]


def test_an_edit_the_other_frames_pages_cannot_stand_through_is_refused() -> None:
    def refused(after: str, aux: str = PATCHED_AUX, pieces_: int = 4) -> str:
        found = pieces.patches(PATCHED_TEX, after, aux, pieces_)
        assert isinstance(found, str), found
        return found
    assert "structure" in refused(PATCHED_TEX.replace("words 3", "\\section{New} words 3"))
    assert "structure" in refused(PATCHED_TEX.replace("words 3", "words \\pause 3"))
    assert "structure" in refused(PATCHED_TEX.replace("words 3", "\\label{x}words 3"))
    assert "structure" in refused(PATCHED_TEX.replace("\\section{S}", "\\section{Renamed}"))
    assert "preamble" in refused(PATCHED_TEX.replace("{beamer}", "[t]{beamer}").replace("words 3", "w"))
    assert "label" in refused(PATCHED_TEX.replace("label=f4", "label=f9"))
    assert "label" in refused(PATCHED_TEX.replace("[label=f3]", "[label=f4]").replace("[label=f4]{T4}", "[label=f3]{T4}", 1))
    five = PATCHED_TEX
    for k in range(2, 7):
        five = five.replace(f"words {k}", "w")
    assert "pages" in refused(five, pieces_=2), "10 pages again, more than a piece's 8: a compile in pieces is faster"
    assert "record" in refused(PATCHED_TEX.replace("words 3", "w"), PATCHED_AUX.replace("\\btwos@shipped{f5}{10}\n", ""))
