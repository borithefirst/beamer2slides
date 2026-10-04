"""Test-run settings shared by every test file.

`fetcher`: a test's downloads go through the function it installs, by the same seam a harness uses
(`google_auth.use_fetcher`, `net`) rather than by patching a module's private helper.

And how the suite splits across `pytest -n N` workers. The default `--dist load`
hands out one test at a time, which is wrong for a file that builds its decks once in a
module-scoped fixture (`test_emit_requests`, `test_raster_images`, `test_sync_fuzz`, ...): spread,
its tests would build those decks again on every worker (`test_emit_requests`' 17 s, eight times
at -n 8). `--dist loadfile` would fix that but serialises files of independent tests
(`test_pure_pdf`'s 75 s) and puts all 53 decks of `test_invariants` on one worker.

So: the tests of a file that read a fixture it defines above function scope are one group (named
by the file), every other test goes out on its own, and a test's own `xdist_group` wins
(`test_invariants` groups per deck, `test_ir_matrix` per case). Run it with `-n N --dist loadgroup`;
without `-n` nothing here changes anything. The hook runs before xdist's, which writes each test's
group into its node id: after it, a group added here is never seen (it was not, until 2026-10-04,
so every test went out on its own). Shared state a fixture does not hold (an `lru_cache`) is per
worker; what several files read, made once per run, is in `built_decks` (the run's folder,
`pytest_configure`).

`needs_decks(*paths)`: a test that reads files under `tests/decks/` - built PDFs, a build script -
which a copy of the tests may leave out (Google's import does). It is skipped when one is missing,
and `-m "not needs_decks"` leaves the lot out.

`B2S_EMIT_STRICT`: emit makes an element it cannot plan the picture of its region, with a warning
(`emit.DeckPlan.contain`); in the suite that would hide the bug, so it raises. Set at import, before
a module-scoped fixture plans a deck; tests of the containment switch it off themselves.
"""

import contextlib
import functools
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from beamer2slides.net import Fetch

from . import built_decks

#: Not `.resolve()`d: under a runfiles tree that would leave the tree for a content store.
DECKS = Path(__file__).parent / "decks"

os.environ.setdefault("B2S_EMIT_STRICT", "1")


@pytest.fixture
def fetcher() -> Iterator[Callable[[Fetch], None]]:
    """`fetcher(fn)` installs `fn(url) -> bytes` for the rest of the test; installing another
    replaces it."""
    from beamer2slides import google_auth

    with contextlib.ExitStack() as stack:
        def install(fn: Fetch) -> None:
            stack.enter_context(google_auth.use_fetcher(fn))
        yield install


LIVE = {"slides", "sync", "inverse", "docs"}


@pytest.fixture(autouse=True)
def _drive_folder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The offline fakes of Drive model files, not folders: their `files.create` bodies are
    compared as they always were, so an offline test runs with `--drive-folder none`. The default,
    `auto`, is tested in `test_drive_folder.py`, and the live suites run with it."""
    if not LIVE & {m.name for m in request.node.iter_markers()}:
        monkeypatch.setenv("B2S_DRIVE_FOLDER", "none")


def pytest_configure(config: pytest.Config) -> None:
    """The run's folder for what its workers make once and share (`built_decks.SHARED`): made by
    the process that starts the workers, which inherit it, and removed when that process ends."""
    if not hasattr(config, "workerinput") and not os.environ.get(built_decks.SHARED):
        os.environ[built_decks.SHARED] = tempfile.mkdtemp(prefix="b2s-tests-")
        config.add_cleanup(functools.partial(_forget_shared, os.environ[built_decks.SHARED]))


def _forget_shared(folder: str) -> None:
    shutil.rmtree(folder, ignore_errors=True)
    if os.environ.get(built_decks.SHARED) == folder:
        del os.environ[built_decks.SHARED]


def shares_module_state(item: pytest.Item) -> bool:
    """Whether the test reads a fixture its own file makes once for several tests (module, class,
    package or session scope)."""
    if not isinstance(item, pytest.Function):
        return False
    file = item.nodeid.split("::")[0]  # (a fixture's baseid is the node id of where it is defined)
    return any(d.scope != "function" and d.baseid == file
               for defs in item._fixtureinfo.name2fixturedefs.values() for d in defs)


@pytest.hookimpl(tryfirst=True)  # before xdist's own, which names each test's group in its node id
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not any(m.name == "xdist_group" for m in item.iter_markers()) and shares_module_state(item):
            item.add_marker(pytest.mark.xdist_group(item.nodeid.split("::")[0]))
        for mark in item.iter_markers("needs_decks"):
            absent = [p for p in mark.args if not (DECKS / p).exists()]
            if absent:
                item.add_marker(pytest.mark.skip(reason=f"not in this copy of tests/decks: {', '.join(absent)}"))
