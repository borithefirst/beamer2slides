"""What survives HTML -> Google Doc -> HTML?

Uploads a stress HTML file with Drive conversion, exports it back in every format
Drive offers, and does it a second time on the exported HTML to see whether the
round trip settles or keeps drifting. Everything lands in `out/docs-probe/`.

    .venv\\Scripts\\python.exe tools\\probe_docs_html.py [--keep]

Needs only the `drive.file` scope (the files are ours); the Docs API is tried with
the same credentials to find out whether `drive.file` reaches `documents.get`.

`--keep` leaves the created files in Drive (their ids are printed); by default they
are deleted again.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides.google_auth import credentials, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-probe"
DOC_MIME = "application/vnd.google-apps.document"


def stress_png() -> bytes:
    """A small, obviously recognisable PNG (red square, blue diagonal)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (120, 80), (220, 60, 60))
    draw = ImageDraw.Draw(img)
    draw.line((0, 0, 119, 79), fill=(40, 60, 200), width=6)
    draw.rectangle((10, 10, 40, 30), fill=(250, 230, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def stress_html(png: bytes) -> str:
    """One numbered case per feature, so the export can be graded case by case."""
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>b2s docs round-trip stress</title>
<style>
  .sheetstyle {{ color: #0b6623; font-style: italic; }}
  h2.tagged {{ color: #8b0000; }}
  p.spaced {{ margin-top: 24pt; margin-bottom: 4pt; line-height: 2.0; }}
  .boxed {{ border: 2px solid #3366cc; padding: 6pt; background-color: #eef4ff; }}
</style>
</head>
<body>

<h1>01 Heading one</h1>
<p>02 A plain paragraph of prose, long enough to wrap at a normal page width so we
can see whether the paragraph itself survives as one paragraph rather than being
split at the line breaks of the source file.</p>

<h2>03 Heading two</h2>
<h3>04 Heading three</h3>
<h4>05 Heading four</h4>
<h5>06 Heading five</h5>
<h6>07 Heading six</h6>

<p>08 inline styles:
<span style="color:#cc0000">red</span>,
<span style="background-color:#ffff00">yellow ground</span>,
<span style="font-family:'Courier New', monospace">Courier New</span>,
<span style="font-family:Georgia, serif">Georgia</span>,
<span style="font-size:18pt">18pt</span>,
<span style="font-size:8pt">8pt</span>,
<span style="font-weight:700">bold700</span>,
<span style="font-weight:bold">boldkw</span>,
<span style="font-style:italic">italic</span>,
<span style="text-decoration:underline">underline</span>,
<span style="text-decoration:line-through">strike</span>,
<span style="letter-spacing:2px">tracking</span>,
<span style="text-transform:uppercase">uppercase</span>,
<span style="vertical-align:super;font-size:8pt">superspan</span>.
</p>

<p>09 semantic inline: <b>b</b> <strong>strong</strong> <i>i</i> <em>em</em>
<u>u</u> <s>s</s> <del>del</del> <ins>ins</ins> <code>code</code> <kbd>kbd</kbd>
<mark>mark</mark> <small>small</small> x<sup>sup</sup> x<sub>sub</sub>
<abbr title="hover">abbr</abbr> <q>quoted</q>.</p>

<p class="sheetstyle">10 A paragraph styled from a &lt;style&gt; block by class.</p>
<h2 class="tagged">11 A heading styled from a &lt;style&gt; block</h2>
<p class="spaced">12 A paragraph with margin-top 24pt, margin-bottom 4pt and
line-height 2.0 from the style block.</p>
<p class="boxed">13 A paragraph with a border, padding and a background colour.</p>

<p style="text-align:center">14 centred</p>
<p style="text-align:right">15 right</p>
<p style="text-align:justify">16 justified text that is long enough to actually need
justification across at least two full lines of the rendered page, otherwise the
setting has nothing to act on and cannot be observed in the export at all.</p>
<p style="margin-left:36pt">17 indented 36pt</p>
<p style="text-indent:24pt">18 first line indented 24pt, with enough words to wrap
onto a second line so the first-line indent is distinguishable from a whole-block one.</p>

<h2>19 Lists</h2>
<ul>
  <li>20 bullet one</li>
  <li>21 bullet two
    <ul>
      <li>22 nested bullet</li>
      <li>23 nested bullet with <b>bold</b>
        <ul><li>24 third level</li></ul>
      </li>
    </ul>
  </li>
  <li>25 bullet three</li>
</ul>
<ol>
  <li>26 number one</li>
  <li>27 number two
    <ol type="a"><li>28 letter a</li><li>29 letter b</li></ol>
  </li>
  <li>30 number three</li>
</ol>
<ol start="7"><li>31 starts at seven</li><li>32 eight</li></ol>
<ul style="list-style-type:square"><li>33 square bullet</li></ul>
<dl><dt>34 term</dt><dd>35 definition</dd></dl>

<h2>36 Table</h2>
<table border="1" style="border-collapse:collapse; width:100%">
  <thead>
    <tr style="background-color:#dddddd">
      <th>37 head A</th><th>38 head B</th><th>39 head C</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>40 a1</td><td>41 b1</td><td style="background-color:#ffe0e0">42 c1 pink</td></tr>
    <tr><td colspan="2">43 spans two columns</td><td>44 c2</td></tr>
    <tr><td rowspan="2">45 spans two rows</td><td>46 b3</td><td>47 c3</td></tr>
    <tr><td style="text-align:right">48 right</td><td>49 c4</td></tr>
  </tbody>
</table>

<table style="border-collapse:collapse">
  <tr><td style="border:none">50 borderless</td><td style="border:none">51 cell</td></tr>
</table>

<h2>52 Links and anchors</h2>
<p>53 An <a href="https://example.com/target">external link</a>,
an <a href="#bookmark">internal link</a> and
<a href="mailto:someone@example.com">a mail link</a>.</p>
<h3 id="bookmark">54 The heading the internal link points at</h3>

<h2>55 Images</h2>
<p>56 data URI, native size: <img src="{data_uri}" alt="alt text data uri"></p>
<p>57 data URI, resized to 60x40:
<img src="{data_uri}" width="60" height="40" alt="resized"></p>
<p>58 remote https:
<img src="https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png"
     alt="remote logo"></p>
<p>59 relative path: <img src="figure.png" alt="relative"></p>

<h2>60 Blocks</h2>
<blockquote>61 A block quote.</blockquote>
<pre><code>62 preformatted
    indented   spaced
def f(x):
    return x * 2
</code></pre>
<hr>
<p>63 after a horizontal rule</p>
<div style="page-break-before:always"></div>
<p>64 after a page break request</p>

<h2>65 Whitespace and characters</h2>
<p>66 non&nbsp;breaking&nbsp;spaces, an em&mdash;dash, an en&ndash;dash,
&laquo;guillemets&raquo;, &amp;, &lt;, &gt;, an ellipsis&hellip;,
a soft&shy;hyphen, unicode: &#x2713; &#x03b1;&#x03b2;&#x03b3; &#x211d; &#x2192;.</p>
<p>67 three   consecutive   spaces and a<br>line break.</p>

<h2>68 Things we expect to lose</h2>
<div style="display:flex; gap:10pt">
  <div style="width:50%; background:#f0f0f0">69 flex column one</div>
  <div style="width:50%; background:#e0e0e0">70 flex column two</div>
</div>
<p style="position:absolute; top:10pt; left:10pt">71 absolutely positioned</p>
<p style="float:right; width:30%">72 floated right</p>
<p><span style="border-bottom:1px dotted #999">73 dotted underline via border</span></p>
<p style="columns:2">74 a two-column paragraph</p>
<footer>75 a footer element</footer>
<p>76 last paragraph, so a trailing loss is visible.</p>

</body>
</html>
"""


FEATURE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  div.wrap p { color: #006699; }
  .outer .inner { font-weight: 700; }
  p#byid { color: #cc00cc; }
  p[data-k="v"] { font-style: italic; }
  @media print { .printonly { color: #ff0000; } }
</style></head><body>
<p>A1 before</p>
<hr style="page-break-before:always">
<p>A2 after hr page-break-before</p>
<br style="page-break-before:always">
<p>A3 after br page-break-before</p>
<p style="page-break-before:always">A4 paragraph page-break-before</p>
<div style="page-break-after:always">A5 div page-break-after</div>
<p>A6 after div page-break-after</p>

<h1 style="font-size:30pt;font-family:Georgia;color:#227722">B1 styled h1</h1>
<h2 style="font-size:10pt;color:#772222">B2 small h2</h2>

<p><a name="oldanchor"></a>C1 after an a-name anchor</p>
<p id="byid">C2 paragraph with an id (and #byid CSS)</p>
<p data-k="v">C3 attribute-selector CSS</p>
<div class="wrap"><p>C4 descendant selector (div.wrap p)</p></div>
<div class="outer"><p><span class="inner">C5 two-level descendant</span></p></div>
<p class="printonly">C6 inside a media query</p>
<p><a href="#byid">C7 link to the id above</a></p>

<table style="border-collapse:collapse;width:480pt">
  <colgroup><col style="width:300pt"><col style="width:180pt"></colgroup>
  <tr><td style="border:1px solid #000">D1 wide 300pt</td>
      <td style="border:1px solid #000">D2 narrow 180pt</td></tr>
  <tr><td style="border:2px dashed #cc0000">D3 dashed red border</td>
      <td style="border-top:3px solid #0000cc;border-bottom:none">D4 partial borders</td></tr>
</table>
<table><tr><td>E1 outer<table><tr><td>E2 nested table</td></tr></table></td></tr></table>

<ul style="list-style-type:disc"><li>F1 disc</li></ul>
<ul style="list-style-type:circle"><li>F2 circle</li></ul>
<ul style="list-style-type:square"><li>F3 square</li></ul>
<ol style="list-style-type:lower-roman"><li>F4 lower roman</li><li>F5 two</li></ol>
<ol style="list-style-type:upper-alpha"><li>F6 upper alpha</li></ol>
<ul><li>F7 plain</li><li style="list-style-type:none">F8 marker none</li></ul>
<ul><li><input type="checkbox">F9 checkbox item</li></ul>

<div style="white-space:pre">G1 white-space pre
   indented    wide
</div>
<p style="white-space:pre-wrap">G2 pre-wrap   three spaces</p>
<p>G3 tab&#9;between&#9;words</p>

<p style="font-family:'Comic Sans MS'">H1 a font Docs may not have</p>
<p style="font-family:Roboto Mono">H2 Roboto Mono</p>
<p style="font-size:7.5pt">H3 fractional 7.5pt</p>
<p style="color:rgb(17,85,170)">H4 rgb() colour</p>
<p style="color:blue">H5 named colour</p>
<p style="background-color:rgba(255,0,0,0.3)">H6 rgba background</p>
</body></html>
"""

# What the run carrying each case should look like, so a regression is visible.
FEATURE_EXPECTED = {
    "B1": 'font-family:"Georgia"', "B2": "color:#772222", "C2": "color:#cc00cc",
    "C4": "color:#006699", "C5": "font-weight:400 (the .outer .inner rule is ignored)",
    "C3": "unstyled (attribute selectors ignored)", "C6": "unstyled (@media ignored)",
    "H1": '"Comic Sans MS"', "H2": '"Roboto Mono"', "H3": "7pt (rounded)",
    "H4": "#1155aa", "H5": "#0000ff", "H6": "no background (rgba dropped)",
}


def feature_report(html: str) -> None:
    """Print the style the export put on each lettered case."""
    # Docs exports a real page break as <hr style="page-break-before:always">.
    # The source asks for five, in four spellings; measured, none survive.
    print("  page breaks surviving:", len(re.findall(r"<hr[^>]*page-break", html)),
          "of 5 asked for (measured: 0 — only insertPageBreak can make one)")
    print("  plain <hr>:", len(re.findall(r"<hr>", html)), "of 1")
    for case in sorted(set(re.findall(r"\b([A-H]\d)\b", html))):
        i = html.find(case)
        last = None
        for m in re.finditer(r'<(span|p|h\d|td|li)\b[^>]*style="([^"]*)"[^>]*>',
                             html[max(0, i - 420):i]):
            last = m
        style = re.sub("&quot;", '"', last.group(2)) if last else ""
        keep = [d.strip() for d in style.split(";") if d.split(":")[0].strip() in
                ("color", "background-color", "font-family", "font-size", "font-weight",
                 "font-style", "width", "border-bottom-style", "white-space")]
        note = FEATURE_EXPECTED.get(case, "")
        print(f"  {case}: {'; '.join(keep)[:96]:98s} {('expect ' + note) if note else ''}")
    print("  minted bookmark ids:", re.findall(r'id="(id\.\w+)"', html)[:4],
          "(the source's own id= attributes are gone)")
    glyphs = re.findall(r'\.lst-kix_\S+-0 > li:before\{content:"([^"]*)"', html)
    print("  level-0 list markers:", sorted(set(glyphs)))


def upload_html(drive, html: str, name: str) -> str:
    media = MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html",
                              resumable=False)
    got = drive.files().create(
        body={"name": name, "mimeType": DOC_MIME},
        media_body=media, fields="id,name,mimeType,size",
    ).execute()
    return got["id"]


def export(drive, file_id: str, mime: str) -> bytes | None:
    try:
        return drive.files().export(fileId=file_id, mimeType=mime).execute()
    except HttpError as err:
        print(f"    export {mime}: {err.resp.status} {err.reason}")
        return None


EXPORTS = {
    "text/html": "html",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/zip": "zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/rtf": "rtf",
    "application/epub+zip": "epub",
}


def round_trip(drive, html: str, tag: str, out: Path) -> tuple[str, str | None]:
    """Upload `html`, save every export. Returns (file id, exported html)."""
    print(f"  uploading {tag} ({len(html)} bytes of HTML)")
    file_id = upload_html(drive, html, f"b2s docs probe {tag}")
    print(f"    file id {file_id}")
    exported_html = None
    for mime, ext in EXPORTS.items():
        data = export(drive, file_id, mime)
        if data is None:
            continue
        path = out / f"{tag}.{ext}"
        path.write_bytes(data)
        print(f"    export {mime:70s} -> {path.name} ({len(data)} bytes)")
        if mime == "text/html":
            exported_html = data.decode("utf-8")
    return file_id, exported_html


def try_docs_api(creds, file_id: str, out: Path) -> None:
    """Does the drive.file scope reach documents.get, and what does the model look like?"""
    from googleapiclient.discovery import build

    try:
        docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        doc = docs.documents().get(documentId=file_id, includeTabsContent=True).execute()
    except HttpError as err:
        print(f"  docs.documents.get: {err.resp.status} {err.reason}")
        print("  -> the documents scope is needed (re-consent), or tabs are unsupported")
        try:
            docs = build("docs", "v1", credentials=creds, cache_discovery=False)
            doc = docs.documents().get(documentId=file_id).execute()
        except HttpError as err2:
            print(f"  docs.documents.get (no tabs): {err2.resp.status} {err2.reason}")
            return
    (out / "document.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"  docs.documents.get: ok, revisionId {doc.get('revisionId')!r}, "
          f"{len(json.dumps(doc))} bytes of JSON -> document.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the files in Drive")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    png = stress_png()
    (OUT / "figure.png").write_bytes(png)
    source = stress_html(png)
    (OUT / "source.html").write_text(source, encoding="utf-8")
    print(f"source -> {OUT / 'source.html'}")

    creds = credentials()
    drive = drive_service(creds)
    ids = []

    print("pass 1: source HTML -> Doc")
    id1, html1 = round_trip(drive, source, "pass1", OUT)
    ids.append(id1)
    try_docs_api(creds, id1, OUT)

    if html1:
        print("pass 2: the exported HTML -> Doc again (does the round trip settle?)")
        id2, html2 = round_trip(drive, html1, "pass2", OUT)
        ids.append(id2)
        if html2:
            same = html1 == html2
            print(f"  pass1.html == pass2.html: {same}")
            if not same:
                print(f"  lengths {len(html1)} vs {len(html2)}")
            print("pass 3: once more, to see whether pass 2 was the fixed point")
            id3, html3 = round_trip(drive, html2, "pass3", OUT)
            ids.append(id3)
            if html3:
                print(f"  pass2.html == pass3.html: {html2 == html3}")

    print("\nfeature cases: page breaks, selectors, anchors, table widths, fonts")
    (OUT / "features-source.html").write_text(FEATURE_HTML, encoding="utf-8")
    fid = upload_html(drive, FEATURE_HTML, "b2s docs probe features")
    ids.append(fid)
    data = export(drive, fid, "text/html")
    if data:
        (OUT / "features.html").write_bytes(data)
        feature_report(data.decode("utf-8"))

    print("\nformats Drive itself admits to (the only authoritative list)")
    about = drive.about().get(
        fields="importFormats,exportFormats,maxImportSizes").execute()
    imports = [m for m, t in about["importFormats"].items() if DOC_MIME in t]
    print("  import ->Doc:", ", ".join(sorted(imports)))
    print("  export Doc->:", ", ".join(about["exportFormats"].get(DOC_MIME, [])))
    print("  max import size:", about.get("maxImportSizes", {}).get(DOC_MIME))

    if args.keep:
        print("kept in Drive:", ", ".join(ids))
    else:
        for file_id in ids:
            drive.files().delete(fileId=file_id).execute()
        print(f"deleted {len(ids)} probe files from Drive")
    print(f"\noutputs in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
