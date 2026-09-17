# beamer2slides

Turn a **beamer PDF** into an **editable Google Slides deck**. Text, lists, tables, pictures
and block panels become native Slides elements. Only what cannot be rebuilt faithfully
(display math, TikZ/pgfplots figures, theme decoration) becomes pictures or stays in the
slide background.

## What you get
- **Text boxes** with colours, bold/italic/small caps and links. Fonts that are Google fonts in
  the PDF (Fira Sans, Source Sans, Roboto, …) keep their family and weight. Helvetica, Times,
  Courier and their TeX Gyre/Nimbus clones become the metric-compatible Arial, Times New Roman
  and Courier New. Computer Modern gets calibrated substitutes (CM Sans → Lato, CM Roman →
  PT Serif, CM Typewriter → Roboto Mono). Line breaks and positions match the PDF within a few points.
- **Bullet and numbered lists** with nesting (glyph, ball, drawn and icon bullets). Description
  lists, algorithm line numbers and custom item labels (`\item[--]`) use a hanging label with a
  tab, so the text lines up exactly.
- **Inline math** as text: `x ∈ ℝ`, `x²`, `aᵢ`, simple fractions `ᵃ⁄ᵦ`. Complex inline formulas
  (roots, sums, stacked scripts) are small pictures placed over a gap in the still-editable
  text and grouped with it.
- **Underlines and `\colorbox` highlights** as text styles.
- **Slide titles** in real title placeholders, so they show in Slides' outline and navigation;
  the title page uses the title and subtitle placeholders.
- **Blocks** grouped with their text, formulas grouped with their paragraph, and pictures with
  alt text (the text they show).
- **Tables** from ruled tabulars (booktabs, `\hline`, vertical rules, `\cline`) as native Slides
  tables, including `\multicolumn`/`\multirow` merged cells, `\rowcolor`/`\cellcolor` shading and
  simple math in cells. Tabulars without any rules become borderless tables.
- **Diagrams**: simple TikZ pictures (rectangles, rounded rectangles, circles, straight arrows,
  node and edge labels) as grouped native shapes, lines and text.
- **Pictures** for other figures, plots, raster images and display equations, each movable.
- **Shapes** for beamer blocks (coloured title bars and bodies) and plain coloured bars.
- **Theme**: header/footer text that is the same on every slide goes onto the slide layouts.
  The background shared by most slides is set on the master, so new slides get it too.
  Frame counters become small text boxes on each slide.
- **Speaker notes** from `show notes` or `show notes on second screen`.
- **Internal links**: table-of-contents entries jump to their slides.
- Overlays: for non-handout PDFs the last step of each frame is kept (`--overlays all` keeps
  every step).

## Setup
1. Python 3.12: `pip install beamer2slides`, or from a checkout `python -m venv .venv` then
   `.venv\Scripts\pip install -e . pytest`. No TeX needed: the input is the compiled PDF.
2. A Google Cloud project with the Slides and Drive APIs enabled and an OAuth client of type
   *Desktop app*. Save its JSON as `client_secret.json` in this folder (git-ignored) or, for an
   installed beamer2slides, in `%APPDATA%\beamer2slides` / `~/.config/beamer2slides`.
   The first run opens a browser for consent and caches `token.json` beside it.

See `docs/install.md` for the credential search order and the scopes asked for.

## Usage
```
beamer2slides convert talk.pdf                     # or python -m beamer2slides convert talk.pdf
python -m beamer2slides classify talk.pdf          # local only: see decisions in out/talk/debug/
python -m beamer2slides fidelity talk.pdf          # compare Google's rendering with the PDF
```
Outputs go to `out/<pdf name>/`. Re-running `convert` on the same PDF updates the same
Google Slides deck — but if anyone edited that deck in Slides, convert stops and says so instead of
replacing it: either merge the new PDF into the deck with `sync` (keeping the edits), make a new
deck with `--new-deck`, or rebuild anyway with `--force-rebuild`, which keeps a `.pptx` backup in
`out/<pdf name>/backups` first (`docs/sync.md`, "Never lose deck edits";
`python tools/deck_backup.py list|export|restore` manages the backups).
Pictures and backgrounds travel inside a
.pptx that Drive imports as the deck's starting point: nothing is ever shared by public link,
so it works where link sharing is blocked. The only files the tool creates in Drive are the
decks themselves (`python tools/drive_usage.py` lists them).

In the debug images, green boxes are text, blue are pictures, purple tables and orange shapes.
Shaded text stays in the background (red math, blue figure, grey theme).

## How it works
`extract` (PDFium via pypdfium2) → `classify` (lines, paragraphs, lists, tables, figures, shapes) →
`render` (background without converted content, picture crops) → `emit` (a .pptx with the
pictures, imported by Drive, then filled through the Google Slides API).
Fidelity is measured on Google's own renderer via slide thumbnails. See `CLAUDE.md` and
`docs/` for design notes, calibration data and pitfalls.

## Tests
```
python tests/decks/build.py          # compile test decks (needs a TeX distribution)
python tests/themes/sweep.py --build # compile + classify a realistic talk in 28 beamer themes
python -m pytest tests               # regression tests (no Google calls)
```
