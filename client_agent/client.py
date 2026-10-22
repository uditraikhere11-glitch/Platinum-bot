"""Equity execution worker consuming durable signals from the local SQLite queue.

Usage: python client.py [path/to/.env]

The Telegram listener and all workers must use the same local DARVAS_SIGNAL_DB.
"""

import os
import sys
import io
import math
import csv
import json
import uuid
import asyncio
import logging
import time as time_mod
import socket
import requests
from datetime import datetime, time, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from signal_queue import (
    claim_next, complete_signal, initialize_queue, mark_uncertain, default_database_path, queue_stats,
)
from services.telegram_log_service import send_telegram_log

try:  # imported as client_agent.client (tests, fno_agent)
    from client_agent.dhan_token import fresh_token, is_token_error
    from client_agent.equity_protection import ProtectionWatcher
except ImportError:  # run as `python client.py` from client_agent/
    from dhan_token import fresh_token, is_token_error
    from equity_protection import ProtectionWatcher

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("client_agent")

# --- Load Environment ---
try:
    from dotenv import load_dotenv
except ImportError:
    logger.error("python-dotenv is required. Install: pip install python-dotenv")
    sys.exit(1)

env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
load_dotenv(env_file, override=True)
logger.info(f"Loaded configuration from {env_file}")

# --- Durable local queue ---
SIGNAL_DB_PATH = os.environ.get("DARVAS_SIGNAL_DB", default_database_path())
QUEUE_POLL_SECONDS = max(0.1, float(os.environ.get("SIGNAL_QUEUE_POLL_SECONDS", "0.25")))

# Broker selection: "zerodha" or "dhan"
BROKER = os.environ.get("BROKER", "zerodha").lower().strip()

# Zerodha credentials
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN", "")

# Dhan credentials
DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_PIN = os.environ.get("DHAN_PIN", "")
DHAN_TOTP_SECRET = os.environ.get("DHAN_TOTP_SECRET", "")

# Execution filters
_RAW_ALLOWED_CHANNELS = os.environ.get("ALLOWED_CHANNELS", "").strip()
ALLOWED_CHANNELS = {c.strip().lower() for c in _RAW_ALLOWED_CHANNELS.split(",")} if _RAW_ALLOWED_CHANNELS else set()

# Capital Allocation
TRADE_AMOUNT_INR = int(os.environ.get("TRADE_AMOUNT_INR", "0"))
DEFAULT_QUANTITY = int(os.environ.get("DEFAULT_QUANTITY", "1"))

# Optional stop-loss + target, as fixed % of the EXECUTED price (placed only after the buy executes).
# Off by default. When on, EQUITY_SL_PCT is required (no default: it's your risk setting).
EQUITY_SL_TARGET_ENABLED = os.environ.get("EQUITY_SL_TARGET_ENABLED", "false").strip().lower() in ("true", "1", "yes")
EQUITY_SL_PCT = float(os.environ.get("EQUITY_SL_PCT", "0") or 0)
EQUITY_TARGET_PCT = float(os.environ.get("EQUITY_TARGET_PCT", "1") or 0)
# Stop-loss leg sells with a LIMIT this % below its trigger, so it fills in a fast fall
EQUITY_SL_LIMIT_BUFFER_PCT = float(os.environ.get("EQUITY_SL_LIMIT_BUFFER_PCT", "1") or 0)

class InstrumentMap:
    """
    In-memory lookup for Kite instrument tokens.
    
    Loads the Zerodha instruments CSV into a dict keyed by
    (exchange, tradingsymbol) → instrument_token for O(1) lookups.
    
    The instruments CSV can be downloaded from:
        https://api.kite.trade/instruments
    """

    def __init__(self, csv_path: str = "instruments.csv"):
        self._map: dict[str, int] = {}
        self._csv_path = csv_path
        self._loaded = False

    def load(self):
        """Load instruments CSV into memory."""
        if not os.path.exists(self._csv_path):
            logger.warning(
                f"Instruments file '{self._csv_path}' not found. "
                f"Download from https://api.kite.trade/instruments"
            )
            return

        import csv

        count = 0
        with open(self._csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row['exchange']}:{row['tradingsymbol']}"
                self._map[key] = int(row["instrument_token"])
                count += 1

        self._loaded = True
        logger.info(f"Loaded {count} instruments into memory.")

    def get_token(self, exchange: str, tradingsymbol: str) -> int | None:
        """Look up instrument token by exchange and trading symbol."""
        key = f"{exchange}:{tradingsymbol}"
        return self._map.get(key)

    def build_tradingsymbol(self, symbol: str, strike: int, option_type: str, expiry: str = "") -> str:
        """
        Build the Zerodha-format trading symbol for an F&O instrument.
        
        Example: NIFTY2492524500CE (NIFTY + YYMDD expiry + strike + CE/PE)
        
        Note: If expiry is not provided, the caller must supply the full
        tradingsymbol directly. This is a helper for common cases.
        """
        if expiry:
            return f"{symbol}{expiry}{strike}{option_type}"
        # Without expiry, return a partial symbol the caller can complete
        return f"{symbol}{strike}{option_type}"


class ZerodhaBroker:
    """
    Broker execution layer using Zerodha Kite Connect.
    
    In production, this class would use the `kiteconnect` SDK.
    For safety, all order placements are logged but can be toggled 
    between LIVE and DRY_RUN mode.
    """

    def __init__(self, api_key: str, access_token: str, dry_run: bool = True):
        self.api_key = api_key
        self.access_token = access_token
        self.dry_run = dry_run
        self._kite = None
        self._instruments = InstrumentMap()

    def connect(self):
        """Initialize Kite Connect client and load instruments."""
        try:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=self.api_key)
            self._kite.set_access_token(self.access_token)
            logger.info("Kite Connect client initialized.")
        except ImportError:
            logger.warning(
                "kiteconnect SDK not installed. Running in simulation mode. "
                "Install: pip install kiteconnect"
            )
            self._kite = None

        # Load instrument map
        self._instruments.load()

    def place_order(self, signal: dict) -> dict | None:
        """
        Place a BUY or SELL order based on the trade signal.
        
        Args:
            signal: TradeSignal dict with action, symbol, strike, option_type, etc.
            
        Returns:
            Order response dict from Kite, or simulated response in dry-run mode.
        """
        action = signal["action"]
        symbol = signal["symbol"]
        strike = signal.get("strike", 0)
        option_type = signal.get("option_type", "CE")
        order_type = signal.get("order_type", "MARKET")
        entry_price = signal.get("entry_price")

        # Build trading symbol (without expiry — would need expiry logic in production)
        tradingsymbol = self._instruments.build_tradingsymbol(symbol, strike, option_type)

        # Determine exchange — NFO for all F&O instruments
        exchange = "NFO"

        # Look up instrument token
        token = self._instruments.get_token(exchange, tradingsymbol)
        if token:
            logger.info(f"Instrument token for {exchange}:{tradingsymbol} = {token}")
        else:
            logger.warning(
                f"Instrument token not found for {exchange}:{tradingsymbol}. "
                f"Order will proceed without token validation."
            )

        # Map action to Kite transaction type
        transaction_type = "BUY" if action == "BUY" else "SELL"

        # --- Capital Allocation & Quantity Calculation ---
        # For Zerodha equity/F&O
        quantity = DEFAULT_QUANTITY
        if TRADE_AMOUNT_INR > 0:
            if order_type == "LIMIT" and entry_price and entry_price > 0:
                quantity = max(1, math.floor(TRADE_AMOUNT_INR / entry_price))
            else:
                # MARKET order - fetch live price (LTP) to calculate quantity
                try:
                    if self._kite is not None:
                        quote_key = f"{exchange}:{tradingsymbol}"
                        logger.info(f"Fetching live quote for {quote_key}...")
                        t0 = time_mod.perf_counter()
                        quote = self._kite.quote([quote_key])
                        t1 = time_mod.perf_counter()
                        
                        ltp = quote.get(quote_key, {}).get("last_price", 0)
                        if ltp and ltp > 0:
                            quantity = max(1, math.floor(TRADE_AMOUNT_INR / ltp))
                            logger.info(f"Fetched LTP: {ltp} in {(t1-t0)*1000:.1f}ms. Calculated QTY: {quantity}")
                        else:
                            logger.warning(f"Failed to extract LTP from quote {quote}. Using DEFAULT_QUANTITY.")
                    else:
                        logger.info("[DRY RUN] No kite client, using DEFAULT_QUANTITY.")
                except Exception as e:
                    logger.error(f"Failed to fetch live price via Kite: {e}. Using DEFAULT_QUANTITY.")

        # Build order params
        order_params = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": "MIS",  # Intraday — configurable
            "order_type": order_type,
            "validity": "DAY",
        }

        if order_type == "LIMIT" and entry_price is not None:
            order_params["price"] = entry_price

        # Stoploss order (if SL provided)
        sl = signal.get("sl")

        if self.dry_run or self._kite is None:
            logger.info(f"[DRY RUN] Would place order: {json.dumps(order_params, indent=2)}")
            if sl:
                logger.info(f"[DRY RUN] Would set SL at: {sl}")
            return {"order_id": "DRY_RUN", "status": "SIMULATED", "params": order_params}

        try:
            order_id = self._kite.place_order(
                variety="regular",
                **order_params,
            )
            logger.info(f"Order placed successfully. Order ID: {order_id}")

            # Place SL order if stoploss is specified
            if sl:
                sl_params = {
                    "tradingsymbol": tradingsymbol,
                    "exchange": exchange,
                    "transaction_type": "SELL" if transaction_type == "BUY" else "BUY",
                    "quantity": order_params["quantity"],
                    "product": "MIS",
                    "order_type": "SL",
                    "trigger_price": sl,
                    "validity": "DAY",
                }
                sl_order_id = self._kite.place_order(variety="regular", **sl_params)
                logger.info(f"SL order placed. Order ID: {sl_order_id}")

            return {"order_id": order_id, "status": "PLACED"}

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

    def exit_position(self, signal: dict) -> dict | None:
        """
        Exit an existing position by placing an opposite market order.
        
        For EXIT signals, we place a counter-trade at market price.
        """
        symbol = signal["symbol"]
        strike = signal.get("strike", 0)
        option_type = signal.get("option_type", "CE")

        tradingsymbol = self._instruments.build_tradingsymbol(symbol, strike, option_type)
        exchange = "NFO"

        # EXIT → place a SELL order (assuming we were long; reverse for shorts)
        # In production, query positions API to determine direction
        exit_params = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": "SELL",  # Assumes long position
            "quantity": 1,
            "product": "MIS",
            "order_type": "MARKET",
            "validity": "DAY",
        }

        if self.dry_run or self._kite is None:
            logger.info(f"[DRY RUN] Would exit position: {json.dumps(exit_params, indent=2)}")
            return {"order_id": "DRY_RUN_EXIT", "status": "SIMULATED", "params": exit_params}

        try:
            order_id = self._kite.place_order(variety="regular", **exit_params)
            logger.info(f"Exit order placed. Order ID: {order_id}")
            return {"order_id": order_id, "status": "EXITED"}
        except Exception as e:
            logger.error(f"Failed to exit position: {e}")
            return None

    def handle_signal(self, signal: dict) -> dict | None:
        """Route a trade signal to the appropriate handler."""
        action = signal.get("action", "").upper()

        if action in ("BUY", "SELL"):
            return self.place_order(signal)
        elif action == "EXIT":
            return self.exit_position(signal)
        elif action == "UPDATE":
            logger.info(f"UPDATE signal received (informational): {signal}")
            return {"status": "ACKNOWLEDGED", "action": "UPDATE"}
        else:
            logger.warning(f"Unknown action '{action}' in signal: {signal}")
            return None


# ============================================================================
# Dhan Broker
# ============================================================================

class DhanScripMap:
    """
    In-memory lookup for Dhan security IDs.

    Downloads the live Dhan scrip master CSV at startup and builds a
    dict keyed by trading symbol → {"id": security_id, "segment": exchange_segment}.
    Prioritizes NSE over BSE for equities available on both.
    """

    SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

    def __init__(self):
        self._equity_map: dict[str, dict] = {}
        self._fno_map: dict[str, dict] = {}
        self._loaded = False

    def load(self):
        """Download and parse the Dhan scrip master CSV."""
        logger.info("Downloading Dhan scrip master CSV...")
        try:
            response = requests.get(self.SCRIP_MASTER_URL, timeout=30)
            if response.status_code != 200:
                logger.error(f"Failed to download Dhan scrip master: HTTP {response.status_code}")
                return

            csv_data = csv.DictReader(io.StringIO(response.text))

            for row in csv_data:
                exchange = row.get("SEM_EXM_EXCH_ID", "")
                inst_type = row.get("SEM_INSTRUMENT_NAME", "")
                symbol = row.get("SEM_TRADING_SYMBOL", "")
                sec_id = row.get("SEM_SMST_SECURITY_ID", "")

                if not symbol or not sec_id:
                    continue

                # --- Equities ---
                if exchange in ("NSE", "BSE") and inst_type == "EQUITY":
                    clean_symbol = symbol.replace("-EQ", "").strip()
                    segment = "NSE_EQ" if exchange == "NSE" else "BSE_EQ"

                    # Prefer NSE over BSE
                    if clean_symbol not in self._equity_map or segment == "NSE_EQ":
                        self._equity_map[clean_symbol] = {
                            "id": sec_id,
                            "segment": segment,
                        }

                # --- F&O (Index Options on NSE) ---
                elif exchange == "NSE" and inst_type in ("OPTIDX", "FUTIDX"):
                    clean_symbol = symbol.strip()
                    self._fno_map[clean_symbol] = {
                        "id": sec_id,
                        "segment": "NSE_FNO",
                    }

            self._loaded = True
            logger.info(
                f"Loaded {len(self._equity_map)} equities and "
                f"{len(self._fno_map)} F&O instruments from Dhan."
            )

        except Exception as e:
            logger.error(f"Error loading Dhan scrip master: {e}")

    def get_equity(self, symbol: str) -> dict | None:
        """Look up equity scrip info by symbol."""
        return self._equity_map.get(symbol.upper())

    def get_fno(self, tradingsymbol: str) -> dict | None:
        """Look up F&O scrip info by exact trading symbol."""
        return self._fno_map.get(tradingsymbol)

    def build_fno_symbol(self, symbol: str, strike: int, option_type: str, expiry: str = "") -> str:
        """
        Build a Dhan-format F&O trading symbol.
        Example: NIFTY-2024-SEP-24500-CE (approximate — actual format depends on Dhan master).
        """
        if expiry:
            return f"{symbol}{expiry}{strike}{option_type}"
        return f"{symbol}{strike}{option_type}"


def _is_amo_window() -> bool:
    """
    Detect if current IST time is outside market hours (AMO window).
    AMO is active:
      - Weekends (Saturday/Sunday)
      - Before 9:15 AM IST or after 3:15 PM IST on weekdays
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)

    # Weekend
    if now.weekday() >= 5:
        return True

    market_start = time(9, 15)
    market_end = time(15, 15)

    return now.time() < market_start or now.time() >= market_end


class DhanBroker:
    """
    Broker execution layer using Dhan HQ REST API.

    Uses the Dhan v2 orders endpoint for both live and AMO orders.
    Downloads the scrip master at startup for instrument resolution.
    """

    ORDERS_URL = "https://api.dhan.co/v2/orders"

    def __init__(self, client_id: str, access_token: str, pin: str = "", totp_secret: str = "", dry_run: bool = True):
        self.client_id = client_id
        self.access_token = access_token
        self.pin = pin
        self.totp_secret = totp_secret
        self.dry_run = dry_run
        self._scrips = DhanScripMap()
        self.watcher = None  # ProtectionWatcher when EQUITY_SL_TARGET_ENABLED

    PROFILE_URL = "https://api.dhan.co/v2/profile"

    def connect(self):
        """Authenticate (if needed) and prepare for order placement."""
        if not self.access_token and self.pin and self.totp_secret:
            # Reuse the token shared with the FnO bot (a new login would invalidate its token)
            success = self._auto_login(rejected="")
            if success and not self._token_works():
                success = self._auto_login(rejected=self.access_token)
            if not success:
                logger.error("Initial Dhan auto-login failed. Exiting.")
                sys.exit(1)
        
        self._scrips.load()

    def _token_works(self) -> bool:
        """Cheap check that the current token is accepted (e.g. the shared one is not expired)."""
        try:
            resp = requests.get(self.PROFILE_URL, headers=self._build_headers(), timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Could not verify Dhan token: {e}")
            return True  # network hiccup: don't burn a login; an order failure will still self-heal

    def _auto_login(self, rejected: str = "") -> bool:
        """
        Get a working Dhan access token after `rejected` failed.

        Uses the token shared with the other bot if it's newer; otherwise logs in with PIN + TOTP.
        """
        token = fresh_token(rejected, self._generate_token)
        if token:
            self.access_token = token
            return True
        return False

    def _generate_token(self) -> str | None:
        """Programmatically generate a new Dhan access token using pyotp."""
        try:
            import pyotp
            logger.info("Generating Dhan TOTP for auto-login...")
            totp = pyotp.TOTP(self.totp_secret)
            current_totp = totp.now()

            url = f"https://auth.dhan.co/app/generateAccessToken?dhanClientId={self.client_id}&pin={self.pin}&totp={current_totp}"
            response = requests.post(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if "accessToken" in data:
                    logger.info("Successfully generated Dhan access token via auto-login!")
                    return data["accessToken"]
                else:
                    logger.error(f"Dhan auto-login failed: {data}")
                    return None
            else:
                logger.error(f"Dhan auto-login HTTP {response.status_code}: {response.text}")
                return None
        except ImportError:
            logger.error("pyotp is required for auto-login. Please run: pip install pyotp")
            return None
        except Exception as e:
            logger.error(f"Dhan auto-login error: {e}")
            return None

    def _build_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token,
        }

    def _place_dhan_order(self, payload: dict, is_retry: bool = False, url: str | None = None) -> dict | None:
        """Send an order to Dhan v2 API."""
        url = url or self.ORDERS_URL
        if self.dry_run:
            logger.info(f"[DRY RUN] Would send Dhan order: {json.dumps(payload, indent=2)}")
            return {"order_id": "DRY_RUN", "status": "SIMULATED", "params": payload}

        try:
            response = requests.post(
                url,
                headers=self._build_headers(),
                json=payload,
                timeout=10,
            )
            result = response.json()

            if response.status_code == 200 and "orderId" in result:
                order_id = result["orderId"]
                order_status = result.get("orderStatus", "QUEUED")
                logger.info(f"Dhan order placed. ID: {order_id}, Status: {order_status}")
                return {"order_id": order_id, "status": order_status}
            
            # --- 24/7 Self-Healing Auth ---
            # Token expired or replaced (401/403, DH-901, DH-906 "Invalid Token"): refresh and retry once.
            elif is_token_error(response.status_code, result):
                if not is_retry and self.pin and self.totp_secret:
                    logger.warning(f"Dhan token rejected (HTTP {response.status_code}: {result}). Triggering mid-trade auto-login...")
                    if self._auto_login(rejected=self.access_token):
                        logger.info("Auto-login succeeded! Retrying order...")
                        return self._place_dhan_order(payload, is_retry=True, url=url)
                    else:
                        logger.error("Mid-trade auto-login failed. Order aborted.")
                
                logger.error(f"Dhan order rejected (Unauthorized): {result}")
                return {"status": "REJECTED_UNAUTHORIZED", "response": result}

            else:
                logger.error(f"Dhan order rejected: {result}")
                return {"status": "REJECTED", "response": result}

        except Exception as e:
            logger.error(f"Dhan API error: {e}")
            return None

    # --- API helpers used by the SL/target watcher ------------------------------------

    def _api(self, method: str, path: str, body: dict | None = None, is_retry: bool = False):
        """Dhan v2 call with the same token self-healing as orders. Returns (status_code, json)."""
        response = requests.request(method, f"https://api.dhan.co/v2{path}", headers=self._build_headers(),
                                    json=body, timeout=10)
        try:
            data = response.json() if response.text else {}
        except ValueError:
            data = {"raw": response.text}
        if is_token_error(response.status_code, data) and not is_retry and self.pin and self.totp_secret:
            if self._auto_login(rejected=self.access_token):
                return self._api(method, path, body, is_retry=True)
        return response.status_code, data

    def order_status(self, order_id: str) -> dict | None:
        """Order as Dhan reports it (orderStatus, filledQty, averageTradedPrice)."""
        code, data = self._api("GET", f"/orders/{order_id}")
        if isinstance(data, list):
            data = data[0] if data else {}
        return data if code == 200 and isinstance(data, dict) and data.get("orderId") else None

    def trades_avg_price(self, order_id: str) -> float | None:
        """Quantity-weighted executed price from the order's trades (fallback for averageTradedPrice)."""
        code, data = self._api("GET", f"/trades/{order_id}")
        trades = data if isinstance(data, list) else [data]
        qty = sum(int(t.get("tradedQuantity") or 0) for t in trades if isinstance(t, dict))
        value = sum(float(t.get("tradedPrice") or 0) * int(t.get("tradedQuantity") or 0)
                    for t in trades if isinstance(t, dict))
        return round(value / qty, 4) if code == 200 and qty else None

    def place_forever_oco(self, rec: dict, qty: int, target: float, sl: float, sl_limit: float) -> dict | None:
        """SELL Forever Order (OCO) protecting `qty` executed shares: target leg + stop-loss leg."""
        payload = {
            "dhanClientId": self.client_id,
            "correlationId": str(uuid.uuid4())[:30],
            "orderFlag": "OCO",
            "transactionType": "SELL",
            "exchangeSegment": rec["segment"],
            "productType": "CNC",
            "orderType": "LIMIT",
            "validity": "DAY",
            "securityId": str(rec["security_id"]),
            "quantity": int(qty),         # target leg
            "price": target,
            "triggerPrice": target,
            "price1": sl_limit,           # stop-loss leg: triggers at sl, sells with a limit just below
            "triggerPrice1": sl,
            "quantity1": int(qty),
            "disclosedQuantity": 0,
        }
        logger.info(f"Placing Dhan Forever OCO: {json.dumps(payload)}")
        code, data = self._api("POST", "/forever/orders", payload)
        if code == 200 and data.get("orderId") and str(data.get("orderStatus", "")).upper() != "REJECTED":
            return {"order_id": data["orderId"], "status": data.get("orderStatus")}
        logger.error(f"Dhan Forever OCO rejected (HTTP {code}): {data}")
        return None

    def place_order(self, signal: dict) -> dict | None:
        """
        Place a BUY equity order on Dhan based on the trade signal.
        
        This is BUY-ONLY equity mode matching the reference trading bot:
        - Looks up stock_symbol in equity scrip map
        - Uses CNC product type (cash-and-carry for delivery)
        - 1% price buffer for LIMIT orders, rounded to tick size
        - Auto-detects AMO windows
        """
        stock_symbol = signal.get("stock_symbol", "")
        order_type = signal.get("order_type", "MARKET")
        entry_price = signal.get("entry_price", 0)

        # Resolve equity instrument
        scrip_info = self._scrips.get_equity(stock_symbol)

        if not scrip_info:
            logger.error(f"Scrip not found for '{stock_symbol}'. Cannot place order.")
            return None

        scrip_id = scrip_info["id"]
        segment = scrip_info["segment"]

        use_amo = _is_amo_window()

        # Strictly CNC for equities
        product_type = "CNC"

        # Price handling: for LIMIT orders, add 1% buffer and round to tick size (0.05)
        if order_type == "LIMIT" and entry_price > 0:
            raw_price = round((entry_price * 1.01) / 0.05) * 0.05
            price = float(f"{raw_price:.2f}")
        else:
            order_type = "MARKET"
            price = 0.0

        # --- Live price (MARKET orders): needed for quantity ---
        ltp = None
        if order_type == "MARKET" and TRADE_AMOUNT_INR > 0:
            ltp = self._fetch_ltp(stock_symbol)

        # --- Capital Allocation & Quantity Calculation ---
        quantity = DEFAULT_QUANTITY
        if TRADE_AMOUNT_INR > 0:
            if order_type == "LIMIT" and entry_price > 0:
                quantity = max(1, math.floor(TRADE_AMOUNT_INR / entry_price))
            elif ltp:
                quantity = max(1, math.floor(TRADE_AMOUNT_INR / ltp))
                logger.info(f"Calculated QTY from LTP {ltp}: {quantity}")
            else:
                quantity = max(1, DEFAULT_QUANTITY)
                logger.warning(f"No live price. Using DEFAULT_QUANTITY: {quantity}")

        payload = {
            "dhanClientId": self.client_id,
            "correlationId": str(uuid.uuid4())[:30],
            "transactionType": "BUY",
            "exchangeSegment": segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(scrip_id),
            "quantity": quantity,
            "price": price,
            "disclosedQuantity": 0,
            "triggerPrice": 0.0,
            "afterMarketOrder": use_amo,
        }

        if use_amo:
            payload["amoTime"] = "OPEN"
            logger.info(f"[🌙 AMO] Placing after-market order for {stock_symbol}")
        else:
            logger.info(f"[☀️ LIVE] Placing live order for {stock_symbol}")

        logger.info(
            f"Executing → Symbol: {stock_symbol} | Segment: {segment} "
            f"| QTY: {quantity} | Type: {order_type} | Price: {price}"
        )

        result = self._place_dhan_order(payload)

        # --- Optional SL/target: placed by the watcher only AFTER this buy executes, at a % of the
        # executed price Dhan reports (AMO orders included: protected once they fill at the open) ---
        if self.watcher and result and result.get("order_id") and result.get("status") != "REJECTED":
            self.watcher.track(str(result["order_id"]), stock_symbol, scrip_id, segment, quantity,
                               dry_price=(price or ltp) if self.dry_run else None)
        return result

    def _fetch_ltp(self, stock_symbol: str) -> float | None:
        """Live price from Yahoo Finance (NSE), or None."""
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_symbol}.NS?interval=1d&range=1d"
            headers = {"User-Agent": "Mozilla/5.0"}
            logger.info(f"Fetching live quote from Yahoo Finance for {stock_symbol}.NS...")
            t0 = time_mod.perf_counter()
            resp = requests.get(url, headers=headers, timeout=5)
            t1 = time_mod.perf_counter()
            if resp.status_code == 200:
                data = resp.json()
                ltp = data.get("chart", {}).get("result", [{}])[0].get("meta", {}).get("regularMarketPrice", 0)
                if ltp and ltp > 0:
                    logger.info(f"Fetched LTP: {ltp} in {(t1-t0)*1000:.1f}ms")
                    return float(ltp)
                logger.warning("LTP not found in YF response.")
            else:
                logger.warning(f"YF returned {resp.status_code}.")
        except Exception as e:
            logger.error(f"Failed to fetch live price via YF: {e}")
        return None

    def handle_signal(self, signal: dict) -> dict | None:
        """
        Handle an incoming equity trade signal.
        
        In BUY-ONLY equity mode, all signals from the server are BUY orders.
        The server-side filters already block sell/exit/index/F&O signals.
        """
        stock_symbol = signal.get("stock_symbol", "UNKNOWN")
        logger.info(f"Processing equity signal for {stock_symbol}")
        return self.place_order(signal)


def _create_broker(dry_run: bool):
    """
    Factory function: create the broker based on BROKER env var.
    Returns a broker instance with a .handle_signal(signal) method.
    """
    if BROKER == "dhan":
        if not DHAN_CLIENT_ID:
            logger.error("Dhan credentials missing. Set DHAN_CLIENT_ID.")
            sys.exit(1)
            
        if not DHAN_ACCESS_TOKEN and not (DHAN_PIN and DHAN_TOTP_SECRET):
            logger.error("Dhan token missing and no auto-login credentials (DHAN_PIN, DHAN_TOTP_SECRET) found.")
            sys.exit(1)
            
        broker = DhanBroker(
            client_id=DHAN_CLIENT_ID,
            access_token=DHAN_ACCESS_TOKEN,
            pin=DHAN_PIN,
            totp_secret=DHAN_TOTP_SECRET,
            dry_run=dry_run,
        )
        broker.connect()
        logger.info("Using Dhan HQ broker.")
        return broker
    else:
        broker = ZerodhaBroker(
            api_key=KITE_API_KEY,
            access_token=KITE_ACCESS_TOKEN,
            dry_run=dry_run,
        )
        broker.connect()
        logger.info("Using Zerodha Kite Connect broker.")
        return broker


async def consume_signal_queue(broker):
    """Poll the durable queue and hand each equity signal to the existing broker code.

    A handler exception is marked UNCERTAIN, never automatically retried, because the
    broker may have accepted an order before the exception occurred.
    """
    db_path = SIGNAL_DB_PATH
    initialize_queue(db_path)
    worker_id = f"equity:{socket.gethostname()}:{os.getpid()}"
    logger.info("Consuming EQUITY signals from SQLite: %s", db_path)
    logger.info("Queue status at startup: %s", queue_stats(db_path))

    while True:
        signal_payload = await asyncio.to_thread(claim_next, "EQUITY", worker_id, db_path)
        if signal_payload is None:
            await asyncio.sleep(QUEUE_POLL_SECONDS)
            continue

        signal_id = signal_payload.get("signal_id", "")
        channel_label = str(signal_payload.get("channel_label", "")).lower()
        server_latency = signal_payload.pop("_latency_ms", None)

        if ALLOWED_CHANNELS and channel_label not in ALLOWED_CHANNELS:
            logger.info("Skipping %s from channel '%s' (not in ALLOWED_CHANNELS)", signal_id, channel_label)
            await asyncio.to_thread(
                complete_signal, signal_id,
                {"status": "IGNORED", "reason": "channel not allowed"}, db_path,
            )
            await send_telegram_log(
                f"⚠️ EQUITY SIGNAL IGNORED\nsignal={signal_id}\n"
                f"channel={channel_label} is not in ALLOWED_CHANNELS; no order submitted.",
                mode=("test" if getattr(broker, "dry_run", True) else "live"),
            )
            continue

        message_ts = signal_payload.get("message_ts")
        max_age = max(1, int(os.environ.get("EQUITY_MAX_SIGNAL_AGE_SEC", "120")))
        if message_ts:
            try:
                parsed_ts = datetime.fromisoformat(str(message_ts).replace("Z", "+00:00"))
                if parsed_ts.tzinfo is None:
                    parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (datetime.now(timezone.utc) - parsed_ts.astimezone(timezone.utc)).total_seconds())
                if age_seconds > max_age:
                    logger.warning("Skipping expired equity signal %s (age %.0fs > %ss)", signal_id, age_seconds, max_age)
                    await asyncio.to_thread(complete_signal, signal_id,
                                            {"status": "IGNORED", "reason": "signal expired in queue", "age_seconds": round(age_seconds)},
                                            db_path)
                    await send_telegram_log(
                        f"⏱️ EQUITY SIGNAL EXPIRED\nsignal={signal_id}\nage={age_seconds:.0f}s "
                        f"(limit {max_age}s); no order submitted.",
                        mode=("test" if getattr(broker, "dry_run", True) else "live"),
                    )
                    continue
            except (TypeError, ValueError) as exc:
                await asyncio.to_thread(mark_uncertain, signal_id, f"invalid message_ts: {exc}", db_path)
                logger.error("Signal %s has invalid message_ts; marked UNCERTAIN", signal_id)
                continue

        logger.info(
            "Claimed equity signal %s: %s @ %s (%s), channel=%s, server=%s",
            signal_id, signal_payload.get("stock_symbol"), signal_payload.get("entry_price", "MARKET"),
            signal_payload.get("order_type", "MARKET"), channel_label, server_latency,
        )
        started = time_mod.perf_counter()
        try:
            # The existing executor is synchronous; keep broker/network I/O off the event loop.
            result = await asyncio.to_thread(broker.handle_signal, signal_payload)
        except asyncio.CancelledError:
            # Leave the persistent row CLAIMED. After restart it requires reconciliation;
            # it is intentionally not replayed automatically.
            raise
        except Exception as exc:
            await asyncio.to_thread(mark_uncertain, signal_id, repr(exc), db_path)
            logger.exception("Signal %s ended with an uncertain broker outcome; not retrying", signal_id)
            await send_telegram_log(f"🚨 EQUITY ORDER UNCERTAIN\nsignal={signal_id}\n{exc!r}\nCheck broker order/trade book before replay.", mode=("test" if getattr(broker, "dry_run", True) else "live"))
            continue

        if result is None:
            # Existing broker adapters may catch API exceptions and return None. That is
            # not proof of rejection: the broker may have accepted the order before the
            # response was lost. Require order-book reconciliation before any replay.
            await asyncio.to_thread(
                mark_uncertain,
                signal_id,
                "broker handler returned no outcome; reconcile broker orders before retrying",
                db_path,
            )
            logger.error("Equity signal %s returned no broker outcome; marked UNCERTAIN", signal_id)
            await send_telegram_log(f"🚨 EQUITY ORDER UNCERTAIN\nsignal={signal_id}\nBroker returned no outcome. Reconcile broker orders before replay.", mode=("test" if getattr(broker, "dry_run", True) else "live"))
            continue

        await asyncio.to_thread(complete_signal, signal_id, result, db_path)
        logger.info(
            "Equity signal %s handed to executor; result=%r, elapsed=%.1fms",
            signal_id, result, (time_mod.perf_counter() - started) * 1000,
        )
        await send_telegram_log(
            f"✅ EQUITY EXECUTOR RESULT\nsignal={signal_id}\n"
            f"symbol={signal_payload.get('stock_symbol')} price={signal_payload.get('entry_price', 'MARKET')}\n"
            f"result={str(result)[:1200]}", mode=("test" if getattr(broker, "dry_run", True) else "live"))



async def run_worker():
    """Validate risk settings, initialize the broker and consume the SQLite queue."""
    if EQUITY_SL_TARGET_ENABLED and (EQUITY_SL_PCT <= 0 or EQUITY_TARGET_PCT <= 0):
        logger.error("EQUITY_SL_TARGET_ENABLED=true needs EQUITY_SL_PCT and EQUITY_TARGET_PCT > 0")
        sys.exit(1)

    dry_run = os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes")
    broker = _create_broker(dry_run)
    if dry_run:
        logger.info("Running in DRY RUN mode. Orders will be simulated.")
    else:
        logger.warning("Running in LIVE mode. Orders WILL be placed on %s.", BROKER.upper())

    if EQUITY_SL_TARGET_ENABLED:
        if isinstance(broker, DhanBroker):
            broker.watcher = ProtectionWatcher(
                broker, EQUITY_SL_PCT, EQUITY_TARGET_PCT,
                EQUITY_SL_LIMIT_BUFFER_PCT, dry_run=dry_run,
            )
            asyncio.create_task(broker.watcher.run(lambda: not _is_amo_window()))
        else:
            logger.warning("EQUITY_SL_TARGET_ENABLED is only supported with BROKER=dhan; ignoring it.")
    await consume_signal_queue(broker)


if __name__ == "__main__":
    logger.info("Starting Equity Signal Execution Worker...")
    logger.info("Signal DB: %s", SIGNAL_DB_PATH)
    logger.info("Broker: %s", BROKER.upper())
    logger.info("Timestamp: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Client stopped by user.")
