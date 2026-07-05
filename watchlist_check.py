"""觀察名單每日檢查 — 若觸發條件達成推 Telegram

每日 14:35 排程跑：
  1. 讀 data/watchlist.json
  2. 逐檔重算評分/EV/勝率/過熱
  3. 檢查觸發條件
  4. 觸發 → Telegram 推播「觀察名單觸發：XXX」
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

os.chdir(Path(__file__).resolve().parent)

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from stock_hist_smart import fetch_hist, fetch_volume_reliable
from analysis import smart_money_score
from institutional import analyze as analyze_instit, fetch_institutional_history
from backtest import backtest_stock, aggregate_stats


def check_watchlist():
    wl_file = Path("data/watchlist.json")
    if not wl_file.exists():
        return

    wl = json.loads(wl_file.read_text(encoding="utf-8"))
    stocks = {k: v for k, v in wl.items() if not k.startswith("_")}
    if not stocks:
        return

    # 抓法人
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0",
                      "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    inst_all, _ = fetch_institutional_history(s, days=10)

    triggered = []
    for code, info in stocks.items():
        hist = fetch_hist(code)
        if hist is None or len(hist) < 60:
            continue

        nets = inst_all.get(code, [])
        inst_info = analyze_instit(nets) if nets else None
        sc = smart_money_score(hist, inst_info)
        last = float(hist["Close"].iloc[-1])
        ma60 = float(hist["Close"].tail(60).mean())
        ret30 = (last - float(hist["Close"].iloc[-30])) / float(hist["Close"].iloc[-30]) * 100

        # 抓法人連續買超天數
        cons = 0
        if nets:
            for n in reversed(nets):
                if n > 0:
                    cons += 1
                else:
                    break

        # 檢查任一觸發條件
        score_ok = sc["score"] >= 3 and cons >= 3
        # ret30 < 20
        cooldown_ok = ret30 <= 20

        # 記錄狀態
        status = {
            "code": code, "name": info.get("name", code),
            "score": sc["score"], "ret30": ret30,
            "cons_buy": cons, "current": last,
        }

        if score_ok:
            triggered.append({**status, "trigger": "評分 ≥ +3 且法人連 3 日買超"})
        elif cooldown_ok:
            triggered.append({**status, "trigger": "30 日報酬回落到 +20% 以下"})

        print(f"  {code} {info.get('name','')} 評分{sc['score']:+d} 30日{ret30:+.1f}% 連續買{cons}日")

    if triggered:
        lines = ["📊 觀察名單觸發", ""]
        for t in triggered:
            lines.append(f"⭐ {t['code']} {t['name']}")
            lines.append(f"  現價 {t['current']:.2f} ｜ 評分 {t['score']:+d} ｜ 30日 {t['ret30']:+.1f}%")
            lines.append(f"  觸發：{t['trigger']}")
            lines.append("")
        body = "\n".join(lines) + "→ 檢查是否可加入推薦榜"
        try:
            import telegram_notify
            telegram_notify.send("📊 觀察名單觸發", body)
            print(f"✅ Telegram 推播 {len(triggered)} 檔觸發")
        except Exception as e:
            print(f"⚠️ Telegram 失敗: {e}")
    else:
        print("觀察名單無觸發")


if __name__ == "__main__":
    check_watchlist()
