"""Fixture-repo tests for the Step-A deterministic skill scripts.

These scripts replace the LLM's repo-exploration work in ``scan_and_analyze``.
They must produce the exact JSON shapes that ``_run_pre_scan_scripts`` reads,
and they must do so against a tmpdir fixture repo with no network or LLM
dependencies. If these tests pass, Repair 1 (skip the LLM analysis when
pre-scan succeeds) can rely on the script output as its source of truth.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


SKILLS_ROOT = Path(__file__).resolve().parent.parent / "orcaid" / "skills"
FIND_STUBS = SKILLS_ROOT / "repo-scan" / "scripts" / "find_pass_stubs.py"
BUILD_GRAPH = SKILLS_ROOT / "dependency-graph" / "scripts" / "build_dependency_graph.py"


# --------------------------------------------------------------------------- #
# Fixture                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Tiny synthetic repo with two pass-stub files and one import edge.

    Layout:
        repo/
        ├── src/
        │   ├── widget.py        # imports helper; one pass stub
        │   └── helper.py        # no imports; two pass stubs
        ├── tests/
        │   └── test_widget.py   # real implementation (no pass), should be skipped
        ├── .venv/
        │   └── ignore.py        # pass stub, but inside .venv → must be ignored
        └── build/
            └── junk.py          # pass stub, inside build/ → must be ignored
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "build").mkdir()

    (tmp_path / "src" / "widget.py").write_text(
        textwrap.dedent(
            """
            from src.helper import help_a

            def make_widget():
                pass

            def already_done():
                return 42
            """
        ).lstrip()
    )

    (tmp_path / "src" / "helper.py").write_text(
        textwrap.dedent(
            """
            def help_a():
                pass

            def help_b():
                pass
            """
        ).lstrip()
    )

    (tmp_path / "tests" / "test_widget.py").write_text(
        textwrap.dedent(
            """
            from src.widget import make_widget

            def test_make_widget():
                assert make_widget() is None
            """
        ).lstrip()
    )

    (tmp_path / ".venv" / "ignore.py").write_text("def x():\n    pass\n")
    (tmp_path / "build" / "junk.py").write_text("def y():\n    pass\n")

    return tmp_path


def _run_script(script: Path, *args: str, stdin: bytes | None = None) -> dict:
    """Run a CLI script, parse stdout as JSON, fail loudly on error."""
    result = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        timeout=15,
        input=stdin,
        check=False,
    )
    assert result.returncode == 0, (
        f"Script failed: {script.name}\nstderr={result.stderr.decode()}"
    )
    return json.loads(result.stdout.decode())


# --------------------------------------------------------------------------- #
# find_pass_stubs.py                                                          #
# --------------------------------------------------------------------------- #


def test_find_pass_stubs_returns_expected_shape(fixture_repo: Path):
    out = _run_script(FIND_STUBS, str(fixture_repo))
    assert out["repo"] == str(fixture_repo.resolve())
    assert isinstance(out["files"], list)


def test_find_pass_stubs_finds_only_real_stubs(fixture_repo: Path):
    out = _run_script(FIND_STUBS, str(fixture_repo))
    by_file = {entry["file"]: set(entry["functions"]) for entry in out["files"]}

    # widget.py: only make_widget (already_done has a real body)
    assert by_file["src/widget.py"] == {"make_widget"}
    # helper.py: both functions are stubs
    assert by_file["src/helper.py"] == {"help_a", "help_b"}


def test_find_pass_stubs_skips_venv_and_build(fixture_repo: Path):
    out = _run_script(FIND_STUBS, str(fixture_repo))
    seen_paths = {entry["file"] for entry in out["files"]}
    assert not any(".venv" in p for p in seen_paths)
    assert not any("build/" in p for p in seen_paths)


def test_find_pass_stubs_handles_empty_repo(tmp_path: Path):
    out = _run_script(FIND_STUBS, str(tmp_path))
    assert out["files"] == []


# --------------------------------------------------------------------------- #
# build_dependency_graph.py                                                   #
# --------------------------------------------------------------------------- #


def test_build_dep_graph_resolves_pass_file_imports(fixture_repo: Path):
    pass_data = _run_script(FIND_STUBS, str(fixture_repo))
    pass_files_json = json.dumps({"files": pass_data["files"]}).encode()

    graph_data = _run_script(BUILD_GRAPH, str(fixture_repo), stdin=pass_files_json)
    graph = graph_data["graph"]

    # widget.py imports helper.py, helper.py imports nothing among pass files
    assert "src/widget.py" in graph
    assert "src/helper.py" in graph["src/widget.py"]
    assert graph["src/helper.py"] == []


def test_build_dep_graph_handles_empty_input(fixture_repo: Path):
    graph_data = _run_script(
        BUILD_GRAPH, str(fixture_repo), stdin=json.dumps({"files": []}).encode()
    )
    assert graph_data["graph"] == {}
    assert graph_data["files_analyzed"] == 0


def test_build_dep_graph_skips_unreadable_files(tmp_path: Path):
    """A pass_files entry pointing at a non-existent path should be silently
    skipped, not crash the graph builder. The pre-scan pipeline should never
    halt on a stale entry."""
    pass_files = [{"file": "nope/missing.py", "functions": ["x"]}]
    stdin = json.dumps({"files": pass_files}).encode()
    graph_data = _run_script(BUILD_GRAPH, str(tmp_path), stdin=stdin)
    assert graph_data["graph"] == {}


# --------------------------------------------------------------------------- #
# Workspace runner round-trip — confirms a real ExecResult shape produces      #
# usable JSON when fed through the manager's parsing path.                    #
# --------------------------------------------------------------------------- #


def test_workspace_runner_shell_command_shape(fixture_repo: Path):
    """Smoke test the shell command shape produced by
    ``_run_script_in_workspace``: the base64-encoded script + base64-encoded
    stdin payload should decode and execute correctly under plain ``bash -c``.

    We can't spin up a Docker workspace inside this test, but we can run the
    same shell command locally and confirm the encoding survives quoting and
    multi-line script content intact. If this passes, the only thing left
    that could break inside the real workspace is the workspace handler
    itself (which is openhands SDK code we're not exercising here).
    """
    import base64
    import shlex
    import uuid

    script_b64 = base64.b64encode(FIND_STUBS.read_bytes()).decode("ascii")
    tmp = f"/tmp/orcaid_skill_{uuid.uuid4().hex[:10]}.py"
    repo_quoted = shlex.quote(str(fixture_repo))

    cmd = (
        f"echo {script_b64} | base64 -d > {tmp} && "
        f"python3 {tmp} {repo_quoted}; "
        f"rc=$?; rm -f {tmp}; exit $rc"
    )

    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        f"workspace-shape shell command failed: {result.stderr.decode()}"
    )
    out = json.loads(result.stdout.decode())
    assert set(entry["file"] for entry in out["files"]) == {
        "src/widget.py",
        "src/helper.py",
    }


def test_workspace_runner_with_stdin_payload(fixture_repo: Path):
    """Same round-trip but for the dep-graph script, which reads JSON on
    stdin. Both base64 payloads (script and stdin) must survive shell
    quoting without corruption."""
    import base64
    import shlex
    import uuid

    pass_files = [
        {"file": "src/widget.py", "functions": ["make_widget"]},
        {"file": "src/helper.py", "functions": ["help_a", "help_b"]},
    ]

    script_b64 = base64.b64encode(BUILD_GRAPH.read_bytes()).decode("ascii")
    stdin_b64 = base64.b64encode(
        json.dumps({"files": pass_files}).encode("utf-8")
    ).decode("ascii")
    tmp = f"/tmp/orcaid_skill_{uuid.uuid4().hex[:10]}.py"
    repo_quoted = shlex.quote(str(fixture_repo))

    cmd = (
        f"echo {script_b64} | base64 -d > {tmp} && "
        f"echo {stdin_b64} | base64 -d | python3 {tmp} {repo_quoted}; "
        f"rc=$?; rm -f {tmp}; exit $rc"
    )

    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdin-payload shell command failed: {result.stderr.decode()}"
    )
    out = json.loads(result.stdout.decode())
    assert out["graph"]["src/widget.py"] == ["src/helper.py"]
    assert out["graph"]["src/helper.py"] == []


def test_load_prompts_resolves_correctly():
    """Verify that load_prompts correctly resolves and loads prompt files."""
    from orcaid.core.utils import load_prompts

    # Test loading "commit0"
    commit0_prompts = load_prompts("commit0")
    assert isinstance(commit0_prompts, dict)
    assert "subagent_prompt" in commit0_prompts
    assert "manager_final_review_all" in commit0_prompts

    # Test loading "paperbench"
    paperbench_prompts = load_prompts("paperbench")
    assert isinstance(paperbench_prompts, dict)

    # Test non-existent task raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        load_prompts("non_existent_task_12345")
