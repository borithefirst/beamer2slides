"""The Google API client library, in one place - the only module that imports it.

Nothing here is about Google's APIs; it is about the *package* that speaks to them, and about
the fact that a caller may have its own. `google_auth.use_services` already lets one hand over
ready Slides/Drive/Docs clients (a harness whose builder answers from a bundled discovery
document, because `discovery.build()` re-parses one per call), and that injection was complete
except for the imports: `googleapiclient` was reached at module scope in six modules, so a
journey that talks to nobody could not even be **imported** without the package installed.
`deck_prepare`, which "needs no account at all", pulled it in through `snapshot` -> `emit` ->
`gslides`, which is a hard blocker for a sandbox that has no account and therefore no Google
libraries at all (reported 2026-09-22 by a caller running exactly that).

So the package is optional (`pip install beamer2slides[google]`), every use of it is behind a
function here, and the one piece that cannot be deferred - `HttpError`, which ~45 `except`
clauses name - is bound **once**:

    try:    from googleapiclient.errors import HttpError
    except: class HttpError(Exception): ...

in this module and nowhere else. Two independent fallbacks would bind two different classes,
and then an `except HttpError` somewhere would quietly stop matching the error another module
raises - a silence that would cost a rebuild guard or a retry. With the package absent nothing
can make an API call, so nothing raises the stand-in and every `except` clause simply never
matches, which is the right answer rather than an accident.

`status_of` / `message_of` / `is_transient` are here for the same reason one step further out:
they are what the call sites actually want of an error (a status to branch on, a sentence for a
person to read), and asking through a function instead of `e.resp.status` is what a client of
another shape could ever answer.
"""

from __future__ import annotations

import json
from typing import Any

#: What to say when the package is wanted and not there. Named in one place so the two ways out
#: - install it, or inject clients - are always both offered.
MISSING = ('the Google API client is not installed. Either `pip install "beamer2slides[google]"`, '
           'or hand this process your own clients with `beamer2slides.google_auth.use_services` '
           '(docs/install.md, "Injecting your own API clients").')

try:
    from googleapiclient.errors import HttpError
except ImportError:                    # no client library: nothing here can make a call, so
    class HttpError(Exception):        # nothing raises this and no `except HttpError` matches.
        """Stand-in for `googleapiclient.errors.HttpError` where the package is not installed.

        It exists so that `except HttpError` compiles and annotations evaluated at def time
        (`def api_message(e: HttpError)`) have a class to name. Nothing in the library raises it -
        with no client library there is no call to refuse - but a test harness that fabricates a
        refusal does (`devtools.agent_tasks`), so it carries Google's two fields and its
        constructor's shape, and `status_of` reads it as it reads the real one.
        """

        def __init__(self, resp: Any = None, content: bytes = b"", *args: Any) -> None:
            super().__init__(resp, content, *args)
            self.resp, self.content = resp, content


def installed() -> bool:
    """Whether the Google API client library is importable in this process."""
    try:
        import googleapiclient  # noqa: F401
    except ImportError:
        return False
    return True


def build(api: str, version: str, credentials: Any):
    """`googleapiclient.discovery.build`, imported now rather than at module import.

    `cache_discovery=False` because the cache wants a writable folder and says so loudly when it
    has none; a caller that minds the per-call discovery fetch injects its own client instead.
    """
    try:
        from googleapiclient.discovery import build as _build
    except ImportError:
        raise ModuleNotFoundError(MISSING) from None
    return _build(api, version, credentials=credentials, cache_discovery=False)


def media_upload(data: Any, mimetype: str, resumable: bool = False):
    """`MediaIoBaseUpload` over a file-like object, for a request's `media_body`."""
    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        raise ModuleNotFoundError(MISSING) from None
    return MediaIoBaseUpload(data, mimetype=mimetype, resumable=resumable)


def status_of(error: BaseException) -> int | None:
    """The HTTP status an API error carries, or None if it carries none."""
    status = getattr(getattr(error, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def message_of(error: BaseException, limit: int = 200) -> str:
    """What Google said, for a person to read: the API's own message, else the exception."""
    try:
        return json.loads(error.content)["error"]["message"][:limit]  # type: ignore[attr-defined]
    except (ValueError, KeyError, TypeError, AttributeError):
        return str(error)[:limit]


def is_transient(error: BaseException) -> bool:
    """Whether the call is worth making again: a rate limit or a server that stumbled."""
    return status_of(error) in (429, 500, 502, 503)
