"""通知模組（原 Telegram，7/06 改為網頁通知中心）

保留原 send(title, body) 介面，所有呼叫方不用改。
訊息寫入 data/notifications.jsonl，儀表板讀取顯示。

保留 is_configured() 回 True，讓現有排程不會誤判失敗。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

NOTIF_FILE = Path(__file__).parent / "data" / "notifications.jsonl"
NOTIF_FILE.parent.mkdir(exist_ok=True)


def send(title: str, body: str, silent: bool = False) -> bool:
    """寫入通知中心。silent 參數保留但不使用（原 Telegram 靜音）。"""
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "title": title,
            "body": body,
            "read": False,
        }
        with open(NOTIF_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def is_configured() -> bool:
    """永遠回 True（網頁通知永遠可用）"""
    return True


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "🧪 網頁通知測試"
    ok = send("📢 系統測試", msg)
    print("✅ 寫入通知中心" if ok else "❌ 失敗")
