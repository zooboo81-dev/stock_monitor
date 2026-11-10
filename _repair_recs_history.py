"""一次性修復 data/recs_history.jsonl（2026-11-11）

舊紀錄的三個量尺問題：
  1. 過期股價：雲端跑的時候 yfinance 還沒有當天日 K，entry_price 其實是「前一天」收盤
     → 標 quality=stale_price，data_date 改成真正用到的那一天
  2. 假日／週末重複快照：非交易日也照樣存了一份跟前一個交易日一樣的推薦
     → 標 quality=non_trading_day（前一個交易日沒有同一檔的才保留為 ok）
  3. T+N 用「扣六日」估交易日、而且用當下抓到的價格對舊的 entry_price（遇除權息會錯）
     → 全部用實際 K 棒重算（recs_snapshot.compute_outcomes）

做法：不刪任何一筆，只加欄位（quality / data_date / rule_version）並重算 T+N。
原檔備份到 data_export/recs_history_backup_YYYYMMDD.jsonl。

用法：py -3 _repair_recs_history.py          # 修復＋印前後對照
      py -3 _repair_recs_history.py --dry    # 只印不寫
"""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import recs_snapshot as rs

TOL = 0.0006


def _stats(rows: list[dict], key: str) -> str:
    v = [r[key] for r in rows if r.get(key) is not None]
    if not v:
        return "n=0"
    v2 = sorted(v)
    return (f"n={len(v):3d} 平均 {sum(v)/len(v):+6.2f}%  中位數 {v2[len(v2)//2]:+6.2f}%  "
            f"勝率 {sum(1 for x in v if x > 0)/len(v)*100:3.0f}%")


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    dry = "--dry" in sys.argv
    records = rs.load_history()
    print(f"讀進 {len(records)} 筆")
    print("【修復前】（原檔數字，含污染）")
    for t in (1, 5, 10, 20):
        print(f"  T+{t:<2d} {_stats(records, f't{t}_return_pct')}")

    start = date(2026, 4, 1)
    bars = rs.fetch_bars(sorted({r["code"] for r in records}), start)
    taiex = rs.fetch_taiex(start)
    print(f"抓到 {len(bars)} 檔 K 棒")

    seen: set[tuple[str, str]] = set()   # (data_date, code) 已有乾淨紀錄
    counts = {"ok": 0, "stale_price": 0, "non_trading_day": 0, "no_data": 0}
    for r in sorted(records, key=lambda x: x["snapshot_date"]):
        r.setdefault("rule_version", 1)
        wr = r.get("backtest_wr")
        if isinstance(wr, (int, float)) and 0 < wr <= 1:
            r["backtest_wr"] = round(wr * 100, 1)       # 舊資料有 0.46 / 58.8 兩種單位，統一成 %
        b = bars.get(r["code"])
        if b is None or b.empty:
            r["quality"] = "no_data"
            counts["no_data"] += 1
            continue
        d = pd.Timestamp(r["snapshot_date"])
        idx = int(b.index.searchsorted(d, side="right")) - 1
        if idx < 1:
            r["quality"] = "no_data"
            counts["no_data"] += 1
            continue
        entry = float(r.get("entry_price") or 0)
        if b.index[idx] == d:
            c0, cp = float(b["Close"].iloc[idx]), float(b["Close"].iloc[idx - 1])
            hit_today = abs(entry - c0) / c0 < TOL
            hit_prev = abs(entry - cp) / cp < TOL
            if hit_prev and not hit_today:
                quality, data_idx = "stale_price", idx - 1
            else:
                quality, data_idx = "ok", idx
        else:
            quality, data_idx = "non_trading_day", idx   # 快照日不是交易日
        data_date = b.index[data_idx].strftime("%Y-%m-%d")
        key = (data_date, r["code"])
        if quality == "non_trading_day" and key not in seen:
            quality = "ok"          # 前一個交易日沒存過這檔 → 這筆是唯一的，留著
        if quality == "ok":
            if key in seen:
                quality = "non_trading_day"
            else:
                seen.add(key)
        r["quality"] = quality
        r["data_date"] = data_date
        counts[quality] += 1
        # 清掉舊的結果欄位再重算
        for k in list(r.keys()):
            if k.startswith("t") and k[1:2].isdigit() and ("_price" in k or "_return" in k or "_excess" in k):
                r[k] = None
        r["max_gain_pct"] = r["max_drawdown_pct"] = None
        r["status"] = "tracking"
        rs.compute_outcomes(r, b, taiex)

    print(f"分類：{counts}")
    ok = [r for r in records if rs.is_ok(r)]
    print(f"【修復後】只算乾淨紀錄 {len(ok)} 筆")
    for t in (1, 5, 10, 20):
        print(f"  T+{t:<2d} 收盤→收盤       {_stats(ok, f't{t}_return_pct')}")
        print(f"       隔日開盤買、扣成本 {_stats(ok, f't{t}_return_realistic_pct')}")
        print(f"       贏大盤多少         {_stats(ok, f't{t}_excess_pct')}")
    stale = [r for r in records if r.get("quality") == "stale_price"]
    if stale:
        print(f"【過期股價那 {len(stale)} 筆】從真正的資料日起算：")
        for t in (1, 5, 20):
            print(f"  T+{t:<2d} 隔日開盤買、扣成本 {_stats(stale, f't{t}_return_realistic_pct')}")

    if dry:
        print("（--dry：不寫檔）")
        return
    bak = Path("data_export") / f"recs_history_backup_{date.today():%Y%m%d}.jsonl"
    bak.parent.mkdir(exist_ok=True)
    if not bak.exists():
        shutil.copy2(rs.HIST_FILE, bak)
        print(f"備份 → {bak}")
    rs.save_history(records)
    print(f"✅ 已寫回 {rs.HIST_FILE}")


if __name__ == "__main__":
    main()
