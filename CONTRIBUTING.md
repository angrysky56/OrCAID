# Contributing

Thank you for your interest in contributing to OrCAID! This guide covers the
development workflow, coding standards, and contribution process.

---

## Prerequisites

- **Python 3.12+**
- **uv** — package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **Docker** — required for subagent execution sandboxes
- **Git** — worktree support (Git 2.5+)

---

## Development Setup

```bash
# Clone the repository
git clone https://github.com/angrysky56/OrCAID.git
cd OrCAID

# Create virtual environment
uv venv

# Activate environment
source .venv/bin/activate

# Install dependencies (including dev tools)
uv pip install -e ".[dev]"

# Copy environment template
cp .env.example .env
# Edit .env with your API keys
```

---

## Project Structure

```
OrCAID/
├── orcaid/              # Main package
│   ├── cli.py           # CLI entrypoint
│   ├── config.py        # Dataclasses
│   ├── bridge.py        # Verification bridge
│   ├── core/            # Manager + SubAgentRunner
│   └── tasks/           # Task implementations
├── prompts/             # YAML prompt templates
├── scripts/             # Shell run wrappers
├── tests/               # Test suite
├── AGENTS.md            # Agent architecture guide
├── ARCHITECTURE.md      # System architecture
├── API.md               # Public API reference
├── CONFIGURATION.md     # Configuration reference
├── SETUP.md             # Setup and deployment guide
└── pyproject.toml       # Package metadata
```

---

## Coding Standards

### Python Style

- Follow **PEP 8** with a 100-character line length.
- Use **type hints** on all function signatures.
- Use **docstrings** on all public classes and methods.
- Prefer `f-strings` over `.format()` or `%` for inline string building.
- Use `from __future__ import annotations` sparingly; prefer native `X | None` syntax.

### Linting

```bash
# Run ruff for linting
uv run ruff check orcaid/

# Auto-fix issues
uv run ruff check --fix orcaid/
```

### Testing

```bash
# Run the test suite
uv run pytest tests/ -v

# Run a specific test
uv run pytest tests/test_bridge_sweeps.py::test_run_indexer_sweep -v
```

All changes must pass the existing test suite before merge.

---

## Commit Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
feat: add self-improve task module
fix: resolve MiniMax provider routing for anthropic endpoint
refactor: extract GitMixin from Manager class
docs: update ARCHITECTURE.md with isolation model
test: add indexer sweep aggregation test
chore: bump litellm to 1.81.11
```

---

## Contribution Workflow

1. **Fork** the repository and create a feature branch:
   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Make changes** following the coding standards above.

3. **Add tests** for any new functionality.

4. **Run the test suite** to ensure nothing is broken:
   ```bash
   uv run pytest tests/ -v
   ```

5. **Commit** with a conventional commit message.

6. **Push** and open a Pull Request against `main`.

---

## Adding a New Task Type

This is the most common extension. Follow these steps:

1. **Create the task module** — `orcaid/tasks/your_task.py`
   - Subclass `TaskModule` and implement all abstract methods.
   - Create a companion config dataclass (e.g., `YourTaskConfig`).

2. **Create prompt templates** — `prompts/your_task.yaml`
   - Define keys: `scan_analyze`, `onboard`, `assign_task`, `followup`,
     `manager_final_review_all`, `background_exploration`.

3. **Register the task** — `orcaid/tasks/__init__.py`
   - Import and add to `__all__`.

4. **Wire into CLI** — `orcaid/cli.py`
   - Add task selection logic in the argument parsing section.

5. **Add tests** — `tests/test_your_task.py`

See `orcaid/tasks/commit0.py` as a reference implementation.

---

## Adding a Verification Checklist

1. Define the checklist function in `orcaid/bridge.py`.
2. Register it in the `TASK_MODULE_TO_CHECKLIST` mapping.
3. Add tests in `tests/test_bridge_sweeps.py`.

---

## Reporting Issues

When filing issues, please include:

- OrCAID version (`python -c "import orcaid; print(orcaid.__version__)"`)
- Python version (`python --version`)
- Task type and model used
- Relevant log output or traceback
- Steps to reproduce

---

## License

OrCAID is released under the MIT License. By contributing, you agree that your
contributions will be licensed under the same terms.
