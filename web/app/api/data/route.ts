import { env } from "cloudflare:workers";
import { ensureSchema, jsonError, ownerEmail } from "../../../lib/server";

export const runtime = "edge";

type PriceRow = {
  province_code: string;
  province: string;
  trade_date: string;
  real_time_avg: number | null;
  day_ahead_avg: number | null;
  point_count: number;
  distribution_json: string;
  collected_at: string;
};

type SummaryRow = {
  start_date: string;
  end_date: string;
  model: string;
  content: string;
  details_json: string;
  created_at: string;
};

export async function GET(request: Request) {
  try {
    await ensureSchema();
    const email = ownerEmail(request);
    const parsed = Number(new URL(request.url).searchParams.get("days") || "7");
    const days = Math.max(1, Math.min(366, Number.isFinite(parsed) ? Math.floor(parsed) : 7));
    const rows = await env.DB.prepare(
      `SELECT province_code, province, trade_date, real_time_avg, day_ahead_avg,
              point_count, distribution_json, collected_at
       FROM daily_prices
       WHERE owner_email = ?
         AND trade_date >= date((SELECT MAX(trade_date) FROM daily_prices WHERE owner_email = ?),
                                '-' || (? - 1) || ' day')
       ORDER BY trade_date, province`,
    ).bind(email, email, days).all<PriceRow>();
    const latestSummary = await env.DB.prepare(
      `SELECT start_date, end_date, model, content, details_json, created_at
       FROM summaries WHERE owner_email = ? ORDER BY id DESC LIMIT 1`,
    ).bind(email).first<SummaryRow>();
    return Response.json({
      days,
      rows: (rows.results || []).map((row) => ({
        provinceCode: row.province_code,
        province: row.province,
        tradeDate: row.trade_date,
        realTimeAvg: row.real_time_avg,
        dayAheadAvg: row.day_ahead_avg,
        pointCount: row.point_count,
        distribution: JSON.parse(row.distribution_json || "{}"),
        collectedAt: row.collected_at,
      })),
      latestSummary: latestSummary ? {
        ...latestSummary,
        ...JSON.parse(latestSummary.details_json || "{}"),
        details_json: undefined,
      } : null,
    });
  } catch (error) {
    return jsonError(error instanceof Error ? error.message : "读取数据失败", 500);
  }
}
