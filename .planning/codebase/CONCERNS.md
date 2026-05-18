# Concerns

**Analysis Date:** 2026-05-18

This document highlights critical technical debts, performance scaling limits, security risks, and outstanding development gaps identified in the OrCAID orchestrator.

---

## 1. Technical Debt & Design Gaps

- **Lack of Isolated Git/Subprocess Unit Tests:**
  - *Description:* The core git automation mechanics (in `core/manager_git.py`) rely heavily on direct shell calls via subprocess execution. 
  - *Impact:* Changes to the local git installation or unexpected repository states (e.g., untracked files, locking files) can cause silent failures or hangs during parallel merges. These are currently difficult to mock reliably without a mock git environment.
- **Dynamic Bridge Module Loading Risks:**
  - *Description:* The `ReviewMixin` imports the verification bridge (`orcaid_verification_bridge.py`) dynamically at runtime inside the verification interceptor.
  - *Impact:* A single syntax error or typo introduced into the bridge during a live run immediately crashes the active coordination, throwing an `ImportError` or `SyntaxError` mid-execution and causing active subagent runs to fail.
- **Metrics Concurrency Thread-Safety:**
  - *Description:* Logging cost metrics, absolute execution durations, and prompt/completion tokens is handled via synchronous accumulation inside the main `Manager` class.
  - *Impact:* High numbers of parallel subagents could theoretically cause race conditions or blocking during state updates, though Python's global interpreter lock (GIL) and basic async structures currently mitigate this.

---

## 2. Performance & Scaling Bottlenecks

- **VRAM Bounds on Local Hardware (RTX 3060):**
  - *Description:* Ty's development machine features an NVIDIA RTX 3060 with 12 GB of VRAM.
  - *Impact:* If local LLMs (e.g., Ollama or vLLM running large Qwen 4B or 7B models) are used, concurrently running 4+ heavy Docker workspaces alongside the model can cause GPU memory exhaustion and system crashes.
- **LiteLLM / API Provider Rate Limits:**
  - *Description:* Concurrently executing up to 4 subagents that rapidly send multi-turn LLM calls will trigger strict provider rate limits (Requests Per Minute/Tokens Per Minute) on platforms like Anthropic or OpenAI.
  - *Impact:* Leads to frequent HTTP 429 retries, slowing down execution times.
- **Context Window Growth & Token Waste:**
  - *Description:* Long, multi-turn code generation and repair loops cause conversation history to expand.
  - *Impact:* Although `LLMSummarizingCondenser` is used, complex tasks requiring multiple verification retries can waste large amounts of input tokens, leading to increased API costs and slower responses.

---

## 3. Security & Container Safety Gaps

- **Untrusted Code Execution in Docker Containers:**
  - *Description:* Subagents generate and execute arbitrary code (e.g., tests, custom CLI scripts) inside Docker containers.
  - *Impact:* If container base images are misconfigured or run with excessive privileges (e.g., bind-mounting the host's root path or using host networking), subagents could compromise the host operating system.
- **Git Credentials Protection in Sandboxes:**
  - *Description:* Because subagents run inside Git worktrees, container volume mounts could expose global git configurations or SSH credentials.
  - *Impact:* If an agent is compromised or behaves pathologically, it could leak global keys or perform unauthorized repository mutations.
