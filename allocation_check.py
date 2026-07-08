"""資產配置檢查 — 每月 1 號 08:50 排程

比對 portfolio.csv 各層 vs allocation_targets.json 目標
若 drift > 15% 觸發 Pushover 警告
"""
from __future__ import annotations
import csv
import json
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

PORTFOLIO = Path("portfolio.csv")
CASH_FILE = Path("data/cash.json")
TARGETS_FILE = Path("data/allocation_targets.json")


def load_cash() -> int:
    if not CASH_FILE.exists():
        return 0
    try:
        return int(json.loads(CASH_FILE.read_text(encoding="utf-8")).get("cash_twd", 0))
    except Exception:
        return 0


def load_holdings() -> list[dict]:
    if not PORTFOLIO.exists():
        return []
    rows = []
    with open(PORTFOLIO, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def fetch_price(code: str) -> float | None:
    """簡易報價：從 TWSE 抓當前價"""
    try:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw|otc_{code}.tw&json=1&delay=0"
        r = s.get(url, timeout=8)
        for d in r.json().get("msgArray", []):
            z = d.get("z"); y = d.get("y")
            try:
                px = float(z) if z not in (None, "-", "") else float(y or 0)
                if px > 0:
                    return px
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return None


def main():
    if not TARGETS_FILE.exists():
        print("❌ 沒有 allocation_targets.json")
        return
    targets = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    tgt = targets.get("targets", {})
    threshold = targets.get("drift_threshold_pct", 15)

    cash = load_cash()
    holdings = load_holdings()

    # 計算各層資產
    actual = {"swing": 0, "core": 0, "income": 0, "cash": cash}
    for h in holdings:
        shares = int(h.get("shares", 0) or 0)
        if shares <= 0:
            continue
        code = h["code"]
        price = fetch_price(code) or float(h.get("cost", 0) or 0)
        val = shares * price
        ht = (h.get("hold_type") or "swing").strip().lower()
        if ht not in actual:
            ht = "swing"
        actual[ht] += val

    target_total = sum(v["target_twd"] for v in tgt.values())
    actual_total = sum(actual.values())

    # 找 drift 超標的層
    alerts = []
    for key, cfg in tgt.items():
        t = cfg["target_twd"]
        v = actual.get(key, 0)
        pct_target = (t / target_total * 100) if target_total else 0
        pct_actual = (v / target_total * 100) if target_total else 0
        drift = pct_actual - pct_target
        alerts.append({
            "key": key,
            "label": cfg.get("label", key),
            "actual": v,
            "target": t,
            "diff": v - t,
            "drift_pct": drift,
            "over_threshold": abs(drift) > threshold,
        })

    # 產月度摘要（每月都推）+ 若有 drift 大的加警告
    lines = [f"📊 資產配置月度檢查\n"]
    lines.append(f"總資產進度：{actual_total:,.0f} / {target_total:,.0f} "
                 f"（{actual_total/target_total*100:.1f}%）\n")
    over_threshold = [a for a in alerts if a["over_threshold"]]

    for a in alerts:
        emo = "✅" if not a["over_threshold"] else ("🚨" if abs(a["drift_pct"]) > 20 else "⚠️")
        fill = a["actual"] / a["target"] * 100 if a["target"] else 0
        lines.append(
            f"{emo} {a['label']}\n"
            f"   {a['actual']:,.0f} / {a['target']:,.0f} ({fill:.0f}%)"
        )

    if over_threshold:
        lines.append("\n🎯 需要調整：")
        for a in over_threshold:
            if a["diff"] < 0:
                lines.append(f"• {a['label']} 少 {-a['diff']:,.0f} → 加碼")
            else:
                lines.append(f"• {a['label']} 多 {a['diff']:,.0f} → 減碼")

    body = "\n".join(lines)
    title = "📊 月度配置檢查" + (f" — {len(over_threshold)} 檔需調整" if over_threshold else " — 健康")

    try:
        import telegram_notify
        telegram_notify.send(title, body)
        print(f"✅ 已推送月度配置檢查")
    except Exception as e:
        print(f"⚠️ 推送錯誤：{e}")


if __name__ == "__main__":
    main()
