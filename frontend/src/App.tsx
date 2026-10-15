import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Code2,
  FileCheck2,
  Fingerprint,
  FlaskConical,
  Heart,
  KeyRound,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MessageSquareText,
  Play,
  RotateCcw,
  ShieldCheck,
  ShieldOff,
  Sparkles,
  Tag,
  TrendingUp,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  type Evaluation,
  type EvaluationInput,
  type Mode,
  type Sample,
  type Scenario,
} from "./api";

const icons: Record<string, typeof ShieldCheck> = {
  fingerprint: Fingerprint,
  shield: ShieldCheck,
  lock: LockKeyhole,
  chart: TrendingUp,
  document: FileCheck2,
  message: MessageSquareText,
  tag: Tag,
  heart: Heart,
};
const modeLabels: Record<Mode, string> = {
  compact: "综合判断",
  detailed: "独立检查",
};
type Run = {
  scenario: string;
  sample?: Sample;
  input: EvaluationInput;
  result: Evaluation;
};
type SuiteRow = {
  scenario: string;
  sample: Sample;
  result?: Evaluation;
  error?: string;
  running?: boolean;
};

function saveFile(name: string, data: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}
function percentile(values: number[], p: number) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const n = (sorted.length - 1) * p,
    lo = Math.floor(n),
    hi = Math.ceil(n);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (n - lo);
}
const ms = (n: number | null | undefined) =>
  n == null ? "—" : `${Math.round(n).toLocaleString()} ms`;

export default function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<Scenario>();
  const [sample, setSample] = useState<Sample>();
  const [content, setContent] = useState("");
  const [source, setSource] = useState<"INPUT" | "OUTPUT">("INPUT");
  const [reference, setReference] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode | "compare">("detailed");
  const [threshold, setThreshold] = useState(0.5);
  const [tab, setTab] = useState<"lab" | "suite">("lab");
  const [username, setUsername] = useState("");
  const [checkingSession, setCheckingSession] = useState(true);
  const [draftUsername, setDraftUsername] = useState("");
  const [draftPassword, setDraftPassword] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const authVersion = useRef(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [focusedMode, setFocusedMode] = useState<Mode>("detailed");
  const [history, setHistory] = useState<Run[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [suite, setSuite] = useState<SuiteRow[]>([]);
  const [suiteMode, setSuiteMode] = useState<Mode>("detailed");
  const [suiteConfig, setSuiteConfig] = useState<{
    mode: Mode;
    threshold: number;
    startedAt: string;
  }>();
  const [suiteRunning, setSuiteRunning] = useState(false);
  const stopSuite = useRef(false);

  function chooseSample(next: Sample) {
    setSample(next);
    setContent(next.content);
    setSource(next.source);
    setReference(next.reference || "");
    setQuery(next.query || "");
    setRuns([]);
    setError("");
  }
  function chooseScenario(next: Scenario) {
    setSelected(next);
    chooseSample(next.cases[0]);
  }
  async function loadCatalog() {
    const version = authVersion.current;
    setLoadError("");
    try {
      const data = await api<{ scenarios: Scenario[] }>("scenarios");
      if (authVersion.current !== version) return;
      setScenarios(data.scenarios);
      chooseScenario(data.scenarios[0]);
    } catch (err) {
      if (authVersion.current === version) setLoadError(handleError(err));
    }
  }
  useEffect(() => {
    let active = true;
    sessionStorage.removeItem("demo-token");
    const version = authVersion.current;
    void api<{ username: string }>("session")
      .then((session) => {
        if (active && authVersion.current === version)
          setUsername(session.username);
      })
      .catch(() => {
        if (active && authVersion.current === version) setUsername("");
      })
      .finally(() => {
        if (active) setCheckingSession(false);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (username) void loadCatalog();
  }, [username]);

  function clearWorkspace() {
    stopSuite.current = true;
    setScenarios([]);
    setSelected(undefined);
    setSample(undefined);
    setContent("");
    setReference("");
    setQuery("");
    setSource("INPUT");
    setTab("lab");
    setHistory([]);
    setHistoryOpen(false);
    setRuns([]);
    setSuite([]);
    setSuiteConfig(undefined);
    setError("");
    setLoadError("");
    setLoginOpen(false);
  }
  function handleError(err: unknown) {
    if (err instanceof ApiError && err.status === 401) {
      authVersion.current++;
      setUsername("");
      clearWorkspace();
    }
    return err instanceof Error ? err.message : "请求未完成，请重试。";
  }
  async function connect(event: React.FormEvent) {
    event.preventDefault();
    authVersion.current++;
    setLoginBusy(true);
    setLoginError("");
    try {
      const session = await api<{ username: string }>("login", {
        username: draftUsername.trim(),
        password: draftPassword,
      });
      setUsername(session.username);
      setDraftPassword("");
      setLoginOpen(false);
    } catch (err) {
      setLoginError(handleError(err));
    } finally {
      setDraftPassword("");
      setLoginBusy(false);
    }
  }
  async function logout() {
    authVersion.current++;
    setLoginBusy(true);
    setLoginError("");
    try {
      await api("logout", {});
      setUsername("");
      clearWorkspace();
    } catch (err) {
      setLoginError(handleError(err));
    } finally {
      setLoginBusy(false);
    }
  }
  async function evaluate() {
    if (!username) {
      setLoginOpen(true);
      return;
    }
    setBusy(true);
    setError("");
    setRuns([]);
    const modes: Mode[] = mode === "compare" ? ["compact", "detailed"] : [mode];
    try {
      for (const current of modes) {
        const input: EvaluationInput = {
          content,
          source,
          mode: current,
          threshold,
          ...(source === "OUTPUT" && reference.trim() ? { reference } : {}),
          ...(source === "OUTPUT" && query.trim() ? { query } : {}),
        };
        const result = await api<Evaluation>("evaluate", input);
        const run = {
          scenario: selected?.title || "自定义内容",
          sample,
          input,
          result,
        };
        setRuns((prev) => [...prev, run]);
        setFocusedMode(current);
        setHistory((prev) => [run, ...prev].slice(0, 20));
      }
    } catch (err) {
      setError(handleError(err));
    } finally {
      setBusy(false);
    }
  }
  async function runSuite() {
    if (!username) {
      setLoginOpen(true);
      return;
    }
    const jobs = scenarios.flatMap((s) =>
      s.cases.map((c) => ({ scenario: s.title, sample: c })),
    );
    setSuiteConfig({
      mode: suiteMode,
      threshold,
      startedAt: new Date().toISOString(),
    });
    setSuite(jobs);
    setSuiteRunning(true);
    stopSuite.current = false;
    for (let i = 0; i < jobs.length && !stopSuite.current; i++) {
      const { sample: test } = jobs[i];
      setSuite((prev) =>
        prev.map((r, index) => (index === i ? { ...r, running: true } : r)),
      );
      try {
        const result = await api<Evaluation>("evaluate", {
          content: test.content,
          source: test.source,
          mode: suiteMode,
          threshold,
          ...(test.reference ? { reference: test.reference } : {}),
          ...(test.query ? { query: test.query } : {}),
        });
        setSuite((prev) =>
          prev.map((r, index) =>
            index === i ? { ...r, result, running: false } : r,
          ),
        );
      } catch (err) {
        const message = handleError(err);
        setSuite((prev) =>
          prev.map((r, index) =>
            index === i ? { ...r, error: message, running: false } : r,
          ),
        );
        // Authentication/capacity failures should not fan out into more failing requests.
        if (err instanceof ApiError && [401, 429].includes(err.status)) break;
      }
    }
    setSuiteRunning(false);
  }
  const locked = busy || suiteRunning;
  const resultRun = runs.find((r) => r.result.mode === focusedMode) || runs[0];
  const result = resultRun?.result;
  const completed = suite.filter((r) => r.result || r.error);
  const valid = suite.filter((r) => r.result);
  const correct = valid.filter((r) => r.result?.decision === r.sample.expected);
  const fn = valid.filter(
    (r) => r.sample.expected === "block" && r.result?.decision === "allow",
  ).length;
  const fp = valid.filter(
    (r) => r.sample.expected === "allow" && r.result?.decision === "block",
  ).length;
  const sampleCount = scenarios.reduce((sum, s) => sum + s.cases.length, 0);
  const SceneIcon = icons[selected?.icon || "shield"] || ShieldCheck;

  if (checkingSession || !username) {
    return (
      <main className="login-page">
        <div className="login-brand">
          <span className="brand-mark">
            <Layers3 size={24} />
          </span>
          <strong>
            decider<span>GUARDRAIL LAB</span>
          </strong>
        </div>
        <section
          className="modal login-card"
          aria-labelledby="login-page-title"
        >
          <div className="modal-icon">
            <LockKeyhole size={25} />
          </div>
          <h1 id="login-page-title">
            {checkingSession ? "正在确认登录状态" : "登录演示"}
          </h1>
          {checkingSession ? (
            <p className="session-loading" role="status">
              <LoaderCircle size={18} className="spin" /> 请稍候…
            </p>
          ) : (
            <>
              <p>使用用户名和密码登录，进入护栏场景实验室。</p>
              <form onSubmit={connect}>
                <label htmlFor="login-username">用户名</label>
                <input
                  id="login-username"
                  autoFocus
                  autoComplete="username"
                  type="text"
                  value={draftUsername}
                  maxLength={64}
                  required
                  onChange={(e) => setDraftUsername(e.target.value)}
                  placeholder="请输入用户名"
                  disabled={loginBusy}
                />
                <label htmlFor="login-password">密码</label>
                <input
                  id="login-password"
                  autoComplete="current-password"
                  type="password"
                  value={draftPassword}
                  maxLength={256}
                  required
                  onChange={(e) => setDraftPassword(e.target.value)}
                  placeholder="请输入密码"
                  disabled={loginBusy}
                />
                {loginError && (
                  <div className="form-error" role="alert">
                    {loginError}
                  </div>
                )}
                <button
                  className="primary-button full-width"
                  disabled={
                    loginBusy || !draftUsername.trim() || !draftPassword
                  }
                >
                  {loginBusy ? (
                    <LoaderCircle size={16} className="spin" />
                  ) : (
                    <LockKeyhole size={16} />
                  )}
                  登录
                </button>
              </form>
            </>
          )}
        </section>
        <p className="login-footer">Strands Decider · Guardrail Lab</p>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/">
          <span className="brand-mark">
            <Layers3 size={22} />
          </span>
          <div>
            decider<span>GUARDRAIL LAB</span>
          </div>
        </a>
        <div className="workspace-label">
          WORKSPACE <span>演示环境</span>
        </div>
        <nav aria-label="主导航">
          <button
            className={tab === "lab" ? "nav-item active" : "nav-item"}
            onClick={() => setTab("lab")}
          >
            <FlaskConical size={18} />
            场景实验室
            <ArrowRight size={14} />
          </button>
          <button
            className={tab === "suite" ? "nav-item active" : "nav-item"}
            onClick={() => setTab("suite")}
          >
            <FileCheck2 size={18} />
            场景回归<span className="nav-count">{sampleCount || "—"}</span>
          </button>
          <button className="nav-item" onClick={() => setHistoryOpen(true)}>
            <Clock3 size={18} />
            本次记录<span className="nav-count">{history.length}</span>
          </button>
        </nav>
        <div className="sidebar-note">
          <ShieldCheck size={22} />
          <strong>看见护栏的边界</strong>
          <p>
            同一条内容，不同检查方式。
            <br />
            用真实结果观察每一次判定。
          </p>
          <div className="tiny-line" />
          <span>STRANDS DECIDER · LIVE INFERENCE</span>
        </div>
        <div className="sidebar-bottom">
          <span className="live-dot" />
          {scenarios.length ? "API 已连接" : "连接服务中"}
          <span>v1.0</span>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>
            工作空间 <span className="slash">/</span>{" "}
            {tab === "lab" ? "场景实验室" : "场景回归"}
          </span>
          <button
            className={"access-button " + (username ? "connected" : "")}
            disabled={locked}
            onClick={() => {
              setLoginError("");
              setLoginOpen(true);
            }}
          >
            <KeyRound size={15} />
            {username ? `已登录 · ${username}` : "登录"}
          </button>
        </header>
        <div className="page">
          <div className="page-title">
            <div>
              <div className="eyebrow">
                <span />
                GUARDRAIL PLAYGROUND
              </div>
              <h1>
                {tab === "lab"
                  ? "让安全判断，清晰可见。"
                  : "把边界，放进测试里。"}
              </h1>
              <p>
                {tab === "lab"
                  ? "选择一个业务场景，观察 Decider 如何判断内容、识别风险。"
                  : "逐条运行成对案例，对照预期结果，发现误报、漏报与延迟变化。"}
              </p>
            </div>
            <div className="page-badge">
              <Activity size={17} />
              <div>
                真实模型推理<small>每次运行均调用已部署模型</small>
              </div>
            </div>
          </div>
          {loadError ? (
            <div className="notice error">
              <CircleAlert size={18} />
              {loadError}
              <button onClick={loadCatalog}>重新连接</button>
            </div>
          ) : null}
          <div className="overview-strip">
            <div>
              <strong>{scenarios.length || "—"}</strong>
              <span>业务场景</span>
            </div>
            <div>
              <strong>{sampleCount || "—"}</strong>
              <span>成对对照样本</span>
            </div>
            <div>
              <strong>12</strong>
              <span>策略检查维度</span>
            </div>
            <div className="overview-last">
              <span className="status-dot" />
              中英文覆盖 <span>·</span> 可自定义输入
            </div>
          </div>
          {tab === "lab" ? (
            <>
              <div className="section-heading">
                <h2>选择场景</h2>
                <span>从一个具体的问题开始</span>
              </div>
              <div className="scenario-grid">
                {scenarios.map((s) => {
                  const Icon = icons[s.icon] || ShieldCheck;
                  return (
                    <button
                      disabled={locked}
                      key={s.id}
                      className={
                        "scenario-card " +
                        (selected?.id === s.id ? "selected" : "")
                      }
                      onClick={() => chooseScenario(s)}
                    >
                      <Icon size={18} />
                      <span>{s.title}</span>
                      {selected?.id === s.id && (
                        <span className="selected-dot" />
                      )}
                    </button>
                  );
                })}
              </div>
              <div className="lab-grid">
                <section className="panel editor-panel">
                  <div className="panel-heading">
                    <div>
                      <SceneIcon size={20} />
                      <h2>{selected?.title || "内容检查"}</h2>
                    </div>
                    <span className="small-tag">
                      {source === "INPUT" ? "输入护栏" : "输出护栏"}
                    </span>
                  </div>
                  <p className="panel-description">{selected?.description}</p>
                  <div className="sample-tabs" aria-label="示例选择">
                    {selected?.cases.map((c, i) => (
                      <button
                        disabled={locked}
                        key={c.id}
                        className={sample?.id === c.id ? "chosen" : ""}
                        onClick={() => chooseSample(c)}
                      >
                        <span
                          className={
                            c.expected === "block"
                              ? "sample-dot block"
                              : "sample-dot"
                          }
                        />
                        {c.label}
                        <span className="sr-only"> 示例 {i + 1}</span>
                      </button>
                    ))}
                  </div>
                  <div className="field-top">
                    <label htmlFor="candidate">待检查内容</label>
                    <select
                      aria-label="检查方向"
                      value={source}
                      disabled={locked}
                      onChange={(e) => {
                        setSource(e.target.value as "INPUT" | "OUTPUT");
                        setSample(undefined);
                        setRuns([]);
                      }}
                    >
                      <option value="INPUT">用户输入</option>
                      <option value="OUTPUT">模型输出</option>
                    </select>
                  </div>
                  <div className="textarea-wrap">
                    <textarea
                      id="candidate"
                      maxLength={12000}
                      disabled={locked}
                      value={content}
                      placeholder="输入或粘贴一段需要检查的内容…"
                      onChange={(e) => {
                        setContent(e.target.value);
                        setSample(undefined);
                        setRuns([]);
                        setError("");
                      }}
                    />
                    <span>{content.length.toLocaleString()} / 12,000</span>
                  </div>
                  {source === "OUTPUT" && (
                    <div className="context-fields">
                      <label>
                        参考资料 <span>用于事实依据检查</span>
                        <textarea
                          value={reference}
                          maxLength={8000}
                          disabled={locked}
                          onChange={(e) => {
                            setReference(e.target.value);
                            setSample(undefined);
                            setRuns([]);
                          }}
                          placeholder="粘贴可作为依据的政策、知识库片段…"
                        />
                      </label>
                      <label>
                        用户问题 <span>用于回答相关性检查</span>
                        <textarea
                          value={query}
                          maxLength={3000}
                          disabled={locked}
                          onChange={(e) => {
                            setQuery(e.target.value);
                            setSample(undefined);
                            setRuns([]);
                          }}
                          placeholder="原始用户问题（可选）"
                        />
                      </label>
                    </div>
                  )}
                  {sample && (
                    <div className="expected-line">
                      <span>预期结果</span>
                      <span className={"decision-tag " + sample.expected}>
                        {sample.expected === "block" ? "应拦截" : "应放行"}
                      </span>
                      <small>用于对照，不会发送给模型</small>
                    </div>
                  )}
                  <div className="settings-row">
                    <div>
                      <label htmlFor="mode">检查方式</label>
                      <div className="select-wrap">
                        <select
                          id="mode"
                          value={mode}
                          disabled={locked}
                          onChange={(e) => {
                            setMode(e.target.value as Mode | "compare");
                            setRuns([]);
                          }}
                        >
                          <option value="detailed">
                            独立检查 · 每项策略分别判断
                          </option>
                          <option value="compact">
                            综合判断 · 单个策略问题
                          </option>
                          <option value="compare">
                            并排比较 · 依次运行两种方式
                          </option>
                        </select>
                        <ChevronDown size={14} />
                      </div>
                    </div>
                    <div className="threshold-control">
                      <label htmlFor="threshold">
                        拦截阈值 <b>{threshold.toFixed(2)}</b>
                      </label>
                      <input
                        id="threshold"
                        type="range"
                        min="0.1"
                        max="0.9"
                        step="0.05"
                        value={threshold}
                        disabled={locked}
                        onChange={(e) => {
                          setThreshold(Number(e.target.value));
                          setRuns([]);
                        }}
                      />
                    </div>
                  </div>
                  <div className="editor-footer">
                    <button
                      className="text-button"
                      disabled={locked || !selected}
                      onClick={() =>
                        selected && chooseSample(selected.cases[0])
                      }
                    >
                      <RotateCcw size={14} />
                      重置示例
                    </button>
                    <button
                      className="primary-button"
                      disabled={locked || !content.trim() || !scenarios.length}
                      onClick={evaluate}
                    >
                      {busy ? (
                        <LoaderCircle size={17} className="spin" />
                      ) : (
                        <Play size={16} />
                      )}
                      {busy
                        ? "正在检查…"
                        : mode === "compare"
                          ? "运行对比"
                          : "运行检查"}
                      {!busy && <span>↵</span>}
                    </button>
                  </div>
                </section>
                <section className="panel result-panel" aria-live="polite">
                  <div className="panel-heading">
                    <div>
                      <Activity size={19} />
                      <h2>判定结果</h2>
                    </div>
                    {result && (
                      <button
                        className="icon-button"
                        aria-label="下载本次结果"
                        onClick={() => saveFile("decider-result.json", runs)}
                      >
                        <ArrowDownToLine size={17} />
                      </button>
                    )}
                  </div>
                  {error && (
                    <div className="notice error">
                      <CircleAlert size={18} />
                      <div>
                        {error}
                        <small>
                          接口异常独立记录，不算模型判断正确或错误。故障处置：阻止继续处理。
                        </small>
                      </div>
                    </div>
                  )}
                  {runs.length > 1 && (
                    <div className="comparison-grid">
                      {runs.map((r) => (
                        <button
                          key={r.result.mode}
                          onClick={() => setFocusedMode(r.result.mode)}
                          className={
                            focusedMode === r.result.mode ? "focused" : ""
                          }
                        >
                          <span>{modeLabels[r.result.mode]}</span>
                          <strong className={r.result.decision}>
                            {r.result.decision === "block" ? "拦截" : "放行"}
                          </strong>
                          <small>{ms(r.result.latency_ms)}</small>
                        </button>
                      ))}
                    </div>
                  )}
                  {busy && !result ? (
                    <div className="empty-result">
                      <div className="result-orbit working">
                        <LoaderCircle size={34} className="spin" />
                      </div>
                      <h3>正在逐项检查内容</h3>
                      <p>
                        长文本与独立检查可能需要更多时间。
                        <br />
                        结果返回后会显示真实分数与耗时。
                      </p>
                    </div>
                  ) : result ? (
                    <>
                      <div className={"decision-banner " + result.decision}>
                        {result.decision === "block" ? (
                          <ShieldOff size={29} />
                        ) : (
                          <ShieldCheck size={29} />
                        )}
                        <div>
                          <span>DECIDER DECISION</span>
                          <h3>
                            {result.decision === "block"
                              ? "建议拦截"
                              : "建议放行"}
                          </h3>
                        </div>
                        <b>
                          {(result.score * 100).toFixed(1)}
                          <small>%</small>
                        </b>
                      </div>
                      {resultRun.sample && (
                        <div
                          className={
                            "agreement " +
                            (resultRun.sample.expected === result.decision
                              ? "matched"
                              : "mismatch")
                          }
                        >
                          {resultRun.sample.expected === result.decision ? (
                            <CheckCircle2 size={15} />
                          ) : (
                            <CircleAlert size={15} />
                          )}
                          {resultRun.sample.expected === result.decision
                            ? "与该示例的预期结果一致"
                            : resultRun.sample.expected === "block"
                              ? "与预期不一致：本次出现漏报"
                              : "与预期不一致：本次出现误报"}
                        </div>
                      )}
                      <div className="result-metrics">
                        <div>
                          <span>模型调用耗时</span>
                          <strong>{ms(result.latency_ms)}</strong>
                        </div>
                        <div>
                          <span>适用检查</span>
                          <strong>
                            {result.active_checks.length}
                            <small> 项</small>
                          </strong>
                        </div>
                        <div>
                          <span>拦截阈值</span>
                          <strong>{result.threshold.toFixed(2)}</strong>
                        </div>
                      </div>
                      <div className="check-list-heading">
                        <h3>
                          {result.mode === "detailed"
                            ? "各项违规分数"
                            : "综合违规分数"}
                        </h3>
                        <span>达到阈值即触发</span>
                      </div>
                      <div className="checks">
                        {[...result.checks]
                          .sort((a, b) => b.score - a.score)
                          .map((check) => (
                            <details
                              className={
                                "check-row " +
                                (check.triggered ? "triggered" : "")
                              }
                              key={check.id}
                            >
                              <summary>
                                <span>{check.name}</span>
                                <div className="score-track">
                                  <i
                                    style={{ width: `${check.score * 100}%` }}
                                  />
                                  <em
                                    style={{
                                      left: `${result.threshold * 100}%`,
                                    }}
                                  />
                                </div>
                                <b>{(check.score * 100).toFixed(1)}%</b>
                                <ChevronDown size={12} />
                              </summary>
                              <p>{check.policy}</p>
                            </details>
                          ))}
                      </div>
                      <p className="score-note">
                        {result.score_note}{" "}
                        展示策略与分数，不生成模型内部推理解释。
                      </p>
                      <details className="run-details">
                        <summary>
                          <Code2 size={14} />
                          运行信息
                          <ChevronDown size={13} />
                        </summary>
                        <dl>
                          <dt>模型</dt>
                          <dd>{result.model}</dd>
                          <dt>模型服务自报耗时</dt>
                          <dd>{ms(result.server_ms)}</dd>
                          <dt>输入 tokens</dt>
                          <dd>{result.usage.input_tokens ?? "—"}</dd>
                          <dt>策略版本</dt>
                          <dd>{result.policy_version}</dd>
                          <dt>请求 ID</dt>
                          <dd>{result.request_id}</dd>
                        </dl>
                      </details>
                    </>
                  ) : (
                    !busy && (
                      <div className="empty-result">
                        <div className="result-orbit">
                          <ShieldCheck size={36} />
                        </div>
                        <h3>下一次判断，由你开始</h3>
                        <p>
                          选择左侧示例，或写下自己的内容。
                          <br />
                          运行后，查看判定、分数和实际耗时。
                        </p>
                        <span className="empty-pill">
                          <Sparkles size={13} />
                          真实推理 · 不预设结果
                        </span>
                      </div>
                    )
                  )}
                </section>
              </div>
              <div className="bottom-note">
                <CircleAlert size={15} />
                <p>
                  场景样本为合成数据。模型可能误报或漏报；对照预期与实际结果，才能看清能力边界。
                </p>
                <span>Decider Guardrails Lab</span>
              </div>
            </>
          ) : (
            <>
              <section className="panel suite-panel">
                <div className="panel-heading">
                  <div>
                    <FileCheck2 size={20} />
                    <h2>场景测试集</h2>
                  </div>
                  <span className="small-tag">
                    {sampleCount} 个样本 · 8 组场景
                  </span>
                </div>
                <p className="panel-description">
                  每个场景包含中文与英文的拦截 /
                  放行对照。串行运行，不重试，不把接口异常混入模型准确率。
                </p>
                <div className="suite-toolbar">
                  <label>
                    检查方式{" "}
                    <select
                      aria-label="回归检查方式"
                      value={suiteMode}
                      disabled={locked}
                      onChange={(e) => setSuiteMode(e.target.value as Mode)}
                    >
                      <option value="detailed">独立检查</option>
                      <option value="compact">综合判断</option>
                    </select>
                  </label>
                  <span>下一轮阈值 {threshold.toFixed(2)}</span>
                  <div className="toolbar-spacer" />
                  <button
                    className="secondary-button"
                    disabled={!completed.length || suiteRunning}
                    onClick={() =>
                      saveFile("decider-scenarios.json", {
                        config: suiteConfig,
                        rows: suite,
                      })
                    }
                  >
                    <ArrowDownToLine size={15} />
                    导出结果
                  </button>
                  {suiteRunning ? (
                    <button
                      className="secondary-button"
                      onClick={() => {
                        stopSuite.current = true;
                      }}
                    >
                      停止后续测试
                    </button>
                  ) : (
                    <button
                      className="primary-button"
                      disabled={busy || !scenarios.length}
                      onClick={runSuite}
                    >
                      <Play size={15} />
                      运行全部场景
                    </button>
                  )}
                </div>
                {suiteConfig && (
                  <p className="panel-description">
                    本轮记录：{modeLabels[suiteConfig.mode]} · 阈值{" "}
                    {suiteConfig.threshold.toFixed(2)}
                  </p>
                )}
                {suite.length > 0 && (
                  <>
                    <div className="suite-progress">
                      <span>
                        已完成 {completed.length} / {suite.length}
                      </span>
                      <div>
                        <i
                          style={{
                            width: `${(completed.length / suite.length) * 100}%`,
                          }}
                        />
                      </div>
                      {suiteRunning && (
                        <LoaderCircle size={16} className="spin" />
                      )}
                    </div>
                    <div className="suite-metrics">
                      <div>
                        <span>有效样本准确率</span>
                        <strong>
                          {valid.length
                            ? `${((correct.length / valid.length) * 100).toFixed(1)}%`
                            : "—"}
                        </strong>
                      </div>
                      <div>
                        <span>误报 / 漏报</span>
                        <strong>
                          {fp} <small>/</small> {fn}
                        </strong>
                      </div>
                      <div>
                        <span>接口异常</span>
                        <strong>{completed.length - valid.length}</strong>
                      </div>
                      <div>
                        <span>模型调用 p95</span>
                        <strong>
                          {ms(
                            percentile(
                              valid.map((r) => r.result!.latency_ms),
                              0.95,
                            ),
                          )}
                        </strong>
                      </div>
                    </div>
                  </>
                )}
                <div className="table-scroll">
                  <table className="suite-table">
                    <thead>
                      <tr>
                        <th>业务场景 / 样本</th>
                        <th>预期</th>
                        <th>实际判定</th>
                        <th>违规分数</th>
                        <th>模型调用耗时</th>
                        <th>结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(suite.length
                        ? suite
                        : scenarios.flatMap((s) =>
                            s.cases.map(
                              (c) =>
                                ({ scenario: s.title, sample: c }) as SuiteRow,
                            ),
                          )
                      ).map((r) => (
                        <tr key={r.sample.id}>
                          <td>
                            <strong>{r.scenario}</strong>
                            <span>
                              {r.sample.language.toUpperCase()} ·{" "}
                              {r.sample.content.slice(0, 62)}
                              {r.sample.content.length > 62 ? "…" : ""}
                            </span>
                          </td>
                          <td>
                            <span
                              className={"decision-tag " + r.sample.expected}
                            >
                              {r.sample.expected === "block" ? "拦截" : "放行"}
                            </span>
                          </td>
                          <td>
                            {r.result ? (
                              <span
                                className={"decision-tag " + r.result.decision}
                              >
                                {r.result.decision === "block"
                                  ? "拦截"
                                  : "放行"}
                              </span>
                            ) : r.running ? (
                              <LoaderCircle className="spin" size={16} />
                            ) : (
                              "—"
                            )}
                          </td>
                          <td>
                            {r.result
                              ? (r.result.score * 100).toFixed(1) + "%"
                              : "—"}
                          </td>
                          <td>{ms(r.result?.latency_ms)}</td>
                          <td>
                            {r.error ? (
                              <span className="result-error" title={r.error}>
                                接口异常
                              </span>
                            ) : r.result ? (
                              r.result.decision === r.sample.expected ? (
                                <span className="match-text">
                                  <Check size={14} />
                                  符合预期
                                </span>
                              ) : (
                                <span className="result-error">
                                  {r.sample.expected === "block"
                                    ? "漏报"
                                    : "误报"}
                                </span>
                              )
                            ) : (
                              <span className="muted">
                                {r.running ? "运行中" : "待运行"}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
              <div className="bottom-note">
                <CircleAlert size={15} />
                <p>
                  演示样本包含原评测的 dev 和 test
                  案例，用于展示场景边界，不作为新的独立基准。
                </p>
              </div>
            </>
          )}
        </div>
      </main>
      {loginOpen && (
        <div
          className="modal-overlay"
          onClick={() => {
            if (!loginBusy) {
              setDraftPassword("");
              setLoginOpen(false);
            }
          }}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="login-title"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close icon-button"
              aria-label="关闭"
              disabled={loginBusy}
              onClick={() => {
                setDraftPassword("");
                setLoginOpen(false);
              }}
            >
              <X size={20} />
            </button>
            <div className="modal-icon">
              <KeyRound size={25} />
            </div>
            <h2 id="login-title">账号信息</h2>
            <p>当前登录：{username}。会话有效期为 8 小时。</p>
            <button
              className="secondary-button full-width"
              onClick={() => void logout()}
              disabled={loginBusy}
            >
              退出登录
            </button>
            {loginError && (
              <div className="form-error" role="alert">
                {loginError}
              </div>
            )}
          </section>
        </div>
      )}
      {historyOpen && (
        <div className="modal-overlay" onClick={() => setHistoryOpen(false)}>
          <section
            className="modal history-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="history-title"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close icon-button"
              aria-label="关闭记录"
              onClick={() => setHistoryOpen(false)}
            >
              <X size={20} />
            </button>
            <h2 id="history-title">本次运行记录</h2>
            <p>保留当前页面最近 20 次成功判断；刷新后清空。</p>
            {!history.length ? (
              <p className="muted">运行一次检查后，记录会出现在这里。</p>
            ) : (
              history.map((r, i) => (
                <div className="history-row" key={r.result.request_id + i}>
                  <span className={"decision-tag " + r.result.decision}>
                    {r.result.decision === "block" ? "拦截" : "放行"}
                  </span>
                  <div>
                    <strong>
                      {r.scenario} · {modeLabels[r.result.mode]}
                    </strong>
                    <p>{r.input.content.slice(0, 80)}</p>
                  </div>
                  <span>{ms(r.result.latency_ms)}</span>
                </div>
              ))
            )}
          </section>
        </div>
      )}
    </div>
  );
}
