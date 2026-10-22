"""Broker interface used by the FnO executor. All methods are synchronous (run via asyncio.to_thread)."""

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

from fno_agent.instruments import Contract

logger = logging.getLogger(__name__)

# Normalised order states
OPEN, COMPLETE, CANCELLED, REJECTED = "OPEN", "COMPLETE", "CANCELLED", "REJECTED"
TERMINAL = {COMPLETE, CANCELLED, REJECTED}


class BrokerError(Exception):
    pass


class BrokerAuthError(BrokerError):
    pass


@dataclass
class OrderState:
    status: str  # OPEN | COMPLETE | CANCELLED | REJECTED
    filled_qty: int = 0
    avg_price: Optional[float] = None
    message: str = ""


class FnOBroker(ABC):
    name = "base"
    # True when place_entry() already attaches SL + target at the broker (Dhan Super Order).
    # False when protection is placed separately after the fill (Kite GTT OCO).
    protects_on_entry = False

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self._dry_orders: Dict[str, int] = {}
        self._dry_prices: Dict[str, Optional[float]] = {}

    @abstractmethod
    def connect(self) -> None:
        """Authenticate. Raises BrokerAuthError if credentials are unusable."""

    def refresh_session(self) -> None:
        """Proactive daily session refresh. Default: reconnect."""
        self.connect()

    @abstractmethod
    def ltp(self, contract: Contract) -> Optional[float]:
        """Last traded price, or None if unavailable."""

    @abstractmethod
    def place_entry(self, contract: Contract, qty: int, price: float,
                    stop_loss: Optional[float], target: Optional[float]) -> str:
        """
        Place a BUY LIMIT entry. Returns the broker order id.
        stop_loss=None and target=None means SL/target is disabled: a plain order, no protection.
        """

    @abstractmethod
    def entry_status(self, order_id: str, protected: bool = True) -> OrderState:
        """protected=False: the entry was placed without SL/target (Dhan: a plain order, not a super order)."""

    @abstractmethod
    def cancel_entry(self, order_id: str, protected: bool = True) -> None:
        ...

    def place_protection(self, contract: Contract, qty: int, stop_loss: float,
                         target: float, last_price: float) -> str:
        """Place a broker-side OCO (SL + target) SELL for qty. Returns its id."""
        raise NotImplementedError(f"{self.name} protects on entry")

    def executed_price(self, order_id: str, protected: bool = True) -> Optional[float]:
        """Average executed price of an entry, as reported by the broker (None if unknown)."""
        try:
            return self.entry_status(order_id, protected).avg_price
        except BrokerError:
            return None

    def modify_protection(self, order_id: str, stop_loss: float, target: float) -> None:
        """Move the SL/target legs of an entry placed with protection (Dhan super order)."""
        raise NotImplementedError(f"{self.name} places protection after the fill")

    # --- dry-run helpers -------------------------------------------------

    def _dry_order(self, kind: str, payload: dict, qty: int) -> str:
        order_id = f"DRY-{uuid.uuid4().hex[:10]}"
        self._dry_orders[order_id] = qty
        self._dry_prices[order_id] = payload.get("price")  # simulated fill at the limit price
        logger.info(f"[DRY RUN] {self.name} {kind} {order_id}: {payload}")
        return order_id

    def _dry_status(self, order_id: str) -> OrderState:
        qty = self._dry_orders.get(order_id, 0)
        return OrderState(COMPLETE if qty else CANCELLED, filled_qty=qty,
                          avg_price=self._dry_prices.get(order_id) if qty else None, message="simulated fill")
