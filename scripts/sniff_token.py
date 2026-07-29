#!/usr/bin/env python3
"""命令行抓取 Authorization，免开 Proxyman 之类的 GUI。

原理：起一个本地 mitmproxy，把系统 HTTP(S) 代理临时指过去，
在小程序里刷新一次电价页面，脚本捞到请求头里的 Authorization 就
写进 .env 并自动收尾（关代理、退出）。

    python run.py sniff              # 自动设置并还原系统代理（会要一次密码）
    python run.py sniff --manual     # 不碰系统设置，自己去网络偏好里填代理

首次使用需要装 mitmproxy 并信任它的根证书：

    pip install mitmproxy
    mitmdump            # 先跑一次生成证书，Ctrl+C 退出
    # 然后把 ~/.mitmproxy/mitmproxy-ca-cert.pem 加进钥匙串并设为"始终信任"
    sudo security add-trusted-cert -d -r trustRoot \\
      -k /Library/Keychains/System.keychain ~/.mitmproxy/mitmproxy-ca-cert.pem
"""
from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

ADDON = Path(__file__).resolve().parent / "mitm_addon.py"
CERT = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="通过本地代理抓取 Authorization 令牌")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=int, default=180, help="等待秒数，超时自动退出")
    parser.add_argument("--manual", action="store_true", help="不自动改系统代理")
    parser.add_argument("--service", help="网络服务名，默认自动探测（如 Wi-Fi）")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- 系统代理

def active_service() -> str | None:
    """找到当前真正在用的网络服务名，用于设置系统代理。"""
    try:
        order = subprocess.run(
            ["networksetup", "-listnetworkserviceorder"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        route = subprocess.run(
            ["route", "get", "default"], capture_output=True, text=True, timeout=10
        ).stdout
        device = ""
        for line in route.splitlines():
            if "interface:" in line:
                device = line.split(":")[-1].strip()
                break
        if device:
            blocks = order.split("\n\n")
            for block in blocks:
                if f"Device: {device}" in block and ") " in block:
                    return block.split(") ", 1)[1].split("\n")[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def set_proxy(service: str, port: int, on: bool) -> bool:
    ok = True
    for key in ("-setwebproxy", "-setsecurewebproxy"):
        state_key = key.replace("-set", "-set") + "state"
        if on:
            cmd = ["networksetup", key, service, "127.0.0.1", str(port)]
        else:
            cmd = ["networksetup", state_key, service, "off"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            ok = False
            message = (result.stderr or result.stdout).strip()
            if message:
                print(f"  {' '.join(cmd[:3])} → {message}", flush=True)
    return ok


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    args = parse_args(argv)

    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        print("未找到 mitmdump。先安装：\n")
        print("    pip install mitmproxy\n")
        print("装完再跑一次本命令。")
        return 127

    if not CERT.exists():
        print(f"还没有 mitmproxy 根证书（{CERT} 不存在）。")
        print("先执行一次 `mitmdump` 让它生成证书，Ctrl+C 退出后再运行本命令。")
        print("生成后需要信任证书，否则小程序的 HTTPS 请求会失败：\n")
        print("    sudo security add-trusted-cert -d -r trustRoot \\")
        print(f"      -k /Library/Keychains/System.keychain {CERT}\n")
        return 1

    before = config.load_token()
    service = None
    if not args.manual:
        service = args.service or active_service()
        if not service:
            print("没能自动识别网络服务名，改用手动模式。")
            args.manual = True

    print(f"启动本地代理 127.0.0.1:{args.port}")
    process = subprocess.Popen(
        [mitmdump, "-q", "-p", str(args.port), "-s", str(ADDON)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    time.sleep(1.5)
    if process.poll() is not None:
        print("代理启动失败：")
        print(process.stdout.read() if process.stdout else "")
        return 1

    proxy_on = False
    try:
        if not args.manual:
            print(f"临时把「{service}」的 HTTP/HTTPS 代理指向本机（可能需要输入密码）…")
            proxy_on = set_proxy(service, args.port, True)
            if not proxy_on:
                print("设置系统代理失败，请改用 --manual 并手动配置。")
        if args.manual or not proxy_on:
            print(f"\n请手动把系统 HTTP 和 HTTPS 代理都设为 127.0.0.1:{args.port}")

        print("\n现在去微信里打开电查查小程序，进入电价页面刷新一次。")
        print(f"最多等待 {args.timeout} 秒，抓到令牌会自动收尾。按 Ctrl+C 可提前退出。\n")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if process.stdout and process.stdout.readable():
                line = process.stdout.readline()
                if line:
                    print(line.rstrip(), flush=True)
                    continue
            time.sleep(0.3)
        else:
            print("等待超时，没有抓到目标请求。")
            process.send_signal(signal.SIGTERM)
    except KeyboardInterrupt:
        print("\n已中断。")
        process.send_signal(signal.SIGTERM)
    finally:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        if proxy_on:
            print(f"还原「{service}」的系统代理设置…")
            set_proxy(service, args.port, False)

    after = config.load_token()
    if after and after != before:
        state = config.token_state(after)
        print(f"\n完成：{state['masked']}")
        if state["issued_at"]:
            print(f"签发时间 {state['issued_at']}")
        print("接下来可以直接运行：python run.py weekly")
        return 0
    if after:
        print("\n令牌没有变化（可能小程序复用了旧令牌）。若采集仍报鉴权失败，请退出小程序重新登录后再抓。")
        return 0
    print("\n没有抓到令牌。检查：代理是否生效、证书是否已信任、小程序是否真的发起了请求。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
