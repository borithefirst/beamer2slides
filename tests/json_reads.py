"""Reading a JSON value a test was handed (a `Result.data`, a transcript, a verdict) by its path.

`Result.data` is a `JsonObject`, so `data["found"]["slides"]` is two narrowings, not two indexings:
`at(data, "found", "slides")` walks a path of keys and indices and says where it stopped when the
value is not the shape the path assumes. The typed ends (`obj`, `arr`, `text`, ...) are the same
walk with the last value narrowed, which is what an assertion about it needs.
"""

from beamer2slides.json_types import (Json, JsonObject, as_array, as_int, as_object, as_objects,
                                      as_str)


def at(value: Json, *path: str | int) -> Json:
    """The value at `path` (keys into objects, indices into arrays); KeyError/IndexError as usual."""
    where = "value"
    for step in path:
        if isinstance(step, str):
            value = as_object(value, where)[step]
        else:
            value = as_array(value, where)[step]
        where = f"{where}[{step!r}]"
    return value


def obj(value: Json, *path: str | int) -> JsonObject:
    return as_object(at(value, *path), repr(path))


def arr(value: Json, *path: str | int) -> list[Json]:
    return as_array(at(value, *path), repr(path))


def objs(value: Json, *path: str | int) -> list[JsonObject]:
    return as_objects(at(value, *path), repr(path))


def text(value: Json, *path: str | int) -> str:
    return as_str(at(value, *path), repr(path))


def texts(value: Json, *path: str | int) -> list[str]:
    return [as_str(v, repr(path)) for v in arr(value, *path)]


def num(value: Json, *path: str | int) -> float:
    """A number: an int or a float (never a bool, which JSON keeps apart)."""
    got = at(value, *path)
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        raise TypeError(f"{path!r}: {got!r} is not a number")
    return got


def integer(value: Json, *path: str | int) -> int:
    return as_int(at(value, *path), repr(path))
