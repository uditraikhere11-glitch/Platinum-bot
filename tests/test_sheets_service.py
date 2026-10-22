import pytest
import asyncio
from unittest.mock import patch, MagicMock

with patch("config.GOOGLE_SHEET_ID", "test-sheet-id"):
    from services.google_sheets_service import append_to_sheet_async

@pytest.mark.asyncio
@patch("os.path.exists")
@patch("os.environ.get")
@patch("gspread.oauth")
async def test_append_to_sheet(mock_oauth, mock_env_get, mock_exists):
    # Mock exists to return True for the oauth files
    mock_exists.return_value = True
    
    # Mock environment variable to be empty
    mock_env_get.return_value = None
    
    # Mock the gspread client and sheet
    mock_client = MagicMock()
    mock_sheet = MagicMock()
    mock_oauth.return_value = mock_client
    mock_client.open_by_key.return_value.sheet1 = mock_sheet
    
    # Mock the sheet length to be 10 so next row is 11
    mock_sheet.get_all_values.return_value = [[]] * 10
    
    trade_data = {
        "NAME": "NSE:RELIANCE",
        "Buying Price": "2500",
        "TARGET": "2600",
        "TARGET_PERCENT": "",
        "STOP_LOSS": "",
        "STOP_LOSS_PERCENT": "",
        "Reason": "Good earnings"
    }
    
    await append_to_sheet_async(trade_data, "2026-07-20 10:00:00")
    
    # Assert oauth was called with right files
    mock_oauth.assert_called_once_with(
        credentials_filename="temp/client_secret.json",
        authorized_user_filename="temp/authorized_user.json"
    )
    
    # Assert row appended with correct data and formulas
    # next_row should be 11
    # Columns: A=Name, B=Gain, C=Buy, D=Target, E=Date, F=Reason, G=Current Price, H=Price Source, I=Stop Loss
    expected_row = [
        "NSE:RELIANCE",
        "=(G11-C11)/C11",  # gain_formula
        "2500",            # buying_price
        "2600",            # target
        "2026-07-20 10:00:00",
        "Good earnings",
        '=GOOGLEFINANCE(A11, "price")',  # current_price_formula
        "Recommended",     # price_source (since buying price was provided)
        ""                 # stop_loss (not provided)
    ]
    mock_sheet.append_row.assert_called_once_with(expected_row, value_input_option='USER_ENTERED')
