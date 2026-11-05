"""融資維持率＋恐慌指數監控（2026-11-05 新增）

每個交易日晚上（證交所融資資料約 21:30 後公布）跑一次：
  1. 用證交所官方資料自算「上市大盤融資維持率」
       維持率 = Σ(個股融資餘額張數 × 1000 × 收盤價) ÷ 融資金額餘額
  2. 抓台指 VIX（期交所）、美股 VIX（CBOE，yfinance）
  3. 寫入 data/margin_fear_history.json（晨報、TAIEX 追蹤器會讀最後一筆）
  4. 任一指標「進到更深一級」才推播（同一波不重複推），門檻見 data/margin_fear_policy.json

同一套算法的歷史大底：2020/3 135.8%、2022/10 139.1%、2024/8 148.2%、2025/4 130.3%

用法：
  python margin_fear_gauge.py                 # 雲端排程：抓最新交易日、判斷、推播
  python margin_fear_gauge.py --dry-run       # 只印出推播內容，不推、不存 state
  python margin_fear_gauge.py --quiet         # 抓＋存，不推播
  python margin_fear_gauge.py --date 20261105 # 指定日期（忽略「已處理過」檢查）
  python margin_fear_gauge.py --backfill 20261104,20261103   # 只補歷史，不碰 state
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 桌機 pythonw 排程沒 stdout；雲端 Linux 有，這段仍安全
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
HISTORY_FILE = BASE / "data" / "margin_fear_history.json"
STATE_FILE = BASE / "data" / "margin_fear_state.json"
POLICY_FILE = BASE / "data" / "margin_fear_policy.json"
TAIEX_STATE_FILE = BASE / "data" / "taiex_ma_state.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TWSE_SLEEP = 3.2   # 證交所有頻率限制，連續抓要慢
TW_TZ = timezone(timedelta(hours=8))
KEEP_DAYS = 500    # 歷史檔最多保留幾筆


# ---------------------------------------------------------------- 小工具
def _num(s) -> float | None:
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _md(iso: str) -> str:
    return f"{iso[5:7]}/{iso[8:10]}"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _session():
    # truststore 是桌機用（Windows 憑證），雲端 Linux 不用
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    import requests
    s = requests.Session()
    s.headers.update(UA)
    return s


def _get_json(sess, url: str) -> dict:
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 連線重試 {attempt + 1}/3：{e}")
            time.sleep(8)
    return {}


# ---------------------------------------------------------------- 資料抓取
def fetch_margin_day(sess, d: str) -> dict | None:
    """抓某日（YYYYMMDD）上市融資＋收盤價，算維持率。資料還沒公布 / 非交易日 → None"""
    m = _get_json(sess, "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
                        f"?date={d}&selectType=ALL&response=json")
    time.sleep(TWSE_SLEEP)
    if m.get("stat") != "OK" or str(m.get("date")) != d:
        return None
    summary = per_stock = None
    for t in m.get("tables", []):
        rows = t.get("data") or []
        if rows and str(rows[0][0]).startswith("融資(交易單位)"):
            summary = rows
        elif len(rows) > 100:
            per_stock = rows
    if summary is None or per_stock is None:
        print(f"  ⚠️ {d} 融資表格結構不符，跳過")
        return None

    p = _get_json(sess, "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                        f"?date={d}&type=ALLBUT0999&response=json")
    time.sleep(TWSE_SLEEP)
    if p.get("stat") != "OK":
        return None
    closes: dict[str, float] = {}
    taiex = taiex_pct = None
    for t in p.get("tables", []):
        f = t.get("fields") or []
        rows = t.get("data") or []
        if "證券代號" in f and "收盤價" in f:
            ci, pi = f.index("證券代號"), f.index("收盤價")
            bi = f.index("最後揭示買價") if "最後揭示買價" in f else None
            for r in rows:
                c = _num(r[pi])
                if c is None and bi is not None:   # 當天沒成交 → 用最後揭示買價
                    c = _num(r[bi])
                if c:
                    closes[str(r[ci]).strip()] = c
        for r in rows:
            if str(r[0]).strip() == "發行量加權股價指數":
                taiex, taiex_pct = _num(r[1]), _num(r[4])
                if taiex_pct is not None and ("-" in str(r[2]) or "green" in str(r[2])):
                    taiex_pct = -abs(taiex_pct)

    mv = lots_priced = lots_missing = 0.0
    for r in per_stock:
        bal = _num(r[6]) or 0          # 融資今日餘額（張）
        if bal <= 0:
            continue
        c = closes.get(str(r[0]).strip())
        if c is None:
            lots_missing += bal
            continue
        lots_priced += bal
        mv += bal * 1000 * c

    amt_k = _num(summary[2][5])        # 融資金額今日餘額（仟元）
    amt_prev_k = _num(summary[2][4])
    if not amt_k or lots_priced == 0:
        return None
    coverage = lots_priced / (lots_priced + lots_missing) * 100
    if coverage < 95:
        print(f"  ⚠️ {d} 只有 {coverage:.1f}% 的融資張數對到收盤價，維持率可能失真")
    return {
        "date": _iso(d),
        "taiex": taiex,
        "taiex_pct": taiex_pct,
        "maint_ratio": round(mv / (amt_k * 1000) * 100, 2),
        "margin_yi": round(amt_k / 1e5, 2),                       # 億元
        "margin_chg_yi": round((amt_k - (amt_prev_k or amt_k)) / 1e5, 2),
        "coverage_pct": round(coverage, 2),
    }


def fetch_tw_vix(sess, months: list[str]) -> dict[str, float]:
    """期交所臺指選擇權波動率指數每日收盤（只提供近 3 個月）→ {YYYY-MM-DD: 值}"""
    out: dict[str, float] = {}
    for ym in months:
        try:
            r = sess.get("https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/"
                         f"{ym}new.txt", timeout=30)
            if r.status_code != 200 or "404" in r.url:
                continue
            for line in r.content.decode("big5", errors="ignore").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0].isdigit() and len(parts[0]) == 8:
                    v = _num(parts[2])
                    if v:
                        out[_iso(parts[0])] = v
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 台指 VIX {ym} 抓取失敗：{e}")
    return out


def fetch_us_vix() -> dict[str, float]:
    """CBOE VIX 已收盤的日線 → {YYYY-MM-DD: 收盤}（盤中那根不算）"""
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="3mo", auto_adjust=False).dropna(subset=["Close"])
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 美股 VIX 抓取失敗：{e}")
        return {}
    out = {idx.strftime("%Y-%m-%d"): round(float(v), 2) for idx, v in h["Close"].items()}
    # 美東還沒收盤 → 最後一根是盤中值，拿掉
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now(timezone(timedelta(hours=-5)))
    today_et = now_et.strftime("%Y-%m-%d")
    if today_et in out and (now_et.hour, now_et.minute) < (16, 30):
        out.pop(today_et)
    return out


def _months_for(iso_dates: list[str]) -> list[str]:
    ms = {d[:4] + d[5:7] for d in iso_dates}
    return sorted(ms)


def attach_vix(records: list[dict], sess) -> None:
    """把台指 VIX（同日）與美股 VIX（該日以前最近一次收盤）補進 records"""
    if not records:
        return
    dates = [r["date"] for r in records]
    first = min(dates)
    prev_month = (datetime.strptime(first[:7] + "-01", "%Y-%m-%d") - timedelta(days=1))
    tw = fetch_tw_vix(sess, _months_for(dates + [prev_month.strftime("%Y-%m-%d")]))
    us = fetch_us_vix()
    us_dates = sorted(us)
    for r in records:
        r["tw_vix"] = tw.get(r["date"])
        # 台灣某日晚上看到的是「前一個美國交易日」的收盤（同日美股要台灣隔天早上才收）
        past = [d for d in us_dates if d < r["date"]]
        if past:
            r["us_vix"], r["us_vix_date"] = us[past[-1]], past[-1]
        else:
            r["us_vix"], r["us_vix_date"] = None, None


# ---------------------------------------------------------------- 歷史 / 等級
def load_history() -> list[dict]:
    return _read_json(HISTORY_FILE, [])


def merge_history(new: list[dict]) -> list[dict]:
    hist = {r["date"]: r for r in load_history()}
    for r in new:
        hist[r["date"]] = {**hist.get(r["date"], {}), **{k: v for k, v in r.items() if v is not None}}
    merged = [hist[d] for d in sorted(hist)][-KEEP_DAYS:]
    _write_json(HISTORY_FILE, merged)
    return merged


def _load_policy() -> dict:
    return _read_json(POLICY_FILE, {})


def zone(kind: str, value: float | None, policy: dict | None = None) -> tuple[int, str]:
    """回傳 (等級 index, 標籤)；-1 = 正常。kind: maint / tw_vix / us_vix"""
    if value is None:
        return -1, ""
    policy = policy or _load_policy()
    key = {"maint": "maint_ratio_zones", "tw_vix": "tw_vix_zones", "us_vix": "us_vix_zones"}[kind]
    idx, label = -1, ""
    for i, z in enumerate(policy.get(key, [])):
        hit = value < z["below"] if "below" in z else value >= z["above"]
        if hit:
            idx, label = i, z["label"]
    return idx, label


def _recovered(kind: str, value: float | None, policy: dict) -> bool:
    if value is None:
        return False
    if kind == "maint":
        return value >= policy.get("maint_ratio_reset_above", 166.7)
    return value < policy.get(f"{kind}_reset_below", 0)


def summary_line(rec: dict | None = None) -> str:
    """給晨報 / TAIEX 追蹤器用的一行摘要（只讀檔，不連網）"""
    if rec is None:
        hist = load_history()
        rec = hist[-1] if hist else None
    if not rec or rec.get("maint_ratio") is None:
        return ""
    pol = _load_policy()
    parts = [f"📉 融資維持率 {rec['maint_ratio']:.1f}%{zone('maint', rec['maint_ratio'], pol)[1][:1]}"
             f"（{_md(rec['date'])}）"]
    if rec.get("tw_vix") is not None:
        parts.append(f"台指VIX {rec['tw_vix']:.1f}{zone('tw_vix', rec['tw_vix'], pol)[1][:1]}")
    if rec.get("us_vix") is not None:
        parts.append(f"美VIX {rec['us_vix']:.1f}{zone('us_vix', rec['us_vix'], pol)[1][:1]}")
    return "｜".join(parts)


def _ago(hist: list[dict], date: str, n: int) -> dict | None:
    earlier = [r for r in hist if r["date"] < date and r.get("maint_ratio") is not None]
    return earlier[-n] if len(earlier) >= n else None


def build_message(rec: dict, hist: list[dict], escalated: list[str], policy: dict) -> tuple[str, str]:
    names = {"maint": "融資維持率", "tw_vix": "台指VIX", "us_vix": "美股VIX"}
    vals = {"maint": rec.get("maint_ratio"), "tw_vix": rec.get("tw_vix"), "us_vix": rec.get("us_vix")}
    # 標題用升級指標中最嚴重的那個
    worst = max(escalated, key=lambda k: zone(k, vals[k], policy)[0])
    w_label = zone(worst, vals[worst], policy)[1]
    unit = "%" if worst == "maint" else ""
    title = f"{w_label[:1]} {names[worst]} {vals[worst]:.1f}{unit} 進入「{w_label[2:]}」"

    lines = []
    mz = zone("maint", vals["maint"], policy)[1] or "⚪ 正常"
    lines.append(f"📉 上市融資維持率 {vals['maint']:.1f}%（{_md(rec['date'])}）{mz}")
    lines.append(f"• 融資餘額 {rec['margin_yi']:,.0f} 億（當日 {rec['margin_chg_yi']:+,.0f} 億）")
    a5, a20 = _ago(hist, rec["date"], 5), _ago(hist, rec["date"], 20)
    if a5 or a20:
        seg = []
        if a5:
            seg.append(f"5 日前 {a5['maint_ratio']:.1f}%")
        if a20:
            seg.append(f"20 日前 {a20['maint_ratio']:.1f}%")
        lines.append("• " + "、".join(seg))
    if vals["tw_vix"] is not None:
        lines.append(f"😱 台指 VIX {vals['tw_vix']:.2f} {zone('tw_vix', vals['tw_vix'], policy)[1] or '⚪ 正常'}")
    if vals["us_vix"] is not None:
        lines.append(f"🇺🇸 美股 VIX {vals['us_vix']:.2f}（{_md(rec['us_vix_date'])}）"
                     f"{zone('us_vix', vals['us_vix'], policy)[1] or '⚪ 正常'}")
    if rec.get("taiex"):
        lines.append(f"📊 加權指數 {rec['taiex']:,.0f}（{(rec.get('taiex_pct') or 0):+.2f}%）")

    lines.append("")
    lines.append(f"📚 同算法歷史大底：{policy.get('history_refs', '')}")
    tx = _read_json(TAIEX_STATE_FILE, {})
    if tx.get("dist_peak_pct") is not None:
        lv = tx.get("bear_alert_level") or 0
        lv_txt = f"已觸發 {lv}% 級" if lv else "未觸發"
        lines.append(f"🎯 空頭加碼梯：距高點 {tx['dist_peak_pct']:+.1f}%（{lv_txt}，資料 {_md(tx.get('date', '----------'))}）")
    lines.append("ℹ️ 指標只是歷史對照，不保證見底")
    return title, "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def latest_available(sess, start: datetime, max_back: int = 6) -> dict | None:
    for back in range(max_back):
        d = (start - timedelta(days=back))
        if d.weekday() >= 5:
            continue
        rec = fetch_margin_day(sess, d.strftime("%Y%m%d"))
        if rec:
            return rec
        print(f"  {d:%Y-%m-%d} 證交所尚無融資資料（未公布或休市）")
    return None


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    quiet = "--quiet" in args
    sess = _session()

    # 只補歷史
    if "--backfill" in args:
        dates = args[args.index("--backfill") + 1].split(",")
        recs = []
        for d in dates:
            r = fetch_margin_day(sess, d.strip())
            print(f"  {d}: {r['maint_ratio'] if r else '無資料'}")
            if r:
                recs.append(r)
        attach_vix(recs, sess)
        merge_history(recs)
        print(f"✅ 補進 {len(recs)} 筆 → {HISTORY_FILE.name}")
        return

    state = _read_json(STATE_FILE, {})
    if "--date" in args:
        d = args[args.index("--date") + 1]
        rec = fetch_margin_day(sess, d)
        if not rec:
            print(f"❌ {d} 沒有資料")
            return
    else:
        rec = latest_available(sess, datetime.now(TW_TZ))
        if not rec:
            print("❌ 近幾日都抓不到證交所融資資料")
            return
        if state.get("last_date") == rec["date"]:
            print(f"ℹ️ {rec['date']} 已處理過，跳過")
            return

    attach_vix([rec], sess)
    hist = load_history() if dry else merge_history([rec])
    policy = _load_policy()
    print(f"=== {rec['date']} ===")
    print(f"  上市融資維持率 {rec['maint_ratio']}%｜融資 {rec['margin_yi']} 億（{rec['margin_chg_yi']:+} 億）"
          f"｜台指VIX {rec.get('tw_vix')}｜美VIX {rec.get('us_vix')}（{rec.get('us_vix_date')}）")

    # 等級判斷：進到更深一級才推；恢復到 reset 門檻外才歸零（單向棘輪，同一波不重複推）
    zones_prev = state.get("zones", {})
    zones_new, escalated = {}, []
    vals = {"maint": rec.get("maint_ratio"), "tw_vix": rec.get("tw_vix"), "us_vix": rec.get("us_vix")}
    for k, v in vals.items():
        prev_idx = zones_prev.get(k, -1)
        cur_idx, _ = zone(k, v, policy)
        if v is None:
            zones_new[k] = prev_idx
        elif cur_idx > prev_idx:
            zones_new[k] = cur_idx
            escalated.append(k)
        elif _recovered(k, v, policy):
            zones_new[k] = -1
        else:
            zones_new[k] = prev_idx
    print(f"  等級 {zones_prev} → {zones_new}；升級：{escalated or '無'}")

    if escalated:
        title, body = build_message(rec, hist, escalated, policy)
        print("\n----- 推播內容 -----\n" + title + "\n" + body + "\n--------------------")
        if not dry and not quiet:
            import telegram_notify
            telegram_notify.send(title, body)
            print("✅ 已推播")

    if not dry:
        _write_json(STATE_FILE, {
            "last_date": rec["date"],
            "zones": zones_new,
            "latest": rec,
            "updated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        })


if __name__ == "__main__":
    main()
