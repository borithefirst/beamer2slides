"""A picture adopt has no drawable file for - a download that came back as a Google sign-in page
or a site's 404 page, a fetch the sandbox refuses with no export or .pptx to fall back on
(`--no-downloads`), an SVG - never becomes text or is silently dropped: `deck_fills.recover_pictures`
crops the slide's own thumbnail for it, a turned one sampled back upright, one partly off the page
cut to the part on it, words drawn above painted out; a background picture is the page the
thumbnail shows around what stands on it (`deck_fills.background_from_thumbnail`).
`adopt.pictures_from_thumbnail` reports the swap and the frame says so in a comment; what stays
truly unreadable (no thumbnail) is still `adopt.pictures_missing`. A .pptx given later puts the
file back (`deck_ir.pictures_from_pptx`)."""

import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from beamer2slides import adopt, deck_fills
from .irs import deck_ir

EMU = 12700
HTML_SIGNIN = b"<!doctype html><html><head><title>Sign in - Google Accounts</title></head></html>"
HTML_404 = b"<!DOCTYPE html><html data-dpl-id=\"x\" id=\"__next_error__\"><title>Not found</title></html>"


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    """As in test_adopt_media: no machine font and no fetching unless a test asks for it."""
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    adopt._FAMILIES.clear()
    yield
    adopt._FAMILIES.clear()


def pt(v: float) -> dict:
    return {"magnitude": v * EMU, "unit": "EMU"}


def at(x: float, y: float) -> dict:
    return {"scaleX": 1.0, "scaleY": 1.0, "translateX": x * EMU, "translateY": y * EMU, "unit": "EMU"}


def image_pe(oid: str, x: float, y: float, w: float, h: float, content_url: str, source_url: str | None = None) -> dict:
    image = {"contentUrl": content_url}
    if source_url:
        image["sourceUrl"] = source_url
    return {"objectId": oid, "size": {"width": pt(w), "height": pt(h)}, "transform": at(x, y), "image": image}


def deck(*elements) -> dict:
    """A 720 x 405 pt deck (16:9, the same scale `test_adopt_fills.py` relies on), one slide."""
    return {"presentationId": "p", "title": "t", "pageSize": {"width": pt(720), "height": pt(405)},
            "masters": [{"objectId": "m", "pageElements": [], "pageProperties": {}}],
            "layouts": [{"objectId": "L", "layoutProperties": {"masterObjectId": "m"}, "pageElements": []}],
            "slides": [{"objectId": "s", "slideProperties": {"layoutObjectId": "L"},
                        "pageElements": list(elements)}]}


def page(bg=(255, 255, 255)) -> np.ndarray:
    return np.full((405, 720, 3), bg, dtype=np.uint8)


# ---------------------------------------------------------------- deck_fills.recover_pictures (unit)

def test_a_fetch_error_is_drawn_from_the_thumbnail(tmp_path):
    thumb = page()
    thumb[100:200, 100:300] = [30, 60, 90]
    el = {"kind": "image", "role": "figure", "bbox": [100, 100, 300, 200], "id": "im", "object": "im",
          "group": None, "error": "refused"}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert "error" not in el
    assert el["picture_source"] == "thumbnail" and el["format"] == "png"
    from PIL import Image
    im = np.asarray(Image.open(el["file"]).convert("RGB"))
    assert (im == thumb[100:200, 100:300]).all()


def test_undecodable_bytes_are_drawn_from_the_thumbnail_not_kept_as_html(tmp_path):
    """`format` "unknown" (bytes `image_format` could not read at all, e.g. an HTML page fetched in
    place of a picture) is recovered exactly like an outright fetch error."""
    thumb = page()
    thumb[50:150, 50:250] = [10, 200, 30]
    el = {"kind": "image", "bbox": [50, 50, 250, 150], "id": "im", "object": "im", "group": None,
          "file": "somewhere.img", "format": "unknown", "sha1": "deadbeef"}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["format"] == "png" and el["picture_source"] == "thumbnail"
    assert el["file"] != "somewhere.img"


def test_a_video_is_left_to_its_own_poster_frame(tmp_path):
    """`settle` takes a video's poster frame from the thumbnail itself (`poster`), link and all."""
    thumb = page()
    thumb[100:200, 100:300] = [30, 60, 90]
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "id": "im", "object": "im", "group": None,
          "error": "refused", "video": {"source": "YOUTUBE", "id": "y"}}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert "picture_source" not in el and el.get("error") == "refused"


@pytest.mark.parametrize("extra", [{}, {"chart": {"chartId": "1"}}, {"flip": True}, {"rotation": 180.0, "flip": True},
                                   {"file": None}, {"file": "gone.png", "format": "png"}])
def test_every_picture_with_no_file_is_recovered(extra, tmp_path):
    """No file at all (`--no-downloads`, no fetcher, the export refused too), one gone from disk, a
    linked chart's render, a mirrored picture: the thumbnail shows each as the slide draws it,
    flip included, so the crop is upright and the flip goes."""
    thumb = page()
    thumb[100:200, 100:300] = [30, 60, 90]
    thumb[100:200, 100:110] = [200, 0, 0]              # which way round it is drawn
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "box": [100, 100, 300, 200], "id": "im",
          "object": "im", "group": None, **extra}
    if "file" not in extra:
        el["error"] = "downloads are off"
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["picture_source"] == "thumbnail" and "flip" not in el and "rotation" not in el
    assert el["bbox"] == [100, 100, 300, 200] == el["box"]
    im = np.asarray(Image.open(el["file"]).convert("RGB"))
    assert (im == thumb[100:200, 100:300]).all()
    assert el["thumbnail_of"].get("flip") == extra.get("flip"), "what the crop replaced is kept"


def test_an_svg_is_drawn_from_the_thumbnail(tmp_path):
    """A format LaTeX cannot include (`adopt.UNINCLUDABLE`) is no file adopt can draw either."""
    svg = tmp_path / "logo.svg"
    svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    thumb = page()
    thumb[40:80, 40:80] = [0, 120, 0]
    el = {"kind": "image", "bbox": [40, 40, 80, 80], "id": "im", "file": str(svg), "format": "svg", "sha1": "s"}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path / "images")
    assert el["picture_source"] == "thumbnail" and el["format"] == "png"


def test_words_drawn_above_are_painted_out_of_the_crop(tmp_path):
    """A caption over the photo is drawn again by its own text box: in the crop too, it would be
    printed twice a little apart."""
    thumb = page()
    thumb[100:200, 100:300] = [200, 30, 30]
    thumb[140:150, 150:250] = [0, 0, 0]               # the caption's letters, on the photo
    photo = {"kind": "image", "bbox": [100, 100, 300, 200], "box": [100, 100, 300, 200], "id": "im", "error": "x"}
    caption = {"kind": "text", "bbox": [140, 130, 260, 160],
               "paragraphs": [{"runs": [{"text": "Sand cat", "color": "#000000"}]}]}
    deck_fills.recover_pictures([photo, caption], thumb, 1.0, tmp_path)
    im = np.asarray(Image.open(photo["file"]).convert("RGB")).astype(int)
    assert np.abs(im - [200, 30, 30]).max() <= 2, "no letter left, and the photo filled in under them"


def rotated_thumbnail(angle: float, box, colour=(30, 160, 60)) -> np.ndarray:
    """The page with a `box` (unturned frame) turned `angle` degrees clockwise about its centre,
    drawn at 4 px per pt and with its left edge marked red, as Slides would render it."""
    from PIL import ImageDraw
    k = 4
    im = Image.new("RGB", (720 * k, 405 * k), (255, 255, 255))
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    t = np.radians(angle)

    def turn(x, y):
        dx, dy = x - cx, y - cy
        return ((cx + dx * np.cos(t) - dy * np.sin(t)) * k, (cy + dx * np.sin(t) + dy * np.cos(t)) * k)
    draw = ImageDraw.Draw(im)
    draw.polygon([turn(x0, y0), turn(x1, y0), turn(x1, y1), turn(x0, y1)], fill=colour)
    draw.polygon([turn(x0, y0), turn(x0 + 10, y0), turn(x0 + 10, y1), turn(x0, y1)], fill=(220, 0, 0))
    return np.asarray(im)


def aabb(angle: float, box) -> list[float]:
    x0, y0, x1, y1 = box
    cx, cy, t = (x0 + x1) / 2, (y0 + y1) / 2, np.radians(angle)
    xs, ys = [], []
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        xs.append(cx + (x - cx) * np.cos(t) - (y - cy) * np.sin(t))
        ys.append(cy + (x - cx) * np.sin(t) + (y - cy) * np.cos(t))
    return [min(xs), min(ys), max(xs), max(ys)]


def test_a_turned_picture_is_sampled_back_upright_and_keeps_its_turn(tmp_path):
    """The rectangle itself, not its bounds: an axis-aligned cut would carry the page's corners, and
    the element would no longer be the deck object's box and turn."""
    box = [200, 100, 360, 220]
    thumb = rotated_thumbnail(30, box)
    el = {"kind": "image", "bbox": aabb(30, box), "box": box, "rotation": 30.0, "id": "im", "error": "x",
          "crop": {"l": 0.1, "t": 0, "r": 0, "b": 0}, "opacity": 0.5}
    deck_fills.recover_pictures([el], thumb, 4.0, tmp_path)
    assert el["rotation"] == 30.0 and el["box"] == box and "crop" not in el and "opacity" not in el
    im = np.asarray(Image.open(el["file"]).convert("RGB")).astype(int)
    assert im.shape[:2] == (480, 640), "the unturned frame at the thumbnail's resolution"
    inner = im[4:-4, 44:-4]
    assert np.abs(inner - [30, 160, 60]).max() <= 8, "no page corner in it"
    assert np.abs(im[4:-4, 4:36] - [220, 0, 0]).max() <= 8, "the left edge is still the left edge"


def test_a_quarter_turn_is_cut_as_it_stands(tmp_path):
    box = [200, 100, 360, 220]
    thumb = rotated_thumbnail(90, box)
    bbox = [round(v, 6) for v in aabb(90, box)]
    el = {"kind": "image", "bbox": bbox, "box": box, "rotation": 90.0, "id": "im", "error": "x"}
    deck_fills.recover_pictures([el], thumb, 4.0, tmp_path)
    assert "rotation" not in el and el["box"] == bbox == el["bbox"]
    im = np.asarray(Image.open(el["file"]).convert("RGB"))
    assert im.shape[:2] == (640, 480)
    assert (im[1:39, 8:-8] == [220, 0, 0]).all(), "turned clockwise: the left edge is on top"


def test_a_picture_partly_off_the_page_is_the_part_on_it(tmp_path):
    thumb = page()
    thumb[50:150, 0:100] = [10, 20, 200]
    el = {"kind": "image", "bbox": [-60, 50, 100, 150], "box": [-60, 50, 100, 150], "id": "im", "error": "x",
          "outline": {"color": "#000000", "weight": 1.0, "dash": "SOLID"}}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["bbox"] == [0, 50, 100, 150] == el["box"]
    assert "outline" not in el, "an outline on the cut edge would be drawn where the deck has none"
    im = np.asarray(Image.open(el["file"]).convert("RGB"))
    assert im.shape[:2] == (100, 100) and (im == [10, 20, 200]).all()


def test_an_outline_stays_on_a_picture_wholly_on_the_page(tmp_path):
    thumb = page()
    thumb[50:150, 50:150] = [10, 20, 200]
    outline = {"color": "#000000", "weight": 1.0, "dash": "SOLID"}
    el = {"kind": "image", "bbox": [50, 50, 150, 150], "id": "im", "error": "x", "outline": outline}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["outline"] == outline


def test_a_good_picture_is_left_alone(tmp_path):
    thumb = page()
    ok = tmp_path / "ok.png"
    Image.new("RGB", (4, 4)).save(ok)
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "id": "im", "object": "im", "group": None,
          "file": str(ok), "format": "png", "sha1": "abc"}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["file"] == str(ok) and "picture_source" not in el


def test_without_a_thumbnail_nothing_changes(tmp_path):
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "id": "im", "object": "im", "group": None,
          "error": "refused"}
    deck_fills.recover_pictures([el], None, 1.0, tmp_path)
    assert el.get("error") == "refused" and "picture_source" not in el


# ---------------------------------------------------------------- deck_ir end to end

def test_deck_ir_recovers_a_picture_whose_download_was_html(tmp_path):
    url = "https://lh7-rt.googleusercontent.com/slidesz/broken=s2048"
    pres = deck(image_pe("im", 100, 100, 200, 100, url))
    thumb = page()
    thumb[100:200, 100:300] = [200, 30, 30]

    def fetch(u):
        assert u == url
        return HTML_SIGNIN

    ir = deck_ir(pres, fetch=fetch, images=tmp_path, foreign=True, thumbnails=lambda n: thumb)
    el = ir["slides"][0]["elements"][0]
    assert el["kind"] == "image" and el["picture_source"] == "thumbnail"
    assert el["format"] == "png" and Path(el["file"]).exists()
    assert "error" not in el


def test_deck_ir_leaves_a_broken_picture_missing_without_a_thumbnail(tmp_path):
    url = "https://lh7-rt.googleusercontent.com/slidesz/broken=s2048"
    pres = deck(image_pe("im", 100, 100, 200, 100, url))

    def fetch(u):
        return HTML_SIGNIN

    ir = deck_ir(pres, fetch=fetch, images=tmp_path, foreign=True, thumbnails=None)
    el = ir["slides"][0]["elements"][0]
    assert el["format"] == "unknown" and "picture_source" not in el


def turned_pe(oid: str, box, angle: float, content_url: str) -> dict:
    """An image element whose `box` (unturned frame, pt) is turned `angle` degrees clockwise about its centre."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    c, s = float(np.cos(np.radians(angle))), float(np.sin(np.radians(angle)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tx, ty = cx - (c * w / 2 - s * h / 2), cy - (s * w / 2 + c * h / 2)
    return {"objectId": oid, "description": "Sand cat", "size": {"width": pt(w), "height": pt(h)},
            "transform": {"scaleX": c, "shearX": -s, "shearY": s, "scaleY": c,
                          "translateX": tx * EMU, "translateY": ty * EMU, "unit": "EMU"},
            "image": {"contentUrl": content_url}}


def refused(url):
    raise PermissionError(f"downloads are off ({url})")


def test_no_downloads_draws_a_turned_picture_from_the_thumbnail_and_the_frame_says_so(tmp_path):
    """`--no-downloads` in a sandbox: the fetch and the export both refuse. The picture is the
    thumbnail's, turned as the deck turns it, and the source says where it came from."""
    box = [200, 100, 360, 220]
    pres = deck(turned_pe("im", box, 30, "https://lh7-rt.googleusercontent.com/cat=s2048"))
    thumb = rotated_thumbnail(30, box)
    ir = deck_ir(pres, fetch=refused, images=tmp_path / "images", foreign=True, thumbnails=lambda n: thumb)
    [el] = ir["slides"][0]["elements"]
    assert el["picture_source"] == "thumbnail" and el["rotation"] == pytest.approx(30, abs=0.01)
    assert "error" in el["thumbnail_of"] and "error" not in el
    assert adopt.pictures_missing(ir) == []
    assert adopt.pictures_from_thumbnail(ir) == [{"slide": 1, "alt": "Sand cat"}]
    text = adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")
    assert "% picture from the slide thumbnail: Sand cat" in text and "picture left out" not in text
    assert re.search(r"\\slidepicture\[angle=-30\]\{[\d.,]+\}\{figures/sand-cat-\w+\.png\}", text)


def test_without_its_thumbnail_the_picture_stays_missing(tmp_path):
    pres = deck(image_pe("im", 100, 100, 200, 100, "https://lh7-rt.googleusercontent.com/cat=s2048"))
    ir = deck_ir(pres, fetch=refused, images=tmp_path / "images", foreign=True, thumbnails=lambda n: None)
    [el] = ir["slides"][0]["elements"]
    assert "picture_source" not in el and len(adopt.pictures_missing(ir)) == 1
    assert "% picture left out" in adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")


def test_a_pptx_given_later_puts_the_file_back_as_the_deck_draws_it(tmp_path):
    """A saved target read with downloads off took the thumbnail's crop; the deck's .pptx then
    brings the file itself, with the crop and outline the thumbnail had baked in."""
    from beamer2slides.deck_ir import pictures_from_pptx

    from .test_adopt_media import cat_deck
    pres, cat, data = cat_deck()
    pres["slides"][0]["pageElements"][0]["image"]["imageProperties"] = {"cropProperties": {"leftOffset": 0.25}}
    thumb = page()
    ir = deck_ir(pres, fetch=refused, images=tmp_path / "images", foreign=True, thumbnails=lambda n: thumb)
    [el] = [e for e in ir["slides"][0]["elements"] if e.get("object") == "p1"]
    assert el["picture_source"] == "thumbnail" and "crop" not in el
    filled, held = pictures_from_pptx(ir, pres, data, tmp_path / "images")
    assert filled >= 1 and held == 1
    assert Path(el["file"]).read_bytes() == cat and el["crop"]["l"] == 0.25
    assert "picture_source" not in el and "thumbnail_of" not in el
    assert adopt.pictures_from_thumbnail({"slides": [{"elements": [el]}]}) == []


def test_a_background_picture_that_never_came_is_the_page_the_thumbnail_shows(tmp_path):
    """The page under a see-through text box is the photo itself, its letters painted out; the
    page under an opaque element is painted in from around it, since that element hides it anyway."""
    pres = deck()
    pres["slides"][0]["pageProperties"] = {"pageBackgroundFill": {"stretchedPictureFill": {
        "contentUrl": "https://lh7-rt.googleusercontent.com/backdrop=s2048"}}}
    from .test_adopt import text_shape
    pres["slides"][0]["pageElements"] = [text_shape("t", "Welcome", 100, 100, 300, 60, colour="000000")]
    yy, xx = np.mgrid[0:405, 0:720]
    thumb = np.dstack([xx * 255 // 720, yy * 255 // 405, 255 - xx * 255 // 720]).astype(np.uint8)
    thumb[120:140, 120:300] = 0                        # the words, in black
    ir = deck_ir(pres, fetch=refused, images=tmp_path / "images", foreign=True, thumbnails=lambda n: thumb)
    s = ir["slides"][0]
    assert s["background_source"] == "thumbnail" and Path(s["background_file"]).exists()
    bg = np.asarray(Image.open(s["background_file"]).convert("RGB")).astype(int)
    assert bg[120:140, 120:300].max(axis=2).min() > 40, "no letter left in the backdrop"
    assert (bg[300:, 500:] == thumb[300:, 500:]).all(), "the page the thumbnail shows, pixel for pixel"
    assert adopt.pictures_missing(ir) == []
    assert adopt.pictures_from_thumbnail(ir) == [{"slide": 1, "alt": "slide background"}]
    assert adopt.THUMBNAIL_BACKGROUND_NOTE in adopt.bootstrap(ir, tmp_path / "tree" / "main.tex")


def test_adopt_reports_what_the_thumbnails_stood_in_for(tmp_path, monkeypatch, fetcher):
    """A deck handed over as files without its pictures: found["pictures_from_thumbnail"] names
    the picture, found["pictures_missing"] does not."""
    from beamer2slides import deck_files
    box = [200, 100, 360, 220]
    pres = deck(turned_pe("im", box, 30, "https://lh7-rt.googleusercontent.com/cat=s2048"))
    folder = tmp_path / "files"
    (folder / "thumbnails").mkdir(parents=True)
    (folder / "presentation.json").write_text(json.dumps(pres), encoding="utf-8")
    Image.fromarray(rotated_thumbnail(30, box)).save(folder / "thumbnails" / "001.png")
    fetcher(refused)
    monkeypatch.setattr("beamer2slides.inverse.run_pull", lambda target, *a, **k: None)
    monkeypatch.setattr(adopt, "record_base", lambda *a, **k: None)
    monkeypatch.setenv("B2S_FONT_CACHE", str(tmp_path / "font-cache"))
    found: dict = {}
    lines: list = []
    with adopt.no_machine_fonts():
        adopt.cmd_adopt(str(folder), tmp_path / "tree" / "main.tex", tmp_path / "work", False, None, 1, None,
                        False, log=lines.append, found=found)
    assert found["pictures_missing"] == []
    assert found["pictures_from_thumbnail"] == [{"slide": 1, "alt": "Sand cat"}]
    assert any("came from Google's thumbnail" in str(line) for line in lines)
    assert found["offline"]["thumbnails"]["given"] and not found["offline"]["pictures"]["given"]
    assert found["offline"]["thumbnails"]["adds"] == deck_files.PARTS["thumbnails"]


# ---------------------------------------------------------------- adopt's report

def target_of(elements: list[dict]) -> dict:
    return {"slides": [{"elements": elements}]}


def test_pictures_missing_excludes_what_the_thumbnail_recovered(tmp_path):
    file = tmp_path / "thumb-x.png"
    file.write_bytes(b"\x89PNG\r\n")
    good = {"kind": "image", "alt": "recovered", "file": str(file), "format": "png", "sha1": "a",
            "picture_source": "thumbnail"}
    assert adopt.pictures_missing(target_of([good])) == []
    assert adopt.pictures_from_thumbnail(target_of([good])) == [{"slide": 1, "alt": "recovered"}]


def test_a_still_unusable_picture_stays_missing_and_unreported_as_recovered():
    bad = {"kind": "image", "alt": "gone", "file": None, "format": None}
    missing = adopt.pictures_missing(target_of([bad]))
    assert len(missing) == 1 and missing[0]["why"] == "the deck gave no file for it"
    assert adopt.pictures_from_thumbnail(target_of([bad])) == []


def test_report_lines_say_it_plainly():
    recovered = [{"slide": 3, "alt": "logo"}]
    lines = adopt.thumbnail_pictures_lines(recovered)
    assert any("Google's thumbnail" in line for line in lines)
    assert any("slide 3" in line and "logo" in line for line in lines)
    assert adopt.thumbnail_pictures_lines([]) == []
