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
const parseLabels: Dict = {
  ready: "解析完成",
  fallback: "降级",
  failed: "失败",
};
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
  const [runtime, setRuntime] = useState<Dict | null>(null);
  const [error, setError] = useState("");
  const [projects, setProjects] = useState<Dict[]>([]);
  const [deletedProjects, setDeletedProjects] = useState<Dict[]>([]);
  const [projectId, setProjectId] = useState("");
  const [conversations, setConversations] = useState<Dict[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [workspaceTab, setWorkspaceTab] = useState("research");
  const [showProjectForm, setShowProjectForm] = useState(false);
  const [projectDraft, setProjectDraft] = useState({
    name: "",
    objective: "",
    tags: "",
  });
  const [artifacts, setArtifacts] = useState<Dict[]>([]);
  const [memories, setMemories] = useState<Dict[]>([]);
  const [observations, setObservations] = useState<Dict[]>([]);
  const [memoryView, setMemoryView] = useState("knowledge");
  const [memoryState, setMemoryState] = useState<Dict | null>(null);
  const [subscriptions, setSubscriptions] = useState<Dict[]>([]);
  const [digests, setDigests] = useState<Dict[]>([]);
  const [digestDetail, setDigestDetail] = useState<Dict | null>(null);
  const [notifications, setNotifications] = useState<Dict[]>([]);
  const [messages, setMessages] = useState<Dict[]>([]);
  const [profile, setProfile] = useState<Dict | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Dict[]>([]);
  const [runs, setRuns] = useState<Dict[]>([]);
  const [active, setActive] = useState("");
  const [run, setRun] = useState<Dict | null>(null);
  const [report, setReport] = useState<Dict | null>(null);
  const [usage, setUsage] = useState<Dict[]>([]);
  const [usageSummary, setUsageSummary] = useState<Dict | null>(null);
  const [events, setEvents] = useState<Dict[]>([]);
  const [question, setQuestion] = useState("");
  const [researchMode, setResearchMode] = useState("research");
  const [uploads, setUploads] = useState<Dict[]>([]);
  const [tab, setTab] = useState("report");
  const [selected, setSelected] = useState<Dict | null>(null);
  const [busy, setBusy] = useState(false);
  const cursor = useRef(0);
  const currentRun = useRef<Dict | null>(null);
  const projectIdRef = useRef("");
  const loadRuns = useCallback(async () => {
    const suffix = projectIdRef.current
      ? `?project_id=${projectIdRef.current}`
      : "";
    const list = await api("/research-runs" + suffix);
    setRuns(list);
    setConnected(true);
  }, []);
  const loadProjectData = useCallback(async (id: string) => {
    const [
      nextConversations,
      nextArtifacts,
      nextMemories,
      nextObservations,
      nextSubscriptions,
      nextProfile,
    ] = await Promise.all([
      api(`/projects/${id}/conversations`),
      api(`/projects/${id}/artifacts`),
      api(`/projects/${id}/memories`),
      api(`/projects/${id}/observations`),
      api(`/projects/${id}/subscriptions`),
      api("/users/me/research-profile"),
    ]);
    setConversations(nextConversations);
    setConversationId((current) =>
      nextConversations.some((item: Dict) => item.id === current)
        ? current
        : nextConversations[0]?.id || "",
    );
    setArtifacts(nextArtifacts);
    setMemories(nextMemories);
    setObservations(nextObservations);
    setSubscriptions(nextSubscriptions);
    const nextDigests = (
      await Promise.all(
        nextSubscriptions.map((item: Dict) =>
          api(`/subscriptions/${item.id}/digests`),
        ),
      )
    ).flat();
    setDigests(nextDigests);
    setProfile(nextProfile);
    setNotifications(await api("/notifications"));
  }, []);
  const selectProject = useCallback(
    async (id: string) => {
      projectIdRef.current = id;
      setProjectId(id);
      setActive("");
      setRun(null);
      setReport(null);
      setSelected(null);
      setSearchResults([]);
      await Promise.all([loadRuns(), loadProjectData(id)]);
    },
    [loadProjectData, loadRuns],
  );
  const loadWorkspace = useCallback(async () => {
    const [list, deleted, runtimeInfo] = await Promise.all([
      api("/projects"),
      api("/projects?status=deleted_pending"),
      api("/health"),
    ]);
    setRuntime(runtimeInfo);
    setProjects(list);
    setDeletedProjects(deleted);
    const chosen =
      list.find((item: Dict) => item.id === projectIdRef.current) || list[0];
    if (chosen) await selectProject(chosen.id);
    else setConnected(true);
  }, [selectProject]);
  useEffect(() => {
    if (!connected || !conversationId) {
      setMessages([]);
      setMemoryState(null);
      return;
    }
    Promise.all([
      api(`/conversations/${conversationId}/messages`),
      api(`/conversations/${conversationId}/memory-state`),
    ])
      .then(([nextMessages, nextMemoryState]) => {
        setMessages(nextMessages);
        setMemoryState(nextMemoryState);
      })
      .catch((e) => setError(String(e)));
  }, [connected, conversationId, run]);
  useEffect(() => {
    loadWorkspace().catch(() => {});
  }, [loadWorkspace]);
  const refresh = useCallback(
    async (id: string) => {
      const next = await api(`/research-runs/${id}`);
      setRun(next);
      currentRun.current = next;
      const [pkg, calls, summary] = await Promise.all([
        loadReport(id),
        api(`/research-runs/${id}/usage`),
        api(`/research-runs/${id}/usage-summary`),
      ]);
      setReport(pkg);
      setUsage(calls);
      setUsageSummary(summary);
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
      await loadWorkspace();
    } catch (e) {
      setError(String(e));
    }
  }
  async function createProject(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await api("/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          name: projectDraft.name,
          objective: projectDraft.objective,
          tags: projectDraft.tags
            .split(/[,，]/)
            .map((item) => item.trim())
            .filter(Boolean),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        }),
      });
      setProjectDraft({ name: "", objective: "", tags: "" });
      setShowProjectForm(false);
      const list = await api("/projects");
      setProjects(list);
      await selectProject(created.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function createConversation() {
    if (!projectId) return;
    try {
      const created = await api(`/projects/${projectId}/conversations`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ title: `研究对话 ${conversations.length + 1}` }),
      });
      await loadProjectData(projectId);
      setConversationId(created.id);
    } catch (e) {
      setError(String(e));
    }
  }
  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api(`/conversations/${conversationId}/messages`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          content: question,
          mode: researchMode,
          attachment_ids: uploads.map(
            (x) => x.source?.id || x.source_id || x.upload_id,
          ),
        }),
      });
      setActive(result.run.id);
      setQuestion("");
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
      let item = await api("/uploads", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body,
      });
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
              entry.upload_id === item.upload_id
                ? { ...entry, ...item }
                : entry,
            ),
          );
          break;
        }
        if (state.status === "failed")
          throw Error(state.error || "文档解析失败");
        item = { ...item, ...state };
      }
      const source = item.source;
      if (source && projectIdRef.current) {
        await api(`/projects/${projectIdRef.current}/artifacts`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({
            source_version_id: source.id,
            title: source.title || file.name,
          }),
        });
        await loadProjectData(projectIdRef.current);
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
  async function updateMemory(id: string, status: string) {
    try {
      await api(`/projects/${projectId}/memories/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function updateObservation(id: string, status: string) {
    try {
      await api(`/projects/${projectId}/observations/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function compactConversation() {
    if (!conversationId) return;
    try {
      const job = await api(`/conversations/${conversationId}/compact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true }),
      });
      setMemoryState((current) => ({
        ...(current || {}),
        jobs: [job, ...(current?.jobs || [])],
      }));
    } catch (e) {
      setError(String(e));
    }
  }
  async function createSubscription(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const formElement = e.currentTarget;
    const form = new FormData(formElement);
    try {
      await api(`/projects/${projectId}/subscriptions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          topic: String(form.get("topic") || ""),
          query_terms: String(form.get("terms") || "")
            .split(/[,，]/)
            .map((item) => item.trim())
            .filter(Boolean),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        }),
      });
      formElement.reset();
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function previewSubscription(id: string) {
    try {
      const preview = await api(`/subscriptions/${id}/preview`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setDigestDetail(preview);
      setError(
        `试运行已从 ${preview.query_snapshot.connectors.join("、")} 获取 ${preview.candidates.length} 个去重候选，选中 ${preview.selected_count} 篇。`,
      );
    } catch (e) {
      setError(String(e));
    }
  }
  async function runSubscription(id: string) {
    try {
      const result = await api(`/subscriptions/${id}/run`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setActive(result.run_id);
      setWorkspaceTab("research");
      setError("周报研究任务已创建；通过质量门禁后会发布为站内周报。");
    } catch (e) {
      setError(String(e));
    }
  }
  async function searchProject(e: React.FormEvent) {
    e.preventDefault();
    try {
      const result = await api(`/projects/${projectId}/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: searchQuery }),
      });
      setSearchResults(result.results);
    } catch (e) {
      setError(String(e));
    }
  }
  async function toggleProfileLearning() {
    try {
      await api("/users/me/research-profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ learning_enabled: !profile?.learning_enabled }),
      });
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function updateProjectStatus(status: "active" | "archived") {
    try {
      await api(`/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      await loadWorkspace();
    } catch (e) {
      setError(String(e));
    }
  }
  async function editProject() {
    if (!currentProject) return;
    const name = prompt("项目名称", currentProject.name);
    if (!name) return;
    const objective = prompt("研究目标", currentProject.objective || "");
    if (objective === null) return;
    try {
      await api(`/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, objective }),
      });
      await loadWorkspace();
    } catch (e) {
      setError(String(e));
    }
  }
  async function deleteProject() {
    if (!confirm("项目将进入 30 天恢复期。确认删除？")) return;
    try {
      await fetch(`/api/backend/projects/${projectId}`, { method: "DELETE" });
      projectIdRef.current = "";
      await loadWorkspace();
    } catch (e) {
      setError(String(e));
    }
  }
  async function restoreProject(id: string) {
    try {
      await api(`/projects/${id}/restore`, { method: "POST" });
      await loadWorkspace();
    } catch (e) {
      setError(String(e));
    }
  }
  async function showDigest(id: string) {
    try {
      setDigestDetail(await api(`/digests/${id}`));
    } catch (e) {
      setError(String(e));
    }
  }
  async function updateConversation(status?: "active" | "archived") {
    const conversation = conversations.find((item) => item.id === conversationId);
    if (!conversation) return;
    const changes: Dict = status
      ? { status }
      : { title: prompt("对话名称", conversation.title) };
    if (!status && !changes.title) return;
    try {
      await api(`/conversations/${conversationId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function addUrlArtifact(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    try {
      await api(`/projects/${projectId}/artifacts/from-url`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ url: String(data.get("url") || "") }),
      });
      form.reset();
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function editArtifact(item: Dict) {
    const title = prompt("资产标题", item.metadata.title || item.source?.title || "");
    if (!title) return;
    const notes = prompt("资产备注", item.metadata.notes || "");
    if (notes === null) return;
    try {
      await api(`/projects/${projectId}/artifacts/${item.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, notes }),
      });
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function removeArtifact(id: string) {
    const response = await fetch(`/api/backend/projects/${projectId}/artifacts/${id}`, { method: "DELETE" });
    if (!response.ok) return setError(`${response.status}: ${await response.text()}`);
    await loadProjectData(projectId);
  }
  async function addProfileSignal(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const data = new FormData(form);
    try {
      await api("/users/me/research-profile/signals", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ field: data.get("field"), value: data.get("value"), source: "explicit" }),
      });
      form.reset();
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function updateProfileSignal(id: string, state: string) {
    try {
      if (state === "deleted") {
        await fetch(`/api/backend/users/me/research-profile/signals/${id}`, { method: "DELETE" });
      } else {
        await api(`/users/me/research-profile/signals/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state }),
        });
      }
      await loadProjectData(projectId);
    } catch (e) {
      setError(String(e));
    }
  }
  async function digestFeedback(digestId: string, canonicalId: string, feedback: string) {
    try {
      await api(`/digests/${digestId}/feedback`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ canonical_paper_id: canonicalId, feedback }),
      });
      setError("反馈已保存，只影响未来周报，不回写历史报告。");
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
  const currentProject = projects.find((item) => item.id === projectId);
  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">R</span>
          <div>
            Deep Research<small>研究工作台 · V1</small>
          </div>
        </div>
        <p className="side-label">PROJECTS</p>
        <button
          className="new-run"
          disabled={!connected}
          onClick={() => setShowProjectForm((value) => !value)}
        >
          ＋ 新建项目
        </button>
        {connected && showProjectForm && (
          <form className="side-project-form" onSubmit={createProject}>
            <input
              aria-label="项目名称"
              placeholder="项目名称"
              required
              maxLength={120}
              value={projectDraft.name}
              onChange={(e) =>
                setProjectDraft({ ...projectDraft, name: e.target.value })
              }
            />
            <textarea
              aria-label="项目目标"
              placeholder="研究目标"
              maxLength={4000}
              value={projectDraft.objective}
              onChange={(e) =>
                setProjectDraft({ ...projectDraft, objective: e.target.value })
              }
            />
            <input
              aria-label="项目标签"
              placeholder="标签，用逗号分隔"
              value={projectDraft.tags}
              onChange={(e) =>
                setProjectDraft({ ...projectDraft, tags: e.target.value })
              }
            />
            <button disabled={busy}>创建项目</button>
          </form>
        )}
        <nav className="project-nav">
          {projects.map((project) => (
            <button
              className={
                projectId === project.id
                  ? "project-link active"
                  : "project-link"
              }
              key={project.id}
              onClick={() => selectProject(project.id)}
            >
              <span>{project.name}</span>
              <small>
                {project.tags?.slice(0, 2).join(" · ") || "未设置标签"} ·{" "}
                {project.active_runs} 个任务
              </small>
            </button>
          ))}
          {deletedProjects.map((project) => (
            <div className="deleted-project" key={project.id}>
              <span>{project.name}</span>
              <button onClick={() => restoreProject(project.id)}>恢复</button>
            </div>
          ))}
        </nav>
        <div className="sidebar-section-heading">
          <span>RECENT RUNS</span>
          <button
            onClick={() => {
              setWorkspaceTab("research");
              setActive("");
              setRun(null);
              setReport(null);
              setSelected(null);
            }}
          >
            ＋
          </button>
        </div>
        <nav className="run-nav">
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
            {currentProject && (
              <small>
                {currentProject.name} ·{" "}
                {currentProject.objective || "持续研究工作区"}
              </small>
            )}
          </div>
          <span className="version">
            V0.4.0-rc.1 · P0/MVP
            {runtime && ` · ${runtime.research_mode === "live" ? "LIVE" : "FIXTURE"}`}
          </span>
        </header>
        {connected && runtime && (
          <div
            className={`mode-notice ${runtime.research_mode === "live" ? "live" : "fixture"}`}
            role="status"
          >
            <strong>
              {runtime.research_mode === "live" ? "真实研究模式" : "合成测试模式"}
            </strong>
            <span>
              {runtime.research_mode === "live"
                ? "新任务会调用真实模型与搜索服务，并产生实际用量。"
                : "不会调用真实模型或搜索；所有结果仅用于流程测试，不可作为研究结论。"}
            </span>
          </div>
        )}
        {error && (
          <div className="error" role="alert">
            {error}
            <button onClick={() => setError("")}>关闭</button>
          </div>
        )}
        {connected && currentProject && !active && (
          <section className="project-toolbar">
            <div>
              <span className="chip">{currentProject.status}</span>
              <strong>{currentProject.name}</strong>
              <small>{currentProject.tags?.join(" · ") || "未设置标签"}</small>
              {!currentProject.is_default && (
                <span className="row-actions project-actions">
                  <button onClick={editProject}>编辑</button>
                  <button
                    onClick={() =>
                      updateProjectStatus(
                        currentProject.status === "archived" ? "active" : "archived",
                      )
                    }
                  >
                    {currentProject.status === "archived" ? "取消归档" : "归档"}
                  </button>
                  <button className="danger" onClick={deleteProject}>删除</button>
                </span>
              )}
            </div>
            <div
              className="workspace-tabs"
              role="tablist"
              aria-label="项目功能"
            >
              {[
                ["research", "研究"],
                ["assets", `资产 ${artifacts.length}`],
                ["memory", `记忆 ${memories.length + observations.length}`],
                ["search", "搜索"],
                ["weekly", `周报 ${subscriptions.length}`],
                ["notifications", `通知 ${notifications.filter((item) => !item.read_at).length}`],
              ].map(([id, label]) => (
                <button
                  role="tab"
                  aria-selected={workspaceTab === id}
                  className={workspaceTab === id ? "selected" : ""}
                  key={id}
                  onClick={() => setWorkspaceTab(id)}
                >
                  {label}
                </button>
              ))}
            </div>
          </section>
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
          <>
            {workspaceTab === "research" && (
              <section className="new-research">
                <div className="intro">
                  <h2>把问题、链接和材料都交给研究 Agent。</h2>
                  <p>
                    本轮会自动使用当前项目已确认记忆、相关画像和资产，并把执行结果继续沉淀在项目中。
                  </p>
                  <div className="features">
                    <span>01 项目上下文</span>
                    <span>02 多 Agent 研究</span>
                    <span>03 证据与长期记忆</span>
                  </div>
                </div>
                <form className="panel research-form" onSubmit={create}>
                  <div className="conversation-picker">
                    <label>
                      当前对话
                      <select
                        value={conversationId}
                        onChange={(e) => setConversationId(e.target.value)}
                      >
                        {conversations.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.title} · {item.message_count} 条消息
                          </option>
                        ))}
                      </select>
                    </label>
                    <button type="button" onClick={createConversation}>
                      ＋ 新对话
                    </button>
                    <button type="button" onClick={() => updateConversation()}>
                      重命名
                    </button>
                    <button type="button" onClick={() => updateConversation("archived")}>
                      归档对话
                    </button>
                    <button type="button" onClick={compactConversation}>
                      压缩上下文
                    </button>
                  </div>
                  {memoryState && (
                    <small className="privacy-note">
                      Memory v{memoryState.memory_system_version} · revision {memoryState.memory_revision} ·
                      待压缩 {memoryState.uncompacted_message_count} 条闭合消息
                    </small>
                  )}
                  <fieldset className="mode-picker">
                    <legend>回答模式</legend>
                    <label>
                      <input
                        type="radio"
                        name="mode"
                        value="quick_answer"
                        checked={researchMode === "quick_answer"}
                        onChange={(e) => setResearchMode(e.target.value)}
                      />
                      快速回答
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="mode"
                        value="research"
                        checked={researchMode === "research"}
                        onChange={(e) => setResearchMode(e.target.value)}
                      />
                      深度研究
                    </label>
                  </fieldset>
                  {messages.length > 0 && (
                    <div className="conversation-history" aria-live="polite">
                      <strong>对话记录</strong>
                      {messages.slice(-8).map((item) => (
                        <p key={item.id} className={`message ${item.role}`}>
                          <small>{item.role === "user" ? "你" : "Agent"} · {item.status}</small>
                          {item.content}
                        </p>
                      ))}
                    </div>
                  )}
                  <label>
                    你想完成什么研究？
                    <textarea
                      required
                      minLength={researchMode === "research" ? 8 : 1}
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      rows={8}
                      placeholder={
                        "直接描述目标，也可以把链接贴在这里。例如：\n读这篇论文并找出可改进之处；先梳理相关工作，再提出三个可验证的切入点。\nhttps://…"
                      }
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
                    <small>
                      文件会先进入当前项目资产池，再冻结到本次 Run 来源快照
                    </small>
                    {uploads.map((x) => (
                      <span className="chip" key={x.upload_id}>
                        {x.source?.filename || x.filename || "上传文件"} ·{" "}
                        {x.source ? "解析完成" : x.status || "解析中"}
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
                    <small>
                      当前项目边界由数据库 RLS 强制执行；外部结论保留证据引用
                    </small>
                    <button
                      className="primary"
                      disabled={
                        busy ||
                        !conversationId ||
                        uploads.some((item) => !item.source)
                      }
                    >
                      {busy
                        ? "处理中…"
                        : researchMode === "research"
                          ? "开始研究 →"
                          : "快速回答 →"}
                    </button>
                  </footer>
                </form>
              </section>
            )}
            {workspaceTab === "assets" && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div>
                    <small>PROJECT ASSETS</small>
                    <h2>项目资产池</h2>
                  </div>
                  <label className="upload-button button-like">
                    ＋ 上传文件
                    <input
                      type="file"
                      accept=".pdf,.md,.html,.txt"
                      hidden
                      onChange={(e) =>
                        e.target.files?.[0] && upload(e.target.files[0])
                      }
                    />
                  </label>
                </div>
                <div className="record-list">
                  <form className="search-form url-form" onSubmit={addUrlArtifact}>
                    <input
                      name="url"
                      type="url"
                      aria-label="资产网址"
                      required
                      placeholder="https://…"
                    />
                    <button>添加网址</button>
                  </form>
                  {artifacts.map((item) => (
                    <article key={item.id}>
                      <div>
                        <span className="chip">{item.kind}</span>
                        <h3>{item.metadata.title || item.source?.title}</h3>
                      </div>
                      <small>
                        {item.source?.mime} ·{" "}
                        {indexLabels[item.index_status] ||
                          item.index_status ||
                          "等待索引"}
                      </small>
                      <p>
                        {item.metadata.notes ||
                          item.source?.filename ||
                          item.source_version_id}
                      </p>
                      <div className="row-actions">
                        <button onClick={() => editArtifact(item)}>编辑元数据</button>
                        <button className="danger" onClick={() => removeArtifact(item.id)}>移除</button>
                      </div>
                    </article>
                  ))}
                  {!artifacts.length && <p className="empty">还没有项目资产</p>}
                </div>
              </section>
            )}
            {workspaceTab === "memory" && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div>
                    <small>EVOMEMORY V2</small>
                    <h2>受治理的混合记忆</h2>
                  </div>
                </div>
                <div className="workspace-tabs memory-tabs" role="tablist" aria-label="记忆视图">
                  {[
                    ["knowledge", `项目知识 ${memories.filter((item) => item.status !== "candidate").length}`],
                    ["profile", `Profile ${profile?.signals?.length || 0}`],
                    ["observations", `Observations ${observations.filter((item) => item.status !== "candidate").length}`],
                    ["pending", `待确认 ${memories.filter((item) => item.status === "candidate").length + observations.filter((item) => item.status === "candidate").length + (profile?.signals?.filter((item: Dict) => item.state === "candidate").length || 0)}`],
                  ].map(([id, label]) => (
                    <button
                      role="tab"
                      aria-selected={memoryView === id}
                      className={memoryView === id ? "selected" : ""}
                      key={id}
                      onClick={() => setMemoryView(id)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {memoryView === "knowledge" && <div className="record-list memory-list">
                  {memories.filter((item) => item.status !== "candidate").map((item) => (
                    <article key={item.id}>
                      <div>
                        <span className="chip">{item.type} · {item.status}{item.legacy ? " · legacy" : ""}</span>
                        <h3>{item.content}</h3>
                      </div>
                      <small>
                        置信度 {(item.confidence * 100).toFixed(0)}% ·{" "}
                        {item.evidence?.length || 0} 个来源指针
                      </small>
                    </article>
                  ))}
                  {!memories.some((item) => item.status !== "candidate") && <p className="empty">还没有已治理的项目知识</p>}
                </div>}
                {memoryView === "observations" && <div className="record-list memory-list">
                  {observations.filter((item) => item.status !== "candidate").map((item) => (
                    <article key={item.id}>
                      <span className="chip">{item.memory_type} · {item.scope} · {item.status}</span>
                      <h3>{item.summary}</h3>
                      <p>{item.body}</p>
                      {item.why_it_matters && <small>价值：{item.why_it_matters}</small>}
                      <small>{item.evidence?.length || 0} 个证据指针 · {item.relations?.length || 0} 条关系</small>
                    </article>
                  ))}
                  {!observations.some((item) => item.status !== "candidate") && <p className="empty">还没有已确认的 Observation</p>}
                </div>}
                {memoryView === "profile" && profile && <>
                  <div className="panel-heading memory-profile-heading">
                    <p className="privacy-note">长期学习关闭时仍会压缩当前对话，但不会生成 Profile 或 Observation 候选。</p>
                    <button onClick={toggleProfileLearning}>
                      {profile.learning_enabled ? "暂停长期学习" : "开启长期学习"}
                    </button>
                  </div>
                  <form className="subscription-form" onSubmit={addProfileSignal}>
                    <label>偏好字段<input name="field" required placeholder="例如 response_format" /></label>
                    <label>显式偏好<input name="value" required placeholder="例如先给结论，再给证据" /></label>
                    <button>添加显式 Profile</button>
                  </form>
                  <div className="record-list">
                    {profile.signals.filter((item: Dict) => item.state !== "candidate").map((item: Dict) => (
                      <article key={item.id}>
                        <span className="chip">{item.category || "user_profile"} · {item.source} · {item.state}</span>
                        <h3>{item.field}</h3><p>{item.value}</p>
                        <div className="row-actions">
                          <button onClick={() => updateProfileSignal(item.id, item.state === "frozen" ? "active" : "frozen")}>{item.state === "frozen" ? "解冻" : "冻结"}</button>
                          <button className="danger" onClick={() => updateProfileSignal(item.id, "deleted")}>否定并删除</button>
                        </div>
                      </article>
                    ))}
                  </div>
                </>}
                {memoryView === "pending" && <div className="record-list memory-list">
                  {memories.filter((item) => item.status === "candidate").map((item) => (
                    <article key={`memory-${item.id}`}>
                      <span className="chip warning">项目知识 · {item.type}</span>
                      <h3>{item.content}</h3>
                      <small>{item.evidence?.length || 0} 个来源指针</small>
                      <div className="row-actions"><button onClick={() => updateMemory(item.id, "confirmed")}>确认</button><button onClick={() => updateMemory(item.id, "rejected")}>拒绝</button></div>
                    </article>
                  ))}
                  {observations.filter((item) => item.status === "candidate").map((item) => (
                    <article key={`observation-${item.id}`}>
                      <span className="chip warning">Observation · {item.memory_type}</span>
                      <h3>{item.summary}</h3><p>{item.body}</p>
                      <small>{item.evidence?.length || 0} 个证据指针</small>
                      <div className="row-actions"><button onClick={() => updateObservation(item.id, "confirmed")}>确认</button><button onClick={() => updateObservation(item.id, "rejected")}>拒绝</button></div>
                    </article>
                  ))}
                  {profile?.signals?.filter((item: Dict) => item.state === "candidate").map((item: Dict) => (
                    <article key={`profile-${item.id}`}>
                      <span className="chip warning">Profile · {item.category}</span>
                      <h3>{item.field}</h3><p>{item.value}</p>
                      <div className="row-actions"><button onClick={() => updateProfileSignal(item.id, "active")}>确认</button><button onClick={() => updateProfileSignal(item.id, "deleted")}>拒绝</button></div>
                    </article>
                  ))}
                </div>}
              </section>
            )}
            {workspaceTab === "search" && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div>
                    <small>UNIFIED SEARCH</small>
                    <h2>搜索文件 对话 记忆与报告</h2>
                  </div>
                </div>
                <form className="search-form" onSubmit={searchProject}>
                  <input
                    aria-label="项目搜索"
                    required
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="输入关键词或研究问题"
                  />
                  <button className="primary">搜索</button>
                </form>
                <div className="record-list">
                  {searchResults.map((item) => (
                    <article
                      key={`${item.type}-${item.id}`}
                      className="search-result"
                      tabIndex={0}
                      onClick={() => {
                        if (item.source_id) inspect(item.source_id);
                        else if (item.run_id) setActive(item.run_id);
                        else if (item.conversation_id) {
                          setConversationId(item.conversation_id);
                          setWorkspaceTab("research");
                        }
                      }}
                    >
                      <span className="chip">{item.type}</span>
                      <h3>{item.title}</h3>
                      <p>{item.snippet}</p>
                      <small>{item.match_reason}</small>
                    </article>
                  ))}
                  {searchQuery && !searchResults.length && (
                    <p className="empty">尚未找到匹配结果</p>
                  )}
                </div>
              </section>
            )}
            {workspaceTab === "weekly" && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div>
                    <small>WEEKLY DIGEST</small>
                    <h2>论文周报订阅</h2>
                  </div>
                </div>
                <form
                  className="subscription-form"
                  onSubmit={createSubscription}
                >
                  <label>
                    订阅主题
                    <input
                      name="topic"
                      required
                      placeholder="例如 3DGS compression"
                    />
                  </label>
                  <label>
                    查询词
                    <input name="terms" placeholder="用逗号分隔，可留空" />
                  </label>
                  <button className="primary">创建每周订阅</button>
                </form>
                <div className="record-list">
                  {subscriptions.map((item) => (
                    <article key={item.id}>
                      <div>
                        <span className="chip">{item.status}</span>
                        <h3>{item.name}</h3>
                      </div>
                      <p>{item.query_config.topic}</p>
                      <small>
                        每周 {item.schedule.delivery_time} ·{" "}
                        {item.schedule.timezone} · 默认{" "}
                        {item.query_config.paper_count} 篇
                      </small>
                      <div className="row-actions">
                        <button onClick={() => previewSubscription(item.id)}>
                          试运行查询
                        </button>
                        <button
                          className="primary"
                          onClick={() => runSubscription(item.id)}
                        >
                          立即生成周报
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
                {digests.length > 0 && (
                  <>
                    <h3 className="section-title">站内周报记录</h3>
                    <div className="record-list digest-list">
                      {digests.map((item) => (
                        <article key={item.id}>
                          <div>
                            <span
                              className={`chip ${item.status === "needs_review" ? "warning" : ""}`}
                            >
                              {item.status}
                            </span>
                            <h3>
                              {item.period_start
                                ? `${item.period_start} — ${item.period_end}`
                                : "查询试运行"}
                            </h3>
                          </div>
                          <small>
                            {item.quality?.state || "scheduled"} · revision{" "}
                            {item.revision}
                          </small>
                          <div className="row-actions">
                            <button onClick={() => showDigest(item.id)}>查看候选</button>
                            {item.run_id && (
                              <button onClick={() => setActive(item.run_id)}>查看周报报告</button>
                            )}
                          </div>
                        </article>
                      ))}
                    </div>
                  </>
                )}
                {digestDetail && (
                  <div className="digest-candidates" aria-live="polite">
                    <div className="panel-heading">
                      <div>
                        <small>CANDIDATE EVIDENCE</small>
                        <h3>候选与连接器披露</h3>
                      </div>
                      <button onClick={() => setDigestDetail(null)}>关闭</button>
                    </div>
                    <p>
                      {digestDetail.connector_attempts
                        ?.map((item: Dict) => `${item.connector}: ${item.status} (${item.result_count})`)
                        .join(" · ")}
                    </p>
                    {digestDetail.candidates?.map((candidate: Dict) => (
                      <article key={candidate.id}>
                        <span className="chip">{candidate.evidence_scope}</span>
                        <h3>{candidate.metadata.title}</h3>
                        <p>{candidate.selection_rationale}</p>
                        <small>
                          {candidate.metadata.connectors?.join(" · ")} · score {candidate.scores.total}
                        </small>
                        <div className="row-actions">
                          <button onClick={() => digestFeedback(digestDetail.id, candidate.canonical_id, "useful")}>有用</button>
                          <button onClick={() => digestFeedback(digestDetail.id, candidate.canonical_id, "irrelevant")}>不相关</button>
                          <button onClick={() => digestFeedback(digestDetail.id, candidate.canonical_id, "never_recommend")}>不再推荐</button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}
            {workspaceTab === "notifications" && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div><small>IN-APP NOTIFICATIONS</small><h2>通知中心</h2></div>
                </div>
                <div className="record-list">
                  {notifications.map((item) => (
                    <article key={item.id}>
                      <span className={`chip ${item.read_at ? "" : "warning"}`}>
                        {item.read_at ? "已读" : "未读"}
                      </span>
                      <h3>{item.subscription_name}</h3>
                      <p>周报状态：{item.digest_status}</p>
                      {!item.read_at && (
                        <button onClick={async () => {
                          await api(`/notifications/${item.id}/read`, { method: "POST" });
                          await loadProjectData(projectId);
                        }}>标为已读</button>
                      )}
                    </article>
                  ))}
                  {!notifications.length && <p className="empty">暂无通知</p>}
                </div>
              </section>
            )}
            {workspaceTab === "profile" && profile && (
              <section className="project-panel panel">
                <div className="panel-heading">
                  <div>
                    <small>RESEARCH PROFILE</small>
                    <h2>研究画像与控制</h2>
                  </div>
                  <button onClick={toggleProfileLearning}>
                    {profile.learning_enabled ? "暂停画像学习" : "开启画像学习"}
                  </button>
                </div>
                <p className="privacy-note">
                  项目标签默认不会进入用户画像。只有显式标签或确认后的建议会影响跨项目检索与周报。
                </p>
                <form className="subscription-form" onSubmit={addProfileSignal}>
                  <label>
                    偏好字段
                    <input name="field" required placeholder="例如 method" />
                  </label>
                  <label>
                    显式偏好
                    <input name="value" required placeholder="例如重视工程实现" />
                  </label>
                  <button>添加显式标签</button>
                </form>
                <p>
                  <a href="/api/backend/users/me/research-profile/export" download>
                    导出画像 JSON ↓
                  </a>
                </p>
                <div className="record-list">
                  {profile.signals.map((item: Dict) => (
                    <article key={item.id}>
                      <span className="chip">
                        {item.source} · {item.state}
                      </span>
                      <h3>{item.field}</h3>
                      <p>{item.value}</p>
                      <small>
                        置信度 {(item.confidence * 100).toFixed(0)}% ·{" "}
                        {item.scope} scope
                      </small>
                      <div className="row-actions">
                        <button onClick={() => updateProfileSignal(item.id, item.state === "frozen" ? "active" : "frozen")}>
                          {item.state === "frozen" ? "解冻" : "冻结"}
                        </button>
                        <button className="danger" onClick={() => updateProfileSignal(item.id, "deleted")}>否定并删除</button>
                      </div>
                    </article>
                  ))}
                  {!profile.signals.length && (
                    <p className="empty">还没有画像标签</p>
                  )}
                </div>
              </section>
            )}
          </>
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
                        <span
                          className={
                            item.role === "supporting" ? "chip support" : "chip"
                          }
                        >
                          {stageNames[item.stage] || item.stage} ·{" "}
                          {item.role === "supporting" ? "内部支撑" : "最终交付"}
                        </span>
                        <strong>{item.deliverable}</strong>
                        <p>{item.objective}</p>
                        <small>{item.methods.join(" · ")}</small>
                        {item.depends_on?.length > 0 && (
                          <small className="dependency">
                            依赖：
                            {item.depends_on
                              .map((x: string) => stageNames[x] || x)
                              .join("、")}
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
                      {stageNames[t.stage] || t.stage} ·{" "}
                      {t.output_role === "supporting" ? "内部支撑" : "最终交付"}{" "}
                      · {t.method}
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
                            {report.report.introduction.hypotheses?.length >
                              0 && (
                              <>
                                <h3>可检验假设</h3>
                                <ul>
                                  {report.report.introduction.hypotheses.map(
                                    (x: string) => (
                                      <li key={x}>{x}</li>
                                    ),
                                  )}
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
                                <span className="chip">
                                  {stageNames[n.stage] || n.stage}
                                </span>
                                <small>
                                  {n.output_mode}
                                  {n.execution_status !== "not_applicable" &&
                                    ` · ${n.execution_status}`}
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
                                <span className="chip">
                                  {x.execution_status}
                                </span>
                                <h3>{x.hypothesis}</h3>
                                <p>
                                  <b>数据：</b>
                                  {x.dataset}
                                </p>
                                <p>
                                  <b>基线：</b>
                                  {x.baselines.join(" · ")}
                                </p>
                                <p>
                                  <b>协议：</b>
                                  {x.protocol}
                                </p>
                                <p>
                                  <b>指标：</b>
                                  {x.metrics.join(" · ")}
                                </p>
                                <p>
                                  <b>分析：</b>
                                  {x.analysis_plan}
                                </p>
                                {x.risks?.length > 0 && (
                                  <p>
                                    <b>风险：</b>
                                    {x.risks.join("；")}
                                  </p>
                                )}
                                {x.artifact_ids?.length > 0 && (
                                  <p>
                                    <b>运行产物：</b>
                                    {x.artifact_ids.join(" · ")}
                                  </p>
                                )}
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
                            解析：{parseLabels[s.parse_status] || "解析完成"} ·
                            索引：{indexLabels[s.index?.status] || "解析中"}
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
                      {usageSummary && (
                        <div className="usage-summary">
                          <strong>
                            {Number(
                              usageSummary.totals.tokens,
                            ).toLocaleString()}{" "}
                            /{" "}
                            {Number(usageSummary.soft_target).toLocaleString()}{" "}
                            soft
                          </strong>
                          <progress
                            max={usageSummary.hard_cap}
                            value={Math.min(
                              usageSummary.totals.tokens,
                              usageSummary.hard_cap,
                            )}
                          />
                          <small>
                            soft 剩余{" "}
                            {Number(
                              usageSummary.soft_remaining,
                            ).toLocaleString()}{" "}
                            · hard 剩余{" "}
                            {Number(
                              usageSummary.hard_remaining,
                            ).toLocaleString()}{" "}
                            · 缓存命中{" "}
                            {Number(
                              Object.values(usageSummary.budget_groups).reduce(
                                (sum: number, group: any) =>
                                  sum + group.cache_hit_tokens,
                                0,
                              ),
                            ).toLocaleString()}
                          </small>
                          {Object.entries(usageSummary.budget_groups).map(
                            ([name, group]: [string, any]) => (
                              <small key={name}>
                                {name}:{" "}
                                {Number(
                                  group.input_tokens + group.output_tokens,
                                ).toLocaleString()}{" "}
                                / {Number(group.target).toLocaleString()} token
                                · {group.model_calls} 次模型 ·{" "}
                                {dollars(group.usd)}
                              </small>
                            ),
                          )}
                          {usageSummary.degradation_events.map(
                            (event: Dict) => (
                              <span className="error" key={event.seq}>
                                降级 {event.payload.level}%：
                                {event.payload.reason}
                              </span>
                            ),
                          )}
                        </div>
                      )}
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
                        {selected.span.start}–{selected.span.end} ·{" "}
                        {selected.span.element_kind || "paragraph"} ·{" "}
                        {selected.span.extraction_method || "native"}
                        {selected.span.confidence != null &&
                          ` · 置信度 ${(selected.span.confidence * 100).toFixed(0)}%`}
                        {selected.span.extraction_method === "vision" &&
                          " · 系统推断"}
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
