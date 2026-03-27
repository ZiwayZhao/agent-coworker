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
VERSION = "0.1.0"

DISCLAIMER = (
    "Technical feature analysis only. Not investment advice. "
    "Past performance does not indicate future results. "
    "仅为技术特征分析，不构成投资建议。"
)

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


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"  fin-agent v{VERSION}")
    print(f"  Skills: stock_info, ma60_position, volume_analysis, golden_eye, market_state")
    print(f"  Dashboard: http://localhost:8090")
    print(f"  Trust: stock_info=tier0 (public), others=tier1 (KNOWN)")
    print()
    agent.serve(expose_skills="all")
