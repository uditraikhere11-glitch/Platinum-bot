"""Broker implementations for the FnO agent."""

from fno_agent.brokers.base import FnOBroker


def create_broker(settings) -> FnOBroker:
    """Factory: BROKER=dhan|kite (zerodha accepted as an alias for kite)."""
    if settings.broker == "dhan":
        from fno_agent.brokers.dhan import DhanFnOBroker

        return DhanFnOBroker(
            client_id=settings.dhan_client_id,
            access_token=settings.dhan_access_token,
            pin=settings.dhan_pin,
            totp_secret=settings.dhan_totp_secret,
            dry_run=settings.dry_run,
        )
    if settings.broker in ("kite", "zerodha"):
        from fno_agent.brokers.kite import KiteFnOBroker

        return KiteFnOBroker(
            api_key=settings.kite_api_key,
            access_token=settings.kite_access_token,
            token_file=settings.kite_token_file,
            sl_limit_buffer_pct=settings.sl_limit_buffer_pct,
            dry_run=settings.dry_run,
        )
    raise ValueError(f"Unknown BROKER '{settings.broker}' (expected dhan or kite)")
