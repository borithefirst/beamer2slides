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

## What the canonical dialect says

The file is the source, so what it cannot spell cannot be authored. The dialect
therefore carries every styling property that a read can see and one of the two
write paths can create, and nothing else.

**Runs.** `<b>`, `<i>`, `<u>`, `<s>` and `<a>` as before, plus:

| IR key | in the file | how it reaches the document |
|---|---|---|
| `font` | `style="font-family:Consolas"` | the import (one name, unquoted, no fallback) |
| `fontsize` | `style="font-size:18pt"` | the import (rounded to whole points) |
| `smallcaps` | `data-smallcaps="1"` | `updateTextStyle` only |
| `script` | `<sup>` / `<sub>`, `data-script="none"` | the import, and `updateTextStyle` |

A superscript is **content**, not decoration: the 2 of x², of H₂O, of a footnote
marker or a citation. It was the last `TextStyle` field the dialect could not say, and
the cost was quiet — a block written again from nothing came back with every raised
character flat on the baseline, and nothing in the report said so, because a field
nobody reads is a field the merge never notices changing. It is not a mark, either:
`baselineOffset` is one of NONE / SUPERSCRIPT / SUBSCRIPT, so the run says *which*,
and "none" is a third value rather than the absence of the other two — a theme can
raise a whole named style, and a reader who puts one word back on the baseline has to
be able to say it (the `data-off` reasoning, one value wider). HTML has had the two
tags since the beginning, so the import carries them; the refusal has no tag, as
`<b>` has no opposite, so the file says `data-script="none"` and only a `batchUpdate`
writes it.

`<code>` used to stand for four monospaced families at once, which silently made
Consolas and Roboto Mono the same face — and the importer drops `<code>`'s styling
anyway, so the face only appeared after the next sync wrote it. A face is now carried
as the face it is, and arrives with the import. The tag is still written and still
read, and still means Courier New (`doc_ir.CODE_FAMILY`), so a file written before
this keeps working.

A size is kept as the document reports it, not rounded. The importer rounds at
`push`, but `push` settles by regenerating the file from the document it made, so the
file then says the whole point and nothing oscillates; every write after that is a
`batchUpdate`, which takes the size as given. Rounding in the reader would instead
move a 7.5 pt a reader chose in the editor.

**Paragraphs.** `text-align` as before, plus `margin-left` (`indentStart`),
`text-indent` (`indentFirstLine`) and `line-height` (`lineSpacing`) — all three kept
by the importer — and attributes of ours that only `batchUpdate` can write:
`data-shading`, `data-space-above`, `data-space-below`, `data-border-top` /
`-bottom` / `-left` / `-right`, `data-page-break` and `data-keep-with-next`. The
spelling matters: `background-color` on a `<p>` is a character highlight on its runs,
not paragraph shading, so the CSS would be a lie; and no margin property is among what
survives. A `<li>` carries the same, except its indents, which belong to the list
preset and would fight `createParagraphBullets`.

A rule is spelled `"<width>pt <solid|dotted|dashed> #rrggbb"` with an optional
` pad <n>pt` (`doc_ir.BORDER_RE`), and a rule of no width is no rule at all — that is
how a reader who took one off reads back, and how the source says to take one off. A
border is written as a **whole struct**, so the padding is always named even when it is
zero (`doc_merge._border_value`): inside a `paragraphBorder` an unset field is not the
API's "back to what you inherit", because the field being written is the border itself.
The two flags are booleans; `data-page-break="1"` is `pageBreakBefore`, which is how a
source says "this heading starts a page", and losing it silently was the shape of every
other unmodelled property — a horizontal rule under a heading is exactly the kind of
thing a person puts in and a rewrite takes away. `borderBetween` stays deliberately
unmodelled: it is a rule *between* consecutive paragraphs that share a style, and a
block model where one paragraph is one block, planned and written on its own, has
nowhere honest to put half of a property that belongs to a pair.

**Every named style Docs has is a kind of its own.** `NORMAL_TEXT` is a paragraph,
`HEADING_1..6` are headings of that level, and `TITLE` and `SUBTITLE` are `title` and
`subtitle` (`doc_ir.NAMED_KINDS`), written as `data-style="title"` on the `<p>`. They
had no kind before, and `namedStyleType` is in `MANAGED_PARAGRAPH` — named on every
restyle whether or not the block asks for it — so a named style the dialect could not
spell was one the merge quietly wrote `NORMAL_TEXT` over: the document's Title became
body text the first time the source touched that block. A named style the dialect does
not know is a named style it destroys, which is why the set is now the whole of Docs'.

`class="title"` reaches nothing in Drive's importer, so a Title and a Subtitle are also
in what no import can carry: `carry_unimported` compares the plan's named style with
the read-back's and `tidy_requests` writes the difference. Comparing the *style* and
not the kind is what keeps a list item the importer left a plain paragraph quiet — both
are `NORMAL_TEXT` — and it is safe against a reader who demoted a heading in the
browser for the reason the rest of the settle is: the plan is the merged IR, and
`_take_shape` gives it the source's shape only where the document kept the base's.

It also repairs a defect that had nothing to do with named styles being missing:
deleting a block takes its paragraph mark, and Docs merges the two keeping the **first**
one's style, so the paragraph under a deleted heading became a heading. Its style is
written before the delete above it, not after, and nothing could reorder those. The
settle now sees a plan that says paragraph and a document that says heading, and writes
`NORMAL_TEXT`.

**A run or paragraph that only repeats its named style says nothing**
(`doc_ir._named_defaults`). An import sets a face and a size on nearly every run it
writes, and without subtracting the named style the canonical file would come back as
a wall of spans. It is safe as well as tidy: clearing such a field falls back to
exactly the value that was left out.

**The document's theme is the reader's, and an edit through the file leaves it alone.**
What a document looks like — what Heading 1 is, what body text is — lives in its named
styles, and those are the one thing the API cannot write at all: there is no request that
changes one (documented; nothing here has tried). So the only question a source edit
raises is whether it can *pin* a paragraph against its theme, and that turns on a single
rule of `documents.get`: a paragraph and its runs report the properties **set on them**,
never the ones they inherit. A heading a theme makes blue, bold and centred reports none
of the three. The file says nothing about them either, and every field the merge owns is
written *named with no value* (`_run_requests(reset=True)`, `paragraph_style`) — the API's
way of saying "back to what you inherit". So the blue and the bold come back from the
theme after a rewrite, and a reader who later recolours Heading 1 in the browser recolours
the text the source wrote along with the rest. What the reader chose on the paragraph
*itself* still counts, because that is what the document reports and what the file carries.

The alignment was the one exception, and it was a real loss: `paragraph_style` gave
`alignment` a value always, `START` whenever the file said nothing — which is also what a
heading centred by the theme says. One source restyle that never mentioned alignment, and
every such heading went to the margin for good. It is subtracted now like everything else
(`_named_defaults` reads the named style's alignment, `_block_of` keeps only a paragraph
that differs from it) and written like everything else: with a value only when somebody
chose one, `left` among them — a heading the theme centres and the reader pulled back to
the margin is a choice, and is written as `START`.

Neither the loss oracle nor the convergence check could see that happen, which is why the
offline world has learnt to inherit and the campaign now hunts it (§"Proving nothing is
lost", `theme_undone`). But all of this rests on named-and-unset meaning "inherit", which
is the API's documented rule and the one `MANAGED_PARAGRAPH` has relied on since it
existed — a world that models the rule cannot confirm it. The experiment that would
settle it is a themed heading through a live sync, which
`tests/test_docs_live_styles.py::test_a_heading_the_theme_centres_survives_a_source_restyle`
asks for and which has not been run.

**A mark is on, absent, or off.** The same question one dimension down, and the alignment's
twin: a theme that bolds Heading 1 bolds every word of one, so a reader who selects a word
and presses Ctrl+B has *taken the bold off* — a choice, reported as `bold: false` set on the
run, and one the file could not say at all. HTML has no tag for not-bold, so `<b>` or nothing
was the whole vocabulary, the first source edit to rewrite that block named no bold, and the
word went back to wearing the theme's. The run carries it as `data-off="bold"`
(`doc_ir._run_html`, `MARK_FIELDS` for the five marks), `_named_defaults` now reports which
marks the named style puts on so `_style_of` can tell "the theme's bold, unmentioned" from
"the theme's bold, refused", and `doc_merge._text_style` writes the field whenever the run has
a value for it — `False` being a value and an absence being the "back to what you inherit"
the rest of this section is about.

The oracle's half is `styling_restored` (severity `loss`): a word the reader took a mark off
wears it again. Where it looks matters in both directions. A mark the reader *put on* is
looked for anywhere in the tab, because a block the sync rewrote and re-keyed still carries
it and nothing was lost; a mark taken *off* is asked of that block alone, since the theme
bolds the heading next door too and its copy of the word would answer for this one (themed
seeds 9, 32, 40 each failed that way). And what a word *inherits* is never counted as a mark
somebody chose: Docs merges the paragraph behind a deleted one into it and hands over its
style, so a paragraph really does become a heading with nobody writing one — counted as the
reader's bold, the next source restyle of that block read as losing it (seed 283). A block
that stopped being a heading, likewise, took nothing off anybody: only an explicit `False`
against a named style that puts the mark on is a refusal (`unmarked_of`). The campaign's
reader op is `read_unmark_word`, and with `_text_style` reverted in memory it catches 13 of
300 `themed` seeds at chain 3 (`test_the_campaign_sees_a_mark_the_file_cannot_say_is_off`).

**What no import can carry is written by the settle.** `doc_sync.settle` already hands
`doc_merge.adopt_keys` the blocks the run planned, so `adopt_keys` compares them with
the read-back and `carry_unimported` notes the shading, the space around a paragraph,
the small caps and the raised or lowered runs the document has not got; `tidy_requests` writes them in the batch
the settle sends anyway. Only what the plan asks for and the document lacks is
written, never a removal: in a read, "absent" is also what a reader who took the
styling off looks like. After a sync it is almost always empty, the merge's own batch
having written those fields; after a `push`, which is an import and nothing else, it
is the whole of them.

**What the merge owns.** `doc_merge.MANAGED` and `MANAGED_PARAGRAPH` are named on a
restyle whether or not the block asks for them, which is what makes a property the
source dropped go away. A field belongs there only when the file can say it *and* a
read can see it — naming a field the file cannot carry would clear, on every source
restyle, something a reader set in the browser and nothing on our side ever knew
about. That is why `weightedFontFamily` stayed out while `<code>` was all the file
could say, and why it, `fontSize`, `smallCaps` and `baselineOffset` are in now — with
that last one, every field of a `TextStyle` the merge could ever mean to write is one
the file can say and a read can see, and `MANAGED` is the whole of them.

**What the dialect does not model, it names** (`doc_ir.unmodelled`). The convergence
check — a second sync writes 0 requests — is measured on the IR, so it proves the IR
round-trips and says *exactly nothing* about a property the IR never looked at. That is
the shape of the blind spot: a field no reader reads survives an ordinary edit, because
a request names the fields it writes, and goes without a word when the block holding it
is written again from nothing. So `_NODES` is the reader's own map — what it takes the
value of, and what it descends into — and `unmodelled` walks a raw `documents.get`
answer against it and reports, by path, everything left over with a count and an
example. It is the same question `checks.lost_ink` asks of a converted slide: does every
difference lie on something we account for?

`adopt` prints each one (`doc_sync.unmodelled_notes(full=True)`), which is what is owed
to whoever hands over a document somebody else wrote; `push` too; a sync says the count
and the commonest three, or every report would carry fifteen lines that never change.
What a real document turns up: page-level structure (`documentStyle`, `headers`,
`footnotes`, `positionedObjects`, `sectionBreak`), paragraph tab stops, `direction`,
`borderBetween` and the widow/orphan and keep-lines flags, a table's column widths and its
merged cells (`tableCellStyle`, which is the ragged-table limit under its real name), a
picture's crop, angle and brightness, a list's `startNumber` — and, of the document's
theme, a named style's **marks**: the face, the alignment and the measures are read
(`_named_defaults`), a heading's bold is not, and does not need to be, since no request
of ours ever names it with a value.

A count is not an address, though, and the question a person actually has is not
"does this document carry something nobody reads" but "is the paragraph I am about to
edit the one carrying it". So the same walk is run a block at a time
(`doc_ir.unmodelled_in`, one structural element; `doc_ir.unread_blocks`, every block
of every tab, most heavily laden first), and `adopt` and `push` name the blocks by the
words the person can see in the document: *the paragraph 'Why this matters' carries
paragraphStyle.borderBetween; rewriting that block through the file would drop it*
(`doc_sync.block_risk_notes`, eight blocks and four properties each before it says how
many more); a sync, which names nothing, at least says how many blocks carry one, which
is the number it can act on. A section break is left out — nothing rewrites one — and so is everything
outside the blocks, since no rewrite of a paragraph can drop the page's margins. It is
a **risk**, not a loss: a block nobody rewrites keeps all of it, which is exactly the
thing one can act on.

And once the plan exists the risk has an answer (`doc_sync.rewrite_losses`). Of the
blocks carrying something unread, which is *this run* about to write again from
nothing? Nearly always none, and then the report says nothing at all; when it is one,
it is the single line in the report that is a loss rather than a caution, and it is
there before the write — in a `--dry-run` above all, which is the run whose whole job
is to say what a write would cost. A block merely restyled or reworded is left out,
and that is the distinction the whole thing rests on: a request names the fields it
writes, and no field the merge owns is a field nobody reads. A block **rewritten**
(the file's runs go in where the document's were) or **moved** (a move is a delete and
a write, and the write says only what the file says) is named. A block the source
**deleted** is left out too — its words are going on purpose, and a property going
with them is not news.

Two tests pin the map. One walks a fixture holding one of everything and asserts the
whole set, so a property Docs adds later surfaces as a failure rather than as silence.
The
other walks every shape of the fuzz corpus — `doc_world` holds a document the way Docs
holds it and was written apart from this map — and gets back the one thing it has that
we do not model, the section break a body opens on. That is also the campaign's blind
spot named out loud: a defect the world cannot represent is a defect the fuzzing cannot
find.

**Tables are written one line per row**, so a changed row is a changed line in a diff.
The breaks go between `</tr>` and `<tr>` and between the table's own tags and its
rows, where an HTML parser has nowhere to put text; a row stays whole with its cells,
because inside a `<td>` the white space *would* be content. Measured on `from_html`;
that Drive's importer ignores them too is what `tests/test_docs_live_styles.py` is
for — **not yet run**.

Still not carried, and easy to add next: `baselineOffset` (`<sup>`/`<sub>`, which the
importer keeps). Not carryable: a real font weight — `font-weight: 600` arrives as
plain `bold: true` with the weight recorded as 400, so the IR could not tell a
reader's 600 from ordinary bold.

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

* **`equation {}` is empty.** Several index units wide (13 here; 10 and 7 for the
  equations of an OMML import: the width follows the formula), no content, no id. The formula
  exists only in the Markdown export (as LaTeX) or the HTML export (as a base64 PNG);
  `text/plain` leaves it out. So **the Markdown export carries information `documents.get`
  does not**, and a sync that keeps equations reads both (see "Equations" below).
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

Measured on the write side: `addDocumentTab {tabProperties: {title}}` answers with the new
`tabId`; `insertText`, `deleteContentRange`, `updateParagraphStyle`, `createNamedRange` and
`insertTable` all take a `tabId` in their location or range (and `endOfSegmentLocation`
takes one of its own), and a request without one goes to the first tab. A named range made
that way appears under its tab's `documentTab.namedRanges` only. So the sync handles tabs
like this:

- **In the file**, the first tab is the body and every other tab is a
  `<section data-tab="t.…" title="…" [data-parent="t.…"]>` after it; a section without
  `data-tab` is a tab the source asks for. Child tabs are flattened, in the document's order,
  and name their parent.
- **Tab by tab** (`doc_sync._write_tabs`): each tab is its own three-way plan, its own
  structure pass and its own batch, with every location and range stamped with its `tabId`
  (`doc_merge.on_tab`). Keys are unique per tab, as named ranges are.
- **The tabs themselves** follow the three-way rule one level up (`doc_merge.pair_tabs`),
  with the tab id as identity: a tab the source added is created (`addDocumentTab`, under
  its parent if it has one and at the index the file puts it at) and written like any tab
  whose base is empty; one it renamed is
  renamed (`updateDocumentTabProperties`) unless the reader renamed it too; one it deleted
  goes (`deleteTab`) only if the document left it exactly as the base has it and keeps no
  wanted tab inside it. A tab the reader added is theirs and is read into the file; one
  the reader deleted stays deleted, with a note if the source had changed it. A tab created
  by a sync that died before it wrote anything is found by its title, not made twice.
- **A tab just added is one empty paragraph**, and its first table cannot be the first thing
  in its body: `insertTable` there leaves an empty paragraph in front of the table that no
  request can delete (measured). Both are hidden from the IR the way the trailer after a
  final table is (`doc_ir._hide_trailer`: `trailer`, and `lead` in front of a first table),
  and the first block written there goes *into* them.
- **A tab the source adds goes where the file puts it.** `addDocumentTab` takes the index
  the new tab is to have among its parent's tabs and pushes the later ones along, so
  `doc_merge.tab_index` names it: after the nearest tab in front of it in the file that the
  document already has — or that this run has just made, the creates going in file order
  with each new id written back before the next one is placed (`tab_siblings` is the
  document's rows to count in). The body is the document's first tab, so a tab the file
  puts first goes to 1: nothing may stand in front of the body, and the file has no way of
  saying it does. Without this a new tab landed at the end and the settle read that order
  back into the file, so the source's own placing disappeared twice over — the same shape
  of loss as a first-tab rename before `first_tab_title`.
- **The order of the tabs that are already there is the document's.** Blocks the source
  moved go back where the file has them, because a move is a delete and a write — and a tab
  cannot be written from nothing: everything in it would have to be made again, chips and
  equations and all. So a source that reorders its sections does not reorder the tabs, and
  since the settle then rewrites the file in the document's order, a reorder in the file
  used to disappear twice over. `doc_merge.tab_order` says it instead, and only where the
  *source* moved one: where the file still has the base's order, it is the reader who moved
  a tab and the file is simply following.

  A tab *can* be moved, which the code used to deny: `TabProperties.index` carries no
  "Output only" marker — unlike `nestingLevel` beside it — and
  `updateDocumentTabProperties.fields` takes any field of `tab_properties` (discovery
  document, revision 20260427). What is unknown is what happens to the tabs it passes:
  `addDocumentTab` says outright that it pushes the later ones along and nothing says an
  update does the same, so an index written blind could leave two tabs on one number or
  shuffle a strip somebody arranged by hand. That is work destroyed on a hunch, which this
  tool does not do, so the reorder waits for a live measurement (**owed**, below) and the
  note says the order stands rather than that nothing could move it.
- **The first tab names itself in a meta.** Every other tab says its title on its
  `<section>`; the first tab *is* the file's body, and the file's `<title>` is the
  **document's** name, which is a different thing — a document of one tab has both, and
  they part company the moment anybody renames either. So the source could rename every
  tab but the one people actually look at, and a name written into the file went twice
  over: dropped by the sync, then taken back out by the settle. `doc_ir.TAB_META`
  (`<meta name="b2s-tab">`) is where it goes, `doc_merge.first_tab_title` merges it the
  way the other tabs' titles merge, and it is an `updateDocumentTabProperties` like
  theirs. One difference at the beginning: a `push` has no base, and unlike the
  document's name — which the import takes from the `<title>` — the first tab's title is
  Drive's own default, which nothing but the file has ever said, so with no base the
  file's name is written rather than treated as a disagreement nobody can settle.

**The document's name.** A Google Doc's title *is* its name in Drive: `documents.get`
reports it and no `batchUpdate` request writes one. `push` names the document from the
file's `<title>` at birth and nothing said it again, so a source that renamed the document
had the rename dropped and then taken back out of the file by the settle, which reads the
old name back — the same double disappearance. `doc_merge.document_title` merges it three
ways like everything else (renamed in the file alone → written; in the document alone →
the file follows at the settle; on both sides → the document's name stands, with a note;
a base too old to hold a title → the document's name, and the note says why), and
`doc_sync.rename_document` writes it through `drive.files.update`, which is why the plan
carries it as `rename` and not as a request. A rename Drive refuses fails nothing and is
said out loud, as a base it refuses is. The name written — not the one the next read
gives — is what the file and the base then say (`settle(renamed=…)`): `documents.get`
need not have caught up with Drive, and taking the read's word for it would undo the
rename the moment it was made, with the base agreeing so nothing tried again.

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
| **An imported list cannot say whether it is numbered** | every nesting level of a list Drive's HTML importer built reads back `glyphType: GLYPH_TYPE_UNSPECIFIED`, with no `glyphFormat` and no `glyphSymbol` — identically for `<ul>` and `<ol>`, though the editor renders them differently. A list the *editor* makes reads properly (`DECIMAL`/`%0.`, `ALPHA`, `ROMAN`, or a `glyphSymbol`) | `doc_ir._ordered` answers `None`, not `False`, and `doc_merge.restore_unreadable` takes the answer from the canonical file. Then the list is given bullets of the document's own (`doc_merge.bullet_requests`, sent by `doc_sync.settle`): measured, `createParagraphBullets` over an imported list replaces the unspecified glyphs with real ones and keeps every item's nesting level, so from then on the list reads back as what it is. Block identity (`_match_shape`) ignores ordered-ness entirely, or every list item would lose its key on the first read-back |
| **Inserted text inherits the style in front of it** | replacing a word at the start of a bold run or a link by deleting and then inserting at the hunk's start gives the new word the style of whatever preceded the hunk — the word falls out of the run | insert at the hunk's **end** first (inheriting from the last character it replaces), delete afterwards. `canonical` → `canonic` stays bold; without it, it does not |
| **Text inserted at a list item's start joins the list** | a new paragraph written at the index where a bulleted paragraph begins comes out as another bullet of that list | every non-item block is written with `deleteParagraphBullets` before its paragraph style |
| **An insert and an edit at the same index** | a block inserted at index *i* pushes the block that starts at *i* down the document, so that block's own edits — planned against the read — land inside the new text | back-to-front ordering breaks the tie the other way: at one index, a block's deletes and edits go before the insert that displaces it |
| **The body's last newline cannot be written past** | a block appended after the document's last paragraph has no following block to insert before. Writing `text\n` at the last paragraph mark puts the words *inside* that paragraph and leaves the empty one it was meant to end | an append writes `\ntext` instead — the break first, the words after it — and it goes in *before* that paragraph's own edits, which end at the very index it was planned at |
| **Two blocks added at one index come out backwards** | both insert at the same place, and what is written last ends up in front | the added blocks are planned back to front too, so the later one is written first |
| **The newline in front of a table cannot be deleted** | one of the deletes the API refuses outright ("Invalid deletion range"), and a refused request throws out the whole batch — so a source that deletes or moves the paragraph directly above a table used to kill the sync | that block gives up the paragraph mark of the block *before* it instead of its own (`doc_merge._delete_range`). Docs merges the two the way the Delete key does, keeping the first one's style — measured — and the table still has a paragraph in front of it. A run of deleted blocks passes the borrowing leftwards so no two ranges ask for the same mark; with nothing in front to borrow from (the document's first block, or a paragraph between two tables) the words go and the empty paragraph stays, which is what Docs wants between two tables anyway |
| **The body's last newline cannot be deleted either** | a source that deletes the document's last paragraph asked for a range ending past the body's own mark — refused, measured, and the batch with it | the last block borrows the mark in front of it the same way (`_delete_range(ends=True)`), and at one index a delete goes before an append, since the borrowed mark is where an append after it is planned |
| **A table that ends the body has an empty paragraph after it** | a document ends on a paragraph, so an import or an `insertTable` at the end leaves one — with the bullet or heading of the paragraph it was split from — and the delete of the mark in front of it is refused | `doc_ir._hide_trailer` leaves it out of the IR (a `<p></p>` would be imported again, and a table appended at the end would never converge), keeps its span as `trailer`, and the first block appended after the table is written *into* it; `doc_merge.tidy_requests` makes it a plain paragraph again |
| **Nothing can be written at a table's own index** | `insertText` at a table's start index is refused (measured), so a block the source added directly in front of a table killed the batch | the block goes after the paragraph before the table instead — `\ntext` at that paragraph's mark, before that paragraph's own edits (`doc_merge.requests`). A body that starts with a table has an undeletable empty paragraph in front of it (`lead`), and the first block written there is typed into it |

A list the reader switches from bullets to numbers used to be invisible (the first row);
since `bullet_requests` it is seen like any other change of kind, and the document wins
it as it wins everything. What the merge reports instead of writing is listed under
"What the merge refuses to write" below.

## The commands

```
python -m beamer2slides docs push  doc.html [--name "In Drive"] [--new-doc]
python -m beamer2slides docs adopt --doc <url|id> [doc.html] [--force]
python -m beamer2slides docs sync  doc.html [--doc <url|id>] [--dry-run]
                                            [--assume-base document-wins|source-wins]
                                            [--no-backup]
```

- **`push`** imports the file through Drive, reads the document back, gives it the
  file's keys, plants one named range per block, and rewrites the file with those keys
  and a `<meta name="b2s-document">` naming the document. From then on the file alone
  says where it lives, and a push is never repeated: `--new-doc` is the only way to get
  a second document out of one file.
- **`adopt`** is the way in for a document nobody pushed — a Google Doc a team has been
  writing for a year, which `push` can only refuse. It reads the document (every tab),
  keys its blocks, plants one named range each, writes the canonical HTML file and
  stores the base; from then on the pair is an ordinary synced pair. The path defaults
  to a slug of the document's title in the current directory. It is idempotent: a
  second run finds every block already named by its range and writes the same file with
  no second set of anchors. It refuses a target file that is already there and names
  another document — or none at all, which is somebody's canonical file — unless
  `--force`. A document Drive's HTML importer built cannot say whether its lists are
  numbered and there is no file yet to say for it, so `settle` gives those lists
  bullets of the document's own, after which they read back as what they are.
- **`sync`** merges base, file and document three ways, sends the edits with
  `requiredRevisionId`, and **regenerates the file from the document it just wrote**.
  A refusal on the revision — somebody typed between the read and the write — re-reads
  and re-plans, three attempts (`B2S_DOCS_BEFORE_WRITE` runs a command right before the
  first write, which is how that path is tested).

Every sync also reads the document's **open comments** (`doc_sync.open_comments`, Drive's
comments API, which `drive.file` reaches for the documents this tool made) and names them
in the report, with who asked, the passage they hang on and how many replies there are.
A comment lives in Drive and not in the document's content, so nothing the merge reads
can see one — and a sync that rewrites the passage a comment hangs on answers it by
accident. Nothing here writes or resolves one: that is the reader's to do, in the
browser. A read that fails (a scope, a share) is reported too, never raised.

`<stem>.sync-report.{json,md}` in `.b2s/` beside the file says what each side
contributed, what conflicted (the document wins) and what was left alone.

### The base is Drive-first

The base of the last sync — what both sides agreed on, and the only thing that can tell
a source change from a document change — is **a JSON file in Drive**, in the document's
own folder, whose id is written into the **document's** `appProperties.b2sBase`. The
copy in `.b2s/<stem>.base.json` beside the file is a **cache**. That is the Slides
side's arrangement (`snapshot.save_drive` / `load_drive`) and the Docs side did not
have it at first: the base lived only in `.b2s/`, which is git-ignorable scratch state,
so a fresh clone, a colleague's machine, a second checkout or anyone who deleted what
looked like scratch dropped `sync` into the `--assume-base` dialog — where both answers
throw somebody's work away. `drive.file` reaches both files (this tool made them), and
no wider scope is asked for.

Reading (`doc_sync.load_base`) prefers Drive and falls back to the cache, and **says
which it used**: that the cache is older than Drive's (another checkout has synced
since — `generation` counts the syncs, so the two can be compared), that Drive's is the
older one and the cache is being used instead (what a sync whose Drive upload failed
leaves behind), that Drive has no base for this document yet, or that the document
names a base in Drive that cannot be read — deleted, or somebody else's now — in which
case the cache may be older than the document. Nothing is lost when it is: the document
wins where both moved, and the other checkout's changes read as the document's. A base
that is truncated or belongs to another document is not used at all, and why is
reported rather than swallowed, because a base ignored in silence makes the next sync
read every difference as somebody's change. Writing (`store_base`) goes to the cache
first, atomically, then to Drive; **a Drive write that fails never fails the sync** —
the document has already been written by then — it is printed and named in the report.

### `--assume-base`: whose work is discarded

Only when there is no base anywhere. The two answers used to be spelled `file` and
`document`, which named the side the base would be *taken from* — the exact opposite of
how anyone reads them, because that is the side whose changes are thereby thrown away.
They are now:

| | what it does |
|---|---|
| `--assume-base document-wins` | the document is right where they differ: nothing is written to it, and the file is rewritten from the document. Every edit made to the source since the last sync is discarded |
| `--assume-base source-wins` | the file is right where they differ: the file is written over the live document. Every edit a reader made there since the last sync is discarded |

`file` and `document` still work as aliases and print one line saying what they really
do. Before the destructive direction — `source-wins`, which overwrites a live document
other people may be in, with no base to merge against — the document is exported to
`.b2s/backups/<stem>-<timestamp>.html` (`files.export`, `text/html`), and the path is
printed and named in the sync report. An export Drive refuses **stops that sync**: the
Slides side's rule (`guard.demand_way_back`) is that a write with no way back is
something one asks for — `--no-backup` — and never something that happens because an
export failed. The export is Drive's HTML, which is a lossy read-back (see "The round
trip does not close on its own"): a copy of the words to recover from, not a file this
tool could push back unchanged. The non-destructive direction writes nothing to the
document and needs no backup, and neither does `--dry-run`: a look at what this answer
would cost leaves nothing behind, and the report says the export a real run would take.

### Batching, and the atomicity it costs

`doc_sync.send` puts a plan of up to 500 requests into one `batchUpdate`, which is
atomic: it lands whole or not at all. Above that it cuts the plan into consecutive
batches of 500. Docs applies a batch in the order it is given and the plan is already
in that order, so consecutive batches write the same document as one batch would —
nothing is reordered, and no request crosses a boundary. `requiredRevisionId` can only
guard the first batch, since after it the document's revision is ours; the answer
carries the new one (`writeControl`), so each batch requires the revision the one
before it produced, and somebody typing half-way through the run is still refused.
(Unmeasured: whether `BatchUpdateDocumentResponse` always answers with a
`writeControl`. If it does not, the chain stops guarding rather than sending a stale
revision.) **The honest caveat:** a chunked write that fails part-way leaves the
batches before it in the document, where a single batch could not. The sync report says
so whenever chunking actually happened. The structure pass (`_write_structure`) is
unaffected — it is already a batch of its own, for its own reasons.

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
- a block that cannot be written from nothing — one holding an equation or another chip
  no request makes — is **not** moved; it stays where the document has it and the report
  says why.
- a **table** is moved by the structural pass below: deleted where it stands and built
  again, blank, where the file has it, its words written on the pass after, like a new
  table's. Only a table whose cells the document left as the base has them, with no chip
  in them, is moved that way — anything a reader wrote in it would be deleted with it —
  and the rest stay put, reported.

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
- **the base says how its lines match.** The pass after a regrid would otherwise have to
  match the rows and columns again from their words, one side of which it just made
  blank; `rebase_tables` records the matching it already knows (`aligned`), and it is
  used for as long as both grids are the size it expects.
- **a table the source also moved is built from the merged grid, not from the file's.**
  A move is a delete and a table built again, and the two grids are the same table until
  this very sync writes one: the regrid goes in the batch above, the base takes it, and
  the round after plans the move against a file that still holds the row the reader
  deleted. Building from the file put that row back and the report said only that the
  table had moved (chain-8 seed 74230). `_apply_source_moves` takes the size from the
  merged block now, and `rebase_tables` gives such a table the same `aligned` a regrid
  gets (`_moved_table`) — its base can only be the blank grid the document shows, and a
  blank row matches nothing by its words, so without it the round after reads the file's
  extra line as one the source has just added and puts the row back a second way. Both
  halves are pinned by a test that fails, differently, with either one out.

**Rows and columns merge three ways, like blocks** (`doc_merge._table_lines`). Tables of
one shape on all three sides are matched by place, as a cell always was, so a row the
source rewrote end to end is still that row. Otherwise the columns are matched first —
by the words in them wherever they stand, since a row added shifts every cell below it
(`_column_score`) — and then the rows, by their cells in the columns that matched
(`_row_score`); both keep their order (`_align`, the highest total of pairs at least
`ALIKE`, and the lines left between two pairs paired by place). Each side is matched
against the base, and the two matchings merge (`_merged_lines`): a line the document
added stays, a line the source added goes in after the line that precedes it in the
file, and a line the source took away goes only if the document left it as the base had
it — otherwise it stays, reported. So both sides may change the grid at once, and one
source edit may change rows and columns together. The ops are written at the document's
indices, rows before columns, each back to front, a delete before an insert at one index
(`_op_order`); a merge that would leave none of the document's lines (the last row cannot
be deleted) is reported instead.

**A cell holds paragraphs, not a paragraph.** When the three sides agree on how many a
cell has, they merge one by one; when they do not, the cell merges as one text with its
paragraph breaks in it (`_merge_cell`), and is written against the live cell read the
same way (`_joined`: the marks between its paragraphs are newlines, the cell's own last
mark stays outside) — so a break the source added is an `insertText` of `"\n…"`, and one a
reader added survives the source rewriting a word next to it.

**Deleting a table**, measured: its own span `[start, end)` leaves the paragraphs on
both sides of it as they were, and taking the mark in front of it along merges the
paragraph before with the one after. The two paragraphs no request can delete are the
exceptions (`doc_merge._delete_range`): a table the body opens on goes with its `lead`,
from the lead's start — measured, `[1, end)` is accepted and leaves the paragraph after
the table first (or, with nothing after it, the one empty paragraph a body keeps); a table the body ends on takes the mark in front of it instead of
leaving its trailer behind as a stray empty paragraph — measured, `abc`, a table and the
trailer become `abc` alone — unless a block is being written into that trailer.

What `insertTable` does exactly, measured: it **splits the paragraph its index is in** —
what was before the index stays a paragraph, then comes the table, then the rest — and
the index must be *inside a paragraph*, which a table's own start index is not. So a
table written in front of an ordinary block goes at that block's start and the empty
paragraph it leaves in front of itself is swallowed by deleting the mark of the block
before it; a table written in front of another table goes at the mark of the paragraph
before it, and the empty half lands *between* the two tables, where Docs wants a
paragraph anyway; and a table written after everything goes to the end of the segment,
where Docs keeps a paragraph after it, because a document ends on one.


### Pictures, and the chips a request can make

A picture in the canonical file is an `<img src alt width height data-object>`: `src` is
a file beside the canonical one (or a URL), `width`/`height` are CSS pixels (measured: a
60 × 40 px picture imports as 45 × 30 pt, `doc_ir.PT_PER_PX`), and `data-object` is the
document's id for it. In the IR it is a frozen run — one index unit, never rewritten as
text — that a sync can nevertheless *create*, which is the difference that matters.

- **The document cannot say which file a picture came from.** `insertInlineImage`
  records the staging URL as its `sourceUri`, and nothing can set a field of ours on it,
  so the file carries the object id and the base remembers it with the file's name and
  a digest of its bytes (`doc_merge.restore_pictures`). A figure the source regenerated
  under the same name is therefore a *changed* picture, and one it only renamed is not.
  Pictures a sync has just inserted have ids nobody knows yet; they learn their files by
  place — the n-th picture of the block the plan wrote (`doc_merge.place_pictures`).
- **Staging** (`doc_sync.Stager`): `insertInlineImage` takes a URL, never bytes — the
  same wall as Slides' `createImage`. The pictures one batch needs are imported as a
  document of their own, with `data:` URIs that Drive's HTML import embeds (measured,
  `alt` and `title` kept), and each picture's `contentUri` there is what the batch
  inserts. The staging document is deleted once the batch is in; measured, the inserted
  picture is a copy and still loads afterwards. No link is ever made public. `push`
  needs none of this: its import carries the pictures as `data:` URIs directly.
- **Written:** a picture paragraph the source added; a picture the source added,
  removed or replaced in a block the document left exactly as it was (the block is
  written again from the file, `rewrite`); a block with a picture that the source moved
  (from the document's own copy — its `contentUri` — so nothing is staged).
- **A picture a reader inserts** comes back as a file: `doc_sync.fetch_pictures` saves
  it once, under its object id, in `<stem>.media/`, because a `contentUri` dies within
  the hour. From then on git keeps it like any other picture.
- **Chips:** `insertPerson` (from the email) and `insertDate` (from the timestamp) make
  those two chips, so a new block carrying them is written with them, and one that
  holds them can be moved. `insertRichLink` is refused (measured); an equation, a
  dropdown, a footnote and a table of contents have no request at all, and a block that
  holds one is still never deleted to be written again.

### Equations

Read-only, and read through a side door. Nothing in the Docs API makes an equation
(an OMML `.docx` import does, which is how the live test gets one), and `documents.get`
reads one as `equation {}`. What it says is in Drive's Markdown export (`text/markdown`),
measured on an OMML import with a second tab (the document of
`tests/test_doc_ir.py::EQUATIONS` and its export, `EQUATIONS_MD`):

- every tab is exported, each under a `# **Title**` heading once there is more than one;
- an equation is `$…$`, a display equation alone on its line `$$…$$`;
- **nothing escapes a dollar** — not one in the text (`costs $5`), not one in the
  equation (`${x}_{1}+α_$$`) — while `_` becomes `\_` and a backslash doubles.

So the dollars cannot say where an equation ends. The document can:
`doc_ir.equation_spots` lists each equation with the words on either side of it in its
paragraph (or the paragraph's edge, or the equation next to it), and `doc_ir.latex_of`
looks for it in the export between those words — up to 24 characters of them, allowing
any escape or bold/italic marker between their characters — in document order, taking
the shortest LaTeX that fits. One it cannot place with certainty gets none, and shows
empty in the file as it always did.

Only the read that becomes the file and the base asks for the export
(`doc_sync.equation_latex`, from `settle`): the LaTeX lands in the frozen run's text,
`<span class="b2s-chip" data-chip="equation">E=m{c}^{2}</span>`. The planning reads
don't, and `doc_merge.restore_unreadable` gives their blank equations the base's LaTeX
back (a block with as many equations as its namesake, in order), so an equation never
reads as changed and the words around it still merge. Editing the LaTeX in the file
does nothing but get reported: it is a frozen run the source changed.

### What the merge refuses to write

Each of these is reported in the sync report, never guessed at:

| Case | Why |
|---|---|
| a block whose frozen runs the source changed while the document also changed it — or one that holds a chip no request creates | an equation, a rich link, a dropdown or a table of contents cannot be written again once deleted, so the text around one is left alone rather than rewritten without it. Pictures, dates and people are written (above) |
| a table with a **row out of step with the others** (merged cells) whose grid changed | rows and columns are matched as whole lines; cells of a ragged table only merge by place, while the three grids agree |
| a **row or column the source took away that the document wrote in** | kept, as a block the source dropped but the document edited is |
| a source restyle of the very words the document rewrote | the marks follow the words (`doc_merge._restyled_words`): every word of the merged text takes the document's styling, and the file's where the file has that word too — so a word the source bolded is bold while the reader rewrites the rest of the paragraph. Only a restyled word the document replaced has nothing to carry the marks: its new words keep the document's styling, and the report says so |
| a **move of a block with an equation-like chip in it, or of a table the document changed** | a move is a delete and a write, and those cannot be written from nothing — the block stays where the document has it |
| a **reorder both sides made** | the document's order stands whole; the file's is reported |
| a **tab the source moved** | a tab cannot be written from nothing, so no request moves one; the document's order of tabs stands and the file's is reported (`doc_merge.tab_order`) |
| a picture's **size or alt text** the source changed | no request in the v1 API changes an embedded object. `insertInlineImage` carries an `objectSize`, so a resize is written when the picture is inserted again anyway (one the source regenerated) and not when the run the plan writes is the document's copy (one merely moved); an alt text is never written. The settle then puts the document's values back into the file, so the edit goes twice over — `doc_merge.unwritten_pictures` names it, and says that a picture written into the file again *without* its `data-object` goes in at the size asked for |

Everything else is written: text on both sides, a block's kind, level and alignment,
its bullets, the marks the source added *or took away* (the fields Docs needs named for
a removal are listed in `doc_merge.MANAGED`), and whole new blocks with their styling.

### Proving nothing is lost

The bargain above is only worth what it can be held to, so the Docs sync is fuzzed the
way the Slides one is: random edits on both sides, and an oracle at the end that asks
one question — *did anything a person put in the document disappear without the report
saying so?* `doc_loss_oracle.py`'s docstring defines every word of that (the **reader**
is the person, the file in git is a *request*, and the merge may refuse a request but
never throw work away in silence), and it judges the read-back **after** the sync has
settled, not the plan and not the requests.

`tools/fuzz_docs.py offline --rounds N [--chain N]` runs it. One round is a whole life
of a document: a shape from the corpus is pushed, the reader types in it, the source
rewrites the file, `docs sync` runs, the oracle judges, and then the sync runs once more
on a copy and must write **nothing** — a sync that does not settle would keep rewriting
a document for ever. About 150 rounds/s at chain 1, 20/s at chain 4.

Three pieces make it find things rather than merely run:

* **`doc_world.py`**, a reference applier that holds a document as Docs holds it and
  applies the real requests `doc_merge.plan` produces under Docs' real index rules —
  including **throwing out the whole batch when one request is refused**. That is the
  failure that killed Slides syncs twice, and an applier that merges *state* instead of
  replaying requests can never see it.
* **`collide`**, a source op that changes exactly what the reader just changed. Two
  independent random draws almost never land on one block; over on the Slides side this
  op took text overrides from 5 in 200 rounds to 56.
* **Chains** (`--chain N`): edit, sync, edit, sync. Several of the worst Slides bugs
  only showed at depth 4 to 8, because they need a base a previous sync wrote rather
  than one a push made.

Everything the campaign reaches is counted and printed — corpus shapes, reader and
source ops, request kinds, merge outcomes. That counting is not decoration: it is what
showed that the whole of §"What the canonical dialect says" beyond bold and italic —
the face, the size, small caps and the six paragraph measures — was unfuzzed, and
therefore that the *clearing* `doc_merge.MANAGED` governs had never been exercised. The
ops now draw from the whole dialect on both sides (`RUN_MARKS`, `PARA_MARKS`,
`READER_FACES`, `READER_MEASURES`), which is the case that matters most: a reader
chooses a face, and then the source restyles that block.

Drawing a field is not enough for the world to *hold* it: `doc_world` applies an
`updateTextStyle` field by field through `API_TO_IR`, so a `MANAGED` field missing
there is applied nowhere and read back never — both sides would draw it, and it would
agree for the one reason that proves nothing. `baselineOffset` was exactly that for
an afternoon, and `test_the_world_applies_every_run_field_the_merge_writes` is the
line that says it can never be again.

**A defect the world cannot represent is a defect the fuzzing cannot find**, and the
alignment loss above is the plainest case there has been. Nothing was deleted, no word
moved, and the settle regenerates the file from the document it wrote, so the file then
says exactly what the document says and the second sync writes nothing: the oracle was
happy, the convergence check was happy, and the document's theme was gone. The world
had no named styles at all, so it could not even hold the *question*. It has them now —
`align` `None` means inherited rather than `START`, a `World.theme` maps a named style
to what it says (no request ever changes one; the API has none), a read-back reports
`alignment` only where it is set, as `documents.get` does, and
`updateParagraphStyle` with the field named and no value puts a paragraph back to
inheriting. The `themed` corpus shape is a document with a centred Heading 1 whose
headings say nothing of their own, and `doc_loss_oracle._inherited_findings` asks, of
every block that survived the sync, whether a field the theme sets for its named style
has turned into one of its own that neither the reader nor the file asked for
(`theme_undone`, severity `loss`).

It has to be *told* what the theme sets — `fuzz_docs.theme_fields` passes it in — and
that is the check's substance, not ceremony: asked about every property, the same
question accused 14 of 120 rounds of Docs' own merge-on-delete rule, where a paragraph
really does take the style of the one deleted in front of it. With the fix reverted in
memory, the campaign catches 6 of the first 40 `themed` seeds at chain 2, which is
`test_the_campaign_sees_a_theme_undone`; without it, none. Inheritance itself is still
Google's word and not something any offline world can settle — that is what the live
experiment in `tests/test_docs_live_styles.py` is for.

The run marks are the same defect one dimension down (§"A mark is on, absent, or off"),
and the world holds them the same way: a named style's `textStyle` says what its runs
wear, `updateTextStyle` with `bold` and no value puts a run back to inheriting it, and
`read_unmark_word` is a reader taking a mark off a word of a themed heading. The oracle
asks it as `styling_restored`, and with `doc_merge._text_style` reverted to writing a mark
only when it is on, the campaign catches 13 of 300 `themed` seeds at chain 3
(`test_the_campaign_sees_a_mark_the_file_cannot_say_is_off`). Both halves needed the
reader op *and* a source op that rewrites the same block, which is what `collide` is for.

The campaign found ten defects, each pinned by a test in `tests/test_doc_fuzz.py` —
`xfail(strict=True)` while it stands, a plain test once it is fixed — and the ones still
standing described in `fuzz_docs.KNOWN`: a table of contents treated as an ordinary block
(which killed the sync outright, because a refused request throws out the batch), a table
the source dropped deleted however much the reader typed into it, one block's key landing
on another, a block reworded *and* moved losing the reader's styling, and the rest. A
defect still standing is let through by default and `--strict` fails on it, so a fix shows
up as a defect that stops being reached; once it is fixed and has a test, its entry goes,
or it would swallow the next defect that looks like it.

**All ten signatures are now at zero and `KNOWN` is empty**, so any finding at all fails
the campaign. What closed them falls into four groups: six ways of losing a block's
identity, two ways of losing the reader's content outright, three ways of killing the sync
where it stood, and one place in a document where Docs will let nothing be written at all.
(Some of them closed one of the first group and one of a later one — a table losing its
anchor, Docs' index rules, the place with nowhere to write.) `lost-key` went 34 → 2 → 0
and `moved-styling` 2 → 0, both below. `crossed-delete` — two blocks under one key and
none under the other, so the merge read the second as dropped by the source, deleted the
paragraph the reader was reading and wrote the source's new wording nowhere — was
`inherit_keys` crossing the keys the file asserts, the first entry in that list of six,
and it went 2 → 1 → 0 with it. Its entry stayed on after the fix "to catch whatever else
can reach the signature", which is backwards: an entry here is *let through*, so keeping
it is the one way to make sure nothing is caught. And one of the ten reached zero with no
defect behind it at all: it was the oracle miscounting, twice, and that is the last part
of this section.

Losing a block's identity is the root of the worst of the rest, because a block the
merge cannot recognise is a block it deletes as "dropped by the source".

* `inherit_keys` matched **every** block of the file again by its words, although the
  file had just named them all with its own `id=`. Two blocks that read alike swapped
  keys; one key then stood on two blocks and none on the other. `doc_ir.key_blocks`
  had the rule written down all along — a key from the file or from a named range
  outranks a guess from the text — and this broke it for every block at once. What is
  left to match is what the file does *not* name, which is what `push` calls it for.
* `settle` adopted and keyed one part at a time, and `key_blocks` recurses into the
  tabs, so the **first** part's keying named every later tab's blocks after their words
  — before those tabs reached `adopt_keys`, which then found them keyed and left them
  alone. A block whose named range the write had taken with it came back under a name
  made from its new text. A table is anchored in its first cell (`anchor_span`), so a
  source that rewords that cell destroys the range *every time*: the table settled as
  `table:<its new first word>`, and file, base and document all agreed on an identity
  the file never gave it. One source op and no reader at all was enough.
  `doc_merge.settle_keys` is now the one place that does both, in two passes — and it
  is shared with the harness, which had the same bug because it is a copy.
* The paragraph under a deleted heading became a heading (Docs merges the two keeping
  the **first** one's style, and the merge writes its style before the delete above it),
  which the settle now repairs — see "Every named style Docs has is a kind" above.
* A table is anchored in its first cell, and a **row delete can take that very cell**.
  The structural batch went out, the table came back with no named range at all, and
  nothing put one back: `anchor_tables` only ever looked for tables the batch had
  *built*. The table settled under a name made from its new first word, the file's
  `table:year` read as gone, and the words planned against the new grid — the source's
  own cell edit — were written nowhere. `structure` now gives a regrid the same `after`
  a new table gets, so the table is found again and `plant_ranges` puts its range back.
  One source op, no reader at all (offline chain-8 seed 7122, shrunk).
* And `adopt_keys` — the one thing that gives a block written from nothing its key
  back — matched a block's **shape and its words together**, so the very blocks a
  write mangles were the ones it could never adopt. Docs merges two paragraphs
  keeping the first one's style, so deleting a block hands the block after it the
  shape of the one that went: a list item under a deleted paragraph comes back a
  plain paragraph, a heading under a deleted subtitle comes back a subtitle. And the
  repair that would put the shape back, `carry_unimported`, is itself keyed by the
  key this pass restores — so the two failures held each other up, and the block
  settled under a name made from its new shape and its new words. `_adopt_by_words`
  is a second pass on the words alone, taken only where one free key and one unkeyed
  block say the same thing. One source op pair and no reader at all (offline chain-8
  seeds 7034 and 7048, shrunk).

  Its other half is not about identity at all: **the bullet**. A list item and a
  plain paragraph are both `NORMAL_TEXT`, so writing the named style back is blind to
  exactly the thing a delete takes away most often, and the settle then wrote that
  plain paragraph into the file — the source's own list, quietly one item shorter,
  with nobody the wiser, since file, base and document then agreed. `carry_unimported`
  reads the bullet too and `restore_bullets` writes it, after the paragraph styling
  and before `bullet_requests`, for the reason bullets always go last.

Then the two losses that were not about identity at all. **A table the source dropped
was deleted however much the reader had typed into it**: a document edit outranks a
source delete, and the test for "edited" was `block_text`, which is empty for a table —
so every table read as untouched. `doc_merge._edited` reads a table's cells and its
grid, and a block's frozen runs, which also stops a picture a reader replaced from being
thrown away. And the same decision now refuses to delete a block holding an equation, a
dropdown or a table of contents **at all**: `_merge_block` already refused to
delete-and-rewrite one, and a plain delete is that same loss with nothing written back.
The block is kept and the report says the source asked for it to go.

And then the three that killed the sync outright, where a request Docs refuses throws
out the whole batch. All three were the same mistake: **Docs' index rules are about
structural elements, not about tables.** Nothing can be inserted at one's own index,
the newline in front of one cannot be deleted, a body cannot open or end on one without
an empty paragraph beside it, and one is deleted by its own span — and every test for
any of that read `kind == "table"`. A table of contents was therefore an ordinary
paragraph to the planner: a block written in front of one went at its own index, a
block deleted in front of one gave up its own mark, and a table added beside one was
written with a delete that landed inside the TOC's own units. `doc_ir.STRUCTURAL` is
the set, `doc_merge._structural` the test, and `_hide_trailer` now hides the lead and
the trailer beside a TOC as it always did beside a table.

The third of them cannot be written at all and is now refused rather than attempted: an
empty paragraph **between two tables** has no mark to give up — its own is the newline
in front of a table, and the block before it is a table with none to lend — so
`_delete_range` came back with a range of length zero, which Docs refuses. Docs wants a
paragraph between two tables anyway, so `restore_undeletable` puts the block back into
the merge (round by round, since keeping one changes what the next delete may take) and
the report says why it stayed.

And then the one that is not a mistake in the arithmetic at all but a place in a
document where Docs will let nothing be written. Where two tables really do touch there
is **no paragraph**, so the mark a block in front of a table borrows does not exist and
the index it borrows is inside the last cell of the table before it: a table added there
was built inside the old one, its words never reached it, `anchor_tables` could not find
it and each re-plan built another; an ordinary paragraph *moved* there was deleted from
its old place first, so the move destroyed it outright. Both are refused now —
`_new_table_requests` asks for nothing, `refuse_nowhere` leaves the paragraph where the
document has it — and the report says why. That is the `two_tables` shape; in the
`between_tables` one the editor's own undeletable paragraph is there to borrow from and
everything goes in.

At one seed, 200 rounds at chain 8: `toc-block` 10 → 0, `toc-table-split` 4 → 0,
`empty-delete` 6 → 0, `dropped-table` 17 → 0, `dropped-frozen` 8 → 0, `crossed-frozen`
22 → 0, `table-in-a-table` 1 → 0, `lost-key` 34 → 2, `crossed-delete` 2 → 1,
`moved-styling` 2 → 2 — and the same at a second seed and at chain 4; `lost-key`,
`moved-styling` and `crossed-delete` then went to 0 (below). Every
signature that reached zero is **out of `KNOWN`** rather than rewritten: each has a
test of its own now, and each was wide enough to swallow the next defect that looks
like it — `block_gone` mentioning `table:` had been catching crossed keys on tables all
along, and `frozen_gone` with no words at all would catch every way of losing a picture
there will ever be. That is not a worry about the future: taking `crossed-frozen` out
uncovered, at two other seeds, a person chip going with the block the source dropped,
which had been filed under it all along.

`lost-key`'s last two had a cause that looked like it had no fix, and the entry said
so. An empty paragraph is all mark, so its named range **is** its mark — and a block
written in front of a table goes in as `"\ntext"` at the mark of the paragraph before
it, which Docs then hands to the new paragraph, a range being pushed along by an insert
at its own first index. The empty paragraph's key rides onto the block that was written
and the key the file gave the moved block is nowhere. There is no index that both
appends after an empty paragraph and leaves its range alone, and reading the key back
off the block whose words contradict the plan was tried, in two widths, and cost that
round its convergence. What works is neither: the write is right, and the range is
**planted again** where it belongs, in the same batch — `doc_merge.requests` puts a
`deleteNamedRange` and a fresh `createNamedRange` on the mark after every append there
and before a block inserted in front of the paragraph (`REPLANT`, between `APPEND` and
`EDIT` in the order at one index), which `doc_world` had to learn to take. The same
drift comes from the reader's side: a person chip or a word put into an empty paragraph
pushes its range onto the mark, so `apply_keys` now records where a range *is*
(`block["range"]`), `doc_ir.replant_requests` names the ones that no longer start where
`anchor_range` puts them, `name_requests` sends those at the settle, and `structure`
heads its batch with them — because the batch that builds a table swallows the empty
paragraph `insertTable` leaves by deleting the mark before it, that mark carried the
chip paragraph's drifted range, and the moved table is found again by the key of the
block it follows (chain-8 seed 296: the table settled as `table:empty`, the file still
naming `table:c`). At one seed, 300 rounds at chain 8 and 600 at chain 4 under
`--strict`: nothing.

A range does not only drift, it **stretches**: text written *inside* one grows it, so a
reader who presses Enter in the middle of a paragraph — or, as the campaign did it, drags
a block into one — leaves a single name over both halves. Nothing shows while both stand,
because `apply_keys` gives the name to the first block, which is where the text it was
given to still is, and the second is keyed by its words at the settle. It shows when the
source later drops that first block: the delete takes only its own span, the stretched
range lives on over the second block's words, and that block reads back under the first
one's key with its own gone, though neither side dropped it. `replant_requests` therefore
plants a range that ends *past* its block again, as it does one that starts too late. Only
past: a range may well end short of its block, since text typed at the paragraph mark falls
outside it, and replanting on that would rewrite a name at every settle for nothing.
Campaign seeds 279 and 361 at chain 4, which came out of nowhere when a new reader op
changed which seeds draw what — and fail identically at the commit before it, which is the
only thing that tells a defect the campaign has just reached from one somebody just wrote.

And a range **outlives its block**, which is the mirror image of the same thing. The two
commonest edits a person makes after typing are pressing Enter in the middle of a paragraph
and backspacing at the start of one, and the campaign was doing neither by hand — it reached
the split only through a dragged block, and the join not at all — so `read_split_block` and
`read_join_blocks` now do them (84 and 73 draws in 200 rounds at chain 4). The join is the
one that bites: Docs merges the two paragraphs keeping the first one's style, and *both*
named ranges are now inside the one paragraph that survives. Nothing shows while it stands,
since `apply_keys` gives the block the range that starts in it and the loser is simply not
looked at — until a source edit rewrites the winner's words. The delete takes the winner's
range with it, the loser is all that is left, and the block comes back under the name of the
paragraph that was swallowed: the key the file asserts names nothing, and a second checkout
reads one block gone and one added, though neither side dropped anything. So `apply_keys`
now records what no block took (`ir["orphans"]`) and `doc_ir.orphan_requests` deletes those
ranges at the settle, at the head of `name_requests` — one block, one name, and nothing
there moves an index. Chain-4 seed 70140; breaking the delete fails 1 round of 200 at
chain 4 and 1 of 200 at chain 8, two `identity_lost` findings each, which is thin enough
that the mechanism is pinned by a test built by hand
(`test_a_range_left_behind_by_a_join_does_not_steal_the_blocks_key`: join, sync, rewrite the
survivor, sync, and the file's key is still on it). With the two ops in, 400 rounds at
chain 4 and 300 at chain 8 under `--strict`: nothing.

**The settle is one write too late**, though, and the campaign said so as soon as it could
join. A reader joins a picture paragraph into a list item and the source rewords that item:
the write replaces the words the *surviving* range sits on, which destroys it, while the
picture is untouched — so the orphan is the only name left in the paragraph, the read-back
names the block after the paragraph that was swallowed, and the key the plan meant it to
have is nowhere for `adopt_keys` to give back. The settle then plants a range for the wrong
key, and the file the author wrote `id=` in comes back saying something else. So the orphan
deletes head the write batch as well (`doc_merge.requests`, where they move no index), and
only a sync that writes nothing leaves them to the settle. Chain-8 seed 77064, shrunk to one
source op and two reader ops; with the head of the batch taken out, 2 of 250 rounds at chain
8 fail with 8 findings between them.

And a third op would have been one too many. `read_paste_block` — the reader copies a
paragraph and pastes it elsewhere — makes the one thing nothing else here makes: **two
blocks that say exactly the same thing**, one named and one known to nobody, which is the
degenerate case of identity by words, the fallback under `key_blocks`, `inherit_keys` and
`_adopt_by_words` alike. It found nothing (124 draws in 400 rounds at chain 4, 137 in 250 at
chain 8) and is kept for what it says while it keeps finding nothing, with
`test_a_paragraph_the_reader_pasted_twice_over_keeps_the_originals_identity` to pin the
behaviour it walks over. What it did do is move every draw, which is how 74230, 76101 and
77064 were reached at all — and that is the campaign's own law again: a seed names a script,
not a defect.

The same signature, a third way: **a body may not end on a table.** The paragraph after a
final one therefore keeps its paragraph mark however it is deleted — `_delete_range` takes
its words and leaves an empty paragraph exactly where it stood — but the index a new block
is appended at came from the last block the sync *keeps*, and with that paragraph gone that
is the table. A table's own last index is inside its last cell, so a block the source added
in the same step as it dropped that paragraph was written **into the table**: the table
swallowed it, the table's named range went with the write, and the whole body came back as
one table under a name made from its new first word. The empty paragraph left behind is a
trailer exactly like the one a body ending on a table already has, so `requests` treats it
as one (`left_empty`) and the first block appended is written into it rather than after the
table (chain-8 seed 189).

`moved-styling` was `_retext`: a block the source reworded *and* moved is written again
from nothing, and the reader's styling was carried over by folding the whole stretch
between two frozen runs into its first writable run, so every mark inside it went while
the report called the block merged. It now pairs the document's words with the merged
text (`_word_pairs`) and gives each one the document's own styling, through the run
builder `_restyled_words` already used (`_runs_from_styles`). Taking the entry out
uncovered two more under the same signature, since a signature with no words swallows
every styling loss there is: a block written from nothing inherits the styling of the
character in front of it (Docs' rule), so one moved under an underlined heading came
out underlined — `_style_requests` names every managed field on every run now,
whatever the run carries; and a word the reader styled that the source then rewrote
has nowhere to carry the styling to, which is right, but nothing said so —
`doc_merge.reader_styling_gone` names the word in the report.

And one of the ten was the **oracle's own** from beginning to end, twice over, which is
the third and fourth time the harness has been the thing at fault. A picture is identified by the file
it shows and not by the object id Docs gave it, because a block the sync rewrites comes
back with a new id — but the file is not stable either: a picture a reader inserted in
the browser has only a `contentUri` until the settle saves it and gives it a name and a
digest (`fetch_pictures`). The document had not changed; the name the oracle knew it by
had. `image_names` takes all of them and `_picture_findings` matches one picture at a
time against any, so a rewrite and a settle are both survivable and two copies of one
file are still two pictures. Thirteen findings became nine.

Then the nine, which were subtler and the same shape: a reader who pastes the same image
twice gives two pictures **one `uri`**, and a name two pictures share says nothing about
which is which. Matched on it, the picture that went was paired with the one that stayed,
the survivor was left over, and the oracle named the survivor — every one of the nine
pointed at a picture still standing in the document. `telling_names` keeps only the names
no other picture beside it carries, `pair_images` matches on those first and on anything
at all second (so two copies of one file still pair off one for one), and the excuse —
*the source took this picture out of the file* — is asked of the telling names too, or
the copy the source kept would answer for the one it dropped. It is worth saying what
that cost: for as long as the oracle has existed it accused the sync of losing every
picture a reader ever inserted, and then of losing whichever picture it had just watched
survive. Behind both accusations there was nothing at all.

And a fifth harness defect, uncovered by taking that entry out. Chips are counted by
value over the whole tab, and the excuse for one that goes is that the file still
holds one of that value — which credits a chip the source added *somewhere else*
against the one it is deleting here. A source that drops the block its person chip is
in and puts a chip of the same address into another block read as no change at all,
and then as a loss when the second block turned out to be one no request can write.
`_source_dropped` leaves out what goes with a block the source dropped and the reader
left exactly as the base has it — and only that, because a chip the reader put in is a
chip the merge keeps the whole block for, and if it ever stopped doing that the oracle
must still say so.

### What an empty `KNOWN` was worth, immediately

The rule above — an entry goes the moment its defect has a fix and a test, or it swallows
the next defect that looks like it — is easy to write down and uncomfortable to follow,
since the campaign is green either way and a wide entry costs nothing until it costs
everything. It paid on the first run after `KNOWN` went empty. At two seeds nobody had
used before (1200 rounds at chain 4 from seed 90000, 400 at chain 8 from seed 40000) the
campaign came back with **six** findings, and `block_gone … though the file still names
it` — which the departed `crossed-delete` covered word for word — was one of them. Four
were real losses in the merge; two were the oracle's own, which makes seven of the
harness's against thirteen of the sync's.

* **A table found again only if it is looked for twice** (seed 90190, `between_tables`).
  `anchor_tables` names the tables a structural batch built by what they follow, and one
  of those anchors may be a table the *same* batch has just stripped of its key: a regrid
  that deletes row 0 takes the cell the table is anchored in, which is why a regrid
  carries an `after` at all (the fourth bullet of the identity list above). The pass went
  through `shaped` once, in order. The moved table's anchor was not there yet, the regrid
  put it back a moment later, and nothing looked again — so the moved table stayed blank
  and unkeyed, the re-plan read the key the file still names as a table the *reader* had
  deleted, and the sync wrote the source's words nowhere. A whole table of the source's
  gone, with no conflict and no note. It runs to a fixed point now, anchors that are
  already there going first in each pass.
* **Two tables behind one anchor, told apart by `shaped`'s order** (seed 40204,
  `ends_on_table`). A table the source *adds* in front of one it *regrids* shares its
  anchor, and the order of `shaped` then decided which was which — although what the
  batch did with them is the order of the requests, and `insertTable` puts the blank new
  table in front. They came out crossed: the regridded table's key went on the blank one
  and the new table's key on the one holding all the words, the base took each other's
  content, and the next round read the real table as one the source had moved and emptied
  it. What tells them apart is what they *say* — a table built from nothing is blank and a
  regridded one still says what it said — so the words are asked first (`_blank_table`)
  and `shaped`'s order is the tie-break it always was.
* **"Left where the document has it" was only half true** (seed 40344, `between_tables`).
  Both places that take a move back — `refuse_nowhere`, when the new place has no
  paragraph to write in, and `restore_undeletable`, when the block cannot be deleted from
  the old one — cleared `moved` and left the block sitting at the *file's* position in
  `merged`. But every index the sync computes comes from a block's span, and a span says
  where a block **is**: a block that stays put while `merged` keeps it elsewhere is an
  anchor pointing at the wrong end of the document. The move of a table in front of such a
  paragraph was written at that paragraph's old index, which is where the table already
  stood; the document came back unchanged, the next round planned the same move, and the
  three rounds `_write_structure` allows ran out with the table blank, its words nowhere
  and an empty paragraph left over from each attempt. `_put_back` puts the block where its
  span says it is.
* **A block whose only change was a mark** (seed 90175, `dropdown`). `_edited` is the test
  that outranks a source delete, and the campaign's own history is written into it: not
  `block_text`, because a table is empty there; the cells, the grid and the frozen runs.
  Never the marks. So a block the reader had only *styled* read as untouched, the source's
  delete went through, and the bold went with the block — in silence, while
  `styling_lost` says everywhere else that bolding a word is a choice a reader made.
  `_styled` compares the styling by the stretch of text it covers, so splitting a run to
  bold a word and joining it again are both nothing.
* Twice the oracle's own. `joined_differently` lets a token both sides edited half of
  alone — a soft hyphen makes `soft\xadhyphen` one `\S+` token — but a reader may also
  *delete* one of the joined words, and the leftover `\xadhyphen` is a whole token the
  base does not have, so it read as one the reader had typed and the source rewording its
  other half read as a loss (seed 91197). `_pared_down` asks it exactly: a base token with
  the span of one of its words cut out, nothing looser. And `styling_restored` asked
  whether an un-marked word wears its mark *anywhere in the block*, so a second occurrence
  the source had just appended — "and willow", plus " and harbour" — answered yes while
  the word the reader pressed Ctrl+B on stood exactly as they left it (seed 40254). It is
  asked by occurrence now: how many the reader un-marked against how many are still
  un-marked.

Each has a test that fails when its mechanism is put back the way it was. After them,
both fresh campaigns and the two standing ones (400 at chain 4, 300 at chain 8) are clean
under `--strict`.

### And the run after that: going deeper rather than wider

Green at four settings is not green, it is four settings. The next run went deeper instead
of wider — 2000 rounds at chain 6 from seed 500000, so each round is six edit-and-sync
steps on one document rather than one — and came back with **five** more findings in four
signatures, three of them the same shape: a table the source added or regridded, gone with
all its words, `block_gone … though the file still names it` again. Three defects behind
them, one of which is the oracle's; a fourth was found by a sweep written to confirm the
second, and is the worst of the four.

* **A table anchored on an empty paragraph the same batch unnames** (seeds 501271, 501871).
  `insertTable` in front of an ordinary block leaves an empty paragraph, and
  `_new_table_requests` gets rid of it by deleting the mark of the block *before* — Docs'
  merge-on-delete keeps the first one's style, so both blocks come out as the file has
  them. Unless that block is itself an empty paragraph, which is all mark: the delete then
  covers its named range whole and it comes back unnamed. By itself that is nothing, since
  the settle keys it again from its words. But the new table is found between the batch and
  the settle, by the key of the block it follows, and that key was this one — so
  `anchor_tables` found no anchor, the blank table settled under a name made from its own
  emptiness, the re-plan read the key the file still names as a table the reader had
  deleted, and the source's table was gone. `_swallowed` names the block the batch is about
  to unname and `_after_key` looks past it.
* **A move whose two ends are one place** (seeds 501429, 500077). `_moved_keys` reads the
  file's order against the **base**, and the merged order is the **document's**, so a block
  the source moved can come out exactly where the document already has it. Writing it
  anyway is a delete and a build from nothing for no gain at all — and for a table it is
  destructive twice over: it is built again blank with its words waiting for the next pass,
  and the next pass asks for the same move again, nothing having changed, until the three
  rounds `_write_structure` allows are spent and the table is left blank. A paragraph got
  off with losing its key. A move whose two ends are one place is no move.
* **A block moved to the end past a table, written into it** (found by sweeping every
  reordering of a five-block body, which is how the scenario for the previous bullet was
  looked for). This is the move half of chain-8 seed 189, fixed a day earlier for deletes
  only: the index a block is appended at is the mark of the last block the sync **keeps**,
  and a block the source moved away is no more kept than one it deleted. Move everything
  after a table to somewhere in front of it and the last kept block is the table — whose
  own last index is inside its last cell — so the moved paragraph was written into the
  table, which swallowed it and took its key. The body cannot end on a table, so the last
  block's mark stays behind however it goes, and that leftover empty paragraph is the
  trailer. One condition now covers both halves.
* Once more the oracle's own (seed 500249). `WORD` is `\S+`, so a reader who moves a
  paragraph ending in a full stop against the `1` in a cell makes the token `.1`, which the
  base does not have and `theirs - was` therefore reads as a word of theirs. It is not one:
  its only word is the base's, and when the source's `edit_cell` rewrites that `1` the stop
  goes along with it — the merge said `.thicket`, with both edits in it. `_dressed_up` asks
  it exactly, like `_pared_down`, and one thing more: the base word has to be gone from the
  tab as well, or a base word the reader typed again somewhere new, with a stop after it,
  would be excused too.
* And the same thing the other way about (chain-4 seed 76101). A drag that carries a full
  stop *away* leaves `section` where the base said `section.`, which whole-token arithmetic
  reads as a word of the reader's just as surely; the word is the base's, only the
  punctuation is theirs, and `collide` making that word into `kestrel` is the source's
  right. `_undressed` is `_dressed_up` mirrored, condition and all. With it out, 1 of 400
  rounds at chain 4 cries wolf.

  Both of those were later **retired**, and the reason is worth keeping beside them: no
  reader can put a full stop inside a word by dragging a paragraph, and neither could the
  harness once `read_move_block` stopped reading its drop index off the document before the
  cut. See "A block being there at all, and the drag that made a document no reader could".

Each has its test, each verified by breaking its mechanism. Two of the three merge defects
were reachable only by chaining — the second needs the source to have reordered a body
*and* added a block to it — which is the argument for depth over breadth: at chain 1 the
file and the document are never far enough apart for the two orders to disagree the way
`_moved_keys` needs.

Fixed seeds from the campaign run in the default offline suite.

### Deeper again: the wound the reader makes, and four false alarms

The next run stayed at chain 6 and moved to fresh seeds (800 rounds from 970000, then
600 from 980000). **Seven** findings in four signatures; three were the merge's and four
the oracle's, and the merge's three are one wound seen from three sides.

* **A table the reader beheaded** (seeds 970200, 970567, and the pair 970705/970711).
  A table is anchored in its first cell (`anchor_span`), because the cells are the only
  text a table has of its own — so a person who deletes its first row in the browser
  takes its named range with them. Every other repair in `doc_merge` is for a range one
  of *our own* writes destroyed; nothing was looking at one a reader destroys. The
  read-back had a table with no key, the merge read the key the file and the base both
  name as a table the reader had deleted, and the source's own edit to a row the reader
  had kept was written nowhere. `recover_tables` pairs the two again and only where
  nothing is in doubt (`TABLE_MATCH`, and `TABLE_MARGIN` clear of the runner-up on both
  sides) — and it asks the **words**, not `_match_text`, whose " | " between every cell
  is most of a small table's characters: a blank 2×2 `insertTable` had just built scored
  0.55 against one with four words in it and took its key, which is the crossing this
  whole family is about.
* **The same wound, and `anchor_tables` looking only forward** (seed 970567,
  `between_tables`). `insertTable` splits the paragraph it goes into and the paragraph's
  named range stays with the half *after* the table, so a new table goes in front of the
  block the plan anchored it on. Where the reader had just made the next table along
  anonymous, the forward search handed *that* table's identity to the one the batch had
  built: the source's rows were written into the reader's table and a blank one was left
  for the rest, with the reader's row gone and the report saying nothing. The search
  looks backwards after it has looked forwards, and a blank table and one with words are
  told apart as they are everywhere else (`_blank_table`).
* **And the tab the merge plans nothing for** (seeds 970705, 970711). A tab the source
  deleted and the document changed is kept — which is a note and no pair at all
  (`pair_tabs`), so nothing plans that tab and the settle has no planned blocks to adopt
  from. The beheaded table there settled under a name made from its surviving first word,
  with file, base and document all agreeing on an identity the file never gave it, and
  the next sync would have built a second table beside it. `settle_keys` asks the base as
  well, and `doc_ir.name_requests` then plants the range back, so the repair reaches the
  document and not only the run's plan.
* Four times the oracle's own, and the first of them was made by the fix above. A table
  the reader beheaded reads as one the reader *made* — `unkeyed` is the oracle's word for
  that — and everything in it then has to survive, so the source's own cell edit read as
  a loss; `_tab_findings` asks `recover_tables` the same question before it judges. Then
  three in the styling, each an accusation with nothing behind it: an un-bolding the
  **base** already records, on a word the file itself now marks, is a source restyle of a
  block the document has not restyled since, which the merge's own rule gives to the
  source (seed 970228). A word is what a reader sees and not what a run holds — the merge
  writing the source's strike on the words the file has left a typed word in a run of its
  own, and the coloured token `\xadvellum` came apart into `\xad` and `vellum`
  (seed 970528, `_under_words`: a word wears what every character of it wears). And
  styling was counted in whole *sets* of marks, so the source adding its strike to a word
  made the tuple the reader's colour was in disappear and the colour read as lost while it
  sat there — one mark at a time now. The fourth: two copies of one picture, one of which
  the source drops. Which copy survived and which the file still asks for are told apart
  by object ids the survivor need not keep, so name-matching put the two roles on
  different copies and named the one that went; the question is how **many** the file
  asks for, not which (seed 980193).

Each has its test, each verified by breaking its mechanism. After them 970000 is clean,
and so are 600 rounds at chain 6, 400 at chain 8 and 900 at chain 4 from fresh seeds,
all under `--strict`.

### The third read, and the style a delete hands over

Two more at fresh seeds, 500 rounds at chain 10 from 992000 (clean) and 700 at chain 6
from 993000 (one), plus a seed left over from the round before.

* **The read nobody recovers a table for** (seed 993608, `ends_on_table`). A sync that
  changes a grid writes it on its own, reads the tab again and hands what it finds to
  `anchor_tables`, whose job is to name a table whose range one of *our own* requests
  destroyed — a regrid that deletes the row the table is anchored in. A table the
  **reader** beheaded has no range either, and that third read was the one place
  `recover_tables` was not run: the free table `anchor_tables` found was the reader's,
  and the regridded table's key went onto it. The reader's rows then stood under the
  source's table's name, the real one settled as `table:empty`, and the key the file
  names was gone. It is recovered before the anchoring now, in `doc_sync._write_structure`
  and in the harness's copy of it, and `plant_ranges` puts its range back in the same
  breath. (What made it hard to see: `anchor_tables` prefers a *blank* table for a new
  one and a worded table for a regrid, so the crossing needs a regrid that leaves the
  table blank — and the reader had emptied the row the source's regrid kept.)
* **The style a delete hands over reaches the measurements** (seed 912452, `themed`).
  Docs merges two paragraphs on a delete keeping the first one's style, which
  `carry_unimported` already repaired for the named style and the bullet. It hands over
  the alignment and the spacing too: a paragraph the reader centred, deleted by the
  source in the same batch, leaves the block behind it centred **of its own** — although
  the merge had written `alignment` named-and-unset one request earlier, because
  following its named style is the whole point of a theme — and the source's own restyle
  of that block, written in the same breath, is handed back the spacing of the paragraph
  that went. The settle writes the plan's **whole** paragraph style back on a block this
  run wrote whose style the write did not leave as the plan asked (`_unwritten`,
  `paragraph_written`), rather than the difference: the repairs read each other's work
  otherwise, the named style deciding what "inherited" means. A block still read as a
  HEADING_1 under a theme that centres headings reports no alignment of its own, and the
  centring shows only once `named` has written NORMAL_TEXT back — which is in this very
  batch. It is the one thing in the settle that takes styling away, and what keeps that
  safe is the narrowing: only a paragraph this run wrote, whose own field is being taken
  back. A block nobody wrote is left alone, so a reader's styling is never undone
  (`test_styling_the_plan_does_not_ask_for_is_never_taken_away` fails without it).

That second one also heals a *plan* that pins an inherited alignment, which is the
defect `test_the_campaign_sees_a_theme_undone` puts back on purpose, so that probe now
opens both doors: what it measures is the oracle's reach, not which of our own
mechanisms is broken.

After them: 700 rounds at chain 6 from 993000, 400 at chain 10 from 995000, both clean
under `--strict`. 500 at chain 8 from 994000 came back with three, two of them the
`frozen_gone` signature that is still open.

### The copy that answers for another, and a paragraph that follows a table away

The three left over from 994000, two signatures, one each side of the line: one the
oracle's, one the merge's.

* **A picture's names do not say which copy it is; its object id does** (seed 994410,
  `equations`, shrunk to three steps). The source adds the same figure twice, then the
  reader deletes one of the two blocks in the browser while the source drops the other.
  Nothing is lost by either — but the copy still standing paired, by their shared digest,
  with the file's entry for the copy the reader had *already* taken away, so the count of
  what the file still asks for came out one too high, nothing was excused, and the picture
  the source itself gave up was named as lost. The excuse pairing asks for the ids now
  (`pair_images(..., ids=True)`): the file's ids are the document's own, since the settle
  regenerates the file from the document it wrote, so a picture the file names by id *is*
  that object and one it names by file alone is a picture the source has just added and
  the document has never held. The other pairing — the document before the sync against
  the document after — still goes by name, because a rewrite gives a picture a new id;
  that asymmetry is the whole of `frozen_key`'s docstring.
* **A block kept because nothing can move it follows nothing that moves** (seed 994424,
  `two_tables`). An empty paragraph between two tables can be deleted in no way at all, so
  one the source dropped is kept where the document has it (`restore_undeletable`), and
  `_after_live` puts it back into the merged list behind the block in front of it *there*
  — which was the very table the source was moving somewhere else. The kept paragraph then
  stood in the merged order as that table's own next block, so `_insert_index` read the
  table's new place off a span a single character behind where the table already was: it
  was deleted and built again in its own place, blank, and the next two passes did the
  same. The three rounds `_write_structure` allows ran out with the table's words written
  nowhere and the file's key gone, and the report said nothing. It is the sibling of
  `test_a_move_the_merge_takes_back_leaves_the_block_where_the_document_has_it`, and the
  same sentence fixes it: `_after_live` looks past a neighbour the source moves.

Then 800 rounds at chain 4 from 998000, 500 at chain 8 from 996000, 400 at chain 10 from
997000, 700 at chain 6 from 991000 and 300 at chain 12 from 999000 — 2,700 rounds, all
clean under `--strict`, and `KNOWN` still empty.

### The name that stayed behind

One round in 500 at chain 8 from 41000, and the only failure in the 3,200 rounds run
since: `identity_lost paragraph:second-section — the block said 'meadow section.' and
has lost its key, though neither side dropped the block`.

A range is destroyed with its text or not at all. That is Docs' rule and the reason
`replant_requests` exists — but there is one delete that takes no text of the block it
is deleting. A block in front of a table gives up the *previous* block's paragraph mark
and keeps its own (`_delete_range`, and the API refuses anything else), and a range can
live exactly there: an empty paragraph is all mark, so its range *is* the mark, and a
reader's chip or word pushes an ordinary paragraph's range onto the mark too
(`doc_ir.apply_keys` records where a range really is, which is what made this visible at
all). Nothing of the range's own text is deleted, so Docs keeps the range — on a mark
that now belongs to the paragraph the two were merged into. The document goes on saying
this block is there.

It costs identity twice over, and the second time is the one that bites. The block was
*moved*, so its range is planted again where it went and the document holds two ranges
of one name; and the stale one sits where the next block written will be, so the sync
after hands *that* block this key and the block that owned it is renamed from its words.
Which is why the report said a block neither side dropped had lost its key: it had been
given away.

`_orphan_range` names and deletes the range in the same batch, wherever the text it sits
on is not the text going. Both halves are pinned by hand
(`test_a_delete_that_borrows_the_mark_in_front_names_the_range_it_leaves_behind`, and
its counterpart saying an ordinary block's range — which stops short of its mark, so the
borrowed-mark delete covers it — is left to Docs); without them, seed 41000 and the
first of the two fail and nothing else does. 500 rounds at chain 8 from 41000, clean.

The empty paragraph this round turned on is itself a thing worth naming: a table the
source adds after a table of contents cannot swallow the leftover of the paragraph
`insertTable` splits, because the block before the insertion point is structural. The
spurious empty block survives the write and the settle keys it. It is legal, it is
harmless, and it is one more reason the identity of a block may never depend on a block
being there for a reason.

### The tab strip, from the reader's side

Everything the campaign knew about tabs, the *source* did: `add_tab`, `drop_tab`,
`rename_tab` were all source ops, and only the rename had a reader counterpart. So the
two commonest things a person does in a tab strip — clicking **+**, and deleting a tab —
had never been drawn, although `pair_tabs` has a rule for each of them
(`read_add_tab`, `read_drop_tab`; the second is told which tabs there are, the way
`read_unmark_word` is told what the theme sets, because which tabs exist is not
something the tab it is looking at can say).

Both rules hold. A tab the reader added is in neither the file nor the base, so the
merge plans nothing for it and the settle reads it into the file with keys and named
ranges of its own — which is what makes the sync after it write nothing rather than see
a tab the file has and the document does not. A tab the reader deleted stays deleted,
its `<section>` leaves the file and its entry the base, and when the source had changed
that tab the report says the changes went nowhere. 500 rounds at chain 6 with the two
ops in, clean.

What the round-trip did turn up is a hole in the **oracle**. Put the resurrection bug
into `pair_tabs` on purpose — one line, a tab the source still asks for and the reader
deleted goes into `create` — and nothing objected: the document converges, every word
is present, and the tab that comes back carries a *new* id, so the first version of the
test, which asked for the old one, passed as well. Nothing of the reader's disappears
when their deletion is undone; it is the decision that is gone, and this oracle only
ever asked about content.

`_resurrection_findings` (`tab_resurrected`) asks the other question, narrowly: the base
had that tab, the read before the sync does not, the file still asks for it, and
something now stands in the document under the same name saying the same words, unknown
to the base. With it in, the injected bug fails the campaign at round 17 of 80 and the
shrinker cuts it to the two new ops and nothing else —

```
shape imported_list
step 0: reader add_tab | source
step 1: reader drop_tab | source
```

— which is as small as a script gets.

### The same hole, one level down

If the oracle could not see a tab the reader deleted come back, it could not see a
*block* come back either — and that is the commoner journey by a long way. The same
experiment says so: take the clause out of `_merge_block`'s placing loop that says a
key in the base and not in the read-back is a delete that stands, so every block the
reader struck out and the file still asks for is written again, and 80 rounds pass
without a word. Every sentence is present, every round converges, and the reader's
deletion is quietly undone on every sync for ever.

`block_resurrected` asks it the way `tab_resurrected` does: the base had the block, the
read before the sync has not, the file still asks for it, and it is there again
afterwards. One thing had to be learned to ask it without crying wolf. **A move in the
browser is a delete and a retype**, so a block the reader dragged loses its named range
and its key exactly as a deleted one does, and the settle names it from its own words
again — which is indistinguishable from a resurrection if you ask about keys. Ask about
the words instead: they never left the document. And about the *words*, not the text,
because a moved block that held an equation comes down without it (no request makes
one), so the two never read alike. It is the forgiving direction on purpose — a block
whose every word still stands somewhere is let go — because the other way round accuses
every move.

Measured over 300 rounds at chain 4: with the clause in place, 0 failed — 7 before the
move was told from the deletion, every one of them shrinking to a lone `move_block`.
With the clause taken out, 68 failed, shrinking to a lone `delete_block`.

### And once more, at the size of a row

A row is the third size the same question has, and the last one: below a row there are
cells, and a cell the reader emptied is words and not a decision. A row carries no key
of its own — the merge knows it by what it says (`doc_merge._table_lines`) — so
`_row_resurrection_findings` knows it the same way: a row the base has, the read before
the sync has not, and the table has again afterwards.

It found a real defect on its second campaign, and one that no amount of staring at
`_table_lines` would have shown, because `_table_lines` is right. The sync writes a
table's grid in a batch of its own and then reads the document again, and `rebase_tables`
moves the base onto the grid that was just written. A row the *reader* deleted is not in
that new grid, so the rebase takes it out of the base — and with it the only thing that
said which of the **file's** rows it was. The round after the regrid then finds that
file row matched to nothing, reads it as a row the source has just added, and inserts
it. The reader's deletion is undone, the report says `inserts a row` twice, and the
notes are empty (offline seed 63138, chain 4, shrunk to `delete_row` against `regrid`).

What was missing is a fact the merge knew and threw away, so it is carried rather than
guessed again: `_table_lines` gives back, beside the merged rows and columns, the file's
lines it has **settled as not in the grid**, `_rebased_table` puts them in `aligned`
next to the matchings it already records there, and `_merged_lines` counts them among
the lines it need not add. The knowledge accumulates over the rounds, since a later
round's own settlement is unioned with the one it inherited.

Calibration, as for the other two: with the settlement carried, 300 rounds at chain 4
and 200 at chain 8 pass. Taking it back out fails 1 of 200 at each depth — thin, which
is why the defect is pinned deterministically by
`test_a_row_the_reader_deleted_is_not_put_back_on_the_pass_after_the_regrid` as well.

The row check also cost the other two a lesson in forgiveness. `block_resurrected` asks
whether the block's words are still in the tab, and three chain-8 rounds said they were
not when the reader had merely dragged the block: the harness's drag is a cut and a
retype at the index the reader dropped on, which lands *inside* the full stop of the
paragraph before it (`section.` becomes `section..`) or, when the block held a chip no
`insertText` can retype, closes the gap the chip left (`harbour grace` becomes
`harbourgrace`). `WORD` is `\S+`, so both read as words that went away. `stands_elsewhere`
asks for the words *inside* the tab's text instead — the forgiveness `joined_differently`
already grants a single token, granted to a whole block — and both resurrection checks
use it. It costs nothing that matters: with the block delete broken on purpose, 34 of 200
rounds at chain 4 still fail, 68 findings.

And one at the size of a tab, from the other side: `tab_resurrected` asks whether some
tab unknown to the base now says the same thing under the same name as the one the
reader deleted — and a tab the *source* freshly asks for is word for word that shape,
since two new empty tabs say exactly as much as each other. The campaign draws tab
titles from a small vocabulary, so two `add_tab`s eventually pick one name (chain-8 seed
65370), and the merge creating the second was accused of resurrecting the first. A
`<section>` with no `data-tab` is a tab the document has never had, so `_fresh_asks`
counts them and each answers for one new tab before any of them is called a
resurrection. With the excuse in place, putting the resurrection back into `pair_tabs`
still fails 26 of 200 rounds at chain 4.

And the forgiveness was still aimed at the wrong text. `block_resurrected` asked whether
the *base's* words were still standing before the sync — but a base is what the document
said one sync ago, and everything the reader's own hand has taken out of that block since
is missing from it by right: a chip no retype carries, a word they went on to delete.
Chain-4 seed 66195 is a paragraph the source gave a person chip, which the reader then
dragged: the drag retyped the text, the chip stayed behind, and the settle keyed the same
untouched paragraph from its own words again — nothing created, nothing written, and a
`block_resurrected` for the chip's name. The question belongs on the block that carries
the key *after* the sync: if what stands there now was standing there before it, nothing
came back at all. A real resurrection puts back words the document did not hold, so it
still fails — with the block delete broken on purpose, 44 of 200 rounds at chain 4, 91
findings.

### The column nobody had ever drawn, and the third judge

Coverage is the campaign's own account of itself, and it said something plainly that
nobody had read: `insertTableRow` and `deleteTableRow` were planned in every run, and
**no column request ever was**. `src_regrid` added and dropped rows only, and the
reader's ops were `add_row` and `delete_row`. So `_column_score`, the column side of
`_align` and the column side of `_merged_lines` — the half of `_table_lines` that the
documentation above describes at length, "columns by their words … so both sides may
regrid, and rows and columns may change at once" — had no measurement behind them at
all. `read_add_column`, `read_delete_column` and a `src_regrid` that draws a column half
the time put that right: 1,700 rounds at chains 4, 6 and 8 with 235 column requests
planned in the concentrated run, and nothing failed.

Which proved less than it looked. Pairing columns **by place** instead of by their
words — the straw man anybody would write first — also passed, 200 rounds at chain 6 on
`two_tables`, without a murmur. Both of the campaign's judges are blind to it, and each
for its own reason. The loss oracle asks about the *reader's* work and says so in its
first paragraph; a column matched to the wrong column never deletes anything of the
reader's, because a line the base and the document disagree about is never `gone`
(`_merged_lines`), so a wrong matching errs towards keeping. And convergence is blind
for a sharper reason: `rebase_tables` writes the matching it used into the base, so the
second sync makes the same reading of the same table and writes nothing. **A merge can
be wrong and stable at once.**

So there is a third judge now, `fuzz_docs._arrived`, and it belongs to the campaign
rather than to the oracle — which is where the oracle's own docstring had always put it
("what this module does not judge: whether the source's changes arrived"). It asks two
questions, both the narrowest that do the job:

* a table **the reader did not touch at all** must come out of the sync at the grid the
  file asks for. There is nothing to merge in that case, so no merge rule can stand in
  the way, and the only excuse is the report naming the table;
* a cell the source rewrote, in a table whose words the reader did not touch — they may
  have added and deleted rows and columns, they may not have written — must be somewhere
  in the table when the sync is over, unless the reader deleted the line it was in.

The second is the one that sees the column matching, because the first cannot: with the
reader's hands off the grid entirely, pairing by place and pairing by words agree, the
base grid and the document's being the same grid. What tells them apart is the reader
*regridding* while the source edits a cell. `src_edit_cell` writes a distinctive token
now rather than a word out of the shared vocabulary — a cell edit nobody can tell from
the cell beside it is one no judge can follow — and with that, pairing columns by place
fails 3 of 200 rounds at chain 6 where it had failed none.

And the judge found a real defect on its first outing, in the other half: a table the
source **regrids and moves at once**. A move is a delete and a table built again blank,
so the shape it is built at is the whole of the question, and `_merge_table` lays a trap
for it — when the merge also regrids it returns *before* it merges the cells, so the
block's own `rows` are still the document's. `_size` of those is the grid the sync was
about to change, and the table came back with an empty row on the end: the source
deletes the header row and moves the table, and the document ends up 2×2 holding
`[['1','2'], ['','']]`. `_built_size` counts the lines the matching settled instead.
Nine findings over 2,100 rounds, at every chain depth and in three shapes, shrunk to
three source ops and **no reader at all** (offline seeds 88033 and 88075) — a defect
a whole campaign built around the reader could never have reached, which is rather the
point of having a judge that is not about the reader. With the fix: 2,800 rounds at
chains 4, 6 and 8, nothing failed, the judge asking some 3,100 questions in them.

### The same question for a paragraph, and the twin that took its key

The narrow half of `_arrived` asks about a grid. The same question for a paragraph is
plainer still — the reader left the block **word for word** as the base has it and the
source reworded it, so there is nothing to merge and the file's words must simply be
there at the end — and nobody was asking it either. `_words_arrived` does, and it costs
nothing to ask: over 900 chain-4 rounds it asks 1,261 times, over 450 chain-8 rounds
1,315.

Two things had to be got right before it would say anything true. A chip's **face is
the document's to draw** — the file asks for a person chip reading `Grace`, Docs renders
`grace` off the address — so the comparison is `_says`: the block's own words, plus its
frozen runs by what they *are* (`oracle.frozen_marks`), which is the oracle's own answer
to the same question. And the finding is `wording_lost`, not `words_lost`: the oracle
already has that kind and it asks the opposite question (words of the *reader's* gone
from a block), and two checks under one kind make one `KNOWN` entry mean two things.

It found a defect straight away, and a bad one. `adopt_keys` is the one place that gives
a block written from nothing the key the plan meant it to have — a rewrite is a delete
and a write, and the delete takes the block's named range with it. It matched the plan to
the read-back through a **dictionary of words**. A reader who pastes a paragraph makes a
second block word for word the same, and a document is full of blocks that read alike; so
when the copy stood *in front* of the original and the same sync rewrote the original
(the source had added a chip to it), the walk down the document reached the copy first and
handed it the key the file had carried since the push. The source's chip landed on exactly
the right block — and that block then settled under a name nobody asked for, with file,
base and document all agreeing on it. Convergence is blind to that by construction, and so
is the loss oracle: nothing of the reader's went anywhere.

Order is the information the dictionary threw away, and there is plenty of it: the plan is
the merged block list and the read-back is the document written from it, so the two are
very nearly the same sequence. `doc_merge._adopt_in_order` aligns them and pairs inside
every matching run. On the **words** and not the shape — Docs merges two paragraphs keeping
the first one's style, so the shape is the very thing a write changes, and a heading the
source had moved *and* restyled came back under neither pass, the alignment refusing it for
its shape and the words pass for its two pasted twins (seed 96300, which the oracle *could*
see, as `identity_lost`, because that key landed on nobody at all). And only where the two
sequences hold the same number of blocks saying that thing: one plan block against three
identical ones in the read-back has an alignment too, and it is a guess, which is the defect
this whole family is about.

Measured: with the pass taken out, seed 94000 fails 2 of 700 rounds at chain 6 and 96000
1 of 1,200 at chain 4. With the pass out **and `_words_arrived` out**, those 700 rounds
pass in silence — which is the judge's whole case.

It also paid for itself twice over in the oracle. `_twin_unmarks`, written a few days
earlier to forgive an un-bolding standing on a block's pasted double, was forgiving a
symptom of this very defect; with the cause fixed it excuses nothing (3,750 rounds at
chains 4 to 8 without it, nothing found) and is gone, because an oracle that forgives what
no longer happens is a blind spot waiting for the next defect that looks like it. And
`_pared_down` was recognising only half of its own case: deleting `soft` out of
`soft\xadhyphen` leaves `\xadhyphen` if the reader stops at the word and plain `hyphen` if
they sweep the soft hyphen up too — the same deletion, and only the first was known, so the
second read as a word they had typed and the source's rewriting of the other half looked
like a loss (seed 94030, 1 of 700 rounds at chain 6 without the widening). The cut may now
take the joiner with it, guarded by the narrow leftover not standing there as well: a base
token pared down once leaves one token, so `hyphen` beside `\xadhyphen` is a word the reader
typed and still has to survive.

### The look the source asked for, and whether it arrived

The judge's third half asks the same question about a block's **look**. The reader left
it word for word as the base has it, the source restyled it, so again there is nothing
to merge: what the file asks for has to be what the document says at the end.
`_styling_arrived` asks it of a block's marks (`_worn`: the run styling as styled
stretches of text, adjacent alike ones joined, so the one thing that does not matter —
how many runs the text was cut into — is the one thing it cannot see) and of its
paragraph (`_shape`: the named style and every measure the merge owns). Over 400 chain-4
rounds it asks 118 times about marks and 233 about paragraphs; over 400 at chain 8, 210
and 454.

Neither other judge can ask it. The loss oracle asks about the **reader's** work, and a
source restyle going nowhere costs the reader nothing. Convergence is satisfied by any
*self-consistent* reading, and the settle regenerates the file from the document it just
wrote — so the second sync agrees with the first about a look neither of them has.

It found one defect in each of the three places there are.

**The harness.** `dict(para)` is a shallow copy and a paragraph's `measures` is a dict of
its own, so a paragraph split off another went on sharing the very dict its measures live
in. Every block a sync appends is written `"\ntext"`, which is a split, so one
`updateParagraphStyle` set the line spacing of half the body — and the next request to
clear a measure cleared it everywhere too, which is exactly why nothing saw it: the
document stayed self-consistent and the base agreed with it (`doc_world.copy_para`; seed
110149, 3 of 200 rounds at chain 4 with the shallow copy back).

**The merge.** `styles_of` counts run boundaries, on purpose: a mark applied to part of a
run splits it, so the boundaries are where the information is. Asked of the *document* it
answers something else as well — a reader who inserts a picture or a person chip splits a
run and puts a frozen one between the halves, which is content the text merge carries and
says nothing about marks. The merge read it as the reader restyling the block, concluded
both sides had, and dropped the source's marks. `doc_merge.marks_of` is the question "did
somebody change what these words are marked with": the frozen runs left out, adjacent
alike stretches joined, `styles_of` keeping its exact meaning where a rewrite needs it.
Seed 110265; without it 6 of 400 rounds fail at chain 4 and 10 of 400 at chain 8.

**The report.** Both of those were found through a silence. Where both sides set one
paragraph, or both restyle one block, the document wins — as it does everywhere — but it
used to win with nothing said. The words have raised a conflict on every clash since the
beginning; the styling, settled the same way, said nothing, and a source that centres a
paragraph or gives a word a face has said something a person will look for. Two notes now
(`_merge_block`), and the second one is why the first defect had to be fixed in `marks_of`
and not in the report: the judge, like the oracle, forgives what the report names, so with
`styles_of` back **and** the note in place the campaign passes — while the report tells
the person that their document restyled a block it never touched. A note that is only ever
true is the whole of what makes it an excuse.

And twice the judge's own, both the same mistake: normalising the file's side less than a
read normalises the document's. A read reports only what a paragraph or a run sets *beyond*
its named style (`doc_ir._named_defaults`), so a `bold: False` left in the file by a source
that has since retitled the heading which made it meaningful says nothing at all (seed
220012), and so does a source centring a heading its theme already centres (seed 96300).
`_worn` subtracts a mark whose truthiness is the style's, in both directions, and `_shape`
subtracts any field whose value the style already gives — taken from `_named_defaults`
itself, so the judge normalises the file exactly as the reader normalises the document.

Then 2,100 rounds clean under `--strict` at four fresh seeds: 800 at chain 4, 600 at
chain 6, 400 at chain 8, 300 at chain 10.

The two notes are for **blocks**. A cell has no key of its own, and the base a cell is
merged against need not be what that cell said last time: a row or a column one side has
just added pairs with nothing, so every cell of it reads as both sides having styled and
set it. The note would then open with "a table cell", which names neither the table nor
the cell; a table's own report is `_merge_table`'s, which can at least say which table —
and that is now what a cell's report says too (see "A cell's report is the table's").

**A row's words are evidence, not the row** (chain-6 seed 260208, the oracle's). The base's
rows say `thicket` and `meadow`, the reader deletes `thicket`, the source rewrites `meadow`
into `thicket`, and the one row left rightly says `thicket` — which
`_row_resurrection_findings` read as the deleted row coming back, the source having spent
its words elsewhere. They are counted now rather than looked up: the reader took the count
to what the document shows, the source has raised it by `file - base` of its own accord,
and a row over that sum is one that came back. Where the source leaves those words alone
the sum is what the document shows, which is the question as it was — and with the
inherited settlement of `_table_lines` put back in memory the campaign still catches the
defect the check was built for (seed 63000, chain 4).

### Where a table may not stand, a face nobody typed, and an indent a bullet ate

Three more at fresh seeds, one of the merge's and two the judges'.

* **A table written right behind another is a table written nowhere** (chain-8 seed 280039,
  shrunk to a `move` and a `restyle`). Docs keeps an undeletable paragraph between two
  tables, so a file asking for two with nothing between them asks for something the
  document cannot hold: `insertTable` splits the paragraph at the index it goes to, the
  half in front of the new table becomes that mandatory paragraph, and the file has no
  block for it. Nothing then says which of the two empty paragraphs is which, and where
  the second is the body's last it is hidden altogether (`doc_ir._hide_trailer`), so the
  settle keys the leftover with the name of the paragraph the file wanted *after* the
  table: the order reads as the base's, the move is undone in silence, and a restyle
  planned for that paragraph in the same run is planned onto nothing next time round.
  `doc_merge.refuse_back_to_back` leaves the table where the document has it and the
  report says why — the sibling of `refuse_nowhere` and `restore_undeletable`, the other
  two places where Docs' own shape outranks the file's. The mirror case is not this one:
  a table written in *front* of another goes at the mark of the paragraph before it and
  the empty half lands between the two, which is where Docs wants a paragraph anyway.
* **A chip's face is not words anybody typed** (chain-8 seed 280398, the oracle's). The
  file says `Grace` and Docs renders `grace` off the address; a date chip re-inserted from
  its value comes back in whatever form the document spells a date in. Counting the face
  as words made a block the source merely *moved* — a delete and a write, so every chip in
  it is inserted again — read as losing the `Sep 20, 2026` the reader's own Backspace had
  brought into it. `oracle.own_words` leaves the frozen runs out, which is `_says` in
  `fuzz_docs` asked from the other side; a chip that really goes is caught by
  `frozen_marks`, which counts it by what identifies it and not by what it reads as.
* **An indent a bullet ate** (chain-10 seed 290010). Docs' merge-on-delete hands a block
  the whole style of the paragraph deleted in front of it, bullet and all, so a plain
  paragraph the source had just indented came back an *item* — and an item's indents are
  the list preset's, belonging to neither side, so `_unwritten` left them alone. The
  settle took the bullet off in the same batch (`restore_bullets`), and the block ended
  with neither the bullet nor the indent, the report saying nothing. The question is
  whether the block is an item once the settle has **finished**, not what the write
  happened to leave: the plan's kind decides (`_paragraph_fields`), because the settle
  writes the bullet to match it, and the plan's kind is the source's only where the
  document kept the base's (`_take_shape`), so a reader who made a list item in the
  browser still keeps it. The delete goes first now, too —
  `deleteParagraphBullets` keeps the nesting by adding indents of its own, so a style
  written before it is a style it then edits, which is the order `_paragraph_requests`
  has always used. 1 of 200 rounds at chain 10 with the old rule put back in memory.

Then 1,000 rounds clean under `--strict`: 300 at chain 10 from 290000 and 500 at chain 6
from 310000, with 800 at chain 4 from 270000 and 600 at chain 6 from 300000 before them.

**And the name a new table's swallow takes** (chain-4 seed 330127, shape `between_tables`).
`insertTable` leaves an empty paragraph in front of the table, which
`_new_table_requests` gets rid of by deleting the mark of the block before it — and a
block that is *itself* an empty paragraph is all mark, so the delete covers its named
range whole and Docs drops it. `_swallowed` has known that since seed 501271 and said, in
so many words, that nothing is lost by it because the settle keys the block again from
its words. That is true of the settle and not of what comes before it: a structural batch
is followed by a **re-plan** against the document it has just written, and there the key
the file asserts names nothing, so the block reads as one the reader deleted and
everything the source asked of it is dropped. Here the source had put a person chip in
the paragraph between two tables; the chip was written nowhere, and the round converged
with file, base and document all agreeing on an empty paragraph, so neither the loss
oracle (nothing of the reader's went) nor convergence could see it — only
`_words_arrived`, and only because `_says` counts a chip by what it *is*.
`doc_merge.recover_swallowed` gives the key back between the batch and the re-plan, after
`anchor_tables` because the survivor is found by the table it stands in front of, and
`plant_ranges` puts the range back in the same breath — the third repair to hang off that
one read, beside `recover_tables` and the anchoring. 900 rounds at chain 4 from 330000
clean afterwards, with 400 at chain 8 from 320000 beside them.

### The order nobody was judging, and the level no request can write

`_grid`, `_cells_arrived`, `_words_arrived` and `_shape_arrived` ask whether the source's
words, grid and look arrived. Nothing asked about the **order** — and a source move that
never arrives has exactly the shape this whole apparatus was built for: it takes nothing of
the reader's, so the loss oracle is silent, and the settle writes the order the document
ended up with into the base, so the next sync agrees with itself and the round converges.
`fuzz_docs._order_arrived` asks it, narrowly: only where the reader left the shared keys in
exactly the base's order, so no merge rule can stand in the way, and only of **pairs** of
keys, because which block "moved" is not a fact about two orders — swapping a paragraph and
the table after it reads as either of them moving, and the merge may refuse and name the one
the file reads as having stayed. A pair is explained where the report names either of its two.

It found four things, and two more came out of the same corner while it was being written.

* **A refused move is not a move, in the merged list either** (chain-4 seed 380191). All
  three places where the merge refuses one — a block between two tables, one in front of the
  table a body opens on, a table right behind another — cleared `moved` and left the block at
  the *file's* position in `merged`. Every index the sync computes comes from a block's span,
  and a span says where a block **is**, so the refusal had to put it back where the document
  has it (`doc_merge._put_back`). Two of the three already did; `structure`'s did not, and the
  table then stood in the merged order where only the file had it, which is a lie the pass
  after reads as truth.
* **`_moved_keys` was not a longest common subsequence** (chain-6 seed 400186). It read the
  file against the base with `SequenceMatcher`'s matching blocks, which are *contiguous*, so a
  closing paragraph sent to the front made every block it passed read as moved: four moves
  where one would do, and among them a table whose move was then refused because the reader
  had regridded it — the order came out neither side's with nothing saying so. It is the
  longest increasing subsequence of base positions now, which is the shortest set of moves
  that explains the file. And the moves go in **before** the additions, because an addition is
  placed after the block the file puts it behind and a move does not carry what stands behind
  it: a picture added behind an item the source moved was left where the item had been, and
  the block after *it* then read as standing in its place already, so its own move was refused
  as one whose two ends are one place.
* **A refused move drags its followers, and the report only named the one that was refused**
  (chain-10 seed 450252). A source that moves a section moves its blocks one by one, each
  placed after the one the file puts it behind — so where that one's move was just refused,
  this one does not go where the file has it either, and the next along is stuck on this one
  in its turn. Nobody could read that off the line naming the table. `_apply_source_moves`
  keeps a `stuck` set and says it, for a block that then does not move at all *and* for one
  that moves only partway.
* **A mark the reader moved from one word to another** (chain-6 seed 400044, the merge's).
  `marks_of` is a sequence of distinct mark-sets with no words in it, so a reader who takes a
  bold off one word and puts it on another is invisible to it — and `_restyled_words` then
  wrote the file's styling onto every word the file has, taking the reader's bold back off.
  It writes only what the source really *changed* now.

And the level. **No request sets a bullet's nesting level**: `createParagraphBullets` says
nothing about one, and Docs reads it off the paragraph's leading tabs, which the merge does
not write. So a level lives on a paragraph mark and survives exactly as long as that mark
does, and there are three ways for it not to — a block **written from nothing** (one the
source added, or moved, a move being a delete and a write) comes out at the level of the list
it lands in, whether that is deeper than the source asks for (chain-4 seed 430296) or
shallower (chain-8 seed 530265: a level-0 item moved to the end of a document whose last item
the reader had indented — the note fired only for an item *asking* to be nested, so the
commoner half was silent); and a block whose **paragraph mark a delete in front of it hands
over** wears the deleted one's level, a run of deletes passing the first one's along (chain-4
seed 430587). Everything else about that style is put back — the named style and the bullet by
`carry_unimported` / `restore_bullets`, the measures by the block's own restyle — and the
level alone cannot be. `doc_merge.unwritten_levels` says so before the write, which is the
only honest thing left: the reader having touched nothing, the loss oracle has no question,
and the base agrees with the document afterwards, so the round converges.
`fuzz_docs._shape_arrived` is the judge that is not satisfied. *Which* list it lands in is
which way round it is written, and that took a third seed to get right — see "The level an
item takes is the list it is written into" below.

### A row the reader deleted, a range two deletes claimed, and a repair undone by the request after it

Four at fresh seeds, three of the merge's and one the campaign's own.

* **The document deleted a row the source wrote in** (chain-10 seed 480066). Deleting a row is
  a change to the grid, and the grid is the document's, so the source's words in that row go
  with it — nothing to merge and nothing to keep. `_table_lines` had the mirror note since it
  was written ("the source took away a row, but the document wrote in it — kept") and not this
  one: the file's line was settled out of the merge in silence. It is asked of the cells of the
  *other* dimension's lines both sides share, as `_line_unchanged` asks, so a column the source
  added beside them is not mistaken for news about every row it crosses.
* **A range two deletes of one batch both claimed** (chain-6 seed 550667), and this one killed
  the sync where it stood. A range is destroyed with its text or not at all, so a
  `deleteNamedRange` for one already gone is refused — and a refusal throws out the whole
  batch. `_orphan_range` asked only whether *this* delete covers the range, and a run of
  deletes hands each mark leftwards: the block in front of the table gives up its neighbour's
  mark and keeps its own, which the neighbour's own delete then takes. Every cut of the batch
  is asked now.
* **The settle undoing its own repair** (chain-4 seed 570181). Docs merges two paragraphs
  keeping the first one's style, so a paragraph the source retitles behind an item the source
  deletes comes back a bulleted NORMAL_TEXT line. `carry_unimported` sees both,
  `restore_bullets` takes the bullet off and `unimported_requests` writes TITLE — and then
  `bullet_requests`, reading the block as the *read-back* has it, counted it in the run of
  items behind it and re-bulletted the lot, a paragraph-wide request that flattened the named
  style again. What a run is made of is what the settle **leaves behind**, not what it found
  (`_settles_as_item`) — the same lesson as seed 290010's indent, one request further on.
* **The cell judge's own blind spot** (chain-4 seed 570177, the campaign's). `_cells_arrived`
  recognises a regrid by the grid's *size*, so a source that takes one row out and puts another
  in slips past it, and every cell below the one that went reads as rewritten with the row
  above's words. The reader had deleted a different row, both deletes stood and the merge was
  right. A word the base already says somewhere is one a regrid shifted into this cell, not one
  the source wrote; the new token an `edit_cell` writes is never one of those, so the judge is
  as sharp as it was.

And two more at fresh seeds, one on each side of the line again.

* **An empty paragraph of the source's own is never a table's lead** (chain-12 seed 630138).
  A body may not open on a table, so Docs keeps an empty paragraph in front of the first one
  and `doc_ir._hide_trailer` leaves it out of the IR — by its **shape**, which a block of the
  file's own can have. A delete is all it takes to put one there: the opening table goes with
  its lead, and the empty paragraph that stood behind it is now the one in front of the next
  table. It then vanished out of the IR, its named range was taken by no block and read as an
  orphan, `orphan_requests` deleted it, and the settle wrote the file with that paragraph
  behind the table — the source's order undone, nothing of the reader's lost, the round
  converging on the new reading. A mark somebody has planted an identity on is not Docs'
  scaffolding: `_planted_starts` gives the reader the `b2s:` ranges and neither lead nor
  trailer is hidden where one begins.
* **A paragraph dragged against a word standing in a cell** (chain-4 seed 600784, the
  oracle's). `WORD` is `\S+`, and a cell is the one place a reader's drag can weld text from
  *anywhere else in the tab* onto a word of the table: "…after it." dropped against the `x` in
  a cell makes one token of the two, which is a token of neither the base's table nor the
  base's paragraph, so it read as a word the reader had typed and the source rewriting its own
  half of it read as a loss. `_welded` sees it as soon as `_cell_findings` is given the tab's
  base words, which is what `_words_findings` already had.

Each pinned by a test that fails with its mechanism put back in memory, and measured the same
way: `_moved_keys` 1 of 200 at chain 6 (no longer reproducible, and for a reason worth
reading — see the end of the next section), `_restyled_words` 1 of 200 at chain 6,
`unwritten_levels` 2 of 700 at chain 4 and its widened half 1 of 900 at chain 4, the stuck
anchor 1 of 300 at chain 10, the row note 1 of 400 at chain 10, `_orphan_range`'s cuts 1 of 700
at chain 6, `_settles_as_item` 1 of 900 at chain 4, the shape-only lead 1 of 400 at chain 12,
the cell judge's blindness to the tab 1 of 1,200 at chain 4. `_order_arrived` itself is what
sees the order defects: with it and the old `_moved_keys` both in place, 300 rounds pass in
silence. Then clean under `--strict` at thirteen fresh settings, `KNOWN` still empty.

### A block being there at all, and the drag that made a document no reader could

The third judge asks whether the source's changes arrived, and it asked about grids, cells,
words, styling and order — everything except the two plainest things a source edit can say:
**a block is new** and **a block is gone**. Neither of the other judges will ever ask. The
loss oracle says in its own first paragraph that it is about the *reader's* work, and a
paragraph of the source's that never arrives takes nothing of theirs; convergence is
satisfied by any self-consistent reading, and the settle writes what the document holds into
the file and the base alike, so the round converges on the block not being there.
`fuzz_docs._existence_arrived` asks it, of the words rather than of the key — a key is made
from a block's words where no range carries it, so twins can trade keys and every wordless
block would share one — and only where the reader left the block exactly as the base has it,
which is the case with nothing to merge.

It found six things, four of them the harness's own and two the merge's, and it could not
have found the first of them without being wrong itself to begin with:

* **The file it is handed has no keys yet** (chain-4 seed 660085, its own first finding).
  `doc_merge.plan` calls `doc_ir.key_blocks` *in place* on the file, and the campaign's
  snapshot of the file is taken before that, so a block the source has just added had no
  key — and the report names a refusal by that key. The one excuse there is could never be
  looked up, and every block the merge refuses to write read as an addition lost in silence.
  The other half was the note itself: `refuse_nowhere` said "not written", which reads as a
  thing still waiting, when in fact the settle regenerates the file from the document and
  the paragraph goes out of the file too. It says where it went and how to get it in.
* **A tab the file only asks for stood in for the body.** `oracle.parts_by_tab` keyed a part
  by `part.get("tab")`, and a `<section>` with no `data-tab` is a tab the *file* asks for
  that the document has never had, so it has no id: it landed on `None`, where the body is,
  and the last one written won. Every question asked of the body then got another tab's
  part. Measured with the collision put back: **107 of 200** rounds at chain 4 fail — and
  **0 of the same 200** with `_existence_arrived` switched off as well. It was invisible
  until this judge existed, which is the argument for the judge as much as for the fix.
* **The drag landed past where the reader let go** (chain-4 seed 710370). `read_move_block`
  is a delete and a retype in two batches, and the second's index was read off the document
  the reader saw rather than the one the first batch leaves behind: drop a block below the
  cut and everything down there has moved up by what went. It landed inside a word, inside a
  chip, or between the two code units of an astral character — which no cursor can be put
  inside, and which left the document holding a lone surrogate that `doc_ir.utf16_len`
  encodes strictly, as the API's own JSON does, so the campaign died where it stood.
  `doc_world.splits_a_pair` refuses such an index now, a judgement rather than a measurement
  (whether Google refuses it is owed). The crash is the smaller half: everywhere it did not
  crash, every judge was being handed a document no reader could have made. Both of the loss
  oracle's punctuation forgivenesses — `_dressed_up` (a full stop landing against the `1` in
  a cell) and `_undressed` (a drag carrying a stop off a word) — had been written for damage
  this one line was doing, and are **gone**: 0 of 800 rounds at chain 4, 0 of 500 at chain 8,
  0 of 400 at chain 12 and 0 of the 60 regression seeds need them, and a forgiveness for
  something that no longer happens is a blind spot waiting, which is what `_twin_unmarks`
  was. `joined_differently`'s other two — `_welded` for a reader joining two paragraphs,
  `_pared_down` for a reader deleting one of two joined words — are still earned.
* **The question, sharpened three times.** A bag of words over the tab let a `collide`
  rewording another paragraph to `ribbon` stand in for a dropped `the reader wrote ribbon`
  (chain-8 seed 720173); counting blocks that say *at least* this one's words let `What we
  found.` become `harbour we found.` and answer for a heading that said `harbour` (720270);
  counting everything a block **says** let a chip put into a twin stop it saying what the new
  copy says, so the count stood still while both blocks arrived (720074). The count is of the
  text alone now, and it is only one of two traces — because the confound is the counting
  itself, a question about one block asked of every *other* block that says the same thing.
  At chain-12 seed 730061 a `collide` reworded the twin in the very step that appended the
  copy, and no counting survives that. The other trace is the **key**: the plan gives a block
  it writes a named range of its own, so the settle keys it back. Either excuses; the finding
  needs no trace at all. Both are earned — blinding the key fails 1 of 400 rounds at chain
  12, blinding the count fails 10 of the same 400.
* **A table that never said anything is recovered onto nothing** (chain-12 seed 730384, and
  the merge's). `recover_tables` pairs a table whose range the *reader* destroyed with the
  base entry that names it, on the words the two hold — and two empty strings are each
  other's perfect match. A base table that had never said anything paired at 1.0 with the
  blank table `insertTable` had just built in the same batch, before `anchor_tables`, the
  pass that knows about that one, had run. The reader's table — beheaded by a row delete,
  holding the only word either of them had — was then the one free table left for the new
  table's key: the merge saw its own blank grid where the reader's table stood, deleted it to
  build the grid again, and the word went with no note. A pairing on words is not a pairing
  when a side has no words. 1 of 400 rounds at chain 12 with it put back.

And the same lesson one judge over, at chain-10 seed 780188 (shape `between_tables`). A key
made from a block's words is no identity where the block has none: it is that block's number
among the wordless ones, and the next one to lose its words takes it. Docs keeps a paragraph
between two tables however it is deleted, so a source that dropped one there left its mark
standing empty — the body's *first* wordless block from then on, so it took `paragraph:empty`
and the block that had carried that name became `paragraph:empty#2`. Nothing moved and
nothing was lost; two names changed hands under a genuine move elsewhere on the page, and
`_order_arrived` read the renaming as the source's order undone. Wordless keys are left out
of its question now — but **only where the four sides disagree about which keys those are**,
which is the narrowest the exclusion can be: where the population is unchanged the numbering
means the same block on every side, and a wordless block (a picture of its own, an empty line
between two sections) is an ordinary thing for a source move to carry. `_existence_arrived`
gives the same reason for asking about the words rather than the key.

And one more of the merge's, at chain-6 seed 790329 (shape `two_tables`), which is the
**reader** beheading a table seen from the other end. `anchor_tables` names the table a
structural batch wrote by what it follows and took the first unkeyed table after that block
— usually the only one, its own, whose named range its row delete had just taken with it.
But a reader can behead a table too, in the browser, and `recover_tables` refuses to pair
that one where the words leave any doubt, so two free tables stood on the page and place
alone decided. The regridded table's key went onto the reader's, the source's rows were
planned against the reader's grid, and the two rows the sync had just written stood under no
name at all — the source's regrid reached the document and then went away again, with
nothing in the report. Where the batch wrote a grid, that grid is what its table has: a free
table of another shape is not it, whatever it follows (`_built_size`). And where none has
that shape, nothing is claimed — the key comes back at the re-plan, where `recover_tables`
sees both tables at once and the words tell them apart. 1 of 600 rounds at chain 6 with the
old choice put back.

Each pinned by a test that fails with its mechanism back in memory. Then clean under
`--strict` at ten fresh settings — 800 rounds at chain 4, 700 at chain 6, 500 and 500 at
chain 8, 400 at chain 12, and, with the order judge narrowed and the anchor fixed, 800 more
at chain 4, 600 at chain 6, 400 at chain 8, 400 at chain 10 and 400 at chain 12 — `KNOWN`
still empty.

One measurement did not survive the campaign that found these, and it is worth saying which.
`_moved_keys`' own defect was recorded at **1 of 200** rounds at chain 6, at seeds around
400186; it is 0 of 200 there now, and 0 of 400 at fresh ones. The exclusion above is not the
reason (0 of the same 200 with it disabled): a *later* fix is. The old `_moved_keys` asks for
four moves where one would do, and since chain-10 seed 450252 `_apply_source_moves` keeps a
`stuck` set and **says** every block whose move it could not make — so the same break now
comes out as a report naming the table and the three blocks behind it, and this judge forgives
a pair the report names. The defect is still there and still wrong; it is no longer silent,
which is the only thing a loss campaign can see. What pins it is the unit test that says
`_moved_keys` is a longest common subsequence, which needs no seed at all.

### Three the shapes found: a stop, a key, and a table with nowhere to go

A round draws its shape at random, so a defect that needs two tables on the page waits for
the dice: of fourteen shapes, `two_tables` is one. Fixing the shape with `--shape` is the
cheap way to press on one part of the machinery, and 800 rounds of `two_tables` at chain 8
came back with three findings where 1,600 mixed rounds at the same depths had none. One is
the oracle's own and two are the merge's.

**A full stop is not part of the word it stands against** (chain-8 seed 870308, the
oracle's). `WORD` is `\S+`, so the token is what a reader would call the word plus whatever
the *source* has since parked against it — and a word wears a mark only where every
character of it does (`_under_words`, the rule written for the split runs of seed 970528).
The reader bolded `zephyr`, the source reworded the sentence around it, the merge wrote
`lantern zephyr` bold and `.` plain, and the bold read as gone while it sat there in the
document. `oracle.core` trims the punctuation clinging to either end (Unicode `P*`, so a
soft hyphen or a slash stays where it is — those join two words rather than dressing one,
and `joined_differently` is the forgiveness written for them), and the question "is the
word still there?" is asked in the same words. 1 of 400 rounds at chain 8 with the untrimmed
token back.

**A key this batch is about to place is not the recovery's to give** (chain-8 seed 870368).
After a structural write two repairs look at the same free tables: `recover_tables`, for a
named range the *reader* destroyed by deleting the row a table is anchored in, and
`anchor_tables`, for one of our own writes. The recovery goes first — that was seed 993608's
lesson, so that the reader's table is not free to be taken — and where the same table was
regridded by us and beheaded by them, the two can cross. A regrid that deletes a table's
first column leaves it saying almost nothing, while the reader's beheaded table still says
most of what the base recorded, so the regridded table's key scored higher on the reader's
table than on its own remnant and took it. `anchor_tables` then found that key already
placed, left the remnant unnamed, and the source's regrid went nowhere. The batch knows
where it put its tables and this pass only guesses, so it is told which keys are
`spoken_for` and goes second on those. 1 of 400 rounds at chain 8 without it.

**A table moved in front of the empty paragraph the body ends behind** (chain-8 seed 890070,
shape `ends_on_table`). A body may not end on a table, so Docs keeps an empty paragraph
after one and no request deletes it: a trailing table goes out by its own span *and the mark
in front of it*, which leaves the block before it as the body's last and no stray empty one
after (`_delete_range`). That block keeps its words, and so its named range — unless it is
itself an empty paragraph, which is all mark. Then the file putting the table in *front* of
that block asks for a place the table's own delete takes away: `insertTable` splits what is
left of the swallowed paragraph, the table lands behind it again, the settle keys the
leftover from its words (it is empty, so its name is its kind and it fits), and the move is
undone in silence. `refuse_eaten_anchor` leaves the table where the document has it and says
why — the mirror of `refuse_back_to_back`, and refused for the same reason: the document
cannot hold what the file asks for, and saying so beats a sync that writes the same two
requests every time it runs. 1 of 400 rounds at chain 8 without it.

Each pinned by a test that fails with its mechanism back in memory, and the three seeds kept
as regressions of their own — `tests/test_doc_fuzz.py`'s `SHAPED`, since a seed names a
script and these scripts only exist on one shape. Then clean under `--strict` at five more
settings: 800 rounds of `two_tables` at chain 8, 800 of `ends_on_table` at chain 8, 700 of
`between_tables` at chain 6, 800 mixed at chain 4 and 400 mixed at chain 10. `KNOWN` still
empty.

### Four from `themed`: what a paragraph inherits, and what a word wears

The same method on the shape whose whole point is what a paragraph *inherits*. `themed` is
the one corpus shape with named styles behind it (`fuzz_docs.THEME`: HEADING_1 centred and
bold), so it is where a field going from inherited to a paragraph's own can be seen at all —
and 700 rounds of it at chain 6 found one in the merge and one in the campaign's own judge,
200 more at chain 10 two in the loss oracle. One of the four is a loss; the other three are
judges answering a question about the wrong thing, which on this shape is the easy mistake:
under a theme, a word that says nothing is not a word nobody styled.

**A heading justified by the paragraph the source deleted above it** (chain-6 seed 1130023).
Docs merges a deleted paragraph into the one behind it and hands over its style, which is
why `carry_unimported` puts the named style and the bullet back. The *measurements* it may
only put back on a paragraph this run wrote (`_unwritten`), and that narrowing is what makes
the settle safe: writing styling back is the one thing there that can take styling away, and
in a read "absent" is also a reader who took it off. But the neighbour of a block this batch
deleted is precisely a block nobody wrote that the write itself changed. The source dropped
a justified paragraph, the heading behind it came out justified *of its own*, and a heading
that sets its own alignment has stopped following the theme's centring for ever — the file is
regenerated from the document afterwards, so the next sync writes nothing and agrees. Only
the loss oracle's `theme_undone` could see it, which is why it exists. `requests` now marks
that neighbour `paragraph_merged` — the block behind, and the one in front too where the
delete borrowed *its* mark (`_delete_range`, a block in front of a table) — and the gate
widens by exactly those. 1 of 400 rounds at chain 6 without it.

**A mark the theme already puts on says nothing, until the named style changes** (chain-8
seed 1140022, chain-6 seed 1150196, the campaign's own). `_worn` subtracts a run's marks by
the block's named style, because the file spells them out and a read leaves out what the
style already says — the normalisation that lets the two sides be compared at all. Under a
theme that bolds HEADING_1, then, a reader bolding one word of a themed heading has said
nothing that side can see, and `_styling_arrived`'s guard read the block as one they had
left alone. The source then made it body text, where the same run styling spells out
differently on either side, and the reader's bold — which the merge kept, as it should —
read as the source's restyle vanishing. Two named styles are two languages, so the run half
is not a comparison at all and is not asked; the named style itself is part of `_shape`, so
nothing goes unjudged. 1 of 400 rounds at chain 8 and 1 of 600 at chain 6 with the old judge.

Then the same shape at chain 10, where both findings were the **oracle's** — `styling_restored`,
the question of whether a mark the reader took off is back on, which is the newest of its
questions and the one with the most ways to be wrong. Neither was a loss; both were the check
answering for a word that was not the one it was asked about.

**A mark taken off a block the source dropped** (chain-10 seed 1180145). The bargain the
`block_gone` branch states in so many words is that a block the reader left exactly as the
base has it goes when the source drops it — and the mark on one of its words goes with it.
What made this one visible is that the *key* does not go: the reader's own pasted copy was
standing beside it, saying the same words in the theme's own bold, and took the name, so the
question was asked of the block and answered by the copy. The excuse is the file saying it
(`dropped`), and only for the taken-off half: a mark the reader *put on* keeps the whole block
alive (`doc_merge._styled`), so the question cannot arise there. It also has to be a file that
exists — asked of a check given none, "the file no longer names this key" is true of every key
there is.

**An un-marked word the source reworded away** (chain-10 seed 1180151). The question is asked
by occurrence and a block may say a word twice: the heading said `thicket` twice, the reader
un-bolded the first, the source reworded that one to `vellum` — which came out un-bold, exactly
as asked — and the plain `thicket` left over answered for it. The count is capped by what the
*file* asks for, which is what says whose doing it was: an occurrence the source has just
**added** is the mirror case (themed seed 40254, `"and willow"` plus `" and harbour"`) and must
still be no excuse, and nothing in the document alone tells the two apart. 1 of 200 rounds at
chain 10 for each.

All four pinned by tests that fail with the mechanism back, all four seeds in `SHAPED`. Clean
under `--strict` afterwards: 700 `themed` at chain 6, 500 and 300 at chain 8, 400, 200 and 250
at chain 10, 700 mixed at chain 6, 500 `chips`, 500 `imported_list`, 300 `equations` and
300 `tabs` at chain 8.

### And one from `between_tables`: the empty block a new table swallows

Sweeping the shapes nobody had pressed — 300 rounds each at chain 8 of `toc`, `opens_on_table`,
`titled`, `astral`, `dropdown` and `prose`, all clean — `between_tables` came back with one
(chain-8 seed 1270233). `insertTable` splits the paragraph its index is in, so a table written
in front of a block leaves an empty paragraph, and `_new_table_requests` gets rid of that by
deleting the mark of the block *before* — which merges the two the way the Delete key does.
When the block before is itself empty it is all mark, so the delete covers its named range
whole and Docs drops it; `recover_swallowed` gives the key back before the re-plan, which
would otherwise read the block as one the reader had deleted.

It asked for a plain *paragraph*, and Docs' merge keeps the first one's style: an empty
subtitle hands the survivor its own named style, so the block comes back a subtitle and the
recovery passed it by. The one thing the source was asking of that block — to stop being a
subtitle — then went nowhere, and the settle keyed it from its words to the very name it had,
so file, base and document all agreed on it and the next sync wrote nothing. Only
`_styling_arrived`, the campaign's own judge, could see it: nothing was deleted and no word
moved. Anything but a structural element counts as the survivor now; what makes it one is
that it is empty, unnamed and standing right in front of the table. 1 of 300 rounds at chain
8, pinned by a test that fails with the kind check back, seed in `SHAPED`; 300 `between_tables`
at chain 8 and 250 at chain 10 clean afterwards.

### And one from `astral`: the orphan a new table's empty paragraph picks up

The same sweep at chain 10 — 250 rounds each of `themed`, `between_tables`, `opens_on_table`,
`two_tables`, `ends_on_table`, `tabs`, `imported_list`, `chips`, `toc`, `titled` and
`equations`, all clean — left `astral` with one (chain-10 seed 1430231), and it is the other
half of the same sentence: a named range can be *given* an empty paragraph as easily as it can
lose one.

A reader backspacing at the start of an empty paragraph leaves that paragraph's range inside
the one that survives, naming nothing — the orphan `doc_ir.orphan_requests` has deleted since
chain-4 seed 70140, because a range with nothing of its own is a key waiting to be stolen. An
empty paragraph is all mark, so the orphan sits exactly on the survivor's paragraph mark; and
`insertTable` in front of another table goes at that very mark, splits the paragraph, and the
range goes with the half after the table. The empty paragraph the insert leaves behind is
therefore read back under the dead name — and the re-plan reads the document again between the
structural batch and the words, so it sees the block the reader deleted standing there for the
source to write into. The deletion undone, the source's words in it, and the report saying
nothing: `block_resurrected`, which exists for exactly this.

`structure` heads its batch with `orphan_requests` now, for the same reason `requests` heads
the batch of words with them one step later — the deletes move no index, so they can head any
batch, and the one place they were not run was the one batch that makes a new empty paragraph.
1 of 250 rounds at chain 10 (0 with it), pinned by a hand-built test that fails when the head
is taken out, seed in `SHAPED`.

### The level an item takes is the list it is written into

Chain 12 next, at fresh seeds: 400 rounds each of `between_tables` and `themed`, 600 mixed,
all clean; `tabs` came back with one (seed 1640036), and the shrinker took it down to two
appends and a restyle-and-move with **no reader op at all** — the source's own edits, on a
document nobody had touched.

An item the source moves is written from nothing, and no request gives a bullet its nesting
level: it comes out at the level of the list it lands in, which `unwritten_levels` has said
before the write since chain-4 seed 430296. What it said was *the level of the item in front
of it* — and that is the rule for one shape of write out of two. A block goes in as "text\n"
at the **start of the block that follows** it (`_insert_index`): Docs splits that paragraph
and the new block is the half in front, keeping the style that was already there, bullet and
level among it. It is only where nothing follows, or a table does, that the text goes in as
"\ntext" at the mark behind and wears the style of the block in *front*.

So a level-0 item moved to just in front of a nested one comes out nested, and the note that
exists for exactly this said nothing, because it was looking the other way. The prediction
now asks the anchor (`_anchor`, the same block `requests` writes in front of) where it
splits, and the block in front only where it appends. 1 of 400 rounds at chain 12, 0 with it.

It also found the note lying the other way, which nothing had asked: the test standing for
chain-8 seed 530265 moved an item *behind* a nested one and asserted the note — but that
write anchors on the plain paragraph after it, so the item really comes out at level 0,
exactly as the source asks, and the note was a prediction of a loss that never happened. A
spurious note is not harmless here: it is `fuzz_docs._shape_arrived`'s excuse, so it hides
the next real one. Both tests now assert the level the **document** ends up at beside the
note they pin.

### The empty paragraph a table's delete eats

Chains 14 and 16, at fresh seeds again, and the two findings that came back were one
defect seen from either side: 500 mixed rounds at chain 14 (seed 1740158) and 300 `tabs`
rounds at chain 16 (seed 1730265), each shrunk to three steps and a source that moves the
table the body ends on.

A body may not end on a table, so Docs keeps an empty paragraph after one and no request
deletes it. A trailing table therefore goes out by its own span **and the mark in front of
it**, or that trailer would be left standing as a second empty paragraph (`_delete_range`,
measured). The block that mark belongs to keeps its words, and so its named range — unless
it is *itself* an empty paragraph, which is all mark: the delete covers its range whole and
Docs drops it, exactly as a new table's swallow does (`_swallowed`). Half of this was known
and refused: where the file puts the table in **front** of that very block, the place the
table is written goes with it and the move is unrealisable (`refuse_eaten_anchor`, chain-8
seed 890070). Where the table moves somewhere else the move stands — and the block was
being destroyed anyway, quietly.

What that costs is identity, and the re-plan after the structural batch is where it is
spent: the file's key names nothing there, so the block reads as one the reader deleted and
everything the source asked of it goes in silence. An empty paragraph the source had made a
`HEADING_1` came back a plain paragraph (1730265), and an empty list item came back between
the two tables under a fresh name, so the order the file asks for was never reached
(1740158). Two different judges, one mechanism.

`recover_eaten` gives the name back, and the place it puts it is what makes this simple:
the survivor is **the trailer**. With the table in front of it gone, that empty paragraph
*is* the block — at the end of a body there is nowhere else for one to be — and
`doc_ir._hide_trailer` leaves a paragraph somebody has planted an identity on out of the
scaffolding, so the name is what makes it a block again. Where the batch left the body
ending on something other than a table the trailer is no longer hidden and stands there as
a plain empty block; that one is the same paragraph and takes the key in place. It runs
after `anchor_tables`, as `recover_swallowed` does, so a table anchored on that block is
found by the name the batch really left behind. Both campaigns clean afterwards.

### A cell's report is the table's

Found from the other end of the same campaign (offline chain-12 seed 1710213, shape
`prose`), and it is not a loss but a **report** the loss oracle could not read. The reader
deletes the row a table is anchored in and writes in another; the source writes in that
same cell; the document wins, as it does everywhere, and the merge raises a conflict so the
person can see the words it did not write. All three sides then have a 2×2 grid, so
`_merge_table`'s fast path matches cells by place, `_merge_cell` merges the cell paragraph
by paragraph — and that branch goes through `_merge_block`, whose fallback name for a
block with no key of its own was the literal `a table cell`.

Which is true, and an address for nothing: a document with three tables says it three times
and points at none of them. `_merge_cell`'s *other* branch — the one for a cell whose three
sides disagree on how many paragraphs it holds — has stamped the table's key on its
conflicts since it was written; the by-paragraph branch is the commoner one by far, one
paragraph per cell being what a table looks like, so the crisper the case the vaguer the
report. `_merge_block` now takes the table's key as its fallback (`inside`), which also
names the notes it can raise for a cell (a chip the source would rewrite, words the source
restyled that the document rewrote). The oracle's `cell_lost` excuse is the report naming
the table, so it could never be found either: 400 prose rounds at chain 12 from seed
1710000 go 1 → 0.

### How the reader set a paragraph

The judges have grown one question at a time, and each new one has found something the
moment it was first asked. This is the largest gap left in the loss oracle, and it was
stated plainly: it asked about the reader's **words**, their **chips and pictures**, their
**rows**, their **blocks and tabs**, and — since the theme work — the **marks on a word**.
It never asked how the reader set a **paragraph**. Centring a quotation, indenting it,
spacing it out, shading it, giving it a rule, making a line a heading: every one of those
is a choice made in the browser without touching a word, and every one of them is as
deliberate as bolding one.

`doc_loss_oracle._shape_findings` asks it (`shape_undone`, severity `loss`): a field of
`doc_merge.SHAPE_KEYS` the reader changed from the base that the document does not have
after the sync. Two things it has to forgive. A list's **ordered-ness** is left out — it is
the one thing an imported document cannot report, so the two sides disagree about it by
construction. And Docs' own **merge-on-delete** rule is not the sync's doing: a paragraph
the source deletes is merged into the one behind it, which takes the *deleted* one's
style, so a block whose predecessor in the base the source dropped is not asked about.
That is the same forgiveness `_inherited_findings` needs its `theme` for.

It failed 7 of 200 rounds at chain 6 the first time it ran, in one signature, and the
shrinker took it down to two operations: **the reader spaces a paragraph out, the source
adds a chip to it** (seed 2000188). A block whose chip or picture the source changed is
written again from the file, because no request edits one — and it was written again from
the file's *shape* too, on the grounds, stated in the comment there, that a document which
left the block's words, run styles and frozen runs exactly as the base has them "has
nothing of its own in it". It may have. All of it went, silently: the words are all there
so the loss oracle passed it, the source's own change arrived so the campaign's judge
passed it, and the settle regenerates the file from the document, so afterwards the file
says what was written and the next sync writes nothing at all.

`doc_merge._merged_shape` is the one rule both ways into a block now share — the source's
shape where the document left it alone, the document's where both sides changed it, with a
note saying so. Clean afterwards at four settings: 200 at chain 6 (7 → 0), 400 at chain 4,
300 at chain 8, and 300 `themed` rounds at chain 6.

The `themed` campaign's own finding at that seed was the oracle's, and it is the twin
hazard once more (2030066, chain 6): **a key is not a block**. The reader pastes a copy of
a heading, which lands in the style of what it was dropped into — Docs' own rule, and here
a heading centred of its own — the source drops the block the key was on, and the settle
keys the copy by its words to the name that went. `_inherited_findings` then compares two
*different* blocks and finds a paragraph that has stopped following the theme, on nobody's
doing: nothing was written to the copy, and the block that really wore the theme is one the
source dropped and the reader left alone. `_twin_already` is the forgiveness, asked of the
words the key names *afterwards*, so a twin saying something else answers for nothing.

### A boundary has to be a word, and a centring has to be an edit

The first campaigns run *with* the new paragraph-shape judge came back with one finding each
(300 at chain 10 from 2040000, 500 at chain 6 from 2050000), and both were the sync's.

**The mark whose boundary moved** (2040246, shape `equations`). `marks_of` is the question
"did somebody change what these words are marked with", and it drops the words to stay deaf
to a chip splitting a run in two — which drops the *boundaries* with them. So the source
taking its bold one word further left, `((), 'the value  holds '), (bold, 'everywhere')`
becoming `((), 'the value '), (bold, ' holds everywhere')`, is the same alternation of mark
sets it always was: the merge read the file as asking for nothing, wrote nothing and said
nothing. The boundary lives in the words, so `doc_merge._remarked` asks the words — and only
the ones both sides have, since a word one side typed or deleted is the text merge's business
and reading a changed word as a changed mark is exactly the noise `marks_of` exists to keep
out.

The two sides are then not asked the same question, and that asymmetry is the whole of the
fix. "Is the source asking for different marks?" decides whether its restyle is written at
all, so it must hear a moved boundary. "Did the reader restyle?" guards a branch that gives
up on the **whole block**, and asked as finely it loses work: a reader who moves a bold from
one word to another would take the source's italic on a third word down with it, where
`_restyled_words` merges the two perfectly well — which is chain-6 seed 400044's test, three
sections up, failing again the moment the finer question is asked on that side.

**The centring the delete took** (2050019, shape `imported_list`). `_edited` is the test that
outranks a source delete, the last thing standing between a reader's work and a
`deleteContentRange`, and fresh-seed 90175 taught it to ask the marks. It still did not ask
`_shape`: the block's kind, its heading or list level, its bullets' ordered-ness, its
alignment, its indents, its spacing, its shading, its rules. Every one of those is a choice
as deliberate as bolding a word, and the whole of the choice lives in properties no word of
the block carries — so a block the reader had only centred read as untouched, and the source's
drop took it. `ordered` counts here although `doc_loss_oracle.SHAPE_FIELDS` leaves it out:
what an import cannot *report* is neither side's fault, but both readings `_edited` compares
are the document's own, one sync apart, so a list the reader renumbered says so.

Each was put back on purpose to see the campaign fail again, and each is pinned by a test of
its own. Both campaigns clean afterwards, 1 → 0.

### The cell nobody had ever styled

Coverage says what is drawn, not where. `updateTextStyle` is in every campaign's request
count by the thousand and `updateParagraphStyle` beside it, and every one of them was on a
paragraph of the body: the reader's styling ops pick their spot from `part["blocks"]` and the
source's from the same list, and a cell is never in either — a cell lives inside a table
block's `rows`. `src_edit_cell` reaches one, and it only rewrites words. So `_merge_cell`'s
styling was as undrawn as the column requests were, and for the same reason: nothing in the
campaign's vocabulary could say it.

`read_cell_style` and `src_restyle_cell` say it now — a face, a size, small caps, a mark, or
one of the paragraph measures, set by either side on a paragraph inside a cell. Two details
are the harness being careful rather than clever. The reader's op reports the **table's** key
as what it touched, so `collide` answers in the same table rather than somewhere else. And
`pageBreakBefore` is left out of what may be set in a cell (`CELL_MEASURES`), because Docs
refuses it there: a request the real API would reject is the harness's own doing and would
make the campaign fail at something the sync never asked for.

It found a defect in the first sixty rounds (chain 4, `two_tables`, seed 3000027 — the first
round the campaign ever styled a cell with). `_table_movable` is the question "can this table
be deleted and built again with nothing lost", asked before the source's move is written, and
it compared the cells' **words**. A rebuild carries nothing but words, so a reader who had
small-capped one word of a cell, or centred it, had that taken off by a move the source asked
for, with nothing in the report. It is `_edited`'s rule from the section above at the size of
a table: styling a cell is a choice the reader made in the document, exactly as much as typing
in one.

The fix needed a shape of `_shapes`, because `_shape` of a table is a row of `None`s — a table
says what it is through its cells, so a question asked of a block has to descend into one to
mean anything at all, which `_styled` already did and `_shape` did not. `_table_movable` asks
all three now, and `_edited` asks `_shapes` rather than `_shape`, so a table whose cell the
reader only centred is not one the source may delete either. Broken on purpose, the new test
fails and the oracle itself names the loss (`styling_lost table:a`).

The four broader campaigns run next — 1,550 rounds at chains 4, 6, 8 and 10 — came back with
one finding each, and all four were the **harness's**, one defect wearing four hats. A
paragraph's first index in `doc_world` was its mark less everything since the mark before it,
and a table is a unit in the body's list with no mark of its own, so the paragraph standing
after a table began, as far as the world was concerned, at the table's own start. Every range
aimed at a **cell** therefore reached it: one source restyle of one cell took the alignment a
reader had given that paragraph, and `createParagraphBullets` inside a cell would have
bulleted it. A table ends the paragraph in front of it — `documents.get` says so, and the
named range this world plants on such a block starts after the table — which is
`doc_world._own`. Nothing had ever styled a cell, so the world could be wrong there for as
long as it liked; with the fix all four campaigns are clean, and the world's reading is pinned
by a test of its own.

One thing to remember when reading the seed windows in `tests/test_doc_fuzz.py`: an op added
to the campaign changes what **every** seed draws.
`test_the_campaign_sees_a_mark_the_file_cannot_say_is_off` samples seeds in a fixed window and
asserts the injected defect is caught at least three times there; the two cell ops moved its
window's hits from six to two, so it was re-measured over 100..400 (hits at 184, 215, 225,
233, 340, 386) and now samples 180..300. That is the second time that test has had to move,
and it is not flakiness: the window is a sample of a population the ops define. (`cell_chip`
below moved both windows again, to 180..310 and 0..24.)

### A chip in a cell, and the mark a delete borrows

Two more from the same corner, one of each kind — a defect in the sync and a blind spot in a
judge — plus the op that found the second.

The sync's: a block standing in front of a table gives up the **previous** block's paragraph
mark, because its own is the undeletable newline in front of the table (`_delete_range`). Where
that previous block is an **empty paragraph** its named range *is* that mark, and a range is
destroyed with its text or not at all, so the delete takes the name of a block the source never
asked to touch. Unnamed, an empty paragraph standing in front of what is now the body's opening
table is scaffolding by shape (`doc_ir._hide_trailer`'s `lead`) and leaves the IR entirely: its
key settled on some other empty paragraph, the source's order read as one that had never
arrived, and nothing was reported (offline chain-10 seed 4200130, shape `two_tables`). It is
the mirror image of `_orphan_range` — there a range outlives its block, here a block outlives
its range — and the repair is the same one `doc_ir.replant_requests` makes, with one
difference: **no `deleteNamedRange` in front of it**. Docs has taken the id already, and a
request naming an id that is gone throws out the whole batch.

The op: `cell_chip`, on both sides. `src_add_chip` and `src_add_picture` pick their block from
`part["blocks"]`, so a frozen run had never once been asked for *inside a table* — and a cell
is a block with no key of its own, inside a structural element the planner treats as one thing,
which is exactly where the `rewrite` path (the only way a block whose frozen runs the source
changed is written at all) might have stopped. It does not: the chip arrives, and a hand-built
test says so.

What did not arrive was the judge. `_cells_arrived` asked `oracle.cells_of`, which is `text_of`
per cell, so a chip was counted by its **face** — and a chip's face is the document's to draw.
The file asks for a person chip reading `Grace`, Docs renders the object character off the
address, and every seed drawing `cell_chip` failed at once: 16 regression seeds, all of them the
judge's own. `_says` had learned this at the size of a block months ago; the cell was the last
place in the file still asking in the old language, and `_cell_says` is the same subtraction one
size down. The two judges were written a week apart, which is the whole explanation.

Then the four campaigns, and one finding in 2,100 rounds — the same rule as `_table_movable`'s
one size further down. A line the source takes away goes **unless the document wrote in it**,
and `_line_unchanged` asked the cells' text, so a reader who had small-capped a word of a cell,
or set its size, had `deleteTableColumn` carry that off with the column and nothing in the
report said so (chain-8 seed 5300013, shape `themed`, shrunk to three steps: the source adds a
table, the reader styles one of its cells, the source drops that cell's column). `_same_set`
asks `_styled` and `_shape` of the cells the two sides share, the line is kept as one written in
would be, and the note says "wrote in it or styled it". That is now the third size at which this
one sentence has had to be said — block, table, line — and each time it was the same sentence:
**styling something is a choice the reader made in the document, exactly as much as typing in
it.**

### A cell with two paragraphs in it

The op is `split_cell` on both sides — the reader pressing Enter inside a cell, the source
giving a cell a second paragraph — and until it existed every cell the campaign ever drew held
exactly one. So `_merge_cell`'s unequal-counts branch, the `diff3` over a cell's joined text,
had never been asked to carry anything that text cannot say. Five findings came out of it, one
of each kind again.

**The sync's, and silent.** A cell whose paragraph count the source changed went through that
`diff3`, which is text, so a chip in it was flattened to the object character — and
`text_requests` skips any hunk holding one, no request rewriting a chip. `_block_edits` came
back empty, neither the split nor the chip was written, and nothing was reported (offline
chain-4 seed 6000042, shape `themed`, the source splitting the cell it had just put a chip in).
Where the merged text runs through a frozen run and the document left the cell exactly as the
base has it, there is nothing of the reader's in the way, so `_cell_kept` sends it down the
`rewrite` path that the same question at the size of a block has always used. Only there, and
not wherever the paragraph counts differ: a minimal edit keeps the reader's styling on the words
it does not touch, and keeps the table's own named range, which a rewrite of its first cell
destroys. A cell **both** sides wrote in is a note now rather than a silence.

Narrowing it that far took the guard out from under the *other* reason a cell's words cannot
carry it, and the campaign said so within the round: an object character stands for whichever
chip is there, so a cell whose picture the file replaces with a person chip has a merged text
equal to the one already written. `text_requests` wrote the split and the chip arrived nowhere
(chain-10 seed 6100210, shape `two_tables`). `_cell_frozen` asks what the frozen runs *are*,
which is `_merge_block`'s own guard at the size of a cell — the source changed them and the
document did not, so the words are not the question.

**Identity.** A table is anchored in its first cell (`doc_ir.anchor_span`), so rewriting that
cell takes the table's named range with it, and `adopt_keys` is the one thing that gives the key
back — by the words the plan and the read-back say. They did not say the same words. A cell the
merge rewrites is one block with the paragraph marks *inside* it (`_cell_runs`) and the document
hands it back as the paragraphs it is, so the plan said `x\nribbon` where the read-back said
`x | ribbon`, and the table went unrecognised. Settled under a name made from its new first
words, it was a table the file and the base both name and the document does not have (offline
chain-4 seed 6000079, shape `astral`, the source adding a chip to the anchor cell and splitting
another). `_match_text` counts a cell's paragraphs one by one, whichever side is holding them.

**The judge's.** Then two of the four campaigns, one signature between them: a chip the document
held, gone. `_source_dropped` is about a *block*, and a cell is not one — the table keeps its
key while the source deletes the column the chip stands in, so the chip that went with the
column had no excuse. The more so where the same source step added a chip of that same address
somewhere else, which `mine_frozen` credits against it, so one round read as a loss twice over
(chain-6 seed 6200507, chain-4 seed 6300228, both shrunk to the source putting a chip in a cell
and dropping that column two steps later). `oracle._dropped_cells` asks what a cell *says*, not
where it stands, a regrid moving every cell after it: a cell the base has word for word, frozen
runs included, that the document still has and the file has not, is a cell the source dropped.
That is also what keeps it narrow — a chip the **reader** put in makes the document's cell
unlike the base's, so the column it stands in is not the source's to take away, which is
`_same_set`'s rule one paragraph down; and the loss is then named by the cell judge rather than
by the chip count, a chip's face being words the cell did not say before.

**And a column is not every row at once.** A fresh chain-8 run then said the same thing about
the row judge, which is the third of them and the newest. A row is known by what it says and
what it says is every cell of it, so a reader who deletes a **column** changes the words of
every row in one stroke, and each of them read as a row they had deleted; the source then added
a column of its own, one of whose cells happened to hold the word the deleted column had held in
that row, and two rows came back from a grave neither of them was ever in (fresh chain-8 seed
6500291, shape `ends_on_table`, shrunk to a reader's column delete against a source's regrid —
the merge's own answer, the reader's column gone and the source's column added, being exactly
right). `oracle._rows_still_shown`: a base row the document still shows *some* of is a row it
still has, its cells covering a document row's — consumed one for one, or a reader who deleted a
row as well as a column would have the survivors answer for that too.

All three campaign judges have now been caught reading a table in the language of one of the
others: `_cells_arrived` asking a chip's face, `_line_unchanged` asking a cell's words without
its styling, and this one asking a row's words across columns that are no longer there. A table
is the one place in this model where four things have identities at once — the block, the row,
the column and the cell — and every question has to name which of them it is about.

Both campaign seed windows were measured again, every op added changing what every seed draws:
`theme_undone` 15 of 60 → 11, the test's own window keeping four of them; `styling_restored` 5
of 240 → 4, of which the old window kept a single seed, so the window moved to the three at the
end of the range. Clean afterwards at 300 rounds chain 10 `two_tables`, 600 chain 6, 800 chain 4
and two runs of 400 chain 8, 2,500 rounds in all, `KNOWN` still empty.

### The bullet button, which nobody had ever pressed

After typing, clicking the bullet button is about the commonest thing anybody does to a
paragraph in a browser, and the campaign had never drawn it: `read_heading` moves a block
between the *named* styles and leaves the items alone, `read_renumber_list` only changes the
glyph of a list that is one already, and on the source's side `src_retitle` walks the same four
named styles — `item` is not one of them. So a bullet was something only a corpus shape ever
had. `read_bullet` (a paragraph becomes an item, or stops being one) and `src_bullet` (the file
writing a `<p>` as an `<li>`, ordered or not, and back) fill that in. Four findings at chain 8,
none at chain 4 or 6 or over 300 rounds of `imported_list`, and they are two things.

**A level lives on a paragraph mark, and a mark that survives keeps it.** `unwritten_levels`
already said this twice — for a block written from nothing, and for one whose mark a delete in
front of it hands over — and both of those are about a mark that *went*. The plainest case is the
one they are exceptions to: the block keeps its own mark, so it keeps the level on it, whatever
the source asks. `createParagraphBullets` re-glyphs the list and says nothing about a nesting
level; a block the source turns into a nested item starts a list of its own at level 0. Nothing
else could see it — the reader left the block alone, so the loss oracle has no question, and the
base agrees with the document afterwards, so the round converges — and now the report says it
before the write, as it does for the other two (offline chain-8 seeds 7400013, 7400167 and
7400363, each shrinking to one source op and no reader at all).

**A glyph belongs to the list, not to the item** (`doc_merge.unwritten_glyphs`, chain-8 seed
7400334, shrunk the same way). `createParagraphBullets` lays a preset over the list the range
falls in — which is how an imported list comes to have glyphs that can be read back at all — so
two items of one list cannot be a bullet and a number at the same time. A source that numbers one
of three either re-glyphs all three or has its own request undone by the settle putting the other
two back the way the file asks, which takes the list with it and leaves even the numbered one
bulleted; which of the two happens is which request goes last, and neither is what the source
asked for. The file can say the thing the document cannot be told, `<ol>` beside `<ul>` being two
lists at a push and one list ever after. Which list an item is in is the document's word and
never the file's, so `doc_ir` now reads the `listId` back as `list` on an item — not part of a
block's shape, nothing the file can say, and the only way to know that two items share the thing
a glyph belongs to.

**And the glyph a delete in front hands over** (chain-4 seed 7700184, shape `themed`, shrunk to
one reader op and one source op). Docs merges two paragraphs keeping the first one's style, and
the style is the whole of it — the bullet's *list* among it, and with the list the glyph. The
source moves an item to the end of the tab and numbers the item behind it; the numbering is
written, the delete that ends the move runs last in the same batch, and the numbered item comes
back in the deleted one's list, bulleted. `carry_unimported` — the settle's repair for everything
an import or a write cannot carry — compared the two sides' *kinds*, so it saw a bullet one side
had and the other did not and was blind to the one case where both are items and they disagree
about the glyph; it asks about the glyph now, and `restore_bullets` writes it. The scenario needs
a **described** list to reproduce: over a list the importer built, whose glyphs nothing can report,
the file's word is taken as the document's (`restore_unreadable`'s `guessed`) and `bullet_requests`
lays the right preset down anyway, so an imported list hides the defect twice over — which is why
the test builds the donor's list with glyphs of its own, and why it asks `doc_ir.from_document`
rather than the sync's own read, which fills a list's ordered-ness in from the file and so answers
with exactly the thing the document is failing to say.

A guard written with that fix went out again: `bullet_requests` grouping its runs by the glyph the
settle is *about* to write, as it already groups them by whether the block will still be an item.
It catches nothing — a block whose bullet is being repaired is one the read-back describes, and a
run is written only where some block in it is `guessed`, which a described block is not — and 500
rounds at chains 4 and 8 never brought the two together. A guard against nothing is how one stops
noticing.

Both seed windows were measured again, every op added changing what every seed draws:
`theme_undone` 15 of 80 seeds, the test's window moving to the first 40 (which hold five) from
24 (which the new op left with two); `styling_restored` 5 of 300, the window moving to seeds 300
to 380 (four) from 360 to 420 (one). Clean afterwards at 400 rounds chain 8 on the seeds that
found them, and at three fresh runs — 600 chain 6, 500 chain 4, 300 chain 10 — `KNOWN` still
empty.

### Deeper into the same button

Three more at chain 10 and chain 8 once the campaign had the op, and they finish the thought the
three above begin: everything a request cannot say about a list is said about a *mark*, and the
question is always whose mark a block comes out on.

**The list a block lands in is not the list it came from** (`doc_merge._landing_lists`, chain-10
seeds 8100237, 8100057 and 8100374, shape `prose`). `unwritten_glyphs` asked the document what
list each block was in — which for a block this very batch is about to write is no list at all,
so it answered for nobody and the run it belonged to was never seen as one run. The source
numbers a paragraph and moves an item to just in front of it; the item goes in as `gamma\n` at
that paragraph's start, Docs splits the paragraph and hands the new half its style, bullet and
list among it, and the two blocks are one list asking for two glyphs. The note was missing and
the source's numbering went nowhere, in silence. `_landing_lists` works the landing list out for
every merged item at once — a written block lands in the list of whatever it splits, one behind a
delete lands in the deleted block's list — which is also where the two rules meet.

**And where they meet, the delete wins** (`doc_merge._mark_donors`, chain-10 seed 8100356, shape
`prose`, shrunk to three source ops and no reader in it). A block written from nothing wears the
style of the block it splits; a block behind a delete is handed the deleted block's style. Where
the source moves a block to exactly where another one goes, the written block is both, and the
delete happens last. `unwritten_levels` gave the deleted mark to whatever stood behind it *before*
the batch — by then one block further on — so it predicted the reader's level for the wrong block
and never named the right one. `_mark_donors` answers "whose mark does this block come out on?"
once, for `unwritten_levels` and `_landing_lists` alike: the same handover, asked once about the
nesting level and once about the glyph.

**The named style a bullet was hiding** (chain-8 seed 8000322, shape `imported_list`) — the only
one of the six that is a *loss* rather than a missing note. `doc_ir` reads a paragraph with a
bullet on it as an item whatever its `namedStyleType` says: the dialect has no bulleted heading
and the file writes `<li>`. So `carry_unimported`'s comparison of the two sides' named styles is
blind to whatever the document carries underneath one — ordinarily nothing, but the reader clicks
the bullet button on a heading, Docs' merge-on-delete puts that heading's style under an item that
never had one, and the moment the settle takes the bullet off, there it is. The item came out a
HEADING_1 the source had asked to be a plain paragraph, with the report saying nothing and the
second sync writing nothing, both sides reading the document as an item. The style goes in whole
now wherever a bullet is taken off, there being no difference to take. Its price is the reader who
bullets a heading *and means the heading*: nothing distinguishes that from one Docs handed over,
so the block becomes what the plan says it is.

Clean afterwards under `--strict` at 400 rounds chain 10 on the seeds that found the first two,
and at fresh seeds: 400 chain 8 `imported_list`, 300 chain 10 `themed`.

One more came out of those fresh runs, and it is the oracle's: **the halves of a word a reader
split in two** (`_cleaved`, chain-6 seed 8400013, shape `two_tables`). `_welded` knows that a
reader joining two paragraphs makes one `\S+` token out of two — and pressing Enter does the
other thing as well. The base cell said "signal 4"; the reader pressed Enter inside the word,
leaving "si" and "gnal 4"; `collide` rewrote that cell to "signal-0c791"; the merge said "si" and
"gnal-0c791", with the reader's break and the source's wording both in it. Neither half is a
token the base has, so `theirs - was` read both as words they typed and the source's rewriting of
one looked like a loss. They hold nothing of theirs: the letters are the base's. Exact, on
`_welded`'s own two conditions — the base token gone from the tab, that being what made this half
disappear, and the other half standing in the reader's *own* text, since nothing else can cut a
word in two. `_cell_findings` passes the reader's cell words for it, as it already passes the
tab's base words for `_welded`.

**Ctrl+K, the last undrawn field of the dialect.** `doc_merge.MANAGED` names eleven run
fields, and coverage said plainly that one of them had never been on a run in the campaign's
life: `link`. Both sides draw it now — the source through `RUN_MARKS`, the reader through
`read_link_word`, which is Ctrl+K on a word and, a quarter of the time, Ctrl+Shift+K taking
one off. It found nothing — 1,920 rounds over six settings (`mixed`, `themed`, `prose`,
`two_tables` and `imported_list`, at chains 4 to 10), drawing 150 to 240 times in each —
and on `read_paste_block`'s precedent it is kept for what it says while it keeps finding
nothing, with three tests in `test_doc_fuzz.py` pinning the behaviour it walks over — each
verified by breaking its mechanism.

The one place a link can be lost is not the restyle but a **move**: a move is a delete and a
write from nothing, so the words come back as the merge has them and the reader's styling has
to be carried onto them run by run (`_retext`, `_style_requests`), which it is
(`test_a_link_the_reader_made_survives_the_source_moving_the_block`; without `_text_style`'s
link it comes back a plain word). A source restyle of the block the reader linked a word of is
settled one rule earlier than the field — both sides restyled it, so the document's styling is
kept and the source's is not written — and the report says which way it went. And what
membership of `MANAGED` actually buys is narrower than it looks: `_text_style` writes a link
the run *has* whatever `MANAGED` says, so the field earns its place only where the run has
none. There a source restyle names `link` with no value in it, which is the API's "back to
what you inherit", and the `<a href>` the file no longer writes stops being a link in the
document too (`test_a_link_the_source_takes_off_goes_because_link_is_managed`; with `link` out
of `MANAGED` the link stays and nobody says so).

## Remaining risks

1. **Pictures** — retired, see "Pictures, and the chips a request can make" above. What
   is still open: a picture's size or alt text the *source* changes is not written (no
   request updates an inline object) — that one is said out loud now rather than dropped
   (`doc_merge.unwritten_pictures`, the table above), with the way to have the size
   anyway, but it is still a thing the file cannot simply ask for; and a picture a reader
   inserts comes back as the `contentUri`'s bytes, which Google may have re-encoded; the
   zip export would be the byte-exact route.
2. **Lists in the read-back.** `listId` is opaque and output-only; whether Docs forks or
   reuses one when a user splits a list in the UI is undocumented. What an *imported*
   list's glyphs read as is no longer a risk but a measurement — see the table above.
3. **Anchors under a human editor** — retired, see "Under a human editor" above. What
   is still unmeasured there: dragging a selection to a new place, "paste without
   formatting", and a second person editing concurrently.
4. **Tabs** — retired, see "Document tabs" above. Still open: a tab the source *moves*
   is not moved (a tab it adds now lands where the file puts it; the order of the ones
   already there is the document's, and a source reorder is reported rather than
   dropped). **Owed, and the one measurement that would close it**: does
   `updateDocumentTabProperties` with `fields: "index"` push the tabs it passes along,
   the way `addDocumentTab` says it does — and what does it do to a child tab's
   siblings, and to a tab it moves under another parent? One live document, three
   writes and three reads. Until then the reorder is refused, because an index written
   blind rearranges a strip somebody arranged by hand. Both names are carried: the
   document's, through Drive, and the first tab's own, in the `b2s-tab` meta.
5. **A bullet's nesting level cannot be written.** `createParagraphBullets` says nothing
   about one and no other request does either: Docs reads a level off the paragraph's
   leading tabs, which are text the merge does not write. So a level lives on a paragraph
   mark, and the three doors above — a block written from nothing, a block moved, a block
   handed the mark of the one deleted in front of it — all lose it.
   `doc_merge.unwritten_levels` says so before the write, which is honest but not a fix.
   **Owed, and the one measurement that would close it**: write the tabs. Does an
   `insertText` of `"\t\ttext"` at a paragraph's start make Docs read that paragraph as
   level 2 once `createParagraphBullets` runs over it, and does the read-back then show
   `nestingLevel: 2` with the tabs *gone* from the text (the UI's own behaviour) or still
   in it? If they are eaten, the level is writable and `_text_style`'s indices are
   unaffected; if they stay, every index the sync computes for that paragraph is two
   units out and the words themselves are wrong. One live document, two writes and two
   reads. Until then a level the source asks for and cannot have is reported.
6. **Page-level structure** — `documentStyle`, headers, footers, footnote bodies,
   section breaks and positioned objects are read by nobody and authored by nobody. The
   paragraph level is now nearly closed (borders, `pageBreakBefore` and `keepWithNext`
   went in with the rest), which leaves the page as the one place where a real document
   carries something the file has no word for. It is a risk that is *named*
   (`doc_ir.unmodelled`) rather than one that bites: nothing outside the blocks is ever
   rewritten, so the margins and the headers of a synced document survive untouched —
   what is missing is the ability to *author* them from the file.

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
equation's LaTeX survives a read, so the sync fetches it as a side-channel (see
"Equations") though the canonical file stays HTML.

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

The whole loop on real documents, about two minutes: a push whose import reads back as what the
file said, both sides editing (a table cell each, a block added, a list item rewritten),
a block appended at the very end, a section the source moved to the end of the document
(the reader's words, the styling and the block's key all ride along — and the paragraph
it left behind stood in front of a table, which is where that delete was found), the same
words rewritten on both sides (the document wins, and the conflict is reported), a table
the source added *and* a row it added to another one while a reader was typing in that
same table, an open comment named in the report, and a reader typing between the plan and
the write (the sync reads again and keeps their words). Every test ends by syncing once more and
finding **0 requests**, and each one deletes its document afterwards. Files stay in
`out/docs-tests/`. To drive one by hand instead:

```
.venv\Scripts\python.exe -m beamer2slides docs push out\docs-cli\doc.html
.venv\Scripts\python.exe -m beamer2slides docs sync out\docs-cli\doc.html   # after editing either side
```
