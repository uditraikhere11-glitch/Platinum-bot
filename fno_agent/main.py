"""
FnO agent entrypoint — runs 24/7 on the VM.

    python -m fno_agent.main [path/to/.env]      (default: fno_agent/.env)

Consumes FNO signals from the local SQLite queue, executes them, and runs daily jobs:
  08:00 IST  broker session refresh (Dhan TOTP login / Kite token-file reload)
  08:30 IST  instrument master refresh (retried every 10 min until it succeeds)
  09:00 IST  heartbeat notification
"""

import asyncio
import socket
import logging
import os
import sys
from datetime import date, time
from logging.handlers import RotatingFileHandler
from typing import Optional

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fno_agent.brokers import create_broker  # noqa: E402
from fno_agent.executor import FnOExecutor  # noqa: E402
from fno_agent.instruments import InstrumentResolver  # noqa: E402
from fno_agent.journal import Journal  # noqa: E402
from fno_agent.market import load_holidays, now_ist  # noqa: E402
from fno_agent.notifier import Notifier  # noqa: E402
from fno_agent.settings import AGENT_DIR, Settings  # noqa: E402
from signal_queue import (
    claim_next, complete_signal, initialize_queue, mark_uncertain, queue_stats,
)  # noqa: E402

logger = logging.getLogger("fno_agent")

SESSION_REFRESH_AT = time(8, 0)
INSTRUMENT_REFRESH_AT = time(8, 30)
HEARTBEAT_AT = time(9, 0)
INSTRUMENT_RETRY_SEC = 600


def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file = RotatingFileHandler(os.path.join(log_dir, "fno_agent.log"), maxBytes=10_000_000, backupCount=10)
    file.setFormatter(fmt)
    root.handlers = [stream, file]


class Agent:
    def __init__(self, settings: Settings):
        self.s = settings
        self.notifier = Notifier(settings.tg_bot_token, settings.tg_chat_id)
        self.broker = create_broker(settings)
        self.resolver = InstrumentResolver(settings.broker if settings.broker == "dhan" else "kite",
                                           settings.min_days_to_expiry)
        self.journal = Journal(settings.journal_path)
        self.executor = FnOExecutor(settings, self.broker, self.resolver, self.journal, self.notifier,
                                    holidays=load_holidays(settings.holidays_file))
        self.broker_ok = False
        self._tasks: set = set()
        self._session_refreshed_on: Optional[date] = None
        self._heartbeat_on: Optional[date] = None
        self._last_instrument_attempt = 0.0

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- startup ------------------------------------------------------------

    async def connect_broker(self) -> bool:
        try:
            await asyncio.to_thread(self.broker.connect)
            self.broker_ok = True
            return True
        except Exception as e:
            self.broker_ok = False
            logger.error(f"Broker connect failed: {e}")
            await self.notifier.send(f"🚨 {self.s.broker} login failed: {e}")
            return False

    async def load_instruments(self):
        while not await asyncio.to_thread(self.resolver.refresh):
            await self.notifier.send("🚨 Instrument master download failed; retrying in 60s")
            await asyncio.sleep(60)

    # --- daily jobs -----------------------------------------------------------

    async def scheduler(self):
        loop = asyncio.get_running_loop()
        while True:
            now = now_ist()
            today = now.date()
            try:
                if now.time() >= SESSION_REFRESH_AT and self._session_refreshed_on != today:
                    self._session_refreshed_on = today
                    try:
                        await asyncio.to_thread(self.broker.refresh_session)
                        self.broker_ok = True
                        logger.info("Daily broker session refresh done.")
                    except Exception as e:
                        self.broker_ok = False
                        await self.notifier.send(f"🚨 Daily {self.s.broker} session refresh failed: {e}")

                if (now.time() >= INSTRUMENT_REFRESH_AT and self.resolver.loaded_on != today
                        and loop.time() - self._last_instrument_attempt >= INSTRUMENT_RETRY_SEC):
                    self._last_instrument_attempt = loop.time()
                    if not await asyncio.to_thread(self.resolver.refresh, today):
                        await self.notifier.send("⚠️ Instrument refresh failed; using yesterday's master, "
                                                 f"retrying in {INSTRUMENT_RETRY_SEC // 60} min")

                if now.time() >= HEARTBEAT_AT and self._heartbeat_on != today and now.weekday() < 5:
                    self._heartbeat_on = today
                    counts = self.journal.count_by_status()
                    await self.notifier.send(
                        f"💓 alive | broker={self.s.broker} ok={self.broker_ok} dry_run={self.s.dry_run} "
                        f"| instruments={self.resolver.loaded_on} | budget ₹{self.s.capital_per_trade:,.0f} "
                        f"| journal {counts}")
            except Exception:
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(60)

    # --- durable signal queue -----------------------------------------------

    async def _process_queue_signal(self, signal_payload: dict, db_path: str):
        signal_id = signal_payload.get("signal_id", "")
        try:
            message_ts = signal_payload.get("message_ts")
            if message_ts:
                try:
                    parsed_ts = datetime.fromisoformat(str(message_ts).replace("Z", "+00:00"))
                    if parsed_ts.tzinfo is None:
                        parsed_ts = parsed_ts.replace(tzinfo=now_ist().tzinfo)
                    age_seconds = max(0.0, (now_ist().astimezone(parsed_ts.tzinfo) - parsed_ts).total_seconds())
                    if age_seconds > self.s.max_signal_age_sec:
                        logger.warning("Skipping expired FnO signal %s (age %.0fs > %ss)",
                                       signal_id, age_seconds, self.s.max_signal_age_sec)
                        await asyncio.to_thread(complete_signal, signal_id,
                                                {"executor_status": "IGNORED", "reason": "signal expired in queue",
                                                 "age_seconds": round(age_seconds)}, db_path)
                        await self.notifier.send(
                            f"⏱️ Ignored expired FnO signal {signal_id}: age={age_seconds:.0f}s "
                            f"(limit {self.s.max_signal_age_sec}s); no order submitted."
                        )
                        return
                except (TypeError, ValueError) as exc:
                    await asyncio.to_thread(mark_uncertain, signal_id, f"invalid message_ts: {exc}", db_path)
                    logger.error("FnO signal %s has invalid message_ts; marked UNCERTAIN", signal_id)
                    await self.notifier.send(
                        f"🚨 FnO signal {signal_id} has an invalid timestamp and is marked UNCERTAIN. "
                        f"Review the queue before replay: {exc}"
                    )
                    return

            latency = signal_payload.pop("_latency_ms", None)
            logger.info("Claimed FnO signal %s from '%s' (server metadata %s)",
                        signal_id, signal_payload.get("channel_label"), latency)
            # FnOExecutor has its own persistent journal and internally records order states.
            # Queue COMPLETED means handed to the executor, not necessarily filled.
            result = await self.executor.handle(signal_payload)
            await asyncio.to_thread(complete_signal, signal_id, {"executor_status": result}, db_path)
            logger.info("FnO signal %s handed to executor; status=%s", signal_id, result)
        except asyncio.CancelledError:
            # Preserve CLAIMED for operator reconciliation after a process interruption.
            raise
        except Exception as exc:
            await asyncio.to_thread(mark_uncertain, signal_id, repr(exc), db_path)
            logger.exception("FnO signal %s has an uncertain outcome; not retrying", signal_id)
            await self.notifier.send(
                f"🚨 FnO ORDER UNCERTAIN\nsignal={signal_id}\n{exc!r}\n"
                "Reconcile broker order/trade records before any manual replay."
            )

    async def listen(self):
        """Poll SQLite for FnO signals with bounded concurrency."""
        db_path = self.s.signal_db_path
        initialize_queue(db_path)
        worker_id = f"fno:{socket.gethostname()}:{os.getpid()}"
        logger.info("Consuming FNO signals from SQLite: %s", db_path)
        logger.info("Queue status at startup: %s", queue_stats(db_path))
        logger.info("FnO concurrent signal limit: %s", self.s.max_concurrent_signals)
        semaphore = asyncio.Semaphore(self.s.max_concurrent_signals)

        while True:
            await semaphore.acquire()
            signal_payload = await asyncio.to_thread(claim_next, "FNO", worker_id, db_path)
            if signal_payload is None:
                semaphore.release()
                await asyncio.sleep(self.s.queue_poll_seconds)
                continue

            async def process_and_release(payload):
                try:
                    await self._process_queue_signal(payload, db_path)
                finally:
                    semaphore.release()

            self._spawn(process_and_release(signal_payload))

    async def run(self):
        mode = "DRY RUN" if self.s.dry_run else "LIVE"
        logger.info(f"FnO agent starting | broker={self.s.broker} | {mode} | budget ₹{self.s.capital_per_trade:,.0f}")
        logger.info("Signal DB: %s", self.s.signal_db_path)
        logger.info("Queue concurrency=%s max_signal_age_sec=%s", self.s.max_concurrent_signals, self.s.max_signal_age_sec)
        if self.s.capital_per_trade <= 0:
            logger.warning("FNO_CAPITAL_PER_TRADE is 0: every signal will be skipped.")

        if await self.connect_broker() and now_ist().time() >= SESSION_REFRESH_AT:
            self._session_refreshed_on = now_ist().date()  # fresh session already; skip today's refresh
        await self.load_instruments()
        await self.notifier.send(f"🚀 FnO agent started ({self.s.broker}, {mode}, "
                                 f"budget ₹{self.s.capital_per_trade:,.0f}, broker_ok={self.broker_ok})")
        self._spawn(self.executor.reconcile())
        self._spawn(self.scheduler())
        await self.listen()


def main():
    env_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(AGENT_DIR, ".env")
    load_dotenv(env_path)
    settings = Settings.from_env()
    setup_logging(settings.log_dir)
    logger.info(f"Loaded env from {env_path}")
    try:
        asyncio.run(Agent(settings).run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
