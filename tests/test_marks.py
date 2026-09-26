"""Adopted pages say what adopt wrote: slides.sty opens a /B2S marked-content sequence around every
element it draws (its key and kind), a /B2Sp one around each paragraph, and the read-back builds
the page's elements from them instead of guessing (docs/adopt-bench.md, 2026-09-26).

Offline: the PDFs here are written by hand (`render_torture.pdf_bytes`), as lualatex writes
them, so no compile is needed. What the backends answer for marks is tests/test_pdf_backend.py's."""

import json

from beamer2slides import extract
from beamer2slides.devtools.render_torture import pdf_bytes

FONT = b" /Font << /F0 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"


def text(x: float, y: float, words: bytes, size: int = 12) -> bytes:
    return b"BT /F0 %d Tf %g %g Td (%s) Tj ET " % (size, x, y, words)


def element(key: bytes, kind: bytes, body: bytes) -> bytes:
    return b"/B2S <</k (%s) /t (%s)>> BDC %s EMC " % (key, kind, body)


def paragraph(i: int, body: bytes) -> bytes:
    return b"/B2Sp <</i %d>> BDC %s EMC " % (i, body)


def raw_of(tmp_path, pages: list[bytes], forms=(), name="p.pdf") -> dict:
    path = tmp_path / name
    path.write_bytes(pdf_bytes(pages, forms, extra_resources=FONT))
    return extract.extract(path)


def test_spans_carry_their_marks_and_never_cross_one(tmp_path):
    """Two boxes whose words touch on one baseline stay two spans, each with its element's and
    paragraph's marks; words outside any mark carry none."""
    page = (element(b"a", b"text", paragraph(0, text(10, 100, b"Left"))) +
            element(b"b", b"text", paragraph(0, text(36, 100, b"Right"))) +
            text(10, 50, b"Loose"))
    spans = raw_of(tmp_path, [page])["pages"][0]["spans"]
    got = {s["text"]: s.get("marks") for s in spans}
    assert got == {"Left": [["B2S", {"k": "a", "t": "text"}], ["B2Sp", {"i": 0}]],
                   "Right": [["B2S", {"k": "b", "t": "text"}], ["B2Sp", {"i": 0}]],
                   "Loose": None}


def test_drawings_and_form_children_carry_marks(tmp_path):
    """A path in a mark carries it; what a form draws is inside the marks around the form's Do;
    a tagged PDF's own marks (/P, /Span) are none of ours."""
    form = (b"/BBox [0 0 200 150]", b"0 1 0 rg 60 60 10 10 re f")
    page = (element(b"s1", b"shape", b"1 0 0 rg 10 10 20 20 re f") +
            element(b"pic", b"picture", b"/X0 Do") +
            b"/P <</MCID 0>> BDC 0 0 1 rg 100 10 20 20 re f EMC")
    drawings = raw_of(tmp_path, [page], [form])["pages"][0]["drawings"]
    marks = [d.get("marks") for d in sorted(drawings, key=lambda d: d["bbox"][0])]
    assert marks == [[["B2S", {"k": "s1", "t": "shape"}]], [["B2S", {"k": "pic", "t": "picture"}]], None]


def test_adopt_names_each_mark_after_its_deck_object():
    """slides-keys.tex lists, frame by frame, the objectId behind each mark in drawing order: a text
    box's panel is `<id>+shape`, a layout's object `layout/object`, an id TeX cannot carry nothing."""
    from beamer2slides import adopt
    assert adopt.mark_key("layout~p3_i2") == "layout/p3_i2"
    assert adopt.mark_key("has space") is None
    box = {"id": "g1_0_5", "kind": "text"}
    panelled = "\\slideshape{rect}{..}\n\\begin{slidebox}{..}"
    assert adopt.piece_keys(box, panelled) == ["g1_0_5+shape", "g1_0_5"]
    assert adopt.piece_keys({"id": "p2", "kind": "image"}, "\\slidepicture{..}") == ["p2"]
    assert adopt.piece_keys({"id": "a b", "kind": "shape"}, "\\sliderect{..}") == [""]

    class Plan:  # the layout draws the logo and the title, at shipout: after the frame's own
        layout, drawn = "title-only", {0, 1}
    title = {"id": "t0", "kind": "text", "placeholder": "TITLE"}
    target = {"slides": [{"layout": "L", "elements": [title, {"id": "logo", "kind": "image", "inherited": "L"},
                                                      box, {"id": "t", "kind": "table"}]},
                         {"elements": [{"id": None, "kind": "text"}]}]}
    pieces = [["\\begin{slidebox}", "\\slidepicture{..}", panelled, "\\begin{slidetable}"], ["\\begin{slidebox}"]]
    got = adopt.keys_file(target, pieces, [Plan(), None], ["s1", "s2"]).splitlines()
    assert [line for line in got if not line.startswith("%")] == ["\\slidekeys{s1}{g1_0_5+shape,g1_0_5,t,logo,t0}"]


def test_a_page_without_marks_extracts_as_before(tmp_path):
    """Marks add a key and split spans at their edges, nothing else: the same page with one mark
    around everything reads the same once the key is taken away."""
    body = text(10, 100, b"Hello world") + b"1 0 0 rg 10 10 20 20 re f " + text(10, 60, b"Again")
    plain = raw_of(tmp_path, [body], name="plain.pdf")["pages"][0]
    marked = raw_of(tmp_path, [element(b"all", b"text", body)], name="marked.pdf")["pages"][0]
    assert all("marks" not in item for kind in ("spans", "drawings", "images") for item in plain[kind])
    for kind in ("spans", "drawings"):
        for item in marked[kind]:
            assert item.pop("marks") == [["B2S", {"k": "all", "t": "text"}]]
    assert json.dumps(marked, sort_keys=True) == json.dumps(plain, sort_keys=True)
