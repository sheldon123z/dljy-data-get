import { env } from "cloudflare:workers";
import {
  decryptSecret,
  encryptSecret,
  ensureSchema,
  jsonError,
  maskSecret,
  ownerEmail,
} from "../../../lib/server";

export const runtime = "edge";

type SettingsRow = {
  encrypted_token: string | null;
  encrypted_llm_key: string | null;
  llm_provider: string;
  llm_base_url: string;
  llm_model: string;
};

const PROVIDERS: Record<string, { baseUrl: string; model: string }> = {
  deepseek: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  glm: { baseUrl: "https://open.bigmodel.cn/api/paas/v4", model: "glm-5.2" },
  custom: { baseUrl: "", model: "" },
};

async function readSettings(email: string) {
  return env.DB.prepare(
    `SELECT encrypted_token, encrypted_llm_key, llm_provider, llm_base_url, llm_model
     FROM settings WHERE owner_email = ?`,
  ).bind(email).first<SettingsRow>();
}

export async function GET(request: Request) {
  try {
    await ensureSchema();
    const email = ownerEmail(request);
    const row = await readSettings(email);
    const token = row ? await decryptSecret(row.encrypted_token) : "";
    const key = row ? await decryptSecret(row.encrypted_llm_key) : "";
    return Response.json({
      tokenSet: Boolean(token),
      tokenMasked: maskSecret(token),
      llmKeySet: Boolean(key),
      llmKeyMasked: maskSecret(key),
      provider: row?.llm_provider || "deepseek",
      baseUrl: row?.llm_base_url || PROVIDERS.deepseek.baseUrl,
      model: row?.llm_model || PROVIDERS.deepseek.model,
    });
  } catch (error) {
    return jsonError(error instanceof Error ? error.message : "读取配置失败", 500);
  }
}

export async function POST(request: Request) {
  try {
    await ensureSchema();
    const email = ownerEmail(request);
    const body = (await request.json()) as Record<string, unknown>;
    const current = await readSettings(email);
    const provider = String(body.provider || current?.llm_provider || "deepseek").trim();
    if (!PROVIDERS[provider]) return jsonError("不支持的模型服务商");

    const baseUrl = String(
      body.baseUrl ?? current?.llm_base_url ?? PROVIDERS[provider].baseUrl,
    ).trim().replace(/\/+$/, "");
    const model = String(body.model ?? current?.llm_model ?? PROVIDERS[provider].model).trim();
    if (!/^https:\/\//i.test(baseUrl)) return jsonError("模型接口地址必须使用 HTTPS");
    if (!model || model.length > 120) return jsonError("模型名称无效");

    let encryptedToken = current?.encrypted_token || null;
    let encryptedLlmKey = current?.encrypted_llm_key || null;
    const token = String(body.token || "").trim();
    const llmKey = String(body.llmKey || "").trim();
    if (token) encryptedToken = await encryptSecret(token);
    if (llmKey) encryptedLlmKey = await encryptSecret(llmKey);

    await env.DB.prepare(
      `INSERT INTO settings
       (owner_email, encrypted_token, encrypted_llm_key, llm_provider, llm_base_url, llm_model, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(owner_email) DO UPDATE SET
         encrypted_token = excluded.encrypted_token,
         encrypted_llm_key = excluded.encrypted_llm_key,
         llm_provider = excluded.llm_provider,
         llm_base_url = excluded.llm_base_url,
         llm_model = excluded.llm_model,
         updated_at = excluded.updated_at`,
    ).bind(
      email,
      encryptedToken,
      encryptedLlmKey,
      provider,
      baseUrl,
      model,
      new Date().toISOString(),
    ).run();
    return GET(request);
  } catch (error) {
    return jsonError(error instanceof Error ? error.message : "保存配置失败", 500);
  }
}
