import csv
import io
import re
import requests
import logging

logger = logging.getLogger(__name__)

VALID_EQUITY_SYMBOLS = set()
# Company-name index: first name word -> [(all name words, ticker), ...]
# Lets messages that name the company ("Bought MANIPAL PAYMENT") resolve to the ticker (MPIMANIPAL),
# including new listings the AI model has never heard of.
_NAME_INDEX: dict = {}
_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize_name(text: str) -> tuple:
    """'Manipal Payment & Identity' -> ('MANIPAL', 'PAYMENT', 'AND', 'IDENTITY')"""
    return tuple(_NON_ALNUM.sub(" ", text.upper().replace("&", " AND ")).split())


def build_name_index(rows) -> dict:
    """rows: iterable of (exchange, ticker, company_name). NSE ticker wins when a company is on both."""
    by_name = {}
    for exchange, ticker, name in rows:
        words = normalize_name(name or "")
        # Skip debt/bond rows listed as EQUITY (e.g. "HEGICL-7.1%-09112031-NCD")
        if not words or not ticker or ticker[0].isdigit() or "%" in (name or "") or "NCD" in words:
            continue
        if words not in by_name or exchange == "NSE":
            by_name[words] = ticker
    index = {}
    for words, ticker in by_name.items():
        index.setdefault(words[0], []).append((words, ticker))
    return index


def resolve_company_name(phrase_words) -> str | None:
    """
    Map the words after "bought"/"buy" to a ticker by company name, e.g. ('MANIPAL', 'PAYMENT') -> 'MPIMANIPAL'.

    Uses the longest leading run of words that matches the start of exactly ONE company name, and
    only accepts it when that is strong evidence:
      - at least two meaningful words ('MANIPAL PAYMENT'), not letters/'AND' ('M&M' -> M AND M), or
      - a single word the ticker itself starts with (renamed 'HEG' -> HEGAM).
    Ambiguous phrases ('MANIPAL' = Health / Payment / Finance) and unknown words return None.
    """
    words = tuple(phrase_words)
    candidates = _NAME_INDEX.get(words[0], []) if words else []
    for n in range(len(words), 0, -1):
        prefix = words[:n]
        tickers = {t for name, t in candidates if name[:n] == prefix}
        if len(tickers) > 1:
            return None  # ambiguous: don't guess
        if len(tickers) == 1:
            ticker = tickers.pop()
            meaningful = [w for w in prefix if len(w) > 1 and w != "AND"]
            if len(meaningful) >= 2 or (n == 1 and len(prefix[0]) > 1 and ticker.startswith(prefix[0])):
                return ticker
            return None  # too little evidence (e.g. a lone word, or 'M AND M')
    return None


def load_valid_scrips():
    """
    Downloads the official Dhan Scrip Master CSV on startup
    and populates a global set of valid NSE/BSE equity symbols.
    This is used by the regex engine to validate symbols, forcing
    fallback to AI for typos or invalid tickers.
    Also builds the company-name index used by resolve_company_name().
    """
    global VALID_EQUITY_SYMBOLS
    try:
        logger.info("Downloading Dhan scrip master for regex validation...")
        response = requests.get(_SCRIP_MASTER_URL, timeout=10)

        if response.status_code != 200:
            logger.error(f"Failed to download scrip master: HTTP {response.status_code}")
            return

        csv_data = csv.DictReader(io.StringIO(response.text))
        names = []

        for row in csv_data:
            exchange = row.get("SEM_EXM_EXCH_ID", "")
            inst_type = row.get("SEM_INSTRUMENT_NAME", "")
            symbol = row.get("SEM_TRADING_SYMBOL", "")

            if exchange in ("NSE", "BSE") and inst_type == "EQUITY" and symbol:
                clean_symbol = symbol.replace("-EQ", "").strip().upper()
                VALID_EQUITY_SYMBOLS.add(clean_symbol)
                names.append((exchange, clean_symbol, row.get("SEM_CUSTOM_SYMBOL", "")))

        _NAME_INDEX.clear()
        _NAME_INDEX.update(build_name_index(names))
        logger.info(f"Loaded {len(VALID_EQUITY_SYMBOLS)} valid equity symbols for regex fallback.")

    except Exception as e:
        logger.error(f"Error loading scrip master: {e}")
