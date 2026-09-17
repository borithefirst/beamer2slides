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

## Remaining risks

1. **Images on the sync path.** Import via HTML is fine (measured, lossless). But
   `insertInlineImage` takes a **URI only** — no byte upload, same wall as Slides'
   `createImage` — so inserting an image into an *existing* doc needs the staging-file
   trick `sync.stage` already uses. Read-back `contentUri`s live ~30 minutes; the **zip
   export is the durable, byte-exact picture route**.
2. **Lists in the read-back.** `listId` is opaque and output-only; whether Docs forks or
   reuses one when a user splits a list in the UI is undocumented.
3. **Anchors under a human editor** — retired, see "Under a human editor" above. What
   is still unmeasured there: dragging a selection to a new place, "paste without
   formatting", and a second person editing concurrently.

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
documents in the probe. Too lossy to be canonical here.

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
All four write to `out/docs-probe/`.
