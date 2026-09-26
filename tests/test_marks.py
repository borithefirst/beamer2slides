"""Adopted pages say what adopt wrote: slides.sty opens a /B2S marked-content sequence around every
element it draws (its key and kind), a /B2Sp one around each paragraph, and the read-back builds
the page's elements from them instead of guessing (docs/adopt-bench.md, 2026-09-26).

Offline: the PDFs here are written by hand (`render_torture.pdf_bytes`), as lualatex writes
them, so no compile is needed. What the backends answer for marks is tests/test_pdf_backend.py's."""

import copy
import json

import pytest

from beamer2slides import classify, extract, pdf
from beamer2slides.devtools.render_torture import pdf_bytes

FONT = b" /Font << /F0 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"


def text(x: float, y: float, words: bytes, size: int = 12) -> bytes:
    return b"BT /F0 %d Tf %g %g Td (%s) Tj ET " % (size, x, y, words)


def element(key: bytes, kind: bytes, body: bytes, more: bytes = b"") -> bytes:
    return b"/B2S <</k (%s) /t (%s)%s>> BDC %s EMC " % (key, kind, more, body)


def paragraph(i: int, body: bytes, more: bytes = b"") -> bytes:
    return b"/B2Sp <</i %d%s>> BDC %s EMC " % (i, more, body)


@pytest.fixture(params=["pdfium", "pure"])
def backend(request):
    """Every PDF here is read by both backends: marks are part of the backend contract."""
    with pdf.use_backend(request.param) as b:
        yield b


def raw_of(tmp_path, pages: list[bytes], forms=(), name="p.pdf") -> dict:
    path = tmp_path / name
    path.write_bytes(pdf_bytes(pages, forms, extra_resources=FONT))
    return extract.extract(path)


def read_back(tmp_path, page: bytes) -> dict:
    """The page's slide as the read-back of an adopted PDF gets it (`classify.classify_page`)."""
    raw = raw_of(tmp_path, [page])
    return classify.classify_page(raw["pages"][0], classify.body_size(raw))


def texts(slide: dict) -> dict:
    """mark -> the text of each paragraph of that text element."""
    return {e.get("mark"): ["".join(r["text"] for r in p["runs"]) for p in e["paragraphs"]]
            for e in slide["elements"] if e["kind"] == "text"}


def test_spans_carry_their_marks_and_never_cross_one(tmp_path, backend):
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


def test_drawings_and_form_children_carry_marks(tmp_path, backend):
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


def test_a_page_without_marks_extracts_as_before(tmp_path, backend):
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


# ------------------------------------------------------------------------------ the marked read-back

INLINE_IMAGE = b"BI /W 2 /H 2 /CS /RGB /BPC 8 ID " + b"\x80" * 12 + b"\nEI "


def test_stacked_boxes_stay_two_boxes(tmp_path, backend):
    """Two boxes one line pitch apart, flush left in one font: to the classifier one box of two
    paragraphs; their marks say two boxes, each with its words."""
    page = (element(b"a", b"text", paragraph(0, text(20, 100, b"First box words"))) +
            element(b"b", b"text", paragraph(0, text(20, 86, b"Second box words"))))
    slide = read_back(tmp_path, page)
    assert slide["marked"] == 2
    assert texts(slide) == {"a": ["First box words"], "b": ["Second box words"]}


def test_a_box_over_a_full_page_picture_is_not_swallowed(tmp_path, backend):
    """A picture filling the page and a box over it: to the classifier the picture is background;
    its mark makes it the deck's picture, alone (its image, no words), and the box stays text."""
    page = (element(b"bg", b"picture", b"q 200 0 0 150 0 0 cm " + INLINE_IMAGE + b"Q ") +
            element(b"t", b"text", paragraph(0, text(30, 70, b"Words over the photo"))))
    slide = read_back(tmp_path, page)
    pictures = [e for e in slide["elements"] if e["kind"] == "image"]
    assert [(e["mark"], e["spans"]) for e in pictures] == [("bg", [])]
    assert [round(v) for v in pictures[0]["bbox"]] == [0, 0, 200, 150]
    assert texts(slide) == {"t": ["Words over the photo"]}


def test_a_wrapped_paragraph_is_one_paragraph_with_its_alignment(tmp_path, backend):
    """What one paragraph mark holds is one paragraph however many lines it wrapped to, aligned as
    the mark says; the next mark is the next paragraph, even flush under it."""
    first = text(20, 110, b"a paragraph that wraps") + text(20, 96, b"onto a second line")
    page = element(b"w", b"text", paragraph(0, first, b" /a (center)") + paragraph(1, text(20, 82, b"Next one")))
    slide = read_back(tmp_path, page)
    assert texts(slide) == {"w": ["a paragraph that wraps onto a second line", "Next one"]}
    box = next(e for e in slide["elements"] if e.get("mark") == "w")
    assert [p["align"] for p in box["paragraphs"]] == ["center", "left"]


def test_shapes_take_the_box_adopt_drew_them_in(tmp_path, backend):
    """A shape is its box (`/box`), not its ink, when the ink fits in it: a curve or a page-cut
    shape falls short of it. An open stroke with a small head is a line: its shaft's box."""
    page = (element(b"s", b"shape", b"1 0 0 rg 12 102 m 58 102 l 58 128 l 12 128 l h f", b" /box (10 20 50 30)") +
            element(b"l", b"shape", b"0 0 0 RG 1 w 20 20 m 100 60 l S 0 0 0 rg 100 60 m 94 60 l 97 55 l h f"))
    shapes = {e["mark"]: e for e in read_back(tmp_path, page)["elements"] if e["kind"] == "shape"}
    assert (shapes["s"]["role"], shapes["s"]["bbox"], shapes["s"]["fill"]) == ("panel", [10, 20, 60, 50], "#ff0000")
    assert shapes["l"]["role"] == "line" and shapes["l"]["fill"] is None
    assert [round(v) for v in shapes["l"]["bbox"]] == [20, 90, 100, 130]


def test_a_line_runs_to_the_points_its_mark_says(tmp_path, backend):
    """TikZ stops an arrow's shaft at its head's base; `\\slideline`'s mark says where the line
    ends (its arrow's tip), and so where the deck's line ends."""
    shaft_and_head = b"0 0 0 RG 1 w 20 130 m 20 66 l S 0 0 0 rg 17 66 m 23 66 l 20 60 l h f"
    page = element(b"l", b"shape", shaft_and_head, b" /box (20bp 20bp 0.01bp 0.01bp) /line (20 20 20 90)")
    line = next(e for e in read_back(tmp_path, page)["elements"] if e.get("mark") == "l")
    assert (line["role"], line["bbox"]) == ("line", [20, 20, 20, 90])


def test_underlined_words_are_underlined_whatever_their_rules(tmp_path, backend):
    """ulem's rules end under a word inside a span; `\\uline`'s mark cuts the span there and says
    the words are underlined."""
    words = b"/B2Su BMC " + text(20, 100, b"Insert Line:") + b"EMC " + text(88, 100, b"click here")
    page = element(b"u", b"text", paragraph(0, words))
    box = next(e for e in read_back(tmp_path, page)["elements"] if e.get("mark") == "u")
    runs = [(r["text"].strip(), r["underline"]) for r in box["paragraphs"][0]["runs"] if r["text"].strip()]
    assert runs == [("Insert Line:", True), ("click here", False)]


def test_a_page_without_marks_is_classified_as_before(tmp_path, backend):
    """No mark, no marked read-back: the slide is `PageClassifier`'s."""
    raw = raw_of(tmp_path, [text(20, 100, b"First box words") + text(20, 86, b"Second box words")])
    page, body = raw["pages"][0], classify.body_size(raw)
    slide = classify.classify_page(page, body)
    assert "marked" not in slide
    assert json.dumps(slide, sort_keys=True) == json.dumps(classify.PageClassifier(page, body).classify(), sort_keys=True)


def test_words_a_later_picture_hides_are_still_the_box_words(tmp_path, backend):
    """A picture drawn over the middle of a box hides its words on the page as in the deck; the
    box still says them (extract keeps a marked element's hidden spans), and names only what shows
    as its page text (render erases what `spans` names)."""
    words = text(10, 100, b"This image does not have transparency.", 8)
    cover = b"q 60 0 0 20 50 92 cm " + INLINE_IMAGE + b"Q "
    page = element(b"t", b"text", paragraph(0, words)) + element(b"pic", b"picture", cover)
    raw = raw_of(tmp_path, [page])
    assert raw["pages"][0]["hidden_spans"]
    assert all(s["id"].startswith("p0h") for s in raw["pages"][0]["hidden_spans"])
    slide = classify.classify_page(raw["pages"][0], classify.body_size(raw))
    assert texts(slide)["t"] == ["This image does not have transparency."]
    box = next(e for e in slide["elements"] if e.get("mark") == "t")
    shown = {s["id"] for s in raw["pages"][0]["spans"]}
    assert box["hidden_spans"] and set(box["spans"]) <= shown
    plain = raw_of(tmp_path, [words + cover], name="plain.pdf")["pages"][0]
    assert "hidden_spans" not in plain and plain["hidden_text"]


# ---------------------------------------------------------------------------------------- compare

def test_compare_pairs_marked_elements_by_their_key():
    """Two boxes with the same words: by words and place the upper pairs with the upper. When the
    read-back says the upper box was written from the lower object, compare believes the key."""
    from beamer2slides.compare import compare
    from .inverse_edits import move
    from .test_inverse import fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    body = next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")
    twin = {**copy.deepcopy(body), "id": "p2t9"}
    deck["slides"][s]["elements"].append(twin)
    tgt = move(deck, s, twin, 0, 60)
    geometry = lambda cur: sorted(round(r["dy"]) for r in compare(cur, tgt).open() if r["kind"] == "geometry")
    assert geometry(tgt) == []
    cur = copy.deepcopy(tgt)
    for e in cur["slides"][s]["elements"]:
        e["mark"] = {"p2t1": "p2t9", "p2t9": "p2t1"}.get(e["id"], e["id"])
    assert geometry(cur) == [-60, 60]  # one per box: each stands where the other object is


def test_marked_boxes_are_not_in_reading_order_again():
    """A box moved above another reads first: by reading order a paragraph_order residual, though
    the move is the box's place. Paired by key, the move is its geometry and nothing else."""
    from beamer2slides.compare import compare
    from .inverse_edits import move
    from .test_inverse import fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    body = next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")
    twin = {**copy.deepcopy(body), "id": "p2t9"}
    twin["paragraphs"] = twin["paragraphs"][:1]
    twin["paragraphs"][0]["runs"] = [{**twin["paragraphs"][0]["runs"][0], "text": "A second box says other things"}]
    deck["slides"][s]["elements"].append(twin)
    tgt = move(deck, s, twin, 0, 60)
    cur = move(tgt, s, twin, 0, -120)
    kinds = lambda c: sorted(r["kind"] for r in compare(c, tgt).open())
    assert "paragraph_order" in kinds(cur)
    for e in cur["slides"][s]["elements"]:
        e["mark"] = e["id"]
    assert kinds(cur) == ["geometry"]


def test_a_target_object_off_the_page_is_no_missing_element():
    """A deck object parked beside its slide reaches no PDF: it is not an open residual."""
    from beamer2slides.compare import compare
    from .test_inverse import fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    tgt = copy.deepcopy(deck)
    parked = {**copy.deepcopy(next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")), "id": "p2t8"}
    w = deck["slides"][s]["size"][0]
    parked["bbox"] = [w + 10, 10, w + 100, 40]
    tgt["slides"][s]["elements"].append(parked)
    got = [r for r in compare(deck, tgt).residuals if r.get("target_element") == "p2t8"]
    assert got and all(r["within"] and r["off_page"] for r in got)
    assert not [r for r in compare(deck, tgt).open() if r.get("target_element") == "p2t8"]


# ------------------------------------------------------------------------------------------ notes

def test_a_note_page_whose_thumbnail_is_empty_is_still_a_note_page(tmp_path, backend):
    """An adopted frame's words are placed at shipout, so beamer's note-page thumbnail of it is an
    empty canvas: the quarter-size canvas at the header's right end says it is a note page."""
    from beamer2slides import notes
    slide = text(20, 100, b"The slide itself")
    header = b"0.8 g 0 112 200 38 re f 0 g " + text(10, 130, b"Frame title", 9)
    canvas = b"1 g 150 112.5 50 37.5 re f 0 g "
    note = text(10, 80, b"Say this slowly")
    for with_canvas, want in ((True, {0: "Say this slowly"}), (False, {})):
        path = tmp_path / f"notes-{with_canvas}.pdf"
        path.write_bytes(pdf_bytes([slide, header + (canvas if with_canvas else b"") + note], extra_resources=FONT))
        out = tmp_path / f"out-{with_canvas}"
        out.mkdir()
        assert notes.prepare(path, out).notes == want
