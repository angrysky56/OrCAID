"""Tests for the MOP-style retry policy and the prior-attempt reader.

The toggle is the ``ORCAID_RETRY_POLICY`` environment variable; default
``kl`` keeps the historical correction-context instructions. Setting it to
``mop`` switches to the diversity-maximizing instructions derived from the
maximum-occupancy-principle synthesis in the LLM wiki.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from orcaid.bridge import (
    _build_correction_context,
    _get_prior_attempts,
    write_drift_log,
)
from orcaid.config import SubAgentResult


def _drift_log_entry(criterion_id: str, category: str = "criteria_mismatch") -> dict:
    return {
        "criterion_id": criterion_id,
        "criterion_description": "",
        "check_result": {"passed": False},
        "category": category,
        "severity": "high",
        "failure_message": f"{criterion_id} failed",
    }


def _make_subagent_result(
    files_modified: list[str] | None = None,
    commit_hash: str | None = "abc1234",
    success: bool = True,
) -> SubAgentResult:
    return SubAgentResult(
        engineer_id="engineer_2",
        task_id="task_42",
        task_node_id="node_42",
        success=success,
        commit_hash=commit_hash,
        files_modified=files_modified or ["src/widget.py"],
        functions_implemented=["compute"],
        requirements="Implement Widget.compute",
    )


# --------------------------------------------------------------------------- #
# Policy dispatch                                                             #
# --------------------------------------------------------------------------- #


def test_kl_policy_is_default(monkeypatch):
    monkeypatch.delenv("ORCAID_RETRY_POLICY", raising=False)
    drift = [_drift_log_entry("test_coverage")]
    ctx = _build_correction_context(
        drift, _make_subagent_result(), task_type="code_review", attempt_number=1
    )
    assert ctx["retry_policy"] == "kl"
    # KL instruction template uses the historical CORRECTION header.
    assert "CORRECTION:" in ctx["instructions"]
    assert "MAXIMIZE DIVERGENCE" not in ctx["instructions"]


def test_mop_policy_when_env_set(monkeypatch):
    monkeypatch.setenv("ORCAID_RETRY_POLICY", "mop")
    drift = [_drift_log_entry("test_coverage")]
    ctx = _build_correction_context(
        drift, _make_subagent_result(), task_type="code_review", attempt_number=2
    )
    assert ctx["retry_policy"] == "mop"
    # MOP instructions explicitly tell the agent to diverge from prior diff.
    assert "MAXIMIZE DIVERGENCE" in ctx["instructions"]
    # And still surface the current drift, since the absorbing state (test
    # failure) must remain visible to the engineer.
    assert "test_coverage" in ctx["instructions"]


def test_malformed_policy_falls_back_to_kl(monkeypatch):
    monkeypatch.setenv("ORCAID_RETRY_POLICY", "BOGUS_VALUE")
    drift = [_drift_log_entry("commit_made", category="phase_skip")]
    ctx = _build_correction_context(
        drift, _make_subagent_result(), task_type="code_review", attempt_number=1
    )
    assert ctx["retry_policy"] == "kl"


def test_mop_instructions_use_prior_attempts_for_avoidance_manifold(monkeypatch):
    """The MOP retry policy reads prior_attempts and lists the files-touched
    union as an explicit do-not-repeat manifold. This is the testable claim
    from the mop-edm-cognitive-architecture wiki page applied to OrCAID."""
    monkeypatch.setenv("ORCAID_RETRY_POLICY", "mop")
    from orcaid.bond_classifier import PriorAttempt

    drift = [_drift_log_entry("test_coverage")]
    prior = [
        PriorAttempt(
            attempt=1,
            files_modified=["src/legacy_a.py", "src/legacy_b.py"],
            failed_criterion_ids=["test_coverage"],
        )
    ]
    ctx = _build_correction_context(
        drift,
        _make_subagent_result(files_modified=["src/legacy_a.py"]),
        task_type="code_review",
        attempt_number=2,
        prior_attempts=prior,
    )
    # Both prior files appear in the avoidance manifold, plus the current attempt's file.
    assert "src/legacy_a.py" in ctx["instructions"]
    assert "src/legacy_b.py" in ctx["instructions"]


# --------------------------------------------------------------------------- #
# Prior-attempt reader                                                        #
# --------------------------------------------------------------------------- #


def test_get_prior_attempts_reads_extended_frontmatter():
    """write_drift_log writes structured frontmatter that
    _get_prior_attempts must round-trip cleanly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        result = _make_subagent_result(files_modified=["src/widget.py", "src/helper.py"])
        drift = [_drift_log_entry("test_coverage"), _drift_log_entry("commit_made")]
        correction_context = {
            "task_type": "code_review",
            "task_id": "task_42",
            "attempt_number": 1,
            "instructions": "n/a",
            "original_requirements": "n/a",
            "retry_policy": "mop",
        }
        write_drift_log(
            drift_log=drift,
            correction_context=correction_context,
            subagent_result=result,
            memory_base=tmp_path,
            missing_bond="self_reflection",
            bond_classification_method="rules",
        )

        attempts = _get_prior_attempts("task_42", tmp_path)
        assert len(attempts) == 1
        a = attempts[0]
        assert a.files_modified == ["src/widget.py", "src/helper.py"]
        assert set(a.failed_criterion_ids) == {"test_coverage", "commit_made"}
        assert a.missing_bond == "self_reflection"


def test_get_prior_attempts_handles_missing_dir():
    with tempfile.TemporaryDirectory() as tmp_dir:
        assert _get_prior_attempts("nonexistent_task", Path(tmp_dir)) == []


# --------------------------------------------------------------------------- #
# Drift log frontmatter regression                                            #
# --------------------------------------------------------------------------- #


def test_write_drift_log_emits_bond_and_policy_fields():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        result = _make_subagent_result()
        drift = [_drift_log_entry("test_coverage")]
        correction_context = {
            "task_type": "code_review",
            "task_id": "task_42",
            "attempt_number": 2,
            "instructions": "n/a",
            "original_requirements": "n/a",
            "retry_policy": "mop",
        }
        log_path = write_drift_log(
            drift_log=drift,
            correction_context=correction_context,
            subagent_result=result,
            memory_base=tmp_path,
            missing_bond="self_exploration",
            bond_classification_method="rules",
        )
        text = log_path.read_text(encoding="utf-8")
        assert "missing_bond: self_exploration" in text
        assert "bond_classification_method: rules" in text
        assert "retry_policy: mop" in text
        assert "files_modified:" in text
        assert "failed_criterion_ids:" in text
