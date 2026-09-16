"""Regression tests for extraction and classification (local only, no Google API).

The PDFs come from `python tests/decks/build.py` and `python tests/themes/sweep.py --build`;
tests whose PDF is missing are skipped.
"""

from pathlib import Path

import pytest

from beamer2slides.classify import classify
from beamer2slides.extract import extract, select_overlays
from beamer2slides.notes import prepare

DECKS = Path(__file__).resolve().parent / "decks" / "out"
THEMES = Path(__file__).resolve().parent / "themes" / "out"


def load(pdf: Path, overlays: str = "last") -> dict:
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    return classify(select_overlays(extract(pdf), overlays))


def deck(name: str) -> dict:
    return load(DECKS / f"{name}-handout.pdf")


def texts(slide: dict) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "text"]


def paragraph_text(p: dict) -> str:
    return "".join(r["text"] for r in p["runs"])


def kinds(slide: dict) -> list[str]:
    return [e["kind"] for e in slide["elements"]]


def test_basic_deck_is_fully_native():
    d = deck("01_basic")
    assert d["stats"]["native_share"] == 1.0
    assert [e["role"] for e in texts(d["slides"][1]) if e["role"] == "title"] == ["title"]


def test_nested_list_levels_and_wrapping():
    slide = deck("01_basic")["slides"][1]
    lists = [e for e in texts(slide) if any(p["bullet"] for p in e["paragraphs"])]
    assert len(lists) == 1, "the whole nested list is one text box"
    levels = [p["level"] for p in lists[0]["paragraphs"]]
    assert levels == [0, 0, 1, 1, 2, 0]
    wrapped = lists[0]["paragraphs"][1]
    assert len(wrapped["lines"]) == 2 and paragraph_text(wrapped).endswith("second line of the slide")


def test_inline_styles_and_links():
    slide = deck("01_basic")["slides"][3]
    runs = [r for e in texts(slide) for p in e["paragraphs"] for r in p["runs"]]
    assert any(r["bold"] and r["text"] == "bold" for r in runs)
    assert any(r["italic"] and r["text"] == "emphasis" for r in runs)
    assert any(r["family"] == "mono" for r in runs)
    assert any(r["link"] == "https://example.com" for r in runs)


def test_inline_math_becomes_text_display_math_pictures():
    d = deck("02_math")
    runs = [r for e in texts(d["slides"][0]) for p in e["paragraphs"] for r in p["runs"]]
    assert any(r["text"].strip() == "ℝ" for r in runs)
    assert any(r["script"] == "super" and r["text"].strip() == "2" for r in runs)
    assert any(r["script"] == "sub" for r in runs)
    assert kinds(d["slides"][1]).count("image") >= 3  # display equations as pictures
    assert not any(l["reason"] == "math" for s in d["slides"] for l in s["left_in_background"])


def test_simple_inline_fraction_becomes_text():
    slide = deck("02_math")["slides"][0]
    lists = [e for e in texts(slide) if any(p["bullet"] for p in e["paragraphs"])]
    assert len(lists) == 1 and len(lists[0]["paragraphs"]) == 4  # the fraction item stays in the list
    runs = [r for p in lists[0]["paragraphs"] for r in p["runs"]]
    i = next(i for i, r in enumerate(runs) if r["text"] == "⁄")
    assert (runs[i - 1]["text"], runs[i - 1]["script"]) == ("a", "super")
    assert (runs[i + 1]["text"], runs[i + 1]["script"]) == ("b", "sub")
    assert lists[0]["strokes"], "the fraction bar leaves the background"


def test_figures_are_pictures_with_their_labels():
    d = deck("03_figures")
    plot = [e for e in d["slides"][2]["elements"] if e["kind"] == "image"]
    assert len(plot) == 1 and len(plot[0]["spans"]) >= 6  # tick labels travel with the plot
    assert kinds(d["slides"][3]).count("image") == 2  # side-by-side images stay separate


def test_simple_tikz_diagram_becomes_native():
    slide = deck("03_figures")["slides"][1]
    diagrams = [e for e in slide["elements"] if e["kind"] == "diagram"]
    assert len(diagrams) == 1
    d = diagrams[0]
    assert [n["shape"] for n in d["nodes"]] == ["ROUND_RECTANGLE"] * 4
    labels = sorted("".join(r["text"] for r in n["paragraphs"][0]) for n in d["nodes"])
    assert labels == ["Classify", "Emit", "Extract", "Render"]
    assert len(d["lines"]) == 3 and all(l["arrow_to"] and not l["arrow_from"] for l in d["lines"])
    # the pgfplots plot on the next slide has curves and axis labels: still a picture
    assert kinds(deck("03_figures")["slides"][2]).count("image") == 1


def test_underline_and_colorbox_become_text_styles():
    slide = deck("11_research_talk")["slides"][5]
    runs = [r for e in texts(slide) for p in e["paragraphs"] for r in p["runs"]]
    assert [r["text"] for r in runs if r["underline"]] == ["underlined"]
    assert [(r["text"], r["highlight"]) for r in runs if r["highlight"]] == [("Highlighted", "#fff200")]
    assert sum(len(e["strokes"]) for e in texts(slide)) == 2  # both drawings leave the background


def test_madrid_blocks_tables_and_footer():
    d = deck("04_theme_blocks")
    assert kinds(d["slides"][1]).count("shape") >= 6  # block title bars and bodies
    table = [e for e in d["slides"][3]["elements"] if e["kind"] == "table"]
    assert len(table) == 1
    cells = [[paragraph_text({"runs": c}) for c in row] for row in table[0]["cells"]]
    assert cells == [["Method", "Precision", "Recall"], ["Baseline", "0.71", "0.64"], ["Ours", "0.89", "0.83"]]
    assert [c["align"] for c in table[0]["columns"]] == ["left", "right", "right"]
    assert {t["key"][0] for t in d["layout_texts"]} == {"B. Tester (TU)", "Themed", "2026"}


def test_image_bullets_numbered_on_balls():
    slide = deck("04_theme_blocks")["slides"][2]
    numbered = [p for e in texts(slide) for p in e["paragraphs"] if p["bullet"] and p["bullet"]["kind"] == "image"]
    assert [p["bullet"]["text"] for p in numbered] == ["1", "2"]


def test_code_block_keeps_indentation():
    slide = deck("05_overlays_notes")["slides"][2]
    code = [e for e in texts(slide) if e["code"]]
    assert len(code) == 1
    lines = [paragraph_text(p) for p in code[0]["paragraphs"]]
    assert lines[1].startswith("    for") and lines[2].startswith("        yield")


def test_overlay_steps_collapse_to_frames():
    d = load(DECKS / "05_overlays_notes.pdf")
    assert len(d["slides"]) == 3
    everything = load(DECKS / "05_overlays_notes.pdf", overlays="all")
    assert len(everything["slides"]) == 6


def test_short_paragraphs_are_not_merged():
    slide = load(DECKS / "05_overlays_notes.pdf")["slides"][1]
    paras = [paragraph_text(p) for e in texts(slide) for p in e["paragraphs"]]
    assert "Different text on overlay two." in paras
    assert "Uncovered from overlay two onwards." in paras


@pytest.mark.parametrize("variant,mode", [("notes-pages", "note pages"), ("notes-second", "second screen")])
def test_speaker_notes(tmp_path, variant, mode):
    pdf = DECKS / "notes" / f"{variant}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not built")
    path, notes, found = prepare(pdf, tmp_path)
    assert found == mode
    assert notes == {0: "Speaker note for the pause frame.", 2: "A note on the code frame."}
    assert len(extract(path)["pages"]) == 3


def test_raster_images_and_full_bleed_background():
    d = deck("07_images")
    assert [kinds(s).count("image") for s in d["slides"]] == [1, 1, 1, 0]


def test_serif_deck():
    d = deck("08_serif")
    assert d["stats"]["native_share"] == 1.0
    families = {r["family"] for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert "serif" in families


def test_google_font_decks_keep_their_font_and_uncounted_frames():
    from beamer2slides.fonts import google_font

    d = deck("09_metropolis_fira")
    assert len(d["slides"]) == 4, "title page and section page share a frame number but are not overlays"
    assert d["stats"]["native_share"] == 1.0
    fonts = {google_font(r["font"]) for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert ("Fira Sans", 300, False) in fonts and ("Fira Sans", 700, False) in fonts


def test_metric_compatible_fonts():
    from beamer2slides.fonts import google_font

    assert google_font("NimbusSanL-ReguItal") == ("Arial", 400, True)
    assert google_font("NimbusRomNo9L-Medi") == ("Times New Roman", 700, False)
    assert google_font("texgyreheros-bolditalic") == ("Arial", 700, True)
    assert google_font("SourceSansPro-It") == ("Source Sans Pro", 400, True)
    assert google_font("CMSS10") is None
    d = deck("10_helvet")
    fonts = {google_font(r["font"]) for s in d["slides"] for e in texts(s) for p in e["paragraphs"] for r in p["runs"]}
    assert {("Arial", 400, False), ("Arial", 700, False), ("Arial", 400, True)} <= fonts


# Minimum native text share per theme for the realistic talk (tests/themes/content.tex).
THEME_FLOORS = {
    "default": 0.95, "Madrid": 0.9, "Warsaw": 0.75, "Berkeley": 0.75, "Bergen": 0.95,
    "Goettingen": 0.75, "Szeged": 0.75, "metropolis": 0.9, "CambridgeUS": 0.85,
}


@pytest.mark.parametrize("theme,floor", sorted(THEME_FLOORS.items()))
def test_theme_sweep_floors(theme, floor):
    d = load(THEMES / theme / "talk.pdf")
    assert d["stats"]["native_share"] >= floor
    assert all("unsure" not in {l["reason"] for l in s["left_in_background"]} for s in d["slides"])
    tables = [e for s in d["slides"] for e in s["elements"] if e["kind"] == "table"]
    assert len(tables) == 1 and len(tables[0]["cells"]) == 4
