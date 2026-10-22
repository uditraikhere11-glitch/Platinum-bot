# Darvas Telegram Trading System

Python Telegram signal ingestion and equity/FnO execution workers. Internal communication uses a **shared local SQLite queue**, not WebSockets. The Telegram listener is now a dedicated process (`telegram_listener.py`); parsing/validation lives in `signal_pipeline.py`; `main.py` is only an optional queue health API and is not required for trading.

> **Deployment rule:** the Telegram listener, equity worker and FnO worker must run on the same GCloud VM and use the same absolute `DARVAS_SIGNAL_DB` file on persistent local storage. SQLite is not a cross-host queue. Do not place the database on NFS.

## Architecture

```text
Telegram source channels
      |
      v
telegram_listener.py (Telethon; dedicated systemd service)
      |
      v
signal_pipeline.py (parse, validate, stale checks, test/live policy)
      |                               |                          +--> TEST Telegram log chat (test messages + parser preview)
      v
SQLite queue (live signals only; explicit gate required)
      |                             
      +-----------------------+-----------------------+
      v                       v
client_agent/client.py       fno_agent/main.py
Equity executor              FnO executor + journal
      |                       |
      +-----------+-----------+
                  v
            Broker APIs / existing broker streams
```

The broker-specific execution functions, fill/protection logic and FnO journal are retained. The queue provides durable hand-off and deduplication, not exactly-once real-world broker execution. `CLAIMED` or `UNCERTAIN` signals must be reconciled against broker order/trade records before replay.

## Test channels and live channels

Set `CHANNEL_MAPPINGS` in the root `.env` to a JSON array. Each source channel has its own mode. **If `mode` is omitted, it defaults to `test`.**

```json
[
  {
    "telegram_channel_id": -1001111111111,
    "label": "test-equity",
    "mode": "test",
    "enable_trading": true,
    "enable_fno_trading": true,
    "discord_webhook_url": ""
  },
  {
    "telegram_channel_id": -1002222222222,
    "label": "live-equity",
    "mode": "live",
    "enable_trading": true,
    "enable_fno_trading": false,
    "discord_webhook_url": ""
  },
  {
    "telegram_channel_id": -1003333333333,
    "label": "live-fno",
    "mode": "live",
    "enable_trading": false,
    "enable_fno_trading": true,
    "discord_webhook_url": ""
  }
]
```

Replace sample IDs with actual source channel IDs. JSON must be a single line in `.env` (spaces are okay). Add the user account running Telethon to all source channels it needs to read.

- `mode="test"`: incoming messages are logged to the test Telegram log chat and parsed. Recognized signals produce a parser preview, but they are **never added to the live execution queue**. Test-mode messages are not forwarded to the production Notion, Discord or Google Sheets integrations.
- `mode="live"`: only the enabled equity/FnO signal types are parsed for execution. A signal enters SQLite only when `LIVE_TRADING_ENABLED=true`.
- `enable_trading` and `enable_fno_trading` remain per-channel allow flags. They do not override `mode` or the global gate.
- Live ingestion and executor live-mode are separate controls. A queued signal still cannot place a real order if its worker has `DRY_RUN=true`.

## Telegram log channels

Create a Telegram bot with [@BotFather](https://t.me/BotFather), add it to both log groups/channels, and grant permission to post. Configure in the root `.env`:

```dotenv
TELEGRAM_LOG_BOT_TOKEN=123456:replace_with_real_bot_token
TELEGRAM_TEST_LOG_CHAT_ID=-1004444444444
TELEGRAM_LIVE_LOG_CHAT_ID=-1005555555555
```

Input channels and log destinations are different things:

| Setting | Direction | Purpose |
|---|---|---|
| `CHANNEL_MAPPINGS` | Input | Telegram channels the Telethon user account reads. Each entry must specify a source label and `mode` (`test` or `live`). |
| `TELEGRAM_TEST_LOG_CHAT_ID` | Output | Receives test-source message summaries, parser previews, and all equity/FnO worker notifications while `DRY_RUN=true`. |
| `TELEGRAM_LIVE_LOG_CHAT_ID` | Output | Receives live-source parser/queue decisions and equity/FnO execution notifications when the relevant worker runs with `DRY_RUN=false`. |
| `TELEGRAM_LOG_BOT_TOKEN` | Output credential | Bot API token used by the listener and both execution workers to post log messages. |

The Bot must be a member of both output groups/channels and have permission to post. The bot token and destination IDs are read from the root `.env` by the shared logger, including when an equity/FnO worker uses its own `.env`. For FnO notifications, the mode-specific shared destination takes priority over legacy `FNO_TG_CHAT_ID`. Never commit real values.

**There are two independent safety gates:** source `mode="test"` always prevents queueing; source `mode="live"` still cannot queue unless `LIVE_TRADING_ENABLED=true`. Even with a queued signal, `DRY_RUN=true` means that worker must simulate rather than place a real order. A live-source parser decision can therefore go to the LIVE log chat while the dry-run worker result goes to the TEST log chat. This split is intentional. The logger never falls back from a missing TEST destination to the LIVE destination, to prevent test output contaminating the live log.

For the listener only, `LOG_GROUP_ID` remains as a legacy fallback for the live log destination using the Telethon user session. Prefer the Bot API token and two explicit destinations for consistent logs from every process.

## Guided chronological setup

For a new VM or a complete reconfiguration, use the master wizard from the repository root:

```bash
bash deploy/setup_vm.sh
```

Run it as the normal VM login user, **not** with `sudo`; the component installers use `sudo` only for system-level operations. The wizard runs the components in dependency order and calls out each environment file before that stage:

1. **Telegram listener + shared SQLite queue — root `.env`.** Enter/verify `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `CHANNEL_MAPPINGS`, `DARVAS_SIGNAL_DB`, `LIVE_TRADING_ENABLED=false`, the logging bot token, and TEST/LIVE log chat IDs. The Telethon source mapping must be configured before the listener can start.
2. **Equity executor — `client_agent/.env`.** Enter broker selection and credentials, investment amount, dry-run mode, and optional stop-loss/target settings. Keep `DRY_RUN=true`.
3. **FnO executor — `fno_agent/.env`.** Enter broker selection and credentials, capital per trade, maximum lots, expiry guard, dry-run mode, and protective exits. Keep `DRY_RUN=true` and start with a conservative lot cap.
4. **Final checks.** Confirm all three files use the same absolute `DARVAS_SIGNAL_DB` value, then inspect service status and logs.

The listener is a prerequisite for both executor setup scripts. If an executor is selected without the listener, the wizard includes the listener stage first. It refuses to proceed when the global `LIVE_TRADING_ENABLED` gate is enabled, when a selected executor already has `DRY_RUN=false`, or when the checkout ownership would prevent safe writes.

### Manual component setup

The component scripts can still be used individually when needed:

```bash
bash deploy/setup_telegram_listener.sh
bash client_agent/deploy/setup.sh
bash fno_agent/deploy/setup.sh
```

Run them in that order. Do not start the equity or FnO executor before the listener/queue and environment files are validated.

## Safe rollout

1. Keep `LIVE_TRADING_ENABLED=false` and `DRY_RUN=true` in both executor files through setup and testing.
2. Post a test signal in a `mode="test"` source channel. Confirm the TEST log receives the source-message and parser-preview entries, and `python signal_queue.py status` does not show a queue increase caused by the test message.
3. Validate a LIVE-source parser decision while `LIVE_TRADING_ENABLED=false`; the LIVE log should explicitly report that queueing is blocked. A LIVE source mapping can be added later; it is not required to validate the TEST source.
4. Before real orders, verify broker sessions, channel allow-flags, pending queue state, risk limits, and protective exits. Only then set `DRY_RUN=false` for the relevant worker and explicitly enable the global gate with `bot gate on` (type `ENABLE LIVE QUEUE`). `bot gate off` stops future live queue inserts but does not cancel signals already queued.

## Deployment services and common commands

```bash
sudo systemctl status darvas-telegram-listener trading-client fno-agent
sudo journalctl -u darvas-telegram-listener -f
sudo journalctl -u trading-client -f
sudo journalctl -u fno-agent -f
python signal_queue.py status
python signal_queue.py attention
```

The listener runs `telegram_listener.py` directly; there is no internal WebSocket listener/endpoint. `main.py` can optionally serve `/health` on loopback for queue health only, but it does not prove that the Telegram listener is connected and is not installed by default.

`bot status` shows service status and the global queue gate. `bot gate off` is the emergency **ingestion queue** switch; for an immediate trading stop, also stop the worker services and manage any open positions/orders at the broker.

## Queue safety semantics

Queue statuses: `PENDING`, `CLAIMED`, `COMPLETED`, `UNCERTAIN`.

- `PENDING`: persisted and waiting for the correct worker.
- `CLAIMED`: taken by a worker; a process crash can leave this for reconciliation.
- `COMPLETED`: executor function returned and result was recorded; it does not prove a fill.
- `UNCERTAIN`: an exception or missing outcome means the broker may have accepted an order; no automatic retry occurs.

```bash
python signal_queue.py status
python signal_queue.py attention
# Only after checking broker orders AND trades:
python signal_queue.py resolve --signal-id=-1001234567890:4521 --status completed --confirm-broker-reconciled
```

Use `--status pending` only after confirming that no matching order was accepted. A database deduplication rule cannot by itself guarantee exactly-once execution at an external broker.

## Config and secrets

- Root `.env`: Telegram input channels, global live-queue gate, optional integrations and Telegram log bot/destinations.
- `client_agent/.env`: broker and equity risk settings (`DRY_RUN=true` by default).
- `fno_agent/.env`: broker and FnO risk settings (`DRY_RUN=true` by default).
- Every service must use the same `DARVAS_SIGNAL_DB` path on this VM.
- Do not commit `.env`, Telethon session strings, broker tokens, service-account JSON keys or runtime database files.
- Prefer a restricted service user, persistent disk, systemd restart policies and Cloud Logging/Monitoring for operational visibility.

## Tests

Install dependencies from all three requirement files, then run `pytest tests/ -v`. Some integration tests require the optional Google SDKs/credentials to be present or mocked. Never use real broker credentials in automated tests.
