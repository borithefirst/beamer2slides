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

`build` is where a client the library made becomes a typed one: the library builds a `Resource`
whose methods exist only at runtime (from the discovery document), so `build` checks that it has
the methods `google_types` says the package calls (`isinstance` against the runtime-checkable
Protocol) and returns it as that Protocol. A client a caller injected is taken at its word
(`google_auth.use_services`).
"""

from __future__ import annotations

import importlib
import json
from typing import IO, TYPE_CHECKING, Literal, Protocol, Union, overload, runtime_checkable

from .google_types import DocsService, DriveService, MediaBody, SlidesService
from .json_types import Json

if TYPE_CHECKING:
    from google.auth.credentials import Credentials
    from typing_extensions import TypeAlias

#: Every API the package calls, by the name `build` takes.
ApiName: TypeAlias = Literal["slides", "drive", "docs"]
Service: TypeAlias = Union[SlidesService, DriveService, DocsService]

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

        def __init__(self, resp: object, content: bytes, *args: object) -> None:
            super().__init__(resp, content, *args)
            self.resp, self.content = resp, content


def installed() -> bool:
    """Whether the Google API client library is importable in this process."""
    try:
        import googleapiclient  # noqa: F401
    except ImportError:
        return False
    return True


@overload
def build(api: Literal["slides"], version: Literal["v1"], credentials: Credentials) -> SlidesService: ...
@overload
def build(api: Literal["drive"], version: Literal["v3"], credentials: Credentials) -> DriveService: ...
@overload
def build(api: Literal["docs"], version: Literal["v1"], credentials: Credentials) -> DocsService: ...
def build(api: ApiName, version: str, credentials: Credentials) -> Service:
    """`googleapiclient.discovery.build`, imported now rather than at module import, as the
    Protocol of its api (`google_types`); a client lacking a method the package calls is refused
    here rather than failing at that call.

    `cache_discovery=False` because the cache wants a writable folder and says so loudly when it
    has none; a caller that minds the per-call discovery fetch injects its own client instead.
    """
    try:
        from googleapiclient.discovery import build as _build
    except ImportError:
        raise ModuleNotFoundError(MISSING) from None
    client: object = _build(api, version, credentials=credentials, cache_discovery=False)
    if api == "slides" and isinstance(client, SlidesService):
        return client
    if api == "drive" and isinstance(client, DriveService):
        return client
    if api == "docs" and isinstance(client, DocsService):
        return client
    raise TypeError(f"the {api} {version} client lacks a method google_types.py says the package calls")


def lent_credentials(client: object) -> Credentials | None:
    """The credentials a client the library built carries on its connection, for a worker thread
    to build its own with (`deck_export`), or None: a client a caller injected, or none at all."""
    creds = getattr(getattr(client, "_http", None), "credentials", None)
    if creds is None:
        return None
    try:
        from google.auth.credentials import Credentials
    except ImportError:
        return None
    return creds if isinstance(creds, Credentials) else None


def patient_http(request: object, seconds: float) -> object | None:
    """An authorised connection that waits `seconds` for an answer, for one call known to be slow,
    or None when the request is not the library's own (an injected client: it runs as it is).
    The library waits 60 s; Drive's .pptx export of a deck set in Noto Sans SC took 115 s and TC
    77 s, since Drive embeds the font (6.7 MB for TC) - measured 2026-09-24."""
    creds = getattr(getattr(request, "http", None), "credentials", None)
    if creds is None:
        return None
    try:
        import google_auth_httplib2
        httplib2: object = importlib.import_module("httplib2")   # a module would pass for any Protocol
    except ImportError:
        return None
    if not isinstance(httplib2, Httplib2):
        return None
    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=seconds))


@runtime_checkable
class Httplib2(Protocol):
    """What `patient_http` calls of `httplib2`, which ships no types (it is imported by name, so no
    import statement asks the checker for them): a connection that waits `timeout` seconds."""

    def Http(self, *, timeout: float) -> object: ...


def media_upload(data: IO[bytes], mimetype: str) -> MediaBody:
    """`MediaIoBaseUpload` over a file-like object, for a request's `media_body`."""
    try:
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        raise ModuleNotFoundError(MISSING) from None
    return MediaIoBaseUpload(data, mimetype=mimetype, resumable=False)


def status_of(error: BaseException) -> int | None:
    """The HTTP status an API error carries, or None if it carries none."""
    status = getattr(getattr(error, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def message_of(error: BaseException) -> str:
    """What Google said, for a person to read (its first 200 characters): `message_within`."""
    return message_within(error, 200)


def message_within(error: BaseException, limit: int) -> str:
    """What Google said, cut at `limit` characters: the API's own message, else the exception."""
    content = getattr(error, "content", None)
    if isinstance(content, (bytes, str)):
        try:
            said: Json = json.loads(content)
        except ValueError:
            said = None
        inner = said.get("error") if isinstance(said, dict) else None
        message = inner.get("message") if isinstance(inner, dict) else None
        if isinstance(message, str):
            return message[:limit]
    return str(error)[:limit]


def is_transient(error: BaseException) -> bool:
    """Whether the call is worth making again: a rate limit or a server that stumbled."""
    return status_of(error) in (429, 500, 502, 503)
