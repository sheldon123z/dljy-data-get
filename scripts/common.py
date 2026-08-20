#!/usr/bin/env python3
"""数据仓公共逻辑：CSV 读写、日期工具、汇总重建。"""
from __future__ import annotations

import csv
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

try:  # POSIX 有 fcntl；Windows 用 msvcrt 实现等价的跨进程文件锁
    import fcntl
except ImportError:  # pragma: no cover - 仅 Windows 走这里
    fcntl = None
    import msvcrt

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

BASE_URL = "https://elecheck.aienertech.cn"
PROVINCE_URL = f"{BASE_URL}/electricCheckApi/home/list/province"
DETAIL_URL = f"{BASE_URL}/electricCheckApi/queryData/clearPrice/detail"

PRICE_UNIT = "元/MWh"

# 小程序 UA。换掉可能被接口侧识别拦截，所以**只在这里定义一份**——
# 以前 collect.py 和 mcp/power_price_mcp.py 各写各的，迟早漂移。
API_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20 MiniProgramEnv/Mac"
)


class ApiAuthError(RuntimeError):
    """令牌失效。要人工换令牌，重试多少次都没用。"""


class ApiRateLimited(RuntimeError):
    """接口限流。重试只会让情况更糟，必须退到上层降频。"""


def raise_for_api_status(status: int) -> None:
    """把 HTTP 状态翻译成"要不要重试"这个决定。

    两类必须和普通错误区分开：
      · 401/403 —— 令牌失效，重试无意义，得人去抓包换令牌；
      · 429     —— 限流，重试是火上浇油，得降频。

    以前只有 MCP 侧单独认 429，collect.py 把它混在 `status >= 400` 里
    指数退避重试 4 次。同一个接口两套脾气，这里收敛成一份。
    """
    if status in (401, 403):
        raise ApiAuthError(f"鉴权失败：HTTP {status}，令牌可能已过期")
    if status == 429:
        raise ApiRateLimited("接口限流 HTTP 429，请降低并发或加大 --delay 后重试")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")

DETAIL_FIELDS = [
    "province",
    "province_code",
    "province_type",
    "trade_date",
    "time_slot",
    "day_ahead_price",
    "real_time_price",
    "unit",
    "collected_at",
]
QUALITY_FIELDS = [
    "province",
    "province_code",
    "trade_date",
    "status",
    "point_count",
    "error",
    "collected_at",
]

DETAIL_FILE = "electricity_price_detail.csv"
QUALITY_FILE = "quality.csv"
DAILY_FILE = "daily_summary.csv"
PROVINCE_SUMMARY_FILE = "province_summary.csv"
METADATA_FILE = "metadata.json"

# 视为“不必重采”的状态
DONE_STATUSES = {"available", "empty"}

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# ---------------------------------------------------------------- 日期

def to_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value).strip()[:10])


def date_strings(start_text, end_text) -> list[str]:
    start = to_date(start_text)
    end = to_date(end_text)
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    values = []
    current = start
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def today_iso() -> str:
    return date.today().isoformat()


def shift_days(value, days: int) -> str:
    return (to_date(value) + timedelta(days=days)).isoformat()


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- CSV

def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def iter_csv(path: Path) -> Iterator[dict[str, str]]:
    """流式读取，明细表几十万行时避免一次性占满内存。"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


class DataLockBusy(RuntimeError):
    """数据仓写锁被别人占着。调用方应当告诉用户"稍后再试"，而不是硬写。"""


_WRITE_LOCK = threading.RLock()
_lock_depth = 0
_lock_handle = None


def _try_lock_exclusive(handle) -> bool:
    """尝试非阻塞加排他锁。POSIX 用 flock，Windows 用 msvcrt.locking。"""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _release_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def data_lock(raw_dir: Path, timeout: float = 120.0):
    """数据仓的跨进程写锁。

    冲突是真实存在的，不是理论风险：
      · automation/daily_update.sh 每天 09:30 采集并跑 build_outputs，
        后者会整体重写 quality / daily_summary / province_summary / metadata；
      · MCP 的 sync_days 由 agent 调用，**用户随时可以触发**，走同一套写入。
    两边撞上，轻则统计表被写乱，重则 append 交错——一次 96 行的明细约 8–10KB，
    超过 PIPE_BUF 的 4096 字节，操作系统不保证整块原子写入。

    读方不需要这把锁：write_csv 是「写临时文件 + rename」的原子替换，
    读到的要么是旧版本要么是新版本，不会是半截。

    进程内用 RLock 允许重入（同一线程里 build_outputs 套 append_csv 不会自锁），
    进程间用 flock（Windows 上为 msvcrt.locking）轮询等待，超时抛
    DataLockBusy 而不是无限等下去。
    """
    global _lock_depth, _lock_handle
    with _WRITE_LOCK:
        if _lock_depth == 0:
            raw_dir.mkdir(parents=True, exist_ok=True)
            handle = (raw_dir / ".write.lock").open("w")
            deadline = time.monotonic() + timeout
            while True:
                if _try_lock_exclusive(handle):
                    break
                if time.monotonic() >= deadline:
                    handle.close()
                    raise DataLockBusy(
                        f"数据仓正被另一个进程写入（等待 {timeout:.0f}s 未获得锁）。"
                        "通常是每日采集任务在跑，稍后重试即可。"
                    )
                time.sleep(0.2)
            _lock_handle = handle
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
            if _lock_depth == 0 and _lock_handle is not None:
                _release_lock(_lock_handle)
                _lock_handle.close()
                _lock_handle = None


def write_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    # 原子替换：读方要么看到旧版本要么看到新版本，不会读到半截文件
    temp_path.replace(path)


def append_csv(path: Path, fields: list[str], rows: Iterable[dict]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with data_lock(path.parent):
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()


# ---------------------------------------------------------------- 数值

def as_number(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mean(values: list[float]) -> float | str:
    return sum(values) / len(values) if values else ""


def round_or_blank(value, digits: int = 2):
    if value in ("", None):
        return ""
    return round(float(value), digits)


# ---------------------------------------------------------------- 区域

def load_provinces(path: Path | None = None) -> list[dict[str, str]]:
    source = path or config.PROVINCE_FILE
    rows = read_csv(source)
    required = {"province", "province_code", "province_type"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"区域代码文件无效：{source}")
    return rows


def select_provinces(provinces: list[dict], wanted: list[str] | None) -> list[dict]:
    if not wanted:
        return provinces
    selected = {item.strip() for item in wanted if item.strip()}
    picked = [
        row
        for row in provinces
        if row["province"] in selected
        or row["province_code"] in selected
        or row["province_type"] in selected
    ]
    if not picked:
        raise SystemExit(f"未匹配到任何区域：{sorted(selected)}")
    return picked


# ---------------------------------------------------------------- 仓库范围

def repo_range(raw_dir: Path) -> tuple[str | None, str | None]:
    """返回仓库已声明的采集区间，取自 metadata.json。"""
    meta_path = raw_dir / METADATA_FILE
    if not meta_path.exists():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, None
    return meta.get("start_date"), meta.get("end_date")


def resolve_range(raw_dir: Path, start: str | None, end: str | None) -> tuple[str, str]:
    """把新区间与仓库既有区间求并集，保证汇总永远覆盖全部历史。"""
    known_start, known_end = repo_range(raw_dir)
    candidates_start = [value for value in (start, known_start) if value]
    candidates_end = [value for value in (end, known_end) if value]
    if not candidates_start or not candidates_end:
        raise SystemExit("无法确定采集区间：请显式传入 --start / --end")
    return min(candidates_start), max(candidates_end)


# ---------------------------------------------------------------- 汇总重建

DAILY_FIELDS = [
    "province",
    "province_code",
    "trade_date",
    "point_count",
    "day_ahead_avg",
    "real_time_avg",
    "day_ahead_min",
    "day_ahead_max",
    "real_time_min",
    "real_time_max",
    "spread_avg",
]
PROVINCE_FIELDS = [
    "province",
    "province_code",
    "province_type",
    "requested_days",
    "available_days",
    "empty_days",
    "failed_days",
    "missing_days",
    "coverage",
    "dominant_points_per_day",
    "irregular_point_days",
    "point_count",
    "day_ahead_avg",
    "real_time_avg",
    "day_ahead_min",
    "day_ahead_max",
    "real_time_min",
    "real_time_max",
    "spread_avg",
    "negative_realtime_points",
    "zero_realtime_points",
    "first_date",
    "last_date",
]


def build_outputs(
    output_dir: Path,
    provinces: list[dict[str, str]],
    start_text: str,
    end_text: str,
    collection_status: str = "complete_or_resumable",
) -> dict:
    """按明细表重算 quality / daily_summary / province_summary / metadata。

    明细以 (区域代码, 交易日, 时点) 去重，后写入的记录覆盖先写入的，
    因此 collect.py 可以放心地追加写。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # 一次重写 quality / daily_summary / province_summary / metadata 四个文件，
    # 对外必须表现为一个步骤——中途被另一个进程插进来写明细，重算结果就是错的
    with data_lock(output_dir):
        return _build_outputs_locked(output_dir, provinces, start_text, end_text, collection_status)


def _build_outputs_locked(
    output_dir: Path,
    provinces: list[dict[str, str]],
    start_text: str,
    end_text: str,
    collection_status: str,
) -> dict:
    detail_path = output_dir / DETAIL_FILE
    quality_path = output_dir / QUALITY_FILE

    detail_by_key: dict[tuple[str, str, str], dict] = {}
    for row in iter_csv(detail_path):
        detail_by_key[(row["province_code"], row["trade_date"], row["time_slot"])] = row
    details = sorted(
        detail_by_key.values(),
        key=lambda row: (row["province_code"], row["trade_date"], row["time_slot"]),
    )
    write_csv(detail_path, DETAIL_FIELDS, details)
    del detail_by_key

    quality_by_key: dict[tuple[str, str], dict] = {}
    for row in read_csv(quality_path):
        quality_by_key[(row["province_code"], row["trade_date"])] = row

    points_by_day = Counter((row["province_code"], row["trade_date"]) for row in details)
    now = now_stamp()
    all_dates = date_strings(start_text, end_text)
    for province in provinces:
        code = province["province_code"]
        for trade_date in all_dates:
            key = (code, trade_date)
            count = points_by_day[key]
            existing = quality_by_key.get(key)
            if count:
                quality_by_key[key] = {
                    "province": province["province"],
                    "province_code": code,
                    "trade_date": trade_date,
                    "status": "available",
                    "point_count": count,
                    "error": "",
                    "collected_at": (existing or {}).get("collected_at", now),
                }
            elif not existing:
                quality_by_key[key] = {
                    "province": province["province"],
                    "province_code": code,
                    "trade_date": trade_date,
                    "status": "missing",
                    "point_count": 0,
                    "error": "尚未采集；缺失不按 0 处理。",
                    "collected_at": "",
                }
            elif existing["status"] == "available":
                # 明细里已经没有这一天了（例如被手工删除），状态需要退回
                existing["status"] = "missing"
                existing["point_count"] = 0
    quality = sorted(
        quality_by_key.values(), key=lambda row: (row["province_code"], row["trade_date"])
    )
    write_csv(quality_path, QUALITY_FIELDS, quality)

    by_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_province: dict[str, list[dict]] = defaultdict(list)
    for row in details:
        by_day[(row["province_code"], row["trade_date"])].append(row)
        by_province[row["province_code"]].append(row)

    province_lookup = {row["province_code"]: row for row in provinces}
    daily_rows = []
    for (code, trade_date), rows in sorted(by_day.items()):
        da = [value for value in (as_number(row["day_ahead_price"]) for row in rows) if value is not None]
        rt = [value for value in (as_number(row["real_time_price"]) for row in rows) if value is not None]
        da_avg = mean(da)
        rt_avg = mean(rt)
        daily_rows.append(
            {
                "province": province_lookup.get(code, {}).get("province", rows[0]["province"]),
                "province_code": code,
                "trade_date": trade_date,
                "point_count": len(rows),
                "day_ahead_avg": round_or_blank(da_avg),
                "real_time_avg": round_or_blank(rt_avg),
                "day_ahead_min": round_or_blank(min(da) if da else ""),
                "day_ahead_max": round_or_blank(max(da) if da else ""),
                "real_time_min": round_or_blank(min(rt) if rt else ""),
                "real_time_max": round_or_blank(max(rt) if rt else ""),
                "spread_avg": round_or_blank(rt_avg - da_avg) if da and rt else "",
            }
        )
    write_csv(output_dir / DAILY_FILE, DAILY_FIELDS, daily_rows)

    quality_groups: dict[str, list[dict]] = defaultdict(list)
    for row in quality:
        quality_groups[row["province_code"]].append(row)

    province_rows = []
    for province in provinces:
        code = province["province_code"]
        rows = by_province[code]
        qrows = quality_groups[code]
        statuses = Counter(row["status"] for row in qrows)
        counts = [int(row["point_count"]) for row in qrows if row["status"] == "available"]
        dominant = Counter(counts).most_common(1)[0][0] if counts else 0
        da = [value for value in (as_number(row["day_ahead_price"]) for row in rows) if value is not None]
        rt = [value for value in (as_number(row["real_time_price"]) for row in rows) if value is not None]
        dates_seen = sorted({row["trade_date"] for row in rows})
        da_avg = mean(da)
        rt_avg = mean(rt)
        province_rows.append(
            {
                "province": province["province"],
                "province_code": code,
                "province_type": province["province_type"],
                "requested_days": len(all_dates),
                "available_days": statuses["available"],
                "empty_days": statuses["empty"],
                "failed_days": statuses["failed"],
                "missing_days": statuses["missing"],
                "coverage": round(statuses["available"] / len(all_dates), 4) if all_dates else 0,
                "dominant_points_per_day": dominant,
                "irregular_point_days": sum(value != dominant for value in counts),
                "point_count": len(rows),
                "day_ahead_avg": round_or_blank(da_avg),
                "real_time_avg": round_or_blank(rt_avg),
                "day_ahead_min": round_or_blank(min(da) if da else ""),
                "day_ahead_max": round_or_blank(max(da) if da else ""),
                "real_time_min": round_or_blank(min(rt) if rt else ""),
                "real_time_max": round_or_blank(max(rt) if rt else ""),
                "spread_avg": round_or_blank(rt_avg - da_avg) if da and rt else "",
                "negative_realtime_points": sum(value < 0 for value in rt),
                "zero_realtime_points": sum(value == 0 for value in rt),
                "first_date": dates_seen[0] if dates_seen else "",
                "last_date": dates_seen[-1] if dates_seen else "",
            }
        )
    write_csv(output_dir / PROVINCE_SUMMARY_FILE, PROVINCE_FIELDS, province_rows)
    write_csv(
        output_dir / "province_codes.csv",
        ["province", "province_code", "province_type"],
        provinces,
    )

    status_totals = Counter(row["status"] for row in quality)
    metadata = {
        "source": DETAIL_URL,
        "province_source": PROVINCE_URL,
        "start_date": start_text,
        "end_date": end_text,
        "requested_days": len(all_dates),
        "region_count": len(provinces),
        "detail_rows": len(details),
        "available_region_days": status_totals["available"],
        "empty_region_days": status_totals["empty"],
        "failed_region_days": status_totals["failed"],
        "missing_region_days": status_totals["missing"],
        "expected_region_days": len(all_dates) * len(provinces),
        "coverage": round(status_totals["available"] / (len(all_dates) * len(provinces)), 4)
        if provinces and all_dates
        else 0,
        "price_unit": PRICE_UNIT,
        "collection_status": collection_status,
        "updated_at": now,
        "credentials_stored": False,
    }
    (output_dir / METADATA_FILE).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata


def pending_tasks(
    raw_dir: Path,
    provinces: list[dict],
    dates: list[str],
    include_failed: bool = True,
) -> list[tuple[dict, str]]:
    """列出仍需请求的“区域 × 日”任务。"""
    done: set[tuple[str, str]] = set()
    for row in iter_csv(raw_dir / QUALITY_FILE):
        status = row["status"]
        if status in DONE_STATUSES:
            done.add((row["province_code"], row["trade_date"]))
        elif status == "failed" and not include_failed:
            done.add((row["province_code"], row["trade_date"]))
    return [
        (province, trade_date)
        for province in provinces
        for trade_date in dates
        if (province["province_code"], trade_date) not in done
    ]


def default_province_file() -> Path:
    return config.PROVINCE_FILE
