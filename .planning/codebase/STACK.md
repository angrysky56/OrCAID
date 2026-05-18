# Tech Stack

**Analysis Date:** 2026-05-18

## Core Languages

- **Python (>=3.12):** The entire application is written in Python, utilizing modern language features such as native type hinting, asynchronous execution (`asyncio`), and structured dataclasses.

## Primary Frameworks & SDKs

- **OpenHands SDK (`==1.11.0`):** Core agent-workspace framework. Provides the environment execution abstraction (`DockerWorkspace`, `DockerDevWorkspace`), agent definitions, stateful conversation management (`Conversation`), and custom visualizer interfaces.
- **LiteLLM (`==1.81.11`):** Injected model adapter to interface with diverse proprietary and open LLM providers (e.g., Anthropic, OpenAI, or local Ollama instances) with unified APIs.
- **Pydantic (`==2.12.5`):** Runtime validation layer for config management, schemas, and LLM-structured outputs.
- **Python Fire (`==0.7.1`):** CLI entrypoint generator that turns the `run_infer.py` module into an actionable terminal application.

## Key Libraries & Tools

- **Rich (`==14.2.0`):** Pretty terminal formatting, logs, and tracebacks.
- **PyYAML (`==6.0.3`):** Parsing prompts, configuration, and verification checklist definitions.
- **Numpy (`==2.4.1`):** Vector arithmetic and numeric helper operations.
- **Datasets (`==3.0.1`):** Hugging Face datasets integration (used primarily for loading benchmarks like Commit0 or Paperbench).
- **OpenAI (`==2.8.1`) & Anthropic (`==0.74.1`):** Primary client wrappers used by LiteLLM for remote model requests.
- **Jiter (`==0.12.0`):** Fast JSON parsing.
- **Tokenizers (`==0.21.4`):** Hugging Face tokenizers for token usage calculations and prompting validation.
- **Python-dotenv (`>=1.2.2`):** Handling of environment variables (`.env`).

## Optional Dependencies (Viz)

- **Matplotlib (`>=3.10`):** Generating visual plots for token usage and costs.
- **Plotly (`>=6.0`):** Interactive visualization dashboards for agent orchestration.
- **HTTPX (`>=0.28`):** Synchronous/asynchronous HTTP client for auxiliary integrations.

## Dev & Formatting Tools

- **Pytest (`>=9.0`):** Central testing runner.
- **Ruff (`>=0.14`):** Formatting and high-speed linting.
- **Uv:** Environment and package manager for sub-second virtualenv setups and package synchronization (`uv pip sync`).

## Deployment & Execution Environment

- **Docker:** Mandatory containerization tool. Every subagent runner runs isolated inside Git worktrees mounted in customizable Docker containers (`DockerWorkspace`/`DockerDevWorkspace`) to execute unsafe, user-submitted code securely.
- **Git Worktrees:** Lightweight directories checking out separate branches concurrently. Used to let subagents run code, make commits, and run tests in complete isolation from the manager and other agents.
