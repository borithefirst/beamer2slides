"""A deck as .pptx in parts, where Drive will not export it whole.

Drive's `files.export` refuses a file past its size limit (about 10 MB: a 403 whose reason is
`exportSizeLimitExceeded`, "This file is too large to be exported"), and a deck set in a big
embedded face can take longer than any connection waits (`gslides.SLOW_EXPORT`). Either way the
whole export is gone, and with it every picture `deck_pictures.LivePictures` would have read out
of it and the .pptx backup a forced rebuild demands (`guard.backup_deck`).

A part of the deck is a Drive copy of it with the slides outside the part deleted, exported and
then deleted: our own temporary file, like sync's staging deck, tagged `b2sStaging` so that
`tools/drive_usage.py` recognises one a killed run left behind. `files.copy` keeps every objectId
of a Slides file - pages, masters, layouts and page elements, measured 2026-09-28 on a 7-slide
converted deck (19 pages) - and deleting slides leaves the masters and layouts alone (1 and 11
before and after, same ids, and the part's export still carries all 11). So the slides to delete
are named by the original's ids with no read of the copy, and a part's export pairs with the
original's `presentations.get` cut to the part's slides (`Export.pictures`); a copy that ever did
renumber is read once and its slides deleted by their place instead (`keep_only`).

Parts split adaptively: a part Drive still refuses as too large is halved, down to one slide; a
slide that is too large alone is reported (`Export.missing`), never guessed. A refusal about
anything else - `insufficientFilePermissions`, a 404 - is not a size and makes no copy at all: a
copy of a file one may not export is the wrong thing to make. Up to `WORKERS` parts are in the air
at once (a part is a copy, a batchUpdate, an export and a delete; four writes of Drive's and
Slides' per-user quota), each on a thread with clients of its own; where a caller lent its own
clients (`google_auth.shared_service`) they run one after the other on the calling thread.
"""

from __future__ import annotations

import io
import json
import random
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TypedDict, Union

from .gapi import HttpError, is_transient, lent_credentials, message_of, message_within, status_of
from .google_types import DriveService, FileBody, Presentation, SlidesService, file_id
from .json_types import Json

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PART_NAME = "beamer2slides export part (temporary)"
WORKERS = 3
TRIES = 3   # exports of one file, where Drive stumbled (`export_bytes`)
#: The reasons Drive gives for an export that is too large, rather than not allowed.
SIZE_REASONS = {"exportSizeLimitExceeded"}

#: The Slides client that cuts copies, or what makes one (called only when parts are needed).
SlidesSource = Union[SlidesService, Callable[[], SlidesService]]
#: What makes a worker thread's own (drive, slides) pair.
Clients = Callable[[], tuple[DriveService, SlidesService]]
Span = tuple[int, int]


def reasons(error: BaseException) -> set[str]:
    """The `reason` strings of an API error's content ({} for anything else)."""
    content = getattr(error, "content", None)
    if not isinstance(content, (bytes, str)) or not content:
        return set()
    try:
        body: Json = json.loads(content)
    except ValueError:
        return set()
    inner = body.get("error") if isinstance(body, dict) else None
    errors = inner.get("errors") if isinstance(inner, dict) else None
    if not isinstance(errors, list):
        return set()
    found: set[str] = set()
    for e in errors:
        reason = e.get("reason") if isinstance(e, dict) else None
        if isinstance(reason, str) and reason:
            found.add(reason)
    return found


def deck_ids(pres: Mapping[str, object]) -> tuple[str, list[str]]:
    """A presentations.get's id and its slides' objectIds, in order - all `export_deck` reads of
    it. Raises ValueError on a read that lacks them (a `fields=` mask that left them out)."""
    pid = pres.get("presentationId")
    slides = pres.get("slides", [])
    if not isinstance(pid, str) or not isinstance(slides, list):
        raise ValueError("a presentation read without its presentationId or slides")
    ids: list[str] = []
    for s in slides:
        oid = s.get("objectId") if isinstance(s, dict) else None
        if not isinstance(oid, str):
            raise ValueError(f"slide {len(ids) + 1} of {pid} was read without its objectId")
        ids.append(oid)
    return pid, ids


def too_large(error: BaseException) -> bool:
    """Whether a failed export failed for the deck's size - past Drive's limit, or past the time a
    connection waits for it - and a smaller part could still come out. A refusal of the file
    itself (a permission, a 404) is not: no copy would be allowed where the file is not."""
    if isinstance(error, TimeoutError):
        return True
    if not isinstance(error, HttpError) or status_of(error) not in (403, 413, None):
        return False
    if reasons(error) & SIZE_REASONS:
        return True
    said = message_within(error, 500).lower()
    return "too large" in said or "exportsizelimitexceeded" in said


def export_bytes(drive: DriveService, fid: str) -> bytes:
    """One .pptx export of a Drive file, in up to `TRIES` tries. A rate limit or a server that
    stumbled is tried again, a timeout is not: an export that took `SLOW_EXPORT` once takes it
    again, and three of them were fifteen minutes of waiting before anybody was told."""
    from .gslides import SLOW_EXPORT, execute_with
    request = drive.files().export_media(fileId=fid, mimeType=PPTX_MIME)
    attempt = 0
    while True:
        try:
            # (bytes from the library; a file-like object from a client a caller injected)
            data: object = execute_with(request, retries=1, timeout=SLOW_EXPORT)
            if isinstance(data, io.BytesIO):
                return data.getvalue()
            if isinstance(data, (bytes, bytearray, memoryview)):
                return bytes(data)
            raise TypeError(f"an export of {fid} answered {type(data).__name__}, not bytes")
        except TimeoutError:
            raise
        except (HttpError, OSError) as e:
            if attempt >= TRIES - 1 or isinstance(e, HttpError) and not is_transient(e):
                raise
            time.sleep(2 ** attempt + random.random())
            attempt += 1


def keep_only(slides: SlidesService, copy: str, ids: list[str], first: int, end: int) -> None:
    """Delete from the copy every slide outside [first, end) of the original's `ids`, in one
    batchUpdate. The copy keeps the original's ids (measured); should one not, the batch is refused
    whole, and the copy's own ids are read and deleted by their place."""
    from .gslides import execute
    doomed = ids[:first] + ids[end:]
    if not doomed:
        return

    def delete(these: list[str]) -> None:
        execute(slides.presentations().batchUpdate(
            presentationId=copy, body={"requests": [{"deleteObject": {"objectId": i}} for i in these]}))
    try:
        delete(doomed)
    except HttpError as e:
        if status_of(e) != 400:
            raise
        _, now = deck_ids({"presentationId": copy, **execute(slides.presentations().get(
            presentationId=copy, fields="slides.objectId"))})
        if len(now) != len(ids):
            raise
        delete(now[:first] + now[end:])


@dataclass(frozen=True, kw_only=True)
class Part:
    first: int       # the part's slides, [first, end) of the deck's, 0-based
    end: int
    data: bytes
    copied: bool     # False: the deck itself, exported whole

    @property
    def name(self) -> str:
        """`slides-001-004`: its slides, 1-based and inclusive, for a file name."""
        return f"slides-{self.first + 1:03}-{self.end:03}"


class Missing(TypedDict):
    """Slides no part holds: [first, last] 1-based, their ids, and why."""
    slides: list[int]
    ids: list[str]
    reason: str


@dataclass(kw_only=True)
class Export:
    """What `export_deck` made: the parts that came out, in slide order, and the slides that did
    not (`missing`); `exports` (calls), `copies` made and `deleted`, `leftovers` (copies Drive would
    not delete: say so, they are the person's to remove, `tools/drive_usage.py --delete-staging`),
    `refused`: why the deck itself was not exported, when that was not its size. Filled in as the
    export goes, hence not frozen."""

    slides: int
    parts: list[Part]
    missing: list[Missing]
    exports: int
    copies: int
    deleted: int
    leftovers: list[str]
    refused: str | None

    @property
    def whole(self) -> bool:
        """The deck came out in one export of its own."""
        return len(self.parts) == 1 and not self.parts[0].copied

    @property
    def complete(self) -> bool:
        return bool(self.parts) and not self.missing

    def pictures(self, pres: Presentation, wanted: Collection[str] | None) -> dict[str, bytes]:
        """Every picture the parts hold, by the id that owns it (`deck_pictures.picture_urls`). A
        part pairs with `pres` cut to its slides; the masters and layouts, which every part
        carries, come from the first part that has them."""
        from .deck_pictures import exported_pictures
        every = pres.get("slides", [])
        got: dict[str, bytes] = {}
        for part in self.parts:
            sub: Presentation = {**pres, "slides": every[part.first:part.end]}
            for oid, data in exported_pictures(part.data, sub, wanted).items():
                got.setdefault(oid, data)
        return got

    def summary(self) -> dict[str, object]:
        return {"slides": self.slides, "parts": [[p.first + 1, p.end] for p in self.parts],
                "bytes": sum(len(p.data) for p in self.parts), "missing": self.missing,
                "exports": self.exports, "copies": self.copies, "deleted": self.deleted,
                "leftovers": self.leftovers, "refused": self.refused}


def _halves(first: int, end: int) -> list[tuple[int, int]]:
    mid = (first + end + 1) // 2
    return [(first, mid), (mid, end)]


@dataclass(frozen=True, kw_only=True)
class _PartDone:
    """What one part's round of calls did, for the calling thread to count: the copy made (None:
    none), whether it was deleted, the exports tried, and the part's .pptx or why there is none."""

    span: Span
    copy: str | None
    deleted: bool
    exports: int
    data: bytes | None
    error: BaseException | None


def _export_part(drive: DriveService, slides: SlidesService, pid: str, ids: list[str], span: Span,
                 body: FileBody) -> _PartDone:
    """One part: copy, delete the slides outside it, export, delete the copy - the last whatever
    happened before it. Returns what happened, for the calling thread to count."""
    from .gslides import execute
    first, end = span
    try:
        copy = file_id(execute(drive.files().copy(fileId=pid, body=body.copy(), fields="id")),
                       f"a copy of {pid}")
    except (HttpError, OSError) as e:
        return _PartDone(span=span, copy=None, deleted=False, exports=0, data=None, error=e)
    exports, data, error, deleted = 0, None, None, False
    try:
        keep_only(slides, copy, ids, first, end)
        exports = 1
        data = export_bytes(drive, copy)
    except (HttpError, OSError) as e:
        error = e
    finally:
        try:
            execute(drive.files().delete(fileId=copy))
            deleted = True
        except (HttpError, OSError):
            pass
    return _PartDone(span=span, copy=copy, deleted=deleted, exports=exports, data=data, error=error)


def _describe(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "the export timed out"
    if isinstance(error, HttpError):
        return f"HTTP {status_of(error)}: {message_of(error)}"
    return f"{type(error).__name__}: {error}"


def _client(slides: SlidesSource) -> SlidesService | None:
    """A Slides client out of a client or a function making one (None where it could not)."""
    if isinstance(slides, SlidesService):
        return slides
    try:
        return slides()
    except Exception:  # noqa: BLE001 (no credentials on this thread: no parts, as with no client)
        return None


def export_deck(drive: DriveService, slides: SlidesSource | None, pres: Mapping[str, object],
                per_part: int | None = None, workers: int = WORKERS, clients: Clients | None = None) -> Export:
    """The deck `pres` describes (a presentations.get; only `presentationId` and the slides'
    objectIds are needed, `deck_ids`) as .pptx: whole where Drive gives it, else in parts.
    `per_part`: start from parts of that many slides instead of the whole deck. `slides`: the
    Slides client that deletes a copy's other slides, or a function making one, called only when
    parts are needed; None (or a function that fails): no parts - the whole export or nothing, as
    before. `clients`: what makes a worker thread's (drive, slides) pair (None: built from
    credentials resolved here)."""
    pid, ids = deck_ids(pres)
    n = len(ids)
    result = Export(slides=n, parts=[], missing=[], exports=0, copies=0, deleted=0, leftovers=[], refused=None)
    cutter: SlidesService | None
    if not per_part or per_part >= n or slides is None:
        result.exports += 1
        try:
            result.parts.append(Part(first=0, end=n, data=export_bytes(drive, pid), copied=False))
            return result
        except (HttpError, OSError) as e:
            cutter = _client(slides) if slides is not None and n > 1 and too_large(e) else None
            if cutter is None or n < 2 or not too_large(e):
                if not too_large(e):
                    result.refused = _describe(e)
                result.missing.append({"slides": [1, n], "ids": ids, "reason": _describe(e)})
                return result
        spans = _halves(0, n)
    else:
        spans = [(a, min(a + per_part, n)) for a in range(0, n, per_part)]
        cutter = _client(slides)
        if cutter is None:
            result.missing.append({"slides": [1, n], "ids": ids, "reason": "no Slides client to cut the deck with"})
            return result
    from .drive_folder import place
    body: FileBody = place({"name": PART_NAME, "appProperties": {"b2sStaging": pid}}, drive)  # (resolved once, here)
    _run(result, drive, cutter, pid, ids, spans, body, workers, clients)
    result.parts.sort(key=lambda p: p.first)
    result.missing.sort(key=lambda m: m["slides"][0])
    return result


def _run(result: Export, drive: DriveService, slides: SlidesService, pid: str, ids: list[str],
         spans: list[Span], body: FileBody, workers: int, clients: Clients | None) -> None:
    from .google_auth import credentials_for_threads, drive_service, shared_service, slides_service
    from .gslides import per_thread

    pending = deque(spans)

    def settle(out: _PartDone) -> None:
        first, end = out.span
        result.exports += out.exports
        if out.copy:
            result.copies += 1
            if out.deleted:
                result.deleted += 1
            else:
                result.leftovers.append(out.copy)
        if out.data is not None:
            result.parts.append(Part(first=first, end=end, data=out.data, copied=True))
        elif end - first > 1 and out.copy and out.error is not None and too_large(out.error):
            pending.extend(_halves(first, end))
        else:
            reason = _describe(out.error) if out.error is not None else "no export and no error"
            result.missing.append({"slides": [first + 1, end], "ids": ids[first:end], "reason": reason})

    if workers <= 1 or clients is None and (shared_service("drive", "v3") or shared_service("slides", "v1")):
        # A caller's own clients: theirs and one thread's (`emit.measure_places`' rule).
        while pending:
            settle(_export_part(drive, slides, pid, ids, pending.popleft(), body))
        return
    if clients is None:
        # Resolved here: a worker inherits no context. The lent client's own first - this may itself
        # run on a thread that was handed clients and no context (`guard.WayBack`), where the token
        # file is not whose deck this is.
        creds = lent_credentials(drive) or credentials_for_threads()

        def own() -> tuple[DriveService, SlidesService]:
            return drive_service(creds), slides_service(creds)
        clients = own
    client = per_thread(clients)

    def job(span: Span) -> _PartDone:
        d, s = client()
        return _export_part(d, s, pid, ids, span, body)

    with ThreadPoolExecutor(workers, thread_name_prefix="b2s-export") as pool:
        running: dict[Future[_PartDone], Span] = {}
        while pending or running:
            while pending and len(running) < workers:
                span = pending.popleft()
                running[pool.submit(job, span)] = span
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                del running[fut]
                settle(fut.result())
