"""Trade Journal 自動模板 — 偵測賣出，自動產出待填寫的回顧記錄

設計：
  1. 每天比對 portfolio.csv vs 上次快照 (data/portfolio_snapshot.json)
  2. 若有股票「消失」(賣出) 或「持股減少」(部份出場)，自動產生模板
  3. 模板存到 data/trade_journal.md (append-only)
  4. 同步推播提醒：「有 X 筆待回顧」
  5. 你只要每天花 5 分鐘填空，1 年累積 300+ 筆完整記錄

模板內容：
  - 自動帶入：日期、代號、名稱、買進成本、賣出推估、損益 %、系統當下評分
  - 你填寫：進場理由、執行紀律、學到什麼、下次如何改進

執行時機：
  - 每天 14:30 排程跑一次（盤後）
  - 或盤中觸發停損時即時跑

用法：
  python trade_journal.py                  # 偵測並產生待填模板
  python trade_journal.py --show-pending   # 列出未填寫的記錄
  python trade_journal.py --review         # 開啟 journal 給你回顧
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# pythonw 排程沒 stdout
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

PORTFOLIO_FILE = Path("portfolio.csv")
SNAPSHOT_FILE = Path("data/portfolio_snapshot.json")
JOURNAL_FILE = Path("data/trade_journal.md")
PENDING_FILE = Path("data/journal_pending.json")
COOLDOWN_FILE = Path("cooldown.json")

SNAPSHOT_FILE.parent.mkdir(exist_ok=True)


# ---------- 快照管理 ----------

def load_snapshot() -> dict:
    """讀上次的 portfolio 快照"""
    if not SNAPSHOT_FILE.exists():
        return {"date": "", "positions": {}}
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"date": "", "positions": {}}


def save_snapshot(positions: dict) -> None:
    """寫入新的 portfolio 快照"""
    snap = {"date": date.today().isoformat(), "positions": positions}
    SNAPSHOT_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")


def read_current_portfolio() -> dict:
    """讀 portfolio.csv → {code: {name, shares, cost, category, stop_loss}}"""
    if not PORTFOLIO_FILE.exists():
        return {}
    df = pd.read_csv(PORTFOLIO_FILE, dtype={"code": str})
    out = {}
    for _, r in df.iterrows():
        out[r["code"]] = {
            "name": r["name"],
            "shares": int(r["shares"]),
            "cost": float(r["cost"]),
            "category": str(r.get("category", "")),
            "stop_loss": float(r["stop_loss"]) if pd.notna(r.get("stop_loss")) else None,
        }
    return out


# ---------- 偵測賣出事件 ----------

def detect_changes(old: dict, new: dict) -> list[dict]:
    """比對快照差異 → 賣出/減倉事件清單"""
    events = []
    for code, info in old.items():
        if code not in new:
            # 完全賣出
            events.append({
                "type": "exit",
                "code": code,
                "name": info["name"],
                "shares": info["shares"],
                "cost": info["cost"],
                "category": info.get("category", ""),
            })
        else:
            old_shares = info["shares"]
            new_shares = new[code]["shares"]
            if new_shares < old_shares:
                events.append({
                    "type": "partial_exit",
                    "code": code,
                    "name": info["name"],
                    "shares": old_shares - new_shares,
                    "cost": info["cost"],
                    "category": info.get("category", ""),
                })
    return events


# ---------- 抓賣出當日資訊 ----------

def fetch_last_price(code: str) -> float | None:
    """抓 TWSE MIS 當日收盤"""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0",
                          "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
        url = (f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
               f"?ex_ch=tse_{code}.tw|otc_{code}.tw&json=1&delay=0")
        r = s.get(url, timeout=8)
        for d in r.json().get("msgArray", []):
            z = d.get("z")
            if z and z not in ("-", ""):
                return float(z)
            y = d.get("y")
            if y and y not in ("-", ""):
                return float(y)
    except Exception:
        pass
    return None


def fetch_cooldown_reason(code: str) -> str:
    """從 cooldown.json 撈系統登記的賣出理由"""
    if not COOLDOWN_FILE.exists():
        return ""
    try:
        cd = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
        if code in cd:
            return cd[code].get("reason", "")
        # 也找 code_xxx 變體
        for k, v in cd.items():
            if k.startswith(code + "_") and isinstance(v, dict):
                return v.get("reason", "")
    except Exception:
        pass
    return ""


# ---------- 產生模板 ----------

def build_template(ev: dict, sell_price: float | None, sys_reason: str) -> str:
    """每筆事件 → markdown 模板"""
    today = date.today().isoformat()
    pnl_pct = ""
    pnl_amt = ""
    if sell_price:
        pct = (sell_price - ev["cost"]) / ev["cost"] * 100
        amt = (sell_price - ev["cost"]) * ev["shares"]
        pnl_pct = f"{pct:+.2f}%"
        pnl_amt = f"{amt:+,.0f} 元"

    typ_label = "🔴 全部賣出" if ev["type"] == "exit" else "🟡 部份賣出"
    sell_str = f"{sell_price:.2f}" if sell_price else "N/A"
    pnl_str = f" ({pnl_pct})" if pnl_pct else ""

    return f"""
---

## 📝 {today} {ev['name']} {ev['code']}  {typ_label}

### 自動帶入

| 欄位 | 內容 |
|---|---|
| 賣出日期 | {today} |
| 代號 | {ev['code']} |
| 名稱 | {ev['name']} |
| 類別 | {ev['category'] or '未分類'} |
| 賣出股數 | {ev['shares']:,} |
| 進場成本 | {ev['cost']:.2f} |
| 賣出推估價 | {sell_str}{pnl_str} |
| 損益金額（估）| {pnl_amt or '請手動填入'} |
| 系統登記理由 | {sys_reason or '（無）'} |

### ✏️ 待填寫（5 分鐘內回答）

**Q1. 當初為什麼進場？**（寫下訊號或題材）
>

**Q2. 是否照計畫執行？**（停損/停利有沒有違紀？延遲？提前？）
>

**Q3. 賺/賠的真實原因？**（拆解：題材 / 技術面 / 系統訊號 / 運氣 / 大盤）
>

**Q4. 情緒狀態**（進場 / 持有 / 出場時，從 1（極度恐懼）到 10（極度貪婪））
> 進場：__ ／ 持有最低：__ ／ 出場：__

**Q5. 下次如何更好？**（3 條具體改進）
> 1.
> 2.
> 3.

**Q6. 1 句話心得**（最重要的一行）
>
"""


# ---------- 寫入 journal + pending 清單 ----------

def append_journal(text: str) -> None:
    """追加到 trade_journal.md（檔頭沒檔就建）"""
    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.write_text(
            "# 📔 Trade Journal — 我的交易回顧\n\n"
            "> **規則**：每筆出場 24 小時內填完。回顧自己的決策，不是責備自己。\n\n",
            encoding="utf-8"
        )
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(text)


def load_pending() -> list[dict]:
    """讀待填清單"""
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pending(items: list[dict]) -> None:
    PENDING_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_pending(ev: dict, sell_price: float | None) -> None:
    """加入待填清單"""
    items = load_pending()
    items.append({
        "added": date.today().isoformat(),
        "code": ev["code"],
        "name": ev["name"],
        "sell_price": sell_price,
        "type": ev["type"],
    })
    save_pending(items)


# ---------- 推播提醒 ----------

def notify_pending(count: int, events: list[dict]) -> None:
    """提醒用戶有 X 筆待回顧"""
    lines = [f"📔 Trade Journal 待回顧 ({count} 筆)：\n"]
    for ev in events:
        lines.append(f"・{ev['name']} {ev['code']}")
    lines.append("\n👉 開啟 data/trade_journal.md 填寫")
    lines.append("⏰ 24 小時內回顧效果最好")
    body = "\n".join(lines)

    # Windows Toast
    try:
        from winotify import Notification, audio
        toast = Notification(app_id="Trade Journal",
                             title=f"📔 {count} 筆待回顧",
                             msg=body, duration="long")
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass

    # Telegram
    try:
        import telegram_notify
        telegram_notify.send(f"📔 Trade Journal — {count} 筆待回顧", body)
    except Exception:
        pass


# ---------- 主流程 ----------

def main():
    snapshot = load_snapshot()
    current = read_current_portfolio()
    events = detect_changes(snapshot["positions"], current)

    # 命令列旗標
    args = sys.argv[1:]
    if "--show-pending" in args:
        pending = load_pending()
        if not pending:
            print("✅ 目前沒有待回顧的交易")
        else:
            print(f"📔 {len(pending)} 筆待回顧：")
            for p in pending:
                print(f"  {p['added']}  {p['name']} {p['code']}")
        return

    if "--review" in args:
        if JOURNAL_FILE.exists():
            os.startfile(str(JOURNAL_FILE.resolve()))
        else:
            print("📔 還沒有任何 journal 記錄")
        return

    # 偵測新事件
    if not events:
        # 第一次跑也要建立快照
        save_snapshot(current)
        print("✅ portfolio 無變化")
        return

    # 為每個事件產模板
    for ev in events:
        sell_price = fetch_last_price(ev["code"])
        sys_reason = fetch_cooldown_reason(ev["code"])
        template = build_template(ev, sell_price, sys_reason)
        append_journal(template)
        add_to_pending(ev, sell_price)
        print(f"📝 新增模板：{ev['name']} {ev['code']} ({ev['type']})")

    # 更新快照
    save_snapshot(current)

    # 推播提醒
    pending = load_pending()
    notify_pending(len(pending), events)
    print(f"📔 共 {len(pending)} 筆待回顧（含這批 {len(events)} 筆）")


if __name__ == "__main__":
    main()
