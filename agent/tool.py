from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any
from typing import Callable
import json
from agent.permission import execution_block
from agent.permission import console_approver
import re

def reslove_inside(workspace:Path,relative:str)->Path:
    root=workspace.resolve()
    target=(root/relative).resolve()
    if target != root and root not in target.parents:
        raise SystemExit(f"路径{relative}不在工作空间{workspace}内")
    return target

def list_dir(workspace:Path,relative:str=".")->list[str]:
    folder=reslove_inside(workspace,relative)
    if not folder.is_dir():
        raise SystemExit(f"路径{relative}不是文件夹")
    names=[]
    for item in sorted(folder.iterdir()):
        suffix="/" if item.is_dir() else ""
        names.append(f"{item.name}{suffix}")
    return names


def read_file(workspace: Path, relative: str) -> str:
    path = reslove_inside(workspace, relative)
    if not path.is_file():
        raise SystemExit(f"不是文件: {relative}")
    return path.read_text(encoding="utf-8")


def write_file(workspace: Path, relative: str, content: str) -> None:
    path = reslove_inside(workspace, relative)
    if path.is_dir():
        raise SystemExit(f"不能写入文件夹: {relative}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def run_command(workspace: Path, command: list[str], relative: str = ".") -> str:
    folder = reslove_inside(workspace, relative)
    if not folder.is_dir():
        raise SystemExit(f"不是文件夹: {relative}")
    try:
        result = subprocess.run(
            command,
            cwd=folder,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("命令超过 30 秒，已停止")
    except FileNotFoundError:
        program = command[0] if command else ""
        raise SystemExit(f"找不到命令: {program}")
    except Exception as e:
        raise SystemExit(f"命令执行失败: {e}")
    output = (result.stdout + result.stderr).strip()
    return f"exit_code: {result.returncode}\n{output}"


@dataclass(frozen=True)
class ToolSpec:
    name:str
    description:str
    parameters:dict
    handler:Callable[[Path, dict],str]
    concurrent_safe:bool=False
    deferred:bool=False

# 只检查参数，不调用工具，不同时返回一句中文，后面原样交给模型
def validate_arguments(parameters: dict, arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "参数必须是对象"
    properties = parameters.get("properties", {})
    required = parameters.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        return "缺少必填参数: " + ", ".join(missing)
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return "未知参数: " + ", ".join(unknown)
    for key, value in arguments.items():
        spec = properties[key]
        expected = spec.get("type")
        if value is None and spec.get("nullable"):
            continue
        if expected == "string" and not isinstance(value, str):
            return f"{key} 必须是字符串"
        if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{key} 必须是整数"
        if expected == "array":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return f"{key} 必须是字符串列表"
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return f"{key} 必须是: " + ", ".join(str(item) for item in enum)
        if expected == "integer":
            minimum = spec.get("minimum")
            maximum = spec.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                return f"{key} 不能小于 {minimum}"
            if isinstance(maximum, int) and value > maximum:
                return f"{key} 不能大于 {maximum}"
        pattern = spec.get("pattern")
        if expected == "string" and pattern and re.fullmatch(pattern, value) is None:
            return f"{key} 格式不正确"
    return None

class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
    
    def register(self, spec: ToolSpec) -> None:
        if not spec.name or spec.name in self._specs:
            raise ValueError(f"工具名称不能重复: {spec.name}")
        self._specs[spec.name] = spec

    def openai_tools(self) -> list:
        tools = []
        for spec in self._specs.values():
            if spec.deferred:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": deepcopy(spec.parameters),
                },
            })
        return tools
    def directory(self) -> str:
        lines = []
        for spec in self._specs.values():
            if spec.deferred:
                lines.append(f"{spec.name}: {spec.description}")
        return "\n".join(lines) if lines else "(没有延迟工具)"
    
    # search 接收当前会话已加载过的名字，返回两样东西，给模型看的文字，以及更新后的名字集合。
    # block返回给模型告诉搜索arguments里面工具的结果，updated返回更新后的结果集合
    # 只有出现在updated里的工具才会被defer_execute执行
    def search(self, loaded: set[str], arguments: Any) -> tuple[str, set[str]]:
        error = validate_arguments(
            {
                "type": "object",
                "properties": {
                    "names": {"type": "array"},
                    "query": {"type": "string"},
                },
            },
            arguments,
        )
        if error:
            return f"参数不正确: {error}", loaded
        names = arguments.get("names") or []
        query = (arguments.get("query") or "").strip()
        if not names and not query:
            return "参数不正确: 需要 names 或 query", set(loaded)
        found: list[ToolSpec] = []
        missing: list[str] = []
        if names:
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    continue
                seen.add(name)
                spec = self._specs.get(name)
                if spec is None or not spec.deferred:
                    missing.append(name)
                else:
                    found.append(spec)
        else:
            words = query.lower().split()
            scored = []
            for spec in self._specs.values():
                if not spec.deferred:
                    continue
                text = f"{spec.name} {spec.description}".lower()
                score = sum(1 for word in words if word in text)
                if score:
                    scored.append((-score, spec.name, spec))
            scored.sort()
            found = [item[2] for item in scored]
            if not found:
                missing.append(query)
        updated = set(loaded)
        blocks = []
        for spec in found:
            state = "已加载" if spec.name in updated else "新加载"
            updated.add(spec.name)
            blocks.append(
                spec.name
                + f"（{state}）\n"
                + json.dumps(spec.parameters, ensure_ascii=False)
            )
        if missing:
            blocks.append("未找到: " + ", ".join(missing))
        return "\n\n".join(blocks), updated

    # defferred_tools的专用的执行入口
    # 只有检验通过了才会交给dispatch处理
    # 检验错误会被当作工具信息返回给agent
    def execute_deferred(self, workspace: Path, loaded: set[str], arguments: Any,approver=None) -> str:
        error = validate_arguments(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
            arguments,
        )
        if error:
            return f"参数不正确: {error}"
        name = arguments["name"]
        tool_arguments = arguments["arguments"]
        if not isinstance(tool_arguments, dict):
            return "参数不正确: arguments 必须是对象"
        spec = self._specs.get(name)
        if spec is None:
            return f"未知工具: {name}"
        if not spec.deferred:
            return f"{name} 可以直接调用，不要使用 defer_execute"
        if name not in loaded:
            return f"请先使用 tool_search 加载 {name}"
        return self.dispatch(workspace, name, tool_arguments, approver=approver)


    # search和defer_execute不会使用hanler执行，而是在dispatch里面被接走执行对应的函数工具
    # 因为handler只能看workspace和参数，看不到会话已经加载的名单，但是dispatch里面有，他的层级更高
    # 所以把这个两个工具执行放在这里，正常执行走不进handler里面。
    def dispatch(self, workspace: Path, name: str, arguments: Any,loaded:set[str]|None=None,approver=None) -> str:
        if name == "tool_search":
            if loaded is None:
                return "内部错误: 缺少已加载名单"
            text, updated = self.search(loaded, arguments)
            loaded.clear()
            loaded.update(updated)
            return text
        if name == "defer_execute":
            if loaded is None:
                return "内部错误: 缺少已加载名单"
            return self.execute_deferred(workspace, loaded, arguments,approver)
        spec = self._specs.get(name)
        if spec is None:
            return f"未知工具: {name}"
        error = validate_arguments(spec.parameters, arguments)
        if error:
            return f"参数不正确: {error}"
        blocked = execution_block(workspace, name, arguments,approver or console_approver)
        if blocked:
            return blocked
        try:
            return spec.handler(workspace, arguments)
        except (SystemExit, Exception) as exc:
            return f"工具执行失败: {exc}"
    
    def dispatch_many(self, workspace: Path, calls: list[tuple[str, Any]],loaded:set[str] | None = None,approver=None) -> list[str]:
        def run_one(call: tuple[str, Any]) -> str:
            name, arguments = call
            return self.dispatch(workspace, name, arguments,loaded,approver)

        specs = [self._specs.get(name) for name, arguments in calls]
        can_run_concurrently = len(calls) > 1 and all(
            spec is not None and spec.concurrent_safe and isinstance(arguments, dict)
            for spec, (name, arguments) in zip(specs, calls)
        )
        if not can_run_concurrently:
            return [run_one(call) for call in calls]

        with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
            return list(pool.map(run_one, calls))
    

def _handle_list_dir(workspace: Path, arguments: dict) -> str:
    names = list_dir(workspace, arguments.get("relative") or ".")
    return "\n".join(names) if names else "(空)"


def _handle_read_file(workspace: Path, arguments: dict) -> str:
    return read_file(workspace, arguments["relative"])


def _handle_write_file(workspace: Path, arguments: dict) -> str:
    write_file(workspace, arguments["relative"], arguments["content"])
    return "已写入"


def _handle_run_command(workspace: Path, arguments: dict) -> str:
    return run_command(
        workspace,
        arguments["command"],
        arguments.get("relative") or ".",
    )


REGISTRY = ToolRegistry()
REGISTRY.register(ToolSpec(
    name="list_dir",
    description="列出工作区中的文件和文件夹",
    parameters={
        "type": "object",
        "properties": {
            "relative": {"type": "string", "description": "相对工作区的路径，默认 ."},
        },
    },
    handler=_handle_list_dir,
    concurrent_safe=True,
))
REGISTRY.register(ToolSpec(
    name="read_file",
    description="读取工作区中的一个文件",
    parameters={
        "type": "object",
        "properties": {
            "relative": {"type": "string", "description": "相对工作区的文件路径"},
        },
        "required": ["relative"],
    },
    handler=_handle_read_file,
    concurrent_safe=True,
))
REGISTRY.register(ToolSpec(
    name="write_file",
    description="把文字写入工作区中的一个文件",
    parameters={
        "type": "object",
        "properties": {
            "relative": {"type": "string", "description": "相对工作区的文件路径"},
            "content": {"type": "string", "description": "要写入的全部文字"},
        },
        "required": ["relative", "content"],
    },
    handler=_handle_write_file,
    deferred=True,
))
REGISTRY.register(ToolSpec(
    name="run_command",
    description="在工作区中运行一条命令",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "description": "拆开的命令，例如 [\"python\", \"-c\", \"print(123)\"]",
            },
            "relative": {"type": "string", "description": "相对工作区的目录，默认 ."},
        },
        "required": ["command"],
    },
    handler=_handle_run_command,
    deferred=True,
))


def _handle_write_memory(workspace: Path, arguments: dict) -> str:
    from agent.workspace import WorkspaceMemory
    fact = WorkspaceMemory(workspace).append_daily_log(
        arguments["content"],
        kind=arguments["kind"],
        importance=arguments["importance"],
        source="model_tool",
        memory_key=arguments.get("memory_key"),
    )
    return f"已追加到工作区日志: {fact.fact_id}"


REGISTRY.register(ToolSpec(
    name="write_memory",
    description="把一条项目事实追加到工作区记忆日志。memory_key 是稳定问题名，例如 runtime.python-version；不填则按原句分别记住。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的一句话"},
            "kind": {
                "type": "string",
                "enum": ["decision", "convention", "pitfall", "outcome"],
                "description": "decision、convention、pitfall 或 outcome",
            },
            "importance": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "重要度，1 到 5",
            },
            "memory_key": {
                "type": "string",
                "pattern": r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
                "description": "可选。同一问题的新旧答案共用这个小写名字",
            },
        },
        "required": ["content", "kind", "importance"],
    },
    handler=_handle_write_memory,
    deferred=True,
))

def _handle_tool_search(workspace: Path, arguments: dict) -> str:
    return "请通过 dispatch 携带已加载名单调用 tool_search"


def _handle_defer_execute(workspace: Path, arguments: dict) -> str:
    return "请通过 dispatch 携带已加载名单调用 defer_execute"


def _handle_update_user_profile(workspace: Path, arguments: dict) -> str:
    from agent.user_memory import UserMemory
    from agent.user_memory import UserMemoryValidationError
    try:
        result = UserMemory().update_profile(arguments)
    except UserMemoryValidationError as exc:
        return f"档案未写入: {exc}"
    changed = ",".join(result.changed) or "-"
    unchanged = ",".join(result.unchanged) or "-"
    return f"档案已更新: changed={changed} unchanged={unchanged}"


REGISTRY.register(ToolSpec(
    name="update_user_profile",
    description="按字段更新这个用户的档案。只填写要改的字段。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "nullable": True, "description": "用户的名字"},
            "call_them": {"type": "string", "nullable": True, "description": "以后怎么称呼"},
            "pronouns": {"type": "string", "nullable": True, "description": "代词"},
            "city": {"type": "string", "nullable": True, "description": "城市"},
            "timezone": {"type": "string", "nullable": True, "description": "时区，例如 UTC+8"},
            "notes": {"type": "string", "nullable": True, "description": "其它稳定说明"},
        },
    },
    handler=_handle_update_user_profile,
    deferred=True,
))


def _handle_save_user_preference(workspace: Path, arguments: dict) -> str:
    from agent.user_memory import StalePreferenceUpdateError
    from agent.user_memory import UserMemory
    from agent.user_memory import UserMemoryValidationError
    expires_at = arguments.get("expires_at")
    if expires_at == "":
        expires_at = None
    try:
        result = UserMemory().set_preference(
            arguments["key"],
            arguments["value"],
            source="model_tool",
            expires_at=expires_at,
        )
    except (UserMemoryValidationError, StalePreferenceUpdateError) as exc:
        return f"偏好未写入: {exc}"
    return f"偏好{result.status.value}: {result.key} revision={result.revision}"


REGISTRY.register(ToolSpec(
    name="save_user_preference",
    description="按 key 保存一条跨项目偏好。只在用户明确要求长期记住时调用。",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "pattern": r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$",
                "description": "稳定的小写名字，例如 response.language",
            },
            "value": {"type": "string", "description": "当前取值"},
            "expires_at": {
                "type": "string",
                "description": "可选。带时区的过期时间，到点后不再进入提示",
            },
        },
        "required": ["key", "value"],
    },
    handler=_handle_save_user_preference,
    deferred=True,
))

def _handle_forget_user_preference(workspace: Path, arguments: dict) -> str:
    from agent.user_memory import StalePreferenceUpdateError
    from agent.user_memory import UserMemory
    from agent.user_memory import UserMemoryValidationError
    try:
        result = UserMemory().delete_preference(arguments["key"], source="model_tool")
    except (UserMemoryValidationError, StalePreferenceUpdateError) as exc:
        return f"偏好未遗忘: {exc}"
    return f"偏好{result.status.value}: {result.key} revision={result.revision}"


REGISTRY.register(ToolSpec(
    name="forget_user_preference",
    description="按精确的 key 忘掉一条跨项目偏好。只在用户明确要求忘掉时调用。",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "pattern": r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$",
                "description": "要忘掉的偏好 key，必须和提示里列出的一致",
            },
        },
        "required": ["key"],
    },
    handler=_handle_forget_user_preference,
    deferred=True,
))


REGISTRY.register(ToolSpec(
    name="tool_search",
    description="搜索后才能执行的工具。目录：\n" + REGISTRY.directory() + "\n用 names 精确查找，或用 query 按关键词查找。看到完整参数后再调用 defer_execute。",
    parameters={
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要加载的延迟工具名字",
            },
            "query": {"type": "string", "description": "在名字和说明里查找的关键词"},
        },
    },
    handler=_handle_tool_search,
))
REGISTRY.register(ToolSpec(
    name="defer_execute",
    description="执行已经用 tool_search 加载过的延迟工具。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "已经加载的延迟工具名字"},
            "arguments": {"type": "object", "description": "该工具自己的参数"},
        },
        "required": ["name", "arguments"],
    },
    handler=_handle_defer_execute,
))