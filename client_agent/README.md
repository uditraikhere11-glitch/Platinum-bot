# Equity execution worker

The equity worker reads `EQUITY` records from the shared SQLite signal queue and passes them to the existing Dhan/Kite broker execution functions. The internal WebSocket client has been removed.

**Required deployment condition:** run the Telegram listener and all workers on the same VM and configure the same absolute `DARVAS_SIGNAL_DB` path in the root `.env`, this directory's `.env`, and `fno_agent/.env` when FnO is enabled. The database should be on persistent local storage, not a network filesystem.

## Setup

```bash
bash deploy/setup.sh
```

The service starts in dry-run mode unless explicitly changed. Keep `DRY_RUN=true` while validating parser results and the end-to-end queue path. The queue marks a signal complete after `broker.handle_signal()` returns; that is not a guarantee that the broker filled the order. If the broker call raises, the row is `UNCERTAIN` and is not retried automatically.

Use `bot today equity` and `bot logs` for operational visibility. See the root [`README.md`](../README.md) for the unified GCloud VM setup, queue semantics and recovery instructions.
