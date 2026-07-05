"""盤中即時警示 — 每 5 分鐘執行（9:00-13:35）

警示條件（觸發即彈 Windows Toast）：
  1. 加權指數單日跌幅 ≤ -2%
  2. TAIEX 跌破 5 日均線
  3. TAIEX 跌破 10 日均線
  4. 美股 NQ 期貨夜盤 ≤ -2%
  5. 持股盤中跌破停損

同一警示一天只跳一次（避免疲勞轟炸）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

# pythonw 排程可能沒 stdout
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

os.chdir(Path(__file__).resolve().parent)

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import pandas as pd
import requests
import yfinance as yf

PORTFOLIO_FILE = Path("portfolio.csv")
STATE_FILE = Path("data/intraday_state.json")
LOG_FILE = Path("data/intraday_alert.log")
STATE_FILE.parent.mkdir(exist_ok=True)


def is_trading_hours() -> bool:
    """週一-五 09:00-13:35 為盤中"""
    n = datetime.now()
    if n.weekday() >= 5:
        return False
    return time(9, 0) <= n.time() <= time(13, 35)


def load_state() -> dict:
    """載入今日已警示記錄"""
    if not STATE_FILE.exists():
        return {"date": "", "alerted": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"date": "", "alerted": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def twse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    })
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    except Exception:
        pass
    return s


def fetch_taiex_live(session: requests.Session) -> dict | None:
    """TWSE MIS - 加權指數即時"""
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0"
    try:
        r = session.get(url, timeout=8)
        data = r.json().get("msgArray", [])
        if not data:
            return None
        d = data[0]
        last = float(d.get("z") or 0)
        y = float(d.get("y") or 0)
        if not last or not y:
            return None
        return {"last": last, "chg_pct": (last - y) / y * 100, "yclose": y}
    except Exception:
        return None


def compute_taiex_ma() -> dict | None:
    """計算 TAIEX 5MA / 10MA（從昨日往前數）"""
    try:
        h = yf.Ticker("^TWII").history(period="30d", auto_adjust=False).dropna(subset=["Close"])
        if len(h) < 10:
            return None
        # 不含今日（用最近 5/10 個歷史日）
        closes = h["Close"].iloc[-10:].tolist()
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes) / 10
        return {"ma5": float(ma5), "ma10": float(ma10)}
    except Exception:
        return None


def fetch_nq_futures() -> dict | None:
    """Nasdaq 期貨即時"""
    try:
        h = yf.Ticker("NQ=F").history(period="5d", auto_adjust=False).dropna(subset=["Close"])
        if len(h) < 2:
            return None
        last = float(h["Close"].iloc[-1])
        prev = float(h["Close"].iloc[-2])
        return {"last": last, "chg_pct": (last - prev) / prev * 100}
    except Exception:
        return None


def fetch_portfolio_prices(session: requests.Session) -> list[dict]:
    """讀持倉 + 抓即時報價，並套用 Trailing Stop（取較高的停損）"""
    if not PORTFOLIO_FILE.exists():
        return []
    df = pd.read_csv(PORTFOLIO_FILE, dtype={"code": str})
    codes = df["code"].tolist()
    if not codes:
        return []
    # 分塊抓
    quotes = {}
    for i in range(0, len(codes), 40):
        sub = codes[i: i + 40]
        ex_ch = "|".join([f"tse_{c}.tw" for c in sub] + [f"otc_{c}.tw" for c in sub])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        try:
            r = session.get(url, timeout=10)
            for d in r.json().get("msgArray", []):
                code = d.get("c")
                if not code or code in quotes:
                    continue
                try:
                    z = d.get("z")
                    px = float(z) if z not in (None, "-", "") else float(d.get("y") or 0)
                    h = d.get("h")
                    today_high = float(h) if h and h not in ("-", "") else px
                    if px > 0:
                        quotes[code] = {"price": px, "today_high": today_high}
                except Exception:
                    continue
        except Exception:
            continue

    # 載入 trailing state 並更新 peak
    try:
        from trailing_stop import compute_trail_stop, effective_stop, load_state, save_state
        state = load_state()
    except Exception:
        state = {}
        compute_trail_stop = effective_stop = save_state = None

    today_iso = datetime.now().strftime("%Y-%m-%d")
    out = []
    for _, r in df.iterrows():
        code = r["code"]
        q = quotes.get(code)
        if not q:
            continue
        px = q["price"]
        today_high = q["today_high"]
        entry = float(r["cost"])
        sl_str = str(r.get("stop_loss", ""))
        try:
            fixed_sl = float(sl_str)
        except ValueError:
            fixed_sl = 0.0

        # 更新 peak
        if compute_trail_stop and state is not None:
            if code not in state:
                state[code] = {"entry": entry, "peak": max(entry, today_high),
                               "peak_date": today_iso, "first_seen": today_iso}
            elif today_high > state[code]["peak"]:
                state[code]["peak"] = today_high
                state[code]["peak_date"] = today_iso

            trail_stop, tier = compute_trail_stop(entry, state[code]["peak"])
            eff_stop = effective_stop(fixed_sl, trail_stop)
            out.append({"code": code, "name": r["name"], "price": px,
                        "stop": eff_stop, "tier": tier,
                        "fixed_stop": fixed_sl, "trail_stop": trail_stop})
        else:
            out.append({"code": code, "name": r["name"], "price": px, "stop": fixed_sl,
                        "tier": "未啟動", "fixed_stop": fixed_sl, "trail_stop": 0.0})

    # 寫回 state
    if save_state and state:
        try:
            save_state(state)
        except Exception:
            pass
    return out


def check_conditions(
    taiex: dict | None,
    taiex_ma: dict | None,
    nq: dict | None,
    stocks: list[dict],
) -> list[tuple[str, str, str]]:
    """回傳 [(alert_key, level, message)]"""
    alerts = []
    if taiex:
        if taiex["chg_pct"] <= -2:
            alerts.append((
                f"taiex_drop_{int(abs(taiex['chg_pct']))}",
                "🔴", f"加權指數 {taiex['chg_pct']:+.2f}% 大跌（收 {taiex['last']:,.0f}）"
            ))
        if taiex_ma:
            if taiex["last"] < taiex_ma["ma5"]:
                alerts.append((
                    "taiex_break_5ma", "🟡",
                    f"TAIEX 跌破 5MA（現 {taiex['last']:,.0f} < 5MA {taiex_ma['ma5']:,.0f}）"
                ))
            if taiex["last"] < taiex_ma["ma10"]:
                alerts.append((
                    "taiex_break_10ma", "🔴",
                    f"TAIEX 跌破 10MA（現 {taiex['last']:,.0f} < 10MA {taiex_ma['ma10']:,.0f}）— 短線轉弱"
                ))
    if nq and nq["chg_pct"] <= -2:
        alerts.append((
            "nq_drop", "🔴",
            f"Nasdaq 期貨 {nq['chg_pct']:+.2f}%（{nq['last']:,.0f}）— 美股弱勢"
        ))
    for s in stocks:
        dist_pct = (s["price"] - s["stop"]) / s["stop"] * 100
        tier = s.get("tier", "未啟動")
        tier_label = f"［{tier}］" if tier != "未啟動" else ""
        if s["price"] <= s["stop"]:
            # 已跌破
            alerts.append((
                f"stop_{s['code']}", "🔴",
                f"⛔ {s['name']} {s['code']} 跌破停損 {s['stop']:.2f}{tier_label}（現 {s['price']:.2f}）"
            ))
        elif dist_pct < 1.0:
            # 距停損 < 1% 緊急預警（紅燈）
            alerts.append((
                f"near_stop_red_{s['code']}", "🔴",
                f"🔴 緊急：{s['name']} {s['code']} 距停損 {dist_pct:+.2f}%{tier_label}\n停損 {s['stop']:.2f}（現 {s['price']:.2f}）— 隨時可能觸發"
            ))
        elif dist_pct < 3.0:
            # 距停損 < 3% 橘燈
            alerts.append((
                f"near_stop_orange_{s['code']}", "🟠",
                f"🟠 注意：{s['name']} {s['code']} 距停損 {dist_pct:+.2f}%{tier_label}\n停損 {s['stop']:.2f}（現 {s['price']:.2f}）— 開始準備"
            ))
        elif dist_pct < 5.0:
            # 距停損 < 5% 黃燈（早期預警）
            alerts.append((
                f"near_stop_yellow_{s['code']}", "🟡",
                f"🟡 預警：{s['name']} {s['code']} 距停損 {dist_pct:+.2f}%{tier_label}\n停損 {s['stop']:.2f}（現 {s['price']:.2f}）— 留意走勢"
            ))
    return alerts


def send_toast(title: str, msg: str) -> None:
    """同時送 Windows Toast + Telegram"""
    # Windows Toast
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="盤中警示", title=title, msg=msg, duration="long",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass
    # Telegram 推播
    try:
        import telegram_notify
        telegram_notify.send(title, msg)
    except Exception:
        pass


def main():
    if not is_trading_hours():
        return

    today = date.today().isoformat()
    state = load_state()
    if state.get("date") != today:
        state = {"date": today, "alerted": []}

    session = twse_session()
    taiex = fetch_taiex_live(session)
    taiex_ma = compute_taiex_ma()
    nq = fetch_nq_futures()
    stocks = fetch_portfolio_prices(session)

    alerts = check_conditions(taiex, taiex_ma, nq, stocks)

    new_alerts = []
    for key, level, msg in alerts:
        if key in state["alerted"]:
            continue
        send_toast(f"{level} 盤中警示", msg)
        log(msg)
        state["alerted"].append(key)
        new_alerts.append(msg)

    save_state(state)

    # 限制 log 大小
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            LOG_FILE.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
