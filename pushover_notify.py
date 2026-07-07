"""Pushover 手機推播 — 通知直接跳鎖屏

Priority Levels:
  -2 lowest    無聲、只加通知列表
  -1 low       無聲、不震動
   0 normal    預設（有聲有震）
   1 high      強震動、跳到最上
   2 emergency 一直響到你按確認（需 retry + expiry）

用法：
  from pushover_notify import send_push
  send_push("測試", "測試訊息", priority=0)
"""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "pushover_config.json"


def _load_config() -> dict | None:
    """優先讀 pushover_config.json（本機）；fallback 讀環境變數（GitHub Actions / Streamlit Secrets）"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 環境變數 fallback（GitHub Actions Secrets / Streamlit Secrets）
    user_key = os.environ.get("PUSHOVER_USER_KEY", "")
    api_token = os.environ.get("PUSHOVER_API_TOKEN", "")
    if user_key and api_token:
        return {"user_key": user_key, "api_token": api_token, "enabled": True}
    return None


def send_push(title: str, body: str, priority: int = 0,
              url: str | None = None, url_title: str | None = None) -> bool:
    """發送 Pushover 推播
    priority: -2/-1/0/1/2
    若 priority=2，會自動加 retry=60、expiry=1800（30 分內每分鐘響一次）
    """
    cfg = _load_config()
    if not cfg or not cfg.get("enabled"):
        return False

    params = {
        "token": cfg["api_token"],
        "user": cfg["user_key"],
        "title": title,
        "message": body,
        "priority": priority,
    }
    if priority == 2:
        params["retry"] = 60      # 每 60 秒重試
        params["expiry"] = 1800   # 30 分鐘後放棄
    if url:
        params["url"] = url
        if url_title:
            params["url_title"] = url_title

    try:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Pushover 錯誤：{e}")
        return False


def is_configured() -> bool:
    cfg = _load_config()
    return bool(cfg and cfg.get("enabled") and cfg.get("user_key") and cfg.get("api_token"))


if __name__ == "__main__":
    # 測試
    ok = send_push(
        "🧪 Pushover 測試",
        "如果你手機看到這則通知 = 設定成功！\n\n未來系統警示都會即時彈到你手機。",
        priority=1,
    )
    print(f"發送結果：{'✅ 成功' if ok else '❌ 失敗'}")
