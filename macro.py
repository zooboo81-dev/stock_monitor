"""總經風險儀表板：VIX（恐慌指數）+ SOX（費半）+ DXY（美元指數）

資料源：yfinance（皆為美股盤後資料，台股開盤前最關鍵的領先指標）
"""
from __future__ import annotations

import yfinance as yf

MACRO_SPEC = {
    "VIX": {
        "symbol": "^VIX",
        "name": "VIX 恐慌指數",
        "warn_high": 25.0,
        "panic_high": 35.0,
        "calm_low": 15.0,
        "desc": "S&P 500 隱含波動率，外資避險情緒",
    },
    "SOX": {
        "symbol": "^SOX",
        "name": "費半指數",
        "warn_chg": -3.0,
        "panic_chg": -5.0,
        "bull_chg": 2.0,
        "desc": "費城半導體指數，台灣半導體股最直接領先",
    },
    "DXY": {
        "symbol": "DX-Y.NYB",
        "name": "美元指數",
        "warn_high": 105.0,
        "panic_high": 110.0,
        "desc": "美元兌六大貨幣，強勢美元→外資匯回壓力",
    },
}

# 夜盤指標（美股期貨幾乎 24 小時交易，反映歐美盤對台股隔天開盤的預期）
NIGHT_SPEC = {
    "ES": {
        "symbol": "ES=F",
        "name": "S&P 500 期",
        "warn_chg": -1.5,
        "panic_chg": -3.0,
        "bull_chg": 1.0,
        "desc": "S&P 500 E-mini 期貨，美股大盤夜盤即時",
    },
    "NQ": {
        "symbol": "NQ=F",
        "name": "Nasdaq 期",
        "warn_chg": -1.5,
        "panic_chg": -3.0,
        "bull_chg": 1.0,
        "desc": "Nasdaq 100 期貨，AI/科技股夜盤即時",
    },
    "TSM": {
        "symbol": "TSM",
        "name": "台積電 ADR",
        "warn_chg": -2.0,
        "panic_chg": -4.0,
        "bull_chg": 2.0,
        "desc": "台積電美國存託憑證，台股隔天開盤先行指標",
    },
}


def _fetch_one(key: str, cfg: dict) -> dict | None:
    try:
        t = yf.Ticker(cfg["symbol"])
        h = t.history(period="5d", auto_adjust=False).dropna(subset=["Close"])
        if len(h) < 2:
            return None
        last = float(h["Close"].iloc[-1])
        prev = float(h["Close"].iloc[-2])
        chg = (last - prev) / prev * 100 if prev else 0.0

        # 等級判定
        level = "normal"
        if key == "VIX":
            if last >= cfg["panic_high"]:
                level = "panic"
            elif last >= cfg["warn_high"]:
                level = "warn"
            elif last <= cfg["calm_low"]:
                level = "calm"
        elif key == "DXY":
            if last >= cfg["panic_high"]:
                level = "panic"
            elif last >= cfg["warn_high"]:
                level = "warn"
        else:  # 漲跌型（SOX / ES / NQ / TSM）
            if chg <= cfg["panic_chg"]:
                level = "panic"
            elif chg <= cfg["warn_chg"]:
                level = "warn"
            elif chg >= cfg["bull_chg"]:
                level = "bull"

        return {
            "last": last, "chg_pct": chg, "level": level,
            "name": cfg["name"], "desc": cfg["desc"], "symbol": cfg["symbol"],
        }
    except Exception:
        return None


def fetch_macro() -> dict:
    """總經指標（美股 + 美元）"""
    return {k: r for k, cfg in MACRO_SPEC.items()
            if (r := _fetch_one(k, cfg)) is not None}


def fetch_night() -> dict:
    """夜盤指標（美股期貨 + 台積電 ADR）"""
    return {k: r for k, cfg in NIGHT_SPEC.items()
            if (r := _fetch_one(k, cfg)) is not None}


def detect_market_regime(taiex_hist) -> dict:
    """市場狀態判斷 —— 2026-11-11 改用 trend.py 的趨勢結構（跟箭頭同一個來源）

    舊版只看「收盤 vs 200MA ±2%」，問題：
      - 2026/10/29~11/4 大盤從高點跌 14%，它還顯示「🟢 多頭模式、訊號可信」
        （同時間另一個橫幅說「跌破 60MA 中期空頭」，兩個打架）
      - 跌破 200MA 才叫熊市，通常已經跌 15~20%，來不及

    新版（回測 2003~2026，data/trend_stats.json）：
      bear    = 空方（T<=1 起算，回到 T>=3 才解除）
                之後 20 日平均 +0.3%（多方 +1.1%）、最差 5% 情境 -11.9%（多方 -6.3%）
                過去「20 日內跌超過 10%」的日子有 55% 發生在空方
      warning = 多方但結構不完整（T<=3）
      bull    = 多方且 T>=4
    回傳鍵維持舊版相容（regime/emoji/label/last/ma200/deviation/advice/banner_type），
    另外多給 trend（完整趨勢狀態）。
    """
    from trend import stat_sentence, trend_state

    if taiex_hist is None or len(taiex_hist) < 141:
        return {"regime": "unknown", "emoji": "⚪", "label": "資料不足"}
    h = taiex_hist.dropna(subset=["Close"])
    st = trend_state(h, "index")
    if st.get("t") is None:
        return {"regime": "unknown", "emoji": "⚪", "label": "資料不足"}

    last = float(h["Close"].iloc[-1])
    ma200 = float(h["Close"].rolling(200).mean().iloc[-1]) if len(h) >= 200 else float("nan")
    deviation = (last - ma200) / ma200 * 100 if ma200 == ma200 else 0.0
    base = {"last": last, "ma200": ma200, "deviation": deviation, "trend": st,
            "arrow": st["arrow"], "trend_label": st["label"], "stat_text": stat_sentence(st, "index")}

    if st["side"] == "short":
        return {**base,
                "regime": "bear", "emoji": "🔴",
                "label": f"{st['arrow']} 大盤{st['label']}（空方）",
                "advice": (f"{st['side_since']} 起轉空方：不加碼個股、不照推薦榜進場、既有部位守停損。"
                           f"站回 20MA＋60MA 之上（T≥3）才解除"),
                "banner_type": "error"}
    if st["t"] <= 3:
        return {**base,
                "regime": "warning", "emoji": "🟡",
                "label": f"{st['arrow']} 大盤{st['label']}（多方但結構轉弱）",
                "advice": "趨勢結構不完整，新部位**減半**、只挑個股本身是 ⬆ 多頭的",
                "banner_type": "warning"}
    return {**base,
            "regime": "bull", "emoji": "🟢",
            "label": f"{st['arrow']} 大盤{st['label']}（多方）",
            "advice": "順勢環境，可參考推薦榜（個股仍要設停損）",
            "banner_type": None}


def macro_risk_score(macro: dict) -> tuple[int, list[tuple[str, str, str]]]:
    """總經風險評分（-5 ~ +5；負數 = 風險升高、應減碼，正數 = 順風、可加碼）。

    回傳: (score, signals: [(emoji, name, txt), ...])
    """
    score = 0
    notes: list[tuple[str, str, str]] = []

    # VIX
    if "VIX" in macro:
        v = macro["VIX"]["last"]
        if macro["VIX"]["level"] == "panic":
            score -= 3
            notes.append(("🔴", "總經", f"VIX {v:.1f} 恐慌（外資避險）"))
        elif macro["VIX"]["level"] == "warn":
            score -= 2
            notes.append(("🔴", "總經", f"VIX {v:.1f} 警戒"))
        elif macro["VIX"]["level"] == "calm":
            score += 1
            notes.append(("🟢", "總經", f"VIX {v:.1f} 平靜（風險偏好高）"))

    # SOX (對你的半導體部位最關鍵)
    if "SOX" in macro:
        c = macro["SOX"]["chg_pct"]
        if macro["SOX"]["level"] == "panic":
            score -= 3
            notes.append(("🔴", "總經", f"費半 {c:+.2f}% 重挫（台股半導體看空）"))
        elif macro["SOX"]["level"] == "warn":
            score -= 2
            notes.append(("🔴", "總經", f"費半 {c:+.2f}% 下跌"))
        elif macro["SOX"]["level"] == "bull":
            score += 2
            notes.append(("🟢", "總經", f"費半 {c:+.2f}% 強漲（台股半導體看多）"))

    # DXY
    if "DXY" in macro:
        v = macro["DXY"]["last"]
        if macro["DXY"]["level"] == "panic":
            score -= 2
            notes.append(("🔴", "總經", f"美元 {v:.1f} 過強（外資匯回壓力）"))
        elif macro["DXY"]["level"] == "warn":
            score -= 1
            notes.append(("🟡", "總經", f"美元 {v:.1f} 偏強"))

    # 夜盤指標（如果傳入）
    for k in ("ES", "NQ", "TSM"):
        if k not in macro:
            continue
        d = macro[k]
        c = d["chg_pct"]
        if d["level"] == "panic":
            score -= 2
            notes.append(("🔴", "夜盤", f"{d['name']} {c:+.2f}%（隔天看空）"))
        elif d["level"] == "warn":
            score -= 1
            notes.append(("🔴", "夜盤", f"{d['name']} {c:+.2f}%"))
        elif d["level"] == "bull":
            score += 1
            notes.append(("🟢", "夜盤", f"{d['name']} {c:+.2f}%（隔天看多）"))

    return max(-5, min(5, score)), notes


def macro_verdict(score: int) -> tuple[str, str]:
    """總經風險文字標籤"""
    if score >= 3:
        return "🟢", "順風（外部環境有利）"
    if score >= 1:
        return "🟢", "偏正面"
    if score >= -1:
        return "⚪", "中性"
    if score >= -3:
        return "🔴", "偏負面（謹慎）"
    return "🔴", "高風險（建議減碼）"
