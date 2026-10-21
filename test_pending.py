import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.graph import (
    build_graph,
    pending_extract_node,
    record_visible_messages,
    run_chat,
)
from agent.pending import (
    PendingRejected,
    apply_extract_operations,
    commit_completions,
    operations_from_extract,
    parse_model_json,
    render_pending_context,
)
from agent.transcript import open_transcript


SOURCE = "transcript:demo:8"
RECORDED = "2026-10-15T09:00:00+00:00"


def ids():
    numbers = iter(["pending:aaa000000001", "pending:aaa000000002"])

    def make_id():
        return next(numbers)

    return make_id


def item(status="open", item_id="pending:aaa000000001", **changes):
    value = {
        "item_id": item_id,
        "description": "升级 Python",
        "status": status,
        "reason": "先不要改文件" if status == "blocked" else None,
        "created_from": SOURCE,
        "created_at": RECORDED,
        "last_changed_from": SOURCE,
        "last_confirmed_at": RECORDED,
        "done_evidence": [],
    }
    value.update(changes)
    return value


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id="call_1"):
        self.id = call_id
        self.function = FakeFunction("list_dir", '{"relative": "."}')


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class PendingContractTest(unittest.TestCase):
    def test_create_open_and_blocked_items(self):
        created = apply_extract_operations(
            [],
            [
                {"action": "create", "description": "查看配置", "status": "open"},
                {
                    "action": "create",
                    "description": "升级 Python",
                    "status": "blocked",
                    "reason": "先不要改文件",
                },
            ],
            source_event_id=SOURCE,
            recorded_at=RECORDED,
            make_id=ids(),
        )

        self.assertEqual(created[0]["item_id"], "pending:aaa000000001")
        self.assertEqual(created[0]["status"], "open")
        self.assertIsNone(created[0]["reason"])
        self.assertEqual(created[1]["status"], "blocked")
        self.assertEqual(created[1]["reason"], "先不要改文件")
        self.assertEqual(created[0]["created_from"], SOURCE)

    def test_blocked_without_reason_is_rejected(self):
        with self.assertRaises(PendingRejected):
            apply_extract_operations(
                [],
                [{"action": "create", "description": "升级 Python", "status": "blocked"}],
                source_event_id=SOURCE,
                recorded_at=RECORDED,
                make_id=ids(),
            )
        with self.assertRaises(PendingRejected):
            apply_extract_operations(
                [item("open")],
                [{"action": "block", "item_id": "pending:aaa000000001", "reason": " "}],
                source_event_id=SOURCE,
                recorded_at=RECORDED,
            )

    def test_blocked_can_open_and_then_finish_with_real_evidence(self):
        opened = apply_extract_operations(
            [item("blocked")],
            [{"action": "open", "item_id": "pending:aaa000000001"}],
            source_event_id="transcript:demo:9",
            recorded_at="2026-10-15T10:00:00+00:00",
        )
        self.assertEqual(opened[0]["status"], "open")
        self.assertIsNone(opened[0]["reason"])
        finished = commit_completions(
            opened,
            [{"item_id": "pending:aaa000000001", "call_ids": ["call_1"], "reason": "测试通过"}],
            {
                "call_1": {
                    "event_id": "transcript:demo:12",
                    "recorded_at": "2026-10-15T10:05:00+00:00",
                    "content": "tests passed",
                }
            },
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "call_1", "function": {"name": "run_command"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "tests passed"},
            ],
        )
        self.assertEqual(finished[0]["status"], "done")
        self.assertEqual(finished[0]["done_evidence"], ["transcript:demo:12"])
        self.assertEqual(finished[0]["last_changed_from"], "transcript:demo:12")

    def test_blocked_or_done_cannot_finish_or_reopen(self):
        blocked = commit_completions(
            [item("blocked")],
            [{"item_id": "pending:aaa000000001", "call_ids": ["call_1"]}],
            {
                "call_1": {
                    "event_id": "transcript:demo:12",
                    "recorded_at": RECORDED,
                    "content": "ok",
                }
            },
            [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}],
        )
        self.assertEqual(blocked[0]["status"], "blocked")
        with self.assertRaises(PendingRejected):
            apply_extract_operations(
                [item("done", done_evidence=["transcript:demo:12"])],
                [{"action": "open", "item_id": "pending:aaa000000001"}],
                source_event_id="transcript:demo:13",
                recorded_at=RECORDED,
            )

    def test_bad_evidence_does_not_finish(self):
        current = [item("open")]
        proposals = [{"item_id": "pending:aaa000000001", "call_ids": ["call_1"]}]
        turn = [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
        rejected = commit_completions(
            current,
            proposals,
            {
                "call_1": {
                    "event_id": "transcript:demo:12",
                    "recorded_at": RECORDED,
                    "content": "未执行: 你拒绝了这次操作",
                }
            },
            turn,
        )
        cancelled = commit_completions(
            current,
            proposals,
            {
                "call_1": {
                    "event_id": "transcript:demo:12",
                    "recorded_at": RECORDED,
                    "content": "未执行: 审批被取消",
                }
            },
            turn,
        )
        unknown = commit_completions(current, proposals, {}, turn)
        other_turn = commit_completions(
            current,
            proposals,
            {
                "call_1": {
                    "event_id": "transcript:demo:12",
                    "recorded_at": RECORDED,
                    "content": "ok",
                }
            },
            [],
        )
        self.assertTrue(all(result[0]["status"] == "open" for result in (rejected, cancelled, unknown, other_turn)))

    def test_done_rendering_keeps_three_newest_items(self):
        done = [
            item("done", item_id=f"pending:aaa00000000{index}", last_confirmed_at=f"2026-10-1{index}T00:00:00+00:00")
            for index in range(4)
        ]
        rendered = render_pending_context([item("open", item_id="pending:bbb000000001")] + done)
        self.assertIn("pending:aaa000000001", rendered)
        self.assertIn("status=open", rendered)
        self.assertIn("pending:aaa000000003", rendered)
        self.assertNotIn("pending:aaa000000000", rendered)
        self.assertIn("done_omitted=1", rendered)

    def test_invalid_extract_json_is_rejected_before_state_changes(self):
        with self.assertRaises(PendingRejected):
            operations_from_extract(parse_model_json("not json"))


class PendingGraphTest(unittest.TestCase):
    def invoke(self, graph, message, event_id, extra=None, config=None):
        payload = {
            "messages": [{"role": "user", "content": message}],
            "turns": 0,
            "stop_reason": None,
            "provider_finish_reason": None,
            "pending_completion_proposals": [],
            "current_user_event_id": event_id,
            "current_user_recorded_at": RECORDED,
            "turn_message_start": 0,
        }
        if extra:
            payload.update(extra)
        return graph.invoke(payload, config or {"recursion_limit": 20})

    def test_same_user_event_extracts_once(self):
        prompts = []

        def fake_json(config, prompt, max_tokens=800):
            prompts.append(prompt)
            return '{"operations":[{"action":"create","description":"查看配置","status":"open"}]}'

        state = {
            "messages": [{"role": "user", "content": "看看配置"}],
            "pending_items": [],
            "processed_pending_source_ids": [],
            "current_user_event_id": SOURCE,
            "current_user_recorded_at": RECORDED,
            "turn_message_start": 0,
        }
        with patch("agent.graph.complete_json", fake_json):
            first = pending_extract_node(state)
            second = pending_extract_node({**state, **first})

        self.assertEqual(len(prompts), 1)
        self.assertEqual(second, {})
        self.assertEqual(len(first["pending_items"]), 1)

    def test_invalid_json_keeps_agent_running_without_pending_changes(self):
        def fake_json(config, prompt, max_tokens=800):
            return "not json"

        def fake_chat(config, messages, tools):
            return FakeMessage("我先不改任务", None), "stop"

        with patch("agent.graph.complete_json", fake_json), patch("agent.graph.chat", fake_chat):
            result = self.invoke(build_graph(), "看看配置", SOURCE)

        self.assertEqual(result.get("pending_items") or [], [])
        self.assertEqual(result["messages"][-1]["content"], "我先不改任务")
        self.assertEqual(result["processed_pending_source_ids"], [SOURCE])
        self.assertNotIn("system", {message["role"] for message in result["messages"]})

    def test_tool_loop_does_not_extract_again(self):
        prompts = []
        chats = {"n": 0}

        def fake_json(config, prompt, max_tokens=800):
            prompts.append(prompt)
            if "completions" in prompt:
                return '{"completions":[]}'
            return '{"operations":[{"action":"create","description":"查看目录","status":"open"}]}'

        def fake_chat(config, messages, tools):
            chats["n"] += 1
            if chats["n"] == 1:
                return FakeMessage("先看目录", [FakeToolCall()]), "tool_calls"
            return FakeMessage("目录看完了", None), "stop"

        def fake_dispatch(workspace, calls, loaded=None, approver=None, loaded_skills=None):
            return ["目录列出了"]

        with (
            patch("agent.graph.complete_json", fake_json),
            patch("agent.graph.chat", fake_chat),
            patch("agent.graph.REGISTRY.dispatch_many", fake_dispatch),
        ):
            result = self.invoke(build_graph(), "看看目录", SOURCE)

        self.assertEqual(chats["n"], 2)
        self.assertEqual(sum("不要输出 done" in prompt for prompt in prompts), 1)
        self.assertEqual(sum("completions" in prompt for prompt in prompts), 1)
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["model_tier"], "craft")
        self.assertTrue(all(message["role"] != "system" for message in result["messages"]))

    def test_resume_does_not_extract_again(self):
        prompts = []
        chats = {"n": 0}

        def fake_json(config, prompt, max_tokens=800):
            prompts.append(prompt)
            return '{"operations":[{"action":"no_change"}]}'

        def fake_chat(config, messages, tools):
            chats["n"] += 1
            if chats["n"] == 1:
                return FakeMessage("准备写入", [FakeToolCall()]), "tool_calls"
            return FakeMessage("写完了", None), "stop"

        def fake_dispatch(workspace, calls, loaded=None, approver=None, loaded_skills=None):
            decision = SimpleNamespace(
                request=SimpleNamespace(name="write_file"),
                rule_id="path.write_requires_approval",
                reason="写入文件",
            )
            approved = approver(decision)
            self.assertTrue(approved)
            return ["已写入"]

        graph = build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "resume-pending"}, "recursion_limit": 20}
        with (
            patch("agent.graph.complete_json", fake_json),
            patch("agent.graph.chat", fake_chat),
            patch("agent.graph.REGISTRY.dispatch_many", fake_dispatch),
        ):
            self.invoke(graph, "写入文件", SOURCE, config=config)
            result = graph.invoke(Command(resume=True), config)

        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["messages"][-1]["content"], "写完了")

    def test_verifier_is_skipped_without_open_work(self):
        prompts = []

        def fake_json(config, prompt, max_tokens=800):
            prompts.append(prompt)
            return '{"operations":[{"action":"no_change"}]}'

        def fake_chat(config, messages, tools):
            return FakeMessage("没有任务", None), "stop"

        with patch("agent.graph.complete_json", fake_json), patch("agent.graph.chat", fake_chat):
            result = self.invoke(build_graph(), "你好", SOURCE)

        self.assertEqual(prompts, [prompts[0]])
        self.assertNotIn("completions", prompts[0])
        self.assertEqual(result["pending_completion_proposals"], [])
        self.assertEqual(result["turns"], 1)

    def test_done_stores_transcript_evidence_and_keeps_prompt_out_of_history(self):
        prompts = []

        def fake_json(config, prompt, max_tokens=800):
            prompts.append(prompt)
            if "completions" in prompt:
                item_id = re.search(r"pending:[0-9a-f]{12}", prompt).group(0)
                return json.dumps(
                    {
                        "completions": [
                            {
                                "item_id": item_id,
                                "call_ids": ["call_1"],
                                "reason": "目录已经列出",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return '{"operations":[{"action":"create","description":"查看目录","status":"open"}]}'

        def fake_chat(config, messages, tools):
            self.assertTrue(any(
                message["role"] == "system" and "查看目录" in message["content"]
                for message in messages
            ))
            if not any(message["role"] == "tool" for message in messages):
                return FakeMessage("先看目录", [FakeToolCall()]), "tool_calls"
            return FakeMessage("目录看完了", None), "stop"

        def fake_dispatch(workspace, calls, loaded=None, approver=None, loaded_skills=None):
            return ["目录列出了"]

        with TemporaryDirectory() as directory:
            graph = build_graph(InMemorySaver())
            with (
                patch("agent.graph.get_graph", return_value=graph),
                patch("agent.transcript.TRANSCRIPTS_DIR", Path(directory)),
                patch("agent.graph.complete_json", fake_json),
                patch("agent.graph.chat", fake_chat),
                patch("agent.graph.REGISTRY.dispatch_many", fake_dispatch),
                patch("agent.graph.write_title_if_absent"),
            ):
                result = run_chat("看看目录", thread_id="pendingtest01")
                stored = graph.get_state({"configurable": {"thread_id": "pendingtest01"}}).values
                events = open_transcript("pendingtest01").read_events()

        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["model_tier"], "craft")
        self.assertTrue(all(message["role"] != "system" for message in stored["messages"]))
        self.assertEqual(events[0]["type"], "message")
        self.assertEqual(events[0]["role"], "user")
        evidence = next(event for event in events if event["type"] == "function_call_result")
        self.assertEqual(stored["pending_items"][0]["status"], "done")
        self.assertEqual(stored["pending_items"][0]["done_evidence"], [evidence["event_id"]])
        self.assertEqual(stored["pending_completion_proposals"], [])
        self.assertEqual(sum("不要输出 done" in prompt for prompt in prompts), 1)

    def test_record_visible_messages_skips_user_and_maps_tool_results(self):
        with TemporaryDirectory() as directory:
            with patch("agent.transcript.TRANSCRIPTS_DIR", Path(directory)):
                first = open_transcript("threada").append(
                    {"type": "message", "role": "user", "content": "看看目录"}
                )
                recorded = record_visible_messages(
                    "threada",
                    [
                        {"role": "user", "content": "看看目录"},
                        {
                            "role": "assistant",
                            "content": "先看目录",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "list_dir", "arguments": '{"relative": "."}'},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": "目录列出了"},
                    ],
                )
                events = open_transcript("threada").read_events()

        self.assertEqual(first["event_id"], "transcript:threada:1")
        self.assertEqual(recorded["call_results"]["call_1"]["event_id"], "transcript:threada:4")
        self.assertEqual([event["type"] for event in events], ["message", "message", "function_call", "function_call_result"])
        self.assertEqual(sum(event.get("role") == "user" for event in events), 1)


if __name__ == "__main__":
    unittest.main()
