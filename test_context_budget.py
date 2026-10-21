import json
import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.context_compact import (
    MessageViewLimitExceeded,
    RequestBudgetExceeded,
    ToolProtocolError,
    assert_request_budget,
    compact_context,
    dedup_file_reads,
    estimate_messages,
    generate_summary,
    prune_old_messages,
    truncate_tool_results,
    validate_tool_protocol,
)
from agent.graph import build_graph
from agent.prompt_assembly import (
    PromptBudgetError,
    base_segment,
    optional_segment,
    plan_prompt,
    tools_budget_segment,
)


def call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def assistant(content, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return message


def tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class AssemblyTest(unittest.TestCase):
    def test_required_segments_fail_closed_when_over_budget(self):
        segments = [base_segment(), tools_budget_segment([{"name": "x" * 50}])]
        with self.assertRaises(PromptBudgetError):
            plan_prompt(segments, budget_chars=10)

    def test_tight_budget_drops_optional_and_keeps_base(self):
        base = base_segment()
        optional = optional_segment("note", "可选说明" * 20, priority=20, budget_priority=1)
        plan = plan_prompt([base, optional], budget_chars=len(base.content) + 5)
        self.assertEqual([message["content"] for message in plan.system_messages], [base.content])
        self.assertEqual(plan.decisions[-1].reason, "char_budget_exceeded")

    def test_blank_memory_does_not_create_a_segment(self):
        self.assertIsNone(optional_segment("user_memory", "  ", priority=20, budget_priority=20))

    def test_tools_consume_budget_without_entering_system_text(self):
        base = base_segment()
        tools = tools_budget_segment([{"marker": "TOOL_MARKER_XYZ"}])
        optional = optional_segment("note", "y" * 30, priority=20, budget_priority=1)
        plan = plan_prompt(
            [base, tools, optional],
            budget_chars=len(base.content) + len(tools.content) + 10,
        )
        rendered = "\n".join(message["content"] for message in plan.system_messages)
        self.assertNotIn("TOOL_MARKER_XYZ", rendered)
        self.assertIn(base.content, rendered)
        self.assertEqual(plan.decisions[-1].status, "rejected")
        without_tools = plan_prompt([base, optional], budget_chars=len(base.content) + len(tools.content) + 10)
        self.assertEqual(without_tools.decisions[-1].status, "selected")


class CompactTest(unittest.TestCase):
    def test_below_threshold_returns_a_copy_and_no_layers(self):
        messages = [{"role": "user", "content": "看看配置"}]
        result = compact_context(messages, soft_threshold=80_000)
        result.messages[0]["content"] = "改过"
        self.assertEqual(messages[0]["content"], "看看配置")
        self.assertEqual(result.applied_layers, ())

    def test_large_tool_result_is_truncated(self):
        content = "x" * 100
        truncated = truncate_tool_results([tool("c1", content)], max_chars=10)
        self.assertTrue(truncated[0]["content"].startswith("x" * 10))
        self.assertIn("原始长度 100 字符", truncated[0]["content"])

    def test_duplicate_file_reads_keep_the_latest_pair(self):
        messages = [
            {"role": "user", "content": "读两次"},
            assistant("", [call("c1", "read_file", {"relative": "a.py"})]),
            tool("c1", "旧内容"),
            assistant("再读", [call("c2", "read_file", {"relative": "a.py"}), call("c3", "list_dir", {"relative": "."})]),
            tool("c2", "新内容"),
            tool("c3", "目录"),
        ]
        deduped = dedup_file_reads(messages)
        rendered = json.dumps(deduped, ensure_ascii=False)
        self.assertNotIn("c1", rendered)
        self.assertNotIn("旧内容", rendered)
        self.assertIn("新内容", rendered)
        self.assertIn("目录", rendered)
        validate_tool_protocol(deduped)

    def test_prune_removes_a_tool_pair_that_crosses_the_boundary(self):
        messages = [
            {"role": "user", "content": "旧问题"},
            assistant("读", [call("c1", "read_file", {"relative": "a.py"})]),
            tool("c1", "文件"),
            {"role": "user", "content": "新问题"},
        ]
        pruned = prune_old_messages(messages, keep_recent=2)
        rendered = json.dumps(pruned, ensure_ascii=False)
        self.assertNotIn("c1", rendered)
        self.assertNotIn("文件", rendered)
        self.assertEqual(pruned, [{"role": "user", "content": "新问题"}])
        validate_tool_protocol(pruned)

    def test_summary_failure_keeps_the_messages_it_received(self):
        messages = [{"role": "user", "content": f"第{index}句"} for index in range(6)]

        def fail(_text):
            raise RuntimeError("lite down")

        summarized, saved = generate_summary(messages, fail)
        self.assertEqual(summarized, messages)
        self.assertEqual(saved, 0)
        result = compact_context(
            messages,
            summarizer=fail,
            soft_threshold=1,
            hard_limit=100_000,
            keep_recent=100,
        )
        self.assertEqual(result.messages, messages)
        self.assertIn("L4", result.applied_layers)

    def test_hard_limit_blocks_the_provider_request(self):
        messages = [{"role": "user", "content": "SECRET_MESSAGE_BODY"}]
        with self.assertRaises(MessageViewLimitExceeded) as caught:
            compact_context(messages, summarizer=lambda _text: "", soft_threshold=1, hard_limit=1)
        self.assertNotIn("SECRET_MESSAGE_BODY", str(caught.exception))

    def test_invalid_tool_protocol_is_rejected_before_the_threshold(self):
        messages = [assistant("调用", [call("c1", "read_file", {"relative": "a.py"})])]
        with self.assertRaises(ToolProtocolError):
            compact_context(messages, soft_threshold=80_000)

    def test_request_budget_counts_system_messages_and_tools(self):
        with self.assertRaises(RequestBudgetExceeded):
            assert_request_budget(
                [{"role": "system", "content": "规则"}],
                [{"role": "user", "content": "你好"}],
                [{"name": "tool"}],
                limit=1,
            )


class ContextGraphTest(unittest.TestCase):
    def test_contexts_are_visible_to_the_model_but_not_written_back(self):
        seen = {}

        def fake_chat(config, messages, tools):
            seen["messages"] = messages
            seen["tools"] = tools
            return FakeMessage("看完了", None), "stop"

        class UserMemory:
            def get_context_for_agent(self):
                return "USER_MEMORY_MARKER"

        class WorkspaceMemory:
            def __init__(self, workspace):
                self.workspace = workspace

            def get_context_for_agent(self):
                return "WORKSPACE_MEMORY_MARKER"

        pending = [{
            "item_id": "pending:aaa000000001",
            "description": "PENDING_MARKER",
            "status": "open",
            "reason": None,
            "created_from": "transcript:demo:1",
            "created_at": "2026-10-16T00:00:00+00:00",
            "last_changed_from": "transcript:demo:1",
            "last_confirmed_at": "2026-10-16T00:00:00+00:00",
            "done_evidence": [],
        }]
        with (
            patch("agent.graph.chat", fake_chat),
            patch("agent.graph.UserMemory", UserMemory),
            patch("agent.graph.WorkspaceMemory", WorkspaceMemory),
        ):
            result = build_graph().invoke(
                {
                    "messages": [{"role": "user", "content": "看看配置"}],
                    "turns": 0,
                    "stop_reason": None,
                    "provider_finish_reason": None,
                    "pending_items": pending,
                },
                {"recursion_limit": 20},
            )
        rendered = "\n".join(
            message["content"] for message in seen["messages"] if message["role"] == "system"
        )
        self.assertIn("USER_MEMORY_MARKER", rendered)
        self.assertIn("WORKSPACE_MEMORY_MARKER", rendered)
        self.assertIn("PENDING_MARKER", rendered)
        self.assertEqual(seen["tools"][0]["function"]["name"], "list_dir")
        self.assertTrue(all(message["role"] != "system" for message in result["messages"]))
        self.assertEqual(result["pending_items"][0]["status"], "open")

    def test_summary_does_not_increase_turns_or_replace_the_agent_model(self):
        summaries = {"n": 0}

        def fake_summary(config, prompt, max_tokens=32):
            summaries["n"] += 1
            self.assertNotIn("PENDING_MARKER", prompt)
            return "旧对话摘要"

        def fake_chat(config, messages, tools):
            self.assertTrue(any("旧对话摘要" in message.get("content", "") for message in messages))
            return FakeMessage("继续", None), "stop"

        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "长" * 500}
            for index in range(6)
        ]
        with (
            patch("agent.context_compact.MESSAGE_SOFT_THRESHOLD", 1),
            patch("agent.graph.complete_text", fake_summary),
            patch("agent.graph.chat", fake_chat),
        ):
            result = build_graph().invoke(
                {
                    "messages": history,
                    "turns": 0,
                    "stop_reason": None,
                    "provider_finish_reason": None,
                },
                {"recursion_limit": 20},
            )
        self.assertEqual(summaries["n"], 1)
        self.assertEqual(result["turns"], 1)
        self.assertEqual(result["model_tier"], "craft")
        self.assertNotIn("旧对话摘要", json.dumps(result["messages"], ensure_ascii=False))

    def test_resume_assembles_and_compacts_again(self):
        from agent import context_compact

        calls = {"n": 0}
        real = context_compact.compact_context

        def spy(messages, summarizer=None, **kwargs):
            calls["n"] += 1
            return real(messages, summarizer=summarizer, **kwargs)

        chats = {"n": 0}

        def fake_chat(config, messages, tools):
            chats["n"] += 1
            if chats["n"] == 1:
                return FakeMessage("准备写入", [type("Call", (), {
                    "id": "call_1",
                    "function": type("Fn", (), {"name": "list_dir", "arguments": '{"relative": "."}'})(),
                })()]), "tool_calls"
            self.assertTrue(any(message["role"] == "system" for message in messages))
            return FakeMessage("写完了", None), "stop"

        def fake_dispatch(workspace, calls_arg, loaded=None, approver=None, loaded_skills=None):
            approver(type("Decision", (), {
                "request": type("Req", (), {"name": "write_file"})(),
                "rule_id": "path.write_requires_approval",
                "reason": "写入",
            })())
            return ["已写入"]

        graph = build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "compact-resume"}, "recursion_limit": 20}
        with (
            patch("agent.graph.compact_context", spy),
            patch("agent.graph.chat", fake_chat),
            patch("agent.graph.complete_json", lambda config, prompt, max_tokens=800: '{"operations":[{"action":"no_change"}]}'),
            patch("agent.graph.REGISTRY.dispatch_many", fake_dispatch),
        ):
            graph.invoke(
                {
                    "messages": [{"role": "user", "content": "写入文件"}],
                    "turns": 0,
                    "stop_reason": None,
                    "provider_finish_reason": None,
                    "current_user_event_id": "transcript:demo:1",
                    "current_user_recorded_at": "2026-10-16T00:00:00+00:00",
                    "turn_message_start": 0,
                    "pending_completion_proposals": [],
                },
                config,
            )
            graph.invoke(Command(resume=True), config)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
