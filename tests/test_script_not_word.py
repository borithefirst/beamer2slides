"""A script touching its letter is no word of prose (real_linear-attention-a s42): beamer's sans
math, with no math font, sets `S_t`, `k_j` as a CMSSBX letter and a CMSSI8 one touching it, and
`prose_share` counted each pair a word. A display of such vectors came out 0.39 prose, over
`DISPLAY_WORD_SHARE`: read as a line of prose with holes, its big parentheses split between the
background and pictures. `classify_text.scripted` keeps the two apart."""

from beamer2slides.classify import Rect, Span, new_span
from beamer2slides.classify_text import prose_share, scripted
from beamer2slides.fonts import font_info


def span(text: str, font: str, x0: float, baseline: float, size: float) -> Span:
    w = 0.5 * size * len(text)
    return new_span(id=f"s{x0}", text=text, font=font, size=size, color="#000000",
                    rect=Rect(x0, baseline - 0.7 * size, x0 + w, baseline + 0.2 * size), baseline=baseline,
                    horizontal=True, info=font_info(font), link=None, drawn=False, visual=None)


def test_a_subscript_touching_its_letter_is_no_word() -> None:
    letter = span("S", "CMSSBX10", 100.0, 100.0, 10.0)
    script = span("t", "CMSSI8", 105.0, 101.5, 8.0)
    assert scripted(letter, script) and scripted(script, letter)
    assert prose_share([letter, script]) == 0.0


def test_letters_of_one_word_on_one_baseline_stay_a_word() -> None:
    # letterspaced, or faked small caps (smaller capitals on the same baseline)
    big = span("W", "CMSS10", 100.0, 100.0, 10.0)
    small = span("ORD", "CMSS10", 105.0, 100.0, 8.0)
    assert not scripted(big, small)
    assert prose_share([big, small]) == 1.0


def test_a_glyph_barely_smaller_is_no_script() -> None:
    a = span("ab", "CMSS10", 100.0, 100.0, 10.0)
    b = span("cd", "CMSS10", 110.0, 101.5, 9.0)  # (a 0.9x size: a word set in another size, not a script)
    assert not scripted(a, b)
