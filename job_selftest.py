"""排程試跑 — 在「沙盒」把每個台股排程工作真的跑一遍，事先抓出會當掉的程式

為什麼要有這支（2026-11-24）：
  晨間摘要遇到 portfolio.csv 的定存區預留列（成本 0）會除以 0、整支當掉；雲端 9/11 起每天失敗沒人發現，
  11/24 搬到這台電腦跑才被失敗推播抓到。同一個坑 7/14 在盤中警示修過，其他程式沒跟著修。
  這種「平常沒跑到、碰到特定資料才炸」的問題，要在真正的排程時間之前先抓出來 → 就是這支。

做法：
  1. 所有（有進版控的）.py 先做語法檢查
  2. 把 stock_monitor 複製一份到 data_export/_selftest/（不含 .git、金鑰、交易截圖、個人日誌）
  3. 在複製出來的 portfolio.csv 後面加幾列「怪資料」：成本 0、股數 0 但成本還在、停損空白、停損 0、
     成本空白、數字寫成 1000.0、查不到報價的代號 → 證明程式碰到這些不會當掉
  4. 用跟排程一樣的指令把每個工作跑一次（盤中警示加 --force、晨間摘要週末照跑、融資已處理過的日期重算）
     環境變數 STOCK_DRY_RUN=1：通知只印出來、不推到手機；Windows 右下角小視窗也不跳
  5. 列出每項 ✅／❌，結果寫到 data/selftest_last.json（local_runner.py status 會顯示）

用法：
  py -3 -X utf8 job_selftest.py                            # 全部跑（約 1～3 分鐘）
  py -3 -X utf8 job_selftest.py morning_briefing taiex_ma  # 只跑這幾項（local_runner 的工作代號）
  py -3 -X utf8 job_selftest.py --canary                   # 排程用：程式／持股／設定都沒改過、7 天內也全過 → 跳過
  py -3 -X utf8 job_selftest.py --keep                     # 跑完不清沙盒（要進去看時用）
  雙擊「試跑全部排程.bat」也可以。
改了排程會跑到的程式、或改了 portfolio.csv 的格式之後，請先跑一次這支再 commit。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

for _s in ("stdout", "stderr"):
    if getattr(sys, _s) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "data_export" / "_selftest"
RESULT_FILE = ROOT / "data" / "selftest_last.json"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

RECHECK_DAYS = 7        # --canary：什麼都沒改的話，最多隔幾天還是整套重跑（抓外部資料格式變了、套件升級）
JOB_CAP_SEC = 900       # 單一工作最多等幾秒（超過記「太慢」，不算當掉）
TOTAL_CAP_SEC = 1800    # 整套最多跑幾秒（排程裡跑時，後面的工作要等它）

# 不複製進沙盒：版本庫、金鑰（沙盒沒有金鑰 → 就算 STOCK_DRY_RUN 漏接也推不出去）、個人日誌、本機狀態
SKIP_DIRS = {".git", "__pycache__", "data_export", "tssco_sync", "cathay_sync", "verify_bundle", ".claude",
             ".venv", "venv", "trades"}
SKIP_FILES = {
    "telegram_config.json", "pushover_config.json", "shioaji_config.json", "shioaji_keys.txt",
    "API key.txt",
    "trade_journal.md", "trade_records.jsonl", "notifications_local.jsonl", "notifications_read.json",
    "local_runner_state.json", "local_runner.log", "local_runner.lock", "selftest_last.json",
    "telegram_intake_offset.json", "telegram_intake_applied.json", "telegram_intake_pending.json",
    "intake_debug.txt",
}
SKIP_SUFFIX = {".exe", ".log", ".pyc", ".lock", ".tmp"}

# 怪資料：附加在沙盒 portfolio.csv 後面。用真的有報價的代號，程式才會一路算到損益、停損那一段
EDGE_ROWS = [
    dict(code="2330", name="試跑-成本0", market="TSE", shares="1000", cost="0",
         category="試跑", stop_loss="500", entry_date="", hold_type="core"),
    dict(code="2317", name="試跑-賣光留列", market="TSE", shares="0", cost="150",
         category="試跑", stop_loss="120", entry_date="2026-01-05", hold_type="swing"),
    dict(code="2303", name="試跑-停損空白", market="TSE", shares="1000", cost="50",
         category="試跑", stop_loss="", entry_date="2026-01-05", hold_type="swing"),
    dict(code="2603", name="試跑-停損0", market="TSE", shares="1000", cost="40",
         category="試跑", stop_loss="0", entry_date="", hold_type="core"),
    dict(code="1101", name="試跑-成本空白", market="TSE", shares="1000", cost="",
         category="試跑", stop_loss="30", entry_date="", hold_type="core"),
    dict(code="2002", name="試跑-小數點", market="TSE", shares="1000.0", cost="25.0",
         category="試跑", stop_loss="20.0", entry_date="2026-01-05", hold_type="swing"),
    dict(code="9999", name="試跑-查無報價", market="TSE", shares="1000", cost="100",
         category="試跑", stop_loss="80", entry_date="", hold_type="core"),
]

# 沙盒裡的假 winotify：試跑時不跳 Windows 右下角通知（程式會優先 import 同資料夾的這支）
WINOTIFY_STUB = '''"""試跑用的假 winotify（只放在 data_export/_selftest 沙盒裡）：不跳 Windows 通知"""
class _Audio:
    Default = Silent = IM = Mail = Reminder = SMS = None

    def __getattr__(self, name):
        return None


audio = _Audio()


class Notification:
    def __init__(self, *a, **k):
        pass

    def set_audio(self, *a, **k):
        pass

    def add_actions(self, *a, **k):
        pass

    def show(self, *a, **k):
        print("[試跑・不跳 Windows 通知]")
'''

# 這些檔一改，--canary 就要整套重跑
FP_PATTERNS = ["*.py", "portfolio.csv", "events.json", "cooldown.json", "requirements.txt",
               "data/*policy*.json", "data/watchlist.json", "data/allocation_targets.json",
               "data/local_schedule.json"]


# ---------------------------------------------------------------- 小工具
def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _decode(x) -> str:
    if x is None:
        return ""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def python_exe() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")      # pythonw 啟動的話，子程序改用 python.exe 才收得到輸出
    return str(cand if cand.exists() else exe)


def fingerprint() -> str:
    """程式、持股、設定、Python／套件版本的指紋：有任何一個變了，--canary 就會整套重跑"""
    h = hashlib.sha256()
    for pat in FP_PATTERNS:
        for p in sorted(ROOT.glob(pat)):
            try:
                h.update(p.relative_to(ROOT).as_posix().encode() + b"\0" + p.read_bytes())
            except Exception:
                pass
    h.update(sys.version.encode())
    try:
        from importlib import metadata
        for pkg in ("pandas", "numpy", "requests", "yfinance", "truststore", "urllib3"):
            try:
                h.update(f"{pkg}={metadata.version(pkg)}".encode())
            except Exception:
                pass
    except Exception:
        pass
    return h.hexdigest()[:16]


def tracked_py() -> list:
    """有進版控的 .py（沒進版控的草稿檔有錯也不算）"""
    try:
        r = subprocess.run(["git", "ls-files", "--", "*.py"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
        names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and "/" not in ln.strip()]
        if r.returncode == 0 and names:
            return [ROOT / n for n in names if (ROOT / n).exists()]
    except Exception:
        pass
    return sorted(ROOT.glob("*.py"))


def syntax_check() -> tuple:
    bad, files = [], tracked_py()
    for p in files:
        try:
            compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
        except SyntaxError as e:
            bad.append(f"{p.name} 第 {e.lineno} 行：{e.msg}")
        except Exception as e:
            bad.append(f"{p.name}：{type(e).__name__}: {e}")
    return bad, len(files)


# ---------------------------------------------------------------- 沙盒
def add_edge_rows(p: Path) -> None:
    text = p.read_bytes().decode("utf-8")
    lines = text.splitlines()
    if not lines:
        return
    header = next(csv.reader([lines[0].lstrip("﻿")]))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in EDGE_ROWS:
        w.writerow([row.get(c.strip(), "") for c in header])
    if not text.endswith(("\n", "\r")):
        text += "\n"
    p.write_bytes((text + buf.getvalue()).encode("utf-8"))


def reset_dedupe(sb: Path) -> None:
    """「這天已處理過就跳過」的記號拿掉，試跑才會真的算一遍（只動沙盒裡的副本）"""
    p = sb / "data" / "margin_fear_state.json"
    st = _read_json(p)
    if isinstance(st, dict) and st.pop("last_date", None) is not None:
        p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def build_sandbox() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    n = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(ROOT)
        for fn in filenames:
            if fn in SKIP_FILES or Path(fn).suffix.lower() in SKIP_SUFFIX:
                continue
            dst = SANDBOX / rel / fn
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(dirpath) / fn, dst)
            n += 1
    (SANDBOX / "winotify.py").write_text(WINOTIFY_STUB, encoding="utf-8")
    if (SANDBOX / "portfolio.csv").exists():
        add_edge_rows(SANDBOX / "portfolio.csv")
    reset_dedupe(SANDBOX)
    return n


# ---------------------------------------------------------------- 工作
def load_specs(only: set) -> tuple:
    """跟 local_runner 用同一份工作清單（含 local_schedule.json 的開關）；同一個指令只跑一次"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import local_runner
    specs, skipped = {}, []
    for j in local_runner.load_jobs():
        if j.key == "selftest" or not j.selftest or (only and j.key not in only):
            continue                           # selftest=False：要連信箱、吃個資檔的工作（國泰對帳單）不進沙盒
        if not j.enabled and not only:
            skipped.append(j.label)
            continue
        for cmd in j.cmds:
            cmd = list(cmd)
            if cmd[0] == "intraday_alert.py" and "--force" not in cmd:
                cmd.append("--force")          # 盤後、週末也要真的跑到持股那一段
            label = j.label + (f"：{cmd[0]}" if len(j.cmds) > 1 else "")
            s = specs.setdefault(tuple(cmd), {"cmd": cmd, "labels": [], "keys": [], "timeout": 0})
            if label not in s["labels"]:
                s["labels"].append(label)
                s["keys"].append(j.key)
            s["timeout"] = max(s["timeout"], j.timeout)
    return list(specs.values()), skipped


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GITHUB_ACTIONS", "PUSHOVER_USER_KEY", "PUSHOVER_API_TOKEN")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               STOCK_RUNNER="1", STOCK_DRY_RUN="1", STOCK_SELFTEST="1")
    return env


_ERR_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning)\b.*")


def error_line(out: str) -> str:
    """從輸出裡挑最像「錯在哪」的一行（Traceback 最後那行）"""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ERR_LINE.match(ln):
            return ln[:200]
    return (lines[-1] if lines else "（沒有輸出）")[:200]


def run_spec(spec: dict, budget_left: float) -> dict:
    timeout = int(max(30, min(spec["timeout"] or 600, JOB_CAP_SEC, budget_left)))
    t0 = time.time()
    try:
        r = subprocess.run([python_exe(), "-X", "utf8", *spec["cmd"]], cwd=SANDBOX, env=child_env(),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        rc, out = r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired as e:
        rc, out = 124, (_decode(e.stdout) + "\n" + _decode(e.stderr)).strip() + f"\n（超過 {timeout} 秒，已中止）"
    except Exception as e:
        rc, out = 125, f"啟動失敗：{type(e).__name__}: {e}"
    has_tb = "Traceback (most recent call last)" in out
    status = "slow" if rc == 124 else ("fail" if rc != 0 else ("warn" if has_tb else "ok"))
    return {
        "jobs": spec["keys"], "label": _label(spec), "cmd": " ".join(spec["cmd"]),
        "status": status, "rc": rc, "sec": round(time.time() - t0, 1),
        "error": "" if status == "ok" else error_line(out),
        "tail": "" if status == "ok" else "\n".join(out.splitlines()[-15:]),
        "dry_pushes": out.count("[試跑・不推播]"),
    }


ICON = {"ok": "✅", "warn": "⚠️", "slow": "⏱", "fail": "❌", "skip": "⏭"}


def _label(spec: dict) -> str:
    """同一個指令被好幾項工作共用時（例如晚上跟早上補跑的融資），只寫第一項、後面註明幾項"""
    lb = spec["labels"]
    return lb[0] + (f"（另 {len(lb) - 1} 項同指令）" if len(lb) > 1 else "")


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    canary, keep = "--canary" in argv, "--keep" in argv
    only = {a for a in argv if not a.startswith("--")}
    now = datetime.now()
    fp = fingerprint()

    last = _read_json(RESULT_FILE)
    if canary and not only and last.get("ok") and not last.get("only") and last.get("fingerprint") == fp:
        try:
            age = now - datetime.fromisoformat(str(last.get("at")))
        except ValueError:
            age = timedelta(days=999)
        if age < timedelta(days=RECHECK_DAYS):
            print(f"ℹ️ 程式、持股、設定都沒改過，{str(last.get('at'))[:16].replace('T', ' ')} 試跑全部通過"
                  f" → 這次跳過（滿 {RECHECK_DAYS} 天會再整套跑）")
            return 0

    t0 = time.time()
    print(f"=== 排程試跑 {now:%Y-%m-%d %H:%M}（沙盒＋{len(EDGE_ROWS)} 列怪資料，通知不推到手機）===")
    bad_syntax, n_py = syntax_check()
    print(f"語法檢查：{n_py} 支 .py " + ("都正常" if not bad_syntax else f"有 {len(bad_syntax)} 支壞掉"))
    for b in bad_syntax:
        print(f"   ❌ {b}")

    specs, skipped = load_specs(only)
    if only and not specs:
        print("沒有這些工作代號。可用的：intraday、morning_briefing、expiration_check、taiex_ma、"
              "watchlist、recs、margin_fear、allocation")
        return 2
    n_files = build_sandbox()
    print(f"沙盒：複製 {n_files} 個檔到 {SANDBOX.relative_to(ROOT)}（金鑰、交易截圖不複製）")
    for lb in skipped:
        print(f"⏭ {lb}：已在 local_schedule.json 關閉，不試跑")

    results = []
    for spec in specs:
        left = TOTAL_CAP_SEC - (time.time() - t0)
        if left < 30:
            results.append({"jobs": spec["keys"], "label": _label(spec),
                            "cmd": " ".join(spec["cmd"]), "status": "skip", "rc": None, "sec": 0,
                            "error": "整套試跑時間到，這項下次再試", "tail": "", "dry_pushes": 0})
        else:
            results.append(run_spec(spec, left))
        r = results[-1]
        extra = f"（試推播 {r['dry_pushes']} 則，沒有真的送出）" if r.get("dry_pushes") else ""
        print(f"{ICON[r['status']]} {r['label']}　{r['sec']:.0f} 秒{extra}")
        if r["status"] != "ok":
            print(f"    ↳ {r['error']}")

    failed = [r for r in results if r["status"] == "fail"]
    ok = not bad_syntax and not failed
    total = round(time.time() - t0, 1)

    RESULT_FILE.write_text(json.dumps({
        "at": now.isoformat(timespec="seconds"), "ok": ok, "fingerprint": fp,
        "mode": "canary" if canary else "manual", "only": sorted(only), "sec": total,
        "syntax_errors": bad_syntax, "edge_rows": len(EDGE_ROWS), "results": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # 失敗的細節（排程推播只帶最後十幾行，所以細節放前面、結論放最後一行）
    for r in failed:
        print(f"\n--- ❌ {r['label']}（{r['cmd']}）最後幾行 ---\n{r['tail']}")
    if ok and not keep:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    elif not ok:
        print(f"\n（沙盒留在 {SANDBOX}，可以進去重跑看錯在哪）")
    warns = [r for r in results if r["status"] in ("warn", "slow", "skip")]
    note = f"；{len(warns)} 項要留意（見上面 ⚠️⏱⏭）" if warns else ""
    if ok:
        print(f"\n結果：✅ 全部通過（{len(results)} 項、共 {total:.0f} 秒{note}）")
        return 0
    parts = [f"{r['label']}（{r['error'][:80]}）" for r in failed] + [f"語法：{b}" for b in bad_syntax]
    print(f"\n結果：❌ {len(parts)} 項會失敗：" + "、".join(parts))
    return 1


if __name__ == "__main__":
    sys.exit(main())
