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
- A local **HTML preview** of the IR is used to iterate without touching Google APIs.
- Overlays: compile in beamer `handout` mode (one page per frame) by default. The Slides
  API cannot create animations.
- Slides API image insertion needs a public URL: upload to Drive, share by link,
  insert, then revoke.

## Environment
- Windows, PowerShell. Python 3.12 venv in `.venv` (`.venv\Scripts\python.exe`).
- MiKTeX (pdflatex / xelatex / lualatex) for the test decks, with on-demand package install.
- Test decks: `tests/decks/*.tex`, built by `tests/decks/build.py` into `tests/decks/out/`
  (normal and `-handout` variants).
