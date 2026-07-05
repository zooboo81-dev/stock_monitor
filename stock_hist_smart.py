"""Smart 歷史 K 線抓取 — 上市 → 上櫃 → Shioaji fallback

問題：Shioaji kbars 對上櫃股 Volume 欄位有 bug（返回 tick 量非日累計）
解法：先試 yfinance .TW → .TWO，最後才 Shioaji（且警告 Shioaji Volume 不可信）

用法：
  from stock_hist_smart import fetch_hist
  hist = fetch_hist('6274', period='2y')  # 自動處理上市/上櫃
"""
from __future__ import annotations
import pandas as pd
import yfinance as yf


def fetch_hist(code: str, period: str = "2y") -> pd.DataFrame | None:
    """優先 yfinance .TW，其次 .TWO，最後 Shioaji（Volume 不可信）"""
    # 1) 試 .TW 上市
    try:
        h = yf.Ticker(f"{code}.TW").history(period=period, auto_adjust=False).dropna(subset=["Close"])
        if len(h) > 60:
            return h
    except Exception:
        pass

    # 2) 試 .TWO 上櫃
    try:
        h = yf.Ticker(f"{code}.TWO").history(period=period, auto_adjust=False).dropna(subset=["Close"])
        if len(h) > 60:
            return h
    except Exception:
        pass

    # 3) Shioaji fallback（Volume 不可信，須警告）
    try:
        import shioaji as sj
        import datetime
        import json
        from pathlib import Path
        # 讀 config
        cfg_file = Path(__file__).parent / "shioaji_keys.txt"
        api_key = "GRUPpRWf8re56aU2KDJ7BcxQHw3BBPyvF5ZXwQ4kCDSc"
        secret_key = "7XGnDBs8FgczRfWw2yGuJ9ppSZpEmqnHh7jVZYSMFX3H"
        api = sj.Shioaji(simulation=False)
        api.login(api_key, secret_key, contracts_timeout=20000)
        contract = None
        for src in [api.Contracts.Stocks.OTC, api.Contracts.Stocks.TSE]:
            try:
                c = src[code]
                if c:
                    contract = c
                    break
            except KeyError:
                continue
        if contract is None:
            api.logout()
            return None

        # 分批抓 30 天內
        end = datetime.date.today()
        chunks = []
        cur_end = end
        while cur_end > end - datetime.timedelta(days=730):
            cur_start = cur_end - datetime.timedelta(days=29)
            kb = api.kbars(contract=contract, start=str(cur_start), end=str(cur_end))
            chunks.append(pd.DataFrame({**kb}))
            cur_end = cur_start - datetime.timedelta(days=1)
        df = pd.concat(chunks, ignore_index=True)
        df["ts"] = pd.to_datetime(df["ts"])
        df["date"] = df["ts"].dt.date
        daily = df.groupby("date").agg(
            Open=("Open", "first"), High=("High", "max"),
            Low=("Low", "min"), Close=("Close", "last"),
            Volume=("Volume", "sum"),
        ).reset_index()
        daily["Volume"] = -1  # ★ 標記為不可信（避免流動性判斷用錯）
        daily["date_idx"] = pd.to_datetime(daily["date"])
        api.logout()
        return daily.set_index("date_idx")[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        pass

    return None


def fetch_volume_reliable(code: str, days: int = 20) -> float | None:
    """單獨抓可信量（用 yfinance）— 給流動性檢查用"""
    for suffix in [".TW", ".TWO"]:
        try:
            h = yf.Ticker(f"{code}{suffix}").history(period=f"{days*2}d", auto_adjust=False).dropna(subset=["Close"])
            if len(h) >= days:
                return float(h["Volume"].tail(days).mean() / 1000)
        except Exception:
            continue
    return None


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "6274"
    h = fetch_hist(code)
    v = fetch_volume_reliable(code)
    if h is not None:
        print(f"K 線筆數: {len(h)}")
        print(f"最近 5 日:")
        print(h.tail(5).to_string())
        if v:
            print(f"\n20 日均量（可信）: {v:,.0f} 張")
