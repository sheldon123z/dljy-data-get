#!/usr/bin/env python3
"""把 CSV 数据仓转成一组便于直接消费的 JSON / JSONL。

产物（默认写到 data/exports/json/）：
  meta.json        元数据、区域清单、字段中文标签
  provinces.json   区域汇总
  daily.json       逐日汇总
  coverage.json    区域 × 日 的采集状态矩阵
  profiles.json    各区域各月的平均日内曲线（看板画图用）
  latest.json      最新一天的全区域分时明细
  detail.jsonl     全量分时明细，一行一条，适合入库
  detail/<拼音>-<年月>.json  分区域分月明细，适合按需加载
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

ALL_PARTS = ["meta", "provinces", "daily", "coverage", "profiles", "latest", "jsonl", "detail"]

FIELD_LABELS = {
    "province": "区域",
    "province_code": "区域代码",
    "province_type": "区域拼音",
    "trade_date": "交易日期",
    "time_slot": "时点",
    "day_ahead_price": "日前价格",
    "real_time_price": "实时价格",
    "day_ahead_avg": "日前均价",
    "real_time_avg": "实时均价",
    "day_ahead_min": "日前最低",
    "day_ahead_max": "日前最高",
    "real_time_min": "实时最低",
    "real_time_max": "实时最高",
    "spread_avg": "实时-日前价差",
    "point_count": "分时点数",
    "coverage": "覆盖率",
    "status": "采集状态",
}

STATUS_LABELS = {
    "available": "有数据",
    "empty": "接口无数据",
    "failed": "请求失败",
    "missing": "尚未采集",
}

STATUS_CODES = {"available": "A", "empty": "E", "failed": "F", "missing": "-"}

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
    "day_ahead_price",
    "real_time_price",
    "day_ahead_avg",
    "real_time_avg",
    "day_ahead_min",
    "day_ahead_max",
    "real_time_min",
    "real_time_max",
    "spread_avg",
    "coverage",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="把 CSV 数据仓导出为 JSON")
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=config.JSON_DIR)
    parser.add_argument(
        "--parts",
        nargs="+",
        choices=ALL_PARTS + ["all"],
        default=["all"],
        help="只导出指定产物，默认全部",
    )
    parser.add_argument("--indent", type=int, default=0, help="缩进空格数，0 表示压缩输出")
    return parser.parse_args(argv)


def typed(row: dict) -> dict:
    """把 CSV 里的字符串还原成数字，空值统一成 None。"""
    out: dict = {}
    for key, value in row.items():
        if key in INT_KEYS:
            out[key] = int(float(value)) if value not in ("", None) else None
        elif key in FLOAT_KEYS:
            out[key] = as_number(value)
        else:
            out[key] = value
    return out


def dump(path: Path, payload, indent: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=indent or None, separators=None if indent else (",", ":")),
        encoding="utf-8",
    )
    return path


def main(argv=None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = set(ALL_PARTS if "all" in args.parts else args.parts)
    indent = args.indent

    metadata = json.loads((raw_dir / METADATA_FILE).read_text(encoding="utf-8"))
    provinces = load_provinces()
    province_summary = [typed(row) for row in read_csv(raw_dir / PROVINCE_SUMMARY_FILE)]
    daily = [typed(row) for row in read_csv(raw_dir / DAILY_FILE)]
    written: list[Path] = []

    if "provinces" in parts:
        written.append(dump(out_dir / "provinces.json", province_summary, indent))

    if "daily" in parts:
        written.append(dump(out_dir / "daily.json", daily, indent))

    quality = read_csv(raw_dir / QUALITY_FILE)
    dates = sorted({row["trade_date"] for row in quality})
    if "coverage" in parts:
        matrix: dict[str, dict[str, str]] = defaultdict(dict)
        for row in quality:
            matrix[row["province_code"]][row["trade_date"]] = STATUS_CODES.get(row["status"], "?")
        coverage = {
            "dates": dates,
            "status_codes": {code: STATUS_LABELS[name] for name, code in STATUS_CODES.items()},
            # 每个区域压成一条与 dates 等长的状态串，体积小、前端好画热力图
            "rows": [
                {
                    "province": province["province"],
                    "province_code": province["province_code"],
                    "province_type": province["province_type"],
                    "series": "".join(
                        matrix[province["province_code"]].get(day, "-") for day in dates
                    ),
                }
                for province in provinces
            ],
        }
        written.append(dump(out_dir / "coverage.json", coverage, indent))

    # 明细相关产物共用一次流式扫描
    need_detail = parts & {"profiles", "latest", "jsonl", "detail"}
    profile_sum: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    latest_date = max((row["trade_date"] for row in daily), default="")
    latest_rows: list[dict] = []
    by_bucket: dict[tuple[str, str], list[dict]] = defaultdict(list)
    jsonl_handle = None

    if need_detail:
        type_by_code = {row["province_code"]: row["province_type"] for row in provinces}
        if "jsonl" in parts:
            jsonl_path = out_dir / "detail.jsonl"
            jsonl_handle = jsonl_path.open("w", encoding="utf-8")
        for row in iter_csv(raw_dir / DETAIL_FILE):
            record = {
                "province": row["province"],
                "province_code": row["province_code"],
                "province_type": row["province_type"] or type_by_code.get(row["province_code"], ""),
                "trade_date": row["trade_date"],
                "time_slot": row["time_slot"],
                "day_ahead_price": as_number(row["day_ahead_price"]),
                "real_time_price": as_number(row["real_time_price"]),
            }
            if jsonl_handle is not None:
                jsonl_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if "profiles" in parts:
                key = (record["province_code"], record["trade_date"][:7], record["time_slot"])
                bucket = profile_sum[key]
                if record["day_ahead_price"] is not None:
                    bucket[0] += record["day_ahead_price"]
                    bucket[1] += 1
                if record["real_time_price"] is not None:
                    bucket[2] += record["real_time_price"]
                    bucket[3] += 1
            if "latest" in parts and record["trade_date"] == latest_date:
                latest_rows.append(record)
            if "detail" in parts:
                by_bucket[(record["province_code"], record["trade_date"][:7])].append(record)
        if jsonl_handle is not None:
            jsonl_handle.close()
            written.append(out_dir / "detail.jsonl")

    if "profiles" in parts:
        grouped: dict[str, dict[str, dict]] = defaultdict(dict)
        for (code, month, slot), (da_sum, da_n, rt_sum, rt_n) in profile_sum.items():
            entry = grouped[code].setdefault(month, {"slots": [], "day_ahead": [], "real_time": []})
            entry["slots"].append(slot)
            entry["day_ahead"].append(round(da_sum / da_n, 2) if da_n else None)
            entry["real_time"].append(round(rt_sum / rt_n, 2) if rt_n else None)
        for code, months in grouped.items():
            for month, entry in months.items():
                order = sorted(range(len(entry["slots"])), key=lambda i: entry["slots"][i])
                entry["slots"] = [entry["slots"][i] for i in order]
                entry["day_ahead"] = [entry["day_ahead"][i] for i in order]
                entry["real_time"] = [entry["real_time"][i] for i in order]
        written.append(dump(out_dir / "profiles.json", grouped, indent))

    if "latest" in parts:
        latest_rows.sort(key=lambda row: (row["province_code"], row["time_slot"]))
        written.append(
            dump(
                out_dir / "latest.json",
                {"trade_date": latest_date, "rows": latest_rows},
                indent,
            )
        )

    if "detail" in parts:
        detail_dir = out_dir / "detail"
        detail_dir.mkdir(parents=True, exist_ok=True)
        type_by_code = {row["province_code"]: row["province_type"] for row in provinces}
        index = []
        for (code, month), rows in sorted(by_bucket.items()):
            slug = type_by_code.get(code, code)
            name = f"{slug}-{month}.json"
            dump(detail_dir / name, rows, indent)
            index.append(
                {"province_code": code, "province_type": slug, "month": month, "file": f"detail/{name}", "rows": len(rows)}
            )
        written.append(dump(detail_dir / "index.json", index, indent))

    if "meta" in parts:
        meta = {
            **metadata,
            "generated_at": now_stamp(),
            "dates": {"first": dates[0] if dates else "", "last": dates[-1] if dates else ""},
            "latest_date": latest_date,
            "field_labels": FIELD_LABELS,
            "status_labels": STATUS_LABELS,
            "provinces": [
                {
                    "province": row["province"],
                    "province_code": row["province_code"],
                    "province_type": row["province_type"],
                }
                for row in provinces
            ],
            "files": sorted(path.name for path in written),
        }
        written.append(dump(out_dir / "meta.json", meta, indent))

    for path in sorted(set(written)):
        size = path.stat().st_size
        print(f"已生成 {path.relative_to(config.ROOT)}（{size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
