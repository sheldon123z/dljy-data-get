import { env } from "cloudflare:workers";

const SCHEMA_STATEMENTS = [
  `CREATE TABLE IF NOT EXISTS settings (
    owner_email TEXT PRIMARY KEY,
    encrypted_token TEXT,
    encrypted_llm_key TEXT,
    llm_provider TEXT NOT NULL DEFAULT 'deepseek',
    llm_base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com/v1',
    llm_model TEXT NOT NULL DEFAULT 'deepseek-chat',
    updated_at TEXT NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS daily_prices (
    owner_email TEXT NOT NULL,
    province_code TEXT NOT NULL,
    province TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    real_time_avg REAL,
    day_ahead_avg REAL,
    point_count INTEGER NOT NULL DEFAULT 0,
    distribution_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL,
    PRIMARY KEY (owner_email, province_code, trade_date)
  )`,
  `CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_email TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    model TEXT NOT NULL,
    content TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS daily_prices_owner_date_idx ON daily_prices(owner_email, trade_date)",
  "CREATE INDEX IF NOT EXISTS summaries_owner_created_idx ON summaries(owner_email, created_at)",
];

export async function ensureSchema() {
  for (const statement of SCHEMA_STATEMENTS) {
    await env.DB.prepare(statement).run();
  }
  const columns = await env.DB.prepare("PRAGMA table_info(summaries)").all<{ name: string }>();
  if (!(columns.results || []).some((column) => column.name === "details_json")) {
    await env.DB.prepare("ALTER TABLE summaries ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'").run();
  }
}

export function ownerEmail(request: Request) {
  const email = request.headers.get("oai-authenticated-user-email");
  if (email) return email;
  const host = new URL(request.url).hostname;
  if (host === "localhost" || host === "127.0.0.1") return "local-dev@localhost";
  throw new Error("需要通过受保护的 Sites 页面访问");
}

function toBase64(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function fromBase64(value: string) {
  const binary = atob(value);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function encryptionKey() {
  const configured = String(env.APP_ENCRYPTION_KEY || "");
  const material = configured || "local-development-key-change-before-deploy";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(material));
  return crypto.subtle.importKey("raw", digest, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

export async function encryptSecret(value: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    await encryptionKey(),
    new TextEncoder().encode(value),
  );
  return `${toBase64(iv)}.${toBase64(new Uint8Array(encrypted))}`;
}

export async function decryptSecret(value: string | null) {
  if (!value) return "";
  const [ivPart, dataPart] = value.split(".");
  if (!ivPart || !dataPart) return "";
  const decrypted = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: fromBase64(ivPart) },
    await encryptionKey(),
    fromBase64(dataPart),
  );
  return new TextDecoder().decode(decrypted);
}

export function jsonError(message: string, status = 400) {
  return Response.json({ error: message }, { status });
}

export function maskSecret(value: string) {
  if (!value) return "未设置";
  if (value.length <= 10) return "*".repeat(value.length);
  return `${value.slice(0, 4)}…${value.slice(-3)}`;
}
