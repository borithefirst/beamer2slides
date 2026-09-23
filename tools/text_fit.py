"""Moved to beamer2slides.devtools.text_fit; this keeps `python tools/text_fit.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.text_fit", run_name="__main__", alter_sys=True)
