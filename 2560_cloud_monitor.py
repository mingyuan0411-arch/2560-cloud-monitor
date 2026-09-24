#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor — 1H Radar v3
================================
核心原則：
- 4H Strict 規則完全不改
- 1H 只做預警，不參與 Strict 歷史績效宣稱
- 1D 是大方向濾網
- 4H 是正式扳機
- 1H 是雷達

三層狀態：
WATCH:
  1D confirm = True
  1H trend confirm = True

PRE-STRICT:
  WATCH = True
  且 4H 已具備趨勢骨架：
  MA25 向上 + Close > MA25
  但尚未出現正式 4H VolMA5 上穿 VolMA60

STRICT:
  原封版規則
  4H MA25向上 + Close>MA25 + VolMA5上穿VolMA60
  + completed 1D confirm
  + 20-bar dedup

通知策略：
- WATCH：只記錄，不推播
- PRE-STRICT：只有 1H confirm 剛由 False -> True 時推播一次
- STRICT：只在最新完成 4H K 剛收線後的排程點推播
- WAIT_HISTORY：不算錯誤，不推播
- 系統性 ERROR：3 個以上標的一次失敗才推播故障通知

No API key. No orders.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.gateio.ws/api/v4"

VALIDATED = ["BTC","ETH","XRP","SOL","BNB"]
EXTENDED = ["ADA","LTC","LINK","DOGE","SUI","HYPE","MU","VRT","DELL","NVDA","TSM","BRKB"]
REQUESTED = VALIDATED + EXTENDED

ONE_H = 3600
FOUR_H = 4 * 3600
ONE_D = 24 * 3600

LIMIT_1H = 220
LIMIT_4H = 220
LIMIT_1D = 120
DEDUP_BARS = 20

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

def gate_get(path, params=None, retries=5):
    params = params or {}
    qs = urllib.parse.urlencode(params)
    url = BASE + path + (("?" + qs) if qs else "")
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept":"application/json","User-Agent":"2560-cloud-monitor/3.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(min(2**k, 8))
    raise RuntimeError(f"Gate request failed: {last}")

def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())

def discover_contracts():
    data = gate_get("/futures/usdt/contracts")
    names = [x.get("name","") for x in data if x.get("name")]
    mapping = {}
    for base in REQUESTED:
        target = norm(base + "USDT")
        exact = [n for n in names if norm(n) == target]
        if exact:
            mapping[base] = exact[0]
            continue
        candidates = []
        for n in names:
            nn = norm(n)
            if nn.endswith("USDT") and nn[:-4] == norm(base):
                candidates.append(n)
        mapping[base] = candidates[0] if candidates else None
    return mapping

def fetch(contract, interval, limit):
    raw = gate_get("/futures/usdt/candlesticks", {
        "contract": contract,
        "interval": interval,
        "limit": limit,
    })
    rows = []
    for x in raw:
        rows.append({
            "t": int(x["t"]),
            "o": float(x["o"]),
            "h": float(x["h"]),
            "l": float(x["l"]),
            "c": float(x["c"]),
            "v": float(x.get("v",0)),
        })
    rows.sort(key=lambda z:z["t"])
    return rows

def completed_only(rows, step, cutoff):
    return [r for r in rows if r["t"] + step <= cutoff]

def sma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i-n+1:i+1]) / n

def add_ind(rows, step):
    closes = [r["c"] for r in rows]
    vols = [r["v"] for r in rows]
    for i,r in enumerate(rows):
        r["i"] = i
        r["ma25"] = sma(closes,25,i)
        r["ma25_prev"] = sma(closes,25,i-1) if i>=25 else None
        r["vma5"] = sma(vols,5,i)
        r["vma60"] = sma(vols,60,i)
        r["vma5_prev"] = sma(vols,5,i-1) if i>=5 else None
        r["vma60_prev"] = sma(vols,60,i-1) if i>=60 else None
        r["close_t"] = r["t"] + step

def enough_for_trend(r):
    need = [r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60")]
    return all(x is not None for x in need)

def trend_confirm(r):
    if not enough_for_trend(r):
        return False
    return (
        r["c"] > r["ma25"]
        and r["ma25"] > r["ma25_prev"]
        and r["vma5"] > r["vma60"]
    )

def fourh_structure(r):
    if r.get("ma25") is None or r.get("ma25_prev") is None:
        return False
    return r["ma25"] > r["ma25_prev"] and r["c"] > r["ma25"]

def core_ok(r):
    need = [
        r.get("ma25"),r.get("ma25_prev"),
        r.get("vma5"),r.get("vma60"),
        r.get("vma5_prev"),r.get("vma60_prev")
    ]
    if any(x is None for x in need):
        return False
    return (
        r["ma25"] > r["ma25_prev"]
        and r["c"] > r["ma25"]
        and r["vma5_prev"] <= r["vma60_prev"]
        and r["vma5"] > r["vma60"]
    )

def last_completed_daily_asof(daily, close_t):
    ans = None
    for r in daily:
        if r["close_t"] <= close_t:
            ans = r
        else:
            break
    return ans

def strict_raw_at(r4, rd):
    d = last_completed_daily_asof(rd, r4["close_t"])
    return core_ok(r4) and trend_confirm(d), d

def kept_strict(r4, rd):
    raw = []
    for r in r4:
        ok,_ = strict_raw_at(r,rd)
        if ok:
            raw.append(r)

    kept = []
    last_i = -10**9
    for r in raw:
        if r["i"] - last_i >= DEDUP_BARS:
            kept.append(r)
            last_i = r["i"]
    return kept

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def just_closed(close_t, now_ts, tolerance=20*60):
    age = now_ts - close_t
    return 0 <= age <= tolerance

def analyze(base_symbol, contract):
    now_ts = int(datetime.now(timezone.utc).timestamp())

    r1 = completed_only(fetch(contract,"1h",LIMIT_1H), ONE_H, now_ts)
    r4 = completed_only(fetch(contract,"4h",LIMIT_4H), FOUR_H, now_ts)
    rd = completed_only(fetch(contract,"1d",LIMIT_1D), ONE_D, now_ts)

    # Strict 至少要有 60期量能資料
    if len(r4) < 65 or len(rd) < 65:
        return {
            "base": base_symbol,
            "contract": contract,
            "group":"VALIDATED" if base_symbol in VALIDATED else "EXTENDED",
            "status":"WAIT_HISTORY",
            "bars_1h":len(r1),
            "bars_4h":len(r4),
            "bars_1d":len(rd),
        }

    if len(r1) < 65:
        return {
            "base": base_symbol,
            "contract": contract,
            "group":"VALIDATED" if base_symbol in VALIDATED else "EXTENDED",
            "status":"WAIT_HISTORY",
            "bars_1h":len(r1),
            "bars_4h":len(r4),
            "bars_1d":len(rd),
        }

    add_ind(r1, ONE_H)
    add_ind(r4, FOUR_H)
    add_ind(rd, ONE_D)

    latest1 = r1[-1]
    prev1 = r1[-2]
    latest4 = r4[-1]

    d_for_1h = last_completed_daily_asof(rd, latest1["close_t"])
    d_for_4h = last_completed_daily_asof(rd, latest4["close_t"])

    oneh_now = trend_confirm(latest1)
    oneh_prev = trend_confirm(prev1)
    oneh_fresh = oneh_now and not oneh_prev

    daily_now = trend_confirm(d_for_1h)
    h4_structure = fourh_structure(latest4)

    watch = daily_now and oneh_now
    pre_strict = watch and h4_structure and not core_ok(latest4)

    strict_raw = core_ok(latest4) and trend_confirm(d_for_4h)
    kept = kept_strict(r4,rd)
    strict_now = bool(kept and kept[-1]["t"] == latest4["t"])

    if strict_now:
        status = "STRICT"
    elif pre_strict:
        status = "PRE-STRICT"
    elif watch:
        status = "WATCH"
    else:
        status = "NO_SIGNAL"

    return {
        "base":base_symbol,
        "contract":contract,
        "group":"VALIDATED" if base_symbol in VALIDATED else "EXTENDED",
        "status":status,
        "latest_close":latest4["c"],
        "1h_close":latest1["c"],
        "1h_confirm":oneh_now,
        "1h_fresh_confirm":oneh_fresh,
        "4h_structure":h4_structure,
        "4h_core":core_ok(latest4),
        "1d_confirm":daily_now,
        "strict_raw":strict_raw,
        "strict":strict_now,
        "latest_1h_open_utc":iso(latest1["t"]),
        "latest_1h_close_utc":iso(latest1["close_t"]),
        "latest_4h_open_utc":iso(latest4["t"]),
        "latest_4h_close_utc":iso(latest4["close_t"]),
        "notify_pre_strict": pre_strict and oneh_fresh and just_closed(latest1["close_t"],now_ts),
        "notify_strict": strict_now and just_closed(latest4["close_t"],now_ts),
    }

def send_ntfy(title, msg, priority="default", tags="bell"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; notification skipped.")
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title":title,
            "Priority":priority,
            "Tags":tags,
            "Content-Type":"text/plain; charset=utf-8",
        }
    )
    with urllib.request.urlopen(req,timeout=20) as resp:
        print("ntfy:",resp.status,title)

def notify_signal(r):
    label = "已驗證組" if r["group"]=="VALIDATED" else "擴充監控組"

    if r.get("notify_strict"):
        send_ntfy(
            f"2560 STRICT {r['base']}",
            (
                f"{r['base']} 出現正式 2560 Strict 多頭訊號\\n"
                f"組別: {label}\\n"
                f"Gate: {r['contract']}\\n"
                f"4H: {r['latest_4h_open_utc']}\\n"
                f"Close: {r['latest_close']}\\n"
                f"4H CORE=True | 1D=True"
            ),
            "high",
            "chart_with_upwards_trend,bell"
        )

    elif r.get("notify_pre_strict"):
        send_ntfy(
            f"2560 PRE-STRICT {r['base']}",
            (
                f"{r['base']} 出現 1H 提前預警\\n"
                f"組別: {label}\\n"
                f"1D 多頭確認=True\\n"
                f"1H 剛轉強=True\\n"
                f"4H 趨勢骨架=True，但尚未正式量能上穿\\n"
                f"這不是正式開網訊號，等待 4H Strict。"
            ),
            "default",
            "eyes,chart_with_upwards_trend"
        )

def main():
    print("2560 Cloud Monitor | 1H Radar v3")
    print(datetime.now(timezone.utc).isoformat())

    try:
        contract_map = discover_contracts()
    except Exception as e:
        send_ntfy(
            "2560 系統故障",
            f"Gate 合約清單取得失敗：{e}",
            "high",
            "warning"
        )
        raise

    print("\\nCONTRACT MAP")
    for base in REQUESTED:
        print(f"{base:<5} -> {contract_map.get(base)}")

    results = []
    errors = []

    print("\\nSCAN")
    for base in REQUESTED:
        contract = contract_map.get(base)
        if not contract:
            print(f"{base:<5} NOT_FOUND")
            errors.append((base,"NOT_FOUND"))
            continue

        try:
            r = analyze(base,contract)
            results.append(r)

            if r["status"] == "WAIT_HISTORY":
                print(
                    f"{base:<5} WAIT_HISTORY "
                    f"contract={contract:<18} "
                    f"1H={r['bars_1h']} 4H={r['bars_4h']} 1D={r['bars_1d']}"
                )
            else:
                print(
                    f"{base:<5} {r['status']:<10} "
                    f"contract={contract:<18} "
                    f"4Hclose={r['latest_close']} "
                    f"1H={r['1h_confirm']} "
                    f"4Hstruct={r['4h_structure']} "
                    f"4Hcore={r['4h_core']} "
                    f"1D={r['1d_confirm']}"
                )
                notify_signal(r)

        except Exception as e:
            print(f"{base:<5} ERROR {e}")
            errors.append((base,str(e)))

        time.sleep(0.12)

    if len(errors) >= 3:
        summary = "\\n".join(f"{a}: {b}" for a,b in errors[:10])
        send_ntfy(
            "2560 雲端監控異常",
            f"本輪有 {len(errors)} 個標的發生錯誤：\\n{summary}",
            "high",
            "warning"
        )

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"],0) + 1

    print("\\nSTATUS COUNTS:", counts)
    print("ERROR COUNT:", len(errors))

if __name__ == "__main__":
    main()
