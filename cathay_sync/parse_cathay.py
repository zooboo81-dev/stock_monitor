r"""
國泰美股對帳單：已下載的檔案 → 入帳 CSV ＋ 核對

只吃本機檔案、不連 Gmail，每次整份重算（所以隨時可以重跑，結果一樣）：
  買賣報告書\*.pdf            每日成交／除息入帳。月對帳單還沒寄來的月份靠它
  月對帳單\*.pdf              整個月的交易（台幣實際交割金額以它為準）＋月底庫存
  海外所得\*.pdf              券商算的年度已實現損益（只拿來核對）
  非交易通知\*.html           除息預告的「原庫存」（只拿來核對）
  核對用_每日持有代號.json    每天持有哪些代號（只拿來核對）

輸出到 stock_monitor\data\trades\（那個資料夾不上 GitHub）：
  cathay_us_statement.csv   交易明細（買進／賣出／除息），一列一筆
  cathay_us_holdings.csv    每月底庫存（券商算的成本、市值）
  cathay_us_check.json      這次核對的結果（戰情室頁面拿來顯示燈號）

用法：
  py -3 -X utf8 parse_cathay.py            → 重算並寫檔
  py -3 -X utf8 parse_cathay.py --dry      → 只算、只印核對結果，不寫檔
  py -3 -X utf8 parse_cathay.py --detail   → 連每一項核對的明細一起印

原則：不猜欄位。每一列都要驗算（股數×價格＝金額、金額±費用＝應收付、美元×匯率＝台幣），
每份買賣報告書要對得上自己的小計；讀不懂的列一律當成問題回報，不會默默跳過。
版面備忘（2026-11 實測 84 份）：欄位寬度每份都不同（跟著股名長短伸縮），所以不能用固定座標切欄，
改看「每筆固定三行、每行由左到右的順序」。
"""
import csv
import json
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict, namedtuple
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import sync_cathay as sc

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "data" / "trades"
OUT_LEDGER = OUT_DIR / "cathay_us_statement.csv"
OUT_HOLD = OUT_DIR / "cathay_us_holdings.csv"
OUT_CHECK = OUT_DIR / "cathay_us_check.json"
INVENTORY_FILE = HERE / "核對用_每日持有代號.json"

W = namedtuple("W", "x0 y0 x1 y1 text")
RE_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
RE_NUM = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
RE_PCT = re.compile(r"^(-?\d[\d,]*(\.\d+)?%|NA|N/A|-)$")
RE_CCY = re.compile(r"^[A-Z]{3}$")
RE_REF = re.compile(r"^\d{8}$")
RE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
KINDS = {"買進": "買進", "賣出": "賣出", "除息": "除息", "一般除息": "除息"}
ZERO = Decimal(0)
CENT = Decimal("0.011")        # 美元金額容許的四捨五入誤差
TWD_TOL = Decimal("1.01")      # 台幣金額是整數，容許差 1 元

LEDGER_COLS = ["date", "code", "name", "kind", "shares", "price", "amount", "fee", "tax", "net",
               "fx", "net_twd", "twd_final", "settle", "ref", "note", "source"]
HOLD_COLS = ["month", "as_of", "code", "name", "shares", "cost_usd", "close", "value_usd", "pnl_usd",
             "ret_pct", "ret_div_pct", "cost_twd", "fx", "value_twd", "pnl_twd", "ret_twd_pct", "custodian"]


class RowError(Exception):
    pass


def D(text):
    return Decimal(text.replace(",", ""))


def plain(d):
    """Decimal → 不帶多餘 0 的字串（5.000000 → 5、0.50 → 0.5）"""
    if d is None:
        return ""
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def money(d):
    return "" if d is None else format(d.quantize(Decimal("0.01")), "f")


def ymd(text):
    return datetime.strptime(text, "%Y/%m/%d").date()


# ───────────────────────── PDF → 行 ─────────────────────────
def page_lines(page):
    """一頁的字 → 由上到下的行，每行由左到右（同一行＝y 差 2.5 點以內；表格三行各差 5.7 點）"""
    words = sorted((W(*w[:5]) for w in page.get_text("words")), key=lambda w: (w.y0, w.x0))
    lines, cur = [], []
    for w in words:
        if cur and w.y0 - cur[0].y0 > 2.5:
            lines.append(sorted(cur, key=lambda w: w.x0))
            cur = []
        cur.append(w)
    if cur:
        lines.append(sorted(cur, key=lambda w: w.x0))
    return lines


def read_pages(path, password):
    doc = sc.open_pdf(path.read_bytes(), password)
    try:
        return [page_lines(p) for p in doc]
    finally:
        doc.close()


def texts(line):
    return [w.text for w in line]


def is_data_font(line):
    """表格資料列的字高約 5.2，表頭 5.7、內文 8 以上。拿來抓『在表格裡卻沒讀到』的列。"""
    return all(w.y1 - w.y0 < 5.5 for w in line)


def section_of(line):
    """區塊標題 → 'tx'（交易明細）／'hold'（庫存）／'unknown:標題'；不是標題回 None"""
    joined = " ".join(texts(line))
    if joined.startswith("海外股票交易明細"):
        return "tx"
    if joined.startswith("海外股票庫存及投資損益"):
        return "hold"
    m = re.match(r"^(\S*(交易明細|庫存及投資損益|庫存明細|配息明細))\s+[A-Za-z]", joined)
    return f"unknown:{m[1]}" if m else None


def near(a, b, limit):
    return 0 < b[0].y0 - a[0].y0 < limit


# ───────────────────────── 一筆三行 → 欄位 ─────────────────────────
def _tail3(t, what):
    """第三行：〔說明…〕 實際交割幣別 匯率 實際應收付"""
    if not (len(t) >= 3 and RE_CCY.match(t[-3]) and RE_NUM.match(t[-2]) and RE_NUM.match(t[-1])):
        raise RowError(f"{what}第三行（台幣那行）格式不對：{' '.join(t)[:60]}")
    return " ".join(t[:-3]), t[-3], D(t[-2]), D(t[-1])


def daily_row(l1, l2, l3):
    """買賣報告書一筆：
       L1 交易序號 代號/名稱… 幣別 價格 應收付
       L2 市場 交易種類 股數 成交金額 手續費 交易稅費 交割/基準日
       L3 〔說明…〕 實際交割幣別 匯率 實際應收付"""
    t1, t2 = texts(l1), texts(l2)
    if not (len(t1) >= 5 and RE_REF.match(t1[0]) and RE_CCY.match(t1[-3])
            and RE_NUM.match(t1[-2]) and RE_NUM.match(t1[-1])):
        raise RowError(f"第一行格式不對：{' '.join(t1)[:60]}")
    if not (len(t2) == 7 and all(RE_NUM.match(x) for x in t2[2:6]) and RE_DATE.match(t2[6])):
        raise RowError(f"第二行格式不對：{' '.join(t2)[:60]}")
    note, ccy2, fx, net2 = _tail3(texts(l3), "")
    code, _, name = " ".join(t1[1:-3]).partition("/")
    if not RE_CODE.match(code.strip()):
        raise RowError(f"商品代號看不懂：{code[:20]}")
    return dict(ref=t1[0], code=code.strip(), name=name.strip(), ccy=t1[-3], price=D(t1[-2]), net=D(t1[-1]),
                market=t2[0], kind_raw=t2[1], shares=D(t2[2]), amount=D(t2[3]), fee=D(t2[4]), tax=D(t2[5]),
                settle=ymd(t2[6]), note=note, ccy2=ccy2, fx=fx, net2=net2)


def monthly_row(l1, l2, l3):
    """月對帳單交易一筆：
       L1 交易日期 代號 市場 交易所 交易種類 幣別 價格 應收付
       L2 股數 成交金額 手續費 交易稅費
       L3 〔說明…〕 實際交割幣別 匯率 實際應收付"""
    t1, t2 = texts(l1), texts(l2)
    if not (len(t1) == 8 and RE_DATE.match(t1[0]) and RE_CODE.match(t1[1]) and RE_CCY.match(t1[5])
            and RE_NUM.match(t1[6]) and RE_NUM.match(t1[7])):
        raise RowError(f"第一行格式不對：{' '.join(t1)[:60]}")
    if not (len(t2) == 4 and all(RE_NUM.match(x) for x in t2)):
        raise RowError(f"第二行格式不對：{' '.join(t2)[:60]}")
    note, ccy2, fx, net2 = _tail3(texts(l3), "")
    return dict(date=ymd(t1[0]), code=t1[1], name="", market=t1[2], exch=t1[3], kind_raw=t1[4], ccy=t1[5],
                price=D(t1[6]), net=D(t1[7]), shares=D(t2[0]), amount=D(t2[1]), fee=D(t2[2]), tax=D(t2[3]),
                note=note, ccy2=ccy2, fx=fx, net2=net2, ref="", settle=None)


def holding_row(l1, l2, l3):
    """月對帳單庫存一筆：
       L1 幣別 投資成本 參考日期 收盤價 參考市值 投資損益 報酬率 含息報酬率
       L2 代號 名稱… 市場 交易所 持有股數
       L3 實際交割幣別 實際投資成本 參考匯率日 匯率 參考市值 投資損益 報酬率 保管機構"""
    t1, t2, t3 = texts(l1), texts(l2), texts(l3)
    if not (len(t1) == 8 and RE_CCY.match(t1[0]) and RE_NUM.match(t1[1]) and RE_DATE.match(t1[2])
            and all(RE_NUM.match(x) for x in t1[3:6]) and RE_PCT.match(t1[6]) and RE_PCT.match(t1[7])):
        raise RowError(f"庫存第一行格式不對：{' '.join(t1)[:60]}")
    if not (len(t2) >= 4 and RE_CODE.match(t2[0]) and RE_NUM.match(t2[-1])):
        raise RowError(f"庫存第二行格式不對：{' '.join(t2)[:60]}")
    if not (len(t3) == 8 and RE_CCY.match(t3[0]) and RE_NUM.match(t3[1]) and RE_DATE.match(t3[2])
            and all(RE_NUM.match(x) for x in t3[3:6]) and RE_PCT.match(t3[6])):
        raise RowError(f"庫存第三行格式不對：{' '.join(t3)[:60]}")
    return dict(code=t2[0], name=" ".join(t2[1:-3]), market=t2[-3], exch=t2[-2], shares=D(t2[-1]),
                ccy=t1[0], cost_usd=D(t1[1]), ref_date=ymd(t1[2]), close=D(t1[3]), value_usd=D(t1[4]),
                pnl_usd=D(t1[5]), ret_pct=t1[6], ret_div_pct=t1[7],
                ccy2=t3[0], cost_twd=D(t3[1]), fx_date=ymd(t3[2]), fx=D(t3[3]), value_twd=D(t3[4]),
                pnl_twd=D(t3[5]), ret_twd_pct=t3[6], custodian=t3[7])


def check_tx(r):
    """逐筆驗算，回傳問題清單（空的＝沒問題）"""
    errs = []
    kind = KINDS.get(r["kind_raw"])
    if not kind:
        return [f"沒看過的交易種類「{r['kind_raw']}」"]
    r["kind"] = kind
    tol = max(CENT, abs(r["shares"]) * Decimal("0.00006"))  # 月對帳單的價格只印到小數 4 位
    if abs(r["shares"] * r["price"] - r["amount"]) > tol:
        errs.append(f"股數×價格 {r['shares'] * r['price']:.2f} ≠ 成交金額 {r['amount']}")
    expect = -(r["amount"] + r["fee"] + r["tax"]) if kind == "買進" else r["amount"] - r["fee"] - r["tax"]
    if abs(expect - r["net"]) > CENT:
        errs.append(f"金額±費用 {expect} ≠ 應收付 {r['net']}")
    if r["fx"] <= 0 and r["net2"] != 0:
        errs.append("沒有匯率卻有台幣金額")
    if r["shares"] <= 0:
        errs.append(f"股數不是正數：{r['shares']}")
    return errs       # 台幣金額另外用 check_twd 整批驗（見下）


def twd_range(r):
    """除息的台幣可能落在哪裡：毛額、稅、費用各自換成台幣再相減，每一項可能進位、也可能捨去 → (最小, 最大)"""
    def up(x):
        return x.to_integral_value(rounding=ROUND_CEILING)

    def down(x):
        return x.to_integral_value(rounding=ROUND_FLOOR)

    g, t, f = (x * r["fx"] for x in (r["amount"], r["tax"], r["fee"]))
    return down(g) - up(t) - up(f), up(g) - down(t) - down(f)


def check_twd(rows):
    """台幣金額驗算，回傳問題清單。
    買賣：通常每筆＝美元應收付×匯率（四捨五入到元）。但同一天、同一個匯率有好幾筆時，銀行是整批換匯一次扣款；
    券商為了讓各筆加起來等於實際扣款，會把 1～幾元的尾差調在其中一筆（2026-11-23 實例：三筆同匯率，其中一筆少 1 元）。
    所以：單筆差 1 元內 → 過；超過 → 同一批的合計要對得上（差 1 元內），而且單筆的差不能超過「筆數×0.5＋1」元。
    除息：不走整批換匯，是毛額、稅各自換成台幣再相減（實測有幾筆跟「淨額×匯率」差 0.5～0.9 元，各項進位或捨去
    看不出固定規則）→ 單筆差 1 元內，或落在 twd_range（各項各自進位／捨去）的範圍內，都算對。"""
    out, groups = [], defaultdict(list)
    for r in rows:
        if r["fx"] <= 0:
            continue
        if r.get("kind") == "除息":
            lo, hi = twd_range(r)
            if not (abs(r["net"] * r["fx"] - r["net2"]) <= TWD_TOL or lo <= r["net2"] <= hi):
                out.append(f"{r['date']} {r['code']} 除息：台幣 {r['net2']:.0f}，淨額×匯率 {r['net'] * r['fx']:.2f}，"
                           f"也不在毛額、稅各自換匯的範圍 {lo:.0f}～{hi:.0f} 內")
            continue
        groups[(r["date"], r["ccy2"], r["fx"])].append(r)
    for (day, _ccy, fx), rs in groups.items():
        gaps = [r["net"] * fx - r["net2"] for r in rs]
        worst = max(abs(g) for g in gaps)
        if worst <= TWD_TOL:
            continue
        if len(rs) > 1 and abs(sum(gaps)) <= TWD_TOL and worst <= Decimal("0.5") * len(rs) + TWD_TOL:
            continue  # 尾差調在其中一筆，整批合計對得上
        bad = max(rs, key=lambda r: abs(r["net"] * fx - r["net2"]))
        out.append(f"{day} {bad['code']} {bad.get('kind', '')}：美元×匯率 {bad['net'] * fx:.0f} ≠ 台幣 {bad['net2']:.0f}"
                   + (f"（同匯率那一批 {len(rs)} 筆合計差 {sum(gaps):+.0f} 元）" if len(rs) > 1 else ""))
    return out


# ───────────────────────── 整份文件 ─────────────────────────
def parse_daily(path, password):
    res = {"file": path.name, "date": None, "rows": [], "subtotals": {}, "problems": []}
    mode = None
    for pno, lines in enumerate(read_pages(path, password), 1):
        i = 0
        while i < len(lines) and mode != "end":
            ln, t = lines[i], texts(lines[i])
            joined = "".join(t)
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日的買賣報告書", joined)
            if m and res["date"] is None:
                res["date"] = date(int(m[1]), int(m[2]), int(m[3]))
            sec = section_of(ln)
            if sec:
                mode = sec
                if sec.startswith("unknown"):
                    res["problems"].append(f"出現沒看過的區塊「{sec[8:]}」（第 {pno} 頁），裡面的資料沒有入帳")
            elif joined.startswith("手續費及相關費用說明"):
                mode = "end"
            elif mode == "tx" and t[0] == "小計":
                if len(t) == 4 and RE_CCY.match(t[2]) and RE_NUM.match(t[3]):
                    res["subtotals"][t[2]] = res["subtotals"].get(t[2], ZERO) + D(t[3])
                else:
                    res["problems"].append(f"小計那行看不懂：{' '.join(t)[:50]}")
            elif mode == "tx" and RE_REF.match(t[0]) and ln[0].x0 < 62:
                try:
                    if not (i + 2 < len(lines) and near(ln, lines[i + 1], 9) and near(ln, lines[i + 2], 16)):
                        raise RowError("這筆不是完整的三行")
                    row = daily_row(ln, lines[i + 1], lines[i + 2])
                    i += 2            # 三行都是這一筆的（驗算沒過也一樣），後兩行不要再被當成「沒讀到的列」
                    errs = check_tx(row)
                    if errs:
                        raise RowError("；".join(errs))
                    res["rows"].append(row)
                except RowError as e:
                    res["problems"].append(f"第 {pno} 頁序號 {t[0]} 讀不懂：{e}")
            elif mode == "tx" and is_data_font(ln):
                res["problems"].append(f"第 {pno} 頁表格裡有一行沒讀到：{' '.join(t)[:50]}")
            i += 1
    if res["date"] is None:
        res["problems"].append("找不到這份報告書的日期")
    for r in res["rows"]:
        r["date"] = res["date"]
    res["problems"] += check_twd(res["rows"])
    by_ccy = defaultdict(lambda: ZERO)
    for r in res["rows"]:
        by_ccy[r["ccy"]] += r["net"]
    for ccy in set(by_ccy) | set(res["subtotals"]):
        if abs(by_ccy[ccy] - res["subtotals"].get(ccy, ZERO)) > CENT or ccy not in res["subtotals"]:
            res["problems"].append(f"{ccy} 各筆加總 {by_ccy[ccy]} ≠ 報告書小計 {res['subtotals'].get(ccy, '（沒有小計）')}")
    if not res["rows"] and not res["problems"]:
        res["problems"].append("一筆交易都沒讀到")
    return res


def parse_monthly(path, password):
    res = {"file": path.name, "start": None, "end": None, "as_of": None, "tx": [], "holdings": [], "problems": []}
    mode = None
    for pno, lines in enumerate(read_pages(path, password), 1):
        i = 0
        while i < len(lines) and mode != "end":
            ln, t = lines[i], texts(lines[i])
            joined = "".join(t)
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日至(\d{4})年(\d{1,2})月(\d{1,2})日", joined)
            if m and res["start"] is None:
                res["start"] = date(int(m[1]), int(m[2]), int(m[3]))
                res["end"] = date(int(m[4]), int(m[5]), int(m[6]))
            m = re.search(r"截至(\d{4})年(\d{1,2})月(\d{1,2})日", joined)
            if m and res["as_of"] is None:
                res["as_of"] = date(int(m[1]), int(m[2]), int(m[3]))
            sec = section_of(ln)
            if sec:
                mode = sec
                if sec.startswith("unknown"):
                    res["problems"].append(f"出現沒看過的區塊「{sec[8:]}」（第 {pno} 頁），裡面的資料沒有入帳")
            elif joined.startswith("《重要公告》"):
                mode = "end"
            elif mode == "tx" and RE_DATE.match(t[0]) and ln[0].x0 < 62:
                try:
                    if not (i + 2 < len(lines) and near(ln, lines[i + 1], 9) and near(ln, lines[i + 2], 16)):
                        raise RowError("這筆不是完整的三行")
                    row = monthly_row(ln, lines[i + 1], lines[i + 2])
                    i += 2            # 三行都是這一筆的（驗算沒過也一樣）
                    errs = check_tx(row)
                    if errs:
                        raise RowError("；".join(errs))
                    res["tx"].append(row)
                except RowError as e:
                    res["problems"].append(f"第 {pno} 頁 {t[0]} {t[1] if len(t) > 1 else ''} 讀不懂：{e}")
            elif (mode == "hold" and len(t) >= 6 and RE_CCY.match(t[0]) and RE_NUM.match(t[1])
                  and RE_DATE.match(t[2])):
                try:
                    if not (i + 2 < len(lines) and near(ln, lines[i + 1], 9) and near(ln, lines[i + 2], 16)):
                        raise RowError("這筆不是完整的三行")
                    res["holdings"].append(holding_row(ln, lines[i + 1], lines[i + 2]))
                    i += 2
                except RowError as e:
                    res["problems"].append(f"第 {pno} 頁庫存有一筆讀不懂：{e}")
            elif mode in ("tx", "hold") and is_data_font(ln):
                res["problems"].append(f"第 {pno} 頁表格裡有一行沒讀到：{' '.join(t)[:50]}")
            i += 1
    if not res["as_of"] or not res["start"]:
        res["problems"].append("找不到這份對帳單的期間")
    res["problems"] += check_twd(res["tx"])
    for h in res["holdings"]:  # 券商自己的數字也驗一下，確認欄位沒讀歪
        if abs(h["shares"] * h["close"] - h["value_usd"]) > max(CENT, h["shares"] * Decimal("0.00006")):
            res["problems"].append(f"庫存 {h['code']}：股數×收盤價 ≠ 參考市值")
        if abs(h["value_usd"] - h["cost_usd"] - h["pnl_usd"]) > CENT:
            res["problems"].append(f"庫存 {h['code']}：市值−成本 ≠ 投資損益")
        if abs(h["value_usd"] * h["fx"] - h["value_twd"]) > TWD_TOL:
            res["problems"].append(f"庫存 {h['code']}：美元市值×匯率 ≠ 台幣市值")
    return res


def parse_annual(path, password):
    """海外所得通知單 → {'year': 西元年, 'sells': [(代號, 交易日, 所得台幣)], 'gain': 財產交易所得, 'dividend': 營利所得}"""
    doc = sc.open_pdf(path.read_bytes(), password)
    try:
        toks = [x.strip() for p in doc for x in p.get_text("text").splitlines() if x.strip()]
    finally:
        doc.close()
    res = {"file": path.name, "year": None, "sells": [], "dividends": [], "gain": None, "dividend": None, "problems": []}
    for k, tok in enumerate(toks):
        m = re.match(r"^(\d{2,3})年度", tok)
        if m and res["year"] is None:
            res["year"] = int(m[1]) + 1911
        if tok.startswith("76 SR") and res["gain"] is None and k + 1 < len(toks) and RE_NUM.match(toks[k + 1]):
            res["gain"] = D(toks[k + 1])
        if tok.startswith("71 SR") and res["dividend"] is None and k + 1 < len(toks) and RE_NUM.match(toks[k + 1]):
            res["dividend"] = D(toks[k + 1])
        if re.match(r"^\d{14}$", tok):  # 憑證單號：後面依序是 代號、核算日、交易日、交易方式、9 個數字
            seg = toks[k + 1:k + 14]
            if (len(seg) == 13 and RE_DATE.match(seg[1]) and RE_DATE.match(seg[2])
                    and all(RE_NUM.match(x) for x in seg[4:13])):
                item = (seg[0], ymd(seg[2]), D(seg[11]))
                (res["sells"] if "賣出" in seg[3] else res["dividends"]).append(item)
            else:
                res["problems"].append(f"海外所得明細有一列看不懂（憑證 …{tok[-6:]}）")
    if res["year"] is None or res["gain"] is None:
        res["problems"].append("海外所得通知單找不到年度或財產交易所得")
    return res


# ───────────────────────── 併成一本帳 ─────────────────────────
def row_key(r):
    """同一筆交易在買賣報告書與月對帳單裡都會出現，用這組欄位認（價格兩邊小數位數不同，不放進來）"""
    return (r["date"], r["code"], r["kind"], r["shares"], r["amount"], r["fee"], r["tax"], r["net"])


def build_ledger(dailies, monthlies):
    """月對帳單為準；買賣報告書補上序號、股名、交割日，並補月對帳單還沒涵蓋的交易。
    回傳 (帳本, 核對訊息)。帳本每列多一個 twd_final：1＝台幣金額來自月對帳單（已定案）。"""
    notes = {"errors": [], "warnings": [], "matched": 0, "daily_only": 0, "monthly_only": 0}
    ledger, pool = [], defaultdict(list)
    seen_monthly = Counter()
    for m in sorted(monthlies, key=lambda m: m["file"]):
        for r in m["tx"]:
            if m["start"] and not (m["start"] <= r["date"] <= m["end"]):
                notes["warnings"].append(f"{m['file']} 裡有一筆日期 {r['date']} 不在該月範圍內（{r['code']} {r['kind']}）")
            row = dict(r, source=Path(m["file"]).stem, twd_final=1)
            if r["fx"] == 0:
                row["twd_final"] = 0
                notes["warnings"].append(f"{m['file']} {r['date']} {r['code']} {r['kind']} 還沒換匯，台幣金額待下一期")
            ledger.append(row)
            pool[row_key(r)].append(row)
            seen_monthly[(Path(m["file"]).stem, row_key(r))] += 1
    last_end = max((m["end"] for m in monthlies if m["end"]), default=None)

    # 同一天有兩份報告書（更正版／重寄）：同一個交易序號只算一次，內容不同以後來那份為準
    by_ref, daily_rows = {}, []
    for d in sorted(dailies, key=lambda d: d["file"]):
        for r in d["rows"]:
            row = dict(r, source=Path(d["file"]).stem)
            ident = (r["date"], r["ref"]) if r["ref"].strip("0") else (r["date"], "div", row_key(r))
            if ident in by_ref:
                if row_key(by_ref[ident]) != row_key(r):
                    notes["warnings"].append(f"{d['file']} 的序號 {r['ref']} 跟前一份內容不同，以這份為準")
                    daily_rows[daily_rows.index(by_ref[ident])] = row
                    by_ref[ident] = row
                continue
            by_ref[ident] = row
            daily_rows.append(row)

    used = Counter()
    for r in daily_rows:
        k = row_key(r)
        cands = pool.get(k, [])
        if used[k] < len(cands):
            target = cands[used[k]]
            used[k] += 1
            target.update(ref=r["ref"], name=r["name"], settle=r["settle"])
            if r["price"].as_tuple().exponent < target["price"].as_tuple().exponent:
                target["price"] = r["price"]  # 報告書的價格小數位比較多
            notes["matched"] += 1
            continue
        row = dict(r, twd_final=0)
        if last_end and r["date"] <= last_end:
            notes["warnings"].append(f"{r['date']} {r['code']} {r['kind']} {plain(r['shares'])} 股：買賣報告書有、"
                                     "月對帳單沒列（月底交易常見，先用報告書的數字入帳）")
        notes["daily_only"] += 1
        ledger.append(row)
    notes["monthly_only"] = sum(len(v) for v in pool.values()) - notes["matched"]

    names = {}
    for r in ledger:
        if r.get("name"):
            names[r["code"]] = r["name"]
    for m in monthlies:
        for h in m["holdings"]:
            names.setdefault(h["code"], h["name"])
    order = {"買進": 0, "賣出": 1, "除息": 2}
    for r in ledger:
        r["name"] = r.get("name") or names.get(r["code"], r["code"])
        r["net_twd"] = r["net2"] if r["fx"] > 0 else None
    ledger.sort(key=lambda r: (r["date"], order[r["kind"]], r.get("ref") or "", r["code"]))
    return ledger, notes


# ───────────────────────── 先進先出 ─────────────────────────
def replay(ledger):
    """照日期重放一遍。回傳 (每日收盤後持股 [(日期, {代號: 股數})], 已實現明細, 最後的批次)"""
    lots = defaultdict(list)   # 代號 → [[剩餘股數, 每股美元成本, 每股台幣成本或 None, 買進日]]
    snaps, realized, errors = [], [], []
    for r in ledger:
        code, sh = r["code"], r["shares"]
        if r["kind"] == "買進":
            twd = (-r["net_twd"] / sh) if r["net_twd"] is not None else None
            lots[code].append([sh, -r["net"] / sh, twd, r["date"]])
        elif r["kind"] == "賣出":
            left, cost_usd, cost_twd, twd_known = sh, ZERO, ZERO, True
            while left > 0 and lots[code]:
                lot = lots[code][0]
                take = min(left, lot[0])
                cost_usd += take * lot[1]
                if lot[2] is None:
                    twd_known = False
                else:
                    cost_twd += take * lot[2]
                lot[0] -= take
                left -= take
                if lot[0] == 0:
                    lots[code].pop(0)
            if left > 0:
                errors.append(f"{r['date']} 賣出 {code} {plain(sh)} 股，但帳上庫存不夠（少 {plain(left)} 股）")
            twd_ok = twd_known and r["net_twd"] is not None
            realized.append(dict(date=r["date"], code=code, name=r["name"], shares=sh, proceeds_usd=r["net"],
                                 cost_usd=cost_usd, pnl_usd=r["net"] - cost_usd,
                                 proceeds_twd=r["net_twd"], cost_twd=cost_twd if twd_known else None,
                                 pnl_twd=(r["net_twd"] - cost_twd) if twd_ok else None))
        if not snaps or snaps[-1][0] != r["date"]:
            snaps.append((r["date"], None))
        snaps[-1] = (r["date"], {c: sum(l[0] for l in ls) for c, ls in lots.items() if ls})
    return snaps, realized, lots, errors


def position_asof(snaps, day):
    """day 當天（含）為止的持股 {代號: 股數}"""
    k = bisect_right([s[0] for s in snaps], day)
    return dict(snaps[k - 1][1]) if k else {}


def cost_asof(ledger, day):
    """day 當天（含）為止，每檔剩餘批次的成本 {代號: (股數, 美元成本, 台幣成本)}"""
    _, _, lots, _ = replay([r for r in ledger if r["date"] <= day])
    out = {}
    for code, ls in lots.items():
        if ls:
            twd = None if any(l[2] is None for l in ls) else sum(l[0] * l[2] for l in ls)
            out[code] = (sum(l[0] for l in ls), sum(l[0] * l[1] for l in ls), twd)
    return out


# ───────────────────────── 核對 ─────────────────────────
def verify(ledger, dailies, monthlies, annuals, notes, today=None):
    """回傳 checks：[{'name', 'ok', 'total', 'passed', 'detail': [...]}]"""
    checks = []
    today = today or date.today()

    def add(name, total, bad, hint=""):
        checks.append({"name": name, "total": total, "passed": total - len(bad), "ok": not bad,
                       "detail": bad, "hint": hint})

    snaps, realized, lots, replay_errors = replay(ledger)

    n_rows = sum(len(d["rows"]) for d in dailies) + sum(len(m["tx"]) for m in monthlies)
    parse_problems = [f"{x['file']}：{p}" for x in dailies + monthlies + annuals for p in x["problems"]]
    add("逐筆驗算（股數×價格＝金額、金額±費用＝應收付、美元×匯率＝台幣）", n_rows + len(parse_problems),
        parse_problems, "每一列都自己算一遍")
    sub_bad = [f"{d['file']}：{p}" for d in dailies for p in d["problems"] if "小計" in p]
    add("買賣報告書各筆加總＝報告書自己的小計", len(dailies), sub_bad)

    # 買賣報告書 ↔ 月對帳單
    last_end = max((m["end"] for m in monthlies if m["end"]), default=None)
    covered = [r for d in dailies for r in d["rows"] if last_end and r["date"] <= last_end]
    unmatched = [w for w in notes["warnings"] if "月對帳單沒列" in w]
    add("買賣報告書的每一筆，月對帳單裡都找得到同一筆", len(covered), unmatched,
        "日期、代號、股數、金額、費用全部一樣才算")

    # 月底庫存：股數
    bad_sh, bad_cost, n_sh, n_cost = [], [], 0, 0
    for m in sorted(monthlies, key=lambda m: m["file"]):
        if not m["as_of"]:
            continue
        mine = cost_asof(ledger, m["as_of"])
        theirs = {h["code"]: h for h in m["holdings"]}
        for code in sorted(set(mine) | set(theirs)):
            n_sh += 1
            a = mine.get(code, (ZERO, ZERO, ZERO))
            b = theirs.get(code)
            if not b or a[0] != b["shares"]:
                bad_sh.append(f"{m['as_of']} {code}：我算 {plain(a[0])} 股，對帳單 {plain(b['shares']) if b else '沒有'}")
                continue
            n_cost += 1
            twd_gap = None if a[2] is None else abs(a[2] - b["cost_twd"])
            if abs(a[1] - b["cost_usd"]) > Decimal("0.05") or (twd_gap is not None and twd_gap > max(Decimal(3), b["cost_twd"] * Decimal("0.0001"))):
                bad_cost.append(f"{m['as_of']} {code}：成本 我算 US${a[1]:.2f}／NT${(a[2] or ZERO):.0f}，"
                                f"對帳單 US${b['cost_usd']}／NT${b['cost_twd']:.0f}")
    add("每月底持股股數＝月對帳單的庫存表", n_sh, bad_sh + replay_errors, "17 個月、每一檔")
    add("每月底持有成本（先進先出）＝月對帳單的投資成本", n_cost, bad_cost, "美元差 0.05 內、台幣差 3 元內")

    # 除息通知的原庫存
    bad_no, n_no = [], 0
    for p in sorted((HERE / "非交易通知").glob("*.html")):
        m = re.match(r"非交易通知_(\d{8})_", p.name)
        if not m:
            continue
        sent = datetime.strptime(m[1], "%Y%m%d").date()
        for row in sc.notice_rows(p.read_bytes().decode("utf-8", errors="replace")):
            code = re.sub(r"[^A-Za-z0-9.]", "", row.get("商品代碼", ""))
            qty = re.sub(r"[^\d.]", "", row.get("原庫存", ""))
            if not code or not qty:
                continue
            n_no += 1
            # 原庫存＝寄信前一天收盤後的股數（2026-11 實測 17 封：除息日前持股有變，券商會再寄一封新的）
            mine = position_asof(snaps, sent - timedelta(days=1)).get(code, ZERO)
            if mine != Decimal(qty):
                bad_no.append(f"{p.name}：{code} 通知寫原庫存 {qty}，我算寄信前一天 {plain(mine)}")
    add("除息通知寫的「原庫存」＝我算的當時持股", n_no, bad_no)

    # 每日庫存電子報：只有代號。約 13:10 寄，反映到前一個美股交易日。兩種時間差不算錯：
    #  1. 電子報偶爾晚一天才出現新買的代號（實測 356 天裡有 1 天）→ 跟前兩天的持股一樣也算對
    #  2. 電子報先到、同一天的買賣報告書後到（中位 13:31、最晚 18:06）→ 這兩天內對不上先當「等報告書」，
    #     超過兩天還對不上才算有問題（＝有交易但沒收到報告書，這正是這項核對要抓的）
    bad_inv, n_inv, lagged, waiting = [], 0, [], []
    if INVENTORY_FILE.exists():
        inv = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
        for day, ticks in sorted(inv.items()):
            d = datetime.strptime(day, "%Y-%m-%d").date()
            n_inv += 1
            mine = set(position_asof(snaps, d - timedelta(days=1)))
            if mine == set(ticks):
                continue
            if set(position_asof(snaps, d - timedelta(days=2))) == set(ticks):
                lagged.append(day)
                continue
            diff = "、".join([f"多了 {c}" for c in sorted(set(ticks) - mine)] + [f"少了 {c}" for c in sorted(mine - set(ticks))])
            if (today - d).days <= 1:
                waiting.append(f"{day} 的庫存電子報{diff}：那筆交易的買賣報告書還沒寄到（通常當天下午到），到了會自動入帳")
            else:
                bad_inv.append(f"{day}：電子報比我的帳{diff}，而且超過兩天沒收到對應的買賣報告書")
    notes["warnings"] += waiting
    hints = ([f"{len(lagged)} 天是電子報晚一天更新"] if lagged else []) + ([f"{len(waiting)} 天在等買賣報告書"] if waiting else [])
    add("每天的庫存電子報列的代號＝我算的當天持股", n_inv, bad_inv, "；".join(hints))

    # 海外所得通知單：券商算的已實現損益
    bad_an, n_an = [], 0
    latest = {}
    for a in sorted(annuals, key=lambda a: a["file"]):
        if a["year"]:
            latest[a["year"]] = a
    for year, a in sorted(latest.items()):
        for code, day, gain in a["sells"]:
            n_an += 1
            mine = [x for x in realized if x["code"] == code and x["date"] == day]
            got = sum((x["pnl_twd"] for x in mine if x["pnl_twd"] is not None), ZERO)
            if not mine or abs(got - gain) > Decimal(3):
                bad_an.append(f"{year} 年 {code} {day}：券商算 {gain:.0f} 元，我算 {got:.0f} 元")
        n_an += 1
        got = sum((x["pnl_twd"] for x in realized
                   if x["pnl_twd"] is not None and (x["code"], x["date"]) in {(c, d) for c, d, _ in a["sells"]}), ZERO)
        if abs(got - a["gain"]) > Decimal(3) * max(1, len(a["sells"])):
            bad_an.append(f"{year} 年合計：券商算 {a['gain']:.0f} 元，我算 {got:.0f} 元")
    add("年度已實現損益＝券商寄的海外所得通知單", n_an, bad_an, "先進先出、台幣")

    return checks, snaps, realized, lots


# ───────────────────────── 輸出 ─────────────────────────
def csv_row(r):
    return [r["date"].isoformat(), r["code"], r["name"], r["kind"], plain(r["shares"]), plain(r["price"]),
            money(r["amount"]), money(r["fee"]), money(r["tax"]), money(r["net"]),
            plain(r["fx"]) if r["fx"] > 0 else "", plain(r["net_twd"]), r["twd_final"],
            r["settle"].isoformat() if r.get("settle") else "", r.get("ref") or "", r.get("note") or "",
            r["source"]]


def _ident(cells):
    """認「是不是同一筆」用：日期、代號、種類、股數、金額、應收付（對應 LEDGER_COLS 的位置）"""
    return (cells[0], cells[1], cells[3], cells[4], cells[6], cells[9])


def existing_keys():
    """目前 CSV 裡已經有哪些交易（Counter）；還沒有 CSV 就回空的"""
    keys = Counter()
    if OUT_LEDGER.exists():
        with open(OUT_LEDGER, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        for cells in rows[1:]:
            if len(cells) >= 10:
                keys[_ident(cells)] += 1
    return keys


def new_rows(ledger, before):
    """這次帳本裡，上次 CSV 還沒有的那幾筆"""
    seen, out = Counter(), []
    for r in ledger:
        k = _ident(csv_row(r))
        seen[k] += 1
        if seen[k] > before.get(k, 0):
            out.append(r)
    return out


def describe(r):
    """一筆交易的白話一行（推播、紀錄用）"""
    if r["kind"] == "除息":
        return f"{r['date']:%m/%d} {r['code']} 除息入帳 US${money(r['net'])}"
    return f"{r['date']:%m/%d} {r['code']} {r['kind']} {plain(r['shares'])} 股 @{r['price']:.2f}"


def write_outputs(ledger, monthlies, checks, notes, counts, realized, lots, write_ledger=True):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if write_ledger:
        tmp = OUT_LEDGER.with_suffix(".csv.tmp")
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(LEDGER_COLS)
            w.writerows(csv_row(r) for r in ledger)
        tmp.replace(OUT_LEDGER)  # 先寫暫存檔再換上去：寫到一半斷電也不會留下半份
    with open(OUT_HOLD, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HOLD_COLS)
        for m in sorted(monthlies, key=lambda m: m["file"]):
            for h in m["holdings"]:
                w.writerow([m["as_of"].strftime("%Y-%m") if m["as_of"] else "", m["as_of"].isoformat() if m["as_of"] else "",
                            h["code"], h["name"], plain(h["shares"]), money(h["cost_usd"]), plain(h["close"]),
                            money(h["value_usd"]), money(h["pnl_usd"]), h["ret_pct"], h["ret_div_pct"],
                            plain(h["cost_twd"]), plain(h["fx"]), plain(h["value_twd"]), plain(h["pnl_twd"]),
                            h["ret_twd_pct"], h["custodian"]])
    check_json = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ok": all(c["ok"] for c in checks),
        "counts": counts,
        "last_trade_date": ledger[-1]["date"].isoformat() if ledger else "",
        "last_statement_month": max((m["as_of"].strftime("%Y-%m") for m in monthlies if m["as_of"]), default=""),
        "pending_twd": sum(1 for r in ledger if not r["twd_final"]),
        "checks": [{k: c[k] for k in ("name", "ok", "total", "passed", "hint")} | {"detail": c["detail"][:20]}
                   for c in checks],
        "warnings": notes["warnings"][:30],
    }
    OUT_CHECK.write_text(json.dumps(check_json, ensure_ascii=False, indent=1), encoding="utf-8")


def _safe(fn, path, password, empty):
    """單一檔案壞掉（下載不完整、券商改版）不要讓整支當掉：記成問題、其他照算"""
    try:
        return fn(path, password)
    except Exception as e:
        return dict(empty, file=path.name, problems=[f"這份檔案讀不了（{type(e).__name__}: {str(e)[:80]}）"])


def run(password, write=True):
    """整份重算。回傳 dict：ok、checks、counts、notes、ledger…（給 sync_cathay 與戰情室用）"""
    dailies = [_safe(parse_daily, p, password, {"date": None, "rows": [], "subtotals": {}})
               for p in sorted((HERE / "買賣報告書").glob("*.pdf"))]
    monthlies = [_safe(parse_monthly, p, password, {"start": None, "end": None, "as_of": None, "tx": [], "holdings": []})
                 for p in sorted((HERE / "月對帳單").glob("*.pdf"))]
    annuals = [_safe(parse_annual, p, password, {"year": None, "sells": [], "dividends": [], "gain": None, "dividend": None})
               for p in sorted((HERE / "海外所得").glob("*.pdf"))]
    ledger, notes = build_ledger([d for d in dailies if d["date"]], monthlies)  # 沒日期的報告書已記成問題
    checks, snaps, realized, lots = verify(ledger, dailies, monthlies, annuals, notes)
    kinds = Counter(r["kind"] for r in ledger)
    counts = {"買賣報告書": len(dailies), "月對帳單": len(monthlies), "海外所得": len(annuals),
              "買進": kinds["買進"], "賣出": kinds["賣出"], "除息": kinds["除息"], "帳本筆數": len(ledger),
              "兩邊對上": notes["matched"], "只有報告書": notes["daily_only"], "只有月對帳單": notes["monthly_only"]}
    # 保險：帳本比上次少一成以上（整批讀不到、券商改版）→ 不覆蓋舊的 CSV，留著上次的好資料
    old_n = sum(existing_keys().values())
    shrunk = old_n > 0 and len(ledger) < old_n * 0.9
    checks.append({"name": "帳本筆數沒有比上次少很多", "total": 1, "passed": 0 if shrunk else 1, "ok": not shrunk,
                   "detail": [f"上次 {old_n} 筆、這次只有 {len(ledger)} 筆，先不覆蓋舊的 CSV"] if shrunk else [],
                   "hint": "少一成以上就不覆蓋，保留上次的資料"})
    if write:
        write_outputs(ledger, monthlies, checks, notes, counts, realized, lots, write_ledger=not shrunk)
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "counts": counts, "notes": notes,
            "ledger": ledger, "monthlies": monthlies, "dailies": dailies, "annuals": annuals,
            "snaps": snaps, "realized": realized, "lots": lots}


def summary_lines(res, detail=False):
    c = res["counts"]
    out = [f"帳本 {c['帳本筆數']} 筆（買進 {c['買進']}、賣出 {c['賣出']}、除息 {c['除息']}）；"
           f"來源：月對帳單 {c['月對帳單']} 份、買賣報告書 {c['買賣報告書']} 份"]
    for ch in res["checks"]:
        mark = "✅" if ch["ok"] else "❌"
        out.append(f"  {mark} {ch['name']}：{ch['passed']}/{ch['total']}")
        for d in ch["detail"][:(50 if detail else 5)]:
            out.append(f"       · {d}")
    for w in res["notes"]["warnings"][:(50 if detail else 5)]:
        out.append(f"  ⚠ {w}")
    return out


def main():
    argv = sys.argv[1:]
    s = sc.load_settings()
    if not s["PDF密碼"]:
        print("設定.txt 的「PDF密碼」還沒填，打不開 PDF。")
        return 1
    res = run(s["PDF密碼"], write="--dry" not in argv)
    for line in summary_lines(res, detail="--detail" in argv):
        print(line)
    if "--dry" not in argv:
        kept_old = any(not c["ok"] and c["name"].startswith("帳本筆數") for c in res["checks"])
        print(f"已寫入 {OUT_DIR}：" + ("（交易明細沒覆蓋，保留上次的）" if kept_old else f"{OUT_LEDGER.name}、")
              + f"{OUT_HOLD.name}、{OUT_CHECK.name}")
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
