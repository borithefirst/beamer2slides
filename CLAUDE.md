# beamer2slides

Convert beamer PDFs into **editable Google Slides decks**.

## Goal
The output should feel like a native Google Slides deck: real text boxes, image
elements and shapes. Anything that can't be converted with good fidelity
(math, TikZ/pgfplots figures, theme decorations we can't rebuild) is baked into
a per-slide background picture.

## Strategy (decided)
- **Input is the compiled PDF**, not the `.tex`. The PDF gives exact geometry, fonts and
  colours. The `.tex` / SyncTeX may later be used as a hint for structure.
- Pipeline stages communicate through a **JSON intermediate representation (IR)**
  so each stage is testable in isolation:
  1. `extract`: PDF → raw spans / images / vector drawings (PyMuPDF)
  2. `classify`: group spans into lines, paragraphs and lists; decide native vs. background
  3. `render`: background PNG per slide with the converted elements removed
     (redaction that keeps images and line art)
  4. `emit`: Google Slides API (primary); python-pptx → Drive import is the fallback
- **Fidelity is measured on Google's own renderer**, not a local preview: render the PDF page
  to PNG, export the emitted slide with `presentations.pages.getThumbnail`, and compare
  them (whole slide and per text box). A calibration deck measures font substitutes
  (width ratios, first-baseline offsets) once and feeds correction tables.
- Local classification debugging = PNG of the PDF page with element boxes drawn on it.
- Findings about beamer PDFs: `docs/pdf-findings.md`. IR draft: `docs/ir.md`.
- Overlays: non-handout PDFs keep the last step of each frame (same page label and heading);
  `--overlays all` keeps every page. The Slides API cannot create animations.

## What becomes native (element kinds in deck.json)
- `text`: paragraphs, bullet lists (glyph, number, image-ball and vector bullets), inline
  math as runs with sub/superscripts, links (external URLs and `#page=N` internal links →
  Slides page links), code blocks. Frame titles, the title page title and big lone headings
  use the layout's TITLE placeholder (role `title`).
- `image`: figure regions (TikZ, plots, raster images, plus their labels) cropped as pictures.
- `table`: text framed by equal-width horizontal rules (`\hline`/booktabs), with optional
  vertical and partial rules (→ per-cell `borders`) and merged cells (`merges`: chunks
  crossing columns, rows halfway between rows). Cell lineSpacing is tightened so rows keep
  the PDF pitch (`emit.table_line_spacing`).
- `diagram` (`classify.diagram_from`): figure clusters of only rectangle/rounded/ellipse nodes,
  straight lines and small arrow tips (open, filled, stealth) → shapes, lines and text boxes
  (node labels and free edge labels), grouped. Lines with filled tips are extended to the tip.
  Diagrams stay intact when edited: a label that fits the node's text rectangle (`label_inside`;
  Slides uses the .pptx text rectangle, an ellipse's is its inscribed square) goes inside a
  padding-free template node, otherwise node and label form a sub-group; line ends on a node's
  connection site are connected (`connection`), and `|-`/`-|` paths become one elbow connector
  (template bentConnector3 with adj 0 or 100000), so edges follow moved nodes.
  Card text (`card_text`: several sizes or lines in a node, e.g. a number over a caption) becomes
  text boxes on the PDF baselines, split where Slides can't set two paragraphs that close.
- `shape`: opaque filled panels such as beamer blocks, not touching the page edge, with
  nothing left in the background on top; verified against the rendered colour. Also
  figure clusters of plain filled rectangles (`plain_rectangles`, role `rule`).
- Text decorations (`text_decorations`): a thin rule tightly under words → run `underline`,
  a filled box tightly around words on one line → run `highlight` (backgroundColor).
- Inline formula holes (`formula_holes`, `Line.holes`): in a line of prose, complex math
  segments (bars, CMEX, stacked or second-level scripts) become image elements (role `math`,
  `anchor` = text element id, grouped with it). The text keeps a run with `hole` = width.
  Emit fills it with no-break spaces in Roboto Mono (a space is exactly 0.6 em), sized to the
  width, and shifts the picture by the predicted width error of the words before it
  (`formula_shifts`). Lines with fewer than two real words stay one display picture.
- Tables also take cell `fills` (filled rects inside a ruled table; their edges give column
  bounds) and simple math in cells (`span_runs` handles math fonts and scripts).
  `plain_tables`: rule-less tabulars (≥3 rows, same cell count, short cells) → borderless tables.
- `literal_list_numbers`: when Slides would number a list wrongly (TOC split into boxes),
  every number on the slide becomes literal text with a tab (Slides can't set a start number).
- Grouping in emit: blocks (`block_groups`: shapes with the same `block` plus content), formula
  pictures with their text, progress bars with their track (`rule_groups`). Title page:
  `subtitle_element` → SUBTITLE placeholder.
- Blocks built to survive resizing (`classify.blocks`, `emit.merge_blocks`): the body reaches up
  under the title bar with the whole block's outline (a group resize never opens a gap), title
  bars are created after bodies (z-order), block titles fill their bar with contentAlignment
  MIDDLE (baseline = middle + 0.362 em, `tools/probe_middle.py`), and the body carries a native
  drop shadow. The gradient strip and shadow pieces are painted out of the background.
- Template shapes: every deck starts from an imported .pptx (`build_pptx`, also for 16:9; rebuilds
  replace the content through `files.update`, keeping the URL) with one template slide per
  layout. Slides needing templates are `duplicateObject` copies of their layout's template slide;
  shapes are duplicated from the templates and restyled. This gives what the API can't set:
  drop shadows (calibrated against beamer: `tools/calibrate_shadow.py`, distance 0.75 and blur
  1.0 × shadow width, alpha 0.5) and exact corner radii (`adj`). Template slides are deleted at the end.
- Nothing stays behind when elements move (`tools/leftovers.py` checks it locally): pictures and
  block decorations are painted out of the background where the page around them is flat;
  numbered balls under literal TOC numbers become pictures grouped with their text.
- One-line left-aligned text boxes reach to the block edge or the next element
  (`text_right_limit`), so text typed later wraps where a user expects.
- Equation numbers beside display equations, icon-font glyphs (pictures) and OpenType
  small caps (`extract.small_caps_spans`, via glyph ids) are handled too.
- Hanging labels (`Line.tab`, paragraph `tab_x0`): algorithmic line numbers, description
  items and item labels without a Slides preset are written `label<TAB>text` with
  indentFirstLine at the label and indentStart at the text (Slides tabs jump to indentStart).
- Layouts (`style_layout_placeholders`, `write_layout_texts`): every layout's TITLE /
  CENTERED_TITLE placeholder gets the position and text style of the deck's frame titles /
  title page, BODY placeholders the most common body font, and shared footer texts go
  onto all layouts. A slide added later in Slides then looks like the converted ones.
- Backgrounds: identical PNGs are uploaded once; the most common background is set on the
  master and all layouts, and those slides inherit it. Frame counters (`FRAME_COUNTER_RE`) become
  per-slide text elements (role `footer`), so theme backgrounds become identical.
- Fonts: `fonts.google_font` passes Google fonts used in the PDF through with their weight
  (weightedFontFamily, no width correction) and maps Helvetica/Times/Courier clones to
  metric-compatible Arial/Times New Roman/Courier New. CM fonts use the calibrated substitutes.
- Simple inline fractions (`classify.simple_fraction`) are written as ᵃ⁄ᵦ; their bars leave
  the background via the text element's `strokes`.
- Performance: uploads run in 6 threads (one Drive service per thread) and slide content is
  sent in batches of up to 400 requests; a 45-slide deck converts in about 80 s.
- Speaker notes: beamer note pages (`show notes`) or `show notes on second screen`
  (`notes.py`), written to the slide's speaker notes.
- Everything else (display math, theme decoration, header/footer text) stays in the
  background picture. A background with nothing left becomes a plain background colour.
- Theme robustness: `tests/themes/sweep.py` compiles a realistic talk with 28 beamer themes
  and classifies them locally (no Google calls).
- Line building: words don't join across a column gutter (`PageClassifier.gutter`), lines up to
  1.45 em apart continue a paragraph, centred lines TeX balanced keep their breaks as soft
  breaks (chr 11), and rule-less tables whose cells wrap like prose aren't tables.
- Google theme (`themes/google`, README there): a beamer theme reproducing the GDG 2024 speaker
  template in Google Sans Flex, written for AI authors; sizes in `\gpt` so the theme's baselines
  follow Slides' text model (first baseline 6.48 + 0.968 em, pitch 1.2 em × line spacing).
- Slides API image insertion needs a public URL: upload to Drive, share by link,
  insert, then revoke.

## Google side
- GCP project `beamer2slides` (personal Gmail): Slides and Drive APIs enabled. The OAuth
  consent screen is External / Testing with the owner as the only test user, so refresh
  tokens expire after 7 days and the browser consent must be repeated.
- Auth: `src/beamer2slides/google_auth.py`, scopes `presentations` + `drive.file`.
  `client_secret.json` and `token.json` sit in the repo root, are git-ignored, and
  are ACL-restricted to the current user. Never print or commit their contents.
- Smoke test: `tools/slides_smoke.py`.
- **`presentations.create` ignores `pageSize`**: new decks are always 720 × 405 pt (16:9).
  4:3 decks need another route (e.g. upload a blank 4:3 .pptx with Drive conversion).
- `getThumbnail` LARGE = 1600 px wide (1600 × 900 for 16:9). Thumbnail download URLs
  occasionally fail with SSL EOF; `gslides.save_thumbnail` retries.
- Object IDs must be 5–50 characters.
- **Font calibration** (`tools/calibrate.py`, results in `docs/calibration.md` and
  `calibration/fonts.json`): in API-created text boxes the first baseline sits at
  6.48 pt + 0.968 em and the line pitch is 1.19 em **for every font**. Default substitute
  for CM Sans is Lato at size / 1.020; titles (CMSS12) need their own factor (~1.035).

- Background images: Drive "beamer2slides assets" folder, shared by link only while the
  batch runs (`uc?export=view&id=` URL; a fresh permission needs a few seconds before
  Slides can fetch it). They are set as the slide's `pageBackgroundFill`. After insertion the
  uploads are moved to the trash (Slides keeps its own copy), unless `--keep-assets`.
- Emit robustness: a rejected batch is retried slide by slide, then element by element; an
  element the API refuses becomes a picture of the original page region (`fallback_picture`).
  A page whose classification raises becomes a full background picture (`classify_page`).
- Decks are uploaded python-pptx files with Drive conversion (the page size is kept; the default
  template's placeholders are rescaled for 16:9).
- Re-running `convert` on the same output folder rebuilds the previous deck in place
  (same URL, content replaced by `files.update`). Use `--new-deck` to force a new one.

## Usage
```
python -m beamer2slides classify deck.pdf   # raw.json, deck.json, debug/ overlays
python -m beamer2slides convert  deck.pdf   # + background.pdf, backgrounds/, Slides deck, emit.json
python -m beamer2slides fidelity deck.pdf   # thumbnails vs PDF: fidelity.json, fidelity/diff-NNN.png
```
Outputs go to `out/<pdf stem>/`. In the diff PNGs, red = only in PDF, blue = only in Slides,
black = both.

## Pitfalls found so far
- Saving a redacted PDF with `garbage>=3` corrupts beamer soft-mask shadows (black bars).
- Redacting images in PDFs is unreliable; ball bullets are patched out of the PNG instead.
- Slides ignores spaceAbove/spaceBelow between bulleted list items (see docs/calibration.md).
- A bullet keeps the text style from when it was created, unless its whole paragraph later
  gets one uniform style. Set each paragraph's base family and size *before*
  createParagraphBullets, or mixed-style paragraphs get oversized 18 pt default bullets.
- Inline math: `classify.math_kind` sends lines with fractions, radicals, big operators,
  stacked or second-level scripts, or formula-like density to the background. Everything else becomes runs
  with `script` super/sub and Unicode symbols (MSBM → ℝ).
- MuPDF's line-art "covered" test is conservative (strokes, transformed TikZ nodes): figure
  and panel removal redacts with a few points of margin.
- Title placeholders exist before any other element: bring them to front after adding shapes.
- Slides table rows are at least 1.195 em × lineSpacing + 14.4 pt tall (7.2 pt cell padding,
  not settable); empty cells count with the default font unless given a styled space.
- Layout pages reject `pageBackgroundFill.propertyState = INHERIT`; set the fill explicitly.
- Pixel checks on hairline shapes need a high zoom (`render._fill_fraction`).
- Bullet colour/size can't be set independently (see docs/calibration.md).
- Page labels can come back as raw `<FEFF...>` hex strings; `extract._label` decodes them.
- PowerShell 5.1 mangles double quotes inside native-command arguments: keep them out of
  git commit messages passed via here-strings. `Get-Content -Raw` reads BOM-less UTF-8 as ANSI:
  edit text files with the editor tools, not a PowerShell read/replace/write.
- Shape shadows, autofit and text insets are read-only in the API. A .pptx import keeps shadows
  (and duplicateObject, fill and transform changes keep them) but not spAutoFit.
- python-pptx autoshapes refer to the theme's effect style, which has a shadow: always give an
  explicit `<a:effectLst/>`.
- Text inside a shape with a shadow gets a shadow too: body text stays in separate text boxes.
- Resizing a group scales every child (title bars get taller; fonts don't scale). Slides'
  thumbnail renderer draws shadows of children of a scaled group at the unscaled size.
- Beamer draws block shadows as black rectangles under a soft mask; removing the panels above
  them without redacting them too leaves solid black bars in the background.
- `fidelity` reuses saved thumbnails unless the deck was emitted again (or `--refresh`).

## Environment
- Windows, PowerShell. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`).
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
