# 全国现货电价采集工具

从电查查小程序接口按「价格区域 × 自然日」采集日前、实时电价，保留接口原生的
24/48/96/288 等日内粒度，并输出 CSV 数据仓、JSON、Excel、周报和一个可交互的 HTML 看板。

支持断点续采、每日定时增量、本地控制台一键操作。

网站控制台支持自定义采集天数、强制重采范围、并发数、自动重建看板，以及通过
DeepSeek、智谱 GLM 或其他 OpenAI 兼容接口生成数据总结。电查查 Token 和模型 API Key
只保存在后端 `.env`，不会进入看板、CSV、JSON、Excel 或浏览器脚本。

## 目录结构

```text
dljy-data-get/
├── run.py                        # 统一入口，日常只需要用它
├── .env                          # 令牌（自动创建，权限 600，已被 .gitignore 忽略）
├── data/
│   ├── province_codes.csv        # 29 个价格区域代码
│   ├── raw/                      # ★ 数据仓：明细 + 质量表 + 汇总 + metadata
│   ├── exports/
│   │   ├── json/                 # JSON / JSONL 产物
│   │   ├── excel/                # 汇总工作簿 + 按月明细
│   │   ├── reports/              # 周报 Markdown / HTML / JSON
│   │   └── 看板.html             # 自包含看板，双击即可打开
│   ├── archive/                  # 历史 Excel 归档（导入数据仓的来源）
│   └── logs/                     # 定时任务日志
├── scripts/
│   ├── config.py                 # 路径约定、令牌读写
│   ├── common.py                 # CSV 读写、汇总重建
│   ├── collect.py                # 采集 / 续采
│   ├── import_excel.py           # 从历史 Excel 反向建仓
│   ├── merge.py                  # 合并多个采集目录
│   ├── export_json.py            # 导出 JSON / JSONL
│   ├── export_excel.py           # 导出 Excel
│   ├── weekly_report.py          # 生成周报
│   ├── dashboard.py              # 生成自包含 HTML 看板
│   ├── serve.py                  # 本地控制台（令牌输入 + 一键采集）
│   └── templates/dashboard.html  # 看板模板
└── automation/
    ├── daily_update.sh           # 每日采集脚本
    └── install_daily.sh          # 安装 / 卸载 launchd 定时任务
```

## 快速上手

```bash
pip install -r requirements.txt
python run.py token        # 录入 Authorization（输入不回显）
python run.py serve        # 打开本地控制台，之后都可以点按钮操作
```

不懂程序的 Windows 用户优先使用 `dist-windows/电力现货价格工作台-Windows便携版.zip`：
完整解压后双击 `电力现货价格工作台.exe`，不需要安装 Python、Node.js 或 Excel。
开发环境也可以直接双击 `启动网站.bat`。

如果双击 EXE 没有反应，且 Windows 安全中心已开启 Smart App Control，请改用
`电力现货价格工作台-SmartAppControl兼容版.zip`，完整解压后双击 `启动工作台.cmd`。
该版本使用 Python.org 官方签名运行时，不需要关闭系统安全保护。

`run.py serve` 会在终端打印一个带一次性密钥的地址并自动打开浏览器。页面顶部就是
令牌输入框和「补采所有缺口 / 采集最近 3 天 / 导出 JSON / 导出 Excel / 生成周报」等按钮，
下方首图是**多天同一时点均价曲线**：可选最近 7 / 14 / 30 个交易日或按月，
横轴统一为 96 个 15 分钟时点，可多选或一键全选省份对比实时或日前价格。图表可导出 PNG，
Excel 导出首个 Sheet 为全国分时均价总览，后续每个 Sheet 对应一个区域的分时均价和有效样本数；另有区域排行、价格区间分布、
覆盖热力图和可排序的区域汇总表。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `python run.py status` | 数据区间、覆盖率、令牌状态（含签发时间与已用时长） |
| `python run.py weekly` | **每周一次的完整刷新**：补采 + 全部导出 + 周报 + 看板 |
| `python run.py token` | 交互式更新 Authorization（写入 `.env`，权限 600） |
| `python run.py sniff` | 用本地代理自动抓取 Authorization，免开抓包 GUI |
| `python run.py import-excel` | 从 `data/archive/` 的历史 Excel 反向建立数据仓 |
| `python run.py collect [参数]` | 采集 / 续采，参数透传给 `scripts/collect.py` |
| `python run.py collect --refresh-missing-prices` | 仅重采日前价或实时价字段为空的区域日 |
| `python run.py backfill` | 补齐区间内所有缺口，然后导出 JSON |
| `python run.py daily` | 每日增量：采最近 3 天 + 导出 + 重建看板 |
| `python run.py export` | 导出 JSON + Excel + 地区/月/周分层 Excel |
| `python run.py export-tree` | 只重建分层 Excel（增量，未变的周跳过） |
| `python run.py report [--week 2026-W30]` | 生成周报 |
| `python run.py ai-summary --days 7 --agent-mode standard` | 使用多 Agent 协作生成最近 N 天总结 |
| `python run.py dashboard` | 生成自包含 HTML 看板 |
| `python run.py artifact` | 生成可发布到 claude.ai 的 Artifact 页面 |
| `python run.py serve` | 启动本地控制台 |
| `python run.py reset-failed` | 把 `failed` 记录退回待采队列（令牌过期后常用） |
| `python run.py all` | 全量补采 + 全部产物 |
| `python run.py merge --inputs A B` | 合并多个采集目录 |

## 1. 令牌

退出并重新登录小程序即可取得新的 Authorization。三种设置方式，优先级从高到低：

```bash
export ELECHECK_TOKEN='你的完整Authorization值'   # 1. 环境变量
python run.py token                                # 2. 交互式写入 .env
# 3. 在本地控制台页面的输入框里粘贴保存
```

脚本把该值原样放进 `Authorization` 请求头；抓包里若带 `Bearer ` 前缀就一并保留。
令牌只存在于 `.env`（权限 600）或进程环境变量中，不会写进任何 CSV、JSON、Excel。

令牌是 JWT，但只带 `iat` 不带 `exp`——有效期由服务端决定，客户端无法预知失效时间，
`run.py status` 因此只报告「签发于什么时候、已经用了多久」。

### 用命令行抓令牌，免开抓包 GUI

```bash
pip install mitmproxy        # 一次性
mitmdump                     # 跑一次生成根证书，Ctrl+C 退出
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain ~/.mitmproxy/mitmproxy-ca-cert.pem
```

之后每次换令牌只要：

```bash
python run.py sniff
```

它会起一个本地 mitmproxy、把系统代理临时指过去，你在微信里打开电查查小程序刷新一次
电价页面，令牌就会被自动写入 `.env`，然后脚本关代理、退出。加 `--manual` 可以不碰
系统网络设置，自己去偏好设置里填 `127.0.0.1:8080`。

Proxyman 的 CLI 主要用来控制它自己的 GUI，做不到无头抓取，所以这里选了 mitmproxy。

## 2. 采集

```bash
python run.py collect                        # 补齐仓库区间内的全部缺口
python run.py collect --start 2026-01-28 --end 2026-07-27
python run.py collect --last-days 7          # 只看最近 7 天
python run.py collect --refresh-days 3       # 强制重采最近 3 天（接口事后补录时用）
python run.py collect --only-provinces 云南 贵州 --dry-run
```

省略 `--start` 时沿用数据仓已有区间，省略 `--end` 时取昨天。反复执行同一条命令是安全的：
`available` / `empty` 会被跳过，只补 `missing` 和 `failed`。建议并发保持 1–4。

常用参数：`--workers` 并发数、`--retries` 重试次数、`--delay` 请求间隔、`--limit` 本次上限、
`--dry-run` 只列待采任务、`--skip-failed` 本次跳过历史失败项。

## 3. 每日自动采集

macOS（launchd）：

```bash
./automation/install_daily.sh                      # 默认每天 09:30
DLJY_HOUR=7 DLJY_MINUTE=0 ./automation/install_daily.sh
./automation/install_daily.sh uninstall
```

Linux（cron）：

```cron
30 9 * * * /bin/bash /path/to/dljy-data-get/automation/daily_update.sh
```

每次执行会回溯 `DLJY_LOOKBACK`（默认 4）天补采，然后重新导出 JSON、Excel 和看板；
每周一额外生成上一周的周报。日志写在 `data/logs/daily_YYYYMMDD.log`，保留 60 天。
若令牌缺失或过期，脚本会记录并以非零码退出，不会破坏已有数据。

## 4. 网站与大模型配置

网站控制台的大模型区域提供三个预设：

- DeepSeek：`https://api.deepseek.com/v1`，默认模型 `deepseek-chat`
- 智谱 GLM：`https://open.bigmodel.cn/api/paas/v4`，模型名称可填写 `glm-5.2` 或账号实际可用模型
- 自定义：填写任意支持 `POST /chat/completions` 的 OpenAI 兼容 Base URL 与模型名称

保存配置后点击“生成所选天数总结”。系统先用本地数据计算全国均价、区域变化、价格区间、
极值和数据完整度，再由多个分工明确的 Agent 并行分析，最后经过独立审校和主编汇总。
提示词要求模型使用 `[E01]` 一类证据编号，且不得虚构供需、新能源出力等无法从价格数据
直接验证的因果关系。产物写入 `data/exports/reports/AI总结_*.{md,html,json}`。

Agent 协作模式：

- 快速双 Agent：趋势分析 + 主编，模型调用少，适合日常速览。
- 标准五 Agent（推荐）：趋势、区域、价格分布 + 独立审校 + 主编。
- 严格七 Agent：再加入质疑者与决策 Agent，适合重要汇报；调用次数和费用相应增加。

每份总结都会附带确定性生成的可靠性等级、覆盖率、分时时点数、数据问题和证据台账。
即使模型遗漏证据编号，该附录也不会被模型改写。可以在网页的“特别关注问题”中指定
比较区域、异常价格区间或汇报重点，也可以使用命令行：

```bash
python run.py ai-summary --days 7 --agent-mode rigorous \
  --focus "重点比较西北与华东，并检查400元/MWh以上时段"
```

也可以复制 `.env.example` 为 `.env` 后手工填写：

```env
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的密钥
LLM_MODEL=deepseek-chat
```

### Docker 运行

```bash
copy .env.example .env
docker compose up --build
```

启动日志会打印带一次性会话密钥的网址。Docker 端口仅绑定到本机
`127.0.0.1:8787`，`data/` 和 `.env` 使用宿主机持久化。

### Windows 一键应用

已经生成便携版时，最终用户的操作只有：

1. 完整解压 `电力现货价格工作台-Windows便携版.zip`。
2. 双击 `电力现货价格工作台.exe`；启动窗口会显示服务状态，并自动打开默认浏览器。
   如果浏览器没有自动出现，点击启动窗口里的“打开工作台”。
3. 首次采集时在“① 数据采集 Authorization”粘贴完整值；有 `Bearer ` 前缀时一并保留。
4. 需要 AI 总结时在“② 模型 API Key”填写对应平台密钥并选择 Agent 模式。
5. 使用完毕点击启动窗口中的“退出应用”，或点击网页中的“退出 Windows 应用 / 本地服务”。

便携包内已经包含 Python 解释器、运行库、项目代码和当前数据。请保留整个目录，不要单独
移动 EXE。开发者重新打包只需在 PowerShell 运行：

```powershell
.\build_windows.ps1
.\build_smartapp_portable.ps1
```

完整运行时、构建依赖、版本锁定和鉴权位置见 [`DEPENDENCIES.md`](DEPENDENCIES.md)。

## 5. 数据产物

### CSV 数据仓（`data/raw/`）

| 文件 | 内容 |
| --- | --- |
| `electricity_price_detail.csv` | 全量分时明细，键为 区域代码 + 交易日 + 时点 |
| `quality.csv` | 每个区域日的状态：`available` / `empty` / `failed` / `missing` |
| `daily_summary.csv` | 逐日均价、极值、价差 |
| `province_summary.csv` | 区域覆盖率与区间统计 |
| `metadata.json` | 区间、行数、覆盖率、更新时间 |

### JSON（`data/exports/json/`）

| 文件 | 用途 |
| --- | --- |
| `meta.json` | 元数据、区域清单、字段中文标签 |
| `provinces.json` / `daily.json` | 区域汇总 / 逐日汇总 |
| `coverage.json` | 区域 × 日的状态矩阵（每区域压成一条状态串） |
| `profiles.json` | 各区域各月的平均日内曲线 |
| `latest.json` | 最新一天的全区域分时 |
| `detail.jsonl` | 全量明细，一行一条，适合入库 |
| `detail/<拼音>-<年月>.json` | 分区域分月明细，配合 `detail/index.json` 按需加载 |

### Excel

- `data/exports/excel/`：一个汇总工作簿 + 按月拆分的明细工作簿（避免单文件过大）
- `data/exports/tree/`：按 **地区 / 月 / 周** 三级目录拆分，方便只取某个区域某一周

```text
data/exports/tree/
├── _索引.xlsx                        # 全部周文件的清单与覆盖情况
├── _增量状态.json                    # 内容指纹，用于跳过没变化的周
└── 新疆/
    ├── 新疆_全期汇总.xlsx            # 按月分表的每日汇总
    └── 2026-07/
        ├── 新疆_2026-07_7-1_0701-0707.xlsx
        ├── 新疆_2026-07_7-2_0708-0714.xlsx
        └── 新疆_2026-07_月汇总.xlsx  # 按周分表的每日汇总
```

每个周文件三张表：**分时明细**（含星期与实时-日前价差）、**每日汇总**、**说明**。

周的切法默认是「当月第几个 7 天」（`7-1` = 1–7 日），也可以按自然周：

```bash
python run.py export-tree --week-mode iso
python run.py export-tree --only-provinces 新疆 山西      # 只重建部分区域
python run.py export-tree --full                          # 忽略增量状态全部重写
```

增量靠 `_增量状态.json` 里的内容指纹：只有数据真的变了才重写那一周，
被重采覆盖的周会替换旧文件，新增的周直接补上。870 个周文件全量重建约 1 分钟，
无变化时重跑只要 6 秒。

### 其它

- `data/exports/reports/`：周报的 Markdown、HTML、JSON 三种形态
- `data/exports/看板.html`：单文件看板，可离线打开、可直接发给别人
- `data/exports/artifact.html`：Artifact 片段，用于发布到 claude.ai

### Artifact 在线看板

`python run.py artifact` 生成的页面可以发布到 claude.ai 长期访问。它是纯静态内嵌数据：
Artifact 的 CSP 挡掉一切外部请求，所以在线版没有令牌输入和采集按钮，
那些功能只在 `python run.py serve` 的本地控制台里。

每周更新时跑完 `python run.py weekly`，再让 Claude 用同一个 URL 重新发布即可保持链接不变。

## 数据口径

- 每次请求 `startDate=endDate`，避免日期区间接口返回「区间分时平均值」。
- 不强制每天 96 点；接口按区域返回 24、48 或 96 点，原样保留。
- 蒙西、四川等区域接口只返回实时价，日前价为空——这是接口口径，不是采集缺失。
- 缺失数据一律留空，不填 0；`available` 有明细，`empty` 接口成功但无数据，`failed` 请求失败，`missing` 尚未采集。
- 价格单位按接口当前口径保存为 `元/MWh`。
- 统计全网日内曲线时会剔除样本量不足的时点，避免不同粒度区域混算造成偏差。

## 注意

本工具基于当前观察到的非公开接口结构，接口地址、字段、鉴权方式或使用规则可能变化。
请仅在你有权访问和采集的范围内使用，并遵守服务条款、数据授权及合理请求频率。

本地控制台只监听 `127.0.0.1` 并用一次性会话密钥校验所有接口，不要把它暴露到公网。
