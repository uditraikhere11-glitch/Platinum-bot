# Deploying the FnO worker

The former Railway/WebSocket deployment guide is obsolete. The current implementation uses a local SQLite signal queue, so the Telegram listener, equity worker and FnO worker must use the same database file on one Google Cloud VM.

1. Set up the Telegram listener first: `bash deploy/setup_telegram_listener.sh`.
2. Set up the FnO worker: `bash fno_agent/deploy/setup.sh`.
3. Confirm all enabled services have the exact same `DARVAS_SIGNAL_DB` path.
4. Keep `DRY_RUN=true` and use a conservative `FNO_MAX_LOTS` while testing.

Useful checks:

```bash
sudo systemctl status darvas-telegram-listener fno-agent
sudo journalctl -u fno-agent -f
python signal_queue.py status
python signal_queue.py attention
```

The queue never automatically replays `CLAIMED` or `UNCERTAIN` records. Check the broker order book/trade book and `fno_agent` journal before resolving one. See the root [`README.md`](../README.md) for complete instructions.
