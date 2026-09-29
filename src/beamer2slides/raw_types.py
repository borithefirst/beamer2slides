"""raw.json, written down: what `extract` hands `classify` for each page.

A key is required when `extract.extract_page` always writes it, optional (in the `total=False`
half of a type) when it writes it only where it holds: a span's `marks` on an adopted source's
page, a page's `hidden_text`. `frame_label` and `notes` are added to the page after extract
(`extract.extract`; `notes` by the callers that read speaker notes), so a page built elsewhere
may have neither.

Coordinates are PDF points from the page's top left. Classify's readers keep their `.get` with a
default for keys a test's hand-built page leaves out; the type says what extract writes.

Python 3.10, no typing_extensions: optional keys come by inheritance, as in `ir.py`.
"""

from typing import Literal, TypedDict

RawColor = str
"""'#rrggbb', lowercase."""

RawMark = list[str | dict[str, str | int]]
"""[tag, params] of one B2S mark on an adopted source's page (`marked.params` reads them)."""

PathItem = tuple[str, list[list[float]]]
"""One piece of a drawing's path: its operator ('re', 'l', 'c', 'qu') and its points [[x, y], ...]
('re': the two corners). In JSON a pair; typed as the tuple every reader unpacks it as
(`for op, pts in path`), and never tested as one."""

DrawingType = Literal["f", "s", "fs"]
"""Filled, stroked, or both."""


class _RawSpanKeys(TypedDict):
    id: str
    text: str
    font: str
    size: float
    color: RawColor
    alpha: float
    """0 (unseen) to 255."""
    origin: list[float]
    """[x, y] of the first glyph's origin, on its baseline."""
    bbox: list[float]
    dir: list[float]
    """The text's direction, [1, 0] for level text."""
    smallcaps: bool


class RawSpan(_RawSpanKeys, total=False):
    """A run of glyphs on one line in one font, size and colour, split at word gaps."""
    marks: list[RawMark]


class _RawImageKeys(TypedDict):
    id: str
    bbox: list[float]
    px: list[float]
    """[width, height] in pixels (a shadow piece's in points)."""


class RawImage(_RawImageKeys, total=False):
    marks: list[RawMark]


class _RawDrawingKeys(TypedDict):
    id: str
    """p<page>d<index into the page's drawings>: render finds the object by it."""
    type: DrawingType
    items: str
    """The path's operators run together ('re', 'lll', 'cccc')."""
    bbox: list[float]
    fill: RawColor | None
    stroke: RawColor | None
    width: float | None
    fill_opacity: float
    stroke_opacity: float
    soft_mask: bool
    """A soft mask or a blend mode (multiply)."""
    corners: dict[str, float]
    """Which bbox corners ('tl', 'br'...) are drawn with a curve, and the curve's radius."""
    path: list[PathItem] | None
    """None for a drawing of more than 20 pieces."""


class RawDrawing(_RawDrawingKeys, total=False):
    marks: list[RawMark]


class _RawLinkKeys(TypedDict):
    bbox: list[float]


class RawLink(_RawLinkKeys, total=False):
    """A link to a URL (`uri`) or to a page of the document (`page`, 0-based)."""
    uri: str
    page: int


class _RawPageKeys(TypedDict):
    index: int
    label: str
    """The page label (beamer: the frame number)."""
    size: list[float]
    """[width, height]."""
    spans: list[RawSpan]
    images: list[RawImage]
    drawings: list[RawDrawing]
    links: list[RawLink]


class RawPage(_RawPageKeys, total=False):
    hidden_text: list[str]
    """Words the page draws and does not show (beamer's transparent overlays)."""
    hidden_spans: list[RawSpan]
    """A marked page's hidden words, as spans."""
    frame_label: str | None
    """The frame's \\label (hyperref's anchor), if it has one."""
    notes: str | None
    """The page's speaker notes."""
