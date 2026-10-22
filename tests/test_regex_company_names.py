"""Company-name resolution in the equity regex fast path ("Bought MANIPAL PAYMENT" -> MPIMANIPAL)."""

import pytest

from services import regex_service, scrip_service
from services.regex_service import extract_trade, filter_message

# (exchange, ticker, company name) as in the Dhan scrip master (SEM_CUSTOM_SYMBOL)
ROWS = [
    ("NSE", "MPIMANIPAL", "Manipal Payment and Identity Solutions"),
    ("BSE", "MPIMANIPAL", "Manipal Payment and Identity Solutions"),
    ("NSE", "MANIPALHOS", "Manipal Health Enterprises"),
    ("BSE", "MNPLFIN", "Manipal Finance"),
    ("NSE", "M&M", "Mahindra & Mahindra"),
    ("NSE", "M&MFIN", "M&M Financial Services"),
    ("NSE", "HEGAM", "HEG Advanced Materials"),
    ("BSE", "710HEGI31", "HEGICL-7.1%-09112031-NCD"),
    ("NSE", "SBIN", "State Bank of India"),
    ("NSE", "SBILIFE", "SBI Life Insurance Company"),
    ("NSE", "SBICARD", "SBI Cards and Payment Services"),
    ("NSE", "POLYCAB", "Polycab India"),
    ("NSE", "BAJFINANCE", "Bajaj Finance"),
    ("NSE", "BAJAJFINSV", "Bajaj Finserv"),
]


@pytest.fixture(autouse=True)
def scrip_master(monkeypatch):
    monkeypatch.setattr(scrip_service, "_NAME_INDEX", scrip_service.build_name_index(ROWS))
    symbols = {t for _, t, _ in ROWS}
    monkeypatch.setattr(regex_service, "VALID_EQUITY_SYMBOLS", symbols)


def sym(text):
    r = extract_trade(text)
    return None if r is None else (r["stock_symbol"], r["entry_price"])


@pytest.mark.parametrize("text, expected", [
    ("Bought\n\nMANIPAL PAYMENT", ("MPIMANIPAL", 0.0)),  # the reported message
    ("Bought Manipal Payment at 450", ("MPIMANIPAL", 450.0)),
    ("Added more Manipal Payment and Identity", ("MPIMANIPAL", 0.0)),
    ("Buy MANIPAL PAYMENT & IDENTITY SOLUTIONS @ 455", ("MPIMANIPAL", 455.0)),
    ("#MANIPAL PAYMENT buy", ("MPIMANIPAL", 0.0)),
    ("Bought Manipal Health", ("MANIPALHOS", 0.0)),
    ("Buy State Bank of India @ 810", ("SBIN", 810.0)),
    ("Bought Bajaj Finance at 7000", ("BAJFINANCE", 7000.0)),
    ("Bought HEG at 500", ("HEGAM", 500.0)),  # renamed company: ticker starts with the word
])
def test_company_names_resolve_to_ticker(text, expected):
    assert filter_message(text) is None
    assert sym(text) == expected


@pytest.mark.parametrize("text", [
    "Bought MANIPAL",  # Health / Payment / Finance: ambiguous
    "Bought SBI",  # SBI Life / SBI Cards: ambiguous (and not a ticker)
    "Bought Bajaj",  # Finance / Finserv: ambiguous
    "#M&M buy at 3000",  # 'M AND M' is not evidence for M&M Financial Services
    "Bought M and M",
    "Bought something nice today",
    "Bought some at 500",
])
def test_ambiguous_or_weak_names_are_not_guessed(text):
    assert sym(text) is None  # falls through to Gemini, exactly as before


@pytest.mark.parametrize("text, expected", [
    ("Buy #SBIN @ 805-810 SL 780", ("SBIN", 805.0)),
    ("Bought in POLYCAB @ 7000", ("POLYCAB", 7000.0)),
    ("Added more #SBIN", ("SBIN", 0.0)),
    ("Bought Polycab India", ("POLYCAB", 0.0)),
])
def test_valid_tickers_unchanged(text, expected):
    assert sym(text) == expected


def test_debt_rows_are_not_indexed():
    index = scrip_service.build_name_index(ROWS)
    assert all(t != "710HEGI31" for entries in index.values() for _, t in entries)


def test_no_scrip_master_loaded_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(regex_service, "VALID_EQUITY_SYMBOLS", set())
    monkeypatch.setattr(scrip_service, "_NAME_INDEX", {})
    assert sym("Bought\n\nMANIPAL PAYMENT") == ("MANIPAL", 0.0)  # unvalidated, as before
