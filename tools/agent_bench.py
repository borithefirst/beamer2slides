"""Lives in beamer2slides.devtools.agent_bench (tests import it from there); this keeps `python tools/agent_bench.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.agent_bench", run_name="__main__", alter_sys=True)
