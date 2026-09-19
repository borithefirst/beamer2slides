"""Lives in beamer2slides.devtools.render_torture_type3 (tests import it from there); this keeps `python tools/render_torture_type3.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.render_torture_type3", run_name="__main__", alter_sys=True)
