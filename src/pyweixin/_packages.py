"""Locate the sibling ``packages/*`` sources in a source checkout.

The reusable agent packages are independently installable
(``pip install -e packages/business_agent``). In this repository they also sit
next to the product as plain source, and a developer running from a checkout
should not have to install anything to run the tests.

This module adds those source roots to ``sys.path`` -- and only those. It is
imported by :mod:`pyweixin.integration`, never by the generic core, which must
stay importable in an environment that has never heard of this repository.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: Package directory name -> import name.
_PACKAGES = ("business_agent", "order_agent")

_loaded = False


def ensure_packages_on_path() -> tuple[str, ...]:
    """Add ``packages/<name>/src`` to ``sys.path`` if present. Idempotent."""
    global _loaded
    if _loaded:
        return _PACKAGES
    # <repo>/src/pyweixin/_packages.py -> parents[0]=pyweixin, [1]=src, [2]=<repo>
    root = Path(__file__).resolve().parents[2]
    packages_dir = root / "packages"
    for name in _PACKAGES:
        candidate = packages_dir / name / "src"
        if candidate.is_dir():
            entry = str(candidate)
            if entry not in sys.path:
                sys.path.append(entry)
    _loaded = True
    return _PACKAGES
