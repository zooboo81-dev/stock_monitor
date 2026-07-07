"""通知模組（原 Telegram，7/06 改網頁通知中心，7/07 加 Pushover 手機推播）

保留原 send(title, body) 介面，所有呼叫方不用改。
訊息會：
  1. 寫入 data/notifications.jsonl（儀表板讀取顯示）
  2. 推播到 Pushover（手機即時彈通知）— 依 title 自動判斷 priority
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

NOTIF_FILE = Path(__file__).parent / "data" / "notifications.jsonl"
NOTIF_FILE.parent.mkdir(exist_ok=True)


def _guess_priority(title: str, body: str) -> int:
    """依 title/body 關鍵字自動判斷 Pushover priority
    -2 lowest / -1 low / 0 normal / 1 high / 2 emergency
    """
    text = (title + " " + body).lower()
    # 🚨 Emergency：硬底線 / 崩盤 / 強制動作
    if any(k in title for k in ("🚨", "強制認賠", "硬底線")):
        return 2
    if any(k in text for k in ("硬底線", "強制認賠")):
        return 2
    # 🔴 High：跌破停損 / 大盤警示 / 盤中警示
    if any(k in title for k in ("🔴", "⛔", "跌破停損", "盤中警示")):
        return 1
    if "T+10" in title or "到期" in title:
        return 1
    # 🟢 Low：出場成功 / 每日摘要 / 待辦
    if any(k in title for k in ("✅", "🎉", "紀律派勝", "待辦", "📌", "📊 推薦榜", "🌅", "☀️")):
        return -1
    # 🟡 Normal：預設
    return 0


def _push_to_pushover(title: str, body: str, priority: int) -> bool:
    try:
        from pushover_notify import send_push, is_configured
        if not is_configured():
            return False
        return send_push(title, body, priority=priority)
    except Exception:
        return False


def send(title: str, body: str, silent: bool = False) -> bool:
    """寫入通知中心 + 推播到手機。silent=True 時強制降級為 low priority。"""
    # 1) 寫入 notifications.jsonl（儀表板顯示）
    ok_jsonl = False
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "title": title,
            "body": body,
            "read": False,
        }
        with open(NOTIF_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        ok_jsonl = True
    except Exception:
        pass

    # 2) Pushover 推播
    priority = -1 if silent else _guess_priority(title, body)
    _push_to_pushover(title, body, priority)

    return ok_jsonl


def is_configured() -> bool:
    """永遠回 True（網頁通知永遠可用）"""
    return True


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "🧪 網頁通知測試"
    ok = send("📢 系統測試", msg)
    print("✅ 寫入通知中心" if ok else "❌ 失敗")
