#!/usr/bin/env python3
"""统一入口，把常用流程收敛成几条好记的命令。

    python run.py status                # 看仓库覆盖情况与令牌状态
    python run.py token                 # 交互式更新 Authorization
    python run.py sniff                 # 用本地代理自动抓取 Authorization
    python run.py import-excel          # 从历史 Excel 建立/补充数据仓
    python run.py collect [参数…]       # 采集或补采（透传给 scripts/collect.py）
    python run.py backfill              # 补齐区间内所有缺口
    python run.py daily                 # 每日增量：采最近 3 天 + 全量导出
    python run.py weekly                # ★ 每周一次：补采 + 全部导出 + 周报 + 看板
    python run.py export                # 导出 JSON + Excel + 地区/月/周分层 Excel
    python run.py export-tree           # 只重建地区/月/周分层 Excel（增量）
    python run.py report [--week …]     # 生成周报
    python run.py ai-summary [--days 7]  # 用已配置的大模型生成总结
    python run.py dashboard             # 生成自包含 HTML 看板
    python run.py artifact              # 生成可发布到 claude.ai 的 Artifact 页面
    python run.py serve                 # 启动本地控制台（含令牌输入框）
    python run.py reset-failed          # 把失败记录退回待采队列
    python run.py all                   # 全量补采 + 全部产物
"""
from __future__ import annotations

import getpass
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import config  # noqa: E402

PYTHON = sys.executable or "python3"


def run_script(name: str, *args: str) -> int:
    return subprocess.call([PYTHON, str(SCRIPTS / name), *args], cwd=str(ROOT))


def chain(*steps: tuple[str, tuple[str, ...]]) -> int:
    for name, args in steps:
        code = run_script(name, *args)
        if code != 0:
            print(f"\n{name} 退出码 {code}，后续步骤已跳过。", file=sys.stderr)
            return code
    return 0


def cmd_status() -> int:
    meta_path = config.RAW_DIR / "metadata.json"
    if not meta_path.exists():
        print("数据仓为空。先运行：python run.py import-excel 或 python run.py collect --start … --end …")
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    token = config.load_token()
    print(f"数据区间   {meta['start_date']} ~ {meta['end_date']}（{meta['requested_days']} 天）")
    print(f"价格区域   {meta['region_count']}")
    print(f"分时点数   {meta['detail_rows']:,}")
    print(
        f"区域日     有数据 {meta['available_region_days']}｜无数据 {meta.get('empty_region_days', 0)}｜"
        f"失败 {meta.get('failed_region_days', 0)}｜未采 {meta.get('missing_region_days', 0)}"
        f"｜共 {meta['expected_region_days']}"
    )
    print(f"覆盖率     {meta['coverage']:.1%}")
    print(f"更新时间   {meta['updated_at']}")
    state = config.token_state(token)
    line = state["masked"]
    if state["issued_at"]:
        line += f"｜签发于 {state['issued_at'][:16].replace('T', ' ')}"
        if state["age_hours"] is not None:
            line += f"（已用 {state['age_hours']} 小时）"
    print(f"令牌       {line}")
    if state["expired"]:
        print("           令牌已过期，运行 python run.py sniff 或 token 更新")
    return 0


def cmd_reset_failed() -> int:
    """清掉 failed 记录，让它们回到待采队列。令牌过期造成大批失败时很有用。"""
    from common import (
        QUALITY_FIELDS,
        QUALITY_FILE,
        build_outputs,
        load_provinces,
        read_csv,
        repo_range,
        write_csv,
    )

    quality_path = config.RAW_DIR / QUALITY_FILE
    rows = read_csv(quality_path)
    kept = [row for row in rows if row["status"] != "failed"]
    removed = len(rows) - len(kept)
    if not removed:
        print("没有 failed 记录。")
        return 0
    write_csv(quality_path, QUALITY_FIELDS, kept)
    start, end = repo_range(config.RAW_DIR)
    build_outputs(config.RAW_DIR, load_provinces(), start, end)
    print(f"已清除 {removed} 条 failed 记录，它们会在下次采集时重试。")
    return 0


def cmd_token() -> int:
    """交互式录入令牌，输入不回显、不进 shell 历史。"""
    print(f"把小程序重新登录后的 Authorization 粘贴进来，将写入 {config.ENV_FILE}（权限 600）。")
    token = getpass.getpass("Authorization: ").strip()
    if not token:
        print("未输入内容，已取消。")
        return 1
    path = config.save_token(token)
    print(f"已保存到 {path}：{config.mask_token(token)}")
    return 0


COMMANDS = {
    "status": lambda argv: cmd_status(),
    "token": lambda argv: cmd_token(),
    "sniff": lambda argv: run_script("sniff_token.py", *argv),
    "reset-failed": lambda argv: cmd_reset_failed(),
    "import-excel": lambda argv: run_script("import_excel.py", *argv),
    "collect": lambda argv: run_script("collect.py", *argv),
    "backfill": lambda argv: chain(("collect.py", tuple(argv)), ("export_json.py", ())),
    "daily": lambda argv: chain(
        ("collect.py", ("--last-days", "3", *argv)),
        ("export_json.py", ()),
        ("export_excel.py", ()),
        ("dashboard.py", ()),
    ),
    # 每周一次的完整刷新：补采 → 全部导出 → 周报 → 看板 → Artifact 页面
    "weekly": lambda argv: chain(
        ("collect.py", ("--last-days", "10", *argv)),
        ("export_json.py", ()),
        ("export_excel.py", ()),
        ("export_tree.py", ()),
        ("weekly_report.py", ()),
        ("dashboard.py", ()),
        ("dashboard.py", ("--artifact",)),
    ),
    "merge": lambda argv: run_script("merge.py", *argv),
    "export": lambda argv: chain(
        ("export_json.py", ()), ("export_excel.py", ()), ("export_tree.py", ())
    ),
    "export-json": lambda argv: run_script("export_json.py", *argv),
    "export-excel": lambda argv: run_script("export_excel.py", *argv),
    "export-tree": lambda argv: run_script("export_tree.py", *argv),
    "report": lambda argv: run_script("weekly_report.py", *argv),
    "ai-summary": lambda argv: run_script("llm_summary.py", *argv),
    "dashboard": lambda argv: run_script("dashboard.py", *argv),
    "artifact": lambda argv: run_script("dashboard.py", "--artifact", *argv),
    "serve": lambda argv: run_script("serve.py", *argv),
    "all": lambda argv: chain(
        ("collect.py", tuple(argv)),
        ("export_json.py", ()),
        ("export_excel.py", ()),
        ("export_tree.py", ()),
        ("weekly_report.py", ()),
        ("dashboard.py", ()),
        ("dashboard.py", ("--artifact",)),
    ),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print("可用命令：" + "、".join(COMMANDS))
        return 0
    command, *rest = argv
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"未知命令：{command}\n可用命令：" + "、".join(COMMANDS), file=sys.stderr)
        return 2
    config.ensure_dirs()
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
