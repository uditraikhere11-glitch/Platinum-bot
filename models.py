"""
Pydantic models for equity trade signal extraction and durable queue payloads.

These schemas are shared across:
- services/regex_service.py (fast-path extraction)
- services/gemini_service.py (LLM fallback extraction)
- main.py (SQLite queue payload)
- client_agent/client.py (execution engine deserialization)
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class GeminiEquitySignal(BaseModel):
    """
    Minimal schema passed to Gemini for structured output extraction.
    Matches the reference code's TradeSignal(stock_symbol, upper_entry_price).
    """
    stock_symbol: str = Field(
        description="The official NSE/BSE ticker symbol, e.g. SBIN, TATAMOTORS, RELIANCE."
    )
    upper_entry_price: float = Field(
        description="The highest entry price mentioned. 0 if no explicit price (CMP/market order)."
    )


class TradeSignal(BaseModel):
    """
    Represents a parsed equity trading signal for the SQLite queue.

    Used as the canonical schema for:
    1. Regex fast-path extraction result
    2. Gemini fallback extraction result
    3. SQLite JSON payload → client_agent
    """
    stock_symbol: str = Field(
        description="The NSE/BSE ticker symbol, e.g. SBIN, TATAMOTORS, RELIANCE."
    )
    entry_price: float = Field(
        default=0.0,
        description="Entry price. 0 means MARKET order (CMP)."
    )
    order_type: Literal["MARKET", "LIMIT"] = Field(
        default="MARKET",
        description="Order type: MARKET (CMP/no price) or LIMIT (specific price)."
    )
    source: Literal["REGEX", "GEMINI"] = Field(
        default="REGEX",
        description="Which extraction method produced this signal."
    )
    raw_text: str = Field(
        default="",
        description="The original message text that was parsed."
    )
    timestamp: str = Field(
        default="",
        description="IST-formatted timestamp of the original message."
    )
