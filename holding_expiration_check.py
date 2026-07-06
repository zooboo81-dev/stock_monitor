"""10 天到期提醒 — 每日檢查持倉，滿 10 個交易日推播

規則（來自 recs 歷史回測）：
  - T+10 是勝率最好的檢查點
  - 若還沒觸發停損/停利，強制回顧一次

用法：
  python holding_expiration_check.py
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

PORTFOLIO = Path("portfolio.csv")
CHECK_LOG = Path("data/expiration_checks.json")  # 記錄哪幾檔已提醒過，避免重複
CHECK_LOG.parent.mkdir(exist_ok=True)


def biz_days_between(start: date, end: date) -> int:
    """計算兩個日期之間的交易日數（排除週六日，不含假日）"""
    if start >= end:
        return 0
    days = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # 週一至五
            days += 1
    return days


def load_holdings() -> list[dict]:
    if not PORTFOLIO.exists():
        return []
    rows = []
    with open(PORTFOLIO, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def load_check_log() -> dict:
    if not CHECK_LOG.exists():
        return {}
    import json
    try:
        return json.loads(CHECK_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_check_log(log: dict) -> None:
    import json
    CHECK_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    today = date.today()
    holdings = load_holdings()
    log = load_check_log()

    reminded = []
    for h in holdings:
        entry_str = (h.get("entry_date") or "").strip()
        if not entry_str:
            continue
        try:
            entry = datetime.strptime(entry_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        biz_days = biz_days_between(entry, today)

        # 檢查點：T+10（交易日）
        if biz_days < 10:
            continue

        code = h["code"]
        key = f"{code}_{entry_str}"
        # 已提醒過就跳過
        if log.get(key, {}).get("t10_reminded"):
            continue

        # 觸發 T+10 提醒
        reminded.append({
            "code": code,
            "name": h.get("name", ""),
            "entry_date": entry_str,
            "cost": h.get("cost"),
            "biz_days": biz_days,
        })
        log[key] = {"t10_reminded": True, "reminded_at": today.isoformat()}

    if reminded:
        # 推播到通知中心
        try:
            import telegram_notify
            for r in reminded:
                title = f"⏰ T+10 到期回顧：{r['code']} {r['name']}"
                body = (
                    f"進場日：{r['entry_date']}\n"
                    f"已持有 {r['biz_days']} 個交易日\n"
                    f"進場成本：{r['cost']}\n\n"
                    f"📋 建議動作：\n"
                    f"• 檢查是否已觸發停利 +11%\n"
                    f"• 若獲利 <5% 或轉負 → 考慮認賠出場\n"
                    f"• 若獲利 5-10% → 移動停損提升至保本\n"
                    f"• 若獲利 >10% → 續抱等 trail 觸發"
                )
                telegram_notify.send(title, body)
                print(f"✅ 已提醒 {r['code']} {r['name']}")
        except Exception as e:
            print(f"⚠️ 推播錯誤：{e}")

        save_check_log(log)
        print(f"\n📊 今日 T+10 提醒 {len(reminded)} 檔")
    else:
        print(f"ℹ️ 今日無需 T+10 提醒（{today}）")


if __name__ == "__main__":
    main()
