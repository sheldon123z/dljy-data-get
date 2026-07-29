import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("build contains the electricity market workbench and no starter preview", async () => {
  const [page, dashboard, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/DashboardClient.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  await access(new URL("../dist/server/index.js", import.meta.url));
  assert.match(page, /<DashboardClient \/>/);
  assert.match(layout, /电力现货价格工作台/);
  assert.match(dashboard, /采集并更新看板/);
  assert.match(dashboard, /各地区实时价格区间占比/);
  assert.match(dashboard, /导出 PNG/);
  assert.match(dashboard, /多 Agent 数据审校与周期总结/);
  assert.match(dashboard, /数据采集 Authorization/);
  assert.match(dashboard, /严格七 Agent/);
  assert.match(dashboard, /可靠性评分/);
  assert.doesNotMatch(`${page}${dashboard}${layout}${packageJson}`, /codex-preview|react-loading-skeleton/i);
});
