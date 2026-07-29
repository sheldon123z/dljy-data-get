import { env } from "cloudflare:workers";
import { decryptSecret, ensureSchema, jsonError, ownerEmail } from "../../../lib/server";

export const runtime = "edge";

type SettingsRow = {
  encrypted_llm_key: string | null;
  llm_base_url: string;
  llm_model: string;
};
type PriceRow = {
  province: string;
  trade_date: string;
  real_time_avg: number | null;
  day_ahead_avg: number | null;
  point_count: number;
  distribution_json: string;
};
type Evidence = {
  id: string;
  label: string;
  value: string | number | null;
  unit?: string;
  scope?: string;
  source: string;
};
type AgentOutput = { role: string; name: string; content: string };

const AGENTS = {
  trend: ["趋势分析 Agent", "分析全国价格方向、幅度和前后周期变化，不推测外部原因。"],
  regional: ["区域比较 Agent", "识别高低价格地区、变化较大地区和区域分化。"],
  distribution: ["价格分布 Agent", "分析各价格区间、负价、高价和极端时点风险。"],
  skeptic: ["审慎质疑 Agent", "寻找覆盖、异常、平均口径和可能误导读者的表述。"],
  decision: ["决策建议 Agent", "把已验证信号转化为下一周期监测清单。"],
} as const;
const MODES = {
  quick: ["trend"],
  standard: ["trend", "regional", "distribution"],
  rigorous: ["trend", "regional", "distribution", "skeptic", "decision"],
} as const;
const MODE_LABELS = { quick: "快速双 Agent", standard: "标准五 Agent", rigorous: "严格七 Agent" };

function average(values: number[]) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}
function round(value: number | null) {
  return value === null ? null : Math.round(value * 100) / 100;
}
function evidenceText(evidence: Evidence[]) {
  return evidence.map((item) =>
    `[${item.id}] ${item.label}：${item.value} ${item.unit || ""}${item.scope ? `（${item.scope}）` : ""}；来源：${item.source}`,
  ).join("\n");
}

async function callModel(settings: SettingsRow, apiKey: string, prompt: string) {
  const remote = await fetch(`${settings.llm_base_url.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: settings.llm_model,
      messages: [
        { role: "system", content: "你只依据提供的结构化证据分析，不编造事实。" },
        { role: "user", content: prompt },
      ],
      temperature: 0.15,
    }),
  });
  if (!remote.ok) {
    const detail = (await remote.text()).slice(0, 240);
    throw new Error(`模型接口返回 HTTP ${remote.status}：${detail}`);
  }
  const completion = (await remote.json()) as { choices?: Array<{ message?: { content?: string } }> };
  const content = completion.choices?.[0]?.message?.content?.trim();
  if (!content) throw new Error("模型未返回总结内容");
  return content;
}

function reliabilityFor(rows: PriceRow[], dates: string[], requestedDays: number) {
  const expectedRegionDays = requestedDays * 29;
  const availableRegionDays = rows.filter((row) => row.point_count > 0).length;
  const coverageRate = expectedRegionDays ? availableRegionDays / expectedRegionDays * 100 : 0;
  const points = rows.reduce((sum, row) => sum + row.point_count, 0);
  const latest = dates.at(-1) ? new Date(`${dates.at(-1)}T00:00:00Z`) : new Date(0);
  const yesterday = new Date();
  yesterday.setUTCHours(0, 0, 0, 0);
  yesterday.setUTCDate(yesterday.getUTCDate() - 1);
  const freshnessDays = Math.max(0, Math.round((yesterday.getTime() - latest.getTime()) / 86400000));
  let score = 100 - Math.min(55, Math.max(0, 100 - coverageRate) * 0.8) - Math.min(15, freshnessDays * 3);
  if (!points) score -= 25;
  score = Math.max(0, Math.round(score * 10) / 10);
  const grade = score >= 90 ? "A" : score >= 75 ? "B" : score >= 60 ? "C" : "D";
  const assessment = grade === "A" ? "可直接使用" : grade === "D" ? "需修订后使用" : "需附带说明";
  const caveats: string[] = [];
  if (coverageRate < 98) caveats.push(`区域日覆盖率为 ${coverageRate.toFixed(1)}%，并非完整覆盖。`);
  if (freshnessDays) caveats.push(`最新交易日距昨日相差 ${freshnessDays} 天。`);
  if (!caveats.length) caveats.push("未发现影响结论的明显覆盖或时效性问题。");
  return {
    score,
    grade,
    assessment,
    coverageRate: Math.round(coverageRate * 100) / 100,
    expectedRegionDays,
    availableRegionDays,
    points,
    freshnessDays,
    caveats,
  };
}

function makeEvidence(
  context: Record<string, unknown>,
  reliability: ReturnType<typeof reliabilityFor>,
  provinceStats: Array<Record<string, string | number | null>>,
  distribution: Record<string, number>,
) {
  const period = context.period as { start: string; end: string; actualDays: number };
  const evidence: Evidence[] = [
    { id: "E01", label: "统计周期", value: `${period.start} 至 ${period.end}`, scope: `${period.actualDays}个交易日`, source: "daily_prices" },
    { id: "E02", label: "全国区域日等权实时均价", value: context.nationalRealTimeAverage as number, unit: "元/MWh", source: "daily_prices" },
    { id: "E03", label: "有效区域日覆盖率", value: reliability.coverageRate, unit: "%", source: "daily_prices" },
    { id: "E04", label: "有效实时分时时点", value: reliability.points, unit: "个", source: "daily_prices" },
    { id: "E05", label: "数据可靠性评分", value: reliability.score, unit: "分", scope: `${reliability.grade}级`, source: "确定性质量检查" },
  ];
  if (provinceStats.length) {
    evidence.push(
      { id: "E06", label: "周期均价最高地区", value: provinceStats[0].average, unit: "元/MWh", scope: String(provinceStats[0].province), source: "daily_prices" },
      { id: "E07", label: "周期均价最低地区", value: provinceStats.at(-1)!.average, unit: "元/MWh", scope: String(provinceStats.at(-1)!.province), source: "daily_prices" },
    );
  }
  Object.entries(distribution).forEach(([label, count], index) => evidence.push({
    id: `E${String(index + 8).padStart(2, "0")}`,
    label: `${label}价格时点`,
    value: count,
    unit: "个",
    source: "daily_prices.distribution_json",
  }));
  return evidence;
}

export async function POST(request: Request) {
  try {
    await ensureSchema();
    const email = ownerEmail(request);
    const body = (await request.json()) as Record<string, unknown>;
    const rawDays = Number(body.days || 7);
    const days = Math.max(1, Math.min(366, Number.isFinite(rawDays) ? Math.floor(rawDays) : 7));
    const agentMode = String(body.agentMode || "standard") as keyof typeof MODES;
    if (!MODES[agentMode]) return jsonError("Agent 模式无效");
    const focus = String(body.focus || "").trim().slice(0, 500);
    const settings = await env.DB.prepare(
      "SELECT encrypted_llm_key, llm_base_url, llm_model FROM settings WHERE owner_email = ?",
    ).bind(email).first<SettingsRow>();
    if (!settings) return jsonError("请先保存大模型配置", 409);
    const apiKey = await decryptSecret(settings.encrypted_llm_key);
    if (!apiKey) return jsonError("请先在明确的模型 API Key 输入框中保存密钥", 409);

    const result = await env.DB.prepare(
      `SELECT province, trade_date, real_time_avg, day_ahead_avg, point_count, distribution_json
       FROM daily_prices
       WHERE owner_email = ?
         AND trade_date >= date((SELECT MAX(trade_date) FROM daily_prices WHERE owner_email = ?),
                                '-' || (? - 1) || ' day')
       ORDER BY trade_date, province`,
    ).bind(email, email, days).all<PriceRow>();
    const rows = result.results || [];
    if (!rows.length) return jsonError("没有可总结的数据，请先采集", 409);

    const dates = [...new Set(rows.map((row) => row.trade_date))].sort();
    const provinces = [...new Set(rows.map((row) => row.province))].sort();
    const provinceStats = provinces.map((province) => {
      const matches = rows.filter((row) => row.province === province);
      const values = matches.map((row) => row.real_time_avg).filter((value): value is number => value !== null);
      return {
        province,
        average: round(average(values)),
        first: round(values[0] ?? null),
        last: round(values.at(-1) ?? null),
        change: values.length > 1 ? round(values.at(-1)! - values[0]) : null,
      };
    }).sort((a, b) => (b.average ?? -Infinity) - (a.average ?? -Infinity));
    const distribution: Record<string, number> = {};
    rows.forEach((row) => {
      const bins = JSON.parse(row.distribution_json || "{}") as Record<string, number>;
      Object.entries(bins).forEach(([label, count]) => {
        distribution[label] = (distribution[label] || 0) + Number(count || 0);
      });
    });
    const reliability = reliabilityFor(rows, dates, days);
    const context = {
      period: { start: dates[0], end: dates.at(-1), requestedDays: days, actualDays: dates.length },
      nationalRealTimeAverage: round(average(rows.map((row) => row.real_time_avg).filter((v): v is number => v !== null))),
      provinceRanking: provinceStats,
      realTimePriceDistribution: distribution,
      coverage: { provinces: provinces.length, rows: rows.length, points: reliability.points },
    };
    const evidence = makeEvidence(context, reliability, provinceStats, distribution);
    const roles = MODES[agentMode];
    const agents: AgentOutput[] = await Promise.all(roles.map(async (role) => {
      const [name, assignment] = AGENTS[role];
      const prompt = `你是${name}。${assignment}
只能依据下列数据和证据；每条重要结论引用 [E##]；不得编造因果；缺失值不按0；外部原因必须写“需要外部数据验证”。
输出不超过550字，按“发现 / 风险与限制 / 建议验证”组织。
${focus ? `用户特别关注：${focus}` : ""}
可靠性：${JSON.stringify(reliability)}
证据：\n${evidenceText(evidence)}
数据：${JSON.stringify(context)}`;
      return { role, name, content: await callModel(settings, apiKey, prompt) };
    }));
    let audit = "";
    if (agentMode !== "quick") {
      audit = await callModel(settings, apiKey, `你是独立审校 Agent。检查草稿中的无证据数字、虚构因果、覆盖率忽略、平均口径混淆和无效证据编号。
按“可保留 / 必须修正 / 不可验证 / 写作约束”输出，不超过650字。
可靠性：${JSON.stringify(reliability)}
证据：\n${evidenceText(evidence)}
草稿：${JSON.stringify(agents)}`);
      agents.push({ role: "auditor", name: "独立审校 Agent", content: audit });
    }
    let content = await callModel(settings, apiKey, `你是报告主编 Agent。合并草稿为1200字以内中文周期简报。
依次写核心结论、全国走势、区域差异、价格区间、数据限制和下一周期关注；每条关键结论引用 [E##]；
全国均价必须写“区域日等权平均”；不得写无证据因果；可靠性非A级时降低结论强度；最后给2—4条外部数据核验建议。
${focus ? `优先回答：${focus}` : ""}
可靠性：${JSON.stringify(reliability)}
证据：\n${evidenceText(evidence)}
审校：${audit || "快速模式无独立审校"}
草稿：${JSON.stringify(agents)}`);
    const validIds = new Set(evidence.map((item) => item.id));
    const cited = [...content.matchAll(/\[(E\d{2})\]/g)].map((match) => match[1]);
    const unknown = [...new Set(cited.filter((id) => !validIds.has(id)))];
    const citationValidation = {
      validEvidenceCount: validIds.size,
      citedEvidenceCount: new Set(cited.filter((id) => validIds.has(id))).size,
      unknownCitations: unknown,
      passed: cited.length > 0 && !unknown.length,
    };
    content += `\n\n## 数据可靠性
- 评级：${reliability.grade}（${reliability.score}分，${reliability.assessment}）
- 有效区域日覆盖：${reliability.availableRegionDays}/${reliability.expectedRegionDays}（${reliability.coverageRate}%）
- 有效实时分时时点：${reliability.points}
- 引证检查：引用 ${citationValidation.citedEvidenceCount}/${citationValidation.validEvidenceCount} 条证据；${citationValidation.passed ? "通过" : "需留意"}
${reliability.caveats.map((item) => `- ${item}`).join("\n")}

## 证据索引
${evidenceText(evidence)}`;
    const createdAt = new Date().toISOString();
    const details = {
      agentMode,
      agentModeLabel: MODE_LABELS[agentMode],
      agentCount: agents.length + 1,
      focus,
      reliability,
      evidence,
      citationValidation,
      agents,
    };
    await env.DB.prepare(
      `INSERT INTO summaries (owner_email, start_date, end_date, model, content, details_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)`,
    ).bind(email, dates[0], dates.at(-1), settings.llm_model, content, JSON.stringify(details), createdAt).run();
    return Response.json({
      startDate: dates[0],
      endDate: dates.at(-1),
      model: settings.llm_model,
      content,
      createdAt,
      ...details,
    });
  } catch (error) {
    return jsonError(error instanceof Error ? error.message : "总结失败", 500);
  }
}
