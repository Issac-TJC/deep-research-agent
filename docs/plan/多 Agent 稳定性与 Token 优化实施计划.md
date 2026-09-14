# 多 Agent 稳定性与 Token 优化实施计划

## Summary

一次性修复审计补证崩溃、Embedding 503 和多 Agent 上下文重复问题。默认采用“质量档”：180k soft target、现有 250k absolute hard cap，保留全部 Agent、最多两轮补证/修订但受边际收益与预算控制。仅保证新 Run；旧数据保持可读，不迁移旧 checkpoint。实现后允许一次最高 $0.50 的真实复测。

## Key Changes

### 1. 审计补证容错

- Reviewer 请求显式携带冻结的 `stage → allowed methods` 矩阵。
- Reviewer 返回后逐条校验 `gap_tasks`；仅把合法任务写回 `review.gap_tasks`。
- 非法任务不再抛出 `ValueError`：追加 warning finding，发送 `review.gap_rejected` 事件，记录原 stage、method、允许值和原因。
- 若全部补证任务均无效，直接进入 Writer；最终以 `needs_review` 披露缺口，不中断 Run。
- 不做静默方法映射，也不额外调用模型修复非法任务，避免改变研究意图和浪费 token。

### 2. 多 Agent 通信压缩

- 引入内部 `ResearchProgress` capsule，保存授权来源、候选 URL、已读范围、检索命中、验收条件覆盖、未解决项和最近工具结果；不保存完整历史作为下一轮 prompt。
- 每个工具批次完成后，在合法的协议边界开始新对话，只发送固定 system/brief、任务、capsule 和当前相关证据。完整请求/响应仍由 action 账本保护性保存，用于恢复与审计。
- 工具调用中的 assistant/tool 配对和 `reasoning_content` 保留到本轮结束；不得截断未闭合的工具协议。
- 跨 Agent 依赖只传 finding 摘要和 Claim/EvidenceSpan ID；正文与引用作为可裁剪 evidence bundles，按问题相关性稳定排序，默认最多选择 16 组，其余保留在数据库并记录 omission。
- 合并重叠 read ranges，同一来源范围只抽取一次；连续两轮无新来源、Claim、Span、冲突或问题覆盖时提前停止工具循环。
- 固定提示保持稳定顺序以利用供应商缓存，剩余额度等易变字段放在消息尾部。

### 3. Token 预算策略

- 保留 `RunProfile.max_tokens=250000` 作为绝对上限；新增内部 180k soft target，不新增用户配置项。
- Soft target 分组：Scope/Plan 18k、Research/Extraction 90k、Review/Gap 24k、Writer/Patch 39k、Contingency 9k。未用额度只能向后流转，Writer/Patch 的 39k 不得被前期研究占用。
- 分离三种计量：
  - serialized bytes：仅用于尺寸诊断；
  - estimated tokens：用于 soft target 和阶段调度；
  - provider actual tokens：用于最终账本和 absolute hard cap。
- Soft target 使用保守校准估算和角色预期输出；absolute hard cap仍使用最大可能输出预留，避免突破用户硬限制。
- 角色预期输出：工具决策 4k、抽取 4k、Scope/Plan/Reviewer 8k、Writer/Patcher 16k；模型截断时仍可在硬上限内按现有机制提高额度。
- 降级规则：
  - 70%：强制 capsule、减少重复证据；
  - 85%：停止发现新来源，只检索/阅读已授权来源；
  - 95%：停止补证，立即进入 Review → Writer；
  - 任意阶段耗尽时交付部分报告，不把任务失败升级为无报告。
- 在 `actions` 墕加 `role`、`budget_group`、`estimated_input_tokens`、`output_token_ceiling`；旧记录默认归入 `legacy`，继续可读。

### 4. Embedding 与索引恢复

- 保留 `gte-multilingual-base@9bbca17` 和现有 768 维索引。
- 构建时解析并锁定 `Alibaba-NLP/new-impl` 的 immutable commit，将动态代码复制到镜像持久目录并改为本地加载；构建阶段在联网环境完成一次真实 warm-load。
- 运行时继续只读、离线和隔离网络，不依赖 `/tmp` 中临时下载的模块。
- Embedding `/health` 执行短文本推理，只有得到非零、归一化的 768 维向量才返回 ready。
- Compose 使用 `service_healthy` 后再启动 API、Worker 和 Indexer。
- Indexer 对暂时性 503 采用有界退避，保留 `lexical_ready`；服务恢复后用新 index version 重建失败索引，不覆盖旧可用版本。
- 保留并验证现有 Parser 的 OpenCV/libxcb 系统依赖修复。

## Public Interfaces and UI

- 保持现有 `GET /research-runs/{id}/usage` 数组格式不变。
- 新增 `GET /research-runs/{id}/usage-summary`，返回 totals、180k soft target、250k hard cap、各 budget group 的输入/输出/缓存/调用/费用，以及已触发的 degradation events。
- 工作台“调用与用量”上方增加阶段用量条、soft/hard 剩余额度、缓存命中和降级原因；不提供预算或通信策略编辑控件。
- CreateRun/RunProfile 的现有外部请求格式保持兼容。

## Test, Rollout, and Documentation

- 单元测试覆盖：非法 gap task 被拒绝但 Run 继续、合法任务保留、无有效 gap 时进入 Writer、capsule 不包含完整历史、工具协议成对、重叠读取去重、阶段额度与降级阈值。
- 集成测试覆盖：并发预算预留、checkpoint 恢复后的 capsule、预算耗尽仍产生 `needs_review` 报告、离线 Embedding 推理返回 768 维、Indexer 从 503 恢复到 `ready`。
- 增加固定工具序列通信基准：序列化请求字节数至少下降 35%，所有任务目标、来源授权和引用定位保持一致。
- 先运行完整后端、Compose、前端和浏览器测试；随后新建一次同类 3DGS 真实 Run，费用上限 $0.50，要求总 token 不超过 180k、无 `run.interrupted`、Embedding 无 503、最终产生报告。
- 新建 `docs/multi-agent-token-strategy.md` 记录 capsule、预算池、降级规则和使用边界；在 `docs/development-log.md` 记录本次 Run ID、175,153-token 基线、两个根因、修复与复测；在 `docs/verification-results.md` 写入测试命令、真实复测指标和尚未验证的质量结论。
- 旧 Run 保持查询和导出能力；修复前 checkpoint 不允许恢复，新代码指纹下要求创建新 Run。
