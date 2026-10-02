"""What one page's classification knows, declared once: the state `PageClassifier`'s mixins share.

Every mixin inherits from `PageState`, one after the other in the order they call each other
(tables, graphics, lines, paragraphs, figures, reasons, then `PageClassifier`), so each method
sees, typed, every attribute and every method of the mixins before it. An attribute is set here
to its empty value and filled by the step that finds it (`analyse_graphics`, `classify`...): a
method run before that step (a test's `build_lines` on a bare page) reads nothing rather than
failing on a missing attribute.

The records the steps pass between them (panels, their frames, a table's rules) are frozen
dataclasses; what becomes deck.json is built as `ir.py` says, elsewhere.
"""

from dataclasses import dataclass
from typing import Literal

from .classify_model import Line, Paragraph, Rect, Span
from .raw_types import RawColor, RawDrawing, RawImage, RawPage

Refusal = Literal[
    "too_large",            # the cluster is most of the page (or more): left in the background
    "over_text",            # drawn over native text: an overlay or nothing
    "image_inside",         # a raster image (or a shading) among the drawings
    "other_text",           # text inside that is none of its labels
    "unreadable_path",      # a drawing whose path extract could not read
    "see_through",          # opacity or a soft mask (a shadow)
    "curve",                # a stroked line with Bezier segments (bend, in/out, loops, braces)
    "unknown_shape",        # a closed path that is no node shape we know
    "rotated_node",         # a turned node whose words stand level (shape border rotate) or a turned card
    "no_nodes",             # lines and labels only
    "nodes_cross",          # nodes overlapping without nesting (a Venn diagram)
    "loose_arrow_tip",      # an arrow head at no line's end
    "math_label",           # a label with a big operator, a fraction or a radical
    "icon_label",           # a label in an icon font, or with no Unicode
    "rotated_label",        # a label set vertically or upside down, or sloped in an upright node, across a
                            # turned one, or in two directions in one node (classify_figures.sloped_labels)
    "too_many_rectangles",  # a QR code, a pixel grid
    "splits_cells",         # a ruled grid table_from could not read
]
"""Why a figure cluster did not become a native diagram (`DiagramRefusal`)."""
REFUSALS: tuple[Refusal, ...] = (
    "too_large", "over_text", "image_inside", "other_text", "unreadable_path", "see_through", "curve",
    "unknown_shape", "rotated_node", "no_nodes", "nodes_cross", "loose_arrow_tip", "math_label", "icon_label",
    "rotated_label", "too_many_rectangles", "splits_cells")
"""Every `Refusal`, to read one back from JSON (`tests/test_diagram_score.py` holds them equal)."""


@dataclass(frozen=True, kw_only=True)
class DiagramRefusal:
    """A figure cluster that stays a picture (or in the background) instead of becoming a
    diagram, and why: what an experiment on diagrams counts (`devtools/diagram_score.py`).
    `detail` names the drawing or label that decided it."""
    box: Rect
    reason: Refusal
    detail: str


@dataclass(frozen=True, kw_only=True)
class Frame:
    """The rules drawn around a box (`frame_of`): an \\fcolorbox's, a listing's frame=single."""
    color: RawColor
    width: float
    sides: str
    """Which of 'l', 't', 'r', 'b' are drawn."""
    box: list[float]
    """[left, top, right, bottom]: the rules' centre lines (the box's edge where a side is missing)."""
    rules: list[list[float]]
    """Each side's pieces joined where they touch, as boxes."""
    ids: list[str]
    """The drawings the frame is made of (and rules in the fill's own colour over it)."""


@dataclass(frozen=True, kw_only=True)
class BareFrame:
    """Rules closing a rectangle around code with nothing filled under them (`rule_frames`)."""
    bbox: Rect
    frame: Frame
    id: str
    """Its first rule's drawing."""


@dataclass(frozen=True, kw_only=True)
class Panel:
    """A filled box text may stand on (a beamer block, a theme bar, a wide image), or a bare
    frame's box. `slide["panels"]` lists them; a text element names its own by index."""
    bbox: Rect
    fill: RawColor | None
    id: str
    """The drawing (or image) it is."""
    rounded: bool
    corners: dict[str, float]
    opacity: float
    image: bool
    frame: Frame | None
    tiles: list[str]
    """The drawings of a stack of fills painted as this one (a listing's line bands)."""


@dataclass(frozen=True, kw_only=True)
class Rule:
    """A horizontal or vertical rule of a table (or one that may be): its box, colour, thickness."""
    rect: Rect
    color: RawColor
    weight: float


@dataclass(frozen=True, kw_only=True)
class Fill:
    """A table's cell shading (\\rowcolor, \\cellcolor)."""
    rect: Rect
    color: RawColor


PathKey = tuple[float, ...]
"""A graphic's box as `Rect.as_list` rounds it: how `graphic_paths` finds its drawing."""


class PageState:
    """The page (`raw`), its body size and size, and what each step has found on it so far."""

    def __init__(self, raw: RawPage, body: float) -> None:
        self.raw: RawPage = raw
        self.body: float = body
        self.W: float = raw["size"][0]
        self.H: float = raw["size"][1]
        # graphics (analyse_graphics)
        self.panels: list[Panel] = []
        self.regions: list[Rect] = []
        """Figure regions: clusters of graphics."""
        self.bars: list[Rect] = []
        """Fraction bars and radical overbars."""
        self.small_images: list[tuple[RawImage, Rect]] = []
        self.decorations: list[Rect] = []
        self.graphics: list[Rect] = []
        self.graphic_drawings: dict[str, Rect] = {}
        """Drawing id -> box, for the drawings among the graphics."""
        self.graphic_paths: dict[PathKey, RawDrawing] = {}
        """A graphic's box -> its drawing."""
        self.table_rules: list[list[Rule]] = []
        """Groups of two or more horizontal rules of one extent."""
        self.frames: dict[str, Frame] = {}
        """A fill's drawing id -> the frame drawn around it."""
        self.frame_ids: set[str] = set()
        self.bare_frames: list[BareFrame] = []
        self.analysed: bool = False
        """Whether `analyse_graphics` has run: `artwork_of` knows nothing before."""
        self._artwork: list[tuple[Rect, bool]] | None = None
        self.title_bridges: list[Rect] = []
        """What joins a plot's title to its plot (`axis_titles`)."""
        self.column_bridges: list[Rect] = []
        """What joins a column of axis labels to its plot (`axis_label_column`)."""
        # decorations on words (text_decorations, underscores)
        self.decor_ids: set[str] = set()
        self.decor_rects: dict[str, list[Rect]] = {}
        """Span id -> the drawings styling it (underlines, strikes, highlights)."""
        # lines and their reasons (assign_reasons)
        self.spans_by_id: dict[str, Span] = {}
        self.all_lines: list[Line] = []
        self._word_graphics: list[Rect] | None = None
        # paragraphs (build_paragraphs)
        self.text_margin: float = 0.0
        self.hfill_pieces: dict[int, Line] = {}
        """Id of a line -> the piece an \\hfill pushed to the right end of its row."""
        self.hfill_hosts: set[int] = set()
        """Ids of those pieces."""
        self.join_indent: float = 0.0
        self.built_paragraphs: list[Paragraph] = []
        # elements (classify)
        self.line_owner: dict[int, tuple[str, str]] = {}
        """Id of a line -> (its text element's id, its paragraph's alignment)."""
        self.hole_boxes: list[Rect] = []
        self.bullet_boxes: list[Rect] = []
        self.icon_bullets: list[Rect] = []
        """Item labels that became pictures."""
        self.diagram_refusals: list[DiagramRefusal] = []
        """Figure clusters that did not become native diagrams, and why (`figures`)."""
