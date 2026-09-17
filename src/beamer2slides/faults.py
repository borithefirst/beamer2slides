"""Fault injection for the crash tests (`tests/test_sync_crash.py`, docs/sync.md).

Nothing here does anything unless the environment variable `B2S_FAIL_AT` is set: `fail_at` is one
`os.environ.get` and a return. It exists so a test can kill a sync or a `pull --apply` exactly
between two writes and check that the next run still reaches the right deck without losing a
person's edit.

    B2S_FAIL_AT=content         raise at the first `content` point
    B2S_FAIL_AT=content:2       raise the second time that point is reached
    B2S_FAIL_AT=stage,base:save several points at once (`base:save` keeps its colon: only a
                                trailing all-digit part is read as the occurrence)
    B2S_FAIL_AT=!content        leave the process at once (os._exit), so no `finally`, no cleanup
                                and no report: what a kill -9 or a lost laptop does

Points, in the order a sync reaches them:
    plan        planned, nothing written yet (the staging deck exists)
    journal     the pending marker is stored, still nothing written
    measure     the scratch slides for hole measurement have been written
    content     after each batch of content requests
    order       after each batch of the z-order phase
    overrides   after each batch that re-applies the deck's own edits
    base:save   everything written, the new base not yet stored
    base:drive  the new base is stored locally, not yet in Drive
    cleanup     after each batch of the final phase (the old objects' deletion)
`pull --apply` has `pull:apply` (raised before each file is replaced).

`B2S_BATCH_SIZE` (also only read when set) lowers the write batch size, so a phase takes several
batches and a kill in it lands between two writes.
"""

import os
import threading

ENV = "B2S_FAIL_AT"
SIZE = "B2S_BATCH_SIZE"  # the write batch size, so a test can force several batches per phase

_counts: dict[str, int] = {}
_lock = threading.Lock()


class InjectedFailure(RuntimeError):
    """Raised at a point named by B2S_FAIL_AT. Never raised in normal use."""


def _wanted(spec: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        head, _, tail = item.rpartition(":")
        if head and tail.isdigit():
            out[head] = int(tail)
        else:
            out[item] = 1
    return out


def fail_at(point: str) -> None:
    """Die here when `B2S_FAIL_AT` names this point (see the module docstring); else do nothing."""
    spec = os.environ.get(ENV)
    if not spec:
        return
    wanted = _wanted(spec)
    hard = point in {p[1:] for p in wanted if p.startswith("!")}
    want = wanted.get(point, wanted.get(f"!{point}"))
    if want is None:
        return
    with _lock:
        _counts[point] = seen = _counts.get(point, 0) + 1
    if seen != want:
        return
    message = f"{ENV}={spec}: dying at {point} (occurrence {seen})"
    if hard:
        print(message, flush=True)
        os._exit(70)
    raise InjectedFailure(message)


def batch_size(default: int) -> int:
    """`default`, unless B2S_BATCH_SIZE says otherwise: the crash tests make the write phases take
    several batches, so a kill can land between two of them."""
    try:
        return max(1, int(os.environ.get(SIZE) or default))
    except ValueError:
        return default


def reset() -> None:
    """Forget how often each point was reached (tests that arm the hook more than once)."""
    with _lock:
        _counts.clear()
