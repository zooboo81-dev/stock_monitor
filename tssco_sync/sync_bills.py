r"""
台新交割憑單自動同步
從 Gmail 抓台新證券每晚寄的「交割憑單」PDF（TSSCO_Bill_YYYYMMDD.pdf），
用設定檔的密碼解開，解析每筆成交，補進 data\trades\broker_statement.csv。
月對帳單（綜合月對帳單YYYY年MM月.pdf）只下載存到「月對帳單」資料夾，不解析。

用法：
  雙擊「執行同步.bat」                 → 一般執行（寫入 CSV、推播）
  py -3 -X utf8 sync_bills.py --test   → 只列出會抓哪些信、解析結果，不寫檔不推播
  py -3 -X utf8 sync_bills.py --dump 交割憑單\TSSCO_Bill_20261106.pdf
                                       → 印出 PDF 解密後的文字（除錯用）

憑單版面重點（2026-11 確認）：
  - 表格「類別」只有 現買／現賣；零股沒有另外標，股數 < 1000 就記為零股（和手抄 CSV 規則一致）
  - 股名是全名且會折成 2～3 行（台達電/子、T50正/2），和 CSV 用的簡稱不同，
    所以用「股名對照.txt」轉換；對照表會從既有 CSV 自動學（同日、同交易別、同股數、同價就是同一檔）
  - 集保轉入不是交易，沒有交割憑單，不會從這裡進來
  - 零股多筆撮合時憑單只印均價，價金和 股數×價 會差 1～2 元，價金與應收付才是真的（check_row 放行）
  - 每張憑單都拿「小計」列驗算明細加總（價金、手續費、交易稅、應收付），對不上就報錯，防止漏列還顯示失敗 0 筆

融資券（目前只在 2019 的舊版 HTML 憑單出現過）：
  - HTML 憑單支援 資買／資賣／券賣／券買；PDF 版沒有樣本，遇到一律回報錯誤，不猜欄位
  - CSV 的 net 存「算實際損益用的淨額」，不是當天現金（現金要扣融資金額、融券保證金，對損益沒意義）：
      資買 side=買進 lot=融資  net = -(價金＋手續費)
      資賣 side=賣出 lot=融資  net = 價金－手續費－交易稅－融資利息
      券賣 side=券賣 lot=融券  net = 價金－手續費－交易稅－融券手續費
      券買 side=券買 lot=融券  net = -(價金＋手續費－券利息)
  - 憑單印的應收付不寫進 CSV，只在 check_row 用來交叉驗算上面的淨額
"""
import csv
import email
import email.utils
import imaplib
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from collections import Counter, defaultdict
from datetime import datetime
from email.header import decode_header
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "設定.txt"
LOG_FILE = HERE / "同步紀錄.txt"
DONE_FILE = HERE / "已處理清單.txt"
NAME_MAP_FILE = HERE / "股名對照.txt"
BILL_DIR = HERE / "交割憑單"
MONTHLY_DIR = HERE / "月對帳單"
SENDER = "billu.tssco.com.tw"
CSV_HEADER = ["date", "name", "side", "lot", "shares", "price", "amount", "fee", "tax", "net", "statement"]

# 表格欄位：標題字 → 欄位名（None = 不需要，但要佔位免得數字被分到隔壁欄）
HEADER_COLS = {
    "數量": "shares", "現沖數量": None, "成交價": "price", "價金": "amount",
    "手續費": "fee", "交易稅": "tax",
    "融券擔保品": None, "融券保證金": None, "標借費用": None, "券利息": None,
    "證所稅": None, "保費": None, "金額": "net",
}
SIDE_MAP = {"現買": "買進", "現賣": "賣出"}
MARGIN_MAP = {"資買": ("買進", "融資"), "資賣": ("賣出", "融資"),  # 只有 HTML 憑單用；類別 → (side, lot)
              "券賣": ("券賣", "融券"), "券買": ("券買", "融券")}
SKIP_TYPES = {"預收款", "券差留置款", "券差留置"}  # 憑單上會出現但不是成交的列
_NUM = r"-?[\d,]+(?:\.\d+)?"  # 憑單上的數字


# ───────────────────────── 小工具 ─────────────────────────
def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M} {msg}"
    try:
        print(msg)
    except Exception:
        pass
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_kv(path):
    s = {}
    if not path or not Path(path).exists():
        return s
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        s[k.strip()] = v.strip()
    return s


def load_settings():
    s = read_kv(SETTINGS_FILE)
    s.setdefault("GMAIL帳號", "")
    s.setdefault("應用程式密碼", "")
    if not s["GMAIL帳號"] or not s["應用程式密碼"]:  # 留空 → 沿用 AI 月費請款那份
        other = read_kv(s.get("沿用Gmail設定", ""))
        s["GMAIL帳號"] = s["GMAIL帳號"] or other.get("GMAIL帳號", "")
        s["應用程式密碼"] = s["應用程式密碼"] or other.get("應用程式密碼", "")
    s["應用程式密碼"] = s["應用程式密碼"].replace(" ", "")
    return s


def decode_str(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        out.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(out)


def pushover(settings, msg):
    path = settings.get("Pushover設定檔", "")
    if not path or not Path(path).exists():
        return
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        if not cfg.get("enabled", True):
            return
        body = urllib.parse.urlencode({
            "token": cfg["api_token"], "user": cfg["user_key"],
            "title": "台新交割憑單同步", "message": msg}).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", body, timeout=15)
    except Exception as e:
        log(f"  Pushover 推播失敗：{e}")


def _num(s):
    return float(s.replace(",", ""))


def _int(s):
    return int(round(_num(s)))


def subtotal_errors(rows, printed, bill_date):
    """
    憑單印的小計 vs 解析出來的明細加總，不符回一列 _error（憑單就不會被標已處理）。
    printed＝{amount, fee, tax, net}，None＝憑單上讀不到小計。明細已經有錯的憑單不再驗，免得重複報。
    應收付比的是憑單印的現金：融資券列的 net 是算損益用的淨額，要改用 margin["cash"]。
    """
    if not rows or any(r.get("_error") for r in rows):
        return []
    if printed is None:
        return [dict(_error="讀不到小計列，無法確認明細有沒有讀完", date=bill_date, name="憑單小計", side="")]
    got = {"amount": sum(r["amount"] for r in rows), "fee": sum(r["fee"] for r in rows),
           "tax": sum(r["tax"] for r in rows),
           "net": sum(r["margin"]["cash"] if r.get("margin") else r["net"] for r in rows)}
    labels = {"amount": "價金", "fee": "手續費", "tax": "交易稅", "net": "應收付"}
    diff = [f"{labels[c]} 明細 {got[c]:,}／小計 {printed[c]:,}" for c in labels if got[c] != printed[c]]
    if diff:
        return [dict(_error="明細加總和小計不符（可能漏列）：" + "、".join(diff), date=bill_date, name="憑單小計", side="")]
    return []


# ───────────────────────── PDF 解析 ─────────────────────────
def open_pdf(data, password):
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            raise ValueError("PDF 密碼錯誤或未填")
    return doc


def parse_bill(doc, bill_date):
    """
    用文字座標解析交割憑單表格 → list[dict]（欄位同 CSV；解析不了的列帶 _error）。
    每頁：找標題列的欄位中心 x，表格每列以「類別」字（現買/現賣）為錨，
    數字依「最靠近哪個欄位中心」歸欄；股名取類別右邊、數量左邊那一小塊（會折行）。
    融資券（資買/資賣/券賣/券買）沒有 PDF 樣本，走「不支援的交易類別」回報錯誤，不猜欄位。
    小計列依序印 價金、手續費、交易稅、應收付，最後拿來驗算明細加總。
    預收款這類非成交列不入帳，但小計的應收付有算它（2024-12-10 西勝：-54,076＋預收款 54,076＝小計 0），驗算時要扣回來。
    """
    rows, printed, other_net = [], None, 0
    for page in doc:
        words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word)
        hdr = [w for w in words if w[4] == "類別"]
        if not hdr:
            continue
        y_hdr = hdr[0][1]
        x_type = hdr[0][0]
        # 欄位中心：從標題列（含第二行）抓
        cols = []
        for w in words:
            if y_hdr - 2 <= w[1] <= y_hdr + 22:
                t = w[4]
                if t in HEADER_COLS:
                    cols.append(((w[0] + w[2]) / 2, HEADER_COLS[t]))
                elif t == "手續費交易稅":  # 兩個標題黏在一起
                    q = (w[2] - w[0]) / 4
                    cols += [(w[0] + q, "fee"), (w[2] - q, "tax")]
                elif t.startswith("融券擔保"):
                    cols.append(((w[0] + w[2]) / 2, None))
        need = {"shares", "price", "amount", "fee", "tax", "net"}
        have = {c for _, c in cols if c}
        if not need <= have:
            rows.append(dict(_error=f"表頭缺欄位 {need - have}", date=bill_date, name="", side=""))
            continue
        x_qty = min(x for x, c in cols if c == "shares")  # 股名區右界
        subtotal = [w for w in words if w[4].startswith("小計")]
        y_end = subtotal[0][1] if subtotal else 10 ** 6
        if subtotal:
            nums = [w[4] for w in sorted(words, key=lambda w: w[0])
                    if abs(w[1] - y_end) < 4 and re.fullmatch(_NUM, w[4])]
            printed = dict(zip(("amount", "fee", "tax", "net"), map(_int, nums))) if len(nums) == 4 else None

        anchors = sorted((w for w in words if abs(w[0] - x_type) < 6 and y_hdr + 10 < w[1] < y_end
                          and re.fullmatch(r"[現零融][買賣]|[^\d,.-]{2,4}", w[4])), key=lambda w: w[1])
        for k, a in enumerate(anchors):
            y = a[1]
            y_lo = (anchors[k - 1][1] + y) / 2 if k else y_hdr + 10
            y_hi = (anchors[k + 1][1] + y) / 2 if k + 1 < len(anchors) else y_end - 1
            name = "".join(w[4] for w in sorted(
                (w for w in words if y_lo <= w[1] < y_hi and a[2] < w[0] < x_qty - 8), key=lambda w: (round(w[1]), w[0])))
            side = SIDE_MAP.get(a[4])
            if not side:
                if a[4] in SKIP_TYPES:  # 不是成交（例如全額交割股的預收款），略過不算錯
                    other_net += sum(_int(w[4]) for w in words if abs(w[1] - y) < 3 and re.fullmatch(_NUM, w[4])
                                     and min(cols, key=lambda c: abs(c[0] - (w[0] + w[2]) / 2))[1] == "net")
                    continue
                rows.append(dict(_error=f"不支援的交易類別「{a[4]}」", date=bill_date, name=name, side=a[4]))
                continue
            vals = {}
            for w in words:
                if abs(w[1] - y) < 3 and re.fullmatch(_NUM, w[4]):
                    cx = (w[0] + w[2]) / 2
                    col = min(cols, key=lambda c: abs(c[0] - cx))[1]
                    if col and col not in vals:
                        vals[col] = w[4]
            try:
                shares = _int(vals["shares"])
                rows.append(dict(date=bill_date, bill_name=name, name=name, side=side,
                                 lot="零股" if shares < 1000 else "現股", shares=shares,
                                 price=_num(vals["price"]), amount=_int(vals["amount"]),
                                 fee=_int(vals["fee"]), tax=_int(vals["tax"]), net=_int(vals["net"])))
            except (KeyError, ValueError) as e:
                rows.append(dict(_error=f"數字欄位對不上（{name} {side}）：{vals}", date=bill_date, name=name, side=side))
    if printed:
        printed["net"] -= other_net
    return rows + subtotal_errors(rows, printed, bill_date)


class _TableRows(HTMLParser):
    """把 HTML 裡所有表格列抓成 [[cell, cell, ...], ...]"""
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell, self._in_cell = [], True
        elif tag == "br" and self._in_cell:
            self._cell.append("/")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).replace("\xa0", " ").strip())
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


HTML_COLS = {"數量": "shares", "成交價": "price", "價金": "amount", "手續費": "fee", "交易稅": "tax", "應收付": "net"}
# 融資券才用到的欄（標題比對前先拿掉 _TableRows 由 <br> 變來的 /，例如「融資//券利息」→「融資券利息」）
HTML_MARGIN_COLS = {"融資金額": "loan", "融資自備款": "deposit", "融券手續費": "margin_fee", "融資券利息": "interest"}


def margin_net(side, amount, fee, tax, margin_fee, interest):
    """融資券列存進 CSV 的淨額（算實際損益用，不是當天現金）。"""
    return {"買進": -(amount + fee),
            "賣出": amount - fee - tax - interest,
            "券賣": amount - fee - tax - margin_fee,
            "券買": -(amount + fee - interest)}[side]


def parse_bill_html(html, bill_date):
    """2019～2024-02 的舊版憑單：ZIP 裡的 HTML 表格，欄位和 PDF 一樣，另支援融資券。
    表格讀到「小計」才算結束：空列跳過、類別空白但有內容的列報錯，最後用小計驗算明細加總。"""
    tp = _TableRows()
    tp.feed(html)
    rows, idx, sub, other_net = [], None, None, 0
    for cells in tp.rows:
        if idx is None:
            if "類別" in cells and "股票" in cells:
                idx = {}
                for i, c in enumerate(cells):
                    for label, col in HTML_COLS.items():
                        if c.startswith(label):
                            idx[col] = i
                    for label, col in HTML_MARGIN_COLS.items():
                        if c.replace("/", "").replace(" ", "").startswith(label):
                            idx[col] = i
                if not {"shares", "price", "amount", "fee", "tax", "net"} <= set(idx):
                    rows.append(dict(_error=f"HTML 表頭缺欄位：{cells}", date=bill_date, name="", side=""))
                    return rows
            continue
        if not any(cells):  # 排版用的空列，後面可能還有成交
            continue
        if cells[0].startswith("小計"):
            sub = cells
            break
        kind, name = cells[0].replace(" ", ""), cells[1].replace(" ", "") if len(cells) > 1 else ""
        if kind in MARGIN_MAP:
            rows.append(_margin_row(cells, idx, kind, name, bill_date))
            continue
        side = SIDE_MAP.get(kind)
        if not side:
            if kind in SKIP_TYPES:  # 不入帳，但小計的應收付有算它
                other_net += _int(cells[idx["net"]]) if idx["net"] < len(cells) and cells[idx["net"]] else 0
                continue
            rows.append(dict(_error=f"不支援的交易類別「{kind or '空白'}」：{cells}", date=bill_date, name=name, side=kind))
            continue
        try:
            shares = _int(cells[idx["shares"]])
            rows.append(dict(date=bill_date, bill_name=name, name=name, side=side,
                             lot="零股" if shares < 1000 else "現股", shares=shares,
                             price=_num(cells[idx["price"]]), amount=_int(cells[idx["amount"]]),
                             fee=_int(cells[idx["fee"]]), tax=_int(cells[idx["tax"]]), net=_int(cells[idx["net"]])))
        except (IndexError, ValueError) as e:
            rows.append(dict(_error=f"數字欄位對不上（{name} {side}）：{cells}", date=bill_date, name=name, side=side))
    try:
        printed = {c: _int(sub[idx[c]]) for c in ("amount", "fee", "tax", "net")} if sub else None
    except (IndexError, ValueError):
        printed = None
    if printed:
        printed["net"] -= other_net
    return rows + subtotal_errors(rows, printed, bill_date)


def _margin_row(cells, idx, kind, name, bill_date):
    """HTML 憑單的一列融資券 → CSV 列；憑單原始金額放 margin，給 check_row 交叉驗算。"""
    side, lot = MARGIN_MAP[kind]
    missing = set(HTML_MARGIN_COLS.values()) - set(idx)
    if missing:
        return dict(_error=f"HTML 表頭缺融資券欄位 {missing}", date=bill_date, name=name, side=side)
    try:
        amount, fee, tax = (_int(cells[idx[c]]) for c in ("amount", "fee", "tax"))
        extra = {c: _int(cells[idx[c]]) if cells[idx[c]] else 0 for c in HTML_MARGIN_COLS.values()}  # 空白 = 0
        return dict(date=bill_date, bill_name=name, name=name, side=side, lot=lot,
                    shares=_int(cells[idx["shares"]]), price=_num(cells[idx["price"]]),
                    amount=amount, fee=fee, tax=tax,
                    net=margin_net(side, amount, fee, tax, extra["margin_fee"], extra["interest"]),
                    margin=dict(extra, cash=_int(cells[idx["net"]])))
    except (IndexError, ValueError):
        return dict(_error=f"數字欄位對不上（{name} {side}）：{cells}", date=bill_date, name=name, side=side)


def resolve_name(bill_name, mapping, csv_names):
    """
    憑單股名 → CSV 股名，只接受完全相同的對應，回傳 (名稱, 是否有解析到)。
    不用「開頭相同」來猜：台塑／台塑石化、中鋼／中鋼結構是不同公司，併錯會污染帳本。
    對不上的名稱照憑單原名入帳並提醒。之後要改名，股名對照.txt 和 CSV 舊列要一起改（見 split_names）。
    """
    if bill_name in mapping:
        return mapping[bill_name], True
    if bill_name in csv_names:
        return bill_name, True
    return bill_name, False


def split_names(csv_rows, mapping):
    """CSV 還留著對照表已改掉的舊名 → {舊名: 新名}。同一檔變兩個名字，先進先出會配不到買進，損益和持股都會錯。"""
    names = {r["name"] for r in csv_rows}
    return {k: v for k, v in mapping.items() if k in names and k != v}


def check_margin(r):
    """融資券列：淨額照公式，再用憑單印的應收付反推，確認每個欄位都讀對。"""
    m = r.get("margin")
    if not m:
        return "融資券列缺憑單明細"
    side, net = r["side"], r["net"]
    expect = margin_net(side, r["amount"], r["fee"], r["tax"], m["margin_fee"], m["interest"])
    if net != expect:
        return f"淨額 {net} ≠ 推算 {expect}"
    if side in ("買進", "券買") and r["tax"] != 0:
        return f"{side}不應有交易稅"
    if side == "券賣" and m["loan"] != net:  # 券賣的擔保品 = 賣出淨額
        return f"融券擔保品 {m['loan']} ≠ 淨額 {net}"
    cash = {"買進": net + m["loan"],                # 融資買：券商借出融資金額，自付其餘
            "賣出": net - m["loan"],                # 融資賣：先還融資金額
            "券賣": -m["deposit"],                  # 融券賣：當天只繳保證金
            "券買": net + m["loan"] + m["deposit"],  # 融券買：退回擔保品與保證金
            }[side]
    if m["cash"] != cash:
        return f"應收付 {m['cash']} ≠ 推算 {cash}"
    return None


def check_row(r):
    """每列自我檢查，回傳錯誤訊息或 None。"""
    if r.get("_error"):
        return r["_error"]
    if r["side"] == "集保轉入":
        return None if r["shares"] > 0 else "集保轉入股數為 0"
    if not r["name"]:
        return "股名是空的"
    if r["shares"] <= 0 or r["price"] <= 0:
        return "股數或成交價不是正數"
    # 零股多筆撮合只印均價，容許 0.1% 的尾差；整股維持 1 元
    tolerance = max(1, r["amount"] * 0.001) if r["shares"] < 1000 else 1
    if abs(r["amount"] - r["shares"] * r["price"]) > tolerance:
        return f"價金 {r['amount']} ≠ 股數×價 {r['shares'] * r['price']:.0f}"
    if r.get("lot") in ("融資", "融券"):
        return check_margin(r)
    expect = -(r["amount"] + r["fee"]) if r["side"] == "買進" else r["amount"] - r["fee"] - r["tax"]
    if r["net"] != expect:
        return f"應收付 {r['net']} ≠ 推算 {expect}"
    if r["side"] == "買進" and r["tax"] != 0:
        return "買進不應有交易稅"
    return None


def fmt_row(r, statement):
    return [r["date"], r["name"], r["side"], r["lot"], str(r["shares"]), str(float(r["price"])),
            str(r["amount"]), str(r["fee"]), str(r["tax"]), str(r["net"]), statement]


def row_key(r):
    return (r["date"], r["name"], r["side"], str(int(float(r["shares"]))), str(float(r["price"])))


def match_key(r):
    """不含股名的鍵，用來學股名對照"""
    return (r["date"], r["side"], str(int(float(r["shares"]))), str(float(r["price"])))


# ───────────────────────── CSV ─────────────────────────
def load_csv(path):
    """回傳 (Counter[row_key], list[dict])"""
    if not path.exists():
        return Counter(), []
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None)
        if header != CSV_HEADER:
            raise RuntimeError(f"CSV 欄位不符：{header}")
        rows = [dict(zip(CSV_HEADER, row)) for row in rd if row]
    return Counter(row_key(r) for r in rows), rows


def append_csv(path, rows):
    """只追加，不動既有列；LF、UTF-8。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    if not new_file:
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):  # 既有檔尾沒換行就先補一個
            with open(path, "ab") as g:
                g.write(b"\n")
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new_file:
            w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(r)


# ───────────────────────── 股名對照 ─────────────────────────
def load_name_map():
    return read_kv(NAME_MAP_FILE)


def save_name_map(mapping):
    lines = ["# 憑單上的股名=CSV 用的股名（程式會自動從既有 CSV 學；已入帳的股票要改名，CSV 舊列要一起改）"]
    lines += [f"{k}={v}" for k, v in sorted(mapping.items())]
    NAME_MAP_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def learn_name_map(parsed_rows, csv_rows, mapping):
    """同日、同交易別、同股數、同價 → 憑單股名對應 CSV 股名。只學沒有歧義的。"""
    csv_names = defaultdict(set)
    for r in csv_rows:
        if r["side"] in ("買進", "賣出"):
            csv_names[match_key(r)].add(r["name"])
    learned = 0
    for r in parsed_rows:
        if r.get("_error") or r["bill_name"] in mapping:
            continue
        names = csv_names.get(match_key(r), set())
        if len(names) == 1:
            mapping[r["bill_name"]] = next(iter(names))
            learned += 1
    return learned


# ───────────────────────── Gmail ─────────────────────────
def all_mail_folder(M):
    """找 Gmail『所有郵件』資料夾（名稱會因介面語言不同，用 \\All 旗標找）"""
    typ, folders = M.list()
    for f in folders or []:
        line = f.decode(errors="replace")
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            return '"' + m.group(1) + '"'
    return "INBOX"


def safe_name(fname, data):
    """附件檔名來自信件，不可信：只留檔名本身、去掉 Windows 非法字元、副檔名固定 .pdf／.zip。"""
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", re.split(r"[/\\]", fname)[-1]).strip(" .")[-100:]
    ext = ".pdf" if data[:4] == b"%PDF" else ".zip"
    return name if name.lower().endswith(ext) else f"{name or 'bill'}{ext}"


def fetch_pdfs(msg):
    """取出信裡的憑單附件：(安全檔名, 內容)。PDF 直接給；ZIP 原樣給（解壓在 parse_attachment）。"""
    out = []
    for part in msg.walk():
        fname = decode_str(part.get_filename())
        low = fname.lower()
        data = part.get_payload(decode=True) if (low.endswith((".pdf", ".zip")) or part.get_content_type() == "application/pdf") else None
        if data and (data[:4] == b"%PDF" or data[:2] == b"PK"):
            out.append((safe_name(fname, data), data))
    return out


def parse_attachment(fname, data, password, bill_date):
    """PDF → 座標解析；ZIP（舊版，2019～2024-02）→ 解壓 HTML 再解析表格。"""
    if data[:4] == b"%PDF":
        return parse_bill(open_pdf(data, password), bill_date)
    if data[:2] == b"PK":
        import io
        zf = zipfile.ZipFile(io.BytesIO(data))
        rows = []
        for info in zf.infolist():
            if info.filename.lower().endswith((".html", ".htm")):
                html = zf.read(info, pwd=password.encode()).decode("utf-8", errors="replace")
                rows += parse_bill_html(html, bill_date)
            elif info.filename.lower().endswith(".pdf"):
                rows += parse_bill(open_pdf(zf.read(info, pwd=password.encode()), password), bill_date)
        return rows
    raise ValueError(f"看不懂的附件：{fname}")


# ───────────────────────── 主流程 ─────────────────────────
def main():
    argv = sys.argv[1:]
    if "--dump" in argv:
        s = load_settings()
        path = Path(argv[argv.index("--dump") + 1])
        doc = open_pdf(path.read_bytes(), s.get("PDF密碼", ""))
        print("\f".join(p.get_text("text") for p in doc))
        return 0

    test_mode = "--test" in argv
    s = load_settings()
    user, pwd = s.get("GMAIL帳號", ""), s.get("應用程式密碼", "")
    pdf_pw = s.get("PDF密碼", "")
    since = s.get("起始日期", "2026-05-01").replace("-", "/")
    csv_path = Path(s.get("CSV路徑", "") or HERE.parent / "data" / "trades" / "broker_statement.csv")
    if not user or not pwd:
        log("❌ 設定.txt 還沒填 Gmail 帳號或應用程式密碼（也找不到可沿用的設定）")
        return 1
    if not pdf_pw:
        log("⚠ 設定.txt 的「PDF密碼」還沒填，這次只列出會抓哪些信，不解析、不寫入。")
        test_mode = True

    done = set()
    if DONE_FILE.exists():
        done = {l.strip() for l in DONE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}

    def mark_done(key):
        if test_mode:
            return
        with open(DONE_FILE, "a", encoding="utf-8") as f:
            f.write(key + "\n")
        done.add(key)

    existing, csv_rows = load_csv(csv_path)
    log(f"CSV 既有 {sum(existing.values())} 筆（{csv_path.name}）")
    name_map = load_name_map()

    added, errors, monthly_new = [], [], []
    pending = []  # (bill_date, stem, rows)
    failed_stems = set()  # 有附件讀不了的日期：就算同日別的附件成功，也不標已處理
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pwd)
        M.select(all_mail_folder(M), readonly=True)
        # X-GM-RAW 不能放中文，主旨用 Python 再分
        typ, data = M.search(None, "X-GM-RAW", f'"from:{SENDER} after:{since}"')
        ids = data[0].split() if data and data[0] else []
        log(f"[Gmail] {since} 起共 {len(ids)} 封台新證券信件")
        mails = []
        for mid in ids:
            typ, hd = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            h = email.message_from_bytes(hd[0][1])
            subject = decode_str(h.get("Subject")).replace("\r\n", "")
            try:
                sent = email.utils.parsedate_to_datetime(h.get("Date"))
            except Exception:
                sent = None
            mails.append((mid, subject, sent))

        bills = [(m, sub) for m, sub, _ in mails if "交割憑單" in sub]
        # 月對帳單：新格式「電子月對帳單-YYYY年MM月」、2019～2020 舊格式「台新綜合證券 (8月) 對帳單」
        monthlies = [(m, sub, d) for m, sub, d in mails if "對帳單" in sub and "交割憑單" not in sub]
        log(f"  交割憑單 {len(bills)} 封、月對帳單 {len(monthlies)} 封")

        # ── 月對帳單：只下載 ──
        for mid, subject, sent in monthlies:
            if "月對帳單" in subject or not sent:   # 新格式：主旨裡的 年月
                key = "monthly:" + re.sub(r"\D", "", subject)[-6:]
            else:                                   # 舊格式：用寄信年月
                key = f"monthly-old:{sent:%Y%m}"
            if key in done:
                continue
            msg = email.message_from_bytes(M.fetch(mid, "(BODY.PEEK[])")[1][0][1])
            for fname, pdf in fetch_pdfs(msg):
                if "月對帳單" not in subject and sent:  # 舊格式檔名沒年份，前面加寄信年月
                    fname = f"{sent:%Y%m}_{fname}"
                if test_mode:
                    log(f"  [測試] 會下載月對帳單：{fname}")
                else:
                    MONTHLY_DIR.mkdir(exist_ok=True)
                    (MONTHLY_DIR / fname).write_bytes(pdf)
                    log(f"  📄 已下載月對帳單：{fname}")
                monthly_new.append(fname)
            mark_done(key)

        # ── 交割憑單：下載＋解析 ──
        for mid, subject in bills:
            m = re.search(r"(\d{4})[./](\d{1,2})[./](\d{1,2})", subject)  # 新「2026.11.6」、舊「2019/3/4」
            if not m:
                log(f"  ⚠ 主旨看不出日期，略過：{subject}")
                continue
            bill_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            stem = f"TSSCO_Bill_{bill_date.replace('-', '')}"
            if stem in done:
                continue
            if not pdf_pw:
                log(f"  [未填密碼] 會處理：{subject}")
                continue
            msg = email.message_from_bytes(M.fetch(mid, "(BODY.PEEK[])")[1][0][1])
            pdfs = fetch_pdfs(msg)
            if not pdfs:
                errors.append(f"{bill_date} 信裡沒有 PDF 附件")
                failed_stems.add(stem)
                continue
            for fname, pdf in pdfs:
                if not test_mode:
                    BILL_DIR.mkdir(exist_ok=True)
                    (BILL_DIR / fname).write_bytes(pdf)  # 原始加密檔留底，方便事後核對
                try:
                    rows = parse_attachment(fname, pdf, pdf_pw, bill_date)
                except Exception as e:
                    errors.append(f"{bill_date} 附件解密／解析失敗：{e}")
                    failed_stems.add(stem)
                    continue
                if not rows:
                    errors.append(f"{bill_date} 解析不到任何成交（憑單格式可能變了）")
                    failed_stems.add(stem)
                    continue
                pending.append((bill_date, stem, rows))
        M.logout()
    except imaplib.IMAP4.error as e:
        log(f"❌ Gmail 登入失敗（請確認應用程式密碼）：{e}")
        if not test_mode:
            pushover(s, f"Gmail 登入失敗，交割憑單沒抓到：{e}")
        return 1

    # ── 股名對照：先從既有 CSV 學，再套用 ──
    all_rows = [r for _, _, rows in pending for r in rows]
    learned = learn_name_map(all_rows, csv_rows, name_map)
    if learned and not test_mode:
        save_name_map(name_map)
        log(f"  股名對照新學到 {learned} 個（股名對照.txt）")
    csv_names = {r["name"] for r in csv_rows}
    unknown = set()
    for r in all_rows:
        if r.get("_error"):
            continue
        r["name"], ok = resolve_name(r["bill_name"], name_map, csv_names)
        if not ok:
            unknown.add(r["bill_name"])
    if unknown:
        log(f"  ℹ 新股票（CSV 沒出現過），先用憑單股名入帳：{'、'.join(sorted(unknown))}")
        log("    之後要改名：股名對照.txt 和 CSV 舊列要一起改，只改對照會把同一檔拆成兩個名字、損益算錯")
    errors += [f"CSV 還有舊股名「{old}」，對照表已改成「{new}」：同一檔變兩個名字、損益會錯，請把 CSV 舊列改名"
               for old, new in sorted(split_names(csv_rows, name_map).items())]

    # ── 依日期排序後檢查、去重、寫入 ──
    pending.sort(key=lambda p: p[0])
    # 同一天有內容不同的憑單（例如更正版）：分不出哪份才對，先不入帳，等人工確認
    conflict = {b[0] for a, b in zip(pending, pending[1:]) if a[0] == b[0] and a[2] != b[2]}
    errors += [f"{d} 同一天有內容不同的憑單，未入帳，請人工確認" for d in sorted(conflict)]
    pending = [p for p in pending if p[0] not in conflict]
    for bill_date, stem, rows in pending:
        good, bad = [], []
        for r in rows:
            err = check_row(r)
            (bad if err else good).append((r, err))
        for r, err in bad:
            errors.append(f"{bill_date} {r.get('name', '')} {r.get('side', '')}：{err}")
        # 用（日期,股名,交易別,股數,價格）去重；同一天同價多筆 → 比既有筆數多出來的才補
        seen = Counter()
        to_add = []
        for r, _ in good:
            k = row_key(r)
            seen[k] += 1
            if seen[k] > existing.get(k, 0):
                to_add.append(r)
        label = f"email:{stem}"
        for r in to_add:
            log(f"  {'[測試] 會新增' if test_mode else '✅ 新增'}：{r['date']} {r['name']} {r['lot']}{r['side']} "
                f"{r['shares']}股 @{r['price']} 應收付 {r['net']}")
            added.append(f"{r['date'][5:]} {r['name']} {r['side']} {r['shares']}股@{r['price']}")
        skipped = len(good) - len(to_add)
        if skipped and test_mode:
            log(f"  [測試] {bill_date} 已在 CSV：{skipped} 筆")
        if not test_mode:
            if to_add:
                append_csv(csv_path, [fmt_row(r, label) for r in to_add])
                for r in to_add:
                    existing[row_key(r)] += 1
            if not bad and stem not in failed_stems:  # 有錯的憑單不標已處理，下次會再試
                mark_done(stem)

    # ── 收尾 ──
    for e in errors:
        log(f"  ❌ {e}")
    summary = f"新增 {len(added)} 筆、失敗 {len(errors)} 筆、月對帳單 {len(monthly_new)} 份"
    log(("[測試] " if test_mode else "") + summary)
    if not test_mode and (added or errors):
        msg = summary
        if added:
            msg += "\n" + "\n".join(added[:8]) + ("\n…" if len(added) > 8 else "")
        if errors:
            msg += "\n失敗：" + "；".join(errors[:3])
        pushover(s, msg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"  # 不用 repr：編碼錯誤的 repr 會帶出整串密碼
        log(f"❌ 執行錯誤：{detail}")
        if not {"--test", "--dump"} & set(sys.argv):
            pushover(load_settings(), f"交割憑單同步中斷，這次沒有跑完：{detail}")
        sys.exit(1)
