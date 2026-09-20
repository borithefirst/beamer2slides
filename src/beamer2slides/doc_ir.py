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

# textStyle keys we carry as a tag of their own, and the tag each becomes.
# `code` is no longer *read* from a document — a face is carried as the face it is
# (`font`), so Consolas and Roboto Mono stop being the same thing — but the tag is
# still written and still understood, so a file written before that keeps working.
MARKS = {"bold": "b", "italic": "i", "underline": "u", "strike": "s", "code": "code"}
# The marks themselves, and the `textStyle` field each one is. A mark is True, absent
# — or **False**, which is not the same thing: a theme that makes its headings bold
# leaves a reader who un-bolds one word with a run that says `bold: false`, and a file
# that could only say "bold" or nothing would have that word bold again the first time
# the source restyled the block (`_style_of`, `data-off`).
MARK_FIELDS = (("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
               ("strike", "strikethrough"), ("smallcaps", "smallCaps"))
# A run raised or lowered from the baseline. Not a mark, because it is not a flag:
# `baselineOffset` is one of NONE / SUPERSCRIPT / SUBSCRIPT, so the run says which,
# and "none" is the third value rather than the absence of the other two — a theme
# could raise a whole named style, and a reader who puts one word back on the
# baseline must be able to say so (the `data-off` reasoning, one value wider).
SCRIPTS = {"SUPERSCRIPT": "super", "SUBSCRIPT": "sub", "NONE": "none"}
SCRIPT_TAGS = {"super": "sup", "sub": "sub"}
# What `<code>` has always meant on the write side, and goes on meaning.
CODE_FAMILY = "Courier New"
# A named style is a block kind; everything else is a paragraph.
HEADINGS = {f"HEADING_{n}": n for n in range(1, 7)}
NAMED_STYLE = {n: f"HEADING_{n}" for n in range(1, 7)} | {0: "NORMAL_TEXT"}
# The two named styles that are not a heading level. They are a block kind of their
# own rather than a level, because that is what they are: Docs has NORMAL_TEXT,
# TITLE, SUBTITLE and HEADING_1..6, and nothing else. `namedStyleType` is a field the
# merge owns (`doc_merge.MANAGED_PARAGRAPH`), so a style it cannot name is one it
# writes NORMAL_TEXT over: before these were modelled, a document's Title that the
# source moved, rewrote or restyled came back as body text, and nothing said so.
NAMED_KINDS = {"TITLE": "title", "SUBTITLE": "subtitle"}
KIND_STYLE = {kind: style for style, kind in NAMED_KINDS.items()}
# Every kind that is one paragraph of text.
TEXT_KINDS = ("paragraph", "heading", "item", *NAMED_KINDS.values())
# And every kind that is a structural element and not a paragraph. Docs' index rules
# are about these: nothing can be inserted at one's own index, the newline in front of
# one cannot be deleted, a body cannot open or end on one without an empty paragraph
# beside it, and one is deleted by its own span. `doc_merge._structural` is the test.
STRUCTURAL = ("table", "toc")
ALIGNMENTS = {"START": "left", "CENTER": "center", "END": "right", "JUSTIFIED": "justify"}
TO_ALIGNMENT = {v: k for k, v in ALIGNMENTS.items()}
# Paragraph properties the importer keeps, and the CSS each is written as (measured,
# docs/google-docs.md: "Paragraph CSS: `text-align` (incl. justify), `margin-left`,
# `text-indent`, `line-height`"). A length is always written in points.
PARAGRAPH_CSS = {"indent": "margin-left", "indent_first": "text-indent",
                 "line_spacing": "line-height"}
# Paragraph properties no HTML can carry, and the attribute each is written as. The
# spelling matters: `background-color` on a `<p>` is *not* paragraph shading, it is
# "a character highlight on its runs" (measured), so the CSS would be a lie; and the
# study lists no `margin-top`/`margin-bottom` among what survives. All three are
# written by `batchUpdate` instead (`doc_merge.tidy_requests`), which was measured
# working for `updateParagraphStyle` with `shading`.
PARAGRAPH_DATA = {"shading": "data-shading", "space_above": "data-space-above",
                  "space_below": "data-space-below",
                  "border_top": "data-border-top", "border_bottom": "data-border-bottom",
                  "border_left": "data-border-left", "border_right": "data-border-right",
                  "page_break": "data-page-break",
                  "keep_with_next": "data-keep-with-next"}
# The four sides of a paragraph's border, and the `paragraphStyle` field of each.
# Borders sit in the very dialog that sets shading (Format → Paragraph styles →
# Borders and shading), so a document whose shading round-tripped while its rule
# vanished on the first rewrite was a difference nobody could explain.
BORDER_SIDES = {"border_top": "borderTop", "border_bottom": "borderBottom",
                "border_left": "borderLeft", "border_right": "borderRight"}
# The two paragraph properties that are simply on or off.
PARAGRAPH_FLAGS = {"page_break": "pageBreakBefore", "keep_with_next": "keepWithNext"}
# Docs' dash styles, spelled as CSS spells them, since the file says a border the
# way CSS says one: `<width> <style> <colour>`, and `pad <n>pt` after it when Docs
# leaves a gap between the rule and the text.
DASH_STYLES = {"SOLID": "solid", "DOT": "dotted", "DASH": "dashed"}
TO_DASH_STYLE = {css: api for api, css in DASH_STYLES.items()}
BORDER_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)pt\s+(solid|dotted|dashed)\s+"
                       r"(#[0-9a-fA-F]{6})(?:\s+pad\s+(-?\d+(?:\.\d+)?)pt)?\s*$")
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
# The first tab's own title. The file's `<title>` is the *document's* name, and a
# document's name is not the name of the tab you are looking at: a document of one tab
# has both, and they differ as soon as somebody renames either. Every other tab says
# its title on its `<section>`, so without this the first tab alone could not be named
# from the file — read back at every settle, so a rename in the file went twice over.
TAB_META = "b2s-tab"
# An `<img width>` is CSS pixels, a document's picture size is points (measured: a
# 60 × 40 px picture imports as 45 × 30 pt).
PT_PER_PX = 0.75
# Picture fields: what the canonical file says about one, and what only a sync keeps.
PICTURE_ATTRS = ("src", "alt", "title")


# ---------------------------------------------------------------- runs

def _style_of(text_style: dict, default: dict | None = None) -> dict:
    """The marks we keep, from a Docs textStyle.

    `default` is what the paragraph's named style already says (`_named_defaults`):
    a run that only repeats it needs nothing in the canonical file, and clearing the
    field leaves the same face on the page. Without that, a document whose importer
    wrote `font-family` on every run would come back as a file of spans, which is
    the opposite of what a file in git is for.

    A size is kept as the document reports it, to two places. The importer rounds a
    size to a whole point (measured: 7.5pt → 7pt), so a half point written in the
    file is lost at `push` — but the push settles by regenerating the file from the
    document it made, so the file then says 7 and nothing oscillates. Every write
    after that is a `batchUpdate`, which takes the size as given: rounding here
    would instead move a size a reader chose in the editor.
    """
    style = {}
    default = default or {}
    for key, api in MARK_FIELDS:
        if text_style.get(api):
            style[key] = True
        elif api in text_style and key in (default.get("marks") or ()):
            # The theme says this mark and the run says no. Saying nothing here would
            # leave the file unable to tell that from a run that simply inherits, and
            # the field is named with no value on every restyle (`doc_merge.MANAGED`),
            # which is "back to what you inherit": the reader's choice would be
            # undone by a source edit that never mentioned it.
            style[key] = False
    script = SCRIPTS.get(text_style.get("baselineOffset", ""))
    if script == "none":
        # Said out loud only against a named style that raises the run, exactly as a
        # mark is said off: everywhere else NONE is what a run with nothing on it
        # falls back to, and writing `<span data-script="none">` around every word of
        # an ordinary paragraph is the wall of spans this is all about avoiding.
        script = "none" if default.get("script") else None
    if script:
        style["script"] = script
    family = text_style.get("weightedFontFamily", {}).get("fontFamily")
    if family and family != default.get("font"):
        style["font"] = family
    size = text_style.get("fontSize", {}).get("magnitude")
    if size:
        size = round(float(size), 2)
        if size and size != default.get("fontsize"):
            style["fontsize"] = size
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
    lists = (doc.get("lists") if "lists" in doc else {}) or _tab_part(doc, tab, "lists")
    objects = (doc.get("inlineObjects") if "body" in doc else None) \
        or _tab_part(doc, tab, "inlineObjects")
    defaults = _named_defaults(doc, tab)
    for element in body:
        block = _block_of(element, lists, objects, defaults)
        if block:
            ir["blocks"].append(block)
    _hide_trailer(ir)
    return ir


def _hide_trailer(ir: dict) -> None:
    """Leave out the empty paragraph a body keeps after a table that ends it.

    A document must end on a paragraph, so a table written last — by an import or by
    `insertTable` — always has an empty one after it that nobody asked for, and that
    no request can delete (measured: the mark in front of it is refused). Read as a
    block it would put a `<p></p>` into the canonical file that the next import makes
    again, and a table appended at the end would never converge. Its span is kept as
    `trailer`, because that is where a block appended after the table is written.
    It is the other half of the paragraph the table was inserted into, so it can come
    with that one's bullet or heading: `trailer_kind` says so, and
    `doc_merge.tidy_requests` makes it a plain paragraph again.
    """
    blocks = ir["blocks"]
    # A body with nothing in it — a tab just added — is one empty paragraph, and that
    # one is a trailer too: what is written there goes *into* it.
    if (blocks and (len(blocks) == 1 or blocks[-2]["kind"] in STRUCTURAL)
            and blocks[-1]["kind"] in TEXT_KINDS and not blocks[-1]["runs"]):
        last = blocks.pop()
        ir["trailer"] = last["span"]
        if last["kind"] != "paragraph" or last.get("align"):
            ir["trailer_kind"] = last["kind"]
    # Its mirror image: a table cannot be the first thing in a body, so one that is has
    # an empty paragraph in front of it that no request can delete either (measured: a
    # tab's first table, made by `insertTable`, always does). That one is the `lead`,
    # and the first block written in front of the table goes into it.
    if (len(blocks) >= 2 and blocks[1]["kind"] in STRUCTURAL
            and blocks[0]["kind"] in TEXT_KINDS and not blocks[0]["runs"]):
        first = blocks.pop(0)
        ir["lead"] = first["span"]
        if first["kind"] != "paragraph" or first.get("align"):
            ir["lead_kind"] = first["kind"]


def _body_of(doc: dict, tab_id: str | None) -> tuple[list, str | None]:
    if "body" in doc:
        return doc["body"].get("content", []), None
    for tab in _flatten_tabs(doc.get("tabs", [])):
        props = tab.get("tabProperties", {})
        if tab_id in (None, props.get("tabId")):
            return tab.get("documentTab", {}).get("body", {}).get("content", []), props.get("tabId")
    return [], tab_id


def tabs_of(doc: dict) -> list:
    """Every tab of a document read with `includeTabsContent`, child tabs included,
    in the order the document shows them."""
    return _flatten_tabs(doc.get("tabs", []))


def parts(ir: dict) -> list[dict]:
    """The IR's tabs, each an IR of its own: the first tab is the IR itself (its
    blocks are the file's body), the others are `ir["tabs"]` — `<section>`s in the
    file, each with its `tab` id, `title` and, for a child tab, `parent`."""
    return [ir] + list(ir.get("tabs", []))


def tab_part(ir: dict | None, tab: str | None) -> dict | None:
    """The tab of `ir` with that id, past the first; None when it has none."""
    for part in (ir or {}).get("tabs", []):
        if tab and part.get("tab") == tab:
            return part
    return None


# ---------------------------------------------------------------- equations

# How much of the words beside an equation anchors it in the Markdown export.
ANCHOR = 24
# What the export puts between characters that the document's text does not have: an
# escape, and the markers of bold, italic and strikethrough.
_MARKUP = r"[\\*_~]*"
# The start of a Markdown line, past whatever opens the block: a list marker, a
# heading's hashes, a quote, a table cell's bar.
_LINE_START = r"(?:^|(?<=\n))[ \t]*(?:(?:[*+-]|\d+\.|#+|>|\|)[ \t]*)*" + _MARKUP
# The end of a paragraph's line: a hard break, a table cell's bar, or the end.
_LINE_END = _MARKUP + r"[ \t]*(?:\\?\n|\||\Z)"


def equation_spots(doc: dict) -> list[dict]:
    """Every equation of a document read with `includeTabsContent`, in the order the
    document shows them, with the words on either side of it in its paragraph.

    `before` / `after` are `_beside`'s: the words up to the next thing that is not
    text — another equation, a chip, or the paragraph's edge — and which of those it
    is, since the Markdown export writes them differently (`latex_of`).
    """
    spots: list[dict] = []
    for tab in _flatten_tabs(doc.get("tabs", [])):
        tab_id = tab.get("tabProperties", {}).get("tabId")
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        for paragraph in _paragraphs(body):
            pieces = []
            for el in paragraph.get("elements", []):
                if "textRun" in el:
                    pieces.append(("text", el["textRun"].get("content", "").rstrip("\n")))
                elif "equation" in el:
                    pieces.append(("equation", el.get("startIndex", 0)))
                else:
                    pieces.append(("object", None))
            for i, (kind, start) in enumerate(pieces):
                if kind != "equation":
                    continue
                spots.append({"tab": tab_id, "start": start,
                              "before": _beside(pieces[:i][::-1], backwards=True),
                              "after": _beside(pieces[i + 1:])})
    return spots


def _paragraphs(content: list):
    for element in content:
        if "paragraph" in element:
            yield element["paragraph"]
        for row in element.get("table", {}).get("tableRows", []):
            for cell in row.get("tableCells", []):
                yield from _paragraphs(cell.get("content", []))


def _beside(pieces: list, backwards: bool = False) -> dict:
    """The words next to an equation, walking away from it until something that is
    not a word: {"text": ..., "then": "edge" | "equation" | "object"}. `backwards`
    walks the pieces in front of it, nearest first."""
    text, then = [], "edge"
    for kind, value in pieces:
        if kind != "text":
            then = kind
            break
        text.append(value)
    return {"text": "".join(text[::-1] if backwards else text), "then": then}


def latex_of(spots: list[dict], markdown: str) -> dict[tuple, str]:
    """Each equation's LaTeX from the document's Markdown export, by (tab, start).

    The export writes an equation as `$…$` (or `$$…$$` on a line of its own) and
    escapes neither a dollar in the text nor one in the equation (measured: `costs $5`
    and `${x}_{1}+α_$$`), so the dollars alone cannot say where one ends. The words the
    document puts on either side can: each equation is looked for between them, in
    document order, and the shortest LaTeX that fits is taken. An equation that is not
    found with certainty gets none — the file then shows it without its LaTeX, as it
    did before.
    """
    found: dict[tuple, str] = {}
    cursor, previous = 0, False
    for spot in spots:
        before, after = spot["before"], spot["after"]
        if before["text"]:
            prefix = _loose(before["text"][-ANCHOR:]) + _MARKUP + r"(?:\]\([^)\n]*\))?" + _MARKUP
        elif before["then"] == "edge":
            prefix = _LINE_START
        elif before["then"] == "equation" and previous:
            prefix = r"\G[ \t]*"
        else:
            prefix = None
        if after["text"]:
            suffix = _MARKUP + r"\[?" + _loose(after["text"][:ANCHOR])
        elif after["then"] == "edge":
            suffix = _LINE_END
        elif after["then"] == "equation":
            suffix = r"[ \t]*" + _MARKUP + r"\$"
        else:
            suffix = None
        previous = False
        if prefix is None and suffix is None:
            continue
        pattern = (prefix or "") + r"(\$\$?)(.+?)\1" + (f"(?={suffix})" if suffix else "")
        if prefix == r"\G[ \t]*":
            match = re.compile(pattern[2:]).match(markdown, cursor)
        else:
            match = re.compile(pattern, re.M).search(markdown, cursor)
        if match is None:
            continue
        found[(spot["tab"], spot["start"])] = match.group(2)
        cursor, previous = match.end(), True
    return found


def _loose(text: str) -> str:
    """A pattern for `text` as the Markdown export writes it: any escape or style
    marker allowed in front of each character, and white space as any white space."""
    out, space = [], False
    for char in text:
        if char.isspace():
            if not space:
                out.append(r"\s+")
            space = True
            continue
        space = False
        out.append(_MARKUP + re.escape(char))
    return "".join(out)


def attach_latex(ir: dict, found: dict[tuple, str]) -> int:
    """Write each equation's LaTeX into its frozen run's text, where `latex_of` found
    it: the file then shows what the equation says, where it showed nothing."""
    done = 0
    for part in parts(ir):
        tab = part.get("tab")
        for block in _all_blocks(part["blocks"]):
            at = block.get("span", [0])[0]
            for run in block.get("runs", []):
                if run.get("chip") == "equation" and (tab, at) in found:
                    run["text"] = found[(tab, at)]
                    done += 1
                at += run.get("width", 1 if run.get("frozen") else utf16_len(run["text"]))
    return done


def _all_blocks(blocks: list):
    for block in blocks:
        yield block
        for row in block.get("rows", []):
            for cell in row:
                yield from _all_blocks(cell)


def _flatten_tabs(tabs: list) -> list:
    out = []
    for tab in tabs:
        out.append(tab)
        out += _flatten_tabs(tab.get("childTabs", []))
    return out


def _named_defaults(doc: dict, tab_id: str | None) -> dict:
    """What each named style says, by `namedStyleType`.

    A paragraph and its runs report the properties *set on them*, and an import sets
    plenty that only repeat the style they are already in. Subtracting the named
    style keeps the canonical file down to what somebody chose: everything here is
    what a cleared field falls back to anyway, so nothing is lost by leaving it out.
    """
    styles = (doc.get("namedStyles") if "body" in doc else None) \
        or _tab_part(doc, tab_id, "namedStyles")
    out: dict = {}
    for style in (styles or {}).get("styles", []):
        text, para = style.get("textStyle", {}), style.get("paragraphStyle", {})
        size = text.get("fontSize", {}).get("magnitude")
        out[style.get("namedStyleType", "")] = {
            # Which marks this style puts on, so that a run saying one of them off
            # can be told from a run inheriting it (`_style_of`). The marks are not
            # subtracted the way the face and the size are: a run that repeats the
            # theme's bold is written bold, which costs the file a `<b>` and keeps it
            # readable on its own.
            "marks": {key for key, api in MARK_FIELDS if text.get(api)},
            # Whether this style *raises* its runs, for the same reason. A style that
            # says NONE says what every run falls back to anyway, so it is no default
            # to tell a run apart from.
            "script": SCRIPTS.get(text.get("baselineOffset", "")) if
            text.get("baselineOffset") in ("SUPERSCRIPT", "SUBSCRIPT") else None,
            "font": text.get("weightedFontFamily", {}).get("fontFamily"),
            "fontsize": round(float(size), 2) if size else None,
            # A theme that centres its headings says so here, and the heading itself
            # then reports no alignment at all. Without this the file said nothing
            # and the write side put START back: the document's own theme, undone by
            # a source edit that never mentioned alignment.
            "align": ALIGNMENTS.get(para.get("alignment", "")),
        } | _paragraph_measures(para)
    return out


def _paragraph_measures(style: dict) -> dict:
    """The paragraph properties the dialect carries, from a Docs paragraphStyle.
    A property the paragraph does not set is None: it is inherited, not zero."""
    out = {key: _points(style.get(api)) for key, api in
           (("indent", "indentStart"), ("indent_first", "indentFirstLine"),
            ("space_above", "spaceAbove"), ("space_below", "spaceBelow"))}
    spacing = style.get("lineSpacing")
    out["line_spacing"] = round(spacing / 100, 3) if spacing else None
    rgb = style.get("shading", {}).get("backgroundColor", {}).get("color", {}).get("rgbColor")
    out["shading"] = _hex(rgb) if rgb else None
    for key, api in BORDER_SIDES.items():
        out[key] = _border(style.get(api))
    for key, api in PARAGRAPH_FLAGS.items():
        out[key] = True if style.get(api) else None
    return out


def _border(side: dict | None) -> str | None:
    """One paragraph border as the file spells it: CSS's `<width> <style> <colour>`,
    with `pad <n>pt` after it where Docs leaves a gap between the rule and the text.

    A border of no width is no border — Docs reports a rule somebody took off that
    way, with its colour still on it — so the file says nothing rather than `0pt`,
    which would make taking a rule off read as setting one.
    """
    if not side:
        return None
    width = _points(side.get("width")) or 0.0
    if not width:
        return None
    rgb = side.get("color", {}).get("color", {}).get("rgbColor")
    said = (f"{_number(width)}pt {DASH_STYLES.get(side.get('dashStyle', ''), 'solid')} "
            f"{_hex(rgb) if rgb else '#000000'}")
    pad = _points(side.get("padding")) or 0.0
    return f"{said} pad {_number(pad)}pt" if pad else said


def _points(dimension: dict | None) -> float | None:
    """A Docs Dimension in points. Rounded, so two reads of one document spell the
    same number and a diff of the file shows only what somebody changed."""
    if not dimension or "magnitude" not in dimension:
        return None
    return round(float(dimension["magnitude"]), 2)


def _tab_part(doc: dict, tab_id: str | None, part: str) -> dict:
    """A tab's `lists` or `inlineObjects`: they sit beside its body, not in it."""
    for tab in _flatten_tabs(doc.get("tabs", [])):
        if tab_id in (None, tab.get("tabProperties", {}).get("tabId")):
            return tab.get("documentTab", {}).get(part, {}) or {}
    return {}


def _picture(run: dict, objects: dict) -> dict:
    """What the document says about a picture: its size, its alt text, and where its
    pixels can be fetched for the next half hour (`uri`, never written to a file)."""
    embedded = (objects.get(run.get("value"), {}).get("inlineObjectProperties", {})
                .get("embeddedObject", {}))
    size = embedded.get("size", {})
    width = size.get("width", {}).get("magnitude")
    height = size.get("height", {}).get("magnitude")
    if width and height:
        run["size"] = [round(width / PT_PER_PX), round(height / PT_PER_PX)]
    for key, api in (("alt", "description"), ("title", "title")):
        if embedded.get(api):
            run[key] = embedded[api]
    uri = embedded.get("imageProperties", {}).get("contentUri")
    if uri:
        run["uri"] = uri
    return run


def _block_of(element: dict, lists: dict, objects: dict | None = None,
              defaults: dict | None = None) -> dict | None:
    objects = objects or {}
    defaults = defaults or {}
    if "table" in element:
        return _table_block(element, lists, objects, defaults)
    if "tableOfContents" in element:
        # Generated content: readable, never writable (no insertTableOfContents in v1).
        return {"kind": "toc", "frozen": True, "runs": [],
                "span": [element.get("startIndex", 0), element.get("endIndex", 0)]}
    para = element.get("paragraph")
    if para is None:
        return None
    default = defaults.get(para.get("paragraphStyle", {}).get("namedStyleType")
                           or "NORMAL_TEXT", {})
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
                                | _style_of(el["textRun"].get("textStyle", {}), default))
            continue
        for key, kind in CHIPS.items():
            if key in el:
                run = _chip_run(kind, el[key]) | {"width": width}
                runs.append(_picture(run, objects) if kind == "image" else run)
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
    elif style.get("namedStyleType") in NAMED_KINDS:
        block["kind"] = NAMED_KINDS[style["namedStyleType"]]
    else:
        block["kind"] = "paragraph"
    align = ALIGNMENTS.get(style.get("alignment", ""))
    if align and align != (default.get("align") or "left"):
        block["align"] = align
    # A property that only says what the paragraph's named style already says is left
    # out; so are a bullet's own indents, which belong to the list preset and not to
    # anybody's choice — writing them back would fight `createParagraphBullets`, and
    # the file would then differ from the document at every sync.
    measured = _paragraph_measures(style)
    for key in PARAGRAPH_CSS | PARAGRAPH_DATA:
        if block["kind"] == "item" and key in ("indent", "indent_first"):
            continue
        if measured[key] is not None and measured[key] != _inherited(key, default):
            block[key] = measured[key]
    return block


def _inherited(key: str, default: dict):
    """What a paragraph that sets nothing shows: its named style's value, or, where
    the style says nothing either, the property's own default — single spacing, no
    indent, no space around it, no shading."""
    if default.get(key) is not None:
        return default[key]
    return ({"line_spacing": 1.0, "shading": None}
            | {k: None for k in BORDER_SIDES} | {k: None for k in PARAGRAPH_FLAGS}
            ).get(key, 0.0)


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
    the canonical file fills it in, and `doc_merge.bullet_requests` gives the list
    bullets of its own, which read back properly from then on.
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


def _table_block(element: dict, lists: dict, objects: dict, defaults: dict | None = None) -> dict:
    rows = []
    for row in element["table"].get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            blocks = [b for b in (_block_of(e, lists, objects, defaults)
                                  for e in cell.get("content", [])) if b]
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
    if ir.get("tab_title"):
        lines.append(f'<meta name="{TAB_META}" content="{escape(ir["tab_title"], quote=True)}">')
    if ir.get("title"):
        lines.append(f"<title>{escape(ir['title'])}</title>")
    lines += ["</head>", "<body>"]
    lines += _blocks_html(ir["blocks"], depth=0)
    for part in ir.get("tabs", []):
        # The tabs past the first. `data-tab` is the document's id for one; a section
        # without it is a tab the source asks for and the next sync creates.
        attrs = "".join(f' {name}="{escape(part[key], quote=True)}"'
                        for name, key in (("data-tab", "tab"), ("title", "title"),
                                          ("data-parent", "parent")) if part.get(key))
        lines += [f"<section{attrs}>"] + _blocks_html(part["blocks"], depth=1) + ["</section>"]
    lines += ["</body>", "</html>", ""]
    return "\n".join(lines)


def blocks_html(blocks: list[dict]) -> str:
    """The blocks as the file writes them: what two reads of a tab are compared by."""
    return "\n".join(_blocks_html(blocks, 0))


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
        if block["kind"] == "table":
            lines += _table_html(block, depth)
            index += 1
            continue
        lines.append(pad + _block_html(block))
        index += 1
    return lines


def _table_html(block: dict, depth: int) -> list[str]:
    """A table over several lines, one per row, so a git diff reads like the document.

    The line breaks go between `</tr>` and `<tr>`, and between the table's own tags
    and its rows — the places where an HTML parser has nowhere to put text, so the
    white space cannot become content. A row stays on one line with its cells: it is
    inside a `<td>` that white space *would* be content. (Measured on `from_html`;
    the importer's side of it wants confirming on a live document.)
    """
    pad = " " * depth
    lines = [pad + f"<table{_key_attr(block)}>"]
    for row in block["rows"]:
        cells = "".join(f"<td>{''.join(_blocks_html(cell, 0))}</td>" for cell in row)
        lines.append(pad + f" <tr>{cells}</tr>")
    return lines + [pad + "</table>"]


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
        lines.append(pad + f" <li{_key_attr(items[index])}"
                           f"{_paragraph_attrs(items[index])}>"
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
    """One block on one line. A table is the exception (`_table_html`): it is written
    across several, and `_blocks_html` sends it there before this is reached."""
    kind = block["kind"]
    if kind == "toc":
        return f'<p class="b2s-toc"{_key_attr(block)}></p>'
    tag = f"h{block['level']}" if kind == "heading" else "p"
    return (f"<{tag}{_key_attr(block)}{_paragraph_attrs(block)}>"
            f"{_runs_html(block['runs'])}</{tag}>")


def _paragraph_attrs(block: dict) -> str:
    """What a paragraph says about itself past its words: its alignment and indents
    as CSS the importer keeps, its shading and the space around it as attributes of
    ours, which only `batchUpdate` can write (`PARAGRAPH_DATA` says why)."""
    # Title and Subtitle have no tag: HTML's headings are levels and these are not,
    # so they go in as ours. Whether the importer makes anything of a `<p>` like this
    # is not measured and does not matter — `push` settles by regenerating the file
    # from the document it made, and every write after that is a `batchUpdate`, which
    # names the style outright.
    out = (f' data-style="{block["kind"]}"' if block["kind"] in KIND_STYLE else "")
    styles = [f"text-align:{block['align']}"] if block.get("align") else []
    for key, css in PARAGRAPH_CSS.items():
        if block.get(key) is not None:
            unit = "" if key == "line_spacing" else "pt"
            styles.append(f"{css}:{_number(block[key])}{unit}")
    out += f' style="{";".join(styles)}"' if styles else ""
    for key, name in PARAGRAPH_DATA.items():
        if block.get(key) is not None:
            out += f' {name}="{escape(str(_number(block[key])), quote=True)}"'
    return out


def _number(value) -> str:
    """A measurement as the file spells it: no trailing zeros, so one number always
    reads the same way and a diff shows only what somebody changed."""
    if isinstance(value, str):
        return value
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _runs_html(runs: list[dict]) -> str:
    return "".join(_run_html(r) for r in runs)


def _img_html(run: dict) -> str:
    """A picture: where its file is, what it says, how big, and which object it is.

    `data-object` is the document's name for it. The document cannot say which file a
    picture came from — `insertInlineImage` records the staging URL, and nothing can
    set a field of ours on it — so this attribute is what ties the two together the
    next time round, the way `id=` does for a block.
    """
    attrs = "".join(f' {k}="{escape(str(run[k]), quote=True)}"' for k in PICTURE_ATTRS if run.get(k))
    if run.get("size"):
        attrs += ' width="%d" height="%d"' % tuple(run["size"])
    if run.get("value"):
        attrs += f' data-object="{escape(run["value"], quote=True)}"'
    return f"<img{attrs}>"


def _run_html(run: dict) -> str:
    if run.get("chip") == "image":
        return _img_html(run)
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
    if run.get("font"):
        # One name, unquoted, no fallback list: a list is *mangled* on import
        # (measured, docs/google-docs.md: `Georgia, serif` arrives as `Geo`), while a
        # single name survives verbatim, `Comic Sans MS` included.
        styles.append(f"font-family:{run['font']}")
    if run.get("fontsize"):
        # In points, as the document says it (the importer rounds a fraction away;
        # `_style_of` says why that is harmless). The key is `fontsize`, not `size`:
        # a picture run's `size` is its width and height.
        styles.append(f"font-size:{_number(run['fontsize'])}pt")
    attrs = f' style="{escape(";".join(styles), quote=True)}"' if styles else ""
    if run.get("smallcaps"):
        # No CSS reaches `smallCaps` through the importer — the study lists it only
        # among the things `updateTextStyle` writes — so the file says it in an
        # attribute of ours and `doc_merge` writes it with `batchUpdate`.
        attrs += ' data-smallcaps="1"'
    if run.get("script") == "none":
        # A run put back on the baseline against a named style that raises it. HTML
        # has no opposite of `<sup>` any more than it has one of `<b>`, so the file
        # says it in an attribute of ours, as `data-off` does for the marks.
        attrs += ' data-script="none"'
    off = " ".join(key for key, _ in MARK_FIELDS if run.get(key) is False)
    if off:
        # A mark turned off against a theme that turns it on. HTML has no tag for it
        # — `<b>` has no opposite — so the file names the marks in an attribute of
        # ours, as it does small caps, and `doc_merge` writes them `False`.
        attrs += f' data-off="{off}"'
    if attrs:
        out = f"<span{attrs}>{out}</span>"
    if run.get("script") in SCRIPT_TAGS:
        out = f"<{SCRIPT_TAGS[run['script']]}>{out}</{SCRIPT_TAGS[run['script']]}>"
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
        self.target: list[dict] = self.ir["blocks"]  # the blocks of the tab being read

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
            self.target.append(block)

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
            elif attr.get("name") == TAB_META and attr.get("content"):
                self.ir["tab_title"] = attr["content"]
        elif tag == "title":
            self.in_title = True
        elif tag == "section":
            self._close()
            part = {"title": attr.get("title") or "", "blocks": []}
            if attr.get("data-tab"):
                part["tab"] = attr["data-tab"]
            if attr.get("data-parent"):
                part["parent"] = attr["data-parent"]
            self.ir.setdefault("tabs", []).append(part)
            self.target = part["blocks"]
        elif tag in ("ul", "ol"):
            self.lists.append(tag == "ol")
        elif tag == "li":
            self._open({"kind": "item", "level": max(0, len(self.lists) - 1),
                        "ordered": bool(self.lists and self.lists[-1]), "runs": []}
                       | _paragraph_of(attr) | _key_of(attr))
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            if attr.get("class") == "b2s-toc":
                self._emit({"kind": "toc", "frozen": True, "runs": []} | _key_of(attr))
                return
            if tag != "p":
                block = {"kind": "heading", "level": int(tag[1]), "runs": []}
            else:
                named = attr.get("data-style", "")
                block = {"kind": named if named in KIND_STYLE else "paragraph",
                         "runs": []}
            self._open(block | _paragraph_of(attr) | _key_of(attr))
        elif tag == "img":
            # A picture is a frozen run like a chip — one index unit, never rewritten
            # as text — that a sync can nevertheless create (`doc_merge.writable`).
            run = {"chip": "image", "frozen": True, "text": ""}
            run |= {k: attr[k] for k in PICTURE_ATTRS if attr.get(k)}
            if attr.get("data-object"):
                run["value"] = attr["data-object"]
            size = [_pixels(attr.get("width")), _pixels(attr.get("height"))]
            if all(size):
                run["size"] = size
            if self.block is None:
                # A picture on its own, outside any paragraph: it is one.
                self._open({"kind": "paragraph", "runs": [run]})
                self._close()
            else:
                self.block["runs"].append(run)
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
        elif tag in ("sup", "sub"):
            self.marks.append({"script": "super" if tag == "sup" else "sub"})
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
                frame = _span_style(attr.get("style", ""))
                if attr.get("data-smallcaps"):
                    frame["smallcaps"] = True
                if attr.get("data-script"):
                    frame["script"] = attr["data-script"]
                for key in attr.get("data-off", "").split():
                    frame[key] = False
                self.marks.append(frame)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "section":
            self._close()
            self.target = self.ir["blocks"]
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
        elif tag in ("b", "strong", "i", "em", "u", "s", "strike", "del", "code", "a",
                     "span", "sup", "sub"):
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


def _pixels(value: str | None) -> int | None:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", value or "")
    return round(float(match.group(1))) if match else None


def _key_of(attr: dict) -> dict:
    return {"key": attr["id"]} if attr.get("id") else {}


def _css(style: str):
    """The declarations of a `style=` attribute, lowercased property and raw value."""
    for piece in style.split(";"):
        key, _, value = piece.partition(":")
        if key.strip():
            yield key.strip().lower(), value.strip()


def _span_style(style: str) -> dict:
    out = {}
    for key, value in _css(style):
        if key == "color":
            out["color"] = value
        elif key == "background-color":
            out["highlight"] = value
        elif key == "font-family":
            # Read as written: one name, no fallback list (`_run_html` says why).
            out["font"] = value.strip("'\"")
        elif key == "font-size":
            size = _length(value)
            if size:
                out["fontsize"] = size
    return out


def _paragraph_of(attr: dict) -> dict:
    """What a `<p>`, `<h*>` or `<li>` says about the paragraph itself.

    The CSS half of it is what the importer keeps; the `data-` half is what only
    `batchUpdate` can write (`PARAGRAPH_CSS`, `PARAGRAPH_DATA`).
    """
    out: dict = {}
    css = {key: value for key, value in _css(attr.get("style", ""))}
    if css.get("text-align") in TO_ALIGNMENT:
        out["align"] = css["text-align"]
    for key, name in PARAGRAPH_CSS.items():
        if name in css:
            value = (_ratio(css[name]) if key == "line_spacing" else _length(css[name]))
            if value is not None:
                out[key] = value
    for key, name in PARAGRAPH_DATA.items():
        if attr.get(name):
            if key == "shading":
                value = attr[name]
            elif key in BORDER_SIDES:
                value = _border_text(attr[name])
            elif key in PARAGRAPH_FLAGS:
                value = True
            else:
                value = _length(attr[name])
            if value is not None:
                out[key] = value
    return out


def _border_text(said: str) -> str | None:
    """A border the file spells, read back — normalised, so the same rule always
    reads the same way. One this dialect cannot spell is no border at all rather
    than a guess, as a length in an unknown unit is."""
    match = BORDER_RE.match(said or "")
    if not match:
        return None
    width, dash, colour, pad = match.groups()
    out = f"{_number(float(width))}pt {dash} {colour.lower()}"
    return f"{out} pad {_number(float(pad))}pt" if pad and float(pad) else out


def _length(value: str) -> float | None:
    """A measurement in points. The dialect writes `pt` and nothing else, so a unit
    we don't know is read as no measurement at all rather than guessed at."""
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(pt)?\s*$", value or "")
    return round(float(match.group(1)), 2) if match else None


def _ratio(value: str) -> float | None:
    """A line height: the unitless multiplier Docs calls `lineSpacing` (× 100)."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*$", value or "")
    return round(float(match.group(1)), 3) if match else None


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
    for part in ir.get("tabs", []):
        key_blocks(part)  # a named range belongs to its tab: keys are unique per tab
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

    What no block takes is left on the IR as `orphans`, for `name_requests` to
    delete — see `orphan_requests` for why a range outliving its block is a key
    waiting to be stolen.
    """
    starts = []
    for name, entry in named_ranges.items():
        if not name.startswith(KEY_PREFIX):
            continue
        for ranged in entry.get("namedRanges", []):
            for span in ranged.get("ranges", []):
                starts.append((span.get("startIndex", 0), span.get("endIndex", 0),
                               name[len(KEY_PREFIX):], ranged.get("namedRangeId", "")))
    starts.sort()
    taken: set[str] = set()
    for block in ir["blocks"]:
        low, high = block.get("span", [0, 0])
        for start, end, key, range_id in starts:
            if low <= start < high and key not in taken:
                block["key"] = key
                block["rangeId"] = range_id
                # Where the range is, as against where `name_requests` would put it:
                # a range drifts (an insert at its first index pushes it along).
                block["range"] = [start, end]
                taken.add(key)
                break
    held = {block.get("rangeId") for block in ir["blocks"]}
    orphans = [range_id for _, _, _, range_id in starts if range_id not in held]
    if orphans:
        ir["orphans"] = orphans
    else:
        ir.pop("orphans", None)
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


def anchor_range(block: dict) -> tuple[int, int] | None:
    """Where a keyed block's named range is planted: its anchor span short of the
    paragraph mark where it can be, so that deleting the newline between two
    paragraphs never silently stretches one block's identity over the other's
    words. An empty paragraph is all mark, so its range is that."""
    span = anchor_span(block) if block.get("key") else None
    if not span:
        return None
    low, high = span
    return low, max(high - 1, low + 1)


def replant_requests(ir: dict) -> list[dict]:
    """The named ranges that drifted, planted again where `anchor_range` puts them.

    A range drifts: text written *at* its first index pushes it along (Docs' rule),
    so a chip or a word put into an empty paragraph leaves the range on the
    paragraph mark, where the next "\\ntext" appended at that mark takes it away
    with the new block and a structural batch that swallows the mark deletes it.
    A range also *stretches*: text written inside it grows it (Docs' rule again), and
    a block written at the end of the one before it — which is how a block appended to
    a body goes in, "\\ntext" at the last paragraph's mark — grows that block's range
    over the new block's words. Two blocks under one name is one block's key gone: the
    first of them wins in `apply_keys`, and a later delete of *that* one leaves the
    stretched range sitting on the other's words, whose own key it then takes
    (campaign seeds 279 and 361, chain 4). A range may end short of its block, since
    text typed at the mark falls outside it, but it may never end past it.

    The old range goes (`deleteNamedRange`) and the new one is planted, in that
    order, so a name is never carried twice. Nothing here moves an index, so these
    can head any batch planned against `ir`.
    """
    out = []
    for block in ir["blocks"]:
        planted = anchor_range(block)
        here = block.get("range")
        if not planted or not block.get("rangeId") or not here \
                or (here[0] == planted[0] and here[1] <= planted[1]):
            continue
        out.append({"deleteNamedRange": {"namedRangeId": block["rangeId"]}})
        out.append({"createNamedRange": {
            "name": KEY_PREFIX + block["key"],
            "range": {"startIndex": planted[0], "endIndex": planted[1]}}})
    return out


def orphan_requests(ir: dict) -> list[dict]:
    """`deleteNamedRange` for every `b2s:` range no block is known by.

    A range outlives the block it named, and a range with nothing of its own is a
    key waiting to be stolen. The commonest way to make one is the commonest edit
    after typing: a reader backspaces at the start of a paragraph, Docs merges it
    into the one above keeping the first one's style, and *both* ranges are now
    inside the one paragraph that survives. `apply_keys` keeps the first, the
    document reads right, and nothing is wrong — until a source edit rewrites the
    words the winner covers. Deleting them takes its range with them, the loser is
    all that is left, and the block is suddenly known by the name of the paragraph
    that was swallowed: the key the file asserts names nothing, and a checkout that
    has not settled yet reads one block gone and one added (chain-4 seed 70140).

    The mirror of `replant_requests`' stretched range, and the same cure: one block,
    one name. Nothing here moves an index, so these can head any batch.
    """
    return [{"deleteNamedRange": {"namedRangeId": range_id}}
            for range_id in ir.get("orphans", [])]


def name_requests(ir: dict) -> list[dict]:
    """`createNamedRange` for every keyed block the document does not name yet — and
    again for one whose range drifted (`replant_requests`) or that no block is known
    by any more (`orphan_requests`)."""
    out = orphan_requests(ir) + replant_requests(ir)
    for block in ir["blocks"]:
        planted = anchor_range(block)
        if not planted or block.get("rangeId"):
            continue
        out.append({"createNamedRange": {
            "name": KEY_PREFIX + block["key"],
            "range": {"startIndex": planted[0], "endIndex": planted[1]}}})
    return out


def _first_words(block: dict) -> str:
    if block["kind"] == "table":
        for row in block.get("rows", []):
            for cell in row:
                for inner in cell:
                    if runs_text(inner.get("runs", [])).strip():
                        return runs_text(inner["runs"])
        return ""
    words = runs_text(block.get("runs", []))
    if not words.strip():
        # A paragraph that is a picture is named after the picture's file.
        for run in block.get("runs", []):
            if run.get("chip") == "image" and (run.get("src") or run.get("alt")):
                name = run.get("src") or ""
                return re.sub(r"\.\w+$", "", name.rsplit("/", 1)[-1]) if name else run["alt"]
    return words


# ---------------------------------------------------------------- what we do not read

# Every part of a `documents.get` answer this module consults, as a small graph: a
# node's `read` is what the reader takes the value of, and `into` maps a key to the
# node its value is — `[]` for a list of them, `{}` for a map of them. Anything a
# document carries that is in neither is what `unmodelled` reports.
#
# This is the other half of the convergence check. "A second sync writes 0 requests"
# is measured on the IR, so it proves the IR round-trips and says exactly nothing
# about what the IR never looked at: a property no node below names is a property a
# rewrite drops without a word. It is the same question `checks.lost_ink` asks of a
# converted slide — does every difference lie on something we account for? — and the
# same answer: name what is left over, out loud, rather than trust that there is none.
#
# Precision is the point, so a node is split wherever the reader reads less than the
# whole of it: a named style's textStyle is `namedTextStyle`, which reads the face, the
# size and the marks — not its colour, which nothing subtracts.
_NODES: dict[str, tuple[tuple, dict]] = {
    "document": (("documentId", "title", "revisionId", "suggestionsViewMode"),
                 {"body": "body", "tabs": "tab[]", "lists": "lists{}",
                  "inlineObjects": "inlineObject{}", "namedStyles": "namedStyles",
                  "namedRanges": "namedRangeGroup{}"}),
    "tab": ((), {"tabProperties": "tabProperties", "documentTab": "documentTab",
                 "childTabs": "tab[]"}),
    "tabProperties": (("tabId", "title", "parentTabId", "index", "nestingLevel"), {}),
    "documentTab": ((), {"body": "body", "lists": "lists{}",
                         "inlineObjects": "inlineObject{}", "namedStyles": "namedStyles",
                         "namedRanges": "namedRangeGroup{}"}),
    "body": ((), {"content": "structural[]"}),
    "structural": (("startIndex", "endIndex"),
                   {"paragraph": "paragraph", "table": "table",
                    "tableOfContents": "toc"}),
    # A table of contents is kept whole and frozen, span and all: what it generates is
    # deliberately opaque, not overlooked.
    "toc": (("content",), {}),
    "paragraph": ((), {"elements": "element[]", "paragraphStyle": "paragraphStyle",
                       "bullet": "bullet"}),
    "paragraphStyle": (("namedStyleType", "alignment", "indentStart", "indentFirstLine",
                        "lineSpacing", "spaceAbove", "spaceBelow", "shading",
                        "pageBreakBefore", "keepWithNext") + tuple(BORDER_SIDES.values()),
                       {}),
    "bullet": (("listId", "nestingLevel"), {}),
    "element": (("startIndex", "endIndex"),
                {"textRun": "textRun", "dateElement": "dateElement", "person": "person",
                 "richLink": "richLink", "footnoteReference": "footnoteReference",
                 "equation": "equation", "inlineObjectElement": "inlineObjectElement",
                 "horizontalRule": "horizontalRule"}),
    "textRun": (("content",), {"textStyle": "textStyle"}),
    "textStyle": (("bold", "italic", "underline", "strikethrough", "smallCaps",
                   "baselineOffset", "weightedFontFamily", "fontSize",
                   "foregroundColor", "backgroundColor", "link"), {}),
    "table": (("rows", "columns"), {"tableRows": "tableRow[]"}),
    "tableRow": (("startIndex", "endIndex"), {"tableCells": "tableCell[]"}),
    "tableCell": (("startIndex", "endIndex"), {"content": "structural[]"}),
    # Chips: what identifies each one, and nothing about how it is drawn.
    "dateElement": ((), {"dateElementProperties": "dateProperties"}),
    "dateProperties": (("displayText", "timestamp", "dateFormat", "locale"), {}),
    "person": ((), {"personProperties": "personProperties"}),
    "personProperties": (("name", "email"), {}),
    "richLink": ((), {"richLinkProperties": "richLinkProperties"}),
    "richLinkProperties": (("title", "uri", "mimeType"), {}),
    "footnoteReference": (("footnoteNumber", "footnoteId"), {}),
    # An equation's LaTeX is in no field at all — it comes from the Markdown export
    # (`equation_spots`, `latex_of`) — so there is nothing here to read.
    "equation": ((), {}),
    "inlineObjectElement": (("inlineObjectId",), {}),
    "horizontalRule": ((), {}),
    "inlineObject": (("objectId",), {"inlineObjectProperties": "objectProperties"}),
    "objectProperties": ((), {"embeddedObject": "embeddedObject"}),
    "embeddedObject": (("title", "description"),
                       {"size": "size", "imageProperties": "imageProperties"}),
    "size": ((), {"width": "dimension", "height": "dimension"}),
    "dimension": (("magnitude", "unit"), {}),
    "imageProperties": (("contentUri",), {}),
    "lists": ((), {"listProperties": "listProperties"}),
    "listProperties": ((), {"nestingLevels": "nestingLevel[]"}),
    "nestingLevel": (("glyphSymbol", "glyphType"), {}),
    "namedStyles": ((), {"styles": "namedStyle[]"}),
    "namedStyle": (("namedStyleType",), {"textStyle": "namedTextStyle",
                                         "paragraphStyle": "namedParagraphStyle"}),
    "namedTextStyle": (("weightedFontFamily", "fontSize")
                       + tuple(api for _, api in MARK_FIELDS), {}),
    "namedParagraphStyle": (("alignment", "indentStart", "indentFirstLine", "lineSpacing",
                             "spaceAbove", "spaceBelow", "shading", "pageBreakBefore",
                             "keepWithNext") + tuple(BORDER_SIDES.values()), {}),
    "namedRangeGroup": (("name",), {"namedRanges": "namedRange[]"}),
    "namedRange": (("namedRangeId", "name"), {"ranges": "range[]"}),
    "range": (("startIndex", "endIndex", "segmentId", "tabId"), {}),
}


def unmodelled(doc: dict) -> dict[str, dict]:
    """Everything a live document carries that this module never reads.

    A `documents.get` answer, walked against `_NODES`; the answer is, by the path
    each was found at, how many there were and one example. It is what a canonical
    file cannot say and therefore what a sync would drop if it ever rewrote the
    block holding it — the report `adopt` owes whoever hands us a document somebody
    else made, and the test that pins it is how a property Docs adds later shows up
    as a failure rather than as silence.

    A path reads like `structural.paragraph.paragraphStyle.borderLeft`. Empty values
    say nothing and are left out, so a document that sets none of a struct's fields
    does not report the struct.
    """
    found: dict[str, dict] = {}
    _walk(doc, "document", "", found)
    return dict(sorted(found.items()))


def _walk(value, node: str, path: str, found: dict) -> None:
    read, into = _NODES[node]
    for key, inner in (value or {}).items():
        here = f"{path}.{key}".lstrip(".")
        if key in read or _nothing(inner):
            continue
        if key in into:
            target = into[key]
            # A list or a map starts the path again at its node's name, so the
            # structural elements of a table cell and those of the body report at one
            # path and the recursion does not make the answer infinite.
            if target.endswith("[]"):
                for item in inner:
                    _walk(item, target[:-2], target[:-2], found)
            elif target.endswith("{}"):
                for item in inner.values():
                    _walk(item, target[:-2], target[:-2], found)
            else:
                _walk(inner, target, here, found)
            continue
        entry = found.setdefault(here, {"count": 0, "example": _example(inner)})
        entry["count"] += 1


def unmodelled_in(element: dict) -> dict[str, dict]:
    """`unmodelled` for one structural element: what a rewrite of *this block* drops.

    The document-wide answer is a count with no address, which is the right thing to
    print once and the wrong thing to act on. A property no node of `_NODES` names
    survives an ordinary edit — a request names the fields it writes — and goes when
    the block holding it is written again from nothing, so the question a person
    actually has is not "does this document carry borders" but "is the paragraph I am
    about to rewrite the one with the border on it".
    """
    found: dict[str, dict] = {}
    _walk(element, "structural", "structural", found)
    return dict(sorted(found.items()))


def unread_blocks(doc: dict) -> list[dict]:
    """Every block of every tab that carries something `unmodelled_in` names.

    The most heavily laden first, each with its tab, its span in that tab, its first
    words and what it carries. It is a *risk*, not a loss: a block nobody rewrites
    keeps all of it.
    """
    out: list[dict] = []
    for tab in tabs_of(doc) or [None]:
        tab_id = tab.get("tabProperties", {}).get("tabId") if tab else None
        body, _ = _body_of(doc, tab_id)
        for element in body:
            found = unmodelled_in(element)
            block = _block_of(element, {}, {}, {})
            # A section break is not a block: no rewrite can reach it, and naming one
            # for it would send a person to a paragraph that is not the one.
            if not found or block is None:
                continue
            out.append({"tab": tab_id,
                        "span": [element.get("startIndex", 0), element.get("endIndex", 0)],
                        "kind": block["kind"],
                        "words": _first_words(block).strip(),
                        "unread": found})
    out.sort(key=lambda e: (-sum(v["count"] for v in e["unread"].values()), e["span"]))
    return out


def _nothing(value) -> bool:
    """A value that says nothing at all: Docs leaves plenty of empty structs about."""
    return value is None or value == {} or value == [] or value is False


def _example(value) -> str:
    """One example of an unmodelled value, short enough to print in a report."""
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(value)[:4]) + "}"
    if isinstance(value, list):
        return f"[{len(value)}]"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."

