"""A live deck's pictures, for their pixel signatures (`snapshot.signature`), with or without
fetching a URL.

A picture in Slides hangs off a `contentUrl`: a capability URL on googleusercontent.com, which a
process that may not fetch the open web (the agent layer's harness at Google) cannot download.
Drive can hand the same pictures over through its authenticated API: `files.export` of the deck
as .pptx is one call carrying every picture of every page, measured on converted decks (a 10-slide
deck: 469 kB in 1.3 s; of 23 pictures on two decks, every exported one signed like its download).
So a picture is downloaded first, where a fetcher is allowed, and whatever did not come is read
out of one export (`LivePictures.get`).

An export does not keep object ids (an object comes back as `Google Shape;128;p13`), but it keeps
the pages in order, each page's objects in drawing order, and every alt-text title - the
`b2s:<slide>/<element>` tags sync puts on its objects. `exported_pictures` pairs a page's exported
objects with the live ones in order, and checks the pairing on every title; a page whose objects
do not pair up that way falls back to the titles alone. A picture it cannot place stays unread,
which is what a failed download is: its signature is missing and the picture is compared by its
URL alone - a new URL then reads as a replaced picture, the answer that never loses one.

Drive refuses an export over its size limit (about 10 MB), and a `drive.file` token only reaches
decks this app made or was shown; either is the same missing picture.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
EMBED = f"{{{NS['r']}}}embed"
RID = f"{{{NS['r']}}}id"
# The drawing objects of a shape tree, each one page element of Slides.
OBJECTS = {f"{{{NS['p']}}}{t}" for t in ("sp", "pic", "grpSp", "graphicFrame", "cxnSp", "contentPart")}
GROUP = f"{{{NS['p']}}}grpSp"


def pages(pres: dict) -> list[tuple[str, dict]]:
    """(kind, page) of a presentations.get in the export's order of parts: slides, masters, layouts."""
    return [("slide", s) for s in pres.get("slides", [])] + [("master", m) for m in pres.get("masters", [])] + \
        [("layout", l) for l in pres.get("layouts", [])]


def picture_urls(pres: dict) -> dict[str, str]:
    """Every picture of every page by the id that owns it: an image's own objectId, a page's for
    its background picture."""
    urls = {}
    for _, page in pages(pres):
        fill = page.get("pageProperties", {}).get("pageBackgroundFill", {})
        if fill.get("stretchedPictureFill", {}).get("contentUrl"):
            urls[page["objectId"]] = fill["stretchedPictureFill"]["contentUrl"]
        stack = list(page.get("pageElements", []))
        while stack:
            e = stack.pop()
            if e.get("image", {}).get("contentUrl"):
                urls[e["objectId"]] = e["image"]["contentUrl"]
            stack += e.get("elementGroup", {}).get("children", [])
    return urls


def _live_objects(elements: list[dict]) -> list[dict]:
    """A page's elements depth first, a group before its children: the export's order."""
    out = []
    for e in elements:
        out.append(e)
        out += _live_objects(e.get("elementGroup", {}).get("children", []))
    return out


def _exported_objects(tree) -> list[dict]:
    """A shape tree's objects depth first: {"title", "embed" (the relationship of its own picture)}."""
    out = []
    for node in tree:
        if node.tag not in OBJECTS:
            continue
        nv = next((c for c in node if c.tag.startswith(f"{{{NS['p']}}}nv")), None)
        c_nv = nv.find("p:cNvPr", NS) if nv is not None else None
        blip = node.find("p:blipFill/a:blip", NS)
        if blip is None:
            blip = node.find("p:spPr/a:blipFill/a:blip", NS)
        out.append({"title": c_nv.get("title") if c_nv is not None else None,
                    "embed": blip.get(EMBED) if blip is not None else None})
        if node.tag == GROUP:
            out += _exported_objects(node)
    return out


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationship id -> the part it targets, for one part of the package."""
    folder, name = posixpath.split(part)
    path = posixpath.join(folder, "_rels", name + ".rels")
    if path not in z.namelist():
        return {}
    root = ET.fromstring(z.read(path))
    return {r.get("Id"): posixpath.normpath(posixpath.join(folder, r.get("Target")))
            for r in root.findall("rel:Relationship", NS) if r.get("TargetMode") != "External"}


def _parts(z: zipfile.ZipFile) -> dict[str, list[str]]:
    """The package's slide, master and layout parts, each in the order the presentation lists them."""
    pres_part = "ppt/presentation.xml"
    root = ET.fromstring(z.read(pres_part))
    rels = _rels(z, pres_part)
    slides = [rels[s.get(RID)] for s in root.findall("p:sldIdLst/p:sldId", NS) if s.get(RID) in rels]
    masters = [rels[m.get(RID)] for m in root.findall("p:sldMasterIdLst/p:sldMasterId", NS) if m.get(RID) in rels]
    layouts = []
    for m in masters:
        mrels = _rels(z, m)
        mroot = ET.fromstring(z.read(m))
        layouts += [mrels[l.get(RID)] for l in mroot.findall("p:sldLayoutIdLst/p:sldLayoutId", NS) if l.get(RID) in mrels]
    return {"slide": slides, "master": masters, "layout": layouts}


def exported_pictures(data: bytes, pres: dict, wanted=None) -> dict[str, bytes]:
    """The pictures of a .pptx export of the deck `pres` describes, by the id that owns each (as
    `picture_urls`); `wanted`: only these ids (None: all). A page whose objects cannot be paired
    with the live ones in order is paired by title, and what neither places is left out."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        parts = _parts(z)
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return {}
    got: dict[str, bytes] = {}
    for kind in ("slide", "master", "layout"):
        live_pages = [p for k, p in pages(pres) if k == kind]
        if len(live_pages) != len(parts[kind]):
            continue  # (the pages cannot be told apart by their place)
        for page, part in zip(live_pages, parts[kind]):
            want = lambda oid: wanted is None or oid in wanted  # noqa: E731
            try:
                root = ET.fromstring(z.read(part))
            except (KeyError, ET.ParseError):
                continue
            rels = _rels(z, part)

            def media(rid: str | None) -> bytes | None:
                target = rels.get(rid) if rid else None
                try:
                    return z.read(target) if target else None
                except KeyError:
                    return None

            fill = page.get("pageProperties", {}).get("pageBackgroundFill", {})
            if "stretchedPictureFill" in fill and want(page["objectId"]):
                blip = root.find("p:cSld/p:bg/p:bgPr/a:blipFill/a:blip", NS)
                picture = media(blip.get(EMBED)) if blip is not None else None
                if picture:
                    got[page["objectId"]] = picture
            tree = root.find("p:cSld/p:spTree", NS)
            exported = _exported_objects(tree) if tree is not None else []
            live = _live_objects(page.get("pageElements", []))
            if len(exported) == len(live) and all((x["title"] or None) == (e.get("title") or None)
                                                  for x, e in zip(exported, live)):
                pairs = list(zip(live, exported))
            else:
                by_title: dict[str, list[dict]] = {}
                for x in exported:
                    if x["title"]:
                        by_title.setdefault(x["title"], []).append(x)
                titled: dict[str, list[dict]] = {}
                for e in live:
                    if e.get("title"):
                        titled.setdefault(e["title"], []).append(e)
                pairs = [(es[0], by_title[t][0]) for t, es in titled.items()
                         if len(es) == 1 and len(by_title.get(t, ())) == 1]
            for e, x in pairs:
                if "image" in e and want(e["objectId"]):
                    picture = media(x["embed"])
                    if picture:
                        got[e["objectId"]] = picture
    return got


class LivePictures:
    """The pictures of one read of a live deck, downloaded where the fetcher allows, and the rest
    out of one Drive export, made at most once (`exported_pictures`).

    `fetch`: the fetcher (`net`), resolved on the calling thread (`google_auth.fetcher_for_threads`)
    when not given. `drive`: the Drive client an export goes through (None: no export). The export
    is made on the thread that asks - a client is one connection."""

    def __init__(self, pres: dict, drive=None, fetch=None, workers: int = 8):
        self.pres, self.drive, self.workers = pres, drive, workers
        if fetch is None:
            from .google_auth import fetcher_for_threads
            fetch = fetcher_for_threads()
        self.fetch = fetch
        self.urls = picture_urls(pres)
        self.got: dict[str, bytes | None] = {}
        self.exported: dict[str, bytes] | None = None
        self.downloads = 0    # how many were asked of the fetcher
        self.exports = 0      # how many exports were made (0 or 1)

    def _download(self, oid: str) -> bytes | None:
        from . import net
        try:
            return net.download(self.urls[oid], self.fetch, tries=3)
        except Exception:  # noqa: BLE001 - a harness's fetcher raises its own types
            return None

    def export(self) -> dict[str, bytes]:
        """Every picture of the deck out of one .pptx export ({} when Drive would not give one)."""
        if self.exported is None:
            self.exported = {}
            if self.drive is not None and self.pres.get("presentationId"):
                from .gapi import HttpError
                from .gslides import SLOW_EXPORT, execute
                try:
                    self.exports += 1
                    data = execute(self.drive.files().export_media(fileId=self.pres["presentationId"],
                                                                   mimeType=PPTX_MIME), retries=3, timeout=SLOW_EXPORT)
                    if isinstance(data, (bytes, bytearray)):
                        self.exported = exported_pictures(bytes(data), self.pres)
                except (HttpError, OSError):
                    pass
        return self.exported

    def get(self, ids) -> dict[str, bytes]:
        """The bytes of these pictures (ids as `picture_urls`), those that could be had."""
        todo = [i for i in dict.fromkeys(ids) if i in self.urls and i not in self.got]
        if todo:
            self.downloads += len(todo)
            if len(todo) == 1:
                data = [self._download(todo[0])]
            else:
                with ThreadPoolExecutor(max_workers=min(self.workers, len(todo))) as pool:
                    data = list(pool.map(self._download, todo))
            self.got.update(zip(todo, data))
            missing = [i for i in todo if not self.got[i]]
            if missing:
                exported = self.export()
                for i in missing:
                    self.got[i] = exported.get(i)
        return {i: self.got[i] for i in ids if self.got.get(i)}
