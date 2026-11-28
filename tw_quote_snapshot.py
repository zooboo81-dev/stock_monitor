"""台股收盤價快照 — 給戰情室「➕ 更新本月數字」的「台新 台股」一鍵帶入（股數 × 最近一個收盤日的官方收盤價）

為什麼要有這支（2026-11-28）：
  戰情室要用「台新交割明細推算的持股 × 當天官方收盤價」估台股市值（回測最近 7 次填表，差 0.0%～1.3%），
  但硬碟上沒有個股收盤價的檔，戰情室又規定不能自己連網抓價 → 比照美股（us_quote_snapshot.py）：
  由這台電腦的排程（local_runner.py 的工作 tw_quotes）每天存一份全市場收盤價，戰情室只讀這個檔。

做法：
  1. 上市：證交所 MI_INDEX（type=ALLBUT0999）從今天往回找最多 10 天，取第一個有個股資料的交易日
     （欄位用名稱找：證券代號、證券名稱、收盤價；收盤價不是正數＝當天沒成交，跳過）
  2. 上櫃：櫃買中心 OpenAPI tpex_mainboard_daily_close_quotes（只給最新一日；Date 是民國 7 碼）
  3. 寫 data/trades/tw_quotes.json（不上 GitHub）。內容是全市場公開行情，**這支不讀任何持股檔**。
     - 這次沒抓到、但上一份快照有而且是 10 天內的價 → 沿用並標 stale（例如停牌、上櫃這次沒抓到）
     - 上市抓不到 → 不覆蓋舊檔、回傳 1（排程會推「失敗」，戰情室照樣用上次的快照）
     - 上市有、上櫃抓不到 → 照樣寫（上櫃沿用舊檔、標 stale），印一行提醒，回傳 0
  已經是最新（舊檔的上市、上櫃日期都是今天）→ 不連網，幾秒就結束。

時間：證交所約 14:00～14:30 公布當天行情、櫃買中心 OpenAPI 晚一點 → 排程 14:40 抓；
      16:10、19:30 再跑：補抓上櫃還沒更新的。兩邊都是今天的了就直接跳過。

用法：
  py -3 -X utf8 tw_quote_snapshot.py             # 排程用（已是最新就跳過）
  py -3 -X utf8 tw_quote_snapshot.py --force     # 不管是不是最新都重抓
  py -3 -X utf8 tw_quote_snapshot.py --dry-run   # 照抓、只印摘要（日期、檔數），不寫檔
  py -3 -X utf8 tw_quote_snapshot.py --out 路徑  # 寫到別處（測試用）
  py -3 -X utf8 local_runner.py run tw_quotes --no-git   # 用排程的方式跑一次
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# pythonw 排程沒有 stdout/stderr
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests  # noqa: E402

from market_data import TW_TZ, taiwan_today  # noqa: E402

ROOT = Path(__file__).resolve().parent
TRADES = ROOT / "data" / "trades"
OUT = TRADES / "tw_quotes.json"             # 這支的輸出，戰情室讀

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
MAX_BACK = 10           # 上市往回找幾天
TWSE_GAP = 1.5          # 證交所兩次請求之間至少等幾秒（限流；規定 1.2 秒以上）
RETRIES, RETRY_SLEEP = 2, 5   # 連線出錯（不是「那天沒資料」）時同一天最多試幾次、中間等幾秒
KEEP_DAYS = 10          # 這次沒抓到的代號，舊價在幾天內才沿用（標 stale）；更舊的丟掉


# ---------------------------------------------------------------- 小工具
def _num(v) -> float | None:
    """'1,234.50' → 1234.5；'--'、'0.00'、空白、不是數字 → None（收盤價一定要是正數）"""
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def weekdays_between(d1: date, d2: date) -> int:
    """d1 之後到 d2（含）有幾個週一～五（粗略當成交易日數；遇到國定假日會多算，寧可晚一點吵）"""
    n, x = 0, d1
    while x < d2:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return n


def load_json(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return v if isinstance(v, dict) else {}


def write_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def make_session() -> requests.Session:
    """連線方式跟 market_data.py 一樣：User-Agent＋（有裝的話）用 Windows 的憑證庫"""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    return s


# ---------------------------------------------------------------- 抓價
def parse_twse(j: dict, d: date) -> dict:
    """MI_INDEX 的 JSON → {代號: {name, close, market, date}}（只取有「證券代號、證券名稱、收盤價」的表格）"""
    out: dict = {}
    for t in j.get("tables") or []:
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if not {"證券代號", "證券名稱", "收盤價"} <= set(f):
            continue
        ic, iname, iclose = f.index("證券代號"), f.index("證券名稱"), f.index("收盤價")
        for row in t.get("data") or []:
            try:
                code, name, close = str(row[ic]).strip(), str(row[iname]).strip(), _num(row[iclose])
            except (IndexError, TypeError):
                continue
            if code and close is not None:          # 收盤價不是正數＝當天沒成交
                out[code] = {"name": name, "close": close, "market": "TSE", "date": d.isoformat()}
    return out


def fetch_twse(session: requests.Session, today: date) -> tuple[date | None, dict, str]:
    """上市：從今天往回找最多 10 天，第一個 stat == OK 而且有個股資料的日子。回傳 (日期, 行情, 抓不到的原因)"""
    d, why, asked = today, "", False
    for _ in range(MAX_BACK):
        if d.weekday() < 5:                         # 週末不會有行情，不浪費請求
            for attempt in range(RETRIES):
                if asked:
                    time.sleep(TWSE_GAP)            # 證交所限流
                asked = True
                try:
                    j = session.get(TWSE_URL.format(ymd=d.strftime("%Y%m%d")), timeout=20).json()
                except Exception as e:  # noqa: BLE001
                    why = f"{d:%m/%d} 連不上證交所（{type(e).__name__}: {str(e)[:120]}）"
                    if attempt + 1 < RETRIES:
                        time.sleep(RETRY_SLEEP)
                    continue
                if not isinstance(j, dict):
                    why = f"{d:%m/%d} 證交所回傳的格式看不懂"
                    break
                if j.get("stat") == "OK":
                    q = parse_twse(j, d)
                    if q:
                        return d, q, ""
                    why = f"{d:%m/%d} 證交所有回應但沒有個股收盤價（表格欄位可能改了）"
                else:
                    why = why or f"往回 {MAX_BACK} 天證交所都說沒有資料（最後：{str(j.get('stat'))[:60]}）"
                break                               # 有回應（不管有沒有資料）→ 不用重試，換前一天
        d -= timedelta(days=1)
    return None, {}, why or f"往回 {MAX_BACK} 天都沒有上市行情"


def roc_date(s) -> date | None:
    """櫃買中心的民國 7 碼日期 '1151127' → date(2026, 11, 27)"""
    s = str(s or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    try:
        return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
    except ValueError:
        return None


def parse_tpex(data) -> tuple[date | None, dict]:
    """櫃買中心 OpenAPI 的清單 → (日期, {代號: {name, close, market, date}})；日期取最常出現的那一天"""
    rows, days = [], {}
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        code, close, d = str(r.get("SecuritiesCompanyCode") or "").strip(), _num(r.get("Close")), roc_date(r.get("Date"))
        if not code or close is None or d is None:
            continue
        if len(code) == 6 and code[0] == "7":       # 上櫃權證（7 開頭 6 碼，兩千多檔）不收：上市那份本來就不含權證，
            continue                                # 而且權證的名字都以股票簡稱開頭，會害戰情室用股名對代號時對錯
        rows.append((code, str(r.get("CompanyName") or "").strip(), close, d))
        days[d] = days.get(d, 0) + 1
    if not rows:
        return None, {}
    main = max(days, key=lambda x: (days[x], x))
    return main, {c: {"name": n, "close": v, "market": "OTC", "date": d.isoformat()} for c, n, v, d in rows if d == main}


def fetch_tpex(session: requests.Session) -> tuple[date | None, dict, str]:
    """上櫃：櫃買中心 OpenAPI（只給最新一日）。回傳 (日期, 行情, 抓不到的原因)"""
    why = ""
    for attempt in range(RETRIES):
        try:
            d, q = parse_tpex(session.get(TPEX_URL, timeout=30).json())
        except Exception as e:  # noqa: BLE001
            why = f"連不上櫃買中心（{type(e).__name__}: {str(e)[:120]}）"
        else:
            if q:
                return d, q, ""
            why = "櫃買中心有回應但沒有個股收盤價（欄位可能改了）"
        if attempt + 1 < RETRIES:
            time.sleep(RETRY_SLEEP)
    return None, {}, why


# ---------------------------------------------------------------- 快照
def up_to_date(prev: dict, today: date) -> bool:
    """舊檔的上市、上櫃日期都已經是今天 → True（不用再連網）"""
    tw = prev.get("twse") if isinstance(prev.get("twse"), dict) else {}
    tp = prev.get("tpex") if isinstance(prev.get("tpex"), dict) else {}
    q = prev.get("quotes") if isinstance(prev.get("quotes"), dict) else {}
    return bool(q) and _d(tw.get("date")) == today and _d(tp.get("date")) == today


def merge(tw_day: date, tw_q: dict, tp_day: date | None, tp_q: dict, tp_why: str,
          prev: dict, today: date, now: datetime) -> tuple[dict, int, int]:
    """這次抓到的＋上一份（這次沒抓到、10 天內的沿用並標 stale）→ (新快照, 沿用幾檔, 丟掉幾檔)"""
    quotes: dict = {**tp_q, **tw_q}                  # 同一個代號兩邊都有（不該發生）→ 上市為準
    pq = prev.get("quotes") if isinstance(prev.get("quotes"), dict) else {}
    n_stale = n_drop = 0
    for code, old in pq.items():
        if str(code) in quotes or not isinstance(old, dict):
            continue
        od, oc = _d(old.get("date")), _num(old.get("close"))
        mk = old.get("market")
        if od is None or oc is None or mk not in ("TSE", "OTC") or (today - od).days > KEEP_DAYS:
            n_drop += 1
            continue
        if mk == "OTC" and len(str(code)) == 6 and str(code)[0] == "7":   # 上櫃權證不收（舊快照裡有的也不沿用）
            continue
        quotes[str(code)] = {"name": str(old.get("name") or "").strip(), "close": oc, "market": mk,
                             "date": od.isoformat(), "stale": True}
        n_stale += 1
    tpex = {"date": tp_day.isoformat() if tp_day else None, "n": len(tp_q)}
    if tp_why:
        tpex["error"] = tp_why
    return {
        "_說明": "台股全市場官方收盤價快照（tw_quote_snapshot.py 寫的，戰情室只讀）。公開行情，不含持股。"
                 "close＝收盤價、date＝這個價是哪一天的、market＝TSE 上市／OTC 上櫃；stale＝這次沒抓到、沿用上一份的價。",
        "as_of": tw_day.isoformat(),
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "證交所 MI_INDEX＋櫃買中心 OpenAPI",
        "twse": {"date": tw_day.isoformat(), "n": len(tw_q)},
        "tpex": tpex,
        "quotes": dict(sorted(quotes.items())),
    }, n_stale, n_drop


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台股全市場收盤價快照（給戰情室「更新本月數字」的台股帶入）")
    ap.add_argument("--force", action="store_true", help="不管是不是最新都重抓")
    ap.add_argument("--dry-run", action="store_true", help="照抓、只印摘要（日期、檔數），不寫檔")
    ap.add_argument("--out", default=str(OUT), help="輸出檔（預設 data/trades/tw_quotes.json）")
    a = ap.parse_args(argv)

    out_path = Path(a.out)
    today = taiwan_today()
    prev = load_json(out_path)
    if not (a.force or a.dry_run) and up_to_date(prev, today):
        print(f"ℹ️ 台股收盤價已是最新（上市、上櫃都是 {today} 收盤，{str(prev.get('fetched_at') or '')[11:16]} 抓的），這次不用抓。")
        return 0

    t0 = time.time()
    session = make_session()
    tw_day, tw_q, tw_why = fetch_twse(session, today)
    if tw_day is None:
        print(f"❌ 上市收盤價抓不到（{tw_why}）：舊的快照不動"
              + (f"（還是 {prev.get('as_of')} 收盤）" if prev.get("as_of") else ""))
        return 1
    old_tw = prev.get("twse") if isinstance(prev.get("twse"), dict) else {}
    old_tw_day = _d(old_tw.get("date"))
    pq = prev.get("quotes") if isinstance(prev.get("quotes"), dict) else {}
    tw_regressed = bool(old_tw_day and tw_day < old_tw_day and not a.dry_run)
    if tw_regressed:
        # 今天那次請求剛好出錯時會往前一天找；不能拿比較舊的行情蓋掉已經抓到的新行情（快照不可以倒退）。
        # → 上市那一半沿用現有快照的，照樣去補抓上櫃。
        print(f"⚠️ 這次只抓到 {tw_day} 的上市行情，比現有快照（{old_tw_day}）還舊（{tw_why or '證交所今天那次沒回應'}）："
              "上市沿用現有快照的，不倒退")
        tw_day = old_tw_day
        tw_q = {str(c): dict(q) for c, q in pq.items() if isinstance(q, dict) and q.get("market") == "TSE"}
    tp_day, tp_q, tp_why = fetch_tpex(session)
    old_tp_day = max((_d(q.get("date")) for q in pq.values()
                      if isinstance(q, dict) and q.get("market") == "OTC" and _d(q.get("date"))), default=None)
    if tp_day and old_tp_day and tp_day < old_tp_day:
        tp_day, tp_q, tp_why = None, {}, f"櫃買中心這次回的是 {tp_day} 的行情，比現有的（{old_tp_day}）還舊"
    if tp_day and tp_day < tw_day:
        for q in tp_q.values():                 # 上櫃這份比上市舊（櫃買中心還沒更新）→ 照實標成舊價
            q["stale"] = True
    snap, n_stale, n_drop = merge(tw_day, tw_q, tp_day, tp_q, tp_why, prev, today, datetime.now(TW_TZ))

    if tp_why:
        old_tp = prev.get("tpex") if isinstance(prev.get("tpex"), dict) else {}
        print(f"⚠️ 上櫃收盤價這次沒抓到（{tp_why}），上櫃沿用上一份"
              + (f"（{old_tp.get('date')}）" if old_tp.get("date") else "（上一份也沒有）") + "、標 stale；上市照樣更新")
    elif tp_day and tp_day < tw_day:
        print(f"ℹ️ 櫃買中心還停在 {tp_day}（上市已經有 {tw_day}），晚一點的排程會再補抓")
    if n_stale:
        print(f"ℹ️ {n_stale} 檔這次沒有收盤價（停牌、沒成交，或上櫃沒抓到），沿用上一份 {KEEP_DAYS} 天內的價、標 stale")
    if n_drop:
        print(f"ℹ️ {n_drop} 檔上一份的價已超過 {KEEP_DAYS} 天沒更新，這次丟掉")
    if not a.dry_run:
        write_json(out_path, snap)
    ok = not tp_why and tp_day == tw_day
    print(f"{'✅' if ok else '⚠️'} 台股收盤價快照：上市 {tw_day}（{len(tw_q)} 檔）、"
          f"上櫃 {tp_day or '—'}（{len(tp_q)} 檔）、沿用舊價 {n_stale} 檔，共 {len(snap['quotes'])} 檔，{time.time() - t0:.0f} 秒"
          + ("（--dry-run：沒有寫檔）" if a.dry_run else f" → {out_path.name}"))
    # 上櫃偶爾一次沒抓到、或 14:40 櫃買中心還沒更新（落後 1 個交易日）都不吵；
    # 上櫃的價落後上市 2 個交易日以上（不管是連不上、還是櫃買中心一直回舊日期）→ 回傳失敗，讓排程推播
    last_otc = max((_d(q.get("date")) for q in snap["quotes"].values() if q.get("market") == "OTC" and _d(q.get("date"))),
                   default=None)
    lag = weekdays_between(last_otc, tw_day) if last_otc else 99
    if lag >= 2:
        print(f"❌ 上櫃收盤價停在 {last_otc or '（完全沒有）'}，已經落後上市 {lag if last_otc else '很多'} 個交易日")
        return 1
    if tw_regressed and tw_day != today:
        print("❌ 今天的上市收盤價這次沒抓到（快照維持原樣，沒有倒退）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
