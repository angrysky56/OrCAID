# Spike Manifest

## Idea
Fix OrCAID commit0 output system chaos: uncoordinated prompts, agents finding no tasks, unreadable logs mixing responses with extra characters. Three-pronged fix: prompt/template improvements, delegation clarity, and output formatting.

## Context
- **User's actual workflow**: `ORCAID_RETRY_POLICY=kl uv run python -m orcaid.cli --task=commit0 --model=minimax/MiniMax-M2.7 --multi_agent=true --max_iterations=100 --sub_iterations=100 --max_subagents 12 --max_rounds_chat 10 --repo=angrysky56/Paper2Code-Enhanced`
- **self_improve observation**: User noted self_improve is "more simplistic and more like an imitation of commit0" - commit0 is the production-grade task

## Requirements

- Subagent instructions must be focused and action-oriented (<500 chars)
- Delegation plan must be populated with clear, identifiable tasks
- Output must be structured JSON-only (no boxed/narrative logs)
- Success must reflect actual file modifications (not false positives)
- Subagents must be able to properly find and claim tasks

## Spikes

| # | Name | Type | Validates | Verdict | Tags |
|---|------|------|-----------|---------|------|
| 001 | prompt-template-focus | standard | Given a subagent task, when instructions are concise (<500 chars) and action-oriented, then agent completes work in fewer iterations | PENDING | prompt, delegation, quality |
| 002 | delegation-plan-populated | standard | Given a manager analysis, when tasks are identified, then delegation_plan contains structured task objects (not empty) | PENDING | delegation, coordination |
| 003 | json-structured-output | standard | Given a running workflow, when events are emitted, then outputs are parseable JSONL (no boxed format) | PENDING | output, formatting, observability |

## Root Causes Being Addressed

| Root Cause | Hypothesis | Spike(s) |
|------------|-----------|----------|
| Prompt misalignment | H2: Instructions too long/unfocused | 001 |
| Task assignment failure | H1: Manager skips delegation when no clear tasks | 002 |
| Iteration mismanagement | H3: Agents enter fix loops without exit criteria | 001 |
| Result reporting confusion | H4: Success claimed even when work fails | 002 |
| Output pollution | H5: Boxed format makes parsing impossible | 003 |