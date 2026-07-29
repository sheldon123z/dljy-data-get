#!/usr/bin/env python3
"""生成每周电价报告（Markdown + HTML）。

默认统计最近一个完整自然周（周一至周日），并与前一周做环比。
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from common import (  # noqa: E402
    DAILY_FILE,
    DETAIL_FILE,
    METADATA_FILE,
    QUALITY_FILE,
    as_number,
    date_strings,
    iter_csv,
    load_provinces,
    mean,
    now_stamp,
    read_csv,
    to_date,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成每周电价报告")
    parser.add_argument("--week", help="ISO 周，例如 2026-W30；优先于 --end")
    parser.add_argument("--end", help="统计周的最后一天 YYYY-MM-DD（该周周日）")
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=config.REPORT_DIR)
    parser.add_argument("--top", type=int, default=8, help="排行榜展示条数")
    return parser.parse_args(argv)


def resolve_week(args, available_dates: list[str]) -> tuple[date, date]:
    if args.week:
        year, _, week = args.week.upper().partition("-W")
        monday = date.fromisocalendar(int(year), int(week), 1)
        return monday, monday + timedelta(days=6)
    if args.end:
        end = to_date(args.end)
        monday = end - timedelta(days=end.weekday())
        return monday, monday + timedelta(days=6)
    if not available_dates:
        raise SystemExit("数据仓为空，无法生成周报")
    last = to_date(available_dates[-1])
    # 取最近一个已结束的完整周
    monday = last - timedelta(days=last.weekday()) - timedelta(days=7)
    if last.weekday() == 6:
        monday += timedelta(days=7)
    return monday, monday + timedelta(days=6)


def window_stats(daily: list[dict], days: set[str]) -> dict[str, dict]:
    """按区域聚合窗口内的日均价。"""
    buckets: dict[str, dict] = defaultdict(
        lambda: {"da": [], "rt": [], "days": 0, "province": "", "code": ""}
    )
    for row in daily:
        if row["trade_date"] not in days:
            continue
        entry = buckets[row["province_code"]]
        entry["province"] = row["province"]
        entry["code"] = row["province_code"]
        entry["days"] += 1
        da = as_number(row["day_ahead_avg"])
        rt = as_number(row["real_time_avg"])
        if da is not None:
            entry["da"].append(da)
        if rt is not None:
            entry["rt"].append(rt)
    result = {}
    for code, entry in buckets.items():
        da_avg = mean(entry["da"])
        rt_avg = mean(entry["rt"])
        result[code] = {
            "province": entry["province"],
            "province_code": code,
            "days": entry["days"],
            "day_ahead_avg": round(da_avg, 2) if da_avg != "" else None,
            "real_time_avg": round(rt_avg, 2) if rt_avg != "" else None,
            "real_time_min": round(min(entry["rt"]), 2) if entry["rt"] else None,
            "real_time_max": round(max(entry["rt"]), 2) if entry["rt"] else None,
            "spread": round(rt_avg - da_avg, 2) if da_avg != "" and rt_avg != "" else None,
        }
    return result


def intraday_profile(raw_dir: Path, days: set[str]) -> list[dict]:
    """窗口内全网平均日内曲线。

    各区域粒度不同（24/48/96 点），像 00:15 这类时点只有 96 点区域才有。
    直接平均会让稀疏时点被少数区域主导，因此按样本量过滤掉不具代表性的点。
    """
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for row in iter_csv(raw_dir / DETAIL_FILE):
        if row["trade_date"] not in days:
            continue
        bucket = sums[row["time_slot"]]
        da = as_number(row["day_ahead_price"])
        rt = as_number(row["real_time_price"])
        if da is not None:
            bucket[0] += da
            bucket[1] += 1
        if rt is not None:
            bucket[2] += rt
            bucket[3] += 1
    if not sums:
        return []
    peak_samples = max(max(values[1], values[3]) for values in sums.values())
    threshold = peak_samples * 0.6
    return [
        {
            "time_slot": slot,
            "day_ahead": round(values[0] / values[1], 2) if values[1] else None,
            "real_time": round(values[2] / values[3], 2) if values[3] else None,
            "samples": max(values[1], values[3]),
        }
        for slot, values in sorted(sums.items())
        if max(values[1], values[3]) >= threshold
    ]


def price_extremes(raw_dir: Path, days: set[str]) -> dict:
    lowest = highest = None
    negative = zero = total = 0
    for row in iter_csv(raw_dir / DETAIL_FILE):
        if row["trade_date"] not in days:
            continue
        rt = as_number(row["real_time_price"])
        if rt is None:
            continue
        total += 1
        if rt < 0:
            negative += 1
        elif rt == 0:
            zero += 1
        item = {
            "province": row["province"],
            "trade_date": row["trade_date"],
            "time_slot": row["time_slot"],
            "price": rt,
        }
        if lowest is None or rt < lowest["price"]:
            lowest = item
        if highest is None or rt > highest["price"]:
            highest = item
    return {
        "lowest": lowest,
        "highest": highest,
        "negative_points": negative,
        "zero_points": zero,
        "total_points": total,
    }


def delta_text(current, previous) -> str:
    if current is None or previous is None:
        return "—"
    diff = current - previous
    if previous == 0:
        return f"{diff:+.2f}"
    return f"{diff:+.2f}（{diff / abs(previous):+.1%}）"


def fmt(value, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.2f}{suffix}"


# ---------------------------------------------------------------- 渲染

def render_markdown(ctx: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 全国现货电价周报 · {ctx['week_label']}")
    add("")
    add(f"统计区间：{ctx['start']} ~ {ctx['end']}｜生成时间：{ctx['generated_at']}｜单位：元/MWh")
    add("")
    add("## 一、本周概览")
    add("")
    add("| 指标 | 本周 | 上周 | 变化 |")
    add("| --- | ---: | ---: | ---: |")
    add(
        f"| 全网日前均价 | {fmt(ctx['now_da'])} | {fmt(ctx['prev_da'])} | "
        f"{delta_text(ctx['now_da'], ctx['prev_da'])} |"
    )
    add(
        f"| 全网实时均价 | {fmt(ctx['now_rt'])} | {fmt(ctx['prev_rt'])} | "
        f"{delta_text(ctx['now_rt'], ctx['prev_rt'])} |"
    )
    add(f"| 有数据区域数 | {ctx['region_count']} | {ctx['prev_region_count']} | — |")
    add(f"| 区域日完整度 | {ctx['coverage']:.1%} | — | — |")
    add(f"| 分时样本点 | {ctx['extremes']['total_points']} | — | — |")
    add("")
    add("## 二、区域实时均价排行")
    add("")
    add("| # | 区域 | 实时均价 | 日前均价 | 价差 | 周内最低 | 周内最高 | 环比 |")
    add("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for index, item in enumerate(ctx["ranked"], 1):
        prev = ctx["prev_stats"].get(item["province_code"], {})
        add(
            f"| {index} | {item['province']} | {fmt(item['real_time_avg'])} | "
            f"{fmt(item['day_ahead_avg'])} | {fmt(item['spread'])} | "
            f"{fmt(item['real_time_min'])} | {fmt(item['real_time_max'])} | "
            f"{delta_text(item['real_time_avg'], prev.get('real_time_avg'))} |"
        )
    add("")
    add("## 三、价格特征")
    add("")
    extremes = ctx["extremes"]
    if extremes["highest"]:
        high = extremes["highest"]
        add(
            f"- 实时最高价：{high['price']:.2f}（{high['province']} "
            f"{high['trade_date']} {high['time_slot']}）"
        )
    if extremes["lowest"]:
        low = extremes["lowest"]
        add(
            f"- 实时最低价：{low['price']:.2f}（{low['province']} "
            f"{low['trade_date']} {low['time_slot']}）"
        )
    add(f"- 实时负价点：{extremes['negative_points']} 个；零价点：{extremes['zero_points']} 个")
    if ctx["peak"] and ctx["valley"]:
        add(
            f"- 全网日内高峰在 {ctx['peak']['time_slot']}（{ctx['peak']['real_time']:.2f}），"
            f"低谷在 {ctx['valley']['time_slot']}（{ctx['valley']['real_time']:.2f}），"
            f"峰谷差 {ctx['peak']['real_time'] - ctx['valley']['real_time']:.2f}"
        )
    add("")
    add("## 四、数据质量")
    add("")
    add(f"- 本周目标区域日：{ctx['expected_days']}，已采集 {ctx['available_days']}")
    if ctx["missing_provinces"]:
        add("- 仍有缺口的区域：")
        for name, count in ctx["missing_provinces"]:
            add(f"  - {name}：缺 {count} 天")
    else:
        add("- 本周全部区域日均已采集。")
    add("")
    add("---")
    add("")
    add("数据来源：电查查小程序公开接口；缺失值不按 0 处理；报告由 weekly_report.py 自动生成。")
    return "\n".join(lines) + "\n"


def sparkline_svg(series: list[dict], width: int = 720, height: int = 220) -> str:
    points = [item for item in series if item["real_time"] is not None]
    if len(points) < 2:
        return ""
    values_rt = [item["real_time"] for item in points]
    values_da = [item["day_ahead"] for item in points if item["day_ahead"] is not None]
    low = min(values_rt + (values_da or values_rt))
    high = max(values_rt + (values_da or values_rt))
    span = high - low or 1
    pad = 36

    def path_for(key: str) -> str:
        coords = []
        for index, item in enumerate(points):
            value = item[key]
            if value is None:
                continue
            x = pad + index * (width - pad * 2) / (len(points) - 1)
            y = height - pad - (value - low) / span * (height - pad * 2)
            coords.append(f"{x:.1f},{y:.1f}")
        return " ".join(coords)

    rt_path = path_for("real_time")
    da_path = path_for("day_ahead")
    labels = []
    step = max(1, len(points) // 8)
    for index in range(0, len(points), step):
        x = pad + index * (width - pad * 2) / (len(points) - 1)
        labels.append(
            f'<text x="{x:.1f}" y="{height - 10}" font-size="11" fill="#64748b" '
            f'text-anchor="middle">{html.escape(points[index]["time_slot"])}</text>'
        )
    return f"""<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="全网平均日内曲线">
  <rect x="0" y="0" width="{width}" height="{height}" fill="none"/>
  <line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#cbd5e1"/>
  <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" stroke="#cbd5e1"/>
  <text x="{pad}" y="{pad - 12}" font-size="11" fill="#64748b">{high:.0f}</text>
  <text x="{pad}" y="{height - pad + 14}" font-size="11" fill="#64748b">{low:.0f}</text>
  {'<polyline fill="none" stroke="#94a3b8" stroke-width="2" points="' + da_path + '"/>' if da_path else ''}
  <polyline fill="none" stroke="#1d4ed8" stroke-width="2.5" points="{rt_path}"/>
  {''.join(labels)}
</svg>"""


def render_html(ctx: dict, markdown_body: str) -> str:
    rows = []
    for index, item in enumerate(ctx["ranked"], 1):
        prev = ctx["prev_stats"].get(item["province_code"], {})
        rows.append(
            "<tr>"
            f"<td>{index}</td><td>{html.escape(item['province'])}</td>"
            f"<td class='num'>{fmt(item['real_time_avg'])}</td>"
            f"<td class='num'>{fmt(item['day_ahead_avg'])}</td>"
            f"<td class='num'>{fmt(item['spread'])}</td>"
            f"<td class='num'>{fmt(item['real_time_min'])}</td>"
            f"<td class='num'>{fmt(item['real_time_max'])}</td>"
            f"<td class='num'>{html.escape(delta_text(item['real_time_avg'], prev.get('real_time_avg')))}</td>"
            "</tr>"
        )
    missing = (
        "".join(f"<li>{html.escape(name)}：缺 {count} 天</li>" for name, count in ctx["missing_provinces"])
        or "<li>本周全部区域日均已采集。</li>"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全国现货电价周报 · {html.escape(ctx['week_label'])}</title>
<style>
  :root {{ color-scheme: light dark; --bg:#ffffff; --fg:#0f172a; --muted:#64748b; --line:#e2e8f0; --accent:#1d4ed8; --card:#f8fafc; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b1220; --fg:#e2e8f0; --muted:#94a3b8; --line:#1e293b; --accent:#60a5fa; --card:#111c30; }} }}
  body {{ margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--fg);
         font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
  main {{ max-width:960px; margin:0 auto; }}
  h1 {{ font-size:28px; margin:0 0 4px; }}
  h2 {{ font-size:19px; margin:36px 0 12px; padding-bottom:8px; border-bottom:1px solid var(--line); }}
  .meta {{ color:var(--muted); font-size:13px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:20px 0; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .card b {{ display:block; font-size:24px; font-weight:650; }}
  .card span {{ color:var(--muted); font-size:12px; }}
  .scroll {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
  th {{ color:var(--muted); font-weight:600; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  ul {{ padding-left:20px; }}
  footer {{ margin-top:40px; color:var(--muted); font-size:12px; }}
</style>
</head>
<body>
<main>
  <h1>全国现货电价周报 · {html.escape(ctx['week_label'])}</h1>
  <p class="meta">统计区间 {ctx['start']} ~ {ctx['end']}｜生成时间 {ctx['generated_at']}｜单位 元/MWh</p>
  <div class="cards">
    <div class="card"><span>全网实时均价</span><b>{fmt(ctx['now_rt'])}</b><span>环比 {html.escape(delta_text(ctx['now_rt'], ctx['prev_rt']))}</span></div>
    <div class="card"><span>全网日前均价</span><b>{fmt(ctx['now_da'])}</b><span>环比 {html.escape(delta_text(ctx['now_da'], ctx['prev_da']))}</span></div>
    <div class="card"><span>有数据区域</span><b>{ctx['region_count']}</b><span>上周 {ctx['prev_region_count']}</span></div>
    <div class="card"><span>区域日完整度</span><b>{ctx['coverage']:.0%}</b><span>{ctx['available_days']}/{ctx['expected_days']}</span></div>
  </div>
  <h2>全网平均日内曲线</h2>
  {sparkline_svg(ctx['profile'])}
  <p class="meta">深色为实时价，浅色为日前价。</p>
  <h2>区域实时均价排行</h2>
  <div class="scroll"><table>
    <thead><tr><th>#</th><th>区域</th><th class="num">实时均价</th><th class="num">日前均价</th>
    <th class="num">价差</th><th class="num">周内最低</th><th class="num">周内最高</th><th class="num">环比</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  <h2>数据质量</h2>
  <ul>{missing}</ul>
  <footer>数据来源：电查查小程序公开接口；缺失值不按 0 处理；报告由 weekly_report.py 自动生成。</footer>
</main>
</body>
</html>
"""


def main(argv=None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    daily = read_csv(raw_dir / DAILY_FILE)
    if not daily:
        raise SystemExit("daily_summary.csv 为空，请先采集或导入数据")
    available_dates = sorted({row["trade_date"] for row in daily})
    monday, sunday = resolve_week(args, available_dates)
    days = set(date_strings(monday, sunday))
    prev_days = set(date_strings(monday - timedelta(days=7), sunday - timedelta(days=7)))

    stats = window_stats(daily, days)
    prev_stats = window_stats(daily, prev_days)
    if not stats:
        raise SystemExit(f"{monday} ~ {sunday} 区间内没有数据，请换一个 --week")

    ranked = sorted(
        stats.values(),
        key=lambda item: (item["real_time_avg"] is None, -(item["real_time_avg"] or 0)),
    )[: args.top]

    provinces = load_provinces()
    quality = [row for row in read_csv(raw_dir / QUALITY_FILE) if row["trade_date"] in days]
    available_days = sum(row["status"] == "available" for row in quality)
    expected_days = len(provinces) * len(days)
    missing_counter: dict[str, int] = defaultdict(int)
    for row in quality:
        if row["status"] != "available":
            missing_counter[row["province"]] += 1

    profile = intraday_profile(raw_dir, days)
    valid_profile = [item for item in profile if item["real_time"] is not None]
    peak = max(valid_profile, key=lambda item: item["real_time"], default=None)
    valley = min(valid_profile, key=lambda item: item["real_time"], default=None)

    now_rt = mean([item["real_time_avg"] for item in stats.values() if item["real_time_avg"] is not None])
    now_da = mean([item["day_ahead_avg"] for item in stats.values() if item["day_ahead_avg"] is not None])
    prev_rt = mean([item["real_time_avg"] for item in prev_stats.values() if item["real_time_avg"] is not None])
    prev_da = mean([item["day_ahead_avg"] for item in prev_stats.values() if item["day_ahead_avg"] is not None])

    iso_year, iso_week, _ = monday.isocalendar()
    ctx = {
        "week_label": f"{iso_year}年第{iso_week}周",
        "start": monday.isoformat(),
        "end": sunday.isoformat(),
        "generated_at": now_stamp(),
        "now_rt": round(now_rt, 2) if now_rt != "" else None,
        "now_da": round(now_da, 2) if now_da != "" else None,
        "prev_rt": round(prev_rt, 2) if prev_rt != "" else None,
        "prev_da": round(prev_da, 2) if prev_da != "" else None,
        "region_count": len(stats),
        "prev_region_count": len(prev_stats),
        "coverage": available_days / expected_days if expected_days else 0,
        "available_days": available_days,
        "expected_days": expected_days,
        "ranked": ranked,
        "prev_stats": prev_stats,
        "extremes": price_extremes(raw_dir, days),
        "profile": profile,
        "peak": peak,
        "valley": valley,
        "missing_provinces": sorted(missing_counter.items(), key=lambda item: -item[1]),
    }

    slug = f"{iso_year}-W{iso_week:02d}"
    markdown = render_markdown(ctx)
    md_path = out_dir / f"周报_{slug}.md"
    html_path = out_dir / f"周报_{slug}.html"
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(render_html(ctx, markdown), encoding="utf-8")
    # JSON 里给出全部区域并附环比，方便看板/Artifact 直接生成完整报告
    full = []
    for item in sorted(
        stats.values(),
        key=lambda row: (row["real_time_avg"] is None, -(row["real_time_avg"] or 0)),
    ):
        previous = prev_stats.get(item["province_code"], {})
        full.append({
            **item,
            "prev_real_time_avg": previous.get("real_time_avg"),
            "prev_day_ahead_avg": previous.get("day_ahead_avg"),
            "delta_real_time": round(item["real_time_avg"] - previous["real_time_avg"], 2)
            if item["real_time_avg"] is not None and previous.get("real_time_avg") is not None
            else None,
        })
    json_path = out_dir / f"周报_{slug}.json"
    json_path.write_text(
        json.dumps(
            {
                **{key: value for key, value in ctx.items() if key != "prev_stats"},
                "all_regions": full,
                "profile": profile,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"已生成 {md_path.relative_to(config.ROOT)}")
    print(f"已生成 {html_path.relative_to(config.ROOT)}")
    print(f"已生成 {json_path.relative_to(config.ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
