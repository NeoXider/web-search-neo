"""Enforce production module sizes and explicit leaf dependency boundaries."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys

try:
    from .architecture_policy import (
        EXCLUDED_DIRECTORIES, EXTERNAL_DEPENDENCIES, HARD_LIMIT, LEGACY_MAX_LINES, PACKAGE_DEPENDENCIES,
        SOFT_LIMIT,
    )
except ImportError:  # Direct invocation: python scripts/check_module_size.py
    from architecture_policy import (
        EXCLUDED_DIRECTORIES, EXTERNAL_DEPENDENCIES, HARD_LIMIT, LEGACY_MAX_LINES, PACKAGE_DEPENDENCIES,
        SOFT_LIMIT,
    )


def production_files(root: Path):
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in EXCLUDED_DIRECTORIES)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix in {".py", ".js"}:
                yield path


def check_sizes(root: Path, *, ratchets=None):
    """Return (errors, warnings); all physical lines, including comments, count."""
    ratchets = LEGACY_MAX_LINES if ratchets is None else ratchets
    errors, warnings = [], []
    for path in production_files(root):
        relative = path.relative_to(root).as_posix()
        count = len(path.read_text(encoding="utf-8-sig").splitlines())
        maximum = ratchets.get(relative, HARD_LIMIT)
        if count > maximum:
            errors.append(f"{relative}: {count} lines exceeds maximum {maximum}")
        elif count > SOFT_LIMIT:
            warnings.append(f"{relative}: {count} lines exceeds soft limit {SOFT_LIMIT}")
    return errors, warnings


def imported_modules(tree: ast.AST, module: str, *, is_package: bool):
    """Resolve ImportFrom, including relative imports, without importing code."""
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - node.level + 1
                if keep <= 0:
                    yield node.lineno, "<outside-package>"
                    continue
                base = ".".join(package[:keep] + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            # An import from an allowed module may name a function/class. Its
            # module is checked; package-level imports must check each child.
            if base == "web_search_neo":
                for alias in node.names:
                    yield node.lineno, f"{base}.{alias.name}"
            else:
                yield node.lineno, base


def boundary_errors(source: str, relative: str):
    path = Path(relative)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "web_search_neo":
        return []
    owner = parts[1]
    if owner not in PACKAGE_DEPENDENCIES:
        return []
    module = ".".join(parts).removesuffix(".py")
    is_package = path.name == "__init__.py"
    if is_package:
        module = module.removesuffix(".__init__")
    allowed = {f"web_search_neo.{owner}"}
    allowed.update(f"web_search_neo.{name}" for name in PACKAGE_DEPENDENCIES[owner])
    allowed.update(EXTERNAL_DEPENDENCIES.get(owner, ()))
    errors = []
    for line, imported in imported_modules(ast.parse(source), module, is_package=is_package):
        if imported.split(".", 1)[0] in sys.stdlib_module_names:
            continue
        if any(imported == item or imported.startswith(item + ".") for item in allowed):
            continue
        errors.append(f"{relative}:{line}: {owner} may not import {imported}")
    return errors


def check_boundaries(root: Path):
    errors = []
    for path in production_files(root):
        if path.suffix == ".py":
            errors.extend(boundary_errors(
                path.read_text(encoding="utf-8-sig"), path.relative_to(root).as_posix()
            ))
    return errors


def main():
    root = Path(__file__).resolve().parents[1]
    errors, warnings = check_sizes(root)
    errors.extend(check_boundaries(root))
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
