from pathlib import Path
import json
import os
import queue
import subprocess
import threading
import time

from agent.tool import REGISTRY
from agent.tool import ToolSpec


CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "mcp.json"
TRUST_PATH = Path(__file__).resolve().parent.parent / "data" / "connector_trust.json"


def _content_text(result: dict) -> str:
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


class StdioMCP:
    def __init__(self, command: str, args: list[str], env: dict):
        merged = os.environ.copy()
        for key, value in env.items():
            merged[str(key)] = str(value)
        self.proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=merged,
            bufsize=0,
        )
        self.queue = queue.Queue()
        self._next_id = 0
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _read_loop(self) -> None:
        buf = b""
        try:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    sep = buf.find(b"\r\n\r\n")
                    if sep < 0:
                        break
                    header = buf[:sep].decode("ascii", "replace")
                    length = None
                    for line in header.split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            length = int(line.split(":", 1)[1].strip())
                    if length is None:
                        buf = buf[sep + 4:]
                        continue
                    start = sep + 4
                    if len(buf) < start + length:
                        break
                    raw = buf[start:start + length]
                    buf = buf[start + length:]
                    try:
                        self.queue.put(json.loads(raw.decode("utf-8")))
                    except json.JSONDecodeError:
                        continue
        finally:
            self.queue.put(None)

    def _send(self, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        frame = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw
        os.write(self.proc.stdin.fileno(), frame)

    def request(self, method: str, params: dict, timeout: float = 30):
        self._next_id += 1
        ident = self._next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                message = self.queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(method) from exc
            if message is None:
                raise ConnectionError(method)
            if message.get("id") != ident:
                continue
            if "error" in message:
                error = message["error"]
                text = error.get("message") if isinstance(error, dict) else "mcp error"
                raise RuntimeError(text or "mcp error")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": {}})

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "dd-coder", "version": "0.1"},
        })
        self.notify("notifications/initialized")

    def list_tools(self) -> list:
        result = self.request("tools/list", {})
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=30)
        text = _content_text(result)
        if result.get("isError"):
            return "工具执行失败: " + text
        return text or "已完成"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _mcp_handler(server: str, tool_name: str):
    def handler(workspace: Path, arguments: dict) -> str:
        return MANAGER.call(server, tool_name, arguments)
    return handler


class ConnectorManager:
    def __init__(self, config_path: Path, trust_path: Path, registry):
        self.config_path = config_path
        self.trust_path = trust_path
        self.registry = registry
        self.sessions: dict[str, StdioMCP] = {}

    def ensure(self) -> None:
        try:
            servers = self._servers()
            trust = self._trust()
        except (OSError, json.JSONDecodeError):
            return
        wanted = set()
        for name, spec in servers.items():
            if not isinstance(name, str) or not self._entry_trusted(trust, name):
                self._drop(name)
                continue
            wanted.add(name)
            session = self.sessions.get(name)
            if session is not None and session.alive():
                continue
            self._drop(name)
            if isinstance(spec, dict):
                self._start(name, spec)
        for name in list(self.sessions):
            if name not in wanted:
                self._drop(name)

    def trusted(self, name: str) -> bool:
        try:
            return self._entry_trusted(self._trust(), name)
        except (OSError, json.JSONDecodeError):
            return False

    def connected(self, name: str) -> bool:
        session = self.sessions.get(name)
        return session is not None and session.alive()

    def call(self, server: str, tool_name: str, arguments: dict) -> str:
        if not self.trusted(server):
            return f"未执行: 连接器 {server} 未信任"
        if not self._ready(server):
            return f"未执行: 连接器 {server} 未连接"
        try:
            return self.sessions[server].call_tool(tool_name, arguments)
        except Exception:
            self._stop(server)
            if not self._ready(server):
                return "工具执行失败: 连接器无响应"
            try:
                return self.sessions[server].call_tool(tool_name, arguments)
            except Exception:
                return "工具执行失败: 连接器无响应"

    def close_all(self) -> None:
        for name in list(self.sessions):
            self._drop(name)

    def _ready(self, server: str) -> bool:
        if self.connected(server):
            return True
        spec = self._servers().get(server)
        if not isinstance(spec, dict):
            return False
        return self._start(server, spec)

    def _start(self, name: str, spec: dict) -> bool:
        command = spec.get("command")
        args = spec.get("args") or []
        env = spec.get("env") or {}
        if not isinstance(command, str) or not command:
            return False
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            return False
        if not isinstance(env, dict):
            return False
        session = None
        try:
            session = StdioMCP(command, args, env)
            session.initialize()
            tools = session.list_tools()
        except Exception:
            if session is not None:
                session.close()
            return False
        self.sessions[name] = session
        for tool in tools:
            self._register(name, tool)
        return True

    def _register(self, server: str, tool: object) -> None:
        if not isinstance(tool, dict):
            return
        tool_name = tool.get("name")
        if not isinstance(tool_name, str) or not tool_name or "__" in tool_name:
            return
        full_name = f"mcp__{server}__{tool_name}"
        if full_name in self.registry._specs:
            return
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        description = tool.get("description")
        if not isinstance(description, str) or not description.strip():
            description = tool_name
        self.registry.register(ToolSpec(
            name=full_name,
            description=" ".join(description.split()),
            parameters=schema,
            handler=_mcp_handler(server, tool_name),
            deferred=True,
        ))

    def _drop(self, name: str) -> None:
        self._stop(name)
        prefix = f"mcp__{name}__"
        for tool_name in list(self.registry._specs):
            if tool_name.startswith(prefix):
                del self.registry._specs[tool_name]

    def _stop(self, name: str) -> None:
        session = self.sessions.pop(name, None)
        if session is not None:
            session.close()

    def _servers(self) -> dict:
        if not self.config_path.is_file():
            return {}
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        return servers if isinstance(servers, dict) else {}

    def _trust(self) -> dict:
        if not self.trust_path.is_file():
            return {}
        data = json.loads(self.trust_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _entry_trusted(self, trust: dict, name: str) -> bool:
        entry = trust.get(name)
        return isinstance(entry, dict) and entry.get("trusted") is True


MANAGER = ConnectorManager(CONFIG_PATH, TRUST_PATH, REGISTRY)
