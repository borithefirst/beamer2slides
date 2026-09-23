"""Lives in beamer2slides.devtools.fuzz_reach; this keeps `python tools/fuzz_reach.py <archive>` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.fuzz_reach", run_name="__main__", alter_sys=True)
