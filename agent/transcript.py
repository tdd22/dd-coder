import re
from pathlib import Path

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "transcripts"
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 解析出文件路径
def transcript_path(thread_id: str) -> Path:
    if not THREAD_ID_RE.fullmatch(thread_id or ""):
        raise ValueError("thread_id 只能包含字母、数字、下划线和短横线")
    return TRANSCRIPTS_DIR / f"{thread_id}.jsonl"


RESERVED_ENVELOPE_FIELDS = {
    "schema_version",
    "sequence",
    "recorded_at",
    "session_id",
    "event_id",
}

class TranscriptCorruptionError(RuntimeError):
    pass

class MemorySelectionError(ValueError):
    pass

class JSONLTranscript:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.session_id = self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ignored_partial_tail = False






    # 把jsonl里面的内容读出来，每一行解析成一个字典
    # 最后一段的 JSON 如果是完整的，即使没有换行，也会留在列表里，标记保持 False。
    # 中间任何一行解析失败，抛异常，不跳过
    # 加上序号检查，中间不能断，断了说明日志文件异常，直接抛异常。
    # 检查每一行是不是属于这个文件 session_id,event_id
    def read_events(self) -> list[dict]:
        self.ignored_partial_tail = False
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if not raw:
            return []
        ended_with_newline = raw.endswith(b"\n")
        parts = raw.decode("utf-8").split("\n")
        if ended_with_newline:
            parts = parts[:-1]
        events = []
        for index, line in enumerate(parts):
            if not line.strip():
                continue
            is_last = index == len(parts) - 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if is_last and not ended_with_newline:
                    self.ignored_partial_tail = True
                    break
                raise TranscriptCorruptionError(
                    f"{self.path.name}: invalid complete JSON record"
                )
            if not isinstance(event, dict):
                raise TranscriptCorruptionError(
                    f"{self.path.name}: record must be a JSON object"
                )
            expected = len(events) + 1
            if event.get("sequence") != expected:
                raise TranscriptCorruptionError(
                    f"{self.path.name}: expected sequence {expected}, got {event.get('sequence')}"
                )
            if event.get("session_id") != self.session_id:
                raise TranscriptCorruptionError(
                    f"{self.path.name}: expected session_id {self.session_id!r}, got {event.get('session_id')!r}"
                )
            expected_event_id = f"transcript:{self.session_id}:{expected}"
            if event.get("event_id") != expected_event_id:
                raise TranscriptCorruptionError(
                    f"{self.path.name}: expected event_id {expected_event_id!r}, got {event.get('event_id')!r}"
                )
            events.append(event)
        return events

    def _next_sequence(self) -> int:
            if not self.path.exists():
                return 1
            text = self.path.read_text(encoding="utf-8")
            count = sum(1 for line in text.split("\n") if line.strip())
            return count + 1

    # 文件如果是空的，或者不存在，可以写
    # 结尾如果不是'/n'，就一个字节不改，接残缺内容的话记录就坏掉了。
    def _refuse_unterminated_tail(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise TranscriptCorruptionError(
                f"{self.path.name}: cannot append after unterminated last line"
            )


    # 检查写入content有没有涉及保留字段
    # 合法的话进行将event添加到最后一行，并且更新其他元信息
    def append(self, event: dict) -> None:
        overlap = RESERVED_ENVELOPE_FIELDS.intersection(event)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"event payload contains reserved envelope fields: {names}")
        self._refuse_unterminated_tail()
        sequence = self._next_sequence()
        envelope = {
            **event,
            "schema_version": 1,
            "sequence": sequence,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event_id": f"transcript:{self.session_id}:{sequence}",
        }
        line = (json.dumps(envelope, ensure_ascii=False) + "\n").encode()
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short transcript write: {written}/{len(line)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        return envelope


    # type是message的行，变成{role:content}
    # ai-title不进message
    # function_call和其他类型先跳过
    def replay(self) -> dict:
        messages = []
        title = None
        for event in self.read_events():
            if event.get("type") == "message":
                messages.append({
                    "role": event.get("role"),
                    "content": event.get("content") or "",
                })
            elif event.get("type") == "ai-title":
                title = event.get("title")
        return {
            "messages": messages,
            "title": title,
            "ignored_partial_tail": self.ignored_partial_tail,
        }


    # 调用时给出event_id，summary，reason
    # 这个是为memory返回candidate信息的函数
    
    def select_memory_candidate(self, event_id: str, summary: str, reason: str) -> dict:
        summary = " ".join(str(summary).split())
        reason = " ".join(str(reason).split())
        if not summary:
            raise MemorySelectionError("summary is required")
        if not reason:
            raise MemorySelectionError("reason is required")
        selected = None
        for event in self.read_events():
            if event.get("event_id") == event_id:
                selected = event
                break
        if selected is None:
            raise MemorySelectionError(f"unknown transcript event: {event_id}")
        if selected.get("type") != "message":
            raise MemorySelectionError(
                f"event type {selected.get('type')!r} is not eligible for memory selection"
            )
        captured_at = str(selected.get("recorded_at") or "").strip()
        if not captured_at:
            raise MemorySelectionError(f"transcript event {event_id} has no recorded_at")
        return {
            "source_id": selected["event_id"],
            "session_id": selected["session_id"],
            "sequence": selected["sequence"],
            "event_type": selected["type"],
            "captured_at": captured_at,
            "summary": summary,
            "reason": reason,
        }

def open_transcript(thread_id: str) -> JSONLTranscript:
    return JSONLTranscript(transcript_path(thread_id))