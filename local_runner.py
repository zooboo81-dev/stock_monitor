"""本機準時跑 — 讓這台 24 小時開著的電腦準時跑台股排程，不再靠 GitHub 的雲端定時器

為什麼要有這支（2026-11-24 查證）：
  GitHub 雲端排程（schedule）是「盡量準時」，實際上晚 7～12 小時才跑：
  - 晚上 22:20 的融資維持率／抄底計分卡，隔天早上 6～10 點才出來
  - 盤中每 5 分鐘的警示，一天只跑 2 次、而且都在收盤後（10/30 起等於沒作用）
  - 08:50 的晨間摘要下午才跑
  所以改成：Windows 工作排程器每 5 分鐘叫一次 `local_runner.py tick`，
  tick 看現在幾點、哪些工作到期 → 先跟 GitHub 同步 → 跑 → 把結果 commit＋push。
  雲端那幾個排程的定時觸發已關掉（只留手動 Run workflow），
  這台電腦是那些狀態檔的唯一寫入者（避免兩邊搶寫）。

用法：
  pythonw local_runner.py tick              # 排程用：到期的才跑（每 5 分鐘叫一次）
  python  local_runner.py run margin_fear   # 立刻跑一個工作（含同步＋推上 GitHub）
  python  local_runner.py run intraday --no-git   # 只跑、不碰 git
  python  local_runner.py sync              # 只跟 GitHub 同步
  python  local_runner.py status            # 每個工作上次什麼時候跑、成功沒、下次幾點
  python  local_runner.py selftest          # 在沙盒把每個工作試跑一遍（不推到手機），事先抓會當掉的

失敗與恢復（2026-11-24 起）：
  工作失敗 → 推「⚠️ 台股排程失敗」；之後重跑成功 → 再推「✅ 已恢復」，你就知道不用處理了。
  同一項一天最多推 3 次失敗（時好時壞也不會一直吵）。
  每天 07:00、20:00 還會跑「排程試跑」（job_selftest.py）：程式、持股、設定有改過（或滿 7 天）就在沙盒
  ＋怪資料把每一項跑一遍，到時候會當掉的先推「🧪」提醒；什麼都沒改的話幾秒就跳過。

設定：data/local_schedule.json（記事本可改：每個工作的開關與時間）
紀錄：data/local_runner.log；狀態：data/local_runner_state.json（都不上 GitHub）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

# pythonw 排程沒有 stdout/stderr
for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CONFIG_FILE = DATA / "local_schedule.json"
STATE_FILE = DATA / "local_runner_state.json"
LOG_FILE = DATA / "local_runner.log"
LOCK_FILE = DATA / "local_runner.lock"
HOLIDAY_FILE = DATA / "twse_holidays.json"

RUNNER_NAME = "本機準時跑"
RUNNER_EMAIL = "local-runner@stock-monitor.local"
SYNC_EVERY_MIN = 10          # 沒有工作要跑時，多久跟 GitHub 同步一次
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # 子程序不要跳黑窗

# 證交所休市日 API 只給「當年度」一張表，115 年的已經被換成 116 年 → 這裡補 115 年剩下的
SEED_CLOSED = {"2026-12-25"}


# ---------------------------------------------------------------- 工作清單
@dataclass
class Job:
    key: str
    label: str
    cmds: list                       # [[程式, 參數...], ...] 依序跑
    commit: list                     # 跑完要 commit＋push 的檔
    msg: str                         # commit 訊息
    times: list = field(default_factory=list)   # 每天這些時刻（"HH:MM"）
    every_min: int = 0               # >0：window 區間內每 N 分鐘跑一次
    window: tuple | None = None      # ("09:00", "13:35")
    catchup_min: int = 120           # 錯過時刻多久內還補跑（電腦剛好重開機時用）
    trading_day_only: bool = True    # 只在交易日跑（週末、休市日跳過）
    month_day: int = 0               # >0：每月這一天才跑
    timeout: int = 600               # 單一程式最多跑幾秒
    optional: tuple = ()             # 這幾個（第幾個指令）失敗不算整個工作失敗
    alert_after: int = 1             # 連續失敗幾次才推播
    max_attempts: int = 2            # 同一時刻最多試幾次
    expect: tuple | None = None      # (檔, 欄位, 最晚時刻)：過了這時刻欄位還不是今天 → 推播
    enabled: bool = True
    alert_title: str = ""            # 失敗推播的標題（空白＝「⚠️ 台股排程失敗：{label}」）
    alert_body: str = ""             # 失敗推播的開頭說明（空白＝「這台電腦跑「…」失敗」）
    alert_silent: bool = False       # 失敗／恢復推播不響（只進通知列表）
    selftest: bool = True            # False＝排程試跑（job_selftest）不跑這項（要連信箱、吃個資檔的工作）


_INTRADAY_FILES = ["data/intraday_state.json", "data/trailing_state.json"]
_MARGIN_FILES = ["data/margin_fear_history.json", "data/margin_fear_state.json",
                 "data/bottom_daily.json", "data/bottom_scorecard.json"]
_MARGIN_CMDS = [["margin_fear_gauge.py"], ["bottom_scorecard.py"]]

DEFAULT_JOBS = [
    Job("intraday", "盤中警示（每 5 分鐘）", [["intraday_alert.py"]], _INTRADAY_FILES,
        "chore: intraday alert", every_min=5, window=("09:00", "13:35"),
        timeout=240, alert_after=3),
    Job("intraday_close", "收盤後補檢查", [["intraday_alert.py", "--force"]], _INTRADAY_FILES,
        "chore: intraday alert", times=["14:30"], catchup_min=90, timeout=240),
    Job("morning_briefing", "晨間摘要", [["morning_briefing.py"]], [],
        "chore: morning briefing", times=["08:50"], catchup_min=90),
    Job("expiration_check", "持股到期檢查（T+10）", [["holding_expiration_check.py"]],
        ["data/expiration_checks.json"], "chore: T+10 expiration check",
        times=["09:00"], catchup_min=240),
    Job("taiex_ma", "大盤均線＋空頭加碼", [["taiex_ma_tracker.py"]], ["data/taiex_ma_state.json"],
        "chore: update TAIEX MA state", times=["14:10", "15:10"], catchup_min=480,
        expect=("data/taiex_ma_state.json", "date", "15:45")),
    Job("watchlist", "觀察名單", [["watchlist_check.py"]], [],
        "chore: watchlist check", times=["14:40"], catchup_min=300),
    Job("recs", "推薦快照", [["recs_snapshot.py"]],
        ["data/recs_history.jsonl", "data/recs_last_run.json"],
        "chore: update recs snapshot", times=["17:30"], catchup_min=360, timeout=1800),
    # 16:40 先更新當天的期貨／選擇權／外資／跌停（下午就公布了），
    # 21:35 起每半小時試一次融資（證交所約 21:30 公布，已處理過的日期程式自己會跳過）
    Job("margin_fear", "融資維持率＋恐慌指數＋抄底計分卡", _MARGIN_CMDS, _MARGIN_FILES,
        "chore: update margin & fear gauge",
        times=["16:40", "21:35", "22:05", "22:35", "23:30"], catchup_min=120,
        optional=(1,), alert_after=2,
        expect=("data/margin_fear_state.json", "last_date", "23:50")),
    # 前一晚證交所延後公布的話，隔天早上補一次（週末也跑：週五的資料週六早上補）
    Job("margin_fear_am", "融資／計分卡（早上補跑）", _MARGIN_CMDS, _MARGIN_FILES,
        "chore: update margin & fear gauge", times=["07:30"], catchup_min=60,
        trading_day_only=False, optional=(1,), alert_after=2),
    Job("allocation", "月度配置檢查（每月 1 號）", [["allocation_check.py"]], [],
        "chore: monthly allocation check", times=["08:50"], catchup_min=600,
        trading_day_only=False, month_day=1),
    # 事先試跑（2026-11-24 起，起因：晨間摘要遇到成本 0 的預留列當掉）：程式、持股、設定改過（或滿 7 天）
    # 就在沙盒＋怪資料把上面每一項跑一遍，「到時候會當掉」的先推播；什麼都沒改的話幾秒就跳過。
    # 07:00 在早上的工作（07:30 融資補跑、08:50 晨間摘要、09:00 開盤）之前；20:00 給白天改過的程式。
    Job("selftest", "排程試跑（事先抓會當掉的程式）", [["job_selftest.py", "--canary"]], [],
        "chore: selftest", times=["07:00", "20:00"], catchup_min=30, trading_day_only=False,
        timeout=2400, alert_after=2, alert_silent=True,
        alert_title="🧪 台股排程試跑：有工作到時候會失敗",
        alert_body="還沒真的出事——是排程時間之前先在沙盒試跑抓到的。把這則訊息給 Claude 看就能修。"),
    # 國泰美股（複委託）對帳單：Gmail → PDF → 入帳 CSV＋核對（cathay_sync\，資料都不進版控，所以 commit 清單是空的）。
    # 時間照實際寄達時間排（2026-11 量 65 封）：買賣報告書成交隔天 13:04～18:06 寄到（中位 13:31、九成 15:04 前），
    # 月對帳單每月 5～10 號、整天都可能寄。週末也跑（週五成交的報告書週六寄）。
    # 不進排程試跑：它要連信箱、讀上鎖的 PDF，而且 cathay_sync 整個資料夾不複製進沙盒。
    Job("cathay_us", "國泰美股對帳單同步", [["cathay_sync/sync_cathay.py"]], [],
        "chore: cathay us statement", times=["14:20", "18:30", "22:50"], catchup_min=180,
        trading_day_only=False, alert_after=2, selftest=False,
        alert_title="⚠️ 國泰美股對帳單同步失敗",
        alert_body="國泰美股對帳單這次沒同步成功（信箱連不上、PDF 打不開，或核對沒過）。"
                   "戰情室先顯示上次的資料。把這則訊息給 Claude 看就能查。"),
    # 美股收盤價快照（2026-11-24）：戰情室「🗽 美股對帳單」拿最近一個美股收盤日的價格算市值、未實現（戰情室自己不連網抓價）。
    # 美股 16:00 收盤＝台灣早上 5 點（夏令 4 點）→ 06:30 抓；15:00 再跑一次：補抓早上沒成功的、補上中午國泰同步新入帳的代號。
    # 已是最新（這個收盤日抓過、持股都有價）就不連網、幾秒結束，所以週日、週一跑到也沒關係。
    # 輸出在 data/trades（不進版控）→ commit 清單是空的；沙盒裡沒有交易檔，排程試跑時會直接正常結束。
    Job("us_quotes", "美股收盤價快照", [["us_quote_snapshot.py"]], [],
        "chore: us quotes", times=["06:30", "15:00"], catchup_min=180,
        trading_day_only=False, alert_after=2, alert_silent=True,
        alert_title="⚠️ 美股收盤價沒抓到",
        alert_body="美股收盤價快照這次沒更新成功（Yahoo 連不上，或交易檔讀不懂）。"
                   "戰情室「美股對帳單」先顯示上次的價格（有標日期）。把這則訊息給 Claude 看就能查。"),
]


# ---------------------------------------------------------------- 小工具
def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _hm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def log(msg: str) -> None:
    line = f"{_iso()} {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG_FILE.stat().st_size > 1_500_000:      # 太大就只留最後 4000 行
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
            LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


class Lock:
    """整支同時間只跑一份（tick 還沒跑完時，下一個 tick 直接結束；程序死掉系統會自動放鎖）"""

    def __init__(self, wait_sec: int = 0):
        self.wait_sec = wait_sec
        self.fh = None

    def __enter__(self):
        import msvcrt
        deadline = _time.time() + self.wait_sec
        self.fh = open(LOCK_FILE, "a+")
        while True:
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if _time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    return False
                _time.sleep(2)

    def __exit__(self, *exc):
        if self.fh:
            try:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            self.fh.close()


# ---------------------------------------------------------------- 設定與狀態
def load_jobs() -> list:
    """程式內建時刻表 ＋ data/local_schedule.json 的覆寫（開關、時間）"""
    cfg = _read_json(CONFIG_FILE, {})
    over = cfg.get("jobs", {}) if isinstance(cfg, dict) else {}
    jobs = []
    for j in DEFAULT_JOBS:
        o = over.get(j.key, {}) if isinstance(over.get(j.key, {}), dict) else {}
        try:
            if "enabled" in o:
                j.enabled = bool(o["enabled"])
            if "times" in o and not j.every_min:
                j.times = [f"{_hm(t):%H:%M}" for t in o["times"]]
            if "every_min" in o and j.every_min:
                j.every_min = max(1, int(o["every_min"]))
            if "window" in o and j.every_min:
                a, b = o["window"]
                j.window = (f"{_hm(a):%H:%M}", f"{_hm(b):%H:%M}")
        except Exception as e:
            log(f"⚠️ 設定檔 {CONFIG_FILE.name} 的 {j.key} 看不懂（{e}），這項用內建值")
        jobs.append(j)
    return jobs


def load_state() -> dict:
    st = _read_json(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("jobs", {})
    st.setdefault("sync", {})
    st.setdefault("alerts", {})
    return st


def save_state(st: dict) -> None:
    keep = {(date.today() - timedelta(days=i)).isoformat() for i in range(4)}
    for js in st["jobs"].values():
        js["done"] = {d: v for d, v in js.get("done", {}).items() if d in keep}
    st["alerts"] = {k: v for k, v in st["alerts"].items() if v in keep}
    _write_json(STATE_FILE, st)


# ---------------------------------------------------------------- 交易日
_CLOSED_MEMO: dict = {}


def _load_closed(today: date) -> set:
    """休市日（證交所官方表，抓過就存檔；抓不到就只當週末休市）"""
    if "v" not in _CLOSED_MEMO:
        _CLOSED_MEMO["v"] = _fetch_closed(today)
    return _CLOSED_MEMO["v"]


def _fetch_closed(today: date) -> set:
    cache = _read_json(HOLIDAY_FILE, {})
    if not isinstance(cache, dict):
        cache = {}
    closed = set(cache.get("closed", []))
    years = set(cache.get("years", []))
    want = {today.year} | ({today.year + 1} if today.month >= 11 else set())
    if not want <= years and cache.get("tried") != today.isoformat():
        cache["tried"] = today.isoformat()
        try:
            import requests
            try:
                import truststore
                truststore.inject_into_ssl()
            except Exception:
                pass
            rows = requests.get("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
                                timeout=15).json()
            for r in rows:
                d = str(r.get("Date", ""))
                if len(d) != 7 or not d.isdigit():
                    continue
                iso = f"{int(d[:3]) + 1911}-{d[3:5]}-{d[5:7]}"
                years.add(int(iso[:4]))
                name = str(r.get("Name", ""))
                # 表裡也列了「開始交易日」「最後交易日」，那些是有開盤的
                if "開始交易" in name or "最後交易" in name:
                    continue
                closed.add(iso)
        except Exception as e:
            log(f"ℹ️ 休市日表抓不到（{type(e).__name__}），先只當週末休市")
        cache["closed"] = sorted(closed)
        cache["years"] = sorted(years)
        try:
            _write_json(HOLIDAY_FILE, cache)
        except Exception:
            pass
    return closed | SEED_CLOSED


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _load_closed(d)


# ---------------------------------------------------------------- 推播
MAX_FAIL_ALERTS_PER_DAY = 3      # 同一項工作一天最多推幾次「失敗」


def _send(title: str, body: str, silent: bool = False) -> None:
    log(f"📣 推播：{title}｜{body[:200]}")
    try:
        import telegram_notify
        telegram_notify.send(title, body, silent=silent)
    except Exception as e:
        log(f"⚠️ 推播失敗：{e}")


def alert_once(st: dict, key: str, title: str, body: str) -> bool:
    """同一件事一天只推一次。回傳這次有沒有推"""
    today = date.today().isoformat()
    if st["alerts"].get(key) == today:
        return False
    st["alerts"][key] = today
    _send(title, body)
    return True


def alert_job_failed(job: "Job", js: dict) -> None:
    """工作失敗推播：一次出事推一則（隔天還沒好會再提醒）；修好推過「已恢復」後再壞才會再推；
    一天最多 MAX_FAIL_ALERTS_PER_DAY 則，時好時壞也不會一直吵"""
    today = date.today().isoformat()
    if js.get("alerted") == today:
        return
    cnt = js.get("alert_count") if isinstance(js.get("alert_count"), dict) else {}
    n = int(cnt.get(today, 0) or 0)
    if n >= MAX_FAIL_ALERTS_PER_DAY:
        return
    title = job.alert_title or f"⚠️ 台股排程失敗：{job.label}"
    intro = job.alert_body or f"這台電腦跑「{job.label}」失敗（連續 {js.get('fail_streak', 1)} 次）。"
    _send(title, f"{intro}\n排程會自動再試；恢復正常時會再推「✅ 已恢復」。\n\n{js.get('last_error', '')[-500:]}",
          silent=job.alert_silent)
    js["alerted"] = today
    js["alert_count"] = {today: n + 1}


def alert_job_recovered(job: "Job", js: dict) -> None:
    """之前推過「失敗」的工作重跑成功 → 補一則「已恢復」，才知道不用處理了"""
    if not js.pop("alerted", None):
        return
    _send(f"✅ 台股排程恢復：{job.label}",
          f"「{job.label}」{_now():%H:%M} 重跑成功，已恢復正常，不用處理。", silent=job.alert_silent)


# ---------------------------------------------------------------- git
def _find_git() -> str:
    g = shutil.which("git")
    if g:
        return g
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if Path(p).exists():
            return p
    return "git"


GIT = _find_git()
_ENV = dict(os.environ, GCM_INTERACTIVE="Never", GIT_TERMINAL_PROMPT="0",
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8", STOCK_RUNNER="1")


def git(*args, timeout: int = 120) -> tuple:
    try:
        r = subprocess.run([GIT, *args], cwd=ROOT, env=_ENV, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} 超過 {timeout} 秒沒回應"
    except Exception as e:
        return 125, f"git 執行不了：{e}"


def repo_busy() -> str | None:
    gd = ROOT / ".git"
    if any((gd / n).exists() for n in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                                       "CHERRY_PICK_HEAD", "REVERT_HEAD")):
        return "資料夾裡有做到一半的合併"
    rc, br = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return f"git 讀不到目前分支（{br[:80]}）"
    if br != "main":
        return f"目前不在 main 分支（在 {br}）"
    return None


def ahead_behind() -> tuple:
    rc, out = git("rev-list", "--left-right", "--count", "HEAD...origin/main")
    try:
        a, b = out.split()
        return int(a), int(b)
    except Exception:
        return 0, 0


def only_runner_ahead() -> bool:
    """還沒推的 commit 是不是全都是這支自己產生的（有你手動改的就不代推）"""
    rc, out = git("log", "origin/main..HEAD", "--format=%ae")
    emails = [x.strip() for x in out.splitlines() if x.strip()]
    return rc == 0 and all(e == RUNNER_EMAIL for e in emails)


def _owned_files() -> set:
    return {f for j in DEFAULT_JOBS for f in j.commit}


def _pull(st: dict) -> tuple:
    """把 GitHub 上的新東西拉下來。回傳 (成功?, 說明)"""
    rc, dirty = git("diff", "--name-only", "HEAD")
    rc2, incoming = git("diff", "--name-only", "HEAD...origin/main")
    clash = set(dirty.split()) & set(incoming.split())
    if clash:
        mine = clash & _owned_files()
        other = clash - mine
        if other:
            return False, "本機改到一半的檔案跟 GitHub 新版撞到：" + "、".join(sorted(other))
        # 狀態檔（只有這支在寫）：本機那份是跑到一半留下的，備份後以 GitHub 為準
        bk = ROOT / "data_export" / f"pull_backup_{_now():%Y%m%d_%H%M%S}"
        bk.mkdir(parents=True, exist_ok=True)
        for f in mine:
            try:
                shutil.copy2(ROOT / f, bk / Path(f).name)
            except Exception:
                pass
        git("checkout", "--", *sorted(mine))
        log(f"ℹ️ 狀態檔跟 GitHub 新版撞到，本機那份備份到 {bk.name} 後改用 GitHub 的：{sorted(mine)}")
    rc, out = git("pull", "--rebase", "--autostash", "origin", "main", timeout=180)
    if rc != 0:
        git("rebase", "--abort")
        return False, "拉 GitHub 新版失敗：" + out[-300:]
    return True, ""


def _push(st: dict) -> tuple:
    for _ in range(3):
        ahead, _behind = ahead_behind()
        if not ahead:
            return True, ""
        if not only_runner_ahead():
            return False, "HUMAN"
        rc, out = git("push", "origin", "main", timeout=120)
        if rc == 0:
            return True, ""
        # 被擋通常是 GitHub 上有新東西 → 拉下來再推
        git("fetch", "origin", timeout=60)
        ok, why = _pull(st)
        if not ok:
            return False, why
    return False, "推上 GitHub 失敗：" + out[-300:]


def sync(st: dict) -> bool:
    """跟 GitHub 對齊：拉新的、推還沒推的。失敗只記錄＋一天推播一次，不影響工作照跑"""
    s = st["sync"]
    s["last"] = _iso()
    busy = repo_busy()
    if busy:
        s.update(ok=False, error=busy)
        log(f"⚠️ 同步跳過：{busy}")
        alert_once(st, "sync_busy", "⚠️ 台股自動同步暫停", f"{busy}，自動同步先不動它。\n處理完就會自己恢復。")
        return False
    rc, out = git("fetch", "origin", timeout=45)
    if rc != 0:
        s.update(ok=False, error="連不上 GitHub：" + out[-200:])
        s["fetch_fail"] = s.get("fetch_fail", 0) + 1
        log(f"⚠️ 連不上 GitHub（第 {s['fetch_fail']} 次）：{out[-200:]}")
        if s["fetch_fail"] >= 6:      # 連續半小時以上才吵你
            alert_once(st, "sync_fetch", "⚠️ 台股連不上 GitHub",
                       "已連續多次連不上 GitHub。資料照樣在這台電腦更新，只是雲端版儀表板不會跟著動。")
        return False
    s["fetch_fail"] = 0
    ahead, behind = ahead_behind()
    pulled = 0
    if behind:
        ok, why = _pull(st)
        if not ok:
            s.update(ok=False, error=why)
            log(f"⚠️ {why}")
            alert_once(st, "sync_pull", "⚠️ 台股自動同步卡住", why[:400])
            return False
        pulled = behind
        log(f"⬇️ 從 GitHub 拉了 {behind} 版")
    ok, why = _push(st)
    ahead, behind = ahead_behind()
    if not ok and why == "HUMAN":
        s.update(ok=True, error="", note=f"本機有 {ahead} 版還沒推（含你手動改的），不代推")
        alert_once(st, "sync_human", "ℹ️ 台股：有修改還沒推上 GitHub",
                   f"這台電腦有 {ahead} 版還沒推上 GitHub，裡面有手動改的內容，所以沒有自動推。\n"
                   f"開 GitHub Desktop 按 Push 就恢復（資料在這台電腦照常更新）。")
        return True
    if not ok:
        s.update(ok=False, error=why)
        log(f"⚠️ {why}")
        alert_once(st, "sync_push", "⚠️ 台股推不上 GitHub",
                   why[:300] + "\n（這台電腦的資料照常更新，只是雲端版儀表板不會跟著動）")
        return False
    s.update(ok=True, error="", note="", ahead=ahead, behind=behind, last_ok=_iso())
    if pulled:
        s["last_pull"] = _iso()
    return True


def commit_outputs(job: Job) -> str | None:
    """把這個工作寫出來的檔 commit 起來（只 commit 這幾個檔，不動你其他改到一半的東西）"""
    if not job.commit or repo_busy():
        return None
    files = [f for f in job.commit if (ROOT / f).exists()]
    if not files:
        return None
    git("add", "--", *files)
    rc, _ = git("diff", "--cached", "--quiet", "--", *files)
    if rc == 0:
        return None          # 沒變動
    rc, out = git("-c", f"user.name={RUNNER_NAME}", "-c", f"user.email={RUNNER_EMAIL}",
                  "commit", "-m", job.msg + "（本機）", "--", *files)
    if rc != 0:
        log(f"⚠️ {job.label} commit 失敗：{out[-200:]}")
        return None
    rc, sha = git("rev-parse", "--short", "HEAD")
    return sha


# ---------------------------------------------------------------- 跑工作
def _python() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")       # pythonw 啟動的話，子程序改用 python.exe 才收得到輸出
    return str(cand if cand.exists() else exe)


def run_cmd(cmd: list, timeout: int) -> tuple:
    try:
        r = subprocess.run([_python(), "-X", "utf8", *cmd], cwd=ROOT, env=_ENV,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        return 124, (out or "") + f"\n（超過 {timeout} 秒，已中止）"
    except Exception as e:
        return 125, f"啟動失敗：{e}"


def run_job(job: Job, st: dict, slots: list | None = None, use_git: bool = True) -> bool:
    js = st["jobs"].setdefault(job.key, {})
    t0 = _now()
    js["last_start"] = _iso(t0)
    ok, tail = True, ""
    for i, cmd in enumerate(job.cmds):
        rc, out = run_cmd(cmd, job.timeout)
        tail = "\n".join(out.splitlines()[-12:])
        for line in out.splitlines()[-25:]:
            log(f"   [{cmd[0]}] {line}")
        if rc != 0:
            if i in job.optional:
                log(f"⚠️ {job.label}：{cmd[0]} 失敗（rc={rc}），這步不擋其他")
                continue
            ok = False
            js["last_error"] = f"{cmd[0]} rc={rc}\n{tail}"
            break
    sha = None
    if ok and use_git:
        sha = commit_outputs(job)
        if sha:
            pushed, why = _push(st)
            log(f"⬆️ {job.label} 已 commit {sha}" + ("、已推上 GitHub" if pushed else f"（還沒推上去：{why[:120]}）"))
            if not pushed and why != "HUMAN":
                st["sync"].update(ok=False, error=why)
    dur = (_now() - t0).total_seconds()
    js.update(last_end=_iso(), last_rc=0 if ok else 1, last_sec=round(dur, 1))
    done = js.setdefault("done", {}).setdefault(date.today().isoformat(), {})
    if ok:
        js.update(last_ok=_iso(), fail_streak=0, last_error="")
        if sha:
            js["last_commit"] = sha
        for s in slots or []:
            done[s] = "ok"
        alert_job_recovered(job, js)
    else:
        js["fail_streak"] = js.get("fail_streak", 0) + 1
        for s in slots or []:
            n = done.get(s, 0)
            done[s] = (n if isinstance(n, int) else 0) + 1
        if js["fail_streak"] >= job.alert_after:
            alert_job_failed(job, js)
    log(f"{'✅' if ok else '❌'} {job.label}（{dur:.0f} 秒）")
    return ok


def due_slots(job: Job, st: dict, now: datetime, trading: bool) -> list | None:
    """這個工作現在該不該跑。回傳到期的時刻清單（每 N 分鐘型回傳 [區間代號]）；不用跑回傳 None"""
    if not job.enabled:
        return None
    if job.trading_day_only and not trading:
        return None
    if job.month_day and now.day != job.month_day:
        return None
    js = st["jobs"].get(job.key, {})
    today = now.date().isoformat()
    done = js.get("done", {}).get(today, {})
    if job.every_min:
        a, b = _hm(job.window[0]), _hm(job.window[1])
        if not (a <= now.time() <= b):
            return None
        mins = now.hour * 60 + now.minute
        bucket = f"~{(mins // job.every_min) * job.every_min // 60:02d}:{(mins // job.every_min) * job.every_min % 60:02d}"
        return None if bucket in done else [bucket]
    out = []
    for t in job.times:
        at = datetime.combine(now.date(), _hm(t))
        if not (at <= now <= at + timedelta(minutes=job.catchup_min)):
            continue
        v = done.get(t)
        if v == "ok" or (isinstance(v, int) and v >= job.max_attempts):
            continue
        out.append(t)
    return out or None


def check_expectations(jobs: list, st: dict, now: datetime, trading: bool) -> None:
    """該更新的資料到時間還沒更新 → 推播（一天一次）"""
    if not trading:
        return
    today = now.date().isoformat()
    for j in jobs:
        if not (j.enabled and j.expect):
            continue
        f, key, by = j.expect
        if now.time() < _hm(by):
            continue
        val = str(_read_json(ROOT / f, {}).get(key, ""))[:10]
        if val != today:
            extra = "明早 07:30 會自動再補一次。" if j.key == "margin_fear" else "下一個時刻會再試。"
            alert_once(st, f"stale:{j.key}", f"⚠️ 台股資料還沒更新：{j.label}",
                       f"到 {by} 資料還停在 {val or '（沒有）'}，不是今天 {today}。\n"
                       f"常見原因：證交所／期交所延後公布，或今天臨時休市。{extra}")


def tick() -> None:
    with Lock() as got:
        if not got:
            return                    # 上一輪還在跑
        jobs, st, now = load_jobs(), load_state(), _now()
        trading = is_trading_day(now.date())
        due = [(j, s) for j in jobs if (s := due_slots(j, st, now, trading))]
        last_sync = st["sync"].get("last", "")
        need_sync = bool(due) or not last_sync or \
            (now - datetime.fromisoformat(last_sync)) >= timedelta(minutes=SYNC_EVERY_MIN)
        try:
            if need_sync:
                try:
                    sync(st)
                except Exception as e:           # 同步出任何狀況都不能擋工作
                    log(f"⚠️ 同步出錯：{type(e).__name__}: {e}")
            for j, slots in due:
                log(f"▶️ {j.label}（{'、'.join(slots)}）")
                try:
                    # 每 N 分鐘型：先記「這個區間跑過了」，失敗也不在同一區間重試
                    if j.every_min:
                        st["jobs"].setdefault(j.key, {}).setdefault("done", {}) \
                            .setdefault(now.date().isoformat(), {})[slots[0]] = "ok"
                        run_job(j, st, [])
                    else:
                        run_job(j, st, slots)
                except Exception as e:
                    log(f"❌ {j.label} 出錯：{type(e).__name__}: {e}")
                save_state(st)
            check_expectations(jobs, st, _now(), trading)
        finally:
            st["tick"] = _iso()
            save_state(st)


# ---------------------------------------------------------------- 指令
def _next_due(job: Job, now: datetime) -> str:
    if not job.enabled:
        return "（已關閉）"
    if job.every_min:
        return f"{job.window[0]}～{job.window[1]} 每 {job.every_min} 分鐘"
    for add in range(0, 40):
        d = now.date() + timedelta(days=add)
        if job.trading_day_only and not is_trading_day(d):
            continue
        if job.month_day and d.day != job.month_day:
            continue
        for t in sorted(job.times):
            at = datetime.combine(d, _hm(t))
            if at > now:
                return f"{at:%m/%d %H:%M}"
    return "—"


def status() -> None:
    jobs, st, now = load_jobs(), load_state(), _now()
    print(f"現在 {now:%Y-%m-%d %H:%M}｜今天{'是' if is_trading_day(now.date()) else '不是'}交易日"
          f"｜排程最後一次檢查 {st.get('tick', '（還沒跑過）')}")
    s = st["sync"]
    if not s.get("last"):
        print("GitHub 同步：還沒跑過")
    else:
        print(f"GitHub 同步：{'正常' if s.get('ok') else '有狀況'}｜最後 {s.get('last', '—')}"
              f"｜{s.get('error') or s.get('note') or '本機與 GitHub 一致'}")
    print("-" * 100)
    for j in jobs:
        js = st["jobs"].get(j.key, {})
        res = "—" if "last_rc" not in js else ("成功" if js["last_rc"] == 0 else "失敗")
        print(f"{j.label:<22}｜上次成功 {js.get('last_ok', '—'):<19}｜上次結果 {res}"
              f"｜下次 {_next_due(j, now)}")
        if js.get("last_error"):
            print("    ↳ " + js["last_error"].splitlines()[0][:120])
    t = _read_json(DATA / "selftest_last.json", {})
    if isinstance(t, dict) and t.get("at"):
        bad = [str(r.get("label", "")) for r in (t.get("results") or []) if r.get("status") == "fail"]
        bad += [str(b) for b in (t.get("syntax_errors") or [])]
        print("-" * 100)
        print(f"排程試跑（沙盒＋怪資料）：{str(t['at'])[:16].replace('T', ' ')}｜"
              + ("全部通過" if t.get("ok") else "有問題：" + "、".join(bad)[:150])
              + ("（只試了部分項目）" if t.get("only") else "")
              + "｜立刻再試：py -3 -X utf8 job_selftest.py")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "tick":
        tick()
    elif cmd == "status":
        status()
    elif cmd == "sync":
        with Lock(wait_sec=180) as got:
            if not got:
                print("另一份還在跑，等不到")
                return
            st = load_state()
            ok = sync(st)
            save_state(st)
            print("同步完成" if ok else f"同步有狀況：{st['sync'].get('error')}")
    elif cmd == "selftest":
        import job_selftest
        sys.exit(job_selftest.main(args[1:]))
    elif cmd == "run" and len(args) >= 2:
        jobs = {j.key: j for j in load_jobs()}
        if args[1] not in jobs:
            print("沒有這個工作。可用的：" + "、".join(jobs))
            return
        use_git = "--no-git" not in args
        with Lock(wait_sec=300) as got:
            if not got:
                print("另一份還在跑，等不到")
                return
            st = load_state()
            if use_git:
                sync(st)
            ok = run_job(jobs[args[1]], st, [], use_git=use_git)
            save_state(st)
            sys.exit(0 if ok else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
