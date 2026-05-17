# OrCAID Agent Architecture

> OrCAID = Orchestrated Centralized Asynchronous Isolated Delegation.
> A multi-agent execution engine where a Manager delegates to parallel Engineer subagents
> running in isolated git worktrees, with self-healing verification闭环 at every handoff.

---

## Agent Roles

### Manager (Core Orchestrator)

**What it does:**
1. `scan_and_analyze()` — reads the task, calls the LLM to produce a task decomposition (task graph)
2. `delegate_tasks()` — assigns task nodes to engineer subagents based on their profiles
3. `onboard_subagents()` — sends each engineer their specific task requirements and context
4. `run_subagents_parallel()` — spawns all engineers in parallel, each in their own worktree
5. `collect_and_merge()` — waits for completion, merges successful branches back to main
6. `final_review_all()` — synthesizes results from all subagents into a final report

**What it does NOT do:** write code. It coordinates, delegates, and reviews.

**Key method:** `_verify_and_return()` — self-healing hook after each subagent completion. Scores output against delegation-verification checklist, writes verified outcomes to orchestrator-memory, or re-invokes with correction context on drift.

---

### Engineer Subagents (Workers)

**What it does:**
- Runs in an isolated git worktree (one per engineer_id)
- Implements functions (commit0) or reproduces papers (paperbench)
- Communicates back via `SubAgentResult` (26 fields)
- Can be: `engineer_1`, `engineer_2`, `engineer_3`, `engineer_4` (each has a Hermes profile)

**Spawned by:** `SubAgentRunner` in `core/subagent.py`

**Profile mapping (OrCAID → Hermes):**
```
engineer_1 → developer
engineer_2 → debugger
engineer_3 → researcher
engineer_4 → reviewer
unknown → coder
```

---

### Verification Bridge (Self-Healing Layer)

**Not an agent — a hook system** embedded in `orcaid_verification_bridge.py`.

Three hook points:

| Hook | Where | What it does |
|---|---|---|
| `verify_subagent_completion()` | After collect_and_merge returns | Scores SubAgentResult against checklist, writes to orchestrator-memory or escalates |
| `discovery_scan_for_orcaid()` | Before scan_and_analyze() | Reads orchestrator-memory/index/discovery.yaml, injects known gaps into manager context |
| `synthesize_orcaid_outcome()` | After final_review_all() | Writes final outcome to orchestrator-memory/verified/ |

**Graceful degradation:** if the bridge module is missing, `_verify_and_return()` logs a warning and returns `review_result` unchanged. OrCAID continues normally.

---

## Delegation State Machine

```
[User/Hermes] → run_infer.py
    → Manager.__init__()
    → Manager.run() [via run_single_agent()]
        → scan_and_analyze()       [LLM decomposes task into task graph]
          → discovery_scan_for_orcaid() called here (not yet wired — see below)
        → delegation_plan         [task nodes with requirements + weights]
        → delegate_tasks()        [assigns task nodes to engineer_ids]
        → onboard_subagents()     [sends requirements to each subagent]
        → run_subagents_parallel()[spawns SubAgentRunner in parallel]
            → [SubAgent completes]
        → collect_and_merge()     [merges worktrees, calls _verify_and_return()]
            → _verify_and_return() → bridge.verify_subagent_completion()
                → PASS: write to orchestrator-memory/verified/
                → FAIL: write drift_log + correction_context → re-invoke
                → ESCALATE: write to orchestrator-memory/escalations/
        → final_review_all()      [synthesize results → outputs/]
```

---

## The Critical Delegation Question: What Can You Delegate?

From the LLM-WIKI research on higher-level users: **"Getting your agents to discover what you are doing manually that could be automated in your activities vs telling your agents what to do."**

The old delegation model:
```
You → "Do X" → subagent → done
```

The higher-level user model:
```
You → "Watch for pattern P. When you see it, route it back with correction C."
     → system learns P → automates C → you stop doing it manually
```

**Not "delegate this task" — "delegate this pattern."**

### What Hermes (me) Currently Does Manually

These are the patterns I should be delegating to OrCAID + verification闭环:

| Manual Action | Delegation Pattern |
|---|---|
| I notice subagent keeps missing edge cases | → drift_log captures it, correction_context applied automatically next run |
| I manually re-run after failures | → _verify_and_return() re-invokes with drift fix, no human needed |
| I track which task types succeed/fail | → orchestrator-memory index + cron sweep handles this |
| I update agent prompts based on failures | → discovery_scan_for_orcaid() feeds gaps back into planning automatically |
| I decide when to escalate to human | → escalate threshold encoded in verification bridge |
| I notice a new task type and assign it | → discovery.yaml populated by cron → discovery_scan() informs planning |
| I correct a subagent's approach mid-run | → correction_context written to drift_log → applied on retry |

**You don't fix the agent — you fix the conditions so the agent fixes itself.**

### The Self-Healing Delegation Loop

```
SubAgent completes → _verify_and_return() fires
    → scores against checklist
    → PASS: write to orchestrator-memory/verified/
    → FAIL: write drift_log + correction_context → re-invoke
    → drift_log grows → cron job sweeps every 6h → discovery.yaml updated
    → discovery_scan_for_orcaid() called next iteration
    → manager gets gap context before planning
    → subagent gets correction_context on retry
    → pattern automated, you stop doing it manually
```

### The Discovery Gap (Not Yet Wired)

`discovery_scan_for_orcaid()` exists but is not yet called from `scan_and_analyze()`. This is the critical missing link:

- Currently: manual correction happens, gets recorded in drift_log, but the NEXT run doesn't automatically get that context injected
- Once wired: the manager calls discovery_scan_for_orcaid() BEFORE analysis, gets prior gap context, includes it in the delegation prompt

This is literally the pattern: "what you do manually that could be automated in your activities."

To wire it, add to `Manager.scan_and_analyze()`:
```python
from orcaid_verification_bridge import discovery_scan_for_orcaid
gaps = discovery_scan_for_orcaid()
if gaps:
    self.conversation.send_message(f"Prior known gaps:\n" + "\n".join([f"- [{g['task_type']}] {g['description']}" for g in gaps]))
```

### What Makes Someone a "Higher-Level User"

A lower-level user: "Do X task with Y subagent."
A higher-level user: "Build conditions where the system watches for failure patterns and routes them back with correction — without being told."

The difference is:
- Lower-level: explicit instruction every step
- Higher-level: encoded judgment that fires automatically

OrCAID + verification闭环 + orchestrator-memory = the technical implementation of "higher-level user" behavior.

---

## Orchestrator Memory (Persistent State)

Located at `~/.hermes/orchestrator-memory/`:

```
orchestrator-memory/
├── skills/           # Copy of delegation-verification, orchestrator-memory, orcaid-verification-bridge skills
├── drift_logs/       # Per-subagent drift records: what failed, why, correction applied
├── escalations/      # Items flagged for human review after max retries
└── index/
    └── discovery.yaml   # Aggregated stats: task_type → total_verified, drift_rate, last_seen
```

**Updated by:**
- `_verify_and_return()` → writes verified outcomes and drift logs
- `orcaid-verification-indexer` cron job (every 6h) → updates `discovery.yaml` from all records
- `discovery_scan_for_orcaid()` → reads discovery.yaml before next scan_and_analyze

---

## SubAgentResult Schema (26 fields)

Key fields for verification:

| Field | Type | Used by |
|---|---|---|
| `success` | bool | primary pass/fail |
| `commit_hash` | str | verification |
| `files_modified` | list[str] | verification |
| `reproduce_script_exists` | bool | paperbench checklist |
| `submission_exists` | bool | paperbench checklist |
| `requirements` | str | task scope |
| `commit_message` | str | verification |
| `git_commits` | int | verification |
| `git_diff` | str | verification |
| `error` | str | drift detection |

Full dataclass in `config.py` line 175.

---

## Adding Custom Subagents

To add a new subagent type (beyond engineer_1-4):

1. Define profile in `config.py` → `SUBAGENT_PROFILES` dict
2. Add Hermes profile mapping in `ORCAID_TO_HERMES_PROFILE` in `orcaid_verification_bridge.py`
3. Register in `SubAgentResult` dataclass if new result fields needed
4. Create a verification checklist in `~/.hermes/skills/gsd/orcaid-verification-bridge/references/`

---

## Hermes Agent Integration Pattern

Hermes (your AI) acts as the orchestrator on top of OrCAID. The pattern:

```
Hermes (CEO/driver)
    → spawns OrCAID as a background process via terminal(background=True)
    → OrCAID Manager delegates to engineer subagents
    → _verify_and_return() fires on each completion
    → bridge writes outcomes to orchestrator-memory
    → Cron job sweeps every 6h, updates discovery.yaml
    → next Hermes delegation reads discovery.yaml via discovery_scan_for_orcaid()
    → Hermes knows which task types historically drift on which subagent profiles
    → Hermes adjusts delegation strategy accordingly
```

**This is what "higher-level users" means:** not "delegate this task" but "create conditions where your system watches for patterns and self-corrects without being told."

---

## Cron Job

**Name:** `orcaid-verification-indexer`  
**Job ID:** `297092f3b347`  
**Schedule:** `0 */6 * * *` (every 6 hours)  
**Skills:** `orcaid-verification-bridge`, `delegation-verification`  
**Delivery:** origin (current chat)

What it does each tick:
1. Sweeps `orchestrator-memory/verified/` → aggregates pass/fail by task_type
2. Reads `orchestrator-memory/drift_logs/` → computes drift_rate per task_type
3. Reads `orchestrator-memory/escalations/` → pending human review items
4. Writes all to `orchestrator-memory/index/discovery.yaml`

---

## Key Files

```
OrCAID/
├── core/
│   ├── manager.py        # Manager class + _verify_and_return() patch
│   └── subagent.py       # SubAgentRunner + spawn logic
├── orcaid_verification_bridge.py  # Self-healing hook implementation
├── config.py             # SubAgentResult dataclass (26 fields)
├── run_infer.py          # Entry point
└── tasks/
    ├── commit0.py        # Implement functions task
    └── paperbench.py     # Reproduce papers task
```

---

## Extending the Bridge

Add new checklist types in `~/.hermes/skills/gsd/orcaid-verification-bridge/references/`:

- `checklist_code_review.yaml` — for code implementation
- `checklist_research_reproduction.yaml` — for paper reproduction

Wire new checklists in `verify_subagent_completion()` by matching `subagent_result.task_category` or `subagent_result.Requirements` against checklist `metadata.task_type`.