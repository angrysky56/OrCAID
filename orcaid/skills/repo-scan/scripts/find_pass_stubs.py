#!/usr/bin/env python3
"""
find_pass_stubs.py — find all functions with `pass` bodies in a Python repo.

Extracts the deterministic "find files with pass statements" work from the
LLM in scan_and_analyze(), shrinking that prompt by ~15K tokens.

Output JSON schema:
{
  "repo": "...",           # absolute path scanned
  "files": [
    {
      "file": "src/foo.py",  # relative path
      "functions": ["func_a", "func_b"]
    }
  ]
}
"""

import ast
import json
import sys
from pathlib import Path


def find_pass_stubs(repo_root: Path) -> list[dict]:
    """Walk repo, return files with pass-stub functions."""
    results = []
    for path in sorted(repo_root.rglob("*.py")):
        if "/.venv/" in str(path) or "/__pycache__/" in str(path) or "/build/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, ValueError, OSError):
            continue

        stubs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check if body is just `pass`
                if (
                    len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)
                ):
                    stubs.append(node.name)

        if stubs:
            results.append({
                "file": str(path.relative_to(repo_root)),
                "functions": stubs,
            })
    return results


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <repo_root>", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(sys.argv[1]).resolve()
    if not repo_root.is_dir():
        print(f"Error: {repo_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    files = find_pass_stubs(repo_root)
    output = {"repo": str(repo_root), "files": files}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()