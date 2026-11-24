"""持股清單（portfolio.csv）的共用讀法 — 要算損益、停損的程式一律從這裡拿「真的有持股」的列

為什麼要有這支（2026-11-24）：
  portfolio.csv 裡有「定存區預留列」（玉山金、中華電…股數 0、成本 0，還沒買），
  以後也可能出現「賣光但列還留著」（股數 0、成本還在）、停損空白、數字被存成 1000.0 之類。
  以前每支程式各自讀 portfolio.csv、各自記得要跳過這些列：
    7/07 儀表板修過、7/14 盤中警示修過，晨間摘要沒修 → 拿成本 0 當除數整支當掉
    （雲端 9/11 起每天失敗沒人發現；11/24 改到這台電腦跑才被推播抓到）。
  所以統一在這裡處理。新寫的程式要讀持股，用 load_positions()，就不會再漏。
  job_selftest.py 會拿一份塞了怪資料的 portfolio.csv 把每個排程跑一遍，確認真的不會當掉。
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

PORTFOLIO_FILE = Path(__file__).resolve().parent / "portfolio.csv"
COLUMNS = ["code", "name", "market", "shares", "cost", "category",
           "stop_loss", "entry_date", "hold_type"]


def to_num(x, default: float = 0.0) -> float:
    """把 portfolio.csv 的一格轉成數字。空白、NaN、"1,000"、"1000.0"、亂打的字都不會讓程式當掉（回 default）"""
    if x is None or isinstance(x, bool):
        return default
    if isinstance(x, (int, float)):
        v = float(x)
    else:
        s = str(x).strip().replace(",", "")
        if not s or s.lower() in ("nan", "none", "null", "-"):
            return default
        try:
            v = float(s)
        except ValueError:
            return default
    return default if (math.isnan(v) or math.isinf(v)) else v


def load_positions(path=None, active_only: bool = True) -> pd.DataFrame:
    """讀 portfolio.csv（代號保持字串，0050 不會變成 50），另外加三個一定是數字的欄位：
         shares_n 股數、cost_n 成本、stop_n 固定停損（空白＝0，代表沒設）
    active_only=True（預設）：只留「股數 > 0 而且成本 > 0」的列 ＝ 真的有持股、算得出損益的。
    檔案不在 → 回空表（欄位齊全，呼叫的程式不用另外判斷）"""
    p = Path(path) if path else PORTFOLIO_FILE
    empty = pd.DataFrame(columns=COLUMNS + ["shares_n", "cost_n", "stop_n"])
    if not p.exists():
        return empty
    df = pd.read_csv(p, dtype={"code": str})
    if "code" not in df.columns:
        return empty
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    df["code"] = df["code"].fillna("").astype(str).str.strip()
    df = df[df["code"] != ""].copy()
    df["name"] = df["name"].fillna("").astype(str)
    df["shares_n"] = [to_num(v) for v in df["shares"]]
    df["cost_n"] = [to_num(v) for v in df["cost"]]
    df["stop_n"] = [max(0.0, to_num(v)) for v in df["stop_loss"]]
    if active_only:
        df = df[(df["shares_n"] > 0) & (df["cost_n"] > 0)]
    return df.reset_index(drop=True)


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    all_rows = load_positions(active_only=False)
    act = load_positions()
    print(f"portfolio.csv 共 {len(all_rows)} 列，真的有持股 {len(act)} 檔：")
    for _, r in act.iterrows():
        print(f"  {r['code']:<6}{r['name']:<8} 股數 {r['shares_n']:>8,.0f}  成本 {r['cost_n']:>9.2f}  停損 {r['stop_n']:.2f}")
    skipped = all_rows[~all_rows["code"].isin(act["code"])]
    if len(skipped):
        print("跳過（預留列／賣光留著／資料不完整）：" + "、".join(f"{c} {n}" for c, n in zip(skipped["code"], skipped["name"])))
