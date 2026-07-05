"""策略回測：把 smart_money_score 套到過去 2 年資料，算真實勝率/賺賠比/期望值。

核心問題：這套系統是真的有 edge，還是「自我感覺良好」？

回測規則：
  - 每當評分 ≥ 閾值（預設 +3）時，模擬以當日收盤價買進
  - 持有固定 N 天（5/10/20 三種）後賣出
  - 一檔最多一個部位（已持有時不再追進）
  - 扣除來回交易成本（一般 0.585%、ETF 0.385%）

回測限制（要誠實告訴用戶）：
  - 不包含三大法人因子（需要 500 天歷史資料抓取成本太高）
  - 使用日 K 收盤價買賣（實際無法買在收盤、滑點未估）
  - 過去績效不代表未來
"""
from __future__ import annotations

import pandas as pd

from analysis import smart_money_score

# 來回交易成本（買進手續費 + 賣出手續費 + 賣出證交稅）
COST_PCT_STOCK = 0.1425 * 2 + 0.3   # ≈ 0.585%
COST_PCT_ETF = 0.1425 * 2 + 0.1     # ≈ 0.385%


def _is_etf(code: str) -> bool:
    return code.startswith("00")


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _build_market_filter(taiex_hist: pd.DataFrame, target_index: pd.Index) -> pd.Series | None:
    """回傳對齊到 target_index 的 Boolean Series：當日 TAIEX 是否站上 200 日均線"""
    if taiex_hist is None or taiex_hist.empty:
        return None
    t = _strip_tz(taiex_hist.dropna(subset=["Close"]).copy())
    t["MA200"] = t["Close"].rolling(200).mean()
    aligned = t[["Close", "MA200"]].reindex(target_index, method="ffill")
    bullish = aligned["Close"] > aligned["MA200"]
    return bullish.fillna(False)


def backtest_stock(
    code: str,
    hist: pd.DataFrame,
    signal_threshold: int = 3,
    hold_days: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
    taiex_hist: pd.DataFrame | None = None,
    use_market_filter: bool = False,
    use_smart_exit: bool = False,
    stop_loss_pct: float = -5.0,
    take_profit_pct: float = 10.0,
    score_exit_threshold: int = -2,
    max_hold_days: int = 30,
) -> list[dict]:
    """單檔回測。
    若 use_market_filter=True：只在 TAIEX > 200MA（多頭）時進場
    若 use_smart_exit=True：用智能出場（停損 / 停利 / 趨勢反轉 / 最大持有）取代固定 hold_days
    """
    h = _strip_tz(hist.dropna(subset=["Close"]).copy())
    if len(h) < 70:
        return []

    market_bullish = _build_market_filter(taiex_hist, h.index) if use_market_filter else None

    cost_pct = COST_PCT_ETF if _is_etf(code) else COST_PCT_STOCK
    trades: list[dict] = []
    in_position = False
    entry_idx = -1

    start_ts = pd.Timestamp(start_date) if start_date else None
    end_ts = pd.Timestamp(end_date) if end_date else None

    for i in range(60, len(h)):
        cur_date = h.index[i]
        in_window = True
        if start_ts is not None and cur_date < start_ts:
            in_window = False
        if end_ts is not None and cur_date > end_ts:
            in_window = False
        if in_position:
            entry_price = float(h.iloc[entry_idx]["Close"])
            current_price = float(h.iloc[i]["Close"])
            days_held = i - entry_idx
            raw_ret = (current_price - entry_price) / entry_price * 100

            exit_reason = None
            if use_smart_exit:
                if raw_ret <= stop_loss_pct:
                    exit_reason = "stop_loss"
                elif raw_ret >= take_profit_pct:
                    exit_reason = "take_profit"
                elif days_held >= max_hold_days:
                    exit_reason = "time_out"
                elif days_held >= 3 and days_held % 2 == 0:
                    sliced = h.iloc[: i + 1]
                    cur_score = smart_money_score(sliced, instit=None).get("score", 0)
                    if cur_score <= score_exit_threshold:
                        exit_reason = "trend_reverse"
            else:
                if days_held >= hold_days:
                    exit_reason = "time_out"

            if exit_reason:
                final_ret = raw_ret - cost_pct
                trades.append({
                    "code": code,
                    "entry_date": h.index[entry_idx].strftime("%Y-%m-%d"),
                    "entry_price": entry_price,
                    "exit_date": cur_date.strftime("%Y-%m-%d"),
                    "exit_price": current_price,
                    "days_held": days_held,
                    "return_pct": final_ret,
                    "win": final_ret > 0,
                    "exit_reason": exit_reason,
                })
                in_position = False
        else:
            if not in_window:
                continue
            # 市場濾鏡：熊市時不進場
            if market_bullish is not None and not bool(market_bullish.iloc[i]):
                continue
            sliced = h.iloc[: i + 1]
            score = smart_money_score(sliced, instit=None).get("score", 0)
            if score >= signal_threshold:
                in_position = True
                entry_idx = i

    return trades


def aggregate_stats(trades: list[dict]) -> dict:
    """彙整一組交易的績效指標"""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "expectancy": 0.0, "profit_factor": 0.0,
            "total_sum_pct": 0.0, "median_return": 0.0, "max_drawdown_pct": 0.0,
            "best_trade": 0.0, "worst_trade": 0.0,
            "max_consecutive_losses": 0,
        }

    df = pd.DataFrame(trades).sort_values("exit_date").reset_index(drop=True)
    wins = df[df["win"]]
    losses = df[~df["win"]]
    total = len(df)
    wc = len(wins)
    lc = len(losses)
    win_rate = wc / total * 100
    avg_win = float(wins["return_pct"].mean()) if wc else 0.0
    avg_loss = float(losses["return_pct"].mean()) if lc else 0.0
    expectancy = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss
    sum_wins = float(wins["return_pct"].sum())
    sum_loss_abs = float(abs(losses["return_pct"].sum()))
    profit_factor = sum_wins / sum_loss_abs if sum_loss_abs > 0 else float("inf")

    # 報酬統計（用加總而非複利，避免「平行交易序列複利」的誤導）
    total_sum_pct = float(df["return_pct"].sum())  # 每筆同金額的總和
    median_ret = float(df["return_pct"].median())

    # 最大回撤（基於累計加總，較真實反映心理壓力）
    cum_sum = df["return_pct"].cumsum()
    rolling_max = cum_sum.cummax()
    dd = cum_sum - rolling_max
    max_dd = float(dd.min()) if len(dd) else 0.0

    # 最大連敗
    streak = 0
    max_streak = 0
    for w in df["win"]:
        if not w:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "total_trades": int(total),
        "win_count": int(wc),
        "loss_count": int(lc),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "total_sum_pct": round(total_sum_pct, 1),
        "median_return": round(median_ret, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "best_trade": round(float(df["return_pct"].max()), 2),
        "worst_trade": round(float(df["return_pct"].min()), 2),
        "max_consecutive_losses": int(max_streak),
    }


def verdict_text(stats: dict) -> tuple[str, str]:
    """根據統計給出系統可信度判斷"""
    if stats["total_trades"] == 0:
        return "⚪", "樣本不足無法判斷"
    ev = stats["expectancy"]
    pf = stats.get("profit_factor") or 0
    if ev > 1.5 and pf > 1.5:
        return "🟢🟢", "系統有明顯 edge，可信"
    if ev > 0.5 and pf > 1.2:
        return "🟢", "系統有微弱 edge，可參考"
    if ev > 0:
        return "🟡", "勉強打平，邊緣狀態"
    return "🔴", "系統無 edge，請勿照做"


def run_portfolio_backtest(
    stock_histories: dict[str, pd.DataFrame],
    signal_threshold: int = 3,
    hold_days: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
    taiex_hist: pd.DataFrame | None = None,
    use_market_filter: bool = False,
    use_smart_exit: bool = False,
    stop_loss_pct: float = -5.0,
    take_profit_pct: float = 10.0,
    score_exit_threshold: int = -2,
    max_hold_days: int = 30,
) -> tuple[list[dict], dict, dict[str, dict]]:
    """跑全部股票的回測（可指定區間 / 市場濾鏡 / 智能出場）。"""
    all_trades: list[dict] = []
    per_stock: dict[str, dict] = {}
    for code, hist in stock_histories.items():
        trades = backtest_stock(
            code, hist, signal_threshold, hold_days,
            start_date=start_date, end_date=end_date,
            taiex_hist=taiex_hist,
            use_market_filter=use_market_filter,
            use_smart_exit=use_smart_exit,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            score_exit_threshold=score_exit_threshold,
            max_hold_days=max_hold_days,
        )
        per_stock[code] = aggregate_stats(trades)
        per_stock[code]["trades"] = trades
        all_trades.extend(trades)
    overall = aggregate_stats(all_trades)
    return all_trades, overall, per_stock


def exit_reason_breakdown(trades: list[dict]) -> dict[str, dict]:
    """各出場原因的勝率和平均報酬"""
    if not trades:
        return {}
    by_reason: dict[str, list[float]] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        by_reason.setdefault(r, []).append(t["return_pct"])
    out = {}
    for r, rets in by_reason.items():
        wins = [x for x in rets if x > 0]
        out[r] = {
            "count": len(rets),
            "win_rate": len(wins) / len(rets) * 100,
            "avg_return": sum(rets) / len(rets),
        }
    return out
