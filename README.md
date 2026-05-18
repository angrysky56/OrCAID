# OrCAID: Orchestrated Delegation with Self-Healing Verification闭环

**OrCAID** (Orchestrated Centralized Asynchronous Isolated Delegation) is a multi-agent execution engine built on the OrCAID principle: **completion stated → automated checklist verification → orchestrator-memory update → all-clear or loop-back with correction context.**

It runs on Hermes Agent as the execution layer. The verification闭环 and orchestrator-memory are the self-healing guardrails that make delegation truly autonomous rather than fire-and-forget.

For full setup instructions (prerequisites, three-repo stack, orchestrator-memory, cron job, env vars, verification hooks), see [SETUP.md](SETUP.md). This document covers the architecture, patterns, and extension points.

---

## Architecture

```
User (or Hermes as orchestrator)
    |
    v
OrCAID Manager (scan_analysis → delegation_plan)
    |
    v
Parallel Engineer Subagents (git worktree isolation)
    |
    v
collect_and_merge() ──→ _verify_and_return()
                           |
                     ┌─────┴─────┐
                     v           v
              [PASS]          [FAIL/DRIFT]
                     |           |
              write to         escalate to human
              orchestrator-    OR re-invoke with
              memory/verified/ correction_context
                     |
                     v
              discovery_scan_for_orcaid()
              (called next iteration before scan_and_analyze)
```

**Five files, four concepts:**

| File                            | Purpose                                                   |
| ------------------------------- | --------------------------------------------------------- |
| `core/manager.py`               | Manager agent — orchestrates the full workflow            |
| `core/subagent.py`              | Engineer subagent spawner — one worktree per subagent     |
| `orcaid_verification_bridge.py` | The self-healing layer — verification + drift correction  |
| `config.py`                     | SubAgentResult dataclass (26 fields), SubAgent config     |
| `orcaid` CLI                    | Entry point — sets up LLM, workspace, task, runs workflow |

---

## The Self-Healing Verification Loop

Every subagent result goes through `Manager._verify_and_return()` which calls `orcaid_verification_bridge.verify_subagent_completion()`:

```
SubAgentResult → bridge.scores_output() → delegation-verification checklist
                                                        │
                                          ┌─────────────┼─────────────┐
                                          v             v             v
                                       PASS           FAIL         ESCALATE
                                          │             │             │
                              write to         write to         escalate
                              verified/        drift_logs/       to human
                              + index update   + correction_ctx
                                          │
                                          v
                              Cron job (every 6h) → discovery.yaml populated
                                          |
                                          v
                              discovery_scan_for_orcaid() called next iteration
                              → manager gets gap context before planning
```

**Graceful degradation:** if `orcaid_verification_bridge` fails to import, `_verify_and_return` skips and returns `review_result` unchanged. OrCAID runs fine without the bridge.

---

## The Critical Delegation Question

From the LLM-WIKI research on higher-level users: **"What can you delegate? Not tasks — patterns. What do you do manually that could be automated in your activities?"**

The answer isn't "delegate this specific coding task." It's: "Create conditions where your agents watch for recurring failure patterns and route them back with correction context — without being told."

OrCAID with verification闭环 is built for this:

| Manual Pattern                               | Delegation Pattern                                          |
| -------------------------------------------- | ----------------------------------------------------------- |
| You notice subagent keeps missing edge cases | → drift_log captures it, next run gets correction_context   |
| You manually re-run after failures           | → \_verify_and_return re-invokes with drift fix             |
| You track what task types succeed/fail       | → orchestrator-memory index + cron sweep                    |
| You update agent prompts based on failures   | → discovery_scan_for_orcaid() feeds gaps back into planning |

**You don't fix the agent — you fix the conditions so the agent fixes itself.**

---

## Hermes Agent Integration

### Hook 1: collect_and_merge() → \_verify_and_return() — ALREADY PATCHED

Three of five return points in `Manager.collect_and_merge()` call `_verify_and_return()`:

- **Line 678**: branch merge success → verify
- **Line 710**: worktree commit+merge success → verify
- **Line 731**: no changes, no commit, no worktree → verify

Skipped: conflict branch (line 656), error set (line ~715)

### Hook 2: discovery_scan_for_orcaid() — WIRED

`discovery_scan_for_orcaid()` is called at the start of `Manager.scan_and_analyze()` (manager.py line 237-244). Prior gaps from `discovery.yaml` are injected into the analysis context before planning, so the manager avoids repeating known failure patterns.

### Hook 3: Cron job — ACTIVE

`orcaid-verification-indexer` runs every 6h (job_id: `297092f3b347`), sweeps verified/ and drift_logs/, updates `discovery.yaml`.

---

## Use Cases

### 1. Research Reproduction Agent Pool

Multiple researcher subagents tackle independent sub-tasks of a paper reproduction. Each one verifies: submission exists, reproduce script exists, dependencies documented. Failed runs get correction context for retry.

### 2. Code Review Pipeline

Engineer subagents implement functions. Verification checklist: commit made, files created, tests pass, style ok. Drift rates per task_type inform future delegation strategy.

### 3. Autonomous Bug Triage

Subagents investigate different bug clusters. Verification: reproduction script exists, root cause identified, fix attempted. Escalation if verification fails 3x.

### 4. LLM-WIKI Research Cascade

`wiki-researcher` ingests papers → verification bridge scores whether the ingestion was useful (entity extracted, synthesis triggered) → drift detection if a type of paper keeps producing shallow output → discovery scan feeds back into the next research cycle.

---

## Entry Points

### Full multi-agent run:

```bash
cd /home/ty/Repositories/ai_workspace/OrCAID
uv run orcaid \
  --task commit0 \
  --model <your-model> \
  --subagent_model <subagent-model> \
  --max_iterations 50 \
  --max_subagents 4 \
  --sub_iterations 50 \
  --max_rounds_chat 2
```

### Single-agent baseline (for comparison):

```bash
uv run orcaid --task commit0 --model <model> --single_agent
```

---

## Adding New Tasks

1. Create `tasks/my_task.py` with `MyTaskConfig` + `MyTask` class extending `TaskModule`
2. Implement six methods: `get_docker_image()`, `get_work_dir()`, `get_workspace_config()`, `load_task_data()`, `setup_workspace()`, `evaluate()`
3. Register in `tasks/__init__.py`

---

## Extending the Verification Bridge

`orcaid_verification_bridge.py` has two extension points per SubAgentResult:

**checklist_code_review.yaml** — for code implementation tasks
**checklist_research_reproduction.yaml** — for paper reproduction tasks

Add new checklists as needed and wire them in `verify_subagent_completion()`.

---

## Environment Variables

```bash
export LLM_BASE_URL=<your-proxy-url>
export LLM_API_KEY=<your-api-key>
export ORCHESTRATOR_MEMORY_BASE=~/.hermes/orchestrator-memory  # optional, default
```

---

## Dependencies

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) (package manager)
- Docker (required by OpenHands workspace)
- Dependencies: `uv sync`
