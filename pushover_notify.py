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
import urllib.error
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
            raw = resp.read().decode()
        j = json.loads(raw)
        # Pushover 接受 = status:1；但 info 若含「no active devices」代表沒送到手機
        # （舊版只看 HTTP 200 → 沒裝置時仍回 True，靜默失敗數天無人知）
        info = j.get("info", "")
        if j.get("status") == 1 and not info:
            return True
        problem = info or "；".join(j.get("errors", [])) or f"status={j.get('status')}"
        print(f"⚠️ Pushover 未送達手機：{problem}")
        return False
    except Exception as e:
        print(f"Pushover 錯誤：{e}")
        return False


def check_health() -> tuple[bool, str]:
    """檢查 Pushover 是否有 active 裝置（validate 端點，不發訊息、不耗額度）。
    回傳 (ok, 說明)；儀表板用來顯示警告橫幅。檢查本身失敗時回 (True, ...) 避免誤報。"""
    cfg = _load_config()
    if not cfg or not cfg.get("enabled"):
        # 拿不到金鑰（如雲端未設 secrets）→ 無法判斷，回 True 不示警避免誤報
        return (True, "未設定，跳過檢查")
    try:
        data = urllib.parse.urlencode(
            {"token": cfg["api_token"], "user": cfg["user_key"]}).encode()
        req = urllib.request.Request(
            "https://api.pushover.net/1/users/validate.json", data=data)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            j = json.loads(e.read().decode())
        if j.get("status") == 1 and j.get("devices"):
            return (True, f"{len(j['devices'])} 個裝置正常")
        return (False, "；".join(j.get("errors", [])) or "沒有 active 裝置")
    except Exception as e:
        return (True, f"健康檢查連線失敗（不阻斷）：{e}")


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
