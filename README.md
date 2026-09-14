# Deep Research Agent

一个证据优先、可恢复、受预算约束的多 Agent 深度研究工作台。系统把研究意图编译成任务 DAG，在授权来源内检索与阅读，生成可定位的 Claim / EvidenceSpan，经审查和写作后交付带引用的结构化报告。

当前版本：**v0.2.0（2026-09-14）**。

v0.2.0 的重点不是增加更多 Agent，而是让现有协作链在真实运行中可控收口：跨轮对话改为紧凑进度 capsule、token 按阶段保留、非法补证不再导致 Run 崩溃、Embedding 可在离线容器中稳定启动，并且即使研究或审查预算耗尽，也会经过质量门禁生成可解释的 `needs_review` 报告。

项目已有真实 DeepSeek / Tavily 联调和浏览器工作台，但仍是本地开发与研究系统，不是生产合规 SaaS。`completed` 表示执行结束，不表示研究结论已被人工认证；fixture 输出为合成内容，不能作为真实选型依据。实测范围见 [验证结果](docs/verification-results.md)。

## 核心能力

- 支持 `technical_comparison`、`paper_review`、`general_research` 三类研究模板。
- 将自由输入编译为 `ResearchIntent`、阶段任务和依赖 DAG，按依赖并发派发 Researcher。
- 支持公开 HTML、Markdown、文本型或扫描型 PDF、上传文件和显式种子 URL。
- 在同租户、当前 Run 授权来源内执行 CJK 词法检索、768 维向量检索、RRF 融合和范围回读。
- 以 `SourceVersion → EvidenceSpan → Claim → ReportNode` 保存证据链，可回到原文字符偏移、页码和可用 bbox。
- Reviewer 同时执行模型审查和程序门禁；Writer / Patcher 只能使用被提供的 Claim / Span，局部修订受稳定节点 ID 限制。
- PostgreSQL 持久化队列、租约、fencing token、父图/子图 checkpoint、动作账本、预算和 SSE 事件。
- 工作台展示计划、任务、来源、引用、报告、用量、缓存命中、阶段预算和降级原因。
- 支持两租户 RLS 隔离、私有 MinIO 工件、受限公网抓取和离线 Parser / Embedding 容器。

## 执行流程

```mermaid
flowchart TD
    UI[Next.js 工作台 / CLI] --> API[FastAPI]
    API --> DB[(PostgreSQL<br/>队列·状态·预算·账本)]
    DB --> W[LangGraph Worker]
    W --> I[Research Director<br/>冻结意图与验收条件]
    I --> P[Planner<br/>阶段任务 DAG]
    P --> R[Researcher 子图<br/>最多 3 路并发]
    R --> T[Search / Fetch / Retrieval / Read]
    T --> E[解析与证据抽取]
    E --> OBJ[(MinIO)]
    E --> IDX[Indexer<br/>FTS + pgvector]
    R --> V[Reviewer<br/>模型审查 + 程序门禁]
    V -->|合法补证且预算允许| R
    V --> WR[Writer]
    WR --> C[报告检查]
    C -->|授权节点需要修订| PA[局部 Patcher]
    PA --> C
    C --> PUB[发布 passed / needs_review]
```

角色只负责语义判断；URL 权限、工具白名单、预算、引用复制、哈希、调度和状态转移由程序控制。Reviewer 提议的补证任务必须满足冻结的 `stage → allowed methods` 矩阵。非法任务会记录 warning 和 `review.gap_rejected` 事件，不会被静默映射，也不会再触发一次模型“修复”。如果没有合法补证，流程直接进入 Writer，并在最终报告披露缺口。

## 上下文与 Token 策略

### ResearchProgress capsule

每个合法工具批次结束后，下一轮不再重放完整消息历史，而是从固定 system / brief、当前任务、紧凑 `ResearchProgress` 和相关证据重新构造请求。capsule 只保存：

- 授权 source ID 与候选 URL；
- 已读且合并后的字符范围；
- 检索命中、验收条件覆盖和未解决项；
- 最近工具结果和连续无增益轮数。

完整模型请求、响应和工具结果仍保存在受 RLS 保护的 action 账本中，用于恢复与审计。带工具调用的一轮会保留 assistant/tool 配对及必要 `reasoning_content`，只在协议闭合后切换 capsule。跨 Agent 默认最多传递 16 组按相关性稳定排序的 Claim / EvidenceSpan bundle，其余证据保留在数据库并记录 omission。

连续两轮没有新增来源、Claim、Span、冲突或验收覆盖时，Researcher 提前停止工具循环。易变的剩余额度放在消息尾部，固定提示保持稳定顺序，以减少重复输入并提高供应商缓存可复用性。

### 默认预算

外部 `RunProfile.max_tokens=250000` 仍是绝对硬上限；内部 soft target 为 180,000 token，不新增用户配置项。

| budget group | soft budget | 典型角色 |
|---|---:|---|
| Scope / Plan | 18,000 | Research Director、Planner |
| Research / Extraction | 90,000 | Researcher、Source Analyst |
| Review / Gap | 24,000 | Reviewer、补证调度 |
| Writer / Patch | 39,000 | Writer、Patcher |
| Contingency | 9,000 | 前序阶段重试与波动 |

未使用额度只向后流转；Writer / Patch 的 39k 不会被前期研究占用。阶段累计门槛为 27k / 117k / 141k / 180k。系统分开记录：

- `serialized_bytes`：请求尺寸诊断；
- `estimated_input_tokens`：soft target 调度；
- provider actual tokens：最终账本和 250k hard cap。

70% 时强制 capsule 并裁剪重复证据；85% 时停止发现新来源；95% 时停止补证并进入 Review → Writer。研究预算或 retrieval 配额耗尽会停止继续派发，取消依赖失败任务，并交付按任务阶段组织的 `needs_review` 报告；不会再绕过 Reviewer / Writer 生成无结构的 `revision 999` 任意 Claim 列表。

## 证据与索引

```text
ReportNode / table cell
        ↓ claim_ids / cell_claim_ids
Claim（原子主张、立场、局限）
        ↓ span_ids
EvidenceSpan（source_id、parsed_hash、start/end、quote_hash、page/bbox）
        ↓
SourceVersion（原文字节哈希、解析版本、对象 key、来源地址）
```

搜索摘要只用于发现 URL，不能直接成为报告事实。模型从已读原文的稳定 `passage_id` 中选择片段，程序复制原文并绑定偏移，避免模型改写“引文”。发布时检查来源版本、偏移、quote 哈希、accepted Claim、表格单元格引用和数字存在性。

精确定位不等于语义支持：真实原文片段仍可能不支持 Claim 的附加从句。Reviewer 和最终人工复核仍是必要环节。

Embedding 固定使用 `Alibaba-NLP/gte-multilingual-base@9bbca17` 和 768 维索引。镜像构建时还固定 `Alibaba-NLP/new-impl@40ced75c3017eb27626c9d4ea981bde21a2662f4`，复制动态模块并完成真实 warm-load；运行时只读、离线。`/health` 必须完成短文本推理并得到有限、非零、归一化的 768 维向量才返回 ready。Indexer 对暂时性 503 做 1/2/4 秒有界退避，保留 `lexical_ready`，恢复后用新 index version 补建，不覆盖旧可用版本。

PDF `auto` 模式优先使用本地 Docling、RapidOCR、TableFormer、公式 enrichment 和 SmolVLM 图片描述；不可用时回退 pdfplumber 并标记 extraction method。复杂公式、OCR、表格和页级 bbox 仍需人工视觉确认。

## 快速启动

需要 Git、Docker Engine / Docker Desktop 和 Compose v2。以下命令在仓库根目录执行：

```sh
cd "/path/to/deep-research-agent"
git switch main

if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env

# 首先运行不消耗模型额度的完整 fixture 栈。
RESEARCH_MODE=fixture docker compose up --build -d

# 首次创建本地测试租户。
umask 077
mkdir -p .local
docker compose run --rm -T migrate research seed > .local/test-tenants.jsonl
chmod 600 .local/test-tenants.jsonl
```

首次构建 Parser / Embedding 会下载并 warm-load 锁定模型，通常明显慢于后续构建。启动完成后打开 <http://localhost:13000>，使用 `.local/test-tenants.jsonl` 中的一条 `api_key` 登录。该 Key 是本项目的租户凭证，不是 DeepSeek / Tavily Key；不要提交或公开。

| 服务 | 地址 |
|---|---|
| 工作台 | <http://localhost:13000> |
| API / OpenAPI | <http://localhost:18000> / <http://localhost:18000/docs> |
| PostgreSQL | `localhost:15432` |
| MinIO API / Console | <http://localhost:19000> / <http://localhost:19001> |
| Parser / Embedding | 仅 Compose 内部网络 |

健康检查：

```sh
docker compose ps
curl --fail http://localhost:18000/health
docker compose logs --tail=100 api worker indexer embedding
```

只执行 `docker compose restart` 不会把本地新代码写入旧镜像。代码或 Dockerfile 更新后使用 `docker compose up --build -d`；需要强制重建时使用 `docker compose build --no-cache <service>`。`docker compose down` 保留命名卷，`docker compose down -v` 会删除项目数据库和对象存储数据。

### 真实 DeepSeek / Tavily

在 `.env` 填写：

```dotenv
RESEARCH_MODE=live
DEEPSEEK_API_KEY=<your-deepseek-key>
TAVILY_API_KEY=<your-tavily-key>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_THINKING=enabled
DEEPSEEK_EFFORT=low
LIVE_CAMPAIGN_USD=10
```

然后同步重建所有依赖后端代码的服务：

```sh
RESEARCH_MODE=live docker compose up --build -d api worker indexer migrate parser embedding web
docker compose run --rm -T migrate research doctor
curl --fail http://localhost:18000/health
```

`doctor` 不打印密钥，也不发起付费调用。live 模式缺少凭证时会明确失败，不会静默切换 fixture。新 Run 会冻结非密钥配置和 runtime fingerprint；修复前 checkpoint 不允许由新代码继续执行，请创建新 Run。旧 Run 仍可查询和导出。

## 本地开发

验证基线为 Python 3.12、uv、Node.js 22 和 pnpm 10。后端依赖锁定在 `uv.lock`，前端锁定在 `apps/web/pnpm-lock.yaml`。

```sh
uv sync --extra dev --frozen
corepack enable
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web run prebuild

docker compose stop api worker web
make infra
make migrate
```

分别启动：

```sh
make api
make worker
make indexer
make web
```

修改 `.env`、运行模式或代码后，需要重启对应本地进程。不要让本地和 Compose 的 API / Worker 同时消费同一个数据库队列。

## API 与用量

除 `/health` 和 OpenAPI 外，接口使用 `Authorization: Bearer <tenant-api-key>`。

| 接口 | 用途 |
|---|---|
| `POST /research-runs` | 用 `Idempotency-Key` 创建 Run |
| `GET /research-runs`、`GET /research-runs/{id}` | 列表、状态、计划、任务和来源摘要 |
| `POST /research-runs/{id}/cancel`、`/resume` | 取消或恢复可恢复的 Run |
| `GET /research-runs/{id}/usage` | 向后兼容的 action 用量数组 |
| `GET /research-runs/{id}/usage-summary` | totals、soft/hard 剩余量、分组调用/缓存/费用和 degradation events |
| `GET /research-runs/{id}/events` | 持久化事件和 SSE 补读 |
| `POST /uploads`、`GET /uploads/{id}` | 上传与异步解析状态 |
| `GET /sources/{id}`、`/raw` | 来源元数据和受保护原文 |
| `GET /evidence-spans/{id}`、`/crop` | 引用片段和可用视觉 crop |
| `GET /research-runs/{id}/report` | 研究包或指定 revision |

接口契约以运行中的 OpenAPI 和 `src/research_agent/contracts.py` 为准。

## 验证

后端完整测试会使用真实 PostgreSQL / MinIO，但不调用付费模型。必须先停止常驻 Worker，否则它可能领取集成测试创建的 queued Run，破坏队列和故障恢复测试时序。

```sh
docker compose stop worker
make infra
make migrate

.venv/bin/ruff check src tests migrations scripts
.venv/bin/pytest -q -m 'not integration'
RUN_INTEGRATION=1 .venv/bin/pytest -q
docker compose config -q
git diff --check

# 完成后恢复服务
docker compose up -d worker
```

前端与浏览器：

```sh
pnpm --dir apps/web build
pnpm --dir apps/web exec playwright test
```

当前 v0.2.0 工作树验证结果为 **72 passed**，并通过 Ruff、Compose 配置、前端构建、真实浏览器测试、Embedding 768 维离线健康检查和固定工具序列通信基准。真实 3DGS 修复后完整质量复测尚未执行，不能把工程回归写成研究质量提升。命令、Run ID 和未验证结论见 [验证结果](docs/verification-results.md)。

## 可靠性与安全边界

- 全局最多 1 个活跃 Run、10 个等待 Run；Worker 使用租约和递增 fencing token 防止旧进程覆盖新结果。
- action ID 稳定；已提交外部结果可在 checkpoint 恢复时复用。供应商响应返回但尚未落库的窗口无法保证 exactly-once，未知费用保留预留。
- 取消会先持久化并使旧 fence 失效；浏览器断线不等于取消，供应商中的请求也不保证立即停止计费。
- PostgreSQL 业务表和 checkpoint 强制 RLS；MinIO 使用私有桶和租户命名空间。
- 抓取只允许公网 HTTP(S) 80/443，检查 DNS 和重定向，限制大小、类型和超时。
- Parser / Embedding 使用内部网络、只读文件系统、cap drop 和无密钥环境；本地主机进程模式不具备相同网络隔离。

## 已知限制

- 真实 heldout 质量评测、B0/B1/B2 同预算对照、citation precision 和人工事实支持率尚未完成。
- Reviewer 与 Writer 可能使用同一模型，存在相关性错误；`passed` 也不替代人工核验。
- 搜索结果可能遇到反爬、导航页、客户端挑战或低质量聚合页；当前不会把抓取失败伪装成有效证据。
- 新来源建立语义索引存在异步延迟；未 ready 时仅使用 lexical fallback，不等于 Embedding 服务故障。
- 没有自动跨 Run 的用户画像/长期语义记忆、任意旧 checkpoint 迁移、SSO、对象 GC 或跨集群调度。
- 系统不执行论文代码，不验证论文新颖性，也不能保证复杂 PDF、公式、OCR 和表格理解正确。

## 仓库导航

| 路径 | 内容 |
|---|---|
| `src/research_agent/graph.py` | 研究图、角色边界、预算收口和降级报告 |
| `src/research_agent/context.py` | capsule、上下文装配、证据选择和 soft target |
| `src/research_agent/gateway.py` | 模型/工具动作、预留、结算和协议诊断 |
| `src/research_agent/db.py` | RLS 数据访问、队列、索引、账本与 usage summary |
| `src/research_agent/evidence.py` | 来源版本、passage、Claim / Span 和报告渲染 |
| `src/research_agent/indexer.py` | 词法/向量索引与恢复 |
| `apps/web/` | Next.js 工作台和 Playwright 测试 |
| `migrations/` | PostgreSQL schema 与数据迁移 |
| `evals/`、`scripts/` | fixture、smoke、冻结语料和评测入口 |
| `docs/` | 设计、开发事故、验证结果与实现边界 |

进一步阅读：

- [多 Agent Token 策略](docs/multi-agent-token-strategy.md)
- [检索与解析](docs/retrieval-and-parsing.md)
- [开发日志](docs/development-log.md)
- [面试深挖：实现、事故与证据边界](docs/interview-deep-dive.md)
- [验证结果](docs/verification-results.md)
- [Research Agent V2 设计](docs/research-agent-v2-design.md)
