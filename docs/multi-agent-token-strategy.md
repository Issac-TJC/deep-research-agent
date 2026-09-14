# 多 Agent 通信与 Token 策略

本策略只应用于新建 Run。旧 Run、报告和动作账本保持可查询、可导出；代码指纹变化后，旧 checkpoint 不允许恢复，必须创建新 Run。

## ResearchProgress capsule

Researcher 每完成一个完整工具批次，就在合法的 assistant/tool 协议边界关闭本轮对话。下一轮仅发送稳定 system policy、冻结 brief、任务说明、`ResearchProgress` 和当前相关证据，不重放完整消息历史。

Capsule 保存：授权 source ID、候选 URL、已合并的读取范围、检索命中、验收条件覆盖、未解决项、最近工具结果摘要、无新增信号轮数。它明确不保存 `messages`、完整网页正文或供应商 reasoning。完整模型请求／响应保存在租户 RLS 保护的 action 账本；工具调用的 assistant/tool 配对必须在重置前闭合。

跨 Agent 只传 finding 摘要以及 Claim/EvidenceSpan ID 和定位信息。写作、审查、修订按问题相关性和稳定 ID 排序证据 bundle，默认最多 16 组；省略 ID 写入 context snapshot，原记录仍在数据库。重叠 read range 合并，同一已覆盖范围不再抽取。连续两轮没有新增来源、范围、检索命中或覆盖信号时停止工具循环。

## 三种计量

- `serialized_bytes`：诊断请求尺寸与协议上限，不作为供应商 token 账单。
- `estimated_input_tokens`：保守多语言估算，用于 soft target 与阶段调度。
- provider actual tokens：供应商返回的输入、输出与缓存 token，是最终账本和 250k hard cap 的事实来源。

Actions 新记录 `role`、`budget_group`、`serialized_bytes`、`estimated_input_tokens`、`output_token_ceiling` 和受保护请求。迁移前记录默认归为 `legacy`，不改变 `/usage` 数组接口。

## 180k soft target

外部 `RunProfile.max_tokens` 保持兼容，默认和绝对上限仍为 250,000。内部质量档为 180,000：

| 预算组 | 目标 |
|---|---:|
| Scope / Plan | 18,000 |
| Research / Extraction | 90,000 |
| Review / Gap | 24,000 |
| Writer / Patch | 39,000 |
| Contingency | 9,000 |

额度只向后流转。9k contingency 可吸收截断重试等阶段超额，因此累计门槛为 27k / 117k / 141k / 180k；即使前期使用 contingency，研究与审查阶段仍不能占用 Writer/Patch 的 39k。数据库在行锁内检查并发预留。角色初始输出上限为：工具决策与抽取 4k，Scope/Plan/Reviewer 8k，Writer/Patcher 16k。结构化输出被截断时，原有限次重试可在 hard cap 内提高输出上限。

预算异常必须在所属协议边界内收口。单个工具配额（包括 Run 级 retrieval 上限）只移除该工具，不能终止整个 Run；Research/Extraction 模型池耗尽时停止启动新研究任务，取消失败依赖，并进入 Review → Writer。此时不得新增补证或修订轮次。若 Reviewer 或 Writer 已无法预留模型额度，则分别使用 provisional claim 审查和按交付任务组织的确定性阶段报告；最终质量必须为 `needs_review`，并保留原始 budget stop reason。

降级规则：

- 70%：强制 capsule 并减少重复证据；当前实现从第一轮即使用 capsule。
- 85%：禁止发现或抓取新来源，只能检索／读取已授权来源。
- 95%：拒绝新的 gap task，直接审查并写作。
- 预算耗尽：发布已有 Claim 的部分报告，质量标记 `needs_review`，不因无完整报告而把 Run 升级成失败。

`GET /research-runs/{id}/usage-summary` 返回 totals、soft/hard 剩余量、各预算组输入／输出／缓存／调用／费用以及 `budget.degraded` 事件。工作台只显示这些信息，不提供运行中修改策略的控件。

## 使用边界

Capsule 压缩减少重复上下文，但不是无损语义压缩；被省略证据不得视为已审查。85% 阈值后不会扩大来源范围，95% 后不会自动修复研究意图。非法 Reviewer gap task 不做静默映射，也不额外调用模型；系统记录 warning 和 `review.gap_rejected` 后继续交付。
