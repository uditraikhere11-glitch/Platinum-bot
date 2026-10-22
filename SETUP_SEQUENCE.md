# Darvas setup sequence

Use the guided installer from `/opt/darvas`:

```bash
bash deploy/setup_vm.sh
```

Run as the regular Linux VM user, not `sudo`. It uses `sudo` internally only where system permissions are required.

## Stage 1 — Telegram listener and queue

File: `/opt/darvas/.env`

Enter/verify:

- `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`
- `CHANNEL_MAPPINGS` (one entry per signal source; each label unique; source mode `test` or `live`)
- `DARVAS_SIGNAL_DB` (absolute local path shared by all three services)
- `LIVE_TRADING_ENABLED=false`
- `TELEGRAM_LOG_BOT_TOKEN`
- `TELEGRAM_TEST_LOG_CHAT_ID` and `TELEGRAM_LIVE_LOG_CHAT_ID`

The source channel is where signals arrive. A log chat is where status/parser/executor notifications are sent. They are separate roles. If only one log group exists initially, the same chat ID can be used for both log destination settings; separate source mappings can be added later.

The listener setup starts the listener after validating the root environment and source mappings.

## Stage 2 — Equity executor

File: `/opt/darvas/client_agent/.env`

Enter/verify:

- `BROKER` and the corresponding broker credentials
- `TRADE_AMOUNT_INR` (and quantity fallback if needed)
- `DRY_RUN=true`
- `EQUITY_SL_TARGET_ENABLED`, stop-loss and target percentages if used
- `DARVAS_SIGNAL_DB` matches the root `.env`

The equity setup installs/configures the systemd service and checks that it is consuming from SQLite.

## Stage 3 — FnO executor

File: `/opt/darvas/fno_agent/.env`

Enter/verify:

- `BROKER` and the corresponding broker credentials
- `FNO_CAPITAL_PER_TRADE`, `FNO_MAX_LOTS`
- `DRY_RUN=true`
- expiry guard, market-hours and stop-loss/target settings
- `DARVAS_SIGNAL_DB` matches the root `.env` and equity `.env`

Start with a low lot cap while validating. The FnO setup installs/configures the service and checks its SQLite consumer.

## Stage 4 — Validation

1. Confirm the global queue gate is still disabled.
2. Confirm both executor `DRY_RUN` values are `true`.
3. Confirm all three `DARVAS_SIGNAL_DB` paths are identical.
4. Check `bot status`, `python signal_queue.py status`, and the journal for each service.
5. Send a TEST-group sample signal and verify parsing/logging without queue insertion or a broker order.

Do not enable live trading during initial setup. `LIVE_TRADING_ENABLED=false` stops live signals from entering the queue, while `DRY_RUN=true` makes each executor simulate orders; both safeguards should remain in place during validation.
