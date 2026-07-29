CREATE TABLE `daily_prices` (
	`owner_email` text NOT NULL,
	`province_code` text NOT NULL,
	`province` text NOT NULL,
	`trade_date` text NOT NULL,
	`real_time_avg` real,
	`day_ahead_avg` real,
	`point_count` integer DEFAULT 0 NOT NULL,
	`distribution_json` text DEFAULT '{}' NOT NULL,
	`collected_at` text NOT NULL,
	PRIMARY KEY(`owner_email`, `province_code`, `trade_date`)
);
--> statement-breakpoint
CREATE TABLE `settings` (
	`owner_email` text PRIMARY KEY NOT NULL,
	`encrypted_token` text,
	`encrypted_llm_key` text,
	`llm_provider` text DEFAULT 'deepseek' NOT NULL,
	`llm_base_url` text DEFAULT 'https://api.deepseek.com/v1' NOT NULL,
	`llm_model` text DEFAULT 'deepseek-chat' NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `summaries` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`owner_email` text NOT NULL,
	`start_date` text NOT NULL,
	`end_date` text NOT NULL,
	`model` text NOT NULL,
	`content` text NOT NULL,
	`created_at` text NOT NULL
);
