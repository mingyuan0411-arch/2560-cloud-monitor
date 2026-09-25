#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
2560 Cloud Monitor v4
=====================

A 層：
- WATCH
- PRE-STRICT
- STRICT

2560 STRICT 核心：
4H CORE
1. MA25 上升
2. Close > MA25
3. VolMA5 由下往上穿 VolMA60

1D 確認
1. Close > MA25
2. MA25 上升
3. VolMA5 > VolMA60

PRE-STRICT：
- 1D 多頭確認
- 4H 多頭結構成立
- 最新完成 1H 剛轉為多頭

WATCH：
- 1D 多頭確認
- 4H 多頭結構成立
- 1H 多頭成立
- 但尚未達 PRE-STRICT / STRICT

通知：
- WATCH 黃色預警
- PRE-STRICT 橘色預警
- STRICT 綠色正式訊號
- 同一標的同一狀態只通知一次
- 狀態改變後才重新通知

資料：
Gate USDT 永續公開 API
不需要 API Key
不下單
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# 基本設定
# ============================================================

BASE = "https://api.gateio.ws/api/v4"

VALIDATED = [
    "BTC",
    "ETH",
    "XRP",
    "SOL",
    "BNB",
]

EXTENDED = [
    "ADA",
    "LTC",
    "LINK",
    "DOGE",
    "SUI",
    "HYPE",
    "MU",
    "VRT",
    "DELL",
    "NVDA",
    "TSM",
    "BRKB",
]

REQUESTED = VALIDATED + EXTENDED

ONE_H = 60 * 60
FOUR_H = 4 * 60 * 60
ONE_D = 24 * 60 * 60

LIMIT_1H = 220
LIMIT_4H = 220
LIMIT_1D = 120

DEDUP_BARS = 20

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

NTFY_TOPIC = os.getenv(
    "NTFY_TOPIC",
    ""
).strip()

STATE_DIR = Path(".monitor_state")
STATE_FILE = STATE_DIR / "2560_state.json"


# ============================================================
# Gate API
# ============================================================

def gate_get(path, params=None, retries=5):

    params = params or {}

    qs = urllib.parse.urlencode(params)

    url = BASE + path

    if qs:
        url += "?" + qs

    last_error = None

    for attempt in range(retries):

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "2560-cloud-monitor/4.0",
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=30
            ) as resp:

                return json.load(resp)

        except Exception as e:

            last_error = e

            time.sleep(
                min(2 ** attempt, 8)
            )

    raise RuntimeError(
        f"Gate request failed: {last_error}"
    )


# ============================================================
# 找 Gate 合約
# ============================================================

def norm(s):

    return re.sub(
        r"[^A-Z0-9]",
        "",
        s.upper()
    )


def discover_contracts():

    data = gate_get(
        "/futures/usdt/contracts"
    )

    names = [
        x.get("name", "")
        for x in data
        if x.get("name")
    ]

    mapping = {}

    for base in REQUESTED:

        target = norm(
            base + "USDT"
        )

        exact = [
            name
            for name in names
            if norm(name) == target
        ]

        if exact:

            mapping[base] = exact[0]
            continue

        candidates = []

        for name in names:

            n = norm(name)

            if (
                n.endswith("USDT")
                and n[:-4] == norm(base)
            ):

                candidates.append(name)

        mapping[base] = (
            candidates[0]
            if candidates
            else None
        )

    return mapping


# ============================================================
# K 線
# ============================================================

def fetch(contract, interval, limit):

    raw = gate_get(
        "/futures/usdt/candlesticks",
        {
            "contract": contract,
            "interval": interval,
            "limit": limit,
        }
    )

    rows = []

    for x in raw:

        rows.append(
            {
                "t": int(x["t"]),
                "o": float(x["o"]),
                "h": float(x["h"]),
                "l": float(x["l"]),
                "c": float(x["c"]),
                "v": float(
                    x.get("v", 0)
                ),
            }
        )

    rows.sort(
        key=lambda z: z["t"]
    )

    return rows


def completed_only(
    rows,
    seconds,
    now_ts
):

    return [
        r
        for r in rows
        if r["t"] + seconds <= now_ts
    ]


# ============================================================
# 指標
# ============================================================

def sma(values, length, i):

    if i + 1 < length:
        return None

    return sum(
        values[
            i - length + 1:
            i + 1
        ]
    ) / length


def add_indicators(
    rows,
    seconds
):

    closes = [
        r["c"]
        for r in rows
    ]

    volumes = [
        r["v"]
        for r in rows
    ]

    for i, r in enumerate(rows):

        r["i"] = i

        r["ma25"] = sma(
            closes,
            25,
            i
        )

        r["ma25_prev"] = (
            sma(
                closes,
                25,
                i - 1
            )
            if i >= 25
            else None
        )

        r["vma5"] = sma(
            volumes,
            5,
            i
        )

        r["vma60"] = sma(
            volumes,
            60,
            i
        )

        r["vma5_prev"] = (
            sma(
                volumes,
                5,
                i - 1
            )
            if i >= 5
            else None
        )

        r["vma60_prev"] = (
            sma(
                volumes,
                60,
                i - 1
            )
            if i >= 60
            else None
        )

        r["close_t"] = (
            r["t"] + seconds
        )


# ============================================================
# 趨勢判斷
# ============================================================

def structure_ok(r):

    if r is None:
        return False

    if (
        r.get("ma25") is None
        or r.get("ma25_prev") is None
    ):
        return False

    return (
        r["ma25"]
        > r["ma25_prev"]
        and
        r["c"]
        > r["ma25"]
    )


def core_ok(r):

    if r is None:
        return False

    needed = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
        r.get("vma60_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["ma25"]
        > r["ma25_prev"]

        and

        r["c"]
        > r["ma25"]

        and

        r["vma5_prev"]
        <= r["vma60_prev"]

        and

        r["vma5"]
        > r["vma60"]
    )


def daily_confirm(r):

    if r is None:
        return False

    needed = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"]
        > r["ma25"]

        and

        r["ma25"]
        > r["ma25_prev"]

        and

        r["vma5"]
        > r["vma60"]
    )


# ============================================================
# 歷史對齊
# ============================================================

def last_completed_daily_asof(
    daily,
    close_t
):

    ans = None

    for r in daily:

        if r["close_t"] <= close_t:

            ans = r

        else:

            break

    return ans


def strict_raw_at(
    r4,
    daily
):

    d = last_completed_daily_asof(
        daily,
        r4["close_t"]
    )

    return (
        core_ok(r4)
        and
        daily_confirm(d)
    )


def kept_strict(
    r4,
    daily
):

    raw = []

    for r in r4:

        if strict_raw_at(
            r,
            daily
        ):

            raw.append(r)

    kept = []

    last_i = -10**9

    for r in raw:

        if (
            r["i"] - last_i
            >= DEDUP_BARS
        ):

            kept.append(r)

            last_i = r["i"]

    return kept


# ============================================================
# 格式
# ============================================================

def iso(ts):

    return datetime.fromtimestamp(
        ts,
        timezone.utc
    ).isoformat()


# ============================================================
# 單一標的分析
# ============================================================

def analyze(
    base_symbol,
    contract
):

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    r1 = completed_only(
        fetch(
            contract,
            "1h",
            LIMIT_1H
        ),
        ONE_H,
        now_ts
    )

    r4 = completed_only(
        fetch(
            contract,
            "4h",
            LIMIT_4H
        ),
        FOUR_H,
        now_ts
    )

    rd = completed_only(
        fetch(
            contract,
            "1d",
            LIMIT_1D
        ),
        ONE_D,
        now_ts
    )

    if (
        len(r1) < 65
        or
        len(r4) < 65
        or
        len(rd) < 65
    ):

        return {
            "base": base_symbol,
            "contract": contract,
            "group": (
                "VALIDATED"
                if base_symbol
                in VALIDATED
                else "EXTENDED"
            ),
            "status": "WAIT_HISTORY",
            "bars_1h": len(r1),
            "bars_4h": len(r4),
            "bars_1d": len(rd),
        }

    add_indicators(
        r1,
        ONE_H
    )

    add_indicators(
        r4,
        FOUR_H
    )

    add_indicators(
        rd,
        ONE_D
    )

    latest1 = r1[-1]

    previous1 = r1[-2]

    latest4 = r4[-1]

    latest_d = (
        last_completed_daily_asof(
            rd,
            latest4["close_t"]
        )
    )

    oneh_now = structure_ok(
        latest1
    )

    oneh_prev = structure_ok(
        previous1
    )

    oneh_fresh = (
        oneh_now
        and
        not oneh_prev
    )

    fourh_structure = structure_ok(
        latest4
    )

    fourh_core = core_ok(
        latest4
    )

    daily_now = daily_confirm(
        latest_d
    )

    strict_raw = (
        fourh_core
        and
        daily_now
    )

    strict_kept = kept_strict(
        r4,
        rd
    )

    strict_now = (
        bool(strict_kept)
        and
        strict_kept[-1]["t"]
        == latest4["t"]
    )

    pre_strict = (
        not strict_now
        and
        daily_now
        and
        fourh_structure
        and
        oneh_fresh
    )

    watch = (
        not strict_now
        and
        not pre_strict
        and
        daily_now
        and
        fourh_structure
        and
        oneh_now
    )

    if strict_now:

        status = "STRICT"

    elif pre_strict:

        status = "PRE-STRICT"

    elif watch:

        status = "WATCH"

    else:

        status = "NO_SIGNAL"

    return {
        "base": base_symbol,
        "contract": contract,

        "group": (
            "VALIDATED"
            if base_symbol
            in VALIDATED
            else "EXTENDED"
        ),

        "status": status,

        "latest_close":
            latest4["c"],

        "1h_close":
            latest1["c"],

        "1h_confirm":
            oneh_now,

        "1h_fresh_confirm":
            oneh_fresh,

        "4h_structure":
            fourh_structure,

        "4h_core":
            fourh_core,

        "1d_confirm":
            daily_now,

        "strict_raw":
            strict_raw,

        "strict":
            strict_now,

        "latest_1h_open_utc":
            iso(
                latest1["t"]
            ),

        "latest_1h_close_utc":
            iso(
                latest1["close_t"]
            ),

        "latest_4h_open_utc":
            iso(
                latest4["t"]
            ),

        "latest_4h_close_utc":
            iso(
                latest4["close_t"]
            ),
    }


# ============================================================
# NTFY
# ============================================================

def send_ntfy(
    title,
    msg,
    priority="default",
    tags="bell"
):

    if not NTFY_TOPIC:

        print(
            "NTFY_TOPIC 未設定，略過通知"
        )

        return

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type":
                "text/plain; charset=utf-8",
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=20
    ) as resp:

        print(
            "ntfy:",
            resp.status,
            title
        )


# ============================================================
# 狀態記憶
# ============================================================

def load_state():

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not STATE_FILE.exists():

        return {
            "symbols": {}
        }

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print(
            "狀態檔讀取失敗，重新建立：",
            e
        )

        return {
            "symbols": {}
        }


def save_state(state):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 通知邏輯
# ============================================================

def notify_signal(
    r,
    state
):

    base = r["base"]

    status = r["status"]

    symbol_state = (
        state
        .setdefault(
            "symbols",
            {}
        )
        .setdefault(
            base,
            {}
        )
    )

    previous_status = (
        symbol_state
        .get(
            "status",
            "UNKNOWN"
        )
    )

    changed = (
        status
        != previous_status
    )

    print(
        f"{base}: "
        f"{previous_status} -> {status}"
    )

    # 狀態沒變，不重複通知
    if not changed:

        return

    label = (
        "已驗證組"
        if r.get("group")
        == "VALIDATED"
        else
        "擴充監控組"
    )

    if status == "WATCH":

        send_ntfy(
            f"2560 WATCH {base}",
            (
                f"{base} 進入 WATCH 黃色預警\n"
                f"組別：{label}\n"
                f"Gate：{r['contract']}\n"
                f"4H Close：{r['latest_close']}\n"
                f"1H 多頭：{r['1h_confirm']}\n"
                f"4H 結構：{r['4h_structure']}\n"
                f"1D 確認：{r['1d_confirm']}\n"
                f"尚未達 PRE-STRICT / STRICT\n"
                f"先觀察，不代表立即進場。"
            ),
            "default",
            "eyes,warning"
        )

    elif status == "PRE-STRICT":

        send_ntfy(
            f"2560 PRE-STRICT {base}",
            (
                f"{base} 進入 PRE-STRICT 橘色預警\n"
                f"組別：{label}\n"
                f"Gate：{r['contract']}\n"
                f"4H Close：{r['latest_close']}\n"
                f"1H 多頭：{r['1h_confirm']}\n"
                f"1H 剛轉強：{r['1h_fresh_confirm']}\n"
                f"4H 結構：{r['4h_structure']}\n"
                f"1D 確認：{r['1d_confirm']}\n"
                f"距離正式 Strict 更近，準備人工複核。"
            ),
            "high",
            "eyes,chart_with_upwards_trend"
        )

    elif status == "STRICT":

        send_ntfy(
            f"2560 STRICT {base}",
            (
                f"{base} 出現正式 2560 Strict 多頭訊號\n"
                f"組別：{label}\n"
                f"Gate：{r['contract']}\n"
                f"4H Close：{r['latest_close']}\n"
                f"4H CORE：{r['4h_core']}\n"
                f"1D：{r['1d_confirm']}\n"
                f"進入人工複核：永續方向 + 長壽多網格。"
            ),
            "high",
            "chart_with_upwards_trend,bell"
        )

    # 更新狀態
    symbol_state["status"] = status

    symbol_state["updated_utc"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    symbol_state["contract"] = (
        r.get("contract")
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "2560 Cloud Monitor | v4"
    )

    print(
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    state = load_state()

    try:

        contract_map = (
            discover_contracts()
        )

    except Exception as e:

        send_ntfy(
            "2560 系統故障",
            f"Gate 合約清單取得失敗：{e}",
            "high",
            "warning"
        )

        raise

    print(
        "\nCONTRACT MAP"
    )

    for base in REQUESTED:

        print(
            f"{base:<5} -> "
            f"{contract_map.get(base)}"
        )

    results = []

    errors = []

    print(
        "\nSCAN"
    )

    for base in REQUESTED:

        contract = (
            contract_map
            .get(base)
        )

        if not contract:

            print(
                f"{base:<5} NOT_FOUND"
            )

            results.append(
                {
                    "base": base,
                    "status":
                        "NOT_FOUND"
                }
            )

            continue

        try:

            r = analyze(
                base,
                contract
            )

            results.append(r)

            if (
                r["status"]
                == "WAIT_HISTORY"
            ):

                print(
                    f"{base:<5} "
                    f"WAIT_HISTORY "
                    f"contract={contract:<18} "
                    f"1H={r['bars_1h']} "
                    f"4H={r['bars_4h']} "
                    f"1D={r['bars_1d']}"
                )

            else:

                print(
                    f"{base:<5} "
                    f"{r['status']:<10} "
                    f"contract={contract:<18} "
                    f"4Hclose={r['latest_close']} "
                    f"1H={r['1h_confirm']} "
                    f"1Hfresh={r['1h_fresh_confirm']} "
                    f"4Hstruct={r['4h_structure']} "
                    f"4Hcore={r['4h_core']} "
                    f"1D={r['1d_confirm']}"
                )

                notify_signal(
                    r,
                    state
                )

        except Exception as e:

            print(
                f"{base:<5} ERROR {e}"
            )

            errors.append(
                (
                    base,
                    str(e)
                )
            )

        time.sleep(0.12)

    save_state(state)

    if len(errors) >= 3:

        summary = "\n".join(
            f"{a}: {b}"
            for a, b
            in errors[:10]
        )

        send_ntfy(
            "2560 雲端監控異常",
            (
                f"本輪有 {len(errors)} 個標的發生錯誤：\n"
                f"{summary}"
            ),
            "high",
            "warning"
        )

    counts = {}

    for r in results:

        status = r.get(
            "status",
            "UNKNOWN"
        )

        counts[status] = (
            counts.get(
                status,
                0
            )
            + 1
        )

    print(
        "\nSTATUS COUNTS:",
        counts
    )

    print(
        "ERROR COUNT:",
        len(errors)
    )

    print(
        "STATE FILE:",
        STATE_FILE
    )


if __name__ == "__main__":

    main()
