"""The render tortures' shared shrinking and pixel comparison (devtools/torture_kit.py)."""

import numpy as np
import pytest

from beamer2slides.devtools.torture_kit import QQ, Fails, drop_form_lines, drop_lines, drop_tokens, pixel_diff


def culprit(*needles: bytes) -> Fails:
    def fails(text: bytes) -> bool:
        return all(n in text for n in needles)
    return fails


def test_lines_drop_down_to_the_culprit_and_the_kept_pairs() -> None:
    page = b"q\n1 0 0 RG\nBAD\n0 0 m\nQ"
    assert drop_lines(page, culprit(b"BAD"), QQ) == b"q\nBAD\nQ"


def test_kept_lines_can_be_a_predicate() -> None:
    page = b"BI /W 1\nID x\nEI\nBAD\nq"
    kept = drop_lines(page, culprit(b"BAD"), keep=lambda line: line.startswith(b"BI ") or line == b"EI")
    assert kept == b"BI /W 1\nEI\nBAD"


def test_tokens_mode_drops_single_tokens_and_keeps_nothing() -> None:
    assert drop_tokens(b"q 1 0 0 RG\nBAD w Q", culprit(b"BAD")) == b"BAD"


def test_a_drop_that_loses_the_difference_is_undone() -> None:
    # two lines needed together: neither goes
    assert drop_lines(b"A\nx\nB", culprit(b"A", b"B"), QQ) == b"A\nB"


def test_forms_shrink_after_the_page_with_the_whole_page_judging() -> None:
    seen: list[int] = []

    def fails(content: bytes, forms: list[tuple[str, bytes]]) -> bool:
        seen.append(len(forms))
        return b"P" in content and all(b"F" in body for _, body in forms)

    content, forms = drop_form_lines(b"x\nP\ny", [("one", b"a\nF"), ("two", b"F\nb")], fails, False)
    assert content == b"P"
    assert forms == [("one", b"F"), ("two", b"F")]
    assert set(seen) == {2}


def test_pixel_diff_counts_pixels_not_channels() -> None:
    a = np.zeros((2, 2, 4), np.uint8)
    b = a.copy()
    b[0, 0, :3] = 5
    b[1, 1, 3] = 1
    n, d = pixel_diff(a, b)
    assert n == 2 and d[0, 0] == 5 and d[1, 1] == 1


def test_pixel_diff_refuses_different_sizes() -> None:
    with pytest.raises(AssertionError):
        pixel_diff(np.zeros((2, 2, 4), np.uint8), np.zeros((2, 3, 4), np.uint8))
