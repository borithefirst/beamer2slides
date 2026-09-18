"""What an installed (pip) beamer2slides needs: package data and credential/output paths."""

import json
import tomllib
from importlib import resources
from pathlib import Path

from beamer2slides import emit, google_auth, paths

ROOT = Path(__file__).resolve().parents[1]


def test_calibration_ships_with_the_package():
    # Reached through the package, so a wheel, a zip import and a build that stages the sources
    # somewhere else all find it; the checkout's own layout never comes into it.
    assert emit.CALIBRATION_DIR == resources.files("beamer2slides") / "calibration"
    for path in (emit.CALIBRATION, emit.CALIBRATION_DIR / "fonts_serif.json"):
        assert path.is_file() and json.loads(path.read_text(encoding="utf-8"))


def test_the_wheel_declares_the_calibration_and_the_command():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = meta["tool"]["setuptools"]["package-data"]["beamer2slides"]
    assert any(emit.CALIBRATION.name in pattern or pattern.endswith("*.json") for pattern in data)
    assert meta["project"]["scripts"]["beamer2slides"] == "beamer2slides.__main__:main"


def test_out_root_is_the_checkout_here_and_the_current_folder_when_installed(monkeypatch, tmp_path):
    assert paths.in_checkout() and paths.out_root() == ROOT / "out"
    monkeypatch.setattr(paths, "in_checkout", lambda: False)
    monkeypatch.chdir(tmp_path)
    assert paths.out_root() == tmp_path / "out"


def test_credentials_come_from_the_override_then_a_home_then_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("B2S_TOKEN", str(tmp_path / "given.json"))
    assert google_auth.credential_file("B2S_TOKEN", "token.json") == tmp_path / "given.json"
    monkeypatch.delenv("B2S_TOKEN")
    monkeypatch.setattr(google_auth, "ROOT", tmp_path)
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    assert google_auth.credential_file("B2S_TOKEN", "token.json") == tmp_path / "token.json"
    (tmp_path / "token.json").unlink()
    assert google_auth.credential_file("B2S_TOKEN", "token.json") == google_auth.config_dir() / "token.json"
    assert google_auth.config_dir().name == "beamer2slides"
