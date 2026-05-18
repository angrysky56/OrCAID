# Integrations

**Analysis Date:** 2026-05-18

This document outlines the external systems, SDK dependencies, API connections, and hardware interfaces integrated into the OrCAID orchestrator.

---

## 1. Docker Runtime Environment

OrCAID relies heavily on Docker containers to construct isolated, reproducible, and secure sandboxes for execution.

- **Purpose:** Execute untrusted user code (e.g., in Commit0 or Paperbench) without exposing the host operating system.
- **Client Interface:** Managed directly by the OpenHands container layer.
- **Key Classes:** `DockerWorkspace` (standard container execution) and `DockerDevWorkspace` (bind-mounting local codebases for active development/testing) in `openhands.workspace`.
- **Image Abstraction:** Configured via `base_image` inside `tasks/commit0.py` and `tasks/paperbench.py`.
- **Isolation Details:** 
  - Every engineer subagent receives an isolated container instance.
  - Directories are bind-mounted read-write into the target container.
- **Lifecycle & Cleanups:**
  - Startup hook: `cleanup_stale_containers()` parses active docker containers and kills orphans from previous crashes to prevent socket/port leakage.
  - Graceful Shutdown: Subagents terminate containers cleanly at the end of their conversational runs.

---

## 2. OpenHands SDK Integration

The OpenHands framework acts as the underlying execution platform.

- **Version:** `1.11.0` (including `openhands-sdk`, `openhands-workspace`, `openhands-tools`, `openhands-agent-server`).
- **Core Abstractions:**
  - **`openhands.sdk.Agent`:** Represents the agent profile (subagent model + system prompt).
  - **`openhands.sdk.Conversation`:** Encapsulates the runtime event loop, holding state, tracking tokens, and invoking tools.
  - **`openhands.tools.preset.default.get_default_tools`:** Generates execution tools for the agent (CLI execution). Browser tools are disabled for subagents to focus purely on coding/cli tasks (`enable_browser=False`).
  - **`LLMSummarizingCondenser`:** Compacts conversational contexts when they exceed token limits to prevent context overflow.

---

## 3. LiteLLM Adapter & LLM APIs

LiteLLM is used to interface with proprietary and open-source models using a unified API layer.

- **Version:** `1.81.11`
- **Supported Integrations:**
  - **Anthropic Claude (e.g., Claude 3.5 Sonnet):** The preferred model for high-complexity management, routing, and reviews.
  - **OpenAI GPT (e.g., GPT-4o):** Used for baseline tasks or comparative runs.
  - **Local LLMs (e.g., Ollama / vLLM):** Configurable in `.env` or passed via CLI args for self-hosted execution.
- **Client Configuration:**
  - Environment keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LITELLM_API_KEY`.
  - Advanced options: `litellm.drop_params = True` strips unsupported parameters automatically.
  - Usage tracker: Direct accumulation of prompt tokens, completion tokens, and dollar costs into metrics databases inside the orchestrator.

---

## 4. Git Worktree System

OrCAID delegates work to parallel subagents in completely independent Git branches.

- **Mechanism:** Git Worktrees allow checking out multiple branches concurrently without cloning a new repository.
- **Directory Isolation:** Subagents are assigned custom workspace subdirectories (e.g., `outputs/worktrees/{engineer_id}/`).
- **State Integration:**
  - **Branch Creation:** Branches are named `feature/{engineer_id}`.
  - **Commit Pipeline:** `commit_worktree_changes` stages modifications, signs the commit as `openhands` (`openhands@all-hands.dev`), commits the changes, and initiates a safe merge via the manager.
  - **Merge Conflict Resolution:** If merge conflicts occur, the manager aborts the merge and invokes a "theirs" strategy option (overwriting with the engineer's work) if no retry rounds are left, or routes the conflict list back to the subagent as drift correction.

---

## 5. Verification Bridge (`orcaid_verification_bridge.py`)

A vital, self-healing closed-loop subsystem connecting the execution outcomes back to the manager's memory.

- **Directory Base:** `~/.hermes/orchestrator-memory/`
- **Sub-Paths:**
  - `/verified/`: Persisted YAML documents of successful task outcomes (including file modifications and duration).
  - `/drift_logs/`: Records of failed tasks, capturing the failed criteria, auto-retry logs, and agent drift assessments.
  - `/escalations/`: Tasks flagged for human review after exceeding `max_retries`.
  - `/index/discovery.yaml`: Aggregated stats swept by a periodic cron job to feed historical pass/fail rates back to the manager.
- **Verification Flow:**
  1. Subagent finishes a round of coding or research.
  2. Manager imports the Verification Bridge dynamically and invokes `verify_subagent_completion()`.
  3. The result is scored against task-specific checklist items loaded from YAML profiles.
  4. Verdict PASS: Written to `verified/`.
  5. Verdict FAIL: Written to `drift_logs/` + correction context injected → re-invokes subagent for another round.
  6. Verdict ESCALATE: Written to `escalations/` for Ty (human) to review.
