# Deploying the equity worker

The former Railway/WebSocket deployment guide is obsolete. The current implementation uses one local SQLite signal queue, so the Telegram listener and equity worker must run on the **same Google Cloud VM** (as must the FnO worker if enabled).

1. Set up the Telegram listener first: `bash deploy/setup_telegram_listener.sh`.
2. Set up the equity worker: `bash client_agent/deploy/setup.sh`.
3. Confirm both environments share the exact same `DARVAS_SIGNAL_DB` path.
4. Keep `DRY_RUN=true` and verify the queue before considering live use.

Useful checks:

```bash
sudo systemctl status darvas-telegram-listener trading-client
sudo journalctl -u trading-client -f
python signal_queue.py status
python signal_queue.py attention
```

`CLAIMED`/`UNCERTAIN` records require broker reconciliation; do not blindly replay them. See the root [`README.md`](../README.md) for complete configuration, recovery and deployment details.
