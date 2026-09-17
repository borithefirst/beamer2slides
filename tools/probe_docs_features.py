"""How far can the HTML dialect reach into Google Docs' own model?

The first probe (`probe_docs_html.py`) graded the HTML export, which only shows what
survives a second lossy conversion. This one reads the document back with
`documents.get`, so it reports what Docs actually built: page size and margins, small
caps, text direction, baseline offsets, list start numbers, pinned header rows, and
every exotic ParagraphElement (person / richLink / dateElement / equation / footnote).

    .venv\\Scripts\\python.exe tools\\probe_docs_features.py [--keep]

Each case is a paragraph whose first token labels it. See docs/google-docs.md.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from googleapiclient.http import MediaIoBaseUpload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from beamer2slides.google_auth import credentials, docs_service, drive_service  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "docs-probe"
DOC_MIME = "application/vnd.google-apps.document"

HTML = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>b2s feature reach</title>
<style>
  @page { size: A4 landscape; margin: 2.5cm 3cm 2cm 3cm; }
  @page :first { margin-top: 5cm; }
  .counted { counter-increment: step; }
  .counted::before { content: "step " counter(step) ". "; }
</style></head><body>

<h1>Feature reach</h1>

<p style="break-before:page">P01 modern break-before page</p>
<div style="break-after:page">P02 modern break-after page</div>
<p>P03 after the modern breaks</p>

<p><span style="font-variant:small-caps">T01 small caps via font-variant</span></p>
<p><span style="font-variant-caps:small-caps">T02 small caps via font-variant-caps</span></p>
<p><span style="text-decoration:underline line-through">T03 both decorations</span></p>
<p><span style="text-decoration:underline wavy #cc0000">T04 wavy red underline</span></p>
<p><span style="word-spacing:6pt">T05 word spacing six points</span></p>
<p>T06 baseline <span style="vertical-align:6pt">raised 6pt</span> back down</p>
<p>T07 legacy <big>big</big> <tt>tt</tt> <strike>strike</strike> <em>em</em></p>
<p><font color="#ff0000" size="6" face="Georgia">T08 legacy font element</font></p>
<center>T09 legacy center element</center>
<p align="right">T10 legacy align attribute</p>
<p dir="rtl">T11 مرحبا paragraph marked dir rtl</p>
<p>T12 <bdo dir="rtl">reversed run</bdo> after bdo</p>
<p>T13 ruby <ruby>漢<rt>kan</rt></ruby> done</p>
<p>T14 emoji 👩‍🔬 ZWJ, combining X&#x0304;, flag 🇫🇷</p>
<p>T15 <abbr title="hover text">abbr</abbr> <time datetime="2026-01-01">time</time>
   <data value="42">data</data></p>
<p>T16 soft&shy;hyphen wbr<wbr>here zwj&zwj;joined</p>
<p style="writing-mode:vertical-rl">T17 vertical writing mode</p>
<p><span style="font-family:Georgia;font-weight:600">T18 weight 600 semibold</span></p>
<p style="background-color:#ffe08a">T19 paragraph shading</p>
<p style="border-left:3pt solid #3366cc;padding-left:8pt">T20 left border rule</p>
<p style="margin-top:18pt;margin-bottom:18pt">T21 space above and below</p>
<p style="orphans:3;widows:3">T22 orphans and widows</p>
<p style="text-align:justify;text-justify:inter-word">T23 justified</p>

<ol reversed><li>L01 reversed first</li><li>L02 reversed second</li></ol>
<ol><li value="7">L03 li value seven</li><li>L04 follows</li></ol>
<ul style="list-style-image:url(data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7)">
  <li>L05 list style image</li></ul>
<p class="counted">L06 css counter before</p>
<ul><li>L07 ☐ unicode ballot box</li><li>L08 ☑ checked</li></ul>

<header>S01 header element</header>
<footer>S02 footer element</footer>
<figure><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
  alt="one pixel"><figcaption>S03 figcaption</figcaption></figure>
<details><summary>S04 summary</summary><p>S05 details body</p></details>
<table><caption>S06 table caption</caption>
  <thead><tr><th>S07 head</th></tr></thead>
  <tbody><tr><td>S08 body cell</td></tr></tbody></table>
<section><p>S09 inside section</p></section>
<aside><p>S10 inside aside</p></aside>
<blockquote style="margin-left:36pt"><p>S11 blockquote with explicit indent</p></blockquote>

<p>G01 inline svg: <svg width="20" height="20"><circle cx="10" cy="10" r="9" fill="red"/></svg></p>
<p>G02 svg data uri: <img alt="svg" width="20" height="20"
  src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyMCIgaGVpZ2h0PSIyMCI+PGNpcmNsZSBjeD0iMTAiIGN5PSIxMCIgcj0iOSIgZmlsbD0icmVkIi8+PC9zdmc+"></p>
<p>G03 mathml: <math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mi>a</mi><mi>b</mi></mfrac></math></p>
<p>G04 iframe: <iframe src="https://example.com" width="80" height="40"></iframe></p>

<p class="title">N01 paragraph with class title</p>
<p class="subtitle">N02 paragraph with class subtitle</p>
<h2><a id="h.b2sanchor"></a>N03 heading carrying a Docs-style empty anchor</h2>
<p>N04 <a href="#h.b2sanchor">link to that anchor</a></p>

<p>F01 a claim needing a note<sup><a href="#ftnt1" id="ftnt_ref1">[1]</a></sup>.</p>

<p style="margin-right:2cm">R01 right margin two centimetres</p>
<p style="break-inside:avoid;break-after:avoid">R02 keep together and with next</p>
<p style="orphans:2;widows:2">R03 orphans and widows two</p>

<div style="column-count:2;column-gap:1cm">
  <p>C01 first column paragraph</p><p>C02 second column paragraph</p></div>

<table style="border-collapse:collapse">
  <colgroup><col width="120"><col width="300"></colgroup>
  <tr><td width="120" style="vertical-align:middle">K01 col width 120</td>
      <td style="vertical-align:bottom">K02 col width 300</td></tr></table>

<p>M01 <a href="https://example.com/x" target="_blank" rel="noopener" title="tip">link with attrs</a></p>
<p>M02 block link:</p>
<a href="https://example.com/block"><p>M03 paragraph inside an anchor</p></a>
<p lang="de">M04 paragraph marked lang de</p>

<hr><div><p><a href="#ftnt_ref1" id="ftnt1">[1]</a> F02 the footnote body, Docs export spelling.</p></div>
</body></html>
"""

# Page setup is document-level, so each dialect needs its own document. The Word
# spelling is the interesting one: Google's importer has to eat Word-generated HTML.
PAGE_SETUP = {
    "plain @page": """<html><head><style>
        @page { size: A4 landscape; margin: 2cm 3cm; }</style></head>
        <body><p>page setup probe</p></body></html>""",
    "@page + size in pt": """<html><head><style>
        @page { size: 842pt 595pt; margin-top: 56pt; margin-left: 85pt; }</style></head>
        <body><p>page setup probe</p></body></html>""",
    "Word WordSection1": """<html><head><style>
        @page WordSection1 { size: 841.9pt 595.3pt; margin: 56.7pt 85.05pt 56.7pt 85.05pt;
          mso-header-margin: 35.4pt; mso-footer-margin: 35.4pt; }
        div.WordSection1 { page: WordSection1; }</style></head>
        <body><div class="WordSection1"><p>page setup probe</p></div></body></html>""",
    "body background": """<html><head></head>
        <body style="background-color:#fff8e1"><p>page setup probe</p></body></html>""",
}


def page_setup(drive, docs) -> list[str]:
    """Can any HTML dialect reach documentStyle? One document per dialect."""
    print("\n=== page setup: does any dialect move pageSize / margins / background? ===")
    print("  (the default is Letter 612x792 with 72pt margins)")
    made = []
    for name, html in PAGE_SETUP.items():
        media = MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html")
        fid = drive.files().create(body={"name": f"b2s page {name}", "mimeType": DOC_MIME},
                                   media_body=media, fields="id").execute()["id"]
        made.append(fid)
        ds = docs.documents().get(documentId=fid).execute().get("documentStyle", {})
        size = ds.get("pageSize", {})
        moved = (round(size.get("width", {}).get("magnitude", 0)) != 612
                 or round(ds.get("marginLeft", {}).get("magnitude", 0)) != 72)
        print(f"  {name:20s} {terse(size)}  margins "
              f"{terse(ds.get('marginTop', {}))}/{terse(ds.get('marginLeft', {}))}"
              f"  background={terse(ds.get('background', {}))}"
              f"  {'<-- MOVED' if moved else ''}")
    return made


def phase_two(docs, doc_id: str) -> None:
    """What can batchUpdate add that no HTML can express? One request at a time,
    so a refusal reports itself instead of killing the batch."""
    print("\n=== phase two: the batchUpdate reach beyond HTML ===")
    doc = docs.documents().get(documentId=doc_id).execute()
    end = doc["body"]["content"][-1]["endIndex"] - 1
    # A paragraph to restyle: the one labelled T01.
    target = next((el for el in doc["body"]["content"]
                   if "".join(r.get("textRun", {}).get("content", "")
                              for r in el.get("paragraph", {}).get("elements", []))
                   .startswith("T01")), None)
    rng = {"startIndex": target["startIndex"], "endIndex": target["endIndex"] - 1}

    attempts = [
        ("page size + margins", {"updateDocumentStyle": {
            "documentStyle": {"pageSize": {"width": {"magnitude": 842, "unit": "PT"},
                                           "height": {"magnitude": 595, "unit": "PT"}},
                              "marginLeft": {"magnitude": 85, "unit": "PT"}},
            "fields": "pageSize,marginLeft"}}),
        ("page background", {"updateDocumentStyle": {
            "documentStyle": {"background": {"color": {"color": {"rgbColor": {
                "red": 1, "green": 0.97, "blue": 0.88}}}}}, "fields": "background"}}),
        ("small caps", {"updateTextStyle": {
            "range": rng, "textStyle": {"smallCaps": True}, "fields": "smallCaps"}}),
        ("weighted font 600", {"updateTextStyle": {
            "range": rng, "textStyle": {"weightedFontFamily": {
                "fontFamily": "Roboto", "weight": 600}}, "fields": "weightedFontFamily"}}),
        ("paragraph shading", {"updateParagraphStyle": {
            "range": rng, "paragraphStyle": {"shading": {"backgroundColor": {"color": {
                "rgbColor": {"red": 0.95, "green": 0.96, "blue": 0.98}}}}},
            "fields": "shading"}}),
        ("paragraph border", {"updateParagraphStyle": {
            "range": rng, "paragraphStyle": {"borderLeft": {
                "color": {"color": {"rgbColor": {"blue": 0.8}}},
                "width": {"magnitude": 3, "unit": "PT"},
                "padding": {"magnitude": 8, "unit": "PT"}, "dashStyle": "SOLID"}},
            "fields": "borderLeft"}}),
        ("keep with next", {"updateParagraphStyle": {
            "range": rng, "paragraphStyle": {"keepWithNext": True, "keepLinesTogether": True,
                                             "avoidWidowAndOrphan": True},
            "fields": "keepWithNext,keepLinesTogether,avoidWidowAndOrphan"}}),
        ("header", {"createHeader": {"type": "DEFAULT"}}),
        ("footer", {"createFooter": {"type": "DEFAULT"}}),
        ("footnote", {"createFootnote": {"location": {"index": end}}}),
        ("page break", {"insertPageBreak": {"location": {"index": end}}}),
        ("section break", {"insertSectionBreak": {"location": {"index": end},
                                                  "sectionType": "NEXT_PAGE"}}),
        ("date chip", {"insertDate": {"location": {"index": end},
                                      "date": {"year": 2026, "month": 9, "day": 17}}}),
        ("person chip", {"insertPerson": {"location": {"index": end},
                                          "person": {"personProperties": {
                                              "email": "someone@example.com"}}}}),
        ("rich link chip", {"insertRichLink": {"location": {"index": end},
                                               "uri": "https://example.com"}}),
        ("checkbox bullets", {"createParagraphBullets": {
            "range": rng, "bulletPreset": "BULLET_CHECKBOX"}}),
        ("table of contents", {"insertTableOfContents": {"location": {"index": end}}}),
    ]
    for name, request in attempts:
        try:
            docs.documents().batchUpdate(documentId=doc_id,
                                         body={"requests": [request]}).execute()
            print(f"  {name:22s} OK")
        except Exception as err:  # HttpError and anything the client raises first
            reason = getattr(getattr(err, "resp", None), "reason", "") or str(err)
            detail = str(err)
            if "Invalid JSON payload" in detail or "Cannot find field" in detail:
                reason = "no such request/field in this API version"
            print(f"  {name:22s} refused: {reason[:96]}")

# TextStyle / ParagraphStyle fields worth showing when they are set.
TEXT_KEYS = ("bold", "italic", "underline", "strikethrough", "smallCaps",
             "baselineOffset", "fontSize", "weightedFontFamily", "foregroundColor",
             "backgroundColor", "link")
PARA_KEYS = ("namedStyleType", "alignment", "direction", "indentStart", "indentFirstLine",
             "spaceAbove", "spaceBelow", "lineSpacing", "shading", "borderLeft",
             "keepLinesTogether", "keepWithNext")


def terse(value):
    """Collapse the API's nested colour/dimension objects to something readable."""
    if isinstance(value, dict):
        if "color" in value and "rgbColor" in value.get("color", {}):
            rgb = value["color"]["rgbColor"]
            return "#%02x%02x%02x" % tuple(round(255 * rgb.get(c, 0))
                                           for c in ("red", "green", "blue"))
        if "magnitude" in value:
            return f"{value['magnitude']:g}{value.get('unit', '')}"
        if "fontFamily" in value:
            return f"{value['fontFamily']}@{value.get('weight', '')}"
        if "url" in value:
            return value["url"][:40]
        if set(value) <= {"headingId", "bookmarkId", "tabId"}:
            return json.dumps(value)
        inner = {k: terse(v) for k, v in value.items() if v not in ({}, None)}
        return inner or None
    return value


def styles(para: dict) -> tuple[str, str, str]:
    """(label text, run style, paragraph style) for one paragraph."""
    text, run_style = "", {}
    exotic = []
    for el in para.get("elements", []):
        if "textRun" in el:
            text += el["textRun"].get("content", "")
            if not run_style:
                run_style = el["textRun"].get("textStyle", {})
        else:
            exotic.extend(k for k in el if k not in ("startIndex", "endIndex"))
    shown = {k: terse(v) for k, v in run_style.items() if k in TEXT_KEYS}
    shown = {k: v for k, v in shown.items() if v not in (False, None, {})}
    pstyle = {k: terse(v) for k, v in para.get("paragraphStyle", {}).items()
              if k in PARA_KEYS}
    pstyle = {k: v for k, v in pstyle.items()
              if v not in (None, {}, "NORMAL_TEXT", "LEFT_TO_RIGHT", False, "0pt")}
    if exotic:
        pstyle["ELEMENTS"] = sorted(set(exotic))
    bullet = para.get("bullet")
    if bullet:
        pstyle["bullet"] = f"{bullet['listId'][-6:]}/L{bullet.get('nestingLevel', 0)}"
    return text.rstrip("\n"), json.dumps(shown), json.dumps(pstyle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "features-source.html").write_text(HTML, encoding="utf-8")

    creds = credentials()
    drive, docs = drive_service(creds), docs_service(creds)
    media = MediaIoBaseUpload(io.BytesIO(HTML.encode("utf-8")), mimetype="text/html")
    meta = drive.files().create(body={"name": "b2s feature reach", "mimeType": DOC_MIME},
                                media_body=media, fields="id,name").execute()
    doc_id = meta["id"]
    print(f"created {doc_id}")
    print(f"  Drive named the file {meta['name']!r} "
          f"({'<title> was used' if meta['name'] != 'b2s feature reach' else 'the <title> was ignored'})")

    doc = docs.documents().get(documentId=doc_id).execute()
    (OUT / "features-document.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                                encoding="utf-8")

    ds = doc.get("documentStyle", {})
    print("\n=== document style (does @page reach it?) ===")
    print(f"  pageSize   {terse(ds.get('pageSize', {}))}   (Letter is 612x792, A4 595x842)")
    print("  margins    " + ", ".join(
        f"{k[6:].lower()} {terse(ds.get(k, {}))}" for k in
        ("marginTop", "marginBottom", "marginLeft", "marginRight")))
    print(f"  header/footer ids: {ds.get('defaultHeaderId')} / {ds.get('defaultFooterId')}"
          f"   firstPageDifferent={ds.get('useFirstPageHeaderFooter')}")
    print(f"  headers in doc: {list(doc.get('headers', {}))}, "
          f"footers: {list(doc.get('footers', {}))}, footnotes: {list(doc.get('footnotes', {}))}")

    print("\n=== per case: what Docs built ===")
    print(f"  {'case':5s} {'text':34s} {'run style':58s} paragraph style")
    for el in doc["body"]["content"]:
        if "paragraph" not in el:
            if "table" in el:
                t = el["table"]
                print(f"  TABLE rows={t['rows']} cols={t['columns']} "
                      f"pinned={t.get('tableStyle', {}).get('tableColumnProperties') and '?' or ''}"
                      f"{el['table'].get('tableRows', [{}])[0].get('tableRowStyle', {})}")
            elif "sectionBreak" in el:
                style = el["sectionBreak"].get("sectionStyle", {})
                cols = style.get("columnProperties")
                print(f"  SECTION BREAK columns={len(cols) if cols else 1} "
                      f"type={style.get('sectionType')}")
            continue
        text, run, para = styles(el["paragraph"])
        if not text.strip():
            continue
        label = text.split(" ", 1)[0][:5]
        print(f"  {label:5s} {text[:34]:34s} {run[:58]:58s} {para}")

    print("\n=== inline objects ===")
    for oid, obj in doc.get("inlineObjects", {}).items():
        emb = obj["inlineObjectProperties"]["embeddedObject"]
        print(f"  {oid}: size={terse(emb.get('size', {}))} title={emb.get('title')!r} "
              f"desc={emb.get('description')!r} "
              f"has imageProperties={'imageProperties' in emb}")
    print(f"  positionedObjects: {list(doc.get('positionedObjects', {}))}")

    print("\n=== lists ===")
    for lid, lst in doc.get("lists", {}).items():
        levels = lst["listProperties"]["nestingLevels"]
        print(f"  {lid[-8:]}: " + " | ".join(
            # Ordered levels carry glyphType, unordered ones glyphSymbol.
            f"L{i} {lv.get('glyphSymbol') or lv.get('glyphType')}"
            f"{'' if lv.get('startNumber', 1) == 1 else ' start=' + str(lv['startNumber'])}"
            for i, lv in enumerate(levels[:3])))

    phase_two(docs, doc_id)

    extra = page_setup(drive, docs)

    if args.keep:
        print(f"\nkept: https://docs.google.com/document/d/{doc_id}/edit")
        print("      " + ", ".join(extra))
    else:
        for fid in [doc_id, *extra]:
            drive.files().delete(fileId=fid).execute()
        print(f"\ndeleted {1 + len(extra)} files from Drive")
    print(f"full JSON in {OUT / 'features-document.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
