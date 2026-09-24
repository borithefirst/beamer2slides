"""Test-run settings shared by every test file.

`fetcher`: a test's downloads go through the function it installs, by the same seam a harness uses
(`google_auth.use_fetcher`, `net`) rather than by patching a module's private helper.

And how the suite splits across `pytest -n N` workers. The default `--dist load`
hands out one test at a time, which is wrong here - several files build their decks once in a
module-scoped fixture (`test_emit_requests`, `test_raster_images`, `test_sync_fuzz`, ...), and
spreading their tests would build those decks again on every worker. `--dist loadfile` would fix
that but puts all 53 decks of `test_invariants` on one worker, which is most of the suite's time
(each deck is extracted, classified and rendered once, ~0.5 s, and its five checks share that).

So: a file is a group unless a test says otherwise, and `test_invariants` says otherwise per deck.
Run it with `-n auto --dist loadgroup`; without `-n` nothing here changes anything.

`needs_decks(*paths)`: a test that reads files under `tests/decks/` - built PDFs, a build script -
which a copy of the tests may leave out (Google's import does). It is skipped when one is missing,
and `-m "not needs_decks"` leaves the lot out.
"""

import contextlib
from pathlib import Path

import pytest

#: Not `.resolve()`d: under a runfiles tree that would leave the tree for a content store.
DECKS = Path(__file__).parent / "decks"


@pytest.fixture
def fetcher():
    """`fetcher(fn)` installs `fn(url) -> bytes` for the rest of the test; installing another
    replaces it."""
    from beamer2slides import google_auth

    with contextlib.ExitStack() as stack:
        yield lambda fn: stack.enter_context(google_auth.use_fetcher(fn))


LIVE = {"slides", "sync", "inverse", "docs"}


@pytest.fixture(autouse=True)
def _drive_folder(request, monkeypatch):
    """The offline fakes of Drive model files, not folders: their `files.create` bodies are
    compared as they always were, so an offline test runs with `--drive-folder none`. The default,
    `auto`, is tested in `test_drive_folder.py`, and the live suites run with it."""
    if not LIVE & {m.name for m in request.node.iter_markers()}:
        monkeypatch.setenv("B2S_DRIVE_FOLDER", "none")


def pytest_collection_modifyitems(items):
    for item in items:
        if not any(m.name == "xdist_group" for m in item.iter_markers()):
            item.add_marker(pytest.mark.xdist_group(item.nodeid.split("::")[0]))
        for mark in item.iter_markers("needs_decks"):
            absent = [p for p in mark.args if not (DECKS / p).exists()]
            if absent:
                item.add_marker(pytest.mark.skip(reason=f"not in this copy of tests/decks: {', '.join(absent)}"))
