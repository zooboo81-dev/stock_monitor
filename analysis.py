"""技術指標 + K線型態 + 主力評分

所有函式都是純 pandas/numpy，無第三方 TA 套件依賴
（避免 pandas-ta 在 pandas 3.0 下的相容問題）。
"""
from __future__ import annotations

import pandas as pd


# ──────────────── 技術指標 ────────────────
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD = DIF, DEM(MACD signal), OSC(柱狀體)"""
    dif = ema(close, fast) - ema(close, slow)
    dem = ema(dif, signal)
    osc = dif - dem
    return dif, dem, osc


def kd_taiwan(df: pd.DataFrame, period: int = 9):
    """台股慣用 KD（Wilder 平滑，初始 K=D=50）"""
    low_n = df["Low"].rolling(period).min()
    high_n = df["High"].rolling(period).max()
    rng = (high_n - low_n).replace(0, pd.NA)
    rsv = (df["Close"] - low_n) / rng * 100
    rsv = rsv.fillna(50.0)

    k_vals: list[float] = []
    d_vals: list[float] = []
    prev_k = prev_d = 50.0
    for v in rsv:
        cur_k = (1 / 3) * float(v) + (2 / 3) * prev_k
        cur_d = (1 / 3) * cur_k + (2 / 3) * prev_d
        k_vals.append(cur_k)
        d_vals.append(cur_d)
        prev_k, prev_d = cur_k, cur_d
    return pd.Series(k_vals, index=df.index), pd.Series(d_vals, index=df.index)


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    return ma + num_std * std, ma, ma - num_std * std


def bias(close: pd.Series, period: int = 10) -> pd.Series:
    ma = close.rolling(period).mean()
    return (close - ma) / ma * 100


# ──────────────── K線型態 ────────────────
def detect_candle_pattern(df: pd.DataFrame) -> tuple[str, str] | None:
    """偵測最後一根 K 的型態，回傳 (情緒色, 名稱) 或 None。"""
    if len(df) < 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]
    o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])
    po, pc = float(prev["Open"]), float(prev["Close"])
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return None
    upper = h - max(o, c)
    lower = min(o, c) - l

    # 紅K吞噬（前日黑K，今日紅K完全包住前日實體）
    if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
        return ("🟢", "紅K吞噬")
    # 黑K吞噬
    if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
        return ("🔴", "黑K吞噬")
    # 鎚子線：實體小、下影長、上影短
    if body < rng * 0.3 and lower > body * 2 and upper < body:
        return ("🟢", "鎚子線")
    # 流星線：實體小、上影長、下影短
    if body < rng * 0.3 and upper > body * 2 and lower < body:
        return ("🔴", "流星線")
    # 十字星
    if body < rng * 0.08:
        return ("🟡", "十字星（猶豫）")
    return None


# ──────────────── 主力評分聚合 ────────────────
def _empty_score() -> dict:
    return {"score": 0, "verdict": "資料不足", "verdict_emoji": "⚪",
            "breakdown": [], "indicators": {},
            "attention_level": 0, "attention_reasons": [],
            "bullish_cnt": 0, "bearish_cnt": 0}


def smart_money_score(hist: pd.DataFrame, instit: dict | None = None) -> dict:
    """
    回傳 {
      'score': int (-10..+10),
      'verdict': '強烈加碼' | '偏多' | '中性' | '偏空' | '強烈減碼',
      'verdict_emoji': '🟢' | ...,
      'breakdown': [(category, points, reason), ...],
      'indicators': {macd, kd, bbands, ma, candle, bias}
    }
    """
    if hist is None or hist.empty or "Close" not in hist.columns:
        return _empty_score()
    h = hist.dropna(subset=["Close"])
    if len(h) < 30:
        return _empty_score()

    close = h["Close"]
    score = 0
    breakdown: list[tuple[str, int, str]] = []

    # 1) MACD：黃金交叉 / 死叉
    dif, dem, osc = macd(close)
    if len(dif) >= 2:
        cross_up = dif.iloc[-1] > dem.iloc[-1] and dif.iloc[-2] <= dem.iloc[-2]
        cross_dn = dif.iloc[-1] < dem.iloc[-1] and dif.iloc[-2] >= dem.iloc[-2]
        above = dif.iloc[-1] > 0
        if cross_up:
            score += 2
            breakdown.append(("MACD", +2, f"黃金交叉（DIF {dif.iloc[-1]:.2f} 上穿 MACD）"))
        elif cross_dn:
            score -= 2
            breakdown.append(("MACD", -2, f"死亡交叉（DIF {dif.iloc[-1]:.2f} 下穿 MACD）"))
        elif above and osc.iloc[-1] > osc.iloc[-2]:
            score += 1
            breakdown.append(("MACD", +1, "0 軸上方且柱狀放大（多方延續）"))
        elif not above and osc.iloc[-1] < osc.iloc[-2]:
            score -= 1
            breakdown.append(("MACD", -1, "0 軸下方且柱狀放大（空方延續）"))

    # 2) KD：黃金交叉 / 死叉 / 超買 / 超賣
    k, d = kd_taiwan(h)
    if len(k) >= 2:
        cross_up = k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]
        cross_dn = k.iloc[-1] < d.iloc[-1] and k.iloc[-2] >= d.iloc[-2]
        if cross_up and k.iloc[-1] < 50:
            score += 2
            breakdown.append(("KD", +2, f"低檔黃金交叉 K={k.iloc[-1]:.0f} D={d.iloc[-1]:.0f}"))
        elif cross_up:
            score += 1
            breakdown.append(("KD", +1, f"黃金交叉 K={k.iloc[-1]:.0f}"))
        elif cross_dn and k.iloc[-1] > 50:
            score -= 2
            breakdown.append(("KD", -2, f"高檔死亡交叉 K={k.iloc[-1]:.0f} D={d.iloc[-1]:.0f}"))
        elif cross_dn:
            score -= 1
            breakdown.append(("KD", -1, f"死亡交叉 K={k.iloc[-1]:.0f}"))
        elif k.iloc[-1] >= 80:
            score -= 1
            breakdown.append(("KD", -1, f"超買區 K={k.iloc[-1]:.0f}（注意回檔）"))
        elif k.iloc[-1] <= 20:
            score += 1
            breakdown.append(("KD", +1, f"超賣區 K={k.iloc[-1]:.0f}（醞釀反彈）"))

    # 3) 均線多空頭排列（5/10/20/60）
    if len(close) >= 60:
        m5 = close.rolling(5).mean().iloc[-1]
        m10 = close.rolling(10).mean().iloc[-1]
        m20 = close.rolling(20).mean().iloc[-1]
        m60 = close.rolling(60).mean().iloc[-1]
        if m5 > m10 > m20 > m60:
            score += 2
            breakdown.append(("均線", +2, "多頭排列（5>10>20>60）"))
        elif m5 < m10 < m20 < m60:
            score -= 2
            breakdown.append(("均線", -2, "空頭排列（5<10<20<60）"))
        elif close.iloc[-1] > m60 and close.iloc[-1] > m20:
            breakdown.append(("均線", 0, "站上季線+月線"))
        elif close.iloc[-1] < m60:
            score -= 1
            breakdown.append(("均線", -1, "跌破季線"))

    # 4) K線型態
    candle = detect_candle_pattern(h)
    if candle:
        emoji, name = candle
        pts = 1 if emoji == "🟢" else (-1 if emoji == "🔴" else 0)
        score += pts
        breakdown.append(("K線", pts, name))

    # 5) 量價（爆量長紅/長黑、量價背離）
    vol = h["Volume"]
    vol5 = vol.rolling(5).mean()
    if len(vol5) >= 2 and pd.notna(vol5.iloc[-2]) and vol5.iloc[-2] > 0:
        ratio = vol.iloc[-1] / vol5.iloc[-2]
        chg = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]
        if ratio >= 2 and chg >= 0.03:
            score += 1
            breakdown.append(("量價", +1, f"爆量長紅 {ratio:.1f}x，漲 {chg*100:.1f}%"))
        elif ratio >= 2 and chg <= -0.03:
            score -= 2
            breakdown.append(("量價", -2, f"爆量長黑 {ratio:.1f}x，跌 {chg*100:.1f}%"))

    # 6) 三大法人（外傳入）
    if instit:
        net_days = instit.get("consecutive_days", 0)
        single = instit.get("latest_net", 0)
        if net_days >= 3:
            score += 2
            breakdown.append(("法人", +2, f"連續 {net_days} 日買超"))
        elif net_days <= -3:
            score -= 2
            breakdown.append(("法人", -2, f"連續 {abs(net_days)} 日賣超"))
        if single >= 1000:
            score += 1
            breakdown.append(("法人", +1, f"今日大買 {single:+,} 張"))
        elif single <= -1000:
            score -= 1
            breakdown.append(("法人", -1, f"今日大賣 {single:+,} 張"))

    # 7) BIAS 過度乖離
    b10 = bias(close, 10)
    if pd.notna(b10.iloc[-1]):
        b = float(b10.iloc[-1])
        if b >= 8:
            score -= 1
            breakdown.append(("乖離", -1, f"10日乖離 +{b:.1f}%（漲多）"))
        elif b <= -8:
            score += 1
            breakdown.append(("乖離", +1, f"10日乖離 {b:.1f}%（跌深）"))

    # 限制範圍
    score = max(-10, min(10, score))

    # 結論
    if score >= 6:
        verdict, emoji = "強烈加碼", "🟢🟢"
    elif score >= 3:
        verdict, emoji = "偏多（可逢低加碼）", "🟢"
    elif score >= -2:
        verdict, emoji = "中性觀望", "⚪"
    elif score >= -5:
        verdict, emoji = "偏空（可逢高減碼）", "🔴"
    else:
        verdict, emoji = "強烈減碼", "🔴🔴"

    # ── 特別關注判定 ──
    # level 0=普通，1=注意 ⭐，2=強烈 ⭐⭐
    bullish_cnt = sum(1 for _, p, _ in breakdown if p > 0)
    bearish_cnt = sum(1 for _, p, _ in breakdown if p < 0)
    aligned = max(bullish_cnt, bearish_cnt)
    attention_level = 0
    attention_reasons: list[str] = []
    if abs(score) >= 6:
        attention_level = 2
        attention_reasons.append("評分極端")
    if aligned >= 4:
        attention_level = max(attention_level, 2)
        attention_reasons.append(f"{aligned} 個訊號同方向疊加")
    elif aligned >= 3:
        attention_level = max(attention_level, 1)
        attention_reasons.append(f"{aligned} 個訊號同方向")
    if instit and abs(instit.get("consecutive_days", 0)) >= 5:
        attention_level = max(attention_level, 1)
        attention_reasons.append(f"法人連 {abs(instit['consecutive_days'])} 日同向")

    return {
        "score": score,
        "verdict": verdict,
        "verdict_emoji": emoji,
        "breakdown": breakdown,
        "attention_level": attention_level,
        "attention_reasons": attention_reasons,
        "bullish_cnt": bullish_cnt,
        "bearish_cnt": bearish_cnt,
        "indicators": {
            "macd_dif": float(dif.iloc[-1]) if len(dif) else None,
            "macd_dem": float(dem.iloc[-1]) if len(dem) else None,
            "macd_osc": float(osc.iloc[-1]) if len(osc) else None,
            "k": float(k.iloc[-1]) if len(k) else None,
            "d": float(d.iloc[-1]) if len(d) else None,
        },
    }
