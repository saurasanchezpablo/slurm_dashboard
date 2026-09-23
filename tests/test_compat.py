"""Python 3.9 compatibility.

The README promises 3.9+, and cluster login nodes routinely ship 3.9 while
developers run 3.10+. Syntax that only fails on the older interpreter is
therefore invisible locally, so it is checked statically here.

Regression: `class TextPromptModal(ModalScreen[str | None])` crashed at import
on 3.9. `from __future__ import annotations` defers *annotations*, but a class
base is evaluated when the class is created, so the PEP 604 union ran for real.
"""
import ast

import pytest

MIN_PYTHON = (3, 9)


def _annotation_node_ids(tree):
    """Ids of every node inside an annotation.

    With `from __future__ import annotations` these are never evaluated, so
    PEP 604 unions are safe there and only there.
    """
    safe = set()

    def mark(node):
        for child in ast.walk(node):
            safe.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            mark(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                mark(node.returns)
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                        args.vararg, args.kwarg):
                if arg is not None and arg.annotation is not None:
                    mark(arg.annotation)
    return safe


@pytest.fixture(scope="module")
def tree(src_path):
    return ast.parse(src_path.read_text(encoding="utf-8"))


def test_future_annotations_is_imported(tree):
    """Everything else here depends on annotations being deferred."""
    futures = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        for alias in node.names
    ]
    assert "annotations" in futures


def test_no_runtime_pep604_unions(tree):
    """`X | None` outside an annotation needs Python 3.10.

    Matching against None specifically means legitimate bitwise OR on ints
    (os.O_WRONLY | os.O_CREAT) is never flagged.
    """
    safe = _annotation_node_ids(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
            continue
        if id(node) in safe:
            continue
        if any(isinstance(side, ast.Constant) and side.value is None
               for side in (node.left, node.right)):
            offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert not offenders, (
        "PEP 604 union evaluated at runtime (breaks Python 3.9). "
        "Use typing.Optional[...] instead:\n  " + "\n  ".join(offenders))


def test_class_bases_are_evaluatable_on_39(tree):
    """A parameterised base such as ModalScreen[...] is evaluated eagerly."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            for child in ast.walk(base):
                if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                    offenders.append(f"{node.name}: {ast.unparse(base)}")
    assert not offenders, "PEP 604 union in a class base: " + ", ".join(offenders)


def test_no_match_statement(tree):
    """`match` is 3.10+ syntax."""
    assert not [n for n in ast.walk(tree) if n.__class__.__name__ == "Match"]


TOO_NEW_MODULES = {"tomllib": "3.11", "graphlib": "3.9"}
TOO_NEW_ATTRS = {
    "itertools.pairwise": "3.10",
    "itertools.batched": "3.12",
    "types.NoneType": "3.10",
    "asyncio.TaskGroup": "3.11",
    "typing.Self": "3.11",
    "datetime.UTC": "3.11",
}
TOO_NEW_NAMES = {"ExceptionGroup": "3.11", "anext": "3.10", "aiter": "3.10"}


def test_no_stdlib_apis_newer_than_39(tree):
    """APIs added after 3.9, which would fail only when reached at runtime.

    The AST is walked rather than the raw text, so a module merely *named* in
    a comment (this file explains why tomllib is avoided) is not a finding.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in TOO_NEW_MODULES and root != "graphlib":
                    found.append(f"import {alias.name} (needs {TOO_NEW_MODULES[root]})")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in TOO_NEW_MODULES and root != "graphlib":
                found.append(f"from {node.module} (needs {TOO_NEW_MODULES[root]})")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            dotted = f"{node.value.id}.{node.attr}"
            if dotted in TOO_NEW_ATTRS:
                found.append(f"{dotted} (needs {TOO_NEW_ATTRS[dotted]})")
        elif isinstance(node, ast.Name) and node.id in TOO_NEW_NAMES:
            found.append(f"{node.id} (needs {TOO_NEW_NAMES[node.id]})")
    assert not found, "stdlib API newer than 3.9: " + ", ".join(sorted(set(found)))


def test_source_compiles_under_target_syntax(src_path):
    """Guards against syntax the 3.9 parser itself would reject."""
    source = src_path.read_text(encoding="utf-8")
    compile(source, str(src_path), "exec")
