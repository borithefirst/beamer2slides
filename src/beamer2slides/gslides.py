"""Small helpers around the Google Slides API."""

import random
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path

from .gapi import HttpError, is_transient, status_of

EMU_PER_PT = 12700


def per_thread(make):
    """A client per thread, built on first use. A googleapiclient service object carries one
    connection and is not thread-safe, so every thread that talks to Google builds its own from
    credentials resolved on the calling thread (`credentials()` itself must not be called on a
    thread that inherited no context: google_auth's ContextVar). A client a caller handed over
    through `google_auth.use_services` is that caller's own and comes back for every thread -
    handing one over says it may be called from several threads at once."""
    local = threading.local()

    def client():
        if not hasattr(local, "client"):
            local.client = make()
        return local.client
    return client


def pt(v: float) -> dict:
    return {"magnitude": v, "unit": "PT"}


def emu(v_pt: float) -> dict:
    return {"magnitude": round(v_pt * EMU_PER_PT), "unit": "EMU"}


# What `execute` did, for a harness to read (devtools/fuzz_sync.py): calls (attempts), retries, the
# seconds slept backing off, how many retries were rate limits (429), and `call <methodId>` per
# method. Observation only - nothing reads it to decide anything. `STATS` is the process's;
# `count_thread()` hands a thread a Counter of its own that its calls add to as well, for a harness
# running several jobs in threads.
STATS: Counter = Counter()
_STATS_LOCK = threading.Lock()
_THREAD = threading.local()


def count_thread() -> Counter:
    """A fresh Counter that this thread's `execute` calls add to from now on."""
    _THREAD.stats = Counter()
    return _THREAD.stats


def _count(by: dict) -> None:
    mine = getattr(_THREAD, "stats", None)
    with _STATS_LOCK:
        STATS.update(by)
        if mine is not None:
            mine.update(by)


def execute(request, retries: int = 6):
    """Run an API request, backing off on rate limits and transient server errors."""
    for attempt in range(retries):
        _count({"calls": 1, f"call {getattr(request, 'methodId', None) or '?'}": 1})
        try:
            return request.execute()
        except HttpError as e:
            if not is_transient(e) or attempt == retries - 1:
                raise
            pause = min(60, 2 ** attempt * 2) + random.random()
            _count({"retries": 1, "backoff_s": pause, "rate_limited": int(status_of(e) == 429)})
            time.sleep(pause)
        except OSError:  # SSL EOFs and connection resets happen now and then
            if attempt == retries - 1:
                raise
            pause = 2 ** attempt + random.random()
            _count({"retries": 1, "backoff_s": pause})
            time.sleep(pause)


def text_box(object_id: str, page_id: str, x: float, y: float, w: float, h: float) -> dict:
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


def save_thumbnail(slides, presentation_id: str, page_id: str, path: Path) -> tuple[int, int]:
    """Export one slide as a LARGE (1600 px wide) PNG rendered by Google."""
    thumb = execute(slides.presentations().pages().getThumbnail(
        presentationId=presentation_id, pageObjectId=page_id,
        thumbnailProperties_mimeType="PNG", thumbnailProperties_thumbnailSize="LARGE",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    for attempt in range(5):
        try:
            urllib.request.urlretrieve(thumb["contentUrl"], tmp)
            break
        except OSError:  # URLError and SSL errors are OSErrors
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    tmp.replace(path)  # never leave a truncated PNG under the final name
    return thumb["width"], thumb["height"]
