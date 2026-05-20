---
spike: 003
name: json-structured-output
type: standard
validates: "Given a running workflow, when events are emitted, then outputs are parseable JSONL (no boxed format)"
verdict: PENDING
related: [001, 002]
tags: [output, formatting, observability, commit0]
---

# Spike 003: JSON Structured Output

## What This Validates

Given a running workflow, when events are emitted, then outputs are parseable JSONL (no boxed format like `╭──╮`).

## Context

- **User's actual command**: `ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli --task=commit0 ...`
- **Task**: commit0 - produces more complex outputs with subagent delegation

## Root Cause Being Addressed

From `output-system-quality.md`:
- **H5**: Boxed log format (`╭──╮`) makes programmatic parsing impossible

## Research

### Current State - The Problem

The `run_*.log` files contain mixed output:
```
╭──────────────────────────────── Agent Action ────────────────────────────────╮
│ Predicted Security Risk: LOW                                                 │
│ Summary: View utils.py content around line 660                               │
```

While `outputs.jsonl` correctly contains structured events.

### Two Distinct Outputs in commit0

| Output | Location | Format | Status |
|--------|----------|--------|--------|
| `outputs.jsonl` | `outputs/*/outputs.jsonl` | Valid JSONL | ✓ Working |
| `run_*.log` | `outputs/*/run_*.log` | Mixed (boxed + JSON) | ✗ Problem |

### commit0 Specific Issues

commit0 runs produce MORE output because:
- More subagents (up to 12 vs 4 in self_improve)
- More complex delegation plans
- Test output embedded in logs

### Source of Boxed Format

The `╭──╮` format comes from OpenHands SDK's `AgentThinking` display and is captured by `TeeLogger` in `orcaid/core/utils.py:457`.

## How to Run Experiment

```bash
# Run commit0 and verify outputs
ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli \
  --task=commit0 \
  --model=minimax/MiniMax-M2.7 \
  --multi_agent=true \
  --max_iterations=5 \
  --sub_iterations=5 \
  --max_subagents 4 \
  --max_rounds_chat 2 \
  --repo=angrysky56/Paper2Code-Enhanced \
  --output_dir outputs/spike003-test

# Verify outputs.jsonl is valid JSONL
python3 -c "
import json
with open('outputs/spike003-test/*/outputs.jsonl') as f:
    for i, line in enumerate(f):
        json.loads(line)
        if i > 10:
            break
print('JSONL valid ✓')
"

# Count boxed characters in log
grep -c '╭' outputs/spike003-test/*/run_*.log || echo "0"
```

## What to Expect

| Metric | Current (broken) | Expected (fixed) |
|--------|-----------------|------------------|
| `outputs.jsonl` validity | Valid JSONL | Valid JSONL (no change) |
| `run_*.log` boxed chars | Many `╭──╮` | Zero |
| Real-time parseability | Impossible | Possible |

## Observability

The `outputs.jsonl` correctly captures:
- `scan_start` - pre-scan initiation
- `analysis_phase_complete` - pre-scan findings
- `delegation_complete` - delegation plan
- `onboarding_complete` - subagent onboarding
- `manager_final_review_all` - final summary

Each event has structured `content` dict with relevant data.

## Investigation Trail

### 2026-05-19: Initial Analysis
- `outputs.jsonl` is valid JSONL (working correctly)
- `run_*.log` is polluted with boxed format from OpenHands
- TeeLogger captures ALL stdout including OpenHands internals

### Next Steps
1. Verify outputs.jsonl is valid (sanity check)
2. Implement suppression of OpenHands boxed output via `--quiet` flag or logger configuration
3. Confirm run_*.log becomes clean