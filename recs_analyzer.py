"""推薦表現分析器 — 算系統真實 alpha、勝率、各時間框架表現

用法：
  python recs_analyzer.py              # 顯示完整統計報告
  python recs_analyzer.py --csv        # 匯出 csv
  python recs_analyzer.py --recent 10  # 只看最近 10 筆

統計維度：
  1. 整體勝率（T+1, T+5, T+10, T+20）
  2. 平均 EV vs 期望
  3. 最大回撤 / 最大獲利
  4. Best/Worst picks
  5. vs 大盤（TAIEX 同期報酬）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import pandas as pd
import yfinance as yf

HIST_FILE = Path("data/recs_history.jsonl")


def load_history() -> list[dict]:
    if not HIST_FILE.exists():
        return []
    out = []
    for line in HIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def compute_taiex_return(start: str, days: int = 20) -> float | None:
    """TAIEX 起點 → T+days 報酬"""
    try:
        s = datetime.strptime(start, "%Y-%m-%d").date()
        e = s + timedelta(days=int(days * 1.5))
        h = yf.Ticker("^TWII").history(
            start=s - timedelta(days=2), end=e + timedelta(days=2),
            auto_adjust=False
        ).dropna(subset=["Close"])
        if len(h) < 2:
            return None
        h.index = h.index.date
        valid = [d for d in h.index if d >= s]
        if len(valid) < 2:
            return None
        entry_p = float(h.loc[valid[0], "Close"])
        # T+days 目標
        target_idx = min(days, len(valid) - 1)
        end_p = float(h.loc[valid[target_idx], "Close"])
        return (end_p - entry_p) / entry_p * 100
    except Exception:
        return None


def main():
    args = sys.argv[1:]
    export_csv = "--csv" in args
    recent_n = None
    if "--recent" in args:
        idx = args.index("--recent")
        if idx + 1 < len(args):
            recent_n = int(args[idx + 1])

    records = load_history()
    if not records:
        print("📈 推薦追蹤資料尚未建立")
        print("   先跑：python recs_snapshot.py")
        return

    print(f"\n📈 推薦表現追蹤報告（共 {len(records)} 筆）")
    print("=" * 70)

    # 篩選有完整 T+N 的紀錄
    completed = [r for r in records if r.get("status") == "completed"]
    tracking = [r for r in records if r.get("status") == "tracking"]
    print(f"  追蹤中: {len(tracking)} 筆")
    print(f"  完成（T+20 到期）: {len(completed)} 筆")

    if recent_n:
        records = sorted(records, key=lambda r: r["snapshot_date"], reverse=True)[:recent_n]

    if export_csv:
        df = pd.DataFrame(records)
        out = Path("data/recs_history.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n📂 已匯出 {out}")
        return

    # 各時間框架統計
    print("\n📊 各時間框架表現（含追蹤中的部分樣本）")
    print(f"{'時間框架':<10}{'樣本':>6}{'平均報酬':>12}{'勝率':>8}{'最佳':>10}{'最差':>10}")
    print("-" * 70)
    for t in [1, 5, 10, 20]:
        key = f"t{t}_return_pct"
        valid = [r for r in records if r.get(key) is not None]
        if not valid:
            print(f"  T+{t:<3} 尚無資料")
            continue
        rets = [r[key] for r in valid]
        avg = sum(rets) / len(rets)
        wins = sum(1 for r in rets if r > 0)
        best = max(rets)
        worst = min(rets)
        print(f"  T+{t:<6}{len(valid):>6}{avg:>+11.2f}%{wins/len(valid)*100:>+7.0f}%{best:>+9.2f}%{worst:>+9.2f}%")

    # 已完成案例詳細
    if completed:
        print(f"\n🏆 已完成的 {len(completed)} 筆推薦（T+20 報酬排序）")
        print(f"{'snapshot':<12}{'代號':<8}{'股名':<10}{'評分':>5}{'EV%':>7}{'T+20':>9}{'最大獲利':>10}{'最大回撤':>10}")
        print("-" * 80)
        done_sorted = sorted(completed, key=lambda r: -(r.get("t20_return_pct") or 0))
        for r in done_sorted:
            tx_ret = compute_taiex_return(r["snapshot_date"], days=20)
            alpha = (r.get("t20_return_pct", 0) - tx_ret) if tx_ret is not None else 0
            print(
                f"  {r['snapshot_date']:<12}"
                f"{r['code']:<8}{r.get('name', '')[:8]:<10}"
                f"{r['score']:>+5}"
                f"{r['backtest_ev']:>+6.1f}"
                f"{r.get('t20_return_pct', 0):>+8.1f}%"
                f"{r.get('max_gain_pct', 0):>+9.1f}%"
                f"{r.get('max_drawdown_pct', 0):>+9.1f}%"
            )

        # 整體 alpha
        print(f"\n💎 系統真實 Alpha（vs TAIEX 同期）")
        alphas = []
        for r in completed:
            tx_ret = compute_taiex_return(r["snapshot_date"], days=20)
            if tx_ret is not None:
                alphas.append(r.get("t20_return_pct", 0) - tx_ret)
        if alphas:
            print(f"  平均 alpha: {sum(alphas)/len(alphas):+.2f}%")
            print(f"  alpha 勝率: {sum(1 for a in alphas if a > 0)/len(alphas)*100:.0f}%")
            print(f"  最佳 alpha: {max(alphas):+.2f}%")
            print(f"  最差 alpha: {min(alphas):+.2f}%")

    # 預測 EV 準確度
    if completed:
        print(f"\n🎯 系統預測 EV 準確度")
        diffs = []
        for r in completed:
            actual = r.get("t20_return_pct", 0)
            predicted = r.get("backtest_ev", 0)
            diffs.append((predicted, actual))
        if diffs:
            avg_pred = sum(p for p, _ in diffs) / len(diffs)
            avg_actual = sum(a for _, a in diffs) / len(diffs)
            print(f"  系統預測平均 EV: {avg_pred:+.2f}%")
            print(f"  實際平均 T+20: {avg_actual:+.2f}%")
            print(f"  預測偏差: {avg_actual - avg_pred:+.2f}%（負=系統樂觀，正=系統保守）")


if __name__ == "__main__":
    main()
