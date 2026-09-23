"""Lives in beamer2slides.devtools.probe_layout (the layout-* sync scenarios import it from there); this keeps `python tools/probe_layout.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.probe_layout", run_name="__main__", alter_sys=True)
