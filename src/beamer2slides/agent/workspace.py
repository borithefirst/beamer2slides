"""Where an agent's files are, and the promise that it cannot reach outside them.

The library needs a real filesystem - LaTeX compiles files, PDFium opens files, python-pptx
reads pictures - so this is not a virtual filesystem and pretending otherwise would only move
the temporary directory somewhere less honest. What a harness actually differs on is narrower:
what a path *means* when an agent writes one, where output is allowed to land, and how a
produced file is named when it is handed back. That is what a `Workspace` decides.

Every path an agent gives or gets is a **ref**: a relative, forward-slashed path under the
workspace root. `resolve` turns one into an absolute path and refuses anything that climbs out;
`ref` turns a path back. A harness that keeps its files somewhere else stages them in and out
around a journey (`stage`, `Artifact.ref`) rather than teaching the library a new kind of path.

`out_dir` is here for a second reason: `paths.out_root()` answers `out/` *in the checkout* when
the library runs from one and `out/` in the process's current folder otherwise, which makes the
same call write to two different places depending on how beamer2slides was installed. A
workspace answers the same question the same way everywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from .types import Refused


@runtime_checkable
class Workspace(Protocol):
    """The filesystem seam. `root` is the only directory a journey may write into."""

    root: Path

    def resolve(self, ref: str, *, write: bool = False) -> Path: ...
    def ref(self, path: Path | str) -> str: ...
    def out_dir(self, name: str) -> Path: ...
    def exists(self, ref: str) -> bool: ...
    def glob(self, pattern: str) -> list[str]: ...


class LocalWorkspace:
    """A plain directory on this machine. The default, and what a local harness wants.

    Reads are confined to `root` plus whatever `readable` names, writes to `root` alone. A
    source tree an agent edits (pull, label, adopt) is therefore expected to live inside the
    workspace, which is the same boundary a harness's own file tools draw around a project.
    """

    def __init__(self, root: Path | str, readable: tuple[Path, ...] = ()) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.readable = tuple(Path(p).resolve() for p in readable)

    # -- paths --------------------------------------------------------------------------

    def resolve(self, ref: str, *, write: bool = False) -> Path:
        """An absolute path for `ref`, or `Refused("outside_workspace")`."""
        raw = Path(ref)
        path = (raw if raw.is_absolute() else self.root / raw).resolve()
        if _inside(path, self.root):
            return path
        if not write:
            for folder in self.readable:
                if _inside(path, folder):
                    return path
        what = "written" if write else "read"
        raise Refused("outside_workspace",
                      f"{ref} is outside the workspace and cannot be {what}. "
                      f"The workspace is {self.root}.", ref=ref, root=str(self.root))

    def ref(self, path: Path | str) -> str:
        """The name a journey hands back for a file it made: relative to the root, or absolute.

        A file outside the root keeps its full path rather than growing a row of `..`, which
        no agent can do anything useful with.
        """
        p = Path(path).resolve()
        if _inside(p, self.root):
            return str(PurePosixPath(*p.relative_to(self.root).parts)) or "."
        return str(p)

    def out_dir(self, name: str) -> Path:
        """`out/<name>/` under the root: the folder a conversion keeps its state in."""
        folder = self.root / "out" / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # -- files --------------------------------------------------------------------------

    def exists(self, ref: str) -> bool:
        try:
            return self.resolve(ref).exists()
        except Refused:
            return False

    def glob(self, pattern: str) -> list[str]:
        return sorted(self.ref(p) for p in self.root.glob(pattern))

    def read_bytes(self, ref: str) -> bytes:
        return self.resolve(ref).read_bytes()

    def read_text(self, ref: str) -> str:
        return self.resolve(ref).read_text(encoding="utf-8")

    def write_bytes(self, ref: str, data: bytes) -> str:
        path = self.resolve(ref, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.ref(path)

    def write_text(self, ref: str, text: str) -> str:
        return self.write_bytes(ref, text.encode("utf-8"))

    def stage(self, path: Path | str, into: str = "inbox") -> str:
        """Copy a file from outside into the workspace and return its ref.

        How a harness brings in a PDF the user named somewhere else, without widening what a
        journey is allowed to touch.
        """
        src = Path(path).resolve()
        if not src.is_file():
            raise Refused("not_found", f"{path} is not a file.")
        dest = self.root / into / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src != dest.resolve():
            shutil.copy2(src, dest)
        return self.ref(dest)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents
