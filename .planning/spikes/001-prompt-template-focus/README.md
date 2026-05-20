---
spike: 001
name: prompt-template-focus
type: standard
validates: "Given a subagent task, when instructions are concise (<500 chars) and action-oriented, then agent completes work in fewer iterations"
verdict: PENDING
related: [002, 003]
tags: [prompt, delegation, quality, commit0]
---

# Spike 001: Prompt/Template Focus

## What This Validates

Given a subagent task, when instructions are concise (<500 chars) and action-oriented, then agent completes work in fewer iterations.

## Context

- **User's actual command**: `ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli --task=commit0 --model=minimax/MiniMax-M2.7 --multi_agent=true --max_iterations=100 --sub_iterations=100 --max_subagents 12 --max_rounds_chat 10 --repo=angrysky56/Paper2Code-Enhanced`
- **Task**: commit0 (not self_improve - commit0 is the production-grade task)

## Root Cause Being Addressed

From `output-system-quality.md`:
- **H2**: Subagents fail because instructions are too long/unfocused
- **H3**: MaxIterations reached because agents enter fix loops without exit criteria

## Research

### Current State (commit0.yaml)

The `subagent_prompt` in `prompts/commit0.yaml` (lines 194-213):
```yaml
subagent_prompt: |
  You are a software engineer working on implementing a code repository...

  Here is your task assigned by the manager:
  {instruction}
  You are assigned to implement the following functions in the file: {file_path}: {functions}
```

The `{instruction}` field is filled with massive context from manager. Example from OrCAID run:
```
instruction: "You are implementing the commit0 task module in the OrCAID multi-agent system. This file (orcaid/tasks/commit0.py) handles the Commit0 task which involves cloning repositories, running tests, and coordinating subagents.\n\n**Repository structure context:**\n- This project (OrCAID) is a multi-agent orchestration system...\n\n**Your task:** Implement the following 2 stub functions..."
```
This is 1500+ chars of context for a simple "implement 2 functions" task.

### Comparison: self_improve vs commit0

| Aspect | self_improve.yaml | commit0.yaml |
|--------|-------------------|--------------|
| Purpose | Code quality improvements | Implement stub functions from paper/repo |
| Instruction length | Unbounded | Unbounded |
| Success metric | files modified | tests pass |
| Delegation style | Same | Same |

Both templates have identical structural issues with `{instruction}` length.

## How to Run Experiment

```bash
# Run commit0 with existing (unfocused) prompts
ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli \
  --task=commit0 \
  --model=minimax/MiniMax-M2.7 \
  --multi_agent=true \
  --max_iterations=100 \
  --sub_iterations=100 \
  --max_subagents 12 \
  --max_rounds_chat 10 \
  --repo=angrysky56/Paper2Code-Enhanced \
  --output_dir outputs/spike001-baseline

# Examine instruction length
cat outputs/spike001-baseline/*/delegations.json | jq '.delegation_plan.first_round.tasks[].instruction | length'
```

## What to Expect

| Metric | Current (unfocused) | Expected (focused) |
|--------|---------------------|-------------------|
| Instruction length | >1500 chars | <500 chars |
| Subagent iterations | 100 (maxed) | <30 |
| Tests passing | Partial/failing | All passing |
| Success rate | Varies | Higher |

## Observability

Track in `outputs.jsonl`:
- `event_type: "agent_response"` with `actual_iterations` vs `max_iterations`
- `event_type: "manager_review"` with `merged: true/false`

## Investigation Trail

### 2026-05-19: Initial Analysis
- User ran commit0 (not self_improve) on Paper2Code-Enhanced repo
- Delegations show empty `{}` in many runs, indicating delegation failure
- When delegation DID work (OrCAID repo), instructions were 1500+ chars

### Next Steps
1. Run baseline experiment on commit0
2. Add `{concise_instruction}` template variable with char limit
3. Modify `build_subagent_prompt()` to truncate if needed
4. Run comparative experiment