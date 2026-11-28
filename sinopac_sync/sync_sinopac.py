r"""
永豐金證券「證券月對帳單」自動同步（2026-11-28 新增）

第一步　抓信：從 Gmail 把永豐金證券寄來的「證券月對帳單」PDF 存到 月對帳單\證券月對帳單_YYYYMM.pdf
          （寄件者 bhu.sinotrade.com.tw；同一個寄件者也寄期貨對帳單，那些不抓）
第二步　入帳：parse_sinopac.py 把每一份的「證券庫存」讀出來，寫成
          stock_monitor\data\trades\sinopac_holdings.csv   每份對帳單的月底庫存（代號、股數、成本、參考市值）
          stock_monitor\data\trades\sinopac_check.json     有哪幾份、各自的截止日、核對有沒有過
          （戰情室「更新本月數字」的「永豐 台股」一鍵帶入讀的就是這兩個檔）

重點：
  - 永豐只有「當月有成交」才會寄月對帳單。沒寄的月份＝那個月沒買賣，庫存跟上一份一樣。
  - 所以這份庫存有時間差：這個月才買賣的，要等下個月初對帳單寄來才會反映。
  - PDF 有上鎖。密碼先看 設定.txt 的「PDF密碼」；留空就用「沿用PDF密碼設定」指到的那個設定檔裡的 PDF密碼。
    程式不會把密碼印出來。
  - 核對沒過的那一份不會寫進庫存檔（寧可缺、不要錯）：版面看不懂、加總對不上、有融資或融券的列都算沒過。

用法：
  雙擊「執行同步.bat」                      → 抓信＋入帳＋核對
  py -3 -X utf8 sync_sinopac.py --test      → 只列出會抓哪些信，不寫檔、不推播
  py -3 -X utf8 sync_sinopac.py --reparse   → 不連 Gmail，只用已下載的 PDF 重算一次
  py -3 -X utf8 sync_sinopac.py --dump 月對帳單\證券月對帳單_YYYYMM.pdf   → 印出 PDF 解開後的文字（除錯用，會有金額）

排程：由 stock_monitor\local_runner.py（本機準時跑）每天叫（工作代號 sinopac）。
  有新的對帳單入帳 → 推播；抓信失敗、PDF 打不開、核對沒過 → 回傳失敗，由本機準時跑推「失敗／已恢復」。
"""
import csv
import email
import email.utils
import hashlib
import imaplib
import json
import os
import re
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import decode_header
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_sinopac as ps  # noqa: E402

SETTINGS_FILE = HERE / "設定.txt"
LOG_FILE = HERE / "同步紀錄.txt"
SEEN_FILE = HERE / "已下載清單.txt"
PDF_DIR = HERE / "月對帳單"
OUT_DIR = HERE.parent / "data" / "trades"
HOLDINGS_CSV = OUT_DIR / "sinopac_holdings.csv"
CHECK_JSON = OUT_DIR / "sinopac_check.json"
SENDER = "bhu.sinotrade.com.tw"
SUBJECT_KEY = "證券月對帳單"
CSV_HEADER = ["as_of", "ym", "kind", "code", "name", "shares", "avg_cost", "cost", "ref_price", "ref_value"]
NOTIFY_TITLE = "永豐月對帳單"


# ───────────────────────── 小工具 ─────────────────────────
def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M} {msg}"
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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
    """Gmail 帳密留空 → 順著「沿用Gmail設定」一路找；PDF 密碼留空 → 用「沿用PDF密碼設定」那個檔的。"""
    s = read_kv(SETTINGS_FILE)
    s.setdefault("GMAIL帳號", "")
    s.setdefault("應用程式密碼", "")
    nxt, hops = s.get("沿用Gmail設定", ""), 0
    while (not s["GMAIL帳號"] or not s["應用程式密碼"]) and nxt and hops < 4:
        other = read_kv(nxt)
        s["GMAIL帳號"] = s["GMAIL帳號"] or other.get("GMAIL帳號", "")
        s["應用程式密碼"] = s["應用程式密碼"] or other.get("應用程式密碼", "")
        nxt, hops = other.get("沿用Gmail設定", ""), hops + 1
    s["應用程式密碼"] = s["應用程式密碼"].replace(" ", "")
    pw = s.get("PDF密碼", "").strip()
    if not pw:
        pw = read_kv(s.get("沿用PDF密碼設定", "")).get("PDF密碼", "").strip()
    s["PDF密碼"] = pw
    return s


def decode_str(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        out.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(out)


def pushover(settings, msg):
    if os.environ.get("STOCK_DRY_RUN") == "1":
        log(f"  （試跑，不推播）{msg}")
        return
    path = settings.get("Pushover設定檔", "")
    if not path or not Path(path).exists():
        return
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        if not cfg.get("enabled", True):
            return
        body = urllib.parse.urlencode({
            "token": cfg["api_token"], "user": cfg["user_key"],
            "title": NOTIFY_TITLE, "message": msg, "priority": -1}).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", body, timeout=15)
    except Exception as e:
        log(f"  Pushover 推播失敗：{type(e).__name__}")


def open_pdf(data, password):
    """打得開回 doc，打不開回 None（密碼原樣、全大寫各試一次）。"""
    doc = fitz.open(stream=data, filetype="pdf")
    if not doc.needs_pass:
        return doc
    for p in dict.fromkeys([password, password.upper()]):
        if p and doc.authenticate(p):
            return doc
    return None


def all_mail_folder(M):
    """找 Gmail『所有郵件』資料夾（名稱會因介面語言不同，用 \\All 旗標找）"""
    typ, folders = M.list()
    for f in folders or []:
        line = f.decode(errors="replace")
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            return '"' + m.group(1) + '"'
    return "INBOX"


# ───────────────────────── 第一步：抓信 ─────────────────────────
def fetch_new(settings, test_mode):
    """回傳 (這次新存的檔名清單, 錯誤訊息清單)。"""
    user, pwd, pdf_pw = settings["GMAIL帳號"], settings["應用程式密碼"], settings["PDF密碼"]
    since = settings.get("起始日期", "2022-01-01").replace("-", "/")
    seen = set()
    if SEEN_FILE.exists():
        seen = {l.strip() for l in SEEN_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    saved, errors = [], []
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
    M.login(user, pwd)
    try:
        M.select(all_mail_folder(M), readonly=True)
        # X-GM-RAW 不能放中文，主旨用 Python 再分
        typ, data = M.search(None, "X-GM-RAW", f'"from:{SENDER} after:{since}"')
        ids = data[0].split() if data and data[0] else []
        todo = []
        for mid in ids:
            typ, hd = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
            h = email.message_from_bytes(hd[0][1])
            if SUBJECT_KEY not in decode_str(h.get("Subject")):
                continue
            key = hashlib.sha256((h.get("Message-ID") or h.get("Date") or "").encode()).hexdigest()[:20]
            try:
                sent = email.utils.parsedate_to_datetime(h.get("Date")).date()
            except Exception:
                sent = None
            todo.append((mid, key, sent))
        new = [(mid, key, sent) for mid, key, sent in todo if key not in seen]
        log(f"[Gmail] {since} 起共 {len(todo)} 封永豐證券月對帳單，其中 {len(new)} 封還沒抓過")
        for mid, key, sent in new:
            if test_mode:
                log(f"  （試跑）會抓：{sent} 寄的那一封")
                continue
            typ, full = M.fetch(mid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1])
            pdfs = []
            for part in msg.walk():
                payload = part.get_payload(decode=True) if part.get_filename() else None
                if payload and payload[:4] == b"%PDF":
                    pdfs.append(payload)
            if not pdfs:
                errors.append(f"{sent} 寄的信沒有 PDF 附件")
                continue
            ok_all = True
            for data_pdf in pdfs:
                doc = open_pdf(data_pdf, pdf_pw)
                if doc is None:
                    errors.append(f"{sent} 寄的 PDF 打不開（密碼不對？請看 設定.txt 的「PDF密碼」）")
                    ok_all = False
                    continue
                ym = ps.parse_statement(doc)["ym"] or (f"{sent:%Y%m}_寄件日" if sent else "未知")
                PDF_DIR.mkdir(exist_ok=True)
                target = PDF_DIR / f"證券月對帳單_{ym}.pdf"
                n = 2
                while target.exists() and target.read_bytes() != data_pdf:   # 同月份第二份（更正版）不蓋掉舊的
                    target = PDF_DIR / f"證券月對帳單_{ym}_{n}.pdf"
                    n += 1
                if not target.exists():
                    tmp = target.with_suffix(".tmp")
                    tmp.write_bytes(data_pdf)
                    tmp.replace(target)             # 先寫暫存再換名，不會留下寫到一半的 PDF
                    saved.append(target.name)
                    log(f"  存好 {target.name}")
            if ok_all:
                with open(SEEN_FILE, "a", encoding="utf-8") as f:
                    f.write(key + "\n")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return saved, errors


# ───────────────────────── 第二步：入帳 ─────────────────────────
def _file_order(path):
    """證券月對帳單_YYYYMM.pdf、_YYYYMM_2.pdf、_YYYYMM_10.pdf → 照月份、再照後面的數字排。"""
    m = re.search(r"_([0-9]{6})(?:_([0-9]+))?[.]pdf$", path.name)
    return (m.group(1), int(m.group(2) or 1)) if m else (path.name, 0)


def rebuild(settings, write=True, fetch_errors=()):
    """用已下載的全部 PDF 重算。回傳 (statements, problems)。statements＝[{ym, as_of, n, ok, file}]（舊→新）。
    fetch_errors：這次抓信遇到的問題，一起寫進結果檔（戰情室的燈號才看得到）。"""
    pdf_pw = settings["PDF密碼"]
    by_ym, problems = {}, []
    for path in sorted(PDF_DIR.glob("證券月對帳單_*.pdf"), key=_file_order):
        doc = open_pdf(path.read_bytes(), pdf_pw)
        if doc is None:
            problems.append(f"{path.name} 打不開（密碼不對？）")
            continue
        st = ps.parse_statement(doc)
        bad = [k for k, v in st["checks"].items() if not v]
        if bad:
            problems.append(f"{path.name} 核對沒過：{'、'.join(bad)}")
        if st["ym"]:
            # 核對沒過、連截止日都讀不到的那一份也要列出來（截止日先用月底）：戰情室才知道「那個月有一份用不了」，不會默默退回用舊的
            st["as_of"] = st["as_of"] or ps.month_end(st["ym"])
            st["file"], st["ok"] = path.name, not bad
            by_ym[st["ym"]] = st          # 同月份有 _2（更正版）時，檔名排在後面的蓋掉前面的
    stmts = sorted(by_ym.values(), key=lambda s: s["as_of"])
    for st in stmts:
        log(f"  {st['ym']}：庫存 {len(st['holdings'])} 檔（截止 {st['as_of']}），核對{'全過' if st['ok'] else '沒過'}")
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HOLDINGS_CSV.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(CSV_HEADER)
            for st in stmts:
                if not st["ok"]:
                    continue                      # 核對沒過的那一份不寫（寧可缺，不要錯）
                for h in st["holdings"]:
                    w.writerow([st["as_of"].isoformat(), st["ym"], h["kind"], h["code"], h["name"],
                                int(round(h["shares"])), h.get("avg_cost"), int(round(h["cost"])),
                                h.get("ref_price"), int(round(h["ref_value"]))])
        tmp.replace(HOLDINGS_CSV)
        good = [s for s in stmts if s["ok"]]
        info = {
            "_說明": "永豐金證券月對帳單同步結果（sync_sinopac.py 寫的，戰情室只讀）。只有當月有成交才會有對帳單。",
            "ok": not problems and not fetch_errors,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "statements": [{"ym": s["ym"], "as_of": s["as_of"].isoformat(), "n": len(s["holdings"]), "ok": s["ok"]} for s in stmts],
            "latest_ym": good[-1]["ym"] if good else None,
            "latest_as_of": good[-1]["as_of"].isoformat() if good else None,
            "problems": list(problems) + ["抓信：" + str(e) for e in fetch_errors],
        }
        tmp = CHECK_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CHECK_JSON)
    return stmts, problems


# ───────────────────────── 主流程 ─────────────────────────
def main():
    argv = sys.argv[1:]
    s = load_settings()
    if "--dump" in argv:
        doc = open_pdf(Path(argv[argv.index("--dump") + 1]).read_bytes(), s["PDF密碼"])
        if doc is None:
            print("打不開（密碼不對？）")
            return 1
        print("\f".join(p.get_text("text") for p in doc))
        return 0
    test_mode = "--test" in argv
    if not s["PDF密碼"]:
        log("❌ 找不到 PDF 密碼：設定.txt 的「PDF密碼」是空的，「沿用PDF密碼設定」指的檔裡也沒有。")
        return 1
    errors, saved = [], []
    if "--reparse" not in argv:
        if not s["GMAIL帳號"] or not s["應用程式密碼"]:
            log("❌ 設定.txt 還沒填 Gmail 帳號或應用程式密碼（也找不到可沿用的設定）")
            return 1
        try:
            saved, errors = fetch_new(s, test_mode)
        except imaplib.IMAP4.error as e:
            log(f"❌ Gmail 登入或讀信失敗：{type(e).__name__}")
            return 1
        except (TimeoutError, ConnectionError, ssl.SSLError, socket.gaierror) as e:
            log(f"❌ 連不上 Gmail：{type(e).__name__}")
            return 1
        except OSError as e:                         # 例如月對帳單資料夾寫不進去
            log(f"❌ 存檔失敗（{type(e).__name__}）：請看 sinopac_sync 資料夾能不能寫入")
            return 1
    if test_mode:
        log("（試跑：沒有寫任何檔）")
        return 0
    stmts, problems = rebuild(s, fetch_errors=errors)
    for p in errors + problems:
        log(f"⚠ {p}")
    # 只講「這次新存、而且核對有過」的那幾份；核對沒過的不說已入帳（那種會回傳失敗，由排程另外推播）
    new_ok = [x for x in stmts if x["ok"] and x.get("file") in saved]
    if new_ok:
        last = new_ok[-1]
        pushover(s, f"{last['ym'][:4]} 年 {int(last['ym'][4:])} 月的對帳單已入帳（庫存 {len(last['holdings'])} 檔）。"
                    "戰情室「更新本月數字」的永豐那格可以按帶入。")
    log(f"完成：新存 {len(saved)} 份；目前共 {len(stmts)} 份對帳單" + ("" if not (errors or problems) else f"；有 {len(errors) + len(problems)} 個問題"))
    return 1 if (errors or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
