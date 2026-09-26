# beamer2slides as tools for an agent

`src/beamer2slides/agent/` is the library's journeys - convert, sync, pull, adopt, label, the
Google Docs loop - offered to something that is not a person at a terminal. This document is
why it is shaped the way it is. The guide the *agent* reads is
[`src/beamer2slides/agent/INSTRUCTIONS.md`](../src/beamer2slides/agent/INSTRUCTIONS.md); this
one is for whoever wires it into a harness.

## What was wrong with the command line

Nothing, for a person. `python -m beamer2slides convert talk.pdf` prints a line per slide and a
deck URL at the end, and exits non-zero when it refuses. Three things in that sentence do not
survive the trip into an agent harness:

* **Results arrive as prose.** `cmd_classify` returns `(pdf, raw, deck)` but prints its report;
  `cmd_convert` returns `None` and prints the URL. An agent would have to parse English to learn
  what happened, and would parse it differently after every wording change.
* **Refusals arrive as `SystemExit`.** A harness running the library in-process takes the exit
  through the heart. The refusals are the *good* part of this library - `guard.check_rebuild`
  exists so that a rebuild can never destroy a deck someone edited - and they were reachable
  only by catching a process death and reading its message.
* **Authentication waits for a browser.** `google_auth.credentials()` calls
  `InstalledAppFlow.run_local_server(open_browser=True)`, which binds a port, opens a browser
  and blocks the calling thread until a human clicks. In a harness that is not an error; it is
  a hang, with nothing in the transcript to say why.

The agent layer fixes exactly those three, and changes nothing else. Underneath, every tool
calls the same functions the CLI calls.

## One tool per journey, not one gateway

The obvious alternative was a single `beamer2slides(action=..., args={...})` entry point: one
thing to register, a small context footprint. It was not taken.

A model chooses a tool by reading its schema. Behind a gateway there is one schema, and the
action names and their arguments live in prose the model has to recall rather than in a
structure it can read; tool-choice accuracy drops, and the failures are silent - a plausible
action name that does not exist, an argument spelled the way the docs spelled it two paragraphs
earlier. Eleven schemas cost more context and buy a model that cannot misremember what
`deck_sync` takes, because it is looking at it.

The eleven are the journeys a person would name, not the flags: `b2s_status`, `deck_inspect`,
`deck_convert`, `deck_sync`, `deck_pull`, `deck_adopt`, `tex_label`, `tex_converge`,
`doc_push`, `doc_sync`, `doc_adopt`. Each maps to one CLI command and reuses its entry point.

### One of them also comes in halves

`deck_convert` is the only journey that does substantial local work *and* writes to Google, and
a caller may not have both in one place: a sandbox that compiles and classifies, a service that
holds the account. So the same body is also published cut in two - `deck_prepare`
(`needs=(READS, WRITES)`, the permission vocabulary's `LOCAL_ONLY`) and `deck_upload`
(`needs=(READS, WRITES, WRITES_GOOGLE)`) - and `deck_convert` is the two in sequence, sharing
`_prepare` and `_upload` with them rather than restating either. There is no third code path,
which is the point: the refusal codes, the artifacts and above all the rebuild guard are the
same ones, instead of a caller reaching past the layer into `classify`, `render` and `emit`.

What crosses between them is **the folder and nothing else**: `deck.json`, `backgrounds/`,
`figures/`, and `prepared.json`, which carries what `_upload` would otherwise have re-read from
the PDF - the title, the overlay mode the base must record, and the source's name and digest.
Those last two are all anything ever wanted of the file: `guard.check_rebuild` compares the
**name** (to catch a folder whose deck came from another PDF) and `snapshot.source_info` stores
a sha1, so `source_info` takes those two facts already measured as readily as it takes a path.
The PDF's *bytes* are read by exactly one code path - `emit.fallback_pictures`, the retry that
crops a refused element's region out of the page when the API rejects it - so `deck_upload`
takes an optional `pdf` for a caller that does have the file, reports
`can_crop_refused_elements` either way, and `emit` says plainly which file it wanted rather
than failing inside the retry. A folder written before this split has no `prepared.json` and is
not refused: `deck.json` carries the source block classify copied out of the PDF, so only the
digest is missing, which costs a later interrupted sync one conservative branch and is said out
loud.

`deck_convert` stays first in `ORDER` and in a model's tool list: the halves are for a caller
who knows they are that caller, and a model reading top to bottom should meet the whole journey
before its parts.

## The three seams a harness plugs into

Everything a harness differs on is in `AgentContext`, and nothing in the agent layer reads the
environment or the filesystem behind its back.

### `Workspace` - where the files are

This is deliberately **not** a virtual filesystem. The library needs real paths: LaTeX compiles
files, PDFium opens files, python-pptx reads pictures. Pretending otherwise would only move the
temporary directory somewhere less honest. What a harness actually differs on is narrower - what
a path *means* when an agent writes one, where output may land, and how a produced file is named
when it is handed back - and that is what `Workspace` decides.

Every path an agent gives or gets is a **ref**: relative, forward-slashed, under the workspace
root. `resolve(ref, write=)` refuses anything that climbs out; `ref(path)` names a file on the
way back; `stage(path)` brings an outside file in. `LocalWorkspace` is the one implementation
that ships. A harness whose files live elsewhere stages in and out around a journey rather than
teaching the library a new kind of path.

`out_dir(name)` is here for a second reason. `paths.out_root()` answers `out/` *in the checkout*
when the library runs from one and `out/` in the process's current folder otherwise - the same
call writing to two different places depending on how beamer2slides was installed. A workspace
answers it the same way everywhere.

#### A harness with no filesystem to name

The paragraph above stays true and is not the whole story. A harness can have no filesystem of
its own - a sandboxed service holding a PDF in memory, with nowhere to put one and nothing to
read one back from. The disk it needs does not disappear; it **leaves the interface**
(`agent/content.py`):

* **In.** Any argument that is a workspace ref also takes the file itself: a `data:` URI, or
  `{"name": "talk.pdf", "base64": "…"}` / `{"text": "…"}`. `take_in` writes it into the
  workspace's `inbox/` and replaces it with the ref, so the journey underneath sees the ordinary
  file it has always seen. It runs in `@tool` and again in `mcp.dispatch` - before the schema
  check there, because a content dict is not a publishable parameter type - and is idempotent,
  since what comes out is a plain ref.
* **Out.** `AgentContext(deliver="inline")` fills each `Artifact` with its own content: `text`
  where it is text, `base64` where it is not, plus `bytes` and `sha256` on every one. A cap per
  artifact and a budget per call stop a thirty-slide conversion from handing a model its own
  weight in PNG; what did not fit says `truncated` and is fetched with `workspace.read_bytes`.
  The default is still `"refs"`, so nothing already running starts carrying payloads.
* **Nowhere.** `AgentContext.detached()` is both at once over a `MemoryWorkspace`: a private
  temporary directory, removed on `close()`, whose name nothing outside the context learns.

```python
with AgentContext.detached(google=InjectedToken(creds)) as ctx:
    result = deck_convert(ctx, pdf={"name": "talk.pdf", "base64": encoded})
```

**A plain string is never content.** `"talk.pdf"` is a ref and a `docs.google.com` URL is a deck
the journey resolves itself; if this layer ever fetched one, `deck_sync(deck=<url>)` would start
downloading the deck's own HTML and syncing against it. The one string form is `data:`, which
nothing else begins with. A `{"url": …}` is fetched by the **context's own fetcher** and refused
by name when there is none: a library that grows its own `urlopen` grows an egress path from a
model's argument to an arbitrary host, inside a call the harness thought was local, while a
harness that wants URL inputs already has a client with its own allow-list and passes it in one
line. Tests: `tests/test_agent_content.py`.

### `GoogleAccess` - where the credentials come from

Three implementations, none of them interactive:

* `TokenFile` - this machine's cached token, refreshed when it can be. A missing or dead token
  comes back as `needs_consent` with the one command a human has to run, in milliseconds,
  rather than as a hang.
* `InjectedToken` - the harness holds the credentials and hands them over, for a harness that
  keeps secrets in its own store and never puts them on disk.
* `NoGoogle` - there is no account here; Google journeys refuse with `offline` and the local
  ones still work.

The library asks for credentials by calling `google_auth.credentials()` from a dozen places, so
rather than thread a parameter through all of them, `google_auth.use_provider` puts the
context's source in front of the browser flow for the length of one journey. `describe()`
answers what an agent legitimately needs - is there access, until when - and never what the
token is; a test asserts that the description of a token file contains none of its contents.

`use_services` is the same hook one step later: a caller that already has a Slides, Drive or
Docs client hands it over instead of having one built, which is what a server wants when
`build()` would otherwise fetch a discovery document per call. It takes either a mapping of api
name to a ready client or a callable `(api, version, creds) -> client | None`; anything it does
not answer for is built as before, and `creds` is `None` where nobody passed any, a client that
carries its own credentials never being a reason to go looking for a token. That last sentence
has an edge a caller hit: on the main path *nobody* passes any - `emit()` opens with
`slides_service(), drive_service()` and the built-in fallback resolves them itself - so a builder
that trusted the argument built an unauthenticated client. Either do `creds or
google_auth.credentials()`, or say `use_services(make, needs_credentials=True)` and be handed the
library's (resolved once, however many clients are asked for).

With that seam in place the client library itself is optional: `gapi.py` is the only module that
imports it, so a harness that injects everything - or runs only `deck_prepare`, `deck_inspect`,
`tex_label`, `tex_converge`, which talk to nobody - needs neither `pip install
"beamer2slides[google]"` nor an account. A host that has a token and not the package hears
`offline` with the one-line fix, not a traceback (docs/install.md, "Injecting your own API
clients").

**Both are per context, not per process.** They were module-level globals, which is fine for one
conversion at a time and wrong for a server answering two requests at once: two visitors' tokens
are in the air and neither may reach the other's deck. They are `ContextVar`s now, so each
request answers with its own. A `ContextVar` has one edge - a thread started inside the block
runs in a *fresh* context and inherits nothing - and the library's own worker pools do not care,
because every one of them (`emit.measure_places`, `deck_ir.slide_thumbnails`,
`snapshot.sign_pictures`) resolves `credentials()` on the calling thread and hands the answer
down. A thread that asks anyway must still never fall through to `InstalledAppFlow` and open a
browser on a server, so while exactly one block is open in the process it is given that one, and
where two different ones are open it gets a `RuntimeError` naming
`contextvars.copy_context().run(...)` - guessing there would hand one visitor another's account.

**Downloads have a door of their own.** What the library downloads besides API answers - picture
signatures for the sync base, thumbnails, a read deck's pictures, a picture's original
`source_url`, a Doc's inserted pictures - goes through `net.download`, and `google_auth.use_fetcher`
(a third hook, per context like the other two, resolved on the calling thread and handed to the
pools) lets a caller own it. `AgentContext.fetch_google_content` installs it for a journey. It is
**not** `fetch`: that one answers a model's `{"url": …}` argument, this one the URLs Google hands
back (`contentUrl`, `lh*.googleusercontent.com`) plus whatever host a person inserted a picture
from - two egress policies a harness may well want to differ. A fetcher takes a URL and returns
bytes, or raises: `PermissionError` means "not allowed" and is not retried, anything else is
retried like a network blip. Nothing falls back to urllib behind the caller's back. Tests:
`tests/test_net.py`, `tests/test_base_storage.py`, `tests/test_deck_pictures.py`.

**No download is needed at all.** `fetch_google_content=net.no_downloads` (or `$B2S_NO_DOWNLOADS=1`,
the CLI's `--no-downloads`) refuses every one: a deck's pictures are signed from the files the run
uploaded and otherwise read out of a Drive `.pptx` export (`deck_pictures`), and a sync reads only
the pictures its plan depends on. What it costs: inline formula and overlay pictures keep their
predicted places (the measuring scratch slides are skipped), `fidelity` cannot run, and fonts
come from `font_source` or stand-ins. A picture a reader inserted into a Google Doc comes out of
the document's zip export instead (`doc_sync.exported_pictures`, paired by document order only
when the count and every size agree). Measured live 2026-09-24: convert and sync of two test decks with every
download refused, 0 fetches, every picture of the base signed.

**Where Drive files land.** `AgentContext.drive_folder` (`$B2S_DRIVE_FOLDER`, `--drive-folder`) puts
everything the library creates in Drive - decks, documents, sync bases, `--backup drive` copies,
the temporary staging files - into one folder. The default, `"auto"`, is a "beamer2slides" folder
of the app's own, found by its `b2sHome` appProperty and created once (a Drive that will not list
or make it gets the old places, with a warning). A folder id works if the app can see it (under
`drive.file`, one it created or was opened with; another is refused by name before anything is
created). `"none"` keeps the old places: new decks and documents in My Drive's root, a base or a
backup beside its file (`drive_folder.py`, `tests/test_drive_folder.py`).

**Fonts go through the same door, or come from disk.** `deck_adopt` wants the deck's own fonts,
and `fontfetch` downloads a missing family from google/fonts on GitHub - with `net.download`, so
the caller's fetcher decides; one that refuses GitHub simply means no fetch. A host with no
internet has two ways to still get them. `AgentContext.font_source` (CLI: `$B2S_FONT_SOURCE`)
names a local copy of the google/fonts tree (`ofl/`, `apache/`, `ufl/`), read in place of GitHub
and cut exactly as a fetch would be. And `deck_adopt(fonts=[...])` (CLI: `adopt --fonts`) takes
font files - .ttf, .otf, .ttc, .woff, .woff2, refs or content, a folder standing for every font
under it - named by their name tables, not their file names (`fontfiles.py`): web fonts are
unwrapped, a variable font is cut into the four styles, a static weight between them kept for
runs set in it. A .woff2 needs `brotli` (extra `[woff2]`). What adopt still had to stand in for is
`data["fonts_missing"]`, one entry per font with its letter count and its stand-in, so the caller
knows which files to ask for; what was given and used is `data["fonts_supplied"]`. `deck_pull`
needs none of this: adopt copies the fonts it set into the source tree's `fonts/`. Tests:
`tests/test_fontfiles.py`.

**Pictures can come with the deck too.** A foreign deck's pictures are its `contentUrl`s, and the
Drive export that stands in for them needs a `drive.file` token the deck was opened with, which a
deck adopt is only pointed at rarely has. With downloads refused both fail and the frames go
without (`data["pictures_missing"]`). `deck_adopt(pptx=...)` (CLI: `adopt --pptx`) takes the deck
as the person downloaded it (File > Download > Microsoft PowerPoint), a ref or content: its
pictures are paired with the read deck page by page as an export's are (`deck_pictures`), used
before any download is tried, and no Drive export is made. A `.json` deck pairs them through the
`presentation.json` beside it (`deck_ir.pictures_from_pptx`). `data["pptx_pictures"]` is how many
of the deck's pictures it held: 0 means another deck, or one changed since (a page whose object
count or titles no longer match gives nothing rather than a wrong picture). Tests:
`tests/test_adopt_media.py`.

### `allow` - what the agent may do

Four actions: `reads`, `writes`, `reads_google`, `writes_google`. A context lists what it
permits, and `@tool` checks before the body runs, so a forbidden journey does no work rather
than discovering the policy halfway through a rebuild. `READ_ONLY` and `LOCAL_ONLY` are the
useful presets.

`@tool` declares the **least** a journey does, which is what lets `deck_sync(dry_run=True)` run
in a read-only context; a body about to write for real calls `j.require(WRITES_GOOGLE)` first
and gets the same refusal the gate would have given. That is the seam a harness uses to let an
agent plan freely and gate only the writes behind a human.

`progress` is the field beside the three: a callback that receives, line by line, whatever the
library prints, so a harness can show a thirty-second conversion happening. `deliver`, `fetch`
and the two inline caps belong to the workspace seam and are described under it.

## The result envelope

```json
{"tool": "deck_sync", "ok": true, "summary": "…", "data": {…},
 "artifacts": [{"ref": "out/talk/sync/sync-report.md", "kind": "report"}],
 "diagnostics": [{"level": "conflict", "message": "…", "where": "slide 7"}],
 "next_steps": ["…"], "seconds": 12.4}
```

`summary` is the only field written for a model to read; `data` is written for it to act on. A
tool never raises: `Refused` carries a code, `SystemExit` becomes `refused`,
`guard.RebuildRefused` becomes `deck_edited`, and anything unforeseen becomes `failed` with the
exception in the summary. The codes are in `types.CODES`, and each one has a documented way
forward in INSTRUCTIONS.md - `deck_edited` says run `deck_sync`, `needs_consent` says tell the
person and stop, `base_choice_needed` says ask rather than guess.

Conflicts are diagnostics, not prose. An agent that reports success with open conflicts is
making a mistake the benchmark is built to catch. Each one a person *can* settle names its own id
in the diagnostic, and `deck_sync(take_source=[...])` relays that decision - a decision made in
words by somebody who read the three versions, never one an agent takes on its own. What such a
call writes over is in `data["resolved"]`, verbatim, because nowhere else will hold it afterwards.

An artifact is `{"ref", "kind", "description"}` and, where the context delivers content inline,
`text` or `base64` beside `bytes` and `sha256`. The three original fields are always there, so a
reader written before any of that still works.

## One journey at a time per process

`@tool` holds a process-wide lock. The library underneath is full of state two journeys would
share: `redirect_stdout` is global, `pdf.use_backend` sets a module variable,
`checks.convert_locally` monkey-patches `render.save_png`, and the pure PDF backend carries
PDFium's own process-wide multiple-master font blend, where even the order documents are opened
in can change what is rendered. A harness that wants two journeys at once runs two processes.

That is a statement about the *pipeline*, not about the seams: `use_provider` and `use_services`
are per context precisely so that the rest of a server - the request that is waiting for the
lock, the one that is answering out of a cache, the one that only asked what a folder holds -
never sees another visitor's account while it waits.

## Plugging it in

```python
from beamer2slides.agent import AgentContext
from beamer2slides.agent.tools import TOOLS, INSTRUCTIONS
from beamer2slides.agent.schema import anthropic_tools, openai_tools, validate

ctx = AgentContext.local("C:/talks")
result = TOOLS["deck_inspect"](ctx, pdf="talk.pdf")
```

`schema.py` turns the registry into JSON Schema in either of the two shapes harnesses want;
parameter descriptions come from `typing.Annotated`, so they sit next to the parameter and
cannot drift from it. `mcp.py` serves the same registry over MCP stdio, with `INSTRUCTIONS` as
the server's instructions - a harness that publishes the tools without the rules will sooner or
later force a rebuild over someone's edits.

`TOOLS` iterates in `tools.ORDER`, which is the order of operations above: a model's tool list,
the MCP catalogue and the workbench's dropdown all read top to bottom, and what they read there
is advice. Collecting them from the modules instead put `deck_convert` above `deck_inspect` and
`doc_adopt` above `doc_push` - the two orders this guide spends a section telling people not to
follow. The collection is still what says *which* tools exist, so a journey added to a module
and left out of `ORDER` raises at import rather than quietly sorting itself last.

## What is deliberately not abstracted

* **LaTeX.** `deck_pull`, `tex_converge` and `deck_adopt` shell out to pdflatex/lualatex and
  read SyncTeX. A harness without a TeX installation gets `compile_failed`, which is the truth.
* **The out-folder layout.** `raw.json`, `deck.json`, `emit.json`, `sync/base.json`: `sync`
  identifies a deck *by its folder*, and the crash-safety story is written in those files.
  Making the layout pluggable would make the guarantees pluggable too.
* **The Google APIs themselves.** The tools inject *credentials*, not services. A fake service
  would have to reproduce what Google refuses, and the project already knows how hard that is -
  `tests/slides_sim.py` reproduces exactly three refusals and `devtools/doc_world.py` a great
  many more, and both exist because a fake that is wrong in the wrong place teaches an agent a
  habit that destroys a real deck.

## Measuring it

`devtools/agent_bench.py` and `docs/agent-bench.md`. The question is not whether the library is
correct - the existing suites answer that - but whether an agent given these tools and these
instructions does the journeys *well*: dry-runs before it writes, reads a conflict before it
claims success, refuses to force a rebuild, tells the person when it needs a consent it cannot
give. Most of that is measurable with no Google at all, by replaying canned results and grading
the decision sequence.
