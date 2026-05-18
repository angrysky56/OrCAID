# Structure

**Analysis Date:** 2026-05-18

This document lists the directory layout, module locations, and specific role responsibilities of every key file in the OrCAID repository.

---

## 1. Repository Directory Map

```
OrCAID/
├── .planning/
│   └── codebase/       # Codebase documentation (where this file resides)
├── core/               # Orchestrator core coordination logic
│   ├── __init__.py
│   ├── manager.py      # Core Manager class
│   ├── manager_assignment.py  # Task allocation mixin
│   ├── manager_exploration.py # Background discovery mixin
│   ├── manager_git.py  # Git worktree automation mixin
│   ├── manager_review.py # Merge and verification mixin
│   └── subagent.py     # Stateful SubAgent runner and event loop
├── judge/              # Grading and verification metrics
├── prompts/            # System and task prompts for LLMs
├── scripts/            # Helper deployment and diagnostic utilities
├── tasks/              # Benchmark configurations and task types
│   ├── __init__.py
│   ├── base.py         # Baseline Task class
│   ├── commit0.py      # Commit0 coding task implementation
│   ├── paperbench.py   # Paperbench paper reproduction implementation
│   └── self_improve.py # Self-improvement loop
├── config.py           # Dataclasses, validation schemas, and profiles
├── orcaid_verification_bridge.py # Dynamic self-healing verification module
├── run_infer.py        # Central CLI entrypoint application
├── pyproject.toml      # Build, dependency, and tool configs
└── README.md           # Getting started and basic setup guide
```

---

## 2. Component and File Roles

### A. Root Application Context

- **`run_infer.py`:**
  - *Role:* Central entrypoint interface.
  - *Details:* Loads the virtualenv, reads command-line flags (e.g., `--task-type`, `--model`, `--max-subagents`), parses task configurations, constructs the global workspace manager, and executes the orchestrator.
- **`config.py`:**
  - *Role:* Type declarations and schema definitions.
  - *Details:* Declares crucial types:
    - `SubAgentResult`: Tracks 26 parameters about subagent executions.
    - `TaskNode`: Graph node representing task tree elements.
    - `DelegationPlan`: Collection of task nodes, profiles, dependencies, and execution weights.
    - `WorkflowConfig`: Validated model configuration options.
- **`orcaid_verification_bridge.py`:**
  - *Role:* Self-healing verification bridge.
  - *Details:* Evaluates `SubAgentResult` objects, compares outputs to standard task-specific checklists, writes to persistent orchestrator memory, and outputs structured correction logs for closed-loop retries.

### B. Core Orchestrator Mixins (`core/`)

OrCAID relies on a modular `Manager` composed of specialized mixin classes:

- **`core/manager.py`:**
  - *Role:* Main orchestrator class.
  - *Details:* Extends all mixins. Tracks absolute run duration, accumulates total dollar costs and token usage across all subagents, and handles overall startup and shutdown routines.
- **`core/manager_git.py` (`GitMixin`):**
  - *Role:* Git worktree automation.
  - *Details:* Dynamically manages the lifecycle of local git worktrees. Handles branch creation, staging files, committing under `openhands` identities, checking out, and merging changes while applying conflict handling overrides.
- **`core/manager_exploration.py` (`ExplorationMixin`):**
  - *Role:* Automated file system and repo code scanning.
  - *Details:* Discovers primary files, libraries, dependencies, and APIs inside targeted codebases concurrently before planning begins.
- **`core/manager_assignment.py` (`AssignmentMixin`):**
  - *Role:* Scopes and distributes workload tasks.
  - *Details:* Uses LLM prompts to decompose high-level requirements into a dependency graph of sub-tasks, matching them with optimized Hermes profiles.
- **`core/manager_review.py` (`ReviewMixin`):**
  - *Role:* Merge management and code quality gatekeeper.
  - *Details:* Directs Git branch sweeps, triggers the closed-loop verification interceptor hook `_verify_and_return()`, handles dynamic bridges, and conducts the final unified review.

### C. Isolated Subagent Sandbox Execution (`core/`)

- **`core/subagent.py` (`SubAgentRunner`):**
  - *Role:* Parallel worker execution engine.
  - *Details:* Manages stateful agent loops inside isolated docker sandboxes and git worktrees. Instantiates the OpenHands SDK `Conversation` loop, connects default CLI tools, parses subagent logs, and returns compiled `SubAgentResult` metrics.

### D. Benchmark Domain Adapters (`tasks/`)

Task modules customize execution setups, base images, and inputs for specific tasks:

- **`tasks/base.py`:** Baseline Task class interface.
- **`tasks/commit0.py`:** Adapts OrCAID to the Commit0 software engineering benchmark. Connects test suites, custom docker setup scripts, and base images.
- **`tasks/paperbench.py`:** Adapts OrCAID to the Paperbench scientific research reproduction benchmark. Connects data parsing, evaluation metrics, and scientific pipelines.
- **`tasks/self_improve.py`:** Implements self-healing loop runs.
