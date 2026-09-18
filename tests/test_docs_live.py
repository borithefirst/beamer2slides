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
sys.path.insert(0, str(ROOT / "tests"))

from test_slides_alignment import MAIN, google_unavailable  # noqa: E402

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


def test_the_cli_reaches_the_same_plan(paper):
    done = subprocess.run([sys.executable, "-m", "beamer2slides", "docs", "sync",
                           str(paper.path), "--dry-run"],
                          env=ENV, cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "0 request(s)" in done.stdout
