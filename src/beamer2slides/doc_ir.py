"""The Google Docs intermediate representation, and the canonical HTML dialect.

The Slides pipeline's IR is `deck.json`; this is its Docs counterpart, and it has
two writers and two readers, because HTML — not the IR — is the artefact the user
keeps in git (docs/google-docs.md):

    canonical HTML  --from_html-->  IR  --to_html-->  canonical HTML
    live document   --from_document->  IR

`from_html(to_html(ir)) == ir` is the law the offline tests hold us to. The live
document is the lossy side: Google's importer builds only what HTML can say
(measured in docs/google-docs.md), and reading a document back finds objects no
HTML can create — chips, equations, dropdowns. Those come back as **frozen** runs:
they carry enough to be shown in the canonical file and diffed, and the merge
refuses to rewrite them, so a chip a human inserted survives every later push.

Indices are UTF-16 code units, the Docs API's own unit, and every block read from
a live document keeps the span it came from so edits can be planned against it.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# textStyle keys we carry, and the tag each becomes in the canonical file.
MARKS = {"bold": "b", "italic": "i", "underline": "u", "strike": "s", "code": "code"}
# A named style is a block kind; everything else is a paragraph.
HEADINGS = {f"HEADING_{n}": n for n in range(1, 7)}
NAMED_STYLE = {n: f"HEADING_{n}" for n in range(1, 7)} | {0: "NORMAL_TEXT"}
ALIGNMENTS = {"START": "left", "CENTER": "center", "END": "right", "JUSTIFIED": "justify"}
TO_ALIGNMENT = {v: k for k, v in ALIGNMENTS.items()}
MONO = {"Courier New", "Roboto Mono", "Consolas", "Source Code Pro"}
# What Docs paints a link with when nobody asked: the import's blue, and the editor's.
LINK_COLORS = {"#0000ee", "#1155cc"}
# Docs writes this private-use character where an object sits that the API will not
# describe: a placeholder chip, or a watermark in the header (docs/google-docs.md).
OBJECT_SENTINEL = ""

# ParagraphElement keys that are not a textRun, and the chip kind each becomes.
CHIPS = {"dateElement": "date", "person": "person", "richLink": "link",
         "footnoteReference": "footnote", "equation": "equation",
         "inlineObjectElement": "image", "horizontalRule": "rule"}
SLUG = re.compile(r"[^a-z0-9]+")
# A block's identity in the live document. The deck writes `b2s:<slide>/<element>`
# into an element's alt-text title; a document has no such field, so the same string
# becomes the name of a named range over the paragraph. Docs moves those ranges with
# the text and keeps them through the editor and undo (docs/google-docs.md).
KEY_PREFIX = "b2s:"
# The `<meta>` that tells a canonical file which document it belongs to.
DOCUMENT_META = "b2s-document"


# ---------------------------------------------------------------- runs

def _style_of(text_style: dict) -> dict:
    """The marks we keep, from a Docs textStyle."""
    style = {}
    for key, api in (("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
                     ("strike", "strikethrough")):
        if text_style.get(api):
            style[key] = True
    family = text_style.get("weightedFontFamily", {}).get("fontFamily")
    if family in MONO:
        style["code"] = True
    rgb = text_style.get("foregroundColor", {}).get("color", {}).get("rgbColor")
    if rgb:
        style["color"] = _hex(rgb)
    back = text_style.get("backgroundColor", {}).get("color", {}).get("rgbColor")
    if back:
        style["highlight"] = _hex(back)
    url = text_style.get("link", {}).get("url")
    if url:
        style["link"] = url
        # Docs paints a link blue and underlines it by itself. Keeping that here
        # would put an <u> and a colour into the canonical file for every link, and
        # write them back on the next push, so the styling a link gets for free is
        # dropped and only the deliberate kind survives.
        if style.get("color") in LINK_COLORS:
            style.pop("color")
            style.pop("underline", None)
    return style


def _hex(rgb: dict) -> str:
    return "#%02x%02x%02x" % tuple(round(255 * rgb.get(c, 0.0)) for c in ("red", "green", "blue"))


def _chip_run(kind: str, value: dict) -> dict:
    """A Docs object no HTML can create: keep what identifies it, and freeze it."""
    run = {"chip": kind, "frozen": True, "text": ""}
    if kind == "date":
        props = value.get("dateElementProperties", {})
        run["text"] = props.get("displayText", "")
        run["value"] = props.get("timestamp", "")
        run["format"] = props.get("dateFormat", "")
        run["locale"] = props.get("locale", "")
    elif kind == "person":
        props = value.get("personProperties", {})
        run["text"] = props.get("name", "")
        run["value"] = props.get("email", "")
    elif kind == "link":
        props = value.get("richLinkProperties", {})
        run["text"] = props.get("title", "")
        run["value"] = props.get("uri", "")
        if props.get("mimeType"):
            run["mime"] = props["mimeType"]
    elif kind == "footnote":
        run["text"] = value.get("footnoteNumber", "")
        run["value"] = value.get("footnoteId", "")
    elif kind == "image":
        run["value"] = value.get("inlineObjectId", "")
    return run


def runs_text(runs: list[dict]) -> str:
    """The words of a block, for fingerprints and diffs. A frozen run is one unit."""
    return "".join(r["text"] for r in runs)


def merge_runs(runs: list[dict]) -> list[dict]:
    """Join neighbouring runs that carry the same style; drop empty ones."""
    out: list[dict] = []
    for run in runs:
        if not run.get("frozen") and not run["text"] and not run.get("width"):
            continue
        last = out[-1] if out else None
        if (last and not last.get("frozen") and not run.get("frozen")
                and _marks(last) == _marks(run)):
            last["text"] += run["text"]
            if "width" in last or "width" in run:
                last["width"] = last.get("width", 0) + run.get("width", 0)
        else:
            out.append(dict(run))
    return out


def _marks(run: dict) -> dict:
    """What decides whether two runs can be one: style, not text or measurements."""
    return {k: v for k, v in run.items() if k not in ("text", "width")}


def utf16_len(text: str) -> int:
    """The Docs API counts indices in UTF-16 code units, not characters."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------- live document -> IR

def from_document(doc: dict, tab_id: str | None = None) -> dict:
    """`documents.get` JSON to the IR.

    A document read with `includeTabsContent=True` has no `body` at all — it has
    `tabs` — and one read without the flag silently holds only the first tab
    (docs/google-docs.md). Both shapes are accepted; `tab_id` picks one tab.
    """
    body, tab = _body_of(doc, tab_id)
    ir = {"title": doc.get("title", ""), "tab": tab, "blocks": []}
    lists = (doc.get("lists") if "lists" in doc else {}) or _tab_lists(doc, tab)
    for element in body:
        block = _block_of(element, lists)
        if block:
            ir["blocks"].append(block)
    return ir


def _body_of(doc: dict, tab_id: str | None) -> tuple[list, str | None]:
    if "body" in doc:
        return doc["body"].get("content", []), None
    for tab in _flatten_tabs(doc.get("tabs", [])):
        props = tab.get("tabProperties", {})
        if tab_id in (None, props.get("tabId")):
            return tab.get("documentTab", {}).get("body", {}).get("content", []), props.get("tabId")
    return [], tab_id


def _flatten_tabs(tabs: list) -> list:
    out = []
    for tab in tabs:
        out.append(tab)
        out += _flatten_tabs(tab.get("childTabs", []))
    return out


def _tab_lists(doc: dict, tab_id: str | None) -> dict:
    for tab in _flatten_tabs(doc.get("tabs", [])):
        if tab_id in (None, tab.get("tabProperties", {}).get("tabId")):
            return tab.get("documentTab", {}).get("lists", {})
    return {}


def _block_of(element: dict, lists: dict) -> dict | None:
    if "table" in element:
        return _table_block(element, lists)
    if "tableOfContents" in element:
        # Generated content: readable, never writable (no insertTableOfContents in v1).
        return {"kind": "toc", "frozen": True, "runs": [],
                "span": [element.get("startIndex", 0), element.get("endIndex", 0)]}
    para = element.get("paragraph")
    if para is None:
        return None
    runs = []
    for el in para.get("elements", []):
        # `width` is how many index units the run holds in the live document: a chip
        # is one unit however long its words look, and an equation was thirteen.
        width = el.get("endIndex", 0) - el.get("startIndex", 0)
        if "textRun" in el:
            content = el["textRun"].get("content", "")
            for piece, frozen in _split_sentinel(content):
                if frozen:
                    runs.append({"chip": "object", "frozen": True, "text": piece, "width": 1})
                elif piece:
                    runs.append({"text": piece, "width": utf16_len(piece)}
                                | _style_of(el["textRun"].get("textStyle", {})))
            continue
        for key, kind in CHIPS.items():
            if key in el:
                runs.append(_chip_run(kind, el[key]) | {"width": width})
                break
        else:
            # A dropdown chip: an element with a span and no content key at all.
            runs.append({"chip": "unknown", "frozen": True, "text": "", "width": width})
    # The trailing newline is the paragraph, not text in it.
    if runs and not runs[-1].get("frozen") and runs[-1]["text"].endswith("\n"):
        runs[-1]["text"] = runs[-1]["text"][:-1]
        runs[-1]["width"] = runs[-1].get("width", 1) - 1
    block = {"runs": merge_runs(runs),
             "span": [element.get("startIndex", 0), element.get("endIndex", 0)]}
    style = para.get("paragraphStyle", {})
    bullet = para.get("bullet")
    if bullet:
        block["kind"] = "item"
        block["level"] = bullet.get("nestingLevel", 0)
        block["ordered"] = _ordered(lists, bullet.get("listId"), block["level"])
    elif style.get("namedStyleType") in HEADINGS:
        block["kind"] = "heading"
        block["level"] = HEADINGS[style["namedStyleType"]]
    else:
        block["kind"] = "paragraph"
    align = ALIGNMENTS.get(style.get("alignment", ""))
    if align and align != "left":
        block["align"] = align
    return block


def _split_sentinel(content: str) -> list[tuple[str, bool]]:
    """Ordinary text and the U+E907 object sentinels inside it, in order."""
    return [(piece, piece == OBJECT_SENTINEL)
            for piece in re.split(f"({OBJECT_SENTINEL})", content) if piece]


def _ordered(lists: dict, list_id: str | None, level: int) -> bool | None:
    """Ordered levels carry a real glyphType, unordered carry a glyphSymbol.

    A list Drive's HTML import made says neither: every level comes back
    `GLYPH_TYPE_UNSPECIFIED` with no glyphFormat and no glyphSymbol, for `<ol>` and
    `<ul>` alike, though the editor renders the two differently (measured on the
    spike document, docs/google-docs.md). So the answer is **None** — not False —
    and the canonical file stays the only place that knows.
    """
    levels = (lists.get(list_id, {}).get("listProperties", {}).get("nestingLevels", []))
    if level >= len(levels):
        return None
    glyphs = levels[level]
    if "glyphSymbol" in glyphs:
        return False
    if glyphs.get("glyphType", "GLYPH_TYPE_UNSPECIFIED") != "GLYPH_TYPE_UNSPECIFIED":
        return True
    return None


def _table_block(element: dict, lists: dict) -> dict:
    rows = []
    for row in element["table"].get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            blocks = [b for b in (_block_of(e, lists) for e in cell.get("content", [])) if b]
            cells.append(blocks)
        rows.append(cells)
    return {"kind": "table", "rows": rows,
            "span": [element.get("startIndex", 0), element.get("endIndex", 0)]}


# ---------------------------------------------------------------- IR -> canonical HTML

def to_html(ir: dict) -> str:
    """The canonical file: one block per line, so a git diff reads like the document.

    Everything here is measured to survive Google's importer except the frozen
    runs, which cannot be created by any import and are written so that a human
    can read them and `from_html` can put them back.
    """
    lines = ["<!DOCTYPE html>", "<html>", "<head>", '<meta charset="utf-8">']
    if ir.get("document"):
        # Which document this file is. The file is the project: told where it lives, it
        # can be synced from any checkout without a folder of state beside it.
        lines.append(f'<meta name="{DOCUMENT_META}" content="{escape(ir["document"], quote=True)}">')
    if ir.get("title"):
        lines.append(f"<title>{escape(ir['title'])}</title>")
    lines += ["</head>", "<body>"]
    lines += _blocks_html(ir["blocks"], depth=0)
    lines += ["</body>", "</html>", ""]
    return "\n".join(lines)


def _blocks_html(blocks: list[dict], depth: int) -> list[str]:
    lines, index = [], 0
    pad = " " * depth
    while index < len(blocks):
        block = blocks[index]
        if block["kind"] == "item":
            run = _item_run(blocks, index)
            lines += _list_html(blocks[index:index + run], depth)
            index += run
            continue
        lines.append(pad + _block_html(block))
        index += 1
    return lines


def _item_run(blocks: list[dict], start: int) -> int:
    """How many list items follow, from `start`, that belong to one list."""
    end = start
    ordered = blocks[start].get("ordered", False)
    while (end < len(blocks) and blocks[end]["kind"] == "item"
           and blocks[end].get("ordered", False) == ordered):
        end += 1
    return end - start


def _list_html(items: list[dict], depth: int) -> list[str]:
    tag = "ol" if items[0].get("ordered") else "ul"
    pad = " " * depth
    lines, index = [pad + f"<{tag}>"], 0
    while index < len(items):
        level = items[index].get("level", 0)
        deeper = index + 1
        while deeper < len(items) and items[deeper].get("level", 0) > level:
            deeper += 1
        lines.append(pad + f" <li{_key_attr(items[index])}>"
                           f"{_runs_html(items[index]['runs'])}</li>")
        if deeper > index + 1:
            lines += _list_html(items[index + 1:deeper], depth + 1)
        index = deeper
    lines.append(pad + f"</{tag}>")
    return lines


def _key_attr(block: dict) -> str:
    """The block's identity, carried in the file the way a deck carries alt-text titles.

    Google's importer drops it — only `<a id>` in a heading ever became anything
    (docs/google-docs.md) — and that is fine: the live document's copy of the same
    identity is a named range. This one exists so that a paragraph rewritten from
    end to end can still be recognised as the same paragraph.
    """
    return f' id="{escape(block["key"], quote=True)}"' if block.get("key") else ""


def _block_html(block: dict) -> str:
    kind = block["kind"]
    if kind == "table":
        cells = []
        for row in block["rows"]:
            inner = "".join(f"<td>{''.join(_blocks_html(c, 0))}</td>" for c in row)
            cells.append(f"<tr>{inner}</tr>")
        return f"<table{_key_attr(block)}>{''.join(cells)}</table>"
    if kind == "toc":
        return f'<p class="b2s-toc"{_key_attr(block)}></p>'
    tag = f"h{block['level']}" if kind == "heading" else "p"
    attrs = f' style="text-align:{block["align"]}"' if block.get("align") else ""
    return f"<{tag}{_key_attr(block)}{attrs}>{_runs_html(block['runs'])}</{tag}>"


def _runs_html(runs: list[dict]) -> str:
    return "".join(_run_html(r) for r in runs)


def _run_html(run: dict) -> str:
    if run.get("frozen"):
        attrs = "".join(f' data-{k}="{escape(str(run[k]), quote=True)}"'
                        for k in ("value", "format", "locale", "mime") if run.get(k))
        return (f'<span class="b2s-chip" data-chip="{run["chip"]}"{attrs}>'
                f'{escape(run["text"])}</span>')
    out = escape(run["text"])
    styles = []
    if run.get("color"):
        styles.append(f"color:{run['color']}")
    if run.get("highlight"):
        styles.append(f"background-color:{run['highlight']}")
    if styles:
        out = f'<span style="{";".join(styles)}">{out}</span>'
    for key, tag in MARKS.items():
        if run.get(key):
            out = f"<{tag}>{out}</{tag}>"
    if run.get("link"):
        out = f'<a href="{escape(run["link"], quote=True)}">{out}</a>'
    return out


# ---------------------------------------------------------------- canonical HTML -> IR

class _Reader(HTMLParser):
    """The dialect's parser. Anything outside the dialect is ignored, not guessed at."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ir: dict = {"title": "", "blocks": []}
        self.block: dict | None = None
        self.marks: list[dict] = []          # style frames pushed by b/i/u/s/code/span/a
        self.lists: list[bool] = []          # one per open ul/ol: ordered?
        self.chip: dict | None = None
        self.in_title = False
        self.cell: list[dict] | None = None  # blocks of the table cell being read
        self.row: list | None = None
        self.table: dict | None = None

    # -- helpers

    def _style(self) -> dict:
        style: dict = {}
        for frame in self.marks:
            style |= frame
        return style

    def _emit(self, block: dict) -> None:
        if self.cell is not None:
            self.cell.append(block)
        else:
            self.ir["blocks"].append(block)

    def _open(self, block: dict) -> None:
        self.block = block

    def _close(self) -> None:
        if self.block is not None:
            self.block["runs"] = merge_runs(self.block["runs"])
            self._emit(self.block)
            self.block = None

    # -- parser

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        if tag == "meta":
            if attr.get("name") == DOCUMENT_META and attr.get("content"):
                self.ir["document"] = attr["content"]
        elif tag == "title":
            self.in_title = True
        elif tag in ("ul", "ol"):
            self.lists.append(tag == "ol")
        elif tag == "li":
            self._open({"kind": "item", "level": max(0, len(self.lists) - 1),
                        "ordered": bool(self.lists and self.lists[-1]), "runs": []}
                       | _key_of(attr))
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            if attr.get("class") == "b2s-toc":
                self._emit({"kind": "toc", "frozen": True, "runs": []} | _key_of(attr))
                return
            block = ({"kind": "heading", "level": int(tag[1]), "runs": []} if tag != "p"
                     else {"kind": "paragraph", "runs": []})
            align = _align_of(attr.get("style", ""))
            if align:
                block["align"] = align
            self._open(block | _key_of(attr))
        elif tag == "table":
            self.table = {"kind": "table", "rows": []} | _key_of(attr)
        elif tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = []
        elif tag in ("b", "strong"):
            self.marks.append({"bold": True})
        elif tag in ("i", "em"):
            self.marks.append({"italic": True})
        elif tag == "u":
            self.marks.append({"underline": True})
        elif tag in ("s", "strike", "del"):
            self.marks.append({"strike": True})
        elif tag == "code":
            self.marks.append({"code": True})
        elif tag == "a":
            self.marks.append({"link": attr.get("href", "")})
        elif tag == "span":
            if attr.get("class") == "b2s-chip":
                self.chip = {"chip": attr.get("data-chip", "object"), "frozen": True, "text": ""}
                for key in ("value", "format", "locale", "mime"):
                    if attr.get(f"data-{key}"):
                        self.chip[key] = attr[f"data-{key}"]
                self.marks.append({})
            else:
                self.marks.append(_span_style(attr.get("style", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
        elif tag in ("li", "p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._close()
        elif tag == "td":
            if self.row is not None:
                self.row.append(self.cell or [])
            self.cell = None
        elif tag == "tr":
            if self.table is not None and self.row is not None:
                self.table["rows"].append(self.row)
            self.row = None
        elif tag == "table":
            if self.table is not None:
                self._emit(self.table)
            self.table = None
        elif tag in ("b", "strong", "i", "em", "u", "s", "strike", "del", "code", "a", "span"):
            if tag == "span" and self.chip is not None:
                chip, self.chip = self.chip, None
                if self.block is not None:
                    self.block["runs"].append(chip)
            if self.marks:
                self.marks.pop()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.ir["title"] += data.strip()
            return
        if self.chip is not None:
            self.chip["text"] += data
            return
        if self.block is None:
            return
        # Newlines in the file are layout, not content: the dialect puts one block per line.
        text = data.replace("\n", " ")
        if not text.strip() and not self.block["runs"]:
            return
        self.block["runs"].append({"text": text} | self._style())


def _key_of(attr: dict) -> dict:
    return {"key": attr["id"]} if attr.get("id") else {}


def _span_style(style: str) -> dict:
    out = {}
    for piece in style.split(";"):
        key, _, value = piece.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "color":
            out["color"] = value
        elif key == "background-color":
            out["highlight"] = value
    return out


def _align_of(style: str) -> str:
    for piece in style.split(";"):
        key, _, value = piece.partition(":")
        if key.strip().lower() == "text-align" and value.strip() in TO_ALIGNMENT:
            return value.strip()
    return ""


def from_html(html: str) -> dict:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    reader._close()
    return reader.ir


# ---------------------------------------------------------------- keys

def slug(text: str, limit: int = 40) -> str:
    return SLUG.sub("-", text.lower()).strip("-")[:limit] or "empty"


def key_blocks(ir: dict) -> dict:
    """Give every *unkeyed* block a key: kind, its first words, an occurrence count.

    The same rule as `identity.slide_key` — recognisable in a diff, and stable while
    the words are. A block that already has one keeps it, because that key came from
    the canonical file or from a named range, and both outrank a guess from the text.
    """
    # Keys already in hand reserve their occurrence number, so a new block never
    # takes a name a keyed one is using.
    seen: dict[str, int] = {}
    for block in ir["blocks"]:
        if block.get("key"):
            stem, _, count = block["key"].partition("#")
            seen[stem] = max(seen.get(stem, 0), int(count or 1))
    for block in ir["blocks"]:
        if block.get("key"):
            continue
        stem = f"{block['kind']}:{slug(_first_words(block))}"
        seen[stem] = seen.get(stem, 0) + 1
        block["key"] = stem if seen[stem] == 1 else f"{stem}#{seen[stem]}"
    return ir


def named_ranges_of(doc: dict, tab_id: str | None = None) -> dict:
    """The document's named ranges, by name, from either read shape."""
    if "body" in doc:
        return doc.get("namedRanges", {}) or {}
    for tab in _flatten_tabs(doc.get("tabs", [])):
        if tab_id in (None, tab.get("tabProperties", {}).get("tabId")):
            return tab.get("documentTab", {}).get("namedRanges", {}) or {}
    return {}


def apply_keys(ir: dict, named_ranges: dict) -> dict:
    """Give every block the key of the `b2s:` named range that starts inside it.

    This is the read half of identity: the file carries the key as an `id`, the
    document carries it as a range, and the two are compared by the merge. A range
    whose paragraph was split shows up twice; the first block wins, which is where
    the range begins and so where the text the key was given to still is.
    """
    starts = []
    for name, entry in named_ranges.items():
        if not name.startswith(KEY_PREFIX):
            continue
        for ranged in entry.get("namedRanges", []):
            for span in ranged.get("ranges", []):
                starts.append((span.get("startIndex", 0), name[len(KEY_PREFIX):],
                               ranged.get("namedRangeId", "")))
    starts.sort()
    taken: set[str] = set()
    for block in ir["blocks"]:
        low, high = block.get("span", [0, 0])
        for start, key, range_id in starts:
            if low <= start < high and key not in taken:
                block["key"] = key
                block["rangeId"] = range_id
                taken.add(key)
                break
    return ir


def anchor_span(block: dict) -> list | None:
    """The text range a block's named range is planted over.

    A table's own span covers its rows and cells, which is not a run of text the API
    will name, so the range goes into its first cell instead: the table is then the
    block that *contains* the range, which is what `apply_keys` looks for.
    """
    if block["kind"] == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    if inner.get("span"):
                        return inner["span"]
        return None
    return block.get("span")


def name_requests(ir: dict) -> list[dict]:
    """`createNamedRange` for every keyed block the document does not name yet.

    The range stops short of the paragraph mark where it can, so that deleting the
    newline between two paragraphs never silently stretches one block's identity
    over the other's words.
    """
    out = []
    for block in ir["blocks"]:
        span = anchor_span(block) if block.get("key") and not block.get("rangeId") else None
        if not span:
            continue
        low, high = span
        out.append({"createNamedRange": {
            "name": KEY_PREFIX + block["key"],
            "range": {"startIndex": low, "endIndex": max(high - 1, low + 1)}}})
    return out


def _first_words(block: dict) -> str:
    if block["kind"] == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    if runs_text(inner.get("runs", [])).strip():
                        return runs_text(inner["runs"])
        return ""
    return runs_text(block.get("runs", []))
