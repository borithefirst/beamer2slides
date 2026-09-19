"""Lives in beamer2slides.devtools.render_torture_image (tests import it from there); this keeps `python tools/render_torture_image.py` working."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module("beamer2slides.devtools.render_torture_image", run_name="__main__", alter_sys=True)
