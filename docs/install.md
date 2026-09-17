# Installing and authorising beamer2slides

## Install

```
pip install beamer2slides          # from a checkout: pip install -e .
beamer2slides convert talk.pdf
```

The wheel is pure Python. Its dependencies (pypdfium2, numpy, pillow, python-pptx and the
Google API client) all ship wheels, so no compiler and no TeX distribution are needed: the
input is the compiled PDF. A TeX distribution is only needed for the test decks and for
`pull`/`converge`, which recompile the source.

The measured font substitutes (`calibration/fonts.json`, `fonts_serif.json`) ship inside the
package, so an installed beamer2slides places text exactly like the checkout does.

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

## Checking an install

```
python -c "from beamer2slides import emit; print(emit.CALIBRATION.exists())"
beamer2slides classify talk.pdf     # local only, no Google calls
```
