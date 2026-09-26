"""A deck handed over as files (`deck_files`): what `deck-files` saves where Google and the web
may be reached, and an adopt that reads it where neither may - to the same source.

Offline: a fake Slides API, a fake google/fonts and a fake picture host; no TeX (the loop is
stubbed), no network.
"""

import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

from beamer2slides import adopt, deck_files, fontfetch
from beamer2slides.deck_files import DeckFiles, Recording, gather, replay

from .test_adopt import text_shape
from .test_adopt_media import VARIABLE_META, cat_deck, png_bytes, tiny_font


@pytest.fixture(autouse=True)
def clean_fonts(monkeypatch, tmp_path):
    monkeypatch.delenv("B2S_FONTS", raising=False)
    monkeypatch.delenv("B2S_FONT_SOURCE", raising=False)
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    adopt.forget_fonts()
    yield
    adopt.forget_fonts()


def refuse(asked: list):
    def fetch(url):
        asked.append(url)
        raise PermissionError(f"no network here: {url}")
    return fetch


# ---------------------------------------------------------------- recordings

def test_a_recording_answers_what_it_holds_and_404s_what_was_absent(tmp_path):
    rec = Recording(tmp_path / "rec")
    rec.put("https://a/pic.png", png_bytes(4, 3))
    rec.gone("https://a/gone.png")
    rec.save()
    back = Recording(tmp_path / "rec")
    below = []
    fetch = replay([back], refuse(below))
    assert fetch("https://a/pic.png") == png_bytes(4, 3) and back.answered == {"https://a/pic.png"}
    with pytest.raises(urllib.error.HTTPError) as err:
        fetch("https://a/gone.png")
    assert err.value.code == 404 and below == [], "absent is a 404, as it was, not a refusal"
    with pytest.raises(PermissionError):
        fetch("https://a/other.png")
    assert below == ["https://a/other.png"], "what it does not hold goes on to the fetcher underneath"


def test_font_files_read_from_a_local_copy_are_seen_too(tmp_path):
    """A producer with a google/fonts checkout reads files from disk: they are recorded all the same."""
    (tmp_path / "gf" / "ofl" / "tiny").mkdir(parents=True)
    (tmp_path / "gf" / "ofl" / "tiny" / "METADATA.pb").write_bytes(b"name: \"Tiny\"")
    seen = {}
    with fontfetch.use_source(tmp_path / "gf"), fontfetch.watching(lambda url, data: seen.setdefault(url, data)):
        fontfetch.get(f"{fontfetch.RAW}ofl/tiny/METADATA.pb")
        with pytest.raises(FileNotFoundError):
            fontfetch.get(f"{fontfetch.RAW}apache/tiny/METADATA.pb")
    assert seen == {f"{fontfetch.RAW}ofl/tiny/METADATA.pb": b"name: \"Tiny\"",
                    f"{fontfetch.RAW}apache/tiny/METADATA.pb": None}


# ---------------------------------------------------------------- the files, named

def test_the_parts_come_from_the_folder_or_each_on_its_own(tmp_path):
    folder = tmp_path / "files"
    (folder / "thumbnails").mkdir(parents=True)
    (folder / "pictures").mkdir()
    (folder / "presentation.json").write_text("{}", encoding="utf-8")
    files = gather(folder)
    assert files.presentation == folder / "presentation.json" and files.thumbnails == [folder / "thumbnails"]
    assert files.pictures == folder / "pictures" and files.google_fonts is None
    assert files.given() == {"presentation": True, "thumbnails": True, "pictures": True,
                             "google_fonts": False, "pptx": False, "fonts": False}
    (tmp_path / "gf").mkdir()
    assert gather(folder, google_fonts=tmp_path / "gf").google_fonts == tmp_path / "gf"
    assert gather("1AbCdEf") is None, "a live deck"
    (tmp_path / "deck.json").write_text("{}", encoding="utf-8")
    assert gather(tmp_path / "deck.json", thumbnails=[tmp_path / "t"]).thumbnails == [tmp_path / "t"]
    with pytest.raises(SystemExit, match="deck read from files"):
        gather("1AbCdEf", pictures=tmp_path / "gf")
    with pytest.raises(SystemExit, match="not a folder"):
        gather(folder, pictures=tmp_path / "nothing")


def test_a_zip_is_unpacked_and_never_outside_its_folder(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "presentation.json").write_text("{}", encoding="utf-8")
    deck_files.zip_folder(folder, tmp_path / "files.zip")
    files = gather(tmp_path / "files.zip", tmp_path / "unpacked")
    assert files.presentation == tmp_path / "unpacked" / "presentation.json"
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../outside.txt", "x")
        z.writestr("presentation.json", "{}")
    with pytest.raises(ValueError, match="outside"):
        gather(evil, tmp_path / "evil")
    assert not (tmp_path / "outside.txt").exists()


def test_thumbnails_are_a_slides_by_number_or_id_and_of_its_shape(tmp_path):
    from beamer2slides.deck_ir import given_thumbnails
    pres = {"pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
            "slides": [{"objectId": "a"}, {"objectId": "b"}, {"objectId": "c"}]}
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "001.png").write_bytes(png_bytes(160, 90))
    (tmp_path / "t" / "c.png").write_bytes(png_bytes(160, 90))
    (tmp_path / "t" / "002.png").write_bytes(png_bytes(100, 100))       # another deck's page shape
    said = []
    get, n = given_thumbnails(pres, [tmp_path / "t"], said.append)
    assert (get(0).name, get(1), get(2).name, n) == ("001.png", None, "c.png", 1 + 1)
    assert any("not the deck's page shape" in s for s in said)
    loose = tmp_path / "loose"
    loose.mkdir()
    for name in ("x.png", "y.png", "z.png"):
        (loose / name).write_bytes(png_bytes(160, 90))
    get, n = given_thumbnails(pres, [loose], said.append)
    assert [get(i).name for i in range(3)] == ["x.png", "y.png", "z.png"], "one per slide, in order"


# ---------------------------------------------------------------- saved, then adopted with nothing

def fake_google(monkeypatch, pres):
    from beamer2slides import google_auth

    from .test_guard import Request
    slides = type("S", (), {"presentations": lambda self: self,
                            "get": lambda self, presentationId: Request(pres)})()
    monkeypatch.setattr(google_auth, "slides_service", lambda *a, **k: slides)
    monkeypatch.setattr(google_auth, "drive_service", lambda *a, **k: pytest.fail("Drive"))

    def thumbnails(pid, pres, folder):
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, _ in enumerate(pres["slides"]):
            (folder / f"{i + 1:03d}.png").write_bytes(png_bytes(160, 90, colour=(30, 90, 200)))
            paths.append(folder / f"{i + 1:03d}.png")
        return lambda n: paths[n] if 0 <= n < len(paths) else None
    monkeypatch.setattr("beamer2slides.deck_ir.slide_thumbnails", thumbnails)


def web():
    """The picture host and google/fonts as the producer reaches them."""
    pres, cat, _ = cat_deck()
    pres["slides"][0]["pageElements"].append(
        text_shape("t", "Words in a fetched face", 10, 10, 300, 40, font="Tiny Flex"))
    answers = {"https://example.invalid/cat.png": cat, "https://example.invalid/backdrop.png": png_bytes(32, 18),
               f"{fontfetch.RAW}ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
               f"{fontfetch.RAW}ofl/tinyflex/OFL.txt": b"licence",
               f"{fontfetch.RAW}ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)}
    asked = []

    def fetch(url):
        asked.append(url)
        if url not in answers:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return answers[url]
    return pres, fetch, asked


def adopted(monkeypatch, tmp_path, name, deck, **kw):
    seen = {}
    monkeypatch.setattr("beamer2slides.inverse.run_pull", lambda target, *a, **k: seen.setdefault("target", target))
    monkeypatch.setattr(adopt, "record_base", lambda target, pres, *a, **k: seen.setdefault("pres", pres))
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / name / "font-cache"))
    found: dict = {}
    log = []
    tex = tmp_path / name / "tree" / "main.tex"
    with adopt.no_machine_fonts():
        adopt.cmd_adopt(deck, tex, tmp_path / name / "work", False, None, 1, None, False,
                        log=log.append, found=found, **kw)
    figures = {p.name: p.read_bytes() for p in (tex.parent / "figures").iterdir()} if (tex.parent / "figures").is_dir() else {}
    fonts = {p.name: p.read_bytes() for p in (tex.parent / "fonts").iterdir()} if (tex.parent / "fonts").is_dir() else {}
    return tex.read_text(encoding="utf-8"), figures, fonts, found, seen, log


def test_an_adopt_from_the_saved_files_is_the_live_adopt(tmp_path, monkeypatch, fetcher):
    """Everything a live adopt reads - Google's answer, its thumbnails, the pictures and the deck's
    typeface from google/fonts - saved by `deck-files`, then adopted where nothing may be fetched:
    the same source, pictures and fonts, and not one download asked for."""
    pytest.importorskip("fontTools")
    import itertools
    clock = itertools.count(3_000_000_000, 1000)    # every font saved in another second: a cut must not say when
    monkeypatch.setattr("fontTools.ttLib.tables._h_e_a_d.timestampNow", lambda: next(clock))
    pres, fetch, _ = web()
    fake_google(monkeypatch, pres)
    fetcher(fetch)
    live = adopted(monkeypatch, tmp_path, "live", "P")
    manifest = deck_files.save("P", tmp_path / "files", log=lambda *a: None)
    assert manifest["counts"] == {"slides": 1, "thumbnails": 1, "pictures": 2, "google_fonts": 3}
    assert set(manifest["parts"]) == {"presentation", "thumbnails", "pictures", "google_fonts"}
    deck_files.zip_folder(tmp_path / "files", tmp_path / "files.zip")

    asked = []
    fetcher(refuse(asked))
    monkeypatch.setattr("beamer2slides.google_auth.slides_service", lambda *a, **k: pytest.fail("Slides"))
    for name, deck in (("folder", tmp_path / "files"), ("zip", tmp_path / "files.zip")):
        text, figures, fonts, found, seen, log = adopted(monkeypatch, tmp_path, name, str(deck))
        assert text == live[0], name
        assert figures == live[1] and fonts == live[2] and fonts, name
        assert seen["pres"]["presentationId"] == "P", "the sync base pairs with the deck's own ids"
        assert asked == [], f"{name}: nothing fetched ({asked[:3]})"
        report = found["offline"]
        assert report["thumbnails"] == {"given": True, "adds": deck_files.PARTS["thumbnails"], "count": 1}
        assert report["pictures"]["count"] == 2 and report["google_fonts"]["count"] >= 3
        assert not report["pptx"]["given"] and "deck read from files:" in log
    assert seen["target"]["slides"][0].get("thumbnail"), "frames are scored against Google's render"


def test_a_part_left_out_is_said_with_what_it_costs(tmp_path, monkeypatch, fetcher):
    pres, cat, data = cat_deck()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    (tmp_path / "deck.pptx").write_bytes(data)
    fetcher(refuse([]))
    text, figures, _, found, seen, log = adopted(monkeypatch, tmp_path, "run", str(tmp_path / "files"),
                                                 pptx=tmp_path / "deck.pptx")
    assert cat in figures.values(), "the picture out of the .pptx"
    report = found["offline"]
    assert report["pptx"]["given"] and report["pptx"]["count"] == 1
    assert not report["thumbnails"]["given"] and "gradients" in report["thumbnails"]["without"]
    assert any(line.startswith("  pictures: not given - ") for line in log)


def test_recorded_pictures_come_before_the_pptx(tmp_path, monkeypatch, fetcher):
    """Google's own bytes, where a .pptx may hold a re-encoded copy."""
    pres, cat, data = cat_deck()
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    rec = Recording(folder / "pictures")
    served = png_bytes(20, 15, colour=(10, 200, 10))
    rec.put("https://example.invalid/cat.png", served)
    rec.save()
    (folder / "deck.pptx").write_bytes(data)
    fetcher(refuse([]))
    _, figures, _, found, _, _ = adopted(monkeypatch, tmp_path, "run", str(folder))
    assert served in figures.values() and cat not in figures.values()
    assert found["offline"]["pictures"]["count"] == 1


def test_the_cli_labels_every_part_with_what_it_adds():
    import subprocess

    from beamer2slides import interpreter
    out = subprocess.run([interpreter.python(), "-m", "beamer2slides", "adopt", "--help"], env=interpreter.env(),
                         capture_output=True, text=True, check=True).stdout
    flat = " ".join(out.split())
    for part in ("thumbnails", "pictures", "google_fonts", "pptx", "fonts"):
        assert " ".join(deck_files.PARTS[part].split())[:60] in flat, part
    assert "--google-fonts" in out and "--thumbnails" in out


def test_a_deck_is_not_saved_over_other_files(tmp_path):
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        deck_files.save("P", tmp_path / "busy")


def test_the_agent_adopts_the_files_as_one_zip_with_no_google(tmp_path, monkeypatch, fetcher):
    import base64

    from beamer2slides.agent import AgentContext
    from beamer2slides.agent.source_tools import deck_adopt
    pres, cat, _ = cat_deck()
    folder = tmp_path / "made"
    folder.mkdir()
    (folder / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    rec = Recording(folder / "pictures")
    rec.put("https://example.invalid/cat.png", cat)
    rec.save()
    buf = io.BytesIO()
    deck_files.zip_folder(folder, tmp_path / "made.zip")
    buf.write((tmp_path / "made.zip").read_bytes())
    asked = []
    fetcher(refuse(asked))
    seen = {}
    monkeypatch.setattr("beamer2slides.inverse.run_pull", lambda target, *a, **k: seen.setdefault("target", target))
    monkeypatch.setattr(adopt, "record_base", lambda *a, **k: None)
    monkeypatch.setattr("beamer2slides.agent.source_tools._finish", lambda *a, **k: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    res = deck_adopt(AgentContext.offline(ws), tex="main.tex",
                     deck={"base64": base64.b64encode(buf.getvalue()).decode()})
    assert res.ok, res.json()
    assert res.data["local_target"] and res.data["offline"]["pictures"]["count"] == 1
    [el] = [e for e in seen["target"]["slides"][0]["elements"] if not e.get("inherited")]
    assert Path(el["file"]).read_bytes() == cat
    assert "https://example.invalid/cat.png" not in asked
