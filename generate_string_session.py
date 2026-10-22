from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = os.environ.get('TG_API_ID')
API_HASH = os.environ.get('TG_API_HASH')

if not API_ID or not API_HASH:
    print("Error: Could not find TG_API_ID or TG_API_HASH in your .env file.")
    exit(1)

print("Starting Telegram Client to generate a String Session...")
print("You will need to log in again (Phone Number + Telegram Code).")
print("-" * 50)

with TelegramClient(StringSession(), int(API_ID), API_HASH) as client:
    session_string = client.session.save()
    print("-" * 50)
    print("SUCCESS! Here is your String Session:")
    print("👇👇👇 COPY THE TEXT BELOW 👇👇👇\n")
    print(session_string)
    print("\n👆👆👆 COPY THE TEXT ABOVE 👆👆👆")
    print("-" * 50)
    print("Add this to your Koyeb Environment Variables with the key: TG_SESSION_STRING")
