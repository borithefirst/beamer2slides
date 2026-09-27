"""A picture download that comes back as something other than a decodable image - a Google
sign-in page for a Drive-hosted link, a site's own 404/Next.js page instead of the asset, a fetch
the sandbox simply refuses - never becomes text or is silently dropped: `deck_fills.recover_pictures`
crops the slide's own thumbnail for it (`adopt.pictures_from_thumbnail` reports the swap; what stays
truly unreadable is still `adopt.pictures_missing`, `deck_ir.stash_picture`'s `error`)."""

from pathlib import Path

import numpy as np
import pytest

from beamer2slides import adopt, deck_fills
from beamer2slides.deck_ir import deck_ir

EMU = 12700
HTML_SIGNIN = b"<!doctype html><html><head><title>Sign in - Google Accounts</title></head></html>"
HTML_404 = b"<!DOCTYPE html><html data-dpl-id=\"x\" id=\"__next_error__\"><title>Not found</title></html>"


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


@pytest.mark.parametrize("extra", [{"video": {"source": "YOUTUBE", "id": "y"}}, {"chart": {"chartId": "1"}},
                                   {"rotation": 12.0}, {"flip": True}])
def test_what_is_never_recovered_this_way(extra, tmp_path):
    """A video's poster frame and a linked chart have their own thumbnail fallback already; a
    rotated or flipped picture can't be, since the crop is axis-aligned."""
    thumb = page()
    thumb[100:200, 100:300] = [30, 60, 90]
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "id": "im", "object": "im", "group": None,
          "error": "refused", **extra}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert "picture_source" not in el and el.get("error") == "refused"


def test_a_good_picture_is_left_alone(tmp_path):
    thumb = page()
    el = {"kind": "image", "bbox": [100, 100, 300, 200], "id": "im", "object": "im", "group": None,
          "file": "ok.png", "format": "png", "sha1": "abc"}
    deck_fills.recover_pictures([el], thumb, 1.0, tmp_path)
    assert el["file"] == "ok.png" and "picture_source" not in el


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
