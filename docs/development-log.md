# 开发日志

本日志记录实际开发事实。`observed` 表示开发或联调实际遇到；`injected` 表示人为故障测试；`risk` 表示尚未验证的风险。测试使用的合成内容不等于真实研究结果。

## 2026-09-11：开始 V1 实现

- 分支：`research-blueprint-v1`；初始仓库只有 README、蓝图和 Git 配置，保留此前两份文档的未提交修改。
- 实施顺序：运行与证据闭环 → 四角色与恢复 → 工作台与隔离 → 对照评测 → 面试深挖文档。
- observed：Docker 客户端已安装，但 daemon 未运行；`docker info` 无法连接 socket。后续验证需启动 Docker 或使用独立本地服务，不能将跳过的集成测试记录为通过。
- risk：DeerFlow 的 DeepSeek 适配保留 `reasoning_content`，官方协议也要求 thinking 工具循环回传。需要测试序列化与 checkpoint 恢复；尚未发生本项目线上故障。
- 真实 API 联调累计预算上限 10 美元；缺少密钥时只运行明确标记的离线 fixtures。

## 记录格式

每个问题记录：类别、场景/约束、现象、版本与 run/trace、复现步骤、证据、根因、尝试与取舍、修复、复测、剩余限制。未知根因不写成已定位，后续补充结果保留原始上下文。

## 2026-09-11：独立环境与本地基础设施

- observed：创建项目 `.venv` 后，第一次 pip 安装因沙箱 DNS/网络不可用失败，错误涉及 hatchling 下载；这是环境访问问题，不是依赖版本不存在。经授权联网安装后成功，生成 `uv.lock`；未向系统 Python 安装项目依赖。
- observed：沙箱内 LaunchServices 报 Docker 应用不可启动；允许启动本机 Docker 后成功。访问 Docker socket 也需要相应执行权限。
- observed：Docker Hub 的 `minio/minio` 镜像拉取返回 access denied。对照 MinIO 官方仓库的 Compose 示例，改为官方 `quay.io/minio/minio` 同版本镜像；结果待拉取验证。

后续验证：Quay 镜像成功启动；数据库迁移通过；真实 PostgreSQL/MinIO 的首轮 8 项集成测试通过。

## 2026-09-11：PDF.js 与追踪协议集成

- observed：前端生产构建在 TypeScript 检查时报 `isEvalSupported` 不属于当前 PDF.js 参数类型。锁定版本为 5.7.284，移除过期参数，保留本地打包 worker 和 canvas 渲染。重新构建成功；不通过类型断言隐藏协议变化。
- observed：接入 OTel 的故障测试实际失败：`TraceFlags.SAMPLED` 是整数，SDK 的 ParentBased sampler 读取 `.sampled` 时抛出 AttributeError。改为 `TraceFlags(TraceFlags.SAMPLED)`，保留统一 run trace ID。该问题发生在测试调用执行前，不是供应商超时的根因；修复后需重跑故障套件。
- 代码检查发现：全局队列领取可能使独立评测拿到别的 run。增加 expected run 参数和迁移 0002，使评测只领取自己创建的运行；常驻 Worker 保留全局领取逻辑。

## 2026-09-11：工作台协议版本漂移（observed）

- 场景：本地 Worker 和 Docker API 在开发中使用了不同构建。技术比较 run `f0a2e54f-0d6b-461e-9129-2b82421eac97`、论文 run `d9762957-170e-4479-b9ed-24245cc39a5c` 均生成了报告，界面却持续等待。
- 证据：浏览器测试超时，API 的报告读取为 422；Worker 新增 `cell_claim_ids`，旧 API 的严格 Pydantic schema 拒绝该字段。前端原先将所有报告读取错误吞为 null。
- 修复：只有 404 被解释为暂无报告；其他读取失败显示错误。创建 run 保存非敏感配置和代码指纹；Worker 构建不同则在付费调用前中断。API／Worker 同步部署。迁移 0003 保留配置快照。
- 复测：真实 PostgreSQL/MinIO 的版本不一致测试通过；完整镜像重建后，两项真实 Chrome 测试通过（12.5 秒），覆盖报告生成、引用定位、Markdown 导出、会话恢复与移动端 PDF。截图位于 `artifacts/ui/`。
- 限制：这阻止不兼容代码接管，并不提供任意旧 checkpoint 的自动迁移。

## 2026-09-11：故障注入与代码检查

- injected：真实 SIGKILL Worker，覆盖 fetch 响应已提交及 Writer 响应已提交、父图 checkpoint 尚未完成的窗口。通过测试管理员注入租约过期；恢复后 fence 增加，已提交调用没有重复执行，预算累计保留。记录位于 `artifacts/recovery/`。
- injected：429、超时未知费用、非法 JSON、预算竞争、跨租户读写、取消后迟到响应、SSRF 重定向到 localhost、未经授权局部修订和比较表引用绕过。最后一次完整套件为 39 项通过（15.13 秒）；后续联调修复的最终复测另行记录。
- inspection：补证后新审查可能忽略旧报告的修改位置；现将未解决修改位置保留到局部修订完成。比较表单元格引用也需通过 accepted-claim 检查，并写入 Markdown 导出。论文结构块使用固定 `paper` 位置授权修订。
- inspection：同内容跨域转载仅按域名分组会重复计数；在 run manifest 与 Reviewer 输入中，按同域或相同原文字节哈希做保守合并。不声称实现语义近重复检测。

## 2026-09-11：首轮 DeepSeek／Tavily 联调（observed）

- 密钥保存在 Git 忽略的 `.env`，未进入日志；DeepSeek `/models` 返回 200，账户支持 `deepseek-flash` 和 `deepseek-v4-pro`。模型调用与 Tavily 搜索均返回 200。
- 场景：中文技术知识库的关键词、向量、混合检索与重排选型；强调 ERR_AUTH_42 精确标识符、官方来源、不编造中文性能数字。run `b4b6dcfc-e7a5-4fdc-a5a3-3b6ae5612c06`，thinking low，单 run 上限 $0.50。
- 现象一：模型一轮提出多次搜索，耗尽 6 次搜索额度；后续 BudgetExceeded 直接退出 Researcher，导致已经读取的 pgvector 原文没有进入 extraction。所有 4 个任务因此失败，最终 0 claims / 0 spans。
- 定位：按 actions 查看 read_source 已完成、search 达到 6 次、后续 task.stop_reason 为 `BudgetExceeded:search_calls`，但 finding 为空；异常跨过了子图 tools→extract 的状态提交。
- 修复：将额度拒绝转换为成对工具反馈，保存先前 read_slices；搜索用尽后隐藏 search 并告知剩余额度，继续 fetch/read 或提取。新增每任务搜索上限，B0 保留全局额度；注入同样的 read＋搜索突发序列做回归。
- 现象二：长中文报告的审查消息触发 prompt_context_limit；原上下文预算没有预留 system/schema/序列化开销。该上限使用保守 UTF-8 字节上界，不是 DeepSeek 的实际 tokenizer 上限。
- 修复：装配时预留协议开销、缩减重复任务／来源元数据，默认单次保守上界从 32k 调整到 64k；用户 brief／约束仍保留。20% 收尾预留同时应用于模型调用数和累计 token。
- 结果：首轮只交付明确 needs_review 的部分包，未作为成功研究。10 次模型、11 次工具（其中 6 次搜索）、49,652 token，105.41 秒，估算 $0.090515796，未知预留 $0。数据在 `artifacts/live/`；这是一例联调失败，不是质量基准。

### 第二轮真实复测：证据保留成功，累计 token 触发部分交付

run `7e4d8836-afe8-4a8d-a815-7624a3153813` 使用同一问题与模型，但搜索结果与模型生成不是冻结控制变量。6 个来源、27 claims / 27 spans 已保存；27 次模型、27 次工具（6 次搜索）、194,420 token、233.80 秒、估算 $0.133852008。审查指出多个 claim 的附加从句超出了所引原文片段，说明“quote 能精确定位”不等于“quote 支持整句话”。之后的调用预留触发累计 token 上限，交付保留这些证据的部分包，质量为 needs_review。

本轮还真实出现一次结构化响应校验失败，已计费并有限重试；一次 fetch URL 未进入发现白名单而被拒绝。该白名单要求 URL 来自用户种子或搜索结果，避免模型随意构造外部请求。审查发现的问题没有通过降级门禁隐藏。

后续调整：抽取要求一句一个主张，引用需覆盖全部断言，不把导航目录作为事实；精简重复任务约束、finding 摘要和报告篇幅，报告审查只携带被该报告引用的 claims/spans。缺少必要固定上下文空间时返回明确部分包，避免中断后无限重试。小规模 smoke 收敛为两个任务、每任务 8 次工具、最多 4 次搜索，不代表修改默认 V1 总额度，也不构成同预算质量提升对照实验。

最新功能回归：40 项通过（16.14 秒）；真实 Parser 容器探测确认无法连接公网且没有模型／数据库／对象存储凭证。

### 第三轮：写作结构化输出失败与诊断缺口（observed）

run `1b858689-9b4a-4467-8a70-80f9e670b13b` 已进入写作，但连续 3 次响应未通过校验，终态 failed / attempts_exhausted。20 次模型、17 次工具、177,732 token、226.68 秒、估算 $0.113123812。

旧实现将截断、空内容和 schema 错误统一记为 invalid_structured_response，并丢弃失败响应。三次写作输出接近当时输出额度，因此**怀疑**额度不足；因旧记录没有 finish_reason/校验路径，不能还原并断言三次均因截断。本次明确暴露的是诊断不足及相同提示的无效重试。

修复：失败响应保存在受 RLS 保护的动作记录，普通 usage 接口只返回 finish_reason、schema 错误位置与类型，不公开原始推理；重试附修复反馈，并为变长的请求重新预留预算。Writer/Patcher 最大输出 16k，Reviewer 8k，规划／抽取／工具决策 4k；仍受 profile、累计 token 和费用约束。供应商最终失败也保留部分研究包，执行状态仍标为失败，不伪装成功。

注入复测：截断与非法 JSON 两种路径均能修复；失败响应不能跨租户读取；下一次调用的预留反映新增上下文。完整测试达到 42 项通过（16.91 秒）。这证明工程路径，不证明模型报告质量或较旧配置有统计收益。

### 第四轮：诊断证实 thinking 消耗全部输出额度（observed）

论文 run `42495d50-ea8c-4042-8429-47eb082236be` 使用真实 RRF 两页 PDF。新诊断确认 extraction 的三次响应 `finish_reason=length`，正文长度均为 0，completion token 接近 4096；仅统计字段长度，没有在日志公开原始推理内容。这次可以证实：把“最终 JSON 很短”当作“总输出可以很小”忽略了 thinking 的额度消耗。

核对 [DeepSeek thinking 文档](https://api-docs.deepseek.com/guides/thinking_mode/)：low 配置有效；无工具请求不需要回传历史 reasoning_content，携带工具时则需要保留。停止该诊断 run 后，已知估算费用 $0.041327544、69,481 token；保留 $0.0119613 的迟到／未知调用预留，未擅自清零。这是开发者主动终止自己的测试，不是用户取消研究的真实业务事故。

修复：结构化调用从 16k 输出额度开始；截断时在最多 2 次重试、累计 token／费用约束内增至 profile 上限（默认 32k）。工具决策从 4k 开始。非工具修复请求不带供应商忽略的旧 reasoning_content；失败原始协议仍受 RLS 保护。每次重试重新计算完整请求预留，避免修复消息变长后突破预算。42 项回归通过（17.76 秒）；真实复测另行记录。

### 第五、六轮：模型重写引文与 PDF 阅读顺序（observed）

论文 run `29ff6bb9-a631-422c-b85d-f31c371d263e` 在提高 thinking 输出额度后生成了结构化论文解释，但 8 个候选引文均不能精确匹配解析原文，最终 0 claims / 0 spans。12 次模型、3 次工具、73,871 token、133.61 秒、估算 $0.049739040。没有把这些重构的句子作为原文证据接受。

排查分两层：渲染 RRF 两页 PDF 与 pdfplumber 输出对照，默认排序存在左右栏交错、词间空格丢失；改为 `use_text_flow=True, x_tolerance=1`，保留页码与 block 边界。对旧引文再做空白／断词归一化仍未匹配，说明不能把全部失败归因于版式，模型也在重写原文。

修复：应用为已读原文生成稳定 `passage_id` 和字符范围，模型只选择 ID，程序负责复制精确 quote。来源版本纳入 parser version，旧版本不覆盖。保持 quote 哈希与偏移验证，不通过模糊匹配把模型改写伪装成引文。新增两栏 PDF 顺序及 passage 偏移回归；43 项测试通过（17.18 秒）。

真实复测 run `23a75041-0116-40d9-9892-fc6e3f3c2d4b` 保存 12 claims / 7 spans、1 个原始 PDF，输出问题、原理、实现、实验、局限与可检验研究方向。12 次模型、4 次工具、91,711 token、157.05 秒、估算 $0.052271532，无重试、无未知预留。旧版指标文件 `citation_locator_errors=16` 实际统计的是全部发布门禁问题；这 16 项是表格单元格缺引用和数字核验，不是 16 个原文偏移错误。后续将总数拆为 `report_gate_errors`，locator 指标只统计原文定位／哈希问题。

限制：模型可选择一个真实但不充分的 passage；页级 bbox 不是逐词视觉框，复杂公式／两栏版式仍需人工核验。本轮是未冻结随机输出的功能复测，不能据此报告引用正确率提升。

### 发布门禁发现缺陷，却没有机会局部修订（observed）

上述论文报告表格缺少 `cell_claim_ids`。模型 Reviewer 未指出这一结构缺陷，程序在 publish 阶段才发现，此时已越过 patch 路由。最终质量明确为 needs_review，但错过了一次可用的修订机会。

修复：抽出共用 `report_checks`，在草稿审查后和发布前都执行；按稳定节点 ID 汇总程序缺陷，与模型审查合并，再进入有界局部修订。Patcher 同样使用上下文装配，获得引用原文；未解决项仍由最终门禁阻止质量通过。

注入复测模拟“模型审查 sufficient=true、没有 findings，但表格漏引用”，验证程序仍路由到指定 matrix 节点；替换为明确 unknown 后修复结构缺陷，仍不会伪造全局证据。首轮测试暴露全 unknown 表格被误判为无证据事实，已修复；旧局部修订测试重用了同一动作轮次但更改输入，触发不可变快照冲突，改为真实的下一轮次。最终 44 项通过（17.30 秒）。

### 第七轮：固定区误装全部证据，写作前超限（observed）

技术选型 run `70885cae-0a2f-415a-9d2d-0a7a11bc0b91` 成功获取 4 个来源，保存 36 claims / 32 spans，但 Writer 装配前触发 `pinned_context_exceeds_limit`，返回可追溯的部分包。20 次模型、17 次工具（3 次搜索）、138,592 token、130.02 秒、估算 $0.080243172。4 份上下文快照中 1 份省略了附加工件；这只是单次 run 的 25%，不是项目长期省略概率。

定位证据：按 UTF-8 保守上界统计，claims 约 19,339、spans 约 25,442、review 约 6,776，叠加 brief 和协议元数据超过为固定区留下的空间。旧 ContextBuilder 只筛选额外来源窗口，claim／span 本体却被放进 pinned，所以不能靠省略窗口解决。

修复：brief、用户约束和任务要求保持固定；claim 与其 span 成组进入可选工件，装配后去重 span 并恢复领域字段。省略 ID 写入快照；Reviewer 不得接受当次未提供的 claim，并显式记录未审查覆盖。Patcher 使用同样策略。完整来源与原有 claim 仍持久保存；这不是删除长期数据，也不是 KV cache 压缩。

新增回归用大证据集验证：部分选择、主张与证据不分离、约束完整、实际请求大小在边界内、快照列明省略。真实复测及最终结果见后续记录和 verification-results。

## 合成评测工具验证（fixture，不是模型质量实验）

`artifacts/fixture-evaluation/20260911T231341Z/`：B2 运行 20 个开发 briefs；B0、B1、B2 串行、B2 全文各 1 次，共 24 次。全部 completed / unchecked，发布检查错误为 0，未调用付费 API。人工质量列保留 null。该批证明评测入口、基线与消融参数能执行，不构成真实研究能力或速度比较；后续代码修复后的重新验证以较新的目录为准。


### 最终技术选型复测与验收（observed）

run `59a284fe-e92e-4a58-9c58-5d50dd0a7f03` 完成研究、写作、审查、一次局部修订及发布，未重现 Writer 固定区超限。报告 revision 2，7 个节点、4 行比较矩阵、16 处单元格引用；4 个来源、16 claims / 9 spans。20 次模型、17 次工具（4 次搜索）、183,227 token、232.11 秒、估算 $0.123929572。8 份上下文快照中 2 份有省略；保留其 ID 和固定 brief 哈希。

最终质量仍为 needs_review：两个数字检查项和覆盖／支持要求未解决；定位／哈希错误 0。5 次 fetch 失败记录为 ValueError，不能把它们写成成功工具调用；还需完善工具错误分类与检查模型对未读来源的描述。没有为追求通过而移除门禁，也没有继续无限付费尝试。

完整后端 45 项通过（16.98 秒）；最终 fixture sweep 在 `artifacts/fixture-evaluation/20260911T232846Z/` 的 24 次均 completed / unchecked、门禁错误 0。同步构建 API／Worker／Parser／Web 成功。8 次真实 run 已知估算费用合计 $0.685002476，未知预留 $0.0119613，未超过 $10 联调限制。

README 已补全用途、角色、memory／上下文／harness／workflow、环境依赖、两种运行模式、API／CLI、测试、评测和复现边界；具体原始设计、实际事故与未执行实验在面试文档中分别说明。密钥仍只在 Git 忽略的私有配置中，待提交候选文件扫描无真实密钥。


最终浏览器复测：真实 Chrome 2 项通过（11.3 秒），截图检查桌面引用高亮和移动端 PDF；测试后 API／Worker 恢复 live 模式，工作台保留本地运行历史。完整质量评测和保留集实验尚未执行，已在 README／verification-results 中标注。
