"""Walk-Forward 驗證 — 避免過擬合，驗證策略穩定性

原理：把 recs_history.jsonl 按時間分月，每月獨立算績效
若績效在各月「穩定」→ 策略可信
若績效「集中在某段時間」→ 可能是碰巧的 in-sample fit

用法：
  python backtest_walkforward.py

產出：
  data_export/walkforward_by_month.csv
  data_export/walkforward_summary.txt
"""
from __future__ import annotations
import csv
import json
import math
import os
import statistics as _s
from collections import defaultdict
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

RECS_FILE = Path("data/recs_history.jsonl")
OUT_DIR = Path("data_export")
OUT_DIR.mkdir(exist_ok=True)


def load_recs():
    recs = []
    for line in RECS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def compute_ic(scores: list[float], returns: list[float]) -> float:
    """Information Coefficient = corr(score, return)
    IC > 0.05 = 有預測力；> 0.10 = 顯著；> 0.15 = 優秀
    """
    if len(scores) < 3 or len(scores) != len(returns):
        return 0.0
    try:
        n = len(scores)
        mean_s = sum(scores) / n
        mean_r = sum(returns) / n
        num = sum((s - mean_s) * (r - mean_r) for s, r in zip(scores, returns))
        den_s = math.sqrt(sum((s - mean_s) ** 2 for s in scores))
        den_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns))
        if den_s == 0 or den_r == 0:
            return 0.0
        return num / (den_s * den_r)
    except Exception:
        return 0.0


def deflated_sharpe(returns: list[float], n_trials: int = 5) -> float:
    """簡化版 Deflated Sharpe
    調整多重比較後的顯著性（試越多策略門檻越高）
    """
    if len(returns) < 3:
        return 0.0
    try:
        mean = sum(returns) / len(returns)
        std = _s.stdev(returns) if len(returns) > 1 else 1.0
        if std == 0:
            return 0.0
        sharpe = mean / std
        # 簡化 deflation：試 N 次策略需除以 sqrt(log(N))
        penalty = math.sqrt(math.log(max(n_trials, 2)))
        return sharpe / penalty
    except Exception:
        return 0.0


def analyze_by_month(recs: list[dict]) -> list[dict]:
    """按月分組計算績效"""
    by_month = defaultdict(list)
    for r in recs:
        d = r.get("snapshot_date", "")
        if len(d) < 7:
            continue
        month = d[:7]  # YYYY-MM
        by_month[month].append(r)

    results = []
    for month, items in sorted(by_month.items()):
        # T+5 為主要指標
        t5_rets = [r.get("t5_return_pct") for r in items if r.get("t5_return_pct") is not None]
        t10_rets = [r.get("t10_return_pct") for r in items if r.get("t10_return_pct") is not None]
        scores = [r["score"] for r in items if r.get("t5_return_pct") is not None]

        if not t5_rets:
            continue

        wins = sum(1 for r in t5_rets if r > 0)
        wr = wins / len(t5_rets) * 100

        results.append({
            "month": month,
            "num_recs": len(items),
            "num_completed_t5": len(t5_rets),
            "avg_t5_return": round(sum(t5_rets) / len(t5_rets), 2),
            "avg_t10_return": round(sum(t10_rets) / len(t10_rets), 2) if t10_rets else None,
            "win_rate_pct": round(wr, 1),
            "median_t5": round(sorted(t5_rets)[len(t5_rets) // 2], 2),
            "best_t5": round(max(t5_rets), 2),
            "worst_t5": round(min(t5_rets), 2),
            "std_t5": round(_s.stdev(t5_rets), 2) if len(t5_rets) > 1 else 0,
            "ic_t5": round(compute_ic(scores, t5_rets), 3),
        })
    return results


def summarize_stability(monthly: list[dict]) -> dict:
    """判斷策略是否穩定"""
    if len(monthly) < 3:
        return {"verdict": "資料不足，至少要 3 個月", "stability": None}

    avg_returns = [m["avg_t5_return"] for m in monthly]
    win_rates = [m["win_rate_pct"] for m in monthly]
    ics = [m["ic_t5"] for m in monthly]

    # 幾個關鍵指標
    positive_months = sum(1 for r in avg_returns if r > 0)
    consistency_pct = positive_months / len(avg_returns) * 100
    avg_return_overall = sum(avg_returns) / len(avg_returns)
    std_return_across_months = _s.stdev(avg_returns) if len(avg_returns) > 1 else 0
    avg_ic = sum(ics) / len(ics)
    avg_wr = sum(win_rates) / len(win_rates)

    # 判斷
    verdict_parts = []
    if consistency_pct >= 70:
        verdict_parts.append("✅ 穩定：70%+ 月份為正報酬")
    elif consistency_pct >= 50:
        verdict_parts.append("⚠️ 中等：一半月份正報酬")
    else:
        verdict_parts.append("❌ 不穩定：多數月份虧損")

    if abs(avg_return_overall) > std_return_across_months:
        verdict_parts.append(f"✅ 訊噪比高（avg {avg_return_overall:.2f} > std {std_return_across_months:.2f}）")
    else:
        verdict_parts.append(f"⚠️ 訊噪比低（波動大於平均，可能是碰運氣）")

    if avg_ic > 0.10:
        verdict_parts.append(f"✅ IC 優秀 {avg_ic:.3f}（score 有預測力）")
    elif avg_ic > 0.05:
        verdict_parts.append(f"🟡 IC 中等 {avg_ic:.3f}（弱預測力）")
    else:
        verdict_parts.append(f"❌ IC 過低 {avg_ic:.3f}（score 無預測力）")

    return {
        "months_analyzed": len(monthly),
        "positive_months_pct": round(consistency_pct, 1),
        "avg_t5_return": round(avg_return_overall, 2),
        "std_across_months": round(std_return_across_months, 2),
        "avg_win_rate": round(avg_wr, 1),
        "avg_ic": round(avg_ic, 3),
        "verdict": "\n".join(verdict_parts),
    }


def main():
    recs = load_recs()
    print(f"讀進 {len(recs)} 筆推薦記錄")

    monthly = analyze_by_month(recs)
    if not monthly:
        print("❌ 無足夠資料")
        return

    # 輸出月度 CSV
    csv_path = OUT_DIR / "walkforward_by_month.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(monthly[0].keys()))
        w.writeheader()
        w.writerows(monthly)
    print(f"✅ 月度績效 → {csv_path}")

    # 輸出摘要
    summary = summarize_stability(monthly)
    summary_path = OUT_DIR / "walkforward_summary.txt"
    lines = [
        "=" * 60,
        "Walk-Forward 驗證結果",
        "=" * 60,
        f"分析月份數：{summary['months_analyzed']}",
        f"正報酬月份佔比：{summary['positive_months_pct']}%",
        f"整體 T+5 平均報酬：{summary['avg_t5_return']:+.2f}%",
        f"月間報酬標準差：{summary['std_across_months']:.2f}%",
        f"整體勝率：{summary['avg_win_rate']}%",
        f"平均 IC：{summary['avg_ic']:.3f}",
        "",
        "判斷：",
        summary["verdict"],
        "",
        "=" * 60,
        "月度細節：",
        "=" * 60,
    ]
    for m in monthly:
        lines.append(
            f"{m['month']}: {m['num_completed_t5']} 檔 "
            f"｜ T+5 {m['avg_t5_return']:+.2f}% "
            f"｜ 勝率 {m['win_rate_pct']}% "
            f"｜ IC {m['ic_t5']:.3f} "
            f"｜ 波動 std {m['std_t5']:.2f}"
        )
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 摘要 → {summary_path}")

    print()
    print("\n".join(lines))


if __name__ == "__main__":
    main()
