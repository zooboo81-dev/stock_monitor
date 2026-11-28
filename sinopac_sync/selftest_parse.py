# -*- coding: utf-8 -*-
r"""parse_sinopac.py 的自我檢查：全部用假資料（自己編的代號、股數、價格），不讀任何真的對帳單、不連網。
改了 parse_sinopac.py 一定要跑：  py -3 -X utf8 sinopac_sync\selftest_parse.py
重點是「看不懂的版面一定要核對沒過」，不可以被當成沒有庫存。全部 PASS 結束碼 0。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_sinopac as ps  # noqa: E402

_results = []


def check(name, ok, note=""):
    _results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"（{note}）" if note and not ok else ""))


# 表頭的位置（跟真的版面同一種排法：數字靠右對齊在表頭右邊一點）
HEAD = [("交易別", 14.7, 31.2), ("證券", 69.4, 80.4), ("庫存餘額", 122.6, 144.6), ("平均成本價格", 165.4, 198.4),
        ("總投資成本", 216.5, 244.0), ("參考市價", 267.5, 289.5), ("參考市値", 315.8, 337.8), ("累計配息", 460.7, 482.7)]
RIGHT = {"shares": 156.4, "avg_cost": 204.7, "cost": 253.0, "ref_price": 301.3, "ref_value": 349.6}


def w(text, x0, y, x1=None):
    x1 = x1 if x1 is not None else x0 + 6 * len(text)
    return (x0, y, x1, y + 6, text, 0, 0, 0)


def num(text, right, y):
    return w(text, right - 4.2 * len(text), y, right)


def row(y, kind, code, name, shares, avg, cost, price, value):
    out = [w(kind, 17.5, y, 28.5), w(code, 48.1, y, 67.1), w(name, 77.2, y - 2.7, 107.2)]
    for key, v in (("shares", shares), ("avg_cost", avg), ("cost", cost), ("ref_price", price), ("ref_value", value)):
        out.append(num(v, RIGHT[key], y))
    return out


def page(rows=(), title="證券庫存", head=HEAD, subtotal=None, as_of="※截至2031年05月30日庫存餘額如上表資料，參考市價為庫存當日之收盤價。",
         trades=True, extra_text=""):
    words, y = [], 123.4
    if title:
        if isinstance(title, (list, tuple)):
            x = 285.1
            for t in title:
                words.append(w(t, x, y))
                x += 6 * len(t) + 2
        else:
            words.append(w(title, 285.1, y, 313.1))
    for text, x0, x1 in head:
        words.append(w(text, x0, 138.3, x1))
    y = 163.8
    for r in rows:
        words += row(y, *r)
        y += 11.3
    if subtotal:
        words.append(w("小計", 86.7, y, 97.7))
        for key, v in subtotal.items():
            words.append(num(v, RIGHT[key], y))
        y += 11.3
    if as_of:
        words.append(w(as_of, 261.3, y, 591.1))
    if trades:
        words.append(w("證券交易明細", 278.1, y + 15, 320.1))
    text = "成交年月：203105\n" + "\n".join(t[4] for t in words) + "\n" + extra_text
    return words, text


class FakePage:
    def __init__(self, words, text):
        self._w, self._t = words, text

    def get_text(self, kind):
        return self._t if kind == "text" else self._w


def doc(*pages):
    return [FakePage(*p) for p in pages]


def passed(st):
    return all(st["checks"].values())


def failed_names(st):
    return [k for k, v in st["checks"].items() if not v]


R1 = ("現股", "1101", "甲乙電子", "1,000", "40.00", "40,000", "50.00", "50,000")
R2 = ("現股", "00999A", "假想主動", "13,000", "19.33", "251,290", "20.00", "260,000")
SUB = {"shares": "14,000", "cost": "291,290", "ref_value": "310,000"}


def main() -> int:
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB)))
    h = {x["code"]: x for x in st["holdings"]}
    check("正常的兩列：核對全過", passed(st), str(failed_names(st)))
    check("正常的兩列：代號、股數、成本、市值都讀對",
          set(h) == {"1101", "00999A"} and h["1101"]["shares"] == 1000 and h["00999A"]["shares"] == 13000
          and h["00999A"]["cost"] == 251290 and h["1101"]["ref_value"] == 50000 and h["00999A"]["avg_cost"] == 19.33, str(h))
    check("截止日、成交年月讀對", st["as_of"] == date(2031, 5, 30) and st["ym"] == "203105", f"{st['as_of']} {st['ym']}")
    check("交易別讀對（現股）", all(x["kind"] == "現股" for x in st["holdings"]))

    # 看不懂的版面 → 一定要沒過（不可以當成沒有庫存）
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, title="證券庫存明細")))
    check("標題改了字 → 核對沒過", not passed(st) and st["holdings"] == [], str(failed_names(st)))
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, title=["證券", "庫存"])))
    check("標題被拆成兩個字 → 核對沒過", not passed(st), str(failed_names(st)))
    bad_head = [(t.replace("庫存餘額", "庫存股數").replace("平均成本價格", "成本均價").replace("總投資成本", "投資成本")
                 .replace("參考市價", "市價").replace("參考市値", "市值"), a, b) for t, a, b in HEAD]
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, head=bad_head,
                                     as_of="※截至2031年05月30日庫存餘額如上表資料")))
    check("表頭全部改名 → 核對沒過", not passed(st), str(failed_names(st)))
    st = ps.parse_statement(doc(page([], subtotal=None, title="證券庫存")))
    check("有庫存表的表頭、卻解析不出任何一列 → 核對沒過", not passed(st), str(failed_names(st)))

    # 真的沒有庫存：整份沒有庫存表的痕跡，只有交易明細
    empty_words = [w("證券交易明細", 278.1, 201.5, 320.1), w("小計", 15.7, 258.9, 26.7)]
    empty_text = "成交年月：203102\n證券交易明細\n小計\n(一)證券庫存之損益試算不含稅費之成本與現值\n(三)公式說明：未實現損益=參考市值-總投資成本"
    st = ps.parse_statement(doc((empty_words, empty_text)))
    check("整份沒有庫存表、只有交易明細 → 空庫存、核對全過、截止日用月底",
          passed(st) and st["holdings"] == [] and st["as_of"] == date(2031, 2, 28), f"{failed_names(st)} {st['as_of']}")
    st = ps.parse_statement(doc(([w("其他內容", 10, 10)], "成交年月：203102\n其他內容")))
    check("連交易明細都沒有（不是對帳單的樣子）→ 核對沒過", not passed(st), str(failed_names(st)))

    # 融資、融券：不處理，算沒過
    st = ps.parse_statement(doc(page([("融資",) + R1[1:], R2], subtotal=SUB)))
    check("有融資列 → 核對沒過", not passed(st) and "每列的交易別都是現股或零股（融資、融券不處理）" in failed_names(st), str(failed_names(st)))
    st = ps.parse_statement(doc(page([("融券",) + R1[1:], R2], subtotal=SUB)))
    check("有融券列 → 核對沒過", not passed(st), str(failed_names(st)))
    st = ps.parse_statement(doc(page([("零股",) + R1[1:], R2], subtotal=SUB)))
    check("零股列 → 照常通過", passed(st), str(failed_names(st)))

    # 數字對不起來
    st = ps.parse_statement(doc(page([R1, R2], subtotal=dict(SUB, shares="15,000"))))
    check("股數加總≠小計 → 沒過", not passed(st))
    st = ps.parse_statement(doc(page([R1, R2], subtotal=dict(SUB, ref_value="999,999"))))
    check("市值加總≠小計 → 沒過", not passed(st))
    st = ps.parse_statement(doc(page([R1[:5] + ("99,000",) + R1[6:], R2], subtotal=dict(SUB, cost="350,290"))))
    check("某列 股數×平均成本≠成本（欄位可能錯位）→ 沒過", not passed(st), str(failed_names(st)))
    st = ps.parse_statement(doc(page([R1[:7] + ("77,777",), R2], subtotal=dict(SUB, ref_value="337,777"))))
    check("某列 股數×市價≠市值 → 沒過", not passed(st))
    st = ps.parse_statement(doc(page([R1, R1], subtotal={"shares": "2,000", "cost": "80,000", "ref_value": "100,000"})))
    check("同一個代號出現兩次 → 沒過", not passed(st))
    st = ps.parse_statement(doc(page([R1, R2], subtotal=None)))
    check("沒有小計 → 沒過", not passed(st))

    # 日期
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, as_of="※截至120年05月30日庫存餘額如上表資料")))
    check("截止日是民國年 → 換成西元", st["as_of"] == date(2031, 5, 30) and passed(st), f"{st['as_of']} {failed_names(st)}")
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, as_of="※截至2031年06月02日庫存餘額如上表資料")))
    check("截止日不在成交年月那個月 → 沒過", not passed(st))
    st = ps.parse_statement(doc(page([R1, R2], subtotal=SUB, as_of=None)))
    check("有庫存卻沒有截止日 → 沒過", not passed(st))

    bad = _results.count(False)
    print(f"結果：{len(_results) - bad} PASS、{bad} FAIL")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
