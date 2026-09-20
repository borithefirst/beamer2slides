"""Lives in beamer2slides.devtools.refusal_sweep; this keeps `python tools/refusal_sweep.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.refusal_sweep", run_name="__main__", alter_sys=True)
