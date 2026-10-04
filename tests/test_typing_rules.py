"""The code patterns that make the type checker do the work (docs/typing.md), held by a ratchet.

The checker can only see what the code says. A default argument is a value a caller can forget to
give and nothing notices; a dataclass field with a default is the same hole in a record (688ebf4 was
a shape built without its `flip`); `Any` is a value the checker stops following; a suppression
comment or `typing.cast` is an assertion nobody checks. So:
  * suppression comments and `typing.cast` do not exist in the package (the checker ignores the
    comments anyway: pyproject.toml's enabled-ignores is empty, so one would only mislead);
  * default arguments, defaulted dataclass fields and `Any` are counted per module against
    typecheck/rules.json, which only goes down: a module's count may fall (lower its entry, or run
    this file as a script to rewrite the ledger at today's counts, which refuses to raise one) and a
    module not in the ledger has none.

Signatures an outside protocol imposes (PEP 517's hooks) live outside the package, in
build_backend/."""

import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "beamer2slides"
LEDGER = Path(__file__).parent.parent / "typecheck" / "rules.json"
RULES = ("default_arguments", "defaulted_fields", "any")

SUPPRESSION = re.compile(r"#\s*(type|pyrefly|pyright|mypy|pyre|ty)\s*:\s*ignore|#\s*pyre-(ignore|fixme)")


def modules() -> dict[str, Path]:
    return {p.relative_to(SRC).as_posix(): p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts}


def is_dataclass(node: ast.ClassDef) -> bool:
    for d in node.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if (isinstance(target, ast.Name) and target.id == "dataclass") or \
                (isinstance(target, ast.Attribute) and target.attr == "dataclass"):
            return True
    return False


def counts(source: str) -> Counter[str]:
    """How often one module uses each counted pattern."""
    found: Counter[str] = Counter()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            found["default_arguments"] += len(node.args.defaults) + sum(d is not None for d in node.args.kw_defaults)
        elif isinstance(node, ast.ClassDef) and is_dataclass(node):
            found["defaulted_fields"] += sum(isinstance(b, ast.AnnAssign) and b.value is not None for b in node.body)
        elif (isinstance(node, ast.Name) and node.id == "Any") or \
                (isinstance(node, ast.Attribute) and node.attr == "Any" and isinstance(node.value, ast.Name)
                 and node.value.id == "typing"):
            found["any"] += 1
    return found


def today() -> dict[str, dict[str, int]]:
    """{rule: {module: count}} over the package, zeros left out."""
    per_module = {name: counts(path.read_text(encoding="utf-8")) for name, path in modules().items()}
    return {rule: {name: c[rule] for name, c in per_module.items() if c[rule]} for rule in RULES}


def ledger() -> dict[str, dict[str, int]]:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def comments(source: str) -> list[tuple[int, str]]:
    return [(t.start[0], t.string) for t in tokenize.generate_tokens(io.StringIO(source).readline)
            if t.type == tokenize.COMMENT]


def test_no_suppression_comment_and_no_cast() -> None:
    found: list[str] = []
    for name, path in modules().items():
        source = path.read_text(encoding="utf-8")
        # (a comment the pattern matches is a piece of the source it matches, so a source it
        # matches nowhere need not be tokenized: most of this test's time)
        if SUPPRESSION.search(source):
            found += [f"{name}:{line}: {text}" for line, text in comments(source) if SUPPRESSION.search(text)]
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module in ("typing", "typing_extensions") and \
                    any(a.name == "cast" for a in node.names):
                found.append(f"{name}:{node.lineno}: imports typing's cast")
            elif isinstance(node, ast.Attribute) and node.attr == "cast" and isinstance(node.value, ast.Name) \
                    and node.value.id in ("typing", "typing_extensions"):
                found.append(f"{name}:{node.lineno}: typing.cast")
    assert not found, "rewrite the code so the checker agrees instead:\n" + "\n".join(found)


def test_the_counted_patterns_only_go_down() -> None:
    now, allowed = today(), ledger()
    grew: list[str] = []
    fell: list[str] = []
    for rule in RULES:
        for name in sorted(set(now[rule]) | set(allowed[rule])):
            n, cap = now[rule].get(name, 0), allowed[rule].get(name, 0)
            if n > cap:
                grew.append(f"{name}: {n} {rule}, the ledger allows {cap}")
            elif n < cap:
                fell.append(f"{name}: {n} {rule}, the ledger says {cap}")
    assert not grew, ("new code takes no default arguments, gives its dataclass fields no defaults and "
                      "names no Any (docs/typing.md):\n" + "\n".join(grew))
    assert not fell, ("fewer than the ledger holds - good: lower typecheck/rules.json "
                      "(python tests/test_typing_rules.py rewrites it)\n" + "\n".join(fell))


def write_ledger() -> None:
    """Rewrite the ledger at today's counts; refuses when any count is above its ledger entry."""
    now = today()
    if LEDGER.exists():
        allowed = ledger()
        grew = [f"{name} {rule}" for rule in RULES for name, n in now[rule].items() if n > allowed[rule].get(name, 0)]
        if grew:
            sys.exit("not written: counts above the ledger (" + ", ".join(grew) + ")")
    LEDGER.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print({rule: sum(now[rule].values()) for rule in RULES})


if __name__ == "__main__":
    write_ledger()
