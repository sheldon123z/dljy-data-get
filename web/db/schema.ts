import { integer, primaryKey, real, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const settings = sqliteTable("settings", {
  ownerEmail: text("owner_email").primaryKey(),
  encryptedToken: text("encrypted_token"),
  encryptedLlmKey: text("encrypted_llm_key"),
  llmProvider: text("llm_provider").notNull().default("deepseek"),
  llmBaseUrl: text("llm_base_url").notNull().default("https://api.deepseek.com/v1"),
  llmModel: text("llm_model").notNull().default("deepseek-chat"),
  updatedAt: text("updated_at").notNull(),
});

export const dailyPrices = sqliteTable(
  "daily_prices",
  {
    ownerEmail: text("owner_email").notNull(),
    provinceCode: text("province_code").notNull(),
    province: text("province").notNull(),
    tradeDate: text("trade_date").notNull(),
    realTimeAvg: real("real_time_avg"),
    dayAheadAvg: real("day_ahead_avg"),
    pointCount: integer("point_count").notNull().default(0),
    distributionJson: text("distribution_json").notNull().default("{}"),
    collectedAt: text("collected_at").notNull(),
  },
  (table) => [primaryKey({ columns: [table.ownerEmail, table.provinceCode, table.tradeDate] })],
);

export const summaries = sqliteTable("summaries", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  ownerEmail: text("owner_email").notNull(),
  startDate: text("start_date").notNull(),
  endDate: text("end_date").notNull(),
  model: text("model").notNull(),
  content: text("content").notNull(),
  detailsJson: text("details_json").notNull().default("{}"),
  createdAt: text("created_at").notNull(),
});
