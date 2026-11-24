"""美股收盤價快照 — 給戰情室「🗽 美股對帳單」算「最近一個美股收盤日」的市值與未實現損益

為什麼要有這支（2026-11-24）：
  國泰複委託的市值只有月對帳單的月底數字（最久會舊到一個多月）。戰情室規定不能自己連網抓價，
  所以改由這台電腦的排程（local_runner.py 的工作 us_quotes）每天抓一次收盤價存成檔，戰情室只讀這個檔。

做法：
  1. 讀 data/trades/cathay_us_statement.csv（cathay_sync 整理的國泰交易明細，不上 GitHub）：
     買進股數－賣出股數 > 0 的代號＝目前持有
  2. 每檔抓 Yahoo（yfinance）日 K，取最近一根「已經收完盤」的收盤價（美東 16:30 以後當天那根才算）＋前一根（算當日漲跌）
  3. 美元兌台幣：Yahoo TWD=X 抓價當下最新一筆
  4. 寫 data/trades/cathay_us_quotes.json（不上 GitHub）
     - 某一檔這次抓不到 → 沿用上次的價格，標 stale（戰情室會標出是哪天的價）
     - 一檔都抓不到 → 不覆蓋舊檔、回傳 1（排程會推「失敗」，戰情室照樣顯示上次的資料）
  已經是最新（這個收盤日抓過了、持股都有價、匯率也有）→ 不連網，幾秒就結束。

時間：美股 16:00 收盤＝台灣早上 5 點（夏令時間 4 點）→ 排程 06:30 抓前一晚的收盤；
      15:00 再跑一次：補抓早上沒成功的，和中午國泰同步（14:20）新入帳的代號。
      週二～週六早上才有新收盤價；週日、週一跑到時快照已是最新，會直接跳過。

匯率說明（2026-11-24 對過 8 份月對帳單）：國泰月對帳單的月底匯率比市場中間價高 0～0.3，時點也不固定，
  所以同一天用 Yahoo 匯率算的台幣市值會跟月對帳單差一點；美元市值（股數×收盤價）會對得上。--verify 會印出差多少。

用法：
  py -3 -X utf8 us_quote_snapshot.py             # 排程用（已是最新就跳過）
  py -3 -X utf8 us_quote_snapshot.py --force     # 不管是不是最新都重抓
  py -3 -X utf8 us_quote_snapshot.py --dry-run   # 只抓、逐檔印出來，不寫檔
  py -3 -X utf8 us_quote_snapshot.py --verify    # 跟最新一份月對帳單比同一天的收盤價、市值（不寫檔）
  py -3 -X utf8 local_runner.py run us_quotes --no-git   # 用排程的方式跑一次
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

# pythonw 排程沒有 stdout/stderr
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
TRADES = ROOT / "data" / "trades"
STATEMENT = TRADES / "cathay_us_statement.csv"   # cathay_sync 整理的交易明細（買進／賣出／除息）
HOLDINGS = TRADES / "cathay_us_holdings.csv"     # 每月底庫存（--verify 對帳用）
OUT = TRADES / "cathay_us_quotes.json"           # 這支的輸出，戰情室讀

NY = ZoneInfo("America/New_York")
TW = timezone(timedelta(hours=8))
CLOSE_DONE = dtime(16, 30)     # 美東 16:30 以後當天的日 K 才算收完（16:00 收盤，Yahoo 幾分鐘後才定案）
FX_SYMBOL = "TWD=X"            # Yahoo 的美元兌台幣
RETRIES, RETRY_SLEEP = 3, 6    # 跟 bottom_scorecard／margin_fear_gauge 同一套：最多試 3 次、中間等 6 秒
YF_TIMEOUT = 20                # 每次連線最多等幾秒（yfinance 預設 10）
GIVE_UP_AFTER = 3              # 前幾檔全部抓不到 → 當成 Yahoo 暫時連不上，剩下的不再一檔一檔等
EPS = 1e-9


# ---------------------------------------------------------------- 小工具
def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _z(x: float, nd: int) -> float:
    """四捨五入後把 -0.0 變成 0.0（印差額時不要出現「-0.00」）"""
    return round(x, nd) + 0.0


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


def yahoo_symbol(code: str) -> str:
    """券商代號 → Yahoo 代號（分級股的點要換成橫線，例如 BRK.B → BRK-B）"""
    return code.strip().upper().replace(".", "-").replace("/", "-").replace(" ", "-")


# ---------------------------------------------------------------- 持股
def held_codes(path: Path) -> tuple[list, dict]:
    """交易明細 → 目前還持有的代號（買進股數－賣出股數 > 0）。回傳 (代號清單, 沒看過的交易種類 {種類: 列數})"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
        cols = set(rd.fieldnames or [])
    if rows and not {"code", "kind", "shares"} <= cols:
        raise ValueError(f"交易檔少了 code／kind／shares 欄位（現在的欄位：{sorted(cols)}）")
    net: dict = {}
    unknown: dict = {}
    for r in rows:
        code = str(r.get("code") or "").strip()
        kind = str(r.get("kind") or "").strip()
        sh = _num(r.get("shares"))
        if not code or sh is None:
            continue
        if kind == "買進":
            net[code] = net.get(code, 0.0) + sh
        elif kind == "賣出":
            net[code] = net.get(code, 0.0) - sh
        elif kind != "除息":
            unknown[kind] = unknown.get(kind, 0) + 1
    return sorted(c for c, s in net.items() if s > EPS), unknown


# ---------------------------------------------------------------- 抓價
def completed_closes(h, now_ny: datetime) -> list:
    """日 K → [(美東日期, 收盤價)]：去掉還在交易中的那一根、空值、不合理的價格"""
    out = []
    if h is None or not len(h) or "Close" not in h:
        return out
    for ts, v in h["Close"].items():
        try:
            d = ts.tz_convert(NY).date() if getattr(ts, "tzinfo", None) is not None else ts.date()
            v = float(v)
        except Exception:
            continue
        if not (math.isfinite(v) and v > 0):
            continue
        if d > now_ny.date() or (d == now_ny.date() and now_ny.time() < CLOSE_DONE):
            continue
        out.append((d, v))
    return out


def fetch_close(symbol: str, now_ny: datetime) -> dict:
    """Yahoo 日 K 最近一根已收盤的收盤價＋前一根。失敗回 {"error": 原因}"""
    import yfinance as yf
    err = ""
    for attempt in range(RETRIES):
        try:
            h = yf.Ticker(symbol).history(period="10d", auto_adjust=False, timeout=YF_TIMEOUT)
            bars = completed_closes(h, now_ny)
            if bars:
                d, c = bars[-1]
                prev = bars[-2][1] if len(bars) > 1 else None
                return {"close": round(c, 4), "date": d.isoformat(),
                        "prev_close": None if prev is None else round(prev, 4)}
            err = "Yahoo 沒有回傳已收盤的日 K（代號不對、剛上市，或暫時連不上）"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
        if attempt + 1 < RETRIES:
            print(f"  ⚠️ {symbol} 重試 {attempt + 1}/{RETRIES}：{err}")
            time.sleep(RETRY_SLEEP)
    return {"error": err}


def fetch_fx(now_utc: datetime) -> dict:
    """美元兌台幣：Yahoo TWD=X 最新一筆（1 小時線最後一根；抓不到改用日線）。失敗回 {"error": 原因}"""
    import yfinance as yf
    err = ""
    for attempt in range(RETRIES):
        for interval, period in (("1h", "5d"), ("1d", "10d")):
            try:
                h = yf.Ticker(FX_SYMBOL).history(period=period, interval=interval, auto_adjust=False,
                                                 timeout=YF_TIMEOUT)
                h = h.dropna(subset=["Close"]) if h is not None and len(h) else None
                if h is None or not len(h):
                    err = f"Yahoo {FX_SYMBOL}（{interval}）沒有回傳資料"
                    continue
                v = float(h["Close"].iloc[-1])
                if not (20 < v < 50):
                    err = f"Yahoo {FX_SYMBOL} 給的匯率 {v} 不合理"
                    continue
                ts = h.index[-1]
                ts = ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")
                at = ts.to_pydatetime() + timedelta(hours=1) if interval == "1h" else now_utc
                return {"rate": round(v, 4), "at": min(at, now_utc).astimezone(TW).isoformat(timespec="minutes"),
                        "source": f"Yahoo {FX_SYMBOL}"}
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {str(e)[:160]}"
        if attempt + 1 < RETRIES:
            print(f"  ⚠️ 匯率重試 {attempt + 1}/{RETRIES}：{err}")
            time.sleep(RETRY_SLEEP)
    return {"error": err}


# ---------------------------------------------------------------- 快照
def last_session(now_ny: datetime) -> date:
    """最近一個「應該已經收完盤」的美股交易日（只扣週末；美國假日那天會多抓一次，沒關係）"""
    d = now_ny.date() if now_ny.time() >= CLOSE_DONE else now_ny.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def up_to_date(prev: dict, codes: list, now_ny: datetime) -> str | None:
    """上次的快照已經是最新（同一個收盤日、持股每檔都有價、沒有沿用舊值、有匯率）→ 回傳說明；否則 None"""
    q = prev.get("quotes") if isinstance(prev.get("quotes"), dict) else {}
    fx = prev.get("fx") if isinstance(prev.get("fx"), dict) else {}
    want = last_session(now_ny).isoformat()
    if str(prev.get("as_of") or "") != want:
        return None
    if any(not isinstance(q.get(c), dict) or q[c].get("stale") or _num(q[c].get("close")) is None for c in codes):
        return None
    if fx.get("stale") or _num(fx.get("rate")) is None:
        return None
    at = str(prev.get("fetched_at") or "")[:16].replace("T", " ")
    return f"收盤價已經是最新（美股 {want} 收盤，{at} 抓的，{len(codes)} 檔都有）"


def merge(codes: list, fetched: dict, fx: dict, prev: dict, now_utc: datetime) -> dict:
    """這次抓到的＋上次的（這次沒抓到的沿用、標 stale）→ 新快照"""
    now_s = now_utc.astimezone(TW).isoformat(timespec="seconds")
    pq = prev.get("quotes") if isinstance(prev.get("quotes"), dict) else {}
    quotes, stale, missing = {}, [], []
    for c in codes:
        r = fetched.get(c) or {}
        old = pq.get(c) if isinstance(pq.get(c), dict) else None
        if "close" in r:
            quotes[c] = {**r, "fetched_at": now_s, "stale": False}
        elif old and _num(old.get("close")) is not None:
            quotes[c] = {**old, "stale": True, "error": r.get("error", ""), "last_try": now_s}
            stale.append(c)
        else:
            missing.append(c)
    fresh = [q["date"] for q in quotes.values() if not q["stale"]]
    pfx = prev.get("fx") if isinstance(prev.get("fx"), dict) else None
    if "rate" in fx:
        fx_out = {**fx, "stale": False}
    elif pfx and _num(pfx.get("rate")) is not None:
        fx_out = {**pfx, "stale": True, "error": fx.get("error", ""), "last_try": now_s}
    else:
        fx_out = None
    return {
        "_說明": "美股收盤價快照（stock_monitor 的 us_quote_snapshot.py 每天 06:30、15:00 寫；戰情室「美股對帳單」頁讀）。"
                 "close＝美元收盤價、date＝那根日 K 的美東日期、prev_close＝前一個收盤日；stale＝這次沒抓到、沿用上次的值。",
        "as_of": max(fresh) if fresh else None,
        "fetched_at": now_s,
        "source": "Yahoo Finance（yfinance 日 K 收盤價，沒有還原除權息）",
        "fx": fx_out,
        "codes": list(codes),
        "quotes": quotes,
        "stale": stale,
        "missing": missing,
    }


# ---------------------------------------------------------------- 對帳（--verify）
def verify(path: Path) -> int:
    """最新一份月對帳單的月底庫存 vs 同一天的 Yahoo 收盤價：收盤價、美元市值、台幣市值（不寫檔）"""
    import yfinance as yf
    if not path.exists():
        print(f"ℹ️ 沒有月對帳單庫存檔（{path.name}），沒辦法對帳")
        return 0
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if str(r.get("as_of") or "").strip()]
    if not rows:
        print("ℹ️ 月對帳單庫存檔是空的")
        return 0
    as_of = max(r["as_of"] for r in rows)
    rows = [r for r in rows if r["as_of"] == as_of]
    d = date.fromisoformat(as_of)
    their_fx = _num(rows[0].get("fx"))
    now_ny = datetime.now(timezone.utc).astimezone(NY)
    print(f"=== 對帳：國泰 {as_of} 月對帳單 vs Yahoo 同一天收盤價（{len(rows)} 檔）===")
    t_usd = m_usd = t_twd = 0.0
    bad, days = [], set()
    for r in sorted(rows, key=lambda x: x["code"]):
        code, sh = r["code"], _num(r.get("shares")) or 0.0
        c_t, v_t, w_t = _num(r.get("close")), _num(r.get("value_usd")) or 0.0, _num(r.get("value_twd")) or 0.0
        bars = []
        for attempt in range(RETRIES):
            try:
                h = yf.Ticker(yahoo_symbol(code)).history(start=(d - timedelta(days=10)).isoformat(),
                                                          end=(d + timedelta(days=1)).isoformat(),
                                                          auto_adjust=False, timeout=YF_TIMEOUT)
                bars = [(x, v) for x, v in completed_closes(h, now_ny) if x <= d]
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ {code} 重試 {attempt + 1}/{RETRIES}：{type(e).__name__}: {str(e)[:100]}")
            if bars:
                break
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_SLEEP)
        if not bars or c_t is None:
            print(f"  ❌ {code}：Yahoo 抓不到 {as_of} 以前的收盤價（或對帳單沒有收盤價）")
            bad.append(code)
            continue
        bd, c_y = bars[-1]
        days.add(bd)
        v_y = sh * c_y
        ok = abs(c_y - c_t) <= 0.006            # 對帳單收盤價只到小數 2 位，Yahoo 有到 4 位
        if not ok:
            bad.append(code)
        t_usd, m_usd, t_twd = t_usd + v_t, m_usd + v_y, t_twd + w_t
        print(f"  {'✅' if ok else '⚠️'} {code:<6} {sh:>8g} 股｜對帳單收盤 {c_t:>10,.4f}｜Yahoo {bd:%m/%d} {c_y:>10,.4f}"
              f"｜差 {_z(c_y - c_t, 4):+.4f}｜美元市值 對帳單 {v_t:>10,.2f}／Yahoo {v_y:>10,.2f}（差 {_z(v_y - v_t, 2):+,.2f}）")
    print("-" * 100)
    print(f"Yahoo 用的收盤日：{'、'.join(sorted(x.isoformat() for x in days)) or '—'}"
          f"（對帳單日期 {as_of}{'，週末就是前一個交易日' if d.weekday() >= 5 else ''}）")
    print(f"美元市值合計：對帳單 {t_usd:,.2f}｜用 Yahoo 收盤價算 {m_usd:,.2f}｜差 {_z(m_usd - t_usd, 2):+,.2f}"
          f"（{_z((m_usd - t_usd) / t_usd * 100 if t_usd else 0, 3):+.3f}%）")
    if their_fx:
        print(f"台幣市值合計：對帳單 {t_twd:,.0f}（匯率 {their_fx}）｜Yahoo 收盤價 × 對帳單匯率 {m_usd * their_fx:,.0f}"
              f"（差 {_z(m_usd * their_fx - t_twd, 0):+,.0f}，只剩四捨五入）")
    fx_snap = None
    if days:
        # 快照的匯率算法＝台灣早上 06:30 抓時的最新一筆 ≈ 美東收盤那天 22:00（UTC）那根 1 小時線
        bd = max(days)
        try:
            h1 = yf.Ticker(FX_SYMBOL).history(start=bd.isoformat(), end=(bd + timedelta(days=2)).isoformat(),
                                              interval="1h", timeout=YF_TIMEOUT).dropna(subset=["Close"])
            cut = datetime(bd.year, bd.month, bd.day, 22, tzinfo=timezone.utc)
            sel = [float(v) for ts, v in h1["Close"].items() if ts.tz_convert("UTC").to_pydatetime() <= cut]
            fx_snap = sel[-1] if sel else None
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 匯率抓不到：{type(e).__name__}: {str(e)[:100]}")
    if fx_snap and their_fx:
        print(f"              Yahoo 收盤價 × 快照算法的匯率 {fx_snap:.4f}（{max(days):%m/%d} 美股收盤後最新一筆）"
              f"＝ {m_usd * fx_snap:,.0f}（差 {m_usd * fx_snap - t_twd:+,.0f}，"
              f"{(m_usd * fx_snap - t_twd) / t_twd * 100 if t_twd else 0:+.2f}%：匯率差 {fx_snap - their_fx:+.4f} 造成）")
    print(f"\n結果：{'✅ 收盤價全部對上' if not bad else '⚠️ 有對不上的：' + '、'.join(bad)}")
    return 0 if not bad else 1


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="美股收盤價快照（給戰情室「美股對帳單」頁算最新市值）")
    ap.add_argument("--force", action="store_true", help="不管是不是最新都重抓")
    ap.add_argument("--dry-run", action="store_true", help="只抓、逐檔印出來，不寫檔")
    ap.add_argument("--verify", action="store_true", help="跟最新一份月對帳單比同一天的收盤價、市值（不寫檔）")
    ap.add_argument("--statement", default=str(STATEMENT), help=argparse.SUPPRESS)   # 測試用：換交易檔
    ap.add_argument("--out", default=str(OUT), help=argparse.SUPPRESS)               # 測試用：換輸出檔
    ap.add_argument("--holdings", default=str(HOLDINGS), help=argparse.SUPPRESS)     # 測試用：換月底庫存檔
    a = ap.parse_args(argv)

    if a.verify:
        return verify(Path(a.holdings))
    st_path, out_path = Path(a.statement), Path(a.out)
    if not st_path.exists():
        print(f"ℹ️ 沒有美股交易檔（{st_path.name}），不用抓收盤價。")    # 排程試跑的沙盒裡就是這樣
        return 0
    try:
        codes, unknown = held_codes(st_path)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 美股交易檔讀不懂（{type(e).__name__}: {e}），舊的快照不動")
        return 1
    for k, n in unknown.items():
        print(f"ℹ️ 交易檔有 {n} 列沒看過的種類「{k or '空白'}」，算持股時先略過（國泰同步的核對會另外抓出來）")

    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(NY)
    prev = load_json(out_path)
    if not codes:
        if a.dry_run:
            print("ℹ️ 目前沒有持有美股（--dry-run：沒有寫檔）")
            return 0
        write_json(out_path, merge([], {}, {}, prev, now_utc))
        print("ℹ️ 目前沒有持有美股，快照清空。")
        return 0
    if not (a.force or a.dry_run):
        why = up_to_date(prev, codes, now_ny)
        if why:
            print(f"ℹ️ {why}，這次不用抓。")
            return 0

    t0 = time.time()
    fetched: dict = {}
    for i, c in enumerate(codes):
        if i >= GIVE_UP_AFTER and all("error" in fetched[x] for x in codes[:i]):
            fetched[c] = {"error": f"前 {i} 檔都抓不到，判斷 Yahoo 暫時連不上，這檔沒再試"}
            continue
        fetched[c] = fetch_close(yahoo_symbol(c), now_ny)
    n_ok = sum(1 for r in fetched.values() if "close" in r)
    if n_ok == 0:
        first = next(iter(fetched.values()), {}).get("error", "")
        print(f"❌ {len(codes)} 檔一檔都沒抓到收盤價（{first[:160]}）：舊的快照不動"
              + (f"（還是 {prev.get('as_of')} 收盤）" if prev.get("as_of") else ""))
        return 1
    fx = fetch_fx(now_utc)
    snap = merge(codes, fetched, fx, prev, now_utc)

    if a.dry_run:
        for c in codes:
            q = snap["quotes"].get(c)
            if not q:
                print(f"  ❌ {c:<6} 沒有價格：{fetched[c].get('error', '')[:120]}")
                continue
            pc = _num(q.get("prev_close"))
            chg = f"{(q['close'] / pc - 1) * 100:+.2f}%" if pc else "—"
            print(f"  {'⚠️' if q['stale'] else '✅'} {c:<6} {q['close']:>10,.4f}｜{q['date']}｜當日 {chg}"
                  + (f"｜沿用舊值（{q.get('error', '')[:80]}）" if q["stale"] else ""))
    for c in snap["stale"]:
        q = snap["quotes"][c]
        print(f"⚠️ {c} 這次沒抓到（{fetched[c].get('error', '')[:100]}），沿用 {q.get('date')} 的收盤價")
    for c in snap["missing"]:
        print(f"⚠️ {c} 抓不到、之前也沒有價格（{fetched[c].get('error', '')[:100]}），戰情室這檔先用成本算")
    f = snap["fx"] or {}
    fx_txt = (f"匯率 {f['rate']}（{str(f.get('at', ''))[5:16].replace('T', ' ')}"
              + ("，這次沒抓到、沿用上次" if f.get("stale") else "") + "）") if f else "匯率抓不到（戰情室改用月底匯率）"
    if not a.dry_run:
        write_json(out_path, snap)
    n_fresh = len(codes) - len(snap["stale"]) - len(snap["missing"])
    print(f"{'✅' if n_fresh == len(codes) and f and not f.get('stale') else '⚠️'} 美股收盤價快照："
          f"{n_fresh}/{len(codes)} 檔抓到、收盤日 {snap['as_of']}（美東）、{fx_txt}，{time.time() - t0:.0f} 秒"
          + ("（--dry-run：沒有寫檔）" if a.dry_run else f" → {out_path.name}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
