# power-price MCP server

把本项目的电价数据仓（`data/raw/`）和采集能力，以 MCP 工具的形式暴露给 agent。

**为什么需要它**：消费方 agent（如 OpenClaw 里的钉钉机器人）跑在 Docker 沙箱里，
看不到宿主文件系统，也没有 python 和项目依赖。MCP server 由 gateway 在**宿主**启动，
agent 只通过 JSON-RPC 调用——「能查电价」和「碰不到你的电脑」于是可以同时成立。

- 文件：`mcp/power_price_mcp.py`（单文件，**纯标准库**，无第三方依赖）
- 协议：MCP stdio / JSON-RPC 2.0，`protocolVersion: 2024-11-05`
- **14 个工具**：11 个读本地数据仓 + 1 个实时接口直取（不落盘）+ 2 个会发请求并落盘

## 两条取数路径：实时查 vs 读数据仓

这是理解本 server 的关键，agent 人设里也要写清楚。

| | 读数据仓 | 实时接口直取 |
|---|---|---|
| 工具 | `get_daily_summary` / `get_price_trend` / `rank_spread` 等 11 个 | **`fetch_live_price`** |
| 数据来源 | 本地 CSV（`data/raw/`） | 直接 POST 电查查接口 |
| 新鲜度 | 取决于上次采集，**可能滞后几天** | 接口上有什么就是什么 |
| 速度 | 毫秒级（进程内索引） | 约 0.5–1s（一次网络往返） |
| 是否要令牌 | **不要** | **要**（失效则整个工具不可用） |
| 跨区域/跨多天分析 | ✅ 擅长 | ❌ 一次只能一个区域一天 |
| 是否落盘 | — | **不落盘**，查完就没了 |

**选择规则**：
- 问「最近趋势」「哪个省价差大」「峰谷规律」→ 读数据仓（快、能跨区域聚合）
- 问「今天/昨天/最新」的电价，或数据仓 `freshness` 显示滞后 → `fetch_live_price`
- 要把新数据**永久存进**数据仓，让后续所有分析都能用 → `sync_days`

9 个查询类工具（上表第一列那 9 个，不含 `get_data_status` / `get_weekly_report`）
返回里都带 **`freshness`** 字段，明确告诉调用方"我给你的是几号的数据"。
`fetch_live_price` 也带，但 `source` 是 `live-api`：

```jsonc
"freshness": {
  "data_last_date": "2026-07-31",
  "source": "warehouse",
  "days_behind_today": 2,
  "staleness_warning": "数据仓最新只到 2026-07-31，距今 2 天。要更新的数据请用 fetch_live_price 直接查接口，或先 sync_days 补采。"
}
```

`days_behind_today` ≥ 2 时才出 `staleness_warning`。**`freshness` 是按区域算的**——
同一时刻不同区域的最新日期可能不一样（有的省刚补过，有的没有）。

---

## 启动方式

```bash
python3 /Users/xiaodongzheng/exps/dljy-data-get/mcp/power_price_mcp.py
```

就这样，**不需要 venv、不需要装任何依赖、不需要环境变量**。

- **解释器**：只用标准库，兼容 **Python 3.9+**（已在 `/usr/bin/python3` 3.9.6 和
  miniforge 3.12.8 下实测）。gateway 是 launchd 服务、PATH 可能很精简，
  **建议在 `mcp add` 里写绝对路径 `/usr/bin/python3`**，别依赖 PATH 解析。
- **项目根**：默认从脚本自身位置推导（`mcp/` 的上一级），
  所以项目整体搬家也不用改代码。需要指到别处时用环境变量 `DLJY_DATA_GET_HOME`。
- **项目 `.venv` 与本 server 无关**：只有 `get_data_status` / `update_daily`
  这两个要 shell out 去跑 `run.py` 的工具会用到它，代码里已自动优先
  `<项目根>/.venv/bin/python`，你不用管。
- **cwd 无所谓**：所有路径都是绝对的。
- **stdout 只有协议报文**，日志一律走 stderr（`[power-price-mcp] ...`）。

### 数据前提

读数据仓的工具依赖以下文件存在（由本项目的采集流程生成，**不进 git**）：

| 文件 | 用途 |
|---|---|
| `data/raw/electricity_price_detail.csv` | 分时明细，39.5 万行，几乎所有工具的数据源 |
| `data/raw/province_summary.csv` | `list_regions` 的数据源 |
| `data/raw/metadata.json` | `get_data_status` 附带返回 |
| `data/exports/reports/*.md` | `get_weekly_report` 读最新一份 |

明细文件缺失时 server 仍能正常握手，只是数据类工具返回 `error`，
启动时会在 stderr 打一条 WARNING。

---

## 安装到 OpenClaw

```bash
OCN=/opt/homebrew/opt/node/bin/node
OCJ=/opt/homebrew/lib/node_modules/openclaw/dist/index.js

# 1) 注册
$OCN $OCJ mcp add power-price \
  --command /usr/bin/python3 \
  --arg /Users/xiaodongzheng/exps/dljy-data-get/mcp/power_price_mcp.py

# 2) 确认能列出工具（必须显示 14 tools）
$OCN $OCJ mcp probe power-price
#   -> power-price: 14 tools
```

> `openclaw` 必须用上面这个完整的 node 路径。PATH 里排第一的 node 版本不满足
> OpenClaw 的要求，直接敲 `openclaw` 会失败。

### ⚠️ 必须加沙箱白名单，否则 agent 根本看不见这些工具

`mcp probe` 通过**只代表 gateway 能启动 server**。如果目标 agent 开了沙箱
（`agents.list[].tools.sandbox`），工具默认**不会**暴露给它——agent 会表现得像
完全不知道有这些工具，然后开始凭印象编数字，非常难排查。

编辑 `~/.openclaw/openclaw.json`，找到目标 agent，往
`tools.sandbox.tools.alsoAllow` 数组里**追加**一条：

```jsonc
{
  "id": "<你的 agent id>",
  "sandbox": { "...": "不要动这块，这是安全隔离" },
  "tools": {
    "sandbox": {
      "tools": {
        "alsoAllow": [
          "tencent-meeting__*",
          "power-price__*"        // ← 追加这一条，不要覆盖已有条目
        ]
      }
    }
  }
}
```

注意事项：

- **追加，不要覆盖**——该数组通常已有其他 MCP 的条目。
- schema **禁止 `allow` 与 `alsoAllow` 并存**，只能用其中一个；已有 `alsoAllow` 就继续用它。
- **不要改 `sandbox` 块**（`mode` / `workspaceAccess` / `workspaceRoot` / `scope`），那是安全隔离。
- 改之前**先备份** `~/.openclaw/openclaw.json`。

改完必须验证并重启：

```bash
$OCN $OCJ config validate      # 必须输出 Config valid，不然立刻回滚
$OCN $OCJ gateway restart
```

### 验证真的接通了

不要只看 CLI 直调，要走 agent 实际链路问一句需要取数的话，然后**核对数字**：

```bash
# 从会话日志确认工具真被调用了，而不是模型嘴上说能查
grep -o 'power-price__[a-z_]*' ~/.openclaw/agents/<agent-id>/sessions/*.jsonl | sort -u
```

如果 agent 答得头头是道但上面 grep 不出东西，就是白名单没生效——回去检查 `alsoAllow`。

### 卸载

```bash
$OCN $OCJ mcp unset power-price
# 再从对应 agent 的 alsoAllow 里移除 "power-price__*"
$OCN $OCJ config validate && $OCN $OCJ gateway restart
```

---

## 数据口径（写进 agent 人设时要带上）

- **单位：元/MWh**（1000 元/MWh = 1 元/kWh）。
- **区域 29 个**，用这些原名（不是标准省名，注意「河北南网」「蒙东」「蒙西」）：

  > 上海、云南、吉林、四川、宁夏、安徽、山东、山西、广东、广西、新疆、江苏、江西、
  > 河北南网、河南、浙江、海南、湖北、湖南、甘肃、福建、蒙东、蒙西、贵州、辽宁、
  > 重庆、陕西、青海、黑龙江

- **日期区间：2026-01-28 起**（截至本文档编写时，多数区域到 2026-07-31，
  山西已补到 08-01）。**各区域的最新日期并不一致**，随采集滚动前移——
  以工具返回的 `freshness.data_last_date` 为准，**不要把日期写死进人设**。
- **日内粒度按区域不同**：96 点 16 个区域、24 点 11 个、48 点 1 个（浙江）、
  288 点 1 个（江西）。跨区域比分时数据时必须注意这一点。
- **蒙西、四川只有实时价，日前价为空**——这是接口口径，不是采集缺失。
- **最新一天的实时价常常还没回填**，此时 `real_time.count` 为 `0`。
  这表示「尚未回填」，**不是价格为零**。
- **缺失一律为 `null`，绝不按 0 计算**。`null` ≠ 0。
- **但 0 和负电价是真实价格**：零电价多出现在午间光伏大发时段，负电价意味着发电要倒贴钱。
  唯一例外是**实时接口对当日未出清时段返回 0**，`fetch_live_price` 已把它还原成 `null`（见上）。
- 这是**历史采集数据，不含未来价格**，不能用来回答「明天电价多少」。

### 🔴 0 与「无数据」的混淆（数据仓历史遗留，读取时已修正）

接口对**没有数据的时段返回数字 `0.0`**——不是 `null`、不是空串、不是缺字段
（实测 96 个点全是 float 类型）。采集脚本 `float()` 之后原样落盘，于是数据仓里
混着两种 0，从值本身**无法区分**：

| | 含义 | 必须 |
|---|---|---|
| 真实零电价 | 午间光伏过剩压到地板价，真实成交 | **保留** |
| 采集缺失 | 那天/那段价格压根没出来，被写成 0 | **按缺失处理** |

不处理的后果是**假数字**：实测 `get_daily_summary("黑龙江","2026-02-08")` 会返回
「均价 0.00、最低 0 元、峰谷 0」——整天 96 个点全是 0.00。这类区域日在
`quality.csv` 里全标 `available`，`run.py daily` 只补 `missing`/`failed`，
**永远不会重采**，所以数据仓自己好不了。

**本 server 在建索引时就把它们还原成 `null`**（两列各自独立判），用两条规则：

1. **尾部零段**：零段一直延伸到当日最后一个时点，且起点早于 20:00 →
   判为采集截断。真实零价由午间光伏驱动、在晚高峰前结束。
2. **双零点覆盖晚高峰**：同一时点日前与实时**同时**恰好 `0.0`，且当日这类点
   落进 19:00–21:00 → 判为采集截断。晚高峰是全天最贵最稀缺的时段，
   两侧同时恰好 0.00 不可能是市场结果。

**为什么规则 2 必须加「覆盖晚高峰」这个条件**：不少省份价格下限就是 0，
午间光伏过剩时日前和实时会一起打到地板价。实测 812 个区域日的双零点集中在
**10:45–13:00**（光伏大发窗口），是真实价格，必须保留；加了这个条件后
只命中 111 个区域日。

**实测影响**：共掩掉 **6683 个点 / 149 个区域日**（占全仓 1.5%）。

| 场景 | 修正前 | 修正后 |
|---|---|---|
| `get_daily_summary("黑龙江","2026-02-08")` | avg **0.00** / min 0 / 峰谷 0 | `count: 0` + `data_quality` 说明缺 96 点 |
| `list_regions` 黑龙江全期实时均价 | **252.53** | **294.48**（+16.6%） |
| `get_daily_summary("山西","2026-08-01")` | avg 348.52，午间 8 个真实零价 | **完全不变** ✓ |

### 规则三：残留全零列（2026-08-19 补）

前两条规则都带位置约束（尾部连续 / 覆盖晚高峰），抓不到**零点零散分布、
且多数点已被掩成 null** 的日子。于是某一列会残留几个孤立的假零，
算出 `avg: 0.00` —— 进而伪造出价差：

```
黑龙江 2026-08-07：96 点里 90 个已判缺失，残留 6 点的日前价全是 0.0
  → day_ahead_avg = 0.00，real_time_avg = 211.87
  → get_price_trend 报出 spread_avg = 211.87   ← 凭空捏造
```

判据：**某列剩下的非缺失点全是 0.0 就整列判缺失**。真实出清不可能整天零——
日前是前一天排好的完整曲线，实时的零价只出现在午间光伏窗口。
实测命中 11 个区域日（吉林 2026-08-10 残留 43 点全零最严重），
修正后全仓**不再有任何一列会算出 0.00 的均价**。

回归测试见 `tests/test_zero_masking.py`（含真实数据仓上的不变量断言）。

### `data_quality` 块

单日工具（`get_daily_summary` / `get_price_curve`）返回：

```jsonc
"data_quality": {
  "missing_points": 96,
  "reason": "这些时点在数据仓里是 0（接口对无数据时段返回数字 0），已判为采集缺失并按缺失处理，不计入均价/最值/峰谷/价差",
  "caveat": "这些时点是**没有数据**，不是价格为 0"
}
```

聚合工具（`get_price_trend` / `compare_regions` / `rank_spread` /
`get_peak_valley` / `get_hourly_profile` / `find_extremes`）返回区间版，
逐日列出哪几天不完整：

```jsonc
"data_quality": {
  "incomplete_days": 2,
  "missing_points_total": 152,
  "details": [
    {"province": "黑龙江", "date": "2026-08-07", "missing_points": 96},
    {"province": "黑龙江", "date": "2026-08-10", "missing_points": 56}
  ],
  "caveat": "……这些天的均价基于**剩余时点**计算，代表性弱于完整日，引用时要说明"
}
```

> 字段更名：`realtime_missing_points` → `missing_points`。
> 掩码从一开始就同时作用于日前和实时两列，旧名字是错的。

`list_regions` 的**价格聚合也从掩码后的索引重算**，不再直接用
`province_summary.csv`（那是 `build_outputs` 从原始明细算的，含被污染的 0）。
覆盖率/日期区间/粒度仍取汇总表。

### 直取路径也要过掩码（2026-08-19 补）

`fetch_live_price` 绕过数据仓直连接口，以前只做未出清掩码、不做假零掩码，
于是同一份脏数据换条路就漏出来了——查黑龙江 2026-08-07 会报
`day_ahead.avg = 0.00` 并算出凭空的价差。而这条路径正是回答
「今天/最新电价」用的，是最高频的场景。

现在两条路径共用 `_mask_fake_zeros`（原名 `_mask_stored_zero_tail`，
改名是因为它处理的是"接口把无数据返回成 0"的产物，与数据从哪读无关）。
顺序是 `_mask_uncleared` → `_mask_fake_zeros`，不能反：盘中未出清的点
接口也返回 0，必须先按未出清处理掉，否则会连累当天已出清的真实价格。

> ⚠️ **这只修了读取路径，数据仓文件本身没动。**
> 用 Excel / 周报 / 看板等其他出口读同一份数据，看到的仍是被 0 污染的值。
>
> 想真正把脏数据换掉，跑定向重采：
>
> ```bash
> python run.py collect --refresh-missing-prices
> ```
>
> `collect.py` 的 `missing_price_tasks` 从 2026-08-19 起也认「某列全零」
> （原先只认空字段，而假零看着是"有值"，`quality.csv` 照标 `available`，
> 增量采集永远跳过它们）。`get_data_status` 的 `masked_zero_quality` 块
> 会列出当前还有哪些区域日需要重采。

---

## 工具清单

| 工具 | 一句话作用 |
|---|---|
| `list_regions` | 列出全部 29 个区域及覆盖区间、全期均价与价差 |
| `get_price_curve` | 某区域某日的分时曲线（日前+实时） |
| `get_daily_summary` | 某区域某日的关键指标（日常问答首选） |
| `compare_regions` | 同一天多区域横向对比 |
| `get_price_trend` | 某区域最近 N 天的日度走势 |
| `find_extremes` | 扫描负电价 / 尖峰时点 |
| `rank_spread` | 某日全网各区域日前实时价差排行 |
| `get_peak_valley` | 日内峰谷差 / 峰谷比 |
| `get_hourly_profile` | 跨日同时刻价格分布（典型峰谷时段、波动最大时点） |
| **`fetch_live_price`** | 🌐 **绕过数据仓**直接查接口，取某区域某日最新电价（不落盘） |
| **`sync_days`** | ⚠️ 轻量增量采集：拉取指定范围、落盘、重建汇总 |
| `get_data_status` | 数据仓覆盖情况与采集令牌状态 |
| `update_daily` | ⚠️ 触发全量增量采集（走 `run.py daily`，耗时数分钟） |
| `get_weekly_report` | 读最新一份周报全文 |

标 🌐 的会发网络请求、标 ⚠️ 的会**消耗接口配额并写数据仓**，其余 11 个纯读本地文件。

下面所有返回样例都是**真实跑出来的**，不是手写的示意。

### `list_regions`

参数：无。

```jsonc
{
  "count": 29,
  "unit": "元/MWh",
  "regions": [
    {
      "province": "云南", "coverage": 1.0,
      "first_date": "2026-01-28", "last_date": "2026-07-31",
      "day_ahead_avg": 236.35, "real_time_avg": 279.97, "spread_avg": 43.62,
      "points_per_day": 24, "negative_realtime_points": 0
    }
    // ... 共 29 条，按区域名排序
  ],
  "note": "……部分区域接口只给实时价，日前为 null 是接口口径而非采集缺失……"
}
```

### `get_daily_summary`

参数：`province`(必填)、`date`(必填, `YYYY-MM-DD`)。

```jsonc
// get_daily_summary(province="山西", date="2026-07-31")
{
  "province": "山西", "date": "2026-07-31", "unit": "元/MWh",
  "point_count": 96,
  "day_ahead": { "count": 96, "avg": 338.97, "min": 260.0, "max": 448.0 },
  "real_time": { "count": 96, "avg": 344.64, "min": 286.67, "max": 402.87 },
  "spread_avg": 5.67,
  "peak":   { "time": "22:30", "real_time": 402.87 },
  "valley": { "time": "15:15", "real_time": 286.67 },
  "negative_realtime_points": 0,
  "note": "spread = 实时 - 日前，正值表示实时高于日前。……"
}
```

### `get_price_curve`

参数：`province`(必填)、`date`(必填)、`max_points`(选填，默认 96，上限 288)。

点数超过 `max_points` 时**等间隔抽稀**，但 `day_ahead` / `real_time` / `spread_avg`
这些统计量**始终按全量点计算**，不受抽稀影响。

```jsonc
// get_price_curve(province="江西", date="2026-07-30")  ← 江西是 288 点区域
{
  "province": "江西", "date": "2026-07-30", "unit": "元/MWh",
  "point_count": 288, "returned_points": 96,
  "downsampled": true, "downsample_step": 3,
  "day_ahead": { "count": 96, "avg": 364.59, "min": 207.0, "max": 524.0 },
  "real_time": { "...": "..." },
  "points": [ { "time": "00:05", "day_ahead": null, "real_time": 381.0 } /* ... */ ]
}
```

注意这个真实样例里首点的 `day_ahead` 是 `null`：江西实时价有 288 点、
日前价只有 96 点，**同一天里两条曲线的粒度可以不一样**，所以逐点比较时
必须跳过单边缺失的时点（`spread_avg` 内部就是这么算的）。

### `compare_regions`

参数：`provinces`(必填, 字符串数组)、`date`(必填)。按实时均价从高到低排序，
无实时价的区域排在末尾（`note` 里已说明这不代表价格低）。查不到的区域进 `missing`。

### `get_price_trend`

参数：`province`(必填)、`days`(选填, 默认 7)、`end_date`(选填)。

```jsonc
// get_price_trend(province="山西", days=7)
{
  "province": "山西", "unit": "元/MWh", "days": 7,
  "period": { "from": "2026-07-25", "to": "2026-07-31" },
  "overall_real_time_avg": 342.44,
  "series": [
    { "date": "2026-07-30", "day_ahead_avg": 342.1, "real_time_avg": 470.41,
      "real_time_max": 1500.0, "real_time_min": 263.53, "spread_avg": 128.31 }
    // ...
  ]
}
```

`days` 取的是「最近 N 个**有数据的**日期」，不是自然日回溯。

### `find_extremes`

参数：`province`(必填)、`kind`(`negative`|`spike`，默认 `negative`)、
`days`(默认 30)、`threshold`(spike 阈值，默认 1000)、`end_date`(选填)。

```jsonc
// find_extremes(province="山东", kind="negative", days=10)
{
  "province": "山东", "kind": "negative", "threshold": 0,
  "scanned_days": 10, "period": { "from": "2026-07-22", "to": "2026-07-31" },
  "hit_count": 5, "hit_days": 3,
  "hits_by_date": { "2026-07-23": 3, "2026-07-26": 1, "2026-07-31": 1 },
  "unit": "元/MWh", "truncated": false,
  "samples": [ { "date": "2026-07-26", "time": "10:00", "real_time": -59.66 } /* 最多 50 条 */ ]
}
```

### `rank_spread`

参数：`date`(必填)、`top`(选填，默认全部)。按 `spread_avg`（实时均价 − 日前均价）降序。

只有日前实时**都有值**的区域才进 `ranking`；只有单边的进 `incomplete`
（最新一两天大部分区域都会落在这里，因为实时价还没回填）。

```jsonc
// rank_spread(date="2026-07-30", top=3)
{
  "date": "2026-07-30", "unit": "元/MWh", "region_count": 11,
  "ranking": [
    { "province": "山西", "spread_avg": 128.31, "day_ahead_avg": 342.1,
      "real_time_avg": 470.41, "spread_max": 1078.5, "spread_min": -20.9 },
    { "province": "山东", "spread_avg": 54.35, "day_ahead_avg": 370.22,
      "real_time_avg": 424.57, "spread_max": 245.27, "spread_min": -41.45 },
    { "province": "宁夏", "spread_avg": 9.56, "day_ahead_avg": 97.62,
      "real_time_avg": 107.19, "spread_max": 138.5, "spread_min": -106.63 }
  ],
  "incomplete": [
    { "province": "四川", "day_ahead_avg": null, "real_time_avg": 278.51,
      "reason": "该日日前与实时没有同时可用的时点" }
    // ...
  ],
  "note": "……spread 显著为正说明日前报低了/实时偏紧……"
}
```

**这是「各区域自己的日前实时价差」横向比较，不是区域之间的电价差**——
省间受输电通道和交易机制约束，不能拿它当跨省套利信号。

### `get_peak_valley`

参数：`province`(必填)、`days`(默认 7)、`end_date`(选填)、
`field`(`real_time`|`day_ahead`，默认 `real_time`)。

```jsonc
// get_peak_valley(province="山西", days=2)
{
  "province": "山西", "field": "real_time", "unit": "元/MWh", "days": 2,
  "period": { "from": "2026-07-30", "to": "2026-07-31" },
  "avg_peak_valley_span": 676.34, "max_peak_valley_span": 1236.47,
  "daily": [
    { "date": "2026-07-30", "available": true,
      "peak_time": "19:00", "peak_price": 1500.0,
      "valley_time": "14:00", "valley_price": 263.53,
      "peak_valley_span": 1236.47, "peak_valley_ratio": 5.69 },
    { "date": "2026-07-31", "available": true,
      "peak_time": "22:30", "peak_price": 402.87,
      "valley_time": "15:15", "valley_price": 286.67,
      "peak_valley_span": 116.2, "peak_valley_ratio": 1.41 }
  ],
  "note": "peak_valley_span = 当日最高价 - 最低价，是日内套利的毛空间上限，尚未扣除效率损失与容量成本。……"
}
```

谷价 ≤ 0 时 `peak_valley_ratio` 给 `null`——负价时比值没有经济含义，
宁可不给也不给一个会被拿去说事的数。

### `get_hourly_profile`

参数：`province`(必填)、`days`(默认 14)、`end_date`(选填)、`field`(默认 `real_time`)。

把最近 N 天按**时点**聚合，看该区域的典型日内形态。

```jsonc
// get_hourly_profile(province="山东", days=14)
{
  "province": "山东", "field": "real_time", "unit": "元/MWh", "days": 14,
  "period": { "from": "2026-07-18", "to": "2026-07-31" },
  "typical_peak_slot":   { "time": "19:00", "avg": 494.58 },
  "typical_valley_slot": { "time": "10:00", "avg": 178.59 },
  "most_volatile_slot":  { "time": "10:00", "stdev": 183.89 },
  "profile": [
    { "time": "01:00", "samples": 14, "avg": 438.99,
      "p10": 398.44, "median": 435.01, "p90": 507.08,
      "min": 387.33, "max": 523.42, "stdev": 34.14 }
    // ... 每个时点一条
  ],
  "note": "……stdev 大的时点意味着日前报价在该时段最容易踩偏差考核。"
}
```

样本数 < 4 时不硬造分位数，`p10`/`p90` 退化为 `min`/`max`。

### `fetch_live_price` 🌐

参数：`province`(必填)、`date`(必填)、`max_points`(选填，默认 96，上限 288)。

**绕过本地数据仓**，直接 POST 电查查接口。这是"查最新"的核心工具。**不落盘**。

```jsonc
// fetch_live_price(province="山西", date="2026-08-01", max_points=6)
// 跑这条的时候本地数据仓最新只到 2026-07-31，所以这一天数据仓里根本没有
{
  "province": "山西", "date": "2026-08-01", "unit": "元/MWh",
  "source": "live-api",
  "fetched_at": "2026-08-02T02:05:11+08:00",
  "elapsed_ms": 512,
  "point_count": 96, "returned_points": 6,
  "downsampled": true, "downsample_step": 16,
  "day_ahead": { "count": 96, "avg": 353.52, "min": 241.43, "max": 529.62 },
  "real_time": { "count": 96, "avg": 348.52, "min": 0.0, "max": 1288.29 },
  "spread_avg": -5.0,
  "peak":   { "time": "20:45", "real_time": 1288.29 },
  "valley": { "time": "10:00", "real_time": 0.0 },
  "negative_realtime_points": 0,
  "in_warehouse": false,          // ← 数据仓里没有这天，实时查才拿得到
  "points": [
    { "time": "00:15", "day_ahead": 398.0, "real_time": 391.0 },
    { "time": "12:15", "day_ahead": 252.8, "real_time": 23.29 },
    { "time": "20:15", "day_ahead": 517.48, "real_time": 620.97 }
    // ...
  ],
  "note": "本条是**实时接口直取**，不经过本地数据仓，也没有落盘。……"
}
```

`source: "live-api"` 和 `in_warehouse` 是给 agent 的判据：前者说明这不是数据仓的数，
后者说明本地有没有存过。查**历史日期**时还会返回 `freshness`（`source: "live-api"`），
字段名与读数据仓的工具一致，agent 用同一条纪律即可。

#### ⚠️ 查当天：未出清时段会被剔除，不能当全天

实时市场**当日滚动出清**，接口对尚未出清的时段返回 **`0.0` 而不是 `null`**。
原样收下会算出彻底假的数字——实测凌晨 2 点查当天，真实只出清了 7 个点，
但 96 个点参与平均，`avg` 变成 **28.1 元/MWh**，`min` 变成 0（会被读成"今天出现过零电价"）。
这种"看起来合理、实际离谱"的数字拿去做交易判断是要出事的。

本工具会把未出清时段还原成 `null`，不计入任何统计：

```jsonc
// fetch_live_price(province="山西", date="2026-08-02")，跑的时候是 02:21
{
  "real_time": { "count": 7, "avg": 385.43, "min": 373.0, "max": 402.0,
                 "cleared_until": "01:45" },     // ← 只统计已出清的 7 个点
  "day_ahead": { "count": 96, "avg": 336.04, "min": 289.18, "max": 381.29 },  // 日前是全天完整的
  "spread_avg": 24.57,
  "uncleared_points": 89,
  "realtime_complete": false,
  "freshness": { "source": "live-api", "data_last_date": "2026-08-02",
                 "fetched_at": "2026-08-02T02:21:37+08:00",
                 "cleared_until": "01:45", "realtime_complete": false },
  "points": [ { "time": "12:00", "day_ahead": 289.18, "real_time": null } /* ... */ ],
  "note": "……⚠️ 当日实时市场滚动出清，01:45 之后的 89 个时点尚未出清……real_time 的均价/最值/峰谷只基于已出清的 7 个点，**不能代表全天**。日前价是当日完整的，可以正常讲。"
}
```

**agent 必须看 `realtime_complete`**：为 `false` 时，实时均价/最值/峰谷**只代表到
`cleared_until` 为止的部分**，绝不能说成"今天的电价是 X"。日前价当天是完整可信的，正常讲。

**判别逻辑（为什么不能简单地把 0 当缺失）**：零电价和负电价在这个市场里是**真实存在**的——
山西历史上有 2590 个零点，2026-08-01 的 8 个零点全在 10:00–11:45，是午间光伏大发压出来的
真实成交价。一刀切会把真实数据抹掉。所以用的是：

1. **只对当日及以后生效**——历史日期数据已终局，里面的 0 和负价原样保留（实测查
   08-01 得到 `uncleared_points: 0`、96 个点全在、8 个真实零价一个不少，
   且 `avg 348.52` 与数据仓存的值完全一致）。
2. **时间还没到的时点**一定没出清 → `null`。
3. **出清有发布延迟**（实测 02:18 只出清到 01:45），所以时间已过但仍为 0 的
   **尾部连续段**也判为未出清；只从尾部连续地判，中间夹着的 0 保留。
4. 规则 3 卡了 **6 小时回溯上限**（`CLEARING_LAG_CAP`）——出清延迟是分钟到小时级，
   没有这个上限，尾部扫描会一路穿回去把当天早些时候的真实零价也吃掉。

**错误分三类**，`error_kind` 可区分：

```jsonc
// 令牌失效 —— 需要人工介入，重试无用
{ "error": "鉴权失败 HTTP 401，令牌已失效。请在宿主机跑 `python run.py sniff`（自动抓）或 `python run.py token`（手工粘贴）更新。",
  "error_kind": "token", "actionable_by": "管理员（宿主机）" }

// 网络/接口错误 —— 内部已重试 2 次并退避，可稍后再试
{ "error": "接口请求失败：接口限流 HTTP 429，请降低调用频率后重试",
  "error_kind": "network_or_api", "hint": "可稍后重试；持续失败用 get_data_status 看令牌状态" }

// 该日无数据（真实返回，问的是 2026-12-25）
{ "province": "山西", "date": "2026-12-25", "source": "live-api",
  "point_count": 0, "in_warehouse": false,
  "error": "接口对 山西 2026-12-25 返回空数据",
  "hint": "该日可能尚未出价（日前价通常 D-1 出、实时价 D+1 才补全），或该区域当日无交易" }
```

### `sync_days` ⚠️

参数：`province`(选填，省略则全部 29 个)、`days`(选填，默认 1)、`end_date`(选填，默认昨天)。

轻量增量：拉取 → 落盘进 `electricity_price_detail.csv` + `quality.csv` →
重建 CSV 汇总 → 让内存索引失效。**不做 JSON/Excel/周报导出**，所以比 `update_daily` 快得多
（实测单省单天 **4.6s**，`run.py daily` 是数分钟）。

```jsonc
// sync_days(province="山西", days=1, end_date="2026-08-01")
{
  "requested": { "province": "山西", "days": 1,
                 "window": { "from": "2026-08-01", "to": "2026-08-01" } },
  "planned_requests": 1,
  "succeeded": 1, "failed": 0, "new_points": 96,
  "summary_rebuilt": true,
  "warehouse_last_date": "2026-08-01",
  "detail": [ { "province": "山西", "date": "2026-08-01", "status": "available", "points": 96 } ],
  "coverage": 0.9946, "detail_rows": 395160,
  "note": "只补采并重建 CSV 汇总，不做 JSON/Excel/周报导出（那些要管理员跑 run.py export/weekly）。"
}
```

**拒绝写当日数据**（`end_date` 默认就是昨天）。当日实时市场没出清完，落盘后
`build_outputs` 见到有点数就会把该区域日标成 `available`，而 `run.py daily`
（`collect --last-days 3`，不带 `--refresh-days`）只补 `missing`/`failed` —— 
于是残缺数据会被**永久钉死**在数据仓里。要看当天用 `fetch_live_price`（不落盘）：

```jsonc
// sync_days(province="山西", end_date="2026-08-02")  ← 今天
{ "error": "拒绝同步当日及以后的数据（请求到 2026-08-02，今天是 2026-08-02）",
  "reason": "当日实时市场尚未出清完，落盘会被标成 available 并被后续增量采集跳过，残缺数据将永久留在数据仓里",
  "hint": "想看当天用 fetch_live_price（不落盘）；要补采请把 end_date 设成 2026-08-01 或更早" }
```

**配额闸**：单次最多 `SYNC_MAX_REQUESTS = 60` 个请求（= 区域数 × 天数），
超了直接拒绝、**一个请求都不发**：

```jsonc
// sync_days(days=30)  ← 29 个区域 × 30 天
{ "error": "本次要发 870 个请求，超过上限 60",
  "hint": "缩小范围：指定 province，或把 days 降到 2 以内",
  "planned_requests": 870 }
```

**幂等**：重复同步同一天不会产生重复行。落盘是追加写，但收尾的汇总重建会按
`(区域, 日期, 时点)` 去重并重写明细文件（后写入的覆盖先写入的），
这和 `scripts/collect.py` 是同一套语义。实测重跑一次，总行数不变、重复键 0。

**一个要知道的副作用**：只同步单个区域的某个新日期，会把数据仓的全局日期区间
推到那天，而其余 28 个区域在那天没有数据 → 会被记为 `missing`，
于是 `coverage` 会下降（实测 0.9998 → 0.9946）。这不是数据损坏，
是覆盖率分母变大了；把其他区域也补上就会回升。`collect.py` 行为一致。

### `get_data_status`

参数：无。跑 `run.py status` 并附带 `data/raw/metadata.json`。

```jsonc
{
  "command_output": {
    "ok": true, "exit_code": 0,
    "stdout": "数据区间   2026-01-28 ~ 2026-07-31（185 天）\n价格区域   29\n分时点数   395,064\n……\n令牌       <token-redacted>（共 140 字符）｜签发于 2026-07-28 11:49（已用 109.5 小时）\n",
    "stderr": ""
  },
  "metadata": { "coverage": 0.9998, "detail_rows": 395064, "price_unit": "元/MWh", "...": "..." }
}
```

**令牌已脱敏**：`run.py status` 会打印 Authorization 的前后缀，
而这条链路的终点是云端模型和聊天群，所以输出里所有 `eyJ...` 形态的串
都被替换成 `<token-redacted>`。

### `update_daily`

参数：无。**会真的发起采集**（补采最近 3 天 + 重新导出），耗时数分钟、消耗接口配额。
只在用户明确要求更新数据时调用。工具内超时 1500s，采完会清掉进程内索引缓存。

### `get_weekly_report`

参数：无。返回 `data/exports/reports/` 里最新一份周报 Markdown 全文（截断到 12000 字符）。

---

## 错误约定

工具永远不抛异常打死 server，出错时返回 `{"error": "...", "hint": "..."}`，
并且 `tools/call` 的 result 里 `isError: true`。

```jsonc
// get_price_trend(province="不存在省")
{ "error": "没有 不存在省 的数据", "hint": "用 list_regions 看可用区域" }

// rank_spread(date="2030-01-01")
{ "error": "没有 2030-01-01 的数据", "hint": "用 get_data_status 或 list_regions 确认数据截止日" }
```

---

## 手工调试

不装进 OpenClaw 也能直接喂 JSON-RPC 测：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_daily_summary","arguments":{"province":"山西","date":"2026-07-31"}}}' \
| /usr/bin/python3 /Users/xiaodongzheng/exps/dljy-data-get/mcp/power_price_mcp.py
```

stderr 上的 `[power-price-mcp] ...` 是正常日志，不影响协议。

---

## 实测状态与已知问题

**实测通过**（真实省份、真实日期，逐个核对返回值）：

- 11 个读数据仓的工具全部通过，外加 4 条错误路径
- **`fetch_live_price` 真打通了接口**：拿到 山西 2026-08-01 共 96 点，
  而当时数据仓最新只到 07-31（`in_warehouse: false`），
  返回值与直接 curl 接口的结果逐点一致
- **未出清剔除逻辑实测**：02:21 查当天，`avg` 从错误的 28.1 修正为 **385.43**
  （只算已出清的 7 个点），`min` 从 0.0 修正为 373.0；
  同一逻辑查历史日 08-01 得 `uncleared_points: 0`、96 点全在、
  8 个午间真实零价一个不少，且 `avg 348.52` 与数据仓存的值**完全一致**（交叉验证）。
  另有 6 个边界场景的离线单测（历史日不动 / 凌晨滚动出清 / 午间真实零价保留 /
  超出延迟上限的零价保留 / 24:00 跨日 / 当天负电价保留）全部通过
- **`sync_days` 真落盘了**：单省单天 4.6s，明细 395064 → 395160 行（+96，一条不多），
  全表重复键 0，BOM 与字段顺序与采集脚本一致；重跑一次仍是 0 重复
- 配额闸、未知区域、缺参数、日期格式错 4 条错误路径均**零请求**拒绝
- `initialize` / `ping` / `tools/list` / `resources/list` / `prompts/list` 均正常
- 在 `/usr/bin/python3`(3.9.6) 与 miniforge python(3.12.8) 下都跑过

**令牌状态**（截至 2026-08-02 02:10）：有效，签发于 2026-07-28 11:49，已用约 110 小时。
该接口的 JWT **只带 `iat` 不带 `exp`**，有效期由服务端决定，**无法预测何时失效**。
失效后 `fetch_live_price` / `sync_days` 会返回 `error_kind: "token"`，
需要管理员在宿主机跑 `python run.py sniff` 或 `python run.py token` 更新。

**已知问题**：

1. **`update_daily` 从未实测**——它会真的发起全量采集，测试时刻意避开了。
   已知风险：工具内超时 1500s，但 gateway/agent 侧自己的工具超时上限可能更短，
   那样 agent 会先看到超时错误而采集仍在后台跑。
   **新增 `sync_days` 后它基本可以不用了**，保留只为兼容。
2. **`sync_days` 的 29 区域全量路径未实测**——实测只跑了单省单天（配额纪律）。
   全量单天 = 29 个请求，在 60 的闸内，但串行 + 0.2s 延时，预计 30–60s，
   可能超过 agent 侧的工具超时。建议按区域分批调。
3. **未出清判别的规则 3（尾部零段）是启发式**，不是接口给的确定信号。
   已用 6 小时回溯上限兜底，但极端情况下仍可能多剔除一个真实的零价尾点——
   这是**保守方向的误差**（少算一个真实点，而不是多算几十个假零点）。
   接口若将来提供出清状态字段，应改用该字段判别。
4. **时区按本机本地时间判**（数据是中国电力市场，本机在 +08:00）。
   若把这个 server 跑在非 +08:00 的机器上，当日未出清的判别会偏。
5. **`sync_days` 会拉低 `coverage`**（见上），这是分母变大而非数据损坏。
6. **令牌脱敏是正则兜底**，只认 `eyJ` 开头的 JWT。若将来 `run.py` 改成打印
   其它形态的凭据，脱敏规则要跟着改。注意 `fetch_live_price` / `sync_days`
   的错误信息里不含令牌本身，只提示去哪更新。
7. **首次数据类调用有约 1s 冷启动**（建 39.5 万行索引），之后进程内缓存；
   常驻内存约 **113 MB**（去重索引比原来多约 20 MB）。gateway 重启后重付一次。
8. **索引缓存只在 `sync_days` / `update_daily` 之后自动失效**。如果数据仓被
   **外部流程**更新（launchd 定时采集、手工跑 `run.py weekly`），
   已启动的 server 进程仍读旧索引，需要 `gateway restart` 才能看到新数据。
   注意：`freshness` 读的也是内存索引，所以这种情况下它会**低报**新鲜度。
9. **未做并发压测**：单进程串行处理 stdio，多个 agent 会话同时打进来时的表现未验证。
   `sync_days` 并发调用尤其危险（同时追加写同一个 CSV），目前没有加锁。
