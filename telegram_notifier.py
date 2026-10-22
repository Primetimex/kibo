"""
Telegram Scheduled Notification Bot
------------------------------------
Sends you a Telegram message on a daily schedule.

SETUP:
1. Fill in BOT_TOKEN and CHAT_ID below.
2. Install dependency:   pip install requests schedule
3. Test it immediately:  python telegram_notifier.py --test
4. Run for real:         python telegram_notifier.py
   (leave it running — e.g. in a screen session, systemd service,
   Docker container, or on a small always-on server / Raspberry Pi)

IMPORTANT — before this will work:
Telegram won't let a bot message you until YOU message the bot first.
1. Open Telegram, find your bot, send it any message (e.g. "hi").
2. Then visit this URL in your browser (with your real token):
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
3. Find "chat":{"id": ...} in the response and copy that number into CHAT_ID below.
"""

import sys
import requests
import schedule
import time
from datetime import datetime

# ---- CONFIG ----
BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"   # from BotFather (regenerate after exposure!)
CHAT_ID = "PUT_YOUR_CHAT_ID_HERE"       # from the getUpdates step
SEND_TIME = "09:00"                     # 24-hour format, your local time
MESSAGE = "🔔 Good morning! This is your daily reminder."

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def check_config():
    """Catch the most common setup mistakes before making a request."""
    problems = []
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        problems.append("BOT_TOKEN is not set.")
    if not CHAT_ID or CHAT_ID == "PUT_YOUR_CHAT_ID_HERE":
        problems.append("CHAT_ID is not set.")
    if problems:
        print("Config problem(s):")
        for p in problems:
            print(f"  - {p}")
        print("\nEdit BOT_TOKEN and CHAT_ID at the top of this file, then try again.")
        sys.exit(1)


def send_notification(message: str = MESSAGE):
    """Send a message to the configured chat."""
    try:
        response = requests.post(
            API_URL,
            data={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
        if response.status_code == 403:
            print(f"[{datetime.now()}] Failed to send: 403 Forbidden.")
            print("This means Telegram is blocking the bot from messaging this chat.")
            print("Fix: open Telegram, message your bot directly first (e.g. 'hi'),")
            print("then re-fetch your chat ID from:")
            print(f"  https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
            return False
        response.raise_for_status()
        print(f"[{datetime.now()}] Notification sent successfully.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"[{datetime.now()}] Failed to send notification: {e}")
        return False


def main():
    check_config()

    # Test mode: send one message immediately and exit, no waiting for schedule
    if "--test" in sys.argv:
        print("Running in test mode — sending one message now...")
        ok = send_notification("✅ Test message — your notifier bot is working!")
        sys.exit(0 if ok else 1)

    print(f"Notification bot started. Scheduled daily at {SEND_TIME}.")
    schedule.every().day.at(SEND_TIME).do(send_notification)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
