"""A deck handed over as files: everything a live read takes from Google and the web, saved by a
process that may reach them, so a process that may reach neither adopts the deck as well.

A live `adopt` reads a deck through four doors, and each gives the source something:

    presentation   the Slides API's `presentations.get` answer. Required: every object with its
                   words, styles and place, the layouts and master, and the object ids a sync base
                   pairs with (`adopt_sync`).
    thumbnails     Google's LARGE render of each slide (`deck_ir.slide_thumbnails`). The fills the
                   API does not report (gradients, table-style colours), measured text insets and
                   line weights (`deck_fills`, `deck_ir.thumbnail_*`), and the page `frame_guard`
                   scores each frame's rounds against.
    pictures       every picture URL the read downloads, recorded. The bytes of slide pictures,
                   page backgrounds, linked charts' renders and video poster frames, and the larger
                   original of a picture inserted by URL (`inverse.Edit.source_url_bytes`).
    google_fonts   every google/fonts file adopt fetched, recorded, the deck's own typefaces
                   (`fontfetch`) - under a font choice that looked at no installed font, so what a
                   machine without them needs is all there (`adopt.no_machine_fonts`).

and two a person may add:

    pptx           the deck as they downloaded it (File > Download). Slide pictures and page
                   backgrounds paired by page and drawing order (`deck_pictures`): the pictures,
                   where no recording could be made. Not charts' renders, poster frames or originals.
    fonts          font files of their own (`fontfiles`): a typeface google/fonts does not carry.

`save` writes the first four into a folder (`deck-files.json` says what is there); `DeckFiles`
is that folder (or its .zip) read back, each part also given on its own. A recording is replayed
through the context's fetcher (`replaying`): a URL it holds is answered from it, one it holds as
absent is a 404 (google/fonts is asked `ofl/` before `apache/`), and any other goes on to the
fetcher underneath - the harness's, which may refuse. Only `save` reaches Google.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "deck-files.json"

#: What each part adds, in the words every interface uses (CLI help, `deck_adopt`, the report).
PARTS = {
    "presentation": "the deck itself (Slides API presentations.get, saved whole): every object's words, "
                    "styles and place, layouts and master, and the object ids a sync pairs with. Required",
    "thumbnails": "Google's render of each slide: fills the API does not report (gradients, table-style "
                  "colours), measured text insets and line weights, and the page each frame's rounds are "
                  "scored against",
    "pictures": "every picture download, recorded: the exact bytes of slide pictures, backgrounds, chart "
                "renders, video poster frames, and the larger originals of pictures inserted by URL",
    "google_fonts": "every google/fonts file adopt fetched, recorded: the deck's own typefaces, so lines "
                    "break where the deck's do",
    "pptx": "the deck as downloaded (File > Download): slide pictures and backgrounds, where no recording "
            "of the pictures could be made",
    "fonts": "font files of the person's own: typefaces google/fonts does not carry",
}
#: What a read without the part lacks, for the report.
WITHOUT = {
    "thumbnails": "fills the API does not report are not read (flat colours instead of gradients) and "
                  "frames are scored by residuals alone",
    "pictures": "pictures come from the .pptx or a download, else are left out",
    "google_fonts": "typefaces come from this machine, a font_source or a download, else stand-ins "
                    "(lines break elsewhere)",
}
FOLDERS = {"presentation": "presentation.json", "thumbnails": "thumbnails", "pictures": "pictures",
           "google_fonts": "google-fonts", "pptx": "deck.pptx"}


# ---------------------------------------------------------------- recordings

class Recording:
    """URLs and what fetching them gave, in a folder: `index.json` ({"files": {url: name},
    "absent": [url]}) and the files, named by their bytes."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        index = self.folder / "index.json"
        doc = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        self.files: dict[str, str] = dict(doc.get("files") or {})
        self.absent: set[str] = set(doc.get("absent") or [])
        self.answered: set[str] = set()     # what a replay took from it

    def __len__(self) -> int:
        return len(self.files)

    def put(self, url: str, data: bytes) -> None:
        from .deck_ir import FORMAT_EXT, image_format
        ext = FORMAT_EXT.get(image_format(data)) or Path(url.split("?")[0]).suffix.lstrip(".")[:8] or "bin"
        name = f"{hashlib.sha1(data).hexdigest()[:16]}.{ext}"
        self.folder.mkdir(parents=True, exist_ok=True)
        if not (self.folder / name).exists():
            (self.folder / name).write_bytes(data)
        self.files[url] = name
        self.absent.discard(url)

    def gone(self, url: str) -> None:
        if url not in self.files:
            self.absent.add(url)

    def get(self, url: str) -> bytes | None:
        name = self.files.get(url)
        if not name or not (self.folder / name).is_file():
            return None
        self.answered.add(url)
        return (self.folder / name).read_bytes()

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "index.json").write_text(json.dumps({"files": self.files, "absent": sorted(self.absent)},
                                                           indent=1), encoding="utf-8")


def recording(fetch, pick):
    """`fetch` that records what it gives into `pick(url)` (a `Recording`, or None: not recorded),
    and a 404 as absent."""
    def recorded(url: str) -> bytes:
        store = pick(url)
        try:
            data = fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and store is not None:
                store.gone(url)
            raise
        if store is not None:
            store.put(url, data)
        return data
    return recorded


def replay(stores, fallback):
    """A fetcher answering from recordings first, then `fallback` (None: refusing)."""
    def fetch(url: str) -> bytes:
        for store in stores:
            data = store.get(url)
            if data is not None:
                return data
            if url in store.absent:
                raise urllib.error.HTTPError(url, 404, "Not Found (recorded)", {}, None)
        if fallback is None:
            raise PermissionError(f"not in the deck's files: {url}")
        return fallback(url)
    return fetch


# ---------------------------------------------------------------- the files, read back

@dataclass
class DeckFiles:
    """A deck handed over as files (module docstring; `PARTS` says what each adds).

    presentation: the Slides API's presentations.get answer, saved whole. Required.
    thumbnails:   Google's render of each slide - picture files or folders of them, a slide's by its
                  number (`003.png`) or objectId, else by order.
    pictures:     a recording of the picture downloads (`save`'s `pictures/`).
    google_fonts: a recording of the google/fonts files (`save`'s `google-fonts/`).
    pptx:         the deck as a person downloaded it: pictures where there is no recording.
    fonts:        font files of the person's own (`adopt --fonts`).
    """
    presentation: Path
    thumbnails: list[Path] = field(default_factory=list)
    pictures: Path | None = None
    google_fonts: Path | None = None
    pptx: Path | None = None
    fonts: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, into: Path | None = None) -> "DeckFiles":
        """The files `save` wrote: its folder, or a .zip of it (unpacked into `into`), or a lone
        presentations.get .json."""
        path = Path(path)
        if path.suffix.lower() == ".zip":
            path = unzip(path, into or path.with_suffix(""))
        if path.is_file():
            return cls(presentation=path)
        found = {k: path / v for k, v in FOLDERS.items() if (path / v).exists()}
        if "presentation" not in found:
            raise FileNotFoundError(f"{path} holds no {FOLDERS['presentation']}: not a deck's files")
        return cls(presentation=found["presentation"],
                   thumbnails=[found["thumbnails"]] if "thumbnails" in found else [],
                   pictures=found.get("pictures"), google_fonts=found.get("google_fonts"),
                   pptx=found.get("pptx"))

    def given(self) -> dict[str, bool]:
        return {"presentation": True, "thumbnails": bool(self.thumbnails), "pictures": self.pictures is not None,
                "google_fonts": self.google_fonts is not None, "pptx": self.pptx is not None,
                "fonts": bool(self.fonts)}


def unzip(path: Path, into: Path) -> Path:
    """A .zip of a deck's files, unpacked (no member outside `into`); the folder that holds the
    presentation - the zip's root, or the one folder it was made from."""
    into = Path(into)
    with zipfile.ZipFile(path) as z:
        for member in z.infolist():
            dest = (into / member.filename).resolve()
            if not dest.is_relative_to(into.resolve()):
                raise ValueError(f"{path}: {member.filename} would land outside the folder")
        z.extractall(into)
    if (into / FOLDERS["presentation"]).exists():
        return into
    inner = [p for p in into.iterdir() if p.is_dir() and (p / FOLDERS["presentation"]).exists()]
    return inner[0] if len(inner) == 1 else into


def at(ref, into: Path | None = None) -> DeckFiles | None:
    """The deck's files `ref` names - a folder `save` wrote, or a .zip of one (unpacked into
    `into`) - else None (a deck URL or id, a converted output folder, a lone .json)."""
    p = Path(str(ref))
    if p.suffix.lower() == ".zip" and p.is_file():
        return DeckFiles.load(p, into or p.parent / f"{p.stem}-files")
    if p.is_dir() and (p / FOLDERS["presentation"]).is_file():
        return DeckFiles.load(p)
    return None


def gather(deck, into: Path | None = None, thumbnails=(), pictures=None, google_fonts=None) -> DeckFiles | None:
    """The deck's files from what a person named: `deck` a folder or .zip `save` wrote, or a saved
    presentations.get .json, with each part given on its own added or put in place of the
    folder's. None when `deck` is none of these and no part was given (a live read)."""
    files = at(deck, into)
    if files is None and not (thumbnails or pictures or google_fonts):
        return None
    if files is None:
        p = Path(str(deck))
        if p.suffix.lower() != ".json" or not p.is_file():
            raise SystemExit("thumbnails, pictures and google fonts go with a deck read from files: a saved "
                             "presentations.get .json, or the folder or .zip deck-files wrote")
        files = DeckFiles(presentation=p)
    for name, folder in (("pictures", pictures), ("google fonts", google_fonts)):
        if folder and not Path(folder).is_dir():
            raise SystemExit(f"{name}: {folder} is not a folder (deck-files records them into one)")
    if thumbnails:
        files.thumbnails = [Path(t) for t in thumbnails]
    files.pictures = Path(pictures) if pictures else files.pictures
    files.google_fonts = Path(google_fonts) if google_fonts else files.google_fonts
    return files


@contextmanager
def replaying(files: DeckFiles | None):
    """Install the files' recordings as the context's fetcher for the block, over whatever
    fetcher was there (`google_auth.use_fetcher`): every download of the run - pictures, originals,
    google/fonts - is answered from them first. Yields {part: Recording} of those given."""
    stores = {} if files is None else {k: Recording(p) for k, p in
                                       (("pictures", files.pictures), ("google_fonts", files.google_fonts))
                                       if p is not None}
    if not stores:
        yield stores
        return
    from . import google_auth
    with google_auth.use_fetcher(replay(list(stores.values()), google_auth.fetcher_for_threads())):
        yield stores


def report(files: DeckFiles, counts: dict) -> tuple[list[str], dict]:
    """Lines for the log and data for the agent: each part, whether it was given, what it adds,
    and for one that was not, what the read lacks."""
    lines, data = ["deck read from files:"], {}
    for part, adds in PARTS.items():
        given = files.given()[part]
        entry = {"given": given, "adds": adds, **({"count": counts[part]} if given and part in counts else {})}
        if not given and part in WITHOUT:
            entry["without"] = WITHOUT[part]
        data[part] = entry
        n = f" ({counts[part]})" if given and part in counts else ""
        lines.append(f"  {part}: {'given' + n if given else 'not given'}"
                     + (f" - {WITHOUT[part]}" if not given and part in WITHOUT else ""))
    return lines, data


# ---------------------------------------------------------------- saving them

def save(ref: str, out: Path, pptx: Path | None = None, log=print) -> dict:
    """Save everything a live `adopt` of `ref` reads into `out`, for a process that may reach
    neither Google nor the web: presentation.json, thumbnails/, pictures/ and google-fonts/
    (recordings), and the person's .pptx when given. Returns the manifest (`deck-files.json`).

    The pictures are those a live read fetches (a download, else Drive's export, as `read_deck`),
    plus the originals of pictures inserted by URL. The fonts are recorded while adopt picks the
    deck's typefaces, and that pick is made as a machine without fonts would make it - from an
    empty google/fonts cache, looking at nothing installed - so the files hold what the machine
    that reads them could need; one with the fonts installed uses those, like a live read there."""
    from . import adopt, fontfetch, google_auth
    from .deck_ir import deck_ir, fetch_url, picture_fetch, presentation_id, slide_thumbnails
    from .google_auth import slides_service
    from .gslides import execute

    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty: deck files go into a folder of their own")
    out.mkdir(parents=True, exist_ok=True)
    pid = presentation_id(ref)
    pres = execute(slides_service().presentations().get(presentationId=pid))
    (out / FOLDERS["presentation"]).write_text(json.dumps(pres, ensure_ascii=False), encoding="utf-8")
    log(f"presentation: {len(pres.get('slides', []))} slides")
    thumbs = slide_thumbnails(pid, pres, out / FOLDERS["thumbnails"])
    shots = sum(thumbs(n) is not None for n in range(len(pres.get("slides", []))))
    log(f"thumbnails: {shots} of {len(pres.get('slides', []))}")

    pictures, fonts = Recording(out / FOLDERS["pictures"]), Recording(out / FOLDERS["google_fonts"])
    pick = lambda url: None if url.startswith(fontfetch.RAW) else pictures   # noqa: E731

    def seen(url: str, data: bytes | None) -> None:
        fonts.gone(url) if data is None else fonts.put(url, data)

    missing: list = []
    # (Windows: a font file adopt measured may still be held open when the folder goes)
    with tempfile.TemporaryDirectory(prefix="b2s-deck-files-", ignore_cleanup_errors=True) as tmp:
        # fonts fetched afresh (so every file is seen), whatever this machine has or was told
        with _environ(B2S_FONT_CACHE=str(Path(tmp) / "font-cache"), B2S_FONTS=None, B2S_FONT_FETCH=None), \
                google_auth.use_fetcher(recording(google_auth.fetcher_for_threads(), pick)), \
                fontfetch.watching(seen), adopt.no_machine_fonts():
            fetch = recording(picture_fetch(pres), lambda url: pictures)   # (a Drive export's too)
            target = deck_ir(pres, None, None, fetch, Path(tmp) / "images", True, thumbs)
            for el in _images(target):
                if el.get("source_url"):
                    try:
                        fetch_url(el["source_url"])
                    except Exception:  # noqa: BLE001 - the deck's own bytes are what a live read keeps then
                        pass
            adopt.bootstrap(target, Path(tmp) / "tree" / "main.tex", False, missing)
    pictures.save()
    fonts.save()
    log(f"pictures: {len(pictures)} recorded")
    log(f"google fonts: {len(fonts)} files recorded"
        + (f"; stood in for (not on google/fonts): {', '.join(sorted({m['font'] for m in missing}))}" if missing else ""))
    if pptx is not None:
        shutil.copyfile(pptx, out / FOLDERS["pptx"])
    manifest = {"version": 1, "presentationId": pres.get("presentationId"), "title": pres.get("title"),
                "revisionId": pres.get("revisionId"),
                "saved": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "parts": {k: {"path": v, "adds": PARTS[k]} for k, v in FOLDERS.items() if (out / v).exists()},
                "counts": {"slides": len(pres.get("slides", [])), "thumbnails": shots, "pictures": len(pictures),
                           "google_fonts": len(fonts)},
                "fonts_missing": sorted({m["font"] for m in missing})}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    return manifest


def zip_folder(folder: Path, dest: Path) -> Path:
    """`folder` as one .zip (its contents at the root): one file to hand a harness."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder).as_posix())
    return dest


@contextmanager
def _environ(**values):
    """Environment variables set (None: unset) for the block, and put back after."""
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _images(node):
    if isinstance(node, dict):
        if node.get("kind") == "image":
            yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _images(v)
    elif isinstance(node, list):
        for v in node:
            yield from _images(v)
