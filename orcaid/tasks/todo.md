# Phase 1: Repo Structure, Configuration, and Packaging - Tasks Todo

## 1. Repository Restructuring
- [x] Create an `orcaid/` top-level directory to serve as the main Python package.
- [x] Move `run_infer.py` to `orcaid/cli.py`.
- [x] Move `config.py` to `orcaid/config.py`.
- [x] Move `orcaid_verification_bridge.py` to `orcaid/bridge.py`.
- [x] Move `core/` to `orcaid/core/`.
- [x] Move `tasks/` to `orcaid/tasks/`.
- [x] Move `judge/` to `orcaid/judge/`.
- [x] Update all import references across the codebase to point to the new `orcaid.*` structure.

## 2. LiteLLM MiniMax Connection Fixes
- [x] Review `orcaid/core/utils.py` and `orcaid/cli.py` for LLM kwargs building logic, particularly around `MiniMax-M2.7`.
- [x] Diagnose connection/routing failures (e.g., missing headers, improper base URL, or custom provider config).
- [x] Implement robust error handling and ensure the LiteLLM configuration for `MiniMax-M2.7` routes successfully.

## 3. Docker and OpenHands SDK Fixes
- [x] Investigate Docker build context issues and checksum failures originating from OpenHands SDK integration.
- [x] Eliminate hardcoded SDK directory paths (like `orcaid-sdk-root`).
- [x] Configure `uv` and the Python environment (Python 3.12) to cleanly resolve OpenHands dependencies without fragile workarounds.

## 4. Project Packaging
- [x] Update `pyproject.toml` to define `[project.scripts]` (e.g., `orcaid = "orcaid.cli:main"`).
- [x] Adjust `[tool.setuptools.packages.find]` to correctly include the new `orcaid` package and any submodules.
- [x] Update `SETUP.md` and `README.md` to reflect standard package installation (`uv pip install -e .` or similar) and the new CLI commands.

## 5. Review Code Modifications & Future Improvements
- [x] Audit the `orcaid/bridge.py` self-healing hooks for stability, making sure it doesn't degrade performance on failures.
- [x] Review the Manager's `_verify_and_return()` method integration.
- [x] Refactor and optimize any anti-patterns from previous modifications to lay a solid foundation for future extensions.
