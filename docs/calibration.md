# Font calibration: Google Slides vs Computer Modern Sans

Produced by `tools/calibrate.py all` (the Slides deck is created once, and thumbnails are
cached in `out/calibration/thumbs`). Raw numbers are in `calibration/fonts.json`.
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
- **Bullet glyphs** (arrow, diamond, disc alike) end ≈ 1.9 pt before `indentFirstLine`.
  Text starts at `indentStart`.
- **Bullet colour and size cannot be set on their own.** A bullet takes a text style
  only when the whole paragraph has that style. Styling the first character or the
  paragraph's newline has no effect, and the list's `bulletStyle` is read-only. Beamer's
  coloured bullets on black text therefore come out black.
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

## 2b. Serif: width vs CM Roman (`--family serif`, `calibration/fonts_serif.json`)
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

## 3. Choice
- **Default for CM Sans: Lato** at size × 1/1.020. Humanist like CM Sans, cap height matches
  within 1%, and nothing gets wider than the original (titles use their own factor).
- **Arial** is the closest match in proportions across all categories (worst 3.9%), but it
  looks less like CM Sans.
- Serif (CMR) and monospace (CMTT) decks are not calibrated yet.
