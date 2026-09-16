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
- Overlays: compile in beamer `handout` mode (one page per frame) by default. The Slides
  API cannot create animations.
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
- `getThumbnail` LARGE = 1600 px wide (1600 × 900 for 16:9).

## Environment
- Windows, PowerShell. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`).
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
