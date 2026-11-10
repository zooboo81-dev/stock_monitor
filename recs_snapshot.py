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

2026-11-11 改版（量尺修正，舊紀錄已用 _repair_recs_history.py 重算）：
  - snapshot_date 改用「評分實際用到的最新一根 K 的日期」(data_date)，不再用主機的今天
    → 假日／週末不會再重複快照；雲端排程延遲到隔天凌晨跑也不會記錯日
  - T+N 一律用實際交易日的 K 棒去數（以前用「扣掉六日」估，遇國定假日會錯）
  - t{N}_return_pct          = 訊號日收盤 → T+N 收盤（理論值）
    t{N}_return_realistic_pct = 隔日開盤買 → T+N 收盤賣，扣來回成本 0.585%（做得到的數字）
    t{N}_excess_pct           = 理論值 − 同期間加權指數（有沒有贏大盤）
  - quality 欄位：ok／stale_price（當時拿到的是前一天股價）／non_trading_day（假日重複快照）
    統計時只算 ok
  - 每次執行寫 data/recs_last_run.json（資料日、幾檔、大盤閘門有沒有擋），儀表板讀它

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
LAST_RUN_FILE = Path("data/recs_last_run.json")
COST_PCT = 0.585          # 來回手續費＋證交稅
RULE_VERSION = 2          # 2 = 2026-11-11 起：大盤閘門＋個股 T=5＋KD 不計分


def is_ok(rec: dict) -> bool:
    """這筆紀錄能不能拿來算戰績（排除過期股價、假日重複快照）"""
    return rec.get("quality", "ok") == "ok"


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

def snapshot_today(top_n: int = 10) -> tuple[list[dict], dict]:
    """跑 discover.top_picks，回傳 (snapshot 紀錄, 執行資訊)"""
    import discover
    from discover import top_picks
    from institutional import fetch_institutional_history
    from market_data import taiwan_today

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
    run = dict(discover.LAST_RUN)
    mkt = run.get("market") or {}
    # 快照日 = 評分實際用到的最新一根 K 的日期（不是主機的今天）
    data_date = (max((p["data_date"] for p in picks), default=None)
                 or run.get("data_date") or taiwan_today().isoformat())

    records = []
    for rank, p in enumerate(picks, 1):
        tr = p.get("trend") or {}
        records.append({
            "snapshot_date": data_date,
            "data_date": p.get("data_date", data_date),
            "rank": rank,
            "code": p["code"],
            "name": p.get("name", ""),
            "score": p.get("score", 0),
            "backtest_ev": round(p.get("backtest_ev", 0.0), 2),
            "backtest_wr": round(p.get("backtest_wr", 0.0), 3),
            "backtest_n": p.get("backtest_n", 0),
            "entry_price": round(p.get("price", 0.0), 2),
            "atr14": round(p["atr14"], 3) if p.get("atr14") else None,
            "strategy_type": (p.get("strategy") or {}).get("type"),   # 優化 C
            "strategy_label": (p.get("strategy") or {}).get("label"),
            "dim_scores": p.get("dim_scores"),   # 優化 B
            "trend_t": tr.get("t"),
            "market_t": mkt.get("t"),
            "market_side": mkt.get("side"),
            "rule_version": RULE_VERSION,
            "quality": "ok",
            "t1_price": None, "t1_return_pct": None,
            "t5_price": None, "t5_return_pct": None,
            "t10_price": None, "t10_return_pct": None,
            "t20_price": None, "t20_return_pct": None,
            "max_gain_pct": None, "max_drawdown_pct": None,
            "status": "tracking",
        })
    info = {
        "data_date": data_date,
        "n_picks": len(records),
        "gate_blocked": bool(run.get("gate_blocked")),
        "market_text": mkt.get("text"), "market_t": mkt.get("t"),
        "market_side": mkt.get("side"), "market_side_since": mkt.get("side_since"),
        "patched_bars": run.get("patched", 0),
        "n_scored": run.get("n_scored", 0), "n_trend_ok": run.get("n_trend_ok", 0),
        "rule_version": RULE_VERSION,
    }
    return records, info


# ---------- 更新 T+N 表現（用實際 K 棒數交易日）----------

def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def fetch_bars(codes: list[str], start: date) -> dict[str, pd.DataFrame]:
    """一次抓多檔日 K（先 .TW 再 .TWO），並用官方收盤補上最新一根。"""
    out: dict[str, pd.DataFrame] = {}
    if not codes:
        return out

    def _dl(tickers: list[str]) -> dict[str, pd.DataFrame]:
        got: dict[str, pd.DataFrame] = {}
        try:
            d = yf.download(tickers, start=start.isoformat(), auto_adjust=False, progress=False,
                            group_by="ticker", threads=True)
        except Exception:
            return got
        for t in tickers:
            try:
                x = d[t] if isinstance(d.columns, pd.MultiIndex) else d
                x = x.dropna(subset=["Close"])
                if len(x) >= 1:
                    got[t.split(".")[0]] = _strip_tz(x)
            except Exception:
                continue
        return got

    out.update(_dl([f"{c}.TW" for c in codes]))
    missing = [c for c in codes if c not in out]
    if missing:
        out.update(_dl([f"{c}.TWO" for c in missing]))
    try:
        from market_data import patch_latest_bars
        out, _, _ = patch_latest_bars(out, _twse_session())
    except Exception:
        pass
    return out


def fetch_taiex(start: date) -> pd.DataFrame | None:
    try:
        t = yf.Ticker("^TWII").history(start=start.isoformat(), auto_adjust=False).dropna(subset=["Close"])
        return _strip_tz(t) if not t.empty else None
    except Exception:
        return None


def compute_outcomes(rec: dict, bars: pd.DataFrame, taiex: pd.DataFrame | None) -> bool:
    """用 K 棒重算一筆紀錄的 T+N（就地修改 rec）。回傳是否有任何欄位被填。
    錨點 = data_date（沒有就用 snapshot_date）當天或之前最近的一根。
    報酬全部用同一條價格序列的比值算，之後除權息調整也不會錯。
    """
    anchor = pd.Timestamp(rec.get("data_date") or rec["snapshot_date"])
    idx = int(bars.index.searchsorted(anchor, side="right")) - 1
    if idx < 0:
        return False
    c0 = float(bars["Close"].iloc[idx])
    if c0 <= 0:
        return False
    n = len(bars)
    o1 = float(bars["Open"].iloc[idx + 1]) if idx + 1 < n else None
    changed = False

    t_idx = None
    if taiex is not None and not taiex.empty:
        t_idx = int(taiex.index.searchsorted(bars.index[idx], side="right")) - 1
        if t_idx < 0:
            t_idx = None

    if o1:
        rec["next_open"] = round(o1, 2)
        rec["gap_pct"] = round((o1 / c0 - 1) * 100, 2)

    for t in (1, 5, 10, 20):
        if idx + t >= n:
            continue
        ct = float(bars["Close"].iloc[idx + t])
        rec[f"t{t}_price"] = round(ct, 2)
        rec[f"t{t}_return_pct"] = round((ct / c0 - 1) * 100, 2)
        if o1:
            rec[f"t{t}_return_realistic_pct"] = round((ct / o1 - 1) * 100 - COST_PCT, 2)
        if t_idx is not None and t_idx + t < len(taiex):
            b = (float(taiex["Close"].iloc[t_idx + t]) / float(taiex["Close"].iloc[t_idx]) - 1) * 100
            rec[f"t{t}_excess_pct"] = round(rec[f"t{t}_return_pct"] - b, 2)
        changed = True

    if idx + 1 < n:
        win = bars.iloc[idx + 1: idx + 21]
        rec["max_gain_pct"] = round((float(win["High"].max()) / c0 - 1) * 100, 2)
        rec["max_drawdown_pct"] = round((float(win["Low"].min()) / c0 - 1) * 100, 2)

    if idx + 20 < n and rec.get("t20_price") is not None:
        rec["status"] = "completed"
    return changed


def update_records(records: list[dict]) -> int:
    """更新所有 tracking 中的紀錄。回傳更新筆數"""
    todo = [r for r in records if r.get("status") != "completed" and r.get("entry_price")]
    if not todo:
        return 0
    start = min(datetime.strptime(r.get("data_date") or r["snapshot_date"], "%Y-%m-%d").date()
                for r in todo) - timedelta(days=10)
    bars = fetch_bars(sorted({r["code"] for r in todo}), start)
    taiex = fetch_taiex(start)
    updated = 0
    for rec in todo:
        b = bars.get(rec["code"])
        if b is None or b.empty:
            continue
        before = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        compute_outcomes(rec, b, taiex)
        if json.dumps(rec, sort_keys=True, ensure_ascii=False) != before:
            updated += 1
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

    lines.append("📏 規則：大盤多方＋個股⬆多頭才上榜")
    lines.append("回測 20 日：平均 +2.5%、上漲率約五成（不保證會漲，務必設停損）")

    body = "\n".join(lines)

    # 推 Telegram
    try:
        import telegram_notify
        ok = telegram_notify.send("📊 推薦榜變動", body)
        print(f"✅ Telegram 推送變動：{'成功' if ok else '失敗'}")
    except Exception as e:
        print(f"⚠️ Telegram 推送失敗：{e}")


# ---------- 主流程 ----------

def _summary(records: list[dict]) -> None:
    ok = [r for r in records if is_ok(r)]
    print(f"📈 推薦追蹤狀態（只算乾淨紀錄 {len(ok)}/{len(records)} 筆）")
    for label, rows in (("全部", ok),
                        ("舊規則 v1", [r for r in ok if r.get("rule_version", 1) == 1]),
                        ("新規則 v2", [r for r in ok if r.get("rule_version", 1) == 2])):
        if not rows:
            continue
        print(f"  ── {label}（{len(rows)} 筆）")
        for t in (5, 10, 20):
            real = [r[f"t{t}_return_realistic_pct"] for r in rows if r.get(f"t{t}_return_realistic_pct") is not None]
            exc = [r[f"t{t}_excess_pct"] for r in rows if r.get(f"t{t}_excess_pct") is not None]
            if real:
                print(f"     T+{t:<2d} 隔日開盤買、扣成本：平均 {sum(real)/len(real):+.2f}%  "
                      f"勝率 {sum(1 for v in real if v > 0)/len(real)*100:.0f}%  (n={len(real)})"
                      + (f"  ｜贏大盤 {sum(exc)/len(exc):+.2f}%" if exc else ""))


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = sys.argv[1:]
    no_snap = "--no-snap" in args
    show_status = "--status" in args

    records = load_history()

    if show_status:
        _summary(records)
        return

    # 1) snapshot 當日推薦 + 偵測變動
    if not no_snap:
        new_records, info = snapshot_today(top_n=10)
        data_date = info["data_date"]
        existing = sum(1 for r in records if r.get("snapshot_date") == data_date)
        print(f"資料日 {data_date}｜大盤 {info.get('market_text')}（{info.get('market_side')}）"
              f"｜官方補 K {info.get('patched_bars')} 檔｜算分 {info.get('n_scored')} 檔、"
              f"多頭結構 {info.get('n_trend_ok')} 檔 → 上榜 {len(new_records)} 檔")
        if info["gate_blocked"]:
            print("⛔ 大盤在空方 → 今日不推薦（回測：空方時推薦 20 日平均 -1.9%、上漲率 40%）")
        elif existing > 0:
            print(f"資料日 {data_date} 已 snapshot {existing} 筆，跳過新增（假日或重跑）")
            info["n_picks"] = existing
        elif new_records:
            prev_dates = sorted({r["snapshot_date"] for r in records}, reverse=True)
            prev_date = prev_dates[0] if prev_dates else None
            prev_records = [r for r in records if r.get("snapshot_date") == prev_date] if prev_date else []
            records.extend(new_records)
            print(f"✅ 新增 {len(new_records)} 筆推薦 snapshot")
            _notify_changes(prev_date, prev_records, data_date, new_records)
        else:
            print("ℹ️ 今日推薦榜 0 檔（沒新增）")
        info["run_at_utc"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        LAST_RUN_FILE.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 更新 T+N 表現
    n_updated = update_records(records)
    if n_updated > 0:
        print(f"✅ 更新 {n_updated} 筆 T+N 表現")

    # 3) 寫回
    save_history(records)
    print(f"📝 寫入 {HIST_FILE}")


if __name__ == "__main__":
    main()
