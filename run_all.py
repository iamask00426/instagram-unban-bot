import os
import sys
import subprocess
import time
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def main():
    procs = []

    # 1. Telegram Bot
    tg_token = os.getenv("BOT_TOKEN", "").strip()
    if tg_token and not tg_token.startswith("your_"):
        logging.info("Starting Telegram Bot (bot.py)...")
        p_tg = subprocess.Popen([sys.executable, "bot.py"])
        procs.append({"name": "Telegram Bot", "proc": p_tg, "script": "bot.py"})

    # 2. Discord Bot
    dc_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if dc_token and not dc_token.startswith("your_"):
        logging.info("Starting Discord Bot (discord_bot.py)...")
        p_dc = subprocess.Popen([sys.executable, "discord_bot.py"])
        procs.append({"name": "Discord Bot", "proc": p_dc, "script": "discord_bot.py"})

    if not procs:
        logging.error("No valid bot tokens configured (BOT_TOKEN / DISCORD_BOT_TOKEN).")
        sys.exit(1)

    logging.info(f"Runner active with {len(procs)} service(s). Monitoring health...")

    try:
        while True:
            for item in procs:
                poll = item["proc"].poll()
                if poll is not None:
                    logging.warning(f"{item['name']} exited with code {poll}. Restarting in 5s...")
                    time.sleep(5)
                    item["proc"] = subprocess.Popen([sys.executable, item["script"]])
            time.sleep(3)
    except KeyboardInterrupt:
        logging.info("Stopping all services...")
        for item in procs:
            item["proc"].terminate()

if __name__ == "__main__":
    main()
