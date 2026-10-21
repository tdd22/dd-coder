# 上下文预算设计：Prompt Assembly + Context Compact

## 目标

让每次 `agent_node` 调用模型前，系统提示和对话历史都落在可控预算内，同时不污染 checkpoint、Transcript、用户记忆、工作区记忆和 pending。

本设计把两件事放在同一份边界里，分两次落地：

1. **Prompt Assembly**：选择并装箱系统侧片段。
2. **Context Compact**：有损压缩发给模型的 messages 副本。

第一版不搬教程整套 `DurableContextState`，不实现召回证据，不实现 Skills / MCP 片段。

## 为什么要一起设计

发给模型的请求现在是三块：

| 块 | 当前来源 | 是否进 checkpoint |
| --- | --- | --- |
| 系统文字 | `UserMemory`、`WorkspaceMemory`、`render_pending_context()` 临时拼进 messages 前面 | 否 |
| 对话 messages | `state["messages"]` | 是 |
| 工具定义 | 模块常量 `TOOLS = REGISTRY.openai_tools()`，作为 `chat()` 第三个参数 | 否 |

压缩只改对话侧，组装只改系统侧。两边共用总预算。只做压缩，记忆和 pending 仍可能撑爆 system；只做组装，长对话工具结果仍可能撑爆 messages。

## 所有权边界

| 对象 | 谁拥有 | 压缩能否有损改 | 组装能否裁剪 |
| --- | --- | --- | --- |
| Transcript JSONL | 审计真相 | 否 | 否 |
| checkpoint `messages` | 图运行时 | 否；只压深拷贝 | 否 |
| `pending_items` | Harness | 否 | 可按渲染预算折叠 `done` |
| 用户记忆 / 工作区记忆 | 各自 store | 否 | 可选片段可整段舍弃 |
| 临时 system 文字 | 本轮 Prompt 视图 | 否；它不是 messages 压缩对象 | 是 |
| `TOOLS` | 工具注册表 | 否 | 第一版不裁；计入总预算估算 |

模型不能改记忆、不能改 pending、不能决定“哪些安全规则可删”。摘要模型只能总结旧对话副本；摘要失败时保留原副本，不能用错误字符串覆盖历史。

## 现有接入点

改动集中在 `agent_node` 调用 `chat()` 之前。不新增图节点，不改变 `pending_extract → agent ↔ tools → pending_verify` 边。

当前代码顺序：

```text
messages = state["messages"]
拼 user / workspace / pending 到 prefix
messages = prefix + messages
chat(routed, messages, TOOLS)
写回 state["messages"] + assistant
```

升级后顺序：

```text
1. 收集候选片段（基础规则、用户记忆、工作区记忆、pending）
2. Prompt Assembly 按预算选出 system_messages
3. working = deepcopy(state["messages"])
4. Context Compact 按阈值压缩 working
5. 估算 system + working + TOOLS；仍超硬上限则 fail-closed
6. chat(routed, system_messages + working, TOOLS)
7. 写回仍用 state["messages"] + assistant
   system_messages 与 working 都不进 checkpoint
```

`TOOLS` 继续由 `graph.py` 模块常量传入 `chat()`，不塞进 messages，也不进压缩管线。

## 共享预算模型

第一版用字符估算，不依赖特定 tokenizer：

```text
estimate(text) = len(text)
estimate_messages(messages) = 所有 role/content/tool_calls 序列化后的字符数
estimate_tools(tools) = json.dumps(tools) 的字符数
```

预留精确 tokenizer 注入点，但默认离线可测。

两档阈值：

| 名称 | 作用对象 | 建议初值 | 行为 |
| --- | --- | --- | --- |
| `MESSAGE_SOFT_THRESHOLD` | messages 副本 | 80_000 字符 | 达到后启动压缩层 |
| `MESSAGE_HARD_LIMIT` | 压缩后的 messages 副本 | 120_000 字符 | 仍超则抛错，不请求 provider |
| `PROMPT_BUDGET_CHARS` | system 片段合计 | 12_000 字符 | 组装时装箱 |
| `REQUEST_HARD_LIMIT` | system + messages + tools | 140_000 字符 | 最终准入；超则 fail-closed |

说明：

- messages 软/硬阈值只数对话副本，不含 system，不含 tools。
- `PROMPT_BUDGET_CHARS` 只数组装后的 system 文本。
- `REQUEST_HARD_LIMIT` 是一次 provider 请求的总准入边界；它不是模型官方上下文窗口的精确值。

## Prompt Assembly

### 文件

新增 `agent/prompt_assembly.py`。

负责：

- `PromptSegment`
- `plan_prompt(...)`
- 决策日志 `SegmentDecision`

不负责：

- 读磁盘记忆（调用方传入已渲染文本）
- 压缩 messages
- 调用模型

### 第一版片段

| 片段 | 来源 | 策略 |
| --- | --- | --- |
| `base` | 固定短规则：身份、工作区边界、权限边界 | required |
| `tools` | 不进入 system 字符串；`TOOLS` 仍走 API `tools=` | required 预算占用 |
| `user_memory` | `UserMemory().get_context_for_agent()` | optional，高价值 |
| `workspace_memory` | `WorkspaceMemory(...).get_context_for_agent()` | optional，高价值 |
| `pending` | `render_pending_context(pending_items)` | optional，高价值；有 open/blocked 时优先保留 |

展示顺序与预算价值分开：

- `priority`：最终拼进 Prompt 的先后。
- `budget_priority`：预算紧时谁先占空间。

流程：

```text
构建片段并记录 provenance
  → 先预留 required（base + tools 占用）
  → required 超预算：PromptBudgetError，不请求模型
  → optional 按 budget_priority 尝试加入
  → 装不下的整段舍弃，记 SegmentDecision
  → 已选片段按 priority 渲染成 system messages
```

规则：

- 片段是原子块，不做半段截断。
- 空片段不进入计划。
- 没有命中记忆时不输出空的 `<memory>` 壳。
- `pending` 的 `done` 折叠仍由现有 `render_pending_context()` 负责；组装只决定整段放不放。
- 第一版不做 S15 的召回候选选择链；记忆文本沿用现有 `get_context_for_agent()` 全文。后续接召回时，再在组装前增加 selection。

### 与 agent_node 的接口

```text
segments = [
  base_segment(),
  tools_budget_segment(TOOLS),   # 只占预算，不产出 system 文本
  optional_segment("user_memory", user_context),
  optional_segment("workspace_memory", workspace_context),
  optional_segment("pending", pending_context),
]
plan = plan_prompt(segments, budget_chars=PROMPT_BUDGET_CHARS)
system_messages = plan.system_messages
```

`plan` 同时返回 `decisions`，便于测试和日志，第一版不必返回给浏览器。

## Context Compact

### 文件

新增 `agent/context_compact.py`。

负责：

- token/字符估算
- 工具协议校验（OpenAI 风格 `tool_calls` / `role=tool`）
- L1 截断、L2 去重、L3 修剪、L4 摘要
- `compact_context(messages) -> CompactionResult`

不负责：

- 改 pending / 记忆
- 写 Transcript
- 组装 system
- 直接调用 craft 模型（L4 摘要走 lite，由调用方注入 summarizer）

### 压缩对象

只压缩 `deepcopy(state["messages"])`。

不进入压缩：

- system 片段
- `TOOLS`
- `pending_items`
- 用户记忆与工作区记忆原文
- Transcript

### 四层策略

沿用 s14 的轻重顺序，适配 OpenAI messages 形状：

| 层 | 做什么 | 停条件 |
| --- | --- | --- |
| L1 | 单条 tool 结果超过上限则截断并标注原始长度 | 低于软阈值则停 |
| L2 | 同一文件多次 `read_file`，只留最新完整 tool_call + tool 结果对 | 同上 |
| L3 | 保留最近 N 条，删除更旧消息；跨越边界的工具对整组移除 | 同上 |
| L4 | lite 模型摘要旧消息，保留最近窗口；失败或空摘要则原样返回 | 同上 |

硬上限：四层后仍 `>= MESSAGE_HARD_LIMIT`，抛 `MessageViewLimitExceeded`，不请求 craft 模型。

协议不变量：

- 每个非空 `tool_calls[].id` 必须对应稍后一条 `role=tool` 且 `tool_call_id` 相同的消息。
- 压缩删除工具交互时，调用与结果成对删除。
- 压缩入口先校验；不合法历史直接抛错，即使未达软阈值也不送 provider。

L4 summarizer：

- 路由任务 `context_summary -> lite`
- 不计入 `turns`
- 不覆盖响应里的 agent `model_tier` / `model_name`
- 摘要只看旧 messages，不接收 pending 或记忆正文

### 与 agent_node 的接口

```text
working = deepcopy(state["messages"])
result = compact_context(working, summarizer=lite_summarizer)
working = result.messages
if estimate(system) + estimate(working) + estimate(TOOLS) >= REQUEST_HARD_LIMIT:
    raise RequestBudgetExceeded
chat(routed, system_messages + working, TOOLS)
```

## 总流程（一次 agent 模型调用）

```text
pending_extract 已更新 pending_items
        │
        ▼
agent_node
  读 user_context / workspace_context / pending_context
        │
        ▼
plan_prompt(...)
  得到 system_messages + segment decisions
        │
        ▼
compact_context(deepcopy(state["messages"]))
  得到 working + compaction layers
        │
        ▼
检查 REQUEST_HARD_LIMIT
        │
        ▼
chat(routed, system_messages + working, TOOLS)
        │
        ▼
写回 state["messages"] + assistant
system / working / 决策日志不进 checkpoint
```

审批恢复从 `tools` 继续时，下一次进入 `agent_node` 会重新组装、重新压缩。这是有意的：pending 或记忆可能已变，且压缩结果本就不持久化。

## 错误处理

| 情况 | 行为 |
| --- | --- |
| required 片段超过 `PROMPT_BUDGET_CHARS` | `PromptBudgetError`，本轮不请求 craft |
| 压缩后 messages 仍超硬阈值 | `MessageViewLimitExceeded`，本轮不请求 craft |
| system + messages + tools 超总硬阈值 | `RequestBudgetExceeded`，本轮不请求 craft |
| L4 摘要失败或空 | 保留压缩前一层的 messages，节省量记 0 |
| 工具协议损坏 | 抛协议错误，不请求 craft |
| 组装或压缩抛错冒到 `run_chat` | 已写入的用户 Transcript 事件保留；不伪造助手回复 |

第一版可以把这些错误变成 `run_chat` 返回的明确失败信息；不要静默删掉 base 规则继续调用。

## 实现顺序

### 第一次落地：Prompt Assembly

1. 新增 `agent/prompt_assembly.py` 与纯函数测试。
2. `agent_node` 改为走 `plan_prompt`，去掉手写三条 prefix 拼接。
3. 增加短 `base` 规则片段。
4. `TOOLS` 计入预算占用，但不改 API 传参方式。
5. 更新 `docs/DEVELOPMENT.md`。

此时还不压缩 messages。

### 第二次落地：Context Compact

1. 新增 `agent/context_compact.py` 与四层测试。
2. 增加 `context_summary` lite 路由。
3. `agent_node` 在组装之后、`chat()` 之前压缩副本。
4. 接上总硬阈值检查。
5. 更新 `docs/DEVELOPMENT.md`。

## 测试

### Assembly

1. required 超预算 fail-closed。
2. 预算紧时先丢掉低优先级 optional，保留 base。
3. 空记忆不产生空片段。
4. pending / user / workspace 文本出现在临时 system 中，但不写回 `state["messages"]`。
5. `TOOLS` 影响预算，但不出现在 system 文本里。

### Compact

6. 低于软阈值返回深拷贝，层列表为空。
7. L1 截断超大 tool 结果。
8. L2 同一文件多次读取只留最新一对。
9. L3 不留下半条 tool_call / tool 结果。
10. L4 失败时保留原 messages。
11. 压缩后仍超硬阈值抛错。
12. checkpoint messages 与 Transcript 不被压缩修改。

### 集成

13. `turns` 不被 L4 增加。
14. agent 响应里的 `model_tier` / `model_name` 仍是 craft。
15. 审批恢复后下一次 `agent_node` 重新组装和压缩。
16. 现有 `test_pending.py`、`test_stop.py` 继续通过。

## 第一版不做

- 不引入教程 `DurableContextState` / `SourcePointerResolver` / `retrieval_evidence`。
- 不做记忆召回的 scope / conflict / top-k 选择链。
- 不裁剪 `TOOLS` 列表。
- 不把压缩结果写回 checkpoint。
- 不做增量摘要持久化。
- 不做 Skills、MCP、专家、区域等额外片段。
- 不在页面展示预算或压缩日志。

## 与 pending 设计的关系

`docs/superpowers/specs/2026-10-15-pending-items-design.md` 已落地的 pending 生命周期保持不变。

本设计只消费 `render_pending_context()` 的输出作为可选 Prompt 片段。压缩不能把 pending 标成 done；完成仍只走 verifier + Harness 证据提交。
