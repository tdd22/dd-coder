import hashlib
from enum import Enum
from pathlib import Path
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
import tempfile
from datetime import date, datetime, timedelta, timezone


MEMORY_ROOT = Path(__file__).resolve().parent.parent / "data" / "workspace-memory"
SCHEMA_VERSION = 2
DEFAULT_RETENTION_DAYS = 30
MAX_CONTEXT_FACTS = 6
MAX_FACT_CHARS = 2_000
MAX_MEMORY_KEY_CHARS = 120
MEMORY_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
class MemoryCorruptionError(RuntimeError):
    pass


class MemoryScopeError(RuntimeError):
    pass

def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)

def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryCorruptionError(f"无法读取时间: {value!r}") from exc
    return _as_utc(parsed)

def _clean_memory_key(value: str) -> str:
    key = value.strip().casefold()
    if not key:
        raise ValueError("memory_key 不能为空")
    if len(key) > MAX_MEMORY_KEY_CHARS:
        raise ValueError(f"memory_key 超过 {MAX_MEMORY_KEY_CHARS} 个字符")
    if not MEMORY_KEY_PATTERN.fullmatch(key):
        raise ValueError("memory_key 只能是小写单词，用 . _ - 分隔")
    return key


def _normal_form(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().casefold()


def _entry_key(kind: str, content: str) -> str:
    material = f"{kind}\0{_normal_form(content)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

# 事实类型
class FactKind(str, Enum):
    DECISION = "decision"
    CONVENTION = "convention"
    PITFALL = "pitfall"
    OUTCOME = "outcome"

# distill后的事实状态
class CuratedStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"



# 以后每一行日志的形状
@dataclass(frozen=True)
class MemoryFact:
    fact_id: str
    workspace_id: str
    recorded_at: str
    kind: str
    content: str
    source: str = "agent"
    importance: int = 3
    evidence: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    memory_key: str | None = None

    # 这一行json能不能变回事实
    @classmethod
    def from_dict(cls, payload: dict) -> "MemoryFact":
        if not isinstance(payload, dict):
            raise MemoryCorruptionError("记忆记录必须是对象")
        try:
            memory_key = payload.get("memory_key")
            fact = cls(
                fact_id=str(payload["fact_id"]),
                workspace_id=str(payload["workspace_id"]),
                recorded_at=str(payload["recorded_at"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                source=str(payload.get("source", "agent")),
                importance=int(payload.get("importance", 3)),
                evidence=dict(payload.get("evidence") or {}),
                schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
                memory_key=None if memory_key in (None, "") else _clean_memory_key(str(memory_key)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"记忆记录无法读取: {exc}") from exc
        if fact.kind not in {item.value for item in FactKind}:
            raise MemoryCorruptionError(f"未知的事实类型: {fact.kind}")
        if not 1 <= fact.importance <= 5:
            raise MemoryCorruptionError("importance 必须在 1 到 5 之间")
        if fact.schema_version != SCHEMA_VERSION:
            raise MemoryCorruptionError(f"不支持的记忆版本: {fact.schema_version}")
        return fact


# 定义提取的规则 
# 满30天，类型属于前三种，重要度至少是4或同样的内容至少出现2次。
# 晋升的门槛
@dataclass(frozen=True)
class DistillPolicy:
    minimum_age_days: int = DEFAULT_RETENTION_DAYS
    minimum_importance: int = 4
    repeat_threshold: int = 2
    supersession_repeat_threshold: int = 2
    stable_kinds: frozenset[str] = frozenset(
        {FactKind.DECISION.value, FactKind.CONVENTION.value, FactKind.PITFALL.value}
    )


def _group_qualifies(facts: list[MemoryFact], policy: DistillPolicy) -> bool:
    if not facts:
        return False
    importance = max(fact.importance for fact in facts)
    return importance >= policy.minimum_importance or len(facts) >= policy.repeat_threshold

def _revision_key(memory_key: str, revision: int, kind: str, content: str) -> str:
    material = f"{memory_key}\0revision:{revision}\0{kind}\0{_normal_form(content)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _group_score(facts: list[MemoryFact]) -> tuple:
    return (
        max(fact.importance for fact in facts),
        len({fact.fact_id for fact in facts}),
        max(_parse_timestamp(fact.recorded_at) for fact in facts),
    )


@dataclass(frozen=True)
class DistillReport:
    created: int = 0
    updated: int = 0
    superseded: int = 0
    tied: tuple[str, ...] = ()


_DIRECTORY_FSYNC_SUPPORTED = os.name != "nt"

# 目录同步
def _fsync_directory(path: Path) -> None:
    if not _DIRECTORY_FSYNC_SUPPORTED:
        return
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)




# 是晋升之后留在curated.json里的一条长期记忆
@dataclass
class CuratedMemoryEntry:
    key: str
    kind: str
    content: str
    first_seen: str
    last_seen: str
    evidence_ids: list[str]
    occurrences: int
    memory_key: str | None = None
    revision: int = 1
    status: str = CuratedStatus.ACTIVE.value
    supersedes: str | None = None
    superseded_by: str | None = None
    @classmethod
    def from_dict(cls, payload: dict) -> "CuratedMemoryEntry":
        if not isinstance(payload, dict):
            raise MemoryCorruptionError("策展记录必须是对象")
        try:
            evidence = payload.get("evidence_ids", [])
            evidence_ids = [str(item) for item in evidence]
            memory_key = payload.get("memory_key")
            if memory_key in (None, ""):
                memory_key = None
            else:
                memory_key = _clean_memory_key(str(memory_key))
            revision = int(payload.get("revision", 1))
            status = str(payload.get("status", CuratedStatus.ACTIVE.value))
            if revision < 1:
                raise ValueError("revision 必须从 1 开始")
            if status not in {item.value for item in CuratedStatus}:
                raise ValueError(f"未知的策展状态: {status}")
            entry = cls(
                key=str(payload["key"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                first_seen=str(payload["first_seen"]),
                last_seen=str(payload["last_seen"]),
                evidence_ids=evidence_ids,
                occurrences=int(payload.get("occurrences", len(evidence_ids))),
                memory_key=memory_key,
                revision=revision,
                status=status,
                supersedes=None if payload.get("supersedes") in (None, "") else str(payload["supersedes"]),
                superseded_by=None if payload.get("superseded_by") in (None, "") else str(payload["superseded_by"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"策展记录无法读取: {exc}") from exc
        if entry.kind not in {item.value for item in FactKind}:
            raise MemoryCorruptionError(f"未知的事实类型: {entry.kind}")
        return entry


class WorkspaceMemory:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace_id = hashlib.sha256(
            str(self.workspace).encode("utf-8")
        ).hexdigest()[:16]
        self.memory_dir = MEMORY_ROOT / self.workspace_id
        self.daily_dir = self.memory_dir / "daily"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.curated_file = self.memory_dir / "curated.json"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
    def daily_log_path(self, day: date) -> Path:
        return self.daily_dir / f"{day.isoformat()}.jsonl"
    def today_log_path(self) -> Path:
        return self.daily_log_path(datetime.now(timezone.utc).date())

    # 把一条已经校验过的事实，变成当天日志里的一行
    # 模型以后调用write_memory也要调用这里
    # 蒸馏只读这里写入的文件
    # 1.整理内容 2.检查类型和重要度 3.整理memory_key 4.转成一条memoryFact 5写成一行json并追加
    def append_daily_log(
        self,
        content: str,
        *,
        kind: str | FactKind = FactKind.OUTCOME,
        importance: int = 3,
        source: str = "agent",
        evidence: dict | None = None,
        memory_key: str | None = None,
        recorded_at: datetime | None = None,
        fact_id: str | None = None,
    ) -> MemoryFact:
        text = re.sub(r"\s+", " ", content).strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        if len(text) > MAX_FACT_CHARS:
            raise ValueError(f"记忆内容超过 {MAX_FACT_CHARS} 个字符")
        if not 1 <= importance <= 5:
            raise ValueError("importance 必须在 1 到 5 之间")
        kind_value = kind.value if isinstance(kind, FactKind) else str(kind)
        if kind_value not in {item.value for item in FactKind}:
            raise ValueError(f"未知的事实类型: {kind_value}")
        timestamp = _as_utc(recorded_at)
        fact = MemoryFact(
            fact_id=fact_id or uuid.uuid4().hex,
            workspace_id=self.workspace_id,
            recorded_at=timestamp.isoformat().replace("+00:00", "Z"),
            kind=kind_value,
            content=text,
            memory_key=None if memory_key is None else _clean_memory_key(memory_key),
            source=source,
            importance=importance,
            evidence=dict(evidence or {}),
        )
        encoded = (json.dumps(asdict(fact), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        path = self.daily_log_path(timestamp.date())
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        try:
            written = os.write(fd, encoded)
            if written != len(encoded):
                raise OSError(f"记忆写入不完整: {written}/{len(encoded)} 字节")
            os.fsync(fd)
        finally:
            os.close(fd)
        return fact
    

    # 列出来所有的日志文件
    def list_logs(self) -> list[Path]:
        return sorted(self.daily_dir.glob("????-??-??.jsonl"))

    # 读某一个日志文件
    def _read_log(self, path: Path) -> list[MemoryFact]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        if not raw:
            return []
        ended_with_newline = raw.endswith(b"\n")
        parts = raw.decode("utf-8").split("\n")
        if ended_with_newline:
            parts = parts[:-1]
        facts = []
        for index, line in enumerate(parts):
            if not line.strip():
                continue
            is_last = index == len(parts) - 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if is_last and not ended_with_newline:
                    break
                raise MemoryCorruptionError(f"{path.name} 里有无法解析的完整行")
            fact = MemoryFact.from_dict(payload)
            if fact.workspace_id != self.workspace_id:
                raise MemoryScopeError(f"{path.name} 属于另一个工作区")
            facts.append(fact)
        return facts

    # 读某一天
    def read_daily_facts(self, day: date | None = None) -> list[MemoryFact]:
        target = day or datetime.now(timezone.utc).date()
        return self._read_log(self.daily_log_path(target))
    
    # 读全部
    def read_all_facts(self) -> list[MemoryFact]:
        facts = [fact for path in self.list_logs() for fact in self._read_log(path)]
        return sorted(facts, key=lambda item: (item.recorded_at, item.fact_id))
    
    # 安全的整文件替换。
    # 蒸馏以后每次都会重写curated.json和memory.md
    # 如果用普通方式直接打开目标文件来写，进程写到一半退出，磁盘上就会留下半个文件，原本那份完整内容也没了

    # 1.在同一个目录下新建一个临时文件 2.把完整内容写进临时文件，flush再fsync 3.os.replace用临时文件换成目标文件 
    # 4.能同步目录的系统，再fsync一次目录 5.中途失败就删掉临时文件
    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    # curated.json是长期记忆的机器真相。蒸馏每次开始的时候读它，才知道哪些fact_id已经晋升过。
    def _load_curated(self) -> list[CuratedMemoryEntry]:
        if not self.curated_file.exists():
            return []
        try:
            payload = json.loads(self.curated_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryCorruptionError("curated.json 无法读取") from exc
        if not isinstance(payload, dict):
            raise MemoryCorruptionError("curated.json 必须是对象")
        if payload.get("workspace_id") != self.workspace_id:
            raise MemoryScopeError("curated.json 属于另一个工作区")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise MemoryCorruptionError("不支持的策展版本")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise MemoryCorruptionError("curated.json 缺少条目列表")
        return [CuratedMemoryEntry.from_dict(item) for item in entries]

    def _save_curated(self, entries: list[CuratedMemoryEntry]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "entries": [asdict(entry) for entry in entries],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write_text(self.curated_file, text)
    
    # 筛选，日志文件里已经年龄，类型，是否已经用过
    # 过了这三关才能进档案筛选
    def _eligible_facts(
        self,
        policy: DistillPolicy | None = None,
        as_of: datetime | None = None,
    ) -> list[MemoryFact]:
        active_policy = policy or DistillPolicy()
        cutoff = _as_utc(as_of) - timedelta(days=active_policy.minimum_age_days)
        used_ids = {
            evidence_id
            for entry in self._load_curated()
            for evidence_id in entry.evidence_ids
        }
        chosen = []
        for fact in self.read_all_facts():
            if _parse_timestamp(fact.recorded_at) >= cutoff:
                continue
            if fact.kind not in active_policy.stable_kinds:
                continue
            if fact.fact_id in used_ids:
                continue
            chosen.append(fact)
        return chosen


    # 只晋升没有memory_key的事实，会保留档案里已经有的其他条目
    # 没有memory_key的新旧事实不会发生覆盖
    # 有memory_key的新旧事实可能发生覆盖

    def _promote_unkeyed(
        self,
        policy: DistillPolicy | None = None,
        as_of: datetime | None = None,
    ) -> list[CuratedMemoryEntry]:
        active_policy = policy or DistillPolicy()
        by_key = {entry.key: entry for entry in self._load_curated()}
        groups: dict[str, list[MemoryFact]] = {}
        for fact in self._eligible_facts(active_policy, as_of):
            if fact.memory_key is not None:
                continue
            key = _entry_key(fact.kind, fact.content)
            groups.setdefault(key, []).append(fact)
        changed = False
        for key in sorted(groups):
            facts = list({fact.fact_id: fact for fact in groups[key]}.values())
            current = by_key.get(key)
            if current is None and not _group_qualifies(facts, active_policy):
                continue
            timestamps = sorted(fact.recorded_at for fact in facts)
            new_ids = {fact.fact_id for fact in facts}
            if current is None:
                chosen = min(
                    facts,
                    key=lambda fact: (
                        -fact.importance,
                        _parse_timestamp(fact.recorded_at),
                        fact.fact_id,
                    ),
                )
                by_key[key] = CuratedMemoryEntry(
                    key=key,
                    kind=chosen.kind,
                    content=chosen.content,
                    first_seen=timestamps[0],
                    last_seen=timestamps[-1],
                    evidence_ids=sorted(new_ids),
                    occurrences=len(new_ids),
                )
            else:
                merged = sorted(set(current.evidence_ids) | new_ids)
                current.first_seen = min(current.first_seen, timestamps[0])
                current.last_seen = max(current.last_seen, timestamps[-1])
                current.evidence_ids = merged
                current.occurrences = len(merged)
            changed = True
        saved = list(by_key.values())
        if changed:
            self._save_curated(saved)
        return saved


    def _promote_keyed(
        self,
        policy: DistillPolicy | None = None,
        as_of: datetime | None = None,
    ) -> DistillReport:
        active_policy = policy or DistillPolicy()
        by_key = {entry.key: entry for entry in self._load_curated()}
        active_by_question: dict[str, CuratedMemoryEntry] = {}
        for entry in by_key.values():
            if entry.memory_key is None or entry.status != CuratedStatus.ACTIVE.value:
                continue
            if entry.memory_key in active_by_question:
                raise MemoryCorruptionError(f"{entry.memory_key} 有两条当前修订")
            active_by_question[entry.memory_key] = entry

        grouped: dict[str, dict[tuple[str, str], list[MemoryFact]]] = {}
        for fact in self._eligible_facts(active_policy, as_of):
            if fact.memory_key is None:
                continue
            signature = (fact.kind, _normal_form(fact.content))
            grouped.setdefault(fact.memory_key, {}).setdefault(signature, []).append(fact)

        created = updated = superseded = 0
        tied: list[str] = []
        changed = False
        for memory_key in sorted(grouped):
            proposals = {
                signature: list({fact.fact_id: fact for fact in facts}.values())
                for signature, facts in grouped[memory_key].items()
            }
            current = active_by_question.get(memory_key)
            if current is not None:
                same_answer = proposals.pop((current.kind, _normal_form(current.content)), None)
                if same_answer:
                    timestamps = sorted(fact.recorded_at for fact in same_answer)
                    merged = sorted(set(current.evidence_ids) | {fact.fact_id for fact in same_answer})
                    current.first_seen = min(current.first_seen, timestamps[0])
                    current.last_seen = max(current.last_seen, timestamps[-1])
                    current.evidence_ids = merged
                    current.occurrences = len(merged)
                    updated += 1
                    changed = True

            challengers = []
            for facts in proposals.values():
                if not _group_qualifies(facts, active_policy):
                    continue
                if current is not None:
                    if len(facts) < active_policy.supersession_repeat_threshold:
                        continue
                    newest = max(_parse_timestamp(fact.recorded_at) for fact in facts)
                    if newest <= _parse_timestamp(current.last_seen):
                        continue
                challengers.append(facts)
            if not challengers:
                continue
            challengers.sort(key=_group_score, reverse=True)
            if len(challengers) >= 2 and _group_score(challengers[0]) == _group_score(challengers[1]):
                tied.append(memory_key)
                continue

            winner = challengers[0]
            chosen = min(
                winner,
                key=lambda fact: (
                    -fact.importance,
                    _parse_timestamp(fact.recorded_at),
                    fact.fact_id,
                ),
            )
            timestamps = sorted(fact.recorded_at for fact in winner)
            revision = 1 if current is None else current.revision + 1
            entry = CuratedMemoryEntry(
                key=_revision_key(memory_key, revision, chosen.kind, chosen.content),
                kind=chosen.kind,
                content=chosen.content,
                first_seen=timestamps[0],
                last_seen=timestamps[-1],
                evidence_ids=sorted(fact.fact_id for fact in winner),
                occurrences=len(winner),
                memory_key=memory_key,
                revision=revision,
                status=CuratedStatus.ACTIVE.value,
                supersedes=None if current is None else current.key,
            )
            if current is not None:
                current.status = CuratedStatus.SUPERSEDED.value
                current.superseded_by = entry.key
                superseded += 1
            by_key[entry.key] = entry
            active_by_question[memory_key] = entry
            created += 1
            changed = True

        if changed:
            self._save_curated(list(by_key.values()))
        return DistillReport(created, updated, superseded, tuple(tied))


    # status为active的条目
    # 分好类型
    def _render_memory(self, entries: list[CuratedMemoryEntry]) -> str:
        labels = {
            FactKind.DECISION.value: "决策",
            FactKind.CONVENTION.value: "约定",
            FactKind.PITFALL.value: "踩坑",
        }
        lines = ["# 工作区记忆", ""]
        found = False
        for kind in (FactKind.DECISION.value, FactKind.CONVENTION.value, FactKind.PITFALL.value):
            items = [
                entry for entry in entries
                if entry.status == CuratedStatus.ACTIVE.value and entry.kind == kind
            ]
            if not items:
                continue
            found = True
            items.sort(key=lambda item: (item.memory_key or "", item.content, item.key))
            lines.extend([f"## {labels[kind]}", ""])
            for item in items:
                lines.append(f"- {item.content}")
            lines.append("")
        if not found:
            return ""
        return "\n".join(lines).rstrip() + "\n"

    def read_memory_md(self) -> str:
        rendered = self._render_memory(self._load_curated())
        current = self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else None
        if current == rendered or (current is None and rendered == ""):
            return rendered
        self._atomic_write_text(self.memory_file, rendered)
        return rendered
    
    def distill(
        self,
        policy: DistillPolicy | None = None,
        as_of: datetime | None = None,
    ) -> DistillReport:
        self._promote_unkeyed(policy, as_of)
        report = self._promote_keyed(policy, as_of)
        self.read_memory_md()
        return report


    # 抄本里的当前答案
    # 最近最多6条还没晋升，且没有memory_key的日志。outcome和还不满30的约定会出现在这里
    def get_context_for_agent(self, recent_limit: int = MAX_CONTEXT_FACTS) -> str:
        parts = []
        curated = self.read_memory_md().strip()
        if curated:
            parts.append(curated)
        used_ids = {
            evidence_id
            for entry in self._load_curated()
            for evidence_id in entry.evidence_ids
        }
        recent = [
            fact
            for fact in self.read_all_facts()
            if fact.memory_key is None and fact.fact_id not in used_ids
        ]
        if recent_limit > 0:
            recent = recent[-recent_limit:]
        else:
            recent = []
        if recent:
            lines = ["# 最近的工作区事实", ""]
            for fact in recent:
                lines.append(f"- [{fact.kind}] {fact.content} ({fact.recorded_at[:10]})")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)


