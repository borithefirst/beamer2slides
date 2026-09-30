"""A deck handed over as files (`deck_files`): what `deck-files` saves where Google and the web
may be reached, and an adopt that reads it where neither may - to the same source.

Offline: a fake Slides API, a fake google/fonts and a fake picture host; no TeX (the loop is
stubbed), no network.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from beamer2slides import adopt, deck_files, fontfetch
from beamer2slides.deck_files import DeckFiles, Recording, gather, replay
from beamer2slides.deck_ir import Thumbnails
from beamer2slides.google_types import Presentation, Presentations, Request, json_object, presentation
from beamer2slides.inverse import Later
from beamer2slides.json_types import JsonObject
from beamer2slides.net import Fetch
from beamer2slides.typing_compat import override

from .fake_google import Answer, Fetcher, NoPresentations, NoSlides
from .json_reads import jarr, jat, jnum, jobj, jobjs, jstr
from .test_adopt import text_shape
from .test_adopt_media import VARIABLE_META, cat_deck, png_bytes, tiny_font

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import GetPresentation


@pytest.fixture(autouse=True)
def clean_fonts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv("B2S_FONTS", raising=False)
    monkeypatch.delenv("B2S_FONT_SOURCE", raising=False)
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    adopt.forget_fonts()
    yield
    adopt.forget_fonts()


def refuse(asked: list[str]) -> Fetch:
    def fetch(url: str) -> bytes:
        asked.append(url)
        raise PermissionError(f"no network here: {url}")
    return fetch


def quiet(line: str) -> None:
    """A log nobody reads."""


def found_files(files: DeckFiles | None) -> DeckFiles:
    """What `gather` found, which a test gave it."""
    assert files is not None
    return files


def name_of(picture: object) -> str:
    """The file name of a thumbnail `given_thumbnails` answers with (a path)."""
    assert isinstance(picture, Path)
    return picture.name


# ---------------------------------------------------------------- recordings

def test_a_recording_answers_what_it_holds_and_404s_what_was_absent(tmp_path: Path):
    rec = Recording(tmp_path / "rec")
    rec.put("https://a/pic.png", png_bytes(4, 3))
    rec.gone("https://a/gone.png")
    rec.save()
    back = Recording(tmp_path / "rec")
    below: list[str] = []
    fetch = replay([back], refuse(below))
    assert fetch("https://a/pic.png") == png_bytes(4, 3) and back.answered == {"https://a/pic.png"}
    with pytest.raises(urllib.error.HTTPError) as err:
        fetch("https://a/gone.png")
    assert err.value.code == 404 and below == [], "absent is a 404, as it was, not a refusal"
    with pytest.raises(PermissionError):
        fetch("https://a/other.png")
    assert below == ["https://a/other.png"], "what it does not hold goes on to the fetcher underneath"


def test_a_page_fetched_in_place_of_a_picture_is_not_named_like_one(tmp_path: Path):
    """octopus.energy's dead asset link, fosdem-green-web: a URL ending `.jpg` that actually answers
    with a Next.js error page (or a Google sign-in page for a Drive link) is recorded as `.html`, not
    `.jpg` - naming it a picture would let it be mistaken for one later."""
    rec = Recording(tmp_path / "rec")
    rec.put("https://octopus.energy/static/banner.jpg", b"<!DOCTYPE html><html><title>Not found</title></html>")
    rec.put("https://accounts.google.com/signin", b"<!doctype html><html><body>sign in</body></html>")
    assert all(name.endswith(".html") for name in rec.files.values())
    # a real font file, whose bytes `image_format` also can't read, still falls back to the URL
    rec.put("https://fonts.gstatic.com/s/tiny/v1/tiny.woff2", tiny_font("Tiny"))
    assert rec.files["https://fonts.gstatic.com/s/tiny/v1/tiny.woff2"].endswith(".woff2")


def test_font_files_read_from_a_local_copy_are_seen_too(tmp_path: Path):
    """A producer with a google/fonts checkout reads files from disk: they are recorded all the same."""
    (tmp_path / "gf" / "ofl" / "tiny").mkdir(parents=True)
    (tmp_path / "gf" / "ofl" / "tiny" / "METADATA.pb").write_bytes(b"name: \"Tiny\"")
    seen: dict[str, bytes | None] = {}

    def see(url: str, data: bytes | None) -> None:
        seen.setdefault(url, data)
    with fontfetch.use_source(tmp_path / "gf"), fontfetch.watching(see):
        fontfetch.get(f"{fontfetch.RAW}ofl/tiny/METADATA.pb")
        with pytest.raises(FileNotFoundError):
            fontfetch.get(f"{fontfetch.RAW}apache/tiny/METADATA.pb")
    assert seen == {f"{fontfetch.RAW}ofl/tiny/METADATA.pb": b"name: \"Tiny\"",
                    f"{fontfetch.RAW}apache/tiny/METADATA.pb": None}


# ---------------------------------------------------------------- the files, named

def test_the_parts_come_from_the_folder_or_each_on_its_own(tmp_path: Path):
    folder = tmp_path / "files"
    (folder / "thumbnails").mkdir(parents=True)
    (folder / "pictures").mkdir()
    (folder / "presentation.json").write_text("{}", encoding="utf-8")
    files = found_files(gather(folder, None, (), None, None))
    assert files.presentation == folder / "presentation.json" and files.thumbnails == [folder / "thumbnails"]
    assert files.pictures == folder / "pictures" and files.google_fonts is None
    assert files.given() == {"presentation": True, "thumbnails": True, "pictures": True,
                             "google_fonts": False, "pptx": False, "fonts": False}
    (tmp_path / "gf").mkdir()
    assert found_files(gather(folder, None, (), None, tmp_path / "gf")).google_fonts == tmp_path / "gf"
    assert gather("1AbCdEf", None, (), None, None) is None, "a live deck"
    (tmp_path / "deck.json").write_text("{}", encoding="utf-8")
    assert found_files(gather(tmp_path / "deck.json", None, [tmp_path / "t"], None, None)).thumbnails == [
        tmp_path / "t"]
    with pytest.raises(SystemExit, match="deck read from files"):
        gather("1AbCdEf", None, (), tmp_path / "gf", None)
    with pytest.raises(SystemExit, match="not a folder"):
        gather(folder, None, (), tmp_path / "nothing", None)


def test_a_zip_is_unpacked_and_never_outside_its_folder(tmp_path: Path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "presentation.json").write_text("{}", encoding="utf-8")
    deck_files.zip_folder(folder, tmp_path / "files.zip")
    files = found_files(gather(tmp_path / "files.zip", tmp_path / "unpacked", (), None, None))
    assert files.presentation == tmp_path / "unpacked" / "presentation.json"
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../outside.txt", "x")
        z.writestr("presentation.json", "{}")
    with pytest.raises(ValueError, match="outside"):
        gather(evil, tmp_path / "evil", (), None, None)
    assert not (tmp_path / "outside.txt").exists()


def test_thumbnails_are_a_slides_by_number_or_id_and_of_its_shape(tmp_path: Path):
    from beamer2slides.deck_ir import given_thumbnails
    pres: JsonObject = {"pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
            "slides": [{"objectId": "a"}, {"objectId": "b"}, {"objectId": "c"}]}
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "001.png").write_bytes(png_bytes(160, 90))
    (tmp_path / "t" / "c.png").write_bytes(png_bytes(160, 90))
    (tmp_path / "t" / "002.png").write_bytes(png_bytes(100, 100))       # another deck's page shape
    said: list[str] = []
    get, n = given_thumbnails(pres, [tmp_path / "t"], said.append)
    assert (name_of(get(0)), get(1), name_of(get(2)), n) == ("001.png", None, "c.png", 1 + 1)
    assert any("not the deck's page shape" in s for s in said)
    loose = tmp_path / "loose"
    loose.mkdir()
    for name in ("x.png", "y.png", "z.png"):
        (loose / name).write_bytes(png_bytes(160, 90))
    get, n = given_thumbnails(pres, [loose], said.append)
    assert [name_of(get(i)) for i in range(3)] == ["x.png", "y.png", "z.png"], "one per slide, in order"


# ---------------------------------------------------------------- saved, then adopted with nothing

class OneDeck(NoPresentations):
    """`presentations()` answering `get` with the one deck, whatever id is asked."""

    def __init__(self, pres: Presentation) -> None:
        self.pres = pres

    @override
    def get(self, **kw: Unpack[GetPresentation]) -> Request[Presentation]:
        return Answer(self.pres)


class OneDeckSlides(NoSlides):
    """A Slides client holding the one deck."""

    def __init__(self, pres: Presentation) -> None:
        self.decks = OneDeck(pres)

    @override
    def presentations(self) -> Presentations:
        return self.decks


def no_slides(*a: object, **k: object) -> NoReturn:
    pytest.fail("Slides")


def no_drive(*a: object, **k: object) -> NoReturn:
    pytest.fail("Drive")


def fake_google(monkeypatch: pytest.MonkeyPatch, pres: JsonObject) -> None:
    from beamer2slides import google_auth
    slides = OneDeckSlides(presentation(pres, "the deck the fake Slides holds"))

    def slides_service(*a: object, **k: object) -> OneDeckSlides:
        return slides
    monkeypatch.setattr(google_auth, "slides_service", slides_service)
    monkeypatch.setattr(google_auth, "drive_service", no_drive)

    def thumbnails(pid: str, pres: Presentation, folder: Path) -> Thumbnails:
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for i, _ in enumerate(pres.get("slides", [])):
            (folder / f"{i + 1:03d}.png").write_bytes(png_bytes(160, 90, colour=(30, 90, 200)))
            paths.append(folder / f"{i + 1:03d}.png")

        def shot(n: int) -> Path | None:
            return paths[n] if 0 <= n < len(paths) else None
        return shot
    monkeypatch.setattr("beamer2slides.deck_ir.slide_thumbnails", thumbnails)


def the_cat_deck() -> tuple[JsonObject, bytes, bytes]:
    """`cat_deck`'s presentation, its photo and its .pptx, the presentation read as JSON."""
    pres, cat, data = cat_deck()
    return jobj(pres), cat, data


def web() -> tuple[JsonObject, Fetch, list[str]]:
    """The picture host and google/fonts as the producer reaches them."""
    pres, cat, _ = the_cat_deck()
    jarr(pres, "slides", 0, "pageElements").append(
        text_shape("t", "Words in a fetched face", 10, 10, 300, 40, font="Tiny Flex"))
    answers = {"https://example.invalid/cat.png": cat, "https://example.invalid/backdrop.png": png_bytes(32, 18),
               f"{fontfetch.RAW}ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
               f"{fontfetch.RAW}ofl/tinyflex/OFL.txt": b"licence",
               f"{fontfetch.RAW}ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)}
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        if url not in answers:
            raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
        return answers[url]
    return pres, fetch, asked


@dataclass(frozen=True, kw_only=True)
class Adoption:
    """What one `cmd_adopt` wrote and said: the source, the tree's figures and fonts by name, what it
    found, what it handed the loop (`target`) and the sync base (`pres`), and its log."""
    text: str
    figures: dict[str, bytes]
    fonts: dict[str, bytes]
    found: dict[str, object]
    seen: dict[str, JsonObject]
    log: list[str]

    def offline(self) -> JsonObject:
        """`found["offline"]`: each part of the deck's files as the report says it."""
        return json_object(self.found["offline"], "found['offline']")


def files_in(folder: Path) -> dict[str, bytes]:
    """The files of `folder` by name (none when there is no such folder)."""
    if not folder.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def adopted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, deck: str, pptx: Path | None) -> Adoption:
    seen: dict[str, JsonObject] = {}

    def run_pull(target: JsonObject | Later, tex: Path, work: Path, apply: bool, out: Path | None, max_iter: int,
                 handout: bool, engine: str | None, log: Callable[[str], object]) -> None:
        assert not isinstance(target, Later)
        seen.setdefault("target", target)

    def record_base(target: JsonObject, pres: JsonObject | None, tex: Path, work: Path, engine: str | None,
                    base_in_drive: bool, log: Callable[[str], None]) -> None:
        assert pres is not None
        seen.setdefault("pres", pres)
    monkeypatch.setattr("beamer2slides.inverse.run_pull", run_pull)
    monkeypatch.setattr(adopt, "record_base", record_base)
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / name / "font-cache"))
    found: dict[str, object] = {}
    log: list[str] = []
    tex = tmp_path / name / "tree" / "main.tex"
    with adopt.no_machine_fonts():
        adopt.cmd_adopt(deck, tex, tmp_path / name / "work", False, None, 1, None, False, None, base=True,
                        base_in_drive=False, log=log.append, fonts=None, found=found, pptx=pptx, files=None)
    return Adoption(text=tex.read_text(encoding="utf-8"), figures=files_in(tex.parent / "figures"),
                    fonts=files_in(tex.parent / "fonts"), found=found, seen=seen, log=log)


def test_an_adopt_from_the_saved_files_is_the_live_adopt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         fetcher: Fetcher):
    """Everything a live adopt reads - Google's answer, its thumbnails, the pictures and the deck's
    typeface from google/fonts - saved by `deck-files`, then adopted where nothing may be fetched:
    the same source, pictures and fonts, and not one download asked for."""
    pytest.importorskip("fontTools")
    import itertools
    clock = itertools.count(3_000_000_000, 1000)    # every font saved in another second: a cut must not say when

    def stamp() -> int:
        return next(clock)
    monkeypatch.setattr("fontTools.ttLib.tables._h_e_a_d.timestampNow", stamp)
    pres, fetch, _ = web()
    fake_google(monkeypatch, pres)
    fetcher(fetch)
    live = adopted(monkeypatch, tmp_path, "live", "P", None)
    manifest = deck_files.save("P", tmp_path / "files", None, quiet)
    assert manifest["counts"] == {"slides": 1, "thumbnails": 1, "pictures": 2, "google_fonts": 3}
    assert set(manifest["parts"]) == {"presentation", "thumbnails", "pictures", "google_fonts"}
    deck_files.zip_folder(tmp_path / "files", tmp_path / "files.zip")

    asked: list[str] = []
    fetcher(refuse(asked))
    monkeypatch.setattr("beamer2slides.google_auth.slides_service", no_slides)
    seen: dict[str, JsonObject] = {}
    for name, deck in (("folder", tmp_path / "files"), ("zip", tmp_path / "files.zip")):
        run = adopted(monkeypatch, tmp_path, name, str(deck), None)
        seen = run.seen
        assert run.text == live.text, name
        assert run.figures == live.figures and run.fonts == live.fonts and run.fonts, name
        assert jat(seen["pres"], "presentationId") == "P", "the sync base pairs with the deck's own ids"
        assert asked == [], f"{name}: nothing fetched ({asked[:3]})"
        report = run.offline()
        assert report["thumbnails"] == {"given": True, "adds": deck_files.PARTS["thumbnails"], "count": 1}
        assert jat(report, "pictures", "count") == 2 and jnum(report, "google_fonts", "count") >= 3
        assert not jat(report, "pptx", "given") and "deck read from files:" in run.log
    assert jobj(seen["target"], "slides", 0).get("thumbnail"), "frames are scored against Google's render"


def test_a_part_left_out_is_said_with_what_it_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    fetcher: Fetcher):
    pres, cat, data = the_cat_deck()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    (tmp_path / "deck.pptx").write_bytes(data)
    fetcher(refuse([]))
    run = adopted(monkeypatch, tmp_path, "run", str(tmp_path / "files"), tmp_path / "deck.pptx")
    assert cat in run.figures.values(), "the picture out of the .pptx"
    report = run.offline()
    assert jat(report, "pptx", "given") and jat(report, "pptx", "count") == 1
    assert not jat(report, "thumbnails", "given") and "gradients" in jstr(report, "thumbnails", "without")
    assert any(line.startswith("  pictures: not given - ") for line in run.log)


def test_recorded_pictures_come_before_the_pptx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher):
    """Google's own bytes, where a .pptx may hold a re-encoded copy."""
    pres, cat, data = the_cat_deck()
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    rec = Recording(folder / "pictures")
    served = png_bytes(40, 30, colour=(10, 200, 10))
    rec.put("https://example.invalid/cat.png", served)
    rec.save()
    (folder / "deck.pptx").write_bytes(data)
    fetcher(refuse([]))
    run = adopted(monkeypatch, tmp_path, "run", str(folder), None)
    assert served in run.figures.values() and cat not in run.figures.values()
    assert jat(run.offline(), "pictures", "count") == 1


def test_the_cli_labels_every_part_with_what_it_adds():
    import subprocess

    from beamer2slides import interpreter
    out = subprocess.run([interpreter.python(), "-m", "beamer2slides", "adopt", "--help"], env=interpreter.env(),
                         capture_output=True, text=True, check=True).stdout
    flat = " ".join(out.split())
    for part in ("thumbnails", "pictures", "google_fonts", "pptx", "fonts"):
        assert " ".join(deck_files.PARTS[part].split())[:60] in flat, part
    assert "--google-fonts" in out and "--thumbnails" in out


def test_a_deck_is_not_saved_over_other_files(tmp_path: Path):
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        deck_files.save("P", tmp_path / "busy", None, print)


def test_the_agent_adopts_the_files_as_one_zip_with_no_google(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              fetcher: Fetcher):
    import base64

    from beamer2slides.agent import AgentContext
    from beamer2slides.agent.source_tools import deck_adopt
    pres, cat, _ = the_cat_deck()
    folder = tmp_path / "made"
    folder.mkdir()
    (folder / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    rec = Recording(folder / "pictures")
    rec.put("https://example.invalid/cat.png", cat)
    rec.save()
    buf = io.BytesIO()
    deck_files.zip_folder(folder, tmp_path / "made.zip")
    buf.write((tmp_path / "made.zip").read_bytes())
    asked: list[str] = []
    fetcher(refuse(asked))
    seen: dict[str, JsonObject] = {}

    def run_pull(target: JsonObject | Later, tex: Path, work: Path, apply: bool, out: Path | None, max_iter: int,
                 handout: bool, engine: str | None, log: Callable[[str], object]) -> None:
        assert not isinstance(target, Later)
        seen.setdefault("target", target)

    def nothing(*a: object, **k: object) -> None:
        return None
    monkeypatch.setattr("beamer2slides.inverse.run_pull", run_pull)
    monkeypatch.setattr(adopt, "record_base", nothing)
    monkeypatch.setattr("beamer2slides.agent.source_tools._finish", nothing)
    ws = tmp_path / "ws"
    ws.mkdir()
    res = deck_adopt(AgentContext.offline(ws), tex="main.tex",
                     deck={"base64": base64.b64encode(buf.getvalue()).decode()})
    assert res.ok, res.json()
    assert res.data["local_target"] and jat(res.data, "offline", "pictures", "count") == 1
    [el] = [e for e in jobjs(seen["target"], "slides", 0, "elements") if not e.get("inherited")]
    assert Path(jstr(el, "file")).read_bytes() == cat
    assert "https://example.invalid/cat.png" not in asked
