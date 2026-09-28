#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2560 Cloud Monitor TARGET-FIRST FINAL 2026-09-28 — Signal Detail Output
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

6. Gate 港股 2560（R2）：
   - 只接受 Gate /stock/symbols?exchange=hk 股票白名單
   - 日K = 戰略方向
   - 1H = 波段結構
   - 15m = 進場窗口
   - 產生合理目標區
   - 不把港股現股偽裝成 USDT/PERP/FUTURES，也不自動建立槓桿網格

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from email.header import Header
from zoneinfo import ZoneInfo

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

# 港股核心追蹤股：永遠保留，不受固定池排名淘汰。
# 固定港股池會另外把 Gate 股票區所有 HK common stocks 納入第一階段日K篩選。
HK_FIXED_CODES = {
    # 科技 / 平台 / 半導體
    "0700": "騰訊控股",
    "1810": "小米集團-W",
    "3690": "美團-W",
    "1024": "快手-W",
    "0981": "中芯國際",
    "3750": "寧德時代",
    "9988": "阿里巴巴-W",

    # 金融 / 交易所 / 地產
    "0388": "香港交易所",
    "2318": "中國平安",
    "0005": "滙豐控股",
    "1398": "工商銀行",
    "3988": "中國銀行",
    "0016": "新鴻基地產",

    # 電信 / 能源
    "0941": "中國移動",
    "0762": "中國聯通",
    "0883": "中國海洋石油",

    # 汽車 / 製造 / 基礎設施
    "1211": "比亞迪股份",
    "0175": "吉利汽車",
    "6869": "長飛光纖光纜",
    "2899": "紫金礦業",

    # 醫療 / 消費
    "6618": "京東健康",
    "1093": "石藥集團",
    "0291": "華潤啤酒",
    "9633": "農夫山泉",

    # 旅遊 / 娛樂
    "0027": "銀河娛樂",
}

# 相容既有函式命名
HK_CORE_CODES = HK_FIXED_CODES

# 港股採固定池，不做每輪全市場海選。
# 每輪只用 Gate 股票清單驗證這 25 檔是否仍可交易，再跑完整 1D + 1H + 15m。
HK_FIXED_POOL_SIZE = len(HK_FIXED_CODES)

# 港股 2560 智慧掃描：
# - 盤中：每個新的 15m 時間桶只掃一次
# - 午休 / 收盤後 / 凌晨：不重複掃描
# - 16:10~16:40：每天強制做一次完整收盤確認
HK_TZ = ZoneInfo("Asia/Hong_Kong")
HK_AM_START = dt_time(9, 30)
HK_AM_END = dt_time(12, 0)
HK_PM_START = dt_time(13, 0)
HK_PM_END = dt_time(16, 0)
HK_CLOSE_CONFIRM_START = dt_time(16, 10)
HK_CLOSE_CONFIRM_END = dt_time(16, 40)


def hk_now():
    return datetime.now(timezone.utc).astimezone(HK_TZ)


def hk_intraday_bucket(now_hk=None):
    """回傳目前所屬 15m bucket，例如 2026-09-28 10:45。非交易時段回 None。"""
    now_hk = now_hk or hk_now()

    if now_hk.weekday() >= 5:
        return None

    t = now_hk.time().replace(tzinfo=None)
    in_session = (
        HK_AM_START <= t < HK_AM_END
        or HK_PM_START <= t < HK_PM_END
    )
    if not in_session:
        return None

    minute = (now_hk.minute // 15) * 15
    bucket = now_hk.replace(minute=minute, second=0, microsecond=0)
    return bucket.strftime("%Y-%m-%d %H:%M")


def hk_scan_decision(state, now_hk=None):
    """
    決定本輪是否需要重新掃港股。
    回傳 (should_scan, reason, token)
    reason:
      INTRADAY_NEW_15M
      CLOSE_CONFIRM
      SKIP_SAME_15M
      SKIP_LUNCH
      SKIP_OFF_HOURS
      SKIP_WEEKEND
      SKIP_WAIT_CLOSE_CONFIRM
    """
    now_hk = now_hk or hk_now()
    meta = state.setdefault("hk_scan_meta", {})
    date_key = now_hk.date().isoformat()

    if now_hk.weekday() >= 5:
        return False, "SKIP_WEEKEND", None

    t = now_hk.time().replace(tzinfo=None)

    bucket = hk_intraday_bucket(now_hk)
    if bucket is not None:
        if meta.get("last_intraday_bucket") == bucket:
            return False, "SKIP_SAME_15M", bucket
        return True, "INTRADAY_NEW_15M", bucket

    if HK_CLOSE_CONFIRM_START <= t < HK_CLOSE_CONFIRM_END:
        if meta.get("last_close_confirm_date") == date_key:
            return False, "SKIP_CLOSE_ALREADY_DONE", date_key
        return True, "CLOSE_CONFIRM", date_key

    if HK_AM_END <= t < HK_PM_START:
        return False, "SKIP_LUNCH", None

    if HK_PM_END <= t < HK_CLOSE_CONFIRM_START:
        return False, "SKIP_WAIT_CLOSE_CONFIRM", None

    return False, "SKIP_OFF_HOURS", None


def mark_hk_scan_done(state, reason, token):
    meta = state.setdefault("hk_scan_meta", {})
    if reason == "INTRADAY_NEW_15M":
        meta["last_intraday_bucket"] = token
    elif reason == "CLOSE_CONFIRM":
        meta["last_close_confirm_date"] = token
    elif reason == "BOOTSTRAP_EMPTY_CACHE":
        meta["last_bootstrap_date"] = token
    meta["last_scan_reason"] = reason
    meta["last_scan_utc"] = now_iso()


def load_previous_hk_results(state):
    """
    非掃描時段沿用上一輪港股結果。
    GitHub Actions 每輪是新 runner，所以不能只依賴 2560_latest.json；
    優先從會被 workflow 保存/還原的 state 讀取。
    """
    cached = state.get("hk_last_results")
    if isinstance(cached, list) and cached:
        return cached

    # 本機或同一 runner 的 fallback
    if not RESULT_FILE.exists():
        return []

    try:
        payload = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        return [
            r for r in payload.get("results", [])
            if r.get("group") == "HK_STOCK"
        ]
    except Exception as e:
        print("HK PREVIOUS RESULT WARN:", e)
        return []

ONE_H = 3600
FIFTEEN_M = 15 * 60
FOUR_H = 4 * 3600
ONE_D = 24 * 3600

# 長壽網格需要更長的波動/支撐樣本
LIMIT_1H = 240
LIMIT_15M = 240
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
                    "User-Agent": "2560-cloud-monitor/final-r2",
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


def hk_code(raw_symbol):
    """把 Gate 港股 symbol 正規化成 Yahoo 常用代碼。"""
    digits = re.sub(r"\D", "", str(raw_symbol or ""))
    if not digits:
        return None
    stripped = digits.lstrip("0") or "0"
    # 00700 / 700 -> 0700；09633 -> 9633。若真有 >4 位有效代碼則保留。
    return stripped.zfill(4) if len(stripped) <= 4 else stripped


def hk_yahoo_symbol(code):
    return f"{code}.HK" if code else None


def localized_stock_name(item):
    """優先取繁中/簡中名稱，沒有再退回 Gate symbol_desc / 固定中文表。"""
    descs = item.get("symbol_descs") or []
    preferred = ("zh-tw", "zh-hk", "zh-cn", "zh", "cn", "tw")
    by_lang = {}
    for d in descs:
        if not isinstance(d, dict):
            continue
        lang = str(d.get("lang", "")).strip().lower().replace("_", "-")
        value = str(d.get("value", "") or "").strip()
        if lang and value:
            by_lang[lang] = value
    for p in preferred:
        if p in by_lang:
            return by_lang[p]
    code = hk_code(item.get("symbol"))
    if code in HK_CORE_CODES:
        return HK_CORE_CODES[code]
    return str(item.get("symbol_desc", "") or code or item.get("symbol", "")).strip()


def discover_hk_stock_universe():
    """載入 Gate 股票區完整香港股票 universe。

    僅保留 asset_type=STOCK 的港股；ETF 不混進個股 2560。
    不設價格上限。中文名稱直接保留在 metadata。
    """
    all_items=[]
    page=1
    page_size=500
    max_pages=50
    while page <= max_pages:
        payload=gate_get(
            "/stock/symbols",
            {
                "exchange":"hk",
                "with_desc_i18n":"true",
                "page":page,
                "page_size":page_size,
            },
        )
        data=payload.get("data",{}) if isinstance(payload,dict) else {}
        items=data.get("list",[]) if isinstance(data,dict) else []
        if not items:
            break
        all_items.extend(items)
        total_page=data.get("total_page", data.get("total_pages")) if isinstance(data,dict) else None
        try:
            total_page=int(total_page) if total_page is not None else None
        except Exception:
            total_page=None
        if (total_page is not None and page>=total_page) or (total_page is None and len(items)<page_size):
            break
        page += 1

    universe=[]
    seen=set()
    for item in all_items:
        if str(item.get("exchange", "hk")).lower() != "hk":
            continue
        if str(item.get("asset_type", "STOCK")).upper() != "STOCK":
            continue
        raw=str(item.get("symbol", "") or "").strip()
        code=hk_code(raw)
        if not raw or not code or code in seen:
            continue
        seen.add(code)
        universe.append({
            "code":code,
            "gate_symbol":raw,
            "yahoo_symbol":hk_yahoo_symbol(code),
            "name_zh":localized_stock_name(item),
            "symbol_desc":str(item.get("symbol_desc", "") or "").strip(),
            "category":item.get("category"),
            "trade_status":item.get("trade_status"),
            "trade_mode":item.get("trade_mode"),
        })

    universe.sort(key=lambda x: x["code"])
    print(f"HK FULL UNIVERSE LOADED: Gate rows={len(all_items)} stocks={len(universe)} pages={page}")
    return universe


def discover_hk_stock_symbols():
    """相容舊介面：回傳 code -> Gate symbol。"""
    return {x["code"]:x["gate_symbol"] for x in discover_hk_stock_universe()}


def build_hk_fixed_pool(universe):
    """只從固定 25 檔中建立本輪港股池，不做全市場 K 線海選。"""
    by_code = {x["code"]: x for x in universe}

    selected = []
    missing = []

    for code, fallback_name in HK_FIXED_CODES.items():
        item = by_code.get(code)
        if item is None:
            missing.append(code)
            continue

        selected.append({
            **item,
            "name_zh": item.get("name_zh") or fallback_name,
        })

    print(
        f"HK FIXED POOL: configured={len(HK_FIXED_CODES)} "
        f"available={len(selected)} missing={len(missing)}"
    )
    if missing:
        print("HK FIXED POOL MISSING:", ", ".join(missing))

    return selected

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]


def yahoo_get(symbol, interval, range_text, retries=4):
    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_text,
        "includePrePost": "false",
        "events": "div,splits",
    })
    last = None
    for attempt in range(retries):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        url = f"{host}/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            result = data.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(f"Yahoo no result: {data.get('chart', {}).get('error')}")
            return result[0]
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 6))
    raise RuntimeError(f"Yahoo request failed: {last}")


def parse_yahoo_rows(data):
    ts = data.get("timestamp") or []
    q = (data.get("indicators", {}).get("quote") or [{}])[0]
    opens=q.get("open") or []; highs=q.get("high") or []; lows=q.get("low") or []
    closes=q.get("close") or []; vols=q.get("volume") or []
    rows=[]
    for i,tstamp in enumerate(ts):
        try:
            o,h,l,c=opens[i],highs[i],lows[i],closes[i]
            v=vols[i] if i < len(vols) else 0
        except IndexError:
            continue
        if None in (o,h,l,c):
            continue
        rows.append({
            "t":int(tstamp),
            "o":float(o),
            "h":float(h),
            "l":float(l),
            "c":float(c),
            "v":float(v or 0),
        })
    rows.sort(key=lambda z:z["t"])
    return rows


def fetch_hk_stock(code, interval):
    y=hk_yahoo_symbol(code)
    if not y:
        raise RuntimeError(f"HK Yahoo symbol missing: {code}")
    if interval == "1d":
        yi,yr="1d","2y"
    elif interval == "1h":
        yi,yr="60m","3mo"
    elif interval == "15m":
        yi,yr="15m","60d"
    else:
        raise RuntimeError(f"unsupported HK interval: {interval}")
    return parse_yahoo_rows(yahoo_get(y,yi,yr))


def hk_daily_prefilter(stock):
    """固定池舊版篩選（目前未使用）：只抓日K，先檢查流動性/波動/歷史。

    價格不設上限；核心股即使未達量能/波動門檻也保留進第二段。
    """
    code=stock["code"]
    rows=fetch_hk_stock(code,"1d")
    if len(rows) < HK_MIN_DAILY_BARS:
        return {**stock,"prefilter_pass":False,"prefilter_reason":"history","daily_bars":len(rows)}

    add_ind(rows,ONE_D)
    last=rows[-1]
    recent20=rows[-20:]
    avg_turnover=sum(abs(x["c"]*x.get("v",0)) for x in recent20)/max(1,len(recent20))
    atr14=last.get("atr14")
    atr_pct=(atr14/last["c"]*100.0) if atr14 and last.get("c") else 0.0
    hi=max(x["h"] for x in recent20)
    lo=min(x["l"] for x in recent20)
    range20_pct=((hi/lo)-1.0)*100.0 if lo>0 else 0.0
    trend_ok=hk_daily_strategy_ok(last)
    is_core=code in HK_CORE_CODES

    passed=(
        is_core or (
            avg_turnover >= HK_MIN_AVG_TURNOVER_20D
            and atr_pct >= HK_MIN_ATR_PCT
            and range20_pct >= HK_MIN_RANGE20_PCT
        )
    )

    # 分數只用來控制第二段掃描數量，不是交易評分或買賣排名。
    liquidity_score=max(0.0, math.log10(max(avg_turnover,1.0))-6.0)
    score=(liquidity_score*1.5)+(atr_pct*1.2)+(range20_pct*0.10)+(1.0 if trend_ok else 0.0)+(3.0 if is_core else 0.0)
    return {
        **stock,
        "prefilter_pass":passed,
        "prefilter_reason":"PASS" if passed else "liquidity_or_volatility",
        "daily_bars":len(rows),
        "last_price":last["c"],
        "avg_turnover_20d":avg_turnover,
        "atr_pct":atr_pct,
        "range20_pct":range20_pct,
        "daily_strategy":trend_ok,
        "prefilter_score":score,
    }


def build_hk_candidate_pool(universe):
    """舊版全市場候選池函式，目前固定池模式不呼叫。"""
    scanned=[]
    with ThreadPoolExecutor(max_workers=HK_SCAN_WORKERS) as ex:
        futs={ex.submit(hk_daily_prefilter,s):s for s in universe}
        for fut in as_completed(futs):
            s=futs[fut]
            try:
                scanned.append(fut.result())
            except Exception as e:
                scanned.append({**s,"prefilter_pass":False,"prefilter_reason":f"ERROR:{e}"})

    passed=[x for x in scanned if x.get("prefilter_pass")]
    passed.sort(key=lambda x:(x.get("prefilter_score",0.0),x.get("avg_turnover_20d",0.0)), reverse=True)

    core=[x for x in passed if x["code"] in HK_CORE_CODES]
    noncore=[x for x in passed if x["code"] not in HK_CORE_CODES]
    slots=max(0,HK_SCAN_MAX_CANDIDATES-len(core))
    selected=core+noncore[:slots]
    # 若核心股數本身已超上限，仍全部保留。
    selected.sort(key=lambda x:(x["code"] not in HK_CORE_CODES,-x.get("prefilter_score",0.0)))

    print(
        f"HK PREFILTER: universe={len(universe)} pass={len(passed)} "
        f"selected={len(selected)} core={len(core)} "
        f"minTurnover={HK_MIN_AVG_TURNOVER_20D:,.0f}HKD "
        f"minATR={HK_MIN_ATR_PCT:.2f}% minRange20={HK_MIN_RANGE20_PCT:.2f}% priceCap=NONE"
    )
    return selected, scanned


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
# HK 2560 stock logic: 1D strategy + 1H wave + 15m entry
# ============================================================

def hk_daily_strategy_ok(r):
    need=[r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60")]
    if any(x is None for x in need):
        return False
    return (
        r["c"] > r["ma25"]
        and r["ma25"] >= r["ma25_prev"]
        and r["vma5"] >= r["vma60"] * 0.90
    )


def hk_hour_wave_ok(r):
    need=[r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60")]
    if any(x is None for x in need):
        return False
    return (
        r["c"] >= r["ma25"] * 0.995
        and r["ma25"] >= r["ma25_prev"] * 0.995
        and (r["vma5"] >= r["vma60"] * 0.90 or r["vma5"] > r.get("vma5_prev",0))
    )


def hk_entry_15m_ok(r):
    need=[r.get("ma25"),r.get("ma25_prev"),r.get("vma5"),r.get("vma60")]
    if any(x is None for x in need):
        return False
    return (
        r["c"] >= r["ma25"] * 0.992
        and r["ma25"] >= r["ma25_prev"] * 0.992
        and r["vma5"] >= r["vma60"] * 0.85
    )


def hk_target_zone(rd, r1, current):
    atr = r1[-1].get("atr14") if r1 else None
    atr_pct = (atr/current*100.0) if atr and current else None
    h1 = sorted({x["h"] for x in r1[-90:] if x["h"] > current})
    hd = sorted({x["h"] for x in rd[-120:] if x["h"] > current})
    allh = sorted(h1 + hd)
    near = allh[0] if allh else None
    major = allh[-1] if allh else None
    atr1 = current + (atr or current*0.015)*1.5
    atr2 = current + (atr or current*0.015)*2.5
    candidates_low=[x for x in (near,atr1) if x is not None]
    base=max(current,min(candidates_low)) if candidates_low else current
    candidates_high=[x for x in (major,atr2) if x is not None]
    high=max(base,min(candidates_high)) if candidates_high else base
    high=min(high,current*1.18)
    base=min(base,high)
    return {
        "target_low":base,
        "target_base":base,
        "target_high":high,
        "expected_base_pct":pct_change(current,base),
        "expected_high_pct":pct_change(current,high),
        "nearest_resistance":near,
        "major_resistance":major,
        "nearest_support": max(
            [x["l"] for x in (r1[-90:] + rd[-120:]) if x.get("l") is not None and x["l"] < current],
            default=None,
        ),
        "major_support": min(
            [x["l"] for x in (r1[-90:] + rd[-120:]) if x.get("l") is not None and x["l"] < current],
            default=None,
        ),
        "atr_pct":atr_pct,
    }


def analyze_hk_stock(base_symbol, gate_stock_symbol, name_zh=None):
    now=int(datetime.now(timezone.utc).timestamp())
    rd=completed_only(fetch_hk_stock(base_symbol,"1d"), ONE_D, now)
    r1=completed_only(fetch_hk_stock(base_symbol,"1h"), ONE_H, now)
    r15=completed_only(fetch_hk_stock(base_symbol,"15m"), FIFTEEN_M, now)

    if len(rd)<65 or len(r1)<65 or len(r15)<65:
        return {
            "base":base_symbol,
            "name_zh":name_zh or HK_CORE_CODES.get(base_symbol, base_symbol),
            "display":f"{base_symbol} {name_zh or HK_CORE_CODES.get(base_symbol, base_symbol)}",
            "contract":gate_stock_symbol,
            "market_type":"HK_STOCK",
            "group":"HK_STOCK",
            "status":"WAIT_HISTORY",
            "history_1d_bars":len(rd),
            "history_1h_bars":len(r1),
            "history_15m_bars":len(r15),
        }

    add_ind(rd,ONE_D); add_ind(r1,ONE_H); add_ind(r15,FIFTEEN_M)
    d=rd[-1]; h=r1[-1]; m=r15[-1]
    strategic=hk_daily_strategy_ok(d)
    wave=hk_hour_wave_ok(h)
    timing=hk_entry_15m_ok(m)

    if strategic and wave and timing:
        status="STRICT"
    elif strategic and wave:
        status="PRE-STRICT"
    elif strategic:
        status="TREND_READY"
    elif wave:
        status="WATCH"
    else:
        status="NO_SIGNAL"

    # 港股同樣先估目標，不因狀態尚未升級而顯示 N/A。
    target=hk_target_zone(rd,r1,m["c"])
    target=ensure_target_zone(target,m["c"],r1)
    return {
        "base":base_symbol,
        "name_zh":name_zh or HK_CORE_CODES.get(base_symbol, base_symbol),
        "display":f"{base_symbol} {name_zh or HK_CORE_CODES.get(base_symbol, base_symbol)}",
        "contract":gate_stock_symbol,
        "market_type":"HK_STOCK",
        "group":"HK_STOCK",
        "status":status,
        "history_mode":"HK_1D_1H_15M",
        "strict_allowed":True,
        "latest_close":m["c"],
        "1d_strategy":strategic,
        "1h_wave":wave,
        "15m_entry":timing,
        "lower_entry_timing":timing,
        "expected_target":target,
        "grid":None,
        "hk_note":"Gate股票區港股；不套用USDT永續4H STRICT，也不自動建立槓桿網格。",
    }


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
# Multi-timeframe timing + expected target
# ============================================================

def lower_entry_flags(r1, r15):
    """1D=戰略、4H=波段；1H/15m只負責進場，不要求全週期均線同向。"""
    need1=[r1.get("ma5"),r1.get("ma10"),r1.get("ma20"),r1.get("ma20_prev")]
    need15=[r15.get("ma5"),r15.get("ma10"),r15.get("ma20")]
    if any(x is None for x in need1+need15):
        return {"1h_entry": False, "15m_entry": False, "timing_ok": False}

    h1=(
        r1["c"] >= r1["ma20"]*0.990
        and r1["ma20"] >= r1["ma20_prev"]*0.995
        and r1["ma5"] >= r1["ma10"]*0.985
    )
    m15=(
        r15["c"] >= r15["ma20"]*0.992
        and r15["ma5"] >= r15["ma10"]*0.985
    )
    return {"1h_entry": h1, "15m_entry": m15, "timing_ok": h1 and m15}


def lower_entry_timing(r1, r15):
    return lower_entry_flags(r1, r15)["timing_ok"]

def expected_target_zone(r4, rd, current):
    """非固定百分比：綜合4H ATR、4H/1D前高與上級趨勢估合理目標區。"""
    atr=r4[-1].get("atr14") if r4 else None
    atr_pct=(atr/current*100.0) if atr and current else None
    highs4=sorted({x["h"] for x in r4[-90:] if x["h"]>current})
    highsd=sorted({x["h"] for x in rd[-90:] if x["h"]>current}) if rd else []
    resistance=(highs4+highsd)
    resistance=sorted(resistance)[0] if resistance else None
    major=sorted(highs4+highsd)[-1] if (highs4 or highsd) else None
    atr1=current+(atr or current*0.02)*1.5
    atr2=current+(atr or current*0.02)*2.5
    base=max(current, min([x for x in (resistance,atr1) if x is not None]))
    high=max(base, min([x for x in (major,atr2) if x is not None]))
    # 防止單一歷史尖峰把目標拉到天邊，但不是用固定%產生目標，只作異常值護欄。
    high=min(high,current*1.20)
    base=min(base,high)
    support_candidates = [
        x["l"] for x in (r4[-90:] + (rd[-90:] if rd else []))
        if x.get("l") is not None and x["l"] < current
    ]
    nearest_support = max(support_candidates) if support_candidates else None
    major_support = min(support_candidates) if support_candidates else None

    return {
        "target_low":base,
        "target_base":base,
        "target_high":high,
        "expected_base_pct":pct_change(current,base),
        "expected_high_pct":pct_change(current,high),
        "nearest_resistance":resistance,
        "major_resistance":major,
        "nearest_support":nearest_support,
        "major_support":major_support,
        "atr_pct":atr_pct,
    }


def ensure_target_zone(target_zone, current, r4):
    """
    候選訊號先有目標，再判斷狀態。
    N/A 只允許代表資料真的不足，不可拿來當作「空間不足」。
    """
    z = dict(target_zone or {})
    if current is None or current <= 0:
        return z

    atr = None
    if r4:
        atr = r4[-1].get("atr14")

    fallback_step = atr if (atr is not None and atr > 0) else current * 0.02
    fallback_base = current + fallback_step * 1.5
    fallback_high = current + fallback_step * 2.5

    if z.get("target_base") is None:
        z["target_base"] = fallback_base
        z["target_low"] = fallback_base
    if z.get("target_high") is None:
        z["target_high"] = max(z["target_base"], fallback_high)

    if z.get("expected_base_pct") is None:
        z["expected_base_pct"] = pct_change(current, z["target_base"])
    if z.get("expected_high_pct") is None:
        z["expected_high_pct"] = pct_change(current, z["target_high"])

    return z


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
    r1 = completed_only(fetch(contract, "1h", LIMIT_1H), ONE_H, now)
    r15 = completed_only(fetch(contract, "15m", LIMIT_15M), FIFTEEN_M, now)

    # 4H 至少要能算 MA120；不足才是真正 WAIT_HISTORY。
    if len(r4) < 121 or len(r1) < 65 or len(r15) < 65:
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
    add_ind(r1, ONE_H)
    add_ind(r15, FIFTEEN_M)

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

    timing_flags = lower_entry_flags(r1[-1], r15[-1])
    timing_ok = timing_flags["timing_ok"]
    # 上級方向成立但下級尚未到進場窗口時，不硬砍趨勢，只降回 TREND_READY/WATCH。
    if status in ("PRE-STRICT", "STRICT") and not timing_ok:
        status = "TREND_READY"

    # 先估目標，再決定是否可執行 PRE-STRICT / STRICT。
    # WATCH / TREND_READY 也必須有合理目標區。
    target_zone = ensure_target_zone(
        expected_target_zone(
            r4,
            rd if history_mode != "4H_PROXY" else [],
            latest["c"],
        ),
        latest["c"],
        r4,
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
        "1h_entry": timing_flags["1h_entry"],
        "15m_entry": timing_flags["15m_entry"],
        "lower_entry_timing": timing_ok,
        "expected_target": target_zone,
        "grid": grid,
    }


def signal_label(r):
    return r.get("display") or r.get("base") or "UNKNOWN"


# ============================================================
# Notifications
# ============================================================

def send_ntfy(title, msg, priority="default", tags="bar_chart"):
    """
    ntfy notification:
    - Title header uses RFC 2047 UTF-8 encoding, so Chinese stock names are safe.
    - Notification failure is non-fatal and must never turn a valid trading signal into ERROR.
    """
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; notification skipped.")
        return False

    try:
        safe_title = Header(str(title), "utf-8").encode()

        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=str(msg).encode("utf-8"),
            method="POST",
            headers={
                "Title": safe_title,
                "Priority": str(priority),
                "Tags": str(tags),
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

        with urllib.request.urlopen(req, timeout=20) as resp:
            print("ntfy:", resp.status, title)
            return 200 <= resp.status < 300

    except Exception as e:
        print(f"NTFY WARN {title}: {e}")
        return False


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



def format_target(t):
    t=t or {}
    return (
        f"合理目標基準：{price_text(t.get('target_base'))} ({pct_text(t.get('expected_base_pct'))})\n"
        f"合理目標上緣：{price_text(t.get('target_high'))} ({pct_text(t.get('expected_high_pct'))})\n"
        f"最近壓力：{price_text(t.get('nearest_resistance'))}\n"
        f"主要壓力：{price_text(t.get('major_resistance'))}\n"
        f"最近支撐：{price_text(t.get('nearest_support'))}\n"
        f"主要支撐：{price_text(t.get('major_support'))}\n"
        f"ATR：{pct_text(t.get('atr_pct'))}"
    )

def format_signal_detail(r):
    t = r.get("expected_target") or {}
    status = r.get("status")

    if r.get("market_type") == "HK_STOCK":
        structure_lines = (
            f"日K戰略：{r.get('1d_strategy')}\n"
            f"1H波段：{r.get('1h_wave')}\n"
            f"15m進場：{r.get('15m_entry')}\n"
        )
    else:
        structure_lines = (
            f"日K soft：{r.get('1d_soft')}\n"
            f"日K確認：{r.get('1d_confirm')}\n"
            f"4H結構：{r.get('4h_structure')}\n"
            f"4H量能：{r.get('4h_relaxed_volume')}\n"
            f"1H進場：{r.get('1h_entry')}\n"
            f"15m進場：{r.get('15m_entry')}\n"
        )

    return (
        f"狀態：{status}\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"目標基準：{price_text(t.get('target_base'))} "
        f"({pct_text(t.get('expected_base_pct'))})\n"
        f"目標上緣：{price_text(t.get('target_high'))} "
        f"({pct_text(t.get('expected_high_pct'))})\n"
        f"最近支撐：{price_text(t.get('nearest_support'))}\n"
        f"最近壓力：{price_text(t.get('nearest_resistance'))}\n"
        f"{structure_lines}"
    )


def notify_early_signal(r):
    send_ntfy(
        f"2560 {r.get('status')} {signal_label(r)}",
        f"{signal_label(r)} 2560 訊號\n\n{format_signal_detail(r)}",
        "default",
        "chart_with_upwards_trend",
    )


def notify_pre_strict(r):
    g = r.get("grid")
    msg = (
        f"{signal_label(r)} 2560 PRE-STRICT\n\n"
        f"定位：第一段可開網候選\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"4H結構：{r.get('4h_structure', 'N/A')}\n"
        f"4H放寬量能：{r.get('4h_relaxed_volume', 'N/A')}\n"
        f"1D soft：{r.get('1d_soft', r.get('1d_strategy', 'N/A'))}\n"
        f"歷史模式：{r.get('history_mode')} "
        f"(4H={r.get('history_4h_bars')} / 1D={r.get('history_1d_bars')})\n"
        f"STRICT可用：{r.get('strict_allowed')}\n"
        f"1H/15m進場窗口：{r.get('lower_entry_timing')}\n\n"
        f"【合理目標區】\n{format_target(r.get('expected_target'))}\n\n"
        f"【執行方式】\n"
        f"{('Gate港股股票區：只做現股趨勢候選，不建立USDT永續槓桿網格。' if r.get('market_type') == 'HK_STOCK' else format_grid(g))}\n\n"
        f"{'⚠ 短歷史標的：可作 PRE-STRICT 第一網候選，但在 1D 歷史滿 61 根前不產生 STRICT。\\n' if not r.get('strict_allowed') else ''}"
        f"原則：寧可寬一點、少成交幾格，也不要下沿太貼現價。"
    )
    send_ntfy(
        f"2560 PRE-STRICT {signal_label(r)}",
        msg,
        "high",
        "chart_with_upwards_trend,bell",
    )


def notify_strict(r, symbol_state):
    g = r.get("grid")
    pre_price = symbol_state.get("pre_strict_entry_price")
    rise = pct_change(pre_price, r.get("latest_close"))

    if r.get("market_type") == "HK_STOCK":
        add_decision = "港股現股 STRICT：日K戰略、1H波段、15m進場三層確認；不建立第二槓桿網。"
    elif pre_price is None:
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
        f"{signal_label(r)} 2560 STRICT\n\n"
        f"定位：趨勢確認 / 第二網審核點\n"
        f"現價：{price_text(r.get('latest_close'))}\n"
        f"PRE-STRICT價：{price_text(pre_price)}\n"
        f"漲幅：{pct_text(rise)}\n"
        f"1H/15m進場窗口：{r.get('lower_entry_timing')}\n\n"
        f"【合理目標區】\n{format_target(r.get('expected_target'))}\n\n"
        f"第二網判斷：{add_decision}\n\n"
        f"【執行方式】\n"
        f"{('Gate港股股票區：STRICT=日K/1H/15m趨勢確認，只做現股候選。' if r.get('market_type') == 'HK_STOCK' else format_grid(g))}"
    )
    send_ntfy(
        f"2560 STRICT {signal_label(r)}",
        msg,
        "high",
        "chart_with_upwards_trend,bell",
    )


def notify_status_change(r, state):
    symbols = state.setdefault("symbols", {})
    st = symbols.setdefault(r["base"], {})
    old_status = st.get("status")
    new_status = r["status"]

    # WATCH / TREND_READY：只在狀態剛進入時通知一次，附完整現價/目標/結構。
    if (
        new_status in ("WATCH", "TREND_READY")
        and old_status != new_status
    ):
        notify_early_signal(r)

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
    print("2560 Cloud Monitor | FINAL 2026-09-28 HK SMART SCAN BOOTSTRAP")
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

    hk_should_scan, hk_scan_reason, hk_scan_token = hk_scan_decision(state)

    # 首次啟用 / state 被清空時，如果沒有任何港股 cache，
    # 即使目前是非交易時段，也破例完整掃一次建立基準結果。
    cached_hk = state.get("hk_last_results")
    hk_bootstrap_cache = not (isinstance(cached_hk, list) and len(cached_hk) > 0)

    if (not hk_should_scan) and hk_bootstrap_cache:
        hk_should_scan = True
        hk_scan_reason = "BOOTSTRAP_EMPTY_CACHE"
        hk_scan_token = hk_now().date().isoformat()

    hk_universe = []
    hk_candidates = []

    print(
        "HK SMART SCAN:",
        f"local={hk_now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"scan={hk_should_scan}",
        f"reason={hk_scan_reason}",
        f"token={hk_scan_token}",
        f"cached={0 if not isinstance(cached_hk, list) else len(cached_hk)}",
    )

    if hk_should_scan:
        hk_universe = discover_hk_stock_universe()
        hk_candidates = build_hk_fixed_pool(hk_universe)

    print("\nCONTRACT MAP")
    for base in REQUESTED:
        print(f"{base:<12} -> {contract_map.get(base)}")

    if hk_should_scan:
        print("\nGATE HK FIXED POOL")
        for s in hk_candidates:
            print(
                f"{s['code']:<6} {s.get('name_zh',''):<18} -> {s.get('gate_symbol')}"
            )
    else:
        print("\nGATE HK FIXED POOL: reuse previous results; no market-data rescan this run")

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

            if r["status"] == "NO_SIGNAL":
                print(
                    f"{base:<5} NO_SIGNAL    "
                    f"close={price_text(r['latest_close'])} "
                    f"hist={r.get('history_mode')}"
                )
            else:
                t = r.get("expected_target") or {}
                print(
                    f"{base:<5} "
                    f"{r['status']:<12} "
                    f"now={price_text(r['latest_close'])} "
                    f"target={price_text(t.get('target_base'))}"
                    f"~{price_text(t.get('target_high'))} "
                    f"space={pct_text(t.get('expected_base_pct'))}"
                    f"~{pct_text(t.get('expected_high_pct'))} "
                    f"support={price_text(t.get('nearest_support'))} "
                    f"resist={price_text(t.get('nearest_resistance'))} "
                    f"1Dsoft={r['1d_soft']} "
                    f"1D={r['1d_confirm']} "
                    f"4H={r['4h_structure']} "
                    f"1H={r.get('1h_entry')} "
                    f"15m={r.get('15m_entry')} "
                    f"hist={r.get('history_mode')}"
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

    print("\nSCAN HK STOCK 2560 | FIXED 25-STOCK POOL")

    if hk_should_scan:
        hk_scan_had_error = False

        for s in hk_candidates:
            base=s["code"]
            gate_symbol=s["gate_symbol"]
            name_zh=s.get("name_zh") or HK_CORE_CODES.get(base,base)
            label=f"{base} {name_zh}"

            try:
                r=analyze_hk_stock(base,gate_symbol,name_zh)
                results.append(r)

                if r.get("status") == "WAIT_HISTORY":
                    print(
                        f"{label:<28} WAIT_HISTORY "
                        f"1D={r.get('history_1d_bars')} "
                        f"1H={r.get('history_1h_bars')} "
                        f"15m={r.get('history_15m_bars')}"
                    )
                elif r["status"] == "NO_SIGNAL":
                    print(
                        f"{label:<28} NO_SIGNAL "
                        f"close={price_text(r.get('latest_close'))}"
                    )
                else:
                    tg=r.get("expected_target") or {}
                    print(
                        f"{label:<28} {r['status']:<12} "
                        f"now={price_text(r.get('latest_close'))} "
                        f"target={price_text(tg.get('target_base'))}"
                        f"~{price_text(tg.get('target_high'))} "
                        f"space={pct_text(tg.get('expected_base_pct'))}"
                        f"~{pct_text(tg.get('expected_high_pct'))} "
                        f"support={price_text(tg.get('nearest_support'))} "
                        f"resist={price_text(tg.get('nearest_resistance'))} "
                        f"1D={r.get('1d_strategy')} "
                        f"1H={r.get('1h_wave')} "
                        f"15m={r.get('15m_entry')}"
                    )

                notify_status_change(r,state)

            except Exception as e:
                hk_scan_had_error = True
                errors.append((label,str(e)))
                print(f"{label:<28} ERROR {e}")
                results.append({
                    "base":base,
                    "name_zh":name_zh,
                    "display":label,
                    "group":"HK_STOCK",
                    "status":"ERROR",
                    "error":str(e),
                })

            time.sleep(0.10)

        # 只有整輪真正跑完才記錄時間桶。
        # 個別股票錯誤仍保留，但不阻止下個 15m bucket 繼續掃。
        mark_hk_scan_done(state, hk_scan_reason, hk_scan_token)

        # 將本輪港股結果寫進 state，讓下一個 GitHub runner 在非交易時段可沿用。
        state["hk_last_results"] = [
            r for r in results
            if r.get("group") == "HK_STOCK"
        ]
        state["hk_last_results_updated_utc"] = now_iso()

        print(
            "HK SCAN COMPLETE:",
            f"reason={hk_scan_reason}",
            f"token={hk_scan_token}",
            f"symbols={len(hk_candidates)}"
        )

    else:
        previous_hk = load_previous_hk_results(state)
        results.extend(previous_hk)
        print(
            "HK SCAN SKIPPED:",
            hk_scan_reason,
            f"| reused_results={len(previous_hk)}"
        )

    RESULT_FILE.write_text(
        json.dumps(
            {
                "generated_utc": now_iso(),
                "rule_version": "2560_FINAL_2026_09_28_HK_SMART_SCAN_BOOTSTRAP",
                "hk_scan_reason": hk_scan_reason,
                "hk_scan_token": hk_scan_token,
                "hk_rescanned_this_run": hk_should_scan,
                "hk_gate_universe_count": len(hk_universe) if hk_should_scan else None,
                "hk_fixed_pool_configured": len(HK_FIXED_CODES),
                "hk_fixed_pool_available": len(hk_candidates) if hk_should_scan else None,
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
