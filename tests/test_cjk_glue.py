"""TeX's glue between Chinese or Japanese text and Latin text (cjk_glue; r2 ledger
cjk-latin-xkanjiskip-lost, real_slide-20250221): luatexja's xkanjiskip is 2.4 pt whatever the size,
0.116 em of the title page's 20.7 pt Fira Sans and 0.139 em of a frame title's 17.2 pt, under
JOIN_GAP, so 'HVCAN 上の IP 電話' came out 'HVCAN上のIP電話' in Slides. Synthetic characters and
spans, no PDF."""

from beamer2slides import extract, text_layout
from beamer2slides.cjk_glue import CJK_GLUE, as_tex, boundary, in_glue, line_glue
from beamer2slides.classify import PageClassifier, new_line, new_paragraph
from beamer2slides.compare import norm_text
from beamer2slides.inverse import latex_escape
from beamer2slides.json_types import JsonObject
from beamer2slides.pdf import Char, char_box
from beamer2slides.raw_types import RawSpan

from .test_span_joining import Page, Shown

G = CJK_GLUE
JA = "HaranoAjiGothic-Bold"
LATIN = "FiraSans-Bold"
BASELINE = 100.0


def chars(pieces: list[tuple[str, str, float, float]]) -> list[Char]:
    """Characters of (text, font, size, pen gap in pt before it) pieces on one baseline: a CJK
    glyph one em wide, a Latin one half an em, the glyphs of a piece touching."""
    out: list[Char] = []
    x = 20.0
    for text, font, size, gap in pieces:
        x += gap
        for c in text:
            adv = size * (1.0 if boundary(c, "a") else 0.5)
            out.append(Char(c=c, font=font, size=size, color=0, alpha=255, origin=(x, BASELINE),
                            box=char_box(x, BASELINE, 1.0, 0.0, adv, size, 0.88, -0.12), dir=(1.0, 0.0), obj=len(out),
                            font_id=0, advance=adv, synthetic=False, ascent=0.88, descent=-0.12, exact_advance=True))
            x += adv
    return out


def texts(pieces: list[tuple[str, str, float, float]]) -> list[str]:
    shown = chars(pieces)
    return [s.text for s in extract.spans(Page(shown, {}), Shown(), False, shown, {})]


def test_the_glue_is_a_six_per_em_space_in_the_latin_span() -> None:
    # the title page: オレオレ IP 電話網, 2.4 pt at 19.88 / 20.66 pt (0.116 em of Fira's size)
    assert texts([("オレ", JA, 19.88, 0), ("IP", LATIN, 20.66, 2.4), ("電話", JA, 19.88, 2.4)]) == \
        ["オレ", G + "IP" + G, "電話"]
    # a frame title (17.21 pt): HVCAN 上の
    assert texts([("HVCAN", LATIN, 17.21, 0), ("上の", JA, 16.57, 2.4)]) == ["HVCAN" + G, "上の"]
    # body text (14.35 pt): 0.167 em, which JOIN_GAP read as a plain space in the Japanese span
    assert texts([("VPN", LATIN, 14.35, 0), ("を", JA, 13.8, 2.4)]) == ["VPN" + G, "を"]
    # one font for both: the glue stays inside the span
    assert texts([("IP", JA, 20.0, 0), ("電話", JA, 20.0, 2.4)]) == ["IP" + G + "電話"]


def test_no_gap_or_a_word_space_is_no_glue() -> None:
    # 上羽 未栞（a.k.a. ... ）: no glue after an opening bracket, before a closing one
    assert texts([("栞（", JA, 11.5, 0), ("aka", LATIN, 11.96, 0), ("）", JA, 11.5, 0)]) == ["栞（", "aka", "）"]
    assert texts([("IP", LATIN, 20.0, 0), ("電話", JA, 20.0, 1.0)]) == ["IP", "電話"]  # (0.05 em: a kern)
    # a gap a plain space comes nearer stays one (xeCJK's CJKecglue, a typed space)
    assert texts([("IP", LATIN, 10.0, 0), ("電話", JA, 10.0, 2.4)]) == ["IP", " 電話"]  # (0.24 em)
    # Latin text on both sides is not this
    assert texts([("IP", LATIN, 20.0, 0), ("x", "LMSans10-Regular", 20.0, 2.4)]) == ["IP", "x"]


def test_the_glue_band() -> None:
    assert boundary("P", "電") and boundary("の", "1") and boundary("fi", "の") and not boundary("P", "x")
    assert not boundary("한", "a") and not boundary("電", "話") and not boundary(" ", "電")
    assert in_glue(0.116) and in_glue(0.168) and not in_glue(0.05) and not in_glue(0.25)
    assert line_glue("た", "M") and line_glue("1", "年") and not line_glue("（", "a") and not line_glue("a", "）")


def raw_span(i: int, text: str, font: str, x0: float, x1: float, baseline: float, size: float) -> RawSpan:
    return {"id": f"s{i}", "text": text, "font": font, "size": size, "color": "#000000", "alpha": 255,
            "origin": [x0, baseline], "bbox": [x0, baseline - 0.88 * size, x1, baseline + 0.12 * size],
            "dir": [1.0, 0.0], "smallcaps": False}


def run_texts(lines: list[list[RawSpan]]) -> list[str]:
    page = PageClassifier({"index": 0, "label": "1", "size": [453, 255], "spans": [s for ln in lines for s in ln],
                           "images": [], "drawings": [], "links": []}, 10)
    spans = page.spans()
    made = [new_line([s for s in spans if s.id in {r["id"] for r in ln}]) for ln in lines]
    return [r["text"] for r in PageClassifier.runs(new_paragraph(made, align="left", reason=None), "", False, None)]


def test_classify_writes_the_glue_between_spans_and_lines() -> None:
    # spans extract left apart (another element's glyphs): the gap is read as the glue
    assert run_texts([[raw_span(0, "オレオレ", JA, 28.4, 107.9, 75.5, 19.88),
                       raw_span(1, "IP", LATIN, 110.3, 128.7, 75.5, 20.66),
                       raw_span(2, "電話網", JA, 131.1, 190.7, 75.5, 19.88)]]) == ["オレオレ", G + "IP" + G, "電話網"]
    # a line TeX broke at the glue: joined with it, in the Latin side's run
    assert "".join(run_texts([[raw_span(0, "黒電話を利用した", JA, 20, 134.4, 100, 14.35)],
                              [raw_span(1, "IP", LATIN, 20, 33, 117, 14.35),
                               raw_span(2, "電話", JA, 35.4, 64.1, 117, 14.35)]])) == "黒電話を利用した" + G + "IP" + G + "電話"


def test_readers_read_the_glue_back() -> None:
    # pull: TeX sets its glue again, the source said 'VPNを'; the edge fill stays one space
    assert latex_escape("VPN" + G + "を使う") == "VPNを使う"
    assert latex_escape("x" + G + G + " y") == "x y" and latex_escape("a" + G + "b") == "a b"
    assert as_tex("その" + G + "1") == "その1"
    # compare: a space, as on both sides
    assert norm_text("VPN" + G + "を") == norm_text("VPN を")


def test_slides_may_break_after_the_glue() -> None:
    style: JsonObject = {"fontFamily": "Lato", "fontSize": 20.0}
    text = "ABCD" + G + "EFGH"
    lines = text_layout.wrap(text, [style] * len(text), 70.0)
    assert [text[a:b] for a, b, _ in lines] == ["ABCD" + G, "EFGH"], lines
