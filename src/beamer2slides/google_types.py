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

A TypedDict is not assignable to a parameter annotated `dict`, and a `JsonObject` is. So
`presentations.get` answers a `Presentation` and `files.get` a `DriveFile`, and their readers
(snapshot, sync, theme_sync, guard, deck_pictures) say so; a reader not typed yet takes a
presentation.json it loaded through `presentation` (checked against the TypedDict, as far as it is
modelled) and hands an answer on as JSON through `as_json` (checked all the way down).

Nothing here imports the client library: `gapi` builds the real clients and says they are these.
The functions here are boundary reads: `file_id` for every `files.create`/`copy` caller, and the
walk of a presentation (`object_id`, `children`, `all_elements`, `background_url`, `image_url`),
where an id a `fields=` mask left out, or a group's child that is no page element, is said once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, TypeGuard, TypeVar, runtime_checkable

from .json_types import Json, JsonObject, JsonShapeError, as_objects

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


# ------------------------------------------------------------------------------ Slides: walking an answer

UNITS = ("EMU", "PT", "UNIT_UNSPECIFIED")
TRANSFORM_NUMBERS = ("scaleX", "scaleY", "shearX", "shearY", "translateX", "translateY")
ELEMENT_PARTS = ("shape", "image", "table", "line", "elementGroup", "sheetsChart", "video", "wordArt",
                 "speakerSpotlight")


def object_id(o: PageElement | Page) -> str:
    """The id of a page or page element. Google answers one for each, and only a `fields=` mask
    that did not ask for it leaves it out: every key of an answer is optional to the type, this one
    is not to its reader, so its absence is said here once rather than as a KeyError."""
    oid = o.get("objectId")
    if oid is None:
        raise JsonShapeError("a page or page element without its objectId (a fields= mask that left it out?)")
    return oid


def presentation_id(p: Presentation) -> str:
    """The id of a presentation read with `presentations.get` (as `object_id`)."""
    pid = p.get("presentationId")
    if pid is None:
        raise JsonShapeError("a presentation without its presentationId (a fields= mask that left it out?)")
    return pid


def _number(v: Json) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_dimension(v: Json) -> bool:
    return isinstance(v, dict) and ("magnitude" not in v or _number(v["magnitude"])) and v.get("unit", "EMU") in UNITS


def _is_size(v: Json) -> bool:
    return isinstance(v, dict) and all(_is_dimension(v[k]) for k in ("width", "height") if k in v)


def _is_transform(v: Json) -> bool:
    return isinstance(v, dict) and all(_number(v[k]) for k in TRANSFORM_NUMBERS if k in v) \
        and v.get("unit", "EMU") in UNITS


def is_page_element(o: JsonObject) -> TypeGuard[PageElement]:
    """Whether `o` holds what `PageElement` says of each key it has - the object itself, not a copy,
    so what a walker writes into it lands in the answer it came from."""
    return all(isinstance(o[k], str) for k in ("objectId", "title", "description") if k in o) \
        and ("size" not in o or _is_size(o["size"])) and ("transform" not in o or _is_transform(o["transform"])) \
        and all(isinstance(o[k], dict) for k in ELEMENT_PARTS if k in o)


def children(e: PageElement, where: str) -> list[PageElement]:
    """A group's own elements (none for any other element). `elementGroup` is JSON to the type, as
    every nested part nobody models, but its children are page elements: checked as such here,
    where a walker steps into them, so a child of another shape is an error naming the group."""
    group = e.get("elementGroup")
    kids = None if group is None else group.get("children")
    if kids is None:
        return []
    out: list[PageElement] = []
    for i, kid in enumerate(as_objects(kids, f"{where}.children")):
        if not is_page_element(kid):
            raise JsonShapeError(f"{where}.children[{i}]: not a page element")
        out.append(kid)
    return out


PAGE_TYPES = ("SLIDE", "MASTER", "LAYOUT", "NOTES", "NOTES_MASTER")


def _strings(o: JsonObject, keys: Sequence[str]) -> bool:
    return all(isinstance(o[k], str) for k in keys if k in o)


def _is_page(v: Json) -> bool:
    if not isinstance(v, dict):
        return False
    elements = v.get("pageElements", [])
    slide = v.get("slideProperties", {})
    layout = v.get("layoutProperties", {})
    return _strings(v, ("objectId", "revisionId")) and v.get("pageType", "SLIDE") in PAGE_TYPES \
        and isinstance(elements, list) and all(isinstance(e, dict) and is_page_element(e) for e in elements) \
        and all(isinstance(v[k], dict) for k in ("pageProperties", "notesProperties", "masterProperties") if k in v) \
        and isinstance(slide, dict) and _strings(slide, ("layoutObjectId", "masterObjectId")) \
        and isinstance(slide.get("isSkipped", False), bool) and ("notesPage" not in slide or _is_page(slide["notesPage"])) \
        and isinstance(layout, dict) and _strings(layout, ("masterObjectId", "name", "displayName"))


def is_presentation(o: JsonObject) -> TypeGuard[Presentation]:
    """Whether `o` - a `presentations.get` kept as JSON (a deck-files presentation.json, a reader
    not typed yet) - holds what `Presentation` says, pages and page elements included, as far as
    they are modelled. The object itself, not a copy."""
    for key in ("slides", "masters", "layouts"):
        pages = o.get(key)
        if pages is not None and not (isinstance(pages, list) and all(_is_page(p) for p in pages)):
            return False
    return ("notesMaster" not in o or _is_page(o["notesMaster"])) \
        and _strings(o, ("presentationId", "title", "locale", "revisionId")) \
        and ("pageSize" not in o or _is_size(o["pageSize"]))


def presentation(o: JsonObject, where: str) -> Presentation:
    """`o` as the `presentations.get` it was read from (`is_presentation`), or an error naming `where`."""
    if not is_presentation(o):
        raise JsonShapeError(f"{where}: not a presentation as presentations.get answers one")
    return o


def _is_json(v: object) -> bool:
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, list):
        return all(_is_json(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _is_json(x) for k, x in v.items())
    return False


def _is_json_object(v: object) -> TypeGuard[JsonObject]:
    return isinstance(v, dict) and _is_json(v)


def as_json(answer: Presentation | Page | PageElement | DriveFile, where: str) -> JsonObject:
    """An answer, or a part of one, handed to a reader that still reads it as JSON (deck_ir, the
    devtools) or put back into JSON (a group's children): what it is, checked all the way down (no
    copy), since a TypedDict is not assignable to a `dict` parameter nor a `Json` value."""
    value: object = answer
    if not _is_json_object(value):
        raise JsonShapeError(f"{where}: not JSON")
    return value


def part(value: Json, where: str) -> JsonObject:
    """A nested object of an answer, which Google leaves out when it is empty: absent is `{}`."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JsonShapeError(f"{where}: an object was expected, found {type(value).__name__}")
    return value


def parts(value: Json, where: str) -> list[JsonObject]:
    """A nested list of objects of an answer (absent: none)."""
    return [] if value is None else as_objects(value, where)


def _url(value: Json, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise JsonShapeError(f"{where}: a string was expected, found {type(value).__name__}")
    return value or None


def background_fill(page: Page) -> JsonObject:
    """A page's `pageBackgroundFill` (`{}`: the page says none)."""
    return part(part(page.get("pageProperties"), "pageProperties").get("pageBackgroundFill"),
                 "pageProperties.pageBackgroundFill")


def background_url(page: Page) -> str | None:
    """The contentUrl of a page's background picture (None: it has none)."""
    picture = part(background_fill(page).get("stretchedPictureFill"), "pageBackgroundFill.stretchedPictureFill")
    return _url(picture.get("contentUrl"), "stretchedPictureFill.contentUrl")


def image_url(e: PageElement) -> str | None:
    """The contentUrl of an image element (None: another element, or no picture)."""
    image = e.get("image")
    return None if image is None else _url(image.get("contentUrl"), "image.contentUrl")


def all_elements(elements: Sequence[PageElement], where: str) -> list[PageElement]:
    """`elements` and everything in their groups, depth first, a group before its children (the
    order a .pptx export draws them in)."""
    out: list[PageElement] = []
    for e in elements:
        out.append(e)
        out += all_elements(children(e, where), f"{where}/{e.get('objectId')}")
    return out


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
    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]: ...
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
    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]: ...
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


# The parts of a document `doc_ir` reads (its `_NODES` graph says the same thing, for the
# report of what it does not read). Styles, colours and sizes are also what the requests
# below write, so the two halves share them.


class DocsDimension(TypedDict, total=False):
    magnitude: float
    unit: str


class DocsRgbColor(TypedDict, total=False):
    red: float
    green: float
    blue: float


class DocsColor(TypedDict, total=False):
    rgbColor: DocsRgbColor


class DocsOptionalColor(TypedDict, total=False):
    color: DocsColor


class DocsWeightedFontFamily(TypedDict, total=False):
    fontFamily: str
    weight: int


class DocsLink(TypedDict, total=False):
    url: str


class DocsTextStyle(TypedDict, total=False):
    bold: bool
    italic: bool
    underline: bool
    strikethrough: bool
    smallCaps: bool
    baselineOffset: str
    weightedFontFamily: DocsWeightedFontFamily
    fontSize: DocsDimension
    foregroundColor: DocsOptionalColor
    backgroundColor: DocsOptionalColor
    link: DocsLink


class DocsParagraphBorder(TypedDict, total=False):
    color: DocsOptionalColor
    width: DocsDimension
    padding: DocsDimension
    dashStyle: str


class DocsShading(TypedDict, total=False):
    backgroundColor: DocsOptionalColor


class DocsParagraphStyle(TypedDict, total=False):
    namedStyleType: str
    alignment: str
    indentStart: DocsDimension
    indentFirstLine: DocsDimension
    lineSpacing: float
    spaceAbove: DocsDimension
    spaceBelow: DocsDimension
    shading: DocsShading
    borderTop: DocsParagraphBorder
    borderBottom: DocsParagraphBorder
    borderLeft: DocsParagraphBorder
    borderRight: DocsParagraphBorder
    pageBreakBefore: bool
    keepWithNext: bool


class DocsTextRun(TypedDict, total=False):
    content: str
    textStyle: DocsTextStyle


class DocsDateElementProperties(TypedDict, total=False):
    displayText: str
    timestamp: str
    dateFormat: str
    timeFormat: str
    locale: str


class DocsDateElement(TypedDict, total=False):
    dateElementProperties: DocsDateElementProperties


class DocsPersonProperties(TypedDict, total=False):
    name: str
    email: str


class DocsPerson(TypedDict, total=False):
    personProperties: DocsPersonProperties


class DocsRichLinkProperties(TypedDict, total=False):
    title: str
    uri: str
    mimeType: str


class DocsRichLink(TypedDict, total=False):
    richLinkProperties: DocsRichLinkProperties


class DocsFootnoteReference(TypedDict, total=False):
    footnoteNumber: str
    footnoteId: str


class DocsInlineObjectElement(TypedDict, total=False):
    inlineObjectId: str


class DocsParagraphElement(TypedDict, total=False):
    startIndex: int
    endIndex: int
    textRun: DocsTextRun
    dateElement: DocsDateElement
    person: DocsPerson
    richLink: DocsRichLink
    footnoteReference: DocsFootnoteReference
    equation: JsonObject
    inlineObjectElement: DocsInlineObjectElement
    horizontalRule: JsonObject


class DocsBullet(TypedDict, total=False):
    listId: str
    nestingLevel: int


class DocsParagraph(TypedDict, total=False):
    elements: list[DocsParagraphElement]
    paragraphStyle: DocsParagraphStyle
    bullet: DocsBullet


class DocsTableCell(TypedDict, total=False):
    startIndex: int
    endIndex: int
    content: list[DocsStructuralElement]


class DocsTableRow(TypedDict, total=False):
    startIndex: int
    endIndex: int
    tableCells: list[DocsTableCell]


class DocsTable(TypedDict, total=False):
    rows: int
    columns: int
    tableRows: list[DocsTableRow]


class DocsStructuralElement(TypedDict, total=False):
    startIndex: int
    endIndex: int
    paragraph: DocsParagraph
    table: DocsTable
    tableOfContents: JsonObject
    sectionBreak: JsonObject


class DocsBody(TypedDict, total=False):
    content: list[DocsStructuralElement]


class DocsNestingLevel(TypedDict, total=False):
    glyphSymbol: str
    glyphType: str
    glyphFormat: str


class DocsListProperties(TypedDict, total=False):
    nestingLevels: list[DocsNestingLevel]


class DocsList(TypedDict, total=False):
    listProperties: DocsListProperties


class DocsSize(TypedDict, total=False):
    width: DocsDimension
    height: DocsDimension


class DocsImageProperties(TypedDict, total=False):
    contentUri: str


class DocsEmbeddedObject(TypedDict, total=False):
    title: str
    description: str
    size: DocsSize
    imageProperties: DocsImageProperties


class DocsInlineObjectProperties(TypedDict, total=False):
    embeddedObject: DocsEmbeddedObject


class DocsInlineObject(TypedDict, total=False):
    objectId: str
    inlineObjectProperties: DocsInlineObjectProperties


class DocsNamedStyle(TypedDict, total=False):
    namedStyleType: str
    textStyle: DocsTextStyle
    paragraphStyle: DocsParagraphStyle


class DocsNamedStyles(TypedDict, total=False):
    styles: list[DocsNamedStyle]


class DocsRange(TypedDict, total=False):
    startIndex: int
    endIndex: int
    segmentId: str
    tabId: str


class DocsNamedRange(TypedDict, total=False):
    namedRangeId: str
    name: str
    ranges: list[DocsRange]


class DocsNamedRanges(TypedDict, total=False):
    """All the ranges of one name (`namedRanges` maps a name to one of these)."""
    name: str
    namedRanges: list[DocsNamedRange]


class DocsTabProperties(TypedDict, total=False):
    tabId: str
    title: str
    parentTabId: str
    index: int
    nestingLevel: int


class DocsDocumentTab(TypedDict, total=False):
    body: DocsBody
    lists: dict[str, DocsList]
    inlineObjects: dict[str, DocsInlineObject]
    namedStyles: DocsNamedStyles
    namedRanges: dict[str, DocsNamedRanges]
    documentStyle: JsonObject


class DocsTab(TypedDict, total=False):
    tabProperties: DocsTabProperties
    documentTab: DocsDocumentTab
    childTabs: list[DocsTab]


class Document(TypedDict, total=False):
    """`documents.get`. With `includeTabsContent` the content is under `tabs`, not `body`."""
    documentId: str
    title: str
    revisionId: str
    suggestionsViewMode: str
    body: DocsBody
    tabs: list[DocsTab]
    namedRanges: dict[str, DocsNamedRanges]
    inlineObjects: dict[str, DocsInlineObject]
    lists: dict[str, DocsList]
    documentStyle: JsonObject
    namedStyles: DocsNamedStyles


# The requests `doc_merge` and `doc_ir` write, as the discovery document spells them. A
# `Request` is a oneof: exactly one of its keys is set, which is how the API says it and
# how a reader (`devtools/doc_world`) asks which one it holds (`request.get(...)`).


class DocsLocation(TypedDict, total=False):
    index: Required[int]
    tabId: str
    segmentId: str


class DocsEndOfSegmentLocation(TypedDict, total=False):
    tabId: str
    segmentId: str


class DocsRangeWrite(TypedDict, total=False):
    """A `Range` a request names: always both ends, a tab where it is not the first."""
    startIndex: Required[int]
    endIndex: Required[int]
    tabId: str
    segmentId: str


class DocsTabsCriteria(TypedDict, total=False):
    tabIds: list[str]


class InsertTextRequest(TypedDict, total=False):
    text: Required[str]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class DeleteContentRangeRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]


class UpdateTextStyleRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    textStyle: Required[DocsTextStyle]
    fields: Required[str]


class UpdateParagraphStyleRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    paragraphStyle: Required[DocsParagraphStyle]
    fields: Required[str]


class CreateParagraphBulletsRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]
    bulletPreset: Required[str]


class DeleteParagraphBulletsRequest(TypedDict, total=False):
    range: Required[DocsRangeWrite]


class CreateNamedRangeRequest(TypedDict, total=False):
    name: Required[str]
    range: Required[DocsRangeWrite]


class DeleteNamedRangeRequest(TypedDict, total=False):
    namedRangeId: str
    name: str
    tabsCriteria: DocsTabsCriteria


class InsertTableRequest(TypedDict, total=False):
    rows: Required[int]
    columns: Required[int]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation


class DocsTableCellLocation(TypedDict, total=False):
    tableStartLocation: Required[DocsLocation]
    rowIndex: Required[int]
    columnIndex: Required[int]


class InsertTableRowRequest(TypedDict, total=False):
    tableCellLocation: Required[DocsTableCellLocation]
    insertBelow: Required[bool]


class InsertTableColumnRequest(TypedDict, total=False):
    tableCellLocation: Required[DocsTableCellLocation]
    insertRight: Required[bool]


class DeleteTableLineRequest(TypedDict, total=False):
    """`deleteTableRow` and `deleteTableColumn`: the line through one cell."""
    tableCellLocation: Required[DocsTableCellLocation]


class DocsObjectSize(TypedDict, total=False):
    width: DocsDimension
    height: DocsDimension


class InsertInlineImageRequest(TypedDict, total=False):
    uri: Required[str]
    location: DocsLocation
    endOfSegmentLocation: DocsEndOfSegmentLocation
    objectSize: DocsObjectSize


class InsertPersonRequest(TypedDict, total=False):
    location: Required[DocsLocation]
    personProperties: Required[DocsPersonProperties]


class InsertDateRequest(TypedDict, total=False):
    location: Required[DocsLocation]
    dateElementProperties: Required[DocsDateElementProperties]


class AddDocumentTabRequest(TypedDict, total=False):
    tabProperties: Required[DocsTabProperties]


class UpdateDocumentTabPropertiesRequest(TypedDict, total=False):
    tabProperties: Required[DocsTabProperties]
    fields: Required[str]


class DeleteTabRequest(TypedDict, total=False):
    tabId: Required[str]


class DocsRequest(TypedDict, total=False):
    """One request of a `documents.batchUpdate`: exactly one of these is set."""
    insertText: InsertTextRequest
    deleteContentRange: DeleteContentRangeRequest
    updateTextStyle: UpdateTextStyleRequest
    updateParagraphStyle: UpdateParagraphStyleRequest
    createParagraphBullets: CreateParagraphBulletsRequest
    deleteParagraphBullets: DeleteParagraphBulletsRequest
    createNamedRange: CreateNamedRangeRequest
    deleteNamedRange: DeleteNamedRangeRequest
    insertTable: InsertTableRequest
    insertTableRow: InsertTableRowRequest
    insertTableColumn: InsertTableColumnRequest
    deleteTableRow: DeleteTableLineRequest
    deleteTableColumn: DeleteTableLineRequest
    insertInlineImage: InsertInlineImageRequest
    insertPerson: InsertPersonRequest
    insertDate: InsertDateRequest
    addDocumentTab: AddDocumentTabRequest
    updateDocumentTabProperties: UpdateDocumentTabPropertiesRequest
    deleteTab: DeleteTabRequest


# Every kind of request above, by the key that says it.
DocsRequestKind = Literal[
    "insertText", "deleteContentRange", "updateTextStyle", "updateParagraphStyle",
    "createParagraphBullets", "deleteParagraphBullets", "createNamedRange", "deleteNamedRange",
    "insertTable", "insertTableRow", "insertTableColumn", "deleteTableRow", "deleteTableColumn",
    "insertInlineImage", "insertPerson", "insertDate", "addDocumentTab",
    "updateDocumentTabProperties", "deleteTab"]
DOCS_REQUEST_KINDS: tuple[DocsRequestKind, ...] = (
    "insertText", "deleteContentRange", "updateTextStyle", "updateParagraphStyle",
    "createParagraphBullets", "deleteParagraphBullets", "createNamedRange", "deleteNamedRange",
    "insertTable", "insertTableRow", "insertTableColumn", "deleteTableRow", "deleteTableColumn",
    "insertInlineImage", "insertPerson", "insertDate", "addDocumentTab",
    "updateDocumentTabProperties", "deleteTab")


def docs_request_kind(request: DocsRequest) -> DocsRequestKind:
    """Which request this is: the one key of the oneof that is set."""
    for kind in DOCS_REQUEST_KINDS:
        if kind in request:
            return kind
    raise ValueError(f"a Docs request of no kind we write: {sorted(request)}")


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
