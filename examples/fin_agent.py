#!/usr/bin/env python3
"""
fin-agent — A股金融研究工作流 Demo

基于 CoWorker Protocol 的 Skill-as-API 架构：
- 用户只能看到 skill 的输入输出 schema
- 量价分析规则、判断逻辑、数据处理代码完全私有
- 通过 XMTP E2E 加密通信

Skills:
  stock_info      — 股票基础数据 + 均线 (trust: 0)
  ma60_position   — 60均线位置判断 (trust: 1)
  volume_analysis — 量价关系分析 (trust: 1)
  golden_eye      — 月线黄金眼检测 (trust: 1)
  market_state    — 综合市场状态 (trust: 1)

Usage:
  pip install agent-coworker akshare
  python fin_agent.py

Then others can call your skills:
  coworker connect <your-invite>
  coworker call <invite> market_state --input '{"symbol":"600519"}'
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import pandas as pd

from agent_coworker import Agent

# ═══════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════

AGENT_NAME = os.getenv("FIN_AGENT_NAME", "fin-agent")
DATA_DIR = os.getenv("FIN_DATA_DIR", os.path.expanduser("~/.coworker"))
CACHE_DIR = os.getenv("FIN_CACHE_DIR", "/tmp/fin-agent-cache")
VERSION = "0.3.0"
SCHEMA_VERSION = "fin-agent.signal.v2"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

DISCLAIMER = (
    "Technical feature analysis only. Not investment advice. "
    "Past performance does not indicate future results. "
    "仅为技术特征分析，不构成投资建议。"
)

# Knowledge base paths
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_KNOWLEDGE_DIR = os.path.join(_SCRIPT_DIR, "fin_knowledge")

# ═══════════════════════════════════════════════════════════
# Cache Layer
# ═══════════════════════════════════════════════════════════

class DataCache:
    """TTL cache with negative caching and thread safety."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self._mem: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, key: str, ttl: int) -> Any | None:
        now = time.time()
        with self._lock:
            if key in self._mem:
                ts, val = self._mem[key]
                if now - ts < ttl:
                    return val
        return None

    def set(self, key: str, value: Any):
        with self._lock:
            self._mem[key] = (time.time(), value)

    def clear_expired(self, max_age: int = 3600):
        now = time.time()
        with self._lock:
            expired = [k for k, (ts, _) in self._mem.items() if now - ts > max_age]
            for k in expired:
                del self._mem[k]


cache = DataCache(CACHE_DIR)

# Negative cache for invalid symbols
_INVALID_SYMBOLS: dict[str, float] = {}
_INVALID_TTL = 3600  # 1 hour

# ═══════════════════════════════════════════════════════════
# Data Client (akshare wrapper with caching)
# ═══════════════════════════════════════════════════════════

def _normalize_symbol(symbol: str) -> str:
    """Normalize stock symbol: accept 600519 / sh600519 / 600519.SH"""
    s = symbol.strip().upper()
    s = re.sub(r'^(SH|SZ|BJ)', '', s)
    s = re.sub(r'\.(SH|SZ|BJ|XSHG|XSHE)$', '', s)
    if not re.match(r'^\d{6}$', s):
        raise ValueError(f"Invalid symbol format: {symbol}. Expected 6-digit code like 600519")
    return s


def _is_trading_hours() -> bool:
    """Check if current time is within A-share trading hours (CST)."""
    now = datetime.now(timezone(timedelta(hours=8)))
    h, m = now.hour, now.minute
    t = h * 60 + m
    return (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)


def _get_realtime_all() -> pd.DataFrame:
    """Get all A-share realtime quotes with caching."""
    ttl = 15 if _is_trading_hours() else 300
    cached = cache.get("realtime_all", ttl)
    if cached is not None:
        return cached

    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    cache.set("realtime_all", df)
    return df


def _get_realtime_one(symbol: str) -> dict:
    """Get realtime quote for a single stock."""
    sym = _normalize_symbol(symbol)

    # Check negative cache
    if sym in _INVALID_SYMBOLS and time.time() - _INVALID_SYMBOLS[sym] < _INVALID_TTL:
        raise ValueError(f"Symbol not found: {sym}")

    df = _get_realtime_all()
    row = df[df['代码'] == sym]
    if row.empty:
        _INVALID_SYMBOLS[sym] = time.time()
        raise ValueError(f"Symbol not found: {sym}")

    r = row.iloc[0]
    return {
        "symbol": sym,
        "name": str(r.get('名称', '')),
        "latest_price": float(r.get('最新价', 0)),
        "change_pct": float(r.get('涨跌幅', 0)),
        "volume": int(r.get('成交量', 0)),
        "amount": float(r.get('成交额', 0)),
        "high": float(r.get('最高', 0)),
        "low": float(r.get('最低', 0)),
        "open": float(r.get('今开', 0)),
        "prev_close": float(r.get('昨收', 0)),
    }


def _get_daily_kline(symbol: str, days: int = 250) -> pd.DataFrame:
    """Get daily K-line data with MA calculations."""
    sym = _normalize_symbol(symbol)
    cache_key = f"daily_{sym}_{days}"
    ttl = 6 * 3600 if not _is_trading_hours() else 60

    cached = cache.get(cache_key, ttl)
    if cached is not None:
        return cached

    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=sym, period="daily", adjust="qfq")
    if df.empty:
        _INVALID_SYMBOLS[sym] = time.time()
        raise ValueError(f"No data for symbol: {sym}")

    # Take last N days
    df = df.tail(max(days, 250)).copy()

    # Calculate MAs
    for period in [5, 10, 20, 60]:
        df[f'MA{period}'] = df['收盘'].rolling(period).mean()

    # Volume MAs
    for period in [5, 10, 60]:
        df[f'VOL_MA{period}'] = df['成交量'].rolling(period).mean()

    cache.set(cache_key, df)
    return df


def _get_monthly_kline(symbol: str) -> pd.DataFrame:
    """Get monthly K-line data for golden eye detection."""
    sym = _normalize_symbol(symbol)
    cache_key = f"monthly_{sym}"

    cached = cache.get(cache_key, 24 * 3600)
    if cached is not None:
        return cached

    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=sym, period="monthly", adjust="qfq")
    if df.empty:
        raise ValueError(f"No monthly data for: {sym}")

    for period in [5, 10, 20]:
        df[f'MA{period}'] = df['收盘'].rolling(period).mean()

    cache.set(cache_key, df)
    return df


# ═══════════════════════════════════════════════════════════
# Analysis Logic (PRIVATE — this is our know-how)
# ═══════════════════════════════════════════════════════════

def _analyze_volume_price(df: pd.DataFrame) -> dict:
    """Apply 4 volume-price rules (private logic).

    Rules (from PKU lecture series):
    1. High + shrink volume → strength continuation
    2. High + expand volume → risk rising
    3. Low + expand volume → activity emerging
    4. Volume up + price up → trend confirmed
    """
    if len(df) < 61:
        return {"pattern": "insufficient_data", "description": "数据不足60日", "rules_triggered": []}

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(latest['收盘'])
    prev_price = float(prev['收盘'])
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else price
    vol = float(latest['成交量'])
    vol_ma60 = float(latest['VOL_MA60']) if pd.notna(latest['VOL_MA60']) else vol

    price_change = (price / prev_price - 1) if prev_price > 0 else 0
    vol_ratio = (vol / vol_ma60) if vol_ma60 > 0 else 1
    is_high = price > ma60 * 1.05  # 5% above MA60
    is_low = price < ma60 * 0.95   # 5% below MA60
    is_expand = vol_ratio > 1.3
    is_shrink = vol_ratio < 0.7

    rules = []

    # Rule 1: High + shrink → strength continuation
    if is_high and is_shrink:
        rules.append({
            "rule": "high_shrink",
            "label": "偏强延续",
            "description": "高位缩量，主力控盘良好",
        })

    # Rule 2: High + expand → risk rising
    if is_high and is_expand:
        rules.append({
            "rule": "high_expand",
            "label": "高位风险抬升",
            "description": "高位放量，多空分歧加大",
        })

    # Rule 3: Low + expand → activity emerging
    if is_low and is_expand:
        rules.append({
            "rule": "low_expand",
            "label": "低位活跃",
            "description": "低位放量，资金开始关注",
        })

    # Rule 4: Volume up + price up → trend confirmed
    if price_change > 0.01 and vol_ratio > 1.2:
        rules.append({
            "rule": "vol_up_price_up",
            "label": "趋势确认",
            "description": "量增价升，趋势得到量能验证",
        })

    # Default pattern description
    vol_desc = "放量" if is_expand else "缩量" if is_shrink else "平量"
    price_desc = "上涨" if price_change > 0.005 else "下跌" if price_change < -0.005 else "横盘"
    pattern = f"{vol_desc}{price_desc}"

    return {
        "pattern": pattern,
        "description": f"成交量为60日均量的{vol_ratio:.1f}倍，{vol_desc}；价格{price_desc}({price_change*100:+.1f}%)",
        "vol_ratio": round(vol_ratio, 2),
        "price_change_pct": round(price_change * 100, 2),
        "rules_triggered": rules,
    }


def _analyze_ma60(df: pd.DataFrame) -> dict:
    """Analyze MA60 position and trend (private logic)."""
    if len(df) < 80:
        return {"position": "insufficient_data", "analysis_text": "数据不足"}

    latest = df.iloc[-1]
    price = float(latest['收盘'])
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else price

    # Position
    distance_pct = (price / ma60 - 1) * 100 if ma60 > 0 else 0
    if abs(distance_pct) < 1:
        position = "touching"
    elif distance_pct > 0:
        position = "above"
    else:
        position = "below"

    # MA60 trend (slope over 20 days)
    ma60_20ago = float(df.iloc[-20]['MA60']) if len(df) >= 20 and pd.notna(df.iloc[-20]['MA60']) else ma60
    slope_pct = (ma60 / ma60_20ago - 1) * 100 if ma60_20ago > 0 else 0
    if slope_pct > 0.5:
        trend = "up"
    elif slope_pct < -0.5:
        trend = "down"
    else:
        trend = "flat"

    # Text
    pos_text = {"above": "上方", "below": "下方", "touching": "附近"}[position]
    trend_text = {"up": "向上", "down": "向下", "flat": "走平"}[trend]
    analysis = f"当前价格在60日均线{pos_text}({distance_pct:+.1f}%)，MA60方向{trend_text}。"

    # Key insight from lectures: below MA60 + downward = weak structure
    if position == "below" and trend == "down":
        analysis += "60均线下方且方向向下，整体结构偏弱。"
    elif position == "above" and trend == "up":
        analysis += "60均线上方且方向向上，趋势健康。"

    return {
        "latest_close": round(price, 2),
        "ma60": round(ma60, 2),
        "position": position,
        "distance_pct": round(distance_pct, 1),
        "ma60_trend": trend,
        "ma60_slope_pct_20d": round(slope_pct, 2),
        "analysis_text": analysis,
    }


def _analyze_golden_eye(mdf: pd.DataFrame) -> dict:
    """Detect monthly golden eye pattern (private logic).

    Golden eye: MA5 crosses above MA10, MA10 crosses above MA20
    Exit signal: MA5 crosses below MA10 (death cross)
    """
    if len(mdf) < 21:
        return {"golden_eye": False, "stage": "insufficient_data", "analysis_text": "月线数据不足"}

    latest = mdf.iloc[-1]
    prev = mdf.iloc[-2]

    ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
    ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0

    prev_ma5 = float(prev['MA5']) if pd.notna(prev['MA5']) else 0
    prev_ma10 = float(prev['MA10']) if pd.notna(prev['MA10']) else 0

    golden = ma5 > ma10 > ma20
    death_cross = ma5 < ma10 and prev_ma5 >= prev_ma10
    golden_cross = ma5 > ma10 and prev_ma5 <= prev_ma10

    if golden:
        stage = "formed"
        text = f"月线黄金眼已形成（MA5={ma5:.0f} > MA10={ma10:.0f} > MA20={ma20:.0f}），中长期趋势向好。"
    elif golden_cross:
        stage = "forming"
        text = f"月线MA5刚上穿MA10，黄金眼正在形成中，需MA10上穿MA20确认。"
    elif death_cross:
        stage = "death_cross"
        text = f"月线MA5下穿MA10，形成死叉，中期趋势转弱。"
    else:
        stage = "not_formed"
        text = f"月线黄金眼未形成（MA5={ma5:.0f}, MA10={ma10:.0f}, MA20={ma20:.0f}）。"

    return {
        "golden_eye": golden,
        "stage": stage,
        "ma5_monthly": round(ma5, 2),
        "ma10_monthly": round(ma10, 2),
        "ma20_monthly": round(ma20, 2),
        "analysis_text": text,
    }


# ═══════════════════════════════════════════════════════════
# Signal Scoring Engine (PRIVATE — core know-how)
# ═══════════════════════════════════════════════════════════

def _compute_decision_signal(ma60_r: dict, vol_r: dict, ge_r: dict, alignment: str) -> dict:
    """Compute machine-readable decision signal from component analyses.

    This is the core proprietary logic — scoring weights and thresholds
    are our know-how, never transmitted to callers.
    """
    # Component scores (0-1)
    # Trend score: MA60 position + trend direction
    trend_score = 0.5
    if ma60_r["position"] == "above" and ma60_r["ma60_trend"] == "up":
        trend_score = 0.9
    elif ma60_r["position"] == "above" and ma60_r["ma60_trend"] == "flat":
        trend_score = 0.7
    elif ma60_r["position"] == "touching":
        trend_score = 0.5
    elif ma60_r["position"] == "below" and ma60_r["ma60_trend"] == "flat":
        trend_score = 0.35
    elif ma60_r["position"] == "below" and ma60_r["ma60_trend"] == "down":
        trend_score = 0.15

    # Volume-price score
    vp_score = 0.5
    vol_ratio = vol_r.get("vol_ratio", 1.0)
    price_chg = vol_r.get("price_change_pct", 0)
    rules = [r["rule"] for r in vol_r.get("rules_triggered", [])]
    if "vol_up_price_up" in rules:
        vp_score = 0.85
    elif "high_shrink" in rules:
        vp_score = 0.75
    elif "low_expand" in rules:
        vp_score = 0.7
    elif "high_expand" in rules:
        vp_score = 0.3
    elif price_chg > 0 and vol_ratio < 0.8:
        vp_score = 0.6  # shrink up — mild positive

    # Structure score (MA alignment)
    structure_score = {"bullish": 0.9, "neutral": 0.5, "bearish": 0.15}.get(alignment, 0.5)

    # Monthly score (golden eye)
    monthly_score = 0.5
    stage = ge_r.get("stage", "not_formed")
    if stage == "formed":
        monthly_score = 0.9
    elif stage == "forming":
        monthly_score = 0.7
    elif stage == "death_cross":
        monthly_score = 0.15

    # Weighted composite
    weights = {"trend": 0.30, "vp": 0.25, "structure": 0.25, "monthly": 0.20}
    composite = (
        trend_score * weights["trend"] +
        vp_score * weights["vp"] +
        structure_score * weights["structure"] +
        monthly_score * weights["monthly"]
    )

    # Signal classification
    if composite >= 0.72:
        signal, action_bias = "bullish", "accumulate"
    elif composite >= 0.58:
        signal, action_bias = "bullish", "observe"
    elif composite >= 0.42:
        signal, action_bias = "neutral", "hold"
    elif composite >= 0.28:
        signal, action_bias = "bearish", "reduce"
    else:
        signal, action_bias = "bearish", "avoid"

    # Signal strength (how decisive the signal is)
    signal_strength = abs(composite - 0.5) * 2  # 0-1, higher = more decisive

    # Risk level
    risk_level = "low" if composite > 0.65 else "high" if composite < 0.35 else "medium"

    # Confidence (data quality + signal clarity)
    confidence = min(0.95, 0.5 + signal_strength * 0.4 + (0.1 if stage != "insufficient_data" else 0))

    return {
        "signal": signal,
        "action_bias": action_bias,
        "confidence_score": round(confidence, 2),
        "signal_strength": round(signal_strength, 2),
        "risk_level": risk_level,
        "time_horizon": "swing_10_30d",
        "composite_score": round(composite, 2),
        "component_scores": {
            "trend_score": round(trend_score, 2),
            "volume_price_score": round(vp_score, 2),
            "structure_score": round(structure_score, 2),
            "monthly_score": round(monthly_score, 2),
        },
    }


def _build_factor_breakdown(ma60_r: dict, vol_r: dict, ge_r: dict, alignment: str) -> dict:
    """Build explainable factor breakdown for LLM consumption."""
    bullish, bearish = [], []

    if ma60_r["position"] == "above":
        bullish.append({"code": "above_ma60", "label": "站上60日均线", "weight": 0.2})
    elif ma60_r["position"] == "below":
        bearish.append({"code": "below_ma60", "label": "跌破60日均线", "weight": 0.2})

    if ma60_r["ma60_trend"] == "up":
        bullish.append({"code": "ma60_up", "label": "60均线方向向上", "weight": 0.15})
    elif ma60_r["ma60_trend"] == "down":
        bearish.append({"code": "ma60_down", "label": "60均线方向向下", "weight": 0.15})

    for r in vol_r.get("rules_triggered", []):
        entry = {"code": r["rule"], "label": r["label"], "weight": 0.12}
        if r["rule"] in ("vol_up_price_up", "high_shrink", "low_expand"):
            bullish.append(entry)
        elif r["rule"] in ("high_expand",):
            bearish.append(entry)

    if alignment == "bullish":
        bullish.append({"code": "ma_bullish", "label": "均线多头排列", "weight": 0.15})
    elif alignment == "bearish":
        bearish.append({"code": "ma_bearish", "label": "均线空头排列", "weight": 0.15})

    if ge_r.get("golden_eye"):
        bullish.append({"code": "golden_eye", "label": "月线黄金眼形成", "weight": 0.1})
    elif ge_r.get("stage") == "death_cross":
        bearish.append({"code": "death_cross", "label": "月线死叉", "weight": 0.1})

    return {"bullish": bullish, "bearish": bearish}


def _build_machine_flags(ma60_r: dict, vol_r: dict, ge_r: dict, alignment: str) -> list:
    """Build flat list of boolean flags for quick machine filtering."""
    flags = []
    flags.append(f"ma60_{ma60_r['position']}")
    flags.append(f"ma60_trend_{ma60_r['ma60_trend']}")
    flags.append(f"alignment_{alignment}")
    if ge_r.get("golden_eye"):
        flags.append("golden_eye_formed")
    if ge_r.get("stage") == "death_cross":
        flags.append("death_cross")
    for r in vol_r.get("rules_triggered", []):
        flags.append(f"vol_{r['rule']}")
    return flags


# ═══════════════════════════════════════════════════════════
# LLM Analysis Engine (PRIVATE — core know-how)
# ═══════════════════════════════════════════════════════════

# Load knowledge base at module level
_SYSTEM_PROMPT = ""
_RULES_DB: list = []
try:
    with open(os.path.join(_KNOWLEDGE_DIR, "system_prompt.md")) as f:
        _SYSTEM_PROMPT = f.read()
    with open(os.path.join(_KNOWLEDGE_DIR, "rules.json")) as f:
        _RULES_DB = json.load(f).get("rules", [])
except FileNotFoundError:
    pass  # Will fall back to rule-engine only mode


def _select_relevant_rules(ma60_r: dict, vol_r: dict, ge_r: dict) -> list:
    """Select relevant courseware rules based on current market state (RAG-lite)."""
    selected_ids = set()

    # Always include core principles
    selected_ids.update(["PRINCIPLE_01", "PRINCIPLE_02"])

    # MA60 position based
    if ma60_r["position"] == "above":
        selected_ids.update(["BUY_01", "BUY_02", "VPA_01", "VPA_06", "VPA_09", "PHASE_03"])
    elif ma60_r["position"] == "below":
        selected_ids.update(["BUY_03", "VPA_03", "VPA_04", "PHASE_01", "PHASE_02", "PATTERN_01"])
    else:
        selected_ids.update(["BUY_02", "VPA_01", "VPA_06", "PHASE_02", "PHASE_03"])

    # Volume rules triggered
    for r in vol_r.get("rules_triggered", []):
        rule_map = {
            "high_shrink": "VPA_01", "high_expand": "VPA_02",
            "low_expand": "VPA_04", "vol_up_price_up": "VPA_06",
        }
        if r["rule"] in rule_map:
            selected_ids.add(rule_map[r["rule"]])

    # Golden eye
    if ge_r.get("stage") in ("formed", "forming"):
        selected_ids.add("BUY_05")
    elif ge_r.get("stage") == "death_cross":
        selected_ids.add("SELL_07")

    # Sell signals if high
    if ma60_r.get("distance_pct", 0) > 15:
        selected_ids.update(["SELL_04", "SELL_05", "SELL_06"])

    rules = [r for r in _RULES_DB if r["rule_id"] in selected_ids]
    return rules[:8]  # Max 8 rules to control token cost


def _get_news(symbol: str) -> str:
    """Fetch recent news for a stock via akshare."""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=symbol)
        if df is not None and len(df) > 0:
            items = []
            for _, row in df.head(5).iterrows():
                title = str(row.get("新闻标题", ""))
                content = str(row.get("新闻内容", ""))[:200]
                items.append(f"- {title}: {content}")
            return "\n".join(items)
    except Exception:
        pass
    return "近期新闻数据暂不可用"


def _call_deepseek(system: str, user: str, max_tokens: int = 2000) -> str:
    """Call DeepSeek API."""
    import urllib.request

    if not DEEPSEEK_API_KEY:
        return "[LLM unavailable: DEEPSEEK_API_KEY not set]"

    data = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(DEEPSEEK_URL, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    })

    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM error: {str(e)[:100]}]"


def _build_llm_analysis(sym: str, rt: dict, df, mdf,
                         ma60_r: dict, vol_r: dict, ge_r: dict,
                         alignment: str) -> str:
    """Build full LLM-powered analysis using courseware knowledge."""
    latest = df.iloc[-1]
    ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
    ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else 0

    # Build structured data
    stock_data = (
        f"## 标的数据\n"
        f"- 股票：{rt['name']}({sym})\n"
        f"- 最新价：{rt['latest_price']}\n"
        f"- 涨跌幅：{rt['change_pct']}%\n"
        f"- 成交量：{rt['volume']}\n"
        f"- MA5={ma5:.2f}, MA10={ma10:.2f}, MA20={ma20:.2f}, MA60={ma60:.2f}\n"
        f"- 均线排列：{alignment}\n"
        f"- 60均线位置：{ma60_r['position']}（距离{ma60_r['distance_pct']:+.1f}%）\n"
        f"- 60均线方向：{ma60_r['ma60_trend']}\n"
        f"- 量价特征：{vol_r['pattern']}（量比={vol_r['vol_ratio']}）\n"
        f"- 月线黄金眼：{ge_r['stage']}（MA5月={ge_r.get('ma5_monthly',0):.0f}, "
        f"MA10月={ge_r.get('ma10_monthly',0):.0f}, MA20月={ge_r.get('ma20_monthly',0):.0f}）\n"
    )

    # Select and format rules
    rules = _select_relevant_rules(ma60_r, vol_r, ge_r)
    rules_text = "\n\n".join([
        f"### {r['rule_id']}: {r['topic']}\n{r['content']}"
        + (f"\n信号条件：{r['signals']}" if 'signals' in r else "")
        + (f"\n失效条件：{r['invalidation']}" if 'invalidation' in r else "")
        for r in rules
    ])

    # Get news
    news = _get_news(sym)

    user_msg = (
        f"请分析以下 A 股标的：\n\n"
        f"{stock_data}\n\n"
        f"## 检索到的相关教材规则\n{rules_text}\n\n"
        f"## 近期新闻\n{news}\n\n"
        f"请按照输出格式（标的概况→阶段判断→量价特征→形态映射→"
        f"支撑证据→失效条件→新闻影响→综合评估→风险声明）进行分析。"
    )

    return _call_deepseek(_SYSTEM_PROMPT, user_msg)


# ═══════════════════════════════════════════════════════════
# Agent + Skills
# ═══════════════════════════════════════════════════════════

agent = Agent(AGENT_NAME, data_dir=DATA_DIR)


@agent.skill(
    "stock_info",
    description="Query A-share stock basic data with moving averages. 查询A股股票基础数据。",
    input_schema={"symbol": "str"},
    output_schema={"symbol": "str", "name": "str", "latest_price": "float",
                   "change_pct": "float", "volume": "int", "ma": "dict"},
    min_trust_tier=0,
    version="1.0.0",
)
def stock_info(symbol: str) -> dict:
    sym = _normalize_symbol(symbol)
    rt = _get_realtime_one(sym)

    # Get MAs from daily kline
    try:
        df = _get_daily_kline(sym)
        latest = df.iloc[-1]
        ma = {
            "ma5": round(float(latest['MA5']), 2) if pd.notna(latest['MA5']) else None,
            "ma10": round(float(latest['MA10']), 2) if pd.notna(latest['MA10']) else None,
            "ma20": round(float(latest['MA20']), 2) if pd.notna(latest['MA20']) else None,
            "ma60": round(float(latest['MA60']), 2) if pd.notna(latest['MA60']) else None,
        }
    except Exception:
        ma = {"ma5": None, "ma10": None, "ma20": None, "ma60": None}

    return {
        "symbol": rt["symbol"],
        "name": rt["name"],
        "latest_price": rt["latest_price"],
        "change_pct": rt["change_pct"],
        "volume": rt["volume"],
        "amount": rt["amount"],
        "high": rt["high"],
        "low": rt["low"],
        "ma": ma,
        "as_of": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "disclaimer": DISCLAIMER,
    }


@agent.skill(
    "ma60_position",
    description="Analyze stock position relative to 60-day moving average. 分析股价与60日均线的位置关系。",
    input_schema={"symbol": "str"},
    output_schema={"position": "str", "distance_pct": "float", "ma60_trend": "str", "analysis_text": "str"},
    min_trust_tier=1,
    version="1.0.0",
)
def ma60_position(symbol: str) -> dict:
    df = _get_daily_kline(symbol)
    result = _analyze_ma60(df)
    result["symbol"] = _normalize_symbol(symbol)
    result["disclaimer"] = DISCLAIMER
    return result


@agent.skill(
    "volume_analysis",
    description="Analyze volume-price relationship patterns. 分析量价关系特征。",
    input_schema={"symbol": "str"},
    output_schema={"pattern": "str", "vol_ratio": "float", "rules_triggered": "list", "description": "str"},
    min_trust_tier=1,
    version="1.0.0",
)
def volume_analysis(symbol: str) -> dict:
    df = _get_daily_kline(symbol)
    result = _analyze_volume_price(df)
    result["symbol"] = _normalize_symbol(symbol)
    result["disclaimer"] = DISCLAIMER
    return result


@agent.skill(
    "golden_eye",
    description="Detect monthly golden eye pattern (MA5>MA10>MA20). 检测月线黄金眼形态。",
    input_schema={"symbol": "str"},
    output_schema={"golden_eye": "bool", "stage": "str", "analysis_text": "str"},
    min_trust_tier=1,
    version="1.0.0",
)
def golden_eye(symbol: str) -> dict:
    mdf = _get_monthly_kline(symbol)
    result = _analyze_golden_eye(mdf)
    result["symbol"] = _normalize_symbol(symbol)
    result["disclaimer"] = DISCLAIMER
    return result


@agent.skill(
    "market_state",
    description="Comprehensive market state analysis combining MA, volume, and monthly patterns. 综合分析市场状态。",
    input_schema={"symbol": "str"},
    output_schema={"summary": "str", "metrics": "dict", "risk_factors": "list"},
    min_trust_tier=1,
    version="1.0.0",
)
def market_state(symbol: str) -> dict:
    sym = _normalize_symbol(symbol)
    rt = _get_realtime_one(sym)
    df = _get_daily_kline(sym)
    mdf = _get_monthly_kline(sym)

    # Run all analyzers
    ma60_result = _analyze_ma60(df)
    vol_result = _analyze_volume_price(df)
    ge_result = _analyze_golden_eye(mdf)

    # MA alignment
    latest = df.iloc[-1]
    ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
    ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else 0

    if ma5 > ma10 > ma20 > ma60:
        alignment = "bullish"
        alignment_text = "多头排列"
    elif ma5 < ma10 < ma20 < ma60:
        alignment = "bearish"
        alignment_text = "空头排列"
    else:
        alignment = "neutral"
        alignment_text = "震荡格局"

    # Build summary
    name = rt["name"]
    summary_parts = [
        f"{name}({sym})",
        ma60_result["analysis_text"],
        f"量价关系：{vol_result['description']}。",
        f"均线排列：{alignment_text}。",
        ge_result["analysis_text"],
    ]

    # Risk factors
    risks = []
    if ma60_result["position"] == "below" and ma60_result["ma60_trend"] == "down":
        risks.append("60均线下方且方向向下")
    if vol_result.get("rules_triggered"):
        for r in vol_result["rules_triggered"]:
            if "风险" in r.get("label", ""):
                risks.append(r["label"])
    if not ge_result["golden_eye"]:
        risks.append("月线黄金眼未形成")
    if alignment == "bearish":
        risks.append("均线空头排列")

    return {
        "symbol": sym,
        "name": name,
        "summary": " ".join(summary_parts),
        "metrics": {
            "price": rt["latest_price"],
            "change_pct": rt["change_pct"],
            "ma60": ma60_result["ma60"],
            "ma60_position": ma60_result["position"],
            "ma60_distance_pct": ma60_result["distance_pct"],
            "ma60_trend": ma60_result["ma60_trend"],
            "volume_pattern": vol_result["pattern"],
            "vol_ratio": vol_result["vol_ratio"],
            "ma_alignment": alignment,
            "golden_eye": ge_result["golden_eye"],
            "golden_eye_stage": ge_result["stage"],
        },
        "volume_rules": vol_result["rules_triggered"],
        "risk_factors": risks,
        "data_source": "akshare (eastmoney)",
        "as_of": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "disclaimer": DISCLAIMER,
    }


@agent.skill(
    "decision_snapshot",
    description="Full pre-trade analysis snapshot for one stock. Returns machine-readable signal + factor breakdown. "
                "Recommended default entry point for AI agent integration. "
                "单票完整决策快照，推荐作为其他 Agent 的默认调用入口。",
    input_schema={"symbol": "str"},
    output_schema={"decision_signal": "dict", "factor_breakdown": "dict",
                   "machine_flags": "list", "raw_metrics": "dict", "summary": "str"},
    min_trust_tier=1,
    version="1.0.0",
)
def decision_snapshot(symbol: str) -> dict:
    sym = _normalize_symbol(symbol)
    rt = _get_realtime_one(sym)
    df = _get_daily_kline(sym)
    mdf = _get_monthly_kline(sym)
    now = datetime.now(timezone(timedelta(hours=8)))

    ma60_r = _analyze_ma60(df)
    vol_r = _analyze_volume_price(df)
    ge_r = _analyze_golden_eye(mdf)

    latest = df.iloc[-1]
    ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
    ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else 0
    alignment = "bullish" if ma5 > ma10 > ma20 > ma60 else \
                "bearish" if ma5 < ma10 < ma20 < ma60 else "neutral"

    signal = _compute_decision_signal(ma60_r, vol_r, ge_r, alignment)
    factors = _build_factor_breakdown(ma60_r, vol_r, ge_r, alignment)
    flags = _build_machine_flags(ma60_r, vol_r, ge_r, alignment)

    # Human summary
    name = rt["name"]
    parts = [f"{name}({sym})", ma60_r["analysis_text"],
             f"量价：{vol_r['description']}。", ge_r["analysis_text"]]

    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "decision_snapshot",
        "asset": {"market": "CN-A", "symbol": sym, "name": name},
        "decision_signal": signal,
        "factor_breakdown": factors,
        "machine_flags": flags,
        "raw_metrics": {
            "latest_price": rt["latest_price"],
            "change_pct": rt["change_pct"],
            "volume": rt["volume"],
            "ma60": ma60_r["ma60"],
            "ma60_position": ma60_r["position"],
            "ma60_distance_pct": ma60_r["distance_pct"],
            "ma60_trend": ma60_r["ma60_trend"],
            "volume_pattern": vol_r["pattern"],
            "vol_ratio": vol_r["vol_ratio"],
            "ma_alignment": alignment,
            "golden_eye": ge_r["golden_eye"],
            "golden_eye_stage": ge_r["stage"],
        },
        "llm_prompt_hint": {
            "use_as": "one input signal among many, not a final trading decision",
            "decision_rule": "If confidence_score < 0.55 or data_quality != 'ok', downweight this signal.",
            "combine_with": "news sentiment, fundamental data, portfolio risk limits",
        },
        "data_quality": {"status": "ok", "source": "akshare/eastmoney",
                         "freshness_seconds": 15 if _is_trading_hours() else 300},
        "human_view": {"summary": " ".join(parts)},
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "disclaimer": DISCLAIMER,
    }


@agent.skill(
    "batch_analyze",
    description="Batch analysis for multiple stocks. Returns ranked signals for watchlist scanning. "
                "批量分析多只股票，返回排序后的信号列表，适合盯盘和选股。",
    input_schema={"symbols": "list"},
    output_schema={"results": "list", "ranking": "list", "total": "int"},
    min_trust_tier=1,
    version="1.0.0",
)
def batch_analyze(symbols: list) -> dict:
    if not isinstance(symbols, list) or len(symbols) == 0:
        return {"error": "symbols must be a non-empty list", "results": [], "ranking": [], "total": 0}
    if len(symbols) > 20:
        return {"error": "max 20 symbols per batch", "results": [], "ranking": [], "total": 0}

    results = []
    for sym_raw in symbols:
        try:
            sym = _normalize_symbol(sym_raw)
            rt = _get_realtime_one(sym)
            df = _get_daily_kline(sym)
            mdf = _get_monthly_kline(sym)

            ma60_r = _analyze_ma60(df)
            vol_r = _analyze_volume_price(df)
            ge_r = _analyze_golden_eye(mdf)

            latest = df.iloc[-1]
            ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
            ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
            ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0
            ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else 0
            alignment = "bullish" if ma5 > ma10 > ma20 > ma60 else \
                        "bearish" if ma5 < ma10 < ma20 < ma60 else "neutral"

            signal = _compute_decision_signal(ma60_r, vol_r, ge_r, alignment)

            results.append({
                "symbol": sym,
                "name": rt["name"],
                "signal": signal["signal"],
                "action_bias": signal["action_bias"],
                "confidence_score": signal["confidence_score"],
                "signal_strength": signal["signal_strength"],
                "composite_score": signal["composite_score"],
                "risk_level": signal["risk_level"],
                "price": rt["latest_price"],
                "change_pct": rt["change_pct"],
                "ma60_position": ma60_r["position"],
                "volume_pattern": vol_r["pattern"],
                "golden_eye": ge_r["golden_eye"],
            })
        except Exception as e:
            results.append({
                "symbol": sym_raw,
                "name": None,
                "signal": "invalid",
                "error": str(e),
                "composite_score": -1,
            })

    # Sort by composite score descending
    valid = [r for r in results if r.get("composite_score", -1) >= 0]
    valid.sort(key=lambda x: x["composite_score"], reverse=True)
    ranking = [r["symbol"] for r in valid]

    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "batch_analyze",
        "results": results,
        "ranking": ranking,
        "total": len(results),
        "valid_count": len(valid),
        "as_of": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "disclaimer": DISCLAIMER,
    }


@agent.skill(
    "deep_analysis",
    description="LLM-powered deep analysis using proprietary courseware methodology. "
                "Combines real-time data + volume-price rules + news into a comprehensive research report. "
                "基于私有课件方法论的 LLM 深度分析，输出完整研究报告。"
                "THIS IS THE KILLER SKILL — the analysis methodology is private and never transmitted.",
    input_schema={"symbol": "str"},
    output_schema={"analysis": "str", "signal": "dict", "data_quality": "str"},
    min_trust_tier=2,
    version="1.0.0",
)
def deep_analysis(symbol: str) -> dict:
    sym = _normalize_symbol(symbol)
    rt = _get_realtime_one(sym)
    df = _get_daily_kline(sym)
    mdf = _get_monthly_kline(sym)
    now = datetime.now(timezone(timedelta(hours=8)))

    ma60_r = _analyze_ma60(df)
    vol_r = _analyze_volume_price(df)
    ge_r = _analyze_golden_eye(mdf)

    latest = df.iloc[-1]
    ma5 = float(latest['MA5']) if pd.notna(latest['MA5']) else 0
    ma10 = float(latest['MA10']) if pd.notna(latest['MA10']) else 0
    ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else 0
    ma60 = float(latest['MA60']) if pd.notna(latest['MA60']) else 0
    alignment = "bullish" if ma5 > ma10 > ma20 > ma60 else \
                "bearish" if ma5 < ma10 < ma20 < ma60 else "neutral"

    # Compute rule-engine signal
    signal = _compute_decision_signal(ma60_r, vol_r, ge_r, alignment)

    # Call LLM with courseware knowledge
    analysis_text = _build_llm_analysis(
        sym, rt, df, mdf, ma60_r, vol_r, ge_r, alignment)

    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "deep_analysis",
        "asset": {"market": "CN-A", "symbol": sym, "name": rt["name"]},
        "analysis": analysis_text,
        "decision_signal": signal,
        "raw_metrics": {
            "price": rt["latest_price"],
            "change_pct": rt["change_pct"],
            "ma60_position": ma60_r["position"],
            "ma60_distance_pct": ma60_r["distance_pct"],
            "volume_pattern": vol_r["pattern"],
            "vol_ratio": vol_r["vol_ratio"],
            "ma_alignment": alignment,
            "golden_eye": ge_r["golden_eye"],
        },
        "data_quality": "ok",
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        print("  ⚠ DEEPSEEK_API_KEY not set — deep_analysis skill will be limited")
        print("  Set it: export DEEPSEEK_API_KEY=sk-xxx")
        print()
    print(f"  fin-agent v{VERSION} — A-share Signal API for AI Agents")
    print(f"  Skills:")
    print(f"    stock_info         — basic data + MA (tier 0)")
    print(f"    ma60_position      — 60-day MA analysis (tier 1)")
    print(f"    volume_analysis    — volume-price patterns (tier 1)")
    print(f"    golden_eye         — monthly golden eye (tier 1)")
    print(f"    market_state       — comprehensive analysis (tier 1)")
    print(f"    decision_snapshot  — full pre-trade signal (tier 1)")
    print(f"    batch_analyze      — multi-stock scanning (tier 1)")
    print(f"    deep_analysis      — LLM research report [KILLER] (tier 2)")
    print(f"  Dashboard: http://localhost:8090")
    print()
    agent.serve(expose_skills="all")
