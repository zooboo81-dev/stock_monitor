"""一次性：回填已知的歷史推薦（5/29 + 6/8 + 6/11）"""
import json
import yfinance as yf
from datetime import date, timedelta
from pathlib import Path

HIST_FILE = Path("data/recs_history.jsonl")

# 已知的歷史推薦
KNOWN = [
    # 5/29 推薦
    {"date": "2026-05-29", "rank": 1, "code": "3149", "name": "正達光電", "score": 3, "ev": 3.67, "wr": 0.46, "n": 13},
    # 6/8 推薦榜（評分高低排序）
    {"date": "2026-06-08", "rank": 1, "code": "8021", "name": "尖點", "score": 4, "ev": 12.59, "wr": 0.76, "n": 17},
    {"date": "2026-06-08", "rank": 2, "code": "3026", "name": "禾伸堂", "score": 4, "ev": 8.44, "wr": 0.54, "n": 11},
    {"date": "2026-06-08", "rank": 3, "code": "8096", "name": "擎亞", "score": 3, "ev": 4.85, "wr": 0.57, "n": 14},
    {"date": "2026-06-08", "rank": 4, "code": "8996", "name": "高力", "score": 7, "ev": 4.67, "wr": 0.71, "n": 17},
    {"date": "2026-06-08", "rank": 5, "code": "3376", "name": "新日興", "score": 3, "ev": 4.62, "wr": 0.67, "n": 12},
    {"date": "2026-06-08", "rank": 6, "code": "1303", "name": "南亞", "score": 5, "ev": 4.46, "wr": 0.57, "n": 14},
    {"date": "2026-06-08", "rank": 7, "code": "3016", "name": "嘉晶", "score": 4, "ev": 4.13, "wr": 0.38, "n": 13},
    {"date": "2026-06-08", "rank": 8, "code": "2368", "name": "金像電", "score": 3, "ev": 3.62, "wr": 0.67, "n": 18},
    # 6/11 推薦
    {"date": "2026-06-11", "rank": 1, "code": "3149", "name": "正達光電", "score": 3, "ev": 3.67, "wr": 0.46, "n": 13},
    # 6/12 凱美 — 雖然被新規則擋下，但回填看看實際表現
    {"date": "2026-06-12", "rank": 99, "code": "2375", "name": "凱美", "score": 7, "ev": 3.67, "wr": 0.46, "n": 13},
]


def fetch_close_at(code, target_date):
    """抓最接近 target_date 但不晚於它的收盤"""
    for suffix in [".TW", ".TWO"]:
        try:
            start = target_date - timedelta(days=3)
            end = target_date + timedelta(days=3)
            h = yf.Ticker(f"{code}{suffix}").history(start=start, end=end, auto_adjust=False).dropna(subset=["Close"])
            if h.empty:
                continue
            h.index = h.index.date
            valid = [d for d in h.index if d <= target_date]
            if valid:
                return float(h.loc[max(valid), "Close"])
        except Exception:
            continue
    return None


def fetch_range(code, start_date, end_date):
    for suffix in [".TW", ".TWO"]:
        try:
            h = yf.Ticker(f"{code}{suffix}").history(
                start=start_date, end=end_date + timedelta(days=2), auto_adjust=False
            ).dropna(subset=["Close"])
            if h.empty:
                continue
            return {"high": float(h["High"].max()), "low": float(h["Low"].min())}
        except Exception:
            continue
    return None


def trading_days_after(d, days):
    cur = d
    n = 0
    while n < days:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return cur


# 載入現有 jsonl
existing = []
if HIST_FILE.exists():
    for line in HIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        existing.append(json.loads(line))
existing_keys = {(r["snapshot_date"], r["code"]) for r in existing}

today = date.today()
added = 0

for k in KNOWN:
    snap_date = k["date"]
    if (snap_date, k["code"]) in existing_keys:
        continue
    snap_dt = date.fromisoformat(snap_date)

    # 進場價（snap_date 收盤）
    entry = fetch_close_at(k["code"], snap_dt)
    if not entry:
        print(f"  ⚠️ 抓不到 {k['code']} {k['date']} 進場價 — 跳過")
        continue

    rec = {
        "snapshot_date": snap_date,
        "rank": k["rank"],
        "code": k["code"],
        "name": k["name"],
        "score": k["score"],
        "backtest_ev": k["ev"],
        "backtest_wr": k["wr"],
        "backtest_n": k["n"],
        "entry_price": round(entry, 2),
        "t1_price": None, "t1_return_pct": None,
        "t5_price": None, "t5_return_pct": None,
        "t10_price": None, "t10_return_pct": None,
        "t20_price": None, "t20_return_pct": None,
        "max_gain_pct": None, "max_drawdown_pct": None,
        "status": "tracking",
    }

    # 算 T+1/5/10/20
    days_elapsed = 0
    cur = snap_dt
    while cur < today:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days_elapsed += 1

    for t in [1, 5, 10, 20]:
        if days_elapsed >= t:
            target = trading_days_after(snap_dt, t)
            px = fetch_close_at(k["code"], target)
            if px:
                rec[f"t{t}_price"] = round(px, 2)
                rec[f"t{t}_return_pct"] = round((px - entry) / entry * 100, 2)

    # 區間最高最低
    end_check = min(today, snap_dt + timedelta(days=int(20 * 1.5)))
    rng = fetch_range(k["code"], snap_dt, end_check)
    if rng:
        rec["max_gain_pct"] = round((rng["high"] - entry) / entry * 100, 2)
        rec["max_drawdown_pct"] = round((rng["low"] - entry) / entry * 100, 2)

    if days_elapsed >= 20 and rec.get("t20_price") is not None:
        rec["status"] = "completed"

    existing.append(rec)
    added += 1
    def f(v): return f"{v:+.1f}%" if v is not None else "---"
    print(f"  ✅ {k['date']} {k['code']} {k['name']:<10} 進場{entry:.2f}  "
          f"T+1 {f(rec.get('t1_return_pct')):<7}  T+5 {f(rec.get('t5_return_pct')):<7}  "
          f"max{f(rec.get('max_gain_pct')):<7}  min{f(rec.get('max_drawdown_pct')):<7}")

# 寫回（按日期排序）
existing.sort(key=lambda r: (r["snapshot_date"], r.get("rank", 99)))
HIST_FILE.write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in existing) + "\n",
    encoding="utf-8",
)

print(f"\n✅ 回填完成，新增 {added} 筆，總共 {len(existing)} 筆")
