import os
from datetime import datetime
from pathlib import Path

from orcaid.core.utils import (
    count_llm_iterations,
    extract_conversation_metrics,
)


class ReviewMixin:
    """Mixin for review-related operations in the Manager."""

    def collect_and_merge(self, subagent_result, output_logger=None) -> dict:
        """
        Collect results from a subagent and merge their changes.

        Args:
            subagent_result: The result returned by the subagent.
            output_logger: Logger for recording events.

        Returns:
            dict: The result of the review and merge operation.
        """
        review_start_time = datetime.now()
        engineer_id = subagent_result.engineer_id
        task_id = subagent_result.task_id
        branch_name = subagent_result.branch_name
        worktree_path = subagent_result.worktree_path

        self.log(f"Collecting {engineer_id}'s work...")
        self.log(f"  - Task: {task_id}")
        extra_log = self.task.get_collect_extra_log(subagent_result)
        if extra_log:
            self.log(extra_log)
        self.log(f"  - Success: {subagent_result.success}")
        self.log(f"  - Commit: {subagent_result.commit_hash or 'None'}")
        self.log(f"  - Worktree: {worktree_path or 'None'}")

        review_result = {
            "engineer_id": engineer_id,
            "task_id": task_id,
            "subagent_success": subagent_result.success,
            "merged": False,
            "merge_message": "",
            "review_notes": "",
            "merge_method": "",
            "conflict_files": [],
        }

        round_num = subagent_result.round_num
        files_modified = subagent_result.files_modified or []

        # Subagent made a commit - try to merge via branch
        if subagent_result.success and subagent_result.commit_hash and branch_name:
            self.log("Attempting branch merge (commit found)...")
            merge_success, merge_message, conflict_files = self.merge_branch(
                branch_name
            )

            if conflict_files:
                self.log(f"Merge conflict - engineer must resolve: {conflict_files}")
                review_result["merge_method"] = "conflict"
                review_result["merge_message"] = merge_message
                review_result["conflict_files"] = conflict_files
                review_result["review_notes"] = (
                    f"Merge conflict in {len(conflict_files)} files, needs engineer resolution"
                )

                if output_logger:
                    output_logger.log_manager_review(
                        engineer_id=engineer_id,
                        task_id=task_id,
                        merged=False,
                        review_reason=f"Merge conflict: {', '.join(conflict_files)}",
                        commit_hash=subagent_result.commit_hash,
                        files_modified=files_modified,
                        round_num=round_num,
                        start_time=review_start_time,
                        end_time=datetime.now(),
                    )

                return review_result

            if merge_success:
                review_result["merged"] = True
                review_result["merge_message"] = merge_message
                review_result["review_notes"] = (
                    "Implementation approved and merged via branch"
                )
                review_result["merge_method"] = "branch_merge"
                self.log(f"Collect: MERGED - {merge_message}")

                if output_logger:
                    output_logger.log_manager_review(
                        engineer_id=engineer_id,
                        task_id=task_id,
                        merged=True,
                        review_reason=merge_message,
                        commit_hash=subagent_result.commit_hash,
                        files_modified=files_modified,
                        round_num=round_num,
                        start_time=review_start_time,
                        end_time=datetime.now(),
                    )

                return self._verify_and_return(subagent_result, review_result)
            else:
                self.log(f"Branch merge failed: {merge_message}")

        # No commit or branch merge failed - try to commit and merge uncommitted changes
        if self.task.should_try_uncommitted_merge and worktree_path and branch_name:
            self.log("Checking for uncommitted changes in worktree...")
            commit_success, commit_message, committed_files = (
                self.commit_worktree_changes(
                    worktree_path, branch_name, engineer_id, task_id
                )
            )

            if commit_success and committed_files:
                review_result["merged"] = True
                review_result["merge_message"] = commit_message
                review_result["review_notes"] = (
                    "Uncommitted changes committed and merged"
                )
                review_result["merge_method"] = "worktree_commit_merge"
                files_modified = committed_files
                self.log(f"Collect: MERGED (worktree commit+merge) - {commit_message}")

                if output_logger:
                    output_logger.log_manager_review(
                        engineer_id=engineer_id,
                        task_id=task_id,
                        merged=True,
                        review_reason=f"Committed and merged: {commit_message}",
                        commit_hash=None,
                        files_modified=committed_files,
                        round_num=round_num,
                        start_time=review_start_time,
                        end_time=datetime.now(),
                    )

                return self._verify_and_return(subagent_result, review_result)
            else:
                self.log(f"No uncommitted changes to merge: {commit_message}")

        # Neither commit nor uncommitted changes available
        error_reason = (
            subagent_result.error
            or "No changes found (no commit and no uncommitted changes)"
        )
        review_result["review_notes"] = f"No changes to merge: {error_reason}"
        review_result["merge_method"] = "none"
        self.log(f"Collect: NO CHANGES - {error_reason}")

        if output_logger:
            output_logger.log_manager_review(
                engineer_id=engineer_id,
                task_id=task_id,
                merged=False,
                review_reason=error_reason,
                round_num=round_num,
                start_time=review_start_time,
                end_time=datetime.now(),
            )

        return self._verify_and_return(subagent_result, review_result)

    def _verify_and_return(self, subagent_result, review_result) -> dict:
        """
        Verification bridge: score subagent output and handle drift.
        Injects delegation-verification after collect_and_merge builds review_result.

        Args:
            subagent_result: The result returned by the subagent.
            review_result: The review result built by collect_and_merge.

        Returns:
            dict: The (potentially modified) review result.
        """
        try:
            from orcaid.bridge import (
                escalate_to_human,
                orcaid_reinvoke_subagent,
                verify_subagent_completion,
            )
        except ImportError:
            try:
                import sys

                sys.path.insert(0, os.path.dirname(__file__))
                from orcaid_verification_bridge import (
                    escalate_to_human,
                    orcaid_reinvoke_subagent,
                    verify_subagent_completion,
                )
            except ImportError:
                # Bridge not installed — skip verification, return original review_result
                self.log(
                    "[VerificationBridge] Verification bridge not found — skipping"
                )
                return review_result

        orchestrator_memory_base = Path(
            os.environ.get(
                "ORCHESTRATOR_MEMORY_BASE",
                str(Path.home() / ".hermes" / "orchestrator-memory"),
            )
        )

        try:
            verification = verify_subagent_completion(
                subagent_result=subagent_result,
                review_result=review_result,
                task_module=self.task,
                orchestrator_memory_base=orchestrator_memory_base,
            )
        except Exception as e:
            self.log(f"[VerificationBridge] Verification failed: {e}")
            return review_result

        if verification.verdict == "pass":
            self.log(
                f"[VerificationBridge] PASS — {subagent_result.task_id} verified, written to orchestrator-memory"
            )
            return review_result

        elif verification.verdict == "fail":
            if verification.retry_recommended and verification.correction_context:
                self.log(
                    f"[VerificationBridge] FAIL (retry {verification.attempt_number}) — {subagent_result.task_id}"
                )
                # Queue retry via bridge
                retry_task = orcaid_reinvoke_subagent(
                    manager=self,
                    engineer_id=subagent_result.engineer_id,
                    original_task_id=subagent_result.task_id,
                    correction_context=verification.correction_context,
                    max_retries=3,
                )
                if retry_task:
                    self.log(f"[VerificationBridge] Retry queued: {retry_task.task_id}")
                    review_result["verification_status"] = "retry_scheduled"
                    review_result["retry_task_id"] = retry_task.task_id
                else:
                    self.log(
                        "[VerificationBridge] Could not queue retry — returning review_result anyway"
                    )
            else:
                self.log(
                    f"[VerificationBridge] FAIL (escalate) — {subagent_result.task_id}"
                )
                escalate_to_human(subagent_result, verification, review_result)
                review_result["verification_status"] = "escalated"
                review_result["verification_drift"] = verification.drift_log

            return review_result

        # fallback: return original
        return review_result

    def final_review_all(self, subagent_results, max_iterations=30) -> dict:
        """
        Manager final review after all engineers complete their work.

        Args:
            subagent_results: List of results from all subagents.
            max_iterations: Maximum iterations for the final review run.

        Returns:
            dict: Statistics about the final review.
        """
        self.log("=" * 60)
        self.log("Manager Final Review")
        self.log("=" * 60)

        final_review_start = datetime.now()
        event_count_before = len(list(self.conversation.state.events))

        metrics_before = extract_conversation_metrics(self.conversation)
        cost_before = metrics_before["cost"]
        tokens_before = metrics_before["total_tokens"]
        iteration_before = count_llm_iterations(self.conversation.state.events)

        # Build engineers summary
        engineers_summary_lines = []
        for r in subagent_results:
            if r.file_path:
                status = "committed" if r.success else "no commit"
                merged = "merged" if r.merged else "not merged"
                engineers_summary_lines.append(
                    f"- {r.engineer_id}: {r.task_id} ({r.file_path}) - {status}, {merged}"
                )
            else:
                status = "submitted" if r.success else "no submission"
                merged = "collected" if r.merged else "not collected"
                commit_msg = (
                    f" | commit: {r.commit_message}" if r.commit_message else ""
                )
                engineers_summary_lines.append(
                    f"- {r.engineer_id}: {r.task_id} ({r.requirements[:50]}...) - {status}, {merged}{commit_msg}"
                )
        engineers_summary = (
            "\n".join(engineers_summary_lines)
            if engineers_summary_lines
            else "No engineers completed tasks"
        )

        # Build merged files summary
        merged_files_lines = []
        for r in subagent_results:
            if r.merged:
                if r.file_path:
                    merged_files_lines.append(f"- {r.file_path}")
                elif r.files_modified:
                    for f in r.files_modified:
                        merged_files_lines.append(
                            f"- {f} ({r.engineer_id}: {r.task_id})"
                        )
        merged_files_summary = (
            "\n".join(sorted(set(merged_files_lines)))
            if merged_files_lines
            else "No files merged"
        )

        # Build unmerged worktrees section (commit0-specific, ignored by paperbench prompt)
        unmerged_lines = []
        for r in subagent_results:
            if not r.merged and r.worktree_path:
                uncommitted_files = self.get_uncommitted_changes(r.worktree_path)
                files_info = (
                    f" (uncommitted files: {', '.join(uncommitted_files)})"
                    if uncommitted_files
                    else " (no uncommitted files found)"
                )
                unmerged_lines.append(
                    f"- {r.engineer_id}: {r.file_path} - worktree at {r.worktree_path}, "
                    f"branch {r.branch_name}{files_info}"
                )
        if unmerged_lines:
            unmerged_worktrees_section = (
                "<unmerged_worktrees>\n"
                "The following engineers did NOT merge their work. Their worktrees may contain useful code:\n"
                + "\n".join(unmerged_lines)
                + "\n</unmerged_worktrees>"
            )
        else:
            unmerged_worktrees_section = ""

        # Get test info (commit0-specific, ignored by paperbench prompt)
        test_info = (self.task.task_data or {}).get("test", {})
        test_cmd = test_info.get(
            "test_cmd", (self.task.task_data or {}).get("test_cmd", "pytest")
        )
        test_dir = test_info.get(
            "test_dir", (self.task.task_data or {}).get("test_dir", "tests/")
        )

        prompt = self.prompts.get("manager_final_review_all", "").format(
            engineers_summary=engineers_summary,
            merged_files_summary=merged_files_summary,
            unmerged_worktrees_section=unmerged_worktrees_section,
            repo_dir=self.repo_dir,
            test_cmd=test_cmd,
            test_dir=test_dir,
        )

        if not prompt:
            self.log("No manager_final_review_all prompt found, skipping")
            return {"skipped": True}

        # Temporarily change max_iterations for final review
        original_max_iter = self.conversation.max_iteration_per_run
        self.conversation.max_iteration_per_run = max_iterations

        try:
            self.log(f"Starting final review (max {max_iterations} iterations)...")
            self.conversation.send_message(prompt)
            try:
                self.conversation.run()
            except Exception as e:
                self.log(f"Agent run ended with: {e}")
        finally:
            self.conversation.max_iteration_per_run = original_max_iter

        final_review_end = datetime.now()
        final_review_duration = (final_review_end - final_review_start).total_seconds()
        self.final_review_total_time = final_review_duration

        events = self.conversation.state.events
        iterations = count_llm_iterations(events) - iteration_before

        metrics_after = extract_conversation_metrics(self.conversation)
        self.final_review_cost = metrics_after["cost"] - cost_before
        self.final_review_tokens = metrics_after["total_tokens"] - tokens_before

        self.log(f"Final review completed in {final_review_duration:.1f}s")
        self.log(f"Iterations: {iterations}/{max_iterations}")
        self.log(
            f"Cost: ${self.final_review_cost:.4f} ({self.final_review_tokens} tokens)"
        )

        log_content = {
            "duration": final_review_duration,
            "cost": self.final_review_cost,
            "tokens": self.final_review_tokens,
            "actual_iterations": iterations,
            "max_iterations": max_iterations,
            "engineers_reviewed": len(subagent_results),
        }
        log_content.update(self.task.get_final_review_log_extras(subagent_results))

        self.output_logger.log_event(
            event_type="manager_final_review_all",
            source="manager",
            start_time=final_review_start,
            end_time=final_review_end,
            content=log_content,
        )

        self.save_events("final_review_all", event_count_before)

        # Trigger Synthesis Hook
        try:
            from orcaid.bridge import synthesize_orcaid_outcome, write_compound_skill

            task_type = (
                "code_review"
                if "Commit" in self.task.__class__.__name__
                else "research_reproduction"
            )
            synthesis = synthesize_orcaid_outcome(
                subagent_results=subagent_results,
                manager_review_results=[{"merged": r.merged} for r in subagent_results],
                task_type=task_type,
            )
            orchestrator_memory_base = Path(
                os.environ.get(
                    "ORCHESTRATOR_MEMORY_BASE",
                    str(Path.home() / ".hermes" / "orchestrator-memory"),
                )
            )
            write_compound_skill(synthesis, memory_base=orchestrator_memory_base)
            self.log("[VerificationBridge] Successfully wrote compound synthesis skill")
        except Exception as e:
            self.log(f"[VerificationBridge] Synthesis skipped/failed: {e}")

        return {
            "duration": final_review_duration,
            "cost": self.final_review_cost,
            "tokens": self.final_review_tokens,
            "iterations": iterations,
        }
