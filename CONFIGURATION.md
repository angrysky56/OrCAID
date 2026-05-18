# Configuration

OrCAID is configured through a combination of **environment variables** (`.env`)
and **CLI flags**. Environment variables set defaults; CLI flags override them.

---

## Quick Start

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
| `MODEL` | No | Same as `LLM_MODEL` | Alias for `LLM_MODEL` (either works) |
| `LLM_API_KEY` | **Yes** | — | API key for the manager model provider |
| `LLM_BASE_URL` | No | Provider default | Base URL for OpenAI-compatible endpoints |

### LLM — Subagent (Optional)

Falls back to the manager model if not set.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_SUBAGENT_MODEL` | No | `LLM_MODEL` | Model for engineer subagents |
| `LLM_SUBAGENT_API_KEY` | No | `LLM_API_KEY` | API key for subagent model |
| `LLM_SUBAGENT_BASE_URL` | No | `LLM_BASE_URL` | Base URL for subagent model |

### Provider-Specific Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MINIMAX_API_KEY` | No | — | Used by LiteLLM auto-detection for MiniMax |
| `MINIMAX_API_BASE` | No | — | MiniMax API base for auto-detection |
| `ANTHROPIC_BASE_URL` | No | — | Anthropic-compatible API base URL |
| `ANTHROPIC_API_KEY` | No | — | Anthropic API key |

### Docker / Workspace

| Variable | Required | Default | Description |
|---|---|---|---|
| `SDK_SOURCE_DIR` | No | `../software-agent-sdk` | Path to the OpenHands SDK source checkout |

### Judge (PaperBench Only)

| Variable | Required | Default | Description |
|---|---|---|---|
| `JUDGE_PYTHON` | No | System Python | Python interpreter for the judge subprocess |

### Orchestrator Memory

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORCHESTRATOR_MEMORY_BASE` | No | `~/.orcaid/orchestrator-memory` | Root directory for verification outcomes, drift logs, and discovery index |
| `ORCAID_BRIDGE_STORAGE` | No | `~/.orcaid/bridge` | Auxiliary storage for the verification bridge (cached checklists, etc.) |

### Misc

| Variable | Required | Default | Description |
|---|---|---|---|
| `LITELLM_LOG` | No | — | Set to `DEBUG` for verbose LiteLLM logging |

---

## CLI Flags

The `orcaid` CLI accepts the following arguments (passed to `cli.py:main()`):

### Common Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--task` | str | — | Task type: `commit0`, `paperbench`, or `self_improve` |
| `--model` | str | `$LLM_MODEL` | Override the manager model |
| `--subagent_model` | str | `$LLM_SUBAGENT_MODEL` | Override the subagent model |
| `--max_iterations` | int | `50` | Max LLM iterations for the manager |
| `--max_subagents` | int | `2` | Number of parallel engineer subagents |
| `--sub_iterations` | int | `80` | Max LLM iterations per subagent |
| `--rounds_of_chat` | int | `2` | Number of assign-and-execute rounds |

### Commit0 Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str | — | Target repository name (e.g., `minitorch`) |
| `--dataset_path` | str | — | Path to a dataset spec file |

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

OrCAID uses [LiteLLM](https://docs.litellm.ai/) for multi-provider model
routing. The model identifier follows the pattern:

```
provider/model-name
```

### Supported Providers

| Provider | Prefix | Example |
|---|---|---|
| MiniMax | `minimax/` | `minimax/MiniMax-M2.7` |
| OpenAI | `openai/` | `openai/gpt-4o` |
| Anthropic | `anthropic/` | `anthropic/claude-opus-4-5` |

### MiniMax Provider Note

MiniMax models using the Anthropic-compatible endpoint
(`https://api.minimax.io/anthropic`) are automatically rerouted through the
OpenAI-compatible endpoint (`https://api.minimax.io/v1`) by the `build_llm_kwargs()`
function in `orcaid/core/utils.py`. This ensures consistent behavior across
the thinking/reasoning split feature.

---

## Orchestrator Memory Layout

The verification bridge persists state to the orchestrator memory directory:

```
~/.orcaid/orchestrator-memory/
├── verified/        # YAML files for each verified subagent outcome
├── drift_logs/      # Markdown reports for failed verifications
├── escalations/     # Items requiring human review
└── index/
    └── discovery.yaml  # Aggregated performance stats (updated by cron)
```

Override with `ORCHESTRATOR_MEMORY_BASE` environment variable.

> **Migrating from `~/.hermes/`:** If you previously used the `~/.hermes/orchestrator-memory`
> path, set `ORCHESTRATOR_MEMORY_BASE=~/.hermes/orchestrator-memory` in your `.env` to
> preserve your existing data.

---

## Cron Job Configuration

The `orcaid-verification-indexer` console script sweeps orchestrator memory
every 6 hours:

```bash
# Add to crontab:
0 */6 * * * /path/to/.venv/bin/orcaid-verification-indexer
```

This updates `discovery.yaml` with:
- Task type completion/failure counts
- Per-profile drift rates
- Last outcome timestamps

The Manager reads this index via `discovery_scan_for_orcaid()` before each run.

---

## Prompt Templates

Prompt templates are stored as YAML files in the `prompts/` directory:

| File | Task Type | Contents |
|---|---|---|
| `prompts/commit0.yaml` | Commit0 | `scan_analyze`, `onboard`, `assign_task`, `followup`, `manager_final_review_all`, `background_exploration` |
| `prompts/paperbench.yaml` | PaperBench | Same keys, tailored for paper reproduction |

Templates use Python `.format()` placeholders (e.g., `{repo_dir}`, `{engineer_id}`).
