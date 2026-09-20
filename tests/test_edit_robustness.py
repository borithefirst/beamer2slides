"""The edit-robustness measurement, offline: the sample, the edits and the judgement.

No corpus and no lualatex here - `run` is what needs those, and it is the one thing not tested.
The frames below are written by hand in both forms, small but shaped like the real ones, so an
edit that stops landing where it is meant to fails here rather than in a ten-minute compile run.
"""

import numpy as np

from beamer2slides.devtools import edit_robustness as er

OLD, NEW = er.OLD, er.NEW

# --------------------------------------------------------------------------------- two forms, one slide

M6_FRAME = r"""% slide 1
\begin{frame}[plain]
  \begin{textblock*}{414.09bp}(9.3bp,13.4bp)
    \vbox to 28.4bp{\slidesbox
    {\leftskip=0.00bp\relax
      \noindent\slidesize{15.87}\color{black}\relax%
      Layer 2 Header
    \baselineskip=18.90bp\par}
    \vss}
  \end{textblock*}
  \begin{textblock*}{200.00bp}(40.0bp,80.0bp)
    \vbox to 60.0bp{\slidesbox
    {\leftskip=0.00bp\relax
      \noindent\slidesize{12.00}\color{black}\relax%
      Alpha beta gamma delta epsilon
    \baselineskip=14.00bp\par}
    {\leftskip=0.00bp\relax
      \noindent\slidesize{12.00}\color{black}\relax%
      Zeta eta theta iota kappa lambda
    \baselineskip=14.00bp\par}
    \vss}
  \end{textblock*}
\end{frame}"""

LS_FRAME = r"""% slide 1
\begin{frame}[plain,layout=title-and-body]
  \frametitle{Layer 2 Header}
  \begin{slidebox}[]{40.0,80.0,200.0,60.0}
    \slidepar[style=body]{Alpha beta gamma delta epsilon}
    \slidepar[style=body]{Zeta eta theta iota kappa lambda}
  \end{slidebox}
\end{frame}"""

WORDS = "Alpha beta gamma delta epsilon"
BOX = [40.0, 80.0, 240.0, 140.0]


def frames() -> dict[str, list[str]]:
    return {OLD: M6_FRAME.split("\n"), NEW: LS_FRAME.split("\n")}


def anchor(**kw) -> er.Anchor:
    got = er.Anchor(text=WORDS, kind="par", bbox=list(BOX))
    for k, v in kw.items():
        setattr(got, k, v)
    return got


def applied(name: str, tag: str, a: er.Anchor | None = None) -> er.Applied:
    got = er.EDITS[name](tag, frames()[tag], a or anchor())
    assert got is not None, f"{name} did not apply in {tag}"
    return got


def changes(before: list[str], after: list[str]) -> list[str]:
    """The lines the edit wrote (added or altered), stripped."""
    old = list(before)
    out = []
    for ln in after:
        if ln in old:
            old.remove(ln)
        else:
            out.append(ln.strip())
    return out


# ------------------------------------------------------------------------------------ what an edit says

def test_rewording_longer_writes_the_same_words_on_one_line_of_each_form():
    """One line changed in either form, the old words still there and half as many again after
    them - the edit a person makes when a sentence grows."""
    for tag in er.FORMS:
        got = applied("reword-longer", tag)
        assert got.touched == 1
        wrote = changes(frames()[tag], got.lines)
        assert len(wrote) == 1 and WORDS in wrote[0]
        assert len(wrote[0].split()) - len(frames()[tag][er.sole_line(frames()[tag], WORDS)].split()) >= 2
        assert got.added and got.added in wrote[0]
        assert not got.grow                      # a reworded line may not claim the room below it


def test_rewording_shorter_drops_the_tail_of_the_line():
    for tag in er.FORMS:
        got = applied("reword-shorter", tag)
        assert got.touched == 1
        assert "epsilon" not in "\n".join(got.lines)
        assert got.removed == "beta gamma delta epsilon"


def test_restyling_a_run_bolds_the_first_two_words_only():
    for tag in er.FORMS:
        got = applied("restyle-run", tag)
        wrote = changes(frames()[tag], got.lines)
        assert len(wrote) == 1
        assert "\\textbf{Alpha beta} gamma delta epsilon" in wrote[0]
        assert got.touched == 1


def test_the_title_is_changed_where_each_form_keeps_it():
    r"""`ls-a` has a real `\frametitle`; `m6-a` has the words inside a `textblock*`. Both cost one
    line, and the edit is judged in the title's own box, not the anchor's."""
    a = anchor(title="Layer 2 Header", title_bbox=[9.3, 13.4, 423.4, 41.8])
    ls = applied("change-title", NEW, a)
    assert "\\frametitle{" + ls.added + "}" in [ln.strip() for ln in ls.lines]
    m6 = applied("change-title", OLD, a)
    assert changes(frames()[OLD], m6.lines)[0].strip() == m6.added
    for got in (ls, m6):
        assert got.touched == 1 and got.removed == "Layer 2 Header"
        assert got.where == "title" and not got.grow and got.shift == 0.0
    assert "Layer 2 Header" not in "\n".join(ls.lines + m6.lines)


def test_moving_a_box_moves_the_line_that_places_it_in_either_form():
    r"""The box's x, wherever the form keeps it: `slidebox`'s first coordinate, `textblock*`'s. The
    edit says how far it moves, so the judgement allows the box that much room to the right and
    nothing else."""
    ls = applied("move-box", NEW)
    assert changes(frames()[NEW], ls.lines) == ["\\begin{slidebox}[]{60.00,80.0,200.0,60.0}"]
    m6 = applied("move-box", OLD)
    assert changes(frames()[OLD], m6.lines) == ["\\begin{textblock*}{200.00bp}(60.00bp,80.0bp)"]
    for got in (ls, m6):
        assert got.touched == 1 and got.where == "anchor" and got.shift == er.SHIFT


def test_a_box_placed_by_a_turn_is_never_moved():
    r"""A box inside `\adoptturned` is put on the page by the turn as much as by its own x: moving
    the x alone would measure the tool, not the form."""
    lines = list(frames()[OLD])
    k = next(i for i, ln in enumerate(lines) if "(40.0bp,80.0bp)" in ln)
    lines.insert(k, "  \\adoptturned{90}{")
    assert er.ed_move_box(OLD, lines, anchor()) is None


def test_adding_a_paragraph_costs_one_line_in_the_new_form_and_the_whole_group_in_the_old():
    """The same edit: one more paragraph after the anchor's, in the same box, saying new words."""
    ls = applied("add-paragraph", NEW)
    assert ls.touched == 1
    assert changes(frames()[NEW], ls.lines) == ["\\slidepar[style=body]{" + ls.added + "}"]
    m6 = applied("add-paragraph", OLD)
    before = frames()[OLD]
    assert m6.touched == 4                       # {\leftskip=..  \noindent..  words  \baselineskip..\par}
    assert len(m6.lines) == len(before) + 4
    end = next(i for i in range(er.sole_line(before, WORDS), len(before)) if "\\par}" in before[i])
    block = [ln.strip() for ln in m6.lines[end + 1:end + 5]]
    assert block[0].startswith("{\\leftskip=") and block[-1].endswith("\\par}")
    assert block[2] == m6.added                  # the same plumbing, the new words in the middle
    for got in (ls, m6):
        assert got.grow                          # a box given more to say may grow downward


def test_a_one_shot_slidetext_becomes_the_box_it_is_short_for():
    r"""`\slidetext{box}{style}{words}` has no room for a second paragraph, so the edit writes the
    `slidebox` it abbreviates - the box keys stay on the box, the rest on both paragraphs."""
    lines = [r"  \slidetext[middle, tail=2, center]{40,80,200,60}{body}{Alpha beta gamma delta epsilon}"]
    got = er.ed_add_paragraph(NEW, lines, anchor(kind="single"))
    assert got is not None and got.touched == 4 and got.grow
    assert [ln.strip() for ln in got.lines] == [
        r"\begin{slidebox}[middle, tail=2]{40,80,200,60}",
        r"\slidepar[style=body, center]{Alpha beta gamma delta epsilon}",
        r"\slidepar[style=body, center]{" + got.added + "}",
        r"\end{slidebox}"]


LS_LIST = r"""\begin{frame}[plain]
  \begin{slidebox}[]{40.0,80.0,200.0,60.0}
    \begin{itemize}
      \item Alpha beta gamma delta epsilon
      \item Zeta eta theta iota kappa lambda
    \end{itemize}
  \end{slidebox}
\end{frame}""".split("\n")


def test_an_item_is_added_and_deleted_by_its_own_line():
    a = anchor(kind="item")
    add = er.ed_add_item(NEW, LS_LIST, a)
    assert add is not None and add.touched == 1 and add.grow
    assert changes(LS_LIST, add.lines) == ["\\item " + add.added]
    drop = er.ed_delete_item(NEW, LS_LIST, a)
    assert drop is not None and drop.touched == 1
    assert WORDS not in "\n".join(drop.lines)
    assert drop.removed == WORDS


def test_an_item_with_a_list_under_it_is_left_alone():
    """Deleting such a line would take the sub-list's `\\item`s out of any list: not the edit asked
    for, and `m6-a` has no counterpart to it, so the slide simply does not take these two edits."""
    lines = list(LS_LIST)
    k = er.sole_line(lines, WORDS)
    lines.insert(k + 1, "      \\begin{itemize}")
    lines.insert(k + 2, "        \\item Deeper")
    lines.insert(k + 3, "      \\end{itemize}")
    assert er.ed_add_item(NEW, lines, anchor(kind="item")) is None
    assert er.ed_delete_item(NEW, lines, anchor(kind="item")) is None


# ------------------------------------------------------------------------------------------- the table

LS_TABLE = r"""\begin{frame}[plain]
  \begin{slidetable}[inset=4.5, h=19.56, fixed, fill=LightYellow]{92.1,83}{100.0,80.0}
    \row[h=19.43] Head & Count \\
    Data & Twelve \\
  \end{slidetable}
  \slidetext[center]{29.6,224.2,394.36,28.4}{body}{Alpha beta gamma delta epsilon}
\end{frame}""".split("\n")

M6_TABLE = r"""\begin{frame}[plain]
  \begin{textblock*}{180.0bp}(92.1bp,83.0bp)
    \adoptrow{0}{19.43bp}
    \adoptrow{1}{19.56bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptcell{1}{0}{0}{91.0bp}{4.54bp}{100.0bp}{-4.54bp}{%
      \raggedright\footnotesize\relax%
      Head}
    \adoptcell{2}{0}{0}{71.0bp}{4.54bp}{80.0bp}{-4.54bp}{%
      \raggedright\footnotesize\relax%
      Count}
    \adoptcell{3}{1}{1}{91.0bp}{4.54bp}{100.0bp}{-4.54bp}{%
      \raggedright\footnotesize\relax%
      Data}
    \adoptcell{4}{1}{1}{71.0bp}{4.54bp}{80.0bp}{-4.54bp}{%
      \raggedright\footnotesize\relax%
      Twelve}
    \adopttops{2}
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \path[use as bounding box] (0bp,0bp) rectangle (180.0bp,{-\adopty{2}});
      \draw[black,line width=0.94bp] (0.0bp,{-\adopty{0}}) -- (180.0bp,{-\adopty{0}});
      \draw[black,line width=0.94bp] (0.0bp,{-\adopty{2}}) -- (180.0bp,{-\adopty{2}});
      \draw[black,line width=0.94bp] (0.0bp,{-\adopty{0}}) -- (0.0bp,{-\adopty{2}});
      \draw[black,line width=0.94bp] (180.0bp,{-\adopty{0}}) -- (180.0bp,{-\adopty{2}});
      \node[anchor=north west] at (4.5bp,{-\adopty{0}-13.18bp+\adoptht{1}}) {\adoptbox{1}};
      \node[anchor=north west] at (104.5bp,{-\adopty{0}-13.18bp+\adoptht{2}}) {\adoptbox{2}};
      \node[anchor=north west] at (4.5bp,{-\adopty{1}-13.18bp+\adoptht{3}}) {\adoptbox{3}};
      \node[anchor=north west] at (104.5bp,{-\adopty{1}-13.18bp+\adoptht{4}}) {\adoptbox{4}};
    \end{tikzpicture}
  \end{textblock*}
\end{frame}""".split("\n")

TABLE_BOX = [92.1, 83.0, 272.1, 83.0]


def test_a_row_is_one_line_in_the_new_form():
    got = er.ed_add_table_row(NEW, LS_TABLE, anchor(table_bbox=TABLE_BOX))
    assert got is not None and got.touched == 1 and got.grow
    assert changes(LS_TABLE, got.lines) == ["Newly & added \\\\"]
    assert got.added == "Newly" and got.where == "table"


def test_a_row_in_the_old_form_touches_every_piece_that_counts_the_rows():
    r"""There a row is a height, a fix, a cell box per column, the count of tops, the bounding box,
    the foot rule, the far end of each vertical rule and a node per cell - all by number."""
    got = er.ed_add_table_row(OLD, M6_TABLE, anchor(table_bbox=TABLE_BOX))
    assert got is not None
    text = "\n".join(got.lines)
    wrote = changes(M6_TABLE, got.lines)
    assert "\\adoptrow{2}{19.56bp}" in wrote                     # the new row, as tall as the last
    assert "\\adoptfix{2}" in wrote
    assert "\\adoptcell{5}{2}{2}{91.0bp}{4.54bp}{100.0bp}{-4.54bp}{%" in wrote
    assert "\\adoptcell{6}{2}{2}{71.0bp}{4.54bp}{80.0bp}{-4.54bp}{%" in wrote
    assert "Newly}" in wrote and "added}" in wrote
    assert "\\adopttops{3}" in text and "\\adopttops{2}" not in text
    assert "rectangle (180.0bp,{-\\adopty{3}})" in text          # the box now reaches the new foot
    assert "(0.0bp,{-\\adopty{3}}) -- (180.0bp,{-\\adopty{3}})" in text
    assert "(0.0bp,{-\\adopty{0}}) -- (0.0bp,{-\\adopty{3}})" in text
    assert "\\adoptbox{5}" in text and "\\adoptbox{6}" in text
    assert got.touched > 8 and got.added == "Newly"


def test_the_table_region_is_the_table_the_source_draws_not_the_box_the_ir_reports():
    """A Slides table sizes itself to its rows, so the element box the API gives is not the table on
    the slide; the source says it exactly, and the two forms agree on it (the old form's
    `textblock*` is 180 bp wide too), so a row that stays in the table is not read as a breach."""
    slide = {"size": [453.5, 255.1],
             "elements": [{"kind": "table", "bbox": [92.1, 83.0, 140.0, 150.0]}]}
    got = er.table_of(slide, {NEW: LS_TABLE, OLD: M6_TABLE})
    assert got == [92.1, 83.0, 92.1 + 180.0, 83.0]


def test_a_slide_whose_forms_do_not_both_hold_a_table_takes_no_row():
    slide = {"size": [453.5, 255.1], "elements": [{"kind": "table", "bbox": [0, 0, 1, 1]}]}
    assert er.table_of(slide, {NEW: LS_TABLE, OLD: M6_FRAME.split("\n")}) is None
    assert er.table_of(slide, {NEW: LS_FRAME.split("\n"), OLD: M6_TABLE}) is None
    assert er.ed_add_table_row(NEW, LS_TABLE, anchor(table_bbox=None)) is None


# ------------------------------------------------------------------------- only what both forms can take

def test_an_edit_only_one_form_can_take_is_left_out_of_the_sample():
    """An edit scored in one form and not in the other is no comparison: a table row can be written
    into the new form's frame here, and the old form's frame has no table, so neither is credited."""
    pick = {"anchor": anchor(table_bbox=TABLE_BOX).__dict__}
    assert er.ed_add_table_row(NEW, LS_TABLE, anchor(table_bbox=TABLE_BOX)) is not None
    assert "add-table-row" not in er.both_forms(pick, {NEW: LS_TABLE, OLD: M6_FRAME.split("\n")})
    assert "add-table-row" in er.both_forms(pick, {NEW: LS_TABLE, OLD: M6_TABLE})
    both = er.both_forms({"anchor": anchor().__dict__}, frames())
    assert set(both) == {"reword-longer", "reword-shorter", "restyle-run", "move-box", "add-paragraph"}


# ------------------------------------------------------------------------------------------ the sample

def index(n: int = 60) -> list[dict]:
    out = []
    for k in range(n):
        out.append({"deck": f"deck{k % 6}", "slide": k,
                    "category": er.CATEGORIES[k % len(er.CATEGORIES)], "anchor": anchor().__dict__})
    return out


def test_the_same_seed_always_draws_the_same_slides():
    a = er.pick_sample(index(), 7, 12)
    b = er.pick_sample(list(reversed(index())), 7, 12)
    assert [(p["deck"], p["slide"]) for p in a] == [(p["deck"], p["slide"]) for p in b]
    assert [(p["deck"], p["slide"]) for p in a] != \
           [(p["deck"], p["slide"]) for p in er.pick_sample(index(), 8, 12)]


def test_the_sample_is_even_over_the_categories_and_over_the_decks():
    picks = er.pick_sample(index(), 7, 12)
    assert len(picks) == 12
    assert sorted(sum(1 for p in picks if p["category"] == c) for c in er.CATEGORIES) == [2] * 6
    counts = [sum(1 for p in picks if p["deck"] == f"deck{k}") for k in range(6)]
    assert max(counts) - min(counts) <= 1        # every deck used once before any is used twice


def test_a_category_that_runs_out_does_not_stop_the_others():
    small = [r for r in index() if r["category"] != "table"]
    picks = er.pick_sample(small, 7, 10)
    assert len(picks) == 10 and not any(p["category"] == "table" for p in picks)


def test_a_slide_is_sorted_into_the_kind_of_slide_it_is():
    def slide(els):
        return {"size": [400, 300], "elements": els}

    def text(paras, **kw):
        return {"kind": "text", "bbox": [0, 0, 10, 10],
                "paragraphs": [{"runs": [{"text": t, "size": 12}], **({"bullet": "DISC"} if b else {})}
                               for t, b in paras], **kw}

    assert er.slide_category(slide([{"kind": "table", "bbox": [0, 0, 9, 9]}])) == "table"
    assert er.slide_category(slide([text([("a", 1), ("b", 1), ("c", 1)])])) == "list"
    assert er.slide_category(slide([{"kind": "shape", "bbox": [0, 0, 1, 1]}] * 4)) == "shape"
    assert er.slide_category(slide([{"kind": "image", "bbox": [0, 0, 200, 200]}])) == "picture"
    assert er.slide_category(slide([{"kind": "text", "bbox": [0, 0, 9, 9],
                                     "paragraphs": [{"runs": [{"text": "Big", "size": 40}]}]}])) == "title"
    assert er.slide_category(slide([text([("a", 0), ("b", 0)])])) == "prose"
    assert er.slide_category(slide([])) is None


def test_the_anchor_is_a_paragraph_both_forms_print_on_one_line_of_their_own():
    slide = {"size": [453.5, 255.1],
             "elements": [{"kind": "text", "bbox": list(BOX), "paragraphs": [
                 {"runs": [{"text": WORDS, "size": 12}]},
                 {"runs": [{"text": "Zeta eta theta iota kappa lambda", "size": 12}]}]}]}
    got = er.anchor_of(slide, frames())
    assert got is not None and got.bbox == BOX
    assert got.text in (WORDS, "Zeta eta theta iota kappa lambda")
    assert got.kind == "par"


def test_a_paragraph_no_person_would_retype_is_never_the_anchor():
    """Words in another script, a ligature the PDF gives back as one character, or LaTeX's own
    specials: the judgement reads the page's text, and this tool's filler is Latin."""
    assert er.plain("Alpha beta gamma")
    assert not er.plain("ההגנה על המידע")            # a Latin filler here would test bidi, not the form
    assert not er.plain("efficient filing")          # fi, fl: one character in the PDF
    assert not er.plain("50% of $x$")


# --------------------------------------------------------------------------------------- the judgement

def test_a_failed_compile_is_reported_with_what_tex_said():
    ok, why = er.compile_verdict(None, "! Undefined control sequence.\nl.42 \\slidepar\n")
    assert not ok and why == "Undefined control sequence."
    ok, why = er.compile_verdict(None, "main.tex:31: Missing } inserted.\nsomething else\n")
    assert not ok and why == "Missing } inserted."
    ok, why = er.compile_verdict(None, "")
    assert not ok and why == "no output"
    assert er.compile_verdict(__import__("pathlib").Path("main.pdf"), "") == (True, "")


def page(*rects) -> np.ndarray:
    img = np.full((100, 200, 3), 255, np.uint8)
    for x0, y0, x1, y1 in rects:
        img[y0:y1, x0:x1] = 0
    return img


def test_ink_that_stays_in_the_box_passes_and_ink_that_leaves_it_names_the_side():
    """The box is the element's, PAD around it; the page is 200 x 100 px for a 200 x 100 pt page."""
    region = er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200))
    inside = er.judge_pixels(page((25, 25, 45, 45)), page((25, 25, 55, 55)), region)
    assert inside["confined"] and inside["visible"] and inside["breach"] == []
    out = er.judge_pixels(page((25, 25, 45, 45)), page((25, 25, 120, 45)), region)
    assert not out["confined"] and out["breach"] == ["right"]
    assert out["outside"] > er.BREACH_PX
    below = er.judge_pixels(page((25, 25, 45, 45)), page((25, 25, 45, 95)), region)
    assert below["breach"] == ["bottom"]


def test_a_page_that_did_not_change_is_not_a_pass_however_well_it_compiled():
    same = er.judge_pixels(page((25, 25, 45, 45)), page((25, 25, 45, 45)),
                           er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200)))
    assert same["changed"] == 0 and not same["visible"]
    assert er.verdict({"compiled": True, "visible": False, "confined": True}) == "invisible"


def test_an_element_is_the_lines_a_compile_without_it_must_leave_out():
    r"""What `element_inks` deletes to see what the element already covers: the `textblock*` in the
    old form, the `slidebox` in the new one, a one-shot `\slidetext` its own line."""
    old, new = frames()[OLD], frames()[NEW]
    assert er.element_span(OLD, old, er.sole_line(old, WORDS)) == (10, 21)
    assert er.element_span(OLD, old, er.sole_line(old, "Layer 2 Header")) == (2, 9)
    assert er.element_span(NEW, new, er.sole_line(new, WORDS)) == (3, 6)
    one = ["\\begin{frame}[plain]",
           "  \\slidetext{9.3,13.4,414.1,28.4}{title}{Layer 2 Header}",
           "\\end{frame}"]
    assert er.element_span(NEW, one, 1) == (1, 1)          # the macro is the whole element
    assert er.element_span(NEW, ["\\begin{frame}", "  words", "\\end{frame}"], 1) is None


def test_each_form_says_which_way_its_box_lays_text_out_in_its_own_words():
    r"""`m6-a` puts `\vss` where the slack goes; `ls-a` names it as a box key. Same fact about the
    same element, read from each form's own syntax."""
    old, new = frames()[OLD], frames()[NEW]
    assert er.box_align(OLD, old, er.sole_line(old, WORDS)) == "top"
    assert er.box_align(NEW, new, er.sole_line(new, WORDS)) == "top"
    down = [ln.replace("\\vbox to 60.0bp{\\slidesbox", "\\vbox to 60.0bp{\\slidesbox\n    \\vss")
            .replace("    \\vss}", "    }") for ln in old]
    down = "\n".join(down).split("\n")
    assert er.box_align(OLD, down, er.sole_line(down, WORDS)) == "bottom"
    mid = "\n".join(old).replace("\\vbox to 60.0bp{\\slidesbox",
                                 "\\vbox to 60.0bp{\\slidesbox\n    \\vss").split("\n")
    assert er.box_align(OLD, mid, er.sole_line(mid, WORDS)) == "middle"
    for key, want in (("bottom", "bottom"), ("middle", "middle"), ("inset=0", "top")):
        ls = "\n".join(new).replace("\\begin{slidebox}[]", f"\\begin{{slidebox}}[{key}]").split("\n")
        assert er.box_align(NEW, ls, er.sole_line(ls, WORDS)) == want
    one = ["\\begin{frame}", "  \\slidetext[bottom,right]{76,95.6,377,57.5}{title}{" + WORDS + "}",
           "\\end{frame}"]
    assert er.box_align(NEW, one, 1) == "bottom"


def test_the_room_an_element_already_takes_is_the_page_without_it():
    """A `confined` that counted only the IR box would charge every edit for a line the slide
    already hangs below its box - Slides allows that and both forms reproduce it. `ink_hull` is
    that room, measured, and `box_mask` adds it to the box."""
    base, without = page((25, 25, 45, 95)), page()
    assert er.ink_hull(base, without) == (25, 25, 45, 95)
    assert er.ink_hull(base, base) is None                 # nothing of it on the page: no claim
    assert er.ink_hull(base, np.zeros((50, 200, 3), np.uint8)) is None
    box = [20.0, 20.0, 60.0, 60.0]
    tight = er.box_mask(box, (200.0, 100.0), (100, 200))
    assert not tight[90, 40]
    wide = er.box_mask(box, (200.0, 100.0), (100, 200), ink=(25, 25, 45, 95))
    assert wide[90, 40] and not wide[90, 120]              # down to the ink, no further sideways
    assert er.judge_pixels(base, page((25, 25, 55, 95)), wide)["confined"]


def test_a_box_that_was_moved_is_allowed_exactly_the_room_it_moved_into():
    moved = er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200), shift=er.SHIFT)
    assert moved[40, int(60 + er.PAD + er.SHIFT) - 1] and not moved[40, int(60 + er.PAD + er.SHIFT) + 2]


def test_a_box_given_more_to_say_grows_the_way_its_own_alignment_says():
    """Never sideways, and towards the edge the flow runs to: a bottom-aligned box given one more
    line grows *upward*, as it does in Slides, so judging it downward would charge it for the one
    direction it can never use (drawings-basics slide 11 is exactly that box)."""
    grown = er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200), grow="top")
    assert grown[95, 40] and not grown[95, 120] and not grown[2, 40]
    assert er.judge_pixels(page((25, 25, 45, 45)), page((25, 25, 45, 95)), grown)["confined"]
    up = er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200), grow="bottom")
    assert up[2, 40] and not up[95, 40] and not up[2, 120]
    assert er.judge_pixels(page((25, 35, 45, 55)), page((25, 5, 45, 55)), up)["confined"]
    both = er.box_mask([20.0, 20.0, 60.0, 60.0], (200.0, 100.0), (100, 200), grow="middle")
    assert both[2, 40] and both[95, 40] and not both[50, 120]


def test_a_box_is_judged_in_the_units_the_page_is_written_in():
    """A deck whose slide size is no paper size beamer knows is written at the paper's scale
    (poster-48x36: 362.8 x 272.1 in the IR, 1728 x 1296 bp on paper), and an unscaled box would be
    held against a piece of the page the element is nowhere near. The table's box is the source's
    already, so it is left alone."""
    a = anchor(title_bbox=[10.0, 20.0, 30.0, 40.0], table_bbox=[1.0, 2.0, 3.0, 4.0])
    same = er.on_page(a, [453.54, 255.12], (453.54, 255.12))
    assert same.bbox == BOX and same.title_bbox == [10.0, 20.0, 30.0, 40.0]
    got = er.on_page(a, [362.83, 272.13], (1725.0, 1293.0))
    assert got.bbox[0] == 40.0 * (1725.0 / 362.83)
    assert got.bbox[3] == 140.0 * (1293.0 / 272.13)
    assert got.title_bbox[2] == 30.0 * (1725.0 / 362.83)
    assert got.table_bbox == [1.0, 2.0, 3.0, 4.0]
    assert er.on_page(a, None, (100.0, 50.0)).bbox == BOX


def test_a_page_of_another_size_is_a_breach_not_a_crash():
    got = er.judge_pixels(np.zeros((10, 10, 3), np.uint8), np.zeros((12, 10, 3), np.uint8),
                          np.ones((10, 10), bool))
    assert not got["confined"] and got["breach"] == ["size"]


def test_the_words_the_edit_writes_must_be_on_the_page_and_the_ones_it_drops_must_not():
    """The PDF's text has no word spaces (TeX draws gaps as kerns), so both sides are squashed."""
    assert er.judge_text("AlphabetagammaNewlyadded", "Alphabetagamma", "Newly added", None)[0]
    assert not er.judge_text("Alphabetagamma", "Alphabetagamma", "Newly added", None)[0]
    assert er.judge_text("Alphabeta", "Alphabetagamma", None, "gamma")[0]
    assert not er.judge_text("Alphabetagamma", "Alphabetagamma", None, "gamma")[0]
    # a word the baseline page says twice cannot say whether this one went: the check stands down
    assert er.judge_text("gammagamma", "gammagamma", None, "gamma")[0]


def test_the_verdict_puts_the_failures_in_the_order_they_matter():
    assert er.verdict({"compiled": False}) == "compile"
    assert er.verdict({"compiled": True, "visible": True, "text_ok": False}) == "invisible"
    assert er.verdict({"compiled": True, "visible": True, "text_ok": True, "confined": False}) == "breach"
    assert er.verdict({"compiled": True, "visible": True, "text_ok": True, "confined": True}) == "pass"


def test_the_summary_counts_only_the_edits_that_applied_in_a_form():
    rows = [{"tag": OLD, "edit": "reword-longer", "applies": True, "verdict": "pass", "lines": 1},
            {"tag": NEW, "edit": "reword-longer", "applies": True, "verdict": "pass", "lines": 1},
            {"tag": OLD, "edit": "add-paragraph", "applies": True, "verdict": "breach", "lines": 4},
            {"tag": NEW, "edit": "add-paragraph", "applies": True, "verdict": "pass", "lines": 1},
            {"tag": OLD, "edit": "add-table-row", "applies": False},
            {"tag": NEW, "edit": "add-table-row", "applies": False}]
    got = er.summary(rows)
    assert "add-table-row" not in got["edits"]
    assert got["edits"]["add-paragraph"][OLD] == {"n": 1, "pass": 0, "lines": 4.0}
    assert got["edits"]["add-paragraph"][NEW] == {"n": 1, "pass": 1, "lines": 1.0}
    assert got["forms"][OLD] == {"n": 2, "pass": 1, "lines": 5}
    assert got["forms"][NEW] == {"n": 2, "pass": 2, "lines": 2}
