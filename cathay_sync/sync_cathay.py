r"""
國泰證券美股（複委託）對帳單自動同步

第一步　抓信：從 Gmail 把國泰綜合證券寄來的信存到這個資料夾
  買賣報告書\買賣報告書_YYYYMMDD.pdf        每個有成交（或除息入帳）的日子一封，隔天下午寄，檔名用成交日
  月對帳單\月對帳單_YYYYMM.pdf              每月初寄上個月的「外國有價證券對帳單」
  海外所得\海外所得_NNN年度.pdf             每年 3 月寄
  非交易通知\非交易通知_YYYYMMDD_代號.html  除息預告（信件本文就是表格，沒有上鎖）
  核對用_每日持有代號.json                  每天的「美股庫存電子報」列了哪些代號（只有代號、沒有股數）
第二步　入帳：parse_cathay.py 把 PDF 讀成 stock_monitor\data\trades\cathay_us_*.csv，並做 8 項核對
          （戰情室「美股對帳單」頁讀的就是這幾個檔）

PDF 都有上鎖，密碼＝身分證字號整串（設定.txt 的「PDF密碼」，自己填；程式不會把它印出來）。

用法：
  雙擊「執行同步.bat」                     → 抓信＋入帳＋核對
  py -3 -X utf8 sync_cathay.py --test      → 只列出會抓哪些信、會新增哪幾筆，不寫檔、不推播
  py -3 -X utf8 sync_cathay.py --dump 買賣報告書\買賣報告書_YYYYMMDD.pdf
                                           → 印出 PDF 解開後的文字（除錯用）；加 --words 連座標一起印
  py -3 -X utf8 parse_cathay.py --detail   → 不連 Gmail，只用已下載的檔重算一次並列出核對明細

排程：由 stock_monitor\local_runner.py（本機準時跑）每天 13:40、21:40 各叫一次（工作代號 cathay_us）。
  有新入帳 → 推播；核對沒過或抓信失敗 → 回傳失敗，由本機準時跑推「失敗／已恢復」。

信件重點（2026-11 確認）：
  - 寄件者都是 cathaysec.com.tw；用主旨分種類（搜尋條件不放中文，Gmail 的 X-GM-RAW 會出錯）
  - 附件檔名每封都一樣（客戶買賣報告書(姓名).pdf），所以改用信件本文寫的日期當檔名
  - 同一天收到第二份、內容又不一樣（更正版／重寄）→ 另存成 _2，不蓋掉舊的
"""
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "設定.txt"
LOG_FILE = HERE / "同步紀錄.txt"
SEEN_FILE = HERE / "已下載清單.txt"
INVENTORY_FILE = HERE / "核對用_每日持有代號.json"
DIRS = {"trade": HERE / "買賣報告書", "monthly": HERE / "月對帳單",
        "annual": HERE / "海外所得", "notice": HERE / "非交易通知"}
LABELS = {"trade": "買賣報告書", "monthly": "月對帳單", "annual": "海外所得通知單", "notice": "非交易通知"}
SENDER = "cathaysec.com.tw"
# 主旨關鍵字 → 種類（其他像行銷信一律不抓；庫存電子報另外處理，只記代號）
KINDS = [("買賣報告書", "trade"), ("外國有價證券對帳單", "monthly"),
         ("海外所得資料明細通知單", "annual"), ("複委託非交易訊息通知", "notice")]
INVENTORY_SUBJECT = "美股庫存電子報"
# 電子報裡每一檔的代號那一格（2026-11 的版面）
RE_TICK = re.compile(r'color:\s*#BBBBBB;\s*font-size:\s*26px;[^"]*">\s*([A-Za-z0-9.\-]+)\s*</td>')
NOTIFY_TITLE = "國泰美股對帳單"


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
        if LOG_FILE.stat().st_size > 1_500_000:  # 太大就只留最後 4000 行
            keep = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
            LOG_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
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
    """Gmail 帳密留空 → 順著「沿用Gmail設定」一路找（台新同步 → AI 月費請款）。"""
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
    s["PDF密碼"] = s.get("PDF密碼", "").strip().upper()  # 信上寫英文字母要大寫，打成小寫也幫忙轉
    return s


def decode_str(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        out.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(out)


def pushover(settings, msg):
    """直接打 Pushover（備援：stock_monitor 的通知模組載不進來時才用）"""
    path = settings.get("Pushover設定檔", "")
    if not path or not Path(path).exists():
        return
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        if not cfg.get("enabled", True):
            return
        body = urllib.parse.urlencode({
            "token": cfg["api_token"], "user": cfg["user_key"],
            "title": NOTIFY_TITLE, "message": msg}).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", body, timeout=15)
    except Exception as e:
        log(f"  Pushover 推播失敗：{e}")


def notify(settings, msg):
    """推到手機＋寫進台股儀表板的通知中心（走 stock_monitor 統一的通知模組；試跑時它只會印出來）"""
    try:
        if str(HERE.parent) not in sys.path:
            sys.path.insert(0, str(HERE.parent))
        import telegram_notify
        telegram_notify.send(NOTIFY_TITLE, msg)
    except Exception as e:
        log(f"  通知模組用不了（{type(e).__name__}），改直接推 Pushover")
        pushover(settings, msg)


# ───────────────────────── HTML ─────────────────────────
class _Text(HTMLParser):
    """HTML → 純文字（只用來找信件本文裡的日期）"""
    def __init__(self):
        super().__init__()
        self.buf, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "tr", "p", "div", "li", "table"):
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def html_text(html):
    p = _Text()
    p.feed(html)
    return re.sub(r"[ \t\xa0]+", " ", "".join(p.buf))


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


def notice_rows(html):
    """非交易通知的表格 → list[dict]（鍵＝表頭：分行代碼、客戶帳號、市場代碼、商品代碼、原庫存、非交易種類、生效日、說明）"""
    tp = _TableRows()
    tp.feed(html)
    header, out = None, []
    for cells in tp.rows:
        if header is None:
            if "商品代碼" in cells:
                header = cells
            continue
        if any(cells):
            out.append(dict(zip(header, cells)))
    return out


# ───────────────────────── PDF ─────────────────────────
def open_pdf(data, password):
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            raise ValueError("PDF 密碼錯誤或未填")
    return doc


def pdf_files():
    """已下載的 PDF：[(種類, 路徑), ...]，依檔名排序"""
    return [(kind, p) for kind in ("trade", "monthly", "annual")
            for p in sorted(DIRS[kind].glob("*.pdf"))]


def check_password(password):
    """每份 PDF 試開一次 → (開得了幾份, [開不了的檔名])。不印密碼。"""
    ok, bad = 0, []
    for _, p in pdf_files():
        try:
            open_pdf(p.read_bytes(), password).close()
            ok += 1
        except Exception:
            bad.append(p.name)
    return ok, bad


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


def classify(subject):
    for word, kind in KINDS:
        if word in subject:
            return kind
    return None


def list_mails(M, since):
    """國泰證券寄來的信：[(序號, Message-ID, 主旨, 寄信時間), ...]。標頭一次抓 200 封，不要一封一封來回。"""
    typ, data = M.search(None, "X-GM-RAW", f'"from:{SENDER} after:{since}"')
    ids = data[0].split() if data and data[0] else []
    mails = []
    for k in range(0, len(ids), 200):
        typ, resp = M.fetch(b",".join(ids[k:k + 200]), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE)])")
        for item in resp:
            if not isinstance(item, tuple):
                continue
            h = email.message_from_bytes(item[1])
            subject = decode_str(h.get("Subject")).replace("\r\n", "").replace("\n", "")
            try:
                sent = email.utils.parsedate_to_datetime(h.get("Date"))
            except Exception:
                sent = None
            mid = (h.get("Message-ID") or "").strip() or f"{subject}|{h.get('Date')}"
            mails.append((item[0].split()[0], mid, subject, sent))
    return mails


def mail_body(msg):
    """信件本文（HTML）→ (原始位元組, 文字)"""
    for part in msg.walk():
        if part.is_multipart() or part.get_filename():
            continue
        if part.get_content_type() == "text/html":
            raw = part.get_payload(decode=True) or b""
            return raw, raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    return b"", ""


def pdf_parts(msg):
    """信裡的 PDF 附件內容（附件檔名來自信件、每封都一樣，不拿來用）"""
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        data = part.get_payload(decode=True) or b""
        if data[:4] == b"%PDF":
            out.append(data)
    return out


def stem_for(kind, subject, body_text, sent):
    """存檔用的檔名主幹；日期以信件本文寫的為準，讀不到才退回寄信日（檔名會標「寄於」）。"""
    if kind == "trade":
        m = re.search(r"這是您\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", body_text)
        if m:
            return f"買賣報告書_{int(m[1]):04d}{int(m[2]):02d}{int(m[3]):02d}"
    elif kind == "monthly":
        m = re.search(r"這是您\s*(\d{4})\s*年\s*(\d{1,2})\s*月份", body_text)
        if m:
            return f"月對帳單_{int(m[1]):04d}{int(m[2]):02d}"
    elif kind == "annual":
        m = re.search(r"(\d{2,3})\s*年度", subject)
        if m:
            return f"海外所得_{m[1]}年度"
    return f"{LABELS[kind]}_寄於{sent:%Y%m%d}" if sent else None


def save_unique(folder, stem, ext, data, test_mode):
    """同名同內容 → 已經有了；同名不同內容（更正版／重寄）→ 加 _2、_3… 另存，不蓋掉舊的。
    回傳 (路徑, "new"｜"same"｜"dup")。"""
    n = 1
    while True:
        p = folder / (f"{stem}{ext}" if n == 1 else f"{stem}_{n}{ext}")
        if not p.exists():
            if not test_mode:
                folder.mkdir(exist_ok=True)
                p.write_bytes(data)
            return p, ("new" if n == 1 else "dup")
        if p.read_bytes() == data:
            return p, "same"
        n += 1


def _subject_day(subject):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", subject)
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None


def update_inventory(M, mails, test_mode):
    """每天的「美股庫存電子報」→ 核對用_每日持有代號.json（日期 → 代號清單）。只補還沒有的日期，回傳補了幾天。"""
    inv = {}
    if INVENTORY_FILE.exists():
        try:
            inv = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
        except ValueError:
            log("  ⚠ 核對用_每日持有代號.json 讀不了，重新建立")
    todo = [seq for seq, _mid, subject, _sent in mails
            if INVENTORY_SUBJECT in subject and (_subject_day(subject) or "") not in inv and _subject_day(subject)]
    added = 0
    for k in range(0, len(todo), 25):
        typ, resp = M.fetch(b",".join(todo[k:k + 25]), "(BODY.PEEK[])")
        for item in resp:
            if not isinstance(item, tuple):
                continue
            msg = email.message_from_bytes(item[1])
            day = _subject_day(decode_str(msg.get("Subject")).replace("\r\n", "").replace("\n", ""))
            ticks = sorted(set(RE_TICK.findall(mail_body(msg)[1])))
            if not day:
                continue
            if not ticks:  # 不存空的：下次再試。若是電子報改版，這行會每次出現
                log(f"  ⚠ {day} 的庫存電子報讀不到代號（當天空手，或電子報改版了）")
                continue
            inv[day] = ticks
            added += 1
    if added and not test_mode:
        INVENTORY_FILE.write_text(json.dumps(dict(sorted(inv.items())), ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    return added


def fetch_mail(s, test_mode):
    """第一步：信 → 檔案。回傳 (新存的 [(種類, 檔名)], 錯誤訊息清單)。"""
    since = s.get("起始日期", "2025-06-01").replace("-", "/")
    seen = set()
    if SEEN_FILE.exists():
        seen = {l.split("\t")[0] for l in SEEN_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    new, errors = [], []
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
    M.login(s["GMAIL帳號"], s["應用程式密碼"])
    try:
        M.select(all_mail_folder(M), readonly=True)
        listed = list_mails(M, since)
        mails = [(seq, mid, sub, sent, classify(sub)) for seq, mid, sub, sent in listed]
        wanted = [m for m in mails if m[4]]
        count = {k: sum(1 for m in wanted if m[4] == k) for k in LABELS}
        log(f"[Gmail] {since} 起國泰證券信件 {len(mails)} 封，其中 " +
            "、".join(f"{LABELS[k]} {count[k]}" for k in LABELS))
        for seq, mid, subject, sent, kind in wanted:
            if mid in seen:
                continue
            msg = email.message_from_bytes(M.fetch(seq, "(BODY.PEEK[])")[1][0][1])
            raw_html, html = mail_body(msg)
            saved = []
            if kind == "notice":
                codes = [re.sub(r"[^A-Za-z0-9.]", "", r.get("商品代碼", "")) for r in notice_rows(html)]
                if not codes or not sent:
                    errors.append(f"非交易通知讀不到表格（{sent:%Y-%m-%d}）" if sent else "非交易通知沒有寄信日期")
                    continue
                stem = f"非交易通知_{sent:%Y%m%d}_{'_'.join(c for c in codes if c) or '未知'}"
                saved.append(save_unique(DIRS[kind], stem, ".html", raw_html, test_mode))
            else:
                pdfs = pdf_parts(msg)
                stem = stem_for(kind, subject, html_text(html), sent)
                if not pdfs or not stem:
                    errors.append(f"{LABELS[kind]}（{subject[:30]}）" + ("沒有 PDF 附件" if not pdfs else "看不出日期"))
                    continue
                if "寄於" in stem:
                    log(f"  ⚠ 信件本文讀不到帳單日期，先用寄信日當檔名：{stem}")
                saved += [save_unique(DIRS[kind], stem, ".pdf", data, test_mode) for data in pdfs]
            for path, status in saved:
                if status == "same":
                    continue
                if status == "dup":
                    log(f"  ⚠ 同一期收到第二份、內容不同（更正版或重寄），另存：{path.name}")
                log(f"  {'[測試] 會下載' if test_mode else '📄 已下載'}：{path.name}")
                new.append((kind, path.name))
            if not test_mode:
                with open(SEEN_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{mid}\t{kind}\t{'、'.join(p.name for p, _ in saved)}\n")
                seen.add(mid)
        try:
            days = update_inventory(M, listed, test_mode)
            if days:
                log(f"  庫存電子報補了 {days} 天的代號")
        except Exception as e:  # 電子報只是核對用，出錯不擋入帳
            log(f"  ⚠ 庫存電子報這次沒更新：{type(e).__name__}: {e}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return new, errors


# ───────────────────────── 主流程 ─────────────────────────
def dump(path, password, with_words):
    doc = open_pdf(path.read_bytes(), password)
    for n, page in enumerate(doc, 1):
        print(f"════════ 第 {n} 頁（{page.rect.width:.0f}×{page.rect.height:.0f}）════════")
        if with_words:
            for w in sorted(page.get_text("words"), key=lambda w: (round(w[1]), w[0])):
                print(f"{w[0]:7.1f} {w[1]:7.1f} {w[2]:7.1f} {w[3]:7.1f}  {w[4]}")
        else:
            print(page.get_text("text"))


def main():
    argv = sys.argv[1:]
    s = load_settings()
    if "--dump" in argv:
        dump(Path(argv[argv.index("--dump") + 1]), s["PDF密碼"], "--words" in argv)
        return 0

    # STOCK_DRY_RUN＝排程試跑（job_selftest）：當成 --test，不寫檔、不推播
    test_mode = "--test" in argv or bool(os.environ.get("STOCK_DRY_RUN"))
    tag = "[測試] " if test_mode else ""
    if not s["GMAIL帳號"] or not s["應用程式密碼"]:
        log("❌ 找不到 Gmail 帳號或應用程式密碼（設定.txt 留空時會沿用台新同步那份，也沒找到）")
        return 1

    # ── 第一步：抓信 ──
    try:
        new, errors = fetch_mail(s, test_mode)
    except imaplib.IMAP4.error as e:
        why = "Gmail 登入失敗（請確認應用程式密碼）" if "AUTH" in str(e).upper() else "Gmail 連線中斷"
        log(f"❌ {why}，國泰對帳單這次沒抓：{e}")
        return 1
    except OSError as e:
        log(f"❌ 連不上 Gmail（網路不通？），國泰對帳單這次沒抓：{type(e).__name__}: {e}")
        return 1
    for e in errors:
        log(f"  ❌ {e}")
    n_new = {k: sum(1 for kind, _ in new if kind == k) for k in LABELS}
    log(tag + "新下載 " + "、".join(f"{LABELS[k]} {n_new[k]}" for k in LABELS) + f"；失敗 {len(errors)} 筆")

    # ── 第二步：入帳＋核對 ──
    files = pdf_files()
    if not s["PDF密碼"]:
        log(f"❌ 設定.txt 的「PDF密碼」是空的：{len(files)} 份 PDF 打不開、沒辦法入帳。"
            "請在 cathay_sync\\設定.txt 的「PDF密碼=」後面填身分證字號。")
        return 1
    ok, bad = check_password(s["PDF密碼"])
    if bad:
        log(f"❌ PDF 密碼打不開 {len(bad)} 份（共 {len(files)} 份），例如 {bad[0]}。"
            "請確認設定.txt 的「PDF密碼」是整串身分證字號、英文字母大寫。")
        return 1

    import parse_cathay as pc
    before = pc.existing_keys()
    res = pc.run(s["PDF密碼"], write=not test_mode)
    added = pc.new_rows(res["ledger"], before)
    for line in pc.summary_lines(res):
        log(line)
    for r in added[:20]:
        log(f"  {tag or '✅ '}新入帳：{pc.describe(r)}")

    failed = [c for c in res["checks"] if not c["ok"]]
    monthly_new = sorted(Path(name).stem.replace("月對帳單_", "") for kind, name in new if kind == "monthly")
    if not test_mode and (added or monthly_new):
        lines = []
        if not before:
            lines.append(f"第一次建帳：共 {len(res['ledger'])} 筆")
        elif added:
            lines.append(f"新入帳 {len(added)} 筆")
            lines += [pc.describe(r) for r in added[:8]] + (["…"] if len(added) > 8 else [])
        if monthly_new:
            lines.append("月對帳單到了：" + "、".join(f"{m[:4]}-{m[4:]}" for m in monthly_new) + "（台幣金額定案）")
        lines.append(f"核對 {len(res['checks'])} 項全過" if not failed else
                     f"⚠ 核對有 {len(failed)} 項沒過：" + "；".join(c["name"][:20] for c in failed))
        notify(s, "\n".join(lines))

    # 排程推「失敗」時只帶最後十幾行，所以結論放最後
    if failed or errors:
        for c in failed:
            log(f"❌ 核對沒過：{c['name']}（{c['passed']}/{c['total']}）" +
                ("｜" + c["detail"][0] if c["detail"] else ""))
        log(f"❌ 國泰美股對帳單：抓信失敗 {len(errors)} 筆、核對沒過 {len(failed)} 項。"
            "資料夾 cathay_sync 的「同步紀錄.txt」有完整內容，把這則給 Claude 看就能查。")
        return 1
    log(f"{tag}✅ 國泰美股對帳單：帳本 {len(res['ledger'])} 筆、新入帳 {len(added)} 筆、核對 {len(res['checks'])} 項全過")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 只記「哪個檔第幾行」＋錯誤訊息；不用 repr（編碼錯誤的 repr 會帶出整串密碼）
        import traceback
        where = " ← ".join(f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(e.__traceback__)[-3:])
        log(f"❌ 國泰美股對帳單同步中斷，這次沒有跑完：{type(e).__name__}: {e}（{where}）")
        sys.exit(1)
