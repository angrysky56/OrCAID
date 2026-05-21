# OrCAID

**OrCAID** (Orchestrated Centralized Asynchronous Isolated Delegation) is a multi-agent execution engine. A Manager agent scans a repository, decomposes work, and delegates to parallel Engineer subagents running in isolated git worktrees. Results are collected, merged, and verified through a self-healing loop before a final patch is generated.

---

## Architecture

```
CLI
 └─ Manager (scan → plan → delegate)
     └─ Parallel Engineer Subagents (isolated git worktrees)
         └─ collect_and_merge()
             └─ _verify_and_return()  ←─ verification bridge
                 ├─ PASS → write to orchestrator-memory/verified/
                 └─ FAIL → write drift_log + correction_context → auto-retry
```

**Core modules:**

| Module | Purpose |
|---|---|
| `orcaid/cli.py` | CLI entry point — wires LLM, workspace, task, workflow |
| `orcaid/core/manager.py` | Manager agent — orchestrates the full workflow |
| `orcaid/core/subagent.py` | Engineer subagent spawner — one git worktree per engineer |
| `orcaid/core/manager_review.py` | `collect_and_merge()` + `_verify_and_return()` self-healing hooks |
| `orcaid/bridge.py` | Verification bridge — checklist scoring, drift detection, memory writes |
| `orcaid/config.py` | `WorkflowConfig`, `SubAgentResult`, and task config dataclasses |
| `orcaid/tasks/` | Task implementations: `commit0`, `paper2code`, `paperbench`, `self_improve` |

---

## Installation

**Prerequisites:** Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), Docker, Git 2.5+

```bash
git clone https://github.com/angrysky56/OrCAID.git
cd OrCAID
uv sync
```

Copy and edit the environment file:

```bash
cp .env.example .env
# Set LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
```

---

## Quick Start

```bash
# Multi-agent run (Manager + parallel engineers):
ORCAID_RETRY_POLICY=kl uv run orcaid \
  --task=commit0 \
  --model=minimax/MiniMax-M2.7 \
  --multi_agent=true \
  --max_iterations=100 \
  --sub_iterations=100 \
  --max_subagents=4 \
  --max_rounds_chat=4 \
  --repo=owner/repo-name

# Single-agent baseline:
uv run orcaid --task=commit0 --model=minimax/MiniMax-M2.7 --multi_agent=false --repo=owner/repo-name

# Auto-apply the generated patch to a local repo after the run:
uv run orcaid --task=commit0 --repo=owner/repo-name \
  --patch_target=/home/user/repos/my-project
```

The run produces a `patch.diff` in `outputs/`. With `--patch_target`, it is automatically committed to an `orcaid-patch` branch in that repo after pytest completes (Step 11).

---

## Applying Patches Manually

Patches can also be applied after the fact with the helper script:

```bash
uv run python scripts/apply_patch.py \
  --patch outputs/commit0/MiniMax-M2.7/my-repo/multi-agent/.../patch.diff \
  --repo-dir /path/to/local/repo \
  --branch orcaid-patch \
  --commit-message "feat: apply OrCAID multi-agent patch"
```

Options: `--patch` / `-p` (required), `--repo-dir` / `-d` (default `.`), `--branch` / `-b` (default `orcaid-patch`), `--commit-message` / `-m`, `--force` / `-f` (apply over uncommitted changes).

---

## Available Tasks

| Task | Flag | Description |
|---|---|---|
| Commit0 | `--task=commit0` | Fix stubs, bugs, and linting in a GitHub repo; evaluate with pytest |
| Paper2Code | `--task=paper2code` | Reproduce a paper's code pipeline |
| PaperBench | `--task=paperbench` | Reproduce ML paper experiments; evaluate with rubric judge |
| Self-Improve | `--task=self_improve` | OrCAID refactors and improves its own codebase |

---

## Self-Healing Verification Loop

Every subagent result goes through `_verify_and_return()` → `bridge.verify_subagent_completion()`:

- **PASS** — writes verified outcome to `~/.orcaid/orchestrator-memory/verified/`
- **FAIL** — writes drift log + correction context → queues auto-retry
- **ESCALATE** — flags for human review

A cron job (optional) sweeps the memory directory every 6 hours and rebuilds `discovery.yaml`. The Manager reads this index at the start of each run via `discovery_scan_for_orcaid()`, so it knows which task types historically fail and why.

Graceful degradation: if `orcaid.bridge` fails to import, `_verify_and_return()` skips silently. OrCAID runs fully without the bridge.

---

## Adding New Tasks

1. Create `orcaid/tasks/my_task.py` with `MyTaskConfig` + `MyTask` extending `TaskModule`
2. Implement: `get_docker_image()`, `get_work_dir()`, `get_workspace_config()`, `load_task_data()`, `setup_workspace()`, `evaluate()`
3. Register in `orcaid/tasks/__init__.py`

See `tasks/commit0.py` and `tasks/paperbench.py` for reference implementations.

---

## Extending the Verification Bridge

`orcaid/bridge.py` has two built-in checklists:

- `checklist_code_review.yaml` — for code implementation tasks
- `checklist_research_reproduction.yaml` — for paper reproduction tasks

Add new checklists in `orcaid/checklists/` and wire them in `bridge.TASK_MODULE_TO_CHECKLIST`.
See [API.md](API.md) for the full bridge function reference.

---

## Environment Variables

```bash
LLM_MODEL=minimax/MiniMax-M2.7        # LiteLLM model identifier
LLM_API_KEY=your-api-key-here         # Provider API key
LLM_BASE_URL=https://api.minimax.io/v1 # OpenAI-compatible endpoint
ORCAID_RETRY_POLICY=kl                 # Bridge retry policy: 'kl' (default) or 'mop'
```

See [CONFIGURATION.md](CONFIGURATION.md) for the full reference.

---

## Documentation

| Document | Description |
|---|---|
| [SETUP.md](SETUP.md) | Prerequisites, installation, orchestrator memory, cron job |
| [CONFIGURATION.md](CONFIGURATION.md) | All env vars, CLI flags, model routing, prompt templates |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module map, data flow, isolation model, self-healing loop |
| [API.md](API.md) | Public interfaces: TaskModule ABC, config dataclasses, bridge functions |
| [AGENTS.md](AGENTS.md) | Agent roles, delegation state machine, orchestrator memory |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, coding standards, commit conventions |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

---

## Dependencies

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) — package manager (`uv sync` installs everything)
- Docker — required for OpenHands workspace sandboxes
- Git 2.5+ — worktree support required
