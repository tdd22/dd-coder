import json
import uuid
from datetime import datetime


class PendingRejected(ValueError):
    pass


MAX_RENDERED_DONE = 3


def new_pending_id() -> str:
    return f"pending:{uuid.uuid4().hex[:12]}"


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())


def _aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PendingRejected("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PendingRejected("timestamp must include a timezone")
    return parsed


def _copy_items(items: list[dict]) -> list[dict]:
    return [dict(item) for item in items]


def _find(items: list[dict], item_id) -> dict:
    if not isinstance(item_id, str) or not item_id.startswith("pending:"):
        raise PendingRejected("unknown pending item")
    matches = [item for item in items if item.get("item_id") == item_id]
    if len(matches) != 1:
        raise PendingRejected("unknown pending item")
    return matches[0]


def parse_model_json(text: str) -> dict:
    try:
        payload = json.loads(str(text or "").strip())
    except json.JSONDecodeError as exc:
        raise PendingRejected("invalid json") from exc
    if not isinstance(payload, dict):
        raise PendingRejected("json must be an object")
    return payload


def operations_from_extract(payload: dict) -> list[dict]:
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise PendingRejected("operations must be a list")
    cleaned = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise PendingRejected("operation must be an object")
        action = operation.get("action")
        if action not in {"create", "block", "open", "no_change"}:
            raise PendingRejected("extract action is not allowed")
        cleaned.append(
            {
                "action": action,
                "item_id": operation.get("item_id"),
                "description": operation.get("description"),
                "status": operation.get("status"),
                "reason": operation.get("reason"),
            }
        )
    return cleaned


def apply_extract_operations(
    items: list[dict],
    operations: list[dict],
    *,
    source_event_id: str,
    recorded_at: str,
    make_id=new_pending_id,
) -> list[dict]:
    if not str(source_event_id).startswith("transcript:"):
        raise PendingRejected("source event is required")
    confirmed_at = _aware_time(recorded_at).isoformat()
    updated = _copy_items(items)
    for operation in operations:
        action = operation.get("action")
        if action == "no_change":
            continue
        if action == "create":
            description = _clean_text(operation.get("description"))
            status = operation.get("status") or "open"
            reason = _clean_text(operation.get("reason"))
            if not description:
                raise PendingRejected("description is required")
            if status not in {"open", "blocked"}:
                raise PendingRejected("create status is not allowed")
            if status == "blocked" and not reason:
                raise PendingRejected("blocked reason is required")
            updated.append(
                {
                    "item_id": make_id(),
                    "description": description,
                    "status": status,
                    "reason": reason if status == "blocked" else None,
                    "created_from": source_event_id,
                    "created_at": confirmed_at,
                    "last_changed_from": source_event_id,
                    "last_confirmed_at": confirmed_at,
                    "done_evidence": [],
                }
            )
            continue
        item = _find(updated, operation.get("item_id"))
        if item["status"] == "done":
            raise PendingRejected("done is terminal")
        if action == "block":
            reason = _clean_text(operation.get("reason"))
            if not reason:
                raise PendingRejected("blocked reason is required")
            item["status"] = "blocked"
            item["reason"] = reason
        elif action == "open":
            if item["status"] != "blocked":
                raise PendingRejected("only blocked work can be opened")
            item["status"] = "open"
            item["reason"] = None
        else:
            raise PendingRejected("extract action is not allowed")
        item["last_changed_from"] = source_event_id
        item["last_confirmed_at"] = confirmed_at
    return updated


def proposals_from_verify(payload: dict) -> list[dict]:
    if "operations" in payload:
        raise PendingRejected("verifier cannot submit operations")
    completions = payload.get("completions", [])
    if not isinstance(completions, list):
        raise PendingRejected("completions must be a list")
    proposals = []
    for completion in completions:
        if not isinstance(completion, dict):
            raise PendingRejected("completion must be an object")
        call_ids = completion.get("call_ids")
        if (
            not isinstance(completion.get("item_id"), str)
            or not isinstance(call_ids, list)
            or not call_ids
            or not all(isinstance(call_id, str) and call_id for call_id in call_ids)
        ):
            raise PendingRejected("completion evidence is invalid")
        proposals.append(
            {
                "item_id": completion["item_id"],
                "call_ids": list(call_ids),
                "reason": _clean_text(completion.get("reason")),
            }
        )
    return proposals


def _turn_call_ids(turn_messages: list[dict]) -> set[str]:
    call_ids = set()
    for message in turn_messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if isinstance(call_id, str):
                    call_ids.add(call_id)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str):
                call_ids.add(call_id)
    return call_ids


def _event_sequence(event_id: str) -> int:
    return int(str(event_id).rsplit(":", 1)[-1])


def commit_completions(
    items: list[dict],
    proposals: list[dict],
    call_results: dict[str, dict],
    turn_messages: list[dict],
) -> list[dict]:
    updated = _copy_items(items)
    allowed_calls = _turn_call_ids(turn_messages)
    for proposal in proposals:
        matches = [item for item in updated if item.get("item_id") == proposal.get("item_id")]
        if len(matches) != 1 or matches[0].get("status") != "open":
            continue
        evidence = []
        for call_id in proposal.get("call_ids") or []:
            if call_id not in allowed_calls:
                evidence = []
                break
            result = call_results.get(call_id)
            content = "" if result is None else str(result.get("content") or "")
            event_id = "" if result is None else str(result.get("event_id") or "")
            if (
                result is None
                or not event_id.startswith("transcript:")
                or content.startswith("未执行:")
            ):
                evidence = []
                break
            evidence.append(result)
        if not evidence:
            continue
        last = max(evidence, key=lambda result: _event_sequence(result["event_id"]))
        item = matches[0]
        item["status"] = "done"
        item["reason"] = None
        item["done_evidence"] = [result["event_id"] for result in evidence]
        item["last_changed_from"] = last["event_id"]
        item["last_confirmed_at"] = _aware_time(last["recorded_at"]).isoformat()
    return updated


def render_pending_context(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["[Pending work]"]
    for item in items:
        if item.get("status") == "done":
            continue
        reason = ""
        if item.get("status") == "blocked" and item.get("reason"):
            reason = f"; reason={item['reason']}"
        lines.append(
            f"- {item['item_id']}: {item['description']} "
            f"(status={item['status']}{reason}; "
            f"source={item['last_changed_from']}; "
            f"confirmed={item['last_confirmed_at']})"
        )
    done = sorted(
        (item for item in items if item.get("status") == "done"),
        key=lambda item: _aware_time(item.get("last_confirmed_at")),
        reverse=True,
    )
    for item in done[:MAX_RENDERED_DONE]:
        lines.append(
            f"- {item['item_id']}: {item['description']} "
            f"(status=done; source={item['last_changed_from']}; "
            f"confirmed={item['last_confirmed_at']})"
        )
    omitted = len(done) - MAX_RENDERED_DONE
    if omitted > 0:
        lines.append(f"- done_omitted={omitted}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
