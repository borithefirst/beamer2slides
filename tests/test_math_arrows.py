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


def test_a_wrapped_formula_line_of_symbols_is_one_hole():
    """r1_math_v3 s6: 'q0 x1 · · · xn ⊔ · · · ;' wrapped under '(b) φstart: row 1 is'. Its
    subscripts were runs and its symbols text: Slides set the ⊔ and the dots at other widths,
    the line drifted from its picture-free neighbours and the ';' ran into the next item. A
    wrapped formula line holding a symbol Slides was never measured on is one picture, its
    trailing punctuation text; a flat continuation ('λ2.', 'legal;') stays words."""
    p = Page()
    # (the deck's spans, as PDFium reads them: text, font, size, box, baseline)
    for text, font, size, bbox, baseline in (
            ("(b)", "SFRM0600", 5.98, [14.97, 161.09, 24.76, 167.07], 165.76),
            ("φ", "CMMI10", 10.91, [33.64, 159.29, 40.77, 170.2], 167.82),
            ("start", "SFRM0800", 7.97, [40.77, 163.23, 58.23, 171.2], 169.45),
            (":", "SFRM1095", 10.91, [58.73, 159.3, 61.74, 170.21], 167.82),
            ("row", "SFRM1095", 10.91, [66.56, 159.3, 83.72, 170.21], 167.82),
            ("1", "CMR10", 10.91, [87.34, 159.29, 92.8, 170.2], 167.82),
            ("is", "SFRM1095", 10.91, [96.41, 159.3, 103.7, 170.21], 167.82),
            ("q", "CMMI10", 10.91, [33.64, 172.84, 38.5, 183.75], 181.37),
            ("0", "CMR8", 7.97, [38.51, 176.77, 42.74, 184.74], 183.0),
            ("x", "CMMI10", 10.91, [45.06, 172.84, 51.29, 183.75], 181.37),
            ("1", "CMR8", 7.97, [51.29, 176.77, 55.52, 184.74], 183.0),
            ("· · ·", "CMSY10", 10.91, [57.84, 172.7, 70.54, 183.61], 181.37),
            (" x", "CMMI10", 10.91, [70.54, 172.84, 78.59, 183.75], 181.37),
            ("n", "CMMI8", 7.97, [78.62, 176.77, 83.76, 184.74], 183.0),
            ("⊔ · · ·", "CMSY10", 10.91, [88.5, 172.7, 110.89, 183.61], 181.37),
            (" ;", "SFRM1095", 10.91, [110.89, 172.85, 115.72, 183.76], 181.37),
            ("(c)", "SFRM0600", 5.98, [15.36, 191.18, 24.37, 197.15], 195.85),
            ("φ", "CMMI10", 10.91, [33.64, 189.38, 40.77, 200.29], 197.9),
            ("move", "SFRM0800", 7.97, [40.77, 193.32, 59.81, 201.29], 199.54),
            (":", "SFRM1095", 10.91, [60.32, 189.39, 63.33, 200.3], 197.9),
            ("every", "SFRM1095", 10.91, [68.15, 189.39, 93.13, 200.3], 197.9),
            ("2", "CMR10", 10.91, [96.76, 189.38, 102.21, 200.29], 197.9),
            (" ×", "CMSY10", 10.91, [102.21, 189.24, 113.11, 200.15], 197.9),
            (" 3", "CMR10", 10.91, [113.11, 189.38, 120.99, 200.29], 197.9),
            ("window", "SFRM1095", 10.91, [124.61, 189.39, 160.44, 200.3], 197.9),
            ("is", "SFRM1095", 10.91, [164.05, 189.39, 171.34, 200.3], 197.9),
            ("legal;", "SFRM1095", 10.91, [33.64, 202.94, 58.34, 213.85], 211.45),
            ("Cheap", "CMR10", 10.91, [204.8, 132.18, 235.08, 143.09], 140.71),
            ("cuts", "CMR10", 10.91, [238.72, 132.18, 258.14, 143.09], 140.71),
            ("force", "CMR10", 10.91, [261.79, 132.18, 284.52, 143.09], 140.71),
            ("a", "CMR10", 10.91, [288.16, 132.18, 293.61, 143.09], 140.71),
            ("small", "CMR10", 10.91, [297.24, 132.18, 322.13, 143.09], 140.71),
            ("λ", "CMMI10", 10.91, [204.8, 145.73, 211.16, 156.64], 154.26),
            ("2", "CMR8", 7.97, [211.16, 149.66, 215.39, 157.63], 155.89),
            (".", "CMR10", 10.91, [215.9, 145.73, 218.92, 156.64], 154.26)):
        s = p.text(text, bbox[0], baseline, size, font=font)
        s["bbox"] = bbox
    body_text(p, 250)
    slide = deck(p)["slides"][0]
    pars = [par for e in slide["elements"] if e["kind"] == "text" for par in e["paragraphs"]]
    said = ["".join(r["text"] for r in par["runs"]) for par in pars]
    [q] = [par for par in pars if any(r.get("hole") for r in par["runs"])]
    assert [r["text"].strip() for r in q["runs"] if not r.get("hole")] == [";"]
    assert not any(c in t for t in said for c in "⊔·")
    assert any("legal;" in t for t in said) and any(t.rstrip().endswith("λ2.") for t in said)
    [hole] = holes(slide)
    assert hole["bbox"][0] <= 33.64 and hole["bbox"][2] >= 110.89


def test_a_wrapped_formula_line_with_words_after_it_keeps_the_words():
    """r2_fonts_firamath s3: 'L(θ, φ) = 𝔼_{q_φ}[log p_θ(x | z)] − β KL(q_φ ‖ p), with β = 1 for the
    plain' wrapped under 'For every x the log-likelihood is bounded by'. Its formula is a hole;
    the words after it stay text, not one picture of the whole line."""
    p = Page()
    light, math = "FiraSans-Light", "FiraMath-Regular"
    for text, font, size, bbox, baseline in (
            ("For every", light, 10.91, [28.35, 61.25, 72.46, 74.34], 71.45),
            (" 𝑥", math, 10.91, [72.46, 62.72, 86.42, 73.63], 71.45),
            ("the log-likelihood is bounded by", light, 10.91, [84.77, 61.25, 243.22, 74.34], 71.45),
            ("𝐿(𝜃, 𝜙) = 𝔼", math, 10.91, [28.35, 78.31, 76.43, 89.21], 87.03),
            ("𝑞", math, 7.86, [76.44, 84.57, 80.96, 92.42], 90.85),
            ("𝜙", math, 6.33, [80.96, 88.54, 84.89, 94.87], 93.6),
            ("[", math, 10.91, [85.79, 78.31, 89.3, 89.21], 87.03),
            ("log", "LMRoman10-Regular", 10.91, [89.3, 74.74, 103.24, 90.2], 87.03),
            (" 𝑝", math, 10.91, [103.24, 78.31, 111.37, 89.21], 87.03),
            ("𝜃", math, 7.86, [111.37, 84.57, 115.87, 92.42], 90.85),
            ("(𝑥∣ 𝑧)] − 𝛽", math, 10.91, [116.48, 78.31, 165.74, 89.21], 87.03),
            (" KL", "LMRoman10-Regular", 10.91, [165.74, 74.74, 183.09, 90.2], 87.03),
            ("(𝑞", math, 10.91, [183.09, 78.31, 192.91, 89.21], 87.03),
            ("𝜙", math, 7.86, [192.91, 84.57, 197.79, 92.42], 90.85),
            ("‖ 𝑝)", math, 10.91, [200.21, 78.31, 218.17, 89.21], 87.03),
            (", with", light, 10.91, [218.18, 76.83, 244.06, 89.92], 87.03),
            (" 𝛽 = 1", math, 10.91, [244.06, 78.31, 271.44, 89.21], 87.03),
            (" for the plain", light, 10.91, [271.44, 76.83, 334.5, 89.92], 87.03),
            ("VAE. The reconstruction term rewards sharp images", light, 10.91, [28.35, 94.25, 300.29, 107.34],
             104.45)):
        s = p.text(text, bbox[0], baseline, size, font=font)
        s["bbox"] = bbox
    body_text(p, 250)
    text = runs_text(deck(p)["slides"][0])
    assert "with" in text and "for the plain" in text


def test_a_label_between_two_arrows_is_the_nearer_ones():
    """r1_math_v2 s7: 'fn −µ→ f' over 'fn −Lp→ f', each arrow's label a small line of its own.
    Lp, over the lower arrow, is also just under the upper one: taken for the upper arrow's
    label, that arrow's picture reached down over the lower line's."""
    p = Page()
    for text, font, size, bbox, baseline in (
            ("f", "CMMI10", 10.91, [32.73, 101.98, 38.06, 112.89], 110.5),
            ("n", "CMMI8", 7.97, [38.07, 105.91, 43.2, 113.88], 112.14),
            ("µ", "CMMI8", 7.97, [49.32, 97.51, 54.41, 105.48], 103.74),
            ("−", "CMSY10", 10.91, [46.74, 101.84, 55.21, 112.75], 110.5),
            ("→", "CMSY10", 10.91, [47.5, 101.84, 58.41, 112.75], 110.5),
            (" f", "CMMI10", 10.91, [58.41, 101.98, 66.78, 112.89], 110.5),
            ("if", "CMR10", 10.91, [71.59, 101.98, 77.94, 112.89], 110.5),
            ("it", "CMR10", 10.91, [81.57, 101.98, 88.14, 112.89], 110.5),
            ("converges", "CMR10", 10.91, [91.8, 101.98, 140.0, 112.89], 110.5),
            ("in", "CMR10", 10.91, [143.6, 101.98, 152.8, 112.89], 110.5),
            ("measure;", "CMR10", 10.91, [156.4, 101.98, 200.0, 112.89], 110.5),
            ("f", "CMMI10", 10.91, [32.73, 122.5, 38.06, 133.41], 131.03),
            ("n", "CMMI8", 7.97, [38.07, 126.44, 43.2, 134.41], 132.67),
            ("L", "CMMI8", 7.97, [49.32, 118.62, 55.08, 126.59], 124.85),
            ("p", "CMMI6", 5.98, [55.08, 117.36, 58.92, 123.34], 122.03),
            ("−→", "CMSY10", 10.91, [46.74, 122.36, 63.42, 133.27], 131.03),
            (" f", "CMMI10", 10.91, [63.42, 122.5, 71.77, 133.41], 131.03),
            ("if", "CMR10", 10.91, [76.58, 122.5, 82.93, 133.41], 131.03),
            ("it", "CMR10", 10.91, [86.58, 122.5, 93.1, 133.41], 131.03),
            ("converges", "CMR10", 10.91, [96.7, 122.5, 145.0, 133.41], 131.03),
            ("in", "CMR10", 10.91, [148.6, 122.5, 157.8, 133.41], 131.03),
            ("norm.", "CMR10", 10.91, [161.4, 122.5, 190.0, 133.41], 131.03)):
        s = p.text(text, bbox[0], baseline, size, font=font)
        s["bbox"] = bbox
    body_text(p, 250)
    slide = deck(p)["slides"][0]
    upper, lower = sorted(holes(slide), key=lambda h: h["bbox"][1])
    assert upper["bbox"][3] <= lower["bbox"][1] + 1.0  # (no picture over the other's)
    assert lower["bbox"][1] <= 117.36 + 0.5 and "L" not in runs_text(slide).replace("Body", "")


def test_a_table_cells_accent_is_over_its_letter():
    """r1_econ_v4 s2: a table header 'β̂ σ̂ ȳ', each accent a span of the text face over its
    letter. Cell runs put the spacing accent beside its letter: 'βˆ | σˆ | y¯'."""
    from .test_charts_diagrams import rect

    p = Page()
    sans, obl, mi = "LMSans10-Regular", "LMSans10-Oblique", "LMMathItalic10-Regular"
    for text, font, bbox, baseline in (
            ("Estimator", sans, [82.62, 166.19, 126.55, 177.1], 174.71),
            ("ˆ", sans, [197.82, 163.46, 203.27, 174.37], 171.99),
            ("β", mi, [196.26, 166.19, 202.43, 177.1], 174.71),
            ("ˆ", sans, [233.7, 166.19, 239.16, 177.1], 174.71),
            ("σ", mi, [233.12, 166.19, 239.35, 177.1], 174.71),
            ("¯", sans, [267.78, 166.19, 273.24, 177.1], 174.71),
            ("y", obl, [267.4, 166.19, 272.43, 177.1], 174.71)):
        s = p.text(text, bbox[0], baseline, font=font)
        s["bbox"] = bbox
    for y, cells in ((190.0, ("TWFE", "0.142", "0.041", "3.91")), (203.5, ("Callaway", "0.158", "0.047", "3.91"))):
        for x, t in zip((82.62, 185.0, 222.0, 260.0), cells):
            p.text(t, x, y, font=sans, w=5.45 * len(t))
    for y0, h in ((158.8, 0.8), (180.1, 0.5), (208.0, 0.8)):
        p.draw(rect(74.64, y0, 288.19, y0 + h), fill="#000000")
    body_text(p, 250)
    slide = deck(p)["slides"][0]
    [table] = [e for e in slide["elements"] if e["kind"] == "table"]
    head = ["".join(r["text"] for r in c).strip() for c in table["cells"][0]]
    assert head == ["Estimator", "β̂", "σ̂", "ȳ"]


def test_a_table_cells_accent_read_with_the_text_before_it_goes_on_its_letter():
    """r1_econ_v3 s7: 'Labelled (β̂_L)' in a table, PDFium reading the hat with the parenthesis
    before it ('(ˆ', then 'β'): the cell said 'Labelled (ˆβL)', a stray caret."""
    from .test_charts_diagrams import rect

    p = Page()
    sans, mi = "LMSans8-Regular", "LMMathItalic8-Regular"
    for text, font, size, bbox, baseline in (
            ("Enrolled", sans, 7.97, [150.0, 96.0, 180.0, 104.0], 102.2),
            ("Days", sans, 7.97, [210.0, 96.0, 228.0, 104.0], 102.2),
            ("Labelled", sans, 7.97, [69.61, 113.64, 98.81, 121.61], 119.86),
            ("(ˆ", sans, 7.97, [101.63, 111.64, 110.35, 121.61], 119.86),
            ("β", mi, 7.97, [104.95, 113.64, 109.7, 121.61], 119.86),
            ("L", "LMSans8-Oblique", 5.98, [109.7, 116.6, 113.16, 122.58], 121.27),
            (")", sans, 7.97, [113.65, 113.64, 116.95, 121.61], 119.86),
            ("0.094", sans, 7.97, [150.0, 113.64, 170.0, 121.61], 119.86),
            ("11.2", sans, 7.97, [210.0, 113.64, 226.0, 121.61], 119.86),
            ("Controls", sans, 7.97, [69.61, 126.0, 99.0, 134.0], 132.2),
            ("No", sans, 7.97, [150.0, 126.0, 160.0, 134.0], 132.2),
            ("Yes", sans, 7.97, [210.0, 126.0, 223.0, 134.0], 132.2)):
        s = p.text(text, bbox[0], baseline, size, font=font)
        s["bbox"] = bbox
    for y0, h in ((92.0, 0.8), (107.0, 0.5), (137.0, 0.8)):
        p.draw(rect(66.0, y0, 240.0, y0 + h), fill="#000000")
    body_text(p, 250)
    slide = deck(p)["slides"][0]
    [table] = [e for e in slide["elements"] if e["kind"] == "table"]
    said = ["".join(r["text"] for r in c).strip() for row in table["cells"] for c in row]
    assert "Labelled (β̂L)" in said


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
