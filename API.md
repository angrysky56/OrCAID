# API Reference

This document describes the public interfaces of the OrCAID package.

---

## Table of Contents

- [Task Module Interface](#task-module-interface)
- [Config Dataclasses](#config-dataclasses)
- [Verification Bridge](#verification-bridge)
- [CLI Entrypoints](#cli-entrypoints)

---

## Task Module Interface

**Module:** `orcaid.tasks.base`

The `TaskModule` ABC defines the contract that all task implementations must
satisfy. Implementing this interface is the primary extension point for
adding new task types to OrCAID.

### Abstract Methods (Must Implement)

```python
class TaskModule(ABC):

    def get_docker_image(self) -> str:
        """Return the Docker image name for the task workspace."""

    def get_work_dir(self) -> str:
        """Return the working directory inside the container."""

    def get_workspace_config(self) -> dict:
        """Return config dict for workspace construction."""

    def load_task_data(self) -> Any:
        """Load task-specific data. Stores loaded data internally and returns it."""

    def setup_workspace(self, workspace) -> None:
        """Initialize the workspace after Docker container is up."""

    def evaluate(self, workspace) -> dict:
        """Run task-specific evaluation and return results dict."""

    def get_prompt_format_args(self, config: WorkflowConfig) -> dict:
        """Return a dict of variables for formatting prompt templates."""

    def build_subagent(self, engineer_id: str, primary_task, all_tasks) -> tuple[SubAgent, str | None]:
        """Create a SubAgent from delegated task(s). Return (SubAgent, combine_log_msg_or_None)."""

    def get_worktree_name(self, engineer_id: str) -> str:
        """Return the worktree directory name for this engineer."""

    def build_completed_task_summary(self, result: SubAgentResult, task_status: str) -> str:
        """Build a text summary of a completed task for the assign_task prompt."""

    def get_single_agent_info(self, workspace, config, prompts) -> tuple[str, str, dict]:
        """Return (header_text, user_instruction, log_content) for single agent mode."""

    def create_subagent_result(self, subagent: SubAgent) -> SubAgentResult:
        """Create a SubAgentResult with task-specific fields populated."""

    def get_followup_prompt_args(self, subagent: SubAgent) -> dict:
        """Return format args dict for the followup_prompt yaml template."""

    def get_run_start_log_lines(self, subagent: SubAgent) -> list[str]:
        """Return list of log lines to print at subagent run start."""

    def populate_success_result(self, result: SubAgentResult, runner, commit_info) -> None:
        """Set task-specific fields on result after successful run."""

    def get_event_serialization_extras(self, subagent: SubAgent) -> dict:
        """Return dict of extra fields for event serialization."""

    def get_print_summary_lines(self, result: SubAgentResult, commit_info) -> list[str]:
        """Return list of log lines for the commit summary."""

    def get_new_task_print_lines(self, subagent: SubAgent) -> list[str]:
        """Return list of print lines when a new task is assigned to a runner."""

    def get_onboard_names(self, engineer_id: str) -> tuple[str, str]:
        """Return (branch_name, worktree_name) for onboarding a new engineer."""

    def get_completion_print_lines(self, result: SubAgentResult) -> list[str]:
        """Return list of print lines when a subagent completes."""

    def get_log_agent_response_kwargs(self, result: SubAgentResult) -> dict:
        """Return kwargs dict for output_logger.log_agent_response()."""

    def get_conflict_instruction_args(self, subagent, conflict_files, workspace, repo_dir) -> dict:
        """Return format args dict for the conflict_resolution yaml template."""

    def get_execution_summary_lines(self, results: list[SubAgentResult]) -> list[str]:
        """Return list of print lines for the final execution summary."""
```

### Optional Override Methods

These have sensible defaults and may be overridden for task-specific behavior:

```python
class TaskModule(ABC):

    def post_load_task_data(self) -> list[str]:
        """Perform post-load processing (e.g., load rubric). Default: []."""

    def get_scan_log_kwargs(self, config) -> dict:
        """Return kwargs for output_logger.log_scan_start(). Default: max_iterations only."""

    def build_analysis_from_state(self) -> tuple[AnalysisResult | None, list[str]]:
        """Build AnalysisResult from pre-loaded state. Default: (None, [])."""

    def check_existing_delegation(self, events, extract_fn) -> bool:
        """Check if events already contain a valid delegation. Default: False."""

    def search_alternative_json(self, events, extract_fn, log_fn) -> dict | None:
        """Search for alternative JSON formats if assign_task JSON not found. Default: None."""

    def extract_assignments(self, assign_data: dict) -> list[dict]:
        """Extract the assignments list from parsed assign_task JSON."""

    def get_assign_context(self, all_completed, workspace, repo_dir) -> dict:
        """Return context dict for assignment processing. Default: {}."""

    def update_subagent_for_assignment(self, subagent, context, workspace, log_fn) -> None:
        """Update SubAgent with task-specific assignment context. Default: sets status='ready'."""

    def get_assigned_targets(self, assignments, default_engineer_id) -> str:
        """Return target string for the manager_instruction log event."""

    def get_assign_event_extras(self, engineer_id: str) -> dict:
        """Return extra fields for the assign_task log event. Default: {}."""

    def get_final_review_log_extras(self, subagent_results) -> dict:
        """Return extra fields for the final_review_all log event. Default: {}."""

    def get_collect_extra_log(self, subagent_result) -> str:
        """Return extra log text for collect_and_merge. Default: ''."""

    @property
    def should_stash_before_merge(self) -> bool:
        """Whether to stash dirty working tree before git merge. Default: False."""

    @property
    def should_try_uncommitted_merge(self) -> bool:
        """Whether to try committing+merging uncommitted worktree changes. Default: False."""

    @property
    def should_setup_on_retry(self) -> bool:
        """Whether to call setup() again on LLM retry. Default: False."""

    @property
    def should_resend_on_retry(self) -> bool:
        """Whether to re-send the prompt on retry. Default: False."""

    def populate_no_commit_result(self, result) -> None:
        """Set extra fields on result when no new commit was detected. Default: no-op."""

    def prepare_reuse_subagent(self, new_subagent, old_runner) -> None:
        """Copy task-specific info from old runner to reused subagent. Default: no-op."""

    def post_onboard_subagent(self, subagent, repo_dir) -> None:
        """Set task-specific fields on subagent after worktree onboarding. Default: no-op."""

    def get_auto_reassign_instruction_args(self, subagent) -> dict:
        """Return format args dict for the auto_reassign yaml template."""
```

---

## Config Dataclasses

**Module:** `orcaid.config`

### `WorkflowConfig`

Top-level configuration for a workflow run.

| Field | Type | Description |
|---|---|---|
| `task` | `str` | Task type identifier |
| `model` | `str` | LLM model for the manager |
| `subagent_model` | `str` | LLM model for engineers |
| `max_iterations` | `int` | Max LLM iterations for manager |
| `sub_iterations` | `int` | Max LLM iterations per subagent |
| `max_subagents` | `int` | Number of parallel engineers |
| `max_rounds_chat` | `int` | Number of assign-and-execute rounds |
| `manager_max_iterations` | `int` | Manager-specific iteration cap |

### `SubAgent`

Per-engineer assignment state.

| Field | Type | Description |
|---|---|---|
| `engineer_id` | `str` | Unique engineer identifier |
| `task_id` | `str` | Assigned task identifier |
| `file_path` | `str` | Target file (Commit0) |
| `functions_to_implement` | `list[str]` | Functions to implement (Commit0) |
| `task_node_id` | `str` | Task graph node reference |
| `requirements` | `str` | Natural language requirements |
| `instruction` | `str` | Full instruction text |
| `estimated_complexity` | `str` | `low`, `medium`, `high` |
| `task_category` | `str \| None` | Task classification |
| `submission_path` | `str` | Working directory path |

### `SubAgentResult`

26-field result record produced by each engineer run.

| Key Fields | Type | Description |
|---|---|---|
| `engineer_id` | `str` | Which engineer produced this |
| `task_id` | `str` | Which task was executed |
| `success` | `bool` | Primary pass/fail |
| `merged` | `bool` | Whether changes were merged |
| `commit_hash` | `str` | Git commit hash |
| `files_modified` | `list[str]` | Files changed |
| `error` | `str` | Error message if failed |
| `duration_seconds` | `float` | Wall-clock time |
| `cost` | `float` | LLM cost in dollars |
| `actual_iterations` | `int` | LLM iterations used |
| `max_iterations` | `int` | Iteration cap |
| `git_diff` | `str` | Full diff output |
| `commit_message` | `str` | Commit message |
| `round_num` | `int` | Which round this was |

---

## Verification Bridge

**Module:** `orcaid.bridge`

### Public Functions

```python
def verify_subagent_completion(
    subagent_result: SubAgentResult,
    review_result: dict,
    task_module: TaskModule,
    orchestrator_memory_base: Path,
) -> VerificationResult:
    """Score subagent output against task-specific checklist."""

def discovery_scan_for_orcaid(
    orchestrator_memory_base: Path,
) -> dict:
    """Read discovery.yaml and return gap context for manager injection."""

def synthesize_orcaid_outcome(
    subagent_results: list[SubAgentResult],
    manager_review_results: list[dict],
    task_type: str,
) -> dict:
    """Synthesize final outcome from all subagent results."""

def write_verified_outcome(
    data: dict,
    memory_base: Path,
) -> Path:
    """Write a verified outcome YAML to orchestrator-memory/verified/."""

def write_drift_log(
    drift_log: list[dict],
    correction_context: dict,
    subagent_result: SubAgentResult,
    memory_base: Path,
) -> Path:
    """Write a drift log Markdown report to orchestrator-memory/drift_logs/."""

def run_indexer_sweep(
    memory_base: Path | None = None,
) -> None:
    """Aggregate verified/ and drift_logs/ into index/discovery.yaml."""

def write_compound_skill(
    synthesis: dict,
    memory_base: Path,
) -> Path:
    """Write compound synthesis skill to orchestrator-memory."""

def escalate_to_human(
    subagent_result: SubAgentResult,
    verification: VerificationResult,
    review_result: dict,
) -> None:
    """Write escalation record for human review."""

def orcaid_reinvoke_subagent(
    manager,
    engineer_id: str,
    original_task_id: str,
    correction_context: dict,
    max_retries: int = 3,
) -> SubAgent | None:
    """Queue a retry with correction context."""

def run_sweep_cli() -> None:
    """CLI entrypoint for orcaid-verification-indexer console script."""
```

---

## CLI Entrypoints

Registered in `pyproject.toml` under `[project.scripts]`:

| Command | Module | Description |
|---|---|---|
| `orcaid` | `orcaid.cli:main` | Main orchestration runner |
| `orcaid-verification-indexer` | `orcaid.bridge:run_sweep_cli` | Indexer sweep for cron |
