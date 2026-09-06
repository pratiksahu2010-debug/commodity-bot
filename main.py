"""
main.py
-------
Standalone MCX commodity alert bot. The one structural difference from
the NSE bots: contracts are resolved dynamically (not hardcoded), and
re-resolved daily at the morning reset so expired contracts are replaced
automatically - see mcx_commodity_feed.resolve_active_contracts().

Run locally:  python main.py
Deploy:       gunicorn main:app  (see Procfile)
"""

import logging
import sys
import time
from datetime import datetime

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import telegram_notify
from storage import BotStorage
from cooldown_manager import CooldownManager
from mcx_commodity_feed import feed
from indicators import enrich_dataframe
from scoring import evaluate, should_alert, is_early_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

app = Flask(__name__)

# Storage is initialized with an EMPTY symbol list at first - actual
# tradeable symbols aren't known until contracts are resolved (they have
# expiry dates baked in). resolve_and_sync_symbols() populates it.
storage = BotStorage(config.SQLITE_PATH, [])
cooldown = CooldownManager(storage)


def resolve_and_sync_symbols():
    """Resolves current active contracts and registers any new ones in
    Settings (old expired-contract rows are simply left inactive/unused -
    harmless clutter, never deleted, so alert history stays intact)."""
    symbols = feed.resolve_active_contracts(config.BASE_COMMODITIES)
    for sym in symbols:
        storage._seed_symbols([sym])
    log.info(f"Symbol sync complete: {len(symbols)} active contracts registered")
    return symbols


def process_symbol(symbol: str):
    try:
        df_raw = feed.get_historical_candles(symbol)
        if df_raw.empty or len(df_raw) < 21:
            storage.log_error(symbol, "NO_DATA", "Insufficient candle data returned")
            storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                    config.MAX_CONSECUTIVE_FAILS_DISABLE)
            return

        df = enrich_dataframe(df_raw)
        live_ltp = feed.get_live_ltp(symbol)
        if live_ltp:
            df.iloc[-1, df.columns.get_loc("close")] = live_ltp

        result = evaluate(df)

        if result.reject_reason == "VWAP_UNAVAILABLE":
            storage.log_error(symbol, "VWAP_MISSING", "VWAP is N/A - alert skipped (mandatory rule)")
            return

        if should_alert(result):
            if not cooldown.can_alert(symbol):
                return
            message_id = telegram_notify.send_trade_alert(
                config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, result, symbol
            )
            storage.log_alert(
                symbol, result.direction, result.price, result.vwap, result.rsi,
                result.adx, result.ema9, result.ema21, result.volume,
                result.confidence, result.score, message_id, signal_type="CONFIRMED",
            )
            cooldown.start_cooldown(symbol)
            storage.reset_fail_counts_if_healthy(symbol)
            log.info(f"ALERT SENT: {symbol} {result.direction} score={result.score}/10")
            return

        if config.EARLY_SIGNAL_ENABLED and is_early_signal(result):
            if cooldown.can_alert_early(symbol):
                early_message_id = telegram_notify.send_early_signal(
                    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, result, symbol
                )
                storage.log_alert(
                    symbol, result.direction, result.price, result.vwap, result.rsi,
                    result.adx, result.ema9, result.ema21, result.volume,
                    result.confidence, result.score, early_message_id, signal_type="EARLY",
                )
                cooldown.start_early_cooldown(symbol)
                log.info(f"EARLY SIGNAL: {symbol} {result.direction} score={result.score}/10 (building)")
            storage.reset_fail_counts_if_healthy(symbol)
            return

        storage.reset_fail_counts_if_healthy(symbol)

    except Exception as e:
        log.exception(f"Unhandled error processing {symbol}")
        storage.log_error(symbol, "UNHANDLED_EXCEPTION", str(e))
        storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                config.MAX_CONSECUTIVE_FAILS_DISABLE)


def check_all_symbols():
    if not _is_market_hours():
        log.info("Outside market hours, skipping scan")
        return
    active_rows = storage.get_active_symbols()
    symbols = [r["symbol"] for r in active_rows if r["symbol"] in feed.instrument_map or config.DRY_RUN]
    log.info(f"Scanning {len(symbols)} resolved commodity contracts...")
    for i in range(0, len(symbols), 5):
        for sym in symbols[i:i + 5]:
            process_symbol(sym)
            time.sleep(1)


def _is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t = datetime.strptime(config.MARKET_OPEN, "%H:%M").time()
    close_t = datetime.strptime(config.MARKET_CLOSE, "%H:%M").time()
    return open_t <= now.time() <= close_t


# ---------------------------------------------------------------------- #
# Scheduled jobs
# ---------------------------------------------------------------------- #
def job_scan():
    check_all_symbols()


def job_morning_reset():
    """Resets cooldowns AND re-resolves contracts - this is how expired
    monthly contracts get replaced automatically, every trading day."""
    cooldown.reset_all()
    if not config.DRY_RUN:
        resolve_and_sync_symbols()
    telegram_notify.send_health_check(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                                       config.BOT_NAME, len(feed.instrument_map))
    log.info("Morning reset + contract re-resolution + health check complete")


def job_daily_summary():
    total = storage.count_alerts_today()
    total_early = storage.count_early_signals_today()
    top = storage.top_symbols_today()
    telegram_notify.send_daily_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                                        config.BOT_NAME, total, top, total_early=total_early)


def job_error_summary():
    total, breakdown = storage.errors_today_summary()
    telegram_notify.send_error_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, total, breakdown)


# ---------------------------------------------------------------------- #
# Startup
# ---------------------------------------------------------------------- #
def bootstrap():
    log.info(f"Booting {config.BOT_NAME}...")
    feed.login()

    if config.DRY_RUN:
        mock_symbols = [f"{c}MOCK" for c in config.BASE_COMMODITIES]
        for sym in mock_symbols:
            storage._seed_symbols([sym])
            feed.instrument_map[sym] = {"token": "0", "base": sym}
        log.info(f"[MCX] DRY_RUN=true, using {len(mock_symbols)} mock symbols instead of real contract resolution")
    else:
        resolve_and_sync_symbols()

    scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
    open_h, _ = map(int, config.MARKET_OPEN.split(":"))
    close_h, _ = map(int, config.MARKET_CLOSE.split(":"))

    scheduler.add_job(job_scan, CronTrigger(
        day_of_week="mon-fri", hour=f"{open_h}-{close_h}", minute=f"*/{config.SCAN_INTERVAL_MINUTES}"
    ), id="scan")

    reset_h, reset_m = map(int, config.MORNING_RESET_TIME.split(":"))
    scheduler.add_job(job_morning_reset, CronTrigger(day_of_week="mon-fri", hour=reset_h, minute=reset_m), id="morning_reset")

    sum_h, sum_m = map(int, config.DAILY_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_daily_summary, CronTrigger(day_of_week="mon-fri", hour=sum_h, minute=sum_m), id="daily_summary")

    err_h, err_m = map(int, config.ERROR_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_error_summary, CronTrigger(day_of_week="mon-fri", hour=err_h, minute=err_m), id="error_summary")

    scheduler.start()
    log.info("Scheduler started with 4 jobs (scan/reset+reresolve/summary/errors)")
    return scheduler


_scheduler = bootstrap()


# ---------------------------------------------------------------------- #
# HTTP endpoints
# ---------------------------------------------------------------------- #
@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_name": config.BOT_NAME,
        "base_commodities": len(config.BASE_COMMODITIES),
        "resolved_contracts": len(feed.instrument_map),
        "dry_run": config.DRY_RUN,
        "time": datetime.now().isoformat(),
    })


@app.route("/status")
def status():
    active_rows = storage.get_active_symbols()
    broken = [r["symbol"] for r in active_rows if r["status"] == "BROKEN"]
    total_errors, error_breakdown = storage.errors_today_summary()
    with storage._conn() as conn:
        recent_errors = [dict(r) for r in conn.execute(
            "SELECT * FROM error_log ORDER BY id DESC LIMIT 10"
        ).fetchall()]

    return jsonify({
        "bot_name": config.BOT_NAME,
        "is_market_hours_now": _is_market_hours(),
        "dry_run": config.DRY_RUN,
        "angel_logged_in": feed._logged_in,
        "resolved_contracts": list(feed.instrument_map.keys()),
        "telegram_token_configured": bool(config.TELEGRAM_TOKEN),
        "telegram_chat_id_configured": bool(config.TELEGRAM_CHAT_ID),
        "active_symbols": len(active_rows),
        "broken_symbols": broken,
        "alerts_sent_today": storage.count_alerts_today(),
        "early_signals_sent_today": storage.count_early_signals_today(),
        "errors_today_total": total_errors,
        "errors_today_by_type": error_breakdown,
        "most_recent_errors": recent_errors,
    })


@app.route("/trigger")
def manual_trigger():
    force = request.args.get("force", "false").lower() == "true"
    if force:
        log.info("Manual trigger with force=true, ignoring market hours")
        for r in storage.get_active_symbols():
            if r["symbol"] in feed.instrument_map or config.DRY_RUN:
                process_symbol(r["symbol"])
    else:
        check_all_symbols()
    return jsonify({"status": "scan triggered", "forced": force})


@app.route("/resolve_contracts")
def resolve_contracts_endpoint():
    """Manually force contract re-resolution without waiting for the
    morning reset - useful right after first deploy to verify it works."""
    if config.DRY_RUN:
        return jsonify({"status": "skipped", "reason": "DRY_RUN is true"})
    symbols = resolve_and_sync_symbols()
    return jsonify({"status": "resolved", "contracts": symbols})


@app.route("/telegram_test")
def telegram_test():
    import requests as _requests
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return jsonify({"sent": False, "reason": "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env var not set"}), 400
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": f"✅ Test message from {config.BOT_NAME}"}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        data = resp.json()
    except Exception as e:
        return jsonify({"sent": False, "reason": f"Request failed: {e}"}), 500
    return jsonify({"sent": bool(data.get("ok")), "http_status": resp.status_code, "telegram_response": data})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT)
