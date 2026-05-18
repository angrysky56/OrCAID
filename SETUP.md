# OrCAID Setup

This covers the full OrCAID stack: Hermes Agent (orchestrator), OrCAID (execution engine), and orchestrator-memory (persistent state). For architecture and delegation patterns, see [AGENTS.md](AGENTS.md). For CLI usage, see [README.md](README.md).

---

## Prerequisites

| Dependency | Version | Notes |
|---|---|---|
| Python | ≥ 3.12 | |
| [uv](https://docs.astral.sh/uv/) | any recent | Package manager |
| Docker | latest | Required by OpenHands workspace |
| Hermes Agent | any recent | Runs OrCAID as a sub-process |
| Git | any recent | Worktree isolation per subagent |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/your-org/OrCAID.git
cd OrCAID
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your LLM credentials:
#   LLM_BASE_URL=https://your-proxy-url/v1
#   LLM_API_KEY=sk-...
```

OrCAID requires `LLM_BASE_URL` and `LLM_API_KEY`. Both are read from the environment at runtime — never hardcode them.

### 3. Run a task

```bash
# Multi-agent (default — Manager + up to 4 engineer subagents):
uv run orcaid \
  --task commit0 \
  --model <your-model> \
  --subagent_model <subagent-model> \
  --max_iterations 50 \
  --max_subagents 4 \
  --sub_iterations 50 \
  --max_rounds_chat 2

# Single-agent baseline (for comparison):
uv run orcaid --task commit0 --model <model> --single_agent
```

---

## The Three Repos and Their Relationships

```
┌─────────────────────────────────────────────────────────────┐
│                     Hermes Agent (you)                       │
│  CEO/driver — spawns OrCAID, reads discovery.yaml,          │
│  adjusts delegation strategy based on gap context           │
└──────────────────────────┬──────────────────────────────────┘
                           │ spawns as subprocess
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                        OrCAID                               │
│  Multi-agent execution engine                               │
│  Manager orchestrates parallel Engineer subagents           │
│  in isolated git worktrees                                  │
│                                                             │
│  After each subagent completion:                            │
│    collect_and_merge() → _verify_and_return()               │
│      → orcaid_verification_bridge.verify_subagent_completion│
│        → writes to orchestrator-memory/                     │
└──────────────────────────┬──────────────────────────────────┘
                           │ cron job every 6h
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   orchestrator-memory                       │
│  ~/.orcaid/orchestrator-memory/                             │
│                                                             │
│  skills/         ← verified outcome skill files             │
│  drift_logs/     ← failed runs with correction context      │
│  escalations/    ← items flagged for human review           │
│  index/discovery.yaml  ← aggregated drift rates + gaps      │
└─────────────────────────────────────────────────────────────┘
```

**Hermes Agent** drives. **OrCAID** executes. **orchestrator-memory** remembers.

---

## Orchestrator Memory

OrCAID writes every subagent result to disk after scoring. This is the persistent state layer that enables self-healing across runs.

```bash
~/.orcaid/orchestrator-memory/
├── skills/                    # Verified outcome skill files
│   └── {task_type}/
│       └── {task_id}__{timestamp}.md
├── drift_logs/               # Failed runs with correction context
│   └── {task_id}__{attempt}__{timestamp}.md
├── escalations/              # Items flagged for human review
└── index/
    └── discovery.yaml         # Aggregated stats (updated by cron job)
```

The directory is created automatically on first run. Override the base path:

```bash
export ORCHESTRATOR_MEMORY_BASE=/path/to/custom/location
```

> **Migrating from `~/.hermes/`:** If you have existing data at `~/.hermes/orchestrator-memory`,
> either move it to `~/.orcaid/orchestrator-memory` or set
> `ORCHESTRATOR_MEMORY_BASE=~/.hermes/orchestrator-memory` in your `.env`.

---

## Cron Job — Discovery Index Updater

A cron job runs every 6 hours to sweep orchestrator-memory and rebuild the discovery index:

```
Name:    orcaid-verification-indexer
Job ID:  297092f3b347
Schedule: 0 */6 * * * (every 6 hours)
Skills:  orcaid-verification-bridge, delegation-verification
Delivery: origin (current chat)
```

**What it does:** Reads all verified outcomes and drift logs → computes drift rates per task type → writes `discovery.yaml`. The next OrCAID run reads this index via `discovery_scan_for_orcaid()` before planning, so the Manager knows which task types historically fail and why.

Create it via Hermes:

```
/cron create orcaid-verification-indexer \
  --skill orcaid-verification-bridge \
  --skill delegation-verification \
  --schedule "0 */6 * * *" \
  --deliver origin \
  --repeat 360
```

Or via the cronjob tool:

```python
cronjob(action='create',
        name='orcaid-verification-indexer',
        job_id='297092f3b347',
        skills=['orcaid-verification-bridge', 'delegation-verification'],
        schedule='0 */6 * * *',
        deliver='origin',
        repeat=360,
        prompt='Sweep orchestrator-memory, update discovery.yaml')
```

Without the cron job, verification still fires per-subagent (drift logs and verified outcomes are written immediately). The index just stays empty until the first sweep.

---

## Hermes Agent Integration

When Hermes runs OrCAID as a subprocess, the pattern is:

```bash
Hermes (you)
  → spawn OrCAID via terminal(background=True)
  → OrCAID Manager delegates to engineer subagents
  → _verify_and_return() fires on each completion
  → bridge writes to orchestrator-memory
  → cron job sweeps every 6h → discovery.yaml updated
  → next run: discovery_scan_for_orcaid() injects gap context
  → Manager knows which task types drift, adjusts delegation
```

The `gsd/orcaid` skill (in Hermes) covers how to load and invoke OrCAID from within Hermes:

```
trigger: "Run OrCAID | add OrCAID task | extend OrCAID | wire verification hook"
```

Load it with: `/skill load orcaid`

---

## Meta-Harness — Evolving OrCAID Itself

The [meta-harness](https://github.com/your-org/meta-harness) repo evaluates model harnesses. One of its domains is `orcaid` — it treats OrCAID's delegation patterns as the subject being evolved. This is separate from running OrCAID as an execution engine.

```
meta-harness --domain orcaid  → evaluates OrCAID's delegation behavior
uv run orcaid                 → runs OrCAID as an execution engine
```

If you want to evolve OrCAID's own prompting or manager behavior:

```bash
cd /path/to/meta-harness
uv run python run_evolution.py --domain orcaid --iterations N
```

This is optional. OrCAID runs perfectly well without meta-harness.

---

## Entry Points

| Command | What it does |
|---|---|
| `uv run orcaid --task commit0 ...` | Full multi-agent run (Manager + engineers) |
| `uv run orcaid --task commit0 --single_agent` | Single-agent baseline |
| `uv run orcaid --task self_improve ...` | Self-improvement task (OrCAID modifies itself) |
| `uv run orcaid --task paperbench ...` | Paper reproduction task |
| Cron job (every 6h) | Sweeps orchestrator-memory → updates discovery.yaml |

Available tasks: `commit0`, `paperbench`, `self_improve`

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_BASE_URL` | Yes | — | LLM API endpoint |
| `LLM_API_KEY` | Yes | — | LLM API key |
| `ORCHESTRATOR_MEMORY_BASE` | No | `~/.orcaid/orchestrator-memory` | Where verified outcomes and drift logs are written |

---

## Verification Hooks — Applied

`_verify_and_return()` is called at 3 of 5 return points in `collect_and_merge()`:

| Line | Branch | Behavior |
|---|---|---|
| ~656 | Conflict detected | Skip verification (conflict resolution is manual) |
| ~678 | Branch merge success | **Verify** → write to orchestrator-memory |
| ~710 | Worktree commit+merge success | **Verify** → write to orchestrator-memory |
| ~715 | Error already set | Skip verification (nothing to verify) |
| ~731 | No changes, no commit, no worktree | **Verify** → write to orchestrator-memory |

Graceful degradation: if `orcaid_verification_bridge` fails to import, `_verify_and_return()` logs a warning and returns `review_result` unchanged. OrCAID continues without verification.

---

## Verification Checklist

Two task types are defined in `orcaid_verification_bridge.py`:

| Task type | Used for | Pass criteria |
|---|---|---|
| `code_review` | Code implementation | commit_made, files_created, tests_pass, style_ok |
| `research_reproduction` | Paper reproduction | submission_exists, results_verified, dependencies_documented |

Add new task types in the bridge module's `TASK_MODULE_TO_CHECKLIST` dict and define the corresponding checklist inline.

---

## Adding a New Task

1. Create `tasks/my_task.py` with `MyTaskConfig` + `MyTask` extending `TaskModule`
2. Implement: `get_docker_image()`, `get_work_dir()`, `get_workspace_config()`, `load_task_data()`, `setup_workspace()`, `evaluate()`
3. Register in `tasks/__init__.py`
4. Add branch in `run_infer.py` `TASK_TO_CLASS` mapping

See `tasks/commit0.py` and `tasks/paperbench.py` for reference implementations.

---

## Dependencies

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker (for OpenHands workspace)
- Hermes Agent (for the orchestrator layer)
- Git (for worktree isolation)

Install all with:

```bash
uv sync
```