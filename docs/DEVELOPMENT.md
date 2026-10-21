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
    pending.py   会话未决任务的校验、迁移和渲染
    prompt_assembly.py  系统提示片段的预算装箱
    skills.py    技能目录、按名字加载、技能声明收窄工具
    connectors.py  MCP 子进程、信任和 tools/call
    context_compact.py  对话副本的分层压缩
    tool_results.py  工具结果外部化：阈值、写盘、指针、按行段读回
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
    tool-results/        每个 thread_id 一个文件夹，第一次外部化后生成
    skills/              用户级技能，每个技能一个 SKILL.md
    mcp.json             外接工具进程配置，有连接器之后才需要
    connector_trust.json 已信任的连接器，用户自己写
    workspace-memory/    每个工作区一份记忆，第一次写入后生成
    user-memory/         本机用户的档案和偏好，第一次打开后生成
  main.py        终端里跑一轮，不走网页
  requirements.txt
  .env           本机密钥，已忽略
  .env.example   不含密钥的示例
```

已删除、不要再加回来的旧实现：`agent/loop.py`、`agent/diff.py`、`agent/history.py`、`data/session.json`。现在的 `agent/pending.py` 是未决任务，不是以前那份审批队列。

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

要把模型调用记到 Langfuse Cloud，再加这三项。公钥和密钥留空时不发送记录。地址要和 Langfuse 项目设置里显示的区域一致，欧盟区是 `https://cloud.langfuse.com`，美国区是 `https://us.cloud.langfuse.com`。

```text
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

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
  -> 先把本轮用户消息写入 Transcript，得到 event_id
  -> 追加本轮用户消息，并把 turns 重置为 0
  -> pending_extract 节点最多根据这条用户事件调用一次 lite 模型
  -> agent 节点组装系统提示，压缩对话副本，再调用模型，turns 加 1
  -> 如果回复里有 tool_calls，进入 tools 节点
  -> 真正调用处理函数之前先做权限判断
  -> 允许：执行工具；超过阈值的结果先写入 tool-results，指针交回模型
  -> 拒绝：不执行，把「未执行」交回模型
  -> 询问：interrupt 暂停，返回 pending，页面显示原因和「同意」「拒绝」
  -> 浏览器 POST /approve 后，从 tools 节点开头继续，再按允许或拒绝走上面两条
  -> tools 执行完，turns 还没到 8，就回到 agent
  -> tools 执行完，turns 已经到 8，就停止，原因是 max_turns
  -> 没有 tool_calls 时进入 pending_verify；finish_reason 是 length 时 stop_reason 仍是 max_tokens，其它是 final_answer
  -> 有 open 任务时，pending_verify 调用 lite 模型，只保存完成建议
  -> 没有 open 任务时，pending_verify 不调用模型
  -> 图结束后先写工具调用和工具结果；没有审批暂停时，Harness 再核验证据并提交 done
  -> 返回 reply、events、pending、stop_reason、turns
  -> 没有 pending 时，页面显示回复；如果不是 final_answer，再显示循环停止原因
```

图的边：

```text
START -> pending_extract -> agent -> 有工具调用? -> tools -> 未到 30 轮? -> agent
                                         没有 -> pending_verify -> END
                                                         已经 30 轮 -> END
```

`llm.chat` 返回模型消息和 `finish_reason`，并带 `max_tokens=8000`。`agent_node` 把 `finish_reason` 写入状态字段 `provider_finish_reason`。`tools_node` 不调用模型，也不覆盖这个字段，所以上一轮记下的值会留在最终状态里。

`agent_node` 用 `stop_reason_for` 决定这一轮是继续还是停止。有工具调用就继续。没有工具调用时，`finish_reason == "length"` 记为 `max_tokens`，其它记为 `final_answer`。`turns` 只统计这一次用户请求里的模型调用次数，每次 `invoke` 都从 0 开始。上限 `MAX_TURNS` 是 30。第 30 次如果还要工具，工具会执行并写入消息，然后停止，不再请求模型。这时 `stop_reason` 是 `max_turns`，`provider_finish_reason` 仍是第 30 次模型调用留下的值。

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
| `read_artifact` | 按行段读取外部化到磁盘的完整工具结果，单次正文最多 30000 字符 | 直接调用 | 可以 |
| `skill` | 按名字加载一份技能。结果只有「已加载」，正文进下一次系统提示 | 直接调用 | 按顺序 |
| `tool_search` | 按 `names` 或 `query` 查找延迟工具，把完整参数写入结果 | 直接调用 | 按顺序 |
| `defer_execute` | 执行已经搜索过的延迟工具。参数是 `name` 和 `arguments` | 直接调用 | 按顺序 |
| `write_file` | 直接覆盖写入。父目录不存在会创建 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `run_command` | `subprocess` 执行字符串列表，工作目录限制在工作区内，超时 30 秒 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `write_memory` | 把一条事实追加到工作区记忆日志。来源固定为 `model_tool`。不蒸馏 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `update_user_profile` | 按字段更新用户档案。没出现的字段保持原样。`null` 删掉该字段 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `save_user_preference` | 按 key 保存一条跨项目偏好。来源固定为 `model_tool`。不能提交事件编号或证据时间 | 先 `tool_search`，再 `defer_execute` | 按顺序 |
| `forget_user_preference` | 按 key 遗忘一条跨项目偏好。来源固定为 `model_tool`。删除时间由程序决定 | 先 `tool_search`，再 `defer_execute` | 按顺序 |

`tool_search` 和 `defer_execute` 的具体逻辑在 `search` 和 `execute_deferred`。`dispatch` 看到这两个名字就转去调用它们，不走登记记录上的 `handler`。那两个 `handler` 只是占位，因为每条 `ToolSpec` 都必须有处理函数。`handler` 只能看到工作区和参数，看不到当前会话已经加载过哪些名字。

已加载工具名单在图的状态字段 `loaded_tools` 里，已加载技能在 `loaded_skills` 里。`tools_node` 每次执行前读出它们，传给 `dispatch_many`，执行完再写回去。`run_chat` 的 `invoke` 输入不要带这两个字段，否则会把这条会话已经搜索过的工具和已经加载的技能清掉。没搜索过就调用 `defer_execute`，会得到先去 `tool_search` 的提示。`read_file` 这类直接工具如果走 `defer_execute`，会得到直接调用的提示。同一个名字第二次搜索，结果里标记为已加载。

处理函数里的路径再经过 `reslove_inside`：解析后的绝对路径必须等于工作区，或者位于工作区里面。`..` 会在这里被拒绝。权限阶段用的是 `path_is_inside`，规则相同，但只返回能不能通过，不抛异常。

命令输出按 UTF-8 解码，无法解码的字节换成占位符。找不到程序时返回「找不到命令」，不会把整个请求打崩。Windows 上经常没有 `python3`、`cat`、`xxd`。

`run_command` 的当前目录在工作区内，但命令本身仍可以读写工作区以外的路径。所以普通命令只询问，不自动允许。危险词检查只看拼出来的命令文字，挡不住所有副作用。

## 权限

逻辑在 `agent/permission.py`。一次工具执行过三道，后面的不改前面的结论。

```text
dispatch 检查完参数
  -> 技能声明：没加载技能时跳过；加载后，工具必须出现在声明里
  -> MCP 还要已信任、已连接，并且声明它的技能允许网络
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
| 3 | `read.allow` | 允许 | 工作区内的 `list_dir`、`read_file`，以及 `tool_search`、`read_artifact`、`skill` |
| 4 | `path.write_requires_approval` | 询问 | 工作区内的 `write_file` |
| 5 | `command.hard_deny` | 拒绝 | 命令文字命中 `sudo`、`rm -rf`、关机、格式化、典型 `dd` 设备写入 |
| 6 | `command.requires_approval` | 询问 | 其余 `run_command` |
| 7 | `memory.write_requires_approval` | 询问 | `write_memory`。同意后才追加日志 |
| 8 | `profile.write_requires_approval` | 询问 | `update_user_profile`。同意后才改档案 |
| 9 | `preference.write_requires_approval` | 询问 | `save_user_preference`。同意后才保存偏好 |
| 10 | `preference.forget_requires_approval` | 询问 | `forget_user_preference`。同意后才遗忘 |
| 11 | `mcp.requires_approval` | 询问 | 名字以 `mcp__` 开头的外部工具。同意后才 `tools/call` |
| 12 | `default.deny` | 拒绝 | 没有规则覆盖的工具名 |

技能声明挡在 `decide` 前面。没声明、路径不在技能允许范围内、连接器未信任或未连接，都是直接拒绝，不会出现同意按钮。`skill`、`tool_search`、`defer_execute` 不收进技能声明。

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

## 技能

逻辑在 `agent/skills.py`。项目技能放在 `{WORKSPACE_DIR}/.dd-coder/skills/<名字>/SKILL.md`，用户技能放在 `data/skills/<名字>/SKILL.md`。同名时用项目里的那份。文件头只认 `title`、`summary`、`read_when`、`agent_created`、`permissions`。`permissions` 里是 `tools`、`network`、`paths.read`、`paths.write`。未知字段、绝对路径、带 `..` 的路径，这份技能不进目录。目录名必须和 `title` 一致。

`agent_node` 每次只把目录放进系统提示：名字、摘要、触发词。模型调用 `skill` 才把正文和权限快照写进 `loaded_skills`。快照留在这个 `thread_id` 的检查点里。之后文件改了，已经加载的快照不变。第二次加载同一个名字，不再读盘。

没加载技能时，现有工具的允许和询问不变。加载之后，要执行的工具必须出现在已加载技能 `tools` 的并集里。`read_file`、`list_dir` 看 `paths.read`，`write_file` 看 `paths.write`。命中其中一份声明了该工具的技能即可。`network` 只约束 MCP 工具。

## MCP

逻辑在 `agent/connectors.py`。配置是 `data/mcp.json` 的 `mcpServers`，每项有 `command`、`args`、`env`。信任写在 `data/connector_trust.json`，例如 `{"github": {"trusted": true}}`。信任不是模型工具。未信任的进程不启动。

已信任的连接器在搜索或组装提示时按 MCP stdio 启动：`Content-Length` 帧、`initialize`、`tools/list`。每个服务器一个子进程，留在服务进程里，不写进检查点。发现的工具登记为延迟工具，名字是 `mcp__<服务器>__<工具名>`。模型先 `tool_search`，再 `defer_execute`。执行前再次检查信任、连接和技能声明，通过后仍要询问，同意才发 `tools/call`。`env` 不进提示和工具结果。调用失败或进程退出后，同一次调用最多再启动一次。

没有技能声明、或者声明了但 `network` 不是 `true`，这些工具不出现在搜索结果里。

## 模型路由

逻辑在 `agent/routing.py`。任务名直接对应层级，不根据用户那句话猜测。

| 任务 | 层级 | 工具 |
| --- | --- | --- |
| `agent` | craft | 可以带现有工具列表 |
| `title` | lite | 不传工具 |
| `pending_extract` | lite | 不传工具 |
| `pending_verify` | lite | 不传工具 |
| `context_summary` | lite | 不传工具 |

`agent_node` 调用 `route_task(settings, "agent")`，复制一份配置，把 `model` 换成路由结果，再交给 `chat()`。`chat()` 的参数不变，它仍然读 `config["model"]`。状态字段 `model_tier` 和 `model_name` 记下这一次；`tools_node` 不覆盖它们。`run_chat` 和 `resume_chat` 把这两个字段放进返回值，`/chat` 和 `/approve` 再带回页面。

`write_title_if_absent` 在没有 `pending`、并且日志里还没有 `ai-title` 时，用 `complete_text()` 走 lite。这次调用不进入 30 轮计数。失败只打印到跑服务的终端，不把已经生成的回复变成错误。

`agent_node` 在调用 `chat()` 之前，用 `plan_prompt()` 组装系统提示，再用 `compact_context()` 压缩 `state["messages"]` 的深拷贝。`chat()` 收到的是系统片段加压缩副本，第三个参数是这一轮的 `REGISTRY.openai_tools()`。返回状态时仍用原来的 `state["messages"]` 加上新的助手消息。系统片段和压缩副本不进 checkpoint，也不进历史栏。聊天不调用 `distill()`。

可选片段里还有技能目录、已加载技能正文，以及当前看得见的延迟工具名单。技能正文的预算优先级高于记忆。正文放不下时，改成一句「已加载但超出预算，正文未注入」。

## 未决任务

逻辑在 `agent/pending.py`，状态放在当前 `thread_id` 的 checkpoint 里。`pending_items` 和 `processed_pending_source_ids` 跨提问保留，`run_chat` 的 `invoke` 输入不能带这两个字段。`pending_completion_proposals` 每次新用户消息重置为空；审批恢复不重置。

一条任务有 `pending:<12 hex>` 编号、描述、`open` / `blocked` / `done`、阻塞原因、创建来源和最近一次变化来源。`blocked` 必须有原因。`done` 是终态。模型不能直接修改任务，也不能生成任务编号。

`pending_extract` 只在新的用户消息进入图时运行一次。它把当前用户 Transcript 事件、时间和 `open` / `blocked` 任务交给 lite 模型。模型只能建议 `create`、`block`、`open` 或 `no_change`。Harness 校验后才写入 `pending_items`，并把来源事件放进 `processed_pending_source_ids`。同一个事件再次进入节点时不再调用模型。模型失败或 JSON 不合法时，任务保持原样，编码循环继续。

`agent` 没有工具调用时进入 `pending_verify`。没有 `open` 任务就不调用模型。verifier 只能提出完成建议，建议先放在 `pending_completion_proposals`。图结束且没有审批暂停时，`record_visible_messages()` 先写工具结果，`commit_pending_done()` 再核对：任务仍是 `open`，`call_id` 属于本轮，结果事件真实存在，并且结果不是 `未执行:`。通过后才把状态改成 `done`，`done_evidence` 保存工具结果的 `event_id`。`blocked` 不能直接完成。审批恢复从 `tools` 继续，不重新提取任务；暂停期间不提交 `done`。

这两次 lite 调用不增加 `turns`，也不覆盖 `model_tier` 和 `model_name`。`run_chat` 会在进图前写用户消息；如果图随后失败，Transcript 仍保留这条已收到的消息。

## 上下文预算

组装在 `agent/prompt_assembly.py`，压缩在 `agent/context_compact.py`。两边共用一次请求的字符预算，但改的不是同一份数据。

系统侧片段：

| 片段 | 策略 |
| --- | --- |
| `base` | 必选。身份、工作区边界、权限边界 |
| `tools` | 必选预算占用。不写进系统文本，工具列表仍走 `chat(..., TOOLS)` |
| `user_memory` | 可选。来自 `UserMemory.get_context_for_agent()` |
| `workspace_memory` | 可选。来自 `WorkspaceMemory.get_context_for_agent()` |
| `pending` | 可选。有 `open` 或 `blocked` 时优先装箱 |

`PROMPT_BUDGET_CHARS` 是 12000。必选片段超预算抛 `PromptBudgetError`，不请求编码模型。可选片段装不下就整段丢掉，不切半段。空记忆不产生片段。

对话侧只压缩深拷贝。`estimate_messages()` 用 JSON 字符数。达到 `MESSAGE_SOFT_THRESHOLD`（80000）才进入四层：截断超长工具结果、同一文件多次 `read_file` 只留最新一对、修剪旧消息并删掉跨边界的工具对、用 lite 任务 `context_summary` 摘要更旧的消息。摘要失败或没有变短，就保留这一层拿到的副本。四层后仍达到 `MESSAGE_HARD_LIMIT`（120000）抛 `MessageViewLimitExceeded`。系统片段、压缩副本和 `TOOLS` 合计达到 `REQUEST_HARD_LIMIT`（140000）抛 `RequestBudgetExceeded`。这两种错误都不请求编码模型。

`context_summary` 不增加 `turns`，也不覆盖 `model_tier` 和 `model_name`。审批恢复后，下一次 `agent_node` 会重新组装、重新压缩。

## 工具结果外部化

逻辑在 `agent/tool_results.py`。`tools_node` 在 `dispatch_many` 返回后、结果写回 `messages` 前检查每条结果：`run_command` 超过 30000 字符，其它工具按 UTF-8 字节超过 50KB，就把完整原文写到 `data/tool-results/<thread_id>/tool_result_NNN.txt`，messages 里改放指针。这是入口控制：大结果从源头就不进 checkpoint，和 `compact_context` 在发送副本上截断是两层不同的防线。

指针包含工具名、原始字符数、SHA-256、文件绝对路径，以及开头 6144 字符和结尾 24576 字符的预览；刚过阈值时结尾预览从开头预览的末尾接着开始，不重复同一段内容。替换发生在写回 `messages` 之前，所以 SQLite checkpoint、后续每一轮、页面 `events` 和 Transcript 的 `function_call_result` 记录的都是指针文本，完整原文只在产物文件里。

文件名从 `tool_result_001.txt` 起用独占创建分配：文件已存在就试下一个编号，服务重启后计数重来也不会覆盖旧证据。写盘用 `newline=""`，磁盘字节和原始结果一致，指针对得上文件；写完 `fsync`。产物目录名复用 Transcript 的 `thread_id` 字符规则，拼路径不会越界。磁盘写入失败（`OSError`）时打印到终端，本轮结果按原文进 messages，不丢掉这次结果。

模型用 `read_artifact` 读回原文，参数是 `path`、`offset`、`limit`（行号 0 起，默认 500 行，最多 2000 行），返回行段加 `[文件名: 第 X-Y 行, 共 N 行]` 的头。`path` 可以是指针里的绝对路径，或相对结果目录的路径；解析后必须仍在 `data/tool-results/` 里面，否则返回错误文字，指针不能变成任意文件读取入口。单次返回正文最多 30000 字符，超出截断并提示缩小 `limit`——这个上限保证读回的结果永远不会再次触发外部化。权限上它是只读工具，走 `read.allow`，直接调用，可以并发；它的路径边界由处理函数检查，权限层不看。

审批暂停发生在权限阶段、工具执行之前，所以被询问的工具不会产生半截产物。但恢复后 `tools_node` 整体重跑，同一轮里暂停前已经外部化的只读结果会再写一份新编号文件，旧产物留在磁盘上。

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

`run_chat` 在进图前把用户消息写成 `type: message`，`append()` 返回带 `event_id` 的完整事件。图返回后，`record_visible_messages` 跳过用户消息，只补带文字的助手消息、`function_call` 和 `function_call_result`。`resume_chat` 没有新的用户消息，只补这次恢复新增的内容。写在 `tools_node` 里面会在恢复审批时记重，所以不要挪到节点里。

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
- 恢复暂停时 `tools_node` 从头再跑。同一轮暂停前的只读工具会再执行一次；已经外部化的结果会再写一份新编号产物，旧产物成为孤儿。
- `read_artifact` 按行分页。单行就超过 30000 字符时（比如压成一行的 JSON），只能截断返回，那部分内容模型读不回来。
- 外部化产物没有清理机制，会话删除后文件仍留在 `data/tool-results/`。
- 命令危险词只检查拼出来的文字，不是沙盒。`run_command` 仍可能访问工作区以外的路径，所以普通命令一律询问。
- 聊天不调用 `distill()`。当天用 `write_memory` 写下的带 `memory_key` 的事实，要另一次蒸馏之后才会进入 `MEMORY.md`。
- 打平的两个答案只出现在 `DistillReport.tied` 里。重启后没有一份待审案子，也没有五阶段事务日志。

## 下次可以做的事

还没做，也不要当成已经答应的范围：

- 工作区记忆的冲突队列、人工裁决，以及多文件事务恢复。
- 外部化产物接入记忆的瘦引用，以及租约、对账和两阶段清理（s13 后半）。
- 删除会话，以及按时间而不是按 id 排列侧边栏。
- 流式输出：边生成边显示回复和工具记录。
- 工作区文件树。
- 清理未使用的 `complete()`。
