"""
Fast-path deterministic regex parser for Indian equity trading signals.

Extracts stock symbols from hashtags and BUY/bought/added patterns,
and entry prices from @/at/CMP patterns. All patterns are pre-compiled
at module load time for <1ms extraction.

Also provides message-level filters (blockers) matching the reference
trading bot logic:
  - Index blocker (NIFTY, BANKNIFTY, etc.)
  - Sell/Short/Exit/F&O blocker (BUY-ONLY equity mode)
  - Message length gatekeeper (>300 chars)
  - Multiple-stock blocker
  - Analysis/vague chat blocker
  - BUY intent filter
"""

import re
from typing import Optional
from services.scrip_service import VALID_EQUITY_SYMBOLS, normalize_name, resolve_company_name

# --- Pre-compiled Patterns (executed once at import time) ---

# Hashtag extraction: #SBIN, # RELIANCE, etc.
_HASHTAG_PATTERN = re.compile(r'#\s*([a-zA-Z]+)')

# Action + symbol: "buy SBIN", "bought in RELIANCE", "added TATAMOTORS", plus typos
_ACTION_SYMBOL_PATTERN = re.compile(
    r'(?:buy|biy|bought|boght|bougth|bot|baught|add|asd|added|aded)\s+(?:in\s+)?(?:#\s*)?([a-zA-Z]+)',
    re.IGNORECASE,
)

# Same anchors, but capture up to 6 words on that line, for company names: "Bought\nMANIPAL PAYMENT"
_WORD = r"[A-Za-z&][A-Za-z&.\-]*"
_HASHTAG_PHRASE_PATTERN = re.compile(r'#\s*(' + _WORD + r'(?:[ \t]+' + _WORD + r'){0,5})')
_ACTION_PHRASE_PATTERN = re.compile(
    r'(?:buy|biy|bought|boght|bougth|bot|baught|add|asd|added|aded)\s+(?:in\s+)?(?:#\s*)?(' + _WORD + r'(?:[ \t]+' + _WORD + r'){0,5})',
    re.IGNORECASE,
)
# Words that end a company-name phrase ("Bought Manipal Payment at 450 for positional")
_PHRASE_STOP_WORDS = frozenset({
    "AT", "CMP", "SL", "TARGET", "TGT", "ENTRY", "FOR", "NOW", "TODAY", "ABOVE", "BELOW",
    "AROUND", "NEAR", "RS", "WITH", "ON", "IN", "QTY", "LEVELS", "LEVEL", "ZONE",
})

# Entry price: "@ 120", "at Rs. 2500", "CMP 120", "at: 500"
_PRICE_PATTERN = re.compile(
    r'(?:@|at|cmp)\s*(?:rs\.?)?\s*[:\-]?\s*([\d\.]+)',
    re.IGNORECASE,
)

# BUY intent filter: message must contain at least one of these to be actionable
_BUY_INTENT_PATTERN = re.compile(
    r'\b(buy|biy|bought|boght|bougth|bot|baught|added|aded|add|asd|accumulate|accumulated|sl|target|entry|cmp)\b',
    re.IGNORECASE,
)

# --- Meta hashtags to ignore (not stock symbols) ---
META_TAGS = frozenset({
    "LEARNING", "LEARN", "EDUCATION", "EDUCATIONAL", "CHART", "CHARTS",
    "BREAKOUT", "RADAR", "SETUP", "DISCLAIMER", "UPDATE", "ALERT", "INTRADAY",
    "SWING", "POSITIONAL", "HOLDING", "TARGET", "SL", "ENTRY", "CMP", "NEWS",
    "IPO", "VIEW", "LEVELS", "STUDY", "WATCHLIST", "KNOWLEDGE", "INFO", "MOMENTUM",
})

# --- Index keywords (block F&O/index trades) ---
_INDEX_KEYWORDS = frozenset({
    "nifty", "banknifty", "sensex", "finnifty", "finifty",
    "bankex", "midcpnifty", "midcap",
})

# --- Sell/Short/Exit/F&O keywords (BUY-ONLY mode) ---
# Use word-boundary regex to prevent false positives like "reliance" matching "ce"
_SELL_PATTERN = re.compile(
    r'\b(?:sell|short|exit|book|booked|booking|close|square\s+off|sl\s+hit|target\s+hit|(?<!\w)ce(?!\w)|(?<!\w)pe(?!\w)|call|put)\b',
    re.IGNORECASE,
)

# --- Analysis/vague keywords ---
_ANALYSIS_KEYWORDS = frozenset({
    "tomorrow", "next week", "next month", "mid term", "long term", "short term",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "ema", "sma", "dma", "support", "resistance", "trendline",
    "watching", "watch", "view",
    "sitting", "holding", "loss", "not gud to add", "not good to add"  # <--- Added your new words here
})
# --- Common English words that are NOT stock symbols ---
_NOISE_WORDS = frozenset({
    "THEM", "IT", "THIS", "THE", "SOME", "NOW", "TODAY", "MORE", "ALL",
    "BUY", "SELL", "BOUGHT", "ADDED", "ADD", "IN", "AT", "FOR",
    "BIY", "BOGHT", "BOUGTH", "BOT", "BAUGHT", "ASD", "ADED"
})

def filter_message(text: str) -> str | None:
    """
    Apply all message-level blockers from the reference trading bot.

    Returns:
        None if the message passes all filters (should be processed).
        A string reason if the message should be BLOCKED.
    """
    if not text or not isinstance(text, str):
        return "Empty message"

    msg_lower = text.lower()

    # 🛡️ Message length gatekeeper
    if len(text) > 300:
        return "Message too long (>300 chars)"

    # 🛑 Index blocker
    if any(kw in msg_lower for kw in _INDEX_KEYWORDS):
        return "Index trade detected (NIFTY/BANKNIFTY/etc.)"

    # 🛑 Sell/Short/Exit/F&O blocker (word-boundary regex)
    if _SELL_PATTERN.search(text):
        return "Sell/Short/Exit/Option signal (BUY-ONLY mode)"

    # Extract hashtags for multi-stock check
    hashtags = _HASHTAG_PATTERN.findall(text)
    unique_symbols = set(h.upper() for h in hashtags if h.upper() not in META_TAGS)
    if len(unique_symbols) > 1:
        return f"Multiple stocks detected: {unique_symbols}"

    # 🛑 Analysis/vague chat blocker
    if any(kw in msg_lower for kw in _ANALYSIS_KEYWORDS):
        return "Future planning or analysis content"

    # ✅ BUY intent filter (must have a buy keyword OR a price specifier like @)
    if not _BUY_INTENT_PATTERN.search(text) and "@" not in text:
        return "No BUY intent detected (casual chat or just a ticker)"

    return None  # Message passes all filters


def _phrase_tokens(phrase: str, skip_noise: bool) -> list:
    """Words of a candidate company-name phrase: optional leading noise dropped, cut at the first stop word."""
    tokens = phrase.split()
    if skip_noise:
        while tokens and tokens[0].upper() in _NOISE_WORDS:
            tokens.pop(0)
    for i, tok in enumerate(tokens):
        if tok.upper().strip(".-") in _PHRASE_STOP_WORDS:
            return tokens[:i]
    return tokens


def _resolve_by_name(tokens: list) -> Optional[str]:
    """Company-name lookup ('MANIPAL PAYMENT' -> 'MPIMANIPAL'); None if unknown or ambiguous."""
    return resolve_company_name(normalize_name(" ".join(tokens))) if tokens else None


def extract_trade(text: str) -> Optional[dict]:
    """
    Attempt to extract an equity trade signal from raw text using deterministic regex.

    Two-stage extraction (matching reference code's fast-path):
    1. Try hashtag symbols first (#SBIN, #RELIANCE)
    2. Try action patterns (buy SBIN, bought in RELIANCE)
    3. Extract price from @/at/CMP patterns

    Returns:
        A dict with {stock_symbol, entry_price, order_type, source, raw_text}
        or None if no confident match found.
    """
    if not text or not isinstance(text, str):
        return None

    # --- Stage 1: Hashtag extraction ---
    all_hashtags = _HASHTAG_PATTERN.findall(text)
    candidate = next(
        (h.upper() for h in all_hashtags if h.upper() not in META_TAGS),
        None,
    )
    phrase = []
    if candidate:
        phrase_match = next((m for m in _HASHTAG_PHRASE_PATTERN.finditer(text)
                             if m.group(1).split()[0].upper().startswith(candidate)), None)
        phrase = _phrase_tokens(phrase_match.group(1), skip_noise=False) if phrase_match else []

    # --- Stage 2: Action pattern fallback ---
    if not candidate:
        action_match = _ACTION_SYMBOL_PATTERN.search(text)
        if action_match:
            symbol = action_match.group(1).upper()
            if symbol not in _NOISE_WORDS:
                candidate = symbol
        phrase_match = _ACTION_PHRASE_PATTERN.search(text)
        if phrase_match:
            # "Added more Manipal Payment": skip leading filler words for the name lookup
            phrase = _phrase_tokens(phrase_match.group(1), skip_noise=True)

    # --- Validate against Scrip Master (if loaded) ---
    if VALID_EQUITY_SYMBOLS and candidate not in VALID_EQUITY_SYMBOLS:
        # Not a ticker: try the company name ("MANIPAL PAYMENT" -> MPIMANIPAL), else fallback to AI
        candidate = _resolve_by_name(phrase)

    if not candidate:
        return None

    # --- Extract entry price ---
    entry_price = 0.0
    price_match = _PRICE_PATTERN.search(text)
    if price_match:
        entry_price = float(price_match.group(1))

    # Determine order type
    order_type = "LIMIT" if entry_price > 0 else "MARKET"

    return {
        "stock_symbol": candidate,
        "entry_price": entry_price,
        "order_type": order_type,
        "source": "REGEX",
        "raw_text": text,
        "timestamp": "",  # Filled by caller
    }
