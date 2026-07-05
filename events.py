"""經濟事件日曆 + 個股財報日

事件 = 對你部位有影響的重大日期（FOMC、CPI、Nvidia 財報等）
財報 = 你持倉的台股，下次月營收 / 季報日期

事件清單可手動編輯 events.json 增減。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

EVENTS_FILE = Path(__file__).parent / "events.json"

# 預設事件（可在 events.json 編輯覆寫）
# impact: high/medium/low；category: fed/cpi/earnings/policy/macro
DEFAULT_EVENTS: list[dict] = [
    # FOMC 2026 (預估，6 月起)
    {"date": "2026-06-17", "name": "FOMC 利率決議", "impact": "high", "category": "fed",
     "note": "Fed 6 月會議，市場關注降息路徑"},
    {"date": "2026-07-29", "name": "FOMC 利率決議", "impact": "high", "category": "fed"},
    {"date": "2026-09-16", "name": "FOMC 利率決議", "impact": "high", "category": "fed"},
    {"date": "2026-10-28", "name": "FOMC 利率決議", "impact": "high", "category": "fed"},
    {"date": "2026-12-16", "name": "FOMC 利率決議", "impact": "high", "category": "fed"},
    # 美 CPI（每月 12 日左右）
    {"date": "2026-06-11", "name": "美 5 月 CPI 公布", "impact": "high", "category": "cpi"},
    {"date": "2026-07-15", "name": "美 6 月 CPI 公布", "impact": "high", "category": "cpi"},
    {"date": "2026-08-13", "name": "美 7 月 CPI 公布", "impact": "high", "category": "cpi"},
    # 台灣 CPI（每月 5 日左右）
    {"date": "2026-06-05", "name": "台灣 5 月 CPI 公布", "impact": "medium", "category": "cpi"},
    {"date": "2026-07-06", "name": "台灣 6 月 CPI 公布", "impact": "medium", "category": "cpi"},
    # 台積電財報（季）
    {"date": "2026-07-16", "name": "台積電 Q2 法說會", "impact": "high", "category": "earnings",
     "note": "台股龍頭，影響整個半導體類股"},
    {"date": "2026-10-15", "name": "台積電 Q3 法說會", "impact": "high", "category": "earnings"},
    # Nvidia 財報
    {"date": "2026-05-27", "name": "Nvidia Q1 財報", "impact": "high", "category": "earnings",
     "note": "AI 題材關鍵指標，影響整個 AI 供應鏈"},
    {"date": "2026-08-26", "name": "Nvidia Q2 財報", "impact": "high", "category": "earnings"},
    {"date": "2026-11-19", "name": "Nvidia Q3 財報", "impact": "high", "category": "earnings"},
    # 月營收（每月 10 日左右，台股公司公布前月營收）
    {"date": "2026-06-10", "name": "台股 5 月營收公布日", "impact": "medium", "category": "macro",
     "note": "你的 13 檔在這天前後公布 5 月營收，注意異常"},
    {"date": "2026-07-10", "name": "台股 6 月營收公布日", "impact": "medium", "category": "macro"},
]


def load_events() -> list[dict]:
    """讀取事件清單。優先使用使用者編輯的 events.json，否則用內建"""
    if EVENTS_FILE.exists():
        try:
            return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 第一次：把預設寫進檔案讓用戶可編輯
    try:
        EVENTS_FILE.write_text(json.dumps(DEFAULT_EVENTS, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass
    return DEFAULT_EVENTS


def upcoming_events(days_ahead: int = 30) -> list[dict]:
    """回傳未來 N 天內的事件，按日期排序"""
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    events = load_events()
    out = []
    for e in events:
        try:
            d = date.fromisoformat(e["date"])
        except (ValueError, KeyError):
            continue
        if today <= d <= cutoff:
            days_left = (d - today).days
            out.append({**e, "_date_obj": d, "days_left": days_left})
    return sorted(out, key=lambda x: x["_date_obj"])


# ──────────────── 個股財報日（基於台灣標準時程推算）────────────────
# 台灣會計年度：
#   - 月營收：每月 10 日前公布前月（不分股票）
#   - Q1 季報：5/15 前
#   - Q2 季報：8/14 前
#   - Q3 季報：11/14 前
#   - 年報：隔年 3/31 前
QUARTERLY_DEADLINES = [(5, 15), (8, 14), (11, 14), (3, 31)]


def next_monthly_revenue(today: date | None = None) -> date:
    """下次月營收公布日（每月 10 日）"""
    today = today or date.today()
    if today.day <= 10:
        return date(today.year, today.month, 10)
    # 下個月
    if today.month == 12:
        return date(today.year + 1, 1, 10)
    return date(today.year, today.month + 1, 10)


def next_quarterly_deadline(today: date | None = None) -> tuple[date, str]:
    """下次季/年報截止日"""
    today = today or date.today()
    candidates = []
    for m, d in QUARTERLY_DEADLINES:
        for yr in (today.year, today.year + 1):
            try:
                dt = date(yr, m, d)
                if dt >= today:
                    quarter_name = {(5, 15): "Q1", (8, 14): "Q2",
                                    (11, 14): "Q3", (3, 31): "年報"}[(m, d)]
                    candidates.append((dt, quarter_name))
            except ValueError:
                pass
    candidates.sort()
    return candidates[0] if candidates else (date.today(), "?")


def portfolio_earnings_calendar(codes: list[str]) -> list[dict]:
    """每檔股票的下次月營收 + 下次季報日期"""
    today = date.today()
    mr_date = next_monthly_revenue(today)
    qr_date, qr_name = next_quarterly_deadline(today)
    rows = []
    for code in codes:
        rows.append({
            "code": code,
            "next_revenue_date": mr_date.isoformat(),
            "next_revenue_days": (mr_date - today).days,
            "next_quarterly_date": qr_date.isoformat(),
            "next_quarterly_name": qr_name,
            "next_quarterly_days": (qr_date - today).days,
        })
    return rows
