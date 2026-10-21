import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchmark.metrics_table import (
    build_row,
    discover_runs,
    render_tool_counts,
    summarize_observations,
    summarize_transcript,
    write_run_meta,
    write_table,
)


def _call(name, arguments, call_id="c1"):
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "callId": call_id,
    }


def _result(call_id, content):
    return {
        "type": "function_call_result",
        "callId": call_id,
        "output": {"content": content},
    }


class TranscriptSummaryTest(unittest.TestCase):
    def test_defer_execute_counts_inner_tool(self):
        events = [
            _call(
                "defer_execute",
                {"name": "write_file", "arguments": {"relative": "a.py", "content": "x"}},
                "w",
            ),
            _result("w", "已写入"),
            _call("list_dir", {"relative": "."}, "l"),
            _result("l", "a.py"),
        ]
        summary = summarize_transcript(events)
        self.assertEqual(summary["tool_counts"], {"write_file": 1, "list_dir": 1})
        self.assertEqual(render_tool_counts(summary["tool_counts"]), "list_dir=1, write_file=1")

    def test_repeated_commands_count_extra_runs(self):
        command = ["python", "-c", "print(1)"]
        events = []
        for index in range(4):
            events.append(_call("run_command", {"command": command}, f"a{index}"))
            events.append(_result(f"a{index}", "exit_code: 0\n1"))
        events.append(_call("run_command", {"command": ["python", "-c", "print(2)"]}, "b"))
        events.append(_result("b", "exit_code: 0\n2"))
        summary = summarize_transcript(events)
        self.assertEqual(summary["repeated_commands"], 3)
        self.assertEqual(summary["command_failures"], 0)

    def test_command_failure_and_externalized_result(self):
        events = [
            _call(
                "defer_execute",
                {"name": "run_command", "arguments": {"command": ["python", "missing.py"]}},
                "bad",
            ),
            _result("bad", "工具结果过大，已写入磁盘: data/tool-results/x.txt\nexit_code: 1\n"),
            _call("run_command", {"command": ["python", "-c", "print(1)"]}, "good"),
            _result("good", "exit_code: 0\n1"),
            _call("run_command", {"command": ["missing"]}, "missing"),
            _result("missing", "工具执行失败: 找不到命令: missing"),
            _call("read_file", {"relative": "a.py"}, "read"),
            _result("read", "工具执行失败: 读取失败"),
        ]
        summary = summarize_transcript(events)
        self.assertEqual(summary["command_failures"], 2)
        self.assertEqual(summary["externalized"], 1)
        self.assertEqual(summary["tool_counts"]["run_command"], 3)


class ObservationSummaryTest(unittest.TestCase):
    def test_tokens_include_lite_calls_and_do_not_double_count_aliases(self):
        observations = [
            {
                "type": "SPAN",
                "name": "run_chat",
                "latency": 12.24,
                "usage_details": {"input": 999},
            },
            {
                "type": "GENERATION",
                "name": "agent",
                "usage_details": {"input": 10, "prompt_tokens": 100, "output": 4},
            },
            {
                "type": "GENERATION",
                "name": "complete_text",
                "usage_details": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        ]
        summary = summarize_observations(observations)
        self.assertEqual(summary["input_tokens"], 15)
        self.assertEqual(summary["output_tokens"], 5)
        self.assertEqual(summary["duration_seconds"], 12.2)
        self.assertEqual(summary["agent_calls"], 1)

    def test_duration_falls_back_to_start_and_end(self):
        start = datetime(2026, 10, 18, tzinfo=timezone.utc)
        observations = [
            {
                "type": "SPAN",
                "name": "run_chat",
                "latency": None,
                "start_time": start,
                "end_time": start + timedelta(seconds=3.5),
            }
        ]
        summary = summarize_observations(observations)
        self.assertEqual(summary["duration_seconds"], 3.5)
        self.assertIsNone(summary["input_tokens"])

    def test_empty_observations_leave_usage_blank(self):
        summary = summarize_observations([])
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["duration_seconds"])
        self.assertIsNone(summary["agent_calls"])


class TableTest(unittest.TestCase):
    def test_row_uses_meta_turns_and_blanks_missing_usage(self):
        row = build_row(
            {"thread_id": "swebench-demo", "stop_reason": "max_turns", "turns": 30},
            [_call("list_dir", {"relative": "."}, "l"), _result("l", "a.py")],
            [],
        )
        self.assertEqual(row["停止原因"], "max_turns")
        self.assertEqual(row["代理轮数"], 30)
        self.assertEqual(row["工具次数"], "list_dir=1")
        self.assertEqual(row["输入token"], "")
        self.assertEqual(row["耗时"], "")

    def test_missing_turns_use_agent_observations(self):
        row = build_row(
            {"thread_id": "swebench-demo"},
            [],
            [
                {"type": "GENERATION", "name": "agent", "usage_details": {"input": 1, "output": 1}},
                {"type": "GENERATION", "name": "agent", "usage_details": {"input": 2, "output": 2}},
            ],
        )
        self.assertEqual(row["代理轮数"], 2)
        self.assertEqual(row["输入token"], 3)

    def test_discover_runs_prefers_meta_and_keeps_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            transcripts = root / "transcripts"
            write_run_meta(
                outputs / "runs" / "swebench-new",
                {"thread_id": "swebench-new", "stop_reason": "final_answer", "turns": 4},
            )
            legacy = outputs / "old.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(
                json.dumps({"thread_id": "swebench-old", "stop_reason": "max_turns", "turns": 8}),
                encoding="utf-8",
            )
            transcripts.mkdir()
            (transcripts / "swebench-old.jsonl").write_text("{}\n", encoding="utf-8")
            (transcripts / "swebench-only.jsonl").write_text("{}\n", encoding="utf-8")
            (transcripts / "chat.jsonl").write_text("{}\n", encoding="utf-8")
            runs = discover_runs(outputs, transcripts)
            by_id = {item["thread_id"]: item for item in runs}
            self.assertEqual(set(by_id), {"swebench-new", "swebench-old", "swebench-only"})
            self.assertEqual(by_id["swebench-new"]["turns"], 4)
            self.assertEqual(by_id["swebench-old"]["stop_reason"], "max_turns")

    def test_write_table_quotes_tool_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.csv"
            write_table(
                path,
                [
                    {
                        "thread_id": "swebench-demo",
                        "停止原因": "max_turns",
                        "代理轮数": 2,
                        "工具次数": "read_file=1, run_command=2",
                        "重复命令": 1,
                        "大结果外置": 0,
                        "命令失败": 0,
                        "输入token": 15,
                        "输出token": 5,
                        "耗时": 12.2,
                    }
                ],
            )
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("停止原因,代理轮数,工具次数", text)
            self.assertIn('"read_file=1, run_command=2"', text)


if __name__ == "__main__":
    unittest.main()
