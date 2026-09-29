"""Typing names newer than Python 3.10, for code that runs on 3.10 (docs/typing.md).

The checker reads them from typing_extensions (installed wherever the checker runs: the [dev] extra
and the build's own requirements); at runtime they are these few lines, so the package needs no
dependency for them.

`assert_never(x)` ends a match or an if-chain over a closed set of cases (a Literal, a union of
dataclasses): the checker proves it unreachable, so a case added to the set and not handled is a
type error where it is missed, and at runtime it raises. `override` marks a method that replaces
its base class's, so a renamed or removed base method is an error in its subclasses."""

from typing import TYPE_CHECKING, NoReturn, TypeVar

if TYPE_CHECKING:
    from typing_extensions import assert_never, override
else:
    _F = TypeVar("_F")

    def assert_never(value: NoReturn) -> NoReturn:
        raise AssertionError(f"unhandled case: {value!r}")

    def override(method: _F) -> _F:
        return method

__all__ = ["assert_never", "override"]
