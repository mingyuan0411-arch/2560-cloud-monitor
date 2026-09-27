#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
2560 Cloud Monitor v4.4
=======================

狀態：
NO_SIGNAL
WATCH
TREND_READY
PRE-STRICT
STRICT

原版 STRICT 規則保持不變：

4H CORE
- MA25 rising
- Close > MA25
- VolMA5 crosses above VolMA60

1D confirmation
- Close > MA25
- MA25 rising
- VolMA5 > VolMA60

20-bar dedup

v4.4 功能：
- WATCH / TREND_READY / PRE-STRICT / STRICT 階段價格記憶
- 各階段首次出現時間
- 5m 完成K作為接近現價
- 各階段 -> 目前 漲跌幅
- 各階段之間漲幅
- PRE-STRICT -> STRICT 追價幅度
- 最近4H壓力價 / 距離
- 最近1D壓力價 / 距離
- NO_SIGNAL 時保留本輪歷史
- 新週期開始時重置

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


# ============================================================
# K線時間
# ============================================================

FIVE_M = 5 * 60
ONE_H = 60 * 60
FOUR_H = 4 * 60 * 60
ONE_D = 24 * 60 * 60

LIMIT_5M = 20
LIMIT_1H = 220
LIMIT_4H = 220
LIMIT_1D = 120

DEDUP_BARS = 20


# ============================================================
# NTFY
# ============================================================

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

NTFY_TOPIC = os.getenv(
    "NTFY_TOPIC",
    ""
).strip()


# ============================================================
# State
# ============================================================

STATE_DIR = Path(".monitor_state")
STATE_FILE = STATE_DIR / "2560_state.json"


# ============================================================
# 基礎工具
# ============================================================

def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def iso(ts):

    return datetime.fromtimestamp(
        ts,
        timezone.utc
    ).isoformat()


def norm(s):

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(s).upper()
    )


def price_text(v):

    if v is None:
        return "N/A"

    if abs(v) >= 100:
        return f"{v:.2f}"

    if abs(v) >= 10:
        return f"{v:.3f}"

    if abs(v) >= 1:
        return f"{v:.4f}"

    return f"{v:.6f}"


def pct_change(
    start_price,
    end_price
):

    if (
        start_price is None
        or end_price is None
        or start_price <= 0
    ):
        return None

    return (
        end_price
        / start_price
        - 1
    ) * 100


def pct_text(v):

    if v is None:
        return "N/A"

    return f"{v:+.2f}%"


# ============================================================
# Gate API
# ============================================================

def gate_get(
    path,
    params=None,
    retries=5
):

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
                    "User-Agent": "2560-cloud-monitor/4.4",
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

    mapping = {}

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

                key = norm(
                    item
                )

                if key in normalized_names:

                    found = normalized_names[
                        key
                    ]

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
            r["t"]
            + seconds
        )


# ============================================================
# 1H 多頭確認
# ============================================================

def one_hour_confirm(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
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
        r["ma25"]
        > r["ma25_prev"]

        and

        r["c"]
        > r["ma25"]
    )


# ============================================================
# 4H Early
# ============================================================

def four_hour_early(r):

    if (
        r.get("ma25") is None
        or
        r.get("ma25_prev") is None
    ):
        return False

    price_ok = (
        r["c"]
        > r["ma25"]
    )

    slope_ok = (
        r["ma25"]
        >= r["ma25_prev"]
    )

    return (
        price_ok
        or
        slope_ok
    )


# ============================================================
# 4H Relaxed Volume
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


# ============================================================
# 原版 1D STRICT
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
# 1D Soft
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
# STRICT history
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
            r["i"]
            - last_i
            >= DEDUP_BARS
        ):

            kept.append(r)

            last_i = r["i"]

    return kept


# ============================================================
# 壓力位
# ============================================================

def find_recent_resistance(
    rows,
    current_price,
    lookback
):

    if (
        current_price is None
        or current_price <= 0
    ):
        return None

    candidates = []

    for r in rows[-lookback:]:

        high = r.get("h")

        if (
            high is not None
            and
            high > current_price
        ):

            candidates.append(
                high
            )

    if not candidates:
        return None

    return min(
        candidates
    )


def resistance_distance(
    current_price,
    resistance
):

    if (
        current_price is None
        or resistance is None
        or current_price <= 0
    ):
        return None

    return (
        resistance
        / current_price
        - 1
    ) * 100


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


    # --------------------------------------------------------
    # 5m：只作為接近現價
    # 完全不參與2560訊號
    # --------------------------------------------------------

    r5 = completed_only(
        fetch(
            contract,
            "5m",
            LIMIT_5M
        ),
        FIVE_M,
        now_ts
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
        len(r5) < 1
        or
        len(r1) < 65
        or
        len(r4) < 65
        or
        len(rd) < 65
    ):

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
                "WAIT_HISTORY",

            "bars_5m":
                len(r5),

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


    latest5 = r5[-1]

    latest1 = r1[-1]

    previous1 = r1[-2]

    latest4 = r4[-1]


    latest_d = (
        last_completed_daily_asof(
            rd,
            latest4["close_t"]
        )
    )


    current_price = (
        latest5["c"]
    )


    # ========================================================
    # 1H
    # ========================================================

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


    # ========================================================
    # 4H
    # ========================================================

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


    # ========================================================
    # 1D
    # ========================================================

    day_strict = daily_confirm(
        latest_d
    )

    day_soft = daily_soft_confirm(
        latest_d
    )


    # ========================================================
    # STRICT
    # ========================================================

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


    # ========================================================
    # Volume Ratio
    # ========================================================

    volume_ratio = None

    if (
        latest4.get("vma5")
        is not None

        and

        latest4.get("vma60")
        not in (
            None,
            0
        )
    ):

        volume_ratio = (
            latest4["vma5"]
            /
            latest4["vma60"]
        )


    # ========================================================
    # 4H / 1D 壓力
    # ========================================================

    resistance_4h = (
        find_recent_resistance(
            r4,
            current_price,
            48
        )
    )

    resistance_1d = (
        find_recent_resistance(
            rd,
            current_price,
            60
        )
    )


    resistance_4h_pct = (
        resistance_distance(
            current_price,
            resistance_4h
        )
    )

    resistance_1d_pct = (
        resistance_distance(
            current_price,
            resistance_1d
        )
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


        # 5m 完成K作為接近現價
        "current_price":
            current_price,


        "latest_4h_close":
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


        # 壓力資訊
        "resistance_4h":
            resistance_4h,

        "resistance_4h_pct":
            resistance_4h_pct,

        "resistance_1d":
            resistance_1d,

        "resistance_1d_pct":
            resistance_1d_pct,


        "latest_5m_open_utc":
            iso(
                latest5["t"]
            ),

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


    # 中文標題安全處理
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

            state = json.load(f)


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
# 新週期清除
# ============================================================

def reset_cycle(
    symbol_state
):

    keys = [

        "watch_price",
        "watch_time",

        "trend_ready_price",
        "trend_ready_time",

        "pre_strict_price",
        "pre_strict_time",

        "strict_price",
        "strict_time",

        "current_price",
        "current_time",
    ]


    for key in keys:

        symbol_state.pop(
            key,
            None
        )


# ============================================================
# 階段價格記憶
# ============================================================

def update_stage_memory(
    r,
    symbol_state,
    previous
):

    status = r["status"]

    current_price = r[
        "current_price"
    ]


    symbol_state[
        "current_price"
    ] = current_price

    symbol_state[
        "current_time"
    ] = now_iso()


    active_states = (
        "WATCH",
        "TREND_READY",
        "PRE-STRICT",
        "STRICT",
    )


    # ========================================================
    # 新週期開始
    # ========================================================

    if (
        previous
        in (
            "NO_SIGNAL",
            "UNKNOWN"
        )

        and

        status in active_states
    ):

        reset_cycle(
            symbol_state
        )

        symbol_state[
            "current_price"
        ] = current_price

        symbol_state[
            "current_time"
        ] = now_iso()


    # ========================================================
    # WATCH
    # ========================================================

    if (
        status == "WATCH"

        and

        symbol_state.get(
            "watch_price"
        )
        is None
    ):

        symbol_state[
            "watch_price"
        ] = current_price

        symbol_state[
            "watch_time"
        ] = now_iso()


    # ========================================================
    # TREND_READY
    # ========================================================

    if (
        status == "TREND_READY"

        and

        symbol_state.get(
            "trend_ready_price"
        )
        is None
    ):

        symbol_state[
            "trend_ready_price"
        ] = current_price

        symbol_state[
            "trend_ready_time"
        ] = now_iso()


    # ========================================================
    # PRE-STRICT
    # ========================================================

    if (
        status == "PRE-STRICT"

        and

        symbol_state.get(
            "pre_strict_price"
        )
        is None
    ):

        symbol_state[
            "pre_strict_price"
        ] = current_price

        symbol_state[
            "pre_strict_time"
        ] = now_iso()


    # ========================================================
    # STRICT
    # ========================================================

    if (
        status == "STRICT"

        and

        symbol_state.get(
            "strict_price"
        )
        is None
    ):

        symbol_state[
            "strict_price"
        ] = current_price

        symbol_state[
            "strict_time"
        ] = now_iso()


# ============================================================
# 階段統計
# ============================================================

def stage_stats(
    symbol_state
):

    current = symbol_state.get(
        "current_price"
    )

    watch = symbol_state.get(
        "watch_price"
    )

    trend = symbol_state.get(
        "trend_ready_price"
    )

    pre = symbol_state.get(
        "pre_strict_price"
    )

    strict = symbol_state.get(
        "strict_price"
    )


    return {

        "current":
            current,

        "watch":
            watch,

        "trend":
            trend,

        "pre":
            pre,

        "strict":
            strict,


        "watch_to_now":
            pct_change(
                watch,
                current
            ),

        "trend_to_now":
            pct_change(
                trend,
                current
            ),

        "pre_to_now":
            pct_change(
                pre,
                current
            ),

        "strict_to_now":
            pct_change(
                strict,
                current
            ),


        "watch_to_trend":
            pct_change(
                watch,
                trend
            ),

        "trend_to_pre":
            pct_change(
                trend,
                pre
            ),

        "pre_to_strict":
            pct_change(
                pre,
                strict
            ),

        "watch_to_strict":
            pct_change(
                watch,
                strict
            ),
    }


# ============================================================
# 階段價格文字
# ============================================================

def build_stage_block(
    symbol_state
):

    s = stage_stats(
        symbol_state
    )

    lines = []


    if s["watch"] is not None:

        lines.append(
            "WATCH："
            + price_text(
                s["watch"]
            )
        )


    if s["trend"] is not None:

        lines.append(
            "TREND_READY："
            + price_text(
                s["trend"]
            )
        )


    if s["pre"] is not None:

        lines.append(
            "PRE-STRICT："
            + price_text(
                s["pre"]
            )
        )


    if s["strict"] is not None:

        lines.append(
            "STRICT："
            + price_text(
                s["strict"]
            )
        )


    lines.append(
        "目前："
        + price_text(
            s["current"]
        )
    )


    movement = []


    if s["watch_to_now"] is not None:

        movement.append(
            "WATCH→目前："
            + pct_text(
                s["watch_to_now"]
            )
        )


    if s["trend_to_now"] is not None:

        movement.append(
            "TREND_READY→目前："
            + pct_text(
                s["trend_to_now"]
            )
        )


    if s["pre_to_now"] is not None:

        movement.append(
            "PRE-STRICT→目前："
            + pct_text(
                s["pre_to_now"]
            )
        )


    if s["strict_to_now"] is not None:

        movement.append(
            "STRICT→目前："
            + pct_text(
                s["strict_to_now"]
            )
        )


    if movement:

        lines.append("")

        lines.extend(
            movement
        )


    transitions = []


    if s["watch_to_trend"] is not None:

        transitions.append(
            "WATCH→TREND_READY："
            + pct_text(
                s["watch_to_trend"]
            )
        )


    if s["trend_to_pre"] is not None:

        transitions.append(
            "TREND_READY→PRE-STRICT："
            + pct_text(
                s["trend_to_pre"]
            )
        )


    if s["pre_to_strict"] is not None:

        transitions.append(
            "PRE-STRICT→STRICT："
            + pct_text(
                s["pre_to_strict"]
            )
        )


    if transitions:

        lines.append("")

        lines.extend(
            transitions
        )


    return "\n".join(
        lines
    )


# ============================================================
# 壓力位文字
# ============================================================

def build_resistance_block(r):

    r4_price = r.get(
        "resistance_4h"
    )

    r4_pct = r.get(
        "resistance_4h_pct"
    )

    r1d_price = r.get(
        "resistance_1d"
    )

    r1d_pct = r.get(
        "resistance_1d_pct"
    )


    if r4_price is None:

        r4_line = (
            "最近4H壓力："
            "目前區間內無明顯上方壓力"
        )

    else:

        r4_line = (
            "最近4H壓力："
            f"{price_text(r4_price)} "
            f"({pct_text(r4_pct)})"
        )


    if r1d_price is None:

        d1_line = (
            "最近1D壓力："
            "目前區間內無明顯上方壓力"
        )

    else:

        d1_line = (
            "最近1D壓力："
            f"{price_text(r1d_price)} "
            f"({pct_text(r1d_pct)})"
        )


    return (
        f"{r4_line}\n"
        f"{d1_line}"
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


    # 先更新價格記憶
    update_stage_memory(
        r,
        symbol_state,
        previous
    )


    # 同狀態不重複通知
    if status == previous:

        symbol_state[
            "updated_utc"
        ] = now_iso()

        return


    ratio = r.get(
        "4h_volume_ratio"
    )

    ratio_text = (
        f"{ratio:.2f}"
        if ratio is not None
        else "N/A"
    )


    stage_block = (
        build_stage_block(
            symbol_state
        )
    )


    resistance_block = (
        build_resistance_block(
            r
        )
    )


    # ========================================================
    # STRICT
    # ========================================================

    if status == "STRICT":

        stats = stage_stats(
            symbol_state
        )

        chase = stats.get(
            "pre_to_strict"
        )


        send_ntfy(
            f"2560 STRICT {base}",
            (
                f"{base} 正式 2560 STRICT\n"
                f"Gate：{r['contract']}\n\n"

                f"{stage_block}\n\n"

                f"{resistance_block}\n\n"

                f"PRE→STRICT追價幅度："
                f"{pct_text(chase)}\n\n"

                f"4H close："
                f"{price_text(r['latest_4h_close'])}\n"

                f"4H CORE=True\n"
                f"1D=True\n"

                f"Vol5/Vol60="
                f"{ratio_text}\n\n"

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
                f"{base} 進入 PRE-STRICT\n\n"

                f"{stage_block}\n\n"

                f"{resistance_block}\n\n"

                f"1H=True\n"
                f"4H structure=True\n"
                f"4H relaxed volume=True\n"
                f"1D=True\n"

                f"4H Core 尚未完成\n"

                f"Vol5/Vol60="
                f"{ratio_text}\n\n"

                f"趨勢與量能已成熟，"
                f"等待原版 Strict。"
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
                f"{base} 進入 TREND_READY\n\n"

                f"{stage_block}\n\n"

                f"{resistance_block}\n\n"

                f"1H=True\n"
                f"4H early=True\n"
                f"4H relaxed volume=True\n"
                f"1D soft=True\n"

                f"Vol5/Vol60="
                f"{ratio_text}\n\n"

                f"尚不是 Strict，"
                f"開始人工注意。"
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
                f"{base} 進入 WATCH\n\n"

                f"{stage_block}\n\n"

                f"{resistance_block}\n\n"

                f"1H 多頭已成立\n"

                f"4H / 量能 / 1D "
                f"尚未完全成熟。\n\n"

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
                    f"{base} 2560 候選環境失效\n\n"

                    f"{stage_block}\n\n"

                    f"{resistance_block}\n\n"

                    f"前一狀態："
                    f"{previous}\n\n"

                    f"本輪歷史保留，"
                    f"新週期再重置。"
                ),
                "default",
                "warning"
            )


    symbol_state[
        "status"
    ] = status

    symbol_state[
        "updated_utc"
    ] = now_iso()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "2560 Cloud Monitor | v4.4"
    )

    print(
        now_iso()
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
                    f"5m={r['bars_5m']} "
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

                f"contract="
                f"{contract:<18} "

                f"now="
                f"{price_text(r['current_price'])} "

                f"4Hclose="
                f"{price_text(r['latest_4h_close'])} "

                f"1H="
                f"{r['1h_confirm']} "

                f"4Hearly="
                f"{r['4h_early']} "

                f"4Hstruct="
                f"{r['4h_structure']} "

                f"VolRelax="
                f"{r['4h_volume_relaxed']} "

                f"V5/V60="
                f"{ratio_text} "

                f"4Hcore="
                f"{r['4h_core']} "

                f"1Dsoft="
                f"{r['1d_soft']} "

                f"1D="
                f"{r['1d_confirm']} "

                f"R4H="
                f"{price_text(r['resistance_4h'])} "

                f"R4Hdist="
                f"{pct_text(r['resistance_4h_pct'])} "

                f"R1D="
                f"{price_text(r['resistance_1d'])} "

                f"R1Ddist="
                f"{pct_text(r['resistance_1d_pct'])}"
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
