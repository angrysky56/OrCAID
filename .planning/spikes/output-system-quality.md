---
name: output-system-quality
description: Spike investigating OrCAID output system chaos - unreadable logs, misaligned prompts, uncoordinated agents
metadata:
  type: spike
  created: 2026-05-19
  status: completed
---

# Spike: OrCAID Output System Quality

## Problem Statement

User reports the output system is "uncoordinated, prompts are not aligned, agents and subagents find no tasks, output is unreadable - mixing logs and responses in a vast document full of extra characters, unclean and impossible to evaluate."

## Investigation Summary

### 1. Output Structure Observed

| Run Config | Files Created | Issues |
|------------|---------------|--------|
| `manageriters=1, subagents=1, subiters=1` | 5 outputs | delegation_plan empty, no work done |
| `manageriters=3, subagents=2, subiters=20` | 5 outputs | delegation_plan empty, no work done |
| `manageriters=30, subagents=2, subiters=30` | 5 outputs + events | **4 files modified**, subagents failed with "MaxIterationsReached" |

### 2. Key Findings

**Finding 1: Delegation Plan Empty**
```json
// From delegations.json - ALL runs show:
{ "delegation_plan": {} }
```
The manager claims to have delegated work but the delegation plan is empty. This is a critical coordination failure.

**Finding 2: Subagent Instructions Are Massive and Unfocused**
Example from `manageriters=30` run:
- `bridge.py` task: 1581 lines, instructions to "add docstrings", "fix empty pass blocks at ~345, ~1080, ~1232, ~1251, ~1382"
- `core/utils.py` task: 1515 lines, similar docstring/pass block focus
- Instructions include entire repo context, making them unfocused

**Finding 3: Agents Hit MaxIterations Without Completing Work**
The subagents (`engineer_1`, `engineer_2`) both failed with `MaxIterationsReached` (30 iterations limit). Example log shows:
- Agent repeatedly trying to fix the same `strict=False` bug in `json.loads()`
- Permission denied when trying to `git checkout` to revert
- Final result still marked as "failed" (`"status": "failed"`) in delegation events

**Finding 4: Output Format Is Narrative-Heavy**
Logs show:
```
╭──────────────────────────────── Agent Action ────────────────────────────────╮
│                                                                              │
│ Predicted Security Risk: LOW                                                 │
│                                                                              │
│ Summary: View utils.py content around line 660                               │
```

This `╭──╮` boxed format is mixed with the actual code, making parsing difficult.

**Finding 5: Success False Positive**
```json
// result.json
{ "success": true, "files_modified": [...], "duration": 0.229998 }
```
Despite subagents failing and no files actually being merged (`files_merged: 0`), `success: true` is reported.

## Root Causes

1. **Prompt misalignment**: Instructions don't clearly communicate what's expected or how to succeed
2. **Task assignment failure**: Subagents not finding/claiming tasks properly
3. **Iteration mismanagement**: Agents hit limits before completing, with no graceful degradation
4. **Result reporting confusion**: Success is claimed even when actual work failed
5. **Output pollution**: Log format mixes structured events with human-readable narrative boxes

## Hypotheses to Test

| # | Hypothesis | Test Approach |
|----|------------|---------------|
| H1 | Delegation plan empty because manager skips delegation when no clear tasks identified | Add task identification phase before delegation |
| H2 | Subagents fail because instructions are too long/unfocused (1581 line files + massive context) | Shorten instructions, make them action-oriented |
| H3 | MaxIterations reached because agents enter fix loops without exit criteria | Add explicit completion signals and iteration budget management |
| H4 | Success=true but files_merged=0 indicates result aggregation bug | Fix result.json generation to reflect actual outcomes |
| H5 | Boxed log format (`╭──╮`) makes programmatic parsing impossible | Normalize to structured JSON-only output |

## Experiment Design

To validate H1-H5, create a minimal test:
1. Run manager with a single, very clear task (e.g., "add one docstring to function X")
2. Verify delegation_plan is populated
3. Verify subagent receives focused, short instruction (<500 chars)
4. Track iterations vs work done ratio
5. Check success reflects actual file modifications

## Related Files Examined

- `outputs/self_improve/MiniMax-M2.7/multi-agent/manageriters=30_subagents=2_subiters=30_rchats=2/` (primary)
- `outputs/self_improve/MiniMax-M2.7/multi-agent/manageriters=1_subagents=1_subiters=1_rchats=1/` (control)
- `outputs/self_improve/MiniMax-M2.7/multi-agent/manageriters=3_subagents=2_subiters=20_rchats=2/` (comparison)