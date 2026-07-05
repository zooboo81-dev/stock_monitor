"""三大法人買賣超（TWSE T86 / TPEx）

TWSE: https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALL&response=json
TPEx: https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading

每天盤後（約 17:00）公布當日資料。資料用 7 天滑動窗判斷連續買/賣超。
"""
from __future__ import annotations

from datetime import date, timedelta

import requests


# TPEx 上櫃股票需要轉換代碼處理
def _twse_t86_url(d: date) -> str:
    return (
        f"https://www.twse.com.tw/rwd/zh/fund/T86"
        f"?date={d.strftime('%Y%m%d')}&selectType=ALL&response=json"
    )


def _tpex_url(d: date) -> str:
    # 上櫃三大法人日報（民國年）
    roc_y = d.year - 1911
    return (
        f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
        f"?l=zh-tw&se=AL&t=D&d={roc_y}/{d.month:02d}/{d.day:02d}"
    )


def _parse_int(s) -> int:
    """法人資料的數字可能含逗號或負號"""
    if s is None:
        return 0
    s = str(s).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "--"):
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _share_to_lots(shares: int) -> int:
    """股數轉張數（1 張 = 1000 股）"""
    return shares // 1000


def _fetch_twse_day(session: requests.Session, d: date) -> dict[str, int] | None:
    """抓 TWSE 某日所有個股法人淨買賣超（單位：張）。"""
    try:
        r = session.get(_twse_t86_url(d), timeout=8)
        data = r.json()
    except Exception:
        return None
    if data.get("stat") != "OK":
        return None

    fields = data.get("fields", [])
    rows = data.get("data", [])

    # 找「三大法人買賣超股數」欄位
    try:
        code_idx = fields.index("證券代號")
    except ValueError:
        return None
    net_idx = None
    for keyword in ("三大法人買賣超股數", "三大法人合計買賣超股數"):
        if keyword in fields:
            net_idx = fields.index(keyword)
            break
    if net_idx is None:
        # 最後一欄常是合計
        net_idx = len(fields) - 1

    out: dict[str, int] = {}
    for row in rows:
        try:
            code = str(row[code_idx]).strip()
            net_shares = _parse_int(row[net_idx])
            out[code] = _share_to_lots(net_shares)
        except (IndexError, KeyError):
            continue
    return out


def _fetch_tpex_latest(session: requests.Session) -> tuple[dict[str, int], date | None]:
    """抓 TPEx OpenAPI 最近一日所有上櫃股的三大法人合計（張）。"""
    try:
        r = session.get(
            "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading",
            timeout=10,
        )
        data = r.json()
    except Exception:
        return {}, None
    if not data:
        return {}, None

    out: dict[str, int] = {}
    d_obj: date | None = None
    for row in data:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        if not code:
            continue
        net_shares = _parse_int(row.get("TotalDifference", 0))
        out[code] = _share_to_lots(net_shares)
        if d_obj is None:
            # TPEx 用民國年 yyyMMdd，例 1150513 = 2026/05/13
            roc = str(row.get("Date", ""))
            if len(roc) == 7:
                try:
                    yy = int(roc[:3]) + 1911
                    mm = int(roc[3:5])
                    dd = int(roc[5:7])
                    d_obj = date(yy, mm, dd)
                except (ValueError, TypeError):
                    pass
    return out, d_obj


def fetch_institutional_history(
    session: requests.Session,
    days: int = 10,
) -> tuple[dict[str, list[int]], list[date]]:
    """
    抓最近 N 個交易日的三大法人淨買賣超（單位：張）。
    - TWSE 上市：完整 N 日歷史
    - TPEx 上櫃：僅最近一日（OpenAPI 限制）

    回傳: ({code: [day-N..day-1]}, [日期 list])
    """
    today = date.today()
    days_data: dict[date, dict[str, int]] = {}
    valid_dates: list[date] = []

    # 1) TWSE 多日
    cur = today
    attempts = 0
    while len(valid_dates) < days and attempts < days * 3:
        attempts += 1
        if cur.weekday() < 5:
            tw = _fetch_twse_day(session, cur)
            if tw:
                days_data[cur] = tw
                valid_dates.append(cur)
        cur -= timedelta(days=1)

    valid_dates.sort()

    # 2) TPEx 最新一日，併入對應日期（找不到就用最近交易日）
    tpex_latest, tpex_date = _fetch_tpex_latest(session)
    if tpex_latest:
        target_date = tpex_date if tpex_date in days_data else (valid_dates[-1] if valid_dates else date.today())
        if target_date not in days_data:
            days_data[target_date] = {}
            if target_date not in valid_dates:
                valid_dates.append(target_date)
                valid_dates.sort()
        days_data[target_date].update(tpex_latest)

    by_code: dict[str, list[int]] = {}
    all_codes = set()
    for d in valid_dates:
        all_codes.update(days_data[d].keys())
    for code in all_codes:
        by_code[code] = [days_data[d].get(code, 0) for d in valid_dates]

    return by_code, valid_dates


def analyze(history: list[int]) -> dict:
    """從一檔個股的日序列推導訊號。

    consecutive_days: 連續買超為正、連續賣超為負（從最近往前數）
    latest_net: 最近一日淨買賣超（張）
    sum_recent: 近 5 日累計
    """
    if not history:
        return {"consecutive_days": 0, "latest_net": 0, "sum_recent": 0}

    latest = history[-1]
    # 連續同向天數
    direction = 1 if latest > 0 else (-1 if latest < 0 else 0)
    cd = 0
    if direction != 0:
        for v in reversed(history):
            if (direction > 0 and v > 0) or (direction < 0 and v < 0):
                cd += 1
            else:
                break
        cd *= direction

    return {
        "consecutive_days": cd,
        "latest_net": latest,
        "sum_recent": sum(history[-5:]),
    }
