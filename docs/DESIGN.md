# 设计说明

## 1. 目标与非目标

目标是把客服在订单、履约、开票、客服工单四个后台之间的重复操作，收敛为一组可被 Agent
调用的工具；同时保留人工对高风险动作的最终控制权。

非目标：不替代真实订单/支付系统，不实现多租户权限，不做模型训练，不追求通用对话能力。
这是一个工程结构完整的个人项目，不是生产级 SaaS。

核心约束：

- `python -m app.demo`、`unittest`、`python -m app.cli eval` 必须离线可跑；
- 核心链路只依赖标准库；
- 高风险工具不能绕过审批；
- 所有工具调用可审计；
- 失败和限制要在评测里被看见，而不是用 demo 掩盖。

## 2. 分层

```text
CLI / FastAPI
      │
AgentService  ── 会话、消息、审批、审计、SQLite 快照
      │
AgentGraph    ── 状态机：节点、条件边、中断/恢复、循环与预算
      │
PolicyEngine  ── 风险、审批规则、幂等键、循环指纹、时间预算
Planner       ── MockPlanner（默认）/ LlmPlanner（可选）
      │
ToolRegistry  ── ToolSpec、schema 校验、风险守卫、执行
      │
工具实现      ── orders / shipments / billing / handoff
      │
SQLite        ── orders / shipments / invoices / approvals / audit_log / runs
```

业务工具不 import Web 框架；状态机不写 SQL；`AgentService` 是唯一同时知道
“审批单是否真的 approved”和“工具执行结果”的层。

## 3. 为什么自己写状态机

这个流程只有七个稳定节点，状态字段不超过二十个。引入 LangGraph 会带来较大的依赖和隐式
行为，反而不利于一个需要“离线跑、可读、可单测”的示例项目。这里用显式代码复现了
StateGraph 的四个核心概念：

| 本项目 | LangGraph |
| --- | --- |
| `AgentState` | State |
| `AgentGraph._dispatch` + `nodes.py` | Node |
| 节点返回下一节点名 | Conditional edge |
| `runs.state_json` | Checkpointer |
| `awaiting_approval` + `approvals` | `interrupt` |
| `AgentGraph.resume` | 同 thread 恢复 |

节点定义：

```text
intake   清洗输入、提示词注入检测
plan     规划器产出 PlanStep 列表
act      补全前序槽位、风险判断、审批或执行
observe  根据结果决定继续 / 收尾 / 转人工
respond  汇总中文回复
escalate 使用工单结果收尾
fail     统一失败出口
```

边：

```text
intake → plan → act → observe ─┬→ act（还有步骤）
                               ├→ respond
                               ├→ escalate（handoff 成功）
                               └→ fail
act（high / conservative medium）→ suspend
approve → 从 act 恢复
reject  → 标记 rejected，从 observe 收尾
```

### 状态持久化

`AgentState.to_dict()` 包含计划、游标、观察结果、审批 id、步数、循环指纹、截止时间等。
每次节点执行后 `AgentService._persist_state` 更新 `runs` 表。进程退出后，`approve` 能加载
同一份状态继续执行，而不是依赖内存对象。

`deadline_epoch` 使用 epoch 秒，跨进程可恢复；时间预算在进入每个节点前检查。

## 4. 规划器

### MockPlanner

默认规划器，输入是用户话术，输出有序的 `PlanStep` 列表。规则按优先级排列：

1. 显式转人工 / 投诉；
2. 运单改派；
3. 订单改地址；
4. 退款；
5. 取消订单；
6. 开票；
7. 查发票；
8. 订单 + 时效 / 物流；
9. 普通订单查询；
10. 订单列表；
11. 兜底转人工。

每条规则都带中文 `reason`，demo 会打印。规则的局限也很明确：复合意图通常只保留第一个
主分支，多订单号只取第一个，`SH\d{8}` 不校验完整边界。这些不是隐藏 bug，而是
评测中的失败用例。

槽位串联示例：用户只说订单号、问“到哪了”，规划器产出：

```json
[
  {"tool": "query_order", "arguments": {"order_no": "SH20250003"}},
  {"tool": "track_shipment", "arguments": {"tracking_no": "$steps[0].shipment.tracking_no"}}
]
```

`act` 在真正执行前从第 0 步成功的观察结果中解析占位符；缺失时标记
`missing_dependency`，不会把字面量 `$steps...` 传给工具。

### LlmPlanner

实现 OpenAI 兼容的 `/chat/completions` function calling：

- 从注册表生成 `tools`；
- `tool_choice="auto"`，`parallel_tool_calls=true`；
- 解析 `tool_calls`，并行返回多个调用时保留顺序；
- 参数必须是合法 JSON object，工具名必须已注册；
- 网络、HTTP、JSON、空 tool_calls 任一失败都降级到 MockPlanner，并在步骤原因里标注；
- 使用标准库 `urllib.request`，不引入 SDK。

限制：只做首轮规划，不把工具结果回填给模型进行第二轮规划；需要真实网络和 API Key，
因此不纳入离线回归。

## 5. 工具层

`ToolSpec` 包含 `name`、`description`、`json_schema`、`risk_level`、`idempotent`。
`ToolRegistry` 负责：

- 注册时检查工具名、风险等级、根 schema；
- 执行前手写校验 JSON Schema 子集：
  `type / required / properties / additionalProperties / enum / pattern /
  minLength / maxLength / minimum / maximum / minItems / maxItems / items`；
- 对 `high` 工具检查 `ToolContext.approval_id`；
- 把 `sqlite3.Error` 和未预期异常统一包装成 `ToolError`，保留稳定错误码。

工具实现只拿到 `ToolContext`（连接、actor、run_id、approval_id、幂等键、时间）和已校验
参数。工具不读环境变量、不发网络请求。

跨字段约束（例如“发票号与订单号至少给一个”）放在实现里，因为手写 schema 子集不需要引入
`oneOf`；这样错误信息也更具体。

## 6. 风险与人工中断

审批规则见 [POLICY.md](POLICY.md)。这里强调实现：

1. `act` 在调用任何工具之前判断 `policy.requires_approval(spec)`；
2. 需要审批时创建持久化 `approvals` 行，返回 `suspend`，不调用工具；
3. `AgentService.approve` 更新审批为 `approved`，校验 run/工具/参数一致后恢复 `act`；
4. `ToolRegistry.execute` 要求高风险调用带 `approval_id`；
5. 工具实现再次检查；
6. 执行成功后审批变为 `consumed`，同一单不能复用。

所以“直接调用未审批的 `change_delivery_address`”会在 registry 层抛出
`ApprovalRequired`；服务层无法用另一个 run 的审批单蒙混，因为参数和 run_id 都会比对。

## 7. 幂等与回滚

`create_invoice` 是唯一的幂等写工具。幂等键由
`sha256(run_id + step_index + tool + canonical JSON args)` 生成，写入
`invoices.idempotency_key` 唯一约束。重复调用同一键返回原发票，`replayed=true`，不会再
插入一行。并发插入撞唯一约束时回读赢家结果。

其它写操作：

- 改地址、取消、改派、退款都是“状态迁移”型操作，不设计自动回滚；
- 写操作在本地 SQLite 中是单事务粒度（当前连接 autocommit，单条 UPDATE 原子）；
- 若未来接入真实 OMS，建议引入 outbox / saga：先写意图表，再调用外部接口，用补偿动作
  处理失败，而不是在 Agent 层做伪回滚。

没有分布式事务，这是已知限制。

## 8. 循环检测、步数与超时

- **循环检测**：每个实际执行的动作生成 `tool + canonical args` 指纹；相同指纹第二次出现
  时立即失败，防止规则或模型陷入死循环。
- **max_steps**：默认 8，执行前检查；超出后标记失败，不继续调用工具。
- **时间预算**：默认 20 秒；每个节点前检查。LLM 请求另有 15 秒 HTTP 超时。
- 当前没有为每个业务工具开线程做硬超时；SQLite 操作是毫秒级，生产接入外部接口时需要
  按工具增加超时与熔断。

## 9. 审计与隐私

`AgentService._execute_tool` 是审计写入点：

- 无论成功失败都写 `audit_log`；
- 记录 actor、session/run、工具、风险、审批 id、是否审批、耗时、错误码；
- 参数摘要递归脱敏：手机号、身份证、邮箱、地址、敏感键名；
- 结果摘要同样经过 `guardrails.mask_pii`；
- `AUDIT_MASK_PII=false` 只用于本地调试，生产必须保持开启。

输入侧 `scan_prompt_injection` 命中后不调用业务工具，直接生成 security 工单；
输出侧 `sanitize_output` 检查系统提示词标记，再统一脱敏。

这些是纵深防御的一部分，不是完整安全方案。

## 10. 评测

40 条用例，每条独立 SQLite + 全新种子数据。审批用例自动批准，以便区分“审批触发是否正确”
和“审批后是否完成”。指标定义：

- 工具选择准确率：工具序列与期望完全一致；
- 参数抽取准确率：期望字段逐项比对；
- 审批触发正确率：是否产生审批与期望一致；
- 任务完成率：工具序列、关键参数、最终状态全部正确。

故意保留难例，当前真实结果约为工具 87.5%、任务 82.5%。失败原因在
[EVAL_REPORT.md](EVAL_REPORT.md) 里逐条给出。MockPlanner 不是按用例反写的；
LLMPlanner 不参与离线回归。

## 11. 如果继续演进

1. 引入 clarification 工具/状态，把“缺槽位”从转人工里拆出来；
2. 让 MockPlanner 支持多意图拆分，但不要按评测用例硬编码；
3. LLMPlanner 增加基于工具结果的多轮规划；
4. 审批接入企业 IM / SSO / RBAC，审批单增加过期时间；
5. 外部系统调用增加 outbox、重试、幂等回执与补偿；
6. 审计落对象存储并接入告警，替换 SQLite；
7. 评测扩充到 200+ 条并加入人工标注的语义等价判定。
