"""A picture the API draws smaller than its own frame - a .pptx's negative `srcRect` (padding),
which `imageProperties.cropProperties` never carries - is found from the slide's own thumbnail:
`deck_thumbs.thumbnail_picture_places` compares the frame stretched over the whole file against
some inset box of it, and moves the element's `bbox`/`box` there when that reads clearly better
(applied-ml slide 43: a 515x484 chart stretched 1.56x too wide over its 8.9-421pt frame, where
Google's own thumbnail draws it only across roughly x 82..346)."""

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from beamer2slides import deck_thumbs
from beamer2slides.arrays import RGB, RGBA, Gray
from beamer2slides.deck_ir_types import TargetElement, TargetImage
from beamer2slides.json_types import Json, JsonObject

from .deck_records import records

PAGE_BG = (245, 245, 245)


def checker(w: int, h: int) -> Gray:
    """A non-flat greyscale pattern (blocks of two shades) - enough texture for a correlation
    score to mean something, unlike a flat fill."""
    xs = (np.arange(w) // 9) % 2
    ys = (np.arange(h) // 7) % 2
    board = (xs[None, :] ^ ys[:, None]).astype(np.uint8)
    return (board * 200 + 30).astype(np.uint8)


def page(w: int, h: int) -> RGB:
    return np.full((h, w, 3), PAGE_BG, dtype=np.uint8)


def paste_resized(thumb: RGB, gray: Gray, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    im = Image.fromarray(gray, "L").resize((x1 - x0, y1 - y0), Image.Resampling.BILINEAR).convert("RGB")
    thumb[y0:y1, x0:x1] = np.asarray(im)


def save_png(path: Path, gray: Gray) -> str:
    Image.fromarray(gray, "L").convert("RGB").save(path)
    return str(path)


def image_el(file: str, bbox: list[float], extra: Mapping[str, Json]) -> JsonObject:
    return {"kind": "image", "role": "figure", "id": "pic", "object": "pic", "group": None,
            "bbox": list(bbox), "file": file, **extra}


def placed(els: list[JsonObject], thumb: RGB | None, px: float) -> list[TargetElement]:
    """`deck_thumbs.thumbnail_picture_places` over the records `els` stand for (the thumbnail
    widened to int16, as the passes read one)."""
    return deck_thumbs.thumbnail_picture_places(records(els), None if thumb is None else thumb.astype(np.int16), px)


def picture(el: TargetElement) -> TargetImage:
    assert isinstance(el, TargetImage)
    return el


# The frame is a 300 x 200 pt box at (50, 50); px == 1.0 keeps pt and pixels the same, so the
# numbers above the test bodies are the ones a reader can check against the module's own thresholds.
FRAME = (50.0, 50.0, 350.0, 250.0)


def test_a_letterboxed_picture_is_placed_at_its_narrower_box(tmp_path: Path) -> None:
    src = checker(120, 60)
    png = save_png(tmp_path / "chart.png", src)
    thumb = page(450, 350)
    drawn = (110, 50, 290, 250)          # 20% inset each side, full height: the padding case
    paste_resized(thumb, src, drawn)

    el = picture(*placed([image_el(png, list(FRAME), {})], thumb, 1.0))

    assert el.picture_place == "thumbnail"
    x0, y0, x1, y1 = el.bbox
    assert x0 == pytest.approx(110, abs=6) and x1 == pytest.approx(290, abs=6)
    assert y0 == pytest.approx(50, abs=3) and y1 == pytest.approx(250, abs=3)
    assert el.box == el.bbox


def test_a_stretch_that_already_reads_well_is_left_alone(tmp_path: Path) -> None:
    src = checker(120, 60)
    png = save_png(tmp_path / "chart.png", src)
    thumb = page(450, 350)
    paste_resized(thumb, src, (50, 50, 350, 250))   # fills the whole frame: no padding at all

    el = picture(*placed([image_el(png, list(FRAME), {})], thumb, 1.0))

    assert el.bbox == FRAME
    assert el.picture_place is None


def test_a_covering_element_above_is_left_out_of_the_comparison(tmp_path: Path) -> None:
    """A label drawn over the picture (applied-ml's rotated white "Loss function" box, sitting
    exactly where the stretch would otherwise put the chart's own words) must not be read as part
    of the picture's own ink - or as a reason the true, narrower box reads worse than the stretch."""
    src = checker(120, 60)
    png = save_png(tmp_path / "chart.png", src)
    thumb = page(450, 350)
    drawn = (110, 50, 290, 250)
    paste_resized(thumb, src, drawn)
    cover_bbox: list[Json] = [170.0, 120.0, 230.0, 180.0]
    thumb[120:180, 170:230] = (255, 255, 255)   # a plain label box painted over part of the chart
    label: JsonObject = {"kind": "text", "bbox": cover_bbox, "paragraphs": [{"runs": [{"text": "Loss function"}]}]}

    first, _ = placed([image_el(png, list(FRAME), {}), label], thumb, 1.0)
    el = picture(first)

    assert el.picture_place == "thumbnail"
    x0, y0, x1, y1 = el.bbox
    assert x0 == pytest.approx(110, abs=6) and x1 == pytest.approx(290, abs=6)


EXTRAS: list[JsonObject] = [{"rotation": 12.0}, {"flip": True}, {"crop": {"l": 0.1, "t": 0, "r": 0, "b": 0}},
                            {"video": {"source": "YOUTUBE", "id": "y"}}, {"chart": {"spreadsheetId": None, "chartId": 1}}]


@pytest.mark.parametrize("extra", EXTRAS)
def test_what_is_never_moved_this_way(extra: JsonObject, tmp_path: Path) -> None:
    src = checker(120, 60)
    png = save_png(tmp_path / "chart.png", src)
    thumb = page(450, 350)
    paste_resized(thumb, src, (110, 50, 290, 250))

    el = picture(*placed([image_el(png, list(FRAME), extra)], thumb, 1.0))

    assert el.bbox == FRAME and el.picture_place is None


def test_a_frame_too_small_to_search_is_left_alone(tmp_path: Path) -> None:
    src = checker(120, 60)
    png = save_png(tmp_path / "chart.png", src)
    thumb = page(80, 80)
    tiny = (10.0, 10.0, 30.0, 20.0)
    paste_resized(thumb, src, (14, 10, 26, 20))

    el = picture(*placed([image_el(png, list(tiny), {})], thumb, 1.0))

    assert el.bbox == tiny and el.picture_place is None


def test_without_a_thumbnail_nothing_changes(tmp_path: Path) -> None:
    png = save_png(tmp_path / "chart.png", checker(120, 60))
    el = picture(*placed([image_el(png, list(FRAME), {})], None, 1.0))
    assert el.bbox == FRAME and el.picture_place is None


def test_a_missing_file_is_left_alone(tmp_path: Path) -> None:
    thumb = page(450, 350)
    el = picture(*placed([image_el(str(tmp_path / "does-not-exist.png"), list(FRAME), {})], thumb, 1.0))
    assert el.bbox == FRAME and el.picture_place is None


def save_rgba(path: Path, size: int) -> RGBA:
    """A circular badge inscribed in its own square canvas (devfest2020's flag icons: a 2048x2048
    PNG, ~22% of it transparent corners) - a chequered disc on a fully transparent, *black* ground,
    the way many icon exporters leave the colour channel under alpha 0."""
    ys, xs = np.mgrid[0:size, 0:size]
    inside = (xs - size / 2) ** 2 + (ys - size / 2) ** 2 <= (size / 2) ** 2
    board = (((xs // 9) % 2) ^ ((ys // 7) % 2)).astype(np.uint8) * 200 + 30
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 0] = np.where(inside, board, 0)
    rgba[..., 1] = np.where(inside, board, 0)
    rgba[..., 2] = np.where(inside, board, 0)
    rgba[..., 3] = np.where(inside, 255, 0)
    Image.fromarray(rgba, "RGBA").save(path)
    return rgba


def test_a_transparent_icon_already_sized_to_its_frame_is_left_alone(tmp_path: Path) -> None:
    """devfest2020 slide 39: 44x44pt frames holding a 2048x2048 RGBA flag badge, a circle cut by
    alpha out of its own square canvas with black under the transparent corners. Read as plain
    greyscale (alpha ignored), those black corners look nothing like the thumbnail's page-coloured
    corners, so a stretch that is already exactly right scored as a bad letterbox and the search
    went hunting for a smaller box - shrinking already-round icons into ovals. Compositing the
    source onto the page colour before scoring must keep this one exactly where it was."""
    rgba = save_rgba(tmp_path / "flag.png", 88)
    thumb = page(450, 350)
    disc_rgb = np.repeat(rgba[..., :3], 1, axis=2)
    alpha = rgba[..., 3:4].astype(np.float32) / 255.0
    composited = (disc_rgb.astype(np.float32) * alpha + np.array(PAGE_BG, dtype=np.float32) * (1 - alpha))
    frame = Image.fromarray(composited.astype(np.uint8), "RGB").resize((300, 200), Image.Resampling.BILINEAR)
    thumb[50:250, 50:350] = np.asarray(frame)

    el = picture(*placed([image_el(str(tmp_path / "flag.png"), list(FRAME), {})], thumb, 1.0))

    assert el.bbox == FRAME and el.picture_place is None


def test_alpha_compositing_is_what_reads_the_icon_as_already_right(tmp_path: Path) -> None:
    """Pins the defect at the score itself, not just the end-to-end outcome above: a bare greyscale
    conversion of the icon (alpha dropped, its transparent corners read as the black RGB sitting
    under them) scores this exact frame as a bad stretch - the opening the search used to shrink
    devfest2020's flag badges into ovals - while compositing onto the page's own colour first (what
    `thumbnail_picture_places` now does) reads it as clearly correct."""
    from beamer2slides.deck_fills import px_box
    rgba = save_rgba(tmp_path / "flag.png", 88)
    thumb = page(450, 350)
    alpha = rgba[..., 3:4].astype(np.float32) / 255.0
    composited = rgba[..., :3].astype(np.float32) * alpha + np.array(PAGE_BG, dtype=np.float32) * (1 - alpha)
    frame_img = Image.fromarray(composited.astype(np.uint8), "RGB").resize((300, 200), Image.Resampling.BILINEAR)
    thumb[50:250, 50:350] = np.asarray(frame_img)

    a0, b0, a1, b1 = px_box(FRAME, 1.0, 450, 350, 0)
    frame_gray = (thumb[b0:b1, a0:a1].astype(np.float32)
                  * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2)
    visible = deck_thumbs._place_visible([], (a0, b0, a1, b1), 1.0, 450, 350)

    naive_grey = np.asarray(Image.fromarray(rgba, "RGBA").convert("L"), dtype=np.float32)
    naive_grid = deck_thumbs._place_resize(naive_grey, deck_thumbs.PLACE_GRID, deck_thumbs.PLACE_GRID)
    naive_score = deck_thumbs._place_score(frame_gray, visible, naive_grid, 0.0, 0.0, 0.0, 0.0)

    fixed_alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    fixed_rgb = rgba[:, :, :3].astype(np.float32) * fixed_alpha + 255.0 * (1.0 - fixed_alpha)
    fixed_grey = (fixed_rgb * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2)
    fixed_grid = deck_thumbs._place_resize(fixed_grey, deck_thumbs.PLACE_GRID, deck_thumbs.PLACE_GRID)
    fixed_score = deck_thumbs._place_score(frame_gray, visible, fixed_grid, 0.0, 0.0, 0.0, 0.0)

    assert naive_score is not None and naive_score < deck_thumbs.PLACE_STRETCHED_OK
    assert fixed_score is not None and fixed_score >= deck_thumbs.PLACE_STRETCHED_OK
