# SPDX-License-Identifier: AGPL-3.0-only
"""AST helpers for source checks (forbidden or required calls, imports, literals).

Checks run on the syntax tree, never on the raw text, so comments and
docstrings may mention a forbidden name. Not a test module itself (no
test_ prefix); test modules import it with `from source_checks import ...`.
"""
import ast
import inspect
import os
import textwrap


def source_tree(obj):
    """Parse the source of a module, class or function into an AST."""
    return ast.parse(textwrap.dedent(inspect.getsource(obj)))


def called_names(tree):
    """Names of all called objects: `f()` gives "f", `a.f()` gives "f" and "a.f"."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
            if isinstance(func.value, ast.Name):
                names.add("%s.%s" % (func.value.id, func.attr))
    return names


def imported_modules(tree):
    """Module names from `import x` and `from x import y` statements."""
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def string_constants(tree):
    """All string literals in source order of the walk (docstrings included)."""
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def production_packages(repo):
    """Sorted names of the directories in the repository root that hold an __init__.py."""
    return sorted(name for name in os.listdir(repo)
                  if os.path.isfile(os.path.join(repo, name, "__init__.py")))
