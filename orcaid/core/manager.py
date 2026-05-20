"""
Manager module for OrCAID.
Handles task analysis, delegation, and subagent orchestration.
"""

# pylint: disable=no-member

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from openhands.sdk import Agent, BaseConversation, Conversation, LLMSummarizingCondenser
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.tools.preset.default import get_default_tools

from orcaid.config import SubAgent
from orcaid.core.manager_assignment import AssignmentMixin
from orcaid.core.manager_exploration import ExplorationMixin
from orcaid.core.manager_git import GitMixin
from orcaid.core.manager_review import ReviewMixin
from orcaid.core.utils import (
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

    def _run_pre_scan_scripts(self) -> bool:
        """
        Step A of the skill-based refactor: run deterministic Python scripts
        to extract pass stubs and dependency graph BEFORE the scan_analysis
        LLM call.

        The scripts run **inside the workspace container** via
        ``self.workspace.execute_command``, NOT on the host. The repo lives
        only inside the Docker workspace (cloned by ``setup_workspace``);
        the host has no copy. The earlier host-side ``Path(self.repo_dir).exists()``
        check failed on every commit0 run because ``/workspace/<repo>_repo``
        is a container path.

        Returns True if both scripts produced usable data — the caller can
        then build ``self.analysis_result`` directly via
        ``_build_analysis_from_prescan`` and *skip* the LLM exploration step
        entirely. Returns False on any failure (no workspace, non-Python repo,
        script error, no pass-stubs found), in which case the caller should
        fall back to the legacy LLM exploration path.
        """
        self._pre_scan_pass_data = None
        self._pre_scan_dep_data = None

        if not getattr(self, "workspace", None):
            self.log("[pre-scan] no workspace available — falling back to LLM exploration")
            return False

        skills_root = Path(__file__).parent.parent / "skills"
        pass_script = skills_root / "repo-scan" / "scripts" / "find_pass_stubs.py"
        dep_script = skills_root / "dependency-graph" / "scripts" / "build_dependency_graph.py"

        if not pass_script.exists() or not dep_script.exists():
            self.log(f"[pre-scan] skill scripts missing at {skills_root}, falling back")
            return False

        # 1. find_pass_stubs.py inside the workspace container
        pass_output = self._run_script_in_workspace(
            pass_script, self.repo_dir, timeout=60
        )
        if pass_output is None:
            self.log("[pre-scan] find_pass_stubs.py failed in workspace, falling back to LLM")
            return False

        try:
            pass_data = json.loads(pass_output)
        except json.JSONDecodeError:
            self.log("[pre-scan] find_pass_stubs.py returned invalid JSON, falling back")
            return False

        pass_files = pass_data.get("files", [])
        self.log(f"[pre-scan] found {len(pass_files)} files with pass stubs in {self.repo_dir}")

        if not pass_files:
            self.log(
                "[pre-scan] zero pass-stubs found — non-commit0 repo or non-Python? "
                "falling back to LLM exploration"
            )
            return False

        # 2. build_dependency_graph.py — takes pass_files JSON on stdin
        dep_output = self._run_script_in_workspace(
            dep_script,
            self.repo_dir,
            stdin_json={"files": pass_files},
            timeout=60,
        )

        dep_data: dict = {"repo": self.repo_dir, "graph": {}, "files_analyzed": 0}
        if dep_output is not None:
            try:
                dep_data = json.loads(dep_output)
            except json.JSONDecodeError:
                self.log("[pre-scan] build_dependency_graph.py returned invalid JSON — proceeding without deps")
        else:
            self.log("[pre-scan] build_dependency_graph.py failed — proceeding without deps")

        self._pre_scan_pass_data = pass_data
        self._pre_scan_dep_data = dep_data
        return True

    def _run_script_in_workspace(
        self,
        script_path: Path,
        repo_dir: str,
        *,
        stdin_json: dict | None = None,
        timeout: int = 60,
    ) -> str | None:
        """Run a host-side Python script inside the workspace container.

        We base64-encode the script source on the host, ship it into the
        container's filesystem via ``execute_command``, run it with the
        target repo path as argv[1], and capture stdout. Optional stdin is
        also base64-shipped so multi-line JSON survives shell quoting intact.

        Returns trimmed stdout on success, None on any failure (the caller
        is expected to fall back to the LLM exploration path).
        """
        import base64
        import shlex
        import uuid

        try:
            script_b64 = base64.b64encode(script_path.read_bytes()).decode("ascii")
        except OSError as e:
            self.log(f"[pre-scan] could not read {script_path}: {e}")
            return None

        tmp = f"/tmp/orcaid_skill_{uuid.uuid4().hex[:10]}.py"
        repo_quoted = shlex.quote(repo_dir)

        if stdin_json is not None:
            try:
                stdin_b64 = base64.b64encode(
                    json.dumps(stdin_json).encode("utf-8")
                ).decode("ascii")
            except (TypeError, ValueError) as e:
                self.log(f"[pre-scan] could not encode stdin payload: {e}")
                return None
            cmd = (
                f"echo {script_b64} | base64 -d > {tmp} && "
                f"echo {stdin_b64} | base64 -d | python3 {tmp} {repo_quoted}; "
                f"rc=$?; rm -f {tmp}; exit $rc"
            )
        else:
            cmd = (
                f"echo {script_b64} | base64 -d > {tmp} && "
                f"python3 {tmp} {repo_quoted}; "
                f"rc=$?; rm -f {tmp}; exit $rc"
            )

        try:
            result = self.workspace.execute_command(cmd, timeout=timeout)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.log(f"[pre-scan] workspace.execute_command failed: {e}")
            return None

        if getattr(result, "exit_code", 1) != 0:
            stderr = getattr(result, "stderr", "") or ""
            self.log(
                f"[pre-scan] {script_path.name} exit_code={result.exit_code} "
                f"stderr={stderr[:200]}"
            )
            return None
        out = getattr(result, "stdout", "") or ""
        return out.strip() or None

    def _build_analysis_from_prescan(self):
        """Construct an ``AnalysisResult`` directly from pre-scan script output.

        This is what makes Step A actually *replace* the LLM exploration
        instead of augmenting it. The deterministic scripts already know
        every fact the LLM was being asked to discover: which files have
        ``pass`` stubs, which functions inside them, and which other pass
        files they import from. Build the analysis dataclass from those facts
        and the LLM exploration step becomes redundant.

        Returns the constructed ``AnalysisResult`` or ``None`` if pre-scan
        data is missing/empty (caller should fall back to LLM path).
        """
        from orcaid.config import AnalysisResult

        pass_data = getattr(self, "_pre_scan_pass_data", None) or {}
        dep_data = getattr(self, "_pre_scan_dep_data", None) or {}

        files = pass_data.get("files", [])
        if not files:
            return None

        pass_files = [pf["file"] for pf in files]
        functions_by_file = {
            pf["file"]: list(pf.get("functions", [])) for pf in files
        }
        blocking_dependencies = dep_data.get("graph", {}) or {}
        total_funcs = sum(len(pf.get("functions", [])) for pf in files)

        # Implementation order: same heuristic as build_analysis_result() in
        # utils.py — files that *more other files depend on* go first.
        blocked_by_count: dict[str, int] = {}
        for deps in blocking_dependencies.values():
            for dep in deps or []:
                blocked_by_count[dep] = blocked_by_count.get(dep, 0) + 1
        implementation_order = sorted(
            set(pass_files),
            key=lambda f: blocked_by_count.get(f, 0),
            reverse=True,
        )

        result = AnalysisResult()
        result.pass_files = pass_files
        result.functions_by_file = functions_by_file
        result.blocking_dependencies = blocking_dependencies
        result.total_funcs = total_funcs
        result.implementation_order = implementation_order
        result.priority_reasoning = (
            f"Pre-scan (deterministic) found {len(pass_files)} pass-stub files "
            f"with {total_funcs} functions; ordered by inbound dependency count."
        )
        result.repo_context = (
            f"Pre-scanned repository at {pass_data.get('repo', self.repo_dir)}."
        )
        result.raw_analysis = {
            "source": "pre-scan-deterministic",
            "pass_data": pass_data,
            "dep_data": dep_data,
        }
        return result

    def _inject_analysis_summary(self, analysis) -> None:
        """Send a compact analysis summary into the manager's conversation.

        Called after pre-scan succeeds (and the LLM exploration is skipped)
        so the delegation step has the file list + dep graph in context
        without having to re-explore. Keep this tight — every token here
        is paid for at delegation time. Roughly 200–400 tokens for typical
        commit0 repos.
        """
        pass_files = analysis.pass_files or []
        functions_by_file = analysis.functions_by_file or {}
        deps = analysis.blocking_dependencies or {}
        order = analysis.implementation_order or pass_files

        lines = [
            "=== REPOSITORY PRE-SCAN (deterministic) ===",
            f"Repo: {self.repo_dir}",
            f"Pass-stub files: {len(pass_files)}",
            f"Total functions to implement: {analysis.total_funcs}",
            "",
            "Files in topological order (implement first-listed first):",
        ]
        for f in order[:50]:
            funcs = functions_by_file.get(f, [])
            # Dedupe while preserving order so duplicate-name stubs (e.g.
            # multiple `_eval` overrides) collapse cleanly in the summary.
            seen = set()
            funcs_unique = [x for x in funcs if not (x in seen or seen.add(x))]
            funcs_str = ", ".join(funcs_unique) if funcs_unique else "(no functions detected)"
            lines.append(f"  - {f}: {len(funcs)} stubs [{funcs_str}]")
        if len(order) > 50:
            lines.append(f"  ... and {len(order) - 50} more files")

        # Show dependencies only when non-trivial (otherwise it's noise).
        nontrivial_deps = {f: d for f, d in deps.items() if d}
        if nontrivial_deps:
            lines.append("")
            lines.append("Dependencies (file → files it imports from):")
            for f, d in list(nontrivial_deps.items())[:30]:
                lines.append(f"  {f} → {d}")

        lines.append("")
        lines.append(
            "You already have the complete repository analysis above. Do NOT "
            "explore the repo further. Proceed directly to producing the "
            "delegation JSON using these files."
        )

        self.conversation.send_message("\n".join(lines))

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

        try:
            try:
                from orcaid.bridge import discovery_scan_for_orcaid
            except ImportError:
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

        # Step A: run deterministic Python scripts to extract pass stubs + dep graph
        # On success, build analysis_result *directly* from the script output and
        # SKIP the LLM exploration entirely — the deterministic scripts already
        # know everything the LLM was being asked to discover.
        prescan_ok = self._run_pre_scan_scripts()
        prescan_analysis = self._build_analysis_from_prescan() if prescan_ok else None

        if prescan_analysis is not None:
            self.log(
                "[pre-scan] using deterministic analysis — skipping LLM exploration "
                f"({prescan_analysis.total_funcs} functions across "
                f"{len(prescan_analysis.pass_files)} files)"
            )
            self.analysis_result = prescan_analysis
            self.analysis_end_time = datetime.now()
            duration = (self.analysis_end_time - self.analysis_start_time).total_seconds()
            self.analysis_cost = 0.0
            self.analysis_tokens = 0
            iterations = 0
            self.log(f"Analysis (deterministic) completed in {duration:.1f}s — $0.00, 0 tokens")

            # Critical: inject a compact pre-scan summary into the manager's
            # conversation so the *next* step (delegate_tasks) has context to
            # produce a delegation plan against, instead of re-exploring the
            # repo and burning hundreds of thousands of tokens. Without this
            # the legacy delegate_tasks LLM has zero context (we skipped the
            # exploration phase) and either churns indefinitely or falls back
            # to a generic per-file plan.
            self._inject_analysis_summary(prescan_analysis)
        else:
            self.log("Starting analysis (LLM exploration path)...")
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

    def _skill_based_delegate_tasks(self) -> None:
        """
        Step B of the skill-based manager refactor: use SkillRunner for a
        focused single LLM call instead of the monolithic conversation chain.

        Shrinks the delegation step from ~66K tokens to ~5-8K tokens by:
        - Running repo-scan + dep-graph scripts (deterministic, already done)
        - Calling task-decompose skill with compact JSON inputs
        - No accumulated conversation history

        Only used when ORCAID_USE_SKILLS=true env var is set.
        """
        from orcaid.skill_runner import SkillRunner

        self.log("[skill-delegate] Using skill-based delegation (Step B)")
        self.delegation_start_time = datetime.now()

        # Get pre-scanned data from _run_pre_scan_scripts results
        pass_data = getattr(self, "_pre_scan_pass_data", None)
        dep_data = getattr(self, "_pre_scan_dep_data", None)

        if not pass_data or not dep_data:
            self.log("[skill-delegate] No pre-scan data, running scripts now...")
            self._run_pre_scan_scripts()
            pass_data = getattr(self, "_pre_scan_pass_data", None) or {}
            dep_data = getattr(self, "_pre_scan_dep_data", None) or {}

        pass_files = pass_data.get("files", [])
        dep_graph = dep_data.get("graph", {})

        if not pass_files:
            self.log("[skill-delegate] No pass files found, using fallback")
            self.delegation_plan = None
            return

        runner = SkillRunner(llm=self.llm, skills_root=Path(__file__).parent.parent / "skills")
        try:
            result = runner.run(
                "task-decompose",
                inputs={
                    "pass_files": pass_files,
                    "dep_graph": dep_graph,
                    "max_agents": self.config.max_subagents,
                },
            )
        except Exception as e:
            self.log(f"[skill-delegate] SkillRunner failed: {e}, falling back")
            self.delegation_plan = None
            # Surface the partial cost even when the call failed mid-flight,
            # so cost.json reflects spent tokens.
            self.delegation_cost = runner.last_cost
            self.delegation_tokens = runner.last_tokens
            return

        # Roll up SkillRunner cost/tokens into the manager's delegation budget
        # so cost.json's manager.delegation block matches the legacy path's
        # shape regardless of which retry policy is in use.
        self.delegation_cost = runner.last_cost
        self.delegation_tokens = runner.last_tokens

        delegation_json = result
        self.delegation_end_time = datetime.now()

        # Build and save delegation plan (same as old path)
        if not delegation_json:
            self.log("[skill-delegate] No delegation JSON from skill, using fallback")
            delegation_json = fallback_delegation(
                self.analysis_result,
                self.config.max_subagents,
            ) or {"delegation_plan": {}}

        self.delegation_plan = build_delegation_plan(delegation_json)
        output_path = Path(self.config.output_dir) / "delegations.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(delegation_json, f, indent=2)
        self.log(f"Delegation plan saved to: {output_path}")

        # Log completion metrics — now includes cost & tokens for parity with
        # the legacy delegate_tasks() event so the cost.json post-processor
        # doesn't see zeros for skill-based runs.
        duration = (self.delegation_end_time - self.delegation_start_time).total_seconds()
        self.log(
            f"Skill-based delegation complete in {duration:.1f}s "
            f"(cost=${self.delegation_cost:.4f}, tokens={self.delegation_tokens})"
        )

        # Save a delegation_complete event for the output logger
        self.output_logger.log_event(
            event_type="delegation_complete",
            source="manager",
            start_time=self.delegation_start_time,
            end_time=self.delegation_end_time,
            content={
                "num_agents": self.delegation_plan.num_agents if self.delegation_plan else 0,
                "first_round": 0,
                "remaining": 0,
                "reasoning": "skill-based delegation (Step B)",
                "max_iterations": self.config.max_subagents,
                "actual_iterations": 1,
                "duration": duration,
                "cost": self.delegation_cost,
                "tokens": self.delegation_tokens,
            },
        )

    def delegate_tasks(self) -> None:
        """Create a delegation plan and split the work between subagents."""
        # Check env var for Step B skill-based path
        if os.getenv("ORCAID_USE_SKILLS") == "true":
            return self._skill_based_delegate_tasks()

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

        # DEBUG: Log delegation_json structure before saving
        if delegation_json.get("delegation_plan"):
            first_round = delegation_json.get("delegation_plan", {}).get("first_round", {})
            num_tasks = len(first_round.get("tasks", []))
            self.log(f"DEBUG: delegation_json has {num_tasks} tasks in first_round")
        else:
            self.log("DEBUG: delegation_json delegation_plan is empty or missing")

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
