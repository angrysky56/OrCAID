# Changelog

All notable changes to OrCAID are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-05-18

### Added

- **Core orchestration engine** — Manager class with LLM-powered task
  decomposition, delegation planning, and multi-round execution.
- **Manager mixin architecture** — modular separation into `AssignmentMixin`,
  `ExplorationMixin`, `GitMixin`, and `ReviewMixin`.
- **SubAgentRunner** — isolated engineer execution in Docker sandboxes with
  independent git worktrees.
- **Verification bridge** (`orcaid/bridge.py`) — self-healing hook system with
  `verify_subagent_completion()`, drift logs, and automated retry/escalation.
- **Indexer sweep** (`orcaid-verification-indexer`) — cron-compatible aggregation
  of verified outcomes and drift logs into `discovery.yaml`.
- **Performance-aware delegation** — `AssignmentMixin` injects historical drift
  and performance metrics from `discovery.yaml` into task assignment prompts.
- **Task modules:**
  - `Commit0Task` — function implementation benchmark.
  - `PaperbenchTask` — research paper reproduction with judge evaluation.
  - `SelfImproveTask` — agent self-improvement loop.
- **TaskModule ABC** — extensible interface for adding new task types
  (`orcaid/tasks/base.py`).
- **CLI entrypoints** — `orcaid` (main orchestrator) and
  `orcaid-verification-indexer` (index sweep).
- **Prompt templates** — YAML-based prompt system for `commit0` and `paperbench`.
- **Configuration** — `.env`-based config with LiteLLM multi-provider routing,
  separate manager/subagent model support.
- **MiniMax provider routing** — automatic reroute from Anthropic-compat to
  OpenAI-compat endpoint for MiniMax models.
- **Documentation suite** — `README.md`, `SETUP.md`, `AGENTS.md`,
  `ARCHITECTURE.md`, `CONFIGURATION.md`, `API.md`, `CONTRIBUTING.md`.
- **Test suite** — verification bridge tests covering drift log formatting,
  indexer sweep aggregation, and prompt injection validation.
- **Shell scripts** — `run_single.sh` and `run_multi.sh` for quick task execution.
