#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
2560 Cloud Monitor v4.2
=======================

狀態：
NO_SIGNAL
WATCH
TREND_READY
PRE-STRICT
STRICT

STRICT 原版保持不變：
4H CORE
- MA25 rising
- Close > MA25
- VolMA5 crosses above VolMA60

1D confirmation
- Close > MA25
- MA25 rising
- VolMA5 > VolMA60

20-bar dedup.

新增：
TREND_READY
- 1H 多頭
- 4H early 趨勢成立
- 4H relaxed volume 成立
- 1D soft 成立

PRE-STRICT
- 1H 多頭
- 4H structure 成立
- 4H relaxed volume 成立
- 1D strict confirmation 成立

用途：
WATCH = 初步候選
TREND_READY = 趨勢與量能開始成形
PRE-STRICT = 趨勢、量能、日線已成熟
STRICT = 原版 2560 正式高標準訊號

Public Gate data only.
No API key.
No orders.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from pathlib import Path
from email.header import Header


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

    query = urllib.parse.urlencode(
        params
    )

    url = BASE + path

    if query:
        url += "?" + query

    last_error = None

    for attempt in range(retries):

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "2560-cloud-monitor/4.2",
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
                min(
                    2 ** attempt,
                    8
                )
            )

    raise RuntimeError(
        f"Gate request failed: {last_error}"
    )


# ============================================================
# 合約辨識
# ============================================================

def norm(s):

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(s).upper()
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

    normalized_names = {
        norm(name): name
        for name in names
    }

    mapping = {}

    aliases = {
        "BRKB": [
            "BRKB",
            "BRKBG",
            "BRK.B",
        ],
        "TSM": [
            "TSM",
            "TSMUS",
        ],
    }

    for base in REQUESTED:

        candidates = [base]

        candidates.extend(
            aliases.get(
                base,
                []
            )
        )

        found = None

        for candidate in candidates:

            possible = [
                candidate,
                candidate + "USDT",
                candidate + "_USDT",
            ]

            for item in possible:

                key = norm(item)

                if key in normalized_names:

                    found = normalized_names[key]
                    break

            if found:
                break

        mapping[base] = found

    return mapping


# ============================================================
# K線
# ============================================================

def fetch(
    contract,
    interval,
    limit
):

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

        rows.append({
            "t": int(x["t"]),
            "o": float(x["o"]),
            "h": float(x["h"]),
            "l": float(x["l"]),
            "c": float(x["c"]),
            "v": float(x.get("v", 0)),
        })

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
# 均線
# ============================================================

def sma(
    values,
    n,
    i
):

    if i + 1 < n:
        return None

    return sum(
        values[
            i - n + 1:
            i + 1
        ]
    ) / n


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

        r["ma5"] = sma(
            closes,
            5,
            i
        )

        r["ma10"] = sma(
            closes,
            10,
            i
        )

        r["ma20"] = sma(
            closes,
            20,
            i
        )

        r["ma25"] = sma(
            closes,
            25,
            i
        )

        r["ma60"] = sma(
            closes,
            60,
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
# 1H 多頭確認
# ============================================================

def one_hour_confirm(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma25"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] > r["ma20"]
        and
        r["ma5"] > r["ma10"]
        and
        r["ma10"] > r["ma20"]
    )


# ============================================================
# 4H 正式結構
# ============================================================

def four_hour_structure(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    return (
        r["ma25"] > r["ma25_prev"]
        and
        r["c"] > r["ma25"]
    )


# ============================================================
# 4H early trend
# ============================================================

def four_hour_early(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    price_ok = (
        r["c"] > r["ma25"]
    )

    slope_ok = (
        r["ma25"] >= r["ma25_prev"]
    )

    return (
        price_ok
        or
        slope_ok
    )


# ============================================================
# 4H relaxed volume
#
# 不要求「剛好上穿60」
# ============================================================

def relaxed_volume_ok(r):

    needed = [
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    near_long_volume = (
        r["vma5"]
        >= r["vma60"] * 0.90
    )

    volume_rising = (
        r["vma5"]
        > r["vma5_prev"] * 1.02
    )

    return (
        near_long_volume
        or
        volume_rising
    )


# ============================================================
# 原版 4H STRICT CORE
# ============================================================

def core_ok(r):

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
        r["ma25"] > r["ma25_prev"]

        and

        r["c"] > r["ma25"]

        and

        r["vma5_prev"]
        <= r["vma60_prev"]

        and

        r["vma5"]
        > r["vma60"]
    )


# ============================================================
# 1D STRICT
# ============================================================

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
        r["c"] > r["ma25"]

        and

        r["ma25"] > r["ma25_prev"]

        and

        r["vma5"] > r["vma60"]
    )


# ============================================================
# 1D soft
# ============================================================

def daily_soft_confirm(r):

    if r is None:
        return False

    if r.get("ma25") is None:
        return False

    return (
        r["c"]
        >= r["ma25"] * 0.97
    )


# ============================================================
# Daily alignment
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


# ============================================================
# Strict history
# ============================================================

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
# ISO
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
    base,
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
            "base": base,
            "contract": contract,

            "group":
                "VALIDATED"
                if base in VALIDATED
                else "EXTENDED",

            "status":
                "WAIT_HISTORY",

            "bars_1h":
                len(r1),

            "bars_4h":
                len(r4),

            "bars_1d":
                len(rd),
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

    oneh_now = one_hour_confirm(
        latest1
    )

    oneh_prev = one_hour_confirm(
        previous1
    )

    oneh_fresh = (
        oneh_now
        and
        not oneh_prev
    )

    h4_structure = (
        four_hour_structure(
            latest4
        )
    )

    h4_early = (
        four_hour_early(
            latest4
        )
    )

    relaxed_volume = (
        relaxed_volume_ok(
            latest4
        )
    )

    h4_core = core_ok(
        latest4
    )

    day_strict = daily_confirm(
        latest_d
    )

    day_soft = daily_soft_confirm(
        latest_d
    )

    strict_raw = (
        h4_core
        and
        day_strict
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

    # ========================================================
    # PRE-STRICT
    #
    # 新版要求 VolRelax=True
    # ========================================================

    pre_strict = (
        not strict_now

        and

        oneh_now

        and

        h4_structure

        and

        relaxed_volume

        and

        day_strict
    )

    # ========================================================
    # TREND_READY
    # ========================================================

    trend_ready = (
        not strict_now

        and

        not pre_strict

        and

        oneh_now

        and

        h4_early

        and

        relaxed_volume

        and

        day_soft
    )

    # ========================================================
    # WATCH
    # ========================================================

    watch = (
        not strict_now

        and

        not pre_strict

        and

        not trend_ready

        and

        oneh_now
    )

    # ========================================================
    # STATUS
    # ========================================================

    if strict_now:

        status = "STRICT"

    elif pre_strict:

        status = "PRE-STRICT"

    elif trend_ready:

        status = "TREND_READY"

    elif watch:

        status = "WATCH"

    else:

        status = "NO_SIGNAL"

    volume_ratio = None

    if (
        latest4.get("vma5")
        and
        latest4.get("vma60")
    ):

        volume_ratio = (
            latest4["vma5"]
            /
            latest4["vma60"]
        )

    return {
        "base":
            base,

        "contract":
            contract,

        "group":
            "VALIDATED"
            if base in VALIDATED
            else "EXTENDED",

        "status":
            status,

        "latest_close":
            latest4["c"],

        "1h_confirm":
            oneh_now,

        "1h_fresh":
            oneh_fresh,

        "4h_early":
            h4_early,

        "4h_structure":
            h4_structure,

        "4h_volume_relaxed":
            relaxed_volume,

        "4h_volume_ratio":
            volume_ratio,

        "4h_core":
            h4_core,

        "1d_soft":
            day_soft,

        "1d_confirm":
            day_strict,

        "strict_raw":
            strict_raw,

        "strict":
            strict_now,

        "latest_1h_open_utc":
            iso(
                latest1["t"]
            ),

        "latest_4h_open_utc":
            iso(
                latest4["t"]
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
            "NTFY_TOPIC 未設定"
        )

        return False

    safe_title = str(
        Header(
            title,
            "utf-8"
        )
    )

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Title":
                safe_title,

            "Priority":
                priority,

            "Tags":
                tags,

            "Content-Type":
                "text/plain; charset=utf-8",
        }
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=20
        ) as resp:

            print(
                "NTFY:",
                resp.status,
                title
            )

            return True

    except Exception as e:

        print(
            "NTFY ERROR:",
            title,
            str(e)
        )

        return False


# ============================================================
# State
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

            state = json.load(
                f
            )

        state.setdefault(
            "symbols",
            {}
        )

        return state

    except Exception as e:

        print(
            "STATE LOAD ERROR:",
            str(e)
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
# 通知
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

    previous = symbol_state.get(
        "status",
        "UNKNOWN"
    )

    print(
        f"{base}: "
        f"{previous} -> {status}"
    )

    # 同狀態不重複通知
    if status == previous:

        symbol_state[
            "updated_utc"
        ] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        return

    ratio = r.get(
        "4h_volume_ratio"
    )

    ratio_text = (
        f"{ratio:.2f}"
        if ratio is not None
        else "N/A"
    )

    # ========================================================
    # STRICT
    # ========================================================

    if status == "STRICT":

        send_ntfy(
            f"2560 STRICT {base}",
            (
                f"{base} 正式 2560 STRICT\n"
                f"Gate：{r['contract']}\n"
                f"4H close：{r['latest_close']}\n"
                f"4H CORE=True\n"
                f"1D=True\n"
                f"Vol5/Vol60={ratio_text}\n\n"
                f"進入長壽多網格人工複核。"
            ),
            "high",
            "chart_with_upwards_trend,bell"
        )

    # ========================================================
    # PRE-STRICT
    # ========================================================

    elif status == "PRE-STRICT":

        send_ntfy(
            f"2560 PRE-STRICT {base}",
            (
                f"{base} 進入 PRE-STRICT\n"
                f"1H=True\n"
                f"4H structure=True\n"
                f"4H relaxed volume=True\n"
                f"1D=True\n"
                f"4H Core 尚未完成\n"
                f"Vol5/Vol60={ratio_text}\n\n"
                f"趨勢與量能已成熟，等待原版 Strict 觸發。"
            ),
            "high",
            "eyes,chart_with_upwards_trend"
        )

    # ========================================================
    # TREND_READY
    # ========================================================

    elif status == "TREND_READY":

        send_ntfy(
            f"2560 TREND READY {base}",
            (
                f"{base} 趨勢正在形成\n"
                f"1H=True\n"
                f"4H early=True\n"
                f"4H relaxed volume=True\n"
                f"1D soft=True\n"
                f"Vol5/Vol60={ratio_text}\n\n"
                f"尚不是 Strict。\n"
                f"開始人工注意，不代表直接開長壽網。"
            ),
            "default",
            "eyes,chart_with_upwards_trend"
        )

    # ========================================================
    # WATCH
    # ========================================================

    elif status == "WATCH":

        send_ntfy(
            f"2560 WATCH {base}",
            (
                f"{base} 進入 WATCH\n"
                f"1H 多頭已成立\n"
                f"4H / 量能 / 1D 尚未成熟\n"
                f"價格：{r['latest_close']}\n\n"
                f"先觀察。"
            ),
            "default",
            "eyes"
        )

    # ========================================================
    # NO_SIGNAL
    # ========================================================

    elif status == "NO_SIGNAL":

        if previous in (
            "WATCH",
            "TREND_READY",
            "PRE-STRICT",
            "STRICT"
        ):

            send_ntfy(
                f"2560 signal invalid {base}",
                (
                    f"{base} 2560 候選環境已失效\n"
                    f"前一狀態：{previous}\n"
                    f"價格：{r['latest_close']}"
                ),
                "default",
                "warning"
            )

    symbol_state[
        "status"
    ] = status

    symbol_state[
        "updated_utc"
    ] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "2560 Cloud Monitor | v4.2"
    )

    print(
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    state = load_state()

    contracts = discover_contracts()

    print(
        "\nCONTRACT MAP"
    )

    for base in REQUESTED:

        print(
            f"{base:<5} -> "
            f"{contracts.get(base)}"
        )

    results = []
    errors = []

    print(
        "\nSCAN"
    )

    for base in REQUESTED:

        contract = contracts.get(
            base
        )

        if not contract:

            print(
                f"{base:<5} NOT_FOUND"
            )

            continue

        try:

            r = analyze(
                base,
                contract
            )

            results.append(
                r
            )

            if (
                r["status"]
                == "WAIT_HISTORY"
            ):

                print(
                    f"{base:<5} "
                    f"WAIT_HISTORY "
                    f"1H={r['bars_1h']} "
                    f"4H={r['bars_4h']} "
                    f"1D={r['bars_1d']}"
                )

                continue

            ratio = r.get(
                "4h_volume_ratio"
            )

            ratio_text = (
                f"{ratio:.2f}"
                if ratio is not None
                else "N/A"
            )

            print(
                f"{base:<5} "
                f"{r['status']:<12} "
                f"contract={contract:<18} "
                f"4Hclose={r['latest_close']} "
                f"1H={r['1h_confirm']} "
                f"4Hearly={r['4h_early']} "
                f"4Hstruct={r['4h_structure']} "
                f"VolRelax={r['4h_volume_relaxed']} "
                f"V5/V60={ratio_text} "
                f"4Hcore={r['4h_core']} "
                f"1Dsoft={r['1d_soft']} "
                f"1D={r['1d_confirm']}"
            )

            notify_signal(
                r,
                state
            )

        except Exception as e:

            errors.append(
                (
                    base,
                    str(e)
                )
            )

            print(
                f"{base:<5} "
                f"ERROR {e}"
            )

        time.sleep(
            0.12
        )

    save_state(
        state
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
