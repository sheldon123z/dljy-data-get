#!/usr/bin/env python3
"""用 OpenAI 兼容接口生成指定时间窗口的现货电价总结。"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from analysis_agents import (  # noqa: E402
    AGENT_PROFILES,
    MODE_AGENTS,
    MODE_LABELS,
    agent_prompt,
    assess_reliability,
    auditor_prompt,
    build_evidence,
    deterministic_appendix,
    editor_prompt,
    validate_citations,
)
from common import DAILY_FILE, DETAIL_FILE, as_number, iter_csv, mean, now_stamp, read_csv  # noqa: E402

PROVIDER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.2",
    },
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="使用大模型生成现货电价总结")
    parser.add_argument("--days", type=int, default=7, help="总结最近 N 天，默认 7")
    parser.add_argument("--provider", choices=["deepseek", "glm", "custom"])
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument(
        "--agent-mode",
        choices=["quick", "standard", "rigorous"],
        default="standard",
        help="快速=2次模型调用，标准=5次，严格=7次",
    )
    parser.add_argument("--focus", default="", help="希望多 Agent 特别关注的问题")
    parser.add_argument("--raw-dir", type=Path, default=config.RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=config.REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="只生成提示词，不请求模型")
    return parser.parse_args(argv)


def resolve_model_config(args) -> dict[str, str]:
    stored = config.llm_config()
    provider = (args.provider or stored.get("provider") or "custom").lower()
    preset = PROVIDER_PRESETS.get(provider, {})
    resolved = {
        "provider": provider,
        "base_url": (args.base_url or stored.get("base_url") or preset.get("base_url") or "").rstrip("/"),
        "model": args.model or stored.get("model") or preset.get("model") or "",
        "api_key": args.api_key or stored.get("api_key") or "",
    }
    if args.dry_run:
        resolved["base_url"] = resolved["base_url"] or PROVIDER_PRESETS["deepseek"]["base_url"]
        resolved["model"] = resolved["model"] or PROVIDER_PRESETS["deepseek"]["model"]
    if not resolved["base_url"]:
        raise SystemExit("尚未配置 LLM Base URL")
    if not resolved["model"]:
        raise SystemExit("尚未配置模型名称")
    if not resolved["api_key"] and not args.dry_run:
        raise SystemExit("尚未配置 LLM API Key")
    return resolved


def average(values):
    result = mean([value for value in values if value is not None])
    return None if result == "" else round(result, 2)


def price_band(value: float) -> str:
    if value < 0:
        return "负价"
    if value < 100:
        return "0-100"
    if value < 200:
        return "100-200"
    if value < 300:
        return "200-300"
    if value < 400:
        return "300-400"
    if value < 500:
        return "400-500"
    return "500以上"


def build_context(raw_dir: Path, days: int) -> dict:
    if not 1 <= days <= 366:
        raise SystemExit("--days 必须在 1–366 之间")
    daily = read_csv(raw_dir / DAILY_FILE)
    if not daily:
        raise SystemExit("daily_summary.csv 为空，请先采集数据")
    available_dates = sorted({row["trade_date"] for row in daily})
    dates = available_dates[-days:]
    prior_dates = available_dates[max(0, len(available_dates) - len(dates) * 2):-len(dates)]
    current_set, prior_set = set(dates), set(prior_dates)
    buckets: dict[str, dict] = {}
    for row in daily:
        code = row["province_code"]
        entry = buckets.setdefault(
            code,
            {"province": row["province"], "rt": [], "da": [], "prior_rt": [], "days": 0},
        )
        if row["trade_date"] in current_set:
            entry["days"] += 1
            entry["rt"].append(as_number(row["real_time_avg"]))
            entry["da"].append(as_number(row["day_ahead_avg"]))
        elif row["trade_date"] in prior_set:
            entry["prior_rt"].append(as_number(row["real_time_avg"]))

    regions = []
    for code, entry in buckets.items():
        current_rt, prior_rt = average(entry["rt"]), average(entry["prior_rt"])
        valid_rt = [value for value in entry["rt"] if value is not None]
        regions.append(
            {
                "province": entry["province"],
                "province_code": code,
                "days": entry["days"],
                "real_time_avg": current_rt,
                "day_ahead_avg": average(entry["da"]),
                "change_vs_prior": round(current_rt - prior_rt, 2)
                if current_rt is not None and prior_rt is not None
                else None,
                "period_min": round(min(valid_rt), 2) if valid_rt else None,
                "period_max": round(max(valid_rt), 2) if valid_rt else None,
            }
        )
    regions.sort(key=lambda row: (row["real_time_avg"] is None, -(row["real_time_avg"] or 0)))

    bands: Counter[str] = Counter()
    lowest = highest = None
    for row in iter_csv(raw_dir / DETAIL_FILE):
        if row["trade_date"] not in current_set:
            continue
        value = as_number(row["real_time_price"])
        if value is None:
            continue
        bands[price_band(value)] += 1
        item = {
            "province": row["province"],
            "date": row["trade_date"],
            "time_slot": row["time_slot"],
            "price": value,
        }
        if lowest is None or value < lowest["price"]:
            lowest = item
        if highest is None or value > highest["price"]:
            highest = item
    total_points = sum(bands.values())
    distribution = {
        key: {
            "points": bands[key],
            "share": round(bands[key] / total_points * 100, 2) if total_points else 0,
        }
        for key in ("负价", "0-100", "100-200", "200-300", "300-400", "400-500", "500以上")
    }
    current_region_avgs = [row["real_time_avg"] for row in regions if row["real_time_avg"] is not None]
    prior_region_avgs = [
        average(entry["prior_rt"]) for entry in buckets.values() if average(entry["prior_rt"]) is not None
    ]
    return {
        "period": {"start": dates[0], "end": dates[-1], "days": len(dates)},
        "previous_period_days": len(prior_dates),
        "national": {
            "real_time_avg": average(current_region_avgs),
            "previous_real_time_avg": average(prior_region_avgs),
            "region_count": len(current_region_avgs),
            "detail_points": total_points,
        },
        "price_distribution": distribution,
        "extremes": {"lowest": lowest, "highest": highest},
        "regions": regions,
        "generated_at": now_stamp(),
    }


def completion_url(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def call_model(settings: dict[str, str], prompt: str, timeout: int = 120) -> tuple[str, dict]:
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": "你只依据用户提供的数据进行分析，不编造事实。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": 3000,
    }
    request = Request(
        completion_url(settings["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"模型接口 HTTP {exc.code}：{detail}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"模型接口请求失败：{exc}") from exc
    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError("模型接口响应中没有 choices[0].message.content") from exc
    return content, result.get("usage") or {}


def _call_agent(settings: dict[str, str], role: str, prompt: str) -> dict:
    content, usage = call_model(settings, prompt)
    return {
        "role": role,
        "name": AGENT_PROFILES.get(role, (role, ""))[0],
        "content": content,
        "usage": usage,
    }


def run_agent_team(
    settings: dict[str, str],
    context: dict,
    reliability: dict,
    evidence: list[dict],
    mode: str,
    focus: str,
) -> tuple[str, list[dict], dict, dict]:
    roles = MODE_AGENTS[mode]
    outputs: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, len(roles))) as executor:
        futures = {
            executor.submit(
                _call_agent,
                settings,
                role,
                agent_prompt(role, context, reliability, evidence, focus),
            ): role
            for role in roles
        }
        for future in as_completed(futures):
            outputs.append(future.result())
    outputs.sort(key=lambda item: roles.index(item["role"]))

    audit = ""
    audit_usage: dict = {}
    if mode != "quick":
        audit, audit_usage = call_model(settings, auditor_prompt(outputs, reliability, evidence))
        outputs.append(
            {
                "role": "auditor",
                "name": "独立审校 Agent",
                "content": audit,
                "usage": audit_usage,
            }
        )
    summary, editor_usage = call_model(
        settings,
        editor_prompt(outputs, audit, reliability, evidence, focus),
    )
    citation_validation = validate_citations(summary, evidence)
    summary = summary.rstrip() + "\n\n" + deterministic_appendix(
        reliability,
        evidence,
        citation_validation,
    )
    usage = {
        "agent_calls": len(outputs) + 1,
        "by_agent": {
            item["role"]: item.get("usage") or {}
            for item in outputs
        },
        "editor": editor_usage,
    }
    return summary, outputs, citation_validation, usage


def render_html(
    title: str,
    summary: str,
    metadata: dict,
    reliability: dict,
    agents: list[dict],
) -> str:
    reliability_class = "good" if reliability["grade"] == "A" else "warn" if reliability["grade"] in {"B", "C"} else "bad"
    caveats = "".join(f"<li>{html.escape(item)}</li>" for item in reliability["caveats"])
    agent_cards = "".join(
        f"<details><summary>{html.escape(item['name'])}</summary><pre>{html.escape(item['content'])}</pre></details>"
        for item in agents
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{margin:0;background:#f6f8fb;color:#172033;font:15px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:960px;margin:28px auto;background:#fff;padding:36px 44px;border:1px solid #e2e8f0;border-radius:12px}}
h1{{font-size:26px;margin:0 0 4px}}.meta{{color:#64748b;font-size:12px;margin-bottom:24px}}
pre{{font:inherit;white-space:pre-wrap;word-break:break-word;margin:0}}.quality{{border:1px solid #dbe3ef;border-left:5px solid #16a34a;padding:14px 18px;margin:18px 0;border-radius:8px}}
.quality.warn{{border-left-color:#f59e0b}}.quality.bad{{border-left-color:#dc2626}}.quality b{{font-size:22px}}.quality ul{{margin:6px 0}}
details{{border:1px solid #e2e8f0;border-radius:8px;margin-top:10px;padding:8px 12px}}summary{{cursor:pointer;font-weight:650}}
details pre{{margin-top:8px;color:#475569}}h2{{margin-top:28px}}@media print{{body{{background:#fff}}main{{margin:0;border:0}}}}
</style></head><body><main><h1>{html.escape(title)}</h1>
<p class="meta">模式：{html.escape(metadata["agent_mode_label"])}｜模型：{html.escape(metadata["model"])}｜生成时间：{html.escape(metadata["generated_at"])}</p>
<section class="quality {reliability_class}"><b>{reliability["grade"]}级 · {reliability["score"]}分</b>
<div>{html.escape(reliability["assessment"])}｜有效区域日覆盖 {reliability["coverage_rate"]}%｜实时分时时点 {reliability["finite_real_time_points"]}</div>
<ul>{caveats}</ul></section>
<pre>{html.escape(summary)}</pre>
<h2>Agent 工作记录</h2>{agent_cards}</main></body></html>"""


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = resolve_model_config(args)
    context = build_context(args.raw_dir.resolve(), args.days)
    reliability = assess_reliability(args.raw_dir.resolve(), context)
    evidence = build_evidence(context, reliability)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "agent_mode": args.agent_mode,
                    "agents": list(MODE_AGENTS[args.agent_mode]),
                    "reliability": reliability,
                    "evidence": evidence,
                    "sample_prompt": agent_prompt(
                        MODE_AGENTS[args.agent_mode][0],
                        context,
                        reliability,
                        evidence,
                        args.focus,
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    summary, agents, citation_validation, usage = run_agent_team(
        settings,
        context,
        reliability,
        evidence,
        args.agent_mode,
        args.focus.strip(),
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{context['period']['end'].replace('-', '')}_{context['period']['days']}天"
    title = f"全国现货电价 AI 总结 · {context['period']['start']} 至 {context['period']['end']}"
    metadata = {
        "provider": settings["provider"],
        "base_url": settings["base_url"],
        "model": settings["model"],
        "agent_mode": args.agent_mode,
        "agent_mode_label": MODE_LABELS[args.agent_mode],
        "agent_count": len(agents) + 1,
        "focus": args.focus.strip(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "usage": usage,
    }
    md_path = output_dir / f"AI总结_{slug}.md"
    html_path = output_dir / f"AI总结_{slug}.html"
    json_path = output_dir / f"AI总结_{slug}.json"
    md_path.write_text(f"# {title}\n\n{summary}\n", encoding="utf-8")
    html_path.write_text(
        render_html(title, summary, metadata, reliability, agents),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            {
                "title": title,
                "summary": summary,
                "metadata": metadata,
                "reliability": reliability,
                "evidence": evidence,
                "citation_validation": citation_validation,
                "agents": agents,
                "context": context,
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
