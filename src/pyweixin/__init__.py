"""Backward-compatible import namespace for older ``pyweixin`` callers.

New code should import :mod:`wechat_ai_assistant`. This shim forwards old
package and submodule imports to the canonical package, preserving module
identity so existing monkeypatches and integrations keep working.
"""
from __future__ import annotations

from importlib import import_module, util
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
import sys
from types import ModuleType
from typing import Any

import wechat_ai_assistant as _canonical

__all__ = _canonical.__all__
__path__ = list(_canonical.__path__)


class _AliasLoader(Loader):
    def __init__(self, target_name: str):
        self.target_name = target_name
        self.original_spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = import_module(self.target_name)
        self.original_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # The import machinery initializes the returned module with the legacy
        # spec. Restore its canonical metadata after installing the old alias.
        spec = self.original_spec
        if spec is not None:
            module.__name__ = spec.name
            module.__spec__ = spec
            module.__loader__ = spec.loader
            module.__package__ = spec.parent
            if spec.has_location:
                module.__file__ = spec.origin
                module.__cached__ = spec.cached
            if spec.submodule_search_locations is not None:
                module.__path__ = list(spec.submodule_search_locations)


class _AliasFinder(MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None,
                  target: ModuleType | None = None) -> ModuleSpec | None:
        prefix = __name__ + "."
        if not fullname.startswith(prefix):
            return None
        canonical_name = "wechat_ai_assistant." + fullname[len(prefix):]
        try:
            canonical_spec = util.find_spec(canonical_name)
        except (ImportError, ModuleNotFoundError, ValueError):
            return None
        if canonical_spec is None:
            return None
        is_package = canonical_spec.submodule_search_locations is not None
        loader = _AliasLoader(canonical_name)
        return util.spec_from_loader(fullname, loader, is_package=is_package)


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name: str) -> Any:
    if name in __all__:
        value = getattr(_canonical, name)
        globals()[name] = value
        return value
    try:
        module = import_module(f"wechat_ai_assistant.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"wechat_ai_assistant.{name}":
            raise
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    sys.modules[f"{__name__}.{name}"] = module
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
