"""raw.json becomes a page only through its parser (`raw_types.parse_raw` / `parse_page`): every key
is checked for presence and type, optional ones too, and a refusal names the path it failed at.
What extract writes parses back to itself, byte for byte once dumped."""

import copy
import json
import re
from pathlib import Path

import pytest

from beamer2slides.classify import classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.json_types import JsonShapeError
from beamer2slides.raw_types import parse_page, parse_raw

from .test_marks import FONT, element, paragraph, text

DECKS = Path(__file__).resolve().parent / "decks" / "out"


def span(sid: str) -> dict:
    return {"id": sid, "text": "Hi", "font": "Helvetica", "size": 12, "color": "#000000", "alpha": 255,
            "origin": [10, 100], "bbox": [10, 90, 30, 103], "dir": [1, 0], "smallcaps": False}


def drawing(did: str) -> dict:
    return {"id": did, "type": "f", "items": "re", "bbox": [10, 10, 30, 30], "fill": "#ff0000", "stroke": None,
            "width": None, "fill_opacity": 1, "stroke_opacity": 1, "soft_mask": False, "corners": {},
            "path": [["re", [[10, 10], [30, 30]]]]}


def raw_file() -> dict:
    """A raw.json with one of everything, as extract writes it."""
    page = {"index": 0, "label": "1", "size": [362.83, 272.13],
            "spans": [span("p0s0"), {**span("p0s1"), "marks": [["B2S", {"k": "a", "t": "text", "n": 1}]]}],
            "images": [{"id": "p0i0", "bbox": [0, 0, 10, 10], "px": [20, 20]}],
            "drawings": [drawing("p0d0")],
            "links": [{"bbox": [0, 0, 5, 5], "uri": "https://example.com"}, {"bbox": [5, 5, 9, 9], "page": 2}],
            "hidden_text": ["later"], "frame_label": None, "notes": "say hi"}
    return {"version": 1, "source": {"pdf": "talk.pdf", "producer": "pdfTeX", "pages": 3, "title": ""},
            "pages": [page, {**copy.deepcopy(page), "index": 1}], "overlays": {"mode": "last", "dropped": 1}}


def refused(raw: dict, message: str) -> None:
    with pytest.raises(JsonShapeError, match=re.escape(message)):
        parse_raw(raw, "raw.json")


def test_a_whole_file_parses_to_itself():
    raw = raw_file()
    assert json.dumps(parse_raw(raw, "raw.json")) == json.dumps(raw)


def test_a_missing_key_is_named_with_its_path():
    raw = raw_file()
    del raw["pages"][1]["spans"][1]["bbox"]
    refused(raw, "raw.json: pages[1].spans[1]: the key 'bbox' is missing")
    raw = raw_file()
    del raw["pages"][0]["drawings"][0]["soft_mask"]  # (every raw.json written before 7486d64, 2026-09-24)
    refused(raw, "raw.json: pages[0].drawings[0]: the key 'soft_mask' is missing")
    raw = raw_file()
    del raw["source"]["title"]
    refused(raw, "raw.json: source: the key 'title' is missing")


def test_a_value_of_another_type_is_named_with_its_path():
    raw = raw_file()
    raw["pages"][1]["spans"][0]["bbox"] = [10, 90, 30]
    refused(raw, "raw.json: pages[1].spans[0].bbox: 4 numbers were expected, found 3")
    raw = raw_file()
    raw["pages"][0]["spans"][0]["bbox"][2] = "30"
    refused(raw, "raw.json: pages[0].spans[0].bbox[2]: a number was expected")
    raw = raw_file()
    raw["pages"][0]["drawings"][0]["type"] = "n"
    refused(raw, "raw.json: pages[0].drawings[0].type: 'f', 's' or 'fs' was expected")
    raw = raw_file()
    raw["pages"][0]["drawings"][0]["fill"] = "red"
    refused(raw, "raw.json: pages[0].drawings[0].fill: a colour '#rrggbb' was expected")
    raw = raw_file()
    raw["pages"][0]["spans"][1]["marks"][0][1]["n"] = 1.5
    refused(raw, "raw.json: pages[0].spans[1].marks[0][1].n")


def test_optional_keys_are_checked_too():
    raw = raw_file()
    raw["pages"][0]["hidden_text"] = [3]
    refused(raw, "raw.json: pages[0].hidden_text[0]")
    raw = raw_file()
    raw["pages"][0]["notes"] = 7
    refused(raw, "raw.json: pages[0].notes")
    raw = raw_file()
    raw["overlays"]["dropped"] = "1"
    refused(raw, "raw.json: overlays.dropped")


def test_a_key_the_type_does_not_have_is_refused():
    """(An image's `xref`: PyMuPDF's, before 55da0be; a raw.json that old is read again by classify,
    not upgraded.)"""
    raw = raw_file()
    raw["pages"][0]["images"][0]["xref"] = 12
    refused(raw, "raw.json: pages[0].images[0].xref: no such key in raw.json")


def test_a_link_has_a_uri_or_a_page_one_of_the_two():
    raw = raw_file()
    raw["pages"][0]["links"][0]["page"] = 1
    refused(raw, "raw.json: pages[0].links[0]: a link has a 'uri' or a 'page', one of the two")
    raw = raw_file()
    del raw["pages"][0]["links"][1]["page"]
    refused(raw, "raw.json: pages[0].links[1]: a link has a 'uri' or a 'page', one of the two")


def test_a_page_alone_names_where_it_came_from():
    with pytest.raises(JsonShapeError, match=re.escape("deck: pages[4].label")):
        parse_page({**raw_file()["pages"][0], "label": None}, "deck: pages[4]")


def test_a_marked_page_parses_back_to_what_extract_made(tmp_path):
    """Marks and path pieces are pairs in JSON and tuples in memory: the parser gives back the
    tuples extract builds."""
    from beamer2slides.devtools.render_torture import MEDIA, pdf_bytes
    page = element(b"a", b"text", paragraph(0, text(10, 100, b"Left"))) + \
        element(b"s", b"shape", b"1 0 0 rg 10 10 20 20 re f") + text(10, 50, b"Loose")
    path = tmp_path / "p.pdf"
    path.write_bytes(pdf_bytes([page], (), MEDIA, FONT, b""))
    raw = extract(path, None)
    assert parse_raw(json.loads(json.dumps(raw)), "p.pdf") == raw


@pytest.mark.needs_decks("out/04_theme_blocks.pdf", "out/05_overlays_notes.pdf", "out/07_images-handout.pdf",
                         "out/17_transparent_overlays.pdf")
@pytest.mark.parametrize("name", ["04_theme_blocks", "05_overlays_notes", "07_images-handout",
                                  "17_transparent_overlays"])
def test_raw_json_of_a_built_deck_reads_back_byte_for_byte(name):
    """Shadow pieces, soft masks, hidden words, overlays, raster images: the file as `classify`
    writes it, parsed, dumps to the same bytes and classifies the same."""
    raw = select_overlays(extract(DECKS / f"{name}.pdf", None), "last")
    text = json.dumps(raw, indent=1, ensure_ascii=False)
    parsed = parse_raw(json.loads(text), f"{name}/raw.json")
    assert json.dumps(parsed, indent=1, ensure_ascii=False) == text
    assert json.dumps(classify(parsed)) == json.dumps(classify(raw))
