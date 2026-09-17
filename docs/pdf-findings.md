# What beamer PDFs look like to PyMuPDF

Findings from running `tools/probe.py` on the test decks (MiKTeX 25.12, PyMuPDF 1.28.2).
Each finding ends with what it means for the pipeline.

Extraction now runs on PDFium (`pdf.py`), which reproduces the span, path and image output
described here; PDFium's own quirks are listed under Pitfalls in `CLAUDE.md`.

## Text
1. **One span per word, no space characters** (pdfTeX and LuaTeX). TeX positions each
   word explicitly, so spaces only appear inside some math runs (`' ='`, `') ='`).
   → Rebuild spaces from the horizontal gaps between spans (gap > ~0.2 em means a space).
2. **PyMuPDF's block/line grouping is unreliable.** A wrapped bullet's second line lands
   in a different block than its first line, and a third-level item shares a block with
   the following first-level item.
   → Do our own layout analysis: group by baseline (`origin` y), then by x-extent.
3. **Use the baseline, not the bbox.** Small caps (CMCSC10) and monospace (CMTT10) have
   different ascent metrics, so their bboxes are shifted 2–3 pt on the same line.
   The bboxes from LuaTeX's OpenType fonts are also much taller.
4. **Styles come from font names.** CMSS = regular, CMSSBX = bold, CMSSI = italic,
   CMTT = mono, CMCSC = small caps; LMSans*/LMRoman* under LuaTeX/XeLaTeX. Colours are
   exact (`\alert` = `#ff0000`, theme titles `#3333b3`).
5. **Unicode is good**: LuaTeX gives correct accents, ligatures resolved, curly quotes,
   em dash. pdfTeX's CM fonts also map cleanly, apart from CMEX big operators (`�`).
6. **Rotated text** (pgfplots `ylabel`) appears as a normal span; the line's `dir`
   vector tells the rotation.
   → Treat non-horizontal text as part of a figure.

## Lists
7. **Bullets vary with the theme:**
   - default theme: an MSAM10 `▶` glyph in the theme colour;
   - Madrid / Warsaw (ball bullets): **tiny inline raster images** (6×6 px), not text;
   - enumerate: a `1.` text span in the theme colour;
   - description: the term is a coloured span that hangs left of the body.
8. **Nesting shows up in x and size**: item text at x = 50.2 / 72.0 / 93.8 with sizes
   10.91 / 9.96 / 8.97 for levels 1 / 2 / 3. Wrapped lines align with their item's text,
   not with the bullet.
   → The level comes from the clusters of bullet x-positions on the slide.

## Math
9. **Beamer's sans-serif math puts variables in CMSSI10**, the same font as `\emph`.
   The font alone can't tell `$x$` from *x*.
10. **Reliable math signals**: CMMI / CMSY / CMEX / MSBM fonts (MSAM too, except bullet
    glyphs); sub/superscripts (smaller size plus a baseline shift); fraction bars and
    radical overbars (short horizontal stroke paths); `�` from CMEX.
    → Detect math per line. Display math and fractions go to the background. Simple
    inline math could later become Unicode (`x²`, `α`); at first the whole paragraph
    goes to the background.

## Figures and graphics
11. **Labels in figures are ordinary body-font text.** TikZ node labels and pgfplots tick
    labels are CMSS10 at body size.
    → Find figure regions from clusters of drawings (curves, many strokes, filled
    non-rectangular paths). Text inside a region stays in the background.
12. **`example-image-*` from mwe is a vector PDF** (grey rect plus a large `A` glyph),
    not a raster image. The decks have no real raster image yet.
    → Add PNG/JPEG images to the test decks.
13. **Every page starts with a full-page white rectangle**, which is ignored.

## Themes
14. **Blocks** (Madrid) are rounded filled paths (`clcll`) for the title and body bars, and
    their **drop shadows are small inline images** (350×5 px, 9×9 px corners).
    → Candidates for native rounded rectangles with text on top; shadows stay in the
    background.
15. **Headline and footline** are filled rectangles with small text (5.98 pt), including
    a **page counter `2 / 4`** that would go stale if slides are reordered.
    Warsaw's section navigation (`Intro`, `Body`) is also text.
    → Leave header and footer text in the background by default.
16. **Tables** are one span per cell plus horizontal rule strokes.
    → Could become native Slides tables later; start with text boxes or background.

## Deck structure
17. **Page labels = frame numbers.** Beamer writes PDF page labels, so the overlay pages
    of one frame share a label even without handout mode.
18. **Notes are not in the PDF** unless compiled with `show notes`; that adds note pages,
    and parsing them needs a separate pass.
19. **Links**: `page.get_links()` returns the URI and its rect, which can be attached to text runs.
20. **Page sizes**: 4:3 = 362.8 × 272.1 pt; 16:9 = 453.5 × 255.1 pt. Scaling by ×1.98
    to the standard Slides size (720 × 540 or 720 × 405 pt) turns body text into ~21.6 pt
    and titles into ~28.5 pt, normal Slides sizes.
