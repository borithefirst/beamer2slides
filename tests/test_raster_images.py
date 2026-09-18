"""The lossless route for `\\includegraphics`: a figure region that is one embedded image
reaches Slides as that image's own file, not as a render of the page.

Offline, on tests/decks/out/23_raster_images.pdf (built from 23_raster_images.tex, whose six
frames hold one case each) and 07_images.pdf. The decision table of render.image_file is the
point: which image objects qualify, what file they give, and that the picture lands in the same
box the rendered crop uses.
"""

import hashlib
import json
from pathlib import Path

import pytest

from beamer2slides.classify import classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.pdf import OBJ_IMAGE, Document
from beamer2slides.render import _looks_like, image_file, render_backgrounds, sole_image

HERE = Path(__file__).resolve().parent
DECK = HERE / "decks" / "out" / "23_raster_images.pdf"
IMAGES = HERE / "decks" / "img"

pytestmark = pytest.mark.skipif(not DECK.exists(), reason="build tests/decks/23_raster_images.tex first")

# page (1-based) -> the route each image object on it takes, in drawing order.
# (frame 1 photo, 2 CMYK + greyscale, 3 alpha + palette, 4 turned + mirrored,
#  5 clipped + faded, 6 photo with a label)
ROUTES = {1: ["raw"], 2: ["decoded", "decoded"], 3: [None, "decoded"],
          4: [None, None], 5: [None, None], 6: ["raw"]}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@pytest.fixture(scope="module")
def doc():
    d = Document(DECK)
    yield d
    d.close()


def image_objects(page):
    return [po.id for po in page.objects() if po.type == OBJ_IMAGE]


def test_routes(doc):
    """Every image of the deck takes the route its case calls for."""
    for page in doc:
        want = ROUTES[page.index + 1]
        got = []
        for po in image_objects(page):
            chosen = image_file(page, po)
            got.append(None if chosen is None else chosen[3])
        assert got == want, f"page {page.index + 1}"


def test_raw_route_gives_the_authors_file(doc):
    """A plain JPEG comes out byte for byte as the author included it, at its own size."""
    wanted = {1: ("photo_big.jpg", (2400, 1600)), 6: ("photo.jpg", (1200, 800))}
    for number, (name, px) in wanted.items():
        page = doc[number - 1]
        data, ext, size, route = image_file(page, image_objects(page)[0])
        assert (route, ext, size) == ("raw", "jpg", px)
        assert sha1(data) == sha1((IMAGES / name).read_bytes())


def test_decoded_route_keeps_the_native_resolution(doc):
    """A Flate image (and a JPEG PDFium reads differently, the CMYK one) becomes a PNG of
    PDFium's own pixels, at the image's pixel size -- not the page's."""
    for number, sizes in ((2, [(600, 400), (800, 600)]), (3, [(800, 600)])):
        page = doc[number - 1]
        got = [(chosen[1], chosen[2]) for po in image_objects(page)
               if (chosen := image_file(page, po)) is not None]
        assert got == [("png", s) for s in sizes]


def test_rejected_images_are_what_they_claim(doc):
    """The cases that must not take the image's own data, and why."""
    reasons = {(3, 0): "transparent", (4, 0): "upright", (4, 1): "upright",
               (5, 0): "clipped", (5, 1): "blended"}
    for (number, index), field in reasons.items():
        im = doc[number - 1].embedded_image(image_objects(doc[number - 1])[index])
        wrong = not im.upright if field == "upright" else getattr(im, field)
        assert wrong, f"page {number} image {index} should be rejected for {field}"


def test_looks_like_has_teeth(doc):
    """The last check really compares the file with the page: the right file passes, another
    deck image in its place does not."""
    page, other = doc[0], doc[1]
    po = image_objects(page)[0]
    data, _, _, _ = image_file(page, po)
    bbox = list(page.images()[0]["bbox"])
    assert _looks_like(data, page, po, bbox)
    wrong, _, _, _ = image_file(other, image_objects(other)[1])
    assert not _looks_like(wrong, page, po, bbox)


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    out = tmp_path_factory.mktemp("raster")
    raw = select_overlays(extract(DECK), "last")
    deck = classify(raw)
    render_backgrounds(DECK, raw, deck, out)
    return out, raw, deck


def figures(deck):
    return {(s["page"], e["id"]): e for s in deck["slides"] for e in s["elements"] if e["kind"] == "image"}


def test_bare_regions_are_the_image_box(converted):
    """A region that is one `\\includegraphics` carries the raw image's id and its very box, so
    the picture -- the embedded file or a crop of the page -- lands where the image is drawn."""
    out, raw, deck = converted
    boxes = {i["id"]: i["bbox"] for p in raw["pages"] for i in p["images"]}
    bare = {el["image"]: el for el in figures(deck).values() if el.get("image")}
    assert len(bare) == 9, sorted(bare)  # every image of the deck but the labelled one
    for image_id, el in bare.items():
        assert el["bbox"] == boxes[image_id]
    labelled = [el for el in figures(deck).values() if not el.get("image")]
    assert len(labelled) == 1 and labelled[0]["spans"]  # frame 6: the caption keeps the crop


def test_pictures_written(converted):
    """What each figure's picture file turns out to be."""
    out, raw, deck = converted
    by_page = {}
    for (page, _), el in sorted(figures(deck).items()):
        by_page.setdefault(page, []).append(el)
    assert [el.get("picture") for el in by_page[0]] == ["raw"]
    assert [el.get("picture") for el in by_page[1]] == ["decoded", "decoded"]
    assert [el.get("picture") for el in by_page[2]] == [None, "decoded"]      # the logo's alpha: a crop
    assert [el.get("picture") for el in by_page[3]] == [None, None]
    assert [el.get("picture") for el in by_page[4]] == [None, None]
    assert [el.get("picture") for el in by_page[5]] == [None]                 # labelled: a crop

    photo = by_page[0][0]
    assert photo["file"].endswith(".jpg") and photo["px"] == [2400, 1600]
    assert sha1((out / photo["file"]).read_bytes()) == sha1((IMAGES / "photo_big.jpg").read_bytes())
    # The picture fills its box: same shape as the region, so Slides does not squeeze it.
    x0, y0, x1, y1 = photo["bbox"]
    assert abs(photo["px"][0] / photo["px"][1] - (x1 - x0) / (y1 - y0)) < 0.01
    # Every picture is on disk and deck.json can carry it.
    for el in figures(deck).values():
        assert (out / el["file"]).exists()
    json.dumps(deck)


def test_crop_and_file_place_the_picture_alike(converted):
    """The box does not depend on the route: a rendered crop of the region shows the same
    picture as the file the image gave (render._looks_like compares them on the page)."""
    out, raw, deck = converted
    doc = Document(DECK)
    try:
        for (page, _), el in figures(deck).items():
            if not el.get("picture"):
                continue
            po = sole_image(doc[page], el["bbox"])
            assert po is not None
            assert _looks_like((out / el["file"]).read_bytes(), doc[page], po, el["bbox"])
    finally:
        doc.close()
