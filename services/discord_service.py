import requests
import asyncio
import os


def send_to_discord_sync(text, message=None, media_path=None, webhook_url=None):
    """
    Synchronous function to post a message (and optionally media) to a Discord Webhook.
    
    Accepts either a pre-downloaded media_path OR a raw Telethon message object.
    If a message object is provided (and has media), it downloads the media here
    — off the critical path of the Telegram event handler.
    """
    if not webhook_url:
        # Silently skip if no webhook URL is provided
        return

    downloaded_path = media_path
    if downloaded_path is None and message is not None and hasattr(message, 'media') and message.media:
        print("[Discord] Warning: message object passed to sync function. Media should be downloaded in async wrapper.")

    data = {"content": text}
    try:
        if downloaded_path and os.path.exists(downloaded_path):
            with open(downloaded_path, "rb") as f:
                response = requests.post(webhook_url, data=data, files={"file": f})
            # Cleanup the local file after upload
            try:
                os.remove(downloaded_path)
                print(f"Cleaned up temporary file: {downloaded_path}")
            except Exception as cleanup_err:
                print(f"Failed to delete temporary file {downloaded_path}: {cleanup_err}")
        else:
            response = requests.post(webhook_url, json=data)
        
        response.raise_for_status()
        print("Successfully logged message to Discord.")
    except Exception as e:
        print(f"Failed to log to Discord: {e}")
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            print(f"Discord API Error: {e.response.text}")


async def send_to_discord_async(text, message=None, media_path=None, webhook_url=None):
    """Asynchronously run the blocking Discord API function."""
    downloaded_path = media_path
    
    # Download media in the main event loop before passing to the thread executor
    if downloaded_path is None and message is not None and hasattr(message, 'media') and message.media:
        try:
            if not os.path.exists("temp_media"):
                os.makedirs("temp_media")
            downloaded_path = await message.download_media(file="temp_media/")
            print(f"[Discord] Downloaded media to: {downloaded_path}")
        except Exception as e:
            print(f"[Discord] Failed to download media for Discord: {e}")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_to_discord_sync, text, None, downloaded_path, webhook_url)
