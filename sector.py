"""台股分類指數熱力圖（28 個類別今日漲跌一覽）

資料源：TWSE 公開 API
  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=IND&response=json
"""
from __future__ import annotations

from datetime import date, timedelta

import requests


def _parse_pct(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_sector_indices(session: requests.Session, max_days_back: int = 5) -> tuple[list[dict], date | None]:
    """抓最近交易日的所有分類指數 + 漲跌幅"""
    today = date.today()
    for back in range(max_days_back + 1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            f"?date={d.strftime('%Y%m%d')}&type=IND&response=json"
        )
        try:
            r = session.get(url, timeout=10)
            data = r.json()
            if data.get("stat") != "OK":
                continue

            # MI_INDEX 回傳多個 table；找含「漲跌百分比」的
            tables = data.get("tables") or [data]
            for tbl in tables:
                fields = tbl.get("fields", [])
                if "漲跌百分比" not in fields and "漲跌(+/-)" not in str(fields):
                    continue
                name_idx = fields.index("指數") if "指數" in fields else 0
                # 尋找百分比欄
                pct_idx = None
                for i, f in enumerate(fields):
                    if "百分比" in f or "漲跌幅" in f:
                        pct_idx = i
                        break
                if pct_idx is None:
                    continue

                rows = tbl.get("data", [])
                out = []
                for row in rows:
                    try:
                        name = str(row[name_idx]).strip()
                        pct = _parse_pct(row[pct_idx])
                        # 過濾掉非分類指數（如未含「類」「指數」等）
                        if pct is None:
                            continue
                        if "類指數" not in name and "類" not in name:
                            continue
                        out.append({"name": name, "change_pct": pct})
                    except (IndexError, KeyError):
                        continue
                if out:
                    return out, d
        except Exception:
            continue
    return [], None
