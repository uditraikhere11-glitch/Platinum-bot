# FnO execution worker

The FnO worker polls `FNO` signals from the shared local SQLite queue and calls the existing `FnOExecutor`. The broker adapters and persistent `fno_agent/journal.py` remain the authority for FnO order tracking and recovery. The internal WebSocket consumer has been removed.

## Setup

```bash
bash fno_agent/deploy/setup.sh
```

Run the Telegram listener, equity worker and FnO worker on the same VM and configure the same absolute `DARVAS_SIGNAL_DB` path in all enabled services. Keep `DRY_RUN=true` while validating. The queue's `COMPLETED` state means the executor call returned; inspect the FnO journal for actual order/fill/protection state.

See the root [`README.md`](../README.md) for unified deployment instructions, queue semantics and handling of `CLAIMED` or `UNCERTAIN` records.
