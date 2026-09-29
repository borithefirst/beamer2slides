"""The replay's caches: a source compiled once, a source that failed fails at once."""

from pathlib import Path

from beamer2slides.devtools import adopt_replay


class Compiles:
    """A Workspace as far as `compiled` uses it (`adopt_replay.Compiling`)."""

    def __init__(self, root: Path, ok: bool) -> None:
        self.build_dir = root / "build"
        self.build_dir.mkdir(parents=True)
        self.main = root / "src" / "main.tex"
        self.ok, self.calls = ok, 0

    def compile(self) -> tuple[Path | None, str]:
        self.calls += 1
        if not self.ok:
            return None, "main.tex:3: Undefined control sequence"
        (self.build_dir / "main.pdf").write_bytes(b"%PDF-1.5 fake")
        (self.build_dir / "main.synctex.gz").write_bytes(b"sync")
        return self.build_dir / "main.pdf", ""


def test_a_source_is_compiled_once(tmp_path: Path) -> None:
    cache = tmp_path / "compiled" / "abc"
    ws = Compiles(tmp_path / "one", True)
    pdf, err, cached = adopt_replay.compiled(ws, cache)
    assert pdf and not cached and ws.calls == 1
    again = Compiles(tmp_path / "two", True)
    pdf, err, cached = adopt_replay.compiled(again, cache)
    assert cached and again.calls == 0
    assert pdf is not None and pdf.read_bytes() == b"%PDF-1.5 fake" and (again.build_dir / "main.synctex.gz").exists()


def test_a_source_that_did_not_compile_fails_again_at_once(tmp_path: Path) -> None:
    cache = tmp_path / "compiled" / "abc"
    assert adopt_replay.compiled(Compiles(tmp_path / "one", False), cache)[0] is None
    again = Compiles(tmp_path / "two", True)
    pdf, err, cached = adopt_replay.compiled(again, cache)
    assert pdf is None and "Undefined control sequence" in err and cached and again.calls == 0


def test_a_saved_run_reads_back_what_the_report_compares() -> None:
    """`--against` reads a saved run: a deck read back gives its numbers, a failed one None."""
    got = adopt_replay.saved_round0({"deck": "d:2", "n": 1, "notes": False, "cached": True, "pages": 1, "ink": 0.98,
                                     "open": 5, "suspect": 3, "kinds": {"text": 5}, "suspect_kinds": {"text": 3},
                                     "slides": [{"slide": 1, "ink": 0.98, "open": 5}],
                                     "seconds": {"target": 0.5, "compile": 1.0}})
    assert got is not None and (got.open, got.suspect, got.cached, got.seconds) == (5, 3, True, 1.5)
    assert adopt_replay.saved_round0({"deck": "d", "error": "compile: x", "seconds": {}}) is None
    line = adopt_replay.line({"deck": "d", "error": "compile: x\nmore", "seconds": {}}, got)
    assert "ERROR compile: x" in line and "more" not in line


def test_the_hash_is_the_sources_bytes_and_whether_notes_are_shown(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("a", encoding="utf-8")
    one = adopt_replay.tree_hash(tmp_path, False)
    assert adopt_replay.tree_hash(tmp_path, True) != one
    (tmp_path / "main.tex").write_text("b", encoding="utf-8")
    assert adopt_replay.tree_hash(tmp_path, False) != one


def test_the_micro_corpus_names_only_manifest_decks_once_each() -> None:
    import json
    from beamer2slides.devtools import adopt_bench
    from beamer2slides.json_types import as_objects
    public = {d["name"] for d in as_objects(json.loads(adopt_bench.MANIFEST.read_text(encoding="utf-8")), "manifest")}
    assert len(set(adopt_bench.MICRO)) == len(adopt_bench.MICRO)
    for spec in adopt_bench.MICRO:
        name, _, slides = spec.partition(":")
        assert name in public, spec                 # never a deck someone sent us privately
        assert slides.replace("-", "").isdigit(), spec


def test_old_compiles_are_pruned(tmp_path: Path) -> None:
    import os
    for k in range(adopt_replay.KEEP + 2):
        (tmp_path / f"c{k}").mkdir()
        os.utime(tmp_path / f"c{k}", (k, k))
    adopt_replay.prune(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"c{k}" for k in range(2, adopt_replay.KEEP + 2)]
