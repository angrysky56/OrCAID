# Phase 2: Advanced Delegation and Indexer Sweep Optimizations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Elevate OrCAID's multi-agent coordination loop with high-signal historical feedback and automated self-healing sweep closing loops.

**Architecture:** Harden verification bridge logging, dynamically inject subagent profile and task type success/drift rates from `discovery.yaml` into manager delegation decisions, and provide a standalone CLI command/cron job `orcaid-verification-indexer` that aggregates outcomes and sweeps escalations.

**Tech Stack:** Python 3.12, PyYAML, Git, Pytest

---

### Task 1: Harden Drift Log Formatting and Structure
**Files:**
- Modify: `orcaid/bridge.py`
- Create: `tests/test_bridge_sweeps.py`

**Step 1: Write the failing test**
Create `tests/test_bridge_sweeps.py` and write `test_write_drift_log_formatting()` to verify that `write_drift_log` writes all required SubAgentResult metrics and structures the Git Diff in a details block.

```python
import tempfile
from pathlib import Path
import yaml
from orcaid.config import SubAgentResult
from orcaid.bridge import write_drift_log, ORCHESTRATOR_MEMORY_BASE

def test_write_drift_log_formatting():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        subagent_result = SubAgentResult(
            engineer_id="engineer_2",
            task_id="task_123",
            success=False,
            error="AssertionError: Test failed",
            duration_seconds=12.5,
            cost=0.045,
            actual_iterations=3,
            max_iterations=5,
            files_modified=["orcaid/core/manager.py"],
            git_diff="--- a/orcaid/core/manager.py\n+++ b/orcaid/core/manager.py\n@@ -1,1 +1,2 @@\n-old\n+new"
        )
        
        drift_log = [
            {
                "criterion_id": "commit_made",
                "failure_message": "No commit was made by the subagent",
                "category": "phase_skip",
                "severity": "high"
            }
        ]
        
        correction_context = {
            "task_type": "commit0",
            "task_id": "task_123",
            "attempt_number": 1,
            "original_requirements": "Implement function X",
            "instructions": "Fix commit_made failure"
        }
        
        log_path = write_drift_log(
            drift_log=drift_log,
            correction_context=correction_context,
            subagent_result=subagent_result,
            memory_base=tmp_path
        )
        
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        
        # Verify markdown elements and tables are present
        assert "| Metric | Value |" in content
        assert "engineer_2" in content
        assert "AssertionError: Test failed" in content
        assert "<details>" in content
        assert "</details>" in content
        assert "--- a/orcaid/core/manager.py" in content
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_bridge_sweeps.py::test_write_drift_log_formatting -v`
Expected: FAIL (or `tests/` directory doesn't exist yet, we'll configure it).

**Step 3: Write minimal implementation**
Harden `write_drift_log` in `orcaid/bridge.py` to format drift entries beautifully and construct a robust Markdown summary with an HTML collapsible git diff details block.

```python
def write_drift_log(
    drift_log: list[dict],
    correction_context: dict,
    subagent_result,
    memory_base: Optional[Path] = None,
) -> Path:
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE
    task_type = correction_context.get("task_type", "unknown")
    task_id = correction_context.get("task_id", "unknown")
    attempt = correction_context.get("attempt_number", 1)
    timestamp = datetime.now().isoformat()

    filename = f"{task_id}__{attempt}__{timestamp.replace(':', '-')}.md"
    log_dir = memory_base / "drift_logs" / task_type
    log_dir.mkdir(parents=True, exist_ok=True)

    drift_lines = []
    for entry in drift_log:
        drift_lines.append(
            f"### [{entry['severity'].upper()}] {entry['criterion_id']}\n"
            f"**Failure Message:** {entry['failure_message']}\n"
            f"**Category:** {entry['category']}\n"
        )
    drift_formatted = "\n".join(drift_lines)

    # Format git diff safely in details block
    diff_content = subagent_result.git_diff or "No git diff available."
    
    content = f"""---
drift_log: true
task_id: {task_id}
task_type: {task_type}
attempt: {attempt}
timestamp: {timestamp}
resolution: in_progress
engineer_id: {subagent_result.engineer_id}
---

## Drift Entries

{drift_formatted}

## Correction Context Sent

### Original Requirements
{correction_context.get('original_requirements', 'N/A')}

### Instructions
{correction_context.get('instructions', 'N/A')}

## Recovery Action

re_invoke_worker_with_correction

## SubAgent Result at Time of Drift

| Metric | Value |
| :--- | :--- |
| **Success** | `{subagent_result.success}` |
| **Engineer ID** | `{subagent_result.engineer_id}` |
| **Duration** | `{subagent_result.duration_seconds:.2f}s` |
| **Cost** | `${subagent_result.cost:.4f}` |
| **Actual Iterations** | `{subagent_result.actual_iterations} / {subagent_result.max_iterations}` |
| **Error** | `{subagent_result.error or "None"}` |
| **Files Modified** | `{subagent_result.files_modified}` |
| **Commit Hash** | `{subagent_result.commit_hash or "N/A"}` |

<details>
<summary><b>View Git Diff</b></summary>

```diff
{diff_content}
```

</details>
"""

    log_path = log_dir / filename
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Update discovery index
    _update_discovery_index(task_type, "failed", memory_base)

    return log_path
```

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_bridge_sweeps.py::test_write_drift_log_formatting -v`
Expected: PASS

**Step 5: Commit**
```bash
git add orcaid/bridge.py tests/test_bridge_sweeps.py
git commit -m "feat: harden drift log markdown formatting and test"
```

---

### Task 2: Refine Subagent Assignment Prompt Injection
**Files:**
- Modify: `orcaid/core/manager_assignment.py`
- Test: `tests/test_bridge_sweeps.py`

**Step 1: Write the failing test**
Add `test_assign_task_prompt_injection()` in `tests/test_bridge_sweeps.py` to verify that `AssignmentMixin.assign_task()` parses `discovery.yaml` and appends a clean historical summary to the manager prompt.

```python
from unittest.mock import MagicMock
from orcaid.core.manager_assignment import AssignmentMixin

class MockManager(AssignmentMixin):
    def __init__(self):
        super().__init__()
        self.config = MagicMock()
        self.config.max_rounds_chat = 3
        self.config.manager_max_iterations = 5
        self.prompts = {"assign_task": "Assign task to {engineer_id}"}
        self.conversation = MagicMock()
        self.conversation.state.events = []
        self.task = MagicMock()
        self.task.build_completed_task_summary.return_value = "Completed task details"
        
    def log(self, msg):
        pass

def test_assign_task_prompt_injection():
    # Setup mock manager
    mgr = MockManager()
    
    completed_result = MagicMock()
    completed_result.engineer_id = "engineer_2"
    completed_result.merged = True
    completed_result.success = True
    completed_result.round_num = 1
    completed_result.error = None
    
    # Run assign_task
    mgr.assign_task(
        completed_result=completed_result,
        all_completed=[],
        running_agents=[]
    )
    
    # Verify that the message sent includes historical statistics if discovery.yaml exists
    sent_prompt = mgr.conversation.send_message.call_args[0][0]
    assert "Assign task to engineer_2" in sent_prompt
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_bridge_sweeps.py::test_assign_task_prompt_injection -v`
Expected: FAIL (or loads and does not assert index data)

**Step 3: Write minimal implementation**
Modify `assign_task` in `orcaid/core/manager_assignment.py` to read the global `index/discovery.yaml`, format the multi-dimensional stats (globally per task_type and dynamically per engineer/role profile), and append to the formatted prompt before calling `send_message`.

```python
        # Load and inject historical performance from discovery.yaml
        history_summary = ""
        try:
            from orcaid.bridge import ORCHESTRATOR_MEMORY_BASE
            import yaml
            
            index_path = ORCHESTRATOR_MEMORY_BASE / "index" / "discovery.yaml"
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    index = yaml.safe_load(f) or {}
                
                parts = ["### Historical Subagent and Task-Type Performance Summary\n"]
                
                # Format task type stats
                task_types = index.get("task_types", {})
                if task_types:
                    parts.append("#### Global Task Type Performance:")
                    for t_type, stats in task_types.items():
                        comp = stats.get("total_completed", 0)
                        fail = stats.get("total_failed", 0)
                        rate = stats.get("drift_rate", 0.0)
                        parts.append(f"- **{t_type}**: {comp} verified, {fail} failed (Drift Rate: {rate:.1%})")
                    parts.append("")
                
                # Format profile stats
                profiles = index.get("profiles", {})
                if profiles:
                    parts.append("#### Subagent Profile Performance:")
                    profile_mapping = {
                        "engineer_1": "developer",
                        "engineer_2": "debugger",
                        "engineer_3": "researcher",
                        "engineer_4": "reviewer",
                    }
                    for prof, stats in profiles.items():
                        role = profile_mapping.get(prof, "coder")
                        comp = stats.get("total_completed", 0)
                        fail = stats.get("total_failed", 0)
                        rate = stats.get("drift_rate", 0.0)
                        parts.append(f"- **{prof}** ({role}):")
                        parts.append(f"  - Total: {comp} verified, {fail} failed (Drift Rate: {rate:.1%})")
                        
                        t_breakdown = stats.get("task_types", {})
                        if t_breakdown:
                            parts.append("  - Breakdowns:")
                            for tt, tt_stats in t_breakdown.items():
                                tt_comp = tt_stats.get("total_completed", 0)
                                tt_fail = tt_stats.get("total_failed", 0)
                                tt_rate = tt_stats.get("drift_rate", 0.0)
                                parts.append(f"    - **{tt}**: {tt_comp} verified, {tt_fail} failed (Drift Rate: {tt_rate:.1%})")
                    parts.append("")
                
                history_summary = "\n".join(parts)
        except Exception as e:
            self.log(f"Warning: Could not load historical statistics: {e}")

        if history_summary:
            prompt += f"\n\n{history_summary}"
```

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_bridge_sweeps.py::test_assign_task_prompt_injection -v`
Expected: PASS

**Step 5: Commit**
```bash
git add orcaid/core/manager_assignment.py
git commit -m "feat: dynamically inject discovery.yaml stats in AssignmentMixin"
```

---

### Task 3: Implement Standalone Indexer Sweep CLI Command
**Files:**
- Modify: `orcaid/bridge.py`
- Test: `tests/test_bridge_sweeps.py`

**Step 1: Write the failing test**
Add `test_run_indexer_sweep()` to `tests/test_bridge_sweeps.py` to verify that `run_indexer_sweep` sweeps the directories, aggregates outcomes per task_type and subagent profile, extracts unresolved escalations, computes drift rates, and overwrites `index/discovery.yaml`.

```python
from orcaid.bridge import run_indexer_sweep

def test_run_indexer_sweep():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # 1. Create a dummy verified outcome
        skills_dir = tmp_path / "skills" / "commit0"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "outcome_1.md").write_text("""---
verified_outcome: true
task_id: task_1
task_type: commit0
worker_profile: developer
engineer_id: engineer_1
---
Summary content
""", encoding="utf-8")
        
        # 2. Create a dummy drift log
        drift_dir = tmp_path / "drift_logs" / "commit0"
        drift_dir.mkdir(parents=True, exist_ok=True)
        (drift_dir / "drift_1.md").write_text("""---
drift_log: true
task_id: task_2
task_type: commit0
engineer_id: engineer_1
attempt: 1
---
Drift details
""", encoding="utf-8")
        
        # 3. Create a dummy escalation
        esc_dir = tmp_path / "escalations"
        esc_dir.mkdir(parents=True, exist_ok=True)
        with open(esc_dir / "escalate__task_3.yaml", "w", encoding="utf-8") as f:
            yaml.dump({
                "escalation": True,
                "task_id": "task_3",
                "engineer_id": "engineer_2",
                "resolution": "pending_human_review"
            }, f)
            
        # Run the sweep
        run_indexer_sweep(memory_base=tmp_path)
        
        # Check discovery.yaml contents
        index_path = tmp_path / "index" / "discovery.yaml"
        assert index_path.exists()
        
        with open(index_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            
        # Verify stats
        assert "task_types" in data
        assert "commit0" in data["task_types"]
        assert data["task_types"]["commit0"]["total_completed"] == 1
        assert data["task_types"]["commit0"]["total_failed"] == 1
        assert data["task_types"]["commit0"]["drift_rate"] == 0.5
        
        assert "profiles" in data
        assert "engineer_1" in data["profiles"]
        assert data["profiles"]["engineer_1"]["total_completed"] == 1
        assert data["profiles"]["engineer_1"]["total_failed"] == 1
        assert data["profiles"]["engineer_1"]["drift_rate"] == 0.5
        
        assert "escalations" in data
        assert len(data["escalations"]) == 1
        assert data["escalations"][0]["task_id"] == "task_3"
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_bridge_sweeps.py::test_run_indexer_sweep -v`
Expected: FAIL (or `run_indexer_sweep` not imported/implemented)

**Step 3: Write minimal implementation**
Implement `run_indexer_sweep` and `run_sweep_cli` in `orcaid/bridge.py`.

```python
def parse_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}

def run_indexer_sweep(memory_base: Optional[Path] = None):
    """
    Sweep verified skills, drift logs, and escalations to recalculate metrics.
    Rebuilds and overwrites index/discovery.yaml with full multi-dimensional stats.
    """
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE
    index_path = memory_base / "index" / "discovery.yaml"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    index = {
        "task_types": {},
        "profiles": {},
        "escalations": [],
        "last_sweep": datetime.now().isoformat()
    }

    # 1. Sweep verified outcomes (skills)
    skills_dir = memory_base / "skills"
    if skills_dir.exists():
        for md_file in skills_dir.glob("**/*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = parse_frontmatter(content)
                if fm.get("verified_outcome") or fm.get("synthesis"):
                    # Synthesize verified stats
                    task_type = fm.get("task_type", "unknown")
                    # If verified_outcome has engineer_id, use it, else default from worker_profile
                    profile_mapping_rev = {
                        "developer": "engineer_1",
                        "debugger": "engineer_2",
                        "researcher": "engineer_3",
                        "reviewer": "engineer_4",
                    }
                    engineer_id = fm.get("engineer_id")
                    if not engineer_id:
                        worker_profile = fm.get("worker_profile", "unknown")
                        engineer_id = profile_mapping_rev.get(worker_profile, "unknown")

                    # Initialize task_type in index
                    if task_type not in index["task_types"]:
                        index["task_types"][task_type] = {
                            "total_completed": 0,
                            "total_failed": 0,
                            "drift_rate": 0.0,
                            "last_verified": fm.get("timestamp"),
                            "last_outcome": "verified"
                        }
                    index["task_types"][task_type]["total_completed"] += 1

                    # Initialize profile in index
                    if engineer_id != "unknown":
                        if engineer_id not in index["profiles"]:
                            index["profiles"][engineer_id] = {
                                "total_completed": 0,
                                "total_failed": 0,
                                "drift_rate": 0.0,
                                "task_types": {}
                            }
                        index["profiles"][engineer_id]["total_completed"] += 1
                        
                        # Task breakdown within profile
                        prof_tasks = index["profiles"][engineer_id]["task_types"]
                        if task_type not in prof_tasks:
                            prof_tasks[task_type] = {
                                "total_completed": 0,
                                "total_failed": 0,
                                "drift_rate": 0.0
                            }
                        prof_tasks[task_type]["total_completed"] += 1
            except Exception:
                pass

    # 2. Sweep drift logs
    drift_dir = memory_base / "drift_logs"
    if drift_dir.exists():
        for md_file in drift_dir.glob("**/*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = parse_frontmatter(content)
                if fm.get("drift_log"):
                    task_type = fm.get("task_type", "unknown")
                    engineer_id = fm.get("engineer_id", "unknown")

                    if task_type not in index["task_types"]:
                        index["task_types"][task_type] = {
                            "total_completed": 0,
                            "total_failed": 0,
                            "drift_rate": 0.0,
                            "last_verified": fm.get("timestamp"),
                            "last_outcome": "failed"
                        }
                    index["task_types"][task_type]["total_failed"] += 1

                    if engineer_id != "unknown":
                        if engineer_id not in index["profiles"]:
                            index["profiles"][engineer_id] = {
                                "total_completed": 0,
                                "total_failed": 0,
                                "drift_rate": 0.0,
                                "task_types": {}
                            }
                        index["profiles"][engineer_id]["total_failed"] += 1

                        prof_tasks = index["profiles"][engineer_id]["task_types"]
                        if task_type not in prof_tasks:
                            prof_tasks[task_type] = {
                                "total_completed": 0,
                                "total_failed": 0,
                                "drift_rate": 0.0
                            }
                        prof_tasks[task_type]["total_failed"] += 1
            except Exception:
                pass

    # 3. Compute drift rates globally and per profile
    for task_type, stats in index["task_types"].items():
        total = stats["total_completed"] + stats["total_failed"]
        stats["drift_rate"] = stats["total_failed"] / total if total > 0 else 0.0

    for engineer_id, stats in index["profiles"].items():
        total = stats["total_completed"] + stats["total_failed"]
        stats["drift_rate"] = stats["total_failed"] / total if total > 0 else 0.0
        for tt, tt_stats in stats["task_types"].items():
            tt_total = tt_stats["total_completed"] + tt_stats["total_failed"]
            tt_stats["drift_rate"] = tt_stats["total_failed"] / tt_total if tt_total > 0 else 0.0

    # 4. Sweep escalations
    escalation_dir = memory_base / "escalations"
    if escalation_dir.exists():
        for esc_file in escalation_dir.glob("escalate_*.yaml"):
            try:
                with open(esc_file, "r", encoding="utf-8") as f:
                    esc = yaml.safe_load(f)
                if esc and esc.get("resolution") == "pending_human_review":
                    index["escalations"].append({
                        "task_id": esc.get("task_id"),
                        "engineer_id": esc.get("engineer_id"),
                        "timestamp": esc.get("timestamp"),
                        "verdict": esc.get("verdict"),
                        "resolution": "pending_human_review"
                    })
            except Exception:
                pass

    # 5. Overwrite discovery.yaml
    with open(index_path, "w", encoding="utf-8") as f:
        yaml.dump(index, f, default_flow_style=False)


def run_sweep_cli():
    """CLI entrypoint for orcaid-verification-indexer."""
    print("Running OrCAID Verification Indexer sweep...")
    run_indexer_sweep()
    print("Sweep complete. Rebuilt ~/.hermes/orchestrator-memory/index/discovery.yaml")
```

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_bridge_sweeps.py::test_run_indexer_sweep -v`
Expected: PASS

**Step 5: Commit**
```bash
git add orcaid/bridge.py
git commit -m "feat: implement full indexer sweep command and tests"
```

---

### Task 4: Packaging and Entrypoint Script Registration
**Files:**
- Modify: `pyproject.toml`

**Step 1: Register script in pyproject.toml**
Add the script under `[project.scripts]`.

```toml
orcaid-verification-indexer = "orcaid.bridge:run_sweep_cli"
```

**Step 2: Run verification**
Reinstall the package locally:
`uv pip install -e .`

Verify CLI runs:
`orcaid-verification-indexer --help` (or run it directly)
Expected: Prints "Running OrCAID Verification Indexer sweep..." and finishes.

**Step 3: Commit**
```bash
git add pyproject.toml
git commit -m "feat: register orcaid-verification-indexer script entrypoint"
```
