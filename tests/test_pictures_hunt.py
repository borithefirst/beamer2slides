"""Visual hunt, fixer W: pictures, overlays and math pictures against the native text around
them - on synthetic pages."""

import numpy as np
from PIL import Image

from beamer2slides import pdf
from beamer2slides.extract import extract_page
from beamer2slides.render import render_backgrounds

from .test_hidden_text import one_page


def raw_of(path) -> dict:
    raw = {"version": 1, "source": {"pdf": str(path), "producer": "", "pages": 1, "title": ""}, "pages": []}
    doc = pdf.Document(path)
    try:
        raw["pages"] = [extract_page(doc[0], "1")]
        raw["pages"][0]["frame_label"] = None
    finally:
        doc.close()
    return raw


def test_a_formula_glyph_hanging_into_the_line_above_stays_in_its_picture(tmp_path):
    """r1_math_v1 s8: `d - 2\\sqrt{d-1}` on the second line of an item, the first line native.
    CMSY's radical hangs from an origin an em above its formula's baseline, so its glyph box lies
    in the first line's x-height band: the native words' band switched it off before the
    formula's crop was taken, and the sign was in neither the text nor the picture."""
    content = (b"BT /F1 12 Tf 20 150 Td (Native words of the first line) Tj ET\n"
               b"BT /F1 12 Tf 20 134 Td (Show that x - 2) Tj ET\n"
               b"BT /F1 12 Tf 110 145 Td (V) Tj ET\n"            # the hanging sign
               b"BT /F1 12 Tf 118 134 Td (d - 1) Tj ET\n")
    path = tmp_path / "radical.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    spans = raw["pages"][0]["spans"]
    first = [s for s in spans if s["origin"][1] < 51]
    sign = [s for s in spans if 54 < s["origin"][1] < 56]
    formula = [s for s in spans if s["origin"][1] > 65]
    assert [s["text"] for s in sign] == ["V"] and first and formula
    # Its box reaches the first line's x-height band (render._band): this is the case.
    assert sign[0]["bbox"][1] < first[0]["origin"][1] - 0.2 * 12
    boxes = [s["bbox"] for s in sign + formula]
    bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    text = {"id": "t0", "kind": "text", "role": "body", "bbox": list(first[0]["bbox"]),
            "spans": [s["id"] for s in first], "paragraphs": []}
    picture = {"id": "h0", "kind": "image", "role": "math", "anchor": "t0", "bbox": bbox,
               "spans": [s["id"] for s in sign + formula]}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [text, picture], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    crop = ink(tmp_path / "out" / picture["file"])
    bbox = picture["bbox"]  # (grown to its ink)
    zoom = crop.shape[1] / (bbox[2] - bbox[0])
    x0, x1 = (np.array([sign[0]["bbox"][0], sign[0]["bbox"][2]]) - bbox[0]) * zoom
    above = crop[: int((min(s["bbox"][1] for s in formula) - bbox[1]) * zoom), int(x0):int(x1)]
    assert above.sum() > 20  # the sign's top is in the picture


def test_a_logo_wordmark_set_tight_above_a_native_line_stays_in_its_overlay(tmp_path):
    """r3_univ_v3 s10: the 'ICR' wordmark of a TikZ logo, with 'Institute for Coastal Research'
    in 6 pt just under it (native). The wordmark's font box reaches the small line's x-height band,
    so it went with the native words before the overlay's crop was taken: the logo came without
    its wordmark."""
    content = (b"0.1 0.2 0.4 rg 20 140 20 20 re f\n"
               b"BT 0.1 0.2 0.4 rg /F1 11 Tf 45 150 Td (ICR) Tj ET\n"
               b"BT 0.3 0.3 0.3 rg /F1 6 Tf 45 146 Td (Institute for Coastal Research) Tj ET\n")
    path = tmp_path / "logo.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    page = raw["pages"][0]
    mark = [s for s in page["spans"] if s["text"] == "ICR"]
    small = [s for s in page["spans"] if s["size"] < 7]
    assert mark and small and len(page["drawings"]) == 1
    assert mark[0]["bbox"][3] > small[0]["origin"][1] - 0.45 * 6  # it reaches the band: the case
    text = {"id": "t0", "kind": "text", "role": "body", "bbox": list(small[0]["bbox"]),
            "spans": [s["id"] for s in small], "paragraphs": []}
    logo = {"id": "f0", "kind": "image", "role": "figure", "overlay": True,
            "bbox": [19, 38, mark[0]["bbox"][2] + 1, 61], "spans": [mark[0]["id"]],
            "drawings": [page["drawings"][0]["id"]]}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [logo, text], "on_layout": []}]}
    render_backgrounds(path, raw, deck, tmp_path / "out")
    crop = np.array(Image.open(tmp_path / "out" / logo["file"]).convert("RGBA")).astype(int)
    zoom = crop.shape[1] / (logo["bbox"][2] - logo["bbox"][0])
    words = crop[:, int((44 - logo["bbox"][0]) * zoom):]
    assert (words[..., 3] > 128).sum() > 50  # the wordmark is in the picture


def test_an_ultra_thick_arrow_reaches_the_node_its_head_touches():
    """r2_overlays_v4 s2: `\\draw<3->[blue, ultra thick, ->] (v) -- (x)` over the black edge. The
    Stealth head is filled and stroked 1.59 pt wide: its mitred point reaches 2.2 pt past its path,
    to the node's outline. Ended at the path's point, the native line stopped short and the black
    edge under it showed as a stub at the tip."""
    from .test_charts_diagrams import Page, body_text, ellipse, lines
    from .test_diagrams_overlays import diagram_of

    p = Page()
    for cx, name in ((145.58, "v"), (213.62, "x")):
        p.draw(ellipse(cx, 72.02, 11.34, 11.34), type="s", stroke="#000000", width=0.8)
        p.text(name, cx - 2.5, 75, 9.96)
    p.draw(lines((157.32, 72.02), (201.88, 72.02)), type="s", stroke="#000000", width=0.8)
    p.draw(lines((157.32, 72.02), (195.68, 72.02)), type="s", stroke="#0000ff", width=1.59)
    p.draw(lines((199.61, 72.02), (194.29, 70.0), (196.07, 72.02), (194.29, 74.04), (199.61, 72.02)),
           type="fs", fill="#0000ff", stroke="#0000ff", width=1.59)
    body_text(p)
    blue = next(ln for ln in diagram_of(p)["lines"] if ln["stroke"] == "#0000ff")
    assert blue["arrow_to"] == "STEALTH_ARROW"
    assert abs(blue["to"][0] - 201.88) < 0.1  # where the black edge ends, at the node


def uline(p, text: str, x0: float, baseline: float, size: float = 10.91) -> None:
    """Words underlined as ulem's \\uline draws them: a rule under each word and each space,
    overlapping end to end."""
    from .test_charts_diagrams import lines

    y = baseline + 3.2
    words = text.split()
    for k, word in enumerate(words):
        s = p.text(word, x0, baseline, size)
        p.draw(lines((x0 - 0.2, y), (s["bbox"][2] + 0.2, y)), type="s", stroke="#000000", width=0.4)
        x0 = s["bbox"][2] + 0.33 * size
        if k < len(words) - 1:
            p.draw(lines((s["bbox"][2] - 0.2, y), (x0 + 0.2, y)), type="s", stroke="#000000", width=0.4)


def test_a_long_uline_wrapped_over_two_lines_is_an_underline():
    """r3_textfx_v3 s4: an item \\uline{...} over two lines. The first line's pieces join into a
    rule wider than half the page, taken for a theme hairline, and the short last line under it
    read as a formula's denominator: the pieces became fraction bars, the item a formula left
    in the background, and one rule a figure whose box took the words above it off the page."""
    from .test_charts_diagrams import Page, deck

    p = Page()
    uline(p, "Please add the sample size and the confidence interval to every chart", 30, 150)
    uline(p, "results section", 30, 165.5)
    p.words("as requested in the first round.", 120, 165.5)
    p.words("Body text that sets the size of the deck", 30, 225)
    slide = deck(p)["slides"][0]
    assert not slide["left_in_background"] and all(e["kind"] == "text" for e in slide["elements"])
    runs = [r for e in slide["elements"] for par in e["paragraphs"] for r in par["runs"]]
    underlined = "".join(r["text"] for r in runs if r["underline"])
    assert underlined.split() == "Please add the sample size and the confidence interval to every chart results section".split()


def test_words_a_thin_picture_grazes_stay_in_the_background(tmp_path):
    """A figure only a rule tall under words left in the background (r3_textfx_v3 s4, one
    underline piece): the words' font boxes reach into the figure's box by their descent, so they
    went off the background with it while the crop showed only the rule."""
    from beamer2slides.render import load_png

    content = (b"BT /F1 12 Tf 20 150 Td (Words above the rule) Tj ET\n"
               b"0 0 0 RG 0.4 w 20 146.5 m 140 146.5 l S\n")
    path = tmp_path / "grazed.pdf"
    path.write_bytes(one_page(content))
    raw = raw_of(path)
    page = raw["pages"][0]
    (rule,) = page["drawings"]
    box = [rule["bbox"][0] - 1, rule["bbox"][1] - 1, rule["bbox"][2] + 1, rule["bbox"][3] + 1]
    assert all(s["bbox"][3] > box[1] for s in page["spans"])  # the words' boxes reach into it: the case
    figure = {"id": "f0", "kind": "image", "role": "figure", "bbox": box, "spans": []}
    deck = {"slides": [{"page": 0, "size": [400, 200], "elements": [figure], "on_layout": []}]}
    [png] = render_backgrounds(path, raw, deck, tmp_path / "out")
    bg = load_png(png)
    z = bg.shape[1] / 400
    words = bg[round(z * 40):round(z * box[1]), round(z * 20):round(z * 140)]
    assert (words.min(axis=2) < 100).sum() > 200  # the words are still on the background


def test_symbols_tex_builds_from_overlapped_pieces_are_one_character():
    """r1_math_v2 s2/s9, r1_sci_v3 s3: \\cong is CMSY's ∼ over CMR's =, \\implies CMR's = kerned
    under CMSY's ⇒, \\longrightarrow and mhchem's reaction arrow relbars under an arrow in one
    CMSY span. Slides set the pieces one after the other: '∼=', '=⇒', '−−→'."""
    from .test_charts_diagrams import Page, body_text, deck

    p = Page()
    x = p.words("Dual spaces: L", 30, 60)
    p.text("∼", x + 3, 57, font="CMSY10", w=8.48)
    p.text("=", x + 3, 60.5, font="CMR10", w=8.48)
    p.words("M for every p.", x + 15, 60)
    x = p.words("If A holds", 30, 80)
    p.text("=", x + 3, 80, font="CMR10", w=8.48)
    p.text("⇒", x + 9.8, 80, font="CMSY10", w=10.9)
    p.words("B holds too.", x + 24, 80)
    x = p.words("Oxide plus acid", 30, 100)
    p.text("−−→", x + 3, 100, font="CMSY10", w=21.8)
    p.words("iodide and water.", x + 28, 100)
    body_text(p)
    runs = lambda e: "".join(r["text"] for par in e["paragraphs"] for r in par["runs"])
    text = " / ".join(runs(e) for e in deck(p)["slides"][0]["elements"] if e["kind"] == "text")
    assert "L ≅ M" in text and "A holds ⟹ B" in text and "acid ⟶ iodide" in text


def test_an_accent_over_a_greek_letter_is_a_formula_hole():
    """r1_econ_v4 s1, r1_math_v2 s8: \\hat\\beta and \\tilde\\mu became β + U+0302: no Slides face
    anchors a mark on a Greek letter, so the hat stood beside the letter, over the next word. The
    letter is a hole, its picture the page's; a Latin letter keeps its combining mark."""
    from .test_charts_diagrams import Page, body_text, deck

    p = Page()
    p.text("β", 50.17, 60, font="CMMI10", w=5.62)
    p.text("ˆ", 51.58, 57.5, font="CMR10", w=4.99)
    p.words("is the semi-elasticity of permits to the reform", 59.64, 60)
    p.text("X", 50.17, 80, font="CMMI10", w=6.63)
    p.text("˜", 51.58, 77.5, font="CMR10", w=4.99)
    p.words("are the residualised controls of the model", 60.5, 80)
    body_text(p)
    slide = deck(p)["slides"][0]
    holes = [e for e in slide["elements"] if e["kind"] == "image" and e.get("anchor")]
    assert len(holes) == 1 and holes[0]["bbox"][1] < 60 < holes[0]["bbox"][3]
    runs = [r for e in slide["elements"] if e["kind"] == "text" for par in e["paragraphs"] for r in par["runs"]]
    assert not any("β" in r["text"] for r in runs) and any("X̃" in r["text"] for r in runs)


def test_holes_with_no_word_between_them_are_one_hole():
    """r3_textfx_v2 s7: \\uwave{all benchmarks} became a hole per word, set side by side. Slides'
    text had one gap there, as wide as both, and measure_places put both pictures in it, one
    over the other ('all' printed over 'be'). With a word between them they stay two."""
    from beamer2slides.classify import Line

    from .test_charts_diagrams import span

    words = [span("Rejected:", 30, 10.91), span("significantly,", 84.3, 10.91), span("all", 142.3, 10.91),
             span("benchmarks", 159.3, 10.91)]  # (1 pt of kerning apart)
    line = Line(list(words))
    line.add_holes([[words[2]], [words[3]]])
    assert [[s.text for s in h] for h in line.holes] == [["all", "benchmarks"]]
    words = [span("x", 30, 10.91), span("and", 40, 10.91), span("y", 60, 10.91)]
    line = Line(list(words))
    line.add_holes([[words[0]], [words[2]]])
    assert len(line.holes) == 2


def ink(path) -> np.ndarray:
    """Dark opaque pixels of a picture (an anchored one has a transparent ground)."""
    px = np.array(Image.open(path).convert("RGBA")).astype(int)
    return (px[..., 3] > 128) & (px[..., :3].min(axis=2) < 100)
