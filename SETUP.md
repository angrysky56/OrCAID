# OrCAID Setup

Full setup guide: prerequisites, installation, environment config, orchestrator memory, and the optional cron job. For architecture details see [AGENTS.md](AGENTS.md); for all CLI flags see [CONFIGURATION.md](CONFIGURATION.md).

---

## Prerequisites

| Dependency | Version | Notes |
|---|---|---|
| Python | ≥ 3.12 | |
| [uv](https://docs.astral.sh/uv/) | any recent | Package manager |
| Docker | latest | Required by OpenHands workspace |
| Git | ≥ 2.5 | Worktree isolation per subagent |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/angrysky56/OrCAID.git
cd OrCAID
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set:
#   LLM_MODEL=minimax/MiniMax-M2.7
#   LLM_API_KEY=sk-...
#   LLM_BASE_URL=https://api.minimax.io/v1
```

`LLM_MODEL`, `LLM_API_KEY`, and (for non-default providers) `LLM_BASE_URL` are required. Never hardcode keys — they are read from the environment at runtime.

### 3. Run a task

```bash
# Multi-agent (Manager + up to N engineer subagents):
ORCAID_RETRY_POLICY=kl uv run orcaid \
  --task=commit0 \
  --model=minimax/MiniMax-M2.7 \
  --max_iterations=100 \
  --max_subagents=4 \
  --sub_iterations=100 \
  --max_rounds_chat=4 \
  --repo=owner/repo-name

# Single-agent baseline (--multi_agent=false, no subagents):
uv run orcaid --task=commit0 --model=minimax/MiniMax-M2.7 --multi_agent=false --repo=owner/repo-name

# Auto-apply the patch to a local copy of the repo after the run:
uv run orcaid --task=commit0 --repo=owner/repo-name \
  --patch_target=/home/user/repos/my-project
```

---

## System Architecture

```
CLI (uv run orcaid)
 └─ Manager
     ├─ scan_and_analyze()     — pre-scan + LLM task decomposition
     ├─ delegate_tasks()        — assigns work to engineers
     ├─ onboard_subagents()     — creates isolated git worktrees
     ├─ run_subagents_parallel()— spawns SubAgentRunners concurrently
     ├─ collect_and_merge()     — merges branches + _verify_and_return()
     └─ final_review_all()      — integrates unmerged work, generates patch.diff

_verify_and_return() → orcaid.bridge
    ├─ PASS  → ~/.orcaid/orchestrator-memory/verified/
    ├─ FAIL  → drift_logs/ + correction_context → auto-retry
    └─ ESCALATE → escalations/ (human review needed)

Cron job (optional, every 6h)
    └─ orcaid-verification-indexer → rebuilds index/discovery.yaml
       └─ read by discovery_scan_for_orcaid() at start of next run
```

---

## Orchestrator Memory

OrCAID writes every verified subagent result to disk. This is the persistent state layer that enables self-healing across runs.

```
~/.orcaid/orchestrator-memory/
├── verified/                  # Successfully verified task outcomes
│   └── {task_type}/
│       └── {task_id}__{timestamp}.md
├── drift_logs/                # Failed runs with correction context
│   └── {task_id}__{attempt}__{timestamp}.md
├── escalations/               # Items flagged for human review
└── index/
    └── discovery.yaml         # Aggregated stats (updated by cron job)
```

The directory is created automatically on first run. Override the base path:

```bash
export ORCHESTRATOR_MEMORY_BASE=/path/to/custom/location
```

> **Migrating from `~/.hermes/`:** Set `ORCHESTRATOR_MEMORY_BASE=~/.hermes/orchestrator-memory` in your `.env` to preserve existing data.

---

## Cron Job — Discovery Index Updater

The `orcaid-verification-indexer` console script sweeps orchestrator memory and rebuilds the discovery index. Add it to crontab to run every 6 hours:

```bash
crontab -e
# Add:
0 */6 * * * /path/to/OrCAID/.venv/bin/orcaid-verification-indexer
```

**What it does:** reads all verified outcomes and drift logs → computes drift rates per task type → writes `discovery.yaml`. The next OrCAID run reads this via `discovery_scan_for_orcaid()` so the Manager knows which task types historically fail and why.

Without the cron job, verification still fires per-subagent (drift logs and verified outcomes are written immediately). The index just stays empty until the first sweep.

---

## Available Tasks

| Task flag | Description |
|---|---|
| `commit0` | Fix stubs, bugs, and linting in a GitHub repo; evaluate with pytest |
| `paper2code` | Reproduce a paper's code pipeline |
| `paperbench` | Reproduce ML paper experiments; evaluate with rubric judge |
| `self_improve` | OrCAID refactors and improves its own codebase |

---

## Adding a New Task

1. Create `orcaid/tasks/my_task.py` with `MyTaskConfig` + `MyTask` extending `TaskModule`
2. Implement: `get_docker_image()`, `get_work_dir()`, `get_workspace_config()`, `load_task_data()`, `setup_workspace()`, `evaluate()`
3. Register in `orcaid/tasks/__init__.py`

See `tasks/commit0.py` and `tasks/paperbench.py` for reference implementations.

---

## Verification Hooks

`_verify_and_return()` (in `orcaid/core/manager_review.py`) is called at the main success paths in `collect_and_merge()`. It is skipped on conflict and hard-error branches. Graceful degradation: if `orcaid_verification_bridge` fails to import, `_verify_and_return()` logs a warning and returns the result unchanged.

Two checklist types are defined in `orcaid/checklists/`:

| Task type | Used for | Pass criteria |
|---|---|---|
| `code_review` | Code implementation | commit_made, files_created, tests_pass, style_ok |
| `research_reproduction` | Paper reproduction | submission_exists, results_verified, dependencies_documented |

Add new task types by adding a checklist YAML to `orcaid/checklists/` and wiring it in `bridge.TASK_MODULE_TO_CHECKLIST`.

---

## Dependencies

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker (for OpenHands workspace)
- Git ≥ 2.5 (for worktree isolation)

Install all Python dependencies:

```bash
uv sync
```
