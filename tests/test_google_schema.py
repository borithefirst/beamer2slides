"""google_types held to Google's own discovery documents (the ones the client library ships), field
by field: a request we build, a part it carries, an answer we read and the keywords of every call.

- Every TypedDict of google_types says here which schema (or which method's parameters) it is; one
  nobody mapped fails, so a type added there is a type checked here.
- Every field is a field of that schema, of its type: string -> str, integer -> int, number ->
  float, boolean -> bool, $ref -> the TypedDict mapped to that schema, array -> list/Sequence of
  the item's type, a map -> dict/Mapping of str to the value's type, enum -> a Literal of values
  the enum has. A string with no enum may be a narrower Literal (Drive's permission roles).
- `JsonObject` stands for a schema only in an answer, where nothing we write can reach it
  (a field Google documents as read-only is not reached through).
- A keyword Google requires is `Required`; `body` is the method's request schema, `media_body`
  only on a method that takes an upload.

What does not hold is exempted here by name with its reason (`EXEMPT`), and an exemption that is
no longer needed fails: the list is the whole of where our types and Google's part ways."""

from __future__ import annotations

import functools
import json
import typing
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Literal

import pytest

from beamer2slides import google_types
from beamer2slides.json_types import Json, JsonObject

from .json_reads import jobj

pytest.importorskip("googleapiclient")
pytest.importorskip("typing_extensions")

Api = Literal["slides", "docs", "drive"]
Schema = tuple[Api, str]

DOCUMENTS: dict[Api, str] = {"slides": "slides.v1.json", "docs": "docs.v1.json", "drive": "drive.v3.json"}


def _slides(*names: str) -> tuple[Schema, ...]:
    return tuple(("slides", n) for n in names)


def _docs(*names: str) -> tuple[Schema, ...]:
    return tuple(("docs", n) for n in names)


def _drive(*names: str) -> tuple[Schema, ...]:
    return tuple(("drive", n) for n in names)


#: Each TypedDict of google_types that is a schema: the schema(s) it is.
SCHEMAS: dict[str, tuple[Schema, ...]] = {
    # Slides: answers
    "Dimension": _slides("Dimension"), "Size": _slides("Size"), "AffineTransform": _slides("AffineTransform"),
    "PageElement": _slides("PageElement"), "SlideProperties": _slides("SlideProperties"),
    "LayoutProperties": _slides("LayoutProperties"), "Page": _slides("Page"),
    "Presentation": _slides("Presentation"), "WriteControl": _slides("WriteControl"),
    "BatchUpdateResponse": _slides("BatchUpdatePresentationResponse"), "Thumbnail": _slides("Thumbnail"),
    # Slides: parts
    "SlidesRgbColor": _slides("RgbColor"), "OpaqueColor": _slides("OpaqueColor"),
    "SlidesOptionalColor": _slides("OptionalColor"), "SolidFill": _slides("SolidFill"),
    "SlidesLink": _slides("Link"), "SlidesWeightedFontFamily": _slides("WeightedFontFamily"),
    "SlidesTextStyle": _slides("TextStyle"), "SlidesParagraphStyle": _slides("ParagraphStyle"),
    "SlidesBullet": _slides("Bullet"), "OutlineFill": _slides("OutlineFill"), "Outline": _slides("Outline"),
    "Shadow": _slides("Shadow"), "ShapeBackgroundFill": _slides("ShapeBackgroundFill"),
    "Autofit": _slides("Autofit"), "ShapeProperties": _slides("ShapeProperties"),
    "StretchedPictureFill": _slides("StretchedPictureFill"), "PageBackgroundFill": _slides("PageBackgroundFill"),
    "ThemeColorPair": _slides("ThemeColorPair"), "ColorScheme": _slides("ColorScheme"),
    "PageProperties": _slides("PageProperties"), "LineFill": _slides("LineFill"),
    "LineConnection": _slides("LineConnection"), "LineProperties": _slides("LineProperties"),
    "SlidesCropProperties": _slides("CropProperties"), "ColorStop": _slides("ColorStop"),
    "Recolor": _slides("Recolor"), "SlidesImageProperties": _slides("ImageProperties"),
    "TableCellBackgroundFill": _slides("TableCellBackgroundFill"),
    "TableCellProperties": _slides("TableCellProperties"), "TableBorderFill": _slides("TableBorderFill"),
    "TableBorderProperties": _slides("TableBorderProperties"), "TableRowProperties": _slides("TableRowProperties"),
    "SlidesTableColumnProperties": _slides("TableColumnProperties"),
    "SlidesTableCellLocation": _slides("TableCellLocation"), "SlidesTableRange": _slides("TableRange"),
    "SlidesRange": _slides("Range"), "SlidesPageElementProperties": _slides("PageElementProperties"),
    "Placeholder": _slides("Placeholder"), "LayoutReference": _slides("LayoutReference"),
    "LayoutPlaceholderIdMapping": _slides("LayoutPlaceholderIdMapping"),
    # Slides: requests
    "SlidesRequest": _slides("Request"),
    "DeleteObjectRequest": _slides("DeleteObjectRequest"),
    "UpdatePageElementTransformRequest": _slides("UpdatePageElementTransformRequest"),
    "UpdatePageElementsZOrderRequest": _slides("UpdatePageElementsZOrderRequest"),
    "UpdatePageElementAltTextRequest": _slides("UpdatePageElementAltTextRequest"),
    "UpdateSlidesPositionRequest": _slides("UpdateSlidesPositionRequest"),
    "CreateSlideRequest": _slides("CreateSlideRequest"), "CreateImageRequest": _slides("CreateImageRequest"),
    "CreateShapeRequest": _slides("CreateShapeRequest"), "CreateLineRequest": _slides("CreateLineRequest"),
    "CreateTableRequest": _slides("CreateTableRequest"), "DuplicateObjectRequest": _slides("DuplicateObjectRequest"),
    "UpdateImagePropertiesRequest": _slides("UpdateImagePropertiesRequest"),
    "UpdateLinePropertiesRequest": _slides("UpdateLinePropertiesRequest"),
    "UpdateSlidePropertiesRequest": _slides("UpdateSlidePropertiesRequest"),
    "UpdateTableBorderPropertiesRequest": _slides("UpdateTableBorderPropertiesRequest"),
    "UpdateTableCellPropertiesRequest": _slides("UpdateTableCellPropertiesRequest"),
    "UpdateTableRowPropertiesRequest": _slides("UpdateTableRowPropertiesRequest"),
    "SlidesUpdateTableColumnPropertiesRequest": _slides("UpdateTableColumnPropertiesRequest"),
    "SlidesCreateParagraphBulletsRequest": _slides("CreateParagraphBulletsRequest"),
    "SlidesDeleteParagraphBulletsRequest": _slides("DeleteParagraphBulletsRequest"),
    "SlidesMergeTableCellsRequest": _slides("MergeTableCellsRequest"),
    "SlidesReplaceImageRequest": _slides("ReplaceImageRequest"),
    "SlidesDeleteTextRequest": _slides("DeleteTextRequest"), "SlidesInsertTextRequest": _slides("InsertTextRequest"),
    "SlidesUpdateTextStyleRequest": _slides("UpdateTextStyleRequest"),
    "SlidesUpdateParagraphStyleRequest": _slides("UpdateParagraphStyleRequest"),
    "UpdatePagePropertiesRequest": _slides("UpdatePagePropertiesRequest"),
    "UpdateShapePropertiesRequest": _slides("UpdateShapePropertiesRequest"),
    "InsertTableRowsRequest": _slides("InsertTableRowsRequest"),
    "InsertTableColumnsRequest": _slides("InsertTableColumnsRequest"),
    "DeleteTableRowRequest": _slides("DeleteTableRowRequest"),
    "DeleteTableColumnRequest": _slides("DeleteTableColumnRequest"),
    "GroupObjectsRequest": _slides("GroupObjectsRequest"), "UngroupObjectsRequest": _slides("UngroupObjectsRequest"),
    # Slides: bodies
    "BatchUpdateBody": _slides("BatchUpdatePresentationRequest"), "NewPresentation": _slides("Presentation"),
    # Drive
    "DriveFile": _drive("File"), "FileList": _drive("FileList"), "FileBody": _drive("File"),
    "Permission": _drive("Permission"), "Comment": _drive("Comment"), "CommentList": _drive("CommentList"),
    "NewComment": _drive("Comment"), "DriveUser": _drive("User"), "Revision": _drive("Revision"),
    "RevisionList": _drive("RevisionList"), "RevisionBody": _drive("Revision"), "About": _drive("About"),
    # Docs: the document
    "DocsWriteControl": _docs("WriteControl"), "DocsDimension": _docs("Dimension"),
    "DocsRgbColor": _docs("RgbColor"), "DocsColor": _docs("Color"), "DocsOptionalColor": _docs("OptionalColor"),
    "DocsWeightedFontFamily": _docs("WeightedFontFamily"), "DocsLink": _docs("Link"),
    "DocsTextStyle": _docs("TextStyle"), "DocsParagraphBorder": _docs("ParagraphBorder"),
    "DocsShading": _docs("Shading"), "DocsParagraphStyle": _docs("ParagraphStyle"), "DocsTextRun": _docs("TextRun"),
    "DocsDateElementProperties": _docs("DateElementProperties"), "DocsDateElement": _docs("DateElement"),
    "DocsPersonProperties": _docs("PersonProperties"), "DocsPerson": _docs("Person"),
    "DocsRichLinkProperties": _docs("RichLinkProperties"), "DocsRichLink": _docs("RichLink"),
    "DocsFootnoteReference": _docs("FootnoteReference"), "DocsInlineObjectElement": _docs("InlineObjectElement"),
    "DocsParagraphElement": _docs("ParagraphElement"), "DocsBullet": _docs("Bullet"),
    "DocsParagraph": _docs("Paragraph"), "DocsTableCell": _docs("TableCell"),
    "DocsTableRowStyle": _docs("TableRowStyle"), "DocsTableRow": _docs("TableRow"),
    "DocsTableColumnProperties": _docs("TableColumnProperties"), "DocsTableStyle": _docs("TableStyle"),
    "DocsTable": _docs("Table"), "DocsStructuralElement": _docs("StructuralElement"), "DocsBody": _docs("Body"),
    "DocsNestingLevel": _docs("NestingLevel"), "DocsListProperties": _docs("ListProperties"),
    "DocsList": _docs("List"), "DocsSize": _docs("Size"), "DocsImageProperties": _docs("ImageProperties"),
    "DocsEmbeddedObject": _docs("EmbeddedObject"), "DocsInlineObjectProperties": _docs("InlineObjectProperties"),
    "DocsInlineObject": _docs("InlineObject"), "DocsNamedStyle": _docs("NamedStyle"),
    "DocsNamedStyles": _docs("NamedStyles"), "DocsRange": _docs("Range"), "DocsNamedRange": _docs("NamedRange"),
    "DocsNamedRanges": _docs("NamedRanges"), "DocsTabProperties": _docs("TabProperties"),
    "DocsDocumentTab": _docs("DocumentTab"), "DocsTab": _docs("Tab"), "DocsBackground": _docs("Background"),
    "DocsDocumentFormat": _docs("DocumentFormat"), "DocsDocumentStyle": _docs("DocumentStyle"),
    "DocsHeader": _docs("Header"), "DocsFooter": _docs("Footer"), "DocsFootnote": _docs("Footnote"),
    "DocsPositionedObjectPositioning": _docs("PositionedObjectPositioning"),
    "DocsPositionedObjectProperties": _docs("PositionedObjectProperties"),
    "DocsPositionedObject": _docs("PositionedObject"), "Document": _docs("Document"),
    # Docs: requests
    "DocsRequest": _docs("Request"),
    "DocsLocation": _docs("Location"), "DocsEndOfSegmentLocation": _docs("EndOfSegmentLocation"),
    "DocsRangeWrite": _docs("Range"), "DocsTabsCriteria": _docs("TabsCriteria"),
    "InsertTextRequest": _docs("InsertTextRequest"), "DeleteContentRangeRequest": _docs("DeleteContentRangeRequest"),
    "UpdateTextStyleRequest": _docs("UpdateTextStyleRequest"),
    "UpdateParagraphStyleRequest": _docs("UpdateParagraphStyleRequest"),
    "CreateParagraphBulletsRequest": _docs("CreateParagraphBulletsRequest"),
    "DeleteParagraphBulletsRequest": _docs("DeleteParagraphBulletsRequest"),
    "CreateNamedRangeRequest": _docs("CreateNamedRangeRequest"),
    "DeleteNamedRangeRequest": _docs("DeleteNamedRangeRequest"), "InsertTableRequest": _docs("InsertTableRequest"),
    "DocsTableCellLocation": _docs("TableCellLocation"), "InsertTableRowRequest": _docs("InsertTableRowRequest"),
    "InsertTableColumnRequest": _docs("InsertTableColumnRequest"),
    "DeleteTableLineRequest": _docs("DeleteTableRowRequest", "DeleteTableColumnRequest"),
    "DocsObjectSize": _docs("Size"), "InsertInlineImageRequest": _docs("InsertInlineImageRequest"),
    "InsertPersonRequest": _docs("InsertPersonRequest"), "InsertDateRequest": _docs("InsertDateRequest"),
    "AddDocumentTabRequest": _docs("AddDocumentTabRequest"),
    "UpdateDocumentTabPropertiesRequest": _docs("UpdateDocumentTabPropertiesRequest"),
    "DeleteTabRequest": _docs("DeleteTabRequest"), "CreateHeaderRequest": _docs("CreateHeaderRequest"),
    "CreateFooterRequest": _docs("CreateFooterRequest"), "CreateFootnoteRequest": _docs("CreateFootnoteRequest"),
    "InsertPageBreakRequest": _docs("InsertPageBreakRequest"),
    "InsertSectionBreakRequest": _docs("InsertSectionBreakRequest"),
    "InsertRichLinkRequest": _docs("InsertRichLinkRequest"),
    "UpdateDocumentStyleRequest": _docs("UpdateDocumentStyleRequest"),
    "DocsBatchUpdateResponse": _docs("BatchUpdateDocumentResponse"),
    "DocsBatchUpdateBody": _docs("BatchUpdateDocumentRequest"),
}

#: Each TypedDict of google_types that is the keywords of a call: the method(s) it is.
PARAMETERS: dict[str, tuple[Schema, ...]] = {
    "GetPresentation": _slides("presentations.get"), "CreatePresentation": _slides("presentations.create"),
    "UpdatePresentation": _slides("presentations.batchUpdate"),
    "GetThumbnail": _slides("presentations.pages.getThumbnail"), "GetPage": _slides("presentations.pages.get"),
    "ListFiles": _drive("files.list"), "CreateFile": _drive("files.create"),
    "FileId": _drive("files.delete", "files.get"),     # (get_media is files.get, downloading)
    "GetFile": _drive("files.get"), "UpdateFile": _drive("files.update"), "CopyFile": _drive("files.copy"),
    "ExportFile": _drive("files.export"),               # (export_media too)
    "CreatePermission": _drive("permissions.create"), "ListComments": _drive("comments.list"),
    "CreateComment": _drive("comments.create"), "ListRevisions": _drive("revisions.list"),
    "GetRevision": _drive("revisions.get"), "UpdateRevision": _drive("revisions.update"),
    "GetAbout": _drive("about.get"),
    "GetDocument": _docs("documents.get"), "UpdateDocument": _docs("documents.batchUpdate"),
}

#: TypedDicts that are ours, not Google's.
OURS: dict[str, str] = {
    "ExecuteOptions": "the library's `HttpRequest.execute` keyword, not the API's",
    "Empty": "what an answer of no body is to us (Drive's delete answers nothing)",
}

#: Where our types and Google's part ways, on purpose: (TypedDict, field) -> why.
EXEMPT: dict[tuple[str, str], str] = {
    ("InsertTableRowsRequest", "tableObjectId"):
        "None: a step planned before its table is named, filled in by sync.named_step before it is sent",
    ("InsertTableColumnsRequest", "tableObjectId"): "None: as insertTableRows (sync.table_steps)",
    ("DeleteTableRowRequest", "tableObjectId"): "None: as insertTableRows (sync.table_steps)",
    ("DeleteTableColumnRequest", "tableObjectId"): "None: as insertTableRows (sync.table_steps)",
    ("BatchUpdateBody", "requests"): "still any JSON request: emit's producers are converted in phase 2",
    ("FileBody", "appProperties"): "an appProperty set to None is removed (Drive's own convention)",
    ("ListFiles", "pageToken"): "None on the first page: the library leaves a None keyword out",
    ("ListComments", "pageToken"): "None on the first page, as files.list",
    ("ListRevisions", "pageToken"): "None on the first page, as files.list",
}


@functools.cache
def discovery(api: Api) -> JsonObject:
    text = (resources.files("googleapiclient") / "discovery_cache" / "documents" / DOCUMENTS[api]).read_text(
        encoding="utf-8")
    value: Json = json.loads(text)
    return jobj(value)


def schema(api: Api, name: str) -> JsonObject:
    return jobj(discovery(api), "schemas", name)


def method(api: Api, path: str) -> JsonObject:
    *resource_path, name = path.split(".")
    node = discovery(api)
    for resource in resource_path:
        node = jobj(node, "resources", resource)
    return jobj(node, "methods", name)


def typed_dicts() -> dict[str, type]:
    return {n: v for n, v in vars(google_types).items()
            if isinstance(v, type) and typing.is_typeddict(v) and v.__module__ == google_types.__name__}


def hints(td: type) -> dict[str, tuple[object, bool]]:
    """A TypedDict's fields as (type, required), read from its own source text: `Required` is
    imported for the checker alone, and `get_type_hints` would expand `JsonObject` past recognition."""
    from typing_extensions import NotRequired, Required, Unpack

    names: dict[str, object] = dict(vars(google_types))
    names.update(Required=Required, NotRequired=NotRequired, Unpack=Unpack)
    out: dict[str, tuple[object, bool]] = {}
    annotations: dict[str, object] = dict(td.__annotations__)
    for field, annotation in annotations.items():
        text = annotation.__forward_arg__ if isinstance(annotation, typing.ForwardRef) else annotation
        hint: object = eval(text, names) if isinstance(text, str) else text    # noqa: S307 (our own source)
        required = typing.get_origin(hint) is Required
        if required or typing.get_origin(hint) is NotRequired:
            hint = typing.get_args(hint)[0]
        out[field] = (hint, required)
    return out


def td_name(hint: object) -> str | None:
    if isinstance(hint, type) and typing.is_typeddict(hint):
        return hint.__name__
    return None


def show(hint: object) -> str:
    name = td_name(hint)
    return name if name is not None else repr(hint)


def disagreement(hint: object, prop: JsonObject, api: Api, json_allowed: bool) -> str | None:
    """How `hint` fails the discovery property `prop` (None: it agrees)."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    ref = prop.get("$ref")
    if isinstance(ref, str):
        name = td_name(hint)
        if name is not None:
            return None if (api, ref) in SCHEMAS.get(name, ()) else f"{name} is not mapped to {api}:{ref}"
        if hint == JsonObject and json_allowed:
            return None
        return f"{show(hint)} for {api}:{ref}" + ("" if json_allowed else " (reached by what we write)")
    kind = prop.get("type")
    enum = prop.get("enum")
    if kind == "array":
        if origin is not list and origin is not Sequence:
            return f"{show(hint)} for an array"
        return disagreement(args[0], jobj(prop, "items"), api, json_allowed)
    if kind == "object":
        if isinstance(prop.get("additionalProperties"), dict):
            if (origin is not dict and origin is not Mapping) or args[0] is not str:
                return f"{show(hint)} for a map of str"
            return disagreement(args[1], jobj(prop, "additionalProperties"), api, json_allowed)
        return None if hint == JsonObject else f"{show(hint)} for an object of no fields"
    if isinstance(enum, list):
        if origin is not Literal:
            return f"{show(hint)} for an enum: a Literal of its values"
        outside = [a for a in args if a not in enum]
        return f"{outside} are not values of the enum" if outside else None
    if kind == "string":
        return None if hint is str or (origin is Literal and all(isinstance(a, str) for a in args)) \
            else f"{show(hint)} for a string"
    if kind == "integer":
        return None if hint is int else f"{show(hint)} for an integer"
    if kind == "number":
        return None if hint is float else f"{show(hint)} for a number"
    if kind == "boolean":
        return None if hint is bool else f"{show(hint)} for a boolean"
    if kind == "any":
        return None if hint == Json else f"{show(hint)} for any JSON"
    return f"a discovery type {kind!r} this test does not know"


def read_only(prop: JsonObject) -> bool:
    if prop.get("readOnly") is True:
        return True
    said = prop.get("description")
    text = said.lower() if isinstance(said, str) else ""
    return "output only" in text or "read-only" in text or "read only" in text


def reached_by_writes() -> set[str]:
    """The TypedDicts something we write can hold: the requests and the bodies of calls, and every
    TypedDict their writable fields reach."""
    tds = typed_dicts()
    todo = ["SlidesRequest", "DocsRequest"]
    for name in PARAMETERS:
        body = hints(tds[name]).get("body")
        found = td_name(body[0]) if body is not None else None
        if found is not None:
            todo.append(found)
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for field, (hint, _) in hints(tds[name]).items():
            props = [jobj(schema(api, s), "properties").get(field) for api, s in SCHEMAS.get(name, ())]
            if any(isinstance(p, dict) and read_only(p) for p in props):
                continue
            todo += [n for n in (td_name(h) for h in _inner(hint)) if n is not None]
    return seen


def _inner(hint: object) -> list[object]:
    """`hint` and every type inside it (a list's items, a map's values, a union's arms)."""
    out = [hint]
    for arg in typing.get_args(hint):
        out += _inner(arg)
    return out


def field_problems(name: str, field: str, hint: object, written: bool) -> list[str]:
    problems: list[str] = []
    for api, s in SCHEMAS[name]:
        prop = jobj(schema(api, s), "properties").get(field)
        if not isinstance(prop, dict):
            problems.append(f"{name}.{field}: no field of {api}:{s}")
            continue
        wrong = disagreement(hint, jobj(prop), api, not written)
        if wrong is not None:
            problems.append(f"{name}.{field} ({api}:{s}): {wrong}")
    return problems


def parameter_problems(name: str, field: str, hint: object, required: bool) -> list[str]:
    problems: list[str] = []
    for api, path in PARAMETERS[name]:
        m = method(api, path)
        params: JsonObject = jobj(m, "parameters") if isinstance(m.get("parameters"), dict) else {}
        by_keyword = {k.replace(".", "_"): k for k in params}
        if field == "body":
            request = m.get("request")
            ref = jobj(request).get("$ref") if isinstance(request, dict) else None
            body = td_name(hint)
            if not isinstance(ref, str) or body is None or (api, ref) not in SCHEMAS.get(body, ()):
                problems.append(f"{name}.body: {show(hint)} for {api} {path}'s request {ref}")
            continue
        if field == "media_body":
            if m.get("supportsMediaUpload") is not True or hint is not google_types.MediaBody:
                problems.append(f"{name}.media_body: {api} {path} takes no upload, or it is no MediaBody")
            continue
        if field in by_keyword:
            prop = jobj(params, by_keyword[field])
            if prop.get("required") is True and not required:
                problems.append(f"{name}.{field}: {api} {path} requires it")
        elif field in jobj(discovery(api), "parameters"):
            prop = jobj(discovery(api), "parameters", field)
        else:
            problems.append(f"{name}.{field}: no parameter of {api} {path}")
            continue
        wrong = disagreement(hint, prop, api, False)
        if wrong is not None:
            problems.append(f"{name}.{field} ({api} {path}): {wrong}")
    return problems


def all_problems() -> dict[tuple[str, str], list[str]]:
    """Every field's disagreements with Google, exemptions not yet applied."""
    tds = typed_dicts()
    written = reached_by_writes()
    out: dict[tuple[str, str], list[str]] = {}
    for name in sorted(tds):
        fields = hints(tds[name])
        if name in PARAMETERS:
            for field, (hint, required) in fields.items():
                out[(name, field)] = parameter_problems(name, field, hint, required)
            for api, path in PARAMETERS[name]:
                params = method(api, path).get("parameters")
                for keyword, prop in (params.items() if isinstance(params, dict) else ()):
                    if isinstance(prop, dict) and prop.get("required") is True and keyword.replace(".", "_") not in fields:
                        out.setdefault((name, keyword), []).append(f"{name}: {api} {path} requires {keyword}")
        elif name in SCHEMAS:
            for field, (hint, _) in fields.items():
                out[(name, field)] = field_problems(name, field, hint, name in written)
    return out


def test_every_typed_dict_is_mapped() -> None:
    unmapped = sorted(n for n in typed_dicts() if n not in SCHEMAS and n not in PARAMETERS and n not in OURS)
    assert unmapped == []
    stale = sorted(n for n in (*SCHEMAS, *PARAMETERS, *OURS) if n not in typed_dicts())
    assert stale == []


def test_every_mapped_schema_and_method_exists() -> None:
    missing = [f"{n}: {api}:{s}" for n, where in SCHEMAS.items() for api, s in where
               if not isinstance(jobj(discovery(api), "schemas").get(s), dict)]
    for n, where in PARAMETERS.items():
        for api, path in where:
            try:
                method(api, path)
            except (KeyError, ValueError, AssertionError):
                missing.append(f"{n}: {api} {path}")
    assert missing == []


def test_every_field_is_google_s_field_of_google_s_type() -> None:
    found = {k: v for k, v in all_problems().items() if v and k not in EXEMPT}
    assert found == {}


def test_every_exemption_is_still_needed() -> None:
    problems = all_problems()
    stale = sorted(k for k in EXEMPT if not problems.get(k))
    assert stale == []


def test_a_request_kind_is_google_s_request_kind() -> None:
    """The oneofs, key by key: every kind we name is a kind Google has, of the request it says."""
    assert set(google_types.SLIDES_REQUEST_KINDS) <= set(jobj(schema("slides", "Request"), "properties"))
    assert set(google_types.DOCS_REQUEST_KINDS) <= set(jobj(schema("docs", "Request"), "properties"))


def test_the_library_s_clients_have_every_method_we_call() -> None:
    """`gapi.build` checks the top of each client against its Protocol; this walks every
    collection below it, on clients built from the same discovery documents (no network)."""
    from googleapiclient.discovery import build_from_document
    from googleapiclient.http import HttpMock

    services: dict[Api, type] = {"slides": google_types.SlidesService, "drive": google_types.DriveService,
                                 "docs": google_types.DocsService}
    missing: list[str] = []
    for api, protocol in services.items():
        client: object = build_from_document(json.dumps(discovery(api)), http=HttpMock())
        missing += _missing_methods(client, protocol, api)
    assert missing == []


def _missing_methods(client: object, protocol: type, where: str) -> list[str]:
    from typing_extensions import Unpack

    missing: list[str] = []
    for name, member in vars(protocol).items():
        if name.startswith("_") or not callable(member):
            continue
        found = getattr(client, name, None)
        if not callable(found):
            missing.append(f"{where}.{name}")
            continue
        returns = typing.get_type_hints(member, localns={"Unpack": Unpack}).get("return")
        if isinstance(returns, type) and returns is not type(None) and typing.get_origin(returns) is None \
                and getattr(returns, "_is_protocol", False):
            missing += _missing_methods(found(), returns, f"{where}.{name}()")
    return missing
