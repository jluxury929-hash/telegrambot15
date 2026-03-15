"""
Verify TELEGRAM_BOT_TOKEN from .env connects to Telegram Bot API.
Run: python check_telegram.py
"""
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN or TOKEN.strip() == "":
    print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
    exit(1)

TOKEN = TOKEN.strip()
print("Testing Telegram Bot API connection...")
print("-" * 50)

try:
    import requests
except ImportError:
    print("ERROR: Install requests (pip install requests python-dotenv)")
    exit(1)

url = f"https://api.telegram.org/bot{TOKEN}/getMe"
r = requests.get(url, timeout=10)
r.raise_for_status()
data = r.json()

if not data.get("ok"):
    print("ERROR: Telegram API returned not OK.")
    print(data)
    exit(1)

bot = data.get("result", {})
username = bot.get("username", "?")
name = bot.get("first_name", "?")
id_ = bot.get("id", "?")
print(f"1. Bot connected: OK")
print(f"2. Bot username: @{username}")
print(f"3. Bot name: {name}")
print(f"4. Bot ID: {id_}")
print("-" * 50)
print("Telegram connection is working. You can start the bot and message it.")
