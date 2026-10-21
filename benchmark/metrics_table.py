import csv
import json
import os
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "benchmark" / "outputs"
TRANSCRIPTS = ROOT / "data" / "transcripts"
TABLE_PATH = OUTPUTS / "metrics.csv"

COLUMNS = [
    "thread_id",
    "停止原因",
    "代理轮数",
    "工具次数",
    "重复命令",
    "大结果外置",
    "命令失败",
    "输入token",
    "输出token",
    "耗时",
]

EXTERNALIZED_MARK = "工具结果过大，已写入磁盘"
EXIT_CODE = re.compile(r"exit_code:\s*(-?\d+)")
INPUT_KEYS = ("input", "prompt_tokens", "input_tokens")
OUTPUT_KEYS = ("output", "completion_tokens", "output_tokens")


def write_run_meta(directory: Path, meta: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "meta.json"
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def discover_runs(outputs: Path, transcripts: Path) -> list[dict]:
    found: dict[str, dict] = {}
    runs = outputs / "runs"
    if runs.is_dir():
        for path in sorted(runs.glob("*/meta.json")):
            meta = json.loads(path.read_text(encoding="utf-8"))
            thread_id = meta.get("thread_id")
            if thread_id:
                found[thread_id] = meta
    if outputs.is_dir():
        for path in sorted(outputs.glob("*.json")):
            _remember_legacy(found, path)
    if transcripts.is_dir():
        for path in sorted(transcripts.glob("swebench-*.jsonl")):
            thread_id = path.stem
            if thread_id not in found:
                found[thread_id] = {"thread_id": thread_id}
    return list(found.values())


def _remember_legacy(found: dict[str, dict], path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    thread_id = payload.get("thread_id")
    if not thread_id or thread_id in found:
        return
    found[thread_id] = {
        "thread_id": thread_id,
        "instance_id": payload.get("instance_id") or "",
        "stop_reason": payload.get("stop_reason") or "",
        "turns": payload.get("turns"),
    }


def load_events(path: Path) -> list[dict] | None:
    if not path.is_file():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def resolve_call(event: dict) -> tuple[str, dict]:
    name = event.get("name") or ""
    arguments = event.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "defer_execute":
        inner = arguments.get("name")
        inner_arguments = arguments.get("arguments")
        if isinstance(inner, str) and inner:
            if not isinstance(inner_arguments, dict):
                inner_arguments = {}
            return inner, inner_arguments
    return name, arguments


def summarize_transcript(events: list[dict]) -> dict:
    counts: Counter[str] = Counter()
    commands: Counter[tuple] = Counter()
    results: dict[str, str] = {}
    externalized = 0
    for event in events:
        if event.get("type") == "function_call_result":
            content = (event.get("output") or {}).get("content") or ""
            results[event.get("callId") or ""] = content
            if EXTERNALIZED_MARK in content:
                externalized += 1
    failures = 0
    for event in events:
        if event.get("type") != "function_call":
            continue
        name, arguments = resolve_call(event)
        counts[name] += 1
        if name != "run_command":
            continue
        command = arguments.get("command")
        if isinstance(command, list):
            commands[tuple(command)] += 1
        content = results.get(event.get("callId") or "", "")
        if _command_failed(content):
            failures += 1
    repeated = sum(count - 1 for count in commands.values() if count >= 2)
    return {
        "tool_counts": dict(counts),
        "repeated_commands": repeated,
        "externalized": externalized,
        "command_failures": failures,
    }


def render_tool_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={counts[name]}" for name in sorted(counts))


def _command_failed(content: str) -> bool:
    match = EXIT_CODE.search(content)
    if match:
        return int(match.group(1)) != 0
    return "工具执行失败" in content


def _field(item: object, key: str):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _usage_number(details: object, keys: tuple[str, ...]) -> int:
    if not isinstance(details, dict):
        return 0
    for key in keys:
        value = details.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return 0


def _duration_seconds(item: object) -> float | None:
    latency = _field(item, "latency")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
        return float(latency)
    start = _field(item, "start_time")
    end = _field(item, "end_time")
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def summarize_observations(observations: list) -> dict:
    if not observations:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "duration_seconds": None,
            "agent_calls": None,
        }
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    agent_calls = 0
    duration = 0.0
    saw_duration = False
    for item in observations:
        if _field(item, "name") == "agent":
            agent_calls += 1
        if str(_field(item, "type") or "").upper() == "GENERATION":
            details = _field(item, "usage_details")
            if isinstance(details, dict) and any(
                key in details for key in INPUT_KEYS + OUTPUT_KEYS
            ):
                saw_usage = True
                input_tokens += _usage_number(details, INPUT_KEYS)
                output_tokens += _usage_number(details, OUTPUT_KEYS)
        if _field(item, "name") == "run_chat":
            seconds = _duration_seconds(item)
            if seconds is not None:
                saw_duration = True
                duration += seconds
    return {
        "input_tokens": input_tokens if saw_usage else None,
        "output_tokens": output_tokens if saw_usage else None,
        "duration_seconds": round(duration, 1) if saw_duration else None,
        "agent_calls": agent_calls,
    }


def build_row(meta: dict, events: list[dict] | None, observations: list | None) -> dict:
    remote = summarize_observations(observations or [])
    turns = meta.get("turns")
    if turns is None and remote["agent_calls"] is not None:
        turns = remote["agent_calls"]
    if events is None:
        tools = ""
        repeated = ""
        externalized = ""
        failures = ""
    else:
        summary = summarize_transcript(events)
        tools = render_tool_counts(summary["tool_counts"])
        repeated = summary["repeated_commands"]
        externalized = summary["externalized"]
        failures = summary["command_failures"]
    return {
        "thread_id": meta.get("thread_id") or "",
        "停止原因": meta.get("stop_reason") or "",
        "代理轮数": "" if turns is None else turns,
        "工具次数": tools,
        "重复命令": repeated,
        "大结果外置": externalized,
        "命令失败": failures,
        "输入token": _blank(remote["input_tokens"]),
        "输出token": _blank(remote["output_tokens"]),
        "耗时": _blank(remote["duration_seconds"]),
    }


def _blank(value: object) -> object:
    return "" if value is None else value


def write_table(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def fetch_observations(thread_id: str) -> list | None:
    if not os.getenv("LANGFUSE_PUBLIC_KEY", "").strip() or not os.getenv(
        "LANGFUSE_SECRET_KEY", ""
    ).strip():
        return None
    from langfuse import get_client

    client = get_client()
    observations = []
    cursor = None
    seen: set[str] = set()
    for _ in range(20):
        page = client.api.observations.get_many(
            session_id=thread_id,
            limit=100,
            cursor=cursor,
            fields="core,basic,usage,metrics",
        )
        observations.extend(page.data)
        cursor = page.meta.cursor
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return observations


def collect_rows(outputs: Path, transcripts: Path, fetch=fetch_observations) -> list[dict]:
    rows = []
    for meta in discover_runs(outputs, transcripts):
        thread_id = meta["thread_id"]
        events = load_events(transcripts / f"{thread_id}.jsonl")
        try:
            observations = fetch(thread_id)
        except Exception as exc:
            print(f"Langfuse 读取失败 {thread_id}: {exc}")
            observations = None
        if observations is not None and not observations:
            print(f"Langfuse 还没有这条 session: {thread_id}")
        rows.append(build_row(meta, events, observations))
    return rows


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    rows = collect_rows(OUTPUTS, TRANSCRIPTS)
    if not rows:
        raise SystemExit("没有找到评测记录。先跑一次 benchmark/run_swebench_lite.py。")
    write_table(TABLE_PATH, rows)
    missing_reason = sum(1 for row in rows if not row["停止原因"])
    missing_usage = sum(1 for row in rows if row["输入token"] == "")
    print(f"写出 {len(rows)} 行: {TABLE_PATH}")
    if missing_reason:
        print(f"{missing_reason} 行没有停止原因。新的评测会写到 benchmark/outputs/runs/{{thread_id}}/meta.json。")
    if missing_usage:
        print(f"{missing_usage} 行没有 token。确认 Langfuse 里能看到对应 session 后再跑一次本脚本。")


if __name__ == "__main__":
    main()
