"""Moved to beamer2slides.devtools.visual_hunt; this keeps `python tools/visual_hunt.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.visual_hunt", run_name="__main__", alter_sys=True)
