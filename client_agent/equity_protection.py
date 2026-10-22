"""
Stop-loss + target for equity buys, placed only AFTER the buy executes, at a % of the EXECUTED price.

Flow (EQUITY_SL_TARGET_ENABLED=true):
  1. The buy is placed as a plain order (live or AMO) and tracked here.
  2. The watcher polls Dhan for that order. Nothing is placed while it hasn't executed; if it is
     cancelled/rejected/expired without a fill, it is simply dropped.
  3. As soon as Dhan reports filled quantity, the executed price is read from Dhan
     (averageTradedPrice, or the order's trades), and a Dhan Forever Order in OCO mode is placed
     for exactly the newly filled quantity:  target = price +X%,  stop-loss = price -Y%.
     Partial fills are protected chunk by chunk.
Tracked orders are saved to a file so a restart (e.g. the daily 08:45 restart, or an AMO that
fills at the next open) picks up where it left off.
"""

import json
import logging
import math
import os
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

PENDING_FILE = os.environ.get("EQUITY_PENDING_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".pending_protection.json")
TERMINAL = {"TRADED", "CANCELLED", "REJECTED", "EXPIRED"}
TICK = 0.05
RETRY_AFTER_SEC = 60


def round_tick(price: float, mode: str) -> float:
    steps = price / TICK
    n = math.floor(steps + 1e-9) if mode == "down" else math.ceil(steps - 1e-9)
    return round(n * TICK, 2)


def levels(executed: float, sl_pct: float, target_pct: float, sl_limit_buffer_pct: float):
    """(stop-loss trigger, stop-loss limit, target) from the executed price, strictly around it."""
    sl = round_tick(executed * (1 - sl_pct / 100), "down")
    sl = min(sl, round_tick(executed - TICK, "down"))
    target = round_tick(executed * (1 + target_pct / 100), "up")
    target = max(target, round_tick(executed + TICK, "up"))
    sl_limit = round_tick(sl * (1 - sl_limit_buffer_pct / 100), "down")
    return max(sl, TICK), max(sl_limit, TICK), target


class ProtectionWatcher:
    """
    broker must provide:
      order_status(order_id) -> dict | None      (Dhan GET /v2/orders/{id})
      trades_avg_price(order_id) -> float | None  (Dhan GET /v2/trades/{id})
      place_forever_oco(record, qty, target, sl, sl_limit) -> dict | None  ({"order_id": ...} on success)
    """

    def __init__(self, broker, sl_pct: float, target_pct: float, sl_limit_buffer_pct: float = 1.0,
                 path: Optional[str] = None, dry_run: bool = False, clock: Callable[[], float] = time.time):
        self.broker = broker
        self.sl_pct, self.target_pct, self.sl_limit_buffer_pct = sl_pct, target_pct, sl_limit_buffer_pct
        self.path = path or PENDING_FILE
        self.dry_run = dry_run
        self.clock = clock
        self._lock = threading.Lock()  # track() runs on the main loop, check_once() in a worker thread
        self.pending: Dict[str, dict] = self._load()
        if self.pending:
            logger.info(f"[SL/TP] Resuming {len(self.pending)} order(s) awaiting execution: {list(self.pending)}")

    # --- persistence ----------------------------------------------------------------

    def _load(self) -> Dict[str, dict]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"[SL/TP] Could not read {self.path}: {e}")
            return {}

    def _save(self):
        with self._lock:
            snapshot = {k: dict(v) for k, v in self.pending.items()}
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, self.path)

    # --- tracking -------------------------------------------------------------------

    def track(self, order_id: str, symbol: str, security_id: str, segment: str, qty: int,
              dry_price: Optional[float] = None):
        """Register a placed buy. SL/target are placed only once it executes."""
        if self.dry_run:
            order_id = f"DRY-{int(self.clock() * 1000)}-{symbol}"
        record = {
            "order_id": order_id, "symbol": symbol, "security_id": str(security_id), "segment": segment,
            "qty": qty, "protected_qty": 0, "protected_value": 0.0, "placed_at": self.clock(),
            "dry_price": dry_price, "retry_at": 0,
        }
        with self._lock:
            self.pending[order_id] = record
        self._save()
        logger.info(f"[SL/TP] Tracking {symbol} order {order_id}: SL/target will be placed after it executes, "
                    f"at -{self.sl_pct:g}% / +{self.target_pct:g}% of the executed price.")

    def _status(self, rec: dict) -> Optional[dict]:
        if rec["order_id"].startswith("DRY-"):
            if not rec.get("dry_price"):
                return {"orderStatus": "TRADED", "filledQty": 0}
            return {"orderStatus": "TRADED", "filledQty": rec["qty"], "averageTradedPrice": rec["dry_price"]}
        return self.broker.order_status(rec["order_id"])

    def check_once(self):
        """One pass over all tracked orders (called by run() every few seconds)."""
        changed = False
        for order_id, rec in list(self.pending.items()):
            if rec.get("retry_at", 0) > self.clock():
                continue
            try:
                st = self._status(rec)
            except Exception as e:
                logger.warning(f"[SL/TP] Status check failed for {rec['symbol']} {order_id}: {e}")
                continue
            if not st:
                continue
            status = str(st.get("orderStatus", "")).upper()
            filled = int(st.get("filledQty") or 0)

            new_qty = filled - rec["protected_qty"]
            if new_qty > 0:
                changed |= self._protect(rec, st, filled, new_qty)

            if status in TERMINAL and rec["protected_qty"] >= filled:
                if filled == 0:
                    logger.info(f"[SL/TP] {rec['symbol']} order {order_id} {status} without executing: "
                                f"no SL/target placed.")
                else:
                    logger.info(f"[SL/TP] {rec['symbol']} order {order_id} done ({status}); "
                                f"{rec['protected_qty']} shares protected.")
                with self._lock:
                    self.pending.pop(order_id, None)
                changed = True
        if changed:
            self._save()

    def _protect(self, rec: dict, st: dict, filled: int, new_qty: int) -> bool:
        avg_all = float(st.get("averageTradedPrice") or 0) or (
            None if rec["order_id"].startswith("DRY-") else self.broker.trades_avg_price(rec["order_id"]))
        if not avg_all:
            logger.warning(f"[SL/TP] {rec['symbol']} executed {filled} but Dhan hasn't reported the executed "
                           f"price yet. Not placing SL/target until it does.")
            rec["retry_at"] = self.clock() + 5
            return True
        # Executed price of just the newly filled shares (partial fills are protected chunk by chunk)
        executed = round((avg_all * filled - rec["protected_value"]) / new_qty, 4)
        sl, sl_limit, target = levels(executed, self.sl_pct, self.target_pct, self.sl_limit_buffer_pct)

        if rec["order_id"].startswith("DRY-"):
            logger.info(f"[DRY RUN] Would place Forever OCO SELL {new_qty} x {rec['symbol']}: executed {executed} "
                        f"→ target {target} (+{self.target_pct:g}%), SL trigger {sl} (-{self.sl_pct:g}%, limit {sl_limit})")
            result = {"order_id": "DRY_RUN"}
        else:
            result = self.broker.place_forever_oco(rec, new_qty, target, sl, sl_limit)

        if not result or not result.get("order_id"):
            logger.error(f"[SL/TP] 🚨 {rec['symbol']}: {new_qty} shares executed @ {executed} are UNPROTECTED — "
                         f"Dhan rejected the SL/target order ({result}). Retrying in {RETRY_AFTER_SEC}s; "
                         f"place SL {sl} / target {target} manually if this repeats.")
            rec["retry_at"] = self.clock() + RETRY_AFTER_SEC
            return True

        rec["protected_qty"] += new_qty
        rec["protected_value"] += executed * new_qty
        rec["retry_at"] = 0
        logger.info(f"[SL/TP] ✅ {rec['symbol']}: {new_qty} shares executed @ {executed} (from Dhan) → "
                    f"SL {sl} (-{self.sl_pct:g}%) / target {target} (+{self.target_pct:g}%) placed at Dhan "
                    f"(Forever OCO {result['order_id']}).")
        return True

    async def run(self, is_market_open: Callable[[], bool]):
        """Poll forever: every 3s during market hours, every 60s otherwise (e.g. AMO waiting for the open)."""
        import asyncio

        while True:
            if self.pending:
                try:
                    await asyncio.to_thread(self.check_once)
                except Exception:
                    logger.exception("[SL/TP] watcher pass failed")
            await asyncio.sleep(3 if is_market_open() else 60)
