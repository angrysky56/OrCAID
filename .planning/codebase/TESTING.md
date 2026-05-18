# Testing

**Analysis Date:** 2026-05-18

This document outlines the testing specifications, mocking paradigms, test runner configurations, and manual validation strategies for OrCAID.

---

## 1. Primary Testing Strategy

OrCAID relies on a modular, asynchronous execution system. Because the codebase involves extensive external API calls (LLMs) and container orchestration (Docker/Worktrees), the test suite divides testing into three main areas:

### A. Unit Testing
- **Target:** Mixins (`GitMixin`, `AssignmentMixin`, `ExplorationMixin`), helper functions, configuration validation schemas (`WorkflowConfig`, `SubAgentResult` in `config.py`), and standard utilities.
- **Execution:** Pure offline execution. No actual git worktree commands, Docker containers, or LLM network connections are triggered.
- **Mechanism:** Standard `unittest.mock` configurations to mock subprocess execution and API endpoints.

### B. Integration & Verification Testing
- **Target:** Closed-loop verification bridges (`orcaid_verification_bridge.py`), task loaders, and dynamic module loading.
- **Execution:** Runs checks using local mock data files to confirm that retry schedules, metrics sweeps, and escalation paths execute correctly.
- **Mocking Boundary:** Simulates `SubAgentResult` inputs to evaluate verification scoring algorithms.

### C. Live End-to-End Task Validation
- **Target:** Real task execution (Commit0 coding or Paperbench research reproduction).
- **Execution:** Triggered via `run_infer.py` with mock or tiny task samples. Requires active LLM API keys and local Docker socket access.
- **Performance Constraints:** Long-running. Monitored closely for API cost accumulation.

---

## 2. Test Execution Commands

To execute tests within the local `uv` virtual environment, run:

```bash
# Run the entire test suite
uv run pytest

# Run tests with verbose output
uv run pytest -v

# Run only unit tests
uv run pytest tests/unit/

# Run tests and generate a coverage report
uv run pytest --cov=core --cov=tasks tests/
```

---

## 3. Mocking Conventions & Boundaries

To keep tests predictable, fast, and cost-free, the following mocking strategies must be used:

### A. Mocking LLM Integrations (LiteLLM)
- **Target:** `litellm.completion`
- **Pattern:** Mock completion requests using predefined mock dict structures containing expected response fields.
- **Example:**
  ```python
  from unittest.mock import patch

  @patch("litellm.completion")
  def test_manager_llm_call(mock_completion):
      mock_completion.return_value = {
          "choices": [{"message": {"content": "Task decomposition text..."}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 20}
      }
      # Trigger manager function and assert response content
  ```

### B. Mocking Git Commands & Subprocesses (`GitMixin`)
- **Target:** `subprocess.run` inside `core/manager_git.py`
- **Pattern:** Mock the return value of `subprocess.run` to simulate clean branch creation, commits, stash states, and worktree checkout runs.
- **Example:**
  ```python
  @patch("subprocess.run")
  def test_git_branch_creation(mock_run):
      mock_run.return_value.returncode = 0
      mock_run.return_value.stdout = b"Created branch feature/engineer_1"
      # Execute branch helper and check subprocess arguments
  ```

### C. Mocking Docker & Workspace Contexts (OpenHands SDK)
- **Target:** `DockerWorkspace` and `DockerDevWorkspace`
- **Pattern:** Mock container initialization, shell commands execution, and state hooks to prevent starting actual local containers.

---

## 4. Manual Verification Paradigm

For validating system modifications without writing comprehensive tests, use standard dry-run pipelines:

```bash
# Trigger a dry-run analysis and decomposition without running parallel subagents
uv run run_infer.py --task-type commit0 --model claude-3-5-sonnet --dry-run
```
