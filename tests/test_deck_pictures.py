"""A live deck's pictures without fetching a URL (`deck_pictures`, `snapshot.upload_signatures`,
`Sync.merge_plan`): the pictures of a Drive .pptx export paired with the live objects, one export for
whatever the downloads missed, pictures this run uploaded signed from their files, and a sync that
reads only the pictures its plan depends on. No Google calls."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from googleapiclient.errors import HttpError

from beamer2slides import deck_pictures, merge, snapshot
from beamer2slides.deck_pictures import LivePictures, exported_pictures
from beamer2slides.emit_state import EmitState, SlideState
from beamer2slides.google_types import Files, Page, PageElement, Presentation, Request, as_json
from beamer2slides.json_types import JsonObject
from beamer2slides.sync import Sync
from beamer2slides.typing_compat import override

from . import sync_work
from .fake_google import Answer, Fetcher, NoDrive, NoFiles
from .json_reads import jarr, jat, jobj, jstr
from .test_base_storage import PID, FakeDrive, after_convert_without_writes, http_error, read_deck
from .test_sync import entry, image_readback, ours_entry, three_slides, triple, unit

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides.google_types import DriveService, ExportFile
    from beamer2slides.snapshot import WrittenSlide

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
EMU = 12700


def png(size: tuple[int, int] = (40, 20), mark: tuple[int, int, int, int] = (5, 5, 15, 15),
        colour: str = "black") -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).rectangle(mark, fill=colour)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def pic(title: str | None = None, rid: str | None = None) -> str:
    t = f' title="{title}"' if title else ""
    blip = f'<p:blipFill><a:blip r:embed="{rid}"/></p:blipFill>' if rid else ""
    return f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="x"{t}/></p:nvPicPr>{blip}</p:pic>'


def sp(title: str | None = None) -> str:
    t = f' title="{title}"' if title else ""
    return f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="y"{t}/></p:nvSpPr></p:sp>'


def pptx(slides: list[tuple[str, dict[str, bytes], bytes | None]]) -> bytes:
    """A .pptx of slides, each (spTree inner xml, {rId: picture bytes}, background picture or None)."""
    buf = io.BytesIO()
    ns = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'
    with zipfile.ZipFile(buf, "w") as z:
        ids = "".join(f'<p:sldId id="{256 + n}" r:id="rId{n + 10}"/>' for n in range(len(slides)))
        z.writestr("ppt/presentation.xml", f"<p:presentation {ns}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>")
        rels = "".join(f'<Relationship Id="rId{n + 10}" Target="slides/slide{n + 1}.xml"/>' for n in range(len(slides)))
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{REL}">{rels}</Relationships>')
        for n, (tree, media, bg) in enumerate(slides, 1):
            media = dict(media)
            back = ""
            if bg is not None:
                media["rIdBg"] = bg
                back = '<p:bg><p:bgPr><a:blipFill><a:blip r:embed="rIdBg"/></a:blipFill></p:bgPr></p:bg>'
            z.writestr(f"ppt/slides/slide{n}.xml", f"<p:sld {ns}><p:cSld>{back}<p:spTree>{tree}</p:spTree></p:cSld></p:sld>")
            rels = ""
            for rid, data in media.items():
                z.writestr(f"ppt/media/s{n}-{rid}.png", data)
                rels += f'<Relationship Id="{rid}" Target="../media/s{n}-{rid}.png"/>'
            z.writestr(f"ppt/slides/_rels/slide{n}.xml.rels", f'<Relationships xmlns="{REL}">{rels}</Relationships>')
    return buf.getvalue()


def image(oid: str, title: str | None = None, size: tuple[float, float] = (40, 20),
          url: str | None = None) -> PageElement:
    e = PageElement(objectId=oid, image={"contentUrl": url or f"u-{oid}"},
                    size={"width": {"magnitude": size[0] * EMU, "unit": "EMU"},
                          "height": {"magnitude": size[1] * EMU, "unit": "EMU"}})
    if title is not None:
        e["title"] = title
    return e


def shape(oid: str) -> PageElement:
    return PageElement(objectId=oid, shape={})


def page(sid: str, elements: list[PageElement], background: bool = False) -> Page:
    p = Page(objectId=sid, pageElements=elements)
    if background:
        p["pageProperties"] = {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": f"u-{sid}"}}}
    return p


# ---------------------------------------------------------------- the export

def test_an_export_pairs_its_objects_with_the_live_ones_in_order() -> None:
    """Ids are lost in an export, but a page's objects come in drawing order (a group before its
    children) and a background is the page's own."""
    one, two, bg = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18)), png(size=(160, 90))
    group = PageElement(objectId="G", elementGroup={"children": [as_json(image("inner"), "inner")]})
    pres = Presentation(slides=[page("S1", [shape("t"), image("a"), group], background=True),
                                page("S2", [image("b")])])
    tree1 = sp() + pic(rid="r1") + f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="g"/></p:nvGrpSpPr>{pic(rid="r2")}</p:grpSp>'
    data = pptx([(tree1, {"r1": one, "r2": two}, bg), (pic(rid="r1"), {"r1": two}, None)])
    assert exported_pictures(data, pres, None) == {"a": one, "inner": two, "S1": bg, "b": two}
    assert exported_pictures(data, pres, {"b"}) == {"b": two}


def test_a_page_whose_objects_do_not_line_up_is_paired_by_title() -> None:
    """A person's object the read does not match (another count, another title in its place)
    leaves the order unproven: only unique titles pair then, and an untitled picture stays unread."""
    one, two = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18))
    b = image("b")
    pres = Presentation(slides=[page("S1", [image("a", "b2s:s/image/0"), b, image("c", "b2s:s/image/2")])])
    tree = pic("b2s:s/image/2", "r2") + pic(None, "r1") + sp() + pic("b2s:s/image/0", "r1")
    got = exported_pictures(pptx([(tree, {"r1": one, "r2": two}, None)]), pres, None)
    assert got == {"a": one, "c": two}
    # a title twice on either side proves nothing
    b["title"] = "b2s:s/image/0"
    assert exported_pictures(pptx([(tree, {"r1": one, "r2": two}, None)]), pres, None) == {"c": two}


def test_pages_that_do_not_pair_up_give_nothing() -> None:
    """A slide added since the read shifts every page: none of them is trusted."""
    pres = Presentation(slides=[page("S1", [image("a")])])
    data = pptx([(pic(rid="r1"), {"r1": png()}, None), (pic(rid="r1"), {"r1": png()}, None)])
    assert exported_pictures(data, pres, None) == {}
    assert exported_pictures(b"not a zip", pres, None) == {}


class ExportingDrive(NoFiles, NoDrive):
    """A Drive client whose .pptx export answers `data` (or raises it), counting the exports."""

    def __init__(self, data: bytes | HttpError) -> None:
        self.data = data
        self.exports = 0

    @override
    def files(self) -> Files:
        return self

    @override
    def export_media(self, **kw: Unpack[ExportFile]) -> Request[bytes]:
        assert kw["mimeType"] == deck_pictures.PPTX_MIME
        self.exports += 1
        return Answer(self.data)


def refuse(url: str) -> bytes:
    raise PermissionError(url)


def test_what_no_download_brought_comes_out_of_one_export() -> None:
    one, two = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18))
    pres = Presentation(presentationId="P", slides=[page("S1", [image("a"), image("b")])])
    drive = ExportingDrive(pptx([(pic(rid="r1") + pic(rid="r2"), {"r1": one, "r2": two}, None)]))
    live = LivePictures(pres, drive, refuse, 8, None, None)
    assert live.get(["a"]) == {"a": one}
    assert live.get(["b", "a"]) == {"b": two, "a": one}
    assert drive.exports == 1 and live.exports == 1 and live.downloads == 2
    # where the downloads are allowed, no export is made
    drive.exports = 0

    def download(url: str) -> bytes:
        return {"u-a": one, "u-b": two}[url]
    live = LivePictures(pres, drive, download, 8, None, None)
    assert live.get(["a", "b"]) == {"a": one, "b": two} and drive.exports == 0


def test_an_export_drive_refuses_is_a_missing_picture() -> None:
    pres = Presentation(presentationId="P", slides=[page("S1", [image("a")])])
    drive = ExportingDrive(http_error(403))
    live = LivePictures(pres, drive, refuse, 8, None, None)
    assert live.get(["a"]) == {} and live.get(["a"]) == {} and live.exports == 1
    assert LivePictures(pres, None, refuse, 8, None, None).get(["a"]) == {}


# ---------------------------------------------------------------- pictures this run uploaded

def test_an_uploaded_picture_is_signed_from_its_file_while_the_deck_has_its_shape(tmp_path: Path) -> None:
    f = tmp_path / "f.png"
    f.write_bytes(png(size=(40, 20)))
    assert snapshot.local_signature(f, (80, 40)) == snapshot.signature(f.read_bytes())
    assert snapshot.local_signature(f, (80, 40.3)) is not None          # (within 1%)
    assert snapshot.local_signature(f, (80, 44)) is None                # Google may have resampled it
    assert snapshot.local_signature(tmp_path / "missing.png", (80, 40)) is None
    pres = Presentation(pageSize={"width": {"magnitude": 160 * EMU, "unit": "EMU"},
                                  "height": {"magnitude": 80 * EMU, "unit": "EMU"}},
                        slides=[page("S1", [image("a", size=(40, 20)), image("b", size=(40, 30)), shape("t")])])
    got = snapshot.upload_signatures(pres, {"a": f, "b": f, "t": f, "S1": f})
    assert set(got) == {"a", "S1"}


def test_convert_signs_what_it_uploaded_without_downloading_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                               fetcher: Fetcher) -> None:
    """Every picture convert put in the deck came from a file of its own folder; only one whose
    live shape differs from its file's is downloaded."""
    from beamer2slides import google_auth
    (tmp_path / "figures").mkdir()
    (tmp_path / "backgrounds").mkdir()
    (tmp_path / "figures" / "f.png").write_bytes(png(size=(40, 20)))
    (tmp_path / "figures" / "g.png").write_bytes(png(size=(40, 20), mark=(20, 2, 38, 18)))
    (tmp_path / "backgrounds" / "b.png").write_bytes(png(size=(160, 90)))
    pres = read_deck()
    pres["pageSize"] = {"width": {"magnitude": 320 * EMU, "unit": "EMU"}, "height": {"magnitude": 180 * EMU, "unit": "EMU"}}
    pres["slides"] = [page("S1", [image("A", size=(80, 40)), image("B", size=(80, 50))], background=True)]
    deck: JsonObject = {"slides": [{"background": "backgrounds/b.png",
                                    "elements": [{"kind": "image", "file": "figures/f.png"},
                                                 {"kind": "image", "file": "figures/g.png"}]}]}
    state = EmitState(presentation_id=PID, url="u", scale=1.0,
                      slides=(SlideState(page=0, object_id="S1", elements=("A", "B"), objects=None, groups=None,
                                         table_margins=None),),
                      contained=None, theme=None, previous=None)
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return png(size=(80, 50))
    fetcher(fetch)
    seen: list[Mapping[str, str] | None] = []

    def read() -> Presentation:
        return pres
    slides = after_convert_without_writes(monkeypatch, read, PID)

    def converted_base(deck: JsonObject, out: Path, pres: Presentation, written: Sequence[WrittenSlide],
                       scale: float | None, pdf: Path | str | JsonObject | None, sign: bool, overlays: str,
                       signatures: Mapping[str, str] | None) -> JsonObject:
        seen.append(signatures)
        return {"slides": [], "generation": 0, "presentationId": PID}
    monkeypatch.setattr(snapshot, "converted_base", converted_base)
    with google_auth.use_services({"slides": slides, "drive": FakeDrive({}, {}, False)}):
        snapshot.snapshot_after_convert(deck, tmp_path, state, {"pdf": "x", "sha1": None}, "last", [])
    assert asked == ["u-B"]
    sigs = seen[-1]           # (the base stored last)
    assert sigs is not None
    assert sigs["A"] == snapshot.signature((tmp_path / "figures" / "f.png").read_bytes())
    assert sigs["S1"] == snapshot.signature((tmp_path / "backgrounds" / "b.png").read_bytes())
    assert sigs["B"] == snapshot.signature(png(size=(80, 50)))


# ---------------------------------------------------------------- a sync reads what its plan depends on

def picture_sync(monkeypatch: pytest.MonkeyPatch, ours_bbox: list[float] | None,
                 drive: DriveService) -> tuple[Sync, JsonObject, Presentation]:
    """A sync of three slides, the second with a figure whose contentUrl Google changed although
    nobody touched it; the source moves the figure to `ours_bbox` (None: leaves it alone). `drive`:
    the Drive the sync may export the deck from."""
    base = three_slides()
    figure: JsonObject = {"id": "p1f0", "kind": "image", "role": "figure", "bbox": [200, 60, 300, 160], "file": None}
    el = entry("image/figure/0", figure, "b2s_s001_f2")
    rb = image_readback([400, 120, 600, 320], "aaa")
    jobj(rb, "image")["signature"] = snapshot.signature(png())
    jobj(el, "readback")["b2s_s001_f2"] = rb
    jarr(base, "slides", 1, "elements").append(el)
    ours, theirs = triple(base)
    if ours_bbox:
        jarr(ours, "slides", 1, "elements")[2] = ours_entry("image/figure/0", {**figure, "bbox": [*ours_bbox]})
    jobj(theirs, "slides", 1, "objects", "b2s_s001_f2")["image"] = {"contentHash": "bbb"}
    pres = Presentation(presentationId="P",
                        slides=[page(jstr(s, "objectId"),[image("b2s_s001_f2", url="u-new")] if n == 1 else [])
                                for n, s in enumerate(jarr(base, "slides"))])
    s = sync_work.bare_sync()
    s.base, s.ours, s.drive = base, ours, drive
    s.follow_labels, s.take_source = False, ()

    def no_adopter(self: Sync, pres: Presentation) -> None:
        return None
    monkeypatch.setattr(Sync, "picture_adopter", no_adopter)
    return s, theirs, pres


def test_a_picture_the_source_left_alone_is_never_read(monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> None:
    """Its unit is kept whatever the deck did to it, so its new URL is not worth a download - and
    an unread picture is not reported as the person's edit."""
    def no_download(url: str) -> bytes:
        pytest.fail(f"downloaded {url}")
    fetcher(no_download)
    s, theirs, pres = picture_sync(monkeypatch, None, NoDrive())
    mplan = merge.merge_plan_json(s.merge_plan(theirs, pres))
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert s.picture_reads == {"unchecked": 1, "asked": 0}
    assert not jat(mplan, "report", "overrides") and not jat(mplan, "report", "conflicts")


def test_a_picture_the_source_moved_is_read_and_found_unchanged(monkeypatch: pytest.MonkeyPatch,
                                                                fetcher: Fetcher) -> None:
    def download(url: str) -> bytes:
        return png()
    fetcher(download)
    s, theirs, pres = picture_sync(monkeypatch, [200, 60, 320, 160], NoDrive())
    mplan = merge.merge_plan_json(s.merge_plan(theirs, pres))
    assert unit(mplan, "results", "image/figure/0")["action"] != "keep"
    assert s.picture_reads == {"unchecked": 1, "asked": 1} and not jat(mplan, "report", "conflicts")


def test_an_unread_picture_the_source_moved_is_the_persons(monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> None:
    """Neither a download nor an export: the new URL reads as a replaced picture, which is kept."""
    fetcher(refuse)
    s, theirs, pres = picture_sync(monkeypatch, [200, 60, 320, 160], ExportingDrive(http_error(403)))
    mplan = merge.merge_plan_json(s.merge_plan(theirs, pres))
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert jat(mplan, "report", "conflicts", 0, "field") == "image"


def test_a_sync_that_may_not_download_reads_the_picture_out_of_an_export(monkeypatch: pytest.MonkeyPatch,
                                                                         fetcher: Fetcher) -> None:
    fetcher(refuse)
    drive = ExportingDrive(pptx([("", {}, None), (pic(rid="r1"), {"r1": png()}, None), ("", {}, None)]))
    s, theirs, pres = picture_sync(monkeypatch, [200, 60, 320, 160], drive)
    mplan = merge.merge_plan_json(s.merge_plan(theirs, pres))
    assert unit(mplan, "results", "image/figure/0")["action"] != "keep" and not jat(mplan, "report", "conflicts")
    assert drive.exports == 1


def test_an_unread_picture_is_not_reported_as_an_edit() -> None:
    """merge.pictures_unchecked: a kept unit's new URL, never signed, is left out of the overrides;
    one the person really replaced (signed, different) is in them."""
    from beamer2slides.sync_model import ObjectId, slide_read
    rb = image_readback([0, 0, 10, 10], "b")
    read: JsonObject = {"objectId": "S", "objects": {"x": rb}, "background": {"picture": "p"}}
    jobj(rb, "image")["unchecked"] = True
    x = ObjectId("x")
    assert merge.pictures_unchecked(slide_read(read, "read"), [x])
    assert not merge.background_unchecked(slide_read(read, "read"))
    jobj(rb, "image").pop("unchecked")
    assert not merge.pictures_unchecked(slide_read(read, "read"), [x])
