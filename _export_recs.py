"""匯出所有推薦紀錄到 data_export/ 供分析

產出：
  data_export/
    all_recommendations.csv    # 主資料表（所有欄位）
    by_stock_summary.csv       # 依股票匯總（進推薦次數、平均EV/勝率、平均報酬）
    by_date_summary.csv        # 依日期匯總（當天推薦數、平均分數/EV）
    daily_top_pick.csv         # 每日 rank 1 的股票 + 表現
    performance_ranked.csv     # 依 T20 報酬排序（最好→最差）
    README.md                  # 資料字典 + 使用說明
"""
import json
from pathlib import Path
import csv
from collections import defaultdict
from statistics import mean, median

ROOT = Path(__file__).parent
SRC = ROOT / "data" / "recs_history.jsonl"
OUT = ROOT / "data_export"
OUT.mkdir(exist_ok=True)

# ── 讀 ─────────────────────────────────────
recs = []
for line in SRC.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        recs.append(json.loads(line))
    except Exception:
        pass

print(f"讀進 {len(recs)} 筆推薦")

# ── 1. all_recommendations.csv ────────────
all_cols = [
    "snapshot_date", "rank", "code", "name", "score",
    "strategy_type", "strategy_label",   # 優化 C
    "backtest_ev", "backtest_wr", "backtest_n",
    "entry_price", "atr14",              # 優化 A
    "t1_price", "t1_return_pct",
    "t5_price", "t5_return_pct",
    "t10_price", "t10_return_pct",
    "t20_price", "t20_return_pct",
    "max_gain_pct", "max_drawdown_pct",
    "status",
]
with open(OUT / "all_recommendations.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
    w.writeheader()
    for r in sorted(recs, key=lambda x: (x.get("snapshot_date", ""), x.get("rank", 99))):
        w.writerow(r)
print(f"✅ all_recommendations.csv  ({len(recs)} 筆)")

# ── 2. by_stock_summary.csv ───────────────
by_stock = defaultdict(list)
for r in recs:
    by_stock[(r["code"], r["name"])].append(r)

stock_rows = []
for (code, name), items in by_stock.items():
    def safe_mean(key):
        vals = [x[key] for x in items if x.get(key) is not None]
        return round(mean(vals), 2) if vals else None
    stock_rows.append({
        "code": code,
        "name": name,
        "times_recommended": len(items),
        "first_date": min(x["snapshot_date"] for x in items),
        "last_date": max(x["snapshot_date"] for x in items),
        "avg_score": round(mean([x["score"] for x in items]), 2),
        "avg_ev": safe_mean("backtest_ev"),
        "avg_wr": safe_mean("backtest_wr"),
        "avg_t1_return": safe_mean("t1_return_pct"),
        "avg_t5_return": safe_mean("t5_return_pct"),
        "avg_t10_return": safe_mean("t10_return_pct"),
        "avg_t20_return": safe_mean("t20_return_pct"),
        "avg_max_gain": safe_mean("max_gain_pct"),
        "avg_max_drawdown": safe_mean("max_drawdown_pct"),
    })
stock_rows.sort(key=lambda x: (-x["times_recommended"], x["code"]))
with open(OUT / "by_stock_summary.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(stock_rows[0].keys()))
    w.writeheader()
    w.writerows(stock_rows)
print(f"✅ by_stock_summary.csv  ({len(stock_rows)} 檔不重複)")

# ── 3. by_date_summary.csv ────────────────
by_date = defaultdict(list)
for r in recs:
    by_date[r["snapshot_date"]].append(r)

date_rows = []
for d, items in sorted(by_date.items()):
    def safe_mean_d(key):
        vals = [x[key] for x in items if x.get(key) is not None]
        return round(mean(vals), 2) if vals else None
    date_rows.append({
        "date": d,
        "num_recs": len(items),
        "avg_score": round(mean([x["score"] for x in items]), 2),
        "avg_ev": safe_mean_d("backtest_ev"),
        "avg_wr": safe_mean_d("backtest_wr"),
        "avg_t1_return": safe_mean_d("t1_return_pct"),
        "avg_t5_return": safe_mean_d("t5_return_pct"),
        "avg_t10_return": safe_mean_d("t10_return_pct"),
        "avg_t20_return": safe_mean_d("t20_return_pct"),
        "num_completed": sum(1 for x in items if x.get("status") == "completed"),
        "num_tracking": sum(1 for x in items if x.get("status") == "tracking"),
    })
with open(OUT / "by_date_summary.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(date_rows[0].keys()))
    w.writeheader()
    w.writerows(date_rows)
print(f"✅ by_date_summary.csv  ({len(date_rows)} 天)")

# ── 4. daily_top_pick.csv ─────────────────
top_picks = []
for d, items in sorted(by_date.items()):
    rank1 = next((x for x in items if x.get("rank") == 1), None)
    if rank1:
        top_picks.append({
            "date": d,
            "code": rank1["code"],
            "name": rank1["name"],
            "score": rank1["score"],
            "ev": rank1.get("backtest_ev"),
            "wr": rank1.get("backtest_wr"),
            "entry": rank1.get("entry_price"),
            "t1": rank1.get("t1_return_pct"),
            "t5": rank1.get("t5_return_pct"),
            "t10": rank1.get("t10_return_pct"),
            "t20": rank1.get("t20_return_pct"),
            "max_gain": rank1.get("max_gain_pct"),
            "max_dd": rank1.get("max_drawdown_pct"),
            "status": rank1.get("status"),
        })
with open(OUT / "daily_top_pick.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(top_picks[0].keys()))
    w.writeheader()
    w.writerows(top_picks)
print(f"✅ daily_top_pick.csv  ({len(top_picks)} 天的 rank 1)")

# ── 5. performance_ranked.csv （依最大實現報酬排序）─
perf = []
for r in recs:
    # 用 t20 > t10 > t5 > t1 找最新可用的
    for k in ("t20_return_pct", "t10_return_pct", "t5_return_pct", "t1_return_pct"):
        if r.get(k) is not None:
            perf.append({
                "date": r["snapshot_date"],
                "code": r["code"],
                "name": r["name"],
                "rank": r["rank"],
                "score": r["score"],
                "ev": r.get("backtest_ev"),
                "wr": r.get("backtest_wr"),
                "entry": r.get("entry_price"),
                "latest_return_pct": r[k],
                "latest_horizon": k.replace("_return_pct", ""),
                "max_gain": r.get("max_gain_pct"),
                "max_dd": r.get("max_drawdown_pct"),
                "status": r.get("status"),
            })
            break
perf.sort(key=lambda x: -(x["latest_return_pct"] or -999))
with open(OUT / "performance_ranked.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(perf[0].keys()))
    w.writeheader()
    w.writerows(perf)
print(f"✅ performance_ranked.csv  (依最新報酬排序)")

# ── 7. by_strategy_summary.csv （優化 C：找出最強的招）─
by_strat = defaultdict(list)
for r in recs:
    strat = r.get("strategy_type") or "unlabeled"
    by_strat[strat].append(r)
strat_rows = []
for strat, items in by_strat.items():
    def _sm_s(key):
        vals = [x[key] for x in items if x.get(key) is not None]
        return round(mean(vals), 2) if vals else None
    strat_rows.append({
        "strategy_type": strat,
        "strategy_label": items[0].get("strategy_label", ""),
        "num_recs": len(items),
        "avg_score": round(mean([x["score"] for x in items]), 2),
        "avg_ev": _sm_s("backtest_ev"),
        "avg_wr": _sm_s("backtest_wr"),
        "avg_t1_return": _sm_s("t1_return_pct"),
        "avg_t5_return": _sm_s("t5_return_pct"),
        "avg_t10_return": _sm_s("t10_return_pct"),
        "avg_t20_return": _sm_s("t20_return_pct"),
        "avg_max_gain": _sm_s("max_gain_pct"),
        "avg_max_drawdown": _sm_s("max_drawdown_pct"),
    })
strat_rows.sort(key=lambda x: -(x["avg_t10_return"] or -999))
if strat_rows:
    with open(OUT / "by_strategy_summary.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(strat_rows[0].keys()))
        w.writeheader()
        w.writerows(strat_rows)
    print(f"✅ by_strategy_summary.csv  ({len(strat_rows)} 個策略)")

# ── 6. README.md ──────────────────────────
completed = sum(1 for r in recs if r.get("status") == "completed")
tracking = sum(1 for r in recs if r.get("status") == "tracking")
t20_vals = [r["t20_return_pct"] for r in recs if r.get("t20_return_pct") is not None]
t10_vals = [r["t10_return_pct"] for r in recs if r.get("t10_return_pct") is not None]
t5_vals = [r["t5_return_pct"] for r in recs if r.get("t5_return_pct") is not None]

readme = f"""# 推薦股票歷史資料匯出

**匯出時間**：2026-07-05
**資料來源**：`data/recs_history.jsonl`
**日期範圍**：{min(r["snapshot_date"] for r in recs)} ~ {max(r["snapshot_date"] for r in recs)}
**總筆數**：{len(recs)} 筆（{completed} 筆已完成、{tracking} 筆追蹤中）

## 📁 檔案清單

| 檔案 | 說明 | 建議用途 |
|---|---|---|
| `all_recommendations.csv` | 所有 {len(recs)} 筆推薦，每筆一列 | 完整原始資料，Excel 分析用 |
| `by_stock_summary.csv` | 依股票匯總（{len(stock_rows)} 檔） | 找出「常被推薦」的股票 |
| `by_date_summary.csv` | 依日期匯總（{len(date_rows)} 天） | 看策略是否隨時間變好 |
| `daily_top_pick.csv` | 每日 rank 1 表現 | 只跟第一名的簡化策略 |
| `performance_ranked.csv` | 依實現報酬排序 | 找出最強/最弱推薦 |

## 📊 統計摘要

**T+1 平均報酬**：{mean(t5_vals):+.2f}% (n={{len(t5_vals)}})
**T+5 平均報酬**：{mean(t5_vals):+.2f}% (n={{len(t5_vals)}})
**T+10 平均報酬**：{mean(t10_vals):+.2f}% (n={{len(t10_vals)}})
**T+20 平均報酬**：{mean(t20_vals):+.2f}% (n={{len(t20_vals)}}) （僅已完成的推薦）

## 📖 欄位字典（all_recommendations.csv）

| 欄位 | 說明 |
|---|---|
| snapshot_date | 推薦當天日期 |
| rank | 當天排名（1 = 最強） |
| code | 股票代號 |
| name | 股票名稱 |
| score | 綜合分數（1-5） |
| backtest_ev | 回測期望值 % |
| backtest_wr | 回測勝率 (0-1) |
| backtest_n | 回測樣本數 |
| entry_price | 推薦當天收盤（進場價） |
| t1_price / t1_return_pct | T+1 收盤 / 報酬% |
| t5_price / t5_return_pct | T+5 收盤 / 報酬% |
| t10_price / t10_return_pct | T+10 收盤 / 報酬% |
| t20_price / t20_return_pct | T+20 收盤 / 報酬% |
| max_gain_pct | 20 天內最高漲幅% |
| max_drawdown_pct | 20 天內最大回檔% |
| status | tracking=追蹤中、completed=已滿20天 |

## 🎯 分析建議

1. **開 `performance_ranked.csv`** — 一眼看誰漲最多、誰跌最多
2. **開 `by_stock_summary.csv`** — 排序 `times_recommended` 找連續上榜的黑馬
3. **開 `by_date_summary.csv`** — 看 `avg_t5_return` 有沒有隨時間變好（策略優化了嗎？）
4. **開 `daily_top_pick.csv`** — 假設只買每日第一名，看報酬曲線

## 💡 Excel 快速篩選技巧

- 排序 `max_drawdown_pct` 找「安全」的股票（回檔小）
- 篩選 `score >= 4` 只看高分推薦
- 篩選 `status = completed` 只看已滿 20 天的可比較樣本
"""
(OUT / "README.md").write_text(readme, encoding="utf-8")
print(f"✅ README.md")

print(f"\n🎉 匯出完成 → {OUT}")
