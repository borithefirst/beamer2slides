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
    lengths. Every verse line is its own paragraph. (The translation's lines beside it, as long
    as the column is wide and without a tell of their own, still read as one wrapped paragraph.)"""
    got = [paragraph_text(p) for p in paras(7)]
    assert "hu tha aethelingas ellen fremedon." in got
    assert "4 \u2003Oft Scyld Scefing sceathena threatum" in got


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
