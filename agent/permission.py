from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from langgraph.errors import GraphInterrupt
HARD_DENY_PATTERNS = (
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\brm\s+-(?=[^\s]*r)(?=[^\s]*f)[^\s]*", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|mkfs(?:\.[a-z0-9]+)?)\b", re.IGNORECASE),
    re.compile(r"\bdd\s+[^\n]*(?:\bif=|\bof=/dev/)", re.IGNORECASE),
    re.compile(r">\s*/dev/(?:sd|disk)", re.IGNORECASE),
)



class PermissionAction(str,Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: object

@dataclass(frozen=True)
class PermissionDecision:
    request: ToolRequest
    action: PermissionAction
    rule_id: str
    reason: str

def _command_text(request: ToolRequest) -> str:
    if not isinstance(request.arguments, dict):
        return ""
    command = request.arguments.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return ""
    return " ".join(command).strip()

def path_is_inside(workspace: Path, relative: str) -> bool:
    try:
        root = workspace.resolve()
        target = (root / relative).resolve()
    except (OSError, ValueError):
        return False
    return target == root or root in target.parents


def _relative(request: ToolRequest) -> str | None:
    if not isinstance(request.arguments, dict):
        return None
    if request.name == "list_dir" and "relative" not in request.arguments:
        return "."
    raw = request.arguments.get("relative")
    if isinstance(raw, str) and raw:
        return raw
    return None

def decide(workspace: Path, request: ToolRequest) -> PermissionDecision:
    if request.name in ("list_dir", "read_file", "write_file"):
        relative = _relative(request)
        if relative is None:
            return PermissionDecision(
                request,
                PermissionAction.DENY,
                "request.invalid_arguments",
                f"{request.name} 需要路径",
            )
        if not path_is_inside(workspace, relative):
            return PermissionDecision(
                request,
                PermissionAction.DENY,
                "path.outside_workspace",
                f"路径 {relative} 不在工作区内",
            )

    # read_artifact 读的是 data/tool-results/ 里的产物，路径边界由它的
    # handler 检查；权限层只确认它是只读工具。
    if request.name in ("list_dir", "read_file", "tool_search", "read_artifact", "skill"):
        return PermissionDecision(
            request,
            PermissionAction.ALLOW,
            "read.allow",
            "只读，直接允许",
        )
    if request.name == "write_file":
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "path.write_requires_approval",
            "写入会改工作区里的文件，需要你同意",
        )
    if request.name == "run_command":
        command = _command_text(request)
        if not command:
            return PermissionDecision(
                request,
                PermissionAction.DENY,
                "request.invalid_arguments",
                "command 必须是非空的字符串列表",
            )
        if any(pattern.search(command) for pattern in HARD_DENY_PATTERNS):
            return PermissionDecision(
                request,
                PermissionAction.DENY,
                "command.hard_deny",
                f"命令命中不可覆盖的拒绝规则: {command}",
            )
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "command.requires_approval",
            f"命令可能有副作用，需要你同意: {command}",
        )
    if request.name == "write_memory":
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "memory.write_requires_approval",
            "写入会把一条事实追加到这个工作区的记忆日志，需要你同意",
        )
    if request.name == "update_user_profile":
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "profile.write_requires_approval",
            "写入会更新跨项目的用户档案，需要你同意",
        )
    if request.name == "save_user_preference":
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "preference.write_requires_approval",
            "写入会保存一条跨项目偏好，需要你同意",
        )
    if request.name == "forget_user_preference":
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "preference.forget_requires_approval",
            "写入会忘掉一条跨项目偏好，需要你同意",
        )
    if request.name.startswith("mcp__"):
        return PermissionDecision(
            request,
            PermissionAction.ASK,
            "mcp.requires_approval",
            "外部工具可能有副作用，需要你同意",
        )
    return PermissionDecision(
    request,
    PermissionAction.DENY,
    "default.deny",
    f"没有规则覆盖工具 {request.name}",
    )


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PermissionResolution:
    decision: PermissionDecision
    allowed: bool
    approval_status: ApprovalStatus


def resolve_permission(decision: PermissionDecision, approver) -> PermissionResolution:
    if decision.action is PermissionAction.ALLOW:
        return PermissionResolution(decision, True, ApprovalStatus.NOT_REQUIRED)
    if decision.action is PermissionAction.DENY:
        return PermissionResolution(decision, False, ApprovalStatus.NOT_REQUIRED)
    try:
        approved = bool(approver(decision))
    except GraphInterrupt:
        raise
    except (KeyboardInterrupt, Exception):
        return PermissionResolution(decision, False, ApprovalStatus.CANCELLED)
    if approved:
        return PermissionResolution(decision, True, ApprovalStatus.APPROVED)
    return PermissionResolution(decision, False, ApprovalStatus.REJECTED)

# 终端输出询问
def console_approver(decision: PermissionDecision) -> bool:
    print(f"\n需要同意: {decision.reason}")
    print(f"规则: {decision.rule_id}")
    answer = input("输入 y 同意，其他键拒绝: ").strip().lower()
    return answer == "y"

# 解析后进行判断
def execution_block(workspace: Path, name: str, arguments: object,approver=console_approver) -> str | None:
    decision = decide(workspace, ToolRequest(name, arguments))
    resolution = resolve_permission(decision, approver)
    if resolution.allowed:
        return None
    if resolution.approval_status == ApprovalStatus.REJECTED:
        return "未执行: 你拒绝了这次操作"
    if resolution.approval_status == ApprovalStatus.CANCELLED:
        return "未执行: 审批被取消"
    return f"未执行: {decision.reason}"