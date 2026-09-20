"""The tools as JSON Schema: what a harness publishes, and what it checks an argument dict against.

A `@tool` function already carries everything a schema needs - the signature says what the
parameters are, the defaults say which are optional, `Annotated[...]` says what each one means,
the docstring says what the journey does, and `needs` says what it will do to the world. Nothing
here invents any of that; it only reads it off and reshapes it, so the description a model sees
and the code that runs are the same text and cannot drift apart.

Two decisions worth stating, because both could have gone the other way:

* **An optional `str | None` is published as `{"type": ["string", "null"]}`**, not as a plain
  string whose description says it may be left out. Both Anthropic and OpenAI accept the array
  form, it is the truth (the parameter's default really is `None`, and a harness replaying a
  logged call will send `null` back), and it lets `validate` accept an explicit null instead of
  guessing what a model meant by the string `"none"`.
* **A missing or empty `Annotated` description is an error, not a shrug.** It is the one thing
  here that rots silently: a parameter with no description still works, still runs, and simply
  costs the model the one sentence it needed. So `tool_schema` refuses to build a schema at all
  and names the tool and the parameter.

`validate` is a few dozen lines rather than a jsonschema dependency because the schemas this
module emits are the only ones it will ever see: flat objects of strings, booleans, integers and
string arrays. It converts an integral float to an int (JSON has one number type, and a harness
that round-trips `3` through a JavaScript client sends `3.0` back) and otherwise refuses.
"""

from __future__ import annotations

import inspect
import types as _pytypes
import typing
from typing import Annotated, Any, Callable, Mapping, get_args, get_origin

from .types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Refused

__all__ = ["SchemaError", "tool_schema", "describe", "all_schemas", "anthropic_tools",
           "openai_tools", "validate", "registry", "effects", "needs_of"]


class SchemaError(Exception):
    """A tool that cannot be published as written. Always names the tool, usually the parameter."""


# Python type -> JSON Schema fragment. Anything not in here is refused by name, so a tool that
# grows a parameter this module cannot describe fails at publication rather than at the model.
_SCALARS: dict[Any, dict] = {
    str: {"type": "string"},
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
}


# -- reading a decorated tool ------------------------------------------------------------------

def _body(fn: Callable) -> Callable:
    """The undecorated function `@tool` kept, or `fn` itself if it was never decorated."""
    return getattr(fn, "body", fn)


def _name(fn: Callable) -> str:
    return getattr(fn, "tool_name", None) or _body(fn).__name__


def needs_of(fn: Callable) -> tuple[str, ...]:
    return tuple(getattr(fn, "needs", (READS,)))


def _description(fn: Callable) -> str:
    doc = inspect.getdoc(_body(fn)) or ""
    if not doc.strip():
        raise SchemaError(f"{_name(fn)} has no docstring, and a tool's docstring is the "
                          f"description the model is given. Write one.")
    return doc.strip()


def _hints(fn: Callable) -> dict[str, Any]:
    body = _body(fn)
    try:
        return typing.get_type_hints(body, include_extras=True)
    except Exception as exc:                                   # a forward reference that moved
        raise SchemaError(f"{_name(fn)}: its annotations cannot be resolved "
                          f"({type(exc).__name__}: {exc}).") from None


def _annotation(fn: Callable, param: str, hint: Any) -> tuple[Any, str]:
    """The declared type and the one-line description, or a loud refusal."""
    tool = _name(fn)
    if hint is inspect.Parameter.empty or hint is None:
        raise SchemaError(f"{tool}: parameter {param!r} has no annotation. Every parameter is "
                          f"Annotated[<type>, \"<one line for the model>\"].")
    if get_origin(hint) is not Annotated:
        raise SchemaError(f"{tool}: parameter {param!r} is annotated {hint!r} with no "
                          f"description. Write Annotated[{hint!r}, \"<one line for the model>\"].")
    declared, *extra = get_args(hint)
    described = next((m for m in extra if isinstance(m, str)), None)
    if described is None:
        raise SchemaError(f"{tool}: parameter {param!r} is Annotated but carries no string "
                          f"description for the model.")
    if not described.strip():
        raise SchemaError(f"{tool}: the description of parameter {param!r} is empty.")
    return declared, " ".join(described.split())


def _json_type(fn: Callable, param: str, declared: Any) -> dict:
    """A JSON Schema fragment for one declared Python type."""
    tool = _name(fn)
    origin = get_origin(declared)

    if origin in (typing.Union, _pytypes.UnionType):
        parts = [a for a in get_args(declared) if a is not type(None)]
        if len(parts) != 1:
            raise SchemaError(f"{tool}: parameter {param!r} is a union of several real types "
                              f"({declared!r}); this layer publishes `X` and `X | None` only.")
        inner = _json_type(fn, param, parts[0])
        kinds = inner["type"] if isinstance(inner["type"], list) else [inner["type"]]
        return {**inner, "type": [*kinds, "null"]}

    if origin in (list, typing.List):
        args = get_args(declared) or (str,)
        return {"type": "array", "items": _json_type(fn, param, args[0])}

    if declared in _SCALARS:
        return dict(_SCALARS[declared])

    raise SchemaError(f"{tool}: parameter {param!r} is declared {declared!r}, which this layer "
                      f"cannot publish. Use str, bool, int, float, list[str] or `X | None`.")


def _parameters(fn: Callable) -> tuple[dict[str, dict], list[str]]:
    """The properties and the required names, in the order the function declares them."""
    tool = _name(fn)
    body = _body(fn)
    hints = _hints(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for i, (param, spec) in enumerate(inspect.signature(body).parameters.items()):
        if i == 0:                                             # the Job the wrapper supplies
            continue
        if spec.kind in (spec.VAR_POSITIONAL, spec.VAR_KEYWORD):
            raise SchemaError(f"{tool}: {'*' if spec.kind is spec.VAR_POSITIONAL else '**'}"
                              f"{param} cannot be published; a tool takes named parameters only.")
        declared, described = _annotation(fn, param, hints.get(param, spec.annotation))
        prop = _json_type(fn, param, declared)
        prop["description"] = described
        if spec.default is inspect.Parameter.empty:
            required.append(param)
        else:
            # A list parameter is declared with a tuple default (nobody writes a mutable one);
            # published, it has to be the JSON array it means.
            prop["default"] = list(spec.default) if isinstance(spec.default, tuple) else spec.default
        properties[param] = prop

    return properties, required


# -- the schemas -------------------------------------------------------------------------------

def tool_schema(fn: Callable) -> dict:
    """`{"name", "description", "input_schema"}` for one decorated tool.

    `input_schema` is a closed object: every parameter of the body but the leading `Job`, typed
    from its annotation, described from its `Annotated` string, with its default where it has
    one and `additionalProperties: False` so a model's invented argument is caught here rather
    than by a `TypeError` inside the journey.
    """
    properties, required = _parameters(fn)
    return {
        "name": _name(fn),
        "description": _description(fn),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def effects(needs: tuple[str, ...]) -> dict:
    """What a harness needs in order to decide what to gate, without reading any prose.

    `approval` is the one-word verdict: a journey that changes a deck or a document someone may
    be looking at is `required`, one that writes files is `recommended`, a read is `none`.
    """
    reads_google = READS_GOOGLE in needs
    writes_google = WRITES_GOOGLE in needs
    writes_local = WRITES in needs
    return {
        "reads_local": READS in needs,
        "reads_google": reads_google or writes_google,
        "writes_local": writes_local,
        "writes_google": writes_google,
        "google": reads_google or writes_google,
        "writes": writes_local or writes_google,
        "approval": "required" if writes_google else "recommended" if writes_local else "none",
    }


def describe(fn: Callable) -> dict:
    """The schema plus what the tool does to the world: `needs` as declared, `effects` derived."""
    needs = needs_of(fn)
    return {**tool_schema(fn), "needs": list(needs), "effects": effects(needs)}


def registry(tools: Mapping[str, Callable] | None = None) -> dict[str, Callable]:
    """The supplied mapping, or `agent.tools.TOOLS` imported now rather than at import time.

    Lazily, because this module is published before the registry exists and because importing
    the registry pulls in the whole pipeline - a harness that only wants to reshape the schemas
    it already has should not pay for PDFium.
    """
    if tools is not None:
        return dict(tools)
    try:
        from . import tools as _tools
    except ImportError as exc:                                 # the registry is not written yet
        raise SchemaError(f"beamer2slides.agent.tools is not importable ({exc}), so there is no "
                          f"registry to publish. Pass a mapping of name -> tool instead.") from None
    found = getattr(_tools, "TOOLS", None)
    if not isinstance(found, Mapping):
        raise SchemaError("beamer2slides.agent.tools has no TOOLS mapping; pass one instead.")
    return dict(found)


def all_schemas(tools: Mapping[str, Callable] | None = None) -> list[dict]:
    """`describe` for every tool in the registry, in the order the registry names them."""
    return [describe(fn) for fn in registry(tools).values()]


def anthropic_tools(tools: Mapping[str, Callable] | None = None) -> list[dict]:
    """The Messages API's shape: `name`, `description`, `input_schema`."""
    return [{"name": s["name"], "description": s["description"],
             "input_schema": s["input_schema"]} for s in all_schemas(tools)]


def openai_tools(tools: Mapping[str, Callable] | None = None) -> list[dict]:
    """The Chat Completions shape: a `function` object whose `parameters` is the input schema."""
    return [{"type": "function",
             "function": {"name": s["name"], "description": s["description"],
                          "parameters": s["input_schema"]}} for s in all_schemas(tools)]


# -- checking what comes back --------------------------------------------------------------

def validate(fn: Callable, arguments: Mapping[str, Any] | None) -> dict:
    """Check one call's arguments against the tool's schema; return them cleaned.

    Refuses an unknown key, a missing required one and a value of the wrong JSON type, always
    with `bad_request` and a sentence saying which parameter and what was expected - the model
    is going to read this and try again, so it says what to send, not what went wrong.

    Defaults are deliberately *not* filled in: the body's own defaults are the only copy, and a
    schema that restated them would be a second place for them to drift.
    """
    published = tool_schema(fn)
    schema = published["input_schema"]
    properties: dict[str, dict] = schema["properties"]
    tool = published["name"]
    given = dict(arguments or {})

    unknown = [k for k in given if k not in properties]
    if unknown:
        known = ", ".join(properties) or "no parameters at all"
        raise Refused("bad_request",
                      f"{tool} was given {', '.join(sorted(unknown))}, which it does not take. "
                      f"It takes {known}.", tool=tool, unknown=sorted(unknown),
                      parameters=sorted(properties))

    missing = [k for k in schema["required"] if k not in given]
    if missing:
        raise Refused("bad_request",
                      f"{tool} needs {', '.join(missing)}, which the call did not give.",
                      tool=tool, missing=missing)

    return {k: _coerce(tool, k, properties[k], v) for k, v in given.items()}


def _kinds(prop: dict) -> list[str]:
    kind = prop["type"]
    return list(kind) if isinstance(kind, list) else [kind]


def _coerce(tool: str, param: str, prop: dict, value: Any) -> Any:
    """One value against one property, returning it (an integral float becomes an int)."""
    kinds = _kinds(prop)

    if value is None:
        if "null" in kinds:
            return None
        raise _wrong(tool, param, kinds, value)
    if "string" in kinds and isinstance(value, str):
        return value
    if "boolean" in kinds and isinstance(value, bool):
        return value
    # `isinstance(True, int)` is True in Python and False in every model's head: a boolean is
    # never an acceptable number here.
    if "integer" in kinds and isinstance(value, int) and not isinstance(value, bool):
        return value
    if "integer" in kinds and isinstance(value, float) and value.is_integer():
        return int(value)
    if "number" in kinds and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if "array" in kinds and isinstance(value, list):
        item = prop.get("items", {"type": "string"})
        return [_coerce(tool, f"{param}[{i}]", item, v) for i, v in enumerate(value)]
    raise _wrong(tool, param, kinds, value)


def _wrong(tool: str, param: str, kinds: list[str], value: Any) -> Refused:
    wanted = " or ".join(kinds)
    return Refused("bad_request",
                   f"{tool}: {param} should be {wanted}, not {type(value).__name__} "
                   f"({value!r}).", tool=tool, parameter=param, expected=kinds,
                   got=type(value).__name__)
