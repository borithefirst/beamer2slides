"""The Google Docs loop on a real document (opt-in, marker `docs`).

`python -m pytest -m docs tests/test_docs_live.py`

Each test pushes the canonical file below to a document of its own, edits both
sides, syncs, and deletes the document again — a minute's worth of Google calls in
all. The offline suite (`test_doc_ir`, `test_doc_merge`, `test_doc_sync`) says what
the plan should be; this one says the document agrees, which is the only place the
importer's own decisions (a list's glyphs, a table's index space, the style an
insert inherits) can be seen at all.

Every test ends the same way: **a second sync writes nothing**. That is the property
the whole design rests on — after a sync the file is regenerated from the document,
so file, document and base say one thing.

Skipped when the Google token needs a browser consent. Files stay in
`out/docs-tests/` of the main checkout; the documents do not stay anywhere.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from .test_slides_alignment import MAIN, google_unavailable

pytestmark = pytest.mark.docs

OUT = Path(os.environ.get("B2S_DOCS_TESTS_OUT", MAIN / "out" / "docs-tests"))
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

SOURCE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>What the sync is for</title>
</head>
<body>
<h1 id="heading:the-report">The report</h1>
<p id="paragraph:opening">The opening paragraph, with <b>bold words</b> and a
<a href="https://example.com/">link</a> in it.</p>
<table id="table:numbers"><tr><td><p>Region</p></td><td><p>Sales</p></td></tr>\
<tr><td><p>North</p></td><td><p>1200</p></td></tr>\
<tr><td><p>South</p></td><td><p>870</p></td></tr></table>
<p id="paragraph:closing">The closing paragraph.</p>
<ul>
 <li id="item:first">the first point</li>
 <li id="item:second">the second point</li>
</ul>
</body>
</html>
"""


TYPE_NOW = '''"""The hook: somebody types into the document between sync's read and its write."""
import sys

from beamer2slides import doc_sync
from beamer2slides.google_auth import credentials, docs_service

ident, after, text = sys.argv[1:4]
docs = docs_service(credentials())
_, ir = doc_sync.read_document(docs, ident)
block = next(b for b in ir["blocks"]
             if "".join(r["text"] for r in b.get("runs", [])).startswith(after))
doc_sync.send(docs, ident, [{"insertText": {"location": {"index": block["span"][1] - 1},
                                            "text": text}}])
'''


def find(ir: dict, starts: str) -> dict:
    """The block whose words begin with `starts`, wherever it is — table cells too."""
    def walk(blocks):
        for block in blocks:
            for row in block.get("rows", []):
                for cell in row:
                    if found := walk(cell):
                        return found
            if "".join(r["text"] for r in block.get("runs", [])).startswith(starts):
                return block
        return None
    found = walk(ir["blocks"])
    assert found is not None, f"no block starting {starts!r}"
    return found


class Paper:
    """A canonical file and the document it was pushed to."""

    def __init__(self, path: Path, ident: str):
        from beamer2slides import doc_sync
        self.path, self.ident, self.sync_module = path, ident, doc_sync

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def edit(self, old: str, new: str) -> None:
        """What a commit to the canonical file does."""
        assert old in self.text, f"{old!r} is not in the file"
        self.path.write_text(self.text.replace(old, new, 1), encoding="utf-8")

    def typed(self, after: str, text: str) -> None:
        """What a reader typing at the end of a paragraph does, in the document."""
        from beamer2slides.google_auth import credentials, docs_service
        docs = docs_service(credentials())
        _, ir = self.sync_module.read_document(docs, self.ident)
        block = find(ir, after)
        self.sync_module.send(docs, self.ident, [{"insertText": {
            "location": {"index": block["span"][1] - 1}, "text": text}}])

    def rewrote(self, after: str, old: str, new: str) -> None:
        """What a reader rewriting a word in a paragraph does, in the document."""
        from beamer2slides import doc_ir
        from beamer2slides.google_auth import credentials, docs_service
        docs = docs_service(credentials())
        _, ir = self.sync_module.read_document(docs, self.ident)
        block = find(ir, after)
        words = "".join(r["text"] for r in block["runs"])
        at = block["span"][0] + doc_ir.utf16_len(words[:words.index(old)])
        end = at + doc_ir.utf16_len(old)
        self.sync_module.send(docs, self.ident, [
            {"insertText": {"location": {"index": end}, "text": new}},
            {"deleteContentRange": {"range": {"startIndex": at, "endIndex": end}}}])

    def moved(self, key: str, after: str | None = None) -> None:
        """What moving a section in the canonical file does.

        `to_html` writes one block per line, so a reorder in the file is exactly this:
        a line taken out and put back somewhere else. `after` names the block it should
        follow; None puts it at the end of the body.
        """
        lines = self.text.split("\n")
        which = next(i for i, line in enumerate(lines) if f'id="{key}"' in line)
        line = lines.pop(which)
        where = (next(i for i, l in enumerate(lines) if l == "</body>") if after is None
                 else next(i for i, l in enumerate(lines) if f'id="{after}"' in l) + 1)
        lines.insert(where, line)
        self.path.write_text("\n".join(lines), encoding="utf-8")

    def sync(self, **kwargs) -> dict:
        return self.sync_module.sync(self.path, **kwargs)

    def settled(self) -> dict:
        """One more sync, which must write nothing: the convergence property."""
        again = self.sync()
        assert again["requests"] == 0, f"a second sync still writes: {again['applied']}"
        assert again["conflicts"] == []
        return again


@pytest.fixture(scope="module")
def google():
    if reason := google_unavailable():
        pytest.skip(reason)


@pytest.fixture
def paper(google, request):
    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, drive_service
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{request.node.name}.html"
    path.write_text(SOURCE, encoding="utf-8")
    doc_sync.base_path(path).unlink(missing_ok=True)
    info = doc_sync.push(path, name=f"b2s docs test: {request.node.name}")
    assert info["anchored"] == info["blocks"], "every block should carry a named range"
    yield Paper(path, info["document"])
    drive_service(credentials()).files().delete(fileId=info["document"]).execute()


def test_a_push_survives_the_importer_and_a_sync_writes_nothing(paper):
    """What the importer built reads back as what the file said."""
    text = paper.text
    assert f'content="{paper.ident}"' in text          # the file says where it lives
    assert "<ul>" in text and "<ol>" not in text       # an imported list reports no glyphs
    assert "<b>bold words</b>" in text
    assert '<a href="https://example.com/">link</a>' in text
    # Docs paints a link blue and underlined itself; the file must not say so again.
    assert "text-decoration" not in text and "#0000ee" not in text
    assert "<td><p>870</p></td>" in text
    paper.settled()


def test_both_sides_edit_and_the_merge_keeps_both(paper):
    paper.edit("The report", "The report, revised")
    paper.edit("<td><p>870</p></td>", "<td><p>905</p></td>")
    paper.edit("<li id=\"item:second\">the second point</li>",
               "<li id=\"item:second\">the second point, sharpened</li>\n"
               " <li>a third point the source added</li>")
    paper.typed("The opening paragraph", " Typed by a reader.")
    paper.typed("1200", " and steady")

    info = paper.sync()
    assert info["conflicts"] == [] and info["requests"] > 0
    text = paper.text
    assert "The report, revised" in text                     # the source's
    assert "Typed by a reader." in text                      # the document's
    assert "<td><p>905</p></td>" in text                     # a cell the source wrote
    assert "<td><p>1200 and steady</p></td>" in text         # a cell the reader wrote
    assert "the second point, sharpened" in text
    # An added paragraph joins the list it was written into unless it is told not to;
    # this one is a list item and must come back as one, not as a fourth word of the
    # item above it.
    assert "<li id=\"item:a-third-point-the-source-added\">a third point the source added</li>" in text
    paper.settled()


def test_a_block_added_at_the_end_becomes_its_own_paragraph(paper):
    """The body's last newline cannot be written past: the break goes in first."""
    paper.edit("</ul>", "</ul>\n<p>A paragraph the source added at the very end.</p>")
    paper.sync()
    text = paper.text
    assert ">A paragraph the source added at the very end.</p>" in text
    assert "the second point</li>" in text          # the block before it is untouched
    # Written the other way round, the words would have joined the last list item and
    # left the paragraph mark behind them as an empty block.
    assert '<p id="paragraph:empty"></p>' not in text
    paper.settled()


def test_a_section_the_source_moved_moves_in_the_document(paper):
    """The API cannot move anything: a move is a delete where the document has the
    block and a write where the file puts it. What rides along is what makes it a
    move and not a rewrite — the reader's words, the styling, and the block's key."""
    paper.typed("The opening paragraph", " Typed by a reader.")
    paper.moved("paragraph:opening")                     # to the end of the body
    info = paper.sync()
    assert info["conflicts"] == []
    assert any("moved to where the source has it" in line for line in info["applied"]), \
        info["applied"]

    text = paper.text
    assert text.index("The opening paragraph") > text.index("the second point")
    assert "Typed by a reader." in text                  # the reader's words rode along
    assert "<b>bold words</b>" in text                   # and the styling with them
    assert '<a href="https://example.com/">link</a>' in text
    # Written after the last list item, it must not have become a third one — and it
    # must still be the paragraph the file named, not a new one keyed from its words.
    assert '<p id="paragraph:opening">' in text
    assert "<li id=\"paragraph:opening\"" not in text
    paper.settled()


def test_a_table_the_source_added_and_a_row_it_added_are_written(paper):
    """A grid is not text: it goes in a batch of its own, the document is read again,
    and the words are written against the grid it then has. The reader's own edit to
    the table that gained the row has to survive all of that."""
    paper.edit('<table id="table:numbers">',
               '<table id="table:costs"><tr><td><p>Item</p></td><td><p>Cost</p></td></tr>'
               '<tr><td><p>Travel</p></td><td><p>340</p></td></tr></table>\n'
               '<table id="table:numbers">')
    paper.edit("<tr><td><p>South</p></td><td><p>870</p></td></tr>",
               "<tr><td><p>South</p></td><td><p>870</p></td></tr>"
               "<tr><td><p>East</p></td><td><p>510</p></td></tr>")
    paper.typed("1200", " and steady")
    info = paper.sync()
    assert info["conflicts"] == []
    assert any("added by the source" in line for line in info["applied"]), info["applied"]
    assert any("inserts a row" in line for line in info["applied"]), info["applied"]

    text = paper.text
    assert "<td><p>Travel</p></td><td><p>340</p></td>" in text     # the table it built
    assert "<td><p>East</p></td><td><p>510</p></td>" in text       # the row it added
    assert "<td><p>1200 and steady</p></td>" in text               # what the reader typed
    assert '<table id="table:costs">' in text                      # and it kept its id
    # The empty paragraph `insertTable` leaves in front of itself is swallowed again.
    assert "<p></p>" not in text and "The opening paragraph" in text
    paper.settled()


def test_a_grid_both_sides_changed_and_a_cell_of_two_paragraphs(paper):
    """The source adds a column, takes a row away and splits a cell in two; the reader
    adds a row and writes in a header. Rows and columns are matched by their words,
    so all of it stands."""
    paper.edit("<td><p>Region</p></td><td><p>Sales</p></td></tr>",
               "<td><p>Region</p></td><td><p>Sales</p></td><td><p>Target</p></td></tr>")
    paper.edit("<td><p>North</p></td><td><p>1200</p></td></tr>",
               "<td><p>North</p></td><td><p>1200</p><p>rising</p></td><td><p>1300</p></td></tr>")
    paper.edit("<tr><td><p>South</p></td><td><p>870</p></td></tr>", "")

    from beamer2slides.google_auth import credentials, docs_service
    docs = docs_service(credentials())
    _, ir = paper.sync_module.read_document(docs, paper.ident)
    grid = next(b for b in ir["blocks"] if b["kind"] == "table")
    paper.sync_module.send(docs, paper.ident, [{"insertTableRow": {"tableCellLocation": {
        "tableStartLocation": {"index": grid["span"][0]}, "rowIndex": 2, "columnIndex": 0},
        "insertBelow": True}}])
    _, ir = paper.sync_module.read_document(docs, paper.ident)
    grid = next(b for b in ir["blocks"] if b["kind"] == "table")
    paper.sync_module.send(docs, paper.ident, [{"insertText": {
        "location": {"index": grid["rows"][3][0][0]["span"][0]}, "text": "West"}}])
    paper.typed("Sales", " (k)")

    info = paper.sync()
    assert info["conflicts"] == []
    text = paper.text
    assert "<td><p>Sales (k)</p></td><td><p>Target</p></td>" in text     # both headers
    assert "<td><p>1200</p><p>rising</p></td><td><p>1300</p></td>" in text
    assert "South" not in text                                          # the source's delete
    assert "<td><p>West</p></td>" in text                               # the reader's row
    paper.settled()


def test_a_table_the_source_moved_is_built_again_where_the_file_has_it(paper):
    """There is no move: the table is deleted and built again, blank, and its words
    are written on the pass after. To the end of the body and back, where the empty
    paragraph a final table keeps after itself has to go with it."""
    paper.moved("table:numbers")
    info = paper.sync()
    assert any("moved where the source has it" in line for line in info["applied"]), \
        info["applied"]
    text = paper.text
    assert text.index("<td><p>South</p></td>") > text.index("the second point")
    assert '<table id="table:numbers">' in text and "<p></p>" not in text
    paper.settled()

    paper.moved("table:numbers", after="paragraph:opening")
    paper.sync()
    text = paper.text
    assert text.index("<td><p>South</p></td>") < text.index("The closing paragraph")
    assert "<p></p>" not in text and 'id="paragraph:empty"' not in text  # nothing left behind
    paper.settled()


def test_a_word_the_source_bolded_in_a_paragraph_the_reader_rewrote(paper):
    """The marks follow the words: the source's on its words, the reader's on theirs."""
    paper.edit("The closing paragraph.", "The <b>closing</b> paragraph.")
    paper.rewrote("The closing", "paragraph", "passage")
    info = paper.sync()
    assert info["conflicts"] == []
    assert "The <b>closing</b> passage." in paper.text
    paper.settled()


def test_a_list_the_reader_numbered_is_numbered_in_the_file(paper):
    """An imported list cannot say whether it is numbered, so the push gives it bullets
    of the document's own — and from then on a reader's switch in the toolbar (the same
    request the toolbar sends) is seen, where before the file silently won."""
    from beamer2slides.google_auth import credentials, docs_service
    docs = docs_service(credentials())
    _, ir = paper.sync_module.read_document(docs, paper.ident)
    first, second = find(ir, "the first point"), find(ir, "the second point")
    paper.sync_module.send(docs, paper.ident, [{"createParagraphBullets": {
        "range": {"startIndex": first["span"][0], "endIndex": second["span"][1]},
        "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}}])
    info = paper.sync()
    assert info["conflicts"] == []
    text = paper.text
    assert "<ol>" in text and "<ul>" not in text
    assert '<li id="item:first">the first point</li>' in text
    paper.settled()


def test_a_table_at_the_very_end_and_the_paragraphs_after_it(paper):
    """A body ends on a paragraph, so a table written last has an empty one after it
    that no request can delete. It is no block: the file never shows it, the block the
    source appends next is written into it, and deleting the body's last paragraph
    takes the mark in front of it, because its own is the body's."""
    paper.edit("</ul>", '</ul>\n<table id="table:last"><tr><td><p>x</p></td>'
                        '<td><p>y</p></td></tr></table>')
    paper.sync()
    assert "<td><p>x</p></td><td><p>y</p></td>" in paper.text
    assert "<p></p>" not in paper.text and 'id="paragraph:empty"' not in paper.text
    paper.settled()

    paper.edit("</table>\n</body>", '</table>\n<p id="paragraph:after">After the last table.</p>'
                                    '\n<p id="paragraph:end">The end.</p>\n</body>')
    paper.sync()
    text = paper.text
    assert text.index("After the last table.") < text.index("The end.")
    assert "<p></p>" not in text and 'id="paragraph:empty"' not in text
    paper.settled()

    paper.edit('\n<p id="paragraph:end">The end.</p>', "")
    paper.sync()
    assert "The end." not in paper.text and "After the last table." in paper.text
    paper.settled()


def png(path: Path, colour: tuple, size=(60, 40)) -> str:
    """A picture file beside the canonical file; its `src` relative to it."""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "PNG")
    return path.relative_to(OUT).as_posix()


def objects(paper) -> dict:
    """The pictures in the document: object id -> its first pixel, fetched."""
    import io
    import urllib.request
    from PIL import Image
    from beamer2slides import doc_merge
    from beamer2slides.google_auth import credentials, docs_service
    _, ir = paper.sync_module.read_document(docs_service(credentials()), paper.ident)
    out = {}
    for run in doc_merge._image_runs(ir["blocks"]):
        with urllib.request.urlopen(run["uri"]) as reply:
            out[run["value"]] = Image.open(io.BytesIO(reply.read())).convert("RGB").getpixel((5, 5))
    return out


def test_pictures_go_in_follow_the_source_and_come_back_from_the_reader(paper, request):
    """`insertInlineImage` takes a URL, never bytes: the sync stages the file as a
    document of its own, inserts from the URL Docs gives it, and deletes the staging
    file. A picture the reader inserts comes back as a file beside the canonical one."""
    import re
    src = png(OUT / f"{request.node.name}-figures" / "square.png", (200, 30, 30))
    paper.edit('<p id="paragraph:closing">',
               f'<p id="paragraph:figure"><img src="{src}" alt="a red square" width="60" '
               f'height="40"></p>\n<p id="paragraph:closing">')
    info = paper.sync()
    assert info["conflicts"] == [] and not info["notes"], info["notes"]
    img = re.search(r'<img src="([^"]+)" alt="a red square" width="60" height="40" '
                    r'data-object="([^"]+)">', paper.text)
    assert img and img.group(1) == src, paper.text
    assert list(objects(paper).values()) == [(200, 30, 30)]
    paper.settled()

    # The source regenerates the figure under the same name: the picture is replaced.
    png(OUT / src, (30, 30, 200))
    paper.sync()
    assert list(objects(paper).values()) == [(30, 30, 200)]
    assert f'src="{src}"' in paper.text and img.group(2) not in paper.text
    paper.settled()

    # A reader inserts a picture of their own; it lands beside the file.
    from beamer2slides.google_auth import credentials, docs_service
    docs = docs_service(credentials())
    _, ir = paper.sync_module.read_document(docs, paper.ident)
    uri = next(r["uri"] for b in ir["blocks"] for r in b.get("runs", []) if r.get("uri"))
    closing = find(ir, "The closing paragraph")
    paper.sync_module.send(docs, paper.ident, [{"insertInlineImage": {
        "location": {"index": closing["span"][1] - 1}, "uri": uri}}])
    paper.sync()
    mine = re.search(r'The closing paragraph\.<img src="([^"]+)"', paper.text)
    assert mine and (OUT / mine.group(1)).is_file(), paper.text
    assert mine.group(1).startswith(f"{paper.path.stem}.media/")
    paper.settled()

    # And the source moves the figure: a move is a delete and a write, and the
    # picture rides along from the document's own copy.
    paper.moved("paragraph:figure")
    paper.sync()
    text = paper.text
    assert text.index(f'src="{src}"') > text.index("the second point")
    assert len(objects(paper)) == 2
    paper.settled()


def test_a_file_with_a_picture_is_pushed_with_it(google, request):
    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, drive_service
    src = png(OUT / f"{request.node.name}-figures" / "plot.png", (20, 160, 20), (80, 50))
    path = OUT / f"{request.node.name}.html"
    path.write_text(f'<html><body><p>A figure:</p><p><img src="{src}" alt="green"></p>'
                    f'<p>after it</p></body></html>', encoding="utf-8")
    doc_sync.base_path(path).unlink(missing_ok=True)
    info = doc_sync.push(path, name=f"b2s docs test: {request.node.name}")
    try:
        text = path.read_text(encoding="utf-8")
        assert f'<img src="{src}" alt="green" width="80" height="50" data-object=' in text, text
        paper = Paper(path, info["document"])
        paper.settled()
    finally:
        drive_service(credentials()).files().delete(fileId=info["document"]).execute()


OMML = ('xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"')
EQUATION_DOCX = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {OMML}><w:body>
<w:p><w:r><w:t xml:space="preserve">Inline </w:t></w:r>
<m:oMath><m:r><m:t>E=m</m:t></m:r><m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e>\
<m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:oMath>
<w:r><w:t xml:space="preserve"> costs $5 and </w:t></w:r>
<m:oMath><m:f><m:num><m:r><m:t>a</m:t></m:r></m:num><m:den><m:r><m:t>b</m:t></m:r></m:den></m:f></m:oMath>
<w:r><w:t xml:space="preserve"> ends it.</w:t></w:r></w:p>
<w:p><w:r><w:t>The closing paragraph.</w:t></w:r></w:p>
</w:body></w:document>"""


def docx(body: str) -> bytes:
    """A .docx of one document part: Drive's importer turns its OMML into equations,
    which is the only way to make one — the Docs API has no request for it."""
    import io
    import zipfile
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
             'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.'
             'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", body)
    return buf.getvalue()


def test_an_equation_reaches_the_file_as_latex_and_survives_a_rewrite(google, request):
    """The LaTeX comes from the Markdown export; a source rewrite of the words around
    the equations keeps them, and the file keeps saying what they are."""
    import io
    from googleapiclient.http import MediaIoBaseUpload
    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, docs_service, drive_service
    drive = drive_service(credentials())
    ident = drive.files().create(
        body={"name": f"b2s docs test: {request.node.name}", "mimeType": doc_sync.DOC_MIME},
        media_body=MediaIoBaseUpload(io.BytesIO(docx(EQUATION_DOCX)), mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        fields="id").execute()["id"]
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{request.node.name}.html"
        path.write_text("<html><body></body></html>", encoding="utf-8")
        doc_sync.base_path(path).unlink(missing_ok=True)
        doc_sync.sync(path, document=ident, assume_base="file")
        paper = Paper(path, ident)
        text = paper.text
        assert 'data-chip="equation">E=m{c}^{2}</span>' in text, text
        assert 'data-chip="equation">\\frac{a}{b}</span>' in text, text
        paper.settled()
        # The source rewrites the words between the equations; the reader, the last line.
        paper.edit(" costs $5 and ", " costs $6 and ")
        paper.rewrote("The closing paragraph", "closing", "last")
        info = paper.sync()
        assert info["conflicts"] == [], info["conflicts"]
        doc, _ = doc_sync.read_document(docs_service(credentials()), ident)
        elements = [e for tab in doc["tabs"] for c in tab["documentTab"]["body"]["content"]
                    for e in c.get("paragraph", {}).get("elements", [])]
        assert sum("equation" in e for e in elements) == 2
        assert any("costs $6 and" in e.get("textRun", {}).get("content", "") for e in elements)
        text = paper.text
        assert 'data-chip="equation">E=m{c}^{2}</span>' in text, text
        assert "The last paragraph." in text
        paper.settled()
    finally:
        drive.files().delete(fileId=ident).execute()


def test_a_block_the_source_added_with_a_date_and_a_person_gets_them(paper):
    """Measured: `insertDate` and `insertPerson` make the chips, so a new block that
    carries them is written with them rather than reported."""
    paper.edit('<p id="paragraph:closing">',
               '<p id="paragraph:due">due <span class="b2s-chip" data-chip="date" '
               'data-value="2026-10-01T12:00:00Z"></span>, ask <span class="b2s-chip" '
               'data-chip="person" data-value="someone@example.com"></span></p>\n'
               '<p id="paragraph:closing">')
    info = paper.sync()
    assert not info["notes"], info["notes"]
    text = paper.text
    assert 'data-chip="date" data-value="2026-10-01T12:00:00Z"' in text, text
    assert 'data-chip="person" data-value="someone@example.com"' in text
    paper.settled()


def test_the_same_words_on_both_sides_conflict_and_the_document_wins(paper):
    paper.edit("The closing paragraph.", "The final paragraph.")
    paper.rewrote("The closing paragraph", "closing", "last")
    info = paper.sync()
    assert [c["key"] for c in info["conflicts"]] == ["paragraph:closing"]
    assert "The last paragraph." in paper.text
    assert "final" not in paper.text
    paper.settled()


def test_a_reader_typing_mid_sync_makes_it_read_again(paper):
    """The re-plan: the write is refused on the revision, and the second plan keeps
    the words that arrived in between."""
    script = OUT / "type-mid-sync.py"
    script.write_text(TYPE_NOW, encoding="utf-8")
    hook = subprocess.list2cmdline([sys.executable, str(script), paper.ident,
                                    "The opening paragraph", " Typed mid-sync."])
    paper.edit("the first point", "the first point, edited in the source")
    os.environ["B2S_DOCS_BEFORE_WRITE"] = hook
    try:
        info = paper.sync()
    finally:
        os.environ.pop("B2S_DOCS_BEFORE_WRITE", None)
    assert info.get("replanned") == 1, "the document moved under the plan and was read again"
    assert "Typed mid-sync." in paper.text          # the reader's words survived
    assert "the first point, edited in the source" in paper.text
    paper.settled()


def test_an_open_comment_in_the_document_is_named_in_the_report(paper):
    """A comment lives in Drive, not in the document's content, so nothing the merge
    reads can see one. The sync reads them separately and says they are there."""
    from beamer2slides.google_auth import credentials, drive_service
    drive_service(credentials()).comments().create(
        fileId=paper.ident, fields="id",
        body={"content": "is this number still right?"}).execute()
    info = paper.sync()
    assert any("is this number still right?" in line for line in info["comments"]), \
        info["comments"]
    assert "## Open comments in the document" in \
        Path(info["report"]).read_text(encoding="utf-8")
    paper.settled()


def test_the_cli_reaches_the_same_plan(paper):
    done = subprocess.run([sys.executable, "-m", "beamer2slides", "docs", "sync",
                           str(paper.path), "--dry-run"],
                          env=ENV, cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "0 request(s)" in done.stdout


def tabs(paper) -> list[tuple[str, str]]:
    """The document's tabs past the first, as (title, words)."""
    from beamer2slides import doc_ir
    from beamer2slides.google_auth import credentials, docs_service
    _, ir = paper.sync_module.read_document(docs_service(credentials()), paper.ident)
    return [(part["title"], " / ".join(
        "".join(r["text"] for r in b.get("runs", [])) or b["kind"] for b in part["blocks"]))
        for part in doc_ir.parts(ir)[1:]]


APPENDIX = """<section title="Appendix">
<p>An appendix line.</p>
<ul>
 <li>point a</li>
 <li>point b</li>
</ul>
<table><tr><td><p>x</p></td><td><p>y</p></td></tr></table>
<p>after the table</p>
</section>
<section title="Scratch">
<p>scratch words</p>
</section>
</body>"""


def test_tabs_the_source_adds_renames_edits_and_deletes(paper):
    """Every tab is synced like the first: its own plan, its requests stamped with its
    `tabId`, created, renamed or deleted by the three-way rule one level up."""
    paper.edit("</body>", APPENDIX)
    info = paper.sync()
    assert not info["notes"], info["notes"]
    assert tabs(paper) == [("Appendix", "An appendix line. / point a / point b / table / "
                                        "after the table"),
                           ("Scratch", "scratch words")]
    text = paper.text
    assert text.count("<section data-tab=") == 2 and "<ul>" in text.split("Appendix")[1]
    paper.settled()

    # The source rewrites a line, renames the tab and drops the other one; a reader
    # types into the appendix meanwhile.
    from beamer2slides.google_auth import credentials, docs_service
    from beamer2slides import doc_ir
    docs = docs_service(credentials())
    _, ir = paper.sync_module.read_document(docs, paper.ident)
    appendix = next(p for p in doc_ir.parts(ir) if p.get("title") == "Appendix")
    point = next(b for b in appendix["blocks"] if b.get("runs") and
                 b["runs"][0]["text"] == "point a")
    paper.sync_module.send(docs, paper.ident, [{"insertText": {"location": {
        "index": point["span"][1] - 1, "tabId": appendix["tab"]}, "text": " (reader)"}}])
    paper.edit("An appendix line.", "An appendix line, revised.")
    paper.edit('title="Appendix"', 'title="Appendix A"')
    text = paper.text
    start = text.index("<section", text.index('title="Scratch"') - 40)
    paper.path.write_text(text[:start] + text[text.index("</section>", start) + 11:],
                          encoding="utf-8")
    info = paper.sync()
    assert info["conflicts"] == [], info["conflicts"]
    assert tabs(paper) == [("Appendix A", "An appendix line, revised. / point a (reader) / "
                                          "point b / table / after the table")]
    assert "Scratch" not in paper.text and "point a (reader)" in paper.text
    paper.settled()


def test_a_file_with_tabs_is_pushed_with_them(google, request):
    from beamer2slides import doc_sync
    from beamer2slides.google_auth import credentials, drive_service
    path = OUT / f"{request.node.name}.html"
    path.write_text("<html><body><p>front</p>" + APPENDIX.replace("</body>", "")
                    + "</body></html>", encoding="utf-8")
    doc_sync.base_path(path).unlink(missing_ok=True)
    info = doc_sync.push(path, name=f"b2s docs test: {request.node.name}")
    try:
        paper = Paper(path, info["document"])
        assert info["tabs"] == 3 and info["anchored"] == info["blocks"]
        assert tabs(paper) == [("Appendix", "An appendix line. / point a / point b / table / "
                                            "after the table"),
                               ("Scratch", "scratch words")]
        paper.settled()
    finally:
        drive_service(credentials()).files().delete(fileId=info["document"]).execute()
