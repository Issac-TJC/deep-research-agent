"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
const PdfPage = dynamic(() => import("./PdfPage"), { ssr: false });
type Dict = Record<string, any>;
const templates = {
  technical_comparison: "技术方案比较",
  paper_review: "论文与实验报告",
  general_research: "通用研究",
};
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const phaseNames: Dict = {
  intake: "整理输入",
  scoping: "理解研究意图",
  planning: "制定计划",
  research: "检索与研读",
  review: "审查证据",
  writing: "撰写报告",
  patching: "局部修订",
  citation_check: "核对引用",
  starting: "开始研究",
  recovering: "恢复运行",
};
const stageNames: Dict = {
  introduction: "Introduction / 问题与意义",
  related_work: "Related Work / 相关工作",
  methodology: "Method / 方法",
  experiment: "Experiment / 实验",
  results: "Results / 结果",
  discussion: "Discussion / 讨论",
  conclusion: "Conclusion / 结论",
};
const parseLabels: Dict = { ready: "解析完成", fallback: "降级", failed: "失败" };
const indexLabels: Dict = {
  pending: "解析中",
  processing: "解析中",
  lexical_ready: "词法可用",
  ready: "混合检索可用",
  failed: "失败",
};
async function api(path: string, init?: RequestInit) {
  const r = await fetch("/api/backend" + path, init);
  if (!r.ok) throw Error(`${r.status}: ${await r.text()}`);
  return r.json();
}
async function loadReport(id: string) {
  const r = await fetch(`/api/backend/research-runs/${id}/report`);
  if (r.status === 404) return null;
  if (!r.ok) throw Error(`报告读取失败 (${r.status})：${await r.text()}`);
  return r.json();
}
export default function Home() {
  const [key, setKey] = useState("");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [runs, setRuns] = useState<Dict[]>([]);
  const [active, setActive] = useState("");
  const [run, setRun] = useState<Dict | null>(null);
  const [report, setReport] = useState<Dict | null>(null);
  const [usage, setUsage] = useState<Dict[]>([]);
  const [events, setEvents] = useState<Dict[]>([]);
  const [question, setQuestion] = useState("");
  const [uploads, setUploads] = useState<Dict[]>([]);
  const [tab, setTab] = useState("report");
  const [selected, setSelected] = useState<Dict | null>(null);
  const [busy, setBusy] = useState(false);
  const cursor = useRef(0);
  const currentRun = useRef<Dict | null>(null);
  const loadRuns = useCallback(async () => {
    const list = await api("/research-runs");
    setRuns(list);
    setConnected(true);
  }, []);
  useEffect(() => {
    loadRuns().catch(() => {});
  }, [loadRuns]);
  const refresh = useCallback(
    async (id: string) => {
      const next = await api(`/research-runs/${id}`);
      setRun(next);
      currentRun.current = next;
      const [pkg, calls] = await Promise.all([
        loadReport(id),
        api(`/research-runs/${id}/usage`),
      ]);
      setReport(pkg);
      setUsage(calls);
      await loadRuns();
    },
    [loadRuns],
  );
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    cursor.current = 0;
    setEvents([]);
    setSelected(null);
    setReport(null);
    (async () => {
      try {
        await refresh(active);
        while (!controller.signal.aborted) {
          const response = await fetch(
            `/api/backend/research-runs/${active}/events?after_seq=${cursor.current}`,
            { signal: controller.signal },
          );
          if (!response.ok) throw Error("事件连接失败");
          const reader = response.body!.getReader();
          const decoder = new TextDecoder();
          let pending = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            pending += decoder.decode(value, { stream: true });
            let end;
            let changed = false;
            while ((end = pending.indexOf("\n\n")) >= 0) {
              const block = pending.slice(0, end);
              pending = pending.slice(end + 2);
              const data = block
                .split("\n")
                .find((x) => x.startsWith("data: "));
              if (data) {
                const event = JSON.parse(data.slice(6));
                if (event.seq > cursor.current) {
                  cursor.current = event.seq;
                  setEvents((old) => [...old.slice(-499), event]);
                  changed = true;
                }
              }
            }
            if (changed) await refresh(active);
          }
          await refresh(active);
          if (terminal.has(currentRun.current?.status)) break;
          await new Promise((r) => setTimeout(r, 1500));
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      }
    })();
    return () => controller.abort();
  }, [active, refresh]);
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const r = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!r.ok) throw Error(await r.text());
      setKey("");
      await loadRuns();
    } catch (e) {
      setError(String(e));
    }
  }
  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api("/research-runs", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          brief: {
            question,
            template: "general_research",
            upload_ids: uploads.map((x) => x.upload_id),
          },
        }),
      });
      setActive(result.run_id);
      await loadRuns();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function upload(file: File) {
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      let item = await api("/uploads", { method: "POST", body });
      setUploads((old) => [...old, { ...item, filename: file.name }]);
      while (!item.source && !["failed"].includes(item.status)) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const state = await api(`/uploads/${item.upload_id}`);
        setUploads((old) =>
          old.map((entry) =>
            entry.upload_id === item.upload_id ? { ...entry, ...state } : entry,
          ),
        );
        if (state.status === "ready" && state.source_id) {
          const loaded = await api(`/sources/${state.source_id}`);
          item = {
            upload_id: item.upload_id,
            status: "ready",
            source: loaded.source,
            index: loaded.index,
          };
          setUploads((old) =>
            old.map((entry) =>
              entry.upload_id === item.upload_id ? { ...entry, ...item } : entry,
            ),
          );
          break;
        }
        if (state.status === "failed") throw Error(state.error || "文档解析失败");
        item = { ...item, ...state };
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function inspect(sourceId: string, span?: Dict) {
    try {
      const item = await api(`/sources/${sourceId}`);
      setSelected({ ...item, span });
    } catch (e) {
      setError(String(e));
    }
  }
  async function control(action: string) {
    try {
      await api(`/research-runs/${active}/${action}`, { method: "POST" });
      await refresh(active);
      if (action === "resume") {
        setActive("");
        setTimeout(() => setActive(active), 0);
      }
    } catch (e) {
      setError(String(e));
    }
  }
  function citations(ids: string[] = []) {
    return ids.flatMap((id) => {
      const claim = report?.claims.find((c: Dict) => c.id === id);
      return (claim?.span_ids || []).map((sid: string) => {
        const span = report?.spans.find((s: Dict) => s.id === sid);
        return span ? (
          <button
            className="cite"
            key={id + sid}
            onClick={() => inspect(span.source_id, span)}
          >
            查看证据 ↗
          </button>
        ) : null;
      });
    });
  }
  const dollars = (value: any) => `$${Number(value || 0).toFixed(4)}`;
  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">R</span>
          <div>
            Deep Research<small>研究工作台 · V1</small>
          </div>
        </div>
        <p className="side-label">RESEARCH LIBRARY</p>
        <button
          className="new-run"
          onClick={() => {
            setActive("");
            setRun(null);
            setReport(null);
            setSelected(null);
          }}
        >
          ＋ 新建研究
        </button>
        <nav>
          {runs.map((r) => (
            <button
              className={active === r.id ? "run-link active" : "run-link"}
              key={r.id}
              onClick={() => setActive(r.id)}
            >
              <span>{r.brief.question}</span>
              <small>
                {r.status} ·{" "}
                {templates[r.brief.template as keyof typeof templates]}
              </small>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className={connected ? "online" : "offline"}>
            ● {connected ? "已连接工作区" : "尚未连接"}
          </span>
          <p>结论可追溯，过程可复核。</p>
          {connected && (
            <button
              onClick={async () => {
                await fetch("/api/session", { method: "DELETE" });
                location.reload();
              }}
            >
              退出工作区
            </button>
          )}
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">EVIDENCE → UNDERSTANDING → DECISION</span>
            <h1>{active ? "研究进行时" : "从一个值得研究的问题开始"}</h1>
          </div>
          <span className="version">V1 · Research workspace</span>
        </header>
        {error && (
          <div className="error" role="alert">
            {error}
            <button onClick={() => setError("")}>关闭</button>
          </div>
        )}
        {!connected ? (
          <section className="panel connect">
            <h2>连接你的研究工作区</h2>
            <p>输入管理员生成的测试租户 API Key。密钥通过服务端会话保存。</p>
            <form onSubmit={login}>
              <label>
                API Key
                <input
                  type="password"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  autoComplete="off"
                  required
                />
              </label>
              <button className="primary">连接工作区</button>
            </form>
          </section>
        ) : !active ? (
          <section className="new-research">
            <div className="intro">
              <h2>把问题、链接和材料都交给研究 Agent。</h2>
              <p>
                不必先选择研究类型。系统会判断你需要 Introduction、Related Work、方法设计还是实验方案，并为每部分选择合适的做法。
              </p>
              <div className="features">
                <span>01 理解意图</span>
                <span>02 编排研究</span>
                <span>03 形成论文工作包</span>
              </div>
            </div>
            <form className="panel research-form" onSubmit={create}>
              <label>
                你想完成什么研究？
                <textarea
                  required
                  minLength={8}
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  rows={8}
                  placeholder={"直接描述目标，也可以把链接贴在这里。例如：\n读这篇论文并找出可改进之处；先梳理相关工作，再用“现有不足—要做什么—为什么有意义”的 Introduction 结构提出三个切入点。\nhttps://…"}
                />
              </label>
              <div className="upload-zone">
                <label className="upload-button">
                  ＋ 添加 PDF / Markdown / HTML
                  <input
                    type="file"
                    accept=".pdf,.md,.html,.txt"
                    hidden
                    onChange={(e) =>
                      e.target.files?.[0] && upload(e.target.files[0])
                    }
                  />
                </label>
                <small>PDF 支持本地 OCR / 表格 / 公式 / 图片解析 · 单文件最多 20 MB</small>
                {uploads.map((x) => (
                  <span className="chip" key={x.upload_id}>
                    {x.source?.filename || x.filename || "上传文件"} · {x.source ? "解析完成" : x.status || "解析中"}
                    <button
                      type="button"
                      aria-label="移除文件"
                      onClick={() =>
                        setUploads((old) => old.filter((y) => y !== x))
                      }
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
              <footer>
                <small>AI 自动识别问题、URL 与参考文件；所有外部结论保留证据引用</small>
                <button
                  className="primary"
                  disabled={busy || uploads.some((item) => !item.source)}
                >
                  {busy ? "处理中…" : "开始研究 →"}
                </button>
              </footer>
            </form>
          </section>
        ) : (
          run && (
            <>
              <section className="run-heading">
                <div>
                  <span className="chip">
                    {templates[run.brief.template as keyof typeof templates]}
                  </span>
                  {run.mode === "fixture" && (
                    <span className="chip warning">
                      合成测试 · 不代表真实研究结论
                    </span>
                  )}
                  <h2>{run.brief.question}</h2>
                  <p>
                    {phaseNames[run.phase] || run.phase} · {run.status}{" "}
                    {run.stop_reason && `· ${run.stop_reason}`}
                  </p>
                </div>
                <div className="controls">
                  {!terminal.has(run.status) && (
                    <button onClick={() => control("cancel")}>取消运行</button>
                  )}
                  {run.status === "interrupted" && (
                    <button onClick={() => control("resume")}>恢复运行</button>
                  )}
                </div>
              </section>
              <section className="stats">
                <div>
                  <small>累计估算成本</small>
                  <strong>{dollars(run.spent_usd)}</strong>
                </div>
                <div>
                  <small>待结算预留</small>
                  <strong>{dollars(run.reserved_usd)}</strong>
                </div>
                <div>
                  <small>Token 累计</small>
                  <strong>{Number(run.tokens).toLocaleString()}</strong>
                </div>
                <div>
                  <small>模型 / 工具调用</small>
                  <strong>
                    {run.model_calls} / {run.tool_calls}
                  </strong>
                </div>
                <div>
                  <small>证据来源</small>
                  <strong>{run.sources.length}</strong>
                </div>
              </section>
              {run.intent && (
                <section className="intent-board panel">
                  <div>
                    <small>AI RESEARCH BRIEF · {run.intent.mode}</small>
                    <h3>{run.intent.normalized_question}</h3>
                  </div>
                  <div className="intent-stages">
                    {run.intent.stages.map((item: Dict) => (
                      <article key={item.stage}>
                        <span className={item.role === "supporting" ? "chip support" : "chip"}>
                          {stageNames[item.stage] || item.stage} · {item.role === "supporting" ? "内部支撑" : "最终交付"}
                        </span>
                        <strong>{item.deliverable}</strong>
                        <p>{item.objective}</p>
                        <small>{item.methods.join(" · ")}</small>
                        {item.depends_on?.length > 0 && (
                          <small className="dependency">
                            依赖：{item.depends_on.map((x: string) => stageNames[x] || x).join("、")}
                          </small>
                        )}
                      </article>
                    ))}
                  </div>
                  {run.intent.capability_gaps?.length > 0 && (
                    <p className="capability-gap">
                      当前能力边界：{run.intent.capability_gaps.join("；")}
                    </p>
                  )}
                </section>
              )}
              <section className="tasks">
                {run.tasks.map((t: Dict) => (
                  <article key={t.id}>
                    <small>
                      {stageNames[t.stage] || t.stage} · {t.output_role === "supporting" ? "内部支撑" : "最终交付"} · {t.method}
                    </small>
                    <h3>{t.objective}</h3>
                    <p>{t.deliverable}</p>
                    {t.stop_reason && <p>{t.stop_reason}</p>}
                  </article>
                ))}
              </section>
              <div
                className={
                  selected ? "content-grid with-inspector" : "content-grid"
                }
              >
                <section className="panel results">
                  <div className="tabs">
                    {[
                      ["report", "研究报告"],
                      ["sources", "来源与证据"],
                      ["trace", "执行记录"],
                    ].map(([id, name]) => (
                      <button
                        key={id}
                        className={tab === id ? "selected" : ""}
                        onClick={() => setTab(id)}
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                  {tab === "report" &&
                    (report ? (
                      <article className="report">
                        <div className="report-meta">
                          <span className="chip">
                            {report.quality_status} · revision {report.revision}
                          </span>
                          <a
                            href={`/api/backend/research-runs/${active}/report?format=markdown`}
                          >
                            导出 Markdown ↓
                          </a>
                        </div>
                        <h2>{report.report.title}</h2>
                        {report.report.introduction && (
                          <section className="structured-block">
                            <span className="chip">Introduction / 论证链</span>
                            {[
                              ["研究背景", "context"],
                              ["现有不足", "gap"],
                              ["要做什么", "objective"],
                              ["为什么这样做", "rationale"],
                              ["研究意义", "significance"],
                            ].map(([label, key]) => (
                              <div key={key}>
                                <h3>{label}</h3>
                                <p>{report.report.introduction[key]}</p>
                              </div>
                            ))}
                            {report.report.introduction.hypotheses?.length > 0 && (
                              <>
                                <h3>可检验假设</h3>
                                <ul>
                                  {report.report.introduction.hypotheses.map((x: string) => (
                                    <li key={x}>{x}</li>
                                  ))}
                                </ul>
                              </>
                            )}
                            {citations(report.report.introduction.claim_ids)}
                          </section>
                        )}
                        {report.report.nodes.map((n: Dict) => (
                          <section key={n.id}>
                            {n.stage && (
                              <div className="node-meta">
                                <span className="chip">{stageNames[n.stage] || n.stage}</span>
                                <small>
                                  {n.output_mode}
                                  {n.execution_status !== "not_applicable" && ` · ${n.execution_status}`}
                                </small>
                              </div>
                            )}
                            {n.title && <h3>{n.title}</h3>}
                            <p>{n.text}</p>
                            {n.rows?.length > 0 && (
                              <div className="table-scroll">
                                <table>
                                  <thead>
                                    <tr>
                                      {Object.keys(n.rows[0]).map((c) => (
                                        <th key={c}>{c}</th>
                                      ))}
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {n.rows.map((r: Dict, i: number) => (
                                      <tr key={i}>
                                        {Object.keys(n.rows[0]).map((c) => (
                                          <td key={c}>
                                            {r[c] || "unknown"}
                                            {citations(
                                              n.cell_claim_ids?.[`${i}.${c}`] ||
                                                [],
                                            )}
                                          </td>
                                        ))}
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                            {citations(n.claim_ids)}
                          </section>
                        ))}
                        {report.report.experiments?.length > 0 && (
                          <section className="structured-block">
                            <h2>Experiment / 实验工作包</h2>
                            {report.report.experiments.map((x: Dict) => (
                              <article className="idea" key={x.id}>
                                <span className="chip">{x.execution_status}</span>
                                <h3>{x.hypothesis}</h3>
                                <p><b>数据：</b>{x.dataset}</p>
                                <p><b>基线：</b>{x.baselines.join(" · ")}</p>
                                <p><b>协议：</b>{x.protocol}</p>
                                <p><b>指标：</b>{x.metrics.join(" · ")}</p>
                                <p><b>分析：</b>{x.analysis_plan}</p>
                                {x.risks?.length > 0 && <p><b>风险：</b>{x.risks.join("；")}</p>}
                                {x.artifact_ids?.length > 0 && <p><b>运行产物：</b>{x.artifact_ids.join(" · ")}</p>}
                                {citations(x.claim_ids)}
                              </article>
                            ))}
                          </section>
                        )}
                        {report.report.paper && (
                          <section className="paper">
                            <h2>读懂论文</h2>
                            <h3>核心要点</h3>
                            <ul>
                              {report.report.paper.takeaways.map(
                                (x: string, i: number) => (
                                  <li key={i}>{x}</li>
                                ),
                              )}
                            </ul>
                            <h3>问题与贡献</h3>
                            <p>{report.report.paper.problem}</p>
                            <ul>
                              {report.report.paper.contributions.map(
                                (x: string, i: number) => (
                                  <li key={i}>{x}</li>
                                ),
                              )}
                            </ul>
                            <h3>方法原理</h3>
                            <p>{report.report.paper.principles}</p>
                            <h3>实现流程</h3>
                            <p>{report.report.paper.implementation}</p>
                            <h3>实验设计</h3>
                            {report.report.paper.experiments.map(
                              (x: Dict, i: number) => (
                                <dl key={i}>
                                  {Object.entries(x).map(([k, v]) => (
                                    <div key={k}>
                                      <dt>{k}</dt>
                                      <dd>{String(v)}</dd>
                                    </div>
                                  ))}
                                </dl>
                              ),
                            )}
                            <h3>局限</h3>
                            {report.report.paper.limitations.map(
                              (x: Dict, i: number) => (
                                <p key={i}>
                                  <span className="chip">{x.attribution}</span>{" "}
                                  {x.text}
                                  {citations(x.claim_ids)}
                                </p>
                              ),
                            )}
                            <h3>后续研究方向</h3>
                            {report.report.paper.ideas.map(
                              (x: Dict, i: number) => (
                                <article className="idea" key={i}>
                                  <small>研究假设 · 新颖性尚未验证</small>
                                  <h4>{x.hypothesis}</h4>
                                  <p>{x.motivation}</p>
                                  <p>
                                    <b>建议实验：</b>
                                    {x.experiment}
                                  </p>
                                  <p>
                                    <b>对照：</b>
                                    {x.baseline} · <b>指标：</b>
                                    {x.metric}
                                  </p>
                                  <p>
                                    <b>预期信号：</b>
                                    {x.expected_signal}
                                  </p>
                                  <p>
                                    <b>风险：</b>
                                    {x.failure_risk}
                                  </p>
                                  {citations(x.claim_ids)}
                                </article>
                              ),
                            )}
                            {citations(report.report.paper.claim_ids)}
                          </section>
                        )}
                        {report.report.unresolved.length > 0 && (
                          <aside className="unresolved">
                            <h3>未解决与待核验</h3>
                            <ul>
                              {report.report.unresolved.map(
                                (x: string, i: number) => (
                                  <li key={i}>{x}</li>
                                ),
                              )}
                            </ul>
                          </aside>
                        )}
                      </article>
                    ) : (
                      <div className="empty">
                        <span>◎</span>
                        <h3>
                          {[
                            "completed",
                            "failed",
                            "cancelled",
                            "interrupted",
                          ].includes(run.status)
                            ? "本次运行暂无可用报告"
                            : "报告正在形成"}
                        </h3>
                        <p>
                          {[
                            "completed",
                            "failed",
                            "cancelled",
                            "interrupted",
                          ].includes(run.status)
                            ? "查看停止原因、来源与执行记录；中断运行可恢复，其余状态可新建研究。"
                            : "上方显示研究任务，来源和执行记录会持续更新。"}
                        </p>
                      </div>
                    ))}
                  {tab === "sources" && (
                    <div className="source-list">
                      {run.sources.map((s: Dict) => (
                        <button key={s.id} onClick={() => inspect(s.id)}>
                          <small>
                            {s.mime} · {s.retrieved_at.slice(0, 10)}
                          </small>
                          <h3>{s.title}</h3>
                          <p>{s.url || s.filename}</p>
                          <span>
                            解析：{parseLabels[s.parse_status] || "解析完成"} · 索引：{indexLabels[s.index?.status] || "解析中"}
                            {s.index?.total_chunks > 0 &&
                              ` · ${s.index.embedded_chunks}/${s.index.total_chunks}`}
                          </span>
                          <span>{s.warnings.join(" · ")}</span>
                        </button>
                      ))}
                      {!run.sources.length && (
                        <p className="empty">还没有来源</p>
                      )}
                    </div>
                  )}
                  {tab === "trace" && (
                    <div className="trace">
                      <h3>持久化事件</h3>
                      {events.map((e) => (
                        <div className="event" key={e.seq}>
                          <code>{e.seq}</code>
                          <span>{e.event_type}</span>
                          <time>
                            {new Date(e.occurred_at).toLocaleTimeString()}
                          </time>
                        </div>
                      ))}
                      <h3>调用与用量</h3>
                      {usage.map((u) => (
                        <div className="event" key={u.id}>
                          <span>
                            {u.kind} · {u.status}
                          </span>
                          <small>
                            {u.usage?.latency_ms || 0} ms ·{" "}
                            {dollars(u.usage?.usd)}
                          </small>
                          {u.error && <span className="error">{u.error}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </section>
                {selected && (
                  <aside className="panel inspector">
                    <button className="close" onClick={() => setSelected(null)}>
                      关闭 ×
                    </button>
                    <small>原文检查器</small>
                    <h3>{selected.source.title}</h3>
                    {selected.source.url && (
                      <a
                        href={selected.source.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        访问来源 ↗
                      </a>
                    )}
                    {selected.source.mime === "application/pdf" && (
                      <PdfPage
                        sourceId={selected.source.id}
                        page={selected.span?.page || 1}
                        bbox={selected.span?.bbox}
                      />
                    )}
                    {selected.span && (
                      <div className="locator">
                        第 {selected.span.page || "—"} 页 · 字符{" "}
                        {selected.span.start}–{selected.span.end} · {selected.span.element_kind || "paragraph"} · {selected.span.extraction_method || "native"}
                        {selected.span.confidence != null &&
                          ` · 置信度 ${(selected.span.confidence * 100).toFixed(0)}%`}
                        {selected.span.extraction_method === "vision" && " · 系统推断"}
                      </div>
                    )}
                    {selected.span?.crop_key && (
                      <img
                        className="evidence-crop"
                        src={`/api/backend/evidence-spans/${selected.span.id}/crop`}
                        alt="证据所在的 PDF 原始区域"
                      />
                    )}
                    <pre>
                      {selected.span ? (
                        <>
                          {selected.document.text.slice(
                            Math.max(0, selected.span.start - 600),
                            selected.span.start,
                          )}
                          <mark>{selected.span.quote}</mark>
                          {selected.document.text.slice(
                            selected.span.end,
                            selected.span.end + 800,
                          )}
                        </>
                      ) : (
                        selected.document.text.slice(0, 20000)
                      )}
                    </pre>
                    <small>
                      原文哈希：{selected.source.raw_hash.slice(0, 16)}…
                    </small>
                  </aside>
                )}
              </div>
            </>
          )
        )}
        <footer className="page-footer">
          Deep Research Agent · 原始来源、推断和研究假设分别呈现
        </footer>
      </main>
    </div>
  );
}
