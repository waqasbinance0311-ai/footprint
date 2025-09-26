#!/usr/bin/env python3
"""
Liquidity Matrix Telegram Bot
--------------------------------
Save this file as liquidity_matrix_bot.py and run on a server/VPS.
Replace TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TD_API_KEY with your credentials.
This script schedules two alerts for each NY session day (Pakistan time):
  1) Pre-alert: 5 minutes BEFORE NY session start (user asked: 5:00 PM PK start -> pre-alert 16:55 PK)
  2) Post-open confirmation alert: ~5 minutes AFTER NY session start (17:05 PK)
It fetches recent 5m and 15m candles, computes liquidity pools, detects a simple "sweep + green close" pattern,
and sends a formatted trade plan (entry/SL/TP) via Telegram if conditions match the user's rules.

IMPORTANT:
- This is a template. For live market data, this uses TwelveData (TIME_SERIES endpoint) by default.
  You need to sign up for an API key at https://twelvedata.com/ (or change the data provider code).
- The detection logic is intentionally conservative and rule-based; tune thresholds to your needs.
- Run as a systemd service or via nohup/screen on a VPS to keep it running every day.
"""

import os
import time
import json
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------ CONFIG ------------------
TELEGRAM_TOKEN = "<YOUR_TELEGRAM_BOT_TOKEN>"    # e.g. "123456:ABC-DEF..."
TELEGRAM_CHAT_ID = "<YOUR_CHAT_ID>"             # e.g. "-1001234567890" or "123456789"
TD_API_KEY = "<YOUR_TWELVEDATA_API_KEY>"        # or set to None if you will plug another data provider

# Trading symbols (adjust to your broker's symbol style if necessary)
SYMBOL_XAU = "XAU/USD"   # TwelveData symbol for gold
SYMBOL_BTC = "BTC/USD"

# User preferences / session config (Pakistan time)
NY_SESSION_START_PK = dtime(hour=17, minute=0)     # 17:00 Pakistan time (UTC+5)
PRE_ALERT_MINUTES = 5     # minutes before session start
POST_ALERT_MINUTES = 5    # minutes after session start

# Strategy params
XAU_SL_PIPS = 20          # 20 pips for XAU (pip definition used as 0.01 typical)
BTC_SL_USD = 350          # default 300-400 range; using 350 as mid
RR = 4                    # 1:4 risk reward
MIN_VOLUME_THRESHOLD = None   # placeholder for providers with volume info

# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Telegram send error:", e)
        return None

def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 100):
    """Fetch time series from TwelveData. Returns list of candles newest-last."""
    if not TD_API_KEY:
        raise RuntimeError("TwelveData API key not set. Set TD_API_KEY in the config.")
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY
    }
    r = requests.get(base, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error or invalid response: {data}")
    # data["values"] is newest-first; convert to oldest-first list of dicts
    return list(reversed(data["values"]))

def parse_candles(raw_candles):
    """Convert TwelveData candle dicts to list of dicts with numeric fields and datetime objects."""
    out = []
    for c in raw_candles:
        out.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume") or 0)
        })
    return out

# ------------------ STRATEGY SIGNALS ------------------

def detect_sweep_and_green(candles_15m, lookback=6):
    """
    Detect simple 'sweep + green close' pattern on 15m timeframe.
    Criteria (conservative):
      - There exists a candle (C_sweep) within the last lookback candles whose low is
        lower than both previous and next candle lows (local min) and that candle has a long lower wick.
      - The candle after that (or the latest candle) must close green (close > open) and preferably engulf part of the sweep body.
    Returns dict with boolean and context fields.
    """
    if len(candles_15m) < lookback+2:
        return {"signal": False, "reason": "not_enough_data"}
    # use the last lookback+1 candles
    window = candles_15m[-(lookback+1):]
    lows = [c["low"] for c in window]
    # find potential sweep index (not the last candle)
    for i in range(1, len(window)-1):
        if window[i]["low"] < window[i-1]["low"] and window[i]["low"] < window[i+1]["low"]:
            # check wick: lower wick length relative to candle range
            body = abs(window[i]["open"] - window[i]["close"])
            lower_wick = window[i]["open"] - window[i]["low"] if window[i]["open"] > window[i]["close"] else window[i]["close"] - window[i]["low"]
            range_ = window[i]["high"] - window[i]["low"] if window[i]["high"] - window[i]["low"] > 0 else 1e-6
            if lower_wick / range_ > 0.4:  # wick is significant
                # check the next/latest candle is green
                next_candle = window[i+1]
                if next_candle["close"] > next_candle["open"]:
                    return {
                        "signal": True,
                        "sweep_candle": window[i],
                        "confirm_candle": next_candle,
                        "sweep_index_from_end": len(window)-(i+1)  # position relative to end
                    }
    return {"signal": False, "reason": "no_sweep_found"}

def detect_bullish_engulfing(candle_prev, candle_latest):
    """Simple bullish engulfing detector between two candles (dicts with open/high/low/close)."""
    return candle_prev["close"] < candle_prev["open"] and candle_latest["close"] > candle_latest["open"] and candle_latest["close"] > candle_prev["open"] and candle_latest["open"] < candle_prev["close"]

def compute_liquidity_zones(candles, lookback_hours=24):
    """Return simple liquidity zones as min lows and max highs over recent period (e.g., last N candles)."""
    # use last N candles (~lookback_hours worth depending on timeframe)
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {
        "recent_low": min(lows),
        "recent_high": max(highs),
        "last_close": candles[-1]["close"]
    }

# ------------------ TRADE PLAN BUILDER ------------------

def build_xau_trade_plan(latest_15m, latest_5m, detection):
    """
    Build XAU trade plan according to user's rules:
      - If sweep + green detected -> LONG
      - Entry at retest area (50% of wick) or small buffer above confirm candle open
      - SL: 20 pips below sweep low
      - TP: RR 1:4 targets
    """
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_low = sweep["low"]
    # pip conversion: assume 1 pip = 0.01 for XAU typical brokers
    pip_value = 0.01
    sl_price = sweep_low - (XAU_SL_PIPS * pip_value)
    # entry: a small buffer above confirm candle open or midpoint between sweep low and confirm close
    entry = max(confirm["open"] + 0.02, (confirm["close"] + sweep_low) / 2)
    rr_distance = entry - sl_price
    tp = entry + rr_distance * RR
    tp1 = entry + rr_distance * 1.0  # same as RR*? keep conservative
    return {
        "side": "LONG",
        "entry": round(entry, 3),
        "sl": round(sl_price, 3),
        "tp": round(tp, 3),
        "tp1": round(tp1, 3),
        "confidence": 0.80,
        "logic": "Sweep detected + green confirm on 15m"
    }

def build_btc_trade_plan(latest_15m, latest_5m, detection):
    """
    Similar builder for BTC. Use USD pip notion (1 pip = $1 here for simplicity).
    """
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_low = sweep["low"]
    sl_price = sweep_low - BTC_SL_USD
    entry = max(confirm["open"] + 1.0, (confirm["close"] + sweep_low) / 2)
    rr_distance = entry - sl_price
    tp = entry + rr_distance * RR
    return {
        "side": "LONG",
        "entry": round(entry, 2),
        "sl": round(sl_price, 2),
        "tp": round(tp, 2),
        "tp1": round(entry + rr_distance * 1.0, 2),
        "confidence": 0.75,
        "logic": "Sweep detected + green confirm on 15m"
    }

# ------------------ MAIN ALERT LOGIC ------------------

def get_and_analyze(symbol, interval_15m="15min", interval_5m="5min"):
    """Fetch candles and run detection, returning plan or None."""
    try:
        raw_15m = twelvedata_get_series(symbol, interval=interval_15m, outputsize=200)
        raw_5m = twelvedata_get_series(symbol, interval=interval_5m, outputsize=200)
    except Exception as e:
        return {"error": f"data_fetch_error: {e}"}

    candles_15m = parse_candles(raw_15m)
    candles_5m = parse_candles(raw_5m)
    detection = detect_sweep_and_green(candles_15m, lookback=6)
    liquidity = compute_liquidity_zones(candles_15m[-96:])  # last 24 hours approx (96 * 15m = 24h)
    result = {
        "symbol": symbol,
        "detection": detection,
        "liquidity": liquidity,
        "latest_15m": candles_15m[-1],
        "latest_5m": candles_5m[-1]
    }
    if detection.get("signal"):
        if "XAU" in symbol:
            result["plan"] = build_xau_trade_plan(candles_15m[-1], candles_5m[-1], detection)
        else:
            result["plan"] = build_btc_trade_plan(candles_15m[-1], candles_5m[-1], detection)
    return result

def format_plan_message(analysis):
    """Format the plan into an HTML message for Telegram."""
    if "error" in analysis:
        return f"⚠ Error fetching data: {analysis['error']}"
    if not analysis.get("plan"):
        return f"ℹ <b>{analysis['symbol']}</b>\nNo Sweep+Green confirmation found on 15m.\nLiquidity snapshot: Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\nLast close: {analysis['liquidity']['last_close']}"
    p = analysis["plan"]
    msg = f"<b>Pro SmartMoney Setup — {analysis['symbol']}</b>\n"
    msg += f"Logic: {p['logic']}\n"
    msg += f"Side: <b>{p['side']}</b>\n"
    msg += f"Entry: <code>{p['entry']}</code>\nSL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\nTP1: <code>{p.get('tp1')}</code>\nConfidence: {int(p['confidence']*100)}%\n\n"
    msg += f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
    msg += f"Latest 15m close: {analysis['latest_15m']['close']}\n"
    msg += "Trade Management:\n- TP1 hit -> move SL to break-even\n- TP2 hit -> scale out 50%\n- TP3 -> leave runner or full close\n"
    msg += "\n---\nPowered by Liquidity Matrix Bot"
    return msg

# ------------------ SCHEDULER TASKS ------------------

def job_pre_alert():
    """Send pre-session liquidity snapshot ~5 minutes before NY session start."""
    now = datetime.utcnow() + timedelta(hours=5)  # convert to Pakistan time naive (UTC+5)
    text = f"🕒 <b>Pre-NY Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning liquidity for XAU & BTC..."
    send_telegram_message(text)
    # quick snapshot: compute liquidity zones but do not require sweep confirmation
    try:
        x = get_and_analyze(SYMBOL_XAU, interval_15m="15min", interval_5m="5min")
        b = get_and_analyze(SYMBOL_BTC, interval_15m="15min", interval_5m="5min")
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Pre-alert error: {e}")

def job_post_open():
    """Post-session alert ~5 minutes after session open — look for sweep+confirm and send plan if found."""
    now = datetime.utcnow() + timedelta(hours=5)
    text = f"🕒 <b>NY Post-Open Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning for sweep+confirm on 15m..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU, interval_15m="15min", interval_5m="5min")
        b = get_and_analyze(SYMBOL_BTC, interval_15m="15min", interval_5m="5min")
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Post-open error: {e}")

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    # Compute UTC times for our PK times
    # PK is UTC+5. To schedule at PK 16:55 and 17:05 convert to UTC:
    pre_pk = datetime.combine(datetime.utcnow().date(), (datetime.min + timedelta(hours=16, minutes=55)).time())
    post_pk = datetime.combine(datetime.utcnow().date(), (datetime.min + timedelta(hours=17, minutes=5)).time())
    # Instead, add daily jobs at fixed hour/min in PK by converting to UTC:
    sched.add_job(job_pre_alert, 'cron', hour=(16-5), minute=55)   # PK 16:55 -> UTC 11:55
    sched.add_job(job_post_open, 'cron', hour=(17-5), minute=5)    # PK 17:05 -> UTC 12:05
    sched.start()
    print("Scheduler started. Pre-alert at PK 16:55, Post-open at PK 17:05")
    try:
        # keep the process alive
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

# ------------------ CLI / RUN ------------------

if __name__ == "__main__":
    # Basic checks
    if "<YOUR_TELEGRAM_BOT_TOKEN>" in TELEGRAM_TOKEN or "<YOUR_CHAT_ID>" in TELEGRAM_CHAT_ID:
        print("Please configure TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in the script before running.")
        print("If you want to test without scheduling, call get_and_analyze() manually.")
    else:
        print("Starting Liquidity Matrix Bot...")
        start_scheduler()
