"""Inline scripts on synthetic lines: which small raised or lowered words are sub/superscripts,
the size the IR gives them (Slides draws a script at 2/3), raised marks drawn as the text face's
own degree sign and asterisk, and where the word space after smaller words goes."""

from beamer2slides.classify import Rect, Span, classify_page
from beamer2slides.fonts import font_info

W, H = 362.83, 272.13
FONT = "LMSans10-Regular"
SYMBOLS = "CMSY8"


def span(text: str, x0: float, baseline: float, size: float = 11.0, w: float | None = None,
         font: str = FONT) -> Span:
    w = len(text) * 0.5 * size if w is None else w
    return Span(id="", text=text, font=font, size=size, color="#000000",
                rect=Rect(x0, baseline - 0.75 * size, x0 + w, baseline + 0.25 * size),
                baseline=baseline, horizontal=True, info=font_info(font))


def after(s: Span, text: str, rise: float = 0.0, size: float = 11.0, gap: float = 0.0, font: str = FONT) -> Span:
    """A span starting `gap` pt after `s`, its baseline `rise` em (of 11 pt) above s's."""
    return span(text, s.rect.x1 + gap, s.baseline - rise * 11.0, size, font=font)


def runs(spans: list[Span]) -> list[dict]:
    raw = [{"id": f"p0s{i}", "text": s.text, "font": s.font, "size": s.size, "color": s.color, "alpha": 255,
            "origin": [s.rect.x0, s.baseline], "bbox": s.rect.as_list(), "dir": [1.0, 0.0], "smallcaps": False}
           for i, s in enumerate(spans)]
    slide = classify_page({"index": 0, "label": "1", "size": [W, H], "spans": raw, "images": [], "drawings": [],
                           "links": [], "frame_label": None}, 11.0)
    texts = [e for e in slide["elements"] if e["kind"] == "text"]
    assert len(texts) == 1, [[r["text"] for p in e["paragraphs"] for r in p["runs"]] for e in texts]
    (par,) = texts[0]["paragraphs"]
    return par["runs"]


def plain(rs: list[dict]) -> list[tuple]:
    return [(r["text"], r["script"], r["size"]) for r in rs]


def test_a_textsubscript_lowered_a_tenth_of_an_em_is_a_subscript():
    """r2_themes_v1 s4: \\framesubtitle{... CO\\textsubscript{2} storage}: the 2 is 0.11 em down
    at 2/3 size. It was no script, kept its size and took the word space: 'CO2storage'."""
    co = span("Geothermal CO", 30, 100)
    two = after(co, "2", rise=-0.11, size=7.3)
    storage = after(two, "storage", gap=3.0)
    assert plain(runs([co, two, storage])) == [("Geothermal CO", None, 11.0), ("2", "sub", 11.0), (" storage", None, 11.0)]


def test_keycap_letters_raised_a_tenth_of_an_em_stay_on_the_line():
    ctrl = span("Press", 30, 100)
    key = after(ctrl, "CTRL", rise=0.1, size=8.0, gap=3.0)
    assert [r["script"] for r in runs([ctrl, key])] == [None, None]


def test_lstinline_raised_underscore_is_code_not_a_superscript():
    """r2_code_v3 s2: listings raises the underscore of \\lstinline!_exit! 0.18 em and the span's
    origin with it: the word became a small raised '-exit'."""
    use = span("Use", 30, 100)
    code = after(use, "_exit", rise=0.18, size=8.47, gap=3.0, font="BeraSansMono-Roman")
    rest = span("in the child", code.rect.x1 + 3, 100)
    rs = runs([use, code, rest])
    assert [(r["text"].strip(), r["script"]) for r in rs] == [("Use", None), ("_exit", None), ("in the child", None)]


def test_a_script_of_a_script_is_given_the_size_slides_draws_small_enough():
    """A script at 0.73 or 2/3 of its line is given the line's size; one at 0.55 (a script's
    script) was too, and Slides drew it at 2/3, as large as the script it hangs from."""
    from beamer2slides.classify import span_runs
    base = span("Total", 30, 100)
    sub = after(base, "sub", rise=-0.15, size=8.0)
    subsub = after(sub, "deeper", rise=-0.3, size=6.0)
    rs = span_runs([base, sub, subsub])  # (a cell's or a node's words; on a text line it is a formula picture)
    assert plain(rs)[:2] == [("Total", None, 11.0), ("sub", "sub", 11.0)]
    assert rs[2]["script"] == "sub" and rs[2]["size"] == round(6.0 / 0.665, 2)


def test_raised_ring_is_a_degree_sign_at_the_line_size():
    """r2_themes_v4 s6 (siunitx \\celsius: CMSY8's \\circ raised) and r3_layout_v2 s3: the ring
    became a WHITE BULLET superscript, a small ring set high."""
    num = span("to 30", 30, 100)
    ring = after(num, "◦", rise=0.4, size=8.0, gap=2.0, font=SYMBOLS)
    unit = after(ring, "C", gap=0.3)
    rs = runs([num, ring, unit])
    assert "".join(r["text"] for r in rs) == "to 30 °C" and all(r["script"] is None and r["size"] == 11.0 for r in rs)


def test_raised_asterisk_is_the_text_faces_asterisk_at_the_line_size():
    """r3_textfx_v2 s8: \\textsuperscript{*} after '18 pt' shrank to a speck."""
    words = span("below 18 pt", 30, 100)
    star = after(words, "*", rise=0.4, size=8.0)
    rs = runs([words, star])
    assert "".join(r["text"] for r in rs) == "below 18 pt*" and all(r["script"] is None and r["size"] == 11.0 for r in rs)


def test_superscript_stacked_over_a_subscript_is_its_lines():
    """r3_scripts_el s5: S_n^{(k)}, the (k) 0.527 em up over the n, came out a text box of its own
    at the PDF place, over the words once Slides set the line."""
    words = span("If", 30, 100)
    s = after(words, "S", gap=3.0)
    n = after(s, "n", rise=-0.25, size=8.0)
    k = span("(k)", s.rect.x1, 100 - 0.527 * 11, 8.0)
    rest = span("is the sum", k.rect.x1 + 3, 100)
    rs = runs([words, s, n, k, rest])  # (one text element: the (k) is no box of its own)
    # Slides cannot stack scripts: the formula is a hole in its line, its picture placed there
    assert [r["text"].strip() for r in rs if not r.get("hole")] == ["If S", "is the sum"]
    assert any(r.get("hole") for r in rs)


def test_word_space_after_smaller_words_is_the_surrounding_texts():
    """A space after a smaller run is set at the smaller size: the words after it touch."""
    big = span("Total", 30, 100)
    small = after(big, "small", size=7.0, gap=3.0)
    word = after(small, "words", gap=3.0)
    rs = runs([big, small, word])
    assert [r["text"] for r in rs] == ["Total ", "small", " words"]
