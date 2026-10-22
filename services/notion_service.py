import requests
import asyncio
import zoneinfo
from config import NOTION_API_KEY, NOTION_PAGE_ID

def send_to_notion_sync(text, message_date_utc, is_new_day):
    """Synchronous function to append a message block to a Notion page."""
    if not all([NOTION_API_KEY, NOTION_PAGE_ID]):
        print("Notion configuration is incomplete. Skipping Notion logging.")
        return

    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # Use IST timezone
    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    message_date_ist = message_date_utc.astimezone(ist_zone)
    formatted_time = message_date_ist.strftime("%Y-%m-%d %H:%M:%S")
    formatted_text = f"[{formatted_time}] {text}"
    
    children = []
    if is_new_day:
        children.append({
            "object": "block",
            "type": "divider",
            "divider": {}
        })
        
    children.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": formatted_text
                    }
                }
            ]
        }
    })
    
    data = { "children": children }
    
    try:
        response = requests.patch(url, headers=headers, json=data)
        response.raise_for_status()
        print("Successfully logged message to Notion.")
    except Exception as e:
        print(f"Failed to log to Notion: {e}")
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            print(f"Notion API Error: {e.response.text}")

async def send_to_notion_async(text, message_date_utc, is_new_day):
    """Asynchronously run the blocking Notion API function."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_to_notion_sync, text, message_date_utc, is_new_day)
