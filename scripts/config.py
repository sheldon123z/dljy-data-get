#!/usr/bin/env python3
"""路径约定、.env 令牌读写。所有脚本共用，避免每次都手写目录参数。"""
from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
JSON_DIR = EXPORT_DIR / "json"
EXCEL_DIR = EXPORT_DIR / "excel"
REPORT_DIR = EXPORT_DIR / "reports"
LOG_DIR = DATA_DIR / "logs"
ARCHIVE_DIR = DATA_DIR / "archive"

PROVINCE_FILE = DATA_DIR / "province_codes.csv"
ENV_FILE = ROOT / ".env"

TOKEN_KEY = "ELECHECK_TOKEN"
LLM_KEYS = {
    "provider": "LLM_PROVIDER",
    "base_url": "LLM_BASE_URL",
    "api_key": "LLM_API_KEY",
    "model": "LLM_MODEL",
}


def ensure_dirs() -> None:
    for path in (RAW_DIR, JSON_DIR, EXCEL_DIR, REPORT_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env，忽略注释与空行。"""
    source = path or ENV_FILE
    values: dict[str, str] = {}
    if not source.exists():
        return values
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_token() -> str:
    """优先取进程环境变量，其次取 .env。"""
    token = os.environ.get(TOKEN_KEY, "").strip()
    if token:
        return token
    return read_env_file().get(TOKEN_KEY, "").strip()


def save_token(token: str, path: Path | None = None) -> Path:
    """把令牌写进 .env 并收紧文件权限到 600。"""
    target = path or ENV_FILE
    token = token.strip()
    if not token:
        raise ValueError("令牌不能为空")
    return save_env_values({TOKEN_KEY: token}, target)


def save_env_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """安全更新 .env 中的指定键，并保留其他已有配置。"""
    target = path or ENV_FILE
    values = read_env_file(target)
    for key, value in updates.items():
        clean_key = str(key).strip()
        clean_value = str(value).strip()
        if not clean_key or "\n" in clean_key or "\r" in clean_key:
            raise ValueError("配置键无效")
        if "\n" in clean_value or "\r" in clean_value:
            raise ValueError(f"{clean_key} 不能包含换行")
        if clean_value:
            values[clean_key] = clean_value
        else:
            values.pop(clean_key, None)
    lines = [
        "# 由 run.py / 本地控制台写入，请勿提交到 Git。",
        *(f"{key}={value}" for key, value in values.items()),
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def llm_config() -> dict[str, str]:
    """读取通用 OpenAI 兼容接口配置，环境变量优先于 .env。"""
    file_values = read_env_file()
    return {
        name: os.environ.get(key, file_values.get(key, "")).strip()
        for name, key in LLM_KEYS.items()
    }


def save_llm_config(values: dict[str, str]) -> Path:
    updates = {
        env_key: values.get(name, "")
        for name, env_key in LLM_KEYS.items()
        if name in values
    }
    return save_env_values(updates)


def mask_token(token: str) -> str:
    """只用于界面回显，绝不输出完整令牌。"""
    if not token:
        return "未设置"
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}…{token[-4:]}（共 {len(token)} 字符）"


def token_claims(token: str | None = None) -> dict:
    """解出 JWT payload 里的时间字段。

    只做 base64 解码，不校验签名——这里只想知道令牌什么时候签发的。
    实测该接口的令牌只带 iat，没有 exp，所以有效期由服务端决定，
    客户端只能报告"已经用了多久"，不能预测什么时候失效。
    """
    value = token if token is not None else load_token()
    if not value:
        return {}
    raw = value.split(None, 1)[-1] if " " in value else value
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    out = {}
    for key in ("iat", "exp"):
        value = data.get(key)
        if not isinstance(value, (int, float)):
            continue
        if value > 1e11:  # 有的实现用毫秒
            value /= 1000
        try:
            out[key] = datetime.fromtimestamp(value, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            pass
    return out


def token_state(token: str | None = None) -> dict:
    """给 status、控制台和定时任务共用的一份令牌状态。"""
    value = token if token is not None else load_token()
    claims = token_claims(value)
    now = datetime.now().astimezone()
    issued = claims.get("iat")
    expiry = claims.get("exp")
    return {
        "set": bool(value),
        "masked": mask_token(value),
        "issued_at": issued.isoformat(timespec="seconds") if issued else None,
        "age_hours": round((now - issued).total_seconds() / 3600, 1) if issued else None,
        "expires_at": expiry.isoformat(timespec="seconds") if expiry else None,
        "expired": bool(expiry and expiry <= now),
    }
