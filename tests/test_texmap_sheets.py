"""SyncTeX sheets vs box resources (`texmap.synctex_pages`).

lualatex writes a box resource (`\\saveboxresource`: transparency groups and shadings, which
beamer's block shadows produce) into the SyncTeX file with the same `{`...`}` shape as a page,
but closes it with `}0` instead of `}<sheet>`. Counted as pages, every page after the first is
mapped to the wrong frame, which gives `pull` the wrong slide keys - it then writes its edits
into the wrong frames. Found on `tests/decks/stress/talk.tex` (lualatex, 89 pages, 15 box
resources); the pdflatex decks have none, so the existing suites never saw it.
"""

import gzip
from pathlib import Path

from beamer2slides.texmap import synctex_pages

PREAMBLE = """SyncTeX Version:1
Input:1:talk.tex
Output:pdf
Magnification:1000
Unit:1
X Offset:0
Y Offset:0
Content:
"""


def write(tmp_path: Path, body: str, gz: bool = False) -> Path:
    path = tmp_path / ("talk.synctex.gz" if gz else "talk.synctex")
    text = PREAMBLE + body
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def sheet(number: int, line: int) -> str:
    return f"{{{number}\n(1,{line}:0,16782118:526254,526254,0\n)\n}}{number}\n"


def resource(number: int, line: int) -> str:
    """What lualatex writes for a saved box: opened with the page number, closed with 0."""
    return f"{{{number}\n(1,{line}:0,16782118:526254,526254,0\n)\n}}0\n"


def test_box_resources_are_not_pages(tmp_path):
    body = resource(1, 31) + sheet(1, 10) + sheet(2, 20) + resource(2, 49) + sheet(3, 30)
    pages = synctex_pages(write(tmp_path, body))
    assert len(pages) == 3
    assert [sorted(p.votes)[0][1] for p in pages] == [10, 20, 30]


def test_a_box_resource_inside_a_sheet_counts_for_that_page(tmp_path):
    body = "{1\n(1,10:0,16782118:526254,526254,0\n)\n" + resource(1, 11) + "}1\n"
    pages = synctex_pages(write(tmp_path, body))
    assert len(pages) == 1
    assert sorted(pages[0].votes) == [("talk.tex", 10), ("talk.tex", 11)]


def test_plain_sheets_are_unchanged(tmp_path):
    pages = synctex_pages(write(tmp_path, "".join(sheet(n, n * 10) for n in range(1, 6)), gz=True))
    assert [sorted(p.votes)[0][1] for p in pages] == [10, 20, 30, 40, 50]


def test_a_missing_file_gives_no_pages(tmp_path):
    assert synctex_pages(tmp_path / "nothing.synctex.gz") == []
