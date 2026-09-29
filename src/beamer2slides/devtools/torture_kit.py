"""What the render tortures share: one PDF rendered by PDFium and by the pure reader, the pixels
they differ in, and shrinking a page that differs to the lines that still make it differ."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from typing import TypedDict, TypeVar

import numpy as np

from ..arrays import Ints, Pixels
from ..pdf.api import Box

Compared = tuple[int, Pixels, Pixels, Ints]
"""(pixels that differ, PDFium's render, pure's render, per-pixel max difference)."""
Refused = tuple[None, Pixels, None, str]
"""(None, PDFium's render, None, the pure reader's PdfError message)."""
Keep = Callable[[bytes], bool] | Collection[bytes]
"""Lines `drop_lines` never drops: a predicate, or the lines themselves."""
Fails = Callable[[bytes], bool]
E = TypeVar("E")
QQ = (b"q", b"Q")
"""The lines a path page keeps paired."""


class Stats(TypedDict):
    """What a torture `run` counts: the seeds apart, the refusals by reason, the pages drawn
    exactly."""
    failed: list[int]
    refused: dict[str, int]
    drawn: int


def pixel_diff(a: Pixels, b: Pixels) -> tuple[int, Ints]:
    """(pixels that differ, per-pixel max difference over the channels)."""
    if a.shape != b.shape:
        raise AssertionError(f"shapes {a.shape} != {b.shape}")
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), d


def compare_renders(data: bytes, zoom: float, transparent: bool) -> Compared:
    """`Compared` of page 0, rendered whole; a PdfError from the pure reader is raised."""
    return compare_clipped(data, zoom, transparent, None)


def compare_clipped(data: bytes, zoom: float, transparent: bool, clip: Box | None) -> Compared:
    """`Compared` of page 0 rendered in `clip` (None: whole); a PdfError is raised."""
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    ref, pure = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = ref[0].render(zoom, clip, transparent=transparent)
        b = pure[0].render(zoom, clip, transparent=transparent)
    finally:
        ref.close()
        pure.close()
    n, d = pixel_diff(a, b)
    return n, a, b, d


def compare_refusing(data: bytes, zoom: float, transparent: bool) -> Compared | Refused:
    """`Compared` of page 0 rendered whole, or `Refused` when the pure reader raises a PdfError."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    ref, pure = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = ref[0].render(zoom, None, transparent=transparent)
        try:
            b = pure[0].render(zoom, None, transparent=transparent)
        except PdfError as e:
            return None, a, None, str(e)
    finally:
        ref.close()
        pure.close()
    n, d = pixel_diff(a, b)
    return n, a, b, d


def outcome(result: Compared | Refused) -> Compared | str:
    """`compare_refusing`'s answer as the comparison, or the refusal's message."""
    n, a, b, d = result
    if isinstance(d, str):
        return d
    assert n is not None and b is not None, "a comparison has both renders"
    return n, a, b, d


def drop_lines(text: bytes, fails: Fails, keep: Keep) -> bytes:
    """Drop one line at a time, first to last and starting over after each drop, while
    `fails(text)` still holds. `keep`: lines never dropped (a collection, or a predicate), so
    q/Q and BT/ET stay paired (`QQ`)."""
    stays = keep if callable(keep) else keep.__contains__
    return _drop(text.split(b"\n"), fails, stays, b"\n")


def drop_tokens(text: bytes, fails: Fails) -> bytes:
    """`drop_lines` over single tokens (split on any whitespace, joined by spaces, nothing kept),
    for mutated pages whose culprit is one token in a line."""
    return _drop(text.split(), fails, _never, b" ")


def _never(part: bytes) -> bool:
    return False


def _drop(parts: list[bytes], fails: Fails, stays: Fails, sep: bytes) -> bytes:
    changed = True
    while changed:
        changed = False
        for i in range(len(parts)):
            if stays(parts[i]):
                continue
            trial = parts[:i] + parts[i + 1:]
            if fails(sep.join(trial)):
                parts, changed = trial, True
                break
    return sep.join(parts)


def drop_form_lines(content: bytes, forms: Sequence[tuple[E, bytes]],
                    fails: Callable[[bytes, list[tuple[E, bytes]]], bool],
                    tokens: bool) -> tuple[bytes, list[tuple[E, bytes]]]:
    """`drop_lines` (keeping q/Q; `tokens`: `drop_tokens`) over the page's content, then over each
    form's, `fails(content, forms)` judging the whole page; forms are (entries, content) pairs."""
    def drop(text: bytes, judge: Fails) -> bytes:
        return drop_tokens(text, judge) if tokens else drop_lines(text, judge, QQ)

    shrunk = list(forms)
    content = drop(content, lambda c: fails(c, shrunk))
    for k in range(len(shrunk)):
        entries = shrunk[k][0]
        shrunk[k] = (entries, drop(
            shrunk[k][1], lambda c: fails(content, shrunk[:k] + [(entries, c)] + shrunk[k + 1:])))
    return content, shrunk