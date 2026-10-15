import unittest
from unittest.mock import patch

from agent.graph import LoopStopReason, build_graph, stop_reason_for


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self):
        self.id = "call_1"
        self.function = FakeFunction("list_dir", '{"relative": "."}')


class FakeMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class StopReasonTest(unittest.TestCase):
    def test_tool_calls_continue_even_if_length(self):
        self.assertIsNone(stop_reason_for(True, "length"))

    def test_length_without_tools_is_max_tokens(self):
        self.assertEqual(stop_reason_for(False, "length"), LoopStopReason.MAX_TOKENS)

    def test_normal_stop_is_final_answer(self):
        self.assertEqual(stop_reason_for(False, "stop"), LoopStopReason.FINAL_ANSWER)

    def test_max_turns_stops_after_two_model_calls(self):
        calls = {"n": 0}

        def fake_chat(config, messages, tools):
            calls["n"] += 1
            return FakeMessage("先看目录", [FakeToolCall()]), "tool_calls"

        with patch("agent.graph.chat", fake_chat), patch("agent.graph.MAX_TURNS", 2):
            graph = build_graph()
            result = graph.invoke(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "turns": 0,
                    "stop_reason": None,
                    "provider_finish_reason": None,
                },
                {"recursion_limit": 20},
            )

        tool_messages = [item for item in result["messages"] if item["role"] == "tool"]
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(result["messages"][-1]["role"], "tool")
        self.assertEqual(result["stop_reason"], "max_turns")
        self.assertEqual(result["provider_finish_reason"], "tool_calls")
        self.assertEqual(result["messages"][-2]["content"], "先看目录")

    def test_length_keeps_partial_text(self):
        calls = {"n": 0}

        def fake_chat(config, messages, tools):
            calls["n"] += 1
            return FakeMessage("hel", None), "length"

        with patch("agent.graph.chat", fake_chat):
            graph = build_graph()
            result = graph.invoke(
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "turns": 0,
                    "stop_reason": None,
                    "provider_finish_reason": None,
                },
                {"recursion_limit": 20},
            )

        self.assertEqual(calls["n"], 1)
        self.assertEqual(result["stop_reason"], "max_tokens")
        self.assertEqual(result["provider_finish_reason"], "length")
        self.assertEqual(result["messages"][-1]["content"], "hel")


if __name__ == "__main__":
    unittest.main()
