"""TXF 訊號新鮮度檢查 — 14:20 排程跑

檢查 txf-backtest/data/scoreboard_log.txt 是否有今日資料。
若今日 14:15 後仍無新資料 → 推 Pushover 警告「TXF 沒跑（桌機沒開？）」

排程建議：Windows Task Scheduler 每天 14:20（週一至五）
"""
from __future__ import annotations
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

TXF_LOG = Path("C:/Users/zoobo/txf-backtest/data/scoreboard_log.txt")

def latest_txf_date() -> str | None:
    if not TXF_LOG.exists():
        return None
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s+資料(\d{4}-\d{2}-\d{2})")
    latest = None
    try:
        for line in TXF_LOG.read_text(encoding="utf-8").splitlines():
            m = pat.match(line)
            if m:
                d = m.group(1)  # 跑的日期
                if latest is None or d > latest:
                    latest = d
    except Exception:
        pass
    return latest


def main():
    today = date.today().isoformat()
    weekday = date.today().weekday()

    # 週末不檢查
    if weekday >= 5:
        return

    latest = latest_txf_date()

    if latest == today:
        print(f"✅ TXF 今日已跑過（{today}）")
        return

    # 沒跑 → 推警告
    try:
        import telegram_notify
        title = "⚠️ TXF 訊號今日未更新"
        body = (
            f"📅 今日 {today}\n"
            f"最後執行 {latest or '無紀錄'}\n\n"
            "可能原因：\n"
            "• 桌機沒開（scoreboard 沒跑）\n"
            "• Shioaji API 錯誤\n\n"
            "💡 動作：開桌機後手動跑 scoreboard.py\n"
            "或先參考儀表板的 TAIEX MA 訊號替代"
        )
        ok = telegram_notify.send(title, body)
        print(f"⚠️ 已推送警告：{'成功' if ok else '失敗'}")
    except Exception as e:
        print(f"❌ 推送錯誤：{e}")


if __name__ == "__main__":
    main()
