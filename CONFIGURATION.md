# Configuration

OrCAID is configured through **environment variables** (`.env`) and **CLI flags**. Environment variables set defaults; CLI flags override them.

```bash
cp .env.example .env
# Edit .env with your API keys and model preferences
```

---

## Environment Variables

### LLM — Manager Agent

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_MODEL` | **Yes** | — | LiteLLM model identifier (e.g., `minimax/MiniMax-M2.7`, `openai/gpt-4o`) |
| `MODEL` | No | Same as `LLM_MODEL` | Alias for `LLM_MODEL` |
| `LLM_API_KEY` | **Yes** | — | API key for the manager model provider |
| `LLM_BASE_URL` | No | Provider default | Base URL for OpenAI-compatible endpoints |

### LLM — Subagent (Optional)

Falls back to the manager model if not set.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_SUBAGENT_MODEL` | No | `LLM_MODEL` | Model for engineer subagents |
| `LLM_SUBAGENT_API_KEY` | No | `LLM_API_KEY` | API key for subagent model |
| `LLM_SUBAGENT_BASE_URL` | No | `LLM_BASE_URL` | Base URL for subagent model |

### Provider-Specific

| Variable | Required | Default | Description |
|---|---|---|---|
| `MINIMAX_API_KEY` | No | — | Used by LiteLLM auto-detection for MiniMax |
| `MINIMAX_API_BASE` | No | — | MiniMax API base for auto-detection |
| `ANTHROPIC_BASE_URL` | No | — | Anthropic-compatible API base URL |
| `ANTHROPIC_API_KEY` | No | — | Anthropic API key |

### Verification Bridge

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORCAID_RETRY_POLICY` | No | `kl` | Bridge retry policy: `kl` (keep-last, default) or `mop` (multi-outcome policy). Used to tag drift logs for A/B analysis. |
| `ORCHESTRATOR_MEMORY_BASE` | No | `~/.orcaid/orchestrator-memory` | Root directory for verification outcomes, drift logs, and discovery index |
| `ORCAID_BRIDGE_STORAGE` | No | `~/.orcaid/bridge` | Auxiliary bridge storage (cached checklists, etc.) |

### Docker / Workspace

| Variable | Required | Default | Description |
|---|---|---|---|
| `SDK_SOURCE_DIR` | No | `../software-agent-sdk` | Path to the OpenHands SDK source checkout |

### Judge (PaperBench Only)

| Variable | Required | Default | Description |
|---|---|---|---|
| `JUDGE_PYTHON` | No | System Python | Python interpreter for the judge subprocess |

### Misc

| Variable | Required | Default | Description |
|---|---|---|---|
| `LITELLM_LOG` | No | — | Set to `DEBUG` for verbose LiteLLM logging |

---

## CLI Flags

All flags are passed to `orcaid/cli.py:main()`. The `orcaid` entry point (from `pyproject.toml`) and `uv run python -m orcaid.cli` are equivalent.

### Common Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--task` | str | — | Task type: `commit0`, `paper2code`, `paperbench`, or `self_improve` |
| `--model` | str | `$LLM_MODEL` | Override the manager model |
| `--subagent_model` | str | `$LLM_SUBAGENT_MODEL` | Override the subagent model |
| `--multi_agent` | bool | `true` | `true` = Manager + parallel engineers; `false` = single-agent baseline |
| `--max_iterations` | int | `50` | Max LLM iterations for the manager |
| `--max_subagents` | int | `2` | Number of parallel engineer subagents |
| `--sub_iterations` | int | `50` | Max LLM iterations per subagent |
| `--max_rounds_chat` | int | `2` | Max task assignment rounds per engineer |
| `--output_dir` | str | auto-generated | Override the output directory path |
| `--patch_target` | str | — | Local git repo path. When set, `patch.diff` is automatically applied to an `orcaid-patch` branch in that repo after pytest completes (commit0 multi-agent only). |

> **Note:** `--rounds_of_chat` is a legacy alias for `--max_rounds_chat`. Both work; `--max_rounds_chat` takes priority if both are supplied.

### Commit0 Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str | — | GitHub repo slug (e.g., `owner/repo`) or absolute local path |
| `--base_branch` | str | auto-detect | Branch to clone (defaults to repo's default branch) |
| `--dataset_path` | str | `data/commit0/commit0_combined` | Path to a local commit0 dataset spec |

### PaperBench Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--paper_id` | str | — | Paper identifier (e.g., `rice`) |
| `--paperbench_dir` | str | — | Override PaperBench data directory |
| `--test_max_depth` | int | `999` | Maximum depth for rubric evaluation |
| `--test_reproduce_timeout` | int | `300` | Timeout (seconds) for reproduction scripts |
| `--judge_type` | str | `simple` | Judge strategy: `simple` or `llm` |
| `--judge_model` | str | `gpt-5-mini` | Model for LLM judge |
| `--code_dev` / `--nocode_dev` | bool | `true` | Enable/disable code development mode |

---

## Model Routing

OrCAID uses [LiteLLM](https://docs.litellm.ai/) for multi-provider routing. Model identifiers follow the pattern `provider/model-name`:

| Provider | Prefix | Example |
|---|---|---|
| MiniMax | `minimax/` | `minimax/MiniMax-M2.7` |
| OpenAI | `openai/` | `openai/gpt-4o` |
| Anthropic | `anthropic/` | `anthropic/claude-opus-4-5` |

**MiniMax note:** Models using the Anthropic-compatible endpoint (`https://api.minimax.io/anthropic`) are automatically rerouted to the OpenAI-compatible endpoint (`https://api.minimax.io/v1`) by `build_llm_kwargs()` in `orcaid/core/utils.py` for consistent behavior.

---

## Orchestrator Memory Layout

```
~/.orcaid/orchestrator-memory/
├── verified/        # YAML files for each verified subagent outcome
├── drift_logs/      # Markdown reports for failed verifications
├── escalations/     # Items requiring human review
└── index/
    └── discovery.yaml  # Aggregated performance stats (updated by cron)
```

Override with `ORCHESTRATOR_MEMORY_BASE`. To migrate from a previous `~/.hermes/` path:

```bash
export ORCHESTRATOR_MEMORY_BASE=~/.hermes/orchestrator-memory
```

---

## Cron Job

The `orcaid-verification-indexer` console script sweeps orchestrator memory and rebuilds the discovery index. Add it to crontab to run every 6 hours:

```bash
0 */6 * * * /path/to/OrCAID/.venv/bin/orcaid-verification-indexer
```

This updates `discovery.yaml` with task type completion/failure counts, per-profile drift rates, and last outcome timestamps. The Manager reads this via `discovery_scan_for_orcaid()` before each run.

Without the cron job, verification still fires per-subagent — the index just stays empty until the first sweep.

---

## Prompt Templates

Templates are stored as YAML in `prompts/` and loaded by `load_prompts()` in `orcaid/core/utils.py`. Each key is a named prompt string with Python `.format()` placeholders.

| File | Task | Keys |
|---|---|---|
| `prompts/commit0.yaml` | commit0, self_improve | `user_instruction`, `scan_analysis`, `task_delegation`, `assign_task`, `single_agent_instruction`, `subagent_prompt`, `followup_prompt`, `conflict_resolution`, `auto_reassign`, `background_exploration`, `manager_final_review_all` |
| `prompts/paperbench.yaml` | paperbench | Same key structure, tailored for paper reproduction |
