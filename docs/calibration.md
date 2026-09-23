# Font calibration: Google Slides vs Computer Modern Sans

Produced by `tools/calibrate.py all` (the Slides deck is created once, and thumbnails are
cached in `out/calibration/thumbs`). Raw numbers are in `src/beamer2slides/calibration/fonts.json`.
Reference: beamer + pdflatex, CMSS10 at 10.91 pt (body), CMSSBX10, CMSSI10, CMSS12 at 14.35 pt (titles).

## 1. Vertical placement in Slides does not depend on the font
For every candidate font (13 of them, cap heights ranging 0.63–0.72 em), in a `TEXT_BOX`
created through the API with `lineSpacing 100`, `spaceAbove/Below 0`:

| quantity | value | max residual |
|---|---|---|
| box top → first baseline | **6.48 pt + 0.968 em** | 0.14 pt over 14/24/40 pt |
| box left → ink of `H` | 6.7 pt + 0.086 em (varies ±0.3 pt with the letter's side bearing) | |
| baseline pitch (soft break or new paragraph) | **1.190 em** | |

PT Sans is the only slight outlier (+0.2 pt). So the converter needs **one vertical model**
for all fonts: to put a baseline at y, set box top = y − 6.48 − 0.968·size.
The fixed 6.5–6.7 pt is presumably the default text-box padding (not verified).

### Paragraph spacing (spacing probes, Lato 20 pt)
Model used by `emit.vertical_layout`, for a line of size z with lineSpacing r:

- The base pitch is **1.195 z**. The part above the baseline is 0.968 z and the part below is 0.227 z.
- **r ≥ 1** adds (r − 1)·1.195 z **below** each line of the paragraph. The first baseline
  of a box never moves.
- **r < 1** removes (1 − r)·1.195 z, about ¾ of it above the line and ¼ below it. This does move the first baseline up.
- `spaceAbove`/`spaceBelow` add exactly their value between ordinary paragraphs, but
  `spaceAbove` of the **first** paragraph in a box is ignored.
- **In bulleted lists, `spaceAbove` and `spaceBelow` between items are ignored** (except the
  first item's `spaceAbove`, which then shows above the list). The gap between items must
  therefore come from each item's lineSpacing.
- **Line pitch snaps to whole CSS pixels (0.75 pt).** Measured over 12–14 lines at 21.2 pt:
  pitch = round(1.2·z·r / 0.75) · 0.75 matches 9 of 11 lineSpacing values from 100% to 140%
  exactly; the two others were at most 0.7 pt lower. So the base pitch is 1.2 em before
  snapping, and the earlier 1.19–1.195 em estimates were pixel noise. Unmodelled, the
  rounding adds up: a 15-entry TOC drifted by 5 pt. `emit.vertical_layout` therefore predicts
  where Slides will put each line and aims every next paragraph at the original position
  from there.
- **Bullet glyphs** end a little before `indentFirstLine`: ≈ 1.9 pt at body sizes, or
  0.06–0.08 em of the bullet's own size (❏ 0.155 em). Text starts at `indentStart`.
- **Bullet colour and size** (`tools/probe_bullets.py`): a bullet keeps the style its
  paragraph had when `createParagraphBullets` ran, until a later `updateTextStyle` request
  covers the whole paragraph. So emit styles each bulleted paragraph in the bullet's colour
  and size first, creates the bullets, then styles the text in at least two requests.
  Styling only the first character or the newline has no effect, and `bulletStyle` is
  read-only. A bullet larger than its text pushes the line down.
- **Glyph choice**: a preset's own three glyphs are those of nesting levels 0–2; levels 3–8
  repeat ● ○ ■ whatever the preset. `createParagraphBullets` counts levels from the
  shallowest paragraph in its range, so a range starting deeper gets a dummy first paragraph.
  Ink heights per em: ● 0.41, ○ 0.43, ■ 0.45 (on the baseline), ➢ 0.525, ★ ◆ 0.81, ◇ 0.87.
- Presets have no filled right-pointing triangle (`LEFTTRIANGLE` is ◀). `ARROW3D` (➢) is the
  closest to beamer's ▶.

### Table cells (`tools/probe_table_rows.py`, Lato 21 pt)
- A cell places text like a text box: its first baseline is 6.48 + 0.968·z below the row top, moved by lineSpacing as above.
- The **minimum row height is 14.4 + 1.195·z·r**. The 14.4 pt is 7.2 pt of padding above and
  below, which the API cannot change. Measured values were 39.6/35.9/32.1/28.2/24.4 pt for r = 1/0.85/0.7/0.55/0.4.
  Text is not clipped when r < 1: it just starts higher in the cell.
- TeX tables are much tighter (row pitch ≈ 1.24 em). `emit.table_requests` therefore uses a
  lineSpacing ratio at which the rows keep the original pitch (at least 50%).
  Otherwise tables grow by about half their height.
- An empty cell still holds a line in the default font (a larger minimum), so it gets a
  space in the table's font.

## 2. Horizontal: width vs CM Sans at the same nominal size
`text` = ink width of running text relative to CMSS10 (size correction = 1/text).
The remaining columns are relative to the font's own `text` ratio, after that correction:

| font | text | CAPS | digits | bold | italic | title | worst | cap height after correction |
|---|---|---|---|---|---|---|---|---|
| Arial | 1.042 | 0.980 | 0.961 | 0.988 | 1.002 | 1.035 | 3.9% | 0.985 |
| Nunito Sans | 1.061 | 0.935 | 0.975 | 0.943 | 0.995 | 1.025 | 6.5% | 0.948 |
| **Lato** | **1.020** | 0.941 | 0.955 | 0.929 | 0.923 | 1.037 | 7.7% | **1.006** |
| Roboto | 1.045 | 0.906 | 0.933 | 0.922 | 0.967 | 1.035 | 9.4% | 0.977 |
| PT Sans | 1.013 | 0.872 | 0.941 | 0.904 | 0.939 | 1.029 | 12.8% | 1.028 |
| Open Sans | 1.102 | 0.862 | 0.913 | 0.972 | 0.940 | 1.037 | 13.8% | 0.926 |
| Source Sans 3 | 0.982 | 0.860 | 0.903 | 0.958 | 0.962 | 1.034 | 14.1% | 0.953 |
| Fira Sans | 1.076 | 0.824 | 0.847 | 0.908 | 0.978 | 1.035 | 17.6% | 0.916 |

(Inter, IBM Plex Sans, Carlito and Noto Sans are in the JSON; none beat the fonts above.)

- Running text is consistent within 1–3% for every font, so **one size correction per font
  works**. What differs is how capitals, digits and bold/italic compare: CM Sans has
  relatively wide capitals, digits and bold.
- **Titles** (CMSS12) come out ~3.5% wider for *every* font, because CMSS12 is a narrower
  optical size than CMSS10. This is a property of the TeX font, applied as its own factor.
- **Narrower is safe, wider is not**: text that ends up narrower never wraps unexpectedly.
  After correction, Lato is narrower or equal in every category except titles.
- "Source Sans Pro" is an alias of "Source Sans 3". No family fell back to Arial.

### Optical sizes (`emit.DESIGN_WIDTH`)
CM's small design sizes are wider per em than the 10 pt cut, and its large ones narrower.
Glyph advances of the Type 1 fonts over a sample sentence, relative to the 10 pt cut:
cmss8 1.062, cmss9 1.027, cmss12 0.975, cmss17 0.938; cmr5 1.376 … cmr17 0.914; cmtt8 1.011.
The size correction divides by these ratios, except for sans 12 pt, which keeps its directly
calibrated title factor (the ratio predicts 1.046 against the measured 1.058). Measured effect:
`\tiny` text (CMSS8 at 6 pt) went from 0.917 to 0.981 of the PDF width, `\Huge` (CMSS17) from 1.034 to 1.004.

## 2b. Serif: width vs CM Roman (`--family serif`, `src/beamer2slides/calibration/fonts_serif.json`)
Reference: CMR10 at 10.91 pt, CMBX10, CMTI10, CMR12 (titles).

| font | text | CAPS | digits | bold | italic | title | worst | cap height after correction |
|---|---|---|---|---|---|---|---|---|
| Georgia | 0.997 | 0.937 | 0.983 | 1.005 | 1.034 | 1.029 | 6.3% | 1.038 |
| Tinos / Times New Roman | 0.913 | 0.996 | 1.007 | 0.925 | 1.009 | 1.010 | 7.5% | 1.048 |
| **PT Serif** | **1.005** | 0.891 | 0.948 | 0.934 | 0.960 | 1.031 | 10.9% | **1.030** |
| Spectral | 1.002 | 0.967 | 0.889 | 0.893 | 0.922 | 1.021 | 11.1% | 0.976 |
| EB Garamond | 0.862 | 1.037 | 0.998 | 0.955 | 0.974 | 1.007 | 4.5% | 1.169 |

**PT Serif** is the default serif substitute: it needs almost no size correction and nothing
comes out wider than the original, except titles, which have their own factor. Georgia is
the most consistent, but its italic is 3.4% wider. The vertical model is font-independent
here too (baseline offset 6.5–7.2 pt + 0.945–0.971 em).

## 2c. Small caps, numbers, em spaces, scripts (`tools/probe_text_fit_fonts.py`)
What `tools/text_fit.py` found on the torture deck `tests/decks/27_text_fit.tex`, measured on
Google's renderer (advances as in `tools/probe_advances.py`, one reference row per font):

- **Small caps.** Slides draws a `smallCaps` lowercase letter as its capital at **0.70** of the
  size, advance and ink height alike (PT Serif m/M 0.700, a/A 0.705, w/W 0.699; Lato 0.695), where
  CMCSC10 draws it at 0.755 - and CMCSC is an extended face (M 0.988 em against CMR10's 0.916),
  while PT Serif's capitals are 11% narrower than CMR's. A CMCSC line therefore came out **0.777**
  of the PDF's width. Over any sentence the substitute small caps are 1.27-1.30 times too narrow
  (probe advances against CMCSC10.afm), so a Computer Modern small-caps run is set at
  **1.28 x** the serif size (`emit.SMALL_CAPS_WIDTH`): width 0.777 -> 0.996 on the torture line.
  The price is height: the capitals of such a line stand ~30% taller than CMCSC's, its small
  capitals ~20% taller - the face cannot be both as wide and as tall as TeX's.
  How Slides lays out a line holding a small-caps run (`line_size_pt`, PT Serif 26 small caps in a
  Lato 20 line): lowercase letters only - the run is drawn wholly in the small font and the line
  takes 0.70 of its size (`mm` 20.3, and 24.0 for 34 pt); one space, capital or comma in the run
  and the line takes its full size (`mm mm`, `Mm`: 26.3). `emit.line_size` is that rule (it
  replaces a flat 0.9).
- **Numbers.** Lato's digits are tabular at 0.577 em (bold 0.581) where Computer Modern's are
  0.5 (CMSSBX 0.55, CMBX 0.575), and stand at cap height, 7% taller than CM's. The size factor is
  calibrated on sentences, so a number column came out 8-14% wider than the PDF's. A run that is
  only a number (digits and `, . : % / - ( ) +`, no letter; not a script) is set at the size that
  gives it the PDF's width from those advances (`emit.CM_NUMBER_EM`, `FontMapper.number_ratio`):
  torture table columns 1.03-1.14 -> 0.97-1.02, prose unchanged to 0.004. A number inside a
  sentence keeps the sentence's size (a mixed-size word would be what a person types after).
  Bold header words are *not* wider: 0.96 (the `width` findings on the header table's elements
  were the digits under the header).
- **Em spaces in typewriter text.** Roboto Mono draws every space - U+0020, U+2003, U+2002, U+2009,
  U+00A0 - at its one advance, 0.600 em, as its digits and letters: the CMTT -> Roboto Mono size
  factor (0.6 / 0.525, with the optical sizes) is exact, and a typewriter line with no wide gap
  comes out at 0.996. What classify writes for a gap of an em or more is `" " + n em spaces`
  (`classify.py`, the em space counted as one PDF em), and in Roboto Mono an em space is 0.525
  PDF em where CMTT's `\quad` is 1.05: a `\quad` loses 5.7 pt at 10.9 pt, three of them made the
  torture's typewriter line 0.912 wide. Emit cannot fix that without changing the text or putting
  a proportional font inside code; in monospaced text the gap is `round(gap / advance)` plain
  spaces, which is classify's to write. Lato's and PT Serif's em spaces are 1.00 em (their spaces
  0.19 and 0.25 em).
- **Scripts.** `baselineOffset` SUBSCRIPT and SUPERSCRIPT set the run at **0.665** of its size
  and move it by **0.37-0.38 em** of the nominal size (Lato H at 40 and 20 pt: down 14.85 / 7.65 pt,
  up 15.3 pt). TeX sets a text-style script at 0.73 of the size and lowers a subscript by 0.15 em
  (0.25 em with a superscript beside it), raising a superscript 0.41 em - so superscripts agree and
  subscripts sit 0.2 em lower, which is what makes two lines of inline math touch (torture frame
  `inline-math`, `crowded`). The API has no other offset: the offset follows the nominal size, so
  bringing it to TeX's would leave the glyph at a third of the text size, and a smaller run with
  no offset is no longer a subscript to the person editing it (nor to `deck_ir`). Left as it is.

## 3. Choice
- **Default for CM Sans: Lato** at size × 1/1.020. Humanist like CM Sans, cap height matches
  within 1%, and nothing gets wider than the original (titles use their own factor).
- **Arial** is the closest match in proportions across all categories (worst 3.9%), but it
  looks less like CM Sans.
- Serif (CMR): PT Serif, section 2b. Monospace (CMTT): Roboto Mono at 0.525 / 0.600 of the size
  (with CMTT's optical sizes), exact since both are monospaced (section 2c).
