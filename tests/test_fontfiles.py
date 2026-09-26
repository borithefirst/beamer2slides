"""Fonts adopt can have without the internet: files a person hands over (`fontfiles`), a local copy
of google/fonts (`fontfetch.use_source`), downloads through the caller's fetcher, and the report of
the fonts that were set in something else (`font_preamble`'s `missing_fonts`, `deck_adopt`'s
`data["fonts_missing"]`).

Offline: fonts are built with fontTools' FontBuilder (`test_adopt_media.tiny_font`), urllib fails
the test if it is ever reached, and no TeX runs.
"""

import base64
import io
import json
from types import SimpleNamespace

import pytest

from beamer2slides import adopt, fontfetch, fontfiles, net
from beamer2slides.agent import ALL_ACTIONS, AgentContext, LocalWorkspace
from beamer2slides.deck_ir import deck_ir

from .test_adopt import text_shape
from .test_adopt_media import STATIC_META, VARIABLE_META, deck_with, tiny_font

pytest.importorskip("fontTools")


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    """No machine font, a cache of the test's own, and no socket."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    monkeypatch.delenv("B2S_FONT_SOURCE", raising=False)
    monkeypatch.setattr(net, "urllib_fetch", lambda url: pytest.fail(f"urllib fetched {url}"))
    adopt.forget_fonts()
    yield
    adopt.forget_fonts()


def web(data: bytes, flavor: str) -> bytes:
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(data))
    font.flavor = flavor
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def italic(data: bytes) -> bytes:
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(data))
    font["OS/2"].fsSelection |= 1
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def given(folder, **files: bytes):
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name.replace("_", ".")).write_bytes(data)
    return folder


# ---------------------------------------------------------------- files a person hands over

def test_web_fonts_are_unwrapped_and_named_by_what_they_say(tmp_path):
    """A web font's file name is often a hash; lualatex reads neither wrapper."""
    src = given(tmp_path / "given", a81f_woff=web(tiny_font("Tiny Sans", 400), "woff"),
                c02d_ttf=tiny_font("Tiny Sans", 700), e9_otf_part=b"not a font at all")
    report = fontfiles.install([src], tmp_path / "laid")
    assert report["families"]["Tiny Sans"]["styles"] == ["Bold", "Regular"]
    regular = tmp_path / "laid" / "tinysans" / "TinySans-Regular.ttf"
    assert regular.read_bytes()[:4] == b"\x00\x01\x00\x00", "an sfnt, not a woff"
    assert report["skipped"] == [], "only font suffixes are read out of a folder"


def test_a_woff2_needs_brotli_or_says_so(tmp_path):
    try:
        import brotli  # noqa: F401
    except ImportError:
        brotli = None
    if brotli is None:
        pytest.skip("no brotli: writing a .woff2 needs it too")
    f = tmp_path / "x.woff2"
    f.write_bytes(web(tiny_font("Tiny Sans", 400), "woff2"))
    assert fontfiles.install([f], tmp_path / "laid")["families"]["Tiny Sans"]["styles"] == ["Regular"]


def test_a_file_that_is_no_font_and_a_family_given_twice_are_said_not_used(tmp_path):
    junk = tmp_path / "notes.ttf"
    junk.write_bytes(b"%PDF-1.7 not a font")
    one, two = given(tmp_path / "a", r_ttf=tiny_font("Tiny Sans")), given(tmp_path / "b", r_ttf=tiny_font("Tiny Sans"))
    report = fontfiles.install([junk, one / "r.ttf", two / "r.ttf"], tmp_path / "laid")
    reasons = {s["file"]: s["reason"] for s in report["skipped"]}
    assert reasons["notes.ttf"] == fontfiles.NOT_A_FONT
    assert "given twice" in reasons["r.ttf"]
    assert report["families"]["Tiny Sans"]["files"] == ["r.ttf"]


def test_a_variable_font_is_cut_into_styles_and_weights_between(tmp_path):
    from fontTools.ttLib import TTFont
    f = tmp_path / "3fa9.woff"
    f.write_bytes(web(tiny_font("Tiny Flex", variable=True), "woff"))
    report = fontfiles.install([f], tmp_path / "laid")
    got = report["families"]["Tiny Flex"]
    assert got["styles"] == ["Bold", "Regular"] and got["variable"]
    regular = tmp_path / "laid" / "tinyflex" / "TinyFlex-Regular.ttf"
    assert "fvar" not in TTFont(regular)
    w600 = fontfetch.weight_file(regular, 600)
    assert w600 == regular.with_name("TinyFlex-W600.ttf") and TTFont(w600)["OS/2"].usWeightClass == 600


def test_static_weights_become_the_styles_and_the_faces_between(tmp_path):
    src = given(tmp_path / "given", r_ttf=tiny_font("Tiny Sans", 400), m_ttf=tiny_font("Tiny Sans", 500),
                b_ttf=tiny_font("Tiny Sans", 700), i_ttf=italic(tiny_font("Tiny Sans", 400)))
    report = fontfiles.install([src], tmp_path / "laid")
    assert report["families"]["Tiny Sans"]["styles"] == ["Bold", "Italic", "Regular"]
    assert report["families"]["Tiny Sans"]["weights"] == ["500"]
    regular = tmp_path / "laid" / "tinysans" / "TinySans-Regular.ttf"
    assert fontfetch.weight_file(regular, 500) == regular.with_name("TinySans-W500.ttf")
    assert fontfetch.weight_file(regular, 300) is None, "nothing to cut a static family's 300 from"


def test_a_family_with_no_upright_is_no_family(tmp_path):
    f = given(tmp_path / "given", i_ttf=italic(tiny_font("Slanted Only")))
    report = fontfiles.install([f], tmp_path / "laid")
    assert "Slanted Only" not in report["families"]
    assert "no upright" in report["skipped"][0]["reason"]


def test_the_folder_is_emptied_only_when_it_is_ours(tmp_path):
    root = tmp_path / "laid"
    fontfiles.install([given(tmp_path / "a", r_ttf=tiny_font("Tiny Sans"))], root)
    fontfiles.install([given(tmp_path / "b", r_ttf=tiny_font("Other Sans"))], root)
    assert not (root / "tinysans").exists(), "an earlier run's fonts are not this one's"
    assert (root / "othersans" / "OtherSans-Regular.ttf").exists()
    theirs = tmp_path / "theirs"
    given(theirs, keep_txt=b"a person's file")
    with pytest.raises(fontfiles.ForeignFolder):
        fontfiles.install([tmp_path / "a" / "r.ttf"], theirs)
    assert (theirs / "keep.txt").read_bytes() == b"a person's file"


def test_supplied_fonts_are_found_first_and_set_the_deck(tmp_path):
    root = tmp_path / "laid"
    fontfiles.install([given(tmp_path / "given", r_ttf=tiny_font("Tiny Sans"), b_ttf=tiny_font("Tiny Sans", 700))], root)
    assert adopt.font_family("Tiny Sans", "sans") == {}, "not before it is supplied"
    ir = deck_ir(deck_with(text_shape("t", "Words in a supplied face", 10, 10, 300, 40, font="Tiny Sans")),
                 foreign=True)
    missing: list = []
    with adopt.use_fonts(root):
        assert adopt.font_family("Tiny Sans", "sans")["UprightFont"].parent == root / "tinysans"
        text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", missing=missing)
    assert "\\setsansfont{TinySans}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular,BoldFont=*-Bold," in text
    assert (tmp_path / "tree" / "fonts" / "TinySans-Regular.ttf").exists()
    assert missing == []
    assert adopt.font_family("Tiny Sans", "sans") == {}, "only for the length of the block"


# ---------------------------------------------------------------- what adopt says it lacked

def test_a_font_that_is_nowhere_is_named_with_what_it_was_set_in(tmp_path):
    ir = deck_ir(deck_with(text_shape("t", "Words in a face nobody has", 10, 10, 300, 40, font="Nowhere Sans")),
                 foreign=True)
    missing: list = []
    adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", missing=missing)
    # (by the name the IR carries it under: the family's name without its spaces)
    assert [(m["font"], m["kind"], m["set_in"]) for m in missing] == [("NowhereSans", "sans", "texgyreheros")]
    assert missing[0]["letters"] >= len("Words in a face nobody has")
    lines = adopt.missing_fonts_lines(missing)
    assert "--fonts" in lines[0] and lines[1].startswith("  NowhereSans (sans, ")
    assert lines[1].endswith("letters): set in texgyreheros")


def test_a_metric_twin_is_still_a_font_the_deck_lacks(tmp_path):
    """Arimo sets Arial's widths, not its shapes: worth saying, since the file would do better."""
    root = tmp_path / "laid"
    fontfiles.install([given(tmp_path / "given", r_ttf=tiny_font("Arimo"))], root)
    ir = deck_ir(deck_with(text_shape("t", "Words in Arial", 10, 10, 300, 40, font="Arial")), foreign=True)
    missing: list = []
    with adopt.use_fonts(root):
        text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", missing=missing)
    assert "\\setsansfont{Arimo}" in text
    assert [(m["font"], m["set_in"]) for m in missing] == [("Arial", "Arimo")]


# ---------------------------------------------------------------- google/fonts without GitHub

def google_fonts_copy(root):
    """A local google/fonts checkout holding Tiny Sans (static) and Tiny Flex (variable)."""
    given(root / "ofl" / "tinysans", METADATA_pb=STATIC_META.encode(), OFL_txt=b"licence",
          **{f"TinySans-{s}_ttf": tiny_font("Tiny Sans", w) for s, w in (("Regular", 400), ("Bold", 700), ("Italic", 400))})
    (root / "ofl" / "tinyflex").mkdir(parents=True)
    (root / "ofl" / "tinyflex" / "METADATA.pb").write_text(VARIABLE_META, encoding="utf-8")
    (root / "ofl" / "tinyflex" / "TinyFlex[wght].ttf").write_bytes(tiny_font("Tiny Flex", variable=True))
    return root


def test_a_local_copy_of_google_fonts_is_read_and_nothing_downloaded(tmp_path, monkeypatch):
    src = google_fonts_copy(tmp_path / "google-fonts")
    monkeypatch.setenv("B2S_FONT_FETCH", "0")               # no network, and still the fonts
    with fontfetch.use_source(src):
        assert fontfetch.enabled()
        got = fontfetch.fetch_family("Tiny Sans", log=lambda *_: None)
        assert set(got) == {"Regular", "Bold", "Italic"}
        assert (fontfetch.cache_dir() / "tinysans" / "TinySans-LICENSE.txt").read_text() == "licence"
        assert set(fontfetch.fetch_family("Tiny Flex", log=lambda *_: None)) == {"Regular", "Bold"}
        assert fontfetch.fetch_family("Calibri", log=lambda *_: None) is None
    assert not (fontfetch.cache_dir() / "missing.json").exists(), "a partial copy says nothing of GitHub"
    assert not fontfetch.enabled()


def test_the_environment_names_the_local_copy_too(tmp_path, monkeypatch):
    monkeypatch.setenv("B2S_FONT_SOURCE", str(google_fonts_copy(tmp_path / "google-fonts")))
    assert fontfetch.source_root() == tmp_path / "google-fonts"
    assert fontfetch.fetch_family("Tiny Sans", log=lambda *_: None)["Regular"].exists()


def test_github_is_reached_through_the_callers_fetcher(tmp_path, fetcher):
    src = google_fonts_copy(tmp_path / "google-fonts")
    asked = []

    def serve(url):
        asked.append(url)
        path = src / url.removeprefix(fontfetch.RAW).replace("%5B", "[").replace("%5D", "]")
        if not path.is_file():
            raise FileNotFoundError(url)            # a harness's 404, in whatever type it likes
        return path.read_bytes()

    fetcher(serve)
    assert fontfetch.fetch_family("Tiny Flex", log=lambda *_: None)["Bold"].exists()
    assert asked and all(u.startswith(fontfetch.RAW) for u in asked)
    assert fontfetch.fetch_family("Calibri", log=lambda *_: None) is None
    assert "calibri" in json.loads((fontfetch.cache_dir() / "missing.json").read_text())


def test_a_fetcher_that_refuses_github_is_no_fetch_and_no_error(fetcher):
    class EgressDenied(Exception):
        pass

    for refusal in (PermissionError("not allowed"), EgressDenied("github.com is not on the list")):
        fetcher(lambda url, refusal=refusal: (_ for _ in ()).throw(refusal))
        said = []
        assert fontfetch.fetch_family("Open Sans", log=said.append) is None
        assert "could not fetch" in said[0]
    assert not (fontfetch.cache_dir() / "missing.json").exists(), "refused is not 'missing'"


# ---------------------------------------------------------------- the agent tool

def fake_adopt(monkeypatch, seen: dict, missing=(), skipped=()):
    def cmd_adopt(deck, tex, work, apply, out, max_iter, engine, flow, target_path, log=print,
                  fonts=None, found=None, pptx=None, files=None):
        seen["fonts"] = list(fonts or [])
        seen["files"] = files
        seen["source"] = fontfetch.source_root()
        seen["pptx"] = pptx
        found["supplied"] = {"families": {"Tiny Sans": {"styles": ["Regular"]}}, "skipped": list(skipped)}
        found["missing"] = [dict(m) for m in missing]
        if pptx is None:
            found["pictures_missing"] = [{"slide": 3, "alt": "a cat", "why": "downloads are off"}]
        else:
            found["pptx_pictures"] = 1
        return SimpleNamespace(converged=True, iterations=[], residuals=[], unresolved=[], files=[],
                               notes=[], theme=[])
    monkeypatch.setattr("beamer2slides.adopt.cmd_adopt", cmd_adopt)


class FakeGoogle:
    def credentials(self):
        return object()

    def describe(self) -> dict:
        return {"available": True, "source": "test", "scopes": []}


def test_deck_adopt_takes_fonts_as_refs_or_content_and_names_what_it_lacked(tmp_path, monkeypatch):
    from beamer2slides.agent.source_tools import deck_adopt
    seen: dict = {}
    fake_adopt(monkeypatch, seen, missing=[{"font": "Montserrat", "kind": "sans", "letters": 812,
                                            "set_in": "texgyreheros"}],
               skipped=[{"file": "fonts-2.bin", "reason": fontfiles.NOT_A_FONT}])
    ws = tmp_path / "ws"
    given(ws / "fonts", r_ttf=tiny_font("Tiny Sans"))
    (ws / "deck.json").write_text(json.dumps({"slides": []}), encoding="utf-8")
    ctx = AgentContext(workspace=LocalWorkspace(ws), google=FakeGoogle(), allow=ALL_ACTIONS,
                       font_source=str(tmp_path))
    res = deck_adopt(ctx, deck="deck.json", tex="new.tex",
                     fonts=["fonts", {"base64": base64.b64encode(web(tiny_font("X"), "woff")).decode()},
                            {"base64": base64.b64encode(b"junk").decode()}])
    assert res.ok, res.json()
    names = [p.name for p in seen["fonts"]]
    assert names[0] == "fonts" and names[1] != names[2], "two unnamed files are two files"
    assert seen["source"] == tmp_path, "the context's local google/fonts, for the call"
    assert res.data["fonts_supplied"] == {"Tiny Sans": ["Regular"]}
    assert res.data["fonts_missing"][0]["font"] == "Montserrat"
    fonts = [d for d in res.diagnostics if d.where == "fonts"]
    assert any("Montserrat" in d.message and "texgyreheros" in d.message for d in fonts)
    assert any("not used" in d.message for d in fonts)
    assert any("fonts=[...]" in s for s in res.next_steps)
    assert fontfetch.source_root() is None, "and not after it"
    assert res.data["pictures_missing"] == [{"slide": 3, "alt": "a cat", "why": "downloads are off"}]
    assert any(d.where == "pictures" and "slide 3" in d.message and "a cat" in d.message
               for d in res.diagnostics)
    assert any("pptx=" in s for s in res.next_steps), "the fix for them: the deck as a .pptx"


def test_deck_adopt_takes_the_decks_pptx_as_content(tmp_path, monkeypatch):
    """The harness hands the deck's download along with the deck: no path, just its bytes."""
    from beamer2slides.agent.source_tools import deck_adopt
    seen: dict = {}
    fake_adopt(monkeypatch, seen)
    (tmp_path / "deck.json").write_text(json.dumps({"slides": []}), encoding="utf-8")
    ctx = AgentContext(workspace=LocalWorkspace(tmp_path), google=FakeGoogle(), allow=ALL_ACTIONS)
    res = deck_adopt(ctx, deck="deck.json", tex="new.tex",
                     pptx={"name": "cats.pptx", "base64": base64.b64encode(b"PK-zip").decode()})
    assert res.ok, res.json()
    assert seen["pptx"].name == "cats.pptx" and seen["pptx"].read_bytes() == b"PK-zip"
    assert res.data["pptx_pictures"] == 1 and res.data["pictures_missing"] == []
    assert not any("pptx=" in s for s in res.next_steps)
    res = deck_adopt(ctx, deck="deck.json", tex="new2.tex", pptx="nosuch.pptx")
    assert not res.ok and res.code == "not_found" and "nosuch.pptx" in res.summary


def test_deck_adopt_refuses_a_font_ref_that_is_not_there(tmp_path, monkeypatch):
    from beamer2slides.agent.source_tools import deck_adopt
    fake_adopt(monkeypatch, {})
    ctx = AgentContext(workspace=LocalWorkspace(tmp_path), google=FakeGoogle(), allow=ALL_ACTIONS)
    res = deck_adopt(ctx, deck="deck.json", tex="new.tex", fonts=["nosuch.woff2"])
    assert not res.ok and res.code == "not_found" and "nosuch.woff2" in res.summary
