"""Lives in beamer2slides.devtools.deck_export; this keeps `python tools/deck_export.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.deck_export", run_name="__main__", alter_sys=True)
