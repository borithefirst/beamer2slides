"""`net`: every download of content the library did not write goes through one door, and a caller
(`google_auth.use_fetcher`, `AgentContext.fetch_google_content`) may own it.

The sites are the ones a backend's egress review found: picture signatures (`snapshot`, in
test_base_storage.py), thumbnails (`gslides.save_thumbnail`), a read deck's pictures and a
picture's original `source_url` (`deck_ir.fetch_url`, `inverse`), a Doc's inserted pictures
(`doc_sync.fetch_pictures`). Each is driven here through an installed fetcher, with urllib made
to fail the test if it is ever reached.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from beamer2slides import google_auth, net
from beamer2slides.doc_ir import Ir, Run
from beamer2slides.google_types import Files, Pages, Presentations, SlidesRequest, Thumbnail
from beamer2slides.json_types import JsonObject
from beamer2slides.typing_compat import override

from .fake_google import Answer, Fetcher, NoDrive, NoFiles, NoPages, NoPresentations, NoSlides

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from beamer2slides import google_types
    from beamer2slides.google_types import ExportFile, GetThumbnail, SlidesService

Colour = tuple[int, int, int]
SQUARE = (8, 8)
RED: Colour = (200, 30, 30)


def _no_urllib(url: str) -> bytes:
    pytest.fail(f"urllib fetched {url}")


def _no_sleep(seconds: float) -> None:
    pass


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net, "urllib_fetch", _no_urllib)
    monkeypatch.setattr(net.time, "sleep", _no_sleep)


def png(*, size: tuple[int, int], colour: Colour) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_nothing_installed_is_urllib() -> None:
    assert google_auth.fetcher_for_threads() is net.urllib_fetch


def test_downloads_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`$B2S_NO_DOWNLOADS` (the CLI's `--no-downloads`) makes `no_downloads` the default: asked
    once, never retried. A fetcher a caller installed still wins."""
    monkeypatch.setenv(net.NO_DOWNLOADS, "0")
    assert not net.downloads_off()
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")
    assert google_auth.fetcher_for_threads() is net.no_downloads and net.downloads_off()
    with pytest.raises(PermissionError, match="switched off"):
        net.download("u", None, net.TRIES)
    with google_auth.use_fetcher(lambda url: b"mine"):
        assert net.download("u", None, net.TRIES) == b"mine" and not net.downloads_off()


def test_with_downloads_off_no_place_is_measured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Measuring a hole's place is a thumbnail download: with downloads off not even the scratch
    slides are made, and every picture keeps its predicted place."""
    from beamer2slides import emit, emit_places

    def measure_jobs(*a: object) -> tuple[list[SlidesRequest], list[emit_places.ScratchJob]]:
        return [{"createSlide": {"objectId": "b2s_m000"}}], [emit_places.ScratchJob(slide="b2s_m000", page=0, gaps=(),
                                                                                     overlays=())]

    def batch(*a: object) -> NoReturn:
        pytest.fail("scratch slides written")

    def placed(e: JsonObject, page: int) -> JsonObject:
        return e

    monkeypatch.setattr(emit_places, "measure_jobs", measure_jobs)
    monkeypatch.setattr(emit_places, "batch", batch)
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")
    assert emit.measure_places(NoSlides(), "P", {"slides": []}, 1.0, emit.FontMapper(), placed, {},
                               tmp_path, emit.SLIDE_W) == ({}, [])


def test_a_download_is_retried_and_the_last_failure_raised() -> None:
    tries: list[str] = []

    def flaky(url: str) -> bytes:
        tries.append(url)
        if len(tries) < 3:
            raise ConnectionError("blip")
        return b"ok"

    assert net.download("u", flaky, tries=3) == b"ok" and len(tries) == 3
    tries.clear()

    def failing(url: str) -> bytes:
        tries.append(url)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        net.download("u", failing, tries=2)
    assert len(tries) == 2


def test_not_allowed_is_asked_once() -> None:
    tries: list[str] = []

    def forbid(url: str) -> bytes:
        tries.append(url)
        raise PermissionError(url)

    with pytest.raises(PermissionError):
        net.download("u", forbid, tries=5)
    assert tries == ["u"]


class Handing:
    """A harness's fetcher, typed to hand back bytes: what it really hands back is `answer`, which
    a test replaces (`monkeypatch`) with what a harness breaking its word would give."""

    def __init__(self) -> None:
        self.answer = b""

    def __call__(self, url: str) -> bytes:
        return self.answer


def test_a_fetcher_must_hand_back_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = Handing()
    monkeypatch.setattr(fetch, "answer", "text")
    with pytest.raises(TypeError, match="not bytes"):
        net.download("u", fetch, tries=1)
    monkeypatch.setattr(fetch, "answer", bytearray(b"ab"))
    assert net.download("u", fetch, tries=1) == b"ab"


def test_the_installed_fetcher_is_used_when_none_is_passed(fetcher: Fetcher) -> None:
    fetcher(lambda url: f"<{url}>".encode())
    assert net.download("u", None, net.TRIES) == b"<u>"


@pytest.mark.parametrize("data, suffix", [(png(size=SQUARE, colour=RED), ".png"), (b"\xff\xd8\xff\xe0rest", ".jpg"),
                                          (b"GIF89a...", ".gif"), (b"RIFF\0\0\0\0WEBPVP8 ", ".webp"),
                                          (b"<?xml version='1.0'?><svg/>", ".svg"), (b"??", ".png")])
def test_a_picture_names_its_own_type(data: bytes, suffix: str) -> None:
    assert net.picture_suffix(data, ".png") == suffix


class ThumbnailPages(NoPages):
    """Every thumbnail is at https://thumb/1, 1600 x 900."""

    @override
    def getThumbnail(self, **kw: Unpack[GetThumbnail]) -> google_types.Request[Thumbnail]:
        return Answer(Thumbnail(contentUrl="https://thumb/1", width=1600, height=900))


class ThumbnailSlides(NoPresentations, NoSlides):
    """A Slides client whose pages are `ThumbnailPages`."""

    @override
    def presentations(self) -> Presentations:
        return self

    @override
    def pages(self) -> Pages:
        return ThumbnailPages()


def test_a_thumbnail_is_downloaded_through_the_fetcher(tmp_path: Path, fetcher: Fetcher) -> None:
    from beamer2slides.gslides import save_thumbnail

    def thumb(url: str) -> bytes:
        assert url == "https://thumb/1", url
        return png(size=SQUARE, colour=RED)

    slides: SlidesService = ThumbnailSlides()
    fetcher(thumb)
    assert save_thumbnail(slides, "pid", "p1", tmp_path / "t" / "1.png", fetch=None) == (1600, 900)
    assert (tmp_path / "t" / "1.png").read_bytes() == png(size=SQUARE, colour=RED)
    assert not list((tmp_path / "t").glob("*.part"))


def test_a_read_deck_s_pictures_come_through_the_fetcher(tmp_path: Path, fetcher: Fetcher) -> None:
    from beamer2slides import deck_ir

    fetcher(lambda url: png(size=SQUARE, colour=RED))
    got = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path)
    assert got.format and got.file is not None
    assert (tmp_path / got.file).read_bytes() == png(size=SQUARE, colour=RED)

    class Refused(Exception):
        pass

    def refuse(url: str) -> bytes:
        raise Refused("egress denied")

    fetcher(refuse)
    error = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path).error
    assert error is not None and "egress denied" in error


def _s15f16(x: float) -> bytes:
    import struct
    return struct.pack(">i", round(x * 65536))


def _xyztype(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\x00" * 4 + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _curv_gamma(gamma: float) -> bytes:
    import struct
    data = b"curv" + b"\x00" * 4 + struct.pack(">I", 1) + struct.pack(">H", round(gamma * 256))
    while len(data) % 4:
        data += b"\x00"
    return data


def icc_gamma_profile(gamma: float) -> bytes:
    """A minimal, valid ICC v2 RGB matrix/TRC profile (lcms2 loads it) whose channels are a plain
    `gamma`, not sRGB's own curve - just enough to prove a picture's embedded profile is applied and
    not silently ignored (deck_ir.srgb_bytes, defect A: firebase-jam slide 23's washed-out teal
    background - a Google-served picture's own "Display" ICC profile, applied, is the saturated
    colour the live thumbnail shows)."""
    import struct

    def desc_type(text: bytes) -> bytes:
        text = text + b"\x00"
        out = b"desc" + b"\x00" * 4 + struct.pack(">I", len(text)) + text
        out += struct.pack(">III", 0, 0, 0)[:8] + struct.pack(">H", 0) + struct.pack(">B", 0) + b"\x00" * 67
        while len(out) % 4:
            out += b"\x00"
        return out

    def text_type(text: bytes) -> bytes:
        out = b"text" + b"\x00" * 4 + text + b"\x00"
        while len(out) % 4:
            out += b"\x00"
        return out

    curve = _curv_gamma(gamma)
    tags = {
        "desc": desc_type(b"test"), "cprt": text_type(b"public domain"),
        "wtpt": _xyztype(0.9642, 1.0, 0.8249),
        "rXYZ": _xyztype(0.4360, 0.2225, 0.0139), "gXYZ": _xyztype(0.3851, 0.7169, 0.0971),
        "bXYZ": _xyztype(0.1431, 0.0606, 0.7139),
        "rTRC": curve, "gTRC": curve, "bTRC": curve,
    }
    order = ["desc", "cprt", "wtpt", "rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC"]
    offset = 128 + 4 + 12 * len(order)
    entries: list[tuple[bytes, int, int]] = []
    blob = b""
    written: dict[bytes, tuple[int, int]] = {}
    for name in order:
        data = tags[name]
        if data in written:
            off, sz = written[data]
        else:
            off, sz = offset, len(data)
            written[data] = (off, sz)
            blob += data
            offset += sz
        entries.append((name.encode(), off, sz))
    header = bytearray(128)
    struct.pack_into(">I", header, 0, 128 + 4 + 12 * len(order) + len(blob))
    header[8:12] = struct.pack(">I", 0x02100000)
    header[12:16], header[16:20], header[20:24], header[36:40] = b"mntr", b"RGB ", b"XYZ ", b"acsp"
    header[68:80] = _s15f16(0.9642) + _s15f16(1.0) + _s15f16(0.8249)
    table = struct.pack(">I", len(order))
    for sig, off, sz in entries:
        table += sig + struct.pack(">II", off, sz)
    return bytes(header) + table + blob


def png_with_icc(colour: Colour, icc: bytes) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buf, format="PNG", icc_profile=icc)
    return buf.getvalue()


def test_a_picture_s_embedded_colour_profile_is_applied_and_dropped(tmp_path: Path, fetcher: Fetcher) -> None:
    """firebase-jam slide 23's page-background picture (a `stretchedPictureFill`) carries a
    "Display" ICC profile: read raw, its teal is (94, 204, 209); colour-managed, Google's own
    saturated (16, 207, 211) - the live thumbnail's colour. LaTeX/PDF only ever draws the raw bytes
    of whatever `stash_picture` saves, so the conversion has to happen once, here, at fetch time
    (deck_ir.srgb_bytes)."""
    from beamer2slides import deck_ir

    icc = icc_gamma_profile(1.0)  # a plain gamma of 1.0: sRGB's own curve is not gamma 1.0
    data = png_with_icc((128, 128, 128), icc)

    fetcher(lambda url: data)
    got = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path)
    assert got.file is not None
    saved = (tmp_path / got.file).read_bytes()
    from PIL import Image
    import numpy as np
    out = Image.open(io.BytesIO(saved))
    assert out.info.get("icc_profile") is None                 # no profile left to (mis)apply again
    px = tuple(np.array(out.convert("RGB"))[0, 0])
    assert px != (128, 128, 128)                                # the profile was actually used
    assert px == (188, 188, 188)                                # deterministic: lcms2's own transform


def test_a_picture_with_no_colour_profile_is_untouched(tmp_path: Path, fetcher: Fetcher) -> None:
    from beamer2slides import deck_ir

    fetcher(lambda url: png(size=SQUARE, colour=RED))
    got = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path)
    assert got.file is not None
    assert (tmp_path / got.file).read_bytes() == png(size=SQUARE, colour=RED)


def test_a_picture_s_source_url_on_any_host_goes_through_the_fetcher(tmp_path: Path, fetcher: Fetcher) -> None:
    """The original of a picture inserted by URL: a host chosen by whoever inserted it, which is
    exactly the egress a backend has to see."""
    from beamer2slides.inverse import BEAMER_PT, Planner, Workspace, fresh_context, loop_picture, picture_look

    big, small = png(size=(64, 64), colour=RED), png(size=(16, 16), colour=RED)
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return big

    fetcher(fetch)
    cur = tmp_path / "cur.png"
    cur.write_bytes(small)
    # A planner holding only what `source_url_bytes` reads: its work folder and the notes.
    ws = Workspace.__new__(Workspace)
    ws.work = tmp_path
    planner = Planner.__new__(Planner)
    planner.ws, planner.ctx = ws, fresh_context(BEAMER_PT)
    te = loop_picture({"source_url": "https://example.org/figure.png"}, (0.0, 0.0, 1.0, 1.0), "test")
    data, _, _ = planner.source_url_bytes(te, small, "png", picture_look(cur))
    assert asked == ["https://example.org/figure.png"]
    assert data == big and planner.ctx.notes, "the larger original was taken"


def _runs_are(runs: list[Run]) -> object:
    """A stand-in for `doc_sync._pictures`: every picture run is one of `runs`."""
    def pictures(live: Ir) -> list[Run]:
        return runs
    return pictures


NO_DOCUMENT = Ir(blocks=[])   # (the picture runs come from `_runs_are`)


def _src(run: Run) -> str:
    """The file a picture run was saved to."""
    src = run.get("src")
    assert src is not None, f"{run.get('value')} was not saved"
    return src


def test_a_doc_s_inserted_pictures_come_through_the_fetcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            fetcher: Fetcher) -> None:
    """A fetcher hands over bytes and no Content-Type, so the file is named by what it holds."""
    from beamer2slides import doc_sync

    runs = [Run(text="", uri="https://lh7/one", value="kix.one"), Run(text="", uri="https://lh7/two", value="kix.two"),
            Run(text="", uri="https://lh7/gone", value="kix.gone")]
    monkeypatch.setattr(doc_sync, "_pictures", _runs_are(runs))
    answers = {"https://lh7/one": png(size=SQUARE, colour=RED), "https://lh7/two": b"\xff\xd8\xff\xe0jpeg"}

    def fetch(url: str) -> bytes:
        if url not in answers:
            raise LookupError(url)
        return answers[url]

    fetcher(fetch)
    assert doc_sync.fetch_pictures(tmp_path / "talk.html", NO_DOCUMENT, drive=None, ident=None) == 2
    assert runs[0].get("src") == "talk.media/kix.one.png" and runs[1].get("src") == "talk.media/kix.two.jpg"
    assert "src" not in runs[2]


def _doc_export(tags: Sequence[tuple[str, float, float]], files: Mapping[str, bytes]) -> NoDrive:
    """What Drive's zip export of a document holds: one HTML page and its images/."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("talk.html", "<html><body>" + "".join(
            f'<p><img alt="" src="{src}" style="width: {w}px; height: {h}px;"></p>'
            for src, w, h in tags) + "</body></html>")
        for name, data in files.items():
            z.writestr(name, data)

    class Exported(NoFiles):
        @override
        def export(self, **kw: Unpack[ExportFile]) -> google_types.Request[bytes]:
            assert kw["mimeType"] == "application/zip"
            return Answer(buf.getvalue())

    class Drive(NoDrive):
        @override
        def files(self) -> Files:
            return Exported()

    return Drive()


def test_a_doc_s_pictures_come_out_of_its_export_when_nothing_downloads(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Downloads off: the pictures pair with the export's `<img>` tags by document order,
    and only when the count and every size agree (a mismatch saves nothing, not the wrong one)."""
    from beamer2slides import doc_sync

    red, green = png(size=SQUARE, colour=(200, 0, 0)), png(size=SQUARE, colour=(0, 200, 0))
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")

    def runs() -> list[Run]:
        return [Run(text="", uri="https://lh7/a", value="kix.a", size=[13, 9]),
                Run(text="", src="talk.media/mine.png", value="kix.mine", size=[30, 20]),
                Run(text="", uri="https://lh7/b", value="kix.b", size=[40, 20])]

    tags: list[tuple[str, float, float]] = [("images/image1.png", 13.33, 8.89), ("images/image2.png", 30, 20),
                                            ("images/image3.png", 40, 20)]
    files = {"images/image1.png": red, "images/image2.png": b"not ours", "images/image3.png": green}
    found = runs()
    monkeypatch.setattr(doc_sync, "_pictures", _runs_are(found))
    assert doc_sync.fetch_pictures(tmp_path / "talk.html", NO_DOCUMENT, _doc_export(tags, files), "doc") == 2
    assert (tmp_path / _src(found[0])).read_bytes() == red
    assert (tmp_path / _src(found[2])).read_bytes() == green
    assert found[1].get("src") == "talk.media/mine.png", "a picture the file carries is left alone"

    for tags_now in (tags[:2], [tags[0], tags[1], ("images/image3.png", 20, 20)]):
        found = runs()
        monkeypatch.setattr(doc_sync, "_pictures", _runs_are(found))
        assert doc_sync.fetch_pictures(tmp_path / "talk.html", NO_DOCUMENT, _doc_export(tags_now, files),
                                       "doc") == 0
        assert "src" not in found[0] and "src" not in found[2]


def test_two_contexts_each_download_through_their_own(fetcher: Fetcher) -> None:
    """Per context, like credentials: a server with two requests in the air hands each its own."""
    import contextvars
    import threading

    got: dict[str, bytes] = {}

    def own(name: str) -> net.Fetch:
        def fetch(url: str) -> bytes:
            return name.encode()
        return fetch

    def request(name: str) -> None:
        with google_auth.use_fetcher(own(name)):
            barrier.wait()
            got[name] = net.download("u", None, net.TRIES)

    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=contextvars.copy_context().run, args=(request, n)) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == {"a": b"a", "b": b"b"}
