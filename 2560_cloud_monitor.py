#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor v3.4 — PRE-STRICT Long-Life Grid
===================================================

用途
----
1. 保留原 2560 STRICT 核心：
   - 4H MA25 上升
   - 4H Close > MA25
   - 4H VolMA5 上穿 VolMA60
   - 1D Close > MA25
   - 1D MA25 上升
   - 1D VolMA5 > VolMA60
   - 20 根 4H 去重

2. 增加狀態：
   NO_SIGNAL -> WATCH -> TREND_READY -> PRE-STRICT -> STRICT

3. PRE-STRICT 定義為「第一網可執行候選」：
   - 4H 價格結構已成立
   - 4H 放寬量能成立
   - 1D 至少 soft-confirm
   - 直接計算長壽多網格 A 的：
     下沿 / 上沿 / 格數 / 等比 / 槓桿 / 首筆資金比例 /
     單格估計 / 回撤容忍 / 強平安全要求

4. STRICT：
   - 保留原嚴格訊號定義
   - 若先前已有 PRE-STRICT，判斷是否適合另開第二網 B
   - 不自動下單，只通知

5. 長壽網格原則（v3.4 資產分流）：
   - 生存 > 順趨勢 > 持續成交 > 短期報酬
   - 下沿不能貼現價
   - 依 ATR / 近期支撐動態放寬
   - 強平價需由平台實際畫面確認，並明顯低於下沿

Public market data only. No API key. No order placement.
"""

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.gateio.ws/api/v4"

VALIDATED = ["BTC","ETH","XRP","SOL","BNB"]
EXTENDED = [
    "ADA","LTC","LINK","DOGE","SUI","HYPE",
    "MU","VRT","DELL","NVDA","TSM","BRKB"
]
REQUESTED = VALIDATED + EXTENDED

CRYPTO_BASES = {
    "BTC","ETH","XRP","SOL","BNB",
    "ADA","LTC","LINK","DOGE","SUI","HYPE",
}

US_STOCK_PERP_BASES = {
    "MU","VRT","DELL","NVDA","TSM","BRKB",
}

FOUR_H = 4 * 3600
ONE_D = 24 * 3600

# 長壽網格需要更長的波動/支撐樣本
LIMIT_4H = 600
LIMIT_1D = 220
DEDUP_BARS = 20

# PRE-STRICT 放寬量能
RELAXED_VOL_RATIO = 0.90
RELAXED_VOL_GROWTH = 1.02

# 1D soft confirmation，避免等到最嚴格才看到
DAILY_SOFT_MA25_FLOOR = 0.97

# STRICT 第二網追價限制
STRICT_ADD_MAX_RISE_PCT = 3.0

# 長壽網格 A/B 建議資金比例
PRE_STRICT_GRID_A_PCT = 60
STRICT_GRID_B_PCT = 40

# 長壽網格生存參數
# 加密幣：允許較深回撤，重點是長壽
CRYPTO_GRID_PROFILE = {
    "LOW": {
        "min_lower": 10.0,
        "max_lower": 15.0,
        "min_upper": 12.0,
        "max_upper": 20.0,
        "per_grid": 0.8,
        "leverage": 3,
    },
    "MEDIUM": {
        "min_lower": 15.0,
        "max_lower": 22.0,
        "min_upper": 15.0,
        "max_upper": 26.0,
        "per_grid": 1.0,
        "leverage": 3,
    },
    "HIGH": {
        "min_lower": 20.0,
        "max_lower": 30.0,
        "min_upper": 18.0,
        "max_upper": 35.0,
        "per_grid": 1.2,
        "leverage": 2,
    },
}

# 美股永續：不要把網撒到海溝裡
US_STOCK_GRID_PROFILE = {
    "LOW": {
        "min_lower": 8.0,
        "max_lower": 12.0,
        "min_upper": 10.0,
        "max_upper": 16.0,
        "per_grid": 0.7,
        "leverage": 3,
    },
    "MEDIUM": {
        "min_lower": 12.0,
        "max_lower": 18.0,
        "min_upper": 12.0,
        "max_upper": 20.0,
        "per_grid": 0.8,
        "leverage": 3,
    },
    "HIGH": {
        "min_lower": 15.0,
        "max_lower": 22.0,
        "min_upper": 15.0,
        "max_upper": 24.0,
        "per_grid": 1.0,
        "leverage": 2,
    },
}

# BRKB 額外收斂，避免低波動標的被拉太寬
BRKB_MAX_LOWER_PCT = 15.0
BRKB_MAX_UPPER_PCT = 18.0

# 強平價要求：實際平台強平價至少再低於網格下沿 10%
LIQ_BUFFER_BELOW_LOWER_PCT = 10.0

# 強平相容性代理：
# 真實 Gate 強平價仍受維持保證金率、合約規格、持倉與帳戶模式影響。
# 這裡只用保守代理先篩掉「區間太深卻還用高槓桿」的組合。
LIQ_PROXY_EXTRA_RESERVE_PCT = 5.0
ALLOWED_LEVERAGES = [5, 4, 3, 2]

# 高槓桿額外門檻：允許，但不代表優先。
# 5x/4x 只有在「下沿較淺 + 波動較低 + 安全餘裕足夠」才可用。
MAX_5X_LOWER_DISTANCE_PCT = 10.0
MAX_4X_LOWER_DISTANCE_PCT = 14.0
MAX_5X_ATR_PCT = 1.8
MAX_4X_ATR_PCT = 2.8
MIN_EXTRA_LIQ_HEADROOM_PCT = 2.0

STATE_DIR = Path(".monitor_state")
STATE_FILE = STATE_DIR / "2560_state.json"
RESULT_FILE = Path("2560_latest.json")

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()


# ============================================================
# Basic helpers
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pct_change(a, b):
    if a is None or b is None or a == 0:
        return None
    return (b / a - 1.0) * 100.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def price_text(x):
    if x is None:
        return "N/A"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 100:
        return f"{x:.2f}"
    if abs(x) >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def pct_text(x):
    return "N/A" if x is None else f"{x:+.2f}%"


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ============================================================
# State
# ============================================================

def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"symbols": {}, "last_summary_utc": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"symbols": {}, "last_summary_utc": None}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# Gate API
# ============================================================

def gate_get(path, params=None, retries=5):
    params = params or {}
    qs = urllib.parse.urlencode(params)
    url = BASE + path + (("?" + qs) if qs else "")
    last = None

    for k in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "2560-cloud-monitor/3.4",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(min(2 ** k, 8))

    raise RuntimeError(f"Gate request failed: {last}")


def discover_contracts():
    data = gate_get("/futures/usdt/contracts")
    names = [x.get("name", "") for x in data if x.get("name")]

    mapping = {}
    aliases = {
        "BRKB": ["BRKB", "BRKBG", "BRKBUS", "BRK.B"],
        "TSM": ["TSM", "TSMUS"],
    }

    for base in REQUESTED:
        candidates_base = aliases.get(base, [base])

        found = None
        for alias in candidates_base:
            target = norm(alias + "USDT")
            exact = [n for n in names if norm(n) == target]
            if exact:
                found = exact[0]
                break

        if found is None:
            for alias in candidates_base:
                for n in names:
                    nn = norm(n)
                    if nn.endswith("USDT") and nn[:-4] == norm(alias):
                        found = n
                        break
                if found:
                    break

        mapping[base] = found

    return mapping


def fetch(contract, interval, limit):
    raw = gate_get(
        "/futures/usdt/candlesticks",
        {
            "contract": contract,
            "interval": interval,
            "limit": limit,
        },
    )

    rows = []
    for x in raw:
        rows.append({
            "t": int(float(x["t"])),
            "o": float(x["o"]),
            "h": float(x["h"]),
            "l": float(x["l"]),
            "c": float(x["c"]),
            "v": float(x.get("v", 0)),
        })

    rows.sort(key=lambda z: z["t"])
    return rows


def completed_only(rows, step, cutoff=None):
    if cutoff is None:
        cutoff = int(datetime.now(timezone.utc).timestamp())
    return [r for r in rows if r["t"] + step <= cutoff]


# ============================================================
# Indicators
# ============================================================

def sma(vals, n, i):
    if i < 0 or i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def true_range(rows, i):
    r = rows[i]
    if i == 0:
        return r["h"] - r["l"]
    prev_close = rows[i - 1]["c"]
    return max(
        r["h"] - r["l"],
        abs(r["h"] - prev_close),
        abs(r["l"] - prev_close),
    )


def atr(rows, n, i):
    if i + 1 < n:
        return None
    vals = [true_range(rows, j) for j in range(i - n + 1, i + 1)]
    return sum(vals) / n


def add_ind(rows, step):
    closes = [r["c"] for r in rows]
    vols = [r["v"] for r in rows]

    for i, r in enumerate(rows):
        r["i"] = i
        r["ma25"] = sma(closes, 25, i)
        r["ma25_prev"] = sma(closes, 25, i - 1)
        r["ma60"] = sma(closes, 60, i)
        r["ma60_prev"] = sma(closes, 60, i - 1)
        r["ma120"] = sma(closes, 120, i)
        r["ma120_prev"] = sma(closes, 120, i - 1)
        r["vma5"] = sma(vols, 5, i)
        r["vma60"] = sma(vols, 60, i)
        r["vma5_prev"] = sma(vols, 5, i - 1)
        r["vma60_prev"] = sma(vols, 60, i - 1)
        r["atr14"] = atr(rows, 14, i)
        r["close_t"] = r["t"] + step


# ============================================================
# 2560 signal logic
# ============================================================

def structure_4h_ok(r):
    need = [r.get("ma25"), r.get("ma25_prev")]
    if any(x is None for x in need):
        return False
    return r["ma25"] > r["ma25_prev"] and r["c"] > r["ma25"]


def relaxed_volume_ok(r):
    need = [r.get("vma5"), r.get("vma60"), r.get("vma5_prev")]
    if any(x is None for x in need):
        return False
    return (
        r["vma5"] >= r["vma60"] * RELAXED_VOL_RATIO
        or r["vma5"] > r["vma5_prev"] * RELAXED_VOL_GROWTH
    )


def core_ok(r):
    need = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
        r.get("vma60_prev"),
    ]
    if any(x is None for x in need):
        return False

    return (
        r["ma25"] > r["ma25_prev"]
        and r["c"] > r["ma25"]
        and r["vma5_prev"] <= r["vma60_prev"]
        and r["vma5"] > r["vma60"]
    )


def daily_soft_ok(r):
    if r is None:
        return False
    need = [r.get("ma25"), r.get("ma25_prev")]
    if any(x is None for x in need):
        return False

    return (
        r["c"] >= r["ma25"] * DAILY_SOFT_MA25_FLOOR
        and r["ma25"] >= r["ma25_prev"]
    )


def daily_ok(r):
    if r is None:
        return False

    need = [r.get("ma25"), r.get("ma25_prev"), r.get("vma5"), r.get("vma60")]
    if any(x is None for x in need):
        return False

    return (
        r["c"] > r["ma25"]
        and r["ma25"] > r["ma25_prev"]
        and r["vma5"] > r["vma60"]
    )


def short_history_4h_proxy_ok(r):
    """
    1D 歷史不足 25 根時的保守代理。
    只用既有 4H 歷史，不生成假的日K。
    約以 60/120 根 4H 均線檢查較慢趨勢。
    注意：此代理只允許走到 PRE-STRICT，不允許產生 canonical STRICT。
    """
    need = [
        r.get("ma60"),
        r.get("ma120"),
        r.get("ma120_prev"),
        r.get("vma5"),
        r.get("vma60"),
    ]
    if any(x is None for x in need):
        return False

    return (
        r["c"] > r["ma120"]
        and r["ma60"] > r["ma120"]
        and r["ma120"] >= r["ma120_prev"]
        and r["vma5"] >= r["vma60"] * RELAXED_VOL_RATIO
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
    return core_ok(r4) and daily_ok(d), d


def kept_strict(r4, rd):
    raw = []
    for r in r4:
        ok, _ = strict_raw_at(r, rd)
        if ok:
            raw.append(r)

    kept = []
    last_i = -10**9
    for r in raw:
        if r["i"] - last_i >= DEDUP_BARS:
            kept.append(r)
            last_i = r["i"]

    return kept


def classify_status(latest, d, strict_now, history_mode="FULL_1D"):
    s4 = structure_4h_ok(latest)
    rv = relaxed_volume_ok(latest)

    if history_mode == "FULL_1D":
        dsoft = daily_soft_ok(d)
    elif history_mode == "SHORT_1D_MA25":
        dsoft = daily_soft_ok(d)
    else:
        dsoft = short_history_4h_proxy_ok(latest)

    if strict_now and history_mode == "FULL_1D":
        return "STRICT"

    # PRE-STRICT 可使用短歷史代理，但 STRICT 不可。
    if s4 and rv and dsoft:
        return "PRE-STRICT"

    if s4 and dsoft:
        return "TREND_READY"

    if s4 or dsoft:
        return "WATCH"

    return "NO_SIGNAL"


# ============================================================
# Long-life grid model
# ============================================================

def recent_low(rows, bars):
    subset = rows[-bars:] if len(rows) >= bars else rows
    return min((r["l"] for r in subset), default=None)


def recent_high(rows, bars):
    subset = rows[-bars:] if len(rows) >= bars else rows
    return max((r["h"] for r in subset), default=None)


def effective_support(rows, bars, quantile=0.10):
    """
    用近期低點分布的分位數當「有效支撐」，
    避免單一極端插針把整張網格拖到太深。
    """
    subset = rows[-bars:] if len(rows) >= bars else rows
    lows = sorted(r["l"] for r in subset if r.get("l") is not None)
    if not lows:
        return None
    idx = int((len(lows) - 1) * quantile)
    idx = max(0, min(idx, len(lows) - 1))
    return lows[idx]


def asset_class(base_symbol):
    if base_symbol in US_STOCK_PERP_BASES:
        return "US_STOCK_PERP"
    return "CRYPTO"


def vol_profile(atr_pct):
    if atr_pct is None:
        return "MEDIUM"
    if atr_pct < 2.0:
        return "LOW"
    if atr_pct < 4.0:
        return "MEDIUM"
    return "HIGH"


def required_drop_to_liq_threshold_pct(entry_price, lower):
    """
    從入場價跌到「下沿再低10%」的總跌幅。
    """
    if not entry_price or not lower or entry_price <= 0:
        return None
    threshold = lower * (
        1.0 - LIQ_BUFFER_BELOW_LOWER_PCT / 100.0
    )
    drop = (1.0 - threshold / entry_price) * 100.0
    return max(0.0, drop)


def leverage_proxy_capacity_pct(leverage):
    """
    簡化保守代理：
    理論 1/L 價格跌幅再扣掉額外安全預留。
    不是 Gate 真實強平價公式。
    """
    if leverage <= 0:
        return 0.0
    return max(
        0.0,
        100.0 / leverage - LIQ_PROXY_EXTRA_RESERVE_PCT,
    )


def choose_safe_leverage(entry_price, lower, atr_pct=None, asset_cls="CRYPTO"):
    """
    依長壽網格下沿反推 5x / 4x / 3x / 2x 是否相容。

    原則：
    - 最大允許 5x
    - 5x/4x 只在下沿較淺、ATR較低、強平代理有額外餘裕時使用
    - 高波動或深網格會自動降槓桿
    - 2x 仍不安全則 REJECT
    """
    required = required_drop_to_liq_threshold_pct(
        entry_price,
        lower,
    )

    if required is None:
        return {
            "selected_leverage": None,
            "compatibility_pass": False,
            "required_drop_pct": None,
            "proxy_capacity_pct": None,
            "extra_headroom_pct": None,
            "reject_reason": "缺少入場價或下沿資料",
        }

    lower_distance_pct = abs(
        pct_change(entry_price, lower) or 0.0
    )
    atr_now = atr_pct if atr_pct is not None else 999.0

    for lev in ALLOWED_LEVERAGES:
        capacity = leverage_proxy_capacity_pct(lev)
        headroom = capacity - required

        # 基本強平相容性先通過
        if capacity < required:
            continue

        # 5x 只給淺區間、低波動，而且要有額外安全餘裕
        if lev == 5:
            if lower_distance_pct > MAX_5X_LOWER_DISTANCE_PCT:
                continue
            if atr_now > MAX_5X_ATR_PCT:
                continue
            if headroom < MIN_EXTRA_LIQ_HEADROOM_PCT:
                continue

        # 4x 比 5x 寬一點，但仍不給深網/高波動
        if lev == 4:
            if lower_distance_pct > MAX_4X_LOWER_DISTANCE_PCT:
                continue
            if atr_now > MAX_4X_ATR_PCT:
                continue
            if headroom < MIN_EXTRA_LIQ_HEADROOM_PCT:
                continue

        # 3x / 2x 只看強平相容性，不額外卡 ATR
        return {
            "selected_leverage": lev,
            "compatibility_pass": True,
            "required_drop_pct": required,
            "proxy_capacity_pct": capacity,
            "extra_headroom_pct": headroom,
            "reject_reason": None,
        }

    return {
        "selected_leverage": None,
        "compatibility_pass": False,
        "required_drop_pct": required,
        "proxy_capacity_pct": leverage_proxy_capacity_pct(
            min(ALLOWED_LEVERAGES)
        ),
        "extra_headroom_pct": None,
        "reject_reason": (
            "即使用2x，保守強平相容性代理仍不足；"
            "不建議建立新網格。"
        ),
    }


def longlife_grid_plan(base_symbol, r4, current_price, capital_pct):
    """
    生存優先的趨勢長壽多網格。

    v3.4：
    - Crypto 與 US-stock perpetual 使用不同風險尺
    - 股票不再直接吃 30/60 日絕對最低點
    - 改用近期「有效支撐」(低點分位數) + ATR 安全距離
    - 再套資產類型最大回撤上限
    """
    latest = r4[-1]
    atr14 = latest.get("atr14")
    atr_pct = None
    if atr14 is not None and current_price:
        atr_pct = atr14 / current_price * 100.0

    profile = vol_profile(atr_pct)
    cls = asset_class(base_symbol)

    if cls == "US_STOCK_PERP":
        cfg = dict(US_STOCK_GRID_PROFILE[profile])
        short_bars = 60
        long_bars = 120
        support_q = 0.10
        resistance_bars = 90

        # BRKB 額外縮窄
        if base_symbol == "BRKB":
            cfg["max_lower"] = min(cfg["max_lower"], BRKB_MAX_LOWER_PCT)
            cfg["max_upper"] = min(cfg["max_upper"], BRKB_MAX_UPPER_PCT)
    else:
        cfg = dict(CRYPTO_GRID_PROFILE[profile])
        short_bars = 180
        long_bars = 360
        support_q = 0.05
        resistance_bars = 180

    min_lower = cfg["min_lower"]
    max_lower = cfg["max_lower"]
    min_upper = cfg["min_upper"]
    max_upper = cfg["max_upper"]
    per_grid_target = cfg["per_grid"]

    # ATR 安全距離
    # Crypto 允許較深，股票不讓 ATR 把區間拉到海溝
    atr_mult = 4.0 if cls == "CRYPTO" else 3.0
    atr_lower = (atr_pct or 0.0) * atr_mult

    target_lower_distance = clamp(
        max(min_lower, atr_lower),
        min_lower,
        max_lower,
    )

    support_short = effective_support(
        r4,
        short_bars,
        support_q,
    )
    support_long = effective_support(
        r4,
        long_bars,
        support_q,
    )

    pct_floor = current_price * (
        1.0 - target_lower_distance / 100.0
    )

    support_candidates = [
        x for x in (support_short, support_long)
        if x is not None and x < current_price
    ]

    if support_candidates:
        # 有效支撐採較保守者，但不准突破資產類型最大下沿
        support_floor = min(support_candidates) * 0.995
        lower = min(pct_floor, support_floor)
    else:
        lower = pct_floor

    # 資產類型硬上限：股票不再允許一律 -30%
    absolute_lower_cap = current_price * (
        1.0 - max_lower / 100.0
    )
    lower = max(lower, absolute_lower_cap)

    actual_lower_pct = abs(
        pct_change(current_price, lower) or 0.0
    )

    # 區間先決定，再由下沿反推可用槓桿。
    # 避免「下沿很深但仍固定3x」與強平安全目標互相衝突。
    liq_check = choose_safe_leverage(
        current_price,
        lower,
        atr_pct=atr_pct,
        asset_cls=cls,
    )
    leverage = liq_check.get("selected_leverage")

    # 上沿：近期壓力 + 趨勢延伸
    resistance = recent_high(r4, resistance_bars)

    atr_upper_mult = 3.0 if cls == "CRYPTO" else 2.5
    atr_upper = (atr_pct or 0.0) * atr_upper_mult

    upper_distance = clamp(
        max(min_upper, atr_upper),
        min_upper,
        max_upper,
    )

    pct_ceiling = current_price * (
        1.0 + upper_distance / 100.0
    )

    if resistance is not None and resistance > current_price:
        upper = max(
            pct_ceiling,
            resistance * 1.01,
        )
    else:
        upper = pct_ceiling

    absolute_upper_cap = current_price * (
        1.0 + max_upper / 100.0
    )
    upper = min(upper, absolute_upper_cap)

    actual_upper_pct = pct_change(
        current_price,
        upper,
    )

    # 等比格數
    if lower > 0 and upper > lower:
        raw_grids = round(
            math.log(upper / lower)
            / math.log(
                1.0 + per_grid_target / 100.0
            )
        )
    else:
        raw_grids = 24

    # 股票區間較窄，避免動不動 40 格
    max_grids = 32 if cls == "US_STOCK_PERP" else 40
    grids = int(
        clamp(
            raw_grids,
            18,
            max_grids,
        )
    )

    geometric_grid_pct = (
        (upper / lower) ** (1.0 / grids)
        - 1.0
    ) * 100.0

    required_liq_below = lower * (
        1.0 - LIQ_BUFFER_BELOW_LOWER_PCT / 100.0
    )

    flash20_price = current_price * 0.80
    flash20_inside = lower <= flash20_price

    range_pass = (
        actual_lower_pct >= min_lower
        and actual_lower_pct <= max_lower + 0.01
    )

    # v3.4：生存 PASS 必須同時通過「區間合理」與「槓桿相容」。
    survival_pass = (
        range_pass
        and liq_check.get("compatibility_pass", False)
    )

    return {
        "asset_class": cls,
        "direction": "LONG_GRID",
        "entry_price": current_price,
        "lower": lower,
        "upper": upper,
        "lower_distance_pct": pct_change(
            current_price,
            lower,
        ),
        "upper_distance_pct": actual_upper_pct,
        "grid_count": grids,
        "grid_mode": "GEOMETRIC",
        "estimated_gross_per_grid_pct": geometric_grid_pct,
        "suggested_leverage": leverage,
        "leverage_compatibility_pass": liq_check.get("compatibility_pass"),
        "required_drop_to_liq_threshold_pct": liq_check.get("required_drop_pct"),
        "leverage_proxy_capacity_pct": liq_check.get("proxy_capacity_pct"),
        "leverage_extra_headroom_pct": liq_check.get("extra_headroom_pct"),
        "grid_reject_reason": liq_check.get("reject_reason"),
        "capital_pct": capital_pct,
        "atr14": atr14,
        "atr_pct": atr_pct,
        "vol_profile": profile,
        "support_short": support_short,
        "support_long": support_long,
        "support_short_bars": short_bars,
        "support_long_bars": long_bars,
        "resistance": resistance,
        "max_lower_pct": max_lower,
        "max_upper_pct": max_upper,
        "flash20_inside_grid": flash20_inside,
        "required_liquidation_below": required_liq_below,
        "liquidation_rule": (
            f"平台實際強平價需 <= "
            f"{price_text(required_liq_below)} "
            f"(至少低於下沿 "
            f"{LIQ_BUFFER_BELOW_LOWER_PCT:.0f}%)；"
            f"程式槓桿判斷僅為保守代理，最終以Gate建單畫面為準。"
        ),
        "survival_pass": survival_pass,
        "leverage_policy": (
            "最大5x；5x/4x僅在淺下沿、低ATR、且強平代理有額外餘裕時使用；"
            "高波動/深網格自動降至3x或2x。"
        ),
    }


# ============================================================
# Analysis
# ============================================================

def analyze(base_symbol, contract):
    now = int(datetime.now(timezone.utc).timestamp())

    r4 = completed_only(
        fetch(contract, "4h", LIMIT_4H),
        FOUR_H,
        now,
    )
    rd = completed_only(
        fetch(contract, "1d", LIMIT_1D),
        ONE_D,
        now,
    )

    # 4H 至少要能算 MA120；不足才是真正 WAIT_HISTORY。
    if len(r4) < 121:
        return {
            "base": base_symbol,
            "contract": contract,
            "group": (
                "VALIDATED"
                if base_symbol in VALIDATED
                else "EXTENDED"
            ),
            "status": "WAIT_HISTORY",
            "history_mode": "INSUFFICIENT_4H",
            "history_4h_bars": len(r4),
            "history_1d_bars": len(rd),
        }

    add_ind(r4, FOUR_H)

    # 1D >= 61：完整 canonical 2560 日線確認。
    # 25~60：可做 MA25 soft 判斷，但 STRICT 暫不開放。
    # <25：使用 4H MA60/MA120 保守代理，只允許到 PRE-STRICT。
    if len(rd) >= 61:
        add_ind(rd, ONE_D)
        history_mode = "FULL_1D"
    elif len(rd) >= 25:
        add_ind(rd, ONE_D)
        history_mode = "SHORT_1D_MA25"
    else:
        history_mode = "4H_PROXY"

    latest = r4[-1]

    d = None
    raw_now = False
    strict_now = False

    if history_mode in ("FULL_1D", "SHORT_1D_MA25"):
        d = last_completed_daily_asof(rd, latest["close_t"])

    # 只有完整日線歷史才允許 canonical STRICT
    if history_mode == "FULL_1D":
        raw_now, d = strict_raw_at(latest, rd)
        kept = kept_strict(r4, rd)
        strict_now = bool(
            kept
            and kept[-1]["t"] == latest["t"]
        )

    status = classify_status(
        latest,
        d,
        strict_now,
        history_mode,
    )

    grid = None
    if status == "PRE-STRICT":
        grid = longlife_grid_plan(
            base_symbol,
            r4,
            latest["c"],
            PRE_STRICT_GRID_A_PCT,
        )
    elif status == "STRICT":
        grid = longlife_grid_plan(
            base_symbol,
            r4,
            latest["c"],
            STRICT_GRID_B_PCT,
        )

    if history_mode == "FULL_1D":
        dsoft = daily_soft_ok(d)
        dconfirm = daily_ok(d)
    elif history_mode == "SHORT_1D_MA25":
        dsoft = daily_soft_ok(d)
        dconfirm = False
    else:
        dsoft = short_history_4h_proxy_ok(latest)
        dconfirm = False

    return {
        "base": base_symbol,
        "contract": contract,
        "group": (
            "VALIDATED"
            if base_symbol in VALIDATED
            else "EXTENDED"
        ),
        "status": status,
        "history_mode": history_mode,
        "history_4h_bars": len(r4),
        "history_1d_bars": len(rd),
        "strict_allowed": history_mode == "FULL_1D",
        "latest_4h_open_utc": iso(latest["t"]),
        "latest_4h_close_utc": iso(latest["close_t"]),
        "latest_close": latest["c"],
        "4h_structure": structure_4h_ok(latest),
        "4h_relaxed_volume": relaxed_volume_ok(latest),
        "4h_core": core_ok(latest),
        "1d_soft": dsoft,
        "1d_confirm": dconfirm,
        "strict_raw": raw_now,
        "strict": strict_now,
        "grid": grid,
    }


# ============================================================
# Notifications
# ============================================================

def send_ntfy(title, msg, priority="default", tags="bar_chart"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; notification skipped.")
        return False

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        print("ntfy:", resp.status, title)
        return 200 <= resp.status < 300


def format_grid(plan):
    if not plan:
        return "網格：N/A"

    flash = (
        "可留在網內"
        if plan.get("flash20_inside_grid")
        else "可能跌破下沿"
    )

    survival = (
        "PASS"
        if plan.get("survival_pass")
        else "REJECT"
    )

    lev = plan.get("suggested_leverage")
    lev_text = f"{lev}x" if lev else "不開"

    reject_reason = plan.get("grid_reject_reason")
    decision = (
        "可建立候選網格，建單時再核對Gate實際強平價。"
        if plan.get("survival_pass")
        else f"不建議開網：{reject_reason or '生存條件未通過'}"
    )

    return (
        f"資產類型：{plan.get('asset_class')}\n"
        f"方向：多網格\n"
        f"建議進場參考：{price_text(plan.get('entry_price'))}\n"
        f"下沿：{price_text(plan.get('lower'))} "
        f"({pct_text(plan.get('lower_distance_pct'))})\n"
        f"上沿：{price_text(plan.get('upper'))} "
        f"({pct_text(plan.get('upper_distance_pct'))})\n"
        f"格數：{plan.get('grid_count')}\n"
        f"模式：等比\n"
        f"預估單格毛幅：約 {plan.get('estimated_gross_per_grid_pct', 0):.2f}%\n"
        f"建議槓桿：{lev_text}\n"
        f"建議投入：總預算 {plan.get('capital_pct')}%\n"
        f"強平安全所需總跌幅：{pct_text(-plan.get('required_drop_to_liq_threshold_pct')) if plan.get('required_drop_to_liq_threshold_pct') is not None else 'N/A'}\n"
        f"槓桿代理可承受：{pct_text(-plan.get('leverage_proxy_capacity_pct')) if plan.get('leverage_proxy_capacity_pct') is not None else 'N/A'}\n"
        f"額外安全餘裕：{pct_text(plan.get('leverage_extra_headroom_pct'))}\n"
        f"4H ATR：{pct_text(plan.get('atr_pct'))}\n"
        f"波動級別：{plan.get('vol_profile')}\n"
        f"近期有效支撐：{price_text(plan.get('support_short'))}\n"
        f"較長有效支撐：{price_text(plan.get('support_long'))}\n"
        f"本類型下沿上限：-{plan.get('max_lower_pct'):.1f}%\n"
        f"20%快速回撤：{flash}\n"
        f"強平安全要求：{plan.get('liquidation_rule')}\n"
        f"生存檢查：{survival}\n"
        f"網格決策：{decision}\n"
        f"槓桿政策：{plan.get('leverage_policy')}"
    )


def notify_pre_strict(r):
    g = r.get("grid")
    msg = (
        f"{r['base']} 2560 PRE-STRICT\n\n"
        f"定位：第一段可開網候選\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"4H結構：{r.get('4h_structure')}\n"
        f"4H放寬量能：{r.get('4h_relaxed_volume')}\n"
        f"1D soft：{r.get('1d_soft')}\n"
        f"歷史模式：{r.get('history_mode')} "
        f"(4H={r.get('history_4h_bars')} / 1D={r.get('history_1d_bars')})\n"
        f"STRICT可用：{r.get('strict_allowed')}\n\n"
        f"【長壽網格 A】\n"
        f"{format_grid(g)}\n\n"
        f"{'⚠ 短歷史標的：可作 PRE-STRICT 第一網候選，但在 1D 歷史滿 61 根前不產生 STRICT。\\n' if not r.get('strict_allowed') else ''}"
        f"原則：寧可寬一點、少成交幾格，也不要下沿太貼現價。"
    )
    send_ntfy(
        f"2560 PRE-STRICT {r['base']}",
        msg,
        "high",
        "chart_with_upwards_trend,bell",
    )


def notify_strict(r, symbol_state):
    g = r.get("grid")
    pre_price = symbol_state.get("pre_strict_entry_price")
    rise = pct_change(pre_price, r.get("latest_close"))

    if pre_price is None:
        add_decision = "沒有記錄到前一個 PRE-STRICT；第二網需人工重新評估。"
    elif rise is not None and rise <= STRICT_ADD_MAX_RISE_PCT:
        add_decision = (
            f"相對 PRE-STRICT 價格 {pct_text(rise)}，"
            f"未超過 +{STRICT_ADD_MAX_RISE_PCT:.1f}%；"
            f"可評估另開第二網 B。"
        )
    else:
        add_decision = (
            f"相對 PRE-STRICT 已上漲 {pct_text(rise)}；"
            f"不追高，不開第二網 B。原網 A 繼續運行。"
        )

    msg = (
        f"{r['base']} 2560 STRICT\n\n"
        f"定位：趨勢確認 / 第二網審核點\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"PRE-STRICT價：{price_text(pre_price)}\n"
        f"漲幅：{pct_text(rise)}\n\n"
        f"第二網判斷：{add_decision}\n\n"
        f"【若允許開網 B，重新依當下行情計算】\n"
        f"{format_grid(g)}"
    )
    send_ntfy(
        f"2560 STRICT {r['base']}",
        msg,
        "high",
        "chart_with_upwards_trend,bell",
    )


def notify_status_change(r, state):
    symbols = state.setdefault("symbols", {})
    st = symbols.setdefault(r["base"], {})
    old_status = st.get("status")
    new_status = r["status"]

    # PRE-STRICT 第一次進入時，鎖第一網參考價
    if new_status == "PRE-STRICT" and old_status != "PRE-STRICT":
        st["pre_strict_entry_price"] = r.get("latest_close")
        st["pre_strict_time_utc"] = now_iso()
        st["pre_strict_grid"] = r.get("grid")
        notify_pre_strict(r)

    # STRICT 狀態變化時才通知，避免洗版
    if new_status == "STRICT" and old_status != "STRICT":
        notify_strict(r, st)

    st["status"] = new_status
    st["last_price"] = r.get("latest_close")
    st["updated_utc"] = now_iso()


# ============================================================
# Main
# ============================================================

def main():
    print("2560 Cloud Monitor | PRE-STRICT Long-Life Grid v3.4")
    print("UTC:", now_iso())
    print(
        "Rule: NO_SIGNAL -> WATCH -> TREND_READY -> "
        "PRE-STRICT -> STRICT"
    )
    print(
        "PRE-STRICT = first long-life grid candidate | "
        "STRICT = confirmation / second-grid review"
    )

    state = load_state()
    contract_map = discover_contracts()

    print("\nCONTRACT MAP")
    for base in REQUESTED:
        print(f"{base:<5} -> {contract_map.get(base)}")

    results = []
    errors = []

    print("\nSCAN")
    for base in REQUESTED:
        contract = contract_map.get(base)

        if not contract:
            print(f"{base:<5} NOT_FOUND")
            results.append({
                "base": base,
                "status": "NOT_FOUND",
            })
            continue

        try:
            r = analyze(base, contract)
            results.append(r)

            if r.get("status") == "WAIT_HISTORY":
                print(
                    f"{base:<5} WAIT_HISTORY "
                    f"4H={r.get('history_4h_bars')} "
                    f"1D={r.get('history_1d_bars')}"
                )
                notify_status_change(r, state)
                time.sleep(0.15)
                continue

            g = r.get("grid") or {}
            grid_text = ""
            if r["status"] in ("PRE-STRICT", "STRICT"):
                grid_text = (
                    f" grid={price_text(g.get('lower'))}"
                    f"~{price_text(g.get('upper'))}"
                    f" n={g.get('grid_count')}"
                    f" lev={str(g.get('suggested_leverage')) + 'x' if g.get('suggested_leverage') else 'REJECT'}"
                    f" liqCompat={g.get('leverage_compatibility_pass')}"
                    f" atr={pct_text(g.get('atr_pct'))}"
                    f" headroom={pct_text(g.get('leverage_extra_headroom_pct'))}"
                    f" survive={g.get('survival_pass')}"
                )

            print(
                f"{base:<5} "
                f"{r['status']:<12} "
                f"close={price_text(r['latest_close'])} "
                f"4Hstruct={r['4h_structure']} "
                f"relVol={r['4h_relaxed_volume']} "
                f"4Hcore={r['4h_core']} "
                f"1Dsoft={r['1d_soft']} "
                f"1D={r['1d_confirm']} "
                f"hist={r.get('history_mode')} "
                f"bars4H={r.get('history_4h_bars')} "
                f"bars1D={r.get('history_1d_bars')}"
                f"{grid_text}"
            )

            notify_status_change(r, state)

        except Exception as e:
            errors.append((base, str(e)))
            print(f"{base:<5} ERROR {e}")
            results.append({
                "base": base,
                "contract": contract,
                "status": "ERROR",
                "error": str(e),
            })

        time.sleep(0.15)

    RESULT_FILE.write_text(
        json.dumps(
            {
                "generated_utc": now_iso(),
                "rule_version": "2560_PRESTRICT_LONG_LIFE_GRID_V3_4",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    save_state(state)

    counts = Counter(r.get("status") for r in results)
    print("\nSTATUS COUNTS:", dict(counts))
    print("SYMBOL COUNT:", len(results))
    print("ERROR COUNT:", len(errors))
    print("STATE FILE:", STATE_FILE)
    print("RESULT FILE:", RESULT_FILE)


if __name__ == "__main__":
    main()
