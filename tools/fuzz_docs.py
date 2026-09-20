"""Moved to beamer2slides.devtools.fuzz_docs (tests import it from there); this keeps `python tools/fuzz_docs.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.fuzz_docs", run_name="__main__", alter_sys=True)
