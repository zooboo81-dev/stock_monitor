"""TXF 訊號新鮮度檢查 — 週一～五 14:20 排程跑（StockMonitor-TxfFreshness）

看 txf-backtest/data/scoreboard_log.txt 今天有沒有「收盤後」跑出來的那一行。沒有 → 推播「TXF 訊號今日未更新」。

2026-11-27 改：
  · 只認今天 13:45（收盤）以後跑的那一行。早上開機時記分板也會跑一次（盤中暫定值），
    以前那一行就算「今天跑過了」，14:00 那次失敗也不會警告。
  · 記分板被 Windows 擋下時會自己重試，最久約 25 分鐘（txf-backtest/sac_retry.py），14:20 可能還在重試：
    它寫的狀態檔顯示「執行中」就先等，最多等 30 分鐘再判斷。
  · 警告裡寫出最可能的原因（沒跑／被 Windows 擋到放棄／其他錯誤）。

手動：py -3 -X utf8 txf_freshness_check.py          照常判斷（沒更新會推播）
      py -3 -X utf8 txf_freshness_check.py --dry    只印判斷結果，不推播、不等待
      py -3 -X utf8 txf_freshness_check.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

TXF_DIR = Path("C:/Users/zoobo/txf-backtest/data")
TXF_LOG = TXF_DIR / "scoreboard_log.txt"
RETRY_STATUS = TXF_DIR / "sac_retry_status.json"      # 記分板每次執行的紀錄（sac_retry.py 寫）
AFTER_CLOSE = "13:45"                                  # 這個時間以後跑的才算收盤後的正式訊號
WAIT_MAX_MIN = 30                                      # 記分板還在重試時最多等幾分鐘
RUNNING_STALE_MIN = 35                                 # 「執行中」超過這麼久沒結束＝那次死掉了，不等
LINE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+資料(\d{4}-\d{2}-\d{2})")


def after_close_runs(text: str, day: str) -> list[str]:
    """今天收盤後跑出來的那幾行的執行時間（HH:MM）。"""
    out = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m and m.group(1) == day and m.group(2) >= AFTER_CLOSE:
            out.append(m.group(2))
    return out


def latest_run_day(text: str) -> str | None:
    days = [m.group(1) for m in (LINE_RE.match(x) for x in text.splitlines()) if m]
    return max(days) if days else None


def today_runs(status: dict, day: str) -> list[dict]:
    return [r for r in status.get("runs", []) if isinstance(r, dict) and str(r.get("started_at", "")).startswith(day)]


def still_running(status: dict, now: datetime) -> bool:
    for r in today_runs(status, f"{now:%Y-%m-%d}"):
        try:
            start = datetime.fromisoformat(r["started_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if r.get("state") == "running" and timedelta(0) <= now - start < timedelta(minutes=RUNNING_STALE_MIN):
            return True
    return False


def reason(status: dict, day: str) -> str:
    """最可能的原因（白話）。"""
    runs = [r for r in today_runs(status, day) if str(r.get("started_at", ""))[11:16] >= "13:30"]
    if not runs:
        return "記分板 14:00 沒有跑（桌機沒開、排程沒觸發）"
    last = runs[-1]
    return {
        "gave_up": f"被 Windows 智慧型應用程式控制擋下，記分板自己重試了 {last.get('blocked', '?')} 次還是過不了",
        "timeout": "記分板跑太久被中止（永豐連線很慢？）",
        "error": "記分板有跑但出錯（多半是永豐 API 登入或抓資料失敗）",
        "running": "記分板跑到一半就停了（可能被關掉）",
        "ok": "記分板顯示跑完了，但紀錄沒寫進去",
    }.get(last.get("state"), "原因不明")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def read_status() -> dict:
    try:
        data = json.loads(RETRY_STATUS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("  OK   " if cond else "  FAIL ") + name)

    log = ("2026-11-26 14:00  資料2026-11-26  【出場訊號】  收39,455\n"
           "2026-11-27 09:41  資料2026-11-27  【偏空】  收39,100\n")
    check("只有早上開機那次（盤中）→ 不算今天收盤後跑過", after_close_runs(log, "2026-11-27") == [])
    check("14:00 那次有跑 → 算", after_close_runs(log + "2026-11-27 14:00  資料2026-11-27  【出場訊號】  收39,363\n",
                                              "2026-11-27") == ["14:00"])
    check("晚上手動補跑（19:56）也算", after_close_runs(log + "2026-11-27 19:56  資料2026-11-27  【出場訊號】  收39,363\n",
                                                 "2026-11-27") == ["19:56"])
    check("休市日（資料日期是前一天）只要收盤後有跑就算，不誤報",
          after_close_runs("2026-12-25 14:00  資料2026-12-24  【偏空】  收39,000\n", "2026-12-25") == ["14:00"])
    now = datetime(2026, 11, 27, 14, 20)
    st_run = {"runs": [{"started_at": "2026-11-27T14:00:00", "state": "running", "blocked": 3}]}
    check("記分板還在重試 → 要等", still_running(st_run, now))
    check("「執行中」超過 35 分鐘 → 不等", not still_running(st_run, now + timedelta(minutes=20)))
    check("昨天的「執行中」不算", not still_running({"runs": [{"started_at": "2026-11-26T14:00:00", "state": "running"}]}, now))
    gave = {"runs": [{"started_at": "2026-11-27T09:41:00", "state": "ok"},
                     {"started_at": "2026-11-27T14:00:00", "state": "gave_up", "blocked": 28}]}
    check("原因：重試到放棄 → 寫被 Windows 擋、重試幾次", "重試了 28 次" in reason(gave, "2026-11-27"))
    check("原因：只有早上那次、14:00 沒跑 → 寫桌機沒開",
          "沒有跑" in reason({"runs": [{"started_at": "2026-11-27T09:41:00", "state": "ok"}]}, "2026-11-27"))
    check("原因：出錯 → 寫永豐 API", "永豐" in reason({"runs": [{"started_at": "2026-11-27T14:00:00", "state": "error"}]},
                                                 "2026-11-27"))
    check("狀態檔沒有或壞掉 → 不會當掉", reason({}, "2026-11-27") and not still_running({"runs": ["壞"]}, now))
    print("全部通過" if ok else "有項目沒過")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    dry = "--dry" in sys.argv
    today = date.today().isoformat()
    if date.today().weekday() >= 5:                     # 週末不檢查
        return 0

    waited = 0
    while not dry and still_running(read_status(), datetime.now()) and waited < WAIT_MAX_MIN * 60:
        time.sleep(30)
        waited += 30

    text = read_text(TXF_LOG)
    runs = after_close_runs(text, today)
    if runs:
        print(f"✅ TXF 今日收盤後已跑過（{today} {runs[-1]}）")
        return 0

    why = reason(read_status(), today)
    latest = latest_run_day(text)
    title = "⚠️ TXF 訊號今日未更新"
    body = (
        f"📅 今日 {today}\n"
        f"最後一次跑 {latest or '無紀錄'}（今天收盤後那次沒有）\n\n"
        f"最可能的原因：{why}\n"
        "細節：txf-backtest\\data\\error_log.txt\n\n"
        "💡 動作：補跑 Start-ScheduledTask TXF-Scoreboard（或雙擊 start_scoreboard.bat）\n"
        "或先參考儀表板的 TAIEX MA 訊號替代"
    )
    if dry:
        print(f"[不推播] {title}\n{body}")
        return 0
    try:
        import telegram_notify
        ok = telegram_notify.send(title, body)
        print(f"⚠️ 已推送警告：{'成功' if ok else '失敗'}")
    except Exception as e:
        print(f"❌ 推送錯誤：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
