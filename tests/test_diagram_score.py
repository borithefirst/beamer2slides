"""The diagram scoreboard (`devtools/diagram_score.py`) on `tests/decks/29_tikz_diagrams.tex`: one
way of drawing TikZ per frame, and what each became. A change that makes a frame native, or
moves its refusal on to the next cause, updates `BOARD` here on purpose; a frame falling back
from native fails."""

from pathlib import Path
from typing import get_args

import pytest

from beamer2slides.classify_state import REFUSALS, Refusal
from beamer2slides.devtools import diagram_score
from beamer2slides.devtools.diagram_score import OUTCOMES, Outcome

PDF = Path(__file__).parent / "decks" / "out" / "29_tikz_diagrams.pdf"

# frame title -> (outcome, the reasons its pictures were refused)
BOARD: dict[str, tuple[Outcome, set[Refusal]]] = {
    "Straight pipeline": ("native", set()),
    "Flowchart with a decision and a loop back": ("native", set()),
    "Edge labels above and below the arrows": ("native", set()),
    "Curved edges: bend left and bend right": ("native", set()),
    "Edges with out and in angles": ("native", set()),
    "Dashed and dotted edges": ("native", set()),
    "Arrow tips: double, Latex, circle, bar": ("native", set()),
    "Self loops and parallel edges": ("native", set()),
    "Sloped labels along diagonal edges": ("picture", {"rotated_label"}),
    "Multi-line node text": ("native", set()),
    "Shapes: cylinder, ellipse, hexagon, circle": ("native", set()),
    "Shapes: cloud, document, chamfered, pill": ("native", set()),
    "Rotated ellipse and rotated rectangle": ("picture", {"rotated_node"}),
    "UML class: a multipart node": ("native", set()),
    "Nodes with drop shadows": ("picture", {"see_through"}),
    "Nodes with shaded fills": ("picture", {"image_inside"}),
    "Labels with math and subscripts": ("picture", {"scripted_label"}),
    "A dashed container around nodes (fit)": ("native", set()),
    "Filled background regions behind groups": ("native", set()),
    "Layered architecture stack": ("native", set()),
    "A tree with straight edges": ("native", set()),
    "An org chart with elbow edges": ("native", set()),
    "A neural network": ("native", set()),
    "A state machine (automata)": ("picture", {"scripted_label"}),
    "A state machine with word labels": ("native", set()),
    "A matrix of nodes with arrows": ("native", set()),
    "A chain with a fork and a join": ("native", set()),
    "A sequence diagram": ("native", set()),
    "A timeline with ticks and events": ("native", set()),
    "A brace over a group of steps": ("native", set()),
    "A Venn diagram": ("picture", {"nodes_cross"}),
    "A mind map": ("picture", {"unknown_shape"}),
    "A commutative diagram (tikz-cd)": ("picture", {"no_nodes"}),
    "A circuit (circuitikz)": ("picture", {"curve"}),
    "A control loop block diagram": ("picture", {"math_label"}),
    "Nodes revealed step by step": ("native", set()),
    "A system architecture": ("native", set()),
}


def test_every_refusal_and_outcome_is_listed() -> None:
    assert REFUSALS == get_args(Refusal)
    assert OUTCOMES == get_args(Outcome)


@pytest.mark.needs_decks("out/29_tikz_diagrams.pdf")
def test_the_tikz_board() -> None:
    board = diagram_score.score(PDF)
    got = {f.title: (f.outcome, {r.reason for r in f.refusals}) for f in board}
    assert got == BOARD
    again = diagram_score.board_of(diagram_score.board_json(board), "board")
    assert [(f.title, f.outcome, f.refusals) for f in again] == [(f.title, f.outcome, f.refusals) for f in board]
