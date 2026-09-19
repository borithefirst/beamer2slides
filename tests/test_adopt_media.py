"""What adopt reads besides shapes, pictures and text: linked charts, videos, WordArt, pages of any
size, and the typefaces it fetches from google/fonts.

Offline: hand-made `presentations.get` answers, no TeX, and no network - every download goes
through a fake `fetch` or a patched `fontfetch.get`.
"""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from beamer2slides import adopt, fontfetch
from beamer2slides.deck_ir import MAX_BEAMER_SCALE, YOUTUBE_THUMB, deck_ir, page_size_for

from .test_adopt import at, presentation, pt, solid, text_shape

EMU = 12700


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    """As in test_adopt: no machine font and no fetching unless a test asks for it."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    adopt._FAMILIES.clear()
    yield
    adopt._FAMILIES.clear()


def png_bytes(w: int, h: int, colour=(200, 30, 30), bars: int = 0) -> bytes:
    """A picture `w` x `h`, black `bars` rows high above and below (YouTube's letterbox)."""
    from PIL import Image
    img = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(Image.new("RGB", (w, h - 2 * bars), colour), (0, bars))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def element(oid: str, x: float, y: float, w: float, h: float, **kind) -> dict:
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y), **kind}


def deck_with(*elements: dict) -> dict:
    pres = presentation()
    pres["slides"] = [{"objectId": "s0", "slideProperties": {"layoutObjectId": "L1"},
                       "pageElements": list(elements), "pageProperties": {"pageBackgroundFill": solid("FFFFFF")}}]
    return pres


class Fetcher:
    def __init__(self, answers: dict[str, bytes]):
        self.answers, self.asked = answers, []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if url not in self.answers:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.answers[url]


def own(ir: dict) -> list[dict]:
    return [e for e in ir["slides"][0]["elements"] if not e.get("inherited")]


# ---------------------------------------------------------------- charts

def test_a_linked_chart_is_the_picture_slides_keeps_of_it(tmp_path):
    """74 of solidity-survey's slides are a chart and its title; the chart was simply not read."""
    chart = element("c1", 60, 80, 400, 250, sheetsChart={
        "spreadsheetId": "sheet", "chartId": 7, "contentUrl": "https://example.invalid/chart.png",
        "sheetsChartProperties": {"chartImageProperties": {
            "outline": {"outlineFill": solid("FF0000"), "weight": pt(2), "propertyState": "RENDERED"}}}})
    fetch = Fetcher({"https://example.invalid/chart.png": png_bytes(40, 25)})
    ir = deck_ir(deck_with(chart), foreign=True, fetch=fetch, images=tmp_path / "images")
    [el] = own(ir)
    assert (el["kind"], el["role"]) == ("image", "figure")
    assert el["chart"] == {"spreadsheetId": "sheet", "chartId": 7}
    assert Path(el["file"]).exists() and el["outline"]["color"] == "#ff0000"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\includegraphics" in text
    assert "sheetsChart" not in json.dumps(deck_ir(deck_with(chart))), "pull reads no chart"
    assert not own(deck_ir(deck_with(chart))), "pull still leaves charts alone"


# ---------------------------------------------------------------- videos

def youtube(oid="v1", w=320.0, h=180.0) -> dict:
    return element(oid, 100, 100, w, h, video={"source": "YOUTUBE", "id": "abc_123",
                                               "url": "https://www.youtube.com/watch?v=abc_123",
                                               "videoProperties": {"outline": {"propertyState": "NOT_RENDERED"}}})


def test_a_youtube_video_is_its_poster_frame_linked_to_the_video(tmp_path):
    thumb = YOUTUBE_THUMB.format(id="abc_123")
    fetch = Fetcher({thumb: png_bytes(480, 360, bars=45)})         # 16:9 inside a 4:3 thumbnail
    ir = deck_ir(deck_with(youtube()), foreign=True, fetch=fetch, images=tmp_path / "images")
    [el] = own(ir)
    assert el["video"]["source"] == "YOUTUBE" and el["file"] and thumb in fetch.asked
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\href{https://www.youtube.com/watch?v=abc_123}{%" in text
    [framed] = (tmp_path / "tree" / "figures").glob("video-*.png")
    assert framed.name in text
    from PIL import Image
    with Image.open(framed) as img:
        assert abs(img.width / img.height - 16 / 9) < 0.01
        assert img.getpixel((img.width // 2, 2)) != (0, 0, 0), "the letterbox bars are cut off in a 16:9 box"


def test_a_poster_frame_keeps_its_bars_in_a_taller_box(tmp_path):
    src = tmp_path / "thumb.png"
    src.write_bytes(png_bytes(480, 360, bars=45))
    out = adopt.letterboxed(src, 300, 300, tmp_path / "square.png")
    from PIL import Image
    with Image.open(out) as img:
        assert img.size == (960, 960)
        assert img.getpixel((480, 5)) == (0, 0, 0) and img.getpixel((480, 480)) != (0, 0, 0)


def test_a_drive_video_is_a_play_panel_linked_to_the_video(tmp_path):
    """No API gives a Drive video's poster frame: a dark panel with a play symbol, not nothing."""
    drive = element("v2", 100, 100, 320, 180, video={"source": "DRIVE", "id": "d1",
                                                     "url": "https://drive.google.com/file/d/d1/view#t=5"})
    fetch = Fetcher({})
    ir = deck_ir(deck_with(drive), foreign=True, fetch=fetch, images=tmp_path / "images")
    assert fetch.asked == ["https://example.invalid/backdrop.png"], "nothing to download for a Drive video"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\href{https://drive.google.com/file/d/d1/view\\#t=5}{%" in text
    assert "circle" in text and "cycle" in text                     # the play symbol
    assert text.count("\\begin{textblock*}") == text.count("\\end{textblock*}")


def test_a_video_whose_thumbnail_is_gone_is_still_drawn(tmp_path):
    """A deleted YouTube video's thumbnail is a 404: the placeholder stands in."""
    ir = deck_ir(deck_with(youtube()), foreign=True, fetch=Fetcher({}), images=tmp_path / "images")
    [el] = own(ir)
    assert "error" in el and not el.get("file")
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\href{https://www.youtube.com/watch?v=abc_123}" in text and "circle" in text


# ---------------------------------------------------------------- WordArt

def test_wordart_is_its_words_stretched_to_its_box(tmp_path):
    art = element("w1", 50, 40, 300, 120, wordArt={"renderedText": "Hello\u000bWorld"})
    art["transform"] = {"scaleX": 0.0, "scaleY": 0.0, "shearX": 1.0, "shearY": -1.0,
                        "translateX": 50 * EMU, "translateY": 340 * EMU, "unit": "EMU"}   # turned 90 degrees
    ir = deck_ir(deck_with(art), foreign=True)
    [el] = own(ir)
    assert el["kind"] == "text" and el["wordart"]
    assert ["".join(r["text"] for r in p["runs"]) for p in el["paragraphs"]] == ["Hello", "World"]
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    line = next(l for l in text.splitlines() if "resizebox" in l)
    assert "\\resizebox*{" in line and "Hello\\\\World" in line and "\\rotatebox[origin=c]" in line
    assert not own(deck_ir(deck_with(art))), "pull reads no WordArt"


def test_empty_wordart_is_nothing():
    assert not own(deck_ir(deck_with(element("w2", 0, 0, 10, 10, wordArt={"renderedText": "\u000b"})),
                           foreign=True))


# ---------------------------------------------------------------- the page

def sized(w: float, h: float) -> dict:
    pres = presentation()
    pres["pageSize"] = {"width": pt(w), "height": pt(h)}
    return pres


@pytest.mark.parametrize("w,h", [(720, 405), (1440, 810), (1920, 1080)])
def test_a_widescreen_deck_of_any_size_is_beamers_169(w, h):
    pw, ph, scale = page_size_for(sized(w, h), None, foreign=True)
    assert (round(pw, 2), round(ph, 2)) == (453.54, 255.12) and abs(scale - w / 453.54) < 1e-9
    assert adopt.page_setup([pw, ph]) == ("aspectratio=169", "")


def test_a_page_beamer_has_no_option_for_is_that_page(tmp_path):
    """An A4 portrait deck drawn on beamer's nearest landscape page came out squeezed."""
    pw, ph, scale = page_size_for(sized(595.3, 841.9), None, foreign=True)
    assert scale == 2.0 and abs(pw / ph - 595.3 / 841.9) < 1e-6
    opt, paper = adopt.page_setup([pw, ph])
    assert opt == "" and paper == f"\\geometry{{papersize={{{pw:.2f}bp,{ph:.2f}bp}}}}"
    text = adopt.bootstrap(deck_ir(sized(595.3, 841.9), foreign=True), tmp_path / "tree" / "main.tex")
    assert "aspectratio" not in text
    assert paper in text and text.index("\\documentclass") < text.index(paper) < text.index("\\begin{document}")


def test_a_poster_is_not_shrunk_to_beamers_page():
    """A 48 x 36 in poster is 4:3, but at beamer's 4:3 page its 24 pt text would be 2.5 pt."""
    w, h = 48 * 72, 36 * 72
    pw, ph, scale = page_size_for(sized(w, h), None, foreign=True)
    assert scale <= MAX_BEAMER_SCALE and (pw, ph) == (w / 2, h / 2)
    assert page_size_for(sized(w, h), None)[2] > MAX_BEAMER_SCALE, "pull keeps beamer's page"


# ---------------------------------------------------------------- fonts from google/fonts

def tiny_font(name: str, weight: int = 400, variable: bool = False, chars: str = "") -> bytes:
    """A font with one glyph drawn for every printable ASCII character (or only for `chars`),
    static or with a wght axis 100-900."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    fb = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "A"]
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({ord(c): "A" for c in (chars or map(chr, range(33, 127)))})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)), pen.lineTo((0, 500)), pen.lineTo((500, 0)), pen.closePath()
    glyph = pen.glyph()
    fb.setupGlyf({g: glyph for g in glyphs})
    fb.setupHorizontalMetrics({g: (600, 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": name, "styleName": "Regular"})
    fb.setupOS2(usWeightClass=weight)
    fb.setupPost()
    if variable:
        fb.setupFvar(axes=[("wght", 100, 400, 900, "Weight")], instances=[])
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


STATIC_META = """name: "Tiny Sans"
designer: "Nobody"
license: "OFL"
fonts {
  name: "Tiny Sans"
  style: "normal"
  weight: 400
  filename: "TinySans-Regular.ttf"
}
fonts {
  name: "Tiny Sans"
  style: "normal"
  weight: 700
  filename: "TinySans-Bold.ttf"
}
fonts {
  name: "Tiny Sans"
  style: "italic"
  weight: 400
  filename: "TinySans-Italic.ttf"
}
"""

VARIABLE_META = """name: "Tiny Flex"
fonts {
  name: "Tiny Flex"
  style: "normal"
  weight: 400
  filename: "TinyFlex[wght].ttf"
}
axes {
  tag: "wght"
  min_value: 100.0
  max_value: 900.0
}
"""


class GitHub:
    """google/fonts, as `fontfetch.get` sees it: raw file URLs, 404 for everything else."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.asked = files, []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        path = url.removeprefix(fontfetch.RAW)
        if path not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.files[path]


def test_metadata_says_which_file_is_which_style():
    meta = fontfetch.parse_metadata(STATIC_META + VARIABLE_META.split("\n", 1)[1])
    assert meta["name"] == "Tiny Sans"
    assert [(f["filename"], f["style"], f["weight"]) for f in meta["fonts"]][:3] == [
        ("TinySans-Regular.ttf", "normal", 400), ("TinySans-Bold.ttf", "normal", 700),
        ("TinySans-Italic.ttf", "italic", 400)]
    assert meta["axes"] == {"wght": (100.0, 900.0)}


def test_a_static_family_is_fetched_once(monkeypatch):
    pytest.importorskip("fontTools")
    hub = GitHub({"ofl/tinysans/METADATA.pb": STATIC_META.encode(), "ofl/tinysans/OFL.txt": b"licence",
                  **{f"ofl/tinysans/TinySans-{s}.ttf": tiny_font("Tiny Sans", w)
                     for s, w in (("Regular", 400), ("Bold", 700), ("Italic", 400))}})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fontfetch.fetch_family("Tiny Sans", log=lambda *_: None)
    assert set(got) == {"Regular", "Bold", "Italic", "BoldItalic"} - {"BoldItalic"}
    assert got["Bold"].read_bytes() == hub.files["ofl/tinysans/TinySans-Bold.ttf"]
    assert (fontfetch.cache_dir() / "tinysans" / "TinySans-LICENSE.txt").read_text() == "licence"
    asked = len(hub.asked)
    assert fontfetch.fetch_family("TinySans", log=lambda *_: None) == got
    assert len(hub.asked) == asked, "the second deck in Tiny Sans asks GitHub nothing"


def test_a_variable_family_is_cut_into_static_instances(monkeypatch):
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fontfetch.fetch_family("Tiny Flex", log=lambda *_: None)
    assert set(got) == {"Regular", "Bold"}, "no italic in the family, none invented"
    for style, weight in (("Regular", 400), ("Bold", 700)):
        font = TTFont(got[style])
        assert "fvar" not in font and font["OS/2"].usWeightClass == weight


def test_a_family_google_fonts_lacks_is_asked_for_once(monkeypatch):
    hub = GitHub({})
    monkeypatch.setattr(fontfetch, "get", hub)
    assert fontfetch.fetch_family("Calibri", log=lambda *_: None) is None
    assert len(hub.asked) == len(fontfetch.LICENCE_DIRS)
    assert "calibri" in json.loads((fontfetch.cache_dir() / "missing.json").read_text())
    assert fontfetch.fetch_family("Calibri", log=lambda *_: None) is None
    assert len(hub.asked) == len(fontfetch.LICENCE_DIRS), "remembered"


def test_offline_is_no_font_and_no_error(monkeypatch):
    def offline(url):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(fontfetch, "get", offline)
    said = []
    assert fontfetch.fetch_family("Open Sans", log=said.append) is None
    assert said and "could not fetch" in said[0]
    assert not (fontfetch.cache_dir() / "missing.json").exists(), "offline is not 'missing'"


def test_fetching_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("B2S_FONT_FETCH", "0")
    monkeypatch.setattr(fontfetch, "get", lambda url: pytest.fail("fetched with fetching off"))
    assert fontfetch.fetch_family("Open Sans") is None


def test_b2s_fonts_means_those_folders_and_no_fetching(monkeypatch):
    monkeypatch.setattr(fontfetch, "fetch_family", lambda *a, **k: pytest.fail("fetched under $B2S_FONTS"))
    assert not adopt.fetching()
    assert adopt.font_family("Open Sans", "sans") == {}


def test_adopt_sets_the_deck_in_a_fetched_family(monkeypatch, tmp_path):
    """The deck is in a font the machine lacks: fetched, then declared like any font it has."""
    pytest.importorskip("fontTools")
    monkeypatch.delenv("B2S_FONTS")
    monkeypatch.setattr(adopt, "font_dirs", lambda: [fontfetch.cache_dir()] if fontfetch.cache_dir().is_dir() else [])
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/OFL.txt": b"licence",
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    ir = deck_ir(deck_with(text_shape("t", "Words in a fetched face", 10, 10, 300, 40, font="Tiny Flex")),
                 foreign=True)
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\setsansfont{TinyFlex}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular,BoldFont=*-Bold]" in text
    fonts = sorted(p.name for p in (tmp_path / "tree" / "fonts").iterdir())
    assert fonts == ["TinyFlex-Bold.ttf", "TinyFlex-LICENSE.txt", "TinyFlex-Regular.ttf"]


def flex_font(name: str, axes: list[tuple]) -> bytes:
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(tiny_font(name, variable=True)))
    from fontTools.fontBuilder import FontBuilder
    fb = FontBuilder(font=font)
    fb.setupFvar(axes=axes, instances=[])
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


GOOGLE_SANS_META = """name: "Google Sans"
fonts {
  name: "Google Sans"
  style: "normal"
  weight: 400
  filename: "GoogleSans[GRAD,opsz,wght].ttf"
}
"""


def test_google_sans_text_is_google_sans_at_its_text_optical_size(monkeypatch):
    """google/fonts has no Google Sans Text: it is Google Sans' opsz 17 (gdg24's 50 pt "Statistics" set
    3.2% narrower in the opsz 18 default that stood in for it). Cut from Google Sans' variable font
    into a family of its own, named so that it cannot be taken for the display cut."""
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/googlesans/METADATA.pb": GOOGLE_SANS_META.encode(),
                  "ofl/googlesans/GoogleSans%5BGRAD%2Copsz%2Cwght%5D.ttf": flex_font(
                      "Google Sans", [("opsz", 17, 18, 18, "Optical size"), ("wght", 400, 400, 700, "Weight")])})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fontfetch.fetch_family("Google Sans Text", log=lambda *_: None)
    assert set(got) == {"Regular", "Bold"}
    assert got["Regular"] == fontfetch.cache_dir() / "googlesanstext" / "GoogleSansText-Regular.ttf"
    font = TTFont(got["Bold"])
    assert "fvar" not in font and font["OS/2"].usWeightClass == 700
    assert font["name"].getDebugName(1) == "Google Sans Text" and font["name"].getDebugName(16) is None
    assert not any("googlesanstext" in u for u in hub.asked), "nothing is asked for under the name Slides uses"
    # and the weights Slides sets per run are cut at the same optical size
    w600 = fontfetch.weight_file(got["Regular"], 600)
    assert w600 == got["Regular"].with_name("GoogleSansText-W600.ttf")
    assert TTFont(w600)["OS/2"].usWeightClass == 600


def test_a_weight_between_regular_and_bold_is_cut_when_the_family_is_variable(monkeypatch, tmp_path):
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    regular = fontfetch.fetch_family("Tiny Flex", log=lambda *_: None)["Regular"]
    for weight in (300, 500, 600):
        path = fontfetch.weight_file(regular, weight)
        assert path.name == f"TinyFlex-W{weight}.ttf" and TTFont(path)["OS/2"].usWeightClass == weight
    assert fontfetch.weight_file(regular, 950) is None, "off the axis"
    assert fontfetch.weight_file(regular, 600, italic=True) is None, "no italic to cut it from"
    elsewhere = tmp_path / "shelf" / "TinyFlex-Regular.ttf"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(regular.read_bytes())
    assert fontfetch.weight_file(elsewhere, 600) is None, "only families this cache fetched"


def test_a_run_in_weight_600_is_set_in_its_own_face(monkeypatch, tmp_path):
    """gdg24's headings are Google Sans 600, which fontspec's four styles do not have: bold stood in,
    2.4% too wide. The weight gets a FontFace of its own and the box selects its series."""
    pytest.importorskip("fontTools")
    monkeypatch.delenv("B2S_FONTS")
    monkeypatch.setattr(adopt, "font_dirs", lambda: [fontfetch.cache_dir()] if fontfetch.cache_dir().is_dir() else [])
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    head = text_shape("h", "This is a Headline in semibold", 10, 10, 400, 40, font="Tiny Flex")
    body = text_shape("b", "Body words set in the regular weight, and one medium word", 10, 100, 400, 60,
                      font="Tiny Flex")
    head["shape"]["text"]["textElements"][1]["textRun"]["style"]["weightedFontFamily"] = {
        "fontFamily": "Tiny Flex", "weight": 600}
    ir = deck_ir(deck_with(head, body), foreign=True)
    run = next(e for e in ir["slides"][0]["elements"] if e.get("id") == "h")["paragraphs"][0]["runs"][0]
    assert run["weight"] == 600 and run["bold"], "600 is bold to anything that knows only two weights"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert ",FontFace={w600}{n}{Font=TinyFlex-W600}]" in text
    assert "\\fontseries{w600}\\selectfont " in text and "\\bfseries" not in text
    assert (tmp_path / "tree" / "fonts" / "TinyFlex-W600.ttf").exists()


def test_a_decks_second_face_gets_a_switch_of_its_own(monkeypatch, tmp_path):
    """Montserrat titles over Open Sans text: both are the deck's look, not whichever has more letters."""
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    for stem in ("OpenSans", "Montserrat"):
        for style in ("Regular", "Bold"):
            (shelf / f"{stem}-{style}.ttf").write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    ir = deck_ir(deck_with(text_shape("t0", "body text " * 20, 10, 10, 600, 100, font="Open Sans"),
                           text_shape("t1", "A title " * 8, 10, 200, 600, 60, font="Montserrat")), foreign=True)
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\setsansfont{OpenSans}" in text
    assert "\\newfontfamily\\adoptfontA{Montserrat}" in text
    title = text[text.index("A title") - 200:text.index("A title")]
    assert "\\adoptfontA" in title
    body = text[text.index("body text") - 200:text.index("body text")]
    assert "\\adoptfontA" not in body


def test_a_face_of_few_but_huge_letters_gets_a_switch_too(monkeypatch, tmp_path):
    """sc-memphis' section numbers: two digits a slide at 528 pt are its look as much as a paragraph;
    the same two digits at body size are not."""
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    for stem in ("OpenSans", "Montserrat"):
        for style in ("Regular", "Bold"):
            (shelf / f"{stem}-{style}.ttf").write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    for size, switched in ((300.0, True), (20.0, False)):
        ir = deck_ir(deck_with(text_shape("t0", "body text " * 20, 10, 10, 600, 100, font="Open Sans"),
                               text_shape("t1", "03", 10, 120, 600, 300, font="Montserrat", size=size)),
                     foreign=True)
        text = adopt.bootstrap(ir, tmp_path / f"tree{size:.0f}" / "main.tex")
        assert ("\\newfontfamily\\adoptfontA{Montserrat}" in text) == switched


def test_a_face_without_the_letters_set_in_it_gets_no_switch(monkeypatch, tmp_path):
    """hebrew-lesson types Hebrew "in" Noto Sans Symbols and Slides draws it in a fallback; a switch
    to the font itself set nothing, and lualatex stops on a font embedded with no glyph."""
    pytest.importorskip("fontTools")
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    (shelf / "OpenSans-Regular.ttf").write_bytes(tiny_font("Open Sans"))
    (shelf / "NotoSansSymbols-Regular.ttf").write_bytes(tiny_font("Noto Sans Symbols", chars="A"))
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    ir = deck_ir(deck_with(text_shape("t0", "AAAA " * 30, 10, 10, 600, 100, font="Open Sans"),
                           text_shape("t1", "שלום " * 20, 10, 200, 600, 60,
                                      font="Noto Sans Symbols")), foreign=True)
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "\\setsansfont{OpenSans}" in text and "NotoSansSymbols" not in text
    assert adopt.font_coverage(shelf / "NotoSansSymbols-Regular.ttf", {"A": 3, "B": 1}) == 0.75
    # and when it is the deck's most used font, it is not the document's either
    only = deck_ir(deck_with(text_shape("t1", "שלום " * 20, 10, 200, 600, 60,
                                        font="Noto Sans Symbols"),
                             text_shape("t2", "AAAA", 10, 10, 600, 100, font="Open Sans")), foreign=True)
    text = adopt.bootstrap(only, tmp_path / "tree2" / "main.tex")
    assert "\\setsansfont{OpenSans}" in text and "NotoSansSymbols" not in text
