# HTML ↔ Google Docs: a measured feasibility study

Can the sync/pull machinery built for beamer → Slides be pointed at a second target:
an **HTML file as the canonical document** in git, a **Google Doc** as the editable
copy, edits flowing both ways?

**Yes, and it is a smaller problem than the Slides pipeline.** Everything below the
first section was measured against the live API on 2026-09-17, not read off a blog.
The probe is `tools/probe_docs_html.py`; its output is in `out/docs-probe/`.

Why it is smaller: Docs is a *flow* model. There are no boxes, no baselines, no font
metrics, no backgrounds, no per-slide rasterisation. The whole of `extract`, `render`,
`fidelity`, `calibration`, `tools/alignment.py`, the hole measurement and the background
painting simply has no counterpart. What is left is the part this repo is already good
at: a canonical source, a read-back IR, a three-way merge and a converge loop.

## Why the web is no use here

Google publishes **no specification of the HTML importer** — not which elements it
honours, not which CSS. Two independent research passes found only folklore, much of it
SEO slop, and it is *wrong* on the points that matter. Claims refuted by measurement:

| Folklore | Measured |
|---|---|
| "base64 `data:` images are stripped" | both data-URI images imported; bytes come back **identical** in the zip export |
| "CSS classes and inline styles are ignored" | inline `style=` works for character *and* paragraph properties |
| "only inline CSS works, `<style>` blocks don't" | `<style>` works, including `.class`, `p#id` and `div.wrap p` |
| "remote `<img src>` needs your own public host" | a plain `https://` image was fetched and embedded on import |
| "`page-break-before` renders as a horizontal line" | it is dropped silently, in all four spellings |

Measure, don't read. `about.get(fields=importFormats,exportFormats,maxImportSizes)` is
the only authoritative source for the format lists, and it says: `text/html`,
`text/markdown`, docx, odt, rtf all import to a Doc; **`application/zip` is export only**
(so no HTML+images bundle); and the import cap is **10 MiB**, not the 50 MB the help
centre advertises.

## What survives HTML → Doc

Measured with a 76-case stress file plus a 30-case feature file.

**Kept.** `h1`–`h6` → real Docs heading styles (and an explicit inline `font-size` /
`font-family` / `color` on the heading overrides the style); `p`, `br`, `hr`;
`b`/`strong`, `i`/`em`, `u`, `s`/`del`, `sup`, `sub`.
Character CSS: `color`, `background-color`, `font-family`, `font-size`, `font-weight`,
`font-style`, `text-decoration` (underline, line-through), `vertical-align` (super/sub).
Paragraph CSS: `text-align` (incl. justify), `margin-left`, `text-indent`, `line-height`.
`<style>` blocks resolve tag, class, `tag.class`, `#id` and one-level descendant
(`div.wrap p`) selectors. Lists: `ul`/`ol` nested at least 3 deep, `list-style-type`
disc/circle/square/lower-roman/upper-alpha, `ol start="7"`, `ol type="a"` — **including
the start number, which the Slides API cannot set at all**. Tables: `colspan`, `rowspan`,
per-cell `background-color`, per-cell `text-align`, per-side borders. Links: external,
`mailto:` and internal (retargeted to a minted Docs bookmark). Images: `data:` URIs and
remote `https://`, deduplicated by content. Entities and Unicode, including `&nbsp;` and
`&shy;`. `<pre>` → one Courier paragraph with `<br>`s and leading spaces as `&nbsp;`.

**Dropped.** `page-break-before`/`-after` in every spelling — page breaks are reachable
only through the Docs API's `insertPageBreak`. `letter-spacing`, `text-transform`,
`rgba()` alpha, `white-space:pre` (the CSS; the `<pre>` *element* is fine), tabs (`&#9;`
→ space), paragraph `border` and `padding` (padding degrades to `margin-left`).
Structure: `dl`/`dt`/`dd` collapse into one paragraph, **nested tables are flattened**,
`blockquote` becomes a plain paragraph, `mark`/`small`/`ins`/`code`/`kbd`/`abbr`/`q`
lose all styling, `<input type="checkbox">` vanishes. Layout: flex, float, position,
columns. Selectors: attribute selectors, `@media`, and two-level class descendants
(`.outer .inner`) — so stay inside the subset above.

**Normalised, which is worse than dropped because it is silent.**
- `font-family` with a fallback list is *mangled*: `Georgia, serif` → **`Geo`**,
  `'Courier New', monospace` → **`Courier`**. Emit exactly one family name, unquoted,
  no fallback. Single names survive verbatim, even `Comic Sans MS`.
- `font-size` is rounded to integer pt (7.5pt → 7pt).
- Table column widths: the CSS `width:` on a `<col>` is discarded and the columns come
  back equal — but the **HTML `width=` attribute works** (`<col width="120">` → 90 pt,
  px × 0.75), so widths are reachable, just not the way you would first spell them.
  `border-style:dashed` → solid, `2px` → `1.5pt`.
- HTML `id` attributes are not preserved; Docs mints its own `id.xxxxxxxx` bookmarks.
  **An `id` is therefore not a usable anchor.**

## How far the dialect reaches into Docs' own model

The two sections above graded the HTML *export*, which only shows what survives a second
lossy conversion. `tools/probe_docs_features.py` instead reads the imported document
back with `documents.get`, so it reports what Docs actually built. That turns up
capabilities the export had hidden — several of them things you would not guess.

**Reachable from HTML, beyond the obvious subset:**

| Docs feature | the HTML spelling that reaches it |
|---|---|
| paragraph alignment | `text-align`, and also legacy `<center>` and `align="right"` |
| character colour / size / family | `style=`, and also legacy `<font color size face>` (`size="6"` → 24 pt) |
| right-to-left paragraphs | `dir="rtl"` → `paragraphStyle.direction: RIGHT_TO_LEFT` |
| **pinned table header row** | `<thead>` → `tableRowStyle.tableHeader: true` |
| **table column widths** | the `width=` *attribute* on `<col>`/`<td>` (px × 0.75 → pt) |
| **cell vertical alignment** | `vertical-align` on a `<td>` → `contentAlignment: MIDDLE`/`BOTTOM` |
| image alt text | `alt=` → `embeddedObject.description` — the only per-image handle |
| **internal cross-references** | `<a id="x"></a>` inside a heading plus `<a href="#x">` → a real `link.bookmarkId`. Docs mints its own id, but the *relationship* survives |
| **page background colour** | `<body style="background-color:…">` → `documentStyle.background`. The only document-level property HTML can set |
| a link over a whole block | `<a>` wrapping a `<p>` styles the paragraph's runs |

**Page setup is not reachable from HTML at all.** `@page { size: A4 landscape; margin:
2cm }` does nothing; neither does an explicit `size: 842pt 595pt`; and neither does the
Word dialect (`@page WordSection1` with `div.WordSection1 { page: WordSection1 }` and
`mso-*` properties) — which was the most promising lead, since Google's importer must eat
Word-generated HTML. All three leave Letter 612 × 792 with 72 pt margins.

**Traps — these fail quietly and are the ones that will cost debugging time:**
- `text-decoration: underline line-through` applies **only the underline**. Use one
  declaration, or nest `<u><s>`.
- `text-decoration: underline wavy #cc0000` applies **nothing at all** — the extra
  keywords kill the whole declaration, so you lose even the plain underline.
- `font-weight: 600` becomes plain `bold: true`, with the weight recorded as 400. Docs
  supports real weights on `weightedFontFamily`, but only through `batchUpdate`.
- `background-color` on a `<p>` becomes a **character highlight on its runs**, not
  `paragraphStyle.shading`.
- `<header>`, `<footer>`, `<figure>`, `<details>`, `<section>`, `<aside>` all unwrap —
  and **consecutive ones merge into a single paragraph**. Content is kept, block
  boundaries are not. That is the sharpest hazard for a canonical file.
- `<li value="7">` is ignored, although `<ol start="7">` works. `<ol reversed>`,
  `list-style-image` and CSS counters are all ignored.
- `class="title"` / `class="subtitle"` do **not** reach the TITLE/SUBTITLE named styles.

**Dropped with no Docs concept behind them:** inline `<svg>` and SVG data URIs (only
PNG/JPEG/GIF import), `<iframe>`, MathML and `<ruby>` (both flattened to their text),
`<big>`/`<tt>`, `letter-spacing`, `word-spacing`, `text-transform`, `writing-mode`,
arbitrary `vertical-align` offsets, `<abbr>`/`<time>`/`<data>` attributes, link
`target`/`rel`/`title`, `lang`, and `<title>` as the document's name.

## The phase-two surface: what `batchUpdate` adds

Everything HTML cannot express, tried one request at a time against the imported
document. **All of these work:**

`updateDocumentStyle` (page size, margins, background) · `updateTextStyle` with
`smallCaps` and a real `weightedFontFamily` weight · `updateParagraphStyle` with
`shading`, `borderLeft`, `keepWithNext`, `keepLinesTogether`, `avoidWidowAndOrphan` ·
`createHeader` · `createFooter` · `createFootnote` · `insertPageBreak` ·
`insertSectionBreak` · `createParagraphBullets` with `BULLET_CHECKBOX`.

**Refused — not in the public v1 discovery document:** `insertDate`, `insertPerson`,
`insertRichLink`, `insertTableOfContents`. So smart chips (date, people, file) and a
table of contents can be *read* from a document a human made, but not *created*. Treat
them as read-only content to preserve, never to generate.

That is a clean division of labour, and it settles the emit design: import the canonical
HTML for structure and the bulk of the styling, then one `batchUpdate` for page setup,
headers/footers, footnotes, page and section breaks, checklists, small caps and
paragraph decoration — and the named-range anchors, which go in the same batch.

## Reading what we cannot write: chips and the rest

`tools/probe_docs_chips.py` makes a document with one labelled line per Docs-native
object, which a human (here, browser automation driving the real editor) then fills in.
Everything below was inserted by hand and read back three ways. **How well an object
reads back has nothing to do with how exotic it looks**, and the API and the exports
disagree in both directions.

| object in the editor | `documents.get` | HTML export | Markdown export |
|---|---|---|---|
| date chip | **`dateElement`**: `timestamp` (ISO 8601 UTC), `locale`, `dateFormat`, `timeFormat`, `displayText` | plain `<span>Sep 25, 2026</span>` | plain text |
| person chip | **`person`**: `personId`, `email`, `name` | `<a href="mailto:…">Name</a>` | `[Name](mailto:…)` |
| file chip | **`richLink`**: `uri`, `title`, `mimeType` | `<a href="google.com/url?q=…">` (wrapped) | `[title](uri)` |
| place (Maps) chip | **`richLink`**, same shape, no `mimeType` | wrapped `<a>` | `[title](uri)` |
| footnote | `footnoteReference` + a `footnotes` map | `<sup><a href="#ftnt1">` + a div | `[^1]` |
| equation | **`equation {}` — empty** | `<img src="data:image/png;base64,…">` | **`${x}^{2\ +\ \sqrt{y}}$`** |
| dropdown chip | **an element with only `startIndex`/`endIndex`** | `<span>Not Started</span>` | plain text |
| placeholder chip | **`` inside an ordinary `textRun`** | `<span>Person</span>` | plain text |
| watermark | **nothing** but a `` in the default header | absent | absent |
| comment | **nothing** (comments are a Drive API concept) | `<sup><a href="#cmnt1">[a]</a></sup>` + its text at the end | absent |
| table of contents | `tableOfContents.content`, with `link.heading` per entry | generated `<p>`s | plain text |
| emoji | an ordinary character in a `textRun` | `&#128640;` | the character |

`dateElement` is the headline: it is **not** in the reference's `ParagraphElement` union,
yet it carries the chip's full semantics — the underlying instant, not just the rendered
words. A date chip can therefore be round-tripped exactly, even though nothing in the API
can create one.

Four traps follow:

* **`equation {}` is empty.** Thirteen index units wide, no content, no id. The formula
  exists only in the Markdown export (as LaTeX) or the HTML export (as a base64 PNG). So
  **the Markdown export carries information `documents.get` does not**, and an IR that
  wants to keep equations has to read both.
* **A dropdown chip is an anonymous element** — one index unit, no content key of any
  kind. You can see *that* something is there and nothing else. Its displayed value shows
  up only in an export.
* **``** is Docs' "an object sits here" sentinel inside plain text — a placeholder
  chip, and the watermark inside the default header, read as that one private-use
  character. `checks.junk_text` already flags private-use characters in the Slides
  pipeline; here they are load-bearing and must not be stripped.
* **Comments are invisible to the Docs API** and visible in the HTML export. Reading them
  properly means `drive.comments.list`, which `drive.file` already covers.

### Document tabs

Tabs are the one structural feature that changes the shape of a read, and the default is
silent data loss:

* `documents.get` **without** `includeTabsContent` returns only the first tab's `body`,
  with no `tabs` key and no sign that the others exist.
* **with** the flag, `body` disappears entirely and `tabs` takes its place — a list of
  `{tabProperties: {tabId, title, index, parentTabId}, documentTab: {body, …}, childTabs}`,
  nested one level deep in the probe (`Tab 2` → `Tab 3`).
* Drive's html / txt / md exports concatenate **every** tab, with no marker between them.

So the reader must always pass `includeTabsContent=True` and handle the `tabs` shape, and
every write has to name its tab. Anchors, named ranges and index arithmetic are per tab.

## The round trip does not close on its own

Feeding Google's own HTML export back into Google's HTML importer **destroys every
list**: the export writes `<ul class="lst-kix_…" style="list-style-type:none">` with the
bullet glyph only in a CSS `:before` rule, and the importer throws the `<li>` away and
keeps the `margin-left`, leaving flat indented paragraphs. Measured: `ul 5 → 0`,
`ol 4 → 0`, `li 14 → 0`, `p 50 → 67`. A second pass changes nothing more — it settles,
at a degraded fixed point.

So: **the HTML export is a lossy read-back, never a source file.** This is not fatal; it
is the same shape as the existing pull loop, which never stores the read-back either. To
check that, a throwaway ~60-line normaliser was put in the loop — export → recover list
nesting from the `lst-kix` CSS → re-import. The result **reached a byte-identical fixed
point in one step** (54 871 bytes on both the second and third pass, identical
structure). The residual differences against the original were entirely defects of that
60-line script (it flattened `<ol>` to `<ul>` and mangled a `thead`), i.e. exactly the
translator bugs a converge loop exists to iterate away.

The conclusion is the honest one: **the loop converges, and the fixed point is only as
good as the translator.** That is the same bargain `inverse.py` already makes.

## Proposed shape

Names are provisional; the point is the correspondence with what exists.

| Slides pipeline | Docs pipeline |
|---|---|
| `.tex` → pdflatex → PDF | `doc.html` (a restricted dialect), no compile step |
| `extract` + `classify` + `render` | *nothing* — HTML is already structured |
| `emit` → `.pptx` → Drive conversion | `doc.html` → Drive conversion, then one `batchUpdate` for what HTML can't carry |
| `snapshot.read_presentation` | `documents.get` (**not** the HTML export) |
| `deck_ir.py` | `doc_ir.py`: Docs JSON → the same IR the canonical HTML parses to |
| alt-text titles `b2s:<slide>/<element>` | **named ranges** `b2s:<block>` |
| `identity.py`, `merge.py` | reused nearly as-is |
| `sync.py` (`requiredRevisionId`) | same, plus Docs' `targetRevisionId` rebasing |
| `inverse.py` converge loop | same skeleton, far simpler translators |

Three things make this genuinely easier than the LaTeX side:

1. **No compile.** `inverse.py` spends most of its complexity on SyncTeX page→frame
   mapping, rerun detection, and repairing a source that stopped building. Editing a
   node in an HTML tree has none of that.
2. **No geometry.** Every `geometry` residual, the `\vspace` secant solver, the
   `RESHAPING` deferral — all gone.
3. **Real anchors.** Named ranges are created at an arbitrary text range, are invisible
   in the UI, and Google keeps their indices up to date as the user types. They are a
   strictly better version of the alt-text tag trick. Caveats, all documented: names are
   not unique (ids are), a range can be *split* into several by user edits
   (`NamedRange.ranges[]` is a list), and content the user copies does **not** carry the
   range — so a duplicated section arrives as one anchored and one anonymous copy, which
   `merge.plan_unit` must treat as a user object.

What HTML cannot express and a follow-up `batchUpdate` must therefore write: page
breaks, exact table column widths, headers/footers, footnotes, and the named ranges
themselves. That is a small, well-defined second phase, and it mirrors the existing
"phase 1 duplicate, phase 2 style" split in `emit`.

Reuse estimate: `identity.py` and `merge.py` are already target-agnostic apart from
three Slides-specific spots (`merge.predicted_text`, `IR_STYLE_TO_API`,
`text_edit_requests`); `compare.py` is pure IR-vs-IR. `snapshot.py`, `sync.py` and
`deck_ir.py` need Docs twins of the same shape. `extract`, `classify`, `render`,
`emit`, `fidelity`, `pdf`, `fonts`, `calibration` are not needed at all.

## Anchors: measured

The Docs API is now enabled on project `beamer2slides`, and named ranges hold up.
`tools/probe_docs_api.py` measures the behaviour Google leaves undocumented; results in
`out/docs-probe/anchors.json`.

| Question | Measured |
|---|---|
| Scope needed | **`drive.file` is enough** for `documents.get` on a doc this app created — we never ask for `documents`, which would reach every Doc the user owns |
| Text typed inside an anchor | the range grows to contain it |
| Text typed at an anchor's first index | falls *outside*: ranges are half-open `[start, end)`, so an anchor can't swallow what precedes it |
| User deletes all the anchored text | **the range disappears** — a missing anchor means deleted content, which is exactly the signal `merge.plan_unit` wants |
| Drive copy of the document | keeps every anchor |
| Count cap | none found; 403 ranges planted on a 4-paragraph document without complaint |
| Stale `requiredRevisionId` | rejected with 400 — the `sync.py` plan / send / re-plan pattern ports directly |
| Paragraph break inside an anchor | did **not** split it (one range, grown by one). Google documents splitting as possible, so still treat `NamedRange.ranges` as the list it is — but it is rarer than the docs imply |

### Under a human editor

The table above went through the API. The Docs editor is a different code path, so the
same anchors were driven by hand in Chrome (`tools/probe_docs_ui.py create`, edit, then
`read`). Every result matched the API path, and two went further:

| Edit made in the editor | Anchor |
|---|---|
| typing inside an anchored paragraph | grows to contain it, as through the API |
| pasting a copy of an anchored paragraph | the copy is **anonymous** — the anchor stays on the original. This is Google's documented "copied content does not carry the range", confirmed in the real editor |
| deleting all the anchored text | disappears, as through the API |
| **Ctrl+Z after that delete** | **comes back, at its exact original span.** Undo restores named ranges, not just text — an accidental delete cannot silently orphan an anchor |
| a *pending* suggested deletion (Suggesting mode) | survives, and `documents.get` **at its default view mode does not show the suggestion at all** — the document reads as if every pending suggestion had been rejected |

That last row matters for the merge: a reviewer's un-accepted suggestions cannot be
mistaken for committed edits. To see them deliberately, read with
`suggestionsViewMode=SUGGESTIONS_INLINE` (the ids sit on the `textRun`, not on the
`ParagraphElement` around it); `PREVIEW_SUGGESTIONS_ACCEPTED` shows the document as it
would be if accepted, and in that view the anchor on a suggested-for-deletion paragraph
is already gone.

**The one bad result, and it shapes the architecture:** a `files.update` rebuild in
place **destroys every named range** (23 before, 0 after). So the trick the Slides
pipeline uses for `convert` — re-upload and let Drive re-convert, keeping the URL — is
unavailable once a document is anchored and being edited. The push path splits exactly
as it already does on the Slides side:

- **first publish** — import the canonical HTML, then plant anchors in one `batchUpdate`;
- **every later push** — incremental `batchUpdate` edits computed by the merge, never a
  re-import. That is what `sync.py` already does; only `convert` ever rebuilds wholesale.

## The loop, end to end: measured on a live document

`src/beamer2slides/doc_ir.py` (IR ↔ canonical HTML ↔ `documents.get`), `doc_merge.py`
(three-way merge → `batchUpdate`) and `doc_sync.py` (the commands, the state and the
report) are written, and `beamer2slides docs push|sync` drives them. The loop proved out
in full:

1. `push` converts the canonical HTML through Drive, reads the result back, gives it the
   file's keys and plants one named range per block;
2. a human edits in the browser — a word rewritten, a **person chip** inserted, a new
   paragraph typed, a numbered list made with the toolbar;
3. the source file is edited at the same time — a heading reworded, a word changed inside
   a bold run, a word changed in a paragraph the document also edited, a block added, a
   block deleted;
4. `sync` merges the three sides, sends the edits with `requiredRevisionId`, then
   **regenerates the canonical file from the document it just wrote**;
5. `sync` again writes **0 requests**. File and document say the same thing.

The chip survived every pass, the file gained it (`<span class="b2s-chip"
data-chip="person" data-value="…">`), and the block whose text the source would have
rewritten around it was left alone and reported. Four traps were found by running it,
all of them now fixed and pinned by offline tests:

| Trap | What happens | What the code does |
|---|---|---|
| **An imported list cannot say whether it is numbered** | every nesting level of a list Drive's HTML importer built reads back `glyphType: GLYPH_TYPE_UNSPECIFIED`, with no `glyphFormat` and no `glyphSymbol` — identically for `<ul>` and `<ol>`, though the editor renders them differently. A list the *editor* makes reads properly (`DECIMAL`/`%0.`, `ALPHA`, `ROMAN`, or a `glyphSymbol`) | `doc_ir._ordered` answers `None`, not `False`, and `doc_merge.restore_unreadable` takes the answer from the canonical file. The mirror image of a frozen run: the document wins on what the file cannot carry, the file wins on what the document cannot report. Block identity (`_match_shape`) ignores ordered-ness entirely, or every list item would lose its key on the first read-back |
| **Inserted text inherits the style in front of it** | replacing a word at the start of a bold run or a link by deleting and then inserting at the hunk's start gives the new word the style of whatever preceded the hunk — the word falls out of the run | insert at the hunk's **end** first (inheriting from the last character it replaces), delete afterwards. `canonical` → `canonic` stays bold; without it, it does not |
| **Text inserted at a list item's start joins the list** | a new paragraph written at the index where a bulleted paragraph begins comes out as another bullet of that list | every non-item block is written with `deleteParagraphBullets` before its paragraph style |
| **An insert and an edit at the same index** | a block inserted at index *i* pushes the block that starts at *i* down the document, so that block's own edits — planned against the read — land inside the new text | back-to-front ordering breaks the tie the other way: at one index, a block's deletes and edits go before the insert that displaces it |
| **The body's last newline cannot be written past** | a block appended after the document's last paragraph has no following block to insert before. Writing `text\n` at the last paragraph mark puts the words *inside* that paragraph and leaves the empty one it was meant to end | an append writes `\ntext` instead — the break first, the words after it — and it goes in *before* that paragraph's own edits, which end at the very index it was planned at |
| **Two blocks added at one index come out backwards** | both insert at the same place, and what is written last ends up in front | the added blocks are planned back to front too, so the later one is written first |
| **The newline in front of a table cannot be deleted** | one of the deletes the API refuses outright ("Invalid deletion range"), and a refused request throws out the whole batch — so a source that deletes or moves the paragraph directly above a table used to kill the sync | that block gives up the paragraph mark of the block *before* it instead of its own (`doc_merge._delete_range`). Docs merges the two the way the Delete key does, keeping the first one's style — measured — and the table still has a paragraph in front of it. A run of deleted blocks passes the borrowing leftwards so no two ranges ask for the same mark; with nothing in front to borrow from (the document's first block, or a paragraph between two tables) the words go and the empty paragraph stays, which is what Docs wants between two tables anyway |

One limitation stands, and it does not block the design: a list the human switches from
bullets to numbers **cannot be seen** (the same measurement as the first row), so the
file keeps saying what it said. What the merge reports instead of writing is listed
under "What the merge refuses to write" below.

## The commands

```
python -m beamer2slides docs push doc.html [--name "In Drive"] [--new-doc]
python -m beamer2slides docs sync doc.html [--doc <url|id>] [--dry-run]
                                           [--assume-base file|document]
```

- **`push`** imports the file through Drive, reads the document back, gives it the
  file's keys, plants one named range per block, and rewrites the file with those keys
  and a `<meta name="b2s-document">` naming the document. From then on the file alone
  says where it lives, and a push is never repeated: `--new-doc` is the only way to get
  a second document out of one file.
- **`sync`** merges base, file and document three ways, sends the edits with
  `requiredRevisionId`, and **regenerates the file from the document it just wrote**.
  A refusal on the revision — somebody typed between the read and the write — re-reads
  and re-plans, three attempts (`B2S_DOCS_BEFORE_WRITE` runs a command right before the
  first write, which is how that path is tested).

State lives beside the file, in `.b2s/`: `<stem>.base.json` is what both sides agreed
on at the end of the last sync, and `<stem>.sync-report.{json,md}` says what each side
contributed, what conflicted (the document wins) and what was left alone. Without a
base there is no way to tell a source change from a document change, and `sync` says so
rather than guessing; `--assume-base file` then treats every difference as the
document's (nothing is written, the file is rewritten from the document) and
`--assume-base document` treats every difference as the source's.

### Order: a section the source moved

The merged list follows the **document's** order — a reader who moved a paragraph in the
browser keeps it where they put it. On top of that, the blocks the *source* moved are
put back where the file has them: the common keys' order is compared base-to-file with
`SequenceMatcher`, and only the complement of the longest common subsequence moves, so a
file that moved one section writes one block, not the whole document. If the document
reordered anything too, the document's order stands whole and the report says so — two
orders cannot both be right, and the document is the side that wins.

The API has no move. A move is therefore a **delete where the document has the block and
a write where the file puts it**, which has two consequences:

- the text written is the *merged* text, so a word the reader changed in a block the
  source moved rides along with it, and so does its styling (it is rebuilt from the
  merged runs);
- a block that cannot be written from nothing — one holding a chip or an equation, or a
  table — is **not** moved; it stays where the document has it and the report says why.

The delete takes the block's named range with it, so the read-back afterwards has no key
for the block that moved. `doc_merge.adopt_keys` hands the plan's key back to it before
the file is regenerated (`doc_sync.settle`), or a paragraph nobody touched would be
renamed in the next diff — and a block the source added with an `id=` of its own would
lose the name its author chose.

### A grid is not text: the structural pass

`insertTable`, `insertTableRow`, `insertTableColumn` and their deletes are the only way
to change a table's shape, and none of them belongs in the batch that writes the words:
every index below a grid that changes moves, and the cells a new table is made of do not
exist until the request has been sent. So a sync that needs one of them sends **them
first, on their own**, reads the document again, and plans the text against the grid it
then has (`doc_merge.structure`, `doc_sync._write_structure`). Three things have to
happen in between:

- **the new table gets its key.** A table `insertTable` built carries no named range,
  and a read cannot tell it from one a reader made in the browser, so it is found by
  what it follows — the block the plan put it after, which the document already had
  (`doc_merge.anchor_tables`) — given the file's key and anchored. Without that the next
  plan would not recognise it and would build it a second time.
- **the base takes the new grid, and only the grid.** That grid is no longer a
  difference between the sides — this sync gave it to the document on the source's
  behalf — so the base says it too, with the cells that were there keeping their words
  and the ones just made empty (`doc_merge.rebase_tables`). Taking the table as the
  document *reports* it would swallow into the base whatever a reader had typed in it,
  and the next plan would read those words as words the source had taken away. That is
  exactly what the live test caught.
- **rows and columns are matched by their words, and only the count is written.** A row
  whose words the source changed is a text edit, and text is merged afterwards cell by
  cell, so a stretch that differs on both sides adds or removes at its end and leaves
  the rest alone (`doc_merge._line_ops`). A row deleted and built again would throw away
  whatever the document had put in it.

What `insertTable` does exactly, measured: it **splits the paragraph its index is in** —
what was before the index stays a paragraph, then comes the table, then the rest — and
the index must be *inside a paragraph*, which a table's own start index is not. So a
table written in front of an ordinary block goes at that block's start and the empty
paragraph it leaves in front of itself is swallowed by deleting the mark of the block
before it; a table written in front of another table goes at the mark of the paragraph
before it, and the empty half lands *between* the two tables, where Docs wants a
paragraph anyway; and a table written after everything goes to the end of the segment,
where Docs keeps a paragraph after it, because a document ends on one.

One dimension at a time: when rows and columns both changed there is nothing left to
match rows on (every row is a column longer), and that, a cell holding two paragraphs,
and a row out of step with the others are all reported instead — which is what every
grid change got before.

### What the merge refuses to write

Each of these is reported in the sync report, never guessed at:

| Case | Why |
|---|---|
| a block whose frozen runs differ between the sides | a chip, an equation, a dropdown or a table of contents cannot be created by any write, so the text around one is left alone rather than rewritten without it |
| a table whose **grid both sides changed** | the document's grid stands, as it does everywhere else. A grid only the source changed is written — see the structural pass above |
| a grid change that is **not whole rows or columns** | rows and columns both changed at once, a cell holding two paragraphs, a row out of step with the others: cells merge by their place, and there would be nothing to match them on |
| a source restyle of words the document rewrote | the marks would have to be matched onto words that are no longer there |
| a **move of a block with a chip, an equation or a table in it** | a move is a delete and a write, and those cannot be written from nothing — the block stays where the document has it |
| a **reorder both sides made** | the document's order stands whole; the file's is reported |

Everything else is written: text on both sides, a block's kind, level and alignment,
its bullets, the marks the source added *or took away* (the fields Docs needs named for
a removal are listed in `doc_merge.MANAGED`), and whole new blocks with their styling.

## Remaining risks

1. **Images on the sync path.** Import via HTML is fine (measured, lossless). But
   `insertInlineImage` takes a **URI only** — no byte upload, same wall as Slides'
   `createImage` — so inserting an image into an *existing* doc needs the staging-file
   trick `sync.stage` already uses. Read-back `contentUri`s live ~30 minutes; the **zip
   export is the durable, byte-exact picture route**. Until then the dialect has no
   picture: an image *in the document* reads back as a frozen run and is never touched,
   and an `<img>` in the canonical file is **reported by `push` and `sync`**
   (`doc_sync.limits`) rather than dropped in silence.
2. **Lists in the read-back.** `listId` is opaque and output-only; whether Docs forks or
   reuses one when a user splits a list in the UI is undocumented. What an *imported*
   list's glyphs read as is no longer a risk but a measurement — see the table above.
3. **Anchors under a human editor** — retired, see "Under a human editor" above. What
   is still unmeasured there: dragging a selection to a new place, "paste without
   formatting", and a second person editing concurrently.
4. **Tabs on the write path.** They were read, never written: whether `batchUpdate` can
   address a tab other than the first, and what an HTML import does to a document that
   already has several, are both unmeasured. Until then, a multi-tab document is
   read-only beyond its first tab, and every sync of one says so in its report, naming
   the tabs it left alone.

## Alternatives considered

**docx instead of HTML** as the wire format (python-docx + Drive conversion, the exact
analogue of the existing `.pptx` route) carries images, page setup, footnotes and
section breaks, which HTML cannot express. But the only hard, reproducible evidence
found about Google's docx importer is that it mishandles OOXML that is spec-legal but
not Word-shaped. And it costs the thing the user actually asked for: a diffable text
artefact in git. **Recommendation: keep HTML canonical, and reach for docx only if page
setup or footnotes become requirements** — it is a swap of the emit stage, not of the
architecture.

**Markdown** is now importable *and* editable natively (a `.md` in Drive opens in Docs
without converting), which would remove the round trip entirely. But its subset drops
colours, highlights and alignment, and the Markdown export proved unstable across
documents in the probe. Too lossy to be canonical here — but it is the **only** place an
equation's LaTeX survives a read, so the reader should fetch it as a side-channel even
though the canonical file stays HTML.

## Reproducing the measurements

```
.venv\Scripts\python.exe tools\probe_docs_html.py
```

Uploads the stress file, exports it in all eight formats, re-imports the export twice to
test stability, and deletes the Drive files again (`--keep` to inspect them).

```
.venv\Scripts\python.exe tools\probe_docs_features.py
```

Imports the exotica file and reads it back with `documents.get`, printing what Docs
built per case; then tries every page-setup dialect in its own document, and every
`batchUpdate` request HTML cannot express, one at a time.

```
.venv\Scripts\python.exe tools\probe_docs_api.py
.venv\Scripts\python.exe tools\probe_docs_ui.py create   # then edit by hand, then `read`
```

The anchor probes: named-range behaviour through the API, and under a human editor.

```
.venv\Scripts\python.exe tools\probe_docs_chips.py create   # then insert the chips, then `read`
```

The chips probe: one labelled line per Docs-native object, read back through
`documents.get` (with and without tabs) and through every export that could carry it.
All five write to `out/docs-probe/`.

```
.venv\Scripts\python.exe -m pytest -m docs tests\test_docs_live.py
```

The whole loop on real documents, about 85 s: a push whose import reads back as what the
file said, both sides editing (a table cell each, a block added, a list item rewritten),
a block appended at the very end, a section the source moved to the end of the document
(the reader's words, the styling and the block's key all ride along — and the paragraph
it left behind stood in front of a table, which is where that delete was found), the same
words rewritten on both sides (the document wins, and the conflict is reported), a table
the source added *and* a row it added to another one while a reader was typing in that
same table, and a reader typing between the plan and the write (the sync reads again and
keeps their words). Every test ends by syncing once more and
finding **0 requests**, and each one deletes its document afterwards. Files stay in
`out/docs-tests/`. To drive one by hand instead:

```
.venv\Scripts\python.exe -m beamer2slides docs push out\docs-cli\doc.html
.venv\Scripts\python.exe -m beamer2slides docs sync out\docs-cli\doc.html   # after editing either side
```
