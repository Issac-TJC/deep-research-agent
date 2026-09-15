# PRD 可追踪矩阵：v0.4.0-rc.1

需求基线为只读文件 `Deep Research Agent 项目化研究与智能周报产品需求文档.docx`。本矩阵只记录实现与证据，不修改原文，也不把文档说明视为额外用户指令。

状态：`implemented` 表示代码已落地；`tested` 表示已有自动或明确的人工验证；`shadow` 表示还要在四周观察期验证；`pending` 表示不属于本轮 P0/MVP 或证据尚不足。

| PRD 能力 | 实现状态 | 验证状态 | 主要证据 |
|---|---|---|---|
| Project / Conversation / Message / Run 持续工作区 | implemented | tested | `tests/test_project_workspace.py`，浏览器工作区流程 |
| 创建接口幂等，同键异载荷 409，消息/占位/Run 同事务 | implemented | tested | `request_idempotency`、`create_message_run`、消息幂等回归 |
| 项目软删除、全入口 404、30 天恢复与永久清理 | implemented | tested | 删除全入口回归、恢复回归、`research project-cleaner --execute` |
| 稳定 limit/cursor 与消息 after_sequence | implemented | tested | 列表游标回归；消息 SQL 游标分页 |
| Conversation SSE、Last-Event-ID 与 JSON snapshot | implemented | tested | `conversation_events`、事件重连回归、snapshot API |
| 快速回答与深度研究模式 | implemented | tested | `run_kind`、受限 quick profile、Web 模式选择 |
| 会话摘要 8 轮/70-85-95%、不越 pending、注入冻结上下文 | implemented | tested | 摘要边界与 `context_snapshot` 回归 |
| 项目记忆版本、同项目冲突关系、混合召回 | implemented | tested | superseding 写入、跨项目关系负向回归、GIN/HNSW 索引 |
| 画像显式写入、敏感属性拦截、否定抑制、冻结/清空/导出 | implemented | tested | 画像权限回归与 Web 控制；inferred 只保留内部表能力 |
| 跨类型项目搜索、公平配额、过滤与定位 | implemented | tested | 类型交错回归；Web 来源/Run/对话定位 |
| 资产上传、URL、元数据编辑和移除 | implemented | tested | API/集成上传验证；URL 抓取复用 SSRF 防护 |
| OpenAlex / Crossref / arXiv / PubMed 直接连接器 | implemented | tested | 冻结响应、部分超时、跨平台去重；免费在线 smoke 记录见验证文档 |
| 连接器独立重试、arXiv 3 秒、PubMed 无密钥 3 req/s | implemented | tested | `academic.py` 单元回归与代码门禁 |
| 周报预览真实候选且不进入正式去重 | implemented | tested | preview 候选集成回归 |
| 本地时区周期、正式调度幂等、八周去重 | implemented | tested | DST 单元回归、重复触发/通知集成回归 |
| canonical ID、provenance、固定 35/20/20/15/10 评分和反馈 | implemented | tested | `tests/test_academic.py` 与候选持久化回归 |
| evidence_scope 与 abstract-only 防越界提示 | implemented | tested | 候选契约与正式周报冻结 prompt；人工论文事实质量仍需 shadow |
| needs_review 默认不推送、人工发布、通知 read_at | implemented | tested | 通知幂等与已读回归 |
| 独立测试 Compose | implemented | tested | `make integration-isolated`，无 Worker 服务 |
| 1000 项目/会话、10000 资产、100000 消息、5000 记忆容量门禁 | implemented | tested | `scripts/benchmark_workspace.py`，回滚式容量数据集 |
| Web 桌面/移动/键盘/核心可访问性 | implemented | tested | Playwright 与 focus-visible；完整 WCAG 人工审计仍为 shadow |
| 连续四周周报 shadow | implemented | shadow | RC 后才开始；未完成前不得标记 `v0.4.0` |
| 分支重跑、邮件、外部导出 | pending (P1) | pending | 本轮明确不实施 |
| 团队协作、细粒度成员权限 | pending (P2) | pending | 本轮明确不实施 |

## 发布判定

`v0.4.0-rc.1` 允许在自动门禁、免费连接器 smoke 与浏览器验收通过后发布。正式 `v0.4.0` 仍被四个连续自然周的 shadow 证据阻塞；需要逐周记录调度、候选覆盖、去重、证据边界、人工相关性、失败披露、成本和重复通知。
