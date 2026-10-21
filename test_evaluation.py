import json
from pathlib import Path

import pytest

from benchmark.evaluation import (
    apply_judge,
    assemble_instance,
    build_instance,
    compare,
    load_instance_list,
    metadata_errors,
    patch_stats,
    write_run,
)


SOURCE = """diff --git a/pkg/app.py b/pkg/app.py
--- a/pkg/app.py
+++ b/pkg/app.py
@@ -1 +1,2 @@
-old
+old
+new
"""

TESTS = """diff --git a/pkg/tests/test_app.py b/pkg/tests/test_app.py
--- a/pkg/tests/test_app.py
+++ b/pkg/tests/test_app.py
@@ -1 +1,2 @@
+def test_added():
+    assert True
"""

HARNESS = """diff --git a/benchmark/run_swebench_lite.py b/benchmark/run_swebench_lite.py
--- a/benchmark/run_swebench_lite.py
+++ b/benchmark/run_swebench_lite.py
@@ -1 +1 @@
-old
+new
"""


def official(instance, *, resolved=True, applied=True, patch_exists=True, infra=False, p2p_fail=()):
    failed = list(p2p_fail)
    return {
        instance: {
            "patch_is_None": not patch_exists,
            "patch_exists": patch_exists,
            "patch_successfully_applied": applied,
            "resolved": resolved,
            "infra_failure": infra,
            "tests_status": {
                "FAIL_TO_PASS": {
                    "success": ["test_target"] if resolved else [],
                    "failure": [] if resolved else ["test_target"],
                },
                "PASS_TO_PASS": {"success": [] if failed else ["test_old"], "failure": failed},
            },
        }
    }


def meta():
    return {
        "run_id": "run-1",
        "started_at": "2026-10-21T00:00:00Z",
        "finished_at": "2026-10-21T01:00:00Z",
        "agent_commit": "abc123",
        "model_provider": "deepseek",
        "model_version": "flash",
        "routing_config": "default",
        "prompt_hash": "p",
        "tools_hash": "t",
        "config_hash": "c",
        "evaluator_version": "swebench",
        "dataset_version": "lite-test",
        "environment_id": "local",
        "lock_version": "uv.lock",
        "seed": "0",
        "judge_config_version": "judge-v1",
    }


def instance(instance_id, *, resolved=True, patch=SOURCE, events=None, stop="final_answer", cost=1.0, duration=10.0, report=None, eval_status=None):
    record = build_instance(
        instance_id,
        report=official(instance_id, resolved=resolved) if report is None else report,
        patch=patch,
        events=[] if events is None else events,
        stop_reason=stop,
        efficiency={"cost": cost, "duration_seconds": duration, "input_tokens": 100, "output_tokens": 20, "turns": 4},
        repo="example/repo",
        base_commit="base",
    )
    if eval_status is not None:
        record["eval_status"] = eval_status
        if eval_status != "completed":
            record["resolved"] = None
    return record


def run(records, **overrides):
    payload = meta()
    payload.update(overrides)
    return {"metadata": payload, "instances": records}


def test_report_maps_resolved_counts_and_regression():
    record = build_instance(
        "sympy__sympy-20590",
        report=official("sympy__sympy-20590", p2p_fail=["test_old"]),
        patch=SOURCE,
        events=[],
        stop_reason="final_answer",
        repo="sympy/sympy",
        base_commit="abc",
    )
    assert record["eval_status"] == "completed"
    assert record["resolved"] is True
    assert record["fail_to_pass_passed"] == 1
    assert record["fail_to_pass_total"] == 1
    assert record["pass_to_pass_passed"] == 0
    assert record["pass_to_pass_total"] == 1
    assert record["patch_generated"] is True
    assert record["patch_applied"] is True
    assert record["regression_introduced"] is True


def test_infra_failure_is_not_an_agent_miss():
    record = build_instance(
        "django__django-10914",
        report=official("django__django-10914", resolved=False, infra=True),
        patch=SOURCE,
        events=[],
        stop_reason="final_answer",
        repo="django/django",
        base_commit="abc",
    )
    assert record["eval_status"] == "infra_error"
    assert record["resolved"] is None


def test_missing_report_with_evaluator_traceback_is_infra_error():
    record = build_instance(
        "django__django-10914",
        report=None,
        patch=SOURCE,
        log_text="Traceback (most recent call last):\nRuntimeError: container died",
        events=[],
        stop_reason="final_answer",
        repo="django/django",
        base_commit="abc",
    )
    assert record["eval_status"] == "infra_error"
    assert record["resolved"] is None


def test_agent_crash_without_report_is_agent_error():
    record = build_instance(
        "django__django-10914",
        report=None,
        patch=None,
        log_text="",
        events=None,
        stop_reason="max_turns",
        repo="django/django",
        base_commit="abc",
    )
    assert record["eval_status"] == "agent_error"


def test_log_timeout_is_timeout():
    record = build_instance(
        "django__django-10914",
        report=None,
        patch=None,
        log_text="Evaluation timed out after 1800s",
        events=[],
        stop_reason="final_answer",
        repo="django/django",
        base_commit="abc",
    )
    assert record["eval_status"] == "timeout"


def test_gate_not_run_is_not_a_pass():
    record = build_instance(
        "django__django-10914",
        report=None,
        patch=None,
        events=None,
        stop_reason=None,
        repo="django/django",
        base_commit="abc",
    )
    assert record["gates"]["modified_tests"]["status"] == "not_evaluated"
    assert record["gates"]["workspace_escape"]["status"] == "not_evaluated"
    assert record["gates"]["gold_leak_detected"]["status"] == "not_evaluated"
    assert all(gate["status"] != "miss" for gate in record["gates"].values())


def test_modified_tests_and_forbidden_paths_use_patch_paths():
    edited = build_instance(
        "id",
        report=official("id"),
        patch=TESTS,
        events=[],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert edited["gates"]["modified_tests"]["status"] == "hit"
    assert "pkg/tests/test_app.py" in edited["gates"]["modified_tests"]["evidence"]
    clean = build_instance(
        "id",
        report=official("id"),
        patch=SOURCE,
        events=[],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert clean["gates"]["modified_tests"]["status"] == "miss"
    harness = build_instance(
        "id",
        report=official("id"),
        patch=HARNESS,
        events=[],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert harness["gates"]["modified_forbidden_paths"]["status"] == "hit"


def test_patch_stats_count_files_lines_binary_and_large_files():
    stats = patch_stats(SOURCE)
    assert stats["files"] == 1
    assert stats["added"] == 2
    assert stats["deleted"] == 1
    assert stats["binary"] is False
    binary = patch_stats("diff --git a/a.bin b/a.bin\nGIT binary patch\nliteral 4\n")
    assert binary["binary"] is True
    large = "diff --git a/pkg/big.py b/pkg/big.py\n" + "\n".join("+" + str(i) for i in range(1000))
    assert patch_stats(large)["large_file"] is True


def test_command_gates_read_the_transcript():
    leaked = build_instance(
        "id",
        report=official("id"),
        patch=SOURCE,
        events=[{"type": "function_call", "name": "run_command", "callId": "1", "arguments": {"command": ["cat", "gold.patch"]}}],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert leaked["gates"]["gold_leak_detected"]["status"] == "hit"
    escaped = build_instance(
        "id",
        report=official("id"),
        patch=SOURCE,
        events=[{"type": "function_call", "name": "write_file", "callId": "1", "arguments": {"relative": "../secret.txt", "content": "x"}}],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert escaped["gates"]["workspace_escape"]["status"] == "hit"
    tampered = build_instance(
        "id",
        report=official("id"),
        patch=SOURCE,
        events=[{"type": "function_call", "name": "run_command", "callId": "1", "arguments": {"command": ["rm", "report.json"]}}],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert tampered["gates"]["evaluation_tampering"]["status"] == "hit"


def test_empty_patch_only_when_the_agent_finished_normally():
    empty = build_instance(
        "id",
        report=official("id", resolved=False, patch_exists=False, applied=False),
        patch="",
        events=[],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert empty["gates"]["empty_patch"]["status"] == "hit"
    assert empty["patch_generated"] is False
    stopped = build_instance(
        "id",
        report=None,
        patch="",
        events=[],
        stop_reason="max_turns",
        repo="example/repo",
        base_commit="abc",
    )
    assert stopped["gates"]["empty_patch"]["status"] == "not_evaluated"


def test_patch_apply_failure_comes_from_the_official_report():
    record = build_instance(
        "id",
        report=official("id", resolved=False, applied=False),
        patch=SOURCE,
        events=[],
        stop_reason="final_answer",
        repo="example/repo",
        base_commit="abc",
    )
    assert record["gates"]["patch_apply_failure"]["status"] == "hit"
    assert record["patch_applied"] is False


def test_missing_metadata_cannot_be_an_official_baseline():
    payload = meta()
    del payload["agent_commit"]
    errors = metadata_errors({"metadata": payload, "instances": []})
    assert any("agent_commit" in error for error in errors)


def test_first50_list_is_the_published_test_order():
    ids = load_instance_list(Path(__file__).resolve().parent / "benchmark" / "swebench_lite_first50.txt")
    assert len(ids) == 50
    assert ids[0] == "astropy__astropy-12907"
    assert ids[-1] == "django__django-13220"


def test_win_is_provisional_improvement():
    report = compare(
        run([instance("a", resolved=False)]),
        run([instance("a", resolved=True)], run_id="run-2"),
    )
    assert report["wins"] == 1
    assert report["losses"] == 0
    assert report["verdict"] == "改善（暂定）"


def test_loss_is_regression():
    report = compare(
        run([instance("a", resolved=True)]),
        run([instance("a", resolved=False)], run_id="run-2"),
    )
    assert report["losses"] == 1
    assert report["verdict"] == "退化"


def test_same_results_with_cost_over_the_preset_line_regresses():
    report = compare(
        run([instance("a", resolved=True, cost=1.0)]),
        run([instance("a", resolved=True, cost=2.0)], run_id="run-2"),
        cost_threshold={"version": "cost-v1", "unchanged_pass_median_cost_ratio": 1.5},
    )
    assert report["wins"] == 0
    assert report["losses"] == 0
    assert report["verdict"] == "退化"


def test_evaluator_crash_is_not_a_comparable_loss():
    crashed = instance("a", eval_status="infra_error")
    report = compare(run([instance("a", resolved=True)]), run([crashed], run_id="run-2"))
    assert report["losses"] == 0
    assert report["wins"] == 0
    assert report["infra_errors"] == 1
    assert report["verdict"] == "无法判断"


def test_test_edit_cannot_be_offset_by_a_pass_or_a_lower_cost():
    cheated = instance("a", resolved=True, patch=TESTS, cost=0.01)
    report = compare(
        run([instance("a", resolved=False, cost=5.0)]),
        run([cheated], run_id="run-2"),
    )
    assert cheated["resolved"] is True
    assert report["verdict"] == "退化"
    assert report["new_gate_violations"][0]["rule"] == "modified_tests"


def test_judge_without_evidence_is_undecided_and_cannot_change_resolved():
    record = instance("a", resolved=False)
    judged = apply_judge(
        record,
        {
            "problem_understanding": {"label": "准确", "reason": "像是读懂了", "evidence": ""},
            "resolved": True,
        },
        config_version="judge-v1",
    )
    assert judged["resolved"] is False
    assert judged["qualitative"]["problem_understanding"]["label"] == "无法判断"


def test_equal_flips_are_undecided():
    report = compare(
        run([instance("a", resolved=False), instance("b", resolved=True)]),
        run([instance("a", resolved=True), instance("b", resolved=False)], run_id="run-2"),
    )
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["verdict"] == "无法判断"
    assert {item["instance_id"] for item in report["flips"]} == {"a", "b"}


def test_efficiency_ignores_failures_and_flips():
    baseline = run([
        instance("keep", resolved=True, cost=10, duration=100),
        instance("flip", resolved=True, cost=10, duration=100),
        instance("fail", resolved=False, cost=1, duration=1),
    ])
    candidate = run([
        instance("keep", resolved=True, cost=8, duration=80),
        instance("flip", resolved=False, cost=0.1, duration=1),
        instance("fail", resolved=False, cost=0.1, duration=1),
    ], run_id="run-2")
    report = compare(baseline, candidate)
    passed = report["efficiency"]["unchanged_pass"]["duration_seconds"]
    assert passed["median"] == 80
    assert report["diagnostic"]["unchanged_fail"]["claims_improvement"] is False
    assert report["verdict"] == "退化"


def test_flat_result_stays_flat_when_cost_improves():
    report = compare(
        run([instance("a", resolved=True, cost=4)]),
        run([instance("a", resolved=True, cost=2)], run_id="run-2"),
        cost_threshold={"version": "cost-v1", "unchanged_pass_median_cost_ratio": 1.5},
    )
    assert report["verdict"] == "持平"
    assert report["cost_improved"] is True


def test_assemble_instance_reads_the_official_directory(tmp_path):
    instance_dir = tmp_path / "django__django-10914"
    instance_dir.mkdir()
    (instance_dir / "report.json").write_text(
        json.dumps(official("django__django-10914")),
        encoding="utf-8",
    )
    (instance_dir / "patch.diff").write_text(SOURCE, encoding="utf-8")
    record = assemble_instance(instance_dir, "django__django-10914", repo="django/django", base_commit="abc", events=[], stop_reason="final_answer")
    assert record["resolved"] is True
    assert record["patch_generated"] is True
    assert record["gates"]["modified_tests"]["status"] == "miss"


def test_write_run_does_not_overwrite(tmp_path):
    payload = run([instance("a")])
    first = write_run(tmp_path, payload)
    with pytest.raises(FileExistsError):
        write_run(tmp_path, payload)
    assert json.loads(first.read_text(encoding="utf-8"))["metadata"]["run_id"] == "run-1"
