# OrCAID Roadmap

## Phase 1: Repo Structure, Configuration, and Packaging
- [x] Move root python files (`config.py`, `orcaid_verification_bridge.py`, `run_infer.py`) into appropriate modules.
- [x] Fix LiteLLM MiniMax connection issues.
- [x] Address OpenHands SDK / Docker environment integration issues.
- [x] Package OrCAID for easy setup and distribution.
- [x] Audit and improve functional code modifications.

## Phase 2: Advanced Delegation and Indexer Sweep Optimizations
- [x] Hardening the drift log formatting and directory sweep structure in the Verification Bridge.
- [x] Refining subagent assignment routing in `AssignmentMixin` based on historical drift rates of Hermes profiles.
- [x] Implementing the core indexer sweeping command (`orcaid-verification-indexer`) to rebuild `discovery.yaml`.

