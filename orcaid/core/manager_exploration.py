"""
Exploration mixin for the Manager class.
Handles background exploration of the codebase.
"""

import asyncio
from datetime import datetime

from openhands.sdk.conversation.exceptions import ConversationRunError

from core.utils import (
    count_llm_iterations,
    extract_conversation_metrics,
)


class ExplorationMixin:
    """Mixin for background exploration operations in the Manager."""

    def __init__(self) -> None:
        """Initialise ExplorationMixin metric accumulators."""
        self.exploration_total_time: float = 0.0
        self.exploration_cost: float = 0.0
        self.exploration_tokens: int = 0

    def cancel_exploration(self) -> None:
        """Cancel background exploration."""
        self.exploration_cancelled = True
        self.log("Exploration cancelled - engineer completed")
        if self.conversation:
            self.conversation.pause()

    def reset_exploration_cancel(self) -> None:
        """Reset the exploration cancellation flag."""
        self.exploration_cancelled = False

    def explore_background(
        self, remaining_tasks: list, running_agents_summary: str
    ) -> dict:
        """
        Perform background exploration for upcoming tasks.

        Args:
            remaining_tasks: List of tasks that haven't been implementation yet.
            running_agents_summary: Summary of currently running agents.

        Returns:
            dict: Findings from the exploration.
        """
        if self.exploration_cancelled:
            self.log("Exploration skipped - already cancelled")
            self.output_logger.log_event(
                event_type="background_exploration",
                source="manager",
                content={
                    "duration": 0,
                    "cost": 0,
                    "tokens": 0,
                    "cancelled": True,
                    "skipped_early": True,
                    "remaining_tasks_explored": 0,
                },
            )
            return {"findings": [], "cancelled": True}

        self.log("Starting background exploration...")
        explore_start = datetime.now()
        event_count_before = len(list(self.conversation.state.events))

        metrics_before = extract_conversation_metrics(self.conversation)
        cost_before = metrics_before["cost"]
        tokens_before = metrics_before["total_tokens"]
        iteration_before = count_llm_iterations(self.conversation.state.events)

        remaining_files = []
        for task in remaining_tasks[:5]:
            remaining_files.append(
                f"- {task.file_path}: {', '.join(task.functions_to_implement[:3])}"
            )
        remaining_str = (
            "\n".join(remaining_files) if remaining_files else "No remaining tasks"
        )

        prompt = self.prompts.get("background_exploration", "").format(
            remaining_tasks=remaining_str,
            running_agents_summary=running_agents_summary,
            repo_dir=self.repo_dir,
        )

        if not prompt:
            self.log("No background_exploration prompt found, skipping")
            return {"skipped": True}

        try:
            self.log("Exploring for upcoming tasks...")
            self.conversation.send_message(prompt)
            self.conversation.run()

            findings = {
                "findings": [],
                "cancelled": self.exploration_cancelled,
            }

            events = self.conversation.state.events
            iterations = count_llm_iterations(events) - iteration_before
            self.log(f"Exploration iterations: {iterations}")

            self.exploration_findings.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "iterations": iterations,
                    "remaining_files": [t.file_path for t in remaining_tasks[:5]],
                }
            )

        except ConversationRunError as e:
            self.log(f"Exploration error (non-fatal): {e}")
            findings = {"findings": [], "error": str(e), "cancelled": False}

        explore_end = datetime.now()
        explore_duration = (explore_end - explore_start).total_seconds()
        self.exploration_total_time += explore_duration

        metrics_after = extract_conversation_metrics(self.conversation)
        explore_cost = metrics_after["cost"] - cost_before
        explore_tokens = metrics_after["total_tokens"] - tokens_before
        self.exploration_cost += explore_cost
        self.exploration_tokens += explore_tokens

        self.log(
            f"Exploration completed in {explore_duration:.1f}s (${explore_cost:.4f})"
        )

        self.output_logger.log_event(
            event_type="background_exploration",
            source="manager",
            start_time=explore_start,
            end_time=explore_end,
            content={
                "duration": explore_duration,
                "cost": explore_cost,
                "tokens": explore_tokens,
                "cancelled": self.exploration_cancelled,
                "remaining_tasks_explored": len(remaining_tasks[:5]),
            },
        )

        self.save_events("background_exploration", event_count_before)
        return findings

    async def explore_background_async(
        self, remaining_tasks: list, running_agents_summary: str
    ) -> dict:
        """
        Async version of explore_background.

        Args:
            remaining_tasks: List of tasks that haven't been implementation yet.
            running_agents_summary: Summary of currently running agents.

        Returns:
            dict: Findings from the exploration.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.explore_background,
            remaining_tasks,
            running_agents_summary,
        )
