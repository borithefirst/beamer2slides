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
   rewrite. Next: consumers parse at their entry, leaves first (emit's `DeckPlan`, identity,
   checks, compare, merge/sync), and producers construct typed values last.
2. **The hubs**: `gslides.execute`'s result; the Slides/Drive/Docs service objects (`gapi.py`);
   PageClassifier's mixins sharing undeclared attributes (one declared base); `inverse.Planner`'s
   `element()` that callers know is present; adopt's `Context` attributes set dynamically.
3. **Units and ids** as NewTypes, from the IR outward.
4. **Module by module to zero**: annotations, defaults removed, `Any` replaced, the baseline pruned.
5. **tests/ and tools/** under the same checker.

When you touch a function for any reason, leave it to these rules: fully annotated, no defaults,
records as dataclasses. Then prune the baseline and lower the ledger.
