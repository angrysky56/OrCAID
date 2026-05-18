"""
OrCAID Verification Bridge
Integrates OrCAID execution engine with delegation-verification闭环.

Implements the three hook points:
  1. verify_subagent_result() - called after collect_and_merge builds review_result
  2. discovery_scan_for_orcaid() - called before scan_and_analyze()
  3. synthesize_orcaid_outcome() - called after final_review_all()

Wires:
  SubAgentResult → delegation-verification scoring → orchestrator-memory write
  → drift: re-invoke with correction context | pass: OrCAID continues
"""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Default paths — configurable via environment variables
#
# ORCHESTRATOR_MEMORY_BASE: where verified outcomes, drift logs, and the
#   discovery index live.  Set via env var to relocate; defaults to
#   ~/.orcaid/orchestrator-memory.
#
# BRIDGE_STORAGE: auxiliary storage for the verification bridge itself
#   (e.g. cached checklists).  Defaults to ~/.orcaid/bridge.
# ---------------------------------------------------------------------------

_DEFAULT_ORCAID_HOME = Path.home() / ".orcaid"


def get_memory_base() -> Path:
    """Return the orchestrator-memory root, resolved once from env."""
    return Path(
        os.environ.get(
            "ORCHESTRATOR_MEMORY_BASE",
            str(_DEFAULT_ORCAID_HOME / "orchestrator-memory"),
        )
    )


def get_bridge_storage() -> Path:
    """Return the bridge storage root, resolved once from env."""
    return Path(
        os.environ.get(
            "ORCAID_BRIDGE_STORAGE",
            str(_DEFAULT_ORCAID_HOME / "bridge"),
        )
    )


# Module-level constants kept for backward compatibility — callers that
# imported ORCHESTRATOR_MEMORY_BASE directly will still work, but the
# canonical way is to call get_memory_base().
ORCHESTRATOR_MEMORY_BASE = get_memory_base()
BRIDGE_STORAGE = get_bridge_storage()

ORCAID_TO_HERMES_PROFILE = {
    "engineer_1": "coder",
    "engineer_2": "coder",
    "engineer_3": "researcher",
    "engineer_4": "reviewer",
}


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class VerificationResult:
    """Result of scoring a SubAgentResult against a checklist."""

    verdict: str  # "pass" | "fail" | "escalate"
    drift_log: list[dict]
    verified_outcome: Optional[dict] = None
    retry_recommended: bool = False
    correction_context: Optional[dict] = None
    escalate: bool = False
    attempt_number: int = 1


@dataclass
class DriftEntry:
    """A single drift detection from a failed pass criterion."""

    criterion_id: str
    criterion_description: str
    check_result: dict
    category: str  # phase_skip, criteria_mismatch, etc.
    severity: str  # critical, high, medium, low
    failure_message: str


# =============================================================================
# Checklist Loading
# =============================================================================


def load_checklist(task_type: str, checklist_base: Optional[Path] = None) -> dict:
    """
    Load the verification checklist for a task type.
    Checks orchestrator-memory first, falls back to bridge's own checklists.
    """
    checklist_base = checklist_base or ORCHESTRATOR_MEMORY_BASE

    # Try orchestrator-memory/skills/{task_type}/checklist.yaml
    om_checklist = checklist_base / "skills" / task_type / "checklist.yaml"
    if om_checklist.exists():
        with open(om_checklist, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # Fall back to bridge's own reference checklists
    bridge_checklist = (
        Path(__file__).parent / "checklists" / f"checklist_{task_type}.yaml"
    )
    if bridge_checklist.exists():
        with open(bridge_checklist, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # Default: code_review checklist
    return {
        "task_type": task_type,
        "version": "1.0.0",
        "pass_criteria": [
            {
                "id": "commit_made",
                "description": "Subagent made at least one new commit",
                "required": True,
                "weight": 10,
            },
            {
                "id": "files_modified",
                "description": "Expected files modified",
                "required": True,
                "weight": 8,
            },
            {
                "id": "no_conflicts",
                "description": "No merge conflicts",
                "required": True,
                "weight": 8,
            },
            {
                "id": "success_flag",
                "description": "Subagent reported success=True",
                "required": True,
                "weight": 7,
            },
        ],
        "drift_categories": {
            "phase_skip": {"severity": "critical", "auto_retry": True},
            "criteria_mismatch": {"severity": "high", "auto_retry": True},
            "output_corruption": {"severity": "high", "auto_retry": True},
            "protocol_breach": {"severity": "medium", "auto_retry": False},
        },
        "max_retries": 2,
        "scoring": {"mode": "all_required"},
    }


def infer_task_type(subagent_result, task_module=None) -> str:
    """
    Map OrCAID context to verification task type.
    Uses task_module class name to distinguish commit0 vs paperbench.
    """
    if task_module is None:
        return "code_review"

    task_module_name = task_module.__class__.__name__

    if "Commit" in task_module_name:
        return "code_review"
    elif "Paper" in task_module_name or "Bench" in task_module_name:
        return "research_reproduction"
    else:
        return "code_review"


# =============================================================================
# Verification Core
# =============================================================================


def verify_subagent_result(
    subagent_result,
    review_result: dict,
    task_type: str,
    checklist_base: Optional[Path] = None,
    attempt_number: int = 1,
) -> VerificationResult:
    """
    Score a SubAgentResult against the checklist for its task type.

    Returns VerificationResult with verdict, drift_log, verified_outcome,
    and optionally correction_context for retry.
    """
    checklist = load_checklist(task_type, checklist_base)
    drift_log = []

    # Evaluate each pass criterion
    for criterion in checklist.get("pass_criteria", []):
        passed = _check_criterion(criterion, subagent_result, review_result)
        if not passed:
            category = _classify_drift(criterion["id"], subagent_result, review_result)
            severity = (
                checklist["drift_categories"]
                .get(category, {})
                .get("severity", "medium")
            )
            drift_log.append(
                {
                    "criterion_id": criterion["id"],
                    "criterion_description": criterion.get("description", ""),
                    "check_result": {"passed": False},
                    "category": category,
                    "severity": severity,
                    "failure_message": criterion.get(
                        "failure_message", f"Failed: {criterion['id']}"
                    ),
                }
            )

    # Build output summary for verified outcome or drift log
    output_summary = _build_output_summary(subagent_result, review_result)

    if not drift_log:
        # PASS
        verified_outcome = _build_verified_outcome(
            subagent_result, review_result, task_type, output_summary
        )
        return VerificationResult(
            verdict="pass",
            drift_log=[],
            verified_outcome=verified_outcome,
            retry_recommended=False,
        )

    # FAIL — decide retry vs escalate
    max_retries = checklist.get("max_retries", 2)

    if attempt_number < max_retries:
        correction_context = _build_correction_context(
            drift_log, subagent_result, task_type, attempt_number
        )
        return VerificationResult(
            verdict="fail",
            drift_log=drift_log,
            verified_outcome=None,
            retry_recommended=True,
            correction_context=correction_context,
            attempt_number=attempt_number,
        )
    else:
        return VerificationResult(
            verdict="fail",
            drift_log=drift_log,
            verified_outcome=None,
            retry_recommended=False,
            escalate=True,
            attempt_number=attempt_number,
        )


def _check_criterion(criterion: dict, subagent_result, review_result: dict) -> bool:
    """Evaluate a single pass criterion against the subagent result."""
    criterion_id = criterion["id"]

    if criterion_id == "commit_made":
        return bool(
            subagent_result.commit_hash and subagent_result.commit_hash != "none"
        )

    elif criterion_id == "files_modified":
        # For code_review: files_modified should not be empty
        # For research_reproduction: submission_exists should be True
        if hasattr(subagent_result, "files_modified"):
            return bool(subagent_result.files_modified)
        return False

    elif criterion_id == "no_conflicts":
        return not subagent_result.conflict_files

    elif criterion_id == "success_flag":
        return subagent_result.success is True

    elif criterion_id == "test_coverage":
        # Check if test files in files_modified
        if not subagent_result.files_modified:
            return False
        return any(
            "test" in f.lower() or f.endswith("_test.py") or f.endswith(".test.ts")
            for f in subagent_result.files_modified
        )

    elif criterion_id == "iteration_efficiency":
        if subagent_result.max_iterations > 0:
            return subagent_result.actual_iterations < (
                subagent_result.max_iterations * 0.9
            )
        return True  # no max set, assume efficient

    elif criterion_id == "submission_exists":
        return getattr(subagent_result, "submission_exists", False) is True

    elif criterion_id == "reproduce_script":
        return getattr(subagent_result, "reproduce_script_exists", False) is True

    elif criterion_id == "commits_made":
        return getattr(subagent_result, "git_commits", 0) > 0

    elif criterion_id == "submission_completeness":
        # For paperbench: at least one file in submission
        if hasattr(subagent_result, "files_modified"):
            return bool(subagent_result.files_modified)
        return False

    return True  # unknown criterion, pass it


def _classify_drift(criterion_id: str, subagent_result, review_result: dict) -> str:
    """Classify the drift category based on which criterion failed."""
    if criterion_id in ("commit_made", "files_modified", "submission_exists"):
        return "phase_skip"
    elif criterion_id in ("no_conflicts", "merge_conflict"):
        return "protocol_breach"
    elif criterion_id == "success_flag":
        return "output_corruption"
    else:
        return "criteria_mismatch"


def _build_output_summary(subagent_result, review_result: dict) -> str:
    """Build human-readable output summary from subagent result."""
    lines = [
        f"Engineer: {subagent_result.engineer_id}",
        f"Task: {subagent_result.task_id}",
        f"Success: {subagent_result.success}",
        f"Commit: {subagent_result.commit_hash or 'none'}",
        f"Files modified: {', '.join(subagent_result.files_modified) if subagent_result.files_modified else 'none'}",
        f"Duration: {subagent_result.duration_seconds:.1f}s",
        f"Cost: ${subagent_result.cost:.4f}",
        f"Round: {subagent_result.round_num}",
        f"Merged: {review_result.get('merged', False)}",
        f"Merge method: {review_result.get('merge_method', 'none')}",
        f"Conflicts: {', '.join(subagent_result.conflict_files) if subagent_result.conflict_files else 'none'}",
    ]
    return "\n".join(lines)


def _build_verified_outcome(
    subagent_result, review_result: dict, task_type: str, output_summary: str
) -> dict:
    """Build a verified outcome dict to write to orchestrator-memory."""
    return {
        "task_id": subagent_result.task_id,
        "task_type": task_type,
        "worker_profile": ORCAID_TO_HERMES_PROFILE.get(
            subagent_result.engineer_id, "coder"
        ),
        "timestamp": datetime.now().isoformat(),
        "output_summary": output_summary,
        "files_modified": subagent_result.files_modified,
        "commit_hash": subagent_result.commit_hash,
        "duration": subagent_result.duration_seconds,
        "cost": subagent_result.cost,
        "merged": review_result.get("merged", False),
        "merge_method": review_result.get("merge_method", ""),
    }


def _build_correction_context(
    drift_log: list[dict],
    subagent_result,
    task_type: str,
    attempt_number: int,
) -> dict:
    """Build correction context for re-invoke."""
    # Format drift log
    drift_lines = []
    for entry in drift_log:
        drift_lines.append(
            f"- [{entry['severity']}] {entry['criterion_id']}: {entry['failure_message']}"
        )
    drift_formatted = "\n".join(drift_lines) if drift_lines else "Unknown drift"

    # Build original requirements from subagent result
    requirements = []
    if hasattr(subagent_result, "file_path") and subagent_result.file_path:
        requirements.append(f"file: {subagent_result.file_path}")
    if (
        hasattr(subagent_result, "functions_implemented")
        and subagent_result.functions_implemented
    ):
        requirements.append(
            f"functions: {', '.join(subagent_result.functions_implemented)}"
        )
    if hasattr(subagent_result, "requirements") and subagent_result.requirements:
        requirements.append(f"requirements: {subagent_result.requirements}")

    instructions = f"""Review the drift log and correct your approach before re-executing.

DRIFT LOG (attempt {attempt_number}):
{drift_formatted}

YOUR OUTPUT:
- Files modified: {subagent_result.files_modified or []}
- Commit: {subagent_result.commit_hash or 'none'}
- Success: {subagent_result.success}

CORRECTION:
"""
    for entry in drift_log:
        if entry["category"] == "phase_skip":
            instructions += f"\n  - Ensure you make a commit with the expected files for {entry['criterion_id']}."
        elif entry["category"] == "protocol_breach":
            instructions += "\n  - Resolve merge conflicts before completing."
        elif entry["category"] == "criteria_mismatch":
            instructions += f"\n  - Fix: {entry['failure_message']}"
        else:
            instructions += f"\n  - Address: {entry['failure_message']}"

    return {
        "task_id": subagent_result.task_id,
        "task_node_id": getattr(subagent_result, "task_node_id", ""),
        "attempt_number": attempt_number + 1,
        "original_requirements": (
            "\n".join(requirements) if requirements else "See task description"
        ),
        "drift_log": drift_log,
        "drift_log_formatted": drift_formatted,
        "instructions": instructions,
        "task_type": task_type,
    }


# =============================================================================
# Orchestrator Memory Write
# =============================================================================


def write_verified_outcome(verified_outcome: dict, memory_base: Optional[Path] = None):
    """Write a verified outcome to orchestrator-memory as a skill."""
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE
    task_type = verified_outcome.get("task_type", "unknown")
    task_id = verified_outcome.get("task_id", "unknown")
    timestamp = verified_outcome.get("timestamp", datetime.now().isoformat())

    filename = f"{task_id}__{timestamp.replace(':', '-')}.md"
    skill_dir = memory_base / "skills" / task_type
    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"""---
verified_outcome: true
task_id: {task_id}
task_type: {task_type}
timestamp: {timestamp}
worker_profile: {verified_outcome.get('worker_profile', 'unknown')}
pass_criteria_checked: commit_made, files_modified, no_conflicts, success_flag
---

## Output Summary

{verified_outcome.get('output_summary', 'N/A')}

## Execution Details

- Duration: {verified_outcome.get('duration', 'N/A')}s
- Files modified: {verified_outcome.get('files_modified', [])}
- Commit: {verified_outcome.get('commit_hash', 'N/A')}
- Cost: ${verified_outcome.get('cost', 0):.4f}
- Merged: {verified_outcome.get('merged', False)}
- Merge method: {verified_outcome.get('merge_method', 'none')}

## Verification

All pass criteria checked and met. Outcome written to orchestrator-memory.
"""

    skill_path = skill_dir / filename
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Update discovery index
    _update_discovery_index(task_type, "verified", memory_base)

    return skill_path


def write_drift_log(
    drift_log: list[dict],
    correction_context: dict,
    subagent_result,
    memory_base: Optional[Path] = None,
) -> Path:
    """Write a drift log entry to orchestrator-memory."""
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
    diff_content = getattr(subagent_result, "git_diff", "") or "No git diff available."

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


def _update_discovery_index(task_type: str, outcome: str, memory_base: Path):
    """Update the discovery index after a write."""
    index_path = memory_base / "index" / "discovery.yaml"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    index = {}
    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            index = yaml.safe_load(f) or {}

    if "task_types" not in index:
        index["task_types"] = {}

    if task_type not in index["task_types"]:
        index["task_types"][task_type] = {
            "last_verified": None,
            "last_outcome": None,
            "total_completed": 0,
            "total_failed": 0,
            "drift_rate": 0.0,
        }

    stats = index["task_types"][task_type]
    stats["last_verified"] = datetime.now().isoformat()
    stats["last_outcome"] = outcome

    if outcome == "verified":
        stats["total_completed"] = stats.get("total_completed", 0) + 1
    elif outcome == "failed":
        stats["total_failed"] = stats.get("total_failed", 0) + 1

    total = stats["total_completed"] + stats["total_failed"]
    if total > 0:
        stats["drift_rate"] = stats["total_failed"] / total

    with open(index_path, "w", encoding="utf-8") as f:
        yaml.dump(index, f)


# =============================================================================
# Integration Entry Points
# =============================================================================


def verify_subagent_completion(
    subagent_result,
    review_result: dict,
    task_module=None,
    orchestrator_memory_base: Optional[Path] = None,
) -> VerificationResult:
    """
    Main integration point for the collect_and_merge hook.
    Called after OrCAID builds review_result but before returning it.

    Returns VerificationResult with pass/fail/escalate verdict.
    Side effects: writes verified_outcome or drift_log to orchestrator-memory.
    """
    task_type = infer_task_type(subagent_result, task_module)
    orchestrator_memory_base = orchestrator_memory_base or ORCHESTRATOR_MEMORY_BASE

    # Check for prior attempts (track retry count)
    attempt_number = _get_attempt_number(
        subagent_result.task_id,
        orchestrator_memory_base,
    )

    verification = verify_subagent_result(
        subagent_result=subagent_result,
        review_result=review_result,
        task_type=task_type,
        checklist_base=orchestrator_memory_base,
        attempt_number=attempt_number,
    )

    if verification.verdict == "pass" and verification.verified_outcome:
        write_verified_outcome(verification.verified_outcome, orchestrator_memory_base)
    elif verification.verdict == "fail" and verification.drift_log:
        if verification.correction_context:
            write_drift_log(
                verification.drift_log,
                verification.correction_context,
                subagent_result,
                orchestrator_memory_base,
            )

    return verification


def build_correction_context(
    verification: VerificationResult, subagent_result, review_result: dict
) -> dict:
    """Build correction context from a failed verification result."""
    return verification.correction_context or {}


def orcaid_reinvoke_subagent(
    manager,
    engineer_id: str,
    original_task_id: str,
    correction_context: dict,
    max_retries: int = 3,
):
    """
    Re-invoke an OrCAID subagent with correction context.
    Uses the existing worktree and branch — creates a retry task.

    This is a best-effort integration. OrCAID's run_subagents_parallel
    loop doesn't natively support retry-within-round, so this creates
    a new SubAgentTask and assigns it through the manager.
    """
    from orcaid.config import SubAgentTask

    attempt = correction_context.get("attempt_number", 2)
    if attempt > max_retries:
        return None  # exceeded max retries

    # Build retry task
    retry_task = SubAgentTask(
        engineer_id=engineer_id,
        task_id=f"{original_task_id}_retry_{attempt}",
        task_node_id=correction_context.get("task_node_id", ""),
        requirements=correction_context.get("original_requirements", ""),
        instruction=correction_context.get("instructions", ""),
        context=f"""PREVIOUS ATTEMPT FAILED. Correct before re-executing.

DRIFT LOG (attempt {attempt - 1}):
{correction_context.get('drift_log_formatted', 'See prior drift log.')}

CORRECTION:
{correction_context.get('instructions', '')}
""",
        estimated_complexity="medium",
    )

    # Attempt to assign through manager's assign_task mechanism
    # Note: This requires OrCAID's internal state to have idle agents
    # If manager.assign_task is synchronous, the retry task gets queued
    # for the next round. This is the best-effort integration point.
    try:
        if hasattr(manager, "delegation_plan") and manager.delegation_plan:
            # Add retry task to remaining tasks — it will be picked up in next round
            manager.delegation_plan.remaining_tasks.append(retry_task)
            manager.log(f"[VerificationBridge] Queued retry task: {retry_task.task_id}")
        else:
            manager.log(
                "[VerificationBridge] No delegation_plan found — cannot queue retry task"
            )
            return None
    except Exception as e:
        manager.log(f"[VerificationBridge] Failed to queue retry task: {e}")
        return None

    return retry_task


def escalate_to_human(
    subagent_result, verification: VerificationResult, review_result: dict
):
    """
    Flag a subagent result for human review.
    Writes an escalation marker to orchestrator-memory and logs to OrCAID output.
    """
    memory_base = get_memory_base()
    escalation_dir = memory_base / "escalations"
    escalation_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().isoformat()
    filename = (
        f"escalate__{subagent_result.task_id}__{timestamp.replace(':', '-')}.yaml"
    )

    escalation = {
        "escalation": True,
        "task_id": subagent_result.task_id,
        "engineer_id": subagent_result.engineer_id,
        "timestamp": timestamp,
        "verdict": verification.verdict,
        "drift_log": verification.drift_log,
        "review_result": review_result,
        "subagent_result": {
            "success": subagent_result.success,
            "commit_hash": subagent_result.commit_hash,
            "files_modified": subagent_result.files_modified,
            "error": subagent_result.error,
        },
        "resolution": "pending_human_review",
    }

    escalation_path = escalation_dir / filename
    with open(escalation_path, "w", encoding="utf-8") as f:
        yaml.dump(escalation, f)

    # Log to OrCAID output logger if available
    # This is a notification-only operation; it doesn't block OrCAID


def _get_attempt_number(task_id: str, memory_base: Path) -> int:
    """Check how many prior attempts exist for this task_id."""
    drift_dir = memory_base / "drift_logs"
    attempt = 1

    if not drift_dir.exists():
        return attempt

    # Walk all drift log directories and count prior attempts for this task
    for type_dir in drift_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for log_file in type_dir.glob(f"{task_id}_*__*.md"):
            # Extract attempt number from filename: task_id__attempt__timestamp.md
            parts = log_file.stem.split("__")
            if len(parts) >= 2:
                try:
                    candidate = int(parts[1])
                    if candidate >= attempt:
                        attempt = candidate + 1
                except ValueError:
                    pass

    return attempt


# =============================================================================
# Discovery Scan
# =============================================================================


def discovery_scan_for_orcaid(
    orchestrator_memory_base: Optional[Path] = None,
    max_age_hours: int = 24,
) -> list[dict]:
    """
    Query orchestrator-memory for gaps that should inform OrCAID's planning.
    Called before scan_and_analyze() to augment the manager's context.

    Returns list of gap dicts with type, task_type, and description.
    """
    orchestrator_memory_base = orchestrator_memory_base or ORCHESTRATOR_MEMORY_BASE
    gaps = []

    index_path = orchestrator_memory_base / "index" / "discovery.yaml"
    if not index_path.exists():
        return gaps

    with open(index_path, "r", encoding="utf-8") as f:
        index = yaml.safe_load(f) or {}

    now = datetime.now()
    for task_type, stats in index.get("task_types", {}).items():
        # High drift rate → needs attention
        drift_rate = stats.get("drift_rate", 0)
        if drift_rate > 0.1:
            gaps.append(
                {
                    "type": "high_drift",
                    "task_type": task_type,
                    "drift_rate": drift_rate,
                    "total_failed": stats.get("total_failed", 0),
                    "description": f"Task type '{task_type}' has high drift rate: {drift_rate:.1%}",
                }
            )

        # Task types with no recent verification
        if stats.get("last_verified"):
            try:
                last_verified = datetime.fromisoformat(stats["last_verified"])
                age_hours = (now - last_verified).total_seconds() / 3600
                if age_hours > max_age_hours:
                    gaps.append(
                        {
                            "type": "stale_knowledge",
                            "task_type": task_type,
                            "hours_since_verified": age_hours,
                            "description": f"'{task_type}' not verified in {age_hours:.0f}h — may need re-analysis",
                        }
                    )
            except (ValueError, TypeError):
                pass

    # Check for escalation files (unresolved issues)
    escalation_dir = orchestrator_memory_base / "escalations"
    if escalation_dir.exists():
        for esc_file in escalation_dir.glob("escalate_*.yaml"):
            try:
                with open(esc_file, "r", encoding="utf-8") as f:
                    esc = yaml.safe_load(f)
                if esc.get("resolution") == "pending_human_review":
                    gaps.append(
                        {
                            "type": "pending_escalation",
                            "task_id": esc.get("task_id"),
                            "engineer_id": esc.get("engineer_id"),
                            "description": f"Unresolved escalation for {esc.get('task_id')} — needs human review",
                        }
                    )
            except (yaml.YAMLError, IOError):
                pass

    return gaps


def format_gaps_for_prompt(gaps: list[dict]) -> str:
    """Format discovery gaps as a string for injection into OrCAID prompts."""
    if not gaps:
        return "No known gaps from prior runs."

    lines = ["=== KNOWLEDGE GAPS FROM PRIOR RUNS ==="]
    for gap in gaps:
        lines.append(f"• [{gap['type']}] {gap['description']}")
    lines.append("==========================================")

    return "\n".join(lines)


# =============================================================================
# Synthesis
# =============================================================================


def synthesize_orcaid_outcome(
    subagent_results: list,
    manager_review_results: list,
    task_type: str,
) -> dict:
    """
    Synthesize all subagent results into a compound outcome skill.
    Called after final_review_all() to write a comprehensive skill.
    """
    timestamp = datetime.now().isoformat()

    successes = sum(1 for r in subagent_results if getattr(r, "success", False))
    failures = len(subagent_results) - successes
    total_cost = sum(getattr(r, "cost", 0) for r in subagent_results)
    total_duration = sum(getattr(r, "duration_seconds", 0) for r in subagent_results)

    files_modified_set = set()
    for r in subagent_results:
        for f in getattr(r, "files_modified", []) or []:
            files_modified_set.add(f)

    commit_hashes = []
    for r in subagent_results:
        ch = getattr(r, "commit_hash", None)
        if ch:
            commit_hashes.append(ch)

    return {
        "synthesis": True,
        "task_type": task_type,
        "timestamp": timestamp,
        "num_subagents": len(subagent_results),
        "successes": successes,
        "failures": failures,
        "total_cost": total_cost,
        "total_duration_seconds": total_duration,
        "files_modified": sorted(files_modified_set),
        "commit_hashes": commit_hashes,
        "manager_review_summary": {
            "num_reviews": len(manager_review_results),
            "merged_count": sum(1 for r in manager_review_results if r.get("merged")),
        },
    }


def write_compound_skill(synthesis: dict, memory_base: Optional[Path] = None) -> Path:
    """Write a compound synthesis skill to orchestrator-memory."""
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE
    task_type = synthesis.get("task_type", "unknown")
    timestamp = synthesis.get("timestamp", datetime.now().isoformat())

    filename = f"synthesis__{timestamp.replace(':', '-')}.md"
    skill_dir = memory_base / "skills" / task_type
    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"""---
synthesis: true
task_type: {task_type}
timestamp: {timestamp}
num_subagents: {synthesis.get('num_subagents', 0)}
successes: {synthesis.get('successes', 0)}
failures: {synthesis.get('failures', 0)}
total_cost: ${synthesis.get('total_cost', 0):.4f}
total_duration: {synthesis.get('total_duration_seconds', 0):.1f}s
---

## Subagent Results

- Subagents: {synthesis.get('num_subagents', 0)}
- Successes: {synthesis.get('successes', 0)}
- Failures: {synthesis.get('failures', 0)}

## Files Modified

{', '.join(synthesis.get('files_modified', [])) or 'none'}

## Commits

{', '.join(synthesis.get('commit_hashes', [])) or 'none'}

## Manager Review

- Reviews: {synthesis.get('manager_review_summary', {}).get('num_reviews', 0)}
- Merged: {synthesis.get('manager_review_summary', {}).get('merged_count', 0)}

## Synthesis

All subagent results synthesized after final_review_all().
"""

    skill_path = skill_dir / filename
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(content)

    return skill_path


def parse_frontmatter(file_path: Path) -> dict:
    """Parse YAML frontmatter from a markdown file."""
    if not file_path.exists():
        return {}
    try:
        content = file_path.read_text(encoding="utf-8")
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return yaml.safe_load(parts[1]) or {}
    except Exception:
        pass
    return {}


def run_indexer_sweep(memory_base: Optional[Path] = None):
    """
    Sweep all verified outcomes and drift logs to rebuild the discovery.yaml index.
    """
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE

    index = {
        "task_types": {},
        "profiles": {}
    }

    # 1. Sweep verified outcomes (skills directory)
    skills_dir = memory_base / "skills"
    if skills_dir.exists():
        for file_path in skills_dir.glob("**/*.md"):
            frontmatter = parse_frontmatter(file_path)
            if not frontmatter.get("verified_outcome"):
                continue

            task_type = frontmatter.get("task_type", "unknown")
            profile = frontmatter.get("worker_profile", "unknown")
            timestamp = frontmatter.get("timestamp")

            # Aggregate task_type stats
            if task_type not in index["task_types"]:
                index["task_types"][task_type] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0,
                    "last_verified": None,
                    "last_outcome": None,
                    "_last_timestamp": None,
                }

            stats = index["task_types"][task_type]
            stats["total_completed"] += 1

            if timestamp:
                if not stats["_last_timestamp"] or timestamp > stats["_last_timestamp"]:
                    stats["_last_timestamp"] = timestamp
                    stats["last_verified"] = timestamp
                    stats["last_outcome"] = "verified"

            # Aggregate profile stats
            if profile not in index["profiles"]:
                index["profiles"][profile] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0,
                    "task_types": {}
                }

            p_stats = index["profiles"][profile]
            p_stats["total_completed"] += 1

            if task_type not in p_stats["task_types"]:
                p_stats["task_types"][task_type] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0
                }
            p_stats["task_types"][task_type]["total_completed"] += 1

    # 2. Sweep drift logs
    drift_dir = memory_base / "drift_logs"
    if drift_dir.exists():
        for file_path in drift_dir.glob("**/*.md"):
            frontmatter = parse_frontmatter(file_path)
            if not frontmatter.get("drift_log"):
                continue

            task_type = frontmatter.get("task_type", "unknown")
            profile = frontmatter.get("engineer_id", "unknown")
            timestamp = frontmatter.get("timestamp")

            # Aggregate task_type stats
            if task_type not in index["task_types"]:
                index["task_types"][task_type] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0,
                    "last_verified": None,
                    "last_outcome": None,
                    "_last_timestamp": None,
                }

            stats = index["task_types"][task_type]
            stats["total_failed"] += 1

            if timestamp:
                if not stats["_last_timestamp"] or timestamp > stats["_last_timestamp"]:
                    stats["_last_timestamp"] = timestamp
                    stats["last_verified"] = timestamp
                    stats["last_outcome"] = "failed"

            # Aggregate profile stats
            if profile not in index["profiles"]:
                index["profiles"][profile] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0,
                    "task_types": {}
                }

            p_stats = index["profiles"][profile]
            p_stats["total_failed"] += 1

            if task_type not in p_stats["task_types"]:
                p_stats["task_types"][task_type] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0
                }
            p_stats["task_types"][task_type]["total_failed"] += 1

    # 3. Calculate drift rates and post-process
    for stats in index["task_types"].values():
        stats.pop("_last_timestamp", None)
        total = stats["total_completed"] + stats["total_failed"]
        stats["drift_rate"] = stats["total_failed"] / total if total > 0 else 0.0

    for p_stats in index["profiles"].values():
        total = p_stats["total_completed"] + p_stats["total_failed"]
        p_stats["drift_rate"] = p_stats["total_failed"] / total if total > 0 else 0.0
        for t_stats in p_stats["task_types"].values():
            t_total = t_stats["total_completed"] + t_stats["total_failed"]
            t_stats["drift_rate"] = t_stats["total_failed"] / t_total if t_total > 0 else 0.0

    # Write the discovery.yaml file
    index_path = memory_base / "index" / "discovery.yaml"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        yaml.dump(index, f, default_flow_style=False)


def run_sweep_cli():
    """CLI entrypoint for orcaid-verification-indexer sweep command.

    Resolves the memory base path from ``ORCHESTRATOR_MEMORY_BASE`` env var
    or falls back to ``~/.orcaid/orchestrator-memory``.
    """
    import sys

    memory_base = get_memory_base()
    print("Starting OrCAID Orchestrator Memory Sweep Indexer...")
    print(f"  Memory base: {memory_base}")
    try:
        run_indexer_sweep(memory_base=memory_base)
        print("OrCAID Sweep Indexer completed successfully.")
    except Exception as e:
        print(f"Error during sweep indexer: {e}", file=sys.stderr)
        sys.exit(1)

