import copy
import json


MESSAGE_SOFT_THRESHOLD = 80_000
MESSAGE_HARD_LIMIT = 120_000
REQUEST_HARD_LIMIT = 140_000
MAX_TOOL_RESULT_CHARS = 20_000
KEEP_RECENT_MESSAGES = 6
SUMMARY_RECENT_MESSAGES = 4


class ToolProtocolError(ValueError):
    pass


class MessageViewLimitExceeded(RuntimeError):
    def __init__(self, *, chars_before: int, chars_after: int, hard_limit: int, applied_layers: tuple[str, ...]):
        self.chars_before = chars_before
        self.chars_after = chars_after
        self.hard_limit = hard_limit
        self.applied_layers = applied_layers
        super().__init__(
            "message view remains above the hard limit after compaction: "
            f"{chars_after} >= {hard_limit}"
        )


class RequestBudgetExceeded(RuntimeError):
    def __init__(self, total_chars: int, limit: int):
        self.total_chars = total_chars
        self.limit = limit
        super().__init__(f"request exceeds hard limit: {total_chars} >= {limit}")


class CompactionResult:
    def __init__(self, messages: list, chars_before: int, chars_after: int, applied_layers: tuple[str, ...]):
        self.messages = messages
        self.chars_before = chars_before
        self.chars_after = chars_after
        self.applied_layers = applied_layers


def estimate_messages(messages: list) -> int:
    return len(json.dumps(messages, ensure_ascii=False, default=str, sort_keys=True))


def estimate_tools(tools: list) -> int:
    return len(json.dumps(tools, ensure_ascii=False, sort_keys=True))


def validate_tool_protocol(messages: list) -> dict[str, tuple[int, int]]:
    pending: dict[str, int] = {}
    pairs: dict[str, tuple[int, int]] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if not call_id or call_id in pending or call_id in pairs:
                    raise ToolProtocolError("invalid tool call id")
                pending[call_id] = index
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ToolProtocolError("tool result has no matching call")
            pairs[call_id] = (pending.pop(call_id), index)
    if pending:
        raise ToolProtocolError("tool call is missing its result")
    return pairs


def truncate_tool_results(messages: list, *, max_chars: int = MAX_TOOL_RESULT_CHARS) -> list:
    updated = []
    for message in messages:
        if message.get("role") == "tool" and len(str(message.get("content") or "")) > max_chars:
            content = str(message.get("content") or "")
            message = dict(message)
            message["content"] = content[:max_chars] + f"\n\n[... 已截断, 原始长度 {len(content)} 字符 ...]"
        updated.append(message)
    return updated


def _read_path(call: dict) -> str | None:
    function = call.get("function") or {}
    if function.get("name") != "read_file":
        return None
    raw = function.get("arguments") or "{}"
    if isinstance(raw, str):
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        arguments = raw
    else:
        return None
    path = arguments.get("relative") if isinstance(arguments, dict) else None
    if not isinstance(path, str) or not path:
        return None
    return path


def _without_tool_interactions(messages: list, obsolete_ids: set[str]) -> list:
    if not obsolete_ids:
        return messages
    kept = []
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") in obsolete_ids:
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls = [call for call in message["tool_calls"] if call.get("id") not in obsolete_ids]
            if len(calls) != len(message["tool_calls"]):
                message = dict(message)
                if calls:
                    message["tool_calls"] = calls
                else:
                    message.pop("tool_calls", None)
                if not str(message.get("content") or "").strip() and not message.get("tool_calls"):
                    continue
        kept.append(message)
    return kept


def dedup_file_reads(messages: list) -> list:
    validate_tool_protocol(messages)
    latest: dict[str, str] = {}
    call_paths: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            path = _read_path(call)
            call_id = call.get("id")
            if path and call_id:
                latest[path] = call_id
                call_paths[call_id] = path
    obsolete = {
        call_id
        for call_id, path in call_paths.items()
        if latest.get(path) != call_id
    }
    deduped = _without_tool_interactions(messages, obsolete)
    validate_tool_protocol(deduped)
    return deduped


def prune_old_messages(messages: list, *, keep_recent: int = KEEP_RECENT_MESSAGES) -> list:
    interactions = validate_tool_protocol(messages)
    if len(messages) <= keep_recent:
        return messages
    start = len(messages) - keep_recent
    selected = set(range(start, len(messages)))
    crossing = {
        tool_id
        for tool_id, (call_index, result_index) in interactions.items()
        if (call_index in selected) != (result_index in selected)
    }
    kept = [message for index, message in enumerate(messages) if index in selected]
    pruned = _without_tool_interactions(kept, crossing)
    validate_tool_protocol(pruned)
    return pruned


def generate_summary(messages: list, summarizer, *, recent_count: int = SUMMARY_RECENT_MESSAGES) -> tuple[list, int]:
    interactions = validate_tool_protocol(messages)
    if summarizer is None or len(messages) <= recent_count:
        return messages, 0
    recent_start = len(messages) - recent_count
    moved = True
    while moved:
        moved = False
        for call_index, result_index in interactions.values():
            if call_index < recent_start <= result_index:
                recent_start = call_index
                moved = True
    if recent_start <= 0:
        return messages, 0
    old_messages = messages[:recent_start]
    recent = messages[recent_start:]
    try:
        summary = str(summarizer(json.dumps(old_messages, ensure_ascii=False, default=str))).strip()
    except Exception:
        return messages, 0
    if not summary:
        return messages, 0
    summarized = [
        {"role": "user", "content": f"[对话摘要]\n{summary}"},
        {"role": "assistant", "content": "好的，我已了解之前的对话内容。"},
        *recent,
    ]
    before = estimate_messages(messages)
    after = estimate_messages(summarized)
    if after >= before:
        return messages, 0
    return summarized, before - after


def compact_context(
    messages: list,
    *,
    summarizer=None,
    soft_threshold: int | None = None,
    hard_limit: int | None = None,
    max_tool_chars: int = MAX_TOOL_RESULT_CHARS,
    keep_recent: int = KEEP_RECENT_MESSAGES,
) -> CompactionResult:
    if soft_threshold is None:
        soft_threshold = MESSAGE_SOFT_THRESHOLD
    if hard_limit is None:
        hard_limit = MESSAGE_HARD_LIMIT
    working = copy.deepcopy(messages)
    validate_tool_protocol(working)
    chars_before = estimate_messages(working)
    if chars_before < soft_threshold:
        return CompactionResult(working, chars_before, chars_before, ())

    applied: list[str] = []
    working = truncate_tool_results(working, max_chars=max_tool_chars)
    applied.append("L1")
    if estimate_messages(working) < soft_threshold:
        return CompactionResult(working, chars_before, estimate_messages(working), tuple(applied))

    working = dedup_file_reads(working)
    applied.append("L2")
    if estimate_messages(working) < soft_threshold:
        return CompactionResult(working, chars_before, estimate_messages(working), tuple(applied))

    working = prune_old_messages(working, keep_recent=keep_recent)
    applied.append("L3")
    if estimate_messages(working) < soft_threshold:
        return CompactionResult(working, chars_before, estimate_messages(working), tuple(applied))

    working, _saved = generate_summary(working, summarizer)
    applied.append("L4")
    chars_after = estimate_messages(working)
    if chars_after >= hard_limit:
        raise MessageViewLimitExceeded(
            chars_before=chars_before,
            chars_after=chars_after,
            hard_limit=hard_limit,
            applied_layers=tuple(applied),
        )
    return CompactionResult(working, chars_before, chars_after, tuple(applied))


def assert_request_budget(system_messages: list, messages: list, tools: list, *, limit: int = REQUEST_HARD_LIMIT) -> int:
    total = estimate_messages(system_messages) + estimate_messages(messages) + estimate_tools(tools)
    if total >= limit:
        raise RequestBudgetExceeded(total, limit)
    return total
