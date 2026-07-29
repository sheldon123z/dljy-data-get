#!/usr/bin/env python3
"""本地控制台：看板 + 令牌管理 + 一键采集/导出。

只监听 127.0.0.1，并用一次性会话密钥校验所有接口，避免本机其它网页越权调用。
令牌只写入项目根目录的 .env（权限 600），任何导出文件都不含令牌。
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import dashboard  # noqa: E402
from common import now_stamp  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
PYTHON = sys.executable or "python3"


def script_command(name: str, *args: str) -> list[str]:
    """兼容源码运行和 PyInstaller 冻结后的内部脚本分派。"""
    if getattr(sys, "frozen", False):
        return [PYTHON, "--run-script", name, *args]
    return [PYTHON, str(SCRIPTS / name), *args]


# 不带用户参数的固定任务
STATIC_TASKS: dict[str, list[list[str]]] = {
    "backfill": [
        script_command("collect.py", "--workers", "3"),
        script_command("export_json.py"),
        script_command("dashboard.py"),
        script_command("dashboard.py", "--artifact"),
    ],
    "json": [script_command("export_json.py")],
    "excel": [script_command("export_excel.py")],
    "tree": [script_command("export_tree.py")],
    "report": [script_command("weekly_report.py")],
    "rebuild": [
        script_command("dashboard.py"),
        script_command("dashboard.py", "--artifact"),
    ],
}

TASK_LABELS = {
    "backfill": "补采所有缺口",
    "json": "导出 JSON",
    "excel": "导出 Excel",
    "tree": "导出分层 Excel",
    "report": "生成周报",
    "rebuild": "重建看板",
}


def bounded_int(value, default: int, low: int, high: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if not low <= number <= high:
        raise ValueError(f"{label}必须在 {low}–{high} 之间")
    return number


def task_spec(task: str, options: dict) -> tuple[list[list[str]], str]:
    """把网页参数转换为白名单命令，绝不接收任意 shell 内容。"""
    if task in STATIC_TASKS:
        return STATIC_TASKS[task], TASK_LABELS.get(task, task)

    days = bounded_int(options.get("days"), 7, 1, 366, "采集天数")
    workers = bounded_int(options.get("workers"), 3, 1, 8, "并发数")
    refresh_days = bounded_int(options.get("refresh_days"), 0, 0, days, "强制重采天数")

    if task in {"collect", "weekly"}:
        collect = script_command(
            "collect.py",
            "--last-days",
            str(days),
            "--workers",
            str(workers),
        )
        if refresh_days:
            collect.extend(["--refresh-days", str(refresh_days)])
        commands = [
            collect,
            script_command("export_json.py"),
            script_command("dashboard.py"),
            script_command("dashboard.py", "--artifact"),
        ]
        if task == "weekly":
            commands[2:2] = [
                script_command("export_excel.py"),
                script_command("export_tree.py"),
                script_command("weekly_report.py"),
            ]
        label = f"{'完整刷新' if task == 'weekly' else '采集并更新看板'}（最近 {days} 天"
        if refresh_days:
            label += f"，强制重采 {refresh_days} 天"
        return commands, label + "）"

    if task == "summarize":
        agent_mode = str(options.get("agent_mode") or "standard").strip().lower()
        if agent_mode not in {"quick", "standard", "rigorous"}:
            raise ValueError("Agent 模式必须是 quick、standard 或 rigorous")
        focus = str(options.get("focus") or "").strip()
        if len(focus) > 500:
            raise ValueError("关注问题不能超过 500 字")
        command = script_command(
            "llm_summary.py",
            "--days",
            str(days),
            "--agent-mode",
            agent_mode,
        )
        if focus:
            command.extend(["--focus", focus])
        return (
            [command],
            f"生成最近 {days} 天多 Agent 总结（{agent_mode}）",
        )
    raise ValueError(f"未知任务：{task}")


class Runner:
    """串行执行任务，输出写进环形缓冲供页面轮询。"""

    def __init__(self, limit: int = 600):
        self.lines: deque[str] = deque(maxlen=limit)
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def log(self, text: str) -> None:
        self.lines.append(text)

    def start(self, commands: list[list[str]], label: str) -> str:
        with self.lock:
            if self.running:
                raise RuntimeError("已有任务在执行")
            self.lines.clear()
            self.log(f"[{now_stamp()}] 开始：{label}")
            self.thread = threading.Thread(target=self._run, args=(commands, label), daemon=True)
            self.thread.start()
            return label

    def _run(self, commands: list[list[str]], label: str) -> None:
        for command in commands:
            self.log(f"$ {' '.join(Path(part).name if part.endswith('.py') else part for part in command)}")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(config.ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                self.log(f"启动失败：{exc}")
                return
            self.process = process
            assert process.stdout is not None
            for line in process.stdout:
                self.log(line.rstrip())
            code = process.wait()
            self.process = None
            if code != 0:
                self.log(f"[{now_stamp()}] 中止：退出码 {code}")
                return
        self.log(f"[{now_stamp()}] 完成：{label}（刷新页面即可看到最新数据）")

    def stop(self) -> None:
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            self.log("已发送终止信号")

    def snapshot(self) -> dict:
        return {"log": "\n".join(self.lines), "running": self.running}


class Handler(BaseHTTPRequestHandler):
    server_version = "DljyDashboard/1.0"
    runner: Runner
    session: str
    cache: dict = {}

    def log_message(self, fmt, *args):  # 保持终端安静
        return

    # ---------------------------------------------------------- 工具
    def _send(self, payload, status: int = 200, content_type: str = "application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Session", ""), self.session)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _status_payload(self) -> dict:
        state = config.token_state()
        label = state["masked"]
        if state["age_hours"] is not None:
            label += f"，已用 {state['age_hours']} 小时"
        return {"token_set": state["set"], "token_masked": label, "expired": state["expired"]}

    def _llm_payload(self) -> dict:
        settings = config.llm_config()
        key = settings.get("api_key", "")
        return {
            "provider": settings.get("provider") or "custom",
            "base_url": settings.get("base_url", ""),
            "model": settings.get("model", ""),
            "api_key_set": bool(key),
            "api_key_masked": config.mask_token(key),
        }

    def _latest_summary(self) -> dict:
        files = sorted(config.REPORT_DIR.glob("AI总结_*.json"), key=lambda path: path.stat().st_mtime)
        if not files:
            return {"available": False}
        try:
            payload = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False}
        return {
            "available": True,
            "title": payload.get("title", ""),
            "summary": payload.get("summary", ""),
            "metadata": payload.get("metadata", {}),
            "reliability": payload.get("reliability", {}),
            "citation_validation": payload.get("citation_validation", {}),
            "agents": payload.get("agents", []),
        }

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            key = parse_qs(parsed.query).get("key", [""])[0]
            if not secrets.compare_digest(key, self.session):
                self._send(
                    "<h1>403</h1><p>缺少会话密钥，请使用启动时终端里打印的完整地址。</p>".encode("utf-8"),
                    403,
                    "text/html",
                )
                return
            payload = dashboard.build_payload(config.RAW_DIR, live=True, session=self.session)
            html = dashboard.render(payload, "全国现货电价看板 · 本地控制台")
            self._send(html.encode("utf-8"), 200, "text/html")
            return
        if parsed.path == "/api/status":
            if not self._authorized():
                self._send({"error": "unauthorized"}, 403)
                return
            self._send(self._status_payload())
            return
        if parsed.path == "/api/logs":
            if not self._authorized():
                self._send({"error": "unauthorized"}, 403)
                return
            self._send(self.runner.snapshot())
            return
        if parsed.path == "/api/llm/config":
            if not self._authorized():
                self._send({"error": "unauthorized"}, 403)
                return
            self._send(self._llm_payload())
            return
        if parsed.path == "/api/summary/latest":
            if not self._authorized():
                self._send({"error": "unauthorized"}, 403)
                return
            self._send(self._latest_summary())
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send({"error": "unauthorized"}, 403)
            return
        if parsed.path == "/api/token":
            token = (self._body().get("token") or "").strip()
            if not token:
                self._send({"error": "令牌不能为空"}, 400)
                return
            config.save_token(token)
            self.runner.log(f"[{now_stamp()}] 令牌已更新（写入 .env，权限 600）")
            self._send(self._status_payload())
            return
        if parsed.path == "/api/llm/config":
            body = self._body()
            provider = (body.get("provider") or "custom").strip().lower()
            if provider not in {"deepseek", "glm", "custom"}:
                self._send({"error": "不支持的模型服务类型"}, 400)
                return
            base_url = (body.get("base_url") or "").strip()
            model = (body.get("model") or "").strip()
            if not base_url.startswith(("https://", "http://")):
                self._send({"error": "Base URL 必须以 http:// 或 https:// 开头"}, 400)
                return
            if not model:
                self._send({"error": "模型名称不能为空"}, 400)
                return
            updates = {"provider": provider, "base_url": base_url.rstrip("/"), "model": model}
            if "api_key" in body and (body.get("api_key") or "").strip():
                updates["api_key"] = body["api_key"].strip()
            try:
                config.save_llm_config(updates)
            except ValueError as exc:
                self._send({"error": str(exc)}, 400)
                return
            self.runner.log(f"[{now_stamp()}] 大模型配置已更新（密钥未写入任何导出文件）")
            self._send(self._llm_payload())
            return
        if parsed.path == "/api/run":
            body = self._body()
            task = body.get("task", "")
            try:
                commands, label = task_spec(task, body)
            except ValueError as exc:
                self._send({"error": str(exc)}, 400)
                return
            try:
                self.runner.start(commands, label)
            except RuntimeError as exc:
                self._send({"error": str(exc)}, 409)
                return
            self._send({"command": label})
            return
        if parsed.path == "/api/stop":
            self.runner.stop()
            self._send({"ok": True})
            return
        if parsed.path == "/api/shutdown":
            self.runner.stop()
            self._send({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send({"error": "not found"}, 404)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动本地看板控制台")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1", help="默认只监听本机，不建议改")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser.parse_args(argv)


def main(argv=None, ready_callback=None) -> int:
    args = parse_args(argv)
    config.ensure_dirs()
    if not (config.RAW_DIR / "metadata.json").exists():
        raise SystemExit(
            "数据仓还是空的。先运行：python run.py import-excel（导入历史 Excel）"
            " 或 python run.py collect --start … --end …"
        )

    Handler.runner = Runner()
    Handler.session = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/?key={Handler.session}"
    print("本地控制台已启动（仅本机可访问，关闭终端即停止）：", flush=True)
    print(f"  {url}", flush=True)
    print("按 Ctrl+C 退出。", flush=True)
    if ready_callback:
        ready_callback(url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
