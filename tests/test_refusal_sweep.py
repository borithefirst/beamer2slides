"""The refusal sweep reads back what it wrote, resumes past what it did, and tallies it right.

The sweep itself walks a whole machine, so what is held here is its parts: a part file's line
becomes a `Swept` record where it enters, a slice skips the documents its part already names, and
the table counts pages, first reasons and documents the way its docstring says.
"""

import json
from pathlib import Path

import pytest

from beamer2slides.devtools import refusal_sweep as sweep
from beamer2slides.json_types import JsonObject

DECKS = Path(__file__).parent / "decks"
BASIC = DECKS / "out" / "01_basic.pdf"


def test_a_part_files_lines_are_read_as_the_records_they_say() -> None:
    checked = sweep.swept(json.dumps({"path": "a.pdf", "pages": 3, "secs": 0.1,
                                      "reasons": [[], ["ICC profiles", "objects"], []]}))
    assert checked == sweep.Swept(path="a.pdf", open_error=None, timed_out=False,
                                  reasons=[[], ["ICC profiles", "objects"], []])
    unopened = sweep.swept(json.dumps({"path": "b.pdf", "open_error": "PdfError: no trailer"}))
    assert unopened is not None and unopened.open_error == "PdfError: no trailer"
    assert unopened.reasons == []
    late = sweep.swept(json.dumps({"path": "c.pdf", "timeout": 90.0}))
    assert late is not None and late.timed_out
    # What a killed worker leaves half-written, or a line of something else, is no record.
    assert sweep.swept('{"path": "d.pdf", "reas') is None
    assert sweep.swept(json.dumps(["not", "a", "record"])) is None
    assert sweep.swept(json.dumps({"pages": 2})) is None
    assert sweep.swept(json.dumps({"path": "e.pdf", "reasons": [[3]]})) is None


def test_the_table_counts_pages_first_reasons_and_documents(tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    part = tmp_path / "part0.ndjson"
    lines: list[JsonObject] = [
        {"path": "one.pdf", "pages": 3,
         "reasons": [["ICC profile v4"], ["objects", "ICC profile v2"], []]},
        {"path": "two.pdf", "pages": 1, "reasons": [[]]},
        {"path": "three.pdf", "open_error": "PdfError: broken"},
        {"path": "four.pdf", "timeout": 90.0}]
    part.write_text("".join(json.dumps(x) + "\n" for x in lines) + "{half a line", encoding="utf-8")

    sweep.tally([str(part)], top=10, docs=True)
    said = capsys.readouterr().out
    assert "documents 3 (1 with a refused page), pages 4 (2 refused, 50.00%), timeouts 1" in said
    assert "open errors: {'PdfError': 1}" in said
    # ICC twice on pages, first once, in one document; `objects` once, first once.
    assert "      2       1      1  ICC" in said
    assert "      1       1      1  objects" in said
    assert "one.pdf" in said and "2/3" in said


def test_the_cli_tallies_the_parts_it_is_given(tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    part = tmp_path / "p.ndjson"
    part.write_text(json.dumps({"path": "x.pdf", "pages": 1, "reasons": [["!ValueError: x"]]}) + "\n",
                    encoding="utf-8")
    assert sweep.main(["tally", str(part)]) == 0
    assert "(crash) ValueError" in capsys.readouterr().out


@pytest.mark.needs_decks("out/01_basic.pdf")
def test_a_slice_checks_its_files_once_and_resumes_past_what_it_did(tmp_path: Path) -> None:
    files = tmp_path / "files.json"
    files.write_text(json.dumps([str(BASIC)]), encoding="utf-8")
    assert sweep.read_list(files) == [str(BASIC)]
    out = tmp_path / "part0.ndjson"
    one = sweep.Slice(files=files, n=1, i=0, out=out, pages=2, timeout=90.0, budget=600.0)

    sweep.run_slice(one)
    [line] = out.read_text(encoding="utf-8").splitlines()
    rec = sweep.swept(line)
    assert rec is not None and rec.path == str(BASIC) and rec.open_error is None
    assert len(rec.reasons) == 2 and rec.reasons == [[], []], "a built deck renders on the pure reader"

    sweep.run_slice(one)                                # the part already names it
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    other = sweep.Slice(files=files, n=2, i=1, out=tmp_path / "part1.ndjson", pages=2,
                        timeout=90.0, budget=600.0)
    sweep.run_slice(other)                              # not this slice's file
    assert not other.out.exists() or other.out.read_text(encoding="utf-8") == ""
