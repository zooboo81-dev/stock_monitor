"""通知模組（原 Telegram，7/06 改網頁通知中心，7/07 加 Pushover 手機推播）

保留原 send(title, body) 介面，所有呼叫方不用改。
訊息會：
  1. 寫入通知檔（儀表板讀取顯示）
       - 雲端（GitHub Actions）→ data/notifications.jsonl（git 追蹤，雲端唯一寫入者）
       - 桌機 → data/notifications_local.jsonl（gitignored）
         2026-11-05 改：桌機排程（Trade Journal、TXF 新鮮度…）以前也寫 notifications.jsonl，
         害本機檔一直變髒、GitHub Desktop Pull 被擋
  2. 推播到 Pushover（手機即時彈通知）— 依 title 自動判斷 priority

已讀狀態記在 data/notifications_read.json（gitignored），不再改寫通知檔本身。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

_DATA = Path(__file__).parent / "data"
NOTIF_FILE = _DATA / "notifications.jsonl"              # 雲端共用
LOCAL_NOTIF_FILE = _DATA / "notifications_local.jsonl"  # 桌機專用
READ_FILE = _DATA / "notifications_read.json"           # 已讀紀錄（本機）
_DATA.mkdir(exist_ok=True)


def _is_cloud() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def target_file() -> Path:
    """這台機器的通知該寫到哪個檔"""
    return NOTIF_FILE if _is_cloud() else LOCAL_NOTIF_FILE


def _key(rec: dict) -> str:
    return f"{rec.get('ts', '')}|{rec.get('title', '')}"


def _read_keys() -> set[str]:
    try:
        return set(json.loads(READ_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def load_all(until_ts: str | None = None) -> list[dict]:
    """讀雲端共用＋桌機專用兩個通知檔，套用已讀紀錄，新→舊排序。
    until_ts：只回傳 ts <= 這個時間的（未來排程通知先隱藏）"""
    read_keys = _read_keys()
    out = []
    for f in (NOTIF_FILE, LOCAL_NOTIF_FILE):
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if until_ts and rec.get("ts", "") > until_ts:
                continue
            rec["read"] = bool(rec.get("read")) or _key(rec) in read_keys
            out.append(rec)
    return sorted(out, key=lambda x: x.get("ts", ""), reverse=True)


def mark_all_read(records: list[dict]) -> None:
    """把這些通知記為已讀（只寫 notifications_read.json，不動通知檔）"""
    keys = _read_keys() | {_key(r) for r in records}
    READ_FILE.write_text(json.dumps(sorted(keys), ensure_ascii=False), encoding="utf-8")


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
    # 🔴 High：跌破停損 / 大盤警示 / 盤中警示 / 抄底反彈進場閘門
    if any(k in title for k in ("🔴", "⛔", "跌破停損", "盤中警示", "🚀", "抄底反彈")):
        return 1
    if "T+10" in title or "到期" in title:
        return 1
    # 🟢 Low：出場成功 / 待辦（不打擾）
    if any(k in title for k in ("✅", "🎉", "紀律派勝", "待辦", "📌")):
        return -1
    # 🌅 Normal：晨間摘要、推薦榜、觀察名單（有聲有震）
    # 預設也是 Normal
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
    # 1) 寫入通知檔（雲端 → notifications.jsonl；桌機 → notifications_local.jsonl）
    ok_jsonl = False
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "title": title,
            "body": body,
            "read": False,
        }
        with open(target_file(), "a", encoding="utf-8") as f:
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
