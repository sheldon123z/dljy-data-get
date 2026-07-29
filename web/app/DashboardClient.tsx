"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { PROVINCES } from "../lib/provinces";

type Settings = {
  tokenSet: boolean;
  tokenMasked: string;
  llmKeySet: boolean;
  llmKeyMasked: string;
  provider: string;
  baseUrl: string;
  model: string;
};

type PriceRow = {
  provinceCode: string;
  province: string;
  tradeDate: string;
  realTimeAvg: number | null;
  dayAheadAvg: number | null;
  pointCount: number;
  distribution: Record<string, number>;
  collectedAt: string;
};

type Summary = {
  start_date?: string;
  end_date?: string;
  startDate?: string;
  endDate?: string;
  model: string;
  content: string;
  created_at?: string;
  createdAt?: string;
  agentMode?: string;
  agentModeLabel?: string;
  agentCount?: number;
  focus?: string;
  reliability?: {
    grade: string;
    score: number;
    assessment: string;
    coverageRate: number;
    points: number;
    caveats: string[];
  };
  agents?: Array<{ role: string; name: string; content: string }>;
  citationValidation?: {
    validEvidenceCount: number;
    citedEvidenceCount: number;
    passed: boolean;
  };
};

const PROVIDERS: Record<string, { baseUrl: string; model: string }> = {
  deepseek: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  glm: { baseUrl: "https://open.bigmodel.cn/api/paas/v4", model: "glm-5.2" },
  custom: { baseUrl: "", model: "" },
};
const BIN_ORDER = ["<0", "0-100", "100-200", "200-300", "300-400", "400-500", "500+"];
const BIN_COLORS: Record<string, string> = {
  "<0": "#7052ce",
  "0-100": "#3c83f6",
  "100-200": "#38b9c7",
  "200-300": "#83c45e",
  "300-400": "#e5b84c",
  "400-500": "#ee7c43",
  "500+": "#db5061",
};

async function requestJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const payload = (await response.json().catch(() => ({}))) as { error?: string };
  if (!response.ok) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
  return payload as T;
}

function isoDay(date: Date) {
  return date.toISOString().slice(0, 10);
}

function recentDates(days: number) {
  const values: string[] = [];
  const end = new Date();
  end.setUTCHours(0, 0, 0, 0);
  end.setUTCDate(end.getUTCDate() - 1);
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const current = new Date(end);
    current.setUTCDate(end.getUTCDate() - offset);
    values.push(isoDay(current));
  }
  return values;
}

function formatPrice(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(2);
}

function TrendChart({ rows }: { rows: PriceRow[] }) {
  const points = useMemo(() => {
    const grouped = new Map<string, number[]>();
    for (const row of rows) {
      if (row.realTimeAvg == null) continue;
      const values = grouped.get(row.tradeDate) || [];
      values.push(row.realTimeAvg);
      grouped.set(row.tradeDate, values);
    }
    return [...grouped.entries()].sort().map(([date, values]) => ({
      date,
      value: values.reduce((sum, value) => sum + value, 0) / values.length,
    }));
  }, [rows]);

  if (!points.length) return <Empty message="采集完成后将在这里显示全国日均实时价格曲线" />;
  const width = 900;
  const height = 270;
  const pad = { left: 58, right: 20, top: 24, bottom: 42 };
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = Math.max(1, max - min);
  const low = min - spread * 0.15;
  const high = max + spread * 0.15;
  const x = (index: number) =>
    pad.left + (index * (width - pad.left - pad.right)) / Math.max(1, points.length - 1);
  const y = (value: number) =>
    pad.top + ((high - value) * (height - pad.top - pad.bottom)) / (high - low);
  const path = points.map((point, index) => `${index ? "L" : "M"} ${x(index)} ${y(point.value)}`).join(" ");

  return (
    <div className="chart-scroll">
      <svg viewBox={`0 0 ${width} ${height}`} className="trend-chart" role="img" aria-label="全国日均实时价格曲线">
        <defs>
          <linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#58d6c5" stopOpacity=".32" />
            <stop offset="100%" stopColor="#58d6c5" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3, 4].map((tick) => {
          const value = low + ((high - low) * (4 - tick)) / 4;
          const top = pad.top + (tick * (height - pad.top - pad.bottom)) / 4;
          return (
            <g key={tick}>
              <line x1={pad.left} x2={width - pad.right} y1={top} y2={top} className="gridline" />
              <text x={pad.left - 10} y={top + 4} textAnchor="end" className="axis-label">{value.toFixed(0)}</text>
            </g>
          );
        })}
        <path d={`${path} L ${x(points.length - 1)} ${height - pad.bottom} L ${x(0)} ${height - pad.bottom} Z`} fill="url(#trend-fill)" />
        <path d={path} className="trend-line" />
        {points.map((point, index) => (
          <g key={point.date}>
            <circle cx={x(index)} cy={y(point.value)} r="5.5" className="trend-dot">
              <title>{`${point.date}：${point.value.toFixed(2)} 元/MWh`}</title>
            </circle>
            <text x={x(index)} y={height - 16} textAnchor="middle" className="axis-label">
              {point.date.slice(5)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function DistributionChart({ rows }: { rows: PriceRow[] }) {
  const regional = useMemo(() => {
    const map = new Map<string, Record<string, number>>();
    for (const row of rows) {
      const total = map.get(row.province) || {};
      for (const label of BIN_ORDER) total[label] = (total[label] || 0) + Number(row.distribution[label] || 0);
      map.set(row.province, total);
    }
    return [...map.entries()].map(([province, bins]) => ({ province, bins }));
  }, [rows]);

  function exportPng() {
    if (!regional.length) return;
    const scale = 2;
    const width = 1200;
    const rowHeight = 36;
    const height = 100 + regional.length * rowHeight;
    const canvas = document.createElement("canvas");
    canvas.width = width * scale;
    canvas.height = height * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.fillStyle = "#0b1724";
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = "#f2f7f8";
    ctx.font = "bold 24px sans-serif";
    ctx.fillText("各地区实时价格区间占比", 28, 36);
    ctx.font = "13px sans-serif";
    BIN_ORDER.forEach((label, index) => {
      ctx.fillStyle = BIN_COLORS[label];
      ctx.fillRect(28 + index * 150, 52, 13, 13);
      ctx.fillStyle = "#b6c4ca";
      ctx.fillText(label, 47 + index * 150, 63);
    });
    regional.forEach(({ province, bins }, index) => {
      const top = 86 + index * rowHeight;
      const total = BIN_ORDER.reduce((sum, label) => sum + (bins[label] || 0), 0);
      ctx.fillStyle = "#d9e4e7";
      ctx.font = "14px sans-serif";
      ctx.fillText(province, 28, top + 17);
      let left = 112;
      BIN_ORDER.forEach((label) => {
        const segment = total ? ((bins[label] || 0) / total) * 1045 : 0;
        ctx.fillStyle = BIN_COLORS[label];
        ctx.fillRect(left, top, segment, 22);
        left += segment;
      });
    });
    const link = document.createElement("a");
    link.download = `实时价格区间占比_${new Date().toISOString().slice(0, 10)}.png`;
    link.href = canvas.toDataURL("image/png");
    link.click();
  }

  if (!regional.length) return <Empty message="暂无价格区间数据" />;
  return (
    <>
      <div className="legend">
        {BIN_ORDER.map((label) => (
          <span key={label}><i style={{ background: BIN_COLORS[label] }} />{label}</span>
        ))}
        <button className="text-button" onClick={exportPng}>导出 PNG</button>
      </div>
      <div className="distribution-list">
        {regional.map(({ province, bins }) => {
          const total = BIN_ORDER.reduce((sum, label) => sum + (bins[label] || 0), 0);
          return (
            <div className="distribution-row" key={province}>
              <span>{province}</span>
              <div className="stack-bar">
                {BIN_ORDER.map((label) => {
                  const count = bins[label] || 0;
                  const percent = total ? (count / total) * 100 : 0;
                  return (
                    <i key={label} style={{ width: `${percent}%`, background: BIN_COLORS[label] }}>
                      <span>{`${province}｜${label} 元/MWh：${count} 个时点（${percent.toFixed(1)}%）`}</span>
                    </i>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function Empty({ message }: { message: string }) {
  return <div className="empty">{message}</div>;
}

export default function DashboardClient() {
  const [days, setDays] = useState(7);
  const [token, setToken] = useState("");
  const [llmKey, setLlmKey] = useState("");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [provider, setProvider] = useState("deepseek");
  const [baseUrl, setBaseUrl] = useState(PROVIDERS.deepseek.baseUrl);
  const [model, setModel] = useState(PROVIDERS.deepseek.model);
  const [agentMode, setAgentMode] = useState("standard");
  const [agentFocus, setAgentFocus] = useState("");
  const [rows, setRows] = useState<PriceRow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [status, setStatus] = useState("准备就绪");
  const [progress, setProgress] = useState(0);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    const next = await requestJson<Settings>("/api/settings");
    setSettings(next);
    setProvider(next.provider);
    setBaseUrl(next.baseUrl);
    setModel(next.model);
  }, []);

  const loadData = useCallback(async (selectedDays = days) => {
    const result = await requestJson<{ rows: PriceRow[]; latestSummary: Summary | null }>(`/api/data?days=${selectedDays}`);
    setRows(result.rows);
    setSummary(result.latestSummary);
  }, [days]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      Promise.all([loadSettings(), loadData()]).catch((error) => setStatus(error.message));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadSettings, loadData]);

  const stats = useMemo(() => {
    const values = rows.map((row) => row.realTimeAvg).filter((value): value is number => value != null);
    const dates = [...new Set(rows.map((row) => row.tradeDate))].sort();
    const provinces = new Set(rows.map((row) => row.province));
    const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    const latestDate = dates.at(-1);
    const latest = rows.filter((row) => row.tradeDate === latestDate);
    const highest = [...latest].filter((row) => row.realTimeAvg != null).sort((a, b) => b.realTimeAvg! - a.realTimeAvg!)[0];
    return { average, dates, provinces: provinces.size, latestDate, highest };
  }, [rows]);

  const ranking = useMemo(() => {
    const map = new Map<string, number[]>();
    rows.forEach((row) => {
      if (row.realTimeAvg == null) return;
      const values = map.get(row.province) || [];
      values.push(row.realTimeAvg);
      map.set(row.province, values);
    });
    return [...map.entries()].map(([province, values]) => ({
      province,
      value: values.reduce((sum, value) => sum + value, 0) / values.length,
    })).sort((a, b) => b.value - a.value);
  }, [rows]);

  async function saveSettings() {
    setSaving(true);
    setStatus("正在安全保存配置…");
    try {
      const saved = await requestJson<Settings>("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, llmKey, provider, baseUrl, model }),
      });
      setSettings(saved);
      setToken("");
      setLlmKey("");
      setStatus("配置已加密保存，密钥不会回传浏览器");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  function selectProvider(value: string) {
    setProvider(value);
    if (value !== "custom") {
      setBaseUrl(PROVIDERS[value].baseUrl);
      setModel(PROVIDERS[value].model);
    }
  }

  async function collect() {
    if (!settings?.tokenSet && !token.trim()) {
      setStatus("请先填写并保存采集 Token");
      return;
    }
    setRunning(true);
    setProgress(0);
    const jobs = recentDates(days).flatMap((tradeDate) =>
      PROVINCES.map(([, provinceCode]) => ({ tradeDate, provinceCode })),
    );
    let nextIndex = 0;
    let completed = 0;
    let failed = 0;
    setStatus(`开始采集 ${days} 天 × ${PROVINCES.length} 个地区…`);
    async function worker() {
      while (nextIndex < jobs.length) {
        const job = jobs[nextIndex++];
        try {
          await requestJson("/api/collect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(job),
          });
        } catch {
          failed += 1;
        }
        completed += 1;
        setProgress(Math.round((completed / jobs.length) * 100));
        setStatus(`已完成 ${completed}/${jobs.length}，失败 ${failed}`);
      }
    }
    try {
      await Promise.all([worker(), worker(), worker()]);
      await loadData(days);
      setStatus(failed ? `采集完成：成功 ${completed - failed}，失败 ${failed}` : `采集完成：${completed} 个地区日任务全部成功`);
    } finally {
      setRunning(false);
    }
  }

  async function summarize() {
    setRunning(true);
    setStatus("正在整理结构化数据并调用大模型…");
    try {
      const next = await requestJson<Summary>("/api/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days, agentMode, focus: agentFocus }),
      });
      setSummary(next);
      setStatus(`总结已由 ${next.model} 生成`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "总结失败");
    } finally {
      setRunning(false);
    }
  }

  return (
    <main>
      <header className="topbar">
        <a className="brand" href="#top"><span>电</span>电力现货价格工作台</a>
        <nav><a href="#overview">数据概览</a><a href="#distribution">区间分布</a><a href="#analysis">智能分析</a></nav>
        <span className="live-indicator"><i />安全私有空间</span>
      </header>

      <section className="hero" id="top">
        <div>
          <p className="eyebrow">SPOT MARKET INTELLIGENCE</p>
          <h1>把价格数据，变成<br /><em>每天可执行的判断</em></h1>
          <p>按设定周期采集 29 个地区电力现货价格，由趋势、区域、分布、质疑、决策与审校 Agent 协作生成可追溯总结。</p>
          <div className="hero-actions">
            <button className="primary" onClick={collect} disabled={running}>采集并更新看板</button>
            <button className="secondary" onClick={summarize} disabled={running}>生成智能总结</button>
          </div>
        </div>
        <aside className="control-panel">
          <div className="panel-heading"><div><span>数据与模型配置</span><small>按 ①—③ 完成首次设置；密钥仅在服务端加密保存</small></div><b>{settings?.tokenSet ? "已配置" : "待配置"}</b></div>
          <label>采集数据长度（天）
            <input type="number" min="1" max="366" value={days} onChange={(event) => setDays(Math.max(1, Math.min(366, Number(event.target.value) || 1)))} />
          </label>
          <label>① 数据采集 Authorization <small>{settings?.tokenSet ? `当前 ${settings.tokenMasked}` : "必填：尚未设置"}</small>
            <input type="password" value={token} onChange={(event) => setToken(event.target.value)} placeholder="粘贴完整 Authorization；Bearer 前缀请保留" autoComplete="off" />
          </label>
          <p className="field-help">仅采集新数据时需要；未填写也可以查看已有数据。值只写入服务端配置，不进入图表或导出文件。</p>
          <div className="split">
            <label>模型服务
              <select value={provider} onChange={(event) => selectProvider(event.target.value)}>
                <option value="deepseek">DeepSeek</option>
                <option value="glm">智谱 GLM</option>
                <option value="custom">兼容接口</option>
              </select>
            </label>
            <label>模型名称
              <input value={model} onChange={(event) => setModel(event.target.value)} />
            </label>
          </div>
          <label>接口地址
            <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
          </label>
          <label>② 模型 API Key <small>{settings?.llmKeySet ? `当前 ${settings.llmKeyMasked}` : "生成总结时必填"}</small>
            <input type="password" value={llmKey} onChange={(event) => setLlmKey(event.target.value)} placeholder="留空则保留原 Key" autoComplete="off" />
          </label>
          <p className="field-help">仅生成 AI 总结时需要；支持 DeepSeek、智谱 GLM 及 OpenAI 兼容接口。</p>
          <label>③ Agent 协作模式
            <select value={agentMode} onChange={(event) => setAgentMode(event.target.value)}>
              <option value="quick">快速双 Agent（省时省费用）</option>
              <option value="standard">标准五 Agent（推荐）</option>
              <option value="rigorous">严格七 Agent（重要汇报）</option>
            </select>
          </label>
          <label>特别关注问题（可选）
            <textarea value={agentFocus} maxLength={500} onChange={(event) => setAgentFocus(event.target.value)} placeholder="例如：重点比较西北与华东，并检查400元/MWh以上时段" />
          </label>
          <button className="save-button" onClick={saveSettings} disabled={saving}>{saving ? "保存中…" : "保存 Authorization 与模型配置"}</button>
          <div className="task-status">
            <span>{status}</span><strong>{running ? `${progress}%` : "READY"}</strong>
            <i><b style={{ width: `${running ? progress : 100}%` }} /></i>
          </div>
        </aside>
      </section>

      <section className="section" id="overview">
        <div className="section-heading">
          <div><p>MARKET OVERVIEW</p><h2>所选周期市场概览</h2></div>
          <button className="text-button" onClick={() => loadData(days).catch((error) => setStatus(error.message))}>刷新数据</button>
        </div>
        <div className="metric-grid">
          <article><small>覆盖交易日</small><strong>{stats.dates.length}</strong><span>设定 {days} 天</span></article>
          <article><small>覆盖地区</small><strong>{stats.provinces}</strong><span>全国省级市场</span></article>
          <article><small>全国实时均价</small><strong>{formatPrice(stats.average)}</strong><span>元/MWh</span></article>
          <article><small>最新日高价地区</small><strong className="text-value">{stats.highest?.province || "—"}</strong><span>{formatPrice(stats.highest?.realTimeAvg)} 元/MWh</span></article>
        </div>
        <div className="dashboard-grid">
          <article className="card chart-card">
            <div className="card-title"><div><h3>全国日均实时价格</h3><p>将鼠标移至曲线节点查看日期和精确数值</p></div><span>元/MWh</span></div>
            <TrendChart rows={rows} />
          </article>
          <article className="card ranking-card">
            <div className="card-title"><div><h3>地区周期均价</h3><p>按实时价格从高到低</p></div></div>
            {ranking.length ? <div className="ranking-list">{ranking.slice(0, 12).map((item, index) => (
              <div key={item.province}><b>{String(index + 1).padStart(2, "0")}</b><span>{item.province}</span><i><em style={{ width: `${Math.max(4, (item.value / ranking[0].value) * 100)}%` }} /></i><strong>{item.value.toFixed(1)}</strong></div>
            ))}</div> : <Empty message="暂无地区均价" />}
          </article>
        </div>
      </section>

      <section className="section section-dark" id="distribution">
        <div className="section-heading">
          <div><p>PRICE DISTRIBUTION</p><h2>各地区实时价格区间占比</h2></div>
          <span className="note">统计原始分时时点，不以日均价替代</span>
        </div>
        <article className="card distribution-card"><DistributionChart rows={rows} /></article>
      </section>

      <section className="section analysis-section" id="analysis">
        <div className="section-heading">
          <div><p>AI MARKET BRIEF</p><h2>多 Agent 数据审校与周期总结</h2></div>
          <button className="primary compact" onClick={summarize} disabled={running}>重新生成</button>
        </div>
        <article className="summary-card">
          <div className="summary-meta">
            <span>协作模式</span><strong>{summary?.agentModeLabel || (agentMode === "rigorous" ? "严格七 Agent" : agentMode === "quick" ? "快速双 Agent" : "标准五 Agent")}</strong>
            <span>模型</span><strong>{summary?.model || model}</strong>
            <span>数据周期</span><strong>{summary ? `${summary.start_date || summary.startDate} — ${summary.end_date || summary.endDate}` : "等待生成"}</strong>
          </div>
          <div className="summary-main">
            {summary?.reliability && (
              <div className={`reliability-card grade-${summary.reliability.grade.toLowerCase()}`}>
                <strong>{summary.reliability.grade}级 · {summary.reliability.score}分</strong>
                <span>{summary.reliability.assessment}｜区域日覆盖 {summary.reliability.coverageRate}%｜{summary.reliability.points} 个分时时点</span>
                <div className="agent-badges">{(summary.agents || []).map((agent) => <i key={agent.role}>{agent.name}</i>)}</div>
              </div>
            )}
            {summary?.content ? <div className="summary-content">{summary.content}</div> : <Empty message="保存模型 API Key 并采集数据后，即可生成带证据编号、可靠性评分和独立审校的周期简报。" />}
          </div>
        </article>
      </section>

      <footer><span>电力现货价格工作台</span><p>数据结果仅供研究与决策支持，请以交易机构正式披露为准。</p></footer>
    </main>
  );
}
