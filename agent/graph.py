from typing_extensions import TypedDict
from langgraph.constants import START
from langgraph.graph import StateGraph, END
from agent.config import load_config
from agent.llm import complete
from agent.llm import chat
from agent.llm import complete_text
from agent.llm import complete_json
from agent.tool import REGISTRY
import json
import sqlite3
from pathlib import Path
from enum import Enum
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt

from agent.routing import route_task
from agent.transcript import TranscriptCorruptionError, open_transcript
from agent.workspace import WorkspaceMemory
from agent.user_memory import UserMemory
from agent.pending import (
    PendingRejected,
    apply_extract_operations,
    commit_completions,
    operations_from_extract,
    parse_model_json,
    proposals_from_verify,
    render_pending_context,
)


class LoopStopReason(str,Enum):
    FINAL_ANSWER= "final_answer"
    MAX_TOKENS= "max_tokens"
    MAX_TURNS= "max_turns"



MAX_TURNS=8

class State(TypedDict):
    turns:int
    stop_reason:str|None
    provider_finish_reason:str|None
    messages:list
    loaded_tools:list
    model_tier:str|None
    model_name:str|None
    pending_items:list
    processed_pending_source_ids:list
    pending_completion_proposals:list
    current_user_event_id:str|None
    current_user_recorded_at:str|None
    turn_message_start:int

# loaded_tools、pending_items、processed_pending_source_ids 要留到下一次提问，
# 所以 run_chat 里 graph.invoke 的那个字典不要加这三个字段。
# pending_completion_proposals 每次新的用户消息重置为空。
def stop_reason_for(has_tool_calls:bool,finish_reason:str|None):
    if has_tool_calls:
        return None
    if finish_reason=="length":
        return LoopStopReason.MAX_TOKENS
    return LoopStopReason.FINAL_ANSWER
TOOLS = REGISTRY.openai_tools()


def agent_node(state:State)->dict:
    settings=load_config()
    route=route_task(settings,"agent")
    routed={**settings,"model":route.model}
    messages=state["messages"]
    user_context=UserMemory().get_context_for_agent()
    workspace_context=WorkspaceMemory(settings["workspace"]).get_context_for_agent()
    pending_context=render_pending_context(state.get("pending_items") or [])
    prefix=[]
    if user_context:
        prefix.append({"role":"system","content":user_context})
    if workspace_context:
        prefix.append({"role":"system","content":workspace_context})
    if pending_context:
        prefix.append({"role":"system","content":pending_context})
    if prefix:
        messages=prefix+messages
    # 在这里和模型进行交互
    # 这段文字只放进这一次chat参数，写回状态仍用state["messages"]
    message ,finish_reason = chat(routed, messages, TOOLS)

    assistant = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        assistant["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    reason=stop_reason_for(bool(message.tool_calls),finish_reason)
    return {
        "messages": state["messages"] + [assistant],
        "turns": state.get("turns", 0) + 1,
        "stop_reason": reason.value if reason else None,
        "provider_finish_reason": finish_reason,
        "model_tier": route.tier.value,
        "model_name": route.model,
    }

def page_approver(decision):
    approved = interrupt({
        "tool": decision.request.name,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
    })
    return bool(approved)

def tools_node(state: State) -> dict:
    settings = load_config()
    workspace = settings["workspace"]
    last = state["messages"][-1]
    calls = []
    ids = []
    for call in last["tool_calls"]:
        name = call["function"]["name"]
        raw = call["function"]["arguments"] or "{}"
        print(f"tool call: {name} {raw}")
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = raw
        calls.append((name, arguments))
        ids.append(call["id"])
    loaded = set(state.get("loaded_tools") or [])
    results = REGISTRY.dispatch_many(workspace, calls,loaded,page_approver)
    tool_messages = []
    for tool_id, result in zip(ids, results):
        tool_messages.append(
            {"role": "tool", "tool_call_id": tool_id, "content": result}
        )

    update={"messages": state["messages"] + tool_messages,
             "loaded_tools": sorted(loaded),
    }
    if state.get("turns", 0) >= MAX_TURNS:
        update["stop_reason"]=LoopStopReason.MAX_TURNS.value
    return update

def route(state: State) -> str:
    last = state["messages"][-1]
    if last.get("tool_calls"):
        return "tools"
    return "pending_verify"

def after_tools(state:State)->str:
    if state.get("stop_reason") == LoopStopReason.MAX_TURNS.value:
        return END
    return "agent"


def _lite_config(task: str) -> dict:
    settings = load_config()
    routed = route_task(settings, task)
    return {**settings, "model": routed.model}


def _active_pending(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("status") in {"open", "blocked"}]


def pending_extract_node(state: State) -> dict:
    source = state.get("current_user_event_id")
    recorded_at = state.get("current_user_recorded_at")
    processed = list(state.get("processed_pending_source_ids") or [])
    if not source or not recorded_at or source in processed:
        return {}
    processed_next = processed + [source]
    start = int(state.get("turn_message_start") or 0)
    messages = state.get("messages") or []
    if start < 0 or start >= len(messages) or messages[start].get("role") != "user":
        return {"processed_pending_source_ids": processed_next}
    user_message = messages[start].get("content") or ""
    if not str(user_message).strip():
        return {"processed_pending_source_ids": processed_next}
    items = list(state.get("pending_items") or [])
    active = _active_pending(items)
    prompt = (
        "根据当前用户消息，为会话中的未决任务提出变更建议。"
        "只输出一个 JSON 对象，不要 Markdown。\n"
        '{"operations":[{"action":"create","description":"任务","status":"open","reason":null}]}\n'
        "action 只能是 create、block、open、no_change。不要输出 done。\n"
        "create 不要提供 item_id。status 只能是 open 或 blocked；blocked 必须有 reason。\n"
        "block 和 open 的 item_id 必须来自下面的清单。没有任务变化时输出 "
        '{"operations":[{"action":"no_change"}]}。\n'
        f"当前未决任务：\n{json.dumps(active, ensure_ascii=False)}\n"
        f"用户消息：\n{user_message}"
    )
    try:
        payload = parse_model_json(complete_json(_lite_config("pending_extract"), prompt))
        updated = apply_extract_operations(
            items,
            operations_from_extract(payload),
            source_event_id=source,
            recorded_at=recorded_at,
        )
    except (PendingRejected, Exception) as exc:
        print(f"pending extract skipped: {exc}")
        return {"processed_pending_source_ids": processed_next}
    return {
        "pending_items": updated,
        "processed_pending_source_ids": processed_next,
    }


def pending_verify_node(state: State) -> dict:
    items = list(state.get("pending_items") or [])
    open_items = [item for item in items if item.get("status") == "open"]
    if not open_items:
        return {"pending_completion_proposals": []}
    start = int(state.get("turn_message_start") or 0)
    turn_messages = (state.get("messages") or [])[start:]
    prompt = (
        "判断哪些 open 任务已经被本轮真实工具结果证明完成。"
        "只输出一个 JSON 对象，不要 Markdown。\n"
        '{"completions":[{"item_id":"pending:xxxxxxxxxxxx","call_ids":["call_id"],"reason":"原因"}]}\n'
        "没有足够证据时输出 {\"completions\":[]}。不要输出 operations，不要把 blocked 或 done 标成完成。\n"
        "call_ids 必须来自下面本轮消息里的工具调用。\n"
        f"open 任务：\n{json.dumps(open_items, ensure_ascii=False)}\n"
        f"本轮消息：\n{json.dumps(turn_messages, ensure_ascii=False)}"
    )
    try:
        payload = parse_model_json(complete_json(_lite_config("pending_verify"), prompt))
        proposals = proposals_from_verify(payload)
    except (PendingRejected, Exception) as exc:
        print(f"pending verify skipped: {exc}")
        return {"pending_completion_proposals": []}
    known = {item["item_id"] for item in open_items}
    return {
        "pending_completion_proposals": [
            proposal for proposal in proposals if proposal["item_id"] in known
        ]
    }


def build_graph(checkpointer=None):
    builder=StateGraph(State)
    builder.add_node("pending_extract", pending_extract_node)
    builder.add_node("agent",agent_node)
    builder.add_node("tools",tools_node)
    builder.add_node("pending_verify", pending_verify_node)

    builder.add_edge(START, "pending_extract")
    builder.add_edge("pending_extract", "agent")
    builder.add_conditional_edges(
        "agent",
        route,
        {"tools": "tools", "pending_verify": "pending_verify"},
    )
    builder.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})
    builder.add_edge("pending_verify", END)
    return builder.compile(checkpointer=checkpointer)

def main()->None:
    graph=build_graph()
    result=graph.invoke({
        "messages":[{"role":"user","content":"把 hello.txt 改成一行：done。不要运行命令。"}],
        "turns": 0,
        "stop_reason": None,
        "provider_finish_reason": None,
    },
    {"recursion_limit": MAX_TURNS * 2 + 5}
    )
    print(result["messages"][-1]["content"])

if __name__=="__main__":
    main()





_graph = None
_conn=None
THREAD_ID = "default"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints.sqlite"


def get_graph():
    global _graph
    if _graph is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _graph = build_graph(checkpointer=SqliteSaver(_conn))
    return _graph


# 将message写进对应thread_id的transcript文件里面
# 将工具调用和工具执行结果写进transcript
def record_visible_messages(thread_id: str, messages: list) -> dict:
    transcript = open_transcript(thread_id)
    events = []
    call_results = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        # 用户消息在 run_chat 进入图之前已经写入，这里再写会变成第二条用户事件。
        if role == "user":
            continue
        if role == "assistant" and content:
            events.append(transcript.append({
                "type": "message",
                "role": role,
                "content": content,
            }))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                raw = call["function"]["arguments"] or "{}"
                try:
                    arguments = json.loads(raw)
                except json.JSONDecodeError:
                    arguments = {"raw": raw}
                if not isinstance(arguments, dict):
                    arguments = {"raw": raw}
                events.append(transcript.append({
                    "type": "function_call",
                    "name": call["function"]["name"],
                    "arguments": arguments,
                    "callId": call["id"],
                }))
        elif role == "tool":
            event = transcript.append({
                "type": "function_call_result",
                "callId": message.get("tool_call_id") or "",
                "output": {"content": content},
            })
            events.append(event)
            call_id = message.get("tool_call_id") or ""
            if call_id:
                call_results[call_id] = {
                    "event_id": event["event_id"],
                    "recorded_at": event["recorded_at"],
                    "content": content,
                }
    return {"events": events, "call_results": call_results}


def commit_pending_done(graph, config: dict, state: dict, call_results: dict) -> None:
    proposals = list(state.get("pending_completion_proposals") or [])
    start = int(state.get("turn_message_start") or 0)
    turn_messages = (state.get("messages") or [])[start:]
    updated = commit_completions(
        list(state.get("pending_items") or []),
        proposals,
        call_results,
        turn_messages,
    )
    graph.update_state(
        config,
        {
            "pending_items": updated,
            "pending_completion_proposals": [],
        },
    )
# 使用lite模型
# 已经有标题了直接返回
# 没有的话用模型按第一条用户消息生成
def write_title_if_absent(thread_id: str) -> None:
    transcript = open_transcript(thread_id)
    state = transcript.replay()
    if state.get("title"):
        return
    user_message = ""
    for message in state["messages"]:
        if message.get("role") == "user" and (message.get("content") or "").strip():
            user_message = message["content"]
            break
    if not user_message.strip():
        return
    settings = load_config()
    route = route_task(settings, "title")
    routed = {**settings, "model": route.model}
    prompt = (
        "用不超过20个字给下面的用户消息起一个会话标题。"
        "只输出标题本身，不要引号，不要解释。\n"
        f"用户消息：\n{user_message}"
    )
    text = complete_text(routed, prompt)
    title = " ".join(text.split())[:40]
    if not title:
        return
    transcript.append({"type": "ai-title", "title": title})

def run_chat(user_message: str,thread_id:str=THREAD_ID) -> dict:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id},
      "recursion_limit": MAX_TURNS * 2 + 5,}
    snapshot = graph.get_state(config)
    previous = []
    if snapshot.values:
        previous = list(snapshot.values.get("messages") or [])
    before = len(previous)
    user_event = open_transcript(thread_id).append({
        "type": "message",
        "role": "user",
        "content": user_message,
    })
    result = graph.invoke(
        {"messages": previous + [{"role": "user", "content": user_message}],
            "turns": 0,
            "stop_reason": None,
            "provider_finish_reason": None,
            "pending_completion_proposals": [],
            "current_user_event_id": user_event["event_id"],
            "current_user_recorded_at": user_event["recorded_at"],
            "turn_message_start": before,
        },
        config,
    )
    reply = ""
    events = []
    stop_reason = result.get("stop_reason") or LoopStopReason.FINAL_ANSWER.value
    for message in result["messages"][before:]:
        if message.get("role") == "tool":
            events.append(message["content"])
        elif message.get("role") == "assistant":
            if stop_reason == LoopStopReason.MAX_TURNS.value or not message.get("tool_calls"):
                reply = message.get("content") or ""
    
    pending=None
    after=graph.get_state(config)
    if after.interrupts:
        pending=after.interrupts[0].value
    recorded = record_visible_messages(thread_id, result["messages"][before:])
    if not pending:
        commit_pending_done(graph, config, result, recorded["call_results"])
        try:
            write_title_if_absent(thread_id)
        except Exception as exc:
            print(f"title skipped: {exc}")
    
    return {
        "reply": reply,
        "events": events,
        "stop_reason": stop_reason,
        "turns": result.get("turns") or 0,
        "provider_finish_reason": result.get("provider_finish_reason"),
        "model_tier": result.get("model_tier") or "",
        "model_name": result.get("model_name") or "",
        "pending":pending
    }
def resume_chat(approved: bool,thread_id:str=THREAD_ID) -> dict:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id},
      "recursion_limit": MAX_TURNS * 2 + 5,}
    snapshot = graph.get_state(config)
    previous = []
    if snapshot.values:
        previous = list(snapshot.values.get("messages") or [])
    before = len(previous)
    result = graph.invoke(Command(resume=approved), config)
    reply = ""
    events = []
    stop_reason = result.get("stop_reason") or LoopStopReason.FINAL_ANSWER.value
    for message in result["messages"][before:]:
        if message.get("role") == "tool":
            events.append(message["content"])
        elif message.get("role") == "assistant":
            if stop_reason == LoopStopReason.MAX_TURNS.value or not message.get("tool_calls"):
                reply = message.get("content") or ""
    pending = None
    after = graph.get_state(config)
    if after.interrupts:
        pending = after.interrupts[0].value

    recorded = record_visible_messages(thread_id, result["messages"][before:])
    # 还在等用户同意或拒绝时先不生成标题，也不提交 done。
    if not pending:
        commit_pending_done(graph, config, result, recorded["call_results"])
        try:
            write_title_if_absent(thread_id)
        except Exception as exc:
            print(f"title skipped: {exc}")
    return {
        "reply": reply,
        "events": events,
        "stop_reason": stop_reason,
        "turns": result.get("turns") or 0,
        "provider_finish_reason": result.get("provider_finish_reason"),
        "model_tier": result.get("model_tier") or "",
        "model_name": result.get("model_name") or "",
        "pending": pending,
    }

def get_history(thread_id:str=THREAD_ID) -> list:
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    messages = []
    if snapshot.values:
        messages = snapshot.values.get("messages") or []
    visible = []
    for message in messages:
        content = message.get("content")
        if message.get("role") in ("user", "assistant") and content:
            visible.append({"role": message["role"], "content": content})
    return visible

import uuid

def session_title(thread_id: str) -> str | None:
    try:
        title = open_transcript(thread_id).replay().get("title")
    except (TranscriptCorruptionError, ValueError):
        return None
    if not title or not str(title).strip():
        return None
    return str(title).strip().splitlines()[0][:40]

def list_sessions() -> list[dict]:
    get_graph()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ).fetchall()
    finally:
        conn.close()
    sessions = []
    for (thread_id,) in rows:
        title = session_title(thread_id)
        if not title:
            title = "新会话"
            for item in get_history(thread_id):
                if item["role"] == "user" and item["content"].strip():
                    title = item["content"].strip().splitlines()[0][:40]
                    break
        sessions.append({"id": thread_id, "title": title})
    sessions.sort(key=lambda item: item["id"])
    return sessions


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]