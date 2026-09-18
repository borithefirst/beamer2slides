"""Frame labels (src/beamer2slides/labels.py, docs/labels.md).

A label is the only piece of a slide's identity that survives compiling, so these tests are about
two promises: a label this program writes lands where beamer reads it, and a label somebody else
wrote is never touched.
"""

from pathlib import Path

import pytest

from beamer2slides import labels, texmap


def source(tmp_path: Path, body: str, name: str = "main.tex") -> texmap.Source:
    path = tmp_path / name
    path.write_text("\\documentclass{beamer}\n\\begin{document}\n" + body + "\n\\end{document}\n", encoding="utf-8")
    return texmap.Source(path)


def frame(title: str = "A frame", opts: str = "", overlay: str = "", body: str = "text") -> str:
    return f"\\begin{{frame}}{overlay}{opts}{{{title}}}\n{body}\n\\end{{frame}}\n"


# ---------------------------------------------------------------- slugs

def test_a_label_is_readable_and_safe_in_an_option_list():
    assert labels.slug("Why decks diverge", set()) == "why-decks-diverge"
    # a beamer option list is split on commas and brackets, and a PDF destination is not prose
    assert labels.slug("Results: 50% (q1, q2) - \u00e9t\u00e9!", set()) == "results-50-q1-q2-ete"
    assert labels.slug(None, set()) == "frame"
    assert labels.slug("2024 in review", set()).startswith("f-2024")


def test_a_label_never_collides_with_one_that_is_already_there():
    taken = {"results"}
    first = labels.slug("Results", taken)
    taken.add(first)
    second = labels.slug("Results", taken)
    assert first == "results-2" and second == "results-3"
    assert len({first, second} & {"results"}) == 0


def test_a_very_long_title_is_cut_but_stays_distinct():
    title = "A frame whose title goes on and on and on past any reasonable length"
    a = labels.slug(title, set())
    b = labels.slug(title, {a})
    assert len(a) <= labels.MAX_SLUG and len(b) <= labels.MAX_SLUG and a != b


# ---------------------------------------------------------------- where the label is written

def test_a_frame_with_no_options_gets_its_own_bracket(tmp_path):
    src = source(tmp_path, frame("Intro"))
    (edit,) = labels.plan(src)
    assert edit["text"] == "[label=intro]"
    (_, text), = labels.apply(src, [edit]).items()
    assert "\\begin{frame}[label=intro]{Intro}" in text


def test_a_frame_that_has_options_keeps_them(tmp_path):
    src = source(tmp_path, frame("Intro", opts="[t,allowframebreaks]"))
    (edit,) = labels.plan(src)
    (_, text), = labels.apply(src, [edit]).items()
    assert "\\begin{frame}[t,allowframebreaks,label=intro]{Intro}" in text


def test_an_empty_option_list_does_not_get_a_stray_comma(tmp_path):
    src = source(tmp_path, frame("Intro", opts="[]"))
    (_, text), = labels.apply(src, labels.plan(src)).items()
    assert "\\begin{frame}[label=intro]{Intro}" in text


def test_the_label_goes_after_an_overlay_specification(tmp_path):
    """`\\begin{frame}<2->[t]{...}`: the `<...>` is not an option list, and a label written before
    it would not be one either."""
    src = source(tmp_path, frame("Intro", overlay="<2->"))
    (_, text), = labels.apply(src, labels.plan(src)).items()
    assert "\\begin{frame}<2->[label=intro]{Intro}" in text


def test_a_frame_that_already_has_a_label_is_left_alone(tmp_path):
    """The promise this module exists to keep: an existing label is what some deck was converted
    from, and rewriting it would tell sync that a frame it knows is a different frame."""
    src = source(tmp_path, frame("Intro", opts="[label=whatever-they-chose]") + frame("Next"))
    plan = labels.plan(src)
    assert [e["label"] for e in plan] == ["next"]
    (_, text), = labels.apply(src, plan).items()
    assert "label=whatever-they-chose" in text and text.count("label=") == 2


def test_several_frames_in_one_file_are_all_labelled_correctly(tmp_path):
    """Every edit is an offset into the file as it was read, so they have to be written back to
    front or each one moves the next."""
    src = source(tmp_path, "".join(frame(t) for t in ("One", "Two", "Three")))
    (_, text), = labels.apply(src, labels.plan(src)).items()
    for t in ("one", "two", "three"):
        assert f"\\begin{{frame}}[label={t}]{{{t.title()}}}" in text


def test_frames_of_an_input_file_are_labelled_too(tmp_path):
    (tmp_path / "part.tex").write_text(frame("Included"), encoding="utf-8")
    src = source(tmp_path, frame("Main") + "\\input{part}\n")
    plan = labels.plan(src)
    assert sorted(e["label"] for e in plan) == ["included", "main"]
    written = labels.apply(src, plan)
    assert len(written) == 2
    assert "[label=included]" in written[tmp_path / "part.tex"]


def test_two_untitled_frames_get_labels_of_their_own(tmp_path):
    src = source(tmp_path, "\\begin{frame}\na\n\\end{frame}\n\\begin{frame}\nb\n\\end{frame}\n")
    assert [e["label"] for e in labels.plan(src)] == ["frame", "frame-2"]


# ---------------------------------------------------------------- what a converted deck says

def info(label=None, title="A title"):
    return {"label": label, "title": title, "text": ""}


def test_a_deck_whose_frames_all_carry_labels_has_nothing_to_report():
    found = labels.survey([info("a", "A"), info("b", "B")])
    assert found["unlabelled"] == [] and found["duplicates"] == []
    assert labels.problems(found) == []


def test_unlabelled_frames_are_named_and_counted():
    found = labels.survey([info("a", "A"), info(None, "Loose"), info(None, "Also loose")])
    assert [u["title"] for u in found["unlabelled"]] == ["Loose", "Also loose"]
    (line,) = labels.problems(found)
    assert "2 of 3 frames have no label" in line and "Loose" in line


def test_overlay_steps_of_one_frame_are_not_a_duplicate_label():
    """Every step of a frame carries the frame's label; `--overlays all` keeps them all."""
    found = labels.survey([info("build", "Building up"), info("build", "Building up"), info("end", "End")])
    assert found["duplicates"] == [] and found["frames"] == 2


def test_the_same_label_on_two_different_frames_is_reported():
    found = labels.survey([info("shared", "First frame"), info("shared", "Second frame")])
    (d,) = found["duplicates"]
    assert d["label"] == "shared" and d["titles"] == ["First frame", "Second frame"]
    assert "on more than one frame" in labels.problems(found)[0]


def test_a_label_that_comes_back_later_in_the_deck_is_reported():
    """Steps of a frame are consecutive. The same label on slides 0 and 2 is two frames, whatever
    their titles say."""
    found = labels.survey([info("shared", "Same"), info("other", "Other"), info("shared", "Same")])
    assert [d["label"] for d in found["duplicates"]] == ["shared"]


# ---------------------------------------------------------------- the whole way round

def test_a_written_label_is_read_back_as_that_frames_label(tmp_path):
    """What `label` writes is what `extract.frame_labels` looks for: beamer turns `label=x` into
    the destination `x` on the frame's first page and `x<n>` on step n. (The compiled half of this
    is proved live; here the two halves are held against each other.)"""
    from beamer2slides.extract import frame_labels
    src = source(tmp_path, frame("Moving labels"))
    (edit,) = labels.plan(src)
    dests = [(edit["label"], 0), (f"{edit['label']}<1>", 0), ("page.1", 0), ("Navigation1", 0)]
    assert frame_labels(dests) == {0: edit["label"]}


@pytest.mark.parametrize("title", ["Résumé", "数据分析", "C++ & you", "   ", "---"])
def test_any_title_produces_a_usable_label(title):
    name = labels.slug(title, set())
    assert name and labels.SAFE.sub("", name) == name and not name.startswith("-") and not name.endswith("-")
