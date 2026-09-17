# Intermediate representation (draft)

Two JSON files per conversion, both in PDF points with a top-left origin
(`pdf.Page` converts PDFium's coordinates). Scaling to the Slides page happens only in `emit`.

## `raw.json`: output of `extract`
A faithful, lossless-enough dump of each page. No interpretation.

```jsonc
{
  "version": 1,
  "source": { "pdf": "talk.pdf", "producer": "MiKTeX pdfTeX-1.40.28" },
  "pages": [
    {
      "index": 0,
      "label": "2",                     // beamer frame number (PDF page label)
      "size": [362.8, 272.1],
      "spans": [
        { "id": "p0s12", "text": "bold", "font": "CMSSBX10", "size": 10.91,
          "color": "#000000", "origin": [127.2, 102.9], "bbox": [127.2, 94.4, 148.6, 105.3],
          "dir": [1, 0] }
      ],
      "images": [
        { "id": "p0i0", "bbox": [22, 181, 28, 187], "px": [6, 6], "xref": 0 }   // xref 0 = inline image
      ],
      "drawings": [
        { "id": "p0d3", "type": "f", "items": "clcll", "bbox": [6.9, 70.5, 355.9, 85.3],
          "fill": "#262686", "stroke": null, "width": null }
      ],
      "links": [ { "bbox": [153.5, 132.4, 196.9, 144.1], "uri": "https://example.com" } ]
    }
  ]
}
```

## `deck.json`: output of `classify`, extended by `render`
One slide per page, holding native elements plus what was left behind and why.

```jsonc
{
  "version": 1,
  "page_size": [362.8, 272.1],
  "slides": [
    {
      "page": 0,
      "frame": "2",
      "background": "bg/slide-000.png",       // added by render
      "elements": [
        { "id": "e0", "kind": "text", "role": "title",   // title | subtitle | body | caption
          "bbox": [8.5, 9.8, 96.3, 24.2],
          "paragraphs": [
            { "level": 0,
              "bullet": null,                         // { "glyph": "▶", "color": "#3333b3" } | { "numbered": "1." }
              "align": "left",                        // left | center | right
              "space_before": 0,
              "runs": [
                { "text": "Itemize, nested", "family": "sans", "size": 14.35, "color": "#3333b3",
                  "bold": false, "italic": false, "mono": false, "smallcaps": false, "link": null }
              ] }
          ],
          "spans": ["p0s0", "p0s1"] },                // raw spans consumed, removed from background

        { "kind": "image", "id": "e1", "bbox": [...], "file": "img/p0-xref12.png", "images": ["p0i2"] },

        { "kind": "shape", "id": "e2", "shape": "round_rect", "bbox": [...],
          "fill": "#262686", "outline": null, "drawings": ["p0d3"] }
      ],
      "left_in_background": [
        { "spans": ["p0s40", "p0s41"], "reason": "math" }       // math | figure | theme | rotated | unsure
      ],
      "notes": null
    }
  ]
}
```

## Rules
- Every raw span / image / drawing belongs to **exactly one** element or to
  `left_in_background`. `render` removes what elements consumed and keeps the rest.
- `reason` strings drive the colour coding of the classification debug PNGs. They are
  also how we measure progress: the share of text converted to native text, per deck.
- `emit` must not need the PDF; `deck.json` + background/image files are enough.
