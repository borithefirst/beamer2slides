"""Text the PDF draws but nobody sees (extract.Visibility, PageObject.clip): outside its clip,
under an opaque fill or image painted after it, at alpha 0. PDFium's text page reports it like
any other text, so it came back as native text in Slides (visual hunt: Boadilla's author box
running under the title box on every slide, tick labels under the footline, a tikz spy's
magnified copies, the trimmed-away panel of an `\\includegraphics[trim, clip]`, a legend under a
white callout, an opacity=0 node on an overlay step)."""

import zlib

import numpy as np
import pytest

from beamer2slides import pdf
from beamer2slides.extract import Visibility, extract_page, select_overlays
from beamer2slides.pdf.pure import foxit

# The pages set unembedded Helvetica, which the pure reader draws with PDFium's own faces from
# their user cache: without them its text is empty, which is no finding about hidden text.
needs_faces = pytest.mark.skipif(not foxit.available(),
                                 reason="no PDFium font cache: python -m beamer2slides.pdf.pure.foxit")
BACKENDS = ["pdfium", pytest.param("pure", marks=needs_faces)]
HELV = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def one_page(content: bytes, xobjects: dict[str, bytes] | None = None, width=400, height=200) -> bytes:
    """A one-page PDF: Helvetica as /F1, transparency states /T0 (alpha 0), /T5 (alpha 0.5) and
    /F0 (fill alpha 0),
    and XObjects given as whole object bodies."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", None,
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream", HELV]
    names = []
    for name, body in (xobjects or {}).items():
        objs.append(body)
        names.append(b"/%s %d 0 R" % (name.encode(), len(objs)))
    objs[2] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R /Resources << "
               b"/Font << /F1 5 0 R >> /ExtGState << /T0 << /ca 0 /CA 0 >> /T5 << /ca 0.5 /CA 0.5 >> /F0 << /ca 0 >> >> "
               b"/XObject << %s >> >> >>" % (width, height, b" ".join(names)))
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for k, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % k + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def form(content: bytes, bbox=b"0 0 400 200") -> bytes:
    return b"<< /Type /XObject /Subtype /Form /BBox [%s] /Resources << /Font << /F1 5 0 R >> >> /Length %d >>\n" \
           b"stream\n%s\nendstream" % (bbox, len(content), content)


def image(rgb=(255, 0, 0)) -> bytes:
    data = zlib.compress(bytes(rgb) * 4)
    return b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 " \
           b"/Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def words(data: bytes, backend: str) -> str:
    """The text extract keeps, as the spans' words in order."""
    with pdf.use_backend(backend):
        doc = pdf.Document(data)
        try:
            return " ".join(s["text"].strip() for s in extract_page(doc[0], "1")["spans"])
        finally:
            doc.close()


def text(x, y, s: bytes, size=12) -> bytes:
    return b"BT /F1 %d Tf %g %g Td (%s) Tj ET\n" % (size, x, y, s)


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_outside_its_clip_is_not_text_of_the_slide(backend):
    # the other panel of an \includegraphics[trim, clip]: a rectangle clip around a form
    data = one_page(b"q 0 0 150 200 re W n " + text(20, 100, b"Shown") + text(200, 100, b"Trimmed") + b"Q\n")
    assert words(data, backend) == "Shown"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_word_cut_at_the_clip_keeps_the_letters_mostly_inside(backend):
    # Helvetica 12: "Author" is ~37 pt wide from x 20; the clip ends at x 45, inside the "o"
    # (26.7 + ... the letters whose box is mostly left of 45 stay)
    data = one_page(b"q 0 0 45 200 re W n " + text(20, 100, b"Author") + b"Q\n")
    kept = words(data, backend)
    assert "Author".startswith(kept) and 2 <= len(kept) < 6


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_letter_the_page_edge_cuts_is_still_text(backend):
    # an overfull table cell at the right edge (r2_code_v2 slide 9): the last "e" of "see"
    # (398.4 - 405.1 on a 400 pt page) shows its part on the page. Judged by samples off the
    # page it went, and the word came out "se" with the half letter left in the background.
    data = one_page(text(20, 100, b"Visible") + text(385, 100, b"see"))
    assert words(data, backend) == "Visible see"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_form_bbox_clips_its_text(backend):
    data = one_page(b"/X1 Do\n" + text(20, 150, b"Page"),
                    {"X1": form(text(20, 100, b"Inside") + text(250, 100, b"Outside"), b"0 0 200 200")})
    assert words(data, backend) == "Inside Page"


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_under_an_opaque_fill_painted_after_it_is_hidden(backend):
    # Boadilla: the author box's text runs on under the title box, which is painted after it
    data = one_page(text(20, 100, b"Visible") + text(120, 100, b"Covered") + b"0.8 0.8 0.9 rg 110 90 200 30 re f\n")
    assert words(data, backend) == "Visible"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_fill_painted_before_the_text_is_its_ground(backend):
    data = one_page(b"0.8 0.8 0.9 rg 110 90 200 30 re f 0 g\n" + text(120, 100, b"OnPanel"))
    assert words(data, backend) == "OnPanel"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_translucent_fill_does_not_hide(backend):
    data = one_page(text(120, 100, b"Tinted") + b"q /T5 gs 1 0 0 rg 110 90 200 30 re f Q\n")
    assert words(data, backend) == "Tinted"


@pytest.mark.parametrize("backend", BACKENDS)
def test_two_fills_that_meet_cover_together(backend):
    # a caption under the footline's two colour bars
    data = one_page(text(120, 100, b"Caption") + b"0.9 g 100 104 200 20 re f 0.6 0 0 rg 100 84 200 20 re f\n")
    assert words(data, backend) == ""


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_rounded_panel_covers_what_is_inside_its_outline(backend):
    # a white callout with rounded corners (curves) painted over a legend
    panel = (b"1 g 110 80 m 290 80 l 300 80 300 90 300 90 c 300 120 l 300 130 290 130 290 130 c "
             b"110 130 l 100 130 100 120 100 120 c 100 90 l 100 80 110 80 110 80 c f\n")
    data = one_page(text(130, 100, b"legend") + text(320, 100, b"beside") + panel)
    assert words(data, backend) == "beside"


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_fill_its_clip_cuts_does_not_count(backend):
    # a mindmap's connection bar: the clip is only known by its box, so a cover it cuts is unsure
    data = one_page(text(120, 100, b"Governance") + b"q 100 90 60 30 re W n 1 0 0 rg 100 80 150 50 re f Q\n")
    assert words(data, backend) == "Governance"


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_under_an_opaque_image_painted_after_it_is_hidden(backend):
    # a frame title under a full-page photo
    data = one_page(text(20, 170, b"Title") + b"q 400 0 0 200 0 0 cm /Im1 Do Q\n" + text(20, 100, b"Over"),
                    {"Im1": image()})
    assert words(data, backend) == "Over"


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_at_alpha_zero_is_not_there(backend):
    data = one_page(b"q /T0 gs " + text(20, 100, b"Invisible") + b"Q " + text(20, 150, b"Seen"))
    assert words(data, backend) == "Seen"


def test_overlay_steps_still_know_the_words_they_do_not_show():
    """r2_overlays_v2: step 1 of a frame shows its first node and an \\only caption, the nodes of
    steps 2 and 3 are drawn at alpha 0. Their words tell step 1 is the same frame as step 3."""
    step = lambda caption, shown, hidden: one_page(
        text(10, 180, b"The pipeline") + text(10, 120, shown) +
        b"q /T0 gs " + text(10, 100, hidden) + b"Q " + text(10, 30, caption))
    pages = [step(b"We start from the raw detector counts, without any preprocessing.",
                  b"Raw stack 16-bit TIFF", b"Noise model PoissonGauss Denoiser U-Net, 4 levels Clean stack"),
             step(b"The output keeps the photon budget and reports a per-pixel uncertainty.",
                  b"Raw stack 16-bit TIFF Noise model PoissonGauss Denoiser U-Net, 4 levels Clean stack", b"")]
    raw = {"pages": []}
    for data in pages:
        doc = pdf.Document(data)
        try:
            raw["pages"].append(extract_page(doc[0], "1"))
        finally:
            doc.close()
    assert raw["pages"][0]["hidden_text"] == ["Noise model PoissonGauss Denoiser U-Net, 4 levels Clean stack"]
    assert "hidden_text" not in raw["pages"][1]
    assert [p["spans"][-1]["text"] for p in select_overlays(raw, "last")["pages"]] == \
           ["The output keeps the photon budget and reports a per-pixel uncertainty."]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_drawing_at_opacity_zero_is_left_out(backend):
    # an opacity=0 tikz node: neither its fill nor its stroke shows
    content = (b"q /T0 gs 1 0.5 0 rg 1 0 0 RG 20 20 60 30 re B Q\n"       # invisible: gone
               b"q /T0 gs 0 0 1 RG 20 80 60 30 re S Q\n"                   # invisible stroke: gone
               b"0 1 0 rg 20 140 60 30 re f\n")                             # seen
    with pdf.use_backend(backend):
        doc = pdf.Document(one_page(content))
        try:
            page = doc[0]
            raw = extract_page(page, "1")
            ids = [d["id"] for d in raw["drawings"]]
            assert [d["fill_opacity"] for d in raw["drawings"]] == [1.0]
            assert ids == [f"p0d{len(page.drawings()) - 1}"]  # ids stay indices into page.drawings()
        finally:
            doc.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_drawing_keeps_the_half_that_shows(backend):
    # fill-opacity=0 with a visible draw: filled and stroked in one path, only its outline shows
    content = b"q /F0 gs 1 0.5 0 rg 1 0 0 RG 20 20 60 30 re B Q\n"
    with pdf.use_backend(backend):
        doc = pdf.Document(one_page(content))
        try:
            raw = extract_page(doc[0], "1")
            assert [d["type"] for d in raw["drawings"]] == ["s"]
        finally:
            doc.close()


@needs_faces
def test_clip_boxes_are_pdfiums_on_both_backends():
    # every clip cuts its text (a clip around the whole object is no clip: see the next test)
    content = (b"q 0.5 0 0 0.5 10.3 10.7 cm 0 0 40 200 re W n " + text(20, 100, b"Scaled") + b"Q\n"
               b"q 30 30 300 100 re W n 50 40 30 50 re W n " + text(60, 60, b"Nested") + b"Q\n"
               b"q 200 90 m 280 90 l 240 130.3 l h W n " + text(210, 100, b"Triangle") + b"Q\n"
               b"/X1 Do\n" + text(20, 180, b"Free"))
    data = one_page(content, {"X1": form(text(20, 20, b"Form"), b"10 10 35 60")})
    docs = [pdf.resolve(spec).open(data) for spec in ("pdfium", "pure")]
    try:
        clips = [[po.clip for po in d[0].objects()] for d in docs]
    finally:
        for d in docs:
            d.close()
    assert clips[0] == clips[1]
    # the form and the free text are clipped by nothing; the form's text by its BBox
    assert sum(c is None for c in clips[0]) == 2 and sum(c is not None for c in clips[0]) == 4


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_clip_that_clips_nothing_is_none(backend):
    # CPDF_ContentParser::CheckClip: a single rectangle clip around the whole object is dropped
    data = one_page(b"q 0 0 400 200 re W n " + text(20, 100, b"Inside") + b"Q\n")
    with pdf.use_backend(backend):
        doc = pdf.Document(data)
        try:
            assert [po.clip for po in doc[0].objects()] == [None]
        finally:
            doc.close()


def test_hidden_text_stays_as_hidden_in_the_background(tmp_path):
    """Classify never makes a hidden character native, so render leaves its object on: it is
    under its cover in the background picture exactly as in the PDF, and the native words are
    switched off."""
    from beamer2slides.classify import classify
    from beamer2slides.render import load_png, render_backgrounds

    content = (text(20, 150, b"Native words") + text(20, 60, b"Buried") +
               b"0.2 0.2 0.2 rg 0 40 400 40 re f\n")                    # a dark band over "Buried"
    path = tmp_path / "hidden.pdf"
    path.write_bytes(one_page(content))
    raw = {"version": 1, "source": {"pdf": str(path), "producer": "", "pages": 1, "title": ""},
           "pages": []}
    doc = pdf.Document(path)
    try:
        raw["pages"] = [extract_page(doc[0], "1")]
        raw["pages"][0]["frame_label"] = None
        page = doc[0]
        vis = Visibility(page)
        hidden = [ch for ch in page.chars() if vis.hidden(ch)]
    finally:
        doc.close()
    assert "".join(ch.c for ch in hidden) == "Buried"
    deck = classify(raw)
    texts = [s["text"] for s in raw["pages"][0]["spans"]]
    assert all("Buried" not in t for t in texts)
    [png] = render_backgrounds(path, raw, deck, tmp_path / "bg")
    bg = load_png(png)
    z = bg.shape[1] / 400
    doc = pdf.Document(path)
    try:
        page = doc[0]
        original = page.render(z)
        page.set_active(sorted({ch.obj for ch in hidden}), False)  # the band without the word under it
        unburied = page.render(z)
    finally:
        doc.close()
    band = (slice(round(z * 121), round(z * 159)), slice(0, bg.shape[1]))  # y down
    assert np.array_equal(bg[band], original[band])                         # the word is still under it
    assert np.array_equal(unburied[band], original[band])                   # (it did not show there)
    words_at = (slice(round(z * 38), round(z * 52)), slice(round(z * 20), round(z * 90)))  # "Native words"
    assert (original[words_at] < 128).any() and not (bg[words_at] < 128).any()
