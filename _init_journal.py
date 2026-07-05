"""一次性：把 6/5-6/8 已賣出但還沒進 journal 的交易補進去 + 建立基準快照"""
import json
from datetime import date
from pathlib import Path

# 6/5-6/8 已賣出的清單（從 cooldown.json）
RECENT_SELLS = [
    ("0050", "元大台灣50", 5000, 101.19, "ETF", "2026-06-05", "大跌調節 + 法人連賣"),
    ("1409", "新纖", 6000, 17.30, "傳產", "2026-06-05", "停損觸發、+56% 大贏家"),
    ("1528", "恩德", 2000, 40.15, "傳產", "2026-06-05", "大跌調節"),
    ("2382", "廣達", 2000, 306.0, "電腦", "2026-06-05", "停損觸發、+25% 大贏家"),
    ("2383", "台光電", 200, 5440.0, "PCB", "2026-06-05", "停損觸發、-9.82%"),
    ("2409", "友達", 5000, 19.85, "面板", "2026-06-05", "大跌調節、系統反指標"),
    ("4989", "榮科", 2000, 102.0, "電子", "2026-06-05", "停損觸發、-9%"),
    ("2301", "光寶科", 2000, 236.84, "電子", "2026-06-08", "週一大跌防禦砍倉"),
    ("2481", "強茂", 2000, 132.19, "半導體", "2026-06-08", "週一大跌防禦砍倉、+%"),
    ("8016", "矽創", 2000, 305.0, "IC設計", "2026-06-08", "停損 293 觸發"),
    ("3711", "日月光投控", 2000, 594.0, "封測", "2026-06-08", "停損 580 觸發"),
    ("3017", "奇鋐", 200, 2440.97, "散熱", "2026-06-08", "週一大跌防禦砍倉"),
    ("8182", "加高", 4000, 35.0, "電腦", "2026-06-08", "週一大跌防禦砍倉、落袋"),
    ("3028", "增你強", 2000, 88.0, "通路", "2026-06-08", "停損 78 觸發"),
    ("3042", "晶技", 1000, 175.0, "晶振", "2026-06-08", "週一大跌防禦砍倉、落袋"),
    ("6285", "啟碁", 1000, 251.0, "通訊", "2026-06-08", "週一大跌防禦砍倉、落袋"),
]

JOURNAL_FILE = Path("data/trade_journal.md")
PENDING_FILE = Path("data/journal_pending.json")
SNAPSHOT_FILE = Path("data/portfolio_snapshot.json")
JOURNAL_FILE.parent.mkdir(exist_ok=True)

# 1. 建立 journal 檔頭
header = "# 📔 Trade Journal — 我的交易回顧\n\n"
header += "> **規則**：每筆出場 24 小時內填完。回顧自己的決策，不是責備自己。\n\n"
header += "> **第一批回顧（5/13-6/8 共 16 筆）由系統一次性匯入，請挑時間慢慢填**\n\n"

# 2. 為每筆建模板
templates = []
pending = []
for code, name, shares, cost, cat, sold_date, reason in RECENT_SELLS:
    templates.append(f"""
---

## 📝 {sold_date} {name} {code}  🔴 已出場

### 自動帶入

| 欄位 | 內容 |
|---|---|
| 賣出日期 | {sold_date} |
| 代號 | {code} |
| 名稱 | {name} |
| 類別 | {cat} |
| 賣出股數 | {shares:,} |
| 進場成本 | {cost:.2f} |
| 系統登記理由 | {reason} |

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

**Q6. 1 句話心得**
>
""")
    pending.append({
        "added": sold_date,
        "code": code,
        "name": name,
        "sell_price": None,
        "type": "exit",
    })

# 3. 寫檔
JOURNAL_FILE.write_text(header + "".join(templates), encoding="utf-8")
PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")

# 4. 建快照（以現在的 portfolio.csv 為基準）
import pandas as pd
df = pd.read_csv("portfolio.csv", dtype={"code": str})
positions = {r["code"]: {
    "name": r["name"], "shares": int(r["shares"]),
    "cost": float(r["cost"]),
    "category": str(r.get("category", "")),
    "stop_loss": float(r["stop_loss"]) if pd.notna(r.get("stop_loss")) else None,
} for _, r in df.iterrows()}
SNAPSHOT_FILE.write_text(json.dumps({"date": date.today().isoformat(), "positions": positions},
                                    ensure_ascii=False, indent=2), encoding="utf-8")

print(f"✅ 已匯入 {len(RECENT_SELLS)} 筆歷史交易")
print(f"✅ Journal 起始檔：{JOURNAL_FILE}")
print(f"✅ 待填清單：{PENDING_FILE}")
print(f"✅ Portfolio 快照：{SNAPSHOT_FILE}")
