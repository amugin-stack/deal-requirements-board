"""The keyword and regex baseline, run from its frozen source at tag `baseline-v1`.

Why a loader instead of a plain module: the frozen file says `from schema import ...` because it
was written when `schema.py` was a top-level module, and the owner's rule is that it is never
edited, imports and formatting included. So the file lives byte-identical under `frozen/`
(no `__init__.py`, so nothing can import it by accident) and is executed here with exactly one
name remapped, `schema` -> `deal_board.schema`. The remap lives in that file's own builtins,
so no top-level `schema` module ever appears in `sys.modules` for the rest of the process.

`RegexExtractor` below is a thin typed wrapper: every call goes straight to the frozen class,
and its output is checked to really be a `DealRequirement` instead of being trusted blindly.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol

from deal_board import schema
from deal_board.schema import DealRequirement

FROZEN_PATH: Final = Path(__file__).resolve().parent / "frozen" / "regex_baseline.py"
_ALIASES: Final[Mapping[str, ModuleType]] = {"schema": schema}


class _FrozenExtractor(Protocol):
    def extract(self, text: str) -> object: ...


def _scoped_import(
    name: str,
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: Sequence[str] | None = (),
    level: int = 0,
) -> ModuleType:
    """`__import__` for the frozen file only: absolute `schema` resolves to `deal_board.schema`."""
    if level == 0 and name in _ALIASES:
        return _ALIASES[name]
    return builtins.__import__(name, globals, locals, fromlist or (), level)


def _load_frozen(path: Path = FROZEN_PATH) -> ModuleType:
    """Execute the frozen source in a fresh module whose `__import__` is `_scoped_import`.

    The module takes this module's name, so the frozen class reports the import path people
    actually use; compiling with the real filename keeps tracebacks pointing at the frozen file.
    """
    module = ModuleType(__name__, __doc__)
    module.__file__ = str(path)
    namespace = vars(module)
    namespace["__builtins__"] = {**vars(builtins), "__import__": _scoped_import}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return module


def _frozen_class(module: ModuleType) -> Callable[[], _FrozenExtractor]:
    cls = vars(module).get("RegexExtractor")
    if not isinstance(cls, type):
        raise ImportError(f"{FROZEN_PATH} no longer defines a RegexExtractor class")
    return cls


_FROZEN_CLASS: Final = _frozen_class(_load_frozen())


class RegexExtractor:
    """The frozen baseline behind the `Extractor` interface; behavior is the frozen file's."""

    name = "regex"

    def __init__(self) -> None:
        self._impl = _FROZEN_CLASS()

    def __reduce__(self) -> tuple[type[RegexExtractor], tuple[()]]:
        """Pickle as "build a fresh shim", never as the wrapped frozen instance.

        Root cause of the old failure: `_load_frozen` runs the frozen class under this
        module's name, so the frozen class says it is `deal_board.extractors.regex_baseline.
        RegexExtractor`. Pickle stores classes by that import path, and looking it up finds
        this shim class instead, so pickling `self._impl` raised "not the same object". The
        frozen class keeps no state (all its tables are module-level), so a fresh `_impl`
        after unpickling extracts identically, and the frozen file stays untouched.
        """
        return (RegexExtractor, ())

    def extract(self, text: str) -> DealRequirement:
        result = self._impl.extract(text)
        if not isinstance(result, DealRequirement):
            raise TypeError(f"frozen RegexExtractor returned {type(result).__name__}, not DealRequirement")
        return result
