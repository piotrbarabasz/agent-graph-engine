from __future__ import annotations

import ast
from pathlib import Path


def test_codex_provider_dependency_direction_and_mutation_boundary() -> None:
    root = Path(__file__).parents[3] / "src" / "agentgraph" / "providers" / "codex"
    forbidden_imports = {"subprocess", "requests", "httpx"}
    forbidden_calls = {"stage_paths", "commit", "create_branch", "switch_branch", "add_worktree"}

    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        imported_modules = {
            (node.module or "").casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not imported_names.intersection(forbidden_imports)
        assert not calls.intersection(forbidden_calls)
        assert not any("speckit" in module or "github" in module for module in imported_modules)
