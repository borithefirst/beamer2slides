"""What an installed (pip) beamer2slides needs: package data and credential/output paths."""

import fnmatch
import json
from importlib import resources
from pathlib import Path

import pytest

from beamer2slides import emit, google_auth, notes, paths, type3

ROOT = Path(__file__).resolve().parents[1]


def test_calibration_ships_with_the_package():
    # Reached through the package, so a wheel, a zip import and a build that stages the sources
    # somewhere else all find it; the checkout's own layout never comes into it.
    assert emit.CALIBRATION_DIR == resources.files("beamer2slides") / "calibration"
    assert type3.TABLE_PATH == emit.CALIBRATION_DIR / "tex_fonts.json"   # not beside a resolved __file__
    for path in (emit.CALIBRATION, emit.CALIBRATION_DIR / "fonts_serif.json", type3.TABLE_PATH):
        assert path.is_file() and json.loads(path.read_text(encoding="utf-8"))


def test_the_wheel_declares_the_calibration_and_the_command():
    tomllib = pytest.importorskip("tomllib")   # (3.11: the 3.10 job has no reader of its own)
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = meta["tool"]["setuptools"]["package-data"]["beamer2slides"]
    assert any(emit.CALIBRATION.name in pattern or pattern.endswith("*.json") for pattern in data)
    assert "tex/*.sty" in data and (resources.files("beamer2slides") / "tex" / notes.PACKAGE).is_file()
    assert meta["project"]["scripts"]["beamer2slides"] == "beamer2slides.__main__:main"


def copied_sources(dockerfile: str) -> list[str]:
    """What the Dockerfile's COPY lines take from the build context (continued lines joined; the
    last word of each is its destination, `--chown=...` a flag)."""
    out: list[str] = []
    for line in dockerfile.replace("\\\n", " ").splitlines():
        words = line.split()
        if words[:1] == ["COPY"]:
            out += [w for w in words[1:-1] if not w.startswith("--")]
    return out


def sent_back(ignore: str) -> list[str]:
    """The patterns an ignore file brings back after excluding everything, without their `!`, a
    leading `/` (git's anchor) or a trailing `/` (a folder)."""
    lines = [s.strip() for s in ignore.splitlines()]
    return [s[1:].strip("/") for s in lines if s.startswith("!")]


@pytest.mark.skipif(not (ROOT / "Dockerfile").exists(), reason="no Dockerfile in this copy")
def test_both_deploy_contexts_send_what_the_dockerfile_copies_and_no_credentials():
    # `gcloud run deploy --source .` reads .gcloudignore, `docker build` .dockerignore: when the
    # build began type-checking (MANIFEST.in, build_backend/, typecheck/) only .dockerignore was
    # told, and every Cloud Run deploy failed at its COPY until 2026-10-04
    sources = copied_sources((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert "MANIFEST.in" in sources and "src" in sources
    for name in (".dockerignore", ".gcloudignore"):
        text = (ROOT / name).read_text(encoding="utf-8")
        first = next(s.strip() for s in text.splitlines() if s.strip() and not s.lstrip().startswith("#"))
        assert first == "*", f"{name} must start by leaving everything out"
        back = sent_back(text)
        missing = [s for s in sources if not any(fnmatch.fnmatch(s, p) for p in back)]
        assert not missing, f"{name} leaves out what the Dockerfile copies: {missing}"
        secrets = [p for p in back for c in ("token.json", "client_secret.json") if fnmatch.fnmatch(c, p)]
        assert not secrets, f"{name} sends credentials back: {secrets}"


def test_out_root_is_the_checkout_here_and_the_current_folder_when_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert paths.in_checkout() and paths.out_root() == ROOT / "out"
    monkeypatch.setattr(paths, "in_checkout", lambda: False)
    monkeypatch.chdir(tmp_path)
    assert paths.out_root() == tmp_path / "out"


def test_credentials_come_from_the_override_then_a_home_then_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("B2S_TOKEN", str(tmp_path / "given.json"))
    assert google_auth.credential_file("B2S_TOKEN", "token.json", None) == tmp_path / "given.json"
    monkeypatch.delenv("B2S_TOKEN")
    monkeypatch.setattr(google_auth, "ROOT", tmp_path)
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    assert google_auth.credential_file("B2S_TOKEN", "token.json", None) == tmp_path / "token.json"
    (tmp_path / "token.json").unlink()
    assert google_auth.credential_file("B2S_TOKEN", "token.json", None) == google_auth.config_dir() / "token.json"
    assert google_auth.config_dir().name == "beamer2slides"
