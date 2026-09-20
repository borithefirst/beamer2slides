"""What the dialect's newer half does on a real document (opt-in, marker `docs`).

`python -m pytest -m docs tests/test_docs_live_styles.py`

`test_docs_live.py` says the loop works; this one says the *styling* survives it —
the faces, sizes and small caps a run carries, the indents, line height, shading
and space a paragraph carries, and the line breaks a table is written across.
Half of it is what Drive's importer keeps (measured in docs/google-docs.md) and
half is what only `batchUpdate` can write, which a push reaches through
`doc_merge.carry_unimported` and `doc_merge.tidy_requests`.

**Not yet run.** Every assertion here follows either a measured line of
docs/google-docs.md or the offline tests in `test_doc_ir` / `test_doc_merge`; what
no offline test can show is whether the importer minds the newlines inside a
`<table>`, and whether it puts a face and a size on the run or on the paragraph's
named style. That is what this file is for.

Skipped when the Google token needs a browser consent.
"""

import pytest

from .test_docs_live import OUT, Paper, find, google_unavailable  # noqa: F401

pytestmark = pytest.mark.docs

SOURCE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>What the dialect can say</title>
</head>
<body>
<h1 id="heading:styling">Styling</h1>
<p id="paragraph:faces">Two faces: <span style="font-family:Consolas">Consolas</span> and \
<span style="font-family:Roboto Mono">Roboto Mono</span>, and \
<span style="font-size:18pt">eighteen points</span>.</p>
<p id="paragraph:caps"><span data-smallcaps="1">Small caps</span> and plain words.</p>
<p id="paragraph:measured" style="margin-left:36pt;text-indent:18pt;line-height:1.5" \
data-shading="#fff2cc" data-space-above="12" data-space-below="6">A paragraph set apart.</p>
<table id="table:numbers">
 <tr><td><p>Region</p></td><td><p>Sales</p></td></tr>
 <tr><td><p>North</p></td><td><p>1200</p></td></tr>
 <tr><td><p>South</p></td><td><p>870</p></td></tr>
</table>
<p id="paragraph:closing">The closing paragraph.</p>
</body>
</html>
"""


@pytest.fixture
def paper(request):
    if reason := google_unavailable():
        pytest.skip(reason)
    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, drive_service
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{request.node.name}.html"
    path.write_text(SOURCE, encoding="utf-8")
    doc_sync.base_path(path).unlink(missing_ok=True)
    info = doc_sync.push(path, name=f"b2s docs test: {request.node.name}")
    yield Paper(path, info["document"])
    drive_service(credentials()).files().delete(fileId=info["document"]).execute()


def document(paper) -> dict:
    from beamer2slides.google_auth import credentials, docs_service
    _, ir = paper.sync_module.read_document(docs_service(credentials()), paper.ident)
    return ir


def test_a_face_and_a_size_come_back_as_themselves(paper):
    """`<code>` made every monospaced face one face; a face is now carried as itself.
    Measured: a single family name imports verbatim, a fallback list does not."""
    runs = find(document(paper), "Two faces")["runs"]
    faces = [r.get("font") for r in runs if r.get("font")]
    assert faces == ["Consolas", "Roboto Mono"]
    assert [r.get("fontsize") for r in runs if r.get("fontsize")] == [18]
    assert "font-family:Consolas" in paper.text and "font-family:Roboto Mono" in paper.text
    paper.settled()


def test_small_caps_reaches_the_document_although_no_html_carries_it(paper):
    """The import cannot make it; `tidy_requests` writes it in the settle's batch."""
    runs = find(document(paper), "Small caps")["runs"]
    assert [r.get("smallcaps") for r in runs] == [True, None]
    assert 'data-smallcaps="1"' in paper.text
    paper.settled()


def test_the_measurements_of_a_paragraph_survive_the_push(paper):
    block = find(document(paper), "A paragraph set apart")
    assert (block["indent"], block["indent_first"]) == (36.0, 18.0)
    assert block["line_spacing"] == 1.5
    # These three no import carries: they are written after it.
    assert block["shading"] == "#fff2cc"
    assert (block["space_above"], block["space_below"]) == (12.0, 6.0)
    # Never as CSS: `background-color` on a `<p>` is a character highlight (measured).
    assert 'data-shading="#fff2cc"' in paper.text
    assert "background-color:#fff2cc" not in paper.text
    paper.settled()


def test_a_table_written_one_row_per_line_brings_no_white_space_with_it(paper):
    """The newlines sit where an HTML parser has nowhere to put text. Whether Drive's
    importer agrees is what this test is here to say."""
    ir = document(paper)
    table = [b for b in ir["blocks"] if b["kind"] == "table"][0]
    assert [[find_text(cell) for cell in row] for row in table["rows"]] == [
        ["Region", "Sales"], ["North", "1200"], ["South", "870"]]
    assert not [b for b in ir["blocks"]
                if b.get("runs") and not "".join(r["text"] for r in b["runs"]).strip()]
    assert " <tr><td><p>North</p></td><td><p>1200</p></td></tr>" in paper.text.splitlines()
    paper.settled()


def find_text(cell: list[dict]) -> str:
    return "".join(r["text"] for block in cell for r in block.get("runs", []))


def test_a_reader_who_changes_a_face_keeps_it_through_a_source_edit(paper):
    """The face round-trips, so the merge may name `weightedFontFamily` on a restyle
    without undoing a choice made in the browser."""
    from beamer2slides import doc_ir
    from beamer2slides.google_auth import credentials, docs_service
    docs = docs_service(credentials())
    block = find(document(paper), "The closing paragraph")
    docs.documents().batchUpdate(documentId=paper.ident, body={"requests": [
        {"updateTextStyle": {
            "range": {"startIndex": block["span"][0], "endIndex": block["span"][1] - 1},
            "textStyle": {"weightedFontFamily": {"fontFamily": "Georgia"}},
            "fields": "weightedFontFamily"}}]}).execute()
    paper.edit("The closing paragraph.", "The closing paragraph, rewritten.")
    paper.sync()
    runs = find(document(paper), "The closing paragraph")["runs"]
    assert all(r.get("font") == "Georgia" for r in runs)
    assert doc_ir.runs_text(runs) == "The closing paragraph, rewritten."
    paper.settled()
