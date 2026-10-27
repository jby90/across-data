import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "backtrack_diagnosis",
    Path(__file__).resolve().parents[2] / "scripts/analyze_backtracks.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_empty_verification_after_model_failure_is_not_a_pass():
    tasks = [{"id": "one", "status": "partial_sample", "backtrack_count": 0}]
    traces = [
        {"id": "a", "task_id": "one", "seq_no": 1, "phase": "analysis", "status": "failed"},
        {
            "id": "v",
            "task_id": "one",
            "seq_no": 2,
            "phase": "verification",
            "status": "passed",
            "output_snapshot_json": {"problems": [], "verification_items": []},
        },
    ]
    report = module.summarize(tasks, traces)
    assert report["evaluated_runs"] == 0
    assert report["first_failure_rate_percent"] is None
    assert report["preceding_analysis_failed_runs"] == 1


def test_first_verification_and_reruns_not_reverification_are_counted():
    tasks = [{"id": "one", "status": "completed", "backtrack_count": 1}]
    traces = [
        {
            "id": "v",
            "task_id": "one",
            "seq_no": 1,
            "phase": "verification",
            "status": "backtracked",
            "output_snapshot_json": {
                "problems": [
                    "结论中的数字「180」未出现在条款原文中",
                ]
            },
        },
        {
            "id": "v2",
            "task_id": "one",
            "seq_no": 2,
            "phase": "re_verification",
            "status": "passed",
            "output_snapshot_json": {"problems": []},
        },
    ]
    report = module.summarize(tasks, traces)
    assert report["first_verification_runs"] == 1
    assert report["first_failure_rate_percent"] == 100
    assert report["reason_counts"] == {"numeric_mismatch": 1}
