import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import yaml
from orcaid.config import SubAgentResult, SubAgent
from orcaid.bridge import write_drift_log, ORCHESTRATOR_MEMORY_BASE, run_indexer_sweep, write_verified_outcome
from orcaid.core.manager_assignment import AssignmentMixin

def test_write_drift_log_formatting():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        subagent_result = SubAgentResult(
            engineer_id="engineer_2",
            task_id="task_123",
            success=False,
            error="AssertionError: Test failed",
            duration_seconds=12.5,
            cost=0.045,
            actual_iterations=3,
            max_iterations=5,
            files_modified=["orcaid/core/manager.py"],
            git_diff="--- a/orcaid/core/manager.py\n+++ b/orcaid/core/manager.py\n@@ -1,1 +1,2 @@\n-old\n+new"
        )
        
        drift_log = [
            {
                "criterion_id": "commit_made",
                "failure_message": "No commit was made by the subagent",
                "category": "phase_skip",
                "severity": "high"
            }
        ]
        
        correction_context = {
            "task_type": "commit0",
            "task_id": "task_123",
            "attempt_number": 1,
            "original_requirements": "Implement function X",
            "instructions": "Fix commit_made failure"
        }
        
        log_path = write_drift_log(
            drift_log=drift_log,
            correction_context=correction_context,
            subagent_result=subagent_result,
            memory_base=tmp_path
        )
        
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        
        # Verify markdown elements and tables are present
        assert "| Metric | Value |" in content
        assert "engineer_2" in content
        assert "AssertionError: Test failed" in content
        assert "<details>" in content
        assert "</details>" in content
        assert "--- a/orcaid/core/manager.py" in content


def test_run_indexer_sweep():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # 1. Write a verified outcome (1 hour older)
        verified_data = {
            "task_type": "commit0",
            "task_id": "task_1",
            "timestamp": (datetime.now() - timedelta(hours=1)).isoformat(),
            "worker_profile": "engineer_1",
            "output_summary": "Successfully verified task 1",
            "duration": 10.0,
            "files_modified": ["orcaid/cli.py"],
            "commit_hash": "abc1234",
            "cost": 0.012,
            "merged": True,
            "merge_method": "rebase"
        }
        write_verified_outcome(verified_data, memory_base=tmp_path)
        
        # 2. Write a drift log (now)
        subagent_result = SubAgentResult(
            engineer_id="engineer_2",
            task_id="task_2",
            success=False,
            error="AssertionError: Test failed",
            duration_seconds=12.5,
            cost=0.045,
            actual_iterations=3,
            max_iterations=5,
            files_modified=["orcaid/core/manager.py"],
            git_diff="--- a/orcaid/core/manager.py"
        )
        drift_log = [
            {
                "criterion_id": "commit_made",
                "failure_message": "No commit was made by the subagent",
                "category": "phase_skip",
                "severity": "high"
            }
        ]
        correction_context = {
            "task_type": "commit0",
            "task_id": "task_2",
            "attempt_number": 1,
            "original_requirements": "Implement function X",
            "instructions": "Fix commit_made failure"
        }
        write_drift_log(
            drift_log=drift_log,
            correction_context=correction_context,
            subagent_result=subagent_result,
            memory_base=tmp_path
        )
        
        # 3. Execute the indexer sweep
        run_indexer_sweep(memory_base=tmp_path)
        
        # 4. Verify discovery.yaml exists and contains correct aggregates
        discovery_path = tmp_path / "index" / "discovery.yaml"
        assert discovery_path.exists()
        
        with open(discovery_path, "r", encoding="utf-8") as f:
            index = yaml.safe_load(f)
            
        assert "task_types" in index
        assert "commit0" in index["task_types"]
        
        task_stats = index["task_types"]["commit0"]
        assert task_stats["total_completed"] == 1
        assert task_stats["total_failed"] == 1
        assert task_stats["drift_rate"] == 0.5
        assert task_stats["last_outcome"] == "failed"
        
        assert "profiles" in index
        assert "engineer_1" in index["profiles"]
        assert index["profiles"]["engineer_1"]["total_completed"] == 1
        assert index["profiles"]["engineer_1"]["drift_rate"] == 0.0
        
        assert "engineer_2" in index["profiles"]
        assert index["profiles"]["engineer_2"]["total_failed"] == 1
        assert index["profiles"]["engineer_2"]["drift_rate"] == 1.0


class MockManager(AssignmentMixin):
    def __init__(self):
        super().__init__()
        self.conversation = MagicMock()
        self.prompts = {"assign_task": "Assign task prompt. Completed: {completed_task_summary}"}
        self.config = MagicMock()
        self.config.max_rounds_chat = 5
        self.config.manager_max_iterations = 3
        self.task = MagicMock()
        self.task.build_completed_task_summary.return_value = "Summary of completed task"
        self.task.extract_assignments.return_value = []
        self.task.get_assign_context.return_value = {}
        self.task.get_assigned_targets.return_value = []
        self.task.get_work_dir.return_value = "work_dir"
        self.task.get_assign_event_extras.return_value = {}
        self.delegation_plan = MagicMock()
        self.delegation_plan.remaining_tasks = []
        self.current_round = 1
        self.workspace = MagicMock()
        self.repo_dir = "/mock/repo"
        self.output_logger = MagicMock()
        
    def log(self, msg):
        print(f"[MockManager] {msg}")

    def save_events(self, name, event_count_before):
        return None


def test_assign_task_prompt_injection():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # 1. Write mock history YAML to index/discovery.yaml
        index_dir = tmp_path / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        discovery_yaml = index_dir / "discovery.yaml"
        
        mock_discovery = {
            "task_types": {
                "commit0": {
                    "total_completed": 10,
                    "total_failed": 2,
                    "drift_rate": 0.1667
                }
            },
            "profiles": {
                "engineer_2": {
                    "total_completed": 8,
                    "total_failed": 0,
                    "drift_rate": 0.0
                }
            }
        }
        with open(discovery_yaml, "w", encoding="utf-8") as f:
            yaml.dump(mock_discovery, f)
            
        # 2. Instantiate MockManager
        manager = MockManager()
        
        # Mock completed result
        completed_result = MagicMock()
        completed_result.engineer_id = "engineer_2"
        completed_result.round_num = 1
        completed_result.merged = True
        completed_result.success = True
        completed_result.error = None
        completed_result.task_id = "task_1"
        
        # Patch ORCHESTRATOR_MEMORY_BASE inside manager_assignment
        import orcaid.core.manager_assignment as ma
        original_base = ma.ORCHESTRATOR_MEMORY_BASE
        ma.ORCHESTRATOR_MEMORY_BASE = tmp_path
        
        try:
            # Mock the utils
            ma.extract_conversation_metrics = MagicMock(return_value={"cost": 0.0, "total_tokens": 0})
            ma.extract_json_from_events = MagicMock(return_value={"assign_task": {"assignments": [], "reasoning": "mocked"}})
            ma.count_llm_iterations = MagicMock(return_value=0)
            
            manager.assign_task(
                completed_result=completed_result,
                all_completed=[],
                running_agents=[],
                idle_agents=[],
                inactive_agents=[],
                finished_agents=[]
            )
            
            # 3. Assert on the sent prompt
            manager.conversation.send_message.assert_called_once()
            sent_prompt = manager.conversation.send_message.call_args[0][0]
            
            assert "Historical Subagent Drift and Performance Context" in sent_prompt
            assert "commit0: completed=10" in sent_prompt
            assert "engineer_2: completed=8" in sent_prompt
            
        finally:
            ma.ORCHESTRATOR_MEMORY_BASE = original_base
