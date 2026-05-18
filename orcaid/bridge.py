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

_DEFAULT_ORCAID_HOME = Path.home() / ".hermes"


def get_memory_base() -> Path:
    """Return the orchestrator-memory root, resolved once from env."""
    return Path(
        os.environ.get(
            "ORCHESTRATOR_MEMORY_BASE",
            str(Path.home() / ".hermes" / "orchestrator-memory"),
        )
    )


def get_bridge_storage() -> Path:
    """Return the bridge storage root, resolved once from env."""
    return Path(
        os.environ.get(
            "ORCAID_BRIDGE_STORAGE",
            str(Path.home() / ".hermes" / "orcaid-bridge"),
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
    # Three-bond drift classification (per Chen et al. 2026, see
    # ``orcaid.bond_classifier``). None means "ambiguous" / not classified.
    missing_bond: Optional[str] = None
    bond_classification_method: Optional[str] = None  # 'rules' | 'llm'


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

    # Fall back to bridge's own reference checklists and auto-initialize
    bridge_checklist = (
        Path(__file__).parent / "checklists" / f"checklist_{task_type}.yaml"
    )
    if bridge_checklist.exists():
        try:
            # Auto-initialize the user's local orchestrator-memory directory structure
            om_checklist.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(bridge_checklist, om_checklist)
            with open(om_checklist, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            # Fallback to loading directly from the packaged templates if copying fails
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
    prior_attempts: list | None = None,
) -> VerificationResult:
    """
    Score a SubAgentResult against the checklist for its task type.

    Returns VerificationResult with verdict, drift_log, verified_outcome,
    and optionally correction_context for retry. On a FAIL verdict, also
    classifies the missing CoT bond via the three-bond classifier so the
    meta-harness can learn the *shape* of failure (not just the count).

    ``prior_attempts`` carries the prior-failed-attempt records for this
    task_id (built by ``_get_prior_attempts``). Both the MOP retry policy
    and the bond classifier consume it; the caller is responsible for
    fetching it once and threading it through.
    """
    checklist = load_checklist(task_type, checklist_base)
    drift_log = []
    prior_attempts = prior_attempts or []

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

    # FAIL — classify the missing bond, then decide retry vs escalate.
    missing_bond, bond_method = _classify_missing_bond(
        drift_log, subagent_result, prior_attempts, attempt_number
    )

    max_retries = checklist.get("max_retries", 2)

    if attempt_number < max_retries:
        correction_context = _build_correction_context(
            drift_log,
            subagent_result,
            task_type,
            attempt_number,
            prior_attempts=prior_attempts,
        )
        return VerificationResult(
            verdict="fail",
            drift_log=drift_log,
            verified_outcome=None,
            retry_recommended=True,
            correction_context=correction_context,
            attempt_number=attempt_number,
            missing_bond=missing_bond,
            bond_classification_method=bond_method,
        )
    else:
        return VerificationResult(
            verdict="fail",
            drift_log=drift_log,
            verified_outcome=None,
            retry_recommended=False,
            escalate=True,
            attempt_number=attempt_number,
            missing_bond=missing_bond,
            bond_classification_method=bond_method,
        )


def _classify_missing_bond(
    drift_log: list[dict],
    subagent_result,
    prior_attempts: list,
    attempt_number: int,
) -> tuple[Optional[str], Optional[str]]:
    """Run the three-bond drift classifier with rules-first / LLM-fallback.

    Returns ``(missing_bond, method)`` where ``method`` is ``'rules'``,
    ``'llm'``, or ``None`` (if the classifier was disabled or remained
    ambiguous). Controlled by env vars:

    * ``ORCAID_BOND_CLASSIFY``  - "1" (default) or "0" to disable entirely.
    * ``ORCAID_BOND_LLM_FALLBACK`` - "1" to call an LLM on ambiguous cases.
      Off by default since the rules cover the common patterns and the LLM
      adds cost.
    """
    if os.environ.get("ORCAID_BOND_CLASSIFY", "1") != "1":
        return None, None

    from orcaid.bond_classifier import classify_bond_deficit_rules

    bond = classify_bond_deficit_rules(
        drift_log,
        subagent_result,
        prior_attempts=prior_attempts,
        attempt_number=attempt_number,
    )
    if bond is not None:
        return bond, "rules"

    if os.environ.get("ORCAID_BOND_LLM_FALLBACK", "0") == "1":
        try:
            bond = _llm_bond_fallback(
                drift_log, subagent_result, prior_attempts, attempt_number
            )
            if bond is not None:
                return bond, "llm"
        except Exception:
            # LLM fallback is best-effort; never fail the verification path.
            pass

    return None, None


def _llm_bond_fallback(
    drift_log: list[dict],
    subagent_result,
    prior_attempts: list,
    attempt_number: int,
) -> Optional[str]:
    """LLM fallback for bond classification on ambiguous failures.

    Uses the model wired into ``ORCAID_BOND_LLM_MODEL`` (default the manager
    model) via ``litellm.completion``. Kept tiny and prompt-only — the
    rubric prompt lives in ``orcaid.bond_classifier.llm_fallback_prompt``.
    """
    from orcaid.bond_classifier import (
        llm_fallback_prompt,
        parse_llm_fallback_response,
    )

    model = os.environ.get("ORCAID_BOND_LLM_MODEL") or os.environ.get("LITELLM_MODEL")
    if not model:
        return None
    try:
        import litellm
    except ImportError:
        return None

    prompt = llm_fallback_prompt(
        drift_log, subagent_result, prior_attempts, attempt_number
    )
    response = litellm.completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a strict rubric classifier. Output exactly one line.",
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=64,
        temperature=0.0,
    )
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_llm_fallback_response(content or "")


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
        return getattr(subagent_result, "error", None) is None

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


def _format_drift_lines(drift_log: list[dict]) -> str:
    """Render a drift_log as bullet lines for prompt injection."""
    drift_lines = [
        f"- [{entry['severity']}] {entry['criterion_id']}: {entry['failure_message']}"
        for entry in drift_log
    ]
    return "\n".join(drift_lines) if drift_lines else "Unknown drift"


def _gather_requirements(subagent_result) -> list[str]:
    """Pull the structured 'what was originally asked' lines off the result."""
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
    return requirements


def _kl_instructions(
    drift_log: list[dict], subagent_result, attempt_number: int
) -> str:
    """KL-style correction: nudge the agent toward fixing the exact failing
    criteria. This is the historical default — keeps retries close to the
    previous attempt, applying targeted patches to the failing bits.
    """
    drift_formatted = _format_drift_lines(drift_log)
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
            instructions += (
                f"\n  - Ensure you make a commit with the expected files "
                f"for {entry['criterion_id']}."
            )
        elif entry["category"] == "protocol_breach":
            instructions += "\n  - Resolve merge conflicts before completing."
        elif entry["category"] == "criteria_mismatch":
            instructions += f"\n  - Fix: {entry['failure_message']}"
        else:
            instructions += f"\n  - Address: {entry['failure_message']}"
    return instructions


def _mop_instructions(
    drift_log: list[dict],
    subagent_result,
    attempt_number: int,
    prior_attempts: list,
) -> str:
    """MOP-style correction: maximize state-entropy from the cone of prior
    failed attempts, bounded by the "test must pass" absorbing state.

    Where KL-style retry says "fix this exact bit," MOP-style retry says
    "all prior attempts touched <these files> and still failed — try a
    *substantially different* approach." This implements the wiki claim
    (mop-edm-cognitive-architecture, sec. KL Regularization Problem) that
    KL toward a failing target is self-defeating for occupancy maximization.
    """
    drift_formatted = _format_drift_lines(drift_log)

    # Build the avoidance manifold: union of files / criteria across history.
    seen_files: set[str] = set()
    seen_criteria: set[str] = set()
    prior_lines: list[str] = []
    # Include the *current* failed attempt as part of the manifold too — it
    # is, after all, the most recent thing not to do again.
    for f in subagent_result.files_modified or []:
        seen_files.add(f)
    for entry in drift_log:
        cid = entry.get("criterion_id")
        if cid:
            seen_criteria.add(cid)
    for p in prior_attempts:
        for f in p.files_modified or []:
            seen_files.add(f)
        for c in p.failed_criterion_ids or []:
            seen_criteria.add(c)
        prior_lines.append(
            f"  - attempt {p.attempt}: files={p.files_modified or 'none'}, "
            f"failed_criteria={p.failed_criterion_ids or 'none'}"
        )

    prior_block = "\n".join(prior_lines) or "  (this is the first retry — only current attempt failed)"
    files_block = ", ".join(sorted(seen_files)) or "none"
    criteria_block = ", ".join(sorted(seen_criteria)) or "none"

    instructions = f"""All prior attempts on this task have failed. The test is the
absorbing state: it must pass. Otherwise, MAXIMIZE DIVERGENCE from prior
attempts — different files, different functions, different logical structure.
Do not refine the previous diff; try a substantively different approach.

CURRENT DRIFT (attempt {attempt_number}):
{drift_formatted}

FAILURE MANIFOLD (do NOT repeat these directions):
- Files touched so far: {files_block}
- Criteria that keep failing: {criteria_block}

PRIOR FAILED ATTEMPTS:
{prior_block}

INSTRUCTIONS:
  1. Pick a retry direction that *minimizes overlap* with the failure manifold
     above. Prefer different files/functions/strategies over patching the same diff.
  2. The only hard constraint is the test command — it must pass at the end.
  3. If you genuinely cannot change direction (e.g. there is only one file
     that can solve this), explain why in your commit message.
"""
    return instructions


def _build_correction_context(
    drift_log: list[dict],
    subagent_result,
    task_type: str,
    attempt_number: int,
    prior_attempts: list | None = None,
) -> dict:
    """Build correction context for re-invoke.

    Dispatches between the KL-style nudge-toward-target instruction and the
    MOP-style maximize-divergence instruction based on the
    ``ORCAID_RETRY_POLICY`` environment variable. Default ``kl`` keeps
    existing behavior; set ``ORCAID_RETRY_POLICY=mop`` to enable the
    diversity-maximizing retry policy for A/B testing.
    """
    drift_formatted = _format_drift_lines(drift_log)
    requirements = _gather_requirements(subagent_result)
    prior_attempts = prior_attempts or []

    policy = os.environ.get("ORCAID_RETRY_POLICY", "kl").lower()
    if policy not in ("kl", "mop"):
        # Fail safely back to the historical policy if env var is malformed.
        policy = "kl"

    if policy == "mop":
        instructions = _mop_instructions(
            drift_log, subagent_result, attempt_number, prior_attempts
        )
    else:
        instructions = _kl_instructions(drift_log, subagent_result, attempt_number)

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
        "retry_policy": policy,
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
    *,
    missing_bond: Optional[str] = None,
    bond_classification_method: Optional[str] = None,
) -> Path:
    """Write a drift log entry to orchestrator-memory.

    The frontmatter is extended with structured fields that the indexer
    sweep and the bond classifier read back later:

    * ``files_modified``        — list, used by MOP retry & exploration check.
    * ``failed_criterion_ids``  — list, used by self-reflection check.
    * ``missing_bond``          — three-bond classifier label or null.
    * ``bond_classification_method`` — 'rules' | 'llm' | null.
    * ``retry_policy``          — 'kl' | 'mop', so A/B runs can be sliced apart.
    """
    memory_base = memory_base or ORCHESTRATOR_MEMORY_BASE
    task_type = correction_context.get("task_type", "unknown")
    task_id = correction_context.get("task_id", "unknown")
    attempt = correction_context.get("attempt_number", 1)
    retry_policy = correction_context.get("retry_policy") or os.environ.get(
        "ORCAID_RETRY_POLICY", "kl"
    )
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

    files_modified = list(getattr(subagent_result, "files_modified", []) or [])
    failed_criterion_ids = [
        entry.get("criterion_id", "") for entry in drift_log if entry.get("criterion_id")
    ]

    # YAML-emit lists inline so callers reading frontmatter back with
    # ``yaml.safe_load`` round-trip cleanly.
    files_modified_yaml = yaml.safe_dump(
        files_modified, default_flow_style=True
    ).strip()
    failed_criterion_yaml = yaml.safe_dump(
        failed_criterion_ids, default_flow_style=True
    ).strip()

    content = f"""---
drift_log: true
task_id: {task_id}
task_type: {task_type}
attempt: {attempt}
timestamp: {timestamp}
resolution: in_progress
engineer_id: {subagent_result.engineer_id}
files_modified: {files_modified_yaml}
failed_criterion_ids: {failed_criterion_yaml}
missing_bond: {missing_bond or "null"}
bond_classification_method: {bond_classification_method or "null"}
retry_policy: {retry_policy}
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

    # Check for prior attempts (track retry count) — single fetch, reused by
    # both the MOP retry policy and the three-bond classifier.
    attempt_number = _get_attempt_number(
        subagent_result.task_id,
        orchestrator_memory_base,
    )
    prior_attempts = _get_prior_attempts(
        subagent_result.task_id,
        orchestrator_memory_base,
    )

    verification = verify_subagent_result(
        subagent_result=subagent_result,
        review_result=review_result,
        task_type=task_type,
        checklist_base=orchestrator_memory_base,
        attempt_number=attempt_number,
        prior_attempts=prior_attempts,
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
                missing_bond=verification.missing_bond,
                bond_classification_method=verification.bond_classification_method,
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


def _get_prior_attempts(task_id: str, memory_base: Path) -> list:
    """Return the list of prior failed attempts for this task_id.

    Each entry is a ``PriorAttempt`` reconstructed from the YAML frontmatter
    of a drift_log markdown file. Used by both the MOP retry policy (to
    decide what to *avoid*) and the bond classifier (to detect locked-in /
    repeated-failure patterns).

    Drift_log frontmatter is the source of truth — body parsing is avoided
    to keep this cheap and robust across format tweaks.
    """
    from orcaid.bond_classifier import PriorAttempt

    drift_dir = memory_base / "drift_logs"
    attempts: list = []
    if not drift_dir.exists():
        return attempts

    for type_dir in drift_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for log_file in sorted(type_dir.glob(f"{task_id}_*__*.md")):
            fm = parse_frontmatter(log_file)
            if not fm.get("drift_log"):
                continue
            # Pre-bond-classifier drift_logs omit these fields; default safely.
            attempts.append(
                PriorAttempt(
                    attempt=int(fm.get("attempt", 0) or 0),
                    files_modified=list(fm.get("files_modified") or []),
                    failed_criterion_ids=list(
                        fm.get("failed_criterion_ids") or []
                    ),
                    missing_bond=fm.get("missing_bond") or None,
                )
            )
    attempts.sort(key=lambda a: a.attempt)
    return attempts


# =============================================================================
# Discovery Scan
# =============================================================================


def _bond_routing_hint(missing_bond: str) -> str:
    """Map a missing-bond label to a one-line routing hint for the manager.

    The mapping is derived from the engineer profiles' bond affinity:
    developer/coder profiles produce the load-bearing backbone (Deep-Reasoning),
    debugger profile produces the corrective fold-back (Self-Reflection), and
    researcher profile produces alternative-direction proposals (Self-Exploration).
    Reviewer is a check, not a bond producer.
    """
    table = {
        "deep_reasoning": (
            "route through a developer/coder profile first and require a commit "
            "+ files_modified before any reflection step."
        ),
        "self_reflection": (
            "route through a debugger profile with explicit 'test then fold back' "
            "instructions; do not re-attempt without a corrective diff."
        ),
        "self_exploration": (
            "route through a researcher profile and explicitly forbid touching "
            "the files that prior attempts modified."
        ),
    }
    return table.get(missing_bond, "no specific hint")


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

        # Bond-deficit pattern → emit a routing hint. The meta-harness uses
        # this to choose a profile / strategy that targets the dominant deficit
        # rather than treating all failures uniformly.
        dominant = stats.get("dominant_deficit")
        bond_counts = stats.get("bond_deficit_counts") or {}
        classified_total = sum(
            v for k, v in bond_counts.items() if k != "unclassified"
        )
        if dominant and classified_total >= 3:
            hint = _bond_routing_hint(dominant)
            dominant_count = bond_counts.get(dominant, 0)
            gaps.append(
                {
                    "type": "bond_deficit",
                    "task_type": task_type,
                    "missing_bond": dominant,
                    "count": dominant_count,
                    "classified_total": classified_total,
                    "description": (
                        f"Task type '{task_type}' shows a {dominant} deficit "
                        f"({dominant_count}/{classified_total} classified drifts). "
                        f"Routing hint: {hint}"
                    ),
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
            missing_bond = frontmatter.get("missing_bond")
            retry_policy = frontmatter.get("retry_policy")

            # Aggregate task_type stats
            if task_type not in index["task_types"]:
                index["task_types"][task_type] = {
                    "total_completed": 0,
                    "total_failed": 0,
                    "drift_rate": 0.0,
                    "last_verified": None,
                    "last_outcome": None,
                    "_last_timestamp": None,
                    "bond_deficit_counts": {
                        "deep_reasoning": 0,
                        "self_reflection": 0,
                        "self_exploration": 0,
                        "unclassified": 0,
                    },
                    "retry_policy_counts": {"kl": 0, "mop": 0},
                }

            stats = index["task_types"][task_type]
            stats["total_failed"] += 1

            # Bond aggregation — count unclassified explicitly so the prompt
            # injection can decide whether the deficit pattern is meaningful.
            bond_counts = stats.setdefault(
                "bond_deficit_counts",
                {
                    "deep_reasoning": 0,
                    "self_reflection": 0,
                    "self_exploration": 0,
                    "unclassified": 0,
                },
            )
            if missing_bond in bond_counts:
                bond_counts[missing_bond] += 1
            else:
                bond_counts["unclassified"] += 1

            # Retry-policy aggregation for the A/B slice. Default to 'kl' for
            # pre-Phase-A drift_logs that lack the field.
            rp_counts = stats.setdefault("retry_policy_counts", {"kl": 0, "mop": 0})
            rp_key = retry_policy if retry_policy in rp_counts else "kl"
            rp_counts[rp_key] = rp_counts.get(rp_key, 0) + 1

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

        # Determine the dominant bond deficit, if any. Used by the discovery
        # scan to inject routing hints into the next planning pass.
        bond_counts = stats.get("bond_deficit_counts", {}) or {}
        # Only consider real bond labels for "dominant" (skip unclassified).
        candidates = {
            k: v for k, v in bond_counts.items() if k != "unclassified" and v > 0
        }
        if candidates:
            stats["dominant_deficit"] = max(candidates, key=candidates.get)
        else:
            stats["dominant_deficit"] = None

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

