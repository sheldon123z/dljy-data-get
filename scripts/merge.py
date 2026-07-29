#!/usr/bin/env python3
"""合并多个采集目录（分段跑、多台机器跑）到一个数据仓并去重。

明细合并键：province_code + trade_date + time_slot，后出现的输入目录优先。
质量表以“有结果”优先：available/empty 覆盖 failed/missing。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    DETAIL_FIELDS,
    DETAIL_FILE,
    DONE_STATUSES,
    QUALITY_FIELDS,
    QUALITY_FILE,
    build_outputs,
    load_provinces,
    read_csv,
    write_csv,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="合并多个采集目录并去重")
    parser.add_argument("--inputs", required=True, nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--start", help="省略则取合并结果中的最早日期")
    parser.add_argument("--end", help="省略则取合并结果中的最晚日期")
    parser.add_argument("--province-file", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    provinces = load_provinces(args.province_file)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    details: dict[tuple[str, str, str], dict] = {}
    quality: dict[tuple[str, str], dict] = {}
    for input_dir in args.inputs:
        input_dir = input_dir.resolve()
        detail_rows = read_csv(input_dir / DETAIL_FILE)
        for row in detail_rows:
            details[(row["province_code"], row["trade_date"], row["time_slot"])] = row
        for row in read_csv(input_dir / QUALITY_FILE):
            key = (row["province_code"], row["trade_date"])
            current = quality.get(key)
            if current is None or current["status"] not in DONE_STATUSES or row["status"] in DONE_STATUSES:
                quality[key] = row
        print(f"读取 {input_dir}：明细 {len(detail_rows)} 行", flush=True)

    if not details:
        raise SystemExit("所有输入目录都没有明细数据")

    write_csv(
        output_dir / DETAIL_FILE,
        DETAIL_FIELDS,
        sorted(
            details.values(),
            key=lambda row: (row["province_code"], row["trade_date"], row["time_slot"]),
        ),
    )
    write_csv(
        output_dir / QUALITY_FILE,
        QUALITY_FIELDS,
        sorted(quality.values(), key=lambda row: (row["province_code"], row["trade_date"])),
    )

    dates = sorted({row["trade_date"] for row in details.values()})
    metadata = build_outputs(
        output_dir,
        provinces,
        args.start or dates[0],
        args.end or dates[-1],
        collection_status="merged_resumable",
    )
    print(
        f"合并完成：{metadata['detail_rows']} 个分时点，"
        f"{metadata['available_region_days']}/{metadata['expected_region_days']} 个有数据区域日"
        f"（覆盖率 {metadata['coverage']:.1%}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
