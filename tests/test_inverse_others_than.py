"""`inverse.others_than`: which objects go off so an adopted picture is judged by what it drew alone.

It reads an object's element the way `marked.split` grouped the element (`marked.group_id`: the
mark's `/n`, else its `/k`, which is the `mark_n` a picture carries). By `/n` alone, a mark with no
`/n` matched nothing and every object went off: the candidate's picture was hashed blank and read
as changed. Offline: PDFs written by hand (`render_torture.pdf_bytes`), read by both backends.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from beamer2slides import inverse, pdf
from beamer2slides.devtools.render_torture import pdf_bytes
from beamer2slides.marked import group_id


@pytest.fixture(params=["pdfium", "pure"])
def backend(request: pytest.FixtureRequest) -> Iterator[None]:
    with pdf.use_backend(request.param):
        yield


def element(params: bytes, body: bytes) -> bytes:
    return b"/B2S <<%s /t (picture)>> BDC %s EMC " % (params, body)


def others(tmp_path: Path, page: bytes, forms: list[tuple[bytes, bytes]], n: int | str) -> list[int]:
    path = tmp_path / "p.pdf"
    path.write_bytes(pdf_bytes([page], forms, (0, 0, 200, 150), b"", b""))
    doc = pdf.Document(path)
    try:
        return inverse.others_than(doc[0], n)
    finally:
        doc.close()


FORM = [(b"/BBox [0 0 200 150]", b"0 1 0 rg 60 60 10 10 re f")]
BACKGROUND = b"0 0 1 rg 100 10 20 20 re f"


def test_an_element_numbered_by_slides_sty_keeps_its_objects(tmp_path: Path, backend: None) -> None:
    """Objects: 0 the form (1 inside it), 2 the other picture's path, 3 the unmarked background."""
    page = element(b"/k (a) /n 2", b"/X0 Do") + element(b"/k (b) /n 3", b"1 0 0 rg 10 10 20 20 re f") + BACKGROUND
    assert others(tmp_path, page, FORM, 2) == [2, 3]
    assert others(tmp_path, page, FORM, 3) == [0, 1, 3]


def test_a_mark_with_only_a_key_is_its_element_by_that_key(tmp_path: Path, backend: None) -> None:
    page = element(b"/k (logo)", b"/X0 Do") + element(b"/k (photo)", b"1 0 0 rg 10 10 20 20 re f") + BACKGROUND
    logo = group_id({"k": "logo"})
    assert logo is not None
    assert others(tmp_path, page, FORM, logo) == [2, 3], "by /n alone every object went off"


def test_the_form_around_a_marked_object_stays_on(tmp_path: Path, backend: None) -> None:
    inner = [(b"/BBox [0 0 200 150]", element(b"/k (a) /n 4", b"0 1 0 rg 60 60 10 10 re f"))]
    assert others(tmp_path, b"/X0 Do " + BACKGROUND, inner, 4) == [2]


def test_an_element_no_object_carries_switches_nothing_off(tmp_path: Path, backend: None) -> None:
    page = element(b"/k (a) /n 2", b"/X0 Do") + BACKGROUND
    assert others(tmp_path, page, FORM, 7) == [], "not a blank picture"
