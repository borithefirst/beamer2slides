"""Every download the library makes of content it did not write: one door, and a caller may own it.

A deck's pictures hang off `contentUrl`s, a slide's thumbnail off another, a Doc's inserted
picture off a `contentUri`, and a picture inserted by URL keeps the address it came from - a host
chosen by whoever inserted it, not by Google. Recording the sync base after a conversion downloads
every picture of the new deck (`snapshot.picture_signatures`), so even a plain `convert` is
egress from a credentialed process. A harness that must send outbound HTTP through its own
reviewed client - its timeouts, retries and egress policy - installs a `Fetch` with
`google_auth.use_fetcher`, and every download below goes through it. With none installed it is
`urllib_fetch`, which is what each site did before, so the CLI is unchanged.

A `Fetch` is `(url) -> bytes` and says no by raising - anything at all; a harness's client raises
its own types. What each site does with a failure is the site's own business and has not moved:
a picture that cannot be downloaded stays unsigned and sync compares its `contentHash` alone
(`snapshot._download`), a thumbnail that cannot be read keeps the predicted place
(`emit.measure_places`). Every failure is retried, except `PermissionError`, which is how a
fetcher says a URL is *not allowed*: asking again cannot change that answer, and a refusing
fetcher should not cost a pool of eight workers three sleeps each.

Not the same door as `agent.context.AgentContext.fetch`, which fetches a URL a *model* supplied
as an argument and which a careful harness leaves unset. This one only ever fetches what Google
answered with, or what the deck itself names.

Fonts are not in here: `fontfetch` has its own off switch (`B2S_FONT_FETCH=0`).
"""

from __future__ import annotations

import time
import urllib.request
from typing import Callable

Fetch = Callable[[str], bytes]

TIMEOUT = 60  # s, per try


def urllib_fetch(url: str) -> bytes:
    """What the library fetches with when nobody installed a fetcher."""
    with urllib.request.urlopen(url, timeout=TIMEOUT) as reply:
        return reply.read()


def download(url: str, fetch: Fetch | None = None, tries: int = 3) -> bytes:
    """`fetch(url)`, tried up to `tries` times with a growing pause (1, 2, 4... s); raises what
    the last try raised. `fetch` None: the fetcher installed for this context
    (`google_auth.fetcher_for_threads`) - resolve it on the calling thread and pass it down
    wherever this runs on a worker, which inherits no context."""
    if fetch is None:
        from .google_auth import fetcher_for_threads
        fetch = fetcher_for_threads()
    for attempt in range(max(1, tries) - 1):
        try:
            data = fetch(url)
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001 - a harness's fetcher raises its own types
            time.sleep(2 ** attempt)
            continue
        return _bytes(data)
    return _bytes(fetch(url))


def _bytes(data) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"a fetcher returned {type(data).__name__}, not bytes")


# The first bytes of the picture formats a deck or a Doc can hold, for a download that arrives
# without a Content-Type: a `Fetch` hands over the bytes and nothing else.
_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"),
               (b"GIF89a", ".gif"), (b"BM", ".bmp"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"))


def picture_suffix(data: bytes, default: str = ".png") -> str:
    """The file suffix a picture's own bytes say it should have."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for magic, suffix in _SIGNATURES:
        if data.startswith(magic):
            return suffix
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return ".svg"
    return default
