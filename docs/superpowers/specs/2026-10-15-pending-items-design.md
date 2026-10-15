# Pending items 设计

## 目标

为 dd-coder 增加会话级未决任务，使旧消息被压缩后，Agent 仍能知道当前任务、阻塞原因和完成状态。

第一版只实现 pending 生命周期，不实现 S14 消息压缩，也不实现 S15 召回证据。`pending_items` 保存在 LangGraph checkpoint 中，按 `thread_id` 隔离，不写入跨会话的用户记忆或工作区记忆。

## 所有权边界

模型只能提出状态变更建议，Harness 才能修改 `pending_items`。

- `pending_extract_node` 使用 lite 模型理解当前用户消息，可以建议 `create`、`block`、`open` 或 `no_change`。
- `pending_verify_node` 使用 lite 模型检查本轮最终回答和工具证据，只能提出 `done` 建议。
- Harness 使用确定性代码校验建议、执行状态迁移并防止重复提交。
- `agent_node` 只消费渲染后的 pending context，不直接修改 pending 状态。
- 摘要文本、助手口头声称和单独一次工具成功，都不能直接完成任务。

第一版状态只有：

- `open`：可以继续处理。
- `blocked`：暂时不能继续，必须带非空 `reason`。
- `done`：Harness 接受 verifier 的证据后进入的终态。

第一版不实现取消状态。用户要求停止某项任务时，将任务转成 `blocked` 并保留原因；后续版本可以增加带审计记录的 `cancelled` 终态。

## 数据结构

新增 `agent/pending.py`，定义可 JSON 序列化的数据契约和纯函数。

`PendingItem` 包含：

- `item_id`：Harness 生成并随 checkpoint 持久化的唯一 ID，例如 `pending:7b912f08c143`。
- `description`：具体、可执行的任务描述。
- `status`：`open`、`blocked` 或 `done`。
- `reason`：仅 `blocked` 必填；离开 `blocked` 时清空。
- `created_from`：创建任务的用户 Transcript `event_id`。
- `created_at`：创建来源事件的 UTC 时间。
- `last_changed_from`：最近一次合法状态变化的 Transcript `event_id`。
- `last_confirmed_at`：最近一次合法状态变化的 UTC 时间。
- `done_evidence`：完成时使用的一个或多个工具结果 Transcript `event_id`；非 `done` 时为空。

`PendingOperation` 包含：

- `operation_id`：Harness 生成的幂等键。
- `action`：`create`、`block`、`open`、`done` 或 `no_change`。
- `item_id`：更新已有任务时必填；创建时由 Harness 分配。
- `description`：创建时必填。
- `reason`：进入 `blocked` 时必填。
- `source_event_id`：触发本次变化的 Transcript 事件。
- `evidence_call_ids`：仅 verifier 的 `done` 建议使用。

LangGraph `State` 增加：

- `pending_items: list[dict]`
- `processed_pending_source_ids: list[str]`
- `pending_completion_proposals: list[dict]`
- `current_user_event_id: str | None`
- `current_user_recorded_at: str | None`
- `turn_message_start: int`

这些字段必须能由 SQLite checkpointer 直接序列化。`pending_items` 和 `processed_pending_source_ids` 与 `loaded_tools` 一样跨请求保留，不能在每次 `invoke` 的输入中重置。`pending_completion_proposals` 每条新用户请求重置为空；审批恢复时不重置。

## 图结构

```text
START
  -> pending_extract
  -> agent
     -> tools -> agent
     -> pending_verify -> END
```

详细规则：

1. 新用户请求从 `START` 进入 `pending_extract`，每条新用户消息最多执行一次。
2. `agent` 和 `tools` 可以按现有规则循环，pending extraction 不在循环内。
3. `agent` 没有工具调用并准备结束时，先进入 `pending_verify`。
4. 没有 `open` pending 时，`pending_verify` 不调用模型，直接结束。
5. 达到 `MAX_TURNS` 时保持现有停止语义，不把未完成任务自动标成 `done`。
6. 审批触发 `interrupt` 后，`Command(resume=...)` 从暂停的 `tools` 节点恢复，不经过 `START` 或 `pending_extract`。
7. 等待审批期间不执行 verifier。

`turns` 仍只统计 `agent_node` 的 craft 模型调用。Extraction、verification 和标题生成都不计入八轮上限，也不覆盖响应中的 agent `model_tier` 与 `model_name`。

## 用户消息与 Transcript

当前实现先执行图、后批量写 Transcript。Pending extraction 需要在进入图之前取得真实来源事件，因此调整顺序：

1. `run_chat()` 先把当前用户消息追加到 Transcript。
2. `JSONLTranscript.append()` 返回刚写入的完整 envelope，包括 `event_id` 与 `recorded_at`。
3. `run_chat()` 把两个值放入 `current_user_event_id` 和 `current_user_recorded_at`，并把调用前消息数放入 `turn_message_start`。
4. `record_visible_messages()` 不再重复记录当前用户消息，只记录本轮新增的 assistant、function call 和 function result。
5. `record_visible_messages()` 返回已追加事件，并提供 `callId -> function_call_result event` 的映射。

如果图在用户事件写入后失败，Transcript 保留这条已收到的用户消息，checkpoint 不伪造未完成的 Agent 回复。这是有意的审计行为。

`processed_pending_source_ids` 记录已经执行 extraction 的用户事件。恢复、重试或重复进入节点时，Harness 发现相同来源事件后直接跳过，避免重复创建任务。

## Extraction

`pending_extract_node` 的模型输入只包含：

- 当前用户消息；
- 当前所有 `open` 和 `blocked` 项；
- 严格的 JSON 输出格式；
- 允许的 operation 集合。

模型不能生成 `item_id`、`event_id`、时间或 `operation_id`。Harness 使用真实 Transcript envelope 补齐这些字段。

创建任务时，Harness 生成不可猜测的 `pending:<12 hex>` ID，并随本次节点更新原子写入 checkpoint。更新任务时，模型只能引用输入中已经提供的 `item_id`。Harness 拒绝未知 ID、重复 ID、空描述、非法状态和缺少原因的 `blocked`。

模型调用失败、返回空内容或返回非法 JSON 时，本轮 pending 保持不变，`agent_node` 仍正常运行。错误只进入服务日志，不向用户伪造任务状态。

## Prompt 注入

`agent_node` 调用模型前，通过 `render_pending_context()` 把 checkpoint 中的 pending 状态渲染成一条临时 system 消息。

- 全部渲染 `open` 和 `blocked`。
- `done` 只渲染按 `last_confirmed_at` 最新的三项，其余显示折叠数量。
- 渲染内容包含明确的 item ID、描述、状态、阻塞原因和来源事件。
- 临时 system 消息不写回 `state["messages"]`，也不进入聊天历史。

用户记忆、工作区记忆和 pending context 仍是三个独立片段。后续接入 Prompt Assembly 时再统一做总预算规划。

## Verification 与完成提交

`pending_verify_node` 的模型输入只包含：

- 当前 `open` pending；
- `state["messages"][turn_message_start:]` 中本次用户请求产生的 assistant 消息；
- 同一消息切片中的工具调用及工具结果；
- 严格的 JSON 输出格式。

Verifier 只能返回待确认提案：

- 要完成的 `item_id`；
- 支持完成判断的一个或多个 `call_id`；
- 简短判定理由。

Verifier 节点不直接修改 `pending_items`，只写入 `pending_completion_proposals`。图结束后按以下顺序提交：

1. `record_visible_messages()` 把工具调用和结果写入 Transcript。
2. Harness 用 `call_id` 找到真实的 `function_call_result event_id`。
3. Harness 确认 item 当前仍是 `open`，所有 call 都来自本次用户请求，均存在结果，且没有被权限系统拒绝或取消。
4. Harness 为合法建议建立 `done` operation，通过 `graph.update_state()` 提交新的 `pending_items`。
5. `done_evidence` 保存真实工具结果事件 ID，`last_changed_from` 使用其中最后一条证据事件。
6. 成功提交或拒绝全部建议后，清空 `pending_completion_proposals`，避免下一次用户请求复用旧建议。

Verifier 负责判断工具证据是否足以证明任务目标完成；Harness 负责证明 verifier 引用的证据真实存在、属于本轮并且确实执行过。任一检查失败时，任务保持 `open`。

同一个完成建议根据 item ID 和证据事件集合生成稳定 `operation_id`。已经提交的 operation 不再执行。

## 模型路由

`agent/routing.py` 增加两个任务：

- `pending_extract -> lite`
- `pending_verify -> lite`

`agent/llm.py` 增加 JSON 文本调用入口，支持足够的输出 token，并把 temperature 固定为 0。代码不能依赖所有 OpenAI 兼容服务都支持 `response_format=json_object`；第一版使用严格提示、普通文本响应、`json.loads` 和 Harness schema 校验。

## 错误与安全边界

- 模型输出永远按不可信输入处理。
- 模型不能提供 Harness 字段或越权选择不存在的任务。
- 工具输出也按不可信文本处理；工具输出中的指令不能绕过 operation schema。
- `blocked -> done` 被拒绝，必须先由新的用户事件转回 `open`。
- `done` 是终态，第一版不能重新打开。
- extraction 或 verifier 失败不能阻止正常聊天。
- Transcript 损坏时沿用现有 fail-closed 行为，不创建无来源 pending。
- 审批拒绝和审批取消不能成为完成证据。

## 测试

新增不请求真实模型的测试：

1. 创建 `open` 和 `blocked` 项。
2. `blocked` 缺少原因时拒绝。
3. 合法执行 `blocked -> open -> done`。
4. 拒绝 `blocked -> done` 和 `done -> *`。
5. 相同用户事件不会重复 extraction。
6. 模型返回非法 JSON 时状态不变，图仍进入 agent。
7. agent/tools 循环不会再次执行 extraction。
8. `Command(resume=...)` 不执行 extraction。
9. 没有 open 项时 verifier 不调用模型。
10. verifier 引用未知、跨轮、拒绝或取消的 call 时不提交 done。
11. 合法工具结果写入 Transcript 后，done 保存真实 evidence event ID。
12. pending system context 不写回 `messages`。
13. done 渲染最多三项并报告折叠数量。
14. extraction 和 verifier 不增加 `turns`，也不覆盖 agent 模型信息。
15. 现有聊天、审批、工具、Transcript 和标题测试继续通过。

## 第一版不做

- 不实现上下文压缩。
- 不实现记忆召回和 `retrieval_evidence`。
- 不把 pending 写进用户记忆或工作区记忆。
- 不提供页面上的任务列表或手动编辑功能。
- 不提供 `cancelled` 状态和 done 归档。
- 不允许 Agent 通过工具直接修改 pending。
