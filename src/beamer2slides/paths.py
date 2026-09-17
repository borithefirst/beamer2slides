"""Where beamer2slides writes when no --out is given."""

from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]  # the source tree, if that is where we run from


def in_checkout() -> bool:
    """False for a pip-installed beamer2slides, whose package sits in site-packages."""
    return (CHECKOUT / "pyproject.toml").exists()


def out_root() -> Path:
    """`out/` in the source checkout when running from one, else `out/` in the current folder."""
    return (CHECKOUT if in_checkout() else Path.cwd()) / "out"
