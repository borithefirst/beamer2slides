# gdg: a Google for Developers beamer theme

`beamerthemegdg.sty` rebuilds the public Google Slides template "GDG24 - PRO - Speaker
Presentation Template" for beamer. It is made so that a deck written in beamer (by a person or an
AI) converts through beamer2slides into a Google Slides deck that looks like one made from the
template: text boxes sit on the template's baselines, and cards, numbers and quotes become native
shapes and text.

Examples: `examples/gdg-talk.tex` (a 13-slide talk written only with the commands below) and
`examples/gdg-template-replica.tex` (the template's own slides, used to check geometry with
`tools/theme_compare.py`).

## Build

```
.\themes\google\make.ps1 themes\google\examples\gdg-talk.tex out\gdg
python -m beamer2slides convert out\gdg\gdg-talk.pdf
```

- LuaLaTeX, 16:9: `\documentclass[aspectratio=169]{beamer}` + `\usetheme[...]{gdg}`.
- `make.ps1` puts this folder on `TEXINPUTS`, so the theme and `fonts/` are found from anywhere,
  and runs two passes (the overlays use `remember picture`).
- The fonts are not committed (third-party binaries). `make.ps1` builds them the first time, or
  run `python themes/google/fonts/build_fonts.py` (needs fontTools, `pip install -e .[dev]`): it
  downloads the variable fonts from github.com/google/fonts into the git-ignored `fonts/src/`,
  checks them against pinned SHA-256 sums, and cuts the static `.ttf` files the theme loads.

## Writing slides

All sizes are in `\gpt`, one point of the 720 × 405 pt Slides page, so numbers can be read
straight off the template. Write plain beamer and pick from this vocabulary; don't position text
by hand (`\vspace`, minipages): the theme's spacing is what Slides reproduces.

### Theme options
| Option | Effect |
|---|---|
| `accent=blue\|red\|yellow\|green` | colour behind `gaccent`, `gaccent-pastel`, ... (default blue) |
| `sectionpages` | a chapter slide at every `\section` |

Title page: `\title` (wrap the emphasised word in `\textbf`), `\subtitle`, `\groupname{...}`,
`\location{...}`, then `\begin{frame}[plain]\titlepage\end{frame}`.

### Frame options
| Option | Layout |
|---|---|
| (none) | 47 pt headline, 14 pt body copy (itemize, paragraphs, columns) |
| `[dark]` | off-black background, white text |
| `[fragile,code]` | dark code slide, use the `code` environment |
| `[split]` | "Learnings": 24 pt heading and body in the left third; put a `\statcard`, `\smallstat` or `\sidecard` on the right |

### Text
- `\subhead{Size}`: 20 pt semibold heading above body copy. Use at the top of a column or between
  paragraphs; it spaces itself. Blocks (`\begin{block}{...}`) render as a sub head, no panel.
- `\begin{columns}[T] \begin{column}{192\gpt} ... \end{columns}`: three columns of 192\gpt fit the
  text width; two columns of 290\gpt.
- `\gsize[line spacing]{pt}` sets a Slides size, e.g. `{\gsize{24}\semibold Heading\par}`;
  `\semibold`, `\medium` pick weights.
- Keep it short: the headline is one line (about 30 characters), bullets one or two lines.

### Layouts
| Command | What |
|---|---|
| `\statcard[fill]{4×}{caption}` | big rounded card with a number, right half (use in `[split]`) |
| `\smallstat[fill]{x}{y}{0\%}{caption}` | 217 × 158 pt card, top left at (x, y); `(452.2,44.4)` and `(367.1,202.5)` stagger two in `[split]` |
| `\sidecard[fill]{heading}{body}` | tall card with a 24 pt heading and body, right half (use in `[split]`) |
| `\bignumber[fill]{92\%}{caption}` | whole-slide statistic with a caption pill (frame without title) |
| `\numbers{\keyfigure{280 MB}{text}}...` (six arguments) | 3 × 2 grid of key figures (frame without title) |
| `\quoteslide[colour][closing colour]{quote}{- author}` | centred 52 pt quote between quote marks (frame without title) |
| `\begin{code}[language=Java, emph={Type,Names}] ... \end{code}` | Google Sans Code with the template's syntax colours; `emph` words get the type colour |

Fills are the template colours: `gblue gred gyellow ggreen`, `-halftone` and `-pastel` versions
(`gblue-pastel`, ...), `gaccent*`, or `none` (outline only).

### Decorations
`\gdgat{x}{y}{content}` places anything at template coordinates (top left origin);
`\gdgarrow[colour]`, `\gdgdots[colour]` and `\gdgpill[colour]` are the template's outlined shapes.
Overlays are drawn behind the text and never move the text flow.

## Conversion notes
- Fidelity on Slides' renderer (`python -m beamer2slides fidelity`): the talk scores 96–100% per
  slide, the replica 94–100%.
- Google Sans Flex is cut at opsz 18 / wdth 100 (within 0.5% of the Google Sans Slides uses);
  PDFs name the fonts GoogleSansFlex-*, which the converter maps to Google Sans Flex in Slides.
- Word spacing is the font's own space without stretch (`WordSpace={1,0,0}`, `\frenchspacing`,
  also inside TikZ nodes), so lines break where Slides breaks them.

## Licences and trademarks
- Google Sans Flex and Google Sans Code are under the SIL Open Font License 1.1
  (`fonts/OFL-*.txt` once built); see `fonts/TRADEMARKS-GoogleSansFlex.md` for the name.
- The theme reproduces the look of a publicly shared community template. Google, Google Developer
  Groups and their logos are trademarks of Google LLC; the theme ships no logos. Use it for GDG and
  Google-related talks within Google's brand guidelines.
