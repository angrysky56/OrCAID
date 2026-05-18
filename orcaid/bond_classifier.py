"""
Three-bond drift classifier for OrCAID.

Maps each failed verification (a ``drift_log`` + ``SubAgentResult`` + prior
attempt history) onto one of three "missing bond" labels derived from Chen et
al.'s molecular dissection of Long CoT (see
``/home/ty/Documents/LLM-WIKI/wiki/synthesis/entropic-machinery-cot-and-flagellum.md``
and ``/home/ty/Documents/LLM-WIKI/wiki/synthesis/self-prompting-via-production-stage-architecture.md``):

* ``deep_reasoning`` — *missing backbone*: the agent never produced a
  load-bearing logical structure. Drift category is ``phase_skip``: no commit,
  no files modified, success flag false. The trajectory has no backbone for
  later folding to attach to.

* ``self_reflection`` — *missing fold-back*: the agent committed work but the
  same failure type keeps reappearing across retries. The agent is not folding
  back to correct its earlier moves; errors accumulate rather than self-cancel.
  This is the load-bearing bond per Chen et al. — its absence is the most
  destructive.

* ``self_exploration`` — *locked-in basin*: across retries the agent keeps
  touching the same files / same functions / producing diffs with high
  signature overlap. It has committed early and cannot escape the basin.

* ``None`` is returned for "ambiguous / first-attempt-novel-shape" — caller may
  optionally route to an LLM fallback.

The classifier is intentionally rules-first: cheap, deterministic, auditable.
LLM fallback is an explicit, opt-in second pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


# Sentinel labels — kept as module constants to avoid stringly-typed bugs at
# call sites.
BOND_DEEP_REASONING = "deep_reasoning"
BOND_SELF_REFLECTION = "self_reflection"
BOND_SELF_EXPLORATION = "self_exploration"

ALL_BONDS = (BOND_DEEP_REASONING, BOND_SELF_REFLECTION, BOND_SELF_EXPLORATION)

# How much file-overlap counts as "stuck in the same basin".
LOCKED_IN_JACCARD_THRESHOLD = 0.7


@dataclass
class PriorAttempt:
    """Compact record of a previous failed attempt for one task_id.

    Constructed by ``orcaid.bridge._get_prior_attempts`` from the YAML
    frontmatter of older drift_log entries. Kept here (not in ``bridge``) so
    the classifier has zero dependency on storage details.
    """

    attempt: int
    files_modified: list[str]
    failed_criterion_ids: list[str]
    missing_bond: str | None = None


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity over two string sets. Empty ↔ empty returns 0.0."""
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def _failed_criterion_ids(drift_log: list[dict]) -> list[str]:
    """Pull the criterion_id list out of the drift_log entries safely."""
    return [
        entry.get("criterion_id", "")
        for entry in (drift_log or [])
        if entry.get("criterion_id")
    ]


def _has_phase_skip(drift_log: list[dict]) -> bool:
    """True if any drift entry is in the ``phase_skip`` category."""
    return any(
        entry.get("category") == "phase_skip" for entry in (drift_log or [])
    )


def _same_failure_repeated(
    current_failed: list[str], prior_attempts: list[PriorAttempt]
) -> bool:
    """True if the *exact* same criterion_id failed in a prior attempt.

    Self-reflection is "long-range corrective fold-back": if the same criterion
    keeps tripping across attempts, the agent is not folding back to fix it.
    """
    if not current_failed or not prior_attempts:
        return False
    current_set = set(current_failed)
    for prior in prior_attempts:
        if current_set & set(prior.failed_criterion_ids):
            return True
    return False


def _high_file_overlap(
    current_files: list[str], prior_attempts: list[PriorAttempt]
) -> bool:
    """True if current attempt overlaps prior files modified above threshold.

    Self-exploration is "basin escape". If retries keep touching the same files
    instead of trying somewhere new, exploration is the missing bond.
    """
    if not current_files or not prior_attempts:
        return False
    for prior in prior_attempts:
        if jaccard(current_files, prior.files_modified) >= LOCKED_IN_JACCARD_THRESHOLD:
            return True
    return False


def classify_bond_deficit_rules(
    drift_log: list[dict],
    subagent_result: Any,
    prior_attempts: list[PriorAttempt] | None = None,
    attempt_number: int = 1,
) -> str | None:
    """Rules-first bond-deficit classifier.

    Returns one of ``ALL_BONDS`` if a rule fires, or ``None`` if the failure
    shape is ambiguous (e.g. a brand-new first attempt failing with a single
    ``criteria_mismatch`` that has no precedent in this run).

    Args:
        drift_log: list of drift entries from ``verify_subagent_result``.
        subagent_result: the OrCAID ``SubAgentResult`` dataclass instance.
        prior_attempts: previously-failed attempts for this task_id, if any.
        attempt_number: 1-indexed retry counter for the current attempt.

    Order of evaluation matters — earlier rules dominate, since a missing
    backbone makes the higher-order bonds meaningless.
    """
    prior_attempts = prior_attempts or []
    current_failed = _failed_criterion_ids(drift_log)
    current_files = list(getattr(subagent_result, "files_modified", []) or [])

    # Rule 1 — Missing backbone (Deep-Reasoning).
    # No commit and no files modified ≈ no logical structure was produced.
    commit_hash = getattr(subagent_result, "commit_hash", None)
    success = bool(getattr(subagent_result, "success", False))
    if _has_phase_skip(drift_log) and not commit_hash and not current_files:
        return BOND_DEEP_REASONING
    if not success and not commit_hash and not current_files:
        return BOND_DEEP_REASONING

    # Rule 2 — Missing fold-back (Self-Reflection).
    # Same failure mode keeps reappearing across retries. Per the entropic-
    # machinery synthesis this is the load-bearing bond — its absence is the
    # most destructive failure mode, so it dominates Rule 3 on ties.
    if attempt_number > 1 and _same_failure_repeated(current_failed, prior_attempts):
        return BOND_SELF_REFLECTION

    # Rule 3 — Missing basin escape (Self-Exploration).
    # Retries that keep touching the same files are stuck in one basin.
    if attempt_number > 1 and _high_file_overlap(current_files, prior_attempts):
        return BOND_SELF_EXPLORATION

    # Ambiguous — caller may want to run an LLM fallback.
    return None


def llm_fallback_prompt(
    drift_log: list[dict],
    subagent_result: Any,
    prior_attempts: list[PriorAttempt],
    attempt_number: int,
) -> str:
    """Build the rubric prompt for the LLM fallback classifier.

    Kept as a pure function so the actual LLM call lives in the bridge (where
    the model client is wired) and this module remains LLM-free at import.
    """
    prior_lines = []
    for p in prior_attempts:
        prior_lines.append(
            f"  - attempt {p.attempt}: files={p.files_modified or 'none'}, "
            f"failed_criteria={p.failed_criterion_ids or 'none'}, "
            f"prev_bond={p.missing_bond or 'n/a'}"
        )
    prior_block = "\n".join(prior_lines) or "  (no prior attempts)"

    current_failed = _failed_criterion_ids(drift_log)
    current_files = list(getattr(subagent_result, "files_modified", []) or [])
    commit_hash = getattr(subagent_result, "commit_hash", None) or "none"

    return f"""You classify a single failed coding attempt by which of three
"bonds" was missing. The three bonds come from Chen et al. 2026's dissection
of Long Chain-of-Thought reasoning:

- deep_reasoning  : the agent never built a load-bearing logical backbone.
                    (No commit, no files, no structure to test against.)
- self_reflection : the agent committed but the same error keeps reappearing
                    across retries with no folding-back to correct it. This is
                    the load-bearing bond; without it reasoning collapses.
- self_exploration: the agent is stuck in one basin — retries touch the same
                    files / same functions, never trying a different approach.

Current attempt (#{attempt_number}):
  commit_hash = {commit_hash}
  files_modified = {current_files or 'none'}
  failed_criteria = {current_failed or 'none'}

Prior attempts on this task_id:
{prior_block}

Respond with exactly one line:
  bond=<deep_reasoning|self_reflection|self_exploration>
"""


def parse_llm_fallback_response(response: str) -> str | None:
    """Extract a bond label from an LLM fallback response. Returns None if
    the response is malformed or names a label outside ``ALL_BONDS``."""
    if not response:
        return None
    for line in response.splitlines():
        line = line.strip().lower()
        if line.startswith("bond="):
            candidate = line.split("=", 1)[1].strip().strip("'\"`")
            if candidate in ALL_BONDS:
                return candidate
    # Fallback: just look for any bond label anywhere in the response.
    lowered = response.lower()
    for bond in ALL_BONDS:
        if bond in lowered:
            return bond
    return None
