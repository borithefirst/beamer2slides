"""`net`: every download of content the library did not write goes through one door, and a caller
(`google_auth.use_fetcher`, `AgentContext.fetch_google_content`) may own it.

The sites are the ones a backend's egress review found: picture signatures (`snapshot`, in
test_base_storage.py), thumbnails (`gslides.save_thumbnail`), a read deck's pictures and a
picture's original `source_url` (`deck_ir.fetch_url`, `inverse`), a Doc's inserted pictures
(`doc_sync.fetch_pictures`). Each is driven here through an installed fetcher, with urllib made
to fail the test if it is ever reached.
"""

import io
from types import SimpleNamespace

import pytest

from beamer2slides import google_auth, net


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    monkeypatch.setattr(net, "urllib_fetch", lambda url: pytest.fail(f"urllib fetched {url}"))
    monkeypatch.setattr(net.time, "sleep", lambda s: None)


def png(size=(8, 8), colour=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def test_nothing_installed_is_urllib():
    assert google_auth.fetcher_for_threads() is net.urllib_fetch


def test_downloads_can_be_switched_off(monkeypatch):
    """`$B2S_NO_DOWNLOADS` (the CLI's `--no-downloads`) makes `no_downloads` the default: asked
    once, never retried. A fetcher a caller installed still wins."""
    monkeypatch.setenv(net.NO_DOWNLOADS, "0")
    assert not net.downloads_off()
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")
    assert google_auth.fetcher_for_threads() is net.no_downloads and net.downloads_off()
    with pytest.raises(PermissionError, match="switched off"):
        net.download("u")
    with google_auth.use_fetcher(lambda url: b"mine"):
        assert net.download("u") == b"mine" and not net.downloads_off()


def test_with_downloads_off_no_place_is_measured(monkeypatch):
    """Measuring a hole's place is a thumbnail download: with downloads off not even the scratch
    slides are made, and every picture keeps its predicted place."""
    from beamer2slides import emit, emit_places
    monkeypatch.setattr(emit_places, "measure_jobs", lambda *a: ([{"createSlide": {}}], [("job",)]))
    monkeypatch.setattr(emit_places, "batch",lambda *a: pytest.fail("scratch slides written"))
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")
    assert emit.measure_places(None, "P", {"slides": []}, 1.0, None, {}, {}, None) == ({}, [])


def test_a_download_is_retried_and_the_last_failure_raised():
    tries = []

    def flaky(url):
        tries.append(url)
        if len(tries) < 3:
            raise ConnectionError("blip")
        return b"ok"

    assert net.download("u", flaky, tries=3) == b"ok" and len(tries) == 3
    tries.clear()
    with pytest.raises(ConnectionError):
        net.download("u", lambda url: tries.append(url) or (_ for _ in ()).throw(ConnectionError("x")), tries=2)
    assert len(tries) == 2


def test_not_allowed_is_asked_once():
    tries = []

    def forbid(url):
        tries.append(url)
        raise PermissionError(url)

    with pytest.raises(PermissionError):
        net.download("u", forbid, tries=5)
    assert tries == ["u"]


def test_a_fetcher_must_hand_back_bytes():
    with pytest.raises(TypeError, match="not bytes"):
        net.download("u", lambda url: "text", tries=1)
    assert net.download("u", lambda url: bytearray(b"ab"), tries=1) == b"ab"


def test_the_installed_fetcher_is_used_when_none_is_passed(fetcher):
    fetcher(lambda url: f"<{url}>".encode())
    assert net.download("u") == b"<u>"


@pytest.mark.parametrize("data, suffix", [(png(), ".png"), (b"\xff\xd8\xff\xe0rest", ".jpg"),
                                          (b"GIF89a...", ".gif"), (b"RIFF\0\0\0\0WEBPVP8 ", ".webp"),
                                          (b"<?xml version='1.0'?><svg/>", ".svg"), (b"??", ".png")])
def test_a_picture_names_its_own_type(data, suffix):
    assert net.picture_suffix(data) == suffix


def test_a_thumbnail_is_downloaded_through_the_fetcher(tmp_path, fetcher):
    class Slides:
        def presentations(self):
            return self

        def pages(self):
            return self

        def getThumbnail(self, **kw):
            return SimpleNamespace(execute=lambda: {"contentUrl": "https://thumb/1", "width": 1600,
                                                    "height": 900})

    from beamer2slides.gslides import save_thumbnail

    fetcher(lambda url: png() if url == "https://thumb/1" else pytest.fail(url))
    assert save_thumbnail(Slides(), "pid", "p1", tmp_path / "t" / "1.png") == (1600, 900)
    assert (tmp_path / "t" / "1.png").read_bytes() == png()
    assert not list((tmp_path / "t").glob("*.part"))


def test_a_read_deck_s_pictures_come_through_the_fetcher(tmp_path, fetcher):
    from beamer2slides import deck_ir

    fetcher(lambda url: png())
    got = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path)
    assert got.format and (tmp_path / got.file).read_bytes() == png()

    class Refused(Exception):
        pass

    fetcher(lambda url: (_ for _ in ()).throw(Refused("egress denied")))
    assert "egress denied" in deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path).error


def _s15f16(x: float) -> bytes:
    import struct
    return struct.pack(">i", int(round(x * 65536)))


def _xyztype(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\x00" * 4 + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _curv_gamma(gamma: float) -> bytes:
    import struct
    data = b"curv" + b"\x00" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256)))
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
    entries, blob, written = [], b"", {}
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


def png_with_icc(colour, icc: bytes) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buf, format="PNG", icc_profile=icc)
    return buf.getvalue()


def test_a_picture_s_embedded_colour_profile_is_applied_and_dropped(tmp_path, fetcher):
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
    saved = (tmp_path / got.file).read_bytes()
    from PIL import Image
    import numpy as np
    out = Image.open(io.BytesIO(saved))
    assert out.info.get("icc_profile") is None                 # no profile left to (mis)apply again
    px = tuple(np.array(out.convert("RGB"))[0, 0])
    assert px != (128, 128, 128)                                # the profile was actually used
    assert px == (188, 188, 188)                                # deterministic: lcms2's own transform


def test_a_picture_with_no_colour_profile_is_untouched(tmp_path, fetcher):
    from beamer2slides import deck_ir

    fetcher(lambda url: png())
    got = deck_ir.stash_picture("https://lh3/pic=s0", lambda u: deck_ir.fetch_url(u, None), tmp_path)
    assert (tmp_path / got.file).read_bytes() == png()


def test_a_picture_s_source_url_on_any_host_goes_through_the_fetcher(tmp_path, fetcher):
    """The original of a picture inserted by URL: a host chosen by whoever inserted it, which is
    exactly the egress a backend has to see."""
    from beamer2slides.inverse import Planner, picture_look

    big, small = png((64, 64)), png((16, 16))
    asked = []
    fetcher(lambda url: asked.append(url) or big)
    cur = tmp_path / "cur.png"
    cur.write_bytes(small)
    planner = SimpleNamespace(ws=SimpleNamespace(work=tmp_path), ctx=SimpleNamespace(notes=[]))
    te = {"source_url": "https://example.org/figure.png"}
    data, _, _ = Planner.source_url_bytes(planner, te, small, "png", picture_look(cur))
    assert asked == ["https://example.org/figure.png"]
    assert data == big and planner.ctx.notes, "the larger original was taken"


def test_a_doc_s_inserted_pictures_come_through_the_fetcher(tmp_path, monkeypatch, fetcher):
    """A fetcher hands over bytes and no Content-Type, so the file is named by what it holds."""
    from beamer2slides import doc_sync

    runs = [{"uri": "https://lh7/one", "value": "kix.one"}, {"uri": "https://lh7/two", "value": "kix.two"},
            {"uri": "https://lh7/gone", "value": "kix.gone"}]
    monkeypatch.setattr(doc_sync, "_pictures", lambda live: runs)
    answers = {"https://lh7/one": png(), "https://lh7/two": b"\xff\xd8\xff\xe0jpeg"}

    def fetch(url):
        if url not in answers:
            raise LookupError(url)
        return answers[url]

    fetcher(fetch)
    assert doc_sync.fetch_pictures(tmp_path / "talk.html", {}, drive=None, ident=None) == 2
    assert runs[0]["src"] == "talk.media/kix.one.png" and runs[1]["src"] == "talk.media/kix.two.jpg"
    assert "src" not in runs[2]


def _doc_export(tags, files):
    """What Drive's zip export of a document holds: one HTML page and its images/."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("talk.html", "<html><body>" + "".join(
            f'<p><img alt="" src="{src}" style="width: {w}px; height: {h}px;"></p>'
            for src, w, h in tags) + "</body></html>")
        for name, data in files.items():
            z.writestr(name, data)

    class Request:
        def execute(self):
            return buf.getvalue()

    class Files:
        def export(self, fileId, mimeType):
            assert mimeType == "application/zip"
            return Request()

    class Drive:
        def files(self):
            return Files()

    return Drive()


def test_a_doc_s_pictures_come_out_of_its_export_when_nothing_downloads(tmp_path, monkeypatch):
    """Downloads off: the pictures pair with the export's `<img>` tags by document order,
    and only when the count and every size agree (a mismatch saves nothing, not the wrong one)."""
    from beamer2slides import doc_sync

    red, green = png(colour=(200, 0, 0)), png(colour=(0, 200, 0))
    monkeypatch.setenv(net.NO_DOWNLOADS, "1")

    def runs():
        return [{"uri": "https://lh7/a", "value": "kix.a", "size": [13, 9]},
                {"src": "talk.media/mine.png", "value": "kix.mine", "size": [30, 20]},
                {"uri": "https://lh7/b", "value": "kix.b", "size": [40, 20]}]

    tags = [("images/image1.png", 13.33, 8.89), ("images/image2.png", 30, 20),
            ("images/image3.png", 40, 20)]
    files = {"images/image1.png": red, "images/image2.png": b"not ours", "images/image3.png": green}
    found = runs()
    monkeypatch.setattr(doc_sync, "_pictures", lambda live: found)
    assert doc_sync.fetch_pictures(tmp_path / "talk.html", {}, _doc_export(tags, files), "doc") == 2
    assert (tmp_path / found[0]["src"]).read_bytes() == red
    assert (tmp_path / found[2]["src"]).read_bytes() == green
    assert found[1]["src"] == "talk.media/mine.png", "a picture the file carries is left alone"

    for tags_now in (tags[:2], [tags[0], tags[1], ("images/image3.png", 20, 20)]):
        found = runs()
        assert doc_sync.fetch_pictures(tmp_path / "talk.html", {}, _doc_export(tags_now, files),
                                       "doc") == 0
        assert "src" not in found[0] and "src" not in found[2]


def test_two_contexts_each_download_through_their_own(fetcher):
    """Per context, like credentials: a server with two requests in the air hands each its own."""
    import contextvars
    import threading

    got = {}

    def request(name):
        with google_auth.use_fetcher(lambda url, name=name: name.encode()):
            barrier.wait()
            got[name] = net.download("u")

    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=contextvars.copy_context().run, args=(request, n)) for n in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == {"a": b"a", "b": b"b"}
