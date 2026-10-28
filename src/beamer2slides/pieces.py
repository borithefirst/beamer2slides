"""One compile on several cores: the frames of a deck cut into pieces, each piece compiled by an
engine of its own, the pages joined (docs/compile-latency.md "A compile in pieces").

Every engine reads the whole source. Before its piece it skips frames as `\\includeonlyframes`
does, after it likewise, and writes nothing of theirs to the .aux; its own frames it typesets as a
whole compile would. What a skipped frame would have left behind - its pages counted, its section
numbered - is put back at its end: every compile records, at the end of each labelled frame, the
count registers that differ from where the document began (`\\btwos@ckpt{label}{i=v;...}` in the
.aux), and an engine skipping that frame sets them as the last compile recorded. Allocation
pointers are left alone: they only grow, and a piece allocates afresh what it skipped.

The registers where the document began are recorded too (`\\btwos@base`), so that a checkpoint's
absent register is known. A piece's own part of the .aux is marked (`\\btwos@own{1}` where it starts, `\\btwos@own{0}` where
it ends); `merged` puts the parts end to end and writes the contents files (.toc, .nav, ...) from
them as LaTeX would at `\\end{document}`. The cuts are labelled frames recorded once. The result is
a fixpoint like any LaTeX pass: once the merged .aux is what the pieces read, every piece began
where the one before it ended, and the pages are a whole compile's. A piece that fails, a marker
missing, a page count that does not add up or an .aux line not known here sends the compile back
to a whole one (`Workspace.compile`).

The engine is told its piece by the file named in $B2S_PIECE_SPEC: the label after which it starts
and the one after which it stops, a line each (empty: the document's start or end). It is read at
`\\begin{document}`, so an engine waiting on the preamble (`inverse.WARM_LUA`) takes its piece
when it is let go."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

LUA_ENV = "B2S_PIECES_LUA"
SPEC_ENV = "B2S_PIECE_SPEC"
# loaded before the preamble, like `inverse.NO_GLYPH_STREAMS`
LOAD = r"\directlua{local f = os.getenv('B2S_PIECES_LUA') if f then dofile(f) end}"

PIECES_LUA = r"""
btwos = btwos or {}
local B = btwos
local ATL = luatexbase.registernumber("catcodetable@atletter")
B.known = {}
B.owned = true
-- \count10 is the last count register allocated; the scratch ones are below 256
local function top() return math.max(tex.count[10], 255) end
-- LaTeX's allocation pointers: \count10-\count22 and the extended ones of ltalloc
local function alloc(i) return (i >= 10 and i <= 22) or (i >= 256 and i <= 263) end
local function counts()
  local c = {}
  for i = 0, top() do c[i] = tex.count[i] end
  return c
end
function B.encoded()
  local out = {}
  for i = 0, top() do
    local v = tex.count[i]
    if not alloc(i) and v ~= (B.base[i] or 0) then out[#out + 1] = i .. "=" .. v end
  end
  return table.concat(out, ";")
end
function B.know(label, data) B.known[label] = data end
local function restore(data)
  local want = {}
  for i, v in data:gmatch("(%d+)=(-?%d+)") do want[tonumber(i)] = tonumber(v) end
  for i = 0, top() do
    local v = want[i] or B.base[i] or 0
    if not alloc(i) and tex.count[i] ~= v then tex.setcount("global", i, v) end
  end
end
local function own(on, mark)
  B.owned = on
  if on then
    tex.print(ATL, "\\global\\let\\@auxout\\@mainaux\\global\\let\\beamer@framerestriction\\@undefined"
      .. "\\immediate\\write\\@mainaux{\\string\\btwos@own{1}}")
  else
    tex.print(ATL, "\\ifx\\btwos@voidopen\\relax\\else\\immediate\\openout\\btwos@void=\\jobname.btwos"
      .. "\\global\\let\\btwos@voidopen\\relax\\fi\\global\\let\\@auxout\\btwos@void"
      .. "\\gdef\\beamer@framerestriction{\\btwos@none,}"
      .. (mark and "\\immediate\\write\\@mainaux{\\string\\btwos@own{0}}" or ""))
  end
end
-- what the preamble left to write to the .aux at the first shipout is the first piece's
local function drop_pending_aux()
  local aux = token.create("@mainaux").mode
  local write = node.subtype("write")
  local whatsit = node.id("whatsit")
  for _, which in ipairs({"page_head", "contrib_head"}) do
    local head = tex.lists[which]
    local n = head
    while n do
      local nxt = n.next
      if n.id == whatsit and n.subtype == write and n.stream == aux then head = node.remove(head, n) end
      n = nxt
    end
    tex.lists[which] = head
  end
end
local function spec()
  local path = os.getenv("B2S_PIECE_SPEC")
  local f = path and io.open(path, "rb")
  if not f then return end
  local on, off = f:read("*l"), f:read("*l")
  f:close()
  B.on_after = (on and on ~= "") and on or nil
  B.off_after = (off and off ~= "") and off or nil
end
function B.begin()
  spec()
  B.base = counts()
  if not B.on_after then
    local out = {}
    for i = 0, top() do
      if not alloc(i) and B.base[i] ~= 0 then out[#out + 1] = i .. "=" .. B.base[i] end
    end
    tex.print(ATL, "\\immediate\\write\\@mainaux{\\string\\btwos@base{" .. table.concat(out, ";") .. "}}")
  end
  if B.on_after then
    drop_pending_aux()
    own(false, false)
  end
end
function B.frameend(label)
  -- a fragile frame ends twice, the first time inside its own mode
  if label == "" or tex.count["beamer@modecount"] ~= 0 then return end
  if B.owned then
    tex.print(ATL, "\\immediate\\write\\@mainaux{\\string\\btwos@ckpt{\\beamer@againname}{" .. B.encoded() .. "}}")
    if label == B.off_after then own(false, true) end
  else
    local data = B.known[label]
    if data then restore(data) end
    if label == B.on_after then own(true, true) end
  end
end
tex.print(ATL, [[\newwrite\btwos@void]]
  .. [[\def\btwos@ckpt#1#2{\directlua{btwos.know("\luaescapestring{#1}", "\luaescapestring{#2}")}}]]
  .. [[\def\btwos@own#1{}\def\btwos@base#1{}]]
  .. [[\def\btwos@frameend{\directlua{btwos.frameend("\luaescapestring{\beamer@againname}")}}]]
  .. [[\AddToHook{begindocument/before}{\@ifundefined{beamer@reseteecodes}{}]]
  .. [[{\g@addto@macro\beamer@reseteecodes{\btwos@frameend}}}]]
  .. [[\AddToHook{begindocument/end}{\directlua{btwos.begin()}}]])
"""

ON = "\\btwos@own{1}"
OFF = "\\btwos@own{0}"
CHECKPOINT = re.compile(r"\\btwos@ckpt\{([^{}]*)\}\{([^{}]*)\}")
BASE = re.compile(r"\\btwos@base\{([^{}]*)\}")
REGISTER = re.compile(r"(\d+)=(-?\d+)")
# a label passed through the spec file and compared in Lua as it is
CUTTABLE = re.compile(r"[A-Za-z0-9:_.+/-]+")
PAGE = re.compile(r"(?:^|;)0=(-?\d+)")
# .aux lines whose only work at \end{document} is `\@writefile` and `\babel@aux`'s (what `merged`
# writes) or none; any other line sends the compile back to a whole one
KNOWN = re.compile(r"\\(?:relax|providecommand|newlabel|@writefile|babel@aux|gdef|HyPL@Entry|pgfsyspdfmark"
                   r"|savepicturepage|savepointas|@nameuse\{bbl@beforestart\}|btwos@ckpt|btwos@own|btwos@base|bibcite"
                   r"|bibstyle|bibdata|citation|zref@newlabel|hyper@newdestlabel|HyField@AuxAddToFields"
                   r"|HyField@AuxAddToCoFields|select@language)(?![A-Za-z@])")
WRITEFILE = re.compile(r"\\@writefile\{([A-Za-z]+)\}\{(.*)\}\s*")
BABEL_AUX = re.compile(r"\\babel@aux\{([^{}]*)\}\{(.*)\}\s*")
# what babel's \babel@aux writes to (\BabelContentsFiles)
BABEL_CONTENTS = ("toc", "lof", "lot")
# files a piece writes that `merged` makes for the whole: the .aux and the contents files
CONTENTS = ("nav", "toc", "snm", "lof", "lot")
MAX_PIECES = 4


def piece_count() -> int:
    """How many pieces a compile is cut into: one per core, at most `MAX_PIECES` (each piece reads
    the whole preamble and walks every frame it skips); $B2S_COMPILE_PIECES overrides, 0 or 1
    compiling whole."""
    given = os.environ.get("B2S_COMPILE_PIECES", "").strip()
    if given.isdigit():
        return int(given)
    affinity = getattr(os, "sched_getaffinity", None)
    cores = len(affinity(0)) if affinity is not None else (os.cpu_count() or 1)
    return min(cores, MAX_PIECES)


@dataclass(frozen=True, kw_only=True)
class Cut:
    """Where piece `index` starts and stops: after these frames' labels (None: the document's
    start, its end)."""
    index: int
    on_after: str | None
    off_after: str | None

    def spec(self) -> str:
        return f"{self.on_after or ''}\n{self.off_after or ''}\n"


def cuts(aux: str, pieces: int) -> list[Cut]:
    """The pieces of the document the .aux of its last compile describes, about as many pages
    each; [] when it records too few frames to cut it in two (no labels, no beamer, a compile
    that never loaded `PIECES_LUA`)."""
    found = CHECKPOINT.findall(aux)
    seen: dict[str, int] = {}
    for label, _ in found:
        seen[label] = seen.get(label, 0) + 1
    usable = [(label, page_of(data)) for label, data in found if seen[label] == 1 and CUTTABLE.fullmatch(label)]
    if pieces < 2 or len(usable) < pieces:
        return []
    total = usable[-1][1]
    chosen: list[str] = []
    for k in range(1, pieces):
        want = total * k / pieces
        # the frame ending nearest the share, after the cut before it and leaving one for each after
        lo = usable.index(next(u for u in usable if u[0] == chosen[-1])) + 1 if chosen else 0
        hi = len(usable) - (pieces - k)
        if lo >= hi:
            return []
        best = min(range(lo, hi), key=lambda i: abs(usable[i][1] - want))
        chosen.append(usable[best][0])
    bounds: list[str | None] = [None, *chosen, None]
    return [Cut(index=i, on_after=bounds[i], off_after=bounds[i + 1]) for i in range(pieces)]


def page_of(data: str) -> int:
    """The page register in a checkpoint (absent: where the document began, 1)."""
    m = PAGE.search(data)
    return int(m.group(1)) if m else 1


@dataclass(frozen=True, kw_only=True)
class Merged:
    aux: str
    contents: dict[str, str]   # extension -> the file's text
    pages: int | None          # \@abspage@last as the merged .aux says it


def merged(auxes: Sequence[str], opened: Sequence[str]) -> Merged | str:
    """The .aux and contents files of the whole from each piece's .aux (in order), or why not.
    `opened`: the contents files (`CONTENTS`) any piece wrote, which a whole compile would have
    opened."""
    n = len(auxes)
    parts: list[str] = []
    for i, text in enumerate(auxes):
        lines = text.splitlines(keepends=True)
        ons = [k for k, line in enumerate(lines) if line.rstrip() == ON]
        offs = [k for k, line in enumerate(lines) if line.rstrip() == OFF]
        if len(ons) != (1 if i else 0) or len(offs) != (1 if i < n - 1 else 0):
            return f"piece {i}: its own part is not marked"
        start = ons[0] + 1 if ons else 0
        end = offs[0] if offs else len(lines)
        if end < start:
            return f"piece {i}: its own part ends before it starts"
        parts += lines[start:end]
    contents: dict[str, list[str]] = {ext: [] for ext in opened}
    pages: int | None = None
    for line in parts:
        if not KNOWN.match(line):
            return f"an .aux line this split does not know: {line.strip()[:60]}"
        m = WRITEFILE.fullmatch(line)
        if m:
            if m.group(1) in contents:
                contents[m.group(1)].append(m.group(2) + "\n")
            continue
        m = BABEL_AUX.fullmatch(line)
        if m:
            for ext in BABEL_CONTENTS:
                if ext in contents:
                    contents[ext].append(f"\\babel@toc {{{m.group(1)}}}{{{m.group(2)}}}\\relax \n")
            continue
        last = re.fullmatch(r"\\gdef\s*\\@abspage@last\{(\d+)\}\s*", line)
        if last:
            pages = int(last.group(1))
    return Merged(aux="".join(parts), contents={ext: "".join(lines) for ext, lines in contents.items()},
                  pages=pages)


def carried(aux: str, given: str, plan: Sequence[Cut]) -> str:
    """The merged .aux of a pass that has not settled, made a likelier start for the next one.
    A piece began where the .aux it read (`given`) said its first frame's predecessor ended; the
    piece before it, this pass, may have ended elsewhere (a page came or went), and then every
    checkpoint the piece wrote is off by as much. Each piece's checkpoints, in order, are moved by
    the difference, a register only up to where the piece set it back (a counter reset). A guess
    for the next pass to read, never a result: a pass settles only on reading the .aux it writes."""
    base_found = BASE.search(aux)
    if base_found is None:
        return aux
    base = registers(base_found.group(1))
    # the registers the given .aux's checkpoints were written against
    given_base_found = BASE.search(given)
    given_base = registers(given_base_found.group(1)) if given_base_found is not None else base
    known = {label: data for label, data in CHECKPOINT.findall(given)}
    lines = aux.splitlines(keepends=True)
    at: dict[str, int] = {}
    for k, line in enumerate(lines):
        m = CHECKPOINT.match(line)
        if m:
            at.setdefault(m.group(1), k)
    for cut in plan:
        if cut.on_after is None or cut.on_after not in at or cut.on_after not in known:
            continue
        start = at[cut.on_after]
        end = at.get(cut.off_after, len(lines)) if cut.off_after is not None else len(lines)
        found = CHECKPOINT.match(lines[start])
        if found is None:
            continue
        now, began = registers(found.group(2)), registers(known[cut.on_after])
        keys = set(now) | set(began)
        delta = {r: now.get(r, base.get(r, 0)) - began.get(r, given_base.get(r, 0)) for r in keys}
        moving = {r: d for r, d in delta.items() if d}
        before = {r: began.get(r, given_base.get(r, 0)) for r in moving}
        for k in range(start + 1, min(end + 1, len(lines))):
            m = CHECKPOINT.match(lines[k])
            if m is None or not moving:
                continue
            values = registers(m.group(2))
            for r in list(moving):
                value = values.get(r, base.get(r, 0))
                if value < before[r]:
                    del moving[r]   # set back: from here on not where the piece began
                    continue
                before[r] = value
                values[r] = value + moving[r]
            data = ";".join(f"{r}={v}" for r, v in sorted(values.items()) if v != base.get(r, 0))
            lines[k] = f"\\btwos@ckpt{{{m.group(1)}}}{{{data}}}" + lines[k][m.end():]
    return "".join(lines)


def registers(data: str) -> dict[int, int]:
    """A checkpoint's registers (`i=v;...`)."""
    return {int(i): int(v) for i, v in REGISTER.findall(data)}


def verbatim(name: str, job: str) -> bool:
    """Whether `name` is a scratch file of beamer's fragile frames (`job.vrb`, `job.2.vrb`): written
    and read back in the same pass, so a piece's own are as a whole compile's."""
    return re.fullmatch(re.escape(job) + r"(?:\.\d+)?\.vrb", name) is not None


def drop_marks(aux: Path) -> None:
    """Take this module's lines out of an .aux, for a compile that does not load `PIECES_LUA`."""
    if not aux.is_file():
        return
    text = aux.read_text(encoding="utf-8", errors="replace")
    if "\\btwos@" not in text:
        return
    kept = [line for line in text.splitlines(keepends=True) if not line.startswith("\\btwos@")]
    aux.write_text("".join(kept), encoding="utf-8")
