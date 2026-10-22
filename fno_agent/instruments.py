"""
Option contract resolution from the broker's instrument master.

Both masters are normalised into one DataFrame with columns:
    symbol, strike, option_type, expiry (date), exchange (NFO|BFO), lot_size, tick_size,
    security_id, exchange_segment   (Dhan)
    tradingsymbol, instrument_token (Kite)
"""

import io
import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests

from services.fno_parser import MONTHS

logger = logging.getLogger(__name__)

DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
KITE_MASTER_URLS = ["https://api.kite.trade/instruments/NFO", "https://api.kite.trade/instruments/BFO"]

# Underlyings whose options trade on BSE (BFO); everything else routes to NSE (NFO)
BFO_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


class ContractNotFound(Exception):
    pass


@dataclass(frozen=True)
class Contract:
    symbol: str
    strike: float
    option_type: str
    expiry: date
    exchange: str  # NFO | BFO
    lot_size: int
    tick_size: float
    security_id: str = ""  # Dhan
    exchange_segment: str = ""  # Dhan: NSE_FNO | BSE_FNO
    tradingsymbol: str = ""  # Kite
    instrument_token: int = 0  # Kite

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Contract":
        d = dict(d)
        d["expiry"] = date.fromisoformat(d["expiry"])
        return cls(**d)

    def describe(self) -> str:
        name = self.tradingsymbol or f"{self.symbol} {self.expiry:%d%b%y} {self.strike:g}{self.option_type}"
        return f"{name} ({self.exchange}, lot {self.lot_size})"


def normalize_dhan(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[
        raw["INSTRUMENT"].isin(["OPTSTK", "OPTIDX"])
        & raw["EXCH_ID"].isin(["NSE", "BSE"])
        & (raw["SEGMENT"] == "D")
    ]
    return pd.DataFrame({
        "symbol": df["UNDERLYING_SYMBOL"].astype(str).str.upper().str.strip(),
        "strike": df["STRIKE_PRICE"].astype(float),
        "option_type": df["OPTION_TYPE"].astype(str).str.upper(),
        "expiry": pd.to_datetime(df["SM_EXPIRY_DATE"]).dt.date,
        "exchange": df["EXCH_ID"].map({"NSE": "NFO", "BSE": "BFO"}),
        "lot_size": df["LOT_SIZE"].astype(float).astype(int),
        # Dhan publishes tick size in paise
        "tick_size": df["TICK_SIZE"].astype(float) / 100.0,
        "security_id": df["SECURITY_ID"].astype(str),
        "exchange_segment": df["EXCH_ID"].map({"NSE": "NSE_FNO", "BSE": "BSE_FNO"}),
        "tradingsymbol": "",
        "instrument_token": 0,
    }).reset_index(drop=True)


def normalize_kite(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[raw["instrument_type"].isin(["CE", "PE"]) & raw["exchange"].isin(["NFO", "BFO"])]
    return pd.DataFrame({
        "symbol": df["name"].astype(str).str.upper().str.strip(),
        "strike": df["strike"].astype(float),
        "option_type": df["instrument_type"].astype(str),
        "expiry": pd.to_datetime(df["expiry"]).dt.date,
        "exchange": df["exchange"],
        "lot_size": df["lot_size"].astype(int),
        "tick_size": df["tick_size"].astype(float),
        "security_id": "",
        "exchange_segment": "",
        "tradingsymbol": df["tradingsymbol"].astype(str),
        "instrument_token": df["instrument_token"].astype(int),
    }).reset_index(drop=True)


def _download_csv(url: str) -> pd.DataFrame:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text), low_memory=False)


class InstrumentResolver:
    def __init__(self, broker: str, min_days_to_expiry: int = 1):
        self.broker = broker
        self.min_days_to_expiry = min_days_to_expiry
        self.df: Optional[pd.DataFrame] = None
        self.loaded_on: Optional[date] = None

    def refresh(self, today: Optional[date] = None) -> bool:
        """Download and swap in a fresh master. Keeps the old one if the download fails."""
        try:
            if self.broker == "dhan":
                df = normalize_dhan(_download_csv(DHAN_MASTER_URL))
            else:
                df = normalize_kite(pd.concat([_download_csv(u) for u in KITE_MASTER_URLS], ignore_index=True))
            if df.empty:
                raise ValueError("instrument master contained no options")
        except Exception as e:
            logger.error(f"Instrument master refresh failed ({self.broker}): {e}")
            return False
        self.load(df, today)
        logger.info(f"Loaded {len(df)} option contracts for {self.broker}.")
        return True

    def load(self, df: pd.DataFrame, today: Optional[date] = None):
        self.df = df
        self.loaded_on = today or date.today()

    def resolve(
        self,
        symbol: str,
        strike: float,
        option_type: str,
        expiry_month: Optional[str] = None,
        expiry_day: Optional[int] = None,
        today: Optional[date] = None,
    ) -> Contract:
        if self.df is None:
            raise ContractNotFound("instrument master not loaded")
        today = today or date.today()
        symbol = symbol.upper()
        exchange = "BFO" if symbol in BFO_UNDERLYINGS else "NFO"
        earliest = today + timedelta(days=self.min_days_to_expiry)

        df = self.df
        rows = df[
            (df["symbol"] == symbol)
            & (df["exchange"] == exchange)
            & (df["option_type"] == option_type.upper())
            & ((df["strike"] - float(strike)).abs() < 1e-6)
            & (df["expiry"] >= earliest)
        ]
        if rows.empty:
            raise ContractNotFound(
                f"no {exchange} contract for {symbol} {strike:g}{option_type} expiring on/after {earliest}"
            )

        if expiry_month:
            month = MONTHS.index(expiry_month.upper()[:3]) + 1
            year = today.year if month >= today.month else today.year + 1
            in_month = rows[rows["expiry"].map(lambda d: d.year == year and d.month == month)]
            if expiry_day:
                in_month = in_month[in_month["expiry"].map(lambda d: d.day == expiry_day)]
            if in_month.empty:
                raise ContractNotFound(
                    f"no {symbol} {strike:g}{option_type} expiry in {expiry_month} {year}"
                    + (f" on day {expiry_day}" if expiry_day else "")
                )
            # A month name means the monthly contract = last expiry of that month
            row = in_month.sort_values("expiry").iloc[-1]
        else:
            row = rows.sort_values("expiry").iloc[0]

        return Contract(
            symbol=row["symbol"],
            strike=float(row["strike"]),
            option_type=row["option_type"],
            expiry=row["expiry"],
            exchange=row["exchange"],
            lot_size=int(row["lot_size"]),
            tick_size=float(row["tick_size"]) or 0.05,
            security_id=str(row["security_id"]),
            exchange_segment=str(row["exchange_segment"]),
            tradingsymbol=str(row["tradingsymbol"]),
            instrument_token=int(row["instrument_token"]),
        )
