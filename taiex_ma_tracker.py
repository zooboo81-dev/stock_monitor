"""TAIEX 月線/季線/年線追蹤器 — 抄底訊號

每日盤後跑：
  1. 算 TAIEX 距 20MA / 60MA / 200MA
  2. 判斷「抄底區」狀態
  3. 觸發推播

抄底訊號分級：
  🟢 抄底區       距 20MA -3% ~ -1%（歷史勝率 74%）
  🟡 已跌深       距 20MA -5% ~ -3%（仍可能反彈但風險高）
  🔴 跌破 60MA    距 60MA < 0%（中期空頭，避免進場）
  ⚪ 正常範圍     距 20MA > -1%（不在抄底區）

每天會輸出 data/taiex_ma_state.json，dashboard 跟 morning_briefing 都能讀。

用法：
  python taiex_ma_tracker.py        # 算一次 + 推播（若狀態改變）
  python taiex_ma_tracker.py --quiet # 只算不推播
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# 桌機 pythonw 排程沒 stdout；雲端 Linux 有，這段仍安全
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

# 雲端跑時，工作目錄可能不是這裡 → 切到腳本所在目錄
os.chdir(Path(__file__).resolve().parent)

# truststore 是桌機用（Windows 憑證），雲端 Linux 不用
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import yfinance as yf

STATE_FILE = Path("data/taiex_ma_state.json")
STATE_FILE.parent.mkdir(exist_ok=True)


def fetch_taiex_status() -> dict | None:
    """抓 TAIEX 即時 + 算 MA 距離 + 反彈確認資料"""
    try:
        twii = yf.Ticker("^TWII").history(period="1y", auto_adjust=False).dropna(subset=["Close"])
        if len(twii) < 200:
            return None
        last = float(twii["Close"].iloc[-1])
        ma5 = float(twii["Close"].tail(5).mean())
        ma20 = float(twii["Close"].tail(20).mean())
        ma60 = float(twii["Close"].tail(60).mean())
        ma200 = float(twii["Close"].tail(200).mean())
        prev = float(twii["Close"].iloc[-2])
        chg = (last - prev) / prev * 100

        # 反彈確認需要：最近 3 個交易日是否有「抄底區」
        recent_oversold = False
        recent_oversold_close = None
        recent_oversold_date = None
        for i in range(1, 4):  # T-1, T-2, T-3
            if len(twii) > i:
                past_close = float(twii["Close"].iloc[-1 - i])
                past_ma20 = float(twii["Close"].iloc[-20 - i: -i].mean()) if len(twii) > 20 + i else past_close
                past_dist = (past_close - past_ma20) / past_ma20 * 100
                if -3 <= past_dist <= -1:
                    recent_oversold = True
                    recent_oversold_close = past_close
                    recent_oversold_date = twii.index[-1 - i].strftime("%Y-%m-%d")
                    break

        return {
            "date": twii.index[-1].strftime("%Y-%m-%d"),
            "close": last,
            "chg_pct": chg,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "ma200": ma200,
            "dist_ma5_pct": (last - ma5) / ma5 * 100,
            "dist_ma20_pct": (last - ma20) / ma20 * 100,
            "dist_ma60_pct": (last - ma60) / ma60 * 100,
            "dist_ma200_pct": (last - ma200) / ma200 * 100,
            "recent_oversold": recent_oversold,
            "recent_oversold_close": recent_oversold_close,
            "recent_oversold_date": recent_oversold_date,
        }
    except Exception:
        return None


def classify(status: dict) -> dict:
    """根據距離分級（含抄底+反彈確認）"""
    d20 = status["dist_ma20_pct"]
    d60 = status["dist_ma60_pct"]
    d5 = status.get("dist_ma5_pct", 0)
    chg = status.get("chg_pct", 0)
    recent_oversold = status.get("recent_oversold", False)

    # ⭐ 抄底+反彈確認（最高優先順序）
    # 條件：最近 3 日有抄底區 + 今日 > 1% + 站回 5MA
    if recent_oversold and chg >= 1.0 and d5 >= 0:
        return {
            "level": "📈 抄底反彈確認",
            "label": f"反彈 {chg:+.2f}%，站回 5MA",
            "action": "歷史 73% 創新高，可分批部署不必等 TXF",
            "color": "#2e7d32",
            "bg": "#c8e6c9",
            "is_rebound_signal": True,
        }

    if d60 < 0:
        return {
            "level": "🔴 跌破 60MA",
            "label": "中期空頭",
            "action": "避免進場、考慮減碼",
            "color": "#d62728",
            "bg": "#fde7e9",
        }
    if -3 <= d20 <= -1:
        return {
            "level": "🟢 抄底區",
            "label": "歷史勝率 74%",
            "action": "等反彈確認（漲 1%+ 站回 5MA）再分批進場",
            "color": "#1a5d2e",
            "bg": "#e6f4ea",
        }
    if d20 < -3:
        return {
            "level": "🟡 跌深區",
            "label": "距 20MA 深，反彈但波動高",
            "action": "等止跌訊號（不要急著抄底）",
            "color": "#e65100",
            "bg": "#fff8e1",
        }
    if d20 > 5:
        return {
            "level": "⚠️ 偏熱區",
            "label": "距 20MA > +5%",
            "action": "等回測 20MA 再進場",
            "color": "#f9a825",
            "bg": "#fff8e1",
        }
    return {
        "level": "⚪ 正常",
        "label": "在 20MA 上方正常範圍",
        "action": "看 TXF 訊號 + 推薦榜",
        "color": "#666",
        "bg": "#f5f5f5",
    }


def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    quiet = "--quiet" in sys.argv

    status = fetch_taiex_status()
    if not status:
        print("❌ 抓不到 TAIEX 資料")
        return

    cls = classify(status)
    state = {**status, **cls, "checked_at": date.today().isoformat()}

    print(f"=== TAIEX 6/{status['date']} 抄底訊號 ===")
    print(f"  收盤 {status['close']:,.0f}  漲跌 {status['chg_pct']:+.2f}%")
    print(f"  距 20MA {status['dist_ma20_pct']:+.2f}% ｜ "
          f"60MA {status['dist_ma60_pct']:+.2f}% ｜ "
          f"200MA {status['dist_ma200_pct']:+.2f}%")
    print(f"  狀態：{cls['level']} — {cls['label']}")
    print(f"  建議：{cls['action']}")

    # 跟昨日比較看是否改變
    prev_state = load_state()
    changed = (prev_state is None) or (prev_state.get("level") != cls["level"])

    save_state(state)

    # 狀態改變才推播
    if changed and not quiet:
        try:
            import telegram_notify
            body = (
                f"📊 TAIEX 抄底訊號變動\n\n"
                f"{cls['level']} — {cls['label']}\n\n"
                f"收盤 {status['close']:,.0f}（{status['chg_pct']:+.2f}%）\n"
                f"距 20MA {status['dist_ma20_pct']:+.2f}%\n"
                f"距 60MA {status['dist_ma60_pct']:+.2f}%\n"
                f"距 200MA {status['dist_ma200_pct']:+.2f}%\n\n"
                f"建議：{cls['action']}\n\n"
                f"⚠️ 仍以 TXF 訊號為主"
            )
            ok = telegram_notify.send("📊 TAIEX MA 訊號", body)
            print(f"✅ Telegram 推送：{'成功' if ok else '失敗'}")
        except Exception as e:
            print(f"⚠️ Telegram 推送錯誤：{e}")
    elif not changed:
        print(f"ℹ️ 狀態未變（{cls['level']}），不推播")


if __name__ == "__main__":
    main()
