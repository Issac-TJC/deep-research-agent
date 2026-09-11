# 本地开发、恢复与演示

## 独立环境

项目使用 Python 3.12 和项目目录下 `.venv`，不向系统 Python 安装依赖。安装 uv 后执行 `uv sync --extra dev --frozen`；激活为 `source .venv/bin/activate`。退出为 `deactivate`。前端使用 Node 22 和 pnpm 10.28.2，进入 `apps/web` 执行 `pnpm install --frozen-lockfile`。

已有 `.env` 不要覆盖。配置模板是 `.env.example`；真实密钥只放入 `.env` 或进程环境。运行 `research doctor` 只检查配置是否存在，不输出密钥、不发送付费请求。

## 服务

| 服务 | 本机地址 | 职责 |
|---|---|---|
| Web | http://localhost:13000 | 工作台、服务端会话与同源 API 代理 |
| API | http://localhost:18000 | REST、SSE、授权与运行控制 |
| PostgreSQL | localhost:15432 | 状态、checkpoint、事件、预算、租户 RLS |
| MinIO | localhost:19000 | 私有原文与解析工件 |
| MinIO console | http://localhost:19001 | 本地对象存储管理 |
| Parser | 不暴露宿主端口 | 内部网络、只读文件系统、受限子进程解析 |

`RESEARCH_MODE=fixture docker compose up --build -d` 启动完整合成演示。数据库和对象存储位于 `research-agent-v1` 专属命名卷。`docker compose down` 停止服务并保留数据；不要使用 `down -v` 除非明确要删除本项目全部数据。

本地热开发可以只启动 `docker compose up -d postgres minio`，然后分别运行 `make api`、`make worker` 和 `make web`。本地解析子进程有时间和资源限制；完整网络隔离只在 Compose 的 Parser 容器中成立。

`research seed` 创建两个测试租户并输出随机 API Key，数据库只存哈希。当前工作区的种子密钥保存在 `.local/test-tenants.jsonl`，不要上传或提交。Web 将密钥放入 HttpOnly、SameSite 会话 cookie，客户端不写 localStorage。

## 首次真实研究

1. 填写 DeepSeek 和 Tavily 环境变量；核对模型、端点、价格和 $10 初次联调总上限。
2. 让 API 与 Worker 使用同一代码构建：`RESEARCH_MODE=live docker compose up --build -d api worker`。
3. 新建小规模研究，确认来源已实际获取、引用可定位、用量有记录，再扩大输入。
4. 真实报告自动门禁不等于人工事实认证；对照实验按 evals 文档执行，未测指标保持空值。

可复核的小规模联调脚本：先停止常驻 Worker，再执行 `.venv/bin/python scripts/smoke_live.py`。论文案例为 `.venv/bin/python scripts/smoke_live.py evals/smoke-paper.json`。脚本从本地环境及测试租户文件读取密钥，单次费用上限 $0.50，归入累计 $10 账本；原始研究包和指标写入 `artifacts/live/`。

## 恢复与取消

- Worker 默认每 10 秒续约，租约 45 秒；单个活跃 run、最多 3 个 Researcher 并发。进程消失后，过期运行可被新 Worker 领取。
- 父图和研究子图都持久化；外部步骤按稳定 logical action key 保存成功结果。崩溃后复用已保存响应，避免再次有效提交；未知供应商结果保留预算预留，无法保证供应商侧 exactly-once。
- 取消先写数据库并递增 fence；旧 Worker 的业务／checkpoint 写入被拒绝。取消后可能仍存在供应商无法立即停止的计费，不能把所有预留直接归零。
- `interrupted` 可通过工作台恢复；`completed`、`failed`、`cancelled` 需要创建关联研究的新运行。恢复不重置累计预算或运行时限。
- `runtime_version_mismatch` 表示 API 创建 run 时与 Worker 的代码指纹不同。使用原构建恢复，或以当前构建创建新 run；不要修改数据库指纹强制执行。新代码部署需同步更新 API 和 Worker。
- 从 DB 状态和事件确认任务是否终止；浏览器断开不会取消运行。SSE 按序号补读，前端按 seq 去重。

## 测试与演示

先停止常驻 Worker：`docker compose stop worker`，并结束本地 `research worker` 进程。然后 `RUN_INTEGRATION=1 .venv/bin/pytest -q`。测试使用随机独立租户；不会清空用户租户。对象存储中可能保留测试产生但已无数据库引用的孤立对象，V1 尚未实现自动 GC。

恢复测试真的启动并 SIGKILL 测试 Worker；为加快测试，由测试管理员注入租约过期。不要将这个耗时描述成真实 45 秒故障检测时间。记录位于 `artifacts/recovery/`。

三段演示：

1. 技术选型：新建检索方案比较 → 查看任务／补证 → 点击报告引用 → 检查原文 → 导出 Markdown。
2. 论文研读：上传文本 PDF → 查看原理、实现、实验、局限和研究方向 → 点击引用 → 查看 PDF 页与原文片段。
3. 恢复：执行 `pytest tests/test_failures.py -k real_worker_process_kill -q` → 对照已保存调用、fence 代次和恢复结果。

Playwright 案例在 `apps/web/tests`，需要已启动的完整 fixture 环境、测试租户文件；先运行 `.venv/bin/python scripts/create_demo_pdf.py` 生成 `artifacts/demo-paper.pdf`。本机使用 Chrome；其他系统通过 `CHROME_PATH` 指定浏览器。运行命令为 `cd apps/web && pnpm exec playwright test`，截图输出在 `artifacts/ui/`。合成数据演示用于证明产品流程，不能证明模型研究质量。

## 可观测性

运行详情和 `/usage` 提供累计调用与费用；动作事件只保存脱敏元数据。可设置 `RESEARCH_TRACE_FILE=.local/traces.jsonl` 输出 OTel 元数据，trace ID 由 run UUID 派生。供应商原始协议只在受租户保护的恢复状态中使用，不作为研究事实或普通日志。

上下文记录包含前后字节上界估计、被选入／省略的工件、brief 哈希；供应商返回的实际 token 和缓存命中另行保存。费用为配置价格估算；未知调用保留预留，需要后续对账而非自动当作免费。


## 合成评测与最终验收记录

停止常驻 Worker 并确认队列空闲，运行 `.venv/bin/python scripts/evaluate_fixtures.py`，会使用第二测试租户执行 20 个开发 briefs 和 4 个基线／消融样例，不调用付费 API。真实测试结果和研究 run ID 统一见 [verification-results.md](../verification-results.md)。独立测试结束后使用 `RESEARCH_MODE=live docker compose up -d api worker` 恢复服务。
