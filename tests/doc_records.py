"""Reading the Docs IR, a document as Docs reads it, and the requests a plan writes, in a test.

`doc_ir`'s `Block` and `Run` and `google_types`' Docs requests are `total=False` TypedDicts: a key
a test knows is there (a read block's `span`, a paragraph's `runs`, the `insertText` of a request
it has just picked out) is still one the checker must be told about. These say it once: each
returns the value or fails the test naming what was missing, where indexing gave a `KeyError`.
`inserts_in(requests)` and its siblings are the requests of one kind, in order."""

from collections.abc import Sequence

from beamer2slides.doc_ir import U16, Aligned, Block, Ir, Run, Unimported
from beamer2slides.google_types import (AddDocumentTabRequest, CreateNamedRangeRequest, CreateParagraphBulletsRequest,
                                        DeleteContentRangeRequest, DeleteNamedRangeRequest, DeleteTableLineRequest,
                                        DocsLocation, DocsParagraph, DocsRequest, DocsStructuralElement,
                                        Document, InsertDateRequest, InsertInlineImageRequest,
                                        InsertPersonRequest, InsertTableColumnRequest, InsertTableRequest,
                                        InsertTableRowRequest, InsertTextRequest, UpdateParagraphStyleRequest,
                                        UpdateTextStyleRequest)


# ------------------------------------------------------------------------------ the IR

def runs_of(block: Block) -> list[Run]:
    runs = block.get("runs")
    assert runs is not None, f"a block with no runs: {block}"
    return runs


def span_of(block: Block) -> list[U16]:
    span = block.get("span")
    assert span is not None, f"a block with no span: {block}"
    return span


def rows_of(block: Block) -> list[list[list[Block]]]:
    rows = block.get("rows")
    assert rows is not None, f"a block with no rows: {block}"
    return rows


def aligned_of(block: Block) -> Aligned:
    aligned = block.get("aligned")
    assert aligned is not None, f"a block with no alignment: {block}"
    return aligned


def unimported_of(block: Block) -> Unimported:
    unimported = block.get("unimported")
    assert unimported is not None, f"a block the import carried whole: {block}"
    return unimported


def key_of(block: Block) -> str:
    key = block.get("key")
    assert key is not None, f"a block with no key: {block}"
    return key


def range_of(block: Block) -> list[U16]:
    found = block.get("range")
    assert found is not None, f"a block with no named range: {block}"
    return found


def tabs_of(ir: Ir) -> list[Ir]:
    """The tabs past the first."""
    tabs = ir.get("tabs")
    assert tabs is not None, f"a document with no tabs past the first: {sorted(ir)}"
    return tabs


def tab_content(document: Document, index: int) -> list[DocsStructuralElement]:
    """The body of a document's `index`th tab, as `documents.get` with its tabs reads it."""
    tabs = document.get("tabs")
    assert tabs is not None, f"a document read without its tabs: {sorted(document)}"
    content = tabs[index].get("documentTab", {}).get("body", {}).get("content")
    assert content is not None, f"tab {index} has no body: {tabs[index]}"
    return content


def paragraph_of(element: DocsStructuralElement) -> DocsParagraph:
    found = element.get("paragraph")
    assert found is not None, f"not a paragraph: {element}"
    return found


def u16s(*values: int) -> list[U16]:
    """A span, a range or a trailer: index units."""
    return [U16(v) for v in values]


# ------------------------------------------------------------------------------ requests

def insert_of(request: DocsRequest) -> InsertTextRequest:
    found = request.get("insertText")
    assert found is not None, f"not an insertText: {request}"
    return found


def delete_of(request: DocsRequest) -> DeleteContentRangeRequest:
    found = request.get("deleteContentRange")
    assert found is not None, f"not a deleteContentRange: {request}"
    return found


def text_style_of(request: DocsRequest) -> UpdateTextStyleRequest:
    found = request.get("updateTextStyle")
    assert found is not None, f"not an updateTextStyle: {request}"
    return found


def paragraph_style_of(request: DocsRequest) -> UpdateParagraphStyleRequest:
    found = request.get("updateParagraphStyle")
    assert found is not None, f"not an updateParagraphStyle: {request}"
    return found


def bullets_of(request: DocsRequest) -> CreateParagraphBulletsRequest:
    found = request.get("createParagraphBullets")
    assert found is not None, f"not a createParagraphBullets: {request}"
    return found


def table_of(request: DocsRequest) -> InsertTableRequest:
    found = request.get("insertTable")
    assert found is not None, f"not an insertTable: {request}"
    return found


def table_row_of(request: DocsRequest) -> InsertTableRowRequest:
    found = request.get("insertTableRow")
    assert found is not None, f"not an insertTableRow: {request}"
    return found


def table_column_of(request: DocsRequest) -> InsertTableColumnRequest:
    found = request.get("insertTableColumn")
    assert found is not None, f"not an insertTableColumn: {request}"
    return found


def row_delete_of(request: DocsRequest) -> DeleteTableLineRequest:
    found = request.get("deleteTableRow")
    assert found is not None, f"not a deleteTableRow: {request}"
    return found


def image_of(request: DocsRequest) -> InsertInlineImageRequest:
    found = request.get("insertInlineImage")
    assert found is not None, f"not an insertInlineImage: {request}"
    return found


def person_of(request: DocsRequest) -> InsertPersonRequest:
    found = request.get("insertPerson")
    assert found is not None, f"not an insertPerson: {request}"
    return found


def date_of(request: DocsRequest) -> InsertDateRequest:
    found = request.get("insertDate")
    assert found is not None, f"not an insertDate: {request}"
    return found


def add_tab_of(request: DocsRequest) -> AddDocumentTabRequest:
    found = request.get("addDocumentTab")
    assert found is not None, f"not an addDocumentTab: {request}"
    return found


def create_range_of(request: DocsRequest) -> CreateNamedRangeRequest:
    found = request.get("createNamedRange")
    assert found is not None, f"not a createNamedRange: {request}"
    return found


def delete_range_of(request: DocsRequest) -> DeleteNamedRangeRequest:
    found = request.get("deleteNamedRange")
    assert found is not None, f"not a deleteNamedRange: {request}"
    return found


def inserts_in(requests: Sequence[DocsRequest]) -> list[InsertTextRequest]:
    return [found for r in requests if (found := r.get("insertText")) is not None]


def deletes_in(requests: Sequence[DocsRequest]) -> list[DeleteContentRangeRequest]:
    return [found for r in requests if (found := r.get("deleteContentRange")) is not None]


def text_styles_in(requests: Sequence[DocsRequest]) -> list[UpdateTextStyleRequest]:
    return [found for r in requests if (found := r.get("updateTextStyle")) is not None]


def paragraph_styles_in(requests: Sequence[DocsRequest]) -> list[UpdateParagraphStyleRequest]:
    return [found for r in requests if (found := r.get("updateParagraphStyle")) is not None]


def images_in(requests: Sequence[DocsRequest]) -> list[InsertInlineImageRequest]:
    return [found for r in requests if (found := r.get("insertInlineImage")) is not None]


Located = InsertTextRequest | InsertTableRequest | InsertInlineImageRequest


def location_of(request: Located) -> DocsLocation:
    location = request.get("location")
    assert location is not None, f"no location: {request}"
    return location


def index_of(request: Located) -> int:
    """Where an insert goes: its `location`'s index."""
    return location_of(request)["index"]


def image_width(image: InsertInlineImageRequest) -> float:
    """The width an inserted picture is given (`objectSize.width.magnitude`)."""
    size = image.get("objectSize")
    assert size is not None, f"a picture with no size: {image}"
    width = size.get("width")
    assert width is not None, f"a picture with no width: {image}"
    magnitude = width.get("magnitude")
    assert magnitude is not None, f"a width with no magnitude: {image}"
    return magnitude


def object_location(request: DocsRequest) -> DocsLocation | None:
    """Where a request that makes one index unit (a picture, a person, a date) puts it."""
    if (image := request.get("insertInlineImage")) is not None:
        return location_of(image)
    if (person := request.get("insertPerson")) is not None:
        return person["location"]
    if (date := request.get("insertDate")) is not None:
        return date["location"]
    return None
