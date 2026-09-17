"""Unit tests for card text and column gutters (synthetic spans), plus the gdg talk deck when built
(`themes/google/make.ps1 themes/google/examples/gdg-talk.tex out/themes/gdg`)."""

from pathlib import Path

import pytest

from beamer2slides.classify import PageClassifier, Rect, Span, card_text, classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.fonts import font_info

GDG = Path(__file__).resolve().parents[1] / "out" / "themes" / "gdg"
FONT = "GoogleSansFlex-Regular"


def span(text: str, x0: float, baseline: float, size: float, w: float | None = None) -> Span:
    w = len(text) * 0.5 * size if w is None else w
    return Span(id=f"s{x0:.0f}-{baseline:.0f}", text=text, font=FONT, size=size, color="#000000",
                rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                baseline=baseline, horizontal=True, info=font_info(FONT))


def centred(text: str, cx: float, baseline: float, size: float) -> Span:
    return span(text, cx - len(text) * 0.25 * size, baseline, size)


def test_centred_label_is_not_card_text():
    node = Rect(400, 100, 700, 200)
    assert card_text(node, [[centred("Label", node.cx, 155, 14)]]) is None


def test_caption_tucked_under_a_big_number_gets_its_own_box():
    node = Rect(300, 100, 700, 380)
    number, cx = centred("92%", 500, 250, 96), 500
    tight = card_text(node, [[number], [centred("of summaries", cx, 280, 14)]])
    assert [len(b["paragraphs"]) for b in tight] == [1, 1]
    loose = card_text(node, [[number], [centred("of summaries", cx, 300, 14)]])
    assert [len(b["paragraphs"]) for b in loose] == [2]
    assert all(p["align"] == "center" for p in loose[0]["paragraphs"])


def test_left_card_joins_wrapped_body_lines():
    node = Rect(400, 140, 735, 472)
    rows = [[span("Next steps", 420, 180, 24)],
            [span("Personalised summaries, more", 420, 215, 14)],
            [span("languages, and a smaller model", 420, 231.1, 14)]]
    (box,) = card_text(node, rows)
    heading, body = box["paragraphs"]
    assert heading["align"] == body["align"] == "left"
    assert "".join(r["text"] for r in body["runs"]) == "Personalised summaries, more languages, and a smaller model"
    assert body["wrap_limit"] is not None and len(body["lines"]) == 2


def test_gutter_between_columns():
    a, b = span("quantisation.", 50, 200, 14, w=80), span("Peak", 260, 200, 14, w=30)
    others = [span("The model ships", 50, 183, 14, w=100), span("RAM must leave", 260, 183, 14, w=100)]
    assert PageClassifier.gutter([a, b] + others, a, b, 14)
    assert not PageClassifier.gutter([a, b], a, b, 14)  # nothing else on these baselines
    across = span("a line crossing the gap", 40, 217, 14, w=300)
    assert not PageClassifier.gutter([a, b, across] + others, a, b, 14)
    label = span("4:", 50, 200, 14, w=10)
    assert not PageClassifier.gutter([label, b] + others, label, b, 14)


def talk() -> dict:
    pdf = GDG / "gdg-talk.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    return classify(select_overlays(extract(pdf), "last"))


def test_gdg_talk_columns_stay_apart_and_cards_are_text():
    d = talk()
    columns = d["slides"][3]
    paragraphs = ["".join(r["text"] for r in p["runs"])
                  for e in columns["elements"] if e["kind"] == "text" for p in e["paragraphs"]]
    assert "Size" in paragraphs and "Memory" in paragraphs and "Quality" in paragraphs
    assert not any("quantisation." in p and "Peak" in p for p in paragraphs)

    def card_texts(slide: dict) -> list[str]:
        return ["".join(r["text"] for box in n["text"] for p in box["paragraphs"] for r in p["runs"])
                for e in slide["elements"] if e["kind"] == "diagram" for n in e["nodes"] if n.get("text")]

    assert card_texts(d["slides"][4]) == ["4×smaller download\x0bwith 4-bit weights"]  # balanced breaks kept
    assert card_texts(d["slides"][8]) == ["0%more crashes", "1.4%battery per day"]
