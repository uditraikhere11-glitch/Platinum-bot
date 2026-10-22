# Migration notes — dedicated Telegram listener + test/live routing

## Architecture changes

- Added `telegram_listener.py` as the dedicated Telethon listener entry point and `deploy/darvas-telegram-listener.service` as its systemd service.
- Extracted message parsing, validation, source-mode policy and SQLite handoff into `signal_pipeline.py`.
- `main.py` is now an optional FastAPI health app for SQLite queue health only; it no longer owns or starts a Telethon session.
- Removed the old Uvicorn/WebSocket broadcaster service. Equity and FnO workers continue to poll the same local SQLite queue.
- Added per-channel `mode` (`test` or `live`) and a fail-closed `LIVE_TRADING_ENABLED=false` default.
- Test-mode messages are parsed/logged to the test log destination and are never enqueued. Live-mode messages are enqueued only if the channel's relevant enable flag and global live-queue gate are both enabled.
- Added shared Telegram Bot API logging via `TELEGRAM_LOG_BOT_TOKEN`, `TELEGRAM_TEST_LOG_CHAT_ID`, and `TELEGRAM_LIVE_LOG_CHAT_ID`. Listener can fall back to legacy `LOG_GROUP_ID`; workers use the shared bot logger.
- Equity executor results/uncertain outcomes are reported to the test log when running dry-run and to the live log when live. FnO notifier selects log destination according to `DRY_RUN`.
- Test-mode source messages are not forwarded into production Notion, Discord or Google Sheets integrations.
- Existing Dhan/Kite executor logic and FnO journal/protection logic are preserved.

## Required migration

1. Pull/extract the updated repository; keep actual credentials and runtime database outside source control.
2. In root `.env`, configure source `CHANNEL_MAPPINGS` with explicit `mode="test"` for all test channels and `mode="live"` only for intended production sources. Omitted mode defaults to `test`.
3. Run `bash deploy/setup_telegram_listener.sh`. It prompts for a Telegram Bot API token and separate TEST/LIVE output chat IDs, then validates `CHANNEL_MAPPINGS`. Add the bot to both output groups/channels with permission to post. Keep the token in `.env` only.
4. Keep `LIVE_TRADING_ENABLED=false` and each worker's `DRY_RUN=true` while verifying parser previews and routing.
5. The setup installs `darvas-telegram-listener.service`, which executes `telegram_listener.py` directly. It does not start FastAPI or WebSockets.
6. Set up/update equity and FnO services and verify all three use the same absolute `DARVAS_SIGNAL_DB` path.
7. Old `darvas-broadcaster.service` is obsolete. The new listener does not expose port 8000 or a WebSocket endpoint; if an old unit remains installed, stop/disable it to prevent a duplicate listener/session.
8. Live queueing requires `LIVE_TRADING_ENABLED=true` and `mode="live"` on the source mapping. Enabling a worker with `bot live equity|fno` does not, by itself, enable signal queueing. To enable queueing after broker and risk validation, run `bot gate on` and type `ENABLE LIVE QUEUE`; to block new queue inserts, run `bot gate off`.

## Recovery warning

SQLite delivers signals durably, but does not guarantee exactly-once broker orders. Inspect `python signal_queue.py attention`, then reconcile the broker order book/trade book and the executor journal before manually resolving a `CLAIMED` or `UNCERTAIN` signal.
