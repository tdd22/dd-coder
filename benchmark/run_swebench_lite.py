import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INSTANCE_ID = sys.argv[1] if len(sys.argv) > 1 else "sympy__sympy-20590"
BENCHMARK = Path(__file__).resolve().parent
WORKSPACE = BENCHMARK / "workspaces" / INSTANCE_ID
OUTPUT = BENCHMARK / "outputs" / "predictions.jsonl"
TRAJECTORY = BENCHMARK / "outputs" / f"{INSTANCE_ID}.json"


def run(args, cwd=None, capture=False):
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )


def remove_tree(path: Path) -> None:
    def make_writable(function, target, _exc):
        os.chmod(target, 0o666)
        function(target)

    shutil.rmtree(path, onexc=make_writable)


def prepare_workspace(repo: str, base_commit: str) -> None:
    if WORKSPACE.exists():
        remove_tree(WORKSPACE)
    WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
    print(f"克隆 {repo}")
    run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            f"https://github.com/{repo}.git",
            str(WORKSPACE),
        ]
    )
    print(f"检出 {base_commit}")
    run(["git", "checkout", "--detach", base_commit], cwd=WORKSPACE)
    status = run(["git", "status", "--short"], cwd=WORKSPACE, capture=True)
    print("初始工作区状态:", status.stdout.strip() or "干净")


def run_agent(problem_statement: str) -> dict:
    os.environ["WORKSPACE_DIR"] = str(WORKSPACE.resolve())
    os.environ["DD_CODER_AUTO_ALLOW"] = "1"
    from agent.graph import run_chat

    prompt = (
        "你正在修复当前工作区里的一个程序错误。\n"
        "请检查代码、修改实现，并在需要时运行命令。\n"
        "写入和命令会直接执行，不需要等待同意。\n"
        "不要通过修改测试来掩盖实现问题。\n"
        "完成后简要说明改了什么。\n\n"
        f"问题描述：\n{problem_statement}"
    )
    thread_id = f"swebench-{INSTANCE_ID}-{uuid.uuid4().hex}"
    result = run_chat(prompt, thread_id=thread_id)
    if result.get("pending"):
        raise RuntimeError("评测模式不应暂停审批")
    result["approvals"] = 0
    result["thread_id"] = thread_id
    return result


def export_patch() -> str:
    completed = run(["git", "diff", "--binary"], cwd=WORKSPACE, capture=True)
    return completed.stdout


def main() -> None:
    from datasets import load_dataset

    dataset = load_dataset("SWE-bench/SWE-bench_Lite", split="test")
    matches = [row for row in dataset if row["instance_id"] == INSTANCE_ID]
    if not matches:
        raise SystemExit(f"Lite 里没有这道题: {INSTANCE_ID}")
    case = matches[0]
    print("instance_id:", case["instance_id"])
    print("repo:", case["repo"])
    print("base_commit:", case["base_commit"])

    prepare_workspace(case["repo"], case["base_commit"])
    print("开始调用 dd-coder")
    result = run_agent(case["problem_statement"])
    patch = export_patch()
    print("停止原因:", result.get("stop_reason"))
    print("模型轮数:", result.get("turns"))
    print("自动批准次数:", result["approvals"])
    print("补丁长度:", len(patch))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": case["instance_id"],
        "model_name_or_path": "dd-coder-deepseek-flash",
        "model_patch": patch,
    }
    OUTPUT.write_text(
        json.dumps(prediction, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    TRAJECTORY.write_text(
        json.dumps(
            {
                "instance_id": case["instance_id"],
                "thread_id": result["thread_id"],
                "reply": result.get("reply"),
                "events": result.get("events"),
                "stop_reason": result.get("stop_reason"),
                "turns": result.get("turns"),
                "approvals": result["approvals"],
                "patch_chars": len(patch),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    from benchmark.metrics_table import write_run_meta

    meta_path = write_run_meta(
        OUTPUT.parent / "runs" / result["thread_id"],
        {
            "instance_id": case["instance_id"],
            "thread_id": result["thread_id"],
            "stop_reason": result.get("stop_reason") or "",
            "turns": result.get("turns") or 0,
        },
    )
    print("预测文件:", OUTPUT)
    print("运行记录:", meta_path)

    run_id = f"dd-coder-{uuid.uuid4().hex[:8]}"
    swebench = Path(sys.executable).with_name("swebench.exe")
    print("开始官方评分，run-id:", run_id)
    run(
        [
            str(swebench),
            "eval",
            "lite",
            "-p",
            str(OUTPUT),
            "-i",
            INSTANCE_ID,
            "--run-id",
            run_id,
            "-j",
            "1",
        ],
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
