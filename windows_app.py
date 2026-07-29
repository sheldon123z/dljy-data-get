#!/usr/bin/env python3
"""Windows 便携应用入口：启动本地网站并分派冻结后的内部脚本。"""
from __future__ import annotations

import importlib
import os
import socket
import sys
import traceback
from pathlib import Path

APP_NAME = "电力现货价格工作台"
APP_VERSION = "2.0.0"
SCRIPT_MODULES = {
    "collect.py": "collect",
    "export_json.py": "export_json",
    "export_excel.py": "export_excel",
    "export_tree.py": "export_tree",
    "weekly_report.py": "weekly_report",
    "dashboard.py": "dashboard",
    "llm_summary.py": "llm_summary",
}


def _prepare_imports() -> None:
    scripts = Path(__file__).resolve().parent / "scripts"
    if scripts.exists():
        sys.path.insert(0, str(scripts))


def dispatch_script(name: str, argv: list[str]) -> int:
    module_name = SCRIPT_MODULES.get(name)
    if not module_name:
        raise SystemExit(f"不允许的内部脚本：{name}")
    module = importlib.import_module(module_name)
    main = getattr(module, "main", None)
    if not callable(main):
        raise SystemExit(f"{name} 没有可调用的 main()")
    result = main(argv)
    return int(result or 0)


def free_port(start: int = 8787, end: int = 8799) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("8787—8799 端口均被占用，请先关闭其他工作台窗口")


def show_error(message: str) -> None:
    try:
        import tkinter.messagebox

        tkinter.messagebox.showerror(APP_NAME, message)
    except Exception:
        print(message, file=sys.stderr)


def launch(no_browser: bool = False) -> int:
    import config
    import serve

    required = config.DATA_DIR / "province_codes.csv"
    if not required.exists():
        raise RuntimeError(
            "应用数据目录不完整。\n\n请完整解压“电力现货价格工作台-Windows便携版.zip”，"
            "不要只复制 EXE 文件。"
        )
    config.ensure_dirs()
    port = free_port()
    arguments = ["--host", "127.0.0.1", "--port", str(port)]
    if no_browser:
        arguments.append("--no-browser")
    return serve.main(arguments)


def main(argv: list[str] | None = None) -> int:
    # PyInstaller 的 --windowed 模式可能把标准输出设为 None；部分内部脚本仍会打印进度。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    _prepare_imports()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "--run-script":
        return dispatch_script(args[1], args[2:])
    if args and args[0] in {"--version", "-V"}:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    try:
        return launch(no_browser="--no-browser" in args)
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        show_error(f"应用启动失败：\n\n{detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
