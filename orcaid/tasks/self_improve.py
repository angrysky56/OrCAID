"""
python -m tasks.self_improve

Self-improvement task: lets OrCAID work on its own codebase.
Engineers modify files in the OrCAID repo, validated by Python syntax checks.
"""

import ast
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import TaskModule


@dataclass
class SelfImproveConfig:
    """Configuration for self-improvement task."""

    repo_path: str = "/home/ty/Repositories/ai_workspace/OrCAID"
    task_description: str = "Improve the OrCAID codebase"
    max_file_size_kb: int = 512


class SelfImproveTask(TaskModule):
    """
    Task that lets OrCAID work on its own codebase (self-improvement loop).

    Engineers modify files in the OrCAID repo. Success is measured by:
    - All modified .py files pass python3 -c "import ast; ast.parse(open(file).read())"
    """

    def __init__(self, config: SelfImproveConfig):
        self.config = config
        self.task_data: Optional[dict] = None

    def get_docker_image(self):
        # Use a generic Python image since we're working on local files
        return "python:3.12-slim"

    def get_work_dir(self):
        return str(Path.home() / "orcaid_workspace")

    def get_workspace_config(self):
        return {
            "base_image": self.get_docker_image(),
            "target": "source-minimal",
        }

    def load_task_data(self):
        """Load task data with the task description."""
        self.task_data = {
            "task_description": self.config.task_description,
            "repo_path": self.config.repo_path,
        }
        return self.task_data

    def setup_workspace(self, workspace):
        """Copy OrCAID repo to workspace directory."""
        src = self.config.repo_path
        dst = self.get_work_dir()

        if not os.path.exists(src):
            raise RuntimeError(f"Source repo not found: {src}")

        print(f"\n{'=' * 60}")
        print("[SelfImprove] Setting up workspace")
        print(f"{'=' * 60}")
        print(f"[SelfImprove] Copying {src} -> {dst}")

        # Remove existing destination if present
        if os.path.exists(dst):
            shutil.rmtree(dst)

        # Copy directory recursively
        shutil.copytree(src, dst, symlinks=True)
        print("[SelfImprove] Workspace setup complete")

    def evaluate(self, workspace) -> dict:
        """
        Evaluate modified Python files for syntax validity.

        Returns:
            dict with success status, files_modified, and syntax_errors
        """
        work_dir = self.get_work_dir()

        # Get all modified Python files via git
        print(f"\n{'=' * 60}")
        print("[SelfImprove] Evaluating modifications")
        print(f"{'=' * 60}")

        # Check for uncommitted changes
        diff_result = workspace.execute_command(
            f"cd {work_dir} && git diff --name-only HEAD", timeout=60
        )

        # Also check staged files
        staged_result = workspace.execute_command(
            f"cd {work_dir} && git diff --cached --name-only HEAD", timeout=60
        )

        staged_files = set()
        if staged_result.exit_code == 0:
            staged_files = set(
                f.strip() for f in staged_result.stdout.splitlines() if f.strip()
            )

        modified_files = set()
        if diff_result.exit_code == 0:
            modified_files = set(
                f.strip() for f in diff_result.stdout.splitlines() if f.strip()
            )

        all_modified = modified_files | staged_files
        py_files = [f for f in all_modified if f.endswith(".py")]

        print(
            f"[SelfImprove] Modified files: {len(all_modified)} total, {len(py_files)} Python"
        )

        syntax_errors = []
        valid_files = []

        for filepath in py_files:
            full_path = os.path.join(work_dir, filepath)
            if not os.path.exists(full_path):
                print(f"[SelfImprove] Warning: file not found: {filepath}")
                continue

            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    code = f.read()
                ast.parse(code)
                valid_files.append(filepath)
                print(f"  ✓ {filepath}")
            except SyntaxError as e:
                error_msg = f"{filepath}: {e.msg} (line {e.lineno})"
                syntax_errors.append(error_msg)
                print(f"  ✗ {filepath}: {e.msg} at line {e.lineno}")

        success = len(syntax_errors) == 0

        print(
            f"\n[SelfImprove] Results: {len(valid_files)} valid, {len(syntax_errors)} syntax errors"
        )

        return {
            "success": success,
            "files_modified": list(all_modified),
            "py_files_valid": valid_files,
            "syntax_errors": syntax_errors,
        }

    def get_prompt_format_args(self, config):
        """Return format args for prompt templates."""
        work_dir = self.get_work_dir()
        return {
            "max_agents": config.max_subagents,
            "max_rounds": config.max_rounds_chat,
            "workspace_dir_name": work_dir.split("/")[-1],
            "repo_path": work_dir,
            "task_description": self.config.task_description,
            "test_cmd": "python3 -c 'import ast; ast.parse(open(f).read())'",
            "test_dir": work_dir,
        }

    # ---- Manager integration methods ----

    def get_scan_log_kwargs(self, config):
        return {
            "repo_name": "orcaid",
            "repo_path": self.get_work_dir(),
            "max_iterations": config.manager_max_iterations,
        }

    def build_subagent(self, engineer_id, primary_task, all_tasks):
        from config import SubAgent

        all_files = []
        all_instructions = []

        for t in all_tasks:
            all_files.append(t.file_path)
            all_instructions.append(f"File: {t.file_path}\n{t.instruction}")

        combined_instruction = "\n\n---\n\n".join(all_instructions)
        combined_file_path = (
            ", ".join(all_files) if len(all_files) > 1 else all_files[0]
        )

        subagent = SubAgent(
            engineer_id=engineer_id,
            task_id=primary_task.task_id,
            file_path=combined_file_path,
            functions_to_implement=[],
            instruction=combined_instruction,
            estimated_complexity=primary_task.estimated_complexity,
        )

        combine_log = (
            f"  (Combined {len(all_tasks)} tasks: {all_files})"
            if len(all_tasks) > 1
            else None
        )
        return subagent, combine_log

    def get_worktree_name(self, engineer_id):
        return f"orcaid_worktree_{engineer_id}"

    def get_subagent_log_lines(self, subagent):
        lines = [f"      File: {subagent.file_path}"]
        return lines

    @property
    def should_stash_before_merge(self):
        return True

    @property
    def should_try_uncommitted_merge(self):
        return True

    def build_completed_task_summary(self, result, task_status):
        return (
            f"task_id: {result.task_id}\n"
            f"file: {result.file_path}\n"
            f"status: {task_status}\n"
            f"merged: {result.merged}\n"
            f"commit: {result.commit_hash or 'none'}"
        )

    def get_single_agent_info(self, workspace, config, prompts):
        header = "Single Agent Mode - Self-Improvement"
        format_args = self.get_prompt_format_args(config)
        user_instruction = prompts.get("single_agent_instruction", "").format(
            **format_args
        )
        log_content = {
            "repo_name": "orcaid",
            "repo_path": self.get_work_dir(),
            "max_iterations": config.manager_max_iterations,
        }
        return header, user_instruction, log_content

    def get_final_review_log_extras(self, subagent_results):
        merged_count = sum(1 for r in subagent_results if r.merged and r.file_path)
        return {"files_merged": merged_count}

    def get_collect_extra_log(self, subagent_result):
        if subagent_result.file_path:
            return f"  - File: {subagent_result.file_path}"
        return ""

    # ---- SubAgent runner integration methods ----

    def create_subagent_result(self, subagent):
        from config import SubAgentResult

        return SubAgentResult(
            engineer_id=subagent.engineer_id,
            task_id=subagent.task_id,
            task_node_id=subagent.task_node_id,
            branch_name=subagent.branch_name or "",
            worktree_path=subagent.worktree_path or "",
            file_path=subagent.file_path,
            functions_implemented=[],
            round_num=subagent.current_round,
        )

    def get_followup_prompt_args(self, subagent):
        return {
            "instruction": subagent.instruction,
            "file_path": subagent.file_path,
        }

    def get_run_start_log_lines(self, subagent):
        return [
            f"  - Task: {subagent.task_id}",
            f"  - File: {subagent.file_path}",
        ]

    @property
    def should_setup_on_retry(self):
        return True

    @property
    def should_resend_on_retry(self):
        return True

    def populate_no_commit_result(self, result):
        result.git_diff = ""

    def populate_success_result(self, result, runner, commit_info):
        result.success = True
        result.commit_hash = commit_info.get("hash", "")
        result.commit_message = commit_info.get("message", "")
        result.git_diff = runner.get_git_diff()
        result.files_modified = runner.get_modified_files()

    def get_event_serialization_extras(self, subagent):
        return {"file_path": subagent.file_path}

    def get_print_summary_lines(self, result, commit_info):
        lines = []
        if result.files_modified:
            lines.append(f"  Files Modified: {', '.join(result.files_modified)}")
        if result.git_diff:
            diff_preview = result.git_diff[:500]
            if len(result.git_diff) > 500:
                diff_preview += "\n... (truncated)"
            lines.append("  Diff Preview:")
            for line in diff_preview.split("\n")[:15]:
                lines.append(f"    {line}")
        return lines

    def prepare_reuse_subagent(self, new_subagent, old_runner):
        pass

    def get_new_task_print_lines(self, subagent):
        return [f"- File: {subagent.file_path}"]

    def get_onboard_names(self, engineer_id):
        branch_name = f"agent_{engineer_id}"
        worktree_name = f"orcaid_worktree_{engineer_id}"
        return branch_name, worktree_name

    def post_onboard_subagent(self, subagent, repo_dir):
        pass

    def get_completion_print_lines(self, result):
        lines = []
        if result.commit_hash:
            lines.append(f"- Commit: {result.commit_hash}")
        return lines

    def get_log_agent_response_kwargs(self, result):
        from datetime import datetime

        return {
            "engineer_id": result.engineer_id,
            "task_id": result.task_id,
            "success": result.success,
            "commit_hash": result.commit_hash,
            "git_diff": result.git_diff,
            "files_modified": result.files_modified,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "actual_iterations": result.actual_iterations,
            "max_iterations": result.max_iterations,
            "cost": result.cost,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "start_time": (
                datetime.fromisoformat(result.start_time) if result.start_time else None
            ),
            "end_time": (
                datetime.fromisoformat(result.end_time) if result.end_time else None
            ),
            "round_num": result.round_num,
        }

    def get_conflict_instruction_args(
        self, subagent, conflict_files, workspace, repo_dir
    ):
        conflict_file_list = "\n".join(f"  - {f}" for f in conflict_files)
        return {"conflict_file_list": conflict_file_list}

    def get_auto_reassign_instruction_args(self, subagent):
        return {"original_instruction": subagent.instruction}

    def get_execution_summary_lines(self, results):
        lines = [
            f"\n{'=' * 70}",
            "[SelfImprove] Execution Summary",
            f"{'=' * 70}",
            f"Total task completions: {len(results)}",
        ]
        merged_count = len([r for r in results if r.merged])
        committed_count = len([r for r in results if r.success])
        failed_count = len([r for r in results if not r.merged])
        lines.append(f"Merged: {merged_count} (committed: {committed_count})")
        lines.append(f"Failed: {failed_count}")

        return lines


def main():
    config = SelfImproveConfig(task_description="Add type hints to core modules")
    task = SelfImproveTask(config)
    print(f"{type(task).__name__} created successfully")


if __name__ == "__main__":
    main()
