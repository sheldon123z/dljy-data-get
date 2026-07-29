#!/usr/bin/env python3
"""Windows 便携应用入口：启动本地网站并分派冻结后的内部脚本。"""
from __future__ import annotations

import importlib
import os
import queue
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

APP_NAME = "电力现货价格工作台"
APP_VERSION = "2.0.1"
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


def launch_desktop(serve, arguments: list[str]) -> int:
    """显示常驻启动窗口，让无控制台应用始终有可见反馈。"""
    import tkinter as tk
    from tkinter import ttk

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    state: dict[str, object] = {"url": "", "closing": False, "exit_code": 0}

    def run_server() -> None:
        try:
            code = serve.main(
                [*arguments, "--no-browser"],
                ready_callback=lambda url: events.put(("ready", url)),
            )
            state["exit_code"] = int(code or 0)
        except BaseException as exc:
            state["exit_code"] = 1
            events.put(
                (
                    "error",
                    "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                )
            )
        finally:
            events.put(("stopped", state["exit_code"]))

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("500x265")
    root.resizable(False, False)
    root.configure(bg="#071421")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Panel.TFrame", background="#071421")
    style.configure(
        "Title.TLabel",
        background="#071421",
        foreground="#58d6c5",
        font=("Microsoft YaHei UI", 18, "bold"),
    )
    style.configure(
        "Body.TLabel",
        background="#071421",
        foreground="#dce8eb",
        font=("Microsoft YaHei UI", 10),
    )
    style.configure(
        "Hint.TLabel",
        background="#071421",
        foreground="#8da4ad",
        font=("Microsoft YaHei UI", 9),
    )
    style.configure(
        "Primary.TButton",
        background="#58d6c5",
        foreground="#071421",
        font=("Microsoft YaHei UI", 10, "bold"),
        padding=(20, 10),
    )
    style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 9), padding=(14, 8))

    panel = ttk.Frame(root, padding=28, style="Panel.TFrame")
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        panel,
        text="本地数据服务正在准备，启动后会自动打开默认浏览器。",
        style="Hint.TLabel",
    ).pack(anchor="w", pady=(5, 20))

    status_var = tk.StringVar(value="● 正在启动，请稍候…")
    status_label = ttk.Label(panel, textvariable=status_var, style="Body.TLabel")
    status_label.pack(anchor="w", pady=(0, 18))

    actions = ttk.Frame(panel, style="Panel.TFrame")
    actions.pack(fill="x")

    def open_site() -> None:
        url = str(state.get("url") or "")
        if not url:
            return
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            webbrowser.open_new_tab(url)

    open_button = ttk.Button(
        actions,
        text="打开工作台",
        command=open_site,
        state="disabled",
        style="Primary.TButton",
    )
    open_button.pack(side="left")

    def stop_server() -> None:
        state["closing"] = True
        url = str(state.get("url") or "")
        if url:
            parsed = urlparse(url)
            session = parse_qs(parsed.query).get("key", [""])[0]
            try:
                request = Request(
                    f"{parsed.scheme}://{parsed.netloc}/api/shutdown",
                    data=b"{}",
                    method="POST",
                    headers={"X-Session": session, "Content-Type": "application/json"},
                )
                urlopen(request, timeout=3).read()
            except OSError:
                pass
        root.destroy()

    ttk.Button(
        actions,
        text="退出应用",
        command=stop_server,
        style="Secondary.TButton",
    ).pack(side="left", padx=(12, 0))
    ttk.Label(
        panel,
        text="请保持此窗口开启；关闭窗口会同时停止本地网站。",
        style="Hint.TLabel",
    ).pack(anchor="w", pady=(20, 0))

    def poll_events() -> None:
        try:
            while True:
                event, value = events.get_nowait()
                if event == "ready":
                    state["url"] = str(value)
                    status_var.set("● 工作台已就绪 · 仅本机可访问")
                    open_button.configure(state="normal")
                    root.after(250, open_site)
                elif event == "error":
                    status_var.set(f"启动失败：{value}")
                    show_error(f"应用启动失败：\n\n{value}")
                elif event == "stopped" and not state["closing"]:
                    status_var.set("本地服务已停止，可以关闭此窗口。")
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(120, poll_events)

    root.protocol("WM_DELETE_WINDOW", stop_server)
    threading.Thread(target=run_server, daemon=True).start()
    root.after(120, poll_events)
    root.mainloop()
    return int(state["exit_code"])


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
        return serve.main([*arguments, "--no-browser"])
    return launch_desktop(serve, arguments)


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
