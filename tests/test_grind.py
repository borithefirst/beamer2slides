"""The adopt grind's round (devtools/grind.py)."""

from pathlib import Path

from beamer2slides.devtools import grind


def test_each_corpus_benches_only_its_own_decks(tmp_path: Path) -> None:
    """A round over two corpora gave every name to both, and metrics stopped on the first name one
    of them had no folder for; a deck with a folder in both is benched where its recordings are."""
    corpus, hunt = tmp_path / "adopt-corpus", tmp_path / "adopt-hunt"
    for folder in (corpus / "instagram" / "deck-files", hunt / "yc" / "deck-files",
                   corpus / "sc-dark", hunt / "sc-dark" / "deck-files", hunt / "unrecorded"):
        folder.mkdir(parents=True)
    got = grind.corpus_decks([corpus, hunt], ["instagram", "yc", "sc-dark", "unrecorded"])
    assert got == {corpus: ["instagram"], hunt: ["yc", "sc-dark", "unrecorded"]}
    assert grind.corpus_decks([corpus, hunt], []) == {corpus: [], hunt: []}
