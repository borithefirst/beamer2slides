"""Small helpers around the Google Slides API."""

import random
import threading
import time
import urllib.request
from pathlib import Path

from googleapiclient.errors import HttpError

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


def execute(request, retries: int = 6):
    """Run an API request, backing off on rate limits and transient server errors."""
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status not in (429, 500, 502, 503) or attempt == retries - 1:
                raise
            time.sleep(min(60, 2 ** attempt * 2) + random.random())
        except OSError:  # SSL EOFs and connection resets happen now and then
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt + random.random())


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
