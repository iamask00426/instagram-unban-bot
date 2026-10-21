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

    # Clean and unquote tokens in case quotes were passed
    raw_tg = os.getenv("BOT_TOKEN", "").strip()
    raw_dc = os.getenv("DISCORD_BOT_TOKEN", "").strip()

    tg_token = raw_tg.strip('"').strip("'").strip()
    dc_token = raw_dc.strip('"').strip("'").strip()

    # Pass sanitized tokens back to environment for child processes
    if tg_token:
        os.environ["BOT_TOKEN"] = tg_token
    if dc_token:
        os.environ["DISCORD_BOT_TOKEN"] = dc_token

    has_tg = bool(tg_token and not tg_token.startswith("your_"))
    has_dc = bool(dc_token and not dc_token.startswith("your_"))

    logging.info(f"Environment check -> BOT_TOKEN: {'Configured (' + str(len(tg_token)) + ' chars)' if has_tg else 'Missing/Placeholder'}, DISCORD_BOT_TOKEN: {'Configured (' + str(len(dc_token)) + ' chars)' if has_dc else 'Missing/Placeholder'}")

    # 1. Telegram Bot
    if has_tg:
        logging.info("Starting Telegram Bot (bot.py)...")
        p_tg = subprocess.Popen([sys.executable, "bot.py"])
        procs.append({"name": "Telegram Bot", "proc": p_tg, "script": "bot.py"})

    # 2. Discord Bot
    if has_dc:
        logging.info("Starting Discord Bot (discord_bot.py)...")
        p_dc = subprocess.Popen([sys.executable, "discord_bot.py"])
        procs.append({"name": "Discord Bot", "proc": p_dc, "script": "discord_bot.py"})

    if not procs:
        logging.error("No valid bot tokens configured (BOT_TOKEN / DISCORD_BOT_TOKEN).")
        logging.error("Please add BOT_TOKEN or DISCORD_BOT_TOKEN in Railway service Variables tab.")
        time.sleep(10)
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
