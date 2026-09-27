"""What the render tortures share: one PDF rendered by PDFium and by the pure reader, the pixels
they differ in, and shrinking a page that differs to the lines that still make it differ."""

from __future__ import annotations

import numpy as np


def pixel_diff(a: np.ndarray, b: np.ndarray) -> tuple[int, np.ndarray]:
    """(pixels that differ, per-pixel max difference over the channels)."""
    if a.shape != b.shape:
        raise AssertionError(f"shapes {a.shape} != {b.shape}")
    d = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return int((d > 0).sum()), d


def compare_renders(data: bytes, zoom: float, transparent: bool, clip=None, refusals: bool = False):
    """(pixels that differ, PDFium's render, pure's render, per-pixel max difference) of page 0.
    `refusals`: a PdfError from the pure reader comes back as (None, PDFium's render, None, its
    message) rather than raised."""
    from ..pdf.api import PdfError
    from ..pdf.pdfium_backend import PdfiumBackend
    from ..pdf.pure.backend import PureBackend
    ref, pure = PdfiumBackend().open(data), PureBackend().open(data)
    try:
        a = ref[0].render(zoom, clip, transparent=transparent)
        try:
            b = pure[0].render(zoom, clip, transparent=transparent)
        except PdfError as e:
            if not refusals:
                raise
            return None, a, None, str(e)
    finally:
        ref.close()
        pure.close()
    n, d = pixel_diff(a, b)
    return n, a, b, d


def drop_lines(text: bytes, fails, keep=(b"q", b"Q"), sep: bytes = b"\n") -> bytes:
    """Drop one line at a time, first to last and starting over after each drop, while
    `fails(text)` still holds. `keep`: lines never dropped (a collection, or a predicate), so
    q/Q and BT/ET stay paired. `sep=b" "` drops single tokens instead (split on any whitespace,
    nothing kept), for mutated pages whose culprit is one token in a line."""
    tokens = sep == b" "
    parts = text.split() if tokens else text.split(sep)
    stays = (lambda part: False) if tokens else keep if callable(keep) else keep.__contains__
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


def drop_form_lines(content: bytes, forms, fails, sep: bytes = b"\n") -> tuple[bytes, list]:
    """`drop_lines` over the page's content, then over each form's, `fails(content, forms)` judging
    the whole page; forms are (entries, content) pairs."""
    forms = list(forms)
    content = drop_lines(content, lambda c: fails(c, forms), sep=sep)
    for k in range(len(forms)):
        entries = forms[k][0]
        forms[k] = (entries, drop_lines(
            forms[k][1], lambda c: fails(content, forms[:k] + [(entries, c)] + forms[k + 1:]), sep=sep))
    return content, forms
