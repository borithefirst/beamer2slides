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

The shapes handed out are TypedDicts (`ToolSchema`, `Described`, ...): their keys are fixed, so
a reader indexes them checked, and `*_json` turns one into plain JSON where it leaves.
"""

from __future__ import annotations

import inspect
import types as _pytypes
import typing
from collections import abc
from collections.abc import Mapping
from typing import Annotated, Literal, TypedDict, get_args, get_origin

from ..json_types import Json, JsonObject, as_object
from .content import SHAPES
from .context import Tool
from .types import READS, READS_GOOGLE, WRITES, WRITES_GOOGLE, Need, Refused

__all__ = ["SchemaError", "tool_schema", "describe", "all_schemas", "anthropic_tools",
           "openai_tools", "validate", "registry", "effects", "needs_of", "type_hints",
           "ToolSchema", "InputSchema", "Effects", "Described", "AnthropicTool", "OpenAITool",
           "input_schema_json", "described_json"]


class SchemaError(Exception):
    """A tool that cannot be published as written. Always names the tool, usually the parameter."""


class InputSchema(TypedDict):
    """A tool's parameters: a closed object of named, described properties."""

    type: Literal["object"]
    properties: dict[str, JsonObject]
    required: list[str]
    additionalProperties: bool


class ToolSchema(TypedDict):
    name: str
    description: str
    input_schema: InputSchema


Approval = Literal["required", "recommended", "none"]


class Effects(TypedDict):
    """What a tool does to the world, derived from its `needs` (`effects`)."""

    reads_local: bool
    reads_google: bool
    writes_local: bool
    writes_google: bool
    google: bool
    writes: bool
    approval: Approval


class Described(TypedDict):
    """`tool_schema` plus `needs` as declared and `effects` derived."""

    name: str
    description: str
    input_schema: InputSchema
    needs: list[Need]
    effects: Effects


class AnthropicTool(TypedDict):
    name: str
    description: str
    input_schema: InputSchema


class OpenAIFunction(TypedDict):
    name: str
    description: str
    parameters: InputSchema


class OpenAITool(TypedDict):
    type: Literal["function"]
    function: OpenAIFunction


# Python type -> JSON Schema fragment. Anything not in here is refused by name, so a tool that
# grows a parameter this module cannot describe fails at publication rather than at the model.
_SCALARS: dict[type, JsonObject] = {
    str: {"type": "string"},
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
}


# -- reading a decorated tool ------------------------------------------------------------------

def needs_of(fn: Tool[...]) -> tuple[Need, ...]:
    return fn.needs


def _description(fn: Tool[...]) -> str:
    doc = inspect.getdoc(fn.body) or ""
    if not doc.strip():
        raise SchemaError(f"{fn.tool_name} has no docstring, and a tool's docstring is the "
                          f"description the model is given. Write one.")
    return doc.strip()


def type_hints(obj: object) -> dict[str, object]:
    """`typing.get_type_hints(obj, include_extras=True)` as 3.11 and later answer it. Python 3.10
    still wraps a parameter defaulting to None in Optional, so `Annotated[str | None, "..."] = None`
    comes back as `Optional[Annotated[...]]`, its description one level down: unwrapped here."""
    hints: dict[str, object] = dict(typing.get_type_hints(obj, include_extras=True))
    for name, hint in hints.items():
        args: tuple[object, ...] = (get_args(hint) if get_origin(hint) in (typing.Union, _pytypes.UnionType)
                                    else ())
        if len(args) == 2 and type(None) in args:
            inner = args[0] if args[1] is type(None) else args[1]
            if get_origin(inner) is Annotated:
                hints[name] = inner
    return hints


def _hints(fn: Tool[...]) -> dict[str, object]:
    try:
        return type_hints(fn.body)
    except Exception as exc:                                   # a forward reference that moved
        raise SchemaError(f"{fn.tool_name}: its annotations cannot be resolved "
                          f"({type(exc).__name__}: {exc}).") from None


def _annotation(fn: Tool[...], param: str, hint: object) -> tuple[object, str]:
    """The declared type and the one-line description, or a loud refusal."""
    tool = fn.tool_name
    if hint is inspect.Parameter.empty or hint is None:
        raise SchemaError(f"{tool}: parameter {param!r} has no annotation. Every parameter is "
                          f"Annotated[<type>, \"<one line for the model>\"].")
    if get_origin(hint) is not Annotated:
        raise SchemaError(f"{tool}: parameter {param!r} is annotated {hint!r} with no "
                          f"description. Write Annotated[{hint!r}, \"<one line for the model>\"].")
    parts: tuple[object, ...] = get_args(hint)
    declared, extra = parts[0], parts[1:]
    described = next((m for m in extra if isinstance(m, str)), None)
    if described is None:
        raise SchemaError(f"{tool}: parameter {param!r} is Annotated but carries no string "
                          f"description for the model.")
    if not described.strip():
        raise SchemaError(f"{tool}: the description of parameter {param!r} is empty.")
    return declared, " ".join(described.split())


def _json_type(fn: Tool[...], param: str, declared: object) -> JsonObject:
    """A JSON Schema fragment for one declared Python type."""
    tool = fn.tool_name
    origin = get_origin(declared)

    if origin in (typing.Union, _pytypes.UnionType):
        members: tuple[object, ...] = get_args(declared)
        parts = [a for a in members if a is not type(None)]
        if str in parts and all(a is str or a in SHAPES for a in parts):
            # `content.File`: a ref, or the file as content. Published as the string it is by
            # the time `validate` reads it - `take_in` has made content a ref before that.
            parts = [str]
        if len(parts) != 1:
            raise SchemaError(f"{tool}: parameter {param!r} is a union of several real types "
                              f"({declared!r}); this layer publishes `X` and `X | None` only.")
        inner = _json_type(fn, param, parts[0])
        if type(None) not in members:
            return inner
        kind = inner["type"]
        kinds: list[Json] = list(kind) if isinstance(kind, list) else [kind]
        return {**inner, "type": [*kinds, "null"]}

    if origin is list or origin is abc.Sequence:          # Sequence: what a tuple default is
        args: tuple[object, ...] = get_args(declared) or (str,)
        return {"type": "array", "items": _json_type(fn, param, args[0])}

    if isinstance(declared, type) and declared in _SCALARS:
        return dict(_SCALARS[declared])

    raise SchemaError(f"{tool}: parameter {param!r} is declared {declared!r}, which this layer "
                      f"cannot publish. Use str, bool, int, float, list[str] or `X | None`.")


def _default(tool: str, param: str, value: object) -> Json:
    """A parameter's default as the JSON it is published as.

    A list parameter with a default is declared `Sequence[str]` with a tuple one (nobody writes a
    mutable default, and a tuple is no `list`); published, it has to be the JSON array it means. A default JSON cannot say is refused by name.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)):
        return [_default(tool, param, v) for v in value]
    raise SchemaError(f"{tool}: parameter {param!r} defaults to {value!r}, which JSON cannot "
                      f"say.")


def _parameters(fn: Tool[...]) -> tuple[dict[str, JsonObject], list[str]]:
    """The properties and the required names, in the order the function declares them."""
    tool = fn.tool_name
    hints = _hints(fn)
    properties: dict[str, JsonObject] = {}
    required: list[str] = []

    for i, (param, spec) in enumerate(inspect.signature(fn.body).parameters.items()):
        if i == 0:                                             # the Job the wrapper supplies
            continue
        if spec.kind in (spec.VAR_POSITIONAL, spec.VAR_KEYWORD):
            raise SchemaError(f"{tool}: {'*' if spec.kind is spec.VAR_POSITIONAL else '**'}"
                              f"{param} cannot be published; a tool takes named parameters only.")
        annotation: object = spec.annotation
        declared, described = _annotation(fn, param, hints.get(param, annotation))
        prop = _json_type(fn, param, declared)
        prop["description"] = described
        default: object = spec.default
        if default is inspect.Parameter.empty:
            required.append(param)
        else:
            prop["default"] = _default(tool, param, default)
        properties[param] = prop

    return properties, required


# -- the schemas -------------------------------------------------------------------------------

def tool_schema(fn: Tool[...]) -> ToolSchema:
    """`{"name", "description", "input_schema"}` for one decorated tool.

    `input_schema` is a closed object: every parameter of the body but the leading `Job`, typed
    from its annotation, described from its `Annotated` string, with its default where it has
    one and `additionalProperties: False` so a model's invented argument is caught here rather
    than by a `TypeError` inside the journey.
    """
    properties, required = _parameters(fn)
    return {
        "name": fn.tool_name,
        "description": _description(fn),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def effects(needs: tuple[Need, ...]) -> Effects:
    """What a harness needs in order to decide what to gate, without reading any prose.

    `approval` is the one-word verdict: a journey that changes a deck or a document someone may
    be looking at is `required`, one that writes files is `recommended`, a read is `none`.
    """
    reads_google = READS_GOOGLE in needs
    writes_google = WRITES_GOOGLE in needs
    writes_local = WRITES in needs
    approval: Approval = "required" if writes_google else "recommended" if writes_local else "none"
    return {
        "reads_local": READS in needs,
        "reads_google": reads_google or writes_google,
        "writes_local": writes_local,
        "writes_google": writes_google,
        "google": reads_google or writes_google,
        "writes": writes_local or writes_google,
        "approval": approval,
    }


def describe(fn: Tool[...]) -> Described:
    """The schema plus what the tool does to the world: `needs` as declared, `effects` derived."""
    needs = needs_of(fn)
    published = tool_schema(fn)
    return {"name": published["name"], "description": published["description"],
            "input_schema": published["input_schema"], "needs": list(needs),
            "effects": effects(needs)}


def registry(tools: Mapping[str, Tool[...]] | None) -> dict[str, Tool[...]]:
    """The supplied mapping, or `agent.tools.TOOLS` imported now rather than at import time.

    Lazily, because this module is published before the registry exists and because importing
    the registry pulls in the whole pipeline - a harness that only wants to reshape the schemas
    it already has should not pay for PDFium.
    """
    if tools is not None:
        return dict(tools)
    try:
        from . import tools as _tools
    except ImportError as exc:                                 # the registry is not importable
        raise SchemaError(f"beamer2slides.agent.tools is not importable ({exc}), so there is no "
                          f"registry to publish. Pass a mapping of name -> tool instead.") from None
    return dict(_tools.TOOLS)


def all_schemas(tools: Mapping[str, Tool[...]] | None = None) -> list[Described]:
    """`describe` for every tool in the registry, in the order the registry names them.

    `tools` keeps its default: `all_schemas()` is how a harness asks for the whole registry
    (docs/agent-tools.md, docs/playground.md).
    """
    return [describe(fn) for fn in registry(tools).values()]


def anthropic_tools(tools: Mapping[str, Tool[...]] | None = None) -> list[AnthropicTool]:
    """The Messages API's shape: `name`, `description`, `input_schema`. (Documented without
    arguments, like `all_schemas`.)"""
    return [{"name": s["name"], "description": s["description"],
             "input_schema": s["input_schema"]} for s in all_schemas(tools)]


def openai_tools(tools: Mapping[str, Tool[...]] | None = None) -> list[OpenAITool]:
    """The Chat Completions shape: a `function` object whose `parameters` is the input schema.
    (Documented without arguments, like `all_schemas`.)"""
    return [{"type": "function",
             "function": {"name": s["name"], "description": s["description"],
                          "parameters": s["input_schema"]}} for s in all_schemas(tools)]


# -- as plain JSON -----------------------------------------------------------------------------

def input_schema_json(schema: InputSchema) -> JsonObject:
    """An input schema as the JSON a harness is handed, key for key."""
    properties: JsonObject = {name: dict(prop) for name, prop in schema["properties"].items()}
    required: list[Json] = list(schema["required"])
    return {"type": schema["type"], "properties": properties, "required": required,
            "additionalProperties": schema["additionalProperties"]}


def effects_json(e: Effects) -> JsonObject:
    return {"reads_local": e["reads_local"], "reads_google": e["reads_google"],
            "writes_local": e["writes_local"], "writes_google": e["writes_google"],
            "google": e["google"], "writes": e["writes"], "approval": e["approval"]}


def described_json(d: Described) -> JsonObject:
    """`describe`'s answer as plain JSON, in the same key order."""
    needs: list[Json] = list(d["needs"])
    return {"name": d["name"], "description": d["description"],
            "input_schema": input_schema_json(d["input_schema"]), "needs": needs,
            "effects": effects_json(d["effects"])}


# -- checking what comes back --------------------------------------------------------------

def validate(fn: Tool[...], arguments: Mapping[str, object] | None) -> dict[str, Json]:
    """Check one call's arguments against the tool's schema; return them cleaned.

    Refuses an unknown key, a missing required one and a value of the wrong JSON type, always
    with `bad_request` and a sentence saying which parameter and what was expected - the model
    is going to read this and try again, so it says what to send, not what went wrong. `None`
    is a call that sent no arguments at all (an MCP client may).

    Defaults are deliberately *not* filled in: the body's own defaults are the only copy, and a
    schema that restated them would be a second place for them to drift.
    """
    published = tool_schema(fn)
    schema = published["input_schema"]
    properties = schema["properties"]
    tool = published["name"]
    given: dict[str, object] = dict(arguments) if arguments is not None else {}

    unknown = sorted(k for k in given if k not in properties)
    if unknown:
        known = ", ".join(properties) or "no parameters at all"
        raise Refused("bad_request",
                      f"{tool} was given {', '.join(unknown)}, which it does not take. "
                      f"It takes {known}.", tool=tool, unknown=list[Json](unknown),
                      parameters=list[Json](sorted(properties)))

    missing = [k for k in schema["required"] if k not in given]
    if missing:
        raise Refused("bad_request",
                      f"{tool} needs {', '.join(missing)}, which the call did not give.",
                      tool=tool, missing=list[Json](missing))

    return {k: _coerce(tool, k, properties[k], v) for k, v in given.items()}


def _kinds(prop: JsonObject) -> list[str]:
    kind = prop["type"]
    if isinstance(kind, list):
        return [k for k in kind if isinstance(k, str)]
    return [kind] if isinstance(kind, str) else []


def _coerce(tool: str, param: str, prop: JsonObject, value: object) -> Json:
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
    if ("integer" in kinds and isinstance(value, float) and not isinstance(value, bool)
            and value.is_integer()):
        return int(value)
    if "number" in kinds and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if "array" in kinds and isinstance(value, list):
        item = as_object(prop.get("items", {"type": "string"}), f"{tool}.{param}.items")
        items: list[object] = list(value)
        return [_coerce(tool, f"{param}[{i}]", item, v) for i, v in enumerate(items)]
    raise _wrong(tool, param, kinds, value)


def _wrong(tool: str, param: str, kinds: list[str], value: object) -> Refused:
    wanted = " or ".join(kinds)
    return Refused("bad_request",
                   f"{tool}: {param} should be {wanted}, not {type(value).__name__} "
                   f"({value!r}).", tool=tool, parameter=param, expected=list[Json](kinds),
                   got=type(value).__name__)
