"""每日選股：從三大法人買超榜挑出潛力加碼標的（排除已持有）。

流程：
  0. 大盤閘門：大盤在「空方」（trend.py）→ 不推薦，直接回空榜
  1. 候選池 = 法人 5 日累計買超的個股，按金額取前 200
  2. 過濾：ETF、權證、字母代號、金融、流動性不足
  3. 批次抓 yfinance 2 年歷史，再用交易所官方收盤補上 yfinance 漏掉的最新一根
  4. 個股趨勢必須是 ⬆ 多頭（T=5）
  5. 跑 smart_money_score（評分 ≥ 3）、過熱保護、2 年回測期望值過濾
  6. 取前 N

2026-11-11 依回測改的三件事（流動性前 300 大 × 2020~2026，20 日後表現）：
  - 加大盤閘門：舊規則在大盤空方時平均 -1.9%、上漲率 40%；多方時 +1.8%
  - 加「個股 T=5」：評分 ≥3 但 T≤4 的，之後 20 日輸同期個股 0.4~1.5%
  - 拿掉「距 60 日高 3% 內就排除」：那一群其實是表現最好、尾部風險最小的（超額 +0.6~0.9%）
  舊規則 → 新規則：20 日平均 +1.27% → +2.50%、上漲率 44.8% → 49.5%、超額 -0.21% → +0.86%
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from analysis import smart_money_score
from institutional import analyze as analyze_instit
from market_data import patch_latest_bars
from trend import trend_state

# 最近一次 top_picks 的執行資訊（給排程／儀表板顯示「為什麼是空榜」「資料到哪一天」）
LAST_RUN: dict = {}


def market_trend(period: str = "2y") -> dict:
    """大盤（加權指數）趨勢狀態。抓不到資料時回 side=unknown（閘門視為放行，不因抓取失敗而全擋）。"""
    try:
        h = yf.Ticker("^TWII").history(period=period, auto_adjust=False).dropna(subset=["Close"])
        from market_data import patch_index_bar
        return trend_state(patch_index_bar(h), "index")
    except Exception:
        return trend_state(None)

COOLDOWN_FILE = Path(__file__).parent / "cooldown.json"


def get_cooldown_codes() -> set[str]:
    """讀取近期賣出冷卻清單，回傳尚在冷卻期內的代號"""
    if not COOLDOWN_FILE.exists():
        return set()
    try:
        data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    today = date.today()
    on_cooldown: set[str] = set()
    for code, info in data.items():
        if code.startswith("_"):  # 跳過 _comment 等
            continue
        try:
            sold = datetime.strptime(info["sold_date"], "%Y-%m-%d").date()
            days = int(info.get("cooldown_days", 30))
            if today < sold + timedelta(days=days):
                on_cooldown.add(code)
        except (KeyError, ValueError, TypeError):
            continue
    return on_cooldown


# 低波動防禦型 — 系統訊號對這類股無意義（波動小、靠殖利率，沒有「30% 短線」的可能）
EXCLUDED_FINANCIALS: set[str] = {
    # 銀行
    "2801", "2809", "2812", "2820", "2834", "2836", "2838", "2845",
    "2849", "2855", "2897", "5820", "5876", "5880",
    # 金控
    "2880", "2881", "2882", "2883", "2884", "2885", "2886", "2887",
    "2888", "2889", "2890", "2891", "2892",
    # 保險
    "2823", "2832", "2850", "2851", "2852", "2867",
    # 證券 / 票券
    "2855", "6005",
    # 電信
    "2412", "3045", "4904",
}


def is_normal_stock(code: str) -> bool:
    """只留標準個股（4 位數，不是 ETF/權證/借券/金融）"""
    if not code or len(code) != 4 or not code.isdigit():
        return False
    if code.startswith("00"):  # ETF
        return False
    if code in EXCLUDED_FINANCIALS:  # 金融/保險/證券
        return False
    return True


def get_candidate_pool(
    instit_by_code: dict[str, list[int]],
    exclude: set[str] | None = None,
    top_n: int = 50,
    min_buy_lots: int = 500,
) -> list[str]:
    """從法人買超榜挑前 top_n 名（用 5 日累計買超排序）"""
    exclude = exclude or set()
    ranked: list[tuple[str, int]] = []
    for code, history in instit_by_code.items():
        if code in exclude or not is_normal_stock(code) or not history:
            continue
        recent_sum = sum(history[-5:])
        if recent_sum < min_buy_lots:
            continue
        ranked.append((code, recent_sum))
    ranked.sort(key=lambda x: -x[1])
    return [c for c, _ in ranked[:top_n]]


def _batch_fetch(codes: list[str], suffix: str, period: str = "6mo") -> dict[str, pd.DataFrame]:
    """一次抓多檔，用 yfinance 多執行緒"""
    if not codes:
        return {}
    tickers = [f"{c}.{suffix}" for c in codes]
    try:
        data = yf.download(
            tickers, period=period, auto_adjust=False,
            progress=False, group_by="ticker", threads=True,
        )
    except Exception:
        return {}
    out: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return out
    for c in codes:
        tk = f"{c}.{suffix}"
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if tk in data.columns.get_level_values(0):
                    df = data[tk].dropna(subset=["Close"])
                    if len(df) >= 30:
                        out[c] = df
            else:
                # 只有一檔時，data 是單層欄
                df = data.dropna(subset=["Close"])
                if len(df) >= 30:
                    out[c] = df
        except Exception:
            continue
    return out


def batch_fetch_history(codes: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """批次抓歷史（先試 .TW 上市，剩下的試 .TWO 上櫃）
    預設 2y 給回測用，scoring 只用尾段 6mo
    """
    out = _batch_fetch(codes, "TW", period=period)
    missing = [c for c in codes if c not in out]
    if missing:
        out.update(_batch_fetch(missing, "TWO", period=period))
    return out


def compute_stock_edge(code: str, hist: pd.DataFrame) -> dict:
    """計算個股 2 年回測期望值（含勝率、樣本數）"""
    from backtest import aggregate_stats, backtest_stock
    trades = backtest_stock(code, hist, signal_threshold=3, hold_days=10)
    return aggregate_stats(trades)


def fetch_stock_names(codes: list[str], session: requests.Session, chunk: int = 40) -> dict[str, str]:
    """用 TWSE MIS API 批次查名稱（分塊避免 URL 過長被拒）"""
    out: dict[str, str] = {}
    for i in range(0, len(codes), chunk):
        sub = codes[i: i + chunk]
        ex_ch = "|".join([f"tse_{c}.tw" for c in sub] + [f"otc_{c}.tw" for c in sub])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        try:
            r = session.get(url, timeout=10)
            for d in r.json().get("msgArray", []):
                code = d.get("c"); name = d.get("n", "")
                if code and name and code not in out:
                    out[code] = name
        except Exception:
            continue
    return out


def fetch_quotes_batch(codes: list[str], session: requests.Session, chunk: int = 50) -> dict[str, float]:
    """批次抓現價，自動分塊（URL 過長 API 會拒絕）"""
    out: dict[str, float] = {}
    for i in range(0, len(codes), chunk):
        sub = codes[i: i + chunk]
        ex_ch = "|".join([f"tse_{c}.tw" for c in sub] + [f"otc_{c}.tw" for c in sub])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        try:
            r = session.get(url, timeout=10)
            data = r.json()
        except Exception:
            continue
        for d in data.get("msgArray", []):
            code = d.get("c")
            if not code or code in out:
                continue
            z = d.get("z"); y = d.get("y")
            try:
                px = float(z) if z not in (None, "-", "") else float(y or 0)
            except (ValueError, TypeError):
                continue
            if px > 0:
                out[code] = px
    return out


def top_picks(
    portfolio_codes: set[str],
    instit_by_code: dict[str, list[int]],
    session: requests.Session,
    top_n: int = 5,
    min_score: int = 3,
    max_chg_pct: float = 9.5,  # 放寬：漲停（~9.97%）才排除，避免錯過強勢但未漲停的標的
    market_gate: bool = True,  # 11/11：大盤空方不推薦
    market_state: dict | None = None,
) -> list[dict]:
    """主入口：回傳 top_n 個推薦標的

    篩選邏輯（2 階段排序避免低價股 bias）:
      1. 先用法人 5 日累計買「張」粗篩前 150（門檻 100 張）
      2. 抓現價，按「金額」(張×價) 重排取前 50
      3. 跑 smart_money_score，篩 >= min_score
    """
    LAST_RUN.clear()
    mkt = market_state if market_state is not None else market_trend()
    LAST_RUN.update({"market": mkt, "gate_blocked": False, "data_date": None,
                     "patched": 0, "n_scored": 0, "n_trend_ok": 0})
    if market_gate and mkt.get("side") == "short":
        LAST_RUN["gate_blocked"] = True
        return []

    # 把冷卻期內的代號也加入排除（避免賣完又被推回）
    full_exclude = set(portfolio_codes) | get_cooldown_codes()
    rough = get_candidate_pool(
        instit_by_code, exclude=full_exclude, top_n=200, min_buy_lots=80,
    )
    if not rough:
        return []

    # 用金額重排（讓高單價但金額大的個股也能浮上來）
    prices = fetch_quotes_batch(rough, session)
    amount_ranked = []
    for code in rough:
        px = prices.get(code)
        if not px:
            continue
        lots = sum(instit_by_code[code][-5:])
        amount_ranked.append((code, lots * 1000 * px))
    amount_ranked.sort(key=lambda x: -x[1])
    # 6/23 修正：從 [:100] → [:200] 擴大候選範圍
    # 之前晶豪科 3006 等中型股因排在 100 後被排除
    # 排程 14:35 跑，慢一點沒關係，覆蓋率優先
    candidates = [c for c, _ in amount_ranked[:200]]
    if not candidates:
        return []

    history_map = batch_fetch_history(candidates, period="2y")
    if not history_map:
        return []
    # ★ 11/11：yfinance 常缺當天的日 K（雲端 10 月有 2/3 的推薦是拿前一天收盤算的）→ 用官方收盤補
    try:
        history_map, latest_td, n_patched = patch_latest_bars(history_map, session)
    except Exception:
        latest_td, n_patched = None, 0
    LAST_RUN["data_date"] = latest_td.isoformat() if latest_td else None
    LAST_RUN["patched"] = n_patched

    names = fetch_stock_names(list(history_map.keys()), session)

    results: list[dict] = []
    for code, hist in history_map.items():
        try:
            # 最新一根不是最新交易日（停牌、補不到）→ 不用過期資料算分
            if latest_td is not None and hist.index[-1].date() < latest_td:
                continue
            instit_info = analyze_instit(instit_by_code.get(code, []))
            score_data = smart_money_score(hist, instit_info)
            LAST_RUN["n_scored"] += 1
            # ★ 11/11：個股必須是多頭結構（T=5）才推
            if (score_data.get("trend") or {}).get("t") != 5:
                continue
            LAST_RUN["n_trend_ok"] += 1
            if score_data["score"] < min_score:
                continue

            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_close
            chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
            if chg_pct > max_chg_pct:
                continue
            if last_close < 10:
                continue

            # ★ 過熱保護（6/12 正達 3149 教訓 — 系統推 +3 但已過熱 +50% on 60MA）
            # 規則 #1：距 60MA > +30% 排除（回測：平均報酬不差，但最差 5% 情境 -25%~-32%）
            # 規則 #2：30 日報酬 > +40% 排除（同上，尾部風險大）
            # （11/11 拿掉舊規則 #3「距 60 日高 3% 內排除」：回測那群反而最好）
            if len(hist) >= 60:
                ma60 = float(hist["Close"].tail(60).mean())
                dist_ma60_pct = (last_close - ma60) / ma60 * 100
                if dist_ma60_pct > 30:
                    continue
            if len(hist) >= 30:
                price_30d_ago = float(hist["Close"].iloc[-30])
                ret_30d_pct = (last_close - price_30d_ago) / price_30d_ago * 100
                if ret_30d_pct > 40:
                    continue

            # ★ 流動性過濾（6/23 洋基 6691 教訓 — 量 < 400 張砍倉風險高）
            # 規則：20 日均量 < 1,000 張 或 5 日均量 < 800 張 排除
            # （避開低成交量股，砍倉時可能砍不掉）
            # 「量」單位：yfinance 給的是「股」→ 除 1000 換成「張」
            if len(hist) >= 20:
                vol_20d_lots = float(hist["Volume"].tail(20).mean()) / 1000
                if vol_20d_lots < 1000:
                    continue
            if len(hist) >= 5:
                vol_5d_lots = float(hist["Volume"].tail(5).mean()) / 1000
                if vol_5d_lots < 800:
                    continue
            # 同時看「20 日成交金額」≥ 1 億（避免低價股看似量大但金額小）
            if len(hist) >= 20:
                amount_20d = float((hist["Close"].tail(20) * hist["Volume"].tail(20)).mean())
                if amount_20d < 100_000_000:  # 1 億
                    continue

            # ★ 個股 2 年回測 —— 11/11 起「只顯示、不當門檻」
            # 原本：樣本 <15 或 EV/勝率不達標就剔除。拿掉的理由：
            #   1. 實盤 135 筆推薦：回測 EV 越高的，之後 20 日反而越差（等級相關 -0.16）
            #      → 這是「拿同一段歷史挑贏家」的典型後見之明，沒有預測力
            #   2. 新規則的回測（trend_backtest.py）本來就沒有這道門檻；正式規則要跟驗證過的規則一致
            #   3. KD 等三項不計分後，單檔 2 年內達 3 分的次數變少，樣本門檻會把榜單清空
            edge_stats = compute_stock_edge(code, hist)
            edge_ev = edge_stats.get("expectancy", 0.0)
            edge_n = edge_stats.get("total_trades", 0)
            edge_wr = edge_stats.get("win_rate", 0.0)

            # 距 60 日高點（越近越好：回測 T=5 且距高 3% 內，20 日超額 +0.9~1.2%、尾部風險最小）
            dist_high_60d = None
            if len(hist) >= 60:
                dist_high_60d = (last_close / float(hist["Close"].tail(60).max()) - 1) * 100

            # ★ ATR14 動態停損（7/06 升級：規格書優化 A）
            # ATR = True Range 14 日平均，用來算個股波動幅度
            atr14 = None
            if len(hist) >= 15 and all(col in hist.columns for col in ("High", "Low", "Close")):
                try:
                    high = hist["High"]
                    low = hist["Low"]
                    close_shift = hist["Close"].shift(1)
                    tr = pd.concat([
                        high - low,
                        (high - close_shift).abs(),
                        (low - close_shift).abs(),
                    ], axis=1).max(axis=1)
                    atr_val = float(tr.tail(14).mean())
                    if atr_val > 0:
                        atr14 = atr_val
                except Exception:
                    pass

            top_reasons = sorted(score_data["breakdown"], key=lambda x: -x[1])[:3]
            results.append({
                "code": code,
                "name": names.get(code, code),
                "score": score_data["score"],
                "verdict": score_data["verdict"],
                "verdict_emoji": score_data["verdict_emoji"],
                "attention_level": score_data.get("attention_level", 0),
                "attention_reasons": score_data.get("attention_reasons", []),
                "breakdown": score_data["breakdown"],
                "top_reasons": top_reasons,
                "price": last_close,
                "chg_pct": chg_pct,
                "instit": instit_info,
                "backtest_ev": edge_ev,
                "backtest_n": edge_n,
                "backtest_wr": edge_wr,
                "atr14": atr14,
                "dist_high_60d": dist_high_60d,
                "dim_scores": score_data.get("dim_scores"),   # 7/06 優化 B
                "strategy": score_data.get("strategy"),        # 7/06 優化 C
                "trend": score_data.get("trend"),              # 11/11 趨勢箭頭
                "data_date": hist.index[-1].date().isoformat(),  # 這筆評分用到的最新一根 K 的日期
            })
        except Exception:
            continue

    # 排序（11/11）：評分高 → 離 60 日高點近（回測有效的兩個因子）
    # 舊版用「個股回測勝率／EV」排，已證實沒有預測力
    results.sort(key=lambda x: (-x["score"], -(x.get("dist_high_60d") if x.get("dist_high_60d") is not None else -99)))
    return results[:top_n]
