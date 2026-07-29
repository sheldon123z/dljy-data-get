#!/usr/bin/env python3
"""mitmproxy addon：从经过代理的请求里捞出 Authorization 并写进 .env。

由 sniff_token.py 以 `mitmdump -s scripts/mitm_addon.py` 的方式加载，
只看目标域名的请求头，抓到一次就写盘并退出，不落盘任何请求体。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

TARGET_HOST = "elecheck.aienertech.cn"


class TokenGrabber:
    def __init__(self):
        self.captured = False

    def request(self, flow):
        if self.captured:
            return
        host = flow.request.pretty_host or ""
        if TARGET_HOST not in host:
            return
        token = flow.request.headers.get("Authorization", "").strip()
        if not token or len(token) < 20:
            return
        self.captured = True
        config.save_token(token)
        print(
            f"\n✅ 已捕获令牌并写入 {config.ENV_FILE}：{config.mask_token(token)}",
            flush=True,
        )
        print(f"   来源请求：{flow.request.method} {flow.request.path[:80]}", flush=True)
        print("   正在关闭代理…", flush=True)
        try:
            from mitmproxy import ctx

            ctx.master.shutdown()
        except Exception:
            raise SystemExit(0)


addons = [TokenGrabber()]
