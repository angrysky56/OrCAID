# OrCAID Agent Architecture

> **OrCAID** = Orchestrated Centralized Asynchronous Isolated Delegation.
> A state-of-the-art multi-agent execution engine where a Manager delegates parallel tasks to isolated Engineer subagents running inside separate Git worktrees, governed by a self-healing verification closed-loop (闭环) at every handoff.

---

## Agent Roles

### Manager (Core Orchestrator)

The Manager acts as the orchestrator and does **not** write code directly. Instead, it coordinates task flow, delegates sub-tasks, manages git integration, and reviews completed work.

#### Core Workflow Methods:
1. `scan_and_analyze()` — Scans the source repository and calls the LLM to generate a structured task decomposition (Task Graph).
2. `delegate_tasks()` — Plans dependencies, allocates leaf tasks to Engineer subagents, and schedules execution.
3. `onboard_subagents()` — Prepares the isolated git worktree environment for each subagent.
4. `run_subagents_parallel()` — Spawns parallel `SubAgentRunner` instances concurrently.
5. `collect_and_merge()` — Collects results, performs git branch merges, and calls the self-healing hook `_verify_and_return()`.
6. `final_review_all()` — Conducts a high-level review of all merged and unmerged outcomes to compile the final patch.

#### Key Self-Healing Hook:
* `_verify_and_return()` (located in `orcaid/core/manager_review.py`) — Wires directly into the **Verification Bridge** to score completed subagent work against checklists, write outcomes to persistent memory, and dynamically trigger auto-retries on criteria drift.

---

### Engineer Subagents (Workers)

Engineer Subagents are worker instances managed by `SubAgentRunner` (in `orcaid/core/subagent.py`) that perform the actual codebase edits.

#### Core Characteristics:
* **Isolation**: Each engineer runs in a completely isolated git worktree with its own dedicated branch.
* **Hermes Profile Mapping**:
  * `engineer_1` ➔ `coder`
  * `engineer_2` ➔ `coder`
  * `engineer_3` ➔ `researcher`
  * `engineer_4` ➔ `reviewer`
* **Task Specialization**:
  * `commit0`: Fills missing code stubs and satisfies package/unit tests.
  * `paperbench`: Reproduces scientific ML research paper experiments.
  * `self_improve`: Automatically refactors and improves placeholders in the OrCAID framework.

---

### Verification Bridge (Self-Healing Layer)

An intelligent, non-agent hook layer implemented in `orcaid/bridge.py` that connects OrCAID to delegation-verification systems.

#### Three Core Hook Points:

| Hook Point | Invocation Spot | Primary Responsibility |
|:---|:---|:---|
| `verify_subagent_completion()` | Inside `_verify_and_return()` | Scores `SubAgentResult` against checklists, detects logical drift, schedules retries, or triggers escalations. |
| `discovery_scan_for_orcaid()` | Before `scan_and_analyze()` | Reads historic failures and gaps from `discovery.yaml` and injects them back into the Manager's planning context. |
| `synthesize_orcaid_outcome()` | Inside `final_review_all()` | Synthesizes overall performance, generates compound skills, and records final verified output. |

#### Advanced Features:
* **Three-Bond Drift Classification**: Incorporates `orcaid.bond_classifier` to group failures into semantic structural gaps (e.g. missing package bindings, logic mismatch, verification failures).
* **Graceful Degradation**: If the bridge module or dependencies are absent, `_verify_and_return()` logs a warning and returns the default result without interrupting execution.
* **Persistent Memory**: Storage resides at `~/.hermes/` (overrideable via `ORCHESTRATOR_MEMORY_BASE` and `ORCAID_BRIDGE_STORAGE` env vars).

---

## State Machine & Execution Flow

```
[User/Cli] ➔ OrCAID CLI (orcaid.cli)
    ➔ Manager.__init__()
    ➔ Manager.run() 
        ➔ scan_and_analyze()             [LLM creates Task Graph]
          ➔ discovery_scan_for_orcaid()   [Injects prior failure gap context]
        ➔ delegation_plan               [Calculates task dependencies and weights]
        ➔ delegate_tasks()              [Assigns tasks to engineers]
        ➔ onboard_subagents()           [Creates isolated git worktrees]
        ➔ run_subagents_parallel()      [Spawns SubAgentRunners in parallel]
            ➔ [SubAgent completes task and commits inside worktree]
        ➔ collect_and_merge()           [Performs git branch merges]
            ➔ _verify_and_return()      [Invokes Verification Bridge]
                ➔ PASS: Writes verified outcomes to memory Base ➔ continues
                ➔ FAIL: Writes drift_log, applies correction context, queues auto-retry
                ➔ ESCALATE: Flags for human review ➔ stops subagent execution
        ➔ final_review_all()            [Reviews patches ➔ generates outputs/patch.diff]
```

---

## Self-Healing Loop & Memory System

### The "Higher-Level User" Philosophy:
Lower-level delegation relies on telling agents exactly what to do step-by-step. OrCAID implements **higher-level pattern-based delegation**: the system actively observes failure patterns, learns from criteria drifts, and injects correction context autonomously without manual intervention.

```
SubAgent Completes ➔ _verify_and_return() fires
    ➔ Scores output against YAML checklist
    ➔ PASS: Logs to ~/.hermes/orchestrator-memory/verified/
    ➔ FAIL: Writes drift_log + correction_context ➔ triggers auto-retry
    ➔ Cron Indexer Sweeper: Aggregates logs to index/discovery.yaml every 6 hours
    ➔ Next Run: discovery_scan_for_orcaid() reads index ➔ feeds gaps into Manager's planning context
```

### Orchestrator Memory Map:
```
~/.hermes/orchestrator-memory/
├── verified/         # Successfully completed and verified tasks
├── drift_logs/       # Detailed drift files for failures and re-invocations
├── escalations/      # Tasks flagged for human review after exceeding maximum retries
└── index/
    └── discovery.yaml  # Aggregated stats mapping task types to pass rates and known gaps
```

---

## Technical Specifications

### 1. Checklist Scoring
Checklists are loaded dynamically from `orcaid/checklists/` as structured YAML configurations:
* `checklist_code_review.yaml` — Validates structural code correctness, imports, tests, and syntax.
* `checklist_research_reproduction.yaml` — Validates ML experiment requirements, metrics, and figures.

### 2. SubAgentResult Key Verification Fields
Located in `orcaid/config.py`:
* `success` (`bool`): Direct status of subagent's execution loop.
* `commit_hash` (`str`): Git commit ID generated by the subagent.
* `files_modified` (`list[str]`): List of paths modified by the worker.
* `git_diff` (`str`): The raw diff representing the subagent's contributions.
* `error` (`str`): Captured exception trace or test failure logs.

---

## Directory Structure & Key Files

```
OrCAID/
├── orcaid/
│   ├── cli.py               # Framework entry point and CLI command parsing
│   ├── config.py            # Workflow configurations and SubAgentResult dataclass
│   ├── bridge.py            # Self-healing verification bridge & hook implementations
│   ├── bond_classifier.py   # Three-bond semantic failure classification
│   ├── checklists/          # Validation checklist YAML files
│   ├── core/
│   │   ├── manager.py       # Manager class and run orchestration
│   │   ├── manager_review.py# collect_and_merge() and self-healing hooks
│   │   ├── subagent.py      # SubAgentRunner and execution lifecycle
│   │   └── utils.py         # Subprocess, token counting, and LLM utilities
│   └── tasks/
│       ├── base.py          # Base TaskModule class definition
│       ├── commit0.py       # Commit0 benchmark task implementation
│       ├── paperbench.py    # Paperbench task implementation
│       └── self_improve.py  # Self-improvement/refactoring task
└── scripts/
    ├── apply_patch.py       # Automated script to apply & commit OrCAID patches
    ├── run_multi.sh         # Shell script helper for multi-agent runs
    └── run_single.sh        # Shell script helper for single-agent runs
```