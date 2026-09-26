"""Lives in beamer2slides.devtools.adopt_replay; this keeps `python tools/adopt_replay.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.adopt_replay", run_name="__main__", alter_sys=True)
