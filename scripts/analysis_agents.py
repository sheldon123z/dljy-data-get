#!/usr/bin/env python3
"""多角色分析所需的确定性证据、数据可靠性评分和提示词。"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from common import DETAIL_FILE, QUALITY_FILE, as_number, iter_csv, read_csv

EXPECTED_REGIONS = 29
AGENT_PROFILES = {
    "trend": (
        "趋势分析 Agent",
        "判断所选周期全国实时价格的方向、幅度、前后周期差异与波动，不解释未经提供的外部原因。",
    ),
    "regional": (
        "区域比较 Agent",
        "识别高低价格地区、变化较大的地区和区域分化，但不得把价格差异直接归因于负荷或新能源。",
    ),
    "distribution": (
        "价格分布 Agent",
        "分析负价及各100元/MWh价格区间占比、极端时点和长尾风险。",
    ),
    "skeptic": (
        "审慎质疑 Agent",
        "主动寻找缺失数据、异常值、平均数口径、样本覆盖和可能误导读者的表述。",
    ),
    "decision": (
        "决策建议 Agent",
        "把已验证的价格信号转化为下一周期监测清单，建议必须与证据强度匹配。",
    ),
}
MODE_AGENTS = {
    "quick": ("trend",),
    "standard": ("trend", "regional", "distribution"),
    "rigorous": ("trend", "regional", "distribution", "skeptic", "decision"),
}
MODE_LABELS = {"quick": "快速双 Agent", "standard": "标准五 Agent", "rigorous": "严格七 Agent"}


def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def assess_reliability(raw_dir: Path, context: dict) -> dict:
    """从原始明细与质量表计算独立于大模型的可信度。"""
    dates = context["period"]
    start, end = dates["start"], dates["end"]
    current_dates = {
        row["trade_date"]
        for row in read_csv(raw_dir / QUALITY_FILE)
        if start <= row.get("trade_date", "") <= end
    }
    quality = [
        row
        for row in read_csv(raw_dir / QUALITY_FILE)
        if row.get("trade_date") in current_dates
    ]
    status_counts = Counter(row.get("status") or "unknown" for row in quality)
    expected_region_days = EXPECTED_REGIONS * dates["days"]
    available_region_days = status_counts.get("available", 0)
    empty_region_days = status_counts.get("empty", 0)
    failed_region_days = status_counts.get("failed", 0)
    recorded_region_days = len({(row.get("province_code"), row.get("trade_date")) for row in quality})

    seen: set[tuple[str, str, str]] = set()
    duplicates = 0
    missing_rt = 0
    detail_rows = 0
    finite_rt = 0
    for row in iter_csv(raw_dir / DETAIL_FILE):
        trade_date = row.get("trade_date", "")
        if not start <= trade_date <= end:
            continue
        detail_rows += 1
        key = (row.get("province_code", ""), trade_date, row.get("time_slot", ""))
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
        if as_number(row.get("real_time_price")) is None:
            missing_rt += 1
        else:
            finite_rt += 1

    coverage = available_region_days / expected_region_days * 100 if expected_region_days else 0
    record_coverage = recorded_region_days / expected_region_days * 100 if expected_region_days else 0
    missing_rate = missing_rt / detail_rows * 100 if detail_rows else 100
    end_date = date.fromisoformat(end)
    freshness_days = max(0, (date.today() - timedelta(days=1) - end_date).days)

    score = 100.0
    score -= min(45, max(0, 100 - coverage) * 0.8)
    score -= min(20, failed_region_days * 1.5)
    score -= min(10, empty_region_days * 0.5)
    score -= min(10, missing_rate)
    score -= min(10, duplicates / max(1, detail_rows) * 1000)
    score -= min(15, freshness_days * 3)
    if not detail_rows:
        score -= 25
    if context.get("previous_period_days", 0) < context["period"]["days"]:
        score -= 5
    score = round(max(0, score), 1)
    if score >= 90:
        grade, assessment = "A", "可直接使用"
    elif score >= 75:
        grade, assessment = "B", "需附带说明"
    elif score >= 60:
        grade, assessment = "C", "需附带说明"
    else:
        grade, assessment = "D", "需修订后使用"

    caveats = []
    if coverage < 98:
        caveats.append(f"区域日有效覆盖率为 {coverage:.1f}%，并非完整覆盖。")
    if freshness_days:
        caveats.append(f"最新交易日距昨日相差 {freshness_days} 天。")
    if failed_region_days:
        caveats.append(f"质量表中有 {failed_region_days} 个区域日采集失败。")
    if empty_region_days:
        caveats.append(f"质量表中有 {empty_region_days} 个区域日为空。")
    if duplicates:
        caveats.append(f"发现 {duplicates} 条重复的区域-日期-时点键。")
    if missing_rate:
        caveats.append(f"实时价格缺失率为 {missing_rate:.2f}%。")
    if context.get("previous_period_days", 0) < context["period"]["days"]:
        caveats.append("前一对比周期不完整，环比结果只能作为方向参考。")
    if not caveats:
        caveats.append("未发现影响结论的明显覆盖、重复或时效性问题。")

    return {
        "score": score,
        "grade": grade,
        "assessment": assessment,
        "coverage_rate": round(coverage, 2),
        "record_coverage_rate": round(record_coverage, 2),
        "expected_region_days": expected_region_days,
        "available_region_days": available_region_days,
        "empty_region_days": empty_region_days,
        "failed_region_days": failed_region_days,
        "detail_rows": detail_rows,
        "finite_real_time_points": finite_rt,
        "missing_real_time_rate": round(missing_rate, 3),
        "duplicate_keys": duplicates,
        "freshness_days": freshness_days,
        "caveats": caveats,
    }


def build_evidence(context: dict, reliability: dict) -> list[dict]:
    """生成供所有 Agent 引用的稳定证据编号。"""
    national = context["national"]
    regions = [row for row in context["regions"] if row.get("real_time_avg") is not None]
    evidence = [
        {
            "id": "E01",
            "label": "统计周期",
            "value": f"{context['period']['start']} 至 {context['period']['end']}",
            "scope": f"{context['period']['days']} 个交易日",
            "source": "daily_summary.csv",
        },
        {
            "id": "E02",
            "label": "全国区域等权实时均价",
            "value": national.get("real_time_avg"),
            "unit": "元/MWh",
            "source": "daily_summary.csv",
        },
        {
            "id": "E03",
            "label": "前一周期全国区域等权实时均价",
            "value": national.get("previous_real_time_avg"),
            "unit": "元/MWh",
            "source": "daily_summary.csv",
        },
        {
            "id": "E04",
            "label": "有效区域日覆盖率",
            "value": reliability["coverage_rate"],
            "unit": "%",
            "source": "quality.csv",
        },
        {
            "id": "E05",
            "label": "有效实时分时时点",
            "value": reliability["finite_real_time_points"],
            "unit": "个",
            "source": "electricity_price_detail.csv",
        },
        {
            "id": "E06",
            "label": "数据可靠性评分",
            "value": reliability["score"],
            "unit": "分",
            "scope": f"{reliability['grade']}级 / {reliability['assessment']}",
            "source": "确定性质量检查",
        },
    ]
    if national.get("real_time_avg") is not None and national.get("previous_real_time_avg") is not None:
        evidence.append(
            {
                "id": "E07",
                "label": "全国实时均价较前一周期变化",
                "value": _round(national["real_time_avg"] - national["previous_real_time_avg"]),
                "unit": "元/MWh",
                "source": "daily_summary.csv",
            }
        )
    if regions:
        evidence.extend(
            [
                {
                    "id": "E08",
                    "label": "周期实时均价最高地区",
                    "value": regions[0]["real_time_avg"],
                    "unit": "元/MWh",
                    "scope": regions[0]["province"],
                    "source": "daily_summary.csv",
                },
                {
                    "id": "E09",
                    "label": "周期实时均价最低地区",
                    "value": regions[-1]["real_time_avg"],
                    "unit": "元/MWh",
                    "scope": regions[-1]["province"],
                    "source": "daily_summary.csv",
                },
            ]
        )
    for index, (band, item) in enumerate(context["price_distribution"].items(), start=10):
        evidence.append(
            {
                "id": f"E{index:02d}",
                "label": f"{band}价格时点占比",
                "value": item["share"],
                "unit": "%",
                "scope": f"{item['points']} 个时点",
                "source": "electricity_price_detail.csv",
            }
        )
    for label, key in (("最低实时价格时点", "lowest"), ("最高实时价格时点", "highest")):
        item = context["extremes"].get(key)
        if item:
            evidence.append(
                {
                    "id": f"E{len(evidence) + 1:02d}",
                    "label": label,
                    "value": item["price"],
                    "unit": "元/MWh",
                    "scope": f"{item['province']} {item['date']} {item['time_slot']}",
                    "source": "electricity_price_detail.csv",
                }
            )
    return evidence


def evidence_text(evidence: list[dict]) -> str:
    return "\n".join(
        f"[{item['id']}] {item['label']}：{item.get('value')} {item.get('unit', '')}"
        + (f"（{item['scope']}）" if item.get("scope") else "")
        + f"；来源：{item['source']}"
        for item in evidence
    )


def agent_prompt(role: str, context: dict, reliability: dict, evidence: list[dict], focus: str = "") -> str:
    name, assignment = AGENT_PROFILES[role]
    return f"""你是{name}。{assignment}

工作规则：
- 只能使用下面的结构化数据和证据表，不得补充未提供的事实。
- 每条重要结论至少引用一个证据编号，格式为 [E01]。
- 价格单位为元/MWh；缺失值不得按0处理。
- 相关性不得写成因果；涉及负荷、新能源出力、供需、天气、燃料或政策时，必须标注“需要外部数据验证”。
- 如果证据不足，应明确写“证据不足”，不要猜测。
- 输出不超过550字，按“发现 / 风险与限制 / 建议验证”组织。
{f"- 用户特别关注：{focus}" if focus else ""}

可靠性检查：
{json.dumps(reliability, ensure_ascii=False, separators=(",", ":"))}

证据表：
{evidence_text(evidence)}

完整结构化数据：
{json.dumps(context, ensure_ascii=False, separators=(",", ":"))}
"""


def auditor_prompt(agent_outputs: list[dict], reliability: dict, evidence: list[dict]) -> str:
    return f"""你是独立审校 Agent。请检查下列各分析 Agent 的文字是否存在：
1. 不在证据表中的数字；2. 不支持的因果解释；3. 忽略覆盖率或缺失；4. 平均口径混淆；
5. 引用不存在的证据编号；6. 结论强度超过数据可靠性。

只输出“可保留结论、必须修正、不可验证说法、最终写作约束”四部分，不超过650字。

可靠性：{json.dumps(reliability, ensure_ascii=False)}
证据表：
{evidence_text(evidence)}
Agent 草稿：
{json.dumps(agent_outputs, ensure_ascii=False)}
"""


def editor_prompt(
    agent_outputs: list[dict],
    audit: str,
    reliability: dict,
    evidence: list[dict],
    focus: str = "",
) -> str:
    return f"""你是报告主编 Agent。请把多位 Agent 的结果合并为一份严谨的中文电力现货价格周期简报。

强制要求：
- 先写“核心结论”，再写“全国走势、区域差异、价格区间与极值、数据限制、下一周期关注”。
- 每条关键结论引用证据编号 [E##]，数字必须来自证据表或结构化草稿。
- 明确全国均价是“区域等权平均”；不得将其误写为按时点或电量加权平均。
- 可靠性低于A级时，不得使用“确定、证明、导致”等强因果词。
- 对负荷、新能源、供需、天气、燃料和政策原因只能写成待验证假设。
- 控制在1200字以内，最后给出2—4条可执行的外部数据核验建议。
{f"- 优先回答用户关注：{focus}" if focus else ""}

可靠性：{json.dumps(reliability, ensure_ascii=False)}
证据表：
{evidence_text(evidence)}
审校意见：
{audit or "快速模式未启用独立审校。"}
分析 Agent 草稿：
{json.dumps(agent_outputs, ensure_ascii=False)}
"""


def validate_citations(summary: str, evidence: list[dict]) -> dict:
    valid = {item["id"] for item in evidence}
    used = set(re.findall(r"\[(E\d{2})\]", summary))
    unknown = sorted(used - valid)
    return {
        "valid_evidence_count": len(valid),
        "cited_evidence_count": len(used & valid),
        "unknown_citations": unknown,
        "citation_check_passed": bool(used) and not unknown,
    }


def deterministic_appendix(reliability: dict, evidence: list[dict], validation: dict) -> str:
    caveats = "\n".join(f"- {item}" for item in reliability["caveats"])
    ledger = "\n".join(
        f"- [{item['id']}] {item['label']}：{item.get('value')} {item.get('unit', '')}"
        + (f"（{item['scope']}）" if item.get("scope") else "")
        for item in evidence
    )
    return f"""## 数据可靠性
- 评级：{reliability['grade']}（{reliability['score']}分，{reliability['assessment']}）
- 有效区域日覆盖：{reliability['available_region_days']}/{reliability['expected_region_days']}（{reliability['coverage_rate']}%）
- 有效实时分时时点：{reliability['finite_real_time_points']}；重复键：{reliability['duplicate_keys']}
- 引证检查：引用 {validation['cited_evidence_count']}/{validation['valid_evidence_count']} 条证据；{"通过" if validation['citation_check_passed'] else "需留意"}
{caveats}

## 证据索引
{ledger}"""
