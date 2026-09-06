# MCX Commodity Alert Bot

Uses the SAME Angel One broker account as your NSE equity bots - MCX is
just a different exchange segment on the same API. No new broker signup
needed if you already have the NSE bots running.

Not investment advice - a technical screening tool. Commodities can gap
hard on geopolitical/macro news in ways equities usually don't. Backtest
before trusting this with real capital.

## ⚠️ About the "120 symbols" request

MCX only has **~23 actual distinct commodities** - there is no way to
reach 120 real, currently-tradeable MCX instruments. Padding the list
with invented contract codes would just produce broken symbols that
error out on every scan. This bot instead monitors **all 23 real MCX
commodities**, and pulls in the nearest 2 contract months per commodity
(configurable) to maximize honest coverage - typically landing around
**40-46 actually tradeable symbols** depending on which contracts MCX
currently lists as active.

## The key difference from your other bots: contracts expire

A stock ticker like `RELIANCE.NS` never changes. A commodity future does
- `GOLD` isn't directly tradeable; the real instrument is something like
`GOLD21SEP2026FUT`, which stops trading at expiry and gets replaced by
the next month's contract. Hardcoding an expiry-coded symbol (the way
the NSE bots hardcode tickers) would go stale within weeks.

**This bot never hardcodes an expiry.** `mcx_commodity_feed.py` reads
Angel One's instrument master, finds every unexpired MCX futures contract
for each of the 23 base commodities, and picks the nearest N (default 2)
automatically - at boot, and again every morning at market open. When a
contract expires, the next one is picked up on its own; no manual symbol
updates, ever.

## Setup

**1. Angel One credentials** - same ones as your NSE bots (`ANGEL_API_KEY`,
`ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`). If you're running
this alongside your NSE bots on the same Angel One account simultaneously,
re-read the session-limit caveat from the NSE bot READMEs - logging in
from multiple deployed bots on one account may invalidate each other's
sessions; confirm with Angel One support whether your account supports
concurrent API sessions.

**2. Telegram bot:** same as always - @BotFather → `/newbot` → save
token; message the bot once → get chat id via `getUpdates` or @userinfobot.

**3. Environment variables on Render:**
```
DRY_RUN=false
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
ANGEL_API_KEY=...
ANGEL_CLIENT_CODE=...
ANGEL_PIN=...
ANGEL_TOTP_SECRET=...
```
Start with `DRY_RUN=true` first - uses mock symbols and synthetic data,
no real Angel One calls, so you can verify the whole pipeline safely.

**4. Deploy:** push to its own repo → Render → New Web Service or
Blueprint → Build `pip install -r requirements.txt` → Start
`gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`.

**5. Verify - in this order:**
```
/health              - confirms service up, shows base commodity + resolved contract counts
/resolve_contracts   - forces contract resolution immediately (don't wait for tomorrow's reset)
/telegram_test       - sends a test message
/status              - full diagnostic incl. exactly which contracts resolved
/trigger?force=true  - runs a scan immediately, ignoring market hours
```

**Check `/resolve_contracts` right after your first real (non-DRY_RUN)
deploy** - this is the step most likely to need a look, since it depends
on Angel One's instrument master having the exact field names
(`name`, `expiry`, `instrumenttype`, `exch_seg`) this code expects. If a
commodity shows up in the "no active unexpired contracts found" warning
in the logs, that specific commodity's naming in the live master may
differ slightly from what's assumed here - check `/status`'s
`resolved_contracts` list against what you expect.

## Market hours (approximate - see config.py)

MCX trading hours are NOT uniform: bullion/base metals/energy trade
roughly 9:00 AM - 11:30 PM IST (11:55 PM during parts of the year due to
US daylight saving affecting international benchmark hours), while agri
commodities close earlier (~9:00 PM). This bot uses one wide window
(9:00 AM - 11:30 PM) covering most sessions. A scan outside a specific
commodity's actual hours just returns a harmless `NO_DATA` error, logged
and skipped - it won't crash or misfire. Narrow `MARKET_OPEN`/
`MARKET_CLOSE` per commodity later if you want tighter precision.

## Adjusting coverage

- `config.BASE_COMMODITIES` - the 23 real MCX commodities. Override via
  `bots_config/base_commodities.json` (flat JSON array) without touching code.
- `config.CONTRACTS_PER_COMMODITY` - default 2 (near + next month). Raise
  to 3 for more symbols, but further-dated contracts are typically
  lower-volume and less liquid - worth checking actual volume before
  trusting signals on them.

## Known limitations

- No persistent disk on Render's free/Starter tier - `data/alerts.db`
  and the cached instrument master reset on redeploy unless you add a
  paid disk.
- Instrument master schema assumptions (field names) are based on
  commonly documented Angel One structure, not guaranteed stable -
  verify via `/resolve_contracts` after deploy.
- No backtesting included - especially important here, since commodity
  volatility/gap behavior differs meaningfully from equities.
