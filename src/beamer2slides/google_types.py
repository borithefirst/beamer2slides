"""The part of Google's Slides, Drive and Docs APIs this package calls, written down as types.

`googleapiclient` builds its clients at runtime from a discovery document, so to a type checker a
client is a `Resource` with no methods at all: `slides.presentations().get(...)` was an unknown
call returning an unknown answer, and so was every answer read from it. These Protocols say which
methods we call, with which keywords, and what each one answers - a misspelt method, a keyword the
API does not take or a missing required one is an error where it is written. They describe only
what the package uses; a call not listed here is one to add here first.

The keywords of each method are a TypedDict (`**kw: Unpack[...]`), as the discovery document lists
them: `Required` where Google requires one, optional otherwise. That is how a method with optional
parameters is written without a default value.

Answers are JSON, described by TypedDicts rather than parsed into dataclasses, on purpose:
- they are Google's schema, not ours, and open-ended: Google adds fields, and a field mask
  (`fields=`) leaves out any of them - so every key is optional (`total=False`), and a reader
  says what happens when one is absent;
- they are kept and written back as they came (a presentation.json, a sync base's read-back), so
  the dict is the value; a dataclass would have to carry every field it does not model;
- what the package decides from an answer is its own type, parsed from these where it is read
  (docs/typing.md, "Parse at the boundary").
Nested parts nobody reads through a typed client yet (a shape's text, a table's cells, a Doc's
body) are `JsonObject` (`json_types`): JSON the checker follows no further until someone models it
here, read with `json_types`' narrowings.

One rule decides between the two for a whole answer: a TypedDict is not assignable to a parameter
annotated `dict`, and a `JsonObject` is. An answer that code annotated `dict` still receives -
`presentations.get` (snapshot's readers, `Sync.first_read`), `files.get` (`snapshot.store_base`),
`documents.get` (doc_ir) - is a `JsonObject` until those parameters say its TypedDict; the
TypedDict is here already (`Presentation`, `DriveFile`, `Document`), and switching the method's
answer to it is the one-line change of that later wave.

Nothing here imports the client library: `gapi` builds the real clients and says they are these.
The one function here, `file_id`, is the boundary read every `files.create`/`copy` caller needs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, TypeVar, runtime_checkable

from .json_types import JsonObject

if TYPE_CHECKING:
    from typing_extensions import Required, Unpack

T_co = TypeVar("T_co", covariant=True)


# ------------------------------------------------------------------------------ one call


class ExecuteOptions(TypedDict, total=False):
    """`HttpRequest.execute`'s keyword: the connection to answer over (`gapi.patient_http`)."""
    http: object


class Request(Protocol[T_co]):
    """One API call, made when executed: what a method of a client returns. `gslides.execute` runs
    one with retries; a client a caller injected answers `execute()` with no keyword at all, and is
    only ever given `http` when `gapi.patient_http` found the library's own connection on it."""

    def execute(self, **options: Unpack[ExecuteOptions]) -> T_co: ...


class MediaBody(Protocol):
    """A file's content for `media_body` (`gapi.media_upload`); a fake Drive reads it back with
    `getbytes(0, size())`, as the library's own upload does."""

    def mimetype(self) -> str: ...
    def size(self) -> int: ...
    def getbytes(self, begin: int, length: int) -> bytes: ...


# ------------------------------------------------------------------------------ Slides: answers


class Dimension(TypedDict, total=False):
    magnitude: float
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"]


class Size(TypedDict, total=False):
    width: Dimension
    height: Dimension


class AffineTransform(TypedDict, total=False):
    scaleX: float
    scaleY: float
    shearX: float
    shearY: float
    translateX: float
    translateY: float
    unit: Literal["EMU", "PT", "UNIT_UNSPECIFIED"]


class PageElement(TypedDict, total=False):
    objectId: str
    size: Size
    transform: AffineTransform
    title: str
    description: str
    shape: JsonObject
    image: JsonObject
    table: JsonObject
    line: JsonObject
    elementGroup: JsonObject
    sheetsChart: JsonObject
    video: JsonObject
    wordArt: JsonObject
    speakerSpotlight: JsonObject


class SlideProperties(TypedDict, total=False):
    layoutObjectId: str
    masterObjectId: str
    notesPage: Page
    isSkipped: bool


class LayoutProperties(TypedDict, total=False):
    masterObjectId: str
    name: str
    displayName: str


class Page(TypedDict, total=False):
    objectId: str
    pageType: Literal["SLIDE", "MASTER", "LAYOUT", "NOTES", "NOTES_MASTER"]
    pageElements: list[PageElement]
    revisionId: str
    pageProperties: JsonObject
    slideProperties: SlideProperties
    layoutProperties: LayoutProperties
    notesProperties: JsonObject
    masterProperties: JsonObject


class Presentation(TypedDict, total=False):
    """`presentations.get` / `create`."""
    presentationId: str
    title: str
    locale: str
    revisionId: str
    pageSize: Size
    slides: list[Page]
    masters: list[Page]
    layouts: list[Page]
    notesMaster: Page


class WriteControl(TypedDict, total=False):
    requiredRevisionId: str


class BatchUpdateResponse(TypedDict, total=False):
    """`presentations.batchUpdate`: one reply per request, in order (`{}` for one that makes
    nothing), and the revision the deck is at after it."""
    presentationId: str
    replies: list[JsonObject]
    writeControl: WriteControl


class Thumbnail(TypedDict, total=False):
    """`presentations.pages.getThumbnail`: where Google put the PNG, and its size in pixels."""
    contentUrl: str
    width: int
    height: int


# ------------------------------------------------------------------------------ Slides: calls


class BatchUpdateBody(TypedDict, total=False):
    requests: Required[Sequence[Mapping[str, object]]]
    writeControl: WriteControl


class NewPresentation(TypedDict, total=False):
    title: str


class GetPresentation(TypedDict, total=False):
    presentationId: Required[str]
    fields: str


class CreatePresentation(TypedDict, total=False):
    body: Required[NewPresentation]


class UpdatePresentation(TypedDict, total=False):
    presentationId: Required[str]
    body: Required[BatchUpdateBody]


class GetThumbnail(TypedDict, total=False):
    presentationId: Required[str]
    pageObjectId: Required[str]
    thumbnailProperties_mimeType: Literal["PNG"]
    thumbnailProperties_thumbnailSize: Literal["LARGE", "MEDIUM", "SMALL"]


class GetPage(TypedDict, total=False):
    presentationId: Required[str]
    pageObjectId: Required[str]


class Pages(Protocol):
    def get(self, **kw: Unpack[GetPage]) -> Request[Page]: ...
    def getThumbnail(self, **kw: Unpack[GetThumbnail]) -> Request[Thumbnail]: ...


class Presentations(Protocol):
    # A `JsonObject`, not a `Presentation`, for now: the read is handed to parameters annotated
    # `dict` (snapshot's, sync's `first_read`), which a TypedDict is not assignable to. When they
    # say `Presentation` (or parse it), this says so too - see the module docstring.
    def get(self, **kw: Unpack[GetPresentation]) -> Request[JsonObject]: ...
    def create(self, **kw: Unpack[CreatePresentation]) -> Request[Presentation]: ...
    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]: ...
    def pages(self) -> Pages: ...


@runtime_checkable
class SlidesService(Protocol):
    """The Slides API v1 client (`google_auth.slides_service`)."""

    def presentations(self) -> Presentations: ...


# ------------------------------------------------------------------------------ Drive


class DriveFile(TypedDict, total=False):
    """A Drive file's metadata, as far as `fields=` asked for it."""
    id: str
    name: str
    mimeType: str
    parents: list[str]
    appProperties: dict[str, str]
    trashed: bool
    createdTime: str
    modifiedTime: str


class FileList(TypedDict, total=False):
    files: list[DriveFile]
    nextPageToken: str


class Empty(TypedDict, total=False):
    """What a call that answers nothing answers (`files.delete`)."""


class FileBody(TypedDict, total=False):
    """What `files.create` / `copy` / `update` write. An appProperty set to None is removed."""
    name: str
    mimeType: str
    parents: list[str]
    appProperties: Mapping[str, str | None]
    trashed: bool


class ListFiles(TypedDict, total=False):
    q: str
    spaces: str
    fields: str
    pageSize: int
    pageToken: str | None


class CreateFile(TypedDict, total=False):
    body: Required[FileBody]
    fields: str
    media_body: MediaBody


class FileId(TypedDict, total=False):
    fileId: Required[str]


class GetFile(TypedDict, total=False):
    fileId: Required[str]
    fields: str


class UpdateFile(TypedDict, total=False):
    fileId: Required[str]
    body: FileBody
    fields: str
    media_body: MediaBody


class CopyFile(TypedDict, total=False):
    fileId: Required[str]
    body: FileBody
    fields: str


class ExportFile(TypedDict, total=False):
    fileId: Required[str]
    mimeType: Required[str]


class Files(Protocol):
    def list(self, **kw: Unpack[ListFiles]) -> Request[FileList]: ...
    # (a `JsonObject` for now, as `Presentations.get`: snapshot's `store_base(info=)` takes a `dict`)
    def get(self, **kw: Unpack[GetFile]) -> Request[JsonObject]: ...
    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]: ...
    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]: ...
    def copy(self, **kw: Unpack[CopyFile]) -> Request[DriveFile]: ...
    def delete(self, **kw: Unpack[FileId]) -> Request[Empty]: ...
    def get_media(self, **kw: Unpack[FileId]) -> Request[bytes]: ...
    def export(self, **kw: Unpack[ExportFile]) -> Request[bytes]: ...
    def export_media(self, **kw: Unpack[ExportFile]) -> Request[bytes]: ...


def file_id(answer: DriveFile, what: str) -> str:
    """The id Drive answered for `what` (a file just created, copied or updated with `fields`
    naming `id`): every key of an answer is optional to the type, and this one is not to the
    caller, so its absence is Drive breaking its word, said here once rather than as a KeyError."""
    fid = answer.get("id")
    if fid is None:
        raise ValueError(f"Drive answered no id for {what}")
    return fid


class Permission(TypedDict, total=False):
    id: str
    type: Literal["user", "group", "domain", "anyone"]
    role: Literal["owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"]


class CreatePermission(TypedDict, total=False):
    fileId: Required[str]
    body: Required[Permission]
    fields: str


class Permissions(Protocol):
    def create(self, **kw: Unpack[CreatePermission]) -> Request[Permission]: ...


class Comment(TypedDict, total=False):
    """A comment on a file (`doc_sync.open_comments` reads them for the report)."""
    id: str
    content: str
    resolved: bool
    author: JsonObject
    quotedFileContent: JsonObject
    replies: list[JsonObject]


class CommentList(TypedDict, total=False):
    comments: list[Comment]
    nextPageToken: str


class NewComment(TypedDict, total=False):
    content: Required[str]


class ListComments(TypedDict, total=False):
    fileId: Required[str]
    fields: Required[str]           # (Drive refuses a comments call without a field mask)
    includeDeleted: bool
    pageSize: int
    pageToken: str | None


class CreateComment(TypedDict, total=False):
    fileId: Required[str]
    fields: Required[str]
    body: Required[NewComment]


class Comments(Protocol):
    def list(self, **kw: Unpack[ListComments]) -> Request[CommentList]: ...
    def create(self, **kw: Unpack[CreateComment]) -> Request[Comment]: ...


@runtime_checkable
class DriveService(Protocol):
    """The Drive API v3 client (`google_auth.drive_service`)."""

    def files(self) -> Files: ...
    def permissions(self) -> Permissions: ...
    def comments(self) -> Comments: ...


# ------------------------------------------------------------------------------ Docs


class DocsWriteControl(TypedDict, total=False):
    requiredRevisionId: str
    targetRevisionId: str


class Document(TypedDict, total=False):
    """`documents.get`. With `includeTabsContent` the content is under `tabs`, not `body`."""
    documentId: str
    title: str
    revisionId: str
    body: JsonObject
    tabs: list[JsonObject]
    namedRanges: JsonObject
    inlineObjects: JsonObject
    lists: JsonObject
    documentStyle: JsonObject
    namedStyles: JsonObject


class DocsBatchUpdateResponse(TypedDict, total=False):
    documentId: str
    replies: list[JsonObject]
    writeControl: DocsWriteControl


class DocsBatchUpdateBody(TypedDict, total=False):
    requests: Required[Sequence[Mapping[str, object]]]
    writeControl: DocsWriteControl


class GetDocument(TypedDict, total=False):
    documentId: Required[str]
    includeTabsContent: bool


class UpdateDocument(TypedDict, total=False):
    documentId: Required[str]
    body: Required[DocsBatchUpdateBody]


class Documents(Protocol):
    def get(self, **kw: Unpack[GetDocument]) -> Request[Document]: ...
    def batchUpdate(self, **kw: Unpack[UpdateDocument]) -> Request[DocsBatchUpdateResponse]: ...


@runtime_checkable
class DocsService(Protocol):
    """The Docs API v1 client (`google_auth.docs_service`)."""

    def documents(self) -> Documents: ...
