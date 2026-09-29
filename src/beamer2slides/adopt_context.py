"""What adopt learns about a deck while it writes the source: the typefaces, text and shape styles,
list levels and the deck's usual size, colour and inset, on top of what inverse's `Context` carries
(colours, packages, pictures). Declared here, in one place, for `adopt` and `adopt_shapes` alike;
`adopt_context()` is a fresh one, holding nothing yet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from .inverse import Context


class _MissingFontKeys(TypedDict):
    font: str
    kind: str
    letters: int
    set_in: str


class MissingFont(_MissingFontKeys, total=False):
    """A font the deck names that is set in something else (`adopt.font_preamble`): which, its kind
    (sans/serif/mono), how many letters of the deck, what they are set in instead. With `script`
    (`scripts.script_preamble`), `kind` is a script no font anywhere draws, `font`/`set_in` empty.
    Serialised as it is (`adopt.cmd_adopt`'s `found["missing"]`, the agent's `fonts_missing`)."""
    script: bool


Metrics = tuple[str, str, str]
"""A paragraph's line box as `\\slidestyle` says it: ascent, pitch, depth (bp)."""

StyleKey = tuple[str, str, str, str, bool, str, Metrics | None]
"""What names a text style (`adopt.text_style`): size, family, face, weight, italic, colour name,
line box (None for a bullet's label)."""

StyleRef = tuple[str, StyleKey]
"""A text style as a paragraph's keys hold it, ("S", key), until the frame is written."""


@dataclass(kw_only=True)
class AdoptContext(Context):
    font_switches: dict[str, str]
    """The deck's second typefaces: font -> the `\\newfontfamily` command `font_preamble` made."""
    font_weights: dict[str, set[tuple[int, bool]]]
    """font -> the (weight, italic) faces `weight_faces` declared besides regular and bold."""
    missing_fonts: list[MissingFont]
    """The fonts the deck names that were set in something else (`font_preamble`)."""
    font_lines: list[str] | None
    """The preamble's fontspec lines, once `bootstrap` has made them."""
    line_struts: float | None
    """While a paragraph of several sizes is written: its line spacing (`run_tex`'s struts)."""
    text_styles: dict[StyleKey, str]
    """Each text style's name, in the order the frames first use them (`text_style`)."""
    last_style_key: StyleKey | None
    """The key of the style `text_style` named last (what `box_parts` records)."""
    bullet_marks: dict[str, str]
    """A drawn bullet's code -> its `\\slidemark` name (`bullet_mark`)."""
    body_size: float | None
    main_colour: str | None
    slide_inset: str | None
    """What most of the deck's words are: their size, their colour, their boxes' top inset
    (`deck_text_defaults`)."""
    deck_style: StyleRef | None
    """The deck's paragraph style (`\\setslidepar`), where most paragraphs share one."""
    list_levels: dict[tuple[str, int], dict[str, object]]
    """(environment, depth) -> what most items of that list level say (`\\setslidelist`)."""
    shape_styles: dict[str, str]
    """TikZ keys -> the name the deck's often-drawn shape look goes by (`survey_styles`)."""
    shape_style_count: dict[str, int] | None
    """While `survey_styles` runs (on a scratch context): how often each look is drawn."""


def adopt_context() -> AdoptContext:
    """A context that has learnt nothing about the deck yet."""
    return AdoptContext(font_switches={}, font_weights={}, missing_fonts=[], font_lines=None, line_struts=None,
                        text_styles={}, last_style_key=None, bullet_marks={}, body_size=None, main_colour=None,
                        slide_inset=None, deck_style=None, list_levels={}, shape_styles={}, shape_style_count=None)
