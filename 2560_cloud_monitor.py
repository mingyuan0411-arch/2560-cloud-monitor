#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor — Expanded Pool v2
=====================================
Validated benchmark group:
BTC / ETH / XRP / SOL / BNB

Extended monitoring group:
ADA / LTC / LINK / DOGE / SUI / HYPE
MU / VRT / DELL / NVDA / TSM / BRKB

Rules:
4H CORE
- MA25 rising
- Close > MA25
- VolMA5 crosses above VolMA60

1D confirmation
- Close > MA25
- MA25 rising
- VolMA5 > VolMA60

20-bar dedup per contract.
Completed candles only.
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

FOUR_H = 4 * 3600
ONE_D = 24 * 3600
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
                headers={"Accept":"application/json","User-Agent":"2560-cloud-monitor/2.0"}
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
    rows=[]
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

def completed_only(rows, step, cutoff=None):
    if cutoff is None:
        cutoff = int(datetime.now(timezone.utc).timestamp())
    return [r for r in rows if r["t"] + step <= cutoff]

def sma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i-n+1:i+1]) / n

def add_ind(rows, step):
    closes=[r["c"] for r in rows]
    vols=[r["v"] for r in rows]
    for i,r in enumerate(rows):
        r["i"]=i
        r["ma25"]=sma(closes,25,i)
        r["ma25_prev"]=sma(closes,25,i-1) if i>=25 else None
        r["vma5"]=sma(vols,5,i)
        r["vma60"]=sma(vols,60,i)
        r["vma5_prev"]=sma(vols,5,i-1) if i>=5 else None
        r["vma60_prev"]=sma(vols,60,i-1) if i>=60 else None
        r["close_t"]=r["t"]+step

def core_ok(r):
    need=[r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60"),
          r.get("vma5_prev"),r.get("vma60_prev")]
    if any(x is None for x in need):
        return False
    return (
        r["ma25"] > r["ma25_prev"] and
        r["c"] > r["ma25"] and
        r["vma5_prev"] <= r["vma60_prev"] and
        r["vma5"] > r["vma60"]
    )

def daily_ok(r):
    if r is None:
        return False
    need=[r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60")]
    if any(x is None for x in need):
        return False
    return (
        r["c"] > r["ma25"] and
        r["ma25"] > r["ma25_prev"] and
        r["vma5"] > r["vma60"]
    )

def last_completed_daily_asof(daily, close_t):
    ans=None
    for r in daily:
        if r["close_t"] <= close_t:
            ans=r
        else:
            break
    return ans

def strict_raw_at(r4, rd):
    d=last_completed_daily_asof(rd,r4["close_t"])
    return core_ok(r4) and daily_ok(d), d

def kept_strict(r4, rd):
    raw=[]
    for r in r4:
        ok,_=strict_raw_at(r,rd)
        if ok:
            raw.append(r)
    kept=[]
    last_i=-10**9
    for r in raw:
        if r["i"] - last_i >= DEDUP_BARS:
            kept.append(r)
            last_i=r["i"]
    return kept

def iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).isoformat()

def analyze(base_symbol, contract):
    now=int(datetime.now(timezone.utc).timestamp())
    r4=completed_only(fetch(contract,"4h",LIMIT_4H),FOUR_H,now)
    rd=completed_only(fetch(contract,"1d",LIMIT_1D),ONE_D,now)

    if len(r4)<65 or len(rd)<65:
        raise RuntimeError(f"insufficient candles: 4h={len(r4)} 1d={len(rd)}")

    add_ind(r4,FOUR_H)
    add_ind(rd,ONE_D)

    latest=r4[-1]
    raw_now,d=strict_raw_at(latest,rd)
    kept=kept_strict(r4,rd)
    strict_now=bool(kept and kept[-1]["t"] == latest["t"])

    return {
        "base":base_symbol,
        "contract":contract,
        "group":"VALIDATED" if base_symbol in VALIDATED else "EXTENDED",
        "latest_4h_open_utc":iso(latest["t"]),
        "latest_4h_close_utc":iso(latest["close_t"]),
        "latest_close":latest["c"],
        "4h_core":core_ok(latest),
        "1d_confirm":daily_ok(d),
        "strict_raw":raw_now,
        "strict":strict_now,
    }

def notify_ntfy(r):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; notification skipped.")
        return

    label = "已驗證組" if r["group"]=="VALIDATED" else "擴充監控組"
    title=f"2560 STRICT {r['base']}"
    msg=(
        f"{r['base']} 出現新的 2560 Strict 多頭訊號\n"
        f"組別: {label}\n"
        f"Gate contract: {r['contract']}\n"
        f"4H: {r['latest_4h_open_utc']}\n"
        f"Close: {r['latest_close']}\n"
        f"4H CORE: {r['4h_core']} | 1D: {r['1d_confirm']}"
    )

    req=urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title":title,
            "Priority":"high",
            "Tags":"chart_with_upwards_trend,bell",
            "Content-Type":"text/plain; charset=utf-8",
        }
    )
    with urllib.request.urlopen(req,timeout=20) as resp:
        print("ntfy:",resp.status,r["base"])

def main():
    print("2560 Cloud Monitor | Expanded Pool v2")
    print(datetime.now(timezone.utc).isoformat())

    contract_map=discover_contracts()
    print("\nCONTRACT MAP")
    for base in REQUESTED:
        print(f"{base:<5} -> {contract_map.get(base)}")

    stricts=[]
    results=[]

    print("\nSCAN")
    for base in REQUESTED:
        contract=contract_map.get(base)
        if not contract:
            print(f"{base:<5} NOT_FOUND")
            results.append({
                "base":base,
                "group":"VALIDATED" if base in VALIDATED else "EXTENDED",
                "status":"NOT_FOUND"
            })
            continue

        try:
            r=analyze(base,contract)
            results.append(r)
            print(
                f"{base:<5} "
                f"{'STRICT' if r['strict'] else 'NO_SIGNAL':<9} "
                f"contract={contract:<18} "
                f"close={r['latest_close']} "
                f"4H={r['4h_core']} 1D={r['1d_confirm']}"
            )
            if r["strict"]:
                stricts.append(r)
        except Exception as e:
            print(f"{base:<5} ERROR {e}")
            results.append({
                "base":base,
                "contract":contract,
                "group":"VALIDATED" if base in VALIDATED else "EXTENDED",
                "status":"ERROR",
                "error":str(e)
            })

        time.sleep(0.15)

    for r in stricts:
        notify_ntfy(r)

    print("\nSTRICT count:",len(stricts))

if __name__=="__main__":
    main()
