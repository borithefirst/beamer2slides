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
    from beamer2slides import emit
    monkeypatch.setattr(emit, "measure_jobs", lambda *a: ([{"createSlide": {}}], [("job",)]))
    monkeypatch.setattr(emit, "batch", lambda *a: pytest.fail("scratch slides written"))
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
    got = deck_ir.stash_picture("https://lh3/pic=s0", deck_ir.fetch_url, tmp_path)
    assert got["format"] and (tmp_path / got["file"]).read_bytes() == png()

    class Refused(Exception):
        pass

    fetcher(lambda url: (_ for _ in ()).throw(Refused("egress denied")))
    assert "egress denied" in deck_ir.stash_picture("https://lh3/pic=s0", deck_ir.fetch_url, tmp_path)["error"]


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
    assert doc_sync.fetch_pictures(tmp_path / "talk.html", {}) == 2
    assert runs[0]["src"] == "talk.media/kix.one.png" and runs[1]["src"] == "talk.media/kix.two.jpg"
    assert "src" not in runs[2]


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
