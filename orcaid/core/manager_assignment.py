"""
Assignment mixin for the Manager class.
Handles task assignment decisions.
"""

from datetime import datetime
import yaml

from openhands.sdk.conversation.exceptions import ConversationRunError

from orcaid.bridge import ORCHESTRATOR_MEMORY_BASE
from orcaid.config import SubAgent
from orcaid.core.utils import (
    count_llm_iterations,
    extract_conversation_metrics,
    extract_json_from_events,
)


class AssignmentMixin:
    """Mixin for task assignment operations in the Manager."""

    def __init__(self) -> None:
        super().__init__()
        self.assign_task_total_time: float = 0.0
        self.assign_task_total_cost: float = 0.0
        self.assign_task_total_tokens: int = 0

    def assign_task(
        self,
        completed_result,
        all_completed,
        running_agents,
        idle_agents=None,
        inactive_agents=None,
        finished_agents=None,
    ) -> dict:
        """
        Decide the next task assignment for an engineer.

        Args:
            completed_result: The result of the task just completed.
            all_completed: List of all completed task results.
            running_agents: List of currently running agents.
            idle_agents: List of idle agents.
            inactive_agents: List of inactive agents.
            finished_agents: List of agents that have finished all rounds.

        Returns:
            dict: The assignment decision.
        """
        engineer_id = completed_result.engineer_id

        # Determine task status based on merge result
        if completed_result.merged and completed_result.success:
            task_status = "success"
        elif completed_result.merged and not completed_result.success:
            task_status = "recovered"
        else:
            task_status = "failed"

        self.log(f"{engineer_id} completed ({task_status}), checking for next task...")

        completed_task_summary = self.task.build_completed_task_summary(
            completed_result, task_status
        )
        if completed_result.error and not completed_result.merged:
            completed_task_summary += f"\nerror: {completed_result.error}"

        running_agents_summary = (
            "\n".join(f"  - {aid}" for aid in running_agents)
            if running_agents
            else "  none"
        )

        idle_agents = idle_agents or []
        if idle_agents:
            idle_agents_summary = "\n".join(f"  - {aid}" for aid in idle_agents)
        else:
            idle_agents_summary = "  none"

        inactive_agents = inactive_agents or []
        if inactive_agents:
            inactive_agents_summary = "\n".join(f"  - {aid}" for aid in inactive_agents)
        else:
            inactive_agents_summary = "  none"

        finished_agents = finished_agents or []
        if finished_agents:
            finished_agents_summary = "\n".join(f"  - {aid}" for aid in finished_agents)
        else:
            finished_agents_summary = "  none"

        # Load historical performance context to guide assignment decisions
        history_summary_lines = []
        index_path = ORCHESTRATOR_MEMORY_BASE / "index" / "discovery.yaml"
        if index_path.exists():
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    index_data = yaml.safe_load(f) or {}

                # Format task type stats
                task_types = index_data.get("task_types", {})
                if task_types:
                    history_summary_lines.append("Historical Task Type Performance & Drift:")
                    for t_type, stats in task_types.items():
                        history_summary_lines.append(
                            f"  - {t_type}: completed={stats.get('total_completed', 0)}, "
                            f"failed={stats.get('total_failed', 0)}, "
                            f"drift_rate={stats.get('drift_rate', 0.0):.1%}"
                        )

                # Format profile stats
                profiles = index_data.get("profiles", {})
                if profiles:
                    history_summary_lines.append("Historical Subagent Profile Performance:")
                    for p_name, stats in profiles.items():
                        history_summary_lines.append(
                            f"  - {p_name}: completed={stats.get('total_completed', 0)}, "
                            f"failed={stats.get('total_failed', 0)}, "
                            f"drift_rate={stats.get('drift_rate', 0.0):.1%}"
                        )
            except Exception:
                pass

        history_context = "\n".join(history_summary_lines) if history_summary_lines else "  No historical performance data available."

        prompt = self.prompts.get("assign_task", "").format(
            engineer_id=engineer_id,
            task_status=task_status,
            completed_round=completed_result.round_num,
            max_rounds=self.config.max_rounds_chat,
            completed_task_summary=completed_task_summary,
            running_agents_summary=running_agents_summary,
            idle_agents_summary=idle_agents_summary,
            inactive_agents_summary=inactive_agents_summary,
            finished_agents_summary=finished_agents_summary,
        )

        prompt += f"\n\n### Historical Subagent Drift and Performance Context\n{history_context}\n"

        # Track time and cost for this assign_task call
        assign_start_time = datetime.now()
        event_count_before = len(list(self.conversation.state.events))

        iteration_before = count_llm_iterations(self.conversation.state.events)
        metrics_before = extract_conversation_metrics(self.conversation)
        cost_before = metrics_before["cost"]
        tokens_before = metrics_before["total_tokens"]

        self.log("Deciding next task assignment...")
        self.conversation.send_message(prompt)
        try:
            self.conversation.run()
        except ConversationRunError as e:
            self.log(f"Agent run ended with: {e}")

        events = self.conversation.state.events
        iterations = count_llm_iterations(events) - iteration_before
        self.log(f"Iterations: {iterations}/{self.config.manager_max_iterations}")

        review_json = extract_json_from_events(events, key_to_find="assign_task")

        if not review_json:
            alternative = self.task.search_alternative_json(
                events, extract_json_from_events, self.log
            )
            if alternative:
                review_json = alternative

        if not review_json:
            self.log("No assign_task JSON found, no assignment")
            self.save_events("assign_and_review", event_count_before)
            return {"assignments": [], "reasoning": "No response from manager"}

        assign_data = review_json.get("assign_task", {})
        reasoning = assign_data.get("reasoning", "")

        assignments_data = self.task.extract_assignments(assign_data)

        self.log(f"Decision: {len(assignments_data)} task(s) to assign")
        self.log(f"Reasoning: {reasoning}")

        result = {"assignments": [], "reasoning": reasoning}

        # Validation sets (applies to all tasks)
        running_set = set(running_agents or [])
        finished_set = set(finished_agents or [])
        assigned_agents = set()
        assigned_tasks = set()

        assign_context = (
            self.task.get_assign_context(all_completed, self.workspace, self.repo_dir)
            if assignments_data
            else {}
        )

        for task_data in assignments_data:
            task_engineer_id = task_data.get("engineer_id", engineer_id)
            task_id = task_data.get("task_id", "")

            # Validate: reject assignments to running/finished/duplicate agents/tasks
            if task_engineer_id in running_set:
                self.log(
                    f"REJECTED: Cannot assign to {task_engineer_id} - agent is currently running"
                )
                continue
            if task_engineer_id in finished_set:
                self.log(
                    f"REJECTED: Cannot assign to {task_engineer_id} "
                    "- agent already finished all rounds"
                )
                continue
            if task_engineer_id in assigned_agents:
                self.log(
                    f"REJECTED: Cannot assign to {task_engineer_id} - already assigned a task"
                )
                continue
            if task_id and task_id in assigned_tasks:
                self.log(
                    f"REJECTED: Cannot assign {task_id} - already assigned to another agent"
                )
                continue

            # Remove from remaining tasks
            if self.delegation_plan and self.delegation_plan.remaining_tasks:
                self.delegation_plan.remaining_tasks = [
                    t
                    for t in self.delegation_plan.remaining_tasks
                    if t.task_id != task_id
                ]

            # Create SubAgent with all available fields
            subagent = SubAgent(
                engineer_id=task_engineer_id,
                task_id=task_id,
                file_path=task_data.get("file_path", ""),
                functions_to_implement=task_data.get("functions_to_implement", []),
                task_node_id=task_data.get("task_node_id", ""),
                requirements=task_data.get("requirements", ""),
                instruction=task_data.get("instruction", ""),
                estimated_complexity=task_data.get("estimated_complexity", "medium"),
                task_category=task_data.get("task_category"),
                submission_path=self.task.get_work_dir(),
            )

            self.task.update_subagent_for_assignment(
                subagent, assign_context, self.workspace, self.log
            )

            result["assignments"].append(subagent)
            assigned_agents.add(task_engineer_id)
            if task_id:
                assigned_tasks.add(task_id)
            self.log(f"Assigned {task_id} to {task_engineer_id}")

        if result["assignments"]:
            self.current_round = max(self.current_round, 2)

        # Calculate time and cost
        assign_end_time = datetime.now()
        assign_duration = (assign_end_time - assign_start_time).total_seconds()
        self.assign_task_total_time += assign_duration

        metrics_after = extract_conversation_metrics(self.conversation)
        assign_cost = metrics_after["cost"] - cost_before
        assign_tokens = metrics_after["total_tokens"] - tokens_before
        self.assign_task_total_cost += assign_cost
        self.assign_task_total_tokens += assign_tokens

        assigned_targets = self.task.get_assigned_targets(
            result["assignments"], engineer_id
        )

        event_content = {
            "completed_task": completed_result.task_id,
            "task_status": task_status,
            "num_assignments": len(assignments_data),
            "reasoning": reasoning,
            "assignments": [
                {"engineer_id": s.engineer_id, "task_id": s.task_id}
                for s in result["assignments"]
            ],
            "remaining_tasks": (
                len(self.delegation_plan.remaining_tasks) if self.delegation_plan else 0
            ),
            "actual_iterations": iterations,
            "max_iterations": self.config.manager_max_iterations,
            "cost": assign_cost,
            "tokens": assign_tokens,
            "duration": assign_duration,
        }
        event_content.update(self.task.get_assign_event_extras(engineer_id))

        self.output_logger.log_event(
            event_type="manager_instruction",
            source="manager",
            target=assigned_targets,
            round_num=self.current_round if assignments_data else None,
            content=event_content,
            start_time=assign_start_time,
            end_time=assign_end_time,
        )

        self.save_events("assign_and_review", event_count_before)
        return result
