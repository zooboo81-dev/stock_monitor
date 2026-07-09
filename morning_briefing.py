"""每日晨間摘要 - 8:50 自動執行

產出：
1. data/morning_briefing.html 報告 + 自動開啟
2. Windows Toast 通知

排程：每週一-五 08:50 觸發
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

# pythonw 排程可能沒 stdout/stderr
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

from analysis import smart_money_score
from events import upcoming_events
from institutional import analyze as analyze_instit
from institutional import fetch_institutional_history
from macro import detect_market_regime, fetch_macro, fetch_night

PORTFOLIO_FILE = Path("portfolio.csv")
OUTPUT_HTML = Path("data/morning_briefing.html")
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
TXF_LOG = Path("C:/Users/zoobo/txf-backtest/data/scoreboard_log.txt")


def read_latest_txf_verdict() -> dict | None:
    """讀台指期記分板 log，回傳最新 verdict（訊號優先於排列/觀望）"""
    if not TXF_LOG.exists():
        return None
    try:
        import re
        pat = re.compile(r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s+資料(\d{4}-\d{2}-\d{2})\s+(【[^】]+】)\s+收([\d,]+)")
        priority = {"【做多訊號】": 3, "【做空訊號】": 3, "【出場訊號】": 3,
                    "【偏多】": 1, "【偏空】": 1, "【觀望】": 1}
        by_date = {}
        for line in TXF_LOG.read_text(encoding="utf-8").splitlines():
            m = pat.match(line)
            if not m:
                continue
            data_date = m.group(2); verdict = m.group(3); close = m.group(4).replace(",", "")
            prio = priority.get(verdict, 0)
            existing = by_date.get(data_date)
            if existing is None or prio >= existing[2]:
                by_date[data_date] = (verdict, close, prio)
        if not by_date:
            return None
        latest_date = max(by_date.keys())
        v, c, _ = by_date[latest_date]
        return {"date": latest_date, "verdict": v, "close": int(c)}
    except Exception:
        return None


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """批次抓現價"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    })
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    except Exception:
        pass
    out = {}
    # 分塊
    for i in range(0, len(codes), 40):
        sub = codes[i: i + 40]
        ex_ch = "|".join([f"tse_{c}.tw" for c in sub] + [f"otc_{c}.tw" for c in sub])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        try:
            r = s.get(url, timeout=10)
            for d in r.json().get("msgArray", []):
                code = d.get("c")
                if not code or code in out:
                    continue
                try:
                    z = d.get("z")
                    px = float(z) if z not in (None, "-", "") else float(d.get("y") or 0)
                    if px > 0:
                        out[code] = {"price": px}
                except Exception:
                    continue
        except Exception:
            continue
    return out


def main():
    today = datetime.now()
    weekday = today.weekday()  # 0=Mon, 6=Sun

    # 週末跳過
    if weekday >= 5:
        return

    # 1. 抓所有資料
    macro = fetch_macro() or {}
    night = fetch_night() or {}

    # 市況
    try:
        taiex = yf.Ticker("^TWII").history(period="2y", auto_adjust=False)
        regime = detect_market_regime(taiex)
    except Exception:
        regime = {"emoji": "⚪", "label": "判斷失敗"}

    # 持倉
    portfolio = pd.read_csv(PORTFOLIO_FILE, dtype={"code": str})
    codes = portfolio["code"].tolist()
    quotes = fetch_quotes(codes)

    # 載入 Trailing Stop 狀態
    try:
        from trailing_stop import compute_trail_stop, effective_stop, load_state
        trail_state = load_state()
    except Exception:
        trail_state = {}
        compute_trail_stop = effective_stop = None

    # 分類停損狀態（套用 Trailing Stop = max(固定停損, trail 停損)）
    triggered = []   # 已跌破生效停損
    near_stop = []   # 距生效停損 < 3%
    safe_locked = []  # 鎖利 > 20%
    for _, r in portfolio.iterrows():
        code = r["code"]
        name = r["name"]
        cost = float(r["cost"])
        sl_str = str(r.get("stop_loss", ""))
        q = quotes.get(code)
        if not q:
            continue
        px = q["price"]
        pnl_pct = (px - cost) / cost * 100
        if not sl_str or sl_str == "nan" or sl_str == "":
            continue
        try:
            fixed_sl = float(sl_str)
        except ValueError:
            continue

        # 計算 Trailing Stop
        tier = "未啟動"
        trail_sl = 0.0
        if compute_trail_stop and code in trail_state:
            peak = trail_state[code]["peak"]
            trail_sl, tier = compute_trail_stop(cost, peak)
        eff_sl = effective_stop(fixed_sl, trail_sl) if effective_stop else fixed_sl

        dist = (px - eff_sl) / eff_sl * 100 if eff_sl > 0 else 0
        item = {
            "code": code, "name": name, "cost": cost, "price": px,
            "stop": eff_sl, "pnl_pct": pnl_pct, "dist_pct": dist,
            "tier": tier, "fixed_stop": fixed_sl, "trail_stop": trail_sl,
        }
        if px <= eff_sl:
            triggered.append(item)
        elif dist < 3:
            near_stop.append(item)
        elif pnl_pct > 20:
            safe_locked.append(item)

    # 重要事件（未來 7 天）
    events = upcoming_events(7)

    # 產出 HTML
    html = generate_html(today, regime, macro, night, triggered, near_stop, safe_locked, events)
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    # 自動開啟
    try:
        os.startfile(str(OUTPUT_HTML))
    except Exception:
        pass

    # 讀台指期最新訊號
    txf = read_latest_txf_verdict()

    # 通知（Windows Toast + Telegram）
    title = f"☀️ 晨間摘要 {today.strftime('%m/%d')}"
    # 市況一行 + 台指期狀態
    regime_line = f"市況 {regime.get('label', '')}（{regime.get('emoji', '')} 200MA 長期）"
    if txf:
        verdict = txf["verdict"]
        verdict_emo = {
            "【做多訊號】": "🟢", "【做空訊號】": "🔴", "【出場訊號】": "🟠",
            "【偏多】": "🔵", "【偏空】": "🔵", "【觀望】": "⚪",
        }.get(verdict, "")
        txf_line = f"\n台指期 {verdict_emo} {verdict}（短中期）  資料 {txf['date']}  收 {txf['close']:,}"
    else:
        txf_line = ""
    critical_count = len(triggered) + len(near_stop)
    if critical_count > 0:
        msg = regime_line + txf_line + f"\n\n⚠️ {critical_count} 檔需處理"
        if triggered:
            msg += "\n\n🔴 已跌破停損："
            for it in triggered[:5]:
                msg += f"\n• {it['name']} {it['code']}  停損 {it['stop']:.2f}（現 {it['price']:.2f}, {it['pnl_pct']:+.1f}%）"
        if near_stop:
            msg += "\n\n⚠️ 接近停損（距 < 3%）："
            for it in near_stop[:5]:
                msg += f"\n• {it['name']} {it['code']}  停損 {it['stop']:.2f}（現 {it['price']:.2f}, 距 {it['dist_pct']:+.2f}%）"
    else:
        msg = regime_line + txf_line + "\n\n✓ 所有持倉安全"

    # 📉 TAIEX MA 抄底訊號
    try:
        import json
        ma_file = Path("data/taiex_ma_state.json")
        if ma_file.exists():
            ma = json.loads(ma_file.read_text(encoding="utf-8"))
            msg += (
                f"\n\n📉 TAIEX {ma['level']}（{ma['label']}）"
                f"\n距 20MA {ma['dist_ma20_pct']:+.2f}% ｜ 60MA {ma['dist_ma60_pct']:+.2f}%"
                f"\n→ {ma['action']}"
            )

            # 7/09 新增：反彈確認閾值提醒（只在跌破 5MA 或 20MA 才顯示）
            _close = ma.get("close", 0)
            _ma5 = ma.get("ma5", 0)
            _dist_ma20 = ma.get("dist_ma20_pct", 0)
            if _close and _ma5 and (_close < _ma5 or _dist_ma20 < 0):
                _thr_ma5 = _ma5
                _thr_rise = _close * 1.01
                _threshold = max(_thr_ma5, _thr_rise)
                _rise_pct = (_threshold - _close) / _close * 100
                msg += (
                    f"\n\n🎯 反彈確認閾值："
                    f"\n• 收 ≥ {_threshold:,.0f}（漲 ≥ {_rise_pct:.2f}%）"
                    f"\n• 站回 5MA {_ma5:,.0f} + 漲 1%+"
                    f"\n→ ✅ 觸發即可分批進場定存區"
                )
    except Exception:
        pass

    # 📔 Trade Journal 待回顧提醒
    try:
        import json
        pending_file = Path("data/journal_pending.json")
        if pending_file.exists():
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            if pending:
                msg += f"\n\n📔 Trade Journal：{len(pending)} 筆待回顧"
                msg += "\n→ 開 data/trade_journal.md 填 5 分鐘"
    except Exception:
        pass

    # Windows Toast
    try:
        from winotify import Notification, audio
        short_msg = msg.split('\n')[0]
        toast = Notification(
            app_id="股票晨間摘要", title=title, msg=short_msg, duration="long",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass

    # Telegram
    try:
        import telegram_notify
        telegram_notify.send(title, msg)
    except Exception:
        pass


def generate_html(today, regime, macro, night, triggered, near_stop, locked, events):
    # 市況顏色
    regime_color = {
        "bull": "#2ca02c", "warning": "#f9a825",
        "bear": "#d62728", "unknown": "#888",
    }.get(regime.get("regime", "unknown"), "#888")
    regime_bg = {
        "bull": "#e6f4ea", "warning": "#fff8e1",
        "bear": "#fde7e9", "unknown": "#f5f5f5",
    }.get(regime.get("regime", "unknown"), "#f5f5f5")

    # 構造 macro/night 卡片
    def macro_card(d, fmt="{:.2f}"):
        if not d:
            return ""
        level = d.get("level", "normal")
        color = {"panic": "#d62728", "warn": "#d62728",
                 "calm": "#2ca02c", "bull": "#2ca02c"}.get(level, "#666")
        return f"""
        <div class="metric">
          <div class="m-name">{d.get('name', '')}</div>
          <div class="m-val" style="color:{color}">{fmt.format(d['last'])}</div>
          <div class="m-chg" style="color:{color}">{d['chg_pct']:+.2f}%</div>
        </div>"""

    macro_html = "".join([
        macro_card(macro.get("VIX")),
        macro_card(macro.get("SOX"), "{:,.0f}"),
        macro_card(macro.get("DXY")),
    ])
    night_html = "".join([
        macro_card(night.get("ES"), "{:,.0f}"),
        macro_card(night.get("NQ"), "{:,.0f}"),
        macro_card(night.get("TSM")),
    ])

    # 警示區
    def alert_row(item, kind):
        c = "#d62728" if kind == "triggered" else "#f9a825"
        bg = "#fde7e9" if kind == "triggered" else "#fff8e1"
        icon = "🚨" if kind == "triggered" else "⚠️"
        return f"""
        <tr style="background:{bg}">
          <td>{icon}</td>
          <td><b>{item['code']} {item['name']}</b></td>
          <td>成本 {item['cost']:.2f}</td>
          <td>現價 <b>{item['price']:.2f}</b></td>
          <td>停損 <b style="color:{c}">{item['stop']:.2f}</b></td>
          <td style="color:{c}">{item['dist_pct']:+.2f}%</td>
          <td>損益 {item['pnl_pct']:+.2f}%</td>
        </tr>"""

    alerts_rows = "".join([alert_row(it, "triggered") for it in triggered] +
                          [alert_row(it, "near_stop") for it in near_stop])
    if not alerts_rows:
        alerts_rows = '<tr><td colspan="7" style="text-align:center; padding:20px; color:#2ca02c">✅ 所有持倉安全，無需處理</td></tr>'

    # 鎖利區
    locked_rows = "".join([
        f"""<tr style="background:#e6f4ea">
          <td>💰</td>
          <td><b>{it['code']} {it['name']}</b></td>
          <td>損益 <b style="color:#d62728">{it['pnl_pct']:+.2f}%</b></td>
          <td>停損 {it['stop']:.2f}（距 {it['dist_pct']:+.2f}%）</td>
        </tr>"""
        for it in sorted(locked, key=lambda x: -x["pnl_pct"])[:5]
    ])
    if not locked_rows:
        locked_rows = '<tr><td colspan="4" style="text-align:center; padding:10px; color:#666">尚無鎖利部位</td></tr>'

    # 事件
    events_rows = "".join([
        f"""<tr>
          <td>{e['date']}</td>
          <td>D-{e['days_left']}</td>
          <td><b>{e['name']}</b></td>
          <td><span style="background:{'#d62728' if e['impact']=='high' else '#f9a825'}; color:white;
              padding:2px 6px; border-radius:3px; font-size:11px">{e['impact'].upper()}</span></td>
        </tr>"""
        for e in events
    ])
    if not events_rows:
        events_rows = '<tr><td colspan="4" style="text-align:center; padding:10px; color:#666">未來 7 天無重大事件</td></tr>'

    # 今日 SOP（根據警示動態調整）
    sop_items = ["□ 9:00 開盤觀察 TAIEX 走勢"]
    if triggered:
        for it in triggered:
            sop_items.append(f"□ 9:00 立刻 賣出 <b style='color:#d62728'>{it['code']} {it['name']}</b>（已跌破停損）")
    if near_stop:
        for it in near_stop:
            sop_items.append(f"□ 掛停損賣單 {it['code']} {it['name']} @ {it['stop']:.2f}")
    sop_items.append("□ 9:10 後可考慮新進場（先看儀表板推薦榜）")
    sop_items.append("□ 進場立刻設停損停利")
    sop_html = "<br>".join(sop_items)

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>晨間摘要 {today.strftime('%Y-%m-%d')}</title>
<style>
  body {{ font-family: 'Microsoft JhengHei', sans-serif; max-width: 1100px;
         margin: 20px auto; padding: 0 20px; background: #fafbfc; color: #1a1a1a; }}
  h1 {{ border-bottom: 3px solid #1a1a1a; padding-bottom: 8px; }}
  h2 {{ background: #1a1a1a; color: white; padding: 8px 14px; border-radius: 4px; margin-top: 30px; }}
  .regime-banner {{ background: {regime_bg}; border: 2px solid {regime_color};
                    padding: 14px 20px; border-radius: 8px; font-size: 20px;
                    margin: 12px 0; }}
  .regime-banner .deviation {{ color: #666; font-size: 14px; }}
  .metrics {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }}
  .metric {{ background: white; border: 1px solid #ddd; border-radius: 6px;
            padding: 12px 18px; min-width: 120px; }}
  .m-name {{ color: #666; font-size: 12px; }}
  .m-val {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .m-chg {{ font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; background: white; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f0f2f6; }}
  .sop {{ background: white; border-left: 5px solid #1a1a1a; padding: 14px 20px;
         line-height: 2; font-size: 15px; }}
</style>
</head>
<body>
  <h1>☀️ 晨間摘要 — {today.strftime('%Y年%m月%d日 (%A) %H:%M')}</h1>

  <div class="regime-banner">
    <b>市況：{regime.get('emoji', '')} {regime.get('label', '')}</b>
    {('<span class="deviation">　TAIEX ' + f"{regime.get('last', 0):,.0f} vs 200MA {regime.get('ma200', 0):,.0f} ({regime.get('deviation', 0):+.1f}%)" + '</span>') if 'deviation' in regime else ''}
    <br><span style="color:#666; font-size:14px">{regime.get('advice', '')}</span>
  </div>

  <h2>🌍 夜盤 / 美股表現</h2>
  <b>日盤指標</b>
  <div class="metrics">{macro_html}</div>
  <b>夜盤指標（即時）</b>
  <div class="metrics">{night_html}</div>

  <h2>🚨 持倉警示（明日需處理）</h2>
  <table>
    <thead><tr><th></th><th>股票</th><th>成本</th><th>現價</th><th>停損</th><th>距停損</th><th>損益</th></tr></thead>
    <tbody>{alerts_rows}</tbody>
  </table>

  <h2>💰 鎖利進行中（大贏家）</h2>
  <table>
    <thead><tr><th></th><th>股票</th><th>損益</th><th>停損保護</th></tr></thead>
    <tbody>{locked_rows}</tbody>
  </table>

  <h2>📅 未來 7 天重要事件</h2>
  <table>
    <thead><tr><th>日期</th><th>距離</th><th>事件</th><th>影響</th></tr></thead>
    <tbody>{events_rows}</tbody>
  </table>

  <h2>✅ 今日 SOP</h2>
  <div class="sop">{sop_html}</div>

  <p style="color:#666; font-size:12px; margin-top:30px; text-align:center">
    自動產生於 {today.strftime('%H:%M:%S')} | 詳細儀表板 → <a href="http://localhost:8501">http://localhost:8501</a>
  </p>
</body>
</html>
"""
    return html


if __name__ == "__main__":
    main()
