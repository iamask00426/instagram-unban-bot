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
    raw_tg = (
        os.getenv("BOT_TOKEN") or
        os.getenv("TELEGRAMBOT") or
        os.getenv("TELEGRAM_BOT_TOKEN") or
        os.getenv("TG_BOT_TOKEN") or
        ""
    ).strip()
    raw_dc = os.getenv("DISCORD_BOT_TOKEN", "").strip()

    tg_token = raw_tg.strip('"').strip("'").strip()
    dc_token = raw_dc.strip('"').strip("'").strip()

    # Pass sanitized tokens back to environment for child processes
    if tg_token:
        os.environ["BOT_TOKEN"] = tg_token
    if dc_token:
        os.environ["DISCORD_BOT_TOKEN"] = dc_token

    has_tg = bool(tg_token and not tg_token.startswith("your_") and ":" in tg_token)
    has_dc = bool(dc_token and not dc_token.startswith("your_"))

    logging.info(
        f"Environment check -> BOT_TOKEN: {'Configured (' + str(len(tg_token)) + ' chars)' if has_tg else 'Disabled/Missing'}, "
        f"DISCORD_BOT_TOKEN: {'Configured (' + str(len(dc_token)) + ' chars)' if has_dc else 'Disabled/Missing'}"
    )

    # Telegram Bot
    if has_tg:
        logging.info("Starting Telegram Bot (bot.py)...")
        p_tg = subprocess.Popen([sys.executable, "bot.py"])
        procs.append({"name": "Telegram Bot", "proc": p_tg, "script": "bot.py", "restarts": 0, "last_start": time.time()})

    # Discord Bot
    if has_dc:
        logging.info("Starting Discord Bot (discord_bot.py)...")
        p_dc = subprocess.Popen([sys.executable, "discord_bot.py"])
        procs.append({"name": "Discord Bot", "proc": p_dc, "script": "discord_bot.py", "restarts": 0, "last_start": time.time()})

    if not procs:
        logging.error("No valid bot tokens configured (BOT_TOKEN or DISCORD_BOT_TOKEN).")
        logging.error("Please add BOT_TOKEN or DISCORD_BOT_TOKEN in environment variables.")
        time.sleep(10)
        sys.exit(1)

    logging.info(f"Runner active with {len(procs)} service(s). Monitoring health every 3s...")

    def shutdown(signum=None, frame=None):
        logging.info("Stopping all child bot processes...")
        for item in procs:
            try:
                item["proc"].terminate()
            except Exception:
                pass
        sys.exit(0)

    import signal
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            time.sleep(3)
            for item in procs:
                poll = item["proc"].poll()
                if poll is not None:
                    item["restarts"] += 1
                    uptime = time.time() - item["last_start"]
                    # If process died in < 10s, it likely has a fatal config error — wait longer
                    delay = 3 if uptime > 30 else 15
                    logging.warning(
                        f"[{item['name']}] exited with code {poll} after {uptime:.0f}s "
                        f"(restart #{item['restarts']}). Restarting in {delay}s..."
                    )
                    time.sleep(delay)
                    item["proc"] = subprocess.Popen([sys.executable, item["script"]])
                    item["last_start"] = time.time()
                    logging.info(f"[{item['name']}] restarted (PID: {item['proc'].pid})")
    except KeyboardInterrupt:
        shutdown()

if __name__ == "__main__":
    main()
