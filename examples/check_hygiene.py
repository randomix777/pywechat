"""Small static hygiene check: unused imports and unreferenced constants.

Run with:  python examples/check_hygiene.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

MODULES = [
    "product",
    "opencode",
    "gateway",
    "gateway_server",
    "webui",
    "client_login",
    "local_parse",
    "merchant_ui",
    "merchant_app",
    "admin",
]


def collect_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def collect_used(tree: ast.AST) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def main() -> int:
    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "wechat_ai_assistant"
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py")))
    corpus += "\n" + "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((package.parents[1] / "tests").glob("*.py"))
    )
    problems = 0
    for name in MODULES:
        path = package / f"{name}.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = collect_imports(tree)
        used = collect_used(tree) | {
            node.value.id for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        exported = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        exported = {
                            element.value
                            for element in getattr(node.value, "elts", [])
                            if isinstance(element, ast.Constant)
                        }
        # `annotations` is the __future__ import, never a real binding.
        unused = sorted(
            i
            for i in imports - used - exported - {"annotations"}
            if f'"{i}"' not in source and f"'{i}'" not in source
        )
        constants = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.isupper()
        ]
        # A constant is dead only if nothing in the package or tests mentions it.
        dead = [c for c in constants if corpus.count(c) == 1 and c not in exported]
        if unused or dead:
            problems += len(unused) + len(dead)
            print(f"{name}: unused_imports={unused} unreferenced_constants={dead}")
    print("clean" if not problems else f"{problems} issue(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
