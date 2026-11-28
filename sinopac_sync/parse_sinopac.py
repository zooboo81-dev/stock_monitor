# -*- coding: utf-8 -*-
r"""
永豐金證券「證券月對帳單」PDF → 月底庫存（代號、股數、成本、參考市值）。純解析：不連網、不寫檔、不印金額。

版面（2026-11-28 確認）：
  「證券庫存」區：表頭一列（交易別、證券、庫存餘額、平均成本價格、總投資成本、參考市價、參考市値…），
  每檔一列：交易別（現股…）＋代號＋名稱（可能折成兩行）＋一排靠右對齊的數字；最後一列「小計」。
  區塊下面有一行「※截至YYYY年MM月DD日庫存餘額如上表資料…」＝這份庫存是哪一天的。
  當月沒有庫存時，整份沒有庫存表（沒有「庫存餘額」「平均成本價格」這些表頭，也沒有「截至」那一行），只有交易明細。
做法：用每個字的座標。代號那個字決定「這一列在哪個高度」，同一高度的數字依「離哪個表頭最近」分欄。

對帳單只有「當月有成交」才會寄；沒寄的月份＝那個月沒有買賣，庫存跟上一份一樣。

最重要的原則：**看不懂就算核對沒過，不可以把「看不懂」當成「沒有庫存」**。
  只有「整份完全沒有庫存表的痕跡、而且有交易明細」才承認是空庫存；版面改了（標題或表頭認不出來）一律沒過。
  有融資、融券的列也算沒過（那種要扣借款或算負的，這裡不處理，寧可缺、不要錯）。
"""
from __future__ import annotations

import calendar
import re
from datetime import date

CODE_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")
NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")
HEADERS = {"庫存餘額": "shares", "平均成本價格": "avg_cost", "總投資成本": "cost",
           "參考市價": "ref_price", "參考市値": "ref_value", "參考市值": "ref_value"}
KINDS = ("現股", "融資", "融券", "興櫃", "零股")
CASH_KINDS = ("現股", "零股")          # 只有這兩種可以直接用「股數 × 收盤價」當資產
# 只要文字裡出現這些，就表示這份有庫存表（就算我們沒解析出任何一列）
TABLE_MARKERS = ("庫存餘額", "平均成本價格", "庫存餘額如上表")
RIGHT_SHIFT = 14     # 數字靠右對齊：數字的中心大約在表頭中心右邊這麼多
COL_TOLERANCE = 20   # 離最近的表頭超過這麼遠就不認


def _num(t: str):
    try:
        return float(t.replace(",", "").rstrip("%"))
    except ValueError:
        return None


def month_end(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[4:6])
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_page(words) -> dict:
    """words＝page.get_text('words')。回傳這一頁的庫存列、小計、截止日、有沒有找到「證券庫存」這個標題。"""
    ws = [(x0, y0, x1, y1, t) for x0, y0, x1, y1, t, *_ in words]
    start = [w for w in ws if w[4] == "證券庫存"]
    if not start:
        return {"found": False, "rows": [], "subtotal": None, "as_of": None}
    y_start = min(w[1] for w in start)
    ends = [w[1] for w in ws if w[4] == "證券交易明細" and w[1] > y_start]
    y_end = min(ends) if ends else float("inf")
    sec = [w for w in ws if y_start < w[1] < y_end]

    as_of = None
    for w in sec:
        m = re.search(r"截至(\d{2,4})年(\d{1,2})月(\d{1,2})日", w[4])
        if m:
            y = int(m.group(1))
            as_of = date(y + 1911 if y < 1911 else y, int(m.group(2)), int(m.group(3)))

    head_words = [w for w in sec if w[4] in HEADERS]
    if not head_words:
        return {"found": True, "rows": [], "subtotal": None, "as_of": as_of}
    head_y = min(w[1] for w in head_words)
    # 表頭那一列所有的字（含我們不取的欄），數字才不會被分到不該去的欄
    head_centers = [((w[0] + w[2]) / 2, HEADERS.get(w[4])) for w in sec if abs(w[1] - head_y) < 1.0]

    def col_of(w):
        c = (w[0] + w[2]) / 2 - RIGHT_SHIFT
        center, key = min(head_centers, key=lambda h: abs(c - h[0]))
        return key if abs(c - center) < COL_TOLERANCE else None

    def numbers_at(y):
        out = {}
        for w in sorted(sec, key=lambda w: w[0]):
            if abs(w[1] - y) < 2.0 and NUM_RE.match(w[4]):
                k = col_of(w)
                if k and k not in out:
                    out[k] = _num(w[4])
        return out

    rows = []
    code_words = sorted((w for w in sec if CODE_RE.match(w[4]) and w[0] < 80 and w[1] > head_y + 5), key=lambda w: w[1])
    for cw in code_words:
        y = cw[1]
        kind = next((w[4] for w in sec if abs(w[1] - y) < 2.0 and w[4] in KINDS and w[0] < cw[0]), "")
        name = "".join(w[4] for w in sorted((w for w in sec if cw[2] < w[0] < 122 and abs(w[1] - y) < 6.0
                                              and not NUM_RE.match(w[4]) and w[4] not in KINDS),
                                             key=lambda w: (w[1], w[0])))
        rows.append({"kind": kind, "code": cw[4], "name": name, **numbers_at(y)})
    sub = None
    for w in sec:
        if w[4] == "小計" and w[1] > head_y:
            sub = numbers_at(w[1])
    return {"found": True, "rows": rows, "subtotal": sub, "as_of": as_of}


def parse_statement(doc) -> dict:
    """整份對帳單 → {"ym", "as_of", "holdings", "checks"}。checks 全是 True 才能信。

    holdings 每列：kind、code、name、shares、avg_cost、cost、ref_price、ref_value。
    沒有庫存的月份：holdings 是空的，as_of 用那個月的最後一天。
    """
    holdings, subs, as_of, ym, found, found_trades, has_table = [], [], None, None, False, False, False
    for page in doc:
        text = page.get_text("text")
        m = re.search(r"成交年月：\s*(\d{6})", text)
        if m and not ym:
            ym = m.group(1)
        found_trades = found_trades or ("證券交易明細" in text)
        has_table = has_table or any(k in text for k in TABLE_MARKERS)
        r = parse_page(page.get_text("words"))
        found = found or r["found"]
        holdings += r["rows"]
        if r["subtotal"]:
            subs.append(r["subtotal"])
        as_of = as_of or r["as_of"]
    truly_empty = (not holdings) and (not has_table) and found_trades   # 整份沒有庫存表的痕跡、只有交易明細
    if truly_empty and as_of is None and ym:
        as_of = month_end(ym)

    def tot(k):
        return sum((h.get(k) or 0) for h in holdings)

    def sub(k):
        return sum((s.get(k) or 0) for s in subs)

    fields_ok = bool(holdings) and all(
        isinstance(h.get("shares"), float) and h["shares"] > 0 and h.get("cost") is not None
        and h.get("avg_cost") is not None and h.get("ref_price") is not None and h.get("ref_value") is not None
        for h in holdings)
    checks = {
        # 有庫存表就一定要解析得出列；完全沒有庫存表（只有交易明細）才算空庫存；其他情況＝版面看不懂
        "版面看得懂（解析得出庫存列；或整份沒有庫存表、只有交易明細）": (found and bool(holdings)) or truly_empty,
        "有成交年月": ym is not None,
        "有庫存截止日": as_of is not None,
        "每列都有股數、成本、市價、市值": fields_ok or truly_empty,
        "每列的交易別都是現股或零股（融資、融券不處理）": all(h.get("kind") in CASH_KINDS for h in holdings),
        "股數加總＝小計": (bool(subs) and abs(tot("shares") - sub("shares")) < 0.5) if holdings else truly_empty,
        "成本加總＝小計": (abs(tot("cost") - sub("cost")) <= 1) if holdings else truly_empty,
        "市值加總＝小計": (abs(tot("ref_value") - sub("ref_value")) <= 1) if holdings else truly_empty,
        "每列 股數×參考市價≒參考市值": (fields_ok and all(
            abs(h["shares"] * h["ref_price"] - h["ref_value"]) <= max(1.0, 0.001 * abs(h["ref_value"])) for h in holdings))
        or truly_empty,
        # 平均成本印到小數兩位，每股最多差 0.005 → 整筆容許 股數×0.006＋1
        "每列 股數×平均成本≒總投資成本": (fields_ok and all(
            abs(h["shares"] * h["avg_cost"] - h["cost"]) <= h["shares"] * 0.006 + 1 for h in holdings)) or truly_empty,
        "代號沒有重複": len({(h["kind"], h["code"]) for h in holdings}) == len(holdings),
        "截止日在成交年月那個月": (as_of is not None and ym is not None and f"{as_of:%Y%m}" == ym),
    }
    return {"ym": ym, "as_of": as_of, "holdings": holdings, "checks": checks}
