"""Small helpers around the Google Slides API."""

from __future__ import annotations

import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

from .gapi import HttpError, is_transient, patient_http, status_of
from .google_types import Request, SlidesService
from .json_types import JsonObject

if TYPE_CHECKING:
    from .net import Fetch

__all__ = ["EMU_PER_PT", "SLOW_EXPORT", "STATS", "HttpError", "count_thread", "emu", "execute",
           "execute_with", "per_thread", "pt", "save_thumbnail", "status_of", "text_box"]

EMU_PER_PT = 12700

T = TypeVar("T")


class _PerThread(threading.local, Generic[T]):
    """One thread's client, once it has been built."""

    client: T | None = None


def per_thread(make: Callable[[], T]) -> Callable[[], T]:
    """A client per thread, built on first use. A googleapiclient service object carries one
    connection and is not thread-safe, so every thread that talks to Google builds its own from
    credentials resolved on the calling thread (`credentials()` itself must not be called on a
    thread that inherited no context: google_auth's ContextVar). A client a caller handed over
    through `google_auth.use_services` is that caller's own and comes back for every thread -
    handing one over says it may be called from several threads at once."""
    local: _PerThread[T] = _PerThread()

    def client() -> T:
        made = local.client
        if made is None:
            made = local.client = make()
        return made
    return client


def pt(v: float) -> JsonObject:
    return {"magnitude": v, "unit": "PT"}


def emu(v_pt: float) -> JsonObject:
    return {"magnitude": round(v_pt * EMU_PER_PT), "unit": "EMU"}


# What `execute` did, for a harness to read (devtools/fuzz_sync.py): calls (attempts), retries, the
# seconds slept backing off (each pause rounded to a whole second: a Counter counts in integers;
# every pause is 1 s or more), how many retries were rate limits (429), and `call <methodId>` per
# method. Observation only - nothing reads it to decide anything. `STATS` is the process's;
# `count_thread()` hands a thread a Counter of its own that its calls add to as well, for a harness
# running several jobs in threads.
STATS: Counter[str] = Counter()
_STATS_LOCK = threading.Lock()


class _ThreadStats(threading.local):
    """The Counter `count_thread` gave this thread, if it asked for one."""

    stats: Counter[str] | None = None


_THREAD = _ThreadStats()


def count_thread() -> Counter[str]:
    """A fresh Counter that this thread's `execute` calls add to from now on."""
    stats: Counter[str] = Counter()
    _THREAD.stats = stats
    return stats


def _count(by: dict[str, int]) -> None:
    mine = _THREAD.stats
    with _STATS_LOCK:
        STATS.update(by)
        if mine is not None:
            mine.update(by)


SLOW_EXPORT = 300.0   # seconds a .pptx export may take (gapi.patient_http: 115 s measured)
RETRIES = 6           # tries `execute` gives one call


def execute(request: Request[T]) -> T:
    """Run an API request, backing off on rate limits and transient server errors: its answer,
    typed by the method that made the request (`google_types`)."""
    return execute_with(request, retries=RETRIES, timeout=None)


def execute_with(request: Request[T], *, retries: int, timeout: float | None) -> T:
    """`execute` with `retries` tries (at least one), and `timeout`: the seconds this one call may
    wait for its answer, where the library's 60 s is too short (`SLOW_EXPORT`). Returns the answer
    or raises what the last try raised - never falls through."""
    http = patient_http(request, timeout) if timeout else None
    method = getattr(request, "methodId", None) or "?"
    attempt = 0
    while True:
        _count({"calls": 1, f"call {method}": 1})
        last = attempt >= retries - 1
        try:
            return request.execute(http=http) if http is not None else request.execute()
        except HttpError as e:
            if not is_transient(e) or last:
                raise
            pause = min(60, 2 ** attempt * 2) + random.random()
            _count({"retries": 1, "backoff_s": round(pause), "rate_limited": int(status_of(e) == 429),
                    f"retry {method} {status_of(e)}": 1})
        except OSError:  # SSL EOFs and connection resets happen now and then
            if last:
                raise
            pause = 2 ** attempt + random.random()
            _count({"retries": 1, "backoff_s": round(pause)})
        time.sleep(pause)
        attempt += 1


def text_box(object_id: str, page_id: str, x: float, y: float, w: float, h: float) -> dict[str, object]:
    return {"createShape": {
        "objectId": object_id,
        "shapeType": "TEXT_BOX",
        "elementProperties": {
            "pageObjectId": page_id,
            "size": {"width": emu(w), "height": emu(h)},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x * EMU_PER_PT,
                          "translateY": y * EMU_PER_PT, "unit": "EMU"},
        },
    }}


def save_thumbnail(slides: SlidesService, presentation_id: str, page_id: str, path: Path,
                   fetch: Fetch | None = None) -> tuple[int, int]:
    """Export one slide as a LARGE (1600 px wide) PNG rendered by Google. `fetch`: what downloads
    it (`net`); pass it on a worker thread, which inherits no context."""
    from . import net
    thumb = execute(slides.presentations().pages().getThumbnail(
        presentationId=presentation_id, pageObjectId=page_id,
        thumbnailProperties_mimeType="PNG", thumbnailProperties_thumbnailSize="LARGE",
    ))
    url, width, height = thumb.get("contentUrl"), thumb.get("width"), thumb.get("height")
    if url is None or width is None or height is None:
        raise ValueError(f"getThumbnail answered no picture for slide {page_id}: {sorted(thumb)}")
    data = net.download(url, fetch, tries=5)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # never leave a truncated PNG under the final name
    return width, height
