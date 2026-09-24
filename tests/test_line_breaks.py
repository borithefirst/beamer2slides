"""Lines, paragraphs and line breaks (visual hunt, fixer G), on tests/decks/28_line_breaks.tex:
where a paragraph ends, how words are spaced, justified text and \\hfill pieces."""

from functools import lru_cache

from .test_classify import deck, paragraph_text, texts


@lru_cache(maxsize=None)
def lines_deck() -> dict:
    return deck("28_line_breaks")


def paras(slide: int) -> list[dict]:
    return [p for e in texts(lines_deck()["slides"][slide]) for p in e["paragraphs"]]


def para(slide: int, start: str) -> dict:
    return next(p for p in paras(slide) if paragraph_text(p).startswith(start))


def test_title_page_institute_keeps_its_forced_break():
    """\\institute{A\\\\B}: two centred lines of different widths are one paragraph with a soft break."""
    assert paragraph_text(para(0, "Department")) == "Department of Comparative Literature\vUniversity of Geneva"


def test_hfill_attribution_is_its_own_line_not_a_label():
    """\\hfill --- Source after a quote: no tab stop between the dash and the source, and the
    attribution does not run on after the quoted words."""
    quote = para(1, "“Upzoning")
    assert paragraph_text(quote).endswith("renters.”")
    attribution = para(1, "— Op-ed")
    assert "\t" not in paragraph_text(attribution) and attribution["tab_x0"] is None


def test_quotation_is_justified_and_its_indented_first_line_joins():
    """A quotation's \\parindent first line (0.37 em in from the others' left edge at \\footnotesize) and its
    justified lines are one paragraph, marked justified."""
    p = para(1, "Local opposition")
    assert paragraph_text(p).endswith("construction costs (13%).") and len(p["lines"]) == 3
    assert p["justified"] and p["lines"][0]["x0"] > p["text_x0"] + 0.2 * p["size"]


def test_justified_parbox_columns():
    """Two \\parbox columns side by side: lines ending together with stretched word spaces."""
    for start in ("Streams differ", "Randomise"):
        p = para(2, start)
        assert p["justified"] and len(p["lines"]) == 4, start
    assert not para(2, "Limitations").get("justified"), "a one-line heading is not justified"


def test_justified_paragraph_is_written_justified_with_its_first_line_indent():
    from beamer2slides.emit import FontMapper, text_box_requests

    el = next(e for e in texts(lines_deck()["slides"][1]) if paragraph_text(e["paragraphs"][0]).startswith("Local"))
    reqs = text_box_requests(el, "s", "b", 1.5, FontMapper())
    styles = [r["updateParagraphStyle"]["style"] for r in reqs if "updateParagraphStyle" in r]
    assert any(s.get("alignment") == "JUSTIFIED" for s in styles)
    firsts = [s["indentFirstLine"]["magnitude"] for s in styles if "indentFirstLine" in s]
    starts = [s["indentStart"]["magnitude"] for s in styles if "indentStart" in s]
    assert firsts and starts and firsts[0] - starts[0] > 3, "the \\parindent is the first line's indent"


def test_thin_spaces_are_no_break_spaces_and_quads_stay_wide():
    """48\\,000\\,EUR: thin spaces (0.167 em) inside one span are no-break spaces, so the number
    is not broken; a \\quad (1 em) is an em space, not a word space."""
    assert paragraph_text(para(3, "A grant")) == "A grant of 48\u00a0000\u00a0EUR per year for three years"
    assert "MySQL \u2003vs. \u2003RocksDB" in paragraph_text(para(3, "Used by"))
    assert "\u00a0" not in paragraph_text(para(3, "Used by"))


def test_explicit_hyphen_at_a_line_end_is_kept_without_a_space():
    """imidazolidin-\\\\5-one: a hyphen the author typed stays, the next line joins it directly."""
    assert "imidazolidin-5-one" in paragraph_text(para(4, "A ring"))


def test_verse_lines_after_a_line_number_are_not_centred_together():
    """A verse in a tabular {r@{\\quad}l}: the line under an unnumbered one starts with the
    number 4, one \\quad before its words; the two lines are centred on each other only by their
    lengths. Every verse line is its own paragraph."""
    got = [paragraph_text(p) for p in paras(7)]
    assert "hu tha aethelingas ellen fremedon." in got
    assert "4 \u2003Oft Scyld Scefing sceathena threatum" in got


def test_translation_lines_broken_by_hand_stay_apart():
    """The translation beside the verse, a tabular {l}: its lines 2 and 3 are too long for the
    next line's first word, yet short of the measure, under a long first line TeX ended with room
    to spare. A run of hand-broken lines: each its own paragraph, not one wrapped paragraph whose
    lines Slides re-wraps into each other (visual hunt r8, r1_lang_v3 s5: 'valour.Often')."""
    got = [paragraph_text(p) for p in paras(7)]
    for line in ("of the Spear-Danes\u2019 kings in days of old,", "how those princes did deeds of valour.",
                 "Often Scyld Scefing from troops of foes"):
        assert line in got, got


def test_an_attribution_flush_with_the_right_aligned_quote_above_is_right_aligned():
    """A quote set flush right past the margin the page's other text keeps, its attribution alone
    on the line under it ending where the quote ends: right-aligned like the quote (r1_lang_v2 s3:
    left-aligned, its words ran out past the text area in Slides)."""
    from .test_columns import paragraphs, span, text
    size, right = 11.0, 345.0
    rows = [("“Whoever wishes to translate word for word,", 100.0), ("will toil greatly.”", 113.0),
            ("— Maimonides, letter (1199)", 126.0)]
    spans = [span(t, right - len(t) * 0.5 * size, y, size) for t, y in rows]
    spans.append(span("Body text starts at the left margin", 30.0, 180.0, size))
    got = {text(p): p["align"] for p in paragraphs(spans)}
    assert got["— Maimonides, letter (1199)"] == "right", got
    assert got["Body text starts at the left margin"] == "left"


def test_a_word_set_in_two_spans_is_one_word_when_it_would_end_the_line_above():
    """\\textsc{Goldbach}: its capital in one span, its small letters in the next. The capital
    alone would have fitted at the end of the line above, so the line was taken for one TeX
    broke by hand, and the paragraph cut in two there (r2_fonts_pazo s5, V-fonts-8)."""
    from .test_columns import W, paragraphs, span, text
    size = 11.0
    spans = [span("Euler writes freely with infinite series and the modern reader", 30.0, 100.0, size, w=W - 60.0),
             span("must supply the rigour himself. The letters to", 30.0, 113.5, size, w=260.0),
             span("G", 30.0, 127.0, size, w=7.5), span("OLDBACH are essential context.", 37.5, 127.0, size, w=150.0)]
    got = [text(p) for p in paragraphs(spans)]
    assert len(got) == 1 and "letters to GOLDBACH are" in got[0], got


def test_nested_numbers_are_labels_like_their_parents():
    """\\item[2.1] under an enumerate item: its label and text are a tab apart like 1. and 2."""
    got = [paragraph_text(p) for p in paras(5) if p["tab_x0"]]
    assert "2.1\tClean the survey answers" in got and "2.2\tMerge the two waves" in got


def test_quote_on_an_untitled_frame_is_no_title():
    """A frame with no title: a large centred quote is body text, and the flush-right
    --- attribution under it is no hanging label."""
    slide = lines_deck()["slides"][6]
    assert not [e for e in texts(slide) if e["role"] == "title"]
    attribution = para(6, "— a teaching")
    assert attribution["tab_x0"] is None
