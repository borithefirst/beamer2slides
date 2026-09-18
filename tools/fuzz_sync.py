"""Moved to beamer2slides.devtools.fuzz_sync (tests import it from there); this keeps `python tools/fuzz_sync.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.fuzz_sync", run_name="__main__", alter_sys=True)
