import hashlib
from pathlib import Path
import os
import tempfile
import re
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum





USER_MEMORY_ROOT = Path(__file__).resolve().parent.parent / "data" / "user-memory"
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, SCHEMA_VERSION})
class WriteStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    DELETED = "deleted"


class StalePreferenceUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreferenceWrite:
    status: WriteStatus
    key: str
    previous_value: str | None
    current_value: str | None
    revision: int


@dataclass(frozen=True)
class ProfileWrite:
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]



@dataclass(frozen=True)
class Preference:
    key: str
    value: str
    source: str
    updated_at: str
    revision: int = 1
    expires_at: str | None = None
    source_event_id: str | None = None
    # 在内存里拼一条偏好记录，把key，值，来源，时间和修订号放在一起
    # 返回一条Prefrence,不写盘
    # 临时偏好需要expires_at，到点后记录还在，只是以后不进提示。
    @classmethod
    def from_dict(cls, payload: dict) -> "Preference":
        if not isinstance(payload, dict):
            raise UserMemoryValidationError("偏好记录必须是对象")
        try:
            revision = int(payload.get("revision", 1))
        except (TypeError, ValueError) as exc:
            raise UserMemoryValidationError(f"偏好记录无法读取: {exc}") from exc
        if revision < 1:
            raise UserMemoryValidationError("revision 必须从 1 开始")
        key = payload.get("key")
        value = payload.get("value")
        source = payload.get("source", "explicit")
        raw_updated_at = payload.get("updated_at")
        if not all(isinstance(item, str) for item in (key, value, source, raw_updated_at)):
            raise UserMemoryValidationError("偏好的 key、值、source 和 updated_at 必须是字符串")
        updated_at = _normalize_timestamp(raw_updated_at, field_name="updated_at")
        raw_expiry = payload.get("expires_at")
        expires_at = None
        if raw_expiry not in (None, ""):
            if not isinstance(raw_expiry, str):
                raise UserMemoryValidationError("expires_at 必须是字符串")
            expires_at = _normalize_timestamp(raw_expiry, field_name="expires_at")
            expiry_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if expiry_at <= updated:
                raise UserMemoryValidationError("expires_at 必须晚于 updated_at")
        return cls(
            key=_preference_key(key),
            value=_clean_text(value, field_name="偏好的值", max_chars=4_000),
            source=_clean_text(source, field_name="source", max_chars=100),
            updated_at=updated_at,
            revision=revision,
            expires_at=expires_at,
            source_event_id=_optional_source_event_id(payload.get("source_event_id")),
        )
    def is_active(self, *, as_of: datetime | None = None) -> bool:
        if self.expires_at is None:
            return True
        current = as_of or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise UserMemoryValidationError("as_of 必须带时区")
        expiry_at = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return current.astimezone(timezone.utc) < expiry_at


@dataclass(frozen=True)
class DeletedPreference:
    key: str
    deleted_at: str
    revision: int
    source: str

    @classmethod
    def from_dict(cls, payload: dict) -> "DeletedPreference":
        if not isinstance(payload, dict):
            raise UserMemoryValidationError("删除记录必须是对象")
        try:
            revision = int(payload["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UserMemoryValidationError(f"删除记录无法读取: {exc}") from exc
        if revision < 1:
            raise UserMemoryValidationError("revision 必须从 1 开始")
        key = payload.get("key")
        deleted_at = payload.get("deleted_at")
        source = payload.get("source")
        if not all(isinstance(item, str) for item in (key, deleted_at, source)):
            raise UserMemoryValidationError("删除记录的 key、deleted_at 和 source 必须是字符串")
        return cls(
            key=_preference_key(key),
            deleted_at=_normalize_timestamp(deleted_at, field_name="deleted_at"),
            revision=revision,
            source=_clean_text(source, field_name="source", max_chars=100),
        )

class UserMemoryValidationError(RuntimeError):
    pass

class UserScopeError(RuntimeError):
    pass

def _scope_id(user_id: str) -> str:
    normalized = user_id.strip().casefold()
    if not normalized:
        raise UserMemoryValidationError("user_id 不能为空")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

PROFILE_FIELDS = frozenset(
    {"name", "call_them", "pronouns", "city", "timezone", "notes"}
)

PREFERENCE_KEY = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SOURCE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

# 统一好偏好的key格式
def _preference_key(value: str) -> str:
    key = value.strip().casefold()
    if not PREFERENCE_KEY.fullmatch(key):
        raise UserMemoryValidationError("偏好的 key 只能是小写单词，用 . _ - 分隔")
    return key


def _optional_source_event_id(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise UserMemoryValidationError("source_event_id 必须是字符串")
    event_id = _clean_text(value, field_name="source_event_id", max_chars=200)
    if not SOURCE_EVENT_ID.fullmatch(event_id):
        raise UserMemoryValidationError("source_event_id 必须是事件编号")
    return event_id


def _render_preferences(preferences: list[Preference]) -> str:
    if not preferences:
        return ""
    lines = ["# Explicit User Preferences", ""]
    for item in preferences:
        expiry = f" (expires {item.expires_at})" if item.expires_at else ""
        lines.append(f"- `{item.key}`: {item.value}{expiry}")
    return "\n".join(lines) + "\n"

# 统一好时间格式
def _normalize_timestamp(value: str, *, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserMemoryValidationError(f"{field_name} 必须是带时区的时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UserMemoryValidationError(f"{field_name} 必须带时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 做写入内容的整理
def _clean_text(value: str, *, field_name: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        raise UserMemoryValidationError(f"{field_name} 不能为空")
    if len(text) > max_chars:
        raise UserMemoryValidationError(f"{field_name} 超过 {max_chars} 个字符")
    return text

# 写入内容的检查和整理
def _profile_text(key: str, value: object) -> str:
    if key not in PROFILE_FIELDS:
        raise UserMemoryValidationError(f"未知的档案字段: {key}")
    if not isinstance(value, str):
        raise UserMemoryValidationError(f"{key} 必须是字符串")
    return _clean_text(value, field_name=key, max_chars=1_000)

class UserMemory:
    def __init__(self, base_dir: Path | None = None, *, user_id: str = "local-user"):
        self.root_dir = Path(base_dir or USER_MEMORY_ROOT).expanduser().resolve()
        self.user_id = user_id.strip()
        self.scope_id = _scope_id(self.user_id)
        self.base_dir = self.root_dir / "users" / self.scope_id
        self.persona_dir = self.base_dir / "persona"
        self.profile_path = self.base_dir / "profile.json"
        self.preferences_path = self.base_dir / "preferences.json"
        self.memory_path = self.base_dir / "MEMORY.md"
        self.user_path = self.persona_dir / "user.md"
        self.persona_dir.mkdir(parents=True, exist_ok=True)

    # 原子写，保证内容完整写进文件
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
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    
    # 同一个函数，调用时传入哪个路径，就读哪个文件。
    def _read_scoped_json(self, path: Path, *, default_key: str) -> dict:
        if not path.exists():
            empty = {} if default_key == "profile" else []
            return {
                "schema_version": SCHEMA_VERSION,
                "user_scope": self.scope_id,
                default_key: empty,
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UserMemoryValidationError(f"无法读取 {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise UserMemoryValidationError(f"{path.name} 的根必须是对象")
        if payload.get("user_scope") != self.scope_id:
            raise UserScopeError(f"{path.name} 属于另一个用户")
        try:
            schema_version = int(payload.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise UserMemoryValidationError(f"{path.name} 的版本无法读取") from exc
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UserMemoryValidationError(f"{path.name} 的版本 {schema_version} 不支持")
        return payload

    # 把一个键写回对应的文件
    def _write_scoped_json(self, path: Path, key: str, value: object) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "user_scope": self.scope_id,
            key: value,
        }
        self._atomic_write_text(
            path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )
    
    # 用read_scope_json读出来，然后在这个函数里做特定的处理
    def read_profile(self) -> dict[str, str]:
        payload = self._read_scoped_json(self.profile_path, default_key="profile")
        profile = payload.get("profile", {})
        if not isinstance(profile, dict):
            raise UserMemoryValidationError("profile 必须是对象")
        return {key: _profile_text(key, value) for key, value in profile.items()}


    # 1、有未知字段直接报错 2、read_file得到原内容 3、逐个看请求，放进changed和unchanged 4、字段不合法抛错 5、写到磁盘 6、返回更新结果
    def update_profile(self, changes: dict[str, object]) -> ProfileWrite:
        unknown = sorted(set(changes) - PROFILE_FIELDS)
        if unknown:
            raise UserMemoryValidationError(f"未知的档案字段: {', '.join(unknown)}")
        profile = self.read_profile()
        changed: list[str] = []
        unchanged: list[str] = []
        for key, raw_value in changes.items():
            if raw_value is None:
                if key in profile:
                    del profile[key]
                    changed.append(key)
                else:
                    unchanged.append(key)
                continue
            value = _profile_text(key, raw_value)
            if profile.get(key) == value:
                unchanged.append(key)
            else:
                profile[key] = value
                changed.append(key)
        if changed:
            self._write_scoped_json(self.profile_path, "profile", profile)
        self._write_profile_projection(profile)
        return ProfileWrite(tuple(sorted(changed)), tuple(sorted(unchanged)))

    # 把给的profile文件写进user.md文档里
    def _write_profile_projection(self, profile: dict[str, str]) -> None:
        labels = {
            "name": "Name",
            "call_them": "Call them",
            "pronouns": "Pronouns",
            "city": "City",
            "timezone": "Timezone",
            "notes": "Notes",
        }
        lines = ["# User", ""]
        lines.extend(
            f"{labels[key]}: {profile[key]}" for key in labels if profile.get(key)
        )
        expected = "\n".join(lines).rstrip() + "\n"
        current = self.user_path.read_text(encoding="utf-8") if self.user_path.exists() else ""
        if current != expected:
            self._atomic_write_text(self.user_path, expected)
    
    # 将文件里的内容转换成对象类，到内存里 
    def list_preferences(self) -> list[Preference]:
        payload = self._read_scoped_json(self.preferences_path, default_key="preferences")
        records = payload.get("preferences", [])
        if not isinstance(records, list):
            raise UserMemoryValidationError("preferences 必须是数组")
        preferences = [Preference.from_dict(item) for item in records]
        if len({item.key for item in preferences}) != len(preferences):
            raise UserMemoryValidationError("偏好的 key 重复")
        return sorted(preferences, key=lambda item: item.key)
    
    def list_deleted_preferences(self) -> list[DeletedPreference]:
        payload = self._read_scoped_json(self.preferences_path, default_key="preferences")
        records = payload.get("deleted", [])
        if not isinstance(records, list):
            raise UserMemoryValidationError("deleted 必须是数组")
        deleted = [DeletedPreference.from_dict(item) for item in records]
        if len({item.key for item in deleted}) != len(deleted):
            raise UserMemoryValidationError("删除记录的 key 重复")
        live = {item.key for item in self.list_preferences()}
        if live & {item.key for item in deleted}:
            raise UserMemoryValidationError("偏好的 key 同时在活记录和删除记录里")
        return sorted(deleted, key=lambda item: item.key)

    # 把改过的偏好列表写回去，并生产memory.md
    def _store_preferences(
        self,
        preferences: list[Preference],
        deleted: list[DeletedPreference] | None = None,
    ) -> None:
        if deleted is None:
            deleted = self.list_deleted_preferences()
        ordered = sorted(preferences, key=lambda item: item.key)
        tombstones = sorted(deleted, key=lambda item: item.key)
        if {item.key for item in ordered} & {item.key for item in tombstones}:
            raise UserMemoryValidationError("偏好的 key 同时在活记录和删除记录里")
        records = [
            {
                "key": item.key,
                "value": item.value,
                "source": item.source,
                "updated_at": item.updated_at,
                "revision": item.revision,
                "expires_at": item.expires_at,
                "source_event_id": item.source_event_id,
            }
            for item in ordered
        ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "user_scope": self.scope_id,
            "preferences": records,
        }
        if tombstones:
            payload["deleted"] = [
                {
                    "key": item.key,
                    "deleted_at": item.deleted_at,
                    "revision": item.revision,
                    "source": item.source,
                }
                for item in tombstones
            ]
        self._atomic_write_text(
            self.preferences_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        active = [item for item in ordered if item.is_active()]
        self._atomic_write_text(self.memory_path, _render_preferences(active))

    # 按key创建，原样重试或换成新值
   
    def set_preference(
        self,
        key: str,
        value: str,
        *,
        source: str = "explicit",
        expires_at: str | None = None,
        updated_at: str | None = None,
        source_event_id: str | None = None,
    ) -> PreferenceWrite:
        normalized_key = _preference_key(key)
        normalized_value = _clean_text(value, field_name="偏好的值", max_chars=4_000)
        normalized_source = _clean_text(source, field_name="source", max_chars=100)
        if expires_at in (None, ""):
            normalized_expires_at = None
        else:
            if not isinstance(expires_at, str):
                raise UserMemoryValidationError("expires_at 必须是字符串")
            normalized_expires_at = _normalize_timestamp(expires_at, field_name="expires_at")
        explicit_updated_at = updated_at is not None
        if updated_at is None:
            moment = datetime.now(timezone.utc)
            normalized_updated_at = moment.isoformat().replace("+00:00", "Z")
        else:
            normalized_updated_at = _normalize_timestamp(updated_at, field_name="updated_at")
            moment = datetime.fromisoformat(normalized_updated_at.replace("Z", "+00:00"))
        normalized_event_id = _optional_source_event_id(source_event_id)
        current = {item.key: item for item in self.list_preferences()}
        deleted = {item.key: item for item in self.list_deleted_preferences()}
        previous = current.get(normalized_key)
        tombstone = deleted.pop(normalized_key, None)
        if tombstone is not None and moment <= datetime.fromisoformat(
            tombstone.deleted_at.replace("Z", "+00:00")
        ):
            raise StalePreferenceUpdateError(f"偏好 {normalized_key} 已在这条证据之后删除")
        newer_confirmation = (
            previous is not None
            and tombstone is None
            and normalized_event_id is None
            and explicit_updated_at
            and moment > datetime.fromisoformat(previous.updated_at.replace("Z", "+00:00"))
        )
        same_state = previous is not None and (
            previous.value,
            previous.source,
            previous.expires_at,
            previous.source_event_id,
        ) == (
            normalized_value,
            normalized_source,
            normalized_expires_at,
            normalized_event_id,
        )
        if same_state and not newer_confirmation:
            return PreferenceWrite(
                WriteStatus.UNCHANGED,
                normalized_key,
                previous.value,
                previous.value,
                previous.revision,
            )
        if normalized_expires_at is not None and datetime.fromisoformat(
            normalized_expires_at.replace("Z", "+00:00")
        ) <= moment:
            raise UserMemoryValidationError("expires_at 必须晚于 updated_at")
        if previous and moment <= datetime.fromisoformat(previous.updated_at.replace("Z", "+00:00")):
            raise StalePreferenceUpdateError(f"偏好 {normalized_key} 已有更新的记录")
        base = previous or tombstone
        revision = base.revision + 1 if base else 1
        current[normalized_key] = Preference(
            key=normalized_key,
            value=normalized_value,
            source=normalized_source,
            updated_at=normalized_updated_at,
            revision=revision,
            expires_at=normalized_expires_at,
            source_event_id=normalized_event_id,
        )
        self._store_preferences(list(current.values()), deleted=list(deleted.values()))
        return PreferenceWrite(
            WriteStatus.UPDATED if previous else WriteStatus.CREATED,
            normalized_key,
            previous.value if previous else None,
            normalized_value,
            revision,
        )
    

    def delete_preference(
        self,
        key: str,
        *,
        source: str = "explicit",
        deleted_at: str | None = None,
    ) -> PreferenceWrite:
        normalized_key = _preference_key(key)
        normalized_source = _clean_text(source, field_name="source", max_chars=100)
        preferences = {item.key: item for item in self.list_preferences()}
        deleted = {item.key: item for item in self.list_deleted_preferences()}
        tombstone = deleted.get(normalized_key)
        if tombstone is not None:
            return PreferenceWrite(
                WriteStatus.UNCHANGED,
                normalized_key,
                None,
                None,
                tombstone.revision,
            )
        previous = preferences.pop(normalized_key, None)
        if previous is None:
            return PreferenceWrite(WriteStatus.UNCHANGED, normalized_key, None, None, 0)
        previous_at = datetime.fromisoformat(previous.updated_at.replace("Z", "+00:00"))
        if deleted_at is None:
            moment = max(datetime.now(timezone.utc), previous_at)
            normalized_deleted_at = moment.isoformat().replace("+00:00", "Z")
        else:
            normalized_deleted_at = _normalize_timestamp(deleted_at, field_name="deleted_at")
            if datetime.fromisoformat(normalized_deleted_at.replace("Z", "+00:00")) <= previous_at:
                raise StalePreferenceUpdateError(f"偏好 {normalized_key} 已有更新的记录")
        deleted[normalized_key] = DeletedPreference(
            normalized_key,
            normalized_deleted_at,
            previous.revision,
            normalized_source,
        )
        self._store_preferences(list(preferences.values()), deleted=list(deleted.values()))
        return PreferenceWrite(
            WriteStatus.DELETED,
            normalized_key,
            previous.value,
            None,
            previous.revision,
        )

    def get_context_for_agent(self, *, as_of: datetime | None = None) -> str:
        profile = self.read_profile()
        if profile or self.profile_path.exists() or self.user_path.exists():
            self._write_profile_projection(profile)
        user_text = self.user_path.read_text(encoding="utf-8").strip() if self.user_path.exists() else ""
        if user_text == "# User":
            user_text = ""
        active = [item for item in self.list_preferences() if item.is_active(as_of=as_of)]
        memory = _render_preferences(active)
        if as_of is None:
            current = self.memory_path.read_text(encoding="utf-8") if self.memory_path.exists() else ""
            if current != memory:
                self._atomic_write_text(self.memory_path, memory)
        parts = []
        if user_text:
            parts.append("## User profile\n" + user_text)
        if memory.strip():
            parts.append("## Explicit user preferences (cross-project)\n" + memory.strip())
        return "\n\n".join(parts)


