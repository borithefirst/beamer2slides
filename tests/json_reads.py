"""Reading JSON in a test: the value at a path, narrowed to what the assertion needs.

What a test inspects is often JSON - a plan's JSON entry, a request emit would send, a base as
snapshot writes it - and a `JsonObject`'s values are `Json`, which cannot be indexed further until
it is narrowed. `jat(plan, "slides", 0, "units")` walks a path (a `str` step reads an object's key,
an `int` step an array's item) and the typed forms say what the value must be: `jobj`, `jarr`,
`jobjs`, `jstr`, `jstrs`, `jnum`, `jnums`, `jint`, `jbool`. A value of another shape fails the test
naming the path (`json_types.JsonShapeError`) rather than passing a wrong-typed value on; a missing
key is the `KeyError` indexing always gave. What they return is the value itself, not a copy, so a
test that edits a fixture through them edits it in place, as it always did.

`jobj(x)` with no path narrows `x` itself. (The `j` keeps them apart from a test's own `obj`s and
`text`s.)"""

from collections.abc import Sequence

from beamer2slides.json_types import Json, JsonArray, JsonObject, JsonShapeError, as_array, as_object, as_str

Step = str | int


def _where(path: Sequence[Step]) -> str:
    return "$" + "".join(f".{s}" if isinstance(s, str) else f"[{s}]" for s in path)


def jat(value: Json, *path: Step) -> Json:
    """The value at `path` in `value`."""
    for n, step in enumerate(path):
        if isinstance(step, str):
            value = as_object(value, _where(path[:n]))[step]
        else:
            value = as_array(value, _where(path[:n]))[step]
    return value


def jobj(value: Json, *path: Step) -> JsonObject:
    return as_object(jat(value, *path), _where(path))


def jarr(value: Json, *path: Step) -> JsonArray:
    return as_array(jat(value, *path), _where(path))


def jobjs(value: Json, *path: Step) -> list[JsonObject]:
    """An array of objects."""
    return [as_object(v, f"{_where(path)}[{i}]") for i, v in enumerate(jarr(value, *path))]


def jstr(value: Json, *path: Step) -> str:
    return as_str(jat(value, *path), _where(path))


def jstrs(value: Json, *path: Step) -> list[str]:
    return [as_str(v, f"{_where(path)}[{i}]") for i, v in enumerate(jarr(value, *path))]


def jnum(value: Json, *path: Step) -> float:
    """A number (an int or a float, never a bool)."""
    v = jat(value, *path)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{_where(path)}: a number was expected, found {type(v).__name__}")


def jnums(value: Json, *path: Step) -> list[float]:
    return [jnum(v) for v in jarr(value, *path)]


def jint(value: Json, *path: Step) -> int:
    v = jat(value, *path)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    raise JsonShapeError(f"{_where(path)}: an integer was expected, found {type(v).__name__}")


def jbool(value: Json, *path: Step) -> bool:
    v = jat(value, *path)
    if isinstance(v, bool):
        return v
    raise JsonShapeError(f"{_where(path)}: a boolean was expected, found {type(v).__name__}")
