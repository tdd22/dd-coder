import hashlib
import os
from pathlib import Path

from agent.transcript import THREAD_ID_RE

TOOL_RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "tool-results"

# run_command 的输出波动大（几字节到几 MB），阈值按字符数定得高一些；
# 其它工具按 UTF-8 字节数，和教程的 CODEBUDDY_TOOL_RESULT_THRESHOLD_KB 对应。
RUN_COMMAND_EXTERNALIZE_CHARS = 30_000
EXTERNALIZE_BYTES = 50 * 1024

# 指针里保留开头和结尾两段预览：关键信息（命令本身、错误摘要、退出码）
# 通常分布在两端，中间多是重复数据。
HEAD_CHARS = 6 * 1024
TAIL_CHARS = 24 * 1024

# read_artifact 单次返回的正文上限。有这个上限，read_artifact 的结果
# 永远到不了外部化阈值，不会出现"读回原文又被换页出去"的循环。
READ_MAX_CHARS = 30_000
DEFAULT_READ_LIMIT = 500
MAX_READ_LIMIT = 2000


def session_dir(thread_id: str) -> Path:
    # 目录名复用 Transcript 的 thread_id 规则，拼路径不会越界。
    if not THREAD_ID_RE.fullmatch(thread_id or ""):
        raise ValueError("thread_id 只能包含字母、数字、下划线和短横线")
    return TOOL_RESULTS_DIR / thread_id


def should_externalize(tool_name: str, result: str) -> bool:
    if tool_name == "run_command":
        return len(result) > RUN_COMMAND_EXTERNALIZE_CHARS
    return len(result.encode("utf-8")) > EXTERNALIZE_BYTES


def write_artifact(thread_id: str, result: str) -> Path:
    folder = session_dir(thread_id)
    folder.mkdir(parents=True, exist_ok=True)
    counter = 0
    while True:
        counter += 1
        path = folder / f"tool_result_{counter:03d}.txt"
        try:
            # 独占创建：服务重启后计数从 001 重来，也只会跳过已有文件，
            # 绝不覆盖旧证据。newline="" 关掉 Windows 的 \n -> \r\n 转换，
            # 磁盘上的字节和原始结果一致，SHA-256 才对得上。
            handle = path.open("x", encoding="utf-8", newline="")
        except FileExistsError:
            continue
        with handle as file:
            file.write(result)
            file.flush()
            os.fsync(file.fileno())
        return path


def make_pointer(tool_name: str, result: str, path: Path) -> str:
    digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
    head = result[:HEAD_CHARS]
    # 临近阈值时结尾预览从开头预览的末尾接着开始，不重复同一段内容。
    tail = result[max(len(result) - TAIL_CHARS, HEAD_CHARS):]
    return (
        f"[工具结果过大，已写入磁盘: {path}]\n"
        f"工具: {tool_name}, 原始 {len(result)} 字符, SHA-256: {digest}\n"
        "下面是开头和结尾的预览。需要完整内容时，"
        "用 read_artifact 工具按行段读取上面的文件。\n\n"
        f"{head}\n"
        f"\n... [完整结果在: {path}] ...\n\n"
        f"{tail}"
    )


def read_artifact(path: str, *, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
    root = TOOL_RESULTS_DIR.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        target = candidate.resolve()
    except OSError:
        return "读取失败: 路径无效"
    # 指针不能变成任意文件读取入口：解析后必须仍在工具结果目录里面。
    if target != root and root not in target.parents:
        return "读取失败: 路径不在工具结果目录内"
    if not target.is_file():
        return f"读取失败: 不是文件: {path}"
    text = target.read_bytes().decode("utf-8", errors="replace")
    lines = text.split("\n")
    if offset >= len(lines):
        return f"[{target.name}: 共 {len(lines)} 行, 起始行 {offset} 超出范围]"
    selected = lines[offset:offset + limit]
    body = "\n".join(selected)
    note = ""
    if len(body) > READ_MAX_CHARS:
        body = body[:READ_MAX_CHARS]
        note = "\n[本次读取超过 30000 字符，已截断；用更小的 limit 分段读取]"
    header = f"[{target.name}: 第 {offset + 1}-{offset + len(selected)} 行, 共 {len(lines)} 行]"
    return f"{header}\n{body}{note}"
