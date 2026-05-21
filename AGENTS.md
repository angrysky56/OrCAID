# OrCAID Agent Architecture

**OrCAID** = Orchestrated Centralized Asynchronous Isolated Delegation. A multi-agent execution engine where a Manager delegates parallel tasks to isolated Engineer subagents running in separate git worktrees, governed by a self-healing verification loop at every handoff.

---

## Agent Roles

### Manager (Core Orchestrator)

The Manager acts as the orchestrator and does **not** write implementation code. It coordinates task flow, delegates sub-tasks, manages git integration, and reviews completed work.

**Core workflow methods:**

1. `scan_and_analyze()` — Runs the initial pre-scan, injects prior failure context from `discovery.yaml`, and calls the LLM to produce a structured task decomposition.
2. `delegate_tasks()` — Plans dependencies, allocates tasks to Engineer subagents, and schedules execution order.
3. `onboard_subagents()` — Creates an isolated git worktree environment for each subagent.
4. `run_subagents_parallel()` — Spawns parallel `SubAgentRunner` instances concurrently.
5. `collect_and_merge()` — Collects results, performs git branch merges, and calls `_verify_and_return()`.
6. `final_review_all()` — Reviews all merged and unmerged outcomes, resolves integration issues, generates `outputs/patch.diff`.

**Key self-healing hook:**

`_verify_and_return()` (in `orcaid/core/manager_review.py`) wires into the Verification Bridge to score completed subagent work against checklists, write outcomes to persistent memory, and trigger auto-retries on criteria drift.

---

### Engineer Subagents (Workers)

Engineer Subagents are worker instances managed by `SubAgentRunner` (in `orcaid/core/subagent.py`) that perform the actual codebase edits.

**Core characteristics:**

- **Isolation:** Each engineer runs in a completely isolated git worktree on a dedicated `feature/engineer-N` branch.
- **Task scope:** Each engineer is assigned specific files and functions — they must not modify files assigned to other engineers.
- **Commit requirement:** Engineers must commit completed work before returning to the manager. Uncommitted work may be rescued during `final_review_all()` but is not guaranteed to be collected.

**Task types:**

| Task | Engineer behavior |
|---|---|
| `commit0` | Fixes stubs, bugs, and linting; runs pytest to verify |
| `paper2code` | Reproduces a paper's code pipeline |
| `paperbench` | Reproduces ML paper experiments |
| `self_improve` | Refactors and improves OrCAID's own codebase |

---

### Verification Bridge (Self-Healing Layer)

An intelligent hook layer implemented in `orcaid/bridge.py` that scores every subagent result against a checklist and writes outcomes to persistent memory.

**Three hook points:**

| Hook | Where | Responsibility |
|---|---|---|
| `verify_subagent_completion()` | Inside `_verify_and_return()` | Scores `SubAgentResult`, detects drift, schedules retries or escalations |
| `discovery_scan_for_orcaid()` | Before `scan_and_analyze()` | Reads `discovery.yaml` and injects historic failure gaps into the Manager's planning context |
| `synthesize_orcaid_outcome()` | Inside `final_review_all()` | Synthesizes overall performance and records the final verified output |

**Graceful degradation:** If `orcaid.bridge` fails to import, `_verify_and_return()` logs a warning and returns the result unchanged. OrCAID runs fully without the bridge.

**Persistent memory:** `~/.orcaid/orchestrator-memory/` (override with `ORCHESTRATOR_MEMORY_BASE`).

---

## Execution Flow

```
[CLI] → run_workflow_inner()
    → Manager.setup_workspace()      [clone repo, install deps, run initial analysis]
    → Manager.scan_and_analyze()     [discovery_scan_for_orcaid() + LLM task graph]
    → Manager.delegate_tasks()       [assign tasks, calculate dependencies]
    → Manager.onboard_subagents()    [create git worktrees]
    → run_subagents_parallel()       [spawn SubAgentRunners]
        → [each engineer: implement → test → commit to feature/engineer-N branch]
    → Manager.collect_and_merge()    [merge branches → _verify_and_return()]
        → PASS:  write verified outcome to orchestrator-memory/verified/
        → FAIL:  write drift_log + correction_context → auto-retry
        → ESCALATE: write to escalations/, stop subagent execution
    → Manager.final_review_all()     [integrate unmerged work, fix integration issues]
    → generate patch.diff            [diff from base commit to final state]
    → pytest evaluation              [run test suite, save results]
    → apply_patch_to_local()         [if --patch_target set: commit patch to local repo]
```

---

## Self-Healing Loop

```
SubAgent completes → _verify_and_return() fires
    → Scores output against YAML checklist
    → PASS:    logs to ~/.orcaid/orchestrator-memory/verified/
    → FAIL:    writes drift_log + correction_context → triggers auto-retry
    → Cron indexer (optional, every 6h):
         aggregates logs → writes index/discovery.yaml
    → Next run: discovery_scan_for_orcaid() reads index
         → Manager gets gap context before planning
```

### Orchestrator Memory Map

```
~/.orcaid/orchestrator-memory/
├── verified/         # Successfully completed and verified tasks
├── drift_logs/       # Detailed drift files for failures and re-invocations
├── escalations/      # Tasks flagged for human review after max retries
└── index/
    └── discovery.yaml  # Aggregated stats: task types → pass rates + known gaps
```

---

## Directory Structure

```
OrCAID/
├── orcaid/
│   ├── cli.py               # Entry point and CLI command parsing
│   ├── config.py            # WorkflowConfig, SubAgentResult, task config dataclasses
│   ├── bridge.py            # Self-healing verification bridge
│   ├── bond_classifier.py   # Semantic failure classification
│   ├── skill_runner.py      # Skill execution utilities
│   ├── checklists/          # Validation checklist YAML files
│   │   ├── checklist_code_review.yaml
│   │   └── checklist_research_reproduction.yaml
│   ├── core/
│   │   ├── manager.py       # Manager class and run orchestration
│   │   ├── manager_review.py# collect_and_merge() and self-healing hooks
│   │   ├── subagent.py      # SubAgentRunner and execution lifecycle
│   │   └── utils.py         # LLM utilities, patch generation, output logging
│   ├── skills/              # Skill definitions
│   └── tasks/
│       ├── base.py          # TaskModule ABC
│       ├── commit0.py       # Commit0 task
│       ├── paper2code.py    # Paper2Code task
│       ├── paperbench.py    # PaperBench task
│       └── self_improve.py  # Self-improvement task
├── prompts/
│   ├── commit0.yaml         # Manager + engineer prompts for commit0/self_improve
│   └── paperbench.yaml      # Manager + engineer prompts for paperbench
└── scripts/
    ├── apply_patch.py       # Manually apply an OrCAID patch.diff to a local repo
    ├── run_multi.sh         # Shell helper for multi-agent runs
    └── run_single.sh        # Shell helper for single-agent runs
```
