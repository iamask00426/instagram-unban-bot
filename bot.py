import os
import time
import threading
import logging
import sys
import requests
from dotenv import load_dotenv
import telebot

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    logging.error("❌ ERROR: BOT_TOKEN is missing or invalid in Environment Variables!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Active monitoring jobs
# Key: f"{chat_id}_{username}", Value: {'event': threading.Event, 'mode': 'unban'|'ban', 'start_time': float, 'username': str}
active_monitors = {}


def format_followers(count) -> str:
    """Format follower numbers as 710.0k, 10.2k, 3.0m, etc."""
    try:
        num = float(count)
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}m"
        elif num >= 1_000:
            return f"{num / 1_000:.1f}k"
        else:
            return f"{int(num)}"
    except (ValueError, TypeError):
        return str(count)


def check_instagram_status(username: str) -> dict:
    """
    Check if Instagram account is active or banned.
    Returns: {'is_active': bool, 'followers': str}
    """
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    headers = {
        "User-Agent": "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; Samsung; SM-G998B; qcom; en_US; 454749221)",
        "X-IG-App-ID": "936619743392459",
        "Accept": "*/*",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code == 200:
            data = response.json()
            user = data.get("data", {}).get("user")
            if user:
                count = user.get("edge_followed_by", {}).get("count", 0)
                return {"is_active": True, "followers": format_followers(count)}
            return {"is_active": True, "followers": "0"}
        else:
            # 404, 400, 403 or non-200 -> Banned / Disabled
            return {"is_active": False, "followers": "0"}
    except Exception as e:
        logging.error(f"Error checking status for {username}: {e}")
        return {"is_active": False, "followers": "0"}


def format_duration(seconds: float) -> str:
    sec = int(seconds)
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    secs = sec % 60
    return f"{hours}h {minutes}m {secs}s"


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "🤖 **Truzd Instagram Monitor Bot**\n\n"
        "Commands:\n"
        "• `/monitor <username>` — Track single IG account for UNBAN.\n"
        "• `/banmonitor <username>` — Track active IG account for BAN.\n"
        "• `/bulk <user1> <user2> ...` — Add multiple IG accounts at once.\n"
        "• `/stop <username>` — Stop monitoring a username.\n"
        "• `/active` — View all active monitoring tasks."
    )
    bot.reply_to(message, welcome_text)


@bot.message_handler(commands=['monitor'])
def handle_monitor(message):
    args = message.text.split()[1:]
    if not args:
        bot.reply_to(message, "❌ Usage: `/monitor <username>`")
        return

    username = args[0].replace("@", "").strip().lower()
    chat_id = message.chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        bot.reply_to(message, f"⚠️ Already monitoring @{username}!")
        return

    status = check_instagram_status(username)
    if status["is_active"]:
        bot.reply_to(message, f"ℹ️ @{username} is already active! Use `/banmonitor` if you want to track bans.")
        return

    bot.reply_to(message, f"🔍 **Monitoring @{username} started!**")

    stop_event = threading.Event()
    start_time = time.time()

    def worker():
        while not stop_event.is_set():
            time.sleep(5)
            st = check_instagram_status(username)
            if st["is_active"]:
                elapsed = format_duration(time.time() - start_time)
                alert_msg = (
                    f"✅ **Username unbanned!**\n\n"
                    f"@{username} is now active again — [View Profile](https://instagram.com/{username})\n"
                    f"Followers: {st['followers']}\n"
                    f"Time elapsed: {elapsed}"
                )
                bot.send_message(chat_id, alert_msg, disable_web_page_preview=False)
                active_monitors.pop(task_key, None)
                break

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    active_monitors[task_key] = {"event": stop_event, "mode": "unban", "start_time": start_time, "username": username}


@bot.message_handler(commands=['banmonitor'])
def handle_banmonitor(message):
    args = message.text.split()[1:]
    if not args:
        bot.reply_to(message, "❌ Usage: `/banmonitor <username>`")
        return

    username = args[0].replace("@", "").strip().lower()
    chat_id = message.chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        bot.reply_to(message, f"⚠️ Already monitoring @{username}!")
        return

    status = check_instagram_status(username)
    if not status["is_active"]:
        bot.reply_to(message, f"⚠️ @{username} is already banned or does not exist!")
        return

    bot.reply_to(message, f"⚡ **Super-fast ban monitoring active for @{username}!**")

    stop_event = threading.Event()
    start_time = time.time()

    def worker():
        while not stop_event.is_set():
            time.sleep(5)
            st = check_instagram_status(username)
            if not st["is_active"]:
                elapsed = format_duration(time.time() - start_time)
                alert_msg = (
                    f"🚨 **Super-Fast Ban Alert!**\n\n"
                    f"@{username} has been **BANNED/DISABLED**!\n"
                    f"Time elapsed: {elapsed}\n\n"
                    f"🔗 [View Profile](https://instagram.com/{username})"
                )
                bot.send_message(chat_id, alert_msg, disable_web_page_preview=False)
                active_monitors.pop(task_key, None)
                break

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    active_monitors[task_key] = {"event": stop_event, "mode": "ban", "start_time": start_time, "username": username}


@bot.message_handler(commands=['bulk'])
def handle_bulk(message):
    args = message.text.split()[1:]
    if not args:
        bot.reply_to(message, "❌ Usage: `/bulk user1 user2 user3`")
        return

    raw_input = " ".join(args)
    raw_list = raw_input.replace(",", " ").split()
    usernames = list(dict.fromkeys([u.replace("@", "").strip().lower() for u in raw_list if u.strip()]))

    chat_id = message.chat.id
    started = []
    skipped = []

    for username in usernames:
        task_key = f"{chat_id}_{username}"
        if task_key in active_monitors:
            skipped.append(f"@{username} (Already monitoring)")
            continue

        status = check_instagram_status(username)
        if status["is_active"]:
            skipped.append(f"@{username} (Already active)")
            continue

        stop_event = threading.Event()
        start_time = time.time()

        def make_worker(u=username, st_time=start_time, key=task_key, ev=stop_event):
            while not ev.is_set():
                time.sleep(5)
                st = check_instagram_status(u)
                if st["is_active"]:
                    elapsed = format_duration(time.time() - st_time)
                    alert_msg = (
                        f"✅ **Username unbanned!**\n\n"
                        f"@{u} is now active again — [View Profile](https://instagram.com/{u})\n"
                        f"Followers: {st['followers']}\n"
                        f"Time elapsed: {elapsed}"
                    )
                    bot.send_message(chat_id, alert_msg, disable_web_page_preview=False)
                    active_monitors.pop(key, None)
                    break

        thread = threading.Thread(target=make_worker, daemon=True)
        thread.start()
        active_monitors[task_key] = {"event": stop_event, "mode": "unban", "start_time": start_time, "username": username}
        started.append(f"@{username}")

    msg_parts = []
    if started:
        msg_parts.append(f"🔍 **Bulk Monitoring Started ({len(started)} accounts):**\n" + ", ".join(started))
    if skipped:
        msg_parts.append(f"⚠️ **Skipped ({len(skipped)} accounts):**\n" + "\n".join(skipped))

    bot.reply_to(message, "\n\n".join(msg_parts))


@bot.message_handler(commands=['stop'])
def handle_stop(message):
    args = message.text.split()[1:]
    if not args:
        bot.reply_to(message, "❌ Usage: `/stop <username>`")
        return

    username = args[0].replace("@", "").strip().lower()
    chat_id = message.chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        active_monitors[task_key]["event"].set()
        active_monitors.pop(task_key, None)
        bot.reply_to(message, f"🛑 Stopped monitoring @{username}.")
    else:
        bot.reply_to(message, f"❌ Not currently monitoring @{username}.")


@bot.message_handler(commands=['active'])
def handle_active(message):
    chat_id = message.chat.id
    user_tasks = [v for k, v in active_monitors.items() if k.startswith(f"{chat_id}_")]

    if not user_tasks:
        bot.reply_to(message, "ℹ️ No active monitoring tasks.")
        return

    lines = ["📊 **Active Monitors:**\n"]
    for t in user_tasks:
        mode_icon = "✅ Unban" if t["mode"] == "unban" else "🚨 Ban"
        elapsed = format_duration(time.time() - t["start_time"])
        lines.append(f"• @{t['username']} ({mode_icon}) - Running for {elapsed}")

    bot.reply_to(message, "\n".join(lines))


if __name__ == "__main__":
    logging.info("⚡ Truzd Monitor Bot is starting...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
