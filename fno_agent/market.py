"""Market-hours, holiday and tick-size helpers (all times IST)."""

import json
import logging
import math
import zoneinfo
from datetime import date, datetime, time
from typing import Iterable, Set

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
ENTRY_CUTOFF = time(15, 25)  # no fresh option entries in the last few minutes


def now_ist() -> datetime:
    return datetime.now(IST)


def load_holidays(path: str) -> Set[date]:
    """Load exchange holidays from {"holidays": ["YYYY-MM-DD", ...]}. Missing file → none."""
    try:
        with open(path) as f:
            return {date.fromisoformat(d) for d in json.load(f).get("holidays", [])}
    except FileNotFoundError:
        logger.warning(f"Holidays file {path} not found; only weekends are treated as closed.")
        return set()
    except Exception as e:
        logger.error(f"Could not read holidays file {path}: {e}")
        return set()


def is_market_open(now: datetime, holidays: Iterable[date] = ()) -> bool:
    now = now.astimezone(IST)
    if now.weekday() >= 5 or now.date() in set(holidays):
        return False
    return MARKET_OPEN <= now.time() <= ENTRY_CUTOFF


def round_to_tick(price: float, tick: float, mode: str = "nearest") -> float:
    """Round a price onto the exchange tick grid. mode: nearest | down | up."""
    tick = tick or 0.05
    steps = price / tick
    # Tolerate float noise (e.g. 104.99999999) before flooring/ceiling
    if mode == "down":
        n = math.floor(steps + 1e-9)
    elif mode == "up":
        n = math.ceil(steps - 1e-9)
    else:
        n = round(steps)
    return round(n * tick, 2)
