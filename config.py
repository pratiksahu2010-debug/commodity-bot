"""
config.py
---------
MCX commodity alert bot. Uses the SAME Angel One broker account as the
NSE equity bots (just a different exchange segment: MCX instead of NSE).

IMPORTANT - read this before expecting 120 symbols: MCX only has ~23
actual distinct commodities. This bot monitors ALL of them, and pulls in
multiple contract months per commodity (near + next, configurable via
CONTRACTS_PER_COMMODITY) to increase coverage - giving somewhere around
40-46 actually-tradeable symbols depending on which contracts MCX
currently lists as active. That is the honest ceiling for this asset
class; there is no way to reach 120 real, distinct, currently-tradeable
MCX instruments without inventing symbols that don't exist.
"""

import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOT_ID = "COMMODITY1"
BOT_NAME = "MCX COMMODITY ALERT BOT"
TELEGRAM_DISPLAY_NAME = "@MCXCommodityAlertBot"

# ---------------------------------------------------------------------------
# Base commodity names - ALL of MCX's actual tradable commodities as of
# early 2026. These are NOT the tradeable symbols themselves (those have
# expiry dates baked in, e.g. "GOLD05DEC25FUT") - mcx_commodity_feed.py
# resolves the current active contract(s) for each of these dynamically,
# every day, so nothing here ever goes stale.
#
# Override by editing bots_config/base_commodities.json (a flat JSON
# array) - if present, it takes priority over this built-in list.
# ---------------------------------------------------------------------------
_BUILT_IN_BASE_COMMODITIES = [
    # Bullion
    "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC",
    # Base metals
    "COPPER", "ZINC", "ZINCMINI", "LEAD", "LEADMINI", "NICKEL", "ALUMINIUM", "ALUMINI",
    # Energy
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    # Agri
    "COTTON", "KAPAS", "MENTHAOIL", "CPO",
]

_BASE_COMMODITIES_PATH = BASE_DIR / "bots_config" / "base_commodities.json"
BASE_COMMODITIES = _BUILT_IN_BASE_COMMODITIES
try:
    with open(_BASE_COMMODITIES_PATH) as f:
        _override = json.load(f)
    if isinstance(_override, list) and len(_override) > 0:
        BASE_COMMODITIES = _override
        print(f"[config] Loaded {len(BASE_COMMODITIES)} base commodities from override file")
    else:
        print(f"[config] base_commodities.json empty/invalid, using built-in list ({len(BASE_COMMODITIES)})")
except (FileNotFoundError, json.JSONDecodeError):
    print(f"[config] base_commodities.json not found, using built-in list ({len(BASE_COMMODITIES)}) - this is fine")

# How many nearest unexpired contract months to track per commodity.
# 2 is a reasonable default (near + next month) - raising this increases
# your symbol count but trades further-dated contracts that are typically
# lower-volume and less liquid.
CONTRACTS_PER_COMMODITY = 2

# ---------------------------------------------------------------------------
# Strict rule thresholds. Commodities can gap and trend hard on
# geopolitical/macro news - bands here sit between the tighter NSE-equity
# bands and the wider crypto bands. Tune to taste; backtest before
# trusting with capital.
# ---------------------------------------------------------------------------
RSI_LONG_MIN, RSI_LONG_MAX = 40, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 60
ADX_MIN = 25
VWAP_MAX_DISTANCE_PCT = 2.5
CONFIDENCE_HIGH_PCT = 0.7
CONFIDENCE_MEDIUM_PCT = 1.8
VOLUME_LOOKBACK = 20
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
ADX_PERIOD = 14

SCORE_ALERT_THRESHOLD = 8
EARLY_SIGNAL_ENABLED = True
EARLY_SCORE_MIN = 6
EARLY_COOLDOWN_HOURS = 1
COOLDOWN_HOURS = 2

CANDLE_INTERVAL = "FIVE_MINUTE"
SCAN_INTERVAL_MINUTES = 15
SCAN_OFFSET_MINUTES = 9     # staggers this bot's scan start relative to other bots
                             # sharing your Telegram/reading attention, so 5 bots
                             # running simultaneously don't all alert in the same
                             # few seconds. Each bot in your 5-bot setup should use
                             # a DIFFERENT offset (e.g. 0, 3, 6, 9, 12) - see the
                             # README for the full staggering scheme.

# MCX trading hours are NOT uniform across commodities: bullion/base
# metals/energy typically trade 9:00 AM - 11:30 PM IST (11:55 PM during
# US non-DST months), while agri commodities close earlier (~9:00 PM).
# This uses one wide window covering most sessions - a scan outside a
# given commodity's actual hours just returns NO_DATA (logged, harmless),
# it will not crash or misfire. Narrow this per-commodity later if needed.
MARKET_OPEN = "09:00"
MARKET_CLOSE = "23:30"
MORNING_RESET_TIME = "08:55"       # also triggers daily contract re-resolution
DAILY_SUMMARY_TIME = "23:35"
ERROR_SUMMARY_TIME = "23:40"
TIMEZONE = "Asia/Kolkata"

MAX_CONSECUTIVE_FAILS_BROKEN = 3
MAX_CONSECUTIVE_FAILS_DISABLE = 5

SQLITE_PATH = str(BASE_DIR / "data" / "alerts.db")

# ---------------------------------------------------------------------------
# Telegram - set in Render's Environment tab, NEVER in this file
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# Angel One SmartAPI - SAME credentials as your NSE equity bots (MCX is
# just a different segment on the same broker account). Set in Render's
# Environment tab, NEVER in this file.
# ---------------------------------------------------------------------------
ANGEL_API_KEY = os.environ.get("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "").strip()
ANGEL_PIN = os.environ.get("ANGEL_PIN", "").strip()
ANGEL_TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "").strip()

ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
INSTRUMENT_CACHE_PATH = str(BASE_DIR / "data" / "instrument_master.json")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
PORT = int(os.environ.get("PORT", "10000"))


def validate_and_report():
    print("=" * 60)
    print(f"[config] BOT: {BOT_NAME}")
    print(f"[config] DRY_RUN: {DRY_RUN}")
    print(f"[config] Base commodities: {len(BASE_COMMODITIES)}, "
          f"up to {CONTRACTS_PER_COMMODITY} contracts each")
    checks = [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ("ANGEL_API_KEY", ANGEL_API_KEY), ("ANGEL_CLIENT_CODE", ANGEL_CLIENT_CODE),
        ("ANGEL_PIN", ANGEL_PIN), ("ANGEL_TOTP_SECRET", ANGEL_TOTP_SECRET),
    ]
    any_missing = False
    for name, value in checks:
        if value:
            print(f"[config]   {name}: SET (length {len(value)})")
        else:
            print(f"[config]   {name}: *** MISSING OR EMPTY *** - set this in Render > Environment")
            any_missing = True
    if any_missing and not DRY_RUN:
        print("[config] WARNING: one or more required env vars are missing and DRY_RUN is false.")
    print("=" * 60)


validate_and_report()
