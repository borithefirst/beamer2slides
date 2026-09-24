"""Long arrows and the formulas around them (visual hunt, wave 4 fixer M)."""
from .test_charts_diagrams import Page, body_text, deck


def runs_text(slide: dict) -> str:
    return " / ".join("".join(r["text"] for r in par["runs"]) for e in slide["elements"] if e["kind"] == "text"
                      for par in e["paragraphs"])


def holes(slide: dict) -> list[dict]:
    return [e for e in slide["elements"] if e["kind"] == "image" and e.get("anchor") is not None]


def test_a_long_arrow_is_a_hole_not_a_short_glyph():
    """r1_sci_v3 s3, r1_math_v3 s2/s6/s8, r1_sci_v2 s5: \\longrightarrow, mhchem's arrow and
    \\iff composed into one ⟶ / ⟺. Slides' fallback draws that glyph about 0.75 em long where
    TeX's is 1.64-3.3 em, set tight against the words ('Oxidation:→H2O2'). As a hole its
    picture keeps the PDF's length and spaces; the words around it stay text."""
    p = Page()
    x = p.words("Oxidation: water", 30, 60)
    p.text("−−→", x + 3, 60, font="CMSY10", w=21.8)
    p.words("peroxide released, needs oxygen", x + 28, 60)
    x = p.words("so the formula is satisfiable", 30, 80)
    p.text("⇐⇒", x + 3, 80, font="CMSY10", w=20)
    p.words("the machine accepts the input", x + 26, 80)
    x = p.words("and then", 30, 100)
    p.text("→", x + 3, 100, font="CMSY10", w=10.9)  # a short arrow stays a character
    p.words("follows from the lemma above", x + 17, 100)
    body_text(p)
    slide = deck(p)["slides"][0]
    text = runs_text(slide)
    assert "Oxidation: water" in text and "peroxide released" in text and "accepts the input" in text
    assert not any(c in text for c in "⟶⟺−⇐⇒")
    assert "→ follows" in text
    assert len(holes(slide)) == 2
    assert all(h["bbox"][2] - h["bbox"][0] >= 20 for h in holes(slide))  # the PDF's length


def test_a_matrix_parenthesis_under_a_word_stays_with_its_display():
    """r1_math_v1 s2: a pmatrix's upper left parenthesis piece (CMEX, hanging from its origin) is
    a line of its own, and a word of the line above ('vertices,') starts where it does. Taken
    for that paragraph's wrapped formula it became a hole, its picture measured into an empty
    text box: the upper half of the parenthesis shifted right of its lower half."""
    def hang(text: str, x0: float, top: float) -> None:
        """A CMEX piece: its origin at the top of its box, the glyph hanging below it."""
        s = p.text(text, x0, top + 0.7, font="CMEX10", w=9.55)
        s["bbox"] = [x0, top, x0 + 9.55, top + 10.91]

    p = Page()
    for word, x0, w in (("For", 28.4, 15.9), ("the", 47.9, 15.1), ("path", 66.7, 21.8), ("P", 92.1, 7.0),
                        ("on", 107.5, 11.5), ("three", 122.7, 24.2), ("vertices,", 150.6, 38.8)):
        p.text(word, x0, 185.4, font="CMMI10" if word == "P" else "CMR10", w=w)
    hang("\uf8eb", 149.6, 199.9)
    hang("\uf8f6", 220.8, 199.9)
    hang("\uf8ed", 149.6, 219.5)
    hang("\uf8f8", 220.8, 219.5)
    for y, row in ((208.9, ("1", "−1", "0")), (222.5, ("−1", "2", "−1")), (236.0, ("0", "−1", "1"))):
        for x, t in zip((163.3, 183.0, 211.1), row):
            p.text(t, x, y, font="CMR10", w=5.5 * len(t))
    p.text("L", 127.6, 222.5, font="CMMI10", w=7.4)
    p.text(" =", 135.0, 222.5, font="CMR10", w=11.5)
    body_text(p, 260)
    slide = deck(p)["slides"][0]
    assert not holes(slide)
    assert "\uf8eb" not in runs_text(slide)


def test_an_items_formula_wrapped_alone_is_a_paragraph_of_its_own():
    """r3_dense_v4 s3: '(O(√n))' wrapped alone under 'Separator theorems', a hole in the item's
    paragraph (wrapped_formula). Lato set 'Separator theorems' wider than the PDF's line, and a
    paragraph with a hole is not measured, so the box kept the PDF's width: 'theorems' wrapped
    onto the formula's line and its picture printed over it. Apart, the item's words are
    measured (the box grows) and the formula starts its own line where TeX broke."""
    from beamer2slides import emit

    from beamer2slides.classify import classify

    from .test_charts_diagrams import lines

    reg, obl = "LMSans9-Regular", "LMSans9-Oblique"
    p = Page()
    # (the deck's right-hand column, as PDFium reads it: text, font, box, baseline)
    for text, font, bbox, baseline in (
            ("Algorithms", "LMSans10-Bold", [244.84, 91.95, 289.11, 100.91], 98.96),
            ("▶", "MSAM10", [254.23, 103.69, 261.2, 112.66], 112.66),
            ("Separator", reg, [266.66, 106.89, 303.17, 115.86], 113.9),
            ("theorems", reg, [306.25, 106.89, 340.84, 115.86], 113.9),
            ("(", reg, [266.66, 117.85, 270.24, 126.82], 124.86),
            ("O", obl, [270.24, 117.85, 277.05, 126.82], 124.86),
            ("(", reg, [277.51, 117.85, 281.09, 126.82], 124.86),
            ("√", "LMMathSymbols9-Regular", [281.1, 111.21, 288.78, 120.18], 118.34),
            ("n", obl, [288.78, 117.85, 293.53, 126.82], 124.86),
            ("))", reg, [293.68, 117.85, 300.83, 126.82], 124.86),
            ("▶", "MSAM10", [254.23, 129.59, 261.2, 138.56], 138.56),
            ("Shortest", reg, [266.66, 132.8, 298.3, 141.76], 139.8),
            ("paths", reg, [301.37, 132.8, 322.16, 141.76], 139.8),
            ("in", reg, [325.23, 132.8, 332.18, 141.76], 139.8),
            ("linear", reg, [266.66, 143.75, 287.2, 152.72], 150.76),
            ("time", reg, [290.28, 143.75, 307.21, 152.72], 150.76),
            ("▶", "MSAM10", [254.23, 155.49, 261.2, 164.46], 164.46),
            ("Approximation", reg, [266.66, 158.7, 322.6, 167.66], 165.71),
            ("schemes", reg, [266.66, 169.66, 298.06, 178.62], 176.67),
            ("(Baker)", reg, [301.14, 169.66, 330.33, 178.62], 176.67)):
        s = p.text(text, bbox[0], baseline, 8.97, font=font, color="#3333b3" if text == "▶" else "#000000")
        s["bbox"] = bbox
    p.draw(lines((288.78, 118.15), (293.69, 118.15)), type="s", stroke="#000000", width=0.38)
    raw = {**p.raw(), "size": [362.83, 272.13]}  # (the deck's page: the item fills its column)
    d = classify({"version": 1, "source": {"title": ""}, "pages": [raw]})
    slide = d["slides"][0]
    [box] = [e for e in slide["elements"] if e["kind"] == "text" and "Separator" in runs_text({"elements": [e]})]
    k = next(i for i, par in enumerate(box["paragraphs"]) if par["runs"][0]["text"].startswith("Separator"))
    item, formula = box["paragraphs"][k:k + 2]
    assert not any(r.get("hole") for r in item["runs"])
    assert formula["bullet"] is None and [bool(r.get("hole")) for r in formula["runs"]] == [True]
    assert formula["lines"][0]["x0"] == item["lines"][0]["x0"]
    scale = emit.SLIDE_W / slide["size"][0]
    assert emit.slides_lines(item, scale, emit.FontMapper()) is not None  # measured: the box can grow


def test_an_arrows_labels_are_in_its_picture():
    """r1_sci_v3 s3: \\ce{->[120 °C][in vacuo]}, r1_sci_v2 s5 'fold' over O2 -> GFP*. The labels
    are small lines of their own over and under the stretched arrow; they stayed text, placed
    where the PDF has them while Slides set the formula at other widths: '120 °C' over
    '(PEA)2PbI4'. They go into the arrow's picture."""
    p = Page()
    x = p.words("Anneal the crystals", 30, 100)
    p.text("−−−−−→", x + 3, 100, font="CMSY10", w=36.1)
    p.text("120 °C", x + 9, 92.5, 7.97, w=24)
    p.text("in vacuo", x + 6, 106.5, 7.97, w=28)
    p.words("to a dry film of it", x + 42, 100)
    body_text(p)
    slide = deck(p)["slides"][0]
    text = runs_text(slide)
    assert "120" not in text and "vacuo" not in text and "Anneal the crystals" in text
    (hole,) = holes(slide)
    assert hole["bbox"][1] < 92.5 - 0.75 * 7.97 + 0.5 and hole["bbox"][3] > 106.5
