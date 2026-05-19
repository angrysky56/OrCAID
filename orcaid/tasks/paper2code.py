"""
Paper2Code TaskModule for OrCAID.

Wraps PaperCoder's three-stage pipeline (planning, analyzing, coding)
as an OrCAID task so it benefits from the delegation + verification loop.
"""

from .base import TaskModule

class Paper2CodeConfig:
    paper_url: str = ""
    output_dir: str = ""

class Paper2CodeTask(TaskModule):
    def get_docker_image(self):
        return "python:3.12-slim"  # Paper2Code uses Python, not minitorch

    def get_work_dir(self):
        return "/workspace/submission"

    def get_workspace_config(self):
        return {
            "server_image": "python:3.12-slim",
        }

    def load_task_data(self):
        """Load task-specific data."""
        pass

    def setup_workspace(self, workspace):
        """Initialize the workspace after Docker container is up."""
        pass

    def evaluate(self, workspace) -> dict:
        # Run eval.py on outputs
        # Return {"success": bool, "score": float, "benchmark_results": dict}
        pass

    def get_prompt_format_args(self, config):
        """Return a dict of variables for formatting prompt templates."""
        return {}

    def build_subagent(self, engineer_id, primary_task, all_tasks):
        """Create a SubAgent from delegated task(s)."""
        from orcaid.config import SubAgent

        subagent = SubAgent(
            engineer_id=engineer_id,
            task_id=primary_task.task_id,
            task_node_id=primary_task.task_node_id,
            requirements=primary_task.requirements,
            instruction=primary_task.instruction,
            estimated_complexity=primary_task.estimated_complexity,
            task_category=primary_task.task_category,
            submission_path="/workspace/submission",
        )
        return subagent, None

    def get_worktree_name(self, engineer_id):
        """Return the worktree directory name for this engineer."""
        return f"paper2code_worktree_{engineer_id}"

    def build_completed_task_summary(self, result, task_status):
        """Build a text summary of a completed task."""
        return (
            f"task_id: {result.task_id}\n"
            f"status: {task_status}\n"
            f"success: {result.success}"
        )

    def get_single_agent_info(self, workspace, config, prompts):
        """Return (header_text, user_instruction, log_content) for single agent mode."""
        return "", "", ""

    def create_subagent_result(self, subagent):
        """Create a SubAgentResult with task-specific fields populated."""
        from orcaid.config import SubAgentResult
        return SubAgentResult(
            success=False,
            commit_hash="",
            files_modified=[],
            git_diff="",
            error="Not implemented",
        )

    def get_followup_prompt_args(self, subagent):
        """Return format args dict for the followup_prompt yaml template."""
        return {}

    def get_run_start_log_lines(self, subagent):
        """Return list of log lines to print at subagent run start."""
        return []

    def populate_success_result(self, result, runner, commit_info):
        """Set task-specific fields on result after successful run."""
        pass

    def get_event_serialization_extras(self, subagent):
        """Return dict of extra fields for event serialization."""
        return {}

    def get_print_summary_lines(self, result, commit_info):
        """Return list of log lines for the commit summary."""
        return []

    def get_new_task_print_lines(self, subagent):
        """Return list of print lines when a new task is assigned to a runner."""
        return []

    def get_onboard_names(self, engineer_id):
        """Return (branch_name, worktree_name) for onboarding a new engineer."""
        return f"paper2code-{engineer_id}", f"paper2code_worktree_{engineer_id}"

    def get_completion_print_lines(self, result):
        """Return list of print lines when a subagent completes."""
        return []

    def get_log_agent_response_kwargs(self, result):
        """Return kwargs dict for output_logger.log_agent_response()."""
        return {}

    def get_conflict_instruction_args(self, subagent, conflict_files, workspace, repo_dir):
        """Return format args dict for the conflict_resolution yaml template."""
        return {}

    def get_execution_summary_lines(self, results):
        """Return list of print lines for the final execution summary."""
        return []