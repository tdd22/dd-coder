import json
from dataclasses import dataclass


PROMPT_BUDGET_CHARS = 12_000

BASE_INSTRUCTIONS = (
    "你是 dd-coder，只能操作当前工作区。"
    "读文件可以直接做。需要写入或运行命令时，直接调用对应工具。"
    "程序会暂停并询问用户，不要先用文字征求同意。"
    "危险命令和越界路径直接拒绝，用户同意也不能放行。"
)


class PromptBudgetError(RuntimeError):
    def __init__(self, required_chars: int, budget_chars: int):
        self.required_chars = required_chars
        self.budget_chars = budget_chars
        super().__init__(
            f"required prompt segments exceed budget: {required_chars} > {budget_chars}"
        )


@dataclass(frozen=True)
class PromptSegment:
    name: str
    content: str
    required: bool
    priority: int
    budget_priority: int
    emits_text: bool = True


@dataclass(frozen=True)
class SegmentDecision:
    name: str
    status: str
    reason: str
    chars: int


@dataclass(frozen=True)
class PromptPlan:
    system_messages: tuple[dict, ...]
    decisions: tuple[SegmentDecision, ...]
    used_chars: int


def tools_budget_json(tools: list) -> str:
    return json.dumps(tools, ensure_ascii=False, sort_keys=True)


def base_segment() -> PromptSegment:
    return PromptSegment(
        name="base",
        content=BASE_INSTRUCTIONS,
        required=True,
        priority=10,
        budget_priority=100,
    )


def tools_budget_segment(tools: list) -> PromptSegment:
    return PromptSegment(
        name="tools",
        content=tools_budget_json(tools),
        required=True,
        priority=0,
        budget_priority=100,
        emits_text=False,
    )


def optional_segment(
    name: str,
    content: str,
    *,
    priority: int,
    budget_priority: int,
) -> PromptSegment | None:
    text = str(content or "").strip()
    if not text:
        return None
    return PromptSegment(
        name=name,
        content=text,
        required=False,
        priority=priority,
        budget_priority=budget_priority,
    )


def plan_prompt(segments: list[PromptSegment], *, budget_chars: int = PROMPT_BUDGET_CHARS) -> PromptPlan:
    required = [segment for segment in segments if segment.required]
    required_chars = sum(len(segment.content) for segment in required)
    if required_chars > budget_chars:
        raise PromptBudgetError(required_chars, budget_chars)

    decisions = [
        SegmentDecision(segment.name, "selected", "required", len(segment.content))
        for segment in required
    ]
    remaining = budget_chars - required_chars
    optional = sorted(
        (segment for segment in segments if not segment.required),
        key=lambda segment: (-segment.budget_priority, segment.name),
    )
    selected = list(required)
    for segment in optional:
        chars = len(segment.content)
        if chars <= remaining:
            selected.append(segment)
            decisions.append(SegmentDecision(segment.name, "selected", "selected", chars))
            remaining -= chars
        else:
            decisions.append(
                SegmentDecision(segment.name, "rejected", "char_budget_exceeded", chars)
            )
    visible = sorted(
        (segment for segment in selected if segment.emits_text),
        key=lambda segment: (segment.priority, segment.name),
    )
    return PromptPlan(
        system_messages=tuple({"role": "system", "content": segment.content} for segment in visible),
        decisions=tuple(decisions),
        used_chars=sum(len(segment.content) for segment in selected),
    )
