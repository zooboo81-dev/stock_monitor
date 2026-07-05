"""推薦表現追蹤器 — 每日 snapshot + 更新歷史推薦的 T+N 表現

設計：
  1. 每日 14:30 排程跑（盤後）
  2. snapshot 當下推薦榜 top 10，附評分、EV、現價
  3. 同時走訪歷史 snapshot，更新 T+1/T+5/T+10/T+20 表現
  4. 連續 20 個交易日後該筆「完成」，進入回測統計

追蹤的時間框架：
  T+1   隔日（看跳空效果）
  T+5   一週後
  T+10  兩週後
  T+20  一個月後（完成節點）

資料檔：data/recs_history.jsonl（append-only）
每行一筆 JSON：
{
  "snapshot_date": "2026-06-12",
  "rank": 1, "code": "8021", "name": "尖點",
  "score": 4, "backtest_ev": 12.59, "backtest_wr": 0.76, "backtest_n": 17,
  "entry_price": 100.5,
  "t1_price": null,  "t1_return_pct": null,
  "t5_price": null,  "t5_return_pct": null,
  "t10_price": null, "t10_return_pct": null,
  "t20_price": null, "t20_return_pct": null,
  "max_drawdown_pct": null, "max_gain_pct": null,
  "status": "tracking"   # tracking → completed
}

用法：
  python recs_snapshot.py             # 完整流程：snapshot + 更新
  python recs_snapshot.py --no-snap   # 只更新，不新增
  python recs_snapshot.py --status    # 顯示追蹤狀態
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
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

import pandas as pd
import requests
import yfinance as yf

HIST_FILE = Path("data/recs_history.jsonl")
HIST_FILE.parent.mkdir(exist_ok=True)


def _twse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0",
                      "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    except Exception:
        pass
    return s


# ---------- 讀寫 jsonl ----------

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


def save_history(records: list[dict]) -> None:
    HIST_FILE.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------- snapshot 當日推薦榜 ----------

def snapshot_today(top_n: int = 10) -> list[dict]:
    """跑 discover.top_picks，回傳 snapshot 紀錄"""
    from discover import top_picks
    from institutional import fetch_institutional_history

    today = date.today().isoformat()
    session = _twse_session()

    # 持倉
    portfolio_codes = set()
    pf_file = Path("portfolio.csv")
    if pf_file.exists():
        df = pd.read_csv(pf_file, dtype={"code": str})
        portfolio_codes = set(df["code"].tolist())

    # 法人（保持 dict 為空也可運作，但盡量抓）
    instit = {}
    try:
        instit_data, _ = fetch_institutional_history(session, days=10)
        instit = instit_data or {}
    except Exception:
        pass

    picks = top_picks(
        portfolio_codes=portfolio_codes,
        instit_by_code=instit,
        session=session,
        min_score=3,
        top_n=top_n,
    )

    records = []
    for rank, p in enumerate(picks, 1):
        records.append({
            "snapshot_date": today,
            "rank": rank,
            "code": p["code"],
            "name": p.get("name", ""),
            "score": p.get("score", 0),
            "backtest_ev": round(p.get("backtest_ev", 0.0), 2),
            "backtest_wr": round(p.get("backtest_wr", 0.0), 3),
            "backtest_n": p.get("backtest_n", 0),
            "entry_price": round(p.get("price", 0.0), 2),
            "t1_price": None, "t1_return_pct": None,
            "t5_price": None, "t5_return_pct": None,
            "t10_price": None, "t10_return_pct": None,
            "t20_price": None, "t20_return_pct": None,
            "max_gain_pct": None, "max_drawdown_pct": None,
            "status": "tracking",
        })
    return records


# ---------- 更新 T+N 表現 ----------

def _trading_days_between(start: date, end: date) -> int:
    """粗估交易日數（週末扣除，沒考慮國定假日）"""
    n = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def _fetch_close_at(code: str, target_date: date) -> float | None:
    """抓 target_date 當日（或最近交易日）收盤價"""
    ticker = f"{code}.TW"
    try:
        # 取 target_date 前後 5 天的歷史
        start = target_date - timedelta(days=3)
        end = target_date + timedelta(days=3)
        h = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False).dropna(subset=["Close"])
        if h.empty:
            # 上櫃改 .TWO
            ticker2 = f"{code}.TWO"
            h = yf.Ticker(ticker2).history(start=start, end=end, auto_adjust=False).dropna(subset=["Close"])
        if h.empty:
            return None
        # 取最接近 target_date 但不晚於它的那天
        h.index = h.index.date
        valid = [d for d in h.index if d <= target_date]
        if not valid:
            return None
        return float(h.loc[max(valid), "Close"])
    except Exception:
        return None


def _fetch_price_range(code: str, start_date: date, end_date: date) -> dict | None:
    """抓區間最高/最低/收盤"""
    ticker = f"{code}.TW"
    try:
        h = yf.Ticker(ticker).history(start=start_date, end=end_date + timedelta(days=2),
                                       auto_adjust=False).dropna(subset=["Close"])
        if h.empty:
            ticker2 = f"{code}.TWO"
            h = yf.Ticker(ticker2).history(start=start_date, end=end_date + timedelta(days=2),
                                            auto_adjust=False).dropna(subset=["Close"])
        if h.empty:
            return None
        return {
            "high": float(h["High"].max()),
            "low": float(h["Low"].min()),
            "last": float(h["Close"].iloc[-1]),
        }
    except Exception:
        return None


def update_records(records: list[dict]) -> int:
    """走訪所有 tracking 中的紀錄，更新 T+N。回傳更新筆數"""
    today = date.today()
    updated = 0

    for rec in records:
        if rec.get("status") == "completed":
            continue

        snap_date = datetime.strptime(rec["snapshot_date"], "%Y-%m-%d").date()
        entry = rec.get("entry_price")
        if not entry:
            continue

        days_elapsed = _trading_days_between(snap_date, today)
        code = rec["code"]

        # T+1 ~ T+20 檢查
        for t in [1, 5, 10, 20]:
            key = f"t{t}_price"
            if rec.get(key) is not None:
                continue
            if days_elapsed < t:
                continue
            # 計算 T+t 的日期
            target = snap_date
            d = 0
            while d < t:
                target += timedelta(days=1)
                if target.weekday() < 5:
                    d += 1
            px = _fetch_close_at(code, target)
            if px:
                rec[key] = round(px, 2)
                rec[f"t{t}_return_pct"] = round((px - entry) / entry * 100, 2)
                updated += 1

        # 更新區間最高/最低
        if days_elapsed >= 1:
            end_check = min(today, snap_date + timedelta(days=int(20 * 1.5)))  # 20 交易日 ≈ 28 自然日
            rng = _fetch_price_range(code, snap_date, end_check)
            if rng:
                rec["max_gain_pct"] = round((rng["high"] - entry) / entry * 100, 2)
                rec["max_drawdown_pct"] = round((rng["low"] - entry) / entry * 100, 2)

        # T+20 完成 → 標記
        if days_elapsed >= 20 and rec.get("t20_price") is not None:
            rec["status"] = "completed"

    return updated


# ---------- 變動偵測 + Telegram 推播 ----------

def _notify_changes(prev_date: str | None, prev: list[dict],
                    today: str, today_picks: list[dict]) -> None:
    """對比昨日 vs 今日推薦榜，把變動推 Telegram

    變動類型：
      🆕 新進榜：今日有、昨日沒有
      ❌ 掉出榜：昨日有、今日沒有
      📈 升等：兩日都在但評分提升 ≥ 2 分
    """
    today_map = {r["code"]: r for r in today_picks}
    prev_map = {r["code"]: r for r in prev}

    new_in = [today_map[c] for c in today_map if c not in prev_map]
    dropped = [prev_map[c] for c in prev_map if c not in today_map]
    upgraded = []
    for c in today_map:
        if c in prev_map:
            d_score = today_map[c]["score"] - prev_map[c]["score"]
            if d_score >= 2:
                upgraded.append((today_map[c], prev_map[c]["score"], d_score))

    # 沒任何變動 → 不推
    if not (new_in or dropped or upgraded):
        return

    lines = ["📊 推薦榜變動"]
    if prev_date:
        lines.append(f"（{prev_date} → {today}）")
    lines.append("")

    if new_in:
        lines.append(f"🆕 新進榜 {len(new_in)} 檔")
        for p in new_in:
            wr = p.get("backtest_wr", 0)
            wr = wr * 100 if wr < 1 else wr
            lines.append(
                f"  {p['code']} {p['name'][:8]}  "
                f"評分{p['score']:+d}  EV{p.get('backtest_ev', 0):+.1f}%  勝率{wr:.0f}%"
            )
        lines.append("")

    if dropped:
        lines.append(f"❌ 掉出榜 {len(dropped)} 檔")
        for p in dropped:
            lines.append(f"  {p['code']} {p['name'][:8]}  原評分{p['score']:+d}")
        lines.append("")

    if upgraded:
        lines.append(f"📈 評分升級 {len(upgraded)} 檔")
        for p, old, delta in upgraded:
            lines.append(f"  {p['code']} {p['name'][:8]}  {old:+d} → {p['score']:+d} (+{delta})")
        lines.append("")

    lines.append("⏰ 24h 訊號保鮮期")
    lines.append("超過 24 小時請重跑確認")

    body = "\n".join(lines)

    # 推 Telegram
    try:
        import telegram_notify
        ok = telegram_notify.send("📊 推薦榜變動", body)
        print(f"✅ Telegram 推送變動：{'成功' if ok else '失敗'}")
    except Exception as e:
        print(f"⚠️ Telegram 推送失敗：{e}")


# ---------- 主流程 ----------

def main():
    args = sys.argv[1:]
    no_snap = "--no-snap" in args
    show_status = "--status" in args

    records = load_history()

    if show_status:
        total = len(records)
        tracking = sum(1 for r in records if r.get("status") == "tracking")
        completed = sum(1 for r in records if r.get("status") == "completed")
        print(f"📈 推薦追蹤狀態")
        print(f"  總筆數: {total}")
        print(f"  追蹤中: {tracking}")
        print(f"  已完成: {completed}")
        if completed > 0:
            done = [r for r in records if r.get("status") == "completed"]
            avg = sum(r.get("t20_return_pct", 0) for r in done) / len(done)
            wins = sum(1 for r in done if r.get("t20_return_pct", 0) > 0)
            print(f"  T+20 平均報酬: {avg:+.2f}%")
            print(f"  T+20 勝率: {wins / len(done) * 100:.0f}%")
        return

    # 1) snapshot 當日推薦 + 偵測變動
    if not no_snap:
        # 同日不重複 snapshot（避免重跑造成重複資料）
        today = date.today().isoformat()
        existing_today = sum(1 for r in records if r.get("snapshot_date") == today)
        if existing_today > 0:
            print(f"今日已 snapshot {existing_today} 筆，跳過新增")
        else:
            new_records = snapshot_today(top_n=10)
            if new_records:
                # 找上一個 snapshot 日（最近的不同日期）
                prev_dates = sorted({r["snapshot_date"] for r in records}, reverse=True)
                prev_date = prev_dates[0] if prev_dates else None
                prev_records = [r for r in records if r.get("snapshot_date") == prev_date] if prev_date else []

                records.extend(new_records)
                print(f"✅ 新增 {len(new_records)} 筆推薦 snapshot")

                # 變動偵測 + Telegram 推播
                _notify_changes(prev_date, prev_records, today, new_records)
            else:
                print("ℹ️ 今日推薦榜 0 檔（沒新增）")

    # 2) 更新 T+N 表現
    n_updated = update_records(records)
    if n_updated > 0:
        print(f"✅ 更新 {n_updated} 筆 T+N 表現")

    # 3) 寫回
    save_history(records)
    print(f"📝 寫入 {HIST_FILE}")


if __name__ == "__main__":
    main()
