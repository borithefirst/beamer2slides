"""What the dialect's newer half does on a real document (opt-in, marker `docs`).

`python -m pytest -m docs tests/test_docs_live_styles.py`

`test_docs_live.py` says the loop works; this one says the *styling* survives it —
the faces, sizes and small caps a run carries, the indents, line height, shading
and space a paragraph carries, and the line breaks a table is written across.
Half of it is what Drive's importer keeps (measured in docs/google-docs.md) and
half is what only `batchUpdate` can write, which a push reaches through
`doc_merge.carry_unimported` and `doc_merge.tidy_requests`.

The last one is the theme question. `doc_world` has named styles now and the campaign
checks the arithmetic of inheritance against them, but only against a theme this repo
wrote: whether a heading a *Google* theme centres comes through a source restyle still
centred is a thing only a real document can say.

**Not yet run.** Every assertion here follows either a measured line of
docs/google-docs.md or the offline tests in `test_doc_ir` / `test_doc_merge`; what
no offline test can show is whether the importer minds the newlines inside a
`<table>`, whether it puts a face and a size on the run or on the paragraph's
named style, and whether named-and-unset really means "inherit". That is what this
file is for.

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


# A .docx whose Heading 1 *style* is centred, bold and blue, and whose heading
# paragraph says none of the three itself. Drive's HTML import cannot make such a
# document (a `<style>` rule does not reach a named style, docs/google-docs.md), and
# the API has no request that writes one either, so an import is the only way in.
THEME_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:jc w:val="center"/></w:pPr>
<w:rPr><w:b/><w:color w:val="1155CC"/></w:rPr></w:style></w:styles>"""

THEME_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>A themed heading</w:t></w:r></w:p>
<w:p><w:r><w:t>A paragraph under it.</w:t></w:r></w:p>
</w:body></w:document>"""


def test_a_heading_the_theme_centres_survives_a_source_restyle(request):
    """The whole of the theme question, live: a heading whose centring, bold and
    colour live in the document's HEADING_1, restyled through the file, must come out
    centred, bold and blue still.

    The mechanism is that every field the merge owns is written *named with no value*
    when the file says nothing, which the API documents as "back to what you inherit".
    The campaign checks the *arithmetic* of that offline — `doc_world` grew named
    styles (`fuzz_docs.THEME`, the `themed` shape) and the oracle judges what a
    paragraph inherits (`doc_loss_oracle._inherited_findings`) — but only against a
    theme this repo wrote. What is still owed to a live run is the premise underneath:
    that Google's own editor and importer behave as the world assumes, and that a
    field named with no value really does fall back rather than clear.

    The premise is checked before the claim. If Drive's .docx import does not put the
    style's centring into the document's named style, the first assertion says so and
    the test has measured that instead — which is worth knowing too.
    """
    import io

    from googleapiclient.http import MediaIoBaseUpload

    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, docs_service, drive_service
    from .test_docs_live import docx

    if reason := google_unavailable():
        pytest.skip(reason)
    drive = drive_service(credentials())
    ident = drive.files().create(
        body={"name": f"b2s docs test: {request.node.name}", "mimeType": doc_sync.DOC_MIME},
        media_body=MediaIoBaseUpload(
            io.BytesIO(docx(THEME_DOCX, THEME_STYLES)), mimetype=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        fields="id").execute()["id"]
    docs = docs_service(credentials())

    def raw() -> dict:
        return docs.documents().get(documentId=ident).execute()

    def heading_style(doc: dict) -> dict:
        return [s for s in doc["namedStyles"]["styles"]
                if s["namedStyleType"] == "HEADING_1"][0]

    def heading_paragraph(doc: dict) -> dict:
        return [e["paragraph"] for e in doc["body"]["content"]
                if "A themed heading" in "".join(
                    el.get("textRun", {}).get("content", "")
                    for el in e.get("paragraph", {}).get("elements", []))][0]

    try:
        before = raw()
        style = heading_style(before)
        assert style.get("paragraphStyle", {}).get("alignment") == "CENTER", (
            "the premise: Drive's importer did not put the style's centring into "
            "HEADING_1, so this document cannot answer the question")
        assert style.get("textStyle", {}).get("bold") is True
        # And the heading itself says none of it: that is the rule the whole answer
        # rests on — a paragraph reports what is set on it, never what it inherits.
        paragraph = heading_paragraph(before)
        assert "alignment" not in paragraph["paragraphStyle"]
        assert not any(el.get("textRun", {}).get("textStyle", {}).get("bold")
                       for el in paragraph["elements"])

        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{request.node.name}.html"
        doc_sync.adopt(ident, path)
        paper = Paper(path, ident)
        assert "text-align" not in paper.text   # the file claims none of the theme
        # A source restyle of that very block: the words change and the styling with
        # them, which is what makes the merge name every field it owns.
        paper.edit("A themed heading", "<em>A themed heading</em>, restyled")
        assert paper.sync()["requests"] > 0

        after = raw()
        assert heading_style(after)["paragraphStyle"]["alignment"] == "CENTER"
        paragraph = heading_paragraph(after)
        assert paragraph["paragraphStyle"].get("alignment") in (None, "CENTER")
        assert paragraph["paragraphStyle"]["namedStyleType"] == "HEADING_1"
        # Nothing of ours pinned the run against the theme either.
        assert not any(el.get("textRun", {}).get("textStyle", {}).get("bold") is False
                       for el in paragraph["elements"])
        paper.settled()
    finally:
        drive.files().delete(fileId=ident).execute()


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
