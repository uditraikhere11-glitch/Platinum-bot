"""
Zerodha Kite broker for options.

Entry is a plain NRML LIMIT order; after the fill, each target tranche is protected by a
two-leg GTT OCO (stop-loss + target). Kite access tokens expire daily and need a manual
login; run `python -m fno_agent.kite_login fno_agent/.env` each morning to refresh the token file.
"""

import logging
from typing import Optional

from fno_agent.brokers.base import (
    CANCELLED, COMPLETE, OPEN, REJECTED, BrokerAuthError, BrokerError, FnOBroker, OrderState,
)
from fno_agent.instruments import Contract
from fno_agent.market import round_to_tick

logger = logging.getLogger(__name__)


def read_token_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


class KiteFnOBroker(FnOBroker):
    name = "kite"
    protects_on_entry = False

    def __init__(self, api_key: str, access_token: str = "", token_file: str = "",
                 sl_limit_buffer_pct: float = 5.0, dry_run: bool = True):
        super().__init__(dry_run)
        self.api_key = api_key
        self.env_token = access_token
        self.token_file = token_file
        self.sl_limit_buffer_pct = sl_limit_buffer_pct
        self.kite = None

    def connect(self) -> None:
        from kiteconnect import KiteConnect

        token = read_token_file(self.token_file) or self.env_token
        self.kite = KiteConnect(api_key=self.api_key)
        if not token:
            if self.dry_run:
                logger.warning("No Kite access token; dry run will skip LTP lookups.")
                return
            raise BrokerAuthError("No Kite access token. Run: python -m fno_agent.kite_login")
        self.kite.set_access_token(token)
        try:
            profile = self.kite.profile()
        except Exception as e:
            raise BrokerAuthError(f"Kite token invalid: {e}") from e
        logger.info(f"Kite session OK for {profile.get('user_id')}.")

    def _call(self, fn_name: str, *args, _retry: bool = True, **kwargs):
        """Call a KiteConnect method; on a token error re-read the token file once and retry."""
        from kiteconnect import exceptions as kex

        try:
            return getattr(self.kite, fn_name)(*args, **kwargs)
        except kex.TokenException as e:
            if _retry:
                logger.warning("Kite token rejected; reloading token file and retrying.")
                self.connect()
                return self._call(fn_name, *args, _retry=False, **kwargs)
            raise BrokerAuthError(f"Kite token rejected: {e}") from e
        except kex.KiteException as e:
            raise BrokerError(f"Kite {fn_name} failed: {e}") from e

    def ltp(self, contract: Contract) -> Optional[float]:
        if self.kite is None or not self.kite.access_token:
            return None
        key = f"{contract.exchange}:{contract.tradingsymbol}"
        try:
            return float(self._call("ltp", [key])[key]["last_price"])
        except Exception as e:
            logger.warning(f"Kite LTP unavailable for {key}: {e}")
            return None

    def place_entry(self, contract: Contract, qty: int, price: float,
                    stop_loss: Optional[float], target: Optional[float]) -> str:
        params = dict(
            variety="regular",
            exchange=contract.exchange,
            tradingsymbol=contract.tradingsymbol,
            transaction_type="BUY",
            quantity=int(qty),
            product="NRML",
            order_type="LIMIT",
            price=price,
            validity="DAY",
            tag="fnoagent",
        )
        if self.dry_run:
            return self._dry_order("entry", params, qty)
        order_id = self._call("place_order", **params)
        logger.info(f"Kite entry placed: {order_id}")
        return str(order_id)

    def entry_status(self, order_id: str, protected: bool = True) -> OrderState:
        if order_id.startswith("DRY-"):
            return self._dry_status(order_id)
        history = self._call("order_history", order_id)
        if not history:
            raise BrokerError(f"Kite order {order_id} has no history")
        last = history[-1]
        raw = str(last.get("status", "")).upper()
        status = {"COMPLETE": COMPLETE, "CANCELLED": CANCELLED, "REJECTED": REJECTED}.get(raw, OPEN)
        return OrderState(
            status=status,
            filled_qty=int(last.get("filled_quantity") or 0),
            avg_price=float(last["average_price"]) if last.get("average_price") else None,
            message=last.get("status_message") or raw,
        )

    def cancel_entry(self, order_id: str, protected: bool = True) -> None:
        if order_id.startswith("DRY-"):
            self._dry_orders[order_id] = 0
            return
        self._call("cancel_order", variety="regular", order_id=order_id)
        logger.info(f"Kite order {order_id} cancelled.")

    def place_protection(self, contract: Contract, qty: int, stop_loss: float,
                         target: float, last_price: float) -> str:
        sl_limit = round_to_tick(stop_loss * (1 - self.sl_limit_buffer_pct / 100), contract.tick_size, "down")
        leg = dict(exchange=contract.exchange, tradingsymbol=contract.tradingsymbol,
                   transaction_type="SELL", quantity=int(qty), order_type="LIMIT", product="NRML")
        params = dict(
            trigger_type="two-leg",
            tradingsymbol=contract.tradingsymbol,
            exchange=contract.exchange,
            trigger_values=[stop_loss, target],
            last_price=last_price,
            orders=[dict(leg, price=max(sl_limit, contract.tick_size)), dict(leg, price=target)],
        )
        if self.dry_run:
            return self._dry_order("gtt-oco", params, qty)
        resp = self._call("place_gtt", **params)
        trigger_id = str(resp.get("trigger_id"))
        logger.info(f"Kite GTT OCO {trigger_id}: {qty} x {contract.tradingsymbol} SL {stop_loss} / T {target}")
        return trigger_id
