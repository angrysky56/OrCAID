---
spike: 002
name: delegation-plan-populated
type: standard
validates: "Given a manager analysis, when tasks are identified, then delegation_plan contains structured task objects (not empty)"
verdict: PENDING
related: [001, 003]
tags: [delegation, coordination, commit0]
---

# Spike 002: Delegation Plan Populated

## What This Validates

Given a manager analysis, when tasks are identified, then `delegation_plan` contains structured task objects (not empty `{}`).

## Context

- **User's actual command**: `ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli --task=commit0 ... --repo=angrysky56/Paper2Code-Enhanced`
- **Task**: commit0 (the production-grade task for implementing stubs)

## Root Cause Being Addressed

From `output-system-quality.md`:
- **H1**: Delegation plan empty because manager skips delegation when no clear tasks identified
- **H4**: Success=true but files_merged=0 indicates result aggregation bug

## Research

### Current State (commit0 runs)

Observed delegation patterns in commit0 runs:

| Run | delegation_plan | Notes |
|-----|----------------|-------|
| `Paper2Code-Enhanced/100x12x100` | `{}` (empty) | Problem: empty |
| `OrCAID/100x4x100x2` | Populated with 3 agents | Working but long instructions |
| `OrCAID.git/100x4x100x4` | `{}` (empty) | Problem: empty |
| `sqlfluff/5x4x50x2` | `{}` (empty) | Problem: empty |

The empty delegation plans correlate with:
1. Pre-scan finding no stubs (AGEM repo had no stubs)
2. The manager deciding "nothing to delegate"

### Investigation Points

In `orcaid/core/manager.py`, the delegation path for commit0:

1. **Pre-scan (skill-based)**: `_skill_based_delegate_tasks()` at line 658
   - Runs `find_pass_stubs.py` and `build_dependency_graph.py`
   - If no stubs found → `delegation_plan = None`

2. **LLM fallback**: `delegate_tasks()` at line 764
   - If `_skill_based_delegate_tasks()` sets `delegation_plan = None`, falls back to LLM
   - LLM extracts `delegation_plan` from conversation events

3. **Saving**: `delegations.json` saved at line ~819
   ```python
   delegation_json = extract_json_from_events(...) or {"delegation_plan": {}}
   ```

### Key Issue

Even when delegation works (OrCAID run with 3 agents), the `delegations.json` for Paper2Code-Enhanced is empty. This suggests:
1. Either the extraction from events is failing
2. Or the manager never emitted a properly structured `delegation_plan` event

## How to Run Experiment

```bash
# Run commit0 and check delegation
ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli \
  --task=commit0 \
  --model=minimax/MiniMax-M2.7 \
  --multi_agent=true \
  --max_iterations=10 \
  --sub_iterations=10 \
  --max_subagents 4 \
  --max_rounds_chat 2 \
  --repo=angrysky56/Paper2Code-Enhanced \
  --output_dir outputs/spike002-test

# Check if delegation_plan is populated
cat outputs/spike002-test/*/delegations.json | jq '.delegation_plan | has("first_round")'

# If empty, check outputs.jsonl for events
cat outputs/spike002-test/*/outputs.jsonl | jq 'select(.event_type == "delegation_complete")'
```

## What to Expect

| Scenario | delegation_plan.first_round.tasks |
|----------|----------------------------------|
| Current (broken) | `[]` or missing |
| Expected (fixed) | 1-12 tasks depending on stubs found |

## Observability

Check these event types in `outputs.jsonl`:
- `scan_start` - pre-scan initiation
- `analysis_phase_complete` - pre-scan results
- `delegation_complete` - manager's delegation decision
- `onboarding_complete` - subagents created

## Investigation Trail

### 2026-05-19: Initial Analysis
- Found many commit0 runs have empty `delegation_plan`
- Paper2Code-Enhanced delegation is empty despite having stubs according to pre-scan
- Traced through `_skill_based_delegate_tasks()` → `delegate_tasks()` → save

### Next Steps
1. Add debug logging to trace `delegation_plan` through the flow
2. Verify pre-scan output is correct
3. Check if `extract_json_from_events` can find delegation in conversation
4. Fix the save mechanism if needed