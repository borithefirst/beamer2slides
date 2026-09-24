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


def ink(path) -> np.ndarray:
    """Dark opaque pixels of a picture (an anchored one has a transparent ground)."""
    px = np.array(Image.open(path).convert("RGBA")).astype(int)
    return (px[..., 3] > 128) & (px[..., :3].min(axis=2) < 100)
