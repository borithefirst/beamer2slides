"""`pull --apply` writes the author's own files: what it replaces has to stay reachable.

The first apply used to copy `talk.tex` to `talk.tex.bak`; a second apply wrote over that copy,
and the version the author had written was gone. Backups are now numbered, pictures get one too,
and a file that already holds what pull wants is left alone. No Google calls, no LaTeX.
"""

import types

import pytest

from beamer2slides import inverse


def result_for(files: dict, tmp_path):
    """The little of a `Result` that write_outputs touches."""
    return types.SimpleNamespace(files=files, patch="", work=tmp_path / "work-src", converged=True,
                                 unresolved=[], iterations=[{}, {}])


@pytest.fixture
def apply_to(tmp_path, monkeypatch):
    monkeypatch.setattr(inverse, "report", lambda result, target: ({}, "# report"))
    work = tmp_path / "pull"
    work.mkdir()

    def run(files):
        inverse.write_outputs(result_for(files, tmp_path), {}, tmp_path / "talk.tex", work,
                              apply=True, out=None, log=lambda *a: None)
    return run


# ---------------------------------------------------------------- names


def test_the_first_backup_is_plain_bak(tmp_path):
    tex = tmp_path / "talk.tex"
    tex.write_text("one", encoding="utf-8")
    assert inverse.backup_for(tex).name == "talk.tex.bak"


def test_later_backups_are_numbered(tmp_path):
    tex = tmp_path / "talk.tex"
    tex.write_text("one", encoding="utf-8")
    (tmp_path / "talk.tex.bak").write_text("older", encoding="utf-8")
    assert inverse.backup_for(tex).name == "talk.tex.bak2"
    (tmp_path / "talk.tex.bak2").write_text("older still", encoding="utf-8")
    assert inverse.backup_for(tex).name == "talk.tex.bak3"


# ---------------------------------------------------------------- applying


def test_the_authors_version_survives_two_applies(apply_to, tmp_path):
    tex = tmp_path / "talk.tex"
    tex.write_text("the author's own text\n", encoding="utf-8")

    apply_to({tex: "after the first pull\n"})
    apply_to({tex: "after the second pull\n"})

    assert tex.read_text(encoding="utf-8") == "after the second pull\n"
    assert (tmp_path / "talk.tex.bak").read_text(encoding="utf-8") == "the author's own text\n"
    assert (tmp_path / "talk.tex.bak2").read_text(encoding="utf-8") == "after the first pull\n"


def test_a_file_that_already_says_it_is_not_copied_aside(apply_to, tmp_path):
    """Byte for byte, line endings included: pull writes its text untranslated (newline="")."""
    tex = tmp_path / "talk.tex"
    tex.write_text("already what pull wants\n", encoding="utf-8", newline="")
    apply_to({tex: "already what pull wants\n"})
    assert not (tmp_path / "talk.tex.bak").exists()


def test_a_picture_it_replaces_is_kept_too(apply_to, tmp_path):
    """Figure names carry a content hash, but a name can still be taken by another file."""
    figure = tmp_path / "figures" / "plot-1234abcd.png"
    figure.parent.mkdir()
    figure.write_bytes(b"the author's plot")
    fresh = tmp_path / "from-the-deck.png"
    fresh.write_bytes(b"the deck's plot")

    apply_to({figure: fresh})

    assert figure.read_bytes() == b"the deck's plot"
    assert (tmp_path / "figures" / "plot-1234abcd.png.bak").read_bytes() == b"the author's plot"


def test_out_dir_keeps_files_that_were_already_there(tmp_path):
    """`--out` is meant for a fresh folder, but pointing it at a tree of one's own must not
    silently write over it."""
    src, dst = tmp_path / "work-src", tmp_path / "elsewhere"
    (src / "figures").mkdir(parents=True)
    (src / "talk.tex").write_text("pulled\n", encoding="utf-8", newline="")
    (src / "figures" / "plot.png").write_bytes(b"pulled picture")
    (dst / "figures").mkdir(parents=True)
    (dst / "talk.tex").write_text("someone else's talk\n", encoding="utf-8", newline="")
    (dst / "figures" / "plot.png").write_bytes(b"someone else's picture")

    kept = inverse.copy_tree(src, dst)

    assert {p.name for p in kept} == {"talk.tex.bak", "plot.png.bak"}
    assert (dst / "talk.tex").read_text(encoding="utf-8") == "pulled\n"
    assert (dst / "talk.tex.bak").read_text(encoding="utf-8") == "someone else's talk\n"
    assert (dst / "figures" / "plot.png.bak").read_bytes() == b"someone else's picture"


def test_out_dir_that_is_empty_makes_no_backups(tmp_path):
    src, dst = tmp_path / "work-src", tmp_path / "fresh"
    src.mkdir()
    (src / "talk.tex").write_text("pulled\n", encoding="utf-8", newline="")
    assert inverse.copy_tree(src, dst) == []
    assert not list(dst.glob("**/*.bak*"))


def test_a_new_file_needs_no_backup(apply_to, tmp_path):
    new = tmp_path / "figures" / "new-00000000.png"
    src = tmp_path / "src.png"
    src.write_bytes(b"picture")
    apply_to({new: src})
    assert new.read_bytes() == b"picture"
    assert not list(tmp_path.glob("**/*.bak*"))
