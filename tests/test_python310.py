"""The package imports and runs on Python 3.10, which Google's monorepo still builds some targets
with (reported against 0.8.3: an adopt of any right-to-left deck died on `re.error: unknown
extension ?>`, because `bidi` read its Unicode tables out of `pdf.pure`, whose package import ran
the whole reader, whose tokenizers used an atomic group - 3.11 syntax).

This machine and CI's main jobs run 3.12, so the promises are checked from here by reading the
source: 3.10 grammar, no regex syntax newer than 3.10, and a table import that loads no reader.
The `python310` workflow runs the offline suite itself on 3.10."""

import ast
import re
import subprocess
from pathlib import Path

from beamer2slides import google_auth, interpreter

SRC = Path(google_auth.__file__).parent   # (not resolved: see tests/test_gapi.py)
# `(?>...)` atomic groups and `*+ ++ ?+ }+` possessive quantifiers: Python 3.11
NEW_REGEX = re.compile(r"\(\?>|(?<!\\)[*+?}]\+")


def sources():
    return sorted(SRC.rglob("*.py"))


def test_every_module_parses_as_python_310():
    bad = []
    for p in sources():
        try:
            ast.parse(p.read_text(encoding="utf-8-sig"), feature_version=(3, 10))
        except SyntaxError as e:
            bad.append(f"{p.relative_to(SRC)}:{e.lineno}: {e.msg}")
    assert not bad


def test_no_regex_uses_syntax_newer_than_310():
    """Every string handed to `re.compile`/`re.match`/... directly: an atomic group is written
    `(?=(X))\\1` instead (`pdf.pure.document._WORD`)."""
    bad = []
    for p in sources():
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" and node.args:
                pattern = node.args[0]
                parts = [pattern] if not isinstance(pattern, ast.BinOp) else list(ast.walk(pattern))
                for part in parts:
                    if isinstance(part, ast.Constant) and isinstance(part.value, (str, bytes)):
                        text = part.value if isinstance(part.value, str) else part.value.decode("latin-1")
                        if NEW_REGEX.search(text):
                            bad.append(f"{p.relative_to(SRC)}:{node.lineno}")
    assert not bad


def test_the_unicode_tables_load_no_pdf_reader():
    code = ("import sys, beamer2slides.pdf.pure.unicode_data, beamer2slides.bidi\n"
            "print(sorted(m for m in sys.modules if m.startswith('beamer2slides.pdf.pure.') "
            "and m != 'beamer2slides.pdf.pure.unicode_data'))")
    done = subprocess.run([interpreter.python(), "-c", code], capture_output=True, text=True,
                          env=interpreter.env(), timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
