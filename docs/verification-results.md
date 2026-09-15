# V1 验证记录

## EvoMemory v2（2026-09-15）工程验收

| 验证 | 实际结果与范围 |
|---|---|
| 独立完整后端 | `make integration-isolated`：**87 passed，21.30 秒**；全新 PostgreSQL/MinIO，迁移到 `0008` 后自动清理 |
| 记忆边界 | 覆盖 v1 legacy 不注入、candidate 不注入、confirmed 注入、用户全局/项目 scope、双租户 RLS、证据指针与 supersede 关系 |
| 会话状态 | 覆盖 pending 协议不进入摘要、最近六轮保留、Conversation checkpoint 可更新/恢复、同对话 queued/running 返回 409 |
| 后台队列 | 覆盖 digest 幂等基础、任务领取/完成、Observation embedding/linking 入口和 job API；fixture 不调用付费辅助模型 |
| 静态与前端 | Ruff、compileall、TypeScript、Next.js production build、两个 Compose config、`git diff --check` 均通过 |

未执行真实辅助模型的 40 轮长对话、中文/英文 Recall@5、纠正事实使用率和关系判断质量评测，因此这些产品验收阈值仍是待测项，而不是已通过指标。

## v0.4.0-rc.1（2026-09-15）P0/MVP 验收

本轮以只读 PRD `Deep Research Agent 项目化研究与智能周报产品需求文档.docx` 为需求基线，完成 P0/MVP；P1 分支重跑、邮件、外部导出与 P2 团队协作不在本 RC 范围。正式 `v0.4.0` 仍须完成连续四周 shadow，RC 验收不能替代长期质量结论。

| 验证 | 命令／方式 | 实际结果与范围 |
|---|---|---|
| 独立完整后端 | `make integration-isolated` | **86 passed，21.55 秒**；独立 PostgreSQL/MinIO、无常驻 Worker 抢占，结束后自动清理测试卷 |
| 迁移前进／回滚 | `.venv/bin/alembic downgrade 0006`、`.venv/bin/alembic upgrade head`、`.venv/bin/alembic current` | 通过，最终为 `0007 (head)`；历史数据结构采用增量迁移 |
| Python 静态与编译 | `.venv/bin/ruff check src migrations tests scripts`、`.venv/bin/python -m compileall -q ...` | 通过 |
| Web 类型与生产构建 | `pnpm --dir apps/web typecheck`、`pnpm --dir apps/web build` | 通过；Next.js 生产构建与 TypeScript 通过 |
| Chrome 端到端 | `pnpm --dir apps/web exec playwright test` | **4 passed，9.2 秒**；研究/证据/导出、移动端双 PDF 分别定位、项目记忆/真实候选周报、键盘焦点与控件可访问名称 |
| Compose 与运行健康 | `docker compose config -q`、`docker compose up -d --build ...`、`GET /health` | 构建/启动通过；健康检查返回 `version=0.4.0-rc.1` |
| 容量性能 | `make performance` | 回滚式生成 1000 项目、1000 会话、10000 资产、100000 消息、5000 记忆；P95：项目列表 2.539ms、会话列表 0.772ms、消息页 0.797ms、记忆检索 0.795ms，均通过门禁 |
| 免费学术接口 smoke | `.venv/bin/python scripts/smoke_academic.py --query "3D Gaussian Splatting" --days 30` | 9 个候选；OpenAlex/Crossref/PubMed 各成功 3 个，arXiv 429 被隔离并披露；partial-success 通过，付费模型调用为 0 |
| 差异卫生 | `git diff --check` | 通过 |

在线 smoke 的观测耗时为 OpenAlex 503ms、Crossref 1166ms、PubMed 845ms、arXiv 4516ms；成功、失败、尝试次数、延迟和平台成本字段均进入连接器审计。OpenAlex 的审计估算成本字段为 `$0.001`，这是平台成本建模字段，不是本轮发生的付费模型调用。

已覆盖的主要回归包括：消息幂等及异载荷 409、项目删除全入口 404/恢复/到期清理、摘要不越 pending 且真正注入冻结上下文、同项目记忆关系、画像来源权限与 180 天否定、跨类型搜索公平性、DST 周期、重复调度与单通知、连接器部分失败、canonical/provenance/评分/反馈、abstract-only 证据边界、SSE `Last-Event-ID` 重连，以及周报固定阶段预算快照。

尚未宣称完成：连续四周 shadow、人工论文相关性/事实支持率、完整 WCAG 2.1 AA 人工审计、生产 SLA，以及 P1/P2 功能。shadow 期间要逐周核对调度准时性、候选覆盖、八周去重、证据范围、人工相关性、失败披露、成本和重复通知；全部通过后才能升为 `v0.4.0`。

## v0.3.0（2026-09-15）项目化研究与智能周报

本轮以产品需求文档为需求来源，把原有单次 Run 工作台改造成 `Project → Conversation → Message → Research Run` 持续研究工作区。迁移把历史 Run 放入各租户默认项目，并为新增用户自动创建默认项目与对话；原 `/research-runs` 接口保留兼容。

| 验证 | 命令／方式 | 实际结果与范围 |
|---|---|---|
| 完整后端 | `RUN_INTEGRATION=1 .venv/bin/pytest -q` | 75 passed；真实 PostgreSQL／MinIO，模型和搜索为 fixture／故障注入 |
| 项目化定向集成 | `RUN_INTEGRATION=1 .venv/bin/pytest -q tests/test_project_workspace.py` | 3 passed；项目、会话、资产、记忆、画像、搜索、RLS、软删除、周报入队与质量门禁 |
| 前端类型与生产构建 | `pnpm --dir apps/web run typecheck`、`pnpm --dir apps/web run build` | 通过；Next.js v0.3 工作台生产构建成功 |
| Chrome 端到端 | `pnpm --dir apps/web exec playwright test` | 3 passed，11.9 秒；保留原研究/PDF 移动端旅程，新增项目记忆与周报完整旅程 |
| 静态／配置 | `.venv/bin/ruff check ...`、`docker compose config -q`、`git diff --check` | 通过 |

周报试运行不会访问外部论文平台。正式入队复用现有受控 `web_search` 研究链，并在报告 `quality_status=passed` 时发布站内通知；fixture 报告为 `unchecked`，因此按设计进入 `needs_review` 而不推送。OpenAlex/Crossref 当前只作为计划连接器记录，直接 API 连接尚未实现。可选 `automation` Compose profile 提供按时区/星期扫描的幂等排程器。本轮没有调用付费模型，也未执行人工论文质量评审。

验证日期：2026-09-11。分支：`research-blueprint-v1`。以下区分真实基础设施测试、合成研究流程、真实供应商联调和尚未执行的质量实验。这里的“通过”不代表已有企业客户、生产 SLA、论文实验复现或人工事实认证。

## 环境与版本

- 项目 Python 独立环境：3.12.14；uv 0.12.13；Node 22；pnpm 项目声明 10.28.2。
- PostgreSQL 17.6；MinIO `RELEASE.2025-09-07T16-13-09Z`；Next.js、PDF.js、LangGraph 等版本以两个 lockfile 为准。
- 后端最终代码指纹：`9fc9ec7303c54127e2d07b677027e61b9aa471cbf2f1425fc8e006856b150a78`。依赖锁与基础镜像摘要另行固定；指纹只覆盖后端模块代码，不代替完整环境版本。
- DeepSeek：`deepseek-flash`，thinking enabled / low。Tavily：basic 搜索。价格为创建 run 时冻结的保守配置估算，不是供应商账单。
- PostgreSQL 与 MinIO 使用项目专属卷。测试随机租户与本地两个演示租户分开；密钥不写入本文。

## 已执行的工程验证

| 验证 | 命令／方式 | 实际结果与范围 |
|---|---|---|
| 完整后端 | `RUN_INTEGRATION=1 .venv/bin/pytest -q` | 45 passed，16.98 秒；真实 PostgreSQL／MinIO，模型与搜索注入 fixture／故障响应 |
| Chrome 端到端 | `pnpm exec playwright test` | 2 passed，11.3 秒；创建、引用高亮、Markdown 下载、会话恢复、移动端 PDF，截图已检查 |
| 静态检查 | `.venv/bin/ruff check src migrations tests scripts` | 通过 |
| 后端／工作台镜像 | `docker compose build api worker parser migrate web` | 同步构建成功；前端生产构建通过 |
| 真实 Worker 中断 | 测试子进程 `SIGKILL`，再接管 | fetch 结果已提交和 Writer 响应已提交两种窗口；fence 从 1 变 2，已保存动作不重复执行，用量累计 |
| 取消／预算竞争 | 并发预留、实际挂起回调后取消、旧 token 提交 | 超额预留被拒绝；迟到响应不能提交；未知调用保留预留 |
| 租户隔离 | API、数据库 RLS、业务／checkpoint、文件／来源／证据／事件／动作缓存 | 双租户负向读写通过；运行时非 BYPASSRLS |
| 模型协议／错误 | 模拟 429、超时、非法 JSON、length、非法工具参数 | 有限重试、计量、修复反馈与 schema 路径记录；无 live→fixture 回退 |
| 抓取／解析／内容 | DNS 混合地址、私网重定向、无效 PDF、HTML 脚本与两栏 PDF | 确定性限制与精确字符偏移测试通过；未宣称抵御所有语义提示注入 |
| Parser 容器 | 实际连接公网探测与环境变量存在性检查 | 公网连接失败；没有模型／数据库／对象存储凭证 |
| 上下文／审查路由 | 大证据集成组筛选；模型漏报表格引用 | 保留 brief、主张与证据成组；省略项可见；程序独立转入局部修订 |

故障注入不是用户线上事故。恢复测试人为注入租约过期以缩短测试，结果不能解释为真实 45 秒故障检测时延。测试管理员权限只用于搭建、清理和注入，不能据此认定普通租户拥有这些权限。

## 合成评测

首批 `artifacts/fixture-evaluation/20260911T231341Z/`：B2 的 20 个开发 briefs，B0、B1、B2 串行和 B2 全文策略各 1 次，共 24 次，均 completed / unchecked，发布检查错误 0。整个流程不调用付费模型或 Tavily。

最终代码重跑目录 `artifacts/fixture-evaluation/20260911T232846Z/`：同样 24 次全部 completed / unchecked，`report_gate_errors=0`，未调用付费 API。这是修复后正式引用的流程结果。

这些 fixture 的输出是预设合成材料。其耗时、token 和模型调用数只用于检查评测管线，不能得出模型质量、实际成本或并发加速结论。保留集没有用于提示词调优；真实 B0／B1／B2 同条件质量对照仍未执行。

## 真实 API 联调

所有下表运行都使用真实 DeepSeek；技术选型同时调用真实 Tavily，论文案例只读取指定 PDF。每次 smoke 上限 $0.50，计入初次 $10 campaign。不同轮次修改了代码、提示或范围，网页与采样也未冻结，不能把它们当作控制变量实验。

| run ID | 场景／结果 | 模型／工具（含搜索） | token | 秒 | 估算已知费用 USD |
|---|---|---:|---:|---:|---:|
| `b4b6dcfc-e7a5-4fdc-a5a3-3b6ae5612c06` | 技术选型，搜索耗尽丢失 read 状态；后续上下文上限，部分包 | 10 / 11（6） | 49,652 | 105.41 | 0.090515796 |
| `7e4d8836-afe8-4a8d-a815-7624a3153813` | 技术选型，27 claims / 27 spans；累计 token 上限，部分包 | 27 / 27（6） | 194,420 | 233.80 | 0.133852008 |
| `1b858689-9b4a-4467-8a70-80f9e670b13b` | Writer 连续 schema 失败；旧诊断不足，不能断言均为截断 | 20 / 17（4） | 177,732 | 226.68 | 0.113123812 |
| `42495d50-ea8c-4042-8429-47eb082236be` | 论文，确认 thinking 用满 4k 输出；开发者停止诊断运行 | 12 / 3（0） | 69,481 | 155.10 | 0.041327544 |
| `29ff6bb9-a631-422c-b85d-f31c371d263e` | 论文，生成解释但引文不匹配，0 有效 spans，needs_review | 12 / 3（0） | 73,871 | 133.61 | 0.049739040 |
| `23a75041-0116-40d9-9892-fc6e3f3c2d4b` | 论文，12 claims / 7 spans；解释与研究建议；表格需复核 | 12 / 4（0） | 91,711 | 157.05 | 0.052271532 |
| `70885cae-0a2f-415a-9d2d-0a7a11bc0b91` | 技术选型，36 claims / 32 spans；固定证据区超限，部分包 | 20 / 17（3） | 138,592 | 130.02 | 0.080243172 |

| `59a284fe-e92e-4a58-9c58-5d50dd0a7f03` | 技术选型完整闭环＋局部修订；16 claims / 9 spans，needs_review | 20 / 17（4） | 183,227 | 232.11 | 0.123929572 |

最终复测交付 7 个稳定节点、4 行比较矩阵、16 处单元格引用和 revision 2。写作前上下文超限未重现；程序检查触发局部修订，最终保留 3 项门禁问题（两个数字核验项与覆盖／支持不足），真实定位／哈希错误为 0。模型／工具总数包含失败动作；本轮有 5 次 fetch 以 ValueError 失败，不能把全部工具调用描述为成功。更具体的来源失败分类与模型如何描述未读资料仍有改进空间。

8 次真实运行的账本已知估算费用合计 **$0.685002476**，未结算预留 **$0.0119613**，合计约 **$0.697**，低于已授权的 $10 初次联调上限。账本快照为 `artifacts/live/verification-ledger.json`；不包含该 API Key 在本项目以外的调用。

第四轮另保留 $0.0119613 未知预留，不能当作未计费。早期 metrics 文件的 `citation_locator_errors` 曾统计全部发布门禁项；现在区分 `report_gate_errors` 与真正的偏移／哈希错误。论文第六轮的 16 项旧计数是表格和数字检查，不能说成 16 处原文定位失败。

结果原件位于本机 `artifacts/live/{run_id}.json`、`.md`、`.metrics.json`；失败可能没有报告，早期失败指标文件不含完整用量，调用账本仍在 PostgreSQL。开发日志保留当时证据和修复，不覆盖早期失败记录。工具数包含 search，不要将二者相加当作总工具次数。

## 三段可复核演示

1. **技术选型**：按照 README 启动 fixture 工作台 → 创建中文知识库检索比较 → 看任务、点击引用、检查原文和导出。再查看上表真实 run 的用量／未解决项，避免把合成建议当成真实研究成绩。
2. **论文理解与研究启发**：运行 `scripts/create_demo_pdf.py` 后在工作台上传合成 PDF，验证产品流程；真实研读使用 `scripts/smoke_live.py evals/smoke-paper.json`，查看方法、实验、局限、假设与引用。既有真实案例为第六轮，不宣称实验已复现。
3. **Worker 中断恢复**：停止常驻 Worker，运行 `RUN_INTEGRATION=1 .venv/bin/pytest tests/test_failures.py -k real_worker_process_kill -q`，检查 `artifacts/recovery/after_fetch.json` 和 `before_draft_commit.json`，对照 fence 和累计用量。

## 仍未完成或不能由当前证据支持的事项

- 10 个保留 briefs 的真实付费质量评测、每例多次重复、完整 B0／B1／B2 以及上下文／并发消融；工具与数据已交付，实验尚未执行。
- 人工 citation precision、事实支持率、论文解释准确性、研究建议可操作性和评审者一致性；相应指标保持 null。
- 长期运行中的上下文省略概率、质量／成本收益、生产 SLA、跨机器性能和相对参考项目的优越性。
- 任意复杂 PDF、OCR、论文代码执行、真实模型下跨 Run 记忆质量验收、SSO、源 ACL 撤销、未知费用自动对账。

因此，简历可描述实际实现和测试范围，可引用带 run/profile/样本条件的调用与耗时；不能写“多 Agent 准确率提升 X%”“无损压缩”“已复现论文”或“生产级合规平台”。

## 2026-09-13 稳定性与 token 优化验证

已执行：

| 验证 | 命令 | 结果 |
|---|---|---|
| Python 静态检查 | `.venv/bin/ruff check src tests migrations` | 通过 |
| Python 单元测试 | `.venv/bin/pytest -q -m 'not integration'` | 39 passed |
| 完整后端与基础设施 | `RUN_INTEGRATION=1 .venv/bin/pytest -q` | 68 passed，17.43 秒；包含 checkpoint capsule 恢复、并发预留、非法/合法 gap、部分报告与索引恢复 |
| Python 编译 | `PYTHONPYCACHEPREFIX=/tmp/research-agent-pycache .venv/bin/python -m compileall -q src tests migrations` | 通过 |
| Compose 配置 | `docker compose config -q` | 通过 |
| Compose 镜像 | `docker compose build embedding parser api worker indexer migrate web` | 通过；Embedding 构建期真实 warm-load 768 维，Parser 的 OpenCV/libxcb 层构建通过 |
| Embedding 运行时 | API 容器访问 `/health` 与 `/embed` | HTTP 200，`status=ready`，768 维，L2 norm=1.0；最近 Indexer/Embedding 日志无 503 |
| Chrome 端到端 | `pnpm --dir apps/web exec playwright test` | 2 passed，8.1 秒；当前自动意图 UI、用量接口、PDF 解析与移动端预览 |
| 固定工具序列通信基准 | `tests/test_token_strategy.py` | 序列化请求至少下降 35%，任务、授权 source 和 Claim/Span 定位不变 |

真实复测 `e52f1a4f-ea6e-4db4-888a-f0f7d3282f81`：同类 3DGS brief，单 Run cap $0.50。结果 completed / needs_review、8,995 token、$0.005786724、无 `run.interrupted`、无 Embedding 503，并生成部分报告。它在 Scope 截断重试后暴露 soft pool 未使用 contingency 的缺陷，尚未进入 Research，因此不满足“最终完整报告”的验收条件。缺陷随后修成累计 27k / 117k / 141k / 180k，并通过 68 项回归；原授权限定一次真实 Run，所以没有把第二次付费运行伪装成已验证。

尚未验证：修正后完整 3DGS Run 是否稳定低于 180k、最终研究质量是否优于 175,153-token 基线，以及 capsule 压缩对人工事实支持率的影响。工程门禁通过不等于研究结论已经人工认证。

## v0.2.0（2026-09-14）预算收口状态机修复

事故复现证据：Run `f4e6862f-abb8-475f-b0e4-720edf52b44c` 以 `budget:retrieval_calls`、111,951 token、`revision 999` 结束。20 个 claim 全部来自前两个任务；方法任务因研究 soft target 失败，实验任务保持 running；没有 Reviewer、Writer 或 Patcher action。新来源索引在 Run 结束后约 1–2 分钟陆续 ready，因此运行中的 `embedding_unavailable` 表示语义索引未 ready 后的 lexical fallback，不等价于 Embedding 服务 503。

修复验证：

| 验证 | 结果 |
|---|---|
| 事故链定向测试 | 20 passed；覆盖预算耗尽进入 Writer、依赖任务取消、Run 级 retrieval 余量、Reviewer provisional 审查、确定性降级报告 |
| 完整后端／基础设施 | `RUN_INTEGRATION=1 .venv/bin/pytest -q`：72 passed，26.38 秒；发布前版本复跑 72 passed，20.02 秒 |
| 静态与格式 | `.venv/bin/ruff check src tests migrations scripts`、目标文件 `ruff format --check`、`git diff --check` 均通过 |
| 生产构建 | `docker compose build web api worker indexer migrate` 通过；Web 使用锁定的 Node 22 / pnpm 10.28.2 完成 Next.js 与 TypeScript 构建，Python 镜像安装 `deep-research-agent==0.2.0` |

测试时需停止常驻 Compose Worker，否则它会领取测试租户的 queued Run，破坏队列上限与 SIGKILL 恢复测试的确定时序；测试完成后恢复 Worker。没有为此次修复运行新的真实 DeepSeek／Tavily 任务，因此尚未把工程回归结果表述为真实报告质量验收。
