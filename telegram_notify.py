"""Telegram 推播統一介面 — 給所有腳本呼叫

設定檔: telegram_config.json
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

CONFIG_FILE = Path(__file__).parent / "telegram_config.json"


def _load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not cfg.get("enabled"):
        return None
    token = cfg.get("bot_token", "")
    chat_id = cfg.get("chat_id", "")
    if not token or token.startswith("PASTE_") or not chat_id:
        return None
    return cfg


def send(title: str, body: str, silent: bool = False) -> bool:
    """送 Telegram 推播。失敗回 False，不會 raise。
    先試 Markdown，若 parse 失敗（特殊字元如 _ . [ 等）自動退回純文字。
    """
    cfg = _load_config()
    if not cfg:
        return False
    md_text = f"*{title}*\n\n{body}"
    plain_text = f"{title}\n\n{body}"
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"

    # 1) 先試 Markdown
    try:
        r = requests.post(url, json={
            "chat_id": cfg["chat_id"],
            "text": md_text,
            "parse_mode": "Markdown",
            "disable_notification": silent,
        }, timeout=8)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # 2) 失敗 → 純文字 retry
    try:
        r = requests.post(url, json={
            "chat_id": cfg["chat_id"],
            "text": plain_text,
            "disable_notification": silent,
        }, timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def is_configured() -> bool:
    return _load_config() is not None


if __name__ == "__main__":
    # 測試用：python telegram_notify.py
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "🧪 Telegram 推播測試成功！"
    ok = send("📢 系統測試", msg)
    print("✅ 推播送出" if ok else "❌ 推播失敗（檢查 token / chat_id / 是否跟 bot 講過話）")
