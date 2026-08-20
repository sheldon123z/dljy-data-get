#!/usr/bin/env python3
"""把数据仓打包成一个自包含 HTML 看板（无外部依赖、可离线打开）。

同一份模板也被 serve.py 复用：live=True 时页面会多出采集控制台。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    DAILY_FILE,
    DETAIL_FILE,
    METADATA_FILE,
    PROVINCE_SUMMARY_FILE,
    QUALITY_FILE,
    as_number,
    iter_csv,
    load_provinces,
    now_stamp,
    read_csv,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE = TEMPLATE_DIR / "dashboard.html"
ARTIFACT_TEMPLATE = TEMPLATE_DIR / "artifact.html"
STATUS_CODES = {"available": "A", "empty": "E", "failed": "F", "missing": "-"}
STATUS_LABELS = {"A": "有数据", "E": "接口无数据", "F": "请求失败", "-": "尚未采集"}
PRICE_BANDS = ("negative", "0_100", "100_200", "200_300", "300_400", "400_500", "500_plus")
QUARTER_HOUR_SLOTS = tuple(
    f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(15, 24 * 60 + 1, 15)
)

INT_KEYS = {
    "point_count",
    "requested_days",
    "available_days",
    "empty_days",
    "failed_days",
    "missing_days",
    "dominant_points_per_day",
    "irregular_point_days",
    "negative_realtime_points",
    "zero_realtime_points",
}
FLOAT_KEYS = {
    "coverage",
    "day_ahead_avg",
    "real_time_avg",
    "day_ahead_min",
    "day_ahead_max",
    "real_time_min",
    "real_time_max",
    "spread_avg",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成自包含 HTML 看板")
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--output", type=Path, help="默认 data/exports/看板.html")
    parser.add_argument("--title", default="全国现货电价看板")
    parser.add_argument(
        "--artifact",
        action="store_true",
        help="输出 Artifact 片段（无 doctype/html/body，供 claude.ai 发布），默认写到 data/exports/artifact.html",
    )
    return parser.parse_args(argv)


def typed(row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        if key in INT_KEYS:
            out[key] = int(float(value)) if value not in ("", None) else None
        elif key in FLOAT_KEYS:
            out[key] = as_number(value)
        else:
            out[key] = value
    return out


def slot_to_minutes(slot: str) -> int:
    """把 ``HH:MM`` 时点转为当日分钟数；24:00 允许作为当日终点。"""
    hour, minute = (int(part) for part in slot.split(":", 1))
    return hour * 60 + minute


def normalize_to_quarter_hours(entry: dict) -> dict:
    """把原始 24/48/96/288 点曲线标准化为 96 个 15 分钟时点。

    各省市场公布的分时粒度不同。少于 96 点时，将该结算时段的价格保持到
    下一个公布时点；多于 96 点时，按有效样本数加权汇总到所属 15 分钟时段。
    这样跨省叠加时横轴始终是同一组 96 个时点。
    """
    slots = entry.get("slots", [])
    if not slots:
        return entry

    source = []
    for index, slot in enumerate(slots):
        source.append(
            {
                "minute": slot_to_minutes(slot),
                "day_ahead": entry["day_ahead"][index],
                "real_time": entry["real_time"][index],
                "day_ahead_samples": entry["day_ahead_samples"][index],
                "real_time_samples": entry["real_time_samples"][index],
            }
        )
    source.sort(key=lambda item: item["minute"])
    source_points = len(source)
    starts_at_zero = source[0]["minute"] == 0

    result = {
        "slots": list(QUARTER_HOUR_SLOTS),
        "day_ahead": [],
        "real_time": [],
        "day_ahead_samples": [],
        "real_time_samples": [],
        "source_points": source_points,
    }

    def weighted(items: list[dict], price_key: str, sample_key: str) -> tuple[float | None, int]:
        valid = [item for item in items if item[price_key] is not None]
        if not valid:
            return None, 0
        weights = [max(1, int(item[sample_key] or 0)) for item in valid]
        value = sum(item[price_key] * weight for item, weight in zip(valid, weights)) / sum(weights)
        # 这里展示的是每个标准时点的平均有效日数，而不是下采样后累加的原始点数。
        samples = round(sum(weights) / len(weights))
        return round(value, 2), samples

    for target_slot in QUARTER_HOUR_SLOTS:
        target = slot_to_minutes(target_slot)
        if source_points >= 96:
            # 96 点原样落位；288 点等更细粒度数据按 (t-15, t] 聚合。
            if source_points == 96:
                candidates = [item for item in source if item["minute"] == target]
                if not candidates and starts_at_zero:
                    candidates = [item for item in source if item["minute"] == target - 15]
            else:
                candidates = [item for item in source if target - 15 < item["minute"] <= target]
        elif starts_at_zero:
            # 起始时点标记（如 00:00、01:00）：持有到下一个结算时点。
            candidates = [item for item in source if item["minute"] < target]
            candidates = candidates[-1:] if candidates else []
        else:
            # 终止时点标记（如 01:00、02:00）：向前填充该结算时段。
            candidates = [item for item in source if item["minute"] >= target]
            candidates = candidates[:1] if candidates else []

        value, samples = weighted(candidates, "day_ahead", "day_ahead_samples")
        result["day_ahead"].append(value)
        result["day_ahead_samples"].append(samples)
        value, samples = weighted(candidates, "real_time", "real_time_samples")
        result["real_time"].append(value)
        result["real_time_samples"].append(samples)
    return result


def build_profiles(raw_dir: Path) -> dict:
    """各区域各月的平均日内曲线。看板画典型日曲线用，比塞全量明细小两个数量级。"""
    sums: dict[tuple[str, str, str], list] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for row in iter_csv(raw_dir / DETAIL_FILE):
        key = (row["province_code"], row["trade_date"][:7], row["time_slot"])
        bucket = sums[key]
        da = as_number(row["day_ahead_price"])
        rt = as_number(row["real_time_price"])
        if da is not None:
            bucket[0] += da
            bucket[1] += 1
        if rt is not None:
            bucket[2] += rt
            bucket[3] += 1
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for (code, month, slot), (da_sum, da_n, rt_sum, rt_n) in sums.items():
        entry = grouped[code].setdefault(
            month,
            {"slots": [], "day_ahead": [], "real_time": [], "day_ahead_samples": [], "real_time_samples": []},
        )
        entry["slots"].append(slot)
        entry["day_ahead"].append(round(da_sum / da_n, 2) if da_n else None)
        entry["real_time"].append(round(rt_sum / rt_n, 2) if rt_n else None)
        entry["day_ahead_samples"].append(da_n)
        entry["real_time_samples"].append(rt_n)
    for months in grouped.values():
        for entry in months.values():
            order = sorted(range(len(entry["slots"])), key=lambda i: entry["slots"][i])
            entry["slots"] = [entry["slots"][i] for i in order]
            entry["day_ahead"] = [entry["day_ahead"][i] for i in order]
            entry["real_time"] = [entry["real_time"][i] for i in order]
            entry["day_ahead_samples"] = [entry["day_ahead_samples"][i] for i in order]
            entry["real_time_samples"] = [entry["real_time_samples"][i] for i in order]
            entry.update(normalize_to_quarter_hours(entry))
    return grouped


def build_rolling_profiles(raw_dir: Path, dates: list[str], windows: tuple[int, ...] = (7, 14, 30, 180)) -> dict:
    """按最近 N 个交易日汇总各区域的同一时点均价，供跨省日内曲线对比。"""
    date_sets = {window: set(dates[-window:]) for window in windows if dates[-window:]}
    sums: dict[int, dict[tuple[str, str], list]] = {
        window: defaultdict(lambda: [0.0, 0, 0.0, 0]) for window in date_sets
    }
    for row in iter_csv(raw_dir / DETAIL_FILE):
        trade_date = row["trade_date"]
        matches = [window for window, selected in date_sets.items() if trade_date in selected]
        if not matches:
            continue
        da = as_number(row["day_ahead_price"])
        rt = as_number(row["real_time_price"])
        key = (row["province_code"], row["time_slot"])
        for window in matches:
            bucket = sums[window][key]
            if da is not None:
                bucket[0] += da
                bucket[1] += 1
            if rt is not None:
                bucket[2] += rt
                bucket[3] += 1

    result: dict[str, dict] = {}
    for window, selected_dates in date_sets.items():
        profiles: dict[str, dict] = {}
        for (code, slot), (da_sum, da_n, rt_sum, rt_n) in sums[window].items():
            entry = profiles.setdefault(
                code,
                {"slots": [], "day_ahead": [], "real_time": [], "day_ahead_samples": [], "real_time_samples": []},
            )
            entry["slots"].append(slot)
            entry["day_ahead"].append(round(da_sum / da_n, 2) if da_n else None)
            entry["real_time"].append(round(rt_sum / rt_n, 2) if rt_n else None)
            entry["day_ahead_samples"].append(da_n)
            entry["real_time_samples"].append(rt_n)
        for entry in profiles.values():
            order = sorted(range(len(entry["slots"])), key=lambda i: entry["slots"][i])
            for key in ("slots", "day_ahead", "real_time", "day_ahead_samples", "real_time_samples"):
                entry[key] = [entry[key][i] for i in order]
            entry.update(normalize_to_quarter_hours(entry))
        result[str(window)] = {"dates": dates[-window:], "profiles": profiles}
    return result


def build_price_distribution(raw_dir: Path) -> dict:
    """按区域、月份压缩全量实时分时价格的区间计数，供看板画堆叠占比图。"""

    def empty_counts() -> dict:
        return {band: 0 for band in PRICE_BANDS}

    def band_of(value: float) -> str:
        if value < 0:
            return "negative"
        if value < 100:
            return "0_100"
        if value < 200:
            return "100_200"
        if value < 300:
            return "200_300"
        if value < 400:
            return "300_400"
        if value < 500:
            return "400_500"
        return "500_plus"

    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in iter_csv(raw_dir / DETAIL_FILE):
        value = as_number(row["real_time_price"])
        if value is None:
            continue
        code, month = row["province_code"], row["trade_date"][:7]
        counts = grouped[code].setdefault(month, empty_counts())
        counts[band_of(value)] += 1
    return grouped


def build_payload(raw_dir: Path, live: bool = False, session: str = "") -> dict:
    raw_dir = raw_dir.resolve()
    metadata = json.loads((raw_dir / METADATA_FILE).read_text(encoding="utf-8"))
    provinces = load_provinces()
    summary = [typed(row) for row in read_csv(raw_dir / PROVINCE_SUMMARY_FILE)]

    daily = read_csv(raw_dir / DAILY_FILE)
    dates = sorted({row["trade_date"] for row in daily})
    index = {day: position for position, day in enumerate(dates)}
    # 列式存储：每个区域一条与 dates 等长的数组，体积远小于逐行 JSON
    series: dict[str, dict] = {
        province["province_code"]: {"da": [None] * len(dates), "rt": [None] * len(dates)}
        for province in provinces
    }
    for row in daily:
        target = series.get(row["province_code"])
        if target is None:
            continue
        position = index[row["trade_date"]]
        target["da"][position] = as_number(row["day_ahead_avg"])
        target["rt"][position] = as_number(row["real_time_avg"])

    quality = read_csv(raw_dir / QUALITY_FILE)
    matrix: dict[str, dict[str, str]] = defaultdict(dict)
    quality_dates = sorted({row["trade_date"] for row in quality})
    for row in quality:
        matrix[row["province_code"]][row["trade_date"]] = STATUS_CODES.get(row["status"], "?")
    coverage = {
        "dates": quality_dates,
        "status_codes": STATUS_LABELS,
        "rows": [
            {
                "province": province["province"],
                "province_code": province["province_code"],
                "series": "".join(
                    matrix[province["province_code"]].get(day, "-") for day in quality_dates
                ),
            }
            for province in provinces
        ],
    }

    return {
        "meta": {
            **metadata,
            "provinces": [
                {
                    "province": row["province"],
                    "province_code": row["province_code"],
                    "province_type": row["province_type"],
                }
                for row in provinces
            ],
        },
        "generated_at": now_stamp(),
        "dates": dates,
        "series": series,
        "provinces": summary,
        "profiles": build_profiles(raw_dir),
        "rolling_profiles": build_rolling_profiles(raw_dir, dates),
        "price_distribution": build_price_distribution(raw_dir),
        "coverage": coverage,
        "live": live,
        "session": session,
    }


def latest_report() -> dict | None:
    """取最新一份周报 JSON，嵌进看板做「本周速览」。"""
    files = sorted(config.REPORT_DIR.glob("周报_*.json"))
    if not files:
        return None
    try:
        report = json.loads(files[-1].read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    keep = {
        "week_label", "start", "end", "generated_at", "now_rt", "now_da", "prev_rt", "prev_da",
        "region_count", "prev_region_count", "coverage", "available_days", "expected_days",
        "ranked", "all_regions", "extremes", "peak", "valley", "missing_provinces",
    }
    return {key: value for key, value in report.items() if key in keep}


def thin(payload: dict) -> dict:
    """把浮点压到 1 位小数。元/MWh 精确到 0.1 足够，体积能省三成。"""

    def walk(node):
        if isinstance(node, float):
            return round(node, 1)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(payload)


def render(payload: dict, title: str = "全国现货电价看板", artifact: bool = False) -> str:
    template = (ARTIFACT_TEMPLATE if artifact else TEMPLATE).read_text(encoding="utf-8")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现的 </script> 提前闭合脚本块
    body = body.replace("</", "<\\/")
    return template.replace("__TITLE__", title).replace("__PAYLOAD__", body)


def main(argv=None) -> int:
    args = parse_args(argv)
    payload = build_payload(args.raw_dir)
    if args.artifact:
        payload["report"] = latest_report()
        payload = thin(payload)
    output = (args.output or (config.EXPORT_DIR / ("artifact.html" if args.artifact else "看板.html"))).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(payload, args.title, args.artifact), encoding="utf-8")
    print(f"已生成 {output.relative_to(config.ROOT)}（{output.stat().st_size / 1024:.0f} KB）")
    if not args.artifact:
        print(f"直接用浏览器打开即可：file://{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
