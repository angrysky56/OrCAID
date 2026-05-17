# OrCAID + Meta-Harness Integration Design

> **Status:** DESIGN ONLY — Do not implement
> **Version:** 1.0
> **Date:** 2026-05-16

---

## 1. Overview

This document describes how to integrate the **OrCAID multi-agent framework** (which uses git worktrees for parallel subagent execution) with the **meta-harness** (which uses `claude --no-input` as the subagent LLM). The integration replaces OpenHands LLM calls with Claude Code invocations in git worktrees, while preserving the Manager's orchestration role.

### Goals
- Enable OrCAID to use Claude Code as the subagent executor via `claude --no-input`
- Preserve the Manager's scan → delegate → onboard → parallel-execute → merge workflow
- Allow Hermes (Ty) to remain the "brain" for manager-level decisions
- Maintain the git worktree isolation model that makes OrCAID robust

---

## 2. Current Architecture Analysis

### 2.1 SubAgentRunner (core/subagent.py)

The `SubAgentRunner` class currently invokes the LLM via **OpenHands SDK**:

```python
# core/subagent.py — lines 62-78
def setup(self):
    tools = get_default_tools(enable_browser=False)
    self.agent = Agent(
        llm=self.llm,           # OpenHands LLM instance
        tools=tools,
        system_prompt_kwargs={"cli_mode": True},
    )
    self.conversation = Conversation(
        agent=self.agent,
        workspace=self.workspace,
        max_iteration_per_run=self.max_iterations,
        visualizer=PanelVisualizer(),
    )
```

The LLM is injected at construction time (line 20):
```python
def __init__(self, llm, workspace, subagent, prompts, task_module, ...):
    self.llm = llm
```

**Key observation:** The `llm` parameter is an OpenHands `LLM` object. Replacing it means replacing the entire `run()` method body, which uses `conversation.send_message(prompt)` and `conversation.run()`.

### 2.2 SubAgentRunner.run() — The Core Execution Loop

```python
# lines 108-235
def run(self):
    # ...
    self.conversation.send_message(prompt)   # line 147
    self.conversation.run()                  # line 148
    # ... result extraction via get_commit_info(), etc.
```

The method also:
- Handles retry logic (lines 137-165)
- Extracts commit info via git commands (lines 241-257)
- Tracks metrics (cost, tokens, iterations)

### 2.3 Manager (core/manager.py) — Key Orchestration Points

The Manager handles:
1. **scan_and_analyze()** — LLM analyzes repository/paper
2. **delegate_tasks()** — LLM creates delegation plan
3. **onboard_subagents()** — Creates git worktrees, branches
4. **collect_and_merge()** — Merges subagent work into main repo
5. **assign_task()** — LLM decides next task assignment (called per-round)
6. **final_review_all()** — Manager reviews all results
7. **Background exploration** — Parallel LLM reasoning about remaining tasks

### 2.4 TaskModule Interface (tasks/base.py)

The `TaskModule` ABC defines the contract:
- `get_docker_image()`, `get_work_dir()`, `get_workspace_config()`
- `load_task_data()`, `setup_workspace()`, `evaluate()`
- `build_subagent()`, `get_worktree_name()`
- `create_subagent_result()`, `populate_success_result()`
- `get_conflict_instruction_args()`, `get_auto_reassign_instruction_args()`

Two concrete implementations exist: `Commit0Task` and `PaperbenchTask`.

---

## 3. Integration Design

### 3.1 Architecture: Replacing OpenHands LLM with Claude Code

The key insight is that **OrCAID's value is the worktree management, not the OpenHands agent**. The integration replaces only the LLM call portion:

```
BEFORE: Manager → OpenHands Agent → LLM API (e.g., GPT-4)
AFTER:  Manager → SubAgentRunner → claude --no-input (in worktree)
```

The Manager and TaskModule remain unchanged. Only `SubAgentRunner.run()` is modified.

### 3.2 Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OrCAID + Meta-Harness                             │
└─────────────────────────────────────────────────────────────────────────────┘

1. MANAGER SCA-AN-DEL:GATE
   Ty/Hermes (OpenHands LLM) → scan repo → delegation plan → git worktrees

2. WORKTREE CREATION (existing OrCAID flow)
   Manager → git worktree add /workspace/engineer_1 → branch feature/engineer_1

3. SUBAGENT EXECUTION (REPLACED PATH)
   ┌──────────────────────────────────────────────────────────────────────┐
   │  SubAgentRunner.run()                                               │
   │                                                                      │
   │  BEFORE (OpenHands):                                                │
   │    self.conversation.send_message(prompt)                            │
   │    self.conversation.run()                                           │
   │                                                                      │
   │  AFTER (Claude Code):                                               │
   │    Execute Claude Code in worktree with instruction prompt:         │
   │    $ claude --no-input --input-dir={worktree} --systemrompt={...}   │
   │                                                                      │
   │  Parse result:                                                       │
   │    - commit hash (from git log in worktree)                         │
   │    - files modified (from git diff)                                │
   │    - exit code (success/failure)                                   │
   │  Construct SubAgentResult equivalent                                │
   └──────────────────────────────────────────────────────────────────────┘

4. MANAGER COLLECT-AND-MERGE
   Manager → git merge feature/engineer_1 → main repo

5. MANAGER ASSIGN_TASK (Hermes decision)
   Ty/Hermes → decide next assignment → returns to step 2 or 4

6. REPEAT until all tasks complete
```

### 3.3 Claude Code Invocation Wrapper

**File:** `core/claude_runner.py` (NEW)

```python
import subprocess
import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List

@dataclass
class ClaudeResult:
    """Structured result from claude --no-input execution."""
    success: bool
    commit_hash: str          # 8-char short hash
    commit_message: str
    files_modified: List[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float

class ClaudeRunner:
    """Invokes claude --no-input in a git worktree as a subagent executor."""

    def __init__(
        self,
        worktree_path: str,
        instruction: str,
        max_duration: int = 600,
        claude_path: str = "claude",
    ):
        self.worktree_path = worktree_path
        self.instruction = instruction
        self.max_duration = max_duration
        self.claude_path = claude_path

    def run(self) -> ClaudeResult:
        """
        Execute claude --no-input in the worktree.

        Exact command format:
          claude --no-input \
            --output-format stream-json \
            --max-duration 600 \
            --system-prompts <(echo '{system_prompt}') \
            <(echo '{user_instruction}')

        The system prompt defines the agent's behavior (coding, git etiquette, etc.)
        The user instruction is the per-task prompt from OrCAID.
        """
        import time
        start = time.time()

        # Build the command
        cmd = [
            self.claude_path,
            "--no-input",
            "--output-format", "stream-json",
            "--max-duration", str(self.max_duration),
        ]

        # System prompt for coding agent (minimal, worktree-aware)
        system_prompt = self._build_system_prompt()
        # User instruction for this task
        user_instruction = self.instruction

        # Use process substitution for stdin input
        import tempfile
        import os

        # Create temp files for prompts (avoid /dev/stdin issues with PTY)
        with tempfile.NamedTemporaryFile(mode='w', suffix='_system', delete=False) as sf:
            sf.write(system_prompt)
            system_file = sf.name

        with tempfile.NamedTemporaryFile(mode='w', suffix='_user', delete=False) as uf:
            uf.write(user_instruction)
            user_file = uf.name

        try:
            # Pipe both system and user prompts via process substitution
            proc = subprocess.Popen(
                f"cat {system_file} | {self.claude_path} --no-input --output-format stream-json --max-duration {self.max_duration}",
                shell=True,
                cwd=self.worktree_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stdout, stderr = proc.communicate(timeout=self.max_duration + 30)
            exit_code = proc.returncode

        finally:
            os.unlink(system_file)
            os.unlink(user_file)

        duration = time.time() - start

        # Parse commit info from worktree
        commit_info = self._get_commit_info()
        files_modified = self._get_modified_files()

        return ClaudeResult(
            success=(exit_code == 0 and bool(commit_info["hash"])),
            commit_hash=commit_info["hash"],
            commit_message=commit_info["message"],
            files_modified=files_modified,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_seconds=duration,
        )

    def _build_system_prompt(self) -> str:
        """Minimal system prompt for coding agent."""
        return """You are a coding agent working in an isolated git worktree.

RULES:
- Make your changes in the current directory (the worktree).
- Always commit your changes before finishing: `git add -A && git commit -m "description"`
- Never push or interact with remote repositories.
- If you need to install dependencies, use appropriate package managers.
- Keep commits focused and atomic.
- Exit when you have completed the task or hit your iteration limit.
"""

    def _get_commit_info(self) -> dict:
        """Get the latest commit info from the worktree."""
        import subprocess
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%s|%an"],
            cwd=self.worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split("|", 2)
            return {
                "hash": parts[0][:8] if len(parts) > 0 else "",
                "message": parts[1] if len(parts) > 1 else "",
                "author": parts[2] if len(parts) > 2 else "",
            }
        return {"hash": "", "message": "", "author": ""}

    def _get_modified_files(self) -> List[str]:
        """Get list of files modified since HEAD."""
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=self.worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return []
```

### 3.4 Modified SubAgentRunner (core/subagent.py)

**Changes to `SubAgentRunner.__init__`:** Accept an optional `executor_mode` parameter:
```python
def __init__(
    self,
    llm,                    # Kept for Manager compatibility, may be None for claude mode
    workspace,
    subagent,
    prompts,
    task_module,
    max_iterations=50,
    max_rounds_chat=2,
    output_dir=None,
    output_logger=None,
    executor_mode="openhands",  # NEW: "openhands" or "claude"
    claude_runner=None,        # NEW: injected ClaudeRunner instance
):
```

**Changes to `SubAgentRunner.run()`:** Branch on executor_mode:
```python
def run(self):
    result = self.create_result()
    start_time = datetime.now()
    result.start_time = start_time.isoformat()

    self.log("Starting implementation...")

    try:
        if self.executor_mode == "claude":
            # NEW PATH: Use Claude Code
            claude_result = self._run_with_claude()
            result = self._convert_claude_result(claude_result)
        else:
            # EXISTING PATH: OpenHands
            result = self._run_with_openhands()

    except Exception as e:
        result.success = False
        result.error = str(e)

    # ... rest unchanged (metrics, event logging, etc.)
    return result

def _run_with_claude(self) -> ClaudeResult:
    """Execute task using claude --no-input in the worktree."""
    if not self.claude_runner:
        raise RuntimeError("claude_runner required when executor_mode='claude'")

    # Build instruction from prompt
    if self.subagent.current_round == 1:
        prompt = self.build_first_round_prompt()
    else:
        prompt = self.build_followup_prompt()

    # Update instruction on subagent
    self.subagent.instruction = prompt

    # Execute
    self.claude_runner.instruction = prompt
    return self.claude_runner.run()

def _convert_claude_result(self, cr: ClaudeResult) -> SubAgentResult:
    """Convert ClaudeResult to SubAgentResult."""
    result = self.create_result()
    result.success = cr.success
    result.commit_hash = cr.commit_hash
    result.commit_message = cr.commit_message
    result.files_modified = cr.files_modified
    result.error = cr.stderr if not cr.success else None
    return result
```

---

## 4. New Files to Create

### 4.1 `tasks/metai2rness.py` — Meta-Harness Task Module

This is the task module for the meta-harness domain. It implements `TaskModule` and provides the bridge between OrCAID's orchestration and the meta-harness's specific requirements.

```python
# tasks/metai2rness.py

from .base import TaskModule
from config import SubAgent, SubAgentResult, WorkflowConfig
from typing import List

class MetaHarnessTask(TaskModule):
    """
    Task module for meta-harness domain.

    The meta-harness is a testing framework that uses Claude Code as subagent
    executor. This module tells OrCAID how to:
    - Create worktrees for meta-harness test runners
    - Format instructions for claude --no-input execution
    - Evaluate results (pass/fail of test suite)
    - Handle meta-harness-specific conflict resolution
    """

    def get_docker_image(self):
        # Meta-harness needs a docker image capable of running claude
        return "ubuntu:22.04"  # or custom image with claude installed

    def get_work_dir(self):
        return "/workspace/metai2rness"

    def get_workspace_config(self):
        return {
            "base_image": "ubuntu:22.04",
            "target": "source",
        }

    def load_task_data(self):
        # Load meta-harness test definitions
        pass

    def setup_workspace(self, workspace):
        # Install claude, checkout meta-harness repo, etc.
        pass

    def evaluate(self, workspace):
        # Run meta-harness test suite, return results
        return {"passed": True, "score": 1.0}

    def get_prompt_format_args(self, config):
        return {}

    # ---- SubAgent integration ----

    def create_subagent_result(self, subagent) -> SubAgentResult:
        return SubAgentResult(
            engineer_id=subagent.engineer_id,
            task_id=subagent.task_id,
            branch_name=subagent.branch_name,
            worktree_path=subagent.worktree_path,
        )

    def get_followup_prompt_args(self, subagent) -> dict:
        return {
            "engineer_id": subagent.engineer_id,
            "task_id": subagent.task_id,
        }

    def get_run_start_log_lines(self, subagent) -> List[str]:
        return [
            f"Running meta-harness task: {subagent.task_id}",
            f"Worktree: {subagent.worktree_path}",
        ]

    def populate_success_result(self, result, runner, commit_info):
        result.commit_hash = commit_info.get("hash", "")
        result.commit_message = commit_info.get("message", "")

    def get_event_serialization_extras(self, subagent) -> dict:
        return {}

    def get_print_summary_lines(self, result, commit_info) -> List[str]:
        return [
            f"Files: {', '.join(result.files_modified or [])}",
        ]

    def get_new_task_print_lines(self, subagent) -> List[str]:
        return [f"Task: {subagent.task_id}"]

    def get_onboard_names(self, engineer_id: str):
        branch_name = f"feature/{engineer_id}"
        worktree_name = f"metai2rness_{engineer_id}"
        return branch_name, worktree_name

    def get_completion_print_lines(self, result) -> List[str]:
        status = "SUCCESS" if result.success else "FAILED"
        return [f"{result.engineer_id}: {status}"]

    def get_log_agent_response_kwargs(self, result) -> dict:
        return {
            "engineer_id": result.engineer_id,
            "task_id": result.task_id,
            "success": result.success,
            "error": result.error,
            "round_num": result.round_num,
        }

    def get_conflict_instruction_args(self, subagent, conflict_files, workspace, repo_dir) -> dict:
        return {
            "engineer_id": subagent.engineer_id,
            "conflict_files": ", ".join(conflict_files),
        }

    def get_execution_summary_lines(self, results) -> List[str]:
        passed = sum(1 for r in results if r.success)
        return [f"Meta-harness: {passed}/{len(results)} tasks passed"]
```

### 4.2 `core/claude_runner.py` — Claude Code Invoker

See Section 3.3 above for full implementation.

### 4.3 `core/subagent_claude.py` — Claude-aware SubAgentRunner

Alternative: Subclass `SubAgentRunner` as `ClaudeSubAgentRunner` to avoid modifying the existing class:

```python
# core/subagent_claude.py

from core.subagent import SubAgentRunner
from core.claude_runner import ClaudeRunner

class ClaudeSubAgentRunner(SubAgentRunner):
    """SubAgentRunner that uses claude --no-input instead of OpenHands."""

    def __init__(self, worktree_path, **kwargs):
        super().__init__(llm=None, **kwargs)  # llm not used
        self.worktree_path = worktree_path
        self.claude_runner = None  # initialized in run()

    def setup(self):
        # No OpenHands setup needed
        self.instruction_time = datetime.now()
        self.last_saved_event_count = 0
        self.log("Claude subagent ready")

    def run(self):
        # Same as SubAgentRunner.run() but using ClaudeRunner
        # ... (see Section 3.4)
```

---

## 5. Files to Modify

### 5.1 `core/subagent.py`

| Change | Location | Description |
|--------|----------|-------------|
| Add `executor_mode` param | `__init__` | Accept "openhands" or "claude" |
| Add `claude_runner` param | `__init__` | Inject ClaudeRunner instance |
| Add `_run_with_claude()` | New method | Execute claude, return ClaudeResult |
| Add `_convert_claude_result()` | New method | Map ClaudeResult → SubAgentResult |
| Modify `run()` | `run()` method | Branch on executor_mode |
| Modify `run_async()` | `run_async()` | Works unchanged (calls run()) |

**Lines to change:** ~40 lines modified, ~30 lines added

### 5.2 `run_infer.py`

| Change | Location | Description |
|--------|----------|-------------|
| Import MetaHarnessTask | Line ~33 | `from tasks.metai2rness import MetaHarnessTask` |
| Add `metai2rness` task option | `run_workflow_inner()` | `elif task == "metai2rness": task_module = MetaHarnessTask(...)` |
| Conditional subagent setup | SubAgentRunner instantiation | Pass `executor_mode="claude"` when using MetaHarnessTask |

### 5.3 `config.py` — Optional

May need to add new config fields if meta-harness has unique requirements (e.g., `claude_path`, `max_duration`).

### 5.4 `core/manager.py` — Minimal Changes

The Manager does **not** need major changes because:
- `scan_and_analyze()`, `delegate_tasks()`, `assign_task()` all use the Manager's own LLM (OpenHands/Ty), not subagents
- `collect_and_merge()` operates on git branches, not on how subagents ran
- `onboard_subagents()` creates worktrees the same way regardless of executor

**However**, one change may be needed:
- If meta-harness tasks are very different from commit0/paperbench, `build_completed_task_summary()` may need adjustment in the MetaHarnessTask module

---

## 6. Hermes Integration Points

### 6.1 Where OrCAID Calls Hermes (Manager-level decisions)

The Manager calls its own LLM (which IS Hermes) at these points:

1. **scan_and_analyze()** — Hermes analyzes the repo/paper
2. **delegate_tasks()** — Hermes creates the delegation plan
3. **assign_task()** — Hermes decides next task assignment per round
4. **final_review_all()** — Hermes reviews all subagent results

Currently these all use the same OpenHands `LLM` instance (`self.llm`). The integration does NOT change this — Hermes remains the Manager's brain.

### 6.2 How Hermes Gets Involved in Meta-Harness

For meta-harness, Hermes would:
1. **Analyze** the test harness definition file (YAML/JSON specifying tests)
2. **Delegate** tests to subagents (one subagent per test, or grouped)
3. **Assign** work to idle engineers as they complete tests
4. **Review** whether all tests pass, determine if retry needed

### 6.3 Hermes as External Coordinator

If Hermes runs **outside** OrCAID (as the parent agent), the integration looks like:

```
Hermes (parent, outside OrCAID)
  │
  ├─ Calls OrCAID: "Run metai2rness task"
  │    └─ OrCAID: scan → delegate → onboard → run subagents
  │         └─ Subagents: use claude --no-input in worktrees
  │
  └─ OrCAID returns results to Hermes
       └─ Hermes decides: retry failed tests, merge, evaluate
```

In this model, Hermes does NOT need to call back into OrCAID mid-execution. The current `run_subagents_parallel` already handles reassignment via `manager.assign_task()`.

---

## 7. Claude Code Invocation — Exact Command Format

### 7.1 Core Invocation

```bash
claude --no-input \
  --output-format stream-json \
  --max-duration 600 \
  --system-prompts /tmp/claude_system_XXXX.txt \
  /tmp/claude_user_XXXX.txt
```

**Args:**
- `--no-input` — Non-interactive, reads from stdin/files
- `--output-format stream-json` — JSON lines output for parsing
- `--max-duration` — Safety timeout (seconds)
- `--system-prompts` — Path to system prompt file
- Final positional arg — Path to user instruction file

### 7.2 Environment Requirements

The worktree must have:
- `claude` binary in PATH
- Git repo initialized with user config:
  ```bash
  git config user.name "orcaid-agent"
  git config user.email "orcaid@local"
  ```
- Write access to worktree directory

### 7.3 Alternative: Inline System Prompt

```bash
claude --no-input \
  --output-format stream-json \
  --max-duration 600 \
  --system-prompts <(echo '{system_prompt_content}') \
  /tmp/user_instruction.txt
```

Process substitution `<(...)` avoids temp files but requires bash.

---

## 8. Edge Cases and Error Handling

### 8.1 Claude Code Timeout
- If `claude --no-input` exceeds `--max-duration`, it is killed
- Treat as failure: `result.success = False`, `result.error = "timeout"`

### 8.2 No Commit Made
- If worktree has no new commits after Claude runs:
  ```python
  if not commit_info["hash"]:
      result.success = False
      result.error = "No new commit was made. Claude may have exited early."
  ```

### 8.3 Merge Conflicts
- Existing conflict resolution logic in `SubAgentRunner` (via `run_subagents_parallel`) already handles this
- Engineer gets reassigned to resolve conflicts in their worktree

### 8.4 Worktree Already Exists
- Git worktree add fails if worktree directory already exists
- Handle by checking and reusing: `git worktree list` → find existing

---

## 9. Summary of Changes

### New Files
| File | Purpose |
|------|---------|
| `tasks/metai2rness.py` | TaskModule for meta-harness |
| `core/claude_runner.py` | Claude Code invocation wrapper |
| `core/subagent_claude.py` | (Optional) Claude-aware SubAgentRunner subclass |

### Modified Files
| File | Changes |
|------|---------|
| `core/subagent.py` | Add executor_mode, claude_runner, _run_with_claude(), modify run() |
| `run_infer.py` | Add metai2rness task type, conditional executor_mode |
| `config.py` | (Optional) Add meta-harness config fields |

### No Changes Needed
- `core/manager.py` — Works unchanged (uses its own LLM)
- `tasks/base.py` — Interface unchanged
- `prompts/*.yaml` — May need meta-harness prompts (TBD)

---

## 10. Open Questions

1. **System prompt content**: What should the minimal coding agent system prompt contain?
2. **Output format**: Should we use `stream-json` or simpler plain text + parse exit code?
3. **Claude binary location**: Default to `claude` in PATH or allow config override?
4. **Result parsing**: Should we parse Claude's stdout for structured data or rely solely on git?
5. **Multi-round behavior**: How does Claude handle follow-up prompts in round 2?

---

*End of integration design.*