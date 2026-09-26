"""Lives in beamer2slides.devtools.plain_decks; this keeps `python tools/plain_decks.py make` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.plain_decks", run_name="__main__", alter_sys=True)
