"""抄底雷達（見底計分卡）— 2026-11-20 新增

把「台股抄底指標資料包」的 8 項見底條件改成每天用官方資料自動計算：
  data/bottom_daily.json      逐日原始指標（雲端每晚補一天）
  data/bottom_scorecard.json  最新計分（儀表板「抄底雷達」、晨報、空頭加碼推播都讀這個）
分數往上跨過 2／4／6 分才推播（同一波不重複推）。規則與門檻：data/bottom_policy.json（記事本可改）。

8 個核心指標（完全符合 1 分、初步符合 0.5 分）：
  1 融資維持率        自家算法（margin_fear_gauge 每晚寫入，上市、證交所官方資料）
  2 融資斷頭潮        融資餘額單日減幅（追繳／斷頭戶數沒有官方即時資料，用這個代替）
  3 外資台指期淨空單  期交所三大法人（臺股期貨）
  4 小台散戶多空比    期交所：−三大法人小台淨未平倉 ÷ 小台全市場未沖銷量
  5 P/C 未平倉比      期交所
  6 新台幣            yfinance TWD=X（美元兌台幣）
  7 上市跌停家數      證交所 MI_INDEX「漲跌證券數合計」
  8 外資現貨買賣超    證交所 BFI82U
參考、不計分：台指 VIX、美股 VIX

用法：
  python bottom_scorecard.py               # 雲端排程：補最近資料 → 計分 → 跨門檻才推播
  python bottom_scorecard.py --quiet       # 補資料＋計分＋存檔，不推播
  python bottom_scorecard.py --dry-run     # 補資料＋計分，只印不存不推
  python bottom_scorecard.py --backfill 2024-06-03:2024-09-30,2025-02-03:2025-05-30
                                           # 回測用：補歷史到 data_export/（不推播、不動正式檔）
  python bottom_scorecard.py --replay      # 回測：逐日重算分數 → data/bottom_backtest_summary.json
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import margin_fear_gauge as mfg  # noqa: E402  共用連線、證交所抓取、融資維持率

DATA = BASE / "data"
DAILY_FILE = DATA / "bottom_daily.json"
CARD_FILE = DATA / "bottom_scorecard.json"
POLICY_FILE = DATA / "bottom_policy.json"
BEAR_POLICY_FILE = DATA / "bear_trigger_policy.json"
TAIEX_STATE_FILE = DATA / "taiex_ma_state.json"
BACKTEST_SUMMARY_FILE = DATA / "bottom_backtest_summary.json"
BACKTEST_DAILY_FILE = Path(os.environ.get("BOTTOM_BT_FILE") or BASE / "data_export" / "bottom_backtest_daily.json")
# 回測時可以分段平行補（每段寫自己的檔），replay 會把 data_export/bottom_backtest_daily*.json 全部合併

TW_TZ = timezone(timedelta(hours=8))
KEEP_DAYS = 180
LIT_SCORE = {"yes": 1.0, "partial": 0.5, "no": 0.0, "na": 0.0}
LIT_MARK = {"yes": "●", "partial": "◐", "no": "○", "na": "·"}
TAIFEX = "https://www.taifex.com.tw/cht/3/"

# 回測對照的歷史大底（收盤低點日；2026 年 7 月由資料自動找）
BOTTOMS = [
    {"id": "2024_jpy_carry", "label": "2024 日圓套利平倉", "window": ("2024-06-03", "2024-09-30"),
     "bottom_search": ("2024-07-20", "2024-08-20")},
    {"id": "2025_tariff", "label": "2025 對等關稅", "window": ("2025-02-03", "2025-05-30"),
     "bottom_search": ("2025-03-25", "2025-04-25")},
    {"id": "2026_jul", "label": "2026 7 月股災", "window": ("2026-05-18", "2026-09-30"),
     "bottom_search": ("2026-06-20", "2026-08-31")},
]


# ---------------------------------------------------------------- 小工具
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _slash_iso(s: str) -> str:
    y, m, d = s.strip().split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _md(iso: str | None) -> str:
    return f"{iso[5:7]}/{iso[8:10]}" if iso else "--/--"


def _pct_txt(x) -> str:
    return "—" if x is None else f"{x:+.1f}%"


def load_policy() -> dict:
    return _read_json(POLICY_FILE, {})


def merge(daily: dict, d: str, fields: dict) -> None:
    rec = daily.setdefault(d, {})
    rec.update({k: v for k, v in fields.items() if v is not None})


# ---------------------------------------------------------------- 資料抓取
def _taifex_table(sess, endpoint: str, form: dict) -> list[dict]:
    """期交所 CSV 下載 → list[dict]（用表頭對欄位，不寫死欄位順序）。查無資料回 []"""
    for attempt in range(3):
        try:
            r = sess.post(TAIFEX + endpoint, data=form, timeout=60)
            r.raise_for_status()
            txt = r.content.decode("cp950", errors="replace")
            rows = [row for row in csv.reader(io.StringIO(txt)) if row]
            if not rows or len(rows[0]) < 5:
                return []          # 查無資料（假日／區間外）期交所會回 HTML 訊息
            head = [h.strip() for h in rows[0]]
            return [dict(zip(head, [c.strip() for c in row])) for row in rows[1:]]
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 期交所 {endpoint} 重試 {attempt + 1}/3：{e}")
            time.sleep(6)
    return []


def _taifex_range(sess, endpoint: str, extra: dict, x: date, y: date) -> list[dict]:
    """區間結尾若是『今天、資料還沒公布』，期交所整段回空 → 結尾往前退一天再試"""
    recent = datetime.now(TW_TZ).date() - timedelta(days=4)
    for back in range(5):
        yy = y - timedelta(days=back)
        if yy < x:
            break
        rows = _taifex_table(sess, endpoint, {"queryStartDate": x.strftime("%Y/%m/%d"),
                                              "queryEndDate": yy.strftime("%Y/%m/%d"), **extra})
        if rows or yy < recent:
            return rows
    return []


def _chunks(a: date, b: date, days: int = 28):
    cur = a
    while cur <= b:
        end = min(b, cur + timedelta(days=days - 1))
        yield cur, end
        cur = end + timedelta(days=1)


def fetch_taifex(sess, a: date, b: date) -> dict[str, dict]:
    """外資台指期淨未平倉、三大法人小台淨未平倉、小台未沖銷量（→散戶多空比）、P/C 未平倉比"""
    out: dict[str, dict] = {}
    num = mfg._num

    def _placeholder(r: dict) -> bool:
        # 11/20 實測：當天資料還沒公布前，期交所會先回一列多空未平倉都是 0 的空殼，不能當成真的 0 口
        return not num(r.get("多方未平倉口數")) and not num(r.get("空方未平倉口數"))

    for x, y in _chunks(a, b):
        for r in _taifex_range(sess, "futContractsDateDown", {"commodityId": "TXF"}, x, y):
            if str(r.get("身份別", "")).startswith("外資") and not _placeholder(r):
                v = num(r.get("多空未平倉口數淨額"))
                if v is not None:
                    merge(out, _slash_iso(r["日期"]), {"fut_foreign_net": int(v)})
        inst: dict[str, float] = {}
        for r in _taifex_range(sess, "futContractsDateDown", {"commodityId": "MXF"}, x, y):
            v = num(r.get("多空未平倉口數淨額"))
            if v is not None and r.get("日期", "")[:1].isdigit() and not _placeholder(r):
                d = _slash_iso(r["日期"])
                inst[d] = inst.get(d, 0) + v
        oi: dict[str, float] = {}
        for r in _taifex_range(sess, "futDataDown", {"down_type": "1", "commodity_id": "MTX"}, x, y):
            # 只算一般時段、單一月份（價差單「202611/202612」不算）
            if (r.get("契約") == "MTX" and "/" not in r.get("到期月份(週別)", "/")
                    and r.get("交易時段", "一般") == "一般"):
                v = num(r.get("未沖銷契約數"))
                if v is not None:
                    d = _slash_iso(r["交易日期"])
                    oi[d] = oi.get(d, 0) + v
        for d, net in inst.items():
            merge(out, d, {"mxf_inst_net": int(net)})
            if oi.get(d):
                merge(out, d, {"mtx_oi": int(oi[d]), "retail_ratio": round(-net / oi[d] * 100, 2)})
        for r in _taifex_range(sess, "pcRatioDown", {}, x, y):
            v = num(r.get("買賣權未平倉量比率%"))
            if v is not None and r.get("日期", "")[:1].isdigit():
                merge(out, _slash_iso(r["日期"]), {"pc_oi_ratio": v})
        time.sleep(1.5)
    return out


def fetch_foreign_spot(sess, ymd: str) -> float | None:
    """證交所 BFI82U：外資及陸資（不含外資自營商）上市買賣超，億元"""
    j = mfg._get_json(sess, "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
                            f"?type=day&dayDate={ymd}&response=json")
    time.sleep(mfg.TWSE_SLEEP)
    if j.get("stat") != "OK" or str(j.get("date")) != ymd:
        return None
    for r in j.get("data") or []:
        if str(r[0]).startswith("外資及陸資"):
            v = mfg._num(r[3])
            return round(v / 1e8, 2) if v is not None else None
    return None


def fetch_breadth(sess, ymd: str) -> dict:
    """證交所 MI_INDEX（輕量版 MS）→ 上市股票漲停／跌停家數"""
    j = mfg._get_json(sess, "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                            f"?date={ymd}&type=MS&response=json")
    time.sleep(mfg.TWSE_SLEEP)
    if j.get("stat") != "OK":
        return {}
    return mfg.parse_breadth(j)


def fetch_yf(symbol: str, a: date, b: date) -> dict[str, float]:
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(start=a.isoformat(), end=(b + timedelta(days=1)).isoformat(),
                                      auto_adjust=False).dropna(subset=["Close"])
        return {i.strftime("%Y-%m-%d"): round(float(v), 4) for i, v in h["Close"].items()}
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ yfinance {symbol} 抓取失敗：{e}")
        return {}


def fetch_usdtwd_16(a: date, b: date, official: bool = False) -> dict[str, float]:
    """美元兌台幣『台北 16:00』匯率 → {YYYY-MM-DD: 匯率}

    注意：yfinance 的 TWD=X『日線收盤』其實是 00:00 UTC（台北早上 8 點、開盤前）的報價，
    拿來判斷「台幣連續升值」會晚一天、方向還可能顛倒（11/20 審查時實測證實），所以不用日線：
      - official=True：央行 BP01D01（台灣時間 16:00 銀行間即期匯率，官方；但資料會晚好幾週）→ 回測用
      - 其餘日期：yfinance 1 小時線 07:00–08:00 UTC 那根的收盤（= 台北 16:00），只有近 730 天
    """
    out: dict[str, float] = {}
    if official:
        try:
            j = mfg._session().get("https://cpx.cbc.gov.tw/API/DataAPI/Get?FileName=BP01D01", timeout=60).json()
            for r in j["data"]["dataSets"]:
                d = f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:8]}"
                v = mfg._num(r[1])
                if v and a.isoformat() <= d <= b.isoformat():
                    out[d] = v
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 央行匯率抓取失敗：{e}")
    start = max(a, datetime.now(TW_TZ).date() - timedelta(days=720))
    if out:
        start = max(start, _d(max(out)) + timedelta(days=1))
    if start <= b:
        try:
            import yfinance as yf
            h = yf.Ticker("TWD=X").history(start=start.isoformat(), end=(b + timedelta(days=1)).isoformat(),
                                           interval="1h").dropna(subset=["Close"])
            idx = h.index.tz_convert("UTC") if h.index.tz is not None else h.index.tz_localize("UTC")
            now_utc = datetime.now(timezone.utc)
            for ts, v in zip(idx, h["Close"]):
                # 07:00–08:00 UTC 這根收在台北 16:00；還沒收完（下午 3 點多跑）就不收，免得拿盤中價計分
                if ts.hour == 7 and ts.to_pydatetime() + timedelta(hours=1) <= now_utc:
                    out.setdefault(ts.strftime("%Y-%m-%d"), round(float(v), 4))
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ yfinance TWD=X 1 小時線抓取失敗：{e}")
    return out


def _attach_us_vix(daily: dict, us: dict[str, float]) -> None:
    """台灣某日晚上看到的是前一個美國交易日的收盤"""
    us_dates = sorted(us)
    for d, rec in daily.items():
        if rec.get("us_vix") is not None:
            continue
        past = [x for x in us_dates if x < d]
        if past:
            rec["us_vix"], rec["us_vix_date"] = us[past[-1]], past[-1]


# ---------------------------------------------------------------- 每日更新（正式）
def update_daily(sess, daily: dict, start: date, end: date, twse_days: int = 12) -> None:
    s_iso, e_iso = start.isoformat(), end.isoformat()
    # 0) 清掉之前存進來的期交所空殼（外資、法人小台淨未平倉都剛好是 0 = 當天還沒公布）
    for rec in daily.values():
        if rec.get("fut_foreign_net") == 0 and rec.get("mxf_inst_net") in (0, None):
            for k in ("fut_foreign_net", "mxf_inst_net", "mtx_oi", "retail_ratio"):
                rec.pop(k, None)
    # 1) 融資維持率／融資增減／跌停家數／VIX ← margin_fear_history（官方自算）
    for r in mfg.load_history():
        if s_iso <= r["date"] <= e_iso:
            merge(daily, r["date"], {k: r.get(k) for k in (
                "taiex", "taiex_pct", "maint_ratio", "margin_yi", "margin_chg_yi",
                "limit_down", "limit_up", "down_count", "up_count", "tw_vix", "us_vix", "us_vix_date")})
    # 2) 期交所（區間下載；已有的只往回補一週）
    have = sorted(d for d in daily if daily[d].get("fut_foreign_net") is not None)
    t_start = max(start, _d(have[-1]) - timedelta(days=7)) if have else start
    print(f"  期交所 {t_start} ～ {end}")
    for d, f in fetch_taifex(sess, t_start, end).items():
        merge(daily, d, f)
    # 3) 證交所逐日（外資現貨、跌停家數）：只補缺的、最近 twse_days 個交易日
    trading = sorted(d for d in daily if s_iso <= d <= e_iso
                     and (daily[d].get("fut_foreign_net") is not None or daily[d].get("maint_ratio") is not None))
    for d in trading[-twse_days:]:
        ymd = d.replace("-", "")
        if daily[d].get("foreign_spot_yi") is None:
            v = fetch_foreign_spot(sess, ymd)
            if v is not None:
                merge(daily, d, {"foreign_spot_yi": v})
        if daily[d].get("limit_down") is None:
            merge(daily, d, fetch_breadth(sess, ymd))
    # 4) 新台幣（台北 16:00，見 fetch_usdtwd_16）、加權指數（官方收盤沒有時才用 yfinance）
    trading_set = set(trading)
    for d, v in fetch_usdtwd_16(start, end).items():
        if d in trading_set:
            merge(daily, d, {"usdtwd": v})
    tw = fetch_yf("^TWII", start, end)
    for d in trading:
        if daily[d].get("taiex") is None and d in tw:
            merge(daily, d, {"taiex": tw[d]})


def trim(daily: dict, keep: int = KEEP_DAYS) -> dict:
    return {d: daily[d] for d in sorted(daily)[-keep:]}


# ---------------------------------------------------------------- 規則
def _ser(daily: dict, key: str, upto: str, n: int | None = None) -> list[tuple[str, float]]:
    xs = [(d, daily[d][key]) for d in sorted(daily) if d <= upto and daily[d].get(key) is not None]
    return xs[-n:] if n else xs


def _streak(s: list[tuple[str, float]], falling: bool) -> int:
    """結尾連續下跌（falling=True）或上漲的天數"""
    n = 0
    for i in range(len(s) - 1, 0, -1):
        if (s[i][1] < s[i - 1][1]) if falling else (s[i][1] > s[i - 1][1]):
            n += 1
        else:
            break
    return n


def r_maint(daily, upto, p):
    s = _ser(daily, "maint_ratio", upto, 60)
    if not s:
        return None
    d, v = s[-1]
    lb = int(p.get("lookback_days", 3))
    yes_b, part_b = p.get("yes_below", 140), p.get("partial_below", 150)
    low = min(x for _, x in s[-lb:])
    lit = "yes" if low <= yes_b else "partial" if low < part_b else "no"
    wl_d, wl = min(s, key=lambda t: t[1])
    return {"value": f"{v:.1f}%", "as_of": d, "lit": lit,
            "rule": f"近 {lb} 日最低 ≤{yes_b:g}% 完全符合、<{part_b:g}% 初步（自家算法歷史大底 130～148%）",
            "detail": f"近 {len(s)} 個交易日最低 {wl:.1f}%（{_md(wl_d)}）"}


def r_forced(daily, upto, p):
    w = int(p.get("window_days", 10))
    rows = []
    for d in sorted(daily):
        r = daily[d]
        if d <= upto and r.get("margin_chg_yi") is not None and r.get("margin_yi"):
            prev = r["margin_yi"] - r["margin_chg_yi"]
            if prev > 0:
                rows.append((d, r["margin_chg_yi"] / prev * 100))
    s = rows[-w:]
    if len(s) < 3:
        return None
    d, v = s[-1]
    cap, ratio = p.get("capitulation_day_pct", -2.5), p.get("exhaust_ratio", 0.5)
    caps = [(dd, x) for dd, x in s if x <= cap]
    pk_d, pk = min(s, key=lambda t: t[1])
    if not caps:
        lit, det = "no", f"近 {len(s)} 日最大減幅 {pk:+.1f}%（{_md(pk_d)}），還沒出現斷頭潮"
    elif caps[-1][0] == d:
        lit, det = "no", (f"今天又是斷頭日（近 {len(s)} 日有 {len(caps)} 天 ≤{cap:g}%，"
                          f"最大 {_md(pk_d)} {pk:+.1f}%），還在出清")
    else:
        last_d, last_x = caps[-1]
        after = [x for dd, x in s if dd > last_d]
        if len(after) >= 2 and all(x > last_x * ratio for x in after):
            lit, det = "yes", f"最後斷頭日 {_md(last_d)}（{last_x:+.1f}%）後，連 {len(after)} 日減幅都不到一半"
        else:
            lit, det = "partial", f"最後斷頭日 {_md(last_d)}（{last_x:+.1f}%）已過，等連 2 日收斂"
    chg = daily[d]["margin_chg_yi"]
    return {"value": f"{v:+.2f}%（{chg:+,.0f} 億）", "as_of": d, "lit": lit,
            "rule": f"出現 ≤{cap:g}% 斷頭日後，之後連 2 日減幅都不到最後斷頭日的一半",
            "detail": det}


def r_fut(daily, upto, p):
    s = _ser(daily, "fut_foreign_net", upto, int(p.get("window_days", 60)))
    if not s:
        return None
    d, v = s[-1]
    pk_d, pk = min(s, key=lambda t: t[1])
    covered = v - pk
    min_s = p.get("min_short", 20000)
    # 回補門檻跟著空單規模走（資料包原本固定 2 萬口；本波最大 10.4 萬口 × 2 成 ≈ 2.1 萬口，等於一樣）
    yes_r, part_r = p.get("yes_cover_ratio", 0.2), p.get("partial_cover_ratio", 0.1)
    yes_c, part_c = abs(pk) * yes_r, abs(pk) * part_r
    if pk > -min_s:
        lit, det = "no", f"近 {len(s)} 日最大淨空單 {pk:,.0f} 口，沒有重壓空（門檻 {min_s:,} 口）"
    else:
        lit = "yes" if covered >= yes_c else "partial" if covered >= part_c else "no"
        det = (f"本波最大淨空 {pk:,.0f} 口（{_md(pk_d)}），已回補 {covered:,.0f} 口"
               f"（完全符合要 {yes_c:,.0f} 口）")
    return {"value": f"{v:,.0f} 口", "as_of": d, "lit": lit,
            "rule": f"自本波最大淨空單回補 ≥{yes_r:.0%} 完全符合、≥{part_r:.0%} 初步", "detail": det}


def r_retail(daily, upto, p):
    s = _ser(daily, "retail_ratio", upto, 10)
    if not s:
        return None
    d, v = s[-1]
    y, pt = p.get("yes_at_or_below", 0), p.get("partial_at_or_below", 5)
    lit = "yes" if v <= y else "partial" if v <= pt else "no"
    det = f"前一日 {s[-2][1]:+.2f}%" if len(s) >= 2 else ""
    hi_d, hi = max(s, key=lambda t: t[1])
    if hi_d != d:
        det += f"；近 {len(s)} 日最高 {hi:+.2f}%（{_md(hi_d)}）"
    return {"value": f"{v:+.2f}%", "as_of": d, "lit": lit,
            "rule": f"≤{y:g}% 完全符合（散戶轉偏空、多單被洗出）、≤{pt:g}% 初步", "detail": det.strip("；")}


def r_pc(daily, upto, p):
    s = _ser(daily, "pc_oi_ratio", upto, int(p.get("window_days", 20)))
    if not s:
        return None
    d, v = s[-1]
    lo_d, lo = min(s, key=lambda t: t[1])
    y, wash, reb = p.get("yes_at_or_above", 80), p.get("washout_at_or_below", 60), p.get("partial_rebound_pts", 15)
    washed = lo <= wash
    if washed and v >= y:
        lit = "yes"
    elif washed and v - lo >= reb:
        lit = "partial"
    else:
        lit = "no"
    det = f"近 {len(s)} 日低點 {lo:.1f}%（{_md(lo_d)}）" + ("" if washed else f"，還沒被殺到 {wash:g}% 以下")
    return {"value": f"{v:.1f}%", "as_of": d, "lit": lit,
            "rule": f"先跌到 ≤{wash:g}%、再回到 ≥{y:g}% 完全符合；自低點回升 ≥{reb:g} 點初步", "detail": det}


def r_twd(daily, upto, p):
    s = _ser(daily, "usdtwd", upto, 25)
    if len(s) < 4:
        return None
    d, v = s[-1]
    up = _streak(s, falling=True)        # 美元兌台幣下跌 = 台幣升值
    y, pt = int(p.get("yes_up_days", 3)), int(p.get("partial_up_days", 2))
    lit = "yes" if up >= y else "partial" if up >= pt else "no"
    hi_d, hi = max(s[-20:], key=lambda t: t[1])
    return {"value": f"{v:.3f}", "as_of": d, "lit": lit,
            "rule": f"台幣連 {y} 天升值完全符合、連 {pt} 天初步（數字是 1 美元兌台幣）",
            "detail": f"連續升值 {up} 天；近 20 日最弱 {hi:.3f}（{_md(hi_d)}）"}


def r_limit(daily, upto, p):
    s = _ser(daily, "limit_down", upto, int(p.get("window_days", 10)))
    if not s:
        return None
    d, v = s[-1]
    pk_d, pk = max(s, key=lambda t: t[1])
    panic, y, ratio = p.get("panic_min", 100), p.get("yes_at_or_below", 9), p.get("partial_ratio", 0.3)
    if pk < panic:
        lit, det = "no", f"近 {len(s)} 日最多 {pk:.0f} 家，沒有恐慌性跌停潮（≥{panic} 家）"
    elif pk_d == d:
        lit, det = "no", "今天就是本波跌停最多的一天"
    elif v <= y:
        lit, det = "yes", f"恐慌日 {_md(pk_d)}（{pk:.0f} 家）後降到個位數"
    elif v <= pk * ratio:
        lit, det = "partial", f"恐慌日 {_md(pk_d)}（{pk:.0f} 家）後已降到 3 成以下"
    else:
        lit, det = "no", f"恐慌日 {_md(pk_d)}（{pk:.0f} 家），跌停還很多"
    return {"value": f"{v:.0f} 家", "as_of": d, "lit": lit,
            "rule": f"恐慌日（≥{panic} 家）後降到 ≤{y} 家完全符合、降到 {ratio:.0%} 以下初步", "detail": det}


def r_spot(daily, upto, p):
    s = _ser(daily, "foreign_spot_yi", upto, int(p.get("window_days", 10)))
    if not s:
        return None
    d, v = s[-1]
    n, ratio, heavy = int(p.get("yes_buy_days", 2)), p.get("partial_shrink_ratio", 0.5), p.get("heavy_sell_yi", 150)
    ms_d, ms = min(s, key=lambda t: t[1])
    if ms > -heavy:
        lit, det = "no", f"近 {len(s)} 日沒有重賣（最大賣超 {min(ms, 0):,.0f} 億，門檻 {heavy:g} 億），談不上止賣"
    elif len(s) >= n and all(x >= 0 for _, x in s[-n:]):
        lit, det = "yes", f"重賣（{_md(ms_d)} {ms:,.0f} 億）後連 {n} 日買超"
    elif _shrink_days(s) >= int(p.get("yes_shrink_days", 3)) and ms_d != d and v >= ms * ratio:
        # 11/20 第二輪審查：連續縮小還要「已經縮到最大賣超日的一半以內」才算，
        # 不然 11/09 賣 901 億（只比前一天少 1%）也會拿滿分
        lit, det = "yes", f"重賣（{_md(ms_d)} {ms:,.0f} 億）後，賣超連續縮小 {_shrink_days(s)} 天、已縮到一半以內"
    elif v >= 0:
        lit, det = "partial", f"重賣（{_md(ms_d)} {ms:,.0f} 億）後轉買超第 1 天"
    elif ms_d != d and v >= ms * ratio:
        lit, det = "partial", f"賣超縮小到最大賣超日 {_md(ms_d)}（{ms:,.0f} 億）的一半以內"
    else:
        lit, det = "no", (f"近 {len(s)} 日最大賣超 {ms:,.0f} 億（{_md(ms_d)}）" if ms < 0 else "")
    return {"value": f"{v:+,.0f} 億", "as_of": d, "lit": lit,
            "rule": (f"先有 ≥{heavy:g} 億重賣，之後連 {n} 日買超或賣超連續縮小 {p.get('yes_shrink_days', 3)} 天"
                     f"完全符合；轉買或賣超縮到最大日一半內初步"),
            "detail": det}


def _shrink_days(s: list[tuple[str, float]]) -> int:
    """結尾連續『比前一天賣得少（或轉成買超）』的天數。轉買超也算延續，
    這樣第一天買超的分數不會反而比前幾天縮小的賣超還低。"""
    n = 0
    for i in range(len(s) - 1, 0, -1):
        if s[i][1] > s[i - 1][1] and s[i - 1][1] < 0:
            n += 1
        else:
            break
    return n


def _r_vix(key: str):
    def rule(daily, upto, p):
        s = _ser(daily, key, upto, int(p.get("window_days", 10)))
        if not s:
            return None
        d, v = s[-1]
        pk_d, pk = max(s, key=lambda t: t[1])
        spike, y = p.get("spike_at_or_above", 40), int(p.get("yes_down_days", 2))
        down = _streak(s, falling=True)
        if pk < spike:
            lit, det = "no", f"近 {len(s)} 日最高 {pk:.1f}，沒衝上 {spike:g}"
        elif pk_d == d:
            lit, det = "no", f"今天創近 {len(s)} 日新高"
        else:
            lit = "yes" if down >= y else "partial" if down >= 1 else "no"
            det = f"高點 {pk:.1f}（{_md(pk_d)}）後連 {down} 天收低"
        as_of = daily[d].get("us_vix_date", d) if key == "us_vix" else d
        return {"value": f"{v:.2f}", "as_of": as_of, "lit": lit,
                "rule": f"衝上 {spike:g} 以上後連 {y} 天收低（參考、不計分）", "detail": det}
    return rule


INDICATORS = [
    ("margin_maintenance_ratio", "融資維持率", True, r_maint),
    ("forced_selling", "融資斷頭潮", True, r_forced),
    ("foreign_futures_net_short", "外資台指期淨空單", True, r_fut),
    ("retail_mtx_long_short", "小台散戶多空比", True, r_retail),
    ("pc_oi_ratio", "P/C 未平倉比", True, r_pc),
    ("twd", "新台幣", True, r_twd),
    ("limit_down_count", "上市跌停家數", True, r_limit),
    ("foreign_spot_net", "外資現貨買賣超", True, r_spot),
    ("tw_vix", "台指VIX", False, _r_vix("tw_vix")),
    ("us_vix", "美股VIX", False, _r_vix("us_vix")),
]


def evaluate(daily: dict, upto: str, policy: dict) -> dict:
    rules = policy.get("rules", {})
    items = []
    for iid, name, core, fn in INDICATORS:
        try:
            res = fn(daily, upto, rules.get(iid, {}))
        except Exception as e:  # noqa: BLE001
            res = {"lit": "na", "detail": f"計算失敗：{e}"}
        res = res or {"lit": "na", "detail": "沒有資料"}
        res.setdefault("value", "—")
        res.setdefault("as_of", None)
        res.setdefault("rule", "")
        # 資料太舊（超過 6 天）→ 不計分，避免拿舊燈號騙自己
        if res.get("as_of") and (_d(upto) - _d(res["as_of"])).days > 6 and iid != "us_vix":
            res["detail"] = f"資料停在 {res['as_of']}，不計分；" + res.get("detail", "")
            res["lit"] = "na"
        items.append({"id": iid, "name": name, "core": core, **res})
    core = [i for i in items if i["core"]]
    return {"score": sum(LIT_SCORE[i["lit"]] for i in core), "max": len(core),
            "available": sum(1 for i in core if i["lit"] != "na"), "indicators": items}


# ---------------------------------------------------------------- 計分卡
def _taiex_block(daily: dict) -> dict:
    tx = _read_json(TAIEX_STATE_FILE, {})
    close, cdate = tx.get("close"), tx.get("date")
    s = _ser(daily, "taiex", "9999-12-31")
    if s and (not cdate or s[-1][0] > cdate):
        cdate, close = s[-1]
    peak = max(tx.get("peak_120d") or 0, close or 0) or None
    dd = (close - peak) / peak * 100 if close and peak else None
    return {"date": cdate, "close": close, "peak": peak, "dist_peak_pct": dd,
            "peak_note": "近 120 個交易日收盤最高（與空頭彈藥梯同一個基準）"}


def build_card(daily: dict, policy: dict, upto: str | None = None) -> dict:
    upto = upto or max(daily)
    ev = evaluate(daily, upto, policy)
    th = policy.get("entry_threshold", 4)
    bear = _read_json(BEAR_POLICY_FILE, {})
    tx_state = _read_json(TAIEX_STATE_FILE, {})
    dates = [d for d in sorted(daily) if d <= upto][-15:]
    tx = _taiex_block(daily)
    gate = policy.get("active_when_drawdown_pct", -10)
    closes = _ser(daily, "taiex", upto)

    def _active_on(d: str):
        # 每一天各自判斷是不是「跌勢中」（儀表板小長條才不會用今天的狀態去塗過去的顏色）
        xs = [x for dd_, x in closes if dd_ <= d][-120:]
        if not xs:
            return None
        pk = max(xs + ([tx["peak"]] if tx.get("peak") and d >= (tx.get("date") or "") else []))
        return (xs[-1] / pk - 1) * 100 <= gate

    hist = [{"date": d, "score": evaluate(daily, d, policy)["score"], "active": _active_on(d)} for d in dates]
    active = tx.get("dist_peak_pct") is not None and tx["dist_peak_pct"] <= gate
    reached = active and ev["score"] >= th
    if not active:
        verdict = (f"大盤距高點 {_pct_txt(tx.get('dist_peak_pct'))}，還沒跌超過 {abs(gate):g}%："
                   "不是抄底情境，分數只供參考")
    elif reached:
        verdict = f"已達 {th} 分門檻：資料包規則＝可以放大部位"
    else:
        verdict = f"未達 {th} 分門檻：資料包規則＝維持小部位、照跌幅分批"
    return {
        "as_of": upto,
        "generated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        "score": ev["score"], "max": ev["max"], "available": ev["available"],
        "threshold": th, "reached": reached, "active": active, "active_gate_pct": gate,
        "verdict": verdict,
        "indicators": ev["indicators"],
        "score_history": hist,
        "taiex": tx,
        "tranches_pct": policy.get("tranche_drawdowns_pct", [25, 30, 35]),
        "bear": {
            "levels": [{"drop_pct": lv.get("drop_pct"), "label": lv.get("label"),
                        "cum_deploy_twd": lv.get("cum_deploy_twd")} for lv in bear.get("levels", [])],
            "reserve_twd": bear.get("reserve_twd"),
            "deployed_twd": tx_state.get("bear_deployed_twd"),
            "deploy_idx": tx_state.get("bear_deploy_idx"),
        },
    }


def summary_line(card: dict | None = None) -> str:
    """給晨報、融資推播、空頭加碼推播用的一行摘要（只讀檔，不連網）"""
    card = card if card is not None else _read_json(CARD_FILE, {})
    if not card.get("indicators"):
        return ""
    lit = [f"{i['name']}{LIT_MARK[i['lit']]}" for i in card["indicators"]
           if i["core"] and i["lit"] in ("yes", "partial")]
    if card.get("active") is False:
        # 沒跌深時分數只供參考，不要寫「門檻」讓人誤會成進場訊號
        dd = (card.get("taiex") or {}).get("dist_peak_pct")
        return (f"🎯 抄底計分卡 {card['score']:g}/{card['max']}（大盤距高點 {_pct_txt(dd)}，未跌深，只供參考；"
                f"資料 {_md(card['as_of'])}）")
    status = f"已達門檻 {card['threshold']}" if card.get("reached") else f"門檻 {card['threshold']}"
    return (f"🎯 抄底計分卡 {card['score']:g}/{card['max']}（{status}，資料 {_md(card['as_of'])}）"
            f"｜{'、'.join(lit) if lit else '8 項都還沒亮'}")


def card_text(card: dict) -> str:
    lines = []
    tx = card.get("taiex") or {}
    if tx.get("close") and tx.get("dist_peak_pct") is not None:
        lines.append(f"📊 加權 {tx['close']:,.0f}（{_md(tx['date'])}），距高點 {tx['peak']:,.0f} "
                     f"{tx['dist_peak_pct']:+.1f}%")
    for i in card["indicators"]:
        tag = "" if i["core"] else "（參考）"
        lines.append(f"{LIT_MARK[i['lit']]} {i['name']}{tag} {i['value']}（{_md(i.get('as_of'))}）"
                     + (f"：{i['detail']}" if i.get("detail") else ""))
    return "\n".join(lines)


def maybe_push(card: dict, prev: dict, policy: dict, send: bool) -> None:
    levels = sorted(policy.get("push_levels", [2, 4, 6]))
    th = card["threshold"]
    prev_lv = prev.get("push_level", 0) if prev else 0
    score = card["score"]
    base = 0 if score < policy.get("push_reset_below", 1.5) else prev_lv
    if not card.get("active"):
        # 11/20 審查：不能一沒跌深就歸零，否則大盤在 -10% 上下來回時同一級會重複推。
        # 只有分數掉回 1.5 以下，或大盤反彈到距高點 -8% 以內（跟空頭彈藥梯同一個重置線）才歸零。
        reset_above = _read_json(BEAR_POLICY_FILE, {}).get("reset_above_pct", -8)
        dd = (card.get("taiex") or {}).get("dist_peak_pct")
        card["push_level"] = 0 if (dd is not None and dd > reset_above) else base
        print(f"  推播：大盤沒跌深（{card['verdict']}），不推（已推到 {card['push_level']} 分）")
        return
    crossed = [lv for lv in levels if score >= lv > prev_lv]
    # --quiet／--dry-run 沒真的推，就不能把「已推到幾分」往上記，否則之後雲端會以為推過了
    card["push_level"] = (max(crossed) if crossed else base) if send else min(base, prev_lv)
    if not crossed:
        print(f"  推播：分數 {score:g}，沒有跨過新門檻（已推到 {prev_lv} 分）")
        return
    lv = max(crossed)
    head = "🔴 " if lv >= th else "🎯 "
    title = (f"{head}抄底計分卡 {score:g}/{card['max']}："
             + ("達到放大部位門檻" if lv >= th else "見底條件開始出現"))
    bear = card.get("bear") or {}
    body = card_text(card) + "\n\n" + f"📏 規則（資料包）：≥{th} 分才放大部位，之前小部位照 " \
        + "／".join(f"-{p}%" for p in card.get("tranches_pct", [])) + " 分批"
    if bear.get("reserve_twd"):
        body += f"\n🎒 空頭彈藥：已投 {(bear.get('deployed_twd') or 0) / 1e4:,.0f} 萬 / {bear['reserve_twd'] / 1e4:,.0f} 萬"
    body += "\nℹ️ 指標只是歷史對照，不保證見底"
    print("\n----- 推播內容 -----\n" + title + "\n" + body + "\n--------------------")
    if send:
        import telegram_notify
        telegram_notify.send(title, body)
        print("✅ 已推播")


# ---------------------------------------------------------------- 回測
def backfill(spec: str) -> None:
    sess = mfg._session()
    bt = _read_json(BACKTEST_DAILY_FILE, {})
    for part in spec.split(","):
        a, b = (_d(x.strip()) for x in part.split(":"))
        print(f"=== 補 {a} ～ {b} ===")
        for d, f in fetch_taifex(sess, a - timedelta(days=95), b).items():
            merge(bt, d, f)
        # 已經有的融資維持率（margin_fear_history，同一套算法）直接用，不重抓大表
        for r in mfg.load_history():
            if a.isoformat() <= r["date"] <= b.isoformat():
                merge(bt, r["date"], {k: r.get(k) for k in (
                    "taiex", "taiex_pct", "maint_ratio", "margin_yi", "margin_chg_yi",
                    "limit_down", "limit_up", "down_count", "up_count", "tw_vix")})
        _write_json(BACKTEST_DAILY_FILE, bt)
        days = sorted(d for d in bt if a.isoformat() <= d <= b.isoformat() and bt[d].get("fut_foreign_net") is not None)
        print(f"  期交所 {len(days)} 個交易日；開始逐日抓證交所（每天 3 次請求）")
        for i, d in enumerate(days):
            ymd, rec = d.replace("-", ""), bt[d]
            if rec.get("maint_ratio") is None:
                m = mfg.fetch_margin_day(sess, ymd)
                if m:
                    merge(bt, d, {k: v for k, v in m.items() if k != "date"})
            elif rec.get("limit_down") is None:
                merge(bt, d, fetch_breadth(sess, ymd))
            if rec.get("foreign_spot_yi") is None:
                v = fetch_foreign_spot(sess, ymd)
                if v is not None:
                    merge(bt, d, {"foreign_spot_yi": v})
            if i % 5 == 4 or i == len(days) - 1:
                _write_json(BACKTEST_DAILY_FILE, bt)
                r = bt[d]
                print(f"  [{i + 1}/{len(days)}] {d} 維持率 {r.get('maint_ratio')} 跌停 {r.get('limit_down')} "
                      f"外資 {r.get('foreign_spot_yi')} 億", flush=True)
        trading = {d for d in bt if bt[d].get("fut_foreign_net") is not None}
        for d, v in fetch_usdtwd_16(a - timedelta(days=60), b, official=True).items():
            if d in trading:
                merge(bt, d, {"usdtwd": v})
        _attach_us_vix(bt, fetch_yf("^VIX", a - timedelta(days=20), b))
        _write_json(BACKTEST_DAILY_FILE, bt)
    print(f"✅ 回測資料 {len(bt)} 天 → {BACKTEST_DAILY_FILE}")


def _fwd(closes: list[tuple[str, float]], d: str, n: int) -> float | None:
    idx = next((i for i, (x, _) in enumerate(closes) if x == d), None)
    if idx is None or idx + n >= len(closes):
        return None
    return round((closes[idx + n][1] / closes[idx][1] - 1) * 100, 2)


def _conclusion(events: list[dict], th: float) -> str:
    """回測結論（只寫數字事實，每次重跑自動更新）"""
    done = [e for e in events if e.get("bottom_date")]
    if not done:
        return ""
    lines = ["**回測結論（數字自動產生）**"]
    near = 0
    for e in done:
        daily = e.get("daily") or []
        idx = next((i for i, x in enumerate(daily) if x["date"] == e["bottom_date"]), None)
        # 只看「跌勢中」的日子（跟正式計分卡同一個前提），沒跌深那幾天的分數不算
        around = [x for x in (daily[max(0, idx - 5): idx + 6] if idx is not None else []) if x.get("active")]
        best = max(around, key=lambda x: x["score"]) if around else None
        if best and best["score"] >= th:
            near += 1
        f = e.get(f"first_{th:g}")
        if f:
            dates = [x["date"] for x in daily]
            lag = (dates.index(f["date"]) - dates.index(e["bottom_date"])
                   if f["date"] in dates and e["bottom_date"] in dates else None)
            when = (f"大底後第 {lag} 個交易日" if lag and lag > 0 else
                    "大底當天" if lag == 0 else f"大底前 {-lag} 個交易日" if lag is not None else "")
            f_txt = (f"第一次 ≥{th:g} 分在 {_md(f['date'])}（{when}，收 {f['close']:,.0f}，"
                     f"比大底收盤高 {(f['close'] / e['bottom_close'] - 1) * 100:.1f}%，之後最多再跌 {f['further_drop_pct']:.1f}%"
                     + (f"，20 日 {f['fwd20_pct']:+.1f}%" if f.get("fwd20_pct") is not None else "")
                     + (f"，60 日 {f['fwd60_pct']:+.1f}%" if f.get("fwd60_pct") is not None else "") + "）")
        else:
            f_txt = f"跌勢中從沒到 {th:g} 分"
        p = e.get("price_minus25")
        p_txt = (f"純價格 -25% 在 {_md(p['date'])}（之後再跌 {p['further_drop_pct']:.1f}%）" if p
                 else "沒跌到 -25%")
        best_txt = (f"大底前後 5 天（跌勢中）最高 {best['score']:g} 分（{_md(best['date'])}）" if best
                    else "大底前後 5 天都沒跌到計分門檻")
        lines.append(f"- {e['label']}：大底 {_md(e['bottom_date'])}（收盤距高點 {e['bottom_dd_pct']:+.1f}%）當天 "
                     f"{e['score_on_bottom']:g} 分；{best_txt}；{f_txt}；{p_txt}。")
    lines.append(f"- 大底前後 5 天內分數有到 {th:g} 分的：{near} / {len(done)} 次。")
    return "\n".join(lines)


def replay() -> None:
    bt: dict = {}
    for f in sorted((BASE / "data_export").glob("bottom_backtest_daily*.json")):
        for d, rec in _read_json(f, {}).items():
            merge(bt, d, rec)
    live = _read_json(DAILY_FILE, {})
    for d, rec in live.items():          # 正式檔較新 → 合進來一起看本波
        merge(bt, d, rec)
    policy = load_policy()
    th = policy.get("entry_threshold", 4)
    gate = policy.get("active_when_drawdown_pct", -10)
    tw = fetch_yf("^TWII", date(2023, 9, 1), datetime.now(TW_TZ).date())
    # 120 日高點要連續的日資料；yfinance 抓不到就停，別拿有缺口的官方收盤算錯高點還覆蓋掉摘要
    if len(tw) < 500 or min(tw) > "2024-01-15":
        print(f"❌ ^TWII 日資料不足（{len(tw)} 筆，最早 {min(tw) if tw else '無'}），回測中止、不覆蓋摘要")
        return
    closes = sorted(tw.items())
    for d, rec in bt.items():            # 官方收盤優先
        if rec.get("taiex"):
            tw[d] = rec["taiex"]
    closes = sorted(tw.items())
    peak_of = {}
    for i, (d, _) in enumerate(closes):
        peak_of[d] = max(c for _, c in closes[max(0, i - 119): i + 1])

    out = {"generated_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
           "threshold": th, "rule_note": "逐日只用當天以前的資料重算（沒有偷看未來）", "events": []}
    rows_csv = []
    windows = BOTTOMS + [{"id": "2026_oct_nov", "label": "2026 10～11 月（進行中）",
                          "window": ("2026-09-29", max(bt) if bt else "2026-11-20"), "bottom_search": None}]
    for ev in windows:
        a, b = ev["window"]
        days = [d for d in sorted(bt) if a <= d <= b and d in tw]
        if not days:
            continue
        scores = {}
        for d in days:
            e = evaluate(bt, d, policy)
            scores[d] = e
            dd = (tw[d] / peak_of[d] - 1) * 100
            rows_csv.append([ev["id"], d, f"{tw[d]:.2f}", f"{dd:.2f}", f"{e['score']:g}", e["available"]]
                            + [i["lit"] for i in e["indicators"]])
        info = {"id": ev["id"], "label": ev["label"], "window": [a, b], "days": len(days)}
        if ev.get("bottom_search"):
            sa, sb = ev["bottom_search"]
            cand = [(d, tw[d]) for d in days if sa <= d <= sb]
            bd, bc = min(cand, key=lambda t: t[1])
            info.update({"bottom_date": bd, "bottom_close": round(bc, 2),
                         "bottom_dd_pct": round((bc / peak_of[bd] - 1) * 100, 2),
                         "score_on_bottom": scores[bd]["score"],
                         "lit_on_bottom": [i["name"] for i in scores[bd]["indicators"]
                                           if i["core"] and i["lit"] in ("yes", "partial")]})
        # 只有「跌勢中」（距高點跌超過 gate）的日子才算數，跟正式計分卡同一個前提
        act = [d for d in days if (tw[d] / peak_of[d] - 1) * 100 <= gate]
        info["active_days"] = len(act)
        mx_d = max(act, key=lambda d: scores[d]["score"]) if act else None   # 只算跌勢中的日子
        info.update({"max_score": scores[mx_d]["score"] if mx_d else None, "max_score_date": mx_d})
        for lv in (2, th):
            hit = next((d for d in act if scores[d]["score"] >= lv), None)
            key = f"first_{lv:g}"
            if not hit:
                info[key] = None
                continue
            after = [tw[d] for d in days if d >= hit]
            info[key] = {"date": hit, "close": round(tw[hit], 2),
                         "dd_pct": round((tw[hit] / peak_of[hit] - 1) * 100, 2),
                         "further_drop_pct": round((min(after) / tw[hit] - 1) * 100, 2),
                         "fwd20_pct": _fwd(closes, hit, 20), "fwd60_pct": _fwd(closes, hit, 60)}
        # 純價格對照：收盤第一次跌破高點 -20% / -25% 的那天
        for lv in (20, 25):
            hit = next((d for d in days if (tw[d] / peak_of[d] - 1) * 100 <= -lv), None)
            if hit:
                after = [tw[d] for d in days if d >= hit]
                info[f"price_minus{lv}"] = {"date": hit, "close": round(tw[hit], 2),
                                            "further_drop_pct": round((min(after) / tw[hit] - 1) * 100, 2),
                                            "fwd20_pct": _fwd(closes, hit, 20), "fwd60_pct": _fwd(closes, hit, 60)}
            else:
                info[f"price_minus{lv}"] = None
        # 假警報：分數 >= 門檻之後 20 天內又跌超過 5%
        fa = []
        for d in act:
            if scores[d]["score"] >= th:
                nxt = [c for x, c in closes if d < x][:20]
                if nxt and min(nxt) / tw[d] - 1 <= -0.05:
                    fa.append(d)
        info["false_alarm_days"] = fa
        info["days_at_or_above_threshold"] = sum(1 for d in act if scores[d]["score"] >= th)
        # 每天分數（跌勢中的日子），給儀表板畫走勢
        info["daily"] = [{"date": d, "close": round(tw[d], 2), "dd": round((tw[d] / peak_of[d] - 1) * 100, 2),
                          "score": scores[d]["score"], "active": d in act} for d in days]
        out["events"].append(info)
        print(json.dumps({k: v for k, v in info.items() if k != "daily"}, ensure_ascii=False))
    out["conclusion"] = _conclusion(out["events"], th)
    print("\n" + out["conclusion"])
    _write_json(BACKTEST_SUMMARY_FILE, out)
    csv_path = BASE / "data_export" / "bottom_backtest_daily_scores.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event", "date", "taiex", "dd_pct", "score", "available"] + [n for _, n, _, _ in INDICATORS])
        w.writerows(rows_csv)
    print(f"✅ 回測摘要 → {BACKTEST_SUMMARY_FILE.name}；逐日明細 → {csv_path}")


# ---------------------------------------------------------------- 主流程
def main() -> None:
    args = sys.argv[1:]
    if "--backfill" in args:
        backfill(args[args.index("--backfill") + 1])
        return
    if "--replay" in args:
        replay()
        return
    dry, quiet = "--dry-run" in args, "--quiet" in args
    sess = mfg._session()
    policy = load_policy()
    daily = _read_json(DAILY_FILE, {})
    today = datetime.now(TW_TZ).date()
    update_daily(sess, daily, today - timedelta(days=150), today)
    daily = trim(daily)
    if not daily:
        print("❌ 沒有任何資料")
        return
    prev = _read_json(CARD_FILE, {})
    card = build_card(daily, policy)
    print(f"=== 抄底計分卡 {card['as_of']}：{card['score']:g}/{card['max']}（門檻 {card['threshold']}）===")
    print(card_text(card))
    print("→ " + card["verdict"])
    maybe_push(card, prev, policy, send=not (dry or quiet))
    if not dry:
        _write_json(DAILY_FILE, daily)
        # 只有時間戳不同就不重寫：避免雲端每跑一次都 commit（會增加跟其他排程搶 push 的機會）
        strip = lambda c: {k: v for k, v in (c or {}).items() if k != "generated_at"}  # noqa: E731
        if strip(card) != strip(prev):
            _write_json(CARD_FILE, card)
            print(f"✅ 已寫入 {DAILY_FILE.name}、{CARD_FILE.name}")
        else:
            print(f"ℹ️ 計分卡內容沒變，{CARD_FILE.name} 不重寫")


if __name__ == "__main__":
    main()
