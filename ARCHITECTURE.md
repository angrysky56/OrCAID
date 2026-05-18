# Architecture

> OrCAID = **O**rchestrated **C**entralized **A**synchronous **I**solated **D**elegation

This document describes the internal architecture of OrCAID — a multi-agent
execution engine where a **Manager** delegates to parallel **Engineer**
subagents running in isolated git worktrees, with a self-healing verification
loop at every handoff.

---

## High-Level Topology

```
┌──────────────┐
│  User / CLI  │  orcaid CLI entrypoint
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Manager    │  Orchestrator (LLM-powered)
│  ┌────────┐  │  ┌──────────────────────────────────┐
│  │Analysis│──┼─▸│ discovery_scan_for_orcaid()       │
│  │Delegate│  │  │ Reads orchestrator-memory/index/  │
│  │Onboard │  │  │ discovery.yaml for gap injection  │
│  │Collect │  │  └──────────────────────────────────┘
│  │Review  │  │
│  └────────┘  │
└──────┬───────┘
       │ spawns N in parallel
       ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Engineer 1  │  │  Engineer 2  │  │  Engineer N  │
│  (worktree)  │  │  (worktree)  │  │  (worktree)  │
│  ┌────────┐  │  │  ┌────────┐  │  │  ┌────────┐  │
│  │ Docker │  │  │  │ Docker │  │  │  │ Docker │  │
│  │ sandbox│  │  │  │ sandbox│  │  │  │ sandbox│  │
│  └────────┘  │  └──┴────────┘  │  └──┴────────┘  │
└──────┬───────┘                                    │
       │ results                                    │
       ▼                                            ▼
┌──────────────────────────────────────────────────────┐
│              Verification Bridge                     │
│  verify_subagent_completion()                        │
│    ├─ PASS  → write_verified_outcome()               │
│    ├─ FAIL  → write_drift_log() → retry / escalate  │
│    └─ ESCALATE → write to escalations/               │
└──────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────┐
│           Orchestrator Memory                        │
│  ~/.orcaid/orchestrator-memory/                      │
│    ├── verified/       pass records                  │
│    ├── drift_logs/     failure + correction context   │
│    ├── escalations/    human review queue             │
│    └── index/                                        │
│        └── discovery.yaml  aggregated stats          │
└──────────────────────────────────────────────────────┘
```

---

## Module Map

```
OrCAID/
├── orcaid/
│   ├── __init__.py              # Package root, version string
│   ├── cli.py                   # CLI entrypoint & workflow orchestration
│   ├── config.py                # Dataclasses: WorkflowConfig, SubAgent, SubAgentResult
│   ├── bridge.py                # Verification bridge, drift logs, indexer sweep
│   ├── core/
│   │   ├── manager.py           # Manager class (coordinator)
│   │   ├── manager_assignment.py  # AssignmentMixin — performance-aware task routing
│   │   ├── manager_exploration.py # ExplorationMixin — background codebase scouting
│   │   ├── manager_git.py       # GitMixin — branch merge, stash, worktree commit
│   │   ├── manager_review.py    # ReviewMixin — collect_and_merge, _verify_and_return
│   │   ├── subagent.py          # SubAgentRunner — Docker sandbox + LLM execution
│   │   ├── utils.py             # LLM kwargs builder, event extractors, metrics
│   │   └── patches.py           # Runtime monkey-patches for SDK compatibility
│   ├── tasks/
│   │   ├── base.py              # TaskModule ABC — interface for all task types
│   │   ├── commit0.py           # Commit0Task — function implementation benchmark
│   │   ├── paperbench.py        # PaperbenchTask — research paper reproduction
│   │   └── self_improve.py      # SelfImproveTask — agent self-improvement loop
│   └── judge/                   # PaperBench judge subprocess
├── prompts/
│   ├── commit0.yaml             # Prompt templates for Commit0 task
│   └── paperbench.yaml          # Prompt templates for PaperBench task
├── scripts/
│   ├── run_single.sh            # Single-agent run wrapper
│   └── run_multi.sh             # Multi-agent run wrapper
└── tests/
    └── test_bridge_sweeps.py    # Verification bridge + indexer sweep tests
```

---

## Core Components

### 1. CLI (`orcaid/cli.py`)

The main entrypoint registered as the `orcaid` console script. Orchestrates the
full workflow lifecycle:

1. Parse arguments and load `.env` configuration.
2. Construct `WorkflowConfig` from CLI flags + env vars.
3. Instantiate the task module (`Commit0Task`, `PaperbenchTask`, or `SelfImproveTask`).
4. Build the Docker workspace via the OpenHands SDK.
5. Run the Manager agent with `run_single_agent()`.
6. Collect outputs, run evaluation, and write results.

### 2. Manager (`orcaid/core/manager.py`)

The central orchestrator. The `Manager` class inherits from four mixins:

| Mixin | File | Responsibility |
|---|---|---|
| `AssignmentMixin` | `manager_assignment.py` | Performance-aware task routing using `discovery.yaml` |
| `ExplorationMixin` | `manager_exploration.py` | Background codebase exploration between rounds |
| `GitMixin` | `manager_git.py` | Branch merges, stash/unstash, worktree commits |
| `ReviewMixin` | `manager_review.py` | `collect_and_merge()`, `_verify_and_return()`, `final_review_all()` |

**Lifecycle:**

```
scan_and_analyze()          # LLM decomposes task into a task graph
  → discovery_scan_for_orcaid()   # inject known gaps from prior runs
  → delegation_plan               # task nodes + weights
delegate_tasks()            # assign tasks to engineer IDs
onboard_subagents()         # create worktrees, prepare Docker containers
run_subagents_parallel()    # spawn SubAgentRunners in parallel
collect_and_merge()         # merge worktrees, call _verify_and_return()
final_review_all()          # synthesize results → outputs/
```

### 3. SubAgentRunner (`orcaid/core/subagent.py`)

Each engineer runs in an isolated environment:

- **Git worktree**: a separate working copy of the repository, created per-engineer.
- **Docker sandbox**: the OpenHands runtime container where the LLM agent executes commands.
- **Multi-round**: supports `max_rounds_chat` iterations with feedback loops.

The runner manages conversation state, prompt injection, and result extraction.
On completion, it produces a `SubAgentResult` (26 fields) consumed by the
Manager's `collect_and_merge()`.

### 4. Verification Bridge (`orcaid/bridge.py`)

The self-healing layer. Not an agent — a hook system embedded in `_verify_and_return()`.

| Hook | Where | Purpose |
|---|---|---|
| `verify_subagent_completion()` | After `collect_and_merge` | Score result against checklist, write to memory |
| `discovery_scan_for_orcaid()` | Before `scan_and_analyze()` | Read `discovery.yaml`, inject gaps |
| `synthesize_orcaid_outcome()` | After `final_review_all()` | Write final synthesis to memory |

**Verdicts:**

- **PASS** → `write_verified_outcome()` to `orchestrator-memory/verified/`
- **FAIL** → `write_drift_log()` → retry with `correction_context` or escalate
- **ESCALATE** → write to `orchestrator-memory/escalations/`

**Indexer sweep** (`orcaid-verification-indexer` console script):
Aggregates `verified/` and `drift_logs/` into `index/discovery.yaml`.
Designed to run as a cron job every 6 hours.

### 5. Task Modules (`orcaid/tasks/`)

All tasks implement the `TaskModule` ABC. The interface defines hooks for:

- Docker image selection and workspace setup
- Subagent creation and prompt formatting
- Evaluation and result serialization
- Assignment context and conflict resolution

| Task | Module | Purpose |
|---|---|---|
| `Commit0Task` | `commit0.py` | Implement functions in a target repository |
| `PaperbenchTask` | `paperbench.py` | Reproduce research papers with code |
| `SelfImproveTask` | `self_improve.py` | Agent self-improvement loop |

### 6. Config (`orcaid/config.py`)

Central dataclass definitions:

- **`WorkflowConfig`**: all CLI/env configuration (model, iterations, rounds, etc.)
- **`SubAgent`**: per-engineer assignment state (task ID, file path, functions, instruction)
- **`SubAgentResult`**: 26-field result record produced by each engineer run

---

## Data Flow

```
.env / CLI args
    │
    ▼
WorkflowConfig ──▸ Manager
    │                 │
    │                 ├─ scan_and_analyze() ──▸ LLM ──▸ task graph
    │                 │
    │                 ├─ delegate_tasks() ──▸ SubAgent[]
    │                 │
    │                 ├─ onboard_subagents() ──▸ worktrees + Docker
    │                 │
    │                 ├─ run_subagents_parallel()
    │                 │     └─ SubAgentRunner × N
    │                 │          └─ SubAgentResult
    │                 │
    │                 ├─ collect_and_merge()
    │                 │     └─ _verify_and_return()
    │                 │          └─ bridge hooks
    │                 │
    │                 └─ final_review_all()
    │                       └─ synthesize_orcaid_outcome()
    │
    ▼
outputs/           # Run logs, events, results
orchestrator-memory/  # Verified outcomes, drift logs, discovery index
```

---

## Isolation Model

Each engineer operates in complete isolation:

1. **Git worktree** — separate directory, separate branch.
2. **Docker container** — OpenHands runtime sandbox with file system isolation.
3. **Conversation state** — independent LLM context per subagent.

The Manager is the _only_ entity that:
- Creates and removes worktrees
- Merges branches back to main
- Decides reassignment after conflicts

---

## Self-Healing Loop

```
SubAgent completes
    → _verify_and_return() fires
    → scores against task-specific checklist
    → PASS: write to orchestrator-memory/verified/
    → FAIL: write drift_log + correction_context → re-invoke
    → drift_log grows → indexer sweep (cron 6h) → discovery.yaml updated
    → discovery_scan_for_orcaid() called on next run
    → manager gets gap context before planning
    → subagent gets correction_context on retry
    → pattern automated, self-corrects without intervention
```

---

## Extension Points

### Adding a New Task Type

1. Create `orcaid/tasks/your_task.py` implementing `TaskModule` ABC.
2. Add a config dataclass (e.g., `YourTaskConfig`).
3. Register in `orcaid/tasks/__init__.py`.
4. Add a prompt template in `prompts/your_task.yaml`.
5. Wire into `cli.py` task selection logic.

### Adding a New Verification Checklist

1. Define the checklist in `orcaid/bridge.py` → `TASK_MODULE_TO_CHECKLIST`.
2. Map the task type to the checklist function.

---

## Dependencies

| Package | Role |
|---|---|
| `openhands-sdk` | Agent execution framework |
| `litellm` | Multi-provider LLM routing |
| `pydantic` | Config validation |
| `fire` | CLI argument parsing |
| `rich` | Terminal output formatting |
| `pyyaml` | YAML parsing (prompts, discovery index) |
| `python-dotenv` | Environment file loading |
