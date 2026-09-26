"""The replay's caches: a source compiled once, a source that failed fails at once."""

from pathlib import Path

from beamer2slides.devtools import adopt_replay


class Compiles:
    """A Workspace as far as `compiled` uses it."""

    def __init__(self, root: Path, ok: bool = True):
        self.build_dir = root / "build"
        self.build_dir.mkdir(parents=True)
        self.main = root / "src" / "main.tex"
        self.ok, self.calls = ok, 0

    def compile(self):
        self.calls += 1
        if not self.ok:
            return None, "main.tex:3: Undefined control sequence"
        (self.build_dir / "main.pdf").write_bytes(b"%PDF-1.5 fake")
        (self.build_dir / "main.synctex.gz").write_bytes(b"sync")
        return self.build_dir / "main.pdf", ""


def test_a_source_is_compiled_once(tmp_path):
    cache = tmp_path / "compiled" / "abc"
    ws = Compiles(tmp_path / "one")
    pdf, err, cached = adopt_replay.compiled(ws, cache)
    assert pdf and not cached and ws.calls == 1
    again = Compiles(tmp_path / "two")
    pdf, err, cached = adopt_replay.compiled(again, cache)
    assert cached and again.calls == 0
    assert pdf.read_bytes() == b"%PDF-1.5 fake" and (again.build_dir / "main.synctex.gz").exists()


def test_a_source_that_did_not_compile_fails_again_at_once(tmp_path):
    cache = tmp_path / "compiled" / "abc"
    assert adopt_replay.compiled(Compiles(tmp_path / "one", ok=False), cache)[0] is None
    again = Compiles(tmp_path / "two")
    pdf, err, cached = adopt_replay.compiled(again, cache)
    assert pdf is None and "Undefined control sequence" in err and cached and again.calls == 0


def test_the_hash_is_the_sources_bytes_and_whether_notes_are_shown(tmp_path):
    (tmp_path / "main.tex").write_text("a", encoding="utf-8")
    one = adopt_replay.tree_hash(tmp_path, False)
    assert adopt_replay.tree_hash(tmp_path, True) != one
    (tmp_path / "main.tex").write_text("b", encoding="utf-8")
    assert adopt_replay.tree_hash(tmp_path, False) != one


def test_old_compiles_are_pruned(tmp_path):
    import os
    for k in range(adopt_replay.KEEP + 2):
        (tmp_path / f"c{k}").mkdir()
        os.utime(tmp_path / f"c{k}", (k, k))
    adopt_replay.prune(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"c{k}" for k in range(2, adopt_replay.KEEP + 2)]
