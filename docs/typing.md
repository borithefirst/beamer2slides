# Types first

The type checker is the tool that makes this code reliable, not a lint pass run afterwards. This
page says why, which patterns follow from it, and how the build holds us to it. Every module is
written to it; the older code is being brought to it (the end of this page).

## Why

Commit 688ebf4 fixed a person's upload dying on `KeyError: 'custom'`. `marked.py` (adopt's reader
of a marked source) wrote shape elements that emit could not take: a shape kind with no Slides
preset, no `flip`, an outline as a bare colour where emit reads `{color, width}`, a table without
its column layout. Every one of those facts was known - to the producer's author, to the
consumer's - but only by convention: a deck.json element was a `dict`, and a dict says nothing
about which keys it holds. The tests ran the producer and the consumer on the decks they had; none
had that combination. The person's deck did.

That is the class of bug to end: *a value whose shape is agreed only by convention between code in
different places*. Tests find the combinations they run. A type checker finds every combination,
at every call site, before anything runs - but only for what the code tells it. So the work is to
move knowledge out of convention and into types, and to write code in the forms the checker can
reason about best.

## The three rules

1. **The code type-checks, with no errors and no warnings, and nothing is suppressed.** When the
   checker disagrees, either the code is wrong or it says less than the author knows. Either way the
   fix is to the code: say what you know (a precise type, a narrowing, a record instead of a dict),
   or restructure until it is true. A suppression - `# type: ignore`, `cast`, `Any` - makes the
   checker agree without making the code right, and it stays forever, because nobody revisits a
   silenced line. The config honours no suppression comment at all.

2. **Nothing is built without the check.** `pip install .`, `pip install -e .`, a wheel, an sdist,
   `pip install git+...`: every build goes through `build_backend/beamer2slides_build.py`, which runs
   pyrefly first and refuses to build what fails. There is no switch to skip it. An optional check
   is skipped exactly when someone is in a hurry, which is when bugs are written.

3. **Code is written so the checker does the most work.** A checker proves what the types let it
   see. The patterns below are the ones that let it see the most. The simplest statement: prefer the
   form in which a mistake is a type error at the place it is made.

## Patterns

### A record is a dataclass

When the keys of a dict are known when the code is written, it is a record, and a record is a
dataclass:

```python
@dataclass(frozen=True, kw_only=True)
class ShapeElement:
    id: str
    box: Box
    shape: TemplateKind
    flip: bool
    outline: Outline | None
```

- **No field defaults.** `ShapeElement(id=..., box=..., shape=...)` without `flip` is a
  `missing-argument` error where it is written. That one line is 688ebf4, caught before it ran
  (`tests/test_typecheck.py` keeps a probe of exactly this).
- **`kw_only=True`**: every construction names every field, so reading a call tells you what it
  sets, and reordering fields breaks nothing.
- **`frozen=True`**: a stage cannot change the value it was given behind its caller's back. A new
  value is `dataclasses.replace(el, box=grown)`, visible where it happens. (The classifier's habit of
  adding keys to an element in place is exactly what made its fields unknowable.)

A dict stays right where its keys are data: a map from ids to things, `dict[ObjectId, Element]`;
and JSON before it is parsed (below).

### No default arguments

A default is a decision made once, for every caller, and invisible at each of them. Worse, it hides
the moment the checker is most useful: add a parameter with a default and every existing call site
still compiles, unexamined. Add it without one and the checker lists every caller, and each gets a
decision. So a function takes no default values. Where there is a usual value, give it a name and
pass it: `render(page, dpi=SCREEN_DPI)`. Where a caller truly has two ways of calling, that is two
functions.

The same holds for a dataclass field: a defaulted field is a default argument of the constructor.

Optional input from a person (a CLI flag, an agent tool parameter) is decided at that boundary -
argparse's default, the tool schema's default - and passed on explicitly from there. Signatures an
outside protocol dictates (PEP 517's build hooks) live outside the package, in `build_backend/`.
So do the calling forms the docs publish to callers outside this repo (the harness at Google): an
agent tool body's optional parameters are the schema's defaults, and `AgentContext(...)`,
`AgentContext.detached()`, `google_auth.use_services(make)`, `schema.all_schemas()` and
`LocalWorkspace(root)` keep theirs. That is what the ledger's remaining 70 defaults and 11 fields
are; nothing internal has one.

How a default leaves (wave 2, 2026-09-30): each caller passes the old value, so behaviour does
not move; a value most callers pass becomes a name (`net.TRIES`, `ink.BAND_GAP_PX`,
`inverse.BEAMER_PT`); two ways of calling become two functions (`align_slides` /
`align_slides_with`, `merge.plan_merge` / `plan_merge_with`, `texmap.locate_words` /
`locate_words_in`); a record built empty and filled later gets a constructor that says so
(`classify_model.new_span`/`new_line`/`new_paragraph`, `inverse.fresh_context`). `None` passed on
purpose reads as what it means: `slides_service(None)` is "the context's credentials",
`save_thumbnail(..., fetch=None)` "the context's fetcher".

### A closed set is a Literal, and every match over it is exhaustive

Element kinds, shape presets, alignments, conflict fields: each is a `Literal[...]` (or an
`Enum`), never a bare `str`. A branch over one ends in `assert_never` (from
`beamer2slides.typing_compat`, since `typing.assert_never` is 3.11):

```python
match el:
    case TextElement(): ...
    case ShapeElement(): ...
    case _:
        assert_never(el)
```

Add a kind and every match that forgot it is an error. A lookup table keyed by a Literal cannot be
proved total by the checker; write the lookup as a match, or keep a test that it is total over the
Literal's values (`tests/test_ir.py` does for emit's tables).

### Variants are a union of dataclasses

`Element = TextElement | ImageElement | ShapeElement | TableElement | DiagramElement`. Narrowing by
`match`/`isinstance` gives each branch that kind's fields, typed. No `el.get("x")` guessing, no
`el["kind"] == "shape"` followed by reads the checker cannot connect to the test.

### Parse at the boundary; inside, only typed values

JSON comes in at a few places: deck.json and raw.json on disk, sync bases (disk and Drive), Slides,
Drive and Docs API responses, agent tool arguments. Each is parsed once, where it enters, into
typed values - a function that returns the dataclass or raises naming the path that was wrong - and
written back once where it leaves. Inside, nothing is `dict[str, Any]`. Reading an old file is the
parser's job too: upgrading an adopt base's shapes (`adopt_sync.upgrade_shapes`) is parsing, and
belongs where the base is read.

### Moving a module that others call with dicts: an entry and a typed twin

Keep `f(d: JsonMap, ...)` with its old signature as one line, `return f_of(parse(d), ...)`, and
write the body in `f_of`, which takes records and has no defaults; callers move to `f_of` one by
one, and the entry goes when the last has. `JsonMap = Mapping[str, Json]` (emit_model) is
read-only, so covariant: a caller's `dict[str, str | float]` is accepted where `JsonObject` is
refused. A value a stage derives (a run marked in-sentence, a hole's spaces) is
`dataclasses.replace` on its record, never a key added to the IR dict another stage diffs. Prove
the move with the module's output recorded before and after over every deck: 0 may differ.

### Different meanings are different types

A Slides objectId, an element key and a slide key are all strings; points, pixels, EMU and ems are
all floats. Mixing them is a real bug family here (every "off by 72/96" and every id looked up in
the wrong table). `NewType("Pt", float)` and `NewType("ObjectId", str)` cost nothing at runtime and
make each mix-up a type error.

### A stage is a type

A classified deck and a rendered deck are different things: render adds picture files, pixel
sizes, bullet ink. When they share one type, "emit was given a deck render never saw" is a KeyError
at runtime. When render returns a `RenderedDeck` and emit takes one, it cannot be written. Fields
added by a later stage belong to the later stage's type, not an optional field of the earlier one.

### `None` means absent, and is narrowed once

`X | None` is for a value that can really be missing, and the code says what to do then, once, at
the top. `None` as "not computed yet" is a stage (above). A function that "can't" return None for
this caller is two functions, or raises.

### Precise containers, read-only where read-only

`list[Element]`, not `list`. Arrays name their dtype (`beamer2slides.arrays`): a bare `np.ndarray`
is `Any` inside. A parameter a function only reads is `Sequence[...]` / `Mapping[...]`: it says the
function will not change it, and it accepts a tuple.

### A class split into mixins has one declared state

The attributes its mixins share are declared, with their types, in one base class
(`classify_state.PageState`), and the mixins inherit from it in a line (`Tables <- Graphics <-
Lines <- Paragraphs <- Figures <- Reasons <- PageClassifier`). Every `self.x` is checked against
one declaration, a mixin's calls into the ones before it are checked with their signatures, and an
attribute set in one mixin and read in another cannot drift. A Protocol for `self` is not checked
against the class that uses it; declarations repeated per mixin drift apart. A value passed as
something it only resembles gets a Protocol of what is read (`classify_text.Row`), rather than
either type widened to fit the other.

### Make illegal states unrepresentable

An outline that is "a colour string or a `{color, width}` dict" is two types for one thing, and
every reader must handle both. One type, one shape. A "table with no columns yet" is a stage, not a
table. When a combination of fields must not happen, choose types in which it cannot be written.

## How the build holds us to it

- **`[tool.pyrefly]` in pyproject.toml**: the strict preset, every warning raised to an error,
  `enabled-ignores = []` (no suppression comment is honoured), Python 3.10 (the oldest we run on),
  `search-path = ["src"]` (a worktree checks its own files).
- **The build**: `build_backend/beamer2slides_build.py` wraps setuptools and runs the check before
  every wheel, editable install and sdist. `[build-system] requires` pins the checker and every
  package whose types it reads, so every machine gets the same answer. It checks on the Python
  building it: the code must type-check against numpy 2.2 on 3.10 as well as numpy 2.5 on 3.11+.
- **`typecheck/baseline.json`**: the errors older than these rules - and nothing else. It only
  shrinks. It is compared as a count - in each file, each kind of error (its message) at most as
  often as the baseline lists it - not by pyrefly's own `--baseline`, which lets one entry excuse
  every error of its column and message (a new bare `dict` at an old one's column went through) and
  loses an old error a retyped line moved. After fixing errors, `python
  build_backend/beamer2slides_build.py prune` drops them, and `tests/test_typecheck.py`'s `CEILING`
  goes down to match. Never `pyrefly --update-baseline`: new code type-checks; it is never added to
  the baseline.
- **`typecheck/tests_baseline.json`**: tests/ under the same checker and config, as the build
  backend's `tests` target (the files handed to pyrefly by name, `.` on the search path so
  `tests` is a package). Admitted whole on 2026-09-29 (12,567 errors, the only time a baseline
  grew), then the same ratchet: `prune tests`, `TESTS_CEILING`. The build cannot run it (its
  environment has no pytest), so `tests/test_typecheck.py` is its gate, with pytest pinned in
  `[tool.beamer2slides.typecheck] tests-requires`. What it buys: a test left calling a function
  the old way after its signature changed is an error at the call, not a failure found when the
  suite runs - and when retyping the package makes a test's error disappear, prune it. Empty since
  2026-09-30 and `TESTS_CEILING` 0: a test that does not type-check fails the gate like src/.
- **`typecheck/tools_baseline.json`**: tools/ (the probes, calibration and proofs run by hand), the
  build backend's `tools` target, gated by `tests/test_typecheck.py::test_the_tools_type_check`.
  Admitted at zero on 2026-09-30 and never anything else: a probe still calling a package function
  the old way is named when the suite runs, not months later when someone runs it against Google.
- **Agent tools** (`agent/context.py`): a tool is `Tool[P]` over its body's `Concatenate[Job, P]`,
  so a Python call is checked against the body; a JSON call (MCP, a replayed transcript) is
  `Tool.dispatch(ctx, arguments)`, the one untyped-by-nature entry, which turns a wrong name or
  type into `bad_request`. A file parameter is `content.File` (a ref or inline content), narrowed
  by `j.ref`: the body's type was `str` while callers passed content dicts, a lie the checker
  could not see through the old `*args: object` call.
- **JSON not yet parsed** is a `json_types.JsonObject`, read through its narrowings (`as_object`,
  `as_str`, ...), which name where a value of the wrong shape was. That is the interim form of
  "parse at the boundary" until a record's parser exists; a TypedDict view of a dict cannot be passed
  to the legacy `dict` parameters, a `JsonObject` can.
- **Google's APIs** (`google_types.py`): the part of Slides, Drive and Docs we call, as Protocols.
  A method's keywords are a TypedDict taken as `**kw: Unpack[...]` (`Required` where Google
  requires one) and it returns `Request[Answer]`, so `gslides.execute(request) -> Answer`; a call
  not listed there is added there first. `gapi.build` checks the library's client against the
  Protocol; an injected client is taken at its word, and a fake takes `**kw` too (pyrefly accepts
  no named keyword parameters against an Unpack). Answers are `total=False` TypedDicts, not
  dataclasses: Google's open schema, where a field mask drops any key, kept and written back as
  they came; read a key with `.get` and say what its absence means (`file_id` reads a new file's).
  An answer still handed to a parameter annotated `dict` stays `JsonObject` until that parameter
  says its TypedDict (today `presentations.get` and `files.get`). `execute_with(request, retries=,
  timeout=)` is the explicit form; it always tries once and ends in a return or a raise.
- **Google's requests** (`google_types.py`): every `batchUpdate` request is a `SlidesRequest` /
  `DocsRequest`, and the bodies take nothing else (`BatchUpdateBody.requests:
  Sequence[SlidesRequest]`), so a misspelt kind or field, or a value of the wrong type, fails the
  build instead of coming back as a 400 that throws out the whole batch. A request is a
  `total=False` TypedDict with one key per kind (34 Slides, 26 Docs, the ones we send); "exactly
  one" is checked at run time by `slides_request_kind` / `docs_request_kind`, because PEP 728's
  `closed=True` needs typing_extensions at run time before 3.15. The payloads (text and paragraph
  styles, shape, page, line, image and table properties, fills, colours, dimensions, bullets) are
  TypedDicts with the discovery document's names, number types and enums as Literals
  (`ShapeType`, `BulletPreset`, `PredefinedLayout`, Docs' `DocsNamedStyleType`...); `fields` masks
  stay `str`. A part read back and copied into a write is parsed where it is read
  (`typed_part(o, Shape, where)` and its named parsers `slides_text_style`, `shape_properties`,
  `page_properties`, `layout_placeholder_id_mapping`, `shape_type`, `bullet_preset`...), never
  copied blind; a typed request or part becomes JSON again only where a file keeps it or a test
  compares it (`slides_json`, `part_json`). Readers narrow by kind (`if "createShape" in r`;
  `tests/slides_sim.py` dispatches on `slides_request_kind`). `tests/test_google_schema.py` checks
  every TypedDict there against the discovery documents the client library ships (names, types,
  enums, `Required`); where we write differently on purpose, its `EXEMPT` says why, and an
  exemption no longer needed fails. Typing them found requests Google refuses with their batch: a
  weight change alone written without its family, `custom`/`line` shape types from old adopt
  marks, a notes edit to a `null` object id, a fill of no colour (all now refused before sending).
- **`typecheck/rules.json`**: per module, how many default arguments, defaulted dataclass fields
  and uses of `Any` remain. Only goes down (`tests/test_typing_rules.py`; `python
  tests/test_typing_rules.py` rewrites it, and refuses to raise a count). A new module has none.
- **Tests**: `tests/test_typecheck.py` runs the build's check, the baseline ratchet, and probes -
  one per bug class the config must see (688ebf4's optional key read by subscript, a record built
  without a field, a case missing from an exhaustive match). `tests/test_typing_rules.py` forbids
  suppression comments and `typing.cast` and holds the ledger.
- **CI**: `.github/workflows/typecheck.yml` on the pinned environment; every workflow that installs
  the package builds it, so builds on 3.10 and 3.12 both type-check.

## Where we are, and the order of work

On 2026-09-29, under strict: 13,714 baseline errors (13,713 once the one real bug was fixed) in 103k lines (7,526 bare generics such as
`dict`, 3,625 unannotated parameters, then attribute and Optional errors); 1,422 default arguments,
277 defaulted dataclass fields, 133 `Any`. A triage of the errors found one real bug by itself
(pure/fonts.py's `_PDFDOC`); the rest point at a few untyped hubs that make everything around them
unknowable. The order:

1. **The IR.** deck.json's elements as dataclasses (one per kind and stage), parsed from and
   written to exactly today's JSON: `identity.ir_fields` hashes the key set, so `to_json` leaves out
   what was never set, fixed-length arrays stay lists, and old bases parse (with their upgrades)
   and compare equal. The classifier's in-place additions become constructions.
   *Types done* (`ir_types.py`, 2026-09-29): every deck.json and every base `ir` under out/
   (2,046 decks, 57,069 base elements) round-trips key for key; the one canonicalisation is a
   `false` flag written as absent (none exists). *Bases read through it* (`sync.base_today`: an
   adopt base's old marked shapes and tables, then `snapshot.rehash_base`): an element whose JSON
   form differs from what `ir_types` writes takes today's form and a new hash, only when the
   recorded hash can be recomputed from the recorded IR; otherwise, and when the parser refuses
   it, it is kept and reported (`BaseForm`, report `base_forms`). A change of JSON form is a
   rewrite of the base, never a change of the source. Over the 1,164 bases under out/: nothing to
   rewrite. *First consumers*: checks.py parses deck.json at the rendered stage; compare.py reads
   both decks into its own view records at entry, because its target is deck_ir's reading of a
   live deck, which ir_types does not model, and its current side carries the pull loop's
   `key`/`frame_index`. Next: consumers parse at their entry (emit's `DeckPlan`, identity,
   merge/sync); deck_ir's IR as its own type; inverse keeping the frame keys beside the deck, and
   residuals as a record per kind; producers construct typed values last.
2. **The hubs**: `gslides.execute`'s result; the Slides/Drive/Docs service objects (`gapi.py`);
   PageClassifier's mixins sharing undeclared attributes (one declared base); `inverse.Planner`'s
   `element()` that callers know is present; adopt's `Context` attributes set dynamically.
3. **Units and ids** as NewTypes, from the IR outward.
4. **Module by module to zero**: annotations, defaults removed, `Any` replaced, the baseline pruned.
   *The package at zero* (2026-09-29): `typecheck/baseline.json` is empty and `CEILING` is 0, so
   every error in src/ fails the build; no `Any` is left, 172 default arguments and 79 defaulted
   fields are. Libraries without types are reached through Protocols after a runtime check
   (`gapi.build`, `gapi.Httplib2`, `devtools/deep_stack.py` for torch, lpips and transformers),
   never imported by a statement the checker would have to follow. *Defaults out* (2026-09-30):
   172 default arguments and 79 defaulted fields down to 70 and 11, all of them published calling
   forms (above).
5. **tests/ and tools/** under the same checker. *tests/ admitted* (2026-09-29, its own baseline
   above). *tests/ at zero* (2026-09-30, from 3,702): helpers return the package's records
   (`ir.Deck`, `raw_types.RawPage`, `TableElement`...) instead of `dict`, JSON is read through
   `tests/json_reads.py`, fakes subclass `tests/fake_google.py`'s Protocol-complete classes, and a
   TOML reader is imported by name behind a Protocol (`test_typecheck.Toml`). Typing them found
   three tests that checked less than they said: a font lookup faked with a dict made
   `deck_thumbs.face_glyphs`' broad `except` swallow the AttributeError, so the test never reached
   its branch (the `except` now covers only reading the font file); `test_request_budget` counted
   the tuples of a plan, not its requests; `tests/decks/sync/build.py`'s drawings hash says
   position-free and is not (paths are tuples after `parse_raw`, so its relative step never ran).
   *tools/ at zero* (2026-09-30, from 958 in the 49 files that are not runpy shims over devtools):
   Google answers read through `google_types`, records frozen dataclasses, no defaults. It found
   a probe's return type that lied (`probe_images.url_variants`, pairs typed as strings) and two
   crashes on malformed answers now said as errors. What the probes call that `google_types` does
   not list yet they read through `json_object` or a local Protocol after a runtime check.
6. **Google's requests typed** (2026-10-01): google_types from the discovery documents (every
   request kind we send and its payloads, Drive `revisions()`/`about()`, `pageSize` on create, a
   file's `size`, `gapi.resumable_media_upload`), checked by `tests/test_google_schema.py`; then
   every producer and reader (emit, snapshot, theme_sync, the devtools, tools/, Docs, sync, merge,
   refit, the fuzzers), and last the bodies: `BatchUpdateBody.requests` is
   `Sequence[SlidesRequest]`, `DocsBatchUpdateBody.requests` `Sequence[DocsRequest]`. sync's
   `{"__b2s_break__": True}` marks between slides became structure (`sync.Blocks`, a list of
   requests per slide). Every request sent is byte for byte what was sent before, except the
   four above that Google would have refused. tools/ lost its local copies of Drive's revisions,
   a sized presentation and gslides' `pt`/`emu`/`text_box`.

When you touch a function for any reason, leave it to these rules: fully annotated, no defaults,
records as dataclasses. Then prune the baseline and lower the ledger.
