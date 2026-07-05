"""飆股觀察 — 找近 N 日累計漲幅最大的股票

用途：即使系統不推薦（漲多了），也讓使用者知道「市場熱錢往哪跑」。
不要照這個追高，而是「題材偵測」+「事後檢討」用。
"""
from __future__ import annotations

import pandas as pd
import requests
import yfinance as yf

from discover import is_normal_stock


def find_hot_movers(
    candidate_codes: list[tuple[str, str]],
    lookback_days: int = 5,
    min_gain_pct: float = 20.0,
    top_n: int = 15,
) -> list[dict]:
    """從給定的代號池找出近 N 日漲幅最大的股票。

    candidate_codes: [(code, "TW"|"TWO"), ...]
    """
    tw_codes = [c for c, m in candidate_codes if m in ("TW", "TSE")]
    two_codes = [c for c, m in candidate_codes if m in ("TWO", "OTC")]

    results: list[dict] = []

    def _process(codes: list[str], suffix: str):
        if not codes:
            return
        # yf.download 一次塞 ~200 沒問題
        tickers = [f"{c}.{suffix}" for c in codes]
        try:
            data = yf.download(
                tickers, period="15d", auto_adjust=False,
                progress=False, group_by="ticker", threads=True,
            )
        except Exception:
            return
        if data is None or len(data) == 0:
            return

        for c in codes:
            tk = f"{c}.{suffix}"
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    if tk not in data.columns.get_level_values(0):
                        continue
                    sub = data[tk]
                    if "Close" not in sub.columns:
                        continue
                    df = sub.dropna(subset=["Close"])
                else:
                    if "Close" not in data.columns:
                        continue
                    df = data.dropna(subset=["Close"])
                if len(df) < lookback_days + 1:
                    continue
                price_now = float(df["Close"].iloc[-1])
                price_then = float(df["Close"].iloc[-lookback_days - 1])
                if price_then <= 0:
                    continue
                gain = (price_now - price_then) / price_then * 100
                if gain < min_gain_pct:
                    continue
                # 今日漲跌
                today_chg = (
                    (price_now - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100
                    if len(df) >= 2 else 0
                )
                results.append({
                    "code": c,
                    "price": price_now,
                    "gain_pct": gain,
                    "price_then": price_then,
                    "today_chg": today_chg,
                    "lookback_days": lookback_days,
                })
            except Exception:
                continue

    _process(tw_codes, "TW")
    _process(two_codes, "TWO")
    results.sort(key=lambda x: -x["gain_pct"])
    return results[:top_n]


def candidate_universe(
    instit_by_code: dict[str, list[int]],
    min_lots_abs: int = 200,
) -> list[tuple[str, str]]:
    """用法人資料當「股票池」— 至少有 ±200 張交易（成交活絡）的個股。
    回傳 [(code, 'TW' or 'TWO'), ...]
    """
    # 我們不知道哪個代號在 TWSE / TPEx，先全部試 TW（yfinance 會自動失敗 TPEx 股）
    # 但 TPEx 股約 4 位數 ≥ 6000 為主，4/5/8 開頭也有。實務上：兩種前綴都跑會重複，
    # 改用：先試 TW，沒抓到的再試 TWO（在 find_hot_movers 裡分開處理會更乾淨）
    out: list[tuple[str, str]] = []
    for code, hist in instit_by_code.items():
        if not is_normal_stock(code):
            continue
        if not hist:
            continue
        max_abs = max(abs(x) for x in hist[-5:])
        if max_abs < min_lots_abs:
            continue
        suffix = "TWO" if code.startswith(("6", "8")) else "TW"
        out.append((code, suffix))
    return out
