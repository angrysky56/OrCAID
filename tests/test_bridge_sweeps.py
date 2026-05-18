import tempfile
from pathlib import Path
from datetime import datetime, timedelta
import yaml
from orcaid.config import SubAgentResult
from orcaid.bridge import write_drift_log, ORCHESTRATOR_MEMORY_BASE, run_indexer_sweep, write_verified_outcome

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
