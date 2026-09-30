"""What adopt reads besides shapes, pictures and text: linked charts, videos, WordArt, pages of any
size, and the typefaces it fetches from google/fonts.

Offline: hand-made `presentations.get` answers, no TeX, and no network - every download goes
through a fake `fetch` or a patched `fontfetch.get`.
"""

import copy
import io
import json
import re
import urllib.error
from collections.abc import Callable, Iterator
from email.message import Message
from pathlib import Path
from typing import NoReturn

import pytest

from beamer2slides import adopt, fontfetch, google_types
from beamer2slides.deck_ir import MAX_BEAMER_SCALE, YOUTUBE_THUMB, page_size_for
from beamer2slides.deck_ir_types import TargetImage, TargetText, target_json
from beamer2slides.google_types import Presentation, Presentations
from beamer2slides.json_types import Json, JsonArray, JsonObject
from beamer2slides.net import Fetch
from beamer2slides.typing_compat import override
from .deck_records import record
from .fake_google import Answer, NoPresentations, NoSlides
from .irs import deck_ir
from .json_reads import jarr, jobj, jobjs, jstr

from .test_adopt import at, presentation, pt, solid, text_shape

EMU = 12700


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """As in test_adopt: no machine font and no fetching unless a test asks for it."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    adopt._FAMILIES.clear()
    yield
    adopt._FAMILIES.clear()


def quiet(message: str) -> None:
    """A log that says nothing."""


def group1(pattern: str, text: str) -> str:
    """The first group of `pattern`'s first match in `text`, which must match."""
    m = re.search(pattern, text)
    assert m is not None, pattern
    return m[1]


def png_bytes(w: int, h: int, colour: tuple[int, int, int] = (200, 30, 30), bars: int = 0) -> bytes:
    """A picture `w` x `h`, black `bars` rows high above and below (YouTube's letterbox)."""
    from PIL import Image
    img = Image.new("RGB", (w, h), (0, 0, 0))
    img.paste(Image.new("RGB", (w, h - 2 * bars), colour), (0, bars))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def element(oid: str, x: float, y: float, w: float, h: float, **kind: Json) -> JsonObject:
    el: JsonObject = {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y), **kind}
    return el


def deck_with(*elements: JsonObject) -> JsonObject:
    pres = jobj(presentation())
    page_elements: JsonArray = [*elements]
    pres["slides"] = [{"objectId": "s0", "slideProperties": {"layoutObjectId": "L1"},
                       "pageElements": page_elements, "pageProperties": {"pageBackgroundFill": solid("FFFFFF")}}]
    return pres


def not_found(url: str) -> urllib.error.HTTPError:
    """The 404 urllib raises for a file that is not there."""
    return urllib.error.HTTPError(url, 404, "Not Found", Message(), None)


class Fetcher:
    def __init__(self, answers: dict[str, bytes]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if url not in self.answers:
            raise not_found(url)
        return self.answers[url]


def own(ir: Json) -> list[JsonObject]:
    return [e for e in jobjs(ir, "slides", 0, "elements") if not e.get("inherited")]


def refuse_everything(url: str) -> bytes:
    """A fetcher that may fetch nothing."""
    raise PermissionError(url)


def no_drive(*a: object, **k: object) -> NoReturn:
    pytest.fail("Drive")


# ---------------------------------------------------------------- charts

def test_a_linked_chart_is_the_picture_slides_keeps_of_it(tmp_path: Path) -> None:
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
    assert Path(jstr(el, "file")).exists() and jstr(el, "outline", "color") == "#ff0000"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert re.search(r"\\slidepicture\[outline=\w+,outline width=[\d.]+\]\{[\d.,]+\}\{figures/chart-\w+\.png\}", text)
    assert "sheetsChart" not in json.dumps(deck_ir(deck_with(chart))), "pull reads no chart"
    assert not own(deck_ir(deck_with(chart))), "pull still leaves charts alone"


def test_a_picture_the_deck_would_not_give_is_named_and_marked_where_it_went(tmp_path: Path) -> None:
    """saudi-cats adopted with --no-downloads: 11 photos simply were not in the source, unsaid."""
    photo = element("p1", 60, 80, 200, 150, description="Sand cat",
                    image={"contentUrl": "https://example.invalid/cat.png"})
    kept = element("p2", 300, 80, 100, 100, image={"contentUrl": "https://example.invalid/ok.png"})
    fetch = Fetcher({"https://example.invalid/ok.png": png_bytes(10, 10)})
    ir = deck_ir(deck_with(photo, kept), foreign=True, fetch=fetch, images=tmp_path / "images")
    jarr(ir, "slides").append(copy.deepcopy(jobj(ir, "slides", 0)))
    theme, missing, again = adopt.pictures_missing(ir)
    assert missing["slide"] == 1 and missing["alt"] == "Sand cat" and "404" in missing["why"]
    assert theme.get("layout") == "m1" and again["slide"] == 2, "the master's picture said once, not per slide"
    jarr(ir, "slides").pop()
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "% picture left out: Sand cat (at " in text
    assert text.count("\\slidepicture") == 1, "the picture it has is still drawn"
    assert any("slide 1 'Sand cat'" in line for line in adopt.missing_pictures_lines([missing]))


def cat_deck() -> tuple[JsonObject, bytes, bytes]:
    """A deck of one photo, and the .pptx a person downloads of it (File > Download)."""
    from .test_deck_pictures import pic, pptx
    photo = element("p1", 60, 80, 200, 150, description="Sand cat",
                    image={"contentUrl": "https://example.invalid/cat.png"})
    pres = deck_with(photo)
    pres["presentationId"] = "P"
    cat = png_bytes(40, 30)                   # (not tiny: `adopt.TINY_PICTURE` would enlarge it)
    return pres, cat, pptx([(pic(None, "r1"), {"r1": cat}, None)])


class OneDeck(NoPresentations):
    """`presentations()` answering `get` with one deck."""

    def __init__(self, pres: Presentation) -> None:
        self.pres = pres

    @override
    def get(self, **kw: object) -> Answer[Presentation]:
        return Answer(self.pres)


class OneDeckSlides(NoSlides):
    def __init__(self, pres: Presentation) -> None:
        self.deck = OneDeck(pres)

    @override
    def presentations(self) -> Presentations:
        return self.deck


def test_a_live_read_takes_the_pptx_it_is_given_before_any_download(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: Callable[[Fetch], None]) -> None:
    """A sandbox that may fetch nothing is handed the deck's download with the deck: the photo
    comes out of it, no download is tried for it, and Drive is not asked for an export."""
    from beamer2slides.deck_ir import read_deck

    monkeypatch.setenv("B2S_ADOPT_THUMBNAILS", "0")
    pres, cat, data = cat_deck()
    asked: list[str] = []

    def refuse(url: str) -> bytes:
        asked.append(url)
        raise PermissionError(url)
    fetcher(refuse)

    def no_export(*a: object, **k: object) -> NoReturn:
        pytest.fail("a supplied .pptx is the export")
    monkeypatch.setattr("beamer2slides.google_auth.drive_service", no_export)
    slides = OneDeckSlides(google_types.presentation(pres, "the test's deck"))
    kept: dict[str, Json] = {}
    ir = target_json(read_deck("P", tmp_path / "images", None, None, slides, True, kept, data))
    [el] = own(ir)
    assert Path(jstr(el, "file")).read_bytes() == cat and kept["pptx_pictures"] == 1
    assert "https://example.invalid/cat.png" not in asked
    [theme] = adopt.pictures_missing(ir)
    assert theme.get("layout") == "m1", "the master's picture was not in this .pptx: still said"


def test_a_saved_presentation_is_read_with_no_google(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: Callable[[Fetch], None]) -> None:
    """A sandbox handed the deck as files: Google's own answer for it and its download. The reader
    runs here; the photo comes out of the .pptx; Drive is never asked."""
    from beamer2slides.deck_ir import is_presentation, read_presentation
    fetcher(refuse_everything)
    monkeypatch.setattr("beamer2slides.google_auth.drive_service", no_drive)
    pres, cat, data = cat_deck()
    assert is_presentation(pres) and not is_presentation(deck_ir(pres, foreign=True))
    kept: dict[str, Json] = {}
    ir = target_json(read_presentation(pres, tmp_path / "images", data, kept, None, True))
    [el] = own(ir)
    assert Path(jstr(el, "file")).read_bytes() == cat and el["object"] == "p1"
    assert kept["pptx_pictures"] == 1 and kept["presentation"] is pres
    assert ir["layouts"], "the layouts and masters are read as from the live deck"


def test_adopt_reads_a_saved_presentation_and_its_pptx(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetcher: Callable[[Fetch], None]) -> None:
    fetcher(refuse_everything)
    pres, cat, data = cat_deck()
    (tmp_path / "deck.json").write_text(json.dumps(pres), encoding="utf-8")
    (tmp_path / "deck.pptx").write_bytes(data)
    targets: list[JsonObject] = []
    bases: list[JsonObject | None] = []

    def pull(target: JsonObject, *a: object, **k: object) -> None:
        targets.append(target)

    def base(target: JsonObject, pres: JsonObject | None, *a: object, **k: object) -> None:
        bases.append(pres)
    monkeypatch.setattr("beamer2slides.inverse.run_pull", pull)
    monkeypatch.setattr(adopt, "record_base", base)
    found: dict[str, object] = {}
    adopt.cmd_adopt("deck.json", tmp_path / "tree" / "main.tex", tmp_path / "work", False, None, 1, None,
                    False, tmp_path / "deck.json", log=quiet, found=found, pptx=tmp_path / "deck.pptx")
    [el] = own(targets[0])
    assert Path(jstr(el, "file")).read_bytes() == cat
    recorded = bases[0]
    assert recorded is not None and recorded["presentationId"] == "P", \
        "a base is recorded from the answer's object ids"
    assert found["pptx_pictures"] == 1
    missing = found["pictures_missing"]
    assert isinstance(missing, list)
    assert [m for m in missing if not (isinstance(m, dict) and m.get("layout"))] == []
    assert "\\slidepicture" in (tmp_path / "tree" / "main.tex").read_text(encoding="utf-8")


def test_a_saved_target_takes_its_pictures_from_the_decks_pptx(tmp_path: Path) -> None:
    """A target read with downloads off, then the .pptx beside it: paired through the
    presentations.get it was read from."""
    from beamer2slides.deck_ir import pictures_from_pptx
    pres, cat, data = cat_deck()
    ir = deck_ir(pres, foreign=True, fetch=Fetcher({}), images=tmp_path / "images")
    [el] = own(ir)
    assert "error" in el and not el.get("file")
    assert pictures_from_pptx(ir, pres, data, tmp_path / "images") == (1, 1)
    assert Path(jstr(el, "file")).read_bytes() == cat and "error" not in el
    assert not [m for m in adopt.pictures_missing(ir) if not m.get("layout")]
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "picture left out: Sand cat" not in text
    # another deck's download pairs nothing and fills nothing
    other = deck_with()
    assert pictures_from_pptx(deck_ir(other, foreign=True), other, data, tmp_path / "images")[0] == 0


# ---------------------------------------------------------------- videos

def youtube(oid: str, w: float, h: float) -> JsonObject:
    return element(oid, 100, 100, w, h, video={"source": "YOUTUBE", "id": "abc_123",
                                               "url": "https://www.youtube.com/watch?v=abc_123",
                                               "videoProperties": {"outline": {"propertyState": "NOT_RENDERED"}}})


def test_a_youtube_video_is_its_poster_frame_linked_to_the_video(tmp_path: Path) -> None:
    thumb = YOUTUBE_THUMB.format(id="abc_123")
    fetch = Fetcher({thumb: png_bytes(480, 360, bars=45)})         # 16:9 inside a 4:3 thumbnail
    ir = deck_ir(deck_with(youtube("v1", 320.0, 180.0)), foreign=True, fetch=fetch, images=tmp_path / "images")
    [el] = own(ir)
    assert jstr(el, "video", "source") == "YOUTUBE" and el["file"] and thumb in fetch.asked
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "\\href{https://www.youtube.com/watch?v=abc_123}{%" in text
    [framed] = (tmp_path / "tree" / "figures").glob("video-*.png")
    assert framed.name in text
    from PIL import Image
    with Image.open(framed) as img:
        assert abs(img.width / img.height - 16 / 9) < 0.01
        assert img.getpixel((img.width // 2, 2)) != (0, 0, 0), "the letterbox bars are cut off in a 16:9 box"


def test_a_poster_frame_keeps_its_bars_in_a_taller_box(tmp_path: Path) -> None:
    src = tmp_path / "thumb.png"
    src.write_bytes(png_bytes(480, 360, bars=45))
    out = adopt.letterboxed(src, 300, 300, tmp_path / "square.png")
    from PIL import Image
    with Image.open(out) as img:
        assert img.size == (960, 960)
        assert img.getpixel((480, 5)) == (0, 0, 0) and img.getpixel((480, 480)) != (0, 0, 0)


def test_a_drive_video_is_a_play_panel_linked_to_the_video(tmp_path: Path) -> None:
    """No API gives a Drive video's poster frame: a dark panel with a play symbol, not nothing."""
    drive = element("v2", 100, 100, 320, 180, video={"source": "DRIVE", "id": "d1",
                                                     "url": "https://drive.google.com/file/d/d1/view#t=5"})
    fetch = Fetcher({})
    ir = deck_ir(deck_with(drive), foreign=True, fetch=fetch, images=tmp_path / "images")
    assert fetch.asked == ["https://example.invalid/backdrop.png"], "nothing to download for a Drive video"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "\\href{https://drive.google.com/file/d/d1/view\\#t=5}{%" in text
    assert "circle" in text and "cycle" in text                     # the play symbol
    assert text.count("\\begin{textblock*}") == text.count("\\end{textblock*}")


def test_a_video_whose_thumbnail_is_gone_is_still_drawn(tmp_path: Path) -> None:
    """A deleted YouTube video's thumbnail is a 404: the placeholder stands in."""
    ir = deck_ir(deck_with(youtube("v1", 320.0, 180.0)), foreign=True, fetch=Fetcher({}), images=tmp_path / "images")
    [el] = own(ir)
    assert "error" in el and not el.get("file")
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "\\href{https://www.youtube.com/watch?v=abc_123}" in text and "circle" in text


# ---------------------------------------------------------------- WordArt

def test_wordart_is_its_words_stretched_to_its_box(tmp_path: Path) -> None:
    art = element("w1", 50, 40, 300, 120, wordArt={"renderedText": "Hello\u000bWorld"})
    art["transform"] = {"scaleX": 0.0, "scaleY": 0.0, "shearX": 1.0, "shearY": -1.0,
                        "translateX": 50 * EMU, "translateY": 340 * EMU, "unit": "EMU"}   # turned 90 degrees
    ir = deck_ir(deck_with(art), foreign=True)
    [el] = own(ir)
    assert el["kind"] == "text" and el["wordart"]
    assert ["".join(jstr(r, "text") for r in jobjs(p, "runs")) for p in jobjs(el, "paragraphs")] == ["Hello", "World"]
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    line = next(l for l in text.splitlines() if "resizebox" in l)
    assert "\\resizebox*{" in line and "Hello\\\\World" in line and "\\rotatebox[origin=c]" in line
    assert not own(deck_ir(deck_with(art))), "pull reads no WordArt"


def test_wordart_with_a_text_box_is_stretched_to_its_bbox() -> None:
    """A WordArt whose `box` is a text box's layout, not its unturned frame, stretches to its bbox.
    Unpacking the layout as four numbers raised a ValueError before 485d624."""
    el = record({"kind": "text", "wordart": True, "bbox": [10.0, 20.0, 110.0, 60.0],
                 "box": {"valign": "top", "scale": 1.0},
                 "paragraphs": [{"runs": [{"text": "Art", "size": 12.0, "color": "#112233"}]}]})
    assert isinstance(el, TargetText)
    text = adopt.wordart_block(el, adopt.adopt_context(), "")
    assert "\\resizebox*{100.0pt}{40.0pt}{\\bfseries Art}" in text


def test_empty_wordart_is_nothing() -> None:
    assert not own(deck_ir(deck_with(element("w2", 0, 0, 10, 10, wordArt={"renderedText": "\u000b"})),
                           foreign=True))


# ---------------------------------------------------------------- the page

def sized(w: float, h: float) -> JsonObject:
    pres = jobj(presentation())
    pres["pageSize"] = {"width": pt(w), "height": pt(h)}
    return pres


def page_of(w: float, h: float) -> Presentation:
    """`sized` as `presentations.get` answers it."""
    return google_types.presentation(sized(w, h), "the test's deck")


@pytest.mark.parametrize("w,h", [(720, 405), (1440, 810), (1920, 1080)])
def test_a_widescreen_deck_of_any_size_is_beamers_169(w: float, h: float) -> None:
    pw, ph, scale = page_size_for(page_of(w, h), None, foreign=True)
    assert (round(pw, 2), round(ph, 2)) == (453.54, 255.12) and abs(scale - w / 453.54) < 1e-9
    assert adopt.page_setup([pw, ph]) == ("aspectratio=169", "")


def test_a_page_beamer_has_no_option_for_is_that_page(tmp_path: Path) -> None:
    """An A4 portrait deck drawn on beamer's nearest landscape page came out squeezed."""
    pw, ph, scale = page_size_for(page_of(595.3, 841.9), None, foreign=True)
    assert scale == 2.0 and abs(pw / ph - 595.3 / 841.9) < 1e-6
    opt, paper = adopt.page_setup([pw, ph])
    assert opt == "" and paper == f"\\geometry{{papersize={{{pw:.2f}bp,{ph:.2f}bp}}}}"
    text = adopt.bootstrap(deck_ir(sized(595.3, 841.9), foreign=True), tmp_path / "tree" / "main.tex", False, None)
    assert "aspectratio" not in text
    assert paper in text and text.index("\\documentclass") < text.index(paper) < text.index("\\begin{document}")


def test_a_poster_is_not_shrunk_to_beamers_page() -> None:
    """A 48 x 36 in poster is 4:3, but at beamer's 4:3 page its 24 pt text would be 2.5 pt."""
    w, h = 48 * 72, 36 * 72
    pw, ph, scale = page_size_for(page_of(w, h), None, foreign=True)
    assert scale <= MAX_BEAMER_SCALE and (pw, ph) == (w / 2, h / 2)
    assert page_size_for(page_of(w, h), None, False)[2] > MAX_BEAMER_SCALE, "pull keeps beamer's page"


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

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        path = url.removeprefix(fontfetch.RAW)
        if path not in self.files:
            raise not_found(url)
        return self.files[path]


def fetched(family: str) -> dict[str, Path]:
    """`fontfetch.fetch_family`, which must find the family."""
    got = fontfetch.fetch_family(family, log=quiet)
    assert got is not None, family
    return got


def cut(upright: Path, weight: int, italic: bool) -> Path:
    """`fontfetch.weight_file`, which must cut the weight."""
    path = fontfetch.weight_file(upright, weight, italic)
    assert path is not None, weight
    return path


def test_metadata_says_which_file_is_which_style() -> None:
    meta = fontfetch.parse_metadata(STATIC_META + VARIABLE_META.split("\n", 1)[1])
    assert meta.name == "Tiny Sans"
    assert [(f.filename, f.style, f.weight) for f in meta.fonts][:3] == [
        ("TinySans-Regular.ttf", "normal", 400), ("TinySans-Bold.ttf", "normal", 700),
        ("TinySans-Italic.ttf", "italic", 400)]
    assert meta.axes == {"wght": (100.0, 900.0)}


def test_a_static_family_is_fetched_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fontTools")
    hub = GitHub({"ofl/tinysans/METADATA.pb": STATIC_META.encode(), "ofl/tinysans/OFL.txt": b"licence",
                  **{f"ofl/tinysans/TinySans-{s}.ttf": tiny_font("Tiny Sans", w)
                     for s, w in (("Regular", 400), ("Bold", 700), ("Italic", 400))}})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fetched("Tiny Sans")
    assert set(got) == {"Regular", "Bold", "Italic", "BoldItalic"} - {"BoldItalic"}
    assert got["Bold"].read_bytes() == hub.files["ofl/tinysans/TinySans-Bold.ttf"]
    assert (fontfetch.cache_dir() / "tinysans" / "TinySans-LICENSE.txt").read_text() == "licence"
    asked = len(hub.asked)
    assert fontfetch.fetch_family("TinySans", log=quiet) == got
    assert len(hub.asked) == asked, "the second deck in Tiny Sans asks GitHub nothing"


def test_a_variable_family_is_cut_into_static_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fetched("Tiny Flex")
    assert set(got) == {"Regular", "Bold"}, "no italic in the family, none invented"
    for style, weight in (("Regular", 400), ("Bold", 700)):
        font = TTFont(got[style])
        assert "fvar" not in font and fontfetch.table_int(font, "OS/2", "usWeightClass") == weight


def test_a_family_google_fonts_lacks_is_asked_for_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = GitHub({})
    monkeypatch.setattr(fontfetch, "get", hub)
    assert fontfetch.fetch_family("Calibri", log=quiet) is None
    assert len(hub.asked) == len(fontfetch.LICENCE_DIRS)
    assert "calibri" in json.loads((fontfetch.cache_dir() / "missing.json").read_text())
    assert fontfetch.fetch_family("Calibri", log=quiet) is None
    assert len(hub.asked) == len(fontfetch.LICENCE_DIRS), "remembered"


def test_offline_is_no_font_and_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(fontfetch, "get", offline)
    said: list[str] = []
    assert fontfetch.fetch_family("Open Sans", log=said.append) is None
    assert said and "could not fetch" in said[0]
    assert not (fontfetch.cache_dir() / "missing.json").exists(), "offline is not 'missing'"


def test_fetching_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("B2S_FONT_FETCH", "0")

    def no_fetch(url: str) -> NoReturn:
        pytest.fail("fetched with fetching off")
    monkeypatch.setattr(fontfetch, "get", no_fetch)
    assert fontfetch.fetch_family("Open Sans", print) is None


def test_b2s_fonts_means_those_folders_and_no_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_fetch(*a: object, **k: object) -> NoReturn:
        pytest.fail("fetched under $B2S_FONTS")
    monkeypatch.setattr(fontfetch, "fetch_family", no_fetch)
    assert not adopt.fetching()
    assert adopt.font_family("Open Sans", "sans", "") is None


def cache_only() -> list[Path]:
    """`adopt.font_dirs` when the only fonts are the ones fetched into the cache."""
    return [fontfetch.cache_dir()] if fontfetch.cache_dir().is_dir() else []


def test_adopt_sets_the_deck_in_a_fetched_family(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The deck is in a font the machine lacks: fetched, then declared like any font it has."""
    pytest.importorskip("fontTools")
    monkeypatch.delenv("B2S_FONTS")
    monkeypatch.setattr(adopt, "font_dirs", cache_only)
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/OFL.txt": b"licence",
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    ir = deck_ir(deck_with(text_shape("t", "Words in a fetched face", 10, 10, 300, 40, font="Tiny Flex")),
                 foreign=True)
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    # the family has no italic (the variable font has no ital axis), so italic and bold italic are
    # fontspec's slant of the two faces it does have, never left unsaid
    assert ("\\setsansfont{TinyFlex}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular,BoldFont=*-Bold,"
            f"ItalicFont=*-Regular,ItalicFeatures={{FakeSlant={adopt.FAKE_SLANT}}},"
            f"BoldItalicFont=*-Bold,BoldItalicFeatures={{FakeSlant={adopt.FAKE_SLANT}}}]") in text
    fonts = sorted(p.name for p in (tmp_path / "tree" / "fonts").iterdir())
    assert fonts == ["TinyFlex-Bold.ttf", "TinyFlex-LICENSE.txt", "TinyFlex-Regular.ttf"]


def test_ligatures_are_drawn_only_from_slides_own_copy_of_a_font(tmp_path: Path) -> None:
    """Slides joins Google Sans' t_t (gdg24's "little"), a google/fonts file as Slides has it; it
    draws Calibri's words in a stand-in with none of the ti/tt joins this machine's Calibri makes,
    and its Lato is older than google/fonts' 2.015, whose ti it does not join (ml-vs-stats). A
    google/fonts stand-in is not what Slides draws: Droid Serif set in Noto Serif (ap-bio-stats)."""
    cache = fontfetch.cache_dir()
    def files(path: Path) -> adopt.FontFiles:
        return adopt.FontFiles(faces={"UprightFont": path}, index=0)
    assert adopt.ligatures(files(cache / "googlesans" / "GoogleSans-Regular.ttf"), None) == ""
    assert adopt.ligatures(files(cache / "lato" / "Lato-Regular.ttf"), None) == adopt.ONLY_F_LIGATURES
    assert adopt.ligatures(files(tmp_path / "Windows" / "Fonts" / "calibri.ttf"), None) == adopt.NO_LIGATURES
    noto = files(cache / "notoserif" / "NotoSerif-Regular.ttf")
    assert adopt.ligatures(noto, None) == ""
    assert adopt.ligatures(noto, "NotoSerif") == adopt.NO_LIGATURES


def flex_font(name: str, axes: list[tuple[str, float, float, float, str]]) -> bytes:
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


def test_google_sans_text_is_google_sans_at_its_text_optical_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """google/fonts has no Google Sans Text: it is Google Sans' opsz 17 (gdg24's 50 pt "Statistics" set
    3.2% narrower in the opsz 18 default that stood in for it). Cut from Google Sans' variable font
    into a family of its own, named so that it cannot be taken for the display cut."""
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/googlesans/METADATA.pb": GOOGLE_SANS_META.encode(),
                  "ofl/googlesans/GoogleSans%5BGRAD%2Copsz%2Cwght%5D.ttf": flex_font(
                      "Google Sans", [("opsz", 17, 18, 18, "Optical size"), ("wght", 400, 400, 700, "Weight")])})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fetched("Google Sans Text")
    assert set(got) == {"Regular", "Bold"}
    assert got["Regular"] == fontfetch.cache_dir() / "googlesanstext" / "GoogleSansText-Regular.ttf"
    font = TTFont(got["Bold"])
    assert "fvar" not in font and fontfetch.table_int(font, "OS/2", "usWeightClass") == 700
    assert font["name"].getDebugName(1) == "Google Sans Text" and font["name"].getDebugName(16) is None
    assert not any("googlesanstext" in u for u in hub.asked), "nothing is asked for under the name Slides uses"
    # and the weights Slides sets per run are cut at the same optical size
    w600 = cut(got["Regular"], 600, False)
    assert w600 == got["Regular"].with_name("GoogleSansText-W600.ttf")
    assert fontfetch.table_int(TTFont(w600), "OS/2", "usWeightClass") == 600


def test_a_weight_between_regular_and_bold_is_cut_when_the_family_is_variable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("fontTools")
    from fontTools.ttLib import TTFont
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    regular = fetched("Tiny Flex")["Regular"]
    for weight in (300, 500, 600):
        path = cut(regular, weight, False)
        assert path.name == f"TinyFlex-W{weight}.ttf" and fontfetch.table_int(TTFont(path), "OS/2", "usWeightClass") == weight
    assert fontfetch.weight_file(regular, 950, False) is None, "off the axis"
    assert fontfetch.weight_file(regular, 600, True) is None, "no italic to cut it from"
    elsewhere = tmp_path / "shelf" / "TinyFlex-Regular.ttf"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(regular.read_bytes())
    assert fontfetch.weight_file(elsewhere, 600, False) is None, "only families this cache fetched"


def thin_default_font() -> bytes:
    """A variable font whose default instance is its Thin, named so, as Montserrat's is."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(tiny_font("Tiny Flex", variable=True)))
    fb = FontBuilder(font=font)
    fb.setupNameTable({"familyName": "Tiny Flex Thin", "styleName": "Regular", "typographicFamily": "Tiny Flex",
                       "typographicSubfamily": "Thin", "psName": "TinyFlex-Thin"})
    fb.setupFvar(axes=[("wght", 100, 100, 900, "Weight")], instances=[])
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def names(path: Path) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    from fontTools.ttLib import TTFont
    with TTFont(path) as font:
        n = font["name"]
        return n.getDebugName(1), n.getDebugName(2), n.getDebugName(6), n.getDebugName(16), n.getDebugName(17)


def test_an_instance_is_named_for_its_weight_not_the_variable_fonts_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Montserrat's default instance is its Thin, and every face cut from it kept that name: the
    saudi-cats deck compiled with bold headings called Montserrat-Thin, and pull read them back as
    not bold. Every instance is named for the style or weight it was cut at."""
    pytest.importorskip("fontTools")
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": thin_default_font()})
    monkeypatch.setattr(fontfetch, "get", hub)
    got = fetched("Tiny Flex")
    assert names(got["Regular"]) == ("Tiny Flex", "Regular", "TinyFlex-Regular", None, None)
    assert names(got["Bold"]) == ("Tiny Flex", "Bold", "TinyFlex-Bold", None, None)
    assert names(cut(got["Regular"], 500, False)) == (
        "Tiny Flex Medium", "Regular", "TinyFlex-Medium", "Tiny Flex", "Medium")
    assert names(cut(got["Regular"], 450, False))[2] == "TinyFlex-W450"
    from beamer2slides.fonts import font_info
    assert font_info("ABCDEF+TinyFlex-Bold").bold and not font_info("ABCDEF+TinyFlex-Regular").bold


def test_instances_cut_before_they_were_named_are_named_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache filled by an older version holds faces that call themselves Thin: they are named in
    place, from the files already there, with nothing downloaded."""
    pytest.importorskip("fontTools")
    from fontTools.varLib import instancer
    from fontTools.ttLib import TTFont
    folder = fontfetch.cache_dir() / "tinyflex"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "TinyFlex[wght].ttf").write_bytes(thin_default_font())
    for file, weight in (("TinyFlex-Regular.ttf", 400), ("TinyFlex-Bold.ttf", 700), ("TinyFlex-W300.ttf", 300)):
        font = instancer.instantiateVariableFont(TTFont(folder / "src" / "TinyFlex[wght].ttf"), {"wght": weight})
        font.save(folder / file)
    assert names(folder / "TinyFlex-Bold.ttf")[2] == "TinyFlex-Thin"
    def no_fetch(url: str) -> NoReturn:
        pytest.fail(f"asked for {url}")
    monkeypatch.setattr(fontfetch, "get", no_fetch)
    got = fetched("Tiny Flex")
    assert names(got["Bold"])[2] == "TinyFlex-Bold" and names(got["Regular"])[:3] == (
        "Tiny Flex", "Regular", "TinyFlex-Regular")
    assert names(folder / "TinyFlex-W300.ttf")[2:] == ("TinyFlex-Light", "Tiny Flex", "Light")
    assert (folder / fontfetch.NAMED).exists()
    before = (folder / "TinyFlex-Bold.ttf").stat().st_mtime_ns
    fontfetch.repair_cache(fontfetch.cache_dir())
    assert (folder / "TinyFlex-Bold.ttf").stat().st_mtime_ns == before, "named once"


def test_a_run_in_weight_600_is_set_in_its_own_face(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """gdg24's headings are Google Sans 600, which fontspec's four styles do not have: bold stood in,
    2.4% too wide. The weight gets a FontFace of its own and the box selects its series."""
    pytest.importorskip("fontTools")
    monkeypatch.delenv("B2S_FONTS")
    monkeypatch.setattr(adopt, "font_dirs", cache_only)
    hub = GitHub({"ofl/tinyflex/METADATA.pb": VARIABLE_META.encode(),
                  "ofl/tinyflex/TinyFlex%5Bwght%5D.ttf": tiny_font("Tiny Flex", variable=True)})
    monkeypatch.setattr(fontfetch, "get", hub)
    head = text_shape("h", "This is a Headline in semibold", 10, 10, 400, 40, font="Tiny Flex")
    body = text_shape("b", "Body words set in the regular weight, and one medium word", 10, 100, 400, 60,
                      font="Tiny Flex")
    jobj(head, "shape", "text", "textElements", 1, "textRun", "style")["weightedFontFamily"] = {
        "fontFamily": "Tiny Flex", "weight": 600}
    ir = deck_ir(deck_with(jobj(head), jobj(body)), foreign=True)
    heading = next(e for e in jobjs(ir, "slides", 0, "elements") if e.get("id") == "h")
    run = jobj(heading, "paragraphs", 0, "runs", 0)
    assert run["weight"] == 600 and run["bold"], "600 is bold to anything that knows only two weights"
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert ",FontFace={w600}{n}{Font=TinyFlex-W600}]" in text
    style = group1(r"\\slidetext\{[^}]*\}\{([\w-]+)\}\{This is a Headline", text)
    assert re.search(rf"\\slidestyle\{{{style}\}}\{{[^}}]*weight=w600[,}}]", text), "its style selects series w600"
    assert "\\bfseries" not in text and "weight=bold" not in text
    assert "\\noexpand\\fontseries{\\slides@k@weight}\\noexpand\\selectfont" in (tmp_path / "tree" / "slides.sty").read_text(encoding="utf-8")
    assert (tmp_path / "tree" / "fonts" / "TinyFlex-W600.ttf").exists()


def test_a_decks_second_face_gets_a_switch_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Montserrat titles over Open Sans text: both are the deck's look, not whichever has more letters."""
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    for stem in ("OpenSans", "Montserrat"):
        for style in ("Regular", "Bold"):
            (shelf / f"{stem}-{style}.ttf").write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("B2S_FONTS", str(shelf))
    ir = deck_ir(deck_with(text_shape("t0", "body text " * 20, 10, 10, 600, 100, font="Open Sans"),
                           text_shape("t1", "A title " * 8, 10, 200, 600, 60, font="Montserrat")), foreign=True)
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "\\setsansfont{OpenSans}" in text
    assert "\\newfontfamily\\adoptfontA{Montserrat}" in text

    def face(words: str) -> str | None:
        """The face option of the style the box holding `words` is set in."""
        style = group1(rf"\\slidetext\{{[^}}]*\}}\{{([\w-]+)\}}\{{{words}", text)
        keys = group1(rf"\\slidestyle\{{{style}\}}\{{([^\n]*)\}}\n", text)
        m = re.search(r"face=(\\\w+)", keys)
        return m[1] if m else None
    assert face("A title") == "\\adoptfontA"
    assert face("body text") is None


def test_a_face_of_few_but_huge_letters_gets_a_switch_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
        text = adopt.bootstrap(ir, tmp_path / f"tree{size:.0f}" / "main.tex", False, None)
        assert ("\\newfontfamily\\adoptfontA{Montserrat}" in text) == switched


def test_a_face_without_the_letters_set_in_it_gets_no_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex", False, None)
    assert "\\setsansfont{OpenSans}" in text and "NotoSansSymbols" not in text
    assert adopt.font_coverage(shelf / "NotoSansSymbols-Regular.ttf", {"A": 3, "B": 1}, 0, False) == 0.75
    # and when it is the deck's most used font, it is not the document's either
    only = deck_ir(deck_with(text_shape("t1", "שלום " * 20, 10, 200, 600, 60,
                                        font="Noto Sans Symbols"),
                             text_shape("t2", "AAAA", 10, 10, 600, 100, font="Open Sans")), foreign=True)
    text = adopt.bootstrap(only, tmp_path / "tree2" / "main.tex", False, None)
    assert "\\setsansfont{OpenSans}" in text and "NotoSansSymbols" not in text


def picture(file: str, alt: str, sha1: str, **props: Json) -> TargetImage:
    """A picture element as `picture_of` reads it: its file, alt text and hash, and what Slides bakes in."""
    el = record({"kind": "image", "bbox": [0, 0, 10, 10], "file": file, "alt": alt, "sha1": sha1, **props})
    assert isinstance(el, TargetImage)
    return el


def picture_in(el: TargetImage, tree: Path) -> adopt.Picture:
    """`adopt.picture_of`, which must find the file."""
    pic = adopt.picture_of(el, tree)
    assert pic is not None
    return pic


def test_a_dimmed_picture_is_baked_into_its_file(tmp_path: Path) -> None:
    """intro-lecture's title photos carry brightness -0.5 and -0.7, which LaTeX has no option for:
    the file in the tree is the picture as Slides shows it - its colours scaled by 1 + b (measured on
    the thumbnails), not moved by b, which would black out every pixel under half grey."""
    from PIL import Image
    src = tmp_path / "photo.png"
    Image.new("RGB", (8, 8), (200, 100, 40)).save(src)
    tree = tmp_path / "src"
    dim = picture_in(picture(str(src), "Hall", "ab" * 20, brightness=-0.5), tree)
    plain = picture_in(picture(str(src), "Hall", "ab" * 20), tree)
    assert dim.rel != plain.rel
    assert Image.open(dim.path).convert("RGB").getpixel((3, 3)) == (100, 50, 20)
    assert Image.open(plain.path).convert("RGB").getpixel((3, 3)) == (200, 100, 40)
    bright = picture_in(picture(str(src), "Hall", "ab" * 20, brightness=0.5), tree)
    assert Image.open(bright.path).convert("RGB").getpixel((3, 3)) == (255, 200, 80)


def test_a_tiny_picture_is_drawn_smooth_at_its_size(tmp_path: Path) -> None:
    """vi-slides' background is a 5x5 px photo Slides stretches smoothed over the page; a PDF viewer
    drew its pixels as squares. It is enlarged, blended, and still the size graphicx gives it."""
    from PIL import Image
    from beamer2slides.inverse import natural_size
    src = tmp_path / "bg.png"
    img = Image.new("RGB", (5, 5), (0, 0, 0))
    img.putpixel((0, 0), (255, 255, 255))
    img.save(src)
    pic = picture_in(picture(str(src), "background", "cd" * 20), tmp_path / "src")
    out = Image.open(pic.path).convert("RGB")
    assert max(out.size) == adopt.SMOOTH_PICTURE
    assert natural_size(pic.path) == pytest.approx((5, 5), abs=0.05)
    pixel = out.getpixel((adopt.SMOOTH_PICTURE // 5, 10))
    assert isinstance(pixel, tuple)
    edge = pixel[0]
    assert 0 < edge < 255, "the pixels blend into each other"
    big = tmp_path / "big.png"
    Image.new("RGB", (64, 64), (9, 9, 9)).save(big)
    assert Image.open(picture_in(picture(str(big), "x", "ef" * 20), tmp_path / "src").path).size == (64, 64)
