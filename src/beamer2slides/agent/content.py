"""Content in and content out, for a harness that has no filesystem to name.

`workspace.py` says the honest thing: the library needs a real filesystem, because LaTeX
compiles files, PDFium opens files and python-pptx reads pictures. None of that changes here.
What a harness without a filesystem actually needs is narrower than a virtual one: it has the
bytes of a PDF in hand and wants to pass them in without writing them anywhere it can name, and
it wants the HTML file a journey produced *back* rather than a path it cannot open. So the disk
stays and the **path leaves the interface**: inline content comes in, is written into the
workspace, and the journey underneath sees an ordinary file the way it always has.

Three things a value may be, and the order matters because only the first is ambiguous:

* **A ref** - today's form, and still the default. Any plain string is one, which is what keeps
  `deck_sync(deck="https://docs.google.com/presentation/d/...")` working: a Drive URL is an
  identifier the journey resolves itself, not a file to fetch, and nothing here may touch it.
* **A `data:` URI** - `data:application/pdf;base64,JVBERi0...`. The one string form, and
  unambiguous: no ref and no Drive URL starts with `data:`.
* **A content dict** - `{"name": "talk.pdf", "base64": "..."}`, `{"text": "..."}`,
  `{"bytes": b"..."}` or `{"url": "https://..."}`. The explicit form, and the one a harness
  should prefer, because it can say what the file is called - a name the model will see again
  in every artifact and every refusal.

A `url` is fetched by **the harness's own fetcher** (`AgentContext.fetch`), never by this
module. A library that grows its own `urlopen` grows an egress path from a model's argument to
an arbitrary host, inside a call the harness thought was local; a harness that wants URL inputs
already has a client it trusts, with its own allow-list and its own proxy, and supplying it is
one line. With no fetcher, a `url` is refused by name rather than quietly fetched.

Going out, `deliver` puts the artifact's own content into the `Artifact` - text where it is
text, base64 where it is not - under a per-artifact cap and a budget for the call, because a
conversion produces a background PNG per slide and no model wants thirty of them. Over the cap
an artifact still carries its size and its digest, and the harness reads it with
`workspace.read_bytes(ref)`; the ref is not a path it has to understand, it is a name it hands
back.
"""

from __future__ import annotations

import base64 as _b64
import binascii
import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote_to_bytes

from .types import Artifact, Refused, Result
from .workspace import LocalWorkspace

__all__ = ["MemoryWorkspace", "take_in", "deliver", "content_bytes", "is_content",
           "INLINE_NOTE", "TEXT_KINDS"]

#: What a harness publishing these tools should tell the model about file arguments. Short on
#: purpose: it is prepended to nothing automatically, and lives in INSTRUCTIONS.md as prose.
INLINE_NOTE = (
    "Anywhere a tool takes a workspace ref for a file, it also takes the file itself: a "
    "`data:` URI, or an object like {\"name\": \"talk.pdf\", \"base64\": \"...\"} or "
    "{\"name\": \"notes.html\", \"text\": \"...\"}. A plain string is always a ref or a "
    "Drive URL and is never fetched.")

#: Kinds whose files are text, and so come back as `text` rather than base64.
TEXT_KINDS = frozenset({"json", "tex", "html", "report", "md", "txt", "csv", "svg", "patch"})

#: Per-artifact and per-call caps for inline delivery. A slide background is ~100 KB and a
#: deck.json a few hundred; the budget is what stops a 30-slide conversion from handing a model
#: its own weight in PNG.
INLINE_LIMIT = 256 * 1024
INLINE_BUDGET = 4 * 1024 * 1024

_DATA_URI = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<params>(?:;[\w.+-]+=?[\w.+-]*)*),",
                       re.IGNORECASE)
#: Extensions for the mime types a harness actually hands in. A name it gives beats all of this.
_SUFFIX = {
    "application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt",
    "application/json": ".json", "text/x-tex": ".tex", "application/x-tex": ".tex",
    "image/png": ".png", "image/jpeg": ".jpg", "text/markdown": ".md",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
#: Where inline content lands. Named so an agent reading a ref back can tell what it is.
INBOX = "inbox"


# -- a workspace nobody outside this process can name ------------------------------------------

class MemoryWorkspace(LocalWorkspace):
    """A workspace whose root is a private temporary directory, removed when it is closed.

    For the harness that has no filesystem of its own: it never names a path, it hands content
    in and takes content out, and the directory underneath exists only because the library needs
    one. `put` and `get` are the whole interface; `close` removes it, and it is a context
    manager, so a run that ends leaves nothing behind.

    It is not a sandbox. The directory is an ordinary one with ordinary permissions and the
    journeys running in it are ordinary code; what it gives is a boundary with no path in it and
    a lifetime that ends, not isolation from the process it runs in.
    """

    #: Nothing written here outlives the call (`AgentContext.ephemeral`).
    ephemeral = True

    def __init__(self, prefix: str = "b2s-agent-") -> None:
        self._temp = Path(tempfile.mkdtemp(prefix=prefix))
        super().__init__(self._temp)

    def put(self, name: str, data: bytes | str) -> str:
        """Write one file in and return the ref that names it from here on."""
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return self.write_bytes(_safe_name(name), raw)

    def get(self, ref: str) -> bytes:
        return self.read_bytes(ref)

    def close(self) -> None:
        shutil.rmtree(self._temp, ignore_errors=True)

    def __enter__(self) -> "MemoryWorkspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# -- coming in ---------------------------------------------------------------------------------

def is_content(value: Any) -> bool:
    """Whether `take_in` would materialise this value rather than pass it through.

    A plain string is content only as a `data:` URI. Everything else that is a string - a ref, a
    Drive id, a presentation URL - is an identifier the journey resolves itself.
    """
    if isinstance(value, str):
        return bool(_DATA_URI.match(value))
    return isinstance(value, Mapping) and bool(
        {"base64", "text", "bytes", "url", "content"} & set(value))


def take_in(ws: Any, arguments: Mapping[str, Any],
            fetch: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """Materialise every inline argument into the workspace; return the arguments with refs.

    Idempotent, and cheap when there is nothing to do: a call whose arguments are all plain
    strings walks the dict once and returns a copy. Runs *before* the schema check, so a
    content dict never has to be a publishable parameter type - by the time a tool's schema
    sees the argument it is the string ref the schema says it is.
    """
    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        out[key] = _one(ws, key, value, fetch) if is_content(value) else value
    return out


def _one(ws: Any, key: str, value: Any, fetch: Callable[[str], bytes] | None) -> str:
    name, raw = content_bytes(key, value, fetch)
    return ws.write_bytes(f"{INBOX}/{_safe_name(name)}", raw)


def content_bytes(key: str, value: Any,
                  fetch: Callable[[str], bytes] | None = None) -> tuple[str, bytes]:
    """One inline value as `(filename, bytes)`. Refuses with `bad_request` and says what it wanted."""
    if isinstance(value, str):
        return _from_data_uri(key, value)
    if not isinstance(value, Mapping):                          # `is_content` already said no
        raise Refused("bad_request", f"{key} is not inline content.", parameter=key)

    name = str(value.get("name") or "").strip()
    if "url" in value and not {"base64", "text", "bytes", "content"} & set(value):
        return _from_url(key, str(value["url"]), name, fetch)

    if "base64" in value or "content" in value:
        encoded = value.get("base64", value.get("content"))
        if not isinstance(encoded, str):
            raise Refused("bad_request",
                          f"{key}.base64 should be a base64 string, not "
                          f"{type(encoded).__name__}.", parameter=key)
        try:
            raw = _b64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Refused("bad_request",
                          f"{key}.base64 is not valid base64 ({exc}). Send the file's bytes "
                          f"base64-encoded, or send text under `text`.", parameter=key) from None
    elif "text" in value:
        text = value["text"]
        if not isinstance(text, str):
            raise Refused("bad_request",
                          f"{key}.text should be a string, not {type(text).__name__}. Binary "
                          f"content goes under `base64`.", parameter=key)
        raw = text.encode("utf-8")
    elif "bytes" in value:
        given = value["bytes"]
        if not isinstance(given, (bytes, bytearray, memoryview)):
            raise Refused("bad_request",
                          f"{key}.bytes should be bytes; over JSON use `base64`.", parameter=key)
        raw = bytes(given)
    else:                                                       # unreachable via `is_content`
        raise Refused("bad_request", f"{key} carries no content.", parameter=key)

    return (name or _named(key, value.get("mime") or value.get("type"), raw)), raw


def _from_data_uri(key: str, uri: str) -> tuple[str, bytes]:
    head = _DATA_URI.match(uri)
    if head is None:                                            # `is_content` already said yes
        raise Refused("bad_request", f"{key} is not a data: URI.", parameter=key)
    body = uri[head.end():]
    mime = (head.group("mime") or "text/plain").lower()
    if ";base64" in (head.group("params") or "").lower():
        try:
            raw = _b64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Refused("bad_request",
                          f"{key} is a base64 data: URI whose payload will not decode "
                          f"({exc}).", parameter=key) from None
    else:
        raw = unquote_to_bytes(body)
    return _named(key, mime, raw), raw


def _from_url(key: str, url: str, name: str,
              fetch: Callable[[str], bytes] | None) -> tuple[str, bytes]:
    if fetch is None:
        raise Refused("bad_request",
                      f"{key} was given a url ({url}) and this context has no fetcher, so "
                      f"nothing here will open it. Pass the bytes as `base64`, or give the "
                      f"context a `fetch` that knows which hosts it is allowed to reach.",
                      parameter=key, url=url)
    try:
        raw = fetch(url)
    except Refused:
        raise
    except Exception as exc:
        raise Refused("not_found", f"{key}: {url} could not be fetched "
                                   f"({type(exc).__name__}: {exc}).",
                      parameter=key, url=url) from None
    if not isinstance(raw, (bytes, bytearray)):
        raise Refused("failed", f"the context's fetch returned {type(raw).__name__}, not bytes.",
                      parameter=key)
    tail = Path(url.split("?", 1)[0].split("#", 1)[0]).name
    return (name or tail or _named(key, None, bytes(raw))), bytes(raw)


def _named(key: str, mime: str | None, raw: bytes) -> str:
    """A filename for content that came with none: the argument's name plus a likely suffix."""
    suffix = _SUFFIX.get((mime or "").split(";", 1)[0].strip().lower()) or _sniff(raw)
    return f"{key}{suffix}"


def _sniff(raw: bytes) -> str:
    if raw[:5] == b"%PDF-":
        return ".pdf"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:2] == b"PK":                                        # .pptx, .docx, any zip
        return ".pptx"
    head = raw[:512].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return ".html"
    if head[:1] in (b"{", b"["):
        return ".json"
    if b"\\documentclass" in raw[:2048] or b"\\begin{document}" in raw[:2048]:
        return ".tex"
    return ".bin"


def _safe_name(name: str) -> str:
    """A filename an argument may not use to climb: no separators, no dots of its own.

    `Workspace.resolve` refuses anything outside the root anyway; this is so that a name a model
    invented cannot land the file somewhere surprising *inside* it either.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(str(name)).name).strip("-.")
    return cleaned or "content.bin"


# -- going out ---------------------------------------------------------------------------------

def deliver(ws: Any, result: Result, *, limit: int = INLINE_LIMIT,
            budget: int = INLINE_BUDGET) -> Result:
    """Put each artifact's own content into it, so a harness never has to open a file.

    Text arrives as `text`, everything else as `base64`; every artifact carries its size and
    sha256 whether or not its content fitted. What did not fit says so (`truncated`) and is read
    with `workspace.read_bytes(ref)` - the ref being a name the harness hands back, not a path
    it has to resolve. Folders are named and never inlined.
    """
    spent = 0
    for art in result.artifacts:
        try:
            path = ws.resolve(art.ref)
        except Exception:                                       # a ref outside, or gone
            continue
        if path.is_dir() or not path.exists():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        art.bytes = len(raw)
        art.sha256 = hashlib.sha256(raw).hexdigest()
        if len(raw) > limit or spent + len(raw) > budget:
            art.truncated = True
            continue
        text = _as_text(art.kind, raw)
        if text is None:
            art.base64 = _b64.b64encode(raw).decode("ascii")
        else:
            art.text = text
        spent += len(raw)
    return result


def _as_text(kind: str, raw: bytes) -> str | None:
    """The content as text, or None when it is not text after all.

    The kind is a hint, not the answer: a `report` is text and a `pdf` is not, but an artifact
    whose kind this layer does not know is decided by the bytes - which is also what catches a
    `json` that turned out to be something else.
    """
    if b"\x00" in raw[:4096]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if kind in TEXT_KINDS or _mostly_printable(text) else None


def _mostly_printable(text: str) -> bool:
    sample = text[:4096]
    if not sample:
        return True
    odd = sum(1 for ch in sample if not ch.isprintable() and ch not in "\r\n\t")
    return odd / len(sample) < 0.01


def inline_artifact(ws: Any, ref: str, kind: str = "", description: str = "",
                    *, limit: int = INLINE_LIMIT) -> Artifact:
    """One artifact with its content in it: what a harness calls to fetch what did not fit."""
    art = Artifact(ref=ref, kind=kind, description=description)
    deliver(ws, Result(tool="", artifacts=[art]), limit=limit, budget=limit)
    return art
