"""JSON before it is parsed: the type of what `json.loads` returns, and the narrowings that turn a
piece of it into a typed value where it is read (docs/typing.md, "Parse at the boundary").

A sync base, a deck.json or an API answer is a `JsonObject` until its parser exists. Code that reads
one narrows each value it takes with these, and a value of another shape is an error naming where it
was - never a `KeyError` or an `AttributeError` three calls later."""

from typing import Union

Json = Union[None, bool, int, float, str, list["Json"], dict[str, "Json"]]
JsonObject = dict[str, Json]
JsonArray = list[Json]


class JsonShapeError(ValueError):
    """A JSON value of another shape than its reader needs."""


def _found(value: Json) -> str:
    return "null" if value is None else type(value).__name__


def as_object(value: Json, where: str) -> JsonObject:
    if isinstance(value, dict):
        return value
    raise JsonShapeError(f"{where}: an object was expected, found {_found(value)}")


def as_array(value: Json, where: str) -> JsonArray:
    if isinstance(value, list):
        return value
    raise JsonShapeError(f"{where}: an array was expected, found {_found(value)}")


def as_objects(value: Json, where: str) -> list[JsonObject]:
    return [as_object(v, f"{where}[{i}]") for i, v in enumerate(as_array(value, where))]


def as_str(value: Json, where: str) -> str:
    if isinstance(value, str):
        return value
    raise JsonShapeError(f"{where}: a string was expected, found {_found(value)}")


def as_int(value: Json, where: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise JsonShapeError(f"{where}: an integer was expected, found {_found(value)}")


def as_optional_str(value: Json, where: str) -> str | None:
    return None if value is None else as_str(value, where)
