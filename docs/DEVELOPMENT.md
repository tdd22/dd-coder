# dd-coder 开发文档

本地 coding agent。浏览器里发一句话，LangGraph 图调用模型；模型可以在选定的工作区里列目录、读文件、写文件、跑命令，也可以把项目事实追加到这份工作区的记忆里。同一工作区的会话共享这份记忆。对话保存在 SQLite，服务重启后还在。

这份文档描述 2026-10-14 的代码。下次开发先读这里，再改代码。

## 已经定下的决定

- 产品是本机服务加浏览器，不是纯网页，也不是桌面安装包。
- 后端用 Python。界面是一个静态页面，由同一个服务返回。
- 模型走 OpenAI 兼容接口。地址、模型名和密钥放在本机 `.env`，不要写进代码，也不要贴到对话里。
- Agent 用 LangGraph 的 `StateGraph`，不再用手写的 `for` 循环。
- 读文件直接执行。写入和普通命令先在页面上询问。危险命令和越界路径直接拒绝，点「同意」也不能放行。
- 网页上的询问用 LangGraph 的 `interrupt` 暂停，用 `Command(resume=...)` 恢复。终端里直接调用 `dispatch` 时，仍用 `input()` 询问。
- 一个服务进程里可以有多条会话。每条会话一个 `thread_id`，都存在同一个 `data/checkpoints.sqlite`。页面记住当前 id，请求里带上它。
- 桌面安装包先不做。等 agent 功能更完整，再考虑把浏览器换成桌面壳。
- 工具只能把相对路径解析到 `WORKSPACE_DIR` 里面。函数名是 `reslove_inside`，拼写就这样，调用处要跟着这个名字。

## 目录

```text
dd-coder/
  agent/
    config.py    读取 .env
    llm.py       OpenAI 兼容客户端
    routing.py   任务到 lite / default / craft 的路由
    transcript.py  每个会话一份只追加的 JSONL 证据
    workspace.py 工作区记忆：日志、蒸馏、抄本
    user_memory.py 用户档案和跨项目偏好
    tool.py      工具登记、分发，以及路径限制
    permission.py  允许、询问、拒绝，以及能不能执行
    graph.py     LangGraph 图、暂停恢复、会话读写
  server/
    server.py    HTTP 接口和首页
  web/
    index.html   页面，样式和脚本都在这一个文件里
  workspace/     默认工作区，agent 可以改这里的文件
  data/
    checkpoints.sqlite   会话数据库，服务运行后生成
    transcripts/         每个 thread_id 一个 .jsonl，服务写过对话后生成
    workspace-memory/    每个工作区一份记忆，第一次写入后生成
    user-memory/         本机用户的档案和偏好，第一次打开后生成
  main.py        终端里跑一轮，不走网页
  requirements.txt
  .env           本机密钥，已忽略
  .env.example   不含密钥的示例
```

已删除、不要再加回来的旧实现：`agent/loop.py`、`agent/pending.py`、`agent/diff.py`、`agent/history.py`、`data/session.json`。

## 环境

Windows，PowerShell，Python 3.13。虚拟环境目录名是 `venv`，不是 `.venv`。

```powershell
cd C:\Users\tdd88\Desktop\dd-coder
venv\Scripts\activate
python -m pip install -r requirements.txt
```

`.env` 需要这四项：

```text
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
OPENAI_MODEL_LITE=
OPENAI_MODEL_DEFAULT=
OPENAI_MODEL_CRAFT=
WORKSPACE_DIR=./workspace
```

`OPENAI_BASE_URL` 和 `OPENAI_MODEL` 按实际服务商填写。`WORKSPACE_DIR` 必须是已存在的文件夹。`load_config()` 会把它解析成绝对路径。

`OPENAI_MODEL_LITE`、`OPENAI_MODEL_DEFAULT`、`OPENAI_MODEL_CRAFT` 可以不填。空着时这三档都使用 `OPENAI_MODEL`。编码循环读 `models["craft"]`，会话标题读 `models["lite"]`。

## 启动

网页：

```powershell
python -m uvicorn server.server:app
```

浏览器打开 `http://127.0.0.1:8000`。必须在项目根目录启动，这样 `server` 和 `agent` 两个包才能被导入。

终端里直接跑一轮，不经过网页，也不写入 SQLite：

```powershell
python main.py
```

`main()` 调用的是 `build_graph()`，没有传入 checkpointer。它和网页会话是分开的。写入和跑命令的暂停依赖 checkpointer，所以 `python main.py` 里如果模型要改文件或跑命令，会在 `interrupt` 处失败。这条路只适合不触发询问的试验。

## 一次请求怎么走

```text
浏览器 POST /chat，正文里带当前 thread_id
  -> server.chat
  -> graph.run_chat
  -> 从 SQLite 读出这个 thread_id 的 messages
  -> 追加本轮用户消息，并把 turns 重置为 0
  -> agent 节点读出工作区记忆，临时放在发给模型的消息前面，不写回会话
  -> agent 节点调用模型，turns 加 1
  -> 如果回复里有 tool_calls，进入 tools 节点
  -> 真正调用处理函数之前先做权限判断
  -> 允许：执行工具，结果交回模型
  -> 拒绝：不执行，把「未执行」交回模型
  -> 询问：interrupt 暂停，返回 pending，页面显示原因和「同意」「拒绝」
  -> 浏览器 POST /approve 后，从 tools 节点开头继续，再按允许或拒绝走上面两条
  -> tools 执行完，turns 还没到 8，就回到 agent
  -> tools 执行完，turns 已经到 8，就停止，原因是 max_turns
  -> 没有 tool_calls，并且 finish_reason 是 length，就停止，原因是 max_tokens
  -> 没有 tool_calls 的其它情况，停止，原因是 final_answer
  -> 返回 reply、events、pending、stop_reason、turns
  -> 没有 pending 时，页面显示回复；如果不是 final_answer，再显示循环停止原因
```

图的边：

```text
START -> agent -> 有工具调用? -> tools -> 未到 8 轮? -> agent
                    没有 -> END              已经 8 轮 -> END
```

`llm.chat` 返回模型消息和 `finish_reason`，并带 `max_tokens=8000`。`agent_node` 把 `finish_reason` 写入状态字段 `provider_finish_reason`。`tools_node` 不调用模型，也不覆盖这个字段，所以上一轮记下的值会留在最终状态里。

`agent_node` 用 `stop_reason_for` 决定这一轮是继续还是停止。有工具调用就继续。没有工具调用时，`finish_reason == "length"` 记为 `max_tokens`，其它记为 `final_answer`。`turns` 只统计这一次用户请求里的模型调用次数，每次 `invoke` 都从 0 开始。上限 `MAX_TURNS` 是 8。第 8 次如果还要工具，工具会执行并写入消息，然后停止，不再请求模型。这时 `stop_reason` 是 `max_turns`，`provider_finish_reason` 仍是第 8 次模型调用留下的值。

放回状态时，`function.arguments` 必须保持字符串。只有在 `tools_node` 里执行前才 `json.loads`。把参数提前变成字典再送回接口，会得到 422。

`run_chat` 用调用前的消息条数切开本轮新增内容。`role == "tool"` 的内容放进 `events`。`final_answer` 和 `max_tokens` 的 `reply` 是最后一条没有 `tool_calls` 的助手文本。`max_turns` 的 `reply` 是最后一条助手文本，这条消息通常还带着工具调用。

`invoke` 返回后，`run_chat` 和 `resume_chat` 都再读一次图状态。有 `interrupts` 时，把第一条的 `value` 放进 `pending`。这个字典是 `page_approver` 交给 `interrupt` 的内容：`tool`、`rule_id`、`reason`。暂停期间助手消息通常还带着工具调用，页面不把那条空回复当最终答案，而是显示「等待你的决定」。

恢复时 `tools_node` 会从头再执行一遍。`interrupt` 第二次被调用时不再暂停，而是返回用户点的 `true` 或 `false`。同一轮里，暂停前已经跑过的只读工具会再跑一次。写入和命令要等这次返回之后才执行。暂停还没恢复时不要再发新的 `/chat`，页面在等待决定时会禁用发送。

## 工具

都在 `agent/tool.py`，并且只登记一次。每条 `ToolSpec` 同时包含名字、参数说明、处理函数、`concurrent_safe` 和 `deferred`。`REGISTRY.openai_tools()` 只把 `deferred` 为假的工具发给模型。`tools_node` 把这一轮调用交给 `REGISTRY.dispatch_many`。

执行顺序是：先按名字查找，再用 `validate_arguments` 检查参数，然后做权限判断，最后才调用处理函数。未知工具返回 `未知工具`。参数不是对象、缺必填项、类型不对、不在枚举里、整数超出范围、字符串不符合 `pattern`，或多了未声明字段，返回 `参数不正确`，此时还不会做权限判断，也不会调用处理函数。布尔值不算整数。处理函数里的 `SystemExit` 和其它异常返回 `工具执行失败`。

`tool_search` 和 `defer_execute` 在权限判断之前就返回。真正的 `write_file`、`run_command` 或 `write_memory` 会由 `execute_deferred` 再进入一次 `dispatch`，权限看的是这一次里面的工具。`execute_deferred` 必须用关键字把询问函数传下去：`dispatch(..., approver=approver)`。写在第 4 个位置会占掉 `loaded`，网页就会退回终端询问。

同一轮有多个调用时，只有每一个都登记过、参数已经是字典、并且 `concurrent_safe` 都是 `True`，才会用最多 4 个线程一起执行。结果仍按原来的调用顺序排回去。其中有写入、跑命令、未知工具或无法解析的参数时，整批按顺序执行。

| 工具 | 行为 | 模型怎么调用 | 并发 |
| --- | --- | --- | --- |
| `list_dir` | 列出目录。文件夹名后面加 `/` | 直接调用 | 可以 |
| `read_file` | 按 UTF-8 读取文件 | 直接调用 | 可以 |
| `tool_search` | 按 `names` 或 `query` 查找延迟工具，把完整参数写入结果 | 直接调用 | 按顺序 |
| `defer_execute` | 执行已经搜索过的延迟工具。参数是 `name` 和 `arguments` | 直接调用 | 按顺序 |
| `write_file` | 直接覆盖写入。父目录不存在会创建 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `run_command` | `subprocess` 执行字符串列表，工作目录限制在工作区内，超时 30 秒 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `write_memory` | 把一条事实追加到工作区记忆日志。来源固定为 `model_tool`。不蒸馏 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `update_user_profile` | 按字段更新用户档案。没出现的字段保持原样。`null` 删掉该字段 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `save_user_preference` | 按 key 保存一条跨项目偏好。来源固定为 `model_tool`。不能提交事件编号或证据时间 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `forget_user_preference` | 按 key 遗忘一条跨项目偏好。来源固定为 `model_tool`。删除时间由程序决定 | 先 `tool_search`，再 `defer_execute` | 按顺序 |

`tool_search` 和 `defer_execute` 的具体逻辑在 `search` 和 `execute_deferred`。`dispatch` 看到这两个名字就转去调用它们，不走登记记录上的 `handler`。那两个 `handler` 只是占位，因为每条 `ToolSpec` 都必须有处理函数。`handler` 只能看到工作区和参数，看不到当前会话已经加载过哪些名字。

已加载名单在图的状态字段 `loaded_tools` 里。`tools_node` 每次执行前读出它，传给 `dispatch_many`，执行完再写回去。`run_chat` 的 `invoke` 输入不要带这个字段，否则会把这条会话已经搜索过的工具清掉。没搜索过就调用 `defer_execute`，会得到先去 `tool_search` 的提示。`read_file` 这类直接工具如果走 `defer_execute`，会得到直接调用的提示。同一个名字第二次搜索，结果里标记为已加载。

处理函数里的路径再经过 `reslove_inside`：解析后的绝对路径必须等于工作区，或者位于工作区里面。`..` 会在这里被拒绝。权限阶段用的是 `path_is_inside`，规则相同，但只返回能不能通过，不抛异常。

命令输出按 UTF-8 解码，无法解码的字节换成占位符。找不到程序时返回「找不到命令」，不会把整个请求打崩。Windows 上经常没有 `python3`、`cat`、`xxd`。

`run_command` 的当前目录在工作区内，但命令本身仍可以读写工作区以外的路径。所以普通命令只询问，不自动允许。危险词检查只看拼出来的命令文字，挡不住所有副作用。

## 权限

逻辑在 `agent/permission.py`。一次工具执行过三道，后面的不改前面的结论。

```text
dispatch 检查完参数
  -> execution_block
  -> decide：只给出 allow / ask / deny，不写文件，不跑命令，不询问
  -> resolve_permission：只决定最后能不能执行
  -> 不能执行：把「未执行」返回给模型
  -> 能执行：dispatch 才调用 handler
```

`decide` 按这个顺序匹配，命中就停：

| 顺序 | rule_id | 结果 | 什么时候 |
| --- | --- | --- | --- |
| 1 | `request.invalid_arguments` | 拒绝 | 路径工具缺少路径，或命令不是非空字符串列表 |
| 2 | `path.outside_workspace` | 拒绝 | `list_dir`、`read_file`、`write_file` 的路径跑出工作区 |
| 3 | `read.allow` | 允许 | 工作区内的 `list_dir`、`read_file`，以及 `tool_search` |
| 4 | `path.write_requires_approval` | 询问 | 工作区内的 `write_file` |
| 5 | `command.hard_deny` | 拒绝 | 命令文字命中 `sudo`、`rm -rf`、关机、格式化、典型 `dd` 设备写入 |
| 6 | `command.requires_approval` | 询问 | 其余 `run_command` |
| 7 | `memory.write_requires_approval` | 询问 | `write_memory`。同意后才追加日志 |
| 8 | `profile.write_requires_approval` | 询问 | `update_user_profile`。同意后才改档案 |
| 9 | `preference.write_requires_approval` | 询问 | `save_user_preference`。同意后才保存偏好 |
| 10 | `preference.forget_requires_approval` | 询问 | `forget_user_preference`。同意后才遗忘 |
| 11 | `default.deny` | 拒绝 | 没有规则覆盖的工具名 |

`list_dir` 可以不传 `relative`，这时按 `.` 判断。`write_file` 即使路径在工作区内也要询问：没越界只说明位置合法，不说明用户同意改文件。

`resolve_permission` 的处理：

- `allow`：可以执行，状态 `not_required`。不调用询问函数。
- `deny`：不能执行，状态 `not_required`。不调用询问函数。询问函数即使回答同意，也改变不了拒绝。
- `ask`：调用传入的询问函数。`True` 是 `approved`，`False` 是 `rejected`。询问过程出错是 `cancelled`，结果也是不能执行。

`execution_block` 在不能执行时返回给模型的文字：

- 用户拒绝：`未执行: 你拒绝了这次操作`
- 询问出错或取消：`未执行: 审批被取消`
- 规则直接拒绝：`未执行:` 后面接 `decide` 的 `reason`

能执行时它返回 `None`，`dispatch` 继续调用处理函数。

网页的询问函数是 `graph.page_approver`。它调用 `interrupt`，所以会抛出 `GraphInterrupt`。`resolve_permission` 必须把这个异常原样抛出，不能收成 `cancelled`，否则页面不会出现按钮，工具会被直接挡住。`interrupt` 第一次暂停图；`Command(resume=true 或 false)` 之后，同一次 `interrupt` 返回这个布尔值。

终端里没传入询问函数时，`execution_block` 用 `console_approver`：打印规则和原因，读一行输入，只有 `y` 算同意。

## 模型路由

逻辑在 `agent/routing.py`。任务名直接对应层级，不根据用户那句话猜测。

| 任务 | 层级 | 工具 |
| --- | --- | --- |
| `agent` | craft | 可以带现有工具列表 |
| `title` | lite | 不传工具 |

`agent_node` 调用 `route_task(settings, "agent")`，复制一份配置，把 `model` 换成路由结果，再交给 `chat()`。`chat()` 的参数不变，它仍然读 `config["model"]`。状态字段 `model_tier` 和 `model_name` 记下这一次；`tools_node` 不覆盖它们。`run_chat` 和 `resume_chat` 把这两个字段放进返回值，`/chat` 和 `/approve` 再带回页面。

`write_title_if_absent` 在没有 `pending`、并且日志里还没有 `ai-title` 时，用 `complete_text()` 走 lite。这次调用不进入 8 轮计数。失败只打印到跑服务的终端，不把已经生成的回复变成错误。

`agent_node` 在调用 `chat()` 之前读取 `UserMemory().get_context_for_agent()`，再读取 `WorkspaceMemory(workspace).get_context_for_agent()`。有内容时，临时放两条 `system`，用户上下文在前，工作区记忆在后。返回状态时仍用原来的 `state["messages"]` 加上新的助手消息，这两段系统文字不进 checkpoint，也不进历史栏。空的那段不加。聊天不调用 `distill()`。

## 工作区记忆

逻辑在 `agent/workspace.py`。它按工作区绝对路径算出 `workspace_id`（SHA-256 的前 16 位），文件放在 `data/workspace-memory/<workspace_id>/`。这个目录在 `WORKSPACE_DIR` 外面，`write_file` 和 `run_command` 改不到它。同一工作区的每条会话读同一份。这里不读取、也不写入用户档案和偏好。

```text
data/workspace-memory/<workspace_id>/
  daily/YYYY-MM-DD.jsonl   原始事实，只追加
  curated.json             长期档案，整份原子替换
  MEMORY.md                只从 active 档案生成的抄本
```

一条事实有 `kind`（`decision`、`convention`、`pitfall`、`outcome`）、`content`、`importance`（1 到 5）。`memory_key` 可选，表示这条事实在回答哪个问题，例如 `runtime.python-version`。不填时，记住的就是这句原话。`append_daily_log` 校验后追加一行并 `fsync`。最后一行没有换行且不是完整 JSON 时，读取丢掉这截半行。中间坏行、`workspace_id` 对不上、版本不是 2，都会报错。

`distill(as_of=...)` 先处理没有 `memory_key` 的事实，再处理有 `memory_key` 的事实，最后按档案重写 `MEMORY.md`。两次都读日志。第二次再读 `curated.json`，是为了把第一次写下的条目一起保存，不是把无 key 的句子改写成带 key 的档案。

挑选门槛：记录时间早于观察日往前 30 天，类型属于前三种，并且还没出现在任何 `evidence_ids` 里。`outcome` 只留在日志里。一组相同的话还要满足：最高重要度至少 4，或不同 `fact_id` 至少 2 个。没有 `memory_key` 时，类型加收平后的原句决定档案编号；同一句合并证据，另一句另建一条。

有 `memory_key` 时，一个问题只有一个 `active` 修订。同一句并进当前修订，`DistillReport.updated` 加 1。不同的说法要至少 2 条独立事实，且最新时间晚于当前修订的 `last_seen`，才新建下一修订，并把旧修订标成 `superseded`。这时 `created` 和 `superseded` 各加 1。两个候选的重要度、证据条数和最新时间都相同，就保持当前答案，把问题名放进 `tied`。打平不写冲突文件，也不做人工裁决。`DistillReport` 只统计带 `memory_key` 的变化；没有问题名的新建和合并仍会写入档案和抄本，但这四个数字可以都是 0。

`MEMORY.md` 只列出 `active` 的决策、约定和踩坑，每行只有原句。被替换的旧答案留在 `curated.json`。`read_memory_md()` 按档案重算；抄本不一致时原子替换。档案和抄本都还没有时，不创建空文件。

`get_context_for_agent()` 只含两部分：这份抄本，以及最近最多 6 条还没晋升、且没有 `memory_key` 的日志。带 `memory_key` 但还没替换成功的句子不进这 6 条。`write_memory` 必须登记在 `tool_search` 之前，搜索目录是在导入时拼好的。

## 用户记忆

逻辑在 `agent/user_memory.py`。本机用户是 `local-user`。`scope_id` 是这个名字收成小写后的 SHA-256 前 16 位，目录是 `data/user-memory/users/<scope_id>/`，在工作区外面。档案的真相是 `profile.json`，偏好的真相是 `preferences.json`。`persona/user.md` 只反映档案，`MEMORY.md` 只反映当前还生效的偏好。

档案字段只有 `name`、`call_them`、`pronouns`、`city`、`timezone`、`notes`。`update_profile` 只改请求里出现的字段，`None` 表示删掉该字段。有改动才重写 `profile.json`；不管改不改，都会把抄本对上。

偏好按 key 创建、替换或删除。值和来源、过期时间、事件编号都相同，才是 `UNCHANGED`。不传 `updated_at` 时用当前 UTC 时间，这次调用本身不算再次确认。显式传入更晚的时间、又没有事件编号，即使值不变也增加 `revision`。同一个事件编号的相同状态重试不改时间和 `revision`。`expires_at` 到达之后记录留在 JSON 里，不进 `MEMORY.md`，也不进 `get_context_for_agent()`。

删除留下墓碑，只记 key、删除时间、`revision` 和来源，不留原值。早于或等于 `deleted_at` 的证据再写入会抛 `StalePreferenceUpdateError`，偏好不复活。更晚的证据可以重建，`revision` 接着墓碑加 1，墓碑同时去掉。重复删除返回 `UNCHANGED`，保留第一次的 `deleted_at`。

`get_context_for_agent()` 在有档案或仍生效的偏好时返回两段文字：`## User profile` 和 `## Explicit user preferences (cross-project)`。空的时候返回空字符串。不传 `as_of` 时，如果 `MEMORY.md` 和当前生效的偏好不一致，会按 JSON 把抄本修好。传入历史时间只影响这次返回的偏好，不改 `MEMORY.md`。

`update_user_profile` 的字段可以是 `null`。参数检查看到 `nullable` 就放行，`update_profile` 据此删掉该字段。没标 `nullable` 的字符串，`null` 仍返回「必须是字符串」。这三个工具都要先 `tool_search`，再 `defer_execute`，同意后才写入。模型不能提交 `source_event_id` 或 `updated_at`。

## JSONL 证据

每个会话一个文件：`data/transcripts/<thread_id>.jsonl`。`thread_id` 只能是字母、数字、下划线和短横线。checkpoint 仍然是图的运行时，暂停和 `loaded_tools` 都靠它。JSONL 只追加已经提交进状态的事件。

`run_chat` 和 `resume_chat` 在图返回后，按本轮新消息调用 `record_visible_messages`。用户和带文字的助手消息写成 `type: message`。助手消息里的每个工具调用写成 `function_call`。工具结果写成 `function_call_result`。写在 `tools_node` 里面会在恢复审批时记重，所以不要挪到节点里。

每一行由 `append` 补上 `schema_version`、`sequence`、`session_id`、`event_id`、`recorded_at`。调用方不能自己带这些字段。`event_id` 是 `transcript:<thread_id>:<sequence>`。文件非空且最后一个字节不是 `\n` 时，拒绝再追加，原文件不改。读取时，只有最后一截不完整、而且没有换行的 JSON 会被跳过，并标上 `ignored_partial_tail`。中间坏行、序号或 `event_id` 对不上，抛 `TranscriptCorruptionError`。

`replay()` 把 `message` 折成 `{role, content}`，把最后一条 `ai-title` 折成标题。工具事件先留在文件里，不进 `replay` 的消息列表。`select_memory_candidate` 只能按已落盘的 `event_id` 选择 `message`，返回摘要和来源，不复制原文。聊天循环不调用它。

## 会话

`get_graph()` 只创建一次图。数据库文件是 `data/checkpoints.sqlite`。连接使用 `check_same_thread=False`，并由 `SqliteSaver` 持有。多条会话共用这一张图和这一个文件，靠 `thread_id` 分开。`run_chat`、`resume_chat`、`get_history` 都把传入的 id 放进 `config["configurable"]["thread_id"]`。没传时用 `default`。

`get_history(thread_id)` 只返回这个 id 里带文字的 `user` 和 `assistant` 消息。工具调用细节不进历史栏，只出现在当轮的 `events`。空状态不会往数据库里插一行。

`list_sessions()` 从 `checkpoints` 表取出出现过的 `thread_id`。标题优先用该会话 jsonl 里最后一条 `ai-title`，最多 40 个字。没有标题、或日志读不出来时，用第一条非空用户消息的第一行；还没有用户消息时标题是「新会话」。列表按 id 字符串排序，不是按时间。

`new_session_id()` 返回 `uuid4` 的前 12 位十六进制。`POST /sessions` 只返回这个 id，不写入数据库。第一条 `POST /chat` 成功之后，这个 id 才会出现在 `checkpoints` 里，侧边栏标题也会换成那条用户消息。

页面把当前 id 放在 `sessionStorage` 的 `threadId`。打开页面时读它，没有就用 `default`，然后请求 `GET /sessions` 和 `GET /history?thread_id=`。

新建：点「开启新对话」会 `POST /sessions`，把返回的 id 写成当前会话，清掉回复区、停止原因和审批框，再拉侧边栏和历史。历史是空的，中间显示「今天有什么可以帮你？」。这条还没发过消息的会话，只在它是当前会话时由页面补进侧边栏。点走之后，列表里不再出现它。

切换：点另一条会话时，先把当前 id 换成它，清掉上一条留在页面上的回复和审批框，再拉侧边栏和这份历史。正在发送或正在等审批时，新建和切换都禁用。点当前这条不会重新加载。

发送和审批都带当前 `thread_id`。回复结束后再拉一次历史和会话列表，这样新会话的标题和气泡会一起出现。

## HTTP

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 返回 `web/index.html` |
| POST | `/chat` | 正文 `{"message", "thread_id"}`。`thread_id` 可省略，省略时是 `default`。返回 `reply`、`events`、`pending`、`stop_reason`、`turns`、`model_tier`、`model_name` |
| POST | `/approve` | 正文 `{"approved": true 或 false, "thread_id"}`。恢复这个 id 上暂停中的图，返回字段和 `/chat` 相同 |
| GET | `/history?thread_id=` | 返回这个会话的 `[{"role", "content"}]`。省略 `thread_id` 时读 `default` |
| GET | `/sessions` | 返回已经写进数据库的 `[{"id", "title"}]` |
| POST | `/sessions` | 返回 `{"id"}`。只生成 id，不创建数据库记录 |

没有暂停时 `pending` 是 `null`。暂停时它是 `{"tool", "rule_id", "reason"}`。

## 页面

`web/index.html` 自包含。左侧栏始终在左边：上面是「+ 开启新对话」，下面是会话列表，当前那条用浅蓝底标出。窗口窄到 800px 以下时，左栏收成 220px，仍然在左边，不叠到对话上面。右边是聊天区，内容列最宽约 800px。没有消息时居中显示「今天有什么可以帮你？」。用户消息在右侧浅底气泡里，助手回复在左边。底部是圆角输入框，回车发送，Shift+回车换行。发送中按钮会禁用。`stop_reason` 是 `max_tokens` 或 `max_turns` 时，回复下面显示一行停止原因。响应里有 `model_tier` 或 `model_name` 时，再显示一行「模型：层级 · 模型名」。发送和审批进行中先把这一行藏起来。

`pending` 有值时，回复区显示「等待你的决定」，输入框上方显示 `reason` 和 `rule_id`，以及「同意」「拒绝」。等待期间发送按钮保持禁用，避免再发一条 `/chat`。点按钮调用 `POST /approve`。恢复完成后决定框隐藏；如果这次恢复又产生新的 `pending`，决定框继续留着。

## 已知问题

- `get_graph()` 里给 `_conn` 赋值时没有写 `global _conn`。连接目前能留下来，是因为 `SqliteSaver` 保存了它。以后改这个函数时，连接必须在进程结束前保持打开。
- `agent/llm.py` 的 `complete()` 已不再被图使用。编码循环走 `chat()`，会话标题走 `complete_text()`。
- `server.py` 在导入时调用 `load_config()`，但请求处理并不使用这个变量。`.env` 缺项时，服务会在启动阶段失败。
- 工具调用过程只 `print` 到跑服务的终端，页面要等整轮结束后才看到 `events`。询问原因会先出现在决定框里。
- `python main.py` 没有 checkpointer。模型在这条路上要写入或跑命令时，`interrupt` 无法暂停。
- 恢复暂停时 `tools_node` 从头再跑。同一轮暂停前的只读工具会再执行一次。
- 命令危险词只检查拼出来的文字，不是沙盒。`run_command` 仍可能访问工作区以外的路径，所以普通命令一律询问。
- 聊天不调用 `distill()`。当天用 `write_memory` 写下的带 `memory_key` 的事实，要另一次蒸馏之后才会进入 `MEMORY.md`。
- 打平的两个答案只出现在 `DistillReport.tied` 里。重启后没有一份待审案子，也没有五阶段事务日志。

## 下次可以做的事

还没做，也不要当成已经答应的范围：

- 工作区记忆的冲突队列、人工裁决，以及多文件事务恢复。
- 删除会话，以及按时间而不是按 id 排列侧边栏。
- 流式输出：边生成边显示回复和工具记录。
- 工作区文件树。
- 清理未使用的 `complete()`。
