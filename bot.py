import os
import time
import asyncio
import logging
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Storage for active monitoring tasks
# Key: f"{chat_id}_{username}", Value: {'task': asyncio.Task, 'mode': 'unban'|'ban', 'start_time': float}
active_monitors = {}


def check_instagram_status(username: str) -> dict:
    """
    Check if Instagram account is active or banned.
    Returns:
        {
            'is_active': bool,
            'followers': str,
            'status_code': int
        }
    """
    url = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code == 200:
            followers = "Active"
            try:
                data = response.json()
                user = data.get("graphql", {}).get("user", {}) or data.get("data", {}).get("user", {})
                follower_count = user.get("edge_followed_by", {}).get("count")
                if follower_count is not None:
                    followers = f"{follower_count:,}"
            except Exception:
                pass
            return {"is_active": True, "followers": followers, "status_code": 200}
        elif response.status_code in [404, 400]:
            return {"is_active": False, "followers": "0", "status_code": response.status_code}
        else:
            return {"is_active": True, "followers": "Unknown", "status_code": response.status_code}
    except Exception as e:
        logging.error(f"Error checking Instagram status for {username}: {e}")
        return {"is_active": True, "followers": "Unknown", "status_code": 0}


def format_duration(seconds: float) -> str:
    sec = int(seconds)
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    secs = sec % 60
    return f"{hours}h {minutes}m {secs}s"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 **Truzd Instagram Monitor Bot**\n\n"
        "Commands:\n"
        "• `/monitor <username>` — Monitor disabled account until UNBANNED.\n"
        "• `/banmonitor <username>` — Monitor active account until BANNED.\n"
        "• `/stop <username>` — Stop monitoring a username.\n"
        "• `/active` — View all active monitoring tasks."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def monitor_unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /monitor <username> (Unban Detection)"""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/monitor <username>`", parse_mode="Markdown")
        return

    username = context.args[0].replace("@", "").strip().lower()
    chat_id = update.effective_chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        await update.message.reply_text(f"⚠️ Already monitoring @{username}!")
        return

    # Check initial status
    status = check_instagram_status(username)
    if status["is_active"]:
        await update.message.reply_text(f"ℹ️ @{username} is already active! Use `/banmonitor` if you want to track bans.")
        return

    await update.message.reply_text(f"🔍 **Monitoring @{username} started!** (Waiting for Unban)", parse_mode="Markdown")

    start_time = time.time()

    async def unban_loop():
        while True:
            await asyncio.sleep(5)
            st = check_instagram_status(username)
            if st["is_active"]:
                elapsed = format_duration(time.time() - start_time)
                alert_msg = (
                    f"✅ **Username unbanned!**\n\n"
                    f"@{username} is active again — [View Profile](https://instagram.com/{username})\n"
                    f"Followers: {st['followers']}\n"
                    f"Time elapsed: {elapsed}"
                )
                await context.bot.send_message(chat_id=chat_id, text=alert_msg, parse_mode="Markdown", disable_web_page_preview=False)
                active_monitors.pop(task_key, None)
                break

    task = asyncio.create_task(unban_loop())
    active_monitors[task_key] = {"task": task, "mode": "unban", "start_time": start_time, "username": username}


async def monitor_ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /banmonitor <username> (Ban Detection)"""
    if not context.args:
        await update.message.reply_text("❌ Usage: `/banmonitor <username>`", parse_mode="Markdown")
        return

    username = context.args[0].replace("@", "").strip().lower()
    chat_id = update.effective_chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        await update.message.reply_text(f"⚠️ Already monitoring @{username}!")
        return

    # Check initial status
    status = check_instagram_status(username)
    if not status["is_active"]:
        await update.message.reply_text(f"⚠️ @{username} is already banned or does not exist!")
        return

    await update.message.reply_text(f"⚡ **Super-fast ban monitoring active for @{username}!**", parse_mode="Markdown")

    start_time = time.time()

    async def ban_loop():
        while True:
            await asyncio.sleep(5)
            st = check_instagram_status(username)
            if not st["is_active"]:
                elapsed = format_duration(time.time() - start_time)
                alert_msg = (
                    f"🚨 **Super-Fast Ban Alert!**\n\n"
                    f"@{username} has been **BANNED/DISABLED**!\n"
                    f"Time elapsed: {elapsed}\n\n"
                    f"🔗 [View Profile](https://instagram.com/{username})"
                )
                await context.bot.send_message(chat_id=chat_id, text=alert_msg, parse_mode="Markdown", disable_web_page_preview=False)
                active_monitors.pop(task_key, None)
                break

    task = asyncio.create_task(ban_loop())
    active_monitors[task_key] = {"task": task, "mode": "ban", "start_time": start_time, "username": username}


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/stop <username>`", parse_mode="Markdown")
        return

    username = context.args[0].replace("@", "").strip().lower()
    chat_id = update.effective_chat.id
    task_key = f"{chat_id}_{username}"

    if task_key in active_monitors:
        active_monitors[task_key]["task"].cancel()
        active_monitors.pop(task_key, None)
        await update.message.reply_text(f"🛑 Stopped monitoring @{username}.")
    else:
        await update.message.reply_text(f"❌ Not currently monitoring @{username}.")


async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_tasks = [v for k, v in active_monitors.items() if k.startswith(f"{chat_id}_")]

    if not user_tasks:
        await update.message.reply_text("ℹ️ No active monitoring tasks.")
        return

    lines = ["📊 **Active Monitors:**\n"]
    for t in user_tasks:
        mode_icon = "✅ Unban" if t["mode"] == "unban" else "🚨 Ban"
        elapsed = format_duration(time.time() - t["start_time"])
        lines.append(f"• @{t['username']} ({mode_icon}) - Running for {elapsed}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN is missing in .env file!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("monitor", monitor_unban_cmd))
    app.add_handler(CommandHandler("banmonitor", monitor_ban_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("active", active_cmd))

    print("⚡ Truzd Monitor Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
