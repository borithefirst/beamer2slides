"""Test-run settings shared by every test file.

Only one thing so far: how the suite splits across `pytest -n N` workers. The default `--dist load`
hands out one test at a time, which is wrong here - several files build their decks once in a
module-scoped fixture (`test_emit_requests`, `test_raster_images`, `test_sync_fuzz`, ...), and
spreading their tests would build those decks again on every worker. `--dist loadfile` would fix
that but puts all 53 decks of `test_invariants` on one worker, which is most of the suite's time
(each deck is extracted, classified and rendered once, ~0.5 s, and its five checks share that).

So: a file is a group unless a test says otherwise, and `test_invariants` says otherwise per deck.
Run it with `-n auto --dist loadgroup`; without `-n` nothing here changes anything.
"""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if not any(m.name == "xdist_group" for m in item.iter_markers()):
            item.add_marker(pytest.mark.xdist_group(item.nodeid.split("::")[0]))
