"""The conversion's local half made faster without changing a byte: each shortcut against the
plain way it replaced (cluster_rects' sweep, PDFium's channel swap, threaded PNG writing, a
page's paths read once)."""

import random
from pathlib import Path

import numpy as np
import pytest

from beamer2slides import render
from beamer2slides.arrays import Pixels
from beamer2slides.classify_model import Rect, cluster_rects
from beamer2slides.pdf import Document
from beamer2slides.pdf.pdfium_backend import _swapped

HERE = Path(__file__).parent
BLOCKS = HERE / "decks" / "out" / "04_theme_blocks.pdf"


def pairwise_clusters(rects: list[Rect], gap: float) -> list[Rect]:
    """cluster_rects as it was: every pair asked."""
    parent = list(range(len(rects)))

    def find(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i
    for i in range(len(rects)):
        grown = rects[i].expand(gap)
        for j in range(i + 1, len(rects)):
            if grown.intersects(rects[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[Rect]] = {}
    for i, r in enumerate(rects):
        groups.setdefault(find(i), []).append(r)
    out: list[Rect] = []
    for members in groups.values():
        out.append(Rect(min(r.x0 for r in members), min(r.y0 for r in members),
                        max(r.x1 for r in members), max(r.y1 for r in members)))
    return out


@pytest.mark.parametrize("seed", range(12))
def test_the_sweep_clusters_as_every_pair_did(seed: int) -> None:
    rng = random.Random(seed)
    rects: list[Rect] = []
    for _ in range(rng.randint(0, 120)):
        x, y = rng.uniform(0, 300), rng.uniform(0, 200)
        # (edges that touch exactly, zero-width marks and repeated boxes too)
        w, h = rng.choice([0.0, 1.0, rng.uniform(0, 30)]), rng.choice([0.0, rng.uniform(0, 10)])
        x = round(x) if rng.random() < 0.3 else x
        rects.append(Rect(x, y, x + w, y + h))
        if rng.random() < 0.1:
            rects.append(Rect(x, y, x + w, y + h))
    for gap in (0.0, 0.5, 3.0, 12.0):
        assert cluster_rects(rects, gap) == pairwise_clusters(rects, gap)


def test_unbounded_boxes_are_still_clustered_pair_by_pair() -> None:
    rects = [Rect(-float("inf"), 0, 5, 5), Rect(100, 1, 110, 4), Rect(3, 2, 8, 9), Rect(200, 0, float("inf"), 1)]
    assert cluster_rects(rects, 1.0) == pairwise_clusters(rects, 1.0)


@pytest.mark.parametrize("channels", [3, 4])
def test_the_channel_swap_is_the_reversed_copy(channels: int) -> None:
    rng = np.random.default_rng(channels)
    bgr = rng.integers(0, 256, (37, 53, channels), dtype=np.uint8)
    want = np.concatenate([bgr[..., 2::-1], bgr[..., 3:]], axis=2) if channels == 4 else bgr[..., ::-1]
    got = _swapped(bgr, channels)
    assert got.dtype == np.uint8 and got.flags.c_contiguous and np.array_equal(got, want)


def test_threaded_pngs_are_the_same_files(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    pictures = [rng.integers(0, 256, (40 + k, 60, 3), dtype=np.uint8) for k in range(12)]
    for workers in (0, 3):
        writer = render.PngWriter(workers)
        try:
            for k, img in enumerate(pictures):
                writer.save(img, tmp_path / str(workers) / f"{k}.png")
            writer.finish()
        finally:
            writer.close()
    for k in range(len(pictures)):
        assert (tmp_path / "0" / f"{k}.png").read_bytes() == (tmp_path / "3" / f"{k}.png").read_bytes()


def test_the_writer_saves_through_save_png_as_it_is_when_it_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kept: list[Path] = []

    def keep(img: Pixels, path: Path) -> None:
        kept.append(path)
    writer = render.PngWriter(2)
    try:
        monkeypatch.setattr(render, "save_png", keep)  # (checks.convert_locally keeps pictures so)
        writer.save(np.zeros((2, 2, 3), np.uint8), tmp_path / "a.png")
        writer.finish()
    finally:
        writer.close()
    assert kept == [tmp_path / "a.png"] and not (tmp_path / "a.png").exists()


def test_a_failed_write_is_raised(tmp_path: Path) -> None:
    def fail() -> None:
        raise OSError("disk full")
    writer = render.PngWriter(2)
    try:
        writer.submit(fail)
        with pytest.raises(OSError, match="disk full"):
            writer.finish()
    finally:
        writer.close()


@pytest.mark.needs_decks("out/04_theme_blocks.pdf")
def test_a_page_read_twice_draws_the_same_and_shares_nothing() -> None:
    doc = Document(BLOCKS)
    try:
        for page in doc:
            first, second = page.drawings(), page.drawings()
            assert first == second
            assert all(a["items"] is not b["items"] for a, b in zip(first, second))
            assert page.object_bounds() == page.object_bounds()
    finally:
        doc.close()
