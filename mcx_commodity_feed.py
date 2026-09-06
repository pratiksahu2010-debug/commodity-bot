"""
mcx_commodity_feed.py
----------------------
MCX commodity data via Angel One's SmartAPI - same broker/account you
already use for the NSE equity bots (MCX is just a different exchange
segment on the same API).

KEY DIFFERENCE FROM EQUITIES: a commodity futures symbol has an expiry.
"GOLD" isn't tradeable on its own - the actual instrument is something
like "GOLD05DEC25FUT", which stops trading at expiry and gets replaced by
the next month's contract. Hardcoding expiry-coded symbols (like the NSE
bots hardcode "RELIANCE.NS") would go stale within weeks.

Instead, this resolves the CURRENT active contract for each base
commodity dynamically, every day at the morning reset, by reading Angel
One's instrument master and picking the nearest unexpired contract(s) per
commodity. No expiry dates are ever hardcoded.

Requires the same smartapi-python + pyotp packages as the NSE bots.
Verify exact instrument-master field names (name/expiry/symbol/exch_seg)
against a real downloaded copy after first deploy - Angel One's schema
is used here based on commonly documented structure, not guaranteed
byte-for-byte stable.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import config

log = logging.getLogger("mcx_feed")

try:
    from SmartApi import SmartConnect
    import pyotp
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    log.warning("smartapi-python / pyotp not installed - live MCX data disabled.")


class MCXCommodityFeed:
    def __init__(self):
        self.smart_connect = None
        self.jwt_token = None
        self.feed_token = None
        self._logged_in = False
        self.instrument_map = {}       # resolved tradable symbol -> {"token":..., "base":...}
        self.live_ltp = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Auth - identical pattern to the NSE bots
    # ------------------------------------------------------------------ #
    def login(self) -> bool:
        if config.DRY_RUN:
            log.info("[MCX] DRY_RUN=true, skipping real login")
            return True
        if not SMARTAPI_AVAILABLE:
            log.error("[MCX] SmartApi SDK not installed, cannot log in")
            return False
        if not all([config.ANGEL_API_KEY, config.ANGEL_CLIENT_CODE,
                    config.ANGEL_PIN, config.ANGEL_TOTP_SECRET]):
            log.error("[MCX] Missing one or more ANGEL_* environment variables")
            return False
        try:
            self.smart_connect = SmartConnect(api_key=config.ANGEL_API_KEY)
            totp = pyotp.TOTP(config.ANGEL_TOTP_SECRET).now()
            session = self.smart_connect.generateSession(
                config.ANGEL_CLIENT_CODE, config.ANGEL_PIN, totp
            )
            if not session or not session.get("status"):
                log.error(f"[MCX] Login failed: {session}")
                return False
            self.jwt_token = session["data"]["jwtToken"]
            self.feed_token = self.smart_connect.getfeedToken()
            self._logged_in = True
            log.info("[MCX] Login successful")
            return True
        except Exception as e:
            log.error(f"[MCX] Login exception: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Contract resolution - THE key difference from the equity bots.
    # Downloads the instrument master, finds all MCX futures ("FUTCOM")
    # contracts for each base commodity name, and picks the nearest
    # N unexpired contracts per commodity (config.CONTRACTS_PER_COMMODITY).
    # Called once at boot AND again at the daily morning reset, so a
    # contract that expires overnight is automatically replaced by the
    # next one - no manual symbol updates ever needed.
    # ------------------------------------------------------------------ #
    def resolve_active_contracts(self, base_commodities: list) -> list:
        try:
            cache_path = config.INSTRUMENT_CACHE_PATH
            try:
                with open(cache_path) as f:
                    master = json.load(f)
                # Re-download once a day regardless of cache, since contracts
                # roll over - a day-old cache could miss a new listing.
                import os
                age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
                if age_hours > 20:
                    raise FileNotFoundError("cache stale, forcing re-download")
                log.info(f"[MCX] Using cached instrument master ({len(master)} rows)")
            except (FileNotFoundError, json.JSONDecodeError):
                log.info("[MCX] Downloading fresh instrument master...")
                resp = requests.get(config.ANGEL_INSTRUMENT_MASTER_URL, timeout=60)
                resp.raise_for_status()
                master = resp.json()
                import os
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(master, f)
                log.info(f"[MCX] Cached fresh instrument master ({len(master)} rows)")
        except Exception as e:
            log.error(f"[MCX] Failed to load instrument master: {e} - "
                      f"cannot resolve contracts, will use no symbols this cycle")
            return []

        today = datetime.now().date()
        by_commodity = {c: [] for c in base_commodities}

        for row in master:
            if row.get("exch_seg") != "MCX":
                continue
            if row.get("instrumenttype") not in ("FUTCOM", "FUTCUR"):
                continue
            name = (row.get("name") or "").upper()
            if name not in by_commodity:
                continue
            expiry_str = row.get("expiry", "")
            try:
                expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
            except (ValueError, TypeError):
                continue
            if expiry_date < today:
                continue  # already expired, skip
            by_commodity[name].append({
                "token": row["token"],
                "symbol": row.get("symbol", name),
                "expiry": expiry_date,
            })

        resolved_symbols = []
        self.instrument_map = {}
        skipped_no_contracts = []

        for base_name, contracts in by_commodity.items():
            contracts.sort(key=lambda c: c["expiry"])
            chosen = contracts[:config.CONTRACTS_PER_COMMODITY]
            if not chosen:
                skipped_no_contracts.append(base_name)
                continue
            for c in chosen:
                self.instrument_map[c["symbol"]] = {"token": c["token"], "base": base_name}
                resolved_symbols.append(c["symbol"])

        if skipped_no_contracts:
            log.warning(
                f"[MCX] No active unexpired contracts found for: {skipped_no_contracts}. "
                f"These may be delisted, renamed on MCX, or the instrument master "
                f"schema doesn't match what this code expects - verify manually if "
                f"this persists."
            )
        log.info(f"[MCX] Resolved {len(resolved_symbols)} active contracts across "
                 f"{len(base_commodities) - len(skipped_no_contracts)}/{len(base_commodities)} commodities")
        return resolved_symbols

    def token_for(self, symbol: str):
        entry = self.instrument_map.get(symbol)
        return entry["token"] if entry else None

    # ------------------------------------------------------------------ #
    # Historical candles (REST) - identical pattern to the NSE bots,
    # exchange="MCX" instead of "NSE".
    # ------------------------------------------------------------------ #
    def get_historical_candles(self, symbol: str, minutes_back: int = 900, retry: bool = True) -> pd.DataFrame:
        if config.DRY_RUN:
            return self._mock_candles(symbol)
        if not self._logged_in:
            log.error(f"[MCX] Not logged in - cannot fetch real candles for {symbol}")
            return pd.DataFrame()

        token = self.token_for(symbol)
        if not token:
            log.error(f"[MCX] No instrument token found for {symbol}")
            return pd.DataFrame()

        try:
            now = datetime.now()
            from_dt = now - timedelta(minutes=minutes_back)
            params = {
                "exchange": "MCX",
                "symboltoken": token,
                "interval": config.CANDLE_INTERVAL,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": now.strftime("%Y-%m-%d %H:%M"),
            }
            resp = self.smart_connect.getCandleData(params)
            if not resp or not resp.get("status"):
                if retry:
                    log.warning(f"[MCX] getCandleData failed for {symbol}, retrying once in 3s: {resp}")
                    time.sleep(3)
                    return self.get_historical_candles(symbol, minutes_back, retry=False)
                log.error(f"[MCX] getCandleData failed for {symbol} after retry: {resp}")
                return pd.DataFrame()

            rows = resp["data"]
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as e:
            if retry:
                log.warning(f"[MCX] Exception fetching candles for {symbol}, retrying once in 3s: {e}")
                time.sleep(3)
                return self.get_historical_candles(symbol, minutes_back, retry=False)
            log.error(f"[MCX] Exception fetching candles for {symbol} after retry: {e}")
            return pd.DataFrame()

    def _mock_candles(self, symbol: str) -> pd.DataFrame:
        import numpy as np
        n = 60
        base = 1000 + (hash(symbol) % 50000)
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        closes = base + np.cumsum(rng.normal(0, base * 0.003, n))
        highs = closes + rng.uniform(0, base * 0.003, n)
        lows = closes - rng.uniform(0, base * 0.003, n)
        opens = closes - rng.normal(0, base * 0.002, n)
        volumes = rng.integers(100, 20000, n)
        now = datetime.now()
        timestamps = [(now - timedelta(minutes=5 * (n - i))).isoformat() for i in range(n)]
        return pd.DataFrame({
            "timestamp": timestamps, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": volumes,
        })

    def get_live_ltp(self, symbol: str):
        with self._lock:
            return self.live_ltp.get(symbol)


feed = MCXCommodityFeed()
