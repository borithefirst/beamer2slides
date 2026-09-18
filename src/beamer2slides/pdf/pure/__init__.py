"""A PDF reader in pure Python that answers api.py the way PDFium does (docs/pdf-from-scratch.md).

Layers, each a port of the PDFium code it names:
- `syntax`: objects, content-stream operators (CPDF_SyntaxParser, CPDF_StreamParser);
- `filters`: Flate, LZW, ASCII85/Hex, RunLength, predictors (fpdfapi/parser/fpdf_parser_decode);
- `document`: xref tables and streams, object streams, repair, page tree, labels, destinations,
  and writing a subset back (CPDF_Parser, CPDF_Document, CPDF_PageLabel, CPDF_Creator);
- `colors`, `cmyk_table`: colour spaces to PDFium's RGB (CPDF_ColorSpace);
- `encodings`, `fonts`: font dictionaries, encodings, ToUnicode, CMaps, widths and glyph boxes
  (CPDF_Font and subclasses; font programs read with fontTools);
- `content`: page objects with PDFium's matrices, rects and clips (CPDF_StreamContentParser);
- `textpage`: the text layer - order, generated spaces, hyphens, boxes (CPDF_TextPage);
- `backend`: api.PdfBackend over all of it (`B2S_PDF_BACKEND=pure`). It does not render.
"""

from .backend import PureBackend  # noqa: F401
