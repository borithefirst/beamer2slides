# Installing and authorising beamer2slides

## Install

```
pip install "beamer2slides[google] @ git+https://github.com/borithefirst/beamer2slides"
beamer2slides convert talk.pdf
```

Nothing is published on PyPI yet, so `pip install beamer2slides` finds no distribution: the wheel
is built from the repository (`pip install build; python -m build` writes an sdist and a wheel into
`dist/`, and both install). `pip install -e .[google]` is the editable install a checkout wants.

**`[google]` is the half that talks to Google** (`google-api-python-client`, `google-auth-oauthlib`),
and since 0.4.0 it is an extra rather than a dependency. Anyone converting a deck from this
machine's own token wants it. Leaving it out is for two kinds of caller: one that has its own
client (below), and one that runs only the local journeys — `beamer2slides classify`,
`deck_prepare`, `deck_inspect`, `tex_label`, `tex_converge` — in a sandbox with no account and
therefore no Google libraries at all. Without it those work as they always did and anything
reaching Google says so in one sentence, naming both ways out.

The wheel is pure Python. Its dependencies (pypdfium2, numpy, pillow, python-pptx, fontTools)
all ship wheels, so no compiler and no TeX distribution are needed:
the input is the compiled PDF. A TeX distribution is only needed for the test decks and for
`pull`/`converge`, which recompile the source.

fontTools was the `[pure]` extra alone until it turned out that `adopt` cannot do without it
either - it reads which faces a machine has and what they cover, how wide a stand-in sets the
deck's words, and the static instances cut out of a variable font fetched from google/fonts. An
install without it read a foreign deck, fetched its fonts and then died on `import fontTools`
with the source tree unwritten. `[pure]` still names it, so an older command line still works.

The measured font substitutes (`calibration/fonts.json`, `fonts_serif.json`) ship inside the
package, so an installed beamer2slides places text exactly like the checkout does. So does
`agent/INSTRUCTIONS.md`, and so does the playground's page; all three are read through
`importlib.resources`, never from a path beside `__file__`, so a zip import finds them too.

Extras: `[google]` is the client library above. `[mcp]` brings the MCP SDK the `beamer2slides-mcp` server speaks through - either
generation of it, 1.x taking its handlers through decorators and 2.x through the constructor
(`agent/mcp.py` wires itself to whichever is installed; the wire protocol is the same, so a client
cannot tell). `[dev]` is what the tests need, and `[pure]` names fontTools, which is a plain
dependency now. Without the SDK, `beamer2slides-mcp` says how to install it and everything else in
`beamer2slides.agent` works as it is.

Not in the wheel yet: the `themes/google` beamer theme (a `.sty` plus its Google Sans Flex
fonts, whose licence has to be checked before redistribution) and the `tools/` probes.

Where output goes when `--out` is not given:
- from a source checkout: `out/<pdf name>/` in the checkout (unchanged);
- from an installed beamer2slides: `out/<pdf name>/` in the current folder.

## Google credentials

Converting needs an OAuth client of type **Desktop app** from a Google Cloud project with the
**Slides** and **Drive** APIs enabled. Download its JSON and save it as `client_secret.json`:

| where it runs | client secret and token |
| --- | --- |
| a source checkout | the checkout root (both git-ignored) |
| an installed beamer2slides | `%APPDATA%\beamer2slides\` (Windows), `~/.config/beamer2slides/` (else) |

`$B2S_CLIENT_SECRET` and `$B2S_TOKEN` override both. The first conversion opens a browser for
consent and caches the token beside the client secret, readable only by its owner
(`icacls` on Windows, mode 600 elsewhere).

Scopes: `presentations` and `drive.file` — the app only ever sees the files it creates or the
user opens with it, never the rest of Drive.

While the consent screen of the client's project is in **Testing** mode, Google expires refresh
tokens after 7 days and the browser consent has to be repeated. Publishing the consent screen
(no verification review is needed for these two scopes as long as the app stays under the
unverified-app cap, which shows a warning screen) removes that.

### Which client the user gets

Two ways to ship this, not yet decided:

- **Bring your own client** (what the code does today): every user makes their own Cloud
  project. No quota shared between users, nothing to verify, no secret in the package — but
  ten minutes of console clicking before the first deck.
- **A published beamer2slides client**: the desktop-app client id and secret ship in the
  package (for an installed app these are not a secret in the OAuth sense — the flow is
  protected by the redirect to localhost, and Google's own quickstarts do this). The user only
  clicks consent. This needs the consent screen published and, for `presentations`, Google's
  verification if the app passes the unverified-app cap; the project's API quota is then shared
  by all users.

## Injecting your own API clients

A caller that already holds Slides/Drive/Docs clients — with a bundled discovery document, its
own retries, its own transport — hands them over for the length of a block, and the library never
builds one (nor needs `[google]` installed at all):

```python
from beamer2slides import google_auth

with google_auth.use_services({"slides": my_slides, "drive": my_drive, "docs": my_docs}):
    ...                                   # convert, sync, pull, adopt: the same journeys
```

A mapping is by API name; a callable is asked per client and may answer `None` to let the library
build that one itself:

```python
def make(api, version, creds):            # creds is None wherever nobody passed any
    return my_pool.client(api, version, creds or google_auth.credentials())

with google_auth.use_services(make):
    ...
```

`creds` is the credentials the *call site* passed, which on the main path is nothing: `emit()`
opens with `slides_service(), drive_service()` and passes none, because the built-in fallback
resolves them itself. A builder that trusts the argument therefore builds an unauthenticated
client. Either do `creds or google_auth.credentials()` as above, or say so once:

```python
with google_auth.use_services(make, needs_credentials=True):   # creds is never None
    ...
```

`needs_credentials=True` is also what makes `credentials_for_threads()` answer, which is how the
library's own pools (`emit.measure_places`, `deck_ir.slide_thumbnails`, `snapshot.sign_pictures`)
hand credentials to a worker thread — a `ContextVar` is not inherited by a thread started inside
the block. Left out, an injected client is never a reason for the library to go looking for a
token.

Credentials alone, with the library building the clients, are `use_provider`:

```python
with google_auth.use_provider(lambda: my_credentials):
    ...
```

Both hooks are per context, not per process: a server answering two requests at once has two
visitors' tokens in the air and neither may reach the other's deck.

`gapi.py` is the only module that imports the client library, so what a journey asks of it is one
file: `build`, `media_upload`, and `status_of` / `message_of` / `is_transient` over an error. The
error class is bound there once and nowhere else — two independent `try/except ImportError`
fallbacks would bind two different classes, and an `except` clause naming the other one would
quietly stop matching.

## Checking an install

```
python -c "from beamer2slides import emit; print(emit.CALIBRATION.is_file())"
beamer2slides classify talk.pdf     # local only, no Google calls
```
