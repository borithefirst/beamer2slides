"""A live deck's pictures without fetching a URL (`deck_pictures`, `snapshot.upload_signatures`,
`Sync.merge_plan`): the pictures of a Drive .pptx export paired with the live objects, one export for
whatever the downloads missed, pictures this run uploaded signed from their files, and a sync that
reads only the pictures its plan depends on. No Google calls."""

import io
import zipfile

import pytest

from beamer2slides import deck_pictures, merge, snapshot
from beamer2slides.deck_pictures import LivePictures, exported_pictures

from .test_base_storage import FakeDrive, http_error, read_deck
from .test_guard import Request
from .test_sync import entry, ours_entry, readback, three_slides, triple, unit

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
EMU = 12700


def png(size=(40, 20), mark=(5, 5, 15, 15), colour="black") -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).rectangle(mark, fill=colour)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def pic(title=None, rid=None) -> str:
    t = f' title="{title}"' if title else ""
    blip = f'<p:blipFill><a:blip r:embed="{rid}"/></p:blipFill>' if rid else ""
    return f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="x"{t}/></p:nvPicPr>{blip}</p:pic>'


def sp(title=None) -> str:
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


def image(oid, title=None, size=(40, 20), url=None):
    return {"objectId": oid, "title": title, "image": {"contentUrl": url or f"u-{oid}"},
            "size": {"width": {"magnitude": size[0] * EMU, "unit": "EMU"},
                     "height": {"magnitude": size[1] * EMU, "unit": "EMU"}}}


def shape(oid, title=None):
    return {"objectId": oid, "title": title, "shape": {}}


def page(sid, elements, background=False):
    p = {"objectId": sid, "pageElements": elements}
    if background:
        p["pageProperties"] = {"pageBackgroundFill": {"stretchedPictureFill": {"contentUrl": f"u-{sid}"}}}
    return p


# ---------------------------------------------------------------- the export

def test_an_export_pairs_its_objects_with_the_live_ones_in_order():
    """Ids are lost in an export, but a page's objects come in drawing order (a group before its
    children) and a background is the page's own."""
    one, two, bg = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18)), png(size=(160, 90))
    group = {"objectId": "G", "elementGroup": {"children": [image("inner")]}}
    pres = {"slides": [page("S1", [shape("t"), image("a"), group], background=True), page("S2", [image("b")])]}
    tree1 = sp() + pic(rid="r1") + f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="g"/></p:nvGrpSpPr>{pic(rid="r2")}</p:grpSp>'
    data = pptx([(tree1, {"r1": one, "r2": two}, bg), (pic(rid="r1"), {"r1": two}, None)])
    assert exported_pictures(data, pres) == {"a": one, "inner": two, "S1": bg, "b": two}
    assert exported_pictures(data, pres, wanted={"b"}) == {"b": two}


def test_a_page_whose_objects_do_not_line_up_is_paired_by_title():
    """A person's object the read does not match (another count, another title in its place)
    leaves the order unproven: only unique titles pair then, and an untitled picture stays unread."""
    one, two = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18))
    pres = {"slides": [page("S1", [image("a", "b2s:s/image/0"), image("b"), image("c", "b2s:s/image/2")])]}
    tree = pic("b2s:s/image/2", "r2") + pic(None, "r1") + sp() + pic("b2s:s/image/0", "r1")
    got = exported_pictures(pptx([(tree, {"r1": one, "r2": two}, None)]), pres)
    assert got == {"a": one, "c": two}
    # a title twice on either side proves nothing
    pres["slides"][0]["pageElements"][1]["title"] = "b2s:s/image/0"
    assert exported_pictures(pptx([(tree, {"r1": one, "r2": two}, None)]), pres) == {"c": two}


def test_pages_that_do_not_pair_up_give_nothing():
    """A slide added since the read shifts every page: none of them is trusted."""
    pres = {"slides": [page("S1", [image("a")])]}
    data = pptx([(pic(rid="r1"), {"r1": png()}, None), (pic(rid="r1"), {"r1": png()}, None)])
    assert exported_pictures(data, pres) == {}
    assert exported_pictures(b"not a zip", pres) == {}


class ExportingDrive:
    def __init__(self, data):
        self.data, self.exports = data, 0

    def files(self):
        return self

    def export_media(self, fileId, mimeType):
        assert mimeType == deck_pictures.PPTX_MIME
        self.exports += 1
        return Request(self.data)


def refuse(url):
    raise PermissionError(url)


def test_what_no_download_brought_comes_out_of_one_export():
    one, two = png(mark=(1, 1, 9, 9)), png(mark=(20, 2, 38, 18))
    pres = {"presentationId": "P", "slides": [page("S1", [image("a"), image("b")])]}
    drive = ExportingDrive(pptx([(pic(rid="r1") + pic(rid="r2"), {"r1": one, "r2": two}, None)]))
    live = LivePictures(pres, drive, refuse)
    assert live.get(["a"]) == {"a": one}
    assert live.get(["b", "a"]) == {"b": two, "a": one}
    assert drive.exports == 1 and live.exports == 1 and live.downloads == 2
    # where the downloads are allowed, no export is made
    drive.exports = 0
    live = LivePictures(pres, drive, lambda url: {"u-a": one, "u-b": two}[url])
    assert live.get(["a", "b"]) == {"a": one, "b": two} and drive.exports == 0


def test_an_export_drive_refuses_is_a_missing_picture():
    pres = {"presentationId": "P", "slides": [page("S1", [image("a")])]}
    drive = ExportingDrive(http_error(403))
    live = LivePictures(pres, drive, refuse)
    assert live.get(["a"]) == {} and live.get(["a"]) == {} and live.exports == 1
    assert LivePictures(pres, None, refuse).get(["a"]) == {}


# ---------------------------------------------------------------- pictures this run uploaded

def test_an_uploaded_picture_is_signed_from_its_file_while_the_deck_has_its_shape(tmp_path):
    f = tmp_path / "f.png"
    f.write_bytes(png(size=(40, 20)))
    assert snapshot.local_signature(f, (80, 40)) == snapshot.signature(f.read_bytes())
    assert snapshot.local_signature(f, (80, 40.3)) is not None          # (within 1%)
    assert snapshot.local_signature(f, (80, 44)) is None                # Google may have resampled it
    assert snapshot.local_signature(tmp_path / "missing.png", (80, 40)) is None
    pres = {"pageSize": {"width": {"magnitude": 160 * EMU, "unit": "EMU"}, "height": {"magnitude": 80 * EMU, "unit": "EMU"}},
            "slides": [page("S1", [image("a", size=(40, 20)), image("b", size=(40, 30)), shape("t")])]}
    got = snapshot.upload_signatures(pres, {"a": f, "b": f, "t": f, "S1": f})
    assert set(got) == {"a", "S1"}


def test_convert_signs_what_it_uploaded_without_downloading_it(monkeypatch, tmp_path, fetcher):
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
    deck = {"slides": [{"background": "backgrounds/b.png",
                        "elements": [{"kind": "image", "file": "figures/f.png"}, {"kind": "image", "file": "figures/g.png"}]}]}
    state = {"presentationId": pres["presentationId"], "slides": [{"objectId": "S1", "elements": ["A", "B"]}]}
    asked = []
    fetcher(lambda url: asked.append(url) or png(size=(80, 50)))
    seen = {}
    monkeypatch.setattr(snapshot, "build_base", lambda *a, **k: seen.update(k) or {"slides": [], "generation": 0,
                                                                            "presentationId": pres["presentationId"]})
    monkeypatch.setattr(snapshot, "write_tags", lambda *a: ([], None))
    monkeypatch.setattr("beamer2slides.theme_sync.record", lambda *a: None)
    slides = type("S", (), {"presentations": lambda self: self,
                            "get": lambda self, presentationId: FakeDrive._request(pres)})()
    with google_auth.use_services({"slides": slides, "drive": FakeDrive()}):
        snapshot.snapshot_after_convert(deck, tmp_path, state, {"pdf": "x", "sha1": None}, problems=[])
    assert asked == ["u-B"]
    sigs = seen["signatures"]
    assert sigs["A"] == snapshot.signature((tmp_path / "figures" / "f.png").read_bytes())
    assert sigs["S1"] == snapshot.signature((tmp_path / "backgrounds" / "b.png").read_bytes())
    assert sigs["B"] == snapshot.signature(png(size=(80, 50)))


# ---------------------------------------------------------------- a sync reads what its plan depends on

def picture_sync(monkeypatch, ours_bbox=None, drive=None):
    """A sync of three slides, the second with a figure whose contentUrl Google changed although
    nobody touched it; the source moves the figure to `ours_bbox` (None: leaves it alone)."""
    from beamer2slides.sync import Sync
    base = three_slides()
    figure = {"id": "p1f0", "kind": "image", "role": "figure", "bbox": [200, 60, 300, 160], "file": None}
    el = entry("image/figure/0", figure, "b2s_s001_f2")
    el["readback"]["b2s_s001_f2"] = readback([400, 120, 600, 320], kind="image", image="aaa")
    el["readback"]["b2s_s001_f2"]["image"]["signature"] = snapshot.signature(png())
    base["slides"][1]["elements"].append(el)
    ours, theirs = triple(base)
    if ours_bbox:
        ours["slides"][1]["elements"][2] = ours_entry("image/figure/0", {**figure, "bbox": ours_bbox})
    theirs["slides"][1]["objects"]["b2s_s001_f2"]["image"] = {"contentHash": "bbb"}
    pres = {"presentationId": "P", "slides": [page(s["objectId"], [image("b2s_s001_f2", url="u-new")] if n == 1 else [])
                                              for n, s in enumerate(base["slides"])]}
    s = Sync.__new__(Sync)
    s.base, s.ours, s.drive, s.follow_labels, s.take_source = base, ours, drive, False, ()
    s.plan = object()  # (a real Sync holds its DeckPlan there: no method may be called that)
    monkeypatch.setattr(Sync, "picture_adopter", lambda self, pres: None)
    return s, theirs, pres


def test_a_picture_the_source_left_alone_is_never_read(monkeypatch, fetcher):
    """Its unit is kept whatever the deck did to it, so its new URL is not worth a download - and
    an unread picture is not reported as the person's edit."""
    fetcher(lambda url: pytest.fail(f"downloaded {url}"))
    s, theirs, pres = picture_sync(monkeypatch)
    mplan = s.merge_plan(theirs, pres)
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert s.picture_reads == {"unchecked": 1, "asked": 0}
    assert not mplan["report"]["overrides"] and not mplan["report"]["conflicts"]


def test_a_picture_the_source_moved_is_read_and_found_unchanged(monkeypatch, fetcher):
    fetcher(lambda url: png())
    s, theirs, pres = picture_sync(monkeypatch, ours_bbox=[200, 60, 320, 160])
    mplan = s.merge_plan(theirs, pres)
    assert unit(mplan, "results", "image/figure/0")["action"] != "keep"
    assert s.picture_reads == {"unchecked": 1, "asked": 1} and not mplan["report"]["conflicts"]


def test_an_unread_picture_the_source_moved_is_the_persons(monkeypatch, fetcher):
    """Neither a download nor an export: the new URL reads as a replaced picture, which is kept."""
    fetcher(refuse)
    s, theirs, pres = picture_sync(monkeypatch, ours_bbox=[200, 60, 320, 160])
    mplan = s.merge_plan(theirs, pres)
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["field"] == "image"


def test_a_sync_that_may_not_download_reads_the_picture_out_of_an_export(monkeypatch, fetcher):
    fetcher(refuse)
    drive = ExportingDrive(pptx([("", {}, None), (pic(rid="r1"), {"r1": png()}, None), ("", {}, None)]))
    s, theirs, pres = picture_sync(monkeypatch, ours_bbox=[200, 60, 320, 160], drive=drive)
    mplan = s.merge_plan(theirs, pres)
    assert unit(mplan, "results", "image/figure/0")["action"] != "keep" and not mplan["report"]["conflicts"]
    assert drive.exports == 1


def test_an_unread_picture_is_not_reported_as_an_edit():
    """merge.unchecked: a kept unit's new URL, never signed, is left out of the overrides; one
    the person really replaced (signed, different) is in them."""
    read = {"objects": {"x": {"image": {"contentHash": "b", "unchecked": True}}}, "background": {"picture": "p"}}
    assert merge.unchecked(read, ["x"]) and not merge.unchecked(read)
    read["objects"]["x"]["image"].pop("unchecked")
    assert not merge.unchecked(read, ["x"])
