"""趨勢箭頭回測 — 產生 data/trend_stats.json（箭頭旁邊那句「歷史上…」的數字來源）

做什麼：
  1. 大盤：^TWII 2003 年至今，每個 T 分數／多空方之後 20、60 日的表現
  2. 個股：全體上市櫃普通股，「當時」20 日均成交金額前 300 大（避免只看現在的熱門股），
     每個 T 分數之後 20、60 日的絕對報酬與「相對同日全體平均」的超額報酬
  3. 對照組：舊的 smart_money_score（不含法人）分數帶，同一把尺

用法：
  py -3 trend_backtest.py            # 下載資料（約 5~10 分鐘）＋回測＋寫 json
  py -3 trend_backtest.py --cached   # 用 data_export/research_cache 的快取重算

誠實聲明（寫進 json 的 caveats）：
  - 抓不到已下市股票 → 個股絕對報酬偏樂觀；所以個股主要看「超額」欄位
  - 進場價用訊號隔日開盤、不含交易成本（這裡量的是方向準度，不是策略損益）
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from trend import SIDE_IN, SIDE_OUT, trend_frame

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data_export" / "research_cache"
OUT = ROOT / "data" / "trend_stats.json"
TOP_N_LIQUID = 300
STOCK_START = "2020-06-01"


# ───────────────────────── 資料 ─────────────────────────
def _session():
    import requests
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    return s


def list_all_codes() -> list[str]:
    """上市＋上櫃 4 碼普通股的 yfinance 代號"""
    s = _session()
    ok = lambda c: len(c) == 4 and c.isdigit() and not c.startswith("00")
    tw = s.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30).json()
    tp = s.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=30).json()
    out = [r["Code"] + ".TW" for r in tw if ok(r["Code"])]
    out += [r["SecuritiesCompanyCode"] + ".TWO" for r in tp if ok(r["SecuritiesCompanyCode"])]
    return sorted(set(out))


def download_all() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    tickers = list_all_codes()
    print(f"下載 {len(tickers)} 檔…")
    stocks: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 150):
        batch = tickers[i:i + 150]
        try:
            d = yf.download(batch, start="2019-06-01", auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
            for t in batch:
                try:
                    x = d[t].dropna(subset=["Close"])
                    if len(x) > 300:
                        stocks[t.split(".")[0]] = x[["Open", "High", "Low", "Close", "Volume"]].astype("float32")
                except Exception:
                    pass
        except Exception as e:
            print("  批次失敗：", e)
        print(f"  {min(i + 150, len(tickers))}/{len(tickers)}  已得 {len(stocks)}")
        time.sleep(2)
    idx = yf.download("^TWII", start="2003-01-01", auto_adjust=False, progress=False)
    if isinstance(idx.columns, pd.MultiIndex):
        idx.columns = idx.columns.get_level_values(0)
    idx = idx.dropna(subset=["Close"])
    CACHE.mkdir(parents=True, exist_ok=True)
    pickle.dump(stocks, open(CACHE / "stocks.pkl", "wb"))
    pickle.dump(idx, open(CACHE / "taiex.pkl", "wb"))
    return stocks, idx


def load_cached() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    return (pickle.load(open(CACHE / "stocks.pkl", "rb")),
            pickle.load(open(CACHE / "taiex.pkl", "rb")))


# ───────────────────────── 舊評分（對照組，向量化）─────────────────────────
def legacy_score_frame(h: pd.DataFrame) -> pd.DataFrame:
    """analysis.smart_money_score 不含法人的向量化版本，逐項保留以便檢查哪一項方向錯。"""
    c, o, hi, lo, v = (h[k].astype("float64") for k in ("Close", "Open", "High", "Low", "Volume"))
    e = lambda s, n: s.ewm(span=n, adjust=False).mean()
    dif = e(c, 12) - e(c, 26)
    dem = e(dif, 9)
    osc = dif - dem
    cu = (dif > dem) & (dif.shift() <= dem.shift())
    cd = (dif < dem) & (dif.shift() >= dem.shift())
    macd = np.select([cu, cd, (dif > 0) & (osc > osc.shift()), (dif <= 0) & (osc < osc.shift())], [2, -2, 1, -1], 0)
    ln, hn = lo.rolling(9).min(), hi.rolling(9).max()
    rsv = ((c - ln) / (hn - ln).replace(0, np.nan) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    ku = (k > d) & (k.shift() <= d.shift())
    kdn = (k < d) & (k.shift() >= d.shift())
    kd = np.select([ku & (k < 50), ku, kdn & (k > 50), kdn, k >= 80, k <= 20], [2, 1, -2, -1, -1, 1], 0)
    m5, m10, m20, m60 = (c.rolling(n).mean() for n in (5, 10, 20, 60))
    ma = np.select([(m5 > m10) & (m10 > m20) & (m20 > m60), (m5 < m10) & (m10 < m20) & (m20 < m60),
                    (c > m60) & (c > m20), c < m60], [2, -2, 0, -1], 0)
    body, rng = (c - o).abs(), hi - lo
    up, dn = hi - np.maximum(o, c), np.minimum(o, c) - lo
    po, pc = o.shift(), c.shift()
    be = (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > (pc - po).abs())
    se = (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > (pc - po).abs())
    ham = (body < rng * 0.3) & (dn > body * 2) & (up < body)
    sst = (body < rng * 0.3) & (up > body * 2) & (dn < body)
    cs = np.where(rng > 0, np.select([be, se, ham, sst], [1, -1, 1, -1], 0), 0)
    ratio = v / v.rolling(5).mean().shift()
    chg = c.pct_change()
    vp = np.select([(ratio >= 2) & (chg >= 0.03), (ratio >= 2) & (chg <= -0.03)], [1, -2], 0)
    b = (c - m10) / m10 * 100
    bi = np.select([b >= 8, b <= -8], [-1, 1], 0)
    out = pd.DataFrame({"s_macd": macd, "s_kd": kd, "s_ma": ma, "s_candle": cs, "s_vol": vp, "s_bias": bi},
                       index=h.index)
    out["score"] = out.sum(axis=1).clip(-10, 10)
    return out


# ───────────────────────── 面板 ─────────────────────────
def build_panel(stocks: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames = []
    for code, h in stocks.items():
        h = h[h["Volume"] > 0]
        if len(h) < 300:
            continue
        h = h.astype("float64")
        tf = trend_frame(h["Close"])
        ls = legacy_score_frame(h)
        c, o = h["Close"], h["Open"]
        d = pd.DataFrame({"T": tf["T"], "side": tf["side"], "valid": tf["valid"]}, index=h.index).join(ls)
        d["val20"] = (c * h["Volume"]).rolling(20).mean()
        d["hi60"] = c / c.rolling(60).max() - 1
        d["d60"] = c / tf["ma60"] - 1
        d["ret20"] = c / c.shift(20) - 1
        o1 = o.shift(-1)
        for n in (5, 20, 60):
            d[f"f{n}"] = ((c.shift(-n) / o1 - 1) * 100).clip(-70, 200)
        d["code"] = code
        frames.append(d[d["valid"]])
    p = pd.concat(frames)
    p.index.name = "Date"
    p = p.reset_index()
    p = p[p["Date"] >= STOCK_START]
    # 「當時」流動性前 N 大
    p["liq_rank"] = p.groupby("Date")["val20"].rank(ascending=False, method="first")
    p = p[p["liq_rank"] <= TOP_N_LIQUID].copy()
    for n in (5, 20, 60):
        p[f"x{n}"] = p[f"f{n}"] - p.groupby("Date")[f"f{n}"].transform("mean")
    return p


def _agg_stock(g: pd.DataFrame) -> dict:
    return {
        "n": int(len(g)),
        "f20": round(float(g["f20"].mean()), 2), "up20": round(float((g["f20"] > 0).mean() * 100), 1),
        "f60": round(float(g["f60"].mean()), 2), "up60": round(float((g["f60"] > 0).mean() * 100), 1),
        "x5": round(float(g["x5"].mean()), 2),
        "x20": round(float(g["x20"].mean()), 2), "x60": round(float(g["x60"].mean()), 2),
        "p5_20": round(float(g["f20"].quantile(0.05)), 1),
    }


def stock_stats(p: pd.DataFrame) -> dict:
    by_t = {str(t): _agg_stock(g) for t, g in p.groupby("T")}
    bins = [-11, -6, -3, -1, 2, 5, 10]
    labs = ["強烈減碼(<=-6)", "偏空(-5~-3)", "中性偏弱(-2~-1)", "中性(0~2)", "偏多(3~5)", "強烈加碼(>=6)"]
    legacy = {str(k): _agg_stock(g) for k, g in p.groupby(pd.cut(p["score"], bins, labels=labs), observed=True)}
    comps = {}
    for col in ("s_macd", "s_kd", "s_ma", "s_candle", "s_vol", "s_bias"):
        comps[col] = {str(int(k)): _agg_stock(g) for k, g in p.groupby(col)}
    yearly = {}
    for yr, g in p.groupby(p["Date"].dt.year):
        yearly[str(yr)] = {
            "T>=4_x20": round(float(g[g["T"] >= 4]["x20"].mean()), 2),
            "T<=1_x20": round(float(g[g["T"] <= 1]["x20"].mean()), 2),
            "legacy>=3_x20": round(float(g[g["score"] >= 3]["x20"].mean()), 2),
            "legacy<=-3_x20": round(float(g[g["score"] <= -3]["x20"].mean()), 2),
        }
    return {"by_t": by_t, "legacy_score": legacy, "legacy_components": comps, "yearly": yearly,
            "universe": f"每日 20 日均成交金額前 {TOP_N_LIQUID} 大，{p['Date'].min().date()}~{p['Date'].max().date()}，"
                        f"{p['code'].nunique()} 檔、{len(p):,} 個樣本"}


def index_stats(idx: pd.DataFrame) -> dict:
    c = idx["Close"].astype("float64")
    tf = trend_frame(c)
    d = pd.DataFrame({"T": tf["T"], "side": tf["side"], "valid": tf["valid"]})
    for n in (20, 60):
        d[f"f{n}"] = (c.shift(-n) / c - 1) * 100
    d = d[d["valid"]]

    def agg(g):
        return {"days": int(len(g)),
                "f20": round(float(g["f20"].mean()), 2), "up20": round(float((g["f20"] > 0).mean() * 100), 1),
                "p5_20": round(float(g["f20"].quantile(0.05)), 1),
                "f60": round(float(g["f60"].mean()), 2), "up60": round(float((g["f60"] > 0).mean() * 100), 1),
                "p5_60": round(float(g["f60"].quantile(0.05)), 1)}

    by_t = {str(t): agg(g) for t, g in d.groupby("T")}
    by_side = {s: agg(g) for s, g in d.groupby("side")}
    # 多空方當閘門的長期效果（空方日空手）
    r = c.pct_change().shift(-1).reindex(d.index).fillna(0)
    sig = (d["side"] == "long").astype(float)

    def perf(x):
        eq = (1 + x).cumprod()
        yrs = len(x) / 250
        return {"cagr": round(float((eq.iloc[-1] ** (1 / yrs) - 1) * 100), 1),
                "mdd": round(float((eq / eq.cummax() - 1).min() * 100), 1)}

    bad = d[d["f20"] < -10]
    return {
        "by_t": by_t, "by_side": by_side,
        "gate": {"buy_hold": perf(r), "long_side_only": perf(r * sig),
                 "switches": int((sig != sig.shift()).sum()),
                 "pct_days_short": round(float((1 - sig.mean()) * 100), 1),
                 "pct_of_big_drops_flagged_short": round(float((bad["side"] == "short").mean() * 100), 1)},
        "period": f"{d.index[0].date()}~{d.index[-1].date()}，{len(d):,} 個交易日",
    }


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--cached" in sys.argv and (CACHE / "stocks.pkl").exists():
        stocks, idx = load_cached()
    else:
        stocks, idx = download_all()
    print(f"個股 {len(stocks)} 檔，大盤 {len(idx)} 日")
    panel = build_panel(stocks)
    res = {
        "generated": date.today().isoformat(),
        "rule": {"side_out": SIDE_OUT, "side_in": SIDE_IN,
                 "T": "收盤>20MA、20MA>60MA、收盤>60MA、60MA上揚、收盤>120MA 成立幾個"},
        "index": index_stats(idx),
        "stock": stock_stats(panel),
        "caveats": ["個股樣本不含已下市股票，絕對報酬偏樂觀，請看超額(x)欄位",
                    "報酬為訊號隔日開盤進場、未扣交易成本，量的是方向準度不是策略損益"],
    }
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 寫入 {OUT}")
    print("\n大盤 by T：")
    print(pd.DataFrame(res["index"]["by_t"]).T.to_string())
    print("\n大盤 多空方：", json.dumps(res["index"]["by_side"], ensure_ascii=False))
    print("閘門：", json.dumps(res["index"]["gate"], ensure_ascii=False))
    print("\n個股 by T：")
    print(pd.DataFrame(res["stock"]["by_t"]).T.to_string())
    print("\n舊評分對照：")
    print(pd.DataFrame(res["stock"]["legacy_score"]).T.to_string())
    print("\n舊評分各項（x20 = 之後 20 日超額）：")
    for col, tab in res["stock"]["legacy_components"].items():
        print(" ", col, {k: v["x20"] for k, v in tab.items()})
    print("\n逐年：")
    print(pd.DataFrame(res["stock"]["yearly"]).T.to_string())
    return panel


if __name__ == "__main__":
    main()
