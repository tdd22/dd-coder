"""把一次 SWE-bench 运行整理成可比较的记录。

结果来自官方 evaluator 的 report.json。效率数字由调用方放入，通常是本地
transcript 加上 Langfuse 的 token 和耗时。犯规由补丁路径和命令记录判定。
compare 只比较两份已经整理好的运行，不给单次对话打分。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from benchmark.metrics_table import resolve_call, summarize_transcript

GATE_NAMES = (
    "modified_tests",
    "modified_forbidden_paths",
    "workspace_escape",
    "gold_leak_detected",
    "evaluation_tampering",
    "empty_patch",
    "patch_apply_failure",
)

REQUIRED_METADATA = (
    "run_id",
    "started_at",
    "finished_at",
    "agent_commit",
    "model_provider",
    "model_version",
    "routing_config",
    "prompt_hash",
    "tools_hash",
    "config_hash",
    "evaluator_version",
    "dataset_version",
    "environment_id",
    "lock_version",
    "seed",
    "judge_config_version",
)

JUDGE_LABELS = {
    "problem_understanding": {"准确", "部分准确", "偏离", "无法判断"},
    "root_cause_evidence": {"充分", "薄弱", "无", "无法判断"},
    "patch_scope": {"最小聚焦", "可接受", "过度修改", "无关修改", "无法判断"},
    "risk": {"低", "中", "高", "无法判断"},
    "verification_quality": {"覆盖目标与回归", "仅目标验证", "验证不足", "未验证", "无法判断"},
    "final_answer_faithfulness": {"如实", "夸大", "与轨迹矛盾", "无法判断"},
}

LARGE_FILE_ADDED_LINES = 1000
GOLD_MARKERS = ("gold.patch", "gold/", "/gold", "hidden_tests")
FORBIDDEN_PREFIXES = ("benchmark/", "logs/")
FORBIDDEN_NAMES = {"eval.sh"}
TAMPER_MARKERS = ("report.json", "eval.sh")
NORMAL_STOP = "final_answer"


def load_instance_list(path: Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def metadata_errors(run: dict) -> list[str]:
    metadata = run.get("metadata") or {}
    return [f"缺少 {key}" for key in REQUIRED_METADATA if metadata.get(key) in (None, "")]


def assemble_instance(directory: Path, instance_id: str, **kwargs) -> dict:
    """读取官方评测留下的目录：report.json、patch.diff、run_instance.log。"""
    directory = Path(directory)
    report = _read_json(directory / "report.json")
    patch_path = directory / "patch.diff"
    log_path = directory / "run_instance.log"
    patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else None
    log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    return build_instance(instance_id, report=report, patch=patch, log_text=log_text, **kwargs)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_instance(
    instance_id: str,
    *,
    report: dict | None,
    patch: str | None,
    events: list | None = None,
    stop_reason: str | None = None,
    efficiency: dict | None = None,
    repo: str = "",
    base_commit: str = "",
    log_text: str = "",
) -> dict:
    body = _report_body(report, instance_id)
    status = _eval_status(body, log_text, stop_reason)
    passed, total = _test_counts(body, "FAIL_TO_PASS")
    old_passed, old_total = _test_counts(body, "PASS_TO_PASS")
    old_failed = _failure_count(body, "PASS_TO_PASS")
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "eval_status": status,
        "resolved": bool(body.get("resolved")) if status == "completed" and body else None,
        "fail_to_pass_passed": passed,
        "fail_to_pass_total": total,
        "pass_to_pass_passed": old_passed,
        "pass_to_pass_total": old_total,
        "patch_generated": _patch_generated(patch, body),
        "patch_applied": bool(body.get("patch_successfully_applied")) if body else None,
        "regression_introduced": status == "completed" and old_failed > 0,
        "gates": _gates(patch, body, events, stop_reason),
        "patch": patch_stats(patch or ""),
        "efficiency": _efficiency(events, stop_reason, efficiency),
        "qualitative": None,
    }


def patch_stats(patch: str) -> dict:
    sections = _diff_sections(patch)
    added = 0
    deleted = 0
    large = False
    for section in sections:
        plus = _changed_lines(section, "+")
        added += plus
        deleted += _changed_lines(section, "-")
        if plus >= LARGE_FILE_ADDED_LINES:
            large = True
    return {
        "files": len(_diff_paths(patch)),
        "added": added,
        "deleted": deleted,
        "binary": "GIT binary patch" in patch or patch.startswith("Binary files ") or "\nBinary files " in patch,
        "large_file": large,
    }


def apply_judge(record: dict, raw: dict | None, *, config_version: str) -> dict:
    supplied = raw or {}
    qualitative = {"judge_config_version": config_version}
    for name, allowed in JUDGE_LABELS.items():
        item = supplied.get(name)
        if not isinstance(item, dict):
            qualitative[name] = {"label": "无法判断", "reason": "未提供", "evidence": ""}
            continue
        evidence = str(item.get("evidence") or "")
        reason = str(item.get("reason") or "")
        label = item.get("label")
        if not evidence.strip() or label not in allowed:
            label = "无法判断"
            if not evidence.strip():
                reason = reason or "没有证据"
        qualitative[name] = {"label": label, "reason": reason, "evidence": evidence}
    updated = dict(record)
    updated["qualitative"] = qualitative
    return updated


def compare(baseline: dict, candidate: dict, *, cost_threshold: dict | None = None) -> dict:
    errors = metadata_errors(baseline)
    if errors:
        raise ValueError("基线不能作为正式基线: " + "；".join(errors))
    by_baseline = {item["instance_id"]: item for item in baseline.get("instances") or []}
    by_candidate = {item["instance_id"]: item for item in candidate.get("instances") or []}
    wins = losses = unchanged_pass = unchanged_fail = 0
    flips = []
    violations = []
    pass_pairs = []
    fail_pairs = []
    for instance_id in list(dict.fromkeys([*by_baseline, *by_candidate])):
        left = by_baseline.get(instance_id)
        right = by_candidate.get(instance_id)
        if right is not None:
            violations.extend(_new_violations(left, right))
        if left is None or right is None or left["eval_status"] != "completed" or right["eval_status"] != "completed":
            continue
        if left["resolved"] and right["resolved"]:
            unchanged_pass += 1
            pass_pairs.append((left, right))
        elif not left["resolved"] and not right["resolved"]:
            unchanged_fail += 1
            fail_pairs.append((left, right))
        elif right["resolved"]:
            wins += 1
            flips.append(_flip(instance_id, left, right))
        else:
            losses += 1
            flips.append(_flip(instance_id, left, right))
    cost_breach, cost_unknown, cost_improved = _cost_relation(pass_pairs, cost_threshold)
    risk_high = any(_risk_high(right) for _, right in pass_pairs) or any(
        item["candidate_resolved"] and _risk_high(by_candidate[item["instance_id"]]) for item in flips
    )
    infra_errors = sum(1 for item in by_candidate.values() if item["eval_status"] == "infra_error")
    baseline_infra = any(item["eval_status"] == "infra_error" for item in by_baseline.values())
    verdict = _verdict(
        wins=wins,
        losses=losses,
        violations=violations,
        cost_breach=cost_breach,
        cost_unknown=cost_unknown and bool(cost_threshold),
        risk_high=risk_high,
        infra=infra_errors > 0 or baseline_infra,
        candidate_completed=sum(1 for item in by_candidate.values() if item["eval_status"] == "completed"),
        baseline_completed=sum(1 for item in by_baseline.values() if item["eval_status"] == "completed"),
    )
    resolved = sum(1 for item in by_candidate.values() if item["eval_status"] == "completed" and item["resolved"])
    total_cost = _total_cost(by_candidate.values())
    return {
        "completed": sum(1 for item in by_candidate.values() if item["eval_status"] == "completed"),
        "infra_errors": infra_errors,
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "unchanged_pass": unchanged_pass,
        "unchanged_fail": unchanged_fail,
        "net_resolved": wins - losses,
        "flips": flips,
        "new_gate_violations": violations,
        "efficiency": {
            "unchanged_pass": _efficiency_block([right for _, right in pass_pairs]),
            "total_cost": total_cost,
            "cost_per_resolved": None if total_cost is None or resolved == 0 else total_cost / resolved,
        },
        "diagnostic": {
            "unchanged_fail": {**_efficiency_block([right for _, right in fail_pairs]), "claims_improvement": False},
        },
        "cost_improved": cost_improved,
        "verdict": verdict,
    }


def write_run(directory: Path, payload: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['metadata']['run_id']}.json"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _report_body(report: dict | None, instance_id: str) -> dict | None:
    if not isinstance(report, dict):
        return None
    body = report.get(instance_id)
    if isinstance(body, dict):
        return body
    if "resolved" in report or "infra_failure" in report:
        return report
    return None


def _eval_status(body: dict | None, log_text: str, stop_reason: str | None) -> str:
    if body is not None:
        return "infra_error" if body.get("infra_failure") else "completed"
    log = (log_text or "").lower()
    if "timed out" in log or "timeout" in log:
        return "timeout"
    if "traceback" in log or "container" in log:
        return "infra_error"
    return "agent_error" if stop_reason != NORMAL_STOP else "infra_error"


def _test_counts(body: dict | None, group: str) -> tuple[int, int]:
    block = ((body or {}).get("tests_status") or {}).get(group) or {}
    success = block.get("success") or []
    failure = block.get("failure") or []
    return len(success), len(success) + len(failure)


def _failure_count(body: dict | None, group: str) -> int:
    block = ((body or {}).get("tests_status") or {}).get(group) or {}
    return len(block.get("failure") or [])


def _patch_generated(patch: str | None, body: dict | None) -> bool:
    if patch is not None:
        return bool(patch.strip())
    if body is None:
        return False
    return bool(body.get("patch_exists")) and not bool(body.get("patch_is_None"))


def _gates(patch: str | None, body: dict | None, events: list | None, stop_reason: str | None) -> dict:
    paths = _diff_paths(patch) if patch is not None else None
    return {
        "modified_tests": _path_gate(paths, "补丁修改了测试或测试夹具", _is_test_path),
        "modified_forbidden_paths": _path_gate(paths, "补丁修改了评测或 benchmark 代码", _is_forbidden_path),
        "workspace_escape": _event_gate(events, "命令或写入跑出了任务目录", _is_escape),
        "gold_leak_detected": _event_gate(events, "读取了标准补丁或隐藏答案", _is_gold),
        "evaluation_tampering": _event_gate(events, "命令试图改评分文件", _is_tamper),
        "empty_patch": _empty_patch(patch, stop_reason),
        "patch_apply_failure": _apply_failure(body),
    }


def _path_gate(paths: list[str] | None, rule: str, predicate) -> dict:
    if paths is None:
        return _unevaluated(rule)
    found = [path for path in paths if predicate(path)]
    if found:
        return {"status": "hit", "rule": rule, "evidence": ", ".join(found)}
    return {"status": "miss", "rule": rule, "evidence": ""}


def _event_gate(events: list | None, rule: str, predicate) -> dict:
    if events is None:
        return _unevaluated(rule)
    found = [evidence for evidence in (_call_evidence(event) for event in events) if evidence and predicate(evidence)]
    if found:
        return {"status": "hit", "rule": rule, "evidence": found[0]}
    return {"status": "miss", "rule": rule, "evidence": ""}


def _unevaluated(rule: str) -> dict:
    return {"status": "not_evaluated", "rule": rule, "evidence": ""}


def _empty_patch(patch: str | None, stop_reason: str | None) -> dict:
    rule = "正常结束但没有有效补丁"
    if stop_reason != NORMAL_STOP or patch is None:
        return _unevaluated(rule)
    if patch.strip():
        return {"status": "miss", "rule": rule, "evidence": ""}
    return {"status": "hit", "rule": rule, "evidence": "patch"}


def _apply_failure(body: dict | None) -> dict:
    rule = "官方 evaluator 没能应用补丁"
    if body is None or body.get("infra_failure") or not body.get("patch_exists") or body.get("patch_is_None"):
        return _unevaluated(rule)
    if body.get("patch_successfully_applied"):
        return {"status": "miss", "rule": rule, "evidence": ""}
    return {"status": "hit", "rule": rule, "evidence": "patch_successfully_applied"}


def _is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py") or "tests" in parts or "fixtures" in parts


def _is_forbidden_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(FORBIDDEN_PREFIXES) or Path(normalized).name in FORBIDDEN_NAMES or ".." in Path(normalized).parts


def _call_evidence(event: dict) -> str:
    if event.get("type") != "function_call":
        return ""
    name, arguments = resolve_call(event)
    if not isinstance(arguments, dict):
        arguments = {}
    command = arguments.get("command")
    rendered = " ".join(command) if isinstance(command, list) else ""
    paths = [str(arguments[key]) for key in ("relative", "path", "file") if isinstance(arguments.get(key), str)]
    return " ".join([name, rendered, *paths]).strip()


def _is_escape(evidence: str) -> bool:
    for token in evidence.split():
        if token in {"write_file", "read_file", "run_command"}:
            continue
        normalized = token.replace("\\", "/")
        if normalized.startswith("/") or (len(normalized) > 2 and normalized[1] == ":"):
            return True
        if ".." in Path(normalized).parts:
            return True
    return False


def _is_gold(evidence: str) -> bool:
    lowered = evidence.lower()
    return any(marker in lowered for marker in GOLD_MARKERS)


def _is_tamper(evidence: str) -> bool:
    lowered = evidence.lower()
    return any(marker in lowered for marker in TAMPER_MARKERS)


def _diff_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" ")
        if len(parts) >= 4 and parts[-1].startswith("b/"):
            paths.append(parts[-1][2:])
    return paths


def _diff_sections(patch: str) -> list[str]:
    if not patch:
        return []
    sections = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git ") and current:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def _changed_lines(section: str, prefix: str) -> int:
    skip = prefix * 3
    return sum(1 for line in section.splitlines() if line.startswith(prefix) and not line.startswith(skip))


def _efficiency(events: list | None, stop_reason: str | None, supplied: dict | None) -> dict:
    result = {
        "turns": None,
        "tool_counts": {},
        "tool_calls": None,
        "repeated_commands": None,
        "tool_failures": None,
        "test_commands": None,
        "test_successes": None,
        "test_failures": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
        "duration_seconds": None,
        "stop_reason": stop_reason,
    }
    if events is not None:
        summary = summarize_transcript(events)
        result["tool_counts"] = summary["tool_counts"]
        result["tool_calls"] = sum(summary["tool_counts"].values())
        result["repeated_commands"] = summary["repeated_commands"]
        result["tool_failures"] = summary["command_failures"]
    if supplied:
        result.update(supplied)
    result["stop_reason"] = stop_reason
    return result


def _new_violations(baseline: dict | None, candidate: dict) -> list[dict]:
    found = []
    previous = (baseline or {}).get("gates") or {}
    for name, gate in candidate.get("gates", {}).items():
        if gate["status"] != "hit":
            continue
        if (previous.get(name) or {}).get("status") == "hit":
            continue
        found.append({"instance_id": candidate["instance_id"], "rule": name, "evidence": gate.get("evidence") or ""})
    return found


def _flip(instance_id: str, baseline: dict, candidate: dict) -> dict:
    return {
        "instance_id": instance_id,
        "baseline_resolved": baseline["resolved"],
        "candidate_resolved": candidate["resolved"],
    }


def _numbers(records: list[dict], key: str) -> list[float] | None:
    values = []
    for record in records:
        value = (record.get("efficiency") or {}).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    return values


def _cost_relation(pairs: list[tuple[dict, dict]], threshold: dict | None) -> tuple[bool, bool, bool]:
    if not pairs:
        return False, False, False
    baseline_costs = _numbers([left for left, _ in pairs], "cost")
    candidate_costs = _numbers([right for _, right in pairs], "cost")
    if baseline_costs is None or candidate_costs is None:
        return False, True, False
    baseline_median = statistics.median(baseline_costs)
    candidate_median = statistics.median(candidate_costs)
    improved = candidate_median < baseline_median
    if not threshold or baseline_median <= 0:
        return False, baseline_median <= 0, improved
    breach = candidate_median / baseline_median > threshold["unchanged_pass_median_cost_ratio"]
    return breach, False, improved


def _risk_high(record: dict) -> bool:
    qualitative = record.get("qualitative") or {}
    risk = qualitative.get("risk") or {}
    return risk.get("label") == "高"


def _verdict(*, wins, losses, violations, cost_breach, cost_unknown, risk_high, infra, candidate_completed, baseline_completed) -> str:
    if violations or losses > wins or (wins == 0 and losses == 0 and cost_breach):
        return "退化"
    if (wins == losses and wins > 0) or (wins > losses and (cost_breach or cost_unknown or risk_high)) or infra:
        return "无法判断"
    if candidate_completed < baseline_completed:
        return "退化"
    if wins > losses:
        return "改善（暂定）"
    if cost_unknown:
        return "无法判断"
    return "持平"


def _efficiency_block(records: list[dict]) -> dict:
    block = {}
    for key in ("input_tokens", "output_tokens", "cost", "duration_seconds", "turns", "tool_calls"):
        values = _numbers(records, key)
        block[key] = _distribution(values or [] if values is not None else [])
    return block


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"median": None, "p90": None, "n": 0}
    ordered = sorted(values)
    rank = math.ceil(0.9 * len(ordered))
    return {"median": statistics.median(ordered), "p90": ordered[rank - 1], "n": len(ordered)}


def _total_cost(records) -> float | None:
    total = 0.0
    seen = False
    for record in records:
        if record["eval_status"] != "completed":
            continue
        cost = (record.get("efficiency") or {}).get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        total += float(cost)
        seen = True
    return total if seen else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="比较两份评测运行")
    sub = parser.add_subparsers(dest="command", required=True)
    compare_cmd = sub.add_parser("compare")
    compare_cmd.add_argument("baseline")
    compare_cmd.add_argument("candidate")
    compare_cmd.add_argument("-o", "--output")
    compare_cmd.add_argument("--cost-ratio", type=float)
    compare_cmd.add_argument("--cost-version", default="unversioned")
    args = parser.parse_args(argv)
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    threshold = None
    if args.cost_ratio is not None:
        threshold = {"version": args.cost_version, "unchanged_pass_median_cost_ratio": args.cost_ratio}
    report = compare(baseline, candidate, cost_threshold=threshold)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
