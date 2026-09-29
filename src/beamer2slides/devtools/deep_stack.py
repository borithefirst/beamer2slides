"""The optional deep-learning stack `slide_metrics`' torch metrics run on - torch, lpips and
transformers - as Protocols of exactly what it calls, and one loader per library.

The libraries live in their own venv (`out/metrics-venv`, docs/adopt-bench.md "Metrics"), never
among the package's dependencies, so the checker has no types for them and must not need any.
Each loader imports its library when a metric first asks for it (`importlib.import_module`, so no
import statement names a module the checker cannot find) and hands it back as its Protocol after
`isinstance` has checked, at runtime, that every name slide_metrics calls is there. Only the
top-level names are checked; what they answer is typed here and taken at its word, as a client
`gapi.build` checked is. A library missing raises `ModuleNotFoundError` as the import statement
did; one lacking a name raises `ImportError` naming it.

A name added to a Protocol is a name the code may now call: add it where the code first does.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, Union, runtime_checkable

import numpy as np
import numpy.typing as npt
from PIL import Image

from beamer2slides.arrays import Floats32

Index = Union[int, slice, None, tuple[Union[int, slice, None], ...]]
"""What a tensor is indexed with here: an int, a slice, None (a new axis), or a tuple of them."""


# ------------------------------------------------------------------------------------------ torch

class DType(Protocol):
    """A tensor's element type (`torch.float32`), passed through, never looked into."""

    @property
    def is_floating_point(self) -> bool: ...


class Tensor(Protocol):
    """The part of `torch.Tensor` the metrics use: indexing, arithmetic with tensors and numbers,
    the reductions to one value, and the way back to numpy."""

    @property
    def shape(self) -> tuple[int, ...]: ...
    def __getitem__(self, index: Index, /) -> Tensor: ...
    def __add__(self, other: Tensor | float, /) -> Tensor: ...
    def __radd__(self, other: float, /) -> Tensor: ...
    def __sub__(self, other: Tensor | float, /) -> Tensor: ...
    def __rsub__(self, other: float, /) -> Tensor: ...
    def __mul__(self, other: Tensor | float, /) -> Tensor: ...
    def __rmul__(self, other: float, /) -> Tensor: ...
    def __truediv__(self, other: Tensor | float, /) -> Tensor: ...
    def __pow__(self, exponent: float, /) -> Tensor: ...
    def __neg__(self) -> Tensor: ...
    def __gt__(self, other: float, /) -> Tensor: ...
    def __float__(self) -> float: ...
    def sum(self) -> Tensor: ...
    def mean(self) -> Tensor: ...
    def max(self) -> Tensor: ...
    def clamp_min(self, low: float, /) -> Tensor: ...
    def permute(self, *dims: int) -> Tensor: ...
    def reshape(self, *shape: int) -> Tensor: ...
    def to(self, device: str, /) -> Tensor: ...
    def cpu(self) -> Tensor: ...
    def numpy(self) -> Floats32: ...


class NoGrad(Protocol):
    """`torch.no_grad()`: a context that swallows no exception."""

    def __enter__(self) -> None: ...
    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None, /) -> None: ...


class Cuda(Protocol):
    def is_available(self) -> bool: ...


class Functional(Protocol):
    """`torch.nn.functional`, as far as the metrics call it."""

    def avg_pool2d(self, x: Tensor, kernel: int, /, *, ceil_mode: bool) -> Tensor: ...
    def conv2d(self, x: Tensor, weight: Tensor, /, *, padding: int) -> Tensor: ...
    def interpolate(self, x: Tensor, /, *, size: tuple[int, int], mode: str, antialias: bool,
                    align_corners: bool) -> Tensor: ...
    def cosine_similarity(self, a: Tensor, b: Tensor, /, *, dim: int) -> Tensor: ...


class Nn(Protocol):
    @property
    def functional(self) -> Functional: ...


@runtime_checkable
class Torch(Protocol):
    """The `torch` module, as far as the metrics call it."""

    @property
    def float32(self) -> DType: ...
    @property
    def float64(self) -> DType: ...
    @property
    def cuda(self) -> Cuda: ...
    @property
    def nn(self) -> Nn: ...
    def no_grad(self) -> NoGrad: ...
    def as_tensor(self, data: npt.NDArray[np.generic], /, *, dtype: DType, device: str) -> Tensor: ...
    def tensor(self, data: list[float], /, *, device: str) -> Tensor: ...
    def arange(self, end: int, /, *, device: str, dtype: DType) -> Tensor: ...
    def zeros_like(self, like: Tensor, /) -> Tensor: ...
    def full_like(self, like: Tensor, value: float, /) -> Tensor: ...
    def where(self, condition: Tensor, a: Tensor, b: Tensor, /) -> Tensor: ...
    def log(self, x: Tensor, /) -> Tensor: ...
    def exp(self, x: Tensor, /) -> Tensor: ...
    def logsumexp(self, x: Tensor, /, *, dim: int) -> Tensor: ...


# ------------------------------------------------------------------------------------------ lpips

class LpipsNet(Protocol):
    """An `lpips.LPIPS` network: (two images in [-1, 1]) -> its distance map, when made spatial."""

    def to(self, device: str, /) -> LpipsNet: ...
    def eval(self) -> LpipsNet: ...
    def __call__(self, in0: Tensor, in1: Tensor, /) -> Tensor: ...


@runtime_checkable
class Lpips(Protocol):
    """The `lpips` module."""

    def LPIPS(self, *, net: str, spatial: bool, verbose: bool) -> LpipsNet: ...


# ------------------------------------------------------------------------------------------ transformers

class HiddenStates(Protocol):
    @property
    def last_hidden_state(self) -> Tensor: ...


class DinoNet(Protocol):
    """A DINOv2 model as `AutoModel` loads it."""

    def to(self, device: str, /) -> DinoNet: ...
    def eval(self) -> DinoNet: ...
    def __call__(self, *, pixel_values: Tensor) -> HiddenStates: ...


class DinoLoader(Protocol):
    def from_pretrained(self, name: str, /) -> DinoNet: ...


@runtime_checkable
class AutoModels(Protocol):
    """The `transformers` module, for its `AutoModel`."""

    @property
    def AutoModel(self) -> DinoLoader: ...


@runtime_checkable
class Pooled(Protocol):
    """What transformers 5's `get_image_features` answers with in place of the tensor 4 gave."""

    @property
    def pooler_output(self) -> Tensor: ...


class ClipNet(Protocol):
    def to(self, device: str, /) -> ClipNet: ...
    def eval(self) -> ClipNet: ...
    def get_image_features(self, *, pixel_values: Tensor) -> Tensor | Pooled: ...


class ClipLoader(Protocol):
    def from_pretrained(self, name: str, /) -> ClipNet: ...


class Features(Protocol):
    """A processor's batch, read by name (`["pixel_values"]`)."""

    def __getitem__(self, name: str, /) -> Tensor: ...


class ClipProcessor(Protocol):
    def __call__(self, *, images: list[Image.Image], return_tensors: str) -> Features: ...


class ProcessorLoader(Protocol):
    def from_pretrained(self, name: str, /) -> ClipProcessor: ...


@runtime_checkable
class ClipModels(Protocol):
    """The `transformers` module, for CLIP's model and processor."""

    @property
    def CLIPModel(self) -> ClipLoader: ...
    @property
    def CLIPProcessor(self) -> ProcessorLoader: ...


# ------------------------------------------------------------------------------------------ loaders

def _imported(name: str, names: Sequence[str]) -> object:
    """Module `name`, each of `names` asked of it first. transformers is a lazy module that makes a
    name only when asked, and a Protocol check looks without asking (Python 3.12 reads attributes
    with `inspect.getattr_static`), so the asking comes first. An `object`, not a `ModuleType`:
    typeshed gives a module an `__getattr__` answering anything, so a module would pass for every
    Protocol, and only the runtime check may say what it is."""
    module = importlib.import_module(name)
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        raise ImportError(f"{name} has no {', '.join(missing)}")
    return module


def load_torch() -> Torch:
    module = _imported("torch", ())
    if isinstance(module, Torch):
        return module
    raise ImportError("torch lacks a name deep_stack.Torch says the metrics call")


def load_lpips() -> Lpips:
    module = _imported("lpips", ())
    if isinstance(module, Lpips):
        return module
    raise ImportError("lpips lacks a name deep_stack.Lpips says the metrics call")


def load_auto_models() -> AutoModels:
    module = _imported("transformers", ("AutoModel",))
    if isinstance(module, AutoModels):
        return module
    raise ImportError("transformers lacks a name deep_stack.AutoModels says the metrics call")


def load_clip_models() -> ClipModels:
    module = _imported("transformers", ("CLIPModel", "CLIPProcessor"))
    if isinstance(module, ClipModels):
        return module
    raise ImportError("transformers lacks a name deep_stack.ClipModels says the metrics call")
