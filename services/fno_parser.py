"""
FnO (option) signal parser.

Turns semi-structured option calls such as

    #POLYCAB 8000 PE OCT @90-105
    SL-30
    Target-500,1000

into a validated FnOSignal. Regex is the fast path; Gemini (Vertex) is the
fallback for messages that clearly mention an option but don't fit the regex.

Safety rules:
- Only BUY entries are produced. Messages containing exit wording
  (exit / book / sell / square off / sl hit) never become an entry.
- A signal must have symbol, strike, CE/PE and an entry price.
- Stop-loss and targets are optional and informational: the FnO bot sets its own
  SL/target as a % of the entry price (FNO_SL_PCT / FNO_TARGET_PCT). Values that
  don't make sense (SL at/above entry, targets at/below entry) are dropped, not
  used to reject the signal.
"""

import asyncio
import logging
import re
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_MONTH_RE = "|".join(MONTHS)
_NUM = r"\d+(?:\.\d+)?"


class FnOSignal(BaseModel):
    symbol: str
    strike: float
    option_type: str  # CE | PE
    expiry_month: Optional[str] = None  # JAN..DEC
    expiry_day: Optional[int] = None
    entry_min: float
    entry_max: float
    stop_loss: Optional[float] = None
    targets: List[float] = Field(default_factory=list)
    caution: bool = False  # "limited qty", "add slowly", ... (informational)
    hold_hint: Optional[str] = None
    raw_text: str = ""


_CORE_RE = re.compile(
    r"(?<![A-Za-z0-9&\-])#?\s*(?P<sym>[A-Za-z][A-Za-z&\-]*)\s*"
    r"(?P<strike>" + _NUM + r")\s*(?P<opt>CE|PE|CALL|PUT)\b"
    r"(?:\s+(?:(?P<day>\d{1,2})\s*)?(?P<mon>" + _MONTH_RE + r")[A-Z]*)?"
    r"[^@\n]*?@\s*(?P<e1>" + _NUM + r")(?:\s*(?:-|–|to)\s*(?P<e2>" + _NUM + r"))?",
    re.IGNORECASE,
)
_SL_RE = re.compile(r"\b(?:SL|STOP\s*-?\s*LOSS)\s*[-_:=]*\s*(" + _NUM + r")", re.IGNORECASE)
_TARGET_RE = re.compile(r"\b(?:TARGETS?|TGT|TP)\s*[-_:=]*\s*([^\n]*)", re.IGNORECASE)
_EXIT_RE = re.compile(r"\b(exit|book|sell|square\s*-?\s*off|sl\s*hit|close)\b", re.IGNORECASE)
_CAUTION_RE = re.compile(r"limited\s+qty|add\s+slowly|small\s+qty|low\s+liquidity|avg|average", re.IGNORECASE)
_HOLD_RE = re.compile(r"month\s*end|\d+\s*weeks?|positional|hold(?:ing)?\s+till[^\n]*", re.IGNORECASE)
# Cheap gate (~1µs): a strike glued to CE/PE, or the words CALL/PUT. Every message the core regex
# can parse also matches this, so it decides which path (FnO vs equity) a message belongs to.
_OPTION_HINT_RE = re.compile(r"\d\s*(CE|PE)\b|\b(CALL|PUT)\b", re.IGNORECASE)

_SYMBOL_ALIASES = {"SENSEX": "SENSEX", "BANK NIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY"}


def _to_float(s: Optional[str]) -> Optional[float]:
    try:
        return float(s) if s is not None else None
    except (TypeError, ValueError):
        return None


def _validate(sig: FnOSignal) -> Optional[FnOSignal]:
    """Normalise and sanity-check a signal. Returns None if it's not tradeable."""
    sig.symbol = _SYMBOL_ALIASES.get(sig.symbol.upper(), sig.symbol.upper())
    sig.option_type = {"CALL": "CE", "PUT": "PE"}.get(sig.option_type.upper(), sig.option_type.upper())
    if sig.expiry_month:
        sig.expiry_month = sig.expiry_month.upper()[:3]
        if sig.expiry_month not in MONTHS:
            sig.expiry_month = None
    if sig.entry_min > sig.entry_max:
        sig.entry_min, sig.entry_max = sig.entry_max, sig.entry_min

    if sig.option_type not in ("CE", "PE"):
        return _reject(sig, "option type not CE/PE")
    if sig.strike <= 0 or sig.entry_min <= 0:
        return _reject(sig, "non-positive strike/entry")
    # SL/targets are optional (the bot uses its own %); keep only values that make sense
    if sig.stop_loss is not None and not (0 < sig.stop_loss < sig.entry_min):
        logger.info(f"[FNO PARSER] Ignoring SL {sig.stop_loss} for {sig.symbol} (not below entry {sig.entry_min})")
        sig.stop_loss = None
    sig.targets = sorted(t for t in sig.targets if t and t > sig.entry_max)
    return sig


def _reject(sig: FnOSignal, reason: str) -> None:
    logger.info(f"[FNO PARSER] Rejected {sig.symbol} {sig.strike}{sig.option_type}: {reason}")
    return None


def looks_like_option(text: str) -> bool:
    """True if the message mentions an option contract (used to route it away from the equity path)."""
    return bool(_OPTION_HINT_RE.search(text or ""))


def is_exit_message(text: str) -> bool:
    return bool(_EXIT_RE.search(text or ""))


def parse_fno_regex(text: str) -> Optional[FnOSignal]:
    """Deterministic parse. Returns a validated FnOSignal or None."""
    if not text or not isinstance(text, str):
        return None
    if is_exit_message(text):
        return None

    m = _CORE_RE.search(text)
    if not m:
        return None

    strike = _to_float(m.group("strike"))
    e1 = _to_float(m.group("e1"))
    e2 = _to_float(m.group("e2")) or e1
    if strike is None or e1 is None:
        return None

    sl_m = _SL_RE.search(text, m.end())
    stop_loss = _to_float(sl_m.group(1)) if sl_m else None

    targets: List[float] = []
    tgt_m = _TARGET_RE.search(text, m.end())
    if tgt_m:
        targets = [v for v in (_to_float(x) for x in re.findall(_NUM, tgt_m.group(1))) if v]

    hold_m = _HOLD_RE.search(text)
    sig = FnOSignal(
        symbol=m.group("sym"),
        strike=strike,
        option_type=m.group("opt"),
        expiry_month=m.group("mon"),
        expiry_day=int(m.group("day")) if m.group("day") else None,
        entry_min=e1,
        entry_max=e2,
        stop_loss=stop_loss,
        targets=targets,
        caution=bool(_CAUTION_RE.search(text)),
        hold_hint=hold_m.group(0).strip() if hold_m else None,
        raw_text=text,
    )
    return _validate(sig)


# ---------------------------------------------------------------------------
# Gemini fallback
# ---------------------------------------------------------------------------

class GeminiFnOSignal(BaseModel):
    is_option_buy_entry: bool = Field(description="True only if this message is a fresh BUY call on an index/stock option")
    symbol: str = Field(description="Underlying symbol, e.g. POLYCAB, NIFTY, SENSEX")
    strike: float
    option_type: str = Field(description="CE or PE")
    expiry_month: Optional[str] = Field(default=None, description="3-letter month like OCT if mentioned, else null")
    entry_min: float
    entry_max: float
    stop_loss: Optional[float] = Field(default=None, description="Stop-loss premium if mentioned, else null")
    targets: List[float] = Field(default_factory=list, description="Target premiums if mentioned, else empty")


_FNO_PROMPT = """You extract option BUY calls from Indian stock market Telegram messages.
Set is_option_buy_entry=false for anything that is not a fresh buy entry on a CE/PE option
(exits, profit booking updates, commentary, equity calls). Prices are option premiums.
If a single entry price is given, set entry_min = entry_max = that price.
Message:
"""

async def parse_fno_gemini(text: str) -> Optional[FnOSignal]:
    """Gemini fallback, gated by a cheap option-keyword check. Returns a validated FnOSignal or None."""
    if not text or is_exit_message(text) or not looks_like_option(text):
        return None

    # Imported lazily: gemini_service initialises Vertex at import time
    from services import gemini_service

    client = gemini_service._client
    if not client:
        logger.warning("[FNO PARSER] Vertex client unavailable; skipping Gemini fallback.")
        return None

    for model_name in gemini_service._FALLBACK_MODELS:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=_FNO_PROMPT + text,
                    config=gemini_service.fast_config(
                        model_name,
                        response_mime_type="application/json",
                        response_schema=GeminiFnOSignal,
                    ),
                ),
                timeout=gemini_service.GEMINI_TIMEOUT_SEC,
            )
            g = response.parsed
            if not g or not g.is_option_buy_entry:
                return None
            sig = FnOSignal(
                symbol=g.symbol,
                strike=g.strike,
                option_type=g.option_type,
                expiry_month=g.expiry_month,
                entry_min=g.entry_min,
                entry_max=g.entry_max,
                stop_loss=g.stop_loss,
                targets=g.targets,
                caution=bool(_CAUTION_RE.search(text)),
                raw_text=text,
            )
            return _validate(sig)
        except asyncio.TimeoutError:
            logger.warning(f"[FNO GEMINI] {model_name} timed out; trying next model.")
        except Exception as e:
            logger.error(f"[FNO GEMINI] {model_name} failed: {e}")
    return None


async def hybrid_fno_extract(text: str) -> Optional[dict]:
    """Regex first, then Gemini. Returns a broadcast-ready dict (with `source`) or None."""
    sig = parse_fno_regex(text)
    source = "REGEX"
    if sig is None:
        sig = await parse_fno_gemini(text)
        source = "GEMINI"
    if sig is None:
        return None
    data = sig.model_dump()
    data["source"] = source
    return data
