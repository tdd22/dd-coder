import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent.connectors import MANAGER
from agent.graph import agent_node
from agent.graph import tools_node
from agent.permission import PermissionAction
from agent.permission import decide
from agent.permission import ToolRequest
from agent.skills import catalog_text
from agent.skills import load_skill
from agent.tool import REGISTRY


def _yaml_list(items, indent):
    if not items:
        return "[]"
    pad = " " * indent
    return "\n" + "\n".join(f"{pad}- {item}" for item in items)


def skill_md(title, summary, tools, body, network=False, read=None, write=None):
    read = ["**"] if read is None else read
    write = [] if write is None else write
    tool_block = _yaml_list(tools, 4)
    read_block = _yaml_list(read, 6)
    write_block = _yaml_list(write, 6)
    lines = [
        "---",
        f"title: {title}",
        f"summary: {summary}",
        "read_when:",
        "  - 提交代码",
        "agent_created: false",
        "permissions:",
        "  tools:" if tools else "  tools: []",
    ]
    if tools:
        lines.append(tool_block)
    lines.extend([
        f"  network: {'true' if network else 'false'}",
        "  paths:",
        "    read:" if read else "    read: []",
    ])
    if read:
        lines.append(read_block)
    lines.append("    write:" if write else "    write: []")
    if write:
        lines.append(write_block)
    lines.extend(["---", "", body, ""])
    return "\n".join(lines)


def write_skill(root, title, text):
    folder = root / title
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")


FAKE_MCP = r"""
import json
import sys

def read_message():
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        header += chunk
        if len(header) > 65536:
            return None
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))

def write_message(payload):
    raw = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if not message:
        break
    method = message.get("method")
    if method == "initialize":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "tools": [{
                    "name": "create_pr",
                    "description": "Create a PR",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                }]
            },
        })
    elif method == "tools/call":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "pr-created"}]},
        })
"""


class SkillCatalogTest(unittest.TestCase):
    def test_catalog_lists_summary_and_project_skill_wins(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            user = root / "user"
            workspace = root / "workspace"
            write_skill(user, "git-commit", skill_md("git-commit", "用户级", ["run_command"], "用户正文"))
            write_skill(
                workspace / ".dd-coder" / "skills",
                "git-commit",
                skill_md("git-commit", "项目级", ["run_command"], "项目正文"),
            )
            text = catalog_text(workspace, user_root=user)
        self.assertIn("git-commit", text)
        self.assertIn("项目级", text)
        self.assertNotIn("用户级", text)
        self.assertNotIn("项目正文", text)

    def test_invalid_skill_is_omitted(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            user = root / "user"
            workspace = root / "workspace"
            write_skill(
                user,
                "git-commit",
                skill_md(
                    "git-commit",
                    "坏技能",
                    ["run_command"],
                    "正文",
                    read=["../secret"],
                ),
            )
            text = catalog_text(workspace, user_root=user)
        self.assertNotIn("git-commit", text)

    def test_load_keeps_the_snapshot_after_the_file_changes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            user = root / "user"
            workspace = root / "workspace"
            workspace.mkdir()
            write_skill(user, "git-commit", skill_md("git-commit", "提交流程", ["run_command"], "先看状态"))
            loaded = []
            text = load_skill(workspace, "git-commit", loaded, user_root=user)
            self.assertEqual(text, "技能 'git-commit' 已加载。")
            write_skill(user, "git-commit", skill_md("git-commit", "提交流程", ["write_file"], "改过了"))
            again = load_skill(workspace, "git-commit", loaded, user_root=user)
        self.assertIn("已加载", again)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["content"], "先看状态")
        self.assertEqual(loaded[0]["permissions"]["tools"], ["run_command"])


class SkillGateTest(unittest.TestCase):
    def test_read_stays_open_when_no_skill_is_loaded(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "note.txt").write_text("hi", encoding="utf-8")
            text = REGISTRY.dispatch(root, "read_file", {"relative": "note.txt"}, loaded_skills=[])
        self.assertEqual(text, "hi")

    def test_loaded_skill_blocks_an_undeclared_read(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "note.txt").write_text("hi", encoding="utf-8")
            loaded = [{
                "title": "git-commit",
                "content": "先看状态",
                "permissions": {
                    "tools": ["run_command"],
                    "network": False,
                    "paths": {"read": ["**"], "write": []},
                },
            }]
            text = REGISTRY.dispatch(root, "read_file", {"relative": "note.txt"}, loaded_skills=loaded)
            self.assertEqual(text, "未执行: 当前技能未声明工具 read_file")
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "hi")

    def test_declared_read_must_match_the_skill_path(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "docs" / "a.txt").write_text("yes", encoding="utf-8")
            (root / "note.txt").write_text("no", encoding="utf-8")
            loaded = [{
                "title": "docs",
                "content": "只读文档",
                "permissions": {
                    "tools": ["read_file"],
                    "network": False,
                    "paths": {"read": ["docs/**"], "write": []},
                },
            }]
            allowed = REGISTRY.dispatch(root, "read_file", {"relative": "docs/a.txt"}, loaded_skills=loaded)
            blocked = REGISTRY.dispatch(root, "read_file", {"relative": "note.txt"}, loaded_skills=loaded)
            self.assertEqual(allowed, "yes")
            self.assertEqual(blocked, "未执行: 路径不在当前技能允许范围内")

    def test_declared_command_still_hits_the_hard_deny(self):
        loaded_tools = {"run_command"}
        skills = [{
            "title": "git-commit",
            "content": "提交",
            "permissions": {
                "tools": ["run_command"],
                "network": False,
                "paths": {"read": ["**"], "write": []},
            },
        }]
        asked = []
        text = REGISTRY.dispatch(
            Path("."),
            "defer_execute",
            {"name": "run_command", "arguments": {"command": ["sudo", "true"]}},
            loaded_tools,
            approver=lambda decision: asked.append(decision) or True,
            loaded_skills=skills,
        )
        self.assertIn("未执行", text)
        self.assertEqual(asked, [])


class SkillStateTest(unittest.TestCase):
    def test_tools_node_stores_the_loaded_skill_on_state(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            user = root / "user"
            workspace = root / "workspace"
            workspace.mkdir()
            write_skill(user, "git-commit", skill_md("git-commit", "提交流程", ["run_command"], "先看状态"))
            state = {
                "messages": [{
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_skill",
                        "type": "function",
                        "function": {"name": "skill", "arguments": '{"name": "git-commit"}'},
                    }],
                }],
                "loaded_tools": [],
                "loaded_skills": [],
                "turns": 1,
            }
            with patch("agent.graph.load_config", return_value={"workspace": workspace}), \
                 patch("agent.skills.USER_SKILLS_DIR", user):
                update = tools_node(state, {"configurable": {"thread_id": "thread-1"}})
        self.assertEqual(update["messages"][-1]["content"], "技能 'git-commit' 已加载。")
        self.assertEqual(update["loaded_skills"][0]["content"], "先看状态")

    def test_prompt_has_the_catalog_until_a_skill_is_loaded(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            user = root / "user"
            workspace = root / "workspace"
            workspace.mkdir()
            write_skill(user, "git-commit", skill_md("git-commit", "提交流程", ["run_command"], "先看状态"))
            captured = {}

            def fake_chat(settings, messages, tools):
                captured["messages"] = messages
                return type("Msg", (), {"content": "好", "tool_calls": None})(), "stop"

            settings = {
                "workspace": workspace,
                "models": {"lite": "m", "default": "m", "craft": "m"},
            }
            with patch("agent.graph.load_config", return_value=settings), \
                 patch("agent.graph.chat", fake_chat), \
                 patch("agent.graph.UserMemory") as user_memory, \
                 patch("agent.graph.WorkspaceMemory") as workspace_memory, \
                 patch("agent.skills.USER_SKILLS_DIR", user):
                user_memory.return_value.get_context_for_agent.return_value = ""
                workspace_memory.return_value.get_context_for_agent.return_value = ""
                agent_node({
                    "messages": [{"role": "user", "content": "帮我提交"}],
                    "loaded_skills": [],
                    "pending_items": [],
                    "turns": 0,
                })
            system = "\n".join(
                message["content"] for message in captured["messages"] if message["role"] == "system"
            )
            self.assertIn("git-commit", system)
            self.assertIn("提交流程", system)
            self.assertNotIn("先看状态", system)


class McpGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = root / "fake_mcp.py"
        self.server.write_text(FAKE_MCP, encoding="utf-8")
        self.config_path = root / "mcp.json"
        self.trust_path = root / "trust.json"
        self.config_path.write_text(json.dumps({
            "mcpServers": {
                "github": {
                    "command": sys.executable,
                    "args": [str(self.server)],
                    "env": {"GITHUB_TOKEN": "secret-token-xyz"},
                }
            }
        }), encoding="utf-8")
        self.trust_path.write_text("{}", encoding="utf-8")
        self.old_config = MANAGER.config_path
        self.old_trust = MANAGER.trust_path
        MANAGER.config_path = self.config_path
        MANAGER.trust_path = self.trust_path

    def tearDown(self):
        MANAGER.close_all()
        MANAGER.config_path = self.old_config
        MANAGER.trust_path = self.old_trust
        for name in list(REGISTRY._specs):
            if name.startswith("mcp__"):
                del REGISTRY._specs[name]
        self.tmp.cleanup()

    def test_untrusted_connector_stays_invisible(self):
        loaded = set()
        skills = [{
            "title": "github-pr",
            "content": "开 PR",
            "permissions": {
                "tools": ["mcp__github__create_pr"],
                "network": True,
                "paths": {"read": [], "write": []},
            },
        }]
        text = REGISTRY.dispatch(
            Path("."),
            "tool_search",
            {"names": ["mcp__github__create_pr"]},
            loaded,
            loaded_skills=skills,
        )
        self.assertIn("未找到", text)
        self.assertEqual(loaded, set())
        self.assertNotIn("secret-token-xyz", text)
        self.assertEqual(MANAGER.sessions, {})

    def test_trusted_tool_is_searched_then_asked_before_call(self):
        self.trust_path.write_text(json.dumps({"github": {"trusted": True}}), encoding="utf-8")
        skills = [{
            "title": "github-pr",
            "content": "开 PR",
            "permissions": {
                "tools": ["mcp__github__create_pr"],
                "network": True,
                "paths": {"read": [], "write": []},
            },
        }]
        loaded = set()
        found = REGISTRY.dispatch(
            Path("."),
            "tool_search",
            {"names": ["mcp__github__create_pr"]},
            loaded,
            loaded_skills=skills,
        )
        self.assertIn("新加载", found)
        self.assertIn("title", found)
        self.assertEqual(loaded, {"mcp__github__create_pr"})
        asked = []
        result = REGISTRY.dispatch(
            Path("."),
            "defer_execute",
            {
                "name": "mcp__github__create_pr",
                "arguments": {"title": "dark mode"},
            },
            loaded,
            approver=lambda decision: asked.append(decision.rule_id) or True,
            loaded_skills=skills,
        )
        self.assertEqual(asked, ["mcp.requires_approval"])
        self.assertEqual(result, "pr-created")
        self.assertNotIn("secret-token-xyz", found + result)

    def test_mcp_without_network_is_not_callable(self):
        self.trust_path.write_text(json.dumps({"github": {"trusted": True}}), encoding="utf-8")
        MANAGER.ensure()
        skills = [{
            "title": "github-pr",
            "content": "开 PR",
            "permissions": {
                "tools": ["mcp__github__create_pr"],
                "network": False,
                "paths": {"read": [], "write": []},
            },
        }]
        decision = decide(Path("."), ToolRequest("mcp__github__create_pr", {"title": "x"}))
        self.assertEqual(decision.action, PermissionAction.ASK)
        text = REGISTRY.dispatch(
            Path("."),
            "defer_execute",
            {"name": "mcp__github__create_pr", "arguments": {"title": "x"}},
            {"mcp__github__create_pr"},
            approver=lambda decision: True,
            loaded_skills=skills,
        )
        self.assertEqual(text, "未执行: 当前技能未声明工具 mcp__github__create_pr")


if __name__ == "__main__":
    unittest.main()
