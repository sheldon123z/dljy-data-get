#!/usr/bin/env python3
"""全国现货电价 MCP server —— 把本项目的数据仓和采集能力暴露给 agent。

为什么是 MCP 而不是让 agent 直接跑命令：
消费方 agent 通常运行在 Docker 沙箱里，看不到宿主文件系统，也没有 python/项目依赖。
MCP server 由 OpenClaw gateway 在**宿主**启动，agent 只通过协议调用，
于是「能查电价」和「碰不到你的电脑」可以同时成立。

协议：MCP stdio，JSON-RPC 2.0。手写而非用 SDK，避免给 gateway 引入额外依赖。
注意：stdout 只能是协议报文，任何日志都必须走 stderr，否则会破坏握手。
只用标准库，且保持 Python 3.9 兼容——gateway 解析到哪个 python3 不由我们决定。
"""

from __future__ import annotations

import csv
import http.client
import json
import os
import re
import ssl
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

# 本文件住在 <项目根>/mcp/ 下，所以项目根就是自己的上一级。
# 从脚本位置推导而不是写死家目录：整个项目被搬到别处也照样能跑。
_DEFAULT_PROJECT = Path(__file__).resolve().parent.parent
PROJECT = Path(os.environ.get("DLJY_DATA_GET_HOME") or _DEFAULT_PROJECT)
RAW = PROJECT / "data" / "raw"
DETAIL = RAW / "electricity_price_detail.csv"
PROV_SUMMARY = RAW / "province_summary.csv"
PROTOCOL_VERSION = "2024-11-05"

# 分时行在内存里的紧凑形式。存原始 dict 要 317MB 常驻，
# 而这个进程由 gateway 长期挂着，所以只留真正会用到的三列。
TIME, DA, RT = 0, 1, 2

# 单次响应里最多回多少个分时点。288 点区域（江西）全曲线约 17KB，
# 塞进对话上下文性价比极低，超出就降采样。
MAX_CURVE_POINTS = 96
MAX_SAMPLES = 50

# sync_days 一次最多发多少个请求。接口配额有限，宁可让 agent 分两次，
# 也不能让一句"同步一下"变成几百次请求。
SYNC_MAX_REQUESTS = 60

# 实时出清的发布延迟上限。超过这个时长还是 0 的时点，判定为真实零电价而非未出清。
# 实测延迟在半小时内（02:18 出清到 01:45），留 6 小时余量。
CLEARING_LAG_CAP = timedelta(hours=6)

# 小程序 UA，与 scripts/collect.py 保持一致——换掉可能被接口侧识别拦截。
API_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20 MiniProgramEnv/Mac"
)

# 尾部零段若起始于这个时刻之前，判为采集截断而非真实零价。
# 依据：真实零价由午间光伏过剩驱动，而晚高峰（19–21 点）是全天最贵的时段，
# 不可能整段恰好 0.00。放在 20:00 是保守取值。
ZERO_TAIL_PEAK_GUARD_MIN = 20 * 60

# 晚高峰窗口。这两小时是全天最贵、最稀缺的时段，日前与实时同时恰好 0.00
# 只可能是没数据；用它把「采集缺失」和「午间光伏压到地板价」区分开。
PEAK_START_MIN = 19 * 60
PEAK_END_MIN = 21 * 60

_detail_cache = None  # type: ignore[var-annotated]
_zero_tail_masked = {}  # type: ignore[var-annotated]  # (province,date) -> 被判为采集截断的点数
_aggregates_cache = None  # type: ignore[var-annotated]
_project_modules = None  # type: ignore[var-annotated]


def log(msg: str) -> None:
    print(f"[power-price-mcp] {msg}", file=sys.stderr, flush=True)


def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    # 数据仓由 Windows 侧工具写过，带 BOM；utf-8-sig 一并吃掉。
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(value):
    """空串/None/脏值一律 None——缺失绝不能退化成 0，否则均价被拉低。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_detail():
    """按 (省份, 日期) 建索引。39.5 万行全表扫描太慢，进程内缓存一次。

    按 (省份, 日期, 时点) 去重、后写入的覆盖先写入的——与 common.build_outputs
    的口径一致。采集是追加写的，build_outputs 跑之前文件里会短暂存在重复行；
    不去重的话同一时点会在曲线里出现两次，均值也会被重复样本带偏。
    """
    global _detail_cache, _zero_tail_masked
    if _detail_cache is not None:
        return _detail_cache
    index = defaultdict(dict)
    with DETAIL.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("province", ""), row.get("trade_date", ""))
            slot = row.get("time_slot", "")
            index[key][slot] = (slot, _f(row.get("day_ahead_price")), _f(row.get("real_time_price")))
    cache, masked = {}, {}
    for k, v in index.items():
        rows = sorted(v.values(), key=lambda r: _slot_min(r[TIME]))
        rows, n = _mask_fake_zeros(rows)
        cache[k] = rows
        if n:
            masked[k] = n
    _detail_cache, _zero_tail_masked = cache, masked
    globals()["_aggregates_cache"] = None
    log(f"detail index built: {len(_detail_cache)} (province,date) keys, "
        f"zero-tail masked: {sum(masked.values())} pts in {len(masked)} region-days")
    return _detail_cache


def _slot_min(slot: str) -> int:
    """'13:45' -> 825。用于排序和时段判断；'24:00' 天然排到最后。"""
    try:
        h, m = slot.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -1


def _mask_fake_zeros(rows: list):
    """把「无数据被写成 0」的价格还原成 None。数据仓读取和接口直取都要过这一关。

    接口对没有数据的时段返回数字 0.0（不是 null、不是空串，实测 96 个点全是 float），
    采集脚本 float() 之后原样落盘，于是数据仓里混着两种 0：
      · 真实零电价——午间光伏过剩压出来的成交价，必须保留；
      · 采集截断——那天实时价压根没出来，被写成一片 0。
    实测数据仓里有 15 个区域日**整天 96 个点全是 0.00**（黑龙江 12 天、山西 2 天、湖南 1 天），
    quality.csv 全标 available，所以 collect.py 的增量采集永远不会重采它们。
    不处理的话 get_daily_summary 会报「均价 0.00、最低 0 元」——纯假数字。

    判别：**零段一直延伸到当日最后一个时点，且起点早于晚高峰（20:00）**。
    真实零价由午间光伏驱动、在晚高峰前就结束；而晚高峰是全天最贵的时段，
    整段恰好 0.00 只可能是没数据。实测这条规则在实时价上命中 50 个区域日，
    保留 1393 个含真实零价的区域日（山西 2026-08-01 的 8 个午间零价尾段为 0，不受影响）。

    **日前价有一模一样的污染**（24 个区域日整天全零），所以两列各自独立判、独立掩。

    规则二（位置无关）：**同一时点日前与实时同时恰好 0.0** 且当日这类"双零点"
    落进晚高峰（19:00–21:00）——晚高峰是全天最贵、最稀缺的时段，两侧同时恰好 0.00
    只可能是没数据。这条能抓到零点分散、尾部却有正常价格的日子，尾部规则抓不到。
    但**不能只看双零**：不少省份价格下限就是 0，午间光伏过剩时日前和实时会一起打到
    地板价——实测 812 个区域日的双零点集中在 10:45–13:00，正是光伏大发窗口，
    必须保留。所以加"覆盖晚高峰"这个条件，实测只命中 111 个区域日。
    """
    out = list(rows)
    masked = 0

    dz = [i for i, r in enumerate(out) if r[DA] == 0.0 and r[RT] == 0.0]
    if any(PEAK_START_MIN <= _slot_min(out[i][TIME]) <= PEAK_END_MIN for i in dz):
        for i in dz:
            out[i] = (out[i][TIME], None, None)
        masked += len(dz)

    for col in (DA, RT):
        tail = 0
        for i in range(len(out) - 1, -1, -1):
            if out[i][col] == 0.0:
                tail += 1
            else:
                break
        if not tail:
            continue
        start = _slot_min(out[len(out) - tail][TIME])
        if start < 0 or start > ZERO_TAIL_PEAK_GUARD_MIN:
            continue
        for i in range(len(out) - tail, len(out)):
            r = out[i]
            out[i] = (r[TIME], None, r[RT]) if col == DA else (r[TIME], r[DA], None)
        masked += tail

    # 规则三（兜底，必须放在前两条之后）：一列被前面掩掉一部分后，**剩下的点若全是 0.0**，
    # 那这一列的均价必然是 0.00 —— 日前和实时都不可能出清成整天零。
    #
    # 前两条规则都有位置约束（尾部连续 / 覆盖晚高峰），抓不到零点零散分布、
    # 且大部分点已被掩成 None 的日子。实测残留 11 个区域日：
    # 黑龙江 2026-08-07 掩剩 6 个点、日前全 0.0，于是 day_ahead_avg=0.00、
    # real_time_avg=211.87，get_price_trend 就报出 spread_avg=211.87 这个
    # 凭空捏造的价差；吉林 2026-08-10 更极端，残留 43 个点全零。
    # 价差是这套数据最核心的业务指标，伪造它比少一天数据严重得多。
    for col in (DA, RT):
        present = [i for i in range(len(out)) if out[i][col] is not None]
        if not present or any(out[i][col] != 0.0 for i in present):
            continue
        for i in present:
            r = out[i]
            out[i] = (r[TIME], None, r[RT]) if col == DA else (r[TIME], r[DA], None)
        masked += len(present)

    return out, masked


def _invalidate_cache() -> None:
    global _detail_cache, _aggregates_cache
    _detail_cache = None
    _aggregates_cache = None


def _dates_of(province: str) -> list:
    return sorted(d for (p, d) in _load_detail() if p == province)


def _pick_dates(province: str, days: int, end_date: str) -> list:
    """取该地区最近 N 个**有数据的**日期（不是自然日）。"""
    dates = _dates_of(province)
    if end_date:
        dates = [d for d in dates if d <= end_date]
    return dates[-max(1, days):]


def _stats(rows: list, col: int) -> dict:
    vals = [r[col] for r in rows if r[col] is not None]
    if not vals:
        return {"count": 0}
    return {
        "count": len(vals),
        "avg": round(sum(vals) / len(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def _spread_avg(rows: list):
    """只在两侧都有值的时点上算价差——单边缺失的时点必须整点丢弃。"""
    pairs = [r[RT] - r[DA] for r in rows if r[DA] is not None and r[RT] is not None]
    return round(sum(pairs) / len(pairs), 2) if pairs else None


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _warehouse_last_date() -> str:
    dates = {d for (_p, d) in _load_detail()}
    return max(dates) if dates else ""


def _region_price_aggregates() -> dict:
    """按区域重算全期价格聚合。

    不能直接用 province_summary.csv 的 real_time_avg——那是 build_outputs 从原始
    明细算的，把「无数据写成 0」的点也算了进去。实测黑龙江因此被拉低 12.1%
    （252.53 实为 283.20）。这里从**掩码后**的内存索引重算，只覆盖价格聚合，
    覆盖率/日期区间/粒度仍取汇总表。
    """
    global _aggregates_cache
    if _aggregates_cache is not None:
        return _aggregates_cache
    da, rt, sp, neg = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(int)
    for (p, _d), rows in _load_detail().items():
        for r in rows:
            if r[DA] is not None:
                da[p].append(r[DA])
            if r[RT] is not None:
                rt[p].append(r[RT])
                if r[RT] < 0:
                    neg[p] += 1
            if r[DA] is not None and r[RT] is not None:
                sp[p].append(r[RT] - r[DA])
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    _aggregates_cache = {
        p: {"day_ahead_avg": avg(da.get(p, [])), "real_time_avg": avg(rt.get(p, [])),
            "spread_avg": avg(sp.get(p, [])), "negative_realtime_points": neg.get(p, 0),
            "realtime_points": len(rt.get(p, []))}
        for p in set(list(da) + list(rt))
    }
    return _aggregates_cache


_QUALITY_REASON = ("这些时点在数据仓里是 0（接口对无数据时段返回数字 0），"
                   "已判为采集缺失并按缺失处理，不计入均价/最值/峰谷/价差")
_QUALITY_CAVEAT = "这些时点是**没有数据**，不是价格为 0"


def _data_quality(province: str, trade_date: str) -> dict:
    """该区域日有没有被判为采集截断。让 agent 知道"这天缺了多少点"，
    而不是拿一个被 0 拉低的均价去回答。"""
    _load_detail()
    n = _zero_tail_masked.get((province, trade_date), 0)
    if not n:
        return {}
    return {"data_quality": {
        "missing_points": n,
        "reason": _QUALITY_REASON,
        "caveat": _QUALITY_CAVEAT,
    }}


def _data_quality_range(pairs) -> dict:
    """多天/跨省聚合的质量提示。pairs 是 (province, date) 序列。

    单日工具早就有 `_data_quality`，但聚合类工具一直没带——同一份残缺数据，
    问 `get_daily_summary` 会被告知"缺 56 个点"，问 `get_price_trend` 就只剩
    一个 `real_time_avg: 120.56`，调用方无从知道它是拿 40/96 个点算出来的。
    数字本身没错，代表性完全不同，于是"如实说明数据完整性"这条纪律
    在聚合场景下根本无从执行——不是不守，是拿不到信息。
    """
    _load_detail()
    hits = []
    total = 0
    for province, trade_date in pairs:
        n = _zero_tail_masked.get((province, trade_date), 0)
        if not n:
            continue
        hits.append({"province": province, "date": trade_date, "missing_points": n})
        total += n
    if not hits:
        return {}
    hits.sort(key=lambda h: (h["date"], h["province"]))
    return {"data_quality": {
        "incomplete_days": len(hits),
        "missing_points_total": total,
        "details": hits[:MAX_SAMPLES],
        "truncated": len(hits) > MAX_SAMPLES,
        "reason": _QUALITY_REASON,
        "caveat": _QUALITY_CAVEAT + "；这些天的均价基于**剩余时点**计算，"
                  "代表性弱于完整日，引用时要说明",
    }}


def _freshness(province: str = "") -> dict:
    """数据新鲜度提示。

    没有这个，agent 会把三天前的数据当成"今天的电价"说出去——
    数据仓永远滞后于现实，滞后多少必须让调用方看见。
    """
    last = _dates_of(province)[-1] if province and _dates_of(province) else _warehouse_last_date()
    info = {"data_last_date": last or None, "source": "warehouse"}
    if last:
        try:
            behind = (date.today() - date.fromisoformat(last)).days
            info["days_behind_today"] = behind
            if behind >= 2:
                info["staleness_warning"] = (
                    f"数据仓最新只到 {last}，距今 {behind} 天。"
                    f"要更新的数据请用 fetch_live_price 直接查接口，或先 sync_days 补采。"
                )
        except ValueError:
            pass
    return info


_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{2,}(?:[.…][A-Za-z0-9_\-.…]*)?")


def _redact(text: str) -> str:
    """run.py status 会打印令牌前后缀。这条链路终点是云端模型和钉钉群，
    令牌片段一个字符都不该流出本机。"""
    return _TOKEN_RE.sub("<token-redacted>", text or "")


def _project_python() -> str:
    """优先用项目自带 venv：gateway 用哪个 python3 拉起我们并不确定，
    但 run.py 的依赖只装在 .venv 里。"""
    venv = PROJECT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else (sys.executable or "python3")


# ──────────────────────── 实时接口（绕过本地数据仓） ────────────────────────

def _load_project_modules():
    """惰性引入项目的 config / common。

    惰性有两个原因：① 只读工具不该因为项目结构变动就连握手都做不了；
    ② 落盘必须复用 common.append_csv / build_outputs，自己手写 CSV 追加
    极易在 BOM、字段顺序、去重口径上和采集脚本产生漂移，那会污染数据仓。
    """
    global _project_modules
    if _project_modules is not None:
        return _project_modules
    scripts = PROJECT / "scripts"
    if not (scripts / "common.py").exists():
        raise RuntimeError(f"项目脚本目录不存在：{scripts}")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import common as _common  # noqa: E402
    import config as _config  # noqa: E402
    _project_modules = (_config, _common)
    return _project_modules


class TokenError(RuntimeError):
    """令牌缺失或失效——要人工介入，和网络错误必须分开处理。"""


def _api_post(payload: dict, timeout: int = 40, retries: int = 2) -> dict:
    """直连电查查接口取一个 (区域, 日) 的分时电价。

    只用 http.client，不引第三方库。鉴权失败单独抛 TokenError，
    因为它需要人工换令牌，重试再多次也没用。
    """
    _config, _common = _load_project_modules()
    token = _config.load_token()
    if not token:
        raise TokenError(
            f"未配置采集令牌。请在宿主机上跑 `python run.py token` 手工录入，"
            f"或 `python run.py sniff` 自动抓取（项目目录 {PROJECT}）。"
        )
    parsed = urlparse(_common.BASE_URL)
    path = _common.DETAIL_URL[len(_common.BASE_URL):]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": token,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": API_USER_AGENT,
    }
    last_error = None
    for attempt in range(retries + 1):
        conn = None
        try:
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443,
                timeout=timeout, context=ssl.create_default_context(),
            )
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status in (401, 403):
                raise TokenError(
                    f"鉴权失败 HTTP {resp.status}，令牌已失效。"
                    f"请在宿主机跑 `python run.py sniff`（自动抓）或 `python run.py token`（手工粘贴）更新。"
                )
            if resp.status == 429:
                raise RuntimeError("接口限流 HTTP 429，请降低调用频率后重试")
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            result = json.loads(raw.decode("utf-8"))
            if result.get("code") != 200:
                raise RuntimeError(
                    f"接口返回 code={result.get('code')}: {result.get('message') or result.get('msg')}"
                )
            return result
        except TokenError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(8, 0.8 * (2 ** attempt)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise RuntimeError(f"请求失败：{last_error}")


def _slot_dt(trade_date: str, slot: str):
    """把 '00:15' / '24:00' 解析成绝对时刻。24:00 表示次日零点，
    用 timedelta 相加天然处理，不能用 time.fromisoformat（它拒绝 24:00）。"""
    try:
        d = date.fromisoformat(trade_date)
        hh, mm = slot.split(":")
        return datetime.combine(d, datetime.min.time()) + timedelta(hours=int(hh), minutes=int(mm))
    except (ValueError, AttributeError):
        return None


def _mask_uncleared(rows: list, trade_date: str):
    """把「尚未出清」的实时价从 0.0 改回 None。

    实时市场当日滚动出清，未出清时段接口返回 **0.0 而不是 null**。原样收下的话，
    均价会被一堆 0 拉到离谱的低位（实测凌晨 2 点查当天：真实 8 个点的均价被 96 个
    点算成 28.1 元/MWh），min 变成 0 还会被读成"今天出现过零电价"。
    这正是"看起来合理、实际离谱"的假数字，拿去做交易判断要出事。

    但**不能把 0 一律当缺失**：这个市场里零电价和负电价是真实存在的——
    山西历史上有 2590 个零点，2026-08-01 的 8 个零点全在 10:00–11:45，
    是午间光伏大发压出来的真实成交价。

    所以用两条规则，且只对**当日及以后**生效（历史日期数据已终局）：
      ① 时间还没到的时点，一定没出清；
      ② 出清有发布延迟（实测 02:18 只出清到 01:45），所以时间已过但仍为 0 的
         **尾部连续段**也按未出清处理。只从尾部连续地判——中间夹着的 0
         是真实零电价，必须原样保留。

    规则 ② 还要卡一个回溯上限 CLEARING_LAG_CAP：出清延迟是分钟到小时级，
    不可能是十几个小时。没有这个上限的话，尾部扫描会一路穿过去，
    把当天早些时候的真实零价也吃掉（例如 23:59 查当天，会误伤中午 12:00 的零价）。
    """
    if trade_date < date.today().isoformat():
        return rows, None, 0
    now = datetime.now()
    out = list(rows)
    masked = 0
    for i, r in enumerate(out):
        dt = _slot_dt(trade_date, r[TIME])
        if r[RT] is not None and dt is not None and dt > now:
            out[i] = (r[TIME], r[DA], None)
            masked += 1
    for i in range(len(out) - 1, -1, -1):
        if out[i][RT] is None:
            continue
        dt = _slot_dt(trade_date, out[i][TIME])
        if dt is not None and now - dt > CLEARING_LAG_CAP:
            break  # 早于出清延迟上限的 0 是真实零电价，不能动
        if out[i][RT] == 0.0:
            out[i] = (out[i][TIME], out[i][DA], None)
            masked += 1
        else:
            break
    cleared_until = None
    for r in out:
        if r[RT] is not None:
            cleared_until = r[TIME]
    return out, cleared_until, masked


def _province_index() -> dict:
    _config, _common = _load_project_modules()
    return {p["province"]: p for p in _common.load_provinces()}


def _api_rows(province_row: dict, trade_date: str) -> list:
    """取一个区域一天的分时点，返回 [(time, day_ahead, real_time), ...]。"""
    result = _api_post({
        "areaCode": province_row["province_code"],
        "startDate": trade_date,
        "endDate": trade_date,
    })
    out = []
    for item in result.get("data") or []:
        # 接口对两个价格字段的类型不一致：日前常是字符串 "398.0"，实时是数字。
        # 一律走 _f 兜底，别假设类型。
        out.append((item.get("time96") or "", _f(item.get("avgDayAheadPrice")), _f(item.get("avgRealTimePrice"))))
    out.sort(key=lambda r: r[TIME])
    return out


def _run_project(args: list, timeout: int) -> dict:
    """调用 run.py。采集类操作耗时长，超时必须显式给，不能无限等。"""
    if not (PROJECT / "run.py").exists():
        return {"ok": False, "error": f"项目不存在: {PROJECT}"}
    try:
        p = subprocess.run(
            [_project_python(), "run.py", *args],
            cwd=str(PROJECT), capture_output=True, text=True, timeout=timeout,
        )
        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": _redact((p.stdout or "")[-4000:]),
            "stderr": _redact((p.stderr or "")[-1500:]),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超过 {timeout}s 未完成；采集任务可能仍在后台跑，稍后用 get_data_status 查看"}


# ──────────────────────────── 工具实现 ────────────────────────────

def t_list_regions(_: dict) -> dict:
    rows = _read_csv(PROV_SUMMARY)
    if not rows:
        return {"error": "汇总表不存在，可能还没采过数据", "path": str(PROV_SUMMARY)}
    agg = _region_price_aggregates()
    out = []
    for r in rows:
        name = r.get("province")
        a = agg.get(name, {})
        out.append({
            "province": name,
            "coverage": _f(r.get("coverage")),
            "first_date": r.get("first_date"),
            "last_date": r.get("last_date"),
            # 价格聚合从掩码后的索引重算，不用汇总表里被 0 污染的值
            "day_ahead_avg": a.get("day_ahead_avg", _f(r.get("day_ahead_avg"))),
            "real_time_avg": a.get("real_time_avg", _f(r.get("real_time_avg"))),
            "spread_avg": a.get("spread_avg", _f(r.get("spread_avg"))),
            "points_per_day": _int(r.get("dominant_points_per_day"), 0),
            "negative_realtime_points": a.get("negative_realtime_points",
                                              _int(r.get("negative_realtime_points"), 0)),
        })
    out.sort(key=lambda x: x["province"] or "")
    return {
        "count": len(out),
        "unit": "元/MWh",
        "freshness": _freshness(),
        "regions": out,
        "note": "day_ahead_avg / real_time_avg 是全区间均价。部分区域（如蒙西、四川）"
                "接口只给实时价，日前为 null 是接口口径而非采集缺失；"
                "points_per_day 不同的区域之间比较分时数据要当心粒度差异。",
    }


def t_get_price_curve(a: dict) -> dict:
    province, date = a.get("province", ""), a.get("date", "")
    rows = _load_detail().get((province, date), [])
    if not rows:
        return {"error": f"没有 {province} {date} 的数据", "hint": "用 list_regions 看可用区域与日期范围"}
    limit = min(_int(a.get("max_points"), MAX_CURVE_POINTS), 288)
    step = max(1, -(-len(rows) // limit))  # 上取整，等间隔抽稀
    shown = rows[::step]
    return {
        "province": province, "date": date, "unit": "元/MWh",
        "point_count": len(rows),
        "returned_points": len(shown),
        "downsampled": step > 1,
        "downsample_step": step,
        "day_ahead": _stats(rows, DA),      # 统计量始终基于全量点，不受抽稀影响
        "real_time": _stats(rows, RT),
        "spread_avg": _spread_avg(rows),
        "freshness": _freshness(province),
        **_data_quality(province, date),
        "points": [{"time": r[TIME], "day_ahead": r[DA], "real_time": r[RT]} for r in shown],
    }


def t_get_daily_summary(a: dict) -> dict:
    province, date = a.get("province", ""), a.get("date", "")
    rows = _load_detail().get((province, date), [])
    if not rows:
        return {"error": f"没有 {province} {date} 的数据"}
    peak = {"time": None, "real_time": None}
    valley = {"time": None, "real_time": None}
    for r in rows:
        t = r[RT]
        if t is None:
            continue
        if peak["real_time"] is None or t > peak["real_time"]:
            peak = {"time": r[TIME], "real_time": round(t, 2)}
        if valley["real_time"] is None or t < valley["real_time"]:
            valley = {"time": r[TIME], "real_time": round(t, 2)}
    return {
        "province": province, "date": date, "unit": "元/MWh",
        "point_count": len(rows),
        "day_ahead": _stats(rows, DA), "real_time": _stats(rows, RT),
        "spread_avg": _spread_avg(rows),
        "peak": peak, "valley": valley,
        "negative_realtime_points": sum(1 for r in rows if r[RT] is not None and r[RT] < 0),
        "freshness": _freshness(province),
        **_data_quality(province, date),
        "note": "spread = 实时 - 日前，正值表示实时高于日前。若 real_time.count 为 0，"
                "说明该日实时价尚未回填（最新一天常见），不是价格为零。",
    }


def t_compare_regions(a: dict) -> dict:
    provinces, date = a.get("provinces") or [], a.get("date", "")
    idx = _load_detail()
    items, missing = [], []
    for p in provinces:
        rows = idx.get((p, date), [])
        if not rows:
            missing.append(p)
            continue
        da, rt = _stats(rows, DA), _stats(rows, RT)
        items.append({
            "province": p,
            "day_ahead_avg": da.get("avg"), "real_time_avg": rt.get("avg"),
            "real_time_max": rt.get("max"), "real_time_min": rt.get("min"),
            "spread_avg": _spread_avg(rows),
            "points_per_day": len(rows),
        })
    items.sort(key=lambda x: (x["real_time_avg"] is None, -(x["real_time_avg"] or 0)))
    return {
        "date": date, "unit": "元/MWh", "compared": items, "missing": missing,
        "freshness": _freshness(),
        **_data_quality_range((it["province"], date) for it in items),
        "note": "real_time_avg 为 null 表示该区域当日无实时价（接口口径或尚未回填），"
                "排序时统一排在末尾，不代表价格低。",
    }


def t_get_price_trend(a: dict) -> dict:
    province = a.get("province", "")
    picked = _pick_dates(province, _int(a.get("days"), 7), a.get("end_date") or "")
    if not picked:
        return {"error": f"没有 {province} 的数据", "hint": "用 list_regions 看可用区域"}
    idx = _load_detail()
    series = []
    for d in picked:
        rows = idx[(province, d)]
        da, rt = _stats(rows, DA), _stats(rows, RT)
        series.append({
            "date": d, "day_ahead_avg": da.get("avg"), "real_time_avg": rt.get("avg"),
            "real_time_max": rt.get("max"), "real_time_min": rt.get("min"),
            "spread_avg": _spread_avg(rows),
        })
    rts = [s["real_time_avg"] for s in series if s["real_time_avg"] is not None]
    return {
        "province": province, "unit": "元/MWh", "days": len(series),
        "period": {"from": picked[0], "to": picked[-1]},
        "overall_real_time_avg": round(sum(rts) / len(rts), 2) if rts else None,
        "freshness": _freshness(province),
        **_data_quality_range((province, d) for d in picked),
        "series": series,
    }


def t_find_extremes(a: dict) -> dict:
    province = a.get("province", "")
    # 缺参数和缺数据必须分开报。以前 province 为空会掉进下面的 `没有 {province} 的数据`，
    # 打出「没有  的数据」——调用方会当成"数据仓缺这个区域"，而不是"我漏传了参数"，
    # 正好踩中最忌讳的「把缺数据说成没有交易」。
    if not province:
        return {
            "error": "province 是必填参数（本工具一次只扫一个区域）",
            "hint": "用 list_regions 看可用区域原名；想看全网当日情况用 rank_spread",
        }
    kind = a.get("kind") or "negative"
    threshold = _f(a.get("threshold"))
    if threshold is None:
        threshold = 1000.0
    dates = _pick_dates(province, _int(a.get("days"), 30), a.get("end_date") or "")
    if not dates:
        return {"error": f"没有 {province} 的数据", "hint": "用 list_regions 看可用区域与各自的数据区间"}
    idx = _load_detail()
    hits, by_date = [], defaultdict(int)
    for d in dates:
        for r in idx[(province, d)]:
            rt = r[RT]
            if rt is None:
                continue
            if (kind == "negative" and rt < 0) or (kind == "spike" and rt >= threshold):
                hits.append({"date": d, "time": r[TIME], "real_time": round(rt, 2)})
                by_date[d] += 1
    hits.sort(key=lambda x: x["real_time"], reverse=(kind == "spike"))
    return {
        "province": province, "kind": kind,
        "threshold": threshold if kind == "spike" else 0,
        "scanned_days": len(dates),
        "period": {"from": dates[0], "to": dates[-1]},
        "hit_count": len(hits),
        "hit_days": len(by_date),
        "hits_by_date": dict(sorted(by_date.items())),
        "unit": "元/MWh",
        "truncated": len(hits) > MAX_SAMPLES,
        "freshness": _freshness(province),
        **_data_quality_range((province, d) for d in dates),
        "samples": hits[:MAX_SAMPLES],
    }


def t_rank_spread(a: dict) -> dict:
    """跨省价差排行：同一天全网按 spread(实时-日前) 排序。"""
    date = a.get("date", "")
    idx = _load_detail()
    provinces = sorted({p for (p, d) in idx if d == date})
    if not provinces:
        return {"error": f"没有 {date} 的数据", "hint": "用 get_data_status 或 list_regions 确认数据截止日"}
    ranked, incomplete = [], []
    for p in provinces:
        rows = idx[(p, date)]
        sp = _spread_avg(rows)
        da, rt = _stats(rows, DA), _stats(rows, RT)
        if sp is None:
            incomplete.append({
                "province": p,
                "day_ahead_avg": da.get("avg"), "real_time_avg": rt.get("avg"),
                "reason": "该日日前与实时没有同时可用的时点",
            })
            continue
        ranked.append({
            "province": p, "spread_avg": sp,
            "day_ahead_avg": da.get("avg"), "real_time_avg": rt.get("avg"),
            "spread_max": round(max(r[RT] - r[DA] for r in rows if r[DA] is not None and r[RT] is not None), 2),
            "spread_min": round(min(r[RT] - r[DA] for r in rows if r[DA] is not None and r[RT] is not None), 2),
        })
    ranked.sort(key=lambda x: -x["spread_avg"])
    top = _int(a.get("top"), 0)
    body = ranked[:top] if top > 0 else ranked
    return {
        "date": date, "unit": "元/MWh",
        "region_count": len(ranked),
        "ranking": body,
        "incomplete": incomplete,
        "freshness": _freshness(),
        **_data_quality_range((r["province"], date) for r in ranked),
        "note": "spread = 实时均价 - 日前均价，只在两侧都有值的时点上计算。"
                "spread 显著为正说明日前报低了/实时偏紧，日前多卖对发电侧不利；"
                "显著为负说明日前报高了，实时买入更便宜。",
    }


def t_get_peak_valley(a: dict) -> dict:
    """峰谷特征：逐日峰谷差、峰谷比与峰谷时刻，用于判断日内套利空间。"""
    province = a.get("province", "")
    field = a.get("field") or "real_time"
    col = RT if field == "real_time" else DA
    picked = _pick_dates(province, _int(a.get("days"), 7), a.get("end_date") or "")
    if not picked:
        return {"error": f"没有 {province} 的数据"}
    idx = _load_detail()
    days, spans = [], []
    for d in picked:
        rows = [r for r in idx[(province, d)] if r[col] is not None]
        if not rows:
            days.append({"date": d, "available": False})
            continue
        hi = max(rows, key=lambda r: r[col])
        lo = min(rows, key=lambda r: r[col])
        span = hi[col] - lo[col]
        spans.append(span)
        days.append({
            "date": d, "available": True,
            "peak_time": hi[TIME], "peak_price": round(hi[col], 2),
            "valley_time": lo[TIME], "valley_price": round(lo[col], 2),
            "peak_valley_span": round(span, 2),
            # 谷价 <= 0 时比值没有经济含义，宁可给 null 也不给一个会被拿去说事的数
            "peak_valley_ratio": round(hi[col] / lo[col], 2) if lo[col] > 0 else None,
        })
    return {
        "province": province, "field": field, "unit": "元/MWh",
        "days": len(picked),
        "period": {"from": picked[0], "to": picked[-1]},
        "avg_peak_valley_span": round(sum(spans) / len(spans), 2) if spans else None,
        "max_peak_valley_span": round(max(spans), 2) if spans else None,
        "freshness": _freshness(province),
        **_data_quality_range((province, d) for d in picked),
        "daily": days,
        "note": "peak_valley_span = 当日最高价 - 最低价，是日内套利（如储能充放）的毛空间上限，"
                "尚未扣除效率损失与容量成本。谷价为负或零时 peak_valley_ratio 给 null。",
    }


def t_get_hourly_profile(a: dict) -> dict:
    """同一时刻在连续多日上的价格分布——找出典型高价时段与最不稳定的时点。"""
    province = a.get("province", "")
    field = a.get("field") or "real_time"
    col = RT if field == "real_time" else DA
    picked = _pick_dates(province, _int(a.get("days"), 14), a.get("end_date") or "")
    if not picked:
        return {"error": f"没有 {province} 的数据"}
    idx = _load_detail()
    buckets = defaultdict(list)
    for d in picked:
        for r in idx[(province, d)]:
            if r[col] is not None:
                buckets[r[TIME]].append(r[col])
    if not buckets:
        return {"error": f"{province} 在 {picked[0]}~{picked[-1]} 没有 {field} 数据"}
    profile = []
    for slot in sorted(buckets):
        vals = sorted(buckets[slot])
        # 分位数至少要 2 个样本，样本太少时退化为 min/max，不硬造分位
        if len(vals) >= 4:
            p10, p50, p90 = statistics.quantiles(vals, n=10)[0], statistics.median(vals), statistics.quantiles(vals, n=10)[8]
        else:
            p10, p50, p90 = vals[0], statistics.median(vals), vals[-1]
        profile.append({
            "time": slot, "samples": len(vals),
            "avg": round(sum(vals) / len(vals), 2),
            "p10": round(p10, 2), "median": round(p50, 2), "p90": round(p90, 2),
            "min": round(vals[0], 2), "max": round(vals[-1], 2),
            "stdev": round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0,
        })
    hottest = max(profile, key=lambda x: x["avg"])
    coldest = min(profile, key=lambda x: x["avg"])
    most_volatile = max(profile, key=lambda x: x["stdev"])
    return {
        "province": province, "field": field, "unit": "元/MWh",
        "days": len(picked),
        "period": {"from": picked[0], "to": picked[-1]},
        "typical_peak_slot": {"time": hottest["time"], "avg": hottest["avg"]},
        "typical_valley_slot": {"time": coldest["time"], "avg": coldest["avg"]},
        "most_volatile_slot": {"time": most_volatile["time"], "stdev": most_volatile["stdev"]},
        "freshness": _freshness(province),
        **_data_quality_range((province, d) for d in picked),
        "profile": profile,
        "note": "按时点跨日聚合。stdev 大的时点意味着日前报价在该时段最容易踩偏差考核。",
    }


def t_fetch_live_price(a: dict) -> dict:
    """绕过本地数据仓，直接问接口要某区域某日的分时电价。"""
    province, trade_date = a.get("province", ""), a.get("date", "")
    if not province or not trade_date:
        return {"error": "province 和 date 都是必填", "hint": "date 格式 YYYY-MM-DD"}
    try:
        provinces = _province_index()
    except Exception as exc:
        return {"error": f"读取区域代码表失败：{exc}"}
    row = provinces.get(province)
    if not row:
        return {"error": f"未知区域：{province}", "hint": "用 list_regions 看可用区域原名"}

    started = time.time()
    try:
        rows = _api_rows(row, trade_date)
    except TokenError as exc:
        return {"error": str(exc), "error_kind": "token", "actionable_by": "管理员（宿主机）"}
    except Exception as exc:
        return {"error": f"接口请求失败：{exc}", "error_kind": "network_or_api",
                "hint": "可稍后重试；持续失败用 get_data_status 看令牌状态"}

    in_warehouse = bool(_load_detail().get((province, trade_date)))
    if not rows:
        return {
            "province": province, "date": trade_date, "source": "live-api",
            "point_count": 0, "in_warehouse": in_warehouse,
            "error": f"接口对 {province} {trade_date} 返回空数据",
            "hint": "该日可能尚未出价（日前价通常 D-1 出、实时价 D+1 才补全），或该区域当日无交易",
        }

    rows, cleared_until, uncleared = _mask_uncleared(rows, trade_date)

    # 假零掩码：数据仓路径在 _load_detail 里做了，直取路径以前漏了。
    # 两条路径的脏数据来源是同一个——接口对没有数据的时段返回数字 0——
    # 只是一个从 CSV 读、一个从 HTTP 拿。实测黑龙江 2026-08-07、吉林 2026-08-10
    # 接口至今仍返回 96 个点的日前价全 0（不是采集失败，是数据源就没有），
    # 不掩的话这里会报 day_ahead.avg=0.00 并算出一个凭空的价差。
    # 必须排在 _mask_uncleared 之后：未出清的点先掩成 None，才不会被误判成假零。
    rows, faked = _mask_fake_zeros(rows)

    limit = min(_int(a.get("max_points"), MAX_CURVE_POINTS), 288)
    step = max(1, -(-len(rows) // limit))
    shown = rows[::step]
    peak = {"time": None, "real_time": None}
    valley = {"time": None, "real_time": None}
    for r in rows:
        t = r[RT]
        if t is None:
            continue
        if peak["real_time"] is None or t > peak["real_time"]:
            peak = {"time": r[TIME], "real_time": round(t, 2)}
        if valley["real_time"] is None or t < valley["real_time"]:
            valley = {"time": r[TIME], "real_time": round(t, 2)}
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rt_stats = _stats(rows, RT)
    if cleared_until:
        rt_stats["cleared_until"] = cleared_until
    note = ("本条是**实时接口直取**，不经过本地数据仓，也没有落盘。"
            "要把它存进数据仓请用 sync_days（当日数据不允许落盘）。")
    if uncleared:
        note += (f"⚠️ 当日实时市场滚动出清，{cleared_until or '起始'} 之后的 {uncleared} 个时点"
                 f"尚未出清（接口对未出清时段返回 0，已按缺失处理、不计入统计）。"
                 f"real_time 的均价/最值/峰谷只基于已出清的 {rt_stats.get('count', 0)} 个点，"
                 f"**不能代表全天**。日前价是当日完整的，可以正常讲。")
    return {
        "province": province, "date": trade_date, "unit": "元/MWh",
        "source": "live-api",
        "fetched_at": fetched_at,
        "elapsed_ms": int((time.time() - started) * 1000),
        "point_count": len(rows), "returned_points": len(shown),
        "downsampled": step > 1, "downsample_step": step,
        "day_ahead": _stats(rows, DA), "real_time": rt_stats,
        "spread_avg": _spread_avg(rows),
        "peak": peak, "valley": valley,
        "negative_realtime_points": sum(1 for r in rows if r[RT] is not None and r[RT] < 0),
        "settled_points": rt_stats.get("count", 0),   # 已出清点数
        "pending_points": uncleared,                  # 尚未出清点数
        "uncleared_points": uncleared,                # pending_points 的别名，保持向后兼容
        "realtime_complete": uncleared == 0,
        "in_warehouse": in_warehouse,
        **({"data_quality": {
            "missing_points": faked,
            "reason": "接口对这些时点返回的是数字 0 而不是空值，已判为无数据并掩掉，"
                      "不计入均价/最值/峰谷/价差",
            "caveat": "这些时点是**数据源没有**，不是价格为 0；"
                      "某一列整列如此，说明该区域该日这个市场就没有公布价格",
        }} if faked else {}),
        # 也给 freshness，字段名与读数据仓的工具保持一致，agent 用同一条纪律即可；
        # source=live-api 表明这不是仓里的数。
        "freshness": {
            "source": "live-api", "data_last_date": trade_date,
            "fetched_at": fetched_at, "cleared_until": cleared_until,
            "realtime_complete": uncleared == 0,
        },
        "points": [{"time": r[TIME], "day_ahead": r[DA], "real_time": r[RT]} for r in shown],
        "note": note,
    }


def t_sync_days(a: dict) -> dict:
    """轻量增量：只补指定范围，落盘并重建汇总。比 update_daily 细粒度。"""
    days = max(1, _int(a.get("days"), 1))
    end_date = a.get("end_date") or ""
    only = a.get("province") or ""
    try:
        _config, _common = _load_project_modules()
        provinces = _common.load_provinces()
    except Exception as exc:
        return {"error": f"加载项目模块失败：{exc}"}
    if only:
        provinces = [p for p in provinces if p["province"] == only]
        if not provinces:
            return {"error": f"未知区域：{only}", "hint": "用 list_regions 看可用区域原名"}

    try:
        end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)
    except ValueError:
        return {"error": f"end_date 格式不对：{end_date}", "hint": "要 YYYY-MM-DD"}
    window = [(end - timedelta(days=i)).isoformat() for i in range(days)][::-1]

    # 当日数据绝不落盘。实时市场滚动出清，当天写进去的是残缺数据，而 build_outputs
    # 见到有点数就会把该区域日标成 available；`run.py daily`（collect --last-days 3，
    # 不带 --refresh-days）只补 missing/failed，于是这份残缺数据会被**永久钉死**。
    # 要看当天就用 fetch_live_price，它不落盘。
    today = date.today().isoformat()
    if window[-1] >= today:
        return {
            "error": f"拒绝同步当日及以后的数据（请求到 {window[-1]}，今天是 {today}）",
            "reason": "当日实时市场尚未出清完，落盘会被标成 available 并被后续增量采集跳过，"
                      "残缺数据将永久留在数据仓里",
            "hint": f"想看当天用 fetch_live_price（不落盘）；要补采请把 end_date 设成 "
                    f"{(date.today() - timedelta(days=1)).isoformat()} 或更早",
        }

    planned = len(provinces) * len(window)
    if planned > SYNC_MAX_REQUESTS:
        return {
            "error": f"本次要发 {planned} 个请求，超过上限 {SYNC_MAX_REQUESTS}",
            "hint": f"缩小范围：指定 province，或把 days 降到 {max(1, SYNC_MAX_REQUESTS // len(provinces))} 以内",
            "planned_requests": planned,
        }

    raw_dir = RAW
    detail_path = raw_dir / _common.DETAIL_FILE
    quality_path = raw_dir / _common.QUALITY_FILE
    collected_at = _common.now_stamp()
    ok = failed = points = 0
    token_error = None
    per_day = []

    for prov in provinces:
        for d in window:
            if token_error:
                break
            try:
                rows = _api_rows(prov, d)
            except TokenError as exc:
                token_error = str(exc)
                break
            except Exception as exc:
                failed += 1
                _common.append_csv(quality_path, _common.QUALITY_FIELDS, [{
                    "province": prov["province"], "province_code": prov["province_code"],
                    "trade_date": d, "status": "failed", "point_count": 0,
                    "error": str(exc)[:300], "collected_at": collected_at,
                }])
                per_day.append({"province": prov["province"], "date": d, "status": "failed", "error": str(exc)[:120]})
                continue
            # 字段顺序与 BOM 全部交给 append_csv，别自己拼 CSV
            _common.append_csv(detail_path, _common.DETAIL_FIELDS, [{
                "province": prov["province"], "province_code": prov["province_code"],
                "province_type": prov["province_type"], "trade_date": d,
                "time_slot": r[TIME],
                "day_ahead_price": "" if r[DA] is None else r[DA],
                "real_time_price": "" if r[RT] is None else r[RT],
                "unit": _common.PRICE_UNIT, "collected_at": collected_at,
            } for r in rows])
            _common.append_csv(quality_path, _common.QUALITY_FIELDS, [{
                "province": prov["province"], "province_code": prov["province_code"],
                "trade_date": d, "status": "available" if rows else "empty",
                "point_count": len(rows), "error": "", "collected_at": collected_at,
            }])
            ok += 1
            points += len(rows)
            per_day.append({"province": prov["province"], "date": d,
                            "status": "available" if rows else "empty", "points": len(rows)})
            time.sleep(0.2)  # 与 collect.py 同样的礼貌延时，别把接口打急了

    rebuilt = False
    meta = {}
    if ok or failed:
        try:
            start, stop = _common.resolve_range(raw_dir, window[0], window[-1])
            meta = _common.build_outputs(raw_dir, _common.load_provinces(), start, stop)
            rebuilt = True
        except Exception as exc:
            return {"error": f"数据已落盘但汇总重建失败：{exc}",
                    "hint": "汇总表可能与明细不一致，请管理员跑 `python run.py export`",
                    "synced_days": ok, "new_points": points}
    _invalidate_cache()  # 数据变了，索引必须重建，否则后续查询读到旧数

    out = {
        "requested": {"province": only or "全部", "days": days,
                      "window": {"from": window[0], "to": window[-1]}},
        "planned_requests": planned,
        "succeeded": ok, "failed": failed, "new_points": points,
        "summary_rebuilt": rebuilt,
        "warehouse_last_date": _warehouse_last_date() or None,
        "detail": per_day[:60],
        "note": "只补采并重建 CSV 汇总，不做 JSON/Excel/周报导出（那些要管理员跑 run.py export/weekly）。",
    }
    if meta:
        out["coverage"] = meta.get("coverage")
        out["detail_rows"] = meta.get("detail_rows")
    if token_error:
        out["error"] = token_error
        out["error_kind"] = "token"
        out["note"] = "令牌失效导致提前中断，已采到的部分仍已落盘。"
    return out


def t_get_data_status(_: dict) -> dict:
    r = _run_project(["status"], timeout=120)
    meta = {}
    mp = RAW / "metadata.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass

    # metadata.json 里的 coverage 来自 quality.csv 的 available 标记，只数
    # 「这个区域日有没有采到东西」，数不出「采到的东西里有多少是假 0」。
    # 实测 coverage 报 99.75%，同时另有 149 个区域日部分时点是缺失的——
    # 只看前者会以为数据近乎完美。两个口径都给出来，并说清差别。
    _load_detail()
    by_province = defaultdict(lambda: {"days": 0, "points": 0})
    for (province, _d), n in _zero_tail_masked.items():
        by_province[province]["days"] += 1
        by_province[province]["points"] += n
    worst = sorted(by_province.items(), key=lambda kv: -kv[1]["days"])
    expected = meta.get("expected_region_days") or 0
    incomplete_days = len(_zero_tail_masked)

    return {
        "command_output": r,
        "metadata": meta,
        "masked_zero_quality": {
            "incomplete_region_days": incomplete_days,
            "missing_points_total": sum(_zero_tail_masked.values()),
            "share_of_region_days": round(incomplete_days / expected, 4) if expected else None,
            "by_province": [
                {"province": p, "days": v["days"], "missing_points": v["points"]}
                for p, v in worst[:15]
            ],
            "reason": "这些区域日在数据仓里有一部分时点写着 0（接口对无数据返回数字 0），"
                      "本 server 读取时已判为缺失并掩掉，不计入任何统计",
            "caveat": "metadata.coverage 数的是「区域日有没有采到数据」，"
                      "不反映这里的部分缺失——两个数不冲突，口径不同",
            "fix": "跑 python run.py collect --refresh-missing-prices 定向重采这些区域日",
        },
    }


def t_update_daily(_: dict) -> dict:
    # 每日增量：采最近 3 天 + 导出。真实耗时通常几分钟。
    r = _run_project(["daily"], timeout=1500)
    global _detail_cache
    _detail_cache = None  # 采完数据变了，缓存必须失效，否则后续查询读到旧数
    return r


def t_get_weekly_report(_: dict) -> dict:
    d = PROJECT / "data" / "exports" / "reports"
    if not d.exists():
        return {"error": "还没有周报目录", "path": str(d)}
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"error": "还没有生成过周报，可先让管理员跑 run.py weekly"}
    latest = files[0]
    return {
        "file": latest.name,
        "modified": datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "content": latest.read_text(encoding="utf-8")[:12000],
    }


_PROV = {"type": "string", "description": "价格区域名，如 山西/山东/蒙西/河北南网。必须用 list_regions 返回的原名"}
_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_FIELD = {"type": "string", "enum": ["real_time", "day_ahead"], "description": "默认 real_time"}

TOOLS = [
    ("list_regions",
     "列出全部 29 个价格区域及其数据覆盖区间、全期均价与价差。想知道有哪些地区、数据到哪天，先调这个。",
     {"type": "object", "properties": {}}, t_list_regions),
    ("get_price_curve",
     "取某地区某一天的分时电价曲线（日前+实时）。默认最多返回 96 个点，更细的粒度会等间隔抽稀，统计量仍按全量算。",
     {"type": "object", "properties": {"province": _PROV, "date": _DATE,
      "max_points": {"type": "integer", "description": "最多返回多少个时点，默认 96，上限 288"}},
      "required": ["province", "date"]}, t_get_price_curve),
    ("get_daily_summary",
     "某地区某日的关键指标：日前/实时均价、最高最低、平均价差、尖峰与低谷时刻、负电价点数。日常问答优先用这个而不是取全曲线。",
     {"type": "object", "properties": {"province": _PROV, "date": _DATE},
      "required": ["province", "date"]}, t_get_daily_summary),
    ("compare_regions",
     "同一天多个地区的电价横向对比，按实时均价从高到低排序。",
     {"type": "object", "properties": {"provinces": {"type": "array", "items": {"type": "string"}}, "date": _DATE},
      "required": ["provinces", "date"]}, t_compare_regions),
    ("get_price_trend",
     "某地区最近 N 天的日度均价与价差走势，用于看趋势、找异常日。",
     {"type": "object", "properties": {"province": _PROV,
      "days": {"type": "integer", "description": "默认 7"}, "end_date": _DATE},
      "required": ["province"]}, t_get_price_trend),
    ("find_extremes",
     "扫描某地区近 N 天的极端价格：negative=负电价时点，spike=超过阈值的尖峰。用于风险与套利分析。",
     {"type": "object", "properties": {"province": _PROV,
      "kind": {"type": "string", "enum": ["negative", "spike"], "description": "默认 negative"},
      "days": {"type": "integer", "description": "默认 30"},
      "threshold": {"type": "number", "description": "spike 阈值，默认 1000"}, "end_date": _DATE},
      "required": ["province"]}, t_find_extremes),
    ("rank_spread",
     "某一天全网各区域的日前/实时价差排行（实时均价-日前均价，从高到低）。用于找当日哪些省实时紧张、哪些省日前报高了。",
     {"type": "object", "properties": {"date": _DATE,
      "top": {"type": "integer", "description": "只返回前 N 名，默认全部"}},
      "required": ["date"]}, t_rank_spread),
    ("get_peak_valley",
     "某地区最近 N 天的日内峰谷特征：峰谷时刻、峰谷差、峰谷比。用于评估储能/可调负荷的日内套利空间。",
     {"type": "object", "properties": {"province": _PROV,
      "days": {"type": "integer", "description": "默认 7"}, "end_date": _DATE, "field": _FIELD},
      "required": ["province"]}, t_get_peak_valley),
    ("get_hourly_profile",
     "某地区连续 N 天里每个时点的价格分布（均值/中位/P10/P90/标准差），并给出典型高价时段、低价时段和波动最大的时点。用于摸清该省的典型日内形态。",
     {"type": "object", "properties": {"province": _PROV,
      "days": {"type": "integer", "description": "默认 14"}, "end_date": _DATE, "field": _FIELD},
      "required": ["province"]}, t_get_hourly_profile),
    ("fetch_live_price",
     "【查最新】绕过本地数据仓，直接请求电查查接口取某区域某日的分时电价。"
     "当用户问「今天/昨天/最新」的电价，或数据仓的 freshness 显示滞后时用这个。不落盘。",
     {"type": "object", "properties": {"province": _PROV, "date": _DATE,
      "max_points": {"type": "integer", "description": "最多返回多少个时点，默认 96，上限 288"}},
      "required": ["province", "date"]}, t_fetch_live_price),
    ("sync_days",
     "【补数据】轻量增量采集：拉取指定范围并落盘进数据仓、重建汇总。"
     f"比 update_daily 细粒度（不做 Excel/周报导出）。单次最多 {SYNC_MAX_REQUESTS} 个请求，会消耗接口配额。",
     {"type": "object", "properties": {
      "province": {"type": "string", "description": "只补这一个区域；省略则全部 29 个"},
      "days": {"type": "integer", "description": "往前补几天，默认 1"},
      "end_date": {"type": "string", "description": "截止日 YYYY-MM-DD，默认昨天"}},
      "required": []}, t_sync_days),
    ("get_data_status",
     "查询数据仓覆盖区间、各区域完整度与采集令牌状态。数据查不到时先用它排查。",
     {"type": "object", "properties": {}}, t_get_data_status),
    ("update_daily",
     "触发每日增量采集（补采最近 3 天并重新导出）。耗时数分钟且会消耗接口配额，只在用户明确要求更新数据时调用。",
     {"type": "object", "properties": {}}, t_update_daily),
    ("get_weekly_report",
     "读取最新一份电价周报全文（含全网概览、区域排行、价格特征）。",
     {"type": "object", "properties": {}}, t_get_weekly_report),
]
HANDLERS = {name: fn for name, _, _, fn in TOOLS}


def handle(req: dict):
    mid, method = req.get("id"), req.get("method")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "power-price", "version": "1.1.0"},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS
        ]}}
    # 我们没声明 resources/prompts 能力，但有些客户端仍会探测；回空表比回错误干净。
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            result = fn(params.get("arguments") or {})
        except Exception as exc:  # 工具异常不能打死整个 server
            log(f"tool {name} failed: {exc!r}")
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": isinstance(result, dict) and "error" in result,
        }}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    log(f"start, project={PROJECT}")
    if not DETAIL.exists():
        log(f"WARNING: 明细数据不存在 {DETAIL}，数据类工具会返回错误")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
