#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor v3.0 — PRE-STRICT Long-Life Grid
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

5. 長壽網格原則：
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
MIN_LOWER_LOW_VOL_PCT = 10.0
MIN_LOWER_MED_VOL_PCT = 15.0
MIN_LOWER_HIGH_VOL_PCT = 20.0
MAX_LOWER_DISTANCE_PCT = 30.0

MIN_UPPER_LOW_VOL_PCT = 12.0
MIN_UPPER_MED_VOL_PCT = 15.0
MIN_UPPER_HIGH_VOL_PCT = 18.0
MAX_UPPER_DISTANCE_PCT = 35.0

# 強平價要求：實際平台強平價至少再低於網格下沿 10%
LIQ_BUFFER_BELOW_LOWER_PCT = 10.0

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
                    "User-Agent": "2560-cloud-monitor/3.0",
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


def classify_status(latest, d, strict_now):
    s4 = structure_4h_ok(latest)
    rv = relaxed_volume_ok(latest)
    dsoft = daily_soft_ok(d)
    dstrict = daily_ok(d)

    if strict_now:
        return "STRICT"

    # PRE-STRICT: 價格結構 + 放寬量能 + 日線至少 soft
    if s4 and rv and dsoft:
        return "PRE-STRICT"

    # TREND_READY: 4H 結構 + 日線 soft，但量能還沒跟上
    if s4 and dsoft:
        return "TREND_READY"

    # WATCH: 4H 或日線已有一邊轉好
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


def vol_profile(atr_pct):
    if atr_pct is None:
        return "MEDIUM"
    if atr_pct < 2.0:
        return "LOW"
    if atr_pct < 4.0:
        return "MEDIUM"
    return "HIGH"


def longlife_grid_plan(r4, current_price, capital_pct):
    """
    生存優先的趨勢長壽多網格。
    不把下沿硬貼在現價附近。
    """
    latest = r4[-1]
    atr14 = latest.get("atr14")
    atr_pct = None
    if atr14 is not None and current_price:
        atr_pct = atr14 / current_price * 100.0

    profile = vol_profile(atr_pct)

    if profile == "LOW":
        min_lower = MIN_LOWER_LOW_VOL_PCT
        min_upper = MIN_UPPER_LOW_VOL_PCT
        per_grid_target = 0.8
        leverage = 3
    elif profile == "MEDIUM":
        min_lower = MIN_LOWER_MED_VOL_PCT
        min_upper = MIN_UPPER_MED_VOL_PCT
        per_grid_target = 1.0
        leverage = 3
    else:
        min_lower = MIN_LOWER_HIGH_VOL_PCT
        min_upper = MIN_UPPER_HIGH_VOL_PCT
        per_grid_target = 1.2
        leverage = 2

    # ATR 越大，下沿越深。4 x 4H ATR 作基本壓力緩衝。
    atr_lower = (atr_pct or 0.0) * 4.0
    lower_distance = clamp(
        max(min_lower, atr_lower),
        min_lower,
        MAX_LOWER_DISTANCE_PCT,
    )

    # 30天約 180 根 4H；60天約 360 根。
    support_30d = recent_low(r4, 180)
    support_60d = recent_low(r4, 360)

    pct_floor = current_price * (1.0 - lower_distance / 100.0)

    support_candidates = [x for x in [support_30d, support_60d] if x is not None]
    support_floor = min(support_candidates) if support_candidates else pct_floor

    # 支撐若更低，採更保守者；但避免無限拉寬，最多約 -30%
    absolute_lower_cap = current_price * (1.0 - MAX_LOWER_DISTANCE_PCT / 100.0)
    lower = min(pct_floor, support_floor * 0.995)
    lower = max(lower, absolute_lower_cap)

    actual_lower_pct = abs(pct_change(current_price, lower) or 0.0)

    # 上沿：近期壓力 + 趨勢延伸空間
    resistance_30d = recent_high(r4, 180)
    atr_upper = (atr_pct or 0.0) * 3.0
    upper_distance = clamp(
        max(min_upper, atr_upper),
        min_upper,
        MAX_UPPER_DISTANCE_PCT,
    )
    pct_ceiling = current_price * (1.0 + upper_distance / 100.0)

    if resistance_30d is not None:
        upper = max(pct_ceiling, resistance_30d * 1.01)
    else:
        upper = pct_ceiling

    absolute_upper_cap = current_price * (1.0 + MAX_UPPER_DISTANCE_PCT / 100.0)
    upper = min(upper, absolute_upper_cap)
    actual_upper_pct = pct_change(current_price, upper)

    # 等比格數，以目標單格毛幅估算
    if lower > 0 and upper > lower:
        raw_grids = round(
            math.log(upper / lower)
            / math.log(1.0 + per_grid_target / 100.0)
        )
    else:
        raw_grids = 24

    grids = int(clamp(raw_grids, 18, 40))
    geometric_grid_pct = ((upper / lower) ** (1.0 / grids) - 1.0) * 100.0

    # 強平不是只靠K線能精確推算，所以給「平台實際強平價必須低於」的硬要求
    required_liq_below = lower * (1.0 - LIQ_BUFFER_BELOW_LOWER_PCT / 100.0)

    # 20%級快速回撤能不能仍在區間內
    flash20_price = current_price * 0.80
    flash20_inside = lower <= flash20_price

    survival_pass = (
        actual_lower_pct >= min_lower
        and actual_lower_pct >= 10.0
    )

    return {
        "direction": "LONG_GRID",
        "entry_price": current_price,
        "lower": lower,
        "upper": upper,
        "lower_distance_pct": pct_change(current_price, lower),
        "upper_distance_pct": actual_upper_pct,
        "grid_count": grids,
        "grid_mode": "GEOMETRIC",
        "estimated_gross_per_grid_pct": geometric_grid_pct,
        "suggested_leverage": leverage,
        "capital_pct": capital_pct,
        "atr14": atr14,
        "atr_pct": atr_pct,
        "vol_profile": profile,
        "support_30d": support_30d,
        "support_60d": support_60d,
        "resistance_30d": resistance_30d,
        "flash20_inside_grid": flash20_inside,
        "required_liquidation_below": required_liq_below,
        "liquidation_rule": (
            f"平台實際強平價需 <= {price_text(required_liq_below)} "
            f"(至少低於下沿 {LIQ_BUFFER_BELOW_LOWER_PCT:.0f}%)"
        ),
        "survival_pass": survival_pass,
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

    if len(r4) < 65 or len(rd) < 65:
        raise RuntimeError(
            f"insufficient candles: 4h={len(r4)} 1d={len(rd)}"
        )

    add_ind(r4, FOUR_H)
    add_ind(rd, ONE_D)

    latest = r4[-1]
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
    )

    grid = None
    if status == "PRE-STRICT":
        grid = longlife_grid_plan(
            r4,
            latest["c"],
            PRE_STRICT_GRID_A_PCT,
        )
    elif status == "STRICT":
        grid = longlife_grid_plan(
            r4,
            latest["c"],
            STRICT_GRID_B_PCT,
        )

    return {
        "base": base_symbol,
        "contract": contract,
        "group": (
            "VALIDATED"
            if base_symbol in VALIDATED
            else "EXTENDED"
        ),
        "status": status,
        "latest_4h_open_utc": iso(latest["t"]),
        "latest_4h_close_utc": iso(latest["close_t"]),
        "latest_close": latest["c"],
        "4h_structure": structure_4h_ok(latest),
        "4h_relaxed_volume": relaxed_volume_ok(latest),
        "4h_core": core_ok(latest),
        "1d_soft": daily_soft_ok(d),
        "1d_confirm": daily_ok(d),
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
        else "CHECK"
    )

    return (
        f"方向：多網格\n"
        f"建議進場參考：{price_text(plan.get('entry_price'))}\n"
        f"下沿：{price_text(plan.get('lower'))} "
        f"({pct_text(plan.get('lower_distance_pct'))})\n"
        f"上沿：{price_text(plan.get('upper'))} "
        f"({pct_text(plan.get('upper_distance_pct'))})\n"
        f"格數：{plan.get('grid_count')}\n"
        f"模式：等比\n"
        f"預估單格毛幅：約 {plan.get('estimated_gross_per_grid_pct', 0):.2f}%\n"
        f"建議槓桿：{plan.get('suggested_leverage')}x\n"
        f"建議投入：總預算 {plan.get('capital_pct')}%\n"
        f"4H ATR：{pct_text(plan.get('atr_pct'))}\n"
        f"波動級別：{plan.get('vol_profile')}\n"
        f"20%快速回撤：{flash}\n"
        f"強平安全要求：{plan.get('liquidation_rule')}\n"
        f"生存檢查：{survival}"
    )


def notify_pre_strict(r):
    g = r.get("grid")
    msg = (
        f"{r['base']} 2560 PRE-STRICT\n\n"
        f"定位：第一段可開網候選\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"4H結構：{r.get('4h_structure')}\n"
        f"4H放寬量能：{r.get('4h_relaxed_volume')}\n"
        f"1D soft：{r.get('1d_soft')}\n\n"
        f"【長壽網格 A】\n"
        f"{format_grid(g)}\n\n"
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
    print("2560 Cloud Monitor | PRE-STRICT Long-Life Grid v3.0")
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

            g = r.get("grid") or {}
            grid_text = ""
            if r["status"] in ("PRE-STRICT", "STRICT"):
                grid_text = (
                    f" grid={price_text(g.get('lower'))}"
                    f"~{price_text(g.get('upper'))}"
                    f" n={g.get('grid_count')}"
                    f" lev={g.get('suggested_leverage')}x"
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
                f"1D={r['1d_confirm']}"
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
                "rule_version": "2560_PRESTRICT_LONG_LIFE_GRID_V3",
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
