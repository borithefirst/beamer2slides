"""Tables misread on real decks (the real-deck review, track C): rows under a blank first cell
that are no wrapped p{} cell, side-by-side centred columns that are no plain table, and a
listing's numbered tail that is code, not a table. Synthetic pages, no PDF."""

from beamer2slides.classify import PageClassifier

from .test_code_columns import LEADING, LEFT, fixed_columns, listing_char, raw_spans
from .test_tables_hunt import CHAR, SIZE, Page, cell_texts, ruled_table, spread, tables, texts


def test_rows_under_a_blank_first_cell_are_rows_not_a_wrapped_cell() -> None:
    """real_pnuc-intro-pnuc slide 6: an `ll` tabular listing designs beside a group's label,
    each on a row of its own under a blank first cell, a row pitch apart (the pitch a p{} cell's
    lines have too). They were read as the next lines of one wrapped cell and joined ("Traditional
    no use No use episodes"), which Slides then wrapped by itself. Each line starts with a capital,
    its first word would have fitted on the line above, or the line above is no full line of a
    justified column: rows of their own."""
    page = Page()
    rows = [[(55, "Group"), (150, "Design")],
            [(55, "No-use"), (150, "Traditional no use")],
            [(150, "No use episodes")],
            [(55, "Active"), (150, "Generalized prevalent new user")],
            [(150, "Prevalent new user")],
            [(150, "Hierarchical prevalent new user")]]
    ruled_table(page, rows, [70, 86, 98, 110, 122, 134], x1=320)
    (t,) = tables(page.elements())
    assert cell_texts(t) == [["Group", "Design"], ["No-use", "Traditional no use"], ["", "No use episodes"],
                             ["Active", "Generalized prevalent new user"], ["", "Prevalent new user"],
                             ["", "Hierarchical prevalent new user"]]
    assert t.get("row_lines") is None


def test_a_capitalised_line_under_a_full_justified_line_still_wraps() -> None:
    """The other side of the rule above: a justified p{} cell's next line may start with a
    capital (a name); its line above ends at the column's edge, where another full line of the
    column ends too, and its first word would not have fitted there."""
    page = Page()
    rows = [[(55, "Term"), (140, "Meaning")], [(55, "Reef")], [(140, "Rarotonga and the sea")], [(55, "Atoll")],
            [(140, "near Tahiti")]]
    ruled_table(page, rows, [70, 86, 98, 110, 122], x1=280)
    # (both first lines set out to one edge, as a justified column does)
    spread(page, "a ridge of rock and coral near", 140, 265, 86)
    spread(page, "a ring of coral round a lagoon", 140, 265, 110)
    (t,) = tables(page.elements())
    assert cell_texts(t)[1][1] == "a ridge of rock and coral near Rarotonga and the sea"
    assert t.get("row_lines") == [1, 2, 2]


def centred_columns(page: Page, offset: float) -> None:
    """Two contact columns side by side, each line centred on its column's axis (beamer's
    columns with \\begin{center}), the second column `offset` pt lower than the first."""
    for axis, lines, drop in ((92.0, ["Data Scientist", "roy@zefsdata.com", "@roycoding"], 0.0),
                              (270.0, ["Research Data Scientist", "stevekochscience@gmail.com", "@skoch3"], offset)):
        for k, line in enumerate(lines):
            width = sum(len(w) for w in line.split()) * CHAR * SIZE + 0.33 * SIZE * (len(line.split()) - 1)
            page.words(line, axis - width / 2, 100 + 13.5 * k + drop)


def test_side_by_side_centred_columns_are_no_plain_table() -> None:
    """real_dstalk-datascience-ta slide 17: two centred columns of contact lines, the second 2.2
    pt lower line for line (each column centred on its own). As a plain table emit re-fitted its
    columns off their centres while each name above stayed a box of its own. A tabular sets a
    row's cells on one baseline; these are columns of text."""
    page = Page()
    centred_columns(page, 2.2)
    els = page.elements()
    assert not tables(els)
    words = " ".join("".join(r["text"] for r in p["runs"]) for e in texts(els) for p in e["paragraphs"])
    assert "stevekochscience@gmail.com" in words
    # (on one baseline, the same lines are a rule-less table's rows)
    level = Page()
    centred_columns(level, 0.0)
    (t,) = tables(level.elements())
    assert cell_texts(t)[1] == ["roy@zefsdata.com", "stevekochscience@gmail.com"]


def test_a_numbered_listings_tail_of_braces_is_no_plain_table() -> None:
    """real_esi-dev1-slides slides 26, 28, 30: a listing's last lines, its line numbers beside
    code and closing braces at decreasing indents, became a rule-less plain table (the braces in
    one column, their indentation lost). Code lines are never a plain table."""
    code = ["for (int i = 0; i < n; ++i) {", "    if (t[i] < 0) {", "        c++;", "    }", "}"]
    chars = fixed_columns(code, "SFSS1000")
    numbers = [listing_char(str(i + 5), LEFT - 9.0, 100.0 + i * LEADING, "SFSS1000") for i in range(len(code))]
    spans = raw_spans(sorted(numbers + chars, key=lambda c: (c.origin[1], c.origin[0])))
    page = PageClassifier({"index": 0, "label": "1", "size": [364.0, 273.0], "spans": spans, "images": [],
                           "drawings": [], "links": []}, SIZE)
    els = page.classify()["elements"]
    assert not tables(els)
