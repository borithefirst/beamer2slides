"""google_types' own promises, offline and without the client library: the request kinds are
listed once each and agree with the oneofs, a request is exactly one kind, and a part read back
from Google is parsed all the way down or refused with the place it went wrong."""

from __future__ import annotations

import io
import typing
from typing import Protocol, runtime_checkable

import pytest

from beamer2slides import gapi, google_types
from beamer2slides.google_types import (DOCS_REQUEST_KINDS, SLIDES_REQUEST_KINDS, DocsRequest, DocsRequestKind,
                                        SlidesRequest, SlidesRequestKind)
from beamer2slides.json_types import JsonObject, JsonShapeError


@runtime_checkable
class Resumable(Protocol):
    """The library's upload, as far as it says whether it goes up in chunks."""

    def resumable(self) -> bool: ...


def test_the_slides_kinds_are_the_oneof_s_keys() -> None:
    assert len(set(SLIDES_REQUEST_KINDS)) == len(SLIDES_REQUEST_KINDS)
    assert set(SLIDES_REQUEST_KINDS) == set(SlidesRequest.__annotations__)
    assert set(SLIDES_REQUEST_KINDS) == set(typing.get_args(SlidesRequestKind))


def test_the_docs_kinds_are_the_oneof_s_keys() -> None:
    assert len(set(DOCS_REQUEST_KINDS)) == len(DOCS_REQUEST_KINDS)
    assert set(DOCS_REQUEST_KINDS) == set(DocsRequest.__annotations__)
    assert set(DOCS_REQUEST_KINDS) == set(typing.get_args(DocsRequestKind))


def test_a_slides_request_is_exactly_one_kind() -> None:
    assert google_types.slides_request_kind({"deleteObject": {"objectId": "x"}}) == "deleteObject"
    with pytest.raises(ValueError, match="0 kinds"):
        google_types.slides_request_kind({})
    with pytest.raises(ValueError, match="2 kinds"):
        google_types.slides_request_kind({"deleteObject": {"objectId": "x"},
                                          "ungroupObjects": {"objectIds": ["g"]}})


def test_a_read_back_style_is_parsed_all_the_way_down() -> None:
    style: JsonObject = {"bold": True, "fontSize": {"magnitude": 12, "unit": "PT"},
                         "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 1.0}}},
                         "weightedFontFamily": {"fontFamily": "Lato", "weight": 400}}
    parsed = google_types.slides_text_style(style, "run")
    assert parsed is style        # (checked, not copied: what is sent is what was read)


@pytest.mark.parametrize(("style", "said"), [
    ({"bold": 1}, "run.bold: a bool"),
    ({"fontSize": {"magnitude": "12"}}, "run.fontSize.magnitude: a number"),
    ({"foregroundColor": {"opaqueColor": {"themeColor": "DARK9"}}},
     "run.foregroundColor.opaqueColor.themeColor: 'DARK9' is none of"),
    ({"weightedFontFamily": {"fontFamily": None, "weight": 400}}, "run.weightedFontFamily.fontFamily: a str"),
    ({"sparkle": True}, "'sparkle' is no field of SlidesTextStyle"),
    ({"link": ["x"]}, "run.link: a SlidesLink object"),
])
def test_a_style_google_would_refuse_is_refused_where_it_goes_wrong(style: JsonObject, said: str) -> None:
    with pytest.raises(JsonShapeError, match=said.replace("(", r"\(").replace(".", r"\.")):
        google_types.slides_text_style(style, "run")


def test_the_other_parts_parse_by_their_own_fields() -> None:
    props: JsonObject = {"shapeBackgroundFill": {"solidFill": {"color": {"themeColor": "ACCENT1"}, "alpha": 1}},
                         "outline": {"propertyState": "NOT_RENDERED", "dashStyle": "DOT"}}
    assert google_types.shape_properties(props, "shape") is props
    page: JsonObject = {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": "https://x"}}}
    assert google_types.page_properties(page, "page") is page
    para: JsonObject = {"alignment": "JUSTIFIED", "lineSpacing": 115, "direction": "RIGHT_TO_LEFT"}
    assert google_types.slides_paragraph_style(para, "para") is para
    mapping: JsonObject = {"layoutPlaceholder": {"type": "TITLE", "index": 0}, "objectId": "t"}
    assert google_types.layout_placeholder_id_mapping(mapping, "ph") is mapping
    with pytest.raises(JsonShapeError, match=r"ph\.layoutPlaceholder\.type"):
        google_types.layout_placeholder_id_mapping({"layoutPlaceholder": {"type": "HEADLINE"}}, "ph")
    with pytest.raises(JsonShapeError, match=r"page\.pageBackgroundFill\.propertyState"):
        google_types.page_properties({"pageBackgroundFill": {"propertyState": "HIDDEN"}}, "page")


def test_a_request_with_required_fields_parses_too() -> None:
    """`Required` is imported for the checker alone; the walker reads a required field as its type."""
    ranged: JsonObject = {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 3}
    assert google_types.typed_part(ranged, google_types.SlidesRange, "range") is ranged
    with pytest.raises(JsonShapeError, match=r"range\.type"):
        google_types.typed_part({"type": "SOME"}, google_types.SlidesRange, "range")


def test_a_shape_type_is_one_slides_has() -> None:
    assert google_types.shape_type("ROUND_2_SAME_RECTANGLE", "preset") == "ROUND_2_SAME_RECTANGLE"
    with pytest.raises(JsonShapeError, match="preset: 'BENT_CONNECTOR' is no Slides shape type"):
        google_types.shape_type("BENT_CONNECTOR", "preset")


def test_a_resumable_upload_is_the_library_s_resumable_media() -> None:
    pytest.importorskip("googleapiclient")
    body = gapi.resumable_media_upload(io.BytesIO(b"PK\x03\x04 a deck"), "application/zip")
    once = gapi.media_upload(io.BytesIO(b"PK\x03\x04 a deck"), "application/zip")
    assert body.mimetype() == once.mimetype() == "application/zip"
    assert body.size() == once.size() == 11
    assert body.getbytes(0, 4) == b"PK\x03\x04"
    assert isinstance(body, Resumable) and isinstance(once, Resumable)
    assert body.resumable() is True and once.resumable() is False
