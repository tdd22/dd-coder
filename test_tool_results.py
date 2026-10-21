import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import tool_results
from agent.permission import PermissionAction, ToolRequest, decide
from agent.tool import REGISTRY
from agent.tool_results import (
    EXTERNALIZE_BYTES,
    HEAD_CHARS,
    RUN_COMMAND_EXTERNALIZE_CHARS,
    TAIL_CHARS,
    make_pointer,
    read_artifact,
    should_externalize,
    write_artifact,
)


class ToolResultsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "tool-results"
        patcher = patch.object(tool_results, "TOOL_RESULTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class ThresholdTest(ToolResultsCase):
    def test_run_command_threshold_counts_chars(self):
        self.assertFalse(should_externalize("run_command", "x" * RUN_COMMAND_EXTERNALIZE_CHARS))
        self.assertTrue(should_externalize("run_command", "x" * (RUN_COMMAND_EXTERNALIZE_CHARS + 1)))

    def test_other_tools_threshold_counts_utf8_bytes(self):
        self.assertFalse(should_externalize("read_file", "x" * EXTERNALIZE_BYTES))
        self.assertTrue(should_externalize("read_file", "x" * (EXTERNALIZE_BYTES + 1)))
        # 20000 个中文字符不到 30000 个字符，但 60000 字节已经超过 50KB。
        self.assertTrue(should_externalize("read_file", "字" * 20000))

    def test_small_results_stay_in_context(self):
        self.assertFalse(should_externalize("list_dir", "a.py\nb.py"))
        self.assertFalse(should_externalize("read_artifact", "x" * 20000))


class WriteArtifactTest(ToolResultsCase):
    def test_writes_numbered_files_with_full_content(self):
        first = write_artifact("sess1", "甲" * 100)
        second = write_artifact("sess1", "乙" * 100)
        self.assertEqual(first.name, "tool_result_001.txt")
        self.assertEqual(second.name, "tool_result_002.txt")
        self.assertEqual(first.read_text(encoding="utf-8"), "甲" * 100)
        self.assertEqual(second.parent, self.root / "sess1")

    def test_never_overwrites_after_a_restart(self):
        # 模拟服务重启后计数器回到 0：001 已存在时必须跳过。
        folder = self.root / "sess1"
        folder.mkdir(parents=True)
        existing = folder / "tool_result_001.txt"
        existing.write_text("旧证据", encoding="utf-8")
        path = write_artifact("sess1", "新结果")
        self.assertEqual(path.name, "tool_result_002.txt")
        self.assertEqual(existing.read_text(encoding="utf-8"), "旧证据")

    def test_sessions_are_isolated(self):
        one = write_artifact("sess1", "结果")
        two = write_artifact("sess2", "结果")
        self.assertEqual(one.parent.name, "sess1")
        self.assertEqual(two.parent.name, "sess2")

    def test_invalid_thread_id_is_rejected(self):
        with self.assertRaises(ValueError):
            write_artifact("../escape", "结果")


class PointerTest(ToolResultsCase):
    def test_pointer_keeps_head_and_tail_and_drops_the_middle(self):
        result = "A" * HEAD_CHARS + "B" * 40000 + "C" * TAIL_CHARS
        path = self.root / "sess1" / "tool_result_001.txt"
        pointer = make_pointer("run_command", result, path)
        self.assertIn("A" * 100, pointer)
        self.assertIn("C" * 100, pointer)
        self.assertNotIn("B" * 100, pointer)
        self.assertIn(str(path), pointer)
        self.assertIn(f"原始 {len(result)} 字符", pointer)
        self.assertIn(hashlib.sha256(result.encode("utf-8")).hexdigest(), pointer)
        self.assertLess(len(pointer), HEAD_CHARS + TAIL_CHARS + 500)

    def test_pointer_near_the_threshold_does_not_duplicate(self):
        # 刚过阈值时结尾预览从开头预览的末尾接着开始，同一段内容不重复出现。
        # 用互不相同的编号块构造结果，避免周期文本让子串统计失真。
        result = "".join(f"{index:06d}" for index in range(5100))
        self.assertGreater(len(result), RUN_COMMAND_EXTERNALIZE_CHARS)
        self.assertLess(len(result), HEAD_CHARS + TAIL_CHARS)
        path = self.root / "sess1" / "tool_result_001.txt"
        pointer = make_pointer("run_command", result, path)
        self.assertIn(result[:HEAD_CHARS], pointer)
        self.assertIn(result[HEAD_CHARS:], pointer)
        # 若结尾预览和开头预览有重叠，接缝前的这段会在指针里出现两次。
        before_seam = result[6040:6140]
        self.assertEqual(pointer.count(before_seam), 1)
        after_seam = result[6144:6244]
        self.assertEqual(pointer.count(after_seam), 1)


class ReadArtifactTest(ToolResultsCase):
    def test_reads_lines_by_page(self):
        text = "\n".join(f"line{index}" for index in range(10))
        path = write_artifact("sess1", text)
        page = read_artifact(str(path), offset=2, limit=3)
        self.assertIn("第 3-5 行, 共 10 行", page)
        self.assertIn("line2\nline3\nline4", page)
        tail = read_artifact(str(path), offset=9, limit=5)
        self.assertIn("第 10-10 行", tail)

    def test_offset_beyond_the_end_reports_range(self):
        path = write_artifact("sess1", "only line")
        page = read_artifact(str(path), offset=5, limit=10)
        self.assertIn("超出范围", page)
        self.assertNotIn("only line", page)

    def test_relative_path_resolves_inside_the_root(self):
        path = write_artifact("sess1", "内容")
        page = read_artifact(f"sess1/{path.name}", offset=0, limit=10)
        self.assertIn("内容", page)

    def test_paths_outside_the_root_are_rejected(self):
        secret = self.tmp / "secret.txt"
        secret.write_text("SECRET_BODY", encoding="utf-8")
        for candidate in ("../secret.txt", str(secret)):
            page = read_artifact(candidate, offset=0, limit=10)
            self.assertIn("读取失败", page)
            self.assertNotIn("SECRET_BODY", page)

    def test_huge_page_is_capped(self):
        path = write_artifact("sess1", "x" * 40000)
        page = read_artifact(str(path), offset=0, limit=10)
        self.assertIn("已截断", page)
        self.assertLess(len(page), 31000)


class PermissionTest(ToolResultsCase):
    def test_read_artifact_is_allowed_like_other_reads(self):
        decision = decide(Path("."), ToolRequest("read_artifact", {"path": "x"}))
        self.assertEqual(decision.action, PermissionAction.ALLOW)
        self.assertEqual(decision.rule_id, "read.allow")

    def test_dispatch_executes_read_artifact_without_an_approver(self):
        path = write_artifact("sess1", "hello")
        content = REGISTRY.dispatch(Path("."), "read_artifact", {"path": str(path)})
        self.assertIn("hello", content)

    def test_dispatch_rejects_bad_arguments_before_permission(self):
        content = REGISTRY.dispatch(Path("."), "read_artifact", {"path": "x", "bogus": 1})
        self.assertIn("参数不正确", content)
        content = REGISTRY.dispatch(
            Path("."), "read_artifact", {"path": "x", "offset": -1}
        )
        self.assertIn("不能小于 0", content)


class ToolsNodeTest(ToolResultsCase):
    def _state(self, name, arguments_json):
        return {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments_json},
                        }
                    ],
                }
            ],
            "loaded_tools": [],
            "turns": 1,
        }

    def test_large_result_is_replaced_by_a_pointer_before_entering_state(self):
        from agent.graph import tools_node

        big = "x" * 40000
        with patch("agent.graph.REGISTRY.dispatch_many", lambda *args, **kwargs: [big]):
            update = tools_node(
                self._state("run_command", '{"command": ["x"]}'),
                {"configurable": {"thread_id": "ext-test"}},
            )
        content = update["messages"][-1]["content"]
        self.assertIn("SHA-256", content)
        self.assertIn("tool_result_001.txt", content)
        self.assertNotIn(big, content)
        artifact = self.root / "ext-test" / "tool_result_001.txt"
        self.assertEqual(artifact.read_text(encoding="utf-8"), big)

    def test_small_result_passes_through_unchanged(self):
        from agent.graph import tools_node

        with patch("agent.graph.REGISTRY.dispatch_many", lambda *args, **kwargs: ["小结果"]):
            update = tools_node(
                self._state("list_dir", '{"relative": "."}'),
                {"configurable": {"thread_id": "ext-test"}},
            )
        self.assertEqual(update["messages"][-1]["content"], "小结果")
        self.assertFalse((self.root / "ext-test").exists())

    def test_disk_failure_keeps_the_original_result(self):
        from agent.graph import tools_node

        big = "x" * 40000

        def fail(_thread_id, _result):
            raise OSError("disk full")

        with (
            patch("agent.graph.REGISTRY.dispatch_many", lambda *args, **kwargs: [big]),
            patch("agent.graph.write_artifact", fail),
        ):
            update = tools_node(
                self._state("run_command", '{"command": ["x"]}'),
                {"configurable": {"thread_id": "ext-test"}},
            )
        self.assertEqual(update["messages"][-1]["content"], big)


if __name__ == "__main__":
    unittest.main()
