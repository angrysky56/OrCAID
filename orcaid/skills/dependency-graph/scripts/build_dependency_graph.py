#!/usr/bin/env python3
"""
build_dependency_graph.py — build import-based dependency graph for Python files.

Reads pass files from stdin (JSON array) and builds an adjacency dict:
{file: [files_it_imports_from]}.  Deterministic — no LLM needed.

Extracts the "build dependency graph" deterministic work from the LLM in
scan_and_analyze(), shrinking that prompt by ~15K tokens.

Usage:
    cat pass_files.json | python build_dependency_graph.py <repo_root>
    python build_dependency_graph.py <repo_root> < pass_files.json
"""

import ast
import json
import sys
from pathlib import Path


def collect_imports(source: str) -> set[str]:
    """Return the *full* dotted module paths imported by source text.

    Returns the full module path (e.g. ``"src.helper"``), not just the top
    component, so multi-level package imports like ``from src.helper import x``
    can be matched against repo-relative file paths.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("_"):
                    names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.module.startswith("_"):
                names.add(node.module)
                # Also expose each submodule named in the `import X, Y` list,
                # since `from pkg import sub` may target `pkg/sub.py`.
                for alias in node.names:
                    if alias.name != "*" and not alias.name.startswith("_"):
                        names.add(f"{node.module}.{alias.name}")
    return names


def _module_to_relpath_candidates(module: str) -> list[str]:
    """Map a dotted module path to candidate repo-relative file paths.

    ``"src.helper"`` → ``["src/helper.py", "src/helper/__init__.py"]``
    ``"helper"``     → ``["helper.py", "helper/__init__.py"]``
    """
    parts = module.split(".")
    base = "/".join(parts)
    return [f"{base}.py", f"{base}/__init__.py"]


def build_graph(repo_root: Path, pass_files: list[dict]) -> dict[str, list[str]]:
    """
    For each pass file, read its imports and match them to other pass files.
    Returns adjacency dict: {pass_file_relpath: [dep_relpath, ...]}.

    Matching is path-based — a dotted module ``src.helper`` is mapped to
    candidate paths ``src/helper.py`` and ``src/helper/__init__.py`` and an
    edge is added whenever any candidate matches a known pass file. Self-edges
    are excluded.
    """
    pass_paths = {f["file"] for f in pass_files}
    graph: dict[str, list[str]] = {}

    for pf in pass_files:
        pfile = Path(pf["file"])
        full_path = repo_root / pfile
        if not full_path.is_file():
            continue

        try:
            source = full_path.read_text(encoding="utf-8")
        except OSError:
            continue

        deps: set[str] = set()
        for module in collect_imports(source):
            for candidate in _module_to_relpath_candidates(module):
                if candidate in pass_paths and candidate != pf["file"]:
                    deps.add(candidate)

        graph[pf["file"]] = sorted(deps)

    return graph


def main() -> None:
    # Read pass_files JSON from stdin if piped
    if not sys.stdin.isatty():
        pass_data = json.load(sys.stdin)
    elif len(sys.argv) >= 3:
        # Backward compat: pass_files JSON as second arg
        pass_data = json.loads(sys.argv[2])
    else:
        print(f"Usage: {sys.argv[0]} <repo_root> [pass_files_json]", file=sys.stderr)
        print(f"   or: cat pass_files.json | {sys.argv[0]} <repo_root>", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(sys.argv[1]).resolve()
    pass_files = pass_data if isinstance(pass_data, list) else pass_data.get("files", [])

    graph = build_graph(repo_root, pass_files)
    output = {"repo": str(repo_root), "graph": graph, "files_analyzed": len(pass_files)}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()