"""Unit tests for the three-bond drift classifier.

Covers the rules-first classifier in ``orcaid.bond_classifier`` plus the
prompt-builder and parser used by the LLM fallback in ``orcaid.bridge``.
The fallback's actual LLM call is not exercised here — that path is wrapped
in ``orcaid.bridge._llm_bond_fallback`` and best-effort by design.
"""

from __future__ import annotations

from orcaid.bond_classifier import (
    ALL_BONDS,
    BOND_DEEP_REASONING,
    BOND_SELF_EXPLORATION,
    BOND_SELF_REFLECTION,
    PriorAttempt,
    classify_bond_deficit_rules,
    jaccard,
    llm_fallback_prompt,
    parse_llm_fallback_response,
)
from orcaid.config import SubAgentResult


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _drift_entry(criterion_id: str, category: str = "criteria_mismatch") -> dict:
    return {
        "criterion_id": criterion_id,
        "criterion_description": "",
        "check_result": {"passed": False},
        "category": category,
        "severity": "high",
        "failure_message": f"{criterion_id} failed",
    }


# --------------------------------------------------------------------------- #
# Rules-first classifier                                                      #
# --------------------------------------------------------------------------- #


def test_jaccard_basic():
    assert jaccard(["a", "b"], ["b", "c"]) == 1 / 3
    assert jaccard([], []) == 0.0
    assert jaccard(["x"], ["x"]) == 1.0


def test_deep_reasoning_no_backbone_phase_skip():
    """phase_skip + no commit + no files → deep_reasoning."""
    drift = [_drift_entry("commit_made", category="phase_skip")]
    result = SubAgentResult(
        engineer_id="engineer_1",
        task_id="t1",
        success=False,
        commit_hash=None,
        files_modified=[],
    )
    assert classify_bond_deficit_rules(drift, result, attempt_number=1) == BOND_DEEP_REASONING


def test_deep_reasoning_no_backbone_success_false_no_commit():
    """No drift category set, but the agent produced nothing → deep_reasoning."""
    drift = [_drift_entry("success_flag", category="output_corruption")]
    result = SubAgentResult(
        engineer_id="engineer_1",
        task_id="t1",
        success=False,
        commit_hash=None,
        files_modified=[],
    )
    assert classify_bond_deficit_rules(drift, result, attempt_number=1) == BOND_DEEP_REASONING


def test_self_reflection_repeated_failure_across_retries():
    """Same criterion failing across attempts with a commit present → self_reflection."""
    drift = [_drift_entry("test_coverage", category="criteria_mismatch")]
    result = SubAgentResult(
        engineer_id="engineer_2",
        task_id="t1",
        success=True,
        commit_hash="abc1234",
        files_modified=["src/foo.py"],
    )
    prior = [
        PriorAttempt(
            attempt=1,
            files_modified=["src/foo.py"],
            failed_criterion_ids=["test_coverage"],
        )
    ]
    assert (
        classify_bond_deficit_rules(drift, result, prior_attempts=prior, attempt_number=2)
        == BOND_SELF_REFLECTION
    )


def test_self_exploration_basin_lock_in():
    """Different criterion but same files across retries → self_exploration."""
    drift = [_drift_entry("iteration_efficiency", category="criteria_mismatch")]
    result = SubAgentResult(
        engineer_id="engineer_3",
        task_id="t1",
        success=True,
        commit_hash="def5678",
        files_modified=["src/foo.py", "src/bar.py"],
    )
    prior = [
        PriorAttempt(
            attempt=1,
            files_modified=["src/foo.py", "src/bar.py"],
            failed_criterion_ids=["commit_made"],  # different criterion this time
        )
    ]
    assert (
        classify_bond_deficit_rules(drift, result, prior_attempts=prior, attempt_number=2)
        == BOND_SELF_EXPLORATION
    )


def test_self_reflection_dominates_self_exploration_on_ties():
    """When the same criterion AND same files repeat, self_reflection (load-bearing
    bond per Chen et al.) takes precedence."""
    drift = [_drift_entry("test_coverage", category="criteria_mismatch")]
    result = SubAgentResult(
        engineer_id="engineer_2",
        task_id="t1",
        success=True,
        commit_hash="abc1234",
        files_modified=["src/foo.py"],
    )
    prior = [
        PriorAttempt(
            attempt=1,
            files_modified=["src/foo.py"],
            failed_criterion_ids=["test_coverage"],
        )
    ]
    assert (
        classify_bond_deficit_rules(drift, result, prior_attempts=prior, attempt_number=2)
        == BOND_SELF_REFLECTION
    )


def test_ambiguous_first_attempt_returns_none():
    """First attempt, novel criterion, has a commit → no rule fires."""
    drift = [_drift_entry("iteration_efficiency", category="criteria_mismatch")]
    result = SubAgentResult(
        engineer_id="engineer_2",
        task_id="t1",
        success=True,
        commit_hash="abc1234",
        files_modified=["src/foo.py"],
    )
    assert classify_bond_deficit_rules(drift, result, attempt_number=1) is None


# --------------------------------------------------------------------------- #
# LLM fallback prompt / parser                                                #
# --------------------------------------------------------------------------- #


def test_llm_fallback_prompt_contains_required_context():
    drift = [_drift_entry("test_coverage")]
    result = SubAgentResult(
        engineer_id="engineer_4",
        task_id="t9",
        commit_hash="cafef00d",
        files_modified=["src/baz.py"],
    )
    prior = [
        PriorAttempt(
            attempt=1, files_modified=["src/baz.py"], failed_criterion_ids=["commit_made"]
        )
    ]
    prompt = llm_fallback_prompt(drift, result, prior, attempt_number=2)
    # Mentions the three bond labels so the model has the rubric.
    for bond in ALL_BONDS:
        assert bond in prompt
    # Surfaces the current attempt's files & failing criteria.
    assert "src/baz.py" in prompt
    assert "test_coverage" in prompt
    # And the prior attempts list.
    assert "attempt 1" in prompt


def test_parse_llm_fallback_response_canonical_format():
    assert parse_llm_fallback_response("bond=self_reflection") == BOND_SELF_REFLECTION
    assert parse_llm_fallback_response("BOND=DEEP_REASONING\n") == BOND_DEEP_REASONING


def test_parse_llm_fallback_response_substring_fallback():
    text = "After analyzing the trace, the missing bond is self_exploration."
    assert parse_llm_fallback_response(text) == BOND_SELF_EXPLORATION


def test_parse_llm_fallback_response_invalid_returns_none():
    assert parse_llm_fallback_response("") is None
    assert parse_llm_fallback_response("bond=unknown_label") is None
