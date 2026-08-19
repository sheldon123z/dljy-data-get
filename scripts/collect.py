#!/usr/bin/env python3
"""逐区域、逐日采集日前与实时电价，写入 CSV 数据仓，可反复执行。

再次运行同一条命令只会补采缺口（missing / failed），已确认的
available / empty 会被跳过，因此可以安全地放进每日定时任务。
"""
from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    BASE_URL,
    DETAIL_FILE,
    DETAIL_FIELDS,
    DETAIL_URL,
    PRICE_UNIT,
    QUALITY_FIELDS,
    QUALITY_FILE,
    API_USER_AGENT,
    ApiAuthError,
    ApiRateLimited,
    append_csv,
    build_outputs,
    raise_for_api_status,
    date_strings,
    iter_csv,
    load_provinces,
    now_stamp,
    pending_tasks,
    read_csv,
    resolve_range,
    select_provinces,
    shift_days,
    today_iso,
    write_csv,
)

THREAD_LOCAL = threading.local()

USER_AGENT = API_USER_AGENT   # 唯一定义在 common.py，避免两处漂移


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="采集日前与实时电价（支持断点续采）")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD；省略则沿用仓库区间")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD；省略则用昨天")
    parser.add_argument(
        "--last-days",
        type=int,
        help="只处理最近 N 天（以 --end 或昨天为终点），常用于每日增量",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=0,
        help="强制重采最近 N 天，用于接口事后补录价格的情况",
    )
    parser.add_argument(
        "--refresh-missing-prices",
        action="store_true",
        help="只重采日前或实时价格字段为空的区域日，不影响价格完整的区域日",
    )
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--province-file", type=Path)
    parser.add_argument("--workers", type=int, default=3, help="并发数，建议 1–4")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=40)
    parser.add_argument("--delay", type=float, default=0.2, help="每个请求结束后的延时秒数")
    parser.add_argument("--limit", type=int, help="本次最多请求多少个区域日，用于试跑")
    parser.add_argument("--skip-failed", action="store_true", help="本次跳过历史 failed 项")
    parser.add_argument("--only-provinces", nargs="*", help="仅采集指定区域名称/代码/拼音")
    parser.add_argument("--dry-run", action="store_true", help="只统计待采任务，不发请求")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- HTTP

def connection(timeout: int):
    cached = getattr(THREAD_LOCAL, "connection", None)
    if cached is None:
        parsed = urlparse(BASE_URL)
        cached = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        THREAD_LOCAL.connection = cached
    return cached


def close_connection():
    cached = getattr(THREAD_LOCAL, "connection", None)
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    THREAD_LOCAL.connection = None


def request_json(token: str, payload: dict, retries: int, timeout: int):
    path = DETAIL_URL.removeprefix(BASE_URL)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": token,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            conn = connection(timeout)
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            raise_for_api_status(response.status)
            result = json.loads(raw.decode("utf-8"))
            if result.get("code") != 200:
                raise RuntimeError(
                    f"API code {result.get('code')}: {result.get('message') or result.get('msg')}"
                )
            return result
        except ApiAuthError as exc:
            # 令牌失效，重试无意义。转成 PermissionError 保持上层既有的处理路径
            close_connection()
            raise PermissionError(str(exc)) from exc
        except ApiRateLimited:
            # 限流时重试是火上浇油，直接抛给上层降频
            close_connection()
            raise
        except Exception as exc:
            last_error = exc
            close_connection()
            if attempt >= retries:
                break
            time.sleep(min(10, 0.8 * (2**attempt)))
    raise RuntimeError(str(last_error))


def fetch_one(province: dict, trade_date: str, token: str, args):
    collected_at = now_stamp()
    payload = {
        "areaCode": province["province_code"],
        "startDate": trade_date,
        "endDate": trade_date,
    }
    try:
        result = request_json(token, payload, args.retries, args.timeout)
        rows = []
        for item in result.get("data") or []:
            da = item.get("avgDayAheadPrice")
            rt = item.get("avgRealTimePrice")
            rows.append(
                {
                    "province": province["province"],
                    "province_code": province["province_code"],
                    "province_type": province["province_type"],
                    "trade_date": trade_date,
                    "time_slot": item.get("time96") or "",
                    "day_ahead_price": "" if da is None else float(da),
                    "real_time_price": "" if rt is None else float(rt),
                    "unit": PRICE_UNIT,
                    "collected_at": collected_at,
                }
            )
        quality = {
            "province": province["province"],
            "province_code": province["province_code"],
            "trade_date": trade_date,
            "status": "available" if rows else "empty",
            "point_count": len(rows),
            "error": "",
            "collected_at": collected_at,
        }
    except Exception as exc:
        rows = []
        quality = {
            "province": province["province"],
            "province_code": province["province_code"],
            "trade_date": trade_date,
            "status": "failed",
            "point_count": 0,
            "error": str(exc)[:300],
            "collected_at": collected_at,
        }
    finally:
        if args.delay > 0:
            time.sleep(args.delay)
    return rows, quality


# ---------------------------------------------------------------- 任务编排

def drop_recent_quality(raw_dir: Path, cutoff: str) -> int:
    """删除 cutoff 及之后的质量记录，使这些日期在本次运行中被重采。"""
    quality_path = raw_dir / QUALITY_FILE
    rows = read_csv(quality_path)
    if not rows:
        return 0
    kept = [row for row in rows if row["trade_date"] < cutoff]
    removed = len(rows) - len(kept)
    if removed:
        write_csv(quality_path, QUALITY_FIELDS, kept)
    return removed


def resolve_dates(args, raw_dir: Path) -> tuple[str, str, list[str]]:
    end = args.end or shift_days(today_iso(), -1)
    if args.last_days:
        start = shift_days(end, -(args.last_days - 1))
    else:
        start = args.start
    if not start:
        start, _ = resolve_range(raw_dir, None, end)
    window = date_strings(start, end)
    # 汇总口径覆盖仓库全历史，而不只是本次窗口
    repo_start, repo_end = resolve_range(raw_dir, start, end)
    return repo_start, repo_end, window


def _is_blank_price(value: str) -> bool:
    return not (value or "").strip()


def _as_float(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def missing_price_tasks(raw_dir: Path, provinces: list[dict], window: list[str]) -> list[tuple[dict, str]]:
    """找出明细中价格残缺的区域日，作为定向重采任务。

    不以 quality.csv 的 available 状态为准，因为接口可能返回完整时点但其中
    的日前价或实时价为空；这种情况需要保留区域日状态并单独补拉价格字段。

    两种残缺都要认：
      · **空字段**——接口没给这一列；
      · **整列全是 0**——接口对无数据时段返回的是数字 0 而不是 null，
        落盘后看着"有值"，`available` 也照标，于是增量采集永远跳过它们，
        这批脏数据就一直留在仓里（实测 149 个区域日、6683 个点）。
        判据取"该列非空值全为 0"：真实出清不可能整天零——日前是前一天
        排好的完整曲线，实时的零价只出现在午间光伏过剩的窗口，晚高峰不可能是 0。
        只有个别时点为 0 的日子不算，那可能是真实的地板价。
    """
    selected = {province["province_code"]: province for province in provinces}
    window_set = set(window)
    incomplete: set[tuple[str, str]] = set()
    # (code, date) -> {列名: [该列出现过的非空值...]}，用来判断某列是不是全零
    seen: dict[tuple[str, str], dict[str, list[str]]] = {}

    for row in iter_csv(raw_dir / DETAIL_FILE):
        code, trade_date = row["province_code"], row["trade_date"]
        if code not in selected or trade_date not in window_set:
            continue
        key = (code, trade_date)
        da, rt = row.get("day_ahead_price"), row.get("real_time_price")
        if _is_blank_price(da) or _is_blank_price(rt):
            incomplete.add(key)
        bucket = seen.setdefault(key, {"day_ahead_price": [], "real_time_price": []})
        if not _is_blank_price(da):
            bucket["day_ahead_price"].append(da.strip())
        if not _is_blank_price(rt):
            bucket["real_time_price"].append(rt.strip())

    for key, cols in seen.items():
        for values in cols.values():
            if values and all(_as_float(v) == 0.0 for v in values):
                incomplete.add(key)
                break

    # 剔除两类"重采也补不回来"的区域日，否则每天的定时任务都在白跑。
    # 实测全量窗口下 949 个待采里有 598 个属于这两类（63%）。
    incomplete -= _drop_unrecoverable(raw_dir, selected, window_set, incomplete)

    return [(selected[code], trade_date) for code, trade_date in sorted(incomplete, key=lambda item: (item[1], item[0]))]


def _is_evenly_gridded(slots: list[str]) -> bool:
    """有值的时点是否按固定间隔铺满全天——那是粒度差异，不是缺失。

    江西的日前价是 96 点（15 分钟）、实时价是 288 点（5 分钟），对齐到同一张
    明细表后，日前在非整刻的时点上必然为空。全年 201 天天天如此，
    却被"字段为空"判据全部当成待补，白采一整年。
    """
    if len(slots) < 3:
        return False
    mins = sorted(_slot_minutes(s) for s in slots)
    if mins[0] < 0:
        return False
    step = mins[1] - mins[0]
    if step <= 0:
        return False
    return all(b - a == step for a, b in zip(mins, mins[1:]))


def _slot_minutes(slot: str) -> int:
    try:
        hh, mm = slot.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return -1


def _drop_unrecoverable(raw_dir: Path, selected: dict, window_set: set, candidates: set) -> set:
    """从待补候选里筛掉「重采也拿不回来」的区域日。

    不是所有"字段为空"都值得重采。实测全量窗口下，原判据给出 949 个待补，
    其中绝大多数属于下面三类，重采纯属浪费接口配额：

    1. **结构性缺失**——该区域该列压根没有这个市场的价格：四川、蒙西的日前价
       在整个窗口 100% 为空（203 天 / 194 天），数据源就没有。
    2. **粒度差异**——江西日前 96 点(15min)、实时 288 点(5min)，对齐到一张表后
       日前在非整刻时点上必然为空，全年 201 天天天如此。见 _is_evenly_gridded。
    3. **假零**——接口对无数据时段返回数字 0。2026-08-19 实测重采 183 个区域日，
       139 个补到了真数据，但 149 个假零区域日**一个都没修好**：接口至今返回的
       仍是全 0（黑龙江 2026-08-07、吉林 2026-08-10 复核过），数据源就是没有。

    真正"没采到"的区域日由 quality.csv 的 missing/failed 状态负责，走 pending_tasks。
    """
    if not candidates:
        return set()

    per_day: dict[tuple[str, str], dict] = {}
    province_totals: dict[str, dict[str, int]] = {}

    for row in iter_csv(raw_dir / DETAIL_FILE):
        code, trade_date = row["province_code"], row["trade_date"]
        if code not in selected or trade_date not in window_set:
            continue
        # 该省该列在整个窗口里出现过多少个值——用来识别"这个省根本没有这个市场"
        acc = province_totals.setdefault(code, {"day_ahead_price": 0, "real_time_price": 0})
        filled = {}
        for field in ("day_ahead_price", "real_time_price"):
            ok = not _is_blank_price(row.get(field))
            filled[field] = ok
            if ok:
                acc[field] += 1
        if (code, trade_date) not in candidates:
            continue
        bucket = per_day.setdefault(
            (code, trade_date),
            {"day_ahead_price": [], "real_time_price": [], "total": 0},
        )
        bucket["total"] += 1
        for field, ok in filled.items():
            if ok:
                bucket[field].append(row.get("time_slot", ""))

    def has_recoverable_gap(code: str, field: str, slots: list, total: int) -> bool:
        if province_totals.get(code, {}).get(field, 0) == 0:
            return False                     # 该省该列全窗口无值：数据源没有这个市场
        if not slots:
            return True                      # 这天一个值都没有，但别的天有 → 可能是漏采
        if len(slots) >= total:
            return False                     # 这一列是满的
        if _is_evenly_gridded(slots):
            return False                     # 等间隔铺满：粒度差异，不是缺失
        return True

    unrecoverable = set()
    for key, cols in per_day.items():
        code = key[0]
        if not any(
            has_recoverable_gap(code, field, cols[field], cols["total"])
            for field in ("day_ahead_price", "real_time_price")
        ):
            unrecoverable.add(key)
    return unrecoverable


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 1 <= args.workers <= 16:
        raise SystemExit("--workers 必须在 1–16 之间，建议 1–4")

    config.ensure_dirs()
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)

    provinces = select_provinces(load_provinces(args.province_file), args.only_provinces)
    repo_start, repo_end, window = resolve_dates(args, raw_dir)

    if args.refresh_days > 0:
        cutoff = shift_days(window[-1], -(args.refresh_days - 1))
        removed = drop_recent_quality(raw_dir, cutoff)
        print(f"强制重采 {cutoff} 起的数据，已清除 {removed} 条质量记录", flush=True)

    tasks = pending_tasks(raw_dir, provinces, window, include_failed=not args.skip_failed)
    if args.refresh_missing_prices:
        repair_tasks = missing_price_tasks(raw_dir, provinces, window)
        existing = {(province["province_code"], trade_date) for province, trade_date in tasks}
        tasks.extend(task for task in repair_tasks if (task[0]["province_code"], task[1]) not in existing)
        print(f"检测到 {len(repair_tasks)} 个价格字段缺失的区域日，已加入定向重采队列", flush=True)
    total = len(provinces) * len(window)
    print(
        f"窗口 {window[0]} ~ {window[-1]}｜区域 {len(provinces)}｜目标区域日 {total}｜"
        f"已完成 {total - len(tasks)}｜待采 {len(tasks)}",
        flush=True,
    )

    if args.limit and len(tasks) > args.limit:
        tasks = tasks[: args.limit]
        print(f"按 --limit 截断为 {len(tasks)} 个任务", flush=True)

    if args.dry_run:
        by_province: dict[str, int] = {}
        for province, _ in tasks:
            by_province[province["province"]] = by_province.get(province["province"], 0) + 1
        for name, count in sorted(by_province.items(), key=lambda item: -item[1]):
            print(f"  {name}: {count} 天")
        return 0

    if not tasks:
        print("窗口内没有待采任务，直接重建汇总。", flush=True)

    token = ""
    if tasks:
        token = config.load_token()
        if not token:
            raise SystemExit(
                f"未找到令牌。请把刷新后的 Authorization 写入 {config.ENV_FILE}"
                f"（{config.TOKEN_KEY}=…），或设置同名环境变量。"
            )

    detail_path = raw_dir / "electricity_price_detail.csv"
    quality_path = raw_dir / QUALITY_FILE
    succeeded = failed = points = 0
    auth_error = None

    if tasks:
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(fetch_one, province, trade_date, token, args): (
                        province,
                        trade_date,
                    )
                    for province, trade_date in tasks
                }
                for index, future in enumerate(as_completed(futures), 1):
                    rows, quality = future.result()
                    append_csv(detail_path, DETAIL_FIELDS, rows)
                    append_csv(quality_path, QUALITY_FIELDS, [quality])
                    points += len(rows)
                    if quality["status"] == "failed":
                        failed += 1
                        if "鉴权失败" in quality["error"]:
                            auth_error = quality["error"]
                    else:
                        succeeded += 1
                    if index % 20 == 0 or index == len(tasks):
                        print(
                            f"进度 {index}/{len(tasks)}｜成功 {succeeded}｜失败 {failed}｜"
                            f"新增点数 {points}",
                            flush=True,
                        )
        except KeyboardInterrupt:
            print("\n已中断，已采到的数据保留在仓库中，重新运行即可续采。", flush=True)

    metadata = build_outputs(
        raw_dir,
        load_provinces(args.province_file),
        repo_start,
        repo_end,
        collection_status="complete_or_resumable",
    )
    print(
        f"仓库状态：{metadata['detail_rows']} 个分时点，"
        f"{metadata['available_region_days']}/{metadata['expected_region_days']} 个有数据区域日"
        f"（覆盖率 {metadata['coverage']:.1%}）"
    )
    if auth_error:
        print(f"注意：{auth_error}，请更新令牌后重跑。", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
