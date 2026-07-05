"""Trailing Stop — 移動式停利

階梯規則（漲幅愈高、容忍回撤愈大，鎖利愈多）：
  漲幅 < +10%   : 不啟動 trail，用 portfolio.csv 的固定 stop_loss
  漲幅 +10~25%  : trail = peak × 0.93  (回撤 -7%)
  漲幅 +25~50%  : trail = peak × 0.90  (回撤 -10%)
  漲幅 ≥ +50%   : trail = peak × 0.85  (回撤 -15%)

生效停損 = max(固定 stop_loss, trail_stop)  ← 永遠保護高者

狀態檔: data/trailing_state.json
  {
    "2455": {
      "entry": 374.53,
      "peak": 410.0,
      "peak_date": "2026-06-08",
      "first_seen": "2026-05-13"
    }
  }

用法：
  python trailing_stop.py                # 更新所有持倉 peak 並印 trail stop
  python trailing_stop.py --init         # 用現價當 peak 初始化（首次設定）
  python trailing_stop.py --show         # 只看不動，列出表格
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# pythonw 排程沒 stdout
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

PORTFOLIO_FILE = Path("portfolio.csv")
STATE_FILE = Path("data/trailing_state.json")
STATE_FILE.parent.mkdir(exist_ok=True)


# ---------- 規則 ----------

def compute_trail_stop(entry: float, peak: float) -> tuple[float, str]:
    """回傳 (trail_stop_price, tier_label)"""
    gain_pct = (peak - entry) / entry * 100
    if gain_pct < 10:
        return (0.0, "未啟動")
    if gain_pct < 25:
        return (peak * 0.93, "T1 (-7%)")
    if gain_pct < 50:
        return (peak * 0.90, "T2 (-10%)")
    return (peak * 0.85, "T3 (-15%)")


def effective_stop(fixed_stop: float, trail_stop: float) -> float:
    """生效停損 = 兩者取高"""
    return max(fixed_stop, trail_stop)


# ---------- 狀態管理 ----------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 抓報價 ----------

def fetch_prices(codes: list[str]) -> dict[str, float]:
    """批次抓 TWSE MIS 即時/收盤"""
    if not codes:
        return {}
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0",
                      "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    except Exception:
        pass
    quotes = {}
    for i in range(0, len(codes), 40):
        sub = codes[i: i + 40]
        ex_ch = "|".join([f"tse_{c}.tw" for c in sub] + [f"otc_{c}.tw" for c in sub])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        try:
            r = s.get(url, timeout=10)
            for d in r.json().get("msgArray", []):
                code = d.get("c")
                if not code or code in quotes:
                    continue
                z = d.get("z")
                px = float(z) if z and z not in ("-", "") else float(d.get("y") or 0)
                if px > 0:
                    # 用 today high 來抓 peak（盤中 trailing 更精準）
                    h = d.get("h")
                    today_high = float(h) if h and h not in ("-", "") else px
                    quotes[code] = {"price": px, "today_high": today_high}
        except Exception:
            continue
    return quotes


# ---------- 主流程 ----------

def update_peaks(init_mode: bool = False) -> list[dict]:
    """更新 peak 並回傳每檔狀態"""
    if not PORTFOLIO_FILE.exists():
        return []
    df = pd.read_csv(PORTFOLIO_FILE, dtype={"code": str})
    state = load_state()
    today = date.today().isoformat()
    quotes = fetch_prices(df["code"].tolist())

    rows = []
    for _, r in df.iterrows():
        code = r["code"]
        entry = float(r["cost"])
        fixed_stop = float(r["stop_loss"]) if pd.notna(r.get("stop_loss")) else 0.0
        q = quotes.get(code, {})
        current = q.get("price", entry)
        today_high = q.get("today_high", current)

        # 第一次見到 → 初始化
        if code not in state:
            init_peak = max(entry, today_high) if init_mode else entry
            state[code] = {
                "entry": entry,
                "peak": init_peak,
                "peak_date": today,
                "first_seen": today,
            }
        else:
            # 用「盤中最高」更新 peak（不只是現價）
            if today_high > state[code]["peak"]:
                state[code]["peak"] = today_high
                state[code]["peak_date"] = today

        peak = state[code]["peak"]
        trail_stop, tier = compute_trail_stop(entry, peak)
        eff = effective_stop(fixed_stop, trail_stop)

        gain_now = (current - entry) / entry * 100
        gain_peak = (peak - entry) / entry * 100
        dist_to_stop = (current - eff) / eff * 100 if eff > 0 else 0

        rows.append({
            "code": code,
            "name": r["name"],
            "entry": entry,
            "current": current,
            "peak": peak,
            "fixed_stop": fixed_stop,
            "trail_stop": trail_stop,
            "effective_stop": eff,
            "tier": tier,
            "gain_now_pct": gain_now,
            "gain_peak_pct": gain_peak,
            "dist_to_stop_pct": dist_to_stop,
        })

    save_state(state)
    return rows


def print_table(rows: list[dict]) -> None:
    print(f"{'代號':<6}{'名稱':<10}{'成本':>8}{'現價':>8}{'最高':>8}{'階梯':<10}{'生效停損':>10}{'距停損':>8}")
    print("-" * 80)
    for r in rows:
        active = "🔴" if r["dist_to_stop_pct"] < 0 else ("🟡" if r["dist_to_stop_pct"] < 3 else "🟢")
        print(f"{r['code']:<6}{r['name']:<10}"
              f"{r['entry']:>8.2f}{r['current']:>8.2f}{r['peak']:>8.2f}"
              f"  {r['tier']:<8}{r['effective_stop']:>10.2f}"
              f"  {active}{r['dist_to_stop_pct']:>+5.1f}%")


def main():
    args = sys.argv[1:]
    init_mode = "--init" in args
    show_only = "--show" in args

    rows = update_peaks(init_mode=init_mode)

    if init_mode:
        print("✅ Trailing Stop 已用今日盤中最高初始化")
    print_table(rows)

    # 預警：距生效停損 < 1.5% 推播
    if not show_only:
        near = [r for r in rows if 0 <= r["dist_to_stop_pct"] < 1.5]
        triggered = [r for r in rows if r["dist_to_stop_pct"] < 0]
        if triggered or near:
            lines = []
            if triggered:
                lines.append("⛔ Trail Stop 已觸發：")
                for r in triggered:
                    lines.append(f"・{r['name']} {r['code']}  停損 {r['effective_stop']:.2f}（現 {r['current']:.2f}）")
            if near:
                lines.append("\n⚠️ 接近 Trail Stop（< 1.5%）：")
                for r in near:
                    lines.append(f"・{r['name']} {r['code']}  停損 {r['effective_stop']:.2f}（現 {r['current']:.2f}, {r['tier']}）")
            body = "\n".join(lines)
            try:
                from winotify import Notification, audio
                t = Notification(app_id="Trail Stop", title="📈 移動停利警示", msg=body, duration="long")
                t.set_audio(audio.Default, loop=False)
                t.show()
            except Exception:
                pass
            try:
                import telegram_notify
                telegram_notify.send("📈 Trail Stop 警示", body)
            except Exception:
                pass


if __name__ == "__main__":
    main()
