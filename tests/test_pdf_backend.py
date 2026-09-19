"""The PDF backend contract (beamer2slides/pdf/api.py) and its implementations.

- Contract tests run against every backend in $B2S_TEST_PDF_BACKENDS (default: pdfium, sandbox
  and pure), so a new backend is checked by naming it there (a spec as for $B2S_PDF_BACKEND).
  Rendering is checked on the backends that draw (`api.renders`). How close the pure Python
  reader comes to PDFium is tests/test_pure_pdf.py's.
- The sandbox must agree with PDFium on everything, value for value: it is PDFium, one process away.
- The wire format and the worker are tested for what they refuse and how they fail.
- The pipeline through the sandbox giving the very same files as in process was measured on all 48
  test decks when the sandbox was written (docs/pdf-backend.md); here one deck keeps it honest."""

import dataclasses
import json
import os
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from beamer2slides import pdf
from beamer2slides.pdf import api, wire
from beamer2slides.pdf.api import (NO_OBJECT, OBJ_FORM, OBJ_IMAGE, OBJ_PATH, OBJ_SHADING, OBJ_TEXT,
                                   PdfDocument, PdfError, PdfPage, char_box, pixel_bounds)
from beamer2slides.pdf.sandbox import SandboxBackend, Worker

pytestmark = pytest.mark.xdist_group("pdf_backend")  # one worker: the sandbox processes are shared

HERE = Path(__file__).parent
OUT = HERE / "decks" / "out"
BLOCKS = OUT / "04_theme_blocks.pdf"      # blocks: soft masks, shadings, forms; internal links
IMAGES = OUT / "23_raster_images.pdf"     # raster images every way (tests/test_raster_images.py)
BASIC = OUT / "01_basic.pdf"              # navigation links, named destinations, labels
PDFS = [BLOCKS, IMAGES, BASIC]
built = pytest.mark.skipif(not all(p.exists() for p in PDFS), reason="no test PDFs built")

SPECS = [s.strip() for s in os.environ.get("B2S_TEST_PDF_BACKENDS", "pdfium,sandbox,pure").split(",")
         if s.strip()]
_backends: dict[str, object] = {}


def get_backend(spec: str):
    if spec not in _backends:
        _backends[spec] = pdf.resolve(spec)
    return _backends[spec]


@pytest.fixture(params=SPECS)
def backend(request):
    return get_backend(request.param)


@pytest.fixture(scope="module", autouse=True)
def _shutdown():
    yield
    for b in _backends.values():
        if hasattr(b, "shutdown"):
            b.shutdown()


# ---------------------------------------------------------------------- the contract


@built
@pytest.mark.parametrize("path", PDFS, ids=lambda p: p.stem)
def test_documents_and_pages_keep_the_contract(backend, path):
    doc = backend.open(path)
    try:
        assert isinstance(doc, PdfDocument) and len(doc) > 0
        assert doc[-1] is doc[len(doc) - 1] and doc[0] is doc[0]
        with pytest.raises(IndexError):
            doc[len(doc)]
        assert set(doc.metadata) == {"title", "producer"}
        assert all(isinstance(doc.label(i), str) for i in range(len(doc)))
        assert all(isinstance(n, str) and isinstance(p, int) for n, p in doc.named_dests())
        for page in doc:
            assert isinstance(page, PdfPage)
            check_page(page)
    finally:
        doc.close()


def check_page(page):
    assert page.rect == (0.0, 0.0, page.width, page.height)
    objects = page.objects()
    assert [po.id for po in objects] == list(range(len(objects)))
    for po in objects:
        assert po.type in (OBJ_TEXT, OBJ_PATH, OBJ_IMAGE, OBJ_SHADING, OBJ_FORM) and len(po.matrix) == 6
        if po.parent is not None:
            assert po.parent < po.id and objects[po.parent].type == OBJ_FORM and po.id in objects[po.parent].children
        assert not po.children or po.type == OBJ_FORM
    bounds = page.object_bounds()
    assert len(bounds) == len(objects) and all(len(b) == 4 and b[0] <= b[2] and b[1] <= b[3] for b in bounds)

    chars = page.chars()
    for ch in chars:
        assert ch.obj == NO_OBJECT or objects[ch.obj].type == OBJ_TEXT
        assert ch.box == pytest.approx(char_box(*ch.origin, *ch.dir, ch.advance, ch.size, ch.ascent, ch.descent))
        assert 0 <= ch.color <= 0xFFFFFF and ch.ascent > 0 >= ch.descent and not ch.synthetic
        assert not any("\ud800" <= u <= "\udfff" for u in ch.c)
        assert not any(u < " " for u in ch.c), f"a control character {ch.c!r}"
    drawn = [ch.obj for ch in chars if ch.obj != NO_OBJECT]
    assert drawn == sorted(drawn), "characters in content order"
    single = [ch for ch in chars if len(ch.c) == 1][:50]
    widths = page.glyph_widths([(ch.font_id, ch.c, ch.size) for ch in single])
    assert len(widths) == len(single) and all(w is None or w >= 0 for w in widths)

    for d in page.drawings():
        assert d["type"] in ("f", "s", "fs") and objects[d["object"]].type == OBJ_PATH
        assert d["items"] and all(item[0] in ("l", "c", "re", "qu") for item in d["items"])
        x0, y0, x1, y1 = d["rect"]
        assert x0 <= x1 and y0 <= y1
    for im in page.images():
        assert objects[im["object"]].type in (OBJ_IMAGE, OBJ_SHADING) and im["width"] > 0 and im["height"] > 0
        assert (page.embedded_image(im["object"]) is None) == (objects[im["object"]].type == OBJ_SHADING)
    text = next((po.id for po in objects if po.type == OBJ_TEXT), None)
    if text is not None:
        assert page.embedded_image(text) is None
    for link in page.links():
        assert ("page" in link) != ("uri" in link) and len(link["bbox"]) == 4


MISC = OUT / "14_misc.pdf"                # its "Hyphenation" frame breaks words at line ends


@pytest.mark.skipif(not MISC.exists(), reason="no test PDFs built")
def test_a_hyphen_ending_a_line_is_a_hyphen(backend):
    """PDFium's text page marks a hyphen that ends a line and reports it as U+0002: the hyphen
    the page shows must come back, not a control character in the middle of a word."""
    doc = backend.open(MISC)
    try:
        page = next(p for p in doc if "".join(c.c for c in p.chars()).startswith("Hyphenation"))
        chars = page.chars()
        lines: dict[int, str] = {}
        for ch in chars:
            lines[round(ch.origin[1])] = lines.get(round(ch.origin[1]), "") + ch.c
        broken = [s for s in lines.values() if s.endswith("-")]
        assert len(broken) >= 3, lines
        check_page(page)
    finally:
        doc.close()


@built
def test_render_follows_pixel_bounds_and_active_objects(backend):
    if not api.renders(backend):
        # a backend that cannot draw everything yet draws a page as PDFium does or refuses it
        ref = pdf.resolve("pdfium")
        for path in (BLOCKS, IMAGES):
            doc, theirs = backend.open(path), ref.open(path)
            try:
                for i in range(min(len(doc), 3)):
                    try:
                        img = doc[i].render(1.0)
                    except PdfError:
                        continue
                    assert np.array_equal(img, theirs[i].render(1.0)), f"{path.name} page {i}"
            finally:
                doc.close()
                theirs.close()
        return
    doc = backend.open(BLOCKS)
    try:
        page = doc[1]
        full = page.render(1.0)
        assert full.dtype == np.uint8 and full.shape == (pixel_bounds(1.0, page.rect)[3], pixel_bounds(1.0, page.rect)[2], 3)
        clip = (10.3, 20.7, 110.2, 60.1)
        _, _, w, h = pixel_bounds(2.0, clip)
        assert page.render(2.0, clip).shape == (h, w, 3)
        clear = page.render(2.0, clip, transparent=True)
        assert clear.shape == (h, w, 4)
        ids = [po.id for po in page.objects()]
        page.set_active(ids, False)
        try:
            assert (page.render(1.0) == 255).all(), "every object off: a white page"
            assert page.drawings() == [] and page.images() == []
        finally:
            page.set_active(ids, True)
        assert np.array_equal(page.render(1.0), full), "switched on again: the same page"
    finally:
        doc.close()


@pytest.mark.parametrize("rotate", [0, 90, 180, 270, -90])
def test_a_turned_page_renders_where_its_geometry_says(backend, rotate):
    """/Rotate turns PDFium's display matrix, while objects, drawings and chars stay in unrotated
    page space: a render must stay there too, or backgrounds and crops miss their elements (the
    PDFium backend drew a /Rotate 90 page turned into a bitmap of the unturned size)."""
    from beamer2slides.devtools.render_torture import pdf_bytes
    data = pdf_bytes([b"1 0 0 rg 10 10 50 20 re f"], media=(5, 7, 205, 157), page_entries=b"/Rotate %d" % rotate)
    doc = backend.open(data)
    try:
        page = doc[0]
        assert page.rect == (0.0, 0.0, 200.0, 150.0)
        (drawing,) = page.drawings()
        if not api.renders(backend):
            return
        img = page.render(2.0)
        assert img.shape == (300, 400, 3)
        ys, xs = np.nonzero(img[..., 1] < 128)
        x0, y0, x1, y1 = drawing["rect"]
        assert (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) == (x0 * 2, y0 * 2, x1 * 2, y1 * 2)
    finally:
        doc.close()


@built
def test_bytes_open_like_the_file_and_save_writes_new_files(backend, tmp_path):
    doc = backend.open(BASIC)
    same = backend.open(BASIC.read_bytes())
    try:
        assert len(same) == len(doc) and same.path is None
        assert [c.c for c in same[1].chars()] == [c.c for c in doc[1].chars()]
        kept = backend.open(doc.save(pages=[0, 2]))
        try:
            assert len(kept) == 2 and len(doc) == 5, "the document itself is left as it is"
            assert [c.c for c in kept[1].chars()] == [c.c for c in doc[2].chars()]
        finally:
            kept.close()
        half = backend.open(doc.save(boxes={0: (0.0, 0.0, doc[0].width / 2, doc[0].height)}))
        try:
            assert half[0].width == pytest.approx(doc[0].width / 2) and half[1].width == pytest.approx(doc[1].width)
            if api.renders(backend):
                left = doc[0].render(1.0, (0.0, 0.0, doc[0].width / 2, doc[0].height))
                assert np.abs(half[0].render(1.0).astype(int) - left.astype(int)).max() <= 1
            else:  # what the cut page still shows: the characters in its left half
                inside = [c.c for c in doc[0].chars() if c.box[2] <= doc[0].width / 2 - 1]
                assert set(inside) <= {c.c for c in half[0].chars()}
        finally:
            half.close()
    finally:
        doc.close()
        same.close()


def test_unreadable_files_are_pdf_errors(backend):
    with pytest.raises(PdfError):
        backend.open(b"%PDF-1.4 truncated")


# ---------------------------------------------------------------------- the sandbox is PDFium


def same(a, b, where=""):
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        assert isinstance(a, np.ndarray) and isinstance(b, np.ndarray) and a.dtype == b.dtype, where
        assert np.array_equal(a, b), where
    elif dataclasses.is_dataclass(a):
        assert type(a) is type(b), where
        for f in dataclasses.fields(a):
            same(getattr(a, f.name), getattr(b, f.name), f"{where}.{f.name}")
    elif isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), where
        for k in a:
            same(a[k], b[k], f"{where}[{k!r}]")
    elif isinstance(a, (list, tuple)):
        assert type(a) is type(b) and len(a) == len(b), f"{where}: {type(a).__name__} {type(b).__name__}"
        for k, (x, y) in enumerate(zip(a, b)):
            same(x, y, f"{where}[{k}]")
    else:
        assert a == b and type(a) is type(b), f"{where}: {a!r} != {b!r}"


@built
@pytest.mark.parametrize("path", PDFS, ids=lambda p: p.stem)
def test_the_sandbox_answers_what_pdfium_answers(path):
    ours, theirs = get_backend("pdfium").open(path), get_backend("sandbox").open(path)
    try:
        for call in ("metadata", "named_dests"):
            same(getattr(ours, call) if call == "metadata" else ours.named_dests(),
                 getattr(theirs, call) if call == "metadata" else theirs.named_dests(), call)
        same([ours.label(i) for i in range(len(ours))], [theirs.label(i) for i in range(len(theirs))], "labels")
        for a, b in zip(ours, theirs):
            where = f"page {a.index}"
            same((a.width, a.height), (b.width, b.height), where)
            for call in ("objects", "object_bounds", "chars", "drawings", "images", "links"):
                same(getattr(a, call)(), getattr(b, call)(), f"{where} {call}")
            queries = [(ch.font_id, ch.c, ch.size) for ch in a.chars() if len(ch.c) == 1]
            same(a.glyph_widths(queries), b.glyph_widths(queries), f"{where} glyph_widths")
            for po in a.objects():
                if po.type == OBJ_IMAGE:
                    same(a.embedded_image(po.id), b.embedded_image(po.id), f"{where} image {po.id}")
            same(a.render(0.5), b.render(0.5), f"{where} render")
            same(a.render(2.0, (5.5, 5.5, 60.25, 40.75), transparent=True),
                 b.render(2.0, (5.5, 5.5, 60.25, 40.75), transparent=True), f"{where} render clip")
    finally:
        ours.close()
        theirs.close()


@built
def test_the_pipeline_through_the_sandbox_writes_the_same_files(tmp_path):
    from beamer2slides import render
    from beamer2slides.classify import classify
    from beamer2slides.extract import extract

    def run(spec, out):
        with pdf.use_backend(get_backend(spec)):
            raw = extract(BLOCKS)
            deck = classify(raw)
            render.render_backgrounds(BLOCKS, raw, deck, out)
        files = {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
        return json.dumps([raw, deck], sort_keys=True, default=str), files

    here, there = run("pdfium", tmp_path / "a"), run("sandbox", tmp_path / "b")
    assert here[0].replace(str(tmp_path / "a"), "") == there[0].replace(str(tmp_path / "b"), "")
    assert here[1] == there[1] and here[1]


# ---------------------------------------------------------------------- how the sandbox fails


def test_a_killed_worker_costs_its_documents_only():
    backend = SandboxBackend()
    try:
        doc = backend.open(BLOCKS.read_bytes()) if BLOCKS.exists() else pytest.skip("no test PDFs built")
        backend._proc.kill()
        backend._proc.wait()
        with pytest.raises(PdfError):
            doc[0].chars()
        with pytest.raises(PdfError, match="restarted"):
            doc[1]                                # a new worker does not know the old documents
        again = backend.open(BLOCKS)
        assert len(again[0].chars()) > 0
        doc.close()                               # closing a lost document is quiet
        again.close()
    finally:
        backend.shutdown()


def test_a_hanging_worker_is_killed_after_the_timeout(tmp_path):
    (tmp_path / "slowpdf.py").write_text(textwrap.dedent("""
        import time
        class Slow:
            name = "slow"
            def open(self, source):
                time.sleep(60)
        backend = Slow()
    """))
    command = [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r}); "
               "from beamer2slides.pdf.sandbox import main; main(['--backend', 'slowpdf:backend'])"]
    backend = SandboxBackend(command=command, timeout=3)
    try:
        with pytest.raises(PdfError, match="took too long"):
            backend.open(b"%PDF")
    finally:
        backend.shutdown()


def test_the_worker_serves_the_api_and_nothing_else():
    worker = Worker(get_backend("pdfium"))
    with pytest.raises(PdfError, match="no document"):
        worker.handle({"op": "chars", "doc": 7, "page": 0})
    if not BLOCKS.exists():
        pytest.skip("no test PDFs built")
    doc = worker.handle({"op": "open", "args": [BLOCKS.read_bytes()]})["doc"]
    for op, page in (("__class__", None), ("_handle", 0), ("page", 0), ("close", 0), ("textpage", 0), ("pdf", None)):
        with pytest.raises(PdfError, match="unknown"):
            worker.handle({"op": op, "doc": doc, "page": page, "args": []})
    assert worker.handle({"op": "page", "doc": doc, "page": None, "args": [0]})[0] > 0
    with pytest.raises(TypeError):
        worker.handle({"op": "open", "args": ["C:/Windows/win.ini"]})  # bytes only: no paths into the worker


def test_the_wire_carries_data_and_refuses_the_rest():
    value = {"a": (1, 2.5, None, True), 3: [b"\x00\xff", np.arange(6, dtype=np.uint8).reshape(2, 3)],
             "char": api.Char("ﬁ", "LMRoman10", 10.0, 0xFF0000, 255, (1.0, 2.0), (1, 2, 3, 4), (1.0, 0.0), 4, 2),
             "nan": float("inf"), "np": np.float32(0.5)}
    import io
    buf = io.BytesIO()
    wire.write_frame(buf, value)
    buf.seek(0)
    back = wire.read_frame(buf)
    assert back["a"] == (1, 2.5, None, True) and back[3][0] == b"\x00\xff"
    assert np.array_equal(back[3][1], value[3][1]) and back["char"] == value["char"] and back["np"] == 0.5
    with pytest.raises(EOFError):
        wire.read_frame(buf)
    for bad in ({"X": 1}, {"A": ["|O", [1], 0]}, {"C": ["Popen", {}]}, {"T": [1], "D": []}, {"B": 5}):
        with pytest.raises(wire.WireError):
            wire.decode(bad, [b"\x00" * 8])
    with pytest.raises(wire.WireError):
        wire.encode(object(), [])
    with pytest.raises(wire.WireError):
        wire.encode(np.array([object()]), [])


# ---------------------------------------------------------------------- choosing the backend


def test_the_backend_is_chosen_from_outside(tmp_path, monkeypatch):
    (tmp_path / "fakepdf.py").write_text(textwrap.dedent("""
        opened = []
        class Fake:
            name = "fake"
            def open(self, source):
                opened.append(source)
                return "a document"
        def make():
            return Fake()
        instance = Fake()
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    assert pdf.resolve("fakepdf:make").name == "fake"       # a factory
    assert pdf.resolve("fakepdf:instance").name == "fake"   # an object
    assert pdf.resolve("pdfium").name == "pdfium"
    assert pdf.resolve("sandbox").name == "sandbox:pdfium"
    assert pdf.resolve("pure").name == "pure" and not api.renders(pdf.resolve("pure"))
    assert api.renders(pdf.resolve("pdfium"))
    with pytest.raises(ValueError):
        pdf.resolve("nonsense")
    with pytest.raises(TypeError):
        pdf.resolve("fakepdf:opened")                        # a list is no backend
    with pdf.use_backend("fakepdf:instance"):
        assert pdf.Document("x.pdf") == "a document"
    assert pdf.backend().name != "fake"                      # put back
    monkeypatch.setenv(pdf.ENV, "fakepdf:make")
    old = pdf.set_backend(None)                              # None: the environment decides again
    try:
        assert pdf.Document(b"%PDF") == "a document"
    finally:
        pdf.set_backend(old)
