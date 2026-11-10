"""趨勢方向（箭頭）— 大盤與個股共用的單一判斷來源

為什麼有這支（2026-11-11）：
  舊系統同時有三套「方向」互相打架 ——
    1. macro.detect_market_regime：只看 200MA ±2%（10/29~11/4 大盤跌 14% 還顯示「多頭、訊號可信」）
    2. taiex_ma_tracker.classify：跌破 60MA 就叫「中期空頭」
    3. analysis.smart_money_score：把 KD 低檔交叉、負乖離（逆勢訊號）跟均線排列（順勢訊號）加在一起
  回測結果（trend_backtest.py，結果寫在 data/trend_stats.json）：
    - 舊評分「偏多」之後 20 日上漲機率 45.9%，反而低於「偏空」的 51.2%；KD 那一項方向是反的
    - 大盤：T<=1（空方）之後平均報酬趨近 0、最差情境加倍；只在多方持有，回撤從 -58% 降到 -26%
    - 個股：只有 T=5（⬆ 多頭）明顯贏同期個股（20 日 +0.7%、60 日 +1.3%），T<=4 都略輸；
      「T>=4 贏 T<=1」在 2020~2026 每一年都成立，但差距不大（每 20 日 0.2~1.6%）
    - 誠實話：個股未來 20 日「會不會漲」任何箭頭都接近五五波（47~53%）。
      箭頭的用途是「現在順勢還是逆勢」，真正影響勝負的是大盤多空方（見下）與停損。

T 分數 = 下列 5 個條件成立幾個（0~5）：
    收盤 > 20MA、20MA > 60MA、收盤 > 60MA、60MA 比 20 天前高、收盤 > 120MA

箭頭：5 ⬆ 多頭｜4 ↗ 偏多｜3 → 盤整偏多｜2 → 盤整偏弱｜1 ↘ 偏空｜0 ⬇ 空頭

多空方（side，給閘門用，有遲滯避免天天翻）：
    T ≤ 1 → 轉「空方」；之後要 T ≥ 3 才翻回「多方」；中間維持原狀。
    大盤 2003~2026：空方期間只佔 27% 的天數，卻包住 55% 的「之後 20 天跌超過 10%」的日子。
    注意：空方 ≠ 一定會跌（之後 20 日上漲機率仍有 57%），它代表的是「平均報酬趨近 0、
    最差情境加倍」，所以用途是「不加碼、收緊停損」，不是放空訊號。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

STATS_FILE = Path(__file__).parent / "data" / "trend_stats.json"

SIDE_OUT = 1   # T <= 1 → 空方
SIDE_IN = 3    # T >= 3 → 多方

LEVELS = {
    5: {"arrow": "⬆", "label": "多頭", "color": "#1b7f3b", "bg": "#e6f4ea"},
    4: {"arrow": "↗", "label": "偏多", "color": "#2e9e52", "bg": "#edf7ee"},
    3: {"arrow": "→", "label": "盤整偏多", "color": "#6b7280", "bg": "#f5f5f5"},
    2: {"arrow": "→", "label": "盤整偏弱", "color": "#b26a00", "bg": "#fff8e1"},
    1: {"arrow": "↘", "label": "偏空", "color": "#d9534f", "bg": "#fdecea"},
    0: {"arrow": "⬇", "label": "空頭", "color": "#b71c1c", "bg": "#fde7e9"},
}

CHECK_NAMES = [
    ("above_ma20", "收盤 > 20MA"),
    ("ma20_gt_ma60", "20MA > 60MA"),
    ("above_ma60", "收盤 > 60MA"),
    ("ma60_rising", "60MA 上揚"),
    ("above_ma120", "收盤 > 120MA"),
]


def trend_frame(close: pd.Series) -> pd.DataFrame:
    """整條序列的趨勢分數（向量化，回測與即時共用同一份邏輯）。
    需要至少 140 根才有值（120MA + 60MA 的 20 日斜率）。
    """
    c = close.astype("float64")
    m20 = c.rolling(20).mean()
    m60 = c.rolling(60).mean()
    m120 = c.rolling(120).mean()
    df = pd.DataFrame({
        "close": c, "ma20": m20, "ma60": m60, "ma120": m120,
        "above_ma20": c > m20,
        "ma20_gt_ma60": m20 > m60,
        "above_ma60": c > m60,
        "ma60_rising": m60 > m60.shift(20),
        "above_ma120": c > m120,
    })
    df["T"] = df[[k for k, _ in CHECK_NAMES]].sum(axis=1).astype(int)
    df["valid"] = m120.notna() & m60.shift(20).notna()
    df["side"] = _side_series(df["T"], df["valid"])
    return df


def _side_series(t: pd.Series, valid: pd.Series) -> pd.Series:
    """遲滯多空方：T<=1 轉空、T>=3 轉多、中間維持。"""
    out: list[str] = []
    state = "long"
    for tv, ok in zip(t.tolist(), valid.tolist()):
        if ok:
            if tv <= SIDE_OUT:
                state = "short"
            elif tv >= SIDE_IN:
                state = "long"
        out.append(state)
    return pd.Series(out, index=t.index)


_STATS_CACHE: dict = {"mtime": None, "data": {}}


def load_stats() -> dict:
    """讀回測統計（trend_backtest.py 產生）。檔不在就回空 dict，不影響箭頭本身。"""
    try:
        mt = STATS_FILE.stat().st_mtime
        if _STATS_CACHE["mtime"] != mt:
            _STATS_CACHE["data"] = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            _STATS_CACHE["mtime"] = mt
        return _STATS_CACHE["data"]
    except Exception:
        return {}


def trend_state(hist: pd.DataFrame | pd.Series | None, kind: str = "stock") -> dict:
    """回傳最新一根的趨勢狀態。

    kind: "stock" 或 "index" —— 只影響附帶的歷史統計取哪一張表。
    回傳鍵：t, arrow, label, color, bg, side, side_label, side_since, side_days,
            checks[(名稱, 是否成立)], stat（該狀態的歷史統計，可能為 None）, text
    """
    empty = {"t": None, "arrow": "·", "label": "資料不足", "color": "#999", "bg": "#f5f5f5",
             "side": "unknown", "side_label": "—", "side_since": None, "side_days": 0,
             "checks": [], "stat": None, "text": "· 資料不足"}
    if hist is None:
        return empty
    close = hist["Close"] if isinstance(hist, pd.DataFrame) else hist
    close = close.dropna()
    if len(close) < 141:
        return empty
    df = trend_frame(close)
    last = df.iloc[-1]
    if not bool(last["valid"]):
        return empty
    t = int(last["T"])
    lv = LEVELS[t]
    side = str(last["side"])
    # 這個多空方從哪天開始
    changed = df["side"] != df["side"].shift()
    since_idx = df.index[changed][-1]
    side_days = int((df.index >= since_idx).sum())
    try:
        since_str = pd.Timestamp(since_idx).strftime("%Y-%m-%d")
    except Exception:
        since_str = str(since_idx)[:10]
    stat = (load_stats().get(kind, {}).get("by_t", {}) or {}).get(str(t))
    return {
        "t": t,
        "arrow": lv["arrow"], "label": lv["label"], "color": lv["color"], "bg": lv["bg"],
        "side": side,
        "side_label": "多方" if side == "long" else "空方",
        "side_since": since_str,
        "side_days": side_days,
        "checks": [(name, bool(last[key])) for key, name in CHECK_NAMES],
        "stat": stat,
        "text": f"{lv['arrow']} {lv['label']}",
        "close": float(last["close"]),
        "ma20": float(last["ma20"]), "ma60": float(last["ma60"]), "ma120": float(last["ma120"]),
        "data_date": str(df.index[-1])[:10],
    }


def stat_sentence(state: dict, kind: str = "stock") -> str:
    """把歷史統計講成一句白話（給橫幅／推播用）。沒有統計就回空字串。"""
    s = state.get("stat")
    if not s:
        return ""
    if kind == "index":
        return (f"歷史上大盤在這個狀態，之後 20 個交易日上漲機率 {s['up20']:.0f}%、"
                f"平均 {s['f20']:+.1f}%、最差 5% 情境 {s['p5_20']:+.1f}%")
    return (f"歷史上個股在這個狀態，之後 20 日平均比同期全體個股 {s['x20']:+.1f}%"
            f"（60 日 {s['x60']:+.1f}%）")
