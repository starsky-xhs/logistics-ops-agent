# 风险与审批策略

## 1. 风险分级

| 等级 | 判定 | 工具 | 默认动作 |
| --- | --- | --- | --- |
| 低 | 只读，不改变履约/资金 | `query_order`、`list_orders`、`track_shipment`、`estimate_delivery`、`query_invoice`、`escalate_to_human` | 直接执行 |
| 中 | 可重放的写操作 | `create_invoice` | `high_only` 直接执行；`conservative` 审批 |
| 高 | 改变履约或资金状态 | `change_delivery_address`、`cancel_order`、`reroute`、`refund` | **必须人工审批** |

`high` 不随配置变化。`APPROVAL_POLICY` 只有：

- `high_only`（默认）：中风险直接执行；
- `conservative`：中风险也进入审批。

没有“关闭所有审批”的配置项。

## 2. 高风险动作的影响

| 工具 | 影响 | 当前允许的前置状态 |
| --- | --- | --- |
| `change_delivery_address` | 修改订单与关联运单收货地址 | 订单 `pending/confirmed/packed` |
| `cancel_order` | 订单取消、关联运单取消 | 订单不是 `shipped/delivered/cancelled` |
| `reroute` | 修改在途运单派送地址 | 运单不是 `delivered/cancelled` |
| `refund` | 发票状态改为 `refunded` | 发票为 `issued` |

不符合前置状态时工具返回 `invalid_state`，不会绕过审批强行写入。

## 3. 审批生命周期

```text
pending ──approve──→ approved ──execute──→ consumed
   │
   └──reject──→ rejected
```

- `pending`：状态机已 suspend，工具尚未执行；
- `approved`：审批人已同意，但还没恢复执行；
- `consumed`：工具已执行，审批授权用完；同一审批单不能再次使用；
- `rejected`：人工拒绝，状态机从 observe 收尾，数据不变。

恢复前会校验：

1. 审批单属于当前 run；
2. 运行仍处于 `awaiting_approval`；
3. 审批单是当前等待的审批；
4. 工具名一致；
5. 审批时保存的参数与当前待执行参数完全一致。

执行成功后工具调用和审批状态都会落库，审计日志记录 `approval_id` 与 `approved=true`。

## 4. 幂等

`create_invoice` 是 medium 风险、幂等工具。幂等键：

```text
idem_<sha256(run_id + step_index + tool_name + canonical_json(arguments))[:32]>
```

`invoices.idempotency_key` 有唯一约束。重复键返回原发票并标记 `replayed=true`；
同一订单已有其它发票时返回 `conflict`，避免一单多票。

其它工具是状态迁移，不是幂等工具。重复执行会被前置状态检查拦住（如订单已取消不能再取消）。
当前没有为这些动作提供“自动回滚”。

## 5. 审计字段

`audit_log` 每次 Agent 工具调用写一行：

| 字段 | 说明 |
| --- | --- |
| `ts` | UTC ISO-8601 时间戳；CLI 展示时转本地时区 |
| `actor` | 执行者，Agent 调用固定为 `agent` |
| `session_id` / `run_id` | 会话与运行标识 |
| `tool_name` | 工具名 |
| `args_summary` | 参数摘要；按敏感键与正则脱敏 |
| `result_summary` | 结果摘要；脱敏 |
| `risk_level` | low / medium / high |
| `approval_id` | 关联审批单；低风险为空 |
| `approved` | 是否经过人工审批 |
| `status` | `ok` / `error` |
| `latency_ms` | 工具耗时 |
| `error` | 错误信息（如有） |
| `idempotency_key` | 幂等键（幂等工具） |

`docs/EVAL_REPORT.md` 中的失败明细不是审计日志，属于离线评测结果。

## 6. PII 与输出

- 手机号：保留前三后四；
- 身份证：保留前四后四；
- 邮箱：保留首字符与域名；
- 地址：保留前 8 个字符，门牌号不进入摘要；
- 参数中的 `phone/mobile/address/email/id_card` 等键递归脱敏；
- 输出统一走 `sanitize_output`，检测系统提示词标记并替换为安全回复；
- 提示词注入命中时转安全工单，不调用业务工具。

这些规则适合演示和基础防护，但不能替代企业 DLP、RBAC 或审计合规系统。

## 7. 生产落地需要补充

1. 审批人身份接入 SSO / RBAC，审批动作签名留痕；
2. 审批单过期时间与升级规则；
3. 高风险操作双人复核或按金额分级；
4. 外部 OMS/WMS/财务接口的幂等回执、重试、补偿；
5. 审计日志只追加、不可篡改存储；
6. 按数据主体做访问控制，而不是只做正则脱敏。
