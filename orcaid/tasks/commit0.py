"""
python -m tasks.commit0
"""

import json
from dataclasses import dataclass

from .base import TaskModule

# Maps detected language to a suitable Docker base image for custom repos.
# Official wentingzhao/ benchmark repos keep their own pre-built images.
_LANGUAGE_IMAGES: dict[str, str] = {
    "python": "python:3.12-slim",
    "typescript": "node:22-slim",
    "javascript": "node:22-slim",
    "go": "golang:1.22-bookworm",
    "rust": "rust:1.76-slim",
    "java": "eclipse-temurin:21-jdk-slim",
}

# Sentinel files used to identify the primary language of a repository.
# Listed in priority order — first match wins.
_LANGUAGE_SENTINELS: list[tuple[str, str]] = [
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("package.json", "typescript"),  # refined to "javascript" if no .ts files found
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
]

# Per-language shell snippets that surface stub / incomplete functions.
# Each snippet outputs one "path:lineno: func_signature" per match.
_STUB_PATTERNS: dict[str, str] = {
    "python": (
        r"grep -rn '^\s*pass\s*$' --include='*.py' "
        r"| grep -v '__pycache__' | grep -v '.venv' | grep -v 'site-packages'"
    ),
    "typescript": (
        "grep -rEin 'throw new Error.*not.implemented|TODO|FIXME' "
        "--include='*.ts' --include='*.tsx' | grep -v 'node_modules' | grep -v '.d.ts'"
    ),
    "javascript": (
        "grep -rEin 'throw new Error.*not.implemented|TODO|FIXME' "
        "--include='*.js' --include='*.mjs' | grep -v 'node_modules'"
    ),
    "go": (
        r"grep -rEn 'panic\(\"not implemented\"\)|// TODO|// FIXME' "
        r"--include='*.go' | grep -v '_test.go'"
    ),
    "rust": (
        r"grep -rEn 'todo!\(\)|unimplemented!\(\)|panic!' "
        r"--include='*.rs' | grep -v '/tests/'"
    ),
    "java": (
        r"grep -rEn 'throw new UnsupportedOperationException|// TODO|// FIXME' "
        r"--include='*.java' | grep -v '/test/'"
    ),
}


def _parse_test_output_heuristic(output: str, lang: str) -> tuple[int, int, int]:
    """Best-effort count of passed/failed/error from raw test output.

    Args:
        output: Combined stdout+stderr from the test runner.
        lang:   Detected language.

    Returns:
        (passed, failed, error) counts — all zero when nothing is parseable.
    """
    import re

    passed = failed = error = 0
    if lang in ("typescript", "javascript"):
        # Vitest: "✓ 5 tests passed" / "✗ 2 tests failed"
        # Jest: "Tests: 3 failed, 5 passed, 8 total"
        m = re.search(r"(\d+)\s+passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+)\s+failed", output)
        if m:
            failed = int(m.group(1))
    elif lang == "go":
        # "ok   github.com/... 0.123s" / "FAIL github.com/... 0.123s"
        passed = output.count("\nok  ")
        failed = output.count("\nFAIL ")
    elif lang == "rust":
        # "test result: ok. 5 passed; 0 failed"
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
    return passed, failed, error


def _default_test_cmd_for_language(lang: str) -> tuple[str, str]:
    """Return (test_cmd, test_dir) defaults for a given language.

    These are best-effort defaults; the actual test runner is refined at
    setup time by reading package.json / Cargo.toml / go.mod etc.
    """
    defaults: dict[str, tuple[str, str]] = {
        "python": ("pytest", "tests/"),
        "typescript": ("npx vitest run", "src"),
        "javascript": ("npx jest", "tests/"),
        "go": ("go test ./...", ""),
        "rust": ("cargo test", ""),
        "java": ("mvn test -q", ""),
    }
    return defaults.get(lang, ("pytest", "tests/"))


@dataclass
class Commit0Config:
    repo_name: str = "minitorch"
    base_branch: str = ""  # Empty = auto-detect repo's default branch
    language: str = ""  # Empty = auto-detect from repo contents via GitHub API
    docker_image_prefix: str = "docker.io/wentingzhao/"
    docker_image: str = (
        ""  # Override docker image directly (e.g., "docker.io/wentingzhao/minitorch:v0")
    )
    dataset_path: str = "data/commit0/commit0_combined"


class Commit0Task(TaskModule):
    def __init__(self, config):
        self.config = config
        self.task_data = None
        self._detected_language: str = ""  # populated by _resolve_language()

    def _get_clean_repo_name(self):
        """Extract repo name from full path/URL and strip any trailing .git."""
        name = self.config.repo_name.split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def _detect_language_from_github(self, repo: str) -> str:
        """Query the GitHub contents API to identify the repo's primary language.

        Checks sentinel files (Cargo.toml, go.mod, package.json, pyproject.toml …)
        in root order and returns the first match.  Falls back to 'python' on any
        error so existing behaviour is preserved.

        Args:
            repo: 'owner/name' string (trailing .git is stripped automatically).

        Returns:
            Lowercase language string, e.g. 'python', 'typescript', 'go', 'rust'.
        """
        import urllib.request

        if "/" not in repo:
            return "python"

        parts = repo.split("/")
        owner, name = parts[-2], parts[-1]
        if name.endswith(".git"):
            name = name[:-4]

        api_url = f"https://api.github.com/repos/{owner}/{name}/contents/"
        try:
            req = urllib.request.Request(
                api_url,
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "OrCAID/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                contents = json.loads(resp.read().decode())
                root_files = {
                    entry["name"] for entry in contents if entry.get("type") == "file"
                }
                for sentinel, lang in _LANGUAGE_SENTINELS:
                    if sentinel in root_files:
                        # Distinguish TypeScript vs JavaScript by checking for .ts files
                        if lang == "typescript" and "tsconfig.json" not in root_files:
                            # Check for any .ts file listing (heuristic: tsconfig present → TS)
                            # If absent, keep "typescript" — vitest/ts-node repos often omit it
                            pass
                        detected = lang
                        print(
                            f"[Commit0] Detected language: '{detected}' (sentinel: {sentinel})"
                        )
                        return detected
        except Exception as exc:
            print(
                f"[Commit0] Warning: language detection failed ({exc}), defaulting to 'python'"
            )

        return "python"

    def _resolve_language(self) -> str:
        """Return the project language: explicit config value or auto-detected."""
        if self._detected_language:
            return self._detected_language
        if self.config.language:
            self._detected_language = self.config.language.lower()
        else:
            self._detected_language = self._detect_language_from_github(
                self.config.repo_name
            )
        return self._detected_language

    def get_docker_image(self):
        """Return the Docker image to use for this repo.

        Priority:
        1. Explicit ``config.docker_image`` override.
        2. Official wentingzhao/ benchmark images (pre-built per-repo).
        3. Language-appropriate slim image detected from repo contents.
        """
        if self.config.docker_image:
            return self.config.docker_image

        repo_lower = self.config.repo_name.lower()

        # Official benchmark repos have pre-built images
        if repo_lower.startswith("wentingzhao/") or "/" not in self.config.repo_name:
            clean_name = repo_lower.split("/")[-1]
            prefix = self.config.docker_image_prefix.rstrip("/")
            return f"{prefix}/{clean_name}:v0".lower()

        # Custom repos: pick image based on detected language
        lang = self._resolve_language()
        image = _LANGUAGE_IMAGES.get(lang, "python:3.12-slim")
        print(f"[Commit0] Using Docker image '{image}' for language '{lang}'")
        return image

    def get_work_dir(self):
        clean_name = self._get_clean_repo_name()
        return f"/workspace/{clean_name}_repo"

    def get_workspace_config(self):
        return {
            "base_image": self.get_docker_image(),
            "target": "source-minimal",
        }

    def load_task_data(self):
        from datasets import load_from_disk

        try:
            dataset = load_from_disk(self.config.dataset_path)
            df = dataset.to_pandas()
            repo_data = df[df["repo"].str.contains(self.config.repo_name, case=False)]
            if not repo_data.empty:
                self.task_data = repo_data.iloc[0].to_dict()
                return self.task_data
        except Exception:
            # Gracefully ignore dataset loading errors for custom repositories
            pass

        # Dynamic dataset-free fallback for custom repositories
        print(
            f"[Commit0] Repository '{self.config.repo_name}' not found in dataset. Using dynamic dataset-free fallback."
        )
        repo_name = self.config.repo_name
        if "/" not in repo_name:
            repo_name = f"wentingzhao/{repo_name}"

        lang = self._resolve_language()
        test_cmd, test_dir = _default_test_cmd_for_language(lang)
        print(f"[Commit0] Default test runner for '{lang}': {test_cmd} {test_dir}")

        self.task_data = {
            "repo": repo_name,
            "language": lang,
            "test_cmd": test_cmd,
            "test_dir": test_dir,
            "test": {"test_cmd": test_cmd, "test_dir": test_dir},
        }
        return self.task_data

    def _detect_default_branch(self, repo: str) -> str:
        """Query the GitHub API to find the repo's actual default branch.

        Falls back to 'main' if the API call fails (no auth, rate-limit, etc.).
        Only queries github.com repos; non-GitHub repos return 'main'.

        Args:
            repo: Full repo path in 'owner/name' format.

        Returns:
            The default branch name string (e.g. 'main' or 'master').
        """
        import urllib.request

        if "/" not in repo:
            return "main"

        parts = repo.split("/")
        owner, name = parts[-2], parts[-1]
        if name.endswith(".git"):
            name = name[:-4]

        api_url = f"https://api.github.com/repos/{owner}/{name}"
        try:
            req = urllib.request.Request(
                api_url,
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "OrCAID/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                branch = data.get("default_branch", "main")
                print(
                    f"[Commit0] Detected default branch: '{branch}' for {owner}/{name}"
                )
                return branch
        except Exception as exc:
            print(
                f"[Commit0] Warning: could not detect default branch ({exc}), falling back to 'main'"
            )
            return "main"

    def _resolve_base_branch(self, repo: str) -> str:
        """Return the branch to clone: explicit config value or auto-detected default.

        Args:
            repo: Full 'owner/name' repo path used when auto-detecting.

        Returns:
            Branch name to pass to git clone -b.
        """
        if self.config.base_branch:
            return self.config.base_branch
        return self._detect_default_branch(repo)

    def setup_workspace(self, workspace):
        if self.task_data is None:
            raise RuntimeError("Call load_task_data() before setup_workspace()")

        work_dir = self.get_work_dir()
        repo = self.task_data["repo"]
        clean_name = self._get_clean_repo_name()

        # Step 1: Clone Repository (or copy if local path)
        print("\n" + "-" * 60)
        print("Step 1: Clone Repository")
        print("-" * 60)
        print(f"[Commit0] Cloning {repo}...")

        repo_url = repo
        if repo_url.startswith("/"):
            # Absolute local path — copy directory into the container instead of cloning

            local_path = repo
            print(f"[Commit0] Using local repo path: {local_path}")
            copy_cmd = f"cp -r {local_path} {work_dir}"
            result = workspace.execute_command(copy_cmd, timeout=600)
            if result.exit_code != 0:
                raise RuntimeError(f"Failed to copy local repo: {result.stderr}")
            # Create a working branch
            branch_cmd = f"cd {work_dir} && git checkout -b openhands 2>/dev/null || git checkout -b openhands || true"
            workspace.execute_command(branch_cmd, timeout=600)
        else:
            if not repo_url.startswith("http://") and not repo_url.startswith(
                "https://"
            ):
                repo_url = f"https://github.com/{repo}"
                if not repo_url.endswith(".git"):
                    repo_url += ".git"

            base_branch = self._resolve_base_branch(repo)
            print(f"[Commit0] Using branch: '{base_branch}'")

            clone_cmd = (
                f"cd /workspace && "
                f"git clone --depth 1 -b {base_branch} "
                f"{repo_url} {clean_name}_repo"
            )
            result = workspace.execute_command(clone_cmd, timeout=600)
            if result.exit_code != 0:
                raise RuntimeError(f"Failed to clone repo: {result.stderr}")

            # Create a working branch (matches official OpenHands benchmark)
            branch_cmd = f"cd {work_dir} && git checkout -b openhands"
            result = workspace.execute_command(branch_cmd, timeout=600)
            if result.exit_code != 0:
                raise RuntimeError(f"Failed to create branch: {result.stderr}")

        # Step 2: Setup Repository
        print("\n" + "-" * 60)
        print("Step 2: Setup Repository")
        print("-" * 60)
        lang = self._resolve_language()
        print(f"[Commit0] Project language: {lang}")
        self._setup_deps(workspace, work_dir, clean_name, lang)

        # Pre-compute stub/test analysis so the manager prompt has it from iteration 1
        print("\n" + "-" * 60)
        print("Step 2b: Initial Analysis (stubs + failing tests)")
        print("-" * 60)
        self._initial_analysis = self.get_initial_analysis(workspace)

    # ------------------------------------------------------------------
    # Language-aware dependency installation
    # ------------------------------------------------------------------

    def _setup_deps(self, workspace, work_dir: str, clean_name: str, lang: str) -> None:
        """Install project dependencies and test tooling for the detected language.

        Args:
            workspace: OpenHands workspace handle.
            work_dir: Absolute path of the cloned repo inside the container.
            clean_name: Sanitised repo name (used for Python package verification).
            lang: Detected language string from ``_resolve_language()``.
        """
        if lang == "python":
            self._setup_python(workspace, work_dir, clean_name)
        elif lang in ("typescript", "javascript"):
            self._setup_node(workspace, work_dir, lang)
        elif lang == "go":
            self._setup_go(workspace, work_dir)
        elif lang == "rust":
            self._setup_rust(workspace, work_dir)
        elif lang == "java":
            self._setup_java(workspace, work_dir)
        else:
            print(f"[Commit0] Unknown language '{lang}', falling back to Python setup.")
            self._setup_python(workspace, work_dir, clean_name)
        print("[Commit0] Workspace setup complete")

    def _setup_python(self, workspace, work_dir: str, clean_name: str) -> None:
        """Install Python package + pytest tooling."""
        print(f"[Commit0] Installing {clean_name} in dev mode...")
        workspace.execute_command(
            f"python -m pip uninstall -y {clean_name} {clean_name.lower()} 2>&1 | tail -3",
            timeout=60,
        )
        result = workspace.execute_command(
            f"cd {work_dir} && python -m pip install -e . 2>&1", timeout=300
        )
        if result.exit_code != 0:
            # Retry ignoring version constraints (e.g. requires-python mismatch with container)
            result2 = workspace.execute_command(
                f"cd {work_dir} && python -m pip install -e . --ignore-requires-python 2>&1",
                timeout=300,
            )
            if result2.exit_code != 0:
                # Fall back to installing just the dependencies from requirements.txt
                print(
                    "[Commit0] pip install -e . failed, falling back to requirements install"
                )
                for req_file in (
                    "requirements.txt",
                    "requirements-dev.txt",
                    "requirements_dev.txt",
                ):
                    workspace.execute_command(
                        f"cd {work_dir} && [ -f {req_file} ] && "
                        f"python -m pip install -r {req_file} --ignore-requires-python 2>&1 | tail -5 || true",
                        timeout=300,
                    )
            else:
                print("[Commit0] Installed with --ignore-requires-python")

        # Detect the actual importable package name from pyproject.toml or setup.py
        # rather than guessing from the repo name (which is often wrong for script repos)
        pkg_detect = (
            f"cd {work_dir} && python3 - <<'EOF'\n"
            f"import sys, os\n"
            f"try:\n"
            f"    import tomllib\n"
            f"except ImportError:\n"
            f"    import tomli as tomllib\n"
            f"try:\n"
            f"    with open('pyproject.toml', 'rb') as f:\n"
            f"        data = tomllib.load(f)\n"
            f"    pkgs = data.get('tool', {{}}).get('setuptools', {{}}).get('packages', [])\n"
            f"    if pkgs:\n"
            f"        print(pkgs[0])\n"
            f"        sys.exit(0)\n"
            f"except Exception:\n"
            f"    pass\n"
            f"# Fallback: first subdirectory containing __init__.py\n"
            f"for d in sorted(os.listdir('.')):\n"
            f"    if os.path.isdir(d) and os.path.exists(os.path.join(d, '__init__.py')):\n"
            f"        if d not in ('tests', 'test', 'docs', '.venv', 'venv'):\n"
            f"            print(d)\n"
            f"            sys.exit(0)\n"
            f"EOF"
        )
        pkg_result = workspace.execute_command(pkg_detect, timeout=15)
        pkg_name = pkg_result.stdout.strip() if pkg_result.exit_code == 0 else ""

        if pkg_name:
            verify_result = workspace.execute_command(
                f"cd {work_dir} && python -c 'import {pkg_name}; print(\"{pkg_name} imported successfully\")'",
                timeout=30,
            )
            if verify_result.exit_code == 0:
                print(f"[Commit0] Package verification: {verify_result.stdout.strip()}")
            else:
                print(
                    f"[Commit0] Note: '{pkg_name}' not importable (script-based repo or missing deps — tests may still run fine)"
                )
        else:
            print(
                "[Commit0] Note: No importable package detected (script-based repo — tests run via pytest directly)"
            )

        print("[Commit0] Installing pytest plugins...")
        uv = workspace.execute_command(
            f"cd {work_dir} && /root/.cargo/bin/uv pip install commit0 2>&1 | tail -5",
            timeout=300,
        )
        if uv.exit_code != 0:
            workspace.execute_command(
                f"cd {work_dir} && python -m pip install commit0 2>&1 | tail -5",
                timeout=300,
            )
        workspace.execute_command(
            f"cd {work_dir} && python -m pip install pytest-json-report pytest-cov 2>&1 | tail -5",
            timeout=300,
        )

    def _setup_node(self, workspace, work_dir: str, lang: str) -> None:
        """Install Node/TypeScript dependencies and detect the test runner."""
        print("[Commit0] Installing Node.js dependencies...")
        # Prefer pnpm → yarn → npm
        for pkg_mgr, lock in (
            ("pnpm", "pnpm-lock.yaml"),
            ("yarn", "yarn.lock"),
            ("npm", "package-lock.json"),
        ):
            probe = workspace.execute_command(
                f"test -f {work_dir}/{lock} && echo yes || echo no", timeout=10
            )
            if probe.stdout.strip() == "yes":
                install_cmd = f"cd {work_dir} && {pkg_mgr} install --frozen-lockfile 2>&1 | tail -10"
                break
        else:
            install_cmd = f"cd {work_dir} && npm install 2>&1 | tail -10"

        result = workspace.execute_command(install_cmd, timeout=600)
        if result.exit_code != 0:
            print(f"[Commit0] Warning: npm/pnpm/yarn install failed: {result.stderr}")

        # Detect test runner from package.json scripts and update task_data
        pkg_result = workspace.execute_command(
            f"cat {work_dir}/package.json 2>/dev/null", timeout=10
        )
        if pkg_result.exit_code == 0:
            try:
                pkg = json.loads(pkg_result.stdout)
                scripts = pkg.get("scripts", {})
                deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
                if "vitest" in deps or "vitest" in scripts.get("test", ""):
                    test_cmd, test_dir = "npx vitest run", "src"
                elif "jest" in deps or "jest" in scripts.get("test", ""):
                    test_cmd, test_dir = "npx jest --json", "."
                elif "mocha" in deps:
                    test_cmd, test_dir = "npx mocha", "test"
                else:
                    # Fall back to the npm test script
                    test_cmd, test_dir = "npm test", ""
                print(f"[Commit0] Detected test runner: {test_cmd}")
                if self.task_data:
                    self.task_data["test_cmd"] = test_cmd
                    self.task_data["test_dir"] = test_dir
                    self.task_data["test"] = {
                        "test_cmd": test_cmd,
                        "test_dir": test_dir,
                    }
            except (json.JSONDecodeError, KeyError):
                pass

        # Ensure TypeScript compiler is available
        workspace.execute_command(
            f"cd {work_dir} && npx tsc --version 2>&1 || npm install -g typescript 2>&1 | tail -5",
            timeout=120,
        )

    def _setup_go(self, workspace, work_dir: str) -> None:
        """Download Go module dependencies."""
        print("[Commit0] Downloading Go modules...")
        result = workspace.execute_command(
            f"cd {work_dir} && go mod download 2>&1 | tail -10", timeout=300
        )
        if result.exit_code != 0:
            print(f"[Commit0] Warning: go mod download failed: {result.stderr}")
        workspace.execute_command(
            f"cd {work_dir} && go build ./... 2>&1 | tail -5", timeout=300
        )

    def _setup_rust(self, workspace, work_dir: str) -> None:
        """Pre-fetch Rust crates."""
        print("[Commit0] Fetching Rust dependencies...")
        result = workspace.execute_command(
            f"cd {work_dir} && cargo fetch 2>&1 | tail -10", timeout=600
        )
        if result.exit_code != 0:
            print(f"[Commit0] Warning: cargo fetch failed: {result.stderr}")

    def _setup_java(self, workspace, work_dir: str) -> None:
        """Resolve Java dependencies (Maven or Gradle)."""
        print("[Commit0] Resolving Java dependencies...")
        mvn = workspace.execute_command(
            f"test -f {work_dir}/pom.xml && echo yes || echo no", timeout=10
        )
        if mvn.stdout.strip() == "yes":
            workspace.execute_command(
                f"cd {work_dir} && mvn dependency:resolve -q 2>&1 | tail -10",
                timeout=600,
            )
        else:
            workspace.execute_command(
                f"cd {work_dir} && ./gradlew dependencies -q 2>&1 | tail -10",
                timeout=600,
            )

    # ------------------------------------------------------------------
    # Proactive stub + failing-test context for the manager
    # ------------------------------------------------------------------

    def get_initial_analysis(self, workspace) -> str:
        """Run a comprehensive pre-scan and return a prioritised code health report.

        Goes well beyond stub detection: surfaces broken imports, TODO/FIXME bugs,
        linter errors, empty/incomplete functions, and structural issues — so the
        manager has real actionable work even when no explicit `pass` stubs exist.

        Sections (highest → lowest priority):
          1. FAILING TESTS   — which tests fail and the first error / traceback line
          2. IMPORT ERRORS   — modules that cannot be imported (PYTHONPATH, missing deps)
          3. LINTER ISSUES   — ruff/flake8 errors: undefined names, unused imports, etc.
          4. TODO/FIXME BUGS — inline markers with surrounding context
          5. STUBS           — explicit placeholder functions (pass, todo!(), etc.)
          6. INCOMPLETE CODE — raise NotImplementedError, empty function bodies, ...
          7. STRUCTURAL      — mismatched file/module names, missing __init__.py, etc.

        Returns:
            A multi-line string suitable for injection into the manager prompt.
        """
        if self.task_data is None:
            return ""
        work_dir = self.get_work_dir()
        lang = self._resolve_language()
        sections: list[str] = []

        # ------------------------------------------------------------------
        # Helper: run a command and return stripped stdout (or fallback text)
        # ------------------------------------------------------------------
        def _run(cmd: str, timeout: int = 30) -> str:
            r = workspace.execute_command(cmd, timeout=timeout)
            return (r.stdout or "").strip()

        # ------------------------------------------------------------------
        # 1. FAILING TESTS
        # ------------------------------------------------------------------
        test_info = self.task_data.get("test", {})
        test_cmd = test_info.get("test_cmd", self.task_data.get("test_cmd", "pytest"))
        test_dir = test_info.get("test_dir", self.task_data.get("test_dir", "tests/"))

        if lang == "python" or test_cmd.strip().startswith("pytest"):
            quick_test = (
                f"cd {work_dir} && "
                f"export PYTHONPATH={work_dir}/src:{work_dir}:{work_dir}/codes:$PYTHONPATH && "
                f"python -m {test_cmd if test_cmd.startswith('pytest') else test_cmd} "
                f"{test_dir} --tb=line -q --no-header 2>&1 | head -60"
            )
        else:
            quick_test = f"cd {work_dir} && {test_cmd} {test_dir} 2>&1 | head -60"

        test_out = _run(quick_test, timeout=120)
        if (
            "passed" in test_out
            and "failed" not in test_out
            and "error" not in test_out.lower()
        ):
            sections.append("### FAILING TESTS\nnone — all tests pass on clean clone.")
        else:
            sections.append(
                f"### FAILING TESTS (initial run)\n{test_out or '(no output)'}"
            )

        # ------------------------------------------------------------------
        # 2. IMPORT ERRORS (Python only)
        # ------------------------------------------------------------------
        if lang == "python":
            import_check = (
                f"cd {work_dir} && "
                f"find . -name '*.py' -not -path '*/.venv/*' -not -path '*/site-packages/*' "
                f"-not -path '*/__pycache__/*' | head -40 | "
                f"xargs -I{{}} python3 -c \"import ast, sys; ast.parse(open('{{}}').read())\" "
                f"2>&1 | grep -v '^$' | head -20"
            )
            syntax_out = _run(import_check, timeout=30)

            # Also try to import top-level packages
            pkgs = _run(
                f"cd {work_dir} && "
                f"find . -maxdepth 2 -name '__init__.py' "
                f"| sed 's|./||;s|/__init__.py||;s|/|.|g' "
                f"| grep -v 'test\\|setup\\|\\.venv\\|site-packages' | head -10",
                timeout=15,
            )
            import_errors: list[str] = []
            if syntax_out:
                import_errors.append(f"Syntax errors:\n{syntax_out}")
            for pkg in pkgs.splitlines():
                pkg = pkg.strip()
                if not pkg:
                    continue
                err = _run(
                    f"cd {work_dir} && "
                    f"PYTHONPATH={work_dir}/src:{work_dir}:{work_dir}/codes:$PYTHONPATH "
                    f"python3 -c 'import {pkg}' 2>&1 | head -3",
                    timeout=10,
                )
                if err and "Error" in err:
                    import_errors.append(f"  import {pkg}: {err.splitlines()[0]}")

            if import_errors:
                sections.append("### IMPORT ERRORS\n" + "\n".join(import_errors))
            else:
                sections.append("### IMPORT ERRORS\nnone detected.")

        # ------------------------------------------------------------------
        # 3. LINTER ISSUES  (ruff preferred, flake8 fallback, eslint for JS/TS)
        # ------------------------------------------------------------------
        if lang == "python":
            ruff_out = _run(
                f"cd {work_dir} && "
                f"ruff check . --select=E,F,I,UP,B --ignore=E501 "
                f"--exclude=.venv,__pycache__,site-packages 2>&1 | head -40",
                timeout=30,
            )
            if not ruff_out or "command not found" in ruff_out:
                ruff_out = _run(
                    f"cd {work_dir} && "
                    f"python3 -m flake8 . --select=E,F,W --exclude=.venv,__pycache__ "
                    f"--max-line-length=120 2>&1 | head -40",
                    timeout=30,
                )
            sections.append(
                f"### LINTER ISSUES\n{ruff_out or 'none (ruff/flake8 not available or no issues).'}"
            )
        elif lang in ("typescript", "javascript"):
            eslint_out = _run(
                f"cd {work_dir} && npx eslint . --ext .ts,.tsx,.js --max-warnings=0 2>&1 | head -30",
                timeout=30,
            )
            sections.append(f"### LINTER ISSUES\n{eslint_out or 'none.'}")
        elif lang == "rust":
            cargo_out = _run(
                f"cd {work_dir} && cargo clippy -- -D warnings 2>&1 | head -40",
                timeout=60,
            )
            sections.append(f"### LINTER ISSUES\n{cargo_out or 'none.'}")

        # ------------------------------------------------------------------
        # 4. TODO / FIXME / HACK / BUG markers with context
        # ------------------------------------------------------------------
        if lang == "python":
            todo_cmd = (
                f"cd {work_dir} && "
                f"grep -rn 'TODO\\|FIXME\\|HACK\\|BUG\\|XXX' --include='*.py' "
                f"--exclude-dir=.venv --exclude-dir=__pycache__ -A1 2>/dev/null | head -60"
            )
        elif lang in ("typescript", "javascript"):
            todo_cmd = (
                f"cd {work_dir} && "
                f"grep -rn 'TODO\\|FIXME\\|HACK\\|BUG' "
                f"--include='*.ts' --include='*.tsx' --include='*.js' "
                f"--exclude-dir=node_modules -A1 2>/dev/null | head -60"
            )
        elif lang == "go":
            todo_cmd = (
                f"cd {work_dir} && "
                f"grep -rn 'TODO\\|FIXME\\|HACK\\|BUG' --include='*.go' -A1 2>/dev/null | head -60"
            )
        else:
            todo_cmd = (
                f"cd {work_dir} && "
                f"grep -rn 'TODO\\|FIXME\\|HACK\\|BUG' "
                f"--include='*.py' --include='*.ts' --include='*.go' --include='*.rs' "
                f"-A1 2>/dev/null | head -60"
            )
        todo_out = _run(todo_cmd, timeout=20)
        sections.append(f"### TODO/FIXME MARKERS\n{todo_out or 'none found.'}")

        # ------------------------------------------------------------------
        # 5. STUB CANDIDATES (explicit placeholders)
        # ------------------------------------------------------------------
        stub_cmd = _STUB_PATTERNS.get(lang, _STUB_PATTERNS["python"])
        stub_out = _run(
            f"cd {work_dir} && {stub_cmd} 2>/dev/null | head -60", timeout=30
        )
        sections.append(
            f"### STUB CANDIDATES ({lang})\n{stub_out or 'none found with standard patterns.'}"
        )

        # ------------------------------------------------------------------
        # 6. INCOMPLETE CODE (raise NotImplementedError, empty bodies, ...)
        # ------------------------------------------------------------------
        if lang == "python":
            notimpl_out = _run(
                f"cd {work_dir} && "
                f"grep -rn 'raise NotImplementedError\\|raise NotImplemented' --include='*.py' "
                f"--exclude-dir=.venv --exclude-dir=__pycache__ 2>/dev/null | head -30",
                timeout=20,
            )
            # Also catch functions whose body is only a docstring + implicit None return
            empty_fn_out = _run(
                f"cd {work_dir} && "
                f"python3 - <<'EOF'\n"
                f"import ast, os, sys\n"
                f"issues = []\n"
                f"for root, dirs, files in os.walk('.'):\n"
                f"    dirs[:] = [d for d in dirs if d not in ('.venv','__pycache__','site-packages')]\n"
                f"    for f in files:\n"
                f"        if not f.endswith('.py'): continue\n"
                f"        path = os.path.join(root, f)\n"
                f"        try:\n"
                f"            tree = ast.parse(open(path).read())\n"
                f"        except SyntaxError:\n"
                f"            continue\n"
                f"        for node in ast.walk(tree):\n"
                f"            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)): continue\n"
                f"            body = [n for n in node.body if not isinstance(n, ast.Expr)]\n"
                f"            if len(body) == 0 and len(node.body) <= 1:\n"
                f"                issues.append(f'{{path}}:{{node.lineno}}: {{node.name}}() — empty body')\n"
                f"for i in issues[:20]: print(i)\n"
                f"EOF",
                timeout=30,
            )
            incomplete_parts = []
            if notimpl_out:
                incomplete_parts.append(f"raise NotImplementedError:\n{notimpl_out}")
            if empty_fn_out:
                incomplete_parts.append(f"Empty function bodies:\n{empty_fn_out}")
            sections.append(
                "### INCOMPLETE CODE\n"
                + (
                    "\n".join(incomplete_parts)
                    if incomplete_parts
                    else "none detected."
                )
            )

        # ------------------------------------------------------------------
        # 7. STRUCTURAL ISSUES (Python: missing __init__.py, PYTHONPATH hints)
        # ------------------------------------------------------------------
        if lang == "python":
            # Detect subdirectories that contain .py files but no __init__.py
            missing_init = _run(
                f"cd {work_dir} && "
                f"find . -name '*.py' -not -path '*/.venv/*' -not -path '*/__pycache__/*' "
                f"-not -path '*/site-packages/*' "
                f"| xargs -I{{}} dirname {{}} | sort -u "
                f'| while read d; do [ ! -f "$d/__init__.py" ] && echo "$d"; done '
                f"| grep -v '^\\.\\?$' | head -20",
                timeout=20,
            )
            # Detect shell scripts / Makefiles that set PYTHONPATH
            path_hints = _run(
                f"cd {work_dir} && "
                f"grep -rn 'PYTHONPATH\\|sys.path' "
                f"--include='*.sh' --include='Makefile' --include='*.py' "
                f"--exclude-dir=.venv --exclude-dir=__pycache__ 2>/dev/null | head -20",
                timeout=20,
            )
            structural_parts = []
            if missing_init:
                structural_parts.append(
                    f"Dirs with .py files but no __init__.py (may cause import failures):\n{missing_init}"
                )
            if path_hints:
                structural_parts.append(
                    f"PYTHONPATH/sys.path hints (verify they are set correctly):\n{path_hints}"
                )
            sections.append(
                "### STRUCTURAL ISSUES\n"
                + (
                    "\n".join(structural_parts)
                    if structural_parts
                    else "none detected."
                )
            )

        summary = "\n\n".join(sections)
        print(
            f"[Commit0] Initial analysis complete ({len(summary)} chars, {len(sections)} sections)"
        )
        return summary

    def evaluate(self, workspace):
        if self.task_data is None:
            raise RuntimeError("Call load_task_data() before evaluate()")

        work_dir = self.get_work_dir()

        # Commit any remaining changes
        print("[Commit0] Committing any remaining changes...")
        workspace.execute_command(f"cd {work_dir} && git add .", timeout=600)
        workspace.execute_command(
            f"cd {work_dir} && "
            'git config --global user.email "evaluation@openhands.dev" && '
            'git config --global user.name "OpenHands Evaluation" && '
            'git commit -m "final changes before test" || true',
            timeout=600,
        )

        # Determine test command from task data
        lang = self._resolve_language()
        test_info = self.task_data.get("test", {})
        test_cmd = test_info.get("test_cmd", self.task_data.get("test_cmd", "pytest"))
        test_dir = test_info.get("test_dir", self.task_data.get("test_dir", "tests/"))

        if lang == "python" or test_cmd.strip().startswith("pytest"):
            # Python path: pytest with JSON reporting
            if test_cmd.strip().startswith("pytest"):
                test_cmd = "python -m " + test_cmd.strip()
            full_cmd = (
                f"cd {work_dir} && "
                f"export PYTHONPATH={work_dir}/src:{work_dir}:$PYTHONPATH && "
                f"{test_cmd} "
                f"--json-report --json-report-file=report.json "
                f"--continue-on-collection-errors "
                f"{test_dir} > test_output.txt 2>&1"
            )
        else:
            # Non-Python: run test command as-is, capture output
            full_cmd = (
                f"cd {work_dir} && " f"{test_cmd} {test_dir} > test_output.txt 2>&1"
            )

        print(f"[Commit0] Running: {test_cmd} {test_dir}")
        workspace.execute_command(full_cmd, timeout=6000)

        # Read output
        output_result = workspace.execute_command(
            f"cat {work_dir}/test_output.txt", timeout=60
        )
        test_output = output_result.stdout if output_result.exit_code == 0 else ""

        # Parse results — pytest JSON for Python, heuristic parse for others
        passed = failed = error = 0
        report_json = "{}"

        if lang == "python" or "pytest" in test_cmd:
            report_result = workspace.execute_command(
                f"cat {work_dir}/report.json", timeout=60
            )
            report_json = report_result.stdout if report_result.exit_code == 0 else "{}"
            try:
                report_data = json.loads(report_json)
                summary = report_data.get("summary", {})
                passed = summary.get("passed", 0)
                failed = summary.get("failed", 0)
                error = summary.get("error", 0)
            except (json.JSONDecodeError, Exception) as e:
                print(f"[Commit0] Warning: could not parse report.json: {e}")
        else:
            # Heuristic: count pass/fail from output text
            passed, failed, error = _parse_test_output_heuristic(test_output, lang)

        print(
            f"[Commit0] Test results: {passed} passed, {failed} failed, {error} error"
        )

        return {
            "exit_code": str(output_result.exit_code),
            "test_output": test_output,
            "report_json": report_json,
            "passed": passed,
            "failed": failed,
            "error": error,
        }

    def get_prompt_format_args(self, config):
        work_dir = self.get_work_dir()
        workspace_dir_name = work_dir.split("/")[-1]
        lang = self._resolve_language()
        test_info = self.task_data.get("test", {}) if self.task_data else {}
        test_cmd = test_info.get(
            "test_cmd",
            self.task_data.get("test_cmd", "pytest") if self.task_data else "pytest",
        )
        test_dir = test_info.get(
            "test_dir",
            self.task_data.get("test_dir", "tests/") if self.task_data else "tests/",
        )
        if lang == "python" and test_cmd.strip().startswith("pytest"):
            test_cmd = "python -m " + test_cmd.strip()

        # initial_analysis is pre-computed during setup and stored for injection
        initial_analysis = getattr(self, "_initial_analysis", "")

        return {
            "max_agents": config.max_subagents,
            "max_rounds": config.max_rounds_chat,
            "workspace_dir_name": workspace_dir_name,
            "test_cmd": test_cmd,
            "test_dir": test_dir,
            "language": lang,
            "initial_analysis": initial_analysis,
        }

    # ---- Manager integration methods ----

    def get_scan_log_kwargs(self, config):
        return {
            "repo_name": self.config.repo_name,
            "repo_path": self.get_work_dir(),
            "max_iterations": config.manager_max_iterations,
        }

    def build_subagent(self, engineer_id, primary_task, all_tasks):
        from orcaid.config import SubAgent

        all_files = []
        all_functions = []
        all_instructions = []
        for t in all_tasks:
            all_files.append(t.file_path)
            all_functions.extend(t.functions_to_implement)
            all_instructions.append(f"File: {t.file_path}\n{t.instruction}")
        combined_instruction = "\n\n---\n\n".join(all_instructions)
        combined_file_path = (
            ", ".join(all_files) if len(all_files) > 1 else all_files[0]
        )
        subagent = SubAgent(
            engineer_id=engineer_id,
            task_id=primary_task.task_id,
            file_path=combined_file_path,
            functions_to_implement=all_functions,
            instruction=combined_instruction,
            estimated_complexity=primary_task.estimated_complexity,
        )
        combine_log = (
            f"  (Combined {len(all_tasks)} tasks: {all_files})"
            if len(all_tasks) > 1
            else None
        )
        return subagent, combine_log

    def get_worktree_name(self, engineer_id):
        clean_name = self._get_clean_repo_name()
        return f"{clean_name}_worktree_{engineer_id}"

    def get_subagent_log_lines(self, subagent):
        lines = [f"      Task: {subagent.file_path}"]
        funcs_str = ", ".join(subagent.functions_to_implement[:3])
        if len(subagent.functions_to_implement) > 3:
            funcs_str += f"... (+{len(subagent.functions_to_implement)-3})"
        lines.append(f"      Functions: {funcs_str}")
        return lines

    @property
    def should_stash_before_merge(self):
        return True

    @property
    def should_try_uncommitted_merge(self):
        return True

    def build_completed_task_summary(self, result, task_status):
        return (
            f"task_id: {result.task_id}\n"
            f"file: {result.file_path}\n"
            f"status: {task_status}\n"
            f"merged: {result.merged}\n"
            f"merge_method: {result.merge_method or 'none'}\n"
            f"commit: {result.commit_hash or 'none'}\n"
            f"commit_message: {result.commit_message or 'none'}"
        )

    def extract_assignments(self, assign_data):
        assignments = assign_data.get("assignments", [])
        if not assignments and "next_task" in assign_data:
            if assign_data.get("should_assign", False):
                assignments = [assign_data["next_task"]]
        return assignments

    def get_assign_context(self, all_completed, workspace, repo_dir):
        cmd_result = workspace.execute_command(
            f"cd {repo_dir} && git rev-parse HEAD", timeout=30
        )
        current_head = cmd_result.stdout.strip() if cmd_result.exit_code == 0 else ""

        completed_files = set()
        for completed in all_completed:
            if completed.merged and completed.file_path:
                completed_files.add(completed.file_path)
        progress_summary = ""
        if completed_files:
            files_list = "\n".join(f"  - {f}" for f in sorted(completed_files))
            progress_summary = f"Files completed by other agents:\n{files_list}"

        return {"current_head": current_head, "progress_summary": progress_summary}

    def update_subagent_for_assignment(self, subagent, context, workspace, log_fn):
        current_head = context.get("current_head", "")
        progress_summary = context.get("progress_summary", "")

        if current_head and subagent.file_path:
            worktree_name = self.get_worktree_name(subagent.engineer_id)
            subagent.worktree_path = f"/workspace/{worktree_name}"
            subagent.base_commit = current_head

            update_cmd = (
                f"cd {subagent.worktree_path} && git reset --hard {current_head}"
            )
            update_result = workspace.execute_command(update_cmd, timeout=60)

            if update_result.exit_code != 0:
                log_fn(
                    f"Failed to update worktree for {subagent.engineer_id}: {update_result.stderr}"
                )
                subagent.status = "failed"
            else:
                subagent.status = "ready"
                log_fn(
                    f"Worktree for {subagent.engineer_id} updated to {current_head[:8]}"
                )

            if progress_summary:
                subagent.instruction = f"{progress_summary}\n\n{subagent.instruction}"
        else:
            subagent.status = "ready"

    def get_single_agent_info(self, workspace, config, prompts):
        header = "Single Agent Mode - Implementing all functions"
        format_args = self.get_prompt_format_args(config)
        format_args["repo_path"] = self.get_work_dir()
        user_instruction = prompts.get("single_agent_instruction", "").format(
            **format_args
        )
        log_content = {
            "repo_name": self.config.repo_name,
            "repo_path": self.get_work_dir(),
            "max_iterations": config.manager_max_iterations,
        }
        return header, user_instruction, log_content

    def get_final_review_log_extras(self, subagent_results):
        merged_count = sum(1 for r in subagent_results if r.merged and r.file_path)
        return {"files_merged": merged_count}

    def get_collect_extra_log(self, subagent_result):
        if subagent_result.file_path:
            return f"  - File: {subagent_result.file_path}"
        return ""

    # ---- SubAgent runner integration methods ----

    def create_subagent_result(self, subagent):
        from orcaid.config import SubAgentResult

        return SubAgentResult(
            engineer_id=subagent.engineer_id,
            task_id=subagent.task_id,
            task_node_id=subagent.task_node_id,
            branch_name=subagent.branch_name or "",
            worktree_path=subagent.worktree_path or "",
            file_path=subagent.file_path,
            functions_implemented=subagent.functions_to_implement.copy(),
            round_num=subagent.current_round,
        )

    def get_followup_prompt_args(self, subagent):
        return {
            "instruction": subagent.instruction,
            "file_path": subagent.file_path,
            "functions": ", ".join(subagent.functions_to_implement),
        }

    def get_run_start_log_lines(self, subagent):
        return [
            f"  - Task: {subagent.task_id}",
            f"  - File: {subagent.file_path}",
            f"  - Functions: {', '.join(subagent.functions_to_implement)}",
        ]

    @property
    def should_setup_on_retry(self):
        return True

    @property
    def should_resend_on_retry(self):
        return True

    def populate_no_commit_result(self, result):
        result.git_diff = ""

    def populate_success_result(self, result, runner, commit_info):
        result.success = True
        result.commit_hash = commit_info.get("hash", "")
        result.commit_message = commit_info.get("message", "")
        result.git_diff = runner.get_git_diff()
        result.files_modified = runner.get_modified_files()

    def get_event_serialization_extras(self, subagent):
        return {"file_path": subagent.file_path}

    def get_print_summary_lines(self, result, commit_info):
        lines = []
        if result.files_modified:
            lines.append(f"  Files Modified: {', '.join(result.files_modified)}")
        if result.git_diff:
            diff_preview = result.git_diff[:500]
            if len(result.git_diff) > 500:
                diff_preview += "\n... (truncated)"
            lines.append("  Diff Preview:")
            for line in diff_preview.split("\n")[:15]:
                lines.append(f"    {line}")
        return lines

    def prepare_reuse_subagent(self, new_subagent, old_runner):
        """Prepare a new subagent for reuse by copying state from the old runner.

        Copies task-specific information (worktree path, branch name, base commit)
        from the old runner's subagent to the new subagent, enabling the new
        subagent to continue working in the same context.

        Args:
            new_subagent: The new SubAgent that will take over the task.
            old_runner: The previous SubAgentRunner that was handling a task.
        """
        new_subagent.worktree_path = old_runner.subagent.worktree_path
        new_subagent.branch_name = old_runner.subagent.branch_name
        new_subagent.base_commit = old_runner.subagent.base_commit

    def get_new_task_print_lines(self, subagent):
        lines = [f"- New file: {subagent.file_path}"]
        funcs = ", ".join(subagent.functions_to_implement[:3])
        if len(subagent.functions_to_implement) > 3:
            funcs += f"... (+{len(subagent.functions_to_implement)-3})"
        lines.append(f"- Functions: {funcs}")
        return lines

    def get_onboard_names(self, engineer_id):
        clean_name = self._get_clean_repo_name()
        branch_name = f"agent_{engineer_id}"
        worktree_name = f"{clean_name}_worktree_{engineer_id}"
        return branch_name, worktree_name

    def post_onboard_subagent(self, subagent, repo_dir):
        """Perform post-onboarding setup for a newly onboarded subagent.

        Called after the worktree and branch are created for a subagent.
        This method handles any additional setup such as logging, workspace
        preparation, or recording the subagent state.

        Args:
            subagent: The newly onboarded SubAgent.
            repo_dir: The path to the main repository directory.
        """
        print(
            f"[SubAgents] Onboarded engineer {subagent.engineer_id}: "
            f"branch={subagent.branch_name}, worktree={subagent.worktree_path}"
        )

    def get_completion_print_lines(self, result):
        lines = []
        if result.commit_hash:
            lines.append(f"- Commit: {result.commit_hash}")
        return lines

    def get_log_agent_response_kwargs(self, result):
        from datetime import datetime

        return {
            "engineer_id": result.engineer_id,
            "task_id": result.task_id,
            "success": result.success,
            "commit_hash": result.commit_hash,
            "git_diff": result.git_diff,
            "files_modified": result.files_modified,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "actual_iterations": result.actual_iterations,
            "max_iterations": result.max_iterations,
            "cost": result.cost,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "start_time": (
                datetime.fromisoformat(result.start_time) if result.start_time else None
            ),
            "end_time": (
                datetime.fromisoformat(result.end_time) if result.end_time else None
            ),
            "round_num": result.round_num,
        }

    def get_conflict_instruction_args(
        self, subagent, conflict_files, workspace, repo_dir
    ):
        conflict_file_list = "\n".join(f"  - {f}" for f in conflict_files)
        return {"conflict_file_list": conflict_file_list}

    def get_auto_reassign_instruction_args(self, subagent):
        return {"original_instruction": subagent.instruction}

    def get_execution_summary_lines(self, results):
        lines = [
            f"\n{'=' * 70}",
            "[SubAgents] Execution Summary",
            f"{'=' * 70}",
            f"Total task completions: {len(results)}",
        ]
        merged_count = len([r for r in results if r.merged])
        committed_count = len([r for r in results if r.success])
        recovered_count = len([r for r in results if r.merged and not r.success])
        failed_count = len([r for r in results if not r.merged])
        lines.append(
            f"Merged: {merged_count} (committed: {committed_count}, recovered: {recovered_count})"
        )
        lines.append(f"Failed: {failed_count}")

        agent_results = {}
        for result in results:
            if result.engineer_id not in agent_results:
                agent_results[result.engineer_id] = []
            agent_results[result.engineer_id].append(result)

        for engineer_id, agent_res in agent_results.items():
            lines.append(f"\n  {engineer_id}:")
            for res in agent_res:
                if res.merged and res.success:
                    status = "SUCCESS"
                elif res.merged and not res.success:
                    status = "RECOVERED"
                else:
                    status = "FAILED"
                lines.append(f"Round {res.round_num}: {status} - {res.task_id}")
                if res.commit_hash:
                    lines.append(f"      Commit: {res.commit_hash}")
                if res.merge_method:
                    lines.append(f"      Merge method: {res.merge_method}")
                if res.error and not res.merged:
                    lines.append(f"      Error: {res.error[:80]}")

        return lines


if __name__ == "__main__":
    config = Commit0Config(repo_name="minitorch")
    task = Commit0Task(config)

    print("=== Commit0 Task Prepare Test ===\n")

    # 1. Docker image
    image = task.get_docker_image()
    print(f"Docker image : {image}")
    assert image == "docker.io/wentingzhao/minitorch:v0", f"unexpected: {image}"

    # 2. Work dir
    work_dir = task.get_work_dir()
    print(f"Work dir     : {work_dir}")
    assert work_dir == "/workspace/minitorch_repo"

    # 3. Workspace config
    kwargs = task.get_workspace_config()
    assert kwargs["base_image"] == image
    assert kwargs["target"] == "source-minimal"

    # 4. Different repo names
    for repo in ["simpy", "portalocker", "flask"]:
        t = Commit0Task(Commit0Config(repo_name=repo))
        expected_image = f"docker.io/wentingzhao/{repo}:v0"
        assert t.get_docker_image() == expected_image, f"{repo}: {t.get_docker_image()}"
        assert t.get_work_dir() == f"/workspace/{repo}_repo"
        print(
            f"  {repo:15s} -> image={t.get_docker_image()}, work_dir={t.get_work_dir()}"
        )

    # 5. task_data should be None before load
    assert task.task_data is None

    print("\nAll checks passed!")
