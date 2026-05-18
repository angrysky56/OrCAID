"""
Manager module for OrCAID.
Handles task analysis, delegation, and subagent orchestration.
"""

# pylint: disable=no-member

import json
import logging
from datetime import datetime
from pathlib import Path

from openhands.sdk import Agent, BaseConversation, Conversation, LLMSummarizingCondenser
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.tools.preset.default import get_default_tools

from config import SubAgent
from core.manager_assignment import AssignmentMixin
from core.manager_exploration import ExplorationMixin
from core.manager_git import GitMixin
from core.manager_review import ReviewMixin
from core.utils import (
    PanelVisualizer,
    build_delegation_plan,
    build_delegation_prompt,
    count_llm_iterations,
    extract_conversation_metrics,
    extract_json_from_events,
    fallback_delegation,
    load_prompts,
    serialize_event,
)

logger = logging.getLogger(__name__)


class Manager(GitMixin, ExplorationMixin, ReviewMixin, AssignmentMixin):
    """
    The central orchestrator for OrCAID.
    Decomposes tasks, delegates to subagents, and manages the implementation lifecycle.
    """

    def __init__(
        self,
        llm,
        workspace,
        task,
        config,
        output_logger,
        prompts=None,
    ):
        """
        Initialize the Manager.

        Args:
            llm: The LLM instance for the manager.
            workspace: The workspace for file operations.
            task: The task object defining implementation/evaluation logic.
            config: Configuration object.
            output_logger: Logger for structured events.
            prompts: Optional custom prompts.
        """
        super().__init__()
        self.llm = llm
        self.workspace = workspace
        self.task = task
        self.config = config
        self.output_logger = output_logger
        self.prompts = prompts or load_prompts()

        self.agent = None
        self.conversation: BaseConversation | None = None
        self.analysis_result = None
        self.delegation_plan = None
        self.repo_dir = task.get_work_dir()

        self.analysis_start_time = None
        self.analysis_end_time = None
        self.delegation_start_time = None
        self.delegation_end_time = None

        # Cumulative time tracking for operations during parallel execution
        self.assign_task_total_time = 0.0
        self.review_total_time = 0.0

        # Per-operation cost tracking (delta costs, not accumulated)
        self.analysis_cost = 0.0
        self.analysis_tokens = 0
        self.delegation_cost = 0.0
        self.delegation_tokens = 0
        self.assign_task_total_cost = 0.0
        self.assign_task_total_tokens = 0
        self.review_total_cost = 0.0
        self.review_total_tokens = 0

        # Background exploration tracking (commit0-specific) — initialised by ExplorationMixin
        self.exploration_findings: list = []
        self.exploration_cancelled: bool = False

        # Final review tracking
        self.final_review_cost = 0.0
        self.final_review_tokens = 0
        self.final_review_total_time = 0.0

        # Test tracking (paperbench-specific)
        self.test_total_time = 0.0
        self.test_result = None

        self.analysis_metrics = None
        self.current_round = 1

    def log(self, message: str) -> None:
        """Log a message to stdout with a [Manager] prefix."""
        print(f"[Manager] {message}")

    def save_events(self, phase: str, event_start_idx: int = 0) -> None:
        """
        Save new events from the conversation to the output logger.

        Args:
            phase: The current execution phase.
            event_start_idx: The index to start saving events from.
        """
        if not self.conversation or not self.output_logger:
            return

        events = list(self.conversation.state.events)
        if event_start_idx >= len(events):
            return

        new_events = events[event_start_idx:]
        self.log(
            f"Saving {len(new_events)} new events (phase={phase}) to manager_events.jsonl..."
        )

        for idx, event in enumerate(new_events):
            global_idx = event_start_idx + idx
            serialized = serialize_event(event, global_idx)
            serialized["engineer_id"] = "manager"
            serialized["phase"] = phase
            serialized["start_time"] = serialized.get("timestamp")
            if global_idx + 1 < len(events):
                next_ts = getattr(events[global_idx + 1], "timestamp", None)
                serialized["end_time"] = next_ts
            else:
                serialized["end_time"] = datetime.now().isoformat()
            self.output_logger.log_agent_event("manager", serialized)

    def setup_workspace(self) -> None:
        """Prepare the workspace and load task data."""
        self.log("Loading task data...")
        self.task.load_task_data()

        for msg in self.task.post_load_task_data():
            self.log(msg)

        self.log("Setting up workspace...")
        self.task.setup_workspace(self.workspace)
        self.log("Workspace setup complete")

    def setup(self, mode: str = "multi_agent") -> None:
        """
        Set up the agent and conversation.

        Args:
            mode: Either 'single_agent' or 'multi_agent'.
        """
        self.log(f"Setting up agent in {mode} mode...")
        tools = get_default_tools(enable_browser=False)

        format_args = self.task.get_prompt_format_args(self.config)

        if mode == "single_agent":
            self.agent = Agent(
                llm=self.llm,
                tools=tools,
            )
        else:
            instruction = self.prompts.get("user_instruction", "").format(**format_args)
            condenser_llm = self.llm.model_copy(update={"usage_id": "condenser"})
            condenser = LLMSummarizingCondenser(
                llm=condenser_llm,
                max_size=200,
                keep_first=4,
            )
            self.agent = Agent(
                llm=self.llm,
                tools=tools,
                agent_context=AgentContext(system_message_suffix=instruction),
                condenser=condenser,
            )

        self.conversation = Conversation(
            agent=self.agent,
            workspace=self.workspace,
            max_iteration_per_run=self.config.manager_max_iterations,
            visualizer=PanelVisualizer(),
        )
        self.log("Agent ready")

    def run_single_agent(self) -> dict:
        """
        Run in single-agent mode (no delegation).

        Returns:
            dict: Statistics about the run.
        """
        header, user_instruction, log_content = self.task.get_single_agent_info(
            self.workspace, self.config, self.prompts
        )

        self.log("=" * 60)
        self.log(header)
        self.log("=" * 60)

        self.output_logger.log_event(
            event_type="single_agent_start",
            source="manager",
            content=log_content,
        )

        self.analysis_start_time = datetime.now()
        self.log("Starting implementation...")
        self.conversation.send_message(user_instruction)
        try:
            self.conversation.run()
        except ConversationRunError as e:
            self.log(f"Agent run failed: {e}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.log(f"Agent run ended with unexpected error: {e}")

        self.analysis_end_time = datetime.now()
        duration = (self.analysis_end_time - self.analysis_start_time).total_seconds()
        self.log(f"Single agent completed in {duration:.1f}s")

        events = self.conversation.state.events
        iterations = count_llm_iterations(events)

        engineer_id = "single_agent"
        self.log(f"Saving {len(list(events))} events to {engineer_id}_events.jsonl...")
        events_list = list(self.conversation.state.events)
        for idx, event in enumerate(events_list):
            serialized = serialize_event(event, idx)
            serialized["engineer_id"] = engineer_id
            serialized["start_time"] = serialized.get("timestamp")
            if idx + 1 < len(events_list):
                next_ts = getattr(events_list[idx + 1], "timestamp", None)
                serialized["end_time"] = next_ts
            else:
                serialized["end_time"] = (
                    self.analysis_end_time.isoformat()
                    if self.analysis_end_time
                    else None
                )
            self.output_logger.log_agent_event(engineer_id, serialized)

        self.output_logger.log_event(
            event_type="single_agent_complete",
            source="manager",
            content={
                "duration": duration,
                "iterations": iterations,
                "max_iterations": self.config.manager_max_iterations,
                "total_events": len(list(self.conversation.state.events)),
            },
            start_time=self.analysis_start_time,
            end_time=self.analysis_end_time,
        )

        self.log(f"Iterations used: {iterations}/{self.config.manager_max_iterations}")

        return {
            "duration": duration,
            "iterations": iterations,
        }

    def scan_and_analyze(self) -> dict | None:
        """
        Analyze the repository and the task to prepare for delegation.

        Returns:
            dict | None: The analysis results.
        """
        self.log("=" * 60)
        self.log("Scan and Analysis")
        self.log("=" * 60)

        self.output_logger.log_scan_start(**self.task.get_scan_log_kwargs(self.config))

        self.analysis_start_time = datetime.now()

        metrics_before = extract_conversation_metrics(self.conversation)
        cost_before = metrics_before["cost"]
        tokens_before = metrics_before["total_tokens"]

        self.log("Injecting gap context from orchestrator-memory...")
        try:
            # pylint: disable=import-outside-toplevel
            from orcaid_verification_bridge import discovery_scan_for_orcaid

            gaps = discovery_scan_for_orcaid()
            if gaps:
                gap_context = "\n".join(
                    [
                        f"- [{g.get('task_type', 'unknown')}] {g.get('description', '')}"
                        for g in gaps
                    ]
                )
                self.log(f"Injecting {len(gaps)} prior gaps into analysis context")
                self.conversation.send_message(
                    f"Prior known gaps for this task type:\n{gap_context}\n\n"
                    "Note: incorporate these failure patterns into your analysis to "
                    "avoid repeating them."
                )
        except (ImportError, AttributeError, RuntimeError) as e:
            self.log(f"[VerificationBridge] discovery_scan_for_orcaid skipped: {e}")

        self.log("Starting analysis...")
        prompt = self.prompts.get("scan_analysis", "")
        self.conversation.send_message(prompt)

        try:
            self.conversation.run()
        except ConversationRunError as e:
            self.log(f"Agent run failed: {e}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.log(f"Agent run ended with unexpected error: {e}")

        self.analysis_end_time = datetime.now()
        duration = (self.analysis_end_time - self.analysis_start_time).total_seconds()
        events = self.conversation.state.events
        iterations = count_llm_iterations(events)

        metrics_after = extract_conversation_metrics(self.conversation)
        self.analysis_cost = metrics_after["cost"] - cost_before
        self.analysis_tokens = metrics_after["total_tokens"] - tokens_before

        self.log(f"Analysis completed in {duration:.1f}s")
        self.log(f"Iterations: {iterations}/{self.config.manager_max_iterations}")
        self.log(f"Cost: ${self.analysis_cost:.4f} ({self.analysis_tokens} tokens)")

        self.save_events("scan_analysis")

        analysis, analysis_logs = self.task.build_analysis_from_state()
        if analysis:
            self.analysis_result = analysis
            for msg in analysis_logs:
                self.log(msg)

        self.output_logger.log_event(
            event_type="analysis_phase_complete",
            source="manager",
            start_time=self.analysis_start_time,
            end_time=self.analysis_end_time,
            content={
                "max_iterations": self.config.manager_max_iterations,
                "actual_iterations": iterations,
                "cost": self.analysis_cost,
                "tokens": self.analysis_tokens,
                "duration": duration,
            },
        )

        return self.analysis_result

    def delegate_tasks(self) -> None:
        """Create a delegation plan and split the work between subagents."""
        self.log("Starting task delegation...")
        self.delegation_start_time = datetime.now()

        metrics_before = extract_conversation_metrics(self.conversation)
        cost_before = metrics_before["cost"]
        tokens_before = metrics_before["total_tokens"]
        event_start_idx = len(list(self.conversation.state.events))

        has_valid_delegation = self.task.check_existing_delegation(
            self.conversation.state.events, extract_json_from_events
        )

        if has_valid_delegation:
            self.log(
                "Valid delegation JSON found from scan_analysis, skipping re-prompt."
            )
        else:
            prompt = build_delegation_prompt(
                self.prompts,
                self.config.max_subagents,
            )
            self.log("Creating delegation plan...")
            self.conversation.send_message(prompt)
            try:
                self.conversation.run()
            except ConversationRunError as e:
                self.log(f"Agent run failed: {e}")
            except Exception as e:  # pylint: disable=broad-exception-caught
                self.log(f"Agent run ended with unexpected error: {e}")

        self.delegation_end_time = datetime.now()

        metrics_after = extract_conversation_metrics(self.conversation)
        self.delegation_cost = metrics_after["cost"] - cost_before
        self.delegation_tokens = metrics_after["total_tokens"] - tokens_before

        duration = (
            self.delegation_end_time - self.delegation_start_time
        ).total_seconds()
        self.log(
            f"Task delegation complete in {duration:.1f}s "
            f"(cost=${self.delegation_cost:.4f}, "
            f"tokens={self.delegation_tokens})"
        )

        self.save_events("task_delegation", event_start_idx=event_start_idx)

        # Extract and save delegation JSON
        delegation_json = extract_json_from_events(
            self.conversation.state.events, key_to_find="delegation_plan"
        )

        if not delegation_json:
            self.log("WARNING: No delegation JSON found, using fallback...")
            delegation_json = fallback_delegation(
                self.analysis_result,
                self.config.max_subagents,
            ) or {"delegation_plan": {}}

        self.delegation_plan = build_delegation_plan(delegation_json)
        output_path = Path(self.config.output_dir) / "delegations.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(delegation_json, f, indent=2)
        self.log(f"Delegation plan saved to: {output_path}")

        actual_iterations = count_llm_iterations(
            list(self.conversation.state.events)[event_start_idx:]
        )
        self.output_logger.log_event(
            event_type="delegation_complete",
            source="manager",
            start_time=self.delegation_start_time,
            end_time=self.delegation_end_time,
            content={
                "num_agents": (
                    self.delegation_plan.num_agents if self.delegation_plan else 0
                ),
                "first_round": (
                    len(self.delegation_plan.first_round_tasks)
                    if self.delegation_plan
                    else 0
                ),
                "remaining": (
                    len(self.delegation_plan.remaining_tasks)
                    if self.delegation_plan
                    else 0
                ),
                "reasoning": (
                    self.delegation_plan.reasoning if self.delegation_plan else ""
                ),
                "max_iterations": self.config.manager_max_iterations,
                "actual_iterations": actual_iterations,
                "cost": self.delegation_cost,
                "tokens": self.delegation_tokens,
                "duration": duration,
            },
        )

    def onboard_subagents(self) -> list[SubAgent]:
        """
        Prepare subagents and their worktrees based on the delegation plan.

        Returns:
            list[SubAgent]: List of initialized subagents.
        """
        if not self.delegation_plan:
            raise RuntimeError("Delegation not completed. Call delegate_tasks() first.")

        self.log("=" * 60)
        self.log("Onboard Subagents")
        self.log("=" * 60)

        subagents = []
        first_round_tasks = self.delegation_plan.first_round_tasks

        if not first_round_tasks:
            self.log("No tasks in first round, skipping onboarding")
            return subagents

        # Group tasks by engineer_id to avoid creating duplicate worktrees
        tasks_by_engineer = {}
        for task in first_round_tasks:
            if task.engineer_id not in tasks_by_engineer:
                tasks_by_engineer[task.engineer_id] = []
            tasks_by_engineer[task.engineer_id].append(task)

        self.log(
            f"Creating {len(tasks_by_engineer)} git worktrees for {len(first_round_tasks)} tasks..."
        )

        # commit0: use self.repo_dir; paperbench: use /workspace/submission
        git_base_dir = self.repo_dir

        result = self.workspace.execute_command(
            f"cd {git_base_dir} && git rev-parse HEAD", timeout=30
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to get current commit: {result.stderr}")
        base_commit = result.stdout.strip()
        self.log(f"Base commit: {base_commit[:8]}")

        for engineer_id, tasks in tasks_by_engineer.items():
            primary_task = tasks[0]

            subagent, combine_log = self.task.build_subagent(
                engineer_id, primary_task, tasks
            )
            worktree_name = self.task.get_worktree_name(engineer_id)

            subagent.worktree_path = f"/workspace/{worktree_name}"
            subagent.base_commit = base_commit

            self.log(f"Creating worktree for {engineer_id}...")
            if combine_log:
                self.log(combine_log)

            branch_cmd = (
                f"cd {git_base_dir} && "
                f"git branch {subagent.branch_name} {base_commit} 2>/dev/null || true"
            )
            self.workspace.execute_command(branch_cmd, timeout=30)

            worktree_cmd = (
                f"cd {git_base_dir} && "
                f"git worktree add {subagent.worktree_path} {subagent.branch_name}"
            )
            result = self.workspace.execute_command(worktree_cmd, timeout=60)

            if result.exit_code != 0:
                self.log(
                    f"WARNING: Failed to create worktree for {engineer_id}: {result.stderr}"
                )
                subagent.status = "failed"
            else:
                subagent.status = "ready"
                self.log(
                    f"  {engineer_id}: {subagent.worktree_path} (branch: {subagent.branch_name})"
                )

            subagents.append(subagent)

        self.output_logger.log_event(
            event_type="onboarding_complete",
            source="manager",
            content={
                "num_subagents": len(subagents),
                "subagents": [s.to_dict() for s in subagents],
                "base_commit": base_commit,
            },
        )

        self.log("Onboarding complete:")
        self.log(f"  Subagents created: {len(subagents)}")
        for s in subagents:
            status_icon = (
                "subagent is ready" if s.status == "ready" else "subagent is not ready"
            )
            self.log(f"  {status_icon} {s.engineer_id}: {s.worktree_path}")
            for line in self.task.get_subagent_log_lines(s):
                self.log(line)

        return subagents

    def cleanup(self) -> None:
        """Close the conversation and release resources."""
        if self.conversation:
            try:
                self.conversation.close()
            except Exception as e:  # pylint: disable=broad-exception-caught
                self.log(f"Warning during cleanup: {e}")
