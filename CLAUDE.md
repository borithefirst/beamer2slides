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
  1. `extract`: PDF → raw spans / images / vector drawings (PDFium through pypdfium2, `pdf.py`)
  2. `classify`: group spans into lines, paragraphs and lists; decide native vs. background
  3. `render`: background PNG per slide with the converted elements removed
     (page objects switched off, partly removed objects composited from a second render)
  4. `emit`: a python-pptx deck carrying all pictures, imported by Drive, then the Slides API
- No PyMuPDF: PDF access is PDFium only. `pdf.py` wraps it (`notes.py` also uses pypdfium2 to
  write slides.pdf); its text and path output reproduces what the MuPDF-based extraction gave (span splitting at word gaps, `re`/`qu`
  path items, char boxes from font ascent/descent), so classify's thresholds still hold.
- No public links: pictures reach Slides inside the imported .pptx, never as shared Drive
  files (they break in protected Workspace domains).
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
- Bullet shapes (`emit.BULLET_SHAPES`, `tools/probe_bullets.py`): bullets keep their PDF colour
  and height (ball colour sampled in render, `ink_colour`; vector bullets' shape from their path,
  `classify.bullet_shape`). The glyph is a preset plus a nesting level (`emit.bullet_level`: ● ○ ■
  repeat below level 3, other glyphs exist at one level only); ranges starting deeper than level 0
  get a dummy first paragraph. Icon-font labels (`\ding`), unrecognised vector bullets (the
  bibliography icon) and `\includegraphics` labels become pictures anchored to their item.
  Short labels ending where a neighbour's hanging label ends get its tab (`label_tabs`).
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
  `closed_frames`: four stroked lines closing a rectangle (`\fbox{\parbox}`, `\fcolorbox` around a
  minipage) are one rectangle node, merged with the fill inside; its justified lines stay one
  left-aligned wrapped paragraph.
- `shape`: opaque filled panels such as beamer blocks, not touching the page edge, with
  nothing left in the background on top; verified against the rendered colour. Also
  figure clusters of plain filled rectangles (`plain_rectangles`, role `rule`).
- Text decorations (`text_decorations`): a thin rule tightly under words → run `underline`,
  a rule through the x-height → run `strike` (ulem draws one piece per word: pieces are merged),
  a filled box tightly around words on one line → run `highlight` (backgroundColor). soul's
  `\hl`/`\st`/`\ul` draw overlapping pieces per word and wrap across lines: pieces are merged too;
  a wrapped underline's next line of text doesn't count as words under a fraction bar.
- Words on small graphics (`graphic_holes`: `\circled`, keycaps, `\fbox`, badges, dashed boxes,
  `\textcircled`'s overlapping glyphs) become holes like inline formulas, so text and graphic
  move together; `Line.hole_pads` widens a hole to the graphic (a closed frame may be wider than
  its words: `\framebox[2.5cm]`; the space after a hole counts from the graphic's end). `\cancel`
  strokes are LINE10 glyphs (icon font → graphic → hole). `join_braces`: an `\underbrace` /
  `\overbrace` glyph line and its label join the prose line, so the formula hole takes them.
  Small graphics on a native panel (the QED box) become pictures (`specks_on_panels`), or the
  panel would hide them.
- Graphics drawn over or at native words (`classify.overlay`: tikzmark arrows, braces, callouts,
  emphasis ellipses) become transparent pictures of only their own drawings and labels
  (`overlay`, `render.crop_overlay`), anchored to the text. Their `marks` (word edges they meet,
  with the words before, holes and em-space gaps closed up) are measured with the holes
  (`measure_places`): the words they lie on (`mark_words`: found in the box text by the words
  before them; a left edge pairs with the right edge on its line) are highlighted in a mark
  colour kept apart from same-coloured highlights nearby, `pick_word` finds them, and
  `fit_overlay` moves the picture by the mean drift and stretches it by the least-squares slope
  (±30% measured: a brace spans its phrase, an arrow meets both words; the callout's label
  stretches along, 5% on the test deck). The move is a RELATIVE transform with scaleX, its
  translation corrected for the scale about the page origin. Unmeasured marks keep the
  prediction (`mark_drifts`, formula-gap model; `overlay_boxes`, ±10%).
  A translucent fill over text (`opacity` < 1) becomes a `highlight` shape with fill alpha,
  anchored to that text. Text turned ±90° (`rotated_texts`) becomes a text box laid out in its
  own frame and turned by the transform. Path bounds use the curve's extremes, not its control
  points (`pdf._curve_extremes`). Test deck: `22_overlays_on_text`.
- Accents PDFium reports as separate chars (`ACCENTS`: ¯ ˆ ˜ …) become combining marks on
  their letter (X̄), also when the accent landed in the previous span.
- Hole and overlay pictures are placed by measurement (`emit.measure_places`, ~2-3 s per deck):
  scratch slides get copies of the text boxes with holes or overlay words (run highlights
  removed), every hole run and marked word highlighted in a mark colour and all text black; one
  thumbnail each (in parallel) shows the real gaps (`find_marks`, sub-pixel;
  `pick_gap` also follows a hole Slides wrapped onto another line), a hole hanging past the box
  edge at a line end is found from the ink before it (`ink_end`), and pictures get a RELATIVE
  transform before grouping; scratch slides are deleted with the sources. Debug output:
  `out/.../holes/marks-NNN.png`, `moves.json`, `overlays.json` (per mark: x, predicted and measured
  drift). `--predict-places` (alias `--predict-holes`) skips it. `fit_holes` sizes a
  hole to the PDF room up to the next word (`next_x0`) less a Slides space, and `hole_offset`
  shares the spaces left and right of the picture in the PDF's proportion.
- `formula_shifts` (the fallback prediction) uses Slides' symbol advances
  (`emit.SYMBOL_ADVANCE_EM`, measured by `tools/probe_symbols.py`) and TeX's stretched spaces
  (`space_shift`; a gap holding an earlier hole counts as a space plus that hole); holes get
  `HOLE_PAD` (1 pt) each side so the picture never touches a word. The `text_overlap` score
  can drop slightly when pictures follow Slides' words rather than the PDF positions.
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
  Labels on balls, circles and squares (numbers or letters) always become the ball picture with
  a centred text box on it (`number`, `emit.number_box_requests`), grouped with the item text.
- Grouping in emit: blocks (`block_groups`: shapes with the same `block` plus content), formula
  pictures with their text, progress bars with their track (`rule_groups`). Title page:
  `subtitle_element` → SUBTITLE placeholder.
- Blocks built to survive resizing (`classify.blocks`, `emit.merge_blocks`): the body reaches up
  under the title bar with the whole block's outline (a group resize never opens a gap), title
  bars are created after bodies (z-order), block titles fill their bar with contentAlignment
  MIDDLE (baseline = middle + 0.362 em, `tools/probe_middle.py`), and the body carries a native
  drop shadow. The gradient strip and shadow pieces are painted out of the background.
- The imported .pptx (`build_pptx`, also for 16:9; rebuilds replace the content through
  `files.update`, keeping the URL) has one source slide per deck slide: its layout, its own
  background picture or colour (none when it inherits the master's), its pictures at their exact
  boxes (alt text in `descr`) and, if needed, template shapes. Phase 1 `duplicateObject`s each
  source slide with an objectIds map (`b2s_sNNN`, pictures `_fI`, template shapes `_kJ`,
  placeholders `_tK`), which keeps backgrounds and pictures; the source slides are deleted at the
  end (`tools/probe_pptx_pictures.py` verified what the import keeps). Template shapes are restyled
  copies giving what the API can't set: drop shadows (calibrated against beamer:
  `tools/calibrate_shadow.py`, distance 0.75 and blur 1.0 × shadow width, alpha 0.5) and exact
  corner radii (`adj`). Pictures get `BRING_TO_FRONT` in element order after the other content.
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
- Backgrounds: identical PNGs are stored once in the .pptx; the most common background is set on
  the master (layouts and those slides inherit it). Frame counters (`FRAME_COUNTER_RE`) become
  per-slide text elements (role `footer`), so theme backgrounds become identical.
- Fonts: `fonts.google_font` passes Google fonts used in the PDF through with their weight
  (weightedFontFamily, no width correction) and maps Helvetica/Times/Courier clones to
  metric-compatible Arial/Times New Roman/Courier New. CM fonts use the calibrated substitutes.
- Simple inline fractions (`classify.simple_fraction`) are written as ᵃ⁄ᵦ; their bars leave
  the background via the text element's `strokes`.
- Performance: one .pptx upload, then slide content in batches of up to 400 requests; the test
  decks convert in 12-20 s (extract + classify + render of a 30-page deck take about 2 s).
- Speaker notes: beamer note pages (`show notes`) or `show notes on second screen`
  (`notes.py`), written to the slide's speaker notes.
- Everything else (display math, theme decoration, header/footer text) stays in the
  background picture. A background with nothing left becomes a plain background colour.
- Theme robustness: `tests/themes/sweep.py` compiles a realistic talk with 28 beamer themes
  and classifies them locally (no Google calls).
- Offline request tests (`tests/test_emit_requests.py`, ~5 s for all built decks): `emit.plan_offline`
  plans what `build_deck` sends (`DeckPlan`: .pptx picture boxes, phase 1 copies, each slide's parts;
  `measure_jobs`: measure_places' scratch slides) against a made-up imported presentation, and the tests
  replay it: object ids, groups, anchored pictures grouped with their text, numbers centred on balls,
  hole widths, bullet styling order, text ranges and texts, page bounds, predicted shifts < 15 pt
  (overlay stretch ±10%), every overlay mark on a highlighted word.
  Unit tests there cover the placement helpers. `build_deck` only adds Google's answers and batching.
- Invariants (`checks.py`, `tests/test_invariants.py`, ~40 s over all test decks and theme talks,
  no Google calls): `convert_locally` extracts, classifies and renders with pictures kept in memory,
  then `stray_ink` (no background ink within 2.5 pt of native line boxes, bullets and anchored
  pictures, unless under an opaque shape or unanchored picture, on a raster image, or running on
  10 pt past the words: page structure), `stray_labels` (no small drawing/image/glyph left at
  x-height left of a bullet-less paragraph), `lost_ink` (PDF page vs background with shapes painted:
  every difference lies on native glyphs, bullets, pictures, tables, diagrams, strokes or shape
  rims/strips/shadows), `structure` (hole runs vs pictures after `emit.fit_holes`, number pictures
  on balls, overlay anchor/marks, decoration strokes, shape bullets, ids and references) and
  `junk_text` (private-use, U+FFFD, picture-font ❤❙✭, control chars, lone combining marks).
  Known problems go in `tests/invariants_allow.json` (deck, page, check, element/bbox, reason);
  stale entries fail. Each check was verified by breaking its mechanism on purpose.
- Line building: words don't join across a column gutter (`PageClassifier.gutter`), lines up to
  1.45 em apart continue a paragraph, centred lines TeX balanced keep their breaks as soft
  breaks (chr 11), and rule-less tables whose cells wrap like prose aren't tables.
- Google theme (`themes/google`, README there): a beamer theme reproducing the GDG 2024 speaker
  template in Google Sans Flex, written for AI authors; sizes in `\gpt` so the theme's baselines
  follow Slides' text model (first baseline 6.48 + 0.968 em, pitch 1.2 em × line spacing).

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

- The Slides API's `createImage` needs a URL Google can fetch; that is why pictures go through
  the .pptx instead (Google's import stretches a picture to its box; `createImage` would
  letterbox it).
- Emit robustness: a rejected batch is retried slide by slide, then element by element; if the
  API refuses elements, the deck is rebuilt once with pictures of those page regions in their
  place (`fallback_pictures`). A page whose classification raises becomes a full background
  picture (`classify_page`).
- Decks are uploaded python-pptx files with Drive conversion (the page size is kept; the default
  template's placeholders are rescaled for 16:9).
- Re-running `convert` on the same output folder rebuilds the previous deck in place
  (same URL, content replaced by `files.update`). Use `--new-deck` to force a new one.

## Usage
```
python -m beamer2slides classify deck.pdf   # raw.json, deck.json, debug/ overlays
python -m beamer2slides convert  deck.pdf   # + backgrounds/, figures/, Slides deck, emit.json
python -m beamer2slides fidelity deck.pdf   # thumbnails vs PDF: fidelity.json, fidelity/diff-NNN.png
```
Outputs go to `out/<pdf stem>/`. In the diff PNGs, red = only in PDF, blue = only in Slides,
black = both.

Alignment on Google's renderer (`tools/alignment.py out/<deck>`, after `fidelity`): reads the
Slides element boxes with `presentations.get` (cached in slides_elements.json) and compares each
thumbnail with the PDF rendered on the same pixel grid, in PDF pt. Writes alignment.json, a table,
and evidence crops `alignment/NNN-item.png` (PDF above, Slides below, measured boxes and edges in
red; `--crops all|failures|none`). It measures:
- hole pictures (anchored math/icon): ink gaps left and right of the picture on its line, Slides
  minus PDF (≤ 1.5 pt), and overlap (text ink touching the picture ink or cut at its opaque box);
- numbers on balls: number-coloured ink centroid minus the ball box centre, Slides minus PDF (≤ 1 pt);
- bullets: ink box and colour in the bullet's cell, ΔE (≤ 15) and size ratio (within 30%; heights
  only for ➢, which stands in for ▶); a translucent highlight over a bullet is unblended first;
- overlay marks: word edge minus where the (stretched) picture meets it, Slides minus PDF (≤ 2 pt).
Picture ink is the picture file itself (alpha, or pixels unlike the page around the box) placed
into its box, so text ink is the rest; the page colour comes from a ring around the picture.

Pull back to the source (`docs/sync.md`, "Pull"): `python -m beamer2slides pull --deck <url|id|out>
--tex main.tex [--apply]` reads the deck only (`deck_ir.py`: live deck → deck.json-shaped IR through
emit's text box model and FontMapper) and loops compile → classify → `compare.py` residuals →
translators (`inverse.py`; `texmap.py` maps pages to frames by SyncTeX and words to source spans by
printed text) until the source's conversion matches; `edits.md`/`edits.json`/`pull.patch` in
`<out>/pull` hand unresolved residuals (with frame file:lines) to an AI. `converge --target deck.json`
is the offline twin. Tests: `tests/test_inverse.py` (offline: texmap, compare, planned edits on
`tests/decks/inverse/a.tex` vs `a.deck.json`, deck_ir via `tests/slides_sim.py`); opt-in
`python -m pytest -m inverse` (compile loop on the `b_*.tex` pairs and synthetic edits,
iterations in `tests/decks/inverse/out/results.json`).

Opt-in suite (real Google Slides, ~95 s): `python -m pytest -m slides` (the default run deselects
the `slides` marker, pyproject.toml). It converts the stress decks (19–22, 25, 13, demo) 3 at a
time into `out/slides-tests/<deck>` of the main checkout (fixed folders: the same decks are
rebuilt), runs `fidelity --refresh` and the measurement, and fails on items over the thresholds
unless `tests/slides_baseline.json` lists them under `known_failures` (with a verdict), or when
an error grows more than 0.75 over its baseline. `B2S_UPDATE_BASELINE=1` rewrites the baseline
(new failures come in marked UNVERIFIED), `B2S_SLIDES_REUSE=1` measures the existing folders
again without converting. Skipped with a message when the token needs a browser consent.
Measurements repeat to 0.01 across rebuilds (Slides renders deterministically).

## Pitfalls found so far
- PDFium (`pdf.py` handles these):
  - Soft-mask contents are not page objects. Beamer's block shadow is a black rectangle under
    a soft mask (`FPDFPageObj_HasTransparency` with an opaque fill); its visible pieces right
    of and below the panel are reported as shading images (`extract._shadow_pieces`).
  - Shadings are page objects of their own (type 4), usually inside form XObjects; their box
    is the clip. Images and shadings are both `images` in raw.json.
  - A ligature glyph comes back as several characters at the same origin: merged into one.
  - The text page reorders text objects on a line; chars are sorted back into content order
    (big operators' limits, accents).
  - pypdfium2's `get_cropbox` falls back to Letter when the box is inherited:
    `FPDF_GetPageBoundingBox` instead. `FPDFPageObj_GetIsActive` takes an out pointer.
  - Math fonts from xdvipdfmx carry their bounding box as ascent/descent (CMEX: −2.96 em);
    those get 0.8/−0.2. CMYK colours convert slightly differently from MuPDF (#fff101 yellow).
  - Type 3 (bitmap) fonts have no base name (`Type3`) and return glyph codes: a TS1 `\textbullet`
    (metropolis under pdflatex without cm-super) comes back as U+0088 (`classify.TYPE3_SYMBOLS`).
  - Page labels can't be rewritten: after `notes.prepare` deletes note pages, the kept pages'
    labels are passed to `extract(pdf, labels)`.
- Switching objects off (`FPDFPageObj_SetIsActive`) needs no content regeneration and is
  undone after each render, so crops and backgrounds share one open page.
- Ball bullets are patched out of the PNG (themes draw them with soft masks shared with shadows).
- Slides ignores spaceAbove/spaceBelow between bulleted list items (see docs/calibration.md).
- A bullet keeps the text style from when it was created, unless one later style request covers
  its whole paragraph. Set each paragraph's family, size and colour *before*
  createParagraphBullets, or bullets get oversized 18 pt default bullets.
- Inline math: `classify.math_kind` sends lines with fractions, radicals, big operators,
  stacked or second-level scripts, or formula-like density to the background. Everything else becomes runs
  with `script` super/sub and Unicode symbols (MSBM → ℝ).
- Figure removal switches off paths whose bounds lie within the figure box + 5 pt (strokes and
  arrow tips reach out); panel removal uses 1.5 pt, then 5 pt if the panel's path is still there.
- Title placeholders exist before any other element: bring them to front after adding shapes.
- Slides table rows are at least 1.195 em × lineSpacing + 14.4 pt tall (7.2 pt cell padding,
  not settable); empty cells count with the default font unless given a styled space.
- Layout pages reject `pageBackgroundFill.propertyState = INHERIT`; set the fill explicitly.
- Pixel checks on hairline shapes need a high zoom (`render._fill_fraction`).
- Bullet colour/size can be set independently only through creation order: style the paragraph
  like the bullet, create bullets, then style the text in two or more requests (a single request
  over the whole paragraph restyles its bullet too). See docs/calibration.md.
- Page labels can come back as raw `<FEFF...>` hex strings; `extract._label` decodes them.
- PowerShell 5.1 mangles double quotes inside native-command arguments: keep them out of
  git commit messages passed via here-strings. `Get-Content -Raw` reads BOM-less UTF-8 as ANSI:
  edit text files with the editor tools, not a PowerShell read/replace/write.
- Shape shadows, autofit and text insets are read-only in the API. A .pptx import keeps shadows
  (and duplicateObject, fill and transform changes keep them) but not spAutoFit.
- python-pptx: setting top/height on a layout placeholder that inherits its position writes x and
  width 0 and scales the (already rescaled) master position again: only rescale placeholders with
  their own `a:xfrm`.
- python-pptx autoshapes refer to the theme's effect style, which has a shadow: always give an
  explicit `<a:effectLst/>`.
- Text inside a shape with a shadow gets a shadow too: body text stays in separate text boxes.
- Resizing a group scales every child (title bars get taller; fonts don't scale). Slides'
  thumbnail renderer draws shadows of children of a scaled group at the unscaled size.
- Beamer draws block shadows as black rectangles under a soft mask; removing the panels above
  them without switching those off too leaves solid black bars in the background.
- `fidelity` reuses saved thumbnails unless the deck was emitted again (or `--refresh`).

## Environment
- Windows, PowerShell. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`).
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
