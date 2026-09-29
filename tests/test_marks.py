"""Adopted pages say what adopt wrote: slides.sty opens a /B2S marked-content sequence around every
element it draws (its key and kind), a /B2Sp one around each paragraph, and the read-back builds
the page's elements from them instead of guessing (docs/adopt-bench.md, 2026-09-26).

Offline: the PDFs here are written by hand (`render_torture.pdf_bytes`), as lualatex writes
them, so no compile is needed. What the backends answer for marks is tests/test_pdf_backend.py's."""

import copy
import json

import pytest

from beamer2slides import classify, extract, pdf
from beamer2slides.devtools.render_torture import MEDIA, pdf_bytes
from beamer2slides.ir import deck_json, slide_json
from beamer2slides.json_types import JsonObject, as_array, as_objects, as_str

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
    path.write_bytes(pdf_bytes(pages, forms, MEDIA, FONT, b""))
    return extract.extract(path, None)


def read_back(tmp_path, page: bytes) -> dict:
    """The page's slide as the read-back of an adopted PDF gets it (`classify.classify_page`)."""
    raw = raw_of(tmp_path, [page])
    return slide_json(classify.classify_page(raw["pages"][0], classify.body_size(raw)))


def by_mark(deck: JsonObject) -> dict[str, JsonObject]:
    """mark -> element of a deck's first slide, as deck.json holds it."""
    slide = as_objects(deck["slides"], "slides")[0]
    return {as_str(e["mark"], "mark"): e for e in as_objects(slide["elements"], "elements") if e.get("mark")}


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
    assert got == {"Left": [("B2S", {"k": "a", "t": "text"}), ("B2Sp", {"i": 0})],
                   "Right": [("B2S", {"k": "b", "t": "text"}), ("B2Sp", {"i": 0})],
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
    assert marks == [[("B2S", {"k": "s1", "t": "shape"})], [("B2S", {"k": "pic", "t": "picture"})], None]


def test_adopt_names_each_mark_after_its_deck_object():
    """slides-keys.tex lists, frame by frame, the objectId behind each mark in drawing order: a text
    box's panel is `<id>+shape`, a layout's object `layout/object`, an id TeX cannot carry nothing."""
    from beamer2slides import adopt
    from beamer2slides.adopt_theme import FramePlan
    from .deck_records import deck, record
    assert adopt.mark_key("layout~p3_i2") == "layout/p3_i2"
    assert adopt.mark_key("has space") is None
    at = {"bbox": [0, 0, 10, 10]}
    box = {"id": "g1_0_5", "kind": "text", **at}
    panelled = "\\slideshape{rect}{..}\n\\begin{slidebox}{..}"
    assert adopt.piece_keys(record(box), panelled) == ["g1_0_5+shape", "g1_0_5"]
    assert adopt.piece_keys(record({"id": "p2", "kind": "image", **at}), "\\slidepicture{..}") == ["p2"]
    assert adopt.piece_keys(record({"id": "a b", "kind": "shape", **at}), "\\sliderect{..}") == [""]

    # the layout draws the logo and the title, at shipout: after the frame's own
    plan = FramePlan(layout="title-only", drawn=frozenset({0, 1}), words={}, background=None, backdrop=None,
                     nonumber=False)
    title = {"id": "t0", "kind": "text", "placeholder": "TITLE", **at}
    target = deck({"slides": [{"layout": "L", "elements": [title, {"id": "logo", "kind": "image", "inherited": "L", **at},
                                                           box, {"id": "t", "kind": "table", **at}]},
                              {"elements": [{"id": "no key", "kind": "text", **at}]}]})
    pieces = [["\\begin{slidebox}", "\\slidepicture{..}", panelled, "\\begin{slidetable}"], ["\\begin{slidebox}"]]
    got = adopt.keys_file(target, pieces, [plan, None], ["s1", "s2"]).splitlines()
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
            assert item.pop("marks") == [("B2S", {"k": "all", "t": "text"})]
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


def test_an_adopted_shape_slides_has_no_preset_for_is_uploaded_as_its_picture(tmp_path, backend):
    """A `\\slidefreeform` outline (`custom`), a line and an outline alone have no Slides shape:
    the conversion makes each the picture of what its mark draws, in its place in the drawing
    order (a `custom` shape raised KeyError in the .pptx's template shapes). A filled rectangle
    stays a shape with its outline. The read-back, which compare reads, keeps all four shapes."""
    from beamer2slides import emit, render
    page = (element(b"r", b"shape", b"0 0 1 RG 2 w 0 1 0 rg 10 10 40 30 re B", b" /box (10 110 40 30)") +
            element(b"f", b"shape", b"1 0 0 rg 70 10 m 110 10 l 90 45 l h f") +
            element(b"l", b"shape", b"0 0 0 RG 1 w 120 20 m 180 60 l S") +
            element(b"o", b"shape", b"0 0 0 RG 1 w 20 70 50 30 re S"))
    raw = raw_of(tmp_path, [page])
    deck = deck_json(classify.classify(raw))
    before = list(by_mark(deck))
    assert {k: (e["kind"], e["shape"]) for k, e in by_mark(deck).items()} == {
        "r": ("shape", "RECTANGLE"), "f": ("shape", "custom"), "l": ("shape", "line"), "o": ("shape", "RECTANGLE")}
    render.render_backgrounds(tmp_path / "p.pdf", raw, deck, tmp_path / "out", frozenset())
    assert list(by_mark(deck)) == before
    assert {k: e["kind"] for k, e in by_mark(deck).items()} == {"r": "shape", "f": "image", "l": "image", "o": "image"}
    for k in "flo":
        assert (tmp_path / "out" / as_str(by_mark(deck)[k]["file"], "file")).is_file()
    line = [v for v in as_array(by_mark(deck)["l"]["bbox"], "bbox") if isinstance(v, (int, float))]
    assert line[0] < 120 and line[2] > 180  # (with the stroke's ink)
    planned = emit.plan_offline(deck)
    assert planned["plan"].keys == []
    reqs = [r for _, _, parts, _ in planned["slides"] for _, rs in parts for r in rs]
    assert [r["createShape"]["shapeType"] for r in reqs if "createShape" in r] == ["RECTANGLE"]
    props = next(r["updateShapeProperties"]["shapeProperties"] for r in reqs if "updateShapeProperties" in r)
    assert props["outline"]["propertyState"] == "RENDERED" and props["outline"]["weight"]["magnitude"] > 0


def cell(r: int, c: int, body: bytes) -> bytes:
    return b"/B2Sc <</r %d /c %d /rs 1 /cs 1>> BDC %s EMC " % (r, c, body)


@pytest.mark.parametrize("grid", [b" /xs (0 80 160) /ys (0 20 40)", b""])
def test_an_adopted_table_carries_the_layout_emit_writes_it_by(tmp_path, backend, grid):
    """A `slidetable`'s mark says its grid (`/xs`, `/ys`; a source adopt wrote before it did: from
    where its words stand), and the table is laid out by it: its rows where the mark puts them, a
    column of numbers right-aligned, the shaded head row's fills and the rule under it borders of
    the cells it runs along. Emit lays it out (an adopted table raised KeyError 'columns')."""
    from beamer2slides import emit
    from beamer2slides.emit_tables import pptx_table, table_requests
    drawn = b"0.9 g 20 110 80 20 re f 100 110 80 20 re f 0 g 0 0 0 RG 1 w 20 110 m 180 110 l S "
    words = (cell(0, 0, text(24, 116, b"Name")) + cell(0, 1, text(146, 116, b"Value")) +
             cell(1, 0, text(24, 96, b"alpha")) + cell(1, 1, text(162, 96, b"42")))
    page = element(b"t", b"table", drawn + words, b" /rows 2 /cols 2 /box (20 20 160 40)" + grid)
    table = next(e for e in read_back(tmp_path, page)["elements"] if e["kind"] == "table")
    cells = [["".join(r["text"] for r in runs) for runs in row] for row in table["cells"]]
    assert cells == [["Name", "Value"], ["alpha", "42"]]
    assert table["frame"] == [20, 20, 180, 60]
    if grid:
        assert table["bounds"] == [20, 100, 180] and [b[1:] for b in table["bands"]] == [[20, 40], [40, 60]]
    assert [c["align"] for c in table["columns"]] == ["left", "right"]
    assert sorted((f["row"], f["col"]) for f in table["fills"]) == [(0, 0), (0, 1)]
    assert sorted((b["row"], b["col"], b["position"]) for b in table["borders"]) == [(0, 0, "BOTTOM"), (0, 1, "BOTTOM")] \
        or sorted((b["row"], b["col"], b["position"]) for b in table["borders"]) == [(1, 0, "TOP"), (1, 1, "TOP")]
    fonts = emit.FontMapper()
    assert len(pptx_table(table, 1.0, fonts)["heights"]) == 2
    reqs = table_requests(table, "s", "tab", 1.0, fonts, imported=True)
    assert [r["insertText"]["text"] for r in reqs if "insertText" in r] == ["Name", "Value", "alpha", "42"]


def test_a_marked_table_of_empty_cells_and_a_centred_merge_are_laid_out(tmp_path, backend):
    """A table adopt wrote with no words in it still gets its layout (it raised KeyError 'columns'),
    and a cell spanning both columns is aligned by its own words, not its first column's: a head
    centred across the table was written START (audit, 2026-09-29)."""
    from beamer2slides import emit
    from beamer2slides.emit_tables import pptx_table, table_requests
    fonts = emit.FontMapper()
    empty = element(b"e", b"table", b"0 g 0 0 0 RG 1 w 20 110 m 180 110 l S ", b" /rows 2 /cols 2 /box (20 20 160 40)")
    table = next(e for e in read_back(tmp_path, empty)["elements"] if e["kind"] == "table")
    assert table["bounds"][0] == 20 and table["bounds"][-1] == 180 and len(table["columns"]) == 2
    assert len(pptx_table(table, 1.0, fonts)["heights"]) == 2
    head = b"/B2Sc <</r 0 /c 0 /rs 1 /cs 2>> BDC %s EMC " % text(60, 116, b"Centred head")
    words = head + cell(1, 0, text(24, 96, b"alpha")) + cell(1, 1, text(162, 96, b"42"))
    page = element(b"t", b"table", words, b" /rows 2 /cols 2 /box (20 20 160 40) /xs (0 80 160) /ys (0 20 40)")
    table = next(e for e in read_back(tmp_path, page)["elements"] if e["kind"] == "table")
    assert [(m["row"], m["col"], m["cols"], m["align"]) for m in table["merges"]] == [(0, 0, 2, "center")]
    assert len(table["merge_x"]) == 1 and 55 < table["merge_x"][0][0] < table["merge_x"][0][1] < 145
    reqs = table_requests(table, "s", "tab", 1.0, fonts, imported=True)
    head_align = [r["updateParagraphStyle"]["style"]["alignment"] for r in reqs if "updateParagraphStyle" in r
                  and r["updateParagraphStyle"]["cellLocation"] == {"rowIndex": 0, "columnIndex": 0}]
    assert head_align and set(head_align) == {"CENTER"}


def test_a_shape_an_old_adopt_base_holds_stays_a_shape_for_sync(tmp_path, backend):
    """Sync pairs the base's elements with ours kind for kind: a freeform an adopt base written
    before pictured shapes recorded as a shape stays one (`kept_shapes`, `marked.shape_marks`), or
    an unchanged source deleted the person's freeform for a picture of it (audit, 2026-09-29)."""
    from beamer2slides import marked, render
    page = (element(b"f", b"shape", b"1 0 0 rg 70 10 m 110 10 l 90 45 l h f") +
            element(b"l", b"shape", b"0 0 0 RG 1 w 120 20 m 180 60 l S"))
    raw = raw_of(tmp_path, [page])
    deck = deck_json(classify.classify(raw))
    base = {"adopt": {}, "slides": [{"elements": [{"kind": "shape", "ir": {"kind": "shape", "mark": "f"}},
                                                  {"kind": "image", "ir": {"kind": "image", "mark": "l"}}]}]}
    assert marked.shape_marks(base) == {"f"} and marked.shape_marks({**base, "adopt": None}) == frozenset()
    render.render_backgrounds(tmp_path / "p.pdf", raw, deck, tmp_path / "out", marked.shape_marks(base))
    assert {k: e["kind"] for k, e in by_mark(deck).items()} == {"f": "shape", "l": "image"}


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
    assert texts(slide_json(slide))["t"] == ["This image does not have transparency."]
    box = next(e for e in slide["elements"] if e["kind"] == "text" and e.get("mark") == "t")
    shown = {s["id"] for s in raw["pages"][0]["spans"]}
    assert box.get("hidden_spans") and set(box["spans"]) <= shown
    plain = raw_of(tmp_path, [words + cover], name="plain.pdf")["pages"][0]
    assert "hidden_spans" not in plain and plain["hidden_text"]


# ---------------------------------------------------------------------------------------- compare

def test_compare_pairs_marked_elements_by_their_key():
    """Two boxes with the same words: by words and place the upper pairs with the upper. When the
    read-back says the upper box was written from the lower object, compare believes the key."""
    from beamer2slides.compare import BoxGeometry, TextGeometry
    from .inverse_edits import move
    from .test_inverse import compared, fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    body = next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")
    twin = {**copy.deepcopy(body), "id": "p2t9"}
    deck["slides"][s]["elements"].append(twin)
    tgt = move(deck, s, twin, 0, 60)
    geometry = lambda cur: sorted(round(r.dy) for r in compared(cur, tgt).open() if isinstance(r, (TextGeometry, BoxGeometry)))
    assert geometry(tgt) == []
    cur = copy.deepcopy(tgt)
    for e in cur["slides"][s]["elements"]:
        e["mark"] = {"p2t1": "p2t9", "p2t9": "p2t1"}.get(e["id"], e["id"])
    assert geometry(cur) == [-60, 60]  # one per box: each stands where the other object is


def test_marked_boxes_are_not_in_reading_order_again():
    """A box moved above another reads first: by reading order a paragraph_order residual, though
    the move is the box's place. Paired by key, the move is its geometry and nothing else."""
    from .inverse_edits import move
    from .test_inverse import compared, fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    body = next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")
    twin = {**copy.deepcopy(body), "id": "p2t9"}
    twin["paragraphs"] = twin["paragraphs"][:1]
    twin["paragraphs"][0]["runs"] = [{**twin["paragraphs"][0]["runs"][0], "text": "A second box says other things"}]
    deck["slides"][s]["elements"].append(twin)
    tgt = move(deck, s, twin, 0, 60)
    cur = move(tgt, s, twin, 0, -120)
    kinds = lambda c: sorted(r.kind for r in compared(c, tgt).open())
    assert "paragraph_order" in kinds(cur)
    for e in cur["slides"][s]["elements"]:
        e["mark"] = e["id"]
    assert kinds(cur) == ["geometry"]


def test_a_target_object_off_the_page_is_no_missing_element():
    """A deck object parked beside its slide reaches no PDF: it is not an open residual."""
    from beamer2slides.compare import residual_json
    from .test_inverse import compared, fixture_deck, slide_of
    deck = fixture_deck()
    s = slide_of(deck, "method")
    tgt = copy.deepcopy(deck)
    parked = {**copy.deepcopy(next(e for e in deck["slides"][s]["elements"] if e["id"] == "p2t1")), "id": "p2t8"}
    w = deck["slides"][s]["size"][0]
    parked["bbox"] = [w + 10, 10, w + 100, 40]
    tgt["slides"][s]["elements"].append(parked)
    got = [j for j in map(residual_json, compared(deck, tgt).residuals) if j.get("target_element") == "p2t8"]
    assert got and all(r["within"] and r["off_page"] for r in got)
    assert not [j for j in map(residual_json, compared(deck, tgt).open()) if j.get("target_element") == "p2t8"]


# ------------------------------------------------------------------------------------------ notes

def test_a_note_page_whose_thumbnail_is_empty_is_still_a_note_page(tmp_path, backend):
    """An adopted frame's words are placed at shipout, so beamer's note-page thumbnail of it is an
    empty canvas: the quarter-size canvas at the header's right end says it is a note page."""
    from beamer2slides import notes
    slide = text(20, 100, b"The slide itself")
    header = b"0.8 g 0 112 200 38 re f 0 g " + text(10, 130, b"Frame title", 9)
    canvas = b"1 g 150 112.5 50 37.5 re f 0 g "
    note = text(10, 80, b"Say this slowly")
    marked = element(b"k", b"text", paragraph(0, note))  # a frame of adopt's says what it holds
    for name, second, want in (("canvas", header + canvas + note, {0: "Say this slowly"}),
                               ("bare", header + note, {}),
                               ("marked", header + canvas + marked, {})):
        path = tmp_path / f"notes-{name}.pdf"
        path.write_bytes(pdf_bytes([slide, second], (), MEDIA, FONT, b""))
        out = tmp_path / f"out-{name}"
        out.mkdir()
        prepared = notes.prepare(path, out)
        assert prepared.notes == want
        assert (prepared.pdf == path) == (not want)  # no note page: the PDF as it is
