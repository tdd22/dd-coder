from pathlib import Path
import fnmatch


USER_SKILLS_DIR = Path(__file__).resolve().parent.parent / "data" / "skills"
HARNESS_TOOLS = {"skill", "tool_search", "defer_execute"}
_TOP_FIELDS = {"title", "summary", "read_when", "agent_created", "permissions"}
_PERMISSION_FIELDS = {"tools", "network", "paths"}
_PATH_FIELDS = {"read", "write"}


def catalog_text(workspace: Path, user_root: Path | None = None) -> str:
    skills = scan_skills(workspace, user_root)
    if not skills:
        return ""
    lines = ["技能目录:"]
    for skill in skills:
        triggers = ", ".join(skill["read_when"])
        lines.append(f"- {skill['title']}: {skill['summary']}（{triggers}）")
    return "\n".join(lines)


def skill_body_text(loaded: list) -> str:
    parts = []
    for skill in loaded or []:
        content = str(skill.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"技能 {skill.get('title') or ''}:\n{content}")
    return "\n\n".join(parts)


def scan_skills(workspace: Path, user_root: Path | None = None) -> list[dict]:
    root = USER_SKILLS_DIR if user_root is None else user_root
    found = {}
    for folder in [*_skill_dirs(root), *_skill_dirs(workspace / ".dd-coder" / "skills")]:
        skill = _read_skill(folder)
        if skill is not None:
            found[skill["title"]] = skill
    return list(found.values())


def load_skill(workspace: Path, title: str, loaded: list, user_root: Path | None = None) -> str:
    if any(item.get("title") == title for item in loaded):
        return f"技能 '{title}' 已加载。"
    skills = {item["title"]: item for item in scan_skills(workspace, user_root)}
    skill = skills.get(title)
    if skill is None:
        return f"未找到技能 '{title}'。可用技能: {[item['title'] for item in skills.values()]}"
    loaded.append({
        "title": skill["title"],
        "summary": skill["summary"],
        "content": skill["content"],
        "permissions": skill["permissions"],
    })
    return f"技能 '{title}' 已加载。"


def grant_block(loaded_skills: list, name: str, arguments: object) -> str | None:
    if name in HARNESS_TOOLS:
        return None
    if name.startswith("mcp__"):
        if not mcp_declared(loaded_skills, name):
            return f"未执行: 当前技能未声明工具 {name}"
        return None
    if not loaded_skills:
        return None
    granting = [
        skill for skill in loaded_skills
        if name in (skill.get("permissions") or {}).get("tools", [])
    ]
    if not granting:
        return f"未执行: 当前技能未声明工具 {name}"
    if name in {"list_dir", "read_file", "write_file"}:
        relative = _relative(name, arguments)
        key = "write" if name == "write_file" else "read"
        if not any(_path_allowed(skill, key, relative) for skill in granting):
            return "未执行: 路径不在当前技能允许范围内"
    return None


def mcp_declared(loaded_skills: list, name: str) -> bool:
    for skill in loaded_skills or []:
        permissions = skill.get("permissions") or {}
        if name in permissions.get("tools", []) and permissions.get("network") is True:
            return True
    return False


def tool_declared(loaded_skills: list, name: str) -> bool:
    if not loaded_skills:
        return True
    return any(name in (skill.get("permissions") or {}).get("tools", []) for skill in loaded_skills)


def _skill_dirs(root: Path):
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            yield child


def _read_skill(folder: Path) -> dict | None:
    try:
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
    parts = _split_frontmatter(text)
    if parts is None:
        return None
    front, body = parts
    parsed = _parse_mapping(front.splitlines(), 0, 0)
    if parsed is None:
        return None
    data, _index = parsed
    skill = _validate(folder.name, data, body)
    return skill


def _validate(folder_name: str, data: dict, body: str) -> dict | None:
    if set(data) - _TOP_FIELDS:
        return None
    title = data.get("title")
    summary = data.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        return None
    if title != folder_name or title in {"", ".", ".."} or "/" in title or "\\" in title:
        return None
    if "---" in title or "---" in summary:
        return None
    read_when = data.get("read_when", [])
    if not isinstance(read_when, list) or not all(isinstance(item, str) for item in read_when):
        return None
    if any("---" in item for item in read_when):
        return None
    agent_created = data.get("agent_created", False)
    if not isinstance(agent_created, bool):
        return None
    permissions = _validate_permissions(data.get("permissions", {}))
    if permissions is None:
        return None
    return {
        "title": title,
        "summary": summary,
        "read_when": read_when,
        "content": body.strip(),
        "permissions": permissions,
    }


def _validate_permissions(raw: object) -> dict | None:
    if raw == {}:
        raw = {"tools": [], "network": False, "paths": {"read": [], "write": []}}
    if not isinstance(raw, dict) or set(raw) - _PERMISSION_FIELDS:
        return None
    tools = raw.get("tools", [])
    network = raw.get("network", False)
    paths = raw.get("paths", {})
    if not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        return None
    if not isinstance(network, bool):
        return None
    if paths == {}:
        paths = {"read": [], "write": []}
    if not isinstance(paths, dict) or set(paths) - _PATH_FIELDS:
        return None
    read = paths.get("read", [])
    write = paths.get("write", [])
    if not _valid_patterns(read) or not _valid_patterns(write):
        return None
    return {
        "tools": list(tools),
        "network": network,
        "paths": {"read": list(read), "write": list(write)},
    }


def _valid_patterns(patterns: object) -> bool:
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        return False
    return not any(_bad_pattern(item) for item in patterns)


def _bad_pattern(pattern: str) -> bool:
    if not pattern or "---" in pattern:
        return True
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        return True
    return ".." in normalized.split("/")


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    return None


def _parse_mapping(lines: list[str], start: int, indent: int) -> tuple[dict, int] | None:
    data = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        current = _indent(line)
        if current < indent:
            break
        if current > indent or line.strip().startswith("- ") or ":" not in line:
            return None
        key, rest = line.strip().split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if not key or key in data:
            return None
        if rest == "":
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index >= len(lines) or _indent(lines[index]) <= indent:
                data[key] = {}
                continue
            if lines[index].strip().startswith("- "):
                parsed = _parse_list(lines, index, _indent(lines[index]))
                if parsed is None:
                    return None
                data[key], index = parsed
            else:
                parsed = _parse_mapping(lines, index, _indent(lines[index]))
                if parsed is None:
                    return None
                data[key], index = parsed
        else:
            value = _scalar(rest)
            if value is None:
                return None
            data[key] = value
            index += 1
    return data, index


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list[str], int] | None:
    items = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        current = _indent(line)
        if current < indent:
            break
        stripped = line.strip()
        if current != indent or not stripped.startswith("- "):
            return None
        value = _scalar(stripped[2:].strip())
        if not isinstance(value, str):
            return None
        items.append(value)
        index += 1
    return items, index


def _scalar(raw: str):
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "[]":
        return []
    if raw == "{}":
        return {}
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    if not isinstance(raw, str) or "\n" in raw:
        return None
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _relative(name: str, arguments: object) -> str:
    if not isinstance(arguments, dict):
        return ""
    raw = arguments.get("relative")
    if name == "list_dir" and not isinstance(raw, str):
        return "."
    if isinstance(raw, str) and raw:
        return raw
    return ""


def _path_allowed(skill: dict, key: str, relative: str) -> bool:
    patterns = (skill.get("permissions") or {}).get("paths", {}).get(key) or []
    return any(_match(pattern, relative) for pattern in patterns)


def _match(pattern: str, relative: str) -> bool:
    rel = relative.replace("\\", "/") or "."
    pattern = pattern.replace("\\", "/")
    if pattern == "**":
        return True
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return rel == base or rel.startswith(base + "/")
    return fnmatch.fnmatch(rel, pattern)
