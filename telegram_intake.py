"""Telegram 截圖 → 持倉自動更新（傳成交回報截圖給 bot，Claude 看圖抽數字，確認後寫入 portfolio.csv）

流程：
  1. 手機把「成交回報」截圖傳給 Telegram bot
  2. 本機這支長輪詢程式收到照片 → 下載 → Claude(視覺)抽出每筆交易
  3. 用「委託書號」去重（已套用的略過），跟 portfolio.csv 對帳，算出提案
  4. bot 回你「提案內容 + 回 OK 套用 / 取消放棄」
  5. 你回「OK」→ 寫 portfolio.csv + cash.json + git commit&push；回「取消」→ 放棄

設定：telegram_config.json（bot_token / chat_id / anthropic_api_key，皆 gitignored）
執行：python telegram_intake.py       （前景，看得到 log）
      或雙擊 telegram_intake.vbs        （背景無黑窗）

安全：只處理設定裡 chat_id 本人傳的訊息；金鑰只在本機檔案。
"""
from __future__ import annotations

import base64
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

os.chdir(Path(__file__).resolve().parent)
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "telegram_config.json"
PORTFOLIO = ROOT / "portfolio.csv"
CASH_FILE = ROOT / "data" / "cash.json"
OFFSET_FILE = ROOT / "data" / "telegram_intake_offset.json"
PENDING_FILE = ROOT / "data" / "telegram_intake_pending.json"
APPLIED_FILE = ROOT / "data" / "telegram_intake_applied.json"

STOP_RATIO = 0.75          # 新持股停損 = 成交價 × 0.75（硬底線，沿用 portfolio 既有規則）
DEFAULT_HOLD_TYPE = "core"  # 新持股預設長線 core（跟近期加碼一致）
BUY_FEE = 0.001425          # 買進手續費估算（用於現金扣款；實際以交割為準）


# ─────────────────────── 設定 / 狀態 ───────────────────────
def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_applied() -> set:
    return set(_load_json(APPLIED_FILE, {"applied_order_ids": []}).get("applied_order_ids", []))


def save_applied(ids: set):
    _save_json(APPLIED_FILE, {"applied_order_ids": sorted(ids)})


# ─────────────────────── Telegram ───────────────────────
def tg(cfg, method, **params):
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/{method}"
    r = requests.post(url, data=params, timeout=70)
    return r.json()


def tg_get_updates(cfg, offset):
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/getUpdates"
    r = requests.get(url, params={"offset": offset, "timeout": 50}, timeout=70)
    return r.json().get("result", [])


def tg_send(cfg, text):
    tg(cfg, "sendMessage", chat_id=cfg["chat_id"], text=text)


def tg_download_photo(cfg, file_id) -> bytes:
    info = tg(cfg, "getFile", file_id=file_id)
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{file_path}"
    return requests.get(url, timeout=60).content


# ─────────────────────── Claude 視覺抽取 ───────────────────────
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "股票名稱"},
                    "code": {"type": "string", "description": "台股代號（4位數字，如京元電子=2449）"},
                    "action": {"type": "string", "enum": ["買進", "賣出"]},
                    "price": {"type": "number", "description": "成交價"},
                    "shares": {"type": "integer", "description": "成交股數"},
                    "date": {"type": "string", "description": "成交日期 YYYY-MM-DD"},
                    "order_id": {"type": "string", "description": "委託書號（去重用；看不到就填空字串）"},
                    "category": {"type": "string", "description": "產業別，繁中簡短，如 AI半導體/金融/電信/ETF"},
                },
                "required": ["name", "code", "action", "price", "shares", "date", "order_id", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["transactions"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = (
    "這是台股券商的成交回報截圖。抽出每一筆交易。"
    "code 填 4 位台股代號（若截圖沒有，用你知道的台灣知名個股名稱推斷，如 京元電子=2449、穩懋=3105、日月光投控=3711、技嘉=2376）。"
    "action 只能是『買進』或『賣出』。order_id 填該列的委託書號（去重用）。"
    "只抽真的有成交的列，不要抽表頭或空列。"
)


def extract_transactions(cfg, img_bytes: bytes) -> list[dict]:
    from anthropic import Anthropic
    client = Anthropic(api_key=cfg["anthropic_api_key"])
    b64 = base64.standard_b64encode(img_bytes).decode()
    resp = client.messages.create(
        model="claude-opus-5",
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text).get("transactions", [])


# ─────────────────────── 對帳 / 提案 ───────────────────────
def load_portfolio() -> list[dict]:
    with open(PORTFOLIO, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_cash() -> int:
    return int(_load_json(CASH_FILE, {"cash_twd": 0}).get("cash_twd", 0))


def _wan(v) -> str:
    s = f"{v / 10000:.1f}"
    return (s[:-2] if s.endswith(".0") else s) + " 萬"


def build_plan(txns: list[dict], applied: set):
    """回傳 (plan | None, message)。plan 內含最終 portfolio rows + 最終 cash + 已用 order_ids。"""
    fresh = [t for t in txns if t.get("order_id") and t["order_id"] not in applied]
    dup = [t for t in txns if t.get("order_id") and t["order_id"] in applied]

    if not fresh:
        return None, ("📸 收到截圖，共 {} 筆，全部都已記錄過（委託書號重複），無新交易。".format(len(txns)))

    rows = load_portfolio()
    by_code = {r["code"]: r for r in rows}
    cash = load_cash()
    cash_delta = 0
    lines = ["📸 截圖解析完成，發現 {} 筆新交易：\n".format(len(fresh))]

    for t in fresh:
        code, name = t["code"], t["name"]
        price, shares = float(t["price"]), int(t["shares"])
        amt = price * shares
        if t["action"] == "買進":
            cash_delta -= amt * (1 + BUY_FEE)
            if code in by_code and int(by_code[code]["shares"]) > 0:
                r = by_code[code]
                old_sh, old_cost = int(r["shares"]), float(r["cost"])
                new_sh = old_sh + shares
                new_cost = round((old_sh * old_cost + shares * price) / new_sh, 2)
                new_stop = round(new_cost * STOP_RATIO)
                r["shares"], r["cost"], r["stop_loss"] = str(new_sh), f"{new_cost}", str(new_stop)
                lines.append(f"• 加碼 {name} {code}：{shares}股@{price} → {new_sh}股 均價{new_cost} 停損{new_stop}")
            elif code in by_code:  # placeholder(0股) → 首次建倉
                r = by_code[code]
                r["shares"], r["cost"] = str(shares), f"{price}"
                r["stop_loss"] = str(round(price * STOP_RATIO))
                if not r.get("entry_date"):
                    r["entry_date"] = t["date"]
                lines.append(f"• 新建 {name} {code}：{shares}股@{price} 停損{round(price*STOP_RATIO)}")
            else:  # 全新個股
                stop = round(price * STOP_RATIO)
                new_row = {
                    "code": code, "name": name, "market": "TSE",
                    "shares": str(shares), "cost": f"{price}",
                    "category": t.get("category", "待分類"), "stop_loss": str(stop),
                    "entry_date": t["date"], "hold_type": DEFAULT_HOLD_TYPE,
                }
                rows.append(new_row)
                by_code[code] = new_row
                lines.append(f"• 新持股 {name} {code}：{shares}股@{price}（{t.get('category','')}，停損{stop}，{DEFAULT_HOLD_TYPE}）")
        else:  # 賣出
            cash_delta += amt
            if code in by_code and int(by_code[code]["shares"]) > 0:
                r = by_code[code]
                new_sh = max(0, int(r["shares"]) - shares)
                r["shares"] = str(new_sh)
                if new_sh == 0 and r.get("hold_type") != "income":
                    rows = [x for x in rows if x is not r]
                lines.append(f"• 賣出 {name} {code}：{shares}股@{price}（剩 {new_sh}股）")
            else:
                lines.append(f"• ⚠️ 賣出 {name} {code} 但持倉查無，僅計入現金 +{_wan(amt)}")

    new_cash = round(cash + cash_delta)
    lines.append(f"\n💰 現金 {_wan(cash)} → {_wan(new_cash)}（{'+' if cash_delta>=0 else ''}{_wan(cash_delta)}，含手續費估算）")
    if dup:
        lines.append(f"（另 {len(dup)} 筆已記錄過，略過）")
    lines.append("\n✅ 回「OK」套用並上雲端　❌ 回「取消」放棄")

    plan = {
        "proposed_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
        "new_cash": new_cash,
        "order_ids": [t["order_id"] for t in fresh],
        "summary": " / ".join(f"{t['action']}{t['name']}{t['shares']}股" for t in fresh),
    }
    return plan, "\n".join(lines)


FIELDS = ["code", "name", "market", "shares", "cost", "category", "stop_loss", "entry_date", "hold_type"]


def apply_plan(cfg, plan) -> str:
    # 寫 portfolio.csv
    with open(PORTFOLIO, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in plan["rows"]:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    # 寫 cash.json
    cash_obj = _load_json(CASH_FILE, {})
    cash_obj["cash_twd"] = plan["new_cash"]
    cash_obj["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    cash_obj["_note"] = f"Telegram 截圖更新：{plan['summary']}（現金含手續費估算，實際以交割為準）"
    _save_json(CASH_FILE, cash_obj)
    # 記錄已套用 order_ids
    applied = load_applied()
    applied.update(plan["order_ids"])
    save_applied(applied)
    # git commit + push（portfolio.csv 追蹤；cash.json gitignored 不會進 commit）
    push_msg = ""
    try:
        subprocess.run(["git", "add", "portfolio.csv"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"更新持倉（Telegram 截圖）：{plan['summary']}"],
                       cwd=ROOT, check=True, capture_output=True)
        pr = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, capture_output=True, text=True)
        push_msg = "，已 push 雲端" if pr.returncode == 0 else f"，⚠️ push 失敗（{pr.stderr.strip()[:80]}）"
    except subprocess.CalledProcessError as e:
        push_msg = f"，⚠️ git 失敗（{(e.stderr or b'').decode(errors='ignore')[:80]}）"
    return f"✅ 已更新持倉：{plan['summary']}{push_msg}"


# ─────────────────────── 主迴圈 ───────────────────────
def handle_message(cfg, msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(cfg["chat_id"]):
        return  # 只認本人

    # 文字：確認 / 取消
    text = (msg.get("text") or "").strip().lower()
    if text in ("ok", "確認", "yes", "y", "好"):
        plan = _load_json(PENDING_FILE, None)
        if not plan:
            tg_send(cfg, "沒有待確認的提案。先傳一張成交回報截圖。")
            return
        result = apply_plan(cfg, plan)
        _save_json(PENDING_FILE, None)
        tg_send(cfg, result)
        return
    if text in ("取消", "cancel", "no", "n"):
        _save_json(PENDING_FILE, None)
        tg_send(cfg, "已取消，未變更。")
        return

    # 照片：解析
    photos = msg.get("photo")
    if photos:
        try:
            tg_send(cfg, "📸 收到截圖，Claude 解析中…")
            img = tg_download_photo(cfg, photos[-1]["file_id"])  # 最大尺寸
            txns = extract_transactions(cfg, img)
            plan, message = build_plan(txns, load_applied())
            tg_send(cfg, message)
            if plan:
                _save_json(PENDING_FILE, plan)
        except Exception as e:
            tg_send(cfg, f"⚠️ 解析失敗：{type(e).__name__}: {e}")
        return

    if text:
        tg_send(cfg, "傳成交回報截圖來自動更新持倉；或回「OK/取消」處理待確認提案。")


def main():
    cfg = load_config()
    if not cfg.get("anthropic_api_key"):
        print("❌ telegram_config.json 缺 anthropic_api_key")
        return
    if OFFSET_FILE.exists():
        offset = _load_json(OFFSET_FILE, {"offset": 0}).get("offset", 0)
    else:
        # 首次啟動：跳過既有積壓訊息，只處理啟動後傳來的
        try:
            ups = tg_get_updates(cfg, 0)
            offset = (max(u["update_id"] for u in ups) + 1) if ups else 0
        except Exception:
            offset = 0
        _save_json(OFFSET_FILE, {"offset": offset})
    print(f"✅ Telegram 截圖更新器啟動（offset={offset}）。傳成交回報截圖給 bot 即可。")
    while True:
        try:
            updates = tg_get_updates(cfg, offset)
            for u in updates:
                offset = u["update_id"] + 1
                _save_json(OFFSET_FILE, {"offset": offset})
                msg = u.get("message") or u.get("edited_message")
                if msg:
                    handle_message(cfg, msg)
        except requests.exceptions.RequestException:
            time.sleep(5)
        except Exception as e:
            print(f"⚠️ 迴圈錯誤：{e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
