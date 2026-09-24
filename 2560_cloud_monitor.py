#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor — Frozen Strict v1
GitHub Actions + ntfy
-------------------------------------
- Gate USDT perpetual public candles
- No API key
- No order placement
- Uses completed 4H + completed 1D candles only
- Sends ntfy notification only when the latest completed 4H candle itself is STRICT
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.gateio.ws/api/v4"
SYMBOLS = ["BTC_USDT","ETH_USDT","XRP_USDT","SOL_USDT","BNB_USDT"]

FOUR_H = 4 * 3600
ONE_D = 24 * 3600
LIMIT_4H = 220
LIMIT_1D = 120
DEDUP_BARS = 20

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

def gate_get(path, params, retries=5):
    qs = urllib.parse.urlencode(params)
    url = BASE + path + "?" + qs
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept":"application/json","User-Agent":"2560-cloud-monitor/1.0"}
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(min(2**k, 8))
    raise RuntimeError(f"Gate request failed: {last}")

def fetch(symbol, interval, limit):
    raw = gate_get("/futures/usdt/candlesticks", {
        "contract": symbol,
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
    d = last_completed_daily_asof(rd, r4["close_t"])
    return core_ok(r4) and daily_ok(d), d

def kept_strict(r4, rd):
    raw=[]
    for r in r4:
        ok,_ = strict_raw_at(r, rd)
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

def analyze(symbol):
    now=int(datetime.now(timezone.utc).timestamp())
    r4=completed_only(fetch(symbol,"4h",LIMIT_4H),FOUR_H,now)
    rd=completed_only(fetch(symbol,"1d",LIMIT_1D),ONE_D,now)

    if len(r4)<65 or len(rd)<65:
        raise RuntimeError(f"insufficient candles 4h={len(r4)} 1d={len(rd)}")

    add_ind(r4,FOUR_H)
    add_ind(rd,ONE_D)

    latest=r4[-1]
    raw_now,d= strict_raw_at(latest,rd)
    kept=kept_strict(r4,rd)
    strict_now=bool(kept and kept[-1]["t"]==latest["t"])

    return {
        "symbol":symbol,
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

    title=f"2560 STRICT {r['symbol']}"
    msg=(
        f"{r['symbol']} 出現新的 2560 Strict 多頭訊號\n"
        f"4H: {r['latest_4h_open_utc']}\n"
        f"Close: {r['latest_close']}\n"
        f"4H CORE: {r['4h_core']} | 1D: {r['1d_confirm']}"
    )

    url=f"{NTFY_SERVER}/{NTFY_TOPIC}"
    req=urllib.request.Request(
        url,
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "chart_with_upwards_trend,bell",
            "Content-Type": "text/plain; charset=utf-8",
        }
    )
    with urllib.request.urlopen(req,timeout=20) as resp:
        print("ntfy:",resp.status)

def main():
    print("2560 Cloud Monitor | Frozen Strict v1")
    print(datetime.now(timezone.utc).isoformat())

    stricts=[]
    results=[]

    for s in SYMBOLS:
        try:
            r=analyze(s)
            results.append(r)
            print(
                s,
                "STRICT" if r["strict"] else "NO_SIGNAL",
                "close=",r["latest_close"],
                "4H=",r["4h_core"],
                "1D=",r["1d_confirm"]
            )
            if r["strict"]:
                stricts.append(r)
        except Exception as e:
            print(s,"ERROR",e)

    for r in stricts:
        notify_ntfy(r)

    print("STRICT count:",len(stricts))

if __name__=="__main__":
    main()
