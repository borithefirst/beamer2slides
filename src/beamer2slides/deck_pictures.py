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

Drive refuses an export over its size limit (about 10 MB), and a slow one can outlast the
connection; with a Slides client at hand the deck is then exported in parts (`deck_export`: Drive
copies cut down to some of the slides, each paired with its own slides of the read). A `drive.file`
token only reaches decks this app made or was shown, and that refusal is the same missing picture.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from .google_types import (DriveService, Page, PageElement, Presentation, all_elements, background_fill,
                           background_url, image_url, object_id)

if TYPE_CHECKING:
    from .deck_export import SlidesSource
    from .net import Fetch

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
WORKERS = 8   # downloads of a deck's pictures in the air at once

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
EMBED = f"{{{NS['r']}}}embed"
RID = f"{{{NS['r']}}}id"
# The drawing objects of a shape tree, each one page element of Slides.
OBJECTS = {f"{{{NS['p']}}}{t}" for t in ("sp", "pic", "grpSp", "graphicFrame", "cxnSp", "contentPart")}
GROUP = f"{{{NS['p']}}}grpSp"


def pages(pres: Presentation) -> list[tuple[str, Page]]:
    """(kind, page) of a presentations.get in the export's order of parts: slides, masters, layouts."""
    return [("slide", s) for s in pres.get("slides", [])] + [("master", m) for m in pres.get("masters", [])] + \
        [("layout", l) for l in pres.get("layouts", [])]


def picture_urls(pres: Presentation) -> dict[str, str]:
    """Every picture of every page by the id that owns it: an image's own objectId, a page's for
    its background picture."""
    urls: dict[str, str] = {}
    for _, page in pages(pres):
        if url := background_url(page):
            urls[object_id(page)] = url
        for e in all_elements(page.get("pageElements", []), object_id(page)):
            if url := image_url(e):
                urls[object_id(e)] = url
    return urls


def picture_slides(pres: Presentation) -> list[str]:
    """The slides (objectIds, in order) owning a picture of their own: a background picture, or an
    image anywhere among their elements. What a master or layout holds comes with any slide."""
    out: list[str] = []
    for kind, page in pages(pres):
        if kind == "slide" and (background_url(page) or any(
                image_url(e) for e in all_elements(page.get("pageElements", []), object_id(page)))):
            out.append(object_id(page))
    return out


@dataclass(frozen=True, kw_only=True)
class Exported:
    """One object of an exported shape tree: its alt-text title and the relationship of its own
    picture (None: it has none)."""
    title: str | None
    embed: str | None


def _exported_objects(tree: ET.Element) -> list[Exported]:
    """A shape tree's objects depth first."""
    out: list[Exported] = []
    for node in tree:
        if node.tag not in OBJECTS:
            continue
        nv = next((c for c in node if c.tag.startswith(f"{{{NS['p']}}}nv")), None)
        c_nv = nv.find("p:cNvPr", NS) if nv is not None else None
        blip = node.find("p:blipFill/a:blip", NS)
        if blip is None:
            blip = node.find("p:spPr/a:blipFill/a:blip", NS)
        out.append(Exported(title=c_nv.get("title") if c_nv is not None else None,
                            embed=blip.get(EMBED) if blip is not None else None))
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
    rels: dict[str, str] = {}
    for r in root.findall("rel:Relationship", NS):
        rid, target = r.get("Id"), r.get("Target")
        if r.get("TargetMode") != "External" and rid is not None and target is not None:
            rels[rid] = posixpath.normpath(posixpath.join(folder, target))
    return rels


def _targets(rels: dict[str, str], nodes: list[ET.Element]) -> list[str]:
    """The parts `nodes` name by their relationship id, those the relationships hold, in order."""
    return [rels[rid] for n in nodes if (rid := n.get(RID)) is not None and rid in rels]


def _parts(z: zipfile.ZipFile) -> dict[str, list[str]]:
    """The package's slide, master and layout parts, each in the order the presentation lists them."""
    pres_part = "ppt/presentation.xml"
    root = ET.fromstring(z.read(pres_part))
    rels = _rels(z, pres_part)
    slides = _targets(rels, root.findall("p:sldIdLst/p:sldId", NS))
    masters = _targets(rels, root.findall("p:sldMasterIdLst/p:sldMasterId", NS))
    layouts: list[str] = []
    for m in masters:
        mroot = ET.fromstring(z.read(m))
        layouts += _targets(_rels(z, m), mroot.findall("p:sldLayoutIdLst/p:sldLayoutId", NS))
    return {"slide": slides, "master": masters, "layout": layouts}


def exported_pictures(data: bytes, pres: Presentation, wanted: Collection[str] | None) -> dict[str, bytes]:
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
            def want(oid: str) -> bool:
                return wanted is None or oid in wanted

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

            pid = object_id(page)
            if "stretchedPictureFill" in background_fill(page) and want(pid):
                blip = root.find("p:cSld/p:bg/p:bgPr/a:blipFill/a:blip", NS)
                picture = media(blip.get(EMBED)) if blip is not None else None
                if picture:
                    got[pid] = picture
            tree = root.find("p:cSld/p:spTree", NS)
            exported: list[Exported] = _exported_objects(tree) if tree is not None else []
            live = all_elements(page.get("pageElements", []), pid)   # (depth first, a group first: the export's order)
            if len(exported) == len(live) and all((x.title or None) == (e.get("title") or None)
                                                  for x, e in zip(exported, live)):
                pairs = list(zip(live, exported))
            else:
                by_title: dict[str, list[Exported]] = {}
                for x in exported:
                    if x.title:
                        by_title.setdefault(x.title, []).append(x)
                titled: dict[str, list[PageElement]] = {}
                for e in live:
                    if title := e.get("title"):
                        titled.setdefault(title, []).append(e)
                pairs = [(es[0], by_title[t][0]) for t, es in titled.items()
                         if len(es) == 1 and len(by_title.get(t, ())) == 1]
            for e, x in pairs:
                if "image" in e and want(object_id(e)):
                    picture = media(x.embed)
                    if picture:
                        got[object_id(e)] = picture
    return got


class LivePictures:
    """The pictures of one read of a live deck, downloaded where the fetcher allows, and the rest
    out of one Drive export, made at most once (`exported_pictures`).

    `fetch`: the fetcher (`net`), resolved on the calling thread (`google_auth.fetcher_for_threads`)
    when not given. `drive`: the Drive client an export goes through (None: no export). The export
    is made on the thread that asks - a client is one connection.

    `pptx`: the bytes of a .pptx of this deck a person downloaded (File > Download), which stands
    for the export: no Drive call is made, and a caller may read it before any download
    (`supplied`).

    `slides`: a Slides client (or a function making one, called only then), which lets a deck
    Drive will not export whole (its size, a timeout) come out in parts
    (`deck_export.export_deck`), of the slides owning a picture alone (`picture_slides`); None: the
    whole export or nothing. What it took
    is counted for reports: `exports` (export calls), `copies` (temporary copies made),
    `parts` (exports that came back), `unexported` (slide ids no export brought)."""

    def __init__(self, pres: Presentation, drive: DriveService | None, fetch: Fetch | None, workers: int,
                 pptx: bytes | None, slides: SlidesSource | None) -> None:
        self.pres, self.drive, self.workers, self.slides = pres, drive, workers, slides
        if fetch is None:
            from .google_auth import fetcher_for_threads
            fetch = fetcher_for_threads()
        self.fetch = fetch
        self.urls = picture_urls(pres)
        self.got: dict[str, bytes | None] = {}
        self.exported: dict[str, bytes] | None = None if pptx is None else exported_pictures(pptx, pres, None)
        self.supplied = pptx is not None
        self.downloads = 0    # how many were asked of the fetcher
        self.exports = 0      # how many exports were asked of Drive (1, or one per part tried)
        self.copies = 0
        self.parts = 0
        self.unexported: list[str] = []

    def _download(self, oid: str) -> bytes | None:
        from . import net
        try:
            return net.download(self.urls[oid], self.fetch, tries=3)
        except Exception:  # noqa: BLE001 - a harness's fetcher raises its own types
            return None

    def export(self) -> dict[str, bytes]:
        """Every picture of the deck out of one .pptx export, else out of its parts where there is
        a Slides client ({} when Drive would give neither)."""
        if self.exported is None:
            self.exported = {}
            if self.drive is not None and self.pres.get("presentationId"):
                from .deck_export import WORKERS as EXPORTS, export_deck
                done = export_deck(self.drive, self.slides, self.pres, per_part=None, workers=EXPORTS, clients=None,
                                   only=picture_slides(self.pres))
                self.exports += done.exports
                self.copies += done.copies
                self.parts += len(done.parts)
                self.unexported = [i for m in done.missing for i in m["ids"]]
                self.exported = done.pictures(self.pres, None)
        return self.exported

    def get(self, ids: Collection[str]) -> dict[str, bytes]:
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
        return {i: picture for i in ids if (picture := self.got.get(i))}
