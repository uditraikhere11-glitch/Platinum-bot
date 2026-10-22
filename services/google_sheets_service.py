import gspread
from google.oauth2.credentials import Credentials
import asyncio
import os
import json
from config import GOOGLE_SHEET_ID

def append_to_sheet_sync(trade_data, date_analysed):
    """
    Synchronously appends a row to Google Sheets.
    Columns: A=NAME, B=Gain, C=Buying Price, D=Target, E=Date, F=Reason, G=Current Price, H=Price Source, I=Stop Loss
    
    If buying price is missing from the AI extraction, uses GOOGLEFINANCE to auto-populate.
    If target percentage is given instead of absolute price, calculates target = price * (1 + pct/100).
    """
    if not GOOGLE_SHEET_ID:
        print("Google Sheet ID missing. Skipping sheets upload.")
        return

    # Support environment variable injection for cloud
    authorized_user_env = os.environ.get("AUTHORIZED_USER_JSON")
    
    try:
        if authorized_user_env:
            # Parse from environment string
            creds_info = json.loads(authorized_user_env)
            creds = Credentials.from_authorized_user_info(creds_info)
            client = gspread.authorize(creds)
        else:
            # Fallback to local files
            if not os.path.exists("temp/client_secret.json") or not os.path.exists("temp/authorized_user.json"):
                print("OAuth credentials not found in env or temp/ directory. Skipping sheets upload.")
                return

            client = gspread.oauth(
                credentials_filename='temp/client_secret.json',
                authorized_user_filename='temp/authorized_user.json'
            )
        
        # Open sheet
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        name = trade_data.get("NAME", "")
        buying_price_raw = str(trade_data.get("Buying Price", "")).replace(',', '').strip()
        target_raw = str(trade_data.get("TARGET", "")).replace(',', '').strip()
        target_pct_raw = str(trade_data.get("TARGET_PERCENT", "")).replace('%', '').strip()
        stop_loss_raw = str(trade_data.get("STOP_LOSS", "")).replace(',', '').strip()
        stop_loss_pct_raw = str(trade_data.get("STOP_LOSS_PERCENT", "")).replace('%', '').strip()
        reason = trade_data.get("Reason", "")
        
        # Get the next row number for formulas
        next_row = len(sheet.get_all_values()) + 1
        
        # --- Buying Price Logic ---
        # If AI extracted a buying price, use it (Recommended).
        # Otherwise use GOOGLEFINANCE historical price at the date of analysis (Auto).
        if buying_price_raw and buying_price_raw != "":
            buying_price = buying_price_raw
            price_source = "Recommended"
        else:
            # Auto-populate from Google Finance using the historical price at trade date
            # E{next_row} = Date Analysed column
            buying_price = f'=INDEX(GOOGLEFINANCE(A{next_row}, "price", E{next_row}), 2, 2)'
            price_source = "Auto (Market Price)"
        
        # --- Target Price Logic ---
        if target_raw and target_raw != "":
            # Absolute target price was given
            target = target_raw
        elif target_pct_raw and target_pct_raw != "":
            # Target percentage was given — calculate: Buying Price * (1 + pct/100)
            # C{next_row} is the Buying Price column
            target = f"=C{next_row}*(1+{target_pct_raw}/100)"
        else:
            target = ""
        
        # --- Stop Loss Logic ---
        if stop_loss_raw and stop_loss_raw != "":
            # Absolute stop loss was given
            stop_loss = stop_loss_raw
        elif stop_loss_pct_raw and stop_loss_pct_raw != "":
            # Stop loss percentage was given — calculate: Buying Price * (1 - pct/100)
            # C{next_row} is the Buying Price column
            stop_loss = f"=C{next_row}*(1-{stop_loss_pct_raw}/100)"
        else:
            stop_loss = ""
        
        # Column G = Current Price using Google Finance
        current_price_formula = f'=GOOGLEFINANCE(A{next_row}, "price")'
        
        # Gain = (Current Price - Buying Price) / Buying Price
        # Columns: A=Name, B=Gain, C=Buy, D=Target, E=Date, F=Reason, G=Current Price, H=Price Source, I=Stop Loss
        gain_formula = f"=(G{next_row}-C{next_row})/C{next_row}"
        
        row_values = [
            name,
            gain_formula,
            buying_price,
            target,
            date_analysed,
            reason,
            current_price_formula,
            price_source,
            stop_loss
        ]
        
        # Append the row, value_input_option='USER_ENTERED' allows formulas to be parsed
        sheet.append_row(row_values, value_input_option='USER_ENTERED')
        
        source_info = f"[{price_source}]" if price_source == "Auto (Market Price)" else ""
        print(f"Successfully added trade for {name} to Google Sheets! {source_info}")
        
    except Exception as e:
        print(f"Failed to append to Google Sheets: {e}")

async def append_to_sheet_async(trade_data, date_analysed):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, append_to_sheet_sync, trade_data, date_analysed)
