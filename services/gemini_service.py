"""
Gemini AI trade extraction service.

Three extraction pipelines:
1. extract_trade_async(text) — Original equity trade extraction for Google Sheets (unchanged).
2. extract_equity_trade_gemini(text) — Equity structured output for the signal queue.
3. hybrid_extract_trade(text, timestamp) — Regex-first, Gemini-fallback for equity signals.
"""

from google import genai
from google.genai import types
import json
import os
import asyncio
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from config import VERTEX_PROJECT_ID, GEMINI_LOCATION
from models import GeminiEquitySignal
from services.regex_service import extract_trade as regex_extract_trade

# Initialize a global client
_client = None

def init_vertex():
    global _client
    if not VERTEX_PROJECT_ID:
        return
        
    creds = None
    vertex_creds_env = os.environ.get("VERTEX_CREDENTIALS_JSON")
    
    try:
        if vertex_creds_env:
            creds_info = json.loads(vertex_creds_env)
            if "refresh_token" in creds_info:
                creds = Credentials.from_authorized_user_info(creds_info)
            else:
                creds = service_account.Credentials.from_service_account_info(creds_info)
    except Exception as e:
        print(f"Failed to load user credentials for Vertex: {e}")

    try:
        _client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT_ID,
            location=GEMINI_LOCATION,
            credentials=creds
        )
    except Exception as e:
        print(f"Failed to initialize google-genai Client: {e}")

init_vertex()


# --- Latency tuning (benchmarked 2026-09-23 on this project) ---
# Primary: gemini-2.5-flash with thinking off (~0.8-1.2s vs ~2.1s with default thinking, same accuracy).
# Fallback: gemini-3.1-flash-lite (~1.1-1.4s, accurate; separate capacity pool). gemini-2.0-flash is retired (404).
_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.1-flash-lite"]
GEMINI_TIMEOUT_SEC = 4.0  # per model attempt; worst case ~8s before giving up


def fast_config(model_name: str, **kwargs) -> types.GenerateContentConfig:
    """GenerateContentConfig tuned for low-latency extraction: no thinking, no AFC."""
    if model_name.startswith("gemini-2."):
        thinking = types.ThinkingConfig(thinking_budget=0)
    else:  # gemini-3.x uses thinking levels instead of a token budget
        thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    return types.GenerateContentConfig(
        temperature=0.0,
        thinking_config=thinking,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **kwargs,
    )

async def extract_trade_async(text):
    """
    Analyzes the text using Vertex AI Gemini and extracts trade details if present.
    Returns a dictionary or None.
    """
    if not _client:
        print("Vertex AI Client missing. Skipping trade extraction.")
        return None

    # Using the new Google GenAI SDK and stable model identifier
    model_name = "gemini-2.5-flash"
    
    prompt = f"""
    You are an expert financial analyst. Analyze the following Telegram message and determine if it contains a stock or cryptocurrency trade setup.
    If it does contain a trade setup, extract the following details:
    - NAME: The exact ticker symbol for Google Finance. Since these are mostly Indian stocks, if a business name is given (e.g. "Reliance"), map it to the correct NSE ticker symbol and prefix it with "NSE:" (e.g., "NSE:RELIANCE").
    - Buying Price: The exact entry/buying price mentioned. If NO specific price is mentioned, use an empty string "".
    - TARGET: The absolute target price (🎯) mentioned. If only a percentage target is given (e.g., "target 10%"), leave this as "" and fill TARGET_PERCENT instead.
    - TARGET_PERCENT: If the target is mentioned as a percentage (e.g., "10% target", "upside of 15%"), extract just the number (e.g., "10" or "15"). If an absolute target price is given or no target at all, use "".
    - STOP_LOSS: The absolute stop loss price mentioned (e.g., "SL 1200", "stop loss at 1200"). If only a percentage is given, leave this as "" and fill STOP_LOSS_PERCENT instead. If not mentioned at all, use "".
    - STOP_LOSS_PERCENT: If the stop loss is mentioned as a percentage (e.g., "SL 5%", "risk 3%"), extract just the number (e.g., "5" or "3"). If an absolute stop loss is given or none at all, use "".
    - Reason: Any rationale or reason mentioned for the trade.

    Format your response EXACTLY as a JSON object with the following schema:
    {{
        "is_trade": boolean,
        "NAME": string,
        "Buying Price": string,
        "TARGET": string,
        "TARGET_PERCENT": string,
        "STOP_LOSS": string,
        "STOP_LOSS_PERCENT": string,
        "Reason": string
    }}
    
    If any field is missing, use an empty string "".
    If it is NOT a trade setup, return {{"is_trade": false}}.
    Return ONLY the raw JSON object, no markdown blocks or extra text.

    Message to analyze:
    {text}
    """

    try:
        # Use aio for async requests
        response = await _client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=fast_config(model_name),
        )
        result_text = response.text.strip()
        
        # Remove potential markdown formatting just in case
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
            
        data = json.loads(result_text.strip())
        
        if data.get("is_trade"):
            print(f"Vertex AI identified a trade: {data.get('NAME')}")
            return data
        else:
            print("Vertex AI analyzed message: Not a trade.")
            return None
            
    except Exception as e:
        print(f"Vertex AI analysis failed: {e}")
        return None


# --- Equity-specific extraction using Gemini Structured Output ---

_EQUITY_PROMPT = """You are an expert Indian stock market data extractor. Extract the trading symbol and the HIGHEST entry price from this message. 
            
CRITICAL RULES:
1. SYMBOLS: Convert all brand names or shorthand into their OFFICIAL NSE/BSE EXCHANGE TICKER (e.g., 'sbi' -> 'SBIN', 'Reliance' -> 'RELIANCE').
2. AMBIGUOUS GROUPS: Default to flagship stock (e.g., 'Tata' -> 'TATAMOTORS') unless specified.
3. PRICE: If an entry price or price range is given, extract the highest number. Ignore SL and Allocation targets.
4. NO PRICE / CMP: If no explicit entry price is mentioned, set upper_entry_price to 0.
5. VAGUE/ANALYSIS/MULTIPLE: If the message recommends buying MORE THAN ONE stock, OR if it is just commentary/future planning, output exactly "IGNORE" as the stock_symbol.
6. THIS BOT TRADES EQUITY ONLY. Ignore options/futures completely.
7. BUY INTENT REQUIRED: If the message does not explicitly suggest entering a long position (e.g., lacks 'buy', 'added', 'accumulate', 'cmp', 'sl', 'target', or similar intent), OR if it just states a ticker (e.g. '#HEG'), OR if it suggests booking profit/selling (e.g., 'book some'), output EXACTLY "IGNORE" as the stock_symbol.

Message: """



async def extract_equity_trade_gemini(text: str) -> dict | None:
    """
    Extract equity trade signal using Gemini structured output.
    
    Uses the model fallback chain with GEMINI_TIMEOUT_SEC per model attempt.
    Returns {stock_symbol, entry_price, order_type, source} or None.
    """
    if not _client:
        print("Vertex AI Client missing. Skipping equity Gemini extraction.")
        return None

    prompt = _EQUITY_PROMPT + text

    for model_name in _FALLBACK_MODELS:
        try:
            response = await asyncio.wait_for(
                _client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=fast_config(
                        model_name,
                        response_mime_type="application/json",
                        response_schema=GeminiEquitySignal,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SEC,
            )

            signal = response.parsed
            if not signal:
                print(f"[GEMINI] {model_name}: No parsed response.")
                continue

            stock_symbol = signal.stock_symbol.upper()
            entry_price = signal.upper_entry_price

            # Block multi-stock or vague messages
            if stock_symbol in ("MULTIPLE_SCRIPS", "IGNORE", "NONE", ""):
                print(f"[GEMINI] AI blocked: '{stock_symbol}' — vague or multiple stocks.")
                return None

            order_type = "LIMIT" if entry_price > 0 else "MARKET"

            print(f"[GEMINI] {model_name} extracted: {stock_symbol} @ {'MARKET' if entry_price == 0 else entry_price}")

            return {
                "stock_symbol": stock_symbol,
                "entry_price": entry_price,
                "order_type": order_type,
                "source": "GEMINI",
                "raw_text": text,
                "timestamp": "",
            }

        except asyncio.TimeoutError:
            print(f"[GEMINI] {model_name} timed out (>{GEMINI_TIMEOUT_SEC}s). Trying next model...")
            continue

        except Exception as e:
            error_msg = str(e)
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                print(f"[GEMINI] {model_name} overloaded (503). Trying next model...")
                continue
            elif "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print(f"[GEMINI] {model_name} rate limited (429). Trying next model...")
                continue
            else:
                # e.g. 404 if a model is retired: still try the next model
                print(f"[GEMINI] {model_name} unhandled error: {error_msg}. Trying next model...")
                continue

    return None


async def hybrid_extract_trade(text: str, timestamp: str = "") -> dict | None:
    """
    Two-stage equity trade extraction: Regex (fast-path) → Gemini (fallback).
    
    1. Tries deterministic regex extraction (<1ms).
    2. If regex returns None, falls back to Gemini structured output.
    3. If both fail, returns None (message is not an equity trade signal).
    
    Note: Message-level filters (index blocker, sell blocker, etc.) are applied
    in main.py BEFORE calling this function.
    
    Args:
        text: Raw message text from Telegram.
        timestamp: IST-formatted timestamp string.
        
    Returns:
        Trade signal dict or None.
    """
    if not text or not isinstance(text, str):
        return None

    # Stage 1: Fast-path regex
    result = regex_extract_trade(text)
    if result is not None:
        result["timestamp"] = timestamp
        print(f"[REGEX] Extracted: {result['stock_symbol']} @ {'MARKET' if result['entry_price'] == 0 else result['entry_price']}")
        return result

    # Stage 2: Gemini fallback
    result = await extract_equity_trade_gemini(text)
    if result is not None:
        result["timestamp"] = timestamp
        return result

    return None
