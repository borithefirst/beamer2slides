"""The Google clients as the offline tests fake them: `google_types`' Protocols, every method there.

A test's fake Drive or Slides client derives from `NoFiles` / `NoDrive` / `NoPresentations` /
`NoSlides` and overrides what it fakes; any other call is the test's own mistake and says so
(`unfaked`). A call's answer is an `Answer` (a value, or the error Google would raise when it is
executed) or a `Later` (worked out when it is executed, as the library does)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, NoReturn, TypeVar

from beamer2slides.google_types import (BatchUpdateResponse, Comment, CommentList, Comments, DocsBatchUpdateResponse,
                                        Document, Documents, DriveFile, Empty, FileList, Files, Page, Pages,
                                        Permissions, Presentation, Presentations, Request, Thumbnail)
from beamer2slides.net import Fetch

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import (CopyFile, CreateComment, CreateFile, CreatePresentation, ExecuteOptions,
                                            ExportFile, FileId, GetDocument, GetFile, GetPage, GetPresentation,
                                            GetThumbnail, ListComments, ListFiles, UpdateDocument, UpdateFile,
                                            UpdatePresentation)

T = TypeVar("T")

Fetcher = Callable[[Fetch], None]
"""What the `fetcher` fixture (conftest) hands a test: installs a download function for the test."""


def unfaked(what: str) -> NoReturn:
    raise AssertionError(f"{what} is not faked here: the test hands in a fake that makes it")


class Answer(Generic[T]):
    """A call whose answer is `value`, or which raises it when it is an exception."""

    def __init__(self, value: T | BaseException) -> None:
        self.value = value

    def execute(self, **options: Unpack[ExecuteOptions]) -> T:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class Later(Generic[T]):
    """A call whose answer `run` works out when it is executed (and may raise)."""

    def __init__(self, run: Callable[[], T]) -> None:
        self.run = run

    def execute(self, **options: Unpack[ExecuteOptions]) -> T:
        return self.run()


class NoFiles:
    """`drive.files()`, refusing every call."""

    def list(self, **kw: Unpack[ListFiles]) -> Request[FileList]:
        unfaked("files().list")

    def get(self, **kw: Unpack[GetFile]) -> Request[DriveFile]:
        unfaked("files().get")

    def create(self, **kw: Unpack[CreateFile]) -> Request[DriveFile]:
        unfaked("files().create")

    def update(self, **kw: Unpack[UpdateFile]) -> Request[DriveFile]:
        unfaked("files().update")

    def copy(self, **kw: Unpack[CopyFile]) -> Request[DriveFile]:
        unfaked("files().copy")

    def delete(self, **kw: Unpack[FileId]) -> Request[Empty]:
        unfaked("files().delete")

    def get_media(self, **kw: Unpack[FileId]) -> Request[bytes]:
        unfaked("files().get_media")

    def export(self, **kw: Unpack[ExportFile]) -> Request[bytes]:
        unfaked("files().export")

    def export_media(self, **kw: Unpack[ExportFile]) -> Request[bytes]:
        unfaked("files().export_media")


class NoDrive:
    """A Drive client refusing every call."""

    def files(self) -> Files:
        unfaked("drive.files()")

    def permissions(self) -> Permissions:
        unfaked("drive.permissions()")

    def comments(self) -> Comments:
        unfaked("drive.comments()")


class NoPresentations:
    """`slides.presentations()`, refusing every call."""

    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
        unfaked("presentations().get")

    def create(self, **kw: Unpack[CreatePresentation]) -> Request[Presentation]:
        unfaked("presentations().create")

    def batchUpdate(self, **kw: Unpack[UpdatePresentation]) -> Request[BatchUpdateResponse]:
        unfaked("presentations().batchUpdate")

    def pages(self) -> Pages:
        unfaked("presentations().pages()")


class NoPages:
    """`slides.presentations().pages()`, refusing every call."""

    def get(self, **kw: Unpack[GetPage]) -> Request[Page]:
        unfaked("pages().get")

    def getThumbnail(self, **kw: Unpack[GetThumbnail]) -> Request[Thumbnail]:
        unfaked("pages().getThumbnail")


class NoSlides:
    """A Slides client refusing every call."""

    def presentations(self) -> Presentations:
        unfaked("slides.presentations()")


class NoComments:
    """`drive.comments()`, refusing every call."""

    def list(self, **kw: Unpack[ListComments]) -> Request[CommentList]:
        unfaked("comments().list")

    def create(self, **kw: Unpack[CreateComment]) -> Request[Comment]:
        unfaked("comments().create")


class NoDocuments:
    """`docs.documents()`, refusing every call."""

    def get(self, **kw: Unpack[GetDocument]) -> Request[Document]:
        unfaked("documents().get")

    def batchUpdate(self, **kw: Unpack[UpdateDocument]) -> Request[DocsBatchUpdateResponse]:
        unfaked("documents().batchUpdate")


class NoDocs:
    """A Docs client refusing every call."""

    def documents(self) -> Documents:
        unfaked("docs.documents()")
