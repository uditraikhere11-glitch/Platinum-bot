import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# We must mock config before importing the service
with patch("config.VERTEX_PROJECT_ID", "test-project"), \
     patch("config.VERTEX_LOCATION", "us-central1"):
    # Mock genai.Client initialization
    with patch("google.genai.Client"):
        from services.gemini_service import extract_trade_async

# --- Gemini Service Tests ---

@pytest.mark.asyncio
async def test_extract_trade_async_valid_trade_with_price():
    """Trade with explicit buying price and absolute target."""
    mock_response = MagicMock()
    mock_response.text = '{"is_trade": true, "NAME": "NSE:TCS", "Buying Price": "3500", "TARGET": "3850", "TARGET_PERCENT": "", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "", "Reason": "Breakout"}'
    
    with patch("services.gemini_service._client") as mock_client_instance:
        mock_client_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)
        
        result = await extract_trade_async("Buy TCS at 3500 target 3850")
        
        assert result is not None
        assert result["is_trade"] is True
        assert result["NAME"] == "NSE:TCS"
        assert result["Buying Price"] == "3500"
        assert result["TARGET"] == "3850"
        assert result["TARGET_PERCENT"] == ""

@pytest.mark.asyncio
async def test_extract_trade_async_no_buying_price():
    """Trade without explicit buying price — should still be a valid trade."""
    mock_response = MagicMock()
    mock_response.text = '{"is_trade": true, "NAME": "NSE:RELIANCE", "Buying Price": "", "TARGET": "2800", "TARGET_PERCENT": "", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "", "Reason": "Strong support"}'
    
    with patch("services.gemini_service._client") as mock_client_instance:
        mock_client_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)
        
        result = await extract_trade_async("Reliance looks good, target 2800")
        
        assert result is not None
        assert result["Buying Price"] == ""
        assert result["TARGET"] == "2800"

@pytest.mark.asyncio
async def test_extract_trade_async_target_percentage():
    """Trade with target as a percentage."""
    mock_response = MagicMock()
    mock_response.text = '{"is_trade": true, "NAME": "NSE:INFY", "Buying Price": "1500", "TARGET": "", "TARGET_PERCENT": "15", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "", "Reason": "Earnings beat"}'
    
    with patch("services.gemini_service._client") as mock_client_instance:
        mock_client_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)
        
        result = await extract_trade_async("Buy Infosys at 1500, 15% upside expected")
        
        assert result is not None
        assert result["TARGET"] == ""
        assert result["TARGET_PERCENT"] == "15"

@pytest.mark.asyncio
async def test_extract_trade_async_not_trade():
    mock_response = MagicMock()
    mock_response.text = '{"is_trade": false}'
    
    with patch("services.gemini_service._client") as mock_client_instance:
        mock_client_instance.aio.models.generate_content = AsyncMock(return_value=mock_response)
        
        result = await extract_trade_async("Hello how are you?")
        
        assert result is None

@pytest.mark.asyncio
async def test_extract_trade_async_no_client():
    with patch("services.gemini_service._client", None):
        result = await extract_trade_async("Buy Reliance at 2500")
        assert result is None


# --- Google Sheets Service Tests ---

with patch("config.GOOGLE_SHEET_ID", "test-sheet-id"):
    from services.google_sheets_service import append_to_sheet_sync

def test_sheets_recommended_price():
    """When buying price is provided, Price Source should be 'Recommended'."""
    trade_data = {"NAME": "NSE:TCS", "Buying Price": "3500", "TARGET": "3850", "TARGET_PERCENT": "", "STOP_LOSS": "3300", "STOP_LOSS_PERCENT": "", "Reason": "Breakout"}
    
    with patch("services.google_sheets_service.gspread") as mock_gspread, \
         patch.dict("os.environ", {"AUTHORIZED_USER_JSON": '{"refresh_token":"x","token_uri":"y","client_id":"z","client_secret":"w"}'}):
        
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [["header"]]
        mock_gspread.authorize.return_value.open_by_key.return_value.sheet1 = mock_sheet
        
        append_to_sheet_sync(trade_data, "2026-07-20 02:00:00")
        
        mock_sheet.append_row.assert_called_once()
        row = mock_sheet.append_row.call_args[0][0]
        # row: [name, gain, buying_price, target, date, reason, current_price, price_source, stop_loss]
        assert row[2] == "3500"  # Buying price
        assert row[7] == "Recommended"  # Price Source
        assert row[8] == "3300"  # Stop Loss

def test_sheets_auto_price():
    """When buying price is missing, should use GOOGLEFINANCE and 'Auto (Market Price)'."""
    trade_data = {"NAME": "NSE:RELIANCE", "Buying Price": "", "TARGET": "2800", "TARGET_PERCENT": "", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "", "Reason": "Support"}
    
    with patch("services.google_sheets_service.gspread") as mock_gspread, \
         patch.dict("os.environ", {"AUTHORIZED_USER_JSON": '{"refresh_token":"x","token_uri":"y","client_id":"z","client_secret":"w"}'}):
        
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [["header"]]
        mock_gspread.authorize.return_value.open_by_key.return_value.sheet1 = mock_sheet
        
        append_to_sheet_sync(trade_data, "2026-07-20 02:00:00")
        
        mock_sheet.append_row.assert_called_once()
        row = mock_sheet.append_row.call_args[0][0]
        assert "INDEX(GOOGLEFINANCE" in row[2]  # Buying price uses historical price formula
        assert row[7] == "Auto (Market Price)"  # Price Source

def test_sheets_target_percentage():
    """When target percentage is given, target should be a formula."""
    trade_data = {"NAME": "NSE:INFY", "Buying Price": "1500", "TARGET": "", "TARGET_PERCENT": "15", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "", "Reason": "Earnings"}
    
    with patch("services.google_sheets_service.gspread") as mock_gspread, \
         patch.dict("os.environ", {"AUTHORIZED_USER_JSON": '{"refresh_token":"x","token_uri":"y","client_id":"z","client_secret":"w"}'}):
        
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [["header"]]
        mock_gspread.authorize.return_value.open_by_key.return_value.sheet1 = mock_sheet
        
        append_to_sheet_sync(trade_data, "2026-07-20 02:00:00")
        
        mock_sheet.append_row.assert_called_once()
        row = mock_sheet.append_row.call_args[0][0]
        assert "15/100" in row[3]  # Target is a formula with the percentage
        assert row[2] == "1500"  # Buying price stays as-is

def test_sheets_absolute_stop_loss():
    """When absolute stop loss is given, it should appear directly in column I."""
    trade_data = {"NAME": "NSE:HDFC", "Buying Price": "1600", "TARGET": "1800", "TARGET_PERCENT": "", "STOP_LOSS": "1500", "STOP_LOSS_PERCENT": "", "Reason": "Support"}
    
    with patch("services.google_sheets_service.gspread") as mock_gspread, \
         patch.dict("os.environ", {"AUTHORIZED_USER_JSON": '{"refresh_token":"x","token_uri":"y","client_id":"z","client_secret":"w"}'}):
        
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [["header"]]
        mock_gspread.authorize.return_value.open_by_key.return_value.sheet1 = mock_sheet
        
        append_to_sheet_sync(trade_data, "2026-07-20 02:00:00")
        
        mock_sheet.append_row.assert_called_once()
        row = mock_sheet.append_row.call_args[0][0]
        assert row[8] == "1500"  # Stop Loss absolute

def test_sheets_stop_loss_percentage():
    """When stop loss percentage is given, it should be a formula: BuyPrice * (1 - pct/100)."""
    trade_data = {"NAME": "NSE:SBIN", "Buying Price": "800", "TARGET": "900", "TARGET_PERCENT": "", "STOP_LOSS": "", "STOP_LOSS_PERCENT": "5", "Reason": "Breakout"}
    
    with patch("services.google_sheets_service.gspread") as mock_gspread, \
         patch.dict("os.environ", {"AUTHORIZED_USER_JSON": '{"refresh_token":"x","token_uri":"y","client_id":"z","client_secret":"w"}'}):
        
        mock_sheet = MagicMock()
        mock_sheet.get_all_values.return_value = [["header"]]
        mock_gspread.authorize.return_value.open_by_key.return_value.sheet1 = mock_sheet
        
        append_to_sheet_sync(trade_data, "2026-07-20 02:00:00")
        
        mock_sheet.append_row.assert_called_once()
        row = mock_sheet.append_row.call_args[0][0]
        assert "1-5/100" in row[8]  # Stop Loss is a formula with the percentage
