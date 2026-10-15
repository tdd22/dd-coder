import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.tool import REGISTRY
from agent.tool import ToolRegistry
from agent.tool import ToolSpec


class DispatchTest(unittest.TestCase):
    def test_unknown_tool(self):
        text = REGISTRY.dispatch(Path("."), "nope", {})
        self.assertEqual(text, "未知工具: nope")

    def test_missing_argument_is_not_an_execution_error(self):
        text = REGISTRY.dispatch(Path("."), "read_file", {})
        self.assertTrue(text.startswith("参数不正确:"))

    def test_unknown_field(self):
        text = REGISTRY.dispatch(Path("."), "read_file", {"relative": "a.txt", "extra": 1})
        self.assertIn("未知参数", text)

    def test_list_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_text("x", encoding="utf-8")
            text = REGISTRY.dispatch(root, "list_dir", {})
        self.assertIn("a.txt", text)

    def test_write_then_read_stays_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            results = REGISTRY.dispatch_many(root, [
                ("write_file", {"relative": "hello.txt", "content": "done"}),
                ("read_file", {"relative": "hello.txt"}),
            ])
        self.assertEqual(results, ["已写入", "done"])

    def test_tool_names_come_from_the_registry(self):
        names = [item["function"]["name"] for item in REGISTRY.openai_tools()]
        self.assertEqual(names, ["list_dir", "read_file", "tool_search", "defer_execute"])

    def test_read_only_batch_overlaps(self):
        order = []
        lock = threading.Lock()

        def handler(workspace, arguments):
            with lock:
                order.append("start")
            time.sleep(0.05)
            with lock:
                order.append("end")
            return "ok"

        registry = ToolRegistry()
        parameters = {"type": "object", "properties": {}}
        registry.register(ToolSpec(
            name="read_a",
            description="a",
            parameters=parameters,
            handler=handler,
            concurrent_safe=True,
        ))
        registry.register(ToolSpec(
            name="read_b",
            description="b",
            parameters=parameters,
            handler=handler,
            concurrent_safe=True,
        ))
        results = registry.dispatch_many(Path("."), [("read_a", {}), ("read_b", {})])
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(order[:2], ["start", "start"])

    def test_a_write_forces_the_batch_to_run_in_order(self):
        order = []
        lock = threading.Lock()

        def handler(workspace, arguments):
            with lock:
                order.append("start")
            time.sleep(0.05)
            with lock:
                order.append("end")
            return "ok"

        registry = ToolRegistry()
        parameters = {"type": "object", "properties": {}}
        registry.register(ToolSpec(
            name="read_a",
            description="a",
            parameters=parameters,
            handler=handler,
            concurrent_safe=True,
        ))
        registry.register(ToolSpec(
            name="write_a",
            description="a",
            parameters=parameters,
            handler=handler,
        ))
        registry.dispatch_many(Path("."), [("read_a", {}), ("write_a", {})])
        self.assertEqual(order, ["start", "end", "start", "end"])

    def test_search_then_execute_writes_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            loaded = set()
            results = REGISTRY.dispatch_many(
                root,
                [
                    ("tool_search", {"names": ["write_file"]}),
                    ("defer_execute", {
                        "name": "write_file",
                        "arguments": {"relative": "hello.txt", "content": "done"},
                    }),
                ],
                loaded,
            )
            self.assertIn("新加载", results[0])
            self.assertEqual(results[1], "已写入")
            self.assertEqual(loaded, {"write_file"})
            self.assertEqual((root / "hello.txt").read_text(encoding="utf-8"), "done")

    def test_execute_before_search_is_rejected(self):
        loaded = set()
        text = REGISTRY.dispatch(
            Path("."),
            "defer_execute",
            {"name": "write_file", "arguments": {"relative": "a.txt", "content": "x"}},
            loaded,
        )
        self.assertIn("tool_search", text)
        self.assertEqual(loaded, set())

    def test_immediate_tool_cannot_use_defer_execute(self):
        text = REGISTRY.dispatch(
            Path("."),
            "defer_execute",
            {"name": "read_file", "arguments": {"relative": "a.txt"}},
            set(),
        )
        self.assertIn("可以直接调用", text)

    def test_missing_name_does_not_load(self):
        text, updated = REGISTRY.search(set(), {"names": ["nope"]})
        self.assertIn("未找到", text)
        self.assertEqual(updated, set())

    def test_second_search_says_already_loaded(self):
        loaded = set()
        REGISTRY.dispatch(Path("."), "tool_search", {"names": ["run_command"]}, loaded)
        text = REGISTRY.dispatch(Path("."), "tool_search", {"names": ["run_command"]}, loaded)
        self.assertIn("已加载", text)
        self.assertEqual(loaded, {"run_command"})


if __name__ == "__main__":
    unittest.main()
