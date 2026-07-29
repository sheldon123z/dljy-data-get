import { env } from "cloudflare:workers";
import { provinceByCode } from "../../../lib/provinces";
import { decryptSecret, ensureSchema, jsonError, ownerEmail } from "../../../lib/server";

export const runtime = "edge";

type SettingsRow = { encrypted_token: string | null };
type RemotePoint = {
  avgDayAheadPrice?: number | string | null;
  avgRealTimePrice?: number | string | null;
};

const BINS = [
  ["<0", Number.NEGATIVE_INFINITY, 0],
  ["0-100", 0, 100],
  ["100-200", 100, 200],
  ["200-300", 200, 300],
  ["300-400", 300, 400],
  ["400-500", 400, 500],
  ["500+", 500, Number.POSITIVE_INFINITY],
] as const;

function mean(values: number[]) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function validDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
}

export async function POST(request: Request) {
  try {
    await ensureSchema();
    const email = ownerEmail(request);
    const body = (await request.json()) as Record<string, unknown>;
    const province = provinceByCode(String(body.provinceCode || ""));
    const tradeDate = String(body.tradeDate || "");
    if (!province) return jsonError("地区代码无效");
    if (!validDate(tradeDate)) return jsonError("交易日期无效");

    const row = await env.DB.prepare(
      "SELECT encrypted_token FROM settings WHERE owner_email = ?",
    ).bind(email).first<SettingsRow>();
    const token = await decryptSecret(row?.encrypted_token || null);
    if (!token) return jsonError("请先保存采集 Token", 409);

    const remote = await fetch(
      "https://elecheck.aienertech.cn/electricCheckApi/queryData/clearPrice/detail",
      {
        method: "POST",
        headers: {
          Authorization: token,
          Accept: "*/*",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          areaCode: province.code,
          startDate: tradeDate,
          endDate: tradeDate,
        }),
      },
    );
    if (remote.status === 401 || remote.status === 403) {
      return jsonError("采集 Token 已失效，请重新填写", 401);
    }
    if (!remote.ok) return jsonError(`上游接口返回 HTTP ${remote.status}`, 502);
    const payload = (await remote.json()) as {
      code?: number;
      message?: string;
      msg?: string;
      data?: RemotePoint[];
    };
    if (payload.code !== 200) {
      return jsonError(payload.message || payload.msg || `上游接口错误 ${payload.code}`, 502);
    }

    const points = Array.isArray(payload.data) ? payload.data : [];
    const realTime = points
      .map((point) => Number(point.avgRealTimePrice))
      .filter(Number.isFinite);
    const dayAhead = points
      .map((point) => Number(point.avgDayAheadPrice))
      .filter(Number.isFinite);
    const distribution = Object.fromEntries(BINS.map(([label]) => [label, 0]));
    for (const price of realTime) {
      const bin = BINS.find(([, low, high]) => price >= low && price < high);
      if (bin) distribution[bin[0]] += 1;
    }

    const collectedAt = new Date().toISOString();
    await env.DB.prepare(
      `INSERT INTO daily_prices
       (owner_email, province_code, province, trade_date, real_time_avg, day_ahead_avg,
        point_count, distribution_json, collected_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(owner_email, province_code, trade_date) DO UPDATE SET
         province = excluded.province,
         real_time_avg = excluded.real_time_avg,
         day_ahead_avg = excluded.day_ahead_avg,
         point_count = excluded.point_count,
         distribution_json = excluded.distribution_json,
         collected_at = excluded.collected_at`,
    ).bind(
      email,
      province.code,
      province.name,
      tradeDate,
      mean(realTime),
      mean(dayAhead),
      points.length,
      JSON.stringify(distribution),
      collectedAt,
    ).run();

    return Response.json({
      ok: true,
      province: province.name,
      tradeDate,
      pointCount: points.length,
      realTimeAvg: mean(realTime),
      dayAheadAvg: mean(dayAhead),
    });
  } catch (error) {
    return jsonError(error instanceof Error ? error.message : "采集失败", 500);
  }
}
