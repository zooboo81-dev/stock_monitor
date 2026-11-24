"""Smart 歷史 K 線抓取 — 上市 → 上櫃 → Shioaji fallback

問題：Shioaji kbars 對上櫃股 Volume 欄位有 bug（返回 tick 量非日累計）
解法：先試 yfinance .TW → .TWO，最後才 Shioaji（且警告 Shioaji Volume 不可信）

用法：
  from stock_hist_smart import fetch_hist
  hist = fetch_hist('6274', period='2y')  # 自動處理上市/上櫃

永豐 Shioaji 金鑰（2026-11-24 起不再寫在程式裡；這個倉庫是公開的）：
  依序找，找到就用，兩個檔都不上 GitHub：
    1. 本資料夾的 shioaji_keys.txt
    2. 台指期記分板的 C:\\Users\\<你>\\txf-backtest\\API key.txt（同一組金鑰，換金鑰只要改這一個）
  格式跟記分板一樣，記事本打兩行：
    API_KEY=你的_API_Key
    SECRET_KEY=你的_Secret_Key
  都找不到 → 直接跳過 Shioaji 這個備援，不會當掉（排程試跑的沙盒刻意不放金鑰、也不去別的資料夾找）。
"""
from __future__ import annotations
import os
from pathlib import Path

import pandas as pd
import yfinance as yf

KEY_FILE = Path(__file__).resolve().parent / "shioaji_keys.txt"
SHARED_KEY_FILE = Path.home() / "txf-backtest" / "API key.txt"


def _read_key_file(p: Path) -> tuple[str, str] | None:
    """讀 API_KEY=… / SECRET_KEY=… 兩行（# 開頭當註解）。檔案不在或格式不對 → None"""
    try:
        text = p.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None
    creds = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        creds[k.strip().upper()] = v.strip().strip('"').strip("'")
    api_key, secret = creds.get("API_KEY"), creds.get("SECRET_KEY")
    if not api_key or not secret or api_key.startswith("你的") or secret.startswith("你的"):
        return None
    return api_key, secret


def load_shioaji_keys() -> tuple[str, str] | None:
    """回傳 (api_key, secret_key)；找不到金鑰檔 → None（呼叫的人自己跳過 Shioaji）"""
    candidates = [KEY_FILE]
    if os.environ.get("STOCK_SELFTEST") != "1":   # 試跑沙盒：只看沙盒自己（裡面沒有金鑰）
        candidates.append(SHARED_KEY_FILE)
    for p in candidates:
        keys = _read_key_file(p)
        if keys:
            return keys
    return None


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

    # 3) Shioaji fallback（Volume 不可信，須警告）；沒有金鑰檔就跳過
    keys = load_shioaji_keys()
    if keys is None:
        return None
    api = None
    try:
        import shioaji as sj
        import datetime
        api = sj.Shioaji(simulation=False)
        api.login(api_key=keys[0], secret_key=keys[1], contracts_timeout=20000)
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
        return daily.set_index("date_idx")[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:   # 套件沒裝／被 Windows 擋、金鑰作廢、登入失敗、抓不到 → 都當作這個來源沒有
        return None
    finally:
        if api is not None:   # 中途出錯也要登出，免得佔住永豐的連線數
            try:
                api.logout()
            except Exception:
                pass


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
